"""No user accounts, external providers, model downloads or live data in these tests."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import pytest

from michiflin import agent, ai, chat, local_ai, state, tools


@pytest.fixture
def server(monkeypatch):
    requests = []
    response = {"text": "Hola desde el modelo local.", "finish_reason": "stop"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _json(self, value, code=200):
            data = json.dumps(value).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            requests.append(("GET", self.path, None))
            self._json({"status": "ok"} if self.path == "/health" else {}, 200 if self.path == "/health" else 404)

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            requests.append(("POST", self.path, payload))
            self._json({"choices": [{"message": {"content": response["text"]}, "finish_reason": response["finish_reason"]}]})

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("LOCAL_GGUF_ENABLED", "1")
    monkeypatch.setenv("LOCAL_GGUF_PORT", str(httpd.server_port))
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    yield requests, response
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=3)


def capture(message, *, history=None, sender="alice@example.test"):
    with mock.patch.object(chat, "sender", return_value=sender), mock.patch.object(state, "get_kill_switch", return_value=False), \
         mock.patch.object(ai, "complete", side_effect=AssertionError("provider model used")), \
         mock.patch.object(ai, "complete_exact", side_effect=AssertionError("provider model used")):
        chat.start_capture()
        try:
            agent.handle(501, message, "es", history)
        finally:
            captured = chat.stop_capture()
    return "\n".join(captured)


def test_disabled_by_default_and_on_actions(monkeypatch):
    monkeypatch.delenv("LOCAL_GGUF_ENABLED", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert not local_ai.configured()
    monkeypatch.setenv("LOCAL_GGUF_ENABLED", "1")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert not local_ai.configured()
    assert local_ai.complete("Hola", "es").status == "disabled"


@pytest.mark.parametrize("query", ["¿Qué puede hacer?", "que puede hacer michiflin", "¿Qué eres capaz de hacer?", "¿Qué puedes hacer?"])
def test_capability_questions_are_local_even_when_no_model(monkeypatch, query):
    monkeypatch.delenv("LOCAL_GGUF_ENABLED", raising=False)
    with mock.patch.object(tools, "search", side_effect=AssertionError("unnecessary search")), \
         mock.patch.object(local_ai, "complete", side_effect=AssertionError("unnecessary model")):
        text = capture(query)
    assert "clima" in text.lower()
    assert "programador" in text.lower()
    assert "sin ningún modelo" in text.lower()


def test_capabilities_are_truthful_when_local_is_ready(server):
    requests, _ = server
    text = capture("¿Qué puede hacer?")
    assert "ia local" in text.lower() and "buscador" in text.lower()
    assert "no ejecuto código" in text.lower()
    assert not any(method == "POST" for method, _, _ in requests)


def test_chat_and_code_use_only_loopback_and_no_search(server):
    requests, _ = server
    with mock.patch.object(tools, "search", side_effect=AssertionError("code must not be sent to web")):
        text = capture("Escribe una función Python para sumar pares en 2026.", history=[["user", "Contexto privado"], ["assistant", "Entendido"]])
    assert "modelo local" in text.lower() and "sin proveedor externo" in text.lower()
    posts = [item for item in requests if item[0] == "POST"]
    assert len(posts) == 1 and posts[0][1] == "/v1/chat/completions"
    body = posts[0][2]
    assert body["max_tokens"] == 256 and body["chat_template_kwargs"]["enable_thinking"] is False
    assert body["messages"][1]["content"] == "Contexto privado"
    assert body["messages"][-1]["content"].startswith("Escribe una función")


def test_loopback_host_is_not_configurable(monkeypatch):
    monkeypatch.setenv("LOCAL_GGUF_PORT", "http://evil.example")
    conn = mock.Mock()
    conn.getresponse.return_value.status = 200
    conn.getresponse.return_value.read.return_value = b'{"status":"ok"}'
    with mock.patch.object(local_ai.http.client, "HTTPConnection", return_value=conn) as factory:
        assert local_ai._http("/health", None, 1) == {"status": "ok"}
    factory.assert_called_once_with("127.0.0.1", 18971, timeout=1)


def test_research_uses_only_current_query_and_actual_sources(server):
    requests, response = server
    response["text"] = "Abre hasta las 17:00 [S1]. Falso [S99]. https://example.bad/falso"
    hit = tools.Result("Biblioteca Sol", "https://public.example.test/horario", "test")
    with mock.patch.object(tools, "search", return_value=[hit]) as search, \
         mock.patch.object(local_ai.smart_agent, "_read", return_value="Biblioteca: lunes 09:00 a 17:00.") as read:
        text = capture("Busca el horario actual de la biblioteca", history=[["user", "Dato privado previo"], ["assistant", "Ok"]])
    search.assert_called_once_with("Busca el horario actual de la biblioteca", "es", 5, engine="standard")
    read.assert_called_once_with(hit.url, total_timeout=12.0)
    post = [p for method, _, p in requests if method == "POST"][0]
    current_prompt = post["messages"][-1]["content"]
    assert "17:00" in current_prompt and hit.url not in current_prompt
    assert "[S1]" in text and "[?]" in text and "[enlace no verificado]" in text
    assert hit.url in text and "example.bad" not in text


def test_private_and_long_query_are_not_sent_to_search(server):
    requests, _ = server
    with mock.patch.object(tools, "search", side_effect=AssertionError("private data transmitted")):
        private = capture("Busca el token secreto de hoy: abc")
        too_long = capture("busca " + "tema " * 50)
    assert "no la envié al buscador" in private
    assert "200 caracteres" in too_long
    assert not any(method == "POST" for method, _, _ in requests)


def test_unavailable_model_does_not_search_or_change_provider(server):
    requests, _ = server
    with mock.patch.object(local_ai, "ready", return_value=False), \
         mock.patch.object(tools, "search", side_effect=AssertionError("unexpected search")):
        text = capture("Busca el horario actual de la biblioteca")
    assert "no está disponible" in text and "no cambié a una API externa" in text
    assert not any(method == "POST" for method, _, _ in requests)


def test_no_sources_does_not_make_up_current_facts(server):
    requests, _ = server
    with mock.patch.object(tools, "search", return_value=[]):
        text = capture("¿Cuál es el precio actual de un libro?")
    assert "no puedo verificar" in text.lower()
    assert not any(method == "POST" for method, _, _ in requests)


def test_research_never_opens_invalid_or_unsafe_addresses(server):
    requests, _ = server
    hits = [tools.Result("x", "http://127.0.0.1/private"), tools.Result("y", "file:///etc/passwd"),
            tools.Result("z", "https://public.example.test/redirect")]
    with mock.patch.object(tools, "search", return_value=hits), \
         mock.patch.object(local_ai.smart_agent, "_read", side_effect=ValueError("private_url")) as read:
        result = local_ai.complete("busca resultados actuales", "es", research=True)
    assert result.status == "no_sources"
    assert read.call_count == 2  # parser rejects file:; reader validates addresses and redirects
    assert not any(method == "POST" for method, _, _ in requests)


def test_single_slot_returns_busy_without_queue(server):
    entered, finish = threading.Event(), threading.Event()
    actual = local_ai._http
    def slow(path, payload, timeout):
        if path == "/v1/chat/completions":
            entered.set()
            assert finish.wait(3)
        return actual(path, payload, timeout)
    with mock.patch.object(local_ai, "_http", side_effect=slow):
        done = []
        thread = threading.Thread(target=lambda: done.append(local_ai.complete("Cuéntame algo", "es")))
        thread.start()
        assert entered.wait(3)
        busy = local_ai.complete("Otra pregunta", "es")
        finish.set()
        thread.join(5)
    assert busy.status == "busy" and done[0].status == "ok"


def test_memory_is_scoped_by_chat_and_sender_and_forget():
    local_ai.remember(19, "alice@example.test", "Mi palabra es limón", "Entendido")
    local_ai.remember(19, "bob@example.test", "Mi palabra es coco", "Entendido")
    assert "limón" in str(local_ai.history(19, "alice@example.test"))
    assert "limón" not in str(local_ai.history(19, "bob@example.test"))
    assert local_ai.history(20, "alice@example.test") == []
    local_ai.forget(19, "alice@example.test")
    assert local_ai.history(19, "alice@example.test") == []
    assert "coco" in str(local_ai.history(19, "bob@example.test"))
    local_ai.forget(19, "bob@example.test")


def test_followup_uses_prior_local_context_only_for_same_sender(server):
    requests, _ = server
    capture("Escríbeme un poema breve", sender="alice@example.test")
    capture("continúa", sender="alice@example.test")
    posts = [p for method, _, p in requests if method == "POST"]
    assert len(posts) == 2
    assert any("poema" in item["content"] for item in posts[-1]["messages"])
    assert local_ai.history(501, "other@example.test") == []
    local_ai.forget(501, "alice@example.test")


def test_kill_switch_prevents_inference_and_search(server):
    requests, _ = server
    with mock.patch.object(chat, "sender", return_value="alice@example.test"), \
         mock.patch.object(state, "get_kill_switch", return_value=True), \
         mock.patch.object(tools, "search", side_effect=AssertionError("kill switch bypass")):
        chat.start_capture()
        agent.handle(501, "busca algo hoy", "es")
        output = " ".join(chat.stop_capture())
    assert "kill switch" in output.lower()
    assert requests == []


def test_malformed_model_response_fails_closed(server):
    requests, response = server
    response["text"] = ""
    result = local_ai.complete("Hola", "es")
    assert result.status == "unavailable"
    assert len([x for x in requests if x[0] == "POST"]) == 1


def test_long_input_is_rejected_without_truncating_or_searching(server):
    requests, _ = server
    result = local_ai.complete("x" * 3001, "es")
    assert result.status == "long_input" and requests == []
