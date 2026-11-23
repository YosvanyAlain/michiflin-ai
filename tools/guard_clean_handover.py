#!/usr/bin/env python3
"""Fence old GitHub writers only while holding the shared workflow concurrency group.

This changes only a private release description, never its assets. A new runtime
explicitly accepts the prefix; the reviewed historical runtimes reject it before
restoring, running mail I/O, or pruning snapshots. The original description is
retained byte-for-byte after the prefix, so the operation is reversible.
"""
from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests  # noqa: E402
import state_backup  # noqa: E402

PUBLIC_REPO = "YosvanyAlain/michiflin-ai"
STATE_REPO = "YosvanyAlain/michiflin-private-state"
CONCURRENCY_LINE = "group: ${{ (inputs.task == 'fence' || inputs.task == 'fence-preview') && 'michiflin-session' || format('tests-{0}', github.ref) }}"
HISTORIC_RUNS = {
    44917724855: ("12cbdf8ff0319ea00e7073d83c9dbdc45f9adb86", ".github/workflows/michiflin.yml"),
    44765180234: ("9dd71a7b323a74815b6d402872c61d99098b94af", ".github/workflows/next-runtime.yml"),
    41567353553: ("e970d056ede3f710bd303096ba2d74a682e06b92", ".github/workflows/michiflin.yml"),
}
FIELDS = ("id", "name", "state", "size", "created_at", "updated_at", "digest")


class FenceError(RuntimeError):
    """Only a fixed error category is safe to print in public Actions logs."""


def require(ok: Any, reason: str) -> None:
    if not ok:
        raise FenceError(reason)


def inventory_digest(items: list[dict]) -> str:
    require(all(isinstance(item.get("id"), int) for item in items), "invalid_inventory")
    selected = sorted([{key: item.get(key) for key in FIELDS} for item in items], key=lambda item: item["id"])
    raw = json.dumps(selected, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


@contextmanager
def environment(**changes: str) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in changes}
    os.environ.update(changes)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def api_get(token: str, repo: str, path: str, params: dict | None = None) -> dict:
    response = requests.get(f"{state_backup._repo_base(repo)}{path}", headers=state_backup.headers(token),
                            params=params, timeout=30, allow_redirects=False)
    require(response.status_code == 200, "github_read_failed")
    data = response.json()
    require(isinstance(data, dict), "github_read_invalid")
    return data


def validate_context(actions_token: str, state_token: str, expected: str) -> None:
    require(os.getenv("GITHUB_ACTIONS") == "true" and os.getenv("MICHIFLIN_FENCE_ARMED") == "1",
            "fence_not_armed")
    require(os.getenv("GITHUB_REPOSITORY") == PUBLIC_REPO and re.fullmatch(r"[0-9]+", os.getenv("GITHUB_RUN_ID", "")),
            "wrong_workflow_context")
    sha = os.getenv("GITHUB_SHA", "")
    require(re.fullmatch(r"[a-f0-9]{40}", sha), "invalid_revision")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    preview = os.getenv("MICHIFLIN_FENCE_PREVIEW", "0")
    require(preview in {"0", "1"}, "invalid_preview_policy")
    task = "fence-preview" if preview == "1" else "fence"
    require(event.get("inputs", {}).get("task") == task
            and event.get("inputs", {}).get("fence_inventory_sha256") == expected,
            "unexpected_dispatch_inputs")
    workflow = (Path(os.environ["GITHUB_WORKSPACE"]) / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    require(CONCURRENCY_LINE in workflow and "cancel-in-progress: false" in workflow,
            "shared_concurrency_missing")
    current = api_get(actions_token, PUBLIC_REPO, "/actions/runs/" + os.environ["GITHUB_RUN_ID"])
    require(current.get("status") == "in_progress" and current.get("event") == "workflow_dispatch"
            and current.get("path") == ".github/workflows/tests.yml" and current.get("head_sha") == sha,
            "workflow_not_holding_session_group")
    for number, (revision, path) in HISTORIC_RUNS.items():
        run = api_get(actions_token, PUBLIC_REPO, f"/actions/runs/{number}")
        require(run.get("head_sha") == revision and run.get("path") == path
                and run.get("event") == "workflow_dispatch", "historic_run_changed")
        require(run.get("status") == "queued"
                or (run.get("status") == "completed" and run.get("conclusion") in {"cancelled", "skipped"}),
                "historic_run_active_or_executed")
        jobs = api_get(actions_token, PUBLIC_REPO, f"/actions/runs/{number}/jobs", {"per_page": 1})
        require(jobs.get("total_count") == 0, "historic_run_changed")
    # Reject any unexpected public runner while holding the session group. The
    # three known zero-job ghosts may remain queued until GitHub resolves them.
    for status in ("in_progress", "queued", "pending", "waiting", "requested"):
        listing = api_get(actions_token, PUBLIC_REPO, "/actions/runs", {"status": status, "per_page": 100})
        rows = listing.get("workflow_runs")
        require(isinstance(rows, list) and listing.get("total_count") == len(rows), "public_run_inventory_incomplete")
        for run in rows:
            if status == "in_progress":
                require(str(run.get("id")) == os.environ["GITHUB_RUN_ID"], "unexpected_public_run")
            else:
                require(status == "queued" and run.get("id") in HISTORIC_RUNS, "unexpected_public_run")
    # Other repositories have no shared concurrency group. Refuse the handover
    # unless the backup token can confirm their writer queues are empty.
    for repo in ("YosvanyAlain/michiflin-ai-next", STATE_REPO):
        for status in ("in_progress", "queued", "pending", "waiting", "requested"):
            runs = api_get(state_token, repo, "/actions/runs", {"status": status, "per_page": 1})
            require(runs.get("total_count") == 0, "other_repository_has_possible_writer")


def raw_release(token: str, repo: str) -> dict:
    release = api_get(token, repo, "/releases/tags/" + state_backup.RELEASE_TAG)
    require(isinstance(release.get("id"), int) and release["id"] > 0, "invalid_release")
    return release


def verify_latest_snapshot(fenced: bool) -> None:
    from tools import clean_owner_identity
    owner = os.getenv("OWNER_ADDRESS", "")
    require(owner and os.getenv("MICHIFLIN_CLEAN_EPOCH", ""), "missing_owner_context")
    with tempfile.TemporaryDirectory(prefix="michiflin-fence-check-", dir=os.getenv("RUNNER_TEMP") or None) as root:
        target = Path(root) / "state"
        # The freshly restored copy is isolated and never starts mail I/O.
        with environment(CONFIG_DIR=str(target), STATE_BACKUP_FENCE_REQUIRED="1" if fenced else "0"):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                state_backup.restore()
                clean_owner_identity.activate(target, owner)


def establish_fence(token: str, repo: str, expected: str, *, preview: bool = False) -> str:
    require(repo == STATE_REPO and re.fullmatch(r"[a-f0-9]{64}", expected), "wrong_inventory_or_repository")
    require(os.getenv("STATE_BACKUP_NAMESPACE") == state_backup.CLEAN_OWNER_NAMESPACE
            and os.getenv("STATE_BACKUP_REQUIRED") == "1"
            and os.getenv("STATE_BACKUP_STORAGE") == "releases"
            and os.getenv("STATE_BACKUP_RETAIN_ALL") == "1", "unsafe_backup_policy")
    state_backup._assert_private(token, repo)
    release = raw_release(token, repo)
    rid, body = release["id"], release.get("body")
    require(isinstance(body, str), "release_description_invalid")
    fenced = body.startswith(state_backup.RELEASE_WRITER_FENCE + state_backup.RELEASE_MARKER)
    require(fenced or body.startswith(state_backup.RELEASE_MARKER), "release_description_unexpected")
    assets = state_backup._assets(token, repo, rid)
    require(inventory_digest(assets) == expected, "inventory_changed")
    clean = [item for item in assets if state_backup._managed(item)]
    require(bool(clean), "clean_snapshot_missing")
    verify_latest_snapshot(fenced)
    require(inventory_digest(state_backup._assets(token, repo, rid)) == expected, "inventory_changed_during_verification")
    print("SAFE_FENCE_RECOVERY_VERIFIED_NO_MAIL_IO", flush=True)
    if preview:
        return "preview_already_fenced" if fenced else "preview_ready"
    original = body[len(state_backup.RELEASE_WRITER_FENCE):] if fenced else body
    wanted = state_backup.RELEASE_WRITER_FENCE + original
    if not fenced:
        require(raw_release(token, repo).get("body") == original, "release_description_changed")
        print("SAFE_FENCE_METADATA_PATCH_ONCE", flush=True)
        response = requests.patch(f"{state_backup._repo_base(repo)}/releases/{rid}",
                                  headers=state_backup.headers(token), json={"body": wanted},
                                  timeout=60, allow_redirects=False)
        require(response.status_code == 200, "fence_patch_unconfirmed")
        changed = response.json()
        require(changed.get("id") == rid and changed.get("body") == wanted, "fence_patch_unconfirmed")
    require(raw_release(token, repo).get("body") == wanted, "fence_readback_failed")
    require(inventory_digest(state_backup._assets(token, repo, rid)) == expected, "inventory_changed_after_fence")
    verify_latest_snapshot(True)
    require(inventory_digest(state_backup._assets(token, repo, rid)) == expected, "inventory_changed_after_restore")
    return "already_fenced" if fenced else "fenced"


def main() -> None:
    settings = state_backup.configuration()
    require(settings is not None, "backup_credentials_missing")
    token, repo, _password = settings
    expected = os.getenv("FENCE_INVENTORY_SHA256", "")
    actions_token = os.getenv("MICHIFLIN_ACTIONS_TOKEN", "")
    require(actions_token, "actions_token_missing")
    validate_context(actions_token, token, expected)
    preview = os.getenv("MICHIFLIN_FENCE_PREVIEW", "0") == "1"
    outcome = establish_fence(token, repo, expected, preview=preview)
    print("SAFE_FENCE_PREVIEW" if preview else "SAFE_FENCE_CONFIRMED", outcome,
          "ORIGINAL_ASSETS_UNCHANGED", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        detail = str(exc) if isinstance(exc, FenceError) else type(exc).__name__
        print("SAFE_FENCE_ABORTED", detail, file=sys.stderr, flush=True)
        raise SystemExit(1) from None
