"use strict";

// Offline copies live in the current WebXDC instance, not in the phone's file system.
  // File contents are immutable, chunked byte arrays; history snapshots metadata and IDs.
// Bytes are staged first; publication and undo/redo commit atomically. This also works
// in WebKit modes that reject storing Blob/File objects directly in IndexedDB.
const FileKitStore = (() => {
  const DB_NAME = "michiflin-filekit-v1";
  let opened = null;

  function request(req) {
    return new Promise((resolve, reject) => {
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error || new Error("storage"));
    });
  }

  function done(tx) {
    return new Promise((resolve, reject) => {
      let cause = null;
      tx.oncomplete = () => resolve();
      tx.onerror = (event) => { cause = event.target.error || cause; };
      tx.onabort = () => reject(tx.error || cause || new Error("storage"));
    });
  }

  function db() {
    if (!opened) opened = new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, 1);
      req.onupgradeneeded = () => {
        const database = req.result;
        database.createObjectStore("entries", { keyPath: "path" });
        database.createObjectStore("blobs");
        database.createObjectStore("meta");
      };
      req.onsuccess = () => {
        const database = req.result;
        database.onversionchange = () => { database.close(); opened = null; };
        resolve(database);
      };
      req.onerror = () => { opened = null; reject(req.error || new Error("storage")); };
      req.onblocked = () => { opened = null; reject(new Error("storage_blocked")); };
    });
    return opened;
  }

  function name(value) {
    if (typeof value !== "string" || !value || value === "." || value === ".." || /[\/\\\u0000-\u001f\u007f]/.test(value) || new TextEncoder().encode(value).length > 255) {
      throw new Error("invalid_name");
    }
    return value;
  }

  function path(value, rootAllowed = true) {
    if (typeof value !== "string") throw new Error("invalid_path");
    if (value === "" && rootAllowed) return "";
    if (!value.startsWith("/") || value.endsWith("/") || value.includes("//")) throw new Error("invalid_path");
    value.slice(1).split("/").forEach(name);
    return value;
  }

  const parent = (value) => value.slice(0, value.lastIndexOf("/"));
  const base = (value) => value.slice(value.lastIndexOf("/") + 1);
  const child = (folder, filename) => path(folder) + "/" + name(filename);
  const within = (value, root) => value === root || value.startsWith(root + "/");
  const clone = (entry) => ({ ...entry });
  const key = () => {
    if (!globalThis.crypto || typeof crypto.getRandomValues !== "function") throw new Error("secure_random_unavailable");
    if (crypto.randomUUID) return crypto.randomUUID();
    return [...crypto.getRandomValues(new Uint8Array(16))].map((value) => value.toString(16).padStart(2, "0")).join("");
  };
  const CHUNK_BYTES = 4 * 1024 * 1024;
  const chunkKey = (id, index) => `${id}:${String(index).padStart(8, "0")}`;

  function bytes(blob) {
    if (blob.arrayBuffer) return blob.arrayBuffer();
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(reader.error || new Error("read_failed"));
      reader.readAsArrayBuffer(blob);
    });
  }

  async function discardStage(stage) {
    const database = await db();
    const tx = database.transaction("blobs", "readwrite");
    const finished = done(tx);
    const store = tx.objectStore("blobs");
    store.delete(stage.id);
    for (let index = 0; index < stage.count; index += 1) store.delete(chunkKey(stage.id, index));
    await finished;
  }

  async function stageBlob(blob) {
    const database = await db();
    const stage = { id: key(), state: "staging", count: 0, size: blob.size, mime: blob.type || "application/octet-stream" };
    try {
      {
        const tx = database.transaction("blobs", "readwrite");
        const finished = done(tx);
        tx.objectStore("blobs").put(stage, stage.id);
        await finished;
      }
      for (let offset = 0; offset < blob.size; offset += CHUNK_BYTES) {
        const data = await bytes(blob.slice(offset, Math.min(offset + CHUNK_BYTES, blob.size)));
        const tx = database.transaction("blobs", "readwrite");
        const finished = done(tx);
        const store = tx.objectStore("blobs");
        store.put(data, chunkKey(stage.id, stage.count));
        stage.count += 1;
        store.put(stage, stage.id);
        await finished;
      }
      return stage;
    } catch (error) {
      await discardStage(stage).catch(() => {});
      throw error;
    }
  }

  function lookup(all, value) {
    if (value === "") return { path: "", type: "dir" };
    return all.find((item) => item.path === value);
  }

  function requireDir(all, value) {
    const item = lookup(all, path(value));
    if (!item || item.type !== "dir") throw new Error("folder_missing");
  }

  function roots(values) {
    if (!Array.isArray(values) || !values.length) throw new Error("nothing_selected");
    const sorted = [...new Set(values.map((value) => path(value, false)))].sort();
    return sorted.filter((value, index) => !sorted.slice(0, index).some((other) => within(value, other)));
  }

  async function view() {
    const database = await db();
    const tx = database.transaction(["entries", "meta"], "readonly");
    const finished = done(tx);
    const [entries, saved] = await Promise.all([request(tx.objectStore("entries").getAll()), request(tx.objectStore("meta").get("history"))]);
    await finished;
    const history = saved || { past: [], future: [] };
    return { entries, canUndo: !!history.past.length, canRedo: !!history.future.length };
  }

  async function get(value) {
    value = path(value, false);
    const database = await db();
    const tx = database.transaction(["entries", "blobs"], "readonly");
    const finished = done(tx);
    const item = await request(tx.objectStore("entries").get(value));
    if (!item || item.type !== "file") { await finished; throw new Error("file_missing"); }
    const store = tx.objectStore("blobs");
    const manifest = await request(store.get(item.blob));
    if (!manifest || manifest.state !== "committed" || !Number.isSafeInteger(manifest.count) || manifest.count < 0) {
      await finished;
      throw new Error("content_missing");
    }
    const chunks = await Promise.all(Array.from({ length: manifest.count }, (_, index) => request(store.get(chunkKey(item.blob, index)))));
    await finished;
    if (chunks.some((part) => !(part instanceof ArrayBuffer)) || chunks.reduce((sum, part) => sum + part.byteLength, 0) !== item.size) {
      throw new Error("content_missing");
    }
    return { entry: item, blob: new Blob(chunks, { type: manifest.mime }) };
  }

  async function mutate(label, makePlan) {
    const database = await db();
    const tx = database.transaction(["entries", "blobs", "meta"], "readwrite");
    const finished = done(tx);
    try {
      const store = tx.objectStore("entries");
      const [all, saved] = await Promise.all([request(store.getAll()), request(tx.objectStore("meta").get("history"))]);
      const history = saved || { past: [], future: [] };
      const plan = makePlan(all);
      if (!plan || (!plan.before.length && !plan.after.length)) { await finished; return; }
      const touched = new Set([...plan.before, ...plan.after].map((item) => item.path));
      for (const value of touched) store.delete(value);
      for (const item of plan.after) store.put(item);
      for (const stage of plan.staged || []) tx.objectStore("blobs").put({ ...stage, state: "committed" }, stage.id);
      history.past.push({ label, before: plan.before.map(clone), after: plan.after.map(clone) });
      history.future = [];
      tx.objectStore("meta").put(history, "history");
      await finished;
    } catch (error) {
      try { tx.abort(); } catch (_) { /* Transaction already finished. */ }
      await finished.catch(() => {});
      throw error;
    }
  }

  async function mkdir(folder, filename) {
    const target = child(folder, filename);
    return mutate("mkdir", (all) => {
      requireDir(all, folder);
      if (lookup(all, target)) throw new Error("exists");
      return { before: [], after: [{ path: target, type: "dir", mtime: Date.now() }] };
    });
  }

  // items are paths relative to folder. Missing parent directories are created atomically.
  async function put(folder, items) {
    folder = path(folder);
    if (!Array.isArray(items) || !items.length) throw new Error("nothing_selected");
    const prepared = items.map((item) => {
      if (!item || typeof item.path !== "string" || (item.type !== "dir" && !(item.blob instanceof Blob))) throw new Error("invalid_file");
      const parts = item.path.split("/");
      parts.forEach(name);
      return { parts, blob: item.blob, type: item.type === "dir" ? "dir" : "file", mtime: Number(item.mtime) || Date.now() };
    });
    const staged = [];
    try {
      for (const item of prepared) if (item.type === "file") { item.stage = await stageBlob(item.blob); staged.push(item.stage); }
      await mutate("import", (all) => {
        requireDir(all, folder);
        const known = new Map(all.map((item) => [item.path, item]));
        const after = [];
        for (const item of prepared) {
          let at = folder;
          for (const part of item.parts.slice(0, -1)) {
            at = child(at, part);
            const present = known.get(at);
            if (present && present.type !== "dir") throw new Error("exists");
            if (!present) {
              const directory = { path: at, type: "dir", mtime: item.mtime };
              known.set(at, directory); after.push(directory);
            }
          }
          const target = child(at, item.parts[item.parts.length - 1]);
          const present = known.get(target);
          if (present) {
            // A directory entry may follow a child that already created it in this batch.
            if (item.type === "dir" && present.type === "dir" && after.includes(present)) continue;
            throw new Error("exists");
          }
          const entry = item.type === "dir" ? { path: target, type: "dir", mtime: item.mtime } :
            { path: target, type: "file", blob: item.stage.id, size: item.blob.size, mime: item.stage.mime, mtime: item.mtime };
          known.set(target, entry); after.push(entry);
        }
        return { before: [], after, staged };
      });
    } catch (error) {
      await Promise.all(staged.map((stage) => discardStage(stage).catch(() => {})));
      throw error;
    }
  }

  async function write(value, blob, expectedBlob = null) {
    value = path(value, false);
    if (!(blob instanceof Blob)) throw new Error("invalid_file");
    const stage = await stageBlob(blob);
    try {
      await mutate("edit", (all) => {
        const old = lookup(all, value);
        if (!old || old.type !== "file") throw new Error("file_missing");
        if (expectedBlob !== null && old.blob !== expectedBlob) throw new Error("edit_conflict");
        const next = { ...old, blob: stage.id, size: blob.size, mime: stage.mime, mtime: Date.now() };
        return { before: [old], after: [next], staged: [stage] };
      });
    } catch (error) {
      await discardStage(stage).catch(() => {});
      throw error;
    }
  }

  async function rename(value, filename) {
    value = path(value, false);
    const target = child(parent(value), filename);
    if (target === value) return;
    return mutate("rename", (all) => relocate(all, [value], parent(value), false, { [value]: target }));
  }

  function relocate(all, values, folder, copy, targets = {}) {
    requireDir(all, folder);
    const selected = roots(values);
    const taken = new Set(all.map((item) => item.path));
    const before = [];
    const after = [];
    for (const source of selected) {
      if (!lookup(all, source)) throw new Error("missing");
      const target = targets[source] || child(folder, base(source));
      if (target === source || within(target, source)) throw new Error("inside_source");
      const subtree = all.filter((item) => within(item.path, source));
      for (const item of subtree) {
        const nextPath = target + item.path.slice(source.length);
        path(nextPath, false);
        if (taken.has(nextPath)) throw new Error("exists");
        taken.add(nextPath);
        after.push({ ...item, path: nextPath });
      }
      if (!copy) before.push(...subtree);
    }
    return { before, after };
  }

  async function transfer(values, folder, copy) {
    folder = path(folder);
    return mutate(copy ? "copy" : "move", (all) => relocate(all, values, folder, !!copy));
  }

  async function remove(values) {
    const selected = roots(values);
    return mutate("delete", (all) => {
      for (const value of selected) if (!lookup(all, value)) throw new Error("missing");
      return { before: all.filter((item) => selected.some((value) => within(item.path, value))), after: [] };
    });
  }

  async function travel(reverse) {
    const database = await db();
    const tx = database.transaction(["entries", "meta"], "readwrite");
    const finished = done(tx);
    try {
      const meta = tx.objectStore("meta");
      const history = await request(meta.get("history")) || { past: [], future: [] };
      const from = reverse ? history.past : history.future;
      const to = reverse ? history.future : history.past;
      if (!from.length) { await finished; return false; }
      const event = from.pop();
      const store = tx.objectStore("entries");
      for (const value of new Set([...event.before, ...event.after].map((item) => item.path))) store.delete(value);
      for (const item of reverse ? event.before : event.after) store.put(item);
      to.push(event);
      meta.put(history, "history");
      await finished;
      return true;
    } catch (error) {
      try { tx.abort(); } catch (_) { /* Transaction already finished. */ }
      await finished.catch(() => {});
      throw error;
    }
  }

  // Explicit history cleanup only: removed versions become unrecoverable.
  async function clearHistory() {
    const database = await db();
    const tx = database.transaction(["entries", "blobs", "meta"], "readwrite");
    const finished = done(tx);
    try {
      const blobs = tx.objectStore("blobs");
      const [all, blobKeys] = await Promise.all([request(tx.objectStore("entries").getAll()), request(blobs.getAllKeys())]);
      const ids = blobKeys.filter((value) => typeof value === "string" && !value.includes(":"));
      const manifests = await Promise.all(ids.map((id) => request(blobs.get(id))));
      const live = new Set(all.filter((item) => item.type === "file").map((item) => item.blob));
      // Another open window may still be importing. Never collect its staged data.
      manifests.forEach((item) => { if (item && item.state === "staging") live.add(item.id); });
      for (const value of blobKeys) if (typeof value === "string" && !live.has(value.split(":", 1)[0])) blobs.delete(value);
      tx.objectStore("meta").put({ past: [], future: [] }, "history");
      await finished;
    } catch (error) {
      try { tx.abort(); } catch (_) { /* Transaction already finished. */ }
      await finished.catch(() => {});
      throw error;
    }
  }

  return { view, get, mkdir, put, write, rename, transfer, remove, undo: () => travel(true), redo: () => travel(false), clearHistory, name, path, parent, base, child };
})();
