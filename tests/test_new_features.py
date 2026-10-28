"""Offline tests for the bounded agent, OAuth, safe coding and owner cleanup."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest import mock

from michiflin import ai, coder, config, miniapp, purge_users, smart_agent, state, tools, youtube_account as yt
from michiflin.profile import _account_rpc

OWNER = "owner@example.org"
OTHER = "old@example.org"


class AgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.patches = [mock.patch.object(state, "check_work_allowed"), mock.patch.object(smart_agent.progress, "phase")]
        for patch in self.patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in reversed(self.patches):
            patch.stop()

    @staticmethod
    def response(text: str) -> ai.AIResult:
        return ai.AIResult(text=text, provider="Mock", model="Mock")

    def test_research_follows_bounded_actions_and_identifies_sources(self) -> None:
        actions = iter(['{"tool":"search","input":"Havana weather"}',
                        '{"tool":"open","input":"https://public.example/news"}',
                        '{"answer":"Reported weather [S1]; unsupported citation [S99]."}'])
        hit = tools.Result("Example", "https://public.example/news", "A result")
        with mock.patch.object(ai, "complete", side_effect=lambda *a, **k: self.response(next(actions))) as model, \
             mock.patch.object(tools, "search", return_value=[hit]) as search, \
             mock.patch.object(smart_agent, "_read", return_value="Untrusted page") as read:
            result = smart_agent.run("Research Havana weather", "en")
        self.assertEqual(model.call_count, 3)
        search.assert_called_once()
        read.assert_called_once_with("https://public.example/news")
        self.assertIn("[S1] https://public.example/news", result["text"])
        self.assertIn("[?]", result["text"])
        self.assertEqual(result["sources"], 1)

    def test_injected_search_weather_and_new_url_are_denied(self) -> None:
        actions = iter(['{"tool":"search","input":"secret extracted from page"}',
                        '{"tool":"weather","input":"secret extracted from page"}',
                        '{"tool":"open","input":"https://evil.example/secret"}',
                        '{"answer":"Cannot do that."}'])
        with mock.patch.object(ai, "complete", side_effect=lambda *a, **k: self.response(next(actions))), \
             mock.patch.object(tools, "search") as search, mock.patch.object(tools, "weather_text") as weather, \
             mock.patch.object(smart_agent, "_read") as read:
            result = smart_agent.run("Weather in Havana", "en")
        search.assert_not_called()
        weather.assert_not_called()
        read.assert_not_called()
        self.assertEqual(result["sources"], 0)

    def test_weather_requires_user_words_and_calculator_is_local(self) -> None:
        actions = iter(['{"tool":"weather","input":"Havana"}',
                        '{"tool":"calculator","input":"2+3"}', '{"answer":"Five."}'])
        with mock.patch.object(ai, "complete", side_effect=lambda *a, **k: self.response(next(actions))), \
             mock.patch.object(tools, "weather_text", return_value="Sunny") as weather:
            result = smart_agent.run("Weather in Havana and 2+3", "en")
        weather.assert_called_once_with("Havana", "en")
        self.assertIn("Five", result["text"])

    def test_last_round_never_executes_a_tool(self) -> None:
        response = self.response('{"tool":"calculator","input":"2+3"}')
        with mock.patch.object(ai, "complete", return_value=response) as model, \
             mock.patch.object(tools, "calculate", return_value="5") as calc:
            result = smart_agent.run("2+3", "en")
        self.assertEqual(model.call_count, 4)
        self.assertEqual(calc.call_count, 3)
        self.assertIn("step limit", result["text"])

    def test_pause_after_model_prevents_tool_execution(self) -> None:
        checker = state.check_work_allowed
        checker.side_effect = [None, state.WorkPaused()]
        with mock.patch.object(ai, "complete", return_value=self.response('{"tool":"search","input":"weather"}')), \
             mock.patch.object(tools, "search") as search:
            with self.assertRaises(state.WorkPaused):
                smart_agent.run("weather", "en")
        search.assert_not_called()

    def test_dns_rejects_private_mixed_and_ipv4_mapped_addresses(self) -> None:
        for addresses in [["127.0.0.1"], ["10.0.0.1"], ["::1"], ["::ffff:192.168.1.1"],
                          ["64:ff9b::7f00:1"], ["2002:7f00:1::"], ["8.8.8.8", "127.0.0.1"]]:
            entries = [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in addresses]
            with self.subTest(addresses=addresses), mock.patch.object(socket, "getaddrinfo", return_value=entries), \
                 mock.patch.object(socket, "socket") as connect:
                with self.assertRaises(ValueError):
                    smart_agent._public_socket("example.org", 443, 1)
                connect.assert_not_called()

    def test_dns_connects_to_prevalidated_sockaddr(self) -> None:
        entries = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        fake = mock.Mock()
        with mock.patch.object(socket, "getaddrinfo", return_value=entries) as lookup, \
             mock.patch.object(socket, "socket", return_value=fake):
            self.assertIs(smart_agent._public_socket("example.org", 443, 1), fake)
        lookup.assert_called_once()
        fake.connect.assert_called_once_with(("8.8.8.8", 443))

    def test_url_and_protocol_reject_credentials_and_unknown_actions(self) -> None:
        self.assertEqual(smart_agent._url("http://u:p@example.org/"), "")
        self.assertEqual(smart_agent._url("file:///etc/passwd"), "")
        self.assertIsNone(smart_agent._parse('{"tool":"search","input":"x","extra":1}'))
        self.assertEqual(smart_agent._parse('```json\n{"answer":"ok"}\n```'), {"answer": "ok"})


class YouTubeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [mock.patch.dict(os.environ, GOOGLE_OAUTH_CLIENT_ID="client", GOOGLE_OAUTH_CLIENT_SECRET="secret",
                                        WEB_SESSION_KEY="x" * 48),
                        mock.patch.object(config, "CONFIG_DIR", Path(self.tmp.name)),
                        mock.patch.object(state, "check_work_allowed")]
        for patch in self.patches:
            patch.start()
        yt._pending.clear()
        self.device = {"device_code": "private-device-code", "user_code": "ABCD-EFGH", "verification_url": "https://www.google.com/device",
                       "expires_in": 600, "interval": 5}
        self.tokens = {"access_token": "private-access-token", "refresh_token": "private-refresh-token", "expires_in": 3600, "scope": yt.SCOPE}

    def tearDown(self) -> None:
        yt._pending.clear()
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    def connect(self) -> None:
        with mock.patch.object(yt, "_request", side_effect=[(200, self.device), (200, self.tokens)]):
            first = yt.start(OWNER)
            self.assertEqual(first["user_code"], "ABCD-EFGH")
            self.assertNotIn("private-device-code", json.dumps(first))
            self.assertTrue(yt.poll(OWNER)["connected"])

    def test_disabled_without_deployment_configuration(self) -> None:
        with mock.patch.dict(os.environ, {"WEB_SESSION_KEY": ""}):
            self.assertFalse(yt.status(OWNER)["configured"])
            with self.assertRaises(yt.AccountError):
                yt.start(OWNER)

    def test_sender_isolated_encrypted_tokens_and_read_only_calls(self) -> None:
        self.connect()
        encrypted = yt._path(OWNER).read_bytes()
        for secret in (OWNER, "private-access-token", "private-refresh-token"):
            self.assertNotIn(secret.encode(), encrypted)
        self.assertFalse(yt.status(OTHER)["connected"])
        with self.assertRaises(yt.AccountError):
            yt.read(OTHER, "channels")
        payload = {"items": [{"snippet": {"title": "Channel", "description": "Description"}}]}
        with mock.patch.object(yt, "_request", return_value=(200, payload)) as request:
            view = yt.read(OWNER, "channels")
        self.assertEqual(view["items"][0]["title"], "Channel")
        args, kwargs = request.call_args
        self.assertEqual(args[:2], ("GET", yt.API_URL + "channels"))
        self.assertEqual(kwargs["params"]["mine"], "true")
        self.assertNotIn("private-access-token", json.dumps(view))
        with self.assertRaises(yt.AccountError):
            yt.read(OWNER, "videos/insert")

    def test_oauth_rejects_other_scope_and_malicious_verification_url(self) -> None:
        evil = dict(self.device, verification_url="https://google.com.evil.example/device")
        with mock.patch.object(yt, "_request", return_value=(200, evil)):
            with self.assertRaises(yt.AccountError):
                yt.start(OWNER)
        bad = dict(self.tokens, scope=yt.SCOPE + " https://www.googleapis.com/auth/youtube.force-ssl")
        with mock.patch.object(yt, "_request", side_effect=[(200, self.device), (200, bad)]):
            yt.start(OWNER)
            with self.assertRaises(yt.AccountError):
                yt.poll(OWNER)
        self.assertFalse(yt._path(OWNER).exists())

    def test_refresh_token_stays_in_backend(self) -> None:
        self.connect()
        value = yt._load(OWNER)
        assert value is not None
        value["expires"] = 0
        yt._save(OWNER, value)
        with mock.patch.object(yt, "_request", side_effect=[(200, {"access_token": "refreshed", "expires_in": 3600}),
                                                             (200, {"items": []})]) as request:
            yt.read(OWNER, "subscriptions")
        self.assertEqual(request.call_count, 2)
        self.assertNotIn(b"private-refresh-token", yt._path(OWNER).read_bytes())

    def test_disconnect_reports_unconfirmed_revocation_and_removes_local_copy(self) -> None:
        self.connect()
        with mock.patch.object(yt, "_request", side_effect=yt.AccountError("network")):
            result = yt.disconnect(OWNER)
        self.assertFalse(result["revoked"])
        self.assertFalse(yt._path(OWNER).exists())

    def test_rotated_or_missing_key_can_remove_unreadable_local_token(self) -> None:
        self.connect()
        with mock.patch.dict(os.environ, {"WEB_SESSION_KEY": "y" * 48}):
            self.assertTrue(yt.status(OWNER)["vault_error"])
            self.assertFalse(yt.disconnect(OWNER)["revoked"])
        self.assertFalse(yt._path(OWNER).exists())
        self.connect()
        with mock.patch.dict(os.environ, {"WEB_SESSION_KEY": ""}):
            self.assertTrue(yt.status(OWNER)["stored"])
            self.assertFalse(yt.disconnect(OWNER)["revoked"])
        self.assertFalse(yt._path(OWNER).exists())

    def test_pause_during_device_request_does_not_save_pending_grant(self) -> None:
        state.check_work_allowed.side_effect = [None, state.WorkPaused()]
        with mock.patch.object(yt, "_request", return_value=(200, self.device)):
            with self.assertRaises(state.WorkPaused):
                yt.start(OWNER)
        self.assertNotIn(OWNER, yt._pending)

    def test_google_http_requests_do_not_forward_tokens_to_redirects(self) -> None:
        session = mock.MagicMock()
        response = mock.Mock(status_code=302, content=b"")
        session.__enter__.return_value.request.return_value = response
        with mock.patch.object(yt.requests, "Session", return_value=session):
            with self.assertRaises(yt.AccountError):
                yt._request("GET", yt.API_URL + "channels", headers={"Authorization": "Bearer secret"})
        call = session.__enter__.return_value.request.call_args
        self.assertFalse(call.kwargs["allow_redirects"])
        self.assertFalse(session.__enter__.return_value.trust_env)


class CoderFilesystemTests(unittest.TestCase):
    def test_symlinks_and_hardlinks_never_touch_or_read_external_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            external = root / "outside"
            external.mkdir()
            secret = external / "secret.txt"
            secret.write_text("private")
            (project / "link").symlink_to(external, target_is_directory=True)
            (project / "filelink").symlink_to(secret)
            os.link(secret, project / "hard.txt")
            mode = secret.stat().st_mode
            coder.write_files(project, [{"path": "link/secret.txt", "text": "overwrite"},
                                        {"path": "filelink", "text": "overwrite"},
                                        {"path": "hard.txt", "text": "overwrite"},
                                        {"path": "good/ok.txt", "text": "hello"}])
            self.assertEqual(secret.read_text(), "private")
            self.assertEqual(secret.stat().st_mode, mode)
            self.assertFalse(coder._safe_delete(project, "link/secret.txt"))
            self.assertFalse(coder._safe_delete(project, "filelink"))
            self.assertEqual(coder.read_files(project), [{"path": "good/ok.txt", "text": "hello"}])
            self.assertTrue(coder._safe_delete(project, "good/ok.txt"))
            self.assertTrue(secret.exists())

    def test_non_regular_files_and_path_traversal_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            os.mkfifo(project / "pipe")
            self.assertFalse(coder._safe_write_text(project, "pipe", "hello", 100))
            self.assertFalse(coder._safe_write_text(project, "../outside", "hello", 100))
            self.assertFalse(coder._safe_delete(project, "pipe"))
            self.assertEqual(coder.read_files(project), [])


class PurgeProjectionTests(unittest.TestCase):
    def test_precise_owner_projection_preserves_unknowns_and_shared_data(self) -> None:
        data = {"version": 5, "people": {OWNER: {"name": "owner"}, OTHER: {"name": "old"}, "unattributed": {"keep": 1}},
                "prefs": {OTHER: {"lang": "en"}}, "notes": {}, "link_queue": {}, "nauta_strict": {}, "kill_switch": {},
                "jobs": {"owner": {"addr": OWNER}, "old": {"addr": OTHER}, "unknown": {"state": "x"},
                         "old-chat": {"chat": 20}},
                "apps": {"1": {"addr": OWNER}, "2": {"addr": OTHER}},
                "app_seen": {"a": [1, 2, 1], "b": [1, 2, 2], "c": [1, 2, 999], "old-msg": [1, 2, 200]},
                "inbox": {"100": 1, "200": 1, "unknown": 1},
                "sent": [[1, 2, OWNER], [1, 2, OTHER], [1, 2, "?"]],
                "access": {"allowed": [OWNER, OTHER, "?"], "blocked": [OTHER], "attempts": {OTHER: 3, OWNER: 2}, "alerts": True},
                "flags": {"limited": [OWNER, OTHER, "?"], "other_flag": 3}, "links": {"shared": "keep"}}
        cleaned, counts = purge_users._owner_projection(data, OWNER, {20}, {200})
        self.assertEqual(cleaned["people"][OWNER], data["people"][OWNER])
        self.assertEqual(cleaned["people"]["unattributed"], {"keep": 1})
        self.assertNotIn(OTHER, cleaned["people"])
        self.assertEqual(set(cleaned["jobs"]), {"owner", "unknown"})
        self.assertEqual(set(cleaned["apps"]), {"1"})
        self.assertEqual(set(cleaned["app_seen"]), {"a", "c"})
        self.assertEqual(set(cleaned["inbox"]), {"100", "unknown"})
        self.assertEqual(cleaned["links"], data["links"])
        self.assertEqual(cleaned["access"]["allowed"], [OWNER, "?"])
        self.assertTrue(cleaned["flags"][purge_users.MARKER])
        self.assertEqual(counts["jobs"], 2)
        self.assertIn(OTHER, data["people"], "input must remain unchanged")

    def test_invalid_state_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            purge_users._owner_projection({"people": []}, OWNER, set(), set())

    @unittest.skipUnless(shutil.which("deltachat-rpc-server"), "native Delta Chat RPC server not on PATH")
    def test_only_unshared_old_attachments_are_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "config"
            root.mkdir()
            proof = Path(directory) / "proof.json"
            state_data = {"version": 5, "people": {}, "prefs": {}, "notes": {}, "link_queue": {}, "nauta_strict": {},
                          "kill_switch": {}, "jobs": {}, "apps": {}, "app_seen": {}, "inbox": {}, "sent": [],
                          "access": {"allowed": [], "blocked": [], "attempts": {}}, "flags": {}}
            (root / "michiflin-state.json").write_text(json.dumps(state_data))
            shared = Path(directory) / "shared.txt"
            old_only = Path(directory) / "old.txt"
            shared.write_text("owner's shared content")
            old_only.write_text("old user's unique content")
            with _account_rpc(str(root / "accounts")) as rpc:
                account = rpc.add_account()
                rpc.set_config(account, "addr", "bot@example.org")
                for addr, file in ((OWNER, shared), (OTHER, shared), ("old2@example.org", old_only)):
                    contact = rpc.create_contact(account, addr, addr)
                    chat = rpc.create_chat_by_contact_id(account, contact)
                    rpc.misc_set_draft(account, chat, "draft", str(file), None, None, None)
                blob = Path(rpc.get_blob_dir(account))
                initial = {path.name for path in blob.iterdir() if path.is_file()}
            counts = purge_users.apply(root, OWNER, proof)
            self.assertEqual(counts["unshared_files"], 1)
            self.assertEqual(len(initial - {path.name for path in blob.iterdir() if path.is_file()}), 1)
            purge_users.verify(root, OWNER, proof)

    @unittest.skipUnless(shutil.which("deltachat-rpc-server"), "native Delta Chat RPC server not on PATH")
    def test_real_offline_delta_cleanup_and_encrypted_roundtrip(self) -> None:
        import state_backup
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "config"
            root.mkdir()
            proof = Path(directory) / "proof.json"
            data = {"version": 5, "people": {OWNER: {"x": 1}, OTHER: {"x": 2}}, "prefs": {}, "notes": {},
                    "link_queue": {}, "nauta_strict": {}, "kill_switch": {}, "jobs": {}, "apps": {}, "app_seen": {},
                    "inbox": {}, "sent": [], "access": {"allowed": [OWNER, OTHER], "blocked": [], "attempts": {}},
                    "flags": {}, "links": {"shared": 1}}
            (root / "michiflin-state.json").write_text(json.dumps(data))
            with _account_rpc(str(root / "accounts")) as rpc:
                account = rpc.add_account()
                rpc.set_config(account, "addr", "bot@example.org")
                for name, address in (("Owner", OWNER), ("Other", OTHER), ("Blocked", "blocked@example.org")):
                    contact = rpc.create_contact(account, address, name)
                    chat = rpc.create_chat_by_contact_id(account, contact)
                    rpc.misc_set_draft(account, chat, "draft " + name, None, None, None, None)
                    if name == "Blocked":
                        rpc.block_contact(account, contact)
            counts = purge_users.apply(root, OWNER, proof)
            self.assertEqual(counts["delta_chats"], 2)
            self.assertEqual(counts["delta_messages"], 2)
            self.assertEqual(counts["delta_contacts"], 2)
            purge_users.verify(root, OWNER, proof)
            salt = os.urandom(state_backup.SALT_BYTES)
            cipher = state_backup.fernet_from("a separate test secret", salt)
            encrypted = cipher.encrypt(state_backup.archive(root))
            state_backup.safely_extract(cipher.decrypt(encrypted), Path(directory) / "restore")
            purge_users.verify(Path(directory) / "restore" / "michiflin", OWNER, proof)
            with self.assertRaises(ValueError):
                purge_users.apply(root, OWNER, proof)


class CleanupGateTests(unittest.TestCase):
    def test_gate_accepts_only_same_verified_tree_as_unreferenced_history_root(self) -> None:
        path = Path(__file__).resolve().parent.parent / ".github/scripts/wait_for_clean_backup.py"
        spec = importlib.util.spec_from_file_location("cleanup_gate_test", path)
        assert spec and spec.loader
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        responses = iter([
            {"object": {"sha": "saved"}}, {"tree": {"sha": "same-tree"}},
            {"object": {"sha": "root"}}, {"parents": [], "tree": {"sha": "same-tree"}},
            [], [{"name": "main"}], {"default_branch": "main"},
        ])
        def get(*_args, **_kwargs):
            response = mock.Mock()
            response.json.return_value = next(responses)
            return response
        with mock.patch.dict(os.environ, {"STATE_BACKUP_REPO": "owner/private", "GITHUB_TOKEN": "fake-token"}), \
             mock.patch.object(gate.requests, "get", side_effect=get) as calls, mock.patch.object(gate.time, "sleep"):
            self.assertEqual(gate.main(), 0)
        self.assertEqual(calls.call_count, 7)
        self.assertTrue(all(call.args[0].startswith("https://api.github.com/repos/owner/private") for call in calls.call_args_list))

    def test_gate_waits_if_backup_tree_differs_or_old_refs_remain(self) -> None:
        path = Path(__file__).resolve().parent.parent / ".github/scripts/wait_for_clean_backup.py"
        spec = importlib.util.spec_from_file_location("cleanup_gate_test2", path)
        assert spec and spec.loader
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        attempts = iter([
            {"object": {"sha": "saved"}}, {"tree": {"sha": "verified"}},
            {"object": {"sha": "wrong"}}, {"parents": [], "tree": {"sha": "different"}},
            [{"name": "old-tag"}], [{"name": "main"}], {"default_branch": "main"},
            {"object": {"sha": "correct"}}, {"parents": [], "tree": {"sha": "verified"}},
            [], [{"name": "main"}], {"default_branch": "main"},
        ])
        def get(*_args, **_kwargs):
            response = mock.Mock()
            response.json.return_value = next(attempts)
            return response
        with mock.patch.dict(os.environ, {"STATE_BACKUP_REPO": "owner/private", "GITHUB_TOKEN": "fake-token"}), \
             mock.patch.object(gate.requests, "get", side_effect=get) as calls, mock.patch.object(gate.time, "sleep") as sleep:
            self.assertEqual(gate.main(), 0)
        self.assertEqual(calls.call_count, 12)
        sleep.assert_called_once_with(5)


class KillSwitchDisconnectTests(unittest.TestCase):
    def test_disconnect_allowed_but_youtube_reads_are_paused(self) -> None:
        class Immediate:
            def submit(self, fn):
                future = Future()
                try:
                    future.set_result(fn())
                except Exception as exc:
                    future.set_exception(exc)
                return future
        function = mock.Mock(return_value={"connected": False, "revoked": True})
        entry = {"chat": 10, "addr": OWNER}
        request = {"id": "abc", "op": "youtube_account", "lang": "en", "args": {"do": "disconnect"}}
        with mock.patch.dict(miniapp.OPS, {"youtube_account": function}), \
             mock.patch.object(miniapp.access, "bound_allowed", return_value=True), \
             mock.patch.object(miniapp.admin, "is_allowed", return_value=True), \
             mock.patch.object(miniapp.admin, "seen"), mock.patch.object(config, "service_access_allowed", return_value=True), \
             mock.patch.object(state, "get_kill_switch", return_value=True), mock.patch.object(state, "work_interrupted", return_value=True), \
             mock.patch.object(miniapp, "respond") as reply, mock.patch.object(miniapp, "_workers", Immediate()), \
             mock.patch.object(miniapp.usage, "note"), mock.patch.object(miniapp.perf, "note_op"):
            miniapp.handle(100, entry, request)
            function.assert_called_once()
            reply.assert_called()
            function.reset_mock()
            request["args"]["do"] = "channels"
            miniapp.handle(100, entry, request)
            function.assert_not_called()


if __name__ == "__main__":
    unittest.main()
