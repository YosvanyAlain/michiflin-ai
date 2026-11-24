"use strict";
// Local books are private to this WebXDC message. Nothing here migrates other
// instances or sends a backup unless the person explicitly exports it.
window.BookShelf = (() => {
  const text = (es, en) => lang() === "en" ? en : es;
  const DB_NAME = "michiflin-reading-library-v1", MAX_BOOKS = 40, MAX_TEXT_BYTES = 3000000;
  const MAX_IMPORT_BYTES = 40 * 1024 * 1024, MAX_PRESENTATION = 5000;
  const coverPending = new Map(), coverQueue = [];
  let coverBusy = 0, speechRevision = 0, readerCleanup = null;
  const cached = store.get("books.coverCache", []);
  const recentCovers = new Map(cached.filter(entry => Array.isArray(entry) && entry.length === 2 &&
    entry[1] && typeof entry[1].at === "number" && Date.now() - entry[1].at < 86400000));

  function pumpCovers() {
    while (coverBusy < 2 && coverQueue.length) {
      const task = coverQueue.shift(); coverBusy++;
      bridge.call("books", { do: "cover", url: task.url }).then(response => {
        if (typeof response.data !== "string" || !/^data:image\/jpeg;base64,[A-Za-z0-9+/=]+$/.test(response.data) || response.data.length > 200000) throw new Error("cover");
        recentCovers.set(task.url, { data: response.data, at: Date.now() });
        while (recentCovers.size > 24) recentCovers.delete(recentCovers.keys().next().value);
        store.set("books.coverCache", [...recentCovers], true); task.resolve(response.data);
      }).catch(task.reject).finally(() => { coverBusy--; coverPending.delete(task.url); pumpCovers(); });
    }
  }
  function coverData(url) {
    const hit = recentCovers.get(url);
    if (hit && typeof hit.data === "string" && /^data:image\/jpeg;base64,[A-Za-z0-9+/=]+$/.test(hit.data)) return Promise.resolve(hit.data);
    if (!coverPending.has(url)) {
      const promise = new Promise((resolve, reject) => coverQueue.push({ url, resolve, reject }));
      coverPending.set(url, promise); pumpCovers();
    }
    return coverPending.get(url);
  }
  function stopSpeech() {
    speechRevision++;
    try { const voice = window.speechSynthesis; if (voice && typeof voice.cancel === "function") voice.cancel(); }
    catch (_) { /* An unavailable voice must not prevent reading. */ }
  }
  function leave() {
    stopSpeech();
    if (readerCleanup) { const cleanup = readerCleanup; readerCleanup = null; cleanup(); }
  }
  const database = () => new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => request.result.createObjectStore("books", { keyPath: "id" });
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(new Error("storage"));
    request.onblocked = () => reject(new Error("storage"));
  });
  async function operation(mode, action) {
    const db = await database();
    try { return await new Promise((resolve, reject) => {
      const tx = db.transaction("books", mode), request = action(tx.objectStore("books"));
      tx.oncomplete = () => resolve(request.result);
      tx.onerror = tx.onabort = () => reject(new Error("storage"));
    }); } finally { db.close(); }
  }
  const all = () => operation("readonly", objectStore => objectStore.getAll());
  const remove = id => operation("readwrite", objectStore => objectStore.delete(id));
  const byteLength = value => new TextEncoder().encode(value).length;
  const clamp = (number, min, max) => Math.max(min, Math.min(max, number));
  // Only display the bot's bounded, explicitly localized library messages;
  // arbitrary exceptions may contain technical details or private URLs.
  const libraryError = error => {
    const message = error && typeof error.message === "string" ? error.message : "";
    return /^(La fuente |La conexión |El archivo |El PDF |El EPUB |La ficha |No se pudo consultar la fuente |No se pudo leer el PDF |The source |The connection |The file |The PDF |The EPUB |The record |Could not query the source |Could not read the PDF )/.test(message) ? message.slice(0, 250) : "";
  };
  const safeNumber = (value, fallback = 0) => Number.isFinite(Number(value)) ? Number(value) : fallback;
  const safeURL = value => {
    try { const url = new URL(String(value || "")); return url.protocol === "https:" && !url.username && !url.password ? url.href : ""; }
    catch (_) { return ""; }
  };
  function exportText(book) {
    exportSheet(new Blob([book.title + "\n" + (book.authors || []).join(", ") + "\n\n" + book.text],
      { type: "text/plain;charset=utf-8" }), niceName(book.title, "libro") + ".txt");
  }
  function cover(book, localOnly = false) {
    const box = h("div", { class: "book-cover", "aria-label": book.title }, icon("book"));
    if (!safeURL(book.cover_url) || (localOnly && !recentCovers.has(book.cover_url))) return box;
    const load = async () => {
      try {
        if (!box.isConnected) return;
        const data = await coverData(book.cover_url);
        if (box.isConnected) fill(box, h("img", { src: data, alt: "", loading: "lazy", decoding: "async" }));
      } catch (_) { /* A book remains usable without its cover. */ }
    };
    if (window.IntersectionObserver) {
      const observer = new IntersectionObserver(entries => { if (entries.some(e => e.isIntersecting)) { observer.disconnect(); load(); } },
        { root: $("#view"), rootMargin: "40px" });
      observer.observe(box);
    } else setTimeout(load, 0);
    return box;
  }

  // Validate offsets and coverage before rendering imported/server data. Never
  // put a book's markup into innerHTML or load external images in the reader.
  function presentationFor(book) {
    const input = book.presentation, value = book.text;
    if (!Array.isArray(input) || input.length > MAX_PRESENTATION || typeof value !== "string") return null;
    const allowed = new Set(["p", "blockquote", "li", "pre", "h1", "h2", "h3", "h4", "h5", "h6"]);
    let end = 0, images = 0, imageBytes = 0, runCount = 0;
    const result = [];
    for (const row of input) {
      if (!row || typeof row !== "object") return null;
      if (row.kind === "image") {
        if (!Number.isInteger(row.at) || row.at < end || row.at > value.length || typeof row.data !== "string" ||
            row.data.length > 150000 || !/^data:image\/jpeg;base64,[A-Za-z0-9+/=]+$/.test(row.data)) return null;
        images++; imageBytes += row.data.length;
        if (images > 12 || imageBytes > 510000) return null;
        result.push({ kind: "image", at: row.at, data: row.data, alt: typeof row.alt === "string" ? row.alt.slice(0, 240) : "" });
        continue;
      }
      if (!allowed.has(row.kind) || !Number.isInteger(row.start) || !Number.isInteger(row.end) ||
          row.start < end || row.end <= row.start || row.end > value.length || /\S/.test(value.slice(end, row.start))) return null;
      const runs = [];
      if (row.runs !== undefined) {
        if (!Array.isArray(row.runs)) return null;
        let runEnd = row.start;
        for (const run of row.runs) {
          if (!Array.isArray(run) || run.length !== 3 || !Number.isInteger(run[0]) || !Number.isInteger(run[1]) ||
              run[0] < runEnd || run[1] <= run[0] || run[1] > row.end || !["b", "i", "bi"].includes(run[2])) return null;
          runEnd = run[1]; runs.push(run); runCount++;
          if (runCount > 20000) return null;
        }
      }
      result.push({ kind: row.kind, start: row.start, end: row.end, runs }); end = row.end;
    }
    return /\S/.test(value.slice(end)) || !result.some(row => row.kind !== "image") ? null : result;
  }
  function normalBook(input) {
    if (!input || typeof input !== "object" || typeof input.id !== "string" || !input.id || input.id.length > 220 ||
        /[\u0000-\u001f]/.test(input.id) || typeof input.title !== "string" || !input.title.trim() || input.title.length > 500 ||
        typeof input.text !== "string" || !input.text.trim() || byteLength(input.text) > MAX_TEXT_BYTES) throw new Error("invalid_book");
    const book = { id: input.id, title: input.title, text: input.text,
      authors: Array.isArray(input.authors) ? input.authors.filter(a => typeof a === "string").slice(0, 20).map(a => a.slice(0, 200)) : [],
      languages: Array.isArray(input.languages) ? input.languages.filter(a => typeof a === "string").slice(0, 8).map(a => a.slice(0, 20)) : [],
      source: typeof input.source === "string" ? input.source.slice(0, 100) : "",
      source_format: typeof input.source_format === "string" ? input.source_format.slice(0, 20) : "txt",
      description: typeof input.description === "string" ? input.description.slice(0, 5000) : "",
      access: typeof input.access === "string" ? input.access.slice(0, 100) : "",
      downloaded: safeNumber(input.downloaded, Date.now()), url: safeURL(input.url), cover_url: safeURL(input.cover_url),
      sha256: typeof input.sha256 === "string" ? input.sha256.slice(0, 100) : "",
      presentation_warning: ["layout_unavailable", "some_images_omitted"].includes(input.presentation_warning) ? input.presentation_warning : "" };
    const visual = presentationFor({ text: book.text, presentation: input.presentation });
    if (visual) book.presentation = visual;
    return book;
  }
  function makePages(value) {
    const pages = [];
    let start = 0;
    while (start < value.length) {
      let cut = Math.min(start + 4500, value.length);
      if (cut < value.length) {
        const boundary = value.lastIndexOf("\n", cut);
        if (boundary > start + 2200) cut = boundary;
        // Don't split an emoji in two; normal text uses the legacy page layout.
        if (cut > start && /[\uD800-\uDBFF]/.test(value[cut - 1]) && /[\uDC00-\uDFFF]/.test(value[cut])) cut--;
      }
      pages.push({ start, end: cut }); start = cut;
    }
    return pages.length ? pages : [{ start: 0, end: 0 }];
  }
  function positionFor(book, pages) {
    const saved = store.get("book.offset." + book.id, null);
    if (typeof saved === "number" && Number.isFinite(saved)) return clamp(Math.floor(saved), 0, Math.max(0, book.text.length - 1));
    const oldPage = clamp(Math.floor(safeNumber(store.get("book.page." + book.id, 0))), 0, pages.length - 1);
    return pages[oldPage].start;
  }
  function localMarks(book) {
    const marks = store.get("book.marks." + book.id, []);
    return marks.filter(item => item && Number.isInteger(item.offset) && item.offset >= 0 && item.offset < book.text.length &&
      typeof item.label === "string").slice(0, 30).map(item => ({ offset: item.offset, label: item.label.slice(0, 100) }));
  }
  function progressOf(book) {
    const pages = makePages(book.text);
    return { offset: positionFor(book, pages), marks: localMarks(book) };
  }
  function renderRuns(node, book, row, start, end) {
    let at = start;
    for (const run of row.runs || []) {
      const from = Math.max(start, run[0]), to = Math.min(end, run[1]);
      if (from >= to) continue;
      if (from > at) node.append(document.createTextNode(book.text.slice(at, from)));
      let span = document.createTextNode(book.text.slice(from, to));
      if (run[2].includes("i")) span = h("em", {}, span);
      if (run[2].includes("b")) span = h("strong", {}, span);
      node.append(span); at = to;
    }
    if (at < end) node.append(document.createTextNode(book.text.slice(at, end)));
  }
  function renderPage(book, pages, index, content, visual) {
    const { start, end } = pages[index];
    content.replaceChildren();
    if (visual) {
      for (const row of visual) {
        if (row.kind === "image") {
          if (row.at >= start && (row.at < end || (index === pages.length - 1 && row.at === end))) {
            add(content, h("figure", { class: "book-figure" }, h("img", { src: row.data, alt: row.alt, loading: "lazy" })));
          }
          continue;
        }
        const from = Math.max(start, row.start), to = Math.min(end, row.end);
        if (from >= to) continue;
        const tag = row.kind === "li" ? "p" : row.kind;
        const node = h(tag, { "data-book-start": from, "data-book-end": to, class: row.kind === "li" ? "book-list-item" : "" });
        renderRuns(node, book, row, from, to); add(content, node);
      }
    } else {
      const slice = book.text.slice(start, end), regexp = /[^\n]+(?:\n(?!\n)[^\n]+)*/g;
      let match;
      while ((match = regexp.exec(slice))) {
        add(content, h("p", { "data-book-start": start + match.index, "data-book-end": start + match.index + match[0].length }, match[0]));
      }
    }
  }
  function reader(book) {
    leave();
    const main = $("#view"), pages = makePages(book.text), visual = presentationFor(book);
    let offset = positionFor(book, pages), page = 0, scrollTimer = null, releaseTimer = null, drawRevision = 0, suppressScroll = false;
    const initial = store.get("book.readerPrefs", {});
    const settings = { theme: ["paper", "sepia", "night"].includes(initial.theme) ? initial.theme : "paper",
      font: ["serif", "sans"].includes(initial.font) ? initial.font : "serif",
      size: clamp(safeNumber(initial.size, 19), 16, 28), line: clamp(safeNumber(initial.line, 1.7), 1.4, 2.2),
      width: ["narrow", "normal", "wide"].includes(initial.width) ? initial.width : "normal" };
    const root = h("div", { class: "book-reader", "data-theme": settings.theme, "data-font": settings.font, "data-width": settings.width });
    const content = h("article", { class: "book-reading", tabindex: "0", "aria-label": book.title });
    const labels = [h("span", { class: "book-page-label", "aria-live": "polite" }), h("span", { class: "book-page-label" })];
    const progressWarning = h("p", { class: "book-progress-warning", hidden: true, role: "status" },
      text("No se pudo guardar el progreso local. Comprueba el espacio disponible y exporta el libro.", "Reading progress could not be saved locally. Check available storage and export the book."));
    const ranges = [];
    const prevs = [], nexts = [];
    const pageFor = value => {
      let lo = 0, hi = pages.length - 1;
      while (lo < hi) { const middle = Math.floor((lo + hi + 1) / 2); if (pages[middle].start <= value) lo = middle; else hi = middle - 1; }
      return lo;
    };
    const saveProgress = value => {
      offset = clamp(Math.floor(value), 0, Math.max(0, book.text.length - 1));
      const offsetSaved = store.set("book.offset." + book.id, offset, true);
      const pageSaved = store.set("book.page." + book.id, page, true);
      progressWarning.hidden = offsetSaved && pageSaved;
      const percent = book.text.length ? Math.round(offset * 100 / book.text.length) : 0;
      labels.forEach(label => { label.textContent = `${text("Parte", "Part")} ${page + 1} / ${pages.length} · ${percent}%`; });
      ranges.forEach(range => { range.value = String(percent); range.setAttribute("aria-valuetext", `${percent}%`); });
    };
    const actualOffset = () => {
      const top = main.getBoundingClientRect().top + 12;
      const nodes = [...content.querySelectorAll("[data-book-start]")];
      if (!nodes.length) return pages[page].start;
      // Scrolling back to the toolbar must not move a bookmark or voice
      // position to the start of the part; keep the last visible text offset.
      if (nodes[0].getBoundingClientRect().top > top) return clamp(offset, pages[page].start, Math.max(pages[page].start, pages[page].end - 1));
      const node = nodes.find(item => item.getBoundingClientRect().bottom > top) || nodes[nodes.length - 1];
      const rect = node.getBoundingClientRect();
      const portion = clamp((top - rect.top) / Math.max(1, rect.height), 0, 1);
      return safeNumber(node.dataset.bookStart) + Math.round(portion * (safeNumber(node.dataset.bookEnd) - safeNumber(node.dataset.bookStart)));
    };
    const restore = value => {
      if (value <= pages[page].start) { main.scrollTop = 0; return; }
      const nodes = [...content.querySelectorAll("[data-book-start]")];
      const node = nodes.find(item => safeNumber(item.dataset.bookEnd) > value) || nodes[nodes.length - 1];
      if (!node) return;
      const rect = node.getBoundingClientRect(), viewRect = main.getBoundingClientRect();
      const from = safeNumber(node.dataset.bookStart), to = safeNumber(node.dataset.bookEnd);
      const fraction = clamp((value - from) / Math.max(1, to - from), 0, 1);
      main.scrollTop += rect.top - viewRect.top + fraction * rect.height - 12;
    };
    const draw = value => {
      stopSpeech(); clearTimeout(scrollTimer); clearTimeout(releaseTimer); suppressScroll = true;
      const revision = ++drawRevision;
      const target = clamp(Math.floor(value), 0, Math.max(0, book.text.length - 1));
      page = pageFor(target); renderPage(book, pages, page, content, visual);
      prevs.forEach(button => { button.disabled = page === 0; });
      nexts.forEach(button => { button.disabled = page === pages.length - 1; });
      saveProgress(target);
      main.scrollTop = 0;
      requestAnimationFrame(() => {
        if (!root.isConnected || revision !== drawRevision) return;
        restore(target);
        releaseTimer = setTimeout(() => {
          if (revision === drawRevision) { suppressScroll = false; if (root.isConnected) saveProgress(actualOffset()); }
        }, 120);
      });
    };
    const nav = index => {
      const previous = h("button", { class: "btn ghost", onclick: () => draw(pages[Math.max(0, page - 1)].start) }, icon("back"), text("Anterior", "Previous"));
      const next = h("button", { class: "btn ghost", onclick: () => draw(pages[Math.min(pages.length - 1, page + 1)].start) }, text("Siguiente", "Next"), icon("next"));
      prevs.push(previous); nexts.push(next);
      const range = h("input", { type: "range", min: "0", max: "100", step: "1", value: "0", "aria-label": text("Ir a porcentaje del libro", "Go to book percentage"),
        onchange: event => draw(Math.floor(safeNumber(event.target.value) * Math.max(0, book.text.length - 1) / 100)) });
      ranges.push(range);
      return h("div", { class: "book-nav", "aria-label": text("Navegación de lectura", "Reading navigation") },
        h("div", { class: "book-nav-buttons" }, previous, labels[index], next), range);
    };
    const applySettings = () => {
      root.dataset.theme = settings.theme; root.dataset.font = settings.font; root.dataset.width = settings.width;
      root.style.setProperty("--book-font-size", settings.size + "px"); root.style.setProperty("--book-line-height", settings.line);
      store.set("book.readerPrefs", settings, true);
    };
    function choice(label, current, choices, callback) {
      const control = select(current, choices, value => { callback(value); applySettings(); });
      return h("label", { class: "book-setting" }, h("span", {}, label), control);
    }
    const sizePicker = h("input", { type: "range", min: "16", max: "28", step: "1", value: String(settings.size),
      "aria-label": text("Tamaño de letra", "Font size"), oninput: event => { settings.size = safeNumber(event.target.value, 19); applySettings(); } });
    const linePicker = h("input", { type: "range", min: "1.4", max: "2.2", step: ".1", value: String(settings.line),
      "aria-label": text("Interlineado", "Line spacing"), oninput: event => { settings.line = safeNumber(event.target.value, 1.7); applySettings(); } });
    const appearance = h("details", { class: "book-disclosure" }, h("summary", {}, text("Ajustes de lectura", "Reading settings")),
      h("div", { class: "book-settings-grid" },
        h("label", { class: "book-setting" }, h("span", {}, text("Tamaño de letra", "Font size")), sizePicker),
        h("label", { class: "book-setting" }, h("span", {}, text("Interlineado", "Line spacing")), linePicker),
        choice(text("Fondo", "Background"), settings.theme, [["paper", text("Claro", "Light")], ["sepia", "Sepia"], ["night", text("Noche", "Night")]], value => { settings.theme = value; }),
        choice(text("Tipografía", "Typeface"), settings.font, [["serif", "Serif"], ["sans", "Sans"]], value => { settings.font = value; }),
        choice(text("Ancho del texto", "Text width"), settings.width, [["narrow", text("Estrecho", "Narrow")], ["normal", "Normal"], ["wide", text("Ancho", "Wide")]], value => { settings.width = value; })));
    const searchInput = field({ type: "search", maxlength: "200", "aria-label": text("Buscar dentro del libro", "Find in book"), placeholder: text("Buscar en este libro", "Find in this book") });
    const find = () => {
      const query = searchInput.value.trim();
      if (!query) return;
      // Escaped literal matching keeps offsets in the original UTF-16 text;
      // locale lowercasing can expand characters and move the found position.
      const pattern = new RegExp(query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "iu");
      const from = Math.min(book.text.length, actualOffset() + 1);
      const relative = book.text.slice(from).search(pattern);
      let found = relative < 0 ? -1 : from + relative;
      if (found < 0) found = book.text.search(pattern);
      if (found < 0) toast(text("No se encontró ese texto.", "Text not found."), "bad");
      else draw(found);
    };
    searchInput.addEventListener("keydown", event => { if (event.key === "Enter") { event.preventDefault(); find(); } });
    const headings = (visual || []).filter(row => /^h[1-6]$/.test(row.kind)).slice(0, 100);
    const contents = h("details", { class: "book-disclosure" }, h("summary", {}, text("Índice", "Contents")),
      headings.length ? h("div", { class: "book-contents" }, headings.map(row => h("button", { class: "btn ghost small", onclick: () => draw(row.start) }, book.text.slice(row.start, row.end).slice(0, 140)))) :
        h("p", { class: "muted small" }, text("Este libro no incluye un índice detectable en la copia de lectura.", "This reading copy has no detectable contents.")));
    const markList = h("div", { class: "book-contents" });
    const markDisclosure = h("details", { class: "book-disclosure" }, h("summary", {}, text("Marcadores", "Bookmarks")), markList);
    const drawMarks = () => {
      const marks = localMarks(book); markList.replaceChildren();
      if (!marks.length) add(markList, h("p", { class: "muted small" }, text("Aún no hay marcadores.", "No bookmarks yet.")));
      marks.forEach(item => add(markList, h("div", { class: "book-mark" },
        h("button", { class: "btn ghost small", onclick: () => draw(item.offset) }, item.label),
        h("button", { class: "icon-btn", "aria-label": text("Eliminar marcador", "Delete bookmark"), onclick: () => {
          store.set("book.marks." + book.id, localMarks(book).filter(mark => mark.offset !== item.offset)); drawMarks();
        } }, icon("trash")))));
    };
    const mark = h("button", { class: "btn ghost small", onclick: () => {
      const at = clamp(actualOffset(), 0, Math.max(0, book.text.length - 1)), marks = localMarks(book);
      if (marks.length >= 30) return toast(text("Límite de 30 marcadores.", "Limit of 30 bookmarks."), "bad");
      if (!marks.some(item => Math.abs(item.offset - at) < 10)) marks.push({ offset: at, label: book.text.slice(at, at + 70).replace(/\s+/g, " ").trim() || `${Math.round(at * 100 / book.text.length)}%` });
      store.set("book.marks." + book.id, marks); drawMarks(); markDisclosure.open = true;
    } }, text("Marcar lugar", "Bookmark place"));
    let canSpeak = false;
    try { canSpeak = !!(window.speechSynthesis && window.SpeechSynthesisUtterance && typeof window.speechSynthesis.speak === "function" && typeof window.speechSynthesis.getVoices === "function"); } catch (_) {}
    const read = h("button", { class: "btn ghost small", disabled: !canSpeak, onclick: () => {
      const start = actualOffset(); stopSpeech(); const revision = speechRevision;
      const parts = book.text.slice(start).match(/[\s\S]{1,1400}/g) || [];
      const rawLanguage = String((book.languages || [lang()])[0] || lang()).toLowerCase().split("-")[0];
      const voiceLanguage = ({ spa: "es", eng: "en", fra: "fr", fre: "fr", por: "pt" })[rawLanguage] || rawLanguage;
      try {
        // Avoid silently routing private book text to a cloud voice. Some
        // clients report no local voices; then export remains available.
        const voice = speechSynthesis.getVoices().find(item => item.localService === true &&
          String(item.lang || "").toLowerCase().split("-")[0] === voiceLanguage);
        if (!voice) return toast(text("No hay una voz local disponible para este idioma. Puedes exportar el texto.", "No local voice is available for this language. You can export the text."), "bad");
        const speak = index => {
          if (revision !== speechRevision || !root.isConnected || index >= parts.length) return;
          const utterance = new SpeechSynthesisUtterance(parts[index]);
          utterance.lang = voice.lang; utterance.voice = voice;
          utterance.onend = () => speak(index + 1);
          utterance.onerror = event => { if (revision === speechRevision && !["canceled", "interrupted"].includes(event.error)) toast(text("La voz del sistema no está disponible.", "System speech is unavailable."), "bad"); };
          speechSynthesis.speak(utterance);
        };
        speak(0);
      } catch (_) { toast(text("La voz del sistema no está disponible.", "System speech is unavailable."), "bad"); }
    } }, icon("audio"), text("Leer en voz alta", "Read aloud"));
    const warning = visual ? (book.presentation_warning === "some_images_omitted" ? text("EPUB adaptado: algunas imágenes no se pudieron incluir.", "Adapted EPUB: some images could not be included.") : text("EPUB adaptado para lectura; la maquetación original puede variar.", "EPUB adapted for reading; original layout may differ.")) :
      text("Copia de texto: no conserva la maquetación ni las ilustraciones originales.", "Text copy: original layout and illustrations are not preserved.");
    fill(root, h("header", { class: "book-reader-head" },
      h("button", { class: "btn ghost small", onclick: () => { leave(); go("books", { tab: "saved" }); } }, icon("back"), text("Mi biblioteca", "My library")),
      h("h2", {}, book.title), h("p", { class: "book-byline" }, (book.authors || []).join(", ")),
      h("p", { class: "book-format-note" }, warning)),
      nav(0), progressWarning,
      h("details", { class: "book-toolbox" }, h("summary", {}, text("Herramientas de lectura", "Reading tools"),
        h("small", {}, text("Ajustes · búsqueda · marcadores", "Settings · find · bookmarks"))),
        appearance,
        h("details", { class: "book-disclosure" }, h("summary", {}, text("Buscar en el libro", "Find in book")),
          h("div", { class: "book-find" }, searchInput, h("button", { class: "btn ghost", onclick: find }, text("Buscar siguiente", "Find next")))),
        contents, markDisclosure,
        h("div", { class: "book-reader-actions" }, mark, read,
          h("button", { class: "btn ghost small", onclick: stopSpeech }, icon("pause"), text("Detener voz", "Stop speech")),
          h("button", { class: "btn ghost small", onclick: () => exportText(book) }, icon("share"), text("Exportar al lector del sistema", "Export to system reader")))),
      content, nav(1));
    fill(main, root); drawMarks(); applySettings();
    const onScroll = () => {
      if (suppressScroll || !root.isConnected) return;
      offset = actualOffset();
      clearTimeout(scrollTimer); scrollTimer = setTimeout(() => saveProgress(offset), 250);
    };
    main.addEventListener("scroll", onScroll, { passive: true });
    readerCleanup = () => { drawRevision++; clearTimeout(scrollTimer); clearTimeout(releaseTimer); if (root.isConnected && !suppressScroll) saveProgress(actualOffset()); main.removeEventListener("scroll", onScroll); };
    draw(offset);
  }

  // Enforce the 40-book limit inside the same write transaction as the insert.
  // This remains correct if two downloads or an import finish concurrently.
  async function saveNew(book) {
    const db = await database(); let saved = null, reason = "storage";
    try { return await new Promise((resolve, reject) => {
      const tx = db.transaction("books", "readwrite"), objectStore = tx.objectStore("books"), request = objectStore.getAll();
      request.onsuccess = () => {
        const existing = request.result.find(item => item.id === book.id);
        if (existing) { saved = existing; return; }
        if (request.result.length >= MAX_BOOKS) { reason = "capacity"; tx.abort(); return; }
        saved = book; objectStore.put(book);
      };
      tx.oncomplete = () => resolve(saved);
      tx.onerror = tx.onabort = () => reject(new Error(reason));
    }); } finally { db.close(); }
  }
  // Merge, never overwrite a book with the same id, and commit all new books
  // in a single IndexedDB transaction. Progress is restored only afterwards.
  async function importBatch(records) {
    const db = await database(); let result = null, reason = "storage";
    try { return await new Promise((resolve, reject) => {
      const tx = db.transaction("books", "readwrite"), objectStore = tx.objectStore("books"), request = objectStore.getAll();
      request.onsuccess = () => {
        const seen = new Set(request.result.map(item => item.id)), added = [], skipped = [];
        for (const record of records) {
          // A repeated legacy TXT has a fresh local id but identical contents.
          // Never overwrite an existing copy or its reading progress.
          const sameText = record.isTxt && [...request.result, ...added.map(item => item.book)].some(item => item.text === record.book.text);
          if (seen.has(record.book.id) || sameText) skipped.push(record.book.id);
          else { seen.add(record.book.id); added.push(record); }
        }
        if (seen.size > MAX_BOOKS) { reason = "capacity"; tx.abort(); return; }
        added.forEach(record => objectStore.put(record.book)); result = { added, skipped };
      };
      tx.oncomplete = () => resolve(result);
      tx.onerror = tx.onabort = () => reject(new Error(reason));
    }); } finally { db.close(); }
  }
  function fileText(file) {
    if (file.text) return file.text();
    return new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result)); reader.onerror = () => reject(new Error("read")); reader.readAsText(file, "utf-8"); });
  }
  async function parseFiles(files) {
    if (!files.length || files.length > MAX_BOOKS || files.reduce((sum, file) => sum + file.size, 0) > MAX_IMPORT_BYTES) throw new Error("size");
    const records = [];
    for (const file of files) {
      if (file.size > MAX_IMPORT_BYTES) throw new Error("size");
      const value = await fileText(file);
      if (/\.json$/i.test(file.name)) {
        const payload = JSON.parse(value);
        if (!payload || payload.type !== "michiflin-library" || payload.version !== 1 || !Array.isArray(payload.books) || payload.books.length > MAX_BOOKS) throw new Error("format");
        for (const item of payload.books) {
          const book = normalBook(item && item.book);
          const p = item && item.progress && typeof item.progress === "object" ? item.progress : {};
          const offset = Number.isInteger(p.offset) ? clamp(p.offset, 0, Math.max(0, book.text.length - 1)) : 0;
          const marks = Array.isArray(p.marks) ? p.marks.filter(m => m && Number.isInteger(m.offset) && m.offset >= 0 && m.offset < book.text.length && typeof m.label === "string").slice(0, 30).map(m => ({ offset: m.offset, label: m.label.slice(0, 100) })) : [];
          records.push({ book, offset, marks });
        }
      } else if (/\.txt$/i.test(file.name)) {
        const name = file.name.replace(/\.txt$/i, "").slice(0, 500) || text("Libro importado", "Imported book");
        // Local ID only; it is not used for authorization or cryptography.
        const id = "import:" + Date.now().toString(36) + ":" + Math.random().toString(36).slice(2) + ":" + records.length;
        const book = normalBook({ id, title: name, text: value.replace(/^\ufeff/, ""), authors: [], source: text("Importado", "Imported"), source_format: "txt" });
        records.push({ book, offset: 0, marks: [], isTxt: true });
      } else throw new Error("format");
      if (records.length > MAX_BOOKS) throw new Error("size");
    }
    return records;
  }
  async function exportBackup(books) {
    const payload = { type: "michiflin-library", version: 1, exported: new Date().toISOString(),
      books: books.map(book => ({ book: normalBook(book), progress: progressOf(book) })) };
    const blob = new Blob([JSON.stringify(payload)], { type: "application/json;charset=utf-8" });
    if (blob.size > MAX_IMPORT_BYTES) {
      toast(text("La copia supera 40 MB. Exporta los libros individualmente como TXT.", "The backup exceeds 40 MB. Export books individually as TXT."), "bad"); return;
    }
    exportSheet(blob, "michiflin-biblioteca.json");
  }

  async function mount(main, params = {}) {
    leave();
    let local = new Map(), revision = 0, active = params.tab === "saved" ? "saved" : "catalogue", page = 0, pending = false;
    let source = store.get("books.source", "all"), language = store.get("books.language", ""), shown = new Set(), slowTimer = null;
    const shelf = h("div", { class: "book-shelf" });
    const status = h("p", { class: "book-status muted small", role: "status", "aria-live": "polite" });
    const query = field({ type: "search", maxlength: "200", "aria-label": text("Título, autor o tema", "Title, author or topic"),
      placeholder: text("Título, autor o tema", "Title, author or topic"), value: params.q || store.get("books.query", "") });
    const savedFilter = field({ type: "search", "aria-label": text("Filtrar mis libros", "Filter my books"), placeholder: text("Filtrar mis libros", "Filter my books") });
    const sourcePicker = select(source, [["all", text("Todos los catálogos", "All catalogues")], ["gutenberg", "Project Gutenberg"], ["openlibrary", "Open Library"],
      ["archive", "Internet Archive"], ["google", "Google Books · gratuitos"], ["standard", "Standard Ebooks"], ["wikisource", "Wikisource"], ["web", text("Buscar en la web", "Search the web")]], value => { source = value; store.set("books.source", value); search(false); });
    sourcePicker.setAttribute("aria-label", text("Catálogo", "Catalogue"));
    const languagePicker = select(language, [["", text("Todos los idiomas", "All languages")], ["es", text("Español", "Spanish")], ["en", text("Inglés", "English")], ["fr", text("Francés", "French")], ["pt", text("Portugués", "Portuguese")]], value => { language = value; store.set("books.language", value); search(false); });
    languagePicker.setAttribute("aria-label", text("Idioma del libro", "Book language"));
    const more = h("button", { class: "btn ghost wide", hidden: true, onclick: () => search(true) }, text("Más libros", "More books"));
    const searchButton = h("button", { class: "btn primary", onclick: () => search(false) }, icon("search"), t("act_search"));
    const refresh = h("button", { class: "icon-btn", "aria-label": t("refresh"), title: t("refresh"), onclick: () => search(false) }, icon("refresh"));
    const details = book => {
      const url = safeURL(book.url);
      openSheet(book.title, [h("p", {}, (book.authors || []).join(", ")),
        h("p", { class: "muted small" }, (book.source || "") + (book.access ? " · " + book.access : "")),
        h("p", {}, book.description || text("Consulta la ficha del proveedor para la descripción y las condiciones de acceso.", "See the provider record for the description and access terms.")),
        h("p", { class: "note" }, text("La miniapp guarda una copia de lectura. Los EPUB compatibles pueden conservar títulos, énfasis y algunas imágenes; PDF se extrae a texto. No se eluden préstamos ni DRM. Comprueba los derechos en tu país.", "The app saves a reading copy. Compatible EPUBs can preserve headings, emphasis and some images; PDF is extracted as text. Loans and DRM are not bypassed. Check rights in your country.")),
        url ? h("a", { class: "btn ghost", href: url, target: "_blank", rel: "noopener noreferrer" }, text("Ficha y formatos originales", "Record and original formats")) : null]);
    };
    const addBook = book => {
      if (!book || typeof book.id !== "string" || typeof book.title !== "string" || shown.has(book.id)) return;
      const saved = local.get(book.id);
      if (!saved && !(book.downloads || []).some(item => ["txt", "epub", "pdf"].includes(item.format))) return;
      shown.add(book.id);
      const button = h("button", { class: "btn primary small", onclick: async () => {
        if (local.has(book.id)) return reader(local.get(book.id));
        button.disabled = true;
        try {
          const downloads = book.downloads || [];
          if (!downloads.length) { details(book); return; }
          const chosen = downloads.find(item => item.format === "epub") || downloads.find(item => item.format === "txt") || downloads[0];
          status.textContent = text("Descargando la copia de lectura; puede tardar. Mantén la miniapp abierta hasta que aparezca «Abrir».", "Downloading the reading copy; this may take a while. Keep the miniapp open until “Open” appears.");
          const response = await bridge.call("books", { do: "download", url: chosen.url, format: chosen.format, visual: true });
          if (!response.complete || typeof response.text !== "string" || !response.text) throw new Error("incomplete");
          const stored = normalBook({ ...book, text: response.text, sha256: response.sha256, downloaded: Date.now(),
            source_format: response.source_format, presentation: response.presentation, presentation_warning: response.presentation_warning });
          const savedCopy = await saveNew(stored); local.set(book.id, savedCopy);
          button.textContent = text("Abrir", "Open");
          status.textContent = text("Guardado en esta miniapp para leer sin conexión.", "Saved in this miniapp for offline reading.");
        } catch (error) {
          status.textContent = error.message === "capacity" ? text("Límite de 40 libros en esta instancia. Exporta o elimina una copia antes de añadir otra.", "This instance is limited to 40 books. Export or remove a copy before adding another.") :
            (libraryError(error) ? text("No se pudo guardar. ", "Could not save. ") + libraryError(error) :
              text("No se pudo descargar o guardar. Revisa la conexión, el acceso de la fuente y el espacio disponible; consulta la ficha original.", "Could not download or save. Check the connection, source access and available storage; see the original record."));
        } finally { button.disabled = false; }
      } }, saved ? text("Abrir", "Open") : text("Descargar", "Download"));
      const progress = saved ? Math.round(progressOf(saved).offset * 100 / saved.text.length) : null;
      const card = h("article", { class: "card book-card" }, cover(book, !!saved),
        h("div", { class: "book-card-info" }, h("h3", { class: "book-title" }, book.title),
          h("p", { class: "book-author muted small" }, (book.authors || []).join(", ") || text("Autor no indicado", "Author not listed")),
          h("span", { class: "tag" }, saved ? text("Guardado · ", "Saved · ") + progress + "%" : (book.source || text("Catálogo", "Catalogue"))),
          h("div", { class: "book-actions" }, button, h("button", { class: "btn ghost small", onclick: () => details(book) }, text("Detalles", "Details")))));
      if (active === "saved") add(card.querySelector(".book-actions"), h("button", { class: "btn ghost small", onclick: async () => {
        if (await confirmBox(t("delete"), text("¿Eliminar solo esta copia de la miniapp? Esta acción no borra archivos exportados.", "Delete only this miniapp copy? Exported files are not deleted."), t("delete"))) {
          try { await remove(book.id); local.delete(book.id); shown.delete(book.id); card.remove();
            store.del("book.offset." + book.id); store.del("book.page." + book.id); store.del("book.marks." + book.id);
            status.textContent = text("Copia eliminada de esta miniapp.", "Copy removed from this miniapp.");
          } catch (_) { status.textContent = text("No se pudo eliminar la copia.", "Could not remove the copy."); }
        }
      } }, icon("trash"), t("delete")));
      add(shelf, card);
    };
    function busy(value) {
      pending = value; searchButton.disabled = value; refresh.disabled = value; more.disabled = value;
      status.classList.toggle("book-sync", value);
      if (value) status.setAttribute("aria-busy", "true"); else status.removeAttribute("aria-busy");
      clearTimeout(slowTimer);
      if (value) slowTimer = setTimeout(() => { if (pending && active === "catalogue") status.textContent = text("Seguimos esperando la respuesta del bot. Puede tardar por la conexión o por los catálogos; no vuelvas a enviar la búsqueda aún.", "Still waiting for the bot. Connection or catalogues may be slow; do not resend the search yet."); }, 20000);
    }
    async function search(append = false) {
      const q = query.value.trim();
      if (!q) {
        revision++; busy(false); page = 0; shown = new Set(); shelf.replaceChildren(); more.hidden = true;
        status.textContent = text("Escribe un título, autor o tema para buscar.", "Enter a title, author or topic to search."); return;
      }
      const version = ++revision, requestedPage = append ? page + 1 : 1;
      if (!append) { page = 0; shown = new Set(); shelf.replaceChildren(); }
      more.hidden = true; busy(true);
      status.textContent = text("Consultando catálogos a través del bot…", "Querying catalogues through the bot…");
      const args = { do: "search", source, q, page: requestedPage, language };
      store.set("books.query", q, true);
      try {
        const response = await bridge.call("books", args);
        if (!shelf.isConnected || version !== revision || active !== "catalogue") return;
        if (!response || !Array.isArray(response.books)) throw new Error("invalid_response");
        page = requestedPage; response.books.forEach(addBook); more.hidden = !response.more;
        const count = shown.size, notices = Array.isArray(response.unavailable) ? response.unavailable.filter(item => typeof item === "string").slice(0, 12) : [];
        more.textContent = !count && notices.length ? text("Reintentar fuentes", "Retry sources") : text("Más libros", "More books");
        status.textContent = (count ? text(`${count} resultado${count === 1 ? "" : "s"}.`, `${count} result${count === 1 ? "" : "s"}.`) : text("No hay resultados descargables en esta respuesta.", "No downloadable results in this response.")) +
          (notices.length ? " " + text("Avisos: ", "Notices: ") + notices.join("; ") + "." : "");
      } catch (error) {
        if (!shelf.isConnected || version !== revision || active !== "catalogue") return;
        more.hidden = !append;
        status.textContent = libraryError(error) || text("No se recibió o no se pudo procesar la respuesta del bot. Puede deberse a la conexión, la sesión o el catálogo. Comprueba Estado del bot y vuelve a intentar.",
          "The bot response was not received or could not be processed. It may be the connection, session or catalogue. Check bot status and try again.");
      } finally { if (version === revision) busy(false); }
    }
    query.addEventListener("input", () => {
      // A late reply for the previous words must not appear under new words.
      revision++; busy(false); page = 0; shown = new Set(); shelf.replaceChildren(); more.hidden = true;
      status.textContent = text("Consulta modificada. Pulsa Buscar para consultar las nuevas palabras.", "Query changed. Press Search to request the new words.");
    });
    query.addEventListener("keydown", event => { if (event.key === "Enter") { event.preventDefault(); if (!pending) search(false); } });
    const controls = h("div", { class: "book-search-panel" },
      h("div", { class: "book-search-row" }, query, searchButton, refresh),
      h("div", { class: "book-filters" }, sourcePicker, languagePicker));
    const importInput = h("input", { type: "file", accept: ".txt,.json,text/plain,application/json", multiple: true, class: "book-file-input", "aria-label": text("Elegir archivos para importar", "Choose files to import") });
    const importButton = h("button", { class: "btn ghost small", onclick: () => importInput.click() }, text("Importar TXT o copia", "Import TXT or backup"));
    const backupButton = h("button", { class: "btn ghost small", onclick: async () => {
      if (!local.size) return toast(text("No hay libros para exportar.", "There are no books to export."), "bad");
      try { await exportBackup([...local.values()]); } catch (_) { toast(text("No se pudo preparar la copia.", "Could not prepare the backup."), "bad"); }
    } }, text("Copia de biblioteca", "Library backup"));
    const savedControls = h("div", { class: "book-saved-controls", hidden: true }, savedFilter,
      h("div", { class: "book-saved-actions" }, importButton, backupButton, importInput),
      h("p", { class: "muted small" }, text("La copia incluye libros, marcadores y progreso de esta instancia; no está cifrada. Los TXT de la miniapp anterior se importan sin su progreso original.",
        "The backup includes books, bookmarks and progress from this instance; it is not encrypted. TXT from the previous miniapp imports without its original progress.")));
    const drawSaved = () => {
      shown = new Set(); shelf.replaceChildren();
      const filter = savedFilter.value.trim().toLocaleLowerCase();
      [...local.values()].sort((a, b) => safeNumber(b.downloaded) - safeNumber(a.downloaded)).filter(book =>
        (book.title + " " + (book.authors || []).join(" ")).toLocaleLowerCase().includes(filter)).forEach(addBook);
      status.textContent = local.size ? text(`${shown.size} de ${local.size} libros guardados en esta instancia.`, `${shown.size} of ${local.size} books saved in this instance.`) :
        text("Aún no hay libros guardados. Busca uno o importa un TXT.", "No books saved yet. Search or import a TXT.");
    };
    savedFilter.addEventListener("input", drawSaved);
    importInput.addEventListener("change", async () => {
      const files = [...(importInput.files || [])]; importInput.value = "";
      if (!files.length) return;
      importButton.disabled = true;
      try {
        status.textContent = text("Comprobando archivos…", "Checking files…");
        const records = await parseFiles(files), result = await importBatch(records);
        let progressSaved = true;
        for (const item of result.added) {
          local.set(item.book.id, item.book);
          progressSaved = store.set("book.offset." + item.book.id, item.offset, true) && progressSaved;
          progressSaved = store.set("book.marks." + item.book.id, item.marks, true) && progressSaved;
        }
        if (active === "saved" && shelf.isConnected) drawSaved();
        status.textContent = text(`Importados: ${result.added.length}. Ya existentes sin modificar: ${result.skipped.length}.`, `Imported: ${result.added.length}. Existing books unchanged: ${result.skipped.length}.`) +
          (progressSaved ? "" : text(" Los libros se guardaron, pero no se pudo restaurar todo el progreso.", " Books were saved, but not all progress could be restored."));
      } catch (error) {
        status.textContent = error.message === "capacity" ? text("No se importó nada: se supera el límite de 40 libros.", "Nothing was imported: the 40-book limit would be exceeded.") :
          text("No se importó nada. Usa archivos TXT o una copia válida de esta biblioteca (máximo 40 MB) y comprueba el espacio disponible.", "Nothing was imported. Use TXT files or a valid library backup (40 MB maximum), and check available space.");
      } finally { importButton.disabled = false; }
    });
    const drawTab = value => {
      stopSpeech(); active = value; revision++; busy(false); shelf.replaceChildren(); shown = new Set(); more.hidden = true;
      controls.hidden = value !== "catalogue"; savedControls.hidden = value !== "saved";
      if (value === "saved") drawSaved();
      else { page = 0; status.textContent = text("Busca por título, autor o tema; la respuesta llega a través del bot.", "Search by title, author or topic; the response arrives through the bot."); }
    };
    add(main, h("div", { class: "book-library-intro" }, h("p", { class: "book-eyebrow" }, text("TU RINCÓN DE LECTURA", "YOUR READING SPACE")),
      h("h2", {}, text("Biblioteca", "Library")), h("p", { class: "muted" }, text("Descubre libros públicos y guarda copias para leer sin conexión.", "Discover public books and save copies to read offline."))),
      segmented(active, [["catalogue", text("Explorar", "Explore")], ["saved", text("Mi biblioteca", "My library")]], drawTab), controls, savedControls, status, shelf, more);
    controls.hidden = active !== "catalogue"; savedControls.hidden = active !== "saved";
    try { local = new Map((await all()).filter(book => book && typeof book.id === "string" && typeof book.text === "string").map(book => [book.id, book])); }
    catch (_) { status.textContent = text("El almacenamiento local no está disponible; no se puede confirmar que se guarden libros.", "Local storage is unavailable; books cannot be confirmed saved."); return; }
    if (!shelf.isConnected) return;
    if (active === "saved") drawSaved();
    else if (params.q && params.q.trim()) search();
    else status.textContent = text("Busca por título, autor o tema; la respuesta llega a través del bot.", "Search by title, author or topic; the response arrives through the bot.");
  }
  return { mount, leave, stopSpeech, all, clearCache() { recentCovers.clear(); store.del("books.coverCache"); store.del("books.catalogueCache"); } };
})();
