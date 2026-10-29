"""Tests for a temporary, per-person file workspace. No real user data is used."""
import io
import multiprocessing
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from michiflin import config, filekit, state


def multiprocess_writer(root, prefix):
    config.MIN_FREE_DISK_BYTES = 0
    space = filekit.Workspace("owner@example.invalid", Path(root))
    for index in range(15):
        space.mkdir("", f"{prefix}-{index}")


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.disk_threshold = mock.patch.object(config, "MIN_FREE_DISK_BYTES", 0)
        self.disk_threshold.start()
        self.addCleanup(self.disk_threshold.stop)
        self.temp = tempfile.TemporaryDirectory(prefix="michiflin-filekit-test-")
        self.root = Path(self.temp.name)
        self.space = filekit.Workspace("owner@example.invalid", self.root)
        self.other = filekit.Workspace("other@example.invalid", self.root)

    def tearDown(self):
        self.temp.cleanup()

    def add(self, path, contents=b"contenido"):
        self.space.put("", [{"path": path, "source": io.BytesIO(contents), "mime": "text/plain"}])

    def read(self, space, path):
        _entry, location = space.content(path)
        return location.read_bytes()

    def test_user_isolation_and_file_operations(self):
        self.space.mkdir("", "carpeta")
        self.space.put("/carpeta", [{"path": "sub/a.txt", "source": io.BytesIO(b"original")}, {"path": "vacia", "type": "dir"}])
        self.space.mkdir("", "destino")
        self.space.transfer(["/carpeta/sub"], "/destino", True)
        self.space.write("/destino/sub/a.txt", io.BytesIO(b"editado"))
        self.assertEqual(self.read(self.space, "/carpeta/sub/a.txt"), b"original")
        self.assertEqual(self.read(self.space, "/destino/sub/a.txt"), b"editado")
        self.assertTrue(self.space.travel(True))
        self.assertEqual(self.read(self.space, "/destino/sub/a.txt"), b"original")
        self.assertTrue(self.space.travel(False))
        self.space.rename("/destino/sub", "nuevo")
        self.space.transfer(["/destino/nuevo"], "/carpeta", False)
        self.space.remove(["/carpeta/nuevo"])
        self.assertTrue(self.space.travel(True))
        self.assertEqual(self.read(self.space, "/carpeta/nuevo/a.txt"), b"editado")
        self.assertIn("/carpeta/vacia", [row["path"] for row in self.space.view()["entries"]])
        self.assertEqual(self.other.view()["entries"], [])
        with self.assertRaisesRegex(filekit.FileKitError, "file_missing"):
            self.other.content("/carpeta/sub/a.txt")
        self.assertNotEqual(self.space.root, self.other.root)

    def test_hostile_paths_conflicts_and_interruption_are_atomic(self):
        self.space.mkdir("", "a")
        self.space.put("/a", [{"path": "ok", "source": io.BytesIO(b"ok")}])
        for path in ("../escape", "sub/../../escape", "/absolute", "foo\\bar", "line\nbreak"):
            with self.subTest(path=path), self.assertRaises(filekit.FileKitError):
                self.space.put("", [{"path": path, "source": io.BytesIO(b"bad")}])
        before_objects = {p.name for p in self.space.objects.iterdir()}
        with self.assertRaisesRegex(filekit.FileKitError, "exists"):
            self.space.put("/a", [{"path": "new", "source": io.BytesIO(b"new")}, {"path": "ok", "source": io.BytesIO(b"oops")}])
        self.assertEqual({p.name for p in self.space.objects.iterdir()}, before_objects)
        self.assertEqual(self.read(self.space, "/a/ok"), b"ok")
        self.assertNotIn("/a/new", [row["path"] for row in self.space.view()["entries"]])
        with self.assertRaisesRegex(filekit.FileKitError, "inside_source"):
            self.space.transfer(["/a"], "/a", False)
        with mock.patch.object(state, "check_work_allowed", side_effect=InterruptedError("pause")):
            with self.assertRaises(InterruptedError):
                self.add("no-se-guarda")
        self.assertEqual({p.name for p in self.space.objects.iterdir()}, before_objects)

    def test_original_files_symlinks_and_history_cleanup(self):
        original = self.root / "original.txt"
        original.write_text("no cambiar", encoding="utf-8")
        link = self.root / "link.txt"
        link.symlink_to(original)
        with self.assertRaises(filekit.FileKitError):
            filekit.open_source(link)
        with filekit.open_source(original) as source:
            self.space.put("", [{"path": "copia.txt", "source": source}])
        self.space.write("/copia.txt", io.BytesIO(b"otra version"))
        self.assertGreaterEqual(len(list(self.space.objects.iterdir())), 2)
        self.space.clear_history()
        self.assertFalse(self.space.view()["canUndo"])
        self.assertEqual(self.read(self.space, "/copia.txt"), b"otra version")
        self.assertEqual(len(list(self.space.objects.iterdir())), 1)
        self.assertEqual(original.read_text(encoding="utf-8"), "no cambiar")
        self.assertTrue(original.exists())

    def test_concurrent_transactions_and_content_limit(self):
        failures = []
        def make(index):
            try:
                self.space.mkdir("", f"carpeta-{index}")
            except Exception as exc:
                failures.append(exc)
        threads = [threading.Thread(target=make, args=(index,)) for index in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(failures, [])
        self.assertEqual(len(self.space.view()["entries"]), 20)
        with mock.patch.object(config, "MAX_FILE_BYTES", 4):
            with self.assertRaisesRegex(filekit.FileKitError, "input_limit"):
                self.add("grande", b"12345")
        self.assertEqual(len(self.space.view()["entries"]), 20)
        self.assertEqual(list(self.space.objects.iterdir()), [])

    def test_exclusive_lock_across_real_processes(self):
        context = multiprocessing.get_context("spawn")
        workers = [context.Process(target=multiprocess_writer, args=(str(self.root), f"p{number}")) for number in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(30)
            if worker.is_alive():
                worker.terminate()
                worker.join()
                self.fail("file workspace process did not finish")
            self.assertEqual(worker.exitcode, 0)
        self.assertEqual(len(self.space.view()["entries"]), 60)
        self.assertTrue(self.space.view()["canUndo"])

    def test_name_validation_and_database_path_never_escape(self):
        with self.assertRaises(filekit.FileKitError):
            filekit.Workspace("", self.root)
        for name in ("", ".", "..", "a/b", "a\\b", "\x00", "a" * 256):
            with self.subTest(name=name), self.assertRaises(filekit.FileKitError):
                self.space.mkdir("", name)
        self.assertTrue(self.space.root.is_relative_to(self.root))
        self.assertEqual(os.stat(self.space.root).st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
