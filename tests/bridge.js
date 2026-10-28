// Bridge checks in plain Node: resends with the same id, acks, and chunked answers that survive an app reopen.
"use strict";
const fs = require("fs");
const path = require("path");
const assert = require("assert");

function boot(storage, sent) {
  const listeners = {};
  const ctx = {
    console, Blob, setTimeout, clearTimeout, setInterval: () => 0, Date, Math, JSON, Promise, Map, Set, Object, Array, String, Number,
    localStorage: { getItem: (k) => (k in storage ? storage[k] : null), setItem: (k, v) => { storage[k] = String(v); }, removeItem: (k) => { delete storage[k]; } },
    navigator: { language: "es" },
    document: { addEventListener: (n, f) => { listeners[n] = f; }, documentElement: { lang: "es", dataset: {} }, visibilityState: "visible",
      createElement: () => ({ style: {}, setAttribute() {}, append() {}, classList: { add() {}, remove() {}, toggle() {} } }), querySelector: () => null, body: null },
    indexedDB: undefined,
  };
  ctx.window = ctx;
  ctx.window.webxdc = { sendUpdate: (update) => sent.push(update.payload), selfAddr: "me@x", selfName: "Me" };
  ctx.window.addEventListener = () => {};
  const vm = require("vm");
  vm.createContext(ctx);
  for (const file of ["i18n.js", "core.js"]) vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "webxdc", file), "utf8"), ctx, { filename: file });
  return vm.runInContext("bridge", ctx);
}

const storage = {};
let sent = [];
let bridge = boot(storage, sent);

// 1. a request is resent with the same id and an increasing retry counter
const { id } = bridge.request("search", { q: "gatos" });
assert.strictEqual(sent.length, 1);
const pending = bridge.pending();
pending[id].sent -= 50000;
bridge.save(pending);
bridge.tick();
assert.strictEqual(sent.length, 2);
assert.strictEqual(sent[1].id, id);
assert.strictEqual(sent[1].retry, 1);

// 2. an ack stops the fast resends
bridge.receive({ serial: 1, payload: { t: "ack", re: id } });
const after = bridge.pending();
after[id].sent -= 200000;
bridge.save(after);
bridge.tick();
assert.strictEqual(sent.length, 2, "acknowledged requests wait instead of resending");

// 3. half of a chunked answer arrives, the app closes, reopens and replays from the floor
const answer = JSON.stringify({ t: "res", re: id, ok: true, data: { text: "y".repeat(50) } });
const half = Math.ceil(answer.length / 2);
bridge.receive({ serial: 2, payload: { t: "chunk", re: id, i: 0, n: 2, s: answer.slice(0, half) } });
assert.ok(bridge.pending()[id], "still waiting for the second chunk");
assert.strictEqual(bridge.startSerial(), 1, "listener restarts before the first chunk");
sent = [];
bridge = boot(storage, sent); // reopen: the in-memory chunks are gone
let pushes = 0;
bridge.pushes.job = () => { pushes += 1; };
let settled = null;
bridge.handlers.search = (data) => { settled = data; };
const from = bridge.startSerial();
const log = [
  { serial: 1, payload: { t: "ack", re: id } },
  { serial: 2, payload: { t: "chunk", re: id, i: 0, n: 2, s: answer.slice(0, half) } },
  { serial: 3, payload: { t: "push", kind: "job", data: {} } },
  { serial: 4, payload: { t: "chunk", re: id, i: 1, n: 2, s: answer.slice(half) } },
];
log.filter((u) => u.serial > from).forEach((u) => bridge.receive(u));
assert.deepStrictEqual(settled, { text: "y".repeat(50) });
assert.strictEqual(pushes, 1, "new pushes are delivered once");
assert.strictEqual(Object.keys(bridge.pending()).length, 0);
assert.strictEqual(bridge.startSerial(), 4, "floor dropped once the answer is complete");

// 4. repair resends what can be resent and gives up on the rest
const second = bridge.request("rates", {});
const pend = bridge.pending();
pend[second.id].p = null;
bridge.save(pend);
const third = bridge.request("search", { q: "x" });
let failed = 0;
second.promise.catch(() => { failed += 1; });
sent = sent.slice(0, 0);
bridge.repair();
setTimeout(() => {
  assert.strictEqual(failed, 1);
  assert.ok(bridge.pending()[third.id], "resendable request kept");
  console.log("bridge ok");
}, 10);
