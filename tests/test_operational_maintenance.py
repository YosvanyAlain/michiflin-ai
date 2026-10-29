"""Offline tests for operational maintenance, recovery and isolation.

No real users, external providers, Delta Chat servers or billable requests are used.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("OWNER_ADDRESS", "owner@example.org")
os.environ.setdefault("OWNER_ONLY", "1")
os.environ.setdefault("FREE_GATEWAYS", "0")
os.environ.setdefault("AI_HORDE", "0")

from michiflin import access, admin, agent, ai, app, chat, config, files, images, live_web, maintenance, media, miniapp, private_media, progress, queue, state

OWNER = "owner@example.org"
ALICE = "alice@example.org"
BOB = "bob@example.org"
NEW = "new@example.org"


class Immediate:
    def submit(self, function, *args, **kwargs):
        future = Future()
        try:
            future.set_result(function(*args, **kwargs))
        except Exception as exc:
            future.set_exception(exc)
        return future


class OperationalMaintenanceTests(unittest.TestCase):
    def setUp(self):
        access.reset()
        self.saved = copy.deepcopy(state.data)
        self.saved_generations = dict(state._kill_generations)
        self.saved_answers = dict(miniapp._answers)
        self.saved_claims = dict(queue._claim_generations)
        self.saved_deliveries = dict(queue._delivery_generations)
        self.temp = tempfile.TemporaryDirectory(prefix="michiflin-operational-")
        root = Path(self.temp.name)
        self.patches = [
            mock.patch.object(config, "OWNER_ONLY", True),
            mock.patch.object(config, "MAINTENANCE_MODE", False),
            mock.patch.object(config, "OWNER_ADDRESS", OWNER),
            mock.patch.object(config, "ADMIN_ADDRESSES", {OWNER}),
            mock.patch.object(config, "ALLOWED_SENDERS", {OWNER}),
            mock.patch.object(config, "MAILBOX_PROFILES_JSON", ""),
            mock.patch.object(config, "JOB_ROOT", root / "jobs"),
            mock.patch.object(state, "CONFIG_DIR", root / "state"),
            mock.patch.object(state, "STATE_FILE", root / "state" / "state.json"),
            mock.patch.object(state, "LEGACY_STATE_FILE", root / "no-legacy.json"),
            mock.patch.object(state, "_save_soon"),
            mock.patch.object(chat, "ready", return_value=False),
            mock.patch.object(progress, "enabled", return_value=False),
        ]
        for patch in self.patches:
            patch.start()
        with state.lock:
            for key in ("jobs", "settings", "flags", "kill_switch", "prefs", "people", "apps", "maintenance_notice", "app_seen", "usage", "health"):
                state.data[key] = {}
            state.data["sent"] = []
            state.data["access"] = {"allowed": [], "blocked": [], "attempts": {}, "alerts": True}
        state._kill_generations.clear()
        miniapp._answers.clear()
        queue._claim_generations.clear()
        queue._delivery_generations.clear()
        queue._stop_flags.clear()
        queue._wake.clear()
        chat.set_sender(OWNER)

    def tearDown(self):
        access.reset()
        with state.lock:
            state.data.clear()
            state.data.update(self.saved)
        state._kill_generations.clear()
        state._kill_generations.update(self.saved_generations)
        miniapp._answers.clear()
        miniapp._answers.update(self.saved_answers)
        queue._claim_generations.clear()
        queue._claim_generations.update(self.saved_claims)
        queue._delivery_generations.clear()
        queue._delivery_generations.update(self.saved_deliveries)
        queue._stop_flags.clear()
        chat.set_sender("")
        for patch in reversed(self.patches):
            patch.stop()
        self.temp.cleanup()

    def share_with(self, *addresses):
        self.assertTrue(access.set_owner_only(False, 60, access.snapshot()["nonce"]))
        for address in addresses:
            self.assertTrue(access.allow_guest(address, access.snapshot()["revision"]))
            self.assertTrue(admin.is_allowed(address))

    def toggle(self, active):
        return maintenance.set_notice(active, "Pausa de prueba", "Test maintenance")

    def new_job(self, address=OWNER, chat_id=7, kind="file", **extra):
        chat.set_sender(address)
        job = queue._new_job(chat_id, kind, "https://example.org/example", "example", 0,
                             name="example.bin", ext="bin", mime="application/octet-stream", **extra)
        state.data["jobs"][job["id"]] = job
        return job

    def test_fast_toggle_invalidates_inflight_work_without_touching_grants(self):
        self.share_with(ALICE)
        before_stamp = access.session_stamp(ALICE)
        before_access = access.revision(ALICE)
        generation = state.kill_generation(ALICE)
        self.assertEqual(generation & ((1 << 128) - 1), before_access)
        with self.assertRaises(state.WorkPaused) as caught:
            with state.guard_work(ALICE, generation):
                self.toggle(True)
                self.toggle(False)
        self.assertTrue(caught.exception.maintenance)
        self.assertFalse(state.maintenance_active())
        self.assertEqual(access.session_stamp(ALICE), before_stamp)
        self.assertTrue(admin.is_allowed(ALICE))
        self.assertTrue(state.work_interrupted(ALICE, generation))
        self.assertNotEqual(state.kill_generation(ALICE), generation)

    def test_queue_pause_resumes_only_current_grants_and_keeps_manual_pauses(self):
        self.share_with(ALICE, BOB)
        alice = self.new_job(ALICE, 11)
        bob = self.new_job(BOB, 12)
        manual = self.new_job(OWNER, 7)
        manual["hold"] = True
        self.toggle(True)
        self.assertIsNone(queue._next_queued())
        self.assertIsNone(queue._deliverable())
        self.assertTrue(manual["hold"])
        self.assertFalse(alice["hold"])
        self.assertFalse(bob["hold"])
        self.assertTrue(access.revoke_guest(BOB))
        self.assertTrue(access.allow_guest(BOB, access.snapshot()["revision"]))
        self.toggle(False)
        self.assertIs(queue._next_queued(), alice)
        self.assertTrue(manual["hold"])
        self.assertFalse(access.job_allowed(bob))  # new grant must not revive the old job
        self.assertTrue(queue._blocked(bob))

    def test_global_maintenance_and_manual_kill_are_independent(self):
        job = self.new_job()
        state.set_kill_switch(OWNER, True)
        self.toggle(True)
        self.toggle(False)
        self.assertTrue(state.get_kill_switch(OWNER))
        self.assertTrue(queue._blocked(job))
        state.set_kill_switch(OWNER, False)
        self.assertFalse(queue._blocked(job))

    def test_restart_restores_active_pause_and_wakes_after_disabling(self):
        job = self.new_job()
        self.toggle(True)
        generation = state.maintenance_generation()
        state.data["maintenance_notice"] = {}
        state.data["jobs"] = {}
        state.load()
        self.assertTrue(state.maintenance_active())
        self.assertEqual(state.maintenance_generation(), generation)
        self.assertIn(job["id"], state.data["jobs"])
        self.assertIsNone(queue._next_queued())
        queue._wake.clear()
        self.toggle(False)
        self.assertTrue(queue._wake.is_set())
        self.assertIsNotNone(queue._next_queued())

    def test_owner_only_and_static_maintenance_remain_stricter(self):
        self.toggle(True)
        self.toggle(False)
        self.assertFalse(access.service_allowed(ALICE))
        self.assertTrue(access.service_allowed(OWNER))
        with mock.patch.object(config, "MAINTENANCE_MODE", True):
            self.assertFalse(access.set_owner_only(False, 60, access.snapshot()["nonce"]))
            self.assertFalse(access.service_allowed(ALICE))
            self.assertTrue(access.service_allowed(OWNER))

    def test_range_download_survives_a_fast_maintenance_toggle(self):
        reached, release = threading.Event(), threading.Event()
        first, second = b"a" * 300, b"b" * 300
        requested = []
        job = self.new_job(resumable=True)
        job["size_est"] = 600

        class Response:
            def __init__(self, resumed):
                self.status_code = 206 if resumed else 200
                self.resumed = resumed
                self.headers = {"ETag": '"fixture-v1"', "Content-Range": "bytes 300-599/600"} if resumed else {"ETag": '"fixture-v1"'}
            def iter_content(self, chunk_size):
                if not self.resumed:
                    yield first
                    reached.set()
                    if not release.wait(4):
                        raise AssertionError("fixture stream not released")
                yield second
            def close(self):
                pass

        def fetch(_url, **kwargs):
            requested.append(kwargs["headers"].get("Range", ""))
            return Response(bool(requested[-1]))

        errors = []
        def prepare():
            try:
                queue._prepare(job)
            except InterruptedError:
                queue._interrupted_preparation(job)
            except Exception as exc:
                errors.append(exc)

        with mock.patch.object(files.net, "fetch", side_effect=fetch):
            worker = threading.Thread(target=prepare)
            worker.start()
            self.assertTrue(reached.wait(4))
            self.toggle(True)
            self.toggle(False)
            release.set()
            worker.join(4)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(job["status"], "queued")
            self.assertEqual(job["progress"], 300)
            self.assertEqual((queue.folder(job) / "src" / "download.bin").read_bytes(), first)
            queue._prepare(job)
        self.assertEqual(requested, ["", "bytes=300-"])
        self.assertEqual(Path(job["part_files"][0]).read_bytes(), first + second)

    def test_http_resume_uses_saved_strong_etag_without_leaking_url(self):
        target = Path(self.temp.name) / "etag.bin"
        url = "https://example.org/file?token=synthetic-test-secret"
        stop = [False]
        seen = []
        class Response:
            def __init__(self, resumed):
                self.status_code = 206 if resumed else 200
                self.headers = {"ETag": '"v1"', "Content-Range": "bytes 3-5/6"} if resumed else {"ETag": '"v1"'}
                self.resumed = resumed
            def iter_content(self, chunk_size):
                if not self.resumed:
                    yield b"abc"
                    stop[0] = True
                yield b"def"
            def close(self):
                pass
        def fetch(_url, **kwargs):
            seen.append(dict(kwargs["headers"]))
            return Response(bool(kwargs["headers"].get("Range")))
        with mock.patch.object(files.net, "fetch", side_effect=fetch):
            with self.assertRaises(InterruptedError):
                files.download(url, target, 6, True, stop=lambda: stop[0])
            stop[0] = False
            files.download(url, target, 6, True, stop=lambda: stop[0])
        self.assertEqual(target.read_bytes(), b"abcdef")
        self.assertEqual(seen[1].get("Range"), "bytes=3-")
        self.assertEqual(seen[1].get("If-Range"), '"v1"')
        self.assertNotIn("synthetic-test-secret", target.with_name(target.name + ".resume.json").read_text())

    def test_http_resume_refuses_changed_source_without_overwriting_partial(self):
        target = Path(self.temp.name) / "changed.bin"
        target.write_bytes(b"abc")
        url = "https://example.org/file"
        files._save_resume_metadata(target, url, '"v1"')
        class Response:
            status_code = 200
            headers = {"ETag": '"v2"'}
            def iter_content(self, chunk_size):
                yield b"UVWXYZ"
            def close(self):
                pass
        with mock.patch.object(files.net, "fetch", return_value=Response()) as fetch:
            with self.assertRaisesRegex(ValueError, "source_changed"):
                files.download(url, target, 6, True)
        self.assertEqual(fetch.call_args.kwargs["headers"].get("If-Range"), '"v1"')
        self.assertEqual(target.read_bytes(), b"abc")

    def test_http_resume_refuses_wrong_content_range_before_writing(self):
        target = Path(self.temp.name) / "range.bin"
        target.write_bytes(b"abc")
        class Response:
            status_code = 206
            headers = {"Content-Range": "bytes 0-2/6"}
            def iter_content(self, chunk_size):
                yield b"XYZ"
            def close(self):
                pass
        with mock.patch.object(files.net, "fetch", return_value=Response()):
            with self.assertRaisesRegex(ValueError, "range_invalid"):
                files.download("https://example.org/file", target, 6, True)
        self.assertEqual(target.read_bytes(), b"abc")

    def test_http_resume_without_strong_validator_is_explicitly_marked_unverified(self):
        target = Path(self.temp.name) / "unverified.bin"
        target.write_bytes(b"abc")  # legacy partial has no saved validator
        warnings = []
        class Response:
            status_code = 206
            headers = {"Content-Range": "bytes 3-5/6"}
            def iter_content(self, chunk_size):
                yield b"def"
            def close(self):
                pass
        with mock.patch.object(files.net, "fetch", return_value=Response()):
            files.download("https://example.org/file", target, 6, True, integrity=warnings.append)
        self.assertEqual(target.read_bytes(), b"abcdef")
        self.assertEqual(warnings, ["range_unverified"])
        self.assertIn("verify", queue.recovery_warning({"range_unverified": True}, "en").lower())
        self.assertIn("verificar", queue.recovery_warning({"range_unverified": True}, "es").lower())

    def test_http_resume_rejects_inconsistent_etag_even_on_206(self):
        target = Path(self.temp.name) / "bad-etag.bin"
        target.write_bytes(b"abc")
        url = "https://example.org/file"
        files._save_resume_metadata(target, url, '"v1"')
        class Response:
            status_code = 206
            headers = {"ETag": '"v2"', "Content-Range": "bytes 3-5/6"}
            def close(self):
                pass
        with mock.patch.object(files.net, "fetch", return_value=Response()):
            with self.assertRaisesRegex(ValueError, "source_changed"):
                files.download(url, target, 6, True)
        self.assertEqual(target.read_bytes(), b"abc")

    def test_http_resume_refuses_symlink_metadata_without_touching_it(self):
        target = Path(self.temp.name) / "unsafe.bin"
        target.write_bytes(b"abc")
        other = Path(self.temp.name) / "external.txt"
        other.write_text("protected")
        target.with_name(target.name + ".resume.json").symlink_to(other)
        with mock.patch.object(files.net, "fetch") as fetch:
            with self.assertRaisesRegex(ValueError, "source_preservation"):
                files.download("https://example.org/file", target, 6, True)
        fetch.assert_not_called()
        self.assertEqual(other.read_text(), "protected")
        self.assertEqual(target.read_bytes(), b"abc")

    def test_nonresumable_restart_preserves_previous_bytes_on_failure(self):
        target = Path(self.temp.name) / "no-range.bin"
        target.write_bytes(b"abc")
        class Response:
            status_code = 403
            headers = {}
            def close(self):
                pass
        with mock.patch.object(files.net, "fetch", return_value=Response()):
            with self.assertRaisesRegex(ValueError, "http_403"):
                files.download("https://example.org/file", target, 6, False)
        saved = list(target.parent.glob("no-range.bin.previous-*"))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].read_bytes(), b"abc")

    def test_incomplete_nonresumable_response_stops_without_destroying_bytes(self):
        target = Path(self.temp.name) / "short.bin"
        target.write_bytes(b"old")
        class Response:
            status_code = 200
            headers = {}  # some servers omit Content-Length
            def iter_content(self, chunk_size):
                yield b"new"
            def close(self):
                pass
        with mock.patch.object(files.net, "fetch", return_value=Response()) as fetch:
            with self.assertRaisesRegex(ValueError, "network"):
                files.download("https://example.org/file", target, 6, False)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(target.read_bytes(), b"new")
        self.assertEqual(list(target.parent.glob("short.bin.previous-*"))[0].read_bytes(), b"old")

    def test_unverified_resume_warning_reaches_both_miniapp_languages(self):
        job = self.new_job(resumable=True, size_est=6)
        target = queue.folder(job) / "src" / "download.bin"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"abc")
        class Response:
            status_code = 206
            headers = {"Content-Range": "bytes 3-5/6"}
            def iter_content(self, chunk_size):
                yield b"def"
            def close(self):
                pass
        with mock.patch.object(files.net, "fetch", return_value=Response()):
            queue._prepare(job)
        self.assertTrue(job.get("range_unverified"))
        self.assertEqual(Path(job["part_files"][0]).read_bytes(), b"abcdef")
        self.assertIn("verificar", miniapp.job_view(job, "es")["warning"])
        self.assertIn("verify", miniapp.job_view(job, "en")["warning"])

    def test_http_416_requires_a_matching_total(self):
        target = Path(self.temp.name) / "range-416.bin"
        target.write_bytes(b"abc")
        class Response:
            status_code = 416
            headers = {"Content-Range": "bytes */6"}
            def close(self):
                pass
        with mock.patch.object(files.net, "fetch", return_value=Response()):
            with self.assertRaisesRegex(ValueError, "range_invalid"):
                files.download("https://example.org/file", target, 0, True)
        self.assertEqual(target.read_bytes(), b"abc")
        Response.headers = {"Content-Range": "bytes */3"}
        with mock.patch.object(files.net, "fetch", return_value=Response()):
            self.assertEqual(files.download("https://example.org/file", target, 0, True), 3)

    def test_completed_file_is_preserved_if_pause_hits_after_packing_move(self):
        payload = b"fully downloaded before packing"
        job = self.new_job(resumable=False)
        original_link = os.link
        toggled = []
        def source_download(_url, target, *_args):
            target.write_bytes(payload)
            return len(payload)
        def link_then_pause(source, destination):
            original_link(source, destination)
            if not toggled:
                toggled.append(True)
                self.toggle(True)
                self.toggle(False)
        with mock.patch.object(files, "download", side_effect=source_download) as download, \
             mock.patch.object(queue.os, "link", side_effect=link_then_pause):
            with self.assertRaises(InterruptedError):
                queue._prepare(job)
            queue._interrupted_preparation(job)
            self.assertEqual(job["progress"], len(payload))
            self.assertEqual((queue.folder(job) / "src" / "download.bin").read_bytes(), payload)
            queue._prepare(job)
        self.assertEqual(download.call_count, 1)
        self.assertEqual(Path(job["part_files"][0]).read_bytes(), payload)

    @unittest.skipUnless(media.available(), "yt-dlp not installed")
    def test_real_ytdlp_resumes_local_media_after_maintenance(self):
        # A local HTTP fixture exercises actual yt-dlp and Range transport without
        # connecting to YouTube or other external services.
        payload = os.urandom(2_000_000)
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_GET(self):
                value = self.headers.get("Range", "")
                start = int(value[6:].split("-")[0]) if value.startswith("bytes=") else 0
                requests.append(value)
                self.send_response(206 if value else 200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(payload) - start))
                self.send_header("Accept-Ranges", "bytes")
                if value:
                    self.send_header("Content-Range", f"bytes {start}-{len(payload) - 1}/{len(payload)}")
                self.end_headers()
                try:
                    for index in range(start, len(payload), 16384):
                        self.wfile.write(payload[index:index + 16384])
                        self.wfile.flush()
                        time.sleep(0.002)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}/fixture.mp4"
        job = self.new_job(kind="video", quality=360)
        job["url"] = url
        interrupted = []
        real_download = media.download
        def download(source, folder, kind, quality, audio, progress_callback, stop):
            def track(done, total):
                progress_callback(done, total)
                if done > 40000 and not interrupted:
                    interrupted.append(True)
                    self.toggle(True)
                    self.toggle(False)
            return real_download(source, folder, kind, quality, audio, track, stop)
        with mock.patch.object(media.net, "validate_public_url", side_effect=lambda value: value), \
             mock.patch.object(media, "supported", return_value=True), \
             mock.patch.object(private_media, "storage_for", return_value=None), \
             mock.patch.object(media, "format_options", return_value={"format": "best"}), \
             mock.patch.object(media, "_routes", side_effect=lambda _url: [""]), \
             mock.patch.object(config, "MIN_FREE_DISK_BYTES", 0), \
             mock.patch.object(media, "download", side_effect=download):
            with self.assertRaises(InterruptedError):
                queue._prepare(job)
            queue._interrupted_preparation(job)
            self.assertGreater(job["progress"], 0)
            self.assertTrue(list((queue.folder(job) / "src").rglob("*.part")))
            queue._prepare(job)
        self.assertTrue(any(value.startswith("bytes=") and not value.startswith("bytes=0-") for value in requests))
        self.assertEqual(Path(job["part_files"][0]).read_bytes(), payload)

    @unittest.skipUnless(media.available(), "yt-dlp not installed")
    def test_different_youtube_clients_keep_incompatible_partials_separate(self):
        output = Path(self.temp.name) / "media-clients"
        seen = []
        class FakeDownloader:
            def __init__(self, options):
                self.options = options
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
            def download(self, _urls):
                attempt = Path(self.options["outtmpl"]).parent
                client = self.options.get("extractor_args", {}).get("youtube", {}).get("player_client", [""])[0]
                seen.append((client, attempt))
                partial = attempt / "media.mp4.part"
                if not client:
                    if not partial.exists():
                        partial.write_bytes(b"first-client")
                    raise ValueError("temporary network error")
                if not partial.exists():
                    partial.write_bytes(b"second-client")
                    raise media.yt_dlp.utils.DownloadCancelled()
                self.assert_partial = partial.read_bytes()
                if self.assert_partial != b"second-client":
                    raise AssertionError("different streams were combined")
                (attempt / "media.mp4").write_bytes(b"finished-second-client")
        with mock.patch.object(media.net, "validate_public_url", side_effect=lambda value: value), \
             mock.patch.object(media, "supported", return_value=True), \
             mock.patch.object(private_media, "storage_for", return_value=None), \
             mock.patch.object(media, "YOUTUBE_CLIENTS", ("", "android")), \
             mock.patch.object(media, "_routes", side_effect=lambda _url: [""]), \
             mock.patch.object(media.yt_dlp, "YoutubeDL", side_effect=FakeDownloader):
            with self.assertRaises(InterruptedError):
                media.download("https://youtube.com/watch?v=fixture", output, "video", 360, "mp4")
            result = media.download("https://youtube.com/watch?v=fixture", output, "video", 360, "mp4")
        self.assertEqual(result.read_bytes(), b"finished-second-client")
        self.assertNotEqual(seen[0][1], seen[1][1])
        self.assertEqual((seen[0][1] / "media.mp4.part").read_bytes(), b"first-client")
        self.assertEqual((seen[1][1] / "media.mp4.part").read_bytes(), b"second-client")

    def test_completed_media_does_not_redownload_after_packing_interruption(self):
        payload = b"synthetic completed media"
        job = self.new_job(kind="video", quality=360)
        original_link = os.link
        toggled = []
        def media_download(_url, work, *_args):
            source = work / "media.mp4"
            source.write_bytes(payload)
            return source
        def link_then_pause(source, destination):
            original_link(source, destination)
            if not toggled:
                toggled.append(True)
                self.toggle(True)
                self.toggle(False)
        with mock.patch.object(media, "download", side_effect=media_download) as download, \
             mock.patch.object(queue.os, "link", side_effect=link_then_pause):
            with self.assertRaises(InterruptedError):
                queue._prepare(job)
            queue._interrupted_preparation(job)
            self.assertEqual((queue.folder(job) / "src" / "media.mp4").read_bytes(), payload)
            queue._prepare(job)
        self.assertEqual(download.call_count, 1)
        self.assertEqual(Path(job["part_files"][0]).read_bytes(), payload)

    def test_copy_fallback_is_cooperatively_interrupted_and_source_is_safe(self):
        payload = b"a" * (3 * 1024 * 1024)
        job = self.new_job()
        source = queue.folder(job) / "src" / "download.bin"
        source.parent.mkdir(parents=True)
        source.write_bytes(payload)
        job.update(source_complete=source.name, source_complete_size=len(payload), status="packing", size=len(payload))
        checks = []
        def stop():
            checks.append(1)
            return len(checks) >= 4
        with mock.patch.object(queue.os, "link", side_effect=OSError("fixture")):
            with self.assertRaises(InterruptedError):
                queue._split(source, job, stop)
        self.assertEqual(source.read_bytes(), payload)
        queue._interrupted_preparation(job)
        self.assertEqual(job["progress"], len(payload))
        with mock.patch.object(files, "download") as download:
            queue._prepare(job)
        download.assert_not_called()
        self.assertEqual(Path(job["part_files"][0]).read_bytes(), payload)

    def test_marked_missing_source_is_recovered_from_named_before_cleanup(self):
        payload = b"marked complete source in named"
        job = self.new_job()
        job.update(status="packing", size=len(payload), progress=len(payload),
                   source_complete="download.bin", source_complete_size=len(payload))
        named = queue.folder(job) / "named" / job["name"]
        named.parent.mkdir(parents=True)
        named.write_bytes(payload)
        queue.recover()
        self.assertFalse(job["hold"])
        self.assertEqual(job["progress"], len(payload))
        self.assertEqual((queue.folder(job) / "src" / job["source_complete"]).read_bytes(), payload)
        with mock.patch.object(files, "download") as download:
            queue._prepare(job)
        download.assert_not_called()
        self.assertEqual(Path(job["part_files"][0]).read_bytes(), payload)

    def test_marked_missing_source_is_recovered_from_whole_part(self):
        payload = b"marked complete source in a part"
        job = self.new_job()
        job.update(status="packing", size=len(payload), progress=len(payload),
                   source_complete="download.bin", source_complete_size=len(payload))
        part = queue.folder(job) / "parts" / job["name"]
        part.parent.mkdir(parents=True)
        part.write_bytes(payload)
        queue.recover()
        self.assertFalse(job["hold"])
        self.assertEqual((queue.folder(job) / "src" / job["source_complete"]).read_bytes(), payload)
        with mock.patch.object(files, "download") as download:
            queue._prepare(job)
        download.assert_not_called()
        self.assertEqual(Path(job["part_files"][0]).read_bytes(), payload)

    def test_marked_wrong_size_source_is_held_without_overwriting_any_copy(self):
        job = self.new_job()
        job.update(status="packing", size=6, progress=6, source_complete="download.bin", source_complete_size=6)
        source = queue.folder(job) / "src" / "download.bin"
        named = queue.folder(job) / "named" / job["name"]
        source.parent.mkdir(parents=True)
        named.parent.mkdir(parents=True)
        source.write_bytes(b"partial")
        named.write_bytes(b"full!!")
        queue.recover()
        self.assertTrue(job["hold"])
        self.assertEqual(job["error"], "source_preservation")
        self.assertEqual(source.read_bytes(), b"partial")
        self.assertEqual(named.read_bytes(), b"full!!")
        self.assertIsNone(queue._next_queued())

    def test_same_size_corrupt_complete_source_is_held_before_deleting_artifacts(self):
        good, corrupt = b"correct!", b"damaged!"
        job = self.new_job()
        job.update(status="queued", size=len(good), progress=len(good), source_complete="download.bin",
                   source_complete_size=len(good), source_complete_sha=hashlib.sha256(good).hexdigest())
        source = queue.folder(job) / "src" / "download.bin"
        named = queue.folder(job) / "named" / job["name"]
        part = queue.folder(job) / "parts" / job["name"]
        for path in (source, named, part):
            path.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(corrupt)
        named.write_bytes(good)
        part.write_bytes(good)
        with mock.patch.object(files, "download") as download:
            queue._prepare(job)
        download.assert_not_called()
        self.assertTrue(job["hold"])
        self.assertEqual(job["error"], "source_preservation")
        self.assertEqual(source.read_bytes(), corrupt)
        self.assertEqual(named.read_bytes(), good)
        self.assertEqual(part.read_bytes(), good)

    def test_interruption_does_not_remove_last_packing_artifacts(self):
        job = self.new_job(resumable=True)
        job.update(status="downloading", progress=10)
        named = queue.folder(job) / "named" / "source.bin"
        part = queue.folder(job) / "parts" / "piece.001"
        for path in (named, part):
            path.parent.mkdir(parents=True, exist_ok=True)
        named.write_bytes(b"valuable source")
        part.write_bytes(b"valuable part")
        queue._interrupted_preparation(job)
        self.assertEqual(named.read_bytes(), b"valuable source")
        self.assertEqual(part.read_bytes(), b"valuable part")
        self.assertEqual(job["progress"], 0)
        self.assertEqual(job["recovery_note"], "progress_lost")

    def test_marked_missing_source_with_ambiguous_artifacts_is_held(self):
        job = self.new_job()
        job.update(status="packing", size=4, progress=4, source_complete="download.bin", source_complete_size=4)
        named = queue.folder(job) / "named"
        named.mkdir(parents=True)
        (named / "one.bin").write_bytes(b"aaaa")
        (named / "two.bin").write_bytes(b"bbbb")
        queue.recover()
        self.assertTrue(job["hold"])
        self.assertEqual(job["error"], "source_preservation")
        self.assertEqual((named / "one.bin").read_bytes(), b"aaaa")
        self.assertEqual((named / "two.bin").read_bytes(), b"bbbb")

    def test_complete_and_partial_named_artifacts_are_both_protected(self):
        job = self.new_job()
        job.update(status="packing", size=4, progress=4, source_complete="download.bin", source_complete_size=4)
        named = queue.folder(job) / "named"
        named.mkdir(parents=True)
        (named / "complete.bin").write_bytes(b"aaaa")
        (named / "partial.bin").write_bytes(b"bb")
        queue.recover()
        self.assertTrue(job["hold"])
        self.assertEqual(job["error"], "source_preservation")
        self.assertEqual((named / "complete.bin").read_bytes(), b"aaaa")
        self.assertEqual((named / "partial.bin").read_bytes(), b"bb")

    def test_marked_missing_media_source_finds_result_name_without_redownload(self):
        payload = b"result media content"
        job = self.new_job(kind="video", quality=360)
        job.update(status="packing", size=len(payload), progress=len(payload),
                   source_complete="media-missing.mp4", source_complete_size=len(payload))
        result = queue.folder(job) / "src" / "media-result-0123456789abcdef.mp4"
        result.parent.mkdir(parents=True)
        result.write_bytes(payload)
        queue.recover()
        self.assertFalse(job["hold"])
        self.assertEqual(job["source_complete"], result.name)
        with mock.patch.object(media, "download") as download:
            queue._prepare(job)
        download.assert_not_called()
        self.assertEqual(Path(job["part_files"][0]).read_bytes(), payload)

    def test_recover_reports_actual_partial_bytes_and_warns_on_progress_loss(self):
        job = self.new_job(resumable=True)
        job.update(status="downloading", progress=500, size_est=1000)
        partial = queue.folder(job) / "src" / "download.bin"
        partial.parent.mkdir(parents=True)
        partial.write_bytes(b"x" * 300)
        queue.recover()
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["progress"], 300)
        self.assertEqual(job["recovery_note"], "progress_lost")
        self.assertIn("progreso", miniapp.job_view(job, "es")["warning"])
        self.assertIn("progress", miniapp.job_view(job, "en")["warning"])

    def test_recover_missing_bytes_does_not_claim_progress_survived(self):
        job = self.new_job(resumable=True)
        job.update(status="downloading", progress=500, size_est=1000,
                   source_complete="download.bin", source_complete_size=500)
        queue.recover()
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["progress"], 0)
        self.assertFalse(job.get("source_complete"))
        self.assertEqual(job["recovery_note"], "progress_lost")
        self.assertIn("progress", miniapp.job_view(job, "en")["warning"])

    def test_prepare_reports_bytes_lost_after_startup(self):
        job = self.new_job(resumable=True)
        job.update(status="queued", progress=500, size_est=1000,
                   source_complete="download.bin", source_complete_size=500)
        with mock.patch.object(files, "download", side_effect=ValueError("network")):
            with self.assertRaisesRegex(ValueError, "network"):
                queue._prepare(job)
        self.assertEqual(job["progress"], 0)
        self.assertFalse(job.get("source_complete"))
        self.assertEqual(job["recovery_note"], "progress_lost")

    def test_source_hash_is_committed_before_packing_can_be_interrupted(self):
        payload = b"verified complete source"
        job = self.new_job()
        def download(_url, target, *_args):
            target.write_bytes(payload)
        with mock.patch.object(files, "download", side_effect=download), \
             mock.patch.object(queue, "_split", side_effect=InterruptedError()):
            with self.assertRaises(InterruptedError):
                queue._prepare(job)
        self.assertEqual(job["status"], "packing")
        self.assertEqual(job["source_complete_sha"], hashlib.sha256(payload).hexdigest())
        self.assertEqual((queue.folder(job) / "src" / job["source_complete"]).read_bytes(), payload)

    def test_prepare_refuses_a_symlinked_source_directory(self):
        job = self.new_job()
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "valuable").write_bytes(b"keep")
        root = queue.folder(job)
        root.mkdir(parents=True)
        (root / "src").symlink_to(outside, target_is_directory=True)
        with mock.patch.object(files, "download") as download:
            queue._prepare(job)
        download.assert_not_called()
        self.assertTrue(job["hold"])
        self.assertEqual(job["error"], "source_preservation")
        self.assertEqual((outside / "valuable").read_bytes(), b"keep")

    def test_prepare_refuses_a_symlinked_partial_file(self):
        job = self.new_job(resumable=True)
        outside = Path(self.temp.name) / "valuable"
        outside.write_bytes(b"keep")
        work = queue.folder(job) / "src"
        work.mkdir(parents=True)
        (work / "download.bin").symlink_to(outside)
        with mock.patch.object(files, "download") as download:
            queue._prepare(job)
        download.assert_not_called()
        self.assertTrue(job["hold"])
        self.assertEqual(job["error"], "source_preservation")
        self.assertEqual(outside.read_bytes(), b"keep")

    def test_prepare_rechecks_missing_source_before_deleting_parts(self):
        job = self.new_job()
        job.update(status="queued", size=4, progress=4, source_complete="download.bin", source_complete_size=4)
        parts = queue.folder(job) / "parts"
        parts.mkdir(parents=True)
        (parts / "incomplete.001").write_bytes(b"ab")
        with mock.patch.object(files, "download") as download:
            queue._prepare(job)
        download.assert_not_called()
        self.assertTrue(job["hold"])
        self.assertEqual(job["error"], "source_preservation")
        self.assertEqual((parts / "incomplete.001").read_bytes(), b"ab")

    def test_old_packing_source_in_named_is_preserved_without_redownload(self):
        payload = b"original from old version"
        job = self.new_job()
        job.update(status="packing", size=len(payload), progress=len(payload))
        named = queue.folder(job) / "named" / job["name"]
        named.parent.mkdir(parents=True)
        named.write_bytes(payload)
        queue.recover()
        self.assertEqual(job["source_complete"], "download.bin")
        self.assertEqual(job["status"], "queued")
        self.assertEqual((queue.folder(job) / "src" / "download.bin").read_bytes(), payload)
        self.assertEqual(named.read_bytes(), payload)
        with mock.patch.object(files, "download") as download:
            queue._prepare(job)
        download.assert_not_called()
        self.assertEqual(Path(job["part_files"][0]).read_bytes(), payload)

    def test_old_single_complete_part_is_preserved_before_clearing_parts(self):
        payload = b"single original in parts"
        job = self.new_job()
        job.update(status="packing", size=len(payload), progress=len(payload))
        part = queue.folder(job) / "parts" / job["name"]
        part.parent.mkdir(parents=True)
        part.write_bytes(payload)
        queue.recover()
        self.assertEqual(job["source_complete"], "download.bin")
        with mock.patch.object(files, "download") as download:
            queue._prepare(job)
        download.assert_not_called()
        self.assertEqual(Path(job["part_files"][0]).read_bytes(), payload)

    def test_ambiguous_old_packing_files_are_held_and_never_overwritten(self):
        job = self.new_job()
        job.update(status="packing", size=4, progress=4)
        named = queue.folder(job) / "named"
        named.mkdir(parents=True)
        (named / "one.bin").write_bytes(b"aaaa")
        (named / "two.bin").write_bytes(b"bbbb")
        queue.recover()
        self.assertTrue(job["hold"])
        self.assertEqual(job["error"], "source_preservation")
        self.assertIsNone(queue._next_queued())
        self.assertIn("protected", queue.hold(job["id"], False, "en"))
        self.assertIn("review", miniapp.job_view(job, "en")["error"])
        self.assertEqual((named / "one.bin").read_bytes(), b"aaaa")
        self.assertEqual((named / "two.bin").read_bytes(), b"bbbb")

    def test_legacy_hardlink_failure_preserves_original_and_holds_job(self):
        job = self.new_job()
        job.update(status="packing", size=4, progress=4)
        named = queue.folder(job) / "named" / job["name"]
        named.parent.mkdir(parents=True)
        named.write_bytes(b"safe")
        with mock.patch.object(queue.os, "link", side_effect=OSError("fixture")):
            queue.recover()
        self.assertTrue(job["hold"])
        self.assertEqual(job["error"], "source_preservation")
        self.assertEqual(named.read_bytes(), b"safe")

    def test_handed_off_message_is_reconciled_not_resent_after_pause(self):
        job = self.new_job()
        part = Path(self.temp.name) / "part.bin"
        part.write_bytes(b"abc")
        job.update(status="sending", parts=1, part_files=[str(part)], todo=[0], sent=0,
                   delivery_delay=int(state.setting("delay")), delivery_batch=0, delivery_daily_cap_mb=0)
        def wait_first(_message, _timeout, stop):
            self.toggle(True)
            self.toggle(False)
            self.assertTrue(stop.is_set())
            return "stopped"
        with mock.patch.object(chat, "send", return_value=77) as send, \
             mock.patch.object(chat, "wait_delivery", side_effect=[wait_first, "delivered"]) as wait:
            # unittest's side_effect list returns a callable as a value; use a dispatcher instead.
            calls = iter([wait_first, lambda *_: "delivered"])
            wait.side_effect = lambda *args: next(calls)(*args)
            queue._send_one(job)
            self.assertEqual(job["inflight"]["msg_id"], 77)
            self.assertEqual(job["todo"], [0])
            queue._send_one(job)
        self.assertEqual(send.call_count, 1)
        self.assertEqual([call.args[0] for call in wait.call_args_list], [77, 77])
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["todo"], [])

    def test_authorized_new_and_existing_contacts_are_told_when_they_try(self):
        self.share_with(ALICE)
        self.toggle(True)
        self.assertTrue(access.allow_guest(NEW, access.snapshot()["revision"]))
        contacts = {11: ALICE, 12: NEW, 13: "unknown@example.org"}
        bot = SimpleNamespace(rpc=SimpleNamespace(
            get_basic_chat_info=lambda *_: SimpleNamespace(chat_type="Single", is_contact_request=False),
            get_contact=lambda _acc, contact: SimpleNamespace(address=contacts[contact])))
        def message(contact):
            return SimpleNamespace(id=100 + contact, chat_id=10 + contact, from_id=contact, is_info=False,
                                   text="hello", file=None, file_name="")
        sent = []
        with mock.patch.object(chat, "text", side_effect=lambda *args: sent.append(args)), \
             mock.patch.object(app, "_refuse") as refuse:
            self.assertIsNone(app._screen(bot, 1, message(11)))
            self.assertIsNone(app._screen(bot, 1, message(12)))
            self.assertIsNone(app._screen(bot, 1, message(12)))
            self.assertIsNone(app._screen(bot, 1, message(13)))
        self.assertEqual(len(sent), 3)
        self.assertTrue(all("maintenance" in item[1].lower() or "mantenimiento" in item[1].lower() for item in sent))
        refuse.assert_called_once()
        self.assertTrue(admin.is_allowed(NEW))

    def test_miniapp_refuses_work_bilingually_but_owner_can_disable(self):
        self.toggle(True)
        entry = {"chat": 7, "addr": OWNER, "build": miniapp.app_build()}
        for locale, word in (("es", "mantenimiento"), ("en", "maintenance")):
            with self.subTest(locale=locale), mock.patch.object(miniapp, "respond") as reply, \
                 mock.patch.object(miniapp._workers, "submit") as submit:
                miniapp.handle(1, entry, {"id": "query", "op": "ask", "lang": locale, "args": {"text": "hello"}})
                submit.assert_not_called()
                self.assertIn(word, reply.call_args.args[3].lower())
        chat.set_sender(OWNER)
        with mock.patch.object(maintenance, "sync_profile", return_value=False), \
             mock.patch.object(miniapp, "_notify_maintenance"):
            result = miniapp.op_maintenance_notice(7, {"active": False, "es": "Pausa de prueba", "en": "Test maintenance"}, "en")
        self.assertFalse(result["notice"]["active"])
        self.assertFalse(state.maintenance_active())

    def test_private_queue_ids_cannot_control_another_authorized_user(self):
        self.share_with(ALICE, BOB)
        alice = self.new_job(ALICE, 11)
        bob = self.new_job(BOB, 12)
        chat.set_sender(ALICE)
        self.assertIs(queue.get(alice["id"]), alice)
        self.assertIsNone(queue.get(bob["id"]))
        for action in (lambda: queue.cancel(bob["id"], "en"),
                       lambda: queue.hold(bob["id"], True, "en"),
                       lambda: queue.resend(bob["id"], 1, "en"),
                       lambda: queue.schedule(bob["id"], "23:30", "en")):
            action()
        self.assertEqual(bob["status"], "queued")
        self.assertFalse(bob["hold"])
        chat.set_sender(BOB)
        self.assertIs(queue.get(bob["id"]), bob)
        self.assertIsNone(queue.get(alice["id"]))
        chat.set_sender(OWNER)
        self.assertIsNone(queue.get(alice["id"]))

    def test_free_agent_never_calls_shared_image_generator(self):
        chat.set_sender(OWNER)
        for prompt, locale in (("dibuja un gato", "es"), ("draw a cat", "en")):
            with self.subTest(prompt=prompt), mock.patch.object(images, "generate") as generate, \
                 mock.patch.object(agent.chat, "text") as text:
                agent.handle(7, prompt, locale)
                generate.assert_not_called()
                self.assertIn("fuente" if locale == "es" else "source", text.call_args.args[1].lower())

    def test_large_result_is_cached_and_reassembled_on_same_id_retry(self):
        payload = base64.b64encode(os.urandom(650_000)).decode()
        entry = {"chat": 7, "addr": OWNER}
        rid = "large-answer"
        self.assertEqual(miniapp._admit(rid, 0, 1), "new")
        sent = []
        with mock.patch.object(chat, "send_update", side_effect=lambda _instance, value, *_args: sent.append(value) or True):
            miniapp.respond(1, rid, {"b64": payload})
            first = [part for part in sent if part.get("t") == "chunk"]
            self.assertGreater(len(first), 25)
            self.assertEqual(json.loads("".join(part["s"] for part in first))["data"]["b64"], payload)
            sent.clear()
            miniapp._route(1, entry, {"id": rid, "op": "image", "retry": 1, "lang": "en", "session": miniapp.BOOT_SESSION})
            second = [part for part in sent if part.get("t") == "chunk"]
        self.assertEqual(len(first), len(second))
        self.assertEqual(json.loads("".join(part["s"] for part in second))["data"]["b64"], payload)

    def test_cache_eviction_reports_loss_instead_of_silent_retry(self):
        entry = {"chat": 7, "addr": OWNER}
        with mock.patch.object(miniapp, "ANSWER_TOTAL_BYTES", 450), \
             mock.patch.object(miniapp, "ANSWER_ITEM_BYTES", 400), \
             mock.patch.object(chat, "send_update", return_value=True):
            miniapp._admit("first", 0, 1)
            miniapp.respond(1, "first", {"text": "x" * 220})
            miniapp._admit("second", 0, 1)
            miniapp.respond(1, "second", {"text": "y" * 220})
            self.assertIsNone(miniapp._answers["first"]["body"])
            with mock.patch.object(miniapp, "respond") as response:
                miniapp._route(1, entry, {"id": "first", "op": "image", "retry": 1,
                                           "lang": "en", "session": miniapp.BOOT_SESSION})
            self.assertIn("no longer available", response.call_args.args[3])
            self.assertIn("may have charged", response.call_args.args[3])

    def test_old_backup_unseen_billable_requests_cannot_run_after_restart(self):
        entry = {"chat": 7, "addr": OWNER}
        for index, op in enumerate(("ask", "image", "code", "command", "action")):
            with self.subTest(op=op), mock.patch.object(miniapp, "respond") as reply, \
                 mock.patch.object(miniapp._pool, "submit") as submit:
                miniapp._route(1, entry, {"id": "old" + str(index), "op": op, "lang": "en", "session": "old-process"})
                submit.assert_not_called()
                self.assertEqual(reply.call_args.kwargs["code"], "session_changed")
                self.assertIn("charges", reply.call_args.args[3])
        self.assertEqual(len(state.data["app_seen"]), 5)  # none was present in the old backup

    def test_old_backup_cannot_replay_unseen_mutations_or_destructive_controls(self):
        entry = {"chat": 7, "addr": OWNER}
        mutations = (("notas", {"do": "clear"}), ("enlaces", {"do": "delete", "id": 1}),
                     ("settings", {"reset": True}), ("queue", {"do": "cancel", "id": "1"}),
                     ("personal_ai", {"do": "remove", "provider": "example"}),
                     ("browser_session", {"do": "disconnect"}), ("admin", {"do": "purge"}),
                     ("maintenance_notice", {"active": False}), ("kill_switch", {"value": False}),
                     ("owner_only", {"enabled": True}), ("reinstall", {}))
        for index, (op, args) in enumerate(mutations):
            with self.subTest(op=op), mock.patch.object(miniapp, "respond") as reply, \
                 mock.patch.object(miniapp._pool, "submit") as work, mock.patch.object(miniapp._control_pool, "submit") as control:
                miniapp._route(1, entry, {"id": "mutation" + str(index), "op": op, "args": args,
                                           "lang": "en", "session": "old-process"})
                work.assert_not_called()
                control.assert_not_called()
                self.assertEqual(reply.call_args.kwargs["code"], "session_changed")
        self.assertFalse(miniapp._needs_session("hello", {}))
        self.assertFalse(miniapp._needs_session("queue", {}))
        self.assertFalse(miniapp._needs_session("notas", {"do": "list"}))
        self.assertFalse(miniapp._needs_session("personal_ai", {"do": "status"}))
        self.assertTrue(miniapp._needs_session("personal_ai", {"do": "remove"}))

    def test_background_health_never_spends_configured_api_keys(self):
        key_model = ai.Model("paid", "model", {"chat"}, 100)
        key_provider = ai.Provider("paid", "Paid fixture", "openai", "https://example.org", "synthetic-key", models=[key_model])
        with mock.patch.object(ai, "providers", return_value=[key_provider]), \
             mock.patch.object(ai, "_call") as call, mock.patch.object(ai.time, "sleep"):
            ai.health_check(0)
        call.assert_not_called()

    def test_health_resumes_after_pause_without_repeating_successful_probes(self):
        models = [ai.Model("ovh", "first", {"chat"}, 0), ai.Model("ovh", "second", {"chat"}, 0)]
        provider = ai.Provider("ovh", "OVH", "openai", "https://example.org", models=models)
        calls = []
        def probe(_provider, model, *_args):
            calls.append(model.model)
            if model.model == "second" and calls.count("second") == 1:
                self.toggle(True)
                self.toggle(False)
            return ai.AIResult(text="OK")
        with mock.patch.object(ai, "providers", return_value=[provider]), \
             mock.patch.object(ai, "available", return_value=True), \
             mock.patch.object(ai, "_call", side_effect=probe), mock.patch.object(ai.time, "sleep"):
            ai.health_check(0)
            self.assertNotEqual(state.flag("health"), state.utc_day())
            self.assertEqual(ai.model_status(models[0].key), "ok")
            ai.health_check(0)
        self.assertEqual(calls, ["first", "second", "second"])
        self.assertEqual(state.flag("health"), state.utc_day())
        self.assertEqual(ai.model_status(models[1].key), "ok")

    def test_discovery_stops_after_fast_pause_instead_of_starting_next_provider(self):
        called = []
        def first():
            called.append("first")
            self.toggle(True)
            self.toggle(False)
        with mock.patch.object(ai, "_discover_openrouter", side_effect=first), \
             mock.patch.object(ai, "_discover_groq", side_effect=lambda: called.append("second")):
            ai.discover()
        self.assertEqual(called, ["first"])

    def test_owner_text_control_runs_immediately_during_maintenance(self):
        self.toggle(True)
        bot = SimpleNamespace(rpc=SimpleNamespace(
            get_basic_chat_info=lambda *_: SimpleNamespace(chat_type="Single", is_contact_request=False),
            get_contact=lambda *_: SimpleNamespace(address=OWNER)))
        message = SimpleNamespace(id=330, chat_id=7, from_id=11, is_info=False,
                                  text="/maintenance no", file=None, file_name="")
        notified = threading.Event()
        with mock.patch.object(chat, "text") as sent, \
             mock.patch.object(maintenance, "sync_profile", return_value=False), \
             mock.patch.object(miniapp, "_notify_maintenance", side_effect=lambda _notice: notified.set()):
            self.assertIsNone(app._screen(bot, 1, message))
            self.assertTrue(notified.wait(2))
        self.assertFalse(state.maintenance_active())
        self.assertIn("disabled", sent.call_args.args[1].lower())

    def test_current_private_chat_without_app_receives_notice(self):
        self.share_with(ALICE, BOB)
        self.assertTrue(access.revoke_guest(BOB))
        state.data["access"]["allowed"] = ["historic@example.org"]
        state.data["apps"] = {"1": {"chat": 7, "addr": OWNER, "locale": "en"}}
        notice = self.toggle(True)
        by_address = {ALICE: 42, BOB: 43, "historic@example.org": 44}
        contacts = {42: ALICE, 43: BOB, 44: "historic@example.org"}
        rpc = SimpleNamespace(lookup_contact_id_by_addr=lambda _acc, address: by_address.get(address, 0),
                              get_chat_id_by_contact_id=lambda _acc, contact: contact + 100,
                              get_basic_chat_info=lambda *_: SimpleNamespace(chat_type="Single", is_contact_request=False,
                                                                             is_self_talk=False),
                              get_contact=lambda _acc, contact: SimpleNamespace(address=contacts[contact]))
        def send(_chat_id, _message, *, interrupted):
            self.assertFalse(interrupted())
            return 123
        with mock.patch.object(chat, "ready", return_value=True), mock.patch.object(chat, "rpc", return_value=rpc), \
             mock.patch.object(chat, "account", return_value=1), mock.patch.object(chat, "send", side_effect=send) as direct, \
             mock.patch.object(miniapp, "push", return_value=True) as push:
            miniapp._notify_maintenance(notice)
        self.assertEqual(direct.call_count, 1)
        self.assertEqual(direct.call_args.args[0], 142)
        self.assertIn("mantenimiento", direct.call_args.args[1].lower())
        self.assertEqual(push.call_args.args[0], 7)
        self.assertNotIn("143", state.data["maintenance_notice"]["notified"])
        self.assertNotIn("144", state.data["maintenance_notice"]["notified"])

    def test_live_browser_is_denied_and_existing_session_is_stopped(self):
        self.toggle(True)
        entry = {"addr": OWNER, "chat": 7, "build": "test-build"}
        rpc = mock.Mock()
        session = SimpleNamespace(instance=7, sid="a" * 32, locale="en", stop=mock.Mock())
        delivered = threading.Event()
        with mock.patch.dict(live_web._sessions, {7: session}, clear=True), \
             mock.patch.object(live_web, "_deny", side_effect=lambda *_: delivered.set()):
            live_web.pause_for_maintenance()
            session.stop.assert_called_once()
            self.assertTrue(delivered.wait(2))
        payload = {"v": 1, "sid": "b" * 32, "t": "start", "url": "https://example.org/", "lang": "en"}
        with mock.patch.object(live_web, "_identity", return_value=(entry, OWNER)), \
             mock.patch.object(miniapp, "app_build", return_value="test-build"), \
             mock.patch.object(chat, "rpc", return_value=rpc), mock.patch.object(chat, "account", return_value=1), \
             mock.patch.object(live_web.Session, "start") as start:
            live_web.receive(7, list(json.dumps(payload).encode()))
        message = json.loads(bytes(rpc.send_webxdc_realtime_data.call_args.args[2]))
        self.assertEqual(message["error"], "maintenance")
        start.assert_not_called()

    def test_running_app_result_is_suppressed_after_fast_pause(self):
        reached, release = threading.Event(), threading.Event()
        calls, replies = [], []
        entry = {"chat": 7, "addr": OWNER, "build": miniapp.app_build()}
        def slow(_chat, _args, _locale):
            calls.append(1)
            reached.set()
            if not release.wait(4):
                raise AssertionError("operation not released")
            return {"text": "result from an external request"}
        with mock.patch.dict(miniapp.OPS, {"synthetic_slow": slow}), \
             mock.patch.object(miniapp, "respond", side_effect=lambda *args: replies.append(args)):
            worker = threading.Thread(target=miniapp.handle, args=(1, entry, {"id": "inflight", "op": "synthetic_slow", "lang": "en"}))
            worker.start()
            self.assertTrue(reached.wait(3))
            self.toggle(True)
            self.toggle(False)
            release.set()
            worker.join(4)
            self.assertFalse(worker.is_alive())
        self.assertEqual(calls, [1])
        self.assertIsNone(replies[0][2])
        self.assertIn("maintenance", replies[0][3].lower())
        self.assertIn("charges", replies[0][3].lower())

    def test_dedupe_limit_refuses_instead_of_forgetting_recent_requests(self):
        entry = {"chat": 7, "addr": OWNER}
        with mock.patch.object(miniapp, "SEEN_LIMIT", 2):
            self.assertEqual(miniapp._admit("one", 0, 1), "new")
            self.assertEqual(miniapp._admit("two", 0, 1), "new")
            with mock.patch.object(miniapp, "respond") as reply, mock.patch.object(miniapp._pool, "submit") as submit:
                miniapp._route(1, entry, {"id": "three", "op": "image", "lang": "en", "session": miniapp.BOOT_SESSION})
            submit.assert_not_called()
            self.assertIn("too many", reply.call_args.args[3].lower())
            self.assertEqual(set(state.data["app_seen"]), {"one", "two"})

    def test_stale_model_health_is_not_shown_as_current(self):
        state.data["health"] = {"model": {"status": "ok", "at": time.time() - 90000}}
        self.assertEqual(ai.model_status("model"), "")
        state.data["health"]["model"]["at"] = time.time()
        self.assertEqual(ai.model_status("model"), "ok")


if __name__ == "__main__":
    unittest.main()
