"""Concurrent synthetic users only: no account, provider, or live bot."""
from __future__ import annotations

import threading
from unittest import mock

import pytest

from michiflin.fair_local_ai import FairTurns


def test_one_turn_per_person_fifo_and_no_priority():
    gate = FairTurns(max_waiters=4, timeout=5)
    status, first = gate.acquire("Alice@example.test")
    assert status == "ok"
    assert gate.acquire("  ALICE@example.test ")[0] == "duplicate"
    waiting = {person: threading.Event() for person in ("bob", "carol", "alice")}
    started = {person: threading.Event() for person in ("bob", "carol", "alice")}
    release = {person: threading.Event() for person in ("bob", "carol", "alice")}
    order = []
    failures = []

    def task(person):
        status, ticket = gate.acquire(person + "@example.test", on_wait=waiting[person].set)
        if status != "ok":
            failures.append(status)
            return
        order.append(person)
        started[person].set()
        if not release[person].wait(5):
            failures.append("release_timeout")
        gate.release(ticket)

    bob = threading.Thread(target=task, args=("bob",))
    carol = threading.Thread(target=task, args=("carol",))
    bob.start(); assert waiting["bob"].wait(2)
    carol.start(); assert waiting["carol"].wait(2)
    gate.release(first)
    alice = threading.Thread(target=task, args=("alice",))
    alice.start()
    assert started["bob"].wait(2)
    assert waiting["alice"].wait(2)
    release["bob"].set(); assert started["carol"].wait(2)
    release["carol"].set(); assert started["alice"].wait(2)
    release["alice"].set()
    for thread in (bob, carol, alice):
        thread.join(3)
        assert not thread.is_alive()
    assert failures == []
    assert order == ["bob", "carol", "alice"]


def test_queue_is_bounded_and_cleans_up_after_timeout():
    gate = FairTurns(max_waiters=1, timeout=0.15)
    status, first = gate.acquire("alice")
    assert status == "ok"
    waiting = threading.Event()
    results = []
    worker = threading.Thread(target=lambda: results.append(gate.acquire("bob", on_wait=waiting.set)))
    worker.start(); assert waiting.wait(2)
    assert gate.acquire("carol")[0] == "busy"
    worker.join(2)
    assert results[0][0] == "wait_timeout"
    gate.release(first)
    status, ticket = gate.acquire("bob")
    assert status == "ok"
    gate.release(ticket)


def test_cancellation_removes_waiter_and_does_not_block_others():
    gate = FairTurns(max_waiters=1, timeout=5)
    _, first = gate.acquire("alice")
    waiting = threading.Event()
    cancel = threading.Event()
    done = threading.Event()
    errors = []

    def check():
        if cancel.is_set():
            raise ValueError("synthetic_pause")

    def task():
        try:
            gate.acquire("bob", check=check, on_wait=waiting.set)
        except ValueError as exc:
            errors.append(str(exc))
        finally:
            done.set()

    worker = threading.Thread(target=task)
    worker.start(); assert waiting.wait(2)
    cancel.set(); assert done.wait(2)
    assert errors == ["synthetic_pause"]
    gate.release(first)
    status, ticket = gate.acquire("carol")
    assert status == "ok"
    gate.release(ticket)
    worker.join(1)


def test_unknown_senders_share_one_identity():
    gate = FairTurns()
    status, ticket = gate.acquire("")
    assert status == "ok" and gate.acquire(" ")[0] == "duplicate"
    gate.release(ticket)


def test_release_requires_the_actual_ticket():
    gate = FairTurns()
    _, ticket = gate.acquire("alice")
    with pytest.raises(RuntimeError):
        gate.release(object())
    gate.release(ticket)


@pytest.mark.parametrize("enabled", ["0", "1"])
def test_existing_app_has_no_prefix_and_does_not_infer_source_from_text(monkeypatch, enabled):
    from michiflin import chat, local_ai, miniapp
    monkeypatch.setenv("LOCAL_GGUF_ENABLED", enabled)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    # Even if a tool's output contains the same footer, it is not a trusted
    # model result and must remain intact and unattributed.
    plain = "respuesta de una herramienta\n\n" + local_ai.model_footer("es")
    def tool_reply(chat_id, *_args):
        chat.text(chat_id, plain)
        return None
    with mock.patch.object(miniapp.agent, "handle", side_effect=tool_reply):
        result = miniapp.op_ask(11, {"space": "agent", "text": "hola"}, "es")
    assert result["text"] == plain
    assert not result.get("sig")
    assert not result["text"].startswith("Prueba:")


def test_missing_recognized_footer_is_left_untouched_without_false_signature(monkeypatch):
    from michiflin import chat, local_ai, miniapp
    monkeypatch.setenv("LOCAL_GGUF_ENABLED", "1")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    def changed_reply(chat_id, *_args):
        chat.text(chat_id, "Texto intacto, sin el pie esperado")
        return local_ai.Result("ok", "Texto intacto")
    with mock.patch.object(miniapp.agent, "handle", side_effect=changed_reply):
        result = miniapp.op_ask(11, {"space": "agent", "text": "hola"}, "es")
    assert result["text"] == "Texto intacto, sin el pie esperado"
    assert not result.get("sig")


def test_only_interacting_app_gets_one_non_notifying_disclosure(monkeypatch):
    from michiflin import miniapp
    monkeypatch.setenv("LOCAL_GGUF_ENABLED", "1")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    miniapp._local_ai_disclosed.clear()
    with mock.patch.object(miniapp.maintenance, "silent_active", return_value=False), \
         mock.patch.object(miniapp, "_send_status", return_value=True) as send:
        miniapp._local_ai_disclosure(10)
        miniapp._local_ai_disclosure(10)
        miniapp._local_ai_disclosure(11)
    assert send.call_count == 2
    for call in send.call_args_list:
        assert len(call.args) == 2  # no notify / no mass message
        assert call.args[1]["kind"] == "notice"
        assert "IA local" in call.args[1]["data"]["es"]
    miniapp._local_ai_disclosed.clear()


def test_actions_local_model_requires_opt_in_socket_and_private_key(monkeypatch, tmp_path):
    from michiflin import local_ai
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("LOCAL_GGUF_ENABLED", "1")
    monkeypatch.delenv("LOCAL_GGUF_ACTIONS_TEST", raising=False)
    assert not local_ai.configured()
    monkeypatch.setenv("LOCAL_GGUF_ACTIONS_TEST", "1")
    assert local_ai.configured() and not local_ai.ready()
    monkeypatch.setenv("LOCAL_GGUF_SOCKET_PATH", "/tmp/attacker.sock")
    key = tmp_path / "local-key"
    key.write_text("a" * 64, encoding="ascii")
    monkeypatch.setenv("LOCAL_GGUF_KEY_FILE", str(key))
    with pytest.raises(ValueError, match="socket_required"):
        local_ai._connection(1)
    monkeypatch.setenv("LOCAL_GGUF_SOCKET_PATH", "/run/michiflin-local-gguf/llama.sock")
    fake = mock.Mock()
    fake.getresponse.return_value.status = 200
    fake.getresponse.return_value.read.return_value = b'{"status":"ok"}'
    with mock.patch.object(local_ai, "_UnixHTTPConnection", return_value=fake) as factory:
        assert local_ai.ready()
    factory.assert_called_once_with("/run/michiflin-local-gguf/llama.sock", 1.5)
    headers = fake.request.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer " + "a" * 64
    key.write_text("x\nmalicious-header", encoding="ascii")
    assert not local_ai.ready()


def test_action_test_workflow_is_fail_closed_and_preserves_handover():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/michiflin.yml").read_text()
    labels = ("Prepare isolated local AI test", "Restore state", "Verify backup recovery",
              "Start isolated local AI test", "Local AI test ready", "Private session",
              "Clean temporary local AI test", "Next session")
    offsets = [workflow.index("- name: " + label) for label in labels]
    assert offsets == sorted(offsets)
    prepare = workflow[offsets[0]:offsets[0] + 300]
    start = workflow[offsets[3]:offsets[4]]
    ready = workflow[offsets[4]:offsets[5]]
    assert "continue-on-error: true" in prepare and "continue-on-error: true" in start
    assert "steps.gguf_prepare.outcome == 'success'" in start
    assert "steps.gguf_start.outcome == 'success'" in ready
    assert "LOCAL_GGUF_ACTIONS_TEST=1" in ready
    assert "LOCAL_GGUF_SOCKET_PATH=/run/michiflin-local-gguf/llama.sock" in ready
    script = (root / "tools/local_gguf_session.sh").read_text()
    for protection in ("PrivateNetwork=yes", "RestrictAddressFamilies=AF_UNIX", "ProtectHome=yes",
                       "NoNewPrivileges=yes", "MemoryMax=7G", "CPUQuota=200%", "--api-key-file", "sha256sum --check"):
        assert protection in script
    session = workflow[offsets[5]:offsets[6]]
    assert "python state_backup.py save" in session and "local_gguf_session.sh stop" in session


def test_failed_startup_artifact_contains_only_safe_diagnostics():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/michiflin.yml").read_text()
    for mode in ("prepare", "start"):
        assert f'gguf-{mode}-raw.log' in workflow
        assert f'gguf-{mode}-status.log' in workflow
    assert "SAFE_GGUF_PREFLIGHT memory_kib=[0-9]+ disk_kib=[0-9]+ cgroup=(max|[0-9]+)" in workflow
    artifact = workflow[workflow.index("- name: Save local AI startup diagnostics"):workflow.index("- name: Local AI test ready")]
    assert "gguf-*-status.log" in artifact and "raw.log" not in artifact
    assert "retention-days: 1" in artifact and "continue-on-error: true" in artifact
    script = (root / "tools/local_gguf_session.sh").read_text()
    assert script.count("--retry 2") == 2
