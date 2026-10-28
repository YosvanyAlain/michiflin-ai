"""Mocked official social flows. They do not claim a real provider login."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from michiflin import config, facebook_account as fb, social_vault as vault, tiktok_account as tt

A = "owner@example.org"
B = "other@example.org"


class SocialAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original = config.CONFIG_DIR
        config.CONFIG_DIR = Path(self.temp.name)
        self.env = mock.patch.dict(os.environ, {
            "WEB_SESSION_KEY": "k" * 48,
            "FACEBOOK_APP_ID": "1234567", "FACEBOOK_CLIENT_TOKEN": "test-client-token",
            "FACEBOOK_READ_POSTS": "0", "TIKTOK_CLIENT_KEY": "test-key", "TIKTOK_CLIENT_SECRET": "test-secret",
            "TIKTOK_OAUTH_REDIRECT_URI": "https://app.example.org/callback", "TIKTOK_READ_VIDEOS": "0",
        })
        self.env.start()
        self.work = mock.patch("michiflin.state.check_work_allowed")
        self.work.start()
        fb._pending.clear(); tt._pending.clear()

    def tearDown(self):
        self.work.stop()
        self.env.stop()
        config.CONFIG_DIR = self.original
        self.temp.cleanup()
        fb._pending.clear(); tt._pending.clear()

    def _facebook_start(self):
        data = {"code": "device-secret", "user_code": "A1NWZ9", "verification_uri": "https://www.facebook.com/device",
                "expires_in": 420, "interval": 5}
        with mock.patch.object(fb, "_request", return_value=(200, data)) as request:
            result = fb.start(A)
        self.assertEqual(result["user_code"], "A1NWZ9")
        self.assertNotIn("device-secret", str(result))
        self.assertEqual(request.call_args.kwargs["data"]["scope"], "public_profile")

    def test_facebook_device_flow_is_per_sender_encrypted_and_read_only(self):
        self._facebook_start()
        pending = (400, {"error": {"error_subcode": 1349174}})
        with mock.patch.object(fb, "_request", return_value=pending):
            self.assertTrue(fb.poll(A)["pending"])
        fb._pending[A]["last_poll"] = 0
        with mock.patch.object(fb, "_request", side_effect=[
            (200, {"access_token": "FB-SECRET", "expires_in": 3600}),
            (200, {"data": [{"permission": "public_profile", "status": "granted"}]}),
        ]) as request:
            result = fb.poll(A)
        self.assertTrue(result["connected"])
        self.assertNotIn("FB-SECRET", str(result))
        self.assertEqual(request.call_args_list[1].args[:2], ("GET", fb.PERMISSIONS))
        self.assertTrue(fb.status(A)["connected"])
        self.assertFalse(fb.status(B)["connected"])
        self.assertNotIn(b"FB-SECRET", vault.path("facebook", A).read_bytes())
        self.assertEqual(vault.path("facebook", A).stat().st_mode & 0o777, 0o600)
        with mock.patch.object(fb, "_request", return_value=(200, {"name": "Mock Owner"})):
            self.assertEqual(fb.read(A, "profile")["items"][0]["title"], "Mock Owner")
        with self.assertRaisesRegex(vault.AccountError, "scope"):
            fb.read(A, "posts")
        with mock.patch.object(fb, "_request", return_value=(200, {"success": True})) as revoke:
            self.assertTrue(fb.disconnect(A)["revoked"])
        self.assertEqual(revoke.call_args.args[:2], ("DELETE", fb.PERMISSIONS))
        self.assertFalse(vault.path("facebook", A).exists())

    def test_facebook_rejects_unrequested_permissions_and_untrusted_url(self):
        self._facebook_start()
        with mock.patch.object(fb, "_request", side_effect=[
            (200, {"access_token": "SECRET", "expires_in": 3600}),
            (200, {"data": [{"permission": "public_profile", "status": "granted"},
                            {"permission": "pages_manage_posts", "status": "granted"}]}),
        ]), self.assertRaisesRegex(vault.AccountError, "scope"):
            fb.poll(A)
        self.assertFalse(vault.path("facebook", A).exists())
        bad = {"code": "x", "user_code": "A1NWZ9", "verification_uri": "https://facebook.com.evil.org/device", "expires_in": 420}
        with mock.patch.object(fb, "_request", return_value=(200, bad)), self.assertRaisesRegex(vault.AccountError, "response"):
            fb.start(B)

    def test_cancel_does_not_revoke_existing_facebook_grant(self):
        vault.save("facebook", A, {"access_token": "old", "expires": time.time() + 1000, "scopes": ["public_profile"]})
        self._facebook_start()
        with mock.patch.object(fb, "_request") as request:
            result = fb.cancel(A)
        request.assert_not_called()
        self.assertTrue(result["connected"])
        self.assertFalse(result["pending"])
        self.assertTrue(vault.path("facebook", A).exists())

    def _tiktok_start(self):
        response = {"token": "temporary-token", "scan_qrcode_url": "aweme://authorize?client_key=test-key&client_ticket=tobefilled"}
        with mock.patch.object(tt, "_request", return_value=(200, response)) as request:
            data = tt.start(A)
        self.assertTrue(data["qr_png"].startswith("iVBOR"))
        self.assertNotIn("temporary-token", str(data))
        self.assertEqual(request.call_args.kwargs["data"]["scope"], "user.info.basic")
        return tt._pending[A]

    def test_tiktok_qr_confirmation_checks_ticket_state_redirect_and_encrypts(self):
        pending = self._tiktok_start()
        confirmed = {"client_ticket": pending["ticket"], "status": "confirmed", "state": pending["state"],
                     "redirect_uri": "https://app.example.org/callback?code=AUTH-CODE"}
        tokens = {"access_token": "TT-SECRET", "refresh_token": "TT-REFRESH", "expires_in": 3600,
                  "refresh_expires_in": 86400, "scope": "user.info.basic"}
        with mock.patch.object(tt, "_request", side_effect=[(200, confirmed), (200, tokens)]) as request:
            result = tt.poll(A)
        self.assertTrue(result["connected"])
        self.assertNotIn("TT-SECRET", str(result))
        self.assertEqual(request.call_args_list[1].kwargs["data"]["redirect_uri"], "https://app.example.org/callback")
        self.assertFalse(tt.status(B)["connected"])
        encrypted = vault.path("tiktok", A).read_bytes()
        self.assertNotIn(b"TT-SECRET", encrypted); self.assertNotIn(b"TT-REFRESH", encrypted)
        with mock.patch.object(tt, "_request", return_value=(200, {"data": {"user": {"display_name": "Mock TT"}}, "error": {"code": "ok"}})):
            self.assertEqual(tt.read(A, "profile")["items"][0]["title"], "Mock TT")
        with self.assertRaisesRegex(vault.AccountError, "scope"):
            tt.read(A, "videos")
        with mock.patch.object(tt, "_request", return_value=(200, {})) as revoke:
            self.assertTrue(tt.disconnect(A)["revoked"])
        self.assertEqual(revoke.call_args.args[:2], ("POST", tt.REVOKE))

    def test_tiktok_rejects_csrf_ticket_redirect_and_broader_scope(self):
        pending = self._tiktok_start()
        valid = {"client_ticket": pending["ticket"], "status": "confirmed", "state": pending["state"],
                 "redirect_uri": "https://app.example.org/callback?code=test"}
        for mutation in ({"client_ticket": "wrong"}, {"state": "wrong"},
                         {"redirect_uri": "https://attacker.invalid/callback?code=test"},
                         {"redirect_uri": "https://app.example.org/callback?code=a&code=b"}):
            with self.subTest(mutation=mutation), self.assertRaisesRegex(vault.AccountError, "response"):
                tt._returned_code(dict(valid, **mutation), pending, "https://app.example.org/callback")
        tokens = {"access_token": "secret", "refresh_token": "refresh", "expires_in": 3600,
                  "scope": "user.info.basic,video.publish"}
        with self.assertRaisesRegex(vault.AccountError, "scope"):
            tt._validated_tokens(tokens, ["user.info.basic"])
        with self.assertRaisesRegex(vault.AccountError, "response"):
            tt._scan_url("https://evil.invalid/?client_key=test-key&client_ticket=x", "test-key", "ticket")

    def test_optional_facebook_posts_request_needs_grant(self):
        with mock.patch.dict(os.environ, {"FACEBOOK_READ_POSTS": "1"}):
            data = {"code": "device", "user_code": "ABCDEF", "verification_uri": "https://www.facebook.com/device", "expires_in": 300}
            with mock.patch.object(fb, "_request", return_value=(200, data)) as request:
                fb.start(A)
            self.assertEqual(request.call_args.kwargs["data"]["scope"], "public_profile,user_posts")
            with mock.patch.object(fb, "_request", side_effect=[
                (200, {"access_token": "token", "expires_in": 3600}),
                (200, {"data": [{"permission": "public_profile", "status": "granted"},
                                {"permission": "user_posts", "status": "granted"}]}),
            ]):
                self.assertTrue(fb.poll(A)["posts_granted"])
            with mock.patch.object(fb, "_request", return_value=(200, {"data": [{"created_time": "test", "message": "My own post"}]})):
                self.assertEqual(fb.read(A, "posts")["items"][0]["description"], "My own post")

    def test_tiktok_refresh_rotates_token_without_exposing_it(self):
        vault.save("tiktok", A, {"access_token": "old", "refresh_token": "refresh", "expires": time.time() - 1,
                                  "refresh_expires": time.time() + 3600, "scopes": ["user.info.basic"]})
        refreshed = {"access_token": "new-token", "refresh_token": "new-refresh", "expires_in": 3600,
                     "refresh_expires_in": 86400, "scope": "user.info.basic"}
        with mock.patch.object(tt, "_request", side_effect=[
            (200, refreshed), (200, {"data": {"user": {"display_name": "Mock"}}, "error": {"code": "ok"}}),
        ]):
            result = tt.read(A, "profile")
        self.assertEqual(result["items"][0]["title"], "Mock")
        self.assertNotIn("new-token", str(result))
        self.assertEqual(vault.load("tiktok", A)["refresh_token"], "new-refresh")

    def test_lost_key_allows_local_removal_but_does_not_claim_revocation(self):
        vault.save("tiktok", A, {"access_token": "secret"})
        with mock.patch.dict(os.environ, {"WEB_SESSION_KEY": "different-key-" + "x" * 40}):
            self.assertTrue(tt.status(A)["vault_error"])
            self.assertFalse(tt.disconnect(A)["revoked"])
        self.assertFalse(vault.path("tiktok", A).exists())

    def test_endpoints_and_redirects_are_fail_closed(self):
        with self.assertRaisesRegex(vault.AccountError, "endpoint"):
            vault.request({"https://allowed.example/"}, "POST", "https://attacker.invalid/")
        for bad in ("http://example.org/callback", "https://localhost/callback", "https://u:p@example.org/callback", "https://example.org/callback?x=1"):
            self.assertFalse(tt._safe_redirect(bad))


if __name__ == "__main__":
    unittest.main()
