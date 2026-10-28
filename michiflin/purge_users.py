"""One-time, owner-authorized removal of attributable non-owner history.

Run only on an offline restored copy, before the bot starts. Never print content
or addresses. Ambiguous/shared records and group chats are retained to protect
the owner. The live backup is not replaced unless this process succeeds.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

from .profile import _account_rpc

MARKER = "nonowner_purge_20261028"
PEOPLE_FIELDS = ("people", "prefs", "notes", "link_queue", "nauta_strict", "kill_switch")
EMAIL = re.compile(r"^[^\s@]{1,128}@[^\s@]{1,255}$")


def _addr(value: Any) -> str:
    return str(value or "").strip().casefold()


def _other(value: Any, owner: str) -> bool:
    addr = _addr(value)
    return bool(EMAIL.fullmatch(addr) and addr != owner)


def _owned_or_unknown(value: Any, owner: str) -> bool:
    return not _other(value, owner)


def _database(rpc: Any, account: int) -> Path:
    blob = Path(rpc.get_blob_dir(account))
    if not blob.name.endswith("-blobs"):
        raise ValueError("unexpected account database")
    database = blob.with_name(blob.name.removesuffix("-blobs"))
    if not database.is_file():
        raise ValueError("account database unavailable")
    return database


def _db(rpc: Any, account: int) -> sqlite3.Connection:
    return sqlite3.connect(_database(rpc, account).resolve().as_uri() + "?mode=ro", uri=True)


def _chat_ids(rpc: Any, account: int) -> set[int]:
    # The public chat list excludes blocked/hidden chats. Read only their ids
    # from the local core database; all classification and deletion use RPC.
    with closing(_db(rpc, account)) as conn:
        ids = {int(row[0]) for row in conn.execute("SELECT id FROM chats WHERE id > 9")}
    # Delta Chat's documented 0x01 means archived-only; 0 includes unarchived.
    ids.update(int(cid) for flags in (0, 1) for cid in rpc.get_chatlist_entries(account, flags, None, None) if int(cid) > 9)
    return ids


def _classify(rpc: Any, account: int, owner: str, bot: str) -> tuple[set[int], set[int], set[int]]:
    other, retained, protected_contacts = set(), set(), set()
    for chat_id in _chat_ids(rpc, account):
        info = rpc.get_basic_chat_info(account, chat_id)
        ids = [int(i) for i in rpc.get_chat_contacts(account, chat_id)]
        if info.is_self_talk or info.is_device_chat or str(info.chat_type) != "Single" or len(ids) != 1:
            retained.add(chat_id)
            protected_contacts.update(ids)
            continue
        contact = rpc.get_contact(account, ids[0])
        address = _addr(contact.address)
        if address and address != bot and _other(address, owner):
            other.add(chat_id)
        else:
            retained.add(chat_id)
            protected_contacts.update(ids)
    return other, retained, protected_contacts


def _message_ids(rpc: Any, account: int, chats: set[int]) -> set[int]:
    if not chats:
        return set()
    with closing(_db(rpc, account)) as conn:
        marks = ",".join("?" for _ in chats)
        # Includes drafts and hidden messages that the visible list omits.
        return {int(row[0]) for row in conn.execute(f"SELECT id FROM msgs WHERE id > 9 AND chat_id IN ({marks})", tuple(chats))}


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _protected_digest(rpc: Any, account: int, chats: set[int]) -> str:
    """Hash retained DB rows and profile, even when old attachments are missing.

    Core's get_message can fail when an attachment was omitted from an earlier
    backup. SQL is read-only; deleting or updating the account is always RPC.
    """
    msg_ids = _message_ids(rpc, account, chats)
    contact_ids = {int(cid) for chat in chats for cid in rpc.get_chat_contacts(account, chat)}
    blob = Path(rpc.get_blob_dir(account)).resolve()
    files = _candidate_files(rpc, account, chats, blob)
    image_paths = [rpc.get_config(account, "selfavatar")]
    image_paths += [getattr(rpc.get_contact(account, cid), "profile_image", None) for cid in contact_ids]
    image_paths += [getattr(rpc.get_basic_chat_info(account, cid), "profile_image", None) for cid in chats]
    for raw in image_paths:
        if raw:
            path = Path(raw)
            if not path.is_symlink() and path.is_file() and blob in path.resolve().parents:
                files.add(path.resolve())
    file_hashes = []
    for path in sorted(files):
        h = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                h.update(chunk)
        file_hashes.append([path.name, h.hexdigest()])
    material: dict[str, Any] = {"account": [rpc.get_config(account, key) for key in ("addr", "selfstatus", "displayname")],
                                "files": file_hashes}
    with closing(_db(rpc, account)) as conn:
        tables = [str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            columns = [str(row[1]) for row in conn.execute(f"PRAGMA table_info({_quote(table)})")]
            conditions: list[str] = []
            values: list[int] = []
            candidates = [("id", chats)] if table == "chats" else [("id", contact_ids)] if table == "contacts" else []
            candidates += [("chat_id", chats), ("msg_id", msg_ids)]
            for col, ids in candidates:
                if col in columns and ids:
                    conditions.append(f"{_quote(col)} IN ({','.join('?' for _ in ids)})")
                    values.extend(sorted(ids))
            if not conditions:
                continue
            rows = conn.execute(f"SELECT * FROM {_quote(table)} WHERE " + " OR ".join(conditions), values)
            encoded = []
            for row in rows:
                safe = [hashlib.sha256(item).hexdigest() if isinstance(item, bytes) else item for item in row]
                encoded.append(json.dumps(safe, ensure_ascii=False, separators=(",", ":")))
            material[table] = sorted(encoded)
    raw = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _candidate_files(rpc: Any, account: int, chats: set[int], blob: Path) -> set[Path]:
    """Only exact file references in non-owner message parameters are candidates."""
    if not chats:
        return set()
    files: set[Path] = set()
    with closing(_db(rpc, account)) as conn:
        marks = ",".join("?" for _ in chats)
        rows = conn.execute(f"SELECT param FROM msgs WHERE chat_id IN ({marks})", tuple(chats))
        for (param,) in rows:
            for line in str(param or "").splitlines():
                prefix = "f=$BLOBDIR/"
                if not line.startswith(prefix):
                    continue
                name = line[len(prefix):]
                if not name or Path(name).name != name:
                    continue
                path = blob / name
                if not path.is_symlink() and path.is_file():
                    files.add(path.resolve())
    return files


def _blob_file(raw: Any, blob: Path) -> Path | None:
    if not raw:
        return None
    path = Path(str(raw))
    try:
        resolved = path.resolve(strict=True)
        if not path.is_symlink() and resolved.is_file() and blob.resolve(strict=True) in resolved.parents:
            return resolved
    except OSError:
        pass
    return None


def _unshared_files(rpc: Any, account: int, candidates: set[Path], chats: set[int], msgs: set[int],
                    contacts: set[int], clean: dict[str, Any]) -> set[Path]:
    """Keep a candidate if any retained/ambiguous DB row or state mentions its name."""
    possible = {path for path in candidates if path.name not in json.dumps(clean, ensure_ascii=False)}
    if not possible:
        return set()
    with closing(_db(rpc, account)) as conn:
        tables = [str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            columns = [str(row[1]) for row in conn.execute(f"PRAGMA table_info({_quote(table)})")]
            conditions, values = [], []
            old_rows = [("chat_id", chats), ("msg_id", msgs)]
            if table == "contacts":
                old_rows.append(("id", contacts))
            elif table == "chats":
                old_rows.append(("id", chats))
            for col, ids in old_rows:
                if col in columns and ids:
                    conditions.append(f"({_quote(col)} IS NULL OR {_quote(col)} NOT IN ({','.join('?' for _ in ids)}))")
                    values.extend(sorted(ids))
            where = " WHERE " + " AND ".join(conditions) if conditions else ""
            for row in conn.execute(f"SELECT * FROM {_quote(table)}" + where, values):
                strings = [v if isinstance(v, str) else v.decode("utf-8", "ignore") for v in row if isinstance(v, (str, bytes))]
                possible = {path for path in possible if not any(path.name in value for value in strings)}
                if not possible:
                    return set()
    return possible


def _owner_projection(data: dict[str, Any], owner: str, other_chats: set[int], other_msgs: set[int]) -> tuple[dict[str, Any], dict[str, int]]:
    """Drop only records whose sender or chat can be attributed to someone else."""
    clean = copy.deepcopy(data)
    counts: dict[str, int] = {}
    for key in PEOPLE_FIELDS:
        value = clean.get(key, {})
        if not isinstance(value, dict):
            raise ValueError("invalid personal state")
        removed = [name for name in value if _other(name, owner)]
        for name in removed:
            del value[name]
        counts[key] = len(removed)

    def record_is_other(item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        addr = _addr(item.get("addr"))
        if EMAIL.fullmatch(addr):
            return addr != owner
        try:
            return int(item.get("chat") or 0) in other_chats
        except (ValueError, TypeError):
            return False

    removed_apps = set()
    for key in ("jobs", "apps"):
        value = clean.get(key, {})
        if not isinstance(value, dict):
            raise ValueError("invalid job or app state")
        ids = [name for name, item in value.items() if record_is_other(item)]
        for name in ids:
            del value[name]
        if key == "apps":
            removed_apps = set(ids)
        counts[key] = len(ids)
    seen = clean.get("app_seen", {})
    if not isinstance(seen, dict):
        raise ValueError("invalid app replay state")
    ids = [key for key, item in seen.items()
           if isinstance(item, list) and len(item) > 2 and
           (str(item[2]) in removed_apps or (str(item[2]).isdigit() and int(item[2]) in other_msgs))]
    for key in ids:
        del seen[key]
    counts["app_seen"] = len(ids)
    inbox = clean.get("inbox", {})
    if not isinstance(inbox, dict):
        raise ValueError("invalid inbox")
    removed = [key for key in inbox if str(key).isdigit() and int(key) in other_msgs]
    for key in removed:
        del inbox[key]
    counts["inbox"] = len(removed)
    history = clean.get("sent", [])
    if not isinstance(history, list):
        raise ValueError("invalid sent history")
    kept = [item for item in history if not isinstance(item, (list, tuple)) or len(item) < 3 or _owned_or_unknown(item[2], owner)]
    counts["sent"] = len(history) - len(kept)
    clean["sent"] = kept
    access = clean.get("access", {})
    if not isinstance(access, dict):
        raise ValueError("invalid access state")
    for field in ("allowed", "blocked"):
        entries = access.get(field, [])
        if not isinstance(entries, list):
            raise ValueError("invalid access list")
        kept = [value for value in entries if _owned_or_unknown(value, owner)]
        counts["access_" + field] = len(entries) - len(kept)
        access[field] = kept
    attempts = access.get("attempts", {})
    if not isinstance(attempts, dict):
        raise ValueError("invalid access attempts")
    removed = [key for key in attempts if _other(key, owner)]
    for key in removed:
        del attempts[key]
    counts["attempts"] = len(removed)
    flags = clean.setdefault("flags", {})
    if not isinstance(flags, dict):
        raise ValueError("invalid flags")
    limited = flags.get("limited")
    if limited is not None:
        if not isinstance(limited, list):
            raise ValueError("invalid limited list")
        flags["limited"] = [addr for addr in limited if _owned_or_unknown(addr, owner)]
        counts["limited"] = len(limited) - len(flags["limited"])
    flags[MARKER] = True
    return clean, counts


def _state_file(root: Path) -> Path:
    path = root / "michiflin-state.json"
    if not path.is_file() or path.is_symlink():
        raise ValueError("missing state")
    return path


def _load(root: Path) -> dict[str, Any]:
    value = json.loads(_state_file(root).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("version") != 5:
        raise ValueError("unsupported state")
    return value


def _hash_state(data: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def apply(root: Path, owner: str, proof_path: Path) -> dict[str, int]:
    owner = _addr(owner)
    if not EMAIL.fullmatch(owner):
        raise ValueError("missing owner")
    data = _load(root)
    if data.get("flags", {}).get(MARKER):
        raise ValueError("already applied")
    with _account_rpc(str(root / "accounts")) as rpc:
        accounts = rpc.get_all_account_ids()
        if len(accounts) != 1:
            raise ValueError("expected one account")
        acc = accounts[0]
        bot = _addr(rpc.get_config(acc, "addr"))
        if not EMAIL.fullmatch(bot) or bot == owner:
            raise ValueError("invalid account identity")
        other, retained, protected_contacts = _classify(rpc, acc, owner, bot)
        other_msgs = _message_ids(rpc, acc, other)
        before = _protected_digest(rpc, acc, retained)
        clean, counts = _owner_projection(data, owner, other, other_msgs)
        blob = Path(rpc.get_blob_dir(acc))
        candidates = _candidate_files(rpc, acc, other, blob)
        contacts = {c.id: c for c in (rpc.get_contacts(acc, 0, None) + rpc.get_contacts(acc, 4, None) + rpc.get_blocked_contacts(acc))}
        removable = {contact.id for contact in contacts.values() if int(contact.id) > 9 and contact.id not in protected_contacts
                     and _other(contact.address, owner) and _addr(contact.address) != bot}
        for contact in contacts.values():
            if contact.id in removable and (path := _blob_file(getattr(contact, "profile_image", None), blob)):
                candidates.add(path)
        for cid in other:
            if path := _blob_file(getattr(rpc.get_basic_chat_info(acc, cid), "profile_image", None), blob):
                candidates.add(path)
        unshared = _unshared_files(rpc, acc, candidates, other, other_msgs, removable, clean)
        for cid in sorted(other):
            rpc.delete_chat(acc, cid)
        for cid in sorted(removable):
            rpc.delete_contact(acc, cid)
        for path in unshared:
            path.unlink(missing_ok=True)
        after_other, after_retained, _ = _classify(rpc, acc, owner, bot)
        if after_other or retained != after_retained or before != _protected_digest(rpc, acc, retained):
            raise RuntimeError("owner preservation check failed")
        counts.update(delta_chats=len(other), delta_messages=len(other_msgs), delta_contacts=len(removable),
                      unshared_files=len(unshared))
        proof = {"state_sha256": _hash_state(clean), "protected_sha256": before, "retained_chats": sorted(retained)}
    path = _state_file(root)
    tmp = path.with_suffix(".purge.tmp")
    tmp.write_text(json.dumps(clean, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)
    proof_path.write_text(json.dumps(proof, separators=(",", ":")), encoding="utf-8")
    return counts


def verify(root: Path, owner: str, proof_path: Path) -> None:
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    data = _load(root)
    if _hash_state(data) != proof["state_sha256"] or not data.get("flags", {}).get(MARKER):
        raise RuntimeError("sanitized runtime state mismatch")
    with _account_rpc(str(root / "accounts")) as rpc:
        accounts = rpc.get_all_account_ids()
        if len(accounts) != 1:
            raise RuntimeError("account mismatch")
        acc = accounts[0]
        other, retained, _ = _classify(rpc, acc, _addr(owner), _addr(rpc.get_config(acc, "addr")))
        if other or sorted(retained) != proof["retained_chats"] or _protected_digest(rpc, acc, retained) != proof["protected_sha256"]:
            raise RuntimeError("sanitized Delta account mismatch")


def main() -> int:
    if len(sys.argv) != 4 or sys.argv[1] not in {"apply", "verify"}:
        print("Usage: python -m michiflin.purge_users apply|verify CONFIG_DIR PROOF", file=sys.stderr)
        return 2
    try:
        root, owner, proof = Path(sys.argv[2]), os.environ.get("OWNER_ADDRESS", ""), Path(sys.argv[3])
        if sys.argv[1] == "apply":
            print("Sanitized aggregate counts:", json.dumps(apply(root, owner, proof), sort_keys=True))
        else:
            verify(root, owner, proof)
            print("Sanitized state and retained account verified")
        return 0
    except Exception as exc:
        print(f"Sanitization not completed ({type(exc).__name__}); original remote backup remains untouched", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
