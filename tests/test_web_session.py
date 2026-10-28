"""Security and functional checks for the per-user browser, with no social accounts.

The optional sandbox integration uses a local TLS fixture and a test-only gateway
mapping. It never changes production network policy or disables certificate checks
outside the disposable test worker.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import http.server
import io
import json
import os
import select
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from michiflin import (access, ai, chat, coder, config, egress, live_web, media, media_worker, miniapp,
                       private_media, queue, remote_worker, sandbox, smart_agent, state, web_vault)

ALICE = "alice@example.org"
BOB = "bob@example.org"


def storage(value: str, host: str = "fixture.example") -> dict:
    return {"cookies": [{"name": "demo_session", "value": value, "domain": host, "path": "/", "expires": -1,
                         "httpOnly": True, "secure": True, "sameSite": "Lax"}] if value else [], "origins": []}


class VaultAndBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = mock.patch.object(config, "CONFIG_DIR", Path(self.temp.name) / "state")
        self.directory.start()
        self.tmpdir = mock.patch.object(config, "TEMP_ROOT", Path(self.temp.name) / "tmp")
        self.tmpdir.start()
        self.environment = mock.patch.dict(os.environ, {"WEB_SESSION_KEY": "fixture-only-key-" * 4})
        self.environment.start()
        state.act_as("")

    def tearDown(self):
        state.act_as("")
        self.environment.stop()
        self.tmpdir.stop()
        self.directory.stop()
        self.temp.cleanup()

    def test_website_mapping_rejects_spoofing_and_unsafe_urls(self):
        self.assertEqual(web_vault.site_of("https://m.youtube.com/watch?v=1"), "youtube")
        self.assertEqual(web_vault.site_of("https://youtu.be/123"), "youtube")
        self.assertEqual(web_vault.site_of("https://youtube.com.evil.example/"), "host:youtube.com.evil.example")
        for url in ("http://youtube.com", "https://alice:pass@example.org/", "https://example.org:444/", "https://bad..host/"):
            with self.subTest(url=url), self.assertRaises(web_vault.VaultError):
                web_vault.site_of(url)

    def test_user_and_site_vaults_are_encrypted_separate_and_bound(self):
        secret_a, secret_b = storage("alice-secret"), storage("bob-secret")
        web_vault.save(ALICE, "youtube", secret_a)
        web_vault.save(BOB, "youtube", secret_b)
        web_vault.save(ALICE, "facebook", storage("other-site"))
        self.assertEqual(web_vault.load(ALICE, "youtube"), secret_a)
        self.assertEqual(web_vault.load(BOB, "youtube"), secret_b)
        a, b = web_vault._path(ALICE, "youtube"), web_vault._path(BOB, "youtube")
        self.assertNotEqual(a, b)
        self.assertEqual(a.stat().st_mode & 0o777, 0o600)
        for path in (a, b, web_vault._path(ALICE, "facebook")):
            self.assertNotIn(b"secret", path.read_bytes())
            self.assertNotIn(b"alice", str(path).encode())
        b.write_bytes(a.read_bytes())
        with self.assertRaises(web_vault.VaultError):
            web_vault.load(BOB, "youtube")
        with mock.patch.dict(os.environ, {"WEB_SESSION_KEY": "another-test-key-" * 4}):
            self.assertIsNone(web_vault.load(ALICE, "youtube"))  # filenames also depend on the key

    def test_removal_and_sender_context_do_not_cross_accounts(self):
        web_vault.save(ALICE, "youtube", storage("one"))
        web_vault.save(BOB, "youtube", storage("two"))
        self.assertIsNone(private_media.storage_for("https://youtube.com/watch?v=test"))
        with state.person(ALICE):
            self.assertEqual(private_media.storage_for("https://youtube.com/watch?v=test"), storage("one"))
            with state.person(BOB):
                self.assertEqual(private_media.storage_for("https://youtu.be/test"), storage("two"))
            self.assertEqual(state.request_sender(), ALICE)
        self.assertEqual(state.request_sender(), "")
        self.assertTrue(web_vault.remove(ALICE, "youtube"))
        self.assertIsNone(web_vault.load(ALICE, "youtube"))
        self.assertIsNotNone(web_vault.load(BOB, "youtube"))

    def test_cookie_conversion_filters_injection_and_retains_secure_cookie(self):
        jar = Path(self.temp.name) / "cookies.txt"
        item = storage("test-cookie")
        item["cookies"].append({"name": "bad\nname", "value": "oops", "domain": "fixture.example", "path": "/"})
        self.assertEqual(media_worker.cookies_from(item, str(jar)), 1)
        text = jar.read_text()
        self.assertIn("fixture.example\tFALSE\t/\tTRUE", text)
        self.assertIn("test-cookie", text)
        self.assertNotIn("oops", text)

    def _authorization(self):
        entry = {"addr": ALICE, "chat": 7, "build": "this-build"}
        rpc = mock.Mock()
        rpc.get_message.return_value = SimpleNamespace(chat_id=7, from_id=1)
        rpc.get_basic_chat_info.return_value = SimpleNamespace(chat_type="Single", is_encrypted=True,
                                                               is_contact_request=False, is_self_talk=False)
        rpc.get_chat_contacts.return_value = [42]
        rpc.get_contact.return_value = SimpleNamespace(address=ALICE)
        stack = mock.patch.multiple(miniapp, _instance=mock.DEFAULT, app_build=mock.DEFAULT)
        values = stack.start()
        values["_instance"].return_value = entry
        values["app_build"].return_value = "this-build"
        self.addCleanup(stack.stop)
        for item in (mock.patch.object(live_web.access, "bound_allowed", return_value=True),
                     mock.patch.object(live_web.admin, "is_allowed", return_value=True),
                     mock.patch.object(config, "service_access_allowed", return_value=True),
                     mock.patch.object(state, "get_kill_switch", return_value=False),
                     mock.patch.object(chat, "rpc", return_value=rpc), mock.patch.object(chat, "account", return_value=3)):
            item.start(); self.addCleanup(item.stop)
        return entry, rpc

    def test_bridge_requires_bot_message_single_encrypted_bound_contact(self):
        entry, rpc = self._authorization()
        self.assertEqual(live_web._authorized(10), (entry, ALICE))
        for field, value in (("chat_type", "Group"), ("is_encrypted", False), ("is_contact_request", True), ("is_self_talk", True)):
            obj = rpc.get_basic_chat_info.return_value
            before = getattr(obj, field)
            setattr(obj, field, value)
            self.assertIsNone(live_web._authorized(10))
            setattr(obj, field, before)
        rpc.get_message.return_value.from_id = 12
        self.assertIsNone(live_web._authorized(10))
        rpc.get_message.return_value.from_id = 1
        rpc.get_contact.return_value.address = BOB
        self.assertIsNone(live_web._authorized(10))
        rpc.get_contact.return_value.address = ALICE
        entry["build"] = "outdated"
        self.assertIsNone(live_web._authorized(10))

    def test_action_retries_do_not_repeat_side_effects(self):
        session = live_web.Session(10, "a" * 32, ALICE, "youtube", "https://youtube.com")
        action = {"seq": 1, "action": "click", "x": 10, "y": 20}
        session.submit(action)
        session.submit(action)
        self.assertEqual(session.queue.qsize(), 1)
        self.assertEqual(session.queue.get_nowait()["seq"], 1)
        with session.mutex:
            session.inflight = 0
            session.last_seq = 1
            session.last_reply = {"t": "frame", "seq": 1}
        with mock.patch.object(live_web, "_send") as send:
            session.submit(action)
            send.assert_called_once()
            session.submit({"seq": 3, "action": "click"})
            self.assertTrue(session.queue.empty())

    def test_revocation_and_stop_prevent_session_save(self):
        session = live_web.Session(10, "a" * 32, ALICE, "youtube", "https://youtube.com")
        result = {"kind": "state", "storage": storage("test")}
        with mock.patch.object(session, "_allowed", side_effect=lambda: not session.stopped.is_set()):
            self.assertEqual(session._reply(result, 1)["t"], "saved")
            session.stop()
            self.assertEqual(session._reply(result, 2)["error"], "closed")
        web_vault.remove(ALICE, "youtube")
        self.assertIsNone(web_vault.load(ALICE, "youtube"))

    def test_forget_serializes_with_a_concurrent_new_browser(self):
        """A new session cannot slip between stopping old workers and deleting storage."""
        owner, site = ALICE, "youtube"
        web_vault.save(owner, site, storage("before-forget"))
        old = live_web.Session(10, "a" * 32, owner, site, "https://youtube.com")
        entered, release, new_started, forgotten = (threading.Event() for _ in range(4))
        failures = []
        original_stop = old.stop

        def slow_stop():
            entered.set()
            if not release.wait(5):
                failures.append("stop did not resume")
            original_stop()

        def forget():
            try:
                miniapp.op_browser_session(7, {"do": "disconnect", "site": site}, "en")
                forgotten.set()
            except Exception as exc:
                failures.append(type(exc).__name__)

        def open_another():
            payload = {"v": 1, "sid": "b" * 32, "t": "start", "url": "https://youtube.com", "lang": "en"}
            live_web.receive(11, list(json.dumps(payload).encode()))

        with live_web._lock:
            live_web._sessions[10] = old
        try:
            with mock.patch.object(chat, "sender", return_value=owner), \
                 mock.patch.object(live_web, "_authorized", return_value=({"chat": 7}, owner)), \
                 mock.patch.object(old, "stop", side_effect=slow_stop), \
                 mock.patch.object(live_web.Session, "start", side_effect=lambda: new_started.set()):
                remover = threading.Thread(target=forget, daemon=True)
                opener = threading.Thread(target=open_another, daemon=True)
                remover.start()
                self.assertTrue(entered.wait(3))
                opener.start()
                try:
                    # Before the fix, the newly opened browser starts here, while
                    # forget is still stopping a snapshot of the old sessions.
                    self.assertFalse(new_started.wait(0.2))
                finally:
                    release.set()
                    remover.join(5)
                    opener.join(5)
                self.assertFalse(remover.is_alive() or opener.is_alive(), "deadlock")
                self.assertTrue(forgotten.is_set())
                self.assertTrue(new_started.is_set())
                self.assertTrue(old.stopped.is_set())
                self.assertIsNone(web_vault.load(owner, site))
                self.assertEqual(failures, [])
        finally:
            release.set()
            with live_web._lock:
                live_web._sessions.pop(10, None)
                live_web._sessions.pop(11, None)

    def test_forget_waits_for_an_inflight_save_under_contention(self):
        """Repeated simultaneous save/forget operations leave no restored state."""
        for index in range(30):
            with self.subTest(iteration=index):
                session = live_web.Session(10, "a" * 32, ALICE, "youtube", "https://youtube.com")
                entered, release, done = (threading.Event() for _ in range(3))
                original_save = web_vault.save
                replies, errors = [], []

                def delayed_save(*args):
                    entered.set()
                    if not release.wait(5):
                        errors.append("save did not resume")
                    return original_save(*args)

                def save():
                    try:
                        replies.append(session._reply({"kind": "state", "storage": storage("inflight")}, 1))
                    except Exception as exc:
                        errors.append(type(exc).__name__)

                def forget():
                    try:
                        live_web.forget_site(ALICE, "youtube")
                        done.set()
                    except Exception as exc:
                        errors.append(type(exc).__name__)

                with live_web._lock:
                    live_web._sessions[10] = session
                try:
                    with mock.patch.object(session, "_allowed", side_effect=lambda: not session.stopped.is_set()), \
                         mock.patch.object(web_vault, "save", side_effect=delayed_save):
                        saver = threading.Thread(target=save, daemon=True)
                        remover = threading.Thread(target=forget, daemon=True)
                        saver.start()
                        self.assertTrue(entered.wait(3))
                        remover.start()
                        try:
                            self.assertFalse(done.wait(0.01))
                        finally:
                            release.set()
                            saver.join(5)
                            remover.join(5)
                        self.assertFalse(saver.is_alive() or remover.is_alive(), "deadlock")
                        self.assertEqual(errors, [])
                        self.assertEqual(replies[0]["t"], "saved")
                        self.assertTrue(done.is_set() and session.stopped.is_set())
                        self.assertIsNone(web_vault.load(ALICE, "youtube"))
                        self.assertEqual(session._reply({"kind": "state", "storage": storage("late")}, 2)["error"], "closed")
                finally:
                    release.set()
                    with live_web._lock:
                        live_web._sessions.pop(10, None)

    def test_kill_switch_races_with_save_and_invalidates_old_generation(self):
        with mock.patch.object(live_web, "_authorized", return_value=({"chat": 7}, ALICE)), \
             mock.patch.object(config, "service_access_allowed", return_value=True), \
             mock.patch.object(state, "save"), mock.patch.dict(state.data, {"kill_switch": {}}):
            for index in range(40):
                with self.subTest(iteration=index):
                    state.set_kill_switch(ALICE, False)
                    session = live_web.Session(10, "a" * 32, ALICE, "youtube", "https://youtube.com")
                    barrier = threading.Barrier(3)
                    replies, errors = [], []

                    def save():
                        try:
                            barrier.wait(5)
                            replies.append(session._reply({"kind": "state", "storage": storage("racing")}, 1))
                        except Exception as exc:
                            errors.append(type(exc).__name__)

                    def pause():
                        try:
                            barrier.wait(5)
                            state.set_kill_switch(ALICE, True)
                        except Exception as exc:
                            errors.append(type(exc).__name__)

                    saver = threading.Thread(target=save, daemon=True)
                    killer = threading.Thread(target=pause, daemon=True)
                    saver.start(); killer.start(); barrier.wait(5)
                    saver.join(5); killer.join(5)
                    self.assertFalse(saver.is_alive() or killer.is_alive(), "deadlock")
                    self.assertEqual(errors, [])
                    self.assertIn(replies[0]["t"], {"saved", "error"})  # either side may linearize first
                    self.assertEqual(session._reply({"kind": "state", "storage": storage("late")}, 2)["error"], "closed")
                    state.set_kill_switch(ALICE, False)
                    self.assertEqual(session._reply({"kind": "state", "storage": storage("late")}, 3)["error"], "closed")

    def test_guest_revocation_and_regrant_do_not_revive_old_browser(self):
        with mock.patch.object(config, "OWNER_ADDRESS", "owner@example.org"), \
             mock.patch.object(config, "OWNER_ONLY", True), mock.patch.object(config, "MAINTENANCE_MODE", False), \
             mock.patch.dict(state.data, {"kill_switch": {}}):
            access.reset()
            try:
                self.assertTrue(access.set_owner_only(False, 0, access.snapshot()["nonce"]))
                self.assertTrue(access.allow_guest(ALICE, access.snapshot()["revision"]))
                session = live_web.Session(10, "a" * 32, ALICE, "youtube", "https://youtube.com")
                with mock.patch.object(live_web, "_authorized", side_effect=lambda _instance: (
                        ({"chat": 7}, ALICE) if access.service_allowed(ALICE) else None)):
                    item = {"kind": "state", "storage": storage("guest")}
                    self.assertEqual(session._reply(item, 1)["t"], "saved")
                    self.assertTrue(access.revoke_guest(ALICE))
                    self.assertEqual(session._reply(item, 2)["error"], "closed")
                    self.assertTrue(access.allow_guest(ALICE, access.snapshot()["revision"]))
                    self.assertEqual(session._reply(item, 3)["error"], "closed")
            finally:
                access.reset()

    def test_capture_sent_as_file_then_temporary_copy_removed(self):
        session = live_web.Session(10, "a" * 32, ALICE, "youtube", "https://youtube.com")
        jpeg = b"\xff\xd8\xff" + b"fixture"
        captured = []
        def send(_chat, _text, **kwargs):
            path = kwargs["file"]
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.read_bytes(), jpeg)
            captured.append(path)
            return 1
        with mock.patch.object(session, "_allowed", return_value=True), \
             mock.patch.object(live_web, "_authorized", return_value=({"chat": 7}, ALICE)), \
             mock.patch.object(config, "service_access_allowed", return_value=True), \
             mock.patch.object(chat, "send", side_effect=send):
            result = session._reply({"kind": "capture", "image": base64.b64encode(jpeg).decode()}, 1)
        self.assertEqual(result["t"], "captured")
        self.assertEqual(len(captured), 1)
        self.assertFalse(captured[0].exists())

    def test_download_is_owner_bound_and_does_not_return_url(self):
        session = live_web.Session(10, "a" * 32, ALICE, "youtube", "https://youtube.com")
        url = "https://www.youtube.com/watch?v=private-test"
        with mock.patch.object(session, "_allowed", return_value=True), \
             mock.patch.object(live_web, "_authorized", return_value=({"chat": 7}, ALICE)), \
             mock.patch.object(config, "service_access_allowed", return_value=True), \
             mock.patch.object(media, "supported", return_value=True), \
             mock.patch.object(queue, "enqueue_media", return_value=({"id": "1"}, "")) as enqueue:
            self.assertEqual(session._queue_video(url, 1)["error"], "not_saved")
            web_vault.save(ALICE, "youtube", storage("one"))
            result = session._queue_video(url, 2)
            self.assertEqual(result["t"], "queued")
            self.assertNotIn(url, json.dumps(result))
            self.assertEqual(chat.sender(), ALICE)
            self.assertEqual(enqueue.call_args.args[0], 7)
            self.assertEqual(session._queue_video("https://facebook.com/video/1", 3)["error"], "not_video")


class BrowserFrameTests(unittest.TestCase):
    def test_sharp_frame_falls_back_and_remains_within_transport_limit(self):
        page = mock.Mock()
        page.screenshot.side_effect = lambda **kw: b"x" * (remote_worker.MAX_FRAME + 1 if kw["scale"] == "device" else 100)
        result = remote_worker.frame(page, "sharp")
        self.assertEqual(result["kind"], "frame")
        self.assertEqual(result["rendered"], "economy")
        self.assertEqual((result["width"], result["height"]), (480, 760))
        self.assertEqual([call.kwargs["scale"] for call in page.screenshot.call_args_list], ["device"] * 4 + ["css"])
        largest = dict(result, image=base64.b64encode(b"x" * remote_worker.MAX_FRAME).decode("ascii"))
        self.assertLess(len(json.dumps({"v": 1, "sid": "a" * 32, **largest})), live_web.MAX_RESPONSE)
        page.reset_mock()
        result = remote_worker.frame(page, "economy")
        self.assertEqual(result["rendered"], "economy")
        self.assertEqual(page.screenshot.call_args.kwargs["scale"], "css")

    def test_frame_rejects_even_an_economy_image_that_cannot_fit(self):
        page = mock.Mock()
        page.screenshot.return_value = b"x" * (remote_worker.MAX_FRAME + 1)
        self.assertEqual(remote_worker.frame(page, "sharp"), {"kind": "error", "error": "frame_too_large"})
        self.assertEqual(page.screenshot.call_count, 8)

    def test_address_bar_stays_in_its_service_container(self):
        self.assertTrue(live_web._same_site_url("https://m.youtube.com/watch?v=1", "youtube"))
        self.assertTrue(live_web._same_site_url("https://youtu.be/test", "youtube"))
        for url in ("https://facebook.com/", "http://youtube.com/", "https://youtube.com.evil.test/",
                    "https://name:pass@youtube.com/", "https://youtube.com:444/", None, "x" * 4097):
            with self.subTest(url=str(url)[:40]):
                self.assertFalse(live_web._same_site_url(url, "youtube"))

    def test_cross_site_address_is_rejected_before_reaching_browser(self):
        session = live_web.Session(10, "a" * 32, ALICE, "youtube", "https://youtube.com/")
        worker = SimpleNamespace(process=SimpleNamespace(stdout=io.BytesIO()))
        sent = []
        def send(_instance, _sid, message):
            sent.append(message)
            if message.get("seq") == 0:
                session.submit({"action": "goto", "seq": 1, "url": "https://facebook.com/"})
            elif message.get("seq") == 1:
                session.stop()
            return True
        with mock.patch.object(session, "_allowed", side_effect=lambda: not session.stopped.is_set()), \
             mock.patch.object(live_web, "_send", side_effect=send), \
             mock.patch.object(web_vault, "load", return_value=None), \
             mock.patch.object(sandbox, "BrowserProcess") as factory, \
             mock.patch.object(session, "_exchange", return_value={"kind": "frame", "image": ""}) as exchange:
            factory.return_value.__enter__.return_value = worker
            session.run()
        self.assertEqual(exchange.call_count, 1)  # opening only, no external goto
        self.assertIn({"t": "error", "seq": 1, "error": "different_site"}, sent)


class EgressAndResearchTests(unittest.TestCase):
    def test_public_addresses_and_authorities_fail_closed(self):
        for address in ("127.0.0.1", "10.2.3.4", "169.254.169.254", "::1", "::ffff:127.0.0.1",
                        "64:ff9b::7f00:1", "2002:7f00:1::", "ff02::1"):
            self.assertFalse(egress._public(address), address)
        self.assertTrue(egress._public("8.8.8.8"))
        for authority in ("example.com:80", "example.com:443/path", "user@example.com:443", "example.com"):
            with self.assertRaises(egress.EgressError):
                egress._authority(authority)
        self.assertEqual(egress._authority("example.com:443"), ("example.com", 443))

    def test_private_download_reserves_disk_for_concurrent_copies(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(config, "MIN_FREE_DISK_BYTES", 16_000_000), \
             mock.patch.object(private_media.shutil, "disk_usage", return_value=SimpleNamespace(free=56_000_000)):
            self.assertEqual(private_media._disk_limit(Path(directory), Path(directory)), 10_000_000)
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(config, "MIN_FREE_DISK_BYTES", 16_000_000), \
             mock.patch.object(private_media.shutil, "disk_usage", return_value=SimpleNamespace(free=16_000_100)):
            with self.assertRaisesRegex(ValueError, "disk_full"):
                private_media._disk_limit(Path(directory), Path(directory))

    def test_mixed_dns_answers_are_not_filtered_or_re_resolved(self):
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                   (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with mock.patch.object(egress.socket, "getaddrinfo", return_value=answers), self.assertRaises(egress.EgressError):
            egress._connect("fixture.example", 443)

    def test_coder_research_restricts_query_and_url(self):
        request = "Build example using Python"
        allowed = {"https://example.org/docs"}
        with mock.patch.object(coder.tools, "search", return_value=[tools_result("https://python.org/")]) as search:
            self.assertIn("python.org", coder._research("SEARCH", "Python example", request, allowed, "en"))
            search.assert_called_once()
            self.assertIn("https://python.org/", allowed)
            self.assertIn("Denied", coder._research("SEARCH", "password hidden", request, allowed, "en"))
        with mock.patch.object(smart_agent, "_read", return_value="public docs") as read:
            self.assertIn("public docs", coder._research("OPEN", "https://example.org/docs", request, allowed, "en"))
            self.assertIn("Denied", coder._research("OPEN", "https://evil.example/", request, allowed, "en"))
            read.assert_called_once()

    def test_gzip_is_bounded_and_decoded(self):
        class Response:
            status = 200
            def getheader(self, key):
                return {"Content-Type": "text/html", "Content-Encoding": "gzip"}.get(key)
            def read(self, _size):
                return gzip.compress(b"<html><body>Readable public page</body></html>")
        connection = mock.Mock()
        connection.getresponse.return_value = Response()
        with mock.patch.object(smart_agent, "_PublicHTTPS", return_value=connection):
            self.assertIn("Readable public page", smart_agent._read("https://example.org/"))

    def test_coder_retries_classifier_output_for_a_build_request(self):
        """A real free-router returned this instead of code: never treat it as a built project."""
        results = [ai.AIResult(text="User Safety: safe", provider="Router", model="Free"),
                   ai.AIResult(text="<<<FILE main.py>>>\nprint('ok')\n<<<END>>>\n"
                                    "<<<RUN>>>\npython3 main.py\n<<<END>>>\n<<<DONE>>>\ndone\n<<<END>>>",
                               provider="Coder", model="Model")]
        seen = []
        def ask(messages, _model, sticky):
            seen.append((messages[-1]["content"], sticky))
            return results.pop(0)
        with mock.patch.object(coder, "_ask", side_effect=ask), mock.patch.object(coder, "_sticky_key", return_value="router:free"), \
             mock.patch.object(coder, "isolation", return_value=True), mock.patch.object(coder, "_lock_private"), \
             mock.patch.object(coder, "_run", return_value=(0, "ok")), mock.patch.object(coder, "_remove", side_effect=shutil.rmtree):
            result = coder.run("Build a Python script", [], "en")
        self.assertEqual([item["path"] for item in result["files"]], ["main.py"])
        self.assertTrue(any(step["kind"] == "run" and step["code"] == 0 for step in result["steps"]))
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[1][1], "")  # the invalid router is not made sticky

    def test_coder_reports_failure_if_only_irrelevant_plain_responses_arrive(self):
        with mock.patch.object(coder, "MAX_STEPS", 2), \
             mock.patch.object(coder, "_ask", return_value=ai.AIResult(text="User Safety: safe")), \
             mock.patch.object(coder, "isolation", return_value=False):
            with self.assertRaises(coder.CoderError):
                coder.run("Build a Python script", [], "en")

    def test_coder_autonomous_loop_researches_then_tests(self):
        url = "https://example.org/docs"
        results = [ai.AIResult(text=f"<<<OPEN>>>\n{url}\n<<<END>>>"),
                   ai.AIResult(text="<<<FILE main.py>>>\nprint('ok')\n<<<END>>>\n<<<RUN>>>\npython3 main.py\n<<<END>>>\n<<<DONE>>>\ndone\n<<<END>>>")]
        seen = []
        def ask(messages, *_args):
            seen.append(messages[-1]["content"])
            return results.pop(0)
        with mock.patch.object(coder, "_ask", side_effect=ask), \
             mock.patch.object(smart_agent, "_read", return_value="PUBLIC DOCUMENTATION"), \
             mock.patch.object(coder, "isolation", return_value=True), mock.patch.object(coder, "_lock_private"), \
             mock.patch.object(coder, "_run", return_value=(0, "ok")), mock.patch.object(coder, "_remove", side_effect=shutil.rmtree):
            result = coder.run("Build example with " + url, [], "en")
        self.assertIn("PUBLIC DOCUMENTATION", seen[1])
        self.assertEqual(result["files"][0]["path"], "main.py")
        self.assertTrue(any(step["kind"] == "research" for step in result["steps"]))
        self.assertTrue(any(step["kind"] == "run" and step["code"] == 0 for step in result["steps"]))


def tools_result(url):
    return SimpleNamespace(url=url, title="Documentation", snippet="public")


@unittest.skipUnless(os.getenv("MICHIFLIN_SANDBOX_TESTS") == "1", "requires Linux namespaces, sudo, ffmpeg, openssl and socat")
class RealSandboxTests(unittest.TestCase):
    def test_worker_starts_with_restrictive_parent_umask(self):
        """The private runner must work even when its parent uses umask 077."""
        original = sandbox.shutil.copyfile
        code = ("import json,os,stat\n"
                "open('/work/permission-probe','w').write('x')\n"
                "print(json.dumps({'uid':os.getuid(),'mode':stat.S_IMODE(os.stat('/work/permission-probe').st_mode),"
                "'system_accessible':os.path.isfile('/usr/bin/env')}),flush=True)\n")
        def copy(src, dst, *args, **kwargs):
            if Path(src).name == "media_worker.py":
                Path(dst).write_text(code)
                return dst
            return original(src, dst, *args, **kwargs)
        with mock.patch.object(sandbox.shutil, "copyfile", side_effect=copy):
            previous = os.umask(0o077)
            try:
                worker = sandbox.BrowserProcess("media_worker.py")
            finally:
                os.umask(previous)
            with worker:
                self.assertTrue(select.select([worker.process.stdout], [], [], 45)[0])
                line = worker.process.stdout.readline()
                self.assertTrue(line, "worker exited before producing a result under umask 077")
                result = json.loads(line)
                self.assertEqual(result, {"uid": 65533, "mode": 0o600, "system_accessible": True})

    @unittest.skipUnless(shutil.which("deno"), "Deno is not installed on this runner")
    def test_real_deno_runs_inside_media_sandbox(self):
        """Exercise the actual Deno binary via the same private worker jail."""
        original = sandbox.shutil.copyfile
        code = ("import json,shutil,subprocess\n"
                "path=shutil.which('deno')\n"
                "result=subprocess.run([path,'eval','console.log(42)'],capture_output=True,text=True,timeout=20)\n"
                "print(json.dumps({'path':path,'code':result.returncode,'output':result.stdout.strip()}),flush=True)\n")
        def copy(src, dst, *args, **kwargs):
            if Path(src).name == "media_worker.py":
                Path(dst).write_text(code)
                return dst
            return original(src, dst, *args, **kwargs)
        with mock.patch.object(sandbox.shutil, "copyfile", side_effect=copy), sandbox.BrowserProcess("media_worker.py") as worker:
            self.assertIsNotNone(worker.process)
            self.assertTrue(select.select([worker.process.stdout], [], [], 45)[0])
            result = json.loads(worker.process.stdout.readline())
            self.assertEqual(result, {"path": "/runtime/deno", "code": 0, "output": "42"})

    def test_cannot_reach_host_files_through_relay_proc_root(self):
        marker = Path(tempfile.gettempdir()) / ("michiflin-test-marker-" + os.urandom(8).hex())
        marker.write_text("TEST-MARKER"); marker.chmod(0o644)
        original = sandbox.shutil.copyfile
        code = ("import glob,json\nfound=[]\n"
                "for p in glob.glob('/proc/[0-9]*'):\n"
                " try:\n"
                f"  found.append(open(p+'/root/tmp/{marker.name}').read())\n"
                " except (OSError,PermissionError): pass\n"
                "print(json.dumps(found),flush=True)\n")
        def copy(src, dst, *args, **kwargs):
            if Path(src).name == "media_worker.py":
                Path(dst).write_text(code)
                return dst
            return original(src, dst, *args, **kwargs)
        try:
            with mock.patch.object(sandbox.shutil, "copyfile", side_effect=copy), sandbox.BrowserProcess("media_worker.py") as worker:
                self.assertIsNotNone(worker.process)
                data = worker.process.stdout.readline()
                self.assertEqual(json.loads(data), [])
        finally:
            marker.unlink(missing_ok=True)

    def test_authenticated_video_isolated_by_user_in_local_fixture(self):
        if not all(shutil.which(item) for item in ("ffmpeg", "openssl")):
            self.skipTest("ffmpeg/openssl missing")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video, cert, key = root / "sample.mp4", root / "cert.pem", root / "key.pem"
            subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=blue:s=96x96:d=0.3",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)], check=True, timeout=25)
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", str(key), "-out", str(cert),
                            "-days", "1", "-nodes", "-subj", "/CN=fixture.example"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=True, timeout=25)
            class Handler(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    if self.path.split("?", 1)[0] == "/long":
                        body = (b'<html><body style="height:2200px">Top'
                                b'<button style="position:fixed;left:20px;top:80px;width:120px;height:50px" '
                                b'onclick="localStorage.setItem(\'clicked\',\'yes\')">Click fixture</button>'
                                b'<div style="position:absolute;top:2000px">Bottom</div></body></html>')
                        self.send_response(200); self.send_header("Content-Type", "text/html")
                        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
                    if "demo_session=alice" not in (self.headers.get("Cookie") or ""):
                        self.send_response(403); self.end_headers(); return
                    body = video.read_bytes() if self.path == "/video.mp4" else b'<html><video src="/video.mp4"></video></html>'
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp4" if self.path == "/video.mp4" else "text/html")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers(); self.wfile.write(body)
                def log_message(self, *_args):
                    pass
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(str(cert), str(key))
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            original_connect, original_copy = egress._connect, sandbox.shutil.copyfile
            def connect(host, port):
                if host == "fixture.example" and port == 443:
                    return socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
                return original_connect(host, port)
            def copy(src, dst, *args, **kwargs):
                if Path(src).name == "media_worker.py":
                    # A disposable worker alone accepts this self-signed fixture certificate.
                    text = Path(src).read_text().replace('"quiet": True, "no_warnings": True,',
                                                         '"quiet": True, "no_warnings": True, "nocheckcertificate": True,')
                    Path(dst).write_text(text); return dst
                if Path(src).name == "remote_worker.py":
                    # The same test-only exception for the self-signed fixture.
                    text = Path(src).read_text().replace('"device_scale_factor": 2,',
                                                         '"device_scale_factor": 2, "ignore_https_errors": True,')
                    Path(dst).write_text(text); return dst
                return original_copy(src, dst, *args, **kwargs)
            # Exercise the entire browser -> vault -> queue -> media route with
            # the restrictive mask used by security-conscious deployments.
            previous_umask = os.umask(0o077)
            try:
                with mock.patch.object(egress, "_connect", side_effect=connect), \
                     mock.patch.object(sandbox.shutil, "copyfile", side_effect=copy), \
                     mock.patch.object(config, "CONFIG_DIR", root / "vault"), \
                     mock.patch.object(config, "MIN_FREE_DISK_BYTES", 16_000_000), \
                     mock.patch.dict(os.environ, {"WEB_SESSION_KEY": "fixture-only-key-" * 4}):
                    url = "https://fixture.example/secure"
                    web_vault.save(ALICE, "host:fixture.example", storage("alice"))
                    web_vault.save(BOB, "host:fixture.example", storage("bob"))
                    with state.person(BOB):
                        with self.assertRaises(ValueError):
                            private_media.inspect(url, private_media.storage_for(url))
                    with state.person(ALICE):
                        mine = private_media.storage_for(url)
                        self.assertTrue(private_media.inspect(url, mine).get("title"))
                        result = private_media.download(url, mine, root / "result", "video", 360, "m4a")
                    self.assertEqual(hashlib.sha256(result.read_bytes()).digest(), hashlib.sha256(video.read_bytes()).digest())
                    # Exercise the real browser-to-queue path, then prepare the
                    # queued media as Alice; the fixture/network overrides exist
                    # only inside this test process.
                    session = live_web.Session(10, "a" * 32, ALICE, "host:fixture.example", url)
                    with mock.patch.object(session, "_allowed", return_value=True), \
                         mock.patch.object(live_web, "_authorized", return_value=({"chat": 7}, ALICE)), \
                         mock.patch.object(config, "service_access_allowed", return_value=True), \
                         mock.patch.object(config, "JOB_ROOT", root / "jobs"), \
                         mock.patch.object(media, "supported", return_value=True), \
                         mock.patch.object(media.net, "validate_public_url", side_effect=lambda value: value), \
                         mock.patch.object(queue.access, "job_allowed", return_value=True), \
                         mock.patch.object(queue, "_capacity_error", return_value=""), \
                         mock.patch.object(queue.usage, "note"), mock.patch.object(state, "save"), \
                         mock.patch.dict(state.data, {"jobs": {}, "flags": {}}):
                        ack = session._queue_video(url, 1)
                        self.assertEqual(ack["t"], "queued")
                        job = state.data["jobs"][ack["id"]]
                        self.assertEqual(job["addr"], ALICE)
                        self.assertEqual(job["origin"], "app")
                        with state.person(ALICE):
                            queue._prepare(job)
                        self.assertIn(job["status"], {"ready", "sending"})
                        part = Path(job["part_files"][0])
                        self.assertEqual(hashlib.sha256(part.read_bytes()).digest(), hashlib.sha256(video.read_bytes()).digest())
                    # A live frame stays small, but the explicit capture covers
                    # the whole long page at twice the CSS resolution.
                    from PIL import Image
                    with sandbox.BrowserProcess() as browser:
                        process = browser.process
                        def exchange(payload):
                            process.stdin.write((json.dumps(payload) + "\n").encode()); process.stdin.flush()
                            self.assertTrue(select.select([process.stdout], [], [], 45)[0])
                            return json.loads(process.stdout.readline(3_000_001))
                        first = exchange({"url": "https://fixture.example/long", "storage": None, "detail": "sharp"})
                        self.assertEqual((first["kind"], first["rendered"]), ("frame", "sharp"))
                        frame = Image.open(io.BytesIO(base64.b64decode(first["image"])))
                        self.assertEqual(frame.size, (960, 1520))
                        self.assertEqual((first["width"], first["height"]), (480, 760))  # click coordinates remain CSS pixels
                        self.assertLess(len(json.dumps({"v": 1, "sid": "a" * 32, **first})), live_web.MAX_RESPONSE)
                        clicked = exchange({"action": "click", "x": 60, "y": 100, "seq": 1})
                        self.assertEqual(clicked["kind"], "frame")
                        saved = exchange({"action": "save", "seq": 2})
                        self.assertTrue(any(item["name"] == "clicked" and item["value"] == "yes"
                                            for origin in saved["storage"]["origins"] for item in origin["localStorage"]))
                        small = exchange({"action": "frame", "detail": "economy", "seq": 3})
                        self.assertEqual((small["kind"], small["rendered"]), ("frame", "economy"))
                        self.assertEqual(Image.open(io.BytesIO(base64.b64decode(small["image"]))).size, (480, 760))
                        image = exchange({"action": "capture", "seq": 4})
                        self.assertEqual(image["kind"], "capture")
                        capture = Image.open(io.BytesIO(base64.b64decode(image["image"])))
                        self.assertEqual(capture.width, 960)
                        self.assertEqual(capture.height, image["height"] * 2)
                        self.assertGreater(capture.height, 3000)
                        self.assertEqual(exchange({"action": "scroll", "amount": 240, "seq": 5})["kind"], "frame")
                        scrolled = exchange({"action": "capture", "seq": 6})
                        self.assertEqual(scrolled["kind"], "capture")
                        self.assertLess(scrolled["height"], image["height"])
                        self.assertEqual(Image.open(io.BytesIO(base64.b64decode(scrolled["image"]))).height,
                                         scrolled["height"] * 2)
                        self.assertEqual(exchange({"action": "goto", "url": "https://fixture.example/long?second=1", "seq": 7})["kind"], "frame")
                        self.assertEqual(exchange({"action": "back", "seq": 8})["kind"], "frame")
                        self.assertEqual(exchange({"action": "forward", "seq": 9})["kind"], "frame")
                        self.assertIn("second=1", exchange({"action": "page_url", "seq": 10})["url"])
            finally:
                os.umask(previous_umask)
                server.shutdown(); server.server_close(); thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
