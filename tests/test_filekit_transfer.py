"""Attachment and completed-job imports are explicit, isolated and reversible."""
import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from michiflin import access, chat, config, filekit, filekit_transfer, state


class FileTransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="filekit-transfer-test-")
        self.addCleanup(self.tmp.cleanup)
        self.jobs = Path(self.tmp.name) / "jobs"
        self.jobs.mkdir()
        self.original = copy.deepcopy(state.data)
        self.addCleanup(self.restore)
        state.data["jobs"] = {}
        patches = [mock.patch.object(config, "JOB_ROOT", self.jobs), mock.patch.object(config, "MIN_FREE_DISK_BYTES", 0),
                   mock.patch.object(access, "job_allowed", return_value=True),
                   mock.patch.object(access, "session_stamp", return_value="synthetic-session"),
                   mock.patch.object(state, "work_interrupted", return_value=False)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(chat.set_sender, chat.sender())
        chat.set_sender("owner@example.invalid")
        self.space = filekit.Workspace("owner@example.invalid")
        filekit_transfer._pending.clear()
        self.addCleanup(filekit_transfer._pending.clear)

    def restore(self):
        with state.lock:
            state.data.clear()
            state.data.update(self.original)

    def make_job(self, status="done"):
        parts = self.jobs / "42" / "parts"
        parts.mkdir(parents=True)
        data = b"contenido sintetico"
        path = parts / "archivo.txt"
        path.write_bytes(data)
        job = {"id": "42", "addr": "owner@example.invalid", "status": status,
               "size": len(data), "sha": hashlib.sha256(data).hexdigest(), "part_files": [str(path)]}
        state.data["jobs"]["42"] = job
        return job, path, data

    def test_completed_job_import_preserves_source_and_isolated_users(self):
        _job, original, data = self.make_job()
        filekit_transfer.import_job(self.space, "42", "", "Descarga 42")
        self.assertEqual(self.space.content("/Descarga 42/archivo.txt")[1].read_bytes(), data)
        self.assertEqual(original.read_bytes(), data)
        self.space.travel(True)
        self.assertEqual(self.space.view()["entries"], [])
        chat.set_sender("other@example.invalid")
        with self.assertRaisesRegex(filekit.FileKitError, "not_authorized"):
            filekit_transfer.import_job(self.space, "42", "", "otra")
        other = filekit.Workspace("other@example.invalid")
        with self.assertRaisesRegex(filekit.FileKitError, "missing"):
            filekit_transfer.import_job(other, "42", "", "otra")
        self.assertEqual(other.view()["entries"], [])

    def test_job_active_modified_external_path_and_symlink_rejected(self):
        job, source, _ = self.make_job("sending")
        with self.assertRaisesRegex(filekit.FileKitError, "job_active"):
            filekit_transfer.import_job(self.space, "42", "", "carpeta")
        job["status"] = "done"
        source.write_bytes(b"bytes cambiados")
        with self.assertRaisesRegex(filekit.FileKitError, "source_changed"):
            filekit_transfer.import_job(self.space, "42", "", "carpeta")
        external = Path(self.tmp.name) / "external.txt"
        external.write_text("no tocar")
        job["part_files"] = [str(external)]
        with self.assertRaisesRegex(filekit.FileKitError, "unsafe_path"):
            filekit_transfer.import_job(self.space, "42", "", "carpeta")
        source.unlink()
        source.symlink_to(external)
        job["part_files"] = [str(source)]
        with self.assertRaises(filekit.FileKitError):
            filekit_transfer.import_job(self.space, "42", "", "carpeta")
        self.assertEqual(external.read_text(), "no tocar")
        self.assertEqual(self.space.view()["entries"], [])

    def test_playable_video_segments_use_part_hashes_not_original_byte_hash(self):
        job, original, _ = self.make_job()
        second = original.with_name("video (2-2).mp4")
        first = original.with_name("video (1-2).mp4")
        original.rename(first)
        first.write_bytes(b"segmento remux uno")
        second.write_bytes(b"segmento remux dos")
        job.update(kind="video", part_format="segments", size=5, sha=hashlib.sha256(b"fuente original diferente").hexdigest(),
                   part_files=[str(first), str(second)], part_sizes=[first.stat().st_size, second.stat().st_size],
                   part_sha=[hashlib.sha256(first.read_bytes()).hexdigest(), hashlib.sha256(second.read_bytes()).hexdigest()])
        filekit_transfer.import_job(self.space, "42", "", "Video")
        self.assertEqual(self.space.content("/Video/video (1-2).mp4")[1].read_bytes(), first.read_bytes())
        self.assertEqual(self.space.content("/Video/video (2-2).mp4")[1].read_bytes(), second.read_bytes())
        for format_name in ("zip", "7z"):
            job["part_format"] = format_name
            filekit_transfer.import_job(self.space, "42", "", f"Archivo-{format_name}")
            self.assertEqual(self.space.content(f"/Archivo-{format_name}/video (2-2).mp4")[1].read_bytes(), second.read_bytes())
        job["part_sha"] = []
        with self.assertRaisesRegex(filekit.FileKitError, "integrity_missing"):
            filekit_transfer.import_job(self.space, "42", "", "Sin huellas")
        self.assertTrue(first.is_file() and second.is_file())

    def test_attachment_token_is_single_use_sender_bound_and_copies_only(self):
        data = b"adjunto sintetico"
        attachment = Path(self.tmp.name) / "attachment.bin"
        attachment.write_bytes(data)
        draft = filekit_transfer.prepare_import("owner@example.invalid", "", "movil.txt", len(data), hashlib.sha256(data).hexdigest())
        self.assertTrue(filekit_transfer.is_import_caption(draft["caption"]))
        message = SimpleNamespace(file=str(attachment), file_mime="text/plain")
        with self.assertRaisesRegex(filekit.FileKitError, "import_expired"):
            filekit_transfer.receive("other@example.invalid", message, draft["caption"])
        filekit_transfer.receive("owner@example.invalid", message, draft["caption"])
        self.assertEqual(self.space.content("/movil.txt")[1].read_bytes(), data)
        self.assertEqual(attachment.read_bytes(), data)
        with self.assertRaisesRegex(filekit.FileKitError, "import_expired"):
            filekit_transfer.receive("owner@example.invalid", message, draft["caption"])
        self.assertEqual(len(self.space.view()["entries"]), 1)

    def test_attachment_token_expires_and_size_mismatch_keeps_original(self):
        attachment = Path(self.tmp.name) / "attachment.bin"
        attachment.write_bytes(b"x")
        draft = filekit_transfer.prepare_import("owner@example.invalid", "", "x.txt", 2, hashlib.sha256(b"xx").hexdigest())
        msg = SimpleNamespace(file=str(attachment), file_mime="text/plain")
        with self.assertRaisesRegex(filekit.FileKitError, "source_changed"):
            filekit_transfer.receive("owner@example.invalid", msg, draft["caption"])
        self.assertEqual(attachment.read_bytes(), b"x")
        self.assertEqual(self.space.view()["entries"], [])
        # Same-size substitution is rejected too; the token remains retryable.
        attachment.write_bytes(b"zz")
        with self.assertRaisesRegex(filekit.FileKitError, "source_changed"):
            filekit_transfer.receive("owner@example.invalid", msg, draft["caption"])
        self.assertEqual(attachment.read_bytes(), b"zz")
        self.assertEqual(self.space.view()["entries"], [])
        token = draft["caption"].removeprefix(filekit_transfer.MARKER)
        filekit_transfer._pending[token]["expires"] = 0
        with self.assertRaisesRegex(filekit.FileKitError, "import_expired"):
            filekit_transfer.receive("owner@example.invalid", msg, draft["caption"])


if __name__ == "__main__":
    unittest.main()
