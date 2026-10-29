#!/usr/bin/env python3
"""Separately authorized one-time retirement of old private backup references.

Dry-run is the default. Execution is restricted to the named private state
repository, after independently verifying the new identity, its profile, the
new backup and a key-free owner archive covering every still-available source.
A force update may replace only that repository's main with its state-free seal
anchor; the public source repository and all tags are never rewritten.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests  # noqa: E402
import state_backup  # noqa: E402
from tools import cutover_identity_once as cutover, export_owner_history_once as history  # noqa: E402
from tools import migrate_relay_once as relay, profile_carryover_once as profile, restore_owner_history as recovery  # noqa: E402

ALLOWED_REPO = "YosvanyAlain/michiflin-private-state"


class RetirementError(RuntimeError):
    pass


def validate_git_topology(heads: list[dict], tags: list[dict], commits: list[dict],
                          trees: dict[str, list[str]], anchor: str, backup_path: str) -> str:
    """Only a sole legacy branch and tags anchored to the safe orphan can proceed."""
    if not re.fullmatch(r"[0-9a-f]{40}", anchor):
        raise RetirementError("invalid state-free anchor")
    if len(heads) != 1 or heads[0].get("ref") != "refs/heads/main":
        raise RetirementError("additional private branches need review")
    head = (heads[0].get("object") or {}).get("sha")
    if not re.fullmatch(r"[0-9a-f]{40}", str(head or "")):
        raise RetirementError("invalid private main")
    if not tags or any((x.get("object") or {}).get("type") != "commit" or
                       (x.get("object") or {}).get("sha") != anchor for x in tags):
        raise RetirementError("a private tag is not on the state-free anchor")
    required = {"refs/tags/" + cutover.SEAL, "refs/tags/" + state_backup.RELEASE_TAG}
    if not required <= {x.get("ref") for x in tags}:
        raise RetirementError("backup seal or release anchor is missing")
    if not commits or commits[0].get("sha") != head:
        raise RetirementError("private Git history changed")
    if head == anchor:
        if len(commits) != 1 or commits[0].get("parents") != [] or trees.get(anchor) != [".michiflin-backup-anchor"]:
            raise RetirementError("state-free private main could not be verified")
        return str(head)
    for index, item in enumerate(commits):
        sha = item.get("sha")
        parents = item.get("parents")
        if not isinstance(parents, list) or trees.get(sha) != [backup_path]:
            raise RetirementError("unrelated private Git content needs review")
        expected = commits[index + 1]["sha"] if index + 1 < len(commits) else None
        if ([p.get("sha") for p in parents] != ([expected] if expected else [])):
            raise RetirementError("private Git history is not a simple old backup chain")
    if trees.get(anchor) != [".michiflin-backup-anchor"]:
        raise RetirementError("anchor contains unexpected files")
    return str(head)


def _reference_summary(items: list[dict]) -> set[tuple[str, str, str]]:
    return {(str(x.get("ref") or ""), str((x.get("object") or {}).get("type") or ""),
             str((x.get("object") or {}).get("sha") or "")) for x in items}


def validate_delete_ids(old_ids: set[int], legacy_id: int, archive_id: int,
                        new_ids: set[int]) -> set[int]:
    if (type(legacy_id) is not int or legacy_id <= 0 or type(archive_id) is not int or archive_id <= 0
            or not new_ids or any(type(x) is not int or x <= 0 for x in old_ids | new_ids)):
        raise RetirementError("invalid backup inventory")
    chosen = old_ids | {legacy_id}
    if archive_id in chosen or chosen & new_ids:
        raise RetirementError("retirement would affect the new identity or owner archive")
    return chosen


def _get(storage: cutover.ReleaseStorage, url: str, **kwargs: Any) -> requests.Response:
    response = requests.get(url, headers=state_backup.headers(storage.token), timeout=60, **kwargs)
    response.raise_for_status()
    return response


def _list(storage: cutover.ReleaseStorage, suffix: str, **params: Any) -> list[dict]:
    response = _get(storage, state_backup._repo_base(storage.repo) + suffix, params={"per_page": 100, **params})
    if 'rel="next"' in response.headers.get("Link", ""):
        raise RetirementError("too many private references to verify safely")
    value = response.json()
    if not isinstance(value, list) or not all(isinstance(x, dict) for x in value):
        raise RetirementError("private reference listing is invalid")
    return value


def _tree(storage: cutover.ReleaseStorage, sha: str) -> list[str]:
    response = _get(storage, state_backup._git_api(storage.repo) + "/trees/" + sha, params={"recursive": 1})
    value = response.json()
    if value.get("truncated") is not False:
        raise RetirementError("private tree is truncated")
    result = value.get("tree")
    if not isinstance(result, list) or not all(x.get("type") == "blob" for x in result):
        raise RetirementError("private tree contains unexpected entries")
    return sorted(str(x.get("path") or "") for x in result)


def inspect(path: Path, storage: cutover.ReleaseStorage, owner: str) -> dict[str, Any]:
    if storage.repo != ALLOWED_REPO:
        raise RetirementError("operation is restricted to the authorized private state repository")
    state_backup._assert_private(storage.token, storage.repo)
    active = cutover.activate(path, storage, owner)
    if not active:
        raise RetirementError("fresh identity is not verified")
    profile_marker = json.loads((path / profile.MARKER).read_text(encoding="utf-8"))
    if profile_marker != {"version": 1, "verified": True}:
        raise RetirementError("profile carryover was not verified in the new backup")
    raw_archive, manifest = recovery.download_verified(path, storage.token, storage.repo, storage.password)
    archive_marker = json.loads((path / history.MARKER).read_text(encoding="utf-8"))
    archive_id = archive_marker["asset_id"]
    legacy_id = active.get("legacy_asset_id")
    assets = state_backup._assets(storage.token, storage.repo, storage.release["id"])
    legacy = [x for x in assets if x.get("id") == legacy_id and
              str(x.get("name") or "").startswith("michiflin-legacy-identity-")]
    if len(legacy) != 1:
        raise RetirementError("pinned old identity is unavailable for final comparison")
    if any(str(x.get("name") or "").startswith("michiflin-legacy-identity-") and x.get("id") != legacy_id for x in assets):
        raise RetirementError("additional old identity copies need review")
    encrypted = storage.download(legacy_id)
    old_raw = state_backup.decrypt_backup(encrypted, storage.password)
    state_backup._verify_plaintext(old_raw)
    with tempfile.TemporaryDirectory(prefix="legacy-retirement-check-") as temp:
        root = Path(temp)
        previous = root / "legacy"
        state_backup._install_restored(old_raw, previous)
        old, new = relay.identity(previous), relay.identity(path)
        if old["fingerprint"] == new["fingerprint"] or old["transports"] & new["transports"]:
            raise RetirementError("old and new identities are not separate")
        versions = history.collect_historical_sources(storage, root, old["fingerprint"])
        patterns: set[bytes] = set()
        for folder in [previous, *versions]:
            patterns.update(history._patterns_from_path(folder))
        sources = [history._build_parts(folder, owner, patterns) for folder in [previous, *versions]]
        history.verify_sources_covered(raw_archive, sources)
        old_ids = {int(x.name.removeprefix("routine-")) for x in versions if x.name.startswith("routine-")}
        git_count = sum(x.name.startswith("git-") for x in versions)
        managed = [x for x in assets if state_backup._managed(x)]
        new_ids: set[int] = set()
        for item in managed:
            if item["id"] in old_ids:
                continue
            folder = root / ("routine-" + str(item["id"]))
            if not folder.is_dir() or relay.identity(folder)["fingerprint"] != new["fingerprint"]:
                raise RetirementError("a managed backup has an unknown identity")
            new_ids.add(item["id"])
        delete_ids = validate_delete_ids(old_ids, legacy_id, archive_id, new_ids)

    anchor = state_backup._get_ref(storage.token, storage.repo, state_backup.RELEASE_TAG)
    if not storage.sealed() or not anchor:
        raise RetirementError("fresh identity seal is unavailable")
    heads = _list(storage, "/git/matching-refs/heads/")
    tags = _list(storage, "/git/matching-refs/tags/")
    commits = _list(storage, "/commits", sha="main")
    trees = {x["sha"]: _tree(storage, x["sha"]) for x in commits}
    anchor_commit = _get(storage, state_backup._git_api(storage.repo) + "/commits/" + anchor).json()
    if anchor_commit.get("parents") != []:
        raise RetirementError("backup anchor must have no parents")
    trees[anchor] = _tree(storage, anchor)
    backup_path = os.getenv("STATE_BACKUP_PATH", "michiflin-state.bin")
    head = validate_git_topology(heads, tags, commits, trees, anchor, backup_path)
    if git_count != (0 if head == anchor else len(commits)):
        raise RetirementError("not every old Git revision was recovered")
    return {"head": head, "anchor": anchor, "delete_ids": delete_ids, "new_ids": new_ids,
            "legacy_id": legacy_id, "archive_id": archive_id, "source_versions": manifest["counts"].get("source_versions"),
            "asset_ids": {x["id"] for x in assets}, "heads": _reference_summary(heads), "tags": _reference_summary(tags)}


def execute(storage: cutover.ReleaseStorage, plan: dict[str, Any]) -> int:
    """Force only the explicitly authorized private main, then delete identified old assets."""
    if storage.repo != ALLOWED_REPO or os.getenv("MICHIFLIN_RETIRE_LEGACY") != "1":
        raise RetirementError("explicit one-time private-repository authorization missing")
    state_backup._assert_private(storage.token, storage.repo)
    base = state_backup._git_api(storage.repo)
    # Recheck refs and inventory immediately before any destructive operation.
    current = _get(storage, base + "/ref/heads/main").json()["object"]["sha"]
    if current != plan["head"] or not storage.sealed():
        raise RetirementError("private refs changed; no old assets were deleted")
    if (_reference_summary(_list(storage, "/git/matching-refs/heads/")) != plan["heads"] or
            _reference_summary(_list(storage, "/git/matching-refs/tags/")) != plan["tags"]):
        raise RetirementError("private branches or tags changed; no old assets were deleted")
    now = state_backup._assets(storage.token, storage.repo, storage.release["id"])
    current_ids = {x["id"] for x in now}
    if not plan["delete_ids"] <= current_ids or not plan["new_ids"] <= current_ids or plan["archive_id"] not in current_ids:
        raise RetirementError("backup inventory changed; no old assets were deleted")
    # A newly created backup can race with the inspection; classify it before retrying.
    if any(state_backup._managed(x) and x["id"] not in plan["asset_ids"] for x in now):
        raise RetirementError("a new managed snapshot requires another review")
    if current != plan["anchor"]:
        response = requests.patch(base + "/refs/heads/main", headers=state_backup.headers(storage.token),
                                  json={"sha": plan["anchor"], "force": True}, timeout=60)
        response.raise_for_status()
    if _get(storage, base + "/ref/heads/main").json()["object"]["sha"] != plan["anchor"] or not storage.sealed():
        raise RetirementError("private main or seal did not verify; no old assets deleted")
    removed = 0
    # Keep the pinned legacy source until every older routine snapshot is retired.
    ordered = sorted(plan["delete_ids"] - {plan["legacy_id"]}) + [plan["legacy_id"]]
    for asset_id in ordered:
        response = requests.delete(state_backup._repo_base(storage.repo) + "/releases/assets/" + str(asset_id),
                                   headers=state_backup.headers(storage.token), timeout=60)
        response.raise_for_status()
        removed += 1
    remaining = {x["id"] for x in state_backup._assets(storage.token, storage.repo, storage.release["id"])}
    if plan["delete_ids"] & remaining or plan["archive_id"] not in remaining or not plan["new_ids"] <= remaining or not storage.sealed():
        raise RetirementError("post-retirement verification incomplete")
    return removed


def main() -> int:
    try:
        if os.getenv("OWNER_ONLY") != "1":
            raise RetirementError("owner-only mode required")
        storage = cutover.ReleaseStorage()
        plan = inspect(state_backup.config_dir(), storage, os.environ.get("OWNER_ADDRESS", ""))
        if os.getenv("MICHIFLIN_RETIRE_LEGACY") == "1":
            removed = execute(storage, plan)
            print("Private old identity references retired; old release assets removed:", removed)
        else:
            print("Dry run: old identity and archive verified; old release assets eligible:", len(plan["delete_ids"]))
        return 0
    except Exception as exc:
        print("Legacy retirement stopped (" + type(exc).__name__ + "); inspect the private state before any retry.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
