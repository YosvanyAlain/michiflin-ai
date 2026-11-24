"use strict";
// Original book bytes remain untouched. Engines are lazy, offline package
// assets; EPUB content is sandboxed and PDF actions/scripts are never enabled.
window.BookOriginal = (() => {
  const MAX_FILE = 8000000;
  const labels = (es, en) => lang() === "en" ? en : es;
  const mime = { txt: "text/plain", epub: "application/epub+zip", pdf: "application/pdf" };
  const loads = new Map();
  const bytes = blob => blob.arrayBuffer ? blob.arrayBuffer() : new Promise((resolve, reject) => {
    const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = () => reject(new Error("read")); reader.readAsArrayBuffer(blob);
  });
  const script = path => {
    if (!loads.has(path)) loads.set(path, new Promise((resolve, reject) => {
      const node = document.createElement("script"); node.src = path;
      node.onload = resolve; node.onerror = () => { loads.delete(path); reject(new Error("engine")); }; document.head.append(node);
    }));
    return loads.get(path);
  };
  const limited = (promise, ms) => new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("timeout")), ms);
    Promise.resolve(promise).then(value => { clearTimeout(timer); resolve(value); }, error => { clearTimeout(timer); reject(error); });
  });
  function filename(book) {
    const ext = book.original_format;
    const raw = typeof book.original_name === "string" ? book.original_name.replace(new RegExp("\\." + ext + "$", "i"), "") : book.title;
    return niceName(raw, "libro") + "." + ext;
  }
  function exportOriginal(book) { exportSheet(book.original, filename(book)); }
  async function safeEpub(blob, cancellation) {
    if (typeof Worker === "undefined") throw new Error("engine");
    const buffer = await bytes(blob);
    if (cancellation && cancellation.cancelled) throw new Error("cancelled");
    return new Promise((resolve, reject) => {
      let worker;
      try { worker = new Worker("book-epub-worker.js"); } catch (_) { reject(new Error("engine")); return; }
      let done = false;
      const finish = (error, value) => {
        if (done) return; done = true; clearTimeout(timer); worker.terminate();
        if (cancellation) cancellation.stop = null;
        if (error) reject(error); else resolve(value);
      };
      const timer = setTimeout(() => finish(new Error("timeout")), 30000);
      if (cancellation) cancellation.stop = () => finish(new Error("cancelled"));
      worker.onerror = () => finish(new Error("engine"));
      worker.onmessage = event => {
        const data = event.data;
        if (!data || !data.ok || !(data.buffer instanceof ArrayBuffer)) return finish(new Error((data && data.code) || "structure"));
        finish(null, data.buffer);
      };
      worker.postMessage(buffer, [buffer]);
    });
  }
  async function inspect(blob, format, verifyEpub = true) {
    if (!(blob instanceof Blob) || !mime[format] || blob.size < 1 || blob.size > MAX_FILE) throw new Error("size");
    const head = new Uint8Array(await bytes(blob.slice(0, Math.min(blob.size, 1024))));
    if (format === "pdf" && !new TextDecoder("latin1").decode(head).includes("%PDF-")) throw new Error("format");
    let limited = false;
    if (format === "epub") {
      if (head.length < 4 || head[0] !== 0x50 || head[1] !== 0x4b || head[2] !== 3 || head[3] !== 4) throw new Error("format");
      if (verifyEpub) {
        try { await safeEpub(blob); }
        catch (error) { if (error.message === "protected") limited = true; else throw error; }
      }
    }
    let content = "", encoding = "";
    if (format === "txt") {
      const data = await bytes(blob);
      try { content = new TextDecoder("utf-8", { fatal: true }).decode(data); encoding = "UTF-8"; }
      catch (_) { content = new TextDecoder("windows-1252").decode(data); encoding = "Windows-1252"; }
      if (!content.trim() || /\u0000/.test(content)) throw new Error("format");
    }
    const hash = await FileKitCrypto.hashBlob(blob);
    return { text: content, encoding, sha256: hash, limited };
  }
  function position(book) { return store.get("book.filePosition." + book.id, null); }
  function marks(book) {
    const value = store.get("book.fileMarks." + book.id, []);
    return Array.isArray(value) ? value.filter(mark => mark && typeof mark === "object" && typeof mark.label === "string" &&
      ((book.original_format === "pdf" && Number.isInteger(mark.page) && mark.page > 0) ||
       (book.original_format === "epub" && typeof mark.cfi === "string" && mark.cfi.length <= 1000))).slice(0, 30) : [];
  }
  function fileState(book) { return { filePosition: position(book), fileMarks: marks(book) }; }
  function common(book, kind) {
    const root = h("div", { class: "book-reader book-file-reader", "data-format": kind });
    const message = h("p", { class: "book-file-status muted small", role: "status" }, labels("Abriendo el archivo original…", "Opening the original file…"));
    const tools = h("div", { class: "book-file-tools" });
    const stage = h("div", { class: `book-file-stage book-file-${kind}` });
    const formatNote = kind === "epub" ? labels(
      "EPUB original: vista segura sin scripts ni recursos remotos; algunas funciones o estilos pueden variar.",
      "Original EPUB: safe view without scripts or remote resources; some features or styling may differ.") : labels(
      "PDF original: se muestran sus páginas; acciones, formularios y scripts están desactivados.",
      "Original PDF: pages are shown; actions, forms and scripts are disabled.");
    fill(root, h("header", { class: "book-reader-head" },
      h("button", { class: "btn ghost small", onclick: () => go("books", { tab: "saved" }) }, icon("back"), labels("Mi biblioteca", "My library")),
      h("h2", {}, book.title), h("p", { class: "book-byline" }, (book.authors || []).join(", ")),
      h("p", { class: "book-format-note" }, formatNote),
      h("button", { class: "btn ghost small", onclick: () => exportOriginal(book) }, icon("share"), labels("Exportar archivo original", "Export original file"))),
      message, tools, stage);
    fill($("#view"), root);
    return { root, message, tools, stage };
  }
  function failure(error) {
    switch (error && error.message) {
      case "protected": return labels("El archivo usa contraseña, cifrado o fuentes ofuscadas que el lector no admite. No se eludió ninguna protección; puedes exportar el original.", "The file uses a password, encryption or obfuscated fonts unsupported by the reader. No protection was bypassed; you can export the original.");
      case "size": return labels("El archivo o su contenido supera los límites seguros. Puedes exportar el original.", "The file or its contents exceed the safe limits. You can export the original.");
      case "timeout": return labels("El lector tardó demasiado. Puedes exportar el original y abrirlo con otra aplicación.", "The reader timed out. You can export the original and open it in another app.");
      default: return labels("Esta miniapp no pudo abrir este archivo o su formato no es compatible. Puedes exportar el original.", "This miniapp could not open this file or its format is unsupported. You can export the original.");
    }
  }
  // Restrict all active markup before it is loaded into the book iframe.
  // A second DOMPurify pass and an iframe CSP defend against parser quirks.
  function stripChapter(doc) {
    doc.querySelectorAll("script,iframe,frame,frameset,object,embed,form,input,button,base,meta,video,audio,portal,svg,math").forEach(node => node.remove());
    for (const node of doc.querySelectorAll("*")) {
      for (const attr of [...node.attributes]) {
        const key = attr.name.toLowerCase(), value = attr.value.trim().replace(/[\u0000-\u0020]/g, "");
        if (key.startsWith("on") || ["srcset", "formaction", "srcdoc", "background", "poster", "xml:base"].includes(key) ||
            (["src", "href", "xlink:href"].includes(key) && /^(?:[a-z][a-z0-9+.-]*:|\/\/|\\)/i.test(value))) node.removeAttribute(attr.name);
      }
    }
  }
  function sanitizeOutput(output) {
    const cleaned = DOMPurify.sanitize(output, { WHOLE_DOCUMENT: true, ADD_TAGS: ["link"],
      // EPUB.js substitutes verified local assets with blob: URLs. Explicitly
      // allow only those and passive raster data URLs; the pass below narrows
      // each attribute again and the iframe CSP blocks all network access.
      ALLOWED_URI_REGEXP: /^(?:blob:|data:image\/(?:png|jpeg|gif|webp);base64,|#)/i,
      ADD_URI_SAFE_ATTR: ["rel", "type"],
      FORBID_TAGS: ["script", "iframe", "frame", "frameset", "object", "embed", "form", "input", "button", "base", "meta", "video", "audio", "portal", "svg", "math"],
      FORBID_ATTR: ["srcset", "formaction", "srcdoc", "background", "poster"] });
    const doc = new DOMParser().parseFromString(cleaned, "text/html");
    for (const node of doc.querySelectorAll("[src],[href],[xlink\\:href]")) {
      for (const key of ["src", "href", "xlink:href"]) {
        if (!node.hasAttribute(key)) continue;
        const value = node.getAttribute(key).trim();
        const allowed = key === "src" ? /^(?:blob:|data:image\/(?:png|jpeg|gif|webp);base64,)/i.test(value) :
          (node.tagName.toLowerCase() === "link" ? /^blob:/i.test(value) : /^#/.test(value));
        if (!allowed) node.removeAttribute(key);
      }
    }
    const meta = doc.createElement("meta"); meta.httpEquiv = "Content-Security-Policy";
    meta.content = "default-src 'none'; script-src 'none'; connect-src 'none'; object-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'; img-src blob: data:; font-src blob: data:; style-src 'unsafe-inline' blob:";
    doc.head.prepend(meta);
    return "<!doctype html>" + doc.documentElement.outerHTML;
  }
  function bookmarkBox(book, kind, jump, current) {
    const list = h("div", { class: "book-contents" });
    const draw = () => {
      fill(list, ...(marks(book).length ? marks(book).map(mark => h("div", { class: "book-mark" },
        h("button", { class: "btn ghost small", onclick: () => jump(kind === "pdf" ? mark.page : mark.cfi) }, mark.label),
        h("button", { class: "icon-btn", "aria-label": labels("Eliminar marcador", "Delete bookmark"), onclick: () => {
          store.set("book.fileMarks." + book.id, marks(book).filter(item => kind === "pdf" ? item.page !== mark.page : item.cfi !== mark.cfi)); draw();
        } }, icon("trash")))) : [h("p", { class: "muted small" }, labels("Aún no hay marcadores.", "No bookmarks yet."))]));
    };
    const disclosure = h("details", { class: "book-disclosure" }, h("summary", {}, labels("Marcadores", "Bookmarks")),
      h("button", { class: "btn ghost small", onclick: () => {
        const loc = current(), existing = marks(book);
        if (!loc) return;
        if (existing.length >= 30) return toast(labels("Límite de 30 marcadores.", "Limit of 30 bookmarks."), "bad");
        if (!existing.some(item => kind === "pdf" ? item.page === loc.page : item.cfi === loc.cfi)) existing.push(loc);
        if (!store.set("book.fileMarks." + book.id, existing, true)) toast(labels("No se pudo guardar el marcador local.", "Could not save the local bookmark."), "bad");
        draw();
      } }, labels("Marcar lugar", "Bookmark place")), list);
    draw(); return disclosure;
  }
  function open(book) {
    const kind = book.original_format, ui = common(book, kind), cancel = { cancelled: false, stop: null };
    let destroy = () => {};
    const alive = () => !cancel.cancelled && ui.root.isConnected;
    const run = async () => {
      if (kind === "epub") {
        const safe = await safeEpub(book.original, cancel);
        await script("vendor/books/jszip.min.js"); await script("vendor/books/epub.min.js"); await script("vendor/books/purify.min.js");
        if (!alive()) return;
        if (typeof window.ePub !== "function" || !window.DOMPurify) throw new Error("engine");
        const epub = window.ePub(safe, { replacements: "blobUrl" });
        destroy = () => { try { epub.destroy(); } catch (_) {} };
        await limited(epub.opened, 20000);
        try { await limited(epub.loaded.navigation, 5000); } catch (_) { /* Contents may be absent. */ }
        if (!alive()) return;
        epub.spine.hooks.content.register(stripChapter);
        // EPUB.js catches hook exceptions: explicitly replace, never leave its
        // unsanitized output in place when a sanitizer or parser fails.
        epub.spine.hooks.serialize.register((_output, section) => {
          try { section.output = sanitizeOutput(section.output); }
          catch (_) { section.output = "<!doctype html><html><head><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'\"></head><body>Contenido no compatible.</body></html>"; }
        });
        const rendition = epub.renderTo(ui.stage, { width: "100%", height: "100%", flow: "paginated", allowScriptedContent: false, allowPopups: false });
        let loc = null;
        const pageLabel = h("span", { class: "book-file-page", "aria-live": "polite" });
        const prev = h("button", { class: "btn ghost", onclick: () => rendition.prev().catch(() => {}) }, icon("back"), labels("Anterior", "Previous"));
        const next = h("button", { class: "btn ghost", onclick: () => rendition.next().catch(() => {}) }, labels("Siguiente", "Next"), icon("next"));
        rendition.on("relocated", location => {
          if (!alive() || !location || !location.start) return;
          loc = location.start.cfi || null;
          if (loc && !store.set("book.filePosition." + book.id, loc, true)) ui.message.textContent = labels("No se pudo guardar la posición local.", "Could not save the local position.");
          const part = Number(location.start.index || 0) + 1, total = epub.spine.spineItems.length;
          const displayed = location.start.displayed || {};
          pageLabel.textContent = labels(`Sección ${part} / ${total} · página ${displayed.page || 1} / ${displayed.total || 1}`,
            `Section ${part} / ${total} · page ${displayed.page || 1} / ${displayed.total || 1}`);
          prev.disabled = !!location.atStart; next.disabled = !!location.atEnd;
        });
        const nav = h("div", { class: "book-file-nav" }, prev, pageLabel, next);
        const size = h("input", { type: "range", min: "75", max: "175", step: "5", value: String(store.get("book.epubFont", 100)),
          "aria-label": labels("Tamaño del texto EPUB", "EPUB text size"), oninput: event => {
            const value = Math.max(75, Math.min(175, Number(event.target.value) || 100));
            rendition.themes.fontSize(value + "%"); store.set("book.epubFont", value, true);
          } });
        const themes = select(store.get("book.epubTheme", "original"), [["original", labels("Original", "Original")], ["paper", labels("Claro", "Light")], ["sepia", "Sepia"], ["night", labels("Noche", "Night")]], value => {
          store.set("book.epubTheme", value, true); rendition.themes.select(value);
        });
        themes.setAttribute("aria-label", labels("Tema EPUB", "EPUB theme"));
        rendition.themes.register("original", {});
        rendition.themes.register("paper", { body: { background: "#ffffff !important", color: "#1b2421 !important" } });
        rendition.themes.register("sepia", { body: { background: "#f3e9d5 !important", color: "#3d2c1e !important" } });
        rendition.themes.register("night", { body: { background: "#151c1b !important", color: "#e9f2ee !important" } });
        rendition.themes.select(store.get("book.epubTheme", "original")); rendition.themes.fontSize(size.value + "%");
        const contents = [];
        const gather = (rows, depth = 0) => {
          if (!Array.isArray(rows) || depth > 4) return;
          for (const item of rows) {
            if (item && typeof item.href === "string" && item.href.length < 1000 && typeof item.label === "string" && contents.length < 150)
              contents.push(h("button", { class: "btn ghost small", onclick: () => rendition.display(item.href).catch(() => {}) }, item.label.trim().slice(0, 150)));
            if (item) gather(item.subitems, depth + 1);
          }
        };
        gather(epub.navigation && epub.navigation.toc);
        const outline = h("details", { class: "book-disclosure" }, h("summary", {}, labels("Índice", "Contents")),
          h("div", { class: "book-contents" }, ...(contents.length ? contents : [h("p", { class: "muted small" }, labels("Sin índice disponible.", "No contents available."))])));
        const results = h("div", { class: "book-contents" });
        const query = field({ type: "search", maxlength: "120", "aria-label": labels("Buscar dentro del EPUB", "Find in EPUB") });
        const find = h("button", { class: "btn ghost small", onclick: async () => {
          const term = query.value.trim(); if (!term) return; find.disabled = true; results.replaceChildren();
          try {
            let hits = 0;
            for (const section of epub.spine.spineItems.slice(0, 300)) {
              if (!alive() || hits >= 30) break;
              await section.load(epub.load.bind(epub));
              const found = section.find(term).slice(0, 30 - hits); hits += found.length;
              found.forEach(hit => add(results, h("button", { class: "btn ghost small", onclick: () => rendition.display(hit.cfi).catch(() => {}) }, String(hit.excerpt || "").slice(0, 160))));
            }
            if (!hits && alive()) add(results, h("p", { class: "muted small" }, labels("No se encontró el texto en las secciones comprobadas.", "Text not found in the checked sections.")));
          } catch (_) { if (alive()) add(results, h("p", { class: "muted small" }, labels("No se pudo buscar en este EPUB.", "Could not search this EPUB."))); }
          finally { find.disabled = false; }
        } }, labels("Buscar", "Find"));
        const search = h("details", { class: "book-disclosure" }, h("summary", {}, labels("Buscar en el libro", "Find in book")),
          h("div", { class: "book-find" }, query, find), results);
        const savedMarks = bookmarkBox(book, "epub", cfi => rendition.display(cfi).catch(() => {}), () => loc ?
          { cfi: loc, label: pageLabel.textContent || labels("Marcador", "Bookmark") } : null);
        const settings = h("details", { class: "book-disclosure" }, h("summary", {}, labels("Ajustes de lectura", "Reading settings")),
          h("div", { class: "book-settings-grid" }, h("label", { class: "book-setting" }, labels("Tamaño de letra", "Font size"), size),
            h("label", { class: "book-setting" }, labels("Fondo", "Background"), themes)));
        fill(ui.tools, nav, h("details", { class: "book-toolbox" }, h("summary", {}, labels("Herramientas de lectura", "Reading tools")), settings, outline, search, savedMarks));
        const saved = position(book);
        try { await limited(rendition.display(typeof saved === "string" && saved.length < 1000 ? saved : undefined), 20000); }
        catch (_) { await limited(rendition.display(), 20000); }
        if (alive()) ui.message.textContent = labels("Lectura sin conexión. El original se conserva sin modificar.", "Offline reading. The original is kept unchanged.");
      } else if (kind === "pdf") {
        const pdfjs = await import("./vendor/books/pdf.min.mjs");
        if (!alive()) return;
        pdfjs.GlobalWorkerOptions.workerSrc = new URL("vendor/books/pdf.worker.min.mjs", window.location.href).href;
        const data = new Uint8Array(await bytes(book.original));
        const task = pdfjs.getDocument({ data, isEvalSupported: false, enableXfa: false, useSystemFonts: true, useWorkerFetch: false,
          maxImageSize: 16000000, stopAtErrors: true });
        let protectedFile = false;
        task.onPassword = () => { protectedFile = true; task.destroy(); };
        destroy = () => { try { task.destroy(); } catch (_) {} };
        let pdf;
        try { pdf = await limited(task.promise, 20000); } catch (error) { throw protectedFile ? new Error("protected") : error; }
        if (!alive()) return;
        if (!pdf.numPages || pdf.numPages > 600) throw new Error("size");
        let current = Number(position(book)) || 1, zoom = 1, renderTask = null, revision = 0, resizeTimer = null;
        current = Math.max(1, Math.min(pdf.numPages, Math.floor(current)));
        const canvas = h("canvas", { class: "book-pdf-canvas" });
        const wrap = h("div", { class: "book-pdf-scroll", tabindex: "0", "aria-label": labels("Página PDF", "PDF page") }, canvas);
        fill(ui.stage, wrap);
        const pageLabel = h("span", { class: "book-file-page", "aria-live": "polite" });
        const pageInput = h("input", { type: "number", min: "1", max: String(pdf.numPages), value: String(current),
          "aria-label": labels("Ir a página", "Go to page"), onchange: event => draw(Number(event.target.value)) });
        const prev = h("button", { class: "btn ghost", onclick: () => draw(current - 1) }, icon("back"), labels("Anterior", "Previous"));
        const next = h("button", { class: "btn ghost", onclick: () => draw(current + 1) }, labels("Siguiente", "Next"), icon("next"));
        async function draw(number) {
          current = Math.max(1, Math.min(pdf.numPages, Math.floor(number) || 1));
          const thisRevision = ++revision;
          if (renderTask) { try { renderTask.cancel(); } catch (_) {} renderTask = null; }
          pageLabel.textContent = labels(`Página ${current} / ${pdf.numPages}`, `Page ${current} / ${pdf.numPages}`);
          pageInput.value = String(current); prev.disabled = current === 1; next.disabled = current === pdf.numPages;
          if (!store.set("book.filePosition." + book.id, current, true)) ui.message.textContent = labels("No se pudo guardar la posición local.", "Could not save the local position.");
          try {
            const page = await pdf.getPage(current); if (!alive() || thisRevision !== revision) return;
            const native = page.getViewport({ scale: 1 }), width = Math.max(100, wrap.clientWidth - 4);
            const scale = Math.min(4, Math.max(.1, width / native.width * zoom));
            const viewport = page.getViewport({ scale });
            const pixels = Math.min(window.devicePixelRatio || 1, 2, Math.sqrt(12000000 / Math.max(1, viewport.width * viewport.height)));
            if (viewport.width > 12000 || viewport.height > 12000 || pixels < .25) throw new Error("size");
            canvas.width = Math.max(1, Math.ceil(viewport.width * pixels)); canvas.height = Math.max(1, Math.ceil(viewport.height * pixels));
            canvas.style.width = Math.ceil(viewport.width) + "px"; canvas.style.height = Math.ceil(viewport.height) + "px";
            canvas.setAttribute("aria-label", labels(`Página ${current} de ${pdf.numPages}`, `Page ${current} of ${pdf.numPages}`));
            renderTask = page.render({ canvasContext: canvas.getContext("2d"), viewport, transform: [pixels, 0, 0, pixels, 0, 0], annotationMode: pdfjs.AnnotationMode.DISABLE });
            await renderTask.promise;
            if (alive() && thisRevision === revision) { renderTask = null; ui.message.textContent = labels("Lectura sin conexión. El original se conserva sin modificar.", "Offline reading. The original is kept unchanged."); }
          } catch (error) {
            if (alive() && thisRevision === revision && error.name !== "RenderingCancelledException") ui.message.textContent = failure(error);
          }
        }
        const zoomControl = h("input", { type: "range", min: "100", max: "200", step: "10", value: "100",
          "aria-label": labels("Zoom del PDF", "PDF zoom"), oninput: event => { zoom = Number(event.target.value) / 100; draw(current); } });
        const query = field({ type: "search", maxlength: "120", "aria-label": labels("Buscar dentro del PDF", "Find in PDF") });
        const find = h("button", { class: "btn ghost small", onclick: async () => {
          const term = query.value.trim().toLocaleLowerCase(); if (!term) return; find.disabled = true;
          let found = false;
          try {
            for (let i = 1; i <= pdf.numPages; i++) {
              if (!alive()) break;
              const number = (current + i - 1) % pdf.numPages + 1, page = await pdf.getPage(number);
              const text = await page.getTextContent();
              if (text.items.map(item => item.str || "").join(" ").toLocaleLowerCase().includes(term)) {
                found = true; await draw(number); ui.message.textContent = labels(`Texto encontrado en la página ${number}.`, `Text found on page ${number}.`); break;
              }
            }
            if (!found && alive()) ui.message.textContent = labels("No se encontró texto extraíble. Los escaneos pueden no admitir búsqueda.", "No extractable text found. Scanned pages may not support search.");
          } catch (_) { if (alive()) ui.message.textContent = labels("No se pudo buscar en este PDF.", "Could not search this PDF."); }
          finally { find.disabled = false; }
        } }, labels("Buscar", "Find"));
        const nav = h("div", { class: "book-file-nav" }, prev, pageLabel, next);
        const settings = h("details", { class: "book-disclosure" }, h("summary", {}, labels("Página y zoom", "Page and zoom")),
          h("div", { class: "book-settings-grid" }, h("label", { class: "book-setting" }, labels("Página", "Page"), pageInput),
            h("label", { class: "book-setting" }, labels("Zoom", "Zoom"), zoomControl)));
        const search = h("details", { class: "book-disclosure" }, h("summary", {}, labels("Buscar en el libro", "Find in book")), h("div", { class: "book-find" }, query, find));
        const savedMarks = bookmarkBox(book, "pdf", draw, () => ({ page: current, label: labels(`Página ${current}`, `Page ${current}`) }));
        fill(ui.tools, nav, h("details", { class: "book-toolbox" }, h("summary", {}, labels("Herramientas de lectura", "Reading tools")), settings, search, savedMarks));
        const resized = () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { if (alive()) draw(current); }, 250); };
        window.addEventListener("resize", resized);
        const priorDestroy = destroy;
        destroy = () => { revision++; clearTimeout(resizeTimer); window.removeEventListener("resize", resized); if (renderTask) renderTask.cancel(); priorDestroy(); };
        await draw(current);
      } else throw new Error("format");
    };
    run().catch(error => { if (alive() && error.message !== "cancelled") ui.message.textContent = failure(error); });
    return () => { cancel.cancelled = true; if (cancel.stop) cancel.stop(); destroy(); };
  }
  return { MAX_FILE, mime, bytes, filename, exportOriginal, inspect, safeEpub, position, marks, fileState, open, failure };
})();
