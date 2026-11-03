"""Small bounded FIFO of distinct senders for one local inference process.

This is deliberately in memory and shared by ordinary-chat and miniapp worker
threads. It is not a durable task queue: on shutdown pending turns are not
replayed, and the user is told if the bounded wait expires.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable


class FairTurns:
    def __init__(self, max_waiters: int = 4, timeout: float = 90.0) -> None:
        if max_waiters < 0 or timeout <= 0:
            raise ValueError("invalid_limits")
        self.max_waiters = max_waiters
        self.timeout = timeout
        self._cv = threading.Condition()
        self._pending: deque[tuple[object, str]] = deque()
        self._people: set[str] = set()
        self._active: tuple[object, str] | None = None

    def acquire(self, person: str, *, check: Callable[[], None] | None = None,
                on_wait: Callable[[], None] | None = None) -> tuple[str, object | None]:
        # A missing sender shares one fallback identity, never a new unbounded
        # identity for every request. The caller supplies the verified sender.
        person = (person or "").strip().casefold()[:320] or "<unknown>"
        ticket = object()
        with self._cv:
            if person in self._people:
                return "duplicate", None
            # With an idle slot, the first pending request is about to start.
            ceiling = self.max_waiters + (1 if self._active is None else 0)
            if len(self._pending) >= ceiling:
                return "busy", None
            self._people.add(person)
            self._pending.append((ticket, person))
            waiting = self._active is not None or len(self._pending) > 1
        deadline = time.monotonic() + self.timeout
        try:
            if waiting and on_wait is not None:
                on_wait()
            while True:
                if check is not None:
                    check()
                with self._cv:
                    if self._active is None and self._pending and self._pending[0][0] is ticket:
                        self._active = self._pending.popleft()
                        return "ok", ticket
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return "wait_timeout", None
                    # Poll the cooperative kill/maintenance guard while waiting.
                    self._cv.wait(min(remaining, 0.5))
        finally:
            with self._cv:
                if self._active is None or self._active[0] is not ticket:
                    self._pending = deque(item for item in self._pending if item[0] is not ticket)
                    self._people.discard(person)
                    self._cv.notify_all()

    def release(self, ticket: object) -> None:
        with self._cv:
            if self._active is None or self._active[0] is not ticket:
                raise RuntimeError("invalid_local_ai_turn")
            _token, person = self._active
            self._active = None
            self._people.discard(person)
            self._cv.notify_all()
