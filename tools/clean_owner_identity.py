#!/usr/bin/env python3
"""Seed and activate a separately paired, owner-only fresh identity.

No legacy account, contacts, messages, jobs, app data or backup are read by
this module. The old encrypted assets remain untouched. Seeding is a deliberate
operator action after out-of-band owner confirmation; normal sessions only
restore and activate an independently verified clean-namespace backup.
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
import state_backup  # noqa: E402
from michiflin import config, identity_guard  # noqa: E402
from tools import cutover_identity_once as verified  # noqa: E402
from tools import migrate_relay_once as relay  # noqa: E402


class CleanIdentityError(RuntimeError):
    pass


def _require(value: Any, reason: str) -> None:
    if not value:
        raise CleanIdentityError(reason)


def make_marker(metadata: dict[str, Any], owner: str, epoch: str) -> dict[str, Any]:
    observed = str(metadata.get("owner_address") or "").strip().casefold()
    expected = str(owner or "").strip().casefold()
    marker = {"version": 1, "confirmed": True, "fresh_account": True, "epoch": epoch,
              "owner_address": expected,
              "owner_fingerprint": identity_guard.normalize_fingerprint(metadata.get("owner_fingerprint", "")),
              "bot_fingerprint": identity_guard.normalize_fingerprint(metadata.get("bot_fingerprint", "")),
              "owner_chat": metadata.get("chat_id")}
    _require(metadata.get("verified") is True and expected and observed == expected, "owner_not_verified")
    _require(state_backup._clean_origin_valid(marker), "clean_marker_invalid")
    return marker


def _no_other_contacts(candidate: Path, expected_owner: str) -> None:
    """Fail closed rather than carrying any unrelated contacts into the seed."""
    expected = expected_owner.strip().casefold()
    with relay.account_rpc(candidate / "accounts") as rpc:
        account = relay.only_account(rpc)
        # Delta Chat normally lists key contacts; 0x04 additionally lists
        # address contacts. Inspect both, plus blocked contacts, without deletion.
        ids = set(rpc.get_contact_ids(account, 0, None)) | set(rpc.get_contact_ids(account, 0x04, None))
        contacts = [rpc.get_contact(account, cid) for cid in ids if int(cid) > 9]
        contacts += list(rpc.get_blocked_contacts(account))
        for contact in contacts:
            address = str(getattr(contact, "address", "") or "").strip().casefold()
            _require(not address or address == expected, "unexpected_contact_in_new_account")


def prepare(candidate: Path, metadata: dict[str, Any], owner: str, epoch: str) -> dict[str, Any]:
    """Start with an independent verified account and a genuinely empty state."""
    _require(candidate.is_dir() and {p.name for p in candidate.iterdir()} == {"accounts"}, "candidate_not_clean")
    _require(re.fullmatch(r"[0-9a-f]{48}", epoch) is not None, "bad_epoch")
    _require(os.getenv("MICHIFLIN_CLEAN_EPOCH", "") == epoch, "epoch_environment_mismatch")
    marker = make_marker(metadata, owner, epoch)
    verified.validate_candidate(candidate, metadata, owner, require_probe=True)
    _no_other_contacts(candidate, owner)
    # This new account has no old tasks. Do not replay any messages accumulated
    # during pairing, including the explicit verification probe. Do not send an
    # unsolicited read receipt; the conversation remains in the new account.
    with relay.account_rpc(candidate / "accounts") as rpc:
        account = relay.only_account(rpc)
        pending = rpc.get_next_msgs(account)
        if pending:
            rpc.set_config(account, "last_msg_id", str(max(int(item) for item in pending)))
    verified._write_json(candidate / state_backup.CLEAN_OWNER_ORIGIN, marker)
    verified._write_json(candidate / "michiflin-state.json", {"version": 5, "flags": {}})
    plaintext = state_backup.archive(candidate)
    state_backup._verify_plaintext(plaintext)
    state_backup._verify_clean_origin(plaintext)
    return marker


def _validate_recovered(path: Path, marker: dict[str, Any], owner: str, *, require_probe: bool) -> None:
    recovered = json.loads((path / state_backup.CLEAN_OWNER_ORIGIN).read_text(encoding="utf-8"))
    _require(recovered == marker and state_backup._clean_origin_valid(recovered), "recovered_marker_mismatch")
    metadata = {"verified": True, "owner_address": marker["owner_address"],
                "owner_fingerprint": marker["owner_fingerprint"], "bot_fingerprint": marker["bot_fingerprint"],
                "chat_id": marker["owner_chat"]}
    verified.validate_candidate(path, metadata, owner, require_probe=require_probe,
                                allow_owner_rotation=False)
    _no_other_contacts(path, owner)
    state = json.loads((path / "michiflin-state.json").read_text(encoding="utf-8"))
    _require(state == {"version": 5, "flags": {}}, "new_runtime_state_not_empty")
    _require(not (path / ".github-native-origin-v1.json").exists()
             and not (path / verified.MARKER).exists()
             and not (path / verified.LEGACY_BINDINGS).exists(), "legacy_marker_present")


def seed(candidate: Path, metadata: dict[str, Any], owner: str, epoch: str) -> str:
    """Publish only a verified new-account seed, never restore an old snapshot."""
    _require(os.getenv("STATE_BACKUP_NAMESPACE", "") == state_backup.CLEAN_OWNER_NAMESPACE, "wrong_namespace")
    _require(os.getenv("STATE_BACKUP_STORAGE", "releases") == "releases", "wrong_storage")
    settings = state_backup.configuration()
    _require(settings is not None, "backup_credentials_missing")
    token, repo, password = settings
    state_backup._assert_private(token, repo)
    release = state_backup._release(token, repo)
    _require(release is not None, "private_release_missing")
    anchor = state_backup._get_ref(token, repo, state_backup.RELEASE_TAG)
    seal = state_backup._get_ref(token, repo, state_backup.CLEAN_OWNER_READY_TAG)
    _require(anchor and (not seal or seal == anchor), "clean_seal_mismatch")
    # A sealed account may already contain new activity. Never replace it with
    # the original seed, even when a later operator retries the same command.
    _require(not seal, "clean_identity_already_sealed")
    marker = prepare(candidate, metadata, owner, epoch)
    existing = sorted((x for x in state_backup._assets(token, repo, release["id"]) if state_backup._managed(x)),
                      key=lambda x: (state_backup._created(x), x["id"]), reverse=True)
    if not existing:
        old_dir = os.environ.get("CONFIG_DIR")
        try:
            os.environ["CONFIG_DIR"] = str(candidate)
            state_backup.save(rotate=False)
        finally:
            if old_dir is None:
                os.environ.pop("CONFIG_DIR", None)
            else:
                os.environ["CONFIG_DIR"] = old_dir
        existing = sorted((x for x in state_backup._assets(token, repo, release["id"]) if state_backup._managed(x)),
                          key=lambda x: (state_backup._created(x), x["id"]), reverse=True)
    _require(existing, "clean_seed_upload_missing")
    # Verify the downloaded encrypted bytes by extraction and live account/key
    # checks *before* the new namespace becomes eligible for restoration.
    blob = state_backup._download_asset(token, repo, existing[0]["id"])
    plaintext = state_backup.decrypt_backup(blob, password)
    state_backup._verify_plaintext(plaintext)
    state_backup._verify_clean_origin(plaintext)
    with tempfile.TemporaryDirectory(prefix="clean-seed-check-") as root:
        recovered = Path(root) / "recovered"
        state_backup._install_restored(plaintext, recovered)
        _validate_recovered(recovered, marker, owner, require_probe=True)
    state_backup._make_ref(token, repo, state_backup.CLEAN_OWNER_READY_TAG, anchor)
    _require(state_backup._get_ref(token, repo, state_backup.CLEAN_OWNER_READY_TAG) == anchor, "clean_seal_unverified")
    with tempfile.TemporaryDirectory(prefix="clean-restore-check-") as root:
        old_dir = os.environ.get("CONFIG_DIR")
        try:
            recovered = Path(root) / "recovered"
            os.environ["CONFIG_DIR"] = str(recovered)
            state_backup.restore()
            _validate_recovered(recovered, marker, owner, require_probe=True)
        finally:
            if old_dir is None:
                os.environ.pop("CONFIG_DIR", None)
            else:
                os.environ["CONFIG_DIR"] = old_dir
    return "verified_and_sealed"


def activate(path: Path, owner: str) -> dict[str, Any]:
    """Verify the new account on every later session; never initialize anew."""
    _require(os.getenv("STATE_BACKUP_NAMESPACE", "") == state_backup.CLEAN_OWNER_NAMESPACE, "wrong_namespace")
    settings = state_backup.configuration()
    _require(settings is not None, "backup_credentials_missing")
    token, repo, _password = settings
    anchor = state_backup._get_ref(token, repo, state_backup.RELEASE_TAG)
    _require(anchor and state_backup._get_ref(token, repo, state_backup.CLEAN_OWNER_READY_TAG) == anchor,
             "clean_seal_missing")
    marker = json.loads((path / state_backup.CLEAN_OWNER_ORIGIN).read_text(encoding="utf-8"))
    _require(state_backup._clean_origin_valid(marker), "clean_marker_invalid")
    _require(marker["owner_address"] == str(owner or "").strip().casefold(), "owner_mismatch")
    metadata = {"verified": True, "owner_address": marker["owner_address"],
                "owner_fingerprint": marker["owner_fingerprint"], "bot_fingerprint": marker["bot_fingerprint"],
                "chat_id": marker["owner_chat"]}
    verified.validate_candidate(path, metadata, owner, require_probe=False,
                                allow_owner_rotation=config.EMAIL_KEY_RECOVERY)
    return marker


def main() -> None:
    _require(len(sys.argv) == 2 and sys.argv[1] == "activate", "usage")
    marker = activate(Path(os.environ["CONFIG_DIR"]), os.environ.get("OWNER_ADDRESS", ""))
    environment = Path(os.environ["GITHUB_ENV"])
    with environment.open("a", encoding="utf-8") as stream:
        stream.write("BOT_EPOCH=" + marker["epoch"] + "\n")
        stream.write("BOT_EPOCH_STRICT=1\nBOT_VERIFIED_CONTACTS_ONLY=1\n")
        stream.write("OWNER_PGP_FINGERPRINT=" + marker["owner_fingerprint"] + "\n")
        stream.write("MICHIFLIN_NATIVE_FENCE_REQUIRED=0\nMICHIFLIN_GITHUB_RESUME_ONCE=0\n")
        stream.write("ALLOWED_SENDERS=\nMAILBOX_PROFILES_JSON=\nOWNER_NOTICE=\n")
    print("SAFE_CLEAN_OWNER_ACTIVATED")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Never log an address, fingerprint, token, secret or message contents.
        print("SAFE_CLEAN_OWNER_FAILED", type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
