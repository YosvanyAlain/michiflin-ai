"""Offline regression tests for owner-only access, Nauta and emergency pause."""
from __future__ import annotations

import copy
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
_TMP = tempfile.TemporaryDirectory(prefix="michiflin-safety-")
os.environ.setdefault("OWNER_ADDRESS", "owner@example.org")
os.environ.setdefault("CONFIG_DIR", f"{_TMP.name}/config")
os.environ.setdefault("JOB_DIR", f"{_TMP.name}/jobs")
os.environ.setdefault("PREVIEW_DIR", f"{_TMP.name}/tmp")
os.environ.setdefault("FREE_GATEWAYS", "0")
os.environ.setdefault("AI_HORDE", "0")

from michiflin import access, admin, agent, ai, answer, app, chat, coder, commands, config, files, images, media, miniapp, progress, queue, state, usage  # noqa: E402

OWNER = "owner@example.org"
OLD = "old-user@example.org"
REAL_SAVE = state.save


class Immediate:
    def submit(self, function, *args):
        future = Future()
        try:
            future.set_result(function(*args))
        except Exception as exc:
            future.set_exception(exc)
        return future


class SafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        access.reset()
        self.saved = copy.deepcopy(state.data)
        self.saved_generations = dict(state._kill_generations)
        self.temp = tempfile.TemporaryDirectory(prefix="michiflin-safety-case-")
        self.patches = [
            mock.patch.object(config, "OWNER_ONLY", True),
            mock.patch.object(config, "MAINTENANCE_MODE", False),
            mock.patch.object(config, "OWNER_ADDRESS", OWNER),
            mock.patch.object(config, "ADMIN_ADDRESSES", {OWNER}),
            mock.patch.object(config, "ALLOWED_SENDERS", {OWNER, OLD}),
            mock.patch.object(config, "MAILBOX_PROFILES_JSON", ""),
            mock.patch.object(config, "JOB_ROOT", Path(self.temp.name) / "jobs"),
            mock.patch.object(state, "save"),
            mock.patch.object(chat, "ready", return_value=False),
            mock.patch.object(progress, "enabled", return_value=False),
        ]
        for patch in self.patches:
            patch.start()
        for key in ("jobs", "nauta_strict", "kill_switch", "settings", "people", "prefs"):
            state.data[key] = {}
        state.data["flags"] = {}
        state.data["sent"] = []
        state.data["apps"] = {}
        state.data["access"] = {"allowed": [OLD], "blocked": [], "attempts": {}, "alerts": True}
        state._kill_generations.clear()
        queue._stop_flags.clear()
        queue._claim_generations.clear()
        queue._delivery_generations.clear()
        chat.set_sender(OWNER)

    def tearDown(self) -> None:
        access.reset()
        state.data.clear()
        state.data.update(self.saved)
        state._kill_generations.clear()
        state._kill_generations.update(self.saved_generations)
        for patch in reversed(self.patches):
            patch.stop()
        self.temp.cleanup()

    def toggle_once(self) -> None:
        state.set_kill_switch(OWNER, True)
        state.set_kill_switch(OWNER, False)

    def open_guest_session(self, minutes: int = 60) -> None:
        nonce = access.snapshot()["nonce"]
        self.assertTrue(access.set_owner_only(False, minutes, nonce))

    def test_app_upgrades_and_reinstall_keep_every_bound_instance(self) -> None:
        # Repeated deliveries must not silently evict the first project-bearing app.
        with (mock.patch.object(miniapp, "app_build", return_value="build-1"),
              mock.patch.object(miniapp, "build", return_value=Path("/tmp/fake.xdc")),
              mock.patch.object(chat, "send", side_effect=[101, 102, 103, 104, 105, 106]),
              mock.patch("michiflin.live_web._identity", side_effect=lambda msg_id: (state.data["apps"][str(msg_id)], OWNER)),
              mock.patch.object(chat, "delete_for_all") as delete):
            for _ in range(4):
                miniapp.send_app(91, "en", address=OWNER)
            self.assertEqual(miniapp.instance_ids(), {101, 102, 103, 104})
            miniapp.replace_app(101, 91, "en", OWNER)
            miniapp.op_reinstall(91, {}, "en")
            self.assertEqual(miniapp.instance_ids(), {101, 102, 103, 104, 105, 106})
            delete.assert_not_called()
        # A temporary RPC outage is not evidence of a deleted message.
        with mock.patch.object(chat, "rpc", side_effect=RuntimeError("temporary")):
            self.assertEqual(miniapp.latest_instance(91), 0)
        self.assertEqual(miniapp.instance_ids(), {101, 102, 103, 104, 105, 106})

    def test_upgrade_is_not_repeated_after_restart_or_window_expiry(self) -> None:
        old = {"chat": 92, "addr": OWNER, "build": "old", "access_session": access.session_stamp(OWNER)}
        state.data["apps"]["150"] = old
        miniapp._upgraded.clear()
        with (mock.patch.object(miniapp, "app_build", return_value="new"),
              mock.patch.object(miniapp, "build", return_value=Path("/tmp/fake.xdc")),
              mock.patch.object(chat, "send", return_value=151) as send,
              mock.patch("michiflin.live_web._identity", return_value=(old, OWNER)),
              mock.patch.object(chat, "delete_for_all") as delete):
            miniapp._maybe_upgrade(150, old, "en")
            self.assertIn("150", state.data["apps"])
            self.assertIn("151", state.data["apps"])
            miniapp._upgraded.clear()  # simulate a new bot session, not a new build
            miniapp._maybe_upgrade(150, old, "en")
            send.assert_called_once()
            delete.assert_not_called()

    def test_unknown_coder_model_is_rejected_without_fallback(self) -> None:
        with (mock.patch.object(ai, "model_by_key", return_value=None),
              mock.patch.object(coder, "run") as run):
            with self.assertRaises(miniapp.AppError):
                miniapp.op_code(7, {"text": "Build a page", "model": "retired:model"}, "en")
            run.assert_not_called()

    def test_session_mode_requires_fresh_nonce_and_quarantines_old_grants(self) -> None:
        old_state = copy.deepcopy(state.data["access"])
        stale = access.snapshot()["nonce"]
        self.assertFalse(access.set_owner_only(False, 60, "bad"))
        self.assertTrue(access.snapshot()["private_mode"])
        self.open_guest_session()
        self.assertFalse(access.snapshot()["owner_only"])
        self.assertTrue(admin.is_allowed(OWNER))
        self.assertFalse(admin.is_allowed(OLD))  # old env and stored lists are not reactivated
        self.assertFalse(admin.is_admin(OLD))
        self.assertNotIn(OLD, [x["address"] for x in admin.snapshot()["users"]])
        self.assertEqual(state.data["access"], old_state)
        self.assertTrue(access.set_owner_only(True))
        self.assertFalse(access.set_owner_only(False, 60, stale))
        access.reset()  # restart: configured private default wins
        self.assertTrue(access.snapshot()["owner_only"])
        self.assertFalse(access.set_owner_only(False, 60, stale))

    def test_guest_is_explicit_not_admin_and_revocation_never_revives_old_jobs(self) -> None:
        self.open_guest_session()
        with mock.patch.object(admin, "_set_blocked_contact", return_value=True):
            self.assertTrue(admin.grant(OLD))
        self.assertTrue(admin.is_allowed(OLD))
        self.assertFalse(admin.is_admin(OLD))
        chat.set_sender(OLD)
        guest_job = queue._new_job(91, "file", "https://example.org/a", "a", 100)
        older_job = dict(guest_job, access_session="")
        self.assertTrue(access.job_allowed(guest_job))
        self.assertFalse(access.job_allowed(older_job))
        generation = state.kill_generation(OLD)
        self.assertTrue(admin.revoke(OLD))
        self.assertFalse(admin.is_allowed(OLD))
        self.assertTrue(state.work_interrupted(OLD, generation))
        with mock.patch.object(admin, "_set_blocked_contact", return_value=True):
            self.assertTrue(admin.grant(OLD))
        self.assertFalse(access.job_allowed(guest_job))  # a new approval cannot resume old work
        self.assertTrue(state.work_interrupted(OLD, generation))
        with self.assertRaises(miniapp.AppError):
            miniapp.op_owner_only(91, {"enabled": True}, "en")
        with self.assertRaises(miniapp.AppError):
            miniapp.op_admin(91, {}, "en")
        self.assertIn(OLD, state.data["access"]["allowed"])  # legacy history remains

    def test_old_webxdc_instance_is_not_reactivated_after_regrant(self) -> None:
        self.open_guest_session()
        with mock.patch.object(admin, "_set_blocked_contact", return_value=True):
            self.assertTrue(admin.grant(OLD))
        old_entry = {"chat": 91, "addr": OLD, "serial": 0, "access_session": access.session_stamp(OLD)}
        state.data["apps"]["123"] = old_entry
        self.assertTrue(admin.revoke(OLD))
        with mock.patch.object(admin, "_set_blocked_contact", return_value=True):
            self.assertTrue(admin.grant(OLD))
        with mock.patch.object(chat, "rpc") as rpc, mock.patch.object(miniapp, "respond") as respond:
            miniapp.on_status_update(123)
            miniapp.handle(123, old_entry, {"id": "stale", "op": "health", "lang": "en"})
            rpc.assert_not_called()
            respond.assert_not_called()
        with mock.patch.object(miniapp, "send_app") as send_app:
            miniapp.welcome(91, "en")
            send_app.assert_called_once()  # authorized guest needs a fresh instance
        new_entry = dict(old_entry, access_session=access.session_stamp(OLD))
        self.assertFalse(access.bound_allowed(old_entry))
        self.assertTrue(access.bound_allowed(new_entry))

    def test_revoked_guest_receives_no_result_from_already_running_app_work(self) -> None:
        self.open_guest_session()
        with mock.patch.object(admin, "_set_blocked_contact", return_value=True):
            self.assertTrue(admin.grant(OLD))
            self.assertTrue(admin.grant("second@example.org"))
        other_generation = state.kill_generation("second@example.org")
        entered, finish = threading.Event(), threading.Event()
        def slow(_chat, _args, _locale):
            entered.set()
            finish.wait(3)
            return {"private": "result"}
        entry = {"chat": 91, "addr": OLD, "access_session": access.session_stamp(OLD), "build": miniapp.app_build()}
        with mock.patch.dict(miniapp.OPS, {"slow_test": slow}), mock.patch.object(miniapp, "respond") as respond:
            worker = threading.Thread(target=miniapp.handle, args=(123, entry, {"id": "slow", "op": "slow_test"}))
            worker.start()
            self.assertTrue(entered.wait(3))
            self.assertTrue(admin.revoke(OLD))
            self.assertFalse(state.work_interrupted("second@example.org", other_generation))
            with mock.patch.object(admin, "_set_blocked_contact", return_value=True):
                self.assertTrue(admin.grant(OLD))
            finish.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())
            respond.assert_not_called()

    def test_old_safe_text_command_does_not_run_after_regrant(self) -> None:
        self.open_guest_session()
        with mock.patch.object(admin, "_set_blocked_contact", return_value=True):
            self.assertTrue(admin.grant(OLD))
        msg = SimpleNamespace(id=551, chat_id=91, from_id=3, file=None)
        app._work_tickets[551] = (state.kill_generation(OLD), False)
        try:
            self.assertTrue(admin.revoke(OLD))
            with mock.patch.object(admin, "_set_blocked_contact", return_value=True):
                self.assertTrue(admin.grant(OLD))
            with mock.patch.object(commands, "dispatch") as dispatch, mock.patch.object(progress, "begin") as begin:
                app._process(msg, "/app", "en", OLD)
                dispatch.assert_not_called()
                begin.assert_not_called()
        finally:
            app._work_tickets.pop(551, None)

    def test_revocation_invalidates_a_pending_slow_grant(self) -> None:
        self.open_guest_session()
        def unblock_then_revoke(_address, _blocked):
            access.revoke_guest(OLD)  # revoke arrives while the core unblock RPC is in flight
            return True
        with mock.patch.object(admin, "_set_blocked_contact", side_effect=unblock_then_revoke):
            self.assertFalse(admin.grant(OLD))
        self.assertFalse(admin.is_allowed(OLD))

    def test_grant_fails_closed_if_core_cannot_unblock(self) -> None:
        self.open_guest_session()
        state.data["access"]["blocked"] = [OLD]
        with mock.patch.object(admin, "_set_blocked_contact", return_value=False):
            self.assertFalse(admin.grant(OLD))
        self.assertFalse(admin.is_allowed(OLD))
        self.assertIn(OLD, state.data["access"]["blocked"])

    def test_timeout_and_maintenance_override_the_session(self) -> None:
        with mock.patch("michiflin.access.time.time", return_value=1000.0):
            self.open_guest_session(15)
            with mock.patch.object(admin, "_set_blocked_contact", return_value=True):
                self.assertTrue(admin.grant(OLD))
            generation = state.kill_generation(OLD)
            self.assertTrue(config.service_access_allowed(OLD))
            with mock.patch.object(config, "MAINTENANCE_MODE", True):
                self.assertTrue(admin.is_allowed(OWNER))
                self.assertFalse(admin.is_allowed(OLD))
                self.assertFalse(access.set_owner_only(False, 60, access.snapshot()["nonce"]))
        with mock.patch("michiflin.access.time.time", return_value=1901.0):
            self.assertTrue(access.snapshot()["owner_only"])
            self.assertFalse(admin.is_allowed(OLD))
            self.assertTrue(state.work_interrupted(OLD, generation))

    def test_expired_session_rejects_stale_open_even_without_prior_poll(self) -> None:
        with mock.patch("michiflin.access.time.time", return_value=1000.0):
            self.open_guest_session(15)
            stale_nonce = access.snapshot()["nonce"]
        with mock.patch("michiflin.access.time.time", return_value=1901.0):
            self.assertFalse(access.set_owner_only(False, 60, stale_nonce))
            self.assertTrue(access.snapshot()["owner_only"])

    def test_owner_ui_op_requires_owner_and_closes_access_even_with_kill(self) -> None:
        with mock.patch.object(miniapp.perf, "snapshot", return_value={}), mock.patch.object(miniapp.usage, "report", return_value={}):
            nonce = admin.snapshot()["control_nonce"]
            opened = miniapp.op_owner_only(7, {"enabled": False, "minutes": 60, "nonce": nonce}, "es")
            self.assertFalse(opened["owner_only"])
            with mock.patch.object(admin, "_set_blocked_contact", return_value=True):
                self.assertTrue(admin.grant(OLD))
            state.set_kill_switch(OWNER, True)
            closed = miniapp.op_owner_only(7, {"enabled": True, "minutes": 60}, "en")
            self.assertTrue(closed["owner_only"])
            self.assertFalse(admin.is_allowed(OLD))
            self.assertIn("owner_only", miniapp.KILL_SAFE_OPS)
            with self.assertRaises(miniapp.AppError):
                miniapp.op_owner_only(7, {"enabled": False, "minutes": 60, "nonce": nonce}, "en")

    def test_owner_command_requires_one_time_confirmation_and_is_bilingual(self) -> None:
        sent = []
        with mock.patch.object(chat, "text", side_effect=lambda _chat, text: sent.append(text)):
            for locale, name in (("es", "/solo_dueno"), ("en", "/owner_only")):
                self.assertTrue(commands.dispatch(7, name + " 0 15", locale))
                self.assertTrue(access.snapshot()["owner_only"])
                code = commands._owner_challenges[7][0]
                self.assertTrue(commands.dispatch(7, name + " 0 " + code, locale))
                self.assertFalse(access.snapshot()["owner_only"])
                self.assertTrue(commands.dispatch(7, name + " 1", locale))
                self.assertTrue(access.snapshot()["owner_only"])
                self.assertTrue(commands.dispatch(7, name + " 0 " + code, locale))
                self.assertTrue(access.snapshot()["owner_only"])
        self.assertTrue(any("OWNER_ONLY=0" in message for message in sent))
        commands._owner_challenges.clear()

    def test_defaults_and_persisted_false_take_precedence(self) -> None:
        with mock.patch.dict(state.DEFAULTS, {"kill_switch": True, "nauta_strict": True}):
            self.assertTrue(state.get_kill_switch(OWNER))
            self.assertTrue(state.get_nauta_strict(OWNER))
            state.set_kill_switch(OWNER, False)
            state.set_nauta_strict(OWNER, False)
            self.assertFalse(state.get_kill_switch(OWNER))
            self.assertFalse(state.get_nauta_strict(OWNER))

    def test_private_mode_requires_explicit_owner_not_allowlist(self) -> None:
        for mode in ("OWNER_ONLY", "MAINTENANCE_MODE"):
            env = dict(os.environ, OWNER_ONLY="0", MAINTENANCE_MODE="0", OWNER_ADDRESS="", ALLOWED_SENDERS="other@example.org")
            env[mode] = "1"
            result = subprocess.run([sys.executable, "-c", "from michiflin import config"], cwd=_ROOT, env=env,
                                    capture_output=True, text=True, timeout=20)
            self.assertNotEqual(result.returncode, 0, mode)
            self.assertIn("requires OWNER_ADDRESS", result.stderr)

    def test_private_mode_never_grants_or_alerts_old_allowlisted_admin(self) -> None:
        self.assertTrue(admin.is_allowed(OWNER))
        self.assertFalse(admin.is_allowed(OLD))
        self.assertFalse(admin.grant(OLD))
        self.assertNotIn("To grant access", admin.alert_text(OLD, "hello"))
        with mock.patch.object(config, "ADMIN_ADDRESSES", {OWNER, OLD}), mock.patch.object(chat, "ready", return_value=True):
            rpc = mock.Mock()
            rpc.lookup_contact_id_by_addr.side_effect = lambda _acc, address: 1 if address == OWNER else 2
            rpc.get_chat_id_by_contact_id.side_effect = lambda _acc, contact: contact + 10
            with mock.patch.object(chat, "rpc", return_value=rpc), mock.patch.object(chat, "account", return_value=1):
                self.assertEqual(admin.admin_chats(), [11])
        snap = admin.snapshot()
        self.assertTrue(snap["private_mode"])
        self.assertNotIn(OLD, [row["address"] for row in snap["users"]])
        self.assertNotIn(OLD, admin.overview("en"))
        self.assertIn(OLD, state.data["access"]["allowed"])  # hidden, not purged

    def test_private_snapshot_shows_only_recent_attempts_without_purging_history(self) -> None:
        now = time.time()
        access = state.data["access"]
        access["blocked"] = [OLD, "new@example.org"]
        access["attempts"] = {
            OLD: {"count": 2, "last": now - 3 * 86400, "sample": "old"},
            "new@example.org": {"count": 1, "last": now, "sample": "hello"},
        }
        snap = admin.snapshot()
        self.assertEqual([item["address"] for item in snap["attempts"]], ["new@example.org"])
        self.assertEqual(snap["blocked"], [])
        self.assertIn(OLD, access["blocked"])
        self.assertIn(OLD, access["attempts"])
        self.assertFalse(admin.grant("new@example.org"))

    def test_blocked_message_text_is_not_newly_stored_or_redisplayed(self) -> None:
        secret = "new-private-message-do-not-store"
        address = "new-attempt@example.org"
        with mock.patch.object(state, "save"):
            admin.record_attempt(address, secret)
        self.assertNotIn(secret, json.dumps(state.data["access"]["attempts"][address]))
        self.assertNotIn(secret, admin.alert_text(address, secret))
        state.data["access"]["attempts"][address]["sample"] = "historical-private-sample"
        snap = admin.snapshot()
        self.assertNotIn("historical-private-sample", json.dumps(snap))
        self.assertEqual(state.data["access"]["attempts"][address]["sample"], "historical-private-sample")

    def test_admin_diagnostics_redact_repeat_and_keep_details(self) -> None:
        admin.clear_logs()
        self.addCleanup(admin.clear_logs)
        fake = "ghp_" + "Z" * 36
        secret = "sample-session-value"
        try:
            raise ValueError("Failure with " + fake + " and " + "x" * 500)
        except ValueError:
            record = logging.LogRecord("michiflin.fake", logging.ERROR, __file__, 100,
                                       f"Address private@example.org IPs 192.0.2.8 [2001:db8:85a3::8a2e:370:7334] URL https://example.org/path?token={secret} Cookie: a={secret}; b={secret}", (), sys.exc_info())
            handler = admin._Buffer()
            handler.emit(record)
            handler.emit(record)
            filtered = copy.copy(record)
            app._Redact().filter(filtered)
        snap = admin.snapshot()
        entry = snap["log_entries"][0]
        text = json.dumps(entry) + str(filtered.msg) + str(filtered.exc_text)
        self.assertEqual(entry["count"], 2)
        self.assertIn("ValueError", entry["detail"])
        self.assertGreater(len(entry["detail"]), 220)
        for sensitive in (fake, secret, "private@example.org", "https://example.org", "192.0.2.8", "2001:db8:85a3::8a2e:370:7334"):
            self.assertNotIn(sensitive, text)
        self.assertIn("[credential]", text)

    def test_ip_redaction_preserves_file_line_numbers_and_times(self) -> None:
        text = admin.redact_log("src/imap/client.rs:163: at 12:30:00 [2001:db8::1] and 192.0.2.4")
        self.assertIn("client.rs:163:", text)
        self.assertIn("12:30:00", text)
        self.assertNotIn("2001:db8::1", text)
        self.assertNotIn("192.0.2.4", text)

    def test_core_diagnostics_keep_context_without_leaking_known_secret(self) -> None:
        secret = "ghp_" + "Q" * 36
        app._core_seen.clear()
        self.addCleanup(app._core_seen.clear)
        event = app.EventTypeWarning(msg="diagnostic " + "x" * 500 + " " + secret)
        with mock.patch.object(app.log, "log") as emit:
            app.on_core_event(None, 0, event)
        text = str(emit.call_args)
        self.assertIn("x" * 400, text)
        self.assertNotIn(secret, text)
        self.assertIn("[credential]", text)

    def test_clear_logs_preserves_other_state(self) -> None:
        before = copy.deepcopy(state.data["access"])
        admin._Buffer().emit(logging.LogRecord("michiflin.fake", logging.WARNING, __file__, 1, "warning", (), None))
        self.assertTrue(admin.snapshot()["log_entries"])
        admin.clear_logs()
        self.assertFalse(admin.snapshot()["log_entries"])
        self.assertEqual(state.data["access"], before)

    def test_nauta_never_increases_a_verified_smaller_ceiling(self) -> None:
        state.set_nauta_strict(OWNER, True)
        state.set_setting("part_mb", 20)
        for size in range(1, 8):
            profile = json.dumps({OWNER: {"verified": True, "safe_part_mb": size,
                                          "source": "test fixture", "checked_at": "2026-10-27"}})
            with self.subTest(size=size), mock.patch.object(config, "MAILBOX_PROFILES_JSON", profile):
                job = queue._new_job(3, "file", "https://example.org/a", "a", 100, part_mb=20)
                view = miniapp.op_nauta_strict(3, {}, "en")
                self.assertEqual(job["part_mb"], size)
                self.assertEqual(view["part_mb"], size)
                self.assertTrue(view["active"])

    def test_nauta_respects_a_user_setting_below_eight(self) -> None:
        state.set_nauta_strict(OWNER, True)
        state.set_setting("part_mb", 2)
        self.assertEqual(queue._new_job(1, "file", "https://example.org/a", "a", 0)["part_mb"], 2)

    def test_worker_started_after_on_off_transition_is_blocked(self) -> None:
        handler = mock.Mock(return_value={"unexpected": True})
        responses = []
        test = self

        class ToggleBeforeRun(Immediate):
            def submit(self, function, *args):
                test.toggle_once()
                return super().submit(function, *args)

        entry = {"chat": 7, "addr": OWNER, "build": miniapp.app_build()}
        with mock.patch.dict(miniapp.OPS, {"race_test": handler}), mock.patch.object(miniapp, "_workers", ToggleBeforeRun()), \
                mock.patch.object(miniapp, "respond", side_effect=lambda *args: responses.append(args)):
            miniapp.handle(42, entry, {"id": "race", "op": "race_test", "lang": "en"})
        handler.assert_not_called()
        self.assertIn("kill switch", responses[0][3].lower())

    def test_queued_dispatch_remembers_admission_even_after_off(self) -> None:
        calls, responses = [], []
        test = self

        class ToggleBeforeDispatch(Immediate):
            def submit(self, function, *args):
                test.toggle_once()
                return super().submit(function, *args)

        with mock.patch.object(miniapp, "_admit", return_value="new"), mock.patch.object(miniapp, "_pool", ToggleBeforeDispatch()), \
                mock.patch.dict(miniapp.OPS, {"race_test": lambda *a: calls.append(1)}), \
                mock.patch.object(miniapp, "respond", side_effect=lambda *args: responses.append(args)):
            miniapp._route(42, {"chat": 7, "addr": OWNER}, {"id": "race2", "op": "race_test", "lang": "es", "session": miniapp.BOOT_SESSION})
        self.assertEqual(calls, [])
        self.assertIn("kill switch", responses[0][3].lower())

    def test_emergency_control_does_not_share_saturated_work_pool(self) -> None:
        ordinary = mock.Mock()
        ordinary.submit.side_effect = AssertionError("ordinary pool is saturated")
        handler = mock.Mock()
        with mock.patch.object(miniapp, "_admit", return_value="new"), mock.patch.object(miniapp, "_pool", ordinary), \
                mock.patch.object(miniapp, "_control_pool", Immediate()), mock.patch.object(miniapp, "handle", handler):
            miniapp._route(42, {"chat": 7, "addr": OWNER}, {"id": "emergency", "op": "kill_switch", "lang": "en"})
        handler.assert_called_once()
        ordinary.submit.assert_not_called()

    def test_status_remains_available_during_kill_in_both_languages(self) -> None:
        state.set_kill_switch(OWNER, True)
        for language, expected in (("es", "ACTIVO"), ("en", "ON")):
            responses = []
            with mock.patch.object(miniapp, "_workers", Immediate()), \
                    mock.patch.object(miniapp, "respond", side_effect=lambda *args: responses.append(args)):
                miniapp.handle(42, {"chat": 7, "addr": OWNER, "build": miniapp.app_build()}, {"id": language, "op": "estado", "lang": language})
            self.assertIn(expected, responses[0][2][language])
            self.assertTrue(responses[0][2]["killed"])

    def test_owner_can_use_safe_text_commands_in_app_mode(self) -> None:
        state.set_setting("chat_mode", "app")
        msg = SimpleNamespace(id=55, chat_id=7, from_id=3, file=None)
        with mock.patch.object(chat, "send", return_value=1) as send, mock.patch.object(progress, "begin"), \
                mock.patch.object(progress, "end"):
            app._process(msg, "/kill_switch sí", "es", OWNER)
            self.assertTrue(state.get_kill_switch(OWNER))
            app._process(msg, "/status", "en", OWNER)
            self.assertIn("ON", str(send.call_args))
            app._process(msg, "/kill_switch no", "es", OWNER)
            self.assertFalse(state.get_kill_switch(OWNER))

    def test_authorized_text_activation_preempts_queued_processing(self) -> None:
        rpc = mock.Mock()
        rpc.get_basic_chat_info.return_value = SimpleNamespace(chat_type="Single", is_contact_request=False)
        rpc.get_contact.return_value = SimpleNamespace(address=OWNER)
        msg = SimpleNamespace(id=55, from_id=3, chat_id=7, text="/kill_switch sí", file=None)
        app._chat_busy.add(7)  # An earlier request has not completed.
        try:
            app._accept(SimpleNamespace(rpc=rpc), 1, msg)
            self.assertTrue(state.get_kill_switch(OWNER))
            self.assertIn(55, app._work_tickets)
            self.assertEqual(len(app._chat_queues[7]), 1)
        finally:
            app._work_tickets.pop(55, None)
            app._chat_queues.pop(7, None)
            app._chat_busy.discard(7)
            app._taken.discard(55)

    def test_old_sender_cannot_activate_kill_even_when_on_allowlist(self) -> None:
        rpc = mock.Mock()
        rpc.get_basic_chat_info.return_value = SimpleNamespace(chat_type="Single", is_contact_request=False)
        rpc.get_contact.return_value = SimpleNamespace(address=OLD)
        msg = SimpleNamespace(id=56, from_id=3, chat_id=7, text="/kill_switch sí", file=None)
        with mock.patch.object(app, "_refuse"):
            self.assertIsNone(app._screen(SimpleNamespace(rpc=rpc), 1, msg))
        self.assertFalse(state.get_kill_switch(OLD))

    def test_cleanup_does_not_delete_finished_old_user_files(self) -> None:
        old = {"id": "8", "addr": OLD, "chat": 70, "status": "done", "kind": "file", "title": "old",
               "updated": time.time() - 10_000_000}
        state.data["jobs"] = {"8": old}
        folder = queue.folder(old)
        folder.mkdir(parents=True)
        artifact = folder / "keep.txt"
        artifact.write_text("historical data")
        with mock.patch.object(queue, "free_disk", return_value=10**15), mock.patch.object(chat, "delete_for_all") as delete:
            queue.cleanup()
            queue.recover()
        self.assertTrue(artifact.exists())
        self.assertIn("8", state.data["jobs"])
        delete.assert_not_called()

    def test_ai_fallback_stops_after_an_inflight_provider(self) -> None:
        models = [ai.Model("mock", "a", {"chat"}, 10), ai.Model("mock", "b", {"chat"}, 10)]
        provider = ai.Provider("mock", "Mock", "openai", "https://example.org")

        def first_call(*_args):
            self.toggle_once()
            return ai.AIResult(error="connection")

        with self.assertRaises(state.WorkPaused), state.guard_work(OWNER), mock.patch.object(ai, "candidates", return_value=models), \
                mock.patch.object(ai, "provider", return_value=provider), mock.patch.object(ai, "available", return_value=True), \
                mock.patch.object(progress, "phase"), mock.patch.object(ai, "_call", side_effect=first_call) as call:
            ai._complete([], ["chat"], 5, None, "", False, None)
        self.assertEqual(call.call_count, 1)

    def test_agent_does_not_start_another_search_after_activation(self) -> None:
        def interrupted_gather(*_args, **_kwargs):
            self.toggle_once()
            return []

        with self.assertRaises(state.WorkPaused), state.guard_work(OWNER), mock.patch.object(progress, "phase"), \
                mock.patch.object(answer, "gather", side_effect=interrupted_gather) as gather:
            agent.reply(7, "a real question", "en")
        self.assertEqual(gather.call_count, 1)

    def test_image_fallback_stops_after_activation(self) -> None:
        sources = [images.Source("first", "First", 5), images.Source("second", "Second", 5)]

        def first(*_args, **_kwargs):
            self.toggle_once()
            return b""

        with self.assertRaises(state.WorkPaused), state.guard_work(OWNER), mock.patch.object(images, "_ordered", return_value=sources), \
                mock.patch.object(images, "english_prompt", return_value="cat"), mock.patch.object(progress, "phase"), \
                mock.patch.object(images, "_run", side_effect=first) as run:
            images._generate("cat")
        self.assertEqual(run.call_count, 1)

    def test_coder_wont_execute_generated_actions_after_activation(self) -> None:
        def model(*_args):
            self.toggle_once()
            return ai.AIResult(text="<<<FILE p.py>>>\nprint(123)\n<<<END>>>")

        with self.assertRaises(state.WorkPaused), state.guard_work(OWNER), mock.patch.object(coder, "ROOT", Path(self.temp.name) / "coder"), \
                mock.patch.object(coder, "isolation", return_value=False), mock.patch.object(progress, "phase"), \
                mock.patch.object(coder, "_ask", side_effect=model) as ask, mock.patch.object(coder, "_run") as run:
            coder.run("write a program", [], "en")
        self.assertEqual(ask.call_count, 1)
        run.assert_not_called()

    def test_old_jobs_and_pushes_blocked_without_erasing_state(self) -> None:
        old = {"id": "7", "addr": OLD, "chat": 70, "status": "queued", "kind": "file", "title": "old", "size_est": 50}
        state.data["jobs"] = {"7": old}
        state.data["apps"] = {"123": {"chat": 70, "addr": OLD, "serial": 0}}
        self.assertIsNone(queue._next_queued())
        self.assertEqual(queue.active_count(), 0)
        with mock.patch.object(chat, "ready", return_value=True), mock.patch.object(chat, "send_update") as send, \
                mock.patch.object(chat, "delete_for_all") as delete:
            miniapp.push(70, "job", {})
            queue.recover()
        send.assert_not_called()
        delete.assert_not_called()
        self.assertEqual(state.data["jobs"]["7"]["status"], "queued")
        self.assertIn("123", state.data["apps"])

    def test_private_outbox_purge_is_scoped_to_owner_chat(self) -> None:
        rpc = mock.Mock()
        rpc.get_chatlist_entries.return_value = [7, 70]
        rpc.get_message_ids.return_value = [44]
        rpc.get_message.return_value = SimpleNamespace(from_id=1, state=20)
        with mock.patch.object(chat, "_bot", SimpleNamespace(rpc=rpc)), mock.patch.object(chat, "_accid", 1), \
                mock.patch.object(chat, "delete") as delete:
            self.assertEqual(chat.purge_outbox(), 0)
            self.assertEqual(chat.purge_outbox(chat_id=7), 1)
        rpc.get_chatlist_entries.assert_not_called()
        rpc.get_message_ids.assert_called_once_with(1, 7, False, False)
        delete.assert_called_once_with([44])

    def test_private_cleanup_and_usage_reset_cannot_remove_old_data(self) -> None:
        state.data["jobs"] = {"8": {"id": "8", "chat": 7, "addr": OLD, "status": "done", "kind": "file", "title": "keep"}}
        state.data["people"] = {OWNER: {"2026-10-27": [1, 0, 0, 0, 0, 0]}, OLD: {"2026-10-27": [2, 0, 0, 0, 0, 0]}}
        state.data["prefs"] = {OLD: {"language": "en"}}
        state.data["apps"] = {"99": {"chat": 7, "addr": OLD}}
        self.assertIsNone(queue.get("8"))
        self.assertEqual(miniapp.jobs_view(7, "en"), [])
        self.assertIn("0 finished", queue.clear_finished(7, "en"))
        usage.clear()
        with mock.patch.object(admin, "_set_blocked_contact"):
            self.assertTrue(admin.revoke(OLD, block=True))
        self.assertIn("8", state.data["jobs"])
        self.assertIn(OLD, state.data["people"])
        self.assertIn(OLD, state.data["prefs"])
        self.assertIn("99", state.data["apps"])
        self.assertNotIn(OLD, usage.totals())

    def test_controlled_session_preserves_old_data_even_when_boot_default_was_shared(self) -> None:
        with mock.patch.object(config, "OWNER_ONLY", False):
            access.reset()
            self.open_guest_session()
            with mock.patch.object(admin, "_set_blocked_contact", return_value=True):
                self.assertTrue(admin.grant(OLD))
            state.data["people"] = {OLD: {f"2000-01-{day:02d}": [1, 0, 0, 0, 0, 0] for day in range(1, 12)}}
            state.data["sent"] = [[1, 123, OLD], [1, 456, "another@example.org"]]
            state.data["usage"]["2000-01-01"] = {"historical": [1, 0, 0]}
            with mock.patch.object(state, "_save_soon"):
                usage.note(OLD, requests=1)
            usage.clear()
            self.assertIn("2000-01-01", state.data["people"][OLD])
            with (mock.patch.object(state, "CONFIG_DIR", Path(self.temp.name)),
                  mock.patch.object(state, "STATE_FILE", Path(self.temp.name) / "state.json")):
                REAL_SAVE()
            self.assertIn("2000-01-01", state.data["usage"])
            self.assertIn([1, 123, OLD], state.data["sent"])
            self.assertIn([1, 456, "another@example.org"], state.data["sent"])
            self.assertTrue(admin.snapshot()["preserve_history"])
            with mock.patch.object(chat, "purge_outbox", return_value=0) as purge:
                miniapp.op_admin(7, {"do": "purge"}, "en")
                purge.assert_called_once_with(chat_id=7)

    def test_send_waiting_for_rpc_lock_rechecks_kill(self) -> None:
        part = Path(self.temp.name) / "waiting.txt"
        part.write_text("abc")
        job = {"id": "25", "addr": OWNER, "chat": 7, "status": "sending", "kind": "file", "title": "x",
               "name": part.name, "size": 3, "part_mb": 10, "part_files": [str(part)], "parts": 1,
               "todo": [0], "sent": 0, "hold": False, "not_before": 0,
               "delivery_delay": int(config.DEFAULTS["delay"]), "delivery_batch": 0, "delivery_daily_cap_mb": 0}
        state.data["jobs"] = {"25": job}
        reached = threading.Event()
        rpc = mock.Mock()
        def caption(*_args):
            reached.set()
            return "file"
        with mock.patch.object(chat, "_bot", SimpleNamespace(rpc=rpc)), mock.patch.object(chat, "_accid", 1), \
                mock.patch.object(queue, "_caption", side_effect=caption):
            with chat._lock:
                thread = threading.Thread(target=queue._send_one, args=(job,))
                thread.start()
                self.assertTrue(reached.wait(3))
                state.set_kill_switch(OWNER, True)
            thread.join(3)
        self.assertFalse(thread.is_alive())
        rpc.send_msg.assert_not_called()
        self.assertEqual(job["todo"], [0])

    def test_slow_send_rpc_does_not_block_kill_activation(self) -> None:
        part = Path(self.temp.name) / "slow.txt"
        part.write_text("abc")
        job = {"id": "26", "addr": OWNER, "chat": 7, "status": "sending", "kind": "file", "title": "x",
               "name": part.name, "size": 3, "part_mb": 10, "part_files": [str(part)], "parts": 1,
               "todo": [0], "sent": 0, "hold": False, "not_before": 0,
               "delivery_delay": int(config.DEFAULTS["delay"]), "delivery_batch": 0, "delivery_daily_cap_mb": 0}
        state.data["jobs"] = {"26": job}
        reached, release, activated = threading.Event(), threading.Event(), threading.Event()
        def slow_send(*_args):
            reached.set()
            if not release.wait(4):
                raise AssertionError("RPC not released")
            return 77
        rpc = SimpleNamespace(send_msg=slow_send)
        def activate():
            state.set_kill_switch(OWNER, True)
            activated.set()
        with mock.patch.object(chat, "_bot", SimpleNamespace(rpc=rpc)), mock.patch.object(chat, "_accid", 1), \
                mock.patch.object(chat, "wait_delivery", return_value="delivered"):
            sender = threading.Thread(target=queue._send_one, args=(job,))
            sender.start()
            self.assertTrue(reached.wait(3))
            switch = threading.Thread(target=activate)
            switch.start()
            worked = activated.wait(1)
            release.set()
            sender.join(3)
            switch.join(3)
        self.assertTrue(worked, "kill activation was blocked by an in-flight RPC")
        self.assertEqual(job["sent"], 1)  # already handed off; do not resend blindly
        self.assertEqual(job["todo"], [])

    def test_inflight_message_is_saved_before_send_and_reconciled_after_crash(self) -> None:
        part = Path(self.temp.name) / "crash.txt"
        part.write_text("abc")
        job = {"id": "27", "addr": OWNER, "chat": 7, "status": "sending", "kind": "file", "title": "x",
               "name": part.name, "size": 3, "part_mb": 10, "part_files": [str(part)], "parts": 1,
               "todo": [0], "sent": 0, "hold": False, "not_before": 0,
               "delivery_delay": int(config.DEFAULTS["delay"]), "delivery_batch": 0, "delivery_daily_cap_mb": 0}
        state.data["jobs"] = {"27": job}
        directory = Path(self.temp.name) / "persistent"
        path = directory / "state.json"
        def rpc_send(*_args):
            saved = json.loads(path.read_text())
            self.assertEqual(saved["jobs"]["27"]["inflight"]["msg_id"], 0)
            return 88
        with mock.patch.object(state, "CONFIG_DIR", directory), mock.patch.object(state, "STATE_FILE", path), \
                mock.patch.object(state, "save", side_effect=REAL_SAVE), \
                mock.patch.object(chat, "_bot", SimpleNamespace(rpc=SimpleNamespace(send_msg=rpc_send))), \
                mock.patch.object(chat, "_accid", 1), mock.patch.object(chat, "wait_delivery", side_effect=RuntimeError("simulated crash")):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                queue._send_one(job)
            self.assertEqual(json.loads(path.read_text())["jobs"]["27"]["inflight"]["msg_id"], 88)
        part.unlink()  # temporary files might disappear across sessions
        queue.recover()
        self.assertEqual(job["status"], "sending")
        with mock.patch.object(chat, "send") as send, mock.patch.object(chat, "wait_delivery", return_value="delivered") as wait:
            queue._send_one(job)
        send.assert_not_called()
        self.assertEqual(wait.call_args.args[0], 88)
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["todo"], [])

    def test_uncertain_delivery_holds_instead_of_resending(self) -> None:
        job = {"id": "28", "addr": OWNER, "chat": 7, "status": "sending", "kind": "file", "title": "x",
               "parts": 1, "todo": [0], "sent": 0, "hold": False, "not_before": 0, "part_mb": 10,
               "delivery_delay": int(config.DEFAULTS["delay"]), "inflight": {"index": 0, "size": 3, "msg_id": 0}}
        state.data["jobs"] = {"28": job}
        with mock.patch.object(chat, "send") as send:
            queue._send_one(job)
            self.assertTrue(job["hold"])
            self.assertIn("uncertain", queue.hold("28", False, "en"))
        send.assert_not_called()
        self.assertEqual(job["todo"], [0])

    def test_timed_out_handoff_rechecks_same_message_not_new_copy(self) -> None:
        job = {"id": "29", "addr": OWNER, "chat": 7, "status": "sending", "kind": "file", "title": "x",
               "parts": 1, "todo": [0], "sent": 0, "hold": False, "not_before": 0, "part_mb": 10,
               "delivery_delay": int(config.DEFAULTS["delay"]), "delivery_batch": 0, "delivery_daily_cap_mb": 0,
               "inflight": {"index": 0, "size": 3, "msg_id": 89}}
        state.data["jobs"] = {"29": job}
        with mock.patch.object(chat, "send") as send, mock.patch.object(chat, "delete") as delete, \
                mock.patch.object(chat, "wait_delivery", side_effect=["timeout", "delivered"]) as wait:
            queue._send_one(job)
            self.assertTrue(job["hold"])
            self.assertEqual(job["inflight"]["msg_id"], 89)
            queue.hold("29", False, "en")
            queue._send_one(job)
        send.assert_not_called()
        delete.assert_not_called()
        self.assertEqual([call.args[0] for call in wait.call_args_list], [89, 89])
        self.assertEqual(job["status"], "done")

    def test_completed_todo_is_not_refilled_after_repreparing(self) -> None:
        import hashlib
        content = b"already delivered"
        job = queue._new_job(7, "file", "https://example.org/f", "f.txt", len(content),
                             name="f.txt", ext="txt", mime="text/plain")
        job.update(status="queued", sha=hashlib.sha256(content).hexdigest(), sent=1, todo=[])
        state.data["jobs"][job["id"]] = job
        def download(_url, path, *_args):
            path.write_bytes(content)
        with mock.patch.object(files, "download", side_effect=download):
            queue._prepare(job)
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["todo"], [])

    def test_media_downloader_interrupts_after_progress_hook(self) -> None:
        generation = state.kill_generation(OWNER)
        test = self
        class FakeDownloader:
            def __init__(self, options):
                self.options = options
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
            def download(self, _urls):
                test.toggle_once()
                self.options["progress_hooks"][0]({"status": "downloading", "downloaded_bytes": 5})
        with mock.patch.object(media, "supported", return_value=True), mock.patch.object(media, "_routes", return_value=[""]), \
                mock.patch.object(media.yt_dlp, "YoutubeDL", FakeDownloader):
            with self.assertRaises(InterruptedError):
                media.download("https://example.org/a", Path(self.temp.name) / "media", "video", 360, "m4a",
                               stop=lambda: state.work_interrupted(OWNER, generation))

    def test_unknown_fields_and_historical_counters_survive_load_and_save(self) -> None:
        directory = Path(self.temp.name) / "state"
        directory.mkdir()
        path = directory / "state.json"
        original = {"version": state.VERSION, "future_key": {"keep": [1, 2]},
                    "usage": {"2000-01-01": {"old": [1, 0, 0]}},
                    "sent": [[1, 30, OLD], [1, 20]], "people": {OLD: {"2000-01-01": [2]}}}
        path.write_text(json.dumps(original))
        with mock.patch.object(state, "CONFIG_DIR", directory), mock.patch.object(state, "STATE_FILE", path), \
                mock.patch.object(state, "LEGACY_STATE_FILE", directory / "legacy-absent"):
            state.load()
            REAL_SAVE()
        after = json.loads(path.read_text())
        for key in ("future_key", "sent", "people"):
            self.assertEqual(after[key], original[key])
        self.assertIn("2000-01-01", after["usage"])

    def test_delivery_claim_invalidated_even_if_kill_was_turned_off(self) -> None:
        part = Path(self.temp.name) / "part.txt"
        part.write_text("abc")
        job = {"id": "20", "addr": OWNER, "chat": 7, "status": "sending", "kind": "file", "title": "x",
               "name": "part.txt", "part_mb": 10, "part_files": [str(part)], "parts": 1,
               "todo": [0], "hold": False, "not_before": 0}
        state.data["jobs"] = {"20": job}
        self.assertIs(queue._deliverable(), job)
        self.toggle_once()
        with mock.patch.object(chat, "send") as send:
            queue._send_one(job)
        send.assert_not_called()
        self.assertEqual(job["todo"], [0])

    def test_already_handed_off_part_is_accounted_for_without_duplicate(self) -> None:
        part = Path(self.temp.name) / "part.txt"
        part.write_text("abc")
        job = {"id": "21", "addr": OWNER, "chat": 7, "status": "sending", "kind": "file", "title": "x",
               "name": "part.txt", "ext": "txt", "size": 3, "part_mb": 10, "part_files": [str(part)], "parts": 1,
               "todo": [0], "sent": 0, "hold": False, "not_before": 0,
               "delivery_delay": int(config.DEFAULTS["delay"]), "delivery_batch": 0, "delivery_daily_cap_mb": 0}
        state.data["jobs"] = {"21": job}
        def delivered(*_args):
            state.set_kill_switch(OWNER, True)
            return "delivered"
        with mock.patch.object(chat, "send", return_value=77) as send, mock.patch.object(chat, "wait_delivery", side_effect=delivered):
            queue._send_one(job)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["sent"], 1)
        self.assertEqual(job["todo"], [])

    def test_active_file_download_pauses_and_resumes_with_real_downloader(self) -> None:
        reached, release = threading.Event(), threading.Event()
        initial = b"a" * 300
        remainder = b"b" * 300
        requests_seen = []

        class Response:
            def __init__(self, resumed):
                self.status_code = 206 if resumed else 200
                self.resumed = resumed
                self.headers = {"ETag": '"fixture-v1"', "Content-Range": "bytes 300-599/600"} if resumed else {"ETag": '"fixture-v1"'}
            def iter_content(self, chunk_size):
                if not self.resumed:
                    yield initial
                    reached.set()
                    if not release.wait(4):
                        raise AssertionError("test did not release the stream")
                yield remainder
            def close(self):
                pass

        def fetch(_url, **kwargs):
            requests_seen.append(kwargs["headers"].get("Range", ""))
            return Response(bool(requests_seen[-1]))

        job = queue._new_job(7, "file", "https://example.org/file.txt", "file.txt", 600,
                             name="file.txt", ext="txt", mime="text/plain", resumable=True)
        state.data["jobs"][job["id"]] = job
        errors = []

        def prepare():
            try:
                queue._prepare(job)
            except InterruptedError:
                queue._interrupted_preparation(job)
            except Exception as exc:
                errors.append(exc)

        with mock.patch.object(files.net, "fetch", side_effect=fetch):
            thread = threading.Thread(target=prepare)
            thread.start()
            self.assertTrue(reached.wait(4))
            state.set_kill_switch(OWNER, True)
            release.set()
            thread.join(4)
            self.assertFalse(thread.is_alive())
            self.assertFalse(errors)
            self.assertEqual(job["status"], "queued")
            self.assertEqual((queue.folder(job) / "src" / "download.bin").read_bytes(), initial)
            state.set_kill_switch(OWNER, False)
            queue._prepare(job)
        self.assertEqual(requests_seen, ["", "bytes=300-"])
        self.assertEqual(Path(job["part_files"][0]).read_bytes(), initial + remainder)
        self.assertIn(job["status"], {"ready", "sending"})

    def test_packer_process_stops_cooperatively(self) -> None:
        stop = threading.Event()
        timer = threading.Timer(0.2, stop.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(InterruptedError):
                queue._run_interruptible([sys.executable, "-c", "import time; time.sleep(8)"], 10, stop.is_set)
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 4)

    def test_unsupported_state_is_not_overwritten(self) -> None:
        path = Path(self.temp.name) / "state.json"
        for content in ('{"version":999,"notes":{"keep":"yes"}}', '{not json', '{"version":5,"jobs":[]}'):
            path.write_text(content)
            with mock.patch.object(state, "STATE_FILE", path), mock.patch.object(state, "LEGACY_STATE_FILE", Path(self.temp.name) / "absent"):
                with self.assertRaises(RuntimeError):
                    state.load()
            self.assertEqual(path.read_text(), content)


if __name__ == "__main__":
    unittest.main()
