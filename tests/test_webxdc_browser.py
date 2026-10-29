"""Headless browser smoke tests for the WebXDC app with an in-memory Delta Chat bridge."""
from __future__ import annotations

import base64
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

from michiflin import access, admin, chat, config, miniapp, queue, state

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
  window.__notes = [];
  window.__failNext = {};
  window.__delay = 0;
  window.__fixtureSession = "synthetic-boot-session";
  window.__sessionChangedNext = "";
  const responseFor = (request) => {
    switch (request.op) {
      case "hello": return { version: "browser-smoke", session: window.__fixtureSession, admin: !!window.__fixtureAdmin, models, health, settings, jobs: [],
        modes: { kill_switch: {active:window.__modes.kill_switch}, nauta_strict: {active:window.__modes.nauta_strict, part_mb:3} },
        maintenance_notice: {active:false, es:"Mensaje de mantenimiento", en:"Maintenance message", revision:0, expires:0} };
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
      case "notas":
        if (request.args.do === "add") window.__notes.push({ id: window.__notes.length + 1, text: request.args.text, ts: Date.now() / 1000 });
        if (request.args.do === "delete") window.__notes = window.__notes.filter(item => item.id !== request.args.id);
        return { notes: window.__notes };
      case "enlaces":
        if (request.args.do === "add") window.__links.push({ id: window.__links.length + 1, title: request.args.url, url: request.args.url, ts: Date.now() / 1000 });
        if (request.args.do === "delete") window.__links = window.__links.filter(item => item.id !== request.args.id);
        return { links: window.__links, empty: window.__links.length === 0, link: window.__links[0] };
      case "models": return models;
      case "health": return health;
      case "settings": return settings;
      case "rates":
        if (request.args.history) return { history: Array.from({length:request.args.days}, (_,i)=>({
          date: new Date(Date.UTC(2026,9,1+i)).toISOString().slice(0,10), rates:{USD:390+i,EUR:420+i*2} })), complete:true, days:request.args.days, source:"elTOQUE API", derived_allowed:false };
        return { rates: { USD: 390, EUR: 420, MLC: 250 }, at: Date.now() / 1000, source:"elTOQUE API", refresh_seconds:300, derived_allowed:false };
      case "weather": return {query: request.args.city, place:{id:1,label:"Havana, Cuba"}, candidates:[{id:1,label:"Havana, Cuba"},{id:2,label:"Havana, USA"}],
        timezone:"America/Havana",as_of:"2026-10-29T10:00",current:{temp:28,feels:30,humidity:72,wind:13,precipitation:0,code:0,is_day:1,description:"sunny"},
        days:[{date:"2026-10-29",code:0,description:"clear",high:30,low:23,rain:10},{date:"2026-10-30",code:63,description:"rain",high:29,low:24,rain:65}],source:"Open-Meteo"};
      case "search": return { engine: request.args.engine || "web", results: window.__searchResults || [] };
      case "youtube_search": {
        const rows = window.__searchResults || [];
        const start = ((request.args.page || 1) - 1) * 15;
        return { results: rows.slice(start, start + 15), more: rows.length > start + 15, limited: false };
      }
      case "maintenance_notice": return { notice: { active: false, es: "Mensaje de mantenimiento", en: "Maintenance message" }, profile_updated: true };
      case "personal_ai": return { providers: [] };
      case "music_search": return { results: [{ id: "album-1", title: "Licensed jazz", creator: "Artist" }] };
      case "music_item": return { id: "album-1", title: "Licensed jazz", creator: "Artist", license: "CC BY https://creativecommons.org/publicdomain/mark/1.0/",
        page: "https://archive.org/details/album-1", files: [{ title: "Jazz track", format: "VBR MP3", size: 12345,
          url: "https://archive.org/download/album-1/track.mp3" }] };
      case "image": return { prompt: request.args.prompt, source: "OVH test", model: "ovh:sdxl", mime: "image/png",
        b64: "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=" };
      case "translate": return { text: "translated smoke test", detected: "en" };
      case "tool": return { text: "smoke test result" };
      case "ask": return { text: "SIMULATED agent answer [S1]", sig: "Mock model" };
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
          let data, error = "", code = "";
          const fail = (window.__failNext[request.op] || 0) > 0;
          if (fail) window.__failNext[request.op] -= 1;
          try {
            if (window.__sessionChangedNext === request.op) {
              window.__sessionChangedNext = "";
              error = request.lang === "en" ? "The bot restarted; check provider charges before retrying." : "El bot se reinició; revisa los cargos antes de repetir.";
              code = "session_changed";
            } else if ((window.__realAccess && ["admin", "owner_only"].includes(request.op)) ||
                (window.__realPersonal && ["personal_ai", "models", "ask"].includes(request.op)) ||
                (window.__realFilekit && request.op === "filekit")) {
              const endpoint = request.op === "filekit" ? "/api/filekit" :
                window.__realPersonal && ["personal_ai", "models", "ask"].includes(request.op) ? "/api/personal" : "/api/access";
              const result = await fetch(endpoint,
                { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(request) });
              const body = await result.json();
              data = body.data;
              error = body.error || "";
            } else data = responseFor(request);
          } catch (err) { error = String(err); }
          if (window.__listener) window.__listener({
            serial: ++window.__serial,
            payload: { t: "res", re: request.id, ok: !fail && !error, data: (fail || error) ? null : data,
              error: fail ? "SIMULATED network uncertainty" : error, code }
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
def browser_fixture(language: str, viewport: dict[str, int], real_access: bool = False, real_personal: bool = False,
                    real_filekit: bool = False, engine: str = "chromium"):
    """Fake Delta bridge; optionally connect access UI to real Python handlers, not a live bot."""
    with tempfile.TemporaryDirectory(prefix="michiflin-browser-fixture-") as temporary:
        root = Path(temporary)
        for name in miniapp.APP_FILES:
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(miniapp.APP_DIR / name, root / name)
        (root / "webxdc.js").write_text(BRIDGE, encoding="utf-8")

        class QuietHandler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                pass

            def do_POST(self) -> None:
                if not ((self.path == "/api/access" and real_access) or (self.path == "/api/personal" and real_personal) or
                        (self.path == "/api/filekit" and real_filekit)):
                    self.send_error(404)
                    return
                try:
                    request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                    chat.set_sender("owner@example.org")
                    if self.path == "/api/personal":
                        from michiflin import personal_ai
                        state.set_personal_ai_secure(True)
                        def fake_provider(_provider, method, suffix, body=None, **_options):
                            if method == "GET" and suffix == "/models":
                                return {"data": [{"id": "simulated-chat"}, {"id": "gpt-image-2"}]}
                            if suffix == "/chat/completions":
                                return {"choices": [{"message": {"content": "SIMULATED personal answer"}, "finish_reason": "stop"}], "usage": {"total_tokens": 3}}
                            raise personal_ai.PersonalError("unsupported")
                        with mock.patch.object(personal_ai, "_request", side_effect=fake_provider):
                            result = miniapp.OPS[request["op"]](7, request.get("args", {}), request.get("lang", "es"))
                    else:
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
                browser = getattr(playwright, engine).launch(**({"args": ["--no-sandbox"]} if engine == "chromium" else {}))
                page = browser.new_page(viewport=viewport, locale=language)
                page.add_init_script(f"window.__fixtureAdmin = true; window.__realAccess = {str(real_access).lower()}; window.__realPersonal = {str(real_personal).lower()}; window.__realFilekit = {str(real_filekit).lower()};")
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
    def test_paused_queue_shows_retained_bytes_and_loss_warning_bilingual(self) -> None:
        for language in ("es", "en"):
            with self.subTest(language=language), browser_fixture(language, {"width": 390, "height": 844}) as (page, errors):
                page.locator('.tab[data-tab="downloads"]:visible').click()
                job = {"id": "7", "title": "SIMULATED partial", "kind": "file", "status": "queued", "hold": False,
                       "size": 1000, "done": 300, "parts": 0, "delivered": 0, "format": "", "created": 0,
                       "error": "", "warning": queue.recovery_warning({"recovery_note": "progress_lost"}, language)}
                page.evaluate("""job => {
                    cache.maintenance_notice = {active:true, es:'Pausa', en:'Paused'};
                    setJobs([job]); bus.emit('maintenance');
                }""", job)
                card = page.locator("#view article.job")
                card.wait_for(state="visible")
                self.assertIn("300 B /", card.inner_text())
                self.assertIn("30.0%", card.locator(".bar i").get_attribute("style"))
                self.assertGreater(card.locator(".row .grow").bounding_box()["width"], 180)
                self.assertIn("progreso" if language == "es" else "progress", card.locator(".note.warn").inner_text())
                self.assertIn("mantenimiento" if language == "es" else "maintenance", card.locator(".status").inner_text().lower())
                self.assertEqual(card.locator(".chips button").count(), 0)
                screenshot_if_requested(page, language + "-partial-progress-warning")
                self.assertEqual(errors, [])

    def test_live_view_zoom_quality_navigation_and_search_with_simulated_realtime(self) -> None:
        # The bridge and picture below are deliberately simulated; the separate
        # sandbox integration verifies real Chromium screenshots and clicks.
        for language in ("es", "en"):
            with self.subTest(language=language), browser_fixture(language, {"width": 390, "height": 844}) as (page, errors):
                page.evaluate(r"""() => {
                  const c = document.createElement('canvas'); c.width = 960; c.height = 1520;
                  const ctx = c.getContext('2d'); ctx.fillStyle = 'white'; ctx.fillRect(0,0,c.width,c.height);
                  ctx.fillStyle = '#18375a'; ctx.font = '48px sans-serif'; ctx.fillText('SIMULATED FRAME', 40, 120);
                  const image = c.toDataURL('image/jpeg', 0.7).split(',')[1];
                  window.__liveActions = [];
                  window.__liveRoute = 'proxy';
                  window.__proxyWarning = '';
                  window.webxdc.joinRealtimeChannel = () => {
                    let listener;
                    return {
                      setListener(fn) { listener = fn; },
                      send(data) {
                        const req = JSON.parse(new TextDecoder().decode(data));
                        window.__liveActions.push(req);
                        if (req.t !== 'start' && req.t !== 'action') return;
                        const msg = {v:1, sid:req.sid, t:'frame', seq:req.seq || 0,
                                     image, width:480, height:760, rendered:req.detail || 'sharp',
                                     route:window.__liveRoute, warning:window.__proxyWarning};
                        // Model a real asynchronous reply: an operation stays pending until this frame arrives.
                        setTimeout(() => listener(new TextEncoder().encode(JSON.stringify(msg))), 200);
                      },
                      leave() {},
                    };
                  };
                  window.__searchResults = [{url:'https://www.tiktok.com/@sample/video/12345',
                    title:'SIMULATED video', source:'TikTok', snippet:'Fixture'}];
                  go('results', {q:'fixture video', engine:'web'});
                }""")
                page.get_by_role("button", name="Abrir en Live" if language == "es" else "Open in Live").click(timeout=15000)
                page.evaluate("""() => {
                  Object.defineProperty(navigator, 'clipboard', {configurable:true, value:{writeText:() => Promise.reject(new Error('blocked'))}});
                  document.execCommand = () => false;
                }""")
                page.get_by_role("button", name="Copiar enlace" if language == "es" else "Copy link").click()
                page.get_by_text("No se pudo copiar" if language == "es" else "Could not copy", exact=False).wait_for()
                settings = page.get_by_text("Calidad y actualización" if language == "es" else "Quality and refresh")
                settings.click()
                auto_name = "Actualizar cada 5 s (usa datos; no es video)" if language == "es" else "Refresh every 5 s (uses data; not video)"
                self.assertTrue(page.get_by_label(auto_name).is_disabled())
                page.get_by_role("tab", name="Menos datos" if language == "es" else "Less data").click()
                self.assertEqual(page.evaluate("window.liveWeb.detail"), "economy")
                page.get_by_role("tab", name="Más nitidez" if language == "es" else "Sharper").click()
                settings.click()
                page.get_by_role("button", name="Iniciar navegador" if language == "es" else "Start browser").click()
                page.wait_for_function("document.querySelector('.live-screen')?.naturalWidth === 960")
                page.wait_for_function("document.querySelector('.live-navigation')?.getBoundingClientRect().top < 150")
                self.assertTrue(page.get_by_role("button", name="Cerrar navegador" if language == "es" else "Close browser").is_visible())
                self.assertLess(page.locator(".live-screen-box").bounding_box()["y"], 250)
                route_label = page.locator(".live-route")
                self.assertIn("proxy configurado" if language == "es" else "proxy configured", route_label.inner_text().lower())
                self.assertNotIn("127.0.0.1", route_label.inner_text())
                refresh = page.get_by_role("button", name="Actualizar vista" if language == "es" else "Refresh view")
                page.evaluate("window.__proxyWarning = 'proxy_unavailable'")
                refresh.click()
                page.get_by_text("No se usó salida directa" if language == "es" else "Direct access was not used", exact=False).wait_for()
                page.evaluate("window.__proxyWarning = ''; window.__liveRoute = 'direct'")
                refresh.click()
                page.get_by_text("Salida: directa, sin proxy." if language == "es" else "Route: direct, no proxy.").wait_for()
                page.evaluate("window.__liveRoute = 'proxy'")
                page.locator(".live-zoom").get_by_role("tab", name="150%").click()
                screen, box = page.locator(".live-screen"), page.locator(".live-screen-box")
                self.assertAlmostEqual(screen.bounding_box()["width"], (box.bounding_box()["width"] - 2) * 1.5, delta=3)
                box.evaluate("e => { e.scrollLeft = 100; e.scrollTop = 100; }")
                self.assertGreater(box.evaluate("e => e.scrollLeft"), 0)
                self.assertGreater(box.evaluate("e => e.scrollTop"), 0)
                box.scroll_into_view_if_needed()
                bounds, image_bounds = box.bounding_box(), screen.bounding_box()
                x, y = bounds["x"] + 30, bounds["y"] + 30
                expected_x = int((x - image_bounds["x"]) / image_bounds["width"] * 480)
                expected_y = int((y - image_bounds["y"]) / image_bounds["height"] * 760)
                page.mouse.click(x, y)
                page.wait_for_function("window.__liveActions.some(x => x.action === 'click')")
                page.wait_for_function("window.liveWeb.ready && !window.liveWeb.pending")
                click = page.evaluate("window.__liveActions.filter(x => x.action === 'click').at(-1)")
                self.assertAlmostEqual(click["x"], expected_x, delta=1)
                self.assertAlmostEqual(click["y"], expected_y, delta=1)
                page.get_by_text("Calidad y actualización" if language == "es" else "Quality and refresh").click()
                page.get_by_role("tab", name="Menos datos" if language == "es" else "Less data").click()
                page.wait_for_function("window.__liveActions.some(x => x.action === 'frame' && x.detail === 'economy')")
                page.wait_for_function("window.liveWeb.ready && !window.liveWeb.pending")
                input_address = page.get_by_placeholder("Dirección HTTPS del mismo sitio" if language == "es" else "HTTPS address on this site")
                input_address.fill("https://facebook.com/")
                input_address.press("Enter")
                page.get_by_text("solo acepta direcciones del mismo sitio" if language == "es" else "only accepts addresses on the same site", exact=False).wait_for()
                self.assertFalse(page.evaluate("window.__liveActions.some(x => x.action === 'goto')"))
                input_address.fill("https://www.tiktok.com/@sample/video/67890")
                input_address.press("Enter")
                page.wait_for_function("window.__liveActions.some(x => x.action === 'goto' && x.url.includes('67890'))")
                page.wait_for_function("window.liveWeb.ready && !window.liveWeb.pending")
                frames_before_auto = page.evaluate("window.__liveActions.filter(x => x.action === 'frame').length")
                page.get_by_label("Actualizar cada 5 s (usa datos; no es video)" if language == "es" else "Refresh every 5 s (uses data; not video)").check()
                page.wait_for_function("n => window.__liveActions.filter(x => x.action === 'frame').length > n", arg=frames_before_auto, timeout=9000)
                screenshot_if_requested(page, f"navegador-{language}")
                page.get_by_label("Actualizar cada 5 s (usa datos; no es video)" if language == "es" else "Refresh every 5 s (uses data; not video)").uncheck()
                self.assertEqual(errors, [])

    def test_notes_are_labelled_and_long_text_wraps_in_both_languages(self) -> None:
        for language in ("es", "en"):
            with self.subTest(language=language), browser_fixture(language, {"width": 375, "height": 640}) as (page, errors):
                page.locator('.tab[data-tab="apps"]:visible').click()
                page.locator(".tile-notes").click()
                placeholder = "Escribe una nota" if language == "es" else "Write a note"
                text = "A long test note should remain completely readable on a narrow screen without being clipped."
                page.get_by_placeholder(placeholder).fill(text)
                page.get_by_role("button", name="Guardar" if language == "es" else "Save", exact=True).click()
                note = page.get_by_text(text, exact=True)
                note.wait_for(state="visible")
                self.assertEqual(note.get_attribute("class"), "grow note-text")
                self.assertGreater(note.bounding_box()["height"], 40)
                self.assertEqual(note.evaluate("e => getComputedStyle(e).overflow"), "visible")
                self.assertEqual(errors, [])

    def test_agent_modes_are_honest_and_bilingual(self) -> None:
        for language in ("es", "en"):
            viewport = {"width": 390, "height": 844} if language == "es" else {"width": 1280, "height": 900}
            with self.subTest(language=language), browser_fixture(language, viewport) as (page, errors):
                page.locator('.tab[data-tab="ai"]:visible').click()
                page.get_by_text("Agente" if language == "es" else "Agent", exact=True).first.click()
                self.assertTrue(page.get_by_role("tab", name="Sin cuota de modelos" if language == "es" else "No model quota").is_visible())
                page.locator("#view textarea").fill("free-mode question")
                page.locator("#view .composer button.primary").click()
                page.get_by_text("SIMULATED agent answer [S1]").wait_for(state="visible")
                sent = page.evaluate("window.__sentUpdates.filter(x => x.payload?.op === 'ask').at(-1).payload.args.space")
                self.assertEqual(sent, "agent")
                page.get_by_role("tab", name="Con IA / API" if language == "es" else "With AI / API").click()
                page.locator("#view .intro").wait_for(state="visible")
                self.assertIn("no inicia" if language == "es" else "cannot log in", page.locator("#view .intro").inner_text().lower())
                self.assertEqual(page.get_by_text("free-mode question").count(), 0)
                screenshot_if_requested(page, language + "-new-agent")
                page.locator("#view textarea").fill("Weather in Havana")
                page.locator("#view .composer button.primary").click()
                page.get_by_text("SIMULATED agent answer [S1]").wait_for(state="visible")
                sent = page.evaluate("window.__sentUpdates.filter(x => x.payload?.op === 'ask').at(-1).payload.args.space")
                self.assertEqual(sent, "smart_agent")
                page.get_by_role("tab", name="Sin cuota de modelos" if language == "es" else "No model quota").click()
                self.assertTrue(page.get_by_text("free-mode question").is_visible())
                self.assertEqual(page.get_by_text("Weather in Havana").count(), 0)

                self.assertEqual(errors, [])

    def test_web_search_and_manual_live_address_without_site_cards(self) -> None:
        cases = [("es", {"width": 320, "height": 700}), ("en", {"width": 1280, "height": 800})]
        for language, viewport in cases:
            with self.subTest(language=language), browser_fixture(language, viewport) as (page, errors):
                self.assertEqual(page.locator("#view .tile-youtube").count(), 1)
                page.locator('.tab[data-tab="apps"]:visible').click()
                self.assertEqual(page.locator("#view .tile-youtube").count(), 1)
                page.evaluate("""() => {
                  store.set('recent', [{q:'preserved search', engine:'videos'}]);
                  store.set('visited', [{url:'https://example.org/', title:'Preserved'}]);
                  prefs.engine = 'videos';
                }""")
                page.locator('.tab[data-tab="web"]:visible').click()
                self.assertTrue(page.get_by_text("Buscar en internet" if language == "es" else "Search the web", exact=True).is_visible())
                self.assertTrue(page.get_by_text("Abrir una dirección en Live" if language == "es" else "Open an address in Live", exact=True).is_visible())
                for name in ("YouTube", "Instagram", "Facebook", "TikTok", "DeepSeek Chat", "Arena.ai", "Revolico", "Z.AI"):
                    self.assertEqual(page.get_by_text(name, exact=True).count(), 0, name)
                self.assertTrue(page.evaluate("'youtube' in VIEWS && !['accounts', 'youtube_account', 'facebook_account', 'tiktok_account'].some(k => k in VIEWS)"))
                self.assertEqual(page.locator('#view .segmented [data-key="web"]').get_attribute("aria-selected"), "true")
                self.assertEqual(page.locator('#view .segmented [data-key="videos"]').count(), 0)
                self.assertEqual(page.evaluate("store.get('recent')[0].q"), "preserved search")
                self.assertEqual(page.evaluate("store.get('visited')[0].title"), "Preserved")
                self.assertTrue(page.evaluate("document.querySelector('#view').scrollWidth <= document.querySelector('#view').clientWidth"))
                screenshot_if_requested(page, language + "-web-simplified")

                manual = page.get_by_label("Dirección para Live" if language == "es" else "Address for Live")
                manual.fill("http://example.org/")
                manual.press("Enter")
                page.get_by_text("Usa una dirección HTTPS" if language == "es" else "Use a public HTTPS address", exact=False).wait_for()
                self.assertTrue(manual.is_visible())
                manual.fill("https://user:password@example.org/")
                manual.press("Enter")
                self.assertEqual(page.evaluate("current.view"), "web")
                manual.fill("ftp://example.org/")
                manual.press("Enter")
                self.assertEqual(page.evaluate("current.view"), "web")
                manual.fill("example.org/path?mode=test#top")
                page.locator(".live-entry-field button").click()
                self.assertEqual(page.evaluate("current.view"), "live_web")
                self.assertEqual(page.evaluate("current.params.url"), "https://example.org/path?mode=test#top")
                self.assertIn("canal cifrado" if language == "es" else "encrypted channel", page.locator("#view").inner_text().lower())
                page.locator("#back").click()

                page.evaluate("""() => { window.__searchResults = [
                  {url:'https://example.org/article?x=1', title:'SIMULATED secure result', source:'Example'},
                  {url:'http://example.net/plain', title:'SIMULATED HTTP result', source:'Example'}]; }""")
                search = page.get_by_placeholder("Buscar o pegar un enlace" if language == "es" else "Search or paste a link")
                search.fill("a public research topic")
                search.press("Enter")
                page.get_by_text("SIMULATED secure result").wait_for(state="visible")
                req = page.evaluate("window.__sentUpdates.filter(x => x.payload?.op === 'search').at(-1).payload.args")
                self.assertEqual(req["q"], "a public research topic")
                self.assertEqual(req["engine"], "web")
                live_label = "Abrir en Live" if language == "es" else "Open in Live"
                secure = page.locator("article.result").filter(has_text="SIMULATED secure result")
                insecure = page.locator("article.result").filter(has_text="SIMULATED HTTP result")
                self.assertEqual(insecure.get_by_role("button", name=live_label).count(), 0)
                secure.get_by_role("button", name=live_label).click()
                self.assertEqual(page.evaluate("current.params.url"), "https://example.org/article?x=1")
                page.locator("#back").click()
                search = page.get_by_placeholder("Buscar o pegar un enlace" if language == "es" else "Search or paste a link")
                search.fill("https://example.org/?query=1#section")
                search.press("Enter")
                self.assertEqual(page.evaluate("current.view"), "live_web")
                self.assertEqual(page.evaluate("current.params.url"), "https://example.org/?query=1#section")
                page.locator('.tab[data-tab="home"]:visible').click()
                home_search = page.get_by_placeholder("Buscar o pegar un enlace" if language == "es" else "Search or paste a link")
                home_search.fill("example.org/?from=home")
                home_search.press("Enter")
                self.assertEqual(page.evaluate("current.view"), "live_web")
                self.assertEqual(page.evaluate("current.params.url"), "https://example.org/?from=home")
                self.assertFalse(page.evaluate("window.__sentUpdates.some(x => ['youtube_account','facebook_account','tiktok_account'].includes(x.payload?.op))"))
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
                sent_modes = page.evaluate("window.__sentUpdates.filter(x => ['kill_switch','nauta_strict'].includes(x.payload?.op)).length")
                self.assertEqual(sent_modes, 0, "opening settings must not send mode operations")
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
                page.locator('#view .space-card').nth(0).click()
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
                  project.files = [{path:'important.txt', text:'x'.repeat(560000)}];
                  coder.set(project);
                }""")
                before = page.evaluate("window.__sentUpdates.filter(x => x.payload?.op === 'code').length")
                self.assertFalse(page.evaluate("coderSend('continue')"))
                self.assertEqual(before, page.evaluate("window.__sentUpdates.filter(x => x.payload?.op === 'code').length"))
                self.assertEqual(page.evaluate("coder.get().files[0].text.length"), 560000)
                self.assertEqual(page.evaluate("coder.get().files[0].path"), "important.txt")
                self.assertEqual(errors, [])

    def test_incomplete_coder_response_preserves_original_project(self) -> None:
        for language in ("es", "en"):
            with self.subTest(language=language), browser_fixture(language, {"width": 390, "height": 844}) as (page, errors):
                actual = page.evaluate("""() => {
                  const project = coder.get();
                  project.files = [{path:'a.txt',text:'original'},{path:'b.txt',text:'keep'}];
                  project.rid = 'pending'; coder.set(project);
                  bridge.handlers.code({files:[{path:'a.txt',text:'partial'}], project_complete:false,
                    summary:'export incomplete', steps:[]}, null, null, 'pending');
                  return coder.get().files;
                }""")
                self.assertEqual(actual, [{"path":"a.txt","text":"original"},{"path":"b.txt","text":"keep"}])
                # A response from an older server without the completeness marker is also not applied.
                actual = page.evaluate("""() => { const p = coder.get(); p.rid = 'old'; coder.set(p);
                  bridge.handlers.code({files:[],summary:'old response'},null,null,'old'); return coder.get().files; }""")
                self.assertEqual(len(actual), 2)
                actual = page.evaluate("""() => { const p = coder.get(); p.rid = 'complete'; coder.set(p);
                  bridge.handlers.code({files:[{path:'new.txt',text:'ok'}],project_complete:true,summary:'ok'},null,null,'complete');
                  return coder.get().files; }""")
                self.assertEqual(actual, [{"path":"new.txt","text":"ok"}])
                self.assertEqual(errors, [])

    def test_youtube_music_and_chat_images_bilingual(self) -> None:
        cases = [("es", {"width": 375, "height": 844}), ("en", {"width": 1280, "height": 800})]
        for language, viewport in cases:
            with self.subTest(language=language), browser_fixture(language, viewport) as (page, errors):
                page.evaluate("""() => { window.__searchResults = [
                  {url:'https://www.youtube.com/watch?v=abc123', title:'SIMULATED video', source:'YouTube'},
                  {url:'https://elsewhere.example/video', title:'OTHER SITE', source:'Elsewhere'}]; }""")
                page.locator('.tab[data-tab="apps"]:visible').click()
                self.assertEqual(page.locator("#view .tile-youtube").count(), 1)
                self.assertEqual(page.locator("#view .tile-audio").count(), 1)
                self.assertEqual(page.locator("#view .tile-facebook, #view .tile-tiktok").count(), 0)
                page.locator("#view .tile-youtube").click()
                query = page.locator('#view input[type="search"]')
                query.fill("sailboat")
                query.press("Enter")
                page.get_by_text("SIMULATED video").wait_for(state="visible")
                self.assertEqual(page.get_by_text("OTHER SITE").count(), 0)
                screenshot_if_requested(page, language + "-youtube")
                page.locator("article.video").get_by_role("button", name="Audio").click()
                sheet = page.locator("#sheet.open")
                self.assertIn("Formato" if language == "es" else "format", sheet.inner_text().lower() if language == "en" else sheet.inner_text())
                sheet.get_by_role("tab", name="OPUS").click()
                self.assertEqual(sheet.get_by_role("tab", name="OPUS").get_attribute("aria-selected"), "true")
                self.assertEqual(sheet.get_by_role("tab", name="MP3").get_attribute("aria-selected"), "false")
                sheet.get_by_role("button", name="128 kbps").click()
                screenshot_if_requested(page, language + "-youtube-audio-quality")
                sheet.get_by_role("button", name="Iniciar descarga" if language == "es" else "Start download").click()
                page.wait_for_function("window.__sentUpdates.some(x => x.payload?.op === 'action')")
                action = page.evaluate("window.__sentUpdates.filter(x => x.payload?.op === 'action').at(-1).payload.args")
                self.assertEqual((action["kind"], action["quality"], action["audio_format"]), ("audio", 128, "opus"))

                page.locator('.tab[data-tab="apps"]:visible').click()
                page.locator("#view .tile-audio").click()
                page.get_by_role("tab", name="Internet Archive").click()
                page.locator('#view input[type="search"]').fill("jazz")
                page.locator('#view input[type="search"]').press("Enter")
                page.get_by_text("Licensed jazz").wait_for(state="visible")
                screenshot_if_requested(page, language + "-music-results")
                page.get_by_text("Licensed jazz").click()
                page.get_by_text("Jazz track").wait_for(state="visible")
                self.assertTrue(page.get_by_text("VBR MP3", exact=False).is_visible())
                self.assertTrue(page.get_by_text("CC BY", exact=False).is_visible())
                # A real Archive license URL previously widened the mobile view beyond the screen.
                widths = page.locator("#view .note").evaluate("el => [el.scrollWidth, el.clientWidth]")
                self.assertLessEqual(widths[0], widths[1], f"music license overflow: {widths}")
                screenshot_if_requested(page, language + "-music-formats")
                page.locator(".music-file").get_by_role("button", name="Descargar" if language == "es" else "Download").click()
                page.wait_for_function("window.__sentUpdates.filter(x => x.payload?.op === 'action').length >= 2")
                action = page.evaluate("window.__sentUpdates.filter(x => x.payload?.op === 'action').at(-1).payload.args")
                self.assertEqual(action["kind"], "download")
                self.assertTrue(action["url"].startswith("https://archive.org/download/"))
                page.locator("#sheet .sheet-head button").click()
                page.locator("#sheet").wait_for(state="hidden")

                page.locator('.tab[data-tab="ai"]:visible').click()
                page.get_by_text("Agente" if language == "es" else "Agent", exact=True).first.click()
                page.get_by_role("tab", name="Con IA / API" if language == "es" else "With AI / API").click()
                page.locator("#view textarea").fill("a red sailboat")
                page.get_by_role("button", name="Crear imagen" if language == "es" else "Create image").click()
                page.locator(".chat-image img").wait_for(state="visible")
                self.assertTrue(page.get_by_text("OVH test", exact=False).first.is_visible())
                image_request = page.evaluate("window.__sentUpdates.filter(x => x.payload?.op === 'image').at(-1).payload.args")
                self.assertEqual(image_request["prompt"], "a red sailboat")
                self.assertTrue(page.evaluate("messages('smart_agent').some(m => m.imageId)"))
                self.assertTrue(page.evaluate("(async () => !!(await media.get(messages('smart_agent').find(m => m.imageId).imageId)))()"))
                screenshot_if_requested(page, language + "-chat-image")
                page.evaluate("go('chat', {space:'other:model'})")
                self.assertEqual(page.get_by_text("a red sailboat", exact=True).count(), 0)
                self.assertTrue(page.evaluate("messages('smart_agent').some(m => m.imageId)"))
                self.assertEqual(errors, [])

    def test_personal_api_webcrypto_real_backend_synthetic_provider_bilingual(self) -> None:
        # Browser performs real WebCrypto and Python decrypts/saves it; Delta and
        # the external provider are synthetic, with no network or paid calls.
        fake_key = "synthetic-browser-key-never-real-123"
        saved_state = copy.deepcopy(state.data)
        try:
            for language in ("es", "en"):
                viewport = {"width": 375, "height": 844} if language == "es" else {"width": 1280, "height": 850}
                with self.subTest(language=language), tempfile.TemporaryDirectory(prefix="personal-browser-") as tmp, \
                     mock.patch.object(config, "CONFIG_DIR", Path(tmp)), mock.patch.object(state, "CONFIG_DIR", Path(tmp)), \
                     mock.patch.object(state, "STATE_FILE", Path(tmp) / "state.json"), \
                     mock.patch.dict(os.environ, {"WEB_SESSION_KEY": "browser-synthetic-master-" + "X" * 48}), \
                     mock.patch.object(config, "OWNER_ADDRESS", "owner@example.org"), \
                     mock.patch.object(config, "ADMIN_ADDRESSES", {"owner@example.org"}), \
                     browser_fixture(language, viewport, real_personal=True) as (page, errors):
                    page.locator('.tab[data-tab="ai"]:visible').click()
                    page.get_by_text("Mis APIs y modelos" if language == "es" else "My APIs and models", exact=True).click()
                    page.get_by_label("Nombre del proveedor" if language == "es" else "Provider name").first.fill("Synthetic")
                    page.get_by_label("URL base HTTPS" if language == "es" else "HTTPS base URL").fill("https://example.com/v1")
                    page.locator('#view input[type="password"]').fill(fake_key)
                    page.get_by_role("button", name="Agregar API personal" if language == "es" else "Add personal API", exact=True).click()
                    page.locator("#sheet").get_by_role("button", name="Confirmar" if language == "es" else "Confirm").click()
                    page.get_by_text("Synthetic", exact=True).first.wait_for(state="visible", timeout=15000)
                    self.assertEqual(page.locator('#view input[type="password"]').input_value(), "")
                    updates = json.dumps(page.evaluate("window.__sentUpdates"))
                    local = json.dumps(page.evaluate("Object.values(localStorage)"))
                    self.assertNotIn(fake_key, updates)
                    self.assertNotIn(fake_key, local)
                    sealed = page.evaluate("window.__sentUpdates.find(x => x.payload?.op === 'personal_ai' && x.payload?.args?.do === 'add').payload.args.sealed")
                    self.assertEqual(set(sealed), {"key", "iv", "data"})
                    files = list((Path(tmp) / "personal-ai-v1").glob("*.enc"))
                    self.assertGreaterEqual(len(files), 2)
                    self.assertTrue(all(fake_key.encode() not in f.read_bytes() for f in files))
                    widths = page.locator("#view").evaluate("e => [e.scrollWidth, e.clientWidth]")
                    self.assertLessEqual(widths[0], widths[1])
                    screenshot_if_requested(page, language + "-personal-api")
                    page.locator('.tab[data-tab="ai"]:visible').click()
                    provider = page.locator("#view .provider-card").filter(has_text="Synthetic")
                    provider.locator(".provider-head").click()
                    provider.locator(".model-category > summary").first.click()
                    page.get_by_text("simulated-chat", exact=True).first.click()
                    page.locator("#view textarea").fill("test question")
                    page.locator("#view .composer button.primary").click()
                    page.get_by_text("SIMULATED personal answer").wait_for(state="visible", timeout=15000)
                    model = page.evaluate("window.__sentUpdates.filter(x => x.payload?.op === 'ask').at(-1).payload.args.space")
                    self.assertTrue(model.startswith("personal-"))
                    self.assertEqual(errors, [])
        finally:
            state.data.clear(); state.data.update(saved_state)
            state.act_as("")

    def test_youtube_show_more_and_maintenance_controls_bilingual(self) -> None:
        for language in ("es", "en"):
            with self.subTest(language=language), browser_fixture(language, {"width": 375, "height": 844}) as (page, errors):
                page.evaluate("""() => { window.__searchResults = Array.from({length: 32}, (_,i) =>
                  ({url:'https://www.youtube.com/watch?v=video' + String(i).padStart(6,'0'), title:'SIM VIDEO ' + i, source:'YouTube'})); }""")
                page.locator('.tab[data-tab="apps"]:visible').click()
                page.locator("#view .tile-youtube").click()
                page.locator('#view input[type="search"]').fill("birds")
                page.locator('#view input[type="search"]').press("Enter")
                page.get_by_text("SIM VIDEO 14", exact=True).wait_for()
                self.assertEqual(page.get_by_text("SIM VIDEO 15", exact=True).count(), 0)
                more = page.get_by_role("button", name="Mostrar más" if language == "es" else "Show more")
                more.click()
                page.get_by_text("SIM VIDEO 29", exact=True).wait_for()
                page.get_by_role("button", name="Mostrar más" if language == "es" else "Show more").click()
                page.get_by_text("SIM VIDEO 31", exact=True).wait_for()
                self.assertEqual(page.get_by_role("button", name="Mostrar más" if language == "es" else "Show more").count(), 0)
                self.assertEqual(page.evaluate("window.__sentUpdates.filter(x => x.payload?.op === 'youtube_search').map(x => x.payload.args.page)"), [1, 2, 3])
                screenshot_if_requested(page, language + "-youtube-more")
                page.locator('.tab[data-tab="settings"]:visible, #gear:visible').first.click()
                page.get_by_text("Administración" if language == "es" else "Administration", exact=True).first.click()
                notice = page.locator(".maintenance-settings")
                notice.wait_for(state="visible")
                notice.locator('input[type="checkbox"]').check()
                notice.get_by_label("Mensaje en español" if language == "es" else "Message in Spanish").fill("Aviso de prueba")
                notice.get_by_label("Mensaje en inglés" if language == "es" else "Message in English").fill("Test notice")
                notice.get_by_role("button", name="Guardar aviso" if language == "es" else "Save notice").click()
                page.wait_for_function("window.__sentUpdates.some(x => x.payload?.op === 'maintenance_notice' && x.payload.args.active === true)")
                screenshot_if_requested(page, language + "-maintenance")
                self.assertEqual(errors, [])

    def test_changed_bot_session_refreshes_without_repeating_image_bilingual(self) -> None:
        for language in ("es", "en"):
            with self.subTest(language=language), browser_fixture(language, {"width": 390, "height": 844}) as (page, errors):
                page.locator('.tab[data-tab="ai"]:visible').click()
                page.get_by_text("Agente" if language == "es" else "Agent", exact=True).first.click()
                page.get_by_role("tab", name="Con IA / API" if language == "es" else "With AI / API").click()
                page.evaluate("""() => { window.__fixtureSession = 'new-synthetic-process'; window.__sessionChangedNext = 'image'; }""")
                before = page.evaluate("window.__sentUpdates.filter(x => x.payload?.op === 'hello').length")
                page.locator("#view textarea").fill("red sailboat")
                page.get_by_role("button", name="Crear imagen" if language == "es" else "Create image").click()
                page.get_by_text("revisa los cargos" if language == "es" else "check provider charges", exact=False).first.wait_for()
                page.wait_for_function("count => window.__sentUpdates.filter(x => x.payload?.op === 'hello').length > count", arg=before)
                page.wait_for_function("JSON.parse(localStorage.getItem('mf.botSession')) === 'new-synthetic-process'")
                page.wait_for_timeout(300)
                requests = page.evaluate("window.__sentUpdates.filter(x => x.payload?.op === 'image').map(x => x.payload)")
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0]["session"], "synthetic-boot-session")
                self.assertEqual(errors, [])
                screenshot_if_requested(page, language + "-session-change")

    def test_large_coder_request_uses_bounded_multipart(self) -> None:
        with browser_fixture("en", {"width": 390, "height": 844}) as (page, errors):
            sent = page.evaluate("""() => {
              const p = coder.get(); p.files = [{path:'a.txt', text:'á'.repeat(70000)}]; coder.set(p);
              return coderSend('continue');
            }""")
            self.assertTrue(sent)
            chunks = page.evaluate("window.__sentUpdates.map(x => x.payload).filter(p => p?.t === 'req_chunk')")
            self.assertGreater(len(chunks), 1)
            decoded = json.loads(base64.b64decode("".join(item["s"] for item in chunks)))
            self.assertEqual(decoded["args"]["files"][0]["text"], "á" * 70000)
            self.assertTrue(all(len(item["s"]) <= 74000 for item in chunks))
            self.assertEqual(errors, [])

    def test_tabs_and_all_app_tiles_render_without_browser_errors(self) -> None:
        with tempfile.TemporaryDirectory(prefix="michiflin-webxdc-smoke-") as temporary:
            root = Path(temporary)
            for name in miniapp.APP_FILES:
                (root / name).parent.mkdir(parents=True, exist_ok=True)
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
                    # In headless mode Playwright may use the separately installed
                    # chromium-headless-shell even when the full browser is absent.
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
