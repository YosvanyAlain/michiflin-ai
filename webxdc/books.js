"use strict";
window.BookShelf = (() => {
  let speechRevision = 0;
  const coverPending = new Map(), coverQueue = [];
  let coverBusy = 0;
  const recentCovers = new Map(store.get("books.coverCache", []).filter(([, value]) => Date.now() - value.at < 86400000));
  function pumpCovers() {
    while (coverBusy < 2 && coverQueue.length) { const task = coverQueue.shift(); coverBusy++;
      bridge.call("books", { do: "cover", url: task.url }).then(response => {
        if (typeof response.data !== "string" || !response.data.startsWith("data:image/jpeg;base64,")) throw new Error("cover");
        recentCovers.set(task.url, { data: response.data, at: Date.now() });
        while (recentCovers.size > 24) recentCovers.delete(recentCovers.keys().next().value);
        store.set("books.coverCache", [...recentCovers]); task.resolve(response.data);
      }).catch(task.reject).finally(() => { coverBusy--; coverPending.delete(task.url); pumpCovers(); });
    }
  }
  function coverData(url) {
    const hit = recentCovers.get(url); if (hit) return Promise.resolve(hit.data);
    if (!coverPending.has(url)) {
      const promise = new Promise((resolve, reject) => coverQueue.push({ url, resolve, reject }));
      coverPending.set(url, promise); pumpCovers();
    }
    return coverPending.get(url);
  }
  const text = (es, en) => lang() === "en" ? en : es;
  const stopSpeech = () => {
    speechRevision++;
    // Some embedded Android clients expose a partial/denied speech API.
    // Optional accessibility cleanup must never prevent every view opening.
    try { const voice = window.speechSynthesis; if (voice && typeof voice.cancel === "function") voice.cancel(); }
    catch (_) { /* Reader export remains available without speech. */ }
  };
  const database = () => new Promise((resolve, reject) => {
    const request = indexedDB.open("michiflin-reading-library-v1", 1);
    request.onupgradeneeded = () => request.result.createObjectStore("books", { keyPath: "id" });
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(new Error("storage"));
  });
  async function operation(mode, action) {
    const db = await database();
    try { return await new Promise((resolve, reject) => {
      const tx = db.transaction("books", mode), request = action(tx.objectStore("books"));
      tx.oncomplete = () => resolve(request.result);
      tx.onerror = tx.onabort = () => reject(new Error("storage"));
    }); } finally { db.close(); }
  }
  const all = () => operation("readonly", store => store.getAll());
  const save = book => operation("readwrite", store => store.put(book));
  const remove = id => operation("readwrite", store => store.delete(id));
  const exportText = book => exportSheet(new Blob([book.title + "\n" + (book.authors || []).join(", ") + "\n\n" + book.text], { type: "text/plain;charset=utf-8" }), niceName(book.title, "libro") + ".txt");
  function cover(book) {
    const box = h("div", { class: "book-cover", "aria-label": book.title }, icon("book"));
    if (!book.cover_url) return box;
    const load = async () => {
      try {
        if (!box.isConnected) return;
        const data = await coverData(book.cover_url);
        if (box.isConnected) fill(box, h("img", { src: data, alt: book.title, loading: "lazy", decoding: "async" }));
      } catch (_) { /* A catalogue entry must remain usable without its cover. */ }
    };
    if (window.IntersectionObserver) {
      const observer = new IntersectionObserver(entries => { if (entries.some(e => e.isIntersecting)) { observer.disconnect(); load(); } }, { root: $("#view"), rootMargin: "40px" });
      observer.observe(box);
    } else setTimeout(load, 0);
    return box;
  }
  function reader(book) {
    stopSpeech();
    const main = $("#view");
    let page = Math.min(Math.max(0, Number(store.get("book.page." + book.id, 0)) || 0), Math.max(0, Math.ceil(book.text.length / 4500) - 1));
    const pages = [];
    let remaining = book.text;
    while (remaining) {
      let cut = Math.min(4500, remaining.length);
      if (cut < remaining.length) { const boundary = remaining.lastIndexOf("\n", cut); if (boundary > 2200) cut = boundary; }
      pages.push(remaining.slice(0, cut)); remaining = remaining.slice(cut);
    }
    page = Math.min(page, pages.length - 1);
    const content = h("article", { class: "book-reading", tabindex: "0", "aria-label": book.title });
    const label = h("span", { class: "muted small" });
    const previous = h("button", { class: "btn ghost small", onclick: () => draw(page - 1) }, icon("back"));
    const next = h("button", { class: "btn ghost small", onclick: () => draw(page + 1) }, icon("next"));
    const draw = index => {
      stopSpeech(); page = Math.min(Math.max(0, index), pages.length - 1);
      content.replaceChildren(...pages[page].split(/\n+/).map(block => h("p", {}, block)));
      label.textContent = `${page + 1} / ${pages.length}`;
      previous.disabled = page === 0; next.disabled = page >= pages.length - 1;
      store.set("book.page." + book.id, page);
      main.scrollTop = 0;
    };
    let canSpeak = false;
    try { canSpeak = !!(window.speechSynthesis && window.SpeechSynthesisUtterance &&
      typeof window.speechSynthesis.speak === "function" && typeof window.speechSynthesis.getVoices === "function"); }
    catch (_) { /* Denied speech must not prevent reading/exporting the text. */ }
    const read = h("button", { class: "btn primary small", disabled: !canSpeak, onclick: () => {
      stopSpeech(); const revision = speechRevision;
      const parts = pages.slice(page).join("\n").match(/[\s\S]{1,1400}/g) || [];
      const speak = index => {
        if (revision !== speechRevision || !main.contains(content) || index >= parts.length) return;
        const utterance = new SpeechSynthesisUtterance(parts[index]);
        utterance.lang = (book.languages || [lang()])[0] || lang();
        const voices = speechSynthesis.getVoices();
        utterance.voice = voices.find(voice => voice.localService && voice.lang.startsWith(utterance.lang)) || voices.find(voice => voice.lang.startsWith(utterance.lang)) || null;
        utterance.onend = () => speak(index + 1);
        utterance.onerror = event => {
          if (revision === speechRevision && !["canceled", "interrupted"].includes(event.error))
            toast(text("La voz del cliente no está disponible. Exporta el texto para el lector del sistema.", "Client speech is unavailable. Export the text for your system reader."), "bad");
        };
        speechSynthesis.speak(utterance);
      };
      try { speak(0); }
      catch (_) { toast(text("La voz del cliente no está disponible. Exporta el texto para el lector del sistema.",
        "Client speech is unavailable. Export the text for your system reader."), "bad"); }
    } }, icon("audio"), text("Leer en voz alta", "Read aloud"));
    fill(main, h("button", { class: "btn ghost small", onclick: () => { stopSpeech(); go("books", { tab: "saved" }); } }, icon("back"), text("Mi biblioteca", "My library")),
      h("h2", {}, book.title), h("p", { class: "muted" }, (book.authors || []).join(", ")),
      h("div", { class: "book-reader-controls" }, previous, label, next, read,
        h("button", { class: "btn ghost small", onclick: stopSpeech }, icon("pause"), text("Detener voz", "Stop speech")),
        h("button", { class: "btn ghost small", onclick: () => exportText(book) }, icon("share"), text("Exportar al lector del sistema", "Export to system reader"))),
      h("p", { class: "muted small" }, text("La voz depende de tu sistema y cliente. El libro descargado se lee sin conexión; exportarlo permite usar otras opciones de accesibilidad.", "Speech depends on your system and client. Downloaded books can be read offline; export them for other accessibility options.")), content);
    draw(page);
  }
  async function mount(main, params = {}) {
    let local = new Map(), revision = 0;
    let active = params.tab === "saved" ? "saved" : "catalogue";
    const shelf = h("div", { class: "book-shelf" });
    const status = h("p", { class: "muted small", role: "status" });
    const query = field({ type: "search", placeholder: text("Título, autor o tema", "Title, author or topic"), value: params.q || store.get("books.query", "") });
    let source = store.get("books.source", "gutenberg"), page = 1;
    const sourcePicker = select(source, [["all", text("Todos los catálogos", "All catalogues")], ["gutenberg", "Project Gutenberg"], ["openlibrary", "Open Library"],
      ["archive", "Internet Archive"], ["google", "Google Books · gratuitos"], ["standard", "Standard Ebooks"], ["wikisource", "Wikisource"], ["web", text("Buscar en la web", "Search the web")]], value => { source = value; page = 1; store.set("books.source", value); search(); });
    const more = h("button", { class: "btn ghost wide", hidden: true, onclick: () => { page++; search(true); } }, text("Más libros", "More books"));
    const details = book => openSheet(book.title, [h("p", {}, (book.authors || []).join(", ")),
      h("p", { class: "muted small" }, book.source + " · " + book.access),
      h("p", {}, book.description || text("Consulta la ficha del proveedor para la descripción completa y las condiciones de acceso.", "See the provider record for the full description and access terms.")),
      h("p", { class: "note" }, text("Descargar guarda una copia de lectura en esta miniapp. EPUB/PDF se convierten a texto. No se eluden préstamos ni DRM. Comprueba los derechos en tu país.", "Download saves a reading copy in this miniapp. EPUB/PDF are converted to text. Loans and DRM are never bypassed. Check rights in your country.")),
      h("a", { class: "btn ghost", href: book.url, target: "_blank", rel: "noopener noreferrer" }, text("Ficha y formatos originales", "Record and original formats"))]);
    const addBook = book => {
      if (!local.has(book.id) && !(book.downloads || []).some(item => ["txt", "epub", "pdf", "html", "htm"].includes(item.format))) return;
      const button = h("button", { class: "btn primary small", onclick: async () => {
        if (local.has(book.id)) return reader(local.get(book.id));
        button.disabled = true;
        try {
          let downloads = book.downloads || [];
          if (!downloads.length && book.archive_id) downloads = (await bridge.call("books", { do: "archive_files", id: book.archive_id })).downloads || [];
          if (!downloads.length) { details(book); return; }
          const chosen = downloads.find(item => item.format === "txt") || downloads.find(item => item.format === "epub") || downloads[0];
          status.textContent = text("Descargando copia de lectura…", "Downloading reading copy…");
          const response = await bridge.call("books", { do: "download", url: chosen.url, format: chosen.format });
          if (!response.complete || typeof response.text !== "string" || !response.text) throw new Error("incomplete");
          if (local.size >= 40 && !local.has(book.id)) throw new Error("storage");
          const stored = { ...book, text: response.text, sha256: response.sha256, downloaded: Date.now(), source_format: response.source_format };
          await save(stored); local.set(book.id, stored);
          button.textContent = text("Abrir", "Open");
          status.textContent = text("Libro guardado para leer sin conexión en esta miniapp.", "Book saved for offline reading in this miniapp.");
        } catch (_) { status.textContent = text("No se pudo descargar o guardar. Puede ser un catálogo, préstamo, formato protegido, límite de tamaño o almacenamiento. Consulta la ficha original.", "Could not download or save. This may be a catalogue, loan, protected format, size limit or storage limit. Check the original record."); }
        finally { button.disabled = false; }
      } }, local.has(book.id) ? text("Abrir", "Open") :
        ((book.downloads || []).length || book.archive_id ? text("Descargar", "Download") : text("Consultar acceso", "Check access")));
      const card = h("article", { class: "card book-card" }, cover(book), h("h3", { class: "book-title" }, book.title),
        h("p", { class: "muted small" }, (book.authors || []).join(", ")), h("span", { class: "tag" }, book.source),
        h("div", { class: "book-actions" }, button, h("button", { class: "btn ghost small", onclick: () => details(book) }, text("Detalles", "Details"))));
      if (active === "saved") add(card, h("button", { class: "btn ghost small", onclick: async () => {
        if (await confirmBox(t("delete"), text("¿Eliminar solo esta copia de la miniapp?", "Delete only this miniapp copy?"), t("delete"))) { await remove(book.id); local.delete(book.id); card.remove(); }
      } }, icon("trash"), t("delete")));
      add(shelf, card);
    };
    const search = async (append = false, force = false) => {
      const version = ++revision;
      status.textContent = t("searching");
      if (!append) shelf.replaceChildren();
      more.hidden = true;
      try {
        const args = { do: "search", source, q: query.value.trim(), page, language: "" };
        store.set("books.query", args.q);
        const key = JSON.stringify(args), recent = store.get("books.catalogueCache", []);
        const hit = recent.find(item => item.key === key && Date.now() - item.at < 600000);
        status.classList.add("book-sync"); status.setAttribute("aria-busy", "true");
        status.textContent = text("Sincronizando catálogo…", "Syncing catalogue…");
        const response = hit && !force ? hit.data : await bridge.call("books", args);
        if (!hit || force) store.set("books.catalogueCache", [{ key, data: response, at: Date.now() }, ...recent.filter(item => item.key !== key)].slice(0, 6));
        status.classList.remove("book-sync"); status.removeAttribute("aria-busy");
        if (!main.isConnected || version !== revision || active !== "catalogue") return;
        (response.books || []).forEach(addBook);
        more.hidden = !response.more;
        status.textContent = response.unavailable?.length ? text("Algunas fuentes no respondieron: ", "Some sources did not respond: ") + response.unavailable.join(", ") : (response.books?.length ? "" : t("no_results"));
      } catch (_) { status.classList.remove("book-sync"); status.removeAttribute("aria-busy"); if (version === revision) status.textContent = text("La fuente no respondió. Prueba otro catálogo o la búsqueda web.", "The source did not respond. Try another catalogue or web search."); }
    };
    const controls = h("div", { class: "stack" });
    // askBox owns its own field in older clients; keep a clear explicit search form.
    fill(controls, sourcePicker, h("div", { class: "row" }, query, h("button", { class: "btn primary", onclick: () => { page = 1; search(); } }, icon("search"), t("search")), h("button", { class: "icon-btn", "aria-label": t("refresh"), onclick: () => { page = 1; search(false, true); } }, icon("refresh"))));
    query.addEventListener("keydown", event => { if (event.key === "Enter") { page = 1; search(); } });
    const drawTab = async value => {
      stopSpeech(); active = value; revision++; shelf.replaceChildren(); more.hidden = true; controls.hidden = value !== "catalogue"; status.textContent = "";
      if (value === "catalogue") { page = 1; search(); }
      if (value === "saved") { for (const book of local.values()) addBook(book); if (!local.size) status.textContent = text("Todavía no hay libros descargados en esta instancia.", "No downloaded books in this instance yet."); }
    };
    add(main, h("p", { class: "note" }, text("Libros con copia de lectura descargable. Los préstamos y fichas sin descarga no se muestran. Las copias guardadas pertenecen a esta miniapp.", "Books with downloadable reading copies. Loans and records without downloads are hidden. Saved copies belong to this miniapp instance.")),
      segmented(active, [["catalogue", text("Explorar", "Explore")], ["saved", text("Mi biblioteca", "My library")]], drawTab), controls, status, shelf, more);
    try { local = new Map((await all()).map(book => [book.id, book])); } catch (_) { status.textContent = text("Almacenamiento local no disponible. No se afirmará que un libro fue guardado.", "Local storage unavailable. Books will not be reported as saved."); }
    if (active === "saved") drawTab("saved");
    else search();
  }
  return { mount, stopSpeech, all, clearCache() { recentCovers.clear(); store.del("books.coverCache"); store.del("books.catalogueCache"); } };
})();
