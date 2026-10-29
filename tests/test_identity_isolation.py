"""A sealed deployment must not accept old or epoch-less WebXDC bindings."""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from michiflin import access, chat, config, miniapp, state


def test_strict_epoch_rejects_old_and_legacy_bindings_and_selects_only_new():
    entries = {
        "20": {"chat": 91, "addr": "owner@example.invalid", "epoch": "new"},
        "21": {"chat": 91, "addr": "owner@example.invalid", "epoch": "old"},
        "22": {"chat": 91, "addr": "owner@example.invalid"},
    }
    rpc = SimpleNamespace(get_message=lambda _acc, _msg_id: SimpleNamespace(chat_id=91))
    with mock.patch.object(state, "data", {"apps": entries}), \
         mock.patch.object(config, "BOT_EPOCH", "new"), \
         mock.patch.object(config, "BOT_EPOCH_STRICT", True), \
         mock.patch.object(access, "bound_allowed", return_value=True), \
         mock.patch.object(chat, "rpc", return_value=rpc), mock.patch.object(chat, "account", return_value=1):
        assert miniapp._instance(20) is entries["20"]
        assert miniapp._instance(21) is None and miniapp._instance(22) is None
        assert miniapp.latest_instance(91) == 20


def test_legacy_instance_compatibility_remains_until_cutover():
    entry = {"chat": 91, "addr": "owner@example.invalid"}
    with mock.patch.object(state, "data", {"apps": {"22": entry}}), \
         mock.patch.object(config, "BOT_EPOCH", "existing"), \
         mock.patch.object(config, "BOT_EPOCH_STRICT", False):
        assert miniapp._instance(22) is entry


def test_strict_old_instance_updates_never_reach_core():
    entry = {"chat": 91, "addr": "owner@example.invalid", "epoch": "old"}
    with mock.patch.object(state, "data", {"apps": {"17": entry}}), \
         mock.patch.object(config, "BOT_EPOCH", "new"), \
         mock.patch.object(config, "BOT_EPOCH_STRICT", True), \
         mock.patch.object(chat, "rpc") as rpc:
        miniapp._paused.clear()
        miniapp.on_status_update(17)
        rpc.assert_not_called()


def test_strict_epoch_preserves_but_never_resumes_old_or_epochless_jobs():
    from michiflin import queue
    old = {"id": "1", "chat": 91, "addr": "owner@example.invalid", "epoch": "old", "status": "sending", "todo": [0]}
    legacy = {"id": "2", "chat": 91, "addr": "owner@example.invalid", "status": "sending", "todo": [0]}
    new = {"id": "3", "chat": 91, "addr": "owner@example.invalid", "epoch": "new", "status": "sending", "todo": [0]}
    with mock.patch.object(config, "BOT_EPOCH", "new"), mock.patch.object(config, "BOT_EPOCH_STRICT", True), \
         mock.patch.object(access, "bound_allowed", return_value=True), mock.patch.object(chat, "send") as send:
        assert not access.job_allowed(old) and not access.job_allowed(legacy)
        assert access.job_allowed(new)
        queue._send_one(old)
        queue._send_one(legacy)
        queue._notify(old, "private")
        send.assert_not_called()
        assert old["todo"] == legacy["todo"] == [0]  # neither data nor progress was deleted


def test_sealed_queue_never_sends_to_an_unverified_or_changed_recipient():
    from michiflin import admin, identity_guard, queue
    job = {"id": "3", "chat": 91, "addr": "owner@example.invalid", "epoch": "new", "status": "sending", "todo": [0], "source_msg": 33}
    with mock.patch.object(config, "BOT_EPOCH", "new"), mock.patch.object(config, "BOT_EPOCH_STRICT", True), \
         mock.patch.object(config, "BOT_VERIFIED_CONTACTS_ONLY", True), mock.patch.object(access, "bound_allowed", return_value=True), \
         mock.patch.object(chat, "ready", return_value=True), mock.patch.object(chat, "rpc", return_value=object()), \
         mock.patch.object(chat, "account", return_value=1), mock.patch.object(admin, "is_allowed", return_value=True), \
         mock.patch.object(identity_guard, "authorized_chat", return_value=False), \
         mock.patch.object(chat, "send") as send, mock.patch.object(queue.progress, "finish_job") as reaction:
        queue._send_one(job)
        queue._notify(job, "private")
        queue._finish_job(job, "done")
        send.assert_not_called()
        reaction.assert_not_called()


def test_fragmented_status_stops_if_epoch_changes_after_first_part(monkeypatch):
    from michiflin import admin
    sent = []
    entry = {"chat": 91, "addr": "owner@example.invalid", "epoch": "new"}
    class Rpc:
        def send_webxdc_status_update(self, _acc, _instance, payload, _summary):
            sent.append(payload)
            entry["epoch"] = "old"
    monkeypatch.setattr(state, "data", {"apps": {"17": entry}})
    monkeypatch.setattr(config, "BOT_EPOCH", "new")
    monkeypatch.setattr(config, "BOT_EPOCH_STRICT", True)
    monkeypatch.setattr(config, "BOT_VERIFIED_CONTACTS_ONLY", False)
    monkeypatch.setattr(miniapp, "CHUNK", 12)
    monkeypatch.setattr(access, "bound_allowed", lambda _entry: True)
    monkeypatch.setattr(admin, "is_allowed", lambda _address: True)
    monkeypatch.setattr(chat, "_bot", SimpleNamespace(rpc=Rpc()))
    monkeypatch.setattr(chat, "_accid", 1)
    miniapp._deliver(17, "result", "x" * 100)
    assert len(sent) == 1


def test_status_rechecks_access_after_waiting_for_send_lock(monkeypatch):
    import threading
    from michiflin import admin
    sent, results = [], []
    entry = {"chat": 91, "addr": "owner@example.invalid", "epoch": "new"}
    rpc = SimpleNamespace(send_webxdc_status_update=lambda *_args: sent.append(True))
    monkeypatch.setattr(state, "data", {"apps": {"17": entry}})
    monkeypatch.setattr(config, "BOT_EPOCH", "new")
    monkeypatch.setattr(config, "BOT_EPOCH_STRICT", True)
    monkeypatch.setattr(config, "BOT_VERIFIED_CONTACTS_ONLY", False)
    monkeypatch.setattr(access, "bound_allowed", lambda _entry: True)
    monkeypatch.setattr(admin, "is_allowed", lambda _address: True)
    monkeypatch.setattr(chat, "_bot", SimpleNamespace(rpc=rpc))
    monkeypatch.setattr(chat, "_accid", 1)
    started = threading.Event()
    def submit():
        started.set()
        results.append(miniapp._send_status(17, {"t": "res"}))
    with chat._lock:
        worker = threading.Thread(target=submit)
        worker.start()
        assert started.wait(2)
        entry["epoch"] = "old"
    worker.join(timeout=2)
    assert not worker.is_alive() and results == [False] and not sent


def test_long_chat_text_sends_all_parts(monkeypatch):
    parts = []
    monkeypatch.setattr(chat, "send", lambda _chat, text, **_kw: parts.append(text) or len(parts))
    assert chat.text(91, "A" * 13001) == 1
    assert len(parts) == 3 and ''.join(parts) == "A" * 13001


def test_ordinary_reply_rechecks_verified_contact_at_each_handoff(monkeypatch):
    from michiflin import admin, identity_guard
    accepted = [True]
    sends = []
    rpc = SimpleNamespace(send_msg=lambda *_args: sends.append(True) or 101)
    monkeypatch.setattr(config, "BOT_VERIFIED_CONTACTS_ONLY", True)
    monkeypatch.setattr(chat, "_bot", SimpleNamespace(rpc=rpc))
    monkeypatch.setattr(chat, "_accid", 1)
    monkeypatch.setattr(state, "check_work_allowed", lambda: None)
    monkeypatch.setattr(admin, "is_allowed", lambda _address: True)
    monkeypatch.setattr(identity_guard, "authorized_chat", lambda *_a, **_kw: accepted[0])
    with chat.recipient_guard(91, "owner@example.invalid", 15, SimpleNamespace()):
        assert chat.send(91, "first") == 101
        accepted[0] = False
        assert chat.send(91, "second") == 0
    assert len(sends) == 1 and chat.recipient_allowed(91)


def test_queued_chat_work_does_not_start_after_contact_changes(monkeypatch):
    from michiflin import app, identity_guard
    monkeypatch.setattr(config, "BOT_VERIFIED_CONTACTS_ONLY", True)
    monkeypatch.setattr(chat, "_bot", SimpleNamespace(rpc=object()))
    monkeypatch.setattr(chat, "_accid", 1)
    monkeypatch.setattr(app.admin, "is_allowed", lambda _address: True)
    monkeypatch.setattr(identity_guard, "authorized_chat", lambda *_a, **_kw: False)
    progress = mock.Mock()
    monkeypatch.setattr(app.progress, "begin", progress)
    previous_sender, previous_person = chat.sender(), state.acting_as()
    try:
        app._process(SimpleNamespace(id=90001, chat_id=91, from_id=15), "hello", "es", "owner@example.invalid")
        progress.assert_not_called()
    finally:
        chat._thread.sender = previous_sender
        state.act_as(previous_person)


def test_new_app_rechecks_contact_inside_send_lock(monkeypatch):
    from michiflin import admin
    checks = iter([True, False])
    sent = []
    rpc = SimpleNamespace(send_msg=lambda *_args: sent.append(True) or 42)
    monkeypatch.setattr(config, "BOT_VERIFIED_CONTACTS_ONLY", True)
    monkeypatch.setattr(chat, "_bot", SimpleNamespace(rpc=rpc))
    monkeypatch.setattr(chat, "_accid", 1)
    monkeypatch.setattr(state, "check_work_allowed", lambda: None)
    monkeypatch.setattr(miniapp, "build", lambda: "/tmp/synthetic.xdc")
    monkeypatch.setattr(miniapp, "_verified_binding", lambda _entry: next(checks))
    monkeypatch.setattr(admin, "is_allowed", lambda _address: True)
    assert miniapp.send_app(91, "es", address="owner@example.invalid") == 0
    assert not sent
