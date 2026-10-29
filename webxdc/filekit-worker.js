"use strict";

// One isolated virtual filesystem per request. Nothing extracted here is written
// to the phone or server; the caller publishes checked results atomically.
importScripts("vendor/7zip/sevenzip-wasm.js");

(() => {
  const fail = (code) => { throw new Error(code); };
  const validPath = (value) => {
    if (typeof value !== "string" || !value || value.startsWith("/") || value.includes("\\") || /^[A-Za-z]:/.test(value) || new TextEncoder().encode(value).length > 4096) fail("unsafe_path");
    const parts = value.split("/");
    if (parts.length > 64 || parts.some((part) => !part || part === "." || part === ".." || /[\u0000-\u001f\u007f]/.test(part) || new TextEncoder().encode(part).length > 255)) fail("unsafe_path");
    return value;
  };

  function listEntries(text) {
    // A valid, even empty, 7-Zip listing has exactly one separator. Never
    // interpret a truncated or unexpected listing as an empty safe archive.
    const lines = text.split("\n");
    const start = lines.indexOf("----------");
    if (start < 0 || lines.indexOf("----------", start + 1) >= 0) fail("unsafe_listing");
    const entries = [];
    let row = null;
    for (const line of lines.slice(start + 1)) {
      if (!line.trim()) {
        if (row) { entries.push(row); row = null; }
        continue;
      }
      const match = /^([^\u0000-\u001f=]+) = (.*)$/.exec(line);
      if (!match) fail("unsafe_listing");
      if (!row) row = {};
      const field = match[1].trim();
      if (Object.prototype.hasOwnProperty.call(row, field)) fail("unsafe_listing");
      row[field] = match[2];
    }
    if (row) entries.push(row);
    if (entries.length > 10000) fail("listing_limit");
    let total = 0;
    const seen = new Set();
    for (const entry of entries) {
      const path = validPath(entry.Path);
      if (seen.has(path)) fail("duplicate_path");
      seen.add(path);
      if (entry["Symbolic Link"] || entry["Hard Link"] || /(?:^|\s)l[rwxstST-]{9}(?:\s|$)/.test(entry.Attributes || "")) fail("unsafe_link");
      if (!/^\d+$/.test(entry.Size || "")) fail("unsafe_listing");
      const size = Number(entry.Size);
      if (!Number.isSafeInteger(size)) fail("output_limit");
      total += size;
      if (!Number.isSafeInteger(total)) fail("output_limit");
    }
    return { entries, total };
  }

  async function run(job) {
    if (!job || !["create", "extract", "list"].includes(job.action) || !Array.isArray(job.files) || !job.files.length) fail("invalid_request");
    if (job.files.length > 10000) fail("listing_limit");
    const limit = Number(job.limit);
    if (!Number.isSafeInteger(limit) || limit <= 0) fail("invalid_limit");
    const password = typeof job.password === "string" ? job.password : "";
    if (/[\u0000\r\n]/.test(password)) fail("invalid_password");
    let inputSize = 0;
    const names = new Set();
    for (const item of job.files) {
      validPath(item.path);
      if (names.has(item.path)) fail("duplicate_path");
      names.add(item.path);
      if (item.type !== undefined && item.type !== "dir" && item.type !== "file") fail("invalid_file");
      if (item.type !== "dir") {
        if (!(item.blob instanceof Blob)) fail("invalid_file");
        inputSize += item.blob.size;
      }
      if (!Number.isSafeInteger(inputSize) || inputSize > limit) fail("input_limit");
    }

    let lines = [], logSize = 0, logOverflow = false;
    const collect = (line) => {
      logSize += line.length + 1;
      if (logSize <= 2 * 1024 * 1024) lines.push(line);
      else logOverflow = true;
    };
    const asset = await fetch("vendor/7zip/sevenzip-wasm.wasm");
    if (!asset.ok) fail("engine_unavailable");
    const mod = await SevenZipWasm({ noInitialRun: true, wasmBinary: await asset.arrayBuffer(), print: collect, printErr: collect });
    const fs = mod.FS;
    fs.mkdir("/in"); fs.mkdir("/out");
    for (const item of job.files) {
      const target = "/in/" + item.path;
      const index = target.lastIndexOf("/");
      fs.mkdirTree(target.slice(0, index));
      if (item.type === "dir") fs.mkdirTree(target);
      else fs.writeFile(target, new Uint8Array(await item.blob.arrayBuffer()));
    }

    // MEMFS holds all bytes in the worker. Count growth even if a hostile archive
    // lies about uncompressed sizes; abort before its output can exceed the budget.
    let outputSize = 0, limitHit = false, createdNodes = 0;
    const fileOps = fs.filesystems.MEMFS.ops_table.file;
    const dirOps = fs.filesystems.MEMFS.ops_table.dir.node;
    const isOutput = (node) => {
      const target = fs.getPath(node);
      return target === "/out" || target.startsWith("/out/");
    };
    const requireOutput = (node) => { if (!isOutput(node)) fail("unsafe_path"); };
    const mknod = dirOps.mknod;
    dirOps.mknod = function (parent, name, mode, dev) {
      requireOutput(parent);
      if (++createdNodes > 10000) fail("listing_limit");
      return mknod.call(this, parent, name, mode, dev);
    };
    const rename = dirOps.rename;
    dirOps.rename = function (node, newDirectory, name) {
      requireOutput(node); requireOutput(newDirectory);
      return rename.call(this, node, newDirectory, name);
    };
    for (const method of ["unlink", "rmdir"]) {
      const original = dirOps[method];
      dirOps[method] = function (parent, name) { requireOutput(parent); return original.call(this, parent, name); };
    }
    dirOps.symlink = function () { fail("unsafe_link"); };
    const charge = (node, requested) => {
      requireOutput(node);
      const growth = Math.max(0, requested - (node.usedBytes || 0));
      if (!Number.isSafeInteger(growth) || outputSize + growth > limit) { limitHit = true; fail("output_limit"); }
      outputSize += growth;
    };
    const write = fileOps.stream.write;
    fileOps.stream.write = function (stream, buffer, offset, length, position, canOwn) {
      const pos = Number.isSafeInteger(position) ? position : stream.position;
      charge(stream.node, pos + length);
      return write.call(this, stream, buffer, offset, length, position, canOwn);
    };
    const setattr = fileOps.node.setattr;
    fileOps.node.setattr = function (node, attributes) {
      if (attributes.size !== undefined) charge(node, attributes.size);
      return setattr.call(this, node, attributes);
    };

    const execute = (args) => {
      lines = []; logSize = 0; logOverflow = false;
      let status;
      try { status = mod.callMain(args); }
      catch (error) { if (limitHit) fail("output_limit"); throw error; }
      if (limitHit) fail("output_limit");
      if (logOverflow) fail("listing_limit");
      const output = lines.join("\n");
      if (status !== 0) {
        if (/wrong password|password is incorrect|can not open encrypted/i.test(output)) fail("wrong_password");
        fail("archive_failed");
      }
      return output;
    };

    let expected = null, expectedParents = null;
    if (job.action === "create") {
      if (!["zip", "7z"].includes(job.format)) fail("unsupported_format");
      validPath(job.name);
      if (job.name.includes("/")) fail("unsafe_path");
      const args = ["a", `-t${job.format}`, "-bd", "-y"];
      if (password) {
        args.push("-p" + password);
        args.push(job.format === "7z" ? "-mhe=on" : "-mem=AES256");
      }
      const volume = Number(job.volumeBytes || 0);
      if (volume && (!Number.isSafeInteger(volume) || volume < 1)) fail("invalid_volume");
      if (volume) args.push(`-v${volume}b`);
      const tops = [...new Set(job.files.map((item) => "./" + item.path.split("/")[0]))];
      fs.chdir("/in");
      execute([...args, "/out/" + job.name, "--", ...tops]);
    } else {
      validPath(job.first);
      if (!names.has(job.first) || job.files.find((item) => item.path === job.first).type === "dir") fail("missing_archive");
      const archive = "/in/" + job.first;
      const listing = execute(["l", "-slt", "-bd", "-p" + password, archive]);
      const details = listEntries(listing);
      const entries = details.entries;
      if (details.total > limit) fail("output_limit");
      if (job.action === "list") return { entries: entries.map((entry) => ({ path: entry.Path, size: Number(entry.Size), directory: entry.Folder === "+" || /^D/.test(entry.Attributes || "") })) };
      expected = new Map(entries.map((entry) => [entry.Path, { size: Number(entry.Size), directory: entry.Folder === "+" || /^D/.test(entry.Attributes || "") }]));
      expectedParents = new Set();
      for (const entry of entries) {
        const parts = entry.Path.split("/");
        for (let i = 1; i < parts.length; i++) expectedParents.add(parts.slice(0, i).join("/"));
      }
      execute(["x", "-bd", "-y", "-p" + password, "-o/out", archive]);
    }

    const files = [], found = new Set();
    const visit = (directory, prefix = "") => {
      for (const item of fs.readdir(directory).filter((value) => value !== "." && value !== "..")) {
        const rel = validPath(prefix ? `${prefix}/${item}` : item);
        const full = `${directory}/${item}`;
        const stat = fs.lstat(full);
        if (fs.isLink(stat.mode)) fail("unsafe_link");
        const declared = expected && expected.get(rel);
        if (fs.isDir(stat.mode)) {
          if (expected && ((!declared && !expectedParents.has(rel)) || (declared && !declared.directory))) fail("unsafe_listing");
          files.push({ path: rel, type: "dir" }); visit(full, rel);
        } else if (fs.isFile(stat.mode)) {
          if (expected && (!declared || declared.directory || declared.size !== stat.size)) fail("unsafe_listing");
          const data = fs.readFile(full).slice().buffer;
          files.push({ path: rel, type: "file", bytes: data });
        } else fail("unsafe_type");
        if (declared) found.add(rel);
        if (files.length > 10000) fail("listing_limit");
      }
    };
    visit("/out");
    if (expected && found.size !== expected.size) fail("unsafe_listing");
    return { files };
  }

  self.onmessage = async (event) => {
    try {
      const result = await run(event.data);
      const transfer = (result.files || []).filter((item) => item.bytes).map((item) => item.bytes);
      self.postMessage({ ok: true, result }, transfer);
    } catch (error) {
      const code = error && typeof error.message === "string" && /^[a-z_]+$/.test(error.message) ? error.message : "engine_failed";
      self.postMessage({ ok: false, code });
    }
  };
})();
