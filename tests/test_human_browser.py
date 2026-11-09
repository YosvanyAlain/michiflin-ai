"""Graphical Live tests: local synthetic fixtures only, never real accounts.

The optional integration runs inside the production namespace/chroot. The
fixture is self-signed, so only a disposable copy of the test worker receives
an explicit TLS exception. A separate pass verifies the original rejects it.
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
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from PIL import Image

from michiflin import config, egress, human_browser_worker as human, sandbox


class HumanBrowserUnitTests(unittest.TestCase):
    def test_missing_gui_dependencies_fail_closed_without_starting_a_worker(self):
        with mock.patch.object(sandbox, "available", return_value=True), \
             mock.patch.object(sandbox.shutil, "which", return_value=None), \
             mock.patch.object(sandbox.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(sandbox.SandboxError, "human_unavailable"):
                sandbox.BrowserProcess("human_browser_worker.py")
            launch.assert_not_called()

    def test_scroll_targets_the_human_pointer_and_validates_before_moving(self):
        with mock.patch.object(human, "_xdotool") as pointer:
            human._scroll({"amount": 240, "x": 100, "y": 500})
            self.assertEqual(pointer.call_args_list, [mock.call("mousemove", "200", "1000"),
                                                      mock.call("click", "--repeat", "3", "--delay", "30", "5")])
        with mock.patch.object(human, "_xdotool") as pointer:
            human._scroll({"amount": -600})  # compatibility with older miniapps
            pointer.assert_called_once_with("click", "--repeat", "7", "--delay", "30", "4")
        for bad in ({"amount": 240, "x": 10}, {"amount": 240, "x": 480, "y": 3},
                    {"amount": 240, "x": True, "y": 3}, {"amount": "240", "x": 2, "y": 3}):
            with self.subTest(request=bad), mock.patch.object(human, "_xdotool") as pointer:
                with self.assertRaises(ValueError):
                    human._scroll(bad)
                pointer.assert_not_called()

    def test_gesture_validation_happens_before_pointer_down(self):
        samples = [None, [], [{"x": 1, "y": 2, "ms": 0}],
                   [{"x": 1, "y": 2, "ms": 1}, {"x": 1, "y": 2, "ms": 2}],
                   [{"x": 1, "y": 2, "ms": 0}, {"x": 480, "y": 2, "ms": 2}],
                   [{"x": 1, "y": 2, "ms": 0}, {"x": 1, "y": 2, "ms": 8001}]]
        for points in samples:
            with self.subTest(points=points), mock.patch.object(human, "_xdotool") as pointer:
                with self.assertRaises(ValueError):
                    human._drag(points)
                pointer.assert_not_called()

    def test_gesture_releases_button_if_movement_fails(self):
        calls = []
        def pointer(*args):
            calls.append(args)
            if args == ("mousemove", "20", "20"):
                raise RuntimeError("fixture")
        with mock.patch.object(human, "_xdotool", side_effect=pointer), self.assertRaises(RuntimeError):
            human._drag([{"x": 1, "y": 1, "ms": 0}, {"x": 10, "y": 10, "ms": 0}])
        self.assertEqual(calls[-1], ("mouseup", "1"))

    def test_text_uses_stdin_not_command_line_and_clipboard_owner_is_stopped(self):
        owner = mock.Mock()
        owner.poll.return_value = None
        text = "synthetic ñ secret"
        with mock.patch.object(human.subprocess, "Popen", return_value=owner) as launch, \
             mock.patch.object(human, "_xdotool") as keys, mock.patch.object(human.time, "sleep"), \
             mock.patch.object(human, "_stop") as stop:
            human._paste(text)
        self.assertNotIn(text, repr(launch.call_args))
        owner.stdin.write.assert_called_once_with(text.encode("utf-8"))
        owner.stdin.close.assert_called_once()
        keys.assert_called_once_with("key", "--clearmodifiers", "ctrl+v")
        stop.assert_called_once_with(owner)

    def test_clipboard_reader_never_puts_selection_in_arguments(self):
        address = b"https://fixture.example/?q=synthetic-secret"
        result = subprocess.CompletedProcess([], 0, stdout=address)
        with mock.patch.object(human.subprocess, "run", return_value=result) as run:
            self.assertEqual(human._read_clipboard(), address)
        self.assertNotIn(address.decode(), repr(run.call_args))
        self.assertEqual(run.call_args.kwargs["timeout"], 3)

    def test_prepared_address_accepts_only_safe_browser_normalizations(self):
        equivalent = [
            ("https://fixture.example", b"https://fixture.example/"),
            ("https://FIXTURE.EXAMPLE:443?q=hello%20world", b"https://fixture.example/?q=hello%20world"),
            ("https://fixture.example/caf\u00e9?q=pi\u00f1a", b"https://fixture.example/caf%C3%A9?q=pi%C3%B1a"),
            ("https://fixture.example/a%2fb", b"https://fixture.example/a%2Fb"),
        ]
        for original, copied in equivalent:
            with self.subTest(original=original):
                self.assertTrue(human._prepared_address_matches(original, copied))
        different = [
            ("https://fixture.example/?q=one", b"https://fixture.example/?q=two"),
            ("https://fixture.example/a%2Fb", b"https://fixture.example/a/b"),
            ("https://fixture.example/", b"https://attacker.example/"),
            ("https://fixture.example/", b"http://fixture.example/"),
            ("https://fixture.example/", b"https://fixture.example/?"),
            ("https://fixture.example/", b"https://fixture.example/#"),
            ("https://fixture.example/", b"\xff"),
        ]
        for original, copied in different:
            with self.subTest(original=original, copied=copied):
                self.assertFalse(human._prepared_address_matches(original, copied))

    def test_initial_address_is_verified_before_one_navigation(self):
        url = "https://fixture.example/caf%C3%A9?q=synthetic"
        with mock.patch.object(human, "_focused_window", return_value="10"), \
             mock.patch.object(human, "_xdotool") as keys, \
             mock.patch.object(human, "_paste") as paste, \
             mock.patch.object(human, "_read_clipboard", side_effect=[b"", url.encode()]), \
             mock.patch.object(human, "_clear_clipboard") as clear, \
             mock.patch.object(human.time, "sleep"):
            human._open_initial_address(url, "10")
        self.assertEqual(paste.call_count, 2)  # preparation, not navigation
        self.assertEqual(clear.call_count, 4)
        self.assertEqual(keys.call_args_list.count(mock.call("key", "--clearmodifiers", "Return")), 1)
        self.assertNotIn(url, repr(keys.call_args_list))

    def test_initial_address_fails_closed_without_submitting_if_unverified(self):
        url = "https://fixture.example/?token=synthetic"
        with mock.patch.object(human, "_focused_window", return_value="10"), \
             mock.patch.object(human, "_xdotool") as keys, \
             mock.patch.object(human, "_paste") as paste, \
             mock.patch.object(human, "_read_clipboard", return_value=b"about:blank"), \
             mock.patch.object(human, "_clear_clipboard") as clear, \
             mock.patch.object(human.time, "sleep"), self.assertRaisesRegex(ValueError, "address_unready"):
            human._open_initial_address(url, "10")
        self.assertEqual(paste.call_count, 6)
        self.assertEqual(clear.call_count, 12)
        self.assertNotIn(mock.call("key", "--clearmodifiers", "Return"), keys.call_args_list)
        self.assertNotIn(url, repr(keys.call_args_list))

    def test_initial_address_rejects_controls_before_any_keyboard_or_clipboard(self):
        with mock.patch.object(human, "_xdotool") as keys, \
             mock.patch.object(human, "_paste") as paste, \
             mock.patch.object(human, "_focused_window") as focus, \
             self.assertRaisesRegex(ValueError, "address_unready"):
            human._open_initial_address("https://fixture.example/?q=a\nb", "10")
        keys.assert_not_called()
        paste.assert_not_called()
        focus.assert_not_called()

    def test_initial_address_clears_clipboard_and_does_not_submit_if_focus_changes(self):
        url = "https://fixture.example/"
        with mock.patch.object(human, "_focused_window", side_effect=["10", "10", "20"]), \
             mock.patch.object(human, "_xdotool") as keys, \
             mock.patch.object(human, "_paste"), \
             mock.patch.object(human, "_read_clipboard", return_value=url.encode()), \
             mock.patch.object(human, "_clear_clipboard") as clear, \
             mock.patch.object(human.time, "sleep"), self.assertRaisesRegex(ValueError, "focus"):
            human._open_initial_address(url, "10")
        self.assertEqual(clear.call_count, 2)
        self.assertNotIn(mock.call("key", "--clearmodifiers", "Return"), keys.call_args_list)

    def test_root_focus_is_restored_only_for_one_unambiguous_browser(self):
        with mock.patch.object(human, "_focused_window", side_effect=["543", "10"]), \
             mock.patch.object(human, "_visible_windows", return_value=["10"]), \
             mock.patch.object(human, "_focus_window") as focus:
            self.assertEqual(human._restore_root_focus("543"), "10")
            focus.assert_called_once_with("10")
        with mock.patch.object(human, "_focused_window", return_value="543"), \
             mock.patch.object(human, "_visible_windows", return_value=["10", "20"]), \
             mock.patch.object(human, "_focus_window") as focus:
            self.assertEqual(human._restore_root_focus("543"), "543")
            focus.assert_not_called()
        with mock.patch.object(human, "_focused_window", return_value="999"), \
             mock.patch.object(human, "_visible_windows") as windows, \
             mock.patch.object(human, "_focus_window") as focus:
            self.assertEqual(human._restore_root_focus("543"), "999")
            windows.assert_not_called()
            focus.assert_not_called()

    def test_direct_addresses_are_https_and_have_no_embedded_credentials(self):
        self.assertTrue(human.valid_url("https://fixture.example/a?q=test"))
        for url in ("http://fixture.example", "https://user:pass@fixture.example", "https://fixture.example:444", "file:///work/worker.py"):
            self.assertFalse(human.valid_url(url))


@unittest.skipUnless(os.getenv("MICHIFLIN_SANDBOX_TESTS") == "1", "requires isolated Linux namespaces and a graphical Chromium")
class HumanBrowserSandboxTests(unittest.TestCase):
    def test_targeted_scroll_in_both_real_isolated_browsers(self):
        """Wheel a nested panel at the chosen point, then return it to the top."""
        if not all(shutil.which(name) for name in ("Xvfb", "xdotool", "xclip", "openssl")):
            self.skipTest("graphical test dependencies missing")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cert, key = root / "certificate.pem", root / "key.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", str(key), "-out", str(cert),
                            "-days", "1", "-nodes", "-subj", "/CN=fixture.example"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=25)
            events = []
            ready, scrolled, at_top = threading.Event(), threading.Event(), threading.Event()

            class Handler(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    if self.path.startswith("/event?"):
                        event = parse_qs(urlsplit(self.path).query)
                        events.append(event)
                        if "ready" in event:
                            ready.set()
                        if "panel" in event:
                            scrolled.set()
                            if event.get("panel") == ["right"] and float(event["top"][0]) == 0:
                                at_top.set()
                        body, kind = b"OK", "text/plain"
                    else:
                        body = b'''<!doctype html><html><body style="margin:0">
                          <div id="left" style="position:absolute;left:10px;top:10px;width:200px;height:320px;overflow:auto">
                            <div style="height:2000px;background:repeating-linear-gradient(red 0 200px,blue 200px 400px)"></div></div>
                          <div id="right" style="position:absolute;left:260px;top:10px;width:200px;height:320px;overflow:auto">
                            <div style="height:2000px;background:repeating-linear-gradient(red 0 200px,blue 200px 400px)"></div></div>
                          <script>for(const name of ['left','right']){const el=document.getElementById(name);
                            el.addEventListener('scroll',()=>fetch('/event?panel='+name+'&top='+el.scrollTop));}
                            fetch('/event?ready=1');</script></body></html>'''
                        kind = "text/html"
                    self.send_response(200); self.send_header("Content-Type", kind)
                    self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

                def log_message(self, *_args):
                    pass

            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); tls.load_cert_chain(str(cert), str(key))
            server.socket = tls.wrap_socket(server.socket, server_side=True)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            original_copy = sandbox.shutil.copyfile

            def connect(host, port, upstream=None):
                if host == "fixture.example" and port == 443 and upstream is None:
                    return socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
                raise egress.EgressError("fixture_offline")

            def copy_with_test_tls_exception(src, dst, *args, **kwargs):
                name = Path(src).name
                if name in {"remote_worker.py", "human_browser_worker.py"}:
                    text = Path(src).read_text(encoding="utf-8")
                    if name == "remote_worker.py":
                        needle, replacement = '"device_scale_factor": 2,', '"device_scale_factor": 2, "ignore_https_errors": True,'
                    else:
                        needle, replacement = 'args = [binary, "--no-sandbox",', 'args = [binary, "--ignore-certificate-errors", "--no-sandbox",'
                    self.assertIn(needle, text)
                    Path(dst).write_text(text.replace(needle, replacement, 1), encoding="utf-8")
                    return dst
                return original_copy(src, dst, *args, **kwargs)

            def exchange(process, payload):
                process.stdin.write((json.dumps(payload) + "\n").encode()); process.stdin.flush()
                self.assertTrue(select.select([process.stdout], [], [], 50)[0], "worker timed out")
                return json.loads(process.stdout.readline(3_000_001))

            try:
                with mock.patch.object(config, "WEB_PROXY", ""), mock.patch.object(config, "MEDIA_PROXY", ""), \
                     mock.patch.object(egress, "_connect", side_effect=connect), \
                     mock.patch.object(sandbox.shutil, "copyfile", side_effect=copy_with_test_tls_exception):
                    for worker_name in ("remote_worker.py", "human_browser_worker.py"):
                        with self.subTest(worker=worker_name):
                            events.clear(); ready.clear(); scrolled.clear(); at_top.clear()
                            with sandbox.BrowserProcess(worker_name) as worker:
                                first = exchange(worker.process, {"url": "https://fixture.example/", "detail": "economy", "hide_infobars": True})
                                self.assertEqual(first["kind"], "frame")
                                self.assertTrue(ready.wait(12), "fixture did not load")
                                # First put the pointer in the left panel. The next
                                # wheel action must move it to the right panel.
                                self.assertEqual(exchange(worker.process, {"action": "click", "x": 50, "y": 180, "seq": 1})["kind"], "frame")
                                down = exchange(worker.process, {"action": "scroll", "x": 350, "y": 180, "amount": 240, "seq": 2})
                                self.assertEqual(down["kind"], "frame")
                                if os.getenv("MICHIFLIN_DEBUG_FRAMES"):
                                    (Path(os.environ["MICHIFLIN_DEBUG_FRAMES"]) / (worker_name + "-first.jpg")).write_bytes(base64.b64decode(first["image"]))
                                    (Path(os.environ["MICHIFLIN_DEBUG_FRAMES"]) / (worker_name + "-down.jpg")).write_bytes(base64.b64decode(down["image"]))
                                self.assertTrue(scrolled.wait(5), "target did not scroll")
                                right = [float(e["top"][0]) for e in events if e.get("panel") == ["right"]]
                                self.assertTrue(right and max(right) > 0, "right panel stayed at the top")
                                self.assertFalse(any(e.get("panel") == ["left"] for e in events), "wrong panel scrolled")
                                before = Image.open(io.BytesIO(base64.b64decode(first["image"]))).convert("RGB")
                                after = Image.open(io.BytesIO(base64.b64decode(down["image"]))).convert("RGB")
                                changed = sum(
                                    sum(abs(a-b) for a, b in zip(before.getpixel((350, y)), after.getpixel((350, y)))) > 100
                                    for y in range(100, 300, 10)
                                )
                                self.assertGreater(changed, 1, "frame did not show the scroll")
                                scrolled.clear()
                                up = exchange(worker.process, {"action": "scroll", "x": 350, "y": 180, "amount": -240, "seq": 3})
                                self.assertEqual(up["kind"], "frame")
                                self.assertTrue(scrolled.wait(5))
                                self.assertTrue(at_top.wait(5), "right panel did not return to the top")
                                self.assertEqual(exchange(worker.process, {"action": "close", "seq": 4})["kind"], "closed")
            finally:
                server.shutdown(); server.server_close()

    def test_graphical_browser_interaction_and_tls_fail_closed(self):
        if not all(shutil.which(name) for name in ("Xvfb", "xdotool", "xclip", "openssl")):
            self.skipTest("graphical test dependencies missing")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cert, key = root / "certificate.pem", root / "key.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", str(key), "-out", str(cert),
                            "-days", "1", "-nodes", "-subj", "/CN=fixture.example"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=25)
            seen = []
            page_loaded = threading.Event()

            class Handler(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    seen.append(self.path)
                    if self.path.startswith("/event?"):
                        if "webdriver=" in self.path:
                            page_loaded.set()
                        body = b"OK"
                        content_type = "text/plain"
                    elif self.path == "/popup":
                        body = b'''<!doctype html><html><body onload="document.getElementById('p').focus()">
                          <input id="p" oninput="fetch('/event?popup_typed='+encodeURIComponent(this.value))">
                          <div style="width:250px;height:70px;background:#00ffff"
                               onclick="fetch('/event?popup_done=1');setTimeout(()=>window.close(),200)">Complete</div>
                          </body></html>'''
                        content_type = "text/html"
                    else:
                        body = b'''<!doctype html><html><body style="margin:0;background:white">
                          <div style="width:250px;height:70px;background:#00ff00" onclick="fetch('/event?clicked='+event.isTrusted)">Click</div>
                          <div style="width:250px;height:60px;background:#ff00ff;display:flex;align-items:center">
                            <input style="margin-left:20px;width:200px" type="range" value="0" oninput="fetch('/event?slide='+this.value)"></div>
                          <div style="width:250px;height:60px;background:#ff3000" onpointerdown="this.dataset.start=performance.now()"
                               onpointerup="fetch('/event?held='+Math.round(performance.now()-this.dataset.start))">Hold</div>
                          <div style="width:250px;height:60px;background:#0000ff;display:flex;align-items:center">
                            <input style="margin-left:20px;width:200px" oninput="fetch('/event?typed='+encodeURIComponent(this.value))"></div>
                          <div style="width:250px;height:60px;background:#ffff00"
                               onclick="window.open('/popup','fixture','popup,width=400,height=400')">Popup</div>
                          <script>fetch('/event?webdriver='+String(navigator.webdriver));</script></body></html>'''
                        content_type = "text/html"
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *_args):
                    pass

            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(str(cert), str(key))
            server.socket = tls.wrap_socket(server.socket, server_side=True)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            original_copy = sandbox.shutil.copyfile

            def connect(host, port, upstream=None):
                if host == "fixture.example" and port == 443 and upstream is None:
                    return socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
                # Do not let this test reach any internet service.
                raise egress.EgressError("fixture_offline")

            def copy_with_test_tls_exception(src, dst, *args, **kwargs):
                if Path(src).name == "human_browser_worker.py":
                    text = Path(src).read_text(encoding="utf-8")
                    needle = 'args = [binary, "--no-sandbox",'
                    self.assertIn(needle, text)
                    text = text.replace(needle, 'args = [binary, "--ignore-certificate-errors", "--no-sandbox",', 1)
                    # Simulate an address bar that did not accept the first
                    # preparation. No navigation should occur until verified.
                    marker = "def _read_clipboard() -> bytes:\n"
                    self.assertIn(marker, text)
                    text = text.replace(marker, "_fixture_reads = 0\n\n" + marker +
                                        "    global _fixture_reads\n    _fixture_reads += 1\n"
                                        "    if _fixture_reads == 1:\n        return b''\n", 1)
                    initial = '_open_initial_address(str(initial["url"]), main_window)'
                    self.assertIn(initial, text)
                    text = text.replace(initial, initial + "\n            if _read_clipboard():\n"
                                        "                raise ValueError('fixture_clipboard_not_cleared')", 1)
                    catch = ('except Exception:\n            # Do not submit an unverified address or expose it in the error.\n'
                             '            emit({"kind": "error", "error": "human_address_unready"})')
                    diagnostic = ('except Exception as exc:\n'
                                  '            emit({"kind": "error", "error": "human_address_unready", '
                                  '"fixture_error": str(exc) if isinstance(exc, ValueError) else type(exc).__name__})')
                    self.assertIn(catch, text)
                    text = text.replace(catch, diagnostic, 1)
                    Path(dst).write_text(text, encoding="utf-8")
                    return dst
                return original_copy(src, dst, *args, **kwargs)

            def exchange(process, payload):
                process.stdin.write((json.dumps(payload) + "\n").encode())
                process.stdin.flush()
                self.assertTrue(select.select([process.stdout], [], [], 45)[0], "worker timed out")
                return json.loads(process.stdout.readline(3_000_001))

            def descendants(pid):
                # Only inspect descendants of this disposable worker. No other
                # host processes, command output or browser data are collected.
                result, pending = set(), [pid]
                while pending:
                    current = pending.pop()
                    try:
                        tasks = list((Path("/proc") / str(current) / "task").iterdir())
                    except OSError:
                        continue
                    for task in tasks:
                        try:
                            children = (task / "children").read_text().split()
                        except OSError:
                            continue
                        for child in children:
                            child_pid = int(child)
                            if child_pid not in result:
                                result.add(child_pid)
                                pending.append(child_pid)
                return result

            def color_bounds(image, predicate, after=0):
                pixels = image.convert("RGB")
                matches = []
                # All fixture rectangles are in the left 250 CSS pixels.
                for y in range(after, pixels.height):
                    for x in range(min(pixels.width, 510)):
                        if predicate(*pixels.getpixel((x, y))):
                            matches.append((x, y))
                self.assertGreater(len(matches), 1000, "fixture rectangle not visible")
                xs, ys = zip(*matches)
                return min(xs), min(ys), max(xs), max(ys)

            try:
                with mock.patch.object(config, "WEB_PROXY", ""), mock.patch.object(config, "MEDIA_PROXY", ""), \
                     mock.patch.object(egress, "_connect", side_effect=connect):
                    with mock.patch.object(sandbox.shutil, "copyfile", side_effect=copy_with_test_tls_exception), \
                         sandbox.BrowserProcess("human_browser_worker.py") as worker:
                        process = worker.process
                        first = exchange(process, {"url": "https://fixture.example?probe=%C3%B1%20one", "detail": "sharp", "hide_infobars": True})
                        self.assertEqual(first["kind"], "frame", f"initial fixture status: {first.get('fixture_error', first.get('error', ''))}")
                        self.assertTrue(page_loaded.wait(12))
                        self.assertEqual(seen.count("/?probe=%C3%B1%20one"), 1, "initial preparation repeated navigation")
                        children = descendants(process.pid)
                        chrome_pids, chrome_main = [], []
                        for pid in children:
                            try:
                                argv = (Path("/proc") / str(pid) / "cmdline").read_bytes()
                            except OSError:
                                continue
                            if b"--user-data-dir=/tmp/human-browser-profile" in argv:
                                self.assertNotIn(b"fixture.example", argv, "address leaked in browser argv")
                                chrome_pids.append(pid)
                                if b"--type=" not in argv:
                                    chrome_main.append(argv)
                        self.assertTrue(chrome_pids, "isolated graphical browser is not running")
                        self.assertTrue(chrome_main and all(b"--disable-infobars" in argv for argv in chrome_main),
                                        "current app's explicit infobar setting was not applied")
                        mountinfo = (Path("/proc") / str(chrome_pids[0]) / "mountinfo").read_text()
                        self.assertTrue(any(line.split()[4] == "/tmp" and " - tmpfs " in line for line in mountinfo.splitlines()),
                                        "graphical profile is not on the isolated tmpfs")
                        frame = exchange(process, {"action": "frame", "seq": 1, "detail": "sharp"})
                        self.assertEqual(frame["kind"], "frame")
                        image = Image.open(io.BytesIO(base64.b64decode(frame["image"])))
                        self.assertEqual(image.size, (960, 1520))
                        green = color_bounds(image, lambda r, g, b: g > 180 and r < 65 and b < 65)
                        magenta = color_bounds(image, lambda r, g, b: r > 180 and b > 180 and g < 65, after=green[3])
                        orange = color_bounds(image, lambda r, g, b: r > 180 and 20 < g < 100 and b < 65, after=magenta[3])
                        blue = color_bounds(image, lambda r, g, b: b > 180 and r < 65 and g < 65, after=orange[3])
                        yellow = color_bounds(image, lambda r, g, b: r > 180 and g > 180 and b < 65, after=blue[3])
                        center = lambda rect: ((rect[0] + rect[2]) // 4, (rect[1] + rect[3]) // 4)
                        x, y = center(green)
                        self.assertEqual(exchange(process, {"action": "click", "x": x, "y": y, "seq": 2})["kind"], "frame")
                        sx, sy = center(magenta)
                        self.assertEqual(exchange(process, {"action": "drag", "seq": 3, "points": [
                            {"x": sx - 90, "y": sy, "ms": 0}, {"x": sx, "y": sy, "ms": 100},
                            {"x": sx + 90, "y": sy, "ms": 200}]})["kind"], "frame")
                        hx, hy = center(orange)
                        self.assertEqual(exchange(process, {"action": "drag", "seq": 4, "points": [
                            {"x": hx, "y": hy, "ms": 0}, {"x": hx, "y": hy, "ms": 220}]})["kind"], "frame")
                        tx, ty = center(blue)
                        self.assertEqual(exchange(process, {"action": "click", "x": tx, "y": ty, "seq": 5})["kind"], "frame")
                        self.assertEqual(exchange(process, {"action": "text", "text": "Año ñ 123", "seq": 6})["kind"], "frame")
                        px, py = center(yellow)
                        self.assertEqual(exchange(process, {"action": "click", "x": px, "y": py, "seq": 7})["kind"], "frame")
                        self.assertIn("/popup", seen)
                        # In a popup, Ctrl+L need not select an address bar. A
                        # direct goto must refuse rather than typing into its form.
                        blocked = exchange(process, {"action": "goto", "url": "https://fixture.example/new", "seq": 8})
                        self.assertEqual(blocked["error"], "human_close_popup_first")
                        self.assertFalse(any("popup_typed=" in path for path in seen))
                        self.assertEqual(exchange(process, {"action": "key", "key": "CloseWindow", "seq": 9})["kind"], "frame")
                        popup = exchange(process, {"action": "click", "x": px, "y": py, "seq": 10})
                        self.assertEqual(popup["kind"], "frame")
                        popup_image = Image.open(io.BytesIO(base64.b64decode(popup["image"])))
                        cyan = color_bounds(popup_image, lambda r, g, b: r < 65 and g > 180 and b > 180)
                        cx, cy = center(cyan)
                        self.assertEqual(exchange(process, {"action": "click", "x": cx, "y": cy, "seq": 11})["kind"], "frame")
                        self.assertIn("/event?popup_done=1", seen)
                        # A popup that closes itself can leave focus on the X
                        # root; the one remaining browser is restored safely.
                        self.assertEqual(exchange(process, {"action": "goto", "url": "https://fixture.example/new", "seq": 12})["kind"], "frame")
                        self.assertEqual(exchange(process, {"action": "save", "seq": 13})["error"], "human_no_saved_session")
                        self.assertEqual(exchange(process, {"action": "page_url", "seq": 14})["error"], "human_no_saved_session")
                        self.assertEqual(exchange(process, {"action": "close", "seq": 15})["kind"], "closed")
                        jail = worker.root
                    self.assertFalse(jail.exists())
                    deadline = time.monotonic() + 5
                    while any((Path("/proc") / str(pid)).exists() for pid in chrome_pids) and time.monotonic() < deadline:
                        time.sleep(0.1)
                    self.assertFalse(any((Path("/proc") / str(pid)).exists() for pid in chrome_pids), "browser survived session close")
                    data = [parse_qs(urlsplit(path).query) for path in seen if path.startswith("/event?")]
                    self.assertTrue(any(item.get("webdriver") == ["false"] for item in data))
                    self.assertTrue(any(item.get("clicked") == ["true"] for item in data))
                    self.assertTrue(any(int(item["slide"][0]) > 70 for item in data if "slide" in item))
                    self.assertTrue(any(int(item["held"][0]) >= 180 for item in data if "held" in item))
                    self.assertTrue(any(item.get("typed") == ["Año ñ 123"] for item in data))
                    self.assertIn("/new", seen)

                    # The original production worker must *not* load the same
                    # self-signed fixture or inherit the test-only TLS exception.
                    seen.clear()
                    page_loaded.clear()
                    with sandbox.BrowserProcess("human_browser_worker.py") as worker:
                        first = exchange(worker.process, {"url": "https://fixture.example/", "detail": "economy"})
                        self.assertEqual(first["kind"], "frame")
                        argv_values = []
                        for pid in descendants(worker.process.pid):
                            try:
                                argv = (Path("/proc") / str(pid) / "cmdline").read_bytes()
                            except OSError:
                                continue
                            if b"--user-data-dir=/tmp/human-browser-profile" in argv:
                                argv_values.append(argv)
                        self.assertTrue(argv_values)
                        self.assertTrue(all(b"--disable-infobars" not in argv for argv in argv_values),
                                        "older app's warning was hidden")
                        self.assertFalse(page_loaded.wait(3), "original worker accepted invalid certificate")
                        # Closing the last GUI window ends the session cleanly.
                        self.assertEqual(exchange(worker.process, {"action": "key", "key": "CloseWindow", "seq": 1})["kind"], "closed")
                    self.assertFalse(any(path == "/" or path.startswith("/event?") for path in seen))
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
