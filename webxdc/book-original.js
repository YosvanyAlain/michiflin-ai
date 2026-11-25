"use strict";
// A short tap at an edge turns a page without intercepting scrolls, links,
// selections or long presses. EPUB gutters live outside the sandboxed frame;
// the helper never injects or enables a script in a book.
window.BookPageGestures = (() => {
  let lastTouchAt = -Infinity;
  const interactive = node => {
    const element = node && node.nodeType === 1 ? node : node && node.parentElement;
    return !!(element && (element.isContentEditable || element.closest("a,button,input,select,textarea,summary,label,[contenteditable]:not([contenteditable='false']),[role~='button'],[role~='link'],[role~='slider'],[role~='checkbox'],[role~='switch'],[role~='tab'],[role~='menuitem'],[role~='textbox']")));
  };
  function bind(node, bounds, turn, extraSelection = null) {
    const doc = node.nodeType === 9 ? node : node.ownerDocument;
    let active = null;
    const selected = () => {
      try {
        const value = doc.getSelection();
        return !!((value && !value.isCollapsed && value.toString()) || (extraSelection && extraSelection()));
      } catch (_) { return true; } // do not turn a page when selection is unknown
    };
    const edge = (x, y) => {
      const result = bounds(), boxes = Array.isArray(result) ? result : [result];
      for (const box of boxes) {
        if (!box || ![box.left, box.right, box.top, box.bottom, x, y].every(Number.isFinite) ||
            x < box.left || x > box.right || y < box.top || y > box.bottom) continue;
        const width = box.right - box.left, margin = Math.min(64, Math.max(28, width * .16));
        if (width < 100) continue;
        if (x <= box.left + margin) return -1;
        if (x >= box.right - margin) return 1;
      }
      return 0;
    };
    const begin = (id, x, y, target) => {
      const direction = edge(x, y);
      active = direction && !interactive(target) && !selected() ? { id, x, y, direction, at: performance.now(), moved: false } : null;
    };
    const move = (id, x, y) => {
      if (active && active.id === id && Math.hypot(x - active.x, y - active.y) > 12) active.moved = true;
    };
    const finish = (id, x, y, target) => {
      const start = active; active = null;
      if (!start || start.id !== id || start.moved || performance.now() - start.at > 600 ||
          Math.hypot(x - start.x, y - start.y) > 12 || edge(x, y) !== start.direction || interactive(target) || selected()) return;
      turn(start.direction);
    };
    const down = event => {
      if (event.isPrimary === false || event.button !== 0) { active = null; return; }
      begin(event.pointerId, event.clientX, event.clientY, event.target);
    };
    const motion = event => move(event.pointerId, event.clientX, event.clientY);
    const up = event => finish(event.pointerId, event.clientX, event.clientY, event.target);
    const cancel = () => { active = null; };
    const touchStart = event => {
      lastTouchAt = performance.now();
      if (event.touches.length !== 1) { cancel(); return; }
      const point = event.touches[0]; begin(point.identifier, point.clientX, point.clientY, event.target);
    };
    const touchMove = event => {
      if (event.touches.length !== 1) { cancel(); return; }
      const point = event.touches[0]; move(point.identifier, point.clientX, point.clientY);
    };
    const touchEnd = event => {
      if (event.touches.length || event.changedTouches.length !== 1) { cancel(); return; }
      const point = event.changedTouches[0]; finish(point.identifier, point.clientX, point.clientY, event.target);
    };
    const mouseDown = event => {
      if (performance.now() - lastTouchAt < 800 || event.button !== 0) { cancel(); return; }
      begin("mouse", event.clientX, event.clientY, event.target);
    };
    const mouseMove = event => move("mouse", event.clientX, event.clientY);
    const mouseUp = event => finish("mouse", event.clientX, event.clientY, event.target);
    const horizontallyScrollable = target => {
      let element = target && target.nodeType === 1 ? target : target && target.parentElement;
      while (element) {
        const style = doc.defaultView.getComputedStyle(element);
        if (element.scrollWidth > element.clientWidth + 1 && /^(auto|scroll)$/.test(style.overflowX)) return true;
        if (element === node) break;
        element = element.parentElement;
      }
      return false;
    };
    const keydown = event => {
      if (event.defaultPrevented || event.isComposing || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey ||
          interactive(event.target) || selected() || horizontallyScrollable(event.target) || !["ArrowLeft", "ArrowRight"].includes(event.key)) return;
      event.preventDefault(); turn(event.key === "ArrowLeft" ? -1 : 1);
    };
    const pointer = typeof window.PointerEvent === "function";
    const bindings = pointer ? [["pointerdown", down], ["pointermove", motion], ["pointerup", up], ["pointercancel", cancel]] :
      [["touchstart", touchStart], ["touchmove", touchMove], ["touchend", touchEnd], ["touchcancel", cancel],
        ["mousedown", mouseDown], ["mousemove", mouseMove], ["mouseup", mouseUp]];
    for (const [name, fn] of bindings) node.addEventListener(name, fn, { capture: true, passive: true });
    node.addEventListener("keydown", keydown, true);
    return () => {
      active = null;
      for (const [name, fn] of bindings) node.removeEventListener(name, fn, true);
      node.removeEventListener("keydown", keydown, true);
    };
  }
  return { bind };
})();

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
    const area = h("div", { class: "book-file-area", tabindex: "0", role: "region",
      "aria-label": labels("Área de lectura. Usa las flechas izquierda y derecha para cambiar de página.", "Reading area. Use the left and right arrows to turn pages.") },
      h("span", { class: "book-edge-side", "aria-hidden": "true" }, icon("back")), stage,
      h("span", { class: "book-edge-side", "aria-hidden": "true" }, icon("next")));
    const navigation = h("div", { class: "book-file-bottom-nav" });
    const hint = h("p", { class: "book-edge-hint", hidden: true }, labels(
      "Toca los márgenes laterales para pasar. Teclado: enfoca el lector y usa ← / →.", "Tap the side margins to turn pages. Keyboard: focus the reader and use ← / →."));
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
      message, tools, hint, area, navigation);
    fill($("#view"), root);
    return { root, message, tools, hint, area, stage, navigation };
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
  function stripChapter(doc, chapterLink) {
    doc.querySelectorAll("script,iframe,frame,frameset,object,embed,form,input,button,base,meta,video,audio,portal,svg,math").forEach(node => node.remove());
    // Resolve a chapter's relative links against the known EPUB spine before
    // DOMPurify strips relative URLs. External and unknown links become text.
    for (const anchor of doc.querySelectorAll("a[href]")) {
      const token = chapterLink(anchor.getAttribute("href"));
      if (token) anchor.setAttribute("href", "#" + token); else anchor.removeAttribute("href");
      for (const key of ["target", "download", "ping"]) anchor.removeAttribute(key);
    }
    for (const node of doc.querySelectorAll("*")) {
      for (const attr of [...node.attributes]) {
        const key = attr.name.toLowerCase(), value = attr.value.trim().replace(/[\u0000-\u0020]/g, "");
        if (key.startsWith("on") || ["srcset", "formaction", "srcdoc", "background", "poster", "xml:base"].includes(key) ||
            (["src", "href", "xlink:href"].includes(key) && /^(?:[a-z][a-z0-9+.-]*:|\/\/|\\)/i.test(value))) node.removeAttribute(attr.name);
      }
    }
  }
  function sanitizeOutput(output, chapterLinks) {
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
        const value = node.getAttribute(key).trim(), tag = node.tagName.toLowerCase();
        if (key === "href" && tag === "a" && value.startsWith("#") && chapterLinks.has(value.slice(1))) {
          // srcdoc inherits the parent's base URL: a bare #fragment can reload
          // the app inside the iframe. An explicit same-document URL stays in
          // the sandbox and signals the host without executing book scripts.
          node.setAttribute(key, "about:srcdoc" + value);
          for (const attr of ["target", "download", "ping"]) node.removeAttribute(attr);
          continue;
        }
        const allowed = key === "src" ? /^(?:blob:|data:image\/(?:png|jpeg|gif|webp);base64,)/i.test(value) :
          (key === "href" && tag === "link" && /^blob:/i.test(value));
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
    if (kind === "epub") ui.root.setAttribute("aria-busy", "true");
    let destroy = () => {};
    const alive = () => !cancel.cancelled && ui.root.isConnected;
    const main = $("#view");
    // Only the vertical region occupied by the file can turn pages. Extend
    // horizontally to the screen margins without placing an overlay over text.
    const stageBounds = () => {
      const view = main.getBoundingClientRect(), stage = ui.stage.getBoundingClientRect(), area = ui.area.getBoundingClientRect();
      const top = Math.max(view.top, stage.top), bottom = Math.min(view.bottom, stage.bottom);
      return [{ left: view.left, right: view.right, top, bottom }, { left: area.left, right: area.right, top, bottom }];
    };
    const showStageTop = () => {
      const view = main.getBoundingClientRect(), stage = ui.stage.getBoundingClientRect();
      if (stage.top < view.top || stage.bottom > view.bottom) main.scrollTop += stage.top - view.top - 8;
    };
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
        // The host accepts only destinations already present in this EPUB's
        // spine; it never opens a URL supplied by a book or makes a request.
        const chapterLinks = new Map(), tokens = new Map(), spinePaths = new Map();
        const base = "https://epub.invalid/", namespace = "michiflin-" + Math.random().toString(36).slice(2) + "-";
        const supportsSrcdoc = "srcdoc" in document.createElement("iframe");
        for (const item of epub.spine.spineItems) {
          try {
            if (typeof item.href !== "string" || item.href.length > 1000 || /[\\\u0000-\u001f\u007f]/.test(item.href) ||
                /^(?:[a-z][a-z0-9+.-]*:|\/\/)/i.test(item.href.trim())) continue;
            const path = new URL(item.href, base);
            if (path.origin === "https://epub.invalid" && !path.search && !path.hash)
              spinePaths.set(decodeURIComponent(path.pathname), item);
          } catch (_) { /* Invalid manifest targets are not navigable. */ }
        }
        const chapterLink = (section, href) => {
          if (!supportsSrcdoc || typeof href !== "string" || !href || href.length > 1000 ||
              /[\\\u0000-\u001f\u007f]/.test(href) || /^(?:[a-z][a-z0-9+.-]*:|\/\/)/i.test(href.trim()) || href.split("#")[0].includes("?")) return "";
          try {
            const current = new URL(section.href, base), destination = new URL(href, current);
            if (current.origin !== "https://epub.invalid" || destination.origin !== current.origin || destination.search) return "";
            const item = spinePaths.get(decodeURIComponent(destination.pathname));
            const fragment = destination.hash ? decodeURIComponent(destination.hash.slice(1)) : "";
            if (!item || fragment.length > 500 || /[\u0000-\u001f\u007f]/.test(fragment)) return "";
            const target = item.href + (fragment ? "#" + fragment : "");
            if (tokens.has(target)) return tokens.get(target);
            if (chapterLinks.size >= 4096) return ""; // bounded even for hostile books
            const token = namespace + chapterLinks.size;
            chapterLinks.set(token, target); tokens.set(target, token); return token;
          } catch (_) { return ""; }
        };
        epub.spine.hooks.content.register((doc, section) => stripChapter(doc, href => chapterLink(section, href)));
        // EPUB.js catches hook exceptions: explicitly replace, never leave its
        // unsanitized output in place when a sanitizer or parser fails.
        epub.spine.hooks.serialize.register((_output, section) => {
          try { section.output = sanitizeOutput(section.output, chapterLinks); }
          catch (_) { section.output = "<!doctype html><html><head><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'\"></head><body>Contenido no compatible.</body></html>"; }
        });
        const rendition = epub.renderTo(ui.stage, { width: "100%", height: "100%", flow: "paginated", allowScriptedContent: false, allowPopups: false });
        // EPUB.js assigns an onclick handler which resolves #fragments as
        // standalone sections. Remove it; navigation stays native and passive
        // inside the no-scripts sandbox, including keyboard activation.
        const linkStates = new WeakMap();
        rendition.hooks.content.register(contents => {
          for (const anchor of contents.document.querySelectorAll('a[href^="about:srcdoc#"]')) anchor.onclick = null;
          const frame = contents.document.defaultView.frameElement;
          if (frame && contents.document.URL === "about:srcdoc") linkStates.set(frame, { ready: true, last: "", pending: null });
        });
        let loc = null, opening = true, displayResolved = false, pendingTurn = 0, progressSaveFailed = false;
        let resolveLocation;
        const firstLocation = new Promise(resolve => { resolveLocation = resolve; });
        const visibleLocation = location => {
          const reported = location.start.cfi;
          try {
            const content = rendition.getContents().find(item => item.sectionIndex === location.start.index);
            if (!content || !reported) return reported;
            const doc = content.document, frame = doc.defaultView.frameElement;
            if (!frame) return reported;
            const stage = ui.stage.getBoundingClientRect(), bounds = frame.getBoundingClientRect();
            const visible = rect => rect && rect.height > 0 && bounds.left + rect.left <= stage.right + 1 &&
              bounds.left + rect.right >= stage.left - 1 && bounds.top + rect.top <= stage.bottom + 1 &&
              bounds.top + rect.bottom >= stage.top - 1;
            const range = content.range(reported);
            if (range && visible(range.getBoundingClientRect())) return reported;
            // In some WebKit column layouts EPUB.js can report a CFI on the
            // previous page. Derive a local text position only when the
            // reported range is visibly outside this stage; leave image-only
            // pages and unsupported caret APIs on the engine's original CFI.
            const style = doc.defaultView.getComputedStyle(doc.body);
            if (style.direction !== "ltr" || !style.writingMode.startsWith("horizontal")) return reported;
            for (const y of [12, 30, 60, 100]) for (const x of [20, 45, 80]) {
              const px = stage.left - bounds.left + Math.min(x, stage.width / 2);
              const py = stage.top - bounds.top + Math.min(y, stage.height / 2);
              let candidate = doc.caretRangeFromPoint && doc.caretRangeFromPoint(px, py);
              if (!candidate && doc.caretPositionFromPoint) {
                const position = doc.caretPositionFromPoint(px, py);
                if (position) { candidate = doc.createRange(); candidate.setStart(position.offsetNode, position.offset); candidate.collapse(true); }
              }
              if (candidate && candidate.startContainer.nodeType === 3 && candidate.startContainer.textContent.trim() &&
                  visible(candidate.getBoundingClientRect())) return content.cfiFromRange(candidate) || reported;
            }
          } catch (_) { /* The book remains readable with the engine's CFI. */ }
          return reported;
        };
        const pageLabel = h("span", { class: "book-file-page", "aria-live": "polite" });
        const prev = h("button", { class: "btn ghost", onclick: () => turn(-1) }, icon("back"), labels("Anterior", "Previous"));
        const next = h("button", { class: "btn ghost", onclick: () => turn(1) }, labels("Siguiente", "Next"), icon("next"));
        const turn = direction => {
          if (!alive()) return;
          // The first relocated event may arrive after display() resolves.
          // Preserve one early tap instead of silently discarding it.
          if (opening) { if (!pendingTurn) pendingTurn = direction; return; }
          if (!loc || (direction < 0 ? prev.disabled : next.disabled)) return;
          showStageTop();
          (direction < 0 ? rendition.prev() : rendition.next()).catch(() => {});
        };
        // WebKit does not dispatch script callbacks inside an iframe whose
        // sandbox disallows scripts. Use the margins outside the frame instead
        // of weakening the sandbox or laying an invisible layer over the book.
        const frameSelected = () => [...ui.stage.querySelectorAll("iframe")].some(frame => {
          const selection = frame.contentWindow.getSelection();
          return !!(selection && !selection.isCollapsed && selection.toString());
        });
        const unbindOuter = BookPageGestures.bind(main, stageBounds, turn, frameSelected);
        // WebKit suppresses even host-installed event callbacks in a frame
        // without allow-scripts. Observe only the passive same-document hash
        // and dispatch the prevalidated target from the parent. No overlay is
        // placed over the text, so selection and native link focus still work.
        const jump = target => {
          showStageTop();
          rendition.display(target).catch(() => {
            if (alive()) ui.message.textContent = labels("No se pudo abrir este destino del libro.", "Could not open this book destination.");
          });
        };
        const linkTimer = setInterval(() => {
          if (!alive()) { clearInterval(linkTimer); return; }
          for (const frame of ui.stage.querySelectorAll("iframe")) {
            try {
              const win = frame.contentWindow, doc = frame.contentDocument, href = win.location.href;
              let state = linkStates.get(frame);
              if (!state) { state = { ready: false, last: "", pending: null }; linkStates.set(frame, state); }
              if (state.pending) {
                const pending = state.pending;
                if (doc !== pending.doc) { state.pending = null; state.ready = false; continue; }
                if (href === "about:srcdoc") {
                  state.pending = null; state.ready = true; state.last = ""; jump(pending.target);
                } else if (performance.now() >= pending.deadline) {
                  state.pending = null;
                  if (href === "about:srcdoc#" + pending.token) {
                    // Fall back without leaving the sandbox if this engine
                    // cannot traverse a same-document history entry.
                    try { win.history.replaceState(null, "", "about:srcdoc"); } catch (_) {}
                    state.ready = win.location.href === "about:srcdoc";
                    jump(pending.target);
                  }
                }
                continue;
              }
              if (href === "about:srcdoc") { state.ready = true; state.last = ""; continue; }
              if (!href.startsWith("about:srcdoc#")) { state.ready = false; continue; }
              const token = href.slice("about:srcdoc#".length), target = chapterLinks.get(token);
              if (!target || state.last === token) continue;
              state.last = token;
              if (state.ready) {
                // Undo only the just-observed fragment entry before opening
                // the target. Repeated taps then reuse one forward slot instead
                // of filling the host's Back history with inert entries.
                state.ready = false;
                state.pending = { doc, token, target, deadline: performance.now() + 2000 };
                try { win.history.back(); } catch (_) { state.pending.deadline = 0; }
              } else {
                try { win.history.replaceState(null, "", "about:srcdoc"); } catch (_) {}
                state.ready = win.location.href === "about:srcdoc";
                jump(target);
              }
            } catch (_) { /* An inaccessible or removed frame is never trusted. */ }
          }
        }, 100);
        const priorDestroy = destroy;
        destroy = () => { clearInterval(linkTimer); unbindOuter(); resolveLocation(); priorDestroy(); };
        rendition.on("relocated", location => {
          if (!alive() || !location || !location.start) return;
          loc = visibleLocation(location) || null;
          if (loc && displayResolved) {
            progressSaveFailed = !store.set("book.filePosition." + book.id, loc, true);
            if (progressSaveFailed) ui.message.textContent = labels("No se pudo guardar la posición local.", "Could not save the local position.");
          }
          const part = Number(location.start.index || 0) + 1, total = epub.spine.spineItems.length;
          const displayed = location.start.displayed || {};
          pageLabel.textContent = labels(`Sección ${part} / ${total} · página ${displayed.page || 1} / ${displayed.total || 1}`,
            `Section ${part} / ${total} · page ${displayed.page || 1} / ${displayed.total || 1}`);
          prev.disabled = !!location.atStart; next.disabled = !!location.atEnd;
          if (displayResolved && loc) resolveLocation();
        });
        const nav = h("nav", { class: "book-file-nav", "aria-label": labels("Navegación de lectura", "Reading navigation") }, prev, pageLabel, next);
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
        fill(ui.navigation, nav);
        fill(ui.tools, h("details", { class: "book-toolbox" }, h("summary", {}, labels("Herramientas de lectura", "Reading tools")), settings, outline, search, savedMarks));
        const saved = position(book);
        try { await limited(rendition.display(typeof saved === "string" && saved.length < 1000 ? saved : undefined), 20000); }
        catch (_) { await limited(rendition.display(), 20000); }
        if (!alive()) return;
        displayResolved = true;
        // A location emitted before display() completed could describe the
        // previous layout. Ask for a fresh one in that unusual ordering.
        if (loc) rendition.reportLocation().catch(() => {});
        await limited(firstLocation, 20000);
        if (alive()) {
          opening = false; ui.root.setAttribute("aria-busy", "false"); ui.hint.hidden = false;
          if (!progressSaveFailed) ui.message.textContent = labels("Lectura sin conexión. El original se conserva sin modificar.", "Offline reading. The original is kept unchanged.");
          if (pendingTurn) { const direction = pendingTurn; pendingTurn = 0; turn(direction); }
        }
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
        const unbindPages = BookPageGestures.bind(main, stageBounds, direction => {
          if (alive() && (direction < 0 ? current > 1 : current < pdf.numPages)) draw(current + direction);
        });
        async function draw(number) {
          const nextPage = Math.max(1, Math.min(pdf.numPages, Math.floor(number) || 1));
          if (nextPage !== current) { wrap.scrollTop = 0; showStageTop(); }
          current = nextPage;
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
        const nav = h("nav", { class: "book-file-nav", "aria-label": labels("Navegación de lectura", "Reading navigation") }, prev, pageLabel, next);
        const settings = h("details", { class: "book-disclosure" }, h("summary", {}, labels("Página y zoom", "Page and zoom")),
          h("div", { class: "book-settings-grid" }, h("label", { class: "book-setting" }, labels("Página", "Page"), pageInput),
            h("label", { class: "book-setting" }, labels("Zoom", "Zoom"), zoomControl)));
        const search = h("details", { class: "book-disclosure" }, h("summary", {}, labels("Buscar en el libro", "Find in book")), h("div", { class: "book-find" }, query, find));
        const savedMarks = bookmarkBox(book, "pdf", draw, () => ({ page: current, label: labels(`Página ${current}`, `Page ${current}`) }));
        fill(ui.navigation, nav);
        fill(ui.tools, h("details", { class: "book-toolbox" }, h("summary", {}, labels("Herramientas de lectura", "Reading tools")), settings, search, savedMarks));
        ui.hint.hidden = false;
        const resized = () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { if (alive()) draw(current); }, 250); };
        window.addEventListener("resize", resized);
        const priorDestroy = destroy;
        destroy = () => { revision++; clearTimeout(resizeTimer); window.removeEventListener("resize", resized); unbindPages(); if (renderTask) renderTask.cancel(); priorDestroy(); };
        await draw(current);
      } else throw new Error("format");
    };
    run().catch(error => {
      if (alive() && error.message !== "cancelled") { ui.root.setAttribute("aria-busy", "false"); ui.message.textContent = failure(error); }
    });
    return () => { cancel.cancelled = true; if (cancel.stop) cancel.stop(); destroy(); };
  }
  return { MAX_FILE, mime, bytes, filename, exportOriginal, inspect, safeEpub, position, marks, fileState, open, failure };
})();
