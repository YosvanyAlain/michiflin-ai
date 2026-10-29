"""Offline safety and rollback tests for the one-time relay procedure."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
from unittest import mock

import pytest

from tools import migrate_relay_once as relay


class CopyBackup:
    def __init__(self, folder: Path, fail_restore: str = "") -> None:
        self.folder, self.fail_restore, self.n = folder, fail_restore, 0
    def save(self, path: Path) -> None:
        self.n += 1
        shutil.copytree(path, self.folder / str(self.n))
    def restore(self, path: Path) -> None:
        if path.name == self.fail_restore:
            raise RuntimeError("synthetic restore failure")
        shutil.copytree(self.folder / str(self.n), path)


@pytest.fixture
def account(tmp_path):
    path = tmp_path / "state"
    (path / "accounts").mkdir(parents=True)
    (path / "address").write_text("old")
    (path / "private-history").write_text("keep me")
    backupdir = tmp_path / "backups"
    backupdir.mkdir()
    return path, backupdir


def fake_identity(path):
    primary = (path / "address").read_text()
    transports = {"old"} if primary == "old" else {"old", "new"}
    return {"primary": primary, "transports": transports, "fingerprint":"same-public-fingerprint", "chats":{1, 2}}


def fake_history(path):
    return {5: (path / "private-history").read_text()}


def fake_migrate(path, _before, _probe):
    (path / "address").write_text("new")
    return {"old":"old", "new":"new"}


def context(migrate=fake_migrate):
    return (mock.patch.object(relay, "identity", side_effect=fake_identity),
            mock.patch.object(relay, "history", side_effect=fake_history),
            mock.patch.object(relay, "blobs", return_value={"attachment":"same-digest"}),
            mock.patch.object(relay, "migrate_network", side_effect=migrate))


def test_migration_checkpoints_restore_and_idempotence(account):
    path, folder = account
    backup = CopyBackup(folder)
    a, b, c, d = context()
    with a, b, c, d as network:
        assert relay.migrate_once(path, backup) == "verified"
        assert relay.migrate_once(path, backup) == "verified"
    assert network.call_count == 1 and backup.n == 4
    assert (path / "address").read_text() == "new"
    assert (path / "private-history").read_text() == "keep me"
    assert json.loads((path / relay.MARKER).read_text())["status"] == "verified"
    assert json.loads((folder / "2" / relay.MARKER).read_text())["status"] == "started"
    assert json.loads((folder / "3" / relay.MARKER).read_text())["status"] == "pending"
    assert json.loads((folder / "4" / relay.MARKER).read_text())["status"] == "verified"


def test_network_failure_rolls_back_original_and_disables_automatic_retry(account):
    path, folder = account
    backup = CopyBackup(folder)
    def fail(path, *_):
        (path / "address").write_text("new")
        (path / "private-history").write_text("broken")
        raise RuntimeError("synthetic failure containing sensitive details")
    a, b, c, d = context(fail)
    with a, b, c, d as network:
        assert relay.migrate_once(path, backup) == "needs_attention"
        assert relay.migrate_once(path, backup) == "needs_attention"
    assert network.call_count == 1
    assert (path / "address").read_text() == "old"
    assert (path / "private-history").read_text() == "keep me"
    assert json.loads((folder / str(backup.n) / relay.MARKER).read_text())["status"] == "needs_attention"


def test_failed_new_backup_restore_rolls_back_instead_of_publishing_change(account):
    path, folder = account
    backup = CopyBackup(folder, fail_restore="after-recovered")
    a, b, c, d = context()
    with a, b, c, d:
        assert relay.migrate_once(path, backup) == "needs_attention"
    assert (path / "address").read_text() == "old"
    assert json.loads((path / relay.MARKER).read_text())["status"] == "needs_attention"
    assert (folder / str(backup.n) / "address").read_text() == "old"


def test_preflight_failure_does_not_attempt_registration_or_modify_account(account):
    path, folder = account
    backup = CopyBackup(folder, fail_restore="before-recovered")
    a, b, c, d = context()
    with a, b, c, d as network:
        assert relay.migrate_once(path, backup) == "preflight_failed"
    network.assert_not_called()
    assert (path / "address").read_text() == "old" and not (path / relay.MARKER).exists()


@pytest.mark.parametrize("status", ["started", "pending", "needs_attention"])
def test_interrupted_migration_never_registers_another_relay(account, status):
    path, folder = account
    relay.write_marker(path, status)
    backup = CopyBackup(folder)
    with mock.patch.object(relay, "migrate_network") as network:
        assert relay.migrate_once(path, backup) == "needs_attention"
    network.assert_not_called()
    assert backup.n == 0


def test_identity_verification_rejects_a_missing_original_blob(account):
    path, _ = account
    before = fake_identity(path)
    with mock.patch.object(relay, "identity", return_value=before), \
         mock.patch.object(relay, "history", return_value={1:"one"}), \
         mock.patch.object(relay, "blobs", return_value={}):
        with pytest.raises(relay.MigrationError, match="attachment"):
            relay.contains_baseline(path, before, {1:"one"}, {"old-file":"digest"})
