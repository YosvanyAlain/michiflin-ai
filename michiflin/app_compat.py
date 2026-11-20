"""Fail-closed compatibility for exact previously shipped WebXDC packages.

Only files changed by a later package are archived. Every other source file is
verified against the pinned manifest before its epoch-specific hash is returned.
A package is identified by its own manifest, so files that later packages added
are skipped; every file the package did ship must still exist and match.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

_PREVIOUS = Path(__file__).resolve().parent / "compat" / "618c355"
_REPLACED = {"index.html", "app.css", "app.js", "brands.js", "coder.js", "core.js", "i18n.js", "live.js"}
_RECENT = Path(__file__).resolve().parent / "compat" / "58a0657"
_RECENT_REPLACED = {"index.html", "app.css", "app.js", "brands.js", "coder.js", "core.js", "i18n.js", "live.js"}
_LATEST = Path(__file__).resolve().parent / "compat" / "582d273"
_LATEST_REPLACED = {"index.html", "app.css", "app.js", "brands.js", "coder.js", "core.js", "i18n.js", "live.js"}
_PUBLISHED = Path(__file__).resolve().parent / "compat" / "e8a104c"
_PUBLISHED_REPLACED = {"index.html", "app.css", "app.js", "brands.js", "coder.js", "core.js", "i18n.js", "live.js"}
_AUDIT_BASE = Path(__file__).resolve().parent / "compat" / "ec7e67e"
_AUDIT_BASE_REPLACED = {"index.html", "app.css", "app.js", "core.js", "i18n.js"}


def _pinned_build(epoch: str, app_dir: str, files: tuple[str, ...], source_dir: Path, replaced: set[str]) -> str:
    try:
        expected = json.loads((source_dir / "manifest.json").read_text(encoding="utf-8"))
        if not isinstance(expected, dict) or not expected or not set(expected) <= set(files):
            return ""
        digest = hashlib.sha256()
        digest.update(f"epoch:{epoch}".encode())
        for name in files:
            if name not in expected:
                continue  # added by a later package, never part of this one
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


def latest_build(epoch: str, app_dir: str, files: tuple[str, ...]) -> str:
    """The exact 582d273 package, or empty when it cannot be verified."""
    return _pinned_build(epoch, app_dir, files, _LATEST, _LATEST_REPLACED)


def published_build(epoch: str, app_dir: str, files: tuple[str, ...]) -> str:
    """The exact e8a104c package, or empty when it cannot be verified."""
    return _pinned_build(epoch, app_dir, files, _PUBLISHED, _PUBLISHED_REPLACED)


def audit_base_build(epoch: str, app_dir: str, files: tuple[str, ...]) -> str:
    """Preserve the exact package shipped before the local QA corrections."""
    return _pinned_build(epoch, app_dir, files, _AUDIT_BASE, _AUDIT_BASE_REPLACED)
