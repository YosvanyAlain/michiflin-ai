"""Offline regression tests for media, model budgets, and large coding requests."""
from __future__ import annotations

import base64
import io
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from PIL import Image

from michiflin import ai, cf_safety, coder, config, images, media, miniapp, music, state


class Response:
    def __init__(self, status=200, body=None, chunks=None, content_type="application/json", text=""):
        self.status_code = status
        self.ok = status < 400
        self.body = body
        self.chunks = chunks if chunks is not None else [json.dumps(body).encode()]
        self.headers = {"content-type": content_type}
        self.text = text
        self.closed = False
        self.reads = 0

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError("failed")

    def iter_content(self, chunk_size=65536):
        for part in self.chunks:
            self.reads += 1
            yield part

    def close(self):
        self.closed = True


def tiny_image() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(output, "PNG")
    return output.getvalue()


class Isolated(unittest.TestCase):
    def setUp(self):
        self.old = {key: state.data[key] for key in ("usage", "cooldowns", "reasons", "remaining")}
        for key in self.old:
            state.data[key] = {}
        self.save = mock.patch.object(state, "save").start()
        self.save_soon = mock.patch.object(state, "_save_soon").start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        for key, value in self.old.items():
            state.data[key] = value


class MusicTests(Isolated):
    def test_video_container_quality_and_unknown_size(self):
        options = media.format_options("video", 2160, "webm")
        self.assertEqual(options["merge_output_format"], "webm")
        self.assertIn("ext=webm", options["format"])
        self.assertEqual(media.format_options("video", 1440, "mkv")["merge_output_format"], "mkv")
        self.assertEqual(media.resolve_quality("video", "2160p", 360), 2160)
        self.assertEqual(media.resolve_quality("audio", "320k", 64), 320)
        video, _audio, maximum = media._estimates({"formats": [{"height": 2160, "vcodec": "av1", "acodec": "none"}]})
        self.assertEqual(maximum, 2160)
        self.assertEqual(video[2160], 0)  # Available, but no fabricated size estimate.

    def test_stream_is_bounded_before_the_rest_is_read(self):
        reply = Response(chunks=[b"a" * 4_100_000, b"b" * 4_100_000, b"not-read"])
        with mock.patch.object(music.requests, "get", return_value=reply) as get:
            with self.assertRaises(music.MusicError):
                music._get("/metadata/example")
        self.assertEqual(reply.reads, 2)
        self.assertTrue(reply.closed)
        self.assertTrue(get.call_args.kwargs["stream"])
        self.assertFalse(get.call_args.kwargs["allow_redirects"])

    def test_redirect_and_non_object_are_rejected(self):
        for reply in (Response(status=302), Response(body=[1, 2])):
            with self.subTest(status=reply.status_code), mock.patch.object(music.requests, "get", return_value=reply):
                with self.assertRaises(music.MusicError):
                    music._get("/metadata/example")
                self.assertTrue(reply.closed)

    def test_search_escapes_solr_and_asks_for_actual_audio_formats(self):
        body = {"response": {"docs": [{"identifier": "album-1", "title": "Music", "creator": "Artist"},
                                      {"identifier": "../escape", "title": "bad"}]}}
        with mock.patch.object(music, "_get", return_value=body) as get:
            found = music.search('jazz" OR mediatype:movies')
        query = get.call_args.args[1]["q"]
        self.assertIn('format:(MP3 OR', query)
        self.assertIn('jazz\\" OR mediatype:movies', query)
        self.assertEqual([item["id"] for item in found], ["album-1"])

    def test_item_filters_private_restricted_and_path_escape(self):
        body = {"metadata": {"mediatype": "audio", "title": "Album", "licenseurl": "https://example.org/license"},
                "files": [{"name": "Song 01.mp3", "format": "VBR MP3", "size": "123", "bitrate": "128"},
                          {"name": "secret.ogg", "private": "true"}, {"name": "../escape.mp3"},
                          {"name": "cover.png"}, {"name": "nested/other.flac", "size": "unknown"}]}
        with mock.patch.object(music, "_get", return_value=body):
            item = music.item("album-1")
        self.assertEqual([f["name"] for f in item["files"]], ["Song 01.mp3", "nested/other.flac"])
        self.assertEqual(item["files"][0]["size"], 123)
        self.assertTrue(item["files"][0]["url"].startswith("https://archive.org/download/album-1/"))
        self.assertIn("Song%2001.mp3", item["files"][0]["url"])
        body["metadata"]["access-restricted-item"] = "true"
        with mock.patch.object(music, "_get", return_value=body):
            self.assertEqual(music.item("album-1")["files"], [])
        with self.assertRaises(music.MusicError):
            music.item("../escape")

    def test_download_audio_options_are_validated_and_passed_per_job(self):
        captured = []
        def capture(operation, _locale):
            operation()
            return {"text": "queued"}
        with mock.patch.object(miniapp, "run_captured", side_effect=capture), \
             mock.patch.object(miniapp, "jobs_view", return_value=[]), \
             mock.patch.object(miniapp.web, "enqueue_media", side_effect=lambda *args: captured.append(args)):
            miniapp.op_action(7, {"kind": "audio", "url": "https://youtube.com/watch?v=a", "quality": 128,
                                  "audio_format": "opus"}, "en")
            self.assertEqual(captured[0][-1], "opus")
            self.assertEqual(captured[0][3], 128)
            miniapp.op_action(7, {"kind": "video", "url": "https://youtube.com/watch?v=a", "quality": 2160,
                                  "video_format": "webm"}, "en")
            self.assertEqual(captured[-1][-1], "webm")
            self.assertEqual(captured[-1][3], 2160)
            with self.assertRaises(miniapp.AppError):
                miniapp.op_action(7, {"kind": "audio", "url": "https://example.org", "audio_format": "exe"}, "en")
            with self.assertRaises(miniapp.AppError):
                miniapp.op_action(7, {"kind": "video", "url": "https://example.org", "video_format": "exe"}, "en")
            with self.assertRaises(miniapp.AppError):
                miniapp.op_action(7, {"kind": "video", "url": "https://example.org", "audio_format": "mp3"}, "en")


class BudgetAndImagesTests(Isolated):
    def test_reservations_are_atomic_under_concurrency_and_persist_before_use(self):
        with mock.patch.object(config, "CLOUDFLARE_ESTIMATED_NEURONS", 1000):
            with ThreadPoolExecutor(max_workers=20) as workers:
                outcomes = list(workers.map(lambda _: cf_safety.reserve(100), range(30)))
        self.assertEqual(sum(outcomes), 10)
        self.assertEqual(cf_safety.used(), 1000)
        self.assertEqual(self.save.call_count, 10)

    def test_failed_persistence_does_not_grant_a_reservation(self):
        self.save.side_effect = OSError("disk")
        with mock.patch.object(config, "CLOUDFLARE_ESTIMATED_NEURONS", 1000):
            self.assertFalse(cf_safety.reserve(200))
        self.assertEqual(cf_safety.used(), 0)

    def test_cost_scales_and_unknown_audio_is_denied(self):
        self.assertGreater(cf_safety.image_cost("cf:flux-2-dev", 1024, 1024),
                           cf_safety.image_cost("cf:flux-2-klein-4b", 1024, 1024))
        self.assertGreater(cf_safety.text_cost("unknown", [{"content": "x" * 500}], None, 8000),
                           cf_safety.text_cost("unknown", [{"content": "x"}], None, 1000))
        self.assertIsNone(cf_safety.audio_cost(b"not audio"))

    def test_ovh_streaming_and_status_errors(self):
        raw = tiny_image()
        reply = Response(chunks=[raw], content_type="image/png")
        with mock.patch.object(images.requests, "post", return_value=reply):
            self.assertEqual(images._run(images.Source("ovh:sdxl", "OVH", 80), "test", 512, 512), raw)
        self.assertTrue(reply.closed)
        with mock.patch.object(images.requests, "post", return_value=Response(status=429)):
            with self.assertRaises(images._BusyError):
                images._run(images.Source("ovh:sdxl", "OVH", 80), "test", 512, 512)
        big = Response(chunks=[b"a" * 6_000_000, b"b" * 6_000_000, b"not-read"], content_type="image/png")
        with mock.patch.object(images.requests, "post", return_value=big):
            with self.assertRaises(images._SourceError):
                images._run(images.Source("ovh:sdxl", "OVH", 80), "test", 512, 512)
        self.assertEqual(big.reads, 2)
        self.assertTrue(big.closed)

    def test_pollinations_rejects_redirects_and_bounds_image_body(self):
        redirect = Response(status=302, content_type="image/jpeg")
        with mock.patch.object(images.requests, "get", return_value=redirect) as get:
            with self.assertRaises(images._SourceError):
                images._run(images.Source("pollinations:flux", "P", 5), "test", 512, 512)
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        self.assertTrue(get.call_args.kwargs["stream"])
        self.assertTrue(redirect.closed)
        huge = Response(chunks=[b"a" * 6_000_000, b"b" * 6_000_000, b"not-read"], content_type="image/jpeg")
        with mock.patch.object(images.requests, "get", return_value=huge):
            with self.assertRaises(images._SourceError):
                images._run(images.Source("pollinations:flux", "P", 5), "test", 512, 512)
        self.assertEqual(huge.reads, 2)
        self.assertTrue(huge.closed)

    def test_horde_does_not_fetch_private_result_urls(self):
        replies = iter([Response(body={"done": True}),
                        Response(body={"generations": [{"img": "https://127.0.0.1/private"}]})])
        def get(*args, **kwargs):
            return next(replies)
        with mock.patch.object(images.requests, "post", return_value=Response(body={"id":"job"})), \
             mock.patch.object(images.requests, "get", side_effect=get) as request, \
             mock.patch.object(images.time, "sleep"):
            with self.assertRaises(images._SourceError):
                images._horde("horde:flux", "x", 512, 512, 1)
        self.assertEqual(request.call_count, 2)

    def test_horde_image_fetch_uses_vetted_socket_and_verified_hostname(self):
        socket = mock.Mock()
        tls = mock.Mock()
        context = mock.Mock()
        context.wrap_socket.return_value = tls
        response = mock.Mock(status=200)
        response.getheader.return_value = "image/png"
        response.read1.side_effect = [b"image bytes", b""]
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch.object(config, "WEB_PROXY", ""), \
             mock.patch.object(images.egress, "_connect", return_value=socket) as connect, \
             mock.patch.object(images.ssl, "create_default_context", return_value=context), \
             mock.patch.object(images.http.client, "HTTPSConnection", return_value=connection):
            result = images._public_image("https://public.example/file.png?token=redacted")
        self.assertEqual(result, b"image bytes")
        connect.assert_called_once_with("public.example", 443, None)
        context.wrap_socket.assert_called_once_with(socket, server_hostname="public.example")
        connection.request.assert_called_once()
        self.assertEqual(connection.request.call_args.args[:2], ("GET", "/file.png?token=redacted"))
        response.close.assert_called_once()
        tls.close.assert_called_once()

    def test_horde_image_fetch_rejects_redirect_and_private_dns(self):
        with mock.patch.object(config, "WEB_PROXY", ""), \
             mock.patch.object(images.egress, "_connect", side_effect=images.egress.EgressError("private")) as connect:
            with self.assertRaises(images._SourceError):
                images._public_image("https://rebind.example/picture.png")
            connect.assert_called_once()
        socket, tls, context, connection = mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock()
        context.wrap_socket.return_value = tls
        response = mock.Mock(status=302)
        response.getheader.return_value = "image/png"
        connection.getresponse.return_value = response
        with mock.patch.object(config, "WEB_PROXY", ""), \
             mock.patch.object(images.egress, "_connect", return_value=socket), \
             mock.patch.object(images.ssl, "create_default_context", return_value=context), \
             mock.patch.object(images.http.client, "HTTPSConnection", return_value=connection):
            with self.assertRaises(images._SourceError):
                images._public_image("https://public.example/redirect")
        response.read1.assert_not_called()
        response.close.assert_called_once()
        tls.close.assert_called_once()

    def test_expensive_local_rejection_does_not_pause_all_cloudflare_models(self):
        cloud = images.Source("cf:flux-2-dev", "Cloud", 40)
        free = images.Source("ovh:sdxl", "OVH", 80)
        def run(source, *_args, **_kwargs):
            if source.key == cloud.key:
                raise images._BudgetError()
            return tiny_image()
        with mock.patch.object(images, "_ordered", return_value=[cloud, free]), \
             mock.patch.object(images, "english_prompt", side_effect=lambda x: x), \
             mock.patch.object(images, "_run", side_effect=run), \
             mock.patch.object(ai, "cloudflare_spent") as spent:
            result = images._generate("red boat")
        self.assertEqual(result.source, "OVH")
        self.assertTrue(result.data.startswith(b"\xff\xd8\xff"))
        spent.assert_not_called()
        self.assertGreater(state.cooldown_until(cloud.key), 0)

    def test_invalid_image_is_not_reported_as_success(self):
        source = images.Source("ovh:sdxl", "OVH", 80)
        with mock.patch.object(images, "sources", return_value=[source]), \
             mock.patch.object(images, "english_prompt", side_effect=lambda x: x), \
             mock.patch.object(images, "_run", return_value=b"not an image"):
            result = images._generate("boat", source.key)
        self.assertFalse(result.data)
        self.assertEqual(result.error, "response")
        self.assertEqual(state.usage_today(source.key), 0)
        self.assertEqual(state.data["usage"][state.utc_day()][source.key][:2], [1, 1])

    def test_image_lock_refuses_a_second_simultaneous_generation(self):
        entered, release = threading.Event(), threading.Event()
        def slow(*_args):
            entered.set()
            release.wait(5)
            return images.ImageResult(data=tiny_image(), source="test")
        with mock.patch.object(images, "_generate", side_effect=slow):
            thread = threading.Thread(target=lambda: images.generate("one"))
            thread.start()
            self.assertTrue(entered.wait(3))
            try:
                self.assertEqual(images.generate("two").error, "busy")
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())


class ModelAndCoderTests(Isolated):
    def test_ovh_anonymous_pacing_survives_restart_and_sends_no_key(self):
        clock = [100.0]
        model = ai.Model("ovh", "gpt-oss-120b", {"code"}, 500)
        provider = ai.Provider("ovh", "OVH", "openai", "https://example.org", api_key="should-not-be-sent")
        response = Response(body={"choices":[{"message":{"content":"ok"}}]})
        def sleep(seconds):
            clock[0] += seconds
        with mock.patch.dict(state.data, {"flags": {}}), \
             mock.patch.object(ai.time, "time", side_effect=lambda: clock[0]), \
             mock.patch.object(ai.time, "sleep", side_effect=sleep), \
             mock.patch.object(ai.state, "check_work_allowed"), \
             mock.patch.object(ai.progress, "phase"), \
             mock.patch.object(ai.requests, "post", return_value=response) as post:
            ai._openai(provider, model, [{"role":"user","content":"hi"}], None, 100)
            saved = json.loads(json.dumps(state.data["flags"]))
            state.data["flags"] = saved  # restore from serialized state
            ai._openai(provider, model, [{"role":"user","content":"hi"}], None, 100)
        self.assertGreaterEqual(clock[0], 135.0)
        self.assertNotIn("Authorization", post.call_args.kwargs["headers"])
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        self.assertEqual(self.save.call_count, 2)

    def test_finish_reason_marks_visible_and_thinking_only_truncation(self):
        model = ai.Model("test", "model", {"chat"}, 100)
        provider = ai.Provider("test", "Test", "openai", "https://example.org")
        reply = Response(body={"choices": [{"finish_reason": "length"}]})
        with mock.patch.object(ai, "_openai", return_value=(reply, "partial", 3)):
            result = ai._call(provider, model, [], None, 1000)
        self.assertTrue(result.ok)
        self.assertTrue(result.truncated)
        with mock.patch.object(ai, "_openai", return_value=(reply, "", 3)):
            result = ai._call(provider, model, [], None, 1000)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "truncated")

    def test_output_cap_retry_is_only_for_parameter_errors(self):
        model = ai.Model("test", "model", {"chat"}, 100)
        provider = ai.Provider("test", "Test", "openai", "https://example.org")
        bad = Response(status=400, text="max_tokens exceeds limit of 8192")
        good = Response(body={"choices": [{"finish_reason": "stop"}]})
        with mock.patch.object(ai, "_openai", side_effect=[(bad, "", 0), (good, "complete", 7)]) as call:
            result = ai._call(provider, model, [], None, 12000)
        self.assertEqual(result.text, "complete")
        self.assertEqual(call.call_args_list[1].args[4], 8192)

    def test_input_files_are_rejected_instead_of_truncated(self):
        invalid = [
            [{"path": "x.txt", "text": "z" * (coder.MAX_FILE + 1)}],
            [{"path": "x.bin", "b64": base64.b64encode(b"x" * (coder.MAX_BINARY + 1)).decode()}],
            [{"path": "x.txt", "text": "one"}, {"path": "./x.txt", "text": "two"}],
            [{"path": "../x.txt", "text": "bad"}],
            [{"path": "x.bin", "b64": "not!base64"}],
        ]
        for files in invalid:
            with self.subTest(files=files[0]["path"]), self.assertRaises(coder.CoderError):
                coder.validate_files(files, "en")
        coder.validate_files([{"path": "safe.txt", "text": "á😀"}], "en")

    def test_export_reports_unrepresentable_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / "large.txt").write_bytes(b"x" * (coder.MAX_FILE + 1))
            (path / "small.txt").write_text("ok")
            issues = []
            result = coder.read_files(path, issues)
        self.assertEqual([f["path"] for f in result], ["small.txt"])
        self.assertIn("large.txt", issues[0])

    def test_unsafe_input_write_aborts_before_any_model_call(self):
        files = [{"path": "important.txt", "text": "original"}]
        with mock.patch.object(coder, "isolation", return_value=False), \
             mock.patch.object(coder, "_safe_write_text", return_value=False), \
             mock.patch("michiflin.ai.complete") as complete:
            with self.assertRaises(coder.CoderError):
                coder.run("change this project", files, "en")
        complete.assert_not_called()
        with self.assertRaises(coder.CoderError):
            coder.validate_files([{"path": "node_modules/a.txt", "text": "keep"}], "en")

    def test_incomplete_export_returns_the_entire_original_project(self):
        files = [{"path": "important.txt", "text": "original"}, {"path": "other.txt", "text": "keep"}]
        response = ai.AIResult(text="<<<FILE important.txt>>>\nnew\n<<<END>>>\n<<<DONE>>>\nDone\n<<<END>>>",
                               provider="Test", model="Coder")
        real_read = coder.read_files
        def with_export_error(folder, issues=None):
            result = real_read(folder, issues)
            if issues is not None:
                issues.append("unexportable file")
            return result
        with mock.patch.object(coder, "isolation", return_value=False), \
             mock.patch.object(coder, "MAX_STEPS", 1), \
             mock.patch.object(coder, "read_files", side_effect=with_export_error), \
             mock.patch.object(coder, "_wrap_up", return_value="done"), \
             mock.patch("michiflin.ai.complete", return_value=response):
            result = coder.run("Change the project", files, "en")
        self.assertFalse(result["project_complete"])
        self.assertFalse(result["model_confirmed"])
        self.assertEqual(result["files"], files)
        self.assertIn("previous files were kept", result["summary"])

    def test_truncated_coder_output_cannot_claim_done(self):
        short = "<<<FILE hello.py>>>\nprint('hi')\n<<<END>>>\n<<<DONE>>>\nFinished\n<<<END>>>"
        responses = [ai.AIResult(text=short, provider="Test", model="Coder", truncated=True),
                     ai.AIResult(text=short.replace("Finished", "Checked"), provider="Test", model="Coder")]
        with mock.patch.object(coder, "isolation", return_value=False), \
             mock.patch.object(coder, "MAX_STEPS", 2), mock.patch("michiflin.ai.complete", side_effect=responses) as complete:
            project = coder.run("Create a Python script", [], "en")
        self.assertEqual(complete.call_count, 2)
        self.assertEqual(project["summary"], "Checked")
        self.assertTrue(project["model_confirmed"])
        self.assertEqual(project["files"][0]["path"], "hello.py")

    def test_failing_test_must_pass_before_later_done_is_accepted(self):
        responses = [
            ai.AIResult(text="<<<FILE hello.js>>>\nconsole.log('hi')\n<<<END>>>\n"
                             "<<<RUN>>>\nnode test.mjs 2>&1\n<<<END>>>\n<<<DONE>>>\nPremature\n<<<END>>>", provider="Test", model="Coder"),
            ai.AIResult(text="<<<DONE>>>\nStill premature\n<<<END>>>", provider="Test", model="Coder"),
            ai.AIResult(text="<<<RUN>>>\nnode test.mjs\n<<<END>>>\n<<<DONE>>>\nChecked\n<<<END>>>", provider="Test", model="Coder"),
        ]
        with mock.patch.object(coder, "isolation", return_value=True), mock.patch.object(coder, "_lock_private"), \
             mock.patch.object(coder, "syntax_check", return_value=[]), mock.patch.object(coder, "MAX_STEPS", 3), \
             mock.patch.object(coder, "_run", side_effect=[(1, "test failed"), (0, "test passed")]), \
             mock.patch("michiflin.ai.complete", side_effect=responses) as complete:
            project = coder.run("Create a JavaScript project", [], "en")
        self.assertEqual(complete.call_count, 3)
        self.assertEqual(project["summary"], "Checked")
        self.assertTrue(project["model_confirmed"])
        self.assertEqual([step["code"] for step in project["steps"] if step["kind"] == "run"], [1, 0])

    def test_cutoff_wrapup_is_explicitly_partial(self):
        response = ai.AIResult(text="<<<FILE hello.py>>>\nprint('hi')\n<<<END>>>", provider="Test", model="Coder")
        with mock.patch.object(coder, "isolation", return_value=False), mock.patch.object(coder, "MAX_STEPS", 1), \
             mock.patch("michiflin.ai.complete", return_value=response), mock.patch.object(coder, "_wrap_up", return_value="Looks done"):
            project = coder.run("Create a Python script", [], "en")
        self.assertIn("may be incomplete", project["summary"])
        self.assertIn("Looks done", project["summary"])
        self.assertTrue(project["project_complete"])  # export is lossless; it is still only a draft
        self.assertFalse(project["model_confirmed"])


class MultipartTests(Isolated):
    @staticmethod
    def parts(request):
        raw = json.dumps(request, ensure_ascii=False).encode()
        data = base64.b64encode(raw).decode()
        chunks = [data[i:i + miniapp.REQUEST_PART] for i in range(0, len(data), miniapp.REQUEST_PART)]
        return [{"t": "req_chunk", "id": request["id"], "i": i, "n": len(chunks), "s": value}
                for i, value in enumerate(chunks)]

    def test_shuffled_chunks_survive_state_serialization_and_reassemble_unicode(self):
        request = {"t": "req", "id": "abcde12345", "op": "code", "args": {"files": [{"path": "x.txt", "text": "🌴á" * 20000}]}}
        pieces = self.parts(request)
        self.assertGreater(len(pieces), 1)
        entry = {}
        self.assertIsNone(miniapp._request_piece(entry, pieces[-1]))
        entry = json.loads(json.dumps(entry))  # simulate a restart after state was saved
        result = None
        for piece in reversed(pieces[:-1]):
            result = miniapp._request_piece(entry, piece)
        self.assertEqual(result, request)
        self.assertEqual(entry["request_parts"], {})

    def test_expired_fragments_are_removed_even_without_new_requests(self):
        import time
        entry = {"request_parts": {"expired": {"at": time.time() - miniapp.REQUEST_AGE - 1},
                                   "current": {"at": time.time()}}}
        with mock.patch.dict(state.data, {"apps": {"1": entry}}):
            miniapp._prune_seen()
        self.assertEqual(set(entry["request_parts"]), {"current"})
        self.save.assert_called()

    def test_dispatch_rechecks_access_and_survives_a_restart_between_parts(self):
        request = {"t": "req", "id": "abcde12345", "op": "code", "args": {"text": "á" * 120000}}
        parts = self.parts(request)
        entry = {"addr": "owner@example.org", "chat": 7, "serial": 0, "build": "current"}
        fake_rpc = mock.Mock()
        with mock.patch.object(miniapp, "_instance", return_value=entry), \
             mock.patch.object(miniapp.access, "bound_allowed", return_value=True), \
             mock.patch.object(miniapp.admin, "is_allowed", return_value=True), \
             mock.patch.object(miniapp.config, "service_access_allowed", return_value=True), \
             mock.patch.object(miniapp, "app_build", return_value="current"), \
             mock.patch.object(miniapp.chat, "rpc", return_value=fake_rpc), \
             mock.patch.object(miniapp.chat, "account", return_value=1), \
             mock.patch.object(miniapp, "_route") as route:
            fake_rpc.get_webxdc_status_updates.return_value = json.dumps([{"serial": 1, "payload": parts[0]}])
            miniapp.on_status_update(9876)
            self.assertFalse(route.called)
            # Simulate persisting and loading state after a bot restart.
            saved = json.loads(json.dumps(entry))
            entry.clear(); entry.update(saved)
            fake_rpc.get_webxdc_status_updates.return_value = json.dumps([
                {"serial": i + 2, "payload": value} for i, value in enumerate(parts[1:])])
            miniapp.on_status_update(9876)
            self.assertEqual(route.call_count, 1)
            self.assertEqual(route.call_args.args[2], request)
            entry["request_parts"] = {}
            with mock.patch.object(miniapp.admin, "is_allowed", return_value=False):
                fake_rpc.get_webxdc_status_updates.return_value = json.dumps([{"serial": 100, "payload": parts[0]}])
                miniapp.on_status_update(9876)
            self.assertEqual(entry["request_parts"], {})
            self.assertEqual(route.call_count, 1)

    def test_conflicting_and_non_code_chunks_are_rejected(self):
        request = {"t": "req", "id": "abcde12345", "op": "code", "args": {"text": "x" * 100000}}
        pieces = self.parts(request)
        entry = {}
        self.assertIsNone(miniapp._request_piece(entry, pieces[0]))
        bad = dict(pieces[0], s="Z" + pieces[0]["s"][1:])
        self.assertIsNone(miniapp._request_piece(entry, bad))
        self.assertEqual(entry["request_parts"], {})
        forbidden = self.parts({"t": "req", "id": "abcde54321", "op": "action", "args": {}})
        self.assertIsNone(miniapp._request_piece({}, forbidden[0]))
        self.assertIsNone(miniapp._request_piece({}, dict(pieces[0], n=99)))


if __name__ == "__main__":
    unittest.main()
