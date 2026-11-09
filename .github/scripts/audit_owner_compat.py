"""Read-only owner app-version and backup-integrity audit.

Only GitHub's encrypted managed backup assets are downloaded. Archives and the
single database stay in memory; no bot starts and no conversation is queried,
printed, exported, or transferred. Only aggregate app metadata is reported.
"""
from __future__ import annotations

import ast
import base64
import hashlib
import hmac
import io
import json
import os
import re
import sqlite3
import sys
import tarfile
import time
from pathlib import Path, PurePosixPath

import requests
from cryptography.fernet import Fernet

MAGIC = b"MICHIFLIN-STATE-1\0"
PATTERN = re.compile(r"^michiflin-owner-clean-state-\d{8}T\d{6}Z-[0-9a-f]{16}\.bin$")
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
MAX_CIPHER = 90_000_000
MAX_DB = 80_000_000
MAX_STATE = 10_000_000


def public_build(root: Path, epoch: str) -> str:
    """Hash only the pinned public package; refuse any altered/missing asset."""
    expected = json.loads((root / ".github/scripts/audit-618c355-hashes.json").read_text("utf-8"))
    module = ast.parse((root / "michiflin/miniapp.py").read_text("utf-8"))
    assignments = [node for node in module.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "APP_FILES" for target in node.targets)]
    if len(assignments) != 1:
        raise ValueError("package_definition")
    files = ast.literal_eval(assignments[0].value)
    if not isinstance(files, tuple) or not isinstance(expected, dict) or len(files) != len(expected) or set(files) != set(expected):
        raise ValueError("package_manifest")
    digest = hashlib.sha256(("epoch:" + epoch).encode())
    for name in files:
        if not isinstance(name, str):
            raise ValueError("package_name")
        data = (root / "webxdc" / name).read_bytes()
        if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), expected[name]):
            raise ValueError("package_content")
        digest.update(name.encode() + data)
    return digest.hexdigest()[:10]


def inspect(ciphertext: bytes, password: str, owner: str, epoch: str, base_build: str) -> dict:
    """Authenticate and inspect selected metadata without reading message rows."""
    if not ciphertext.startswith(MAGIC) or not len(MAGIC) + 16 < len(ciphertext) <= MAX_CIPHER:
        raise ValueError("ciphertext")
    salt = ciphertext[len(MAGIC):len(MAGIC) + 16]
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 480_000, dklen=32)
    archive = Fernet(base64.urlsafe_b64encode(key)).decrypt(ciphertext[len(MAGIC) + 16:])
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tf:
        members = tf.getmembers()
        states = [m for m in members if m.isfile() and m.name == "michiflin/michiflin-state.json"]
        databases = [m for m in members if m.isfile() and PurePosixPath(m.name).name == "dc.db"]
        markers = [m for m in members if m.isfile() and PurePosixPath(m.name).name == ".owner-clean-origin-v1.json"]
        if len(states) != 1 or states[0].size > MAX_STATE or len(databases) != 1 or databases[0].size > MAX_DB or len(markers) != 1 or markers[0].size > 4096:
            raise ValueError("archive_layout")
        state_file, database_file, marker_file = (tf.extractfile(states[0]), tf.extractfile(databases[0]),
                                                  tf.extractfile(markers[0]))
        if state_file is None or database_file is None or marker_file is None:
            raise ValueError("archive_members")
        state = json.load(state_file)
        marker = json.load(marker_file)
        database = database_file.read()
    if not isinstance(state, dict) or not isinstance(state.get("apps"), dict) or not isinstance(marker, dict):
        raise ValueError("state_metadata")
    # The live workflow replaces its initial BOT_EPOCH with the clean marker's
    # epoch in clean_owner_identity.activate. Check the actual effective epoch,
    # and never print the marker, its fingerprints, address, or chat identifier.
    owner_fp = str(marker.get("owner_fingerprint") or "")
    bot_fp = str(marker.get("bot_fingerprint") or "")
    if not (marker.get("version") == 1 and marker.get("confirmed") is True and marker.get("fresh_account") is True
            and marker.get("epoch") == epoch and str(marker.get("owner_address") or "").strip().casefold() == owner
            and re.fullmatch(r"[A-F0-9]{40,64}", owner_fp) and re.fullmatch(r"[A-F0-9]{40,64}", bot_fp)
            and owner_fp != bot_fp and type(marker.get("owner_chat")) is int and marker["owner_chat"] > 9
            and not any(key in marker for key in ("source_asset_id", "legacy_asset_id", "imported_backup"))):
        raise ValueError("clean_identity_marker")
    connection = sqlite3.connect(":memory:")
    try:
        connection.deserialize(database)
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA temp_store = MEMORY")
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("database_integrity")
    finally:
        connection.close()
    apps = []
    for key, entry in state["apps"].items():
        if not isinstance(entry, dict) or not str(key).isdigit():
            continue
        if str(entry.get("addr") or "").strip().casefold() != owner:
            continue
        apps.append((int(key), entry))
    eligible = [(key, entry) for key, entry in apps if entry.get("epoch") == epoch]
    verified_chat = [(key, entry) for key, entry in eligible if entry.get("chat") == marker["owner_chat"]]
    matched = [(key, entry) for key, entry in verified_chat if entry.get("build") == base_build]
    latest = max(verified_chat, key=lambda item: item[0])[1] if verified_chat else None
    return {"total": len(apps), "current_epoch": len(eligible), "verified_chat": len(verified_chat),
            "matching": len(matched), "latest_matches": bool(latest and latest.get("build") == base_build)}


def audit() -> None:
    token = os.environ["STATE_TOKEN"]
    repo = os.environ["STATE_REPO"].strip().strip("/")
    password = os.environ["STATE_PASSPHRASE"]
    owner = os.environ["OWNER_ADDRESS"].strip().casefold()
    epoch = os.environ["CLEAN_IDENTITY_EPOCH"].strip()
    if not token or not REPO.fullmatch(repo) or not password or not owner or not re.fullmatch(r"[0-9a-f]{48}", epoch):
        raise ValueError("configuration")
    base = public_build(Path.cwd(), epoch)
    api = "https://api.github.com/repos/" + repo
    headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "Cache-Control": "no-cache"}
    response = requests.get(api + "/releases/tags/michiflin-state-releases-v1", headers=headers,
                            params={"_": time.time_ns()}, timeout=45)
    response.raise_for_status()
    assets = [a for a in response.json().get("assets", []) if PATTERN.fullmatch(str(a.get("name", "")))]
    assets.sort(key=lambda a: (a.get("created_at", ""), int(a.get("id") or 0)), reverse=True)
    if len(assets) < 2 or assets[0]["id"] == assets[1]["id"]:
        raise ValueError("insufficient_backups")
    reports = []
    for index, asset in enumerate(assets[:2]):
        size = int(asset["size"])
        if not 0 < size <= MAX_CIPHER:
            raise ValueError("backup_size")
        download_headers = {**headers, "Accept": "application/octet-stream"}
        downloaded = requests.get(api + "/releases/assets/" + str(int(asset["id"])), headers=download_headers,
                                  params={"_": time.time_ns()}, timeout=120)
        downloaded.raise_for_status()
        ciphertext = downloaded.content
        remote_digest = str(asset.get("digest") or "")
        if len(ciphertext) != size or not remote_digest.startswith("sha256:") or not hmac.compare_digest(hashlib.sha256(ciphertext).hexdigest(), remote_digest[7:]):
            raise ValueError("download_integrity")
        reports.append(inspect(ciphertext, password, owner, epoch, base))
        print(f"SAFE_BACKUP_{index + 1}_ID={int(asset['id'])}")
        print(f"SAFE_BACKUP_{index + 1}_CREATED={str(asset['created_at'])}")
        print(f"SAFE_BACKUP_{index + 1}_AUTH_AND_SQLITE=YES")
        del ciphertext, downloaded
    latest, previous = reports
    print("SAFE_OWNER_APPS_TOTAL=" + str(latest["total"]))
    print("SAFE_OWNER_APPS_CURRENT_EPOCH=" + str(latest["current_epoch"]))
    print("SAFE_OWNER_APPS_IN_VERIFIED_CHAT=" + str(latest["verified_chat"]))
    print("SAFE_OWNER_APPS_MATCH_618C355=" + str(latest["matching"]))
    print("SAFE_OWNER_LATEST_CURRENT_EPOCH_MATCH_618C355=" + ("YES" if latest["latest_matches"] else "NO"))
    print("SAFE_OWNER_PREVIOUS_BACKUP_LATEST_MATCH_618C355=" + ("YES" if previous["latest_matches"] else "NO"))
    print("SAFE_AUDIT_RESULT=SUCCESS")


if __name__ == "__main__":
    try:
        audit()
    except Exception as error:
        # Do not print exception text or traceback: an API/library exception
        # could include private paths, headers, response bodies or other data.
        print("SAFE_AUDIT_ERROR=" + type(error).__name__)
        sys.exit(1)
