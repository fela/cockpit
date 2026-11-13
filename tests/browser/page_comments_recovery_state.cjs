// A comments read can outlast a restart. The next successful read clears its error while
// preserving errors from comment edits and version changes. No server or account data is used.
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const folder = process.argv[2] || path.resolve(__dirname, "../../src/cockpit/static");
const tick = () => new Promise((resolve) => setImmediate(resolve));

class Element {
  constructor() { this.children = []; this.hidden = false; this.style = {}; }
  setAttribute() {}
  addEventListener() {}
  append(child) { this.children.push(child); }
  replaceChildren(...children) { this.children = children; }
  querySelector() { return null; }
  remove() {}
  get textContent() { return this.children.map((child) => child.textContent).join(""); }
  set textContent(text) { this.children = [{ textContent: text }]; }
}

function world(carried, onPlace) {
  const column = new Element();
  const failures = [];
  const marked = [];
  const loaded = [];
  const replies = [{ status: 502, data: {} }];
  const frame = { contentWindow: { postMessage: (message) => marked.push(message) } };
  const context = vm.createContext({
    Node: Element,
    document: { body: new Element(), createElement: () => new Element(), createTextNode: (text) => ({ textContent: text }) },
    addEventListener() {}, removeEventListener() {},
    Icons: { icon: () => new Element() },
    logError: (place, error) => failures.push({ place, error }),
    readThroughRestart: async () => {
      const next = replies.shift();
      assert.ok(next, "unexpected read");
      if (next instanceof Error) throw next;
      return { response: { ok: next.status === 200, status: next.status }, data: next.data };
    },
    column, frame, carried, onPlace,
    onChange: (comments) => loaded.push(comments),
  });
  context.window = context;
  for (const file of ["reply.js", "page-comments.js"]) vm.runInContext(fs.readFileSync(path.join(folder, file), "utf8"), context);
  vm.runInContext(`controller = PageComments.attach({
    frame, column, carried, onPlace, page: { id: 22, revision: "revision" }, stepId: null,
    kept: null, onChange, onStale() {}, onUnsaved() {}, onPick() {},
  });`, context);
  return { column, failures, loaded, replies, marked, controller: context.controller };
}

async function main() {
  for (const onPlace of [null, () => {}]) {
    const w = world("", onPlace);
    await tick();
    assert.match(w.column.textContent, /Comments not loaded: Cockpit answered 502/);
    assert.equal(w.failures.length, 1, "a failed read is logged");
    assert.equal(w.column.hidden, false);
    w.replies.push({ status: 200, data: { comments: [{ id: 7, state: "unsent", step_id: null,
      step_open: false, anchor: { kind: "text", quote: "Exports" }, text: "Keep these words" }] } });
    await w.controller.refresh();
    assert.doesNotMatch(w.column.textContent, /Comments not loaded/, "successful read leaves an obsolete load error");
    assert.equal(w.loaded.at(-1)[0].id, 7);
    assert.equal(w.marked.at(-1).cockpitComments.marks[0].id, 7, "recovered comments reach the frame");
    assert.equal(w.column.hidden, Boolean(onPlace), "an error-only column hides after recovery");
    w.replies.push(new TypeError("Failed to fetch"));
    await w.controller.refresh();
    assert.match(w.column.textContent, /Comments not loaded: Failed to fetch/);
    w.replies.push({ status: 200, data: { comments: [] } });
    await w.controller.refresh();
    assert.doesNotMatch(w.column.textContent, /Comments not loaded/);
    assert.equal(w.failures.length, 2, "each failed read stays in the error log");
    w.controller.detach();
  }
  const w = world("Keep my unsaved comment", null);
  await tick();
  w.replies.push({ status: 200, data: { comments: [] } });
  await w.controller.refresh();
  assert.match(w.column.textContent, /Keep my unsaved comment/, "recovery preserves the unsaved comment warning");
  assert.doesNotMatch(w.column.textContent, /Comments not loaded/);
  w.controller.detach();
  console.log("pass: comment reads recover from HTTP and connection failures and preserve edit warnings");
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
