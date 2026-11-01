#!/usr/bin/env python3
"""Read-only legacy snapshot export. Never starts a bot or changes a backup.

The encrypted result is deliberately labelled partial: the old backup format
never included external queue/file-kit bytes, temporary files, or large blobs.
All paths and upload destinations are fixed by the reviewed workflow, not used
as shell commands. No account contents, config values or URLs are printed.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import time
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, urlsplit

import requests

EXPORT_NAMES = (
    "OWNER_ADDRESS", "OWNER_BACKUP_ADDRESS", "OWNER_NOTICE", "BOT_EPOCH",
    "BAZAARLINK_API_KEY", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_AI_TOKEN", "ELTOQUE_API_TOKEN",
    "GEMINI_API_KEY", "GROQ_API_KEY", "HF_TOKEN", "MICHIFLIN_GITHUB_TOKEN", "MISTRAL_API_KEY",
    "OPENROUTER_API_KEY", "ORCAROUTER_API_KEY", "POLLINATIONS_API_KEY", "YDC_API_KEY", "ZAI_API_KEY",
    "WEB_SESSION_KEY", "STATE_BACKUP_REPO", "STATE_BACKUP_PASSPHRASE", "STATE_GITHUB_TOKEN",
    "OWNER_ONLY", "INVITE_AUTO_ACCESS", "EMAIL_KEY_RECOVERY",
)
DESTINATION = "/data/home/michiflin-staging/production-legacy-recovery-20261101.tar.gpg"
MAX_TOTAL_BYTES = 2 * 1024**3


class ExportError(Exception):
    pass


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _safe_name(name: str) -> bool:
    p = PurePosixPath(name)
    return bool(name and not p.is_absolute() and ".." not in p.parts and "\x00" not in name)


def settings_bytes(environment: dict[str, str]) -> bytes:
    # Secrets, including the existing backup key, are encrypted together with
    # the state. The export passphrase, signed URL and runner tokens are NOT.
    selected = {name: environment.get(name, "") for name in EXPORT_NAMES}
    return json.dumps({"version": 1, "values": selected}, ensure_ascii=True,
                      separators=(",", ":"), sort_keys=True).encode("utf-8")


def records_for(root: Path, settings: bytes) -> tuple[list[dict[str, Any]], list[str]]:
    if root.is_symlink() or not root.is_dir():
        raise ExportError("state unavailable")
    files: list[dict[str, Any]] = [{"path": "settings.json", "size": len(settings),
                                    "sha256": hashlib.sha256(settings).hexdigest()}]
    dirs = ["state"]
    total = len(settings)
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        name = "state/" + rel
        if not _safe_name(name):
            raise ExportError("unsafe state name")
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            dirs.append(name)
        elif stat.S_ISREG(info.st_mode):
            total += info.st_size
            if total > MAX_TOTAL_BYTES:
                raise ExportError("state exceeds safe export limit")
            files.append({"path": name, "size": info.st_size, "sha256": _digest(path)})
        else:
            raise ExportError("state contains unsupported file type")
    if len({x["path"] for x in files}) != len(files):
        raise ExportError("duplicate state name")
    return files, dirs


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.mode = 0o600
    info.size = len(data)
    info.mtime = 0
    tar.addfile(info, io.BytesIO(data))


def create_archive(root: Path, settings: bytes, key: Path, dest: Path, asset_id: int | None = None) -> None:
    if dest.exists() or not key.is_file() or key.is_symlink() or not key.stat().st_size:
        raise ExportError("export preflight failed")
    files, dirs = records_for(root, settings)
    manifest = json.dumps({"version": 1, "kind": "partial-legacy-snapshot", "asset_id": asset_id,
                           "files": files, "directories": dirs,
                           "limitations": ["external job and file-kit data absent", "large blobs and temporary files omitted by legacy backup"]},
                          separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    if len(manifest) > 20_000_000:
        raise ExportError("manifest too large")
    partial = dest.with_name(dest.name + ".partial")
    if partial.exists():
        raise ExportError("export destination already exists")
    proc = subprocess.Popen(["gpg", "--batch", "--yes", "--pinentry-mode", "loopback",
                             "--passphrase-file", str(key), "--cipher-algo", "AES256",
                             "--s2k-digest-algo", "SHA256", "--compress-algo", "ZLIB",
                             "--output", str(partial), "--symmetric"],
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if proc.stdin is None:
            raise ExportError("encryption unavailable")
        with tarfile.open(fileobj=proc.stdin, mode="w|") as tar:
            _add_bytes(tar, "manifest.json", manifest)
            _add_bytes(tar, "settings.json", settings)
            for name in dirs:
                source = root if name == "state" else root / name.removeprefix("state/")
                info = tar.gettarinfo(str(source), arcname=name)
                if not info.isdir():
                    raise ExportError("directory changed during export")
                tar.addfile(info)
            for record in files:
                name = record["path"]
                if name == "settings.json":
                    continue
                path = root / name.removeprefix("state/")
                info = tar.gettarinfo(str(path), arcname=name)
                if not info.isfile() or info.size != record["size"]:
                    raise ExportError("state changed during export")
                with path.open("rb") as f:
                    tar.addfile(info, f)
        proc.stdin.close()
        if proc.wait(timeout=360):
            raise ExportError("encryption failed")
        verify_archive(partial, key)
        os.link(partial, dest, follow_symlinks=False)  # fail rather than overwrite
        partial.unlink()
    except BaseException:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if proc.stdin is not None and not proc.stdin.closed:
            proc.stdin.close()
        partial.unlink(missing_ok=True)
        raise


def verify_archive(archive: Path, key: Path) -> dict[str, Any]:
    proc = subprocess.Popen(["gpg", "--batch", "--quiet", "--pinentry-mode", "loopback",
                             "--passphrase-file", str(key), "--decrypt", str(archive)],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    manifest: dict[str, Any] | None = None
    expected: dict[str, dict[str, Any]] = {}
    expected_dirs: set[str] = set()
    seen_files: set[str] = set()
    seen_dirs: set[str] = set()
    try:
        if proc.stdout is None:
            raise ExportError("decryption unavailable")
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            for index, item in enumerate(tar):
                name = item.name.rstrip("/")
                if not _safe_name(name) or item.issym() or item.islnk():
                    raise ExportError("unsafe archive")
                if index == 0:
                    if name != "manifest.json" or not item.isfile() or item.size > 20_000_000:
                        raise ExportError("invalid manifest")
                    f = tar.extractfile(item)
                    if f is None:
                        raise ExportError("invalid manifest")
                    manifest = json.load(f)
                    if manifest.get("version") != 1 or manifest.get("kind") != "partial-legacy-snapshot":
                        raise ExportError("invalid manifest")
                    records = manifest.get("files")
                    directories = manifest.get("directories")
                    if not isinstance(records, list) or not isinstance(directories, list):
                        raise ExportError("invalid manifest")
                    expected = {x["path"]: x for x in records}
                    expected_dirs = set(directories)
                    if len(expected) != len(records) or len(expected_dirs) != len(directories):
                        raise ExportError("duplicate manifest paths")
                    if any(not _safe_name(x) for x in set(expected) | expected_dirs):
                        raise ExportError("unsafe manifest")
                    continue
                if item.isdir():
                    if name not in expected_dirs or name in seen_dirs:
                        raise ExportError("unexpected directory")
                    seen_dirs.add(name)
                elif item.isfile():
                    if name not in expected or name in seen_files or item.size != expected[name]["size"]:
                        raise ExportError("unexpected file")
                    f = tar.extractfile(item)
                    if f is None:
                        raise ExportError("unreadable file")
                    h = hashlib.sha256()
                    for block in iter(lambda: f.read(1024 * 1024), b""):
                        h.update(block)
                    if h.hexdigest() != expected[name]["sha256"]:
                        raise ExportError("content mismatch")
                    seen_files.add(name)
                else:
                    raise ExportError("unsupported archive content")
        proc.stdout.close()
        if proc.wait(timeout=30):
            raise ExportError("decryption failed")
        if manifest is None or seen_files != set(expected) or seen_dirs != expected_dirs:
            raise ExportError("incomplete archive")
        return manifest
    except BaseException:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if proc.stdout is not None and not proc.stdout.closed:
            proc.stdout.close()
        raise


def validate_upload_url(url: str) -> None:
    try:
        p = urlsplit(url)
        query = parse_qs(p.query, strict_parsing=True)
        if (p.scheme != "https" or p.hostname != "api.instabox.sh" or p.port not in (None, 443)
                or p.username is not None or p.password is not None or p.fragment
                or not re.fullmatch(r"/v1/machines/m_[a-z0-9]+/files", p.path)
                or query.get("path") != [DESTINATION] or len(query.get("token", [])) != 1):
            raise ExportError("upload destination rejected")
    except (ValueError, TypeError):
        raise ExportError("upload destination rejected") from None


def upload_archive(archive: Path, url: str) -> None:
    validate_upload_url(url)
    size = archive.stat().st_size
    expected = _digest(archive)
    try:
        with archive.open("rb") as f:
            r = requests.put(url, data=f, headers={"Content-Type": "application/octet-stream", "Content-Length": str(size)},
                             timeout=(20, 180), allow_redirects=False)
        if r.status_code != 201:
            raise ExportError("upload not confirmed")
        item = r.json()
        if (item.get("created") is not True or item.get("size") != size or item.get("sha256") != expected
                or item.get("path") != DESTINATION):
            raise ExportError("upload mismatch")
    except (requests.RequestException, ValueError):
        raise ExportError("upload failed") from None


def main() -> int:
    os.umask(0o077)
    try:
        import state_backup
        tmp = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
        root, key, archive = tmp / "migration-readonly", tmp / "migration-key", tmp / "migration-export.tar.gpg"
        if root.exists() or key.exists() or archive.exists():
            raise ExportError("temporary destination already exists")
        password = os.environ.get("MIGRATION_EXPORT_PASSPHRASE", "")
        if len(password) < 32:
            raise ExportError("export key missing")
        key.write_text(password, encoding="utf-8")
        os.chmod(key, 0o600)
        os.environ["CONFIG_DIR"] = str(root)
        os.environ["STATE_BACKUP_REQUIRED"] = "1"
        os.environ["STATE_BACKUP_STORAGE"] = "releases"
        os.environ["GITHUB_TOKEN"] = os.environ.get("STATE_GITHUB_TOKEN", "")
        ids: list[int] = []
        original_download = state_backup._download_asset
        def track(token: str, repo: str, asset_id: int) -> bytes:
            result = original_download(token, repo, asset_id)
            ids.append(asset_id)
            return result
        state_backup._download_asset = track
        state_backup.restore()  # read-only; no account process is started
        if not ids:
            raise ExportError("release identity could not be verified")
        create_archive(root, settings_bytes(dict(os.environ)), key, archive, ids[-1])
        upload_archive(archive, os.environ.get("MIGRATION_UPLOAD_URL", ""))
        print("Encrypted legacy snapshot verified and transferred; this is not a complete live-runner backup.")
        return 0
    except Exception:
        print("Read-only export failed; originals and the running bot were not changed.", file=sys.stderr)
        return 1
    finally:
        # These are newly created private runner files, never the original backup.
        try:
            (Path(os.environ["RUNNER_TEMP"]) / "migration-key").unlink(missing_ok=True)
        except (KeyError, OSError):
            pass


if __name__ == "__main__":
    raise SystemExit(main())
