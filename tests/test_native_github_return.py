"""Strict snapshot selection and a quiet, non-replaying GitHub return."""
from __future__ import annotations
import os
from unittest import mock
import pytest
import state_backup


def asset(number: int, prefix: str) -> dict:
    return {"id": number, "name": prefix + "20261103T010203Z-0123456789abcdef.bin",
            "state": "uploaded", "created_at": "2026-11-03T01:02:03Z"}


def test_github_namespace_rejects_imported_states_and_pending_uploads():
    legacy = asset(10, "michiflin-state-")
    native = asset(20, "michiflin-github-state-")
    with mock.patch.dict(os.environ, {"STATE_BACKUP_NAMESPACE": "github-native-v1"}):
        assert not state_backup._managed(legacy)
        assert state_backup._managed(native)
        assert not state_backup._managed(asset(30, "michiflin-github-pending-"))
        assert state_backup._managed(asset(30, "michiflin-github-pending-"), state_backup.GITHUB_NATIVE_PENDING_PREFIX)
    with mock.patch.dict(os.environ, {"STATE_BACKUP_NAMESPACE": ""}):
        assert state_backup._managed(legacy)
        assert not state_backup._managed(native)


def test_missing_native_snapshot_never_falls_back_to_git(tmp_path):
    env = {"STATE_BACKUP_NAMESPACE": "github-native-v1", "STATE_BACKUP_REQUIRED": "1",
           "STATE_BACKUP_STORAGE": "releases"}
    with mock.patch.dict(os.environ, env), \
         mock.patch.object(state_backup, "configuration", return_value=("t", "owner/repo", "p")), \
         mock.patch.object(state_backup, "config_dir", return_value=tmp_path / "state"), \
         mock.patch.object(state_backup, "_get_ref", return_value=None), \
         mock.patch.object(state_backup, "_release", return_value=None), \
         mock.patch.object(state_backup, "_restore_legacy") as legacy:
        with pytest.raises(RuntimeError):
            state_backup.restore()
        legacy.assert_not_called()


def test_corrupt_or_missing_native_snapshot_never_chooses_imported(tmp_path):
    env = {"STATE_BACKUP_NAMESPACE": "github-native-v1", "STATE_BACKUP_REQUIRED": "1",
           "STATE_BACKUP_STORAGE": "releases"}
    old = asset(10, "michiflin-state-")
    new = asset(20, "michiflin-github-state-")
    with mock.patch.dict(os.environ, env), \
         mock.patch.object(state_backup, "configuration", return_value=("t", "owner/repo", "p")), \
         mock.patch.object(state_backup, "config_dir", return_value=tmp_path / "state"), \
         mock.patch.object(state_backup, "_get_ref", return_value="same-anchor"), \
         mock.patch.object(state_backup, "_release", return_value={"id": 1}), \
         mock.patch.object(state_backup, "_assets", return_value=[old, new]), \
         mock.patch.object(state_backup, "_download_asset", return_value=b"bad") as download, \
         mock.patch.object(state_backup, "decrypt_backup", side_effect=ValueError("bad")), \
         mock.patch.object(state_backup, "_restore_legacy") as legacy:
        with pytest.raises(RuntimeError):
            state_backup.restore()
        assert [call.args[2] for call in download.call_args_list] == [20]
        legacy.assert_not_called()


def test_unknown_namespace_fails_closed():
    with mock.patch.dict(os.environ, {"STATE_BACKUP_NAMESPACE": "typo"}):
        with pytest.raises(ValueError):
            state_backup._managed(asset(1, "michiflin-state-"))


def base36(number: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    result = ""
    while number:
        number, remainder = divmod(number, 36)
        result = alphabet[remainder] + result
    return result or "0"


def test_transition_holds_old_or_unknown_chat_but_accepts_new(monkeypatch):
    from types import SimpleNamespace
    from michiflin import handover, state
    boundary = 1_793_676_800
    monkeypatch.setitem(state.data, "flags", {handover.FLAG: {"source_asset_id": handover.SOURCE_ASSET_ID, "cutoff": boundary}})
    assert handover.hold_message(SimpleNamespace(timestamp=boundary - 1))
    assert handover.hold_message(SimpleNamespace(timestamp=0))
    assert handover.hold_message(SimpleNamespace())
    assert not handover.hold_message(SimpleNamespace(timestamp=boundary + 1))


def test_transition_fails_closed_until_cutoff_is_loaded(monkeypatch):
    from types import SimpleNamespace
    from michiflin import handover, state
    monkeypatch.setenv("MICHIFLIN_NATIVE_FENCE_REQUIRED", "1")
    monkeypatch.setitem(state.data, "flags", {})
    with pytest.raises(handover.FenceNotReady):
        handover.hold_message(SimpleNamespace(timestamp=1_800_000_000))
    monkeypatch.setitem(state.data, "flags", {handover.FLAG: {"source_asset_id": handover.SOURCE_ASSET_ID, "cutoff": None}})
    with pytest.raises(handover.FenceNotReady):
        handover.hold_update({"t": "req", "id": "whatever"}, "new-session")


def test_old_webxdc_requests_and_chunks_are_held_new_requests_pass(monkeypatch):
    from michiflin import handover, state
    boundary = 1_793_676_800
    monkeypatch.setitem(state.data, "flags", {handover.FLAG: {"source_asset_id": handover.SOURCE_ASSET_ID, "cutoff": boundary}})
    old = base36((boundary - 1) * 1000) + "abcde"
    new = base36((boundary + 1) * 1000) + "abcde"
    for kind in ("req", "req_chunk"):
        assert handover.hold_update({"t": kind, "id": old}, "new-session")
        assert not handover.hold_update({"t": kind, "id": new}, "new-session")
        assert handover.hold_update({"t": kind, "id": "unknown-format"}, "new-session")
    assert not handover.hold_update({"t": "req", "id": "unknown-format", "session": "new-session"}, "new-session")
    assert not handover.hold_update({"t": "res", "id": old}, "new-session")


def test_held_chat_is_recorded_without_screening_or_read_receipts(monkeypatch):
    from types import SimpleNamespace
    from michiflin import app, handover, state
    boundary = 1_793_676_800
    monkeypatch.setitem(state.data, "flags", {handover.FLAG: {"source_asset_id": handover.SOURCE_ASSET_ID, "cutoff": boundary}})
    monkeypatch.setitem(state.data, handover.HOLD, {})
    monkeypatch.setattr(state, "save", lambda: None)
    app._taken.clear()
    app._draining.clear()
    message = SimpleNamespace(id=7124, timestamp=boundary - 10)
    with mock.patch.object(app, "_screen") as screen, mock.patch.object(app, "_mark_seen") as mark:
        app._accept(SimpleNamespace(rpc=object()), 1, message)
        screen.assert_not_called()
        mark.assert_not_called()
    assert "7124" in state.data[handover.HOLD]["messages"]
    app._taken.clear()


def test_webxdc_old_update_is_recorded_but_new_request_is_routed(monkeypatch):
    import json
    from types import SimpleNamespace
    from michiflin import access, chat, handover, miniapp, state
    boundary = 1_793_676_800
    monkeypatch.setitem(state.data, "flags", {handover.FLAG: {"source_asset_id": handover.SOURCE_ASSET_ID, "cutoff": boundary}})
    monkeypatch.setitem(state.data, handover.HOLD, {})
    monkeypatch.setattr(state, "save", lambda: None)
    old = base36((boundary - 1) * 1000) + "abcde"
    new = base36((boundary + 1) * 1000) + "abcde"
    entry = {"serial": 0, "addr": "person@example.invalid", "chat": 17, "build": "current"}
    updates = [{"serial": 1, "payload": {"t": "req", "id": old, "op": "hello"}},
               {"serial": 2, "payload": {"t": "req", "id": new, "op": "hello"}}]
    rpc = SimpleNamespace(get_webxdc_status_updates=lambda *_args: json.dumps(updates))
    miniapp._paused.clear()
    with mock.patch.object(miniapp, "_instance", return_value=entry), \
         mock.patch.object(access, "bound_allowed", return_value=True), \
         mock.patch.object(miniapp, "_verified_binding", return_value=True), \
         mock.patch.object(chat, "rpc", return_value=rpc), mock.patch.object(chat, "account", return_value=1), \
         mock.patch.object(miniapp, "_route") as route:
        miniapp.on_status_update(22)
    assert entry["serial"] == 2
    assert list(state.data[handover.HOLD]["updates"]) == ["22:1"]
    assert route.call_count == 1 and route.call_args.args[2]["id"] == new


def test_native_ready_marker_required_before_any_restore(tmp_path):
    env = {"STATE_BACKUP_NAMESPACE": "github-native-v1", "STATE_BACKUP_REQUIRED": "1",
           "STATE_BACKUP_STORAGE": "releases", "MICHIFLIN_NATIVE_READY_REQUIRED": "1"}
    def ref(_token, _repo, name):
        return None if name == "michiflin-github-native-ready-v1" else "anchor"
    with mock.patch.dict(os.environ, env), \
         mock.patch.object(state_backup, "configuration", return_value=("t", "owner/repo", "p")), \
         mock.patch.object(state_backup, "config_dir", return_value=tmp_path / "state"), \
         mock.patch.object(state_backup, "_get_ref", side_effect=ref), \
         mock.patch.object(state_backup, "_release", return_value={"id": 1}), \
         mock.patch.object(state_backup, "_download_asset") as download:
        with pytest.raises(RuntimeError):
            state_backup.restore()
        download.assert_not_called()


def test_guard_preserves_bytes_and_recovers_if_interrupted(monkeypatch):
    from tools import github_native_guard as guard
    from unittest.mock import Mock
    monkeypatch.setenv("STATE_BACKUP_NAMESPACE", "github-native-v1")
    monkeypatch.delenv("MICHIFLIN_NATIVE_GUARD_DRY", raising=False)
    originals = [asset(10, "michiflin-state-"), asset(11, "michiflin-pending-")]
    payloads = {10: b"encrypted-state-one", 11: b"encrypted-state-two"}
    for item in originals:
        item["size"] = len(payloads[item["id"]])
    release = {"id": 12, "tag_name": state_backup.RELEASE_TAG, "body": state_backup.RELEASE_MARKER + ". Original description."}
    original_body = release["body"]
    refs = {state_backup.RELEASE_TAG: "anchor", state_backup.READY_TAG: "anchor", "michiflin-fresh-identity-v1": "anchor"}
    patch_calls = []
    interrupted = [True]
    def patch(url, *, headers, json, timeout):
        patch_calls.append(url)
        if url.endswith("/releases/12"):
            release["body"] = json["body"]
            return Mock(json=lambda: dict(release), raise_for_status=lambda: None)
        aid = int(url.rsplit("/", 1)[1])
        if aid == 10 and interrupted[0]:
            raise RuntimeError("synthetic interruption")
        item = next(x for x in originals if x["id"] == aid)
        item["name"] = json["name"]
        return Mock(json=lambda: dict(item), raise_for_status=lambda: None)
    def get_ref(_token, _repo, name):
        return refs.get(name)
    def make_ref(_token, _repo, name, sha):
        refs[name] = sha
    def owned_release(_token, _repo):
        if not release["body"].startswith(state_backup.RELEASE_MARKER):
            raise ValueError("release locked")
        return dict(release)
    monkeypatch.setattr(state_backup, "configuration", lambda: ("t", "owner/repo", "p"))
    monkeypatch.setattr(state_backup, "_assert_private", lambda *_args: None)
    monkeypatch.setattr(state_backup, "_get_ref", get_ref)
    monkeypatch.setattr(state_backup, "_make_ref", make_ref)
    monkeypatch.setattr(state_backup, "_assets", lambda *_args: [dict(x) for x in originals])
    monkeypatch.setattr(state_backup, "_download_asset", lambda _t, _r, aid: payloads[aid])
    monkeypatch.setattr(state_backup, "_release", owned_release)
    monkeypatch.setattr(guard, "raw_release", lambda *_args: dict(release))
    monkeypatch.setattr(guard, "verify_seed", lambda *_args: None)
    monkeypatch.setattr(guard, "previous_cutoff", lambda: 1_793_676_800)
    monkeypatch.setattr(guard.requests, "patch", patch)
    with pytest.raises(RuntimeError):
        guard.guard()
    assert release["body"].startswith(guard.LOCK)
    assert refs.get(guard.NATIVE_READY) is None
    # A retry sees the lock and finishes. It never changes or deletes bytes.
    interrupted[0] = False
    guard.guard()
    assert release["body"] == original_body
    assert refs[guard.NATIVE_READY] == "anchor"
    assert all(item["name"].startswith(guard.RETAINED) for item in originals)
    assert payloads == {10: b"encrypted-state-one", 11: b"encrypted-state-two"}
    assert len(patch_calls) >= 4


def test_native_marker_is_required_even_in_a_decryptable_archive(tmp_path):
    import io
    import json
    import tarfile
    root = tmp_path / "michiflin"
    root.mkdir()
    (root / "michiflin-state.json").write_text("{}")
    def build() -> bytes:
        result = io.BytesIO()
        with tarfile.open(fileobj=result, mode="w:gz") as tar:
            tar.add(root, arcname="michiflin")
        return result.getvalue()
    with pytest.raises(ValueError):
        state_backup._verify_native_origin(build())
    marker = root / ".github-native-origin-v1.json"
    marker.write_text(json.dumps({"version": 1, "source_asset_id": 703991127, "source_sha256": "a" * 64}))
    with pytest.raises(ValueError):
        state_backup._verify_native_origin(build())
    marker.write_text(json.dumps({"version": 1, "source_asset_id": 699441108, "source_sha256": "a" * 64}))
    state_backup._verify_native_origin(build())


def test_cutover_uses_verified_drain_start_not_end_of_backup(monkeypatch):
    import io
    import zipfile
    from datetime import datetime, timezone
    from unittest.mock import Mock
    from tools import github_native_guard as guard
    lines = "\n".join([
        "2026-11-03T03:34:00.1000000Z stopping (SIGTERM): finishing work in progress",
        "2026-11-03T03:34:12.1000000Z stopped cleanly: 0 chats busy, 0 app operations, 0 messages unsent",
        "2026-11-03T03:34:17.1000000Z [backup] encrypted and verified snapshot saved (1234 KB)",
    ])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("1_private.txt", lines)
    response = Mock(content=buffer.getvalue(), raise_for_status=lambda: None)
    monkeypatch.setenv("GITHUB_REPOSITORY", "YosvanyAlain/michiflin-ai")
    monkeypatch.setenv("MICHIFLIN_ACTIONS_TOKEN", "test")
    monkeypatch.setattr(guard, "public_api", lambda _path: {"status": "completed", "head_sha": "e970d056ede3f710bd303096ba2d74a682e06b92"})
    monkeypatch.setattr(guard.requests, "get", lambda *_args, **_kwargs: response)
    assert guard.previous_cutoff() == int(datetime(2026, 11, 3, 3, 34, 1, tzinfo=timezone.utc).timestamp())
