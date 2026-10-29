"""Music discovery through Internet Archive's public metadata API.

The archive contains uploads with many different rights. A declared license is
not a guarantee of ownership or permission. We expose only actual audio files
listed for an item, without inventing quality or bypassing access restrictions.
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

import requests

BASE = "https://archive.org"
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
AUDIO = re.compile(r"\.(?:mp3|m4a|ogg|oga|opus|flac|wav|aiff|aac)$", re.I)
MAX_FILES = 100
HEADERS = {"User-Agent": "Michiflin/1.0 (public music search)", "Accept": "application/json"}


class MusicError(Exception):
    pass


def _text(value: Any, limit: int = 200) -> str:
    if isinstance(value, list):
        value = ", ".join(str(item) for item in value[:3])
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        # The host and paths are fixed; never follow an API redirect to an unknown host.
        response = requests.get(BASE + path, params=params, headers=HEADERS, timeout=25,
                                allow_redirects=False, stream=True)
        try:
            response.raise_for_status()
            if 300 <= response.status_code < 400:
                raise MusicError("response")
            size = 0
            chunks = []
            for chunk in response.iter_content(chunk_size=65536):
                size += len(chunk)
                if size > 8_000_000:
                    raise MusicError("response")
                chunks.append(chunk)
            body = json.loads(b"".join(chunks))
        finally:
            response.close()
        if not isinstance(body, dict):
            raise MusicError("response")
        return body
    except (requests.RequestException, ValueError, TypeError) as exc:
        raise MusicError("unavailable") from exc


def search(query: str, limit: int = 15) -> list[dict[str, str]]:
    # Quote the user's search as a literal, not as arbitrary Solr query syntax.
    value = re.sub(r"[\x00-\x1f]", " ", str(query)).strip()[:200]
    value = value.replace("\\", "\\\\").replace('"', '\\"')
    if not value:
        return []
    data = _get("/advancedsearch.php", {
        "q": f'mediatype:audio AND licenseurl:* AND format:(MP3 OR "Ogg Vorbis" OR Flac OR WAVE OR Opus OR MPEG4) AND ("{value}")',
        "fl[]": ["identifier", "title", "creator", "licenseurl"],
        "rows": min(20, max(1, int(limit))), "output": "json",
    })
    found = []
    for item in ((data.get("response") or {}).get("docs") or []):
        if not isinstance(item, dict):
            continue
        identifier = _text(item.get("identifier"), 100)
        if not IDENTIFIER.fullmatch(identifier):
            continue
        found.append({"id": identifier, "title": _text(item.get("title")) or identifier,
                      "creator": _text(item.get("creator"), 140), "license": _text(item.get("licenseurl"), 500)})
    return found


def item(identifier: str) -> dict[str, Any]:
    if not IDENTIFIER.fullmatch(str(identifier or "")):
        raise MusicError("identifier")
    body = _get("/metadata/" + quote(identifier, safe=""))
    metadata = body.get("metadata") or {}
    if not isinstance(metadata, dict) or str(metadata.get("mediatype", "")) != "audio":
        raise MusicError("not_audio")
    restricted = str(metadata.get("access-restricted-item", "")).lower() in {"true", "1", "yes"}
    rows = []
    if not restricted:
        for entry in body.get("files") or []:
            if not isinstance(entry, dict) or str(entry.get("private", "")).lower() in {"true", "1", "yes"}:
                continue
            name = str(entry.get("name") or "")
            if (not AUDIO.search(name) or name.startswith("/") or "\\" in name
                    or any(part in {"", ".", ".."} for part in name.split("/"))):
                continue
            try:
                size = max(0, int(entry.get("size") or 0))
            except (TypeError, ValueError):
                size = 0
            rows.append({"name": name, "title": _text(entry.get("title")) or name.rsplit("/", 1)[-1],
                         "format": _text(entry.get("format"), 50) or name.rsplit(".", 1)[-1].upper(),
                         "bitrate": _text(entry.get("bitrate"), 30), "size": size,
                         "url": f"{BASE}/download/{quote(identifier, safe='')}/{quote(name, safe='/')}"})
    return {"id": identifier, "title": _text(metadata.get("title")) or identifier,
            "creator": _text(metadata.get("creator"), 140),
            "license": _text(metadata.get("licenseurl"), 500), "rights": _text(metadata.get("rights"), 500),
            "page": f"{BASE}/details/{quote(identifier, safe='')}",
            "restricted": restricted, "files": rows[:MAX_FILES], "more": len(rows) > MAX_FILES}
