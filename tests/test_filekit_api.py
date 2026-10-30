"""Synthetic integration of the WebXDC operation with authenticated workspaces."""
import base64
import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from michiflin import chat, config, filekit, filekit_api, filekit_crypto, filekit_transfer, miniapp, state


def enc(data):
    return base64.b64encode(data).decode()


def public(private):
    return private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def seal(text):
    info = filekit_crypto.public_key()
    private = X25519PrivateKey.generate()
    shared = private.exchange(X25519PublicKey.from_public_bytes(base64.b64decode(info["publicKey"])))
    nonce = b"\x42" * 12
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=nonce, info=b"michiflin-filekit-password-v1").derive(shared)
    ciphertext = ChaCha20Poly1305(key).encrypt(nonce, text.encode(), b"michiflin-filekit-password-v1|" + info["keyId"].encode())
    return {"keyId": info["keyId"], "ephemeral": enc(public(private)), "nonce": enc(nonce), "ciphertext": enc(ciphertext)}


def decrypt_reply(private, envelope):
    peer = X25519PublicKey.from_public_bytes(base64.b64decode(envelope["ephemeral"]))
    nonce = base64.b64decode(envelope["nonce"])
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=nonce, info=b"michiflin-filekit-reply-v1").derive(private.exchange(peer))
    return ChaCha20Poly1305(key).decrypt(nonce, base64.b64decode(envelope["ciphertext"]), b"michiflin-filekit-reply-v1|" + enc(public(private)).encode())


class FileKitApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="filekit-api-test-")
        self.addCleanup(self.tmp.cleanup)
        self.old_state = copy.deepcopy(state.data)
        self.addCleanup(self.restore)
        for patch in (mock.patch.object(config, "JOB_ROOT", Path(self.tmp.name) / "jobs"),
                      mock.patch.object(config, "MIN_FREE_DISK_BYTES", 0), mock.patch.object(state, "check_work_allowed")):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(chat.set_sender, chat.sender())
        chat.set_sender("owner@example.invalid")
        self.space = filekit.Workspace("owner@example.invalid")
        self.space.put("", [{"path": "doc.txt", "source": io.BytesIO(b"antes"), "mime": "text/plain"}])

    def restore(self):
        with state.lock:
            state.data.clear()
            state.data.update(self.old_state)
        filekit_transfer._pending.clear()

    def call(self, action, **args):
        return miniapp.op_filekit(31, {"action": action, **args}, "es")

    def test_list_mutations_history_and_sender_isolation(self):
        view = self.call("list")
        self.assertEqual([item["path"] for item in view["entries"]], ["/doc.txt"])
        self.assertIn("publicKey", view["key"])
        self.call("mkdir", folder="", name="carpeta")
        self.call("copy", paths=["/doc.txt"], folder="/carpeta")
        self.call("rename", path="/carpeta/doc.txt", name="copia.txt")
        self.call("delete", paths=["/carpeta/copia.txt"])
        self.call("undo")
        self.assertIn("/carpeta/copia.txt", [entry["path"] for entry in self.call("list")["entries"]])
        self.call("redo")
        chat.set_sender("other@example.invalid")
        self.assertEqual(self.call("list")["entries"], [])
        with self.assertRaises(miniapp.AppError):
            self.call("delete", paths=["/doc.txt"])
        chat.set_sender("owner@example.invalid")
        self.assertEqual(self.space.content("/doc.txt")[1].read_bytes(), b"antes")

    def test_bulk_rename_api_and_rollback(self):
        self.space.put("", [{"path": "segundo.txt", "source": io.BytesIO(b"dos")}])
        self.call("rename_many", changes=[{"path": "/doc.txt", "name": "segundo.txt"},
                                          {"path": "/segundo.txt", "name": "doc.txt"}])
        self.assertEqual(self.space.content("/doc.txt")[1].read_bytes(), b"dos")
        before = self.space.view()
        with self.assertRaises(miniapp.AppError):
            self.call("rename_many", changes=[{"path": "/doc.txt", "name": "x"}, {"path": "/segundo.txt", "name": "x"}])
        self.assertEqual(self.space.view(), before)
        self.call("undo")
        self.assertEqual(self.space.content("/doc.txt")[1].read_bytes(), b"antes")

    def test_encrypted_read_edit_and_concurrent_write_guard(self):
        private = X25519PrivateKey.generate()
        result = self.call("read", path="/doc.txt", replyKey=enc(public(private)))
        self.assertNotIn("antes", str(result))
        self.assertEqual(decrypt_reply(private, result["sealed"]), b"antes")
        encrypted = seal("después\ntexto")
        self.call("write", path="/doc.txt", version=result["version"], sealed=encrypted)
        self.assertEqual(self.space.content("/doc.txt")[1].read_bytes(), "después\ntexto".encode())
        with self.assertRaisesRegex(miniapp.AppError, "otra ventana"):
            self.call("write", path="/doc.txt", version=result["version"], sealed=seal("perdida"))
        self.assertEqual(self.space.content("/doc.txt")[1].read_bytes(), "después\ntexto".encode())
        self.space.put("", [{"path": "binary.dat", "source": io.BytesIO(b"\xff\x00")}])
        item, _ = self.space.content("/binary.dat")
        with self.assertRaisesRegex(miniapp.AppError, "UTF-8"):
            self.call("write", path="/binary.dat", version=item["blob"], sealed=seal("incorrecto"))
        self.assertEqual(self.space.content("/binary.dat")[1].read_bytes(), b"\xff\x00")

    def test_remote_raw_join_keeps_full_hash_encrypted(self):
        self.space.put("", [{"path": "unido.rar.001", "source": io.BytesIO(b"uno")},
                            {"path": "unido.rar.002", "source": io.BytesIO(b"dos")}])
        digest = hashlib.sha256(b"unodos").hexdigest()
        args = {"paths": ["/unido.rar.002", "/unido.rar.001"], "folder": "", "name": "unido.rar",
                "sealed": seal(json.dumps({"sha256": digest}))}
        self.assertNotIn(digest, str(args))
        self.call("join_raw", **args)
        self.assertEqual(self.space.content('/unido.rar')[1].read_bytes(), b"unodos")
        with self.assertRaises(miniapp.AppError):
            self.call("join_raw", paths=args["paths"], folder="", name="otro.rar", sha256=digest)

    def test_fast_rar_listing_is_delayed_for_existing_clients_and_can_pause(self):
        private = X25519PrivateKey.generate()
        listing = {"entries": [{"path": "a.txt", "size": 1, "type": "file"}], "version": "v", "solid": False}
        request = {"paths": ["/doc.txt"], "first": "/doc.txt", "replyKey": enc(public(private))}
        with mock.patch.object(filekit_api.filekit_rar_native, "inspect", return_value=listing), \
             mock.patch.object(filekit_api, "monotonic", side_effect=[100.0, 100.1]), \
             mock.patch.object(filekit_api, "sleep") as wait:
            result = self.call("rar_list", **request)
            self.assertEqual(json.loads(decrypt_reply(private, result["sealed"])), listing)
            self.assertAlmostEqual(wait.call_args.args[0], 0.25)
        # Slow listings already outlive the transition and need no extra delay.
        with mock.patch.object(filekit_api.filekit_rar_native, "inspect", return_value=listing), \
             mock.patch.object(filekit_api, "monotonic", side_effect=[100.0, 101.0]), \
             mock.patch.object(filekit_api, "sleep") as wait:
            self.call("rar_list", **request)
            wait.assert_not_called()
        # Maintenance/cancellation is checked after any waiting, before returning data.
        with mock.patch.object(filekit_api.filekit_rar_native, "inspect", return_value=listing), \
             mock.patch.object(filekit_api, "monotonic", side_effect=[100.0, 100.1]), \
             mock.patch.object(filekit_api, "sleep"), \
             mock.patch.object(state, "check_work_allowed", side_effect=state.WorkPaused(maintenance=True)):
            with self.assertRaises(state.WorkPaused):
                self.call("rar_list", **request)

    def test_selective_rar_listing_and_selection_are_encrypted(self):
        private = X25519PrivateKey.generate()
        listing = {"entries": [{"path": "nombre-privado.txt", "size": 18, "type": "file", "encrypted": True}],
                   "version": "a" * 64, "solid": True, "multiVolume": False}
        with mock.patch.object(filekit_api.filekit_rar_native, "inspect", return_value=listing) as inspect, \
             mock.patch.object(filekit_api.filekit_rar_native, "extract", return_value={}) as extract:
            result = self.call("rar_list", paths=["/doc.txt"], first="/doc.txt",
                               secret=seal("clave-sintetica"), replyKey=enc(public(private)))
            self.assertNotIn("nombre-privado", str(result))
            self.assertNotIn("clave-sintetica", str(result))
            self.assertEqual(json.loads(decrypt_reply(private, result["sealed"])), listing)
            self.assertEqual(inspect.call_args.args[1:], (["/doc.txt"], "/doc.txt", "clave-sintetica"))
            choice = json.dumps({"members": ["nombre-privado.txt"], "version": listing["version"]})
            request = {"paths": ["/doc.txt"], "first": "/doc.txt", "folder": "", "name": "salida",
                       "secret": seal("clave-sintetica"), "sealed": seal(choice)}
            self.assertNotIn("nombre-privado", str(request))
            self.assertNotIn("clave-sintetica", str(request))
            self.assertEqual(self.call("rar_extract", **request), {"changed": True})
            self.assertEqual(extract.call_args.args[4:], (["nombre-privado.txt"], listing["version"]))
            with self.assertRaises(miniapp.AppError):
                self.call("rar_extract", paths=["/doc.txt"], first="/doc.txt", folder="", name="salida",
                          members=["nombre-privado.txt"], version=listing["version"])
            with self.assertRaises(miniapp.AppError):
                self.call("rar_list", paths=["/doc.txt"], first="/doc.txt", password="clave-en-claro",
                          replyKey=enc(public(private)))

    def test_prepare_import_and_queue_handoff_are_explicit(self):
        draft = self.call("prepare_import", folder="", name="copia.txt", size=5, secret=seal(hashlib.sha256(b"antes").hexdigest()))
        self.assertTrue(filekit_transfer.is_import_caption(draft["caption"]))
        with mock.patch.object(filekit_api.queue, "enqueue_local", return_value=({"id": "55"}, "")) as enqueue:
            self.assertEqual(self.call("send", path="/doc.txt", partMiB=4), {"job": "55"})
            call = enqueue.call_args
            self.assertEqual(call.args[0], 31)
            self.assertEqual(call.args[2:], ("doc.txt", 5, "text/plain", "es", 4))
        with self.assertRaisesRegex(miniapp.AppError, "divídela"):
            self.call("prepare_import", folder="", name="muy-grande", size=21 * 1024 * 1024)
        with self.assertRaisesRegex(miniapp.AppError, "contraseña"):
            self.call("archive_create", paths=["/doc.txt"], name="x.zip", format="zip", password="not-allowed")

    def test_import_job_requires_verified_fragments(self):
        parts = config.JOB_ROOT / "42" / "parts"
        parts.mkdir(parents=True)
        first, second = parts / "x.001", parts / "x.002"
        first.write_bytes(b"abc")
        second.write_bytes(b"def")
        job = {"id": "42", "addr": "owner@example.invalid", "status": "done", "size": 6,
               "part_files": [str(first), str(second)], "part_sizes": [3, 3]}
        state.data["jobs"] = {"42": job}
        with mock.patch.object(filekit_transfer.queue.access, "job_allowed", return_value=True):
            with self.assertRaisesRegex(filekit.FileKitError, "integrity_missing"):
                filekit_transfer.import_job(self.space, "42", "", "sin-huella")
            job["sha"] = hashlib.sha256(b"abcdef").hexdigest()
            second.write_bytes(b"BAD")
            with self.assertRaisesRegex(filekit.FileKitError, "source_changed"):
                filekit_transfer.import_job(self.space, "42", "", "incorrecta")
            self.assertFalse(any(x["path"].startswith("/incorrecta") for x in self.space.view()["entries"]))
            second.write_bytes(b"def")
            filekit_transfer.import_job(self.space, "42", "", "correcta")
        self.assertEqual(self.space.content("/correcta/x.001")[1].read_bytes(), b"abc")
        self.assertEqual(self.space.content("/correcta/x.002")[1].read_bytes(), b"def")
        self.assertEqual(first.read_bytes(), b"abc")
        self.assertEqual(second.read_bytes(), b"def")


if __name__ == "__main__":
    unittest.main()
