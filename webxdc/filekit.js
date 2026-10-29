"use strict";

// File kit UI. It owns no originals: the local store and the remote workspace
// contain copies. All host access uses the existing authenticated bridge.
const FileKitUI = (() => {
  let activeWorker = null;
  let previewURL = "";
  let cancellation = 0;
  const MiB = 1024 * 1024;

  function stop() {
    cancellation += 1;
    if (activeWorker) {
      const active = activeWorker;
      activeWorker = null;
      clearTimeout(active.timer);
      active.worker.terminate();
      active.reject(new Error("worker_cancelled"));
    }
    if (previewURL) URL.revokeObjectURL(previewURL);
    previewURL = "";
  }

  function message(error) {
    const code = error && (error.name === "QuotaExceededError" ? error.name : error.message);
    if (code === "worker_cancelled") return t("kit_cancelled");
    if (code && has("kit_err_" + code)) return t("kit_err_" + code);
    // Remote errors are already localized; never display raw JS exceptions.
    if (typeof code === "string" && code.length < 300 && /[ .;,áéíóúñ]/i.test(code)) return code;
    return t("kit_error");
  }

  function bytes(blob) {
    if (blob.arrayBuffer) return blob.arrayBuffer();
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(reader.error || new Error("read_failed"));
      reader.readAsArrayBuffer(blob);
    });
  }

  function worker(job) {
    if (activeWorker) return Promise.reject(new Error("storage_busy"));
    return new Promise((resolve, reject) => {
      const instance = new Worker("filekit-worker.js");
      const finish = (result, error) => {
        if (!activeWorker || activeWorker.worker !== instance) return;
        clearTimeout(activeWorker.timer);
        activeWorker = null;
        instance.terminate();
        if (error) reject(error);
        else resolve(result);
      };
      const timer = setTimeout(() => finish(null, new Error("worker_timeout")), 180000);
      activeWorker = { worker: instance, timer, reject };
      instance.onerror = () => finish(null, new Error("engine_unavailable"));
      instance.onmessage = (event) => {
        const data = event.data || {};
        if (data.ok) finish(data.result);
        else finish(null, new Error(data.code || "archive_failed"));
      };
      instance.postMessage(job);
    });
  }

  function actionButton(label, iconName, handler, disabled = false, key = "") {
    return h("button", { class: "chip action", type: "button", disabled, "data-kit-action": key || null, onclick: handler }, icon(iconName), t(label));
  }

  function inputDialog(title, label, initial, callback, help = "") {
    const input = field({ type: "text", value: initial, maxlength: "255", autocomplete: "off" });
    const submit = h("button", { class: "btn primary", type: "button", onclick: async () => {
      const value = input.value.trim();
      try {
        FileKitStore.name(value);
        submit.disabled = true;
        closeSheet();
        await callback(value);
      } catch (error) { toast(message(error), "bad"); submit.disabled = false; }
    } }, t("confirm"));
    openSheet(title, [help ? h("p", { class: "muted small" }, help) : null,
      h("label", { class: "field-label" }, label), input,
      h("div", { class: "row end" }, h("button", { class: "btn ghost", onclick: closeSheet }, t("cancel")), submit)]);
    setTimeout(() => input.focus(), 50);
  }

  function render(main, options = {}) {
    const root = h("div", { class: "kit-shell", "data-testid": "filekit" });
    const tree = h("div", { class: "kit-tree", role: "tree", "aria-label": t("kit_tree"), tabindex: "0" });
    const tools = h("div", { class: "kit-tools" });
    const crumbs = h("div", { class: "kit-crumbs" });
    const preview = h("div", { class: "kit-preview" }, h("p", { class: "muted small" }, t("kit_preview_empty")));
    const status = h("div", { class: "kit-status", role: "status", hidden: true });
    const note = h("p", { class: "kit-note" });
    const importHint = h("p", { class: "muted small" });
    const selected = new Set();
    let mode = options.jobId ? "remote" : "local";
    let folder = "";
    let view = { entries: [], canUndo: false, canRedo: false };
    let clip = null;
    let expanded = new Set([""]);
    let busy = false;
    let previewSerial = 0;
    let listSerial = 0;
    let modeControl;

    const isHere = () => root.isConnected;
    const within = (value, ancestor) => value === ancestor || value.startsWith(ancestor + "/");
    const byPath = (value) => view.entries.find((entry) => entry.path === value);
    const selectedEntries = () => [...selected].map(byPath).filter(Boolean);
    const selectionRoots = () => [...selected].sort().filter((value, index, all) => !all.slice(0, index).some((other) => within(value, other)));
    const single = (file = false) => {
      const entries = selectedEntries();
      if (entries.length !== 1 || (file && entries[0].type !== "file")) {
        toast(t(file ? "kit_select_file" : "kit_select_one"), "bad");
        return null;
      }
      return entries[0];
    };
    const requireSelection = () => {
      if (!selected.size) { toast(t("kit_nothing"), "bad"); return false; }
      return true;
    };
    const remote = (action, args = {}) => bridge.call("filekit", { action, ...args });
    const mutate = async (action, args = {}) => {
      if (mode === "remote") return remote(action, args);
      switch (action) {
        case "mkdir": return FileKitStore.mkdir(args.folder, args.name);
        case "rename": return FileKitStore.rename(args.path, args.name);
        case "copy": case "move": return FileKitStore.transfer(args.paths, args.folder, action === "copy");
        case "delete": return FileKitStore.remove(args.paths);
        case "undo": return FileKitStore.undo();
        case "redo": return FileKitStore.redo();
        case "clear_history": return FileKitStore.clearHistory();
        default: throw new Error("invalid_request");
      }
    };

    const work = async (task, refreshAfter = true, cancellable = false, successKey = "kit_done") => {
      if (busy) return false;
      busy = true;
      status.hidden = false;
      fill(status, h("i", { class: "spinner" }), h("span", { class: "grow" }, t("kit_working")),
        cancellable ? actionButton("kit_cancel_work", "close", stop, false, "cancel-work") : null);
      drawTools();
      try {
        await task();
        if (isHere() && successKey) toast(t(successKey), "ok");
        if (isHere() && refreshAfter) await refresh().catch(() => {});
        return true;
      } catch (error) {
        if (isHere()) toast(message(error), "bad");
        return false;
      } finally {
        busy = false;
        if (isHere()) { status.hidden = true; drawTools(); }
      }
    };

    const upload = async (blob, filename, destination) => {
      const info = mode === "remote" && view.key ? view : await remote("list");
      if (info.partMiB && blob.size > info.partMiB * MiB) throw new Error("import_part_limit");
      const digest = await FileKitCrypto.hashBlob(blob);
      const secret = FileKitCrypto.seal(info.key, digest);
      const draft = await remote("prepare_import", { folder: destination, name: filename, size: blob.size, secret });
      // This opens a user-confirmed draft; its resolution is NOT a delivery receipt.
      await sendToChat(blob, filename, draft.caption);
    };

    const importMobile = async () => {
      let files;
      try { files = await pickFiles(mode === "local"); }
      catch (error) { toast(t("pick_failed"), "bad"); return; }
      if (!files || !files.length) return;
      const destination = folder;
      if (mode === "local") {
        await work(() => FileKitStore.put(destination, files.map((file) => ({ path: file.name, blob: file, mtime: file.lastModified }))));
      } else {
        const file = files[0];
        await work(() => upload(file, file.name, destination), false, false, "");
      }
    };

    const copyToServer = () => {
      const entry = single(true);
      if (!entry) return;
      work(async () => {
        const { blob } = await FileKitStore.get(entry.path);
        await upload(blob, FileKitStore.base(entry.path), "");
      }, false, false, "");
    };

    const drawTools = () => {
      if (modeControl) modeControl.querySelectorAll("button").forEach((button) => { button.disabled = busy; });
      const one = selected.size === 1, any = selected.size > 0;
      fill(tools,
        h("div", { class: "kit-actions" },
          actionButton(mode === "local" ? "kit_import" : "kit_import_remote", "plus", importMobile, busy, "import"),
          actionButton("kit_new_folder", "folder", () => inputDialog(t("kit_new_folder"), t("kit_name"), "", (name) => work(() => mutate("mkdir", { folder, name }))), busy, "mkdir"),
          actionButton("kit_refresh", "refresh", refresh, busy, "refresh")),
        h("div", { class: "kit-actions" },
          actionButton("kit_rename", "notes", () => {
            const entry = single();
            if (entry) inputDialog(t("kit_rename"), t("kit_name"), FileKitStore.base(entry.path), (name) => work(() => mutate("rename", { path: entry.path, name })));
          }, busy || !one, "rename"),
          actionButton("kit_copy", "copy", () => { if (requireSelection()) { clip = { paths: selectionRoots(), mode: "copy", space: mode }; toast(t("kit_clip_copy"), "ok"); drawTools(); } }, busy || !any, "copy"),
          actionButton("kit_cut", "swap", () => { if (requireSelection()) { clip = { paths: selectionRoots(), mode: "move", space: mode }; toast(t("kit_clip_move"), "ok"); drawTools(); } }, busy || !any, "move"),
          actionButton("kit_paste", "clip", () => {
            if (!clip || clip.space !== mode) return;
            const saved = clip;
            work(async () => { await mutate(saved.mode, { paths: saved.paths, folder }); clip = null; });
          }, busy || !clip || clip.space !== mode, "paste"),
          actionButton("kit_delete", "trash", async () => {
            if (!requireSelection()) return;
            if (await confirmBox(t("kit_delete"), t("kit_delete_confirm"), t("kit_delete"))) {
              const paths = selectionRoots();
              work(async () => { await mutate("delete", { paths }); selected.clear(); });
            }
          }, busy || !any, "delete")),
        h("div", { class: "kit-actions" },
          actionButton("kit_undo", "back", () => work(() => mutate("undo")), busy || !view.canUndo, "undo"),
          actionButton("kit_redo", "next", () => work(() => mutate("redo")), busy || !view.canRedo, "redo"),
          actionButton("kit_clear_history", "trash", async () => {
            if (await confirmBox(t("kit_clear_history"), t("kit_clear_confirm"), t("kit_clear_history"))) work(() => mutate("clear_history"));
          }, busy || !(view.canUndo || view.canRedo), "clear-history"),
          mode === "local" ? actionButton("kit_to_server", "send", copyToServer, busy || !one, "to-server") : null,
          mode === "local" ? actionButton("kit_join_selected", "join", () => {
            const entries = selectedEntries();
            if (!entries.length || entries.some((entry) => entry.type !== "file")) { toast(t("kit_select_file"), "bad"); return; }
            go("join", { filekitPaths: entries.map((entry) => entry.path), kitFolder: folder });
          }, busy || !any, "join-selected") : null),
        h("small", { class: "muted" }, t("kit_items", { n: selected.size })));
    };

    const drawCrumbs = () => {
      const parts = folder ? folder.slice(1).split("/") : [];
      const nodes = [h("button", { class: "kit-crumb", onclick: () => { folder = ""; draw(); } }, t("kit_root"))];
      let at = "";
      for (const part of parts) {
        at += "/" + part;
        const destination = at;
        nodes.push(h("span", { "aria-hidden": "true" }, "/"), h("button", { class: "kit-crumb", onclick: () => { folder = destination; draw(); } }, part));
      }
      fill(crumbs, ...nodes);
      crumbs.setAttribute("aria-label", t("kit_folder"));
    };

    const drawTree = () => {
      const children = new Map();
      for (const entry of view.entries) {
        const parent = FileKitStore.parent(entry.path);
        if (!children.has(parent)) children.set(parent, []);
        children.get(parent).push(entry);
      }
      for (const list of children.values()) list.sort((a, b) => (a.type === b.type ? FileKitStore.base(a.path).localeCompare(FileKitStore.base(b.path), lang(), { numeric: true }) : a.type === "dir" ? -1 : 1));
      const nodes = [];
      const visit = (parent, depth) => {
        for (const entry of children.get(parent) || []) {
          const isDir = entry.type === "dir", name = FileKitStore.base(entry.path), open = expanded.has(entry.path);
          const check = h("input", { type: "checkbox", checked: selected.has(entry.path), "aria-label": t("kit_select", { name }), onchange: (event) => {
            if (event.target.checked) selected.add(entry.path); else selected.delete(entry.path);
            drawTools(); event.currentTarget.closest(".kit-row").classList.toggle("selected", event.target.checked);
          } });
          const toggle = isDir ? h("button", { class: "kit-toggle", type: "button", "aria-label": t(open ? "kit_collapse" : "kit_expand"), "aria-expanded": String(open), onclick: () => {
            if (open) expanded.delete(entry.path); else expanded.add(entry.path); drawTree();
          } }, icon(open ? "down" : "next")) : h("span", { class: "kit-toggle placeholder" });
          const openItem = h("button", { class: "kit-item", type: "button", "data-kit-open": entry.path, onclick: () => {
            if (isDir) { folder = entry.path; expanded.add(entry.path); draw(); }
            else showFile(entry);
          } }, icon(isDir ? "folder" : "file"), h("span", { class: "kit-item-name" }, name),
          isDir ? null : h("small", { class: "muted" }, fmtSize(entry.size || 0)));
          nodes.push(h("div", { class: `kit-row${selected.has(entry.path) ? " selected" : ""}${folder === entry.path ? " current" : ""}`,
            role: "treeitem", "aria-level": String(depth + 1), "data-kit-path": entry.path,
            style: `padding-inline-start:${Math.min(depth, 12) * 16 + 5}px` }, toggle, check, openItem));
          if (isDir && open) visit(entry.path, depth + 1);
        }
      };
      visit("", 0);
      if (!nodes.length) nodes.push(h("p", { class: "kit-empty" }, t("kit_empty")));
      fill(tree, ...nodes);
    };

    const draw = () => {
      note.textContent = t(mode === "local" ? "kit_local_note" : "kit_remote_note");
      importHint.textContent = mode === "local" ? t("kit_import_local_hint") : `${t("kit_import_remote_hint")} ${t("kit_upload_limit", { n: view.partMiB || 10 })}`;
      drawCrumbs(); drawTree(); drawTools();
    };

    const refresh = async () => {
      const serial = ++listSerial, requestedMode = mode;
      try {
        const next = requestedMode === "local" ? await FileKitStore.view() : await remote("list");
        if (!isHere() || serial !== listSerial || mode !== requestedMode) return;
        view = next;
        for (const path of [...selected]) if (!byPath(path)) selected.delete(path);
        if (folder && (!byPath(folder) || byPath(folder).type !== "dir")) folder = "";
        draw();
      } catch (error) {
        if (isHere() && serial === listSerial) toast(message(error), "bad");
        throw error;
      }
    };

    const changeMode = (next) => {
      if (busy) return;
      stop(); previewSerial++;
      mode = next; folder = ""; selected.clear(); clip = null; expanded = new Set([""]);
      view = { entries: [], canUndo: false, canRedo: false };
      fill(preview, h("p", { class: "muted small" }, t("kit_preview_empty")));
      draw(); refresh().catch(() => {});
    };

    const sendDialog = (entry) => {
      const part = field({ type: "number", min: "1", max: "20", step: "1", value: String(view.partMiB || 10) });
      const submit = h("button", { class: "btn primary", onclick: () => {
        const amount = Number(part.value);
        if (!Number.isInteger(amount) || amount < 1 || amount > 20) { toast(t("kit_err_invalid_volume"), "bad"); return; }
        closeSheet();
        work(async () => {
          await remote("send", { path: entry.path, partMiB: amount });
          toast(t("kit_queued"), "ok");
        }, false);
      } }, t("kit_send"));
      openSheet(t("kit_send"), [h("p", { class: "muted small" }, t("kit_part_hint", { n: view.partMiB || 10 })),
        h("label", { class: "field-label" }, t("kit_part_size")), part, submit]);
    };

    const showFile = async (entry) => {
      const serial = ++previewSerial, sourceMode = mode;
      if (previewURL) URL.revokeObjectURL(previewURL);
      previewURL = "";
      fill(preview, loading(t("kit_preview")));
      try {
        let blob, version;
        if (sourceMode === "local") {
          const item = await FileKitStore.get(entry.path);
          blob = item.blob; version = item.entry.blob;
        } else {
          const reply = FileKitCrypto.replyKey();
          try {
            const result = await remote("read", { path: entry.path, replyKey: reply.publicKey });
            const raw = FileKitCrypto.openReply(reply, result.sealed);
            blob = new Blob([raw], { type: result.mime || mimeOf(entry.path) });
            raw.fill(0);
            version = result.version;
          } finally { reply.secretKey.fill(0); }
        }
        if (!isHere() || mode !== sourceMode || serial !== previewSerial) return;
        const filename = FileKitStore.base(entry.path);
        const type = blob.type || mimeOf(filename);
        const extType = mimeOf(filename);
        const effective = type === "application/octet-stream" ? extType : type;
        const controls = h("div", { class: "kit-actions" }, sourceMode === "local"
          ? actionButton("kit_export", "share", () => exportSheet(blob, filename), false, "export")
          : actionButton("kit_send", "send", () => sendDialog(entry), false, "send"));
        const content = [];
        const safeImage = /^image\/(png|jpeg|gif|webp|avif)$/i.test(effective);
        const safeMedia = /^(audio|video)\/(mpeg|mp4|quicktime|webm|ogg|wav|x-wav|opus)$/i.test(effective);
        if (safeImage || safeMedia) {
          previewURL = URL.createObjectURL(blob);
          content.push(safeImage ? h("img", { class: "kit-media", src: previewURL, alt: filename })
            : h(effective.startsWith("audio/") ? "audio" : "video", { class: "kit-media", src: previewURL, controls: true, preload: "metadata", playsinline: true }));
        } else {
          const maxText = sourceMode === "remote" ? (view.textBytes || 70000) : 4 * MiB;
          if (blob.size > maxText) content.push(h("p", { class: "muted small" }, t("kit_text_large", { size: fmtSize(maxText) })));
          else {
            try {
              const data = new Uint8Array(await bytes(blob));
              if (data.includes(0)) throw new Error("binary_not_text");
              const value = new TextDecoder("utf-8", { fatal: true }).decode(data);
              const area = h("textarea", { class: "kit-editor", "aria-label": t("kit_preview"), spellcheck: "false", value });
              const save = h("button", { class: "btn primary small", "data-kit-action": "save-text", onclick: () => {
                const text = area.value;
                if (new TextEncoder().encode(text).length > maxText) { toast(t("kit_text_large", { size: fmtSize(maxText) }), "bad"); return; }
                const saving = sourceMode === "local"
                  ? work(() => FileKitStore.write(entry.path, new Blob([text], { type: blob.type || "text/plain" }), version))
                  : work(async () => {
                    const sealed = FileKitCrypto.sealText(view.key, text);
                    await remote("write", { path: entry.path, version, sealed });
                  });
                saving.then((ok) => { if (ok && isHere() && mode === sourceMode) showFile(byPath(entry.path) || entry); });
              } }, t("kit_save_text"));
              content.push(area, save);
            } catch (error) { content.push(h("p", { class: "muted small" }, t("kit_preview_unknown"))); }
          }
        }
        if (!isHere() || mode !== sourceMode || serial !== previewSerial) return;
        fill(preview, h("div", { class: "row between" }, h("b", { class: "clamp1" }, filename), h("small", { class: "muted" }, fmtSize(blob.size))), controls, ...content);
      } catch (error) {
        if (isHere() && serial === previewSerial) fill(preview, h("p", { class: "note warn" }, message(error)),
          sourceMode === "remote" ? actionButton("kit_send", "send", () => sendDialog(entry), false, "send") : null);
      }
    };

    const gather = async (roots) => {
      const files = [], seen = new Set();
      const entries = view.entries.slice();
      for (const selectedPath of roots) {
        for (const entry of entries.filter((item) => within(item.path, selectedPath))) {
          const path = FileKitStore.base(selectedPath) + entry.path.slice(selectedPath.length);
          if (seen.has(path)) throw new Error("duplicate_path");
          seen.add(path);
          if (entry.type === "dir") files.push({ path, type: "dir" });
          else files.push({ path, type: "file", blob: (await FileKitStore.get(entry.path)).blob });
        }
      }
      return files;
    };

    const archiveDialog = (action) => {
      if (!requireSelection()) return;
      const paths = selectionRoots(), files = selectedEntries().filter((entry) => entry.type === "file");
      if (action === "extract" && !files.length) { toast(t("kit_select_file"), "bad"); return; }
      const firstDefault = files.find((entry) => /(?:\.001|\.part0*1\.rar)$/i.test(entry.path)) || files[0];
      const first = h("select", { class: "field", "aria-label": t("kit_first") }, ...files.map((entry) => h("option", { value: entry.path, selected: entry === firstDefault }, entry.path)));
      const format = h("select", { class: "field", "aria-label": t("kit_format") }, h("option", { value: "zip" }, "ZIP"), h("option", { value: "7z" }, "7z"));
      const suggested = action === "create" ? "Paquete.zip" : (FileKitStore.base(firstDefault.path).replace(/(?:\.part0*1\.rar|\.(?:zip|7z|rar)(?:\.001)?)$/i, "") || "Extraido") + " - archivos";
      const filename = field({ type: "text", value: suggested, maxlength: "255" });
      if (action === "create") format.addEventListener("change", () => { filename.value = filename.value.replace(/\.(zip|7z)$/i, "") + "." + format.value; });
      const password = field({ type: "password", autocomplete: "new-password", placeholder: t("kit_password") });
      const volume = field({ type: "number", min: "0", max: "512", step: "1", value: "0" });
      const savedMemory = Math.max(16, Math.min(128, Number(store.get("kit.memory", 64)) || 64));
      const memory = h("select", { class: "field", "aria-label": t("kit_memory") }, ...[16, 32, 64, 128].map((n) => h("option", { value: String(n), selected: n === savedMemory }, `${n} MB`)));
      const destination = folder, selectedMode = mode;
      const submit = h("button", { class: "btn primary wide", "data-kit-action": "run-archive", onclick: () => {
        const name = filename.value.trim(), pass = password.value, amount = Number(volume.value), chosen = first.value;
        try {
          FileKitStore.name(name);
          if (/[\u0000\r\n]/.test(pass) || pass.length > 1024) throw new Error("invalid_password");
          if (!Number.isInteger(amount) || amount < 0 || amount > 512) throw new Error("invalid_volume");
        } catch (error) { toast(message(error), "bad"); return; }
        password.value = "";
        const budget = Number(memory.value) * MiB, ticket = cancellation;
        if (selectedMode === "local") store.set("kit.memory", Number(memory.value));
        closeSheet();
        work(async () => {
          if (selectedMode === "remote") {
            const args = { paths, folder: destination, name };
            if (pass) args.secret = FileKitCrypto.seal(view.key, pass);
            if (action === "create") Object.assign(args, { format: format.value, volumeBytes: amount * MiB });
            else args.first = chosen;
            await remote(action === "create" ? "archive_create" : "archive_extract", args);
          } else {
            const total = view.entries.filter((entry) => entry.type === "file" && paths.some((path) => within(entry.path, path)))
              .reduce((sum, entry) => sum + (entry.size || 0), 0);
            if (!Number.isSafeInteger(total) || total > budget) throw new Error("input_limit");
            const source = await gather(paths);
            if (ticket !== cancellation || !isHere()) throw new Error("worker_cancelled");
            const picked = paths.find((rootPath) => within(chosen, rootPath));
            const relativeFirst = picked ? FileKitStore.base(picked) + chosen.slice(picked.length) : "";
            const job = { action, files: source, limit: budget, password: pass };
            if (action === "create") Object.assign(job, { format: format.value, name, volumeBytes: amount * MiB });
            else job.first = relativeFirst;
            const result = await worker(job);
            if (!isHere() || mode !== selectedMode || ticket !== cancellation) throw new Error("worker_cancelled");
            const cancelButton = status.querySelector("[data-kit-action=cancel-work]");
            if (cancelButton) cancelButton.remove(); // Publishing is now one atomic store transaction.
            const items = (result.files || []).map((item) => ({ path: action === "extract" ? name + "/" + item.path : item.path,
              type: item.type, blob: item.type === "file" ? new Blob([item.bytes]) : undefined }));
            if (action === "extract") items.unshift({ path: name, type: "dir" });
            if (!items.length) throw new Error("archive_failed");
            await FileKitStore.put(destination, items);
          }
        }, true, selectedMode === "local");
      } }, t("kit_run"));
      openSheet(t(action === "create" ? "kit_create" : "kit_extract"), [
        action === "create" ? [h("label", { class: "field-label" }, t("kit_format")), format] : [h("label", { class: "field-label" }, t("kit_first")), first],
        h("label", { class: "field-label" }, t(action === "create" ? "kit_archive_name" : "kit_extract_name")), filename,
        h("label", { class: "field-label" }, t("kit_password")), password, h("p", { class: "muted small" }, t("kit_password_hint")),
        action === "create" ? [h("label", { class: "field-label" }, t("kit_volume")), volume] : null,
        selectedMode === "local" ? [h("label", { class: "field-label" }, t("kit_memory")), memory, h("p", { class: "muted small" }, t("kit_memory_hint"))]
          : h("p", { class: "muted small" }, t("kit_server_engine")),
        h("p", { class: "muted small" }, t("kit_compat")), submit]);
    };

    const importJobDialog = (jobId, defaultName) => {
      inputDialog(t("kit_import_job"), t("kit_job_folder"), defaultName || `Descarga ${jobId}`,
        (name) => work(() => remote("import_job", { job: String(jobId), folder, name })), t("kit_import_job_hint"));
    };

    modeControl = segmented(mode, [["local", t("kit_local"), "file"], ["remote", t("kit_remote"), "downloads"]], changeMode);
    add(main, root);
    add(root,
      h("div", { class: "kit-heading" }, h("div", {}, h("h2", {}, t("kit_title")), h("p", { class: "muted small" }, t("kit_archive_hint"))),
        h("button", { class: "chip action", onclick: () => go("join") }, icon("join"), t("kit_join"))),
      modeControl, note, importHint, status,
      card(h("div", { class: "row between" }, h("h3", {}, t("kit_tree")), h("small", { class: "muted" }, t("kit_folder"))), crumbs, tree, tools),
      card(h("h3", {}, t("kit_preview")), preview),
      card(h("h3", {}, t("kit_archive")), h("p", { class: "muted small" }, t("kit_archive_hint")),
        h("div", { class: "kit-actions" }, actionButton("kit_create", "join", () => archiveDialog("create"), false, "archive-create"),
          actionButton("kit_extract", "open", () => archiveDialog("extract"), false, "archive-extract")),
        h("p", { class: "muted small" }, t("kit_compat"))),
      card(h("h3", {}, t("kit_join")), h("p", { class: "muted small" }, t("kit_join_hint")),
        h("button", { class: "btn", onclick: () => go("join") }, icon("join"), " ", t("kit_join"))));
    draw();
    refresh().then(() => { if (options.jobId && isHere()) importJobDialog(options.jobId, options.jobName); }).catch(() => {});
  }

  return { render, stop };
})();
