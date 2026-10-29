"""Localized command names: every reply shows commands in the reader's language only.

Both spellings are always accepted as input; output uses the locale of the reply.
"""
from __future__ import annotations

# Canonical key -> (Spanish, English). The key is the internal handler name.
COMMANDS: dict[str, tuple[str, str]] = {
    "help": ("ayuda", "help"),
    "app": ("app", "app"),
    "web": ("web", "web"),
    "more": ("mas", "more"),
    "pdf": ("pdf", "pdf"),
    "capture": ("captura", "screenshot"),
    "search": ("buscar", "search"),
    "videos": ("videos", "videos"),
    "summary": ("resumen", "summary"),
    "info": ("info", "info"),
    "video": ("video", "video"),
    "audio": ("audio", "audio"),
    "download": ("bajar", "download"),
    "queue": ("cola", "queue"),
    "pause": ("pausar", "pause"),
    "resume": ("seguir", "resume"),
    "send": ("enviar", "send"),
    "cancel": ("cancelar", "cancel"),
    "retry": ("reintentar", "retry"),
    "resend": ("reenviar", "resend"),
    "schedule": ("programar", "schedule"),
    "ai": ("ia", "ai"),
    "image": ("img", "img"),
    "forget": ("olvidar", "reset"),
    "model": ("modelo", "model"),
    "translate": ("traducir", "translate"),
    "convert": ("convertir", "convert"),
    "time": ("hora", "time"),
    "transcript": ("subtitulos", "transcript"),
    "weather": ("clima", "weather"),
    "rate": ("tasa", "rate"),
    "news": ("noticias", "news"),
    "calc": ("calc", "calc"),
    "github": ("github", "github"),
    "settings": ("ajustes", "settings"),
    "set": ("ajustar", "set"),
    "health": ("salud", "health"),
    "purge": ("limpiar", "purge"),
    "next": ("siguiente", "next"),
    "back": ("anterior", "back"),
    "admin": ("admin", "admin"),
    "owner_only": ("solo_dueno", "owner_only"),
    "maintenance": ("mantenimiento", "maintenance"),
    "nota": ("nota", "note"),
    "notas": ("notas", "notes"),
    "borrar_nota": ("borrar_nota", "delete_note"),
    "enlace": ("enlace", "link"),
    "enlaces": ("enlaces", "links"),
    "procesar_enlace": ("procesar_enlace", "process_link"),
    "nauta_strict": ("nauta_estricto", "nauta_strict"),
    "kill_switch": ("interruptor", "kill_switch"),
    "estado": ("estado", "status"),
    "estimar": ("estimar", "estimate"),
    "calidad": ("calidad", "quality"),
}

# Sub-actions written after an underscore (/cola_limpiar, /queue_clear).
WORDS: dict[str, tuple[str, str]] = {
    "clear": ("limpiar", "clear"),
    "account": ("cuenta", "account"),
    "notifications": ("notificaciones", "notifications"),
    "all": ("todas", "all"),
    "read": ("leidas", "read"),
    "auto": ("auto", "auto"),
    "allow": ("permitir", "allow"),
    "remove": ("quitar", "remove"),
    "block": ("bloquear", "block"),
    "attempts": ("intentos", "attempts"),
    "logs": ("registros", "logs"),
    "restart": ("reiniciar", "restart"),
    "alerts": ("alertas", "alerts"),
}


def name(locale: str, key: str) -> str:
    pair = COMMANDS[key]
    return pair[1] if locale == "en" else pair[0]


def word(locale: str, key: str) -> str:
    pair = WORDS.get(key)
    if pair is None:
        return key
    return pair[1] if locale == "en" else pair[0]


def cmd(locale: str, key: str, *args: object) -> str:
    """/name_arg1_arg2 in the reply language; known sub-action words are translated too."""
    parts = [name(locale, key)] + [word(locale, str(item)) for item in args if str(item) != ""]
    return "/" + "_".join(parts)


def aliases() -> dict[str, str]:
    """Every localized spelling mapped to its canonical key."""
    found: dict[str, str] = {}
    for key, pair in COMMANDS.items():
        for spelling in pair:
            found.setdefault(spelling, key)
    return found


def locale_of(text: str) -> str:
    """'es' or 'en' when a command is spelled in only one language, else ''."""
    if not text.startswith("/"):
        return ""
    for piece in text[1:].split(" ", 1)[0].split("@", 1)[0].lower().split("_"):
        for table in (COMMANDS, WORDS):
            for spanish, english in table.values():
                if spanish != english and piece in (spanish, english):
                    return "es" if piece == spanish else "en"
    return ""


def word_key(value: str) -> str:
    """Canonical sub-action key for a typed word in either language."""
    value = value.lower()
    for key, pair in WORDS.items():
        if value in pair or value == key:
            return key
    return value
