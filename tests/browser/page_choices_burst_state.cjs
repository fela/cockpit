// The page-choice bridge coalesces typing, guards pending text, and flushes before an answer.
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const folder = process.argv[2] || path.resolve(__dirname, "../../src/cockpit/static");
const source = (name) => fs.readFileSync(path.join(folder, name), "utf8");
const tick = () => new Promise((resolve) => setImmediate(resolve));
const noop = () => {};
function world() {
  const handlers = new Map(), timers = new Map(), sent = [], held = new Set(), notes = [];
  let timerId = 0;
  const frame = { contentWindow: {}, getAttribute: () => "/pages/3/figure", dataset: { step: "7" } };
  const context = vm.createContext({ CustomEvent, TRY_MS: 30000,
    document: { querySelectorAll: () => [frame] }, logError: noop,
    setTimeout: (fn) => { const id = ++timerId; timers.set(id, fn); return id; },
    clearTimeout: (id) => timers.delete(id),
    addEventListener: (name, fn) => handlers.set(name, fn),
    dispatchEvent: (event) => notes.push(event.detail),
    holdLeave: (work) => { held.add(work); work.finally(() => held.delete(work)); },
    replyData: ({ data }) => data,
    sendAction: async (_url, options) => {
      sent.push(JSON.parse(options.body));
      return { data: {} };
    },
  });
  context.window = context;
  vm.runInContext(source("page-choices.js"), context);
  const message = (body) => handlers.get("message")({ source: frame.contentWindow, data: { cockpitChoices: body } });
  const choose = (chosen, typing) => message({ type: "chose", name: "field", chosen, label: "", typing });
  const settle = () => context.CockpitChoices.settled(7);
  return { context, message, choose, sent, held, timers, notes, settle };
}
(async () => {
  {
    const w = world();
    for (let i = 1; i <= 91; ++i) {
      w.choose("x".repeat(i), true);
      await tick(); // A fast server could finish each save before the next keystroke.
    }
    assert.equal(w.sent.length, 0, "typing writes before the quiet interval");
    assert.equal(w.held.size, 1, "waiting text must guard leaving");
    assert.equal(w.timers.size, 1, "keystrokes accumulate timers");
    await w.settle();
    assert.equal(w.sent.length, 1);
    assert.equal(w.sent[0].chosen, "x".repeat(91));
    assert.equal(w.held.size, 0);
    assert.equal(w.timers.size, 0);
    console.log("PASS 91 typing events: 1 PUT; Send flushes the final text and clears the leave guard");
  }
  {
    const w = world();
    w.choose("paused", true);
    for (const fn of [...w.timers.values()]) fn();
    await tick();
    assert.equal(w.sent[0].chosen, "paused", "a typing pause must save without Send");
    w.choose("blurred", true);
    w.message({ type: "flush", name: "field" });
    await tick();
    assert.equal(w.sent.at(-1).chosen, "blurred");
    w.choose(["yes"], false);
    assert.deepEqual(w.sent.at(-1).chosen, ["yes"], "checkbox changes must save immediately");
    await w.settle();
    console.log("PASS typing pauses, blur, and immediate checkbox saves");
  }
  {
    const w = world();
    let finish;
    const gate = new Promise((resolve) => { finish = resolve; });
    w.context.sendAction = async (_url, options) => {
      w.sent.push(JSON.parse(options.body));
      if (w.sent.length === 1) await gate;
      return { data: {} };
    };
    w.choose("first", false);
    w.choose("middle", true);
    w.choose("last", true);
    const answer = w.settle();
    await tick();
    assert.equal(w.sent.length, 1);
    finish();
    await tick();
    assert.equal(w.timers.size, 0, "Send must flush text waiting behind an in-flight save");
    await answer;
    assert.deepEqual(w.sent.map((body) => body.chosen), ["first", "last"]);
    assert.equal(w.held.size, 0);
    console.log("PASS slow saves retain the newest queued text and hold the answer");
  }
  {
    const pending = [], errors = [];
    let reads = 0, rendered = 0;
    const context = vm.createContext({
      api: () => { ++reads; return new Promise((resolve, reject) => pending.push({resolve, reject})); },
      logError: (_where, error) => errors.push(error), renderPreview: () => { ++rendered; },
    });
    const pages = source("pages.js");
    vm.runInContext('let state;\n' + pages.slice(pages.indexOf("let stateLoad ="), pages.indexOf("function refresh()", pages.indexOf("let stateLoad ="))), context);
    const first = vm.runInContext("loadState()", context);
    for (let i = 0; i < 91; ++i) vm.runInContext("loadState()", context);
    assert.equal(reads, 1, "choice saves overlap full status reads");
    pending.shift().resolve({revision: 1});
    await tick();
    assert.equal(reads, 2, "saves during a read need one fresh status after it");
    await first;
    pending.shift().resolve({revision: 2});
    await tick();
    assert.equal(vm.runInContext("state.revision", context), 2);
    const slow = vm.runInContext("loadState().then(renderPreview)", context);
    const before = rendered;
    for (let i = 0; i < 5; ++i) {
      vm.runInContext("loadState()", context);
      pending.shift().resolve({revision: 10 + i});
      await tick();
      assert.ok(rendered > before + i, "continuing refreshes starve redraws");
      assert.equal(pending.length, 1, "refreshes overlap pending status reads");
    }
    pending.shift().resolve({revision: 20});
    await slow;
    await tick();
    const failed = vm.runInContext("loadState()", context);
    pending.shift().reject(new Error("status unavailable"));
    await failed;
    assert.equal(errors.length, 1);
    const recovered = vm.runInContext("loadState()", context);
    pending.shift().resolve({revision: 3});
    await recovered;
    assert.equal(vm.runInContext("state.revision", context), 3);
    console.log("PASS 91 saves during a slow status read share one subsequent read; failures recover");
  }
  {
    const posts = [], handlers = new Map();
    const field = { name: "comment", localName: "textarea", value: "First", labels: [] };
    const parent = { postMessage: (data) => posts.push(data.cockpitChoices) };
    const context = vm.createContext({ parent, CustomEvent,
      document: { readyState: "complete", querySelectorAll: () => [field],
        addEventListener: (name, fn) => handlers.set(name, fn) },
      addEventListener: noop, dispatchEvent: noop,
    });
    context.window = context;
    vm.runInContext(source("page-choices-frame.js"), context);
    handlers.get("input")({ target: field, type: "input" });
    handlers.get("change")({ target: field, type: "change" });
    assert.equal(posts.filter((entry) => entry.type === "chose").length, 1, "blur resends the same text");
    assert.equal(posts.at(-1).type, "flush");
    field.value = "";
    handlers.get("input")({ target: field, type: "input" });
    assert.equal(posts.at(-1).chosen, null, "clearing text must still save");
    console.log("PASS the frame reports text once, flushes on blur, and preserves clearing");
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
