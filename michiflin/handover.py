"""Non-destructive replay fence for the explicit return to older GitHub state.

Messages held here stay in Delta Chat, and WebXDC update serials are recorded
with their instance ids. No old request is answered, executed or deleted.
New requests use their timestamped id or the new process's unguessable session.
"""
from __future__ import annotations

import os
import re
from typing import Any

from . import state

SOURCE_ASSET_ID = 699441108
FLAG = "github_native_cutover_v1"
HOLD = "github_native_held_v1"


class FenceNotReady(RuntimeError):
    """Do not take messages before the verified cutover cutoff is loaded."""


def cutoff() -> float | None:
    with state.lock:
        flags = state.data.get("flags")
        marker = flags.get(FLAG) if isinstance(flags, dict) else None
    required = os.getenv("MICHIFLIN_NATIVE_FENCE_REQUIRED", "0").strip() == "1"
    if marker is None and not required:
        return None  # other installations retain their existing behavior
    if not isinstance(marker, dict) or marker.get("source_asset_id") != SOURCE_ASSET_ID:
        raise FenceNotReady("cutover marker unavailable")
    try:
        result = float(marker["cutoff"])
    except (KeyError, TypeError, ValueError):
        raise FenceNotReady("cutover cutoff unavailable") from None
    if not 1_000_000_000 < result < 4_000_000_000:
        raise FenceNotReady("cutover cutoff invalid")
    return result


def hold_message(message: Any) -> bool:
    boundary = cutoff()
    if boundary is None:
        return False
    try:
        sent = float(getattr(message, "timestamp", 0) or 0)
    except (ValueError, TypeError):
        sent = 0
    return not (sent > boundary)


def hold_update(payload: Any, current_session: str) -> bool:
    boundary = cutoff()
    if boundary is None or not isinstance(payload, dict) or payload.get("t") not in {"req", "req_chunk"}:
        return False
    # A process-only token cannot appear in the earlier deployment's requests.
    if payload.get("session") == current_session and current_session:
        return False
    # The shipped WebXDC creates ids with Date.now().toString(36) plus five
    # random base36 characters. Older/unknown formats stay held, not discarded.
    rid = payload.get("id")
    if not isinstance(rid, str) or not re.fullmatch(r"[0-9a-z]{13}", rid):
        return True
    try:
        created_ms = int(rid[:-5], 36)
    except ValueError:
        return True
    return not (created_ms > int(boundary * 1000))


def remember(kind: str, identity: str, details: dict[str, Any]) -> None:
    """Checkpoint references for later review without copying/deleting payloads."""
    if kind not in {"messages", "updates"}:
        raise ValueError("invalid hold kind")
    with state.lock:
        all_held = state.data.setdefault(HOLD, {})
        if not isinstance(all_held, dict):
            raise FenceNotReady("invalid held-record ledger")
        group = all_held.setdefault(kind, {})
        if not isinstance(group, dict):
            raise FenceNotReady("invalid held-record group")
        if identity not in group:
            group[identity] = details
            state.save()  # no cursor is advanced if the checkpoint fails
