"""One deliberate, session-limited guest grant without reviving old access."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from michiflin import access, admin, chat, config, miniapp, state


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    access.reset()
    monkeypatch.setattr(config, "OWNER_ADDRESS", "owner@example.invalid")
    monkeypatch.setattr(config, "OWNER_ONLY", True)
    monkeypatch.setattr(config, "MAINTENANCE_MODE", False)
    monkeypatch.setitem(state.data, "access", {"allowed": ["old@example.invalid"], "blocked": [], "attempts": {}, "alerts": True})
    monkeypatch.setattr(state, "save", lambda: None)
    monkeypatch.setattr(state, "maintenance_active", lambda: False)
    monkeypatch.setattr(chat, "ready", lambda: True)
    monkeypatch.setattr(chat, "account", lambda: 1)
    monkeypatch.setattr(chat, "rpc", lambda: SimpleNamespace(get_blocked_contacts=lambda *_: []))
    yield
    access.reset()


def test_owner_can_approve_exactly_one_guest_with_one_action(monkeypatch):
    nonce = access.snapshot()["nonce"]
    assert not admin.is_allowed("old@example.invalid") and not admin.is_allowed("alice@example.invalid")
    assert admin.grant_one("alice@example.invalid", 60, nonce)
    assert admin.is_allowed("owner@example.invalid") and admin.is_allowed("alice@example.invalid")
    assert not admin.is_allowed("old@example.invalid") and not admin.is_allowed("bob@example.invalid")
    mode = access.snapshot()
    assert mode["controlled"] and not mode["owner_only"] and mode["guests"] == ["alice@example.invalid"]
    assert mode["expires"] > 0
    assert not admin.grant_one("bob@example.invalid", 60, nonce)  # stale nonce / open session
    access.reset()
    assert not admin.is_allowed("alice@example.invalid")  # boot default is still OWNER_ONLY=1


def test_single_guest_never_opens_during_maintenance_or_with_stale_confirmation(monkeypatch):
    nonce = access.snapshot()["nonce"]
    monkeypatch.setattr(config, "MAINTENANCE_MODE", True)
    assert not admin.grant_one("alice@example.invalid", 60, nonce)
    monkeypatch.setattr(config, "MAINTENANCE_MODE", False)
    monkeypatch.setattr(state, "maintenance_active", lambda: True)
    assert not admin.grant_one("alice@example.invalid", 60, nonce)
    monkeypatch.setattr(state, "maintenance_active", lambda: False)
    assert not admin.grant_one("alice@example.invalid", 60, "stale-confirmation")
    assert not admin.grant_one("alice@example.invalid", 17, nonce)
    assert access.snapshot()["owner_only"] and not admin.is_allowed("alice@example.invalid")


def test_failed_state_write_rolls_back_new_grant(monkeypatch):
    nonce = access.snapshot()["nonce"]
    monkeypatch.setattr(state, "save", lambda: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        admin.grant_one("alice@example.invalid", 60, nonce)
    assert access.snapshot()["owner_only"] and not admin.is_allowed("alice@example.invalid")


def test_single_guest_operation_requires_the_real_owner(monkeypatch):
    monkeypatch.setattr(chat, "sender", lambda: "guest@example.invalid")
    monkeypatch.setattr(admin, "is_admin", lambda _address: True)
    with pytest.raises(miniapp.AppError):
        miniapp.op_admin(1, {"do": "allow_one", "address": "alice@example.invalid", "minutes": 60,
                            "nonce": access.snapshot()["nonce"]}, "es")
    assert access.snapshot()["owner_only"]
