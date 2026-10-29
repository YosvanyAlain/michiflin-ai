# Changelog

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
