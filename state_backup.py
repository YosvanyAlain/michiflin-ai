#!/usr/bin/env python3
"""Encrypted Michiflin AI state backup to one private GitHub repository.

The Delta Chat state contains private encryption keys. This program never uploads
that state in plaintext: it creates a fresh salt, derives a Fernet key from
STATE_BACKUP_PASSPHRASE, encrypts the tar.gz archive, then uses the GitHub API.
It preserves encrypted runtime state between sessions.

Required environment variables:
  GITHUB_TOKEN              fine-grained token, Contents: Read and write only
  STATE_BACKUP_REPO         owner/private-repository
  STATE_BACKUP_PASSPHRASE   unique long secret, different from GitHub password
Optional environment variables:
  CONFIG_DIR, STATE_BACKUP_PATH, STATE_BACKUP_BRANCH

Usage: python state_backup.py save | restore
"""
from __future__ import annotations

import base64
import hashlib
import io
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

import requests
from cryptography.fernet import Fernet, InvalidToken

MAGIC = b"MICHIFLIN-STATE-1\0"
SALT_BYTES = 16
ITERATIONS = 480_000
# The Git data API accepts blobs up to 100 MB; the contents API only about 1 MB.
MAX_BACKUP_BYTES = 90_000_000
MAX_CONTENTS_BACKUP_BYTES = 950_000
# Attachment copies above this size are not needed to resume the account.
MAX_BLOB_BYTES = 200_000


def configuration() -> tuple[str, str, str] | None:
    """Read backup credentials without printing any sensitive value."""
    token = os.getenv("GITHUB_TOKEN", "").strip()
    repo = os.getenv("STATE_BACKUP_REPO", "").strip().strip("/")
    passphrase = os.getenv("STATE_BACKUP_PASSPHRASE", "")
    if not token or not repo or not passphrase:
        return None
    if repo.count("/") != 1:
        raise ValueError("STATE_BACKUP_REPO must use owner/repository format")
    return token, repo, passphrase


def config_dir() -> Path:
    return Path(os.getenv("CONFIG_DIR", os.path.expanduser("~/.config/michiflin")))


def api_url(repo: str) -> str:
    path = quote(os.getenv("STATE_BACKUP_PATH", "michiflin-state.bin").lstrip("/"), safe="/")
    return f"https://api.github.com/repos/{repo}/contents/{path}"


def headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def fernet_from(passphrase: str, salt: bytes) -> Fernet:
    key = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, ITERATIONS, dklen=32)
    return Fernet(base64.urlsafe_b64encode(key))


def _is_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(16) == b"SQLite format 3\0"
    except OSError:
        return False


def archive(source: Path) -> bytes:
    """Build an in-memory gzip archive; live databases are copied as consistent, compacted snapshots."""
    if not source.is_dir():
        raise FileNotFoundError(f"State directory does not exist: {source}")
    snapshots: dict[str, Path] = {}
    scratch = Path(tempfile.mkdtemp(prefix=".snapshot-"))
    try:
        for path in source.rglob("*"):
            if path.is_file() and not path.name.endswith(("-wal", "-shm", "-journal")) and _is_sqlite(path):
                target = scratch / f"{len(snapshots)}.db"
                try:
                    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)) as connection:
                        connection.execute("VACUUM INTO ?", (str(target),))
                    snapshots[str(path.relative_to(source))] = target
                except sqlite3.Error as exc:
                    print(f"[backup] snapshot skipped for {path.name}: {exc}")

        def pick(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
            member = _skip_bulky(member)
            if member is None:
                return None
            relative = member.name.partition("/")[2]
            if member.isfile() and relative.endswith(("-wal", "-shm")) and relative.rsplit("-", 1)[0] in snapshots:
                return None
            return member

        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w:gz") as tar:
            tar.add(str(source), arcname="michiflin", recursive=False)
            for path in sorted(source.rglob("*")):
                relative = str(path.relative_to(source))
                name = f"michiflin/{relative}"
                if relative in snapshots:
                    info = tar.gettarinfo(str(snapshots[relative]), arcname=name)
                    info.mode = path.stat().st_mode & 0o777
                    with snapshots[relative].open("rb") as handle:
                        tar.addfile(info, handle)
                    continue
                info = tar.gettarinfo(str(path), arcname=name)
                if pick(info) is None:
                    continue
                if info.isfile():
                    with path.open("rb") as handle:
                        tar.addfile(info, handle)
                else:
                    tar.addfile(info)
        return data.getvalue()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _skip_bulky(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
    """Leave out sent/received attachment copies and temporary files; keys and databases stay."""
    if member.isfile() and "-blobs/" in member.name and member.size > MAX_BLOB_BYTES:
        return None
    if member.name.endswith(".tmp"):
        return None
    return member


def safely_extract(data: bytes, destination: Path) -> None:
    """Extract a trusted encrypted archive while still rejecting unsafe paths."""
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = tar.getmembers()
        root = destination.resolve()
        for member in members:
            target = (destination / member.name).resolve()
            if target != root and root not in target.parents:
                raise ValueError("backup contains an unsafe path")
            if member.issym() or member.islnk():
                raise ValueError("backup contains an unsupported link")
        tar.extractall(destination, members=members, filter="data")


def _git_api(repo: str) -> str:
    return f"https://api.github.com/repos/{repo}/git"


def _save_git(token: str, repo: str, encrypted: bytes, branch: str) -> None:
    """Append a backup commit without force-updating or discarding older backups."""
    base = _git_api(repo)
    path = os.getenv("STATE_BACKUP_PATH", "michiflin-state.bin").lstrip("/")
    ref_url = f"{base}/ref/heads/{quote(branch, safe='')}"
    previous = requests.get(ref_url, headers=headers(token), timeout=60)
    parent, base_tree = "", ""
    if previous.status_code == 200:
        parent = str(previous.json()["object"]["sha"])
        commit = requests.get(f"{base}/commits/{parent}", headers=headers(token), timeout=60)
        commit.raise_for_status()
        base_tree = str(commit.json()["tree"]["sha"])
    elif previous.status_code != 404:
        previous.raise_for_status()
    blob = requests.post(f"{base}/blobs", headers=headers(token), timeout=180,
                         json={"content": base64.b64encode(encrypted).decode("ascii"), "encoding": "base64"})
    blob.raise_for_status()
    tree_data: dict[str, object] = {"tree": [{"path": path, "mode": "100644", "type": "blob", "sha": blob.json()["sha"]}]}
    if base_tree:
        tree_data["base_tree"] = base_tree
    tree = requests.post(f"{base}/trees", headers=headers(token), timeout=60, json=tree_data)
    tree.raise_for_status()
    commit = requests.post(f"{base}/commits", headers=headers(token), timeout=60,
                           json={"message": "Encrypted Michiflin AI state", "tree": tree.json()["sha"],
                                 "parents": [parent] if parent else []})
    commit.raise_for_status()
    sha = commit.json()["sha"]
    if parent:
        ref = requests.patch(f"{base}/refs/heads/{quote(branch, safe='')}", headers=headers(token), timeout=60,
                             json={"sha": sha, "force": False})
    else:
        ref = requests.post(f"{base}/refs", headers=headers(token), timeout=60, json={"ref": f"refs/heads/{branch}", "sha": sha})
    ref.raise_for_status()


def _save_contents(token: str, repo: str, encrypted: bytes, branch: str) -> None:
    if len(encrypted) > MAX_CONTENTS_BACKUP_BYTES:
        raise ValueError("encrypted state is too large for the contents API; the previous backup was kept")
    url = api_url(repo)
    response = requests.get(url, headers=headers(token), params={"ref": branch}, timeout=30)
    previous_sha = ""
    if response.status_code == 200:
        previous_sha = str(response.json().get("sha", ""))
    elif response.status_code != 404:
        response.raise_for_status()
    payload: dict[str, str] = {
        "message": "Update encrypted Michiflin AI state",
        "content": base64.b64encode(encrypted).decode("ascii"),
        "branch": branch,
    }
    if previous_sha:
        payload["sha"] = previous_sha
    response = requests.put(url, headers=headers(token), json=payload, timeout=60)
    response.raise_for_status()


def save() -> None:
    """Encrypt the current state and store it as one file in the private repository."""
    settings = configuration()
    if not settings:
        print("[backup] disabled: backup secrets are missing")
        return
    token, repo, passphrase = settings
    plaintext = archive(config_dir())
    salt = os.urandom(SALT_BYTES)
    encrypted = MAGIC + salt + fernet_from(passphrase, salt).encrypt(plaintext)
    if len(encrypted) > MAX_BACKUP_BYTES:
        raise ValueError("encrypted state is too large; the previous backup was kept")
    branch = os.getenv("STATE_BACKUP_BRANCH", "main")
    try:
        _save_git(token, repo, encrypted, branch)
    except requests.RequestException as exc:
        print(f"[backup] git data API failed ({exc.__class__.__name__}); trying the contents API")
        _save_contents(token, repo, encrypted, branch)
    print(f"[backup] encrypted state saved to {repo} ({len(encrypted) // 1024} KB)")


def restore() -> None:
    """Restore state only when no local state exists yet."""
    settings = configuration()
    required = os.getenv("STATE_BACKUP_REQUIRED", "0").strip() == "1"
    if not settings:
        message = "[backup] restoration disabled: backup secrets are missing"
        if required:
            raise RuntimeError(message + "; refusing to initialize a fresh account")
        print(message)
        return
    token, repo, passphrase = settings
    target = config_dir()
    if target.exists():
        print("[backup] local state already exists; it will not be overwritten")
        return
    response = requests.get(
        api_url(repo),
        headers={**headers(token), "Accept": "application/vnd.github.raw"},
        params={"ref": os.getenv("STATE_BACKUP_BRANCH", "main")},
        timeout=180,
    )
    if response.status_code == 404:
        if required and os.getenv("STATE_BACKUP_ALLOW_EMPTY", "0").strip() != "1":
            raise FileNotFoundError("required remote state backup is missing; refusing to initialize a fresh account")
        print("[backup] no remote state exists; fresh initialization is allowed only for first setup")
        return
    response.raise_for_status()
    blob = response.content
    if not blob.startswith(MAGIC) and response.headers.get("Content-Type", "").startswith("application/json"):
        blob = base64.b64decode(str(response.json().get("content", "")).replace("\n", ""))
    if not blob.startswith(MAGIC) or len(blob) <= len(MAGIC) + SALT_BYTES:
        raise ValueError("remote file is not a valid Michiflin AI backup")
    salt_at = len(MAGIC)
    salt = blob[salt_at : salt_at + SALT_BYTES]
    encrypted = blob[salt_at + SALT_BYTES :]
    try:
        plaintext = fernet_from(passphrase, salt).decrypt(encrypted)
    except InvalidToken as exc:
        raise ValueError("could not decrypt backup; check STATE_BACKUP_PASSPHRASE") from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".restore-", dir=target.parent))
    try:
        safely_extract(plaintext, staging)
        (staging / "michiflin").replace(target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    print(f"[backup] state restored from {repo}")


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"save", "restore"}:
        print("Usage: python state_backup.py save | restore", file=sys.stderr)
        return 2
    try:
        if sys.argv[1] == "save":
            save()
        else:
            restore()
    except Exception as exc:
        print(f"[backup] error: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
