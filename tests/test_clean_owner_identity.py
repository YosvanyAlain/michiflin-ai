"""Clean-identity bootstrapping; all accounts, names and payloads are synthetic."""
from __future__ import annotations

from contextlib import contextmanager
import copy
import json
from types import SimpleNamespace
from unittest import mock

import pytest

import state_backup
from tools import clean_owner_identity as clean, cutover_identity_once as verified

EPOCH = "a" * 48
OWNER = "owner@example.test"
META = {"verified": True, "owner_address": OWNER, "owner_fingerprint": "A" * 40,
        "bot_fingerprint": "B" * 40, "chat_id": 77}


def _asset(number: int, prefix: str) -> dict:
    return {"id": number, "name": prefix + "20261103T010203Z-0123456789abcdef.bin",
            "state": "uploaded", "created_at": "2026-11-03T01:02:03Z"}


def test_clean_namespace_never_matches_old_assets(monkeypatch):
    monkeypatch.setenv("STATE_BACKUP_NAMESPACE", state_backup.CLEAN_OWNER_NAMESPACE)
    assert state_backup._managed(_asset(1, state_backup.CLEAN_OWNER_ASSET_PREFIX))
    assert not state_backup._managed(_asset(2, state_backup.GITHUB_NATIVE_ASSET_PREFIX))
    assert not state_backup._managed(_asset(3, state_backup.ASSET_PREFIX))
    assert not state_backup._managed(_asset(4, state_backup.CLEAN_OWNER_PENDING_PREFIX))


def test_clean_origin_requires_correct_epoch_verified_owner_and_new_fingerprint(monkeypatch):
    monkeypatch.setenv("MICHIFLIN_CLEAN_EPOCH", EPOCH)
    marker = clean.make_marker(META, OWNER, EPOCH)
    assert state_backup._clean_origin_valid(marker)
    for change in ({"epoch": "b" * 48}, {"fresh_account": False}, {"bot_fingerprint": "A" * 40},
                   {"owner_chat": 1}, {"source_asset_id": 42}, {"legacy_asset_id": 123}):
        bad = dict(marker, **change)
        assert not state_backup._clean_origin_valid(bad)
    with pytest.raises(clean.CleanIdentityError):
        clean.make_marker(dict(META, owner_address="someone-else@example.test"), OWNER, EPOCH)
    monkeypatch.setenv("MICHIFLIN_CLEAN_EPOCH", "")
    assert not state_backup._clean_origin_valid(marker)


def test_existing_or_legacy_state_is_never_accepted_for_clean_restore(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_BACKUP_NAMESPACE", state_backup.CLEAN_OWNER_NAMESPACE)
    monkeypatch.setenv("MICHIFLIN_CLEAN_EPOCH", EPOCH)
    monkeypatch.setenv("STATE_BACKUP_REQUIRED", "1")
    monkeypatch.setattr(state_backup, "configuration", lambda: ("token", "private/repo", "pass"))
    target = tmp_path / "state"
    target.mkdir()
    monkeypatch.setattr(state_backup, "config_dir", lambda: target)
    with pytest.raises(RuntimeError, match="provenance"):
        state_backup.restore()
    target.rmdir()
    monkeypatch.setenv("STATE_BACKUP_STORAGE", "legacy-git")
    fallback = mock.Mock(side_effect=AssertionError("old backup accessed"))
    monkeypatch.setattr(state_backup, "_restore_legacy", fallback)
    with pytest.raises(RuntimeError, match="legacy"):
        state_backup.restore()
    fallback.assert_not_called()
    with pytest.raises(RuntimeError, match="legacy"):
        state_backup.save()


def test_clean_restore_without_seal_or_asset_never_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_BACKUP_NAMESPACE", state_backup.CLEAN_OWNER_NAMESPACE)
    monkeypatch.setenv("MICHIFLIN_CLEAN_EPOCH", EPOCH)
    monkeypatch.setenv("STATE_BACKUP_REQUIRED", "1")
    monkeypatch.setenv("STATE_BACKUP_STORAGE", "releases")
    monkeypatch.setattr(state_backup, "configuration", lambda: ("token", "private/repo", "pass"))
    monkeypatch.setattr(state_backup, "config_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(state_backup, "_get_ref", lambda *_: None)
    monkeypatch.setattr(state_backup, "_release", lambda *_: {"id": 1})
    fallback = mock.Mock(side_effect=AssertionError("old snapshot accessed"))
    monkeypatch.setattr(state_backup, "_restore_legacy", fallback)
    with pytest.raises(RuntimeError, match="not sealed"):
        state_backup.restore()
    fallback.assert_not_called()


def test_seed_creates_fresh_empty_runtime_verifies_download_and_leaves_old_assets(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_BACKUP_NAMESPACE", state_backup.CLEAN_OWNER_NAMESPACE)
    monkeypatch.setenv("MICHIFLIN_CLEAN_EPOCH", EPOCH)
    monkeypatch.setenv("STATE_BACKUP_REQUIRED", "1")
    monkeypatch.setenv("STATE_BACKUP_STORAGE", "releases")
    monkeypatch.setattr(state_backup, "configuration", lambda: ("token", "private/repo", "fresh-passphrase"))
    monkeypatch.setattr(state_backup, "_assert_private", lambda *_: None)
    monkeypatch.setattr(state_backup, "_release", lambda *_: {"id": 1})
    refs = {state_backup.RELEASE_TAG: "anchor", state_backup.READY_TAG: "anchor"}
    monkeypatch.setattr(state_backup, "_get_ref", lambda _t, _r, tag: refs.get(tag))
    monkeypatch.setattr(state_backup, "_make_ref", lambda _t, _r, tag, sha: refs.update({tag: sha}))
    old = _asset(10, state_backup.GITHUB_NATIVE_ASSET_PREFIX)
    listing = [copy.deepcopy(old)]
    blobs: dict[int, bytes] = {10: b"legacy-must-never-be-read"}
    reads: list[int] = []
    monkeypatch.setattr(state_backup, "_assets", lambda *_: list(listing))
    def download(_t, _r, number):
        reads.append(number)
        assert number != 10
        return blobs[number]
    monkeypatch.setattr(state_backup, "_download_asset", download)
    def upload(_t, _r, password, *, rotate):
        assert not rotate
        raw = state_backup.archive(state_backup.config_dir())
        state_backup._verify_clean_origin(raw)
        listing.append(_asset(20, state_backup.CLEAN_OWNER_ASSET_PREFIX))
        blobs[20] = verified.seal_bytes(raw, password)
    monkeypatch.setattr(state_backup, "_save_release", upload)
    validated = []
    monkeypatch.setattr(verified, "validate_candidate", lambda path, data, owner, **kwargs: validated.append((path.name, kwargs)))
    monkeypatch.setattr(clean, "_no_other_contacts", lambda *_: None)
    rpc = SimpleNamespace(get_all_account_ids=lambda: [1], get_next_msgs=lambda _a: [7, 11], set_config=mock.Mock())
    @contextmanager
    def account_rpc(_path):
        yield rpc
    monkeypatch.setattr(clean.relay, "account_rpc", account_rpc)
    candidate = tmp_path / "candidate"
    (candidate / "accounts").mkdir(parents=True)
    (candidate / "accounts" / "independent.txt").write_text("new synthetic account")
    assert clean.seed(candidate, META, OWNER, EPOCH) == "verified_and_sealed"
    assert json.loads((candidate / "michiflin-state.json").read_text()) == {"version": 5, "flags": {}}
    assert refs[state_backup.CLEAN_OWNER_READY_TAG] == "anchor"
    assert reads == [20, 20] and listing[0] == old and blobs[10] == b"legacy-must-never-be-read"
    rpc.set_config.assert_called_once_with(1, "last_msg_id", "11")
    assert len(validated) == 3  # candidate, downloaded seed, independent restore
    with pytest.raises(clean.CleanIdentityError, match="already_sealed"):
        clean.seed(candidate, META, OWNER, EPOCH)
    assert len(listing) == 2  # no overwrite after activation


def test_candidate_rejects_extra_contacts_instead_of_purging_them(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate"
    (candidate / "accounts").mkdir(parents=True)
    contacts = {10: SimpleNamespace(address=OWNER), 11: SimpleNamespace(address="other@example.test")}
    rpc = SimpleNamespace(get_all_account_ids=lambda: [1], get_contact_ids=lambda *_: [10, 11],
                          get_contact=lambda _a, i: contacts[i], get_blocked_contacts=lambda _a: [])
    @contextmanager
    def account_rpc(_path):
        yield rpc
    monkeypatch.setattr(clean.relay, "account_rpc", account_rpc)
    with pytest.raises(clean.CleanIdentityError, match="unexpected_contact"):
        clean._no_other_contacts(candidate, OWNER)
    assert len(contacts) == 2  # never remove anyone or pretend the state is clean
