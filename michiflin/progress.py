"""Live status notices: one self-editing message per task, deleted once the result is out.

A notice only appears when a task outlasts STATUS_DELAY, is edited at a growing interval
(every edit is a small e-mail), and is removed for both sides when the task ends.

Reactions on the owner's message are a lighter signal: ⏳ once a task outlasts STATUS_DELAY,
then ✅ or ❌. Fast replies get no reaction; queued downloads keep ⏳ until delivery.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from . import chat, config, state

_local = threading.local()


def enabled() -> bool:
    return state.setting("progress") == "on"


def elapsed_text(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min {seconds % 60:02d} s"
    return f"{seconds // 3600} h {seconds % 3600 // 60:02d} min"


class Notice:
    def __init__(self, chat_id: int, locale: str, text: str, delay: float | None = None,
                 on_shown: Callable[[int], None] | None = None, msg_id: int = 0, since: float = 0.0) -> None:
        self.chat_id = chat_id
        self.locale = locale
        self.text = text
        self.timer = True
        self.started = time.monotonic() - (max(0.0, time.time() - since) if since else 0.0)
        self.delay = config.STATUS_DELAY if delay is None else delay
        self.on_shown = on_shown
        self.msg_id = msg_id
        self.shown = text
        self.last_edit = time.monotonic() if msg_id else 0.0
        self.edits = 0
        self.closed = False
        self.lock = threading.Lock()
        if enabled() and chat.ready():
            threading.Thread(target=self._run, name="notice", daemon=True).start()

    def set(self, text: str, timer: bool = True) -> None:
        with self.lock:
            self.text, self.timer = text, timer

    def render(self, now: float) -> str:
        if not self.timer:
            return self.text
        return f"{self.text}\n⏱ {elapsed_text(now - self.started)}"

    def _gap(self, now: float) -> float:
        return min(120.0, max(float(config.STATUS_EDIT_SECONDS), (now - self.started) * 0.2))

    def _run(self) -> None:
        while True:
            time.sleep(1)
            now = time.monotonic()
            with self.lock:
                if self.closed:
                    return
                body = self.render(now)
                if not self.msg_id:
                    if now - self.started < self.delay:
                        continue
                    self.msg_id = chat.send(self.chat_id, body)
                    self.shown, self.last_edit = body, now
                    if self.msg_id and self.on_shown:
                        self.on_shown(self.msg_id)
                    if not self.msg_id:
                        return
                    continue
                if body != self.shown and now - self.last_edit >= self._gap(now) and self.edits < config.STATUS_MAX_EDITS:
                    if chat.edit(self.msg_id, body):
                        self.shown = body
                        self.edits += 1
                    self.last_edit = now

    def close(self, delete: bool = True) -> None:
        with self.lock:
            self.closed = True
            msg_id = self.msg_id
        if msg_id and delete:
            chat.delete_for_all([msg_id])


class _Silent:
    """Stand-in when no task notice is active on this thread."""
    locale = "es"

    def set(self, text: str, timer: bool = True) -> None:
        return None

    def close(self, delete: bool = True) -> None:
        return None


SEEN, WORKING, DONE, FAILED = "👀", "⏳", "✅", "❌"
# With reactions on, the generic text notice waits longer: the ⏳ reaction covers short tasks.
REACTION_NOTICE_DELAY = 15.0


def reactions() -> str:
    return str(state.setting("reactions") or "off")


class Reaction:
    """Tracks the reaction on one incoming message while its handler runs."""

    def __init__(self, msg_id: int) -> None:
        self.msg_id = msg_id
        self.current = ""
        self.finished = False
        self.handed_off = False
        self.lock = threading.Lock()
        if not msg_id or reactions() == "off":
            self.msg_id = 0
            return
        if reactions() == "all":
            self._set(SEEN)
        threading.Thread(target=self._watch, name="reaction", daemon=True).start()

    def _set(self, emoji: str) -> None:
        if emoji != self.current and chat.react(self.msg_id, emoji):
            self.current = emoji

    def _watch(self) -> None:
        time.sleep(config.STATUS_DELAY)
        with self.lock:
            if not self.finished:
                self._set(WORKING)

    def hand_off(self) -> int:
        """A queued job takes over the reaction; returns the message id to finish later."""
        with self.lock:
            if not self.msg_id:
                return 0
            self.handed_off = True
            self._set(WORKING)
            return self.msg_id

    def finish(self, failed: bool) -> None:
        with self.lock:
            self.finished = True
            if self.msg_id and not self.handed_off and self.current:
                self._set(FAILED if failed else DONE)


def begin(chat_id: int, locale: str, msg_id: int = 0) -> Notice:
    text = "⏳ Recibido · trabajando…" if locale == "es" else "⏳ Received · working…"
    _local.reaction = Reaction(msg_id)
    chat.reset_warned()
    delay = REACTION_NOTICE_DELAY if _local.reaction.msg_id else None
    notice = Notice(chat_id, locale, text, delay=delay)
    _local.notice = notice
    return notice


def hand_off() -> int:
    reaction = getattr(_local, "reaction", None)
    return reaction.hand_off() if reaction else 0


def finish_job(msg_id: int, outcome: str) -> None:
    """Final reaction for a queued job: done, failed, working (retry) or cleared."""
    if msg_id and reactions() != "off":
        chat.react(int(msg_id), {"done": DONE, "failed": FAILED, "working": WORKING}.get(outcome, ""))


def current() -> Notice | _Silent:
    return getattr(_local, "notice", None) or _Silent()


def phase(es: str, en: str = "", timer: bool = True) -> None:
    """Update the active task notice on this thread (no-op when there is none)."""
    notice = current()
    notice.set(es if notice.locale == "es" or not en else en, timer)


def end(failed: bool = False) -> None:
    notice = getattr(_local, "notice", None)
    reaction = getattr(_local, "reaction", None)
    _local.notice = _local.reaction = None
    if notice:
        notice.close()
    if reaction:
        reaction.finish(failed or chat.warned())
