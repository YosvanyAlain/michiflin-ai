"use strict";
// Programador: free coding models write, run and fix a small project on the bot; the app keeps the files.

const CODER_SEND_MAX = 90000; // project files travel back with each follow-up when they fit in one update

const coder = {
  get() {
    const value = store.get("coder", {});
    return { files: [], turns: [], steps: [], history: [], lastChanges: [], summary: "", sig: "", isolated: false, rid: "",
      ...(value && typeof value === "object" ? value : {}),
      files: coderCloneFiles(value && value.files),
      turns: Array.isArray(value && value.turns) ? value.turns : [],
      steps: Array.isArray(value && value.steps) ? value.steps : [],
      history: Array.isArray(value && value.history) ? value.history : [],
      lastChanges: Array.isArray(value && value.lastChanges) ? value.lastChanges : [] };
  },
  set(project) {
    store.set("coder", project);
    bus.emit("coder");
  },
};

function coderCloneFiles(files) {
  const valid = Array.isArray(files) ? files.filter((file) => file && typeof file === "object" && typeof file.path === "string") : [];
  return JSON.parse(JSON.stringify(valid));
}

function coderFilesSignature(files) {
  return JSON.stringify(coderCloneFiles(files).sort((left, right) => left.path.localeCompare(right.path)));
}

function coderChanges(before, after) {
  const oldFiles = new Map(coderCloneFiles(before).map((file) => [file.path, file]));
  const newFiles = new Map(coderCloneFiles(after).map((file) => [file.path, file]));
  return [...new Set([...oldFiles.keys(), ...newFiles.keys()])].sort().flatMap((path) => {
    const oldFile = oldFiles.get(path) || null;
    const newFile = newFiles.get(path) || null;
    if (JSON.stringify(oldFile) === JSON.stringify(newFile)) return [];
    return [{ path, kind: !oldFile ? "added" : !newFile ? "removed" : "changed", before: oldFile, after: newFile }];
  });
}

function coderDiffText(change) {
  const oldText = change.before && typeof change.before.text === "string" ? change.before.text : "";
  const newText = change.after && typeof change.after.text === "string" ? change.after.text : "";
  if ((!change.before || change.before.text === undefined) && (!change.after || change.after.text === undefined)) return t("coder_binary_diff");
  const oldLines = oldText.split("\n");
  const newLines = newText.split("\n");
  let prefix = 0;
  while (prefix < oldLines.length && prefix < newLines.length && oldLines[prefix] === newLines[prefix]) prefix += 1;
  let suffix = 0;
  while (suffix < oldLines.length - prefix && suffix < newLines.length - prefix
         && oldLines[oldLines.length - 1 - suffix] === newLines[newLines.length - 1 - suffix]) suffix += 1;
  let removed = oldLines.slice(prefix, oldLines.length - suffix);
  let added = newLines.slice(prefix, newLines.length - suffix);
  const clipped = removed.length + added.length > 600;
  if (clipped) { removed = [...removed.slice(0, 300), "[… diff clipped …]"]; added = [...added.slice(0, 300), "[… diff clipped …]"]; }
  return [`--- ${change.before ? `before/${change.path}` : "/dev/null"}`,
    `+++ ${change.after ? `after/${change.path}` : "/dev/null"}`,
    `@@ -${prefix + 1},${removed.length} +${prefix + 1},${added.length} @@`,
    ...removed.map((line) => `-${line}`), ...added.map((line) => `+${line}`)].join("\n");
}

function coderDiffSheet(project) {
  const changes = Array.isArray(project.lastChanges) ? project.lastChanges : [];
  openSheet(t("coder_diff"), changes.length ? changes.map((change) => h("div", { class: "stack" },
    h("b", {}, `${t(`coder_${change.kind}`)} · ${change.path}`),
    h("pre", { class: "logs" }, coderDiffText(change)))) : [h("p", { class: "muted" }, t("coder_no_diff"))]);
}

function coderUndo() {
  const project = coder.get();
  const history = Array.isArray(project.history) ? project.history : [];
  if (!history.length) return;
  const previous = history.shift();
  project.lastChanges = coderChanges(project.files, previous.files);
  project.files = coderCloneFiles(previous.files);
  project.summary = previous.summary || "";
  project.name = previous.name || "";
  project.history = history;
  coder.set(project);
  toast(t("coder_undo_done"), "ok");
}

bridge.handlers.code = (data, error, ctx, rid) => {
  const project = coder.get();
  if (project.rid !== rid) return;
  project.rid = "";
  if (data) {
    const nextFiles = Array.isArray(data.files) ? data.files : [];
    if (coderFilesSignature(project.files) !== coderFilesSignature(nextFiles)) {
      project.lastChanges = coderChanges(project.files, nextFiles);
      project.history = [{ files: coderCloneFiles(project.files), summary: project.summary || "", name: project.name || "", ts: now() },
        ...(Array.isArray(project.history) ? project.history : [])].slice(0, 3);
      project.files = nextFiles;
    } else {
      project.lastChanges = [];
    }
    project.steps = data.steps || [];
    project.summary = data.summary || "";
    project.sig = data.sig || "";
    project.isolated = !!data.isolated;
    project.name = data.name || project.name || "";
    project.turns.push({ role: "assistant", text: project.summary, ts: now() });
    if (data.models) {
      cache.models = data.models;
      saveCache();
      bus.emit("models");
    }
    toast(t("coder_ready"), "ok");
  } else {
    project.turns.push({ role: "assistant", text: error, error: true, ts: now() });
    toast(error, "bad");
  }
  coder.set(project);
};

function coderModels() {
  const list = [];
  for (const provider of (cache.models && cache.models.providers) || []) {
    for (const model of provider.models || []) {
      if ((model.roles || []).includes("code") && !modelBroken(model, provider)) list.push([model.key, `${model.title} · ${provider.name}`]);
    }
  }
  return list;
}

function coderSend(text) {
  const project = coder.get();
  if (project.rid && bridge.pending()[project.rid]) {
    toast(t("coder_busy"), "bad");
    return false;
  }
  if (prefs.coderModel && !coderModels().some(([key]) => key === prefs.coderModel)) {
    toast(t("coder_model_unavailable"), "bad");
    return false;
  }
  const files = project.files;
  if (new Blob([JSON.stringify(files)]).size > CODER_SEND_MAX) {
    toast(t("coder_too_big"), "bad");
    return false; // never send an empty substitute that could overwrite the project
  }
  const { id } = bridge.request("code", { text, files, model: prefs.coderModel || "" });
  if (!id) return false;
  project.rid = id;
  project.turns.push({ role: "user", text, ts: now() });
  project.turns = project.turns.slice(-30);
  coder.set(project);
  return true;
}

// Minimal store-only ZIP writer (no compression; the files are small text).
const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n += 1) {
    let c = n;
    for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
})();

function crc32(bytes) {
  let crc = 0xffffffff;
  for (let index = 0; index < bytes.length; index += 1) crc = CRC_TABLE[(crc ^ bytes[index]) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

function fileBytes(file) {
  if (file.b64 !== undefined) {
    const binary = atob(file.b64);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
    return bytes;
  }
  return new TextEncoder().encode(file.text || "");
}

function makeZip(files, folder) {
  const encoder = new TextEncoder();
  const parts = [];
  const central = [];
  let offset = 0;
  const stamp = new Date();
  const time = (stamp.getHours() << 11) | (stamp.getMinutes() << 5) | (stamp.getSeconds() >> 1);
  const date = ((stamp.getFullYear() - 1980) << 9) | ((stamp.getMonth() + 1) << 5) | stamp.getDate();
  for (const file of files) {
    const name = encoder.encode(folder ? `${folder}/${file.path}` : file.path);
    const data = fileBytes(file);
    const crc = crc32(data);
    const local = new DataView(new ArrayBuffer(30));
    [[0, 0x04034b50, 4], [4, 20, 2], [6, 0x0800, 2], [8, 0, 2], [10, time, 2], [12, date, 2], [14, crc, 4],
      [18, data.length, 4], [22, data.length, 4], [26, name.length, 2], [28, 0, 2]]
      .forEach(([at, value, size]) => (size === 4 ? local.setUint32(at, value, true) : local.setUint16(at, value, true)));
    const entry = new DataView(new ArrayBuffer(46));
    [[0, 0x02014b50, 4], [4, 20, 2], [6, 20, 2], [8, 0x0800, 2], [10, 0, 2], [12, time, 2], [14, date, 2], [16, crc, 4],
      [20, data.length, 4], [24, data.length, 4], [28, name.length, 2], [30, 0, 2], [32, 0, 2], [34, 0, 2], [36, 0, 2],
      [38, 0, 4], [42, offset, 4]]
      .forEach(([at, value, size]) => (size === 4 ? entry.setUint32(at, value, true) : entry.setUint16(at, value, true)));
    parts.push(local, name, data);
    central.push(entry, name);
    offset += 30 + name.length + data.length;
  }
  const size = central.reduce((total, part) => total + part.byteLength, 0);
  const end = new DataView(new ArrayBuffer(22));
  [[0, 0x06054b50, 4], [8, files.length, 2], [10, files.length, 2], [12, size, 4], [16, offset, 4]]
    .forEach(([at, value, bytes]) => (bytes === 4 ? end.setUint32(at, value, true) : end.setUint16(at, value, true)));
  return new Blob([...parts, ...central, end], { type: "application/zip" });
}

// Projects with index.html at the root are web apps: they export as a Delta Chat mini app (.xdc) named after the project.
function coderName(project) {
  const manifest = project.files.find((file) => file.path === "manifest.toml");
  const fromManifest = manifest && manifest.text ? (manifest.text.match(/^\s*name\s*=\s*"([^"\n]{1,60})"/m) || [])[1] : "";
  return (fromManifest || project.name || "").trim() || t("coder_default_name");
}

function isWebApp(project) {
  return project.files.some((file) => file.path === "index.html");
}

function appIcon(name) {
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = 128;
  const context = canvas.getContext("2d");
  let hash = 0;
  for (const char of name) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  const gradient = context.createLinearGradient(0, 0, 128, 128);
  gradient.addColorStop(0, `hsl(${hash % 360} 70% 55%)`);
  gradient.addColorStop(1, `hsl(${(hash + 50) % 360} 70% 40%)`);
  context.fillStyle = gradient;
  context.fillRect(0, 0, 128, 128);
  context.fillStyle = "#fff";
  context.font = "bold 64px system-ui, sans-serif";
  context.textAlign = "center";
  context.textBaseline = "middle";
  context.fillText((name.trim()[0] || "A").toUpperCase(), 64, 68);
  const b64 = canvas.toDataURL("image/png").split(",")[1] || "";
  if (!b64) throw new Error("no canvas");
  return { path: "icon.png", b64 };
}

function xdcFiles(project) {
  const name = coderName(project);
  const files = project.files.filter((file) => !/\.(zip|xdc)$/i.test(file.path) && file.path !== "webxdc.js");
  const manifest = files.find((file) => file.path === "manifest.toml");
  if (!manifest) files.push({ path: "manifest.toml", text: `name = "${name.replace(/"/g, "'")}"\n` });
  if (!files.some((file) => /^icon\.(png|jpg)$/i.test(file.path))) {
    try {
      files.push(appIcon(name));
    } catch (error) {
      // No canvas: the messenger shows its default icon.
    }
  }
  return files;
}

function exportProject(project, asApp) {
  const base = niceName(coderName(project), "app");
  if (asApp) exportSheet(new Blob([makeZip(xdcFiles(project), "")], { type: "application/zip" }), `${base}.xdc`, coderName(project));
  else exportSheet(makeZip(project.files, base), `${base}.zip`, project.summary.slice(0, 300));
}

// Light syntax colouring: comments, strings, numbers and keywords for the usual languages.
const CODE_KEYWORDS = new Set(("def class return if elif else for while in not and or import from as with try except finally raise " +
  "lambda yield pass break continue None True False function const let var new this typeof instanceof async await export default " +
  "switch case null undefined true false public private static void int float double char struct include fn match impl use mut").split(" "));

function highlight(text, path) {
  const hash = /\.(py|sh|rb|ya?ml|toml)$/i.test(path);
  const pattern = hash
    ? /(#[^\n]*)|("""[\s\S]*?"""|'''[\s\S]*?'''|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')|(\b\d+(?:\.\d+)?\b)|(\b[A-Za-z_]\w*\b)/g
    : /(\/\/[^\n]*|\/\*[\s\S]*?\*\/|<!--[\s\S]*?-->)|(`(?:\\.|[^`\\])*`|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')|(\b\d+(?:\.\d+)?\b)|(\b[A-Za-z_]\w*\b)/g;
  const root = h("code", {});
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > last) root.append(text.slice(last, match.index));
    const [token, comment, string, number, word] = match;
    if (comment) root.append(h("span", { class: "tk-c" }, token));
    else if (string) root.append(h("span", { class: "tk-s" }, token));
    else if (number) root.append(h("span", { class: "tk-n" }, token));
    else if (word && CODE_KEYWORDS.has(word)) root.append(h("span", { class: "tk-k" }, token));
    else root.append(token);
    last = match.index + token.length;
  }
  root.append(text.slice(last));
  return root;
}

function fileSheet(file) {
  const tools = h("div", { class: "row wrap" },
    file.text !== undefined ? h("button", { class: "btn small", onclick: () => copyText(file.text).then(() => toast(t("copied"), "ok")) }, icon("copy"), " ", t("copy")) : null,
    h("button", { class: "btn ghost small", onclick: () => exportSheet(new Blob([fileBytes(file)]), file.path.split("/").pop()) }, icon("share"), " ", t("export")));
  openSheet(file.path, [
    h("p", { class: "muted small" }, fmtSize(fileBytes(file).length)),
    file.text !== undefined ? h("pre", { class: "code-view" }, file.text.length > 60000 ? file.text.slice(0, 60000) : highlight(file.text, file.path))
      : h("p", { class: "muted" }, t("coder_binary")),
    tools,
  ]);
}

// Web projects run inside the app: local CSS and JS are inlined into a sandboxed frame.
function previewHtml(files) {
  const byPath = new Map(files.map((file) => [file.path, file]));
  const page = byPath.get("index.html") || files.find((file) => /\.html?$/i.test(file.path));
  if (!page || page.text === undefined) return "";
  const base = page.path.includes("/") ? page.path.slice(0, page.path.lastIndexOf("/") + 1) : "";
  const local = (href) => byPath.get((base + href).replace(/^\.\//, "")) || byPath.get(href.replace(/^\.\//, ""));
  // The messenger's own webxdc.js must never load in the preview: the in-memory stand-in below replaces it.
  const html = page.text
    .replace(/<script[^>]*\ssrc=["'](\.\/)?webxdc\.js["'][^>]*><\/script>/gi, "")
    .replace(/<link[^>]+href=["']([^"']+\.css)["'][^>]*>/gi, (tag, href) => {
      const file = local(href);
      return file && file.text !== undefined ? `<style>${file.text}</style>` : tag;
    })
    .replace(/<script([^>]*)\ssrc=["']([^"']+\.m?js)["']([^>]*)><\/script>/gi, (tag, before, href, after) => {
      const file = local(href);
      return file && file.text !== undefined ? `<script${before}${after}>${file.text.replace(/<\/script/gi, "<\\/script")}</script>` : tag;
    })
    .replace(/(<img[^>]+src=)["']([^"':]+\.(png|jpe?g|gif|webp|svg))["']/gi, (tag, start, href, kind) => {
      const file = local(href);
      if (!file) return tag;
      const type = `image/${kind.toLowerCase().replace("jpg", "jpeg").replace("svg", "svg+xml")}`;
      const data = file.b64 || (file.text !== undefined ? btoa(unescape(encodeURIComponent(file.text))) : "");
      return data ? `${start}"data:${type};base64,${data}"` : tag;
    });
  return /<head[^>]*>/i.test(html) ? html.replace(/<head[^>]*>/i, (tag) => tag + PREVIEW_SHIM) : PREVIEW_SHIM + html;
}

// The preview frame has no origin, so web storage throws there: give pages an in-memory stand-in.
const PREVIEW_SHIM = `<script>(() => {
  const memory = () => { const data = new Map(); return {
    get length() { return data.size; }, key: (i) => [...data.keys()][i] ?? null,
    getItem: (k) => (data.has(String(k)) ? data.get(String(k)) : null),
    setItem: (k, v) => { data.set(String(k), String(v)); }, removeItem: (k) => { data.delete(String(k)); }, clear: () => data.clear() }; };
  for (const name of ["localStorage", "sessionStorage"]) {
    try { window[name].length; } catch (error) { Object.defineProperty(window, name, { value: memory(), configurable: true }); }
  }
  if (!window.webxdc) {
    const updates = [];
    let listener = null;
    window.webxdc = { selfAddr: "preview@local", selfName: "Preview", sendUpdateInterval: 1000, sendUpdateMaxSize: 128000,
      setUpdateListener(fn, serial = 0) { listener = fn; updates.filter((u) => u.serial > serial).forEach((u) => fn(u)); return Promise.resolve(); },
      sendUpdate(update) { const item = { ...update, serial: updates.length + 1, max_serial: updates.length + 1 }; updates.push(item); setTimeout(() => listener && listener(item), 0); },
      sendToChat() { return Promise.reject(new Error("preview")); }, importFiles() { return Promise.resolve([]); }, joinRealtimeChannel() { return { send() {}, setListener() {}, leave() {} }; } };
  }
})();<\/script>`;

function previewSheet(html) {
  openSheet(t("coder_preview"), [
    h("iframe", { class: "code-preview", sandbox: "allow-scripts allow-modals allow-forms", srcdoc: html, title: t("coder_preview") }),
    h("p", { class: "muted small" }, t("coder_preview_hint")),
  ]);
}

function stepRow(step) {
  if (step.kind === "research") {
    return h("div", { class: "step" }, h("span", {}, step.cmd === "open" ? t("coder_open_public") : t("coder_search_public")));
  }
  if (step.kind === "run") {
    return h("div", { class: `step ${step.code === 0 ? "ok" : "bad"}` },
      h("b", {}, "$ ", step.cmd), h("small", {}, step.code === 0 ? t("coder_ok") : t("coder_exit", { code: step.code })),
      step.out ? h("pre", { class: "logs" }, step.out) : null);
  }
  const label = step.kind === "delete" ? t("coder_deleted", { path: step.path }) : t("coder_wrote", { path: step.path, n: step.lines || 0 });
  return h("div", { class: "step" }, h("span", {}, label));
}

VIEWS.coder = {
  tab: "ai",
  title: () => t("coder"),
  render(main) {
    const body = h("div", { class: "stack" });
    const area = autosize(h("textarea", { class: "input", rows: 3 }));
    const pickRow = h("div", { class: "row wrap" });
    const drawPicker = () => {
      const options = coderModels();
      if (prefs.coderModel && !options.some(([key]) => key === prefs.coderModel)) {
        options.unshift([prefs.coderModel, t("coder_model_unavailable")]);
      }
      fill(pickRow, h("small", { class: "muted" }, t("coder_model")),
        select(prefs.coderModel || "", [["", t("auto")], ...options], (value) => { prefs.coderModel = value; savePrefs(); drawPicker(); }), quotaChip("code"));
    };
    const send = () => {
      const text = area.value.trim();
      if (!text) return toast(t("coder_empty"), "bad");
      if (coderSend(text)) area.value = "";
    };
    const draw = () => {
      const project = coder.get();
      const working = project.rid && bridge.pending()[project.rid];
      area.placeholder = project.files.length ? t("coder_change") : t("coder_placeholder");
      const parts = [];
      if (!project.files.length && !project.turns.length) {
        parts.push(card(h("div", { class: "row" }, h("span", { class: "space-icon" }, icon("code")),
          h("div", { class: "grow" }, h("b", {}, t("coder")), h("small", { class: "muted block" }, t("coder_intro")))),
        h("div", { class: "chips" }, ...["coder_ex1", "coder_ex2", "coder_ex3"].map((key) =>
          h("button", { class: "chip", onclick: () => { area.value = t(key); area.dispatchEvent(new Event("input")); area.focus(); } }, t(key))))));
      }
      if (project.turns.length) {
        parts.push(section(t("coder_talk"), h("div", { class: "stack" }, ...project.turns.slice(-6).map((turn) =>
          h("div", { class: `bubble ${turn.role === "user" ? "me" : ""} ${turn.error ? "error" : ""}` }, turn.role === "user" ? turn.text : rich(turn.text))))));
      }
      if (working) parts.push(card(h("div", { class: "row" }, h("i", { class: "spinner" }), h("span", { class: "grow" }, t("coder_working")), since(project.turns.length ? project.turns[project.turns.length - 1].ts : now())),
        h("small", { class: "muted" }, t("coder_working_hint"))));
      if (project.files.length) {
        const html = previewHtml(project.files);
        parts.push(section(null, headRow(t("coder_files", { n: project.files.length }),
          project.lastChanges && project.lastChanges.length ? iconButton("info", t("coder_diff"), () => coderDiffSheet(project)) : null,
          project.history && project.history.length ? iconButton("refresh", t("coder_undo"), coderUndo) : null,
          html ? iconButton("web", t("coder_preview"), () => previewSheet(html)) : null,
          iconButton("downloads", t("coder_zip"), () => exportProject(project, false)),
          iconButton("trash", t("coder_new"), () => confirmBox(t("coder_new"), t("coder_new_body"), t("coder_new")).then((yes) => {
            if (yes) coder.set({ files: [], turns: [], steps: [], history: [], lastChanges: [], summary: "", sig: "", isolated: false, rid: "" });
          }))),
        h("div", { class: "list" }, ...project.files.map((file) => h("button", { class: "list-row", onclick: () => fileSheet(file) },
          icon("file"), h("span", { class: "grow mono" }, file.path), h("small", { class: "muted" }, fmtSize(fileBytes(file).length)), icon("next")))),
        html ? h("button", { class: "btn primary wide", onclick: () => previewSheet(html) }, icon("web"), " ", t("coder_try")) : null,
        isWebApp(project) ? h("button", { class: "btn wide", onclick: () => exportProject(project, true) }, icon("apps"), " ", t("coder_xdc", { name: coderName(project) })) : null,
        isWebApp(project) ? h("p", { class: "muted small" }, t("coder_xdc_hint")) : null,
        h("p", { class: "muted small" }, [project.sig, project.isolated ? t("coder_isolated") : t("coder_no_run")].filter(Boolean).join(" · "))));
      }
      if (project.steps.length) {
        const log = h("div", { class: "stack", hidden: true }, ...project.steps.map(stepRow));
        const runs = project.steps.filter((step) => step.kind === "run");
        const toggle = h("button", { class: "list-row", onclick: () => { log.hidden = !log.hidden; } }, icon("info"),
          h("span", { class: "grow" }, t("coder_steps", { n: project.steps.length, ok: runs.filter((step) => step.code === 0).length, runs: runs.length })), icon("next"));
        parts.push(section(null, h("div", { class: "list" }, toggle), log));
      }
      fill(body, ...parts);
    };
    bus.on("coder", draw);
    bus.on("models", drawPicker);
    bus.on("quota", drawPicker);
    add(main, body, card(area, pickRow, h("div", { class: "row end" },
      h("button", { class: "btn primary", onclick: send }, icon("send"), " ", t("coder_send")))));
    drawPicker();
    draw();
  },
};
