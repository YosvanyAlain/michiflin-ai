"""Shared, source-preserving elTOQUE polling, fallback and owner notices."""
from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta
import time

import pytest

from michiflin import config, miniapp, rate_store, rates_monitor, state, tools


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setitem(state.data, "eltoque", {})
    monkeypatch.setattr(state, "save", lambda: None)
    monkeypatch.setattr(config, "ELTOQUE_API_TOKEN", "synthetic-token")
    monkeypatch.setattr(config, "OWNER_ADDRESS", "owner@example.invalid")
    monkeypatch.setattr(state, "check_work_allowed", lambda: None)
    tools._cache.clear()
    yield
    tools._cache.clear()


def test_latest_uses_saved_value_only_with_explicit_stale_marker(monkeypatch):
    observed = time.time() - 300
    monkeypatch.setattr(tools, "current_rates", lambda: {"USD": 400.25, "BTC": 380.75})
    monkeypatch.setattr(tools, "rates_observed_at", lambda: observed)
    fresh = tools.rates_snapshot()
    assert not fresh["stale"] and fresh["at"] == observed and fresh["persisted"]
    def fail():
        raise tools.RateError("rate_limit", 60)
    monkeypatch.setattr(tools, "current_rates", fail)
    saved = tools.rates_snapshot()
    assert saved["stale"] and saved["error"] == "rate_limit" and saved["retry_after"] == 60
    assert saved["at"] == observed and saved["rates"] == fresh["rates"]
    assert "último dato guardado" in tools.rate_text("USD", "es")


def test_without_valid_snapshot_failure_is_explicit(monkeypatch):
    monkeypatch.setattr(tools, "current_rates", lambda: (_ for _ in ()).throw(tools.RateError("authorization")))
    with pytest.raises(tools.RateError, match="authorization"):
        tools.rates_snapshot()


def test_historical_values_reused_across_process_cache_reset(monkeypatch):
    calls = []
    def request(params, *, deadline=None):
        calls.append(params["date_from"][:10])
        return {"USD": 400.1234, "USDT_TRC20": 500}
    monkeypatch.setattr(tools, "_eltoque_request", request)
    first = tools.historical_rates(7)
    assert first["complete"] and len(calls) == 7 and first["persisted"]
    tools._cache.clear()  # same state restored in a fresh bot process
    second = tools.historical_rates(7)
    assert second["complete"] and len(calls) == 7
    assert [x["date"] for x in first["history"]] == [x["date"] for x in second["history"]]
    assert all(point["rates"]["USD"] == 400.1234 and point["queried_at"] for point in second["history"])


def test_saved_historical_points_survive_missing_key_but_are_marked(monkeypatch):
    yesterday = (datetime.now(config.LOCAL_ZONE).date() - timedelta(days=1)).isoformat()
    assert rate_store.save_days({yesterday: {"rates": {"USD": 500}, "at": time.time()}})
    monkeypatch.setattr(config, "ELTOQUE_API_TOKEN", "")
    monkeypatch.setattr(tools, "_eltoque_request", lambda *_a, **_kw: pytest.fail("must not contact another source"))
    result = tools.historical_rates(7)
    assert result["stale"] and result["error"] == "authorization" and not result["complete"]
    assert [p["date"] for p in result["history"]] == [yesterday]


def test_on_rate_limit_still_shows_later_cached_days_without_more_requests(monkeypatch):
    yesterday = (datetime.now(config.LOCAL_ZONE).date() - timedelta(days=1)).isoformat()
    rate_store.save_days({yesterday: {"rates": {"USD": 700}, "at": time.time()}})
    calls = []
    def request(*_args, **_kwargs):
        calls.append(1)
        raise tools.RateError("rate_limit", 30)
    monkeypatch.setattr(tools, "_eltoque_request", request)
    result = tools.historical_rates(7)
    assert len(calls) == 1 and result["stale"] and result["history"][0]["date"] == yesterday


def test_background_polls_once_and_records_a_real_daily_window(monkeypatch):
    monkeypatch.setattr(state, "maintenance_active", lambda: False)
    monkeypatch.setattr(state, "get_kill_switch", lambda _address: False)
    monkeypatch.setattr(state, "guard_work", lambda _address: nullcontext())
    monkeypatch.setattr(tools, "current_rates", lambda: {"USD": 405.75})
    monkeypatch.setattr(tools, "rates_observed_at", lambda: time.time())
    seen = []
    def history(params):
        seen.append(params)
        return {"USD": 400.125}
    monkeypatch.setattr(tools, "_eltoque_request", history)
    monkeypatch.setattr(rates_monitor, "_owner_notification", lambda *_a: True)
    rates_monitor.poll_once()
    rates_monitor.poll_once()
    assert len(seen) == 1 and seen[0]["date_from"].endswith("00:00:00") and seen[0]["date_to"].endswith("23:59:59")
    assert rate_store.latest()["rates"]["USD"] == 405.75
    yesterday = (datetime.now(config.LOCAL_ZONE).date() - timedelta(days=1)).isoformat()
    assert rate_store.day(yesterday)["rates"]["USD"] == 400.125
    assert rate_store.status()["error"] == ""


def test_maintenance_and_kill_switch_suspend_polling(monkeypatch):
    monkeypatch.setattr(tools, "current_rates", lambda: pytest.fail("maintenance must suspend polling"))
    monkeypatch.setattr(state, "maintenance_active", lambda: True)
    rates_monitor.poll_once()
    monkeypatch.setattr(state, "maintenance_active", lambda: False)
    monkeypatch.setattr(state, "get_kill_switch", lambda _address: True)
    rates_monitor.poll_once()


def test_owner_notice_is_coalesced_retried_and_recovery_is_reported(monkeypatch):
    notices = []
    def notify(es, en, kind):
        notices.append((es, en, kind))
        return len(notices) != 1  # first queue attempt fails
    monkeypatch.setattr(rates_monitor, "_owner_notification", notify)
    now = time.time()
    rates_monitor.record_health("authorization", now)
    assert state.data["eltoque"]["health"]["pending"] == "error"
    rates_monitor.record_health("authorization", now + 60)
    assert len(notices) == 2 and "pending" not in state.data["eltoque"]["health"]
    rates_monitor.record_health("authorization", now + 120)
    assert len(notices) == 2
    rates_monitor.record_health("", now + 180)
    assert len(notices) == 3 and notices[-1][2] == "ok"
    assert all("synthetic-token" not in str(notice) for notice in notices)


def test_derived_calculations_can_be_disabled_without_altering_rates(monkeypatch):
    monkeypatch.setattr(tools, "current_rates", lambda: {"USD": 500})
    monkeypatch.setattr(tools, "rates_observed_at", time.time)
    monkeypatch.setattr(config, "ELTOQUE_DERIVED_CALCULATIONS", False)
    assert "500" in tools.rate_text("USD", "es")
    assert "desactivadas" in tools.rate_text("100 USD", "es")
    assert miniapp.op_rates(1, {}, "es")["derived_allowed"] is False
    monkeypatch.setattr(config, "ELTOQUE_DERIVED_CALCULATIONS", True)
    assert "50.000" in tools.rate_text("100 USD", "es")


def test_real_state_survives_a_new_python_process(tmp_path):
    """This exercises disk serialization/reload, not just an in-memory cache clear."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    env = dict(os.environ, CONFIG_DIR=str(tmp_path / "config"), OWNER_ADDRESS="owner@example.invalid",
               ELTOQUE_API_TOKEN="synthetic-token", BOT_TIMEZONE="America/Havana")
    producer = r'''
from datetime import datetime, timedelta
import time
from michiflin import config, rate_store, state
state.load()
yesterday = (datetime.now(config.LOCAL_ZONE).date() - timedelta(days=1)).isoformat()
assert rate_store.save_latest({"USD": 456.123456, "BTC": 400.75}, time.time() - 42)
assert rate_store.save_days({yesterday: {"rates": {"USD": 450.875}, "at": time.time()}})
'''
    consumer = r'''
from datetime import datetime, timedelta
from michiflin import config, rate_store, state, tools
state.load()
yesterday = (datetime.now(config.LOCAL_ZONE).date() - timedelta(days=1)).isoformat()
assert rate_store.latest()["rates"] == {"USD": 456.123456, "BTC": 400.75}
assert rate_store.day(yesterday)["rates"]["USD"] == 450.875
tools.current_rates = lambda: (_ for _ in ()).throw(tools.RateError("connection"))
snapshot = tools.rates_snapshot()
assert snapshot["stale"] and snapshot["rates"]["USD"] == 456.123456
assert tools.saved_rates_snapshot()["source"] == "elTOQUE API"
'''
    for code in (producer, consumer):
        result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                                env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr


def test_storage_error_rolls_back_observation_and_is_reported(monkeypatch):
    old_time = time.time() - 100
    assert rate_store.save_latest({"USD": 100}, old_time)
    monkeypatch.setattr(state, "save", lambda: (_ for _ in ()).throw(OSError("disk unavailable")))
    assert not rate_store.save_latest({"USD": 200}, time.time())
    assert rate_store.latest()["rates"] == {"USD": 100}
    assert not rate_store.save_days({"2026-10-01": {"rates": {"USD": 200}, "at": time.time()}})
    assert rate_store.day("2026-10-01") is None


def test_notifications_require_directly_verified_single_owner_chat(monkeypatch):
    from types import SimpleNamespace
    from michiflin import chat, identity_guard

    entry = {"chat": 8, "addr": "owner@example.invalid", "sent": 1}
    monkeypatch.setitem(state.data, "apps", {"99": entry})
    monkeypatch.setattr(chat, "ready", lambda: True)
    monkeypatch.setattr(chat, "account", lambda: 1)
    monkeypatch.setattr(config, "BOT_VERIFIED_CONTACTS_ONLY", False)
    contact = SimpleNamespace(address=config.OWNER_ADDRESS, is_verified=True, is_key_contact=True,
                              e2ee_avail=True, verifier_id=42)
    info = SimpleNamespace(chat_type="Single", is_encrypted=True, is_contact_request=False, is_self_talk=False)
    rpc = SimpleNamespace(get_basic_chat_info=lambda *_: info, get_chat_contacts=lambda *_: [2],
                          get_contact=lambda *_: contact)
    monkeypatch.setattr(chat, "rpc", lambda: rpc)
    sent = []
    monkeypatch.setattr(miniapp, "push", lambda *args: sent.append(args) or True)
    assert not rates_monitor._owner_notification("aviso", "notice", "warn")
    assert not sent  # a third-party verification is not direct owner verification
    contact.verifier_id = 1
    assert rates_monitor._owner_notification("aviso", "notice", "warn")
    assert len(sent) == 1 and sent[0][0] == 8
    monkeypatch.setattr(identity_guard, "authorized_entry", lambda *_: False)
    assert not rates_monitor._owner_notification("aviso", "notice", "warn")
    assert len(sent) == 1


def test_anomaly_not_lost_when_owner_was_offline(monkeypatch):
    sent = []
    monkeypatch.setattr(rates_monitor, "_owner_notification", lambda *_args: False)
    now = time.time()
    rates_monitor.record_health("connection", now)
    rates_monitor.record_health("", now + 30)
    assert state.data["eltoque"]["health"]["pending"] == "recovered"
    monkeypatch.setattr(rates_monitor, "_owner_notification", lambda *args: sent.append(args) or True)
    rates_monitor.record_health("", now + 60)
    assert len(sent) == 1 and sent[0][2] == "ok"
