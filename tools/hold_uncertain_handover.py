#!/usr/bin/env python3
"""Make the first restored handover non-replaying, while preserving old state.

The existing jobs and their status messages are retained. In-flight deliveries
are held for review, not resent. A durable cutoff holds older, possibly already
answered interactions; requests made after the cutoff remain serviceable.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time

FLAG = "clean_handover_replay_v1"
ACTIVE = {"queued", "downloading", "packing", "ready", "sending"}


def hold_inflight(state_file: Path) -> tuple[bool, int]:
    if state_file.is_symlink() or not state_file.is_file():
        raise ValueError("existing state is required")
    data = json.loads(state_file.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("version") != 5
            or not isinstance(data.get("jobs"), dict) or not isinstance(data.get("flags"), dict)):
        raise ValueError("invalid existing state")
    if FLAG in data["flags"]:
        marker = data["flags"][FLAG]
        if (not isinstance(marker, dict) or marker.get("version") != 1
                or type(marker.get("cutoff")) is not int or not 1_000_000_000 < marker["cutoff"] < 4_000_000_000):
            raise ValueError("invalid handover marker")
        return False, 0
    boundary = int(time.time())
    if not 1_000_000_000 < boundary < 4_000_000_000:
        raise ValueError("invalid system clock")
    count = 0
    for job in data["jobs"].values():
        if not isinstance(job, dict):
            raise ValueError("invalid job metadata")
        # Do not delete old status messages or expire historical job records.
        # The normal retention policy for *new* media is unchanged.
        job["preserve_status_on_recovery"] = True
        job["preserve_handover_metadata"] = True
        if (job.get("status") in ACTIVE and isinstance(job.get("inflight"), dict)
                and job["inflight"] and not job.get("hold")):
            job["hold"] = True
            job["error"] = "delivery_unknown"
            count += 1
    data["flags"][FLAG] = {"version": 1, "cutoff": boundary}
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    descriptor, temp = tempfile.mkstemp(prefix=".handover-inflight-", dir=state_file.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, state_file)
    except BaseException:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass
        raise
    return True, count


def main() -> None:
    if (os.getenv("STATE_BACKUP_NAMESPACE") != "owner-clean-v1"
            or os.getenv("STATE_BACKUP_REQUIRED") != "1"
            or os.getenv("STATE_BACKUP_RETAIN_ALL") != "1"
            or os.getenv("STATE_BACKUP_FENCE_REQUIRED") != "1"
            or os.getenv("MICHIFLIN_CLEAN_REPLAY_FENCE_REQUIRED") != "1"):
        raise ValueError("handover backup policy is not armed")
    path = Path(os.environ["CONFIG_DIR"]) / "michiflin-state.json"
    changed, count = hold_inflight(path)
    print("SAFE_HANDOVER_INFLIGHT", "initialized" if changed else "already_initialized", "held", count)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("SAFE_HANDOVER_INFLIGHT_FAILED", type(exc).__name__, file=sys.stderr)
        raise SystemExit(1) from None
