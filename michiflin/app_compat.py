"""Narrow compatibility for the immediately preceding WebXDC package.

The three changed public files from 618c355 are retained for hash calculation,
not distributed in the new app. Every other file must still match its pinned
hash. A missing or changed file fails closed instead of whitelisting a guess.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

_PREVIOUS = Path(__file__).resolve().parent / "compat" / "618c355"
_REPLACED = {"app.css", "i18n.js", "live.js"}


def previous_build(epoch: str, app_dir: str, files: tuple[str, ...]) -> str:
    """Compute the exact 618c355 build for this deployment epoch, or deny it."""
    try:
        expected = json.loads((_PREVIOUS / "manifest.json").read_text(encoding="utf-8"))
        if not isinstance(expected, dict) or len(files) != len(expected) or set(files) != set(expected):
            return ""
        digest = hashlib.sha256()
        digest.update(f"epoch:{epoch}".encode())
        for name in files:
            source = (_PREVIOUS if name in _REPLACED else Path(app_dir)) / name
            data = source.read_bytes()
            if hashlib.sha256(data).hexdigest() != expected[name]:
                return ""
            digest.update(name.encode() + data)
        return digest.hexdigest()[:10]
    except (OSError, TypeError, ValueError, UnicodeError, KeyError):
        return ""
