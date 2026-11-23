"""Synthetic tests for the reversible writer fence and conservative handover."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest import mock

import pytest

import state_backup
from tools import guard_clean_handover as guard, hold_uncertain_handover as hold


@pytest.mark.parametrize(
    "policy,body,accepted",
    [
        ("0", state_backup.RELEASE_MARKER + ". Original.", True),
        ("0", state_backup.RELEASE_WRITER_FENCE + state_backup.RELEASE_MARKER, False),
        ("1", state_backup.RELEASE_MARKER, False),
        ("1", state_backup.RELEASE_WRITER_FENCE + state_backup.RELEASE_MARKER + ". Original.", True),
        ("1", "Unowned release", False),
        ("invalid", state_backup.RELEASE_MARKER, False),
    ],
)
def test_only_explicit_reviewed_writers_accept_the_fence(monkeypatch, policy, body, accepted):
    response = SimpleNamespace(status_code=200, raise_for_status=lambda: None,
                               json=lambda: {"id": 12, "body": body})
    monkeypatch.setattr(state_backup.requests, "get", lambda *_a, **_k: response)
    monkeypatch.setenv("STATE_BACKUP_FENCE_REQUIRED", policy)
    if accepted:
        assert state_backup._release("synthetic", "test/private")["id"] == 12
    else:
        with pytest.raises(ValueError):
            state_backup._release("synthetic", "test/private")


def fake_asset(number: int, when: str) -> dict:
    return {"id": number, "name": "michiflin-owner-clean-state-20261103T010203Z-0123456789abcdef.bin",
            "state": "uploaded", "size": 22, "created_at": when, "updated_at": when, "digest": None}


def test_inventory_digest_is_order_independent_but_changes_with_any_original_asset():
    items = [fake_asset(1, "2026-11-03T01:02:03Z"), fake_asset(2, "2026-11-04T01:02:03Z")]
    digest = guard.inventory_digest(items)
    assert digest == guard.inventory_digest(list(reversed(items)))
    for field in guard.FIELDS:
        altered = copy.deepcopy(items)
        altered[0][field] = 999 if field in {"id", "size"} else "changed"
        assert guard.inventory_digest(altered) != digest


@pytest.fixture
def fenced_store(monkeypatch):
    for key, value in {
        "STATE_BACKUP_NAMESPACE": state_backup.CLEAN_OWNER_NAMESPACE,
        "STATE_BACKUP_REQUIRED": "1", "STATE_BACKUP_STORAGE": "releases", "STATE_BACKUP_RETAIN_ALL": "1",
    }.items():
        monkeypatch.setenv(key, value)
    release = {"id": 17, "body": state_backup.RELEASE_MARKER + ". The complete original description."}
    assets = [fake_asset(2, "2026-11-04T01:02:03Z"), fake_asset(1, "2026-11-03T01:02:03Z")]
    original_assets = copy.deepcopy(assets)
    events = []
    monkeypatch.setattr(state_backup, "_assert_private", lambda *_: events.append("private"))
    monkeypatch.setattr(guard, "raw_release", lambda *_: copy.deepcopy(release))
    monkeypatch.setattr(state_backup, "_assets", lambda *_: copy.deepcopy(assets))
    monkeypatch.setattr(guard, "verify_latest_snapshot", lambda fenced: events.append(f"verify:{fenced}"))

    def patch(_url, *, json, **_kwargs):
        events.append("patch")
        assert set(json) == {"body"}
        release["body"] = json["body"]
        return SimpleNamespace(status_code=200, json=lambda: copy.deepcopy(release))

    monkeypatch.setattr(guard.requests, "patch", patch)
    monkeypatch.setattr(guard.requests, "delete", mock.Mock(side_effect=AssertionError("no deletion allowed")))
    return SimpleNamespace(release=release, assets=assets, original_assets=original_assets, events=events,
                           expected=guard.inventory_digest(assets))


def test_fence_restores_before_and_after_metadata_change_without_deleting(fenced_store):
    item = fenced_store
    original_body = item.release["body"]
    assert guard.establish_fence("synthetic", guard.STATE_REPO, item.expected) == "fenced"
    assert item.events == ["private", "verify:False", "patch", "verify:True"]
    assert item.release["body"] == state_backup.RELEASE_WRITER_FENCE + original_body
    assert item.release["body"][len(state_backup.RELEASE_WRITER_FENCE):] == original_body
    assert item.assets == item.original_assets
    guard.requests.delete.assert_not_called()


def test_preview_restores_but_never_changes_metadata_or_assets(fenced_store):
    item = fenced_store
    original_body = item.release["body"]
    assert guard.establish_fence("synthetic", guard.STATE_REPO, item.expected, preview=True) == "preview_ready"
    assert item.events == ["private", "verify:False"]
    assert item.release["body"] == original_body and item.assets == item.original_assets
    guard.requests.delete.assert_not_called()


def test_repeat_with_same_inventory_is_idempotent_and_does_not_patch(fenced_store):
    item = fenced_store
    item.release["body"] = state_backup.RELEASE_WRITER_FENCE + item.release["body"]
    assert guard.establish_fence("synthetic", guard.STATE_REPO, item.expected) == "already_fenced"
    assert item.events == ["private", "verify:True", "verify:True"]
    assert item.assets == item.original_assets


def test_no_fence_when_snapshot_verification_fails(fenced_store, monkeypatch):
    item = fenced_store
    previous_body = item.release["body"]
    monkeypatch.setattr(guard, "verify_latest_snapshot", mock.Mock(side_effect=ValueError("not restorable")))
    with pytest.raises(ValueError):
        guard.establish_fence("synthetic", guard.STATE_REPO, item.expected)
    assert item.release["body"] == previous_body and "patch" not in item.events
    assert item.assets == item.original_assets


def test_no_fence_if_inventory_or_release_changes(fenced_store):
    item = fenced_store
    previous_body = item.release["body"]
    item.assets[0]["size"] += 1
    with pytest.raises(guard.FenceError, match="inventory_changed"):
        guard.establish_fence("synthetic", guard.STATE_REPO, item.expected)
    assert item.release["body"] == previous_body and "patch" not in item.events


def test_no_fence_without_retain_all(fenced_store, monkeypatch):
    monkeypatch.setenv("STATE_BACKUP_RETAIN_ALL", "0")
    with pytest.raises(guard.FenceError, match="unsafe_backup_policy"):
        guard.establish_fence("synthetic", guard.STATE_REPO, fenced_store.expected)
    assert "patch" not in fenced_store.events


def test_context_requires_shared_group_and_empty_external_writer_queues(tmp_path, monkeypatch):
    expected = "a" * 64
    sha = "b" * 40
    workflow = tmp_path / ".github/workflows/tests.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("concurrency:\n  " + guard.CONCURRENCY_LINE + "\n  cancel-in-progress: false\n")
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"inputs": {"task": "fence", "fence_inventory_sha256": expected}}))
    for key, value in {"GITHUB_ACTIONS": "true", "MICHIFLIN_FENCE_ARMED": "1",
                       "GITHUB_REPOSITORY": guard.PUBLIC_REPO, "GITHUB_RUN_ID": "123",
                       "GITHUB_SHA": sha, "GITHUB_EVENT_PATH": str(event), "GITHUB_WORKSPACE": str(tmp_path)}.items():
        monkeypatch.setenv(key, value)
    external = {"total_count": 0}

    def api(_token, repo, path, params=None):
        if repo != guard.PUBLIC_REPO:
            return external
        if path == "/actions/runs/123":
            return {"status": "in_progress", "event": "workflow_dispatch",
                    "path": ".github/workflows/tests.yml", "head_sha": sha}
        if path == "/actions/runs":
            status = params["status"]
            rows = ([{"id": 123}] if status == "in_progress" else
                    [{"id": number} for number in guard.HISTORIC_RUNS] if status == "queued" else [])
            return {"total_count": len(rows), "workflow_runs": rows}
        if path.endswith("/jobs"):
            return {"total_count": 0}
        revision, workflow_path = guard.HISTORIC_RUNS[int(path.rsplit("/", 1)[1])]
        return {"status": "queued", "head_sha": revision, "path": workflow_path, "event": "workflow_dispatch"}

    monkeypatch.setattr(guard, "api_get", api)
    guard.validate_context("action-test", "state-test", expected)
    external["total_count"] = 1
    with pytest.raises(guard.FenceError, match="other_repository_has_possible_writer"):
        guard.validate_context("action-test", "state-test", expected)
    external["total_count"] = 0
    workflow.write_text("concurrency:\n  group: wrong-group\n  cancel-in-progress: false\n")
    with pytest.raises(guard.FenceError, match="shared_concurrency_missing"):
        guard.validate_context("action-test", "state-test", expected)


def test_only_preexisting_inflight_is_held_once_and_all_details_are_preserved(tmp_path):
    path = tmp_path / "michiflin-state.json"
    jobs = {
        "old": {"status": "sending", "hold": False, "inflight": {"msg_id": 77, "index": 2},
                "todo": [2, 3], "status_msg": 19, "url": "private-test-source"},
        "old-already-held": {"status": "sending", "hold": True, "error": "previous-reason",
                             "inflight": {"msg_id": 88}, "status_msg": 20},
        "new-ready": {"status": "queued", "inflight": None, "todo": [0]},
        "finished": {"status": "done", "inflight": {"msg_id": 99}},
    }
    original = copy.deepcopy(jobs)
    path.write_text(json.dumps({"version": 5, "jobs": jobs, "flags": {"other": 4}}))
    assert hold.hold_inflight(path) == (True, 1)
    value = json.loads(path.read_text())
    old = value["jobs"]["old"]
    assert old["hold"] and old["error"] == "delivery_unknown" and old["preserve_status_on_recovery"]
    for field in ("inflight", "todo", "status_msg", "url", "status"):
        assert old[field] == original["old"][field]
    assert value["jobs"]["old-already-held"]["error"] == "previous-reason"
    for key in original:
        assert value["jobs"][key]["preserve_status_on_recovery"]
        assert value["jobs"][key]["preserve_handover_metadata"]
    for key in ("new-ready", "finished"):
        for field, expected in original[key].items():
            assert value["jobs"][key][field] == expected
    marker = value["flags"][hold.FLAG]
    assert value["flags"]["other"] == 4 and marker["version"] == 1
    assert 1_000_000_000 < marker["cutoff"] < 4_000_000_000
    # A subsequent owner-requested resume must not be silently paused again.
    value["jobs"]["old"]["hold"] = False
    path.write_text(json.dumps(value))
    assert hold.hold_inflight(path) == (False, 0)
    assert json.loads(path.read_text())["jobs"]["old"]["hold"] is False


def test_recover_does_not_delete_a_held_handoff_status(monkeypatch):
    from michiflin import access, chat, queue, state
    jobs = {"review": {"id": "review", "status": "sending", "hold": True, "inflight": {"msg_id": 77},
                       "status_msg": 19, "preserve_status_on_recovery": True},
            "normal": {"id": "normal", "status": "done", "status_msg": 20}}
    monkeypatch.setattr(queue, "jobs", lambda: jobs)
    monkeypatch.setattr(access, "job_allowed", lambda _job: True)
    monkeypatch.setattr(queue, "_recover_packing_source", lambda _job: True)
    monkeypatch.setattr(state, "save", lambda: None)
    monkeypatch.setattr(chat, "ready", lambda: True)
    deleted = mock.Mock()
    monkeypatch.setattr(chat, "delete_for_all", deleted)
    queue.recover()
    deleted.assert_called_once_with([20])
    assert jobs["review"]["status_msg"] == 19 and jobs["review"]["inflight"]["msg_id"] == 77
    assert jobs["normal"]["status_msg"] == 0


def test_replay_fence_holds_old_requests_but_permits_new_ones(monkeypatch):
    from michiflin import handover, state
    boundary = 1_793_676_800
    monkeypatch.setenv("MICHIFLIN_CLEAN_REPLAY_FENCE_REQUIRED", "1")
    monkeypatch.setitem(state.data, "flags", {handover.CLEAN_FLAG: {"version": 1, "cutoff": boundary}})
    assert handover.CLEAN_FLAG == hold.FLAG
    assert handover.hold_message(SimpleNamespace(timestamp=boundary - 5))
    assert handover.hold_message(SimpleNamespace(timestamp=0))
    assert not handover.hold_message(SimpleNamespace(timestamp=boundary + 5))
    assert handover.hold_update({"t": "req", "id": "unknown-old-format"}, "new-boot")
    assert not handover.hold_update({"t": "req", "id": "unknown-format", "session": "new-boot"}, "new-boot")
    assert not handover.hold_update({"t": "res", "id": "unknown-format"}, "new-boot")
    monkeypatch.setitem(state.data, "flags", {})
    with pytest.raises(handover.FenceNotReady):
        handover.hold_message(SimpleNamespace(timestamp=boundary + 5))
    monkeypatch.setitem(state.data, "flags", {handover.CLEAN_FLAG: {"version": 1, "cutoff": True}})
    with pytest.raises(handover.FenceNotReady):
        handover.hold_update({"t": "req", "id": "x"}, "new-boot")


def test_old_jobs_do_not_expire_or_lose_local_media_during_routine_cleanup(monkeypatch):
    import time
    from michiflin import access, chat, config, queue, state
    now = time.time()
    old = {"id": "old", "status": "done", "updated": 1, "preserve_handover_metadata": True,
           "messages": [123], "inflight": None}
    new = {"id": "new", "status": "done", "updated": now, "messages": [456], "inflight": None}
    records = {"old": old, "new": new}
    monkeypatch.setattr(queue, "jobs", lambda: records)
    monkeypatch.setattr(access, "job_allowed", lambda _job: True)
    monkeypatch.setattr(state, "setting", lambda _key: 1)
    monkeypatch.setattr(config, "RETAIN_DOWNLOADED_MEDIA", False)
    monkeypatch.setattr(queue, "free_disk", lambda: 10**15)
    discard = mock.Mock()
    monkeypatch.setattr(queue, "_discard_completed_download", discard)
    monkeypatch.setattr(state, "save", lambda: None)
    queue.cleanup()
    assert records["old"] is old and records["new"] is new
    discard.assert_called_once_with(new)
    # Even an explicit completion callback cannot clean a marked historical job.
    monkeypatch.undo()
    with mock.patch.object(chat, "discard_delivered_media") as remote_cleanup:
        queue._discard_completed_download(old)
        remote_cleanup.assert_not_called()


def test_preexisting_local_directory_cannot_bypass_required_writer_fence(tmp_path, monkeypatch):
    target = tmp_path / "already-present"
    target.mkdir()
    monkeypatch.setenv("STATE_BACKUP_NAMESPACE", "")
    monkeypatch.setenv("STATE_BACKUP_REQUIRED", "1")
    monkeypatch.setenv("STATE_BACKUP_STORAGE", "releases")
    monkeypatch.setenv("STATE_BACKUP_FENCE_REQUIRED", "1")
    monkeypatch.setattr(state_backup, "configuration", lambda: ("synthetic", "test/private", "pw"))
    monkeypatch.setattr(state_backup, "config_dir", lambda: target)
    monkeypatch.setattr(state_backup, "_release", lambda *_args: None)
    with pytest.raises(RuntimeError, match="fence is unavailable"):
        state_backup.restore()
    monkeypatch.setattr(state_backup, "_release", lambda *_args: {"id": 1})
    state_backup.restore()


def test_unknown_state_version_is_not_mutated(tmp_path):
    path = tmp_path / "state.json"
    original = json.dumps({"version": 6, "jobs": {}, "flags": {}})
    path.write_text(original)
    with pytest.raises(ValueError, match="invalid existing state"):
        hold.hold_inflight(path)
    assert path.read_text() == original
