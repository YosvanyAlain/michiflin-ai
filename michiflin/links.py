"""Tappable navigation: URLs become short chat commands that prefill the input box.

Delta Chat turns "/command" tokens into taps that fill the draft instead of opening a
browser, so every URL the bot lists is offered as /v_<id>, /pdf_<id>, /dl_<id>, etc.
"""
from __future__ import annotations

from urllib.parse import quote

from . import state
from .util import compact, host_of, unlinked

# Action prefixes accepted as "/<action>_<id>".
ACTIONS = {
    "v": "web",
    "pdf": "pdf",
    "cap": "capture",
    "dl": "download",
    "a": "audio",
    "vid": "video",
    "info": "info",
    "mas": "more",
}

_bot_address = ""


def set_bot_address(address: str) -> None:
    global _bot_address
    _bot_address = address or ""


def bot_address() -> str:
    return _bot_address


def command(action: str, url: str, suffix: str = "") -> str:
    token = state.link_id(url)
    return f"/{action}_{token}" + (f"_{suffix}" if suffix else "")


def mailto(url: str, action: str = "") -> str:
    """HTML link that opens a new message to the bot with the URL (or a command) as body."""
    body = f"{('/' + action + ' ') if action else ''}{url}"
    return f"mailto:{_bot_address}?body={quote(body, safe='')}" if _bot_address else url


def resolve(token: str) -> str:
    return state.resolve_link(token)


def entry(index: int, title: str, url: str, snippet: str = "", action: str = "v") -> str:
    """One compact search/listing line: title, unlinked host, tap command."""
    lines = [f"{index}. {compact(title, 110) or unlinked(host_of(url))}"]
    if snippet:
        lines.append(f"   {compact(snippet, 170)}")
    lines.append(f"   {unlinked(host_of(url))} · {command(action, url)}")
    return "\n".join(lines)
