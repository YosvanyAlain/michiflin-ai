"""Fail-closed compatibility for exact previously shipped WebXDC packages.

The oldest non-retired sources retain exact source verification. Later packages
use independently calculated, pinned fingerprints for the deployed epoch so
retired theme implementations do not need to remain in this source tree.
Unknown epochs and modified manifests fail closed.
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

_PRE_REFRESH = Path(__file__).resolve().parent / "compat" / "pre-refresh"
_PRE_REFRESH_REPLACED = {'app.css', 'core.js', 'i18n.js', 'app.js', 'index.html'}


_KNOWN_BUILDS = Path(__file__).resolve().parent / "compat" / "known-builds.json"
_KNOWN_BUILDS_SHA256 = "282a177ae427159bb09e739bfe609ef9614a8d53b0d694c1ff30121cedcd7104"


def _registered_build(epoch: str, app_dir: str, files: tuple[str, ...], source_dir: Path, replaced: set[str], package: str) -> str:
    try:
        raw = _KNOWN_BUILDS.read_bytes()
        if hashlib.sha256(raw).hexdigest() != _KNOWN_BUILDS_SHA256:
            return ""
        registry = json.loads(raw)
        if registry.get("version") != 1:
            return ""
        entry = registry["packages"][package]
        manifest = (source_dir / "manifest.json").read_bytes()
        if hashlib.sha256(manifest).hexdigest() != entry["manifest_sha256"]:
            return ""
        expected = json.loads(manifest)
        if not isinstance(expected, dict) or not expected or not set(expected) <= set(files):
            return ""
        for name, digest in expected.items():
            if name not in replaced and hashlib.sha256((Path(app_dir) / name).read_bytes()).hexdigest() != digest:
                archived = _MOBILE_BEFORE / name
                if name not in _MOBILE_REPLACED or not archived.is_file() or hashlib.sha256(archived.read_bytes()).hexdigest() != digest:
                    return ""
        build = entry["builds"].get(hashlib.sha256(epoch.encode()).hexdigest(), "")
        return build if isinstance(build, str) and len(build) == 10 and all(c in "0123456789abcdef" for c in build) else ""
    except (OSError, TypeError, ValueError, UnicodeError, KeyError, AttributeError):
        return ""


def _pinned_build(epoch: str, app_dir: str, files: tuple[str, ...], source_dir: Path, replaced: set[str], release: str = "") -> str:
    try:
        expected = json.loads((source_dir / "manifest.json").read_text(encoding="utf-8"))
        if not isinstance(expected, dict) or not expected or not set(expected) <= set(files):
            return ""
        digest = hashlib.sha256()
        digest.update(f"epoch:{epoch}".encode())
        if release:
            digest.update(f"miniapp-release:{release}".encode())
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
    return _registered_build(epoch, app_dir, files, _PUBLISHED, _PUBLISHED_REPLACED, "e8a104c")


def audit_base_build(epoch: str, app_dir: str, files: tuple[str, ...]) -> str:
    """Preserve the exact package shipped before the local QA corrections."""
    return _registered_build(epoch, app_dir, files, _AUDIT_BASE, _AUDIT_BASE_REPLACED, "ec7e67e")


def pre_refresh_build(epoch: str, app_dir: str, files: tuple[str, ...]) -> str:
    """Recognize the unmodified package already in the owner's chat. Never served anew."""
    return _registered_build(epoch, app_dir, files, _PRE_REFRESH, _PRE_REFRESH_REPLACED, "pre-refresh")


_MOBILE_BEFORE = Path(__file__).resolve().parent / "compat" / "mobile-before"
_MOBILE_REPLACED = {"index.html", "app.css", "app.js", "brands.js", "coder.js", "core.js", "i18n.js", "live.js"}


def mobile_previous_build(epoch: str, app_dir: str, files: tuple[str, ...]) -> str:
    """Keep the owner's existing app working while the mobile refresh is installed."""
    return _pinned_build(epoch, app_dir, files, _MOBILE_BEFORE, _MOBILE_REPLACED)


_POLISH_BEFORE = Path(__file__).resolve().parent / "compat" / "polish-before"
_POLISH_REPLACED = {"i18n.js", "index.html", "app.css", "app.js", "core.js", "brands.js", "books.js", "utilities.js"}


def polish_previous_build(epoch: str, app_dir: str, files: tuple[str, ...]) -> str:
    """The exact mobile/library app already delivered, never shipped anew."""
    return _pinned_build(epoch, app_dir, files, _POLISH_BEFORE, _POLISH_REPLACED)


def community_previous_build(epoch: str, release: str, app_dir: str, files: tuple[str, ...]) -> str:
    """Exact pre-community package remains usable; never re-admit older retired apps."""
    directory = Path(__file__).resolve().parent / "compat" / "community-before"
    try:
        if hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest() != "e19445e21551c01c78855a8fa19299ce5e0f868a9fe92e75ef5ecb9feaa35178":
            return ""
    except OSError:
        return ""
    return _pinned_build(epoch, app_dir, files, directory, {"index.html", "app.css", "app.js", "i18n.js"}, release)
