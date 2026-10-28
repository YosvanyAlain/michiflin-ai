"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const project = {
  files: [{ path: "app.py", text: "def value():\n    return 1\n" }],
  history: [{ files: [{ path: "app.py", text: "def value():\n    return 0\n" }], summary: "old", name: "Demo" }],
  lastChanges: [], summary: "new", name: "Demo", rid: "",
};
const store = {
  get(_key, fallback) { return project || fallback; },
  set(_key, value) { Object.assign(project, value); },
};
const context = {
  store,
  bridge: { handlers: {}, pending: () => ({}), request: () => ({ id: "test" }) },
  bus: { emit() {} },
  VIEWS: {},
  t: (key) => key,
  toast() {},
  now: () => 1,
  h: () => ({}),
  openSheet() {},
};
vm.createContext(context);
vm.runInContext(fs.readFileSync("webxdc/coder.js", "utf8"), context, { filename: "coder.js" });
const before = [{ path: "app.py", text: "one\ntwo\n" }];
const after = [{ path: "app.py", text: "one\nthree\n" }, { path: "new.txt", text: "hello" }];
const changes = context.coderChanges(before, after);
assert.equal(JSON.stringify(changes.map((item) => [item.path, item.kind])), JSON.stringify([["app.py", "changed"], ["new.txt", "added"]]));
assert.match(context.coderDiffText(changes[0]), /-two/);
assert.match(context.coderDiffText(changes[0]), /\+three/);
context.coderUndo();
assert.equal(project.files[0].text, "def value():\n    return 0\n");
assert.equal(project.history.length, 0);
Object.assign(project, { files: [], history: [], lastChanges: [], summary: "", name: "", rid: "initial" });
context.bridge.handlers.code({ files: [{ path: "first.txt", text: "first" }], summary: "first" }, null, null, "initial");
assert.equal(project.history.length, 1);
assert.equal(project.history[0].files.length, 0);
context.coderUndo();
assert.equal(project.files.length, 0);
console.log("coder history ok");
