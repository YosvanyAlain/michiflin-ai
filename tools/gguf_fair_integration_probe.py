"""Synthetic concurrent users against the real, isolated local GGUF model."""
from __future__ import annotations

import json
import threading
import time
from michiflin import local_ai

order: list[str] = []
waiting = {n: threading.Event() for n in ("bob", "carol")}
started = {n: threading.Event() for n in ("alice", "bob", "carol")}
results: dict[str, local_ai.Result] = {}
lock = threading.Lock()
prompts = {
    "alice": "Escribe seis frases cortas y distintas en español sobre árboles, hojas, raíces, sombra, lluvia y frutos.",
    "bob": "En una sola oración, explica qué es una biblioteca.",
    "carol": "En una sola oración, explica qué es una bicicleta.",
}

def started_callback(name: str) -> None:
    with lock:
        order.append(name)
    started[name].set()

def worker(name: str) -> None:
    results[name] = local_ai.complete(
        prompts[name], "es", sender=name + "@example.test",
        on_wait=(lambda: waiting[name].set()) if name in waiting else None,
        on_start=lambda: started_callback(name),
    )

begin = time.monotonic()
a = threading.Thread(target=worker, args=("alice",)); a.start()
assert started["alice"].wait(10), "first_user_did_not_start"
b = threading.Thread(target=worker, args=("bob",)); b.start()
assert waiting["bob"].wait(10), "second_user_did_not_queue"
c = threading.Thread(target=worker, args=("carol",)); c.start()
assert waiting["carol"].wait(10), "third_user_did_not_queue"
duplicate = local_ai.complete("Pregunta duplicada", "es", sender="ALICE@example.test")
assert duplicate.status == "duplicate", "sender_not_limited"
for item in (a, b, c):
    item.join(150)
    assert not item.is_alive(), "model_timeout"
assert order == ["alice", "bob", "carol"], "not_fifo"
assert all(results[name].status == "ok" and results[name].text.strip() for name in prompts), "model_unavailable"
print("SAFE_FAIR_REAL_MODEL", json.dumps({"order": order, "duplicate": duplicate.status,
      "waiters": 2, "statuses": {k: v.status for k, v in results.items()},
      "answer_lengths": {k: len(v.text) for k, v in results.items()}, "elapsed_seconds": round(time.monotonic() - begin, 1),
      "identity_used": False, "bot_started": False}, sort_keys=True), flush=True)
