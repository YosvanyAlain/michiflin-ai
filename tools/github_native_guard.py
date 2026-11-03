#!/usr/bin/env python3
"""Fail-closed GitHub-native return; all older encrypted assets are retained.

No account is started here. The transition temporarily prefixes the backup
release description so an obsolete runner cannot restore any old asset. It
then renames (never deletes) default-namespace assets, verifies their bytes,
creates a native-ready tag at the unchanged identity anchor, and unlocks.
An interrupted operation leaves the lock in place and is safe to retry.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests  # noqa: E402
import state_backup  # noqa: E402

PREVIOUS_RUN = 41568086969
SOURCE_ASSET = 699441108
SEED_ASSET = 704130339
NATIVE_READY = "michiflin-github-native-ready-v1"
LOCK = "GITHUB NATIVE RETURN LOCK v1: no legacy restore during transition.\n"
RETAINED = "preserved-github-return-v1-"
OLD = re.compile(r"^michiflin-(?:state|pending)-\d{8}T\d{6}Z-[0-9a-f]{16}\.bin$")
ORIGIN = ".github-native-origin-v1.json"
FLAG = "github_native_cutover_v1"


def require(ok: Any, reason: str) -> None:
    if not ok:
        raise RuntimeError(reason)


def public_api(path: str) -> Any:
    repo = os.environ["GITHUB_REPOSITORY"]
    require(repo == "YosvanyAlain/michiflin-ai", "wrong_public_repository")
    token = os.environ["MICHIFLIN_ACTIONS_TOKEN"]
    response = requests.get(f"https://api.github.com/repos/{repo}{path}", headers=state_backup.headers(token), timeout=60)
    response.raise_for_status()
    return response.json()


def previous_cutoff() -> int:
    run = public_api(f"/actions/runs/{PREVIOUS_RUN}")
    require(run.get("status") == "completed" and run.get("head_sha") == "e970d056ede3f710bd303096ba2d74a682e06b92",
            "previous_run_not_completed")
    token = os.environ["MICHIFLIN_ACTIONS_TOKEN"]
    repo = os.environ["GITHUB_REPOSITORY"]
    response = requests.get(f"https://api.github.com/repos/{repo}/actions/runs/{PREVIOUS_RUN}/logs",
                            headers=state_backup.headers(token), timeout=90)
    response.raise_for_status()
    require(len(response.content) < 50_000_000, "previous_log_too_large")
    drains: list[datetime] = []
    stops: list[datetime] = []
    backups: list[datetime] = []
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        for name in archive.namelist():
            if not name.endswith(".txt"):
                continue
            require(archive.getinfo(name).file_size < 80_000_000, "previous_log_entry_too_large")
            for line in archive.read(name).decode("utf-8", errors="replace").splitlines():
                if ("stopping (" not in line and "stopped cleanly:" not in line
                        and "[backup] encrypted and verified snapshot saved" not in line):
                    continue
                match = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z)", line)
                if not match:
                    continue
                stamp = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
                if "stopping (" in line and "finishing work in progress" in line:
                    drains.append(stamp)
                if "stopped cleanly: 0 chats busy, 0 app operations, 0 messages unsent" in line:
                    stops.append(stamp)
                if "[backup] encrypted and verified snapshot saved" in line:
                    backups.append(stamp)
    require(bool(stops) and any(item > max(stops) for item in backups), "previous_shutdown_not_verified")
    prior_drains = [item for item in drains if item <= max(stops)]
    require(bool(prior_drains), "previous_drain_boundary_missing")
    # The old bot stops admitting new work at drain start, not at backup end.
    cutoff = math.ceil(max(prior_drains).timestamp())
    require(1_793_600_000 < cutoff < int(datetime.now(timezone.utc).timestamp()) + 60, "previous_cutoff_invalid")
    return cutoff


def raw_release(token: str, repo: str) -> dict:
    response = requests.get(f"{state_backup._repo_base(repo)}/releases/tags/{state_backup.RELEASE_TAG}",
                            headers=state_backup.headers(token), timeout=30)
    response.raise_for_status()
    release = response.json()
    require(isinstance(release.get("id"), int) and release.get("tag_name") == state_backup.RELEASE_TAG,
            "release_invalid")
    return release


def body_set(token: str, repo: str, release_id: int, body: str) -> None:
    response = requests.patch(f"{state_backup._repo_base(repo)}/releases/{release_id}",
                              headers=state_backup.headers(token), json={"body": body}, timeout=60)
    response.raise_for_status()
    require(response.json().get("body") == body, "release_lock_unverified")


def verify_seed(token: str, repo: str, password: str, assets: list[dict]) -> None:
    items = [a for a in assets if a.get("id") == SEED_ASSET and state_backup._managed(a)]
    require(len(items) == 1, "verified_native_seed_missing")
    plain = state_backup.decrypt_backup(state_backup._download_asset(token, repo, SEED_ASSET), password)
    state_backup._verify_plaintext(plain)
    with tempfile.TemporaryDirectory(prefix="native-guard-") as folder:
        path = Path(folder) / "state"
        state_backup._install_restored(plain, path)
        origin = json.loads((path / ORIGIN).read_text(encoding="utf-8"))
        data = json.loads((path / "michiflin-state.json").read_text(encoding="utf-8"))
        mark = (data.get("flags") or {}).get(FLAG)
        require(origin.get("version") == 1 and origin.get("source_asset_id") == SOURCE_ASSET
                and isinstance(mark, dict) and mark.get("source_asset_id") == SOURCE_ASSET
                and (data.get("maintenance_notice") or {}).get("silent") is True, "native_seed_provenance")


def guard() -> None:
    require(os.getenv("STATE_BACKUP_NAMESPACE") == state_backup.GITHUB_NATIVE_NAMESPACE, "wrong_namespace")
    settings = state_backup.configuration()
    require(settings, "backup_credentials_missing")
    token, repo, password = settings
    state_backup._assert_private(token, repo)
    anchor = state_backup._get_ref(token, repo, state_backup.RELEASE_TAG)
    require(anchor and state_backup._get_ref(token, repo, state_backup.READY_TAG) == anchor
            and state_backup._get_ref(token, repo, "michiflin-fresh-identity-v1") == anchor, "identity_anchor_invalid")
    release = raw_release(token, repo)
    rid = release["id"]
    body = release.get("body") or ""
    locked = body.startswith(LOCK)
    original_body = body[len(LOCK):] if locked else body
    require(original_body.startswith(state_backup.RELEASE_MARKER), "backup_release_not_owned")
    assets = state_backup._assets(token, repo, rid)
    verify_seed(token, repo, password, assets)
    old = [a for a in assets if isinstance(a.get("name"), str) and OLD.fullmatch(a["name"])]
    native_ready = state_backup._get_ref(token, repo, NATIVE_READY)
    require(not native_ready or native_ready == anchor, "native_ready_marker_mismatch")
    if native_ready and not locked:
        require(not old, "unexpected_legacy_backups_after_cutover")
        print("SAFE_NATIVE_GUARD", json.dumps({"legacy_namespace_empty": True, "native_marker_verified": True}), flush=True)
        return
    if os.getenv("MICHIFLIN_NATIVE_GUARD_DRY", "0") == "1":
        # Read-only while the current session is still running; real execution
        # additionally requires the verified clean shutdown below.
        print("SAFE_NATIVE_GUARD_DRY", json.dumps({"legacy_assets_to_preserve": len(old), "seed_verified": True,
              "would_lock_and_rename_only": True}), flush=True)
        return
    # Establish the former process actually stopped and verified its final
    # backup before making any metadata changes to the previous namespace.
    previous_cutoff()
    if not locked:
        body_set(token, repo, rid, LOCK + original_body)
    require(raw_release(token, repo).get("body") == LOCK + original_body, "release_lock_lost")
    # As long as LOCK is visible, old _release() fails before downloading any
    # snapshot; interrupted renames cannot cause a fallback to the old host.
    renamed = 0
    for item in sorted(old, key=lambda a: (str(a.get("created_at") or ""), a["id"]), reverse=True):
        aid, name, size = item["id"], item["name"], item.get("size")
        target = RETAINED + name
        require(not any(a.get("id") != aid and a.get("name") == target for a in assets), "retained_name_collision")
        before = state_backup._download_asset(token, repo, aid)
        require(len(before) == size, "retained_size_before")
        response = requests.patch(f"{state_backup._repo_base(repo)}/releases/assets/{aid}",
                                  headers=state_backup.headers(token), json={"name": target}, timeout=60)
        response.raise_for_status()
        changed = response.json()
        require(changed.get("id") == aid and changed.get("name") == target and changed.get("size") == size
                and changed.get("state") == "uploaded", "asset_metadata_changed")
        after = state_backup._download_asset(token, repo, aid)
        require(hashlib.sha256(before).digest() == hashlib.sha256(after).digest(), "retained_bytes_changed")
        renamed += 1
    remaining = state_backup._assets(token, repo, rid)
    require(not any(isinstance(a.get("name"), str) and OLD.fullmatch(a["name"]) for a in remaining),
            "legacy_namespace_not_empty")
    if not native_ready:
        state_backup._make_ref(token, repo, NATIVE_READY, anchor)
    require(state_backup._get_ref(token, repo, NATIVE_READY) == anchor, "native_ready_not_verified")
    body_set(token, repo, rid, original_body)
    require(state_backup._release(token, repo)["id"] == rid, "release_unlock_unverified")
    print("SAFE_NATIVE_GUARD", json.dumps({"legacy_assets_preserved_and_verified": renamed,
          "legacy_namespace_empty": True, "native_marker_verified": True, "identity_anchor_unchanged": True}), flush=True)


def arm() -> None:
    path = Path(os.environ["CONFIG_DIR"])
    origin = json.loads((path / ORIGIN).read_text(encoding="utf-8"))
    require(origin.get("version") == 1 and origin.get("source_asset_id") == SOURCE_ASSET, "restored_origin_invalid")
    file = path / "michiflin-state.json"
    data = json.loads(file.read_text(encoding="utf-8"))
    flags = data.get("flags") or {}
    marker = flags.get(FLAG)
    require(isinstance(marker, dict) and marker.get("source_asset_id") == SOURCE_ASSET, "restored_fence_missing")
    cutoff = marker.get("cutoff")
    if cutoff is not None:
        require(isinstance(cutoff, (int, float)) and 1_793_600_000 < cutoff < 4_000_000_000,
                "restored_cutoff_invalid")
        print("SAFE_NATIVE_FENCE", json.dumps({"retained": True, "cutoff_present": True}), flush=True)
        return
    require((data.get("maintenance_notice") or {}).get("silent") is True
            and (data.get("maintenance_notice") or {}).get("active") is True, "quiet_maintenance_missing")
    cutoff = previous_cutoff()
    marker["cutoff"] = cutoff
    tmp = file.with_name(file.name + ".github-native-tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(file)
    state_backup.save(rotate=False)
    with tempfile.TemporaryDirectory(prefix="native-fence-check-") as folder:
        before = os.environ["CONFIG_DIR"]
        try:
            os.environ["CONFIG_DIR"] = str(Path(folder) / "check")
            state_backup.restore()
            recovered = json.loads((Path(os.environ["CONFIG_DIR"]) / "michiflin-state.json").read_text(encoding="utf-8"))
            require((recovered.get("flags") or {}).get(FLAG) == marker, "cutoff_recovery_mismatch")
        finally:
            os.environ["CONFIG_DIR"] = before
    print("SAFE_NATIVE_FENCE", json.dumps({"cutoff_from_clean_shutdown": True, "quiet": True,
          "independent_recovery": True}), flush=True)


if __name__ == "__main__":
    try:
        require(len(sys.argv) == 2 and sys.argv[1] in {"guard", "arm"}, "usage")
        (guard if sys.argv[1] == "guard" else arm)()
    except Exception as exc:
        # Request errors may include sensitive URLs; print only safe categories.
        print("SAFE_NATIVE_TRANSITION_FAILED", type(exc).__name__, str(exc) if isinstance(exc, RuntimeError) else "", flush=True)
        sys.exit(1)
