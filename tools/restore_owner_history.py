#!/usr/bin/env python3
"""Verify or recover the key-free owner-history archive; never reactivate an old bot.

Run with a previously downloaded archive and STATE_BACKUP_PASSPHRASE, or with
private-backup credentials and CONFIG_DIR pointing to the restored new identity.
The recovered folder is sensitive plaintext: keep it private and delete it
when no longer needed.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
import tarfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import state_backup  # noqa: E402
from tools import export_owner_history_once as history  # noqa: E402


def download_verified(config_dir: Path, token: str, repo: str, password: str) -> tuple[bytes, dict]:
    state_backup._assert_private(token, repo)
    marker = json.loads((config_dir / history.MARKER).read_text(encoding="utf-8"))
    asset_id = marker.get("asset_id")
    if marker.get("version") != 1 or marker.get("verified") is not True or type(asset_id) is not int or asset_id <= 0:
        raise history.ExportError("owner archive marker invalid")
    release = state_backup._release(token, repo)
    if not release:
        raise history.ExportError("private backup release unavailable")
    assets = state_backup._assets(token, repo, release["id"])
    if not any(x.get("id") == asset_id and str(x.get("name") or "").startswith(history.PREFIX) for x in assets):
        raise history.ExportError("owner archive is not in the private release")
    encrypted = state_backup._download_asset(token, repo, asset_id)
    if not secrets.compare_digest(hashlib.sha256(encrypted).hexdigest(), str(marker.get("sha256") or "")):
        raise history.ExportError("owner archive checksum mismatch")
    raw = state_backup.decrypt_backup(encrypted, password)
    manifest = history.verify(raw, expected_counts=marker.get("counts"))
    return raw, manifest


def recover(raw: bytes, destination: Path) -> None:
    """Extract only a previously verified archive into a new private directory."""
    history.verify(raw)
    if destination.exists() or destination.is_symlink():
        raise history.ExportError("destination already exists")
    destination.mkdir(mode=0o700, parents=False)
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
            for item in tar.getmembers():
                target = destination / item.name
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if target.is_symlink() or destination.resolve() not in target.resolve().parents:
                    raise history.ExportError("unsafe recovery target")
                source = tar.extractfile(item)
                if source is None:
                    raise history.ExportError("missing archive member")
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as out:
                    shutil.copyfileobj(source, out)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify or recover an encrypted owner-history archive without recreating the old account.")
    parser.add_argument("archive", nargs="?", help="downloaded encrypted archive; omit to retrieve the verified private release asset")
    parser.add_argument("--check", action="store_true", help="verify without extracting")
    parser.add_argument("--destino", "--destination", dest="destination", help="new private directory for recovered plaintext")
    args = parser.parse_args(argv)
    if args.check == bool(args.destination):
        parser.error("choose either --check or --destino")
    try:
        if args.archive:
            password = os.getenv("STATE_BACKUP_PASSPHRASE", "")
            if not password:
                raise history.ExportError("private backup passphrase unavailable")
            source = Path(args.archive).expanduser()
            if not source.is_file() or source.is_symlink() or source.stat().st_size > state_backup.MAX_BACKUP_BYTES:
                raise history.ExportError("encrypted archive unavailable or too large")
            raw = state_backup.decrypt_backup(source.read_bytes(), password)
            history.verify(raw)
        else:
            config = state_backup.configuration()
            if not config:
                raise history.ExportError("private backup credentials unavailable")
            raw, _manifest = download_verified(state_backup.config_dir(), *config)
        if args.destination:
            target = Path(args.destination).expanduser().absolute()
            if target == state_backup.config_dir().resolve() or state_backup.config_dir().resolve() in target.parents:
                raise history.ExportError("recovery folder cannot be inside the bot state")
            recover(raw, target)
            print("Owner history recovered to a new private plaintext directory; protect it and remove it after use.")
        else:
            print("Encrypted owner history archive independently recovered and verified.")
        return 0
    except Exception as exc:
        print("Owner history recovery stopped safely (" + type(exc).__name__ + ").", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
