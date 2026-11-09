"""Synthetic weather and identities only; no mail, API, or historical state."""
from unittest import mock
import pytest
from michiflin import access, admin, chat, config, miniapp, state, tools, weather_home

_real_state_save = state.save


@pytest.fixture(autouse=True)
def isolated_weather(monkeypatch):
    monkeypatch.setitem(state.data, "weather_home", {})
    monkeypatch.setattr(state, "save", lambda: None)
    monkeypatch.setattr(config, "OWNER_ADDRESS", "owner@example.test")
    monkeypatch.setattr(config, "OWNER_BACKUP_ADDRESS", "")
    monkeypatch.setattr(config, "BOT_EPOCH", "synthetic-epoch")
    monkeypatch.setattr(config, "BOT_EPOCH_STRICT", True)
    monkeypatch.setattr(config, "INVITE_AUTO_ACCESS", False)
    monkeypatch.setattr(admin, "is_allowed", lambda a: a in {"owner@example.test", "guest@example.test"})
    monkeypatch.setattr(access, "session_stamp", lambda _a: "grant-a")
    monkeypatch.setattr(access, "snapshot", lambda: {"controlled": True})
    monkeypatch.setattr(state, "maintenance_active", lambda: False)
    monkeypatch.setattr(state, "get_kill_switch", lambda _a: False)
    monkeypatch.setattr(state, "kill_generation", lambda _a: 0)
    monkeypatch.setattr(state, "work_interrupted", lambda _a, _g: False)
    monkeypatch.setattr(state, "maintenance_interrupted", lambda _g: False)
    monkeypatch.setattr(chat, "sender", lambda: "owner@example.test")


def sample(city="La Habana, Cuba", place_id=42, temp=27):
    return {"query": city, "place": {"id": place_id, "label": city},
            "candidates": [{"id": place_id, "label": city}], "timezone": "America/Havana", "as_of": "2026-11-05T08:00",
            "current": {"temp": temp, "feels": temp, "humidity": 65, "wind": 8, "precipitation": 0,
                        "code": 0, "is_day": 1, "description": "Cielo despejado"},
            "days": [{"date": "2026-11-05", "code": 0, "description": "Cielo despejado", "high": 29, "low": 22, "rain": 0}],
            "source": "Open-Meteo"}


def test_default_is_explicit_city_without_device_location_and_guest_is_opt_in(monkeypatch):
    fetch = mock.Mock(side_effect=AssertionError("network during view"))
    monkeypatch.setattr(tools, "weather_data", fetch)
    owner = weather_home.view("owner@example.test", "es")
    guest = weather_home.view("guest@example.test", "es")
    assert owner["city"] == "La Habana, Cuba" and owner["auto"] is True and owner["last"] is None
    assert guest["auto"] is False and guest["last"] is None
    assert weather_home.view("nobody@example.test", "es") is None
    fetch.assert_not_called()


def test_manual_lookup_persists_and_default_requires_deliberate_selection(monkeypatch):
    monkeypatch.setattr(tools, "weather_data", lambda city, locale, place_id=0: sample(city, place_id or 99))
    response = miniapp.op_weather(1, {"city": "Santiago, Cuba", "place_id": 99}, "es")
    assert response["saved_on_bot"] is True
    before = weather_home.view("owner@example.test", "es")
    assert before["city"] == "La Habana, Cuba" and before["last"]["data"]["place"]["id"] == 99
    after = miniapp.op_weather_settings(1, {"do": "use_last", "city": "Santiago, Cuba", "place_id": 99}, "es")
    assert after["city"] == "Santiago, Cuba" and after["place_id"] == 99 and after["auto"] is True
    off = miniapp.op_weather_settings(1, {"do": "auto", "on": False}, "es")
    assert off["auto"] is False and off["last"]["data"]["query"] == "Santiago, Cuba"
    on = miniapp.op_weather_settings(1, {"do": "auto", "on": True}, "es")
    assert on["auto"] is True and on["next_at"] >= on["last"]["at"] + weather_home.INTERVAL
    assert weather_home.view("guest@example.test", "en")["last"] is None


def test_default_selection_rejects_a_result_that_changed_elsewhere():
    weather_home.remember("owner@example.test", sample("Santiago, Cuba", 99), at=200)
    with pytest.raises(miniapp.AppError, match="última consulta cambió"):
        miniapp.op_weather_settings(1, {"do": "use_last", "city": "La Habana, Cuba", "place_id": 42}, "es")
    assert weather_home.view("owner@example.test", "es")["city"] == "La Habana, Cuba"


def test_background_is_quiet_throttled_and_keeps_snapshot_on_failure(monkeypatch):
    weather_home.view("owner@example.test", "es")
    calls = mock.Mock(side_effect=[sample(), tools.WeatherError("unavailable")])
    monkeypatch.setattr(tools, "weather_data", calls)
    send = mock.Mock(side_effect=AssertionError("background must not send mail"))
    monkeypatch.setattr(chat, "send_update", send)
    assert weather_home.poll_once(at=10_000)
    first = weather_home.view("owner@example.test", "es")
    assert first["last"]["data"]["current"]["temp"] == 27
    assert first["last"]["at"] == 10_000 and first["next_at"] == 10_000 + weather_home.INTERVAL
    assert not weather_home.poll_once(at=10_001)
    assert weather_home.poll_once(at=10_000 + weather_home.INTERVAL)
    after = weather_home.view("owner@example.test", "es")
    assert after["last"] == first["last"] and after["next_at"] == 10_000 + 2 * weather_home.INTERVAL
    assert not weather_home.poll_once(at=10_000 + weather_home.INTERVAL + 1)
    assert calls.call_count == 2
    send.assert_not_called()


def test_pause_kill_and_preference_change_cannot_commit_inflight(monkeypatch):
    weather_home.view("owner@example.test", "es")
    fetch = mock.Mock(return_value=sample())
    monkeypatch.setattr(tools, "weather_data", fetch)
    monkeypatch.setattr(state, "maintenance_active", lambda: True)
    assert not weather_home.poll_once(at=1000)
    monkeypatch.setattr(state, "maintenance_active", lambda: False)
    monkeypatch.setattr(state, "get_kill_switch", lambda _a: True)
    assert not weather_home.poll_once(at=1000)
    monkeypatch.setattr(state, "get_kill_switch", lambda _a: False)
    def disable_while_fetching(*_args):
        weather_home.settings("owner@example.test", "auto", False)
        return sample()
    fetch.side_effect = disable_while_fetching
    assert weather_home.poll_once(at=1000)
    result = weather_home.view("owner@example.test", "es")
    assert result["auto"] is False and result["last"] is None and fetch.call_count == 1


def test_no_background_fetch_if_throttle_cannot_be_saved(monkeypatch):
    weather_home.view("owner@example.test", "es")
    fetch = mock.Mock(side_effect=AssertionError("network"))
    monkeypatch.setattr(tools, "weather_data", fetch)
    monkeypatch.setattr(state, "save", mock.Mock(side_effect=OSError("disk")))
    assert not weather_home.poll_once(at=1000)
    assert weather_home.view("owner@example.test", "es")["next_at"] == 0
    fetch.assert_not_called()


def test_revoked_grant_is_not_reused_or_refreshed(monkeypatch):
    weather_home.view("guest@example.test", "es")
    weather_home.remember("guest@example.test", sample("Camagüey", 2), at=100)
    weather_home.settings("guest@example.test", "use_last", {"city": "Camagüey", "place_id": 2})
    weather_home.settings("guest@example.test", "auto", True)
    old_key = weather_home._scope("guest@example.test")[0]
    monkeypatch.setattr(access, "session_stamp", lambda _a: "grant-b")
    fresh = weather_home.view("guest@example.test", "es")
    assert fresh["last"] is None and fresh["auto"] is False
    assert old_key in state.data["weather_home"]
    assert not weather_home.poll_once(at=100_000)


def test_manual_save_failure_is_explicit(monkeypatch):
    weather_home.view("owner@example.test", "es")
    monkeypatch.setattr(tools, "weather_data", lambda *args: sample())
    monkeypatch.setattr(state, "save", mock.Mock(side_effect=OSError("disk")))
    response = miniapp.op_weather(1, {"city": "La Habana, Cuba", "place_id": 42}, "es")
    assert response["current"]["temp"] == 27 and response["saved_on_bot"] is False
    assert weather_home.view("owner@example.test", "es")["last"] is None


def test_settings_reject_missing_last_and_invalid_types():
    with pytest.raises(miniapp.AppError, match="Consulta y guarda"):
        miniapp.op_weather_settings(1, {"do": "use_last"}, "es")
    with pytest.raises(miniapp.AppError, match="No se pudo"):
        miniapp.op_weather_settings(1, {"do": "auto", "on": "true"}, "es")


def test_toggling_does_not_clear_failed_attempt_backoff(monkeypatch):
    weather_home.view("owner@example.test", "es")
    fetch = mock.Mock(side_effect=tools.WeatherError("unavailable"))
    monkeypatch.setattr(tools, "weather_data", fetch)
    assert weather_home.poll_once(at=1000)
    weather_home.settings("owner@example.test", "auto", False)
    weather_home.settings("owner@example.test", "auto", True)
    assert not weather_home.poll_once(at=1001)
    assert weather_home.view("owner@example.test", "es")["next_at"] == 1000 + weather_home.INTERVAL
    assert fetch.call_count == 1


def test_equivalent_city_query_does_not_trigger_duplicate_auto_lookup(monkeypatch):
    data = sample("La Habana", 42)
    data["place"]["label"] = "La Habana, Provincia de La Habana, Cuba"
    weather_home.view("owner@example.test", "es")
    weather_home.remember("owner@example.test", data, at=1000)
    fetch = mock.Mock(side_effect=AssertionError("duplicate request"))
    monkeypatch.setattr(tools, "weather_data", fetch)
    assert not weather_home.poll_once(at=1001)
    fetch.assert_not_called()


def test_weather_state_survives_restart_and_one_missed_cycle(tmp_path, monkeypatch):
    # Exercise the real atomic JSON writer/loader against synthetic state only.
    import copy
    real_load = state.load
    # Restore the writer captured before the autouse fixture patched it.
    monkeypatch.setattr(state, "save", _real_state_save)
    original = copy.deepcopy(state.data)
    monkeypatch.setattr(state, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(state, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(state, "LEGACY_STATE_FILE", tmp_path / "missing-legacy.json")
    try:
        weather_home.view("owner@example.test", "es")
        weather_home.remember("owner@example.test", sample("La Habana, Cuba", 42), at=1000)
        key = weather_home._scope("owner@example.test")[0]
        assert state.data["weather_home"][key]["last"]["at"] == 1000
        state.data["weather_home"] = {}
        real_load()
        after = weather_home.view("owner@example.test", "es")
        assert after["last"]["data"]["place"]["id"] == 42
        assert after["next_at"] == 1000 + weather_home.INTERVAL
        fetch = mock.Mock(return_value=sample())
        monkeypatch.setattr(tools, "weather_data", fetch)
        assert weather_home.poll_once(at=1000 + 5 * weather_home.INTERVAL)
        assert not weather_home.poll_once(at=1001 + 5 * weather_home.INTERVAL)
        assert fetch.call_count == 1  # no replay of missed intervals
    finally:
        state.data.clear()
        state.data.update(original)


def test_geocoding_never_silently_falls_back_to_a_different_region(monkeypatch):
    class Response:
        def __init__(self, body):
            self.body = body
        def raise_for_status(self):
            pass
        def json(self):
            return self.body

    places = [{"id": 1, "name": "Santiago", "admin1": "Región Metropolitana", "country": "Chile", "latitude": -33, "longitude": -70},
              {"id": 2, "name": "Santiago", "admin1": "Santiago", "country": "Cuba", "latitude": 20, "longitude": -75}]
    forecast = {"current": {"temperature_2m": 25, "weather_code": 0}, "daily": {}}
    def get(url, **_kwargs):
        return Response({"results": places} if "geocoding" in url else forecast)
    monkeypatch.setattr(tools.requests, "get", get)
    selected = tools.weather_data("Santiago, Santiago, Cuba", "es")
    assert selected["place"]["id"] == 2
    assert selected["current"]["is_day"] == 1  # historical clients retain their field
    assert selected["current"]["is_day_known"] is False  # no claim about missing data
    with pytest.raises(tools.WeatherError, match="not_found"):
        tools.weather_data("Santiago, España", "es")
    # An explicitly selected provider ID survives a translated region label.
    assert tools.weather_data("Santiago, Havana, Cuba", "es", place_id=2)["place"]["id"] == 2
