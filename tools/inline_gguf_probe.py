"""Synthetic real-GGUF probe, no bot identity, mailbox or external AI."""
from __future__ import annotations

import threading
import time
from unittest import mock

from michiflin import agent, answer_inline, chat, local_ai, state, tools

started = time.monotonic()
start_order: list[str] = []
start_times: dict[str, float] = {}
results: dict[str, local_ai.Result] = {}
waiters = {name: threading.Event() for name in ("bob", "carol")}
first_started = threading.Event()
questions = {
    "alice": "Dame una guía concreta para cultivar tomates en macetas, con varios pasos prácticos y detalles útiles.",
    "bob": "Explica brevemente una ventaja de regar plantas temprano.",
    "carol": "Indica una precaución al cuidar una planta en maceta.",
}


def work(name: str) -> None:
    def on_start() -> None:
        start_order.append(name)
        start_times[name] = time.monotonic() - started
        if name == "alice":
            first_started.set()
    results[name] = local_ai.complete(
        questions[name], "es", sender=name + "@example.test",
        on_start=on_start, on_wait=waiters[name].set if name in waiters else None,
    )


if not local_ai.ready():
    raise SystemExit("local server not ready")
with mock.patch.object(state, "check_work_allowed", lambda: None):
    workers = {name: threading.Thread(target=work, args=(name,), name=name) for name in questions}
    workers["alice"].start()
    assert first_started.wait(25), "first request never began"
    workers["bob"].start()
    assert waiters["bob"].wait(10), "bob did not enter queue"
    workers["carol"].start()
    assert waiters["carol"].wait(10), "carol did not enter queue"
    duplicate = local_ai.complete("duplicada", "es", sender="ALICE@example.test")
    for worker in workers.values():
        worker.join(180)
    assert not any(worker.is_alive() for worker in workers.values()), "worker timeout"
assert duplicate.status == "duplicate", duplicate.status
assert start_order == ["alice", "bob", "carol"], (start_order, {k: v.status for k, v in results.items()})
assert all(result.status == "ok" for result in results.values()), {k: v.status for k, v in results.items()}
assert start_times["carol"] < 90, start_times
print("SAFE_FIFO", start_order, {k: round(v, 1) for k, v in start_times.items()},
      {k: v.status for k, v in results.items()}, "total_s", round(time.monotonic() - started, 1), flush=True)

first = results["alice"]
first_display = local_ai.display(first, "es")
pages = answer_inline.InlinePages()
k = pages.key(1, "alice@example.test", "synthetic-grant")
first_visible, paged = pages.start(k, {"text": first_display, "sig": "IA local"}, "es")
assert (not paged and first_visible["text"] == first_display) or len(first_visible["text"]) > 3000
assert "HTML enviado al chat" not in first_visible["text"]
if first.truncated:
    assert "cortada" in first_visible["text"] or "límite" in first_visible["text"]

scope, sender, chat_id = "test-app:grant-one", "alice@example.test", 999
local_ai.forget(chat_id, sender)
local_ai.remember(chat_id, sender, questions["alice"], first.text, scope)
payloads: list[dict] = []
actual_http = local_ai._http


def tracked(path, payload, timeout):
    if path == "/v1/chat/completions":
        payloads.append(payload)
    return actual_http(path, payload, timeout)


with mock.patch.object(state, "check_work_allowed", lambda: None), \
     mock.patch.object(state, "get_kill_switch", lambda _address: False), \
     mock.patch.object(chat, "sender", lambda: sender), \
     mock.patch.object(tools, "search", side_effect=AssertionError("unexpected external search")), \
     mock.patch.object(local_ai, "_http", side_effect=tracked):
    chat.start_capture()
    continued = agent.handle(chat_id, "continúa", "es", scope=scope)
    visible = "\n".join(item for item in chat.stop_capture() if isinstance(item, str))
assert continued and continued.status == "ok" and visible
assert len(payloads) == 1
assert any(questions["alice"] in item["content"] for item in payloads[0]["messages"])
assert any(first.text[-100:] in item["content"] for item in payloads[0]["messages"] if item["role"] == "assistant")
assert any(term in continued.text.casefold() for term in ("planta", "maceta", "tomat", "riego", "regar", "agua", "tierra", "sustrato", "cultiv", "semill", "huert")), "continuation lost topic"
assert not local_ai.history(chat_id, "bob@example.test", scope)
assert not local_ai.history(chat_id, sender, "different-app-or-grant")
print("SAFE_REAL_CONTINUATION", "first_chars", len(first.text), "first_cut", first.truncated,
      "continued_chars", len(continued.text), "continued_cut", continued.truncated,
      "same_topic", True, "per_user_and_grant_isolated", True, flush=True)
