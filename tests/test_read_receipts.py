"""Real Delta Chat read receipts are enabled only for authorized incoming chats."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace as NS
import time

from michiflin import app, admin, identity_guard, state


class FakeRpc:
    def __init__(self):
        self.enabled = None
        self.calls = []
        self.fail_off = False
        self.info = NS(chat_type="Single", is_contact_request=False)
        self.contact = NS(address="owner@example.invalid")

    def set_config(self, _account, name, value):
        assert name == "mdns_enabled"
        if value == "0" and self.fail_off:
            raise RuntimeError("synthetic error")
        self.enabled = value
        self.calls.append(("set", value))

    def markseen_msgs(self, _account, ids):
        time.sleep(0.002)  # make cross-thread mode leaks observable
        self.calls.append(("seen", list(ids), self.enabled))

    def get_basic_chat_info(self, _account, _chat):
        return self.info

    def get_contact(self, _account, _contact):
        return self.contact


def test_read_receipt_requires_allowed_sender_and_verified_chat(monkeypatch):
    rpc = FakeRpc()
    msg = NS(chat_id=12, from_id=13, is_info=False)
    monkeypatch.setattr(admin, "is_allowed", lambda address: address == "owner@example.invalid")
    monkeypatch.setattr(identity_guard, "authorized_chat", lambda *_a, **_k: True)
    assert app._receipt_allowed(rpc, 1, msg)
    rpc.contact.address = "other@example.invalid"
    assert not app._receipt_allowed(rpc, 1, msg)
    rpc.contact.address = "owner@example.invalid"
    monkeypatch.setattr(identity_guard, "authorized_chat", lambda *_a, **_k: False)
    assert not app._receipt_allowed(rpc, 1, msg)
    monkeypatch.setattr(identity_guard, "authorized_chat", lambda *_a, **_k: True)
    rpc.info.is_contact_request = True
    assert not app._receipt_allowed(rpc, 1, msg)
    rpc.info = NS(chat_type="Group", is_contact_request=False)
    assert not app._receipt_allowed(rpc, 1, msg)


def test_rejected_message_is_marked_silently_then_receipts_restored():
    rpc = FakeRpc()
    assert app._mark_seen(rpc, 1, [21], False)
    assert rpc.calls == [("set", "0"), ("seen", [21], "0"), ("set", "1")]
    assert app._mark_seen(rpc, 1, [22], True)
    assert rpc.calls[-2:] == [("set", "1"), ("seen", [22], "1")]


def test_silent_switch_failure_never_marks_or_sends_to_rejected_sender():
    rpc = FakeRpc()
    rpc.fail_off = True
    assert not app._mark_seen(rpc, 1, [21], False)
    assert not [call for call in rpc.calls if call[0] == "seen"]
    assert rpc.enabled == "1"


def test_concurrent_accepted_and_rejected_do_not_leak_account_wide_setting():
    rpc = FakeRpc()
    expected = {i: bool(i % 2) for i in range(30)}
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(lambda i: app._mark_seen(rpc, 1, [i], expected[i]), expected))
    observed = {call[1][0]: call[2] == "1" for call in rpc.calls if call[0] == "seen"}
    assert observed == expected


def test_cursor_enables_mdn_but_seeds_old_messages_silently(monkeypatch):
    rpc = FakeRpc()
    rpc.get_next_msgs = lambda _a: [20]
    rpc.get_message = lambda *_a: NS(timestamp=time.time() - 1000)
    monkeypatch.setattr(state, "flag", lambda _x: False)
    flags = []
    monkeypatch.setattr(state, "set_flag", lambda *args: flags.append(args))
    app._start_cursor(NS(rpc=rpc), 1)
    assert rpc.calls == [("set", "1"), ("set", "0"), ("seen", [20], "0"), ("set", "1")]
    assert flags and rpc.enabled == "1"
