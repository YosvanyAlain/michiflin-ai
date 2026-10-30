#!/usr/bin/env python3
"""One-time, fail-closed transition to a separately verified Delta Chat identity.

This procedure never registers an account or prints invitations, fingerprints,
addresses, keys, messages or exception contents. It preserves a full, encrypted,
independently restored legacy snapshot before touching the active account. A
private repository seal prevents a future fallback from silently starting the
old account again. Current jobs are retained but cannot run under the new epoch.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sys
import tempfile
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests  # noqa: E402
import state_backup  # noqa: E402
from michiflin import config, identity_guard  # noqa: E402
from tools import migrate_relay_once as relay  # noqa: E402

MARKER = ".verified-identity-v1.json"
LEGACY_BINDINGS = ".pre-identity-bindings-v1.json"
SEAL = "michiflin-fresh-identity-v1"
PROBE = "MICHIFLIN PRUEBA SEGURA"
MAX_BUNDLE_BYTES = 4_000_000


class CutoverError(RuntimeError):
    pass


def seal_bytes(raw: bytes, password: str) -> bytes:
    salt = os.urandom(state_backup.SALT_BYTES)
    return state_backup.MAGIC + salt + state_backup.fernet_from(password, salt).encrypt(raw)


def make_bundle(account: bytes, verification: bytes, password: str, *, confirmed: bool) -> bytes:
    if not confirmed:
        raise CutoverError("out-of-band owner confirmation is required")
    raw_account = state_backup.decrypt_backup(account, password)
    state_backup._verify_plaintext(raw_account)
    metadata = json.loads(state_backup.decrypt_backup(verification, password))
    if not isinstance(metadata, dict) or metadata.get("verified") is not True:
        raise CutoverError("candidate proof is missing")
    packet = json.dumps({"version": 1, "confirmed": True,
                         "account": base64.b64encode(account).decode("ascii"),
                         "verification": base64.b64encode(verification).decode("ascii")},
                        separators=(",", ":")).encode()
    sealed = seal_bytes(packet, password)
    if len(sealed) > MAX_BUNDLE_BYTES:
        raise CutoverError("candidate bundle exceeds safe size")
    return sealed


def unpack_bundle(blob: bytes, password: str, path: Path) -> dict[str, Any]:
    if not blob or len(blob) > MAX_BUNDLE_BYTES or path.exists():
        raise CutoverError("invalid candidate bundle or destination")
    packet = json.loads(state_backup.decrypt_backup(blob, password))
    if not isinstance(packet, dict) or packet.get("version") != 1 or packet.get("confirmed") is not True:
        raise CutoverError("candidate confirmation is absent")
    account = base64.b64decode(packet["account"], validate=True)
    verification = base64.b64decode(packet["verification"], validate=True)
    raw_account = state_backup.decrypt_backup(account, password)
    state_backup._verify_plaintext(raw_account)
    metadata = json.loads(state_backup.decrypt_backup(verification, password))
    if not isinstance(metadata, dict) or metadata.get("verified") is not True:
        raise CutoverError("candidate verification is absent")
    state_backup._install_restored(raw_account, path)
    return metadata


@contextmanager
def strict_owner(address: str, fingerprint: str) -> Iterator[None]:
    previous = (config.BOT_VERIFIED_CONTACTS_ONLY, config.OWNER_ADDRESS, config.OWNER_PGP_FINGERPRINT,
                config.EMAIL_KEY_RECOVERY)
    config.BOT_VERIFIED_CONTACTS_ONLY = True
    config.OWNER_ADDRESS = address
    config.OWNER_PGP_FINGERPRINT = fingerprint
    config.EMAIL_KEY_RECOVERY = False  # a new identity cutover always needs a real pin
    try:
        yield
    finally:
        (config.BOT_VERIFIED_CONTACTS_ONLY, config.OWNER_ADDRESS, config.OWNER_PGP_FINGERPRINT,
         config.EMAIL_KEY_RECOVERY) = previous


def validate_candidate(path: Path, metadata: dict[str, Any], expected_owner: str, *,
                       require_probe: bool = True, allow_owner_rotation: bool = False) -> dict[str, Any]:
    expected_owner = str(expected_owner or "").strip().casefold()
    observed_owner = str(metadata.get("owner_address") or "").strip().casefold()
    owner_fp = identity_guard.normalize_fingerprint(metadata.get("owner_fingerprint", ""))
    bot_fp = identity_guard.normalize_fingerprint(metadata.get("bot_fingerprint", ""))
    owner_chat = metadata.get("chat_id")
    if (not expected_owner or observed_owner != expected_owner or not owner_fp or not bot_fp
            or owner_fp == bot_fp or type(owner_chat) is not int or owner_chat <= 9):
        raise CutoverError("candidate does not match the configured owner")
    candidate = relay.identity(path)
    if len(candidate["transports"]) != 1 or candidate["fingerprint"].upper() != bot_fp:
        raise CutoverError("candidate identity does not match the confirmed key")
    # For an already sealed deployment only, the owner explicitly may allow
    # email-based key changes. Keep validating the bot's own identity and the
    # private seal, but do not require the phone's obsolete key to survive.
    if allow_owner_rotation:
        if require_probe:
            raise CutoverError("new identity cutovers still require a verified owner probe")
        return candidate
    with strict_owner(expected_owner, owner_fp), relay.account_rpc(path / "accounts") as rpc:
        account = relay.only_account(rpc)
        if not identity_guard.authorized_chat(rpc, account, owner_chat, expected_owner):
            raise CutoverError("owner chat is not directly verified")
        if require_probe:
            verified = False
            for mid in rpc.search_messages(account, PROBE, None):
                msg = rpc.get_message(account, mid)
                if (msg.text == PROBE and int(msg.chat_id) == owner_chat and
                        identity_guard.authorized_chat(rpc, account, owner_chat, expected_owner,
                                                       contact_id=int(msg.from_id), message=msg)):
                    verified = True
                    break
            if not verified:
                raise CutoverError("signed owner test message is missing")
    return candidate


def mark_probe_seen(path: Path, metadata: dict[str, Any], owner: str) -> None:
    """Do not replay the completed private handshake test as an agent request."""
    chat_id = int(metadata["chat_id"])
    with strict_owner(owner, metadata["owner_fingerprint"]), relay.account_rpc(path / "accounts") as rpc:
        account = relay.only_account(rpc)
        matched = []
        for mid in rpc.search_messages(account, PROBE, None):
            msg = rpc.get_message(account, mid)
            if (msg.text == PROBE and int(msg.chat_id) == chat_id and
                    identity_guard.authorized_chat(rpc, account, chat_id, owner,
                                                   contact_id=int(msg.from_id), message=msg)):
                matched.append(int(mid))
        if not matched:
            raise CutoverError("verified test message disappeared")
        rpc.markseen_msgs(account, matched)


def full_legacy_archive(path: Path) -> bytes:
    """Unlike routine backups, include all currently present attachments."""
    previous = state_backup._skip_bulky
    try:
        state_backup._skip_bulky = lambda member: None if member.name.endswith(".tmp") else member
        data = state_backup.archive(path)
    finally:
        state_backup._skip_bulky = previous
    state_backup._verify_plaintext(data)
    return data


def _write_json(path: Path, value: Any) -> None:
    tmp = path.with_name(path.name + ".tmp-" + secrets.token_hex(8))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _prepare_runtime(path: Path, marker: dict[str, Any]) -> None:
    file = path / "michiflin-state.json"
    if not file.is_file():
        raise CutoverError("existing owner state is absent")
    stored = json.loads(file.read_text(encoding="utf-8"))
    if not isinstance(stored, dict) or stored.get("version") != 5:
        raise CutoverError("existing owner state has an unknown format")
    if (path / MARKER).exists() or (path / LEGACY_BINDINGS).exists():
        raise CutoverError("an earlier cutover marker already exists")
    saved: dict[str, Any] = {}
    for name in ("apps", "app_seen", "inbox"):
        value = stored.get(name, {})
        if not isinstance(value, dict):
            raise CutoverError("legacy bindings have an invalid format")
        saved[name] = value
        stored[name] = {}
    # Jobs and owner settings remain unchanged. Strict epoch prevents replay to
    # old chat/message identifiers; ambiguous deliveries are never resent.
    _write_json(path / LEGACY_BINDINGS, saved)
    _write_json(file, stored)
    _write_json(path / MARKER, marker)


class ReleaseStorage:
    def __init__(self) -> None:
        settings = state_backup.configuration()
        if not settings:
            raise CutoverError("required private backup configuration is missing")
        self.token, self.repo, self.password = settings
        state_backup._assert_private(self.token, self.repo)
        self.release = state_backup._ensure_release(self.token, self.repo)

    def sealed(self) -> bool:
        value = state_backup._get_ref(self.token, self.repo, SEAL)
        if not value:
            return False
        anchor = state_backup._get_ref(self.token, self.repo, state_backup.RELEASE_TAG)
        if not anchor or value != anchor:
            raise CutoverError("identity seal does not match the private backup anchor")
        return True

    def seal(self) -> None:
        anchor = state_backup._get_ref(self.token, self.repo, state_backup.RELEASE_TAG)
        if not anchor:
            raise CutoverError("private backup anchor is missing")
        existing = state_backup._get_ref(self.token, self.repo, SEAL)
        if existing and existing != anchor:
            raise CutoverError("identity seal points to a different anchor")
        if not existing:
            state_backup._make_ref(self.token, self.repo, SEAL, anchor)
        if state_backup._get_ref(self.token, self.repo, SEAL) != anchor:
            raise CutoverError("identity seal could not be verified")

    def download(self, asset_id: int) -> bytes:
        return state_backup._download_asset(self.token, self.repo, asset_id)

    def pin_legacy(self, raw: bytes) -> int:
        encrypted = seal_bytes(raw, self.password)
        if len(encrypted) > state_backup.MAX_BACKUP_BYTES:
            raise CutoverError("full owner recovery copy is too large; old identity retained")
        name = "michiflin-legacy-identity-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + secrets.token_hex(8) + ".bin"
        url = f"https://uploads.github.com/repos/{self.repo}/releases/{self.release['id']}/assets"
        response = requests.post(url, headers={**state_backup.headers(self.token), "Content-Type": "application/octet-stream"},
                                 params={"name": name}, data=encrypted, timeout=180, allow_redirects=False)
        response.raise_for_status()
        asset = response.json()
        asset_id = asset.get("id") if isinstance(asset, dict) else None
        if type(asset_id) is not int or asset_id <= 0 or asset.get("name") != name or asset.get("state") != "uploaded":
            raise CutoverError("legacy copy upload could not be verified")
        downloaded = self.download(asset_id)
        if not secrets.compare_digest(hashlib.sha256(downloaded).digest(), hashlib.sha256(encrypted).digest()):
            raise CutoverError("legacy copy changed during upload")
        recovered = state_backup.decrypt_backup(downloaded, self.password)
        if not secrets.compare_digest(hashlib.sha256(recovered).digest(), hashlib.sha256(raw).digest()):
            raise CutoverError("legacy copy recovery failed")
        state_backup._verify_plaintext(recovered)
        return asset_id

    def save(self, path: Path) -> None:
        previous = os.environ.get("CONFIG_DIR")
        os.environ["CONFIG_DIR"] = str(path)
        try:
            state_backup.save(rotate=False)
        finally:
            if previous is None:
                os.environ.pop("CONFIG_DIR", None)
            else:
                os.environ["CONFIG_DIR"] = previous

    def restore(self, path: Path) -> None:
        previous = os.environ.get("CONFIG_DIR")
        os.environ["CONFIG_DIR"] = str(path)
        try:
            state_backup.restore()
        finally:
            if previous is None:
                os.environ.pop("CONFIG_DIR", None)
            else:
                os.environ["CONFIG_DIR"] = previous


def transition(path: Path, candidate: Path, metadata: dict[str, Any], storage: Any, expected_owner: str, revision: str) -> str:
    if (not path.is_dir() or not (path / "accounts").is_dir() or (path / MARKER).exists()
            or storage.sealed() or not re.fullmatch(r"[0-9a-f]{40}", revision)):
        raise CutoverError("preflight failed or a previous transition is sealed")
    original = relay.identity(path)
    fresh = validate_candidate(candidate, metadata, expected_owner)
    if (original["fingerprint"] == fresh["fingerprint"] or original["primary"] == fresh["primary"]
            or original["transports"] & fresh["transports"]):
        raise CutoverError("candidate is not a separate identity")
    baseline_messages, baseline_blobs = relay.history(path), relay.blobs(path)
    plaintext = full_legacy_archive(path)
    with tempfile.TemporaryDirectory(prefix="identity-cutover-") as root:
        root = Path(root)
        restored_old = root / "old-recovered"
        state_backup._install_restored(plaintext, restored_old)
        relay.contains_baseline(restored_old, original, baseline_messages, baseline_blobs)
        legacy_id = storage.pin_legacy(plaintext)
        if type(legacy_id) is not int or legacy_id <= 0:
            raise CutoverError("legacy recovery asset is not verified")
        marker = {"version": 1, "confirmed": True, "owner_address": expected_owner.strip().casefold(),
                  "owner_fingerprint": identity_guard.normalize_fingerprint(metadata["owner_fingerprint"]),
                  "bot_fingerprint": identity_guard.normalize_fingerprint(metadata["bot_fingerprint"]),
                  "owner_chat": int(metadata["chat_id"]), "epoch": secrets.token_hex(24),
                  "legacy_asset_id": legacy_id, "revision": revision}
        backup = root / "original"
        shutil.copytree(path, backup, symlinks=False)
        saved_new = False
        try:
            shutil.rmtree(path / "accounts")
            shutil.copytree(candidate / "accounts", path / "accounts", symlinks=False)
            _prepare_runtime(path, marker)
            validate_candidate(path, metadata, expected_owner)
            mark_probe_seen(path, metadata, expected_owner)
            storage.save(path)
            saved_new = True
            recovered_new = root / "new-recovered"
            storage.restore(recovered_new)
            recovered_marker = json.loads((recovered_new / MARKER).read_text(encoding="utf-8"))
            if recovered_marker != marker:
                raise CutoverError("new identity backup marker differs")
            validate_candidate(recovered_new, metadata, expected_owner)
            storage.seal()
            return "verified"
        except Exception:
            # The complete pinned legacy copy is never removed. Restore locally;
            # if a new managed snapshot may have been published, write the old
            # state again without rotation so recovery does not prefer it.
            shutil.rmtree(path)
            shutil.copytree(backup, path, symlinks=False)
            if saved_new and not storage.sealed():
                try:
                    storage.save(path)
                except Exception:
                    pass  # an unavailable backup service must not erase the pinned copy
            raise


def activate(path: Path, storage: Any, expected_owner: str) -> dict[str, Any] | None:
    marker_file = path / MARKER
    sealed = storage.sealed()
    if not marker_file.is_file():
        if sealed:
            raise CutoverError("sealed identity cannot fall back to an older account")
        return None
    if not sealed:
        raise CutoverError("identity marker exists without the private seal")
    marker = json.loads(marker_file.read_text(encoding="utf-8"))
    if (not isinstance(marker, dict) or marker.get("version") != 1 or marker.get("confirmed") is not True
            or not re.fullmatch(r"[0-9a-f]{48}", str(marker.get("epoch") or ""))):
        raise CutoverError("identity marker is invalid")
    metadata = {"verified": True, "owner_address": marker.get("owner_address"),
                "owner_fingerprint": marker.get("owner_fingerprint"), "bot_fingerprint": marker.get("bot_fingerprint"),
                "chat_id": marker.get("owner_chat")}
    validate_candidate(path, metadata, expected_owner, require_probe=False,
                       allow_owner_rotation=config.EMAIL_KEY_RECOVERY)
    return marker


def main() -> None:
    path = Path(os.environ["CONFIG_DIR"])
    owner = os.environ.get("OWNER_ADDRESS", "")
    if os.environ.get("OWNER_ONLY") != "1":
        raise CutoverError("production owner-only mode is required")
    storage = ReleaseStorage()
    mode = sys.argv[1] if len(sys.argv) > 1 else "activate"
    if mode == "cutover":
        revision = os.environ.get("RUNNING_SHA", "")
        if revision != os.environ.get("IDENTITY_CUTOVER_REVISION", ""):
            raise CutoverError("cutover is not armed for this revision")
        existing = activate(path, storage, owner)
        if existing:
            if existing.get("revision") != revision:
                raise CutoverError("a different verified transition is already active")
            print("Verified identity transition already active; no account was changed.")
        else:
            asset = int(os.environ.get("CANDIDATE_BUNDLE_ASSET_ID", "0"))
            password = os.environ.get("CANDIDATE_BUNDLE_KEY", "")
            if asset <= 0 or not password:
                raise CutoverError("candidate bundle is unavailable")
            with tempfile.TemporaryDirectory(prefix="candidate-import-") as root:
                candidate = Path(root) / "candidate"
                meta = unpack_bundle(storage.download(asset), password, candidate)
                transition(path, candidate, meta, storage, owner, revision)
            print("Verified identity transition saved; original recovery copy retained.")
    elif mode != "activate":
        raise CutoverError("unknown operation")
    marker = activate(path, storage, owner)
    if marker:
        environment = Path(os.environ["GITHUB_ENV"])
        with environment.open("a", encoding="utf-8") as stream:
            stream.write("BOT_EPOCH=" + marker["epoch"] + "\n")
            stream.write("BOT_EPOCH_STRICT=1\nBOT_VERIFIED_CONTACTS_ONLY=1\n")
            stream.write("OWNER_PGP_FINGERPRINT=" + marker["owner_fingerprint"] + "\n")
        print("Verified fresh-identity controls enabled.")
    else:
        print("Existing identity retained; no fresh-identity transition is active.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Never expose addresses, keys, response bodies or invitation tokens.
        print("Identity transition stopped safely (" + type(exc).__name__ + ").", file=sys.stderr)
        raise SystemExit(1)
