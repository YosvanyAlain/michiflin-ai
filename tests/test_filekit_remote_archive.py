"""Actual short-lived archive jail tests with synthetic files, never a live bot."""
import io
import os
import socket
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from michiflin import config, filekit, filekit_archive

FIXTURES = Path(__file__).parent / "fixtures" / "filekit"
REQUESTED = os.environ.get("MICHIFLIN_SANDBOX_TESTS") == "1"
ENABLED = REQUESTED and filekit_archive.available()


class SandboxToolchainTests(unittest.TestCase):
    def test_node_is_resolved_from_operator_path_not_system_only(self):
        def which(name, path=None):
            if name == "node":
                return "/opt/hostedtoolcache/node/22/bin/node" if path is None else None
            return "/usr/bin/" + name
        with mock.patch.object(filekit_archive.shutil, "which", side_effect=which):
            self.assertTrue(filekit_archive.available())

    @unittest.skipUnless(REQUESTED, "set MICHIFLIN_SANDBOX_TESTS=1 to require the toolchain")
    def test_requested_toolchain_is_present(self):
        self.assertTrue(filekit_archive.available(), "sandbox test flag is on but a required tool is missing")


@unittest.skipUnless(ENABLED, "set MICHIFLIN_SANDBOX_TESTS=1 with the Linux sandbox toolchain")
class RemoteArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="filekit-workspace-test-")
        self.addCleanup(self.tmp.cleanup)
        self.threshold = mock.patch.object(config, "MIN_FREE_DISK_BYTES", 0)
        self.threshold.start()
        self.addCleanup(self.threshold.stop)
        self.space = filekit.Workspace("owner@example.invalid", Path(self.tmp.name))

    def test_real_7z_zip_encrypted_parts_and_rar(self):
        self.space.put("", [{"path": "origen/foto.txt", "source": io.BytesIO(b"sintetico")}, {"path": "origen/vacia", "type": "dir"}])
        for fmt in ("7z", "zip"):
            result = filekit_archive.archive(self.space, "create", ["/origen"], folder="", name=f"salida.{fmt}",
                                             format=fmt, password="clave-sintetica", volume_bytes=128)
            parts = sorted(item["path"] for item in result["entries"] if item["path"].startswith(f"/salida.{fmt}."))
            self.assertGreaterEqual(len(parts), 2)
            with self.assertRaisesRegex(filekit.FileKitError, "wrong_password"):
                filekit_archive.archive(self.space, "extract", parts, folder="", name=f"error-{fmt}", first=parts[0], password="incorrecta")
            self.assertNotIn(f"/error-{fmt}", [x["path"] for x in self.space.view()["entries"]])
            filekit_archive.archive(self.space, "extract", parts, folder="", name=f"extraido-{fmt}", first=parts[0], password="clave-sintetica")
            self.assertEqual(self.space.content(f"/extraido-{fmt}/origen/foto.txt")[1].read_bytes(), b"sintetico")
            self.assertIn(f"/extraido-{fmt}/origen/vacia", [x["path"] for x in self.space.view()["entries"]])
        with (FIXTURES / "test_read_format_rar_encryption_data.rar").open("rb") as source:
            self.space.put("", [{"path": "ejemplo.rar", "source": source}])
        filekit_archive.archive(self.space, "extract", ["/ejemplo.rar"], folder="", name="rar", first="/ejemplo.rar", password="12345678")
        self.assertTrue(any(item["path"].startswith("/rar/") for item in self.space.view()["entries"]))

    def test_traversal_and_missing_isolation_fail_without_mutation(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as zf:
            zf.writestr("../fuera.txt", "no")
        data.seek(0)
        self.space.put("", [{"path": "inseguro.zip", "source": data}])
        before = self.space.view()
        with self.assertRaisesRegex(filekit.FileKitError, "unsafe_path"):
            filekit_archive.archive(self.space, "extract", ["/inseguro.zip"], folder="", name="salida", first="/inseguro.zip")
        self.assertEqual(self.space.view(), before)
        with mock.patch.object(filekit_archive, "available", return_value=False):
            with self.assertRaisesRegex(filekit.FileKitError, "isolation_unavailable"):
                filekit_archive.archive(self.space, "extract", ["/inseguro.zip"], folder="", name="salida", first="/inseguro.zip")
        self.assertEqual(self.space.view(), before)

    def test_sandbox_cannot_read_host_or_reach_host_loopback(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            with filekit_archive._temporary_jail() as base:
                work, jail, runner = base / "work", base / "jail", base / "runner.sh"
                work.mkdir(mode=0o700)
                jail.mkdir(mode=0o755)
                runner.write_text(filekit_archive.RUNNER, encoding="utf-8")
                runner.chmod(0o755)
                (work / "filekit_node.js").write_text(f"""
                  const fs=require('fs'), net=require('net');
                  const socket=net.createConnection({{host:'127.0.0.1', port:{port}}});
                  function done(reachable) {{
                    process.stdout.write(JSON.stringify({{ok:true,result:{{uid:process.getuid(),
                      hostVisible:fs.existsSync('/home/user/michiflin-ai'), procVisible:fs.existsSync('/proc/self/environ'),
                      inherited:!!process.env.FILEKIT_SYNTHETIC_SECRET, reachable}}}}));
                  }}
                  socket.setTimeout(1500,()=>{{socket.destroy();done(false)}});
                  socket.on('connect',()=>{{socket.destroy();done(true)}});
                  socket.on('error',()=>done(false));
                """, encoding="utf-8")
                with mock.patch.dict(os.environ, {"FILEKIT_SYNTHETIC_SECRET": "not-for-the-jail"}):
                    result = filekit_archive._run(work, jail, runner, {}, 16 * 1024 * 1024, 512 * 1024 * 1024)
                self.assertEqual(result, {"uid": 65533, "hostVisible": False, "procVisible": False, "inherited": False, "reachable": False})


if __name__ == "__main__":
    unittest.main()
