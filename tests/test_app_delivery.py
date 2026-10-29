"""Synthetic delivery tests: automatic apps must not race or discard data."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from michiflin import admin, chat, config, live_web, miniapp, state

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
@pytest.mark.parametrize("first", ["upgrade", "other"])
def test_automatic_paths_cannot_send_two_apps_concurrently(app_state, monkeypatch, other, first):
    """Block one handoff so a competing auto path has a chance to race it."""
    sent, errors = [], []
    sent_lock = threading.Lock()
    rpc_lock = threading.Lock()  # the real chat layer also serializes the RPC handoff
    first_handoff = threading.Event()
    competing_handoff = threading.Event()
    release = threading.Event()

    def send(_chat_id, text="", **kwargs):
        if kwargs.get("viewtype") != "Webxdc":
            return 900  # an unquoted explanatory text is not another app
        with sent_lock:
            sent.append(text)
            number = len(sent)
        if number == 1:
            first_handoff.set()
        else:
            competing_handoff.set()
        with rpc_lock:
            if number == 1:
                assert release.wait(3), "synthetic handoff did not get released"
            return 200 + number

    monkeypatch.setattr(chat, "send", send)
    monkeypatch.setattr(miniapp, "latest_instance", lambda _chat: 0)

    def upgrade():
        miniapp._maybe_upgrade(100, app_state, "en")

    def alternative():
        if other == "welcome":
            miniapp.welcome(CHAT, "es")
        else:
            miniapp.point_to_app(CHAT, "es")

    def run(action):
        try:
            action()
        except BaseException as exc:
            errors.append(exc)

    one, two = (upgrade, alternative) if first == "upgrade" else (alternative, upgrade)
    t1 = threading.Thread(target=run, args=(one,), daemon=True)
    t2 = threading.Thread(target=run, args=(two,), daemon=True)
    try:
        t1.start()
        assert first_handoff.wait(2), "first synthetic delivery never started"
        t2.start()
        competing_handoff.wait(0.25)
    finally:
        release.set()
        t1.join(3)
        if t2.ident is not None:
            t2.join(3)
    assert not t1.is_alive() and not t2.is_alive(), "automatic delivery deadlocked"
    assert not errors
    assert len(sent) == 1
    assert "100" in state.data["apps"] and "201" in state.data["apps"]


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
    assert any("/app" in text for text, kwargs in sent if not kwargs.get("viewtype"))


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
    assert app_state["serial"] == 1
    route.assert_called_once()


def test_later_automatic_update_uses_the_observed_app_language(app_state, monkeypatch):
    app_state.update(build="current-build", locale="en")
    update = {"serial": 1, "payload": {"t": "req", "id": "synthetic", "op": "hello", "lang": "es", "args": {}}}
    rpc = SimpleNamespace(get_webxdc_status_updates=lambda *_args: json.dumps([update]))
    monkeypatch.setattr(chat, "rpc", lambda: rpc)
    monkeypatch.setattr(chat, "account", lambda: 1)
    monkeypatch.setattr(miniapp, "_route", mock.Mock())
    miniapp.on_status_update(100)
    send = mock.Mock(return_value=201)
    monkeypatch.setattr(miniapp, "replace_app", send)
    miniapp.proactive_upgrades()
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
