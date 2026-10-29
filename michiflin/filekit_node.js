"use strict";

// This adapter runs only inside a short-lived, networkless Linux sandbox. It
// exposes a virtual Worker environment to the same WebAssembly worker used on
// the phone. Passwords arrive on stdin, never in argv, environment or logs.
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { webcrypto } = require("node:crypto");
const { performance } = require("node:perf_hooks");
const ROOT = "/work";

function safeRelative(value) {
  if (typeof value !== "string" || !value || value.startsWith("/") || value.includes("\\") || /^[A-Za-z]:/.test(value)) throw Error("unsafe_path");
  if (value.split("/").some((part) => !part || part === "." || part === ".." || /[\u0000-\u001f\u007f]/.test(part) || Buffer.byteLength(part) > 255)) throw Error("unsafe_path");
  return value;
}

try {
  const request = JSON.parse(fs.readFileSync(0, "utf8"));
  const job = { ...request.job };
  job.files = request.files.map((item) => {
    const relative = safeRelative(item.path);
    if (item.type === "dir") return { path: relative, type: "dir" };
    if (!/^\d{1,9}$/.test(item.source)) throw Error("invalid_file");
    const source = path.join(ROOT, "input", item.source);
    return { path: relative, type: "file", blob: new Blob([fs.readFileSync(source)]) };
  });
  const context = {
    Blob, Uint8Array, ArrayBuffer, TextDecoder, TextEncoder, WebAssembly, performance, crypto: webcrypto,
    WorkerGlobalScope: function WorkerGlobalScope() {}, location: { href: "file:///work/filekit-worker.js" },
    setTimeout, clearTimeout, console: { log() {}, error() {}, warn() {} },
  };
  context.self = context;
  const sandbox = vm.createContext(context);
  context.importScripts = (name) => {
    if (name !== "vendor/7zip/sevenzip-wasm.js") throw Error("engine_unavailable");
    vm.runInContext(fs.readFileSync(path.join(ROOT, "sevenzip-wasm.js"), "utf8"), sandbox, { filename: "sevenzip-wasm.js" });
  };
  context.fetch = async (name) => {
    if (name !== "vendor/7zip/sevenzip-wasm.wasm") throw Error("engine_unavailable");
    const data = fs.readFileSync(path.join(ROOT, "sevenzip-wasm.wasm"));
    return { ok: true, arrayBuffer: async () => data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength) };
  };
  context.postMessage = (message) => {
    if (message.ok && message.result && message.result.files) {
      const targetRoot = path.join(ROOT, "result");
      fs.mkdirSync(targetRoot, { recursive: true, mode: 0o700 });
      const visible = [];
      for (const item of message.result.files) {
        const relative = safeRelative(item.path);
        const target = path.join(targetRoot, relative);
        if (!target.startsWith(targetRoot + path.sep)) throw Error("unsafe_path");
        if (item.type === "dir") {
          fs.mkdirSync(target, { recursive: true, mode: 0o700 });
          visible.push({ path: relative, type: "dir" });
        } else {
          fs.mkdirSync(path.dirname(target), { recursive: true, mode: 0o700 });
          const data = Buffer.from(item.bytes);
          fs.writeFileSync(target, data, { flag: "wx", mode: 0o600 });
          visible.push({ path: relative, type: "file", size: data.length });
        }
      }
      message.result.files = visible;
    }
    const reply = JSON.stringify(message);
    if (Buffer.byteLength(reply) > 8 * 1024 * 1024) throw Error("listing_limit");
    process.stdout.write(reply);
  };
  vm.runInContext(fs.readFileSync(path.join(ROOT, "filekit-worker.js"), "utf8"), sandbox, { filename: "filekit-worker.js" });
  context.onmessage({ data: job });
} catch (error) {
  const code = error && /^[a-z_]+$/.test(error.message || "") ? error.message : "engine_failed";
  process.stdout.write(JSON.stringify({ ok: false, code }));
}
