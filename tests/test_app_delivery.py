"""Synthetic delivery tests: automatic apps must not race or discard data."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from michiflin import admin, app_compat, chat, config, live_web, miniapp, state

OWNER = "owner@example.invalid"
CHAT = 91


@pytest.fixture
def app_state(monkeypatch):
    old = {"chat": CHAT, "addr": OWNER, "build": "old-build", "locale": "en", "sent": time.time() - 3600,
           "serial": 0, "epoch": "test-epoch", "access_session": ""}
    monkeypatch.setitem(state.data, "apps", {"100": old})
    monkeypatch.setattr(state, "save", lambda: None)
    monkeypatch.setattr(config, "BOT_EPOCH", "test-epoch")
    monkeypatch.setattr(config, "BOT_EPOCH_STRICT", True)
    monkeypatch.setattr(config, "BOT_VERIFIED_CONTACTS_ONLY", False)
    monkeypatch.setattr(miniapp, "_verified_binding", lambda _entry: True)
    monkeypatch.setattr(miniapp.access, "bound_allowed", lambda entry: entry.get("addr") == OWNER)
    monkeypatch.setattr(admin, "is_allowed", lambda address: address == OWNER)
    monkeypatch.setattr(config, "service_access_allowed", lambda address: address == OWNER)
    monkeypatch.setattr(miniapp, "app_build", lambda: "new-build")
    monkeypatch.setattr(miniapp, "build", lambda: Path("/tmp/synthetic-app.xdc"))
    monkeypatch.setattr(chat, "sender", lambda: OWNER)
    monkeypatch.setattr(live_web, "_identity", lambda msg_id: (state.data["apps"][str(msg_id)], OWNER))
    monkeypatch.setattr(miniapp, "_upgraded", {})
    monkeypatch.setattr(miniapp, "_pointed", {})
    return old


@pytest.mark.parametrize("other", ["welcome", "point"])
def test_concurrent_first_welcome_and_pointer_send_at_most_one_app(app_state, monkeypatch, other):
    """A first delivery and a competing help path share one handoff lock."""
    state.data["apps"] = {}
    sent, errors = [], []
    entered, release = threading.Event(), threading.Event()

    def send(_chat_id, text="", **kwargs):
        if kwargs.get("viewtype") != "Webxdc":
            return 900
        sent.append(text)
        entered.set()
        assert release.wait(3)
        return 201

    monkeypatch.setattr(chat, "send", send)
    monkeypatch.setattr(miniapp, "latest_instance", lambda _chat: 0)
    def run(action):
        try:
            action()
        except BaseException as exc:
            errors.append(exc)
    first = threading.Thread(target=run, args=(lambda: miniapp.welcome(CHAT, "en"),), daemon=True)
    second_action = (lambda: miniapp.welcome(CHAT, "es")) if other == "welcome" else (lambda: miniapp.point_to_app(CHAT, "es"))
    second = threading.Thread(target=run, args=(second_action,), daemon=True)
    try:
        first.start()
        assert entered.wait(2)
        second.start()
    finally:
        release.set()
        first.join(3)
        if second.ident is not None:
            second.join(3)
    assert not first.is_alive() and not second.is_alive() and not errors
    assert len(sent) == 1 and "201" in state.data["apps"]


@pytest.mark.parametrize("path", ["welcome", "point"])
def test_older_authorized_app_is_never_automatically_replaced(app_state, monkeypatch, path):
    monkeypatch.setattr(miniapp, "latest_instance", lambda _chat: 0)
    send = mock.Mock(return_value=201)
    monkeypatch.setattr(chat, "send", send)
    if path == "welcome":
        miniapp.welcome(CHAT, "es")
    else:
        miniapp.point_to_app(CHAT, "es")
    assert not any(call.kwargs.get("viewtype") == "Webxdc" for call in send.call_args_list)
    assert set(state.data["apps"]) == {"100"}


def test_two_welcome_texts_after_a_transient_lookup_failure_are_deduplicated(app_state, monkeypatch):
    """A welcome followed by an automatic pointer must not send another welcome."""
    state.data["apps"] = {}
    sent = []

    def send(_chat, text="", **kwargs):
        sent.append((text, kwargs))
        return 200 + len(sent)

    def unreadable_message(*_args):
        raise ConnectionError("synthetic temporary core outage")

    monkeypatch.setattr(chat, "send", send)
    monkeypatch.setattr(chat, "rpc", lambda: SimpleNamespace(get_message=unreadable_message))
    monkeypatch.setattr(chat, "account", lambda: 1)
    miniapp.welcome(CHAT, "en")
    miniapp.point_to_app(CHAT, "es")
    apps = [text for text, kwargs in sent if kwargs.get("viewtype") == "Webxdc"]
    assert len(apps) == 1 and "Hi!" in apps[0]
    assert "201" in state.data["apps"]
    assert any("no se ha eliminado" in text for text, kwargs in sent if not kwargs.get("viewtype"))


def test_repeated_welcome_does_not_resend_a_known_current_app(app_state, monkeypatch):
    app_state.update(build="new-build", sent=time.time() - 3600)
    send = mock.Mock(return_value=201)
    monkeypatch.setattr(chat, "send", send)
    miniapp.welcome(CHAT, "es")
    send.assert_not_called()
    assert "100" in state.data["apps"]


def test_temporary_lookup_failure_does_not_automatically_replace_known_current_app(app_state, monkeypatch):
    app_state.update(build="new-build", sent=time.time() - 3600)
    monkeypatch.setattr(miniapp, "latest_instance", lambda _chat: 0)
    send = mock.Mock(return_value=201)
    monkeypatch.setattr(chat, "send", send)
    miniapp.point_to_app(CHAT, "es")
    assert all(not call.kwargs.get("viewtype") == "Webxdc" for call in send.call_args_list)
    assert "100" in state.data["apps"]


def test_foreign_epoch_binding_does_not_suppress_a_new_welcome(app_state, monkeypatch):
    app_state.update(build="new-build", epoch="older-identity", sent=time.time())
    send = mock.Mock(return_value=201)
    monkeypatch.setattr(chat, "send", send)
    miniapp.welcome(CHAT, "es")
    send.assert_called_once()
    assert set(state.data["apps"]) == {"100", "201"}


def test_first_contact_still_receives_one_app(app_state, monkeypatch):
    state.data["apps"] = {}
    send = mock.Mock(return_value=201)
    monkeypatch.setattr(chat, "send", send)
    miniapp.welcome(CHAT, "es")
    miniapp.welcome(CHAT, "en")
    send.assert_called_once()
    assert "201" in state.data["apps"]


def test_explicit_app_command_still_allows_deliberate_recovery(app_state, monkeypatch):
    from michiflin import commands

    app_state.update(build="new-build", sent=time.time() - 3600)
    send = mock.Mock(return_value=201)
    monkeypatch.setattr(chat, "send", send)
    commands.cmd_app(CHAT, [], "", "es")
    send.assert_called_once()
    assert set(state.data["apps"]) == {"100", "201"}


def test_revoked_binding_does_not_suppress_a_new_welcome(app_state, monkeypatch):
    app_state.update(build="new-build", addr="revoked@example.invalid")
    send = mock.Mock(return_value=201)
    monkeypatch.setattr(chat, "send", send)
    miniapp.welcome(CHAT, "es")
    send.assert_called_once()
    assert set(state.data["apps"]) == {"100", "201"}


@pytest.mark.parametrize("path", ["welcome", "point"])
def test_regranted_recipient_does_not_get_an_automatic_duplicate(app_state, monkeypatch, path):
    # The prior binding remains unauthorized; its mere existence only blocks
    # automatic delivery. Nothing here reactivates or updates its old grant.
    guest = "guest@example.invalid"
    app_state.update(addr=guest, access_session="old-grant")
    original = dict(app_state)
    monkeypatch.setattr(chat, "sender", lambda: guest)
    monkeypatch.setattr(miniapp.access, "bound_allowed", lambda entry: entry.get("access_session") == "new-grant")
    monkeypatch.setattr(miniapp, "send_app", send_app := mock.Mock())
    monkeypatch.setattr(chat, "send", send_text := mock.Mock(return_value=202))
    if path == "welcome":
        miniapp.welcome(CHAT, "es")
    else:
        miniapp.point_to_app(CHAT, "es")
    send_app.assert_not_called()
    assert app_state == original and set(state.data["apps"]) == {"100"}
    if path == "point":
        assert send_text.call_count == 1
        assert not send_text.call_args.kwargs.get("viewtype")
        assert "/app" not in send_text.call_args.args[1]


def test_unknown_historical_address_blocks_automatic_delivery_conservatively(app_state, monkeypatch):
    app_state.update(addr="", access_session="old-grant")
    monkeypatch.setattr(miniapp.access, "bound_allowed", lambda _entry: False)
    monkeypatch.setattr(miniapp, "send_app", send_app := mock.Mock())
    miniapp.welcome(CHAT, "es")
    send_app.assert_not_called()


def test_authorized_hello_remembers_actual_app_language(app_state, monkeypatch):
    app_state.update(build="new-build", locale="en")
    update = {"serial": 1, "payload": {"t": "req", "id": "synthetic", "op": "hello", "lang": "es", "args": {}}}
    rpc = SimpleNamespace(get_webxdc_status_updates=lambda *_args: json.dumps([update]))
    monkeypatch.setattr(chat, "rpc", lambda: rpc)
    monkeypatch.setattr(chat, "account", lambda: 1)
    route = mock.Mock()
    monkeypatch.setattr(miniapp, "_route", route)
    miniapp.on_status_update(100)
    assert app_state["locale"] == "es"
    assert app_state["language_observed"] is True
    assert miniapp.observed_locale(CHAT) == "es"
    assert app_state["serial"] == 1
    route.assert_called_once()


def test_explicit_update_uses_the_observed_app_language(app_state, monkeypatch):
    app_state.update(build="current-build", locale="en")
    update = {"serial": 1, "payload": {"t": "req", "id": "synthetic", "op": "hello", "lang": "es", "args": {}}}
    rpc = SimpleNamespace(get_webxdc_status_updates=lambda *_args: json.dumps([update]))
    monkeypatch.setattr(chat, "rpc", lambda: rpc)
    monkeypatch.setattr(chat, "account", lambda: 1)
    monkeypatch.setattr(miniapp, "_route", mock.Mock())
    miniapp.on_status_update(100)
    send = mock.Mock(return_value=201)
    monkeypatch.setattr(miniapp, "replace_app", send)
    miniapp.op_reinstall(CHAT, {"_app_instance_id": 100}, miniapp.observed_locale(CHAT))
    send.assert_called_once_with(100, CHAT, "es", OWNER)


def test_unapproved_request_cannot_change_the_remembered_language(app_state, monkeypatch):
    app_state.update(build="new-build", locale="en")
    update = {"serial": 1, "payload": {"t": "req", "id": "synthetic", "op": "hello", "lang": "es", "args": {}}}
    rpc = SimpleNamespace(get_webxdc_status_updates=lambda *_args: json.dumps([update]))
    monkeypatch.setattr(chat, "rpc", lambda: rpc)
    monkeypatch.setattr(chat, "account", lambda: 1)
    monkeypatch.setattr(admin, "is_allowed", lambda _address: False)
    monkeypatch.setattr(miniapp, "_route", mock.Mock())
    miniapp.on_status_update(100)
    assert app_state["locale"] == "en"


def test_sweeper_reads_pending_updates_without_broadcasting_on_startup(app_state, monkeypatch):
    """A restart alone must not deliver an app or a maintenance notice to any chat."""
    from michiflin import maintenance

    other = {"chat": 92, "addr": "other@example.invalid", "build": "older", "locale": "en", "serial": 0}
    state.data["apps"]["101"] = other
    monkeypatch.setattr(maintenance, "get", lambda: {"revision": 3, "active": True})
    monkeypatch.setattr(maintenance, "silent", lambda: False)
    monkeypatch.setattr(maintenance, "sync_profile", mock.Mock())
    monkeypatch.setattr(miniapp, "on_status_update", mock.Mock())
    monkeypatch.setattr(miniapp, "_prune_seen", lambda: None)
    monkeypatch.setattr(miniapp.time, "sleep", mock.Mock(side_effect=StopIteration))
    send = mock.Mock()
    replace = mock.Mock()
    push = mock.Mock()
    monkeypatch.setattr(chat, "send", send)
    monkeypatch.setattr(miniapp, "replace_app", replace)
    monkeypatch.setattr(miniapp, "push", push)
    with pytest.raises(StopIteration):
        miniapp.sweep_forever()
    assert {call.args[0] for call in miniapp.on_status_update.call_args_list} == {100, 101}
    send.assert_not_called()
    replace.assert_not_called()
    push.assert_not_called()


def test_previous_compatible_build_matches_exact_public_618c355_package(app_state, monkeypatch):
    # The expected value is from the original public package and this synthetic
    # epoch. It is not a production identity or a user-specific hash.
    previous = app_compat.previous_build("test-epoch", str(miniapp.APP_DIR), miniapp.APP_FILES)
    assert previous == "c7544a8050"
    assert miniapp.compatible_build({"build": previous})
    assert not miniapp.compatible_build({"build": "unknown-old"})
    monkeypatch.setattr(config, "BOT_EPOCH", "other-epoch")
    assert not miniapp.compatible_build({"build": previous})
    assert app_compat.previous_build("other-epoch", str(miniapp.APP_DIR), miniapp.APP_FILES) == "e46788cba5"


def test_previous_compatibility_fails_closed_when_snapshot_or_other_asset_changes(app_state, monkeypatch, tmp_path):
    import shutil
    old = tmp_path / "previous"
    shutil.copytree(app_compat._PREVIOUS, old)
    monkeypatch.setattr(app_compat, "_PREVIOUS", old)
    assert app_compat.previous_build("test-epoch", str(miniapp.APP_DIR), miniapp.APP_FILES) == "c7544a8050"
    with (old / "live.js").open("a", encoding="utf-8") as handle:
        handle.write("\n// tampered synthetic copy\n")
    assert app_compat.previous_build("test-epoch", str(miniapp.APP_DIR), miniapp.APP_FILES) == ""
    assert not miniapp.compatible_build({"build": "c7544a8050"})
    # Restore the snapshot, but mutate an unchanged current package asset.
    shutil.rmtree(old)
    shutil.copytree(Path(__file__).resolve().parents[1] / "michiflin" / "compat" / "618c355", old)
    current = tmp_path / "webxdc"
    shutil.copytree(miniapp.APP_DIR, current)
    with (current / "index.html").open("a", encoding="utf-8") as handle:
        handle.write("\n// tampered synthetic copy\n")
    assert app_compat.previous_build("test-epoch", str(current), miniapp.APP_FILES) == ""


def test_compatible_previous_app_keeps_normal_work_without_sending_a_new_app(app_state, monkeypatch):
    from concurrent.futures import Future

    class Immediate:
        def submit(self, function, *args):
            future = Future()
            try:
                future.set_result(function(*args))
            except Exception as exc:
                future.set_exception(exc)
            return future

    previous = app_compat.previous_build("test-epoch", str(miniapp.APP_DIR), miniapp.APP_FILES)
    app_state["build"] = previous
    monkeypatch.setattr(miniapp, "_workers", Immediate())
    monkeypatch.setattr(miniapp, "respond", mock.Mock())
    monkeypatch.setattr(state, "maintenance_active", lambda: False)
    monkeypatch.setattr(state, "maintenance_interrupted", lambda _generation: False)
    operation, send, replace = mock.Mock(return_value={"ok": True}), mock.Mock(), mock.Mock()
    monkeypatch.setitem(miniapp.OPS, "fixture_compatible", operation)
    monkeypatch.setattr(chat, "send", send)
    monkeypatch.setattr(miniapp, "replace_app", replace)
    miniapp.handle(100, dict(app_state), {"id": "work", "op": "fixture_compatible", "lang": "es", "args": {}})
    operation.assert_called_once()
    send.assert_not_called()
    replace.assert_not_called()
    assert set(state.data["apps"]) == {"100"}


def test_hello_from_old_app_does_not_create_a_new_instance(app_state, monkeypatch):
    """A hello is not consent to send a new instance."""
    from concurrent.futures import Future

    class Immediate:
        def submit(self, function, *args):
            future = Future()
            try:
                future.set_result(function(*args))
            except Exception as exc:
                future.set_exception(exc)
            return future

    state.data["apps"]["101"] = {"chat": 92, "addr": "other@example.invalid", "build": "old-build"}
    monkeypatch.setitem(miniapp.OPS, "hello", lambda _chat, _args, _locale: {"hello": True})
    monkeypatch.setattr(miniapp, "_workers", Immediate())
    monkeypatch.setattr(miniapp, "respond", mock.Mock())
    monkeypatch.setattr(miniapp, "_local_ai_disclosure", lambda _instance: None)
    monkeypatch.setattr(state, "maintenance_active", lambda: False)
    monkeypatch.setattr(state, "maintenance_interrupted", lambda _generation: False)
    replace = mock.Mock(return_value=201)
    monkeypatch.setattr(miniapp, "replace_app", replace)
    miniapp.handle(100, dict(app_state), {"id": "interactive-hello", "op": "hello", "lang": "es", "args": {}})
    replace.assert_not_called()
    assert "100" in state.data["apps"] and "101" in state.data["apps"]


def test_invitee_bootstrap_does_not_upgrade_an_inactive_chat(app_state, monkeypatch):
    """Only a missing first welcome may be recovered at startup."""
    from contextlib import nullcontext
    from michiflin import app, community, identity_guard, maintenance

    monkeypatch.setattr(config, "INVITE_AUTO_ACCESS", True)
    monkeypatch.setattr(maintenance, "silent", lambda: False)
    monkeypatch.setattr(community, "addresses", lambda: [OWNER])
    monkeypatch.setattr(community, "record", lambda _address: {"chat": CHAT})
    monkeypatch.setattr(identity_guard, "authorized_chat", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(chat, "recipient_guard", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(miniapp, "_bound_apps", lambda _chat: [app_state])
    welcome = mock.Mock()
    monkeypatch.setattr(miniapp, "welcome", welcome)
    bot = SimpleNamespace(rpc=SimpleNamespace(get_chat_contacts=lambda *_args: [11]))
    previous_sender, previous_person = chat.sender(), state.acting_as()
    try:
        app._bootstrap_invitees(bot, 1)
    finally:
        chat.set_sender(previous_sender)
        state.act_as(previous_person)
    welcome.assert_not_called()


def test_double_explicit_update_is_coalesced_without_deleting_original(app_state, monkeypatch):
    entered, release, errors = threading.Event(), threading.Event(), []
    calls = []
    def replace(instance, chat_id, locale, address):
        calls.append((instance, chat_id, locale, address))
        entered.set()
        assert release.wait(3)
        return 201
    monkeypatch.setattr(miniapp, "replace_app", replace)
    results = []
    def request():
        try:
            results.append(miniapp.op_reinstall(CHAT, {"_app_instance_id": 100}, "en"))
        except BaseException as exc:
            errors.append(exc)
    one, two = threading.Thread(target=request), threading.Thread(target=request)
    try:
        one.start()
        assert entered.wait(2)
        two.start()
    finally:
        release.set()
        one.join(3)
        if two.ident is not None:
            two.join(3)
    assert not errors and not one.is_alive() and not two.is_alive()
    assert calls == [(100, CHAT, "en", OWNER)] and "100" in state.data["apps"]
    assert len(results) == 2 and all(item["sent"] for item in results)
    assert sum(bool(item.get("already")) for item in results) == 1


def test_old_app_manual_update_uses_server_instance_not_client_spoof(app_state, monkeypatch):
    from concurrent.futures import Future
    class Immediate:
        def submit(self, function, *args):
            future = Future()
            try:
                future.set_result(function(*args))
            except Exception as exc:
                future.set_exception(exc)
            return future
    monkeypatch.setattr(miniapp, "_workers", Immediate())
    monkeypatch.setattr(miniapp, "respond", mock.Mock())
    monkeypatch.setattr(state, "maintenance_active", lambda: False)
    monkeypatch.setattr(state, "maintenance_interrupted", lambda _generation: False)
    replace = mock.Mock(return_value=201)
    monkeypatch.setattr(miniapp, "replace_app", replace)
    miniapp.handle(100, dict(app_state), {"id": "install", "op": "reinstall", "lang": "en", "args": {"_app_instance_id": 999}})
    replace.assert_called_once_with(100, CHAT, "en", OWNER)
    assert "100" in state.data["apps"]


def test_old_app_keeps_owner_access_controls_but_not_normal_work(app_state, monkeypatch):
    from concurrent.futures import Future
    class Immediate:
        def submit(self, function, *args):
            future = Future()
            try:
                future.set_result(function(*args))
            except Exception as exc:
                future.set_exception(exc)
            return future
    monkeypatch.setattr(miniapp, "_workers", Immediate())
    monkeypatch.setattr(state, "maintenance_active", lambda: False)
    monkeypatch.setattr(state, "maintenance_interrupted", lambda _generation: False)
    monkeypatch.setattr(config, "is_owner_admin", lambda address: address == OWNER)
    response = mock.Mock()
    monkeypatch.setattr(miniapp, "respond", response)
    admin_op, work = mock.Mock(return_value={"ok": True}), mock.Mock(return_value={"ok": True})
    monkeypatch.setitem(miniapp.OPS, "admin", admin_op)
    monkeypatch.setitem(miniapp.OPS, "ask", work)
    miniapp.handle(100, dict(app_state), {"id": "owner-control", "op": "admin", "lang": "es", "args": {"do": "allow_one"}})
    admin_op.assert_called_once()
    miniapp.handle(100, dict(app_state), {"id": "old-work", "op": "ask", "lang": "es", "args": {}})
    work.assert_not_called()
    assert "Pedir app nueva" in response.call_args.args[3]


def test_old_app_guest_cannot_bypass_owner_control_gate(app_state, monkeypatch):
    monkeypatch.setattr(config, "is_owner_admin", lambda _address: False)
    monkeypatch.setattr(state, "maintenance_active", lambda: False)
    response, administrative = mock.Mock(), mock.Mock()
    monkeypatch.setattr(miniapp, "respond", response)
    monkeypatch.setitem(miniapp.OPS, "admin", administrative)
    miniapp.handle(100, dict(app_state), {"id": "guest-control", "op": "admin", "lang": "es", "args": {"do": "allow_one"}})
    administrative.assert_not_called()
    assert "Pedir app nueva" in response.call_args.args[3]


def test_owner_can_regrant_one_guest_from_preserved_old_app(app_state, monkeypatch):
    """An upgrade must not lock the real owner out of the access controls."""
    from concurrent.futures import Future
    from michiflin import access
    class Immediate:
        def submit(self, function, *args):
            future = Future()
            try:
                future.set_result(function(*args))
            except Exception as exc:
                future.set_exception(exc)
            return future

    guest = "friend@example.invalid"
    access.reset()
    monkeypatch.setattr(config, "OWNER_ADDRESS", OWNER)
    monkeypatch.setattr(config, "OWNER_BACKUP_ADDRESS", "")
    monkeypatch.setattr(config, "OWNER_ONLY", True)
    monkeypatch.setattr(config, "INVITE_AUTO_ACCESS", False)
    monkeypatch.setattr(config, "MAINTENANCE_MODE", False)
    monkeypatch.setattr(config, "service_access_allowed", access.service_allowed)
    monkeypatch.setattr(miniapp, "_workers", Immediate())
    monkeypatch.setattr(state, "maintenance_active", lambda: False)
    monkeypatch.setattr(state, "maintenance_interrupted", lambda _generation: False)
    monkeypatch.setattr(state, "get_kill_switch", lambda _address: False)
    monkeypatch.setattr(state, "work_interrupted", lambda *_args: False)
    monkeypatch.setattr(admin, "_set_blocked_contact", lambda *_args: True)
    monkeypatch.setattr(admin, "snapshot", lambda: {})
    monkeypatch.setattr(miniapp.usage, "report", lambda: {})
    monkeypatch.setattr(miniapp.perf, "snapshot", lambda: {})
    monkeypatch.setitem(state.data, "access", {"allowed": [], "blocked": [], "attempts": {}, "alerts": True})
    response = mock.Mock()
    monkeypatch.setattr(miniapp, "respond", response)
    try:
        nonce = access.snapshot()["nonce"]
        miniapp.handle(100, dict(app_state), {"id": "grant-one", "op": "admin", "lang": "es",
                                               "args": {"do": "allow_one", "address": guest, "minutes": 60, "nonce": nonce}})
        assert access.service_allowed(guest)
        assert not access.service_allowed("someone-else@example.invalid")
        assert not access.bound_allowed({"chat": 92, "addr": guest, "access_session": "old-grant"})
        assert response.call_args.args[2]["message"] == "Hecho."
        assert not response.call_args.args[3]
    finally:
        access.reset()


def test_latest_two_compatible_packages_keep_exact_original_live_bytes(app_state, monkeypatch):
    # The published packages were archived byte-for-byte before this UI change.
    # This synthetic epoch is independent of any user's real instance.
    latest = app_compat.latest_build("test-epoch", str(miniapp.APP_DIR), miniapp.APP_FILES)
    recent = app_compat.recent_build("test-epoch", str(miniapp.APP_DIR), miniapp.APP_FILES)
    older = app_compat.previous_build("test-epoch", str(miniapp.APP_DIR), miniapp.APP_FILES)
    assert (latest, recent, older) == ("925b474bbe", "35e3d9314d", "c7544a8050")
    assert all(miniapp.compatible_build({"build": value}) for value in (latest, recent, older))
    assert miniapp.discloses_infobars({"build": latest})
    assert miniapp.discloses_infobars({"build": recent})
    assert not miniapp.discloses_infobars({"build": older})
    monkeypatch.setattr(config, "BOT_EPOCH", "unrelated-epoch")
    assert all(not miniapp.compatible_build({"build": value}) for value in (latest, recent, older))


def test_latest_and_recent_compatibility_fail_closed_if_their_live_copy_changes(app_state, monkeypatch, tmp_path):
    import shutil
    for attr, build, expected in (("_LATEST", app_compat.latest_build, "925b474bbe"),
                                  ("_RECENT", app_compat.recent_build, "35e3d9314d")):
        original = getattr(app_compat, attr)
        copy = tmp_path / attr
        shutil.copytree(original, copy)
        with monkeypatch.context() as patch:
            patch.setattr(app_compat, attr, copy)
            assert build("test-epoch", str(miniapp.APP_DIR), miniapp.APP_FILES) == expected
            with (copy / "live.js").open("a", encoding="utf-8") as handle:
                handle.write("\n// synthetic change\n")
            assert build("test-epoch", str(miniapp.APP_DIR), miniapp.APP_FILES) == ""
            assert not miniapp.compatible_build({"build": expected})
