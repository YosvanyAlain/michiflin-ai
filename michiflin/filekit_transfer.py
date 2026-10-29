"""Explicit, authenticated transfers between chat attachments and a file workspace."""
from __future__ import annotations

import os
import re
import secrets
import stat
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from . import access, chat, config, filekit, queue, state

MARKER = "MICHIFLIN-FILEKIT/1 "
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
_pending_lock = threading.Lock()
_pending: dict[str, dict[str, Any]] = {}


def prepare_import(address: str, folder: str, name: str, size: int, sha256: str) -> dict[str, str]:
    """Issue a single-use, short-lived, sender-bound token for a user-sent draft."""
    address = (address or "").strip().lower()
    folder, name = filekit._path(folder), filekit._name(name)
    if (not address or not isinstance(size, int) or isinstance(size, bool) or size < 0 or size > config.MAX_FILE_BYTES
            or not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256)):
        raise filekit.FileKitError("invalid_file")
    space = filekit.Workspace(address)
    view = space.view()
    by_path = {item["path"]: item for item in view["entries"]}
    if folder and (folder not in by_path or by_path[folder]["type"] != "dir"):
        raise filekit.FileKitError("folder_missing")
    if filekit._child(folder, name) in by_path:
        raise filekit.FileKitError("exists")
    state.check_work_allowed()
    now = time.time()
    token = secrets.token_urlsafe(24)
    with _pending_lock:
        for old in [key for key, item in _pending.items() if item["expires"] <= now and not item["busy"]]:
            _pending.pop(old, None)
        _pending[token] = {"address": address, "folder": folder, "name": name, "size": size, "sha256": sha256,
                           "stamp": access.session_stamp(address), "generation": state.kill_generation(address),
                           "expires": now + 1800, "busy": False}
    return {"caption": MARKER + token}


def is_import_caption(text: str) -> bool:
    return str(text or "").strip().startswith(MARKER)


def receive(address: str, message: Any, caption: str) -> None:
    """Copy only the intended received attachment; never delete the chat message."""
    address = (address or "").strip().lower()
    token = str(caption or "").strip().removeprefix(MARKER)
    if not _TOKEN.fullmatch(token):
        raise filekit.FileKitError("import_expired")
    with _pending_lock:
        item = _pending.get(token)
        if (not item or item["address"] != address or item["expires"] <= time.time() or item["busy"]
                or item["stamp"] != access.session_stamp(address) or state.work_interrupted(address, item["generation"])):
            raise filekit.FileKitError("import_expired")
        item["busy"] = True
    success = False
    try:
        path = getattr(message, "file", None)
        if not path:
            raise filekit.FileKitError("content_missing")
        with filekit.open_source(Path(path)) as source:
            if os.fstat(source.fileno()).st_size != item["size"]:
                raise filekit.FileKitError("source_changed")
            space = filekit.Workspace(address)
            space.put(item["folder"], [{"path": item["name"], "source": source, "expected_size": item["size"], "sha256": item["sha256"],
                                       "mime": str(getattr(message, "file_mime", "") or "application/octet-stream")}])
        success = True
    finally:
        with _pending_lock:
            if success:
                _pending.pop(token, None)
            elif token in _pending:
                _pending[token]["busy"] = False


def _open_job_part(job_id: str, name: str) -> Any:
    """Open through non-symlink directory descriptors to avoid traversal races."""
    directory = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        with ExitStack() as stack:
            root_fd = os.open(config.JOB_ROOT, directory)
            stack.callback(os.close, root_fd)
            job_fd = os.open(job_id, directory, dir_fd=root_fd)
            stack.callback(os.close, job_fd)
            parts_fd = os.open("parts", directory, dir_fd=job_fd)
            stack.callback(os.close, parts_fd)
            fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parts_fd)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                raise filekit.FileKitError("invalid_file")
            return os.fdopen(fd, "rb")
    except OSError:
        raise filekit.FileKitError("content_missing") from None


def import_job(space: filekit.Workspace, job_id: str, folder: str, name: str) -> dict[str, Any]:
    """Copy only immutable, prepared parts of an authorized finished/ready job."""
    job_id = str(job_id or "")
    folder, name = filekit._path(folder), filekit._name(name)
    if not re.fullmatch(r"[0-9]{1,20}", job_id):
        raise filekit.FileKitError("missing")
    if space.identity != filekit._identity(chat.sender()):
        raise filekit.FileKitError("not_authorized")
    job = queue.get(job_id)  # requires the authenticated sender to own this job
    if not job:
        raise filekit.FileKitError("missing")
    with state.lock:
        if job.get("status") not in {"ready", "done"}:
            raise filekit.FileKitError("job_active")
        paths = list(job.get("part_files") or [])
        sizes = list(job.get("part_sizes") or [])
        hashes = list(job.get("part_sha") or [])
        complete_sha = str(job.get("sha") or "")
        size = int(job.get("size") or 0)
        transformed = job.get("part_format") in {"segments", "zip", "7z"} and len(paths) > 1
    if not paths or len(paths) > 10000:
        raise filekit.FileKitError("content_missing")
    valid_hash = lambda value: isinstance(value, str) and bool(re.fullmatch(r"[0-9a-fA-F]{64}", value))
    all_part_hashes = len(hashes) == len(paths) and all(valid_hash(value) for value in hashes)
    whole_hash = complete_sha if valid_hash(complete_sha) else ""
    if not all_part_hashes and (transformed or not whole_hash):
        # Never import unverified fragments, even when an older queue record
        # happens to contain paths and sizes. Preserve all originals instead.
        raise filekit.FileKitError("integrity_missing")
    # Playable media segments and ZIP/7z volumes are transformed outputs: their
    # concatenated bytes and size do not equal the original source's SHA or
    # length. Their individual digests are the relevant integrity proof.
    if transformed:
        whole_hash = ""
        size = sum(sizes) if len(sizes) == len(paths) and all(type(value) is int and value >= 0 for value in sizes) else None
    expected_parent = config.JOB_ROOT / job_id / "parts"
    with ExitStack() as stack:
        sources = [{"path": name, "type": "dir"}]
        for index, value in enumerate(paths):
            path = Path(value)
            if path.parent != expected_parent:
                raise filekit.FileKitError("unsafe_path")
            filename = filekit._name(path.name)
            stream = stack.enter_context(_open_job_part(job_id, filename))
            expected_size = sizes[index] if len(sizes) == len(paths) else size if len(paths) == 1 else None
            expected_hash = hashes[index] if all_part_hashes else ""
            sources.append({"path": f"{name}/{filename}", "source": stream, "expected_size": expected_size, "sha256": expected_hash})
        space.put(folder, sources, expected_sha256=whole_hash, expected_size=size)
    return space.view()
