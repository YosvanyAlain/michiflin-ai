"""Conservative, local budget for shared Cloudflare Workers AI usage.

Reservations are persisted *before* making the request and are not refunded on
errors, since a timeout does not prove the provider did no work. This is only an
estimate: it cannot see other clients or account billing, and cannot guarantee
that a paid account will not be charged. A Workers Free account is the hard stop.
"""
from __future__ import annotations

import math
import shutil
import subprocess
from typing import Any

from . import config, state

KEY = "cf:safety-neurons"


def used() -> int:
    with state.lock:
        entry = state.data["usage"].get(state.utc_day(), {}).get(KEY, [0, 0, 0])
        return int(entry[2]) if len(entry) > 2 else 0


def reserve(amount: int) -> bool:
    limit = min(10_000, max(0, int(config.CLOUDFLARE_ESTIMATED_NEURONS)))
    amount = max(1, int(amount))
    with state.lock:
        day = state.data["usage"].setdefault(state.utc_day(), {})
        entry = day.setdefault(KEY, [0, 0, 0])
        if int(entry[2]) + amount > limit:
            return False
        entry[0] += 1
        entry[2] += amount
        try:
            state.save()  # do not risk a restart forgetting a pending reservation
        except OSError:
            entry[0] -= 1
            entry[2] -= amount
            return False
        return True


def text_cost(model: str, messages: list[dict[str, Any]], media: list[tuple[str, bytes]] | None, max_tokens: int) -> int:
    # Byte count is an intentionally conservative upper bound on text tokens.
    # For vision, raw bytes are also counted as tokens as a safety margin, but
    # the provider's actual image tokenization and price can change.
    rates = {
        "@cf/meta/llama-3.3-70b-instruct-fp8-fast": (26668, 204805),
        "@cf/qwen/qwen2.5-coder-32b-instruct": (60000, 90909),
        "@cf/meta/llama-4-scout-17b-16e-instruct": (24545, 77273),
    }
    incoming, outgoing = rates.get(model, (120000, 450000))
    size = sum(len(str(message.get("content", "")).encode("utf-8")) for message in messages)
    size += sum(len(data) for _mime, data in media or [])
    return max(1, math.ceil((size * incoming + max_tokens * outgoing) / 1_000_000 * 1.3 + 10))


def image_cost(key: str, width: int, height: int) -> int:
    tiles = max(1, math.ceil(width / 512) * math.ceil(height / 512))
    # Price list checked 2026-10-28; image estimates deliberately include headroom.
    estimates = {
        "cf:flux-1-schnell": 4 * 4.8 + 6 * 9.6,
        "cf:flux-2-klein-4b": tiles * 26.05,
        "cf:flux-2-klein-9b": 1363.64,
        "cf:flux-2-dev": tiles * 20 * 37.5,
        "cf:lucid-origin": tiles * 636 + 20 * 12,
        "cf:phoenix": tiles * 530 + 25 * 10,
        # Cloudflare does not currently publish a neuron conversion for these
        # legacy models; keep a conservative local reservation, not a promise.
        "cf:sdxl-lightning": 2000,
        "cf:dreamshaper": 2000,
    }
    return math.ceil(estimates.get(key, 8000) * 1.3 + 20)


def audio_cost(data: bytes) -> int | None:
    """Estimate Whisper cost from real duration; skip Cloudflare if unknown.

    ffprobe gets only a pipe protocol, so it cannot fetch embedded network URLs.
    The bound and timeout also prevent oversized or stalled probes.
    """
    probe = shutil.which("ffprobe")
    if not probe or not data or len(data) > 25_000_000:
        return None
    try:
        result = subprocess.run([probe, "-v", "error", "-protocol_whitelist", "pipe", "-i", "pipe:0",
                                 "-select_streams", "a:0", "-show_entries", "packet=pts_time,duration_time",
                                 "-of", "csv=p=0"], input=data, capture_output=True, timeout=8, check=True)
        first, last = None, None
        for line in result.stdout.splitlines():
            fields = line.split(b",")
            try:
                start = float(fields[0])
                duration = float(fields[1]) if len(fields) > 1 else 0.0
            except ValueError:
                continue
            if not math.isfinite(start) or not math.isfinite(duration) or duration < 0:
                return None
            first = start if first is None else min(first, start)
            last = start + duration if last is None else max(last, start + duration)
        seconds = (last - first) if first is not None and last is not None else 0
        if not 0 < seconds <= 7200:
            return None
        return math.ceil(seconds / 60 * 46.63 * 1.3 + 20)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
