# Web access and official account connections

The general reader opens accessible public pages; it is **not** a persistent logged-in browser. The cards for Instagram, Facebook, TikTok, Revolico, Arena.ai, Z.AI and DeepSeek Chat can also copy a website address for manual use. Opening a site manually does not connect the bot. `yt-dlp` support for public links changes by website and does not imply access to private or DRM-protected content. `YTDLP_COOKIES` is a single administrator-only facility, not a per-person social session; do not place other people's cookies there.

## Optional social API connections

YouTube, Facebook and TikTok have separate, opt-in, sender-specific flows using official APIs. They are **not automatically connected**: provider app registration, configuration, and each account holder's approval are necessary, and the provider may restrict or reject an application. WebXDC has no direct network access. No passwords, cookies, refresh tokens, or access tokens should be pasted into a chat or sent to WebXDC. Device/QR approvals are temporary and remain tied to the requesting sender. The implementation is read-only apart from token lifecycle operations such as revocation; it does not grant arbitrary private feeds or a general browser session.

The server stores tokens in `CONFIG_DIR/web-sessions` using a key derived from the separate, stable high-entropy `WEB_SESSION_KEY` (at least 32 characters). Filenames and encryption domains differ by service and sender. The outer state backup encrypts these files again. Losing/rotating `WEB_SESSION_KEY` makes old sessions unreadable. Disconnect removes the local encrypted file and requests provider revocation; if not confirmed, the account holder must revoke the app at the provider. Previously made encrypted backups can still contain an older copy until rotation; provider revocation is important to invalidate it.

### YouTube

1. Enable YouTube Data API v3 and create a Google OAuth client of type **TVs and limited-input devices**. Complete the consent-screen setup and any required verification.
2. Configure server secrets `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` and `WEB_SESSION_KEY`.
3. In **Web → Accounts and sites → My YouTube account**, choose Connect. Open the displayed Google device address in a browser, enter the short code, and authorize `youtube.readonly` on Google. Return and select **I have authorized**.
4. The first page of the account's own channel and subscriptions can be read, up to 20 results. This is not arbitrary private video access or a logged-in website.

The code is documented by [Google's device authorization guide](https://developers.google.com/youtube/v3/guides/auth/devices). If disconnect does not confirm revocation, use `https://myaccount.google.com/permissions`.

### Facebook

1. Create a Meta app, enable **Facebook Login for Devices**, and comply with Meta's review and allowed-use requirements. Configure `FACEBOOK_APP_ID`, `FACEBOOK_CLIENT_TOKEN` and `WEB_SESSION_KEY` as secrets.
2. The default request is only `public_profile`; the account owner opens `https://www.facebook.com/device`, enters the displayed code, and grants permission. Return and select **I have authorized**. The bot can then read the authorized account's basic profile.
3. Optionally set repository variable `FACEBOOK_READ_POSTS=1` only if the app and use are approved for `user_posts`. The owner must grant it; only their own posts are requested, and availability is subject to Meta's restrictions. No posting or deletion permission is requested.

The endpoint and error codes follow [Facebook Login for Devices](https://developers.facebook.com/documentation/facebook-login/for-devices). Device tokens can expire or be revoked and are not assumed valid just because a ciphertext file exists.

### TikTok

1. Register a TikTok developer app with Login Kit, configure an approved HTTPS redirect URI, and complete applicable review. Configure `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET`, `TIKTOK_OAUTH_REDIRECT_URI` (exactly the registered URI, without query or fragment), and `WEB_SESSION_KEY` as secrets.
2. In **My TikTok account**, choose Connect and scan the temporary QR using TikTok on another device. Approve the request there and return to select **I have authorized**. The backend verifies `client_ticket`, random `state` and the registered redirect before exchanging a code; the authorization code and tokens do not enter WebXDC.
3. The default permission is `user.info.basic`, which permits basic account information. `TIKTOK_READ_VIDEOS=1` optionally requests `video.list` **only after** the app and scope are approved; this allows the authorized user's own video list, not their private For You feed. Permissions can be declined and API availability depends on TikTok.

See TikTok's [QR authorization](https://developers.tiktok.com/docs/en/login-kit-qr-code-authorization) and [token management](https://developers.tiktok.com/docs/en/oauth-user-access-token-management) guides. The QR flow is implemented and mock-tested; without real app credentials and approval, actual provider behavior and review remain unverified.

## Other requested services

- **Instagram:** the [official platform](https://developers.facebook.com/docs/instagram-platform/overview/) targets professional/business or creator accounts and has app, authorization, permission and often review requirements. Its business-login flow needs an appropriate configured redirect or business setup; a universal personal Instagram feed is not available through this integration. It has **not** been implemented here and the manual link is not a connected session.
- **Revolico:** no reliable official authenticated API has been verified; only public reading/manual browsing is attempted. No account login is implemented.
- **Arena.ai:** its [terms](https://help.arena.ai/articles/5629909088-terms-of-use) restrict programmatic use without an applicable authorization/license. Do not automate the website or reuse cookies. A separately authorized [API](https://platform.arena.ai/docs) is not a website login.
- **DeepSeek Chat:** the [developer API](https://api-docs.deepseek.com/) is a separate, potentially paid product, not an authenticated session for `chat.deepseek.com`. No website login is implemented.
- **Z.AI:** the bot's configured API route is separate from a website session; availability, authorization and pricing depend on the actual key and service. No Z.AI website login is implemented.

Mock tests and simulated browser screenshots do not establish a real social connection. Confirm each provider app and account authorization individually before claiming operational access.
