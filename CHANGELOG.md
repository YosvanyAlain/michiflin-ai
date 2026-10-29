# Changelog

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
