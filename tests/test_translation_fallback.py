"""Translation rate limits and independent fallback; synthetic data only."""
import json
import time
import unittest
from unittest import mock

import requests

from michiflin import config, extras, miniapp, state


def response(status, payload, headers=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload).encode()
    result.headers.update(headers or {})
    result.url = "https://synthetic.example.invalid/"
    return result


class TranslationFallbackTests(unittest.TestCase):
    def setUp(self):
        extras._TRANSLATION_BACKOFF.clear()
        self.addCleanup(extras._TRANSLATION_BACKOFF.clear)
        self.stack = []
        for patch in (mock.patch.object(config, "LIBRETRANSLATE_URL", ""),
                      mock.patch.object(config, "APERTIUM_URL", "https://beta.apertium.org/apy"),
                      mock.patch.object(config, "APERTIUM_ENABLED", True),
                      mock.patch.object(state, "check_work_allowed")):
            patch.start()
            self.addCleanup(patch.stop)

    def test_google_429_backoff_and_successful_independent_detection(self):
        calls = []
        def post(url, **options):
            self.assertTrue(url.startswith("https://beta.apertium.org/apy/"))
            self.assertNotIn("hola", url.lower())
            self.assertFalse(options["allow_redirects"])
            calls.append((url, options["data"]))
            if url.endswith("/identifyLang"):
                return response(200, {"spa": 0.97, "cat": 0.02})
            self.assertEqual(options["data"]["langpair"], "spa|eng")
            return response(200, {"responseStatus":200, "responseData":{"translatedText":"Hello world"}})
        with mock.patch.object(extras.requests, "get", return_value=response(429, {}, {"Retry-After":"600"})) as get, \
             mock.patch.object(extras.requests, "post", side_effect=post):
            translated = extras.translate_detailed("hola mundo", "en")
            self.assertEqual(translated, {"text":"Hello world", "detected":"es", "provider":"Apertium (beta)"})
            self.assertGreater(extras._TRANSLATION_BACKOFF["google"], time.monotonic() + 590)
            again = extras.translate_detailed("otra oración", "en", "es")
            self.assertEqual(again["text"], "Hello world")
            self.assertEqual(get.call_count, 1)  # respect the provider's cooldown
        self.assertEqual(len(calls), 3)

    def test_low_confidence_detection_never_guesses_a_source(self):
        def post(url, **_options):
            self.assertTrue(url.endswith("/identifyLang"))  # no translation call
            return response(200, {"cat":0.60, "spa":0.39})
        with mock.patch.object(extras.requests, "get", side_effect=requests.ConnectionError()), \
             mock.patch.object(extras.requests, "post", side_effect=post):
            result = extras.translate_detailed("hola", "en", "auto")
        self.assertEqual(result["text"], "")
        self.assertEqual(result["error"], "detection_uncertain")
        with mock.patch.object(extras, "translate_detailed", return_value=result):
            with self.assertRaisesRegex(miniapp.AppError, "Elige el idioma de origen"):
                miniapp.op_translate(1, {"text":"hola", "source":"auto", "target":"en"}, "es")

    def test_explicit_source_preserves_unknown_markers_and_blocks_redirects(self):
        def post(url, **kwargs):
            self.assertTrue(url.endswith("/translate"))
            self.assertEqual(kwargs["data"]["langpair"], "eng|spa")
            self.assertEqual(kwargs["data"]["format"], "txt")
            self.assertNotIn("markUnknown", kwargs["data"])
            self.assertFalse(kwargs["allow_redirects"])
            return response(200, {"responseStatus":200, "responseData":{"translatedText":"Hola *example"}})
        with mock.patch.object(extras.requests, "get", side_effect=requests.ConnectionError()), \
             mock.patch.object(extras.requests, "post", side_effect=post):
            result = extras.translate_detailed("Hello example", "es", "en")
        self.assertEqual(result["text"], "Hola *example")
        self.assertEqual(result["provider"], "Apertium (beta)")

    def test_unsupported_pair_and_malformed_results_are_not_fabricated(self):
        with mock.patch.object(extras.requests, "get", side_effect=requests.ConnectionError()), \
             mock.patch.object(extras.requests, "post", return_value=response(400, {"explanation":"pair missing"})):
            result = extras.translate_detailed("Привет", "es", "ru")
        self.assertEqual(result["error"], "pair_unavailable")
        self.assertFalse(result["text"])
        with mock.patch.object(extras.requests, "get", side_effect=requests.ConnectionError()), \
             mock.patch.object(extras.requests, "post", return_value=response(200, {"responseStatus":200, "responseData":{}})):
            self.assertFalse(extras.translate_detailed("hello", "es", "en")["text"])

    def test_second_chunk_failure_never_returns_partial_text(self):
        def post(_url, **kwargs):
            calls = post.calls
            post.calls += 1
            self.assertLessEqual(len(kwargs["data"]["q"].encode()), 3500)
            if calls:
                return response(503, {})
            return response(200, {"responseStatus":200, "responseData":{"translatedText":"a partial result"}})
        post.calls = 0
        with mock.patch.object(extras.requests, "get", side_effect=requests.ConnectionError()), \
             mock.patch.object(extras.requests, "post", side_effect=post):
            result = extras.translate_detailed("hola ñ " * 600, "en", "es")
        self.assertGreaterEqual(post.calls, 2)
        self.assertFalse(result["text"])

    def test_rate_limit_and_disabled_fallback_do_not_repeat_requests(self):
        with mock.patch.object(extras.requests, "get", return_value=response(429, {}, {"Retry-After":"600"})) as get, \
             mock.patch.object(extras.requests, "post", return_value=response(429, {}, {"Retry-After":"720"})) as post:
            self.assertEqual(extras.translate_detailed("hello", "es", "en")["error"], "rate_limited")
            self.assertEqual(extras.translate_detailed("hello", "es", "en")["error"], "rate_limited")
            self.assertEqual(get.call_count, 1)
            self.assertEqual(post.call_count, 1)
        extras._TRANSLATION_BACKOFF.clear()
        with mock.patch.object(config, "APERTIUM_ENABLED", False), \
             mock.patch.object(extras.requests, "get", side_effect=requests.ConnectionError()), \
             mock.patch.object(extras.requests, "post") as post:
            self.assertFalse(extras.translate_detailed("hello", "es", "en")["text"])
            post.assert_not_called()

    def test_maintenance_is_not_swallowed(self):
        with mock.patch.object(state, "check_work_allowed", side_effect=state.WorkPaused(maintenance=True)), \
             mock.patch.object(extras.requests, "get") as get, mock.patch.object(extras.requests, "post") as post:
            with self.assertRaises(state.WorkPaused):
                extras.translate_detailed("hello", "es", "en")
            get.assert_not_called()
            post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
