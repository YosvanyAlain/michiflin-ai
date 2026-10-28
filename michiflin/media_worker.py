"""yt-dlp worker for a single user's encrypted browser session.

It runs inside sandbox.py's isolated namespaces. Tokens and extractor errors are
never printed; stdout contains only bounded progress and sanitized results.
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yt_dlp


def emit(value: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def fail(exc: Exception) -> str:
    text = str(exc).lower()
    if "sign in to confirm" in text or "not a bot" in text or "captcha" in text:
        return "bot_check"
    if "private" in text or "login" in text or "sign in" in text:
        return "private"
    if "unavailable" in text or "removed" in text or "not available" in text:
        return "unavailable"
    if "geo" in text or "country" in text:
        return "geo"
    if "unsupported url" in text:
        return "unsupported"
    if "too large" in text or "max_filesize" in text or "file is larger" in text:
        return "too_large"
    return "media_error"


def cookies_from(storage: Any, path: str) -> int:
    if not isinstance(storage, dict) or not isinstance(storage.get("cookies"), list):
        raise ValueError("storage")
    jar = http.cookiejar.MozillaCookieJar(path)
    count = 0
    for row in storage["cookies"][:5000]:
        if not isinstance(row, dict):
            continue
        domain, name, value, cookie_path = (str(row.get(key) or "") for key in ("domain", "name", "value", "path"))
        if not domain or not name or not cookie_path.startswith("/") or any(
                char in item for item in (domain, name, value, cookie_path) for char in ("\n", "\r", "\t", "\x00")):
            continue
        try:
            expiry = float(row.get("expires", -1))
            expires = int(expiry) if expiry > 0 else None
        except (TypeError, ValueError, OverflowError):
            continue
        cookie = http.cookiejar.Cookie(
            version=0, name=name, value=value, port=None, port_specified=False,
            domain=domain, domain_specified=domain.startswith("."), domain_initial_dot=domain.startswith("."),
            path=cookie_path, path_specified=True, secure=bool(row.get("secure")),
            expires=expires, discard=expires is None, comment=None, comment_url=None,
            rest={"HttpOnly": None} if row.get("httpOnly") else {}, rfc2109=False,
        )
        jar.set_cookie(cookie)
        count += 1
    jar.save(ignore_discard=True, ignore_expires=False)
    return count


def narrow(info: dict[str, Any]) -> dict[str, Any]:
    keys = ("title", "uploader", "channel", "duration", "extractor_key", "extractor", "webpage_url", "is_live", "height", "_type")
    output = {key: info.get(key) for key in keys if isinstance(info.get(key), (str, int, float, bool))}
    formats = []
    for row in (info.get("formats") or [])[:300]:
        if isinstance(row, dict):
            formats.append({key: row.get(key) for key in ("filesize", "filesize_approx", "tbr", "vbr", "abr", "vcodec", "acodec", "height")
                            if isinstance(row.get(key), (str, int, float))})
    output["formats"] = formats
    entries = []
    for row in (info.get("entries") or [])[:30]:
        if isinstance(row, dict):
            entries.append({key: row.get(key) for key in ("title", "url", "webpage_url", "id", "duration")
                            if isinstance(row.get(key), (str, int, float))})
    output["entries"] = entries
    return output


def main() -> int:
    os.umask(0o077)  # temporary cookie jar and downloaded content are private
    line = sys.stdin.buffer.readline(3_000_001)
    if not line or len(line) > 3_000_000:
        raise ValueError("input")
    request = json.loads(line)
    url = str(request.get("url") or "")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.port not in (None, 443) or parsed.username or parsed.password or len(url) > 4096:
        raise ValueError("bad_url")
    storage = request.get("storage")
    cookies_from(storage, "/work/session.txt")
    options: dict[str, Any] = {
        "quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
        "socket_timeout": 45, "retries": 2, "fragment_retries": 2, "concurrent_fragment_downloads": 2,
        "proxy": "http://127.0.0.1:18080", "cookiefile": "/work/session.txt",
        "js_runtimes": {"deno": {}, "node": {}},
    }
    agent = str(storage.get("_michiflin_user_agent") or "") if isinstance(storage, dict) else ""
    if agent and len(agent) <= 512 and not any(ch in agent for ch in "\r\n"):
        options["http_headers"] = {"User-Agent": agent}
    operation = request.get("operation")
    if operation == "inspect":
        options.update(skip_download=True, extract_flat="in_playlist")
        with yt_dlp.YoutubeDL(options) as client:
            info = client.extract_info(url, download=False)
        if not isinstance(info, dict):
            raise ValueError("media_error")
        emit({"kind": "info", "info": narrow(info)})
        return 0
    if operation != "download":
        raise ValueError("operation")
    kind = str(request.get("kind") or "video")
    quality = int(request.get("quality") or 360)
    audio = str(request.get("audio_format") or "m4a")
    if kind not in {"audio", "video"} or quality not in {32, 48, 64, 96, 128, 160, 192, 144, 240, 360, 480, 720, 1080}:
        raise ValueError("format")
    if audio not in {"m4a", "mp3", "opus", "ogg", "wav"}:
        raise ValueError("format")
    maximum = int(request.get("max_bytes") or 0)
    if maximum < 1 or maximum > 20_000_000_000:
        raise ValueError("limit")
    last = [0.0]
    def progress(value: dict[str, Any]) -> None:
        done = int(value.get("downloaded_bytes") or 0)
        if done > maximum:
            raise ValueError("too large")
        if value.get("status") == "downloading" and time.monotonic() - last[0] > 1:
            last[0] = time.monotonic()
            emit({"kind": "progress", "done": done,
                  "total": int(value.get("total_bytes") or value.get("total_bytes_estimate") or 0)})
    options.update(outtmpl="/work/media.%(ext)s", progress_hooks=[progress], max_filesize=maximum)
    minutes = int(request.get("max_minutes") or 0)
    if minutes > 0:
        options["match_filter"] = yt_dlp.utils.match_filter_func(f"duration <= {minutes * 60}")
    if kind == "audio":
        options.update(format=f"ba[abr<={max(48, int(quality * 1.4))}]/ba/b",
                       postprocessors=[{"key": "FFmpegExtractAudio", "preferredcodec": audio, "preferredquality": str(quality)}])
    else:
        options.update(format="bv*+ba/b", format_sort=[f"res:{quality}", "vcodec:h264", "acodec:aac", "abr:96", "+size"],
                       merge_output_format="mp4", postprocessors=[{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}])
    with yt_dlp.YoutubeDL(options) as client:
        client.download([url])
    results = [item for item in Path("/work").glob("media.*") if re.fullmatch(r"media\.[a-zA-Z0-9]{1,8}", item.name)
               and item.is_file() and item.stat().st_size <= maximum]
    if not results:
        raise ValueError("media_error")
    result = max(results, key=lambda item: item.stat().st_size)
    emit({"kind": "done", "file": result.name, "size": result.stat().st_size})
    return 0


if __name__ == "__main__":
    try:
        status = main()
    except Exception as exc:
        # Only the category escapes the sandbox, never exception text or URLs.
        emit({"kind": "error", "error": fail(exc)})
        status = 1
    raise SystemExit(status)
