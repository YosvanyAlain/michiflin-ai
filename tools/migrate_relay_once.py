#!/usr/bin/env python3
"""One-time, recoverable bot relay migration, authorized for this deployment.

The account, history and old transport remain. Addresses, QR codes, keys,
messages, and exception contents are never logged. The network check uses an
independently generated synthetic contact which is removed afterwards.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import state_backup  # noqa: E402
from deltachat2 import IOTransport, MessageData, Rpc  # noqa: E402

MARKER = ".relay-migration-5-1.json"
RELAYS = ("nine.testrun.org", "chtml.ca")


class MigrationError(RuntimeError):
    pass


@contextmanager
def account_rpc(accounts: Path) -> Iterator[Rpc]:
    transport = IOTransport(accounts_dir=str(accounts), stderr=subprocess.DEVNULL)
    transport.start()
    try:
        yield Rpc(transport)
    finally:
        transport.close()
        process = getattr(transport, "process", None)
        if process is not None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            if process.stdout is not None:
                process.stdout.close()


def only_account(rpc: Rpc) -> int:
    ids = rpc.get_all_account_ids()
    if len(ids) != 1:
        raise MigrationError("expected one existing account")
    return ids[0]


def fingerprint(card: str) -> str:
    """Inspect only the public vCard key; fingerprints, addresses and keys stay private."""
    lines = re.sub(r"\r?\n[ \t]", "", card).splitlines()
    key = next((line.split(":", 1)[1] for line in lines if line.upper().startswith("KEY:") and ":" in line), "")
    if key.startswith("data:") and "," in key:
        key = key.split(",", 1)[1]
    if not key:
        raise MigrationError("missing public identity key")
    try:
        raw = base64.b64decode(key, validate=True)
    except ValueError as exc:
        raise MigrationError("invalid public identity key") from exc
    with tempfile.TemporaryDirectory(prefix="relay-key-") as folder:
        os.chmod(folder, 0o700)
        result = subprocess.run(["gpg", "--homedir", folder, "--batch", "--with-colons", "--show-keys"],
                                input=raw, capture_output=True, timeout=20, check=False)
        lines = result.stdout.decode("utf-8", errors="replace").splitlines()
        values = [line.split(":")[9] for line in lines if line.startswith("fpr:") and len(line.split(":")) > 9]
        if result.returncode or not values or not re.fullmatch(r"[A-Fa-f0-9]{40,64}", values[0]):
            raise MigrationError("public fingerprint unavailable")
        return values[0].upper()


def identity(path: Path) -> dict[str, Any]:
    with account_rpc(path / "accounts") as rpc:
        account = only_account(rpc)
        primary = str(rpc.get_config(account, "configured_addr") or "").casefold()
        transports = {str(t.addr).casefold() for t in rpc.list_transports(account) if t.addr}
        card = rpc.make_vcard(account, [1])
        ids = set(rpc.get_chatlist_entries(account, None, None, None))
    if not primary or primary not in transports:
        raise MigrationError("configured address is not a transport")
    return {"primary": primary, "transports": transports, "fingerprint": fingerprint(card), "chats": ids}


def history(path: Path) -> dict[int, str]:
    """Digest immutable message content locally; never log or export the rows."""
    databases = list((path / "accounts").rglob("dc.db"))
    if len(databases) != 1:
        raise MigrationError("expected one account database")
    with sqlite3.connect(f"file:{databases[0]}?mode=ro", uri=True, timeout=30) as db:
        rows = db.execute("SELECT id, rfc724_mid, chat_id, from_id, to_id, timestamp, type, txt, txt_raw FROM msgs")
        return {int(row[0]): hashlib.sha256(json.dumps(row, ensure_ascii=False, default=str).encode()).hexdigest() for row in rows}


def blobs(path: Path) -> dict[str, str]:
    """Require every currently present Delta attachment blob in the recovered copy."""
    result = {}
    for item in (path / "accounts").rglob("*"):
        if item.is_file() and any(part.endswith("-blobs") for part in item.relative_to(path).parts):
            digest = hashlib.sha256()
            with item.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            result[str(item.relative_to(path))] = digest.hexdigest()
    return result


def contains_baseline(path: Path, original_identity: dict[str, Any], messages: dict[int, str], files: dict[str, str]) -> dict[str, Any]:
    current = identity(path)
    if current["fingerprint"] != original_identity["fingerprint"] or not original_identity["chats"].issubset(current["chats"]):
        raise MigrationError("identity or chats differ")
    current_messages, current_files = history(path), blobs(path)
    if any(current_messages.get(key) != value for key, value in messages.items()):
        raise MigrationError("original history differs")
    if any(current_files.get(key) != value for key, value in files.items()):
        raise MigrationError("original attachment is missing")
    return current


class Backup:
    """Use the existing encrypted private backup and never rotate older copies here."""
    @staticmethod
    def save(path: Path) -> None:
        previous = os.environ.get("CONFIG_DIR")
        os.environ["CONFIG_DIR"] = str(path)
        try:
            state_backup.save(rotate=False)
        finally:
            if previous is None:
                os.environ.pop("CONFIG_DIR", None)
            else:
                os.environ["CONFIG_DIR"] = previous

    @staticmethod
    def restore(path: Path) -> None:
        previous = os.environ.get("CONFIG_DIR")
        os.environ["CONFIG_DIR"] = str(path)
        try:
            state_backup.restore()
        finally:
            if previous is None:
                os.environ.pop("CONFIG_DIR", None)
            else:
                os.environ["CONFIG_DIR"] = previous


def wait_for(function: Any, seconds: int = 150) -> Any:
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        value = function()
        if value:
            return value
        time.sleep(1)
    raise MigrationError("encrypted probe timed out")


def delivered(rpc: Rpc, account: int, message: int) -> bool:
    item = rpc.get_message(account, message)
    status = int(getattr(item.state, "value", item.state))
    if status == 24:
        raise MigrationError("encrypted probe failed")
    return status in (26, 28) and bool(item.show_padlock)


def received(rpc: Rpc, account: int, label: str) -> Any:
    for msg_id in rpc.search_messages(account, label, None):
        item = rpc.get_message(account, msg_id)
        if item.text == label and item.show_padlock:
            return item
    return None


def migrate_network(path: Path, before: dict[str, Any], probe_dir: Path) -> dict[str, Any]:
    if len(before["transports"]) != 1:
        raise MigrationError("multiple existing transports require manual review")
    old = before["primary"]
    target = next((host for host in RELAYS if not old.endswith("@" + host)), "")
    if not target:
        raise MigrationError("no different relay available")
    with account_rpc(path / "accounts") as bot_rpc, account_rpc(probe_dir) as probe_rpc:
        bot = only_account(bot_rpc)
        probe = probe_rpc.add_account()
        probe_rpc.set_config(probe, "displayname", "Relay test")
        probe_rpc.add_transport_from_qr(probe, "DCACCOUNT:" + RELAYS[0])
        probe_addr = str(probe_rpc.get_config(probe, "configured_addr") or "").casefold()
        if not probe_addr or bot_rpc.lookup_contact_id_by_addr(bot, probe_addr):
            raise MigrationError("probe contact is not new")
        bot_rpc.start_io(bot)
        probe_rpc.start_io(probe)
        bot_rpc.add_transport_from_qr(bot, "DCACCOUNT:" + target)
        addresses = {str(t.addr).casefold() for t in bot_rpc.list_transports(bot) if t.addr}
        new = addresses - before["transports"]
        if len(new) != 1 or not before["transports"].issubset(addresses):
            raise MigrationError("new transport verification failed")
        selected = next(iter(new))
        if not selected.endswith("@" + target):
            raise MigrationError("unexpected new relay")
        bot_rpc.set_config(bot, "configured_addr", selected)
        if str(bot_rpc.get_config(bot, "configured_addr") or "").casefold() != selected:
            raise MigrationError("new transport was not selected")
        card = bot_rpc.make_vcard(bot, [1])
        if fingerprint(card) != before["fingerprint"]:
            raise MigrationError("public key fingerprint changed")
        probe_contacts = probe_rpc.import_vcard_contents(probe, card)
        bot_on_probe = next((cid for cid in probe_contacts if probe_rpc.get_contact(probe, cid).address.casefold() == selected), None)
        if not bot_on_probe or not probe_rpc.get_contact(probe, bot_on_probe).e2ee_avail:
            raise MigrationError("new public identity is unavailable")
        bot_contacts = bot_rpc.import_vcard_contents(bot, probe_rpc.make_vcard(probe, [1]))
        probe_on_bot = next((cid for cid in bot_contacts if bot_rpc.get_contact(bot, cid).address.casefold() == probe_addr), None)
        if not probe_on_bot or not bot_rpc.get_contact(bot, probe_on_bot).e2ee_avail:
            raise MigrationError("probe public identity is unavailable")
        probe_chat = probe_rpc.create_chat_by_contact_id(probe, bot_on_probe)
        bot_chat = bot_rpc.create_chat_by_contact_id(bot, probe_on_bot)
        inbound = "relay-in-" + secrets.token_hex(12)
        msg = probe_rpc.send_msg(probe, probe_chat, MessageData(text=inbound))
        wait_for(lambda: delivered(probe_rpc, probe, msg))
        wait_for(lambda: received(bot_rpc, bot, inbound))
        outbound = "relay-out-" + secrets.token_hex(12)
        msg = bot_rpc.send_msg(bot, bot_chat, MessageData(text=outbound))
        wait_for(lambda: delivered(bot_rpc, bot, msg))
        wait_for(lambda: received(probe_rpc, probe, outbound))
        # The only deleted records are the fresh synthetic probe's contact/chat.
        if (bot_rpc.get_contact(bot, probe_on_bot).address.casefold() != probe_addr
                or bot_rpc.get_chat_id_by_contact_id(bot, probe_on_bot) != bot_chat
                or bot_chat in before["chats"]):
            raise MigrationError("probe cleanup could not be isolated")
        bot_rpc.delete_chat(bot, bot_chat)
        bot_rpc.delete_contact(bot, probe_on_bot)
        bot_rpc.stop_io(bot)
        probe_rpc.stop_io(probe)
    return {"old": old, "new": selected}


def write_marker(path: Path, status: str, **values: Any) -> None:
    file = path / MARKER
    file.write_text(json.dumps({"status": status, **values}, sort_keys=True), encoding="utf-8")
    file.chmod(0o600)


def migrate_once(path: Path, backup: Any) -> str:
    marker = path / MARKER
    if marker.exists():
        try:
            status = json.loads(marker.read_text(encoding="utf-8"))["status"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise MigrationError("migration marker invalid") from exc
        if status == "verified":
            return status
        if status in {"needs_attention", "started", "pending"}:
            return "needs_attention"  # no automatic registration after an interruption
        raise MigrationError("migration marker invalid")
    if not path.is_dir() or not (path / "accounts").is_dir():
        raise MigrationError("existing account required")
    with tempfile.TemporaryDirectory(prefix="michiflin-relay-migration-") as temp:
        tmp = Path(temp)
        original = tmp / "original"
        shutil.copytree(path, original)
        # No registration is attempted until a complete fresh encrypted backup
        # can be independently restored. An unavailable backup skips migration.
        try:
            before, messages, files = identity(path), history(path), blobs(path)
            backup.save(path)
            backup.restore(tmp / "before-recovered")
            recovered = contains_baseline(tmp / "before-recovered", before, messages, files)
            if recovered["primary"] != before["primary"] or recovered["transports"] != before["transports"]:
                raise MigrationError("pre-migration backup did not match")
        except Exception:
            return "preflight_failed"  # original account not changed; serving may continue
        try:
            # Persist the attempted state before registration. If the runner is
            # interrupted, the next one will not register another relay.
            write_marker(path, "started")
            backup.save(path)
            backup.restore(tmp / "started-recovered")
            started = json.loads((tmp / "started-recovered" / MARKER).read_text(encoding="utf-8"))
            if started.get("status") != "started":
                raise MigrationError("attempt checkpoint unavailable")
            transfer = migrate_network(path, before, tmp / "probe")
            after = contains_baseline(path, before, messages, files)
            if after["primary"] != transfer["new"] or not before["transports"].issubset(after["transports"]):
                raise MigrationError("migrated account did not survive restart")
            write_marker(path, "pending", old=transfer["old"], new=transfer["new"])
            backup.save(path)
            backup.restore(tmp / "after-recovered")
            restored = contains_baseline(tmp / "after-recovered", before, messages, files)
            saved = json.loads((tmp / "after-recovered" / MARKER).read_text(encoding="utf-8"))
            if (restored["primary"] != transfer["new"] or not before["transports"].issubset(restored["transports"])
                    or saved.get("status") != "pending"):
                raise MigrationError("migrated backup did not match")
            write_marker(path, "verified", old=transfer["old"], new=transfer["new"])
            backup.save(path)
            backup.restore(tmp / "verified-recovered")
            verified = contains_baseline(tmp / "verified-recovered", before, messages, files)
            final = json.loads((tmp / "verified-recovered" / MARKER).read_text(encoding="utf-8"))
            if verified["primary"] != transfer["new"] or final.get("status") != "verified":
                raise MigrationError("final backup did not match")
            return "verified"
        except Exception:
            # Restore the original locally and as the latest recovery snapshot.
            # A failed attempt is recorded so later sessions never keep registering relays.
            shutil.rmtree(path)
            shutil.copytree(original, path)
            write_marker(path, "needs_attention")
            backup.save(path)
            backup.restore(tmp / "rollback-recovered")
            recovered = contains_baseline(tmp / "rollback-recovered", before, messages, files)
            saved = json.loads((tmp / "rollback-recovered" / MARKER).read_text(encoding="utf-8"))
            if (recovered["primary"] != before["primary"] or recovered["transports"] != before["transports"]
                    or saved.get("status") != "needs_attention"):
                raise MigrationError("rollback backup could not be verified")
            return "needs_attention"


def main() -> int:
    if (os.getenv("MICHIFLIN_RELAY_MIGRATE_ONCE") != "1"
            or os.getenv("GITHUB_REPOSITORY") != "YosvanyAlain/michiflin-ai"
            or os.getenv("GITHUB_REF") != "refs/heads/main"
            or os.getenv("STATE_BACKUP_REQUIRED") != "1"):
        print("Relay migration authorization/preconditions absent; no action taken.")
        return 1
    workspace, path = Path(os.environ["GITHUB_WORKSPACE"]).resolve(), Path(os.environ["CONFIG_DIR"]).resolve()
    if path != workspace / ".michiflin-state":
        print("Relay migration refused: unexpected state path.")
        return 1
    try:
        outcome = migrate_once(path, Backup())
    except Exception as exc:
        print("Relay migration stopped safely; backup or verification requires attention:", type(exc).__name__)
        return 1
    if output := os.getenv("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as file:
            file.write(f"verified={'true' if outcome == 'verified' else 'false'}\n")
    if outcome == "verified":
        print("Relay migration verified; old transport and original history retained.")
    elif outcome == "preflight_failed":
        print("::warning::Relay migration skipped: complete recovery could not be verified; the original account was not changed.")
    else:
        print("::warning::Relay migration requires attention; no additional automatic registration was attempted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
