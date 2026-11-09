"""Fail-closed compatibility for the two exact previously shipped WebXDC packages.

Only files changed by a later package are archived. Every other source file is
verified against the pinned manifest before its epoch-specific hash is returned.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

_PREVIOUS = Path(__file__).resolve().parent / "compat" / "618c355"
_REPLACED = {"app.css", "app.js", "brands.js", "coder.js", "core.js", "i18n.js", "live.js"}
_RECENT = Path(__file__).resolve().parent / "compat" / "58a0657"
_RECENT_REPLACED = {"app.css", "app.js", "brands.js", "coder.js", "core.js", "i18n.js"}


def _pinned_build(epoch: str, app_dir: str, files: tuple[str, ...], source_dir: Path, replaced: set[str]) -> str:
    try:
        expected = json.loads((source_dir / "manifest.json").read_text(encoding="utf-8"))
        if not isinstance(expected, dict) or len(files) != len(expected) or set(files) != set(expected):
            return ""
        digest = hashlib.sha256()
        digest.update(f"epoch:{epoch}".encode())
        for name in files:
            source = (source_dir if name in replaced else Path(app_dir)) / name
            data = source.read_bytes()
            if hashlib.sha256(data).hexdigest() != expected[name]:
                return ""
            digest.update(name.encode() + data)
        return digest.hexdigest()[:10]
    except (OSError, TypeError, ValueError, UnicodeError, KeyError):
        return ""


def previous_build(epoch: str, app_dir: str, files: tuple[str, ...]) -> str:
    """The exact 618c355 package, or an empty string when it cannot be verified."""
    return _pinned_build(epoch, app_dir, files, _PREVIOUS, _REPLACED)


def recent_build(epoch: str, app_dir: str, files: tuple[str, ...]) -> str:
    """The exact 58a0657 package, or an empty string when it cannot be verified."""
    return _pinned_build(epoch, app_dir, files, _RECENT, _RECENT_REPLACED)
