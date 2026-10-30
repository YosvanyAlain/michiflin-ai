"""Streaming raw-parts joins are atomic, ordered and scoped to one workspace."""
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from michiflin import config, filekit, filekit_join_remote, state


class RemoteRawJoinTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="filekit-raw-join-")
        self.addCleanup(self.temp.cleanup)
        threshold = mock.patch.object(config, "MIN_FREE_DISK_BYTES", 0)
        threshold.start()
        self.addCleanup(threshold.stop)
        self.space = filekit.Workspace("owner@example.invalid", Path(self.temp.name))
        self.space.mkdir("", "partes")
        self.space.put("/partes", [{"path": "video.rar.001", "source": io.BytesIO(b"uno")},
                                    {"path": "video.rar.002", "source": io.BytesIO(b"dos")},
                                    {"path": "video.rar.003", "source": io.BytesIO(b"tres")},
                                    {"path": "otro.rar.002", "source": io.BytesIO(b"otro")}])
        self.paths = [f"/partes/video.rar.{n:03d}" for n in (1, 2, 3)]

    def run_join(self, paths=None, name="video.rar", sha=""):
        return filekit_join_remote.join(self.space, paths or self.paths, folder="", name=name, sha256=sha)

    def test_order_hash_originals_collision_and_history(self):
        digest = hashlib.sha256(b"unodostres").hexdigest()
        self.run_join(list(reversed(self.paths)), sha=digest)
        self.assertEqual(self.space.content("/video.rar")[1].read_bytes(), b"unodostres")
        self.assertEqual(self.space.content(self.paths[0])[1].read_bytes(), b"uno")
        with self.assertRaisesRegex(filekit.FileKitError, "exists"):
            self.run_join()
        self.assertTrue(self.space.travel(True))
        self.assertNotIn("/video.rar", [item["path"] for item in self.space.view()["entries"]])
        self.assertTrue(self.space.travel(False))
        self.assertEqual(self.space.content("/video.rar")[1].read_bytes(), b"unodostres")

    def test_gaps_mixed_groups_hash_mismatch_and_maintenance_are_atomic(self):
        before = self.space.view()
        objects = {p.name for p in self.space.objects.iterdir()}
        for parts, code in [([self.paths[0], self.paths[2]], "join_missing_parts"),
                            ([self.paths[1], self.paths[2]], "join_missing_parts"),
                            ([self.paths[0], "/partes/otro.rar.002"], "join_parts"),
                            ([self.paths[0], self.paths[0]], "join_parts")]:
            with self.subTest(parts=parts), self.assertRaisesRegex(filekit.FileKitError, code):
                self.run_join(parts)
            self.assertEqual(self.space.view(), before)
        with self.assertRaisesRegex(filekit.FileKitError, "join_mismatch"):
            self.run_join(sha="a" * 64)
        self.assertEqual(self.space.view(), before)
        self.assertEqual({p.name for p in self.space.objects.iterdir()}, objects)
        with mock.patch.object(state, "check_work_allowed", side_effect=InterruptedError("maintenance")):
            with self.assertRaises(InterruptedError):
                self.run_join()
        self.assertEqual(self.space.view(), before)
        self.assertEqual({p.name for p in self.space.objects.iterdir()}, objects)

    def test_size_limit_and_unavailable_disk_do_not_publish(self):
        before = self.space.view()
        with mock.patch.object(config, "MAX_FILE_BYTES", 2):
            with self.assertRaisesRegex(filekit.FileKitError, "output_limit"):
                self.run_join()
        with mock.patch.object(filekit_join_remote.shutil, "disk_usage", return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(filekit.FileKitError, "disk_full"):
                self.run_join()
        self.assertEqual(self.space.view(), before)
