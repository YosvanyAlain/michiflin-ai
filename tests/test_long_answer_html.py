"""Offline HTML delivery: preserve answers, identity, follow-ups and deadlines."""
from __future__ import annotations

import json
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup

from michiflin import answer_html, chat, live_web, local_ai, miniapp, smart_agent, state


def long_text() -> str:
    return ("# Guía de prueba\n\n" + "Contenido útil y explicación. " * 68
            + "\n\n```html\n<div>uno & dos</div>\n<script>alert('not executed')</script>\n```"
            + "\n\nFuentes consultadas:\n[S1] https://example.test/reference\nFIN-INTEGRO")


def admission():
    return {"chat": 31, "addr": "person@example.test", "epoch": "epoch-one", "access_session": "grant-one", "build": "test-build"}


@pytest.fixture
def bound(monkeypatch, tmp_path):
    entry = admission()
    monkeypatch.setattr(miniapp.config, "TEMP_ROOT", tmp_path / "private")
    monkeypatch.setattr(state, "work_interrupted", lambda *_: False)
    monkeypatch.setattr(live_web, "_authorized", lambda _instance: (entry.copy(), entry["addr"]))
    monkeypatch.setattr(miniapp, "_html_history", answer_html.PreviewHistory())
    return entry


def test_elegant_standalone_html_is_inert_and_preserves_code_and_sources():
    text = long_text() + "\n\n<svg/onload=alert(1)> & a malicious <img src=https://evil.test/x>"
    source = '</style><script>alert("signature")</script>'
    document = answer_html.render(text, "es", signature=source, truncated=True)
    soup = BeautifulSoup(document, "html.parser")
    assert soup.html["lang"] == "es"
    assert soup.find("script") is None and soup.find("img") is None and soup.find("svg") is None
    assert not soup.find_all(href=True) and not soup.find_all(src=True)
    assert all(not key.lower().startswith("on") for tag in soup.find_all() for key in tag.attrs)
    assert soup.find("meta", attrs={"http-equiv": "Content-Security-Policy"})
    assert "default-src 'none'" in document and "form-action 'none'" in document
    assert "FIN-INTEGRO" in soup.get_text() and "https://example.test/reference" in soup.get_text()
    assert soup.pre.code.get_text() == "<div>uno & dos</div>\n<script>alert('not executed')</script>\n"
    assert source in soup.get_text() and "IA local" not in document  # signature was not invented
    assert "se cortó" in soup.find(class_="notice").get_text()
    assert "@media(max-width:480px)" in document and "prefers-color-scheme:dark" in document
    assert len(document.encode("utf-8")) < 15_000


def test_markdown_escaping_handles_unclosed_fence_numbering_and_language():
    document = answer_html.render("## Subtítulo\n\n10. diez\n12. doce\n\n```html\n<section><x>\n& final", "en")
    soup = BeautifulSoup(document, "html.parser")
    assert soup.h3.get_text() == "Subtítulo"
    assert [li.get("value") for li in soup.select("ol li")] == ["10", "12"]
    assert soup.select_one(".code-label").get_text() == "html"
    assert soup.pre.code.get_text() == "<section><x>\n& final"
    assert soup.html["lang"] == "en" and "Your answer" in soup.title.get_text()


def test_preview_is_brief_bilingual_and_never_swallows_the_notice_in_code():
    text = "```html\n" + "<div>safe</div>\n" * 150 + "```"
    es, en = answer_html.preview(text, "es", truncated=True), answer_html.preview(text, "en")
    assert len(es) < 500 and "El código continúa" in es and "quedó cortada" in es
    assert "enviado al chat" in es and "puede tardar" in es
    assert "sent to the chat" in en and "may take" in en
    assert es.count("```") % 2 == 0
    assert not answer_html.is_long("short")
    assert answer_html.is_long("x\n" * 23 + "x")


def test_html_size_guard_never_truncates_a_document():
    with pytest.raises(ValueError):
        answer_html.render("X" * (answer_html.MAX_TEXT_CHARS + 1), "es")
    # Small text that escapes to more than the byte guard also fails closed.
    with pytest.raises(ValueError):
        answer_html.render('"' * answer_html.MAX_TEXT_CHARS, "es", signature='"' * 160)


def test_success_sends_one_file_only_after_ack_and_cleans_private_temp(bound, monkeypatch):
    captured = {}

    def send(chat_id, text, file, filename, viewtype, interrupted):
        assert not interrupted() and chat_id == bound["chat"]
        captured.update(path=Path(file), name=filename, caption=text, body=Path(file).read_text(encoding="utf-8"))
        assert stat.S_IMODE(Path(file).stat().st_mode) == 0o600
        assert stat.S_IMODE(Path(file).parent.stat().st_mode) == 0o700
        assert viewtype == "File"
        return 51

    monkeypatch.setattr(chat, "send", send)
    original = {"text": long_text(), "sig": "IA local", "actions": [{"t": "/old"}], "files": [], "truncated": True}
    output = miniapp._long_answer(91, bound, {"space": "agent"}, "es", original, 0)
    assert captured["name"] == "respuesta-michiflin.html" and "FIN-INTEGRO" in captured["body"]
    assert "IA local" in captured["body"] and not captured["path"].exists()
    assert output["text"] != original["text"] and len(output["text"]) < 600
    assert "puede tardar" in output["text"] and "quedó cortada" in output["text"]
    assert output["files"] == [] and output["actions"] == [] and output["sig"] == "IA local"
    scope = miniapp._html_scope(bound, {"space": "agent"})
    assert miniapp._html_history.expand(91, bound["addr"], scope, [["assistant", output["text"]]])[0][1] == original["text"]
    assert miniapp._html_history.expand(91, "other@example.test", scope, [["assistant", output["text"]]])[0][1] == output["text"]
    other_grant = miniapp._html_scope(dict(bound, access_session="new-grant"), {"space": "agent"})
    assert miniapp._html_history.expand(91, bound["addr"], other_grant, [["assistant", output["text"]]])[0][1] == output["text"]
    assert original["text"] == long_text()  # original result was never mutated


def test_short_existing_attachment_and_error_are_not_changed(bound, monkeypatch):
    monkeypatch.setattr(chat, "send", lambda *_a, **_kw: pytest.fail("unexpected HTML"))
    for value in ({"text": "Hola", "files": []}, {"text": long_text(), "files": ["other.zip"]},
                  {"text": "⚠️ " + long_text(), "files": []}, {"text": long_text(), "error": "failed"}):
        assert miniapp._long_answer(91, bound, {}, "es", value, 0) is value


def test_rpc_failure_keeps_every_character_inline_and_no_false_files(bound, monkeypatch):
    paths = []

    def send(*_args, **kwargs):
        paths.append(Path(kwargs["file"]))
        assert not kwargs["interrupted"]()
        return 0

    monkeypatch.setattr(chat, "send", send)
    original = {"text": long_text(), "files": [], "sig": "Local AI"}
    output = miniapp._long_answer(91, bound, {}, "en", original, 0)
    assert output["text"].startswith(original["text"]) and "could not confirm" in output["text"]
    assert output["files"] == [] and output["sig"] == "Local AI" and not paths[0].exists()
    assert "sent to the chat" not in output["text"]


def test_oversize_and_no_encrypted_identity_fall_back_without_sending(bound, monkeypatch):
    monkeypatch.setattr(chat, "send", lambda *_a, **_kw: pytest.fail("unexpected HTML"))
    original = {"text": "x" * (answer_html.MAX_TEXT_CHARS + 1), "sig": "Local AI"}
    assert miniapp._long_answer(91, bound, {}, "en", original, 0)["text"].startswith(original["text"])
    monkeypatch.setattr(live_web, "_authorized", lambda _id: None)
    original = {"text": long_text()}
    assert miniapp._long_answer(91, bound, {}, "en", original, 0)["text"].startswith(original["text"])


@pytest.mark.parametrize("field,replacement", [("addr", "someone@example.test"), ("chat", 32),
                                                ("epoch", "new-epoch"), ("access_session", "new-grant")])
def test_recipient_or_grant_change_refuses_the_attachment(bound, monkeypatch, field, replacement):
    current = dict(bound, **{field: replacement})
    monkeypatch.setattr(live_web, "_authorized", lambda _id: (current, current["addr"]))
    monkeypatch.setattr(chat, "send", lambda *_a, **_kw: pytest.fail("incorrect recipient"))
    result = {"text": long_text()}
    assert miniapp._long_answer(91, bound, {}, "es", result, 0)["text"].startswith(result["text"])


def test_revocation_between_creation_and_handoff_rechecks_inside_send(bound, monkeypatch):
    current = {"ok": True}
    monkeypatch.setattr(live_web, "_authorized", lambda _id: (bound.copy(), bound["addr"]) if current["ok"] else None)
    paths = []

    def send(*_args, **kwargs):
        paths.append(Path(kwargs["file"]))
        current["ok"] = False  # revoked after creation, just before send's locked guard
        assert kwargs["interrupted"]()
        return 0

    monkeypatch.setattr(chat, "send", send)
    original = {"text": long_text()}
    output = miniapp._long_answer(91, bound, {}, "es", original, 0)
    assert output["text"].startswith(original["text"]) and not paths[0].exists()


def test_real_chat_send_calls_guard_before_rpc(bound, monkeypatch):
    data_seen = []
    fake = SimpleNamespace(rpc=SimpleNamespace(send_msg=lambda acc, target, data: (data_seen.append((acc, target, Path(data.file).read_text())), 62)[1]))
    monkeypatch.setattr(chat, "_bot", fake)
    monkeypatch.setattr(chat, "_accid", 1)
    original = {"text": long_text()}
    output = miniapp._long_answer(91, bound, {}, "es", original, 0)
    assert len(data_seen) == 1 and data_seen[0][:2] == (1, bound["chat"])
    assert "FIN-INTEGRO" in data_seen[0][2] and "enviado al chat" in output["text"]


def test_real_deltachat_core_copies_attachment_before_return_without_network(tmp_path):
    """Synthetic self-chat, no I/O started and no real identity or message sent."""
    from deltachat2 import IOTransport, MessageData, Rpc

    executable = shutil.which("deltachat-rpc-server", path=str(Path(sys.executable).parent)) or shutil.which("deltachat-rpc-server")
    assert executable, "the same Delta Chat RPC package used by the bot is required"
    source = tmp_path / "answer.html"
    content = answer_html.render("Offline copy check", "en")
    source.write_text(content, encoding="utf-8")
    with IOTransport(accounts_dir=str(tmp_path / "accounts"), rpc_executable=executable, stderr=subprocess.DEVNULL) as transport:
        rpc = Rpc(transport)
        account = rpc.add_account()
        rpc.set_config(account, "addr", "offline@example.invalid")
        rpc.set_config(account, "configured_addr", "offline@example.invalid")
        self_chat = rpc.create_chat_by_contact_id(account, 1)
        message_id = rpc.send_msg(account, self_chat, MessageData(text="Offline test", file=str(source),
                                                                   filename="answer.html", viewtype="File"))
        stored = Path(rpc.get_message(account, message_id).file)
        assert stored != source and stored.is_file()
        source.unlink()
        assert stored.read_text(encoding="utf-8") == content
        assert Path(rpc.get_message(account, message_id).file).read_text(encoding="utf-8") == content


def test_preview_cache_is_scoped_bounded_expires_and_avoids_collisions():
    now = [100.0]
    cache = answer_html.PreviewHistory(ttl=30, maximum=3, per_instance=2, clock=lambda: now[0])
    history = [["user", "q"], ["assistant", "preview"]]
    cache.remember(1, "Alice@example.test", "agent", "preview", "the full answer")
    assert cache.expand(1, "alice@example.test", "agent", history)[1][1] == "the full answer"
    for owner, instance, space in [("bob@example.test", 1, "agent"), ("alice@example.test", 2, "agent"),
                                   ("alice@example.test", 1, "smart_agent")]:
        assert cache.expand(instance, owner, space, history) == history
    cache.remember(1, "Alice@example.test", "agent", "preview", "a different full answer")
    assert cache.expand(1, "alice@example.test", "agent", history) == history  # ambiguous
    cache.remember(1, "Alice@example.test", "agent", "p2", "START" + "x" * 7000 + "LAST")
    restored = cache.expand(1, "alice@example.test", "agent", [["assistant", "p2"]])[0][1]
    assert len(restored) <= 6000 and restored.startswith("START") and restored.endswith("LAST")
    cache.remember(1, "Alice@example.test", "agent", "p3", "full3")
    assert len(cache._items) == 2
    now[0] += 31
    assert cache.expand(1, "alice@example.test", "agent", [["assistant", "p3"]]) == [["assistant", "p3"]]
    assert not cache._items


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
    assert payloads[-1][1]["max_tokens"] == 768 and payloads[-1][2] == 125
    monkeypatch.setattr(local_ai._turns, "waiting", lambda: 2)
    assert local_ai.complete("question", "en", sender="limit-test@example.test").status == "ok"
    assert payloads[-1][1]["max_tokens"] == 384  # an existing queue gets shorter turns
    data.update(text="x" * (local_ai._MAX_GENERATED_CHARS + 3), finish="stop")
    result = local_ai.complete("question", "es", sender="limit-test@example.test")
    assert len(result.text) == local_ai._MAX_GENERATED_CHARS and result.truncated
    data.update(text="partial", finish="length")
    result = local_ai.complete("question", "en", sender="limit-test@example.test")
    assert result.truncated and "ask to continue" in local_ai.display(result, "en")


def test_local_memory_keeps_start_and_end_bounded_and_per_sender():
    user = "long-memory@example.test"
    local_ai.forget(444, user)
    body = "START-IMPORTANT " + "x" * 1500 + " END-WHERE-IT-STOPPED"
    local_ai.remember(444, user, "question", body)
    history = local_ai.history(444, user)
    assistant = history[-1][1]
    assert len(assistant) <= 600 and "START-IMPORTANT" in assistant and "END-WHERE-IT-STOPPED" in assistant
    assert local_ai.history(444, "somebody@example.test") == []
    assert "END-WHERE-IT-STOPPED" in local_ai._history(history)[-1]["content"]
    local_ai.forget(444, user)


def test_smart_agent_no_silent_nine_thousand_character_cut(monkeypatch):
    raw = "x" * 12000 + "END"
    monkeypatch.setattr(smart_agent.ai, "complete", lambda *_a, **_kw: SimpleNamespace(ok=True, text=json.dumps({"answer": raw}),
                                                                                        truncated=False, signature=lambda: "test"))
    monkeypatch.setattr(state, "check_work_allowed", lambda: None)
    answer = smart_agent.run("question", "en")
    assert answer["text"] == raw and "truncated" not in answer
    parsed = smart_agent._parse(json.dumps({"answer": "x" * (smart_agent.MAX_ANSWER_CHARS + 1)}))
    assert parsed and parsed.get("truncated") and len(parsed["answer"]) == smart_agent.MAX_ANSWER_CHARS


def test_timeout_never_sends_late_html(bound, monkeypatch):
    sent, replies = [], []
    monkeypatch.setattr(miniapp.access, "bound_allowed", lambda _e: True)
    monkeypatch.setattr(miniapp.admin, "is_allowed", lambda _a: True)
    monkeypatch.setattr(miniapp.admin, "seen", lambda _a: None)
    monkeypatch.setattr(miniapp, "_verified_binding", lambda _e: True)
    monkeypatch.setattr(miniapp.config, "service_access_allowed", lambda _a: True)
    monkeypatch.setattr(state, "maintenance_active", lambda: False)
    monkeypatch.setattr(state, "get_kill_switch", lambda _a: False)
    monkeypatch.setattr(state, "kill_generation", lambda _a: 0)
    monkeypatch.setattr(miniapp, "app_build", lambda: "test-build")
    monkeypatch.setattr(live_web, "_identity", lambda _id: (bound.copy(), bound["addr"]))
    monkeypatch.setattr(miniapp, "respond", lambda _id, _rid, data, error="": replies.append((data, error)))
    monkeypatch.setattr(chat, "send", lambda *_a, **_kw: sent.append(True) or 1)
    monkeypatch.setattr(miniapp.usage, "note", lambda *_a, **_kw: None)
    monkeypatch.setattr(miniapp.usage, "quota_view", lambda _a: {})
    monkeypatch.setitem(miniapp.OP_LIMITS, "ask", 0.01)

    def slow(_chat, _args, _locale):
        time.sleep(0.08)
        return {"text": long_text()}

    monkeypatch.setitem(miniapp.OPS, "ask", slow)
    miniapp.handle(91, bound, {"id": "request1", "op": "ask", "lang": "en", "args": {}})
    time.sleep(0.12)  # allow the abandoned worker to finish
    assert len(replies) == 1 and "too long" in replies[0][1]
    assert not sent


def test_handle_attaches_once_and_only_restores_history_for_verified_identity(bound, monkeypatch):
    sent, replies, histories = [], [], []
    monkeypatch.setattr(miniapp.access, "bound_allowed", lambda _e: True)
    monkeypatch.setattr(miniapp.admin, "is_allowed", lambda _a: True)
    monkeypatch.setattr(miniapp.admin, "seen", lambda _a: None)
    monkeypatch.setattr(miniapp, "_verified_binding", lambda _e: True)
    monkeypatch.setattr(miniapp.config, "service_access_allowed", lambda _a: True)
    monkeypatch.setattr(state, "maintenance_active", lambda: False)
    monkeypatch.setattr(state, "get_kill_switch", lambda _a: False)
    monkeypatch.setattr(state, "kill_generation", lambda _a: 0)
    monkeypatch.setattr(miniapp, "app_build", lambda: "test-build")
    verified = {"value": True}
    monkeypatch.setattr(live_web, "_identity", lambda _id: (bound.copy(), bound["addr"]) if verified["value"] else None)
    monkeypatch.setattr(miniapp, "respond", lambda _id, _rid, data, error="": replies.append((data, error)))
    monkeypatch.setattr(miniapp.usage, "note", lambda *_a, **_kw: None)
    monkeypatch.setattr(miniapp.usage, "quota_view", lambda _a: {})

    def send(*_a, **kw):
        assert not kw["interrupted"]() and Path(kw["file"]).is_file()
        sent.append(Path(kw["file"]).read_text(encoding="utf-8"))
        return 70

    def ask(_chat, args, _locale):
        histories.append(args.get("history"))
        return {"text": long_text() if len(histories) == 1 else "short answer", "sig": "test"}

    monkeypatch.setattr(chat, "send", send)
    monkeypatch.setitem(miniapp.OPS, "ask", ask)
    miniapp.handle(91, bound, {"id": "request2", "op": "ask", "lang": "en", "args": {"space": "auto", "history": []}})
    assert len(sent) == 1 and len(replies) == 1 and not replies[0][1]
    preview = replies[0][0]["text"]
    assert "sent to the chat" in preview and "FIN-INTEGRO" in sent[0]
    followup = {"space": "auto", "history": [["assistant", preview]]}
    miniapp.handle(91, bound, {"id": "request3", "op": "ask", "lang": "en", "args": followup})
    assert histories[-1][0][1] == long_text() and len(sent) == 1
    verified["value"] = False
    miniapp.handle(91, bound, {"id": "request4", "op": "ask", "lang": "en", "args": followup})
    assert histories[-1][0][1] == preview and len(sent) == 1


def test_model_budget_reduces_with_real_waiters_and_preserves_fifo(monkeypatch):
    import threading
    from michiflin.fair_local_ai import FairTurns

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
    assert budgets == {"alice": 768, "bob": 384, "carol": 768}
