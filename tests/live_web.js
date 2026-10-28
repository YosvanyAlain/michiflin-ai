// Deterministic tests for browser startup deadlines and realtime cleanup.
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { TextDecoder, TextEncoder } = require("util");
const { webcrypto } = require("crypto");

function boot() {
  let nextId = 0;
  const timers = new Map(), sent = [];
  let listener = null, leaves = 0;
  const channel = {
    setListener(fn) { listener = fn; },
    send(data) { sent.push(JSON.parse(new TextDecoder().decode(data))); },
    leave() { leaves += 1; },
  };
  const context = {
    console, TextDecoder, TextEncoder, Uint8Array, crypto: webcrypto, VIEWS: {},
    lang: () => "en",
    setTimeout(fn, ms) { const id = ++nextId; timers.set(id, { fn, ms, interval: false }); return id; },
    setInterval(fn, ms) { const id = ++nextId; timers.set(id, { fn, ms, interval: true }); return id; },
    clearTimeout(id) { timers.delete(id); },
    clearInterval(id) { timers.delete(id); },
  };
  context.window = context;
  context.webxdc = { joinRealtimeChannel: () => channel };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "webxdc", "live.js"), "utf8"), context);
  return {
    web: context.liveWeb, timers, sent,
    receive(data) { assert(listener); listener(new TextEncoder().encode(JSON.stringify(data))); },
    leaves() { return leaves; },
    fire(ms) {
      const item = [...timers.entries()].find(([, value]) => value.ms === ms);
      assert(item, `timer for ${ms}ms is not scheduled`);
      const [id, value] = item;
      if (!value.interval) timers.delete(id);
      value.fn();
    },
  };
}

// When no peer answers, the actual view's error handler calls stop().  Verify
// that it emits a visible timeout, stops retrying and leaves the channel.
{
  const test = boot(), messages = [];
  test.web.start("https://example.org/", (message) => {
    messages.push(message);
    if (message.t === "error") test.web.stop();
  });
  assert.strictEqual(test.sent.length, 1);
  assert.strictEqual(test.sent[0].t, "start");
  test.fire(3500);
  assert.strictEqual(test.sent.filter((item) => item.t === "start").length, 2);
  test.fire(110000);
  assert.strictEqual(messages.length, 1);
  assert.strictEqual(messages[0].error, "timeout");
  assert.strictEqual(test.web.ready, false);
  assert.strictEqual(test.web.channel, null);
  assert.strictEqual(test.timers.size, 0);
  assert.strictEqual(test.leaves(), 1);
  assert.strictEqual(test.sent[test.sent.length - 1].t, "stop");
}

// A genuine first frame cancels startup retry/deadline; a foreign session and
// a malformed packet must never make the browser appear ready.
{
  const test = boot(), messages = [];
  test.web.start("https://example.org/", (message) => messages.push(message));
  const sid = test.web.sid;
  test.receive({ v: 1, sid: "0".repeat(32), t: "frame", seq: 0 });
  assert.strictEqual(test.web.ready, false);
  test.receive({ v: 1, sid, t: "opening" });
  assert.strictEqual(test.web.ready, false);
  test.receive({ v: 1, sid, t: "frame", seq: 0, image: "image" });
  assert.strictEqual(test.web.ready, true);
  assert.strictEqual(test.timers.size, 0);
  assert.strictEqual(messages[messages.length - 1].t, "frame");
  test.web.stop();
  assert.strictEqual(test.leaves(), 1);
}

console.log("live web startup ok");
