// Focused render-work checks with controlled state, DOM builders and time.
// node tests/browser/render_work_state.cjs
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const folder = path.resolve(__dirname, "../../src/cockpit/static");
function part(file, from, to) {
  const text = fs.readFileSync(path.join(folder, file), "utf8");
  const start = text.indexOf(from), end = text.indexOf(to, start + from.length);
  assert.ok(start >= 0 && end > start, `${file}: missing boundary ${from}`);
  return text.slice(start, end);
}
const noop = () => {};
function world(values) {
  const context = vm.createContext(values);
  return { context, run: (source) => vm.runInContext(source, context) };
}

async function usageChecks() {
  let state = { now: new Date().toISOString(), usage: { accounts: [] }, permission_requests: [], page_comments: [], queue: [] };
  const calls = { usage: 0, fit: 0, render: 0 };
  const ui = { tick: 0, pending: new Map(), snoozing: new Map(), handing: new Map(), busy: new Set(),
    renderedAt: Date.now(), error: "", catchUpFrom: null, openedHidden: new Set() };
  const w = world({ ui, document: { hidden: false, querySelector: () => null }, window: {},
    readState: async () => state, URLSearchParams, ADDS_WAIT_MS: 1000, ARRIVED_MS: 10000, RERENDER_MS: 60000,
    commentDeletions: new Map(), noticeCodexSignIns: noop,
    forgetSentMarks: noop, settleUploads: noop, noticeSideAnswers: noop, noticeSnoozeReads: noop, renderStatus: noop, caughtUp: noop,
    fill: () => calls.usage++, usagePill: noop, fitHeader: () => calls.fit++,
    logError: (_label, error) => { throw error; }, Motion: { redraw: (work) => work() }, renderBanner: noop,
  });
  w.run(part("app.js", "let usageGeneration =", "function usagePill("));
  w.run(part("app.js", "async function refresh()", "// Resolves to whether the work went through."));
  w.context.safeRender = () => { calls.render++; w.run("renderUsage(ui.state); fitHeader()"); };
  await w.run("refresh()");
  assert.deepEqual(calls, { usage: 1, fit: 1, render: 1 }, "full render repeats usage work");
  state = { ...state, usage: { accounts: [1] } };
  await w.run("refresh()");
  assert.deepEqual(calls, { usage: 2, fit: 2, render: 1 }, "usage-only change fails to draw");
  w.context.safeRender = () => { ui.pendingRender = true; };
  state = { ...state, queue: [1], usage: { accounts: [2] } };
  await w.run("refresh()");
  assert.deepEqual(calls, { usage: 3, fit: 3, render: 1 }, "deferred render loses usage update");
  console.log("pass: full render, usage-only refresh and deferred render usage work");
}

function sidebarChecks() {
  const built = [];
  const ui = { shownItem: 3, addsHeld: new Map(), open: new Set() };
  const h = (tag, props, ...children) => ({ tag, ...props, children: children.flat(Infinity), dataset: {} });
  const w = world({ ui, h, sessionView: {}, settingsView: { open: false }, sessionMap: () => new Map(), warningList: () => [], warningRow: noop,
    handHeld: (id) => id === 40 ? {} : null,
    handHeldRow: (item) => { built.push(item.id); return h("li", { class: "held-hand" }); },
    heldRow: (item) => { built.push(item.id); return h("li", { class: "held" }); },
    itemSessions: () => [], raisedPriority: () => false, itemHasDraft: () => false, mergedSideMark: noop,
    Icons: { projectMark: (id) => { built.push(id); } }, icon: noop, age: () => "now", clip: (text) => text,
  });
  w.run(part("sidebar.js", "function sidebarRow(", "// Another task merged"));
  w.run(part("sidebar.js", "function inboxEmptyRow()", "// The pulsing dot"));
  w.run(part("sidebar.js", "function moreRows(", "// The parts of Waiting"));
  // Stop after Inbox so remaining claims can be inspected before later groups use them.
  w.run(part("sidebar.js", "function renderSidebar(", "  // A handed step shows")
    + "return { mine, remaining: state.items.filter(item => take(item.id)) }; }");
  const items = Array.from({ length: 7 }, (_, index) => ({ id: index + 1, project: index + 1, title: `Item ${index + 1}` }));
  const step = (id, item_id) => ({ id, item_id });
  w.context.state = { items, queue: [step(10, 1)], ready_next: [step(11, 1), step(20, 2), step(30, 3),
    step(31, 3), step(40, 4), step(50, 5), { ...step(60, 6), left: true }] };
  let result = w.run("renderSidebar(state)");
  assert.deepEqual(built, [1, 3], "folded ready-next constructs hidden rows");
  assert.equal(result.mine[0][4], 5, "Inbox count includes duplicates or left steps");
  assert.equal(result.mine[0][2].at(-1).children[0].children[0].text, "Show 3 more");
  assert.deepEqual(Array.from(result.remaining, (item) => item.id), [6, 7], "hidden rows were not claimed");
  ui.open.add("sidebar:more"); built.length = 0;
  result = w.run("renderSidebar(state)");
  assert.deepEqual(built, [1, 2, 3, 4, 5], "expanded ready-next order or handed rows changed");
  assert.deepEqual(Array.from(result.mine[0][2].slice(0, -1), (row) => row.dataset.step), [10, 20, 30, 40, 50]);
  assert.equal(result.mine[0][2].at(-1).children[0].children[0].text, "Show fewer");
  ui.open.clear(); ui.shownItem = 4; built.length = 0;
  result = w.run("renderSidebar(state)");
  assert.deepEqual(built, [1], "handed row incorrectly treated as selected");
  ui.shownItem = 3; ui.addsHeld.set(3, { row: true }); built.length = 0;
  w.run("renderSidebar(state)");
  assert.deepEqual(built, [1], "held Add row incorrectly treated as selected");
  ui.addsHeld.clear(); w.context.state.ready_next = [step(30, 3)];
  result = w.run("renderSidebar(state)");
  assert.equal(result.mine[0][2].length, 2, "all-selected ready list gained a Show 0 more row");
  console.log("pass: folded builders, order, selected/held rows, counts and cross-group claims");
}

function timerChecks() {
  let now = Date.parse("2026-11-11T00:00:00Z"), writes = 0, freshnessChecks = 0, idleChecks = 0, pulses = 0;
  function element(text, dataset) {
    return { dataset, get textContent() { return text; }, set textContent(value) { writes++; text = value; } };
  }
  const timer = element("0:05", { timerSeconds: "5", timerStart: new Date(now).toISOString() });
  const total = element("0m", { totalSince: new Date(now).toISOString() });
  const age = element("just now", { ago: new Date(now - 30000).toISOString() });
  const document = { hidden: false, body: { classList: { toggle: () => pulses++ } },
    querySelectorAll: (selector) => ({ "[data-timer-start]": [timer], "[data-total-since]": [total], "[data-ago]": [age], "[data-expires]": [] })[selector],
    addEventListener: (_name, handler) => { document.visibility = handler; } };
  const w = world({ document, Date: class extends Date { static now() { return now; } },
    checkIdle: () => idleChecks++, showFresh: () => freshnessChecks++, ui: { seenBefore: null },
    readStored: () => null, SEEN_KEY: "seen", startCatchUp: noop, refresh: noop });
  w.run(part("app.js", "function ago(", "// How fresh a moment"));
  w.run(part("app.js", "function timerSeconds(", "// The step's time in its"));
  w.run(part("app.js", "function durationWords(", "// What the user typed"));
  w.run(part("app.js", 'document.addEventListener("visibilitychange", () => {\n  if (document.hidden) {', "// A connection that comes back"));
  w.run("tickTimers()");
  assert.equal(writes, 0, "unchanged text was assigned");
  assert.equal(freshnessChecks, 1, "unchanged age skipped freshness update");
  document.hidden = true; now += 90000;
  w.run("tickTimers()");
  assert.equal(writes, 0); assert.equal(idleChecks, 2); assert.equal(pulses, 1);
  document.hidden = false; document.visibility();
  assert.equal(timer.textContent, "1:35"); assert.equal(total.textContent, "1m"); assert.equal(age.textContent, "2 min ago");
  assert.equal(writes, 3); assert.equal(freshnessChecks, 2); assert.equal(idleChecks, 3);
  console.log("pass: unchanged timer text, hidden ticks, freshness and visibility catch-up");
}

(async () => { await usageChecks(); sidebarChecks(); timerChecks(); })().catch((error) => {
  console.error(error); process.exitCode = 1;
});
