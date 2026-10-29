"""Environment configuration. Every value has a safe default."""
from __future__ import annotations

import os
import time
from pathlib import Path
from zoneinfo import ZoneInfo

MIB = 1024 * 1024
APP_NAME = "michiflin"
STARTED_AT = time.monotonic()


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def env_int(name: str, default: int) -> int:
    try:
        return int(float(env(name, str(default))))
    except ValueError:
        return default


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in env(name, default).split(",") if item.strip()]


# Access control: an empty allow-list locks the bot.
OWNER_ADDRESS = env("OWNER_ADDRESS").lower()
MAINTENANCE_MODE = env("MAINTENANCE_MODE", "0").lower() in {"1", "true", "yes", "on"}
# Private single-user mode: only the explicitly configured OWNER_ADDRESS may use the bot.
OWNER_ONLY = env("OWNER_ONLY", "0").lower() in {"1", "true", "yes", "on"}
ALLOWED_SENDERS = {item.lower() for item in env_list("ALLOWED_SENDERS")}
if OWNER_ADDRESS:
    ALLOWED_SENDERS.add(OWNER_ADDRESS)
if not OWNER_ADDRESS and not (OWNER_ONLY or MAINTENANCE_MODE) and len(ALLOWED_SENDERS) == 1:
    OWNER_ADDRESS = next(iter(ALLOWED_SENDERS))
# Once an owner is configured, allow-listed guests are never administrators.
ADMIN_ADDRESSES = {OWNER_ADDRESS} if OWNER_ADDRESS else set(ALLOWED_SENDERS)
MAILBOX_PROFILES_JSON = env("MAILBOX_PROFILES_JSON")
# Opaque deployment epoch: miniapps built under another epoch are rejected (no migration path).
BOT_EPOCH = env("BOT_EPOCH") or env("RUNNING_SHA", "local")[:12]
if (MAINTENANCE_MODE or OWNER_ONLY) and not OWNER_ADDRESS:
    raise RuntimeError("OWNER_ONLY/MAINTENANCE_MODE requires OWNER_ADDRESS; refusing to start without an owner")


def service_access_allowed(address: str) -> bool:
    """Apply the live owner-only gate and controlled guest access to service work."""
    from . import access  # avoid an import cycle at configuration startup
    return access.service_allowed(address)


# One-time message from the deployer to the owner's app (delivered once per distinct text).
OWNER_NOTICE = env("OWNER_NOTICE")

# AI providers (all optional; only free tiers are used).
GEMINI_API_KEY = env("GEMINI_API_KEY") or env("AI_API_KEY")
GROQ_API_KEY = env("GROQ_API_KEY")
MISTRAL_API_KEY = env("MISTRAL_API_KEY")
OPENROUTER_API_KEY = env("OPENROUTER_API_KEY")
ZAI_API_KEY = env("ZAI_API_KEY")
CLOUDFLARE_ACCOUNT_ID = env("CLOUDFLARE_ACCOUNT_ID")
CLOUDFLARE_AI_TOKEN = env("CLOUDFLARE_AI_TOKEN")
# Conservative local estimate shared across Workers AI text and images; not an account billing cap.
CLOUDFLARE_ESTIMATED_NEURONS = env_int("CLOUDFLARE_ESTIMATED_NEURONS", 7500)
# OVH SDXL is currently free anonymously, subject to the provider's rate limits and availability.
OVH_IMAGES = env("OVH_IMAGES", "1") != "0"
GITHUB_MODELS_TOKEN = env("GITHUB_MODELS_TOKEN")
POLLINATIONS_API_KEY = env("POLLINATIONS_API_KEY")
BAZAARLINK_API_KEY = env("BAZAARLINK_API_KEY")
ORCAROUTER_API_KEY = env("ORCAROUTER_API_KEY")
SAMBANOVA_API_KEY = env("SAMBANOVA_API_KEY")
# Keyless gateways (OVH anonymous, LLM7.io turbo tier, Kilo Gateway free models).
FREE_GATEWAYS = env("FREE_GATEWAYS", "1") != "0"
LLM7_API_KEY = env("LLM7_API_KEY")
KILO_API_KEY = env("KILO_API_KEY")
# Hugging Face public ZeroGPU Space: anonymous by default, or an optional account token.
# Paid/PRO account settings can spend credits after the included quota; check the account.
HF_IMAGES = env("HF_IMAGES", "1") != "0"
HF_TOKEN = env("HF_TOKEN")
HF_IMAGE_SPACE = env("HF_IMAGE_SPACE", "black-forest-labs/FLUX.1-schnell")
# You.com Web Search: kept under its free daily allowance, then the keyless search takes over.
YDC_API_KEY = env("YDC_API_KEY")
YDC_DAILY = env_int("YDC_DAILY", 90)
# Keyless public fallbacks can be disabled with "0".
POLLINATIONS_PUBLIC = env("POLLINATIONS_PUBLIC", "1") != "0"
# AI Horde: volunteer GPUs, keyless (anonymous queue); a free registered key earns priority.
AI_HORDE = env("AI_HORDE", "1") != "0"
AI_HORDE_API_KEY = env("AI_HORDE_API_KEY")
# JSON list of extra OpenAI-compatible free providers, see README.
EXTRA_PROVIDERS = env("EXTRA_PROVIDERS")
AI_EXTRA_ALLOWED_HOSTS = {host.lower() for host in env_list("AI_EXTRA_ALLOWED_HOSTS")}
AI_TIMEOUT = env_int("AI_TIMEOUT", 75)
AI_MAX_OUTPUT_TOKENS = env_int("AI_MAX_OUTPUT_TOKENS", 6000)
# Coder runs code only inside an isolated sandbox (sudo + namespaces); 0 keeps it to writing files.
CODE_RUN = env("CODE_RUN", "1") != "0"
# Live status notices: seconds before the first notice, minimum gap between edits, edit cap.
STATUS_DELAY = env_int("STATUS_DELAY", 4)
STATUS_EDIT_SECONDS = env_int("STATUS_EDIT_SECONDS", 12)
STATUS_MAX_EDITS = env_int("STATUS_MAX_EDITS", 30)
AI_SYSTEM_PROMPT = env(
    "AI_SYSTEM_PROMPT",
    "You are Michiflin AI, a helpful private assistant. Match the depth and length of "
    "your answer to the task: be brief for simple questions and thorough for complex requests. "
    "Use clear paragraphs or lists and never claim actions you did not perform.",
)

# Optional services.
ELTOQUE_API_TOKEN = env("ELTOQUE_API_TOKEN")
MICHIFLIN_GITHUB_TOKEN = env("MICHIFLIN_GITHUB_TOKEN")

# Network egress helpers for blocked media/web sources (e.g. socks5://127.0.0.1:40000).
MEDIA_PROXY = env("MEDIA_PROXY")
WEB_PROXY = env("WEB_PROXY", MEDIA_PROXY)
YTDLP_COOKIES = env("YTDLP_COOKIES")

# Runtime paths.
CONFIG_DIR = Path(env("CONFIG_DIR", str(Path.home() / ".config" / APP_NAME)))
STATE_FILE = CONFIG_DIR / "michiflin-state.json"
LEGACY_STATE_FILE = CONFIG_DIR / "queue-state.json"
JOB_ROOT = Path(env("JOB_DIR", "/tmp/michiflin-queue"))
TEMP_ROOT = Path(env("PREVIEW_DIR", "/tmp/michiflin-temp"))
TIMEZONE_NAME = env("BOT_TIMEZONE", "UTC")
try:
    LOCAL_ZONE = ZoneInfo(TIMEZONE_NAME)
except Exception:  # pragma: no cover - invalid zone falls back to UTC
    TIMEZONE_NAME = "UTC"
    LOCAL_ZONE = ZoneInfo("UTC")
SESSION_SECONDS = env_int("SESSION_DURATION_SECONDS", 19200)

# Storage and download limits (permissive; disk space is checked live).
MAX_FILE_BYTES = env_int("MAX_FILE_MB", 4096) * MIB
MAX_QUEUE_BYTES = env_int("MAX_QUEUE_MB", 12288) * MIB
MIN_FREE_DISK_BYTES = env_int("MIN_FREE_DISK_MB", 1536) * MIB
MAX_MEDIA_MINUTES = env_int("MAX_DURATION_MINUTES", 0)
MAX_PAGE_BYTES = env_int("MAX_WEB_PREVIEW_MB", 8) * MIB
MAX_DOCUMENT_BYTES = env_int("MAX_AI_DOCUMENT_MB", 20) * MIB
MAX_DOCUMENT_CHARS = env_int("MAX_AI_DOCUMENT_CHARS", 60000)
MAX_DOCUMENT_PAGES = env_int("MAX_AI_DOCUMENT_PAGES", 60)

# Owner-adjustable defaults (stored settings override these).
DEFAULTS = {
    "language": env("LANGUAGE_MODE", "auto").lower(),
    "ai_pin": "",
    "video_height": env_int("DEFAULT_VIDEO_HEIGHT", 360),
    "audio_kbps": env_int("DEFAULT_AUDIO_KBPS", 64),
    "audio_format": env("DEFAULT_AUDIO_FORMAT", "m4a").lower(),
    "part_mb": env_int("CHUNK_MB", 20),
    "part_format": env("PART_FORMAT", "raw").lower(),
    "batch": env_int("DEFAULT_BATCH_COUNT", 0),
    "delay": env_int("DEFAULT_SEND_DELAY_SECONDS", 15),
    "window": env("SEND_WINDOW", ""),
    "auto_send": env("AUTO_SEND", "1") != "0",
    "daily_cap_mb": env_int("MAIL_SAFETY_CAP_MB", 0),
    "reader_images": env("READER_IMAGES", "off").lower(),
    "reader_kb": env_int("READER_PAGE_KB", 120),
    "reader_links": env("READER_LINKS", "chat").lower(),
    "capture_device": env("CAPTURE_DEVICE", "mobile").lower(),
    "notify": env("NOTIFY_MODE", "minimal").lower(),
    "progress": env("PROGRESS_MODE", "on").lower(),
    "reactions": env("REACTIONS_MODE", "on").lower(),
    "chat_mode": env("CHAT_MODE", "app").lower(),
    "capture_quality": env("CAPTURE_QUALITY", "high").lower(),
    "image_size": env_int("IMAGE_SIZE", 768),
    "ttl_hours": env_int("JOB_TTL_HOURS", 48),
    "user_jobs": env_int("USER_MAX_JOBS", 3),
    "ai_daily": env_int("USER_AI_DAILY", 60),
    "image_daily": env_int("USER_IMAGE_DAILY", 15),
    "code_daily": env_int("USER_CODE_DAILY", 4),
    "ai_reserve": env_int("AI_RESERVE_PERCENT", 25),
    "user_daily_mb": env_int("USER_DAILY_MB", 0),
    "nauta_strict": env("NAUTA_STRICT", "0").lower() in {"1", "true", "yes", "on"},
    "kill_switch": env("KILL_SWITCH", "0").lower() in {"1", "true", "yes", "on"},
}
