"use strict";
// Private ephemeral browser frames and input travel only through WebXDC realtime.
// Never save a frame, a password, or a realtime payload in localStorage/updates.

function liveSite(value) {
  let url;
  try { url = new URL(value); } catch (error) { throw new Error("bad_url"); }
  if (url.protocol !== "https:" || url.username || url.password || (url.port && url.port !== "443")) throw new Error("bad_url");
  const host = url.hostname.toLowerCase().replace(/\.$/, "");
  const matches = (domain) => host === domain || host.endsWith("." + domain);
  if (["youtube.com", "youtu.be", "youtube-nocookie.com"].some(matches)) return "youtube";
  return "host:" + host;
}

// Fit the interaction area to the space actually visible within the app.
// visualViewport can shrink when a phone keyboard overlays the layout viewport;
// the containing client still decides how its own keyboard behaves.
function fitLiveWorkspace(alignFocused = false) {
  const main = document.querySelector('.view[data-view="live_web"]');
  const work = main && main.querySelector(".live-workspace");
  if (!work || work.hidden) return;
  const rect = main.getBoundingClientRect(), viewport = window.visualViewport;
  const top = Math.max(rect.top, viewport ? viewport.offsetTop : 0);
  const bottom = Math.min(rect.bottom, viewport ? viewport.offsetTop + viewport.height : window.innerHeight);
  const height = Math.max(160, Math.min(720, Math.floor(bottom - top - 12)));
  work.style.height = height + "px";
  // Short visual viewports need space for a readable two-line status without
  // hiding the input. Normal-height screens keep the larger touch targets.
  work.classList.toggle("live-compact", height < 260);
  if (alignFocused && work.contains(document.activeElement)) {
    requestAnimationFrame(() => {
      if (!work.isConnected || work.hidden) return;
      const current = window.visualViewport;
      const visibleTop = Math.max(main.getBoundingClientRect().top, current ? current.offsetTop : 0);
      main.scrollTop += work.getBoundingClientRect().top - visibleTop;
    });
  }
}
if (typeof window.addEventListener === "function") window.addEventListener("resize", () => fitLiveWorkspace(true));
if (window.visualViewport) {
  window.visualViewport.addEventListener("resize", () => fitLiveWorkspace(true));
  window.visualViewport.addEventListener("scroll", () => fitLiveWorkspace(false));
}

window.liveWeb = {
  channel: null, sid: "", seq: 0, pending: null, ready: false, openingSeen: false, detail: "economy", browser: "graphical_saved",
  startTimer: 0, startDeadline: 0, actionTimer: 0, autoTimer: 0, onMessage: null, onTraffic: null,
  sentBytes: 0, receivedBytes: 0,
  reportTraffic() {
    if (this.onTraffic) this.onTraffic(this.sentBytes, this.receivedBytes);
  },
  send(body) {
    if (!this.channel) return;
    const data = new TextEncoder().encode(JSON.stringify({ v: 1, sid: this.sid, ...body }));
    if (data.length > 15000) {
      const error = new Error("too_large");
      error.liveLocalLimit = true;
      throw error;
    }
    this.channel.send(data);
    // Payload bytes only: not ETECSA billing, protocol overhead, or mail traffic.
    this.sentBytes += data.byteLength;
    this.reportTraffic();
  },
  start(url, listener, trafficListener = null) {
    this.stop();
    this.sentBytes = 0;
    this.receivedBytes = 0;
    this.onTraffic = typeof trafficListener === "function" ? trafficListener : null;
    if (!window.webxdc || !window.webxdc.joinRealtimeChannel || !window.crypto || !crypto.getRandomValues) throw new Error("unsupported");
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    this.sid = [...bytes].map((value) => value.toString(16).padStart(2, "0")).join("");
    this.seq = 0;
    this.ready = false;
    this.openingSeen = false;
    this.onMessage = listener;
    this.channel = window.webxdc.joinRealtimeChannel();
    this.channel.setListener((data) => {
      let message;
      try { message = JSON.parse(new TextDecoder().decode(data)); } catch (error) { return; }
      if (!message || message.v !== 1 || message.sid !== this.sid) return;
      // Count duplicate and out-of-order payloads too: they still crossed the channel.
      this.receivedBytes += typeof data.byteLength === "number" ? data.byteLength : 0;
      this.reportTraffic();
      if (message.t === "opening" && this.ready) return;
      if (message.t === "opening") this.openingSeen = true;
      // Duplicate replies can arrive after a retry; do not put an old image or
      // status over a newer action, and never acknowledge an unknown sequence.
      if (Number.isInteger(message.seq) && message.seq > 0 && (!this.pending || message.seq !== this.pending.seq)) return;
      if (message.t === "frame" && message.seq === 0 && (this.seq > 0 || this.pending)) return;
      if (message.t === "frame" && message.seq === 0) {
        clearInterval(this.startTimer);
        clearTimeout(this.startDeadline);
        this.startTimer = 0;
        this.startDeadline = 0;
        this.ready = true;
      }
      if (this.pending && message.seq === this.pending.seq) {
        this.seq = this.pending.seq;
        this.pending = null;
        clearTimeout(this.actionTimer);
        this.actionTimer = 0;
      }
      if (message.t === "error" && (message.seq === undefined || (message.seq === 0 && !this.ready))) {
        clearInterval(this.startTimer);
        clearTimeout(this.startDeadline);
        this.startTimer = 0;
        this.startDeadline = 0;
      }
      if (this.onMessage) this.onMessage(message);
    });
    const hello = () => { try { this.send({ t: "start", url, lang: lang(), detail: this.detail, browser: this.browser }); } catch (error) { /* peer may not be ready yet */ } };
    hello();
    this.startTimer = setInterval(hello, 3500);
    this.startDeadline = setTimeout(() => {
      if (!this.ready && this.onMessage) this.onMessage({ t: "error", error: this.openingSeen ? "timeout" : "channel_timeout" });
    }, 110000);
  },
  action(action, args = {}) {
    if (!this.ready || this.pending) return false;
    const request = { t: "action", action, seq: this.seq + 1, ...args, detail: this.detail };
    this.pending = request;
    let attempts = 0, sent = false;
    const retry = () => {
      if (!this.pending || this.pending.seq !== request.seq) return;
      if (++attempts > 5) {
        this.pending = null;
        this.ready = false;
        if (this.onMessage) this.onMessage({ t: "error", error: "unconfirmed" });
        return;
      }
      try { this.send(request); sent = true; } catch (error) {
        this.pending = null;
        if (error.liveLocalLimit && !sent) {
          // Rejected locally before channel.send: the user may shorten the
          // text and retry the same sequence without reopening the browser.
          if (this.onMessage) this.onMessage({ t: "error", seq: request.seq, error: "too_large" });
        } else {
          this.ready = false;
          if (this.onMessage) this.onMessage({ t: "error", error: error.message || "browser_failed" });
        }
        return;
      }
      this.actionTimer = setTimeout(retry, 8000);
    };
    retry();
    // In particular, do not clear typed text if the first send failed (for
    // example because the UTF-8 payload exceeded the realtime size limit).
    return sent;
  },
  setAuto(enabled) {
    clearInterval(this.autoTimer);
    this.autoTimer = 0;
    if (enabled && this.channel) {
      // Explicit opt-in: one still frame every five seconds, not a video stream.
      // A pending click or refresh is never repeated or interrupted.
      this.autoTimer = setInterval(() => {
        if (this.ready && !this.pending) this.action("frame");
      }, 5000);
    }
  },
  stop() {
    this.setAuto(false);
    clearInterval(this.startTimer);
    clearTimeout(this.startDeadline);
    clearTimeout(this.actionTimer);
    if (this.channel) {
      try { this.send({ t: "stop" }); } catch (error) { /* a disconnected peer will time out */ }
      try { this.channel.leave(); } catch (error) { /* already left */ }
    }
    this.channel = null;
    this.pending = null;
    this.ready = false;
    this.openingSeen = false;
    this.onMessage = null;
    this.onTraffic = null;
    this.sid = "";
  },
};

VIEWS.live_web = {
  tab: "web",
  title: (params) => params.name || t("live_title"),
  render(main, params) {
    let site = "";
    try { site = liveSite(params.url); } catch (error) { add(main, failure(t("live_bad_url"))); return; }
    window.liveWeb.detail = "economy";
    window.liveWeb.browser = "graphical_saved";
    const status = h("div", { class: "note", role: "status" }, t("live_click_start"));
    const introStatus = h("div", { class: "live-intro-status" }, status);
    const workStatus = h("div", { class: "live-work-status", role: "region", tabindex: "0", "aria-label": t("live_status_label") });
    const workspace = h("section", { class: "live-workspace", hidden: true, "aria-label": t("live_workspace") });
    const route = h("small", { class: "muted live-route", hidden: true });
    const traffic = h("small", { class: "muted live-traffic", hidden: true });
    const reportTraffic = (sent, received) => {
      if (!main.isConnected) return;
      traffic.hidden = false;
      traffic.textContent = t("live_traffic", { sent: fmtSize(sent), received: fmtSize(received) });
    };
    const stored = h("small", { class: "muted" }, t("live_checking"));
    const picture = h("img", { class: "live-screen", alt: t("live_screen"), hidden: true, draggable: "false" });
    const screenBox = h("div", { class: "live-screen-box", hidden: true, tabindex: "0", role: "region", "aria-label": t("live_screen") }, picture);
    const viewSettings = h("div", { class: "stack live-settings" });
    const secret = field({ type: "password", placeholder: t("live_type_placeholder"), autocomplete: "off",
      "aria-label": t("live_type_label"), enterkeyhint: "send" });
    const address = field({ type: "url", value: params.url, maxlength: "4096", placeholder: t("live_address"), "aria-label": t("live_address") });
    const controls = h("div", { class: "stack live-extra-controls", hidden: true });
    const navigation = h("nav", { class: "live-navigation", hidden: true, "aria-label": t("live_navigation_label") });
    let positionedFirstFrame = false;
    let gesture = null, pageSwipe = null, suppressClick = false, suppressTimer = 0, pressedWhileBusy = false;
    let lastTarget = { x: 240, y: 380 }, activeMode = null, nextMode = null;
    let frameLoading = false, frameVersion = 0;
    let zoomValue = "100", zoomPicker = null, quickZoom = null;
    const auto = h("input", { type: "checkbox", disabled: true, onchange: (event) => window.liveWeb.setAuto(event.target.checked) });
    const pauseAuto = () => { auto.checked = false; window.liveWeb.setAuto(false); };
    const swipeToggle = h("input", { type: "checkbox", disabled: true, "aria-label": t("live_swipe_page"), onchange: (event) => {
      pageSwipe = null;
      picture.classList.toggle("live-scroll-active", event.target.checked);
      if (event.target.checked) {
        dragToggle.checked = false; gesture = null;
        picture.classList.remove("live-drag-active");
        pauseAuto();
      }
    } });
    const dragToggle = h("input", { type: "checkbox", disabled: true, "aria-label": t("live_gesture"), onchange: (event) => {
      gesture = null;
      picture.classList.toggle("live-drag-active", event.target.checked);
      if (event.target.checked) {
        swipeToggle.checked = false; pageSwipe = null;
        picture.classList.remove("live-scroll-active");
        // Do not let an automatic refresh race a human gesture.
        pauseAuto();
      }
    } });
    const errKey = (code) => ({ bad_url: "live_bad_url", not_configured: "live_not_configured", vault: "live_vault",
      too_large: "live_too_large", site_limit: "live_site_limit", busy: "live_busy", unsupported: "live_unsupported",
      isolation_unavailable: "live_isolation", python_unavailable: "live_isolation", browser_missing: "live_isolation",
      proxy_unavailable: "live_proxy_unavailable", proxy_invalid: "live_proxy_invalid",
      paused: "live_paused", maintenance: "live_maintenance", stale_app: "live_stale_app", channel_timeout: "live_channel_timeout",
      unconfirmed: "live_unconfirmed", timeout: "live_timeout", closed: "live_closed", not_saved: "live_save_first",
      different_site: "live_same_site", frame_too_large: "live_frame_large",
      human_unavailable: "live_human_unavailable", human_address_unready: "live_human_address_unready",
      human_no_saved_session: "live_human_no_saved_session", human_close_popup_first: "live_human_close_popup_first", multiple_pages: "live_multiple_pages",
      not_video: "live_not_video", queue_failed: "live_queue_failed", capture_failed: "live_capture_failed" }[code] || "live_failed");
    const modeNote = h("small", { class: "muted" }, t("live_mode_graphical_saved_hint"));
    const modePicker = segmented("graphical_saved", [["graphical_saved", t("live_mode_graphical_saved")], ["human", t("live_mode_human")]], async (value) => {
      if (!window.liveWeb.channel) { applyMode(value); return; }
      const previous = activeMode || window.liveWeb.browser;
      if (value === previous) return;
      // segmented() selects optimistically; restore the actual browser while
      // asking. A mode change must never silently discard a live session.
      paintMode(previous);
      if (!window.liveWeb.ready || window.liveWeb.pending || frameLoading) { toast(t("live_wait_action"), "bad"); return; }
      if (!await confirmBox(t("live_switch_title"), t("live_switch_q"), t("live_switch_confirm")) || !modePicker.isConnected) return;
      if (!window.liveWeb.channel) { applyMode(value); return; }
      if (activeMode !== previous || !window.liveWeb.ready || window.liveWeb.pending || frameLoading) {
        toast(t("live_wait_action"), "bad"); return;
      }
      nextMode = value;
      if (!run("close")) nextMode = null;
    });
    const paintMode = (value) => modePicker.querySelectorAll("button").forEach((button) => {
      const selected = button.dataset.key === value;
      button.classList.toggle("on", selected);
      button.setAttribute("aria-selected", String(selected));
    });
    const applyMode = (value) => {
      window.liveWeb.browser = value;
      paintMode(value);
      modeNote.textContent = t(value === "human" ? "live_mode_human_hint" : "live_mode_graphical_saved_hint");
      saveButton.hidden = value === "human";
      downloadButton.hidden = value === "human";
      closeWindowButton.hidden = !["human", "graphical_saved"].includes(value);
    };
    const run = (action, args) => {
      try {
        if (frameLoading || !window.liveWeb.ready || window.liveWeb.pending) { toast(t("live_wait_action"), "bad"); return false; }
        if (!window.liveWeb.action(action, args)) return false;
        status.textContent = t("live_wait_action");
        return true;
      } catch (error) { status.textContent = t(errKey(error.message)); return false; }
    };
    const scrollAt = (amount, target = lastTarget) => run("scroll", { amount, x: target.x, y: target.y });
    const setZoom = (value) => {
      zoomValue = value;
      picture.style.width = value + "%";
      screenBox.classList.toggle("live-zoomed", value !== "100");
      screenBox.scrollLeft = 0; screenBox.scrollTop = 0;
      if (zoomPicker) zoomPicker.querySelectorAll("button").forEach((button) => {
        const selected = button.dataset.key === value;
        button.classList.toggle("on", selected);
        button.setAttribute("aria-selected", String(selected));
      });
      if (quickZoom) {
        const label = t("live_quick_zoom", { value: value === "100" ? t("live_zoom_fit") : value + "%" });
        quickZoom.textContent = value === "100" ? t("live_zoom_short") : value + "%";
        quickZoom.setAttribute("aria-label", label);
        quickZoom.title = label;
      }
      if (window.liveWeb.ready) status.textContent = t(value === "100" ? "live_ready" : "live_zoom_pan");
    };
    zoomPicker = segmented("100", [["100", t("live_zoom_fit")], ["150", "150%"], ["200", "200%"]], setZoom);
    add(viewSettings,
      h("div", { class: "row live-zoom" }, h("small", { class: "muted" }, t("live_zoom")), zoomPicker),
      h("small", { class: "muted" }, t("live_zoom_hint")),
      h("details", { class: "live-options" }, h("summary", {}, t("live_view_options")),
        h("small", { class: "muted" }, t("live_detail_hint")),
        segmented("economy", [["economy", t("live_detail_economy")], ["sharp", t("live_detail_sharp")]], (value) => {
          window.liveWeb.detail = value;
          if (window.liveWeb.ready && !window.liveWeb.pending) run("frame");
        }),
        h("label", { class: "row live-auto" }, auto, t("live_auto"))));
    const updateStored = async () => {
      try {
        const result = await bridge.call("browser_session", { do: "status", site });
        if (stored.isConnected) stored.textContent = t(result.stored ? "live_stored" : "live_not_stored");
      } catch (error) { if (stored.isConnected) stored.textContent = error.message || t("live_status_unknown"); }
    };
    const clearView = () => {
      window.liveWeb.setAuto(false);
      auto.checked = false; auto.disabled = true;
      dragToggle.checked = false; dragToggle.disabled = true; gesture = null;
      swipeToggle.checked = false; swipeToggle.disabled = true; pageSwipe = null;
      clearTimeout(suppressTimer); suppressClick = false; pressedWhileBusy = false;
      picture.classList.remove("live-drag-active", "live-scroll-active");
      frameVersion += 1; frameLoading = false; picture.onload = null; picture.onerror = null;
      screenBox.removeAttribute("aria-busy");
      picture.removeAttribute("src"); picture.hidden = true; screenBox.hidden = true;
      controls.hidden = true; navigation.hidden = true; closeButton.hidden = true; workspace.hidden = true;
      introStatus.append(status);
      secret.value = ""; secret.type = "password"; showButton.setAttribute("aria-pressed", "false");
      start.hidden = false; positionedFirstFrame = false; activeMode = null;
      lastTarget = { x: 240, y: 380 };
      route.hidden = true; route.textContent = "";
    };
    const receive = (message) => {
      if (!workspace.isConnected) return;
      if (message.route === "proxy" || message.route === "direct") {
        route.textContent = t(message.route === "proxy" ? "live_route_proxy" : "live_route_direct");
        route.hidden = false;
      }
      if (message.t === "opening") status.textContent = t("live_opening");
      else if (message.t === "frame") {
        if (typeof message.image !== "string" || message.image.length > 120000) return;
        // A failed close can return a warning frame; do not switch modes later
        // merely because the user closes the browser for another reason.
        if (nextMode) nextMode = null;
        const version = ++frameVersion;
        frameLoading = true;
        screenBox.setAttribute("aria-busy", "true");
        picture.onload = () => {
          if (version !== frameVersion || !picture.isConnected) return;
          frameLoading = false;
          screenBox.removeAttribute("aria-busy");
        };
        picture.onerror = () => {
          if (version !== frameVersion || !picture.isConnected) return;
          frameLoading = false;
          screenBox.removeAttribute("aria-busy");
          picture.hidden = true;
          status.textContent = t("live_frame_display_failed");
        };
        picture.src = "data:image/jpeg;base64," + message.image;
        picture.hidden = false;
        screenBox.hidden = false;
        auto.disabled = false;
        dragToggle.disabled = false;
        swipeToggle.disabled = false;
        controls.hidden = false;
        navigation.hidden = false;
        closeButton.hidden = false;
        workspace.hidden = false;
        workStatus.append(status);
        start.hidden = true;
        status.textContent = t(message.warning === "proxy_unavailable" ? "live_proxy_unavailable"
          : message.warning ? "live_action_warning"
          : window.liveWeb.detail === "sharp" && message.rendered === "economy" ? "live_reduced" : "live_ready");
        fitLiveWorkspace();
        if (message.seq === 0 && !positionedFirstFrame) {
          positionedFirstFrame = true;
          requestAnimationFrame(() => { if (main.isConnected && !workspace.hidden) workspace.scrollIntoView({ block: "start" }); });
        }
      } else if (message.t === "saved") {
        stored.textContent = t("live_stored");
        status.textContent = t("live_saved");
      } else if (message.t === "captured") {
        status.textContent = t("live_captured");
      } else if (message.t === "queued") {
        status.textContent = t("live_queued", { id: message.id });
      } else if (message.t === "closed") {
        const selected = nextMode;
        nextMode = null;
        status.textContent = t(message.reason === "expired" ? "live_expired" : "live_closed");
        window.liveWeb.stop();
        start.disabled = false;
        clearView();
        if (selected) {
          applyMode(selected);
          status.textContent = t("live_switch_ready");
          requestAnimationFrame(() => { if (modePicker.isConnected) modePicker.scrollIntoView({ block: "center" }); });
        }
      } else if (message.t === "error") {
        nextMode = null;
        status.textContent = t(errKey(message.error));
        if (message.error === "frame_too_large") { auto.checked = false; window.liveWeb.setAuto(false); }
        if (message.seq === undefined || (message.seq === 0 && !window.liveWeb.ready)) {
          nextMode = null;
          window.liveWeb.stop();
          start.disabled = false;
          clearView();
        }
      }
    };
    const screenPoint = (event) => {
      const rect = picture.getBoundingClientRect();
      return {
        x: Math.max(0, Math.min(479, Math.floor((event.clientX - rect.left) / rect.width * 480))),
        y: Math.max(0, Math.min(759, Math.floor((event.clientY - rect.top) / rect.height * 760))),
      };
    };
    picture.addEventListener("click", (event) => {
      if (dragToggle.checked || suppressClick || pressedWhileBusy) {
        event.preventDefault();
        suppressClick = false; pressedWhileBusy = false; clearTimeout(suppressTimer);
        return;
      }
      const target = screenPoint(event);
      if (run("click", target)) lastTarget = target;
    });
    picture.addEventListener("wheel", (event) => {
      if (!swipeToggle.checked || dragToggle.checked || !event.deltaY) return;
      event.preventDefault();
      lastTarget = screenPoint(event);
      scrollAt((event.deltaY < 0 ? -1 : 1) * (Math.abs(event.deltaY) >= 400 ? 600 : 240));
    }, { passive: false });
    // Both modes relay only gestures made by the person. A normal swipe scrolls
    // the page at the touched point; the separate drag mode holds the mouse
    // button for sliders or controls. Neither analyzes or solves challenges.
    picture.addEventListener("pointerdown", (event) => {
      if (event.pointerType === "touch" && !event.isPrimary && pageSwipe) pageSwipe.multi = true;
      if (!event.isPrimary || event.button !== 0) return;
      // Do not apply a press that began on an old image even if a delayed
      // reply arrives before the browser dispatches its synthetic click.
      pressedWhileBusy = !window.liveWeb.ready || !!window.liveWeb.pending || frameLoading;
      if (pressedWhileBusy) { toast(t("live_wait_action"), "bad"); return; }
      if ((!dragToggle.checked && !swipeToggle.checked) || gesture || pageSwipe) return;
      if (swipeToggle.checked) {
        pageSwipe = { id: event.pointerId, x: event.clientX, y: event.clientY, target: screenPoint(event), moved: 0, multi: false };
      } else {
        event.preventDefault();
        gesture = { id: event.pointerId, start: event.timeStamp, points: [{ ...screenPoint(event), ms: 0 }], tooLong: false };
      }
      try { picture.setPointerCapture(event.pointerId); } catch (error) { /* pointer capture is optional */ }
    });
    const collectPoint = (event) => {
      if (pageSwipe && event.pointerId === pageSwipe.id) {
        pageSwipe.moved = Math.max(pageSwipe.moved, Math.abs(event.clientX - pageSwipe.x), Math.abs(event.clientY - pageSwipe.y));
        return;
      }
      if (!gesture || event.pointerId !== gesture.id) return;
      event.preventDefault();
      const ms = Math.max(0, Math.round(event.timeStamp - gesture.start));
      if (ms > 8000) { gesture.tooLong = true; return; }
      // Thin long paths while preserving their beginning and eventual end.
      if (gesture.points.length >= 63) gesture.points = gesture.points.filter((_, i) => i === 0 || i % 2 === 0);
      const previous = gesture.points[gesture.points.length - 1];
      gesture.points.push({ ...screenPoint(event), ms: Math.max(previous.ms, ms) });
    };
    picture.addEventListener("pointermove", collectPoint);
    picture.addEventListener("pointerup", (event) => {
      if (pageSwipe && event.pointerId === pageSwipe.id) {
        const completed = pageSwipe;
        pageSwipe = null;
        const dx = event.clientX - completed.x, dy = event.clientY - completed.y;
        if (completed.multi || Math.max(completed.moved, Math.abs(dx), Math.abs(dy)) >= 25) {
          // Swiping, returning to the starting point, or adding another finger
          // must never also click a challenge tile or another control.
          event.preventDefault();
          suppressClick = true; clearTimeout(suppressTimer);
          suppressTimer = setTimeout(() => { suppressClick = false; }, 600);
          if (!completed.multi && Math.abs(dy) > Math.abs(dx)) {
            lastTarget = completed.target;
            scrollAt((dy < 0 ? 1 : -1) * (Math.abs(dy) >= 180 ? 600 : 240), lastTarget);
          }
        } else if (event.pointerType === "touch") {
          // Some touch browsers omit the synthesized click after pointer
          // capture. Relay a deliberate short tap here, and swallow any later
          // compatibility click so it cannot activate the remote page twice.
          event.preventDefault();
          suppressClick = true; clearTimeout(suppressTimer);
          suppressTimer = setTimeout(() => { suppressClick = false; }, 600);
          const target = screenPoint(event);
          if (run("click", target)) lastTarget = target;
        }
        return;
      }
      if (!gesture || event.pointerId !== gesture.id) return;
      collectPoint(event);
      const completed = gesture;
      gesture = null;
      // Stay in gesture mode until explicitly turned off, so the synthesized
      // click following pointerup cannot produce a second remote action.
      if (completed.tooLong) { status.textContent = t("live_gesture_long"); return; }
      lastTarget = { x: completed.points[0].x, y: completed.points[0].y };
      run("drag", { points: completed.points });
    });
    picture.addEventListener("pointercancel", (event) => {
      if (pageSwipe && event.pointerId === pageSwipe.id) pageSwipe = null;
      if (gesture && event.pointerId === gesture.id) { gesture = null; status.textContent = t("live_gesture_cancelled"); }
    });
    const start = h("button", { class: "btn primary", onclick: () => {
      try {
        liveSite(params.url);
        activeMode = window.liveWeb.browser;
        window.liveWeb.start(params.url, receive, reportTraffic);
        status.textContent = t("live_wait"); start.disabled = true;
      } catch (error) { activeMode = null; status.textContent = t(errKey(error.message)); }
    } }, t("live_start"));
    const closeButton = h("button", { class: "btn small ghost", hidden: true, "aria-label": t("live_close"), onclick: () => run("close") }, t("close"));
    const openAddress = () => {
      const value = address.value.trim();
      try {
        if (value.length > 4096 || liveSite(value) !== site) throw new Error("different_site");
        run("goto", { url: value });
      } catch (error) { status.textContent = t(errKey(error.message)); }
    };
    address.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); openAddress(); }
    });
    const disconnect = h("button", { class: "btn small ghost", onclick: async () => {
      const sid = window.liveWeb.sid;
      if (!await confirmBox(t("live_forget"), t("live_forget_q"), t("live_forget"))) return;
      if (!disconnect.isConnected || window.liveWeb.sid !== sid) {
        if (disconnect.isConnected) status.textContent = t("live_session_changed");
        return;
      }
      window.liveWeb.stop();
      start.disabled = false;
      clearView();
      try {
        await bridge.call("browser_session", { do: "disconnect", site });
        stored.textContent = t("live_not_stored");
        status.textContent = t("live_forgotten");
      } catch (error) { status.textContent = t("live_closed"); toast(error.message, "bad"); }
    } }, t("live_forget"));
    const navButton = (label, text, onclick) => h("button", {
      class: "btn small ghost", "aria-label": label, title: label, onclick,
    }, text);
    add(navigation,
      navButton(t("live_history_back"), "←", () => run("back")),
      navButton(t("live_history_forward"), "→", () => run("forward")),
      navButton(t("live_reload_page"), "↻", () => run("reload")),
      navButton(t("live_refresh"), t("live_view_short"), () => run("frame")),
      navButton(t("live_scroll_up"), "↑", () => scrollAt(-240)),
      navButton(t("live_scroll_down"), "↓", () => scrollAt(240)));
    const confirmLiveAction = async (action, title, question, args) => {
      const sid = window.liveWeb.sid, mode = activeMode;
      if (!sid || !window.liveWeb.ready || window.liveWeb.pending || frameLoading) {
        toast(t("live_wait_action"), "bad"); return;
      }
      if (!await confirmBox(title, question, title)) return;
      // The consent belongs to this specific open browser, not to another site
      // or a new session that appeared while the confirmation was visible.
      if (!workspace.isConnected || window.liveWeb.sid !== sid || activeMode !== mode || window.liveWeb.browser !== mode) {
        if (workspace.isConnected) status.textContent = t("live_session_changed");
        return;
      }
      run(action, args);
    };
    const closeWindowButton = h("button", { class: "btn small ghost", hidden: true, onclick: () =>
      confirmLiveAction("key", t("live_human_close_window"), t("live_human_close_window_q"), { key: "CloseWindow" })
    }, t("live_human_close_window"));
    const saveButton = h("button", { class: "btn primary", onclick: () =>
      confirmLiveAction("save", t("live_save"), t("live_save_q"))
    }, t("live_save"));
    const downloadButton = h("button", { class: "btn ghost", onclick: () =>
      confirmLiveAction("download", t("live_download"), t("live_download_q"))
    }, t("live_download"));
    const sendText = () => {
      const text = secret.value;
      if (!text) return;
      // The workers accept at most 4096 Unicode characters. Keep the text in
      // the field on a local validation failure or when a previous step waits.
      if (text.length > 8192 || [...text].length > 4096 || text.includes("\0")) {
        status.textContent = t("live_text_limit"); return;
      }
      if (run("text", { text })) secret.value = "";
    };
    secret.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.isComposing && event.keyCode !== 229) { event.preventDefault(); sendText(); }
    });
    secret.addEventListener("focus", () => fitLiveWorkspace(true));
    const showButton = h("button", { class: "btn small ghost", "aria-label": t("live_show"), title: t("live_show"),
      "aria-pressed": "false", onclick: () => {
        const revealed = secret.type === "password";
        secret.type = revealed ? "text" : "password";
        showButton.setAttribute("aria-pressed", String(revealed));
      } }, icon("eye"));
    quickZoom = h("button", { class: "btn small ghost", "aria-label": t("live_quick_zoom", { value: t("live_zoom_fit") }),
      title: t("live_quick_zoom", { value: t("live_zoom_fit") }), onclick: () =>
        setZoom(zoomValue === "100" ? "150" : zoomValue === "150" ? "200" : "100")
    }, t("live_zoom_short"));
    const keyButton = (key, label) => h("button", { class: "btn small ghost", "aria-label": t("live_key_label", { key: label }),
      title: t("live_key_label", { key: label }), onclick: () => run("key", { key }) }, label);
    const composer = h("div", { class: "live-composer" },
      h("div", { class: "row live-compose-text" }, secret, h("button", { class: "btn small primary", onclick: sendText }, t("live_type"))),
      h("div", { class: "live-compose-keys" }, showButton, quickZoom, keyButton("Tab", "Tab"), keyButton("Enter", "Enter"), keyButton("Backspace", "⌫")));
    add(workspace, navigation, screenBox, workStatus, composer);
    add(controls,
      h("small", { class: "muted" }, t("live_navigation_hint")),
      h("div", { class: "row wrap live-gestures" },
        h("label", { class: "row live-gesture" }, swipeToggle, t("live_swipe_short")),
        h("label", { class: "row live-gesture" }, dragToggle, t("live_drag_short")),
        navButton(t("live_scroll_page_up"), "↑↑", () => scrollAt(-600)),
        navButton(t("live_scroll_page_down"), "↓↓", () => scrollAt(600)), closeButton),
      h("small", { class: "muted" }, t("live_address_hint")),
      h("div", { class: "row" }, address, h("button", { class: "btn small", onclick: openAddress }, t("go"))),
      h("p", { class: "muted small" }, t("live_click_hint")),
      h("div", { class: "row wrap" },
        ...[["Shift+Tab", "⇤"], ["Space", t("live_key_space")], ["Escape", "Esc"],
            ["ArrowLeft", "←"], ["ArrowRight", "→"]].map(([key, label]) => keyButton(key, label)), closeWindowButton),
      h("div", { class: "row wrap" }, saveButton,
        h("button", { class: "btn ghost", onclick: () =>
          confirmLiveAction("capture", t("live_capture"), t("live_capture_q"))
        }, t("live_capture")), downloadButton));
    const privacy = h("details", { class: "live-privacy" }, h("summary", {}, t("live_privacy_more")),
      h("p", { class: "muted small" }, t("live_privacy")), h("p", { class: "muted small" }, t("live_personal_hint")));
    const siteRow = h("div", { class: "row" }, h("small", { class: "site-address" }, params.url),
      h("button", { class: "btn small ghost", onclick: () => copyText(params.url) }, t("copy_link")));
    const external = h("div", { class: "live-device-link" },
      h("a", { class: "link", href: new URL(params.url).href, target: "_blank", rel: "noopener noreferrer" }, t("live_external_open")),
      h("small", { class: "muted" }, t("live_external_hint")), h("small", { class: "muted" }, t("live_accessibility_hint")));
    add(main, h("p", { class: "note live-brief" }, t("live_privacy_brief")), privacy,
      siteRow, external, stored,
      h("small", { class: "muted" }, t("live_mode_label")), modePicker, modeNote,
      h("div", { class: "row wrap" }, start, disconnect), introStatus, route, traffic,
      workspace, controls, viewSettings);
    applyMode("graphical_saved");
    updateStored();
  },
};
