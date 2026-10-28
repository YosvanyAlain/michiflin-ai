"""Run media extraction using one person's browser state in an isolated worker."""
from __future__ import annotations

import json
import os
import re
import select
import shutil
import stat
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from . import config, sandbox, state, web_vault

MAX_LINE = 1_000_000


def storage_for(url: str) -> dict[str, Any] | None:
    """No sender means no private cookie access, even on a background thread."""
    owner = state.request_sender().strip().casefold()
    if not owner:
        return None
    try:
        site = web_vault.site_of(url)
        return web_vault.load(owner, site)
    except web_vault.VaultError as exc:
        if str(exc) in {"bad_url", "not_configured"}:
            return None
        raise ValueError("vault") from None


def _send_input(worker: sandbox.BrowserProcess, value: dict[str, Any]) -> None:
    process = worker.process
    if process is None or process.stdin is None:
        raise ValueError("media_error")
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(raw) > 3_000_000:
        raise ValueError("too_large")
    view = memoryview(raw)
    while view:
        done = process.stdin.write(view)
        if not done:
            raise ValueError("media_error")
        view = view[done:]


def _read(worker: sandbox.BrowserProcess, *, stop: Callable[[], bool] | None = None,
          progress: Callable[[int, int], None] | None = None, timeout: int = 900) -> dict[str, Any]:
    process = worker.process
    if process is None or process.stdout is None:
        raise ValueError("media_error")
    fd = process.stdout.fileno()
    buffer = bytearray()
    end = time.monotonic() + timeout
    result = None
    while time.monotonic() < end:
        if stop and stop():
            raise InterruptedError()
        readable, _, _ = select.select([fd], [], [], 1)
        if not readable:
            if process.poll() is not None:
                break
            continue
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > MAX_LINE and b"\n" not in buffer:
            raise ValueError("media_error")
        while b"\n" in buffer:
            raw, _, remainder = buffer.partition(b"\n")
            buffer = bytearray(remainder)
            if len(raw) > MAX_LINE:
                raise ValueError("media_error")
            try:
                item = json.loads(raw)
            except (ValueError, UnicodeError):
                raise ValueError("media_error") from None
            if not isinstance(item, dict):
                raise ValueError("media_error")
            if item.get("kind") == "progress":
                if progress:
                    progress(max(0, int(item.get("done") or 0)), max(0, int(item.get("total") or 0)))
            elif item.get("kind") == "error":
                code = str(item.get("error") or "media_error")
                raise ValueError(code if code in {"bot_check", "private", "unavailable", "geo", "unsupported", "too_large"} else "media_error")
            elif item.get("kind") in {"info", "done"}:
                result = item
                break
        if result is not None:
            break
    if result is None:
        raise ValueError("media_error")
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        raise ValueError("media_error") from None
    if process.returncode != 0:
        raise ValueError("media_error")
    return result


def inspect(url: str, storage: dict[str, Any]) -> dict[str, Any]:
    try:
        with sandbox.BrowserProcess("media_worker.py", max_bytes=80_000_000, lifetime=180) as worker:
            _send_input(worker, {"operation": "inspect", "url": url, "storage": storage})
            result = _read(worker, timeout=160)
    except sandbox.SandboxError:
        raise ValueError("isolation_unavailable") from None
    info = result.get("info")
    if result.get("kind") != "info" or not isinstance(info, dict):
        raise ValueError("media_error")
    return info


def _claim(worker: sandbox.BrowserProcess, name: str, destination: Path, stop: Callable[[], bool] | None) -> Path:
    if not re.fullmatch(r"media\.[a-zA-Z0-9]{1,8}", name):
        raise ValueError("media_error")
    work = worker.root / "work"
    owner = f"{os.getuid()}:{os.getgid()}"
    for path in (work, work / name):
        # No untrusted process remains after _read() waited for the PID namespace.
        if path != work:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > config.MAX_FILE_BYTES:
                raise ValueError("media_error")
        done = subprocess.run(["sudo", "-n", "chown", "--no-dereference", owner, "--", str(path)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        if done.returncode:
            raise ValueError("media_error")
    source = work / name
    fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > config.MAX_FILE_BYTES:
            raise ValueError("media_error")
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / name
        partial = destination / (name + ".private-part")
        try:
            with os.fdopen(fd, "rb", closefd=False) as source_file, partial.open("wb") as output:
                while True:
                    if stop and stop():
                        raise InterruptedError()
                    block = source_file.read(1024 * 1024)
                    if not block:
                        break
                    output.write(block)
            os.replace(partial, target)
        finally:
            partial.unlink(missing_ok=True)
        return target
    finally:
        os.close(fd)


def _disk_limit(work: Path, destination: Path) -> int:
    """Reserve space for concurrent downloads, intermediates and packaging."""
    destination.mkdir(parents=True, exist_ok=True)
    reserve = max(16_000_000, config.MIN_FREE_DISK_BYTES)
    work_free = shutil.disk_usage(work).free
    dest_free = shutil.disk_usage(destination).free
    if work.stat().st_dev == destination.stat().st_dev:
        # Two preparers can each need a download and a second copy.
        allowance = (min(work_free, dest_free) - reserve) // 4
    else:
        allowance = min((work_free - reserve) // 4, (dest_free - reserve) // 4)
    if allowance < 1_000_000:
        raise ValueError("disk_full")
    return min(config.MAX_FILE_BYTES, allowance)


def download(url: str, storage: dict[str, Any], folder: Path, kind: str, quality: int, audio_format: str,
             progress: Callable[[int, int], None] | None = None, stop: Callable[[], bool] | None = None) -> Path:
    limit = min(8_000_000_000, config.MAX_FILE_BYTES * 2 + 80_000_000)
    try:
        with sandbox.BrowserProcess("media_worker.py", max_bytes=limit, lifetime=1200) as worker:
            maximum = _disk_limit(worker.root, folder)
            _send_input(worker, {"operation": "download", "url": url, "storage": storage, "kind": kind,
                                 "quality": quality, "audio_format": audio_format,
                                 "max_bytes": maximum, "max_minutes": config.MAX_MEDIA_MINUTES})
            result = _read(worker, stop=stop, progress=progress, timeout=1190)
            if result.get("kind") != "done":
                raise ValueError("media_error")
            return _claim(worker, str(result.get("file") or ""), folder, stop)
    except sandbox.SandboxError:
        raise ValueError("isolation_unavailable") from None
