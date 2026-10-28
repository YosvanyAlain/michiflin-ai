"""Server performance for administrators: host, CPU, memory, disk, network, response times and a bounded stress test."""
from __future__ import annotations

import collections
import hashlib
import os
import platform
import shutil
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import requests

from . import access, chat, config, queue, state

_ops: collections.deque[tuple[float, str, float, bool]] = collections.deque(maxlen=400)
_net_last: tuple[float, int, int] | None = None
_bench_lock = threading.Lock()
_bench: dict[str, Any] = {}
latest_sha = ""
SPEED_URL = "https://speed.cloudflare.com/__down?bytes=25000000"


def note_op(name: str, seconds: float, ok: bool) -> None:
    _ops.append((time.time(), name, seconds, ok))


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _cpu_times() -> tuple[float, float] | None:
    line = _read("/proc/stat").split("\n", 1)[0].split()
    if not line or line[0] != "cpu":
        return None
    values = [float(item) for item in line[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


def cpu_percent(sample: float = 0.25) -> float:
    """Whole-machine CPU use over a short sample."""
    first = _cpu_times()
    if first is None:
        return 0.0
    time.sleep(sample)
    second = _cpu_times() or first
    total, idle = second[0] - first[0], second[1] - first[1]
    return round(max(0.0, min(100.0, 100.0 * (1 - idle / total))) if total > 0 else 0.0, 1)


def _meminfo() -> dict[str, int]:
    found = {}
    for line in _read("/proc/meminfo").splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            found[name] = int(parts[0]) * 1024
    return found


def _rss(pid: int | str) -> int:
    for line in _read(f"/proc/{pid}/status").splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) * 1024
    return 0


def _children(pid: int) -> list[int]:
    found = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            fields = _read(f"/proc/{entry.name}/stat").rsplit(")", 1)[-1].split()
            if len(fields) > 1 and fields[1] == str(pid):
                found.append(int(entry.name))
    return found


def _process_memory() -> int:
    pid = os.getpid()
    total, pending, seen = 0, [pid], set()
    while pending and len(seen) < 200:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        total += _rss(current)
        pending += _children(current)
    return total


def _folder_size(folder: Path, limit: int = 20000) -> int:
    total = 0
    try:
        for count, item in enumerate(folder.rglob("*")):
            if count > limit:
                break
            if item.is_file():
                total += item.stat().st_size
    except OSError:
        pass
    return total


def _network() -> dict[str, float]:
    global _net_last
    received = sent = 0
    for line in _read("/proc/net/dev").splitlines()[2:]:
        name, _, rest = line.partition(":")
        fields = rest.split()
        if name.strip() != "lo" and len(fields) >= 9:
            received += int(fields[0])
            sent += int(fields[8])
    now = time.time()
    rates = {"rx": received, "tx": sent, "rx_rate": 0.0, "tx_rate": 0.0}
    if _net_last and now > _net_last[0]:
        span = now - _net_last[0]
        rates["rx_rate"] = max(0.0, (received - _net_last[1]) / span)
        rates["tx_rate"] = max(0.0, (sent - _net_last[2]) / span)
    _net_last = (now, received, sent)
    return rates


def _cpu_model() -> str:
    for line in _read("/proc/cpuinfo").splitlines():
        if line.lower().startswith("model name"):
            return line.split(":", 1)[1].strip()
    return platform.processor() or platform.machine()


def host() -> dict[str, Any]:
    on_actions = os.getenv("GITHUB_ACTIONS") == "true"
    elapsed = time.monotonic() - config.STARTED_AT
    running = os.getenv("RUNNING_SHA", "")
    return {
        "provider": "GitHub Actions" if on_actions else os.getenv("HOST_PROVIDER", "") or platform.node(),
        "os": f"{os.getenv('ImageOS', '') or platform.system()} {os.getenv('ImageVersion', '')}".strip(),
        "kernel": platform.release(), "arch": platform.machine(), "python": sys.version.split()[0],
        "runner": os.getenv("RUNNER_NAME", ""), "environment": os.getenv("RUNNER_ENVIRONMENT", ""),
        "run": os.getenv("GITHUB_RUN_ID", ""), "number": os.getenv("GITHUB_RUN_NUMBER", ""),
        "attempt": os.getenv("GITHUB_RUN_ATTEMPT", ""), "started": time.time() - elapsed,
        "ends": time.time() + max(0.0, config.SESSION_SECONDS - elapsed),
        "sha": running[:7], "latest": latest_sha[:7], "outdated": bool(running and latest_sha and latest_sha != running),
    }


def _latency() -> dict[str, Any]:
    now = time.time()
    recent = [item for item in _ops if now - item[0] < 3600]
    quick = sorted(seconds for _at, name, seconds, _ok in recent if name in {"hello", "health", "settings", "queue", "models", "admin"})
    slow = sorted(seconds for _at, name, seconds, _ok in recent if name in {"ask", "image", "read", "search", "code", "action"})

    def summary(values: list[float]) -> dict[str, float]:
        if not values:
            return {"n": 0, "median": 0.0, "p90": 0.0}
        return {"n": len(values), "median": round(statistics.median(values), 2), "p90": round(values[int(0.9 * (len(values) - 1))], 2)}

    return {"quick": summary(quick), "work": summary(slow), "errors": sum(1 for item in recent if not item[3]), "total": len(recent)}


def snapshot() -> dict[str, Any]:
    memory = _meminfo()
    load = os.getloadavg() if hasattr(os, "getloadavg") else (0.0, 0.0, 0.0)
    root = config.JOB_ROOT if config.JOB_ROOT.exists() else Path("/")
    disk = shutil.disk_usage(root)
    with state.lock:
        statuses = collections.Counter(job["status"] for job in queue.jobs().values()
                                       if access.job_allowed(job))
    return {
        "now": time.time(), "host": host(),
        "cpu": {"cores": os.cpu_count() or 1, "model": _cpu_model(), "percent": cpu_percent(), "load": [round(value, 2) for value in load]},
        "memory": {"total": memory.get("MemTotal", 0), "available": memory.get("MemAvailable", 0), "bot": _process_memory(),
                   "swap": memory.get("SwapTotal", 0) - memory.get("SwapFree", 0)},
        "disk": {"total": disk.total, "used": disk.used, "free": disk.free, "jobs": _folder_size(config.JOB_ROOT),
                 "temp": _folder_size(config.TEMP_ROOT)},
        "process": {"uptime": time.monotonic() - config.STARTED_AT, "threads": threading.active_count(), "pid": os.getpid()},
        "network": _network(), "latency": _latency(),
        "queue": {"queued": statuses.get("queued", 0), "downloading": statuses.get("downloading", 0) + statuses.get("packing", 0),
                  "sending": statuses.get("sending", 0), "ready": statuses.get("ready", 0), "bytes": queue.queue_bytes(),
                  "sent": state.sent_last_day(), "paused": bool(state.flag("queue_paused")),
                  "workers": queue.PREPARERS},
        "bench": dict(_bench),
    }


# ---------------------------------------------------------------------------
# Stress test: a few bounded seconds of CPU, memory, disk, network and core work.
# ---------------------------------------------------------------------------
def _hash_rate(seconds: float, block: bytes) -> float:
    done, started = 0, time.perf_counter()
    while time.perf_counter() - started < seconds:
        hashlib.sha256(block).digest()
        done += len(block)
    return done / (time.perf_counter() - started)


def _cpu_test() -> dict[str, float]:
    block = os.urandom(4 * 1024 * 1024)
    single = _hash_rate(1.5, block)
    cores = os.cpu_count() or 1
    rates: list[float] = []
    workers = [threading.Thread(target=lambda: rates.append(_hash_rate(1.5, block))) for _ in range(cores)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    total = sum(rates)
    return {"single": single, "multi": total, "scale": round(total / single, 2) if single else 0.0, "cores": cores}


def _memory_test() -> float:
    data = bytearray(os.urandom(1024 * 1024)) * 64
    started, copies = time.perf_counter(), 0
    while time.perf_counter() - started < 1.0:
        bytes(data)
        copies += 1
    return copies * len(data) / (time.perf_counter() - started)


def _disk_test() -> dict[str, float]:
    size, chunk = 256 * 1024 * 1024, os.urandom(4 * 1024 * 1024)
    folder = config.JOB_ROOT if config.JOB_ROOT.exists() else Path(tempfile.gettempdir())
    if shutil.disk_usage(folder).free < size * 8:
        return {"skipped": 1.0}
    path = folder / f".bench-{os.getpid()}"
    try:
        started = time.perf_counter()
        with path.open("wb") as handle:
            for _ in range(size // len(chunk)):
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        write = size / (time.perf_counter() - started)
        with path.open("rb") as handle:
            if hasattr(os, "posix_fadvise"):
                os.posix_fadvise(handle.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
            started = time.perf_counter()
            while handle.read(len(chunk)):
                pass
        read = size / (time.perf_counter() - started)
        return {"write": write, "read": read}
    finally:
        path.unlink(missing_ok=True)


def _network_test() -> dict[str, float]:
    started = time.perf_counter()
    received = 0
    try:
        with requests.get(SPEED_URL, stream=True, timeout=15) as response:
            first = time.perf_counter() - started
            for block in response.iter_content(256 * 1024):
                received += len(block)
                if time.perf_counter() - started > 12:
                    break
    except requests.RequestException:
        return {"down": 0.0, "latency": 0.0}
    span = time.perf_counter() - started
    return {"down": received / span if span else 0.0, "latency": round(first * 1000)}


def _core_test() -> dict[str, float]:
    if not chat.ready():
        return {"rpc": 0.0}
    times = []
    for _ in range(10):
        started = time.perf_counter()
        try:
            chat.rpc().get_connectivity(chat.account())
        except Exception:
            return {"rpc": 0.0}
        times.append(time.perf_counter() - started)
    started = time.perf_counter()
    state.save()
    return {"rpc": round(statistics.median(times) * 1000, 1), "save": round((time.perf_counter() - started) * 1000, 1)}


def bench() -> dict[str, Any]:
    """Run the stress test once at a time; about 15 seconds, never more than a few hundred MB of temporary disk."""
    global _bench
    if not _bench_lock.acquire(blocking=False):
        return {"busy": True}
    try:
        started = time.time()
        before = cpu_percent()
        result = {"at": started, "cpu": _cpu_test(), "memory": _memory_test(), "disk": _disk_test(), "network": _network_test(),
                  "core": _core_test(), "load_before": before}
        result["seconds"] = round(time.time() - started, 1)
        _bench = result
        return result
    finally:
        _bench_lock.release()
