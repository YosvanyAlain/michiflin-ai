"""Apply the owner's one-time opening to verified private contact-link visitors.

No invitation, address, credential, identity or per-user data is exported.
A later emergency close is preserved on every subsequent session.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RELEASE = "private-invites-20261119-v1"
MINIAPP_RELEASE = "public-private-20261119-v1"


def apply() -> bool:
    from michiflin import community, config, state
    if (os.getenv("PUBLIC_ACCESS_RELEASE") != RELEASE or os.getenv("STATE_BACKUP_REQUIRED") != "1"
            or config.MINIAPP_RELEASE != MINIAPP_RELEASE or config.OWNER_ONLY
            or not config.INVITE_AUTO_ACCESS or not config.REVOKE_LEGACY_MINIAPPS
            or config.MAINTENANCE_MODE or not config.OWNER_ADDRESS):
        raise ValueError("public private-chat release is not armed")
    if not state.STATE_FILE.is_file():
        raise ValueError("existing state could not be loaded")
    state.load()  # returns whether a legacy migration occurred, not load success
    with state.lock:
        if state.data.get("public_access_release") == RELEASE:
            return False  # never undo an emergency close after the initial cutover
        previous_policy = copy.deepcopy(state.data.get("invite_policy"))
        previous_release = state.data.get("public_access_release")
        state.data["invite_policy"] = {"closed": False, "until": 0.0}
        state.data["public_access_release"] = RELEASE
        try:
            state.save()
            community._checkpoint()  # required encrypted backup, verified by the deployer
        except Exception:
            if previous_policy is None:
                state.data.pop("invite_policy", None)
            else:
                state.data["invite_policy"] = previous_policy
            if previous_release is None:
                state.data.pop("public_access_release", None)
            else:
                state.data["public_access_release"] = previous_release
            state.save()
            raise
    return True


if __name__ == "__main__":
    try:
        changed = apply()
        print("SAFE_PRIVATE_INVITE_ACCESS " + ("applied" if changed else "already_applied"))
    except Exception as exc:
        print("SAFE_PRIVATE_INVITE_ACCESS_FAILED " + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
