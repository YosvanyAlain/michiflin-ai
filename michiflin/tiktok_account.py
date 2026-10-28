"""Optional TikTok Login Kit QR authorization and read-only Display API.

The QR is an ephemeral authorization request, not a browser cookie. The
provider's ticket, state and registered redirect are checked before exchanging
an authorization code. No code or token is returned to WebXDC.
"""
from __future__ import annotations

import base64
import hmac
import io
import os
import secrets
import threading
import time
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

import qrcode

from . import social_vault as vault, state

AccountError = vault.AccountError
BASE = "https://open.tiktokapis.com/v2/"
GET_QR = BASE + "oauth/get_qrcode/"
CHECK_QR = BASE + "oauth/check_qrcode/"
TOKEN = BASE + "oauth/token/"
REVOKE = BASE + "oauth/revoke/"
PROFILE = BASE + "user/info/"
VIDEOS = BASE + "video/list/"
ALLOWED = {GET_QR, CHECK_QR, TOKEN, REVOKE, PROFILE, VIDEOS}
_pending: dict[str, dict[str, Any]] = {}
_lock = threading.RLock()


def _request(method: str, url: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
    return vault.request(ALLOWED, method, url, **kwargs)


def _settings() -> tuple[str, str, str, str]:
    return (os.getenv("TIKTOK_CLIENT_KEY", "").strip(), os.getenv("TIKTOK_CLIENT_SECRET", "").strip(),
            os.getenv("TIKTOK_OAUTH_REDIRECT_URI", "").strip(), os.getenv("WEB_SESSION_KEY", "").strip())


def _safe_redirect(value: str) -> bool:
    try:
        p = urlsplit(value)
        return bool(p.scheme == "https" and p.hostname and p.port in {None, 443} and not p.username and not p.password
                    and not p.query and not p.fragment and p.hostname not in {"localhost", "127.0.0.1", "::1"})
    except ValueError:
        return False


def configured() -> bool:
    client, secret, redirect, key = _settings()
    return bool(client and secret and _safe_redirect(redirect) and len(key) >= 32)


def _qr_image(url: str) -> str:
    output = io.BytesIO()
    qrcode.make(url, box_size=5, border=4).save(output, format="PNG")
    result = base64.b64encode(output.getvalue()).decode("ascii")
    if len(result) > 100_000:
        raise AccountError("response")
    return result


def _scan_url(value: str, client: str, ticket: str) -> str:
    if len(value) > 4000:
        raise AccountError("response")
    try:
        parts = urlsplit(value)
        query = parse_qsl(parts.query, keep_blank_values=True, max_num_fields=100)
        values = [v for k, v in query if k == "client_ticket"]
        clients = [v for k, v in query if k == "client_key"]
        if (parts.scheme != "aweme" or parts.netloc != "authorize" or parts.path not in {"", "/"} or parts.fragment
                or len(values) != 1 or clients != [client]):
            raise ValueError("invalid QR authorization URL")
        query = [(k, ticket if k == "client_ticket" else v) for k, v in query]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
    except (ValueError, TypeError):
        raise AccountError("response") from None


def status(address: str) -> dict[str, Any]:
    address = vault.address(address)
    with _lock:
        stored = vault.path("tiktok", address).exists()
        if not configured():
            return {"configured": False, "connected": False, "stored": stored}
        try:
            item = vault.load("tiktok", address)
        except AccountError as exc:
            if str(exc) != "vault":
                raise
            return {"configured": True, "connected": False, "stored": stored, "vault_error": True}
        pending = _pending.get(address)
        if pending and pending["expires"] <= time.time():
            _pending.pop(address, None)
            pending = None
        valid = bool(item and (float(item.get("expires", 0)) > time.time() or float(item.get("refresh_expires", 0)) > time.time()))
        result: dict[str, Any] = {"configured": True, "connected": valid, "stored": stored, "expired": bool(item and not valid),
                                  "pending": bool(pending), "videos_granted": bool(item and "video.list" in item.get("scopes", []))}
        if pending:
            result.update(qr_png=pending["qr"], expires_in=max(0, int(pending["expires"] - time.time())))
        return result


def start(address: str) -> dict[str, Any]:
    address = vault.address(address)
    if not configured():
        raise AccountError("not_configured")
    state.check_work_allowed()
    client, _secret, _redirect, _key = _settings()
    scopes = ["user.info.basic"]
    if os.getenv("TIKTOK_READ_VIDEOS", "0").strip() == "1":
        scopes.append("video.list")
    ticket, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    code, data = _request("POST", GET_QR, data={"client_key": client, "scope": ",".join(scopes), "state": csrf})
    state.check_work_allowed()
    if code != 200 or data.get("error"):
        raise AccountError("provider")
    token = data.get("token")
    if not isinstance(token, str) or not token or len(token) > 1000:
        raise AccountError("response")
    scan = _scan_url(str(data.get("scan_qrcode_url") or ""), client, ticket)
    qr = _qr_image(scan)
    try:
        expires = max(1, min(int(data.get("expires_in", 300)), 600))
    except (TypeError, ValueError):
        raise AccountError("response") from None
    with _lock:
        _pending[address] = {"token": token, "ticket": ticket, "state": csrf, "qr": qr,
                             "expires": time.time() + expires, "last_poll": 0.0, "interval": 5, "scopes": scopes}
    return {"configured": True, "connected": False, "pending": True, "qr_png": qr, "expires_in": expires}


def _returned_code(data: dict[str, Any], pending: dict[str, Any], registered: str) -> str:
    if not hmac.compare_digest(str(data.get("client_ticket") or ""), pending["ticket"]):
        raise AccountError("response")
    try:
        actual, expected = urlsplit(str(data.get("redirect_uri") or "")), urlsplit(registered)
        if (actual.scheme, actual.netloc, actual.path, actual.fragment) != (expected.scheme, expected.netloc, expected.path, ""):
            raise ValueError("redirect mismatch")
        query = parse_qs(actual.query, keep_blank_values=True, max_num_fields=32)
        codes = query.get("code", [])
        returned_state = data.get("state") or (query.get("state") or [""])[0]
        if len(codes) != 1 or not codes[0] or len(codes[0]) > 2000:
            raise ValueError("missing authorization code")
        if not isinstance(returned_state, str) or not hmac.compare_digest(returned_state, pending["state"]):
            raise ValueError("state mismatch")
        if "state" in query and (len(query["state"]) != 1 or not hmac.compare_digest(query["state"][0], pending["state"])):
            raise ValueError("state mismatch")
        return codes[0]
    except (TypeError, ValueError):
        raise AccountError("response") from None


def _validated_tokens(data: dict[str, Any], allowed_scopes: list[str]) -> dict[str, Any]:
    token, refresh = data.get("access_token"), data.get("refresh_token")
    scopes = {x.strip() for x in str(data.get("scope") or "").split(",") if x.strip()}
    if (not isinstance(token, str) or not token or len(token) > 4096 or not isinstance(refresh, str)
            or not refresh or len(refresh) > 4096 or "user.info.basic" not in scopes or not scopes.issubset(set(allowed_scopes))):
        raise AccountError("scope")
    try:
        expires = max(1, min(int(data.get("expires_in", 0)), 86400))
        refresh_expires = max(1, min(int(data.get("refresh_expires_in", 31536000)), 366 * 86400))
    except (TypeError, ValueError):
        raise AccountError("response") from None
    return {"access_token": token, "refresh_token": refresh, "expires": time.time() + expires,
            "refresh_expires": time.time() + refresh_expires, "scopes": sorted(scopes)}


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
        remaining = pending["interval"] - (time.time() - pending["last_poll"])
        if remaining > 0:
            return {"pending": True, "retry_in": max(1, int(remaining + 0.999))}
        pending["last_poll"] = time.time()
        client, secret, redirect, _key = _settings()
        code, data = _request("POST", CHECK_QR, data={"client_key": client, "client_secret": secret, "token": pending["token"]})
        state.check_work_allowed()
        if code != 200 or data.get("error"):
            raise AccountError("provider")
        status_value = data.get("status")
        if status_value == "new":
            return {"pending": True, "retry_in": pending["interval"]}
        if status_value == "scanned":
            if not hmac.compare_digest(str(data.get("client_ticket") or ""), pending["ticket"]):
                raise AccountError("response")
            return {"pending": True, "scanned": True, "retry_in": pending["interval"]}
        if status_value in {"expired", "utilised"}:
            _pending.pop(address, None)
            return {"pending": False, "expired": True}
        if status_value != "confirmed":
            raise AccountError("response")
        auth_code = _returned_code(data, pending, redirect)
        code, tokens = _request("POST", TOKEN, data={"client_key": client, "client_secret": secret,
                                "grant_type": "authorization_code", "code": auth_code, "redirect_uri": redirect})
        state.check_work_allowed()
        if code != 200 or tokens.get("error"):
            _pending.pop(address, None)
            raise AccountError("provider")
        item = _validated_tokens(tokens, pending["scopes"])
        vault.save("tiktok", address, item)
        _pending.pop(address, None)
        return {"configured": True, "connected": True, "stored": True, "pending": False,
                "videos_granted": "video.list" in item["scopes"]}


def _token(address: str) -> tuple[str, list[str]]:
    item = vault.load("tiktok", address)
    if not item:
        raise AccountError("not_connected")
    scopes = item.get("scopes", [])
    if float(item.get("expires", 0)) > time.time() + 60:
        return str(item["access_token"]), scopes
    if float(item.get("refresh_expires", 0)) <= time.time():
        raise AccountError("reauthorize")
    client, secret, _redirect, _key = _settings()
    code, data = _request("POST", TOKEN, data={"client_key": client, "client_secret": secret,
                           "grant_type": "refresh_token", "refresh_token": item["refresh_token"]})
    if code != 200 or data.get("error"):
        raise AccountError("reauthorize")
    state.check_work_allowed()
    refreshed = _validated_tokens(data, scopes)
    vault.save("tiktok", address, refreshed)
    return refreshed["access_token"], refreshed["scopes"]


def read(address: str, kind: str) -> dict[str, Any]:
    address = vault.address(address)
    if kind not in {"profile", "videos"}:
        raise AccountError("endpoint")
    state.check_work_allowed()
    with _lock:
        token, scopes = _token(address)
        auth = {"Authorization": "Bearer " + token}
        if kind == "videos":
            if "video.list" not in scopes:
                raise AccountError("scope")
            code, data = _request("POST", VIDEOS, headers=auth, params={"fields": "id,title,video_description,create_time"},
                                  json={"max_count": 20})
        else:
            code, data = _request("GET", PROFILE, headers=auth, params={"fields": "display_name"})
    state.check_work_allowed()
    error = data.get("error")
    if code != 200 or (isinstance(error, dict) and error.get("code") not in {None, "ok"}):
        raise AccountError("provider")
    payload = data.get("data") if isinstance(data.get("data"), dict) else {}
    if kind == "profile":
        user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
        name = str(user.get("display_name") or "")[:200]
        return {"kind": kind, "items": [{"title": name, "description": ""}] if name else []}
    videos = payload.get("videos") if isinstance(payload.get("videos"), list) else []
    return {"kind": kind, "items": [{"title": str(x.get("title") or "Video")[:200],
                                      "description": str(x.get("video_description") or "")[:2000]}
                                     for x in videos[:20] if isinstance(x, dict)]}


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
            item = vault.load("tiktok", address)
        except AccountError as exc:
            if str(exc) not in {"vault", "not_configured"}:
                raise
            return {"connected": False, "revoked": not vault.remove("tiktok", address)}
        if not item:
            return {"connected": False, "revoked": True}
        client, secret, _redirect, _key = _settings()
        try:
            code, data = _request("POST", REVOKE, data={"client_key": client, "client_secret": secret,
                                  "token": item.get("access_token", "")})
            revoked = code == 200 and not data.get("error")
        except AccountError:
            revoked = False
        vault.remove("tiktok", address)
        return {"connected": False, "revoked": revoked}
