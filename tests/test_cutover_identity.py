"""Offline transaction and downgrade tests for the verified identity cutover."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
from unittest import mock

import pytest

from tools import cutover_identity_once as cutover

OWNER = "owner@example.invalid"
FP_OLD = "A" * 40
FP_NEW = "B" * 40
FP_OWNER = "C" * 40
META = {"verified": True, "owner_address": OWNER, "owner_fingerprint": FP_OWNER,
        "bot_fingerprint": FP_NEW, "chat_id": 24}
REVISION = "d" * 40


class FakeStorage:
    def __init__(self, root: Path, fail: str = "") -> None:
        self.root, self.fail, self.seal_state = root, fail, False
        self.backups: list[Path] = []
        self.pinned = []
    def sealed(self):
        return self.seal_state
    def seal(self):
        if self.fail == "seal":
            raise RuntimeError("private seal failed")
        self.seal_state = True
    def pin_legacy(self, raw):
        if self.fail == "pin":
            raise RuntimeError("pin failed")
        self.pinned.append(raw)
        return 7
    def save(self, path):
        if self.fail == "save" and not self.backups:
            raise RuntimeError("new save failed")
        out = self.root / f"snapshot-{len(self.backups)}"
        shutil.copytree(path, out)
        self.backups.append(out)
    def restore(self, path):
        if self.fail == "restore":
            raise RuntimeError("new restore failed")
        shutil.copytree(self.backups[-1], path)


@pytest.fixture
def work(tmp_path):
    active, candidate, copies = (tmp_path / name for name in ("active", "candidate", "copies"))
    for path in (active, candidate):
        (path / "accounts").mkdir(parents=True)
    copies.mkdir()
    (active / "accounts" / "profile").write_text("old")
    (candidate / "accounts" / "profile").write_text("new")
    (active / "accounts" / "huge-blob").write_bytes(b"A" * 300_000)
    (active / "michiflin-state.json").write_text(json.dumps({
        "version": 5, "prefs": {OWNER: {"language": "es"}},
        "notes": {OWNER: [{"text": "private note"}]},
        "jobs": {"4": {"addr": OWNER, "chat": 11, "epoch": "old", "todo": [1]}},
        "apps": {"8": {"chat": 11, "addr": OWNER}},
        "app_seen": {"old-request": [1, 2]}, "inbox": {"11": 0},
    }))
    return active, candidate, copies


def fake_identity(path):
    name = (path / "accounts" / "profile").read_text()
    return {"primary": name, "transports": {name}, "fingerprint": FP_OLD if name == "old" else FP_NEW, "chats": {1}}


def fake_validate(path, metadata, expected_owner, **_kw):
    if metadata != META or expected_owner != OWNER:
        raise cutover.CutoverError("owner mismatch")
    return fake_identity(path)


def fake_archive(path):
    # Synthetic bytes stand in for a separately verified full legacy archive.
    assert (path / "accounts" / "huge-blob").stat().st_size > 200_000
    return b"full-old-archive"


def run(work, storage):
    active, candidate, _ = work
    def restore_old(_raw, target):
        shutil.copytree(active, target)
    with mock.patch.object(cutover.relay, "identity", side_effect=fake_identity), \
         mock.patch.object(cutover.relay, "history", return_value={1: "digest"}), \
         mock.patch.object(cutover.relay, "blobs", return_value={"large": "digest"}), \
         mock.patch.object(cutover.relay, "contains_baseline", return_value=True), \
         mock.patch.object(cutover, "validate_candidate", side_effect=fake_validate), \
         mock.patch.object(cutover, "mark_probe_seen"), \
         mock.patch.object(cutover, "full_legacy_archive", side_effect=fake_archive), \
         mock.patch.object(cutover.state_backup, "_install_restored", side_effect=restore_old):
        return cutover.transition(active, candidate, META, storage, OWNER, REVISION)


def test_cutover_keeps_owner_data_while_disabling_old_bindings(work):
    active, _, copies = work
    storage = FakeStorage(copies)
    assert run(work, storage) == "verified"
    assert storage.sealed() and storage.pinned == [b"full-old-archive"]
    assert (active / "accounts" / "profile").read_text() == "new"
    state = json.loads((active / "michiflin-state.json").read_text())
    assert state["notes"][OWNER][0]["text"] == "private note"
    assert state["jobs"]["4"]["todo"] == [1] and state["jobs"]["4"]["epoch"] == "old"
    assert state["apps"] == state["app_seen"] == state["inbox"] == {}
    archived = json.loads((active / cutover.LEGACY_BINDINGS).read_text())
    assert archived["apps"]["8"]["chat"] == 11 and archived["inbox"] == {"11": 0}
    marker = json.loads((active / cutover.MARKER).read_text())
    assert marker["owner_fingerprint"] == FP_OWNER and marker["bot_fingerprint"] == FP_NEW
    assert marker["legacy_asset_id"] == 7 and marker["epoch"] != "old"


@pytest.mark.parametrize("failure", ["pin", "save", "restore", "seal"])
def test_failure_keeps_original_and_never_seals_an_unverified_change(work, failure):
    active, _, copies = work
    storage = FakeStorage(copies, failure)
    with pytest.raises(RuntimeError):
        run(work, storage)
    assert not storage.sealed() and (active / "accounts" / "profile").read_text() == "old"
    assert not (active / cutover.MARKER).exists()
    original = json.loads((active / "michiflin-state.json").read_text())
    assert original["apps"]["8"]["chat"] == 11 and original["jobs"]["4"]["todo"] == [1]
    if failure != "pin":
        assert storage.pinned == [b"full-old-archive"]


def test_sealed_repository_never_starts_an_old_snapshot(work):
    active, _, copies = work
    storage = FakeStorage(copies)
    storage.seal_state = True
    with pytest.raises(cutover.CutoverError, match="fall back"):
        cutover.activate(active, storage, OWNER)
    with pytest.raises(cutover.CutoverError, match="preflight"):
        run(work, storage)


def test_marker_without_private_seal_fails_closed(work):
    active, _, copies = work
    (active / cutover.MARKER).write_text('{}')
    with pytest.raises(cutover.CutoverError, match="without the private seal"):
        cutover.activate(active, FakeStorage(copies), OWNER)


def test_owner_and_identity_must_match_before_any_backup(work):
    active, candidate, copies = work
    storage = FakeStorage(copies)
    with mock.patch.object(cutover.relay, "identity", side_effect=fake_identity), \
         mock.patch.object(cutover, "validate_candidate", side_effect=cutover.CutoverError("wrong key")):
        with pytest.raises(cutover.CutoverError):
            cutover.transition(active, candidate, META, storage, OWNER, REVISION)
    assert not storage.pinned and not storage.backups and not storage.sealed()
    assert (active / "accounts" / "profile").read_text() == "old"


def test_bundle_requires_owner_confirmation_and_authentication(tmp_path):
    import state_backup
    directory = tmp_path / "account"
    directory.mkdir()
    (directory / "example").write_text("synthetic only")
    password = "synthetic-password"
    raw = state_backup.archive(directory)
    account = cutover.seal_bytes(raw, password)
    verification = cutover.seal_bytes(json.dumps(META).encode(), password)
    with pytest.raises(cutover.CutoverError, match="confirmation"):
        cutover.make_bundle(account, verification, password, confirmed=False)
    bundle = cutover.make_bundle(account, verification, password, confirmed=True)
    result = cutover.unpack_bundle(bundle, password, tmp_path / "restored")
    assert result == META and (tmp_path / "restored" / "example").read_text() == "synthetic only"
    damaged = bytearray(bundle); damaged[-8] = ord("A") if damaged[-8] != ord("A") else ord("B")
    with pytest.raises(ValueError):
        cutover.unpack_bundle(bytes(damaged), password, tmp_path / "bad")


def test_bootstrap_delivers_only_to_verified_owner_and_never_duplicates(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from michiflin import admin, app, chat, config, identity_guard, miniapp
    marker = {"confirmed": True, "epoch": "new-epoch", "owner_address": OWNER,
              "owner_fingerprint": FP_OWNER, "owner_chat": 24}
    (tmp_path / cutover.MARKER).write_text(json.dumps(marker))
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "BOT_EPOCH_STRICT", True)
    monkeypatch.setattr(config, "BOT_VERIFIED_CONTACTS_ONLY", True)
    monkeypatch.setattr(config, "OWNER_ONLY", True)
    monkeypatch.setattr(config, "BOT_EPOCH", "new-epoch")
    monkeypatch.setattr(config, "OWNER_ADDRESS", OWNER)
    monkeypatch.setattr(config, "OWNER_PGP_FINGERPRINT", FP_OWNER)
    rpc = SimpleNamespace(get_chat_contacts=lambda _a, _c: [11])
    bot = SimpleNamespace(rpc=rpc)
    monkeypatch.setattr(chat, "_bot", bot)
    monkeypatch.setattr(chat, "_accid", 1)
    monkeypatch.setattr(admin, "is_allowed", lambda _a: True)
    monkeypatch.setattr(identity_guard, "authorized_chat", lambda *_a, **_kw: True)
    monkeypatch.setattr(app, "_locale", lambda *_a: "es")
    welcome = mock.Mock()
    monkeypatch.setattr(miniapp, "welcome", welcome)
    monkeypatch.setattr(miniapp, "latest_instance", lambda _chat: 0)
    previous_sender, previous_person = chat.sender(), app.state.acting_as()
    try:
        app._bootstrap_verified_owner(bot, 1)
        welcome.assert_called_once_with(24, "es")
        monkeypatch.setattr(miniapp, "latest_instance", lambda _chat: 31)
        app._bootstrap_verified_owner(bot, 1)
        assert welcome.call_count == 1
        monkeypatch.setattr(identity_guard, "authorized_chat", lambda *_a, **_kw: False)
        monkeypatch.setattr(miniapp, "latest_instance", lambda _chat: 0)
        app._bootstrap_verified_owner(bot, 1)
        assert welcome.call_count == 1
    finally:
        chat._thread.sender = previous_sender
        app.state.act_as(previous_person)
