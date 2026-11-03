"""Isolated synthetic users exercise long output and FIFO against the real GGUF."""
from __future__ import annotations
import json
import threading
import time
from michiflin import answer_html, local_ai

order = []
waiting = {name: threading.Event() for name in ("bob", "carol")}
first_request = threading.Event()
results = {}
budgets = {}
started = {}
finished = {}
lock = threading.Lock()
original_http = local_ai._http

prompts = {
    "alice": "Escribe una secuencia numerada del 1 al 80. Cada número debe ir seguido de una frase completa y diferente sobre plantas, bosques, ríos y animales. Empieza directamente por el número 1 y continúa sin resumir.",
    "bob": "En una sola oración, explica qué es una biblioteca.",
    "carol": "En una sola oración, explica qué es una bicicleta.",
}

def observing_http(path, payload, timeout):
    if payload is not None:
        name = threading.current_thread().name
        with lock:
            budgets[name] = payload.get("max_tokens")
            started[name] = time.monotonic()
        if name == "alice":
            first_request.set()
    return original_http(path, payload, timeout)

local_ai._http = observing_http

def run(name):
    def on_start():
        with lock:
            order.append(name)
    results[name] = local_ai.complete(prompts[name], "es", sender=name + "@example.test",
                                      on_wait=waiting[name].set if name in waiting else None,
                                      on_start=on_start)
    finished[name] = time.monotonic()

begin = time.monotonic()
threads = {name: threading.Thread(target=run, args=(name,), name=name) for name in prompts}
threads["alice"].start()
assert first_request.wait(15), "first_model_request_did_not_start"
threads["bob"].start(); assert waiting["bob"].wait(10), "second_did_not_queue"
threads["carol"].start(); assert waiting["carol"].wait(10), "third_did_not_queue"
duplicate = local_ai.complete("Pregunta duplicada", "es", sender="ALICE@example.test")
assert duplicate.status == "duplicate", "sender_not_limited"
for thread in threads.values():
    thread.join(180)
    assert not thread.is_alive(), "turn_timeout"
assert order == ["alice", "bob", "carol"], "not_fifo"
assert all(results[name].status == "ok" and results[name].text.strip() for name in prompts), "model_unavailable_or_wait_timeout"
assert budgets == {"alice": 768, "bob": 384, "carol": 768}, "unexpected_turn_budget"
first = results["alice"]
assert answer_html.is_long(first.text), "model_did_not_generate_a_long_answer"
document = answer_html.render(first.text, "es", signature="IA local", truncated=first.truncated)
assert document.startswith("<!doctype html>") and "default-src 'none'" in document
assert "<script" not in document.lower() and len(document.encode("utf-8")) < answer_html.MAX_DOCUMENT_BYTES
elapsed = time.monotonic() - begin
print("SAFE_REAL_LONG_HTML", json.dumps({
    "order": order, "duplicate": duplicate.status, "statuses": {name: results[name].status for name in prompts},
    "budgets": budgets, "first_answer_chars": len(first.text), "first_cut": first.truncated,
    "html_bytes": len(document.encode("utf-8")), "first_seconds": round(finished["alice"] - started["alice"], 1),
    "last_start_seconds": round(started["carol"] - begin, 1), "elapsed_seconds": round(elapsed, 1),
    "identity_used": False, "bot_started": False, "external_ai": False,
}, sort_keys=True), flush=True)
