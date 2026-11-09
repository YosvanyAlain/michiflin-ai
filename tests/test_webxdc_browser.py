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
from contextlib import ExitStack, contextmanager, nullcontext
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
  const models = { now: Date.now() / 1000, reset: Date.now() / 1000 + 86400, providers: [], images: [], pin: "" };
  const health = {
    version: "browser-smoke", now: Date.now() / 1000, uptime: 60, restart: Date.now() / 1000 + 86400,
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
  window.__weatherHome = { city: "La Habana, Cuba", place_id: 0, auto: true, last: null,
    next_at: Date.now() / 1000 + 21600, interval: 21600 };
  const responseFor = (request) => {
    switch (request.op) {
      case "hello": return { version: "browser-smoke", session: window.__fixtureSession, admin: !!window.__fixtureAdmin, models, health, settings, jobs: [],
        modes: { kill_switch: {active:window.__modes.kill_switch}, nauta_strict: {active:window.__modes.nauta_strict, part_mb:3} },
        maintenance_notice: {active:false, es:"Mensaje de mantenimiento", en:"Maintenance message", revision:0, expires:0},
        weather_home: window.__weatherHome };
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
      case "weather": {
        const placeId = request.args.place_id || 1;
        const data = {query: request.args.city, place:{id:placeId,label:placeId === 1 ? "Havana, Cuba" : "Havana, USA"},
          candidates:[{id:1,label:"Havana, Cuba"},{id:2,label:"Havana, USA"}],
          timezone:"America/Havana",as_of:"2026-10-29T10:00",current:{temp:28,feels:30,humidity:72,wind:13,precipitation:0,code:0,is_day:1,description:"clear",descriptions:{es:"despejado",en:"clear"}},
          days:[{date:"2026-10-29",code:0,description:"clear",descriptions:{es:"despejado",en:"clear"},high:30,low:23,rain:10},{date:"2026-10-30",code:63,description:"rain",descriptions:{es:"lluvia",en:"rain"},high:29,low:24,rain:65}],source:"Open-Meteo"};
        const at = Date.now() / 1000;
        window.__weatherHome.last = {at, data};
        return {...data, saved_at:at, saved_on_bot:true};
      }
      case "weather_settings":
        if (request.args.do === "auto") window.__weatherHome.auto = request.args.on;
        if (request.args.do === "use_last" && window.__weatherHome.last) {
          const last = window.__weatherHome.last.data;
          if (last.query !== request.args.city || last.place.id !== request.args.place_id) throw Error("result changed");
          window.__weatherHome.city = last.query; window.__weatherHome.place_id = last.place.id;
        }
        return window.__weatherHome;
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
      case "ask": return window.__fixtureAskResult || { text: "SIMULATED agent answer [S1]", sig: "Mock model" };
      case "code": return { files:[{path:"synthetic.txt",text:"synthetic"}], project_complete:true, model_confirmed:true,
        summary:"Proyecto sintético listo", steps:[], name:"Synthetic" };
      case "queue": return { jobs: [] };
      case "action": return { title: "Smoke test", text: "smoke test result" };
      default: return {};
    }
  };
  window.__sentUpdates = [];
  window.__fixtureResponses = [];
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
                (window.__realFilekit && request.op === "filekit") ||
                (window.__realDelivery && request.op === "reinstall")) {
              const endpoint = request.op === "filekit" ? "/api/filekit" :
                request.op === "reinstall" ? "/api/delivery" :
                window.__realPersonal && ["personal_ai", "models", "ask"].includes(request.op) ? "/api/personal" : "/api/access";
              const result = await fetch(endpoint,
                { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(request) });
              const body = await result.json();
              data = body.data;
              error = body.error || "";
            } else data = responseFor(request);
          } catch (err) { error = String(err); }
          if (window.__listener) {
            window.__fixtureResponses.push(request.op);
            window.__listener({
              serial: ++window.__serial,
              payload: { t: "res", re: request.id, ok: !fail && !error, data: (fail || error) ? null : data,
                error: fail ? "SIMULATED network uncertainty" : error, code }
            });
          }
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
                    real_filekit: bool = False, engine: str = "chromium", touch: bool = False,
                    legacy_package: bool | str = False, delivery_handler=None):
    """Fake Delta bridge; optionally connect UI to Python handlers, never a live bot."""
    from michiflin import app_compat
    with tempfile.TemporaryDirectory(prefix="michiflin-browser-fixture-") as temporary:
        root = Path(temporary)
        is_recent = legacy_package == "recent"
        if legacy_package:
            build = app_compat.recent_build if is_recent else app_compat.previous_build
            if not build("fixture-epoch", str(miniapp.APP_DIR), miniapp.APP_FILES):
                raise ValueError("the previous package could not be verified")
        for name in miniapp.APP_FILES:
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            archive, replaced = (app_compat._RECENT, app_compat._RECENT_REPLACED) if is_recent else (app_compat._PREVIOUS, app_compat._REPLACED)
            source = archive if legacy_package and name in replaced else miniapp.APP_DIR
            shutil.copy2(source / name, root / name)
        (root / "webxdc.js").write_text(BRIDGE, encoding="utf-8")

        class QuietHandler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                pass

            def do_POST(self) -> None:
                if not ((self.path == "/api/access" and real_access) or (self.path == "/api/personal" and real_personal) or
                        (self.path == "/api/filekit" and real_filekit) or (self.path == "/api/delivery" and delivery_handler)):
                    self.send_error(404)
                    return
                try:
                    request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                    chat.set_sender("owner@example.org")
                    if self.path == "/api/delivery":
                        result = delivery_handler(request)
                    elif self.path == "/api/personal":
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
                page = browser.new_page(viewport=viewport, locale=language, has_touch=touch)
                page.add_init_script(f"window.__fixtureAdmin = true; window.__realAccess = {str(real_access).lower()}; window.__realPersonal = {str(real_personal).lower()}; window.__realFilekit = {str(real_filekit).lower()}; window.__realDelivery = {str(bool(delivery_handler)).lower()};")
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


def install_live_frame_fixture(page) -> None:
    """Open Live against a harmless synthetic still frame and recorded actions."""
    page.evaluate(r"""() => {
      const canvas = document.createElement('canvas');
      canvas.width = 480; canvas.height = 760;
      const ctx = canvas.getContext('2d');
      ctx.fillStyle = 'white'; ctx.fillRect(0, 0, 480, 760);
      ctx.fillStyle = 'black'; ctx.font = '22px sans-serif';
      for (let i = 0; i < 15; i++) ctx.fillText('Fixture ' + (i + 1), 20, 32 + i * 48);
      const image = canvas.toDataURL('image/jpeg', 0.7).split(',')[1];
      window.__testLiveActions = [];
      window.webxdc.joinRealtimeChannel = () => {
        let listener;
        return {
          setListener(fn) { listener = fn; },
          send(data) {
            const req = JSON.parse(new TextDecoder().decode(data));
            window.__testLiveActions.push(req);
            if (req.t !== 'start' && req.t !== 'action') return;
            const msg = { v: 1, sid: req.sid, t: req.action === 'close' ? 'closed' : 'frame',
              seq: req.seq || 0, image, width: 480, height: 760, rendered: req.detail || 'economy' };
            setTimeout(() => listener(new TextEncoder().encode(JSON.stringify(msg))), window.__testLiveDelay || 100);
          },
          leave() {},
        };
      };
      go('live_web', { url: 'https://fixture.example/' });
    }""")


@unittest.skipUnless(sync_playwright, "playwright is not installed")
class WebXDCBrowserSmoke(unittest.TestCase):
    def test_previous_package_manual_update_needs_confirmation_and_keeps_old_instance(self) -> None:
        """Exercise the old iPhone-sized UI through the real, synthetic delivery handler."""
        from concurrent.futures import Future
        from michiflin import app_compat, live_web, maintenance, perf

        class Immediate:
            def submit(self, function, *args):
                future = Future()
                try:
                    future.set_result(function(*args))
                except Exception as error:
                    future.set_exception(error)
                return future

        owner, epoch = "owner@example.org", "fixture-epoch"
        old_build = app_compat.previous_build(epoch, str(miniapp.APP_DIR), miniapp.APP_FILES)
        self.assertTrue(old_build)
        entry = {"chat": 7, "addr": owner, "build": old_build, "epoch": epoch,
                 "locale": "es", "sent": time.time(), "serial": 0, "access_session": ""}
        original_entry = copy.deepcopy(entry)
        synthetic_state = copy.deepcopy(state.data)
        synthetic_state["apps"] = {"100": entry}
        sent, replies = [], []

        def fake_send(chat_id, text, **kwargs):
            self.assertEqual(chat_id, 7)
            self.assertEqual(kwargs.get("viewtype"), "Webxdc")
            self.assertFalse(kwargs.get("interrupted", lambda: False)())
            self.assertTrue(Path(kwargs["file"]).is_file())
            sent.append((text, kwargs))
            return 201

        def record_response(instance, request_id, data, error="", **_kwargs):
            self.assertEqual(instance, 100)
            replies.append((request_id, data, error))

        def delivery_handler(request):
            self.assertEqual(request.get("op"), "reinstall")
            before = len(replies)
            miniapp.handle(100, dict(entry), request)
            if len(replies) != before + 1 or replies[-1][2]:
                raise ValueError("synthetic delivery refused")
            return replies[-1][1]

        with tempfile.TemporaryDirectory(prefix="michiflin-update-fixture-") as temporary, ExitStack() as stack:
            for obj, name, value in (
                (state, "data", synthetic_state), (state, "save", lambda: None),
                (state, "maintenance_active", lambda: False), (state, "maintenance_interrupted", lambda _generation: False),
                (state, "get_kill_switch", lambda _address: False), (state, "kill_generation", lambda _address: 0),
                (state, "work_interrupted", lambda _address, _generation: False),
                (state, "guard_work", lambda *_args: nullcontext()),
                (config, "BOT_EPOCH", epoch), (config, "BOT_EPOCH_STRICT", True),
                (config, "TEMP_ROOT", Path(temporary)), (config, "service_access_allowed", lambda address: address == owner),
                (access, "bound_allowed", lambda item: item.get("addr") == owner),
                (admin, "is_allowed", lambda address: address == owner), (admin, "seen", lambda _address: None),
                (miniapp, "_verified_binding", lambda _entry: True), (miniapp, "_workers", Immediate()),
                (miniapp, "_upgraded", {}), (miniapp, "respond", record_response),
                (live_web, "_identity", lambda instance: (entry, owner) if instance == 100 else None),
                (chat, "sender", lambda: owner), (chat, "send", fake_send),
                (maintenance, "text", lambda _locale: ""), (perf, "note_op", lambda *_args: None),
            ):
                stack.enter_context(mock.patch.object(obj, name, value))
            with browser_fixture("es", {"width": 390, "height": 844}, engine="webkit", touch=True,
                                 legacy_package=True, delivery_handler=delivery_handler) as (page, errors):
                self.assertEqual(sent, [])  # opening and hello are not consent
                page.evaluate("go('settings')")
                request = page.get_by_role("button", name="Pedir app nueva", exact=True)
                request.tap()
                page.get_by_text("Cada instancia tiene almacenamiento independiente", exact=False).wait_for()
                self.assertEqual(sent, [])
                page.get_by_role("button", name="Cancelar", exact=True).tap()
                self.assertEqual(sent, [])
                request.tap()
                page.get_by_role("button", name="Enviar app nueva", exact=True).tap()
                page.get_by_text("Listo: abre la app nueva", exact=False).wait_for()
                self.assertEqual(len(sent), 1)
                self.assertEqual(set(synthetic_state["apps"]), {"100", "201"})
                self.assertEqual(synthetic_state["apps"]["100"], original_entry)
                self.assertEqual(synthetic_state["apps"]["201"]["build"], miniapp.app_build())
                self.assertIn("almacenamiento independiente", sent[0][0])
                # A second synthetic confirmation is deduplicated, not another delivery.
                request.tap()
                page.get_by_role("button", name="Enviar app nueva", exact=True).tap()
                page.wait_for_function("window.__fixtureResponses.filter(op => op === 'reinstall').length === 2")
                self.assertEqual(len(sent), 1)
                self.assertEqual(len(replies), 2)
                self.assertTrue(replies[-1][1].get("already"))
                self.assertEqual(errors, [])

    def test_home_carousel_and_saved_weather_bilingual(self) -> None:
        cases = [("es", {"width": 320, "height": 760}), ("en", {"width": 1280, "height": 800})]
        for language, viewport in cases:
            with self.subTest(language=language), browser_fixture(language, viewport) as (page, errors):
                page.emulate_media(reduced_motion="reduce")
                self.assertEqual(page.locator(".home-carousel").count(), 1)
                self.assertEqual(page.locator(".home-banner-controls button").count(), 3)
                if language == "es":
                    self.assertIn("la tasa de cambio a un toque", page.evaluate("I18N.es.home_sub_morning"))
                rate_name = "Tasas de cambio" if language == "es" else "Exchange rates"
                weather_name = "Clima" if language == "es" else "Weather"
                page.locator(".home-banner-controls").get_by_role("button", name=rate_name).click()
                self.assertTrue(page.locator(".home-banner-slot .rate-banner").is_visible())
                page.locator(".home-banner-controls").get_by_role("button", name=weather_name).click()
                self.assertTrue(page.locator(".home-banner-slot .weather-banner").is_visible())
                self.assertIn("La Habana", page.locator(".home-banner-slot .weather-banner").inner_text())
                screenshot_if_requested(page, language + "-home-weather-empty")
                page.locator(".home-banner-slot .weather-banner").click()
                self.assertEqual(page.evaluate("current.view"), "weather")
                self.assertTrue(page.get_by_text("no se usa la ubicación del dispositivo" if language == "es" else "device location is not used", exact=False).is_visible())
                city = page.get_by_placeholder("Ciudad" if language == "es" else "City")
                city.fill("La Habana, Cuba")
                city.press("Enter")
                page.locator(".weather-temp").wait_for()
                self.assertIn("28", page.locator(".weather-temp").inner_text())
                self.assertEqual(page.evaluate("weatherHome().last.data.place.id"), 1)
                use = page.get_by_role("button", name="Usar esta ubicación como predeterminada" if language == "es" else "Use this location as the default")
                use.click()
                page.get_by_role("button", name="Ubicación predeterminada" if language == "es" else "Default location", exact=True).wait_for()
                self.assertEqual(page.evaluate("window.__weatherHome.place_id"), 1)
                pause = page.get_by_role("button", name="Pausar actualización" if language == "es" else "Pause refresh")
                pause.click()
                page.get_by_role("button", name="Activar actualización" if language == "es" else "Enable refresh").wait_for()
                self.assertFalse(page.evaluate("window.__weatherHome.auto"))
                page.get_by_role("button", name="Activar actualización" if language == "es" else "Enable refresh").click()
                page.get_by_role("button", name="Pausar actualización" if language == "es" else "Pause refresh").wait_for()
                screenshot_if_requested(page, language + "-weather-saved")
                weather_calls = page.evaluate("window.__sentUpdates.filter(x => x.payload?.op === 'weather').length")
                page.locator('.tab[data-tab="home"]:visible').click()
                page.locator(".home-banner-controls").get_by_role("button", name=weather_name).click()
                self.assertIn("28", page.locator(".weather-banner").inner_text())
                screenshot_if_requested(page, language + "-home-weather-saved")
                page.locator(".weather-banner").click()
                self.assertIn("28", page.locator(".weather-temp").inner_text())
                self.assertIn("despejado" if language == "es" else "clear", page.locator(".weather-place").inner_text().lower())
                # Switch language while offline: descriptions already travel with
                # the stored forecast; no weather request is necessary.
                page.evaluate("""() => { prefs.lang = lang() === 'es' ? 'en' : 'es'; render(); }""")
                self.assertIn("clear" if language == "es" else "despejado", page.locator(".weather-place").inner_text().lower())
                self.assertIn("rain" if language == "es" else "lluvia", page.locator(".weather-days").inner_text().lower())
                self.assertEqual(page.evaluate("weatherDescription({description: 'old snapshot'})"), "old snapshot")
                self.assertEqual(page.evaluate("window.__sentUpdates.filter(x => x.payload?.op === 'weather').length"), weather_calls)
                self.assertTrue(page.evaluate("document.querySelector('#view').scrollWidth <= document.querySelector('#view').clientWidth"))
                self.assertEqual(errors, [])

    def test_home_carousel_rotates_without_extra_weather_queries(self) -> None:
        with browser_fixture("es", {"width": 390, "height": 844}) as (page, errors):
            page.wait_for_function("document.querySelector('.home-banner-controls [aria-current=true]')?.getAttribute('aria-label') === 'Tasas de cambio'", timeout=11000)
            page.wait_for_function("document.querySelector('.home-banner-controls [aria-current=true]')?.getAttribute('aria-label') === 'Clima'", timeout=11000)
            self.assertEqual(page.evaluate("window.__sentUpdates.filter(x => x.payload?.op === 'weather').length"), 0)
            self.assertEqual(errors, [])

    def test_human_gestures_operate_a_local_simulated_challenge(self) -> None:
        """No third-party sites or real challenges: exercise the real Chromium input path."""
        from michiflin import remote_worker
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(args=["--no-sandbox"])
            try:
                page = browser.new_page(viewport={"width": 480, "height": 760})
                page.set_content('''<!doctype html><style>body {margin:0}
                  #slider {position:absolute;left:20px;top:25px;width:220px}
                  #hold {position:absolute;left:20px;top:90px;width:150px;height:50px}
                  iframe {position:absolute;left:20px;top:180px;width:200px;height:90px}</style>
                  <input id="slider" type="range" min="0" max="100" value="0">
                  <button id="hold">Hold</button>
                  <iframe srcdoc="&lt;button id='inside' onclick='document.body.dataset.clicked=1'&gt;Choose&lt;/button&gt;"></iframe>
                  <script>hold.onpointerdown=()=>window.pressed=performance.now();
                    hold.onpointerup=()=>window.held=performance.now()-window.pressed;</script>''')
                remote_worker.drag_pointer(page, [
                    {"x": 30, "y": 34, "ms": 0}, {"x": 80, "y": 34, "ms": 80},
                    {"x": 160, "y": 34, "ms": 160}, {"x": 220, "y": 34, "ms": 240},
                ])
                self.assertGreater(int(page.locator("#slider").input_value()), 70)
                remote_worker.drag_pointer(page, [{"x": 70, "y": 110, "ms": 0}, {"x": 70, "y": 110, "ms": 200}])
                self.assertGreaterEqual(page.evaluate("window.held"), 180)
                frame = page.frame_locator("iframe")
                box = frame.locator("#inside").bounding_box()
                page.mouse.click(box["x"] + 8, box["y"] + 8)
                self.assertEqual(frame.locator("body").get_attribute("data-clicked"), "1")
                result = remote_worker.frame(page, "economy")
                self.assertEqual(result["kind"], "frame")
                self.assertEqual(result["rendered"], "economy")
            finally:
                browser.close()

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

    def test_live_small_mobile_frame_scroll_and_swipe(self) -> None:
        # A narrow mobile frame used to be trapped in a second vertical scroller.
        # The realtime bridge and picture here are synthetic, not a live site.
        for language, engine, viewport in (
            ("es", "chromium", {"width": 320, "height": 568}),
            ("es", "chromium", {"width": 360, "height": 640}),
            ("en", "webkit", {"width": 360, "height": 640}),
        ):
            with self.subTest(language=language, engine=engine, viewport=viewport), browser_fixture(language, viewport, engine=engine) as (page, errors):
                install_live_frame_fixture(page)
                page.get_by_role("button", name="Iniciar navegador" if language == "es" else "Start browser").click()
                page.wait_for_function("document.querySelector('.live-screen')?.naturalWidth === 480")
                box, image = page.locator(".live-screen-box"), page.locator(".live-screen")
                self.assertLessEqual(box.evaluate("e => e.scrollHeight - e.clientHeight"), 1,
                                     "100% frame is clipped by an inner vertical scroller")
                switch_label = ("Deslizar en la vista para mover la página" if language == "es"
                                else "Swipe on the view to scroll the page")
                swipe = page.get_by_label(switch_label)
                swipe.check()
                image.scroll_into_view_if_needed()
                rect = image.bounding_box()
                x = rect["x"] + rect["width"] * .5
                y = max(80, rect["y"] + min(280, rect["height"] * .7))
                args = {"pointerId": 1, "pointerType": "touch", "isPrimary": True, "button": 0,
                        "clientX": x, "clientY": y, "bubbles": True}
                image.dispatch_event("pointerdown", {**args, "buttons": 1})
                image.dispatch_event("pointermove", {**args, "buttons": 1, "clientY": y - 90})
                image.dispatch_event("pointerup", {**args, "buttons": 0, "clientY": y - 90})
                # A browser may synthesize a click after pointerup; it must not
                # accidentally activate the webpage following a swipe.
                image.dispatch_event("click", {"clientX": x, "clientY": y - 90, "bubbles": True})
                page.wait_for_function("window.__testLiveActions.some(x => x.action === 'scroll')")
                actions = page.evaluate("window.__testLiveActions")
                scroll = [item for item in actions if item.get("action") == "scroll"][-1]
                self.assertEqual(scroll["amount"], 240)
                self.assertTrue(0 <= scroll["x"] < 480 and 0 <= scroll["y"] < 760)
                self.assertFalse(any(item.get("action") == "click" for item in actions))
                page.wait_for_function("window.liveWeb.ready && !window.liveWeb.pending")
                swipe.uncheck()
                page.locator(".live-zoom").get_by_role("tab", name="150%").click()
                self.assertGreater(box.evaluate("e => e.scrollHeight - e.clientHeight"), 0)
                self.assertEqual(errors, [])

    def test_live_real_touch_swipe_and_tap_are_distinct(self) -> None:
        # Chromium touch events go through the browser's actual input pipeline;
        # the webpage/realtime peer is still a local synthetic fixture.
        with browser_fixture("es", {"width": 360, "height": 640}, touch=True) as (page, errors):
            install_live_frame_fixture(page)
            page.get_by_role("button", name="Iniciar navegador").click()
            page.wait_for_function("document.querySelector('.live-screen')?.naturalWidth === 480")
            page.locator(".live-screen-box:not([aria-busy])").wait_for()
            swipe = page.get_by_label("Deslizar en la vista para mover la página")
            swipe.check()
            image = page.locator(".live-screen")
            image.scroll_into_view_if_needed()
            rect = image.bounding_box()
            x, y = rect["x"] + rect["width"] * .5, min(490, max(220, rect["y"] + 250))
            cdp = page.context.new_cdp_session(page)
            cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y, "id": 0}]})
            for dy in (20, 45, 70, 100):
                cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x, "y": y - dy, "id": 0}]})
            cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
            page.wait_for_function("window.__testLiveActions.some(x => x.action === 'scroll')")
            page.wait_for_function("window.liveWeb.ready && !window.liveWeb.pending")
            page.locator(".live-screen-box:not([aria-busy])").wait_for()
            actions = page.evaluate("window.__testLiveActions")
            self.assertEqual(len([item for item in actions if item.get("action") == "scroll"]), 1)
            self.assertFalse(any(item.get("action") == "click" for item in actions))
            # This deliberate tap can follow quickly, even while the preceding
            # swipe's compatibility-click suppression is still active.
            page.touchscreen.tap(x, y - 30)
            page.wait_for_function("window.__testLiveActions.some(x => x.action === 'click')")
            page.wait_for_timeout(750)  # any synthesized click must not duplicate it
            self.assertEqual(len([item for item in page.evaluate("window.__testLiveActions") if item.get("action") == "click"]), 1)
            self.assertEqual(errors, [])

    def test_touch_tap_in_swipe_mode_needs_no_native_click_and_never_duplicates(self) -> None:
        # Test both mobile engines. The first deliberate tap emits pointer
        # events only, modelling browsers that omit the compatibility click.
        for engine in ("chromium", "webkit"):
            with self.subTest(engine=engine), browser_fixture("es", {"width": 360, "height": 640}, engine=engine, touch=True) as (page, errors):
                install_live_frame_fixture(page)
                page.get_by_role("button", name="Iniciar navegador").tap()
                page.wait_for_function("document.querySelector('.live-screen')?.naturalWidth === 480")
                page.locator(".live-screen-box:not([aria-busy])").wait_for()
                page.get_by_label("Deslizar en la vista para mover la página").check()
                image = page.locator(".live-screen")
                image.scroll_into_view_if_needed()
                rect = image.bounding_box()
                x, y = rect["x"] + rect["width"] / 2, min(480, max(200, rect["y"] + 200))
                pointer = {"pointerId": 31, "pointerType": "touch", "isPrimary": True, "button": 0,
                           "clientX": x, "clientY": y, "bubbles": True}
                # A stroke that returns to its origin is not a tap. Neither is
                # a gesture with a second finger or a cancelled touch.
                image.dispatch_event("pointerdown", {**pointer, "buttons": 1})
                image.dispatch_event("pointermove", {**pointer, "buttons": 1, "clientY": y - 70})
                image.dispatch_event("pointerup", {**pointer, "buttons": 0})
                image.dispatch_event("click", {"clientX": x, "clientY": y, "bubbles": True})
                image.dispatch_event("pointerdown", {**pointer, "buttons": 1})
                image.dispatch_event("pointerdown", {**pointer, "pointerId": 32, "isPrimary": False, "buttons": 1})
                image.dispatch_event("pointerup", {**pointer, "buttons": 0})
                image.dispatch_event("click", {"clientX": x, "clientY": y, "bubbles": True})
                image.dispatch_event("pointerdown", {**pointer, "buttons": 1})
                image.dispatch_event("pointercancel", {**pointer, "buttons": 0})
                image.dispatch_event("pointerup", {**pointer, "buttons": 0})
                self.assertFalse(any(item.get("action") in {"click", "scroll"} for item in page.evaluate("window.__testLiveActions")))
                image.dispatch_event("pointerdown", {**pointer, "buttons": 1})
                image.dispatch_event("pointerup", {**pointer, "buttons": 0})
                page.wait_for_function("window.__testLiveActions.filter(x => x.action === 'click').length === 1")
                # If the browser subsequently supplies a compatibility click,
                # it cannot turn the same tap into another remote action.
                image.dispatch_event("click", {"clientX": x, "clientY": y, "bubbles": True})
                self.assertEqual(len([x for x in page.evaluate("window.__testLiveActions") if x.get("action") == "click"]), 1)
                page.wait_for_function("window.liveWeb.ready && !window.liveWeb.pending")
                page.locator(".live-screen-box:not([aria-busy])").wait_for()
                image.scroll_into_view_if_needed()
                rect = image.bounding_box()
                x, y = rect["x"] + rect["width"] / 2, min(480, max(200, rect["y"] + 200))
                page.touchscreen.tap(x, y)
                page.wait_for_function("window.__testLiveActions.filter(x => x.action === 'click').length === 2")
                page.wait_for_timeout(750)
                self.assertEqual(len([x for x in page.evaluate("window.__testLiveActions") if x.get("action") == "click"]), 2)
                self.assertEqual(errors, [])

    def test_live_does_not_apply_a_press_started_while_a_frame_was_pending(self) -> None:
        with browser_fixture("es", {"width": 360, "height": 640}) as (page, errors):
            install_live_frame_fixture(page)
            page.get_by_role("button", name="Iniciar navegador").click()
            page.wait_for_function("document.querySelector('.live-screen')?.naturalWidth === 480")
            page.locator(".live-screen-box:not([aria-busy])").wait_for()
            page.evaluate("window.__testLiveDelay = 700")
            image = page.locator(".live-screen")
            image.scroll_into_view_if_needed()
            rect = image.bounding_box()
            x, y = rect["x"] + rect["width"] / 2, min(480, max(200, rect["y"] + 200))
            page.mouse.click(x, y)
            page.wait_for_function("!!window.liveWeb.pending")
            page.mouse.move(x + 20, y + 20)
            page.mouse.down()  # begins on the stale frame while the first click is pending
            page.wait_for_function("!window.liveWeb.pending")
            page.locator(".live-screen-box:not([aria-busy])").wait_for()
            page.mouse.up()  # must not become a second click on the new page
            page.wait_for_timeout(200)
            self.assertEqual(len([v for v in page.evaluate("window.__testLiveActions") if v.get("action") == "click"]), 1)
            page.mouse.click(x + 20, y + 20)
            page.wait_for_function("window.__testLiveActions.filter(x => x.action === 'click').length === 2")
            self.assertEqual(errors, [])

    def test_live_change_mode_requires_confirmation_and_restart(self) -> None:
        with browser_fixture("es", {"width": 360, "height": 640}) as (page, errors):
            install_live_frame_fixture(page)
            page.get_by_role("button", name="Iniciar navegador").click()
            page.wait_for_function("document.querySelector('.live-screen')?.naturalWidth === 480")
            human = page.get_by_role("tab", name="Gráfico temporal")
            self.assertFalse(human.is_disabled(), "mode change must not silently become unusable")
            human.click()
            page.get_by_role("button", name="Cancelar", exact=True).click()
            self.assertEqual(page.evaluate("window.liveWeb.browser"), "graphical_saved")
            self.assertFalse(page.evaluate("window.__testLiveActions.some(x => x.action === 'close')"))
            human.click()
            page.locator("#sheet .btn.danger").click()
            page.wait_for_function("window.__testLiveActions.some(x => x.action === 'close')")
            page.wait_for_function("window.liveWeb.browser === 'human' && !window.liveWeb.channel")
            self.assertEqual(human.get_attribute("aria-selected"), "true")
            self.assertTrue(page.get_by_role("button", name="Iniciar navegador").is_visible())
            page.get_by_role("button", name="Iniciar navegador").click()
            page.wait_for_function("window.__testLiveActions.some(x => x.t === 'start' && x.browser === 'human')")
            self.assertEqual(errors, [])

    def test_live_unverified_initial_address_explains_failure_bilingually(self) -> None:
        for language in ("es", "en"):
            with self.subTest(language=language), browser_fixture(language, {"width": 390, "height": 844}) as (page, errors):
                page.evaluate("""() => {
                  window.webxdc.joinRealtimeChannel = () => {
                    let listener;
                    return {setListener(fn) { listener = fn; }, send(data) {
                      const req = JSON.parse(new TextDecoder().decode(data));
                      if (req.t === 'start') setTimeout(() => listener(new TextEncoder().encode(JSON.stringify(
                        {v:1,sid:req.sid,t:'error',seq:0,error:'human_address_unready'}))), 50);
                    }, leave() {}};
                  };
                  go('live_web', {name:'Fixture', url:'https://fixture.example'});
                }""")
                page.get_by_role("tab", name="Gráfico temporal" if language == "es" else "Temporary graphical").click()
                start = page.get_by_role("button", name="Iniciar navegador" if language == "es" else "Start browser")
                start.click()
                message = "No se pudo comprobar la dirección" if language == "es" else "The address could not be verified"
                page.get_by_text(message, exact=False).wait_for()
                self.assertTrue(start.is_enabled())
                self.assertFalse(page.evaluate("!!window.liveWeb.channel"))
                self.assertEqual(errors, [])

    def test_live_mode_and_tap_in_iphone_sized_webkit(self) -> None:
        # WebKit with touch input approximates an iPhone WebView. It is not an
        # iOS device, and the realtime peer is intentionally a synthetic stub.
        with browser_fixture("es", {"width": 390, "height": 844}, engine="webkit", touch=True) as (page, errors):
            install_live_frame_fixture(page)
            page.get_by_role("button", name="Iniciar navegador").tap()
            page.wait_for_function("document.querySelector('.live-screen')?.naturalWidth === 480")
            page.locator(".live-screen-box:not([aria-busy])").wait_for()
            image = page.locator(".live-screen")
            image.scroll_into_view_if_needed()
            rect = image.bounding_box()
            page.touchscreen.tap(rect["x"] + rect["width"] / 2, min(650, max(220, rect["y"] + 200)))
            page.wait_for_function("window.__testLiveActions.some(x => x.action === 'click')")
            page.wait_for_function("window.liveWeb.ready && !window.liveWeb.pending")
            page.locator(".live-screen-box:not([aria-busy])").wait_for()
            human = page.get_by_role("tab", name="Gráfico temporal")
            self.assertFalse(human.is_disabled())
            human.tap()
            page.get_by_role("button", name="Cancelar", exact=True).tap()
            self.assertEqual(page.evaluate("window.liveWeb.browser"), "graphical_saved")
            human.tap()
            page.locator("#sheet .btn.danger").tap()
            page.wait_for_function("window.liveWeb.browser === 'human' && !window.liveWeb.channel")
            self.assertEqual(human.get_attribute("aria-selected"), "true")
            page.get_by_role("button", name="Iniciar navegador").tap()
            page.wait_for_function("window.__testLiveActions.some(x => x.t === 'start' && x.browser === 'human')")
            self.assertEqual(errors, [])

    def test_live_view_zoom_quality_navigation_and_search_with_simulated_realtime(self) -> None:
        # The bridge and picture below are deliberately simulated; the separate
        # sandbox integration verifies real Chromium screenshots and clicks.
        for language, engine in (("es", "chromium"), ("en", "chromium"), ("es", "webkit"), ("en", "webkit")):
            with self.subTest(language=language, engine=engine), browser_fixture(language, {"width": 390, "height": 844}, engine=engine) as (page, errors):
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
                self.assertEqual(page.evaluate("window.liveWeb.detail"), "economy")
                settings.click()
                auto_name = "Actualizar cada 5 s (usa datos; no es video)" if language == "es" else "Refresh every 5 s (uses data; not video)"
                self.assertTrue(page.get_by_label(auto_name).is_disabled())
                page.get_by_role("tab", name="Menos datos" if language == "es" else "Less data").click()
                self.assertEqual(page.evaluate("window.liveWeb.detail"), "economy")
                page.get_by_role("tab", name="Más nitidez" if language == "es" else "Sharper").click()
                settings.click()
                self.assertEqual(page.evaluate("window.liveWeb.browser"), "graphical_saved")
                mode = page.get_by_role("tab", name="Gráfico temporal" if language == "es" else "Temporary graphical")
                mode.click()
                self.assertEqual(page.evaluate("window.liveWeb.browser"), "human")
                self.assertTrue(page.get_by_text("no carga ni borra" if language == "es" else "does not load or delete", exact=False).is_visible())
                page.get_by_role("button", name="Iniciar navegador" if language == "es" else "Start browser").click()
                page.wait_for_function("document.querySelector('.live-screen')?.naturalWidth === 960")
                self.assertFalse(mode.is_disabled())  # changing mode now requires an explicit close confirmation
                self.assertEqual(mode.get_attribute("aria-selected"), "true")
                self.assertTrue(page.evaluate("window.__liveActions.some(x => x.t === 'start' && x.browser === 'human')"))
                self.assertFalse(page.get_by_role("button", name="Guardar sesión" if language == "es" else "Save session").is_visible())
                self.assertFalse(page.get_by_role("button", name="Descargar este video" if language == "es" else "Download this video").is_visible())
                close_window = page.get_by_role("button", name="Cerrar ventana actual" if language == "es" else "Close current window")
                self.assertTrue(close_window.is_visible())
                close_window.click()
                self.assertTrue(page.get_by_text("se perderá la sesión temporal" if language == "es" else "temporary session is lost", exact=False).is_visible())
                page.locator(".btn.danger").click()
                page.wait_for_function("window.__liveActions.some(x => x.action === 'key' && x.key === 'CloseWindow')")
                page.wait_for_function("window.liveWeb.ready && !window.liveWeb.pending")
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
                # Capture an actual pointer path on the zoomed view; the app must
                # send exactly one drag, never a synthetic second click.
                gesture_label = "Mantener o arrastrar en la vista" if language == "es" else "Hold or drag on the view"
                gesture = page.get_by_label(gesture_label, exact=False)
                gesture.check()
                before_clicks = page.evaluate("window.__liveActions.filter(x => x.action === 'click').length")
                screen.scroll_into_view_if_needed()
                bounds = page.locator(".live-screen-box").bounding_box()
                x, y = bounds["x"] + 35, bounds["y"] + 45
                page.mouse.move(x, y)
                page.mouse.down()
                page.mouse.move(x + 90, y + 12, steps=8)
                page.wait_for_timeout(120)
                page.mouse.up()
                page.wait_for_function("window.__liveActions.some(x => x.action === 'drag')")
                page.wait_for_function("window.liveWeb.ready && !window.liveWeb.pending")
                drag = page.evaluate("window.__liveActions.filter(x => x.action === 'drag').at(-1)")
                self.assertGreaterEqual(len(drag["points"]), 2)
                self.assertEqual(drag["points"][0]["ms"], 0)
                self.assertGreater(drag["points"][-1]["x"], drag["points"][0]["x"])
                self.assertGreater(drag["points"][-1]["ms"], 0)
                self.assertEqual(page.evaluate("window.__liveActions.filter(x => x.action === 'click').length"), before_clicks)
                self.assertIn("no es tu saldo" if language == "es" else "not your ETECSA balance", page.locator(".live-traffic").inner_text())
                gesture.uncheck()
                page.get_by_role("button", name="Tecla Espacio" if language == "es" else "Key Space").click()
                page.wait_for_function("window.__liveActions.some(x => x.action === 'key' && x.key === 'Space')")
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

    def test_copy_own_and_bot_messages_without_importing_history(self) -> None:
        for language in ("es", "en"):
            with self.subTest(language=language), browser_fixture(language, {"width": 390, "height": 844}) as (page, errors):
                page.evaluate("""() => {
                  window.__copied = [];
                  Object.defineProperty(navigator, 'clipboard', {configurable:true, value:{writeText: text => {
                    window.__copied.push(text); return Promise.resolve();
                  }}});
                }""")
                page.locator('.tab[data-tab="ai"]:visible').click()
                page.get_by_text("Agente" if language == "es" else "Agent", exact=True).first.click()
                original = "Mensaje nuevo de prueba: á, ñ, 123.\nSegunda línea."
                page.locator("#view textarea").fill(original)
                page.locator("#view .composer button.primary").click()
                page.get_by_text("SIMULATED agent answer [S1]").wait_for(state="visible")
                label = "Copiar" if language == "es" else "Copy"
                page.locator(".bubble.me").get_by_role("button", name=label).click()
                page.wait_for_function("window.__copied.length === 1")
                self.assertEqual(page.evaluate("window.__copied[0]"), original)
                page.locator(".bubble.bot:not(.pending)").get_by_role("button", name=label).click()
                page.wait_for_function("window.__copied.length === 2")
                self.assertEqual(page.evaluate("window.__copied[1]"), "SIMULATED agent answer [S1]")
                self.assertEqual(errors, [])

    def test_agent_modes_are_honest_and_bilingual(self) -> None:
        for language in ("es", "en"):
            viewport = {"width": 390, "height": 844} if language == "es" else {"width": 1280, "height": 900}
            with self.subTest(language=language), browser_fixture(language, viewport) as (page, errors):
                page.locator('.tab[data-tab="ai"]:visible').click()
                page.get_by_text("Agente" if language == "es" else "Agent", exact=True).first.click()
                self.assertTrue(page.get_by_role("tab", name="Herramientas / IA local" if language == "es" else "Tools / local AI").is_visible())
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
                page.get_by_role("tab", name="Herramientas / IA local" if language == "es" else "Tools / local AI").click()
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
