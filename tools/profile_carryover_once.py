#!/usr/bin/env python3
"""Carry a previous profile description into the separately verified new account.

Runs offline after restoring the new account and before starting it. Only the
human-readable description is copied; old addresses, invite links, credentials,
key material, chat bindings and jobs are never transferred. The legacy archive
is never removed by this tool. No profile content is printed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import state_backup  # noqa: E402
from michiflin import profile  # noqa: E402
from tools import cutover_identity_once as cutover, migrate_relay_once as relay  # noqa: E402

MARKER = ".legacy-profile-carryover-v1.json"


def merge_description(current: str, previous: str, old_addresses: set[str]) -> str:
    """Preserve distinct safe lines, never re-advertise an old identity or invite."""
    forbidden = {value.casefold() for value in old_addresses if value}
    lines = []
    seen = set()
    for source in (previous, current):
        for line in str(source or "").splitlines():
            line = line.strip()
            if not line or len(line) > 4096:
                continue
            decoded = unquote(unquote(line)).casefold()
            if (any(address in decoded for address in forbidden)
                    or re.search(r"(?:https?://i\.delta\.chat/|dcaccount:|securejoin:)", decoded)):
                continue
            key = line.casefold()
            if key not in seen:
                seen.add(key)
                lines.append(line)
    result = profile.profile_status("\n".join(lines))
    if len(result) > 4096:
        raise ValueError("profile description is too long to preserve safely")
    return result


def carryover(path: Path, storage: cutover.ReleaseStorage, owner: str) -> str:
    marker = path / MARKER
    if marker.is_file():
        value = json.loads(marker.read_text(encoding="utf-8"))
        if value == {"version": 1, "verified": True}:
            return "already_verified"
        raise ValueError("invalid profile marker")
    active = cutover.activate(path, storage, owner)
    if not active:
        return "no_fresh_identity"
    asset_id = active.get("legacy_asset_id")
    if type(asset_id) is not int or asset_id <= 0:
        raise ValueError("legacy recovery copy is not identified")
    matches = [item for item in state_backup._assets(storage.token, storage.repo, storage.release["id"])
               if item.get("id") == asset_id and str(item.get("name") or "").startswith("michiflin-legacy-identity-")]
    if len(matches) != 1:
        raise ValueError("legacy recovery copy is unavailable")
    plaintext = state_backup.decrypt_backup(storage.download(asset_id), storage.password)
    state_backup._verify_plaintext(plaintext)
    with tempfile.TemporaryDirectory(prefix="profile-carryover-") as temp:
        legacy = Path(temp) / "legacy"
        state_backup._install_restored(plaintext, legacy)
        old_identity, new_identity = relay.identity(legacy), relay.identity(path)
        if (old_identity["fingerprint"] == new_identity["fingerprint"]
                or old_identity["transports"] & new_identity["transports"]):
            raise ValueError("old and new identities are not separate")
        with relay.account_rpc(legacy / "accounts") as old_rpc, relay.account_rpc(path / "accounts") as new_rpc:
            old_id, new_id = relay.only_account(old_rpc), relay.only_account(new_rpc)
            previous = old_rpc.get_config(old_id, "selfstatus") or ""
            current = new_rpc.get_config(new_id, "selfstatus") or ""
            merged = merge_description(current, previous, old_identity["transports"])
            if merged != current:
                new_rpc.set_config(new_id, "selfstatus", merged)
            if new_rpc.get_config(new_id, "selfstatus") != merged:
                raise ValueError("updated profile did not verify")
    cutover._write_json(marker, {"version": 1, "verified": True})
    return "verified"


def main() -> int:
    try:
        if os.getenv("OWNER_ONLY") != "1":
            if os.getenv("INVITE_AUTO_ACCESS") == "1":
                print("Profile carryover skipped while verified invitations are enabled.")
                return 0
            raise ValueError("owner-only mode required")
        path = Path(os.environ["CONFIG_DIR"])
        outcome = carryover(path, cutover.ReleaseStorage(), os.environ.get("OWNER_ADDRESS", ""))
        print("Profile carryover:", outcome)
        return 0
    except Exception as exc:
        print("Profile carryover not completed (" + type(exc).__name__ + "); original backup retained.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
