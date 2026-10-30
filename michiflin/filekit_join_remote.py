"""Stream ordinary numbered fragments into one temporary workspace copy."""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any

from . import config, filekit, state


class _Parts:
    """Read one immutable object at a time; never materialize the whole output."""
    def __init__(self, sources: list[tuple[Path, int]]) -> None:
        self.sources = sources
        self.index = 0
        self.current = None
        self.read_size = 0

    def close(self) -> None:
        if self.current is not None:
            self.current.close()
            self.current = None

    def read(self, amount: int) -> bytes:
        if amount <= 0 or amount > 1024 * 1024:
            raise filekit.FileKitError("invalid_file")
        chunks: list[bytes] = []
        remaining = amount
        while remaining and self.index < len(self.sources):
            state.check_work_allowed()
            path, expected = self.sources[self.index]
            if self.current is None:
                self.current = filekit.open_source(path)
                if os.fstat(self.current.fileno()).st_size != expected:
                    raise filekit.FileKitError("source_changed")
                self.read_size = 0
            block = self.current.read(remaining)
            if block:
                self.read_size += len(block)
                if self.read_size > expected:
                    raise filekit.FileKitError("source_changed")
                chunks.append(block)
                remaining -= len(block)
            else:
                if self.read_size != expected:
                    raise filekit.FileKitError("source_changed")
                self.close()
                self.index += 1
        return b"".join(chunks)


def join(space: filekit.Workspace, paths: list[str], *, folder: str, name: str, sha256: str = "") -> dict[str, Any]:
    folder, name = filekit._path(folder), filekit._name(name)
    if not isinstance(paths, list) or len(paths) < 2 or len(paths) > 999 or any(not isinstance(p, str) for p in paths):
        raise filekit.FileKitError("join_parts")
    paths = [filekit._path(p, root=False) for p in paths]
    if len(set(paths)) != len(paths):
        raise filekit.FileKitError("join_parts")
    if not isinstance(sha256, str) or (sha256 and not re.fullmatch(r"[0-9a-fA-F]{64}", sha256)):
        raise filekit.FileKitError("invalid_hash")
    numbered = []
    expected_base = None
    expected_parent = None
    for path in paths:
        match = re.fullmatch(r"(.+)\.(\d{3})", filekit._base(path))
        if not match:
            raise filekit.FileKitError("join_parts")
        base, number = match.group(1), int(match.group(2))
        if expected_base is None:
            expected_base, expected_parent = base, filekit._parent(path)
        if base != expected_base or filekit._parent(path) != expected_parent or number < 1:
            raise filekit.FileKitError("join_parts")
        numbered.append((number, path))
    numbered.sort()
    if [number for number, _ in numbered] != list(range(1, len(numbered) + 1)):
        raise filekit.FileKitError("join_missing_parts")
    with space.lock:
        sources: list[tuple[Path, int]] = []
        with space._db() as db:
            entries = filekit._snapshot(db)
            filekit._directory(entries, folder)
            if filekit._child(folder, name) in entries:
                raise filekit.FileKitError("exists")
            for _, path in numbered:
                entry = entries.get(path)
                if not entry or entry["type"] != "file":
                    raise filekit.FileKitError("missing")
                sources.append((space._object(entry["blob"]), int(entry["size"])))
        total = sum(size for _, size in sources)
        if total > config.MAX_FILE_BYTES:
            raise filekit.FileKitError("output_limit")
        if shutil.disk_usage(space.root).free < config.MIN_FREE_DISK_BYTES + total:
            raise filekit.FileKitError("disk_full")
        stream = _Parts(sources)
        try:
            try:
                space.put(folder, [{"path": name, "source": stream, "expected_size": total}],
                          expected_sha256=sha256, expected_size=total)
            except filekit.FileKitError as exc:
                if str(exc) == "source_changed":
                    raise filekit.FileKitError("join_mismatch") from None
                raise
        finally:
            stream.close()
        return space.view()
