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
    // This scrollable region contains native buttons and checkboxes, not an
    // ARIA tree widget (which would require a different arrow-key model).
    const tree = h("div", { class: "kit-tree", role: "region", "aria-label": t("kit_tree"), tabindex: "0" });
    const selectionBar = h("div", { class: "kit-selection" });
    const tools = h("div", { class: "kit-tools" });
    const crumbs = h("div", { class: "kit-crumbs" });
    const preview = h("div", { class: "kit-preview" }, h("p", { class: "muted small" }, t("kit_preview_empty")));
    const joinBody = h("div", { class: "stack", "data-testid": "filekit-join" });
    const joinPanel = h("details", { class: "card kit-join" }, h("summary", {}, t("kit_join")),
      h("p", { class: "muted small" }, t("kit_join_hint")), joinBody);
    let joiner = null;
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
        case "rename_many": return FileKitStore.renameMany(args.changes);
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
      drawSelection(); drawTools();
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
        if (isHere()) { status.hidden = true; drawSelection(); drawTools(); }
      }
    };

    const showJoin = async (paths = []) => {
      joinPanel.open = true;
      try {
        if (paths.length && joiner) await joiner.addPaths(paths);
        if (isHere()) joinPanel.scrollIntoView({ behavior: "smooth", block: "start" });
      } catch (error) { if (isHere()) toast(message(error), "bad"); }
    };

    const joinRemoteDialog = () => {
      const entries = selectedEntries();
      if (entries.length < 2 || entries.length > 999 || entries.some((entry) => entry.type !== "file" || !/\.\d{3}$/.test(entry.path))) {
        toast(t("kit_join_remote_unsupported"), "bad"); return;
      }
      const paths = entries.map((entry) => entry.path), firstName = FileKitStore.base(paths[0]);
      const suggested = firstName.replace(/\.\d{3}$/, "");
      const filename = field({ type: "text", value: suggested, maxlength: "255" });
      const hash = field({ type: "text", autocomplete: "off", placeholder: t("kit_join_remote_hash"), maxlength: "64" });
      const destination = folder;
      const submit = h("button", { class: "btn primary wide", type: "button", "data-kit-action": "run-remote-join", onclick: () => {
        const name = filename.value.trim(), digest = hash.value.trim();
        try {
          FileKitStore.name(name);
          if (digest && !/^[a-f\d]{64}$/i.test(digest)) throw new Error("invalid_hash");
          const sealed = FileKitCrypto.sealText(view.key, JSON.stringify({ sha256: digest }));
          hash.value = "";
          closeSheet();
          work(() => remote("join_raw", { paths, folder: destination, name, sealed }));
        } catch (error) { toast(message(error), "bad"); }
      } }, t("kit_join_selected"));
      openSheet(t("kit_join_remote_title"), [h("p", { class: "muted small" }, t("kit_join_remote_hint")),
        h("label", { class: "field-label" }, t("kit_name")), filename,
        h("label", { class: "field-label" }, t("kit_join_remote_hash")), hash,
        h("p", { class: "muted small" }, t("kit_join_remote_hash_hint")), submit]);
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

    // A bulk rename is previewed, validated and committed as one history event.
    const renameDialog = () => {
      const roots = selectionRoots();
      if (!roots.length) { toast(t("kit_nothing"), "bad"); return; }
      if (roots.length === 1) {
        const entry = byPath(roots[0]);
        if (entry) inputDialog(t("kit_rename"), t("kit_name"), FileKitStore.base(entry.path),
          (name) => work(() => mutate("rename", { path: entry.path, name })));
        return;
      }
      if (roots.length > 1000) { toast(t("kit_bulk_limit"), "bad"); return; }
      const selectedMode = mode;
      const modeInput = h("select", { class: "input", "data-kit-bulk": "mode" },
        h("option", { value: "prefix" }, t("kit_bulk_prefix")), h("option", { value: "suffix" }, t("kit_bulk_suffix")),
        h("option", { value: "replace" }, t("kit_bulk_replace")), h("option", { value: "number" }, t("kit_bulk_number")));
      const value = field({ type: "text", maxlength: "255", autocomplete: "off", "data-kit-bulk": "value" });
      const find = field({ type: "text", maxlength: "255", autocomplete: "off", "data-kit-bulk": "find" });
      const start = field({ type: "number", min: "0", max: "999999", step: "1", value: "1", "data-kit-bulk": "start" });
      const findBox = h("div", {}, h("label", { class: "field-label" }, t("kit_bulk_find")), find);
      const startBox = h("div", {}, h("label", { class: "field-label" }, t("kit_bulk_start")), start);
      const valueLabel = h("label", { class: "field-label" });
      const preview = h("div", { class: "kit-bulk-preview", tabindex: "0", "aria-label": t("kit_bulk_preview") });
      const warning = h("p", { class: "note bad", role: "status", hidden: true });
      let plan = [];
      const submit = h("button", { class: "btn primary", type: "button", "data-kit-bulk": "submit", onclick: async () => {
        if (submit.disabled || mode !== selectedMode) return;
        const changes = plan.map((item) => ({ path: item.path, name: item.name }));
        submit.disabled = true;
        closeSheet();
        await work(async () => { await mutate("rename_many", { changes }); selected.clear(); });
      } }, t("kit_bulk_apply"));
      const update = () => {
        const kind = modeInput.value;
        findBox.hidden = kind !== "replace";
        startBox.hidden = kind !== "number";
        valueLabel.textContent = t(kind === "replace" ? "kit_bulk_with" : kind === "number" ? "kit_bulk_base" : "kit_bulk_text");
        warning.hidden = true; plan = [];
        try {
          const first = Number(start.value);
          if ((kind === "prefix" || kind === "suffix") && !value.value) throw new Error("kit_bulk_enter");
          if (kind === "replace" && !find.value) throw new Error("kit_bulk_find_required");
          if (kind === "number" && (!Number.isInteger(first) || first < 0 || first > 999999)) throw new Error("kit_bulk_start_invalid");
          const padding = Math.max(3, String(first + roots.length - 1).length);
          for (const [index, source] of roots.entries()) {
            const entry = byPath(source);
            if (!entry) throw new Error("kit_err_missing");
            const original = FileKitStore.base(source);
            const dot = entry.type === "file" ? original.lastIndexOf(".") : -1;
            const hasExt = dot > 0 && dot < original.length - 1;
            const stem = hasExt ? original.slice(0, dot) : original;
            const ext = hasExt ? original.slice(dot) : "";
            const name = kind === "prefix" ? value.value + original : kind === "suffix" ? stem + value.value + ext
              : kind === "replace" ? original.split(find.value).join(value.value) : value.value + String(first + index).padStart(padding, "0") + ext;
            FileKitStore.name(name);
            plan.push({ path: source, name, target: FileKitStore.child(FileKitStore.parent(source), name) });
          }
          const changed = plan.filter((item) => item.path !== item.target);
          if (!changed.length) throw new Error("kit_bulk_no_change");
          const moved = view.entries.filter((entry) => changed.some((item) => within(entry.path, item.path)));
          const old = new Set(moved.map((entry) => entry.path));
          const taken = new Set(view.entries.filter((entry) => !old.has(entry.path)).map((entry) => entry.path));
          for (const item of changed) for (const entry of moved) {
            if (!within(entry.path, item.path)) continue;
            const target = item.target + entry.path.slice(item.path.length);
            if (taken.has(target)) throw new Error("kit_err_exists");
            taken.add(target);
          }
          fill(preview, ...plan.map((item) => h("div", { class: "kit-bulk-row" },
            h("span", {}, item.path), h("span", { "aria-hidden": "true" }, " → "), h("strong", {}, item.target))));
          submit.disabled = false;
        } catch (error) {
          const key = error && error.message;
          warning.textContent = has(key) ? t(key) : message(error);
          warning.hidden = false; submit.disabled = true;
          fill(preview);
        }
      };
      for (const input of [modeInput, value, find, start]) { input.addEventListener("input", update); input.addEventListener("change", update); }
      openSheet(t("kit_bulk_title"), [h("p", { class: "muted small" }, t("kit_bulk_hint", { n: roots.length })),
        h("label", { class: "field-label" }, t("kit_bulk_mode")), modeInput, findBox, valueLabel, value, startBox,
        h("h3", {}, t("kit_bulk_preview")), preview, warning, submit]);
      update();
    };

    const drawSelection = () => {
      const members = view.entries.filter((entry) => FileKitStore.parent(entry.path) === folder);
      const selectedHere = members.filter((entry) => selected.has(entry.path)).length;
      const check = h("input", { type: "checkbox", checked: !!members.length && selectedHere === members.length,
        disabled: busy || !members.length, "data-kit-action": "select-folder", "aria-label": t("kit_select_folder"),
        onchange: (event) => {
          for (const entry of members) { if (event.target.checked) selected.add(entry.path); else selected.delete(entry.path); }
          drawTree(); drawSelection(); drawTools();
        } });
      check.indeterminate = selectedHere > 0 && selectedHere < members.length;
      fill(selectionBar, h("label", { class: "kit-select-all" }, check, h("span", {}, t("kit_select_folder"))),
        actionButton("kit_clear_selection", "close", () => { selected.clear(); drawTree(); drawSelection(); drawTools(); }, busy || !selected.size, "clear-selection"),
        h("small", { class: "muted" }, t("kit_selection_scope")));
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
          actionButton("kit_rename", "notes", renameDialog, busy || !any, "rename"),
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
          actionButton("kit_join_selected", "join", () => {
            const entries = selectedEntries();
            if (!entries.length || entries.some((entry) => entry.type !== "file")) { toast(t("kit_select_file"), "bad"); return; }
            if (mode === "remote") joinRemoteDialog();
            else showJoin(entries.map((entry) => entry.path));
          }, busy || !any, "join-selected")),
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
            drawSelection(); drawTools(); event.currentTarget.closest(".kit-row").classList.toggle("selected", event.target.checked);
          } });
          const toggle = isDir ? h("button", { class: "kit-toggle", type: "button", "aria-label": `${t(open ? "kit_collapse" : "kit_expand")}: ${name}`, "aria-expanded": String(open), onclick: () => {
            if (open) expanded.delete(entry.path); else expanded.add(entry.path); drawTree();
          } }, icon(open ? "down" : "next")) : h("span", { class: "kit-toggle placeholder" });
          const openItem = h("button", { class: "kit-item", type: "button", "data-kit-open": entry.path, onclick: () => {
            if (isDir) { folder = entry.path; expanded.add(entry.path); draw(); }
            else showFile(entry);
          } }, icon(isDir ? "folder" : "file"), h("span", { class: "kit-item-name" }, name),
          isDir ? null : h("small", { class: "muted" }, fmtSize(entry.size || 0)));
          nodes.push(h("div", { class: `kit-row${selected.has(entry.path) ? " selected" : ""}${folder === entry.path ? " current" : ""}`,
            "data-kit-path": entry.path, style: `padding-inline-start:${Math.min(depth, 12) * 16 + 5}px` }, toggle, check, openItem));
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
      drawCrumbs(); drawTree(); drawSelection(); drawTools();
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

    const chooseRar = (listing, args, sealedPass) => {
      if (!listing || !Array.isArray(listing.entries) || typeof listing.version !== "string") throw new Error("unsafe_listing");
      const entries = listing.entries.filter((item) => item && item.type === "file" && typeof item.path === "string");
      if (!entries.length) { toast(t("kit_rar_no_files"), "bad"); return; }
      const boxes = [];
      const list = h("div", { class: "kit-rar-list", tabindex: "0", role: "group", "aria-label": t("kit_rar_choose") });
      const all = h("input", { type: "checkbox", "data-kit-action": "rar-select-all" });
      const go = h("button", { class: "btn primary wide", type: "button", disabled: true, "data-kit-action": "rar-extract-selected" }, t("kit_rar_extract_selected"));
      const update = () => {
        const count = boxes.filter((item) => item.box.checked).length;
        all.checked = count === boxes.length;
        all.indeterminate = count > 0 && count < boxes.length;
        go.disabled = count === 0;
      };
      for (const entry of entries) {
        const box = h("input", { type: "checkbox", "data-rar-member": entry.path, onchange: update });
        boxes.push({ box, path: entry.path });
        add(list, h("label", { class: "kit-rar-row" }, box, h("span", {}, entry.path), h("small", {}, fmtSize(entry.size || 0))));
      }
      all.addEventListener("change", () => { for (const item of boxes) item.box.checked = all.checked; update(); });
      go.addEventListener("click", () => {
        const members = boxes.filter((item) => item.box.checked).map((item) => item.path);
        if (!members.length) { toast(t("kit_nothing"), "bad"); return; }
        if (members.length > 1000) { toast(t("kit_rar_max"), "bad"); return; }
        let sealed;
        try { sealed = FileKitCrypto.sealText(view.key, JSON.stringify({ members, version: listing.version })); }
        catch (error) { toast(message(error), "bad"); return; }
        closeSheet();
        work(() => remote("rar_extract", { ...args, ...(sealedPass ? { secret: sealedPass } : {}), sealed }));
      });
      openSheet(t("kit_rar_choose"), [h("p", { class: "muted small" }, t("kit_rar_hint")),
        h("p", { class: "muted small" }, t("kit_rar_password_check")),
        listing.solid ? h("p", { class: "note warn" }, t("kit_rar_solid")) : null,
        h("label", { class: "kit-select-all" }, all, t("kit_rar_select_all")), list,
        h("p", { class: "muted small" }, t("kit_rar_resource_hint")), go]);
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
      const submit = h("button", { class: "btn primary wide", "data-kit-action": "run-archive", onclick: async () => {
        const name = filename.value.trim(), amount = Number(volume.value), chosen = first.value;
        let pass = password.value;
        try {
          FileKitStore.name(name);
          if (/[\u0000\r\n]/.test(pass) || pass.length > 1024) throw new Error("invalid_password");
          if (!Number.isInteger(amount) || amount < 0 || amount > 512) throw new Error("invalid_volume");
        } catch (error) { toast(message(error), "bad"); return; }
        // Raw mail-sized fragments must be joined before the native reader;
        // genuine RAR volumes named .part1.rar remain separate inputs.
        if (selectedMode === "remote" && action === "extract" && /\.rar\.\d{3}$/i.test(chosen)) {
          toast(t("kit_rar_join_first"), "bad"); return;
        }
        // The native remote RAR reader lists the contents first, then asks
        // which entries to extract. Member names and selection use envelopes.
        if (selectedMode === "remote" && action === "extract" && /\.rar$/i.test(chosen)) {
          if (!view.rarNative) { toast(t("kit_rar_unavailable"), "bad"); return; }
          if (paths.some((path) => !byPath(path) || byPath(path).type !== "file")) { toast(t("kit_rar_only_files"), "bad"); return; }
          let sealedPass = null;
          try { if (pass) sealedPass = FileKitCrypto.seal(view.key, pass); }
          catch (error) { toast(message(error), "bad"); return; }
          password.value = ""; pass = "";
          closeSheet();
          let listing = null;
          const args = { paths, first: chosen, folder: destination, name };
          const ok = await work(async () => {
            const reply = FileKitCrypto.replyKey();
            try {
              const result = await remote("rar_list", { paths, first: chosen, replyKey: reply.publicKey,
                ...(sealedPass ? { secret: sealedPass } : {}) });
              const raw = FileKitCrypto.openReply(reply, result.sealed);
              try { listing = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(raw)); }
              finally { raw.fill(0); }
            } finally { reply.secretKey.fill(0); }
          }, false, false, "");
          if (ok && isHere() && mode === selectedMode) {
            try { chooseRar(listing, args, sealedPass); }
            catch (error) { toast(message(error), "bad"); }
          }
          return;
        }
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
      const labelSubmit = () => { submit.textContent = t(selectedMode === "remote" && action === "extract" && /\.rar$/i.test(first.value) ? "kit_rar_list" : "kit_run"); };
      first.addEventListener("change", labelSubmit);
      labelSubmit();
      openSheet(t(action === "create" ? "kit_create" : "kit_extract"), [
        action === "create" ? [h("label", { class: "field-label" }, t("kit_format")), format] : [h("label", { class: "field-label" }, t("kit_first")), first],
        h("label", { class: "field-label" }, t(action === "create" ? "kit_archive_name" : "kit_extract_name")), filename,
        h("label", { class: "field-label" }, t("kit_password")), password, h("p", { class: "muted small" }, t("kit_password_hint")),
        action === "create" ? [h("label", { class: "field-label" }, t("kit_volume")), volume] : null,
        selectedMode === "local" ? [h("label", { class: "field-label" }, t("kit_memory")), memory, h("p", { class: "muted small" }, t("kit_memory_hint"))]
          : h("p", { class: "muted small" }, t("kit_server_engine")),
        selectedMode === "remote" && action === "extract" ? h("p", { class: "muted small" }, t("kit_rar_resource_hint")) : null,
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
        h("button", { class: "chip action", "data-kit-action": "open-join", onclick: () => showJoin() }, icon("join"), t("kit_join"))),
      modeControl, note, importHint, status,
      card(h("div", { class: "row between" }, h("h3", {}, t("kit_tree")), h("small", { class: "muted" }, t("kit_folder"))),
        crumbs, selectionBar, h("p", { class: "muted small kit-scroll-hint" }, t("kit_scroll_hint")), tree, tools),
      card(h("h3", {}, t("kit_preview")), preview),
      card(h("h3", {}, t("kit_archive")), h("p", { class: "muted small" }, t("kit_archive_hint")),
        h("div", { class: "kit-actions" }, actionButton("kit_create", "join", () => archiveDialog("create"), false, "archive-create"),
          actionButton("kit_extract", "open", () => archiveDialog("extract"), false, "archive-extract")),
        h("p", { class: "muted small" }, t("kit_compat"))),
      joinPanel);
    joiner = FileKitJoin.mount(joinBody, { getFolder: () => folder, canSave: () => mode === "local", onSaved: () => refresh() });
    draw();
    refresh().then(() => {
      if (!isHere()) return;
      if (options.jobId) importJobDialog(options.jobId, options.jobName);
      else if (options.openJoin) showJoin();
    }).catch(() => {});
  }

  return { render, stop };
})();
