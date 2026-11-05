"""Short-lived filesystem jail for untrusted Programador commands.

The worker receives only the current project, public read-only runtimes and a
private temporary filesystem. Network and PID namespaces are separate. Fail
closed if any mount, privilege drop or chroot operation is unavailable.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

RUNNER = r'''#!/bin/sh
set -eu
umask 022
ROOT="$1"
WORK="$2"
TOOLPATH="$3"
CMD="$4"
shift 4
mkdir -p "$ROOT/usr" "$ROOT/opt" "$ROOT/etc" "$ROOT/dev" "$ROOT/proc" "$ROOT/tmp" "$ROOT/work"
mount --bind /usr "$ROOT/usr"
mount -o remount,bind,ro "$ROOT/usr"
for path in bin sbin lib lib64; do
  if [ -L "/$path" ]; then
    ln -s "$(readlink "/$path")" "$ROOT/$path"
  elif [ -d "/$path" ]; then
    mkdir -p "$ROOT/$path"
    mount --bind "/$path" "$ROOT/$path"
    mount -o remount,bind,ro "$ROOT/$path"
  fi
done
# Only the individual installed runtime trees needed by Python/Node/etc., not
# the entire /opt tree or any bot state. Roots were validated by the parent.
for runtime in "$@"; do
  mkdir -p "$ROOT$runtime"
  mount --bind "$runtime" "$ROOT$runtime"
  mount -o remount,bind,ro "$ROOT$runtime"
done
for path in alternatives ssl/certs 'java-[0-9]*-openjdk'; do
  for source in /etc/$path; do
    if [ -d "$source" ]; then
      relative=${source#/etc/}
      mkdir -p "$ROOT/etc/$relative"
      mount --bind "$source" "$ROOT/etc/$relative"
      mount -o remount,bind,ro "$ROOT/etc/$relative"
    fi
  done
done
if [ -f /etc/ld.so.cache ]; then
  touch "$ROOT/etc/ld.so.cache"
  mount --bind /etc/ld.so.cache "$ROOT/etc/ld.so.cache"
  mount -o remount,bind,ro "$ROOT/etc/ld.so.cache"
fi
printf 'sandbox:x:65534:65534:sandbox:/work:/bin/sh\n' > "$ROOT/etc/passwd"
printf 'sandbox:x:65534:\n' > "$ROOT/etc/group"
mount -t tmpfs -o size=16m,mode=755,nosuid,nodev tmpfs "$ROOT/dev"
for dev in null zero random urandom; do
  touch "$ROOT/dev/$dev"
  mount --bind "/dev/$dev" "$ROOT/dev/$dev"
done
mkdir -m 1777 "$ROOT/dev/shm"
mount -t proc -o nosuid,nodev,noexec proc "$ROOT/proc"
mount -t tmpfs -o size=256m,mode=1777,nosuid,nodev tmpfs "$ROOT/tmp"
mount --bind "$WORK" "$ROOT/work"
chroot "$ROOT" /usr/bin/setpriv --reuid=65534 --regid=65534 --clear-groups --no-new-privs \
  /usr/bin/env -i "PATH=$TOOLPATH" HOME=/tmp LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 PYTHONIOENCODING=utf-8 \
  'JAVA_TOOL_OPTIONS=-Xms16m -Xmx256m -Xss512k -XX:MaxMetaspaceSize=256m -XX:CompressedClassSpaceSize=128m -XX:ReservedCodeCacheSize=64m -XX:ActiveProcessorCount=2 -XX:+UseSerialGC' \
  /usr/bin/prlimit --cpu=20 --as=2147483648 --fsize=20971520 --nproc=256 -- \
  /bin/bash -c "cd /work; umask 000; $CMD"
'''


def runtime_roots() -> list[str]:
    """Bind individual /opt toolchains only; never all of /opt or the user's home."""
    executables = [sys.executable, *(shutil.which(name) or "" for name in ("node", "gcc", "g++", "javac", "java"))]
    candidates: set[Path] = set()
    for value in executables:
        if not value:
            continue
        try:
            binary = Path(value).resolve(strict=True)
        except OSError:
            continue
        if not str(binary).startswith("/opt/"):
            continue
        # Standard installed runtimes place executables in <root>/bin.
        if binary.parent.name != "bin":
            continue
        root = binary.parent.parent
        if root in {Path("/opt"), Path("/opt/hostedtoolcache")} or not root.is_dir():
            continue
        candidates.add(root)
    # Do not attempt a nested mount inside a runtime already made read-only.
    roots: list[Path] = []
    for path in sorted(candidates, key=lambda item: len(item.parts)):
        if not any(parent == path or parent in path.parents for parent in roots):
            roots.append(path)
    return [str(path) for path in roots]


def run(work: Path, command: str, *, timeout: int, tool_path: str) -> tuple[int, str]:
    """Run in an isolated jail or return an error; never fall back to the host."""
    try:
        work = Path(work).resolve(strict=True)
        if not work.is_dir():
            return 1, "[sandbox unavailable]"
        jail = Path(tempfile.mkdtemp(prefix="michi-coder-jail-"))
        jail.chmod(0o755)
    except (OSError, ValueError):
        return 1, "[sandbox unavailable]"
    try:
        args = ["sudo", "-n", "timeout", "-k", "3", str(timeout),
                "unshare", "--net", "--mount", "--pid", "--fork", "--kill-child", "--",
                "/bin/sh", "-c", RUNNER, "coder-jail", str(jail), str(work), tool_path, command, *runtime_roots()]
        # No bot credentials or runtime environment are inherited by the
        # privileged setup process; the command receives a second clean env.
        env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
        try:
            # A command can print without bound. Capture to an unlinked private
            # file instead of allocating its whole output in the bot's memory;
            # the jail's file-size limit applies to this inherited descriptor.
            with tempfile.TemporaryFile() as captured:
                result = subprocess.run(args, stdout=captured, stderr=subprocess.STDOUT, timeout=timeout + 10, env=env)
                size = captured.tell()
                captured.seek(0)
                if size > 32_000:
                    first = captured.read(16_000)
                    captured.seek(-16_000, 2)
                    raw = first + b"\n[...]\n" + captured.read(16_000)
                else:
                    raw = captured.read()
                return result.returncode, raw.decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            return 124, ""
        except (OSError, subprocess.SubprocessError):
            return 1, "[sandbox unavailable]"
    finally:
        # The namespace has ended, so mounts have disappeared. The jail contains
        # no project or bot files, only empty mountpoints and system metadata.
        try:
            subprocess.run(["sudo", "-n", "rm", "-rf", "--", str(jail)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        except (OSError, subprocess.SubprocessError):
            pass
        shutil.rmtree(jail, ignore_errors=True)
