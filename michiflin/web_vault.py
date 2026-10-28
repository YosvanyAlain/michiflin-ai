"""Encrypted browser storage states, independently keyed per user and site.

A storage state contains bearer-equivalent cookies. Never log it or return it in
WebXDC status updates. It is only passed to a private browser over local pipes.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import config, social_vault

MAX_STATE_BYTES = 2_000_000
MAX_SITES = 16


class VaultError(Exception):
    """A short, non-sensitive failure code."""


def site_of(url: str) -> str:
    """Find the isolation key without accepting credentials or non-HTTPS URLs."""
    try:
        parsed = urlsplit(str(url).strip())
        host = (parsed.hostname or "").rstrip(".").lower().encode("idna").decode("ascii")
        port = parsed.port
    except (ValueError, UnicodeError):
        raise VaultError("bad_url") from None
    if parsed.scheme.lower() != "https" or not host or parsed.username is not None or parsed.password is not None:
        raise VaultError("bad_url")
    if port not in (None, 443) or len(host) > 253 or not re.fullmatch(r"[a-z0-9.-]+", host):
        raise VaultError("bad_url")
    if not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split(".")):
        raise VaultError("bad_url")
    # The same service may use its canonical, mobile or short video hostname.
    groups = {
        "youtube": ("youtube.com", "youtu.be", "youtube-nocookie.com"),
        "facebook": ("facebook.com", "fb.watch"),
        "tiktok": ("tiktok.com",),
        "instagram": ("instagram.com",),
        "deepseek": ("deepseek.com",),
        "arena": ("arena.ai",),
        "zai": ("z.ai",),
    }
    for name, domains in groups.items():
        if any(host == domain or host.endswith("." + domain) for domain in domains):
            return name
    # Generic websites are separated by exact hostname. Two unrelated hosts
    # must not be grouped by an improvised public-suffix calculation.
    return "host:" + host


def valid_site(site: str) -> bool:
    if site in {"youtube", "facebook", "tiktok", "instagram", "deepseek", "arena", "zai"}:
        return True
    if not isinstance(site, str) or not site.startswith("host:"):
        return False
    try:
        return site_of("https://" + site[5:]) == site
    except VaultError:
        return False


def _secret() -> bytes:
    value = os.getenv("WEB_SESSION_KEY", "").strip()
    if len(value) < 32:
        raise VaultError("not_configured")
    return value.encode("utf-8")


def _identity(owner: str) -> str:
    try:
        return social_vault.address(owner)
    except social_vault.AccountError:
        raise VaultError("identity") from None


def _key(owner: str, site: str) -> bytes:
    if not isinstance(site, str) or not site or len(site) > 270:
        raise VaultError("site")
    info = b"michiflin-browser-v1\0" + _identity(owner).encode() + b"\0" + site.encode()
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"michiflin-browser-v1", info=info).derive(_secret())


def _path(owner: str, site: str) -> Path:
    # HMAC obscures account addresses and service names in backup filenames.
    secret = _secret()
    account = hmac.new(secret, b"user\0" + _identity(owner).encode(), hashlib.sha256).hexdigest()
    website = hmac.new(secret, b"site\0" + site.encode(), hashlib.sha256).hexdigest()
    return config.CONFIG_DIR / "browser-sessions-v1" / account / (website + ".enc")


def _cipher(owner: str, site: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(_key(owner, site)))


def load(owner: str, site: str) -> dict[str, Any] | None:
    target = _path(owner, site)
    try:
        if not target.is_file():
            return None
        raw = target.read_bytes()
        if len(raw) > MAX_STATE_BYTES * 2:
            raise ValueError("oversized")
        value = json.loads(_cipher(owner, site).decrypt(raw))
        if not isinstance(value, dict) or value.get("owner") != _identity(owner) or value.get("site") != site:
            raise ValueError("binding")
        state = value.get("storage")
        if not isinstance(state, dict) or not isinstance(state.get("cookies"), list) or not isinstance(state.get("origins"), list):
            raise ValueError("format")
        return state
    except (OSError, ValueError, TypeError, InvalidToken):
        raise VaultError("vault") from None


def save(owner: str, site: str, storage: dict[str, Any]) -> None:
    if not isinstance(storage, dict) or not isinstance(storage.get("cookies"), list) or not isinstance(storage.get("origins"), list):
        raise VaultError("format")
    try:
        plain = json.dumps({"owner": _identity(owner), "site": site, "storage": storage},
                           separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (ValueError, TypeError):
        raise VaultError("format") from None
    if len(plain) > MAX_STATE_BYTES:
        raise VaultError("too_large")
    target = _path(owner, site)
    cipher = _cipher(owner, site)
    try:
        config.CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        config.CONFIG_DIR.chmod(0o700)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.parent.parent.chmod(0o700)
        target.parent.chmod(0o700)
        if not target.exists() and sum(1 for path in target.parent.glob("*.enc") if path.is_file()) >= MAX_SITES:
            raise VaultError("site_limit")
    except OSError:
        raise VaultError("vault") from None
    # Random temp names avoid collisions if two authorized saves race.
    temporary = target.parent / ("." + secrets.token_hex(12) + ".tmp")
    try:
        fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(cipher.encrypt(plain))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except OSError:
        raise VaultError("vault") from None
    finally:
        temporary.unlink(missing_ok=True)


def remove(owner: str, site: str) -> bool:
    target = _path(owner, site)
    try:
        existed = target.exists()
        target.unlink(missing_ok=True)
        return existed
    except OSError:
        raise VaultError("vault") from None
