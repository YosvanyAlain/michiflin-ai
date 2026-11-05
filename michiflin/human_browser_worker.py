"""Optional manually operated graphical browser in the existing private jail.

No WebDriver, CDP, page scripts or CAPTCHA solver are used. GUI events are
limited to the authenticated person's requests, over stdin/stdout. The browser
profile and clipboard live on the jail's tmpfs, not the host filesystem.
"""
from __future__ import annotations

import base64
import ctypes
import io
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from PIL import Image, ImageGrab
from playwright.sync_api import sync_playwright

WIDTH, HEIGHT, SCALE = 480, 760, 2
MAX_FRAME = 87_000
MAX_LINE = 3_000_000
MAX_GESTURE_POINTS = 64
MAX_GESTURE_MS = 8_000
DISPLAY = ":87"
HOME = Path("/tmp/human-browser-home")
PROFILE = Path("/tmp/human-browser-profile")
ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(HOME), "TMPDIR": "/tmp", "LANG": "C.UTF-8",
       "XDG_CONFIG_HOME": str(HOME / "config"), "XDG_CACHE_HOME": str(HOME / "cache"),
       "XDG_DATA_HOME": str(HOME / "data"), "DISPLAY": DISPLAY}


def emit(value: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, separators=(",", ":"), ensure_ascii=True) + "\n")
    sys.stdout.flush()


def read() -> dict[str, Any] | None:
    raw = sys.stdin.buffer.readline(MAX_LINE + 1)
    if not raw or len(raw) > MAX_LINE:
        return None
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (ValueError, UnicodeError):
        return None


def valid_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return bool(parsed.scheme == "https" and parsed.hostname and parsed.port in (None, 443)
                    and parsed.username is None and parsed.password is None and len(url) <= 4096)
    except ValueError:
        return False


def _process(argv: list[str]) -> subprocess.Popen:
    return subprocess.Popen(argv, env=ENV, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)


def _stop(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _xdotool(*args: str) -> None:
    # Arguments here are only an allowlisted key or bounded numeric coordinates;
    # typed text is never put in the process command line.
    subprocess.run(["xdotool", *args], env=ENV, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, check=True, timeout=8)


def _root_window() -> str:
    # Xvfb has no window manager; focus may revert to the X root after an
    # ordinary sign-in popup closes itself. Ask X11 for that root, not a guess.
    library = ctypes.CDLL("libX11.so.6")
    library.XOpenDisplay.argtypes = [ctypes.c_char_p]
    library.XOpenDisplay.restype = ctypes.c_void_p
    library.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    library.XDefaultRootWindow.restype = ctypes.c_ulong
    library.XCloseDisplay.argtypes = [ctypes.c_void_p]
    handle = library.XOpenDisplay(DISPLAY.encode("ascii"))
    if not handle:
        raise ValueError("display")
    try:
        return str(library.XDefaultRootWindow(handle))
    finally:
        library.XCloseDisplay(handle)


def _focused_window() -> str:
    result = subprocess.run(["xdotool", "getwindowfocus"], env=ENV, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, check=True, timeout=3)
    return result.stdout.strip()


def _visible_windows() -> list[str]:
    result = subprocess.run(["xdotool", "search", "--onlyvisible", "--class", "chromium"], env=ENV,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=3)
    return [item for item in result.stdout.splitlines() if item.isdecimal()] if result.returncode == 0 else []


def _focus_window(window: str) -> None:
    _xdotool("windowraise", window)
    _xdotool("windowfocus", "--sync", window)


def _restore_root_focus(root: str) -> str:
    focused = _focused_window()
    if focused == root:
        windows = _visible_windows()
        # Never guess between multiple windows or interrupt a focused dialog.
        if len(windows) == 1:
            _focus_window(windows[0])
            focused = _focused_window()
    return focused


def _paste(text: object) -> None:
    if not isinstance(text, str) or len(text) > 4096 or "\x00" in text:
        raise ValueError("text")
    # A foreground selection owner serves a temporary, in-memory clipboard.
    # There is no secret in argv, logs, files, environment, or status updates.
    owner = subprocess.Popen(["xclip", "-selection", "clipboard", "-in", "-quiet"], env=ENV,
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    try:
        if owner.stdin is None:
            raise ValueError("clipboard")
        owner.stdin.write(text.encode("utf-8"))
        owner.stdin.close()
        time.sleep(0.2)
        if owner.poll() is not None:
            raise ValueError("clipboard")
        _xdotool("key", "--clearmodifiers", "ctrl+v")
        # Give the browser time to request the clipboard, then destroy its owner.
        time.sleep(0.25)
    finally:
        _stop(owner)


def _point(point: Any) -> tuple[int, int, int]:
    if not isinstance(point, dict):
        raise ValueError("point")
    x, y, ms = point.get("x"), point.get("y"), point.get("ms")
    if (type(x) is not int or type(y) is not int or type(ms) is not int
            or not 0 <= x < WIDTH or not 0 <= y < HEIGHT or not 0 <= ms <= MAX_GESTURE_MS):
        raise ValueError("point")
    return x, y, ms


def _drag(points: Any) -> None:
    if not isinstance(points, list) or not 2 <= len(points) <= MAX_GESTURE_POINTS:
        raise ValueError("gesture")
    path = [_point(point) for point in points]
    if path[0][2] != 0 or any(b[2] < a[2] for a, b in zip(path, path[1:])):
        raise ValueError("gesture")
    _xdotool("mousemove", str(path[0][0] * SCALE), str(path[0][1] * SCALE))
    _xdotool("mousedown", "1")
    try:
        started = time.monotonic()
        for x, y, ms in path[1:]:
            delay = ms / 1000 - (time.monotonic() - started)
            if delay > 0:
                time.sleep(delay)
            _xdotool("mousemove", str(x * SCALE), str(y * SCALE))
    finally:
        _xdotool("mouseup", "1")


def _screenshot(detail: str) -> dict[str, Any]:
    time.sleep(0.3)
    image = ImageGrab.grab(xdisplay=DISPLAY)
    if image.size != (WIDTH * SCALE, HEIGHT * SCALE):
        raise ValueError("screen")
    scales = ("device", "css") if detail != "economy" else ("css",)
    qualities = (35, 25, 15, 10) if detail == "economy" else (60, 45, 30, 18)
    for scale in scales:
        view = image if scale == "device" else image.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
        for quality in qualities:
            output = io.BytesIO()
            view.save(output, format="JPEG", quality=quality)
            data = output.getvalue()
            if len(data) <= MAX_FRAME:
                return {"kind": "frame", "image": base64.b64encode(data).decode("ascii"),
                        "width": WIDTH, "height": HEIGHT, "rendered": "sharp" if scale == "device" else "economy"}
    return {"kind": "error", "error": "frame_too_large"}


def main() -> int:
    initial = read()
    if not initial or not valid_url(str(initial.get("url") or "")):
        emit({"kind": "error", "error": "bad_url"})
        return 1
    if not all(shutil.which(name, path=ENV["PATH"]) for name in ("Xvfb", "xdotool", "xclip")):
        emit({"kind": "error", "error": "human_unavailable"})
        return 1
    HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    PROFILE.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Only locate the vendor browser here; do not launch it through Playwright.
    with sync_playwright() as playwright:
        binary = playwright.chromium.executable_path
    if not Path(binary).is_file():
        emit({"kind": "error", "error": "human_unavailable"})
        return 1
    display = _process(["Xvfb", DISPLAY, "-screen", "0", f"{WIDTH*SCALE}x{HEIGHT*SCALE}x24", "-nolisten", "tcp", "-ac"])
    browser: subprocess.Popen | None = None
    try:
        for _ in range(100):
            if display.poll() is not None:
                raise ValueError("display")
            if Path("/tmp/.X11-unix/X87").exists():
                break
            time.sleep(0.1)
        else:
            raise ValueError("display")
        # The private network namespace has only this vetted local CONNECT
        # gateway. No direct or bypass connection to the host is available.
        args = [binary, "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--no-first-run",
                "--no-default-browser-check", "--disable-background-networking", "--disable-sync",
                "--disable-quic", "--proxy-server=http://127.0.0.1:18080", "--proxy-bypass-list=<-loopback>",
                "--user-data-dir=" + str(PROFILE), "--window-size=480,760", "--window-position=0,0",
                "--force-device-scale-factor=2", "--new-window", "about:blank"]
        browser = _process(args)
        # The desktop is not exposed as VNC or a host TCP port. Wait briefly
        # for a visible Chromium window before sending the first frame.
        for _ in range(120):
            if browser.poll() is not None:
                raise ValueError("browser")
            windows = _visible_windows()
            if windows:
                main_window = windows[-1]
                break
            time.sleep(0.1)
        else:
            raise ValueError("browser")
        # Do not expose the entered URL or its query string in process argv.
        # Refuse to paste if the expected normal window is not focused.
        root_window = _root_window()
        if _restore_root_focus(root_window) != main_window:
            raise ValueError("focus")
        _xdotool("key", "--clearmodifiers", "ctrl+l")
        _paste(initial["url"])
        _xdotool("key", "--clearmodifiers", "Return")
        time.sleep(1)
        requested_detail = initial.get("detail")
        detail = requested_detail if isinstance(requested_detail, str) and requested_detail in {"sharp", "economy"} else "economy"
        emit(_screenshot(str(detail)))
        while True:
            request = read()
            if not request:
                break
            action = str(request.get("action") or "")
            seq = request.get("seq")
            requested_detail = request.get("detail")
            if isinstance(requested_detail, str) and requested_detail in {"sharp", "economy"}:
                detail = requested_detail
            try:
                if browser.poll() is not None:
                    emit({"kind": "error", "seq": seq, "error": "browser_failed"})
                    break
                if action == "close":
                    emit({"kind": "closed", "seq": seq})
                    break
                if action in {"save", "page_url"}:
                    emit({"kind": "error", "seq": seq, "error": "human_no_saved_session"})
                    continue
                if action in {"click", "drag", "text", "key", "scroll", "back", "forward", "reload", "goto"}:
                    # If a provider just closed its own popup, restore the only
                    # remaining window after X11 focus reverted to the root.
                    _restore_root_focus(root_window)
                if action == "capture":
                    image = ImageGrab.grab(xdisplay=DISPLAY)
                    output = io.BytesIO()
                    image.save(output, format="JPEG", quality=60)
                    data = output.getvalue()
                    if len(data) > 1_000_000:
                        raise ValueError("capture")
                    emit({"kind": "capture", "seq": seq, "image": base64.b64encode(data).decode("ascii"), "height": HEIGHT})
                    continue
                if action == "click":
                    x, y, _ = _point({"x": request.get("x"), "y": request.get("y"), "ms": 0})
                    _xdotool("mousemove", str(x*SCALE), str(y*SCALE), "click", "1")
                elif action == "drag":
                    _drag(request.get("points"))
                elif action == "text":
                    _paste(request.get("text"))
                elif action == "key":
                    keys = {"Enter": "Return", "Tab": "Tab", "Shift+Tab": "shift+Tab", "Backspace": "BackSpace",
                            "Delete": "Delete", "Escape": "Escape", "Space": "space", "ArrowDown": "Down",
                            "ArrowUp": "Up", "ArrowLeft": "Left", "ArrowRight": "Right", "Home": "Home", "End": "End",
                            "CloseWindow": "ctrl+w"}
                    key = request.get("key")
                    if not isinstance(key, str) or key not in keys:
                        raise ValueError("key")
                    _xdotool("key", "--clearmodifiers", keys[key])
                    if key == "CloseWindow":
                        time.sleep(0.4)
                        visible = _visible_windows()
                        if browser.poll() is not None or not visible:
                            emit({"kind": "closed", "seq": seq})
                            break
                        # Xvfb has no desktop window manager to restore focus
                        # after a popup closes; bring the remaining browser back.
                        _focus_window(main_window if main_window in visible else visible[-1])
                elif action == "scroll":
                    amount = request.get("amount")
                    if type(amount) is not int or amount not in {-600, -240, 240, 600}:
                        raise ValueError("scroll")
                    _xdotool("click", "--repeat", "7" if abs(amount) == 600 else "3", "--delay", "30", "4" if amount < 0 else "5")
                elif action in {"back", "forward", "reload"}:
                    _xdotool("key", "--clearmodifiers", {"back": "alt+Left", "forward": "alt+Right", "reload": "ctrl+r"}[action])
                elif action == "goto":
                    # The outer bridge already restricts direct addresses to
                    # the service selected for this session. Keep HTTPS checks
                    # here too; the gateway still rejects non-public targets.
                    url = str(request.get("url") or "")
                    if not valid_url(url):
                        raise ValueError("url")
                    # Popup windows often have no editable address bar. Pasting
                    # there could instead type the URL into a login field or
                    # submit a form, so refuse until the main window has focus.
                    if _focused_window() != main_window:
                        emit({"kind": "error", "seq": seq, "error": "human_close_popup_first"})
                        continue
                    _xdotool("key", "--clearmodifiers", "ctrl+l")
                    _paste(url)
                    _xdotool("key", "--clearmodifiers", "Return")
                elif action != "frame":
                    raise ValueError("action")
                result = _screenshot(str(detail))
                result["seq"] = seq
                emit(result)
            except Exception:
                try:
                    result = _screenshot(str(detail))
                    result.update(seq=seq, warning="action_failed")
                    emit(result)
                except Exception:
                    emit({"kind": "error", "seq": seq, "error": "action_failed"})
    finally:
        _stop(browser)
        _stop(display)
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        # Never print a traceback containing URLs, input, or provider data.
        try:
            emit({"kind": "error", "error": "human_unavailable"})
        except Exception:
            pass
        code = 1
    raise SystemExit(code)
