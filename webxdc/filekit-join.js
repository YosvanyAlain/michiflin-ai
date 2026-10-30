"use strict";

// ---------------------------------------------------------------------------
// File-kit joiner: glue raw ".001" parts and compatible multimedia with optional SHA-256 verification.
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

const FileKitJoin = {
  mount(main, params = {}) {
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
      const declared = job ? job.total : (parts.find((part) => part.marker) || { marker: null }).marker?.total;
      const allRaw = parts.every((part) => part.info.kind === "raw");
      const indexes = parts.map(orderOf).filter(Number.isFinite);
      const have = new Set(indexes);
      const expected = Number(declared || (allRaw && indexes.length ? indexes.reduce((max, value) => Math.max(max, value), 0) : 0));
      const allKnownSame = parts.every((part) => part.known && part.known.job.id === parts[0].known?.job.id);
      const mixed = expected > 10000 || indexes.some((index) => index < 1) || have.size !== indexes.length ||
        (allRaw && parts.some((part) => part.info.index < 1 && !part.known)) ||
        (allRaw && new Set(parts.map((part) => part.info.base.toLowerCase())).size > 1 && !allKnownSame) ||
        new Set(parts.filter((part) => part.known).map((part) => part.known.job.id)).size > 1;
      const missing = expected > 0 && expected <= 10000 ? Array.from({ length: expected }, (_, index) => index + 1).filter((index) => !have.has(index)) : [];
      const unordered = parts.some((part) => orderOf(part) === Infinity);
      const label = (part) => (part.known ? t("join_by_hash") : part.marker ? t("join_by_tag") : part.info.index ? t("join_by_name") : t("join_by_none"));
      fill(list,
        card(h("div", { class: "list" }, parts.map((part, index) => h("div", { class: `list-row join-row ${part.bad ? "bad" : part.good || part.known ? "good" : ""}` },
          part.thumb ? h("img", { class: "join-thumb", src: part.thumb, alt: "" }) : h("span", { class: "num" }, String(index + 1)),
          h("span", { class: "grow" },
            h("b", { class: "kit-join-name" }, part.thumb ? `${index + 1}. ${part.file.name}` : part.file.name),
            h("small", { class: "muted" }, [fmtSize(part.file.size), fmtClip(part.duration), label(part)].filter(Boolean).join(" · "))),
          part.bad ? icon("alert") : part.known ? icon("check") : null,
          iconButton("up", t("move_up"), () => move(index, index - 1), "tiny"),
          iconButton("down", t("move_down"), () => move(index, index + 1), "tiny"),
          iconButton("close", t("remove"), () => { parts.splice(index, 1); draw(); }, "tiny")))),
          h("p", { class: "muted small" }, t("join_total", { n: parts.length, size: fmtSize(total) }))),
        missing.length ? h("p", { class: "note bad" }, icon("alert"), t("join_missing", { list: missing.slice(0, 100).join(", ") })) : null,
        mixed ? h("p", { class: "note bad" }, icon("alert"), t("join_mixed")) : null,
        unordered ? h("p", { class: "note warn" }, icon("info"), t("join_unordered")) : null,
        method === "none" ? h("p", { class: "note warn" }, icon("info"), t("join_segments")) : null,
        h("label", { class: "field-label" }, t("join_name")), nameInput,
        h("button", { class: "btn primary wide", disabled: mixed || !!missing.length || method === "none" || parts.length < 2 && method !== "bytes", onclick: (event) => join(event.currentTarget, method) },
          icon("join"), t("join_go")));
      nameInput.value = name;
    };
    const addFiles = async (files) => {
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
    const pick = async () => {
      try { await addFiles(await pickFiles()); }
      catch (error) { toast(t("pick_failed"), "bad"); }
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
      const saveCopy = h("button", { class: "btn wide", onclick: async () => {
        saveCopy.disabled = true;
        try {
          FileKitStore.name(finalName);
          if (params.canSave && !params.canSave()) throw new Error("join_local_only");
          const destination = params.getFolder ? params.getFolder() : "";
          await FileKitStore.put(destination, [{ path: finalName, blob }]);
          if (params.onSaved) await params.onSaved();
          toast(t("kit_saved"), "ok");
        } catch (error) {
          saveCopy.disabled = false;
          const key = `kit_err_${error.message}`;
          toast(has(key) ? t(key) : t("kit_error"), "bad");
        }
      } }, icon("folder"), t("kit_save_join"));
      fill(output, card(h("div", { class: "row" }, icon("file", "big"), h("span", { class: "grow" }, h("b", {}, finalName), h("small", { class: "muted" }, fmtSize(blob.size)))),
        verdict, preview, saveCopy, h("button", { class: "btn primary wide", onclick: () => exportSheet(blob, finalName) }, icon("share"), t("export"))));
      output.scrollIntoView({ behavior: "smooth", block: "start" });
    };
    add(main, card(h("div", { class: "row" }, icon("join", "big accent"), h("p", { class: "grow" }, t("join_intro"))),
      h("button", { class: "btn primary wide", onclick: pick }, icon("plus"), t("join_pick")),
      h("p", { class: "note" }, icon("info"), t("join_files_hint"))), list, output);
    draw();
    const addPaths = async (paths) => {
      const files = await Promise.all(paths.map(async (path) => {
        const { blob } = await FileKitStore.get(path);
        return new File([blob], FileKitStore.base(path), { type: blob.type });
      }));
      await addFiles(files);
    };
    return { addPaths };
  },
};

