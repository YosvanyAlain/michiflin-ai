"""Offline safeguards for returning the existing bot to GitHub Actions."""
from __future__ import annotations

import io
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import state_backup
from michiflin import app, chat, maintenance, miniapp, profile, state


class HandoverSafety(unittest.TestCase):
    def test_large_existing_blob_can_be_included_without_touching_original(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root) / "state"
            folder = base / "accounts" / "1" / "dc.db-blobs"
            folder.mkdir(parents=True)
            source = folder / "existing.bin"
            source.write_bytes(b"x" * 250_000)
            with mock.patch.dict(os.environ, {"STATE_BACKUP_INCLUDE_BLOBS": "0"}):
                partial = state_backup.archive(base)
            with tarfile.open(fileobj=io.BytesIO(partial), mode="r:gz") as tar:
                self.assertFalse(any(x.name.endswith("existing.bin") for x in tar))
            with mock.patch.dict(os.environ, {"STATE_BACKUP_INCLUDE_BLOBS": "1"}):
                full = state_backup.archive(base)
            with tarfile.open(fileobj=io.BytesIO(full), mode="r:gz") as tar:
                item = next(x for x in tar if x.name.endswith("existing.bin"))
                self.assertEqual(tar.extractfile(item).read(), source.read_bytes())
            self.assertEqual(source.stat().st_size, 250_000)

    def test_retain_all_disables_rotation_even_when_requested(self) -> None:
        env = {"STATE_BACKUP_RETAIN_ALL": "1", "STATE_BACKUP_STORAGE": "releases"}
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ, env), \
             mock.patch.object(state_backup, "configuration", return_value=("token", "repo/private", "pass")), \
             mock.patch.object(state_backup, "config_dir", return_value=Path(root) / "state"), \
             mock.patch.object(state_backup, "_save_release") as save:
            state_backup.save(rotate=True)
            self.assertEqual(save.call_args.kwargs, {"rotate": False})

    def test_quiet_handover_has_no_startup_broadcasts_or_automatic_apps(self) -> None:
        with mock.patch.object(maintenance, "silent", return_value=True), \
             mock.patch.object(maintenance, "silent_active", return_value=False), \
             mock.patch.object(miniapp, "_bound_apps", return_value=[{"build": "older"}]), \
             mock.patch.object(miniapp, "send_app") as send, \
             mock.patch.object(miniapp, "replace_app") as replace:
            miniapp.welcome(10, "es")
            app._bootstrap_invitees(None, 0)
            app._bootstrap_verified_owner(None, 0)
            app._bootstrap_email_recovery(None, 0)
            send.assert_not_called()
            replace.assert_not_called()

    def test_quiet_pause_clears_once_only_after_ready(self) -> None:
        order = []
        with mock.patch.dict(os.environ, {"MICHIFLIN_GITHUB_RESUME_ONCE": "1"}), \
             mock.patch.object(state, "flag", return_value=False), \
             mock.patch.object(maintenance, "silent_active", return_value=True), \
             mock.patch.object(chat, "ready", return_value=True), \
             mock.patch.object(maintenance, "get", return_value={"es": "pause", "en": "pause"}), \
             mock.patch.object(state, "set_flag", side_effect=lambda *a: order.append("marked")), \
             mock.patch.object(maintenance, "set_notice", side_effect=lambda *a, **k: order.append("ended")):
            app._resume_github_handover()
        self.assertEqual(order, ["marked", "ended"])
        with mock.patch.dict(os.environ, {"MICHIFLIN_GITHUB_RESUME_ONCE": "1"}), \
             mock.patch.object(state, "flag", return_value=True), \
             mock.patch.object(maintenance, "set_notice") as end:
            app._resume_github_handover()
            end.assert_not_called()

    def test_profile_does_not_request_a_second_app(self) -> None:
        value = profile.profile_status("custom\n" + profile.OLD_START_HINT)
        self.assertIn("custom", value)
        self.assertIn(profile.START_HINT, value)
        self.assertNotIn("/app", value)
        self.assertEqual(profile.profile_status(value), value)


if __name__ == "__main__":
    unittest.main()
