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
  STATE_BACKUP_STORAGE (releases by default; legacy-git only for rollback)

Usage: python state_backup.py save | restore
"""
from __future__ import annotations

import base64
import contextlib
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import tomllib
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
RELEASE_TAG = "michiflin-state-releases-v1"
READY_TAG = "michiflin-state-release-ready-v1"
RELEASE_MARKER = "Managed encrypted Michiflin state snapshots v1"
ASSET_PREFIX = "michiflin-state-"
PENDING_PREFIX = "michiflin-pending-"
ASSET_PATTERN = re.compile(r"^michiflin-(?:state|pending)-\d{8}T\d{6}Z-[0-9a-f]{16}\.bin$")
KEEP_RECENT = 12
KEEP_DAILY = 7
KEEP_WEEKLY = 4


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
                    # Never publish a possibly inconsistent live database in place
                    # of its snapshot, and never prune an older backup afterward.
                    raise RuntimeError("could not safely snapshot a state database; old backups kept") from exc

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
                if path.is_symlink():
                    raise ValueError("state directory contains a symbolic link; old backups kept")
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


def _save_legacy() -> None:
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


def _restore_legacy() -> None:
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
    plaintext = decrypt_backup(blob, passphrase)
    _install_restored(plaintext, target)
    print(f"[backup] state restored from {repo} (legacy Git snapshot)")



def _repo_base(repo: str) -> str:
    return f"https://api.github.com/repos/{repo}"


def _get_ref(token: str, repo: str, tag: str) -> str | None:
    response = requests.get(f"{_git_api(repo)}/ref/tags/{quote(tag, safe='')}",
                            headers=headers(token), timeout=30)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return str(response.json()["object"]["sha"])


def _assert_private(token: str, repo: str) -> None:
    response = requests.get(_repo_base(repo), headers=headers(token), timeout=30)
    response.raise_for_status()
    data = response.json()
    if data.get("private") is not True or data.get("archived") is True:
        raise ValueError("backup repository must be private and writable; old backups were kept")


def _release(token: str, repo: str) -> dict | None:
    response = requests.get(f"{_repo_base(repo)}/releases/tags/{RELEASE_TAG}",
                            headers=headers(token), timeout=30)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    item = response.json()
    if not isinstance(item, dict) or not str(item.get("body") or "").startswith(RELEASE_MARKER):
        raise ValueError("the reserved backup release does not belong to this application")
    if not isinstance(item.get("id"), int) or item["id"] <= 0:
        raise ValueError("invalid release metadata")
    return item


def _make_ref(token: str, repo: str, tag: str, sha: str) -> None:
    response = requests.post(f"{_git_api(repo)}/refs", headers=headers(token), timeout=30,
                             json={"ref": "refs/tags/" + tag, "sha": sha})
    if response.status_code == 422 and _get_ref(token, repo, tag) == sha:
        return
    response.raise_for_status()


def _ensure_release(token: str, repo: str) -> dict:
    existing = _release(token, repo)
    if existing is not None:
        return existing
    # A no-parent, state-free anchor prevents the release tag from retaining the old
    # Git backup chain. It is never the default branch, and no force-push occurs.
    anchor = _get_ref(token, repo, RELEASE_TAG)
    if anchor is None:
        base = _git_api(repo)
        tree = requests.post(base + "/trees", headers=headers(token), timeout=30,
                             json={"tree": [{"path": ".michiflin-backup-anchor", "mode": "100644", "type": "blob",
                                             "content": "Anchor for encrypted release assets; no account state.\n"}]})
        tree.raise_for_status()
        commit = requests.post(base + "/commits", headers=headers(token), timeout=30,
                               json={"message": "Encrypted state release anchor", "tree": tree.json()["sha"], "parents": []})
        commit.raise_for_status()
        anchor = str(commit.json()["sha"])
        try:
            _make_ref(token, repo, RELEASE_TAG, anchor)
        except requests.HTTPError:
            # Another authorized worker may have created the anchor first.
            anchor = _get_ref(token, repo, RELEASE_TAG)
            if not anchor:
                raise
    response = requests.post(f"{_repo_base(repo)}/releases", headers=headers(token), timeout=60,
                             json={"tag_name": RELEASE_TAG, "name": "Encrypted Michiflin state",
                                   "body": RELEASE_MARKER + ". Only managed encrypted snapshots are rotated.",
                                   "draft": False, "prerelease": False})
    if response.status_code == 422:
        found = _release(token, repo)
        if found is not None:
            return found
    response.raise_for_status()
    created = _release(token, repo)
    if created is None:
        raise RuntimeError("new backup release could not be verified")
    return created


def _assets(token: str, repo: str, release_id: int) -> list[dict]:
    result: list[dict] = []
    for page in range(1, 21):
        response = requests.get(f"{_repo_base(repo)}/releases/{release_id}/assets", headers=headers(token),
                                params={"per_page": 100, "page": page}, timeout=60)
        response.raise_for_status()
        batch = response.json()
        if not isinstance(batch, list):
            raise ValueError("invalid backup asset listing")
        result.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < 100:
            return result
    raise RuntimeError("too many backup assets to safely rotate")


def _created(item: dict) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(item["created_at"]).replace("Z", "+00:00"))
        return value.astimezone(timezone.utc) if value.tzinfo else None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _managed(item: dict, prefix: str = ASSET_PREFIX) -> bool:
    name = item.get("name")
    return (isinstance(name, str) and name.startswith(prefix) and ASSET_PATTERN.fullmatch(name) is not None
            and isinstance(item.get("id"), int) and item["id"] > 0
            and item.get("state") == "uploaded" and _created(item) is not None)


def retention_ids(items: list[dict]) -> set[int]:
    """Keep the newest 12, plus up to 7 distinct days and 4 distinct weeks."""
    ordered = sorted((x for x in items if _managed(x)), key=lambda x: (_created(x), x["id"]), reverse=True)
    keep = {x["id"] for x in ordered[:KEEP_RECENT]}
    days: set[object] = set()
    weeks: set[tuple[int, int]] = set()
    for item in ordered:
        when = _created(item)
        assert when is not None
        day = when.date()
        year, week, _weekday = when.isocalendar()
        if day not in days and len(days) < KEEP_DAILY:
            keep.add(item["id"])
            days.add(day)
        if (year, week) not in weeks and len(weeks) < KEEP_WEEKLY:
            keep.add(item["id"])
            weeks.add((year, week))
    return keep


def decrypt_backup(blob: bytes, passphrase: str) -> bytes:
    if not blob.startswith(MAGIC) or len(blob) <= len(MAGIC) + SALT_BYTES:
        raise ValueError("remote file is not a valid Michiflin AI backup")
    salt_at = len(MAGIC)
    salt = blob[salt_at : salt_at + SALT_BYTES]
    try:
        return fernet_from(passphrase, salt).decrypt(blob[salt_at + SALT_BYTES :])
    except InvalidToken as exc:
        raise ValueError("could not decrypt backup; check STATE_BACKUP_PASSPHRASE") from exc


def _verify_plaintext(plaintext: bytes) -> None:
    """Prove the encrypted download is extractable and SQLite snapshots are sound."""
    with tempfile.TemporaryDirectory(prefix=".backup-verify-") as directory:
        root = Path(directory)
        safely_extract(plaintext, root)
        state_dir = root / "michiflin"
        if not state_dir.is_dir():
            raise ValueError("backup is missing its state directory")
        state_json = state_dir / "michiflin-state.json"
        if state_json.exists():
            with state_json.open("r", encoding="utf-8") as handle:
                if not isinstance(json.load(handle), dict):
                    raise ValueError("invalid state metadata")
        accounts = state_dir / "accounts"
        manifest = accounts / "accounts.toml"
        if manifest.exists():
            data = tomllib.loads(manifest.read_text(encoding="utf-8"))
            for entry in data.get("accounts", []):
                folder = entry.get("dir") if isinstance(entry, dict) else None
                if not isinstance(folder, str) or not folder or Path(folder).name != folder:
                    raise ValueError("invalid account manifest")
                database = accounts / folder / "dc.db"
                if not database.is_file() or not _is_sqlite(database):
                    raise ValueError("account database is missing or invalid")
        for path in state_dir.rglob("*"):
            if path.is_file() and _is_sqlite(path):
                with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)) as connection:
                    result = connection.execute("PRAGMA quick_check").fetchone()
                if not result or result[0] != "ok":
                    raise ValueError("state database failed its integrity check")


def _download_asset(token: str, repo: str, asset_id: int) -> bytes:
    response = requests.get(f"{_repo_base(repo)}/releases/assets/{asset_id}",
                            headers={**headers(token), "Accept": "application/octet-stream"}, timeout=180)
    response.raise_for_status()
    if len(response.content) > MAX_BACKUP_BYTES:
        raise ValueError("remote backup exceeds the safe size limit")
    return response.content


def _mark_ready(token: str, repo: str) -> None:
    anchor = _get_ref(token, repo, RELEASE_TAG)
    if not anchor:
        raise RuntimeError("backup release anchor is missing")
    ready = _get_ref(token, repo, READY_TAG)
    if ready is None:
        _make_ref(token, repo, READY_TAG, anchor)
    elif ready != anchor:
        raise RuntimeError("backup release marker does not match its anchor")


def _prune(token: str, repo: str, release_id: int, newly_verified: int, passphrase: str) -> None:
    items = _assets(token, repo, release_id)
    candidates = sorted((x for x in items if _managed(x) and x["id"] != newly_verified),
                        key=lambda x: (_created(x), x["id"]), reverse=True)
    # The new snapshot and at least one independently downloaded prior snapshot
    # must both decrypt and pass integrity checks before any deletion.
    prior_verified = False
    for item in candidates[:5]:
        try:
            _verify_plaintext(decrypt_backup(_download_asset(token, repo, item["id"]), passphrase))
            prior_verified = True
            break
        except requests.RequestException:
            raise  # a network outage is not proof that a previous copy is corrupt
        except (ValueError, tarfile.TarError, sqlite3.Error, OSError):
            continue
    if not prior_verified:
        print("[backup] rotation deferred until two independently verified copies exist")
        return
    keep = retention_ids(items)
    now = datetime.now(timezone.utc)
    removed = 0
    for item in items:
        when = _created(item)
        old_snapshot = _managed(item) and item["id"] not in keep
        old_pending = _managed(item, PENDING_PREFIX) and when is not None and when < now - timedelta(days=2)
        if not (old_snapshot or old_pending):
            continue
        if removed >= 200:
            break
        response = requests.delete(f"{_repo_base(repo)}/releases/assets/{item['id']}",
                                   headers=headers(token), timeout=60)
        if response.status_code not in (204, 404):
            response.raise_for_status()
        removed += 1
    print(f"[backup] rotation kept up to {KEEP_RECENT} recent, {KEEP_DAILY} daily and {KEEP_WEEKLY} weekly snapshots; removed {removed}")


def _save_release(token: str, repo: str, passphrase: str, *, rotate: bool = True) -> None:
    _assert_private(token, repo)
    plaintext = archive(config_dir())
    salt = os.urandom(SALT_BYTES)
    encrypted = MAGIC + salt + fernet_from(passphrase, salt).encrypt(plaintext)
    if len(encrypted) > MAX_BACKUP_BYTES:
        raise ValueError("encrypted state is too large; previous backups were kept")
    release = _ensure_release(token, repo)
    rid = release["id"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(8) + ".bin"
    pending_name, final_name = PENDING_PREFIX + stamp, ASSET_PREFIX + stamp
    # Construct the upload URL ourselves; never send credentials to a URL from a response.
    upload = f"https://uploads.github.com/repos/{repo}/releases/{rid}/assets"
    response = requests.post(upload, headers={**headers(token), "Content-Type": "application/octet-stream"},
                             params={"name": pending_name}, data=encrypted, timeout=180, allow_redirects=False)
    response.raise_for_status()
    asset = response.json()
    if not isinstance(asset.get("id"), int) or asset["id"] <= 0:
        raise ValueError("backup upload returned invalid metadata; previous copies were kept")
    asset_id = asset["id"]
    downloaded = _download_asset(token, repo, asset_id)
    if not secrets.compare_digest(hashlib.sha256(downloaded).digest(), hashlib.sha256(encrypted).digest()):
        raise ValueError("uploaded backup did not match its download; previous copies were kept")
    _verify_plaintext(decrypt_backup(downloaded, passphrase))
    response = requests.patch(f"{_repo_base(repo)}/releases/assets/{asset_id}", headers=headers(token),
                              json={"name": final_name}, timeout=60)
    response.raise_for_status()
    renamed = response.json()
    if renamed.get("name") != final_name or renamed.get("id") != asset_id or renamed.get("state") != "uploaded":
        raise RuntimeError("verified backup could not be marked ready; old copies were kept")
    _mark_ready(token, repo)
    print(f"[backup] encrypted and verified snapshot saved ({len(encrypted) // 1024} KB)")
    if rotate:
        try:
            _prune(token, repo, rid, asset_id, passphrase)
        except (requests.RequestException, ValueError, tarfile.TarError, sqlite3.Error, OSError, RuntimeError) as exc:
            # A maintenance failure must never invalidate an already verified backup.
            print(f"[backup] rotation deferred ({exc.__class__.__name__}); old copies were kept where possible")


def _install_restored(plaintext: bytes, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".restore-", dir=target.parent))
    try:
        safely_extract(plaintext, staging)
        state_dir = staging / "michiflin"
        if not state_dir.is_dir():
            raise ValueError("backup is missing its state directory")
        if target.exists():
            raise FileExistsError("local state appeared while restoring; it was not overwritten")
        state_dir.replace(target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


@contextlib.contextmanager
def _save_lock():
    # The periodic job and the final shutdown hook may overlap locally.
    target = config_dir().absolute()
    target.parent.mkdir(parents=True, exist_ok=True)
    name = ".michiflin-backup-" + hashlib.sha256(str(target).encode()).hexdigest()[:16] + ".lock"
    fd = os.open(str(target.parent / name), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def save(*, rotate: bool = True) -> None:
    """Save state; short-lived delivery checkpoints may retain all old copies."""
    settings = configuration()
    if not settings:
        if os.getenv("STATE_BACKUP_REQUIRED", "0").strip() == "1":
            raise RuntimeError("required backup secrets are missing; no backup was saved")
        print("[backup] disabled: backup secrets are missing")
        return
    storage = os.getenv("STATE_BACKUP_STORAGE", "releases").strip()
    with _save_lock():
        if storage == "legacy-git":
            _save_legacy()
        elif storage == "releases":
            _save_release(*settings, rotate=rotate)
        else:
            raise ValueError("unknown backup storage mode")


def restore() -> None:
    settings = configuration()
    required = os.getenv("STATE_BACKUP_REQUIRED", "0").strip() == "1"
    if not settings:
        if required:
            raise RuntimeError("required backup secrets are missing; refusing to initialize a fresh account")
        print("[backup] restoration disabled: backup secrets are missing")
        return
    target = config_dir()
    if target.exists():
        print("[backup] local state already exists; it will not be overwritten")
        return
    storage = os.getenv("STATE_BACKUP_STORAGE", "releases").strip()
    if storage == "legacy-git":
        _restore_legacy()
        return
    if storage != "releases":
        raise ValueError("unknown backup storage mode")
    token, repo, passphrase = settings
    ready = _get_ref(token, repo, READY_TAG)
    release = _release(token, repo)
    if ready:
        anchor = _get_ref(token, repo, RELEASE_TAG)
        if not anchor or anchor != ready or release is None:
            raise RuntimeError("verified release backup is missing; refusing stale Git fallback")
    if release is not None:
        snapshots = sorted((x for x in _assets(token, repo, release["id"]) if _managed(x)),
                           key=lambda x: (_created(x), x["id"]), reverse=True)
        for index, item in enumerate(snapshots):
            # Network failures are not mistaken for corruption or an empty account.
            blob = _download_asset(token, repo, item["id"])
            try:
                plaintext = decrypt_backup(blob, passphrase)
                _verify_plaintext(plaintext)
            except (ValueError, tarfile.TarError, sqlite3.Error, OSError):
                print("[backup] an invalid encrypted snapshot was skipped")
                continue
            _install_restored(plaintext, target)
            print("[backup] restored a verified release snapshot" + (" (older recovery copy)" if index else ""))
            return
        if snapshots or ready:
            raise RuntimeError("no verified release snapshot could be restored; old copies were kept")
    if ready:
        raise RuntimeError("verified release backup is missing; refusing stale Git fallback")
    # Bootstrap only: once the ready marker exists, the old Git snapshot cannot
    # silently overwrite newer work if release assets are missing.
    print("[backup] release storage not initialized; trying the existing Git snapshot once")
    _restore_legacy()


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
        # A request exception may contain a signed asset URL. Never log it.
        detail = "remote request failed; previous backups were kept" if isinstance(exc, requests.RequestException) else str(exc)
        print(f"[backup] error: {exc.__class__.__name__}: {detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
