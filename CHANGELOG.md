# Changelog

## 5.5.0

- Integrate the existing parts joiner directly inside the file kit, retaining raw and compatible media joining, ordering, gap checks and available verification. Add streaming remote `.001` joins with optional encrypted SHA-256 verification; media remuxing remains on-device.
- Add folder-scoped select-all, long-name scrolling and previewed, atomic bulk renaming with extension-aware modes and undo/redo.
- Add selective server-side RAR inspection and extraction in a restricted, networkless native worker. Archive bytes and selected output are streamed, passwords and member selections are sealed, and unsafe paths or links are rejected. Include the separately licensed official UnRAR 7.30 beta 2 source; compatibility, isolation and resources are not guaranteed.
- Cover encrypted RAR4/RAR5, solid and multivolume samples, untrusted paths, unavailable isolation, low resources, real encrypted bridge and synthetic Chromium/WebKit UI. A separate local 600 MiB streaming check does not prove all large archives or physical devices work.
- The server workspace and RAR scratch files remain temporary, not a backup. Existing identity/access settings, previous miniapps, jobs and original phone files are not migrated or replaced by this update.

## 5.4.0

- Add an ES/EN file kit under Downloads, with a scrollable tree, offline copies, a temporary authenticated server workspace, folder organization, text editing, preview, undo/redo and explicit history cleanup. The existing parts joiner is nested under the kit and can open selected local copies.
- Add ZIP/7z creation and extraction, optional passwords and volumes, and RAR extraction using separately licensed 7-Zip 26.05 WebAssembly. Local operations have an adjustable memory budget. Remote extraction requires a short-lived, networkless Linux sandbox and fails closed if isolation is unavailable. Supported variants and practical sizes depend on the client and memory.
- Keep originals untouched. Server imports use one-use, session- and sender-bound attachment drafts and verify the chosen file's size and SHA-256. Finished downloads are copied only when integrity can be verified; playable media segments and archive volumes are checked against their individual hashes. File-kit exports use the existing queue and mailbox ceilings, without remuxing the file.
- Protect archive passwords, remote text edits and previews with ephemeral encrypted envelopes. File names and operation metadata remain in the encrypted chat updates. Local storage is per miniapp instance and can be reclaimed; the server workspace is temporary and is not part of the state backup.
- Add synthetic adversarial, process-locking, Chromium, WebKit, bridge and isolated-worker checks to CI. Browser-engine tests are not a substitute for testing WebXDC on physical Android and iOS devices.
- Restore a no-AI-quota translation path when the unofficial Google endpoint is unavailable or rate-limited: use the configured LibreTranslate service, then independently operated Apertium beta where the language pair is available. Apertium is rule-based, approximate and may mark unknown words with `*`; uncertain language detection requires an explicit source. Respect provider `Retry-After`, show which service was used, prevent duplicate clicks, and never return a partially translated text. All services receive submitted text and can impose limits.

## 5.3.1

- Coordinate automatic welcome, chat fallback and upgrade deliveries so overlapping paths do not create duplicate app instances. A failed message lookup is not proof that existing data disappeared; `/app` still explicitly requests a new copy. Previously sent apps are never deleted.
- Remember the language observed in an authorized app request for later notices. The app's own automatic or manually selected language remains local to that instance. Delivery after a crash or stale backup cannot be guaranteed exactly once.
- Add synthetic concurrency, transient-lookup, identity-isolation and language regressions to CI. This server-only fix does not change the WebXDC package hash.

## 5.3.0

- Preserve all elTOQUE codes while presenting ECU as EUR, the confirmed USDT TRC20 and BTC USD-denominated rates, and TRX per unit. Unify the compact home banner and rates view. Enable clearly labelled local conversions and scenarios at the operator’s request; permission for derivative uses is unconfirmed and they can be disabled. Original provider data remain unchanged.
- Mark eligible new authorized messages read using Delta Chat’s real MDN mechanism after a durable checkpoint; rejected messages and old messages seeded at cursor initialization remain silent. Receipt delivery depends on both clients and the network and does not prove task completion.
- Add guarded profile carryover and an independently recoverable, encrypted owner-history export which excludes account databases and old identity keys. Preserve distinct owner records and available attachments across reviewed old snapshots. The export does not restore the old account or WebXDC local storage; absent files remain absent.
- Update Spanish/English help and recovery documentation.

## 5.2.0

- Hardened the authorized elTOQUE API integration: preserve provider codes, display confirmed cryptocurrency denominations without guessing suffixed fields, stop on rate limits, persist received observations and windows, and show only real partial history. A shared monitor and home banner show honest freshness; verified-owner anomaly notices are coalesced. Derived calculations are off by default until provider approval. Website scraping is no longer a fallback; a valid application token is required for current rates and history. The optional 30-day view is on demand. Enable derived calculations only with explicit provider approval.
- Added a guarded, one-time cutover for a separately created and owner-verified identity. It retains and independently restores a full encrypted legacy snapshot before replacement, quarantines old bindings, verifies the new snapshot, and seals against silent fallback to the old account. The fresh identity pins the verified owner key and uses a new strict epoch. Old queued jobs remain stored but do not resume against prior chat IDs.
- Added a single-confirmation temporary named-guest flow, without reactivating historic grants.
- Rechecked epoch, access and verified contact at each WebXDC fragment and outgoing handoff in strict mode, and fixed long chat replies that previously stopped after their first part. Revocation cannot recall a message already submitted to the mail core.
- Added API, mobile, disk-restart, offline project recovery, queue-target and identity-binding isolation regressions; tests against synthetic accounts are not proof of a production migration.

## 5.1.0

- Unified the agent into **No model quota** and **With AI / API** modes. Automatic routing is in the latter; the previous automatic conversation remains accessible without being erased. The free mode recognizes capability questions and selected follow-ups. The AI mode supports an explicit model and compatible attachments, with bounded public tools when no attachment is sent.
- Fixed narrow-phone agent navigation and kept the mode switch visible while scrolling. Text models are grouped by provider and category; image models by provider and recognizable family in expandable cards; web search modes have distinct icons; long help is contextual.
- Improved no-AI-quota translation with source detection, UTF-8-safe segments and explicit size/failure handling. The primary external service and an optional configured LibreTranslate endpoint have independent availability and limits; untrusted public translation-memory matches are not used.
- Added Open-Meteo current conditions, location selection and a five-day forecast, plus responsive weather icons and reduced-motion support.
- Added elTOQUE history and charting through its authorized application API. Only received daily data are plotted; gaps are flagged. A simple three-day linear scenario is separately labelled as an estimate, **not** an elTOQUE forecast or financial advice. Without `ELTOQUE_API_TOKEN`, history cannot be fetched through the official API.
- Settings read the saved/default Kill switch and Nauta Strict states without modifying them. The maintenance notice starts disabled; the owner may explicitly set a duration up to one year or use manual end. Existing permissions and individual pauses are not changed.
- Added an information and collaboration section without inventing payment details. Updated the owner's PDF and examples.
- Added a one-time, guarded relay migration procedure: it checks encrypted recovery, existing history and attachment blobs, retains the old transport, verifies the primary OpenPGP fingerprint and encrypted messaging in both directions with a synthetic contact, and refuses repeated registration after interruption. A successful synthetic rehearsal is not proof that a production account has migrated; inspect the production verification step. Older WebXDC instances are retained, and their local storage is not automatically migrated.

External providers may change or fail. Maintenance is cooperative, and already-submitted provider requests or deliveries may finish.
