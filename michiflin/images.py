"""Image generation from free sources: an exact model chosen by the user, or fair rotation, with compact JPEG output."""
from __future__ import annotations

import base64
import io
import random
import re
import time
from dataclasses import dataclass
from urllib.parse import quote

import requests

from . import config, progress, state

IMAGE_PATTERNS = [
    r"\b(genera|generar|gen[eé]rame|crea|crear|cr[eé]ame|haz|hazme|dibuja|dib[uú]jame|pinta|p[ií]ntame|dise[nñ]a|ilustra|quiero|necesito|dame|mu[eé]strame)\b.{0,40}\b(imagen|imágen|foto|fotograf[ií]a|dibujo|ilustraci[oó]n|retrato|logo|logotipo|wallpaper|fondo de pantalla|p[oó]ster|cartel|avatar|sticker|arte)\b",
    r"\b(imagen|foto|dibujo|ilustraci[oó]n|retrato|logo|wallpaper|p[oó]ster)\s+(de|del|con|sobre)\b",
    r"\b(generate|create|make|draw|paint|design|render|illustrate)\b.{0,30}\b(image|picture|photo|drawing|illustration|portrait|logo|wallpaper|poster|avatar|art)\b",
    r"\b(image|picture|photo|drawing|illustration|portrait|logo|wallpaper|poster) of\b",
    r"^\s*(dibuja|dib[uú]jame|pinta|draw|paint)\b",
]
LEAD = re.compile(
    r"^\s*(por favor\s+|please\s+|oye\s+|michiflin\s*,?\s+)?((me\s+)?(puedes|podr[ií]as|can you|could you)\s+)?"
    r"(gen[eé]rame|generar|genera|cr[eé]ame|crear|crea|hazme|haz|dib[uú]jame|dibujar|dibuja|p[ií]ntame|pinta|dise[nñ]a(me)?|ilustra|"
    r"quiero|necesito|dame|mu[eé]strame|generate|create|make|draw|paint|design|render|illustrate|i want)\s+(me\s+)?"
    r"(((una?|an?|la|el|some)\s+)?(imagen|im[aá]genes|fotograf[ií]a|foto|dibujo|ilustraci[oó]n|image|picture|photo|drawing|illustration|"
    r"retrato|portrait|logo|wallpaper|p[oó]ster|poster)\s+((de|del|of|con|with|sobre|about)\s+)?)?",
    re.I,
)
NOUN_LEAD = re.compile(r"^\s*(una?\s+|an?\s+)?(imagen|foto|dibujo|ilustraci[oó]n|image|picture|photo|drawing)\s+(de|del|of|con|with)\s+", re.I)
SIZE_HINTS = {
    "horizontal": (1024, 576), "paisaje": (1024, 576), "landscape": (1024, 576), "wallpaper de pc": (1280, 720),
    "vertical": (576, 1024), "portrait": (576, 1024), "wallpaper": (576, 1024), "fondo de pantalla": (576, 1024),
    "cuadrada": (768, 768), "square": (768, 768), "hd": (1024, 1024), "alta calidad": (1024, 1024),
}


@dataclass
class ImageResult:
    data: bytes = b""
    source: str = ""
    prompt: str = ""
    error: str = ""


@dataclass
class Source:
    key: str
    label: str
    daily: int


def is_image_request(text: str) -> bool:
    lowered = text.lower().strip()
    if len(lowered) > 600 or "?" in lowered[:3]:
        return False
    return any(re.search(pattern, lowered) for pattern in IMAGE_PATTERNS)


def clean_prompt(text: str) -> str:
    value = NOUN_LEAD.sub("", LEAD.sub("", text.strip(), count=1), count=1)
    return value.strip(" .,:;") or text.strip()


def size_for(text: str) -> tuple[int, int]:
    lowered = text.lower()
    for hint, size in SIZE_HINTS.items():
        if hint in lowered:
            return size
    side = int(state.setting("image_size"))
    return side, side


def sources() -> list[Source]:
    items: list[Source] = []
    if config.CLOUDFLARE_ACCOUNT_ID and config.CLOUDFLARE_AI_TOKEN:
        items += [Source(key, label, daily) for key, (_model, label, daily, _style) in CLOUDFLARE.items()]
    if config.HF_TOKEN and _gradio_available():
        # Free ZeroGPU minutes give about eight images a day.
        items.append(Source("hf:space", "FLUX.1 schnell (HF)", 7))
    if config.AI_HORDE:
        items += [Source(key, label, 60) for key, (_model, label, *_rest) in HORDE.items()]
    if config.POLLINATIONS_API_KEY:
        # Finite Pollen balance (FLUX schnell costs 0.002 an image); spent only after renewable sources.
        items.append(Source("pollinations:key", "Pollinations FLUX", 40))
    if config.POLLINATIONS_PUBLIC:
        # Keyless images are downgraded and watermarked: last resort only.
        items.append(Source("pollinations:flux", "Pollinations", 300))
    return items


# Workers AI models: key -> (model id, label, daily share of the free neurons, request style).
CLOUDFLARE = {
    "cf:flux-1-schnell": ("@cf/black-forest-labs/flux-1-schnell", "FLUX.1 schnell", 250, "schnell"),
    "cf:flux-2-klein-4b": ("@cf/black-forest-labs/flux-2-klein-4b", "FLUX.2 klein 4B", 150, "form"),
    "cf:flux-2-klein-9b": ("@cf/black-forest-labs/flux-2-klein-9b", "FLUX.2 klein 9B", 80, "form"),
    "cf:flux-2-dev": ("@cf/black-forest-labs/flux-2-dev", "FLUX.2 dev", 30, "form"),
    "cf:sdxl-lightning": ("@cf/bytedance/stable-diffusion-xl-lightning", "SDXL Lightning", 150, "num_steps:8"),
    "cf:dreamshaper": ("@cf/lykon/dreamshaper-8-lcm", "DreamShaper 8", 150, "num_steps:8"),
    "cf:lucid-origin": ("@cf/leonardo/lucid-origin", "Lucid Origin", 40, "steps:20"),
    "cf:phoenix": ("@cf/leonardo/phoenix-1.0", "Phoenix 1.0", 40, "num_steps:25"),
}
# AI Horde models: key -> (model name, label, steps, cfg scale, sampler).
HORDE = {
    "horde:flux": ("Flux.1-Schnell fp8 (Compact)", "FLUX.1 schnell (Horde)", 4, 1.0, "k_euler"),
    "horde:zimage": ("Z-Image-Turbo", "Z-Image Turbo (Horde)", 8, 1.0, "k_euler"),
    "horde:juggernaut": ("Juggernaut XL", "Juggernaut XL (Horde)", 25, 6.0, "k_dpmpp_2m"),
    "horde:albedo": ("AlbedoBase XL (SDXL)", "AlbedoBase XL (Horde)", 25, 6.0, "k_dpmpp_2m"),
}
HORDE_API = "https://aihorde.net/api/v2"
HORDE_WAIT = 300
# Renewable fast sources first, then the slower volunteer queue, the finite balance and the watermarked public endpoint.
PRIORITY = {"hf:space": 1, **{key: 2 for key in HORDE}, "pollinations:key": 3, "pollinations:flux": 4}
_balance_cache: dict[str, float] = {}


def pollinations_balance() -> float | None:
    """Remaining Pollen on the configured key (cached for ten minutes)."""
    if not config.POLLINATIONS_API_KEY:
        return None
    if time.time() - _balance_cache.get("at", 0) < 600:
        return _balance_cache.get("value")
    try:
        response = requests.get("https://gen.pollinations.ai/account/balance",
                                headers={"Authorization": f"Bearer {config.POLLINATIONS_API_KEY}"}, timeout=15)
        value = float(response.json().get("balance")) if response.ok else None
    except (requests.RequestException, ValueError, TypeError):
        value = None
    _balance_cache.update(at=time.time(), value=value)
    return value


def _gradio_available() -> bool:
    try:
        import gradio_client  # noqa: F401
    except ImportError:
        return False
    return True


def _ordered() -> list[Source]:
    pool = [item for item in sources() if not state.cooldown_until(item.key)]
    random.shuffle(pool)
    return sorted(pool, key=lambda item: (PRIORITY.get(item.key, 0), state.usage_today(item.key) / max(1, item.daily)))


def english_prompt(prompt: str) -> str:
    """Image models follow English best; the free translator handles other languages, no AI model involved."""
    if re.fullmatch(r"[\x00-\x7f]+", prompt) and not re.search(r"\b(de|del|con|una|un|el|la|los|las|que|en)\b", prompt.lower()):
        return prompt
    from . import extras

    try:
        text, _source = extras.translate(prompt, "en")
    except Exception:
        text = ""
    text = (text or "").strip().strip('"')
    return text if 3 < len(text) < 1500 else prompt


def generate(text: str, model: str = "") -> ImageResult:
    """One image, counted against the daily image allowance of people who are not administrators."""
    from . import usage

    address = state.request_sender()
    state.charge_once()  # the image is the unit: helper AI calls inside it are not counted again
    if state.acting_as() and not usage.allowed(address, "image"):
        return ImageResult(error="person_quota")
    result = _generate(text, model)
    if result.data and address:
        usage.spend(address, "image")
    return result


def _generate(text: str, model: str = "") -> ImageResult:
    """A chosen model is used alone (no silent fallback), otherwise sources rotate fairly."""
    state.check_work_allowed()
    prompt = clean_prompt(text)
    width, height = size_for(text)
    english = english_prompt(prompt)[:1900]
    state.check_work_allowed()
    last_error = "no_source"
    if model:
        chosen = next((item for item in sources() if item.key == model), None)
        if chosen is None:
            return ImageResult(error="unknown", prompt=english)
        if state.cooldown_until(chosen.key):
            return ImageResult(error="cooldown", prompt=english, source=chosen.label)
        pool = [chosen]
    else:
        pool = _ordered()
    for source in pool:
        state.check_work_allowed()
        progress.phase(f"🎨 Dibujando · {source.label}", f"🎨 Drawing · {source.label}")
        try:
            data = _run(source, english, width, height, patient=bool(model))
        except requests.RequestException:
            data, last_error = b"", "connection"
            state.set_cooldown(source.key, 300)
        except _QuotaError:
            data, last_error = b"", "quota"
            if source.key.startswith("cf:"):
                from . import ai

                ai.cloudflare_spent()
            else:
                state.set_cooldown(source.key, 6 * 3600, "quota")
        except _BusyError:
            data, last_error = b"", "busy"
            state.set_cooldown(source.key, 60, "busy")
        except _AuthError:
            data, last_error = b"", "auth"
            for item in sources():
                if item.key.split(":")[0] == source.key.split(":")[0]:
                    state.set_cooldown(item.key, 6 * 3600, "auth")
        except _SourceError as exc:
            data, last_error = b"", str(exc) or "response"
            state.set_cooldown(source.key, 600)
        state.record_usage(source.key, bool(data))
        state.check_work_allowed()
        if data:
            return ImageResult(compress(data), source.label, english)
    return ImageResult(error=last_error, prompt=english, source=pool[0].label if model and pool else "")


class _QuotaError(Exception):
    pass


class _SourceError(Exception):
    pass


class _BusyError(Exception):
    pass


class _AuthError(Exception):
    pass


def _cloudflare(model: str, **kwargs) -> requests.Response:
    url = f"https://api.cloudflare.com/client/v4/accounts/{config.CLOUDFLARE_ACCOUNT_ID}/ai/run/{model}"
    response = requests.post(url, headers={"Authorization": f"Bearer {config.CLOUDFLARE_AI_TOKEN}"}, timeout=120, **kwargs)
    if response.ok:
        return response
    text = response.text[:800].lower()
    if re.search(r"\b4006\b|daily free allocation|neurons", text):
        raise _QuotaError()  # the account's free neurons for today are spent
    if response.status_code in {401, 403}:
        raise _AuthError()
    if response.status_code in {429, 503} or re.search(r"\b3040\b|capacity|rate limit", text):
        raise _BusyError()
    if not response.ok:
        raise _SourceError("response")
    return response


def _decode(response: requests.Response) -> bytes:
    if response.headers.get("content-type", "").startswith("image/"):
        return response.content
    body = response.json()
    result = body.get("result", body) if isinstance(body, dict) else {}
    image = result.get("image") if isinstance(result, dict) else None
    if not image:
        raise _SourceError("empty")
    return base64.b64decode(image)


def _run(source: Source, prompt: str, width: int, height: int, patient: bool = False) -> bytes:
    seed = random.randint(1, 2**31 - 1)
    if source.key in CLOUDFLARE:
        model, _label, _daily, style = CLOUDFLARE[source.key]
        if style == "schnell":
            return _decode(_cloudflare(model, json={"prompt": prompt[:2048], "steps": 6, "seed": seed}))
        if style == "form":
            fields = {"prompt": prompt, "width": str(width), "height": str(height), "seed": str(seed)}
            if model.endswith("klein-4b"):
                fields["steps"] = "4"
            return _decode(_cloudflare(model, files={key: (None, value) for key, value in fields.items()}))
        name, steps = style.split(":")
        return _decode(_cloudflare(model, json={"prompt": prompt, "width": width, "height": height, name: int(steps), "seed": seed}))
    if source.key == "pollinations:key":
        balance = pollinations_balance()
        if balance is not None and balance < 0.004:
            raise _QuotaError()
        params = {"width": width, "height": height, "nologo": "true", "seed": seed, "model": "flux", "private": "true"}
        response = requests.get(f"https://gen.pollinations.ai/image/{quote(prompt[:900])}", params=params,
                                headers={"Authorization": f"Bearer {config.POLLINATIONS_API_KEY}"}, timeout=150)
        _balance_cache.pop("at", None)
        if response.status_code in {401, 402, 403, 429}:
            raise _QuotaError()
        if not response.ok or not response.headers.get("content-type", "").startswith("image/"):
            raise _SourceError("response")
        return response.content
    if source.key == "pollinations:flux":
        params = {"width": width, "height": height, "nologo": "true", "seed": seed, "model": "flux", "private": "true"}
        response = requests.get(f"https://image.pollinations.ai/prompt/{quote(prompt[:900])}", params=params, timeout=150)
        if response.status_code == 429:
            raise _QuotaError()
        if not response.ok or not response.headers.get("content-type", "").startswith("image/"):
            raise _SourceError("response")
        return response.content
    if source.key == "hf:space":
        return _hf_space(prompt, width, height, seed)
    if source.key in HORDE:
        return _horde(source.key, prompt, width, height, seed, HORDE_WAIT if patient else HORDE_WAIT // 2)
    raise _SourceError("unknown")


def _horde_size(width: int, height: int) -> tuple[int, int]:
    """Keep within the anonymous limit (no upfront kudos): at most 1024x1024 pixels, multiples of 64."""
    scale = min(1.0, (832 * 832 / max(1, width * height)) ** 0.5)
    return tuple(max(512, min(1024, int(side * scale) // 64 * 64)) for side in (width, height))  # type: ignore[return-value]


def _horde(key: str, prompt: str, width: int, height: int, seed: int, wait: int = HORDE_WAIT) -> bytes:
    model, _label, steps, cfg, sampler = HORDE[key]
    headers = {"apikey": config.AI_HORDE_API_KEY or "0000000000", "Client-Agent": "michiflin-ai:5:private"}
    width, height = _horde_size(width, height)
    body = {
        "prompt": prompt[:1000], "models": [model], "nsfw": False, "censor_nsfw": True, "r2": True, "shared": False,
        "slow_workers": True, "trusted_workers": False,
        "params": {"width": width, "height": height, "steps": steps, "cfg_scale": cfg, "sampler_name": sampler,
                   "seed": str(seed), "n": 1, "karras": sampler != "k_euler"},
    }
    response = requests.post(f"{HORDE_API}/generate/async", json=body, headers=headers, timeout=30)
    if response.status_code == 429:
        raise _BusyError()
    if response.status_code == 401:
        raise _AuthError()
    if not response.ok:
        raise _SourceError("response")
    job = str(response.json().get("id") or "")
    started = time.monotonic()
    try:
        while True:
            time.sleep(6)
            check = requests.get(f"{HORDE_API}/generate/check/{job}", headers=headers, timeout=20).json()
            if check.get("faulted") or check.get("is_possible") is False:
                raise _SourceError("response")
            if check.get("done"):
                break
            if time.monotonic() - started > wait:
                raise _BusyError()
        status = requests.get(f"{HORDE_API}/generate/status/{job}", headers=headers, timeout=30).json()
    except (_BusyError, _SourceError):
        requests.delete(f"{HORDE_API}/generate/status/{job}", headers=headers, timeout=15)
        raise
    generation = (status.get("generations") or [{}])[0]
    if generation.get("censored"):
        raise _SourceError("policy")
    image = str(generation.get("img") or "")
    if image.startswith("http"):
        download = requests.get(image, timeout=60)
        if not download.ok:
            raise _SourceError("response")
        return download.content
    if image:
        return base64.b64decode(image)
    raise _SourceError("empty")


def _hf_space(prompt: str, width: int, height: int, seed: int) -> bytes:
    from gradio_client import Client

    try:
        client = Client(config.HF_IMAGE_SPACE, token=config.HF_TOKEN, verbose=False)
        result = client.predict(prompt=prompt[:1500], seed=seed, randomize_seed=False, width=width, height=height,
                                num_inference_steps=4, api_name="/infer")
    except Exception as exc:
        message = str(exc)
        if "quota" in message.lower():
            raise _QuotaError() from exc
        raise _SourceError("response") from exc
    path = result[0] if isinstance(result, (list, tuple)) else result
    path = path.get("path") if isinstance(path, dict) else path
    try:
        with open(str(path), "rb") as handle:
            return handle.read()
    except OSError as exc:
        raise _SourceError("empty") from exc


def compress(data: bytes, max_side: int = 1280, quality: int = 82) -> bytes:
    """Re-encode as progressive JPEG to keep mailbox usage low."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            image = image.convert("RGB")
            image.thumbnail((max_side, max_side))
            output = io.BytesIO()
            image.save(output, "JPEG", quality=quality, optimize=True, progressive=True)
            return output.getvalue()
    except Exception:
        return data


def status_lines() -> list[str]:
    from .util import local_time

    lines = []
    for item in sources():
        cooling = state.cooldown_until(item.key)
        extra = ""
        if item.key == "pollinations:key":
            balance = pollinations_balance()
            extra = f" · {balance:.3f} Pollen ≈{int(balance / 0.002)} img" if balance is not None else ""
        lines.append(f"{'⏸' if cooling else '●'} {item.label}: {state.usage_today(item.key)}/{item.daily}{extra}" + (f" ⏸{local_time(cooling, '%H:%M')}" if cooling else ""))
    return lines


def stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")
