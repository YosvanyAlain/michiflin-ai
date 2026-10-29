"""Offline security and regression tests for personal providers and upgrades.

All credentials are synthetic. No real accounts, provider billing or internet are used.
"""
from __future__ import annotations

import base64
import io
import json
import os
import socket
import ssl
import http.server
import tarfile
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from michiflin import ai, chat, config, egress, images, live_web, maintenance, miniapp, personal_ai, state, tools
import state_backup

ALICE = "alice@example.org"
BOB = "bob@example.org"
FAKE_KEY = "synthetic-Alice-key-never-real-123"


def seal(info, payload):
    public = serialization.load_der_public_key(base64.b64decode(info["public_key"]))
    raw = os.urandom(32)
    iv = os.urandom(12)
    body = json.dumps({**payload, "challenge": info["challenge"]}).encode()
    encrypted = AESGCM(raw).encrypt(iv, body, personal_ai.AAD)
    wrapped = public.encrypt(raw, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    return {"key": base64.b64encode(wrapped).decode(), "iv": base64.b64encode(iv).decode(),
            "data": base64.b64encode(encrypted).decode()}


class Isolated(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="personal-ai-test-")
        path = Path(self.temp.name)
        self.patches = [mock.patch.object(config, "CONFIG_DIR", path), mock.patch.object(state, "CONFIG_DIR", path),
                        mock.patch.object(state, "STATE_FILE", path / "state.json"),
                        mock.patch.dict(os.environ, {"WEB_SESSION_KEY": "local-test-master-" + "X" * 48}),
                        mock.patch.object(state, "check_work_allowed"), mock.patch.object(state, "_save_soon")]
        for item in self.patches:
            item.start()
        with state.lock:
            self.old_data = dict(state.data)
            state.data["usage"] = {}
            state.data["personal_ai_budgets"] = {}
            state.data["apps"] = {}
            state.data["maintenance_notice"] = {}
        state.act_as(ALICE)
        state.set_personal_ai_secure(True)
        personal_ai._challenges.clear()

    def tearDown(self):
        with state.lock:
            state.data.clear()
            state.data.update(self.old_data)
        state.act_as("")
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def add(self, address=ALICE, key=FAKE_KEY, kind="openai", daily=0, models=None):
        rows = models if models is not None else [{"id": "chat-one"}, {"id": "gpt-image-2"}, {"id": "text-embedding-3"}]
        data = {"models" if kind == "gemini" else "data": rows}
        with mock.patch.object(personal_ai, "_request", return_value=data):
            result = personal_ai.add(address, {"name": "Synthetic", "base": "https://api.example.com/v1",
                                              "key": key, "kind": kind, "daily": daily})
        return result["providers"][-1]

    def test_handshake_encryption_sender_binding_replay_and_tampering(self):
        alice = personal_ai.handshake(ALICE)
        bob = personal_ai.handshake(BOB)
        ciphertext = seal(alice, {"key": FAKE_KEY, "base": "https://api.example.com"})
        self.assertNotIn(FAKE_KEY, json.dumps(ciphertext))
        with self.assertRaises(personal_ai.PersonalError):
            personal_ai.unseal(BOB, ciphertext)
        result = personal_ai.unseal(ALICE, ciphertext)
        self.assertEqual(result["key"], FAKE_KEY)
        with self.assertRaises(personal_ai.PersonalError):
            personal_ai.unseal(ALICE, ciphertext)
        fresh = personal_ai.handshake(BOB)
        altered = seal(fresh, {"key": FAKE_KEY})
        data = bytearray(base64.b64decode(altered["data"]))
        data[0] ^= 1
        altered["data"] = base64.b64encode(data).decode()
        with self.assertRaises(personal_ai.PersonalError):
            personal_ai.unseal(BOB, altered)
        self.assertNotEqual(alice["challenge"], bob["challenge"])

    def test_private_vault_at_rest_backup_and_cross_user_isolation(self):
        personal_ai.handshake(ALICE)
        provider = self.add()
        vault = personal_ai._path(ALICE)
        encrypted = vault.read_bytes()
        self.assertNotIn(FAKE_KEY.encode(), encrypted)
        self.assertNotIn(ALICE.encode(), encrypted)
        self.assertNotIn(FAKE_KEY, json.dumps(personal_ai.public_list(ALICE)))
        self.assertEqual(personal_ai.public_list(BOB), [])
        with self.assertRaises(personal_ai.PersonalError):
            personal_ai.lookup(BOB, provider["id"] + ":chat-one")
        self.assertEqual(vault.stat().st_mode & 0o777, 0o600)
        self.assertEqual(vault.parent.stat().st_mode & 0o777, 0o700)
        archive = state_backup.archive(Path(self.temp.name))
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            self.assertIn("michiflin/personal-ai-v1/" + vault.name, tar.getnames())
            backed_up = tar.extractfile("michiflin/personal-ai-v1/" + vault.name).read()
            self.assertNotIn(FAKE_KEY.encode(), backed_up)
        with mock.patch.dict(os.environ, {"WEB_SESSION_KEY": "wrong-but-long-" + "Y" * 48}):
            # The encrypted transport-key canary detects a rotated master key
            # rather than making the vault deceptively appear empty.
            with self.assertRaises(personal_ai.PersonalError):
                personal_ai.public_list(ALICE)
        with mock.patch.dict(os.environ, {"WEB_SESSION_KEY": "short"}):
            with self.assertRaises(personal_ai.PersonalError):
                personal_ai.handshake(ALICE)

    def test_catalog_and_manual_capabilities_no_generation_probes(self):
        provider = self.add()
        ids = [m["id"] for m in provider["models"]]
        self.assertEqual(ids, ["chat-one", "gpt-image-2"])
        by_id = {m["id"]: m for m in provider["models"]}
        self.assertTrue(by_id["chat-one"]["chat"])
        self.assertFalse(by_id["chat-one"]["vision"])
        self.assertTrue(by_id["gpt-image-2"]["image"])
        personal_ai.configure_model(ALICE, provider["id"], "custom-model", True, True, False)
        self.add()  # identical endpoint + key updates rather than duplicating
        providers = personal_ai.public_list(ALICE)
        self.assertEqual(len(providers), 1)
        self.assertEqual(providers[0]["id"], provider["id"])
        custom = next(m for m in providers[0]["models"] if m["id"] == "custom-model")
        self.assertTrue(custom["vision"])
        self.assertEqual(custom["source"], "manual")
        personal_ai.configure_model(ALICE, provider["id"], "chat-one", False, False, False)
        self.assertNotIn(provider["id"] + ":chat-one", [m.key for p in personal_ai.providers(ALICE) for m in p.models])
        self.add(key="different-synthetic-key")
        self.assertEqual(len(personal_ai.public_list(ALICE)), 2)

    def test_gemini_and_anthropic_declared_metadata(self):
        gemini = self.add(kind="gemini", models=[
            {"name": "models/gemini-demo", "displayName": "Demo", "supportedGenerationMethods": ["generateContent"], "outputTokenLimit": 4096},
            {"name": "models/gemini-3.1-flash-image", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/embedder", "supportedGenerationMethods": ["embedContent"]},
        ])
        self.assertEqual(len(gemini["models"]), 2)
        self.assertTrue(gemini["models"][0]["chat"])
        self.assertEqual(gemini["models"][0]["max_output"], 4096)
        self.assertTrue(gemini["models"][1]["image"])
        anthro = self.add(key="another-fake-key", kind="anthropic", models=[
            {"id": "claude-demo", "max_tokens": 8192, "capabilities": {"image_input": {"supported": True}}}
        ])
        self.assertTrue(anthro["models"][0]["vision"])
        with self.assertRaises(personal_ai.PersonalError):
            personal_ai.configure_model(ALICE, anthro["id"], "claude-demo", True, True, True)

    def test_endpoints_reject_credentials_query_and_private_ip(self):
        for url in ["http://example.com", "https://user:secret@example.com", "https://example.com/v1?key=abc",
                    "https://example.com/#foo", "https://example.com\\@private/v1"]:
            with self.subTest(url=url), self.assertRaises(personal_ai.PersonalError):
                personal_ai._normalize_base(url, "openai")
        self.assertEqual(personal_ai._normalize_base("https://api.example.com", "openai"), "https://api.example.com/v1")
        self.assertEqual(personal_ai.detect_kind("https://generativelanguage.googleapis.com"), "gemini")
        with self.assertRaises(egress.EgressError):
            egress._connect("127.0.0.1", 443)
        provider = {"base": "https://127.0.0.1/v1", "key": FAKE_KEY, "kind": "openai"}
        with self.assertRaises(personal_ai.PersonalError) as result:
            personal_ai._request(provider, "GET", "/models")
        self.assertEqual(str(result.exception), "connection")

    def test_http_redirects_refused_and_credentials_not_in_errors(self):
        class FakeResponse:
            status = 302
            def read(self, _n):
                raise AssertionError("redirect body must not be read")
        connection = mock.Mock()
        connection.getresponse.return_value = FakeResponse()
        provider = {"base": "https://api.example.com/v1", "key": FAKE_KEY, "kind": "openai"}
        with mock.patch.object(personal_ai, "_PinnedHTTPS", return_value=connection):
            with self.assertRaises(personal_ai.PersonalError) as error:
                personal_ai._request(provider, "GET", "/models")
        self.assertEqual(str(error.exception), "redirect")
        self.assertNotIn(FAKE_KEY, str(error.exception))
        connection.close.assert_called_once()

    def test_personal_key_never_in_global_router_and_explicit_use_only(self):
        provider = self.add()
        model_key = provider["id"] + ":chat-one"
        self.assertIsNone(ai.model_by_key(model_key + "-missing"))
        self.assertEqual(ai.model_by_key(model_key).key, model_key)
        self.assertNotIn(provider["id"], [p.key for p in ai.providers()])
        self.assertNotIn(model_key, [m.key for m in ai.candidates("chat", provider["id"])])
        self.assertNotIn(provider["id"], [p.key for p in personal_ai.providers(BOB)])
        with mock.patch.object(personal_ai, "_request", return_value={"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
                                                                       "usage": {"total_tokens": 7}}) as request:
            result = ai.complete_exact([{"role": "user", "content": "hi"}], model_key)
        self.assertTrue(result.ok)
        self.assertEqual(request.call_count, 1)
        state.set_personal_ai_secure(False)
        self.assertIsNone(ai.model_by_key(model_key))
        state.act_as(BOB)
        state.set_personal_ai_secure(True)
        self.assertIsNone(ai.model_by_key(model_key))

    def test_real_tls_transport_with_synthetic_local_provider(self):
        # Exercise the HTTP/TLS wire format and both operations. The pinned
        # connector is redirected to a private test server only inside this test;
        # private-address rejection is tested separately above.
        server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "provider.example")])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(server_key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
                .not_valid_after(now + timedelta(hours=1)).add_extension(x509.SubjectAlternativeName([x509.DNSName("provider.example")]), False)
                .sign(server_key, hashes.SHA256()))
        root = Path(self.temp.name)
        cert_path, key_path = root / "test-cert.pem", root / "test-key.pem"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(server_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                     serialization.NoEncryption()))
        observed = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args): pass
            def answer(self, payload):
                raw = json.dumps(payload).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
            def do_GET(self):
                observed.append((self.command, self.path, self.headers.get("Authorization")))
                self.answer({"data": [{"id": "chat-one"}, {"id": "gpt-image-2"}]})
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                observed.append((self.command, self.path, self.headers.get("Authorization"), body.get("model")))
                if self.path.endswith("/chat/completions"):
                    self.answer({"choices": [{"message": {"content": "wire success"}, "finish_reason": "stop"}]})
                else:
                    self.answer({"data": [{"b64_json": base64.b64encode(b"synthetic-image-bytes").decode()}]})
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); server_ctx.load_cert_chain(cert_path, key_path)
        server.socket = server_ctx.wrap_socket(server.socket, server_side=True)
        client_ctx = ssl.create_default_context(cafile=str(cert_path))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            connector = lambda host, port, upstream=None: socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
            with mock.patch.object(egress, "_connect", side_effect=connector), \
                 mock.patch.object(config, "WEB_PROXY", ""), mock.patch.object(personal_ai.ssl, "create_default_context", return_value=client_ctx):
                result = personal_ai.add(ALICE, {"name": "Local test", "base": "https://provider.example/v1", "key": FAKE_KEY,
                                                 "kind": "openai", "daily": 3})
                provider = result["providers"][0]
                text, _tokens, _finish = personal_ai.complete(ALICE, provider["id"] + ":chat-one", [{"role": "user", "content": "hi"}], 40)
                image_bytes = personal_ai.image(ALICE, provider["id"] + ":gpt-image-2", "test image")
            self.assertEqual(text, "wire success")
            self.assertEqual(image_bytes, b"synthetic-image-bytes")
            self.assertEqual([item[1] for item in observed], ["/v1/models", "/v1/chat/completions", "/v1/images/generations"])
            self.assertTrue(all(item[2] == "Bearer " + FAKE_KEY for item in observed))
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)

    def test_gemini_and_anthropic_generation_uses_documented_shapes(self):
        gemini = self.add(kind="gemini", models=[{"name": "models/gemini-demo", "supportedGenerationMethods": ["generateContent"]}])
        anthro = self.add(key="another-fake-key", kind="anthropic", models=[{"id": "claude-demo", "max_tokens": 4096,
                           "capabilities": {"image_input": {"supported": True}}}])
        message = [{"role": "system", "content": "Be brief"}, {"role": "user", "content": [
            {"type": "text", "text": "what is this?"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGk="}}]}]
        gem_result = {"candidates": [{"content": {"parts": [{"text": "answer"}]}, "finishReason": "STOP"}],
                      "usageMetadata": {"totalTokenCount": 4}}
        with mock.patch.object(personal_ai, "_request", return_value=gem_result) as request:
            text, tokens, finish = personal_ai.complete(ALICE, gemini["id"] + ":gemini-demo", message, 5000)
        self.assertEqual((text, tokens, finish), ("answer", 4, "stop"))
        body = request.call_args.args[3]
        self.assertEqual(body["contents"][0]["parts"][1]["inline_data"]["mime_type"], "image/png")
        ant_result = {"content": [{"type": "text", "text": "response"}], "usage": {"input_tokens": 3, "output_tokens": 5},
                      "stop_reason": "end_turn"}
        with mock.patch.object(personal_ai, "_request", return_value=ant_result) as request:
            text, tokens, finish = personal_ai.complete(ALICE, anthro["id"] + ":claude-demo", message, 5000)
        self.assertEqual((text, tokens, finish), ("response", 8, "end_turn"))
        body = request.call_args.args[3]
        self.assertEqual(body["max_tokens"], 4096)
        self.assertEqual(body["messages"][0]["content"][1]["source"]["media_type"], "image/png")

    def test_legacy_app_without_build_is_invalid_but_not_deleted(self):
        entry = {"chat": 2, "addr": ALICE}
        state.data["apps"] = {"1": entry}
        handler = mock.Mock(return_value={"unsafe": True})
        with mock.patch.object(miniapp.access, "bound_allowed", return_value=True), \
             mock.patch.object(miniapp.admin, "is_allowed", return_value=True), \
             mock.patch.object(config, "service_access_allowed", return_value=True), \
             mock.patch.dict(miniapp.OPS, {"legacy_test": handler}), mock.patch.object(miniapp, "respond") as respond:
            miniapp.handle(1, entry, {"id": "legacy", "op": "legacy_test", "lang": "en"})
        handler.assert_not_called()
        self.assertIn("no longer valid", respond.call_args.args[3])
        self.assertIn("1", state.data["apps"])

    def test_daily_cap_counts_attempts_and_is_atomic(self):
        provider = self.add(daily=5)
        actual, _ = personal_ai.lookup(ALICE, provider["id"] + ":chat-one")
        def reserve(_):
            try:
                personal_ai._budget(actual)
                return True
            except personal_ai.PersonalError:
                return False
        with ThreadPoolExecutor(max_workers=12) as pool:
            result = list(pool.map(reserve, range(20)))
        self.assertEqual(sum(result), 5)
        self.assertEqual(personal_ai.attempts(provider["id"]), 5)
        self.assertEqual(json.loads((Path(self.temp.name) / "state.json").read_text())["personal_ai_budgets"][state.utc_day()][provider["id"]], 5)

    def test_personal_image_never_automatically_selected(self):
        provider = self.add()
        item = provider["id"] + ":gpt-image-2"
        personal = [s for s in images.sources() if s.key == item]
        self.assertEqual(len(personal), 1)
        self.assertTrue(personal[0].personal)
        self.assertNotIn(item, [s.key for s in images._ordered()])
        state.set_personal_ai_secure(False)
        self.assertNotIn(item, [s.key for s in images.sources()])

    def test_unicode_and_image_updates_stay_within_webxdc_size_limit(self):
        for content in ("😀" * 30000, "汉" * 40000, ('\\\\"' * 30000), "A" * 160000):
            text = json.dumps({"t": "res", "re": "synthetic", "data": content}, ensure_ascii=False, separators=(",", ":"))
            with mock.patch.object(chat, "send_update", return_value=True) as send:
                miniapp._deliver(7, "synthetic", text)
            chunks = [call.args[1] for call in send.call_args_list]
            self.assertGreater(len(chunks), 1)
            self.assertEqual("".join(item["s"] for item in chunks), text)
            self.assertEqual([item["i"] for item in chunks], list(range(len(chunks))))
            for item in chunks:
                wire = json.dumps({"payload": item}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                self.assertLess(len(wire), 128_000)

    def test_generated_miniapp_image_is_jpeg_at_most_1024_pixels(self):
        from PIL import Image
        source = io.BytesIO()
        Image.new("RGB", (2400, 1300), (70, 90, 130)).save(source, "PNG")
        generated = images.ImageResult(data=source.getvalue(), source="Synthetic")
        with mock.patch.object(images, "generate", return_value=generated), mock.patch.object(miniapp, "models_view", return_value={}):
            response = miniapp.op_image(7, {"prompt": "synthetic image"}, "en")
        self.assertEqual(response["mime"], "image/jpeg")
        with Image.open(io.BytesIO(base64.b64decode(response["b64"]))) as result:
            self.assertEqual(result.format, "JPEG")
            self.assertLessEqual(max(result.size), 1024)

    def test_paid_request_is_not_replayed_after_unknown_restart(self):
        with mock.patch.object(miniapp, "_admit", return_value="lost"), \
             mock.patch.object(miniapp, "respond") as respond, mock.patch.object(miniapp._pool, "submit") as submit:
            miniapp._route(8, {"chat": 2, "addr": ALICE}, {"id": "unique", "op": "image", "lang": "en",
                                                              "args": {"model": "personal-abcdef:image"}})
        submit.assert_not_called()
        self.assertIn("may have charged", respond.call_args.args[3])

    def test_profile_notice_preserves_custom_profile_and_can_be_removed(self):
        class RPC:
            def __init__(self): self.status = "My custom profile /app\nhttps://github.com/example/repo"
            def get_config(self, _account, _name): return self.status
            def set_config(self, _account, _name, value): self.status = value
        rpc = RPC()
        original = rpc.status
        with mock.patch.object(chat, "ready", return_value=True), mock.patch.object(chat, "rpc", return_value=rpc), \
             mock.patch.object(chat, "account", return_value=1):
            maintenance.set_notice(True, "Mensaje de mantenimiento", "Maintenance message")
            self.assertTrue(maintenance.sync_profile())
            self.assertIn(original, rpc.status)
            self.assertIn("Mensaje de mantenimiento", rpc.status)
            self.assertIn("Maintenance message", rpc.status)
            first = rpc.status
            self.assertTrue(maintenance.sync_profile())
            self.assertEqual(first, rpc.status)
            maintenance.set_notice(False, "Mensaje de mantenimiento", "Maintenance message")
            self.assertTrue(maintenance.sync_profile())
            self.assertNotIn("Mensaje de mantenimiento", rpc.status)
            self.assertIn(original, rpc.status)

    def test_maintenance_notifies_only_authorized_and_retries_queue_failure(self):
        state.data["apps"] = {"1": {"chat": 1, "addr": ALICE, "locale": "en"},
                              "2": {"chat": 2, "addr": BOB, "locale": "es"}}
        notice = maintenance.set_notice(True, "Aviso", "Notice")
        attempts = []
        def send(chat_id, kind, payload, notification):
            attempts.append((chat_id, notification))
            return len(attempts) > 1
        with mock.patch.object(miniapp.access, "bound_allowed", side_effect=lambda e: e["addr"] == ALICE), \
             mock.patch.object(miniapp.admin, "is_allowed", side_effect=lambda addr: addr == ALICE), \
             mock.patch.object(miniapp, "push", side_effect=send):
            miniapp._notify_maintenance(notice)  # local queue failed, retry
            miniapp._notify_maintenance(notice)
            miniapp._notify_maintenance(notice)  # successful queueing is not repeated
        self.assertEqual(attempts, [(1, "Notice"), (1, "Notice")])
        self.assertEqual(state.data["maintenance_notice"]["notified"]["1"], notice["revision"])

    def test_updates_preserve_old_apps_check_recipient_and_send_each_new_build(self):
        original = {"chat": 2, "addr": ALICE, "build": "old", "locale": "en", "sent": 1}
        state.data["apps"] = {"1": original.copy()}
        miniapp._upgraded.clear()
        with mock.patch.object(miniapp, "app_build", return_value="build-two"), \
             mock.patch.object(miniapp.access, "bound_allowed", return_value=True), \
             mock.patch.object(miniapp.admin, "is_allowed", return_value=True), \
             mock.patch.object(live_web, "_identity", return_value=(original, ALICE)), \
             mock.patch.object(miniapp, "replace_app", return_value=2) as send:
            miniapp.proactive_upgrades()
            miniapp.proactive_upgrades()
            self.assertEqual(send.call_count, 1)
            self.assertEqual(state.data["apps"]["1"], original)
        with mock.patch.object(miniapp, "app_build", return_value="build-three"), \
             mock.patch.object(miniapp.access, "bound_allowed", return_value=True), \
             mock.patch.object(miniapp.admin, "is_allowed", return_value=True), \
             mock.patch.object(live_web, "_identity", return_value=(original, ALICE)), \
             mock.patch.object(miniapp, "replace_app", return_value=3) as send:
            miniapp.proactive_upgrades()
            self.assertEqual(send.call_count, 1)  # a different build is not blocked by the 30-minute duplicate guard
        with mock.patch.object(miniapp, "app_build", return_value="build-four"), \
             mock.patch.object(miniapp.access, "bound_allowed", return_value=True), \
             mock.patch.object(miniapp.admin, "is_allowed", return_value=True), \
             mock.patch.object(live_web, "_identity", return_value=None), \
             mock.patch.object(miniapp, "replace_app") as send:
            miniapp.proactive_upgrades()
            send.assert_not_called()

    def test_youtube_pages_preserve_order_deduplicate_and_isolate_cache(self):
        tools._video_cache.clear()
        def fake_search(query, limit, locale):
            return [tools.Result(str(i), "https://www.youtube.com/watch?v=" + f"video{i:06d}", "") for i in range(limit)]
        with mock.patch.object(tools, "_youtube_search", side_effect=fake_search) as request:
            first = tools.youtube_page("birds", "en", 1, ALICE)
            second = tools.youtube_page("birds", "en", 2, ALICE)
            third = tools.youtube_page("birds", "en", 3, ALICE)
            fourth = tools.youtube_page("birds", "en", 4, ALICE)
            other = tools.youtube_page("birds", "en", 1, BOB)
        self.assertEqual(len(first["results"]), 15)
        self.assertEqual(len(second["results"]), 15)
        self.assertEqual(len(third["results"]), 15)
        self.assertEqual(len(fourth["results"]), 15)
        self.assertTrue(first["more"] and second["more"] and third["more"])
        urls = [x.url for page in (first, second, third, fourth) for x in page["results"]]
        self.assertEqual(len(urls), len(set(urls)))
        self.assertEqual(len(other["results"]), 15)
        self.assertEqual(request.call_count, 3)  # first batch, expansion, separate user
        with self.assertRaises(ValueError):
            tools.youtube_page("birds", "en", 0, ALICE)


if __name__ == "__main__":
    unittest.main()
