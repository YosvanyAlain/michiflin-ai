"""Ephemeral WebXDC remote-browser bridge for a single authorized encrypted chat.

Realtime RPC events do not identify a sender. Accordingly, only a bot-sent,
current-build instance explicitly bound to an authorized individual encrypted
chat is accepted. No private browser data goes through persistent updates.
"""
from __future__ import annotations

import base64
import io
import json
import os
import queue
import re
import select
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from . import access, admin, chat, config, sandbox, state, web_vault

MAX_MESSAGE = 16_000
MAX_RESPONSE = 125_000
MAX_SESSIONS = 2
IDLE_SECONDS = 150
LIFETIME_SECONDS = 600
SESSION_ID = re.compile(r"^[0-9a-f]{32}$")
_lock = threading.RLock()
_sessions: dict[int, Session] = {}
_advertised: dict[int, float] = {}


def _authorized(instance: int) -> tuple[dict[str, Any], str] | None:
    # Delayed imports avoid a cycle with miniapp event registration.
    from . import miniapp
    with state.lock:
        original = miniapp._instance(instance)
        entry = dict(original) if original else None
    if not entry or entry.get("build") != miniapp.app_build() or not access.bound_allowed(entry):
        return None
    owner = str(entry.get("addr") or "").strip().casefold()
    if not owner or not admin.is_allowed(owner) or not config.service_access_allowed(owner) or state.get_kill_switch(owner):
        return None
    try:
        rpc, account = chat.rpc(), chat.account()
        message = rpc.get_message(account, instance)
        if not message or int(message.chat_id) != int(entry["chat"]) or int(message.from_id) != 1:
            return None
        info = rpc.get_basic_chat_info(account, int(entry["chat"]))
        kind = getattr(info.chat_type, "value", info.chat_type)
        if str(kind) != "Single" or not info.is_encrypted or info.is_contact_request or info.is_self_talk:
            return None
        contacts = rpc.get_chat_contacts(account, int(entry["chat"]))
        if len(contacts) != 1:
            return None
        actual = str(rpc.get_contact(account, int(contacts[0])).address or "").strip().casefold()
        if actual != owner:
            return None
    except Exception:
        return None
    return entry, owner


def _same_site_url(url: Any, site: str) -> bool:
    """Direct address-bar navigation stays in the session's service container.

    Provider-driven redirects and login pop-ups can still visit other domains;
    the encrypted state may therefore also contain their cookies.
    """
    if not isinstance(url, str) or len(url) > 4096:
        return False
    try:
        return web_vault.site_of(url) == site
    except web_vault.VaultError:
        return False


def _send(instance: int, sid: str, message: dict[str, Any]) -> bool:
    if _authorized(instance) is None:
        return False
    try:
        raw = json.dumps({"v": 1, "sid": sid, **message}, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_RESPONSE:
            return False
        chat.rpc().send_webxdc_realtime_data(chat.account(), instance, list(raw))
        return True
    except Exception:
        return False


def advertisement(instance: int) -> None:
    if _authorized(instance) is None:
        return
    now = time.monotonic()
    with _lock:
        if now - _advertised.get(instance, 0) < 5:
            return
        _advertised[instance] = now
    try:
        chat.rpc().send_webxdc_realtime_advertisement(chat.account(), instance)
    except Exception:
        pass


def receive(instance: int, values: list[int]) -> None:
    if len(values) > MAX_MESSAGE or _authorized(instance) is None:
        return
    try:
        body = json.loads(bytes(values).decode("utf-8"))
    except (ValueError, TypeError, UnicodeError, OverflowError):
        return
    if not isinstance(body, dict) or body.get("v") != 1:
        return
    sid = body.get("sid")
    if not isinstance(sid, str) or not SESSION_ID.fullmatch(sid):
        return
    operation = body.get("t")
    with _lock:
        existing = _sessions.get(instance)
        if operation == "start":
            if existing and existing.sid == sid:
                existing.repeat_start()
                return
            if existing:
                existing.stop()
            if sum(1 for session in _sessions.values() if not session.stopped.is_set()) >= MAX_SESSIONS:
                _send(instance, sid, {"t": "error", "error": "busy"})
                return
            authenticated = _authorized(instance)
            if authenticated is None:
                return
            url = body.get("url")
            if not isinstance(url, str) or len(url) > 4096:
                _send(instance, sid, {"t": "error", "error": "bad_url"})
                return
            try:
                site = web_vault.site_of(url)
                web_vault._secret()  # fail before launching if encrypted persistence is not configured
            except web_vault.VaultError as exc:
                _send(instance, sid, {"t": "error", "error": str(exc)})
                return
            locale = body.get("lang")
            if not isinstance(locale, str) or locale not in {"es", "en"}:
                locale = "es"
            detail = body.get("detail")
            if not isinstance(detail, str) or detail not in {"sharp", "economy"}:
                detail = "sharp"
            session = Session(instance, sid, authenticated[1], site, url, locale, detail)
            _sessions[instance] = session
            session.start()
        elif existing and existing.sid == sid:
            if operation == "action":
                existing.submit(body)
            elif operation == "stop":
                existing.stop()


def forget_site(owner: str, site: str) -> bool:
    """Stop every matching browser and erase its state as one admission step.

    Starting a browser also takes _lock. Holding it until the saved state is
    removed prevents a new worker from slipping in after the stop snapshot and
    later writing the just-forgotten cookies back. Each stop waits for an
    already-running save via the session mutex before the deletion occurs.
    """
    with _lock:
        sessions = [item for item in _sessions.values() if item.owner == owner and item.site == site]
        for session in sessions:
            session.stop()
        return web_vault.remove(owner, site)


def shutdown() -> None:
    with _lock:
        sessions = list(_sessions.values())
    for session in sessions:
        session.stop()
    deadline = time.monotonic() + 50
    for session in sessions:
        session.thread.join(timeout=max(0, deadline - time.monotonic()))


class Session:
    def __init__(self, instance: int, sid: str, owner: str, site: str, url: str, locale: str = "es", detail: str = "sharp"):
        self.instance, self.sid, self.owner, self.site, self.url, self.locale = instance, sid, owner, site, url, locale
        self.detail = detail if isinstance(detail, str) and detail in {"sharp", "economy"} else "sharp"
        self.generation = state.kill_generation(owner)
        self.stopped = threading.Event()
        self.queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=2)
        self.mutex = threading.Lock()
        self.last_seq = 0
        self.inflight = 0
        self.last_reply: dict[str, Any] = {"t": "opening"}
        self.last_active = time.monotonic()
        self.started = self.last_active
        self.thread = threading.Thread(target=self.run, name="private-web", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        # Serialize against an in-progress save so disconnect cannot be undone.
        with self.mutex:
            self.stopped.set()

    def repeat_start(self) -> None:
        with self.mutex:
            reply = self.last_reply if self.last_seq == 0 else {"t": "opening"}
        _send(self.instance, self.sid, reply)

    def submit(self, body: dict[str, Any]) -> None:
        seq, action = body.get("seq"), body.get("action")
        if type(seq) is not int or seq < 1 or seq > 1_000_000 or not isinstance(action, str) or action not in {
            "click", "text", "key", "scroll", "back", "forward", "reload", "goto", "frame", "save", "close", "capture", "download"
        }:
            return
        with self.mutex:
            self.last_active = time.monotonic()
            if seq == self.last_seq:
                reply = self.last_reply
            elif seq == self.inflight:
                return  # repeated request already being executed, never repeat a click
            elif seq != self.last_seq + 1 or self.inflight:
                return
            else:
                self.inflight = seq
                reply = None
                # The queue contains plaintext briefly, never on disk or in logs.
                request = {key: body[key] for key in ("action", "seq", "x", "y", "text", "key", "amount", "url", "detail") if key in body}
                try:
                    self.queue.put_nowait(request)
                except queue.Full:
                    self.inflight = 0
                    return
        if reply is not None:
            _send(self.instance, self.sid, reply)

    def _allowed(self) -> bool:
        authenticated = _authorized(self.instance)
        return bool(authenticated and authenticated[1] == self.owner and not self.stopped.is_set()
                    and not state.work_interrupted(self.owner, self.generation))

    def _exchange(self, process: sandbox.BrowserProcess, reader: io.BufferedReader, value: dict[str, Any], timeout: float) -> dict[str, Any]:
        worker = process.process
        if worker is None or not worker.stdin or not worker.stdout:
            raise sandbox.SandboxError("browser_failed")
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        view = memoryview(encoded)
        while view:
            written = worker.stdin.write(view)
            if not written:
                raise sandbox.SandboxError("browser_failed")
            view = view[written:]
        if not select.select([worker.stdout], [], [], timeout)[0]:
            raise sandbox.SandboxError("timeout")
        line = reader.readline(3_000_001)
        if not line or len(line) > 3_000_000:
            raise sandbox.SandboxError("browser_failed")
        result = json.loads(line)
        if not isinstance(result, dict):
            raise sandbox.SandboxError("browser_failed")
        return result

    @staticmethod
    def _route_reply(reply: dict[str, Any], worker: sandbox.BrowserProcess) -> dict[str, Any]:
        # Report only the selected route, never the proxy address or credentials.
        # A connected upstream does not itself prove WARP or a site is working.
        reply["route"] = worker.route
        if worker.gateway is not None and worker.gateway.take_proxy_failure():
            if reply.get("t") == "frame":
                reply["warning"] = "proxy_unavailable"
            elif reply.get("t") == "error" and reply.get("error") in {"browser_failed", "action_failed"}:
                reply["error"] = "proxy_unavailable"
        return reply

    def _reply(self, result: dict[str, Any], seq: int) -> dict[str, Any]:
        kind = result.get("kind")
        if kind == "frame":
            return {"t": "frame", "seq": seq, "image": result.get("image", ""), "width": result.get("width"),
                    "height": result.get("height"), "rendered": result.get("rendered", ""),
                    "warning": result.get("warning", "")}
        if kind == "state":
            # Linearize persistence with both kill-switch transitions and guest
            # revocation. A concurrent disconnect also takes self.mutex first.
            with self.mutex, state.lock, access._lock:
                if not self._allowed():
                    return {"t": "error", "seq": seq, "error": "closed"}
                try:
                    web_vault.save(self.owner, self.site, result.get("storage"))
                    return {"t": "saved", "seq": seq}
                except web_vault.VaultError as exc:
                    return {"t": "error", "seq": seq, "error": str(exc)}
        if kind == "capture":
            return self._send_capture(result, seq)
        if kind == "page_url":
            return self._queue_video(str(result.get("url") or ""), seq)
        if kind == "closed":
            self.stop()
            return {"t": "closed", "seq": seq}
        return {"t": "error", "seq": seq, "error": str(result.get("error") or "browser_failed")[:40]}

    def _send_capture(self, result: dict[str, Any], seq: int) -> dict[str, Any]:
        if not self._allowed():
            return {"t": "error", "seq": seq, "error": "closed"}
        try:
            image = base64.b64decode(str(result.get("image") or ""), validate=True)
            if not image.startswith(b"\xff\xd8\xff") or len(image) > 1_000_000:
                raise ValueError("capture")
            entry, _ = _authorized(self.instance) or ({}, "")
            chat_id = int(entry["chat"])
            config.TEMP_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
            config.TEMP_ROOT.chmod(0o700)
            fd, name = tempfile.mkstemp(prefix="private-capture-", suffix=".jpg", dir=config.TEMP_ROOT)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(image)
                chat.set_sender(self.owner)
                with state.guard_work(self.owner, self.generation):
                    sent = chat.send(chat_id, "Captura solicitada" if self.locale == "es" else "Requested screenshot",
                                     file=Path(name), filename="captura.jpg" if self.locale == "es" else "screenshot.jpg",
                                     viewtype="File", interrupted=lambda: not self._allowed())
                if not sent:
                    raise ValueError("capture")
            finally:
                Path(name).unlink(missing_ok=True)
            return {"t": "captured", "seq": seq}
        except Exception:
            return {"t": "error", "seq": seq, "error": "capture_failed"}

    def _queue_video(self, url: str, seq: int) -> dict[str, Any]:
        from . import media, queue as download_queue
        if not self._allowed():
            return {"t": "error", "seq": seq, "error": "closed"}
        try:
            if web_vault.site_of(url) != self.site or not media.supported(url):
                return {"t": "error", "seq": seq, "error": "not_video"}
            if web_vault.load(self.owner, self.site) is None:
                return {"t": "error", "seq": seq, "error": "not_saved"}
            entry, _ = _authorized(self.instance) or ({}, "")
            chat.set_sender(self.owner)
            chat.start_capture()  # the queue treats this as a WebXDC request
            try:
                with state.guard_work(self.owner, self.generation):
                    info = media.MediaInfo(url=url, title="Video", uploader="", duration=0, extractor="", webpage=url)
                    job, problem = download_queue.enqueue_media(int(entry["chat"]), info, "video",
                                                                int(state.setting("video_height")), self.locale)
            finally:
                chat.stop_capture()
            if problem or not job:
                return {"t": "error", "seq": seq, "error": "queue_failed"}
            return {"t": "queued", "seq": seq, "id": str(job["id"])}
        except Exception:
            return {"t": "error", "seq": seq, "error": "queue_failed"}

    def run(self) -> None:
        try:
            if not self._allowed():
                return
            _send(self.instance, self.sid, {"t": "opening"})
            storage = web_vault.load(self.owner, self.site)
            with sandbox.BrowserProcess() as worker:
                if worker.process is None or worker.process.stdout is None:
                    raise sandbox.SandboxError("browser_failed")
                opening = {"t": "opening", "route": worker.route}
                with self.mutex:
                    self.last_reply = opening
                _send(self.instance, self.sid, opening)
                reader = io.BufferedReader(worker.process.stdout, buffer_size=65536)
                first = self._exchange(worker, reader, {"url": self.url, "storage": storage, "detail": self.detail}, 50)
                if not self._allowed():
                    return
                reply = self._route_reply(self._reply(first, 0), worker)
                with self.mutex:
                    self.last_reply = reply
                _send(self.instance, self.sid, reply)
                while self._allowed():
                    now = time.monotonic()
                    if now - self.last_active > IDLE_SECONDS or now - self.started > LIFETIME_SECONDS:
                        break
                    try:
                        request = self.queue.get(timeout=2)
                    except queue.Empty:
                        continue
                    seq = int(request["seq"])
                    worker_request = dict(request)
                    if worker_request["action"] == "download":
                        worker_request["action"] = "page_url"
                    if worker_request["action"] == "goto" and not _same_site_url(worker_request.get("url"), self.site):
                        result = {"kind": "error", "error": "different_site"}
                    else:
                        result = self._exchange(worker, reader, worker_request, 45)
                    if not self._allowed():
                        return
                    reply = self._route_reply(self._reply(result, seq), worker)
                    with self.mutex:
                        self.last_seq, self.inflight, self.last_reply = seq, 0, reply
                    _send(self.instance, self.sid, reply)
        except (web_vault.VaultError, sandbox.SandboxError) as exc:
            _send(self.instance, self.sid, {"t": "error", "error": str(exc)[:40]})
        except Exception:
            _send(self.instance, self.sid, {"t": "error", "error": "browser_failed"})
        finally:
            self.stop()
            with _lock:
                leave = _sessions.get(self.instance) is self
                if leave:
                    _sessions.pop(self.instance, None)
            if leave:
                try:
                    chat.rpc().leave_webxdc_realtime(chat.account(), self.instance)
                except Exception:
                    pass
