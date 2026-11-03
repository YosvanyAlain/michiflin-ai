"""Synthetic provider errors; never contacts YouTube or uses identity/cookies."""
from contextlib import nullcontext
from unittest import mock

from michiflin import media, tools


def test_youtube_search_does_not_switch_route_after_challenge():
    routes = ["socks5://127.0.0.1:40000", ""]
    client = mock.Mock()
    client.extract_info.side_effect = RuntimeError("Sign in to confirm you're not a bot")
    with mock.patch.object(media, "available", return_value=True), \
         mock.patch.object(media, "_routes", return_value=routes), \
         mock.patch.object(media, "_options", side_effect=lambda route: {"proxy": route}), \
         mock.patch.object(media.yt_dlp, "YoutubeDL", side_effect=lambda _options: nullcontext(client)) as factory:
        result = tools._youtube_search("sintético", 3, "es")
    assert result == []
    assert factory.call_count == 1 and client.extract_info.call_count == 1


def test_youtube_search_may_retry_after_ordinary_network_failure():
    routes = ["socks5://127.0.0.1:40000", ""]
    first, second = mock.Mock(), mock.Mock()
    first.extract_info.side_effect = OSError("network unavailable")
    second.extract_info.return_value = {"entries": [{"id": "abcdefghijk", "title": "Prueba"}]}
    with mock.patch.object(media, "available", return_value=True), \
         mock.patch.object(media, "_routes", return_value=routes), \
         mock.patch.object(media, "_options", side_effect=lambda route: {"proxy": route}), \
         mock.patch.object(media.yt_dlp, "YoutubeDL", side_effect=[nullcontext(first), nullcontext(second)]) as factory:
        result = tools._youtube_search("sintético", 3, "es")
    assert len(result) == 1 and result[0].title == "Prueba"
    assert factory.call_count == 2


def test_verifying_on_a_different_device_is_not_promised_to_authorize_server():
    assert "no siempre autoriza al servidor" in media.describe_error("bot_check", "es")
    assert "does not always authorize the server" in media.describe_error("bot_check", "en")
