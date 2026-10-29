"""Regression tests for quota-free tools, state-safe controls and bounded agent behavior."""
from __future__ import annotations

from datetime import date
from unittest import mock

import pytest
import requests

from michiflin import agent, ai, chat, config, extras, maintenance, miniapp, queue, state, tools


class Response:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status
        self.ok = status < 400
    def json(self): return self.data
    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(str(self.status_code))


def test_translation_fallback_preserves_whole_multibyte_text_and_limits_bytes():
    text = ("Una oración con ñ y acentos. " * 150).strip()[:4400]
    seen = []
    def post(url, **kwargs):
        assert url == "https://translate.example.org/translate"
        chunk = kwargs["json"]["q"]
        seen.append(chunk)
        assert len(chunk.encode("utf-8")) <= 3500
        assert kwargs["json"]["source"] == "es" and kwargs["json"]["target"] == "en"
        assert kwargs["allow_redirects"] is False
        return Response({"translatedText": chunk.upper()})
    with mock.patch.object(extras.requests, "get", side_effect=requests.ConnectionError()), \
         mock.patch.object(extras.requests, "post", side_effect=post), \
         mock.patch.object(config, "LIBRETRANSLATE_URL", "https://translate.example.org/translate"), \
         mock.patch.object(state, "check_work_allowed"):
        result = extras.translate_detailed(text, "en", "es")
    assert result["text"] == text.upper()  # separators and every byte retained
    assert result["provider"] == "LibreTranslate"
    assert len(seen) > 1


def test_translation_detection_never_guesses_or_returns_partial_text():
    with mock.patch.object(extras.requests, "get", side_effect=requests.ConnectionError()) as get:
        result = extras.translate_detailed("bonjour", "es", "auto")
    assert result["text"] == "" and result["detected"] == "auto"
    assert get.call_count == 1  # no untrusted public translation-memory fallback
    assert extras.translate_detailed("x" * 4501, "es", "en")["text"] == ""
    assert extras.translate_detailed("same text", "en", "en")["text"] == "same text"


def test_translation_primary_detects_language_and_preserves_long_text():
    def get(_url, **kwargs):
        word = kwargs["params"]["q"]
        return Response([[[word.upper(), word, None, None]], None, "es"])
    text = ("hola mundo. " * 450).strip()[:4400]
    with mock.patch.object(extras.requests, "get", side_effect=get), mock.patch.object(state, "check_work_allowed"):
        result = extras.translate_detailed(text, "en")
    assert result == {"text": text.upper(), "detected": "es", "provider": "Google Translate"}


def test_weather_selects_only_search_candidates_and_builds_forecast():
    places = [dict(id=1, name="Santiago", admin1="Cuba", country="Cuba", latitude=20, longitude=-75),
              dict(id=2, name="Santiago", admin1="Chile", country="Chile", latitude=-33, longitude=-70)]
    body = {"timezone":"America/Santiago", "current":{"time":"2026-10-29T12:00", "temperature_2m":20,
            "weather_code":63, "is_day":1, "apparent_temperature":19, "relative_humidity_2m":60, "wind_speed_10m":7, "precipitation":1},
            "daily":{"time":["2026-10-29"], "weather_code":[63], "temperature_2m_max":[22], "temperature_2m_min":[15], "precipitation_probability_max":[80]}}
    def get(url, **kwargs):
        if "geocoding" in url: return Response({"results":places})
        assert kwargs["params"]["latitude"] == -33
        return Response(body)
    with mock.patch.object(tools.requests, "get", side_effect=get):
        data = tools.weather_data("Santiago", "en", 2)
        assert data["place"]["id"] == 2
        assert len(data["candidates"]) == 2
        assert data["current"]["description"] == "rain"
        assert data["days"][0]["rain"] == 80
        with pytest.raises(tools.WeatherError): tools.weather_data("Santiago", "en", 55)


def test_history_official_windows_cached_and_missing_token():
    tools._cache.clear()
    observed = []
    def get(url, **kwargs):
        assert url == tools.ELTOQUE_ENDPOINT
        assert kwargs["headers"]["Authorization"].startswith("Bearer ")
        start, end = kwargs["params"]["date_from"], kwargs["params"]["date_to"]
        assert start.endswith("00:00:00") and end.endswith("23:59:59") and start[:10] == end[:10]
        observed.append(start[:10])
        return Response({"tasas": {"USD": 500 + len(observed), "EUR": 600 + len(observed)}})
    with mock.patch.object(config, "ELTOQUE_API_TOKEN", "synthetic-token"), mock.patch.object(tools.requests, "get", side_effect=get), mock.patch.object(state, "check_work_allowed"):
        data = tools.historical_rates(7)
        assert data["complete"] and len(data["history"]) == 7
        assert [item["date"] for item in data["history"]] == sorted(observed)
        assert all(date.fromisoformat(day) for day in observed)
        assert tools.historical_rates(7) == data
        assert len(observed) == 7
    with mock.patch.object(config, "ELTOQUE_API_TOKEN", ""):
        with pytest.raises(tools.RateError, match="authorization"): tools.historical_rates(7)
    tools._cache.clear()


def test_history_stops_on_rate_limit_and_never_fabricates_missing_points():
    tools._cache.clear()
    responses = [Response({"tasas":{"USD": 500}}), Response({}, 429)]
    with mock.patch.object(config, "ELTOQUE_API_TOKEN", "synthetic-token"), mock.patch.object(tools.requests, "get", side_effect=responses) as get, mock.patch.object(state, "check_work_allowed"):
        data = tools.historical_rates(7)
    assert get.call_count == 2
    assert data["complete"] is False and data["error"] == "rate_limit" and len(data["history"]) == 1
    tools._cache.clear()


def test_agent_understands_natural_capability_questions_without_model_or_search():
    variants = ["¿qué puedes hacer ?", "Oye, dime qué puedes hacer por favor", "¿Qué funciones tienes?", "What can you do?", "Please tell me what you can do here?"]
    for query in variants:
        chat.start_capture()
        with mock.patch.object(chat, "sender", return_value=""), mock.patch.object(tools, "search", side_effect=AssertionError("must be local")), \
             mock.patch.object(ai, "complete", side_effect=AssertionError("no model quota")):
            agent.handle(19, query, "en" if query.lower().startswith(("what", "please")) else "es")
        output = " ".join(chat.stop_capture())
        assert "clima" in output.lower() or "weather" in output.lower(), query
        assert "programador" in output.lower() or "coder" in output.lower(), query


def test_agent_weather_followup_uses_previous_context_without_ai():
    agent.forget(20)
    with mock.patch.object(chat, "sender", return_value=""), mock.patch.object(tools, "weather_text", side_effect=lambda city, _: city), \
         mock.patch.object(ai, "complete", side_effect=AssertionError("no model quota")):
        chat.start_capture(); agent.handle(20, "clima en La Habana", "es"); first = chat.stop_capture()
        chat.start_capture(); agent.handle(20, "¿y en Santiago?", "es"); second = chat.stop_capture()
    assert first == ["La Habana"] and second == ["Santiago"]
    agent.forget(20)


def test_hello_read_only_mode_snapshot_does_not_write_or_wake():
    with mock.patch.object(chat, "sender", return_value="owner@example.org"), mock.patch.object(state, "get_kill_switch", return_value=True), \
         mock.patch.object(state, "get_nauta_strict", return_value=False), mock.patch.object(state, "setting", return_value=15), \
         mock.patch.object(state, "set_kill_switch") as kill, mock.patch.object(state, "set_nauta_strict") as nauta, \
         mock.patch.object(queue, "wake") as wake, mock.patch.object(miniapp.mail_profiles, "effective_settings", return_value={"part_mb":15}), \
         mock.patch.object(miniapp, "models_view", return_value={}), mock.patch.object(miniapp, "health_view", return_value={}), \
         mock.patch.object(miniapp, "settings_view", return_value=[]), mock.patch.object(miniapp, "jobs_view", return_value=[]), \
         mock.patch.object(miniapp.usage, "quota_view", return_value={}):
        result = miniapp.op_hello(7, {}, "es")
    assert result["modes"]["kill_switch"]["active"] is True
    assert result["modes"]["nauta_strict"] == {"active": False, "part_mb": 15}
    kill.assert_not_called(); nauta.assert_not_called(); wake.assert_not_called()


def test_timed_maintenance_is_explicit_and_expires_without_grant_changes():
    with state.lock:
        old = state.data.get("maintenance_notice")
        state.data["maintenance_notice"] = {}
    try:
        with mock.patch.object(state, "save"), mock.patch.object(queue, "wake") as wake, \
             mock.patch("michiflin.live_web.pause_for_maintenance"), mock.patch("michiflin.maintenance.threading.Thread"):
            assert maintenance.get()["active"] is False
            wake.assert_not_called()
            with mock.patch.object(maintenance.time, "time", return_value=1000):
                notice = maintenance.set_notice(True, "Aviso", "Notice", 180)
            assert notice["active"] and notice["expires"] == 11800
            assert state.maintenance_generation() == 1
            # Updating a notice without a duration preserves the existing deadline.
            with mock.patch.object(maintenance.time, "time", return_value=1050):
                assert maintenance.set_notice(True, "Otro aviso", "Notice")["expires"] == 11800
            with mock.patch.object(maintenance.time, "time", return_value=11800):
                assert maintenance.expire_if_due()
                assert not state.maintenance_active()
                assert not maintenance.expire_if_due()
            assert state.maintenance_generation() == 1
            assert wake.call_count >= 2
            with pytest.raises(ValueError): maintenance.set_notice(True, "Aviso", "Notice", 525601)
    finally:
        with state.lock: state.data["maintenance_notice"] = old


def test_untrusted_translation_memory_is_not_used_and_failed_chunks_do_not_leak_partial():
    with mock.patch.object(config, "LIBRETRANSLATE_URL", ""), \
         mock.patch.object(extras.requests, "get", side_effect=requests.ConnectionError()) as get, \
         mock.patch.object(extras.requests, "post") as post:
        assert extras.translate_detailed("hello", "es", "en")["text"] == ""
        assert get.call_count == 1
        post.assert_not_called()
    with mock.patch.object(config, "LIBRETRANSLATE_URL", "http://example.org/translate"), \
         mock.patch.object(extras.requests, "get", side_effect=requests.ConnectionError()), \
         mock.patch.object(extras.requests, "post") as post:
        assert extras.translate_detailed("hello", "es", "en")["text"] == ""
        post.assert_not_called()  # only explicit HTTPS endpoints
    calls = 0
    def get(_url, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2: raise requests.ConnectionError()
        return Response([[[kwargs["params"]["q"].upper()]], None, "en"])
    with mock.patch.object(config, "LIBRETRANSLATE_URL", ""), \
         mock.patch.object(extras.requests, "get", side_effect=get), mock.patch.object(state, "check_work_allowed"):
        assert extras.translate_detailed("hello " * 730, "es", "en")["text"] == ""
        assert calls == 2


def test_agent_selected_model_passes_image_and_bounded_history_to_provider():
    import base64
    model = ai.Model("test", "vision", {"chat", "vision"}, 100)
    binary = b"synthetic-image-content"
    args = {"space":"smart_agent", "text":"What is this?", "agent_model":model.key,
            "file":{"name":"image.png", "mime":"image/png", "b64":base64.b64encode(binary).decode()},
            "history":[["user","earlier context"], ["assistant","earlier reply"]]}
    with mock.patch.object(ai, "model_by_key", return_value=model), \
         mock.patch.object(ai, "complete_exact", return_value=ai.AIResult(text="A test image", model="vision", provider="Test")) as complete, \
         mock.patch.object(miniapp, "models_view", return_value={}):
        result = miniapp.op_ask(5, args, "en")
    messages, key = complete.call_args.args[:2]
    assert key == model.key and messages[1]["content"] == "earlier context"
    assert messages[2]["content"] == "earlier reply" and messages[-1]["content"] == "What is this?"
    assert complete.call_args.kwargs["images"] == [("image/png", binary)]
    assert result["text"] == "A test image"


def test_agent_attachment_rejects_unsupported_media_and_unknown_model():
    import base64
    model = ai.Model("test", "text", {"chat"}, 100)
    item = {"name":"x.png", "mime":"image/png", "b64":base64.b64encode(b"synthetic").decode()}
    with mock.patch.object(ai, "model_by_key", return_value=model), mock.patch.object(ai, "complete_exact") as complete:
        with pytest.raises(miniapp.AppError, match="cannot read images"):
            miniapp.op_ask(5, {"space":"smart_agent", "agent_model":model.key, "file":item}, "en")
        complete.assert_not_called()
    with mock.patch.object(ai, "model_by_key", return_value=None):
        with pytest.raises(miniapp.AppError, match="no longer available"):
            miniapp.op_ask(5, {"space":"smart_agent", "agent_model":"nonexistent:model", "file":item}, "en")


def test_due_maintenance_restored_from_state_expires_once_under_concurrency():
    from concurrent.futures import ThreadPoolExecutor
    import copy
    import threading
    saved = copy.deepcopy(state.data.get("maintenance_notice"))
    try:
        with state.lock:
            state.data["maintenance_notice"] = {"active":True, "expires":1, "generation":4, "revision":9}
        notified = threading.Event()
        with mock.patch.object(state, "save"), mock.patch.object(queue, "wake") as wake, \
             mock.patch.object(maintenance, "sync_profile"), \
             mock.patch.object(miniapp, "_notify_maintenance", side_effect=lambda _notice: notified.set()) as notify:
            with ThreadPoolExecutor(max_workers=8) as pool:
                outcomes = list(pool.map(lambda _: maintenance.expire_if_due(), range(30)))
            assert notified.wait(3)
        assert outcomes.count(True) == 1
        assert state.data["maintenance_notice"]["active"] is False
        assert state.data["maintenance_notice"]["generation"] == 4
        assert state.data["maintenance_notice"]["revision"] == 10
        assert wake.call_count == 1 and notify.call_count == 1
    finally:
        with state.lock: state.data["maintenance_notice"] = saved
