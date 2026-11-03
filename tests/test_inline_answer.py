"""Inline answers: no attachment, meaningful pages, private continuation."""
from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from michiflin import answer_inline, chat, live_web, local_ai, miniapp, smart_agent, state
from michiflin.fair_local_ai import FairTurns


def binding():
    return {"chat": 31, "addr": "person@example.test", "epoch": "epoch-one", "access_session": "grant-one", "build": "test-build"}


def body() -> str:
    return "# Huerta doméstica\n\n" + "".join(f"Paso {i}: riega las plantas por la mañana y revisa la tierra.\n\n" for i in range(280)) + "\n[S1] https://example.test/fuente\nFINAL-EXACTO"


def key(pages, instance=91, address="person@example.test", scope="mode-grant"):
    return pages.key(instance, address, scope)


def history(text):
    return [["user", "consulta"], ["assistant", text]]


def raw_without_note(text):
    for note in (
        "Hay más de esta respuesta. Escribe «continúa» para seguir.",
        "El modelo alcanzó su límite; queda texto generado. Escribe «continúa» para verlo.",
        "There is more to this answer. Type ‘continue’ to keep reading.",
        "The model reached its limit; generated text remains. Type ‘continue’ to read it.",
    ):
        suffix = "\n\n" + note
        if text.endswith(suffix):
            return text[:-len(suffix)]
    return text


def test_normal_long_answer_is_inline_and_substantial_without_html():
    pages = answer_inline.InlinePages()
    data = {"text": "Explicación completa. " * 100, "sig": "IA local"}
    result, paged = pages.start(key(pages), data, "es")
    assert not paged and result is data and len(result["text"]) > 2000
    assert "HTML" not in result["text"] and "enviado al chat" not in result["text"]
    assert pages.status(key(pages), history(data["text"])) == "none"


@pytest.mark.parametrize("locale", ["es", "en"])
def test_unusually_long_response_is_exactly_recoverable_in_meaningful_pages(locale):
    pages = answer_inline.InlinePages()
    initial = {"text": body(), "sig": "Local AI", "truncated": True, "actions": []}
    first, was_paged = pages.start(key(pages), initial, locale)
    assert was_paged and initial["text"] == body()
    assert len(first["text"]) > 3000 and answer_inline._units(first["text"]) < 6000
    assert "límite" in first["text"] if locale == "es" else "limit" in first["text"]
    collected, previous = [raw_without_note(first["text"])], first["text"]
    while pages.status(key(pages), history(previous)) == "ready":
        item = pages.take(key(pages), history(previous), locale)
        assert item and item["sig"] == "Local AI" and item["truncated"]
        assert answer_inline._units(item["text"]) < 6000
        collected.append(raw_without_note(item["text"]))
        previous = item["text"]
    assert "".join(collected) == body()
    assert collected[-1].endswith("FINAL-EXACTO") and "https://example.test/fuente" in collected[-1]
    assert pages.status(key(pages), history(previous)) == "none"


def test_retry_and_concurrent_requests_never_skip_an_unseen_page():
    pages = answer_inline.InlinePages()
    k = key(pages)
    first, _ = pages.start(k, {"text": body()}, "es")
    second = pages.take(k, history(first["text"]), "es")
    assert second and second["text"] != first["text"]
    # Two requests both based on the first page see the same second page.
    again = pages.take(k, history(first["text"]), "es")
    assert again == second
    assert pages.take(k, [], "es") == second
    third = pages.take(k, history(second["text"]), "es")
    assert third and third != second


def test_key_isolation_and_cache_pressure_never_evict_another_person():
    pages = answer_inline.InlinePages(maximum=1, total=500_000)
    alice = key(pages, address="Alice@example.test")
    bob = key(pages, address="bob@example.test")
    first, paged = pages.start(alice, {"text": body()}, "es")
    assert paged and pages.status(key(pages, address="alice@EXAMPLE.test"), history(first["text"])) == "ready"
    # Forged/stale receipts may trigger only a generic unavailable notice,
    # never reveal another person's or instance's page.
    assert pages.status(bob, history(first["text"])) != "ready"
    assert pages.status(key(pages, instance=92, address="Alice@example.test"), history(first["text"])) != "ready"
    assert pages.status(key(pages, address="Alice@example.test", scope="different-grant"), history(first["text"])) != "ready"
    original = {"text": body(), "sig": "test"}
    fallback, was_paged = pages.start(bob, original, "en")
    assert fallback is original and not was_paged  # send complete answer; do not evict Alice
    assert pages.status(alice, history(first["text"])) == "ready"
    assert pages.start(alice, {"text": "x" * (answer_inline.MAX_STORED_CHARS + 1)}, "es")[1] is False


def test_expired_pending_pages_report_loss_and_do_not_skip_ahead():
    moment = [10.0]
    pages = answer_inline.InlinePages(ttl=30, clock=lambda: moment[0])
    k = key(pages)
    first, _ = pages.start(k, {"text": body()}, "es")
    moment[0] += 31
    assert pages.status(k, history(first["text"])) == "expired"
    assert "ya no está disponible" in pages.take(k, history(first["text"]), "es")["text"]
    assert pages.status(key(pages, address="other@example.test"), history(first["text"])) != "ready"
    pages.start(k, {"text": "New topic"}, "es")
    assert pages.status(k, []) == "none"


def test_expired_but_seen_final_page_does_not_block_model_continuation():
    moment = [0.0]
    pages = answer_inline.InlinePages(ttl=5, clock=lambda: moment[0])
    k = key(pages)
    first, _ = pages.start(k, {"text": "x " * 3000}, "en")
    last = pages.take(k, history(first["text"]), "en")
    assert last and "continue" not in last["text"]
    moment[0] = 6
    assert pages.status(k, history(last["text"])) == "none"


def test_utf16_budget_handles_emoji_and_reconstructs_exact_text():
    text = "🌱" * 11_000 + " FIN"
    pages = answer_inline.InlinePages()
    k = key(pages)
    first, yes = pages.start(k, {"text": text}, "es")
    assert yes
    parts, visible = [raw_without_note(first["text"])], first["text"]
    while pages.status(k, history(visible)) == "ready":
        item = pages.take(k, history(visible), "es")
        visible = item["text"]
        assert answer_inline._units(visible) < 6000
        parts.append(raw_without_note(visible))
    assert "".join(parts) == text


def test_fenced_code_stays_readable_and_notice_outside_code_without_losing_content():
    text = "Explicación.\n\n```python\n" + "print('huerta')\n" * 800 + "```\n\nFUENTE Y FINAL"
    pages = answer_inline.InlinePages()
    k = key(pages)
    first, yes = pages.start(k, {"text": text}, "es")
    assert yes
    item, start = first, 0
    while True:
        end = answer_inline._end(text, start)
        prefix = answer_inline._open_fence(text, start)
        expected = (("```" + prefix + "\n") if prefix is not None else "") + text[start:end]
        assert item["text"].startswith(expected)
        if end < len(text):
            assert len(answer_inline._FENCE.findall(item["text"])) % 2 == 0
            assert item["text"].endswith("continúa» para seguir.")
            item = pages.take(k, history(item["text"]), "es")
            assert item
            start = end
        else:
            assert item["text"].endswith("FUENTE Y FINAL")
            break


@pytest.mark.parametrize("value", ["continúa", "continúe", "continua por favor", "por favor, sigue", "más", "continue", "go on please", "continúa con la respuesta", "continue with the next part"])
def test_continuation_is_bilingual(value):
    assert answer_inline.is_continue(value)


@pytest.mark.parametrize("value", ["continúa con otro tema", "resume esto", "busca más información", "más clima en Cuba", ""])
def test_specific_requests_are_not_hijacked(value):
    assert not answer_inline.is_continue(value)


def test_unclosed_code_fence_does_not_hide_the_cut_notice():
    result = local_ai.Result("ok", "```python\nprint(1)", truncated=True)
    text = local_ai.display(result, "es")
    assert "print(1)\n```\n\n[Respuesta cortada" in text


def test_local_context_pins_original_and_latest_without_mixing_users_or_scopes():
    user = "alice@example.test"
    local_ai.forget(887, user)
    topic = "Explica una huerta cubana en varios pasos."
    local_ai.remember(887, user, topic, "PRIMER PASO " + "x" * 700 + " FINAL PRIMERO", "instance-a")
    for index in range(6):
        local_ai.remember(887, user, "continúa", f"RESPUESTA {index}: " + "y" * 800 + f" DETALLE-{index}", "instance-a")
    previous = local_ai.history(887, user, "instance-a")
    anchor = local_ai.anchor(887, user, "instance-a")
    messages = local_ai._history(previous, anchor)
    assert messages[0]["content"] == topic
    assert "PRIMER PASO" in messages[1]["content"]
    assert "DETALLE-5" in messages[-1]["content"]
    assert sum(len(item["content"]) for item in messages) <= 2400
    assert local_ai.history(887, user, "instance-b") == []
    assert local_ai.anchor(887, "bob@example.test", "instance-a") is None
    local_ai.remember(887, user, "Otro tema", "Otra respuesta", "instance-a")
    assert local_ai.anchor(887, user, "instance-a")[0] == "Otro tema"
    local_ai.forget(887, user)
    assert local_ai.anchor(887, user, "instance-a") is None


def test_local_model_flags_server_and_character_cuts_without_losing_normal_long_output(monkeypatch):
    monkeypatch.setattr(local_ai, "configured", lambda: True)
    monkeypatch.setattr(local_ai, "ready", lambda: True)
    monkeypatch.setattr(state, "check_work_allowed", lambda: None)
    payloads = []
    data = {"text": "normal long text " * 500, "finish": "stop"}

    def http(path, payload, timeout):
        payloads.append((path, payload, timeout))
        return {"choices": [{"message": {"content": data["text"]}, "finish_reason": data["finish"]}]}

    monkeypatch.setattr(local_ai, "_http", http)
    result = local_ai.complete("question", "en", sender="limit-test@example.test")
    assert result.status == "ok" and result.text == data["text"].strip() and not result.truncated
    assert payloads[-1][1]["max_tokens"] == 512 and payloads[-1][1]["t_max_predict_ms"] == 55_000 and payloads[-1][2] == 125
    monkeypatch.setattr(local_ai._turns, "waiting", lambda: 2)
    assert local_ai.complete("question", "en", sender="limit-test@example.test").status == "ok"
    assert payloads[-1][1]["max_tokens"] == 192 and payloads[-1][1]["t_max_predict_ms"] == 15_000
    data.update(text="x" * (local_ai._MAX_GENERATED_CHARS + 3), finish="stop")
    result = local_ai.complete("question", "es", sender="limit-test@example.test")
    assert len(result.text) == local_ai._MAX_GENERATED_CHARS and result.truncated
    data.update(text="partial", finish="length")
    assert "ask to continue" in local_ai.display(local_ai.complete("question", "en", sender="limit-test@example.test"), "en")


def test_smart_agent_no_silent_nine_thousand_character_cut(monkeypatch):
    raw = "x" * 12000 + "END"
    monkeypatch.setattr(smart_agent.ai, "complete", lambda *_a, **_kw: SimpleNamespace(ok=True, text=json.dumps({"answer": raw}),
                                                                                        truncated=False, signature=lambda: "test"))
    monkeypatch.setattr(state, "check_work_allowed", lambda: None)
    answer = smart_agent.run("question", "en")
    assert answer["text"] == raw and "truncated" not in answer
    parsed = smart_agent._parse(json.dumps({"answer": "x" * (smart_agent.MAX_ANSWER_CHARS + 1)}))
    assert parsed and parsed.get("truncated") and len(parsed["answer"]) == smart_agent.MAX_ANSWER_CHARS


def test_model_budget_reduces_with_real_waiters_and_preserves_fifo(monkeypatch):
    monkeypatch.setattr(local_ai, "configured", lambda: True)
    monkeypatch.setattr(local_ai, "ready", lambda: True)
    monkeypatch.setattr(state, "check_work_allowed", lambda: None)
    monkeypatch.setattr(local_ai, "_turns", FairTurns(max_waiters=4, timeout=5))
    first_started, release = threading.Event(), threading.Event()
    waiting = {name: threading.Event() for name in ("bob", "carol")}
    budgets, results = {}, {}

    def http(_path, payload, _timeout):
        name = threading.current_thread().name
        budgets[name] = payload["max_tokens"]
        if name == "alice":
            first_started.set()
            assert release.wait(3)
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(local_ai, "_http", http)
    def work(name):
        results[name] = local_ai.complete("question", "en", sender=name + "@example.test",
                                          on_wait=waiting[name].set if name in waiting else None).status
    alice = threading.Thread(target=work, name="alice", args=("alice",))
    bob = threading.Thread(target=work, name="bob", args=("bob",))
    carol = threading.Thread(target=work, name="carol", args=("carol",))
    alice.start(); assert first_started.wait(2)
    bob.start(); assert waiting["bob"].wait(2)
    carol.start(); assert waiting["carol"].wait(2)
    assert local_ai.complete("duplicate", "en", sender="ALICE@example.test").status == "duplicate"
    release.set()
    for thread in (alice, bob, carol):
        thread.join(3)
        assert not thread.is_alive()
    assert list(budgets) == ["alice", "bob", "carol"]
    assert set(results) == {"alice", "bob", "carol"} and all(value == "ok" for value in results.values())
    assert budgets == {"alice": 512, "bob": 192, "carol": 512}


@pytest.fixture
def integration(monkeypatch):
    entry, replies, generated, sent = binding(), [], [], []
    pages = answer_inline.InlinePages()
    monkeypatch.setattr(miniapp, "_inline_pages", pages)
    monkeypatch.setattr(miniapp.access, "bound_allowed", lambda _e: True)
    monkeypatch.setattr(miniapp.admin, "is_allowed", lambda _a: True)
    monkeypatch.setattr(miniapp.admin, "seen", lambda _a: None)
    monkeypatch.setattr(miniapp, "_verified_binding", lambda _e: True)
    monkeypatch.setattr(miniapp.config, "service_access_allowed", lambda _a: True)
    monkeypatch.setattr(state, "maintenance_active", lambda: False)
    monkeypatch.setattr(state, "get_kill_switch", lambda _a: False)
    monkeypatch.setattr(state, "kill_generation", lambda _a: 0)
    monkeypatch.setattr(state, "work_interrupted", lambda *_: False)
    monkeypatch.setattr(state, "maintenance_interrupted", lambda *_: False)
    monkeypatch.setattr(miniapp, "app_build", lambda: "test-build")
    monkeypatch.setattr(live_web, "_identity", lambda _id: (entry.copy(), entry["addr"]))
    monkeypatch.setattr(live_web, "_authorized", lambda _id: (entry.copy(), entry["addr"]))
    monkeypatch.setattr(miniapp, "respond", lambda _id, _rid, data, error="": replies.append((data, error)))
    monkeypatch.setattr(chat, "send", lambda *_a, **_kw: sent.append(True) or 99)
    monkeypatch.setattr(miniapp.usage, "note", lambda *_a, **_kw: None)
    monkeypatch.setattr(miniapp.usage, "quota_view", lambda _a: {})
    def ask(_chat, args, _locale):
        generated.append(args)
        return {"text": body() if len(generated) == 1 else "Nueva respuesta.", "sig": "IA local", "actions": []}
    monkeypatch.setitem(miniapp.OPS, "ask", ask)
    return entry, pages, replies, generated, sent


def test_real_handler_never_attaches_and_continues_without_calling_model(integration):
    entry, pages, replies, generated, sent = integration
    miniapp.handle(91, entry, {"id": "q1", "op": "ask", "lang": "es", "args": {"space": "agent", "text": "huerta", "history": [], "_local_scope": "spoof"}})
    assert len(generated) == 1 and not sent and len(replies) == 1
    first = replies[-1][0]["text"]
    assert len(first) > 3000 and "continúa»" in first and "HTML" not in first
    assert generated[0]["_local_scope"].startswith("91:") and "spoof" not in generated[0]["_local_scope"]
    miniapp.handle(91, entry, {"id": "q2", "op": "ask", "lang": "es", "args": {"space": "agent", "text": "continúa por favor", "history": history(first)}})
    assert len(generated) == 1 and not sent
    second = replies[-1][0]["text"]
    assert second != first and "Paso" in second
    miniapp.handle(91, entry, {"id": "q3", "op": "ask", "lang": "es", "args": {"space": "agent", "text": "continúa", "history": history(first)}})
    assert replies[-1][0]["text"] == second  # stale receipt repeats instead of skipping
    assert len(generated) == 1


@pytest.mark.parametrize("field,replacement", [("addr", "other@example.test"), ("chat", 32),
                                                ("epoch", "new-epoch"), ("access_session", "new-grant"), ("build", "other-build")])
def test_changed_identity_never_reads_pending_page(integration, monkeypatch, field, replacement):
    entry, pages, replies, generated, sent = integration
    original = miniapp._inline_pages.key(91, entry["addr"], miniapp._inline_scope(entry, {"space": "agent"}))
    first, _ = pages.start(original, {"text": body()}, "es")
    changed = dict(entry, **{field: replacement})
    monkeypatch.setattr(live_web, "_authorized", lambda _id: (changed, changed["addr"]))
    # Direct recheck is denial; cached data is never sent under changed identity.
    assert not miniapp._same_inline_identity(91, entry)
    miniapp.handle(91, entry, {"id": "changed", "op": "ask", "lang": "es", "args": {
        "space": "agent", "text": "continúa", "history": history(first["text"])}})
    assert replies[-1][0] is None and "verificar el acceso" in replies[-1][1]
    assert pages.status(original, history(first["text"])) == "ready" and not sent


def test_timeout_never_sends_late_message_or_adds_pending_pages(integration, monkeypatch):
    entry, pages, replies, generated, sent = integration
    monkeypatch.setitem(miniapp.OP_LIMITS, "ask", 0.01)
    def slow(_chat, _args, _locale):
        time.sleep(0.08)
        return {"text": body()}
    monkeypatch.setitem(miniapp.OPS, "ask", slow)
    miniapp.handle(91, entry, {"id": "slow", "op": "ask", "lang": "en", "args": {"space": "agent", "text": "question"}})
    time.sleep(0.12)
    assert len(replies) == 1 and "too long" in replies[0][1]
    assert not sent and not pages._items


def test_handler_continues_with_model_only_after_all_saved_text_is_seen(integration):
    entry, _pages, replies, generated, sent = integration
    miniapp.handle(91, entry, {"id": "start", "op": "ask", "lang": "es", "args": {"space": "agent", "text": "huerta"}})
    visible = replies[-1][0]["text"]
    for i in range(10):
        if "continúa» para seguir." not in visible:
            break
        miniapp.handle(91, entry, {"id": f"page-{i}", "op": "ask", "lang": "es", "args": {
            "space": "agent", "text": "continúa", "history": history(visible)}})
        visible = replies[-1][0]["text"]
    assert "FINAL-EXACTO" in visible and len(generated) == 1
    miniapp.handle(91, entry, {"id": "next-model", "op": "ask", "lang": "es", "args": {
        "space": "agent", "text": "continúa", "history": history(visible)}})
    assert len(generated) == 2 and replies[-1][0]["text"] == "Nueva respuesta." and not sent


def test_short_error_and_existing_attachment_are_unchanged():
    pages = answer_inline.InlinePages()
    for value in ({"text": "Hola"}, {"text": body(), "files": ["other.zip"]},
                  {"text": "⚠️ " + body()}, {"text": body(), "error": "failed"}):
        result, paged = pages.start(key(pages), value, "es")
        assert result is value and not paged


def test_person_forget_removes_only_their_pages_and_tombstones():
    pages = answer_inline.InlinePages()
    alice, bob = key(pages, address="alice@example.test"), key(pages, address="bob@example.test")
    first_a, _ = pages.start(alice, {"text": body()}, "es")
    first_b, _ = pages.start(bob, {"text": body()}, "es")
    pages.forget("ALICE@example.test")
    assert pages.status(alice, history(first_a["text"])) == "unavailable"
    assert pages.status(bob, history(first_b["text"])) == "ready"


def test_background_sweep_prunes_expired_page_text():
    moment = [0.0]
    pages = answer_inline.InlinePages(ttl=10, clock=lambda: moment[0])
    k = key(pages)
    first, _ = pages.start(k, {"text": body()}, "es")
    moment[0] = 11
    pages.prune()
    assert not pages._items and pages.status(k, history(first["text"])) == "expired"


def test_continuation_with_attachment_is_a_new_request_not_a_cached_page(integration):
    entry, _pages, replies, generated, sent = integration
    miniapp.handle(91, entry, {"id": "first-file", "op": "ask", "lang": "es", "args": {"space": "auto", "text": "huerta"}})
    first = replies[-1][0]["text"]
    miniapp.handle(91, entry, {"id": "new-file", "op": "ask", "lang": "es", "args": {
        "space": "auto", "text": "continúa", "history": history(first),
        "file": {"name": "fragmento.txt", "b64": "eA=="}}})
    assert len(generated) == 2 and replies[-1][0]["text"] == "Nueva respuesta." and not sent


def test_pending_request_cannot_consume_a_newer_replacement_answer():
    pages = answer_inline.InlinePages()
    k = key(pages)
    first, _ = pages.start(k, {"text": body()}, "es")
    state_name, version = pages.reserve(k, history(first["text"]))
    assert state_name == "ready" and version is not None
    newer, _ = pages.start(k, {"text": "NUEVO TEMA \n\n" + body()}, "es")
    assert pages.take(k, history(first["text"]), "es", expected=version) is None
    assert pages.status(k, history(newer["text"])) == "ready"


def test_reservation_stays_bound_if_the_same_answer_expires():
    moment = [0.0]
    pages = answer_inline.InlinePages(ttl=5, clock=lambda: moment[0])
    k = key(pages)
    first, _ = pages.start(k, {"text": body()}, "es")
    _, version = pages.reserve(k, history(first["text"]))
    moment[0] = 6
    assert "ya no está disponible" in pages.take(k, history(first["text"]), "es", expected=version)["text"]


def test_continuation_keeps_long_question_constraints_and_more_recent_answer():
    person, scope, chat_id = "detailed@example.test", "verified-app", 882
    local_ai.forget(chat_id, person)
    question = "TEMA-INICIAL " + "detalle " * 200 + " REQUISITO-DEL-FINAL"
    first = "PRIMER-INICIO " + "pasos " * 300 + " PRIMER-FINAL"
    local_ai.remember(chat_id, person, question, first, scope)
    anchor = local_ai.anchor(chat_id, person, scope)
    assert len(anchor[0]) <= 600 and "TEMA-INICIAL" in anchor[0] and "REQUISITO-DEL-FINAL" in anchor[0]
    initial_context = local_ai._history(local_ai.history(chat_id, person, scope), anchor)
    assert len(initial_context[1]["content"]) > 1000 and "PRIMER-FINAL" in initial_context[1]["content"]
    for number in range(4):
        local_ai.remember(chat_id, person, "continúa", "INICIO " + "detalle " * 200 + f" ULTIMO-{number}", scope)
    messages = local_ai._history(local_ai.history(chat_id, person, scope), local_ai.anchor(chat_id, person, scope))
    assert "REQUISITO-DEL-FINAL" in messages[0]["content"]
    assert "ULTIMO-3" in messages[-1]["content"] and len(messages[-1]["content"]) >= 1000
    assert sum(len(item["content"]) for item in messages) <= 2400
    local_ai.forget(chat_id, person)


@pytest.mark.parametrize("locale", ["es", "en"])
def test_restart_or_expired_tombstone_reports_missing_generated_pages(locale):
    original = answer_inline.InlinePages()
    k = key(original)
    first, paged = original.start(k, {"text": body()}, locale)
    assert paged
    restarted = answer_inline.InlinePages()
    status, version = restarted.reserve(k, history(first["text"]))
    assert status == "unavailable" and version is not None
    warning = restarted.take(k, history(first["text"]), locale, expected=version)
    assert ("ya no está disponible" if locale == "es" else "no longer available") in warning["text"]
    assert restarted.status(k, history("a finished response")) == "none"


def test_unavailable_reservation_cannot_touch_a_new_answer():
    pages = answer_inline.InlinePages()
    k = key(pages)
    old, _ = pages.start(k, {"text": body()}, "es")
    pages = answer_inline.InlinePages()  # process restart
    _, reservation = pages.reserve(k, history(old["text"]))
    newer, _ = pages.start(k, {"text": "NUEVO TEMA\n\n" + body()}, "es")
    assert pages.take(k, history(old["text"]), "es", expected=reservation) is None
    assert pages.status(k, history(newer["text"])) == "ready"


def test_handler_warns_after_restart_instead_of_fabricating_missing_text(integration, monkeypatch):
    entry, _pages, replies, generated, sent = integration
    miniapp.handle(91, entry, {"id": "before-restart", "op": "ask", "lang": "es", "args": {"space": "agent", "text": "huerta"}})
    first = replies[-1][0]["text"]
    assert len(generated) == 1
    monkeypatch.setattr(miniapp, "_inline_pages", answer_inline.InlinePages())
    miniapp.handle(91, entry, {"id": "after-restart", "op": "ask", "lang": "es", "args": {
        "space": "agent", "text": "continúa", "history": history(first)}})
    assert "ya no está disponible" in replies[-1][0]["text"]
    assert len(generated) == 1 and not sent
