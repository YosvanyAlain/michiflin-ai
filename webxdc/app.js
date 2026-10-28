"use strict";

const TABS = [
  { id: "home", icon: "home" },
  { id: "ai", icon: "ai" },
  { id: "web", icon: "web" },
  { id: "apps", icon: "apps" },
  { id: "downloads", icon: "downloads" },
  { id: "settings", icon: "settings" },
];
const ENGINES = ["web", "news", "videos", "wiki"];
const IOS = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const TOUCH = matchMedia("(pointer: coarse)").matches;

const cache = Object.assign({ version: "", admin: false, models: null, health: null, settings: null, jobs: [], lang: "", at: 0 }, store.get("cache", {}));
const memo = new Map();
const VIEWS = {};
let current = { view: TABS.some((tab) => tab.id === store.get("tab")) ? store.get("tab") : "home", params: {} };
let trail = [];

function saveCache() {
  store.set("cache", cache);
}

// Daily AI allowance of people who are not administrators (null for administrators).
function onQuota(quota) {
  cache.quota = quota || null;
  saveCache();
  bus.emit("quota");
}

function quotaLeft(kind) {
  const item = cache.quota && cache.quota[kind];
  return item && item.limit ? Math.max(0, item.limit - item.used) : null;
}

function quotaChip(kind) {
  if (!cache.quota) return null;
  if (cache.quota.low && kind !== "image") return h("span", { class: "chip warn" }, icon("bolt"), t("quota_low_chip"));
  const left = quotaLeft(kind);
  if (left === null) return null;
  const item = cache.quota[kind];
  if (!left) return h("span", { class: "chip warn" }, icon("clock"), t("quota_none_" + kind, { time: fmtTime(cache.quota.reset) }));
  return h("span", { class: "chip" }, icon("user"), t("quota_left_" + kind, { n: left, m: item.limit }));
}

function quotaNote() {
  const box = h("div", {});
  const draw = () => {
    if (!cache.quota) return fill(box);
    const left = (kind) => { const value = quotaLeft(kind); return value === null ? "∞" : String(value); };
    fill(box, h("p", { class: `note ${cache.quota.low ? "warn" : ""}` }, icon(cache.quota.low ? "bolt" : "user"), h("span", { class: "grow" },
      t("quota_note", { ask: left("ask"), image: left("image"), code: left("code"), time: fmtTime(cache.quota.reset) }),
      cache.quota.low ? " " + t("quota_low_note") : "")));
  };
  bus.on("quota", draw);
  draw();
  return box;
}

const bus = {
  local: [],
  global: [],
  on(kind, fn) {
    this.local.push([kind, fn]);
  },
  always(kind, fn) {
    this.global.push([kind, fn]);
  },
  emit(kind, data) {
    for (const [name, fn] of [...this.global, ...this.local]) if (name === kind) fn(data);
  },
};

// ---------------------------------------------------------------------------
// Shell and navigation.
// ---------------------------------------------------------------------------
function chrome() {
  const button = (tab) => h("button", { class: "tab", "data-tab": tab.id, onclick: () => goTab(tab.id) },
    h("span", { class: "tab-icon" }, icon(tab.icon), tab.id === "downloads" ? h("i", { class: "tab-badge", hidden: true }) : null), h("span", {}, t("tab_" + tab.id)));
  fill($("#rail"), h("div", { class: "rail-brand" }, icon("cat"), h("span", {}, "Michiflin ", h("b", {}, "AI"))), ...TABS.map(button));
  fill($("#tabs"), ...TABS.filter((tab) => tab.id !== "settings").map(button));
  fill($("#back"), icon("back"));
  $("#back").setAttribute("aria-label", t("back"));
  $("#back").onclick = back;
  fill($("#gear"), icon("settings"));
  $("#gear").setAttribute("aria-label", t("tab_settings"));
  $("#gear").onclick = () => goTab("settings");
  $("#activity").onclick = pendingSheet;
  activity();
  jobBadge();
}

// Count of downloads still in progress on the Downloads tab, so a started download is easy to find.
let badgeCount = -1;
function jobBadge() {
  const count = cache.jobs.filter((job) => !["done", "failed", "cancelled"].includes(job.status)).length;
  document.querySelectorAll(".tab-badge").forEach((node) => {
    node.hidden = count === 0;
    node.textContent = count > 99 ? "99+" : String(count);
    if (count > badgeCount && badgeCount >= 0) {
      node.classList.remove("pop");
      void node.offsetWidth;
      node.classList.add("pop");
    }
    node.closest(".tab").setAttribute("aria-label", count ? `${t("tab_downloads")} · ${t("downloads_active", { n: count })}` : t("tab_downloads"));
  });
  badgeCount = count;
}

function goTab(tab) {
  trail = [];
  current = { view: tab, params: {} };
  store.set("tab", tab);
  render();
}

function go(view, params = {}) {
  trail.push({ ...current, scroll: $("#view").scrollTop });
  current = { view, params };
  render();
}

function swap(view, params = {}) {
  current = { view, params };
  render();
}

function back() {
  const previous = trail.pop();
  current = previous || { view: VIEWS[current.view].tab || "home", params: {} };
  render(previous ? previous.scroll : 0);
}

function render(scroll = 0) {
  const definition = VIEWS[current.view];
  bus.local = [];
  $("#title").textContent = definition.title(current.params);
  $("#back").hidden = trail.length === 0;
  const tab = definition.tab || current.view;
  document.querySelectorAll(".tab").forEach((node) => node.classList.toggle("on", node.dataset.tab === tab));
  $("#gear").classList.toggle("on", tab === "settings");
  const main = $("#view");
  main.className = "view enter";
  main.replaceChildren();
  definition.render(main, current.params);
  main.scrollTop = scroll;
  requestAnimationFrame(() => requestAnimationFrame(() => main.classList.remove("enter")));
}

function activity() {
  const pending = Object.keys(bridge.pending()).length;
  const node = $("#activity");
  node.hidden = pending === 0;
  fill(node, h("i", { class: "spinner" }), h("span", {}, String(pending)));
  node.setAttribute("aria-label", t("pending_title"));
}

// Translated text with a live elapsed counter in place of {time}.
function pendingState(meta) {
  const [key, at, vars] = meta.ack ? ["pending_working", meta.ack, {}]
    : meta.tries ? ["pending_retried", meta.sent, { n: meta.tries }]
    : ["pending_sent", meta.ts, {}];
  const [before, after = ""] = t(key, { ...vars, time: "\u0001" }).split("\u0001");
  return [before, since(at / 1000), after];
}

function opLabel(op) {
  const key = "op_" + op;
  const text = t(key);
  return text === key ? t("op_action") : text;
}

function pendingSheet() {
  const pending = bridge.pending();
  const rows = Object.entries(pending).sort((a, b) => b[1].ts - a[1].ts).map(([id, meta]) =>
    h("div", { class: "list-row" },
      h("i", { class: "spinner" }),
      h("div", { class: "grow" }, h("b", {}, opLabel(meta.op)), h("small", { class: "muted" }, ...pendingState(meta))),
      meta.p ? h("button", { class: "icon-btn tiny", title: t("retry"), "aria-label": t("retry"), onclick: () => { bridge.resend(id); toast(t("retry_sent"), "ok"); closeSheet(); } }, icon("refresh")) : null,
      h("button", { class: "icon-btn tiny", title: t("cancel"), "aria-label": t("cancel"), onclick: () => { bridge.forget(id); closeSheet(); } }, icon("close"))));
  openSheet(t("pending_title"), [
    h("p", { class: "muted small" }, t("pending_hint")),
    rows.length ? rows : h("p", { class: "empty" }, t("pending_none")),
    syncTools(closeSheet),
  ]);
}

// Repair tools: shown in the pending sheet and in Settings, so a stuck sync never needs a deleted chat.
function syncTools(after = () => {}) {
  return h("div", { class: "sync-tools" },
    h("p", { class: "muted small" }, t("sync_hint")),
    h("div", { class: "row wrap" },
      h("button", { class: "btn small", onclick: () => {
        bridge.repair();
        hello(true);
        toast(t("sync_repaired"), "ok");
        after();
      } }, icon("refresh"), " ", t("sync_repair")),
      h("button", { class: "btn ghost small", onclick: () => confirmBox(t("reinstall_title"), t("reinstall_body"), t("reinstall_go")).then((yes) => {
        if (!yes) return;
        bridge.call("reinstall").then(() => toast(t("reinstall_sent"), "ok")).catch((error) => toast(error.message, "bad"));
        after();
      }) }, icon("downloads"), " ", t("reinstall"))));
}

// ---------------------------------------------------------------------------
// Shared building blocks.
// ---------------------------------------------------------------------------
function section(title, ...children) {
  return h("section", { class: "section" }, title ? h("h3", { class: "section-title" }, title) : null, ...children);
}

function card(...children) {
  return h("div", { class: "card" }, ...children);
}

function empty(text, name = "info") {
  return h("div", { class: "empty" }, icon(name, "big"), h("p", {}, text));
}

function loading(text) {
  return h("div", { class: "loading" }, h("i", { class: "spinner" }), h("span", {}, text || t("waiting")), since(now()),
    h("small", { class: "muted" }, t("waiting_hint")));
}

function failure(message, retry) {
  return h("div", { class: "failure" }, icon("alert"), h("p", {}, message), retry ? h("button", { class: "btn small", onclick: retry }, icon("refresh"), t("retry")) : null);
}

function iconButton(name, label, onclick, extra = "") {
  return h("button", { class: `icon-btn ${extra}`, "aria-label": label, title: label, onclick }, icon(name));
}

function headRow(title, ...tools) {
  return h("div", { class: "head-row" }, h("h3", { class: "section-title" }, title), h("div", { class: "row" }, ...tools));
}

function bar(value, total, extra = "") {
  const ratio = total > 0 ? Math.min(1, value / total) : 0;
  return h("div", { class: `bar ${extra}` }, h("i", { style: `width:${(ratio * 100).toFixed(1)}%` }));
}

function field(props) {
  return h("input", Object.assign({ class: "input", autocomplete: "off", autocapitalize: "off", spellcheck: false }, props));
}

function select(value, options, onchange) {
  return h("select", { class: "input", onchange: (event) => onchange(event.target.value) },
    options.map(([key, label]) => h("option", { value: key, selected: key === value }, label)));
}

function segmented(value, options, onchange) {
  const root = h("div", { class: "segmented", role: "tablist" });
  options.forEach(([key, label, iconName]) => add(root, h("button", {
    class: key === value ? "on" : "", role: "tab", "aria-selected": String(key === value),
    onclick: () => {
      root.querySelectorAll("button").forEach((node) => {
        const selected = node.dataset.key === key;
        node.classList.toggle("on", selected);
        node.setAttribute("aria-selected", String(selected));
      });
      onchange(key);
    },
    "data-key": key,
  }, iconName ? icon(iconName) : null, label)));
  return root;
}

function autosize(area) {
  const fit = () => {
    area.style.height = "auto";
    area.style.height = `${Math.min(area.scrollHeight, 180)}px`;
  };
  area.addEventListener("input", fit);
  requestAnimationFrame(fit);
  return area;
}

function isUrl(text) {
  return /^(https?:\/\/)?([\w-]+\.)+[a-z]{2,}(\/\S*)?$/i.test(text.trim()) && !/\s/.test(text.trim());
}

function normalUrl(text) {
  const value = text.trim();
  return /^https?:\/\//i.test(value) ? value : `https://${value}`;
}

function remember(key, value, limit = 8) {
  const list = store.get(key, []).filter((item) => JSON.stringify(item) !== JSON.stringify(value));
  list.unshift(value);
  store.set(key, list.slice(0, limit));
}

// Request with in-memory reuse, so going back does not cost another e-mail.
async function fetchView(target, op, args, draw, options = {}) {
  const key = op + JSON.stringify(args);
  if (memo.has(key) && !options.fresh) {
    draw(memo.get(key));
    return;
  }
  fill(target, loading(options.label));
  try {
    const data = await bridge.call(op, args);
    memo.set(key, data);
    if (target.isConnected) draw(data);
  } catch (error) {
    if (target.isConnected && !error.cancelled) fill(target, failure(error.message, () => fetchView(target, op, args, draw, { ...options, fresh: true })));
  }
}

function notice(text, kind = "info") {
  const list = store.get("notices", []);
  list.unshift({ id: Date.now(), ts: now(), text: plain(text), kind });
  store.set("notices", list.slice(0, 30));
  bus.emit("notices");
}

// ---------------------------------------------------------------------------
// Bot replies: tap commands become buttons, links open in the reader.
// ---------------------------------------------------------------------------
function actionLabel(action) {
  const quality = action.a && action.a[1] && /^\d+$/.test(action.a[1]) ? action.a[1] : "";
  const unit = action.k === "video" ? "p" : action.k === "audio" ? " kbps" : "";
  const base = has("act_" + action.k) ? t("act_" + action.k) : action.t;
  if (action.k === "resend" && action.a && action.a[1]) return `${base} ${action.a[1]}`;
  if (action.k === "more" && action.a && action.a[1]) return t("act_page", { n: action.a[1] });
  return quality ? `${base} ${quality}${unit}` : base;
}

async function runCommand(text, button) {
  if (button) button.classList.add("busy");
  try {
    const reply = await bridge.call("command", { text });
    if (reply.jobs) setJobs(reply.jobs);
    if (reply.page) {
      memo.set("read" + JSON.stringify({ url: reply.page.url, page: reply.page.page }), reply.page);
      go("reader", { url: reply.page.url, page: reply.page.page });
    } else {
      replySheet(reply);
    }
  } catch (error) {
    if (!error.cancelled) toast(error.message, "bad");
  } finally {
    if (button) button.classList.remove("busy");
  }
}

function hooksFor(actions = []) {
  const known = new Map(actions.map((action) => [action.t, action]));
  return {
    link: (url) => go("reader", { url }),
    command: (token) => {
      const action = known.get(token);
      if (!action) return h("code", {}, token);
      const button = h("button", { class: "chip action", onclick: () => runCommand(token, button) }, actionLabel(action));
      return button;
    },
  };
}

function replyBody(reply) {
  const parts = [];
  if (reply.text) parts.push(rich(reply.text, hooksFor(reply.actions)));
  if (reply.files && reply.files.length) parts.push(h("p", { class: "note" }, icon("chat"), t("files_in_chat", { n: reply.files.length })));
  return parts;
}

function replySheet(reply) {
  openSheet(t("reply"), replyBody(reply).length ? replyBody(reply) : h("p", { class: "muted" }, t("done")));
}

// ---------------------------------------------------------------------------
// Data from the bot.
// ---------------------------------------------------------------------------
function setJobs(jobs) {
  cache.jobs = jobs;
  saveCache();
  jobBadge();
  bus.emit("jobs");
}

function upsertJob(job) {
  const jobs = cache.jobs.filter((item) => item.id !== job.id);
  jobs.push(job);
  jobs.sort((a, b) => Number(a.id) - Number(b.id));
  setJobs(jobs.slice(-25));
}

let syncing = null;

function hello(force = false) {
  if (!force && cache.at && now() - cache.at < 900 && cache.lang === lang()) return Promise.resolve();
  syncing = syncing || sync(force).finally(() => { syncing = null; });
  return syncing;
}

async function sync(force) {
  try {
    const data = await bridge.call("hello");
    Object.assign(cache, { version: data.version, admin: data.admin, models: data.models, health: data.health, settings: data.settings,
      jobs: data.jobs, lang: lang(), at: now() });
    saveCache();
    jobBadge();
    ["models", "health", "settings", "jobs", "hello"].forEach((kind) => bus.emit(kind));
  } catch (error) {
    if (force && !error.cancelled) toast(error.message, "bad");
  }
}

bridge.pushes.notice = (data) => notice(lang() === "en" ? data.en : data.es, data.kind);

bridge.pushes.job = (job) => {
  const previous = cache.jobs.find((item) => item.id === job.id);
  upsertJob(job);
  if (!previous || previous.status !== job.status) {
    if (job.status === "done") notice(t("notice_done", { name: job.title }), "ok");
    else if (job.status === "failed") notice(t("notice_failed", { name: job.title }), "bad");
  }
};

bridge.watchers.add(activity);

// ---------------------------------------------------------------------------
// Home.
// ---------------------------------------------------------------------------
const TILES = [
  ["ai", "tile_agent", "chat", { space: "agent" }],
  ["image", "tile_images", "images"],
  ["youtube", "tile_youtube", "youtube"],
  ["translate", "tile_translate", "translate"],
  ["rates", "tile_rates", "rates"],
  ["weather", "tile_weather", "weather"],
  ["news", "tile_news", "news"],
  ["tools", "tile_tools", "tools"],
  ["join", "tile_join", "join"],
  ["health", "tile_health", "health"],
  ["notes", "tile_notes", "notes"],
];

function tiles(extra = [], omit = []) {
  const list = [...TILES, ...extra].filter(([name]) => !omit.includes(name));
  if (IOS) list.sort((a, b) => (b[0] === "join") - (a[0] === "join"));
  return h("div", { class: "tiles" }, list.map(([name, label, view, params]) =>
    h("button", { class: `tile tile-${name}`, onclick: () => go(view, params || {}) },
      h("span", { class: "tile-icon" }, icon(name)), h("span", { class: "tile-label" }, t(label)),
      name === "join" && IOS ? h("span", { class: "badge" }, "iOS") : null)));
}

function greeting() {
  const hour = new Date().getHours();
  return t(hour < 5 ? "greet_late" : hour < 12 ? "greet_morning" : hour < 19 ? "greet_afternoon" : "greet_evening");
}

function greetingLine() {
  const hour = new Date().getHours();
  return t(hour < 5 ? "home_sub_late" : hour < 12 ? "home_sub_morning" : hour < 19 ? "home_sub" : "home_sub_evening");
}

function statusChips() {
  if (!cache.health) return [h("span", { class: "chip" }, h("i", { class: "spinner" }), t("connecting"))];
  const active = cache.jobs.filter((job) => !["done", "failed", "cancelled"].includes(job.status)).length;
  return [
    h("span", { class: "chip" }, icon("clock"), t("restart_in"), " ", until(cache.health.restart, t("restarting"))),
    h("button", { class: "chip", onclick: () => goTab("downloads") }, icon("downloads"), t("active_jobs", { n: active })),
    h("span", { class: "chip subtle" }, icon("refresh"), t("updated"), " ", since(cache.at)),
  ];
}

function searchBox(initial = {}, replace = false) {
  let engine = initial.engine || prefs.engine;
  let mode = initial.mode || prefs.searchMode || "standard";
  const modeButton = h("button", { class: "mode-btn", type: "button" });
  const drawMode = () => {
    modeButton.classList.toggle("on", mode === "advanced");
    modeButton.title = t(mode === "advanced" ? "mode_advanced_hint" : "mode_standard_hint");
    fill(modeButton, icon(mode === "advanced" ? "bolt" : "search"), h("span", {}, t("mode_" + mode)));
  };
  modeButton.onclick = () => {
    mode = mode === "advanced" ? "standard" : "advanced";
    prefs.searchMode = mode;
    savePrefs();
    drawMode();
    toast(t(mode === "advanced" ? "mode_advanced_hint" : "mode_standard_hint"));
  };
  drawMode();
  const input = field({ type: "search", placeholder: t("search_placeholder"), value: initial.q || "", enterkeyhint: "search" });
  const submit = () => {
    const q = input.value.trim();
    if (!q) return;
    const move = replace ? swap : go;
    if (isUrl(q)) {
      move("reader", { url: normalUrl(q) });
      return;
    }
    remember("recent", { q, engine, mode });
    move(engine === "videos" ? "youtube" : "results", { q, engine, mode });
  };
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      submit();
    }
  });
  return h("div", { class: "searchbox" },
    h("div", { class: "search-field" }, modeButton, input, h("button", { class: "btn primary small", onclick: submit }, t("go"))),
    segmented(engine, ENGINES.map((key) => [key, t("engine_" + key)]), (value) => { engine = value; }));
}

function noticeList() {
  const box = h("div", { class: "card list" });
  const draw = () => {
    const list = store.get("notices", []);
    fill(box, ...(list.length ? list.slice(0, 8).map((item) =>
      h("div", { class: `list-row notice ${item.kind}` }, icon(item.kind === "ok" ? "check" : item.kind === "bad" ? "alert" : "bell"),
        h("span", { class: "grow" }, item.text), h("small", { class: "muted" }, fmtTime(item.ts)))) : [h("p", { class: "empty small" }, t("notices_none"))]));
  };
  bus.on("notices", draw);
  draw();
  return box;
}

VIEWS.home = {
  title: () => "Michiflin AI",
  render(main) {
    const status = h("div", { class: "chips" });
    const drawStatus = () => fill(status, ...statusChips());
    ["health", "jobs", "hello"].forEach((kind) => bus.on(kind, drawStatus));
    drawStatus();
    add(main, 
      h("div", { class: "hero" },
        h("div", {}, h("h2", {}, greeting()), h("p", {}, greetingLine())),
        h("div", { class: "hero-art" }, icon("cat"))),
      status,
      searchBox(),
      section(t("shortcuts"), tiles([], ["health"])),
      section(null, headRow(t("notices"), iconButton("trash", t("clear"), () => { store.set("notices", []); bus.emit("notices"); })), noticeList()),
      h("button", { class: "card link-card", onclick: () => go("help") }, icon("help"),
        h("span", { class: "grow" }, h("b", {}, t("help_title")), h("small", {}, t("help_sub"))), icon("next")));
  },
};

const HELP = [["how", "info"], ["ai", "ai"], ["coder", "code"], ["web", "web"], ["downloads", "downloads"], ["join", "join"], ["save", "bolt"], ["privacy", "shield"]];

VIEWS.help = {
  tab: "home",
  title: () => t("help_title"),
  render(main) {
    add(main, h("button", { class: "btn primary wide", onclick: () => onboarding(true) }, icon("cat"), " ", t("tour_again")));
    add(main, ...HELP.map(([key, name]) => h("details", { class: "card help", open: key === "how" },
      h("summary", {}, icon(name), h("b", {}, t(`help_${key}_t`)), icon("down", "caret")),
      rich(t(`help_${key}_b`), hooksFor()))));
  },
};

// ---------------------------------------------------------------------------
// AI.
// ---------------------------------------------------------------------------
function modelInfo(key) {
  for (const provider of (cache.models && cache.models.providers) || []) {
    const model = provider.models.find((item) => item.key === key);
    if (model) return { provider, model };
  }
  return null;
}

function spaceTitle(space) {
  if (space === "agent") return t("agent");
  if (space === "auto") return t("auto");
  const info = modelInfo(space);
  return info ? info.model.title : space.split(":").pop();
}

function spaceCard(name, title, hint, onclick, badge) {
  return h("button", { class: "card space-card", onclick }, h("span", { class: "space-icon" }, icon(name)),
    h("span", { class: "grow" }, h("b", {}, title), h("small", {}, hint)), badge ? h("span", { class: "badge ok" }, badge) : icon("next"));
}

function role(name) {
  return has("role_" + name) ? t("role_" + name) : name;
}

const REASONS = { auth: "reason_auth", model: "reason_model", quota: "reason_quota", busy: "reason_busy", connection: "reason_connection", response: "reason_response" };

// Broken for the day: rejected key or a model no longer offered for free.
function modelBroken(model, provider) {
  return (provider && provider.cooldown > now() && provider.reason === "auth") || (model.cooldown > now() && model.reason === "model");
}

function modelState(model, provider) {
  if (provider && provider.cooldown > now() && provider.reason === "auth") return h("span", { class: "chip bad" }, icon("alert"), t("reason_auth"));
  if (model.cooldown > now() && model.reason === "model") return h("span", { class: "chip bad" }, icon("alert"), t("reason_model"));
  if (model.cooldown > now()) {
    return h("span", { class: "chip warn" }, icon("clock"), REASONS[model.reason] ? t(REASONS[model.reason]) + " · " : "", until(model.cooldown, t("available")));
  }
  if (model.remaining) return h("span", { class: "chip" }, t("limit_left", { n: model.remaining[0], m: model.remaining[1] }));
  return h("span", { class: "chip" }, t("uses_left", { n: Math.max(0, model.daily - model.used) }));
}

async function pinProvider(key) {
  try {
    cache.settings = await bridge.call("settings", { pin: key });
    cache.models.pin = key;
    saveCache();
    bus.emit("models");
    toast(key ? t("pinned") : t("unpinned"), "ok");
  } catch (error) {
    if (!error.cancelled) toast(error.message, "bad");
  }
}

function modelSections() {
  const data = cache.models;
  if (!data) return [empty(t("models_none"), "ai")];
  const out = [h("p", { class: "muted small" }, icon("clock"), t("quota_reset"), " ", until(data.reset, t("now")))];
  for (const provider of data.providers) {
    const pinned = data.pin === provider.key;
    out.push(card(
      h("div", { class: "provider-head" },
        h("div", { class: "grow" }, h("b", {}, provider.name), h("small", { class: "muted" }, t("used_of", { n: provider.used, m: provider.daily }))),
        provider.cooldown > now() ? h("span", { class: "chip warn" }, icon("clock"), until(provider.cooldown, t("available"))) : null,
        iconButton("pin", pinned ? t("unpin") : t("pin"), () => pinProvider(pinned ? "" : provider.key), pinned ? "on" : "")),
      bar(provider.used, provider.daily),
      h("div", { class: "list" }, provider.models.map((model) =>
        h("button", { class: "list-row model-row" + (modelBroken(model, provider) ? " off" : ""), onclick: () => go("chat", { space: model.key }) },
          brandMark(brandOf(model, provider)),
          h("div", { class: "grow" }, h("b", {}, model.title), h("div", { class: "roles" }, model.roles.map((name) => h("span", { class: "tag" }, role(name))))),
          modelState(model, provider), icon("next"))))));
  }
  return out;
}

async function refreshModels(button) {
  button.classList.add("busy");
  try {
    cache.models = await bridge.call("models");
    saveCache();
    bus.emit("models");
  } catch (error) {
    if (!error.cancelled) toast(error.message, "bad");
  } finally {
    button.classList.remove("busy");
  }
}

VIEWS.ai = {
  title: () => t("tab_ai"),
  render(main) {
    const list = h("div", { class: "stack" });
    const draw = () => fill(list, ...modelSections());
    bus.on("models", draw);
    draw();
    const refresh = iconButton("refresh", t("refresh"), () => refreshModels(refresh));
    add(main, 
      h("div", { class: "stack" },
        spaceCard("bolt", t("agent"), t("agent_hint"), () => go("chat", { space: "agent" }), t("no_quota")),
        spaceCard("ai", t("auto"), t("auto_hint"), () => go("chat", { space: "auto" })),
        spaceCard("code", t("coder"), t("coder_hint"), () => go("coder")),
        spaceCard("image", t("tile_images"), t("images_hint"), () => go("images")),
        quotaNote()),
      section(null, headRow(t("models"), refresh), list));
  },
};

function messages(space, settled = true) {
  const list = store.get("chat." + space, []);
  const pending = bridge.pending();
  for (const item of list) {
    if (settled && item.pending && !pending[item.rid]) {
      item.pending = false;
      item.error = t("no_answer");
    }
  }
  return list;
}

function saveMessages(space, list) {
  store.set("chat." + space, list.slice(-80));
  bus.emit("chat:" + space);
}

const attachments = new Map(); // message time -> attachment, kept in memory for retries only

function ask(space, text, file = null) {
  const list = messages(space);
  const history = space === "agent" ? [] : list.filter((item) => !item.pending && !item.error && (item.text || item.file))
    .slice(-12).map((item) => [item.role, item.file ? `${item.text}\n[${item.file.name}]`.trim() : item.text]);
  const args = { space, text, history };
  if (file) args.file = { name: file.name, mime: file.mime, b64: file.b64 };
  const { id } = bridge.request("ask", args, { space });
  if (!id) return false;
  const ts = now();
  if (file) attachments.set(ts, file);
  list.push({ role: "user", text, ts, file: file ? { name: file.name, size: file.size, kind: file.kind } : null },
    { role: "assistant", text: "", ts, rid: id, pending: true });
  saveMessages(space, list);
  return true;
}

const ATTACH_MAX = 88000; // base64 grows it to ~117 KB; one webxdc update holds 128 KB
const DOC_FILE = /\.(txt|md|rst|csv|tsv|json|xml|ya?ml|html?|log|srt|vtt|ini|toml|sql|sh|py|js|ts|css|c|cc|cpp|h|java|kt|go|rs|rb|php|docx|xlsx|pptx|odt|ods|odp|epub|rtf|pdf)$/i;
const AUDIO_TYPES = { mp3: "audio/mpeg", m4a: "audio/mp4", aac: "audio/aac", ogg: "audio/ogg", opus: "audio/ogg", wav: "audio/wav", flac: "audio/flac" };

function spaceAccepts(space) {
  if (space === "agent") return [];
  if (space === "auto") return ["document", "image"];
  const info = modelInfo(space);
  return info && info.model.accepts ? info.model.accepts : ["document", "image", "pdf", "audio"]; // unknown yet: the bot decides
}

async function shrinkImage(file, limit) {
  const url = URL.createObjectURL(file);
  try {
    const picture = await new Promise((resolve, reject) => {
      const element = new Image();
      element.onload = () => resolve(element);
      element.onerror = () => reject(new Error("image"));
      element.src = url;
    });
    let side = 1280;
    let quality = 0.82;
    for (let attempt = 0; attempt < 12; attempt += 1) {
      const scale = Math.min(1, side / Math.max(picture.naturalWidth, picture.naturalHeight));
      const canvas = h("canvas", { width: Math.max(1, Math.round(picture.naturalWidth * scale)), height: Math.max(1, Math.round(picture.naturalHeight * scale)) });
      canvas.getContext("2d").drawImage(picture, 0, 0, canvas.width, canvas.height);
      const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", quality));
      if (blob && blob.size <= limit) return blob;
      if (quality > 0.5) quality -= 0.12;
      else side = Math.round(side * 0.75);
    }
    throw new Error("size");
  } finally {
    URL.revokeObjectURL(url);
  }
}

async function prepareAttachment(file, accepts) {
  const extension = extOf(file.name);
  const type = file.type || AUDIO_TYPES[extension] || (extension === "pdf" ? "application/pdf" : "");
  if (type.startsWith("image/")) {
    if (!accepts.includes("image")) throw new Error(t("attach_no_image"));
    const blob = await shrinkImage(file, ATTACH_MAX - 2000);
    return { name: file.name.replace(/\.[^.]+$/, "") + ".jpg", mime: "image/jpeg", size: blob.size, kind: "image", b64: await blobToBase64(blob) };
  }
  const audio = type.startsWith("audio/");
  if (audio && !accepts.includes("audio")) throw new Error(t("attach_no_audio"));
  if (!audio && !DOC_FILE.test(file.name) && !type.startsWith("text/")) throw new Error(t("attach_type"));
  if (file.size > ATTACH_MAX) throw new Error(t("attach_big", { size: fmtSize(ATTACH_MAX) }));
  return { name: file.name, mime: type || "application/octet-stream", size: file.size, kind: audio ? "audio" : "document", b64: await blobToBase64(file) };
}

function attachmentLine(file) {
  return h("div", { class: "attach-line" }, icon(file.kind === "image" ? "image" : file.kind === "audio" ? "audio" : "file"),
    h("span", { class: "clamp1" }, file.name), h("small", { class: "muted" }, fmtSize(file.size)));
}

bridge.handlers.ask = (data, error, ctx, rid) => {
  const list = messages(ctx.space, false);
  const item = list.find((entry) => entry.rid === rid);
  if (!item) return;
  Object.assign(item, { pending: false, ts: now() });
  if (error) {
    item.error = error;
  } else {
    Object.assign(item, { text: data.text, actions: data.actions || [], sig: data.sig || "", files: (data.files || []).length });
    if (data.models) {
      cache.models = data.models;
      saveCache();
      bus.emit("models");
    }
    if (data.jobs) setJobs(data.jobs);
  }
  saveMessages(ctx.space, list);
  if (current.view !== "chat" || current.params.space !== ctx.space) notice(t("notice_answer", { name: spaceTitle(ctx.space) }));
};

function spaceMeta(space) {
  if (space === "agent") return [h("span", { class: "chip ok" }, icon("bolt"), t("no_quota")), h("span", { class: "chip subtle" }, t("agent_meta"))];
  if (space === "auto") return [h("span", { class: "chip" }, icon("ai"), t("auto_meta")), quotaChip("ask")];
  const info = modelInfo(space);
  if (!info) return [h("span", { class: "chip warn" }, t("model_unknown"))];
  const { model, provider } = info;
  const chips = [h("span", { class: "chip" }, t("uses_left_of", { n: Math.max(0, model.daily - model.used), m: model.daily })), modelState(model, provider), quotaChip("ask")];
  if (provider.cooldown > now() && provider.reason !== "auth") {
    chips.push(h("span", { class: "chip warn" }, provider.name, " · ", REASONS[provider.reason] ? t(REASONS[provider.reason]) + " · " : "", until(provider.cooldown, t("available"))));
  }
  chips.push(h("span", { class: "chip subtle" }, icon("clock"), t("quota_reset"), " ", until(cache.models.reset, t("now"))));
  return chips;
}

function bubble(space, item, list) {
  if (item.role === "user") return h("div", { class: "bubble me" }, item.file ? attachmentLine(item.file) : null, item.text ? h("div", { class: "bubble-text" }, item.text) : null);
  if (item.pending) {
    return h("div", { class: "bubble bot pending" }, h("span", { class: "dots" }, h("i"), h("i"), h("i")),
      h("small", { class: "muted" }, t("thinking"), " ", since(item.ts)),
      h("button", { class: "link small", onclick: () => { bridge.forget(item.rid); saveMessages(space, list.filter((entry) => entry !== item)); } }, t("cancel")));
  }
  if (item.error) {
    const index = list.indexOf(item);
    const question = list.slice(0, index).reverse().find((entry) => entry.role === "user");
    const retry = () => { saveMessages(space, list.filter((entry) => entry !== item && entry !== question)); ask(space, question.text, attachments.get(question.ts) || null); };
    const automatic = () => { ask("auto", question.text); go("chat", { space: "auto" }); };
    return h("div", { class: "bubble bot error" }, icon("alert"), h("span", { class: "grow" }, item.error),
      question ? h("div", { class: "error-actions" },
        h("button", { class: "btn small ghost", onclick: retry }, icon("refresh"), t("retry")),
        space !== "auto" && space !== "agent" ? h("button", { class: "btn small ghost", onclick: automatic }, icon("ai"), t("use_auto")) : null) : null);
  }
  return h("div", { class: "bubble bot" },
    replyBody({ text: item.text, actions: item.actions, files: item.files ? new Array(item.files) : [] }),
    h("div", { class: "bubble-foot" }, item.sig ? h("small", { class: "muted" }, item.sig) : h("small", { class: "muted" }, fmtTime(item.ts)),
      iconButton("copy", t("copy"), () => copyText(item.text), "tiny")));
}

function chatIntro(space, send) {
  const keys = space === "agent" ? ["sugg_agent_1", "sugg_agent_2", "sugg_agent_3", "sugg_agent_4"] : ["sugg_ai_1", "sugg_ai_2", "sugg_ai_3"];
  const info = modelInfo(space);
  const brand = info ? brandOf(info.model, info.provider) : "";
  return h("div", { class: "intro" }, brand ? brandMark(brand, "big") : icon(space === "agent" ? "bolt" : "ai", "big"),
    h("p", {}, t(space === "agent" ? "intro_agent" : "intro_ai")),
    h("div", { class: "chips center" }, keys.map((key) => h("button", { class: "chip", onclick: () => send(t(key)) }, t(key)))));
}

VIEWS.chat = {
  tab: "ai",
  title: (params) => spaceTitle(params.space),
  render(main, { space }) {
    main.classList.add("chat-view");
    const meta = h("div", { class: "chips chat-meta" });
    const list = h("div", { class: "messages" });
    const area = autosize(h("textarea", { class: "input", rows: 1, placeholder: t("ask_placeholder"), enterkeyhint: "send" }));
    const tray = h("div", { class: "attach-tray" });
    let attached = null;
    const drawTray = () => fill(tray, attached ? h("div", { class: "attach-chip" }, attachmentLine(attached),
      iconButton("close", t("remove"), () => { attached = null; drawTray(); }, "tiny")) : null);
    const attach = async () => {
      const [file] = await pickFiles(false);
      if (!file) return;
      try {
        attached = await prepareAttachment(file, spaceAccepts(space));
        drawTray();
      } catch (error) {
        toast(error.message === "size" || error.message === "image" ? t("attach_image_fail") : error.message, "bad");
      }
    };
    const submit = (text) => {
      const value = (text || area.value).trim();
      if (!value && !attached) return;
      if (!ask(space, value, attached)) return;
      area.value = "";
      area.dispatchEvent(new Event("input"));
      attached = null;
      drawTray();
    };
    area.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !TOUCH) {
        event.preventDefault();
        submit();
      }
    });
    const drawMeta = () => fill(meta, ...spaceMeta(space), iconButton("trash", t("clear_chat"), async () => {
      if (await confirmBox(t("clear_chat"), t("clear_chat_q"), t("clear"))) saveMessages(space, []);
    }, "tiny"));
    const drawList = () => {
      const items = messages(space);
      fill(list, ...(items.length ? items.map((item) => bubble(space, item, items)) : [chatIntro(space, submit)]));
      requestAnimationFrame(() => { main.scrollTop = main.scrollHeight; });
    };
    bus.on("models", drawMeta);
    bus.on("quota", drawMeta);
    bus.on("chat:" + space, drawList);
    add(main, meta, list, h("div", { class: "composer-wrap" }, tray, h("div", { class: "composer" },
      space !== "agent" ? iconButton("clip", t("attach_hint"), attach, "attach-btn") : null,
      area, h("button", { class: "btn primary round", "aria-label": t("send"), onclick: () => submit() }, icon("send")))));
    drawMeta();
    drawList();
  },
};

// ---------------------------------------------------------------------------
// AI images: kept on the device (IndexedDB), exportable to a chat or the gallery.
// ---------------------------------------------------------------------------
function imageSourceLabel(key) {
  const found = ((cache.models && cache.models.images) || []).find((source) => source.key === key);
  return found ? found.label : t("img_auto");
}

function createImage(prompt, model = prefs.imageModel || "") {
  const { id } = bridge.request("image", { prompt, model }, { prompt });
  if (!id) return false;
  const list = store.get("img.pending", []);
  list.unshift({ rid: id, prompt, model, ts: now() });
  store.set("img.pending", list);
  bus.emit("images");
  return true;
}

bridge.handlers.image = async (data, error, ctx, rid) => {
  store.set("img.pending", store.get("img.pending", []).filter((item) => item.rid !== rid));
  if (error) {
    notice(t("image_failed", { error }), "bad");
  } else {
    await media.put({ id: rid, prompt: data.prompt, source: data.source, model: data.model || "", mime: data.mime, b64: data.b64, ts: now() });
    if (data.models) {
      cache.models = data.models;
      saveCache();
      bus.emit("models");
    }
    if (current.view !== "images") notice(t("image_ready"), "ok");
  }
  bus.emit("images");
};

function imageViewer(item) {
  const blob = () => base64ToBlob(item.b64, item.mime);
  openSheet(t("image"), [
    h("img", { class: "viewer", src: `data:${item.mime};base64,${item.b64}`, alt: item.prompt }),
    h("p", {}, item.prompt),
    h("p", { class: "muted small" }, `${item.source} · ${fmtTime(item.ts)} · ${fmtSize(item.b64.length * 0.75)}`),
    h("div", { class: "row wrap" },
      h("button", { class: "btn primary", onclick: () => exportSheet(blob(), `${niceName(item.prompt, "michiflin-" + item.id)}.jpg`, item.prompt) }, icon("share"), t("export")),
      h("button", { class: "btn ghost", onclick: () => { closeSheet(); createImage(item.prompt, item.model || ""); } }, icon("refresh"), t("again")),
      h("button", { class: "btn ghost", onclick: () => copyText(item.prompt) }, icon("copy"), t("copy_prompt")),
      h("button", { class: "btn ghost danger-text", onclick: async () => { await media.remove(item.id); closeSheet(); bus.emit("images"); } }, icon("trash"), t("delete"))),
  ]);
}

VIEWS.images = {
  tab: "ai",
  title: () => t("tile_images"),
  render(main) {
    const area = autosize(h("textarea", { class: "input", rows: 2, placeholder: t("images_placeholder") }));
    const quotas = h("div", { class: "chips" });
    const waiting = h("div", { class: "stack" });
    const gallery = h("div", { class: "gallery" });
    const drawQuotas = () => {
      const list = (cache.models && cache.models.images) || [];
      if (prefs.imageModel && !list.some((source) => source.key === prefs.imageModel)) prefs.imageModel = "";
      const pick = (key) => { prefs.imageModel = key; savePrefs(); drawQuotas(); if (key.startsWith("horde:")) toast(t("img_horde_slow")); };
      fill(quotas, quotaChip("image"),
        h("button", { class: `chip ${!prefs.imageModel ? "on" : ""}`, onclick: () => pick("") }, icon("swap"), t("img_auto")),
        ...list.map((source) => {
          const resting = source.cooldown > now();
          return h("button", { class: `chip ${prefs.imageModel === source.key ? "on" : ""} ${resting ? "warn" : ""}`,
            title: resting ? t("img_resting", { time: fmtTime(source.cooldown) }) : t("used_of", { n: source.used, m: source.daily }),
            onclick: () => (resting ? toast(t("img_resting", { time: fmtTime(source.cooldown) }), "bad") : pick(source.key)) },
          source.label, h("small", { class: "muted" }, resting ? ` · ${fmtTime(source.cooldown)}` : ` ${source.used}/${source.daily}`));
        }),
        ...(() => {
          const spent = list.find((source) => source.key.startsWith("cf:") && source.reason === "quota" && source.cooldown > now());
          return spent ? [h("p", { class: "note warn wide" }, icon("info"), t("img_cf_spent", { time: fmtTime(spent.cooldown) }))] : [];
        })());
    };
    const draw = async () => {
      const pending = bridge.pending();
      const list = store.get("img.pending", []).filter((item) => pending[item.rid]);
      store.set("img.pending", list);
      fill(waiting, ...list.map((item) => h("div", { class: "card row" }, h("i", { class: "spinner" }),
        h("span", { class: "grow" }, h("span", { class: "clamp" }, item.prompt), h("small", { class: "muted" }, imageSourceLabel(item.model || ""))), since(item.ts))));
      const items = await media.all();
      fill(gallery, ...(items.length ? items.map((item) => h("button", { class: "thumb", onclick: () => imageViewer(item) },
        h("img", { src: `data:${item.mime};base64,${item.b64}`, alt: item.prompt, loading: "lazy" }))) : [empty(t("images_none"), "image")]));
    };
    bus.on("images", draw);
    bus.on("models", drawQuotas);
    bus.on("quota", drawQuotas);
    add(main, card(area, h("div", { class: "row end" }, h("button", { class: "btn primary", onclick: () => {
      const prompt = area.value.trim();
      if (prompt && createImage(prompt)) area.value = "";
    } }, icon("image"), t("generate")))), section(t("img_model"), quotas), waiting, section(t("gallery"), gallery));
    drawQuotas();
    draw();
  },
};

// ---------------------------------------------------------------------------
// Web: search, reader and page actions.
// ---------------------------------------------------------------------------
function formatLabel(format) {
  if (format === "segments") return t("fmt_segments");
  return format && format !== "raw" ? format.toUpperCase() : "";
}

function hostOf(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch (error) {
    return url;
  }
}

async function runAction(kind, url, quality = 0) {
  try {
    const before = new Set(cache.jobs.map((job) => job.id));
    const reply = await bridge.call("action", { kind, url, quality });
    if (reply.jobs) setJobs(reply.jobs);
    const added = (reply.jobs || []).find((job) => !before.has(job.id));
    if (added) toast(t("queued_ok", { name: plain(added.title) }), "ok");
    else if (reply.files && reply.files.length) toast(t("check_chat"), "ok");
    else if (reply.text) replySheet(reply);
    else toast(t("done"), "ok");
  } catch (error) {
    if (!error.cancelled) toast(error.message, "bad");
  }
}

function qualitySheet(kind, url) {
  const options = kind === "video" ? [144, 240, 360, 480, 720, 1080] : [32, 48, 64, 96, 128, 160];
  const unit = kind === "video" ? "p" : " kbps";
  const settingKey = kind === "video" ? "video_height" : "audio_kbps";
  const row = cache.settings && cache.settings.flatMap((group) => group.items).find((item) => item.key === settingKey);
  const chosen = row ? Number(row.value) : 0;
  openSheet(t(kind === "video" ? "pick_video" : "pick_audio"), [
    h("p", { class: "muted small" }, t("pick_hint")),
    h("div", { class: "chips" }, options.map((value) => h("button", {
      class: `chip big ${value === chosen ? "on" : ""}`,
      onclick: () => { closeSheet(); runAction(kind, url, value); },
    }, `${value}${unit}`))),
  ]);
}

function actionBar(url, isMedia) {
  const chip = (name, label, onclick) => h("button", { class: "chip action", onclick }, icon(name), label);
  return h("div", { class: "chips actions-bar" },
    isMedia ? chip("video", t("act_video"), () => qualitySheet("video", url)) : null,
    isMedia ? chip("audio", t("act_audio"), () => qualitySheet("audio", url)) : null,
    isMedia ? chip("subtitles", t("act_transcript"), () => go("transcript", { url })) : null,
    chip("pdf", "PDF", () => runAction("pdf", url)),
    chip("camera", t("act_capture"), () => runAction("capture", url)),
    chip("downloads", t("act_download"), () => runAction("download", url)),
    chip("copy", t("copy_link"), () => copyText(url)));
}

function resultCard(item) {
  return h("article", { class: "card result" },
    h("button", { class: "result-main", onclick: () => go("reader", { url: item.url }) },
      h("small", { class: "muted" }, [item.source || hostOf(item.url), item.date].filter(Boolean).join(" · ")),
      h("b", {}, plain(item.title)),
      item.snippet ? h("p", { class: "clamp" }, plain(item.snippet)) : null),
    h("div", { class: "result-tools" },
      iconButton("pdf", "PDF", () => runAction("pdf", item.url), "tiny"),
      iconButton("camera", t("act_capture"), () => runAction("capture", item.url), "tiny"),
      iconButton("copy", t("copy_link"), () => copyText(item.url), "tiny")));
}

function resultList(data) {
  if (!data.results.length) return [empty(t("no_results"), "search")];
  return data.results.map((item) => (data.engine === "videos" ? videoCard(item) : resultCard(item)));
}

VIEWS.web = {
  title: () => t("tab_web"),
  render(main) {
    const recent = store.get("recent", []);
    const visited = store.get("visited", []);
    add(main, searchBox(),
      recent.length ? section(t("recent"), h("div", { class: "chips" }, recent.map((item) =>
        h("button", { class: "chip", onclick: () => go(item.engine === "videos" ? "youtube" : "results", item) }, icon(item.engine === "videos" ? "youtube" : "search"), item.q)))) : null,
      section(null, headRow(t("visited"), visited.length ? iconButton("trash", t("clear"), () => { store.set("visited", []); swap("web"); }) : null),
        visited.length ? h("div", { class: "card list" }, visited.map((item) =>
          h("button", { class: "list-row", onclick: () => go("reader", { url: item.url }) }, icon("web"),
            h("span", { class: "grow" }, h("b", { class: "clamp1" }, item.title), h("small", { class: "muted" }, hostOf(item.url))),
            h("small", { class: "muted" }, fmtTime(item.ts))))) : empty(t("visited_none"), "web")),
      h("p", { class: "note" }, icon("info"), t("web_hint")));
  },
};

VIEWS.results = {
  tab: "web",
  title: (params) => params.q,
  render(main, params) {
    const box = h("div", { class: "stack" });
    add(main, searchBox(params, true), box);
    fetchView(box, "search", { q: params.q, engine: params.engine, mode: params.mode }, (data) => fill(box,
      data.mode === "advanced" ? h("p", { class: "note" }, icon("bolt"), data.left ? t("mode_left", { n: data.left }) : t("mode_empty")) : null,
      ...resultList(data)), { label: t("searching") });
  },
};

function openHref(href) {
  if (!href || href.startsWith("#")) return;
  if (href.startsWith("mailto:")) {
    const body = mailBody(href).trim();
    if (body.startsWith("/")) runCommand(body);
    else if (body) go("reader", { url: normalUrl(body) });
    return;
  }
  if (/^https?:\/\//i.test(href)) go("reader", { url: href });
}

function drawPage(box, data, params) {
  if (data.file) {
    fill(box, card(h("div", { class: "row" }, icon("file", "big"), h("b", {}, t("file_link"))), replyBody(data),
      h("button", { class: "btn primary", onclick: () => goTab("downloads") }, icon("downloads"), t("tab_downloads"))));
    return;
  }
  remember("visited", { url: data.url, title: plain(data.title) || hostOf(data.url), ts: now() }, 15);
  const page = data.page || 1;
  const pager = data.pages > 1 ? h("div", { class: "pager" },
    h("button", { class: "btn ghost", disabled: page <= 1, onclick: () => swap("reader", { url: params.url, page: page - 1 }) }, icon("back"), t("previous")),
    h("span", { class: "muted" }, t("page_of", { n: page, m: data.pages })),
    h("button", { class: "btn ghost", disabled: page >= data.pages, onclick: () => swap("reader", { url: params.url, page: page + 1 }) }, t("next"), icon("next"))) : null;
  fill(box, 
    h("header", { class: "page-head" }, h("small", { class: "muted" }, hostOf(data.url)), h("h2", {}, plain(data.title))),
    actionBar(data.url, data.media),
    data.nav && data.nav.length ? h("details", { class: "card nav" }, h("summary", {}, icon("apps"), t("sections"), icon("down", "caret")),
      h("div", { class: "chips" }, data.nav.map(([label, target]) => h("button", { class: "chip", onclick: () => go("reader", { url: target }) }, plain(label))))) : null,
    sanitize(data.html, { href: openHref }),
    pager);
}

VIEWS.reader = {
  tab: "web",
  title: (params) => hostOf(params.url),
  render(main, params) {
    const box = h("div", { class: "reader" });
    add(main, box);
    fetchView(box, "read", { url: params.url, page: params.page || 1 }, (data) => drawPage(box, data, params), { label: t("loading_page") });
  },
};

// ---------------------------------------------------------------------------
// YouTube facade.
// ---------------------------------------------------------------------------
function videoCard(item) {
  return h("article", { class: "card video" },
    h("div", { class: "video-art" }, icon("youtube")),
    h("div", { class: "video-body" },
      h("b", { class: "clamp" }, plain(item.title)),
      h("small", { class: "muted" }, [item.source, item.date].filter(Boolean).join(" · ")),
      item.snippet ? h("p", { class: "clamp small" }, plain(item.snippet)) : null,
      h("div", { class: "chips" },
        h("button", { class: "chip action", onclick: () => qualitySheet("video", item.url) }, icon("video"), t("act_video")),
        h("button", { class: "chip action", onclick: () => qualitySheet("audio", item.url) }, icon("audio"), t("act_audio")),
        h("button", { class: "chip action", onclick: () => go("transcript", { url: item.url }) }, icon("subtitles"), t("act_transcript")),
        h("button", { class: "chip action", onclick: () => runAction("info", item.url) }, icon("info"), t("act_info")))));
}

VIEWS.youtube = {
  tab: "apps",
  title: () => "YouTube",
  render(main, params) {
    const input = field({ type: "search", placeholder: t("youtube_placeholder"), value: params.q || "", enterkeyhint: "search" });
    const submit = () => {
      const q = input.value.trim();
      if (!q) return;
      if (isUrl(q)) {
        openSheet(t("video_link"), [h("p", { class: "muted small clamp1" }, q), actionBar(normalUrl(q), true)]);
        return;
      }
      remember("recent", { q, engine: "videos" });
      swap("youtube", { q });
    };
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        submit();
      }
    });
    const box = h("div", { class: "stack" });
    add(main, h("div", { class: "searchbox" }, h("div", { class: "search-field" }, icon("youtube"), input,
      h("button", { class: "btn primary small", onclick: submit }, t("go")))), box);
    if (params.q) {
      fetchView(box, "search", { q: params.q, engine: "videos" }, (data) => fill(box, ...resultList(data)), { label: t("searching") });
    } else {
      add(box, empty(t("youtube_hint"), "youtube"));
    }
  },
};

VIEWS.transcript = {
  tab: "apps",
  title: () => t("act_transcript"),
  render(main, params) {
    const box = h("div", { class: "stack" });
    add(main, box);
    fetchView(box, "action", { kind: "transcript", url: params.url }, (data) => {
      const name = `${plain(data.title).replace(/[\\/:*?"<>|]+/g, "").slice(0, 60) || "transcript"}.txt`;
      fill(box, 
        h("header", { class: "page-head" }, h("small", { class: "muted" }, hostOf(params.url)), h("h2", {}, plain(data.title))),
        h("div", { class: "row wrap" },
          h("button", { class: "btn ghost", onclick: () => copyText(data.text) }, icon("copy"), t("copy")),
          h("button", { class: "btn ghost", onclick: () => exportSheet(new Blob([data.text], { type: "text/plain" }), name) }, icon("share"), t("export"))),
        h("div", { class: "card prose" }, data.text.split(/\n{2,}/).map((block) => h("p", {}, block))));
    }, { label: t("loading_transcript") });
  },
};

// ---------------------------------------------------------------------------
// Mini apps.
// ---------------------------------------------------------------------------
VIEWS.apps = {
  title: () => t("tab_apps"),
  render(main) {
    add(main, tiles(cache.admin ? [["shield", "tile_admin", "admin"]] : []), h("p", { class: "note" }, icon("info"), t("apps_hint")));
  },
};

function toolResult(box, name, arg) {
  fetchView(box, "tool", { name, arg }, (data) => fill(box, card(rich(data.text, hooksFor()))), { fresh: true });
}

function askBox({ placeholder, value = "", onsubmit, iconName = "search", label }) {
  const input = field({ type: "text", placeholder, value, enterkeyhint: "go" });
  const submit = () => input.value.trim() && onsubmit(input.value.trim());
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      submit();
    }
  });
  return h("div", { class: "search-field" }, icon(iconName), input, h("button", { class: "btn primary small", onclick: submit }, label || t("go")));
}

VIEWS.rates = {
  tab: "apps",
  title: () => t("tile_rates"),
  render(main) {
    const saved = store.get("rates", null);
    const board = h("div", { class: "stack" });
    const result = h("div", { class: "convert-out" });
    let amount = store.get("rates.amount", "100");
    let code = store.get("rates.code", "USD");
    let toCup = store.get("rates.toCup", true);
    const convert = () => {
      const data = store.get("rates", null);
      if (!data || !data.rates[code]) {
        result.textContent = "";
        return;
      }
      const value = Number(String(amount).replace(",", ".")) || 0;
      const out = toCup ? value * data.rates[code] : value / data.rates[code];
      fill(result, h("b", {}, `${fmtNumber(value, 2)} ${toCup ? code : "CUP"}`), " = ", h("b", { class: "accent" }, `${fmtNumber(out, 2)} ${toCup ? "CUP" : code}`));
      store.set("rates.amount", amount);
      store.set("rates.code", code);
      store.set("rates.toCup", toCup);
    };
    const codes = h("div", { class: "row" });
    const draw = (data) => {
      const entries = Object.entries(data.rates);
      fill(board, 
        h("div", { class: "rate-grid" }, entries.map(([name, value]) => h("div", { class: "card rate" }, h("small", { class: "muted" }, `1 ${name}`),
          h("b", {}, fmtNumber(value, 2)), h("small", { class: "muted" }, "CUP")))),
        h("p", { class: "muted small" }, icon("clock"), t("updated"), " ", since(data.at), " · elTOQUE"));
      if (!data.rates[code]) code = entries.length ? entries[0][0] : "USD";
      fill(codes, select(code, entries.map(([name]) => [name, name]), (value) => { code = value; convert(); }));
      convert();
    };
    const refresh = async (button) => {
      if (button) button.classList.add("busy");
      if (!store.get("rates", null)) fill(board, loading());
      try {
        const data = await bridge.call("rates");
        store.set("rates", data);
        if (board.isConnected) draw(data);
      } catch (error) {
        if (!error.cancelled) toast(error.message, "bad");
      } finally {
        if (button) button.classList.remove("busy");
      }
    };
    const amountInput = field({ type: "number", inputmode: "decimal", value: amount, oninput: (event) => { amount = event.target.value; convert(); } });
    const refreshButton = iconButton("refresh", t("refresh"), () => refresh(refreshButton));
    add(main, headRow(t("rates_title"), refreshButton), board,
      section(t("converter"), card(h("div", { class: "row convert-row" }, amountInput, codes,
        iconButton("swap", t("swap"), () => { toCup = !toCup; convert(); })), result)));
    if (saved) draw(saved);
    if (!saved || now() - saved.at > 1800) refresh();
  },
};

VIEWS.weather = {
  tab: "apps",
  title: () => t("tile_weather"),
  render(main) {
    const box = h("div", { class: "stack" });
    const cities = store.get("cities", [t("default_city")]);
    const run = (city) => {
      remember("cities", city, 6);
      toolResult(box, "weather", city);
    };
    add(main, askBox({ placeholder: t("weather_placeholder"), value: cities[0], onsubmit: run, iconName: "weather" }),
      h("div", { class: "chips" }, cities.map((city) => h("button", { class: "chip", onclick: () => run(city) }, city))), box);
    add(box, empty(t("weather_hint"), "weather"));
  },
};

const TOPICS = ["topic_cuba", "topic_world", "topic_tech", "topic_sports", "topic_economy", "topic_science"];

VIEWS.news = {
  tab: "apps",
  title: () => t("tile_news"),
  render(main, params) {
    const box = h("div", { class: "stack" });
    const run = (q) => swap("news", { q });
    add(main, askBox({ placeholder: t("news_placeholder"), value: params.q || "", onsubmit: run, iconName: "news" }),
      h("div", { class: "chips" }, TOPICS.map((key) => h("button", { class: `chip ${params.q === t(key) ? "on" : ""}`, onclick: () => run(t(key)) }, t(key)))), box);
    const q = params.q || t("topic_cuba");
    fetchView(box, "search", { q, engine: "news" }, (data) => fill(box, ...resultList(data)), { label: t("searching") });
  },
};

const LANGS = ["es", "en", "pt", "fr", "it", "de", "ru", "zh-CN", "ja", "ko", "ar", "hi", "tr", "nl", "pl", "uk", "sv", "el", "he", "ca", "ro", "vi", "id"];

function langName(code) {
  try {
    const names = new Intl.DisplayNames([lang()], { type: "language" });
    const name = names.of(code);
    return name.charAt(0).toLocaleUpperCase(lang()) + name.slice(1);
  } catch (error) {
    return code.toUpperCase();
  }
}

VIEWS.translate = {
  tab: "apps",
  title: () => t("tile_translate"),
  render(main) {
    let source = store.get("tr.source", "auto");
    let target = store.get("tr.target", lang() === "es" ? "en" : "es");
    const area = autosize(h("textarea", { class: "input", rows: 4, placeholder: t("translate_placeholder"), value: store.get("tr.text", "") }));
    const output = h("div", { class: "card translation" }, h("p", { class: "muted" }, t("translate_hint")));
    const pickers = h("div", { class: "row lang-row" });
    const drawPickers = () => fill(pickers, 
      select(source, [["auto", t("detect")], ...LANGS.map((code) => [code, langName(code)])], (value) => { source = value; store.set("tr.source", value); }),
      iconButton("swap", t("swap"), () => {
        if (source === "auto") return;
        [source, target] = [target, source];
        store.set("tr.source", source);
        store.set("tr.target", target);
        drawPickers();
      }),
      select(target, LANGS.map((code) => [code, langName(code)]), (value) => { target = value; store.set("tr.target", value); }));
    const run = async (button) => {
      const text = area.value.trim();
      if (!text) return;
      store.set("tr.text", text);
      button.classList.add("busy");
      fill(output, loading(t("translating")));
      try {
        const data = await bridge.call("translate", { text, source, target });
        fill(output, h("p", { class: "translated" }, data.text),
          h("div", { class: "row between" }, h("small", { class: "muted" }, data.detected ? t("detected", { name: langName(data.detected) }) : ""),
            iconButton("copy", t("copy"), () => copyText(data.text), "tiny")));
      } catch (error) {
        if (!error.cancelled) fill(output, failure(error.message));
      } finally {
        button.classList.remove("busy");
      }
    };
    const button = h("button", { class: "btn primary", onclick: () => run(button) }, icon("translate"), t("translate"));
    drawPickers();
    add(main, card(pickers, area, h("div", { class: "row between" }, h("small", { class: "muted" }, t("translate_note")), button)), output);
  },
};

const UTILITIES = [["convert", "tools", "util_convert", "util_convert_hint"], ["time", "clock", "util_time", "util_time_hint"], ["calc", "tools", "util_calc", "util_calc_hint"]];

VIEWS.tools = {
  tab: "apps",
  title: () => t("tile_tools"),
  render(main) {
    add(main, ...UTILITIES.map(([name, iconName, title, hint]) => {
      const box = h("div");
      return section(t(title), card(askBox({ placeholder: t(hint), onsubmit: (value) => toolResult(box, name, value), iconName }), box));
    }));
  },
};

// ---------------------------------------------------------------------------
// Part joiner: glue raw ".001" parts on the device and verify them with the bot's SHA-256 hashes.
// ---------------------------------------------------------------------------
const MIME = { mp4: "video/mp4", m4v: "video/mp4", mov: "video/quicktime", webm: "video/webm", mkv: "video/x-matroska", mp3: "audio/mpeg", m4a: "audio/mp4",
  opus: "audio/ogg", ogg: "audio/ogg", wav: "audio/wav", jpg: "image/jpeg", jpeg: "image/jpeg", png: "image/png", gif: "image/gif", webp: "image/webp",
  pdf: "application/pdf", zip: "application/zip", epub: "application/epub+zip", txt: "text/plain" };

function partInfo(name) {
  let match = name.match(/^(.*)\.(\d{3})$/);
  if (match) return { base: match[1], index: Number(match[2]), kind: "raw" };
  match = name.match(/^(.*) \((\d+)-(\d+)\)(\.[^.]+)$/);
  if (match) return { base: match[1] + match[4], index: Number(match[2]), kind: "segment" };
  return { base: name, index: 0, kind: "other" };
}

function mimeOf(name) {
  return MIME[(name.split(".").pop() || "").toLowerCase()] || "application/octet-stream";
}

async function sha256(blob) {
  if (!(window.crypto && crypto.subtle)) return "";
  const digest = await crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function stem(name) {
  return name.replace(/\.[^.]+$/, "").toLowerCase();
}

// Match by name first; files picked from the gallery are renamed by iOS, so the exact total size also identifies the download.
function jobFor(base, size) {
  const jobs = cache.jobs.slice().reverse().filter((job) => job.sha);
  return jobs.find((job) => job.title === base || stem(job.title) === stem(base)) || jobs.find((job) => size && job.size === size) || null;
}

// Every part the bot sent is known by its SHA-256, which survives any renaming.
function partBySha(sha) {
  for (const job of cache.jobs.slice().reverse()) {
    const index = (job.part_sha || []).indexOf(sha);
    if (index >= 0) return { job, index: index + 1, total: job.part_sha.length };
  }
  return null;
}

const blobSource = (file) => ({ size: file.size, read: async (offset, length) => new Uint8Array(await file.slice(offset, offset + length).arrayBuffer()) });
const extOf = (name) => (name.split(".").pop() || "").toLowerCase();
const MP4_LIKE = ["mp4", "m4v", "m4a", "mov"];

function joinMethod(parts) {
  const exts = new Set(parts.map((part) => extOf(part.file.name)));
  if (parts.some((part) => part.info.kind === "raw")) return "bytes";
  if ([...exts].every((ext) => MP4_LIKE.includes(ext))) return "mp4";
  if ([...exts].every((ext) => ext === "mp3")) return "mp3";
  if ([...exts].some((ext) => ["webm", "mkv", "ogg", "opus"].includes(ext))) return "none";
  return "bytes";
}

// Small poster frame and duration for media parts, so the order can be checked by eye.
function mediaPreview(file) {
  return new Promise((resolve) => {
    const type = mimeOf(file.name).split("/")[0];
    if (type !== "video" && type !== "audio") return resolve({});
    const url = URL.createObjectURL(file);
    const element = document.createElement(type);
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      element.onerror = null;
      element.removeAttribute("src");
      element.load();
      URL.revokeObjectURL(url);
      resolve(value);
    };
    element.muted = true;
    element.preload = "metadata";
    element.playsInline = true;
    element.onloadedmetadata = () => {
      const duration = element.duration;
      if (type === "audio" || !element.videoWidth) return finish({ duration });
      element.onseeked = () => {
        try {
          const canvas = document.createElement("canvas");
          canvas.width = 96;
          canvas.height = Math.round((96 * element.videoHeight) / element.videoWidth) || 54;
          canvas.getContext("2d").drawImage(element, 0, 0, canvas.width, canvas.height);
          finish({ duration, thumb: canvas.toDataURL("image/jpeg", 0.6) });
        } catch (error) {
          finish({ duration });
        }
      };
      element.currentTime = Math.min(0.5, duration / 2 || 0);
    };
    element.onerror = () => finish({});
    setTimeout(() => finish({}), 8000);
    element.src = url;
  });
}

function fmtClip(seconds) {
  if (!seconds || !isFinite(seconds)) return "";
  const total = Math.round(seconds);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

function orderOf(part) {
  if (part.known) return part.known.index;
  if (part.marker) return part.marker.index;
  return part.info.index || Infinity;
}

async function pickFiles(multiple = true) {
  if (window.webxdc.importFiles) return window.webxdc.importFiles({ multiple });
  return new Promise((resolve) => {
    const input = h("input", { type: "file", multiple, style: "display:none", onchange: () => { resolve([...input.files]); input.remove(); } });
    add(document.body, input);
    input.click();
  });
}

VIEWS.join = {
  tab: "apps",
  title: () => t("tile_join"),
  render(main) {
    let parts = [];
    let name = "";
    const list = h("div", { class: "stack" });
    const output = h("div", { class: "stack" });
    const nameInput = field({ type: "text", placeholder: t("join_name"), oninput: (event) => { name = event.target.value; } });
    const move = (from, to) => {
      if (to < 0 || to >= parts.length) return;
      [parts[from], parts[to]] = [parts[to], parts[from]];
      draw();
    };
    const draw = () => {
      fill(output);
      if (!parts.length) {
        fill(list, empty(t("join_empty"), "join"));
        return;
      }
      const method = joinMethod(parts);
      const total = parts.reduce((sum, part) => sum + part.file.size, 0);
      const job = (parts.find((part) => part.known) || {}).known;
      const expected = job ? job.total : (parts.find((part) => part.marker) || { marker: null }).marker?.total;
      const have = new Set(parts.map(orderOf));
      const missing = expected ? Array.from({ length: expected }, (_, index) => index + 1).filter((index) => !have.has(index)) : [];
      const unordered = parts.some((part) => orderOf(part) === Infinity);
      const label = (part) => (part.known ? t("join_by_hash") : part.marker ? t("join_by_tag") : part.info.index ? t("join_by_name") : t("join_by_none"));
      fill(list,
        card(h("div", { class: "list" }, parts.map((part, index) => h("div", { class: `list-row join-row ${part.bad ? "bad" : part.good || part.known ? "good" : ""}` },
          part.thumb ? h("img", { class: "join-thumb", src: part.thumb, alt: "" }) : h("span", { class: "num" }, String(index + 1)),
          h("span", { class: "grow" },
            h("b", { class: "clamp1" }, part.thumb ? `${index + 1}. ${part.file.name}` : part.file.name),
            h("small", { class: "muted" }, [fmtSize(part.file.size), fmtClip(part.duration), label(part)].filter(Boolean).join(" · "))),
          part.bad ? icon("alert") : part.known ? icon("check") : null,
          iconButton("up", t("move_up"), () => move(index, index - 1), "tiny"),
          iconButton("down", t("move_down"), () => move(index, index + 1), "tiny"),
          iconButton("close", t("remove"), () => { parts.splice(index, 1); draw(); }, "tiny")))),
          h("p", { class: "muted small" }, t("join_total", { n: parts.length, size: fmtSize(total) }))),
        missing.length ? h("p", { class: "note bad" }, icon("alert"), t("join_missing", { list: missing.join(", ") })) : null,
        unordered ? h("p", { class: "note warn" }, icon("info"), t("join_unordered")) : null,
        method === "none" ? h("p", { class: "note warn" }, icon("info"), t("join_segments")) : null,
        h("label", { class: "field-label" }, t("join_name")), nameInput,
        h("button", { class: "btn primary wide", disabled: method === "none" || parts.length < 2 && method !== "bytes", onclick: (event) => join(event.currentTarget, method) },
          icon("join"), t("join_go")));
      nameInput.value = name;
    };
    const pick = async () => {
      let files;
      try {
        files = await pickFiles();
      } catch (error) {
        toast(t("pick_failed"), "bad");
        return;
      }
      const known = new Set(parts.map((part) => part.file.name + part.file.size));
      const fresh = files.filter((file) => !known.has(file.name + file.size));
      if (!fresh.length) return;
      fill(output, loading(t("join_reading")));
      for (const file of fresh) {
        const part = { file, info: partInfo(file.name) };
        const ext = extOf(file.name);
        if (MP4_LIKE.includes(ext) || ext === "mp3") {
          try {
            part.marker = await Mp4Join.marker(blobSource(file));
          } catch (error) {
            part.marker = null;
          }
        }
        if (file.size <= 200 * 1024 * 1024) {
          const sha = await sha256(file);
          part.known = sha ? partBySha(sha) : null;
        }
        Object.assign(part, await mediaPreview(file));
        parts.push(part);
      }
      parts.sort((a, b) => orderOf(a) - orderOf(b) || a.file.name.localeCompare(b.file.name));
      if (!name && parts.length) {
        const job = (parts.find((part) => part.known) || {}).known;
        const tagged = parts.find((part) => part.marker && part.marker.name);
        name = job ? job.job.title : tagged ? tagged.marker.name : parts[0].info.base;
      }
      draw();
    };
    const join = async (button, method) => {
      button.classList.add("busy");
      const finalName = (name || parts[0].info.base).trim();
      let blob;
      let verdict = null;
      try {
        if (method === "mp4" || method === "mp3") {
          const result = method === "mp4" ? await Mp4Join.join(parts.map((part) => ({ ...blobSource(part.file), file: part.file })))
            : await Mp4Join.joinMp3(parts.map((part) => ({ ...blobSource(part.file), file: part.file })));
          blob = new Blob(result.parts.map((piece) => (piece instanceof Uint8Array ? piece : piece.source.file.slice(piece.start, piece.end))), { type: mimeOf(finalName) });
          verdict = h("p", { class: "note ok" }, icon("check"), t("join_merged", { time: fmtClip(result.duration) || "-" }));
        } else {
          blob = new Blob(parts.map((part) => part.file), { type: mimeOf(finalName) });
        }
      } catch (error) {
        button.classList.remove("busy");
        fill(output, h("p", { class: "note bad" }, icon("alert"), t("join_err_" + (error.code || "broken"))));
        return;
      }
      const job = method === "bytes" ? jobFor(finalName, blob.size) : null;
      if (!verdict) verdict = h("p", { class: "muted small" }, t("verify_none"));
      if (job && blob.size <= 512 * 1024 * 1024 && window.crypto && crypto.subtle) {
        fill(output, loading(t("verifying")));
        const whole = await sha256(blob);
        if (whole === job.sha) {
          parts.forEach((part) => { part.good = true; });
          verdict = h("p", { class: "note ok" }, icon("check"), t("verify_ok"));
        } else {
          const bad = [];
          for (const [index, part] of parts.entries()) {
            const expected = job.part_sha[index];
            if (expected && (await sha256(part.file)) !== expected) {
              part.bad = true;
              bad.push(index + 1);
            }
          }
          verdict = h("div", { class: "note bad" }, icon("alert"), h("span", {}, bad.length ? t("verify_parts", { list: bad.join(", ") }) : t("verify_bad")),
            ...bad.map((number) => h("button", { class: "chip action", onclick: () => resend(job.id, number) }, t("act_resend"), " ", String(number))));
        }
        draw();
      }
      button.classList.remove("busy");
      const url = URL.createObjectURL(blob);
      const type = blob.type.split("/")[0];
      const preview = type === "video" ? h("video", { src: url, controls: true, playsinline: true, class: "viewer" })
        : type === "audio" ? h("audio", { src: url, controls: true }) : type === "image" ? h("img", { src: url, class: "viewer" }) : null;
      fill(output, card(h("div", { class: "row" }, icon("file", "big"), h("span", { class: "grow" }, h("b", {}, finalName), h("small", { class: "muted" }, fmtSize(blob.size)))),
        verdict, preview, h("button", { class: "btn primary wide", onclick: () => exportSheet(blob, finalName) }, icon("share"), t("export"))));
      output.scrollIntoView({ behavior: "smooth", block: "start" });
    };
    add(main, card(h("div", { class: "row" }, icon("join", "big accent"), h("p", { class: "grow" }, t("join_intro"))),
      h("button", { class: "btn primary wide", onclick: pick }, icon("plus"), t("join_pick")),
      h("p", { class: "note" }, icon("info"), t("join_files_hint"))), list, output);
    draw();
  },
};

async function resend(id, part) {
  try {
    const reply = await bridge.call("queue", { do: "resend", id, part });
    setJobs(reply.jobs);
    toast(reply.message || t("done"), "ok");
  } catch (error) {
    if (!error.cancelled) toast(error.message, "bad");
  }
}

// ---------------------------------------------------------------------------
// Downloads.
// ---------------------------------------------------------------------------
const FINAL = ["done", "failed", "cancelled"];

async function queueOp(action, id, button) {
  if (button) button.classList.add("busy");
  try {
    const reply = await bridge.call("queue", { do: action, id });
    setJobs(reply.jobs);
    if (reply.message) toast(reply.message, "ok");
  } catch (error) {
    if (!error.cancelled) toast(error.message, "bad");
  } finally {
    if (button) button.classList.remove("busy");
  }
}

function resendSheet(job) {
  openSheet(t("act_resend"), [h("p", { class: "muted small" }, t("resend_hint")),
    h("div", { class: "chips" }, Array.from({ length: job.parts }, (unused, index) =>
      h("button", { class: "chip big", onclick: () => { closeSheet(); resend(job.id, index + 1); } }, String(index + 1))))]);
}

function jobCard(job) {
  const tool = (name, label, action) => {
    const button = h("button", { class: "chip action", onclick: () => queueOp(action, job.id, button) }, icon(name), label);
    return button;
  };
  const progress = job.status === "downloading" && job.size ? [bar(job.done, job.size), h("small", { class: "muted" }, `${fmtSize(job.done)} / ${fmtSize(job.size)}`)]
    : job.status === "sending" && job.parts ? [bar(job.delivered, job.parts), h("small", { class: "muted" }, t("parts_sent", { n: job.delivered, m: job.parts }))]
      : job.status === "queued" || job.status === "packing" ? [bar(0, 0, "indeterminate")] : [];
  const meta = [job.size ? fmtSize(job.size) : "", job.parts > 1 ? t("parts_n", { n: job.parts }) : "", job.parts > 1 ? formatLabel(job.format) : "", fmtTime(job.created)];
  return h("article", { class: `card job ${job.status}` },
    h("div", { class: "row" }, h("span", { class: "job-icon" }, icon(job.kind === "video" ? "video" : job.kind === "audio" ? "audio" : "file")),
      h("div", { class: "grow" }, h("b", { class: "clamp" }, plain(job.title)), h("small", { class: "muted" }, meta.filter(Boolean).join(" · "))),
      h("span", { class: `status s-${job.hold && !FINAL.includes(job.status) ? "paused" : job.status}` },
        job.hold && !FINAL.includes(job.status) ? t("status_paused") : t("status_" + job.status))),
    ...progress,
    job.error ? h("p", { class: "note bad" }, icon("alert"), plain(job.error)) : null,
    h("div", { class: "chips" },
      !FINAL.includes(job.status) ? (job.hold ? tool("play", t("resume"), "resume") : tool("pause", t("pause"), "pause")) : null,
      !FINAL.includes(job.status) ? tool("close", t("cancel"), "cancel") : null,
      job.status === "failed" || job.status === "cancelled" ? tool("refresh", t("retry"), "retry") : null,
      job.status === "done" && job.parts > 0 ? h("button", { class: "chip action", onclick: () => resendSheet(job) }, icon("send"), t("act_resend")) : null,
      job.status === "done" && job.parts > 1 && job.format === "raw" ? h("button", { class: "chip action", onclick: () => go("join") }, icon("join"), t("tile_join")) : null));
}

function downloadPanel(main) {
    const input = field({ type: "url", placeholder: t("download_placeholder"), enterkeyhint: "go" });
    const list = h("div", { class: "stack" });
    const url = () => {
      const value = input.value.trim();
      if (!isUrl(value)) {
        toast(t("invalid_url"), "bad");
        return "";
      }
      return normalUrl(value);
    };
    const start = (kind) => () => {
      const target = url();
      if (!target) return;
      if (kind === "video" || kind === "audio") qualitySheet(kind, target);
      else runAction(kind, target);
    };
    const draw = () => {
      const jobs = cache.jobs.slice().reverse();
      fill(list, ...(jobs.length ? jobs.map(jobCard) : [empty(t("jobs_none"), "downloads")]));
    };
    const refresh = iconButton("refresh", t("refresh"), () => queueOp("", "", refresh));
    add(main, 
      card(h("div", { class: "search-field" }, icon("link"), input),
        h("div", { class: "chips" },
          h("button", { class: "chip action", onclick: start("download") }, icon("file"), t("dl_file")),
          h("button", { class: "chip action", onclick: start("video") }, icon("video"), t("act_video")),
          h("button", { class: "chip action", onclick: start("audio") }, icon("audio"), t("act_audio")),
          h("button", { class: "chip action", onclick: start("info") }, icon("info"), t("act_info")))),
      section(null, headRow(t("queue"), refresh, iconButton("trash", t("clear_done"), () => queueOp("clear", ""))), list),
      h("p", { class: "note" }, icon("info"), t("downloads_hint")));
    draw();
    return draw;
}

VIEWS.downloads = {
  title: () => t("tab_downloads"),
  render(main, params) {
    const body = h("div", { class: "stack" });
    const chosen = params.section === "links" ? "links" : "downloads";
    let drawJobs = null;
    bus.on("jobs", () => { if (drawJobs) drawJobs(); });
    const draw = (which) => {
      current.params = { section: which };
      drawJobs = null;
      fill(body);
      if (which === "links") VIEWS.links.render(body);
      else drawJobs = downloadPanel(body);
    };
    add(main, segmented(chosen, [["downloads", t("downloads_section"), "downloads"], ["links", t("links_section"), "links"]], draw), body);
    draw(chosen);
  },
};

// ---------------------------------------------------------------------------
// Settings, health and administration.
// ---------------------------------------------------------------------------
async function saveSetting(key, value, row) {
  row.classList.add("saving");
  try {
    cache.settings = await bridge.call("settings", { set: { [key]: value } });
    saveCache();
    toast(t("saved"), "ok");
  } catch (error) {
    if (!error.cancelled) toast(error.message, "bad");
    bus.emit("settings");
  } finally {
    row.classList.remove("saving");
  }
}

function settingRow(item) {
  const row = h("label", { class: "setting" }, h("span", { class: "grow" }, item.label));
  let control;
  if (item.kind === "choice") {
    control = select(item.value, item.choices.map((choice) => [choice.v, choice.l]), (value) => saveSetting(item.key, value, row));
  } else {
    control = field({ type: item.kind === "number" ? "number" : "text", value: item.value, min: item.min, max: item.max, inputmode: item.kind === "number" ? "numeric" : null,
      onchange: (event) => saveSetting(item.key, event.target.value.trim(), row) });
  }
  add(row, control, h("i", { class: "spinner" }));
  return row;
}

function botSettings() {
  const box = h("div", { class: "stack" });
  const draw = () => {
    if (!cache.settings || cache.lang !== lang()) {
      fill(box, loading());
      return;
    }
    const reset = async () => {
      if (!(await confirmBox(t("settings_reset"), t("settings_reset_q"), t("settings_reset_do")))) return;
      try {
        cache.settings = await bridge.call("settings", { reset: true });
        saveCache();
        toast(t("done"), "ok");
      } catch (error) {
        if (!error.cancelled) toast(error.message, "bad");
      }
      bus.emit("settings");
    };
    fill(box, h("p", { class: "note" }, icon(cache.admin ? "info" : "user"), h("span", { class: "grow" }, t(cache.admin ? "settings_admin" : "settings_own"))),
      ...cache.settings.map((group) => section(group.title, h("div", { class: "stack" },
        group.shared ? h("p", { class: "note" }, icon("shield"), h("span", { class: "grow" }, t("settings_shared"))) : null,
        h("div", { class: "card settings" }, group.items.map(settingRow))))),
      cache.admin ? null : h("button", { class: "list-row", onclick: reset }, icon("refresh"), h("span", { class: "grow" }, t("settings_reset")), icon("next")));
  };
  bus.on("settings", draw);
  draw();
  if (cache.lang !== lang()) hello(true);
  return box;
}

function clearKeys(prefix) {
  Object.keys(localStorage).filter((key) => key.startsWith("mf." + prefix)).forEach((key) => localStorage.removeItem(key));
}

VIEWS.settings = {
  title: () => t("tab_settings"),
  render(main) {
    const appRow = (label, control) => h("div", { class: "setting" }, h("span", { class: "grow" }, label), control);
    const wipe = (title, work) => h("button", { class: "list-row", onclick: async () => {
      if (await confirmBox(title, t("wipe_q"), t("delete"))) {
        await work();
        toast(t("done"), "ok");
      }
    } }, icon("trash"), h("span", { class: "grow" }, title), icon("next"));
    const modes = h("div", { class: "mode-grid" });
    add(main, 
      section(t("set_app"), h("div", { class: "card settings" },
        appRow(t("set_language"), select(prefs.lang, [["auto", t("lang_auto")], ["es", "Español"], ["en", "English"]], (value) => {
          prefs.lang = value;
          savePrefs();
          applyLook();
          chrome();
          render();
        })),
        appRow(t("set_theme"), segmented(prefs.theme, [["system", t("theme_system")], ["light", t("theme_light")], ["dark", t("theme_dark")]], (value) => {
          prefs.theme = value;
          savePrefs();
          applyLook();
        })),
        appRow(t("set_engine"), select(prefs.engine, ENGINES.map((key) => [key, t("engine_" + key)]), (value) => {
          prefs.engine = value;
          savePrefs();
        })))),
      section(t("set_modes"), h("p", { class: "muted small" }, t("set_modes_hint")), modes),
      botSettings(),
      section(t("more"), h("div", { class: "card list" },
        cache.admin ? h("button", { class: "list-row", onclick: () => go("admin") }, icon("shield"), h("span", { class: "grow" }, t("tile_admin")), icon("next")) : null,
        h("button", { class: "list-row", onclick: () => go("help") }, icon("help"), h("span", { class: "grow" }, t("help_title")), icon("next")),
        h("button", { class: "list-row", onclick: () => hello(true) }, icon("refresh"), h("span", { class: "grow" }, t("sync")), icon("next")))),
      section(t("sync_title"), h("div", { class: "card" }, syncTools())),
      section(t("set_data"), h("div", { class: "card list" },
        wipe(t("wipe_chats"), () => clearKeys("chat.")),
        wipe(t("wipe_images"), () => media.clear()),
        wipe(t("wipe_history"), () => { store.set("visited", []); store.set("recent", []); store.set("notices", []); memo.clear(); }))),
      h("p", { class: "about" }, icon("cat"), " Michiflin AI", cache.version ? ` · ${cache.version}` : ""));
    modeControl(modes, "nauta_strict", t("tile_nauta"), "nauta");
    modeControl(modes, "kill_switch", t("tile_killswitch"), "killswitch");
  },
};

function healthCards(data) {
  const tile = (name, label, value, extra = "") => h("div", { class: `card stat ${extra}` }, icon(name), h("small", { class: "muted" }, label), h("b", {}, value));
  const capped = data.cap_mb > 0;
  return [
    h("div", { class: "rate-grid" },
      tile("clock", t("restart_in"), until(data.restart, t("restarting"))),
      tile("health", t("uptime"), fmtDuration(data.uptime)),
      tile("downloads", t("active"), String(data.active)),
      tile("file", t("queue_size"), fmtSize(data.queue_bytes)),
      tile("downloads", t("disk_free"), fmtSize(data.disk_free), data.disk_free < 2 * 1024 ** 3 ? "warn" : ""),
      tile("send", t("sent_you"), capped ? `${fmtSize(data.sent)} / ${data.cap_mb} MB` : fmtSize(data.sent))),
    capped ? bar(data.sent, data.cap_mb * 1024 * 1024) : null,
    section(t("services"), h("div", { class: "card list" },
      ...[["ffmpeg", "ffmpeg"], ["7z", "7-Zip"], ["browser", t("svc_browser")], ["media", t("svc_media")]].map(([key, label]) =>
        h("div", { class: "list-row" }, icon(data.tools[key] ? "check" : "alert", data.tools[key] ? "ok" : "bad"), h("span", { class: "grow" }, label),
          h("small", { class: "muted" }, data.tools[key] ? t("svc_ok") : t("svc_missing")))),
      h("div", { class: "list-row" }, icon("search"), h("span", { class: "grow" }, t("svc_search")),
        h("small", { class: "muted" }, data.search.you ? t("used_of", { n: data.search.used, m: data.search.daily }) : t("svc_free"))))),
    h("p", { class: "muted small" }, t("health_note")),
  ];
}

VIEWS.health = {
  tab: "apps",
  title: () => t("tile_health"),
  render(main) {
    const box = h("div", { class: "stack" });
    const summary = h("div", { class: "status-summary" });
    const draw = () => fill(box, ...(cache.health ? healthCards(cache.health) : [loading()]));
    const drawSummary = () => fetchView(summary, "estado", {}, (data) => fill(summary,
      h("div", { class: "card status-brief" }, h("span", { class: "mode-icon status-color" }, icon("status")),
        h("div", { class: "grow" }, h("b", {}, t("estado_title")), h("p", { role: "status" }, data[lang()] || data.es || "—")))), { fresh: true });
    const refresh = iconButton("refresh", t("refresh"), async () => {
      refresh.classList.add("busy");
      drawSummary();
      try {
        cache.health = await bridge.call("health");
        saveCache();
        draw();
      } catch (error) {
        if (!error.cancelled) toast(error.message, "bad");
      } finally {
        refresh.classList.remove("busy");
      }
    });
    bus.on("health", draw);
    add(main, headRow(cache.version ? `Michiflin AI ${cache.version}` : "Michiflin AI", refresh), summary, box);
    drawSummary();
    draw();
  },
};

async function adminOp(box, args, confirmText) {
  if (confirmText && !(await confirmBox(t("tile_admin"), confirmText, t("confirm")))) return;
  try {
    const data = await bridge.call("admin", args);
    memo.set("admin{}", data);
    if (box.isConnected) drawAdmin(box, data);
    if (data.message) toast(data.message, "ok");
  } catch (error) {
    // Administration actions can have taken effect even if the reply was lost.
    if (box.isConnected && !error.cancelled) fill(box, failure(error.message, () => adminOp(box, {})));
  }
}

const rate = (bytes) => `${fmtSize(bytes || 0)}/s`;
const mbit = (bytes) => `${Math.round(((bytes || 0) * 8) / 1e6)} Mbit/s`;

function statTile(name, label, value, sub = "", level = "") {
  return h("div", { class: `card stat ${level}` }, icon(name), h("small", { class: "muted" }, label), h("b", {}, value), sub ? h("small", { class: "muted sub" }, sub) : null);
}

function perfSection(perf) {
  const memoryUsed = perf.memory.total - perf.memory.available;
  const diskUsedShare = perf.disk.total ? perf.disk.used / perf.disk.total : 0;
  const cores = perf.cpu.cores || 1;
  return section(t("perf_title"),
    h("div", { class: "rate-grid" },
      statTile("bolt", t("perf_cpu"), `${perf.cpu.percent}%`, t("perf_load", { a: perf.cpu.load[0], b: perf.cpu.load[1], n: cores }),
        perf.cpu.percent > 85 ? "bad" : perf.cpu.percent > 60 ? "warn" : ""),
      statTile("health", t("perf_ram"), `${fmtSize(memoryUsed)} / ${fmtSize(perf.memory.total)}`, t("perf_bot_ram", { size: fmtSize(perf.memory.bot) }),
        perf.memory.available < perf.memory.total * 0.1 ? "bad" : perf.memory.available < perf.memory.total * 0.25 ? "warn" : ""),
      statTile("downloads", t("perf_disk"), t("perf_free", { size: fmtSize(perf.disk.free) }), t("perf_jobs_disk", { size: fmtSize(perf.disk.jobs), total: fmtSize(perf.disk.total) }),
        perf.disk.free < 2 * 1024 ** 3 ? "bad" : diskUsedShare > 0.8 ? "warn" : ""),
      statTile("web", t("perf_net"), `↓ ${rate(perf.network.rx_rate)}`, `↑ ${rate(perf.network.tx_rate)}`),
      statTile("clock", t("perf_latency"), perf.latency.quick.n ? `${perf.latency.quick.median} s` : "—",
        perf.latency.work.n ? t("perf_work", { m: perf.latency.work.median, p: perf.latency.work.p90 }) : t("perf_no_work"),
        perf.latency.quick.median > 20 ? "warn" : ""),
      statTile("rates", t("perf_queue"), t("perf_queue_v", { q: perf.queue.queued, d: perf.queue.downloading, s: perf.queue.sending }),
        perf.queue.paused ? t("perf_paused") : t("perf_workers", { n: perf.queue.workers }), perf.queue.paused ? "warn" : "")),
    h("div", { class: "card kv" },
      h("span", {}, t("perf_threads")), h("span", {}, String(perf.process.threads)),
      h("span", {}, t("uptime")), h("span", {}, fmtDuration(perf.process.uptime)),
      h("span", {}, t("perf_requests")), h("span", {}, t("perf_requests_v", { n: perf.latency.total, e: perf.latency.errors })),
      h("span", {}, t("sent_day")), h("span", {}, fmtSize(perf.queue.sent)),
      h("span", {}, t("queue_size")), h("span", {}, fmtSize(perf.queue.bytes)),
      h("span", {}, t("perf_traffic")), h("span", {}, `↓ ${fmtSize(perf.network.rx)} · ↑ ${fmtSize(perf.network.tx)}`)));
}

function hostSection(perf) {
  const host = perf.host;
  const rows = [
    [t("host_provider"), host.provider], [t("host_system"), host.os], [t("host_cpu"), `${perf.cpu.model} · ${perf.cpu.cores} ${t("host_cores")}`],
    [t("host_kernel"), `${host.kernel} · ${host.arch} · Python ${host.python}`], [t("host_runner"), host.runner],
    [t("host_run"), host.run ? `#${host.number || "?"} · ${host.run}${host.attempt && host.attempt !== "1" ? ` · ${t("host_attempt", { n: host.attempt })}` : ""}` : ""],
    [t("host_started"), fmtTime(host.started)], [t("restart_in"), until(host.ends, t("restarting"))],
    [t("host_version"), host.sha ? (host.outdated ? t("host_outdated", { sha: host.sha, latest: host.latest }) : t("host_current", { sha: host.sha })) : ""],
  ].filter(([, value]) => value);
  return section(t("host_title"), h("div", { class: "card kv" }, ...rows.flatMap(([label, value]) => [h("span", {}, label), h("span", {}, value)])),
    host.outdated ? h("p", { class: "note warn" }, icon("refresh"), t("host_update_note")) : null);
}

function benchSection(box, perf) {
  const result = perf.bench || {};
  const button = h("button", { class: "btn primary wide", onclick: () => {
    button.classList.add("busy");
    button.disabled = true;
    adminOp(box, { do: "bench" });
  } }, icon("bolt"), " ", t("bench_run"));
  const rows = result.at ? [
    [t("bench_cpu1"), rate(result.cpu.single)], [t("bench_cpun", { n: result.cpu.cores }), `${rate(result.cpu.multi)} · ×${result.cpu.scale}`],
    [t("bench_memory"), rate(result.memory)],
    [t("bench_disk"), result.disk.skipped ? t("bench_skipped") : `${t("bench_write")} ${rate(result.disk.write)} · ${t("bench_read")} ${rate(result.disk.read)}`],
    [t("bench_net"), result.network.down ? `${mbit(result.network.down)} · ${result.network.latency} ms` : t("bench_failed")],
    [t("bench_core"), result.core.rpc ? `${result.core.rpc} ms · ${t("bench_save", { ms: result.core.save })}` : "—"],
    [t("bench_when"), `${fmtTime(result.at)} · ${result.seconds} s`],
  ] : [];
  return section(t("bench_title"), h("p", { class: "muted small" }, t("bench_hint")), button,
    rows.length ? h("div", { class: "card kv" }, ...rows.flatMap(([label, value]) => [h("span", {}, label), h("span", {}, value)])) : null);
}

function usageSection(box, usage, privateMode = false, preserveHistory = false) {
  const rows = usage.people.map((person) => {
    const limited = usage.limited.includes(person.address);
    return h("div", { class: "share" },
      h("div", { class: "row" }, icon("user"), h("b", { class: "grow clamp1" }, person.address),
        person.heavy ? h("span", { class: "badge bad" }, t("usage_heavy")) : null,
        person.over ? h("span", { class: "badge" }, t("usage_over")) : null,
        limited ? h("span", { class: "badge" }, t("usage_limited")) : null),
      bar(person.share * 1000, 1000),
      h("small", { class: "muted" }, t("usage_line", { share: Math.round(person.share * 100), size: fmtSize(person.bytes), n: person.requests, ai: person.ai, img: person.images || 0, code: person.code || 0, jobs: person.jobs, today: fmtSize(person.today) })),
      person.admin || privateMode ? null : h("div", { class: "chips" },
        h("button", { class: "chip action", onclick: () => adminOp(box, { do: limited ? "unlimit" : "limit", address: person.address }, limited ? "" : t("usage_limit_q", { address: person.address })) },
          icon(limited ? "play" : "pause"), limited ? t("usage_unlimit") : t("usage_limit"))));
  });
  return section(privateMode ? t("usage_private_title", { n: usage.days }) : t("usage_title", { n: usage.days }),
    h("p", { class: "muted small" }, t(privateMode ? "usage_private_hint" : "usage_hint")),
    h("div", { class: "card" }, rows.length ? rows : h("p", { class: "empty small" }, t("usage_none"))),
    h("div", { class: "card list" },
      privateMode ? null : h("div", { class: "list-row" }, icon("downloads"), h("span", { class: "grow" }, t("usage_rules", { n: usage.max_jobs, mb: usage.budget_mb ? `${usage.budget_mb} MB` : t("usage_no_quota") })),
        h("button", { class: "btn small ghost", onclick: () => goTab("settings") }, t("usage_change"))),
      usage.ai && !privateMode ? h("div", { class: "list-row" }, icon("ai"), h("span", { class: "grow" }, t("usage_ai_rules", {
        ask: usage.ai.ask || "∞", image: usage.ai.image || "∞", code: usage.ai.code || "∞", reserve: usage.ai.reserve, left: usage.ai.left }))) : null,
      h("label", { class: "setting" }, h("span", { class: "grow" }, t("usage_pause_all")),
        h("input", { type: "checkbox", class: "switch", checked: usage.paused, onchange: (event) => adminOp(box, { do: "pause_queue", value: event.target.checked }) })),
      h("button", { class: "list-row", onclick: () => adminOp(box, { do: "reset_usage" }, t(privateMode || preserveHistory ? "usage_private_reset_q" : "usage_reset_q")) }, icon("trash"), h("span", { class: "grow" }, t(privateMode || preserveHistory ? "usage_private_reset" : "usage_reset")))));
}

function logSection(box, data) {
  const entries = Array.isArray(data.log_entries) ? data.log_entries : (data.logs || []).map((message) => ({
    message, level: "WARNING", last: 0, count: 1, detail: ""
  })).reverse();
  const rows = h("div", { class: "log-list" });
  const draw = (filter) => {
    const shown = entries.filter((entry) => filter === "all" || (filter === "errors" ? entry.level === "ERROR" || entry.level === "CRITICAL" : entry.level === "WARNING"));
    fill(rows, ...(shown.length ? shown.map((entry) => h("details", { class: "log-entry" },
      h("summary", {}, h("span", { class: `log-level ${entry.level === "WARNING" ? "warn" : "bad"}` }, entry.level),
        h("span", { class: "grow clamp1" }, entry.message),
        entry.count > 1 ? h("span", { class: "log-count" }, `×${entry.count}`) : null,
        icon("down", "caret")),
      h("small", { class: "muted" }, [entry.last ? fmtTime(entry.last) : "", entry.logger || ""].filter(Boolean).join(" · ")),
      h("pre", { class: "logs" }, [entry.message, entry.detail].filter(Boolean).join("\n\n")))) : [h("p", { class: "empty small" }, t("admin_no_logs"))]));
  };
  draw("all");
  return section(t("admin_logs"), h("p", { class: "muted small" }, t("admin_logs_hint")),
    h("div", { class: "row wrap log-tools" },
      segmented("all", [["all", t("admin_logs_all")], ["errors", t("admin_logs_errors")], ["warnings", t("admin_logs_warnings")]], draw),
      h("button", { class: "btn small ghost", onclick: () => adminOp(box, { do: "clear_logs" }, t("admin_logs_clear_q")) }, icon("trash"), t("admin_logs_clear"))),
    rows);
}

function ownerAccessSection(box, data) {
  if (typeof data.owner_only !== "boolean") return null;  // compatible with an older backend
  let minutes = "60";
  const durations = select(minutes, [["15", t("owner_15m")], ["60", t("owner_1h")], ["240", t("owner_4h")],
    ["1440", t("owner_24h")], ["0", t("owner_manual")]], (value) => { minutes = value; });
  const change = async (enabled) => {
    if (!(await confirmBox(t("owner_control"), t(enabled ? "owner_close_q" : "owner_open_q"), t("confirm")))) return;
    button.disabled = true;
    try {
      const updated = await bridge.call("owner_only", { enabled, minutes: Number(minutes), nonce: data.control_nonce || "" });
      memo.set("admin{}", updated);
      if (box.isConnected) drawAdmin(box, updated);
      toast(t(enabled ? "owner_on" : "owner_off"), "ok");
    } catch (error) {
      // A lost reply may still mean the mode changed: hide stale controls until refreshed.
      if (box.isConnected && !error.cancelled) fill(box, failure(error.message, () => adminOp(box, {})));
    }
  };
  const button = h("button", { class: `btn ${data.owner_only ? "primary" : "ghost"} wide`, disabled: data.maintenance && data.owner_only,
    onclick: () => change(!data.owner_only) }, icon(data.owner_only ? "user" : "shield"), t(data.owner_only ? "owner_open" : "owner_close"));
  const card = h("div", { class: "card stack owner-access" },
    h("b", {}, t(data.maintenance ? "owner_maintenance_title" : (data.owner_only ? "owner_on" : "owner_off"))),
    h("p", { class: "muted small" }, t(data.maintenance ? "owner_maintenance" : "owner_hint")),
    !data.owner_only ? h("p", { class: "note" }, icon("clock"), data.owner_only_expires
      ? t("owner_until", { when: new Date(data.owner_only_expires * 1000).toLocaleString() }) : t("owner_until_restart")) : null,
    data.owner_only && !data.maintenance ? h("label", { class: "stack small" }, t("owner_duration"), durations) : null,
    button);
  clearTimeout(box._ownerAccessTimer);
  if (!data.owner_only && data.owner_only_expires) {
    // Use the server's remaining interval, not an assumed-synchronized device clock.
    if (!data._accessReceivedAt) data._accessReceivedAt = Date.now();
    const remaining = data.server_now
      ? (data.owner_only_expires - data.server_now) * 1000 - (Date.now() - data._accessReceivedAt)
      : data.owner_only_expires * 1000 - Date.now();
    const delay = Math.max(1000, Math.min(2147483647, remaining + 100));
    box._ownerAccessTimer = setTimeout(async () => {
      if (!box.isConnected) return;
      fill(box, h("p", { class: "muted" }, t("owner_checking")));
      try {
        const updated = await bridge.call("owner_only", {});
        memo.set("admin{}", updated);
        if (box.isConnected) drawAdmin(box, updated);
      } catch (error) {
        if (box.isConnected) fill(box, failure(error.message, () => adminOp(box, {})));
      }
    }, delay);
  }
  return section(t("owner_control"), card);
}

function drawAdmin(box, data) {
  const address = field({ type: "email", placeholder: t("admin_address") });
  const person = (entry, ...tools) => h("div", { class: "list-row" }, icon("user"),
    h("span", { class: "grow" }, h("b", { class: "clamp1" }, entry.address), entry.sub ? h("small", { class: "muted" }, entry.sub) : null), ...tools);
  fill(box,
    headRow(t("admin_live"), iconButton("refresh", t("refresh"), () => adminOp(box, {}))),
    ownerAccessSection(box, data),
    data.perf ? perfSection(data.perf) : null,
    data.usage ? usageSection(box, data.usage, data.private_mode, data.preserve_history) : null,
    data.perf ? hostSection(data.perf) : null,
    data.perf ? benchSection(box, data.perf) : null,
    section(t(data.private_mode ? "admin_owner" : "admin_users"),
      data.private_mode ? h("p", { class: "note" }, icon("shield"), t("admin_private_note")) : null,
      h("div", { class: "card list" },
        data.users.filter((user) => !data.private_mode || user.active).map((user) => person({ address: user.address, sub: user.admin ? "" : user.last ? t("admin_last", { when: fmtTime(user.last) }) : "" },
          data.private_mode && !user.active ? h("span", { class: "badge" }, t("admin_inactive"))
            : user.admin ? h("span", { class: "badge" }, t("admin_role"))
              : data.private_mode ? null : iconButton("close", t("admin_remove"), () => adminOp(box, { do: "remove", address: user.address }, t("admin_remove_q", { address: user.address })), "tiny"))),
        data.private_mode ? null : h("div", { class: "row add-row" }, address, h("button", { class: "btn primary small", onclick: () => address.value.trim() && adminOp(box, { do: "allow", address: address.value.trim() }, t("admin_grant_q", { address: address.value.trim() })) }, icon("plus"), t("admin_allow"))))),
    section(t("admin_attempts"), data.attempts.length ? h("div", { class: "card list" }, data.attempts.map((item) =>
      person({ address: item.address, sub: `${t("admin_tries", { n: item.count })} · ${fmtTime(item.last)}` },
        data.private_mode ? h("button", { class: "btn small ghost", disabled: true, title: t("admin_private_no_grant"), "aria-label": t("admin_private_no_grant") }, icon("shield"), t("admin_allow"))
          : iconButton("check", t("admin_allow"), () => adminOp(box, { do: "allow", address: item.address }, t("admin_grant_q", { address: item.address })), "tiny"),
        iconButton("ban", t("admin_block"), () => adminOp(box, { do: "block", address: item.address }), "tiny")))) : h("p", { class: "empty small" }, t("admin_no_attempts"))),
    !data.private_mode && data.blocked.length ? section(t("admin_blocked"), h("div", { class: "card list" }, data.blocked.map((item) =>
      person({ address: item }, data.private_mode ? null : h("button", { class: "btn small ghost", onclick: () => adminOp(box, { do: "allow", address: item }, t("admin_grant_q", { address: item })) }, t("admin_unblock")))))) : null,
    section(t("admin_controls"), h("div", { class: "card list" },
      h("label", { class: "setting" }, h("span", { class: "grow" }, t("admin_alerts")),
        h("input", { type: "checkbox", class: "switch", checked: data.alerts, onchange: (event) => adminOp(box, { do: "alerts", value: event.target.checked }) })),
      h("button", { class: "list-row", onclick: () => adminOp(box, { do: "purge" }, t(data.private_mode || data.preserve_history ? "admin_purge_private_q" : "admin_purge_q")) }, icon("trash"), h("span", { class: "grow" }, t("admin_purge"))),
      h("button", { class: "list-row danger-text", onclick: () => adminOp(box, { do: "restart" }, t("admin_restart_q")) }, icon("refresh"), h("span", { class: "grow" }, t("admin_restart"))))),
    logSection(box, data));
}

VIEWS.admin = {
  tab: "apps",
  title: () => t("tile_admin"),
  render(main) {
    const box = h("div", { class: "stack" });
    add(main, box);
    fetchView(box, "admin", {}, (data) => drawAdmin(box, data), { fresh: true });
  },
};

// ---------------------------------------------------------------------------
// B1: Notes / recuérdamelo
// ---------------------------------------------------------------------------
VIEWS.notes = {
  tab: "apps",
  title: () => t("tile_notes"),
  render(main) {
    const input = field({ type: "text", placeholder: t("ask_placeholder"), enterkeyhint: "go" });
    const list = h("div", { class: "stack" });
    const onAdd = async () => {
      const text = input.value.trim();
      if (!text) return;
      try {
        await bridge.call("notas", { do: "add", text });
        input.value = "";
        load();
      } catch (error) {
        if (!error.cancelled) toast(error.message, "bad");
      }
    };
    input.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); onAdd(); } });
    const load = async () => {
      try {
        const data = await bridge.call("notas", { do: "list" });
        fill(list, ...(data.notes && data.notes.length ? data.notes.map((n) =>
          h("div", { class: "list-row" }, h("span", { class: "grow clamp1" }, n.text),
            h("small", { class: "muted" }, fmtTime(n.ts)),
            iconButton("close", t("delete"), () => {
              bridge.call("notas", { do: "delete", id: n.id }).then(() => { toast(t("nota_deleted"), "ok"); load(); }).catch((e) => { if (!e.cancelled) toast(e.message, "bad"); });
            }, "tiny"))) : [empty(t("nota_empty"), "notes")]));
      } catch (error) {
        if (!error.cancelled) toast(error.message, "bad");
      }
    };
    const cardEl = card(h("div", { class: "search-field" }, icon("notes"), input,
      h("button", { class: "btn primary small", onclick: onAdd }, icon("plus"), t("done"))));
    add(main, cardEl, section(null, list));
    load();
  },
};

// ---------------------------------------------------------------------------
// B2: Cola de enlaces "más tarde"
// ---------------------------------------------------------------------------
VIEWS.links = {
  tab: "downloads",
  title: () => t("tile_links"),
  render(main) {
    const input = field({ type: "url", placeholder: t("download_placeholder"), enterkeyhint: "go" });
    const list = h("div", { class: "stack" });
    const onAdd = async () => {
      const url = normalUrl(input.value.trim());
      if (!isUrl(url)) { toast(t("invalid_url"), "bad"); return; }
      try {
        await bridge.call("enlaces", { do: "add", url });
        input.value = "";
        load();
      } catch (error) {
        if (!error.cancelled) toast(error.message, "bad");
      }
    };
    input.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); onAdd(); } });
    const load = async () => {
      try {
        const data = await bridge.call("enlaces", { do: "list" });
        fill(list, ...(data.links && data.links.length ? data.links.map((l) =>
          h("div", { class: "card saved-link" },
            h("div", { class: "row" }, icon("link"), h("span", { class: "grow" },
              h("b", { class: "clamp1" }, l.title && l.title !== l.url ? l.title : hostOf(l.url)),
              h("small", { class: "muted" }, `${hostOf(l.url)} · ${fmtTime(l.ts)}`)),
            iconButton("close", t("delete"), () => {
              bridge.call("enlaces", { do: "delete", id: l.id }).then(() => load()).catch((e) => { if (!e.cancelled) toast(e.message, "bad"); });
            }, "tiny")),
            h("small", { class: "muted saved-url" }, l.url),
            h("div", { class: "chips actions-bar" },
              h("button", { class: "chip action", onclick: () => go("reader", { url: l.url }) }, icon("web"), t("saved_open")),
              h("button", { class: "chip action", onclick: () => runAction("capture", l.url) }, icon("camera"), t("act_capture")),
              h("button", { class: "chip action", onclick: () => runAction("download", l.url) }, icon("downloads"), t("act_download")),
              h("button", { class: "chip action", onclick: () => copyText(l.url) }, icon("copy"), t("copy_link"))))) : [empty(t("link_empty"), "link")]));
      } catch (error) {
        if (!error.cancelled) toast(error.message, "bad");
      }
    };
    const processNext = async () => {
      try {
        const data = await bridge.call("enlaces", { do: "next" });
        if (data.empty) { toast(t("link_no_pending"), "bad"); return; }
        go("reader", { url: data.link.url });
      } catch (error) {
        if (!error.cancelled) toast(error.message, "bad");
      }
    };
    const cardEl = card(h("div", { class: "search-field" }, icon("link"), input,
      h("button", { class: "btn primary small", onclick: onAdd }, icon("plus"), t("done"))));
    add(main, cardEl,
      h("button", { class: "btn primary wide", onclick: processNext }, icon("play"), t("link_open_next")),
      h("p", { class: "note" }, icon("info"), t("saved_public_hint")), section(null, list));
    load();
  },
};

// ---------------------------------------------------------------------------
// Nauta and the kill switch: wait for authoritative state before enabling controls.
// ---------------------------------------------------------------------------
function modeControl(main, op, title, prefix) {
  const box = h("div", { class: "stack" });
  add(main, box);
  const draw = (data) => {
    if (!data || typeof data.active !== "boolean") throw new Error(t("bad_answer"));
    const active = data.active;
    const toggle = h("button", { class: `btn ${active ? "primary" : "ghost"}`, "aria-pressed": String(active), onclick: async () => {
      toggle.disabled = true;
      try {
        const updated = await bridge.call(op, { value: !active });
        if (box.isConnected) draw(updated);
      } catch (error) {
        // A timed-out change may have reached the bot: do not guess its state.
        if (box.isConnected && !error.cancelled) fill(box, failure(error.message, load));
      }
    } }, icon(active ? "pause" : "play"), t(active ? "mode_disable" : "mode_enable"));
    const mode = op === "nauta_strict" ? "nauta" : "killswitch";
    fill(box, h("div", { class: `card mode-card mode-${mode}` },
      h("div", { class: "mode-heading" }, h("span", { class: `mode-icon ${mode}-color` }, icon(mode)), h("h2", {}, title)),
      h("p", { role: "status" }, t(prefix + (active ? "_on" : "_off"))),
      op === "nauta_strict" ? h("p", { class: "muted" }, t("nauta_part", { mb: data.part_mb })) : null, toggle));
  };
  const load = async () => {
    fill(box, loading());
    try {
      const data = await bridge.call(op);
      if (box.isConnected) draw(data);
    } catch (error) {
      if (box.isConnected && !error.cancelled) fill(box, failure(error.message, load));
    }
  };
  load();
}

VIEWS.nauta_strict = {
  tab: "settings",
  title: () => t("tile_nauta"),
  render(main) { modeControl(main, "nauta_strict", t("tile_nauta"), "nauta"); },
};

VIEWS.kill_switch = {
  tab: "settings",
  title: () => t("tile_killswitch"),
  render(main) { modeControl(main, "kill_switch", t("tile_killswitch"), "killswitch"); },
};

VIEWS.estado = {
  tab: "apps",
  title: () => t("estado_title"),
  render(main) {
    const box = h("div", { class: "stack" });
    add(main, box);
    fetchView(box, "estado", {}, (data) => fill(box, card(h("h2", {}, t("estado_title")),
      h("p", { role: "status" }, data[lang()] || data.es || "—"))), { fresh: true });
  },
};

// ---------------------------------------------------------------------------
// First-run introduction: the app is the whole bot.
// ---------------------------------------------------------------------------
const INTRO = [["cat", "tour_1"], ["apps", "tour_2"], ["send", "tour_3"], ["downloads", "tour_4"], ["help", "tour_5"]];

function onboarding(force = false) {
  if (!force && store.get("tour_done", false)) return;
  document.querySelectorAll(".intro-overlay").forEach((node) => node.remove());
  let step = 0;
  const finish = () => {
    store.set("tour_done", true);
    overlay.classList.add("gone");
    setTimeout(() => overlay.remove(), 300);
  };
  const body = h("div", { class: "intro-card", role: "dialog", "aria-modal": "true" });
  const overlay = h("div", { class: "intro-overlay" }, body);
  const draw = () => {
    const [name, key] = INTRO[step];
    const last = step === INTRO.length - 1;
    fill(body,
      h("div", { class: "intro-icon" }, icon(name)),
      h("h2", {}, t(key + "_t")),
      h("p", {}, t(key + "_b")),
      h("div", { class: "intro-dots" }, ...INTRO.map((unused, index) => h("i", { class: index === step ? "on" : "" }))),
      h("div", { class: "row end" },
        last ? null : h("button", { class: "btn ghost", onclick: finish }, t("tour_skip")),
        step ? h("button", { class: "btn ghost", onclick: () => { step -= 1; draw(); } }, t("back")) : null,
        h("button", { class: "btn primary", onclick: () => { if (last) finish(); else { step += 1; draw(); } } }, last ? t("tour_start") : t("tour_next"))));
  };
  draw();
  document.body.append(overlay);
}

// ---------------------------------------------------------------------------
// Start.
// ---------------------------------------------------------------------------
// The splash stays long enough to be seen and to cover the first paint of the home screen.
const SPLASH_MIN = 2600;
const SPLASH_MAX = 6000;

function boot() {
  applyLook();
  chrome();
  const splash = $("#splash");
  const started = performance.now();
  let gone = false;
  const hide = () => {
    if (gone || !splash) return;
    gone = true;
    setTimeout(() => {
      splash.classList.add("gone");
      setTimeout(() => splash.remove(), 800);
      onboarding();
    }, Math.max(0, SPLASH_MIN - (performance.now() - started)));
  };
  setTimeout(hide, SPLASH_MAX);
  if (!window.webxdc) {
    fill($("#view"), failure(t("no_webxdc")));
    hide();
    return;
  }
  render();
  Promise.resolve(window.webxdc.setUpdateListener((update) => bridge.receive(update), bridge.startSerial())).then(() => {
    bus.emit("images");
    const first = hello();
    bridge.watchExpiry();
    // With a cached snapshot the home is already complete; the very first open waits for its sync (up to SPLASH_MAX).
    if (cache.at) hide();
    else Promise.resolve(first).finally(hide);
  });
}

boot();
