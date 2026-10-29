"""User-owned AI providers, isolated and encrypted per sender.

Only deliberately selected personal models are ever called. Credentials are
sealed by the client before a durable WebXDC update and encrypted again at rest.
Model discovery reads metadata only; it cannot prove generation capabilities.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import ssl
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import config, egress, state

MAX_PROVIDERS = 8
MAX_MODELS = 200
MAX_RESPONSE = 16_000_000
PREFIX = "personal-"
AAD = b"michiflin-personal-ai-v1"
_lock = threading.RLock()
_challenges: dict[str, tuple[str, float]] = {}


class PersonalError(Exception):
    """A short, sanitized error code; provider bodies and keys never go here."""


def _sender(address: str) -> str:
    value = str(address or "").strip().casefold()
    if not value or len(value) > 320 or any(ord(c) < 33 for c in value):
        raise PersonalError("identity")
    return value


def _secret() -> bytes:
    value = os.getenv("WEB_SESSION_KEY", "").strip().encode()
    if len(value) < 32:
        raise PersonalError("key_unavailable")
    return value


def _derive(purpose: bytes, address: str = "") -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"Michiflin personal AI v1",
                info=purpose + b"\0" + address.encode()).derive(_secret())


def _fernet(purpose: bytes, address: str = "") -> Fernet:
    return Fernet(base64.urlsafe_b64encode(_derive(purpose, address)))


def _root() -> Path:
    path = Path(config.CONFIG_DIR) / "personal-ai-v1"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _write(path: Path, data: bytes) -> None:
    # Same directory: os.replace is atomic, permissions are private before data is written.
    tmp = path.with_name("." + path.name + "." + secrets.token_hex(8))
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _path(address: str) -> Path:
    address = _sender(address)
    digest = hmac.new(_derive(b"filenames"), address.encode(), hashlib.sha256).hexdigest()
    return _root() / (digest + ".enc")


def _load(address: str) -> dict[str, Any]:
    address = _sender(address)
    with _lock:
        # The encrypted transport key also acts as a canary for master-key
        # rotation; otherwise a different HMAC filename would look like an empty vault.
        if (_root() / "transport.enc").is_file():
            _transport_key()
        path = _path(address)
        if not path.exists():
            return {"version": 1, "owner": address, "providers": []}
        try:
            raw = path.read_bytes()
            if len(raw) > 4_000_000:
                raise PersonalError("vault")
            data = json.loads(_fernet(b"vault", address).decrypt(raw))
            if (not isinstance(data, dict) or data.get("version") != 1 or data.get("owner") != address
                    or not isinstance(data.get("providers"), list)):
                raise PersonalError("vault")
            return data
        except (InvalidToken, ValueError, TypeError, OSError, json.JSONDecodeError):
            raise PersonalError("vault") from None


def _save(address: str, data: dict[str, Any]) -> None:
    address = _sender(address)
    try:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
        if len(payload) > 2_500_000:
            raise PersonalError("vault")
        _write(_path(address), _fernet(b"vault", address).encrypt(payload))
    except (OSError, ValueError, TypeError):
        raise PersonalError("vault") from None


def _transport_key() -> rsa.RSAPrivateKey:
    with _lock:
        path = _root() / "transport.enc"
        try:
            if path.exists():
                key = serialization.load_pem_private_key(_fernet(b"transport").decrypt(path.read_bytes()), password=None)
                if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
                    raise PersonalError("vault")
                return key
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            raw = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
            _write(path, _fernet(b"transport").encrypt(raw))
            return key
        except (InvalidToken, ValueError, TypeError, OSError):
            raise PersonalError("key_unavailable") from None


def handshake(address: str) -> dict[str, str]:
    address = _sender(address)
    key = _transport_key()
    raw = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    challenge = secrets.token_urlsafe(32)
    with _lock:
        now = time.monotonic()
        for who, (_, expiry) in list(_challenges.items()):
            if expiry <= now:
                _challenges.pop(who, None)
        _challenges[address] = (challenge, now + 900)
    return {"algorithm": "RSA-OAEP-256+A256GCM", "public_key": base64.b64encode(raw).decode(),
            "challenge": challenge}


def unseal(address: str, envelope: Any) -> dict[str, Any]:
    """Decrypt once and bind the encrypted payload to this sender's challenge."""
    address = _sender(address)
    if not isinstance(envelope, dict):
        raise PersonalError("envelope")
    try:
        wrapped = base64.b64decode(str(envelope.get("key") or ""), validate=True)
        iv = base64.b64decode(str(envelope.get("iv") or ""), validate=True)
        ciphertext = base64.b64decode(str(envelope.get("data") or ""), validate=True)
        if len(wrapped) != 256 or len(iv) != 12 or len(ciphertext) > 12_000:
            raise ValueError("size")
        key = _transport_key().decrypt(wrapped, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                                                             algorithm=hashes.SHA256(), label=None))
        if len(key) != 32:
            raise ValueError("key")
        data = json.loads(AESGCM(key).decrypt(iv, ciphertext, AAD))
        if not isinstance(data, dict):
            raise ValueError("payload")
        challenge = str(data.pop("challenge", ""))
        with _lock:
            item = _challenges.pop(address, None)
        if not item or item[1] <= time.monotonic() or not hmac.compare_digest(item[0], challenge):
            raise PersonalError("challenge")
        return data
    except PersonalError:
        raise
    except Exception:
        # Includes authentication/decryption failure. Do not log ciphertext or plaintext.
        raise PersonalError("envelope") from None


def _normalize_base(value: Any, kind: str) -> str:
    if not isinstance(value, str) or len(value) > 1000 or any(ord(c) < 33 for c in value):
        raise PersonalError("url")
    try:
        parsed = urlsplit(value.strip())
        if (parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or "\\" in parsed.netloc or "\\" in parsed.path):
            raise PersonalError("url")
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        port = parsed.port or 443
        if not 1 <= port <= 65535 or "%" in host:
            raise PersonalError("url")
        netloc = ("[" + host + "]" if ":" in host else host) + ((":" + str(port)) if port != 443 else "")
        path = parsed.path.rstrip("/")
        if not path:
            path = "/v1beta" if kind == "gemini" else "/v1"
        return urlunsplit(("https", netloc, path, "", ""))
    except (ValueError, UnicodeError):
        raise PersonalError("url") from None


def detect_kind(base: str) -> str:
    try:
        host = (urlsplit(base).hostname or "").lower()
    except ValueError:
        return "openai"
    if host == "generativelanguage.googleapis.com":
        return "gemini"
    if host == "api.anthropic.com":
        return "anthropic"
    return "openai"  # compatible is an assumption for unknown hosts, not verification


class _PinnedHTTPS(http.client.HTTPSConnection):
    def connect(self) -> None:
        # egress rejects all private/mixed DNS answers and pins the validated IP.
        upstream = egress._proxy(config.WEB_PROXY) if config.WEB_PROXY else None
        raw = egress._connect(self.host, self.port, upstream)
        try:
            raw.settimeout(float(self.timeout or 30))
            self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def _request(provider: dict[str, Any], method: str, suffix: str, body: dict[str, Any] | None = None,
             *, limit: int = MAX_RESPONSE, timeout: int = 90) -> dict[str, Any]:
    state.check_work_allowed()
    parsed = urlsplit(provider["base"])
    host, port = parsed.hostname or "", parsed.port or 443
    headers = {"Accept": "application/json", "Accept-Encoding": "identity", "User-Agent": "Michiflin/1"}
    key, kind = provider["key"], provider["kind"]
    if kind == "gemini":
        headers["x-goog-api-key"] = key
    elif kind == "anthropic":
        headers["x-api-key"] = key
        headers["anthropic-version"] = "2023-06-01"
    else:
        headers["Authorization"] = "Bearer " + key
    raw_body = None
    if body is not None:
        raw_body = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        if len(raw_body) > 3_000_000:
            raise PersonalError("size")
        headers["Content-Type"] = "application/json"
    connection = _PinnedHTTPS(host, port, timeout=timeout)
    try:
        connection.request(method, parsed.path.rstrip("/") + suffix, body=raw_body, headers=headers)
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise PersonalError("redirect")  # never forward keys to an arbitrary Location
        if response.status in (401, 403):
            raise PersonalError("auth")
        if response.status in (404, 405):
            raise PersonalError("unsupported")
        if response.status == 429:
            raise PersonalError("quota")
        if response.status >= 400:
            raise PersonalError("provider")
        raw = response.read(limit + 1)
        if len(raw) > limit:
            raise PersonalError("size")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise PersonalError("response")
        state.check_work_allowed()
        return data
    except PersonalError:
        raise
    except (OSError, ValueError, TypeError, http.client.HTTPException, ssl.SSLError, egress.EgressError):
        raise PersonalError("connection") from None
    finally:
        connection.close()


def _capability(value: Any) -> bool:
    return value is True or (isinstance(value, dict) and value.get("supported") is True)


def _safe_model_id(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.removeprefix("models/").strip()
    if not value or len(value) > 120 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        return ""
    return value


def _catalog(provider: dict[str, Any]) -> list[dict[str, Any]]:
    kind = provider["kind"]
    suffix = "/models?pageSize=100" if kind == "gemini" else "/models?limit=100" if kind == "anthropic" else "/models"
    all_rows: list[Any] = []
    for _ in range(3):
        data = _request(provider, "GET", suffix, limit=2_000_000, timeout=30)
        rows = data.get("models" if kind == "gemini" else "data")
        if not isinstance(rows, list):
            raise PersonalError("response")
        all_rows.extend(rows)
        if len(all_rows) >= MAX_MODELS:
            break
        if kind == "gemini" and isinstance(data.get("nextPageToken"), str):
            token = data["nextPageToken"]
            suffix = "/models?pageSize=100&pageToken=" + quote(token[:500], safe="")
        elif kind == "anthropic" and data.get("has_more") is True and isinstance(data.get("last_id"), str):
            suffix = "/models?limit=100&after_id=" + quote(data["last_id"][:200], safe="")
        else:
            break
    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in all_rows:
        if not isinstance(row, dict):
            continue
        mid = _safe_model_id(row.get("name") if kind == "gemini" else row.get("id"))
        if not mid or mid in seen:
            continue
        seen.add(mid)
        title = str(row.get("displayName") or row.get("display_name") or mid).strip()[:120] or mid
        methods = row.get("supportedGenerationMethods") if isinstance(row.get("supportedGenerationMethods"), list) else []
        caps = row.get("capabilities") if isinstance(row.get("capabilities"), dict) else {}
        inputs = row.get("input_modalities") if isinstance(row.get("input_modalities"), list) else []
        outputs = row.get("output_modalities") if isinstance(row.get("output_modalities"), list) else []
        vision = _capability(caps.get("image_input")) or "image" in inputs
        # Known names indicate likely behavior, not a charged generation test.
        openai_image = kind == "openai" and mid.startswith("gpt-image-")
        gemini_image = kind == "gemini" and mid.startswith("gemini-") and "-image" in mid
        image = openai_image or gemini_image or "image" in outputs
        if kind == "gemini":
            chat = "generateContent" in methods and not image
        elif kind == "anthropic":
            chat = True
        else:
            definitely_other = mid.startswith(("text-embedding-", "whisper-", "tts-", "omni-moderation-", "dall-e-"))
            chat = not image and not definitely_other
        if not (chat or image):
            continue
        maximum = row.get("max_tokens") if kind == "anthropic" else row.get("outputTokenLimit")
        maximum = maximum if isinstance(maximum, int) and 1 <= maximum <= 2_000_000 else 0
        models.append({"id": mid, "title": title, "chat": chat, "vision": vision, "image": image,
                       "max_output": maximum, "source": "metadata" if vision or maximum else "inferred"})
        if len(models) >= MAX_MODELS:
            break
    return models


def add(address: str, values: dict[str, Any]) -> dict[str, Any]:
    address = _sender(address)
    if not isinstance(values, dict):
        raise PersonalError("input")
    raw_base = str(values.get("base") or "").strip()
    kind = str(values.get("kind") or "auto")
    kind = detect_kind(raw_base) if kind == "auto" else kind
    if kind not in {"openai", "anthropic", "gemini"}:
        raise PersonalError("protocol")
    base = _normalize_base(raw_base, kind)
    key = values.get("key")
    if not isinstance(key, str) or not 1 <= len(key) <= 1024 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise PersonalError("key")
    name = " ".join(str(values.get("name") or urlsplit(base).hostname or "Personal").split())[:48]
    try:
        daily = int(values.get("daily") or 0)
    except (ValueError, TypeError):
        raise PersonalError("limit") from None
    if not 0 <= daily <= 100_000:
        raise PersonalError("limit")
    provider: dict[str, Any] = {"id": PREFIX + secrets.token_hex(12), "name": name, "kind": kind,
                                "base": base, "key": key, "daily": daily, "models": []}
    try:
        provider["models"] = _catalog(provider)
        discovery = "metadata"
    except PersonalError as exc:
        if str(exc) != "unsupported":
            raise
        discovery = "unsupported"  # user can add a model manually; no paid generation probe
    state.check_work_allowed()
    with _lock:
        data = _load(address)
        existing = next((p for p in data["providers"] if p.get("base") == base and p.get("key") == key), None)
        if existing:
            # Preserve explicit capability choices while refreshing identical credentials.
            provider["id"] = existing["id"]
            _merge_models(provider, existing)
            data["providers"].remove(existing)
        elif len(data["providers"]) >= MAX_PROVIDERS:
            raise PersonalError("provider_limit")
        data["providers"].append(provider)
        _save(address, data)
    return {"providers": public_list(address), "discovery": discovery}


def _merge_models(new: dict[str, Any], old: dict[str, Any]) -> None:
    # User-confirmed roles are not lost on a catalog refresh. Manual IDs survive too.
    prior = {item.get("id"): item for item in old.get("models", []) if isinstance(item, dict)}
    current = {item.get("id") for item in new.get("models", [])}
    for item in new.get("models", []):
        saved = prior.get(item.get("id"))
        if saved and saved.get("source") == "manual":
            item.update({k: saved[k] for k in ("chat", "vision", "image", "source") if k in saved})
    for mid, item in prior.items():
        if mid not in current and item.get("source") == "manual" and len(new["models"]) < MAX_MODELS:
            new["models"].append(item)


def _find(data: dict[str, Any], provider_id: str) -> dict[str, Any]:
    item = next((p for p in data["providers"] if isinstance(p, dict) and p.get("id") == provider_id), None)
    if not item:
        raise PersonalError("not_found")
    return item


def public_list(address: str) -> list[dict[str, Any]]:
    data = _load(address)
    out = []
    for p in data["providers"]:
        if not isinstance(p, dict):
            continue
        out.append({"id": p.get("id", ""), "name": p.get("name", ""), "kind": p.get("kind", ""),
                    "base": p.get("base", ""), "daily": p.get("daily", 0), "attempts": attempts(str(p.get("id") or "")),
                    "models": [{k: m.get(k) for k in ("id", "title", "chat", "vision", "image", "max_output", "source")}
                               for m in p.get("models", []) if isinstance(m, dict)]})
    return out


def refresh(address: str, provider_id: str) -> list[dict[str, Any]]:
    address = _sender(address)
    with _lock:
        old = dict(_find(_load(address), provider_id))
    # Never keep the global vault lock during a slow or unresponsive provider.
    catalog = _catalog(old)
    with _lock:
        data = _load(address)
        current = _find(data, provider_id)
        if (current.get("base"), current.get("key"), current.get("kind")) != (old.get("base"), old.get("key"), old.get("kind")):
            raise PersonalError("not_found")
        latest = {**current, "models": catalog}
        _merge_models(latest, current)
        data["providers"][data["providers"].index(current)] = latest
        _save(address, data)
    return public_list(address)


def configure_model(address: str, provider_id: str, model_id: str, chat: Any, vision: Any, image: Any) -> list[dict[str, Any]]:
    address, model_id = _sender(address), _safe_model_id(model_id)
    if not model_id or any(type(v) is not bool for v in (chat, vision, image)):
        raise PersonalError("input")
    if vision and not chat:
        raise PersonalError("input")
    with _lock:
        data = _load(address)
        provider = _find(data, provider_id)
        if image and provider["kind"] not in {"openai", "gemini"}:
            raise PersonalError("unsupported")
        model = next((m for m in provider["models"] if m.get("id") == model_id), None)
        if not model:
            if len(provider["models"]) >= MAX_MODELS:
                raise PersonalError("model_limit")
            model = {"id": model_id, "title": model_id, "max_output": 0}
            provider["models"].append(model)
        model.update({"chat": chat, "vision": vision, "image": image, "source": "manual"})
        _save(address, data)
    return public_list(address)


def edit_provider(address: str, provider_id: str, name: Any, daily: Any) -> list[dict[str, Any]]:
    address = _sender(address)
    if not isinstance(name, str) or not name.strip() or len(name) > 48:
        raise PersonalError("input")
    try:
        limit = int(daily)
    except (ValueError, TypeError):
        raise PersonalError("limit") from None
    if not 0 <= limit <= 100_000:
        raise PersonalError("limit")
    with _lock:
        data = _load(address)
        provider = _find(data, provider_id)
        provider["name"] = " ".join(name.split())
        provider["daily"] = limit
        _save(address, data)
    return public_list(address)


def remove(address: str, provider_id: str) -> list[dict[str, Any]]:
    address = _sender(address)
    with _lock:
        data = _load(address)
        item = _find(data, provider_id)
        data["providers"].remove(item)
        _save(address, data)
    return public_list(address)


def lookup(address: str, key: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(key, str) or ":" not in key or not key.startswith(PREFIX):
        raise PersonalError("not_found")
    provider_id, model_id = key.split(":", 1)
    data = _load(address)
    provider = _find(data, provider_id)
    model = next((m for m in provider.get("models", []) if isinstance(m, dict) and m.get("id") == model_id), None)
    if not model:
        raise PersonalError("not_found")
    return provider, model


def providers(address: str) -> list[Any]:
    # Import lazily to avoid a module cycle. Personal providers are excluded from
    # ai.providers()/candidates() and are never automatic fallbacks.
    from . import ai
    out = []
    for item in public_list(address):
        models = []
        for m in item["models"]:
            if not m.get("chat"):
                continue
            roles = ["chat", "code"] + (["vision"] if m.get("vision") else [])
            models.append(ai.Model(item["id"], m["id"], set(roles), 0, label=m["title"], weight=0))
        if models:
            out.append(ai.Provider(item["id"], item["name"], item["kind"], "", "", models=models,
                                   daily=int(item.get("daily") or 0), personal=True))
    return out


def image_sources(address: str) -> list[Any]:
    from . import images
    out = []
    for p in public_list(address):
        for m in p["models"]:
            if m.get("image"):
                out.append(images.Source(p["id"] + ":" + m["id"], p["name"] + " · " + m["title"],
                                         int(p.get("daily") or 0), personal=True))
    return out


def _budget(provider: dict[str, Any]) -> None:
    """Reserve a conservative daily attempt before network I/O, even on timeout.

    This local limit is not a billing guarantee: restores, provider pricing and
    use of the same key outside Michiflin are outside its control.
    """
    daily = int(provider.get("daily") or 0)
    today = state.utc_day()
    with state.lock:
        budgets = state.data.setdefault("personal_ai_budgets", {})
        row = budgets.setdefault(today, {})
        count = int(row.get(provider["id"], 0))
        if daily and count >= daily:
            raise PersonalError("quota")
        row[provider["id"]] = count + 1
        # Keep one current-day counter; a restored older backup can still lose it.
        for day in list(budgets):
            if day < today:
                budgets.pop(day, None)
        try:
            state.save()
        except OSError:
            row[provider["id"]] = count
            raise PersonalError("vault") from None


def attempts(provider_id: str) -> int:
    with state.lock:
        return int((state.data.get("personal_ai_budgets") or {}).get(state.utc_day(), {}).get(provider_id, 0))


def _parts(content: Any, kind: str) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"text": content}] if kind == "gemini" else [{"type": "text", "text": content}]
    if not isinstance(content, list):
        raise PersonalError("input")
    parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            parts.append({"text": str(item.get("text") or "")} if kind == "gemini" else
                         {"type": "text", "text": str(item.get("text") or "")})
        elif item.get("type") == "image_url":
            url = (item.get("image_url") or {}).get("url", "") if isinstance(item.get("image_url"), dict) else ""
            match = re.fullmatch(r"data:(image/(?:png|jpeg|gif|webp));base64,([A-Za-z0-9+/=]+)", str(url))
            if not match:
                raise PersonalError("input")
            mime, data = match.groups()
            parts.append({"inline_data": {"mime_type": mime, "data": data}} if kind == "gemini" else
                         {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}})
    return parts


def complete(address: str, key: str, messages: list[dict[str, Any]], max_tokens: int) -> tuple[str, int, str]:
    provider, model = lookup(address, key)
    if not model.get("chat"):
        raise PersonalError("unsupported")
    _budget(provider)
    kind, mid = provider["kind"], model["id"]
    cap = max(1, min(int(max_tokens), int(model.get("max_output") or max_tokens)))
    if kind == "openai":
        body: dict[str, Any] = {"model": mid, "messages": messages}
        if (urlsplit(provider["base"]).hostname or "") == "api.openai.com":
            body["max_completion_tokens"] = cap
        else:
            body["max_tokens"] = cap
        data = _request(provider, "POST", "/chat/completions", body)
        try:
            choice = data["choices"][0]
            value = choice["message"].get("content")
            text = value if isinstance(value, str) else "".join(str(p.get("text") or "") for p in value if isinstance(p, dict))
            tokens = int((data.get("usage") or {}).get("total_tokens") or 0)
            finish = str(choice.get("finish_reason") or "")
        except (KeyError, IndexError, TypeError, ValueError):
            raise PersonalError("response") from None
    elif kind == "gemini":
        system = "\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "system")
        contents = [{"role": "model" if m.get("role") == "assistant" else "user", "parts": _parts(m.get("content"), kind)}
                    for m in messages if m.get("role") in {"user", "assistant"}]
        body = {"contents": contents, "generationConfig": {"maxOutputTokens": cap}}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        data = _request(provider, "POST", "/models/" + quote(mid, safe="-._~") + ":generateContent", body)
        try:
            choice = data["candidates"][0]
            text = "".join(str(p.get("text") or "") for p in choice["content"]["parts"]
                           if isinstance(p, dict) and not p.get("thought"))
            tokens = int((data.get("usageMetadata") or {}).get("totalTokenCount") or 0)
            finish = str(choice.get("finishReason") or "").lower()
        except (KeyError, IndexError, TypeError, ValueError):
            raise PersonalError("response") from None
    else:
        system = "\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "system")
        turns = [{"role": m["role"], "content": _parts(m.get("content"), kind)}
                 for m in messages if m.get("role") in {"user", "assistant"}]
        body = {"model": mid, "max_tokens": cap, "messages": turns}
        if system:
            body["system"] = system
        data = _request(provider, "POST", "/messages", body)
        try:
            text = "".join(str(p.get("text") or "") for p in data["content"] if isinstance(p, dict) and p.get("type") == "text")
            usage = data.get("usage") or {}
            tokens = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
            finish = str(data.get("stop_reason") or "").lower()
        except (KeyError, TypeError, ValueError):
            raise PersonalError("response") from None
    if not text.strip():
        raise PersonalError("response")
    return text.strip(), tokens, finish


def image(address: str, key: str, prompt: str) -> bytes:
    provider, model = lookup(address, key)
    if not model.get("image"):
        raise PersonalError("unsupported")
    _budget(provider)
    kind, mid = provider["kind"], model["id"]
    if kind == "openai":
        body: dict[str, Any] = {"model": mid, "prompt": prompt, "n": 1}
        if (urlsplit(provider["base"]).hostname or "") != "api.openai.com":
            body["response_format"] = "b64_json"
        data = _request(provider, "POST", "/images/generations", body, timeout=120)
        try:
            encoded = data["data"][0]["b64_json"]
        except (KeyError, IndexError, TypeError):
            raise PersonalError("response") from None
    elif kind == "gemini":
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}}
        data = _request(provider, "POST", "/models/" + quote(mid, safe="-._~") + ":generateContent", body, timeout=120)
        try:
            parts = data["candidates"][0]["content"]["parts"]
            inline = next((p.get("inlineData") or p.get("inline_data") for p in parts
                           if isinstance(p, dict) and (p.get("inlineData") or p.get("inline_data"))), None)
            encoded = inline["data"]
        except (KeyError, IndexError, TypeError):
            raise PersonalError("response") from None
    else:
        raise PersonalError("unsupported")
    try:
        result = base64.b64decode(encoded, validate=True)
    except (TypeError, ValueError):
        raise PersonalError("response") from None
    if not result or len(result) > 11_000_000:
        raise PersonalError("size")
    return result
