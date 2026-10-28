"use strict";
// Private ephemeral browser frames and input travel only through WebXDC realtime.
// Never save a frame, a password, or a realtime payload in localStorage/updates.

function liveSite(value) {
  let url;
  try { url = new URL(value); } catch (error) { throw new Error("bad_url"); }
  if (url.protocol !== "https:" || url.username || url.password || (url.port && url.port !== "443")) throw new Error("bad_url");
  const host = url.hostname.toLowerCase().replace(/\.$/, "");
  const matches = (domain) => host === domain || host.endsWith("." + domain);
  const groups = [
    ["youtube", ["youtube.com", "youtu.be", "youtube-nocookie.com"]], ["facebook", ["facebook.com", "fb.watch"]],
    ["tiktok", ["tiktok.com"]], ["instagram", ["instagram.com"]], ["deepseek", ["deepseek.com"]],
    ["arena", ["arena.ai"]], ["zai", ["z.ai"]],
  ];
  for (const [name, domains] of groups) if (domains.some(matches)) return name;
  return "host:" + host;
}

window.liveWeb = {
  channel: null, sid: "", seq: 0, pending: null, ready: false, openingSeen: false, detail: "sharp",
  startTimer: 0, startDeadline: 0, actionTimer: 0, autoTimer: 0, onMessage: null,
  send(body) {
    if (!this.channel) return;
    const data = new TextEncoder().encode(JSON.stringify({ v: 1, sid: this.sid, ...body }));
    if (data.length > 15000) throw new Error("too_large");
    this.channel.send(data);
  },
  start(url, listener) {
    this.stop();
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
    const hello = () => { try { this.send({ t: "start", url, lang: lang(), detail: this.detail }); } catch (error) { /* peer may not be ready yet */ } };
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
    let attempts = 0;
    const retry = () => {
      if (!this.pending || this.pending.seq !== request.seq) return;
      if (++attempts > 5) {
        this.pending = null;
        this.ready = false;
        if (this.onMessage) this.onMessage({ t: "error", error: "unconfirmed" });
        return;
      }
      try { this.send(request); } catch (error) {
        this.pending = null;
        this.ready = false;
        if (this.onMessage) this.onMessage({ t: "error", error: error.message || "browser_failed" });
        return;
      }
      this.actionTimer = setTimeout(retry, 8000);
    };
    retry();
    return true;
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
    this.sid = "";
  },
};

VIEWS.live_web = {
  tab: "web",
  title: (params) => params.name || t("live_title"),
  render(main, params) {
    let site = "";
    try { site = liveSite(params.url); } catch (error) { add(main, failure(t("live_bad_url"))); return; }
    window.liveWeb.detail = "sharp";
    const status = h("div", { class: "note", role: "status" }, t("live_click_start"));
    const route = h("small", { class: "muted live-route", hidden: true });
    const stored = h("small", { class: "muted" }, t("live_checking"));
    const picture = h("img", { class: "live-screen", alt: t("live_screen"), hidden: true, draggable: "false" });
    const screenBox = h("div", { class: "live-screen-box", hidden: true, tabindex: "0", role: "region", "aria-label": t("live_screen") }, picture);
    const viewSettings = h("div", { class: "stack live-settings" });
    const secret = field({ type: "password", placeholder: t("live_type_placeholder"), autocomplete: "off" });
    const address = field({ type: "url", value: params.url, maxlength: "4096", placeholder: t("live_address") });
    const controls = h("div", { class: "stack", hidden: true });
    const navigation = h("div", { class: "row wrap live-navigation", hidden: true });
    let positionedFirstFrame = false;
    const auto = h("input", { type: "checkbox", disabled: true, onchange: (event) => window.liveWeb.setAuto(event.target.checked) });
    const errKey = (code) => ({ bad_url: "live_bad_url", not_configured: "live_not_configured", vault: "live_vault",
      too_large: "live_too_large", site_limit: "live_site_limit", busy: "live_busy", unsupported: "live_unsupported",
      isolation_unavailable: "live_isolation", python_unavailable: "live_isolation", browser_missing: "live_isolation",
      proxy_unavailable: "live_proxy_unavailable", proxy_invalid: "live_proxy_invalid",
      paused: "live_paused", stale_app: "live_stale_app", channel_timeout: "live_channel_timeout",
      unconfirmed: "live_unconfirmed", timeout: "live_timeout", closed: "live_closed", not_saved: "live_save_first",
      different_site: "live_same_site", frame_too_large: "live_frame_large",
      not_video: "live_not_video", queue_failed: "live_queue_failed", capture_failed: "live_capture_failed" }[code] || "live_failed");
    const run = (action, args) => {
      try {
        if (!window.liveWeb.action(action, args)) return toast(t("live_wait_action"), "bad");
        status.textContent = t("live_wait_action");
      } catch (error) { status.textContent = t(errKey(error.message)); }
    };
    const setZoom = (value) => { picture.style.width = value + "%"; screenBox.scrollLeft = 0; screenBox.scrollTop = 0; };
    add(viewSettings,
      h("div", { class: "row live-zoom" }, h("small", { class: "muted" }, t("live_zoom")),
        segmented("100", [["100", "100%"], ["150", "150%"], ["200", "200%"]], setZoom)),
      h("small", { class: "muted" }, t("live_zoom_hint")),
      h("details", { class: "live-options" }, h("summary", {}, t("live_view_options")),
        h("small", { class: "muted" }, t("live_detail_hint")),
        segmented("sharp", [["sharp", t("live_detail_sharp")], ["economy", t("live_detail_economy")]], (value) => {
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
      picture.removeAttribute("src"); picture.hidden = true; screenBox.hidden = true;
      controls.hidden = true; navigation.hidden = true; closeButton.hidden = true; secret.value = "";
      start.hidden = false; positionedFirstFrame = false;
      route.hidden = true; route.textContent = "";
    };
    const receive = (message) => {
      if (!main.isConnected) return;
      if (message.route === "proxy" || message.route === "direct") {
        route.textContent = t(message.route === "proxy" ? "live_route_proxy" : "live_route_direct");
        route.hidden = false;
      }
      if (message.t === "opening") status.textContent = t("live_opening");
      else if (message.t === "frame") {
        if (typeof message.image !== "string" || message.image.length > 120000) return;
        picture.src = "data:image/jpeg;base64," + message.image;
        picture.hidden = false;
        screenBox.hidden = false;
        auto.disabled = false;
        controls.hidden = false;
        navigation.hidden = false;
        closeButton.hidden = false;
        start.hidden = true;
        status.textContent = t(message.warning === "proxy_unavailable" ? "live_proxy_unavailable"
          : message.warning ? "live_action_warning"
          : window.liveWeb.detail === "sharp" && message.rendered === "economy" ? "live_reduced" : "live_ready");
        if (message.seq === 0 && !positionedFirstFrame) {
          positionedFirstFrame = true;
          requestAnimationFrame(() => { if (main.isConnected && !navigation.hidden) navigation.scrollIntoView({ block: "start" }); });
        }
      } else if (message.t === "saved") {
        stored.textContent = t("live_stored");
        status.textContent = t("live_saved");
      } else if (message.t === "captured") {
        status.textContent = t("live_captured");
      } else if (message.t === "queued") {
        status.textContent = t("live_queued", { id: message.id });
      } else if (message.t === "closed") {
        status.textContent = t(message.reason === "expired" ? "live_expired" : "live_closed");
        window.liveWeb.stop();
        start.disabled = false;
        clearView();
      } else if (message.t === "error") {
        status.textContent = t(errKey(message.error));
        if (message.error === "frame_too_large") { auto.checked = false; window.liveWeb.setAuto(false); }
        if (message.seq === undefined || (message.seq === 0 && !window.liveWeb.ready)) {
          window.liveWeb.stop();
          start.disabled = false;
          clearView();
        }
      }
    };
    picture.addEventListener("click", (event) => {
      const rect = picture.getBoundingClientRect();
      const x = Math.max(0, Math.min(479, Math.floor((event.clientX - rect.left) / rect.width * 480)));
      const y = Math.max(0, Math.min(759, Math.floor((event.clientY - rect.top) / rect.height * 760)));
      run("click", { x, y });
    });
    const start = h("button", { class: "btn primary", onclick: () => {
      try { liveSite(params.url); window.liveWeb.start(params.url, receive); status.textContent = t("live_wait"); start.disabled = true; }
      catch (error) { status.textContent = t(errKey(error.message)); }
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
      if (!await confirmBox(t("live_forget"), t("live_forget_q"), t("live_forget"))) return;
      window.liveWeb.stop();
      start.disabled = false;
      clearView();
      try {
        await bridge.call("browser_session", { do: "disconnect", site });
        stored.textContent = t("live_not_stored");
        status.textContent = t("live_forgotten");
      } catch (error) { status.textContent = t("live_closed"); toast(error.message, "bad"); }
    } }, t("live_forget"));
    add(navigation,
      h("small", { class: "muted" }, t("live_page_scroll")),
      h("button", { class: "btn small ghost", "aria-label": t("live_scroll_up"), onclick: () => run("scroll", { amount: -240 }) }, "↑"),
      h("button", { class: "btn small ghost", "aria-label": t("live_scroll_down"), onclick: () => run("scroll", { amount: 240 }) }, "↓"),
      h("button", { class: "btn small ghost", "aria-label": t("live_scroll_page_up"), onclick: () => run("scroll", { amount: -600 }) }, "↑↑"),
      h("button", { class: "btn small ghost", "aria-label": t("live_scroll_page_down"), onclick: () => run("scroll", { amount: 600 }) }, "↓↓"),
      closeButton);
    add(controls,
      h("div", { class: "row wrap" },
        h("button", { class: "btn small ghost", onclick: () => run("back") }, "← ", t("back")),
        h("button", { class: "btn small ghost", onclick: () => run("forward") }, t("next"), " →"),
        h("button", { class: "btn small ghost", "aria-label": t("refresh"), onclick: () => run("reload") }, "↻"),
        h("button", { class: "btn small ghost", onclick: () => run("frame") }, t("live_refresh"))),
      h("small", { class: "muted" }, t("live_address_hint")),
      h("div", { class: "row" }, address, h("button", { class: "btn small", onclick: openAddress }, t("go"))),
      h("p", { class: "muted small" }, t("live_click_hint")),
      h("div", { class: "row" }, secret,
        h("button", { class: "btn small", onclick: () => {
          const text = secret.value;
          if (!text) return;
          try {
            if (window.liveWeb.action("text", { text })) { secret.value = ""; status.textContent = t("live_wait_action"); }
            else toast(t("live_wait_action"), "bad");
          } catch (error) { secret.value = ""; status.textContent = t(errKey(error.message)); }
        } }, t("live_type"))),
      h("div", { class: "row wrap" },
        h("button", { class: "btn small ghost", onclick: () => { secret.type = secret.type === "password" ? "text" : "password"; } }, t("live_show")),
        ...[["Tab", "Tab"], ["Enter", "Enter"], ["Backspace", "⌫"]].map(([key, label]) =>
          h("button", { class: "btn small ghost", onclick: () => run("key", { key }) }, label))),
      h("div", { class: "row wrap" },
        h("button", { class: "btn primary", onclick: async () => {
          if (await confirmBox(t("live_save"), t("live_save_q"), t("live_save"))) run("save");
        } }, t("live_save")),
        h("button", { class: "btn ghost", onclick: async () => {
          if (await confirmBox(t("live_capture"), t("live_capture_q"), t("live_capture"))) run("capture");
        } }, t("live_capture")),
        h("button", { class: "btn ghost", onclick: async () => {
          if (await confirmBox(t("live_download"), t("live_download_q"), t("live_download"))) run("download");
        } }, t("live_download"))));
    const privacy = h("details", { class: "live-privacy" }, h("summary", {}, t("live_privacy_more")),
      h("p", { class: "muted small" }, t("live_privacy")), h("p", { class: "muted small" }, t("live_personal_hint")));
    const siteRow = h("div", { class: "row" }, h("small", { class: "site-address" }, params.url),
      h("button", { class: "btn small ghost", onclick: () => copyText(params.url) }, t("copy_link")));
    add(main, h("p", { class: "note live-brief" }, t("live_privacy_brief")), privacy,
      siteRow, stored,
      h("div", { class: "row wrap" }, start, disconnect), status, route,
      navigation, screenBox, viewSettings, controls);
    updateStored();
  },
};
