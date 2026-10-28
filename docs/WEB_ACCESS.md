# Web access and official account connections

## Interactive web sessions

The **Web → Accounts and sites** screen opens an interactive, isolated Playwright-controlled Chromium browser. The account owner enters their own login and completes any verification; no website-specific API registration is required. It works only in a supported Delta Chat client with the experimental WebXDC realtime channel and in an authorized individual encrypted chat. Frames and typed input go over that ephemeral channel, not WebXDC status updates; the server processes them in memory and delivery may fail or be delayed. A phone was observed opening public pages in disposable staging; that does not establish compatibility with every client or an authenticated social login.

**Sharper** frames use up to 960×1520 pixels while taps still address a 480×760 CSS viewport. Very complex frames automatically fall back to 480×760 to fit the channel; **Less data** uses that smaller size from the start. Magnification is local to the displayed image: pan it to see hidden portions, and use the arrows to scroll the remote page. Still-frame refresh every five seconds is opt-in and uses data; it is not video streaming. The address bar accepts a direct address for the same service; provider-driven redirects and sign-in pop-ups may visit other domains. For a different service, close the browser and open a separate session.

Some web-index results have video actions. A download attempt uses that person's saved browser state for the service if available, or public access otherwise; search results can be incomplete and neither a recognized link nor a queued job proves a download succeeded.

Choose **Save session** explicitly to encrypt the browser state for that person and site. The state may contain bearer-equivalent cookies, local storage and IndexedDB, including data from third-party sign-in domains visited within that browser, so it is as sensitive as a credential. It is encrypted under the separate stable `WEB_SESSION_KEY`, in `CONFIG_DIR/browser-sessions-v1`, with distinct keys and filenames for each person and site; backups encrypt the state again. A key change or loss makes old sessions unavailable. Saving does not prove login succeeded, and providers can expire or reject sessions. A normal browser on the phone has separate storage; its sessions are not transferred to the bot.

**Send screenshot to chat** deliberately creates a persistent attachment in the encrypted chat, up to 3,000 CSS pixels starting at the current scroll position; the attachment and backups can outlive the browser session. **Download this video** queues a supported video from the current page using the same person's saved cookies and user-agent, and sends the result subject to normal download settings and limits. It does not forward local storage or IndexedDB to yt-dlp; websites that require those mechanisms may fail. Private/unlisted URLs are kept in server-side job metadata, which is included in encrypted state backups, but are not included in the realtime acknowledgment or WebXDC status updates. Only download content that you own or have permission to download.

**Forget session** closes active browsers for that person/site and deletes the current encrypted file. It does not revoke the provider's session or erase older encrypted backups; sign out/revoke at the provider to invalidate it. Production must provide Linux namespaces, sudo, chroot, socat, iproute2, util-linux, a Playwright Chromium installation, yt-dlp and ffmpeg. Without isolation or `WEB_SESSION_KEY`, the browser fails closed. Browser and media workers use an HTTPS gateway which blocks private-address destinations; they do not inherit the bot's environment or home directory.

The browser was tested with a demonstration login, including encrypted save and restore, and media with a protected **local test fixture**. In disposable staging a mobile client displayed public pages, while Facebook presented challenges and Google rejected a software-controlled login. A public web index found TikTok results and one separately selected public TikTok video was downloaded locally, with its advertised permission not independently verified. These observations do not prove social account login, private YouTube downloads, an authenticated Facebook capture, MFA, or native TikTok search. Google may refuse controlled browsers; providers can require CAPTCHA, block automation, change pages, or prohibit the requested access. Complete challenges yourself; do not bypass them or promise universal access.

A proxy or Cloudflare WARP can change the network path but is neither a browser nor proof of trust; it may increase challenges or invalidate sessions. The production workflow only enables its optional media proxy after a connectivity check, and a successful workflow step alone does not prove WARP is active. Currently the isolated browser and its private-media worker both use the validated direct HTTPS gateway, not that optional proxy. They do not guarantee the same public IP across separate connections or hosted-runner jobs. No IP rotation or alternate-route/client retry is attempted after a recognized verification challenge. Geographic IP spoofing in the ordinary downloader is disabled. Any future proxy support must preserve destination validation, per-user isolation and consistent routing for the browser and its downloads.

`YTDLP_COOKIES` remains a single administrator-only facility for the older public-link downloader, not a per-person session; never place other people's cookies there. Official connections below remain optional and are separate from the browser.

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

The interactive browser offers links for Instagram, Revolico, Arena.ai, DeepSeek Chat and Z.AI, but login and restoration on those services have **not** been verified. Provider rules and technical blocks still apply. Instagram's [official platform](https://developers.facebook.com/docs/instagram-platform/overview/) targets professional accounts; Arena.ai's [terms](https://help.arena.ai/articles/5629909088-terms-of-use) restrict programmatic use without applicable authorization. The [DeepSeek API](https://api-docs.deepseek.com/) and the bot's Z.AI API route are separate from those websites' browser sessions. Do not treat an API connection, a saved state, or a displayed login form as proof of a working website login.

Mock tests and simulated browser screenshots do not establish a real social connection. Confirm each provider app and account authorization individually before claiming operational access.
