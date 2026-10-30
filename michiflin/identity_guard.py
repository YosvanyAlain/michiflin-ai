"""Opt-in contact/key guard for a separately verified fresh-identity cutover.

The legacy address allow-list is not a cryptographic identity.  In strict mode
we require a directly verified key-contact in an encrypted one-to-one chat and
pin the owner's primary OpenPGP fingerprint.  No fingerprint is extracted from
human-readable encryption-info text or a contact's display name.
"""
from __future__ import annotations

import base64
import binascii
from collections import OrderedDict
import copy
import hashlib
import hmac
import os
import re
import subprocess
import tempfile
import threading
from typing import Any

from . import config

MAX_VCARD = 256_000
MAX_KEY = 200_000
MAX_CACHE = 128
_cache: OrderedDict[str, str] = OrderedDict()
_cache_lock = threading.Lock()


class BindingCheckpointError(RuntimeError):
    """A permitted key change could not be durably saved; retry the message."""


def normalize_fingerprint(value: str) -> str:
    """Accept a fingerprint with optional whitespace, never arbitrary prose."""
    value = re.sub(r"\s+", "", str(value or "")).upper()
    return value if re.fullmatch(r"(?:[0-9A-F]{40}|[0-9A-F]{64})", value) else ""


def _card_key(card: Any) -> bytes:
    """Extract one KEY property, rejecting malformed or multiple-key cards."""
    if not isinstance(card, str) or len(card.encode("utf-8")) > MAX_VCARD:
        return b""
    lines = re.sub(r"\r?\n[ \t]", "", card).splitlines()
    if sum(line.upper() == "BEGIN:VCARD" for line in lines) != 1 or sum(line.upper() == "END:VCARD" for line in lines) != 1:
        return b""
    keys = [line.partition(":")[2] for line in lines if line.partition(":")[0].upper() == "KEY" and ":" in line]
    if len(keys) != 1:
        return b""
    value = keys[0]
    if value.lower().startswith("data:"):
        head, separator, value = value.partition(",")
        # Delta Chat's vCard escapes the comma separating the data URI.
        if head.endswith("\\"):
            head = head[:-1]
        if not separator or not head.lower().endswith(";base64"):
            return b""
    try:
        result = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        result = value.encode("utf-8") if value.startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----") else b""
    return result if 0 < len(result) <= MAX_KEY else b""


def _fingerprint_from_key(key: bytes) -> str:
    """Use GnuPG's machine-readable output and accept exactly one primary key."""
    if not key:
        return ""
    digest = hashlib.sha256(key).hexdigest()
    with _cache_lock:
        if digest in _cache:
            _cache.move_to_end(digest)
            return _cache[digest]
    try:
        with tempfile.TemporaryDirectory(prefix="michiflin-public-key-") as home:
            result = subprocess.run(
                ["gpg", "--no-options", "--batch", "--no-autostart", "--no-auto-key-retrieve",
                 "--homedir", home, "--with-colons", "--show-keys"],
                input=key, capture_output=True, timeout=8, check=False,
            )
        if result.returncode != 0 or len(result.stdout) > MAX_VCARD:
            return ""
        primary = []
        waiting_for_primary = False
        for line in result.stdout.decode("utf-8", errors="strict").splitlines():
            fields = line.split(":")
            kind = fields[0]
            if kind in {"sec", "ssb"}:
                return ""  # public material only
            if kind == "pub":
                primary.append("")
                waiting_for_primary = True
            elif kind in {"sub", "uid", "uat"}:
                waiting_for_primary = False
            elif kind == "fpr" and waiting_for_primary and len(fields) > 9:
                primary[-1] = normalize_fingerprint(fields[9])
                waiting_for_primary = False
        if len(primary) != 1 or not primary[0]:
            return ""
        fingerprint = primary[0]
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        return ""
    with _cache_lock:
        _cache[digest] = fingerprint
        _cache.move_to_end(digest)
        while len(_cache) > MAX_CACHE:
            _cache.popitem(last=False)
    return fingerprint


def contact_fingerprint(rpc: Any, account: int, contact_id: int) -> str:
    try:
        return _fingerprint_from_key(_card_key(rpc.make_vcard(account, [contact_id])))
    except Exception:
        return ""  # transient failure or unsupported vCard: fail closed


def _key_digest(fingerprint: str) -> str:
    value = normalize_fingerprint(fingerprint)
    return hashlib.sha256(("email-key-v1:" + value).encode("ascii")).hexdigest() if value else ""


def current_email_chat(address: str) -> int:
    """Return the most recently bound chat without revealing a stored key."""
    if not config.EMAIL_KEY_RECOVERY:
        return 0
    from . import state
    with state.lock:
        record = state.data.get("email_key_bindings", {}).get(str(address or "").strip().casefold(), {})
        try:
            return max(0, int(record.get("chat") or 0)) if isinstance(record, dict) else 0
        except (TypeError, ValueError):
            return 0


def _email_binding(address: str, chat_id: int, fingerprint: str, learn: bool, *, inspect_only: bool = False) -> bool:
    """Bind an allowed email to its latest encrypted key; retire older keys.

    This deliberately trusts an address rather than proving who owns it. It is
    off by default. A forged allowed sender can therefore replace a binding.
    Only an inbound encrypted message or explicit secure-join event may learn;
    background delivery is restricted to the last bound chat and key.
    """
    from . import state
    digest = _key_digest(fingerprint)
    if not digest or int(chat_id) <= 9:
        return False
    with state.lock:
        bindings = state.data.get("email_key_bindings")
        if bindings is None:
            bindings = {} if inspect_only else state.data.setdefault("email_key_bindings", {})
        if not isinstance(bindings, dict):
            return False
        previous = copy.deepcopy(bindings.get(address))
        if previous is not None and not isinstance(previous, dict):
            return False
        active = str((previous or {}).get("key") or "")
        retired = (previous or {}).get("retired", [])
        if not isinstance(retired, list) or len(retired) > 256 or any(not re.fullmatch(r"[0-9a-f]{64}", str(x)) for x in retired):
            return False
        if active and not re.fullmatch(r"[0-9a-f]{64}", active):
            return False
        # Preserve the old owner's pin only as a retired digest if the first
        # accepted message already uses a different key; old backups stay intact.
        old_pin = _key_digest(config.OWNER_PGP_FINGERPRINT) if address == config.OWNER_ADDRESS.casefold() else ""
        if not active and old_pin and old_pin != digest:
            retired = list(dict.fromkeys([*retired, old_pin]))
        if digest in retired:
            return False
        old_chat = (previous or {}).get("chat")
        if active == digest and old_chat == int(chat_id):
            return True
        if not learn:
            return False
        if active and active != digest:
            retired = list(dict.fromkeys([*retired, active]))
        if len(retired) > 256:
            return False
        # Candidate inspection must not bind or retire anything before the
        # newest message is selected. The caller must perform a real check later.
        if inspect_only:
            return True
        bindings[address] = {"key": digest, "chat": int(chat_id), "retired": retired}
        try:
            state.save()
            # Production explicitly requires backups: verify this change in a
            # non-rotating encrypted checkpoint before admitting the new key.
            if os.getenv("STATE_BACKUP_REQUIRED", "0").strip() == "1":
                import state_backup
                state_backup.save(rotate=False)
        except Exception as exc:
            if previous is None:
                bindings.pop(address, None)
            else:
                bindings[address] = previous
            try:
                state.save()
            except Exception:
                pass
            raise BindingCheckpointError("key change could not be saved; retry later") from exc
        return True


def authorized_chat(rpc: Any, account: int, chat_id: int, address: str,
                    *, contact_id: int | None = None, message: Any = None,
                    allow_rotation: bool = False, _inspect_recovery: bool = False) -> bool:
    """Verify a chat, defaulting to a pinned owner and directly verified contacts.

    Explicit email-key recovery is weaker and applies only to an address with a
    current access grant. Single-chat and end-to-end encryption requirements
    still apply. Old keys cannot be reused after a new one has been bound.
    Private inspection identifies candidates; it never authorizes a delivery.
    """
    if _inspect_recovery and not (config.EMAIL_KEY_RECOVERY and message is not None):
        return False
    if not (config.BOT_VERIFIED_CONTACTS_ONLY or config.EMAIL_KEY_RECOVERY or config.INVITE_AUTO_ACCESS):
        return True
    try:
        info = rpc.get_basic_chat_info(account, chat_id)
        kind = getattr(info.chat_type, "value", info.chat_type)
        if str(kind) != "Single" or info.is_encrypted is not True or info.is_self_talk is not False:
            return False
        members = rpc.get_chat_contacts(account, chat_id)
        if len(members) != 1 or (contact_id is not None and int(members[0]) != int(contact_id)):
            return False
        contact = rpc.get_contact(account, int(members[0]))
        actual = str(contact.address or "").strip().casefold()
        expected = str(address or "").strip().casefold()
        if (not expected or actual != expected or contact.is_key_contact is not True or contact.e2ee_avail is not True):
            return False
        if message is not None and (int(message.chat_id) != int(chat_id) or int(message.from_id) != int(members[0])
                                    or message.show_padlock is not True):
            return False
        if config.INVITE_AUTO_ACCESS and not config.is_owner_admin(expected):
            if _inspect_recovery:
                return False
            from . import community
            fingerprint = contact_fingerprint(rpc, account, int(members[0]))
            return bool(info.is_contact_request is False and contact.is_verified is True and contact.verifier_id == 1
                        and community.matches(expected, chat_id, fingerprint))
        if config.EMAIL_KEY_RECOVERY:
            from . import admin
            if admin.is_allowed(expected):
                learn = bool(message is not None or allow_rotation)
                if info.is_contact_request is not False and not learn:
                    return False
                return _email_binding(expected, int(chat_id), contact_fingerprint(rpc, account, int(members[0])), learn,
                                      inspect_only=_inspect_recovery)
        if _inspect_recovery:
            return False
        if not config.BOT_VERIFIED_CONTACTS_ONLY:
            return not config.INVITE_AUTO_ACCESS
        if info.is_contact_request is not False or contact.is_verified is not True or contact.verifier_id != 1:
            return False
        if expected == config.OWNER_ADDRESS.strip().casefold():
            expected_fp = normalize_fingerprint(config.OWNER_PGP_FINGERPRINT)
            actual_fp = contact_fingerprint(rpc, account, int(members[0]))
            return bool(expected_fp and actual_fp and hmac.compare_digest(expected_fp, actual_fp))
        return True  # guests still require a separate explicit address grant
    except BindingCheckpointError:
        raise  # the durable inbox must retain this message for a later retry
    except Exception:
        return False


def authorized_entry(rpc: Any, account: int, entry: dict[str, Any]) -> bool:
    """Apply the same chat/key check to a bound WebXDC instance."""
    if not (config.BOT_VERIFIED_CONTACTS_ONLY or config.EMAIL_KEY_RECOVERY or config.INVITE_AUTO_ACCESS):
        return True
    try:
        return authorized_chat(rpc, account, int(entry["chat"]), str(entry["addr"]))
    except Exception:
        return False
