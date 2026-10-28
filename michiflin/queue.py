"""Download queue: prepare (download, verify, split) and deliver parts one by one.

Delivery waits for each part to reach the relay before sending the next, honors the
send window, batch size, delay and optional daily cap, and survives restarts by
re-downloading and resuming when the source hash is unchanged.
"""
from __future__ import annotations

import hashlib
import logging
import re
import shutil
import subprocess
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import quote
from typing import Any, Callable

from . import access, admin, chat, config, files, media, names, progress, state, usage, mail_profiles
from .config import MIB
from .util import clean_filename, duration_text, human_size, local_time, tr

log = logging.getLogger("michiflin")

ACTIVE = {"queued", "downloading", "packing", "ready", "sending"}
FINAL = {"done", "failed", "cancelled"}
STATUS_LABELS = {
    "queued": ("⏳ en cola", "⏳ queued"),
    "downloading": ("⬇️ descargando", "⬇️ downloading"),
    "packing": ("🗜️ empaquetando", "🗜️ packing"),
    "ready": ("📦 listo", "📦 ready"),
    "sending": ("📤 enviando", "📤 sending"),
    "done": ("✅ entregado", "✅ delivered"),
    "failed": ("❌ falló", "❌ failed"),
    "cancelled": ("🚫 cancelado", "🚫 cancelled"),
}

PREPARERS = 2  # downloads run side by side, so one long download never blocks everyone else
_wake = threading.Event()
_claim = threading.Lock()
_stop_flags: dict[str, threading.Event] = {}
_claim_generations: dict[str, int] = {}
_delivery_generations: dict[str, int] = {}
_notices: dict[str, progress.Notice] = {}
_started = False
_locale = "es"


def set_locale(locale: str) -> None:
    global _locale
    _locale = locale or "es"


# ---------------------------------------------------------------------------
# Job storage helpers.
# ---------------------------------------------------------------------------
def jobs() -> dict[str, dict[str, Any]]:
    return state.data["jobs"]


def get(job_id: str) -> dict[str, Any] | None:
    with state.lock:
        job = jobs().get(str(job_id).lstrip("#"))
        return job if job and access.job_allowed(job) else None


LISTENERS: list[Callable[[dict[str, Any], dict[str, Any]], None]] = []


def update(job: dict[str, Any], **values: Any) -> None:
    with state.lock:
        job.update(values)
        job["updated"] = time.time()
    state.save()
    for listener in LISTENERS:
        try:
            listener(job, values)
        except Exception:
            log.exception("job listener failed")


def folder(job: dict[str, Any]) -> Path:
    return config.JOB_ROOT / str(job["id"])


def _next_id() -> str:
    with state.lock:
        number = int(state.data["flags"].get("job_seq", 0)) + 1
        state.data["flags"]["job_seq"] = number
    return str(number)


def queue_bytes() -> int:
    total = 0
    if config.JOB_ROOT.exists():
        for item in config.JOB_ROOT.rglob("*"):
            if item.is_file():
                total += item.stat().st_size
    return total


def free_disk() -> int:
    config.JOB_ROOT.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(config.JOB_ROOT).free


def _new_job(chat_id: int, kind: str, url: str, title: str, size: int, **extra: Any) -> dict[str, Any]:
    address = chat.sender().strip().lower()
    delivery = mail_profiles.effective_settings(address, {
        "part_mb": int(state.setting("part_mb")), "batch": int(state.setting("batch")),
        "delay": int(state.setting("delay")), "daily_cap_mb": int(state.setting("daily_cap_mb")),
    })
    # Nauta strict is a ceiling, never a floor: preserve any smaller verified limit.
    if state.get_nauta_strict(address):
        delivery["part_mb"] = min(delivery["part_mb"], 10)
    job = {
        "id": _next_id(), "chat": chat_id, "kind": kind, "url": url, "title": title, "name": "", "ext": "",
        "mime": "", "label": "", "size_est": size, "size": 0, "quality": 0, "audio_format": state.setting("audio_format"),
        "part_mb": delivery["part_mb"], "part_format": state.setting("part_format"), "status": "queued",
        "delivery_batch": delivery["batch"], "delivery_delay": delivery["delay"],
        "delivery_daily_cap_mb": delivery["daily_cap_mb"],
        "hold": False, "parts": 0, "todo": [], "sent": 0, "sha": "", "created": time.time(), "updated": time.time(),
        "error": "", "not_before": 0.0, "retries": 0, "progress": 0, "messages": [], "resumable": False,
        "locale": _locale, "source_msg": progress.hand_off(), "origin": "app" if chat.capturing() else "chat",
        "addr": address,
    }
    job.update(extra)
    job["access_session"] = access.session_stamp(address)
    # Explicit arguments and persisted jobs must not bypass a verified per-mailbox ceiling.
    delivery = mail_profiles.effective_settings(address, {
        "part_mb": int(job.get("part_mb", delivery["part_mb"])),
        "batch": int(job.get("delivery_batch", delivery["batch"])),
        "delay": int(job.get("delivery_delay", delivery["delay"])),
        "daily_cap_mb": int(job.get("delivery_daily_cap_mb", delivery["daily_cap_mb"])),
    })
    if state.get_nauta_strict(address):
        delivery["part_mb"] = min(delivery["part_mb"], 10)
    job["part_mb"] = delivery["part_mb"]
    job["delivery_batch"] = delivery["batch"]
    job["delivery_delay"] = delivery["delay"]
    job["delivery_daily_cap_mb"] = delivery["daily_cap_mb"]
    return job


def source_messages() -> set[int]:
    """Owner messages whose reaction is still pending on an unfinished job."""
    with state.lock:
        return {int(job["source_msg"]) for job in jobs().values() if job.get("source_msg") and job["status"] not in FINAL}


def _person_error(locale: str) -> str:
    """Per-person limit on unfinished jobs; administrators are exempt."""
    address = chat.sender().lower()
    if not address or admin.is_admin(address):
        return ""
    limit = int(state.setting("user_jobs"))
    with state.lock:
        running = sum(1 for job in jobs().values() if job.get("addr") == address and job["status"] not in FINAL
                      and access.job_allowed(job))
    if running >= limit:
        return tr(locale, f"⚠️ Ya tienes {running} descargas en curso (máximo {limit}). Espera a que terminen para que todos tengan su turno.",
                  f"⚠️ You already have {running} downloads in progress (limit {limit}). Wait for them so everyone gets a turn.")
    return ""


def _capacity_error(size: int, locale: str) -> str:
    problem = _person_error(locale)
    if problem:
        return problem
    if size and size > config.MAX_FILE_BYTES:
        return tr(locale, f"⚠️ El archivo ({human_size(size)}) supera el máximo de {human_size(config.MAX_FILE_BYTES)}.",
                  f"⚠️ The file ({human_size(size)}) exceeds the {human_size(config.MAX_FILE_BYTES)} maximum.")
    reserved = sum(int(job.get("size_est") or 0) for job in jobs().values() if job["status"] in {"queued", "downloading"}
                   and access.job_allowed(job))
    if size and queue_bytes() + reserved + size > config.MAX_QUEUE_BYTES:
        return tr(locale, "⚠️ La cola está llena. Espera a que termine o usa /cola_limpiar.", "⚠️ The queue is full. Wait or use /queue_clear.")
    if size and free_disk() - reserved - size * 2 < config.MIN_FREE_DISK_BYTES:
        return tr(locale, "⚠️ No hay espacio en disco suficiente ahora.", "⚠️ Not enough disk space right now.")
    return ""


def enqueue_file(chat_id: int, info: files.FileInfo, locale: str) -> tuple[dict[str, Any] | None, str]:
    problem = _capacity_error(info.size, locale)
    if problem:
        return None, problem
    job = _new_job(chat_id, "file", info.final_url, info.name, info.size, name=info.name, ext=info.ext, mime=info.mime,
                   label=files.describe(info.kind, locale), resumable=info.resumable, source_url=info.url, locale=locale)
    with state.lock:
        jobs()[job["id"]] = job
    state.save()
    usage.note(job["addr"], jobs=1)
    _wake.set()
    return job, ""


def enqueue_media(chat_id: int, info: media.MediaInfo, kind: str, quality: int, locale: str) -> tuple[dict[str, Any] | None, str]:
    sizes = info.audio_sizes if kind == "audio" else info.video_sizes
    estimate = sizes.get(quality) or (max(sizes.values()) if sizes else 0)
    if kind == "video" and sizes and quality not in sizes:
        fitting = [height for height in sizes if height <= quality]
        estimate = sizes[max(fitting)] if fitting else min(sizes.values())
    problem = _capacity_error(estimate, locale)
    if problem:
        return None, problem
    fmt = state.setting("audio_format") if kind == "audio" else "mp4"
    job = _new_job(chat_id, kind, info.webpage or info.url, info.title, estimate, quality=quality, audio_format=state.setting("audio_format"),
                   ext=fmt, label=(tr(locale, "Audio", "Audio") if kind == "audio" else "Video"), duration=info.duration, locale=locale)
    with state.lock:
        jobs()[job["id"]] = job
    state.save()
    usage.note(job["addr"], jobs=1)
    _wake.set()
    return job, ""


def parts_for(size: int, part_mb: int) -> int:
    part = part_mb * MIB
    if not size:
        return 0
    # Archive volumes add a small header, so an exact multiple spills into one more part.
    return 1 if size <= part else -(-(size + 4096) // part)


# ---------------------------------------------------------------------------
# Live status: the job confirmation message is edited with progress and removed at the end.
# ---------------------------------------------------------------------------
def attach_notice(job: dict[str, Any], msg_id: int, head: str) -> None:
    if msg_id and progress.enabled():
        update(job, status_msg=msg_id, status_head=head)


def _status(job: dict[str, Any], line: str, timer: bool = True) -> None:
    if (not access.job_allowed(job) or not progress.enabled()
            or not chat.ready() or job.get("origin") == "app"):
        return
    head = job.get("status_head") or f"📥 #{job['id']} {(job.get('name') or job['title'])[:100]}"
    locale = job.get("locale", "es")
    body = f"{head}\n{line}\n{names.cmd(locale, 'cancel', job['id'])} · {names.cmd(locale, 'queue')}"
    notice = _notices.get(job["id"])
    if (notice is None or notice.closed) and not job.get("status_msg") and time.time() - float(job.get("created") or 0) < 10:
        return  # the confirmation message is about to be attached
    if notice is None or notice.closed:
        notice = progress.Notice(int(job["chat"]), job.get("locale", "es"), body, delay=0, msg_id=int(job.get("status_msg") or 0),
                                 on_shown=lambda msg_id: update(job, status_msg=msg_id), since=float(job.get("created") or 0))
        _notices[job["id"]] = notice
    notice.set(body, timer)


def _status_done(job: dict[str, Any]) -> None:
    notice = _notices.pop(job["id"], None)
    if notice is not None:
        notice.close()
    elif job.get("status_msg"):
        chat.delete_for_all([int(job["status_msg"])])
    if job.get("status_msg"):
        update(job, status_msg=0)


def _rate_line(job: dict[str, Any], done: int, total: int, sample: dict[str, float]) -> str:
    locale = job.get("locale", "es")
    now = time.monotonic()
    if not sample:
        sample.update(t=now, b=float(done), speed=0.0)
    elif now - sample["t"] >= 3:
        current = (done - sample["b"]) / (now - sample["t"])
        sample["speed"] = current if not sample["speed"] else sample["speed"] * 0.5 + current * 0.5
        sample.update(t=now, b=float(done))
    speed = f" · {human_size(int(sample['speed']))}/s" if sample["speed"] > 0 else ""
    if total:
        percent = min(100, int(done * 100 / total))
        left = ""
        if sample["speed"] > 0 and total > done:
            left = " · ~" + progress.elapsed_text((total - done) / sample["speed"])
        return tr(locale, "⬇️ Descargando", "⬇️ Downloading") + f" {percent}% · {human_size(done) if done else '0 B'}/{human_size(total)}{speed}{left}"
    return tr(locale, "⬇️ Descargando", "⬇️ Downloading") + f" {human_size(done) if done else '0 B'}{speed}"


# ---------------------------------------------------------------------------
# Preparation.
# ---------------------------------------------------------------------------
def _checkpoint(stop: Callable[[], bool] | None) -> None:
    if stop and stop():
        raise InterruptedError()


def _sha256(path: Path, stop: Callable[[], bool] | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            _checkpoint(stop)
            digest.update(block)
    _checkpoint(stop)
    return digest.hexdigest()


def _run_interruptible(command: list[str], timeout: int, stop: Callable[[], bool] | None = None,
                       cwd: str | None = None) -> subprocess.CompletedProcess:
    """Stop only this job's packer when the switch is activated; never purge other jobs."""
    if stop is None:
        return subprocess.run(command, cwd=cwd, capture_output=True, timeout=timeout)
    _checkpoint(stop)
    process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + timeout
    try:
        while True:
            _checkpoint(stop)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                out, err = process.communicate(timeout=min(0.5, remaining))
                _checkpoint(stop)
                return subprocess.CompletedProcess(command, process.returncode, out, err)
            except subprocess.TimeoutExpired:
                continue
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()


SEGMENT_FORMATS = {"mp4": "mp4", "m4a": "mp4", "webm": "webm", "mp3": "mp3", "opus": "ogg", "ogg": "ogg"}


def short_stem(name: str, limit: int = 40) -> str:
    """Short, export-friendly file stem: no brackets, no long titles."""
    stem = name.rsplit(".", 1)[0] if "." in name else name
    stem = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", stem)
    stem = re.sub(r"[_\s]+", " ", clean_filename(stem, "file")).strip(" -_")
    return stem[:limit].rstrip(" -_.") or "file"


def _probe_duration(path: Path) -> float:
    if not shutil.which("ffprobe"):
        return 0.0
    try:
        result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                                capture_output=True, text=True, timeout=60)
        return float(result.stdout.strip() or 0)
    except (subprocess.SubprocessError, ValueError):
        return 0.0


def _segments(named: Path, out: Path, job: dict[str, Any], part_bytes: int,
              stop: Callable[[], bool] | None = None) -> list[Path]:
    """Cut media into independently playable pieces under the part size (stream copy, no re-encode)."""
    ext = named.suffix.lstrip(".").lower()
    muxer = SEGMENT_FORMATS.get(ext)
    if not muxer or not shutil.which("ffmpeg"):
        return []
    _checkpoint(stop)
    duration = float(job.get("duration") or 0) or _probe_duration(named)
    _checkpoint(stop)
    if not duration:
        return []
    size = named.stat().st_size
    seconds = max(10.0, duration * part_bytes * 0.9 / size)
    for _attempt in range(4):
        _checkpoint(stop)
        for item in out.glob("seg*"):
            item.unlink()
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(named), "-map", "0", "-c", "copy",
                   "-f", "segment", "-segment_time", f"{seconds:.2f}", "-reset_timestamps", "1", "-segment_format", muxer]
        if muxer == "mp4":
            command += ["-segment_format_options", "movflags=+faststart"]
        result = _run_interruptible(command + [str(out / f"seg%03d.{ext}")], timeout=1800, stop=stop)
        pieces = sorted(out.glob(f"seg*.{ext}"))
        if result.returncode != 0 or not pieces:
            return []
        biggest = max(piece.stat().st_size for piece in pieces)
        if biggest <= part_bytes:
            stem = short_stem(job["name"])
            final = []
            for index, piece in enumerate(pieces, 1):
                _checkpoint(stop)
                target = out / f"{stem} ({index}-{len(pieces)}).{ext}"
                if not _tag_segment(piece, target, muxer, _marker(job["id"], index, len(pieces), f"{stem}.{ext}"),
                                    f"{stem} ({index}-{len(pieces)})", stop):
                    piece.replace(target)
                piece.unlink(missing_ok=True)
                final.append(target)
            return final
        seconds *= part_bytes / biggest * 0.9
    for item in out.glob("seg*"):
        item.unlink()
    return []


def _marker(job_id: Any, index: int, total: int, name: str) -> str:
    """Part number plus the original file name, so renamed copies can still be ordered and named."""
    return f"michiflin:{job_id}:{index}/{total}:{quote(name, safe='')[:150]}"


def _tag_segment(piece: Path, target: Path, muxer: str, marker: str, title: str,
                 stop: Callable[[], bool] | None = None) -> bool:
    """Embed the part number in the file itself: the join tool reads it back even after a phone renames the file."""
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(piece), "-map", "0", "-c", "copy",
               "-metadata", f"comment={marker}", "-metadata", f"title={title}"]
    if muxer == "mp4":
        command += ["-movflags", "+faststart"]
    if muxer == "mp3":
        command += ["-id3v2_version", "3"]
    try:
        result = _run_interruptible(command + ["-f", muxer, str(target)], timeout=600, stop=stop)
    except subprocess.SubprocessError:
        result = None
    if result is None or result.returncode != 0 or not target.exists() or target.stat().st_size < piece.stat().st_size * 0.9:
        target.unlink(missing_ok=True)
        return False
    return True


def _split(source: Path, job: dict[str, Any], stop: Callable[[], bool] | None = None) -> list[Path]:
    """Return the parts to send: playable segments for media, raw pieces or volumes for files."""
    _checkpoint(stop)
    part_bytes = int(job["part_mb"]) * MIB
    out = folder(job) / "parts"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    named = folder(job) / "named" / job["name"]
    named.parent.mkdir(parents=True, exist_ok=True)
    source.replace(named)
    if named.stat().st_size <= part_bytes:
        final = out / job["name"]
        named.replace(final)
        return [final]
    if job["kind"] in {"video", "audio"} or named.suffix.lstrip(".").lower() in SEGMENT_FORMATS:
        pieces = _segments(named, out, job, part_bytes, stop)
        if pieces:
            job["part_format"] = "segments"
            named.unlink(missing_ok=True)
            return pieces
        job["part_format"] = "raw"
    stem = short_stem(job["name"])
    ext = job["name"].rsplit(".", 1)[-1][:8] if "." in job["name"] else "bin"
    method = job["part_format"]
    seven = shutil.which("7z") or shutil.which("7zz")
    if method in {"7z", "zip"} and seven:
        short = named.with_name(f"{stem}.{ext}")
        named.replace(short)
        archive = out / f"{stem}.{method}"
        command = [seven, "a", f"-t{method}", "-mx0", f"-v{job['part_mb']}m", "-bd", "-y"]
        if method == "7z":
            command += ["-mtm=off", "-mtc=off", "-mta=off"]
        command += [str(archive), short.name]
        result = _run_interruptible(command, cwd=str(short.parent), timeout=3600, stop=stop)
        if result.returncode == 0:
            short.unlink(missing_ok=True)
            prefix = f"{stem}.{method}."
            return sorted(path for path in out.iterdir() if path.name.startswith(prefix))
        short.replace(named)
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True)
    job["part_format"] = "raw"
    parts: list[Path] = []
    with named.open("rb") as handle:
        index = 1
        while True:
            _checkpoint(stop)
            block = handle.read(part_bytes)
            if not block:
                break
            target = out / f"{stem}.{ext}.{index:03d}"
            target.write_bytes(block)
            parts.append(target)
            index += 1
    named.unlink(missing_ok=True)
    return parts


def _prepare(job: dict[str, Any], generation: int | None = None) -> None:
    stop = _stop_flags.setdefault(job["id"], threading.Event())
    owner = usage.job_owner(job)
    admitted = state.kill_generation(owner) if generation is None else generation
    interrupted = lambda: stop.is_set() or not access.job_allowed(job) or state.work_interrupted(owner, admitted)
    _checkpoint(interrupted)
    work = folder(job) / "src"
    shutil.rmtree(folder(job) / "parts", ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    update(job, status="downloading", progress=0, error="")
    sample: dict[str, float] = {}
    _status(job, tr(job.get("locale", "es"), "⬇️ Iniciando descarga…", "⬇️ Starting download…"))

    def progress(done: int, total: int = 0) -> None:
        job["progress"] = done
        if total and not job.get("size_est"):
            job["size_est"] = total
        _status(job, _rate_line(job, done, total or int(job.get("size_est") or 0), sample))

    if job["kind"] == "file":
        target = work / "download.bin"
        files.download(job["url"], target, int(job.get("size_est") or 0), bool(job.get("resumable")), progress,
                       interrupted, config.MAX_FILE_BYTES)
        _checkpoint(interrupted)
        with target.open("rb") as handle:
            head = handle.read(65536)
        hint = job["ext"] if job.get("ext") else ""
        kind = files.sniff(head, hint)
        if kind not in {"bin", "txt", "html"} and kind != hint and hint not in {"tar.gz", "xls", "ppt", "msi", "apks", "xapk"}:
            stem = job["name"].rsplit(".", 1)[0] if "." in job["name"] else job["name"]
            update(job, ext=kind, name=clean_filename(f"{stem}.{kind}"), mime=files.KINDS[kind][0], label=files.describe(kind, job.get("locale", "es")))
        source = target
    else:
        source = media.download(job["url"], work, job["kind"], int(job["quality"]), job.get("audio_format", "m4a"),
                                lambda done, total: progress(done, total), interrupted)
        _checkpoint(interrupted)
        ext = source.suffix.lstrip(".").lower() or job["ext"]
        mime = files.KINDS.get(ext, files.KINDS["bin"])[0]
        quality = f"{job['quality']}p" if job["kind"] == "video" else f"{job['quality']}k"
        update(job, ext=ext, mime=mime, name=clean_filename(f"{job['title'][:90]} [{quality}].{ext}", "media"))
    size = source.stat().st_size
    if size > config.MAX_FILE_BYTES:
        raise ValueError("too_large")
    digest = _sha256(source, interrupted)
    previous = job.get("sha")
    sent_before = int(job.get("sent") or 0)
    update(job, status="packing", size=size, progress=size)
    if size > int(job["part_mb"]) * MIB:
        _status(job, tr(job.get("locale", "es"), f"🗜️ Empaquetando {human_size(size)} en partes…", f"🗜️ Packing {human_size(size)} into parts…"))
    parts = _split(source, job, interrupted)
    _checkpoint(interrupted)
    if not parts:
        raise ValueError("pack_error")
    todo = job.get("todo") or []
    note = ""
    if previous and previous != digest and sent_before:
        todo = list(range(len(parts)))
        note = "changed"
        update(job, sent=0, messages=[])
    elif not todo or not previous:
        todo = [index for index in range(len(parts)) if index >= sent_before] if previous == digest else list(range(len(parts)))
    todo = [index for index in todo if index < len(parts)]
    if not todo and previous == digest and sent_before >= len(parts):
        # A restart may have occurred after the last delivered part was recorded.
        # Do not turn an empty todo list back into a full resend.
        update(job, status="done", done_at=time.time(), todo=[], sha=digest, parts=len(parts),
               part_files=[str(path) for path in parts])
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(folder(job) / "named", ignore_errors=True)
        return
    if not todo:
        todo = list(range(len(parts)))
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(folder(job) / "named", ignore_errors=True)
    next_status = "sending" if state.setting("auto_send") or job.get("send_requested") else "ready"
    part_sha = [_sha256(path, interrupted) for path in parts] if len(parts) > 1 else []
    _checkpoint(interrupted)
    update(job, sha=digest, parts=len(parts), part_files=[str(path) for path in parts], todo=todo, status=next_status, restart_note=note,
           part_sha=part_sha,
           part_sizes=[path.stat().st_size for path in parts] if len(parts) > 1 else [])
    if note:
        _notify(job, tr(job.get("locale", "es"), f"♻️ #{job['id']}: la fuente cambió tras el reinicio; se reenvía desde la parte 1.",
                        f"♻️ #{job['id']}: the source changed after restart; resending from part 1."))
    if next_status == "ready":
        _status(job, tr(job.get("locale", "es"), f"📦 Listo: {len(parts)} parte(s) · /enviar_{job['id']}", f"📦 Ready: {len(parts)} part(s) · /send_{job['id']}"), timer=False)
    if next_status == "ready" and state.setting("notify") == "normal":
        _notify(job, tr(job.get("locale", "es"), f"📦 #{job['id']} listo: {len(parts)} partes. /enviar_{job['id']}", f"📦 #{job['id']} ready: {len(parts)} parts. /send_{job['id']}"))


def _blocked(job: dict[str, Any]) -> bool:
    """Globally paused queue, kill switch, or downloads of one person paused by an administrator."""
    owner = usage.job_owner(job)
    return (not access.job_allowed(job) or bool(state.flag("queue_paused"))
            or state.get_kill_switch(owner) or owner in (state.flag("limited") or []))


def _next_queued() -> dict[str, Any] | None:
    """Claim the next queued job fairly: people with fewer running downloads and less delivered today go first."""
    with _claim, state.lock:
        busy: dict[str, int] = {}
        for job in jobs().values():
            if job["status"] in {"downloading", "packing"}:
                busy[usage.job_owner(job)] = busy.get(usage.job_owner(job), 0) + 1
        pending = [job for job in jobs().values() if job["status"] == "queued" and not job.get("hold") and not _blocked(job)]
        if not pending:
            return None
        job = min(pending, key=lambda item: usage.priority(item, busy))
        _claim_generations[str(job["id"])] = state.kill_generation(usage.job_owner(job))
        job["status"] = "downloading"
        return job


def _preparer() -> None:
    while True:
        _wake.wait(5)
        _wake.clear()
        while True:
            job = _next_queued()
            if job is None:
                break
            owner = usage.job_owner(job)
            with _claim:
                generation = _claim_generations.pop(str(job["id"]), state.kill_generation(owner))
            try:
                with state.person(job.get("addr", "")):
                    _prepare(job, generation)
            except InterruptedError:
                _interrupted_preparation(job)
            except Exception as exc:
                if state.work_interrupted(owner, generation) or job.get("status") == "cancelled":
                    _interrupted_preparation(job)
                    continue
                code = str(exc) if str(exc) in {"too_large", "disk_full", "network", "bot_check", "private", "unavailable", "geo", "media_error", "pack_error", "vault", "isolation_unavailable"} else exc.__class__.__name__
                log.warning("job %s failed: %s", job["id"], exc)
                update(job, status="failed", error=code)
                shutil.rmtree(folder(job), ignore_errors=True)
                _status_done(job)
                progress.finish_job(job.get("source_msg", 0), "failed")
                _notify(job, tr(job.get("locale", "es"), f"❌ #{job['id']} {job['title'][:60]}: {_error_text(code, 'es')}",
                                f"❌ #{job['id']} {job['title'][:60]}: {_error_text(code, 'en')}") + "\n" + names.cmd(job.get("locale", "es"), "retry", job["id"]))
            _wake.set()


def _interrupted_preparation(job: dict[str, Any]) -> None:
    if job.get("status") == "cancelled":
        shutil.rmtree(folder(job), ignore_errors=True)
        _status_done(job)
        return
    # Keep a resumable partial download; discard only incomplete packaging of this job.
    shutil.rmtree(folder(job) / "parts", ignore_errors=True)
    shutil.rmtree(folder(job) / "named", ignore_errors=True)
    update(job, status="queued", progress=0, error="")
    _status(job, tr(job.get("locale", "es"), "⏸️ Descarga pausada por el kill switch.",
                    "⏸️ Download paused by the kill switch."), timer=False)
    _wake.set()


def _error_text(code: str, locale: str) -> str:
    if code in {"bot_check", "private", "unavailable", "geo", "media_error", "too_large", "disk_full", "vault", "isolation_unavailable"}:
        return media.describe_error(code, locale).removeprefix("⚠️ ")
    if code.startswith("http_"):
        return tr(locale, f"el servidor respondió {code[5:]}", f"the server answered {code[5:]}")
    if code == "pack_error":
        return tr(locale, "no se pudo dividir en partes", "splitting into parts failed")
    if code == "network":
        return tr(locale, "la conexión falló repetidamente", "the connection failed repeatedly")
    return tr(locale, f"error interno ({code})", f"internal error ({code})")


# ---------------------------------------------------------------------------
# Delivery.
# ---------------------------------------------------------------------------
def in_window(now: float | None = None, job: dict[str, Any] | None = None) -> bool:
    """Inside the delivery window of the job's owner (or of the acting person)."""
    if job is not None:
        with state.person(job.get("addr", "")):
            return in_window(now)
    window = str(state.setting("window") or "").strip()
    if not window or "-" not in window:
        return True
    try:
        start, end = [int(part.split(":")[0]) * 60 + int((part.split(":") + ["0"])[1]) for part in window.split("-", 1)]
    except ValueError:
        return True
    current = local_time(now or time.time(), "%H:%M")
    minutes = int(current[:2]) * 60 + int(current[3:])
    return start <= minutes < end if start <= end else minutes >= start or minutes < end


def _caption(job: dict[str, Any], index: int) -> str:
    locale = job.get("locale", "es")
    total = int(job["parts"])
    if total == 1:
        return f"📦 {job['name']}\n{job.get('label', '')} · {human_size(job['size'])}"
    method = job.get("part_format")
    title = job.get("title") or job["name"]
    if method == "segments":
        icon = "🎧" if job["kind"] == "audio" else "🎬"
        lines = [f"{icon} {title[:100]} · {index + 1}/{total}"]
        if index == 0:
            lines.append(tr(locale, f"{human_size(job['size'])} en {total} partes. Cada parte se reproduce sola, en orden.",
                            f"{human_size(job['size'])} in {total} parts. Each part plays on its own, in order."))
        if index == total - 1:
            lines.append(tr(locale, "✅ Última parte.", "✅ Last part."))
        return "\n".join(lines)
    lines = [f"📦 {job['name']} · {index + 1}/{total}"]
    if index == 0:
        joiner = names.cmd(locale, "app")
        if method == "raw":
            how = tr(locale, f"Para unir: abre la .001 con 7-Zip o ZArchiver, o usa la herramienta Unir de {joiner}.",
                     f"To join: open the .001 with 7-Zip or ZArchiver, or use the Join tool in {joiner}.")
        else:
            how = tr(locale, f"Guarda todas las partes juntas y abre la .{method}.001 con ZArchiver o 7-Zip.",
                     f"Keep all parts together and open the .{method}.001 with ZArchiver or 7-Zip.")
        lines.append(f"{job.get('label', '')} · {human_size(job['size'])} · {how}")
    if index == total - 1:
        lines.append(tr(locale, f"✅ Última parte. SHA-256 del original: {job['sha'][:16]}…", f"✅ Last part. Original SHA-256: {job['sha'][:16]}…"))
    return "\n".join(lines)


def _is_webxdc(job: dict[str, Any]) -> bool:
    paths = job.get("part_files") or []
    try:
        with zipfile.ZipFile(paths[0]) as archive:
            return "index.html" in archive.namelist()
    except (OSError, IndexError, zipfile.BadZipFile):
        return False


def _viewtype(job: dict[str, Any]) -> str:
    if int(job["parts"]) != 1 and job.get("part_format") != "segments":
        return "File"
    ext = str(job.get("ext") or job["name"].rsplit(".", 1)[-1]).lower()
    if ext == "xdc" and _is_webxdc(job):
        return "Webxdc"  # arrives as a mini app that opens right in the chat, with its own name and icon
    if job["kind"] == "audio" or str(job.get("mime", "")).startswith("audio/") or ext in {"m4a", "mp3", "opus", "ogg"}:
        return "Audio"
    if ext == "mp4" and (job["kind"] == "video" or job.get("part_format") == "segments"):
        return "Video"
    return "File"


def _notify(job: dict[str, Any], message: str) -> None:
    if access.job_allowed(job) and chat.ready():
        chat.text(int(job["chat"]), message)


def _deliverable() -> dict[str, Any] | None:
    """Next part to send, taking turns between people (one part each) instead of draining one job first."""
    now = time.time()
    with state.lock:
        candidates = [job for job in jobs().values() if job["status"] == "sending" and not job.get("hold")
                      and float(job.get("not_before") or 0) <= now and job.get("todo") and not _blocked(job)]
        candidates = [job for job in candidates if in_window(now, job)]
        if not candidates:
            return None
        job = min(candidates, key=usage.priority)
        _delivery_generations[str(job["id"])] = state.kill_generation(usage.job_owner(job))
        return job


def _size_rejection(error: str) -> bool:
    """Recognize permanent SMTP/provider errors about message size, not quota or transport failures."""
    text = str(error or "").lower()
    return any(marker in text for marker in (
        "message too large", "message size", "maximum message", "size limit", "size exceeds", "size too large",
        "maximum size", "exceeds the maximum size", "exceeds maximum size", "attachment too large", "message is too big",
        "message exceeds", "demasiado grande", "tamaño máximo", "tamaño excede", "5.3.4",
    ))


def _retry_with_smaller_parts(job: dict[str, Any], error: str) -> bool:
    """Redownload and repack once after a clear size rejection; never duplicate delivered pieces."""
    current = int(job.get("part_mb") or 0)
    attempts = int(job.get("size_retry_count") or 0)
    if not _size_rejection(error) or current <= 1 or int(job.get("sent") or 0) or attempts >= 10:
        return False
    next_size = max(1, current * 3 // 4)
    if next_size >= current:
        return False
    update(job, part_mb=next_size, size_retry_count=attempts + 1, status="queued", hold=False,
           send_requested=True, not_before=0, parts=0, todo=[], part_files=[], part_sha=[], part_sizes=[],
           sha="", sent=0, messages=[], retries=0, error="message_too_large")
    locale = job.get("locale", "es")
    notice = tr(locale, f"📨 El proveedor rechazó el tamaño del mensaje. Reintentando con partes de {next_size} MB.",
                f"📨 The provider rejected the message size. Retrying with {next_size} MB parts.")
    _status(job, notice, timer=False)
    _notify(job, notice)
    _wake.set()
    return True


def _send_one(job: dict[str, Any]) -> None:
    owner = usage.job_owner(job)
    with state.lock:
        generation = _delivery_generations.pop(str(job["id"]), state.kill_generation(owner))
        if _blocked(job) or state.work_interrupted(owner, generation) or job.get("hold"):
            return
    inflight = job.get("inflight") if isinstance(job.get("inflight"), dict) else None
    index = int(inflight["index"]) if inflight else int(job["todo"][0])
    locale = job.get("locale", "es")
    delivery = mail_profiles.effective_settings(str(job.get("addr") or ""), {
        "part_mb": int(job.get("part_mb", state.setting("part_mb"))),
        "batch": int(job.get("delivery_batch", state.setting("batch"))),
        "delay": int(job.get("delivery_delay", state.setting("delay"))),
        "daily_cap_mb": int(job.get("delivery_daily_cap_mb", state.setting("daily_cap_mb"))),
    })
    if not inflight and job.get("delivery_delay") != delivery["delay"]:
        # A newly tightened profile also applies to queued work; wait from now if the previous send time is unknown.
        wait_until = max(float(job.get("not_before") or 0), time.time() + int(delivery["delay"]))
        update(job, delivery_delay=delivery["delay"], not_before=wait_until)
        return
    if inflight:
        size = int(inflight.get("size") or 0)
        msg_id = int(inflight.get("msg_id") or 0)
        if not msg_id:
            update(job, hold=True, error="delivery_unknown")
            _status(job, tr(locale, "⚠️ Entrega incierta: revisa el chat antes de repetir para evitar duplicados.",
                            "⚠️ Delivery uncertain: check the chat before retrying to avoid duplicates."), timer=False)
            return
    else:
        paths = job.get("part_files") or []
        path = Path(paths[index]) if index < len(paths) else None
        if path is None or not path.exists():
            update(job, status="queued")
            _wake.set()
            return
        size = path.stat().st_size
        limit = mail_profiles.part_limit_bytes(str(job.get("addr") or ""))
        if limit is not None and size > limit:
            already_blocked = bool(job.get("profile_blocked"))
            update(job, hold=True, profile_blocked=True)
            if not already_blocked:
                limit_mb = limit // MIB
                notice = tr(locale, f"⚠️ Perfil verificado: esta parte supera el límite seguro de {limit_mb} MB. No se enviará. Cancela y vuelve a crear el trabajo con partes menores.",
                            f"⚠️ Verified profile: this part exceeds the safe {limit_mb} MB limit. It will not be sent. Cancel and recreate the job with smaller parts.")
                _status(job, notice, timer=False)
                _notify(job, notice)
            return
        cap = int(delivery["daily_cap_mb"]) * MIB
        if cap and usage.sent_today(usage.job_owner(job)) + size > cap:
            update(job, not_before=time.time() + 1800)
            _status(job, tr(locale, "🕒 Tope diario de envío alcanzado; sigue más tarde.", "🕒 Daily send cap reached; continuing later."), timer=False)
            return
        total = int(job["parts"])
        label = f" {index + 1}/{total}" if total > 1 else ""
        _status(job, tr(locale, f"📤 Enviando{label} · {human_size(size)}", f"📤 Sending{label} · {human_size(size)}"))
        # Recheck immediately before handing a part to Delta Chat without holding
        # state.lock during the RPC. A persisted intent prevents blind resends.
        interrupted = lambda: (_blocked(job) or state.work_interrupted(owner, generation)
                               or job.get("hold") or job.get("status") != "sending")
        if interrupted():
            return

        def mark_handoff() -> bool:
            with state.lock:
                if interrupted():
                    return False
                job["inflight"] = {"index": index, "size": size, "msg_id": 0}
            state.save()
            return True

        msg_id = chat.send(int(job["chat"]), _caption(job, index), file=path, filename=path.name,
                           viewtype=_viewtype(job), interrupted=interrupted, before_handoff=mark_handoff)
        if not msg_id:
            if job.get("inflight"):
                update(job, hold=True, error="delivery_unknown")
                _status(job, tr(locale, "⚠️ Entrega incierta: revisa el chat antes de repetir para evitar duplicados.",
                                "⚠️ Delivery uncertain: check the chat before retrying to avoid duplicates."), timer=False)
            elif not interrupted():
                update(job, not_before=time.time() + 60)
            return
        update(job, inflight={"index": index, "size": size, "msg_id": msg_id})
    outcome = chat.wait_delivery(msg_id, 1200, _stop_flags.setdefault(job["id"], threading.Event()))
    if outcome == "delivered":
        state.record_sent(size, usage.job_owner(job))
        usage.note(job.get("addr", ""), bytes=size)
        with state.lock:
            job["todo"] = [item for item in job["todo"] if item != index]
            job["sent"] = int(job.get("sent") or 0) + 1
            job["messages"] = (job.get("messages") or []) + [msg_id]
            job["retries"] = 0
            job["inflight"] = None
            if not job["todo"]:
                job["status"] = "done"
                job["done_at"] = time.time()
        update(job)
        batch = int(delivery["batch"])
        if not job["todo"]:
            _status_done(job)
            progress.finish_job(job.get("source_msg", 0), "done")
            return
        if batch and int(job["sent"]) % batch == 0:
            update(job, hold=True)
            _status(job, tr(locale, f"⏸️ {job['sent']}/{job['parts']} enviadas · /seguir_{job['id']}", f"⏸️ {job['sent']}/{job['parts']} sent · /resume_{job['id']}"), timer=False)
            _notify(job, tr(job.get("locale", "es"), f"⏸️ #{job['id']}: {job['sent']}/{job['parts']} enviadas. Continúa con /seguir_{job['id']}",
                            f"⏸️ #{job['id']}: {job['sent']}/{job['parts']} sent. Continue with /resume_{job['id']}"))
            return
        update(job, not_before=time.time() + int(delivery["delay"]))
        _status(job, tr(locale, f"📤 {job['sent']}/{job['parts']} partes entregadas", f"📤 {job['sent']}/{job['parts']} parts delivered"))
        return
    if outcome == "stopped":
        return
    if outcome in {"timeout", "missing"}:
        # The message may still be delivered. Keep its id and require review;
        # resuming only checks that same id, rather than sending another copy.
        update(job, hold=True, error="delivery_unknown")
        _status(job, tr(locale, "⚠️ Sin confirmación: revisa el chat antes de repetir para evitar duplicados.",
                        "⚠️ No confirmation: check the chat before retrying to avoid duplicates."), timer=False)
        return
    delivery_error = chat.message_error(msg_id) if outcome == "failed" else ""
    update(job, inflight=None)
    chat.delete([msg_id])
    if outcome == "failed" and _retry_with_smaller_parts(job, delivery_error):
        return
    retries = int(job.get("retries") or 0) + 1
    if retries > 3 or outcome == "timeout":
        update(job, hold=True, retries=0)
        _status(job, tr(locale, f"⚠️ Pausado en la parte {index + 1} · /seguir_{job['id']}", f"⚠️ Paused at part {index + 1} · /resume_{job['id']}"), timer=False)
        reason = tr(job.get("locale", "es"), "sin confirmación del servidor", "no server confirmation") if outcome == "timeout" else tr(job.get("locale", "es"), "el envío falló 3 veces", "sending failed 3 times")
        _notify(job, tr(job.get("locale", "es"), f"⚠️ #{job['id']} parte {index + 1}: {reason}. Pausado; /seguir_{job['id']} para reintentar.",
                        f"⚠️ #{job['id']} part {index + 1}: {reason}. Paused; /resume_{job['id']} to retry."))
        return
    update(job, retries=retries, not_before=time.time() + 60 * retries)


def _sender() -> None:
    while True:
        time.sleep(3)
        if not chat.ready():
            continue
        job = _deliverable()
        if job is None:
            _window_notices()
            continue
        try:
            with state.person(job.get("addr", "")):
                _send_one(job)
        except Exception as exc:
            log.warning("delivery error on #%s: %s", job.get("id"), exc)
            update(job, not_before=time.time() + 120)


def _window_notices() -> None:
    with state.lock:
        waiting = [job for job in jobs().values() if job["status"] == "sending" and not job.get("hold")]
    for job in waiting:
        if _blocked(job) or in_window(job=job):
            continue
        with state.person(job.get("addr", "")):
            window = state.setting("window")
            _status(job, tr(job.get("locale", "es"), f"🕒 Esperando la ventana de envío ({window})",
                            f"🕒 Waiting for the send window ({window})"), timer=False)


def _janitor() -> None:
    while True:
        time.sleep(600)
        try:
            cleanup()
        except Exception as exc:
            log.warning("cleanup error: %s", exc)


def cleanup() -> None:
    ttl = int(state.setting("ttl_hours")) * 3600
    now = time.time()
    with state.lock:
        expired = [job for job in jobs().values() if access.job_allowed(job)
                   and job["status"] in FINAL and now - float(job.get("updated") or 0) > ttl]
        for job in expired:
            jobs().pop(job["id"], None)
    for job in expired:
        shutil.rmtree(folder(job), ignore_errors=True)
    # Delivered files are kept for re-sends until disk gets tight.
    if free_disk() < config.MIN_FREE_DISK_BYTES * 2:
        with state.lock:
            done = sorted((job for job in jobs().values() if job["status"] == "done"
                           and access.job_allowed(job)), key=lambda item: float(item.get("done_at") or 0))
        for job in done:
            shutil.rmtree(folder(job), ignore_errors=True)
            if free_disk() >= config.MIN_FREE_DISK_BYTES * 2:
                break
    if expired:
        state.save()


def recover() -> None:
    """After a restart, files in temporary storage are gone: re-prepare unfinished jobs."""
    stale = []
    with state.lock:
        for job in jobs().values():
            if not access.job_allowed(job):
                continue  # preserve historical non-owner jobs without starting or cleaning them
            if job.get("status_msg"):
                stale.append(int(job["status_msg"]))
                job["status_msg"] = 0
            if job["status"] in ACTIVE:
                paths = job.get("part_files") or []
                if job.get("inflight"):
                    # Reconcile the already handed-off message before any re-download.
                    job["status"] = "sending"
                    job["not_before"] = 0
                elif job["status"] == "sending" and job.get("parts") and not job.get("todo"):
                    job["status"] = "done"
                    job["done_at"] = time.time()
                elif job["status"] in {"downloading", "packing"} or not paths or not all(Path(path).exists() for path in paths):
                    job["status"] = "queued"
                    job["progress"] = 0
                    if job.get("parts"):
                        job["send_requested"] = True
    state.save()
    if stale and chat.ready():
        chat.delete_for_all(stale)


def start() -> None:
    global _started
    if _started:
        return
    _started = True
    config.JOB_ROOT.mkdir(parents=True, exist_ok=True)
    recover()
    workers = [(_preparer, f"preparer-{index + 1}") for index in range(PREPARERS)]
    for target, name in (*workers, (_sender, "sender"), (_janitor, "janitor")):
        threading.Thread(target=target, name=name, daemon=True).start()
    _wake.set()


# ---------------------------------------------------------------------------
# Owner operations.
# ---------------------------------------------------------------------------
def status_label(job: dict[str, Any], locale: str) -> str:
    pair = STATUS_LABELS.get(job["status"], (job["status"], job["status"]))
    label = pair[1] if locale == "en" else pair[0]
    if job.get("hold") and job["status"] not in FINAL:
        label += tr(locale, " · ⏸️ pausado", " · ⏸️ paused")
    return label


def describe_job(job: dict[str, Any], locale: str, detailed: bool = False) -> str:
    size = job.get("size") or job.get("size_est") or 0
    head = f"#{job['id']} {job.get('name') or job['title']}"[:110]
    lines = [head, f"   {status_label(job, locale)} · {human_size(size)}"]
    if job["status"] == "downloading" and size:
        lines[-1] += f" · {min(100, int(job.get('progress', 0) * 100 / max(1, size)))}%"
    if job.get("parts"):
        delivered = int(job["parts"]) - len(job.get("todo") or []) if job["status"] in {"sending", "done", "ready"} else int(job.get("sent") or 0)
        lines[-1] += f" · {delivered}/{job['parts']} " + tr(locale, "partes", "parts")
    if float(job.get("not_before") or 0) > time.time() + 60:
        lines.append("   🕒 " + local_time(float(job["not_before"]), "%d/%m %H:%M"))
    if job["status"] == "failed" and job.get("error"):
        lines.append(f"   {_error_text(job['error'], locale)}")
    ops = []
    if job["status"] in ACTIVE:
        ops.append(names.cmd(locale, "resume" if job.get("hold") or job["status"] == "ready" else "pause", job["id"]))
        ops.append(names.cmd(locale, "cancel", job["id"]))
    elif job["status"] == "failed":
        ops.append(names.cmd(locale, "retry", job["id"]))
    elif job["status"] == "done" and detailed:
        ops.append(names.cmd(locale, "resend", job["id"], 1))
    if detailed:
        if job.get("kind") != "file" and job.get("duration"):
            lines.append(f"   ⏱️ {duration_text(job['duration'])}")
        if job.get("sha"):
            lines.append(f"   SHA-256 {job['sha'][:24]}…")
    if ops:
        lines.append("   " + " · ".join(ops))
    return "\n".join(lines)


def list_text(chat_id: int, locale: str) -> str:
    with state.lock:
        mine = sorted((job for job in jobs().values() if int(job["chat"]) == chat_id
                       and access.job_allowed(job)), key=lambda item: int(item["id"]))
    if not mine:
        return tr(locale, "📭 La cola está vacía.", "📭 The queue is empty.")
    active = [job for job in mine if job["status"] not in FINAL]
    finished = [job for job in mine if job["status"] in FINAL][-5:]
    used = queue_bytes()
    used_text = human_size(used) if used else "0 B"
    lines = [tr(locale, f"📥 Cola · {len(active)} activas · {used_text} en disco", f"📥 Queue · {len(active)} active · {used_text} on disk")]
    window = state.setting("window")
    if window:
        lines.append(tr(locale, f"Ventana de envío: {window}" + ("" if in_window() else " (fuera de horario)"), f"Send window: {window}" + ("" if in_window() else " (outside)")))
    lines.append("")
    lines += [describe_job(job, locale) for job in active + finished]
    if finished:
        lines += ["", names.cmd(locale, "queue", "clear")]
    return "\n".join(lines)


def hold(job_id: str, value: bool, locale: str) -> str:
    job = get(job_id)
    if not job or job["status"] in FINAL:
        return tr(locale, "⚠️ Trabajo no encontrado o terminado.", "⚠️ Job not found or finished.")
    if not value:
        inflight = job.get("inflight")
        if isinstance(inflight, dict) and not inflight.get("msg_id"):
            return tr(locale, "⚠️ Entrega incierta: revisa el chat. Para evitar duplicados no se reenviará automáticamente; cancela y crea otro trabajo solo si es necesario.",
                      "⚠️ Delivery uncertain: check the chat. To avoid duplicates it will not be resent automatically; cancel and create another job only if needed.")
        if job.get("profile_blocked"):
            paths = job.get("part_files") or []
            index = int((job.get("todo") or [0])[0])
            path = Path(paths[index]) if index < len(paths) else None
            limit = mail_profiles.part_limit_bytes(str(job.get("addr") or ""))
            if path and path.exists() and limit is not None and path.stat().st_size > limit:
                return tr(locale, "⚠️ El límite verificado sigue bloqueando esta parte. Cancela el trabajo y vuelve a encolarlo con partes menores.",
                          "⚠️ The verified limit still blocks this part. Cancel the job and requeue it with smaller parts.")
        status = "sending" if job["status"] == "ready" else job["status"]
        update(job, hold=False, profile_blocked=False, status=status, not_before=0, send_requested=True)
        _wake.set()
        return tr(locale, f"▶️ #{job['id']} continúa.", f"▶️ #{job['id']} resumed.")
    update(job, hold=True)
    _status(job, tr(locale, f"⏸️ Pausado · /seguir_{job['id']}", f"⏸️ Paused · /resume_{job['id']}"), timer=False)
    return tr(locale, f"⏸️ #{job['id']} pausado.", f"⏸️ #{job['id']} paused.")


def cancel(job_id: str, locale: str) -> str:
    job = get(job_id)
    if not job or job["status"] in FINAL:
        return tr(locale, "⚠️ Trabajo no encontrado o terminado.", "⚠️ Job not found or finished.")
    _stop_flags.setdefault(job["id"], threading.Event()).set()
    update(job, status="cancelled", hold=False)
    shutil.rmtree(folder(job), ignore_errors=True)
    _status_done(job)
    progress.finish_job(job.get("source_msg", 0), "")
    return tr(locale, f"🚫 #{job['id']} cancelado.", f"🚫 #{job['id']} cancelled.")


def retry(job_id: str, locale: str) -> str:
    job = get(job_id)
    if not job or job["status"] not in {"failed", "cancelled"}:
        return tr(locale, "⚠️ Solo se reintentan trabajos fallidos o cancelados.", "⚠️ Only failed or cancelled jobs can be retried.")
    _stop_flags.pop(job["id"], None)
    update(job, status="sending" if job.get("inflight") else "queued", error="", hold=False, not_before=0)
    progress.finish_job(job.get("source_msg", 0), "working")
    _wake.set()
    return tr(locale, f"🔁 #{job['id']} en cola de nuevo.", f"🔁 #{job['id']} queued again.")


def resend(job_id: str, part: int, locale: str) -> str:
    job = get(job_id)
    if not job or not job.get("parts"):
        return tr(locale, "⚠️ Trabajo no encontrado.", "⚠️ Job not found.")
    if not 1 <= part <= int(job["parts"]):
        return tr(locale, f"⚠️ Parte fuera de rango (1–{job['parts']}).", f"⚠️ Part out of range (1–{job['parts']}).")
    todo = sorted(set((job.get("todo") or []) + [part - 1]))
    paths = job.get("part_files") or []
    missing = not paths or not Path(paths[part - 1]).exists()
    update(job, todo=todo, status="queued" if missing else "sending", hold=False, not_before=0, send_requested=True)
    _wake.set()
    return tr(locale, f"🔁 #{job['id']}: reenviando parte {part}.", f"🔁 #{job['id']}: resending part {part}.")


def schedule(job_id: str, when: str, locale: str) -> str:
    job = get(job_id)
    if not job or job["status"] in FINAL:
        return tr(locale, "⚠️ Trabajo no encontrado o terminado.", "⚠️ Job not found or finished.")
    from datetime import datetime, timedelta

    try:
        hour, minute = [int(item) for item in when.replace(".", ":").split(":")[:2]]
        now = datetime.now(config.LOCAL_ZONE)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
    except (ValueError, IndexError):
        return tr(locale, "Uso: /programar_ID HH:MM", "Usage: /schedule_ID HH:MM")
    status = "sending" if job["status"] == "ready" else job["status"]
    update(job, not_before=target.timestamp(), status=status, send_requested=True, hold=False)
    return tr(locale, f"🕒 #{job['id']} se enviará desde {target.strftime('%d/%m %H:%M')}.", f"🕒 #{job['id']} will send from {target.strftime('%d/%m %H:%M')}.")


def clear_finished(chat_id: int, locale: str) -> str:
    with state.lock:
        finished = [job for job in jobs().values() if int(job["chat"]) == chat_id and job["status"] in FINAL
                    and access.job_allowed(job)]
        for job in finished:
            jobs().pop(job["id"], None)
    for job in finished:
        shutil.rmtree(folder(job), ignore_errors=True)
    state.save()
    return tr(locale, f"🧹 {len(finished)} trabajos terminados eliminados.", f"🧹 {len(finished)} finished jobs removed.")


def wake() -> None:
    _wake.set()


def active_count() -> int:
    with state.lock:
        return sum(1 for job in jobs().values() if job["status"] in ACTIVE and access.job_allowed(job))
