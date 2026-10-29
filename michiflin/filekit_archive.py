"""Networkless, unprivileged archive worker for the temporary file workspace.

The engine is the same separately-licensed WASM module used offline. Untrusted
archives are decoded inside its virtual filesystem, itself inside a chroot and
fresh network/mount/PID namespaces. Failure to create the isolation fails closed.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import filekit, state

APP = Path(__file__).resolve().parent.parent / "webxdc"
RUNNER = r'''#!/bin/sh
set -eu
umask 022
ROOT="$1"
WORK="$2"
NODE="$3"
DATA_LIMIT="$4"
FILE_LIMIT="$5"
mkdir -p "$ROOT/usr" "$ROOT/etc" "$ROOT/dev" "$ROOT/tmp" "$ROOT/work" "$ROOT/runtime"
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
printf 'sandbox:x:65533:65533:sandbox:/work:/bin/sh\n' > "$ROOT/etc/passwd"
printf 'sandbox:x:65533:\n' > "$ROOT/etc/group"
mount -t tmpfs -o size=16m,mode=755,nosuid,nodev tmpfs "$ROOT/dev"
for dev in null zero random urandom; do touch "$ROOT/dev/$dev"; mount --bind "/dev/$dev" "$ROOT/dev/$dev"; done
mount -t tmpfs -o size=32m,mode=1777,nosuid,nodev tmpfs "$ROOT/tmp"
mount --bind "$WORK" "$ROOT/work"
touch "$ROOT/runtime/node"
mount --bind "$NODE" "$ROOT/runtime/node"
mount -o remount,bind,ro "$ROOT/runtime/node"
umask 077
cd "$ROOT/work"
chroot "$ROOT" /usr/bin/setpriv --reuid=65533 --regid=65533 --clear-groups --no-new-privs \
  /usr/bin/env -i PATH=/usr/bin:/bin HOME=/work TMPDIR=/tmp LANG=C.UTF-8 \
  /usr/bin/prlimit --cpu=180 --data="$DATA_LIMIT" --fsize="$FILE_LIMIT" --nofile=256 --nproc=128 -- \
  /runtime/node /work/filekit_node.js
'''


def available() -> bool:
    commands = ("sudo", "unshare", "setpriv", "prlimit", "timeout", "mount", "chroot")
    # System isolation tools use a fixed trusted search path. Node is selected
    # by the operator's PATH (for example setup-node's hosted tool cache) and
    # bind-mounted by absolute resolved path; the sandbox gets a clean PATH.
    return bool(shutil.which("node") and all(
        shutil.which(name, path=os.defpath + ":/usr/local/bin:/usr/local/sbin:/usr/sbin:/sbin") for name in commands
    ))


def _available_memory() -> int:
    """Conservative free RAM, also honoring a cgroup's limit when present."""
    try:
        fields = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines() if ":" in line)
        available_bytes = int(fields["MemAvailable"].split()[0]) * 1024
    except (OSError, KeyError, ValueError):
        available_bytes = 512 * 1024 * 1024
    try:
        line = next(line for line in Path("/proc/self/cgroup").read_text().splitlines() if line.startswith("0::"))
        relative = line[3:].strip().lstrip("/")
        if ".." not in relative.split("/"):
            group = Path("/sys/fs/cgroup") / relative
            maximum = (group / "memory.max").read_text().strip()
            current = int((group / "memory.current").read_text().strip())
            if maximum != "max":
                available_bytes = min(available_bytes, max(0, int(maximum) - current))
    except (OSError, ValueError, StopIteration):
        pass
    return available_bytes


def limits() -> tuple[int, int]:
    """Bound real writable mappings and the virtual filesystem before parsing."""
    free = _available_memory()
    data_limit = min(int(free * 0.65), 2 * 1024 * 1024 * 1024)
    if data_limit < 192 * 1024 * 1024:
        raise filekit.FileKitError("memory_unavailable")
    # Input, output, transfer buffers and library overhead coexist. 512 MiB is
    # also below the addressable limit of this 32-bit WASM build.
    archive_limit = min((data_limit - 128 * 1024 * 1024) // 3, 512 * 1024 * 1024)
    if archive_limit < 8 * 1024 * 1024:
        raise filekit.FileKitError("memory_unavailable")
    return archive_limit, data_limit


def _command(argv: list[str], *, timeout: int = 20) -> None:
    try:
        result = subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout,
                                env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"})
    except (OSError, subprocess.SubprocessError):
        raise filekit.FileKitError("isolation_unavailable") from None
    if result.returncode:
        raise filekit.FileKitError("isolation_unavailable")


@contextmanager
def _temporary_jail() -> Iterator[Path]:
    base = Path(tempfile.mkdtemp(prefix="michiflin-filekit-"))
    try:
        base.chmod(0o755)
        yield base
    finally:
        # Root-owned mountpoint directories were created only under this unique
        # mkdtemp path. The namespace and its mounts have already exited.
        if base.parent == Path(tempfile.gettempdir()) and base.name.startswith("michiflin-filekit-") and not base.is_symlink():
            _command(["sudo", "-n", "rm", "-rf", "--", str(base)], timeout=15)


def _terminate(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, PermissionError):
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        # The PID and group were created by this function, never supplied by a user.
        try:
            subprocess.run(["sudo", "-n", "kill", "-KILL", "--", f"-{process.pid}"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass


def _run(work: Path, jail: Path, runner: Path, payload: dict[str, Any], archive_limit: int, data_limit: int) -> dict[str, Any]:
    if not available():
        raise filekit.FileKitError("isolation_unavailable")
    node = shutil.which("node")
    if not node:
        raise filekit.FileKitError("engine_unavailable")
    node = str(Path(node).resolve())
    _command(["sudo", "-n", "chown", "-R", "65533:65533", str(work)])
    args = ["sudo", "-n", "timeout", "-k", "5", "195", "unshare", "--net", "--mount", "--pid", "--fork", "--kill-child", "--",
            str(runner), str(jail), str(work), node, str(data_limit), str(max(archive_limit * 2, 8 * 1024 * 1024))]
    safe_env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
    process = None
    try:
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=output, stderr=subprocess.DEVNULL,
                                       env=safe_env, start_new_session=True)
            assert process.stdin is not None
            process.stdin.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            process.stdin.close()
            deadline = time.monotonic() + 200
            while process.poll() is None:
                state.check_work_allowed()
                if time.monotonic() >= deadline:
                    raise filekit.FileKitError("archive_timeout")
                time.sleep(0.1)
            output.seek(0)
            raw = output.read(8 * 1024 * 1024 + 1)
            if process.returncode or len(raw) > 8 * 1024 * 1024:
                raise filekit.FileKitError("archive_failed")
            try:
                result = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                raise filekit.FileKitError("archive_failed") from None
            if not isinstance(result, dict) or not result.get("ok"):
                code = result.get("code", "archive_failed") if isinstance(result, dict) else "archive_failed"
                allowed = {"unsafe_path", "unsafe_link", "unsafe_listing", "duplicate_path", "output_limit", "input_limit", "listing_limit",
                           "wrong_password", "archive_failed", "engine_unavailable", "invalid_password", "unsupported_format", "invalid_volume"}
                raise filekit.FileKitError(code if code in allowed else "archive_failed")
            return result.get("result") or {}
    except BaseException:
        if process and process.poll() is None:
            _terminate(process)
        raise
    finally:
        _command(["sudo", "-n", "chown", "-R", f"{os.getuid()}:{os.getgid()}", str(work)])


def _safe_result(root: Path, relative: str) -> Path:
    filekit._path("/" + relative, root=False)
    target = root / relative
    current = root
    for part in relative.split("/"):
        current = current / part
        if current.is_symlink():
            raise filekit.FileKitError("unsafe_link")
    try:
        if not target.resolve(strict=True).is_relative_to(root.resolve(strict=True)):
            raise filekit.FileKitError("unsafe_path")
    except OSError:
        raise filekit.FileKitError("content_missing") from None
    return target


def archive(space: filekit.Workspace, action: str, paths: list[str], *, folder: str, name: str = "", password: str = "",
            format: str = "", volume_bytes: int = 0, first: str = "") -> dict[str, Any]:
    """Copy a snapshot into the jail; publish checked output in one transaction."""
    if action not in {"create", "extract", "list"}:
        raise filekit.FileKitError("invalid_request")
    folder = filekit._path(folder)
    selected = filekit._roots(paths)
    if not isinstance(password, str) or len(password) > 1024 or any(char in password for char in "\x00\r\n"):
        raise filekit.FileKitError("invalid_password")
    archive_limit, data_limit = limits()
    if not available():
        raise filekit.FileKitError("isolation_unavailable")
    with _temporary_jail() as base:
        work, jail = base / "work", base / "jail"
        work.mkdir(mode=0o700)
        jail.mkdir(mode=0o755)
        (work / "input").mkdir(mode=0o700)
        runner = base / "runner.sh"
        runner.write_text(RUNNER, encoding="utf-8")
        runner.chmod(0o755)
        for source, destination in ((APP / "filekit-worker.js", work / "filekit-worker.js"),
                                    (APP / "vendor/7zip/sevenzip-wasm.js", work / "sevenzip-wasm.js"),
                                    (APP / "vendor/7zip/sevenzip-wasm.wasm", work / "sevenzip-wasm.wasm"),
                                    (Path(__file__).with_name("filekit_node.js"), work / "filekit_node.js")):
            shutil.copyfile(source, destination)
        sources = []
        input_bytes = 0
        translated_first = ""
        with space.lock, space._db() as db:
            all_entries = filekit._snapshot(db)
            filekit._directory(all_entries, folder)
            for selected_path in selected:
                if selected_path not in all_entries:
                    raise filekit.FileKitError("missing")
                for entry in all_entries.values():
                    if not filekit._within(entry["path"], selected_path):
                        continue
                    relative = filekit._base(selected_path) + entry["path"][len(selected_path):]
                    filekit._path("/" + relative, root=False)
                    if entry["path"] == first:
                        translated_first = relative
                    if entry["type"] == "dir":
                        sources.append({"path": relative, "type": "dir"})
                    else:
                        input_bytes += int(entry["size"])
                        if input_bytes > archive_limit:
                            raise filekit.FileKitError("input_limit")
                        number = str(len(sources))
                        with filekit.open_source(space._object(entry["blob"])) as source, (work / "input" / number).open("xb") as target:
                            if os.fstat(source.fileno()).st_size != int(entry["size"]):
                                raise filekit.FileKitError("content_missing")
                            copied = 0
                            while True:
                                state.check_work_allowed()
                                chunk = source.read(1024 * 1024)
                                if not chunk:
                                    break
                                copied += len(chunk)
                                if copied > int(entry["size"]):
                                    raise filekit.FileKitError("content_missing")
                                target.write(chunk)
                            if copied != int(entry["size"]):
                                raise filekit.FileKitError("content_missing")
                        sources.append({"path": relative, "type": "file", "source": number})
                    if len(sources) > 10000:
                        raise filekit.FileKitError("listing_limit")
        if len({item["path"] for item in sources}) != len(sources):
            raise filekit.FileKitError("duplicate_path")
        job: dict[str, Any] = {"action": action, "limit": archive_limit, "password": password}
        if action == "create":
            job.update({"name": filekit._name(name), "format": format, "volumeBytes": volume_bytes})
        else:
            if not translated_first:
                raise filekit.FileKitError("missing_archive")
            job["first"] = translated_first
        result = _run(work, jail, runner, {"job": job, "files": sources}, archive_limit, data_limit)
        state.check_work_allowed()
        if action == "list":
            return result
        output = result.get("files")
        if not isinstance(output, list):
            raise filekit.FileKitError("archive_failed")
        result_root = work / "result"
        with ExitStack() as stack:
            imported = []
            if action == "extract":
                destination = filekit._name(name)
                imported.append({"path": destination, "type": "dir"})
            for item in output:
                if not isinstance(item, dict) or item.get("type") not in {"dir", "file"}:
                    raise filekit.FileKitError("archive_failed")
                relative = item.get("path")
                if not isinstance(relative, str):
                    raise filekit.FileKitError("unsafe_path")
                path = _safe_result(result_root, relative)
                target = f"{destination}/{relative}" if action == "extract" else relative
                if item["type"] == "dir":
                    if not path.is_dir():
                        raise filekit.FileKitError("unsafe_type")
                    imported.append({"path": target, "type": "dir"})
                else:
                    stream = stack.enter_context(filekit.open_source(path))
                    if os.fstat(stream.fileno()).st_size != int(item.get("size", -1)):
                        raise filekit.FileKitError("archive_failed")
                    imported.append({"path": target, "source": stream})
            if not imported:
                raise filekit.FileKitError("archive_failed")
            space.put(folder, imported)
        return space.view()
