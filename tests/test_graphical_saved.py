"""Graphical savable Live integration, with synthetic sites and no real accounts.

The test-only copies of the isolated workers accept a disposable self-signed
fixture. Production workers must never receive that TLS exception.
"""
from __future__ import annotations

import base64
import http.server
import io
import json
import os
import select
import shutil
import socket
import ssl
import subprocess
import threading
import time
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import pytest
from PIL import Image

from michiflin import config, egress, live_web, media_worker, miniapp, sandbox, web_vault


def exchange(process, payload):
    process.stdin.write((json.dumps(payload) + "\n").encode())
    process.stdin.flush()
    assert select.select([process.stdout], [], [], 60)[0], "isolated worker timed out"
    return json.loads(process.stdout.readline(3_000_001))


def test_graphical_saved_bridge_keeps_legacy_and_temporary_modes_distinct(monkeypatch):
    old = live_web.Session(1, "a" * 32, "alice@example.org", "host:fixture.example", "https://fixture.example/", browser="controlled")
    temp = live_web.Session(2, "b" * 32, "alice@example.org", "host:fixture.example", "https://fixture.example/", browser="human")
    saved = live_web.Session(3, "c" * 32, "alice@example.org", "host:fixture.example", "https://fixture.example/", browser="graphical_saved")
    assert old.browser == "controlled" and temp.browser == "human" and saved.browser == "graphical_saved"
    assert temp._reply({"kind": "state", "storage": {"cookies": [], "origins": []}}, 1)["error"] == "human_no_saved_session"
    with mock.patch.object(saved, "_allowed", return_value=True), mock.patch.object(web_vault, "save") as vault_save:
        state = {"cookies": [], "origins": []}
        assert saved._reply({"kind": "state", "storage": state}, 2) == {"t": "saved", "seq": 2}
        vault_save.assert_called_once_with("alice@example.org", "host:fixture.example", state)
    with mock.patch.object(saved, "_queue_video", return_value={"t": "queued", "seq": 3}) as enqueue:
        assert saved._reply({"kind": "page_url", "url": "https://fixture.example/video"}, 3)["t"] == "queued"
        enqueue.assert_called_once_with("https://fixture.example/video", 3)


def test_new_graphical_mode_requires_the_build_that_discloses_it():
    sid = "d" * 32
    payload = {"v": 1, "sid": sid, "t": "start", "url": "https://fixture.example/", "browser": "graphical_saved"}
    with mock.patch.object(live_web, "_authorized", return_value=({"chat": 7, "build": "old"}, "alice@example.org")), \
         mock.patch.object(web_vault, "_secret", return_value=b"synthetic"), \
         mock.patch.object(miniapp, "app_build", return_value="current"), \
         mock.patch.object(live_web, "_send") as send, mock.patch.object(live_web.Session, "start") as start:
        live_web.receive(10, list(json.dumps(payload).encode()))
        assert send.call_args.args[2]["error"] == "unsupported"
        assert not start.called and 10 not in live_web._sessions
    with mock.patch.object(live_web, "_authorized", return_value=({"chat": 7, "build": "current"}, "alice@example.org")), \
         mock.patch.object(web_vault, "_secret", return_value=b"synthetic"), \
         mock.patch.object(miniapp, "app_build", return_value="current"), mock.patch.object(live_web.Session, "start") as start:
        try:
            live_web.receive(10, list(json.dumps(payload).encode()))
            assert live_web._sessions[10].browser == "graphical_saved" and start.called
        finally:
            with live_web._lock:
                live_web._sessions.pop(10, None)


@pytest.mark.skipif(os.getenv("MICHIFLIN_SANDBOX_TESTS") != "1", reason="requires namespaces and graphical Chromium")
def test_storage_roundtrip_legacy_graphical_and_legacy_offline(tmp_path, monkeypatch):
    if not sandbox.available() or not all(shutil.which(x) for x in ("Xvfb", "xdotool", "xclip", "openssl")):
        pytest.skip("graphical isolation dependencies missing")
    cert, key = tmp_path / "certificate.pem", tmp_path / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", str(key), "-out", str(cert),
                    "-days", "1", "-nodes", "-subj", "/CN=fixture.example"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=25)
    events = []
    paths = []
    event = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlsplit(self.path)
            paths.append(parsed.path)
            if parsed.path == "/event":
                events.append(parse_qs(parsed.query))
                event.set()
                body = b"ok"
            else:
                # Cookies, localStorage and IndexedDB are synthetic. The only
                # communication goes to the fixture listener through the jail's
                # test gateway; no third-party network connection is allowed.
                body = b'''<!doctype html><html><body style="margin:0;background:white">
                <div style="width:240px;height:100px;background:#12af31" onclick="document.cookie='fixture_session=second; path=/; Secure; SameSite=Lax'; localStorage.setItem('fixture-local','second'); fetch('/event?action=clicked')">Save fixture</div>
                <div style="width:240px;height:80px;background:#ff00ff" onclick="window.open('/popup','test-popup','popup,width=400,height=400')">Open popup</div>
                <script>
                (async () => {
                  const path = location.pathname;
                  if(path === '/seed') {
                    document.cookie='fixture_session=first; path=/; Secure; SameSite=Lax';
                    localStorage.setItem('fixture-local','first');
                  }
                  const request=indexedDB.open('fixture-db',1);
                  request.onupgradeneeded=()=>request.result.createObjectStore('values');
                  request.onsuccess=()=>{
                    const db=request.result;
                    if(path==='/seed') {
                      const tx=db.transaction('values','readwrite');
                      tx.objectStore('values').put('first','key');
                      tx.oncomplete=()=>fetch('/event?path='+path+'&cookie='+encodeURIComponent(document.cookie)+'&local='+encodeURIComponent(localStorage.getItem('fixture-local'))+'&idb=first');
                    } else {
                      const q=db.transaction('values').objectStore('values').get('key');
                      q.onsuccess=()=>fetch('/event?path='+path+'&cookie='+encodeURIComponent(document.cookie)+'&local='+encodeURIComponent(localStorage.getItem('fixture-local'))+'&idb='+encodeURIComponent(q.result||'missing'));
                    }
                  };
                })();
                </script></body></html>'''
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(str(cert), str(key))
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original_copy = sandbox.shutil.copyfile

    def connect(host, port, upstream=None):
        if host == "fixture.example" and port == 443 and upstream is None:
            return socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
        raise egress.EgressError("fixture_offline")

    def copy_fixture_worker(src, dst, *args, **kwargs):
        name = Path(src).name
        if name in {"remote_worker.py", "human_browser_worker.py"}:
            text = Path(src).read_text(encoding="utf-8")
            if name == "remote_worker.py":
                needle, replacement = '"device_scale_factor": 2,', '"device_scale_factor": 2, "ignore_https_errors": True,'
            else:
                needle, replacement = 'args = [binary, "--no-sandbox",', 'args = [binary, "--ignore-certificate-errors", "--no-sandbox",'
            assert needle in text
            Path(dst).write_text(text.replace(needle, replacement, 1), encoding="utf-8")
            return dst
        return original_copy(src, dst, *args, **kwargs)

    def wait_for_path(path):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            match = next((item for item in reversed(events) if item.get("path") == [path]), None)
            if match:
                return match
            event.wait(0.5)
            event.clear()
        pytest.fail("offline fixture did not report the expected path")

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "vault")
    monkeypatch.setenv("WEB_SESSION_KEY", "synthetic-unit-test-key-which-is-long-enough")
    monkeypatch.setattr(config, "WEB_PROXY", "")
    monkeypatch.setattr(config, "MEDIA_PROXY", "")
    try:
        with mock.patch.object(egress, "_connect", side_effect=connect), mock.patch.object(sandbox.shutil, "copyfile", side_effect=copy_fixture_worker):
            with sandbox.BrowserProcess("remote_worker.py") as worker:
                assert exchange(worker.process, {"url": "https://fixture.example/seed", "detail": "economy"})["kind"] == "frame"
                seeded = wait_for_path("/seed")
                assert seeded["local"] == ["first"] and seeded["idb"] == ["first"]
                legacy_state = exchange(worker.process, {"action": "save", "seq": 1})["storage"]
                assert legacy_state["cookies"] and legacy_state["origins"]
                assert any(origin.get("indexedDB") for origin in legacy_state["origins"])
            owner, site = "alice@example.org", "host:fixture.example"
            web_vault.save(owner, site, legacy_state)
            raw = web_vault._path(owner, site).read_bytes()
            assert b"fixture_session" not in raw and b"fixture-local" not in raw
            assert web_vault.load("bob@example.org", site) is None
            assert web_vault.load(owner, "host:other.example") is None
            with sandbox.BrowserProcess("human_browser_worker.py") as worker:
                first = exchange(worker.process, {"url": "https://fixture.example/restore", "detail": "economy", "hide_infobars": True,
                                                  "savable": True, "storage": web_vault.load(owner, site)})
                assert first["kind"] == "frame", first.get("error")
                restored = wait_for_path("/restore")
                assert restored["cookie"] == ["fixture_session=first"]
                assert restored["local"] == ["first"] and restored["idb"] == ["first"]
                # Screenshot is the actual graphical desktop, including its chrome.
                assert first["width"] == 480 and first["height"] == 760
                url = exchange(worker.process, {"action": "page_url", "seq": 1})
                assert url["kind"] == "page_url" and url["url"] == "https://fixture.example/restore"
                # A deliberate click inside the large green rectangle changes
                # the synthetic cookie and local storage through ordinary GUI input.
                assert exchange(worker.process, {"action": "click", "x": 90, "y": 185, "seq": 2})["kind"] == "frame"
                assert event.wait(8) or any(item.get("action") == ["clicked"] for item in events)
                state = exchange(worker.process, {"action": "save", "seq": 3})
                assert state["kind"] == "state", state.get("error")
                saved_state = state["storage"]
                assert any(c["name"] == "fixture_session" and c["value"] == "second" for c in saved_state["cookies"])
                assert any(o.get("indexedDB") for o in saved_state["origins"])
                # The existing private downloader consumes the same cookie jar;
                # its provider/network compatibility is not guaranteed here.
                jar = tmp_path / "synthetic-cookies.txt"
                assert media_worker.cookies_from(saved_state, str(jar)) >= 1
                assert "fixture_session\tsecond" in jar.read_text()
                jar.unlink()
                web_vault.save(owner, site, saved_state)
                # An open popup makes it ambiguous which video page was meant.
                # Locate the synthetic magenta button in the GUI frame instead
                # of assuming a particular height for the browser chrome.
                image = Image.open(io.BytesIO(base64.b64decode(first["image"]))).convert("RGB")
                matches = [(x, y) for y in range(image.height) for x in range(min(image.width, 250))
                           if (lambda r, g, b: r > 200 and g < 60 and b > 180)(*image.getpixel((x, y)))]
                assert len(matches) > 1000, "synthetic popup button not visible in GUI"
                x = (min(v[0] for v in matches) + max(v[0] for v in matches)) // 2
                y = (min(v[1] for v in matches) + max(v[1] for v in matches)) // 2
                assert exchange(worker.process, {"action": "click", "x": x, "y": y, "seq": 4})["kind"] == "frame"
                wait_for_path("/popup")
                ambiguous = exchange(worker.process, {"action": "page_url", "seq": 5})
                assert ambiguous.get("error") == "multiple_pages"
                assert exchange(worker.process, {"action": "key", "key": "CloseWindow", "seq": 6})["kind"] == "frame"
                assert exchange(worker.process, {"action": "page_url", "seq": 7})["url"] == "https://fixture.example/restore"
            with sandbox.BrowserProcess("remote_worker.py") as worker:
                assert exchange(worker.process, {"url": "https://fixture.example/back", "storage": web_vault.load(owner, site)})["kind"] == "frame"
                restored = wait_for_path("/back")
                assert restored["cookie"] == ["fixture_session=second"]
                assert restored["local"] == ["second"] and restored["idb"] == ["first"]
            with sandbox.BrowserProcess("human_browser_worker.py") as worker:
                assert exchange(worker.process, {"url": "https://fixture.example/temporary", "storage": saved_state})["kind"] == "frame"
                restored = wait_for_path("/temporary")
                assert restored["local"] == ["null"] and restored["idb"] == ["missing"]
                assert "fixture_session" not in restored.get("cookie", [""])[0]
                assert exchange(worker.process, {"action": "save", "seq": 1})["error"] == "human_no_saved_session"
        # The unmodified production graphical-saved worker rejects the same
        # self-signed certificate. Its Playwright connection is a private pipe,
        # never a TCP DevTools listener or exposed browser endpoint.
        paths.clear()
        with mock.patch.object(egress, "_connect", side_effect=connect), sandbox.BrowserProcess("human_browser_worker.py") as worker:
            result = exchange(worker.process, {"url": "https://fixture.example/untrusted", "savable": True, "detail": "economy"})
            assert result["kind"] == "frame"
            frontier, seen, chrome = [worker.process.pid], set(), []
            while frontier:
                pid = frontier.pop()
                if pid in seen:
                    continue
                seen.add(pid)
                try:
                    argv = (Path("/proc") / str(pid) / "cmdline").read_bytes()
                    children = []
                    for task in (Path("/proc") / str(pid) / "task").iterdir():
                        children.extend((task / "children").read_text().split())
                except OSError:
                    continue
                frontier.extend(map(int, children))
                if b"--remote-debugging-pipe" in argv and b"--type=" not in argv:
                    chrome.append(argv)
            assert chrome and all(b"--remote-debugging-port" not in cmd for cmd in chrome)
            assert all(b"--ignore-certificate-errors" not in cmd for cmd in chrome)
            time.sleep(2)
            assert "/untrusted" not in paths, "production worker accepted invalid TLS"
    finally:
        server.shutdown()
        server.server_close()
