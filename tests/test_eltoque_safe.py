"""Safe handling of real elTOQUE API response shapes and rate limits."""
from __future__ import annotations

import time

import pytest

from michiflin import config, miniapp, state, tools


class Response:
    def __init__(self, body, status=200, headers=None):
        self.body, self.status_code = body, status
        self.headers = headers or {}
        self.ok = 200 <= status < 400

    def json(self):
        return self.body


@pytest.fixture(autouse=True)
def reset_rate_state(monkeypatch):
    monkeypatch.setitem(state.data, "eltoque", {})
    monkeypatch.setattr(state, "save", lambda: None)
    tools._cache.clear()
    monkeypatch.setattr(tools, "ELTOQUE_MIN_SECONDS", 0, raising=False)
    monkeypatch.setattr(tools, "_eltoque_next_request", 0.0, raising=False)
    monkeypatch.setattr(tools, "_eltoque_blocked_until", 0.0, raising=False)
    yield
    tools._cache.clear()


def test_api_parser_keeps_provider_symbols_and_rejects_unsafe_values():
    payload = {"tasas": {"USD": 855, "BTC": 780.65, "USDT_TRC20": 1095, "ECU": 950,
                          "TRX": 424.69, "BAD": "NaN", "INF": "inf", "NEG": -1,
                          "BOOLEAN": True, "BAD/KEY": 10, "USDX-CASH": 100}}
    result = tools._parse_eltoque_rates(payload)
    assert result == {"USD": 855.0, "BTC": 780.65, "USDT_TRC20": 1095.0, "ECU": 950.0, "TRX": 424.69, "USDX-CASH": 100.0}
    assert tools.rate_unit("BTC") == "usd_crypto"
    assert tools.rate_unit("USDT_TRC20") == "usd_crypto"  # provider table explicitly marks TRC20 per USD
    assert tools.rate_unit("TRX") == "unit"
    assert tools.rate_unit("ECU") == "unit"  # API Euro alias cross-checked against the provider table
    assert tools.rate_unit("USDT_OTHER") == "unknown"


def test_crypto_quote_never_claims_one_bitcoin_equals_the_api_value(monkeypatch):
    rates = {"USD": 855.0, "BTC": 780.65, "USDT_TRC20": 1095.0, "ECU": 950.0, "TRX": 424.69}
    monkeypatch.setattr(tools, "_rates", lambda: rates)
    shown = tools.rate_text("", "es")
    assert "USD en BTC" in shown and "1 USD en USDT (TRC20)" in shown
    assert "1 EUR: 950 CUP" in shown and "ECU" not in shown
    assert "1 BTC ≈" not in shown
    refusal = tools.rate_text("1 BTC", "es")
    assert "USD en BTC" in refusal and "1 BTC ≈" not in refusal
    assert "1 TRX ≈" in tools.rate_text("1 TRX", "es")
    assert "1 EUR ≈ 950 CUP" in tools.rate_text("1 ECU", "es")
    assert "1 EUR ≈ 950 CUP" in tools.rate_text("EUR", "es")


def test_requests_never_follow_redirects_or_bypass_429(monkeypatch):
    seen = []
    def get(url, **kwargs):
        seen.append((url, kwargs))
        return Response({}, 429, {"Retry-After": "-142"})
    monkeypatch.setattr(config, "ELTOQUE_API_TOKEN", "synthetic-token")
    monkeypatch.setattr(tools.requests, "get", get)
    with pytest.raises(tools.RateError, match="rate_limit") as caught:
        tools.current_rates()
    assert caught.value.retry_after >= 60
    assert seen[0][0] == tools.ELTOQUE_ENDPOINT and seen[0][1]["allow_redirects"] is False
    assert "synthetic-token" not in str(caught.value)
    with pytest.raises(tools.RateError, match="rate_limit"):
        tools.current_rates()
    assert len(seen) == 1


def test_partial_history_reuses_only_received_days_after_cooldown(monkeypatch):
    calls = []
    def get(_url, **kwargs):
        day = kwargs["params"]["date_from"][:10]
        calls.append(day)
        if len(calls) == 3:
            return Response({}, 429, {"Retry-After": "2"})
        return Response({"tasas": {"USD": 500 + len(calls), "USDT_TRC20": 600}})
    monkeypatch.setattr(config, "ELTOQUE_API_TOKEN", "synthetic-token")
    monkeypatch.setattr(tools.requests, "get", get)
    monkeypatch.setattr(state, "check_work_allowed", lambda: None)
    partial = tools.historical_rates(7)
    assert len(partial["history"]) == 2 and not partial["complete"] and partial["error"] == "rate_limit"
    assert partial["retry_after"] >= 2
    first_two = calls[:2]
    monkeypatch.setattr(tools, "_eltoque_blocked_until", time.monotonic() - 1)
    complete = tools.historical_rates(7)
    assert complete["complete"] and len(complete["history"]) == 7
    assert all(calls.count(day) == 1 for day in first_two)
    assert all("USDT_TRC20" in item["rates"] for item in complete["history"])
    assert tools.historical_rates(7) == complete and len(calls) == 8


def test_bad_shape_and_redirect_fail_closed(monkeypatch):
    monkeypatch.setattr(config, "ELTOQUE_API_TOKEN", "synthetic-token")
    monkeypatch.setattr(tools.requests, "get", lambda *_a, **_kw: Response({"tasas": []}))
    with pytest.raises(tools.RateError):
        tools.current_rates()
    monkeypatch.setattr(tools.requests, "get", lambda *_a, **_kw: Response({}, 302))
    with pytest.raises(tools.RateError, match="http"):
        tools.current_rates()


def test_rate_ui_receives_explicit_unit_metadata(monkeypatch):
    monkeypatch.setattr(tools, "current_rates", lambda: {"USD": 855.0, "BTC": 780.65, "ECU": 950.0})
    monkeypatch.setattr(config, "ELTOQUE_API_TOKEN", "synthetic-token")
    result = miniapp.op_rates(7, {}, "es")
    assert result["units"] == {"USD": "unit", "BTC": "usd_crypto", "ECU": "unit"}
    assert result["source"] == "elTOQUE API"


def test_retry_after_dates_use_server_clock_and_invalid_values_are_conservative():
    assert tools._retry_after({"Retry-After": "4"}) == 4
    assert tools._retry_after({"Retry-After": "-142"}) >= 60
    assert tools._retry_after({"Retry-After": "0"}) >= 60
    assert tools._retry_after({"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT",
                               "Date": "Wed, 21 Oct 2015 07:27:53 GMT"}) == 7
    assert tools._retry_after({"Retry-After": "Wed, 21 Oct 2015 07:27:00 GMT",
                               "Date": "Wed, 21 Oct 2015 07:28:00 GMT"}) >= 60


def test_all_threads_share_the_request_pacing_gate(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    observed = []
    seen_lock = threading.Lock()
    def get(_url, **_kwargs):
        with seen_lock:
            observed.append(time.monotonic())
        return Response({"tasas": {"USD": 500}})
    monkeypatch.setattr(config, "ELTOQUE_API_TOKEN", "synthetic-token")
    monkeypatch.setattr(tools, "ELTOQUE_MIN_SECONDS", 0.05)
    monkeypatch.setattr(tools.requests, "get", get)
    monkeypatch.setattr(state, "check_work_allowed", lambda: None)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _n: tools._eltoque_request(), range(3)))
    assert all(result == {"USD": 500} for result in results)
    assert len(observed) == 3 and all(b - a >= 0.045 for a, b in zip(observed, observed[1:]))


def test_on_demand_30_day_history_uses_same_day_windows(monkeypatch):
    seen = []
    def get(_url, **kwargs):
        params = kwargs["params"]
        assert params["date_from"][:10] == params["date_to"][:10]
        assert params["date_from"].endswith("00:00:00") and params["date_to"].endswith("23:59:59")
        seen.append(params["date_from"][:10])
        return Response({"tasas": {"USD": 500}})
    monkeypatch.setattr(config, "ELTOQUE_API_TOKEN", "synthetic-token")
    monkeypatch.setattr(tools.requests, "get", get)
    monkeypatch.setattr(state, "check_work_allowed", lambda: None)
    data = tools.historical_rates(30)
    assert data["complete"] and len(data["history"]) == len(seen) == 30
    assert len(set(seen)) == 30 and seen == sorted(seen)
    assert data["units"]["USD"] == "unit"


def test_maintenance_interrupt_is_not_swallowed(monkeypatch):
    def paused():
        raise state.WorkPaused(maintenance=True)
    monkeypatch.setattr(config, "ELTOQUE_API_TOKEN", "synthetic-token")
    monkeypatch.setattr(state, "check_work_allowed", paused)
    with pytest.raises(state.WorkPaused):
        miniapp.op_rates(7, {}, "es")


def test_missing_key_does_not_scrape_the_public_website(monkeypatch):
    monkeypatch.setattr(config, "ELTOQUE_API_TOKEN", "")
    monkeypatch.setattr(tools.requests, "get", lambda *_a, **_kw: pytest.fail("Website scraping is not an API fallback"))
    with pytest.raises(tools.RateError, match="authorization"):
        tools.current_rates()


def test_expired_budget_and_concurrent_history_do_not_send_requests(monkeypatch):
    monkeypatch.setattr(config, "ELTOQUE_API_TOKEN", "synthetic-token")
    monkeypatch.setattr(tools.requests, "get", lambda *_a, **_kw: pytest.fail("No request after the deadline"))
    with pytest.raises(tools.RateError, match="timeout"):
        tools._eltoque_request(deadline=time.monotonic() - 1)
    assert tools._eltoque_history_lock.acquire(blocking=False)
    try:
        with pytest.raises(tools.RateError, match="busy"):
            tools.historical_rates(7)
    finally:
        tools._eltoque_history_lock.release()


def test_422_distinguishes_jwt_error_from_input_validation(monkeypatch):
    monkeypatch.setattr(config, "ELTOQUE_API_TOKEN", "synthetic-token")
    monkeypatch.setattr(tools.requests, "get", lambda *_a, **_kw: Response({"msg": "Not enough segments"}, 422))
    with pytest.raises(tools.RateError, match="authorization"):
        tools.current_rates()
    monkeypatch.setattr(tools.requests, "get", lambda *_a, **_kw: Response({"detail": [{"loc": ["query"]}]}, 422))
    with pytest.raises(tools.RateError, match="http"):
        tools.current_rates()


def test_display_does_not_silently_round_provider_rate_to_two_decimals(monkeypatch):
    monkeypatch.setattr(tools, "_rates", lambda: {"USD": 1.234567, "BTC": 0.12345678})
    value = tools.rate_text("", "es")
    assert "1,234567" in value and "0,12345678" in value


def test_euro_alias_and_explicit_usd_crypto_conversion_do_not_modify_provider_values(monkeypatch):
    raw = {"USD": 855.0, "ECU": 950.0, "BTC": 780.65, "USDT_TRC20": 1095.0, "TRX": 424.69}
    monkeypatch.setattr(tools, "_rates", lambda: raw)
    monkeypatch.setattr(config, "ELTOQUE_DERIVED_CALCULATIONS", True)
    assert "95.000 CUP" in tools.rate_text("100 EUR", "es")
    assert "EUR" in tools.rate_text("950 CUP EUR", "es")
    assert "78.065 CUP" in tools.rate_text("100 USD_BTC", "es")
    assert "USD en BTC" in tools.rate_text("100 USD_BTC", "es")
    assert "109.500 CUP" in tools.rate_text("100 USD_USDT_TRC20", "es")
    assert "moneda completa" in tools.rate_text("100 USDT_TRC20", "es")
    assert "Michiflin" in tools.rate_text("100 EUR", "es")
    assert raw == {"USD": 855.0, "ECU": 950.0, "BTC": 780.65, "USDT_TRC20": 1095.0, "TRX": 424.69}


def test_euro_native_key_takes_precedence_over_legacy_alias(monkeypatch):
    monkeypatch.setattr(tools, "_rates", lambda: {"EUR": 800, "ECU": 950})
    assert "800" in tools.rate_text("EUR", "en")
    assert "950" in tools.rate_text("ECU", "en")


def test_derived_are_never_calculated_from_stale_data_or_unconfirmed_codes(monkeypatch):
    monkeypatch.setattr(config, "ELTOQUE_DERIVED_CALCULATIONS", True)
    monkeypatch.setattr(tools, "rates_snapshot", lambda: {"rates": {"ECU": 950, "USDT_OTHER": 100}, "at": time.time(), "stale": True})
    assert "pausada" in tools.rate_text("100 EUR", "es")
    assert "confirmada" in tools.rate_text("100 USDT_OTHER", "es")
