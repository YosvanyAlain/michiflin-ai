"""AI provider registry with free-tier discovery, quota tracking and fair routing.

Each model has roles (chat, fast, code, reason, long, vision). A request asks for a
role; candidates are ordered by the fraction of their daily free allowance already
used (per model and per provider), so load spreads evenly across providers. Quota,
auth and model errors put the model or provider on a local cooldown and the next
candidate is tried silently.
"""
from __future__ import annotations

import base64
import contextlib
import contextvars
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlsplit
import ipaddress

import requests

from . import config, progress, state

log = logging.getLogger("michiflin")
# The agent never touches a model: inside isolated() every model call fails closed without a request.
_blocked: contextvars.ContextVar[bool] = contextvars.ContextVar("ai_blocked", default=False)


def blocked() -> bool:
    return _blocked.get()


@contextlib.contextmanager
def isolated():
    token = _blocked.set(True)
    try:
        yield
    finally:
        _blocked.reset(token)
from .util import strip_thinking

ROLES = ("chat", "fast", "code", "reason", "long", "vision")
# Models that always think before answering and return nothing when the budget is too small.
REASONING_FLOOR = re.compile(r"hy4|hy3")


@dataclass
class Model:
    provider: str
    model: str
    roles: set[str]
    daily: int
    weight: float = 1.0
    label: str = ""

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model}"

    def title(self) -> str:
        return self.label or self.model.split("/")[-1]


@dataclass
class Provider:
    key: str
    name: str
    kind: str  # "openai" or "gemini"
    base_url: str
    api_key: str = ""
    daily: int = 0  # shared daily allowance across the provider's models (0 = per model only)
    headers: dict[str, str] = field(default_factory=dict)
    models: list[Model] = field(default_factory=list)
    custom: bool = False
    personal: bool = False


@dataclass
class AIResult:
    text: str = ""
    provider: str = ""
    model: str = ""
    error: str = ""
    seconds: float = 0.0
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.text)

    def signature(self) -> str:
        if not self.ok:
            return ""
        took = f" · {self.seconds:.0f} s" if self.seconds >= 1 else ""
        return f"{self.model} · {self.provider}{took}"


_lock = threading.RLock()
_providers: dict[str, Provider] = {}
_last_used: dict[str, float] = {}
_ovh_locks: dict[str, threading.Lock] = {}


def _m(provider: str, model: str, roles: str, daily: int, weight: float = 1.0, label: str = "") -> Model:
    return Model(provider, model, set(roles.split(",")), daily, weight, label)


def build_registry() -> None:
    """Create the static registry from configured keys; discovery refines it later."""
    providers: list[Provider] = []
    if config.GEMINI_API_KEY:
        gemini = Provider("gemini", "Gemini", "gemini", "https://generativelanguage.googleapis.com/v1beta", config.GEMINI_API_KEY)
        gemini.models = [
            _m("gemini", "gemini-3.5-flash-lite", "chat,fast,long,vision", 1000, label="Gemini 3.5 Flash-Lite"),
            _m("gemini", "gemini-3.1-flash-lite", "chat,fast,long,vision", 1000, label="Gemini 3.1 Flash-Lite"),
            _m("gemini", "gemini-3.5-flash", "chat,reason,code,long,vision", 250, label="Gemini 3.5 Flash"),
            _m("gemini", "gemini-3.6-flash", "chat,reason,code,long,vision", 250, label="Gemini 3.6 Flash"),
            _m("gemini", "gemini-3.8-flash", "reason,code,long,vision", 250, 0.5, "Gemini 3.8 Flash"),
        ]
        providers.append(gemini)
    if config.GROQ_API_KEY:
        groq = Provider("groq", "Groq", "openai", "https://api.groq.com/openai/v1", config.GROQ_API_KEY)
        groq.models = [
            _m("groq", "openai/gpt-oss-20b", "chat,fast", 1000, label="GPT-OSS 20B"),
            _m("groq", "openai/gpt-oss-120b", "chat,reason,code", 1000, label="GPT-OSS 120B"),
            _m("groq", "qwen/qwen3.8-27b", "chat,code,reason,fast,vision", 1000, label="Qwen 3.8 27B"),
        ]
        providers.append(groq)
    if config.MISTRAL_API_KEY:
        mistral = Provider("mistral", "Mistral", "openai", "https://api.mistral.ai/v1", config.MISTRAL_API_KEY)
        mistral.models = [
            # Free-plan keys only reach the Ministral family and Codestral; Small, Medium and Large answer 429/403.
            _m("mistral", "ministral-14b-latest", "chat,reason,long,vision", 800, label="Ministral 14B"),
            _m("mistral", "ministral-8b-latest", "chat,fast,vision", 1200, label="Ministral 8B"),
            _m("mistral", "codestral-latest", "code", 800, label="Codestral"),
        ]
        providers.append(mistral)
    if config.ZAI_API_KEY:
        zai = Provider("zai", "Z.AI", "openai", "https://api.z.ai/api/paas/v4", config.ZAI_API_KEY, headers={"Accept-Language": "en-US,en"})
        zai.models = [
            _m("zai", "glm-4.7-flash", "chat,code,reason", 1500, label="GLM-4.7 Flash"),
            _m("zai", "glm-4.5-flash", "chat,fast", 1500, label="GLM-4.5 Flash"),
            _m("zai", "glm-4.6v-flash", "vision", 500, label="GLM-4.6V Flash"),
        ]
        providers.append(zai)
    if config.OPENROUTER_API_KEY:
        router = Provider("openrouter", "OpenRouter", "openai", "https://openrouter.ai/api/v1", config.OPENROUTER_API_KEY, daily=50,
                          headers={"X-Title": "Michiflin AI"})
        router.models = [_m("openrouter", "openrouter/free", "chat,reason,code,long,vision", 50, label="OpenRouter Free")]
        providers.append(router)
    if config.CLOUDFLARE_ACCOUNT_ID and config.CLOUDFLARE_AI_TOKEN:
        # Low weight: Cloudflare neurons are shared with image generation.
        cloud = Provider("cloudflare", "Cloudflare", "openai",
                         f"https://api.cloudflare.com/client/v4/accounts/{config.CLOUDFLARE_ACCOUNT_ID}/ai/v1", config.CLOUDFLARE_AI_TOKEN)
        cloud.models = [
            _m("cloudflare", "@cf/meta/llama-3.3-70b-instruct-fp8-fast", "chat", 60, 0, "Llama 3.3 70B"),
            _m("cloudflare", "@cf/qwen/qwen2.5-coder-32b-instruct", "code", 40, 0, "Qwen 2.5 Coder"),
            _m("cloudflare", "@cf/meta/llama-4-scout-17b-16e-instruct", "vision,chat", 40, 0, "Llama 4 Scout"),
        ]
        providers.append(cloud)
    if config.GITHUB_MODELS_TOKEN:
        models = Provider("ghmodels", "GitHub Models", "openai", "https://models.github.ai/inference", config.GITHUB_MODELS_TOKEN)
        models.models = [
            _m("ghmodels", "openai/gpt-4.1-mini", "chat,fast,vision", 150, label="GPT-4.1 mini"),
            _m("ghmodels", "openai/gpt-4.1", "reason,code,long,vision", 50, 0.8, "GPT-4.1"),
        ]
        providers.append(models)
    if config.BAZAARLINK_API_KEY:
        bazaar = Provider("bazaarlink", "BazaarLink", "openai", "https://api.bazaarlink.ai/v1", config.BAZAARLINK_API_KEY)
        bazaar.models = [
            _m("bazaarlink", "qwen/qwen3.7-flash:free", "chat,fast,code,long", 250, label="Qwen 3.7 Flash"),
            _m("bazaarlink", "deepseek/deepseek-v4-flash-0731free:free", "chat,reason,code,long", 250, label="DeepSeek V4 Flash"),
        ]
        providers.append(bazaar)
    if config.ORCAROUTER_API_KEY:
        # Free tier without top-up: about 50 requests a day shared, plus a separate Hy4 allowance.
        orca = Provider("orcarouter", "OrcaRouter", "openai", "https://api.orcarouter.ai/v1", config.ORCAROUTER_API_KEY)
        orca.models = [
            _m("orcarouter", "tencent/hy4-preview-free", "chat,reason,code,long", 240, label="Hunyuan Hy4"),
            _m("orcarouter", "deepseek/deepseek-v4-flash-free", "chat,fast,reason,code", 20, label="DeepSeek V4 Flash"),
            _m("orcarouter", "z-ai/glm-5.3-flash-free", "chat,code", 15, label="GLM-5.3 Flash"),
            _m("orcarouter", "tencent/hy3-free", "chat,long", 15, label="Hunyuan Hy3"),
        ]
        providers.append(orca)
    if config.SAMBANOVA_API_KEY:
        # Free tier currently documents 20 requests/day and 200k tokens/day per production model.
        samba = Provider("sambanova", "SambaNova", "openai", "https://api.sambanova.ai/v1", config.SAMBANOVA_API_KEY)
        samba.models = [
            _m("sambanova", "DeepSeek-V3.1", "chat,reason,code,long", 20, label="DeepSeek V3.1"),
            _m("sambanova", "Meta-Llama-3.3-70B-Instruct", "chat,long", 20, label="Llama 3.3 70B"),
            _m("sambanova", "gpt-oss-120b", "chat,reason,code,long", 20, label="GPT-OSS 120B"),
        ]
        providers.append(samba)
    if config.FREE_GATEWAYS:
        # Anonymous access only: attaching a paid OVH key would change billing.
        # The public endpoint is rate-limited per IP and per model (2/minute).
        ovh = Provider("ovh", "OVHcloud · anonymous", "openai", "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1", daily=1000)
        ovh.models = [
            _m("ovh", "gpt-oss-120b", "chat,reason,code,long", 500, label="GPT-OSS 120B"),
            _m("ovh", "Qwen3.6-27B", "chat,reason,code,long,vision", 500, label="Qwen 3.6 27B"),
        ]
        providers.append(ovh)
        # Anonymous: 10 requests/min, 60/h, 500k tokens a day (twice that with a free token).
        llm7 = Provider("llm7", "LLM7", "openai", "https://api.llm7.io/v1", config.LLM7_API_KEY, daily=600 if config.LLM7_API_KEY else 300)
        llm7.models = [
            _m("llm7", "codestral-latest", "code,fast", 300, label="Codestral"),
            _m("llm7", "GLM-5.3-Flash", "chat,code,reason,long", 300, label="GLM-5.3 Flash"),
            _m("llm7", "gpt-oss:20b", "chat,fast", 300, label="GPT-OSS 20B"),
        ]
        # Free models only: 200 requests an hour per IP, anonymous or not.
        kilo = Provider("kilo", "Kilo", "openai", "https://api.kilo.ai/api/gateway", config.KILO_API_KEY, daily=1500)
        kilo.models = [
            # These current free endpoints are explicitly coding-capable; discovery retains
            # the roles while removing them if the gateway stops listing them as free.
            _m("kilo", "reflection/beam-free", "chat,reason,code,long", 400, label="Beam Free"),
            _m("kilo", "recursive/flint-v1.0:free", "chat,reason,code,long", 400, label="Flint Free"),
            _m("kilo", "nvidia/nemotron-3-ultra-550b-a55b:free", "chat,reason,code,long", 400, label="Nemotron 3 Ultra"),
            _m("kilo", "poolside/laguna-s-2.1:free", "code,long", 400, 0.5, "Laguna S 2.1"),
            _m("kilo", "cohere/north-mini-code:free", "code,long", 400, label="North Mini Code"),
            _m("kilo", "kilo-auto/free", "chat,code,long", 400, 0.5, "Kilo Auto Free"),
        ]
        providers += [llm7, kilo]
    providers.extend(_extra_providers())
    with _lock:
        _providers.clear()
        _providers.update({provider.key: provider for provider in providers})


def _safe_extra_endpoint(base_url: str) -> bool:
    """Extra API keys only go to explicitly allowlisted HTTPS hosts, never arbitrary URLs."""
    try:
        parsed = urlsplit(base_url)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return False
    try:
        ipaddress.ip_address(host)
        is_ip = True
    except ValueError:
        is_ip = False
    return (parsed.scheme == "https" and bool(host) and not parsed.username and not parsed.password
            and port in (None, 443) and not parsed.query and not parsed.fragment and not is_ip
            and host in config.AI_EXTRA_ALLOWED_HOSTS)


def _extra_providers() -> list[Provider]:
    """Load optional OpenAI-compatible providers only for explicit trusted HTTPS hosts."""
    if not config.EXTRA_PROVIDERS:
        return []
    try:
        items = json.loads(config.EXTRA_PROVIDERS)
    except (TypeError, ValueError):
        return []
    if not isinstance(items, list):
        return []
    found: list[Provider] = []
    reserved = {"gemini", "groq", "mistral", "zai", "openrouter", "cloudflare", "ghmodels",
                "bazaarlink", "orcarouter", "sambanova", "llm7", "kilo", "ovh"}
    for item in items:
        try:
            if not isinstance(item, dict):
                continue
            key = re.sub(r"[^a-z0-9]", "", str(item["key"]).lower())[:16]
            name = str(item.get("name", key)).strip()[:60]
            base_url = str(item["base_url"]).strip().rstrip("/")
            api_key_env = str(item.get("api_key_env", ""))
            secret = config.env(api_key_env) if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", api_key_env) else ""
            if not key or key in reserved or any(provider.key == key for provider in found):
                continue
            if not secret or not name or not _safe_extra_endpoint(base_url):
                continue
            provider_daily = int(item.get("daily", 0))
            specs = item.get("models", [])
            if not isinstance(specs, list) or not 0 <= provider_daily <= 100000:
                continue
            provider = Provider(key, name, "openai", base_url, secret, provider_daily, custom=True)
            for spec in specs:
                if not isinstance(spec, dict):
                    continue
                model_id = str(spec.get("id", "")).strip()[:160]
                roles = spec.get("roles", ["chat"])
                if not model_id or not isinstance(roles, list) or not roles or any(role not in ROLES for role in roles):
                    continue
                daily = int(spec.get("daily", 200))
                weight = float(spec.get("weight", 1.0))
                if not 1 <= daily <= 100000 or not 0 <= weight <= 2:
                    continue
                provider.models.append(_m(key, model_id, ",".join(roles), daily, weight))
            if provider.models:
                found.append(provider)
        except (KeyError, TypeError, ValueError):
            continue
    return found


# ---------------------------------------------------------------------------
# Free model discovery (runs once in the background at startup).
# ---------------------------------------------------------------------------
def discover() -> None:
    epoch = state.maintenance_generation()
    for function in (_discover_openrouter, _discover_groq, _discover_gemini, _discover_bazaarlink, _discover_orcarouter,
                     _discover_llm7, _discover_kilo):
        if state.maintenance_active() or state.maintenance_generation() != epoch:
            return
        try:
            function()
        except Exception:
            continue
    if not state.maintenance_active() and state.maintenance_generation() == epoch:
        _watch_catalog()


def _discover_openrouter() -> None:
    provider = _providers.get("openrouter")
    if not provider:
        return
    response = requests.get("https://openrouter.ai/api/v1/models", timeout=25)
    response.raise_for_status()
    free = []
    for item in response.json().get("data", []):
        pricing = item.get("pricing") or {}
        if str(pricing.get("prompt")) != "0" or str(pricing.get("completion")) != "0":
            continue
        model_id = str(item.get("id", ""))
        if not model_id or "safety" in model_id or "guard" in model_id or model_id == "openrouter/free":
            continue
        size = max([int(n) for n in re.findall(r"(\d+)b", model_id.lower())] or [0])
        modalities = (item.get("architecture") or {}).get("input_modalities") or []
        roles = {"chat"}
        if size >= 100 or "ultra" in model_id:
            roles.update({"reason", "long"})
        if re.search(r"code|coder|laguna|devstral", model_id):
            roles.add("code")
        if "image" in modalities:
            roles.add("vision")
        free.append((size, model_id, roles))
    free.sort(reverse=True)
    models = [_m("openrouter", model_id, ",".join(sorted(roles)), 50) for _size, model_id, roles in free[:4]]
    models.append(_m("openrouter", "openrouter/free", "chat,reason,code,long,vision", 50, label="OpenRouter Free"))
    with _lock:
        provider.models = models


def _label(model_id: str) -> str:
    name = model_id.split("/")[-1].split(":")[0].removesuffix("-free")
    name = re.sub(r"-\d{4}free$", "", name)
    name = re.sub(r"-a\d+b(?=-|$)|-\d{4}$|-instruct(?=-|$)", "", name, flags=re.I)
    name = re.sub(r"(?<=-)(\d+)b(?=-|$)", lambda match: match.group(1) + "B", name)
    special = {"deepseek": "DeepSeek", "minimax": "MiniMax", "xs": "XS", "gpt": "GPT"}

    def word(part: str) -> str:
        lowered = part.lower()
        if lowered in special:
            return special[lowered]
        if lowered in {"glm", "oss"} or re.fullmatch(r"v\d+(\.\d+)?|\d+b|m\d+(\.\d+)?", lowered):
            return part.upper()
        return part[:1].upper() + part[1:]
    return " ".join(word(part) for part in name.split("-"))


def _discover_bazaarlink() -> None:
    """Keep only zero-priced models; auto:free only re-routes to them."""
    provider = _providers.get("bazaarlink")
    if not provider:
        return
    response = requests.get(f"{provider.base_url}/models", headers={"Authorization": f"Bearer {provider.api_key}"}, timeout=20)
    response.raise_for_status()
    known = {model.model: model for model in provider.models}
    models = []
    for item in response.json().get("data", []):
        pricing = item.get("pricing") or {}
        model_id = str(item.get("id", ""))
        if not model_id or model_id.startswith("auto") or str(pricing.get("prompt")) != "0" or str(pricing.get("completion")) != "0":
            continue
        models.append(known.get(model_id) or _m("bazaarlink", model_id, "chat,code,long", 200, label=_label(model_id)))
    if models:
        with _lock:
            provider.models = models


def _discover_orcarouter() -> None:
    """The free lineup rotates; read the current ids and their own daily ceilings."""
    provider = _providers.get("orcarouter")
    if not provider:
        return
    response = requests.get("https://api.orcarouter.ai/api/free-package/public", timeout=20)
    response.raise_for_status()
    body = response.json().get("data") or {}
    known = {model.model: model for model in provider.models}
    models = []
    for item in body.get("models") or []:
        model_id = str(item.get("free_alias", ""))
        if not model_id.endswith("-free") or "verify" in model_id or not item.get("is_free_tier", True):
            continue
        tiers = [tier for tier in item.get("tiers") or [] if not tier.get("min_paid_usd")]
        model = known.get(model_id) or _m("orcarouter", model_id, "chat,code", 15, label=_label(model_id))
        if tiers and tiers[0].get("rpd"):
            model.daily = max(1, int(tiers[0]["rpd"]) - 10)
        models.append(model)
    if models:
        with _lock:
            provider.models = models


def _gateway_roles(model_id: str, context: int, vision: bool, reasoning: bool) -> str:
    lowered = model_id.lower()
    size = max([int(n) for n in re.findall(r"(\d+)b", lowered)] or [0])
    roles = {"chat"}
    if re.search(r"code|coder|codestral|laguna|devstral", lowered):
        roles.add("code")
    if reasoning or size >= 100 or "ultra" in lowered:
        roles.update({"reason", "code"})
    if context >= 128000:
        roles.add("long")
    if vision:
        roles.add("vision")
    if size and size <= 30 or "flash" in lowered or "mini" in lowered:
        roles.add("fast")
    return ",".join(sorted(roles))


GATEWAY_SKIP = re.compile(r"safety|guard|lyria|embed|whisper|tts|openrouter/|kilo-auto/(?!free)|summarize|extract|lfm-2\.5-2\.6b", re.I)


def _discover_llm7() -> None:
    """Only the turbo tier is free; pro models answer 402."""
    provider = _providers.get("llm7")
    if not provider:
        return
    response = requests.get(f"{provider.base_url}/models", timeout=20)
    response.raise_for_status()
    known = {model.model: model for model in provider.models}
    models = []
    for item in response.json().get("data", []):
        model_id = str(item.get("id", ""))
        if item.get("tier") != "turbo" or item.get("model_type") != "chat" or item.get("usage_based_only") or GATEWAY_SKIP.search(model_id):
            continue
        caps = item.get("capabilities") or {}
        context = int((item.get("context_window") or {}).get("tokens") or 0)
        roles = _gateway_roles(model_id, context, bool(caps.get("vision")), bool(item.get("reasoning")))
        models.append(known.get(model_id) or _m("llm7", model_id, roles, provider.daily, label=_label(model_id)))
    if models:
        with _lock:
            provider.models = models


def _discover_kilo() -> None:
    """Keep the models the gateway flags as free; the lineup rotates."""
    provider = _providers.get("kilo")
    if not provider:
        return
    response = requests.get(f"{provider.base_url}/models", timeout=25)
    response.raise_for_status()
    known = {model.model: model for model in provider.models}
    models = []
    for item in response.json().get("data", []):
        model_id = str(item.get("id", ""))
        pricing = item.get("pricing") or {}
        free = item.get("isFree") or model_id.endswith(":free")
        if not free or str(pricing.get("prompt", "0")) not in {"0", "0.0"} or GATEWAY_SKIP.search(model_id):
            continue
        modalities = (item.get("architecture") or {}).get("input_modalities") or []
        roles = _gateway_roles(model_id, int(item.get("context_length") or 0), "image" in modalities, "reasoning" in model_id)
        weight = 0.5 if re.search(r"^kilo-auto/|laguna|reasoning", model_id) else 1.0
        models.append(known.get(model_id) or _m("kilo", model_id, roles, 400, weight, _label(model_id)))
    if models:
        with _lock:
            provider.models = models[:12]


def _discover_groq() -> None:
    provider = _providers.get("groq")
    if not provider:
        return
    response = requests.get(f"{provider.base_url}/models", headers={"Authorization": f"Bearer {provider.api_key}"}, timeout=20)
    response.raise_for_status()
    available = {str(item.get("id")) for item in response.json().get("data", []) if item.get("active", True)}
    if not available:
        return
    known = {model.model for model in provider.models}
    models = [model for model in provider.models if model.model in available]
    for model_id in sorted(available - known):
        lowered = model_id.lower()
        if re.search(r"whisper|guard|orpheus|tts|safeguard|playai|distil|allam|compound", lowered):
            continue
        roles = "chat,code,reason" if re.search(r"120b|70b|kimi|qwen|maverick|deepseek", lowered) else "chat,fast"
        if re.search(r"scout|maverick|vision", lowered):
            roles += ",vision"
        models.append(_m("groq", model_id, roles, 1000))
    if models:
        with _lock:
            provider.models = models


def _discover_gemini() -> None:
    provider = _providers.get("gemini")
    if not provider:
        return
    response = requests.get(f"{provider.base_url}/models", params={"key": provider.api_key, "pageSize": 200}, timeout=20)
    response.raise_for_status()
    available = {str(item.get("name", "")).removeprefix("models/") for item in response.json().get("models", [])}
    models = [model for model in provider.models if model.model in available]
    if models:
        with _lock:
            provider.models = models


# ---------------------------------------------------------------------------
# Routing.
# ---------------------------------------------------------------------------
def providers() -> list[Provider]:
    with _lock:
        return list(_providers.values())


def provider(key: str) -> Provider | None:
    if key.startswith("personal-"):
        if not state.personal_ai_secure():
            return None
        from . import personal_ai
        try:
            return next((item for item in personal_ai.providers(state.request_sender()) if item.key == key), None)
        except (personal_ai.PersonalError, OSError):
            return None
    with _lock:
        return _providers.get(key)


def _provider_fraction(item: Provider) -> float:
    used = sum(state.usage_today(model.key) for model in item.models)
    capacity = item.daily or sum(model.daily for model in item.models) or 1
    return used / capacity


def _model_fraction(model: Model) -> float:
    remaining = state.remaining_for(model.key)
    if remaining and remaining[1] > 0:
        return 1 - remaining[0] / remaining[1]
    return state.usage_today(model.key) / max(1, model.daily)


def locally_exhausted(model: Model) -> bool:
    item = provider(model.provider)
    if item and item.personal:
        from . import personal_ai
        return bool(item.daily and personal_ai.attempts(item.key) >= item.daily)
    if model.daily > 0 and state.usage_today(model.key) >= model.daily:
        return True
    return bool(item and item.daily > 0 and sum(state.usage_today(other.key) for other in item.models) >= item.daily)


def available(model: Model) -> bool:
    return not state.cooldown_until(model.key) and not state.cooldown_until(model.provider) and not locally_exhausted(model)


def candidates(role: str, pin: str = "") -> list[Model]:
    """Fairly ordered models able to serve a role; a pinned provider goes first."""
    pool: list[tuple[float, float, Model]] = []
    if _blocked.get():
        return []
    for item in providers():
        provider_share = _provider_fraction(item)
        for model in item.models:
            if role not in model.roles or model.weight <= 0 or not available(model):
                continue  # weight 0: only in the model's own chat
            share = max(provider_share, _model_fraction(model))
            if share >= 1:
                continue
            score = share / max(0.05, model.weight)
            pool.append((score, _last_used.get(item.key, 0.0), model))
    pool.sort(key=lambda entry: (round(entry[0], 3), entry[1]))
    ordered = [entry[2] for entry in pool]
    if pin:
        ordered.sort(key=lambda model: 0 if model.provider == pin or model.key == pin else 1)
    return ordered


def role_chain(role: str) -> list[str]:
    fallbacks = {
        "code": ["code", "reason", "chat"],
        "reason": ["reason", "chat"],
        "long": ["long", "reason", "chat"],
        "fast": ["fast", "chat"],
        "vision": ["vision"],
        "chat": ["chat"],
    }
    return fallbacks.get(role, ["chat"])


def accepts(model: Model | None) -> list[str]:
    """Attachment kinds a chat can take: text documents always; images, PDF and audio where the model reads them natively."""
    kinds = ["document"]
    if model is None or "vision" in model.roles:
        kinds.append("image")
    if model is not None and model.provider == "gemini":
        kinds += ["pdf", "audio"]
    return kinds


def model_by_key(key: str) -> Model | None:
    items = providers()
    if str(key).startswith("personal-"):
        item = provider(str(key).split(":", 1)[0])
        items = [item] if item else []
    for item in items:
        for model in item.models:
            if model.key == key:
                return model
    return None


def capacity_left() -> float:
    """Share of today's free AI capacity still unused, over every model in rotation."""
    total = left = 0.0
    for item in providers():
        for model in item.models:
            if model.weight <= 0 or model.daily <= 0:
                continue
            total += model.daily
            if available(model):
                left += max(0.0, 1.0 - _model_fraction(model)) * model.daily
    return left / total if total else 1.0


def _allowance(fast_only: bool) -> tuple[str, bool, str]:
    """Daily allowance and administrators' reserve for the person behind this request: (error, charged, address)."""
    from . import usage

    address = state.request_sender()
    if not address or not state.charge_once():
        return "", False, address
    if state.acting_as():
        if not usage.allowed(address, "ask"):
            state.uncharge()
            return "person_quota", False, address
        if not fast_only and usage.reserve_low():
            state.uncharge()
            return "reserved", False, address
    return "", True, address


def _settle(result: AIResult, charged: bool, address: str) -> AIResult:
    from . import usage

    if charged:
        if result.ok:
            usage.spend(address, "ask")
        else:
            state.uncharge()
    return result


def complete_exact(messages: list[dict[str, Any]], key: str, max_tokens: int | None = None,
                   images: list[tuple[str, bytes]] | None = None, raw: bool = False) -> AIResult:
    """Ask one specific model, without fallback (the mini app's per-model chats)."""
    model = model_by_key(key)
    # A personal provider does not consume the shared free-provider reserve.
    error, charged, address = _allowance(bool(model and ("fast" in model.roles or model.provider.startswith("personal-"))))
    if error:
        return AIResult(error=error)
    return _settle(_complete_exact(model, messages, max_tokens, images, raw), charged, address)


def _complete_exact(model: Model | None, messages: list[dict[str, Any]], max_tokens: int | None,
                    images: list[tuple[str, bytes]] | None, raw: bool) -> AIResult:
    item = provider(model.provider) if model else None
    if model is None or item is None:
        return AIResult(error="unknown_model")
    if _blocked.get():
        return AIResult(error="no_provider")
    if locally_exhausted(model):
        return AIResult(provider=item.name, model=model.title(), error="quota")
    if not available(model):
        return AIResult(provider=item.name, model=model.title(), error="cooldown")
    state.check_work_allowed()
    started = time.monotonic()
    result = _call(item, model, messages, images, max_tokens or config.AI_MAX_OUTPUT_TOKENS, raw)
    state.check_work_allowed()
    if result.ok:
        _last_used[item.key] = time.time()
        result.seconds = time.monotonic() - started
    return result


def complete(messages: list[dict[str, Any]], role: str = "chat", images: list[tuple[str, bytes]] | None = None,
             max_attempts: int = 6, max_tokens: int | None = None, only: str = "", raw: bool = False) -> AIResult:
    """Ask the fairest available model; silently fall back on quota or service errors."""
    from . import usage

    chain = role_chain("vision" if images else role)
    if state.acting_as() and not images and state.request_sender() and usage.reserve_low():
        chain = ["fast"]  # people other than administrators keep the light models when the reserve is reached
    error, charged, address = _allowance(True)
    if error:
        return AIResult(error=error)
    return _settle(_complete(messages, chain, max_attempts, max_tokens, only, raw, images), charged, address)


def _complete(messages: list[dict[str, Any]], chain: list[str], max_attempts: int, max_tokens: int | None, only: str,
              raw: bool, images: list[tuple[str, bytes]] | None) -> AIResult:
    pin = str(state.setting("ai_pin") or "")
    tried: set[str] = set()
    last = AIResult(error="no_provider")
    started = time.monotonic()
    for current_role in chain:
        for model in candidates(current_role, pin):
            state.check_work_allowed()
            if model.key in tried or (only and model.provider != only) or not available(model):
                continue  # a provider-wide pause may have started during this request
            tried.add(model.key)
            item = provider(model.provider)
            if not item:
                continue
            progress.phase(f"🧠 Pensando · {model.title()} ({item.name})", f"🧠 Thinking · {model.title()} ({item.name})")
            last = _call(item, model, messages, images, max_tokens or config.AI_MAX_OUTPUT_TOKENS, raw)
            state.check_work_allowed()
            if last.ok:
                _last_used[item.key] = time.time()
                last.seconds = time.monotonic() - started
                return last
            if last.error == "policy" or len(tried) >= max_attempts:
                return last
    return last


def cloudflare_spent() -> None:
    """Workers AI text and image models share one daily neuron budget; pause all of them until it renews."""
    seconds = state.until_utc_midnight()
    state.set_cooldown("cloudflare", seconds, "quota")
    from . import images

    for source in images.sources():
        if source.key.startswith("cf:"):
            state.set_cooldown(source.key, seconds, "quota")


def _cooldown_for(item: Provider, model: Model, status: int, body: str, retry_after: str) -> str:
    lowered = body.lower()
    if item.key == "cloudflare" and re.search(r"\b4006\b|daily free allocation", lowered):
        cloudflare_spent()
        return "quota"
    if re.search(r"insufficient balance|recharge|no resource package|requires? (a )?(paid|credits)|add credits to use", lowered) and not re.search(
            r"site-wide|not your personal", lowered):
        state.set_cooldown(model.key, 24 * 3600, "model")  # this model needs a balance the free plan does not have
        return "model"
    if status == 429 and re.search(r"limit:\s*0\b", lowered):
        state.set_cooldown(model.key, 24 * 3600, "model")  # the free plan grants this model no requests at all
        return "model"
    if status == 429 and (item.key == "llm7" or (item.key == "kilo" and "for free models" in lowered)):
        # Keyless gateways limit by IP across all their models: pause the whole provider.
        match = re.search(r'"retry_after"\s*:\s*(\d+)', lowered) or re.search(r"retry after (\d+)", lowered)
        seconds = int(match.group(1)) if match else 120
        if "daily" in lowered:
            seconds = max(seconds, 3600)
        state.set_cooldown(item.key, max(20, min(seconds, 24 * 3600)), "quota")
        return "quota"
    if status in {429, 503} and re.search(r"overload|temporarily|site-wide|not your personal|upstream|retry shortly|try again later|capacity", lowered):
        match = re.search(r"retry (?:after|in) (\d+) ?s", lowered)
        state.set_cooldown(model.key, max(20, min(int(match.group(1)) + 5, 300)) if match else 60, "busy")
        return "busy"
    if "free_rate_limited" in lowered:
        if "err_free_prompt_cap" in lowered:
            return "response"
        if "err_free_access_denied" in lowered:
            state.set_cooldown(item.key, 24 * 3600, "auth")
            return "auth"
        match = re.search(r'"retry_after_seconds"\s*:\s*(\d+)', lowered)
        state.set_cooldown(model.key, max(20, min(int(match.group(1)), 24 * 3600)) if match else 120, "quota")
        return "quota"
    if status == 429 or "quota" in lowered or "rate limit" in lowered:
        seconds = 600
        if retry_after.replace(".", "", 1).isdigit():
            seconds = max(30, min(int(float(retry_after)), 6 * 3600))
        if re.search(r"per ?day|daily|perday|requests per day|rpd", lowered):
            seconds = 6 * 3600
        state.set_cooldown(model.key, seconds, "quota")
        return "quota"
    if status in {402, 403} and re.search(r"subscription|tier|plan|upgrade|not available|payment|credits", lowered):
        state.set_cooldown(model.key, 24 * 3600, "model")  # the key works; only this model is paid
        return "model"
    if status in {401, 403}:
        state.set_cooldown(item.key, 6 * 3600, "auth")
        return "auth"
    if status in {400, 404, 422} and re.search(r"model|not found|does not exist|unsupported|decommission", lowered):
        state.set_cooldown(model.key, 24 * 3600, "model")
        return "model"
    if re.search(r"safety|content policy|moderation|prohibited", lowered):
        return "policy"
    kind = "busy" if status >= 500 or status in {408, 409} else "response"
    state.set_cooldown(model.key, 180, kind)
    return kind


def _track_headers(model: Model, headers: Any) -> None:
    if headers.get("x-ratelimit-remaining") == "0" and not headers.get("x-ratelimit-remaining-requests"):
        state.set_cooldown(model.key, 60)
    remaining = headers.get("x-ratelimit-remaining-requests")
    limit = headers.get("x-ratelimit-limit-requests")
    if remaining and limit and str(remaining).isdigit() and str(limit).isdigit():
        state.set_remaining(model.key, int(remaining), int(limit))
        if int(remaining) <= 0:
            state.set_cooldown(model.key, 3600)


def _call(item: Provider, model: Model, messages: list[dict[str, Any]], images: list[tuple[str, bytes]] | None, max_tokens: int,
          raw: bool = False) -> AIResult:
    if item.personal:
        from . import personal_ai
        payload = [dict(message) for message in messages]
        if images:
            if "vision" not in model.roles or not payload:
                return AIResult(provider=item.name, model=model.title(), error="unknown_model")
            last = payload[-1]
            parts: list[dict[str, Any]] = [{"type": "text", "text": str(last.get("content", ""))}]
            for mime, image_data in images[:4]:
                parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(image_data).decode()}"}})
            last["content"] = parts
        try:
            text, tokens, finish = personal_ai.complete(state.request_sender(), model.key, payload, max_tokens)
        except (personal_ai.PersonalError, OSError) as exc:
            reason = str(exc) if isinstance(exc, personal_ai.PersonalError) else "connection"
            state.record_usage(model.key, False)
            return AIResult(provider=item.name, model=model.title(), error=reason if reason in {"quota", "auth", "connection"} else "response")
        text = strip_thinking(text) if raw else chat_format(strip_thinking(text))
        state.record_usage(model.key, bool(text), tokens)
        truncated = finish.lower() in {"length", "max_tokens", "max_output_tokens"}
        return AIResult(text=text.strip(), provider=item.name, model=model.title(),
                        error="" if text.strip() else "response", truncated=truncated)
    if item.key == "cloudflare":
        from . import cf_safety

        if not cf_safety.reserve(cf_safety.text_cost(model.model, messages, images, max_tokens)):
            state.set_cooldown(model.key, state.until_utc_midnight(), "quota")
            return AIResult(provider=item.name, model=model.title(), error="quota")
    try:
        if item.kind == "gemini":
            response, text, tokens = _gemini(item, model, messages, images, max_tokens)
        else:
            if REASONING_FLOOR.search(model.model):
                max_tokens = max(max_tokens, 1500)
            if item.key == "kilo":
                max_tokens = max(max_tokens, 3200)  # free output; several free models think at length first
            response, text, tokens = _openai(item, model, messages, images, max_tokens)
    except (requests.RequestException, OSError) as exc:
        state.record_usage(model.key, False)
        state.set_cooldown(model.key, 120, "connection")
        log.warning("AI %s %s: connection failed (%s)", item.key, model.model, exc.__class__.__name__)
        return AIResult(provider=item.name, model=model.title(), error="connection")
    if response is None:
        state.record_usage(model.key, False)
        return AIResult(provider=item.name, model=model.title(), error="policy")
    # Some gateways host models with smaller output caps. Only retry a clear
    # parameter-limit rejection, at a lower budget, never on policy/quota errors.
    if (item.kind != "gemini" and not response.ok and response.status_code in {400, 422}
            and re.search(r"max(?:imum)?(?:_output|_completion)?[_ ]tokens", response.text, re.I)
            and re.search(r"exceed|limit|at most|too large|less than|must be", response.text, re.I)
            and max_tokens > 4096):
        match = re.search(r"(?:limit|maximum|at most|less than)\D{0,30}(\d[\d,]*)", response.text, re.I)
        smaller = min(max_tokens - 1, int(match.group(1).replace(",", ""))) if match else 4096
        if smaller >= 512:
            try:
                response, text, tokens = _openai(item, model, messages, images, smaller)
            except requests.RequestException:
                state.record_usage(model.key, False)
                state.set_cooldown(model.key, 120, "connection")
                return AIResult(provider=item.name, model=model.title(), error="connection")
    if not response.ok:
        state.record_usage(model.key, False)
        kind = _cooldown_for(item, model, response.status_code, response.text[:2000], str(response.headers.get("Retry-After", "")))
        log.warning("AI %s %s: HTTP %s -> %s · %s", item.key, model.model, response.status_code, kind, _excerpt(response.text))
        if kind in {"model", "auth"}:
            _mark(item, model, kind)
        return AIResult(provider=item.name, model=model.title(), error=kind)
    _track_headers(model, response.headers)
    text = strip_thinking(text) if raw else chat_format(strip_thinking(text))
    state.record_usage(model.key, bool(text), tokens)
    try:
        body = response.json()
        if item.kind == "gemini":
            finish = str((body.get("candidates") or [{}])[0].get("finishReason") or "")
        else:
            finish = str((body.get("choices") or [{}])[0].get("finish_reason") or "")
        truncated = finish.lower() in {"length", "max_tokens", "max_output_tokens"}
    except (ValueError, TypeError, AttributeError, IndexError):
        truncated = False
    if not text:
        state.set_cooldown(model.key, 120, "response")
        log.warning("AI %s %s: %s answer", item.key, model.model, "truncated" if truncated else "empty")
        return AIResult(provider=item.name, model=model.title(), error="truncated" if truncated else "empty", truncated=truncated)
    return AIResult(text=text.strip(), provider=item.name, model=model.title(), truncated=truncated)


# Listeners get (kind, Spanish text, English text) when a model stops or starts working for good.
LISTENERS: list[Callable[[str, str, str], None]] = []


def _announce(kind: str, spanish: str, english: str) -> None:
    for listener in list(LISTENERS):
        try:
            listener(kind, spanish, english)
        except Exception:
            log.exception("health listener failed")


def _mark(item: Provider, model: Model, status: str) -> None:
    """Remember the lasting state of a model and announce only real changes (paid, retired, rejected key, back)."""
    with state.lock:
        health = state.data.setdefault("health", {})
        key = item.key if status == "auth" else model.key
        before = (health.get(key) or {}).get("status")
        health[key] = {"status": status, "at": time.time()}
        if status == "ok" and (health.get(item.key) or {}).get("status") == "auth":
            health[item.key] = {"status": "ok", "at": time.time()}
            before = "auth"
    if status == before or (status == "ok" and before not in {"model", "auth"}):
        return
    name = f"{model.title()} ({item.name})"
    if status == "auth":
        _announce("bad", f"{item.name} rechazó la clave API: revísala o renuévala.", f"{item.name} rejected the API key: check or renew it.")
    elif status == "model":
        _announce("bad", f"{name} ya no es gratis o fue retirado; se omite.", f"{name} is no longer free or was retired; skipping it.")
    else:
        _announce("ok", f"{name} vuelve a funcionar.", f"{name} works again.")


def model_status(key: str) -> str:
    item = state.data.get("health", {}).get(key) or {}
    # Old results are not proof that a model is still available today.
    return str(item.get("status") or "") if time.time() - float(item.get("at") or 0) < 86400 else ""


def health_check(delay: float = 150.0) -> None:
    """Probe only the anonymous OVH endpoint; never spend a configured API key in the background.

    Other models remain unverified until used. Even anonymous probes stop on a
    maintenance epoch change, including a quick on/off while a call is in flight.
    """
    epoch = state.maintenance_generation()
    time.sleep(delay)
    if state.maintenance_active() or state.maintenance_generation() != epoch:
        return
    today = state.utc_day()
    if state.flag("health") == today:
        return
    for item in providers():
        if item.key != "ovh" or item.api_key or item.personal or state.cooldown_until(item.key):
            continue
        pool = [model for model in item.models if model.roles & {"fast", "chat", "code", "reason", "long"}]
        for model in pool:
            if state.maintenance_active() or state.maintenance_generation() != epoch:
                return
            if not available(model):
                continue
            # A completed probe is not repeated when only a later probe was paused.
            status = (state.data.get("health", {}).get(model.key) or {})
            if status.get("status") == "ok" and datetime.fromtimestamp(float(status.get("at") or 0), timezone.utc).date().isoformat() == today:
                continue
            generation = state.kill_generation("")
            if state.maintenance_active() or state.maintenance_generation() != epoch:
                return
            try:
                # _pace_ovh checks this guard while waiting, before making a request.
                with state.guard_work("", generation):
                    result = _call(item, model, [{"role": "user", "content": "Reply with the word OK."}], None, 300)
            except state.WorkPaused:
                return
            if state.maintenance_active() or state.maintenance_generation() != epoch:
                return
            log.info("AI health %s %s: %s", item.key, model.model, "ok" if result.ok else result.error)
            if result.ok:
                _mark(item, model, "ok")
            elif result.error == "auth":
                break
            time.sleep(4)
    if not state.maintenance_active() and state.maintenance_generation() == epoch:
        state.set_flag("health", today)


def _watch_catalog() -> None:
    """Announce free models that appeared or disappeared since the last start."""
    current = {model.key: model.title() for item in providers() for model in item.models}
    with state.lock:
        known = dict(state.data.get("catalog") or {})
        state.data["catalog"] = current
    if not known:
        return
    added = [title for key, title in current.items() if key not in known]
    removed = [title for key, title in known.items() if key not in current]
    if added:
        _announce("ok", "Nuevos modelos gratis: " + ", ".join(added[:6]), "New free models: " + ", ".join(added[:6]))
    if removed:
        _announce("bad", "Modelos retirados de la lista: " + ", ".join(removed[:6]), "Models removed from the list: " + ", ".join(removed[:6]))


def chat_format(text: str) -> str:
    """Delta Chat shows raw text: drop Markdown headings, double asterisks and table rulers, never inside code blocks."""
    parts = re.split(r"(```.*?(?:```|$)|`[^`\n]+`)", text or "", flags=re.S)
    for index in range(0, len(parts), 2):
        part = re.sub(r"(?m)^#{1,6}\s+", "", parts[index])
        part = re.sub(r"\*\*(.+?)\*\*", r"*\1*", part)
        part = re.sub(r"(?<![\w])__(\S[^_\n]*\s[^_\n]*\S)__(?![\w])", r"\1", part)
        parts[index] = re.sub(r"(?m)^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$", "", part)
    return re.sub(r"\n{3,}", "\n\n", "".join(parts)).strip()


def _pace_ovh(model: Model) -> None:
    """Reserve one anonymous request per 35 seconds per model, across restarts.

    OVH permits two requests per minute per IP/model; other clients sharing the
    same IP can still consume that quota. A failed save fails closed.
    """
    with _lock:
        gate = _ovh_locks.setdefault(model.key, threading.Lock())
    with gate:
        name = "ovh-last:" + model.model
        while True:
            state.check_work_allowed()
            with state.lock:
                previous = float(state.data["flags"].get(name) or 0)
                wait = max(0.0, previous + 35.0 - time.time()) if previous else 0.0
                if wait <= 0:
                    state.data["flags"][name] = time.time()
                    state.save()
                    return
            progress.phase("⏳ Esperando el límite de OVH…", "⏳ Waiting for OVH's rate limit…")
            time.sleep(min(wait, 2.0))


def _openai(item: Provider, model: Model, messages: list[dict[str, Any]], images: list[tuple[str, bytes]] | None, max_tokens: int):
    if item.key == "ovh":
        _pace_ovh(model)
    payload_messages = [dict(message) for message in messages]
    if images and payload_messages:
        last = payload_messages[-1]
        parts: list[dict[str, Any]] = [{"type": "text", "text": str(last.get("content", ""))}]
        for mime, data in images[:4]:
            parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}})
        last["content"] = parts
    payload: dict[str, Any] = {"model": model.model, "messages": payload_messages, "max_tokens": max_tokens, "temperature": 0.6}
    if item.key == "groq" and "gpt-oss" in model.model:
        payload["reasoning_effort"] = "medium"
        payload["include_reasoning"] = False
    if item.key == "groq" and "qwen" in model.model:
        payload["reasoning_format"] = "hidden"
        if model.model == "qwen/qwen3.8-27b":
            payload["reasoning_effort"] = "default"  # documented thinking mode for complex work
    # Z.AI chooses its supported/default thinking mode; never blanket-disable it.
    headers = {"Content-Type": "application/json", **item.headers}
    if item.api_key and item.key != "ovh":
        headers["Authorization"] = f"Bearer {item.api_key}"
    timeout = max(config.AI_TIMEOUT, 120) if item.key == "kilo" or max_tokens >= 8000 else config.AI_TIMEOUT
    request_options = {"allow_redirects": False} if item.custom or item.key == "ovh" else {}
    response = requests.post(f"{item.base_url}/chat/completions", headers=headers, json=payload, timeout=timeout,
                             **request_options)
    if item.key == "ovh" and 300 <= int(response.status_code) < 400:
        raise requests.RequestException("OVH redirect refused")
    if not response.ok or (item.custom and 300 <= int(response.status_code) < 400):
        return response, "", 0
    body = response.json()
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        content = "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    usage = body.get("usage") or {}
    return response, str(content or ""), int(usage.get("total_tokens") or 0)


def _gemini(item: Provider, model: Model, messages: list[dict[str, Any]], images: list[tuple[str, bytes]] | None, max_tokens: int):
    system = "\n".join(str(message["content"]) for message in messages if message["role"] == "system")
    contents = []
    for message in messages:
        if message["role"] == "system":
            continue
        role = "model" if message["role"] == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": str(message["content"])}]})
    if images and contents:
        for mime, data in images[:4]:
            contents[-1]["parts"].append({"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}})
    payload: dict[str, Any] = {"contents": contents, "generationConfig": {"maxOutputTokens": min(65536, max(1, max_tokens))}}
    # Gemini 3.x uses thinkingLevel, not the 2.5 thinkingBudget parameter.
    if model.model in {"gemini-3.1-flash-lite", "gemini-3.5-flash-lite"} and max_tokens >= 8000:
        payload["generationConfig"]["thinkingConfig"] = {"thinkingLevel": "medium"}
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    response = requests.post(f"{item.base_url}/models/{model.model}:generateContent", params={"key": item.api_key}, json=payload,
                             timeout=max(config.AI_TIMEOUT, 120) if max_tokens >= 8000 else config.AI_TIMEOUT)
    if not response.ok:
        return response, "", 0
    body = response.json()
    candidates_list = body.get("candidates") or []
    if not candidates_list:
        if (body.get("promptFeedback") or {}).get("blockReason"):
            return None, "", 0
        return response, "", 0
    parts = (candidates_list[0].get("content") or {}).get("parts") or []
    text = "\n".join(str(part.get("text", "")) for part in parts if not part.get("thought"))
    usage = body.get("usageMetadata") or {}
    return response, text, int(usage.get("totalTokenCount") or 0)


# ---------------------------------------------------------------------------
# Speech to text for voice notes.
# ---------------------------------------------------------------------------
def transcribe(data: bytes, filename: str, mime: str) -> str:
    groq = provider("groq")
    if groq and not _blocked.get() and not state.cooldown_until("groq:whisper"):
        try:
            response = requests.post(
                f"{groq.base_url}/audio/transcriptions",
                headers={"Authorization": f"Bearer {groq.api_key}"},
                files={"file": (filename, data, mime or "audio/ogg")},
                data={"model": "whisper-large-v3-turbo", "response_format": "json"},
                timeout=120,
            )
            state.record_usage("groq:whisper", response.ok)
            if response.ok and response.json().get("text"):
                return str(response.json()["text"]).strip()
            state.set_cooldown("groq:whisper", 600)
        except requests.RequestException:
            pass
    cloud = provider("cloudflare")
    if cloud and not _blocked.get():
        from . import cf_safety

        cost = cf_safety.audio_cost(data)
        if cost is None or not cf_safety.reserve(cost):
            cloud = None  # unknown duration or local budget exhausted; do not risk unreserved usage
    if cloud and not _blocked.get():
        try:
            response = requests.post(
                f"https://api.cloudflare.com/client/v4/accounts/{config.CLOUDFLARE_ACCOUNT_ID}/ai/run/@cf/openai/whisper-large-v3-turbo",
                headers={"Authorization": f"Bearer {cloud.api_key}"},
                json={"audio": base64.b64encode(data).decode()},
                timeout=120,
            )
            state.record_usage("cloudflare:whisper", response.ok)
            if response.ok:
                text = ((response.json().get("result") or {}).get("text") or "").strip()
                if text:
                    return text
        except requests.RequestException:
            pass
    if provider("gemini"):
        result = complete([{"role": "user", "content": "Transcribe this audio verbatim. Output only the transcript."}],
                          images=[(mime or "audio/ogg", data)], only="gemini")
        return result.text
    return ""


def status_lines(locale: str) -> list[str]:
    from .util import local_time, tr

    lines: list[str] = []
    for item in providers():
        used = sum(state.usage_today(model.key) for model in item.models)
        capacity = item.daily or sum(model.daily for model in item.models)
        cooling = state.cooldown_until(item.key)
        mark = "⏸" if cooling else "●"
        lines.append(f"{mark} {item.name}: {used}/{capacity}" + (tr(locale, f" · pausa hasta {local_time(cooling, '%H:%M')}", f" · paused until {local_time(cooling, '%H:%M')}") if cooling else ""))
        for model in item.models:
            model_cool = state.cooldown_until(model.key)
            remaining = state.remaining_for(model.key)
            extra = f" · {remaining[0]}/{remaining[1]}" if remaining else ""
            pause = f" ⏸{local_time(model_cool, '%H:%M')}" if model_cool else ""
            lines.append(f"   {model.title()} [{','.join(sorted(model.roles))}] {state.usage_today(model.key)}/{model.daily}{extra}{pause}")
    return lines or [tr(locale, "⚠️ No hay proveedores de IA configurados.", "⚠️ No AI providers configured.")]


def _excerpt(body: str) -> str:
    """Short single-line error body for the log, with anything key-like masked."""
    text = re.sub(r"[A-Za-z0-9_\-]{28,}", "…", " ".join(str(body or "").split()))
    return text[:180]


def _wait_text(seconds: float, locale: str) -> str:
    from .util import tr

    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return tr(locale, f"{minutes} min", f"{minutes} min")
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest:02d} min"


def describe_error(error: str, locale: str, result: AIResult | None = None, key: str = "") -> str:
    """Explain a failure precisely: rejected key, withdrawn model, limit with wait time, outage."""
    from .util import tr

    if error in {"person_quota", "reserved"}:
        from . import usage

        return "⚠️ " + (usage.limit_text("ask", locale) if error == "person_quota" else usage.reserve_text(locale))
    name = (result.provider if result else "") or tr(locale, "el proveedor", "the provider")
    model = model_by_key(key) if key else None
    if error == "cooldown" and model:
        provider_wait, model_wait = state.cooldown_until(model.provider), state.cooldown_until(model.key)
        error = state.cooldown_reason(model.provider if provider_wait else model.key) or "quota"
        wait = max(provider_wait, model_wait) - time.time()
    else:
        wait = max(state.cooldown_until(model.key), state.cooldown_until(model.provider)) - time.time() if model else 0
    later = tr(locale, f"; vuelve a estar disponible en {_wait_text(wait, locale)}", f"; available again in {_wait_text(wait, locale)}") if wait > 30 else ""
    messages = {
        "no_provider": ("no hay modelos libres ahora mismo: todos están en pausa o agotados; prueba en unos minutos",
                        "no model is free right now: all are paused or used up; try again in a few minutes"),
        "quota": (f"este modelo alcanzó un límite local o del proveedor{later}", f"this model reached a local or provider limit{later}"),
        "truncated": ("la respuesta se agotó antes de generar texto; pide una parte más concreta o prueba otro modelo",
                      "the response limit was reached before text was generated; request a smaller part or try another model"),
        "auth": (f"{name} rechazó la clave de acceso; hay que renovarla en su web", f"{name} rejected the API key; it needs to be renewed"),
        "model": ("este modelo ya no se ofrece gratis; elige otro", "this model is no longer offered for free; pick another one"),
        "busy": (f"{name} está saturado{later or ''}; prueba otro modelo", f"{name} is overloaded{later or ''}; try another model"),
        "connection": (f"no hubo conexión con {name}; prueba de nuevo", f"{name} could not be reached; try again"),
        "policy": ("el proveedor rechazó la solicitud por su política de contenido", "the provider refused the request under its content policy"),
        "unknown_model": ("ese modelo ya no está disponible", "that model is no longer available"),
    }
    pair = messages.get(error, (f"{name} no devolvió una respuesta útil; prueba otro modelo", f"{name} returned no usable answer; try another model"))
    return "⚠️ IA: " + tr(locale, *pair)


def provider_keys() -> list[str]:
    return [item.key for item in providers()]


def short_status() -> str:
    names = [item.name for item in providers() if not state.cooldown_until(item.key)]
    return ", ".join(names) or "—"


__all__ = ["AIResult", "build_registry", "complete", "discover", "transcribe", "candidates", "providers"]
