"""Headless browser smoke tests for the WebXDC app with an in-memory Delta Chat bridge."""
from __future__ import annotations

import copy
import functools
import http.server
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from michiflin import access, admin, chat, config, miniapp, state, tiktok_account

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None


BRIDGE = r"""
(() => {
  const settings = [{ title: "Downloads", shared: false, items: [
    { key: "video_height", label: "Video quality", kind: "choice", value: "360", choices: [
      { v: "360", l: "360p" }, { v: "720", l: "720p" }
    ] }
  ] }];
  const models = { now: Date.now(), reset: Date.now() + 86400000, providers: [], images: [], pin: "" };
  const health = {
    version: "browser-smoke", now: Date.now(), uptime: 60, restart: Date.now() + 86400000,
    active: 0, queue_bytes: 0, disk_free: 10000000000, sent: 0, cap_mb: 0,
    search: { you: false, used: 0, daily: 0 },
    tools: { ffmpeg: true, "7z": true, browser: true, media: true }
  };
  window.__modes = { kill_switch: true, nauta_strict: true };
  window.__links = [];
  window.__failNext = {};
  window.__delay = 0;
  window.__ytConfigured = false;
  window.__ytConnected = false;
  window.__fbConfigured = false;
  window.__fbConnected = false;
  window.__ttConfigured = false;
  window.__ttConnected = false;
  const simulatedQr = "__SIMULATED_QR__";
  const responseFor = (request) => {
    switch (request.op) {
      case "hello": return { version: "browser-smoke", admin: !!window.__fixtureAdmin, models, health, settings, jobs: [] };
      case "kill_switch":
      case "nauta_strict":
        if (typeof request.args.value === "boolean") window.__modes[request.op] = request.args.value;
        return { active: window.__modes[request.op], part_mb: 3 };
      case "estado": return {
        es: window.__modes.kill_switch ? "SIMULADO: Kill switch ACTIVO · cola 0" : "SIMULADO: Sesión OK · cola 0",
        en: window.__modes.kill_switch ? "SIMULATED: Kill switch ON · queue 0" : "SIMULATED: Session OK · queue 0"
      };
      case "admin": return { private_mode: true, users: [
        { address: "owner@example.org", admin: true, active: true },
        { address: "historical@example.org", admin: false, active: false }
      ], attempts: [{ address: "pending@example.org", count: 1, last: Date.now() / 1000, sample: "hello" }],
        blocked: ["historical@example.org"], alerts: true, logs: [], log_entries: [
          { level: "ERROR", last: Date.now() / 1000, count: 3, logger: "michiflin.test", message: "Example failure",
            detail: "Traceback: detailed example" }
        ] };
      case "enlaces":
        if (request.args.do === "add") window.__links.push({ id: window.__links.length + 1, title: request.args.url, url: request.args.url, ts: Date.now() / 1000 });
        if (request.args.do === "delete") window.__links = window.__links.filter(item => item.id !== request.args.id);
        return { links: window.__links, empty: window.__links.length === 0, link: window.__links[0] };
      case "models": return models;
      case "health": return health;
      case "settings": return settings;
      case "rates": return { rates: { USD: 390, EUR: 420, MLC: 250 }, at: Date.now() };
      case "search": return { engine: request.args.engine || "web", results: [] };
      case "translate": return { text: "translated smoke test", detected: "en" };
      case "tool": return { text: "smoke test result" };
      case "ask": return { text: "SIMULATED agent answer [S1]", sig: "Mock model" };
      case "youtube_account":
        if (request.args.do === "start") return { configured: true, connected: false, pending: true, url: "https://www.google.com/device", user_code: "TEST-CODE" };
        if (request.args.do === "poll") { window.__ytConnected = true; return { configured: true, connected: true, pending: false }; }
        if (request.args.do === "disconnect") { window.__ytConnected = false; return { connected: false, revoked: true }; }
        if (request.args.do === "channels" || request.args.do === "subscriptions") return { items: [{ title: "SIMULATED channel", description: "Mock account data" }] };
        return { configured: window.__ytConfigured, connected: window.__ytConnected };
      case "facebook_account":
        if (request.args.do === "start") return { configured: true, connected: false, pending: true, url: "https://www.facebook.com/device", user_code: "FB-TEST-CODE" };
        if (request.args.do === "poll") { window.__fbConnected = true; return { configured: true, connected: true, pending: false, posts_granted: false }; }
        if (request.args.do === "disconnect") { window.__fbConnected = false; return { connected: false, revoked: true }; }
        if (request.args.do === "profile") return { items: [{ title: "SIMULATED Facebook profile", description: "" }] };
        return { configured: window.__fbConfigured, connected: window.__fbConnected };
      case "tiktok_account":
        if (request.args.do === "start") return { configured: true, connected: false, pending: true, qr_png: simulatedQr };
        if (request.args.do === "poll") { window.__ttConnected = true; return { configured: true, connected: true, pending: false, videos_granted: false }; }
        if (request.args.do === "disconnect") { window.__ttConnected = false; return { connected: false, revoked: true }; }
        if (request.args.do === "profile") return { items: [{ title: "SIMULATED TikTok profile", description: "" }] };
        return { configured: window.__ttConfigured, connected: window.__ttConnected };
      case "queue": return { jobs: [] };
      case "action": return { title: "Smoke test", text: "smoke test result" };
      default: return {};
    }
  };
  window.__sentUpdates = [];
  window.__serial = 0;
  window.__listener = null;
  window.webxdc = {
    selfAddr: "owner@example.org",
    selfName: "Browser smoke test",
    sendUpdate(update) {
      window.__sentUpdates.push(update);
      const request = update && update.payload;
      if (request && request.t === "req") {
        setTimeout(async () => {
          let data, error = "";
          const fail = (window.__failNext[request.op] || 0) > 0;
          if (fail) window.__failNext[request.op] -= 1;
          try {
            if (window.__realAccess && ["admin", "owner_only"].includes(request.op)) {
              const result = await fetch("/api/access", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(request) });
              const body = await result.json();
              data = body.data;
              error = body.error || "";
            } else data = responseFor(request);
          } catch (err) { error = String(err); }
          if (window.__listener) window.__listener({
            serial: ++window.__serial,
            payload: { t: "res", re: request.id, ok: !fail && !error, data: (fail || error) ? null : data,
              error: fail ? "SIMULATED network uncertainty" : error }
          });
        }, window.__delay);
      }
    },
    setUpdateListener(listener) {
      window.__listener = listener;
      return Promise.resolve();
    },
    sendToChat: async () => {},
    importFiles: async () => []
  };
})();
"""


@contextmanager
def browser_fixture(language: str, viewport: dict[str, int], real_access: bool = False):
    """Fake Delta bridge; optionally connect access UI to real Python handlers, not a live bot."""
    with tempfile.TemporaryDirectory(prefix="michiflin-browser-fixture-") as temporary:
        root = Path(temporary)
        for name in miniapp.APP_FILES:
            shutil.copy2(miniapp.APP_DIR / name, root / name)
        qr = tiktok_account._qr_image("aweme://authorize?client_key=SIMULATED&client_ticket=SIMULATED")
        (root / "webxdc.js").write_text(BRIDGE.replace("__SIMULATED_QR__", qr), encoding="utf-8")

        class QuietHandler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                pass

            def do_POST(self) -> None:
                if self.path != "/api/access" or not real_access:
                    self.send_error(404)
                    return
                try:
                    request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                    chat.set_sender("owner@example.org")
                    result = miniapp.OPS[request["op"]](7, request.get("args", {}), request.get("lang", "es"))
                    body = {"data": result, "error": ""}
                except Exception as exc:
                    body = {"data": None, "error": str(exc)}
                encoded = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(root)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        errors = []
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(args=["--no-sandbox"])
                page = browser.new_page(viewport=viewport, locale=language)
                page.add_init_script(f"window.__fixtureAdmin = true; window.__realAccess = {str(real_access).lower()};")
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}/index.html", wait_until="domcontentloaded")
                page.wait_for_function("window.__sentUpdates.some(x => x.payload?.op === 'hello')", timeout=10000)
                page.locator(".intro-overlay").wait_for(state="visible", timeout=10000)
                page.locator(".intro-overlay .btn.ghost").first.click()
                page.locator(".intro-overlay").wait_for(state="detached", timeout=5000)
                try:
                    yield page, errors
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def screenshot_if_requested(page, name: str) -> None:
    target = os.environ.get("MICHIFLIN_SCREENSHOT_DIR", "")
    if target:
        directory = Path(target)
        directory.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(directory / f"SIMULADO-{name}.png"), full_page=False)


@unittest.skipUnless(sync_playwright, "playwright is not installed")
class WebXDCBrowserSmoke(unittest.TestCase):
    def test_new_agent_and_account_views_are_honest_and_bilingual(self) -> None:
        for language in ("es", "en"):
            viewport = {"width": 390, "height": 844} if language == "es" else {"width": 1280, "height": 900}
            with self.subTest(language=language), browser_fixture(language, viewport) as (page, errors):
                page.locator('.tab[data-tab="ai"]:visible').click()
                page.get_by_text("Agente" if language == "es" else "Agent", exact=True).first.click()
                self.assertTrue(page.get_by_role("tab", name="Sin cuota de modelos" if language == "es" else "No model quota").is_visible())
                page.locator("#view textarea").fill("free-mode question")
                page.locator("#view .composer button").click()
                page.get_by_text("SIMULATED agent answer [S1]").wait_for(state="visible")
                sent = page.evaluate("window.__sentUpdates.filter(x => x.payload?.op === 'ask').at(-1).payload.args.space")
                self.assertEqual(sent, "agent")
                page.get_by_role("tab", name="Con IA / API" if language == "es" else "With AI / API").click()
                page.locator("#view .intro").wait_for(state="visible")
                self.assertIn("no inicia" if language == "es" else "cannot log in", page.locator("#view .intro").inner_text().lower())
                self.assertEqual(page.get_by_text("free-mode question").count(), 0)
                screenshot_if_requested(page, language + "-new-agent")
                page.locator("#view textarea").fill("Weather in Havana")
                page.locator("#view .composer button").click()
                page.get_by_text("SIMULATED agent answer [S1]").wait_for(state="visible")
                sent = page.evaluate("window.__sentUpdates.filter(x => x.payload?.op === 'ask').at(-1).payload.args.space")
                self.assertEqual(sent, "smart_agent")
                page.get_by_role("tab", name="Sin cuota de modelos" if language == "es" else "No model quota").click()
                self.assertTrue(page.get_by_text("free-mode question").is_visible())
                self.assertEqual(page.get_by_text("Weather in Havana").count(), 0)

                page.locator('.tab[data-tab="web"]:visible').click()
                page.get_by_role("button", name="Cuentas y sitios" if language == "es" else "Accounts and sites").click()
                for site in ("Instagram", "Facebook", "TikTok", "DeepSeek Chat", "Arena.ai", "Revolico", "Z.AI"):
                    self.assertTrue(page.get_by_text(site, exact=True).is_visible())
                self.assertIn("inicia sesión tú mismo" if language == "es" else "sign in yourself", page.locator("#view").inner_text().lower())
                screenshot_if_requested(page, language + "-accounts-browser")
                page.get_by_role("button", name="Instagram", exact=False).first.click()
                self.assertIn("canal cifrado" if language == "es" else "encrypted channel", page.locator("#view").inner_text().lower())
                self.assertTrue(page.get_by_role("button", name="Iniciar navegador" if language == "es" else "Start browser").is_visible())
                screenshot_if_requested(page, language + "-browser-before-start")
                page.locator("#back").click()
                page.get_by_role("button", name="Mi cuenta de YouTube" if language == "es" else "My YouTube account", exact=False).first.click()
                page.get_by_text("El servidor aún no tiene" if language == "es" else "The server has not configured", exact=False).wait_for(state="visible")
                self.assertEqual(page.get_by_role("button", name="Conectar con Google" if language == "es" else "Connect with Google").count(), 0)
                screenshot_if_requested(page, language + "-youtube-not-configured")
                page.evaluate("window.__ytConfigured = true; go('youtube_account')")
                page.get_by_role("button", name="Conectar con Google" if language == "es" else "Connect with Google").click()
                page.get_by_text("TEST-CODE").wait_for(state="visible")
                self.assertTrue(page.locator("#view .site-address").evaluate("e => {const a=e.getBoundingClientRect(), b=e.nextElementSibling.getBoundingClientRect(); return a.right <= b.left || a.bottom <= b.top}"))
                screenshot_if_requested(page, language + "-youtube-device-SIMULATED")
                page.get_by_role("button", name="Ya autoricé" if language == "es" else "I have authorized").click()
                page.get_by_role("button", name="Mi canal" if language == "es" else "My channel").click()
                page.get_by_text("SIMULATED channel").wait_for(state="visible")
                self.assertEqual(errors, [])

    def test_official_social_views_with_simulated_bridge(self) -> None:
        cases = [("es", {"width": 390, "height": 844}), ("en", {"width": 1280, "height": 800})]
        for language, viewport in cases:
            with self.subTest(language=language), browser_fixture(language, viewport) as (page, errors):
                page.evaluate("go('accounts')")
                page.get_by_role("button", name="Mi cuenta de Facebook" if language == "es" else "My Facebook account", exact=False).click()
                page.get_by_text("El servidor aún no tiene" if language == "es" else "The server does not yet have", exact=False).wait_for(state="visible")
                self.assertEqual(page.get_by_role("button", name="Conectar con Facebook" if language == "es" else "Connect with Facebook").count(), 0)
                screenshot_if_requested(page, language + "-facebook-not-configured")
                page.evaluate("window.__fbConfigured = true; go('facebook_account')")
                page.get_by_role("button", name="Conectar con Facebook" if language == "es" else "Connect with Facebook").click()
                page.get_by_text("FB-TEST-CODE").wait_for(state="visible")
                screenshot_if_requested(page, language + "-facebook-device-SIMULATED")
                page.get_by_role("button", name="Ya autoricé" if language == "es" else "I have authorized").click()
                page.get_by_role("button", name="Mi perfil" if language == "es" else "My profile").click()
                page.get_by_text("SIMULATED Facebook profile").wait_for(state="visible")

                page.evaluate("go('tiktok_account')")
                page.get_by_text("El servidor aún no tiene" if language == "es" else "The server does not yet have", exact=False).wait_for(state="visible")
                screenshot_if_requested(page, language + "-tiktok-not-configured")
                page.evaluate("window.__ttConfigured = true; go('tiktok_account')")
                page.get_by_role("button", name="Conectar con TikTok" if language == "es" else "Connect with TikTok").click()
                qr = page.locator("#view img.social-qr")
                qr.wait_for(state="visible")
                self.assertTrue(qr.evaluate("e => e.complete && e.naturalWidth > 0 && e.src.startsWith('data:image/png;base64,')"))
                screenshot_if_requested(page, language + "-tiktok-qr-SIMULATED")
                page.get_by_role("button", name="Ya autoricé" if language == "es" else "I have authorized").click()
                page.get_by_role("button", name="Mi perfil" if language == "es" else "My profile").click()
                page.get_by_text("SIMULATED TikTok profile").wait_for(state="visible")
                self.assertEqual(errors, [])

    def test_bilingual_mode_status_chat_and_private_admin(self) -> None:
        cases = [("es", {"width": 390, "height": 844}), ("en", {"width": 1280, "height": 800})]
        for language, viewport in cases:
            with self.subTest(language=language), browser_fixture(language, viewport) as (page, errors):
                page.evaluate("window.__delay = 350")
                self.assertEqual(page.locator("#view .tile-health").count(), 0, "status has one entry in Apps, not another home shortcut")
                page.locator('.tab[data-tab="apps"]:visible').click()
                self.assertEqual(page.locator("#view .tile-health").count(), 1)
                self.assertEqual(page.locator("#view .tile-status, #view .tile-killswitch, #view .tile-nauta, #view .tile-links").count(), 0)
                self.assertEqual(page.locator("#view .tile-notes").count(), 1)
                colors = page.locator("#view .tile-health .tile-icon, #view .tile-notes .tile-icon").evaluate_all("nodes => nodes.map(n => getComputedStyle(n).color)")
                self.assertNotEqual(colors[0], colors[1])

                if page.locator("#gear").is_visible():
                    page.locator("#gear").click()
                else:
                    page.locator('#rail [data-tab="settings"]').click()
                self.assertEqual(page.locator('#view button[aria-pressed]').count(), 0, "must not guess while loading")
                self.assertEqual(page.get_by_role("button", name="Estado" if language == "es" else "Status", exact=True).count(), 0)
                toggle = page.locator('#view .mode-killswitch button[aria-pressed]')
                toggle.wait_for(state="visible")
                self.assertEqual(toggle.get_attribute("aria-pressed"), "true")
                self.assertIn("ACTIVADO" if language == "es" else "ON", page.locator('#view .mode-killswitch [role="status"]').inner_text())
                self.assertIn("3 MB", page.locator("#view .mode-nauta").inner_text())
                screenshot_if_requested(page, language + "-settings-modes")
                toggle.click()
                self.assertTrue(toggle.is_disabled(), "prevent double clicks while waiting for the backend")
                page.wait_for_function("document.querySelector('#view .mode-killswitch button[aria-pressed]')?.getAttribute('aria-pressed') === 'false'")

                # The remote side may apply the change even when its response is lost.
                page.evaluate("window.__failNext.kill_switch = 1")
                page.locator('#view .mode-killswitch button[aria-pressed]').click()
                page.locator('#view .mode-grid .failure').wait_for(state="visible")
                self.assertEqual(page.locator('#view .mode-killswitch button[aria-pressed]').count(), 0, "unknown state must not be rendered as OFF")
                page.locator('#view .mode-grid .failure button').click()
                page.wait_for_function("document.querySelector('#view .mode-killswitch button[aria-pressed]')?.getAttribute('aria-pressed') === 'true'")

                page.locator('.tab[data-tab="apps"]:visible').click()
                page.locator(".tile-health").click()
                page.locator('#view .status-brief [role="status"]').wait_for(state="visible")
                self.assertIn("SIMULADO" if language == "es" else "SIMULATED", page.locator('#view .status-brief [role="status"]').inner_text())
                screenshot_if_requested(page, language + "-estado-unificado")

                page.locator('.tab[data-tab="downloads"]:visible').click()
                page.locator('#view .segmented [data-key="links"]').click()
                page.locator('#view input[type="url"]').fill("https://example.org/saved")
                page.locator('#view .search-field button').click()
                page.locator(".saved-link .saved-url").wait_for(state="visible")
                self.assertIn("https://example.org/saved", page.locator(".saved-link .saved-url").inner_text())
                self.assertTrue(page.locator(".saved-link button").count() >= 4)
                screenshot_if_requested(page, language + "-links-downloads")
                page.locator('#view .segmented [data-key="downloads"]').click()
                self.assertEqual(page.locator('#view .segmented [data-key="downloads"]').get_attribute("aria-selected"), "true")
                listeners_before = page.evaluate("bus.local.filter(([kind]) => kind === 'jobs').length")
                for _ in range(5):
                    page.locator('#view .segmented [data-key="links"]').click()
                    page.locator('#view .segmented [data-key="downloads"]').click()
                self.assertEqual(page.evaluate("bus.local.filter(([kind]) => kind === 'jobs').length"), listeners_before)

                page.locator('.tab[data-tab="ai"]:visible').click()
                page.locator('#view .space-card').nth(1).click()
                page.locator('#view .intro').wait_for(state="visible")
                self.assertNotEqual(page.locator('#view .intro').evaluate("e => getComputedStyle(e).position"), "fixed")
                area = page.locator('#view textarea')
                area.fill("15% de 2400")
                self.assertEqual(area.input_value(), "15% de 2400")
                self.assertTrue(area.evaluate("e => {const r=e.getBoundingClientRect(); return document.elementFromPoint(r.left+r.width/2,r.top+r.height/2) === e}") )
                screenshot_if_requested(page, language + "-chat-input")
                page.evaluate("onboarding(true)")
                page.locator(".intro-overlay .btn.ghost").first.click()
                page.locator(".intro-overlay").wait_for(state="detached")
                self.assertEqual(page.locator('#view .intro').count(), 1, "closing tour must not remove chat introduction")

                if page.locator("#gear").is_visible():
                    page.locator("#gear").click()
                else:
                    page.locator('#rail [data-tab="settings"]').click()
                label = "Administración" if language == "es" else "Administration"
                page.get_by_role("button", name=label, exact=False).first.click()
                page.get_by_text("pending@example.org").wait_for(state="visible")
                self.assertEqual(page.get_by_text("historical@example.org").count(), 0)
                self.assertEqual(page.locator(".add-row").count(), 0)
                disabled = page.locator('#view button[disabled][aria-label]').filter(has_text="Autorizar" if language == "es" else "Authorize")
                self.assertEqual(disabled.count(), 1)
                page.get_by_text("Example failure", exact=True).click()
                self.assertIn("Traceback: detailed example", page.locator("#view .log-entry[open]").inner_text())
                self.assertIn("×3", page.locator("#view .log-entry").inner_text())
                screenshot_if_requested(page, language + "-admin-private")
                self.assertEqual(errors, [])

    def test_owner_access_real_handlers_in_browser_with_fake_delta_bridge(self) -> None:
        saved = copy.deepcopy(state.data)
        try:
            with (mock.patch.object(config, "OWNER_ADDRESS", "owner@example.org"),
                  mock.patch.object(config, "OWNER_ONLY", True),
                  mock.patch.object(config, "MAINTENANCE_MODE", False),
                  mock.patch.object(state, "save"),
                  mock.patch.object(admin, "_set_blocked_contact", return_value=True),
                  mock.patch.object(miniapp.perf, "snapshot", return_value=None),
                  mock.patch.object(miniapp.usage, "report", return_value=None)):
                access.reset()
                state.data["access"] = {"allowed": ["historical@example.org"], "blocked": [], "attempts": {}, "seen": {}}
                for language in ("es", "en"):
                    access.reset()
                    viewport = {"width": 390, "height": 844} if language == "es" else {"width": 1280, "height": 900}
                    with self.subTest(language=language), browser_fixture(language, viewport, real_access=True) as (page, errors):
                        if page.locator("#gear").is_visible():
                            page.locator("#gear").click()
                        else:
                            page.locator('#rail [data-tab="settings"]').click()
                        label = "Administración" if language == "es" else "Administration"
                        page.get_by_role("button", name=label, exact=False).first.click()
                        card = page.locator(".owner-access")
                        card.wait_for(state="visible")
                        self.assertIn("1 ·", card.inner_text())
                        self.assertFalse(admin.is_allowed("historical@example.org"))
                        screenshot_if_requested(page, language + "-access-closed-real-backend")
                        page.evaluate("window.__failNext.owner_only = 1")  # request applies, but its reply is lost
                        card.locator("button").click()
                        page.locator("#sheet .btn.danger").click()
                        page.locator("#view .failure").wait_for(state="visible")
                        self.assertEqual(page.locator(".owner-access").count(), 0, "do not present a guessed mode")
                        self.assertFalse(access.snapshot()["owner_only"])
                        page.locator("#view .failure button").click()
                        page.wait_for_function("document.querySelector('.owner-access b')?.textContent.startsWith('0')")
                        self.assertFalse(admin.is_allowed("historical@example.org"))
                        self.assertEqual(page.get_by_text("historical@example.org").count(), 0)
                        screenshot_if_requested(page, language + "-access-open-real-backend")
                        page.locator('.add-row input[type="email"]').fill("guest@example.org")
                        page.locator('.add-row button').click()
                        page.locator("#sheet .btn.danger").click()
                        page.locator("#sheet").wait_for(state="hidden")
                        page.get_by_text("guest@example.org", exact=True).wait_for(state="visible")
                        self.assertTrue(admin.is_allowed("guest@example.org"))
                        screenshot_if_requested(page, language + "-access-guest-real-backend")
                        # Accelerate only the test's deadline, then let the real UI timer refresh from the real handler.
                        with access._lock:
                            access._deadline = time.time() + 1.2
                        page.evaluate("async () => { const data = await bridge.call('owner_only', {}); drawAdmin(document.querySelector('#view > .stack'), data); }")
                        page.wait_for_function("document.querySelector('.owner-access b')?.textContent.startsWith('1')", timeout=8000)
                        self.assertFalse(admin.is_allowed("guest@example.org"))
                        self.assertIn("historical@example.org", state.data["access"]["allowed"])
                        self.assertEqual(errors, [])
                        screenshot_if_requested(page, language + "-access-expired-real-backend")
        finally:
            access.reset()
            state.data.clear()
            state.data.update(saved)

    def test_unavailable_coder_selection_requires_an_explicit_new_choice(self) -> None:
        for language, viewport in (("es", {"width": 390, "height": 844}), ("en", {"width": 1280, "height": 800})):
            with self.subTest(language=language), browser_fixture(language, viewport) as (page, errors):
                self.assertEqual(page.locator("html").get_attribute("lang"), language)
                page.evaluate("prefs.coderModel = 'retired:model'")
                page.locator('.tab[data-tab="ai"]:visible').click()
                page.get_by_role("button", name="Programador" if language == "es" else "Coder", exact=False).first.click()
                picker = page.locator("#view select")
                self.assertEqual(picker.input_value(), "retired:model")
                self.assertIn("no disponible" if language == "es" else "unavailable", picker.inner_text().lower())
                screenshot_if_requested(page, language + "-coder-unavailable")
                before = page.evaluate("window.__sentUpdates.filter(x => x.payload?.op === 'code').length")
                self.assertFalse(page.evaluate("coderSend('Make a page')"))
                after = page.evaluate("window.__sentUpdates.filter(x => x.payload?.op === 'code').length")
                self.assertEqual(before, after)
                picker.select_option("")
                self.assertEqual(picker.input_value(), "")
                screenshot_if_requested(page, language + "-coder-automatic")
                self.assertEqual(errors, [])

    def test_oversized_coder_project_is_kept_and_never_sent_as_empty(self) -> None:
        for language in ("es", "en"):
            with self.subTest(language=language), browser_fixture(language, {"width": 390, "height": 844}) as (page, errors):
                page.evaluate("""() => {
                  const project = coder.get();
                  project.files = [{path:'important.txt', text:'x'.repeat(95000)}];
                  coder.set(project);
                }""")
                before = page.evaluate("window.__sentUpdates.filter(x => x.payload?.op === 'code').length")
                self.assertFalse(page.evaluate("coderSend('continue')"))
                self.assertEqual(before, page.evaluate("window.__sentUpdates.filter(x => x.payload?.op === 'code').length"))
                self.assertEqual(page.evaluate("coder.get().files[0].text.length"), 95000)
                self.assertEqual(page.evaluate("coder.get().files[0].path"), "important.txt")
                self.assertEqual(errors, [])

    def test_tabs_and_all_app_tiles_render_without_browser_errors(self) -> None:
        with tempfile.TemporaryDirectory(prefix="michiflin-webxdc-smoke-") as temporary:
            root = Path(temporary)
            for name in miniapp.APP_FILES:
                shutil.copy2(miniapp.APP_DIR / name, root / name)
            (root / "webxdc.js").write_text(BRIDGE, encoding="utf-8")

            class QuietHandler(http.server.SimpleHTTPRequestHandler):
                def log_message(self, _format: str, *_args: object) -> None:
                    pass

            handler = functools.partial(QuietHandler, directory=str(root))
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            errors: list[str] = []
            try:
                with sync_playwright() as playwright:
                    if not Path(playwright.chromium.executable_path).exists():
                        self.skipTest("Playwright Chromium is not installed")
                    browser = playwright.chromium.launch(args=["--no-sandbox"])
                    page = browser.new_page(viewport={"width": 390, "height": 844})
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(f"http://127.0.0.1:{server.server_port}/index.html", wait_until="domcontentloaded")
                    page.wait_for_function("document.querySelector('#title')?.textContent.length > 0", timeout=15000)
                    page.wait_for_function("window.__sentUpdates.some(x => x.payload?.op === 'hello')", timeout=10000)
                    page.locator(".intro-overlay").wait_for(state="visible", timeout=10000)
                    page.locator(".intro-overlay .btn.ghost").first.click()
                    page.locator(".intro-overlay").wait_for(state="detached", timeout=5000)

                    for tab in ("ai", "web", "apps", "downloads"):
                        page.locator(f'#tabs [data-tab="{tab}"]').click()
                        page.wait_for_timeout(60)
                        self.assertTrue(page.locator("#view").inner_text().strip(), tab)

                    page.locator("#gear").click()
                    page.wait_for_timeout(80)
                    self.assertTrue(page.locator("#view").inner_text().strip(), "settings")
                    page.locator('#tabs [data-tab="apps"]').click()
                    tiles = page.locator("#view .tiles .tile")
                    count = tiles.count()
                    self.assertGreaterEqual(count, 9)
                    for index in range(count):
                        tile = page.locator("#view .tiles .tile").nth(index)
                        label = tile.inner_text()
                        tile.click()
                        page.wait_for_timeout(80)
                        self.assertTrue(page.locator("#title").inner_text().strip(), label)
                        self.assertTrue(page.locator("#view").inner_text().strip(), label)
                        page.locator("#back").evaluate("(element) => element.click()")
                        page.wait_for_timeout(100)
                        self.assertGreater(
                            page.locator("#view .tiles .tile").count(),
                            0,
                            f"Back did not return to app tiles after opening {label!r}; title={page.locator('#title').inner_text()!r}",
                        )
                    browser.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
