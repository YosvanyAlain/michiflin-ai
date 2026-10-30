"""Selective large-RAR reading in a temporary, networkless native sandbox.

Archive bytes are read through read-only bind mounts, not loaded into RAM or
copied into a WASM filesystem. The UnRAR library streams only selected file
bytes to anonymous numbered scratch files; the parent validates and imports
those copies atomically. No archive-provided path is ever used as a filesystem
output path. Passwords and member names enter the child through its stdin only.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import config, filekit, filekit_archive, state

_WORKER = Path(__file__).with_name("filekit_rar_worker.py")
_VERSION_KEY = secrets.token_bytes(32)  # deliberately changes after every bot restart
_MIB = 1024 * 1024

RUNNER = r'''#!/bin/sh
set -eu
umask 022
ROOT="$1"; WORK="$2"; LIB="$3"; UIDNUM="$4"; GIDNUM="$5"; MEMORY="$6"; FILEMAX="$7"; MANIFEST="$8"
mkdir -p "$ROOT/usr" "$ROOT/etc" "$ROOT/dev" "$ROOT/tmp" "$ROOT/work" "$ROOT/runtime" "$ROOT/input"
mount --bind /usr "$ROOT/usr"
mount -o remount,bind,ro "$ROOT/usr"
for name in bin sbin lib lib64; do
  if [ -L "/$name" ]; then
    ln -s "$(readlink "/$name")" "$ROOT/$name"
  elif [ -d "/$name" ]; then
    mkdir -p "$ROOT/$name"; mount --bind "/$name" "$ROOT/$name"; mount -o remount,bind,ro "$ROOT/$name"
  fi
done
if [ -f /etc/ld.so.cache ]; then
  touch "$ROOT/etc/ld.so.cache"
  mount --bind /etc/ld.so.cache "$ROOT/etc/ld.so.cache"
  mount -o remount,bind,ro "$ROOT/etc/ld.so.cache"
fi
printf 'sandbox:x:%s:%s:sandbox:/work:/bin/sh\n' "$UIDNUM" "$GIDNUM" > "$ROOT/etc/passwd"
printf 'sandbox:x:%s:\n' "$GIDNUM" > "$ROOT/etc/group"
mount -t tmpfs -o size=16m,mode=755,nosuid,nodev tmpfs "$ROOT/dev"
for dev in null zero random urandom; do touch "$ROOT/dev/$dev"; mount --bind "/dev/$dev" "$ROOT/dev/$dev"; done
mount -t tmpfs -o size=32m,mode=1777,nosuid,nodev tmpfs "$ROOT/tmp"
mount --bind "$WORK" "$ROOT/work"
touch "$ROOT/runtime/libunrar.so"
mount --bind "$LIB" "$ROOT/runtime/libunrar.so"
mount -o remount,bind,ro "$ROOT/runtime/libunrar.so"
# The private manifest keeps archive volume names out of process arguments.
while IFS=: read -r SRC64 NAME64; do
  SRC="$(printf %s "$SRC64" | base64 -d)"
  NAME="$(printf %s "$NAME64" | base64 -d)"
  touch "$ROOT/input/$NAME"
  mount --bind "$SRC" "$ROOT/input/$NAME"
  mount -o remount,bind,ro "$ROOT/input/$NAME"
done < "$MANIFEST"
umask 077
cd "$ROOT/work"
printf 'READY\n'
chroot "$ROOT" /usr/bin/setpriv --reuid="$UIDNUM" --regid="$GIDNUM" --clear-groups --no-new-privs \
  /usr/bin/env -i PATH=/usr/bin:/bin HOME=/work TMPDIR=/tmp LANG=C.UTF-8 \
  /usr/bin/prlimit --cpu=900 --as="$MEMORY" --fsize="$FILEMAX" --nofile=256 --nproc=128 -- \
  /usr/bin/python3 /work/filekit_rar_worker.py
'''


def native_library() -> Path | None:
    raw = os.environ.get("MICHIFLIN_UNRAR_LIB", "")
    if not raw:
        return None
    path = Path(raw)
    try:
        if path.is_absolute() and not path.is_symlink() and path.is_file():
            return path.resolve(strict=True)
    except OSError:
        pass
    return None


def available() -> bool:
    return bool(os.getuid() != 0 and native_library() and _WORKER.is_file() and filekit_archive.available()
                and Path("/usr/bin/python3").is_file())


def _memory_limit() -> tuple[int, int]:
    available_bytes = filekit_archive._available_memory()
    limit = min(int(available_bytes * 0.65), 2 * 1024 * _MIB)
    if limit < 256 * _MIB:
        raise filekit.FileKitError("memory_unavailable")
    return limit, max(64, (limit - 128 * _MIB) // _MIB)


@contextmanager
def _jail(root: Path) -> Iterator[Path]:
    # Unlike the old WASM jail, scratch output lives on the actual workspace
    # filesystem rather than a potentially tiny /tmp tmpfs. The workspace
    # lock is held by callers, so remnants of a crashed earlier operation are
    # not still in use and can be removed before creating a new scratch tree.
    for candidate in root.glob(".rar-work-*"):
        if candidate.parent == root and not candidate.is_symlink() and candidate.is_dir():
            filekit_archive._command(["sudo", "-n", "rm", "-rf", "--", str(candidate)], timeout=30)
    base = Path(tempfile.mkdtemp(prefix=".rar-work-", dir=root))
    try:
        base.chmod(0o755)
        yield base
    finally:
        if base.parent == root and base.name.startswith(".rar-work-") and not base.is_symlink():
            filekit_archive._command(["sudo", "-n", "rm", "-rf", "--", str(base)], timeout=30)


def _run(base: Path, sources: list[dict[str, Any]], payload: dict[str, Any], file_limit: int) -> dict[str, Any]:
    if not available():
        raise filekit.FileKitError("isolation_unavailable")
    lib = native_library()
    if lib is None:
        raise filekit.FileKitError("engine_unavailable")
    memory, memory_mib = _memory_limit()
    work = base / "work"
    if not work.exists():
        work.mkdir(mode=0o700)
    # A fresh mount tree per invocation: listing and extraction may run one
    # after another in the same scratch directory.
    jail = Path(tempfile.mkdtemp(prefix="jail-", dir=base))
    jail.chmod(0o755)
    runner = base / "runner.sh"
    runner.write_text(RUNNER, encoding="utf-8")
    runner.chmod(0o755)
    shutil.copyfile(_WORKER, work / "filekit_rar_worker.py")
    (work / "result").mkdir(mode=0o700, exist_ok=True)
    manifest = base / "mounts.private"
    with os.fdopen(os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="ascii") as handle:
        for item in sources:
            source = base64.b64encode(os.fsencode(item["object"])).decode("ascii")
            name = base64.b64encode(item["name"].encode("utf-8")).decode("ascii")
            handle.write(source + ":" + name + "\n")
    args = ["sudo", "-n", "timeout", "-k", "5", "920", "unshare", "--net", "--mount", "--pid", "--fork", "--kill-child", "--",
            str(runner), str(jail), str(work), str(lib), str(os.getuid()), str(os.getgid()), str(memory),
            str(max(8 * _MIB, file_limit)), str(manifest)]
    payload = {**payload, "memoryMiB": memory_mib}
    environment = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
    process: subprocess.Popen | None = None
    try:
        with tempfile.TemporaryFile(dir=base) as output:
            process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=output, stderr=subprocess.DEVNULL,
                                       env=environment, start_new_session=True)
            assert process.stdin is not None
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(encoded) > 1024 * 1024:
                raise filekit.FileKitError("listing_limit")
            process.stdin.write(encoded)
            process.stdin.close()
            deadline = time.monotonic() + 925
            while process.poll() is None:
                state.check_work_allowed()
                if time.monotonic() >= deadline:
                    raise filekit.FileKitError("archive_timeout")
                time.sleep(0.1)
            output.seek(0)
            raw = output.read(8 * _MIB + 1)
            if not raw.startswith(b"READY\n"):
                raise filekit.FileKitError("isolation_unavailable")
            if process.returncode or len(raw) > 8 * _MIB:
                raise filekit.FileKitError("archive_failed")
            try:
                response = json.loads(raw[len(b"READY\n"):])
            except (ValueError, UnicodeDecodeError):
                raise filekit.FileKitError("archive_failed") from None
            if not isinstance(response, dict) or not response.get("ok"):
                code = response.get("code", "archive_failed") if isinstance(response, dict) else "archive_failed"
                allowed = {"invalid_request", "nothing_selected", "invalid_file", "invalid_password", "missing_archive", "missing",
                           "unsafe_path", "unsafe_link", "duplicate_path", "listing_limit", "output_limit", "disk_full", "wrong_password",
                           "wrong_password_or_corrupt", "archive_failed", "engine_unavailable", "memory_unavailable", "archive_timeout"}
                raise filekit.FileKitError(code if code in allowed else "archive_failed")
            result = response.get("result")
            if not isinstance(result, dict):
                raise filekit.FileKitError("archive_failed")
            return result
    except BaseException:
        if process and process.poll() is None:
            filekit_archive._terminate(process)
        raise


def _sources(space: filekit.Workspace, paths: list[str], first: str) -> tuple[list[dict[str, Any]], str]:
    selected = filekit._roots(paths)
    first = filekit._path(first, root=False)
    if first not in selected or len(selected) > 1000:
        raise filekit.FileKitError("missing_archive")
    sources = []
    names = set()
    with space._db() as db:
        entries = filekit._snapshot(db)
        for path in selected:
            item = entries.get(path)
            if not item or item["type"] != "file":
                raise filekit.FileKitError("missing_archive")
            name = filekit._base(path)
            if name in names or name.lower().endswith(".rev"):
                raise filekit.FileKitError("duplicate_path")
            names.add(name)
            blob = space._object(item["blob"])
            with filekit.open_source(blob) as stream:
                if os.fstat(stream.fileno()).st_size != int(item["size"]):
                    raise filekit.FileKitError("source_changed")
            sources.append({"path": path, "name": name, "blob": item["blob"], "size": int(item["size"]), "object": blob})
    digest_data = json.dumps([[i["path"], i["blob"], i["size"]] for i in sources] + [["first", first]],
                             ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    version = hmac.new(_VERSION_KEY, digest_data, hashlib.sha256).hexdigest()
    return sources, version


def _validated_listing(result: dict[str, Any]) -> list[dict[str, Any]]:
    entries = result.get("entries")
    if not isinstance(entries, list) or len(entries) > 10000:
        raise filekit.FileKitError("unsafe_listing")
    seen: set[str] = set()
    safe = []
    for item in entries:
        if not isinstance(item, dict) or item.get("type") not in {"file", "dir"}:
            raise filekit.FileKitError("unsafe_listing")
        path = item.get("path")
        if not isinstance(path, str):
            raise filekit.FileKitError("unsafe_path")
        try:
            filekit._path("/" + path, root=False)
        except filekit.FileKitError:
            raise filekit.FileKitError("unsafe_path") from None
        if len(path.encode("utf-8")) > 4096 or len(path.split("/")) > 64 or re.match(r"^[A-Za-z]:", path):
            raise filekit.FileKitError("unsafe_path")
        if path in seen:
            raise filekit.FileKitError("duplicate_path")
        seen.add(path)
        size = item.get("size")
        if type(size) is not int or size < 0:
            raise filekit.FileKitError("unsafe_listing")
        safe.append({"path": path, "type": item["type"], "size": size, "encrypted": bool(item.get("encrypted"))})
    return safe


def inspect(space: filekit.Workspace, paths: list[str], first: str, password: str) -> dict[str, Any]:
    if not available():
        raise filekit.FileKitError("engine_unavailable")
    with space.lock:
        sources, version = _sources(space, paths, first)
        first_name = next(item["name"] for item in sources if item["path"] == first)
        with _jail(space.root) as base:
            result = _run(base, sources, {"action": "list", "archive": "/input/" + first_name,
                          "password": password, "outputLimit": 0, "diskReserve": config.MIN_FREE_DISK_BYTES}, 8 * _MIB)
        state.check_work_allowed()
        return {"entries": _validated_listing(result), "version": version,
                "solid": bool(result.get("solid")), "multiVolume": bool(result.get("multiVolume"))}


def extract(space: filekit.Workspace, paths: list[str], first: str, password: str, selected: list[str], version: str,
            *, folder: str, name: str) -> dict[str, Any]:
    if not available():
        raise filekit.FileKitError("engine_unavailable")
    folder, name = filekit._path(folder), filekit._name(name)
    if not isinstance(selected, list) or not selected or len(selected) > 1000:
        raise filekit.FileKitError("nothing_selected")
    if any(not isinstance(value, str) or len(value) > 4096 for value in selected):
        raise filekit.FileKitError("invalid_path")
    if len(set(selected)) != len(selected):
        raise filekit.FileKitError("duplicate_path")
    if not isinstance(version, str) or not re.fullmatch(r"[0-9a-f]{64}", version):
        raise filekit.FileKitError("source_changed")
    with space.lock:
        sources, current_version = _sources(space, paths, first)
        if not hmac.compare_digest(current_version, version):
            raise filekit.FileKitError("source_changed")
        with space._db() as db:
            entries = filekit._snapshot(db)
            filekit._directory(entries, folder)
            if filekit._child(folder, name) in entries:
                raise filekit.FileKitError("exists")
        first_name = next(item["name"] for item in sources if item["path"] == first)
        with _jail(space.root) as base:
            common = {"archive": "/input/" + first_name, "password": password, "diskReserve": config.MIN_FREE_DISK_BYTES}
            listed = _run(base, sources, {**common, "action": "list", "outputLimit": 0}, 8 * _MIB)
            members = _validated_listing(listed)
            by_path = {entry["path"]: entry for entry in members}
            if any(path not in by_path or by_path[path]["type"] != "file" for path in selected):
                raise filekit.FileKitError("missing")
            sizes = [by_path[path]["size"] for path in selected]
            if any(size > config.MAX_FILE_BYTES for size in sizes) or sum(sizes) > config.MAX_QUEUE_BYTES:
                raise filekit.FileKitError("output_limit")
            total = sum(sizes)
            # Scratch plus the atomic staged copies can coexist until commit.
            if shutil.disk_usage(space.root).free < config.MIN_FREE_DISK_BYTES + total * 2:
                raise filekit.FileKitError("disk_full")
            result = _run(base, sources, {**common, "action": "extract", "outputLimit": total, "selected": selected},
                          max(sizes, default=0) + 1)
            state.check_work_allowed()
            files = result.get("files")
            if not isinstance(files, list) or len(files) != len(selected):
                raise filekit.FileKitError("archive_failed")
            seen: set[str] = set()
            slots: set[str] = set()
            with ExitStack() as stack:
                imports: list[dict[str, Any]] = [{"path": name, "type": "dir"}]
                for item in files:
                    if not isinstance(item, dict):
                        raise filekit.FileKitError("archive_failed")
                    path, slot, size = item.get("path"), item.get("slot"), item.get("size")
                    if path not in by_path or path not in selected or path in seen or not isinstance(slot, str) or not re.fullmatch(r"[0-9]{1,6}", slot) or slot in slots:
                        raise filekit.FileKitError("archive_failed")
                    if type(size) is not int or size != by_path[path]["size"]:
                        raise filekit.FileKitError("archive_failed")
                    source = filekit_archive._safe_result(base / "work/result", slot)
                    stream = stack.enter_context(filekit.open_source(source))
                    if os.fstat(stream.fileno()).st_size != size:
                        raise filekit.FileKitError("archive_failed")
                    imports.append({"path": name + "/" + path, "source": stream, "expected_size": size})
                    seen.add(path); slots.add(slot)
                if seen != set(selected):
                    raise filekit.FileKitError("archive_failed")
                space.put(folder, imports)
        return space.view()
