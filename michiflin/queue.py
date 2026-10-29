"""Download queue: prepare (download, verify, split) and deliver parts one by one.

Delivery waits for each part to reach the relay before sending the next, honors the
send window, batch size, delay and optional daily cap, and survives restarts by
re-downloading and resuming when the source hash is unchanged.
"""
from __future__ import annotations

import hashlib
import logging
import os
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
    """A user-facing job id must belong to the current authenticated sender."""
    sender = (chat.sender() or state.request_sender() or "").strip().lower()
    with state.lock:
        job = jobs().get(str(job_id).lstrip("#"))
        owner = str(job.get("addr") or "").strip().lower() if job else ""
        return job if job and sender and owner == sender and access.job_allowed(job) else None


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


def enqueue_media(chat_id: int, info: media.MediaInfo, kind: str, quality: int, locale: str, audio_format: str = "", video_format: str = "") -> tuple[dict[str, Any] | None, str]:
    sizes = info.audio_sizes if kind == "audio" else info.video_sizes
    estimate = sizes.get(quality) or (max(sizes.values()) if sizes else 0)
    if kind == "video" and sizes and quality not in sizes:
        fitting = [height for height in sizes if height <= quality]
        estimate = sizes[max(fitting)] if fitting else min(sizes.values())
    problem = _capacity_error(estimate, locale)
    if problem:
        return None, problem
    fmt = (audio_format or state.setting("audio_format")) if kind == "audio" else (video_format or "mp4")
    if ((kind == "audio" and fmt not in {"mp3", "m4a", "opus"}) or
            (kind == "video" and fmt not in {"mp4", "mkv", "webm"})):
        return None, tr(locale, "⚠️ Formato de salida no válido.", "⚠️ Invalid output format.")
    job = _new_job(chat_id, kind, info.webpage or info.url, info.title, estimate, quality=quality, audio_format=fmt,
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


def _copy_source(source: Path, target: Path, stop: Callable[[], bool] | None) -> None:
    """Copy without moving the original, checking maintenance between blocks."""
    with source.open("rb") as incoming, target.open("wb") as outgoing:
        while True:
            _checkpoint(stop)
            block = incoming.read(1024 * 1024)
            if not block:
                break
            outgoing.write(block)
    _checkpoint(stop)


def _split(source: Path, job: dict[str, Any], stop: Callable[[], bool] | None = None) -> list[Path]:
    """Return the parts to send: playable segments for media, raw pieces or volumes for files."""
    _checkpoint(stop)
    part_bytes = int(job["part_mb"]) * MIB
    out = folder(job) / "parts"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    named = folder(job) / "named" / job["name"]
    named.parent.mkdir(parents=True, exist_ok=True)
    named.unlink(missing_ok=True)
    # Keep the completed source until *all* parts and their hashes are committed.
    # A hard link avoids doubling large files on the normal same-filesystem path.
    try:
        os.link(source, named)
    except OSError:
        _copy_source(source, named, stop)
    _checkpoint(stop)
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


def _retained_progress(job: dict[str, Any], work: Path) -> int:
    if work.is_symlink():
        return 0
    name = str(job.get("source_complete") or "")
    source = work / name if name and Path(name).name == name else None
    if source and not source.is_symlink() and source.is_file() and source.stat().st_size == int(job.get("source_complete_size") or -1):
        return source.stat().st_size
    partial = work / "download.bin"
    if job["kind"] == "file" and job.get("resumable") and not partial.is_symlink() and partial.is_file():
        return partial.stat().st_size
    if job["kind"] in {"audio", "video"}:
        sizes = []
        for attempt in work.glob(".resume-*"):
            if not attempt.is_symlink() and attempt.is_dir():
                sizes.append(sum(path.stat().st_size for path in attempt.glob("media.*")
                                 if not path.is_symlink() and path.is_file() and not path.name.endswith((".ytdl", ".temp"))))
        return max(sizes, default=0)
    return 0


def _prepare(job: dict[str, Any], generation: int | None = None) -> None:
    stop = _stop_flags.setdefault(job["id"], threading.Event())
    owner = usage.job_owner(job)
    admitted = state.kill_generation(owner) if generation is None else generation
    interrupted = lambda: stop.is_set() or not access.job_allowed(job) or state.work_interrupted(owner, admitted)
    _checkpoint(interrupted)
    # Files may have disappeared after startup (for example on an ephemeral
    # runner). Protect any surviving packing artifacts before clearing them.
    try:
        preserved = _recover_packing_source(job)
    except (OSError, TypeError, ValueError):
        preserved = False
    if not preserved:
        update(job, status="queued", hold=True, error="source_preservation")
        return
    work = folder(job) / "src"
    work.mkdir(parents=True, exist_ok=True)
    # _split clears reproducible packing artifacts only after the completed
    # source has been read and checked; a failed re-download keeps old bytes.
    name = str(job.get("source_complete") or "")
    candidate = work / name if name and Path(name).name == name else None
    if candidate and (not candidate.is_file() or candidate.stat().st_size != int(job.get("source_complete_size") or -1)):
        candidate = None
    retained = _retained_progress(job, work)
    values: dict[str, Any] = {"status": "downloading", "progress": retained, "error": ""}
    if int(job.get("progress") or 0) > retained:
        values["recovery_note"] = "progress_lost"
    update(job, **values)
    sample: dict[str, float] = {}
    _status(job, tr(job.get("locale", "es"), "⬇️ Iniciando descarga…", "⬇️ Starting download…"))

    def progress(done: int, total: int = 0) -> None:
        job["progress"] = done
        if total and not job.get("size_est"):
            job["size_est"] = total
        _status(job, _rate_line(job, done, total or int(job.get("size_est") or 0), sample))

    def integrity(note: str) -> None:
        if note == "range_unverified" and not job.get("range_unverified"):
            update(job, range_unverified=True)

    if job["kind"] == "file":
        source = candidate or (work / "download.bin")
        if source.is_symlink() or (source.exists() and not source.is_file()):
            update(job, status="queued", hold=True, error="source_preservation")
            return
        if candidate is None:
            if not retained and job.get("range_unverified"):
                update(job, range_unverified=False)
            files.download(job["url"], source, int(job.get("size_est") or 0), bool(job.get("resumable")), progress,
                           interrupted, config.MAX_FILE_BYTES, integrity)
            # Only a completed download gets this marker; partial range downloads
            # continue to use the existing resumable path instead.
            update(job, source_complete=source.name, source_complete_size=source.stat().st_size, source_complete_sha="")
        _checkpoint(interrupted)
        with source.open("rb") as handle:
            head = handle.read(65536)
        hint = job["ext"] if job.get("ext") else ""
        kind = files.sniff(head, hint)
        if kind not in {"bin", "txt", "html"} and kind != hint and hint not in {"tar.gz", "xls", "ppt", "msi", "apks", "xapk"}:
            stem = job["name"].rsplit(".", 1)[0] if "." in job["name"] else job["name"]
            update(job, ext=kind, name=clean_filename(f"{stem}.{kind}"), mime=files.KINDS[kind][0], label=files.describe(kind, job.get("locale", "es")))
    else:
        source = candidate
        if source is None:
            source = media.download(job["url"], work, job["kind"], int(job["quality"]), job.get("audio_format", "m4a"),
                                    lambda done, total: progress(done, total), interrupted)
            if source.parent != work:
                raise ValueError("media_error")
            update(job, source_complete=source.name, source_complete_size=source.stat().st_size, source_complete_sha="")
        _checkpoint(interrupted)
        ext = source.suffix.lstrip(".").lower() or job["ext"]
        mime = files.KINDS.get(ext, files.KINDS["bin"])[0]
        quality = f"{job['quality']}p" if job["kind"] == "video" else f"{job['quality']}k"
        update(job, ext=ext, mime=mime, name=clean_filename(f"{job['title'][:90]} [{quality}].{ext}", "media"))
    size = source.stat().st_size
    if size > config.MAX_FILE_BYTES:
        raise ValueError("too_large")
    digest = _sha256(source, interrupted)
    recorded_source_sha = str(job.get("source_complete_sha") or "")
    if recorded_source_sha and recorded_source_sha != digest:
        # A same-sized local copy can still be corrupt. Keep it and every old
        # artifact for review rather than replacing either copy.
        update(job, status="queued", hold=True, error="source_preservation")
        return
    previous = job.get("sha")
    sent_before = int(job.get("sent") or 0)
    update(job, status="packing", size=size, progress=size, source_complete_sha=digest)
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
               part_files=[str(path) for path in parts], source_complete="", source_complete_size=0, source_complete_sha="")
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(folder(job) / "named", ignore_errors=True)
        return
    if not todo:
        todo = list(range(len(parts)))
    next_status = "sending" if state.setting("auto_send") or job.get("send_requested") else "ready"
    part_sha = [_sha256(path, interrupted) for path in parts] if len(parts) > 1 else []
    _checkpoint(interrupted)
    update(job, sha=digest, parts=len(parts), part_files=[str(path) for path in parts], todo=todo, status=next_status, restart_note=note,
           part_sha=part_sha, source_complete="", source_complete_size=0, source_complete_sha="",
           part_sizes=[path.stat().st_size for path in parts] if len(parts) > 1 else [])
    # These are redundant links/copies only after the new parts were persisted.
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(folder(job) / "named", ignore_errors=True)
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
    return (not access.job_allowed(job) or state.maintenance_active() or bool(state.flag("queue_paused"))
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
                text = str(exc)
                known = {"too_large", "disk_full", "network", "source_preservation", "source_changed", "range_invalid", "bot_check", "private", "unavailable", "geo", "media_error", "pack_error", "vault", "isolation_unavailable", "proxy_unavailable", "proxy_invalid"}
                code = text if text in known or re.fullmatch(r"http_\d{3}", text) else exc.__class__.__name__
                # Provider exceptions can include signed URLs or credentials.
                log.warning("job %s failed: %s", job["id"], code)
                update(job, status="failed", error=code)
                # Keep reusable sources and partials for an explicit retry; normal
                # expiry or a user-requested cleanup still controls storage.
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
    # Preserve source *and* packing artifacts during the pause. The next
    # successful preparation clears reproducible artifacts only after it has
    # verified a complete source; otherwise they may be the last surviving copy.
    previous = int(job.get("progress") or 0)
    try:
        retained = _retained_progress(job, folder(job) / "src")
        values: dict[str, Any] = {"status": "queued", "progress": retained, "error": ""}
    except (OSError, TypeError, ValueError):
        retained = 0
        values = {"status": "queued", "progress": 0, "hold": True, "error": "source_preservation"}
    if previous > retained:
        values["recovery_note"] = "progress_lost"
    update(job, **values)
    _status(job, tr(job.get("locale", "es"), "⏸️ Trabajo pausado; continuará al terminar la pausa.",
                    "⏸️ Job paused; it will continue when the pause ends."), timer=False)
    _wake.set()


def _error_text(code: str, locale: str) -> str:
    if code in {"bot_check", "private", "unavailable", "geo", "media_error", "too_large", "disk_full", "vault", "isolation_unavailable", "proxy_unavailable", "proxy_invalid"}:
        return media.describe_error(code, locale).removeprefix("⚠️ ")
    if code.startswith("http_"):
        return tr(locale, f"el servidor respondió {code[5:]}", f"the server answered {code[5:]}")
    if code == "source_preservation":
        return tr(locale, "archivo anterior protegido; requiere revisión antes de reanudar",
                  "previous source protected; review is required before resuming")
    if code == "source_changed":
        return tr(locale, "el origen cambió o no aceptó la reanudación; se conservó el archivo parcial",
                  "the source changed or did not accept resumption; the partial file was retained")
    if code == "range_invalid":
        return tr(locale, "el servidor respondió con un rango incompatible; se conservó el archivo parcial",
                  "the server returned an incompatible range; the partial file was retained")
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
    cancel_stop = _stop_flags.setdefault(job["id"], threading.Event())
    class DeliveryStop:
        def is_set(self) -> bool:
            # Retain the existing message id for reconciliation after the pause;
            # stopping the poll never pretends to retract an RPC already handed off.
            return (cancel_stop.is_set() or _blocked(job) or job.get("hold")
                    or state.work_interrupted(owner, generation))
    outcome = chat.wait_delivery(msg_id, 1200, DeliveryStop())
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


def _recover_packing_source(job: dict[str, Any]) -> bool:
    """Protect a missing or legacy packing source before any artifact is removed.

    A stored marker is not proof that its file still exists. If the bytes are
    absent, recover one unambiguous complete local copy, or hold the job when
    surviving artifacts cannot be identified safely. No remote durability is
    implied; when no local copy exists the ordinary download must start again.
    """
    marker = str(job.get("source_complete") or "")
    base = folder(job)
    if base.is_symlink() or any((base / name).is_symlink() for name in ("src", "named", "parts")):
        return False
    if job.get("status") != "packing" and not marker:
        return True
    try:
        marked_size = int(job.get("source_complete_size") or 0)
        packed_size = int(job.get("size") or 0)
        expected = marked_size if marker else packed_size
    except (TypeError, ValueError):
        return False
    work = folder(job) / "src"
    if work.is_symlink() or (marker and (Path(marker).name != marker or marker in {".", ".."})):
        return False
    if marker:
        source = work / marker
        if source.is_symlink():
            return False
        if source.exists():
            # A wrong-sized marker may be a useful partial or a different file;
            # never overwrite it even if another candidate happens to exist.
            return (source.is_file() and marked_size > 0 and source.stat().st_size == marked_size
                    and (job.get("status") != "packing" or not packed_size or packed_size == marked_size))

    named = folder(job) / "named"
    parts = folder(job) / "parts"
    if named.is_symlink() or parts.is_symlink():
        return False
    named_items = list(named.iterdir()) if named.is_dir() else []
    part_items = list(parts.iterdir()) if parts.is_dir() else []
    if expected <= 0:
        # With no trustworthy complete size, do not guess which artifact is a source.
        return not marker and not named_items and not part_items

    if job.get("kind") == "file":
        source_items = [work / "download.bin"]
    else:
        source_items = [path for path in work.glob("media*")
                        if re.fullmatch(r"(?:media\.[a-zA-Z0-9]{1,8}|media-result-[0-9a-f]{16}\.[a-zA-Z0-9]{1,8})", path.name)]
    candidates = [path for path in source_items if not path.is_symlink() and path.is_file() and path.stat().st_size == expected]
    surviving = [path for path in source_items if path.exists() or path.is_symlink()]
    if len(candidates) > 1 or len(surviving) != len(candidates):
        return False  # do not overwrite an incomplete or competing source
    if candidates:
        job["source_complete"] = candidates[0].name
        job["source_complete_size"] = expected
        return True

    valid_named = [path for path in named_items if not path.is_symlink() and path.is_file() and path.stat().st_size == expected]
    if len(named_items) > 1:
        return False  # a second full or partial named file is ambiguous
    # A previous version may have moved a small, complete file into parts/.
    name = str(job.get("name") or "")
    whole = parts / name if name and Path(name).name == name else None
    valid_whole = (whole is not None and not whole.is_symlink() and whole.is_file()
                   and expected <= int(job.get("part_mb") or 0) * MIB and whole.stat().st_size == expected)
    if valid_named and valid_whole and not valid_named[0].samefile(whole):
        return False  # two different complete-looking copies: never guess
    old_source = valid_named[0] if valid_named else whole if valid_whole else None
    if old_source is None:
        if named_items or part_items or any(path.is_symlink() for path in source_items):
            return False  # incomplete or ambiguous artifacts remain for review
        if marker:
            job["source_complete"] = ""
            job["source_complete_size"] = 0
            job["source_complete_sha"] = ""
        return True  # no completed local source remains; re-download is necessary

    target_name = marker or ("download.bin" if job.get("kind") == "file" else "media" + (old_source.suffix or ".bin"))
    target = work / target_name
    try:
        work.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            return False  # never overwrite a different surviving local file
        os.link(old_source, target)  # preserve the old copy until packing succeeds
    except OSError:
        return False
    job["source_complete"] = target.name
    job["source_complete_size"] = expected
    return True


def recover() -> None:
    """After a restart, preserve local sources and re-prepare unfinished jobs."""
    stale = []
    with state.lock:
        for job in jobs().values():
            if not access.job_allowed(job):
                continue  # preserve historical non-owner jobs without starting or cleaning them
            if job.get("status_msg"):
                stale.append(int(job["status_msg"]))
                job["status_msg"] = 0
            if job["status"] in ACTIVE:
                try:
                    preserved = _recover_packing_source(job)
                except (OSError, TypeError, ValueError):
                    preserved = False
                if not preserved:
                    job["hold"] = True
                    job["error"] = "source_preservation"
                    log.warning("job %s paused to preserve an old packaging source", job.get("id", "?"))
                paths = job.get("part_files") or []
                if job.get("inflight"):
                    # Reconcile the already handed-off message before any re-download.
                    job["status"] = "sending"
                    job["not_before"] = 0
                elif job["status"] == "sending" and job.get("parts") and not job.get("todo"):
                    job["status"] = "done"
                    job["done_at"] = time.time()
                elif job["status"] in {"downloading", "packing"} or not paths or not all(Path(path).exists() for path in paths):
                    previous = int(job.get("progress") or 0)
                    try:
                        retained = _retained_progress(job, folder(job) / "src")
                    except (OSError, TypeError, ValueError):
                        retained = 0
                        job["hold"] = True
                        job["error"] = "source_preservation"
                    job["status"] = "queued"
                    job["progress"] = retained
                    if previous > retained:
                        # On ephemeral runners state may outlive the actual bytes.
                        # Show the loss rather than claiming the old progress survived.
                        job["recovery_note"] = "progress_lost"
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
    if state.maintenance_active() and job["status"] not in FINAL:
        label += tr(locale, " · 🛠️ mantenimiento", " · 🛠️ maintenance")
    if job.get("hold") and job["status"] not in FINAL:
        label += tr(locale, " · ⏸️ pausa manual", " · ⏸️ manually paused")
    return label


def recovery_warning(job: dict[str, Any], locale: str) -> str:
    warnings = []
    if job.get("recovery_note") == "progress_lost":
        warnings.append(tr(locale,
                           "Parte del progreso local anterior no estaba disponible tras la pausa o el reinicio; solo se conservan los bytes guardados.",
                           "Some previous local progress was unavailable after a pause or restart; only the stored bytes are retained."))
    if job.get("range_unverified"):
        warnings.append(tr(locale, "No se pudo verificar si el origen cambió al reanudar; debes verificar el archivo.",
                           "The source could not be verified when resuming; verify the file."))
    return " ".join(warnings)


def describe_job(job: dict[str, Any], locale: str, detailed: bool = False) -> str:
    size = job.get("size") or job.get("size_est") or 0
    head = f"#{job['id']} {job.get('name') or job['title']}"[:110]
    lines = [head, f"   {status_label(job, locale)} · {human_size(size)}"]
    if job["status"] in {"queued", "downloading", "packing"} and size and job.get("progress"):
        lines[-1] += f" · {min(100, int(job.get('progress', 0) * 100 / max(1, size)))}%"
    if job.get("parts"):
        delivered = int(job["parts"]) - len(job.get("todo") or []) if job["status"] in {"sending", "done", "ready"} else int(job.get("sent") or 0)
        lines[-1] += f" · {delivered}/{job['parts']} " + tr(locale, "partes", "parts")
    if float(job.get("not_before") or 0) > time.time() + 60:
        lines.append("   🕒 " + local_time(float(job["not_before"]), "%d/%m %H:%M"))
    if (job["status"] == "failed" or job.get("error") == "source_preservation") and job.get("error"):
        lines.append(f"   {_error_text(job['error'], locale)}")
    if warning := recovery_warning(job, locale):
        lines.append(f"   ⚠️ {warning}")
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
        if job.get("error") == "source_preservation":
            return tr(locale, "⚠️ El archivo anterior está protegido. No se reanudará hasta que lo revise el administrador.",
                      "⚠️ The previous source is protected. It will not resume until an administrator reviews it.")
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
