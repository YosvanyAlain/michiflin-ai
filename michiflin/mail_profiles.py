"""Explicit, verified per-mailbox delivery limits; never infer them from an email domain."""
from __future__ import annotations

import json
import re
from datetime import date
from dataclasses import dataclass
from typing import Any

from . import config


@dataclass(frozen=True)
class DeliveryProfile:
    address: str
    verified: bool
    safe_part_mb: int | None = None
    min_delay_seconds: int | None = None
    max_batch_parts: int | None = None
    daily_cap_mb: int | None = None
    source: str = ""
    checked_at: str = ""


def parse_profiles(raw: str) -> dict[str, DeliveryProfile]:
    """Parse exact-address profiles. Verified limits require provenance; secrets are never fields."""
    if not raw.strip():
        return {}
    try:
        items = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("MAILBOX_PROFILES_JSON must be a JSON object") from exc
    if not isinstance(items, dict):
        raise ValueError("MAILBOX_PROFILES_JSON must be a JSON object")
    profiles: dict[str, DeliveryProfile] = {}
    limits = {
        "safe_part_mb": (1, 20),
        "min_delay_seconds": (0, 86400),
        "max_batch_parts": (1, 200),
        "daily_cap_mb": (1, 100000),
    }
    allowed = set(limits) | {"verified", "source", "checked_at"}
    for raw_address, raw_profile in items.items():
        address = str(raw_address).strip().lower()
        if (not address or address.count("@") != 1 or any(char.isspace() for char in address)
                or address.startswith("@") or address.endswith("@") or not isinstance(raw_profile, dict)):
            raise ValueError("each mailbox profile needs an exact email address and an object")
        if address in profiles:
            raise ValueError(f"duplicate mailbox profile after case normalization: {address}")
        if set(raw_profile) - allowed:
            raise ValueError(f"unknown field in mailbox profile for {address}")
        verified = raw_profile.get("verified", False)
        if not isinstance(verified, bool):
            raise ValueError(f"verified must be boolean for {address}")
        values: dict[str, Any] = {}
        for name, (minimum, maximum) in limits.items():
            value = raw_profile.get(name)
            if value is None:
                values[name] = None
                continue
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise ValueError(f"{name} for {address} must be an integer in {minimum}..{maximum}")
            values[name] = value
        source = str(raw_profile.get("source", "")).strip()[:500]
        checked_at = str(raw_profile.get("checked_at", "")).strip()
        if verified:
            if not any(value is not None for value in values.values()):
                raise ValueError(f"verified profile for {address} has no delivery limits")
            try:
                date.fromisoformat(checked_at)
            except ValueError as exc:
                raise ValueError(f"checked_at for {address} must be a real YYYY-MM-DD date") from exc
            if not source or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", checked_at):
                raise ValueError(f"verified profile for {address} needs source and checked_at (YYYY-MM-DD)")
        profiles[address] = DeliveryProfile(address=address, verified=verified, source=source, checked_at=checked_at, **values)
    return profiles


# Refuse to start with malformed operator configuration rather than silently ignore it.
parse_profiles(config.MAILBOX_PROFILES_JSON)


def _domain(address: str) -> str:
    normalized = (address or "").strip().lower()
    return normalized.rsplit("@", 1)[1].rstrip(".") if normalized.count("@") == 1 else ""


def is_nauta(address: str) -> bool:
    """Recognize ETECSA's documented public mailbox domain, not arbitrary lookalike suffixes."""
    return _domain(address) == "nauta.cu"


def profile_for(address: str, raw: str | None = None) -> DeliveryProfile | None:
    """Return only an exact-address match. Domain defaults are separate from verified profiles."""
    normalized = (address or "").strip().lower()
    if not normalized:
        return None
    profiles = parse_profiles(config.MAILBOX_PROFILES_JSON if raw is None else raw)
    return profiles.get(normalized)


def effective_settings(address: str, settings: dict[str, int], raw: str | None = None) -> dict[str, int]:
    """Use 20 MB for non-Nauta by default, cap Nauta at 10 MB, then apply verified stricter bounds."""
    result = {key: int(value) for key, value in settings.items()}
    base_ranges = {"part_mb": (1, 20), "batch": (0, 200), "delay": (0, 86400), "daily_cap_mb": (0, 100000)}
    for name, (minimum, maximum) in base_ranges.items():
        value = result.get(name)
        if value is not None and not minimum <= value <= maximum:
            raise ValueError(f"effective {name} must be in {minimum}..{maximum}")
    if is_nauta(address):
        result["part_mb"] = min(result.get("part_mb", 10), 10)
    profile = profile_for(address, raw)
    if not profile or not profile.verified:
        return result
    if profile.safe_part_mb is not None:
        result["part_mb"] = min(result.get("part_mb", profile.safe_part_mb), profile.safe_part_mb)
    if profile.min_delay_seconds is not None:
        result["delay"] = max(result.get("delay", 0), profile.min_delay_seconds)
    if profile.max_batch_parts is not None:
        current_batch = result.get("batch", 0)
        result["batch"] = min(current_batch, profile.max_batch_parts) if current_batch else profile.max_batch_parts
    if profile.daily_cap_mb is not None:
        current_cap = result.get("daily_cap_mb", 0)
        result["daily_cap_mb"] = min(current_cap, profile.daily_cap_mb) if current_cap else profile.daily_cap_mb
    return result


def part_limit_bytes(address: str, raw: str | None = None) -> int | None:
    """Current built-in or verified ceiling for one part, or None when none is configured."""
    ceilings: list[int] = [10] if is_nauta(address) else []
    profile = profile_for(address, raw)
    if profile and profile.verified and profile.safe_part_mb is not None:
        ceilings.append(profile.safe_part_mb)
    return min(ceilings) * config.MIB if ceilings else None
