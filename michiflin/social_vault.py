"""Per-sender encrypted credentials for optional, official social APIs.

Provider secrets and tokens stay on the backend. No browser cookie, password,
access token, or refresh token is ever sent to the WebXDC application.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import requests
from cryptography.fernet import Fernet, InvalidToken

from . import config

SERVICES = {"facebook", "tiktok"}


class AccountError(Exception):
    """A non-sensitive error code suitable for translation."""


def address(value: str) -> str:
    value = (value or "").strip().casefold()
    if not re.fullmatch(r"[^\s@]{1,128}@[^\s@]{1,255}", value):
        raise AccountError("identity")
    return value


def path(service: str, owner: str) -> Path:
    if service not in SERVICES:
        raise AccountError("endpoint")
    identity = hashlib.sha256((service + ":" + address(owner)).encode()).hexdigest()
    return config.CONFIG_DIR / "web-sessions" / (identity + ".enc")


def _cipher(service: str) -> Fernet:
    if service not in SERVICES:
        raise AccountError("endpoint")
    secret = os.getenv("WEB_SESSION_KEY", "").strip()
    if len(secret) < 32:
        raise AccountError("not_configured")
    key = hashlib.sha256(("michiflin-" + service + "-v1:" + secret).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def load(service: str, owner: str) -> dict[str, Any] | None:
    owner = address(owner)
    file = path(service, owner)
    cipher = _cipher(service)
    if not file.exists():
        return None
    try:
        data = json.loads(cipher.decrypt(file.read_bytes()).decode("utf-8"))
        if not isinstance(data, dict) or data.get("address") != owner or data.get("service") != service:
            raise ValueError("identity mismatch")
        return data
    except (InvalidToken, OSError, ValueError, TypeError):
        raise AccountError("vault") from None


def save(service: str, owner: str, data: dict[str, Any]) -> None:
    owner = address(owner)
    file = path(service, owner)
    cipher = _cipher(service)
    file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    file.parent.chmod(0o700)
    encrypted = cipher.encrypt(json.dumps(dict(data, address=owner, service=service), separators=(",", ":")).encode())
    temporary = file.with_suffix(".tmp")
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encrypted)
        os.replace(temporary, file)
        file.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def remove(service: str, owner: str) -> bool:
    file = path(service, owner)
    present = file.exists()
    file.unlink(missing_ok=True)
    return present


def request(allowed: set[str], method: str, url: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
    """A closed set of HTTPS endpoints, no proxy inheritance or redirect with credentials."""
    if url not in allowed:
        raise AccountError("endpoint")
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.request(method, url, timeout=(10, 20), allow_redirects=False, **kwargs)
            if 300 <= response.status_code < 400:
                raise AccountError("network")
            if len(response.content) > 1_000_000:
                raise AccountError("response")
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            return response.status_code, payload if isinstance(payload, dict) else {}
    except requests.RequestException:
        raise AccountError("network") from None
