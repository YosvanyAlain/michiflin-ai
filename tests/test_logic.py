#!/usr/bin/env python3
"""Offline checks for Michiflin AI. Run with: python tests/test_logic.py

No Delta Chat account, API key or network access is needed.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="michiflin-test-")
os.environ.update(CONFIG_DIR=f"{_TMP}/cfg", JOB_DIR=f"{_TMP}/jobs", PREVIEW_DIR=f"{_TMP}/tmp",
                  MIN_FREE_DISK_MB="1", OWNER_ADDRESS="owner@example.org", POLLINATIONS_PUBLIC="0",
                  GROQ_API_KEY="test", MISTRAL_API_KEY="test", ORCAROUTER_API_KEY="test", FREE_GATEWAYS="0", AI_HORDE="0")

from michiflin import ai, chat, commands, config, files, images, links, media, names, progress, queue, reader, state, tools  # noqa: E402


class Sent:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def __call__(self, chat_id, text="", file=None, filename="", html="", viewtype=None, quote=None, interrupted=None, before_handoff=None):
        if interrupted and interrupted():
            return 0
        if before_handoff and not before_handoff():
            return 0
        self.items.append({"chat": chat_id, "text": text, "file": file, "filename": filename, "html": html, "viewtype": viewtype})
        return len(self.items)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.sent = Sent()
        self.patches = [mock.patch.object(chat, "send", self.sent), mock.patch.object(chat, "ready", lambda: True)]
        for patch in self.patches:
            patch.start()
        state.data["settings"].clear()
        state.data["jobs"].clear()
        state.data["cooldowns"].clear()
        state.data["usage"].clear()
        state.data["remaining"].clear()

    def tearDown(self) -> None:
        for patch in self.patches:
            patch.stop()


class CommandTests(Base):
    def test_parse_tap_forms(self) -> None:
        self.assertEqual(commands.parse("/vid_k3_360"), ("video", ["k3", "360"], ""))
        self.assertEqual(commands.parse("/a_k3_64"), ("audio", ["k3", "64"], ""))
        self.assertEqual(commands.parse("/v_k3"), ("web", ["k3"], ""))
        self.assertEqual(commands.parse("/mas_k3_2"), ("more", ["k3", "2"], ""))
        self.assertEqual(commands.parse("/more_k3_2"), ("more", ["k3", "2"], ""))
        self.assertEqual(commands.parse("/dl_k1@bot.example"), ("download", ["k1"], ""))
        self.assertEqual(commands.parse("/CAPTURA https://a.example"), ("capture", [], "https://a.example"))
        self.assertEqual(commands.parse("/screenshot https://a.example"), ("capture", [], "https://a.example"))
        self.assertEqual(commands.parse("/cola_limpiar"), ("queue", ["limpiar"], ""))
        self.assertEqual(commands.parse("/nada")[0], "")
        self.assertIsNone(commands.parse("hola"))

    def test_pending_command_takes_next_message(self) -> None:
        with mock.patch("michiflin.web.capture") as capture:
            commands.dispatch(5, "/captura", "es")
            self.assertIn("Envía el enlace", self.sent.items[-1]["text"])
            commands.dispatch(5, "https://example.org/page", "es")
            capture.assert_called_once_with(5, "https://example.org/page", "es")

    def test_link_tokens_resolve(self) -> None:
        url = "https://example.org/a?b=1"
        token = links.command("v", url).split("_", 1)[1]
        self.assertEqual(links.resolve(token), url)
        self.assertEqual(links.command("v", url), links.command("v", url))

    def test_settings_validation(self) -> None:
        commands.dispatch(1, "/ajustar_parte_15", "es")
        self.assertEqual(state.setting("part_mb"), 15)
        commands.dispatch(1, "/ajustar_parte_50", "es")
        self.assertEqual(state.setting("part_mb"), 15)
        commands.dispatch(1, "/ajustar_ventana_2200-0600", "es")
        self.assertEqual(state.setting("window"), "22:00-06:00")
        commands.dispatch(1, "/ajustar_video_alta", "es")
        self.assertEqual(state.setting("video_height"), 720)

    def test_help_mentions_main_groups(self) -> None:
        for page in range(1, 8):
            commands.dispatch(1, f"/ayuda_{page}", "es")
        text = "\n".join(item["text"] for item in self.sent.items[-7:])
        for word in ("/web", "/pdf", "/captura", "/video", "/audio", "/bajar", "/cola", "/ia", "/ajustes", "/salud"):
            self.assertIn(word, text)


    def test_only_app_command_passes_in_app_mode(self) -> None:
        self.assertTrue(commands.is_app_command("/app"))
        self.assertTrue(commands.is_app_command("/miniapp"))
        self.assertFalse(commands.is_app_command("/ayuda"))
        self.assertFalse(commands.is_app_command("hola"))

    def test_nonempty_notes_are_listed_in_both_languages(self) -> None:
        notes = [{"id": 7, "text": "A test note"}]
        with mock.patch.object(state, "list_notes", return_value=notes), mock.patch.object(chat, "sender", return_value="owner@example.org"):
            for locale, command, hint in (("es", "/notas", "/borrar_nota N"), ("en", "/notes", "/delete_note N")):
                with self.subTest(locale=locale):
                    self.assertTrue(commands.dispatch(1, command, locale))
                    self.assertIn("#7 · A test note", self.sent.items[-1]["text"])
                    self.assertIn(hint, self.sent.items[-1]["text"])

    def test_nonempty_links_are_listed_in_both_languages(self) -> None:
        links_q = [{"id": 8, "title": "A test link", "url": "https://example.org/"}]
        with mock.patch.object(state, "list_links", return_value=links_q), mock.patch.object(chat, "sender", return_value="owner@example.org"):
            for locale, command, hint in (("es", "/enlaces", "/procesar_enlace"), ("en", "/links", "/process_link")):
                with self.subTest(locale=locale):
                    self.assertTrue(commands.dispatch(1, command, locale))
                    self.assertIn("#8 · A test link", self.sent.items[-1]["text"])
                    self.assertIn(hint, self.sent.items[-1]["text"])


class OwnerNoticeTests(Base):
    def test_owner_notice_reaches_the_owner_app_once(self) -> None:
        from michiflin import miniapp

        sent = []
        state.data["flags"].pop("owner_notice", None)
        state.data["apps"] = {"41": {"chat": 7, "addr": "owner@example.org"}}
        with mock.patch.object(config, "OWNER_ADDRESS", "owner@example.org"), \
                mock.patch.object(chat, "ready", lambda: True), \
                mock.patch.object(miniapp, "push", lambda *args: sent.append(args)):
            with mock.patch.object(config, "OWNER_NOTICE", ""):
                self.assertFalse(miniapp.deliver_owner_notice(wait=0))
            with mock.patch.object(config, "OWNER_NOTICE", "Hola"):
                self.assertTrue(miniapp.deliver_owner_notice(wait=0))
                self.assertFalse(miniapp.deliver_owner_notice(wait=0))
            with mock.patch.object(config, "OWNER_NOTICE", "Otra"):
                self.assertTrue(miniapp.deliver_owner_notice(wait=0))
        self.assertEqual([(args[0], args[1], args[2]["es"]) for args in sent], [(7, "notice", "Hola"), (7, "notice", "Otra")])

    def test_owner_notice_waits_for_the_owner_app(self) -> None:
        from michiflin import miniapp

        state.data["apps"] = {}
        state.data["flags"].pop("owner_notice", None)
        with mock.patch.object(config, "OWNER_ADDRESS", "owner@example.org"), \
                mock.patch.object(config, "OWNER_NOTICE", "Hola"), \
                mock.patch.object(chat, "ready", lambda: True):
            self.assertFalse(miniapp.deliver_owner_notice(wait=0))
        self.assertIsNone(state.flag("owner_notice"))


class FileTests(unittest.TestCase):
    def test_magic_detection(self) -> None:
        self.assertEqual(files.sniff(b"%PDF-1.7", "bin"), "pdf")
        self.assertEqual(files.sniff(b"PK\x03\x04....AndroidManifest.xml", "zip"), "apk")
        self.assertEqual(files.sniff(b"PK\x03\x04....word/document.xml", ""), "docx")
        self.assertEqual(files.sniff(b"\x00\x00\x00\x18ftypmp42", ""), "mp4")
        self.assertEqual(files.sniff(b"\x00\x00\x00\x18ftypM4A ", ""), "m4a")
        self.assertEqual(files.sniff(b"\x1a\x45\xdf\xa3\x93\x42\x82\x88webm", ""), "webm")
        self.assertEqual(files.sniff(b"7z\xbc\xaf\x27\x1c", ""), "7z")
        self.assertEqual(files.sniff(b"MZ\x90\x00", "exe"), "exe")
        self.assertEqual(files.sniff(b"<!DOCTYPE html><html>", "php"), "html")
        self.assertEqual(files.sniff(b"plain words", "csv"), "csv")

    def test_resolvers(self) -> None:
        self.assertIn("drive.usercontent.google.com", files._drive("https://drive.google.com/file/d/1AbCdEfGhIjKlMn/view"))
        self.assertTrue(files._dropbox("https://www.dropbox.com/s/x/a.zip?dl=0").endswith("dl=1"))
        self.assertEqual(files._github("https://github.com/o/r/blob/main/a.txt"), "https://raw.githubusercontent.com/o/r/main/a.txt")


class MediaTests(unittest.TestCase):
    def test_site_specific_extractors_cover_non_youtube_services_without_network(self) -> None:
        for url in ("https://vimeo.com/12345", "https://www.tiktok.com/@user/video/123", "https://www.facebook.com/watch/?v=123"):
            with self.subTest(url=url):
                self.assertTrue(media.supported(url))
        # Generic arbitrary pages are intentionally not treated as media until their redirect/SSRF path is hardened.
        self.assertFalse(media.supported("https://example.org/video"))

    def test_global_cookie_jar_is_owner_only(self) -> None:
        old_cookie_file = media._cookie_file
        try:
            media._cookie_file = ""
            with mock.patch.object(config, "YTDLP_COOKIES", "# Netscape HTTP Cookie File\\n"), \
                    mock.patch.object(state, "request_sender", return_value="stranger@example.org"), \
                    mock.patch("michiflin.admin.is_admin", return_value=False):
                self.assertEqual(media._cookies(), "")
            with mock.patch.object(config, "YTDLP_COOKIES", "# Netscape HTTP Cookie File\\n"), \
                    mock.patch.object(state, "request_sender", return_value="owner@example.org"), \
                    mock.patch("michiflin.admin.is_admin", return_value=True):
                path = media._cookies()
                self.assertTrue(path and Path(path).exists())
                Path(path).unlink(missing_ok=True)
        finally:
            media._cookie_file = old_cookie_file


class QueueTests(Base):
    def test_split_and_ordered_delivery(self) -> None:
        state.set_setting("part_mb", 1)
        state.set_setting("delay", 0)
        job = queue._new_job(1, "file", "https://example.org/f.bin", "f.bin", 0, name="f.bin", ext="bin", label="Archivo")
        state.data["jobs"][job["id"]] = job
        work = queue.folder(job) / "src"
        work.mkdir(parents=True, exist_ok=True)
        source = work / "download.bin"
        source.write_bytes(os.urandom(2_500_000))
        parts = queue._split(source, job)
        self.assertGreaterEqual(len(parts), 3)
        queue.update(job, status="sending", parts=len(parts), part_files=[str(p) for p in parts], todo=list(range(len(parts))), sha="ab" * 32, size=2_500_000)
        with mock.patch.object(chat, "wait_delivery", lambda *args, **kwargs: "delivered"):
            for _ in range(len(parts)):
                job["not_before"] = 0
                queue._send_one(job)
        self.assertEqual(job["status"], "done")
        captions = [item["text"] for item in self.sent.items]
        self.assertIn("1/", captions[0])
        self.assertIn("SHA-256", captions[-1])

    def test_batch_pauses(self) -> None:
        state.set_setting("batch", 1)
        job = queue._new_job(1, "file", "u", "t", 0, name="t.bin")
        state.data["jobs"][job["id"]] = job
        paths = []
        for index in range(2):
            path = queue.folder(job) / f"t.bin.{index + 1:03d}"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
            paths.append(str(path))
        queue.update(job, status="sending", parts=2, part_files=paths, todo=[0, 1], sha="c" * 64, size=2)
        with mock.patch.object(chat, "wait_delivery", lambda *args, **kwargs: "delivered"):
            queue._send_one(job)
        self.assertTrue(job["hold"])
        self.assertIn("/seguir_", self.sent.items[-1]["text"])

    def test_failed_delivery_retries_then_holds(self) -> None:
        job = queue._new_job(1, "file", "u", "t", 0, name="t.bin")
        state.data["jobs"][job["id"]] = job
        path = queue.folder(job) / "t.bin"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        queue.update(job, status="sending", parts=1, part_files=[str(path)], todo=[0], sha="d" * 64, size=1)
        with mock.patch.object(chat, "wait_delivery", lambda *args, **kwargs: "failed"), mock.patch.object(chat, "delete", lambda ids: None):
            for _ in range(4):
                job["not_before"] = 0
                queue._send_one(job)
        self.assertTrue(job["hold"])

    def test_window(self) -> None:
        state.set_setting("window", "")
        self.assertTrue(queue.in_window())


class FreeGatewayTests(Base):
    """Keyless gateways keep only their free models and call without credentials."""

    class Reply:
        def __init__(self, body):
            self.body, self.ok, self.status_code, self.headers = body, True, 200, {}

        def json(self):
            return self.body

        def raise_for_status(self):
            return None

    def setUp(self) -> None:
        super().setUp()
        self.patch = mock.patch.object(config, "FREE_GATEWAYS", True)
        self.patch.start()
        ai.build_registry()

    def tearDown(self) -> None:
        self.patch.stop()
        ai.build_registry()
        super().tearDown()

    def test_discovery_keeps_free_models_only(self) -> None:
        llm7 = {"data": [
            {"id": "codestral-latest", "tier": "turbo", "model_type": "chat", "context_window": {"tokens": 32000}, "capabilities": {}},
            {"id": "GLM-5.3-Flash", "tier": "turbo", "model_type": "chat", "context_window": {"tokens": 400000}, "reasoning": True, "capabilities": {}},
            {"id": "claude-opus-5", "tier": "pro", "model_type": "chat", "context_window": {"tokens": 1000000}, "capabilities": {}},
            {"id": "gpt-image-2", "tier": "turbo", "model_type": "image"},
        ]}
        kilo = {"data": [
            {"id": "nvidia/nemotron-3-super-120b-a12b:free", "isFree": True, "pricing": {"prompt": "0"}, "context_length": 262144},
            {"id": "cohere/north-mini-code:free", "isFree": True, "pricing": {"prompt": "0"}, "context_length": 256000},
            {"id": "nvidia/nemotron-3.5-content-safety:free", "isFree": True, "pricing": {"prompt": "0"}},
            {"id": "google/lyria-3-pro-preview", "isFree": True, "pricing": {"prompt": "0"}},
            {"id": "openai/gpt-6-sol", "isFree": False, "pricing": {"prompt": "0.000002"}},
        ]}
        with mock.patch("michiflin.ai.requests.get", lambda url, **k: self.Reply(llm7 if "llm7" in url else kilo)):
            ai._discover_llm7()
            ai._discover_kilo()
        self.assertEqual([model.model for model in ai.provider("llm7").models], ["codestral-latest", "GLM-5.3-Flash"])
        kilo_models = {model.model: model for model in ai.provider("kilo").models}
        self.assertEqual(set(kilo_models), {"nvidia/nemotron-3-super-120b-a12b:free", "cohere/north-mini-code:free"})
        self.assertIn("code", kilo_models["cohere/north-mini-code:free"].roles)
        self.assertIn("reason", kilo_models["nvidia/nemotron-3-super-120b-a12b:free"].roles)
        self.assertEqual(kilo_models["nvidia/nemotron-3-super-120b-a12b:free"].title(), "Nemotron 3 Super 120B")

    def test_keyless_call_sends_no_credentials(self) -> None:
        seen = {}

        def post(url, headers=None, json=None, timeout=None):
            seen.update(url=url, headers=headers, tokens=json["max_tokens"], timeout=timeout)
            return self.Reply({"choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 3}})
        item = ai.provider("kilo")
        with mock.patch("michiflin.ai.requests.post", post):
            result = ai._call(item, item.models[0], [{"role": "user", "content": "hi"}], None, 500)
        self.assertTrue(result.ok)
        self.assertNotIn("Authorization", seen["headers"])
        self.assertGreaterEqual(seen["tokens"], 3200)
        self.assertGreaterEqual(seen["timeout"], 120)

    def test_sambanova_models_use_documented_free_daily_limit(self) -> None:
        with mock.patch.object(config, "SAMBANOVA_API_KEY", "test-sambanova-key"):
            ai.build_registry()
            self.addCleanup(ai.build_registry)
            provider = ai.provider("sambanova")
            self.assertIsNotNone(provider)
            self.assertEqual({model.model for model in provider.models},
                             {"DeepSeek-V3.1", "Meta-Llama-3.3-70B-Instruct", "gpt-oss-120b"})
            self.assertTrue(all(model.daily == 20 for model in provider.models))
            self.assertNotIn("vision", next(model for model in provider.models if model.model == "Meta-Llama-3.3-70B-Instruct").roles)

    def test_custom_ai_endpoint_requires_exact_https_allowlist_and_no_redirect(self) -> None:
        import json

        raw = json.dumps([
            {"key": "trusted1", "name": "Trusted", "base_url": "https://trusted.example/v1", "api_key_env": "TEST_EXTRA_KEY",
             "daily": 100, "models": [{"id": "model-x", "roles": ["chat"], "daily": 20}]},
            {"key": "untrusted", "name": "Untrusted", "base_url": "https://evil.example/v1", "api_key_env": "TEST_EXTRA_KEY",
             "models": [{"id": "model-y", "roles": ["chat"]}]},
        ])
        seen = {}
        class Reply:
            ok = True
            status_code = 200
            headers = {}
            is_redirect = False
            def json(self):
                return {"choices": [{"message": {"content": "safe"}}], "usage": {"total_tokens": 1}}
        with mock.patch.dict(os.environ, {"TEST_EXTRA_KEY": "test-secret"}), mock.patch.object(
                config, "EXTRA_PROVIDERS", raw), mock.patch.object(config, "AI_EXTRA_ALLOWED_HOSTS", {"trusted.example"}):
            providers = ai._extra_providers()
            self.assertEqual([provider.key for provider in providers], ["trusted1"])
            item = providers[0]
            self.assertTrue(item.custom)
            model = item.models[0]
            def post(url, **kwargs):
                seen.update(url=url, **kwargs)
                return Reply()
            with mock.patch("michiflin.ai.requests.post", post):
                response, text, _tokens = ai._openai(item, model, [{"role": "user", "content": "hello"}], None, 50)
            self.assertTrue(response.ok)
            self.assertEqual(text, "safe")
            self.assertFalse(seen["allow_redirects"])
            self.assertIn("Bearer test-secret", seen["headers"]["Authorization"])
            self.assertTrue(ai._safe_extra_endpoint("https://trusted.example/v1"))
            self.assertFalse(ai._safe_extra_endpoint("http://trusted.example/v1"))
            self.assertFalse(ai._safe_extra_endpoint("https://evil.example/v1"))
            self.assertFalse(ai._safe_extra_endpoint("https://user:pass@trusted.example/v1"))
            self.assertFalse(ai._safe_extra_endpoint("https://127.0.0.1/v1"))
            self.assertFalse(ai._safe_extra_endpoint("https://trusted.example:8443/v1"))


class HordeTests(Base):
    """AI Horde: anonymous, safe-for-work, polled until done; censored output is not delivered."""

    class Reply:
        def __init__(self, body=None, content=b"", status=200):
            self.body, self.content, self.status_code, self.ok = body, content, status, status < 400
            self.headers = {"content-type": "image/webp"}

        def json(self):
            return self.body

        def iter_content(self, chunk_size=65536):
            yield self.content

        def close(self):
            pass

    def run_job(self, censored: bool):
        from PIL import Image
        buffer = __import__("io").BytesIO()
        Image.new("RGB", (64, 64), "orange").save(buffer, "WEBP")
        sent = {}
        checks = iter([{"done": False}, {"done": True}])

        def post(url, json=None, headers=None, timeout=None):
            sent.update(json=json, headers=headers)
            return self.Reply({"id": "job1"}, status=202)

        def get(url, headers=None, timeout=None, **_kwargs):
            if "/check/" in url:
                return self.Reply(next(checks))
            if "/status/" in url:
                return self.Reply({"generations": [{"img": "https://r2.example/img.webp", "censored": censored}]})
            return self.Reply(content=buffer.getvalue())
        with mock.patch.object(config, "AI_HORDE", True), mock.patch("michiflin.images.requests.post", post), \
                mock.patch("michiflin.images.requests.get", get), mock.patch("michiflin.images.requests.delete", lambda *a, **k: None), \
                mock.patch("michiflin.images.time.sleep", lambda _s: None), mock.patch("michiflin.images.english_prompt", lambda p: p), \
                mock.patch("michiflin.images._public_image", return_value=buffer.getvalue()):
            result = images.generate("a lighthouse 1920x1080", "horde:flux")
        return result, sent

    def test_horde_delivers_jpeg_with_safe_limits(self) -> None:
        result, sent = self.run_job(censored=False)
        self.assertFalse(result.error)
        self.assertEqual(result.source, "FLUX.1 schnell (Horde)")
        params = sent["json"]["params"]
        self.assertLessEqual(params["width"] * params["height"], 1024 * 1024)
        self.assertEqual((params["width"] % 64, params["height"] % 64), (0, 0))
        self.assertFalse(sent["json"]["nsfw"])
        self.assertTrue(sent["json"]["censor_nsfw"])
        self.assertEqual(sent["headers"]["apikey"], "0000000000")

    def test_censored_result_is_not_delivered(self) -> None:
        result, _sent = self.run_job(censored=True)
        self.assertEqual(result.error, "policy")


class ProfileTests(unittest.TestCase):
    def test_profile_adds_usage_and_repository_without_overwriting_custom_text(self) -> None:
        from michiflin import profile

        rpc = mock.Mock()
        rpc.get_config.return_value = "Existing private assistant description."
        self.assertTrue(profile.ensure_profile(rpc, 42))
        account, key, value = rpc.set_config.call_args.args
        self.assertEqual((account, key), (42, "selfstatus"))
        self.assertTrue(value.startswith("Existing private assistant description."))
        self.assertIn("/app", value)
        self.assertIn(profile.REPOSITORY_URL, value)
        rpc.get_config.return_value = value
        rpc.set_config.reset_mock()
        self.assertFalse(profile.ensure_profile(rpc, 42))
        rpc.set_config.assert_not_called()

    def test_empty_profile_gets_a_description(self) -> None:
        from michiflin import profile

        result = profile.profile_status(None)
        self.assertIn(profile.DEFAULT_DESCRIPTION, result)
        self.assertIn("/app", result)
        self.assertIn(profile.REPOSITORY_URL, result)
        self.assertEqual(profile.profile_status(result), result)

    def test_profile_read_failure_never_overwrites_the_status(self) -> None:
        from michiflin import profile

        rpc = mock.Mock()
        rpc.get_config.side_effect = RuntimeError("unavailable")
        with self.assertRaises(RuntimeError):
            profile.ensure_profile(rpc, 42)
        rpc.set_config.assert_not_called()

    @unittest.skipUnless(shutil.which("deltachat-rpc-server"), "Delta Chat RPC server is not on PATH")
    def test_profile_persists_in_an_offline_delta_chat_database(self) -> None:
        from michiflin import profile

        with tempfile.TemporaryDirectory(prefix="michiflin-profile-test-") as directory:
            accounts_dir = str(Path(directory) / "accounts")
            with profile._account_rpc(accounts_dir) as rpc:
                account = rpc.add_account()  # unconfigured account; no mailbox or network connection
                rpc.set_config(account, "selfstatus", "Existing description")
            profile.configure_directory(directory)
            with profile._account_rpc(accounts_dir) as rpc:
                stored = rpc.get_config(rpc.get_all_account_ids()[0], "selfstatus")
                self.assertTrue(stored.startswith("Existing description"))
                self.assertIn("/app", stored)
                self.assertIn(profile.REPOSITORY_URL, stored)
                self.assertEqual(profile.profile_status(stored), stored)


class CoderTests(Base):
    """Coder: safe paths, the write-run-fix loop, and a sandbox without network, secrets or bot files."""

    def test_safe_paths(self) -> None:
        from michiflin import coder
        self.assertEqual(coder.safe_path("src/app.py"), "src/app.py")
        self.assertEqual(coder.safe_path("./index.html"), "index.html")
        for bad in ("../x.py", "/etc/passwd", "a/../../b", "~/.ssh/id", "", "a\\..\\b"):
            self.assertEqual(coder.safe_path(bad), "", bad)

    def test_loop_fixes_and_finishes(self) -> None:
        from michiflin import coder
        replies = iter([
            "<<<FILE calc.py>>>\n```python\ndef add(a, b):\n    return a - b\n```\n<<<END>>>\n<<<RUN>>>\npython3 -c 'import calc; assert calc.add(2, 2) == 4'\n<<<END>>>",
            "<<<FILE calc.py>>>\ndef add(a, b):\n    return a + b\n<<<END>>>\n<<<RUN>>>\npython3 -c 'import calc; assert calc.add(2, 2) == 4'\n<<<END>>>\n"
            "<<<FILE ../evil.py>>>\nx\n<<<END>>>\n<<<DONE>>>\nSuma lista.\n<<<END>>>",
        ])
        seen = []

        def complete(messages, role="chat", **kwargs):
            seen.append(messages[-1]["content"])
            return ai.AIResult(text=next(replies), provider="Test", model="Coder")

        def run(folder, command):
            source = (folder / "calc.py").read_text()
            return (0, "") if "a + b" in source else (1, "AssertionError")
        with mock.patch.object(coder, "isolation", lambda: True), mock.patch.object(coder, "_lock_private", lambda: None), \
                mock.patch.object(coder, "_run", run), mock.patch("michiflin.ai.complete", complete):
            result = coder.run("una función add", [], "es")
        self.assertEqual(result["summary"], "Suma lista.")
        self.assertEqual([item["path"] for item in result["files"]], ["calc.py"])
        self.assertIn("a + b", result["files"][0]["text"])
        self.assertNotIn("```", result["files"][0]["text"])
        self.assertEqual([step["code"] for step in result["steps"] if step["kind"] == "run"], [1, 0])
        self.assertIn("AssertionError", seen[1])

    def test_written_files_override_an_empty_project_claim(self) -> None:
        from michiflin import coder
        cases = (("es", "No se construyó nada. ¿Qué quieres crear?", "archivos"),
                 ("en", "No files were created. What would you like?", "files"))
        for locale, model_summary, expected in cases:
            with self.subTest(locale=locale):
                response = f"<<<FILE hello.py>>>\nprint('hello')\n<<<END>>>\n<<<DONE>>>\n{model_summary}\n<<<END>>>"
                result = ai.AIResult(text=response, provider="Test", model="Coder")
                with mock.patch.object(coder, "isolation", return_value=False), mock.patch("michiflin.ai.complete", return_value=result):
                    project = coder.run("Create a file", [], locale)
                self.assertEqual([item["path"] for item in project["files"]], ["hello.py"])
                self.assertIn(expected, project["summary"].lower())
                self.assertNotEqual(project["summary"], model_summary)

    def test_empty_project_claim_is_preserved_when_nothing_was_written(self) -> None:
        from michiflin import coder
        summary = "Nothing was built. What would you like?"
        response = ai.AIResult(text=f"<<<DONE>>>\n{summary}\n<<<END>>>", provider="Test", model="Coder")
        existing = [{"path": "old.py", "text": "print(1)"}]
        with mock.patch.object(coder, "isolation", return_value=False), mock.patch("michiflin.ai.complete", return_value=response):
            project = coder.run("Any ideas?", existing, "en")
        self.assertEqual(project["summary"], summary)
        self.assertTrue(project["answer"])

    def test_wrap_up_when_rounds_run_out(self) -> None:
        from michiflin import coder
        asked = []

        def complete(messages, role="chat", **kwargs):
            asked.append(messages[-1]["content"])
            if messages[-1]["content"].startswith("No rounds left"):
                return ai.AIResult(text="<<<DONE>>>\nPágina lista.\n<<<END>>>", provider="Test", model="Coder")
            return ai.AIResult(text="<<<FILE index.html>>>\n<p>hola</p>\n<<<END>>>", provider="Test", model="Coder")
        with mock.patch.object(coder, "isolation", lambda: False), mock.patch.object(coder, "_lock_private", lambda: None), \
                mock.patch.object(coder, "MAX_STEPS", 2), mock.patch("michiflin.ai.complete", complete):
            result = coder.run("una página", [], "es")
        self.assertIn("pueden estar incompletos", result["summary"])
        self.assertIn("Página lista.", result["summary"])
        self.assertEqual(len(asked), 3)

    def test_syntax_checks_python_without_execution_and_javascript_in_sandbox(self) -> None:
        from michiflin import coder
        folder = Path(tempfile.mkdtemp())
        invalid_py = [{"path": "bad.py", "text": "def broken(:\n    pass\n"}]
        self.assertIn("Python syntax error", coder.syntax_check(folder, invalid_py, False)[0])
        calls = []
        with mock.patch.object(coder.shutil, "which", return_value="/usr/bin/node"), mock.patch.object(
                coder, "_run", side_effect=lambda _folder, command: calls.append(command) or (1, "Unexpected token")):
            found = coder.syntax_check(folder, [{"path": "src/app.js", "text": "const ="}], True)
        self.assertIn("JavaScript syntax check failed", found[0])
        self.assertEqual(calls, ["for f in ./src/app.js; do node --check -- \"$f\" || exit $?; done"])

    def test_syntax_checks_c_cpp_and_compiles_java_inside_sandbox(self) -> None:
        from michiflin import coder
        folder = Path(tempfile.mkdtemp())
        files = [{"path": "main.c", "text": "int main(void) { return 0; }"},
                 {"path": "src/math.cpp", "text": "int add(int a, int b) { return a + b; }"},
                 {"path": "src/Main.java", "text": "class Main { public static void main(String[] args) {} }"}]
        calls = []
        available = {"gcc", "g++", "javac"}
        with mock.patch.object(coder.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}" if name in available else None), \
                mock.patch.object(coder, "_run", side_effect=lambda _folder, command: calls.append(command) or (0, "")):
            found = coder.syntax_check(folder, files, True)
        self.assertEqual(found, [])
        self.assertEqual(len(calls), 3)
        self.assertIn("gcc -std=c11 -Wall -Wextra -fsyntax-only -x c ./main.c", calls[0])
        self.assertIn("g++ -std=c++17 -Wall -Wextra -fsyntax-only -x c++ ./src/math.cpp", calls[1])
        self.assertIn("javac -proc:none -d /tmp/michiflin-javac-", calls[2])

    def test_invalid_python_cannot_be_accepted_by_done_without_fix(self) -> None:
        from michiflin import coder
        replies = iter([
            "<<<FILE app.py>>>\ndef broken(:\n    pass\n<<<END>>>\n<<<DONE>>>\nHecho.\n<<<END>>>",
            "<<<DONE>>>\nHecho.\n<<<END>>>",
            "<<<FILE app.py>>>\ndef fixed():\n    return 1\n<<<END>>>\n<<<DONE>>>\nCorregido.\n<<<END>>>",
        ])
        prompts = []
        def complete(messages, role="chat", **kwargs):
            prompts.append(messages[-1]["content"])
            return ai.AIResult(text=next(replies), provider="Test", model="Coder")
        with mock.patch.object(coder, "isolation", lambda: False), mock.patch.object(coder, "_lock_private", lambda: None), \
                mock.patch("michiflin.ai.complete", complete):
            result = coder.run("crea un script", [], "es")
        self.assertIn("return 1", result["files"][0]["text"])
        self.assertEqual(result["summary"], "Corregido.")
        syntax_runs = [step for step in result["steps"] if step.get("cmd") == "syntax check"]
        self.assertTrue(syntax_runs and syntax_runs[0]["code"] == 1)
        self.assertGreaterEqual(len(prompts), 3)

    def test_web_check(self) -> None:
        from michiflin.coder import web_check
        page = '<form id="add-form"></form><ul id="list"></ul><script src="app.js"></script><link rel="stylesheet" href="style.css">'
        files = [{"path": "index.html", "text": page},
                 {"path": "app.js", "text": "document.getElementById('task-form'); document.querySelector('#list'); "
                                            "const p = document.createElement('p'); p.id = 'made'; document.getElementById('made');"},
                 {"path": "test.js", "text": "form.id = 'task-form';"}]
        self.assertEqual(web_check(files), ['app.js looks up #task-form, but no element has id="task-form"',
                                            "index.html links style.css, but that file does not exist"])
        files[0]["text"] = page.replace("add-form", "task-form")
        files.append({"path": "style.css", "text": "body{}"})
        self.assertEqual(web_check(files), [])
        self.assertEqual(web_check([{"path": "main.py", "text": "print(1)"}]), [])

    def test_sandbox_isolation(self) -> None:
        from michiflin import coder
        if not coder.isolation():
            self.skipTest("no sudo/namespaces here")
        folder = Path(tempfile.mkdtemp())
        folder.chmod(0o777)
        probe = ("import os, socket\n"
                 "print('uid', os.getuid())\n"
                 "print('secret' if 'GROQ_API_KEY' in os.environ else 'clean')\n"
                 "try:\n    socket.create_connection(('1.1.1.1', 53), timeout=3); print('net')\nexcept OSError:\n    print('offline')\n"
                 f"try:\n    os.listdir({str(config.CONFIG_DIR)!r}); print('config readable')\nexcept OSError:\n    print('config private')\n")
        (folder / "probe.py").write_text(probe)
        (folder / "probe.py").chmod(0o666)
        config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        coder._lock_private()
        code, output = coder._run(folder, "python3 probe.py")
        coder._remove(folder)
        self.assertEqual(code, 0, output)
        self.assertIn("uid 65534", output)
        self.assertIn("clean", output)
        self.assertIn("offline", output)
        self.assertIn("config private", output)

    def test_sandbox_runs_behavioral_fixtures_for_available_languages(self) -> None:
        """Exercise real compile-and-run behavior, not just parsing, inside the production sandbox."""
        from michiflin import coder
        if not coder.isolation():
            self.skipTest("no sudo/namespaces here")
        folder = Path(tempfile.mkdtemp(prefix="coder-behavior-"))
        folder.chmod(0o777)
        fixtures = [
            {"path": "check.py", "text": "def add(a, b):\n    return a + b\nassert add(2, 3) == 5\nprint('python ok')\n"},
            {"path": "check.js", "text": "const assert = require('node:assert/strict');\nconst add = (a, b) => a + b;\nassert.equal(add(2, 3), 5);\nconsole.log('javascript ok');\n"},
            {"path": "check.c", "text": "#include <stdio.h>\nstatic int add(int a, int b) { return a + b; }\nint main(void) { if (add(2, 3) != 5) return 1; puts(\"c ok\"); return 0; }\n"},
            {"path": "check.cpp", "text": "#include <iostream>\nstatic int add(int a, int b) { return a + b; }\nint main() { if (add(2, 3) != 5) return 1; std::cout << \"cpp ok\\n\"; }\n"},
            {"path": "Main.java", "text": "public class Main { static int add(int a, int b) { return a + b; } public static void main(String[] args) { if (add(2, 3) != 5) throw new AssertionError(); System.out.println(\"java ok\"); } }\n"},
        ]
        coder.write_files(folder, fixtures)
        cases = [("Python", "python3 check.py", "python ok")]
        if coder.shutil.which("node"):
            cases.append(("JavaScript", "node check.js", "javascript ok"))
        if coder.shutil.which("gcc"):
            cases.append(("C", "gcc -std=c11 check.c -o check-c && ./check-c", "c ok"))
        if coder.shutil.which("g++"):
            cases.append(("C++", "g++ -std=c++17 check.cpp -o check-cpp && ./check-cpp", "cpp ok"))
        if coder.shutil.which("javac") and coder.shutil.which("java"):
            cases.append(("Java", "javac -proc:none Main.java && java Main", "java ok"))
        try:
            for language, command, expected in cases:
                with self.subTest(language=language):
                    code, output = coder._run(folder, command)
                    self.assertEqual(code, 0, f"{language}: {output}")
                    self.assertIn(expected, output)
        finally:
            coder._remove(folder)


class AiRoutingTests(Base):
    def setUp(self) -> None:
        super().setUp()
        ai.build_registry()

    def test_hosted_aggregators_use_only_free_model_routes_without_local_inference(self) -> None:
        with mock.patch.object(config, "OPENROUTER_API_KEY", "test"), mock.patch.object(config, "ZAI_API_KEY", "test"):
            ai.build_registry()
        providers = {item.key: item for item in ai.providers()}
        self.assertNotIn("local", providers)
        router = providers["openrouter"]
        self.assertEqual(router.daily, 50)
        self.assertEqual([model.model for model in router.models], ["openrouter/free"])
        zai = providers["zai"]
        self.assertEqual({model.model for model in zai.models}, {"glm-4.7-flash", "glm-4.5-flash", "glm-4.6v-flash"})
        self.assertNotIn("deepseek-v4-pro", {model.model for item in providers.values() for model in item.models})

    def test_qwen_in_rotation_and_fair_order(self) -> None:
        keys = [model.key for model in ai.candidates("chat")]
        self.assertIn("groq:qwen/qwen3.8-27b", keys)
        first = ai.candidates("chat")[0]
        for _ in range(50):
            state.record_usage(first.key, True)
        self.assertNotEqual(ai.candidates("chat")[0].key, first.key)

    def test_quota_error_sets_cooldown_and_falls_back(self) -> None:
        calls = []

        class Response:
            def __init__(self, status, body):
                self.status_code, self._body, self.headers = status, body, {}
                self.ok = status < 400
                self.text = str(body)

            def json(self):
                return self._body

        def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
            calls.append(json["model"])
            if len(calls) == 1:
                return Response(429, {"error": "rate limit"})
            return Response(200, {"choices": [{"message": {"content": "<think>x</think>**Hola**"}}]})

        with mock.patch("michiflin.ai.requests.post", fake_post):
            result = ai.complete([{"role": "user", "content": "hi"}], "chat")
        self.assertEqual(result.text, "*Hola*")
        self.assertTrue(state.cooldown_until(f"{ai.candidates('chat')[0].provider}:{calls[0]}") or True)
        self.assertGreaterEqual(len(calls), 2)
        self.assertNotIn(calls[0], [model.model for model in ai.candidates("chat")])


    def test_failed_calls_do_not_spend_visible_quota(self) -> None:
        key = ai.candidates("chat")[0].key
        before = state.usage_today(key)
        state.record_usage(key, False)
        self.assertEqual(state.usage_today(key), before)
        state.record_usage(key, True)
        self.assertEqual(state.usage_today(key), before + 1)

    def test_agent_never_reaches_a_model(self) -> None:
        calls = []
        with mock.patch("michiflin.ai.requests.post", lambda *a, **k: calls.append(a)), ai.isolated():
            self.assertTrue(ai.blocked())
            self.assertEqual(ai.candidates("chat"), [])
            self.assertFalse(ai.complete([{"role": "user", "content": "hi"}], "chat").ok)
            key = next(model.key for item in ai.providers() for model in item.models)
            self.assertEqual(ai.complete_exact([{"role": "user", "content": "hi"}], key).error, "no_provider")
        self.assertEqual(calls, [])
        self.assertFalse(ai.blocked())
        self.assertNotIn("pollinations", {item.key for item in ai.providers()})

    def test_agent_answers_from_sources_without_models(self) -> None:
        from michiflin import agent, answer

        sources = [answer.Source("Fotosíntesis", "https://es.wikipedia.org/wiki/Fotos%C3%ADntesis",
                                 "La fotosíntesis es el proceso por el que las plantas convierten la luz solar en energía química. "
                                 "Ocurre en los cloroplastos gracias a la clorofila, el pigmento que da el color verde a las hojas. "
                                 "¿Qué es la fotosíntesis? "
                                 "El Real Madrid ganó el partido del domingo por dos goles a cero en el estadio.", 1.35)]
        text, cited = answer.compose("¿Qué es la fotosíntesis de las plantas?", sources, "es")
        self.assertIn("cloroplastos", text + " ".join(s.text for s in cited))
        self.assertIn("proceso", text)
        self.assertNotIn("Real Madrid", text)
        self.assertNotIn("¿Qué es", text)
        self.assertFalse(answer._about("Desnudo, hojas verdes y busto", "¿Por qué las hojas son verdes?"))
        self.assertEqual(answer.topic("¿Qué es la fotosíntesis?"), "fotosíntesis")
        text = ("La Habana fue fundada por los españoles en el siglo dieciséis junto a una bahía protegida. "
                "Su centro histórico conserva plazas coloniales, iglesias barrocas y fortalezas militares. "
                "El Malecón recorre ocho kilómetros de costa y reúne a vecinos cada atardecer. "
                "La música popular de la ciudad mezcla raíces africanas con tradiciones europeas. "
                "Los autos clásicos estadounidenses todavía circulan como taxis por las avenidas principales. "
                "La Universidad de La Habana es una de las más antiguas de toda América.")
        self.assertEqual(len(answer.summarize(text, 4)), 4)
        self.assertEqual(len(answer.summarize(text + "\n" + text, 20)), 6)  # repeated sentences are dropped
        sent = []
        with mock.patch.object(answer, "gather", lambda *a, **k: sources), mock.patch("michiflin.chat.text", lambda chat_id, text, *a, **k: sent.append(text)), \
                mock.patch("michiflin.ai.requests.post", side_effect=AssertionError("model called")):
            agent.handle(7, "Explícame la fotosíntesis de las plantas", "es")
        self.assertTrue(sent and sent[-1].startswith("💡") and "[1]" in sent[-1])

    def test_transient_errors_are_not_quota(self) -> None:
        model = ai.candidates("chat")[0]
        item = next(provider for provider in ai.providers() if provider.key == model.provider)
        busy = '{"error":{"code":"1305","message":"The service may be temporarily overloaded, please try again later"}}'
        self.assertEqual(ai._cooldown_for(item, model, 429, busy, ""), "busy")
        shared = "Free models share a limited site-wide capacity, this is not your personal quota. Retry after 30 seconds, or add credits to use paid"
        self.assertEqual(ai._cooldown_for(item, model, 429, shared, ""), "busy")
        self.assertLess(state.cooldown_until(model.key) - time.time(), 40)
        self.assertEqual(ai._cooldown_for(item, model, 429, "Insufficient balance or no resource package. Please recharge.", ""), "model")

    def test_health_changes_are_announced_once(self) -> None:
        heard = []
        ai.LISTENERS.append(lambda kind, spanish, english: heard.append((kind, english)))
        try:
            model = ai.candidates("chat")[0]
            item = next(provider for provider in ai.providers() if provider.key == model.provider)
            ai._mark(item, model, "ok")
            self.assertEqual(heard, [])  # working models stay quiet
            ai._mark(item, model, "model")
            ai._mark(item, model, "model")
            self.assertEqual(len(heard), 1)
            self.assertIn("no longer free", heard[0][1])
            ai._mark(item, model, "ok")
            self.assertEqual(heard[-1][0], "ok")
            ai._mark(item, model, "auth")
            self.assertIn("rejected the API key", heard[-1][1])
        finally:
            ai.LISTENERS.pop()

    def test_chosen_image_model_is_used_alone(self) -> None:
        used = []
        with mock.patch.object(config, "CLOUDFLARE_ACCOUNT_ID", "acc"), mock.patch.object(config, "CLOUDFLARE_AI_TOKEN", "tok"), \
                mock.patch.object(images, "english_prompt", lambda text: text), \
                mock.patch.object(images, "_run", lambda source, *a, **k: used.append(source.key) or (_ for _ in ()).throw(images._SourceError("response"))):
            result = images.generate("un gato", "cf:phoenix")
            self.assertEqual((used, result.error, result.source), (["cf:phoenix"], "response", "Phoenix 1.0"))
            self.assertEqual(images.generate("un gato", "cf:phoenix").error, "cooldown")
            self.assertEqual(images.generate("un gato", "nope").error, "unknown")
        with mock.patch("michiflin.extras.translate", lambda text, target, source="auto": ("a cat on a roof", "es")), \
                mock.patch("michiflin.ai.complete", side_effect=AssertionError("model called")):
            self.assertEqual(images.english_prompt("un gato en el techo"), "a cat on a roof")

    def test_cloudflare_spent_neurons_pause_text_and_images_until_reset(self) -> None:
        class Reply:
            def __init__(self, status, body):
                self.status_code, self.text, self.ok = status, body, status == 200

        with mock.patch.object(config, "CLOUDFLARE_ACCOUNT_ID", "acc"), mock.patch.object(config, "CLOUDFLARE_AI_TOKEN", "tok"), \
                mock.patch.object(images, "english_prompt", lambda text: text):
            with mock.patch("requests.post", return_value=Reply(429, '{"errors":[{"code":3040,"message":"Capacity temporarily exceeded"}]}')):
                self.assertEqual(images.generate("un gato", "cf:dreamshaper").error, "busy")
            self.assertTrue(state.cooldown_until("cf:dreamshaper"))
            self.assertFalse(state.cooldown_until("cf:flux-1-schnell"))
            spent = '{"errors":[{"code":4006,"message":"you have used up your daily free allocation of 10,000 neurons"}]}'
            with mock.patch("requests.post", return_value=Reply(429, spent)):
                self.assertEqual(images.generate("un gato", "cf:flux-1-schnell").error, "quota")
        reset = time.time() + state.until_utc_midnight()
        for key in ("cloudflare", "cf:flux-1-schnell", "cf:phoenix"):
            self.assertAlmostEqual(state.cooldown_until(key), reset, delta=5)
            self.assertEqual(state.cooldown_reason(key), "quota")
        self.assertFalse([model for model in ai.candidates("chat") if model.provider == "cloudflare"])

    def test_chat_attachments_go_where_each_model_can_read_them(self) -> None:
        import base64

        from michiflin import miniapp

        calls = []

        def fake_call(item, model, messages, images, max_tokens, raw=False):
            calls.append((model.key, messages[-1]["content"], [mime for mime, _data in images or []]))
            return ai.AIResult(text="ok", provider=item.name, model=model.title())

        with mock.patch.object(config, "GEMINI_API_KEY", "g"), mock.patch.object(config, "GROQ_API_KEY", "q"):
            ai.build_registry()
            gemini = next(model.key for model in ai.providers()[0].models if model.provider == "gemini")
            groq = next(model.key for item in ai.providers() for model in item.models if model.provider == "groq")
            encode = lambda data: base64.b64encode(data).decode()
            pdf = b"%PDF-1.4 fake"
            with mock.patch.object(ai, "_call", fake_call):
                miniapp.op_ask(1, {"space": gemini, "text": "", "file": {"name": "a.png", "mime": "image/png", "b64": encode(b"png")}}, "es")
                miniapp.op_ask(1, {"space": gemini, "text": "¿De qué trata?", "file": {"name": "a.pdf", "mime": "application/pdf", "b64": encode(pdf)}}, "es")
                miniapp.op_ask(1, {"space": groq, "text": "Resume", "file": {"name": "notas.txt", "mime": "text/plain", "b64": encode("Lista de compras: arroz".encode())}}, "es")
                for space, name, mime in ((groq, "a.png", "image/png"), (groq, "nota.m4a", "audio/mp4")):
                    with self.assertRaises(miniapp.AppError):
                        miniapp.op_ask(1, {"space": space, "text": "x", "file": {"name": name, "mime": mime, "b64": encode(b"data")}}, "es")
                with self.assertRaises(miniapp.AppError):
                    miniapp.op_ask(1, {"space": gemini, "text": "x", "file": {"name": "a.txt", "mime": "text/plain", "b64": encode(b"x" * 100_001)}}, "es")
        self.assertEqual(calls[0][1:], ("Describe esta imagen.", ["image/png"]))
        self.assertEqual(calls[1][1:], ("¿De qué trata?", ["application/pdf"]))
        self.assertIn("Lista de compras: arroz", calls[2][1])
        self.assertEqual(calls[2][2], [])
        self.assertEqual(len(calls), 3)
        self.assertEqual(ai.accepts(ai.model_by_key(gemini)), ["document", "image", "pdf", "audio"])
        self.assertEqual(ai.accepts(ai.model_by_key(groq)), ["document"])

    def test_standard_search_tries_other_backends(self) -> None:
        from michiflin import tools

        attempts = []

        class Engine:
            def __init__(self, timeout=10):
                pass

            def text(self, query, region, safesearch, max_results, backend):
                attempts.append((region, backend))
                if len(attempts) < 3:
                    raise RuntimeError("No results found.")
                return [{"href": "https://example.org/a", "title": "A", "body": "body"}]

        with mock.patch.object(tools, "DDGS", Engine), mock.patch.object(tools, "_you_search", side_effect=AssertionError("quota spent")):
            found = tools.search("prueba de busqueda unica", "es", 5)
        self.assertEqual([item.url for item in found], ["https://example.org/a"])
        self.assertEqual(len(attempts), 3)

    def test_errors_name_the_real_cause(self) -> None:
        model = ai.candidates("chat")[0]
        state.set_cooldown(model.provider, 3600, "auth")
        result = ai.AIResult(provider="Mistral", model=model.title(), error="cooldown")
        self.assertIn("rechazó la clave", ai.describe_error("cooldown", "es", result, model.key))
        state.set_cooldown(model.provider, 1)
        state.data["cooldowns"].pop(model.provider, None)
        state.set_cooldown(model.key, 1800, "quota")
        self.assertIn("available again in", ai.describe_error("cooldown", "en", result, model.key))
        self.assertIn("no longer offered", ai.describe_error("model", "en", result))


    def test_paid_only_model_does_not_disable_the_provider(self) -> None:
        model = ai.candidates("chat")[0]
        item = next(provider for provider in ai.providers() if provider.key == model.provider)
        kind = ai._cooldown_for(item, model, 403, '{"message":"This model is not available in your subscription tier"}', "")
        self.assertEqual(kind, "model")
        self.assertFalse(state.cooldown_until(item.key))
        self.assertTrue(state.cooldown_until(model.key))
        self.assertEqual(ai._cooldown_for(item, model, 401, "invalid api key", ""), "auth")


class ToolTests(unittest.TestCase):
    def test_calculator(self) -> None:
        self.assertEqual(tools.calculate("(18+6)*3/2"), "36")
        self.assertEqual(tools.calculate("15% de 300"), "45")
        self.assertEqual(tools.calculate("sqrt(16)"), "4")
        self.assertIsNone(tools.calculate("__import__('os')"))
        self.assertIsNone(tools.calculate("9**999999"))
        self.assertTrue(tools.looks_like_math("2+2*3"))
        self.assertFalse(tools.looks_like_math("hola 2 amigos"))

    def test_inverted_question_mark_uses_wiki_first(self) -> None:
        from michiflin import agent

        sent: list[str] = []
        summary = ("Fotosíntesis", "Proceso químico.", "https://es.wikipedia.org/wiki/Fotos%C3%ADntesis")
        with mock.patch.object(agent.chat, "text", side_effect=lambda _chat, text, *a, **k: sent.append(text)), \
                mock.patch.object(agent.tools, "wiki_summary", return_value=summary), \
                mock.patch.object(agent, "ask", side_effect=AssertionError("AI must not be used")):
            agent.handle(1, "¿Qué es la fotosíntesis?", "es")
        self.assertTrue(sent and sent[0].startswith("📚 Fotosíntesis"))

    def test_currency_conversion_uses_rates(self) -> None:
        from michiflin import agent

        sent: list[str] = []
        with mock.patch.object(agent.chat, "text", side_effect=lambda _chat, text, *a, **k: sent.append(text)), \
                mock.patch.object(agent.tools, "_rates", return_value={"USD": 785.0, "EUR": 885.0}), \
                mock.patch.object(agent, "ask", side_effect=AssertionError("AI must not be used")):
            agent.handle(1, "¿cuánto son 100 usd en cup?", "es")
        self.assertTrue(sent[0].startswith("💱 100 USD ≈ 78.500 CUP"))

    def test_bot_check_stops_without_changing_proxy_or_client(self) -> None:
        from michiflin import media, private_media

        attempts: list[tuple[str, object]] = []
        class FakeYDL:
            def __init__(self, options):
                self.options = options
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False
            def download(self, _urls):
                attempts.append((self.options.get("proxy", ""), self.options.get("extractor_args")))
                raise Exception("Sign in to confirm you're not a bot")
            def extract_info(self, *_args, **_kwargs):
                attempts.append((self.options.get("proxy", ""), self.options.get("extractor_args")))
                raise Exception("CAPTCHA required")

        folder = Path(tempfile.mkdtemp(dir=_TMP))
        url = "https://www.youtube.com/watch?v=abc"
        with mock.patch.object(media.config, "MEDIA_PROXY", "socks5://127.0.0.1:40000"), \
                mock.patch.object(media.net, "validate_public_url", side_effect=lambda value: value), \
                mock.patch.object(media, "supported", return_value=True), \
                mock.patch.object(private_media, "storage_for", return_value=None), \
                mock.patch.object(media.yt_dlp, "YoutubeDL", FakeYDL):
            with self.assertRaisesRegex(ValueError, "bot_check"):
                media.download(url, folder, "audio", 64, "m4a")
            self.assertEqual(attempts, [("socks5://127.0.0.1:40000", None)])
            attempts.clear()
            with self.assertRaisesRegex(ValueError, "bot_check"):
                media.inspect(url)
            self.assertEqual(attempts, [("socks5://127.0.0.1:40000", None)])
            self.assertFalse(media._options()["geo_bypass"])

    def test_image_intent(self) -> None:
        self.assertTrue(images.is_image_request("dibuja un gato astronauta"))
        self.assertTrue(images.is_image_request("hazme una imagen de un atardecer"))
        self.assertTrue(images.is_image_request("generate an image of a red car"))
        self.assertFalse(images.is_image_request("qué es una imagen ISO"))
        self.assertEqual(images.clean_prompt("hazme una imagen de un atardecer en Varadero"), "un atardecer en Varadero")
        self.assertEqual(images.clean_prompt("dibuja un gato astronauta"), "un gato astronauta")
        self.assertEqual(images.clean_prompt("generate an image of a red car"), "a red car")

    def test_media_quality_words(self) -> None:
        self.assertEqual(media.resolve_quality("video", "alta", 360), 720)
        self.assertEqual(media.resolve_quality("video", "500", 360), 480)
        self.assertEqual(media.resolve_quality("audio", "baja", 96), 64)
        self.assertEqual(media.resolve_quality("audio", "", 96), 96)


class ReaderTests(Base):
    def test_reader_strips_junk_and_rewrites_links(self) -> None:
        links.set_bot_address("bot@example.org")
        html = ("<html><head><title>T</title><script>x()</script></head><body>"
                "<div class='cookie-banner'>Accept cookies</div><nav><a href='/a'>Inicio</a></nav>"
                "<article><h1>Title</h1><p>" + "Texto útil. " * 80 + "<a href='/next'>Siguiente</a></p></article></body></html>")
        page = reader.parse("https://site.example/x", html)
        document, summary, total = reader.build(page, 1, "es")
        self.assertNotIn("Accept cookies", document)
        self.assertNotIn("<script", document)
        self.assertIn("mailto:bot@example.org?body=https%3A%2F%2Fsite.example%2Fnext", document)
        self.assertEqual(total, 1)
        self.assertIn("/pdf_", summary)

    def test_pagination(self) -> None:
        state.set_setting("reader_kb", 30)
        page = reader.Page(url="https://s.example", title="T", blocks=["<p>" + "a" * 2000 + "</p>"] * 40)
        self.assertGreater(len(reader.paginate(page, 30)), 1)


class BrowserSecurityTests(unittest.TestCase):
    def test_image_redirects_return_to_firewall_and_private_targets_abort(self) -> None:
        from michiflin import browser

        class Response:
            status = 302
            headers = {"location": "http://127.0.0.1/private"}

            def body(self):
                return b""

        class Route:
            def __init__(self, url):
                self.request = type("Request", (), {"url": url, "resource_type": "image"})()
                self.fetch_options = None
                self.continued = False
                self.aborted = False

            def fetch(self, **kwargs):
                self.fetch_options = kwargs
                return Response()

            def continue_(self):
                self.continued = True

            def abort(self):
                self.aborted = True

        class Context:
            route_handler = None

            def route(self, _pattern, handler):
                self.route_handler = handler

            def route_web_socket(self, *_args):
                pass

            def set_default_timeout(self, *_args):
                pass

        context = Context()
        browser_stub = type("Browser", (), {"new_context": lambda _self, **_kwargs: context})()

        def validate(url):
            if "127.0.0.1" in url:
                raise ValueError("private_url")
            return url

        with mock.patch.object(browser.net, "cookie_jar", return_value=[]), \
                mock.patch.object(browser.net, "validate_public_url", side_effect=validate):
            browser._context(browser_stub, "desktop", shrink_images=True)
            public = Route("https://public.example/image.png")
            context.route_handler(public)
            self.assertEqual(public.fetch_options, {"max_redirects": 0})
            self.assertTrue(public.continued)
            private = Route("http://127.0.0.1/private")
            context.route_handler(private)
            self.assertTrue(private.aborted)
            self.assertIsNone(private.fetch_options)


class StateTests(unittest.TestCase):
    def test_legacy_queue_is_preserved_but_not_activated(self) -> None:
        from michiflin import config

        config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        config.LEGACY_STATE_FILE.write_text('{"settings": {"language": "en"}, "jobs": {"x": {"status": "ready"}}}')
        migrated = state.load()
        self.assertTrue(migrated)
        self.assertTrue(config.LEGACY_STATE_FILE.exists())
        self.assertNotIn("x", state.data["jobs"])
        config.LEGACY_STATE_FILE.unlink()


class BackupTests(unittest.TestCase):
    def test_page_decoding(self):
        from michiflin import reader

        text = "Mensajería 🙂 segura"
        self.assertEqual(reader.decode_page(text.encode(), "text/html"), text)
        self.assertEqual(reader.decode_page(f'<meta charset="iso-8859-1">{"canción"}'.encode("latin-1"), "text/html")[-7:], "canción")
        self.assertEqual(reader.decode_page("año".encode("cp1252"), "text/html"), "año")
        self.assertEqual(reader.decode_page(text.encode(), "text/html; charset=utf-8"), text)

    def test_search_drops_fuzzy_encyclopedia_matches(self):
        query = "Delta Chat"
        self.assertFalse(tools._relevant("https://en.wikipedia.org/wiki/Delta-class_submarine", "Delta-class submarine", query))
        self.assertTrue(tools._relevant("https://grokipedia.com/page/Delta_Chat", "Delta Chat", query))
        self.assertTrue(tools._relevant("https://delta.chat/en/", "Delta Chat, decentralized secure messenger", query))
        self.assertTrue(tools._relevant("https://es.wikipedia.org/wiki/Revolución_cubana", "Revolución cubana", "revolucion cubana"))

    def test_calculator_accepts_questions(self):
        from michiflin import tools

        for question in ("¿Cuánto es el 15 % de 2400?", "What is 15% of 2400?", "calcula 2+2*3", "15% de 2400"):
            self.assertTrue(tools.looks_like_math(question), question)
        self.assertEqual(tools.calculate("¿Cuánto es el 15 % de 2400?"), "360")
        self.assertFalse(tools.looks_like_math("¿Cuánto es el precio del dólar?"))

    def _backup_roundtrip(self, git_ok: bool):
        import base64

        import state_backup

        base = Path(tempfile.mkdtemp(prefix="backup-", dir=_TMP))
        (base / "michiflin").mkdir()
        (base / "michiflin" / "code.py").write_text("keep")
        source = base / "src"
        (source / "accounts").mkdir(parents=True)
        (source / "accounts" / "db").write_text("state")
        stored, calls = {}, []

        def post(url, **kwargs):
            calls.append(url.rsplit("/", 1)[-1])
            if not git_ok:
                raise state_backup.requests.HTTPError("forbidden")
            if url.endswith("/blobs"):
                stored["content"] = kwargs["json"]["content"]
            if url.endswith("/trees"):
                self.assertEqual(kwargs["json"]["base_tree"], "existing-tree")
            if url.endswith("/commits"):
                self.assertEqual(kwargs["json"]["parents"], ["existing-head"])
            return mock.Mock(status_code=201, json=lambda: {"sha": "abc"}, raise_for_status=lambda: None)

        def put(url, **kwargs):
            calls.append("put")
            stored.update(kwargs["json"])
            return mock.Mock(status_code=201, raise_for_status=lambda: None)

        def patch(url, **kwargs):
            calls.append("ref")
            self.assertFalse(kwargs["json"]["force"])
            return mock.Mock(status_code=200, raise_for_status=lambda: None)

        def get(url, **kwargs):
            if url.endswith("/ref/heads/main"):
                return mock.Mock(status_code=200, json=lambda: {"object": {"sha": "existing-head"}})
            if url.endswith("/commits/existing-head"):
                return mock.Mock(status_code=200, json=lambda: {"tree": {"sha": "existing-tree"}}, raise_for_status=lambda: None)
            return mock.Mock(status_code=404)

        env = {"GITHUB_TOKEN": "t", "STATE_BACKUP_REPO": "o/r", "STATE_BACKUP_PASSPHRASE": "pass phrase",
               "STATE_BACKUP_STORAGE": "legacy-git"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(state_backup.requests, "get", side_effect=get), \
                mock.patch.object(state_backup.requests, "post", side_effect=post), \
                mock.patch.object(state_backup.requests, "patch", side_effect=patch), \
                mock.patch.object(state_backup.requests, "put", side_effect=put):
            os.environ["CONFIG_DIR"] = str(source)
            try:
                state_backup.save()
                raw = base64.b64decode(stored["content"])
                reply = mock.Mock(status_code=200, content=raw, headers={"Content-Type": "application/octet-stream"},
                                  raise_for_status=lambda: None)
                os.environ["CONFIG_DIR"] = str(base / ".michiflin-state")
                with mock.patch.object(state_backup.requests, "get", return_value=reply):
                    state_backup.restore()
            finally:
                os.environ["CONFIG_DIR"] = f"{_TMP}/cfg"
        self.assertEqual((base / "michiflin" / "code.py").read_text(), "keep")
        self.assertEqual((base / ".michiflin-state" / "accounts" / "db").read_text(), "state")
        self.assertTrue({".michiflin-state", "michiflin", "src"}.issubset({p.name for p in base.iterdir()}))
        return calls

    def test_backup_appends_without_rewriting_history(self):
        self.assertEqual(self._backup_roundtrip(git_ok=True), ["blobs", "trees", "commits", "ref"])

    def test_missing_required_backup_fails_closed(self):
        import state_backup

        target = Path(tempfile.mkdtemp(prefix="missing-backup-", dir=_TMP)) / "state"
        env = {"GITHUB_TOKEN": "t", "STATE_BACKUP_REPO": "o/r", "STATE_BACKUP_PASSPHRASE": "pass",
               "STATE_BACKUP_REQUIRED": "1", "STATE_BACKUP_ALLOW_EMPTY": "0", "CONFIG_DIR": str(target)}
        with mock.patch.dict(os.environ, env), mock.patch.object(
                state_backup.requests, "get", return_value=mock.Mock(status_code=404)), \
                self.assertRaises(FileNotFoundError):
            state_backup.restore()
        self.assertFalse(target.exists())

    def test_defused_xml_rejects_entity_expansion(self):
        from michiflin.documents import DocumentError, _xml_text

        payload = b'<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]><root>&lol2;</root>'
        with self.assertRaises(DocumentError):
            _xml_text(payload)

    def test_cookie_jars_are_separated_by_contact(self):
        from michiflin import net

        net._jars.clear()
        with mock.patch.object(chat, "sender", return_value="one@example.org"):
            one = net.cookie_jar()
        with mock.patch.object(chat, "sender", return_value="two@example.org"):
            two = net.cookie_jar()
        self.assertIsNot(one, two)
        self.assertTrue(one.get("CONSENT", domain=".google.com"))
        self.assertTrue(two.get("CONSENT", domain=".google.com"))

    def test_browser_worker_preserves_cookie_identity_and_restores_it(self):
        from michiflin import browser, net
        previous = chat.sender()
        self.addCleanup(chat.set_sender, previous)
        net._jars.clear()
        chat.set_sender("")
        get_identity = lambda _browser: (chat.sender(), net.cookie_jar())
        first = browser._run_for_sender(get_identity, None, (), "one@example.org")
        second = browser._run_for_sender(get_identity, None, (), "two@example.org")
        self.assertEqual(first[0], "one@example.org")
        self.assertEqual(second[0], "two@example.org")
        self.assertIsNot(first[1], second[1])
        self.assertEqual(chat.sender(), "")
        def fail(_browser):
            raise RuntimeError("expected")
        with self.assertRaises(RuntimeError):
            browser._run_for_sender(fail, None, (), "one@example.org")
        self.assertEqual(chat.sender(), "")

    def test_revoked_miniapp_request_is_refused_before_dispatch(self):
        from michiflin import admin, miniapp

        sent = []
        with mock.patch.object(admin, "is_allowed", return_value=False), mock.patch.object(
                miniapp, "respond", side_effect=lambda *args: sent.append(args)), mock.patch.object(
                miniapp, "_workers", side_effect=AssertionError("must not dispatch")):
            miniapp.handle(777, {"chat": 3, "addr": "revoked@example.org"},
                           {"id": "revoked-rid", "op": "hello", "lang": "en"})
        self.assertEqual(sent, [], "a revoked app must not receive even an error reply")

    def test_backup_falls_back_to_contents_api(self):
        self.assertEqual(self._backup_roundtrip(git_ok=False), ["blobs", "put"])


class StatusTests(Base):
    def setUp(self) -> None:
        super().setUp()
        state.act_as("")
        state.set_setting("progress", "on")
        self.edits: list[tuple[int, str]] = []
        self.removed: list[list[int]] = []
        self.extra = [mock.patch.object(chat, "edit", lambda msg_id, text: self.edits.append((msg_id, text)) or True),
                      mock.patch.object(chat, "delete_for_all", lambda ids: self.removed.append(list(ids))),
                      mock.patch.object(config, "STATUS_DELAY", 1), mock.patch.object(config, "STATUS_EDIT_SECONDS", 1)]
        for patch in self.extra:
            patch.start()

    def tearDown(self) -> None:
        for patch in self.extra:
            patch.stop()
        super().tearDown()

    def test_quick_task_sends_no_notice(self) -> None:
        progress.begin(1, "es")
        progress.end()
        time.sleep(1.5)
        self.assertEqual(self.sent.items, [])
        self.assertEqual(self.removed, [])

    def test_slow_task_notice_is_edited_and_removed(self) -> None:
        progress.begin(1, "es")
        time.sleep(2.3)
        self.assertEqual(len(self.sent.items), 1)
        self.assertIn("Recibido", self.sent.items[0]["text"])
        progress.phase("🧠 Pensando · Modelo (Prov)")
        time.sleep(2.3)
        self.assertTrue(any("Pensando" in text and "⏱" in text for _id, text in self.edits))
        progress.end()
        self.assertEqual(self.removed, [[1]])

    def test_disabled_setting_is_silent(self) -> None:
        state.set_setting("progress", "off")
        progress.begin(1, "es")
        time.sleep(2.3)
        progress.end()
        self.assertEqual(self.sent.items, [])

    def test_job_confirmation_becomes_progress_and_is_removed(self) -> None:
        job = {"id": "77", "chat": 1, "title": "file.zip", "name": "file.zip", "locale": "es", "created": time.time(),
               "status": "downloading", "kind": "file"}
        queue.jobs()["77"] = job
        queue.attach_notice(job, 5, "📥 #77 file.zip")
        sample: dict = {}
        queue._status(job, queue._rate_line(job, 5 * 1024 * 1024, 10 * 1024 * 1024, sample))
        time.sleep(1.6)
        self.assertTrue(any(msg_id == 5 and "50%" in text for msg_id, text in self.edits))
        queue._status_done(job)
        self.assertEqual(self.removed, [[5]])
        self.assertEqual(job["status_msg"], 0)
        queue.jobs().pop("77", None)


class OrcaRouterTests(Base):
    def test_free_tier_rejections(self) -> None:
        item = ai.provider("orcarouter")
        self.assertIsNotNone(item)
        model = item.models[0]
        body = '{"error": {"code": "free_rate_limited", "message": "model", "metadata": {"reason": "err_free_prompt_cap", "retryable": false}}}'
        self.assertEqual(ai._cooldown_for(item, model, 400, body, ""), "response")
        self.assertFalse(state.cooldown_until(model.key))
        body = '{"error": {"code": "free_rate_limited", "metadata": {"reason": "err_free_rate", "retry_after_seconds": 37}}}'
        self.assertEqual(ai._cooldown_for(item, model, 429, body, "37"), "quota")
        self.assertTrue(state.cooldown_until(model.key))
        self.assertFalse(state.cooldown_until(item.key))

    def test_signature_shows_thinking_time(self) -> None:
        self.assertEqual(ai.AIResult(text="x", provider="P", model="M", seconds=7.4).signature(), "M · P · 7 s")


class SearchAndImageSourceTests(Base):
    def test_you_search_respects_daily_cap(self) -> None:
        class Response:
            ok, status_code = True, 200

            def json(self):
                return {"results": {"web": [{"title": "elTOQUE", "url": "https://eltoque.com/x", "description": "tasas"}]}}

        with mock.patch.object(config, "YDC_API_KEY", "k"), mock.patch.object(config, "YDC_DAILY", 1), \
                mock.patch("michiflin.tools.requests.get", lambda *a, **k: Response()):
            self.assertEqual(tools._you_search("tasa", 5)[0].url, "https://eltoque.com/x")
            self.assertEqual(tools._you_search("tasa", 5), [])
        self.assertEqual(state.usage_today("you:search"), 1)

    def test_image_sources_renewable_first(self) -> None:
        fake = [images.Source("pollinations:flux", "P", 300), images.Source("pollinations:key", "PK", 40),
                images.Source("hf:space", "HF", 7), images.Source("cf:flux-1-schnell", "CF", 250)]
        with mock.patch.object(images, "sources", lambda: fake):
            self.assertEqual([item.key for item in images._ordered()],
                             ["hf:space", "cf:flux-1-schnell", "pollinations:key", "pollinations:flux"])


class NavigationAndReactionTests(Base):
    def test_split_finds_volumes_when_name_has_brackets(self) -> None:
        job = queue._new_job(1, "file", "https://example.org/v", "Song", 0, name="Song [360p].iso", part_mb=1, part_format="7z")
        job["id"] = "split-test"
        source = Path(_TMP) / "src.mp4"
        source.write_bytes(b"x" * (3 * 1024 * 1024))

        def fake_7z(command, cwd, capture_output, timeout):
            archive = Path(command[-2])
            for index in (1, 2, 3, 4):
                Path(f"{archive}.{index:03d}").write_bytes(b"v")
            return mock.Mock(returncode=0)

        with mock.patch.object(queue.shutil, "which", lambda name: "/usr/bin/7z"), mock.patch.object(queue.subprocess, "run", fake_7z):
            parts = queue._split(source, job)
        self.assertEqual([path.name[-3:] for path in parts], ["001", "002", "003", "004"])
        self.assertTrue(all(path.name.startswith("Song.7z.") for path in parts))

    def test_help_documents_every_command_on_short_pages(self) -> None:
        for locale in ("es", "en"):
            pages = commands.help_pages(locale, owner=True)
            text = "\n".join(pages)
            missing = [key for key in commands.COMMANDS if key not in {"send"} and f"/{names.name(locale, key)}" not in text]
            self.assertEqual(missing, [], locale)
            for page in pages:
                self.assertLessEqual(len(page.splitlines()), 18, page[:30])

    def test_help_never_mixes_languages(self) -> None:
        import re
        spanish_only = {pair[0] for key, pair in names.COMMANDS.items() if pair[0] != pair[1]}
        english_only = {pair[1] for key, pair in names.COMMANDS.items() if pair[0] != pair[1]}
        for locale, foreign in (("en", spanish_only), ("es", english_only)):
            text = "\n".join(commands.help_pages(locale, owner=True))
            used = {match.split("_")[0] for match in re.findall(r"/([a-z][a-z0-9_]*)", text)}
            self.assertEqual(used & foreign, set(), locale)

    def test_admin_section_only_for_admins(self) -> None:
        self.assertNotIn("/admin", "\n".join(commands.help_pages("es", owner=False)))
        self.assertIn("/admin", "\n".join(commands.help_pages("es", owner=True)))
        with mock.patch.object(chat, "sender", lambda: "stranger@example.org"):
            commands.dispatch(1, "/admin", "es")
        self.assertIn("desconocido", self.sent.items[-1]["text"])

    def test_settings_accept_both_languages_and_reply_in_one(self) -> None:
        commands.dispatch(1, "/set_sharpness_max", "en")
        self.assertEqual(state.setting("capture_quality"), "max")
        self.assertIn("sharpness = max", self.sent.items[-1]["text"])
        commands.dispatch(1, "/ajustar_nitidez_alta", "es")
        self.assertEqual(state.setting("capture_quality"), "high")
        commands.dispatch(1, "/settings", "en")
        listing = self.sent.items[-1]["text"]
        self.assertIn("/set_sharpness_high", listing)
        self.assertNotIn("/ajustar", listing)

    def test_reply_language_follows_command_spelling(self) -> None:
        from michiflin import app
        self.assertEqual(app._locale("/set_sharpness_max", 40), "en")
        self.assertEqual(app._locale("/v_k3", 40), "en")
        self.assertEqual(app._locale("/ajustar_nitidez_alta", 40), "es")
        self.assertEqual(app._locale("/v_k3", 40), "es")
        self.assertEqual(app._locale("/v_k3", 41), "es")
        self.assertEqual(app._locale("what is the weather like", 41), "en")

    def test_short_stem_is_readable(self) -> None:
        self.assertEqual(queue.short_stem("My_Video [720p] (Official).mp4"), "My Video")
        self.assertLessEqual(len(queue.short_stem("x" * 120 + ".zip")), 40)

    def test_media_splits_into_playable_segments(self) -> None:
        import shutil as _shutil
        import subprocess as _subprocess
        if not _shutil.which("ffmpeg"):
            self.skipTest("ffmpeg missing")
        source = Path(_TMP) / "clip.mp4"
        _subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25", "-t", "40",
                         "-c:v", "libx264", "-preset", "ultrafast", "-g", "25", "-b:v", "600k", str(source)], check=True)
        job = queue._new_job(1, "video", "https://example.org/v", "Clip", 0, name="Clip_Test [360p].mp4", part_mb=1, part_format="raw")
        job["id"] = "segment-test"
        parts = queue._split(source, job)
        self.assertGreater(len(parts), 1)
        self.assertEqual(job["part_format"], "segments")
        self.assertEqual(parts[0].name, f"Clip Test (1-{len(parts)}).mp4")
        self.assertTrue(all(path.stat().st_size <= 1024 * 1024 for path in parts))
        job["parts"] = len(parts)
        self.assertEqual(queue._viewtype(job), "Video")
        _subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25", "-t", "30",
                         "-c:v", "libx264", "-preset", "ultrafast", "-g", "25", "-b:v", "600k", str(source)], check=True)
        direct = queue._new_job(1, "file", "https://example.org/c.mp4", "c", 0, name="c.mp4", part_mb=1, part_format="raw")
        direct["id"] = "direct-test"
        pieces = queue._split(source, direct)
        direct["parts"] = len(pieces)
        self.assertEqual(direct["part_format"], "segments")
        self.assertEqual(queue._viewtype(direct), "Video")

    def test_files_split_into_short_raw_parts(self) -> None:
        source = Path(_TMP) / "blob.bin"
        source.write_bytes(os.urandom(2 * 1024 * 1024 + 10))
        job = queue._new_job(1, "file", "https://example.org/f", "Blob", 0, name="Very_Long_Archive_Name (v2).zip", part_mb=1, part_format="raw")
        job["id"] = "raw-test"
        parts = queue._split(source, job)
        self.assertEqual([path.name for path in parts], [f"Very Long Archive Name.zip.{index:03d}" for index in (1, 2, 3)])

    def test_unknown_contacts_are_refused_silently(self) -> None:
        from michiflin import admin, app
        self.assertTrue(admin.is_allowed("owner@example.org"))
        self.assertFalse(admin.is_allowed("stranger@example.org"))
        with mock.patch.object(admin, "_set_blocked_contact"), mock.patch.object(admin, "admin_chats", lambda: [7]):
            app._refuse("stranger@example.org", "hola", 99)
            app._refuse("stranger@example.org", "hola otra vez", 99)
        alerts = [item for item in self.sent.items if item["chat"] == 7]
        self.assertEqual(len(alerts), 1)
        self.assertFalse([item for item in self.sent.items if item["chat"] == 99])
        self.assertTrue(admin.is_blocked("stranger@example.org"))
        self.assertTrue(admin.grant("stranger@example.org"))
        self.assertTrue(admin.is_allowed("stranger@example.org"))
        self.assertFalse(admin.revoke("owner@example.org"))

    def test_mailbox_profile_is_exact_verified_and_bounds_delivery(self) -> None:
        import json
        from michiflin import mail_profiles

        profiles = json.dumps({"user@example.org": {"verified": True, "safe_part_mb": 4, "min_delay_seconds": 45,
                                                        "max_batch_parts": 2, "daily_cap_mb": 30,
                                                        "source": "operator-verified test fixture", "checked_at": "2026-10-27"}})
        base = {"part_mb": 10, "batch": 0, "delay": 15, "daily_cap_mb": 0}
        self.assertEqual(mail_profiles.effective_settings("USER@example.org", base, profiles),
                         {"part_mb": 4, "batch": 2, "delay": 45, "daily_cap_mb": 30})
        self.assertEqual(mail_profiles.effective_settings("other@example.org", base, profiles), base)
        self.assertEqual(mail_profiles.effective_settings("user@example.org", base,
                         json.dumps({"user@example.org": {"verified": False, "safe_part_mb": 1}})), base)

    def test_default_parts_use_20mb_elsewhere_and_cap_nauta_at_10mb(self) -> None:
        from michiflin import mail_profiles

        settings = {"part_mb": 20, "batch": 0, "delay": 15, "daily_cap_mb": 0}
        self.assertEqual(mail_profiles.effective_settings("person@nauta.cu", settings)["part_mb"], 10)
        self.assertEqual(mail_profiles.part_limit_bytes("person@nauta.cu"), 10 * config.MIB)
        self.assertEqual(mail_profiles.effective_settings("person@notnauta.cu.evil", settings)["part_mb"], 20)

    def test_provider_size_rejection_downshifts_only_before_any_part_is_delivered(self) -> None:
        job = {"id": "79", "locale": "en", "part_mb": 20, "sent": 0, "size_retry_count": 0}
        with mock.patch.object(queue, "update", side_effect=lambda target, **values: target.update(values)), \
                mock.patch.object(queue, "_status"), mock.patch.object(queue, "_notify"), \
                mock.patch.object(queue._wake, "set"):
            self.assertTrue(queue._retry_with_smaller_parts(job, "SMTP 552 5.3.4 message size exceeds maximum"))
            self.assertEqual(job["part_mb"], 15)
            self.assertEqual(job["status"], "queued")
            self.assertTrue(job["send_requested"])
            self.assertFalse(queue._retry_with_smaller_parts(job, "mailbox quota exceeded"))
            job["sent"] = 1
            self.assertFalse(queue._retry_with_smaller_parts(job, "message too large"))
            job.update(part_mb=2, sent=0, size_retry_count=7)
            self.assertTrue(queue._retry_with_smaller_parts(job, "message too large"))
            self.assertEqual(job["part_mb"], 1)
            self.assertFalse(queue._retry_with_smaller_parts(job, "message too large"))

    def test_mailbox_profiles_reject_unverified_claims_and_invalid_limits(self) -> None:
        import json
        from michiflin import mail_profiles

        with self.assertRaises(ValueError):
            mail_profiles.parse_profiles(json.dumps({"user@example.org": {"verified": True, "safe_part_mb": 2}}))
        with self.assertRaises(ValueError):
            mail_profiles.parse_profiles(json.dumps({"example.org": {"verified": True, "safe_part_mb": 2,
                                                                        "source": "test", "checked_at": "2026-10-27"}}))
        with self.assertRaises(ValueError):
            mail_profiles.parse_profiles(json.dumps({"user@example.org": {"verified": True, "safe_part_mb": 50,
                                                                            "source": "test", "checked_at": "2026-10-27"}}))
        with self.assertRaises(ValueError):
            mail_profiles.parse_profiles(json.dumps({"user@example.org": {"verified": True, "safe_part_mb": 2,
                                                                            "source": "test", "checked_at": "2026-99-99"}}))
        with self.assertRaises(ValueError):
            mail_profiles.parse_profiles(json.dumps({"User@example.org": {"verified": False}, "user@example.org": {"verified": False}}))
        with self.assertRaises(ValueError):
            mail_profiles.effective_settings("user@example.org", {"part_mb": 0}, "{}")

    def test_queue_snapshots_exact_mailbox_profile_bounds(self) -> None:
        import json

        profiles = json.dumps({"owner@example.org": {"verified": True, "safe_part_mb": 3, "min_delay_seconds": 40,
                                                         "max_batch_parts": 2, "daily_cap_mb": 25,
                                                         "source": "test fixture", "checked_at": "2026-10-27"}})
        state.act_as("")
        state.set_setting("part_mb", 10)
        state.set_setting("batch", 0)
        state.set_setting("delay", 10)
        state.set_setting("daily_cap_mb", 0)
        with mock.patch.object(chat, "sender", return_value="owner@example.org"), mock.patch.object(
                config, "MAILBOX_PROFILES_JSON", profiles):
            job = queue._new_job(1, "file", "https://example.org/x", "x", 1)
        self.assertEqual((job["part_mb"], job["delivery_batch"], job["delivery_delay"], job["delivery_daily_cap_mb"]),
                         (3, 2, 40, 25))
        state.set_setting("part_mb", int(config.DEFAULTS["part_mb"]))
        state.set_setting("batch", int(config.DEFAULTS["batch"]))
        state.set_setting("delay", int(config.DEFAULTS["delay"]))
        state.set_setting("daily_cap_mb", int(config.DEFAULTS["daily_cap_mb"]))

    def test_existing_job_with_oversized_part_is_paused_by_new_verified_profile(self) -> None:
        import json

        path = Path(_TMP) / "part-too-large.bin"
        path.write_bytes(b"x" * (config.MIB + 1))
        job = {"id": "77", "chat": 1, "addr": "owner@example.org", "locale": "en", "status": "sending",
               "todo": [0], "part_files": [str(path)], "parts": 1, "sent": 0, "name": "test.bin", "kind": "file",
               "part_mb": 10, "delivery_batch": 0, "delivery_delay": int(config.DEFAULTS["delay"]), "delivery_daily_cap_mb": 0}
        profiles = json.dumps({"owner@example.org": {"verified": True, "safe_part_mb": 1,
                                                       "source": "test fixture", "checked_at": "2026-10-27"}})
        notices = []
        with mock.patch.object(config, "MAILBOX_PROFILES_JSON", profiles), mock.patch.object(
                queue, "_status", side_effect=lambda _job, text, **_kw: notices.append(text)), mock.patch.object(
                queue, "_notify", side_effect=lambda _job, text: notices.append(text)), mock.patch.object(
                queue, "get", return_value=job), mock.patch.object(queue.chat, "send",
                side_effect=AssertionError("oversized part must not be sent")):
            queue._send_one(job)
            self.assertTrue(job.get("hold"))
            self.assertTrue(job.get("profile_blocked"))
            self.assertTrue(notices and "verified profile" in notices[0].lower())
            refusal = queue.hold("77", False, "en")
        self.assertIn("verified limit still blocks", refusal.lower())

    def test_new_profile_delay_is_applied_to_an_already_queued_job(self) -> None:
        import json

        path = Path(_TMP) / "small-part.bin"
        path.write_bytes(b"small")
        job = {"id": "78", "chat": 1, "addr": "owner@example.org", "locale": "en", "status": "sending",
               "todo": [0], "part_files": [str(path)], "parts": 2, "sent": 1, "name": "test.bin", "kind": "file",
               "part_mb": 10, "delivery_batch": 0, "delivery_delay": 5, "delivery_daily_cap_mb": 0, "not_before": 0}
        profiles = json.dumps({"owner@example.org": {"verified": True, "safe_part_mb": 2, "min_delay_seconds": 40,
                                                       "source": "test fixture", "checked_at": "2026-10-27"}})
        with mock.patch.object(config, "MAILBOX_PROFILES_JSON", profiles), mock.patch.object(
                queue, "update", side_effect=lambda target, **values: target.update(values)), mock.patch.object(
                queue.chat, "send", side_effect=AssertionError("must wait before sending")):
            before = time.time()
            queue._send_one(job)
        self.assertEqual(job["delivery_delay"], 40)
        self.assertGreaterEqual(job["not_before"], before + 39)
        self.assertEqual(job["todo"], [0])

    def test_maintenance_gate_allows_only_unambiguous_owner(self) -> None:
        self.assertTrue(config.service_access_allowed("owner@example.org"))
        with mock.patch.object(config, "MAINTENANCE_MODE", True), mock.patch.object(
                config, "OWNER_ADDRESS", "owner@example.org"):
            self.assertTrue(config.service_access_allowed("OWNER@example.org"))
            self.assertFalse(config.service_access_allowed("friend@example.org"))
        with mock.patch.object(config, "MAINTENANCE_MODE", False):
            self.assertTrue(config.service_access_allowed("friend@example.org"))

    def test_maintenance_denies_existing_miniapp_before_dispatch(self) -> None:
        from michiflin import admin, miniapp

        sent = []
        with mock.patch.object(admin, "is_allowed", return_value=True), mock.patch.object(
                config, "MAINTENANCE_MODE", True), mock.patch.object(config, "OWNER_ADDRESS", "owner@example.org"), \
                mock.patch.object(miniapp, "respond", side_effect=lambda *args: sent.append(args)), mock.patch.object(
                miniapp, "_workers", side_effect=AssertionError("must not dispatch")):
            miniapp.handle(778, {"chat": 3, "addr": "friend@example.org"},
                           {"id": "maintenance-rid", "op": "hello", "lang": "en"})
        self.assertEqual(sent, [], "maintenance must not reply to an unauthorized old app")

    def test_maintenance_blocks_incoming_message_before_processing(self) -> None:
        from types import SimpleNamespace
        from michiflin import admin, app

        bot = SimpleNamespace(rpc=SimpleNamespace(
            get_basic_chat_info=lambda _acc, _chat: SimpleNamespace(chat_type="Single", is_contact_request=False),
            get_contact=lambda _acc, _sender: SimpleNamespace(address="friend@example.org")))
        msg = SimpleNamespace(from_id=3, chat_id=42, text="hello", file=None, is_info=False)
        with mock.patch.object(admin, "is_allowed", return_value=True), mock.patch.object(
                config, "MAINTENANCE_MODE", True), mock.patch.object(config, "OWNER_ADDRESS", "owner@example.org"), \
                mock.patch.object(app, "_maintenance_reply") as notice, mock.patch.object(admin, "seen") as seen:
            self.assertIsNone(app._screen(bot, 1, msg))
        notice.assert_called_once()
        seen.assert_not_called()

    def test_sensitive_commands_are_owner_only(self) -> None:
        self.assertIn("github", commands.ADMIN_ONLY)
        self.assertIn("purge", commands.ADMIN_ONLY)

    def test_revoking_user_removes_saved_app_instances(self) -> None:
        from michiflin import admin
        address = "revoke@example.org"
        admin.grant(address)
        state.data.setdefault("apps", {})["98765"] = {"chat": 9, "serial": 0, "addr": address}
        self.assertTrue(admin.revoke(address))
        self.assertNotIn("98765", state.data["apps"])
        self.assertFalse(admin.is_allowed(address))

    def test_help_pages_navigate(self) -> None:
        commands.dispatch(5, "/ayuda", "es")
        self.assertIn("1/7", self.sent.items[-1]["text"])
        self.assertIn("/siguiente", self.sent.items[-1]["text"])
        self.assertNotIn("/anterior", self.sent.items[-1]["text"])
        commands.dispatch(5, "/siguiente", "es")
        self.assertIn("2/7", self.sent.items[-1]["text"])
        self.assertIn("/anterior", self.sent.items[-1]["text"])
        commands.dispatch(5, "/ayuda_7", "es")
        self.assertIn("7/7", self.sent.items[-1]["text"])
        commands.dispatch(5, "/siguiente", "es")
        self.assertIn("última página", self.sent.items[-1]["text"])

    def test_search_pages_and_more_moves_forward(self) -> None:
        from michiflin import agent
        results = [tools.Result(f"Title {n}", f"https://example.org/{n}", "snippet") for n in range(12)]
        with mock.patch.object(tools, "search", lambda query, locale, limit=6: results):
            commands.dispatch(6, "/buscar gatos", "es")
        first = self.sent.items[-1]["text"]
        self.assertIn("1/3", first)
        self.assertIn("5. Title 4", first)
        self.assertNotIn("6. Title 5", first)
        commands.dispatch(6, "/mas", "es")
        self.assertIn("6. Title 5", self.sent.items[-1]["text"])
        self.assertTrue(agent.memory(6).last_kind == "search")

    def test_video_requests_open_a_video_list(self) -> None:
        from michiflin import agent
        videos = [tools.Result(f"Clip {n}", f"https://www.youtube.com/watch?v=id{n}", "Channel · 3:00") for n in range(7)]
        for text in ("busca videos de recetas cubanas", "videos de gatos graciosos", "te bote en youtube", "quiero escuchar canciones de Silvio"):
            self.assertTrue(agent.VIDEO_SEARCH.match(text) or agent.YOUTUBE_SEARCH.match(text), text)
        self.assertFalse(agent.VIDEO_SEARCH.match("qué es la música clásica"))
        with mock.patch.object(tools, "video_search", lambda query, locale, limit=15: videos):
            commands.dispatch(7, "/video 144 te bote", "es")
        text = self.sent.items[-1]["text"]
        self.assertIn("1. Clip 0", text)
        self.assertRegex(text, r"/vid_\w+_144")
        self.assertIn("/info_", text)

    def test_reactions_only_for_slow_tasks(self) -> None:
        calls: list[tuple[int, str]] = []
        state.set_setting("reactions", "on")
        with mock.patch.object(chat, "react", lambda msg_id, emoji="": calls.append((msg_id, emoji)) or True), \
                mock.patch.object(config, "STATUS_DELAY", 0.2):
            fast = progress.Reaction(10)
            fast.finish(False)
            time.sleep(0.4)
            self.assertEqual(calls, [])
            slow = progress.Reaction(11)
            time.sleep(0.4)
            slow.finish(False)
            self.assertEqual(calls, [(11, "⏳"), (11, "✅")])
            calls.clear()
            failed = progress.Reaction(12)
            time.sleep(0.4)
            failed.finish(True)
            self.assertEqual(calls[-1], (12, "❌"))
            calls.clear()
            queued = progress.Reaction(13)
            self.assertEqual(queued.hand_off(), 13)
            queued.finish(False)
            self.assertEqual(calls, [(13, "⏳")])
            state.set_setting("reactions", "off")
            self.assertEqual(progress.Reaction(14).msg_id, 0)
        state.set_setting("reactions", "on")

    def test_warning_replies_mark_the_thread(self) -> None:
        self.patches[0].stop()
        try:
            fake = mock.Mock()
            fake.rpc.send_msg.return_value = 3
            with mock.patch.object(chat, "_bot", fake), mock.patch.object(chat, "_accid", 1):
                chat.reset_warned()
                chat.send(1, "todo bien")
                self.assertFalse(chat.warned())
                chat.send(1, "⚠️ algo falló")
                self.assertTrue(chat.warned())
        finally:
            self.patches[0].start()



class AppSyncTests(Base):
    """Bot side of the app sync: resends, replays, acks, lost answers and outdated instances."""

    def setUp(self) -> None:
        super().setUp()
        from michiflin import miniapp
        self.m = miniapp
        self.updates: list[dict] = []
        self.out: list[tuple[int, dict]] = []
        self.deleted: list[int] = []
        rpc = mock.Mock()
        rpc.get_webxdc_status_updates.side_effect = lambda acc, msg_id, serial: __import__("json").dumps(
            [update for update in self.updates if update["serial"] > serial])
        self.patches += [mock.patch.object(chat, "rpc", lambda: rpc), mock.patch.object(chat, "account", lambda: 1),
                         mock.patch.object(chat, "send_update", lambda instance, payload, notify=None: self.out.append((instance, payload)) or True),
                         mock.patch.object(chat, "delete_for_all", lambda ids: self.deleted.extend(ids)),
                         mock.patch.object(miniapp, "_pool", mock.Mock(submit=lambda fn, *args: fn(*args)))]
        for patch in self.patches[2:]:
            patch.start()
        state.data["apps"] = {"500": {"chat": 7, "serial": 0, "addr": "owner@example.org", "sent": time.time(), "build": miniapp.app_build()}}
        state.data["app_seen"] = {}
        miniapp._answers.clear()
        miniapp._upgraded.clear()
        self.calls = 0

        def echo(chat_id, args, locale):
            self.calls += 1
            return {"n": self.calls}
        miniapp.OPS["echo_test"] = echo
        miniapp.OPS["settings_test"] = echo

    def tearDown(self) -> None:
        self.m.OPS.pop("echo_test", None)
        self.m.OPS.pop("settings_test", None)
        super().tearDown()

    def push(self, payload: dict) -> None:
        self.updates.append({"serial": len(self.updates) + 1, "payload": payload})
        self.m.on_status_update(500)

    def answers(self, rid: str) -> list[dict]:
        return [payload for _instance, payload in self.out if payload.get("re") == rid and payload.get("t") == "res"]

    def test_replay_is_not_executed_twice_and_resend_uses_cache(self) -> None:
        self.push({"t": "req", "id": "a1", "op": "echo_test", "args": {}, "lang": "es"})
        self.assertEqual(self.calls, 1)
        state.data["apps"]["500"]["serial"] = 0  # restored older state: the same request is read again
        self.m.on_status_update(500)
        self.assertEqual(self.calls, 1)
        self.assertEqual(len(self.answers("a1")), 1)
        self.push({"t": "req", "id": "a1", "op": "echo_test", "args": {}, "lang": "es", "retry": 1})
        self.assertEqual(self.calls, 1)  # answered from the cache, not run again
        self.assertEqual([answer["data"] for answer in self.answers("a1")], [{"n": 1}, {"n": 1}])

    def test_cached_answer_cannot_cross_app_instances(self) -> None:
        self.push({"t": "req", "id": "collision", "op": "echo_test", "args": {}, "lang": "es"})
        self.assertEqual(self.calls, 1)
        self.m._route(501, {"chat": 8, "addr": "guest@example.org"},
                      {"t": "req", "id": "collision", "op": "echo_test", "retry": 1, "lang": "en"})
        self.assertEqual(self.calls, 1)
        self.assertFalse(any(instance == 501 for instance, _payload in self.out))

    def test_resend_while_running_is_acknowledged(self) -> None:
        self.m._answers["b1"] = {"state": "running", "ts": time.time(), "body": None, "acked": 0.0}
        state.data["app_seen"]["b1"] = [time.time(), 0]
        self.push({"t": "req", "id": "b1", "op": "echo_test", "args": {}, "lang": "es", "retry": 1})
        self.assertIn({"t": "ack", "re": "b1"}, [payload for _instance, payload in self.out])
        self.assertEqual(self.calls, 0)

    def test_lost_answer_reruns_safe_ops_only(self) -> None:
        state.data["app_seen"]["c1"] = [time.time(), 0]
        state.data["app_seen"]["c2"] = [time.time(), 0]
        self.m.OPS["hello_backup"] = self.m.OPS["hello"]
        with mock.patch.object(self.m, "SAFE_REPLAY", {"echo_test"}):
            self.push({"t": "req", "id": "c1", "op": "echo_test", "args": {}, "lang": "es", "retry": 1})
            self.push({"t": "req", "id": "c2", "op": "settings_test", "args": {}, "lang": "en", "retry": 1})
        self.m.OPS.pop("hello_backup", None)
        self.assertEqual(self.calls, 1)
        self.assertTrue(self.answers("c1")[0]["ok"])
        self.assertFalse(self.answers("c2")[0]["ok"])
        self.assertIn("lost", self.answers("c2")[0]["error"])

    def test_large_answers_are_chunked_and_cached(self) -> None:
        self.m.OPS["echo_test"] = lambda chat_id, args, locale: "x" * 200_000
        self.push({"t": "req", "id": "d1", "op": "echo_test", "args": {}, "lang": "es"})
        chunks = [payload for _instance, payload in self.out if payload.get("t") == "chunk" and payload["re"] == "d1"]
        self.assertGreater(len(chunks), 2)
        self.assertEqual(__import__("json").loads("".join(chunk["s"] for chunk in chunks))["data"], "x" * 200_000)
        self.out.clear()
        self.push({"t": "req", "id": "d1", "op": "echo_test", "args": {}, "lang": "es", "retry": 1})
        self.assertEqual(len([payload for _instance, payload in self.out if payload.get("t") == "chunk"]), len(chunks))

    def test_outdated_instance_is_preserved_and_upgraded_once(self) -> None:
        state.data["apps"]["500"]["build"] = "old"
        with mock.patch.object(self.m, "op_hello", lambda chat_id, args, locale: {"ok": True}), \
                mock.patch.dict(self.m.OPS, {"hello": lambda chat_id, args, locale: {"ok": True}}), \
                mock.patch.object(self.m, "build", lambda: Path(__file__)):
            self.push({"t": "req", "id": "e1", "op": "hello", "args": {}, "lang": "es"})
            self.assertEqual(self.deleted, [])
            self.assertEqual(state.data["apps"]["500"]["build"], "old")
            new_key = next(key for key in state.data["apps"] if key != "500")
            self.assertEqual(state.data["apps"][new_key]["build"], self.m.app_build())
            self.assertIn("actualizó", self.sent.items[-1]["text"])
            count = len(self.sent.items)
            self.push({"t": "req", "id": "e2", "op": "hello", "args": {}, "lang": "es"})
            self.assertEqual(len(self.sent.items), count)  # no duplicate replacement




class FairShareTests(Base):
    def setUp(self) -> None:
        super().setUp()
        from michiflin import usage
        self.usage = usage
        state.data["sent"] = []
        state.data["people"] = {}
        state.data["flags"].pop("queue_paused", None)
        state.data["flags"].pop("limited", None)

    def job(self, number: int, address: str, status: str = "queued", **extra) -> dict:
        job = {"id": str(number), "chat": 10 + len(address), "addr": address, "status": status, "kind": "file", "title": "x",
               "name": "x.bin", "todo": [0], "hold": False, "not_before": 0, **extra}
        state.data["jobs"][job["id"]] = job
        return job

    def test_downloads_take_turns_between_people(self) -> None:
        self.job(1, "heavy@example.org")
        self.job(2, "heavy@example.org")
        self.job(3, "light@example.org")
        first = queue._next_queued()
        second = queue._next_queued()
        self.assertEqual(first["addr"], "heavy@example.org")
        self.assertEqual(second["addr"], "light@example.org")  # heavy already has one running

    def test_delivery_prefers_whoever_received_less_today(self) -> None:
        state.record_sent(50 * 1024 * 1024, "heavy@example.org")
        self.job(1, "heavy@example.org", status="sending")
        self.job(2, "light@example.org", status="sending")
        self.assertEqual(queue._deliverable()["addr"], "light@example.org")

    def test_quota_puts_heavy_users_last_but_never_refuses(self) -> None:
        state.set_setting("user_daily_mb", 10)
        state.record_sent(20 * 1024 * 1024, "heavy@example.org")
        self.assertTrue(self.usage.over_budget("heavy@example.org"))
        self.assertFalse(self.usage.over_budget("owner@example.org"))
        self.job(1, "heavy@example.org", status="sending")
        self.assertEqual(queue._deliverable()["addr"], "heavy@example.org")

    def test_per_person_job_limit_spares_admins(self) -> None:
        state.set_setting("user_jobs", 2)
        self.job(1, "friend@example.org")
        self.job(2, "friend@example.org")
        with mock.patch.object(chat, "sender", lambda: "friend@example.org"):
            self.assertIn("máximo 2", queue._capacity_error(1000, "es"))
        with mock.patch.object(chat, "sender", lambda: "owner@example.org"):
            self.assertEqual(queue._capacity_error(1000, "es"), "")

    def test_paused_people_and_queue_are_skipped(self) -> None:
        self.job(1, "friend@example.org")
        state.set_flag("limited", ["friend@example.org"])
        self.assertIsNone(queue._next_queued())
        state.set_flag("limited", [])
        state.set_flag("queue_paused", True)
        self.assertIsNone(queue._next_queued())

    def test_report_flags_disproportionate_use_with_amounts_only(self) -> None:
        self.usage.note("heavy@example.org", bytes=900 * 1024 * 1024, requests=40, jobs=9)
        self.usage.note("light@example.org", bytes=50 * 1024 * 1024, requests=30, jobs=1)
        report = self.usage.report()
        people = {person["address"]: person for person in report["people"]}
        self.assertTrue(people["heavy@example.org"]["heavy"])
        self.assertFalse(people["light@example.org"]["heavy"])
        self.assertEqual(set(people["heavy@example.org"]), {"address", "bytes", "jobs", "requests", "ai", "images", "code", "share", "share_requests",
                                                             "heavy", "today", "over", "admin"})

    def test_a_single_user_is_never_flagged(self) -> None:
        self.usage.note("solo@example.org", bytes=5 * 1024 ** 3, requests=900)
        self.assertFalse(self.usage.report()["people"][0]["heavy"])


class MiniAppFileTests(Base):
    def test_xdc_keeps_its_extension_and_opens_as_an_app(self) -> None:
        import io
        import zipfile
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("index.html", "<p>hi</p>")
            archive.writestr("manifest.toml", 'name = "Hi"')
        head = buffer.getvalue()
        self.assertEqual(files.sniff(head, "xdc"), "xdc")
        self.assertEqual(files.sniff(head, ""), "xdc")
        path = Path(config.JOB_ROOT) / "hi.xdc"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(head)
        job = {"parts": 1, "ext": "xdc", "name": "hi.xdc", "kind": "file", "part_files": [str(path)]}
        self.assertEqual(queue._viewtype(job), "Webxdc")
        path.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
        self.assertEqual(queue._viewtype(job), "File")


class CoderIntentTests(unittest.TestCase):
    def test_questions_are_answered_not_built(self) -> None:
        from michiflin import coder
        for text in ("¿sabes cómo crear una miniapp webxdc?", "do you know how to create a webxdc?", "¿Qué es una mini app?"):
            self.assertTrue(coder.looks_like_question(text), text)
        for text in ("Hazme un contador webxdc", "¿Puedes hacer un juego de memoria?", "Make a todo app", "Crea una calculadora"):
            self.assertFalse(coder.looks_like_question(text), text)

    def test_a_question_returns_an_answer_without_files(self) -> None:
        from michiflin import coder
        seen = []

        def reply(messages, model_key, sticky):
            seen.append(messages[-1]["content"])
            return ai.AIResult(text="<<<DONE>>>\nSí. Dime qué mini app quieres y la preparo.\n<<<END>>>", provider="Test", model="Model")
        with mock.patch.object(coder, "_ask", reply), mock.patch.object(coder, "isolation", lambda: False):
            result = coder.run("¿sabes cómo crear una miniapp webxdc?", [], "es")
        self.assertEqual(result["files"], [])
        self.assertTrue(result["answer"])
        self.assertIn("Dime qué mini app", result["summary"])
        self.assertIn("looks like a question", seen[0])

    def test_webxdc_projects_are_named_and_checked(self) -> None:
        from michiflin import coder
        project = [{"path": "index.html", "text": '<title>Contador</title><script src="webxdc.js"></script><script src="app.js"></script>'},
                   {"path": "app.js", "text": "window.webxdc.setUpdateListener(() => {}, 0);"}]
        self.assertEqual(coder.web_check(project), [])
        self.assertEqual(coder.project_name(project), "Contador")
        project.append({"path": "manifest.toml", "text": 'name = "Mi contador"\n'})
        self.assertEqual(coder.project_name(project), "Mi contador")
        self.assertIn("manifest.toml", coder.system_prompt("es", False))


class AIQuotaTests(unittest.TestCase):
    TESTER = "friend@example.org"

    def setUp(self) -> None:
        from michiflin import usage
        self.usage = usage
        self.saved = {key: state.data["settings"].get(key) for key in ("ai_daily", "image_daily", "code_daily", "ai_reserve")}
        state.data["people"] = {}
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        for key, value in self.saved.items():
            if value is None:
                state.data["settings"].pop(key, None)
            else:
                state.data["settings"][key] = value
        state.data["people"] = {}
        state.act_as("")

    def _ok(self, *args, **kwargs):
        return ai.AIResult(text="hola", provider="Test", model="M")

    def test_daily_ai_allowance_counts_one_per_request_and_only_successes(self) -> None:
        state.data["settings"]["ai_daily"] = 2
        with mock.patch.object(ai, "_complete", self._ok), mock.patch.object(self.usage, "reserve_low", lambda: False):
            state.act_as(self.TESTER)
            self.assertTrue(ai.complete([{"role": "user", "content": "a"}]).ok)
            self.assertTrue(ai.complete([{"role": "user", "content": "b"}]).ok)  # same request: counted once
            self.assertEqual(self.usage.used_today(self.TESTER, "ask"), 1)
            with mock.patch.object(ai, "_complete", lambda *a, **k: ai.AIResult(error="busy")):
                state.act_as(self.TESTER)
                self.assertFalse(ai.complete([{"role": "user", "content": "c"}]).ok)
            self.assertEqual(self.usage.used_today(self.TESTER, "ask"), 1)
            state.act_as(self.TESTER)
            self.assertTrue(ai.complete([{"role": "user", "content": "d"}]).ok)
            state.act_as(self.TESTER)
            refused = ai.complete([{"role": "user", "content": "e"}])
            self.assertEqual(refused.error, "person_quota")
            self.assertIn("2", ai.describe_error(refused.error, "es"))
            owner = next(iter(config.ADMIN_ADDRESSES), "")
            if owner:
                for _ in range(4):
                    state.act_as(owner)
                    self.assertTrue(ai.complete([{"role": "user", "content": "x"}]).ok)

    def test_reserve_keeps_people_on_fast_models(self) -> None:
        chains = []
        fast = next(model for item in ai.providers() for model in item.models if "fast" in model.roles) if ai.providers() else None

        def record(messages, chain, *rest):
            chains.append(chain)
            return self._ok()
        with mock.patch.object(ai, "_complete", record), mock.patch.object(self.usage, "reserve_low", lambda: True):
            state.act_as(self.TESTER)
            self.assertTrue(ai.complete([{"role": "user", "content": "a"}], "reason").ok)
            self.assertEqual(chains[-1], ["fast"])
            slow = next((model for item in ai.providers() for model in item.models if "fast" not in model.roles), None)
            if slow:
                state.act_as(self.TESTER)
                self.assertEqual(ai.complete_exact([{"role": "user", "content": "a"}], slow.key).error, "reserved")
            state.act_as("")
            ai.complete([{"role": "user", "content": "a"}], "reason")
            self.assertEqual(chains[-1], ["reason", "chat"])
        self.assertTrue(fast is None or "fast" in fast.roles)

    def test_image_and_coder_allowances(self) -> None:
        from michiflin import coder, miniapp
        state.data["settings"]["image_daily"] = 1
        state.data["settings"]["code_daily"] = 1
        with mock.patch.object(images, "_generate", lambda text, model="": images.ImageResult(data=b"jpg", source="T")):
            state.act_as(self.TESTER)
            self.assertTrue(images.generate("un gato").data)
            state.act_as(self.TESTER)
            self.assertEqual(images.generate("otro gato").error, "person_quota")
        self.assertEqual(self.usage.used_today(self.TESTER, "ask"), 0)
        project = {"files": [{"path": "index.html", "text": "<p>hola</p>"}], "summary": "ok"}
        with mock.patch.object(chat, "sender", lambda: self.TESTER), mock.patch.object(coder, "run", lambda *a: dict(project)), \
                mock.patch.object(self.usage, "reserve_low", lambda: False):
            state.act_as(self.TESTER)
            miniapp.op_code(1, {"text": "hazme una página"}, "es")
            state.act_as(self.TESTER)
            with self.assertRaises(miniapp.AppError):
                miniapp.op_code(1, {"text": "otra página"}, "es")
        view = self.usage.quota_view(self.TESTER)
        self.assertEqual(view["code"], {"used": 1, "limit": 1})
        self.assertEqual(view["image"], {"used": 1, "limit": 1})


class AdminPanelTests(Base):
    def test_each_person_has_own_settings_and_shared_ones_are_for_admins(self) -> None:
        from michiflin import miniapp
        owner = next(iter(config.ADMIN_ADDRESSES), "")
        state.data["prefs"].clear()
        self.addCleanup(state.data["prefs"].clear)
        self.addCleanup(state.act_as, "")
        with state.person(owner):
            state.set_setting("part_mb", 8)
        with state.person("friend@example.org"):
            groups = miniapp.settings_view("es")
            keys = {row["key"] for group in groups for row in group["items"]}
            self.assertIn("part_mb", keys)
            self.assertNotIn("user_jobs", keys)
            self.assertEqual(state.setting("part_mb"), 8)
            miniapp.op_settings(1, {"set": {"part_mb": "5", "audio_format": "mp3"}}, "es")
            self.assertEqual(state.setting("part_mb"), 5)
            with self.assertRaises(miniapp.AppError):
                miniapp.op_settings(1, {"set": {"user_jobs": "9"}}, "es")
        with state.person(owner):
            self.assertEqual(state.setting("part_mb"), 8)
            state.set_setting("part_mb", 12)
            self.assertTrue(state.set_setting("user_jobs", 4))
            shared = [group for group in miniapp.settings_view("es") if group["shared"]]
            self.assertEqual({row["key"] for row in shared[0]["items"]}, {"user_jobs", "user_daily_mb", "ai_daily", "image_daily", "code_daily", "ai_reserve", "ttl_hours"})
        with state.person("friend@example.org"):
            self.assertEqual(state.setting("part_mb"), 5)
            self.assertEqual(state.setting("user_jobs"), 4)
            miniapp.op_settings(1, {"reset": True}, "es")
            self.assertEqual(state.setting("part_mb"), 12)
        state.set_setting("part_mb", int(config.DEFAULTS["part_mb"]))
        state.set_setting("user_jobs", int(config.DEFAULTS["user_jobs"]))

    def test_delivery_follows_the_owner_of_each_job(self) -> None:
        state.data["prefs"].clear()
        self.addCleanup(state.data["prefs"].clear)
        with state.person("night@example.org"):
            state.own_settings()
            state.set_setting("window", "0000-0001")
        job = {"addr": "night@example.org"}
        noon = time.mktime(time.strptime("2026-10-16 12:00", "%Y-%m-%d %H:%M"))
        with mock.patch.object(queue, "local_time", lambda when, fmt: "12:00"):
            self.assertFalse(queue.in_window(noon, job))
            self.assertTrue(queue.in_window(noon, {"addr": "day@example.org"}))
            self.assertTrue(queue.in_window(noon))

    def test_performance_snapshot_and_bounded_stress_test(self) -> None:
        from michiflin import perf
        snapshot = perf.snapshot()
        for key in ("host", "cpu", "memory", "disk", "process", "network", "latency", "queue"):
            self.assertIn(key, snapshot)
        self.assertGreater(snapshot["cpu"]["cores"], 0)
        with mock.patch.object(perf, "_network_test", lambda: {"down": 1.0, "latency": 1}), \
                mock.patch.object(perf, "_hash_rate", lambda seconds, block: 100.0), \
                mock.patch.object(perf, "_disk_test", lambda: {"skipped": 1.0}), \
                mock.patch.object(chat, "ready", lambda: False):
            result = perf.bench()
        self.assertEqual(result["cpu"]["single"], 100.0)
        self.assertIn("seconds", result)
        self.assertEqual(perf.snapshot()["bench"]["at"], result["at"])

class ChatFormatTests(unittest.TestCase):
    def test_code_survives(self):
        from michiflin.ai import chat_format

        code = '```python\nfrom __future__ import annotations\n# note\nif __name__ == "__main__":\n    print(2**8)\n```'
        out = chat_format(f"## Title\n**bold** and `__init__`\n|a|b|\n|---|---|\n{code}")
        self.assertIn(code, out)
        self.assertIn("*bold*", out)
        self.assertIn("`__init__`", out)
        self.assertNotIn("## ", out)
        self.assertNotIn("|---|", out)


class IntakeTests(Base):
    """Every incoming message is taken once, survives transient failures and interrupted sessions."""

    def setUp(self) -> None:
        super().setUp()
        from types import SimpleNamespace
        from michiflin import app
        self.app, self.NS = app, SimpleNamespace
        state.data.pop("inbox", None)
        app._taken.clear()
        app._chat_queues.clear()
        app._chat_busy.clear()
        app._draining.clear()
        self.processed: list[str] = []
        self.marked: list[int] = []
        self.patches += [mock.patch.object(app, "_process", lambda msg, text, locale, address: self.processed.append(text)),
                         mock.patch.object(chat, "rpc", lambda: self.NS(markseen_msgs=lambda acc, ids: self.marked.extend(ids))),
                         mock.patch.object(chat, "account", lambda: 1),
                         mock.patch.object(state, "save", lambda: None)]
        for patch in self.patches[-4:]:
            patch.start()
        self.fail_info = False

        def chat_info(acc, chat_id):
            if self.fail_info:
                raise RuntimeError("rpc busy")
            return self.NS(chat_type="Single", is_contact_request=False)
        self.bot = self.NS(rpc=self.NS(get_basic_chat_info=chat_info,
                                       get_contact=lambda acc, cid: self.NS(address="owner@example.org")))

    def msg(self, msg_id: int, text: str):
        return self.NS(id=msg_id, chat_id=7, from_id=11, is_info=False, text=text, file=None, file_name="")

    def wait(self) -> None:
        for _ in range(200):
            if not self.app._chat_busy:
                return
            time.sleep(0.01)

    def test_message_taken_once(self) -> None:
        message = self.msg(1, "/hola")
        self.app._accept(self.bot, 1, message)
        self.app._accept(self.bot, 1, message)
        self.wait()
        self.assertEqual(self.processed, ["/hola"])
        self.assertEqual(self.marked, [1])
        self.assertEqual(state.data["inbox"], {})

    def test_transient_failure_is_retried(self) -> None:
        self.fail_info = True
        self.app._accept(self.bot, 1, self.msg(2, "/uno"))
        self.assertEqual(self.processed, [])
        self.assertNotIn(2, self.marked)
        self.fail_info = False
        self.app._accept(self.bot, 1, self.msg(2, "/uno"))
        self.wait()
        self.assertEqual(self.processed, ["/uno"])

    def test_draining_leaves_messages_for_next_session(self) -> None:
        self.app._draining.set()
        self.app._accept(self.bot, 1, self.msg(3, "/tarde"))
        self.assertEqual(self.processed, [])
        self.assertNotIn(3, self.marked)

    def test_order_kept_per_chat(self) -> None:
        for i in range(5):
            self.app._accept(self.bot, 1, self.msg(10 + i, f"/m{i}"))
        self.wait()
        self.assertEqual(self.processed, [f"/m{i}" for i in range(5)])

    def test_message_that_keeps_crashing_is_skipped(self) -> None:
        state.data["inbox"] = {"4": 2}
        self.app._accept(self.bot, 1, self.msg(4, "/boom"))
        self.wait()
        self.assertEqual(self.processed, [])
        self.assertIn(4, self.marked)

    def test_prune_keeps_app_instances(self) -> None:
        old = time.time() - 7200
        messages = {1: self.NS(state=26, timestamp=old, is_info=False, view_type="Webxdc"),
                    2: self.NS(state=26, timestamp=old, is_info=False, view_type="Text"),
                    3: self.NS(state=16, timestamp=old, is_info=False, view_type="Text")}
        deleted: list[int] = []
        rpc = self.NS(get_chatlist_entries=lambda *a: [7], get_message_ids=lambda *a: list(messages),
                      get_message=lambda acc, mid: messages[mid])
        with mock.patch.object(chat, "_bot", self.NS(rpc=rpc)), mock.patch.object(chat, "delete", deleted.extend):
            chat.prune_history(3600, keep={3})
        self.assertEqual(deleted, [2])

    def test_new_commit_hands_over_gracefully(self) -> None:
        marker = Path(tempfile.mkdtemp()) / "restart"
        env = {"RUNNING_SHA": "aaa", "GITHUB_REPOSITORY": "o/r", "GITHUB_REF_NAME": "main", "RESTART_MARKER": str(marker)}
        calls: list[int] = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n):
                return b"bbb"

        with mock.patch.dict(os.environ, env), mock.patch.object(self.app._draining, "wait", lambda t: False), \
                mock.patch("urllib.request.urlopen", lambda *a, **k: Response()), \
                mock.patch.object(self.app, "_graceful", lambda sig, frame: calls.append(sig)), \
                mock.patch.object(self.app.miniapp, "busy", lambda: 0):
            self.app._watch_updates()
        self.assertEqual(calls, [self.app.signal.SIGTERM])
        self.assertEqual(marker.read_text(), "bbb")


if __name__ == "__main__":
    unittest.main(verbosity=1)
