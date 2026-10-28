"""Optional YouTube read-only OAuth device flow, isolated by Michiflin sender.

The user signs in only at Google's site. Device codes stay in memory; access and
refresh tokens never leave the backend. A separate deployment secret encrypts
per-sender files which are also included in the encrypted state backup.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from cryptography.fernet import Fernet, InvalidToken

from . import config, state

DEVICE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API_URL = "https://www.googleapis.com/youtube/v3/"
SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
_pending: dict[str, dict[str, Any]] = {}
_lock = threading.RLock()


class AccountError(Exception):
    """An intentionally non-sensitive error code."""


def _settings() -> tuple[str, str, str]:
    client = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    vault = os.getenv("WEB_SESSION_KEY", "").strip()
    return client, secret, vault


def configured() -> bool:
    client, secret, vault = _settings()
    return bool(client and secret and len(vault) >= 32)


def _address(address: str) -> str:
    value = (address or "").strip().casefold()
    if not re.fullmatch(r"[^\s@]{1,128}@[^\s@]{1,255}", value):
        raise AccountError("identity")
    return value


def _path(address: str) -> Path:
    # Stable across key rotation, so an unreadable old grant can still be removed.
    ident = hashlib.sha256(("youtube:" + _address(address)).encode()).hexdigest()
    return config.CONFIG_DIR / "web-sessions" / (ident + ".enc")


def _vault(address: str) -> tuple[Path, Fernet]:
    _, _, secret = _settings()
    if len(secret) < 32:
        raise AccountError("not_configured")
    key = hashlib.sha256(("michiflin-youtube-v1:" + secret).encode()).digest()
    return _path(address), Fernet(base64.urlsafe_b64encode(key))


def _load(address: str) -> dict[str, Any] | None:
    path, cipher = _vault(address)
    if not path.exists():
        return None
    try:
        obj = json.loads(cipher.decrypt(path.read_bytes()).decode("utf-8"))
        if obj.get("address") != _address(address) or obj.get("service") != "youtube":
            raise ValueError("identity mismatch")
        return obj
    except (InvalidToken, OSError, ValueError, TypeError, AttributeError):
        raise AccountError("vault") from None


def _save(address: str, value: dict[str, Any]) -> None:
    path, cipher = _vault(address)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    payload = dict(value, address=_address(address), service="youtube")
    encrypted = cipher.encrypt(json.dumps(payload, separators=(",", ":")).encode())
    temp = path.with_suffix(".tmp")
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encrypted)
        os.replace(temp, path)
        path.chmod(0o600)
    finally:
        temp.unlink(missing_ok=True)


def _request(method: str, url: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
    """Only fixed Google HTTPS endpoints are used; no redirect can receive a token."""
    permitted = {DEVICE_URL, TOKEN_URL, REVOKE_URL, API_URL + "channels", API_URL + "subscriptions"}
    if url not in permitted:
        raise AccountError("endpoint")
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.request(method, url, timeout=(10, 20), allow_redirects=False, **kwargs)
            if response.status_code in range(300, 400):
                raise AccountError("network")
            if len(response.content) > 1_000_000:
                raise AccountError("response")
            try:
                result = response.json()
            except ValueError:
                result = {}
            return response.status_code, result if isinstance(result, dict) else {}
    except requests.RequestException:
        raise AccountError("network") from None


def status(address: str) -> dict[str, Any]:
    address = _address(address)
    if not configured():
        return {"configured": False, "connected": False, "stored": _path(address).exists()}
    with _lock:
        try:
            present = _load(address) is not None
        except AccountError as exc:
            if str(exc) != "vault":
                raise
            return {"configured": True, "connected": False, "stored": True, "vault_error": True}
        item = _pending.get(address)
        if item and item["expires"] <= time.time():
            _pending.pop(address, None)
            item = None
        view = {"configured": True, "connected": present, "pending": bool(item)}
        if item:
            view.update(url=item["url"], user_code=item["user"], expires_in=max(0, int(item["expires"] - time.time())))
        return view


def start(address: str) -> dict[str, Any]:
    address = _address(address)
    if not configured():
        raise AccountError("not_configured")
    state.check_work_allowed()
    client, _secret, _vault_key = _settings()
    code, data = _request("POST", DEVICE_URL, data={"client_id": client, "scope": SCOPE})
    state.check_work_allowed()
    if code != 200:
        raise AccountError("provider")
    device, user = str(data.get("device_code", "")), str(data.get("user_code", ""))
    verification = str(data.get("verification_url", ""))
    parsed = urlsplit(verification)
    if (not device or len(device) > 1000 or not user or len(user) > 64 or not user.isascii()
            or not all(32 <= ord(c) < 127 for c in user)
            or parsed.scheme != "https" or parsed.hostname not in {"google.com", "www.google.com"}
            or parsed.username or parsed.password or parsed.port not in {None, 443} or parsed.path != "/device"):
        raise AccountError("response")
    try:
        expires = max(1, min(int(data.get("expires_in", 0)), 3600))
        interval = max(5, min(int(data.get("interval", 5)), 60))
    except (TypeError, ValueError):
        raise AccountError("response") from None
    with _lock:
        _pending[address] = {"device": device, "user": user, "url": verification, "expires": time.time() + expires,
                             "interval": interval, "last_poll": 0.0}
    return {"configured": True, "connected": False, "pending": True, "url": verification,
            "user_code": user, "expires_in": expires}


def poll(address: str) -> dict[str, Any]:
    address = _address(address)
    if not configured():
        raise AccountError("not_configured")
    state.check_work_allowed()
    with _lock:
        pending = _pending.get(address)
        if not pending or pending["expires"] <= time.time():
            _pending.pop(address, None)
            return {"pending": False, "expired": True}
        remaining = pending["interval"] - (time.time() - pending["last_poll"])
        if remaining > 0:
            return {"pending": True, "retry_in": max(1, int(remaining + 0.999))}
        pending["last_poll"] = time.time()
        device = pending["device"]
        client, secret, _vault_key = _settings()
        code, data = _request("POST", TOKEN_URL, data={"client_id": client, "client_secret": secret,
                               "device_code": device, "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        state.check_work_allowed()
        if code != 200:
            error = data.get("error")
            if error in {"authorization_pending", "slow_down"}:
                if error == "slow_down":
                    pending["interval"] = min(120, pending["interval"] + 5)
                return {"pending": True, "retry_in": pending["interval"]}
            _pending.pop(address, None)
            if error in {"access_denied", "expired_token"}:
                return {"pending": False, "denied": error == "access_denied", "expired": error == "expired_token"}
            raise AccountError("provider")
        access_token, refresh = data.get("access_token"), data.get("refresh_token")
        scopes = str(data.get("scope", "")).split()
        if not isinstance(access_token, str) or not access_token or not isinstance(refresh, str) or not refresh or set(scopes) != {SCOPE}:
            raise AccountError("scope")
        try:
            expires = max(1, min(int(data.get("expires_in", 3600)), 86400))
        except (TypeError, ValueError):
            raise AccountError("response") from None
        _save(address, {"access_token": access_token, "refresh_token": refresh, "expires": time.time() + expires, "scope": SCOPE})
        _pending.pop(address, None)
        return {"configured": True, "connected": True, "pending": False}


def _token(address: str) -> str:
    item = _load(address)
    if not item:
        raise AccountError("not_connected")
    if float(item.get("expires", 0)) > time.time() + 60:
        return str(item["access_token"])
    client, secret, _vault_key = _settings()
    code, data = _request("POST", TOKEN_URL, data={"client_id": client, "client_secret": secret,
                           "refresh_token": item["refresh_token"], "grant_type": "refresh_token"})
    if code != 200 or not data.get("access_token"):
        raise AccountError("reauthorize")
    state.check_work_allowed()
    item["access_token"] = str(data["access_token"])
    item["expires"] = time.time() + max(1, min(int(data.get("expires_in", 3600)), 86400))
    _save(address, item)
    return item["access_token"]


def read(address: str, kind: str) -> dict[str, Any]:
    address = _address(address)
    if kind not in {"channels", "subscriptions"}:
        raise AccountError("endpoint")
    state.check_work_allowed()
    with _lock:
        token = _token(address)
        code, data = _request("GET", API_URL + kind, headers={"Authorization": "Bearer " + token},
                              params={"part": "snippet", "mine": "true", "maxResults": 20})
    state.check_work_allowed()
    if code != 200:
        raise AccountError("provider")
    rows = []
    for entry in data.get("items", [])[:20]:
        if not isinstance(entry, dict):
            continue
        snippet = entry.get("snippet") if isinstance(entry.get("snippet"), dict) else {}
        title = str(snippet.get("title") or "")[:200]
        if title:
            rows.append({"title": title, "description": str(snippet.get("description") or "")[:300]})
    return {"items": rows, "kind": kind}


def disconnect(address: str) -> dict[str, Any]:
    address = _address(address)
    with _lock:
        _pending.pop(address, None)
        try:
            item = _load(address)
        except AccountError as exc:
            if str(exc) not in {"vault", "not_configured"}:
                raise
            # A rotated/lost key cannot decrypt the old grant. Remove the local
            # ciphertext anyway and tell the user to revoke it with Google.
            path = _path(address)
            existed = path.exists()
            path.unlink(missing_ok=True)
            return {"connected": False, "revoked": not existed}
        if not item:
            return {"connected": False, "revoked": True}
        try:
            code, _ = _request("POST", REVOKE_URL, data={"token": item.get("refresh_token") or item.get("access_token")})
            revoked = code == 200
        except AccountError:
            revoked = False
        path, _cipher = _vault(address)
        path.unlink(missing_ok=True)
        return {"connected": False, "revoked": revoked}
