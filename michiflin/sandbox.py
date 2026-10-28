"""Short-lived Linux namespaces for the interactive browser.

Fail closed when the runner cannot create the namespaces and chroot. The
unprivileged worker sees no host home, bot state, process environment, or
external network route. A bounded gateway mediates public HTTPS connections.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from . import egress

UID = "65533"
RUNNER = r'''#!/bin/sh
set -eu
ROOT="$1"
WORK="$2"
SOCK="$3"
BROWSERS="$4"
PYTHON="$5"
PYROOT="$6"
DENO="$7"
ip link set lo up
mkdir -p "$ROOT/usr" "$ROOT/opt" "$ROOT/etc" "$ROOT/dev" "$ROOT/proc" "$ROOT/tmp" "$ROOT/work" "$ROOT/browsers"
mount --bind /usr "$ROOT/usr"
mount -o remount,bind,ro "$ROOT/usr"
# Actions may place Python under /opt. Mount only that interpreter's runtime,
# never the whole hosted-tool cache or unrelated files in /opt.
if [ -n "$PYROOT" ]; then
  mkdir -p "$ROOT$PYROOT"
  mount --bind "$PYROOT" "$ROOT$PYROOT"
  mount -o remount,bind,ro "$ROOT$PYROOT"
fi
if [ -n "$DENO" ]; then
  mkdir -p "$ROOT/runtime"
  touch "$ROOT/runtime/deno"
  mount --bind "$DENO" "$ROOT/runtime/deno"
  mount -o remount,bind,ro "$ROOT/runtime/deno"
fi
for path in bin sbin lib lib64; do
  if [ -L "/$path" ]; then
    ln -s "$(readlink "/$path")" "$ROOT/$path"
  elif [ -d "/$path" ]; then
    mkdir -p "$ROOT/$path"; mount --bind "/$path" "$ROOT/$path"; mount -o remount,bind,ro "$ROOT/$path"
  fi
done
# Only non-secret system configuration required by the browser and interpreter.
for path in ssl/certs fonts alternatives; do
  if [ -d "/etc/$path" ]; then
    mkdir -p "$ROOT/etc/$path"; mount --bind "/etc/$path" "$ROOT/etc/$path"; mount -o remount,bind,ro "$ROOT/etc/$path"
  fi
done
for path in ld.so.cache ssl/openssl.cnf; do
  if [ -f "/etc/$path" ]; then
    mkdir -p "$(dirname "$ROOT/etc/$path")"; touch "$ROOT/etc/$path"; mount --bind "/etc/$path" "$ROOT/etc/$path"; mount -o remount,bind,ro "$ROOT/etc/$path"
  fi
done
printf 'sandbox:x:65533:65533:sandbox:/work:/bin/sh\n' > "$ROOT/etc/passwd"
printf 'sandbox:x:65533:\n' > "$ROOT/etc/group"
mount -t tmpfs -o size=16m,mode=755,nosuid,nodev tmpfs "$ROOT/dev"
for dev in null zero random urandom; do touch "$ROOT/dev/$dev"; mount --bind "/dev/$dev" "$ROOT/dev/$dev"; done
mkdir -m 1777 "$ROOT/dev/shm"
mount -t proc -o nosuid,nodev,noexec proc "$ROOT/proc"
mount -t tmpfs -o size=256m,mode=1777,nosuid,nodev tmpfs "$ROOT/tmp"
mount --bind "$WORK" "$ROOT/work"
mount --bind "$BROWSERS" "$ROOT/browsers"
mount -o remount,bind,ro "$ROOT/browsers"
# The loopback listener has no access to a host TCP port. Only the gateway's
# Unix socket is reachable from outside the chroot by this unprivileged relay.
# Use a distinct UID: a worker must not traverse /proc/<relay-pid>/root to
# reach the relay's host filesystem, even though they share a PID namespace.
setpriv --reuid=65532 --regid=65532 --clear-groups --no-new-privs \
  socat TCP-LISTEN:18080,bind=127.0.0.1,reuseaddr,fork UNIX-CONNECT:"$SOCK" &
relay=$!
trap 'kill "$relay" 2>/dev/null || true' EXIT
cd "$ROOT/work"
chroot "$ROOT" /usr/bin/setpriv --reuid=65533 --regid=65533 --clear-groups --no-new-privs \
  /usr/bin/env -i PATH=/runtime:/usr/local/bin:/usr/bin:/bin HOME=/work TMPDIR=/tmp LANG=C.UTF-8 \
  HTTP_PROXY=http://127.0.0.1:18080 HTTPS_PROXY=http://127.0.0.1:18080 \
  http_proxy=http://127.0.0.1:18080 https_proxy=http://127.0.0.1:18080 NO_PROXY= no_proxy= \
  PLAYWRIGHT_BROWSERS_PATH=/browsers /usr/bin/prlimit --cpu=180 --nproc=512 --nofile=1024 -- \
  "$PYTHON" /work/worker.py
'''


class SandboxError(Exception):
    """A failure without credentials or provider details."""


def available() -> bool:
    path = os.defpath + ":/usr/local/sbin:/usr/sbin:/sbin"
    return all(shutil.which(tool, path=path) for tool in ("sudo", "unshare", "setpriv", "prlimit", "timeout", "socat", "ip", "mount", "chroot"))


def browser_cache() -> Path:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            root = Path(playwright.chromium.executable_path).parents[2]
        if not root.is_dir():
            raise ValueError("missing")
        return root
    except Exception:
        raise SandboxError("browser_missing") from None


class BrowserProcess:
    def __init__(self, worker_file: str = "remote_worker.py", max_bytes: int = 100_000_000, lifetime: int = 660) -> None:
        if worker_file not in {"remote_worker.py", "media_worker.py"}:
            raise SandboxError("worker_unavailable")
        if not available():
            raise SandboxError("isolation_unavailable")
        self.root = Path(tempfile.mkdtemp(prefix="michiflin-browser-"))
        self.root.chmod(0o711)  # the relay must traverse this random name to reach its socket
        self.gateway: egress.Gateway | None = None
        self.process: subprocess.Popen[bytes] | None = None
        self._closed = False
        try:
            work, jail = self.root / "work", self.root / "jail"
            work.mkdir(mode=0o700)
            jail.mkdir(mode=0o755)
            source = Path(__file__).with_name(worker_file)
            shutil.copyfile(source, work / "worker.py")
            (work / "worker.py").chmod(0o644)
            runner = self.root / "runner.sh"
            runner.write_text(RUNNER, encoding="utf-8")
            runner.chmod(0o755)
            changed = subprocess.run(["sudo", "-n", "chown", UID + ":" + UID, str(work)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            if changed.returncode:
                raise SandboxError("isolation_unavailable")
            self.gateway = egress.Gateway(self.root / "gate.sock", max_bytes=max_bytes)
            python = str(Path(sys.executable).resolve())
            if not python.startswith(("/usr/", "/opt/")):
                raise SandboxError("python_unavailable")
            if worker_file == "remote_worker.py":
                cache = browser_cache()
            else:
                cache = self.root / "empty-browsers"
                cache.mkdir(mode=0o755)
            python_root = str(Path(python).parent.parent) if python.startswith("/opt/") else ""
            if python_root and (python_root in {"/opt", "/opt/hostedtoolcache"} or not Path(python_root).is_dir()):
                raise SandboxError("python_unavailable")
            deno = shutil.which("deno") or ""
            deno = str(Path(deno).resolve()) if deno else ""
            cmd = ["sudo", "-n", "timeout", "-k", "5", str(max(60, min(lifetime, 3600))), "unshare", "--net", "--mount", "--pid", "--fork",
                   "--kill-child", "--", str(runner), str(jail), str(work), str(self.root / "gate.sock"), str(cache), python, python_root, deno]
            # sudo/unshare and the PID 1 shell never inherit runner or bot secrets.
            safe_env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
            self.process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                            env=safe_env, start_new_session=True, bufsize=0)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.process is not None:
            try:
                self.process.terminate()
                self.process.wait(timeout=8)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self.process.kill()
                    self.process.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            for pipe in (self.process.stdin, self.process.stdout):
                if pipe:
                    pipe.close()
        if self.gateway is not None:
            self.gateway.close()
        # Only paths allocated by mkdtemp directly in the system temp directory.
        if self.root.parent == Path(tempfile.gettempdir()) and self.root.name.startswith("michiflin-browser-"):
            try:
                subprocess.run(["sudo", "-n", "rm", "-rf", "--", str(self.root)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            except (OSError, subprocess.SubprocessError):
                pass
            shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self) -> BrowserProcess:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()
