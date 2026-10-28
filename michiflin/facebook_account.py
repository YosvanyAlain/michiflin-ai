"""Optional Facebook Login for Devices and narrowly scoped read-only Graph API."""
from __future__ import annotations

import os
import re
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from . import social_vault as vault, state

AccountError = vault.AccountError
BASE = "https://graph.facebook.com/v26.0/"
DEVICE = BASE + "device/login"
STATUS = BASE + "device/login_status"
ME = BASE + "me"
PERMISSIONS = BASE + "me/permissions"
POSTS = BASE + "me/posts"
ALLOWED = {DEVICE, STATUS, ME, PERMISSIONS, POSTS}
_pending: dict[str, dict[str, Any]] = {}
_lock = threading.RLock()


def _request(method: str, url: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
    return vault.request(ALLOWED, method, url, **kwargs)


def _settings() -> tuple[str, str, str]:
    return (os.getenv("FACEBOOK_APP_ID", "").strip(), os.getenv("FACEBOOK_CLIENT_TOKEN", "").strip(),
            os.getenv("WEB_SESSION_KEY", "").strip())


def configured() -> bool:
    app, client, key = _settings()
    return bool(app and client and len(key) >= 32)


def status(address: str) -> dict[str, Any]:
    address = vault.address(address)
    with _lock:
        stored = vault.path("facebook", address).exists()
        if not configured():
            return {"configured": False, "connected": False, "stored": stored}
        try:
            item = vault.load("facebook", address)
        except AccountError as exc:
            if str(exc) != "vault":
                raise
            return {"configured": True, "connected": False, "stored": stored, "vault_error": True}
        pending = _pending.get(address)
        if pending and pending["expires"] <= time.time():
            _pending.pop(address, None)
            pending = None
        valid = bool(item and float(item.get("expires", 0)) > time.time())
        result: dict[str, Any] = {"configured": True, "connected": valid, "stored": stored,
                                  "expired": bool(item and not valid), "pending": bool(pending),
                                  "posts_granted": bool(item and "user_posts" in item.get("scopes", []))}
        if pending:
            result.update(url=pending["url"], user_code=pending["user"], expires_in=max(0, int(pending["expires"] - time.time())))
        return result


def start(address: str) -> dict[str, Any]:
    address = vault.address(address)
    if not configured():
        raise AccountError("not_configured")
    state.check_work_allowed()
    app, client, _key = _settings()
    scopes = ["public_profile"]
    if os.getenv("FACEBOOK_READ_POSTS", "0").strip() == "1":
        scopes.append("user_posts")
    code, data = _request("POST", DEVICE, data={"access_token": app + "|" + client, "scope": ",".join(scopes)})
    state.check_work_allowed()
    if code != 200:
        raise AccountError("provider")
    device, user = str(data.get("code", "")), str(data.get("user_code", ""))
    url = str(data.get("verification_uri", ""))
    try:
        parsed = urlsplit(url)
        safe_url = (parsed.scheme == "https" and parsed.hostname == "www.facebook.com" and parsed.port in {None, 443}
                    and not parsed.username and not parsed.password and parsed.path.rstrip("/") == "/device"
                    and not parsed.query and not parsed.fragment)
        expires = max(1, min(int(data.get("expires_in", 0)), 3600))
        interval = max(5, min(int(data.get("interval", 5)), 120))
    except (TypeError, ValueError):
        raise AccountError("response") from None
    if not device or len(device) > 1000 or not re.fullmatch(r"[A-Za-z0-9-]{4,24}", user) or not safe_url:
        raise AccountError("response")
    with _lock:
        _pending[address] = {"device": device, "user": user, "url": url, "expires": time.time() + expires,
                             "interval": interval, "last_poll": 0.0, "scopes": scopes}
    return {"configured": True, "connected": False, "pending": True, "url": url,
            "user_code": user, "expires_in": expires}


def poll(address: str) -> dict[str, Any]:
    address = vault.address(address)
    if not configured():
        raise AccountError("not_configured")
    state.check_work_allowed()
    with _lock:
        pending = _pending.get(address)
        if not pending or pending["expires"] <= time.time():
            _pending.pop(address, None)
            return {"pending": False, "expired": True}
        wait = pending["interval"] - (time.time() - pending["last_poll"])
        if wait > 0:
            return {"pending": True, "retry_in": max(1, int(wait + 0.999))}
        pending["last_poll"] = time.time()
        app, client, _key = _settings()
        code, data = _request("POST", STATUS, data={"access_token": app + "|" + client, "code": pending["device"]})
        state.check_work_allowed()
        if code != 200 or not data.get("access_token"):
            error = data.get("error") if isinstance(data.get("error"), dict) else {}
            subcode = error.get("error_subcode")
            if subcode in {1349174, 1349172}:
                if subcode == 1349172:
                    pending["interval"] = min(120, pending["interval"] + 5)
                return {"pending": True, "retry_in": pending["interval"]}
            _pending.pop(address, None)
            if subcode == 1349152:
                return {"pending": False, "expired": True}
            raise AccountError("provider")
        token = data.get("access_token")
        if not isinstance(token, str) or not token or len(token) > 4096:
            raise AccountError("response")
        auth = {"Authorization": "Bearer " + token}
        permission_code, permissions = _request("GET", PERMISSIONS, headers=auth)
        state.check_work_allowed()
        entries = permissions.get("data")
        if permission_code != 200 or not isinstance(entries, list):
            raise AccountError("scope")
        granted = {str(x.get("permission")) for x in entries if isinstance(x, dict) and x.get("status") == "granted"}
        if "public_profile" not in granted or not granted.issubset(set(pending["scopes"])):
            raise AccountError("scope")
        try:
            expires = max(1, min(int(data.get("expires_in", 0)), 60 * 86400))
        except (TypeError, ValueError):
            raise AccountError("response") from None
        vault.save("facebook", address, {"access_token": token, "expires": time.time() + expires, "scopes": sorted(granted)})
        _pending.pop(address, None)
        return {"configured": True, "connected": True, "pending": False, "stored": True, "posts_granted": "user_posts" in granted}


def read(address: str, kind: str) -> dict[str, Any]:
    address = vault.address(address)
    if kind not in {"profile", "posts"}:
        raise AccountError("endpoint")
    state.check_work_allowed()
    with _lock:
        item = vault.load("facebook", address)
        if not item:
            raise AccountError("not_connected")
        if float(item.get("expires", 0)) <= time.time():
            raise AccountError("reauthorize")
        if kind == "posts" and "user_posts" not in item.get("scopes", []):
            raise AccountError("scope")
        auth = {"Authorization": "Bearer " + str(item["access_token"])}
        if kind == "profile":
            code, data = _request("GET", ME, headers=auth, params={"fields": "name"})
        else:
            code, data = _request("GET", POSTS, headers=auth, params={"fields": "message,created_time", "limit": 20})
    state.check_work_allowed()
    if code != 200:
        if isinstance(data.get("error"), dict) and data["error"].get("code") == 190:
            raise AccountError("reauthorize")
        raise AccountError("provider")
    if kind == "profile":
        name = str(data.get("name") or "")[:200]
        return {"kind": kind, "items": [{"title": name, "description": ""}] if name else []}
    rows = []
    for entry in data.get("data", [])[:20]:
        if isinstance(entry, dict) and entry.get("message"):
            rows.append({"title": str(entry.get("created_time") or "")[:40], "description": str(entry["message"])[:2000]})
    return {"kind": kind, "items": rows}


def cancel(address: str) -> dict[str, Any]:
    address = vault.address(address)
    with _lock:
        _pending.pop(address, None)
        return status(address)


def disconnect(address: str) -> dict[str, Any]:
    address = vault.address(address)
    with _lock:
        _pending.pop(address, None)
        try:
            item = vault.load("facebook", address)
        except AccountError as exc:
            if str(exc) not in {"vault", "not_configured"}:
                raise
            return {"connected": False, "revoked": not vault.remove("facebook", address)}
        if not item:
            return {"connected": False, "revoked": True}
        try:
            code, data = _request("DELETE", PERMISSIONS, headers={"Authorization": "Bearer " + str(item.get("access_token", ""))})
            revoked = code == 200 and data.get("success") is True
        except AccountError:
            revoked = False
        vault.remove("facebook", address)
        return {"connected": False, "revoked": revoked}
