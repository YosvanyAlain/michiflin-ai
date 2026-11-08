"""Compare only contact-invitation tokens in an encrypted clean-state snapshot.

The archive and SQLite database stay in memory. No message, contact, key, token,
address or invitation is printed or exported. This never opens a Delta account
or starts a second bot. The expected value is a SHA-256 of both random tokens.
"""
from __future__ import annotations
import base64
import hashlib
import io
import os
import re
import sqlite3
import tarfile

import requests
from cryptography.fernet import Fernet

MAGIC = b"MICHIFLIN-STATE-1\0"


def pair_present(db: sqlite3.Connection, expected: str) -> bool:
    first = db.execute("SELECT token FROM tokens WHERE namespc=100 AND COALESCE(foreign_key,'')='' LIMIT 101").fetchall()
    second = db.execute("SELECT token FROM tokens WHERE namespc=110 AND COALESCE(foreign_key,'')='' LIMIT 101").fetchall()
    print("SAFE_CONTACT_INVITATION_COUNT_" + str(len(first)))
    print("SAFE_CONTACT_AUTH_COUNT_" + str(len(second)))
    if len(first) > 100 or len(second) > 100:
        raise ValueError("too_many_tokens")
    for (a,) in first:
        for (b,) in second:
            if not isinstance(a, str) or not isinstance(b, str):
                raise ValueError("invalid_token_type")
            if hashlib.sha256((a + ":" + b).encode("utf-8")).hexdigest() == expected:
                return True
    return False


def main() -> None:
    digest = os.environ.get("EXPECTED_PAIR_DIGEST", "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("invalid_digest")
    token = os.environ["GITHUB_TOKEN"]
    repo = os.environ["STATE_BACKUP_REPO"]
    password = os.environ["STATE_BACKUP_PASSPHRASE"]
    if repo != "YosvanyAlain/michiflin-private-state":
        raise ValueError("unexpected_repository")
    headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    url = f"https://api.github.com/repos/{repo}/releases/tags/michiflin-state-releases-v1"
    res = requests.get(url, headers=headers, timeout=30); res.raise_for_status()
    release = res.json()
    assets = [item for item in release.get("assets", []) if re.fullmatch(r"michiflin-owner-clean-state-\d{8}T\d{6}Z-[0-9a-f]{16}\.bin", str(item.get("name", "")))]
    if not assets:
        raise ValueError("no_clean_snapshot")
    latest = max(assets, key=lambda item: item["name"])
    asset = requests.get(f"https://api.github.com/repos/{repo}/releases/assets/{int(latest['id'])}",
                         headers={**headers, "Accept": "application/octet-stream"}, timeout=120)
    asset.raise_for_status(); encrypted = asset.content
    if not encrypted.startswith(MAGIC) or not 0 < len(encrypted) <= 100_000_000:
        raise ValueError("invalid_ciphertext")
    salt = encrypted[len(MAGIC):len(MAGIC) + 16]
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 480_000, dklen=32)
    archive = Fernet(base64.urlsafe_b64encode(key)).decrypt(encrypted[len(MAGIC) + 16:])
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        databases = [member for member in tar.getmembers() if member.isfile() and member.name.endswith("/dc.db")]
        if len(databases) != 1 or databases[0].size > 50_000_000:
            raise ValueError("invalid_database_count")
        handle = tar.extractfile(databases[0])
        if handle is None:
            raise ValueError("missing_database")
        db_bytes = handle.read()
    with sqlite3.connect(":memory:") as db:
        db.deserialize(db_bytes)
        exists = pair_present(db, digest)
    print("SAFE_INVITATION_PAIR_PRESENT" if exists else "SAFE_INVITATION_PAIR_NOT_PRESENT")
    print("SAFE_ONLY_TOKEN_TABLE_QUERIED")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("SAFE_AUDIT_FAILED_" + type(exc).__name__)
        raise SystemExit(1) from None
