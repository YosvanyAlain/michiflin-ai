# Michiflin AI · User guide

This guide walks through the mini app and the chat features. For setup and configuration see the [README](../README.md).

- [Getting started](#getting-started)
- [Home](#home)
- [AI](#ai)
- [Web](#web)
- [Apps](#apps)
- [Downloads](#downloads)
- [File kit and joining parts](#file-kit-and-joining-parts)
- [Settings](#settings)
- [Status and administration](#status-and-administration)
- [Classic chat mode](#classic-chat-mode)
- [Saving data](#saving-data)
- [Privacy](#privacy)

## Getting started

1. Open the private chat with the bot in Delta Chat. Production starts owner-only; the owner may explicitly authorize temporary guests. Old allow-list entries do not grant access.
2. The bot sends the mini app as soon as the chat is set up, with a short welcome explaining that the app is the whole bot; tap **Start** to open it. The first time, a brief introduction shows the tabs and how answers arrive (you can watch it again from **How it works**). If you lose the app, send `/app` for a fresh copy. Pin the chat or keep the app message at hand.
3. Use the app for everything. Every action is sent to the bot as a small message inside the same chat, and the answer comes back to the app, usually within a few seconds to a couple of minutes.

You can close the app at any time: pending work completes and shows up when you return. The indicator at the top shows requests in progress, and a request with no answer after a few minutes is marked so you can retry it.

Text written in the chat itself only gets a short reply pointing to the app. Files you send (voice notes, photos, documents) are still transcribed or analyzed.

## Home

<p align="center">
  <img src="media/en/home.png" width="230" alt="Home">
  <img src="media/en/home-dark.png" width="230" alt="Home in dark theme">
</p>

- **Search box:** type a search or paste a link. The chips below choose Web, News, Videos or Wikipedia. The button at the left of the bar switches between the **standard** engine (keyless, no limits, the default) and the **advanced** engine (better results, daily limit).
- **Status chips:** time until the next server restart, items in the queue and the last update.
- **Shortcuts:** the most used apps, one tap away.

The theme follows the device (light or dark) unless you choose one in Settings.

## AI

<p align="center">
  <img src="media/en/ai.png" width="230" alt="AI spaces and models">
  <img src="media/en/agent.png" width="230" alt="Agent chat">
  <img src="media/en/model-chat.png" width="230" alt="Chat with one model">
</p>

The AI tab has one unified agent, Coder, images and expandable model cards:

- **Agent — No model quota:** searches and reads sources without calling an AI model. It handles math, conversions, time, weather, rates, translation and summaries, and can use limited conversational context.
- **Agent — With AI / API:** chooses an exact model or automatic routing and may use up to four bounded public-tool steps. It consumes provider quota or balance. With an attachment it sends the supported media and recent context directly to the chosen model. It does not log in, send messages or execute code. Previous automatic-chat history is available separately and is not deleted.
- **Coder:** builds small programs and web pages (see below).
- **AI images:** a small studio to create images. Choose **Automatic** or one exact model; each chip shows today's uses. Cloudflare models share one free daily budget; when it is spent, all of them pause until it renews (00:00 UTC) and the studio says so.
- **Models:** each model has its own chat, showing remaining uses and the daily reset time. Only successful answers count as uses. A paused model says why: limit reached (with the wait), key rejected (the provider key must be renewed) or no longer free; models that cannot work are dimmed. The bot checks each provider once a day and sends a notice when a model stops working, stops being free or comes back.
- **Attachments:** the clip in a model chat attaches one file. Photos are shrunk automatically and go to models that can see (Gemini, Ministral). PDFs are read natively by Gemini and as text by the others. Documents (TXT, Markdown, CSV, JSON, DOCX, XLSX, PPTX, ODT, EPUB, code) work with every model. Audio works with Gemini. Files other than photos must be under 85 KB, because each request travels as a small e-mail.

Conversations are stored only on your device. Answers can be copied with one tap. Pin a provider to make Automatic use it first.

### Coder

Describe what you want, for example "a nice calculator web page", "a counter mini app for Delta Chat" or "a Python script that counts words", and tap **Build**. A plain question ("do you know how to make a mini app?") gets an answer, not a project. Hosted coding models write files and can attempt tests and fixes in up to nine rounds. A request can take several minutes; generated code and results always need review.

- **Sandbox:** when the isolation tools are available and `CODE_RUN` is enabled, commands run in a sandbox without internet or credentials, with time and memory limits. Otherwise the bot writes files without running them. Isolation is not a guarantee that generated code is correct.
- **Result:** the project lists every file with syntax colours. **Try the page** opens web pages inside the app (mini apps run with a simulated `webxdc` bridge), **Export as a mini app** packages web projects as an `.xdc` file named after the project, with a manifest and an icon, that opens directly in Delta Chat, the download button saves the source as a ZIP, and the log shows each step and test.
- **Changes:** write in the box under the project to ask for changes; the current files travel with the request.
- **Limits:** without internet it cannot install packages, so it uses Python, Node and their built-in modules. Choose one exact model in the selector, or leave **Automatic**.

### Images

<p align="center">
  <img src="media/en/images.png" width="230" alt="Image studio">
  <img src="media/en/image-viewer.png" width="230" alt="Image viewer">
</p>

Describe the image and tap **Generate**. Images are kept in the app gallery; open one to try again, copy its description, or export it to the gallery, Files or another chat. In the chat, writing "draw…" works too.

## Web

<p align="center">
  <img src="media/en/clip-web.gif" width="260" alt="Searching and reading a page">
</p>
<p align="center"><sub><a href="media/en/clip-web.mp4">MP4 version</a></sub></p>

<p align="center">
  <img src="media/en/web-results.png" width="230" alt="Search results">
  <img src="media/en/web-reader.png" width="230" alt="Reader view">
</p>

- Results show the title, site and a short description. Tap one to open it in the **reader**.
- The reader shows the page as clean, paginated text. Links open in the app, and **Sections** jumps to a heading.
- From any page you can request a **PDF**, a **Screenshot** or a **Download**, or copy the link.
- Recently visited pages are listed in the Web tab.

The default engine and the reader page size can be changed in Settings.

## Apps

<p align="center">
  <img src="media/en/apps.png" width="230" alt="Apps">
  <img src="media/en/youtube.png" width="230" alt="YouTube">
  <img src="media/en/translator.png" width="230" alt="Translator">
</p>

Each app asks the bot only for what it needs, to save data. Weather uses [Open-Meteo](https://open-meteo.com/); exchange rates and history use the [elTOQUE API](https://tasas.eltoque.com/docs/) under its [terms](https://eltoque.com/api-tasas-de-eltoque-terminos-y-condiciones-de-uso). The translation service receives the submitted text. External services may impose limits or be unavailable.

The translator sends the text to an external provider: the existing Google endpoint, configured LibreTranslate, or Apertium beta as an independent fallback. Do not submit secrets. Apertium is approximate, marks unknown words with `*`, and supports only its available language pairs; if automatic detection is uncertain, choose the source language. The app displays the provider, respects rate limits and does not consume the configured AI model quota. None of the services is guaranteed available or unlimited.

The rates app never uses the public website to work around API errors. A shared monitor refreshes approximately every five minutes while the bot runs (120–3600 seconds), saves received observations and can notify the verified owner about errors. The home banner opens the same rates view and shows real age; failures show saved values with a stale warning. Histories use real queried windows and mark gaps. The API's timezone is undocumented and it can return fewer currencies than the website.

Original provider codes are retained. `ECU` is presented as EUR based on the provider's published euro value, although the alias is not defined in the API contract. BTC and USDT (TRC20) are CUP per USD represented in the cryptocurrency, not per whole coin; TRX is per unit. For represented-USD conversions use `USD_BTC` or `USD_USDT_TRC20`. The converter and illustrative scenario are enabled by operator request and explicitly identified as Michiflin calculations, not elTOQUE quotes or forecasts; permission for derivatives is not confirmed. Review the provider's terms or set `ELTOQUE_DERIVED_CALCULATIONS=0`. Official links can be copied where external navigation is blocked.

For a newly authorized chat message, the bot uses Delta Chat's read-receipt mechanism after the durable checkpoint; the sender's settings and delivery determine whether a receipt appears, and it does not indicate task completion. Rejected messages and old messages seeded when the cursor is first initialized are not intentionally acknowledged. See the [owner-history recovery guide](RECOVER_OWNER_HISTORY.md) for the separate encrypted, key-free archive.

| App | What it does |
|---|---|
| **YouTube** | Search videos, then download video or audio in the chosen quality, or read the transcript |
| **Translator** | Without AI model quota; automatic detection, external services and limits |
| **elTOQUE** | Reference rates and real 7/14/30-day history require an authorized token; conversions and scenarios are labelled Michiflin calculations, with provider permission for derivatives unconfirmed |
| **Weather** | Open-Meteo current conditions, five-day forecast and location selection |
| **News** | Headlines by topic or by search |
| **Utilities** | Unit converter, local time and calculator |
| **File kit** | In Downloads: organizes copies, archives and the existing joiner (see below) |
| **Status** | Server, services and quotas; the short status summarizes the real current state |
| **Notes** | Personal notes; existing notes are not deleted by a navigation change |
| **Administration** | Owner controls and recent diagnostics |

<p align="center">
  <img src="media/en/rates.png" width="230" alt="Exchange rates">
  <img src="media/en/weather.png" width="230" alt="Weather">
  <img src="media/en/news.png" width="230" alt="News">
  <img src="media/en/tools.png" width="230" alt="Utilities">
</p>

## Downloads

<p align="center">
  <img src="media/en/clip-download.gif" width="260" alt="Starting a download">
  <img src="media/en/downloads.png" width="230" alt="Downloads tab">
</p>
<p align="center"><sub><a href="media/en/clip-download.mp4">MP4 version</a> (sped up)</sub></p>

In Downloads choose the download queue or **Saved links**. Paste a link and choose **File**, **Video** or **Audio**, or use a saved link's reader, screenshot, copy and download buttons. Views depend on the site and do not sign in to your account. Files arrive in the chat as messages, not inside the app.

- **Long videos and audio** arrive as segments named like `Movie (1-5).mp4`. Each one plays on its own.
- **Other files** arrive as `.001`, `.002`… parts. The last part carries the SHA-256 fingerprint of the whole file. With the `packing` setting they can be 7z or zip volumes instead.
- **Mini apps** (`.xdc` files) arrive ready to open in the chat, keeping their name.
- The tab shows progress for every job, and its icon carries a badge with the number of downloads still in progress. You can **pause**, **resume**, **cancel**, **retry**, or **resend** a single missing part.
- **Private queue:** production initially allows only the owner. An explicitly authorized temporary guest can submit new jobs; historical jobs from earlier authorizations remain stored but are not resumed or sent.
- Delivery can be throttled: parts per batch, delay between parts, a night window (for example `2200-0600`) and a daily cap in MB.
- Finished jobs are kept for a while (`retention`) so missing parts can be resent.

Reactions on your message show the state: ⏳ while working, then ✅ or ❌.

## File kit and joining parts

Open **Downloads → File kit** for local offline copies or the authorized user's temporary server workspace. The tree supports folder select-all, previewed bulk rename, copy, move, delete, long-name scrolling and undo/redo. The joiner is integrated; raw numbered parts can also be streamed together on the server. ZIP/7z and compatible RAR variants can be opened; the server RAR reader can list and extract selected files in a restricted sandbox. Original phone files are not changed. Resources, variants and temporary storage have real limits; see the [detailed bilingual guide](FILE_KIT.md).

The existing **Join parts** tool is inside the kit and can load selected local copies or files you choose from the device. The screenshots below show an earlier navigation layout.

<p align="center">
  <img src="media/en/clip-join.gif" width="260" alt="Joining parts">
  <img src="media/en/join.png" width="230" alt="Join parts">
</p>
<p align="center"><sub><a href="media/en/clip-join.mp4">MP4 version</a></sub></p>

On supported Android and iOS clients, **Join parts** can join `.001` parts inside the mini app:

1. Tap **Choose parts** and select them all from the chat or from Files. Save parts to **Files**, not Photos: Photos renames them and may re-encode videos.
2. The app puts them in order by itself, using the bot's fingerprint of each part, the number stored inside video and audio segments, or the file name. Each row says how its place was found, and media rows show a thumbnail and the duration. Missing parts are listed. You can still move parts by hand.
3. Tap **Join**. When the corresponding fingerprints are available and the client can calculate them, the app compares the result and reports mismatches. Without the necessary fingerprints it cannot verify the original, and the app says so.
4. **Export** the file to the gallery, Files or another chat.

Media segments play on their own, but joining gives one file: MP4, M4A and MP3 segments are joined without re-encoding (WebM and MKV are not supported). On desktop and Android any archive tool or `cat` works too.

## Settings

<p align="center">
  <img src="media/en/settings.png" width="230" alt="Settings">
</p>

- **App:** language (automatic, Spanish or English), theme (system, light or dark) and default search engine.
- **Safety and email:** Nauta strict limits parts to 10 MB or a smaller verified mailbox limit; the kill switch blocks new work and cooperatively pauses downloads. Opening settings only reads the saved/default state; it does not change either control. Changes are explicit and wait for the bot's confirmed state.
- **Bot settings:** downloads, delivery, web and general options, including **Chat use** (app only, or app and commands). The owner controls the deployed bot. Legacy per-person settings can remain stored without granting access. See the [settings table](../README.md#settings).
- **Device data:** delete AI chats, images, or history and notices. This cannot be undone.

## Status and administration

<p align="center">
  <img src="media/en/admin.png" width="230" alt="Administration">
</p>

**Status** shows uptime, time until the planned restart, free disk, the queue, the state of each service (search, browser, media) and AI quotas. The server restarts by itself every few hours; downloads may continue afterward, while uncertain deliveries pause for review.

**Administration** is available to the owner and any explicitly configured backup administrator. It shows recent foreign contact attempts, bounded session warnings and errors, performance and server controls. In private mode only those configured administrators are active; historical access records remain stored but are hidden. In private or controlled sessions, only attempts from the last 24 hours are shown, without message text. The owner can explicitly approve one named guest and open a temporary controlled session in one confirmed action, or open a session first and then approve individuals. Neither action revives previous grants. Unknown senders receive no reply; the owner may receive an alert. Diagnostics are redacted on a best-effort basis and must be reviewed before sharing; clearing the session buffer does not erase server logs. See [session access control](ACCESS_CONTROL.md).

An optional email-key recovery policy can accept a replacement key for an already permitted address, but it is susceptible to sender impersonation. Encryption in transit does not verify who owns the address. Previously bound chats, queued targets and app-local data are preserved rather than automatically migrated; see [access control](ACCESS_CONTROL.md).

It also includes:

- **Maintenance and notice:** inactive by default; an explicit saved change starts or ends the pause. A duration may be set from 1 minute up to one year, or 0 for manual end. Authorized users are notified, and eligible queued work can resume; already-started requests may still finish.
- **Performance:** CPU use and load, memory (total and the bot's own), disk (free space and the downloads folder), network rate, response times of quick and heavy requests, threads and the queue.
- **Usage:** the private view shows the owner's usage; historical amounts remain stored without granting access. Pausing the queue or resetting usage are explicit owner actions; neither is performed as a historical-data purge automatically.
- **Server:** hosting, system image, processor, session number, start time, time until the planned restart, and whether a newer version is waiting.
- **Stress test:** about 15 seconds of CPU, memory, disk, download speed and messaging-core checks, bounded so it never fills the disk.

### Emergency pause

Use **Settings → Kill switch** or `/kill_switch yes|no`. It remains available even when chat is set to app-only. New requests are blocked; active downloads and packaging stop at checkpoints and are queued again. Switching off lets the queue continue; repeat interrupted AI/agent requests. An already-sent provider request or a part entering Delta Chat handoff cannot be recalled, and a blocked network read may take until its timeout. Uncertain deliveries pause for review; a stale backup or transport interruption means exactly-once delivery cannot be guaranteed, so check the chat before retrying. Pending outbox messages are not automatically deleted.

## Classic chat mode

The bot works through the mini app by default. Setting **Chat use** to *App and commands* (`chat` = `full`) also enables writing normally in the chat ("what is photosynthesis", "weather in Havana", a plain link) and these commands. Each has an English and a Spanish name; tapping one fills the input box.

- **Help and app:** `/help` (`/ayuda`), `/app`
- **Web:** `/web`, `/pdf`, `/screenshot` (`/captura`), `/search` (`/buscar`), `/videos`, `/summary` (`/resumen`)
- **Downloads:** `/video`, `/audio`, `/download` (`/bajar`), `/transcript` (`/subtitulos`), `/queue` (`/cola`), `/pause` (`/pausar`), `/resume` (`/seguir`), `/cancel` (`/cancelar`), `/retry` (`/reintentar`), `/resend` (`/reenviar`), `/schedule` (`/programar`)
- **AI:** `/ai` (`/ia`), `/img`, `/model` (`/modelo`), `/reset` (`/olvidar`)
- **Utilities:** `/translate` (`/traducir`), `/convert` (`/convertir`), `/time` (`/hora`), `/weather` (`/clima`), `/rate` (`/tasa`), `/news` (`/noticias`), `/calc`
- **Account:** `/settings` (`/ajustes`), `/health` (`/salud`), `/status` (`/estado`), `/github`, `/purge` (`/limpiar`), `/admin`

## Saving data

- Use the Agent before the models.
- Lower the video or audio quality in Settings.
- Reduce the reader page size and keep reader images off.
- Schedule a night delivery window and set a daily cap.
- Keep notices on `minimal`.

## Privacy

The production bot starts with owner-only access; the owner can authorize specific temporary guests. AI chats and images may remain on your device and providers receive the requests. Account state and encrypted backups can contain metadata, notes, queued links and small attachment copies; temporary job parts can remain for retries. Treat backups and logs as sensitive.

<p align="center">
  <img src="media/en/desktop-home.png" width="640" alt="Mini app on desktop">
</p>
