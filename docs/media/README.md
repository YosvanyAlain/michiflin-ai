# Current-interface captures / Capturas de la interfaz actual

These screenshots and tours are generated from the current `webxdc/` renderer, **not a mock drawing of the interface**. They use the repository's offline test bridge, with a visible **DEMO · synthetic data / datos simulados** label. All addresses, queues, results and administrator examples are synthetic. No mailbox, provider key or real login is used.

Son capturas del renderizador real con un puente de pruebas sin conexión. **No demuestran** tiempos del correo, descargas completas de fuentes externas, disponibilidad de un modelo ni funcionamiento en un teléfono físico. Los clips son secuencias de fotogramas sostenidos durante dos segundos: no son una medida de velocidad ni una sesión continua por correo.

## Regenerate / Regenerar

From the repository root, with development dependencies and `ffmpeg` installed:

```bash
pip install -r requirements.txt pytest
python -m playwright install --with-deps chromium
python tools/record_current_tours.py
```

The tool uses only the local test fixture. Do not replace it with a private production account to create public documentation. `capture-manifest.json` records the renderer source fingerprint and capture method.

## Tours in Spanish and English

- `es/clip-tour.gif`, `en/clip-tour.gif` and their MP4 versions: the greeting on Home, the main tabs and Settings, an example unread bell and its notification center, and the optional five-step guide opened from Help. Startup has a brief local, theme-aware mark; it does **not** open a welcome card or guide automatically, and reduced-motion preferences disable its motion.
- `clip-web`: search and reader using a reserved example URL and a synthetic result.
- `clip-download`: illustrative queued/completed states. This does not depict a real external download.
- `clip-join`: the **current integrated file kit and join panel**, not the retired standalone joiner. It shows where to work, not a verified media-remux result.

Mobile captures use a 390×844 viewport; desktop captures use 1440×900. A 24-pixel caption strip is added below the captured viewport so the synthetic-data label never covers the interface. These are Chromium browser viewports. Android-style layout/failure tests, WebKit coverage and the separately installed old Chromium engine are described in [Android startup scope](../ANDROID_STARTUP.md). No screenshots or clips certify every device.

## Privacy and accuracy

Never publish invitation links, tokens, private keys, personal chat histories, real provider requests or user workspaces in these assets. Administration captures are explicitly synthetic and do not imply normal users can access administration. When interface or behavior changes, regenerate rather than editing pictures to promise unavailable functions.
