#!/usr/bin/env python3
"""One-shot, opt-in delivery of the source ZIP and manual to the verified owner.

Run only in the existing, serialized bot workflow after account restoration and
before the bot opens the database. The recipient must exactly match OWNER_ADDRESS
and have an existing verified, encrypted individual Delta Chat. Durable delivery
intents are encrypted in the normal state backup *before* handing files to core;
uncertain outcomes are held, not automatically re-sent. This script never prints
addresses, secrets, message contents, or provider errors.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MAX_ATTACHMENT = 8 * 1024 * 1024
LEDGER_NAME = "owner-document-delivery-v1.json"


class DeliveryError(Exception):
    """Non-sensitive reason; do not include recipient or secret values."""


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def source_zip(repo: Path, revision: str, destination: Path) -> str:
    """Package tracked source from one commit; exclude only large guide media."""
    revision = revision.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise DeliveryError("invalid_revision")
    result = subprocess.run(["git", "archive", "--format=tar", revision], cwd=repo,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=45, check=False)
    if result.returncode:
        raise DeliveryError("archive_failed")
    included: list[tuple[str, bytes, int]] = []
    try:
        with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
            for member in archive:
                if member.isdir():
                    continue
                path = PurePosixPath(member.name)
                if (not member.isfile() or path.is_absolute() or ".." in path.parts
                        or not member.name or member.size > 30_000_000):
                    raise DeliveryError("unsafe_archive")
                if member.name.startswith("docs/media/") or member.name == "docs/Manual_Michiflin.pdf":
                    continue  # screenshots/video are non-code; the manual is sent separately
                handle = archive.extractfile(member)
                if handle is None:
                    raise DeliveryError("unsafe_archive")
                included.append((member.name, handle.read(), member.mode))
    except (tarfile.TarError, OSError):
        raise DeliveryError("archive_failed") from None
    if not included or not any(name == "bot.py" for name, _, _ in included):
        raise DeliveryError("archive_failed")
    manifest = [f"{hashlib.sha256(data).hexdigest()}  {name}" for name, data, _ in sorted(included)]
    note = ("Michiflin AI - codigo fuente rastreado\n"
            f"Commit: {revision}\n"
            "Incluye codigo, pruebas, configuracion de ejemplo y documentacion textual.\n"
            "Se omiten capturas/videos de docs/media; el manual PDF se envia aparte.\n"
            "No incluye estado, cuentas, claves, secretos, historiales ni .git.\n"
            "Comprueba las huellas listadas en SHA256SUMS.txt.\n")
    included.extend([("LEEME_ENTREGA.txt", note.encode(), 0o644),
                     ("SHA256SUMS.txt", ("\n".join(manifest) + "\n").encode(), 0o644)])
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as output:
        for name, data, mode in sorted(included):
            info = zipfile.ZipInfo("Michiflin-AI/" + name, (2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (mode & 0o777) << 16
            output.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return _digest(destination)


def _ledger(root: Path) -> dict:
    path = root / LEDGER_NAME
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise DeliveryError("invalid_ledger")
    if not path.exists():
        return {"version": 1, "deliveries": {}}
    try:
        if path.stat().st_size > 256_000:
            raise ValueError("size")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("deliveries"), dict):
            raise ValueError("format")
        return data
    except (ValueError, OSError, TypeError):
        raise DeliveryError("invalid_ledger") from None


def _checkpoint(root: Path, ledger: dict, backup: Callable[[], None]) -> None:
    """The normal encrypted backup must contain the intent before any send."""
    path = root / LEDGER_NAME
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise DeliveryError("invalid_ledger")
    fd, temporary = tempfile.mkstemp(prefix=".owner-document-", dir=root)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(ledger, handle, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        backup()  # fail closed; no document is handed off on a failed checkpoint
    except Exception:
        raise DeliveryError("backup_failed") from None
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def verified_owner_chat(rpc, account: int, owner: str, expected: str) -> int:
    owner, expected = owner.strip().casefold(), expected.strip().casefold()
    if not owner or not expected or owner != expected:
        raise DeliveryError("recipient_mismatch")
    contact_id = rpc.lookup_contact_id_by_addr(account, owner)
    if not contact_id:
        raise DeliveryError("owner_chat_missing")
    contact = rpc.get_contact(account, contact_id)
    if str(getattr(contact, "address", "")).strip().casefold() != owner:
        raise DeliveryError("recipient_mismatch")
    if not getattr(contact, "is_verified", False) or not getattr(contact, "e2ee_avail", False):
        raise DeliveryError("owner_chat_not_verified")
    chat_id = rpc.get_chat_id_by_contact_id(account, contact_id)
    if not chat_id:
        raise DeliveryError("owner_chat_missing")
    info = rpc.get_basic_chat_info(account, chat_id)
    kind = getattr(getattr(info, "chat_type", ""), "value", getattr(info, "chat_type", ""))
    if str(kind) != "Single" or not getattr(info, "is_encrypted", False):
        raise DeliveryError("owner_chat_not_verified")
    return int(chat_id)


def _wait_for_delivery(rpc, account: int, msg_id: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = rpc.get_message(account, msg_id)
        state = int(getattr(getattr(message, "state", 0), "value", getattr(message, "state", 0)) or 0)
        if state in {26, 28}:
            if not getattr(message, "show_padlock", False):
                raise DeliveryError("encryption_not_confirmed")
            return  # accepted by the relay; this does not prove the recipient opened it
        if state == 24:
            raise DeliveryError("delivery_failed")
        time.sleep(2)
    raise DeliveryError("delivery_uncertain")


def deliver(rpc, account: int, root: Path, owner: str, expected: str, revision: str,
            package: Path, manual: Path, backup: Callable[[], None], timeout: float = 300) -> None:
    from deltachat2 import MessageData

    chat_id = verified_owner_chat(rpc, account, owner, expected)
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise DeliveryError("invalid_revision")
    for path in (package, manual):
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_ATTACHMENT:
            raise DeliveryError("attachment_invalid")
    if not manual.read_bytes().startswith(b"%PDF-") or not zipfile.is_zipfile(package):
        raise DeliveryError("attachment_invalid")
    bundle_hash, manual_hash = _digest(package), _digest(manual)
    delivery_id = hashlib.sha256((revision + "\0" + manual_hash + "\0" + owner.strip().casefold()).encode()).hexdigest()
    ledger = _ledger(root)
    record = ledger["deliveries"].setdefault(delivery_id, {"revision": revision, "documents": {}})
    if not isinstance(record, dict) or not isinstance(record.get("documents"), dict):
        raise DeliveryError("invalid_ledger")
    docs = (("source", package, "Michiflin_codigo_fuente.zip", bundle_hash),
            ("manual", manual, "Manual_Michiflin.pdf", manual_hash))
    for key, path, name, digest in docs:
        item = record["documents"].get(key)
        if item is not None and (not isinstance(item, dict) or item.get("sha256") != digest):
            raise DeliveryError("invalid_ledger")
        if item and item.get("state") == "delivered":
            continue
        if item and item.get("state") == "pending" and isinstance(item.get("msg_id"), int):
            msg_id = item["msg_id"]
        elif item:
            raise DeliveryError("delivery_uncertain")  # never repeat an uncertain handoff
        else:
            item = {"state": "intent", "sha256": digest}
            record["documents"][key] = item
            _checkpoint(root, ledger, backup)
            try:
                data = MessageData(text="Entrega solicitada: " + name, file=str(path), filename=name)
                msg_id = int(rpc.send_msg(account, chat_id, data))
            except Exception:
                raise DeliveryError("delivery_uncertain") from None
            if msg_id <= 0:
                raise DeliveryError("delivery_uncertain")
            item.update(state="pending", msg_id=msg_id)
            _checkpoint(root, ledger, backup)
        _wait_for_delivery(rpc, account, msg_id, timeout)
        item["state"] = "delivered"
        _checkpoint(root, ledger, backup)


def main() -> int:
    if os.environ.get("CONFIRM_OWNER_DELIVERY") != "1":
        print("Document delivery was not requested.")
        return 0
    try:
        from deltachat2 import Rpc
        from deltachat2.transport import IOTransport
        from michiflin import config, state
        import state_backup

        owner = config.OWNER_ADDRESS
        expected = os.environ.get("EXPECTED_DELIVERY_ADDRESS", "")
        root = Path(config.CONFIG_DIR)
        revision = os.environ.get("RUNNING_SHA", "").strip().lower()
        manual = ROOT / "docs" / "Manual_Michiflin.pdf"
        if not root.is_dir() or not (root / "accounts").is_dir():
            raise DeliveryError("account_unavailable")
        state.load()
        if config.MAINTENANCE_MODE or state.maintenance_active() or state.get_kill_switch(owner):
            raise DeliveryError("paused")
        if not state_backup.configuration():
            raise DeliveryError("backup_unavailable")
        with tempfile.TemporaryDirectory(prefix="michiflin-owner-delivery-") as temporary:
            package = Path(temporary) / "Michiflin_codigo_fuente.zip"
            source_zip(ROOT, revision, package)
            transport = IOTransport(accounts_dir=str(root / "accounts"), stderr=subprocess.DEVNULL)
            transport.start()
            try:
                rpc = Rpc(transport)
                accounts = rpc.get_all_account_ids()
                if len(accounts) != 1:
                    raise DeliveryError("account_unavailable")
                account = int(accounts[0])
                verified_owner_chat(rpc, account, owner, expected)
                rpc.start_io(account)
                deliver(rpc, account, root, owner, expected, revision, package, manual,
                        lambda: state_backup.save(rotate=False))
            finally:
                transport.close()
        print("Both encrypted documents were accepted by the relay; opening them is not confirmed.")
        return 0
    except DeliveryError as exc:
        print("Document delivery stopped safely:", str(exc))
        return 1
    except Exception:
        print("Document delivery stopped safely: unexpected_failure")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
