# Web access and session scope

## Current behavior

- The reader and browser can open public pages accessible from the server. They do not perform interactive website login or maintain browser cookies as a persistent login per contact.
- An optional YouTube **Data API** connection uses Google's official device authorization, not a browser session. It is disabled until the deployer configures an OAuth client and a separate session-encryption key. It is not connected by default.
- `yt-dlp` has extractors for many public YouTube, Facebook, TikTok, Instagram, and other links. Support depends on the extractor, public access, website changes, and anti-bot checks; DRM and private content are not promised.
- `YTDLP_COOKIES` is a single global file used only for the administrator. It is not per-contact authentication; do not place other people's cookies there.
- Revolico's public pages can be tried with the general reader, but no stable official API or reliable extraction is claimed.

## Social services: requested scope and official routes

The requested scope for Instagram, Facebook, YouTube, and TikTok is **read-only**. Link cards offer reading, screenshots, copying, and downloading where accessible; they are not a browser with a personal session. YouTube's optional OAuth connection is limited to the owner's own channel and the first 20 subscriptions and requires explicit authorization. The other sites are manual browser links, not connected sessions.

- **YouTube:** the [Data API](https://developers.google.com/youtube/v3/guides/authentication) can access certain private data with OAuth, consent, and credentials for a Google Cloud application. It does not automatically permit playback of arbitrary private or restricted content.
- **TikTok:** [Login Kit and Display API](https://developers.tiktok.com/doc/login-kit-overview?enter_method=left_navigation) require an application, authorization, and approved permissions. `user.info.basic` and `video.list` have the scope described in the [permissions reference](https://developers.tiktok.com/doc/tiktok-api-scopes?enter_method=left_navigation); they do not grant unlimited access to private feeds.
- **Instagram:** the [official API](https://developers.facebook.com/docs/instagram-platform/overview/) documents professional accounts, not a universal personal browsing session. A Meta app and the appropriate permissions are needed.
- **Facebook:** capabilities depend on the Meta product, account type, review, and granted permissions. No API is claimed to replicate a personal feed freely, and credential-based scraping has not been implemented.

These flows require application registration, consent, encrypted storage separated by identity, and revocation; some providers also require a trusted web redirect, whereas YouTube's device flow does not. Provider restrictions or approvals may prevent access to certain data. WebXDC has no direct network access, so it is unsafe to imitate login or ask for passwords through messages. **Do not ask users to paste passwords or cookies into the bot.**

## Other evaluated websites

- **Arena.ai:** its [terms](https://help.arena.ai/articles/5629909088-terms-of-use) restrict automated or programmatic access without an applicable license or authorization. Do not automate the site or reuse cookies without an expressly permitted route. Inputs may be shared with model providers; a free interface does not imply confidential processing. See the [API documentation](https://platform.arena.ai/docs) and [terms](https://help.arena.ai/articles/5629909088-terms-of-use).
- **DeepSeek Chat:** the website and developer API are different products. The official API charges for tokens and Search/Fetch; it is not configured as a free route. Aggregators may offer free DeepSeek-named models under their own conditions. See [official pricing](https://api-docs.deepseek.com/quick_start/pricing/).
- **Revolico:** authenticated interaction has not been implemented and no public API for it has been verified. Reading public pages is not equivalent to login.
- **Z.AI:** the bot already has an OpenAI-compatible API route for Flash variants marked free in its [pricing table](https://docs.z.ai/guides/overview/pricing). No universal daily allowance for those variants was found there; Michiflin's caps are internal controls, not provider guarantees. Respect `429` responses and do not configure paid models on that key.

## Safe prerequisites for personal sessions

If website login is added later, each session must belong solely to the authenticated sender, remain isolated by address, expire, and be removable from the app. Never request or copy passwords or cookies between users. Before enabling it, use a provider-authorized flow, preferably OAuth or an official API, encrypted storage, and a separate test instance. Without staging, real login has not been tested and must not be presented as operational.

No website session is connected for Arena.ai, Revolico, DeepSeek Chat, or Z.AI. The app can copy their official addresses for manual use outside WebXDC; this does not grant the bot access. Z.AI's existing API route is distinct from a web session and depends on the key's validity, permissions, and fees.

## Optional YouTube connection

1. In Google Cloud, enable YouTube Data API v3 and create an OAuth client of type **TVs and limited-input devices**. Complete the consent-screen setup and any applicable verification/review. Do not request write scopes.
2. Configure `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, and a separate high-entropy `WEB_SESSION_KEY` of at least 32 characters as deployment secrets. Keep the encryption key stable across restarts. Do not share any secret through bot messages or WebXDC.
3. In Web → Accounts and sites → My YouTube account, choose Connect. In a separate browser, open the Google address, enter the displayed short code, and approve `youtube.readonly`. Return to the app and select “I have authorized”. WebXDC itself has no Internet access.
4. The bot can read the first page of the authorized account's channel and subscriptions. This is not a website login, private video downloader, or a general feed. Google may reject or expire authorization, or require app review.
5. Disconnect removes the local encrypted token and asks Google to revoke it. If revocation is not confirmed, revoke the application yourself at `https://myaccount.google.com/permissions`.

Device codes are temporary and kept only in memory. Refresh tokens are encrypted with the separate session key in sender-specific files; the existing backup encrypts the whole account again. Loss/rotation of the session key makes the old sessions unreadable. Never reuse a token between contacts. This integration needs an actual configured Google app and user consent; mocked tests do not prove that a real account is connected.
