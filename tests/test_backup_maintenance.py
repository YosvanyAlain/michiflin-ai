"""Backup and housekeeping safety tests; no real user data or accounts."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sqlite3
import tarfile
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import state_backup as backup


def asset(number: int, when: datetime, *, pending: bool = False, name: str | None = None) -> dict:
    prefix = backup.PENDING_PREFIX if pending else backup.ASSET_PREFIX
    return {"id": number, "name": name or (prefix + when.strftime("%Y%m%dT%H%M%SZ") + f"-{number:016x}.bin"),
            "state": "uploaded", "created_at": when.isoformat().replace("+00:00", "Z")}


class ReleaseRetentionTests(unittest.TestCase):
    def test_retention_is_bounded_and_keeps_recent_days_and_weeks(self):
        now = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
        items = [asset(i + 1, now - timedelta(hours=i * 2)) for i in range(30 * 12)]
        items.append(asset(999, now, name="someone-elses-important-file.bin"))
        kept = backup.retention_ids(items)
        self.assertLessEqual(len(kept), backup.KEEP_RECENT + backup.KEEP_DAILY + backup.KEEP_WEEKLY)
        self.assertTrue({x["id"] for x in items[:backup.KEEP_RECENT]}.issubset(kept))
        self.assertNotIn(999, kept)
        self.assertGreaterEqual(len({backup._created(x).date() for x in items if x["id"] in kept}), 7)
        self.assertGreaterEqual(len({backup._created(x).isocalendar()[:2] for x in items if x["id"] in kept}), 4)

    def test_prune_requires_two_independently_verified_copies(self):
        now = datetime.now(timezone.utc)
        items = [asset(i + 1, now - timedelta(days=i)) for i in range(30)]
        with mock.patch.object(backup, "_assets", return_value=items), \
             mock.patch.object(backup, "_download_asset", return_value=b"corrupt"), \
             mock.patch.object(backup.requests, "delete") as delete:
            backup._prune("token", "o/r", 7, items[0]["id"], "pass")
        delete.assert_not_called()

    def test_only_owned_old_assets_are_deleted_after_verification(self):
        now = datetime.now(timezone.utc)
        items = [asset(i + 1, now - timedelta(days=i)) for i in range(30)]
        items += [asset(100, now - timedelta(days=3), pending=True),
                  asset(101, now - timedelta(hours=2), pending=True),
                  asset(102, now - timedelta(days=100), name="another-backup.bin")]
        with mock.patch.object(backup, "_assets", return_value=items), \
             mock.patch.object(backup, "_download_asset", return_value=b"encrypted"), \
             mock.patch.object(backup, "decrypt_backup", return_value=b"plain"), \
             mock.patch.object(backup, "_verify_plaintext"), \
             mock.patch.object(backup.requests, "delete", return_value=mock.Mock(status_code=204)) as delete:
            backup._prune("token", "o/r", 7, items[0]["id"], "pass")
        deleted = {int(call.args[0].rsplit("/", 1)[-1]) for call in delete.call_args_list}
        self.assertIn(100, deleted)
        self.assertNotIn(101, deleted)
        self.assertNotIn(102, deleted)
        self.assertNotIn(items[0]["id"], deleted)
        self.assertTrue(deleted)

    def test_network_failure_never_invokes_deletion(self):
        now = datetime.now(timezone.utc)
        items = [asset(i + 1, now - timedelta(days=i)) for i in range(25)]
        with mock.patch.object(backup, "_assets", return_value=items), \
             mock.patch.object(backup, "_download_asset", side_effect=backup.requests.Timeout), \
             mock.patch.object(backup.requests, "delete") as delete, self.assertRaises(backup.requests.Timeout):
            backup._prune("t", "o/r", 7, 1, "pass")
        delete.assert_not_called()

    def test_invalid_new_upload_is_not_marked_or_rotated(self):
        response = mock.Mock(status_code=201, json=lambda: {"id": 123}, raise_for_status=lambda: None)
        with mock.patch.object(backup, "_assert_private"), mock.patch.object(backup, "archive", return_value=b"data"), \
             mock.patch.object(backup, "_ensure_release", return_value={"id": 7}), \
             mock.patch.object(backup.requests, "post", return_value=response), \
             mock.patch.object(backup, "_download_asset", return_value=b"different"), \
             mock.patch.object(backup.requests, "patch") as patch, \
             mock.patch.object(backup, "_prune") as prune, self.assertRaises(ValueError):
            backup._save_release("t", "o/r", "pass")
        patch.assert_not_called()
        prune.assert_not_called()

    def test_public_repository_is_rejected(self):
        response = mock.Mock(json=lambda: {"private": False, "archived": False}, raise_for_status=lambda: None)
        with mock.patch.object(backup.requests, "get", return_value=response), self.assertRaises(ValueError):
            backup._assert_private("t", "o/r")


class BackupRestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t", "STATE_BACKUP_REPO": "o/r",
             "STATE_BACKUP_PASSPHRASE": "passphrase", "CONFIG_DIR": str(self.root / "target"),
             "STATE_BACKUP_STORAGE": "releases", "STATE_BACKUP_REQUIRED": "1"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_bootstrap_uses_legacy_only_before_marker(self):
        with mock.patch.object(backup, "_get_ref", return_value=None), mock.patch.object(backup, "_release", return_value=None), \
             mock.patch.object(backup, "_restore_legacy") as legacy:
            backup.restore()
            legacy.assert_called_once()

    def test_missing_release_never_falls_back_after_ready(self):
        with mock.patch.object(backup, "_get_ref", return_value="abc"), mock.patch.object(backup, "_release", return_value=None), \
             mock.patch.object(backup, "_restore_legacy") as legacy, self.assertRaises(RuntimeError):
            backup.restore()
        legacy.assert_not_called()
        self.assertFalse((self.root / "target").exists())

    def test_mismatched_marker_never_restores(self):
        with mock.patch.object(backup, "_get_ref", side_effect=["one", "two"]), \
             mock.patch.object(backup, "_release", return_value={"id": 1}), self.assertRaises(RuntimeError):
            backup.restore()

    def test_existing_state_is_not_overwritten(self):
        target = self.root / "target"
        target.mkdir(); (target / "keep").write_text("local")
        with mock.patch.object(backup, "_get_ref") as call:
            backup.restore()
        call.assert_not_called()
        self.assertEqual((target / "keep").read_text(), "local")

    def test_roundtrip_and_manifest_detects_missing_or_invalid_database(self):
        source = self.root / "src"
        folder = source / "accounts" / "a"
        folder.mkdir(parents=True)
        (source / "accounts" / "accounts.toml").write_text('[[accounts]]\ndir = "a"\n')
        with closing(sqlite3.connect(folder / "dc.db")) as db:
            db.execute("create table example (id int)")
            db.execute("insert into example values (42)")
            db.commit()
        (source / "michiflin-state.json").write_text(json.dumps({"v": 42}))
        plaintext = backup.archive(source)
        salt = os.urandom(backup.SALT_BYTES)
        encrypted = backup.MAGIC + salt + backup.fernet_from("passphrase", salt).encrypt(plaintext)
        backup._verify_plaintext(backup.decrypt_backup(encrypted, "passphrase"))
        backup._install_restored(plaintext, self.root / "target")
        self.assertEqual(json.loads((self.root / "target" / "michiflin-state.json").read_text())["v"], 42)
        with self.assertRaises(ValueError):
            backup.decrypt_backup(encrypted, "wrong passphrase")
        (folder / "dc.db").write_bytes(b"corrupted")
        with self.assertRaises(ValueError):
            backup._verify_plaintext(backup.archive(source))

    def test_unsafe_tar_paths_and_links_are_rejected(self):
        for name, link in (("../outside", False), ("michiflin/link", True)):
            output = io.BytesIO()
            with tarfile.open(fileobj=output, mode="w:gz") as archive:
                info = tarfile.TarInfo(name)
                if link:
                    info.type = tarfile.SYMTYPE; info.linkname = "../../outside"
                archive.addfile(info)
            with self.assertRaises(ValueError):
                backup._verify_plaintext(output.getvalue())

    def test_save_lock_serializes_overlapping_writers(self):
        intervals = []
        def worker():
            with backup._save_lock():
                start = time.monotonic(); time.sleep(0.06); intervals.append((start, time.monotonic()))
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(len(intervals), 2)
        self.assertGreaterEqual(intervals[1][0], intervals[0][1])


class ActionLogsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parent.parent / ".github/scripts/trim_action_logs.py"
        spec = importlib.util.spec_from_file_location("trim_action_logs", path)
        assert spec and spec.loader
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_only_completed_old_logs_after_latest_ten_are_deleted(self):
        now = datetime.now(timezone.utc)
        runs = [{"id": i + 1, "workflow_id": 9, "status": "completed",
                 "created_at": (now - timedelta(days=40 + i)).isoformat()} for i in range(15)]
        runs += [{"id": 888, "workflow_id": 9, "status": "in_progress", "created_at": (now - timedelta(days=100)).isoformat()},
                 {"id": 999, "workflow_id": 10, "status": "completed", "created_at": (now - timedelta(days=100)).isoformat()}]
        response = mock.Mock(json=lambda: {"workflow_runs": runs, "total_count": len(runs)}, raise_for_status=lambda: None)
        with mock.patch.object(self.module.requests, "get", return_value=response), \
             mock.patch.object(self.module.requests, "delete", return_value=mock.Mock(status_code=204)) as delete:
            eligible, removed = self.module.cleanup("t", "o/r")
        self.assertEqual((eligible, removed), (5, 5))
        self.assertEqual({int(c.args[0].split("/")[-2]) for c in delete.call_args_list}, set(range(11, 16)))

    def test_partial_listing_never_deletes(self):
        batch = [{"id": i, "workflow_id": 1, "status": "completed", "created_at": "2020-01-01T00:00:00Z"} for i in range(100)]
        response = mock.Mock(json=lambda: {"workflow_runs": batch, "total_count": 1200}, raise_for_status=lambda: None)
        with mock.patch.object(self.module.requests, "get", return_value=response), mock.patch.object(self.module.requests, "delete") as delete:
            self.assertEqual(self.module.cleanup("t", "o/r"), (0, 0))
        delete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
