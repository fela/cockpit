// Controlled slow reads and history aging, using the page's real polling and cache code.
// node tests/browser/polling_load_state.cjs [static source directory]
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const folder = process.argv[2] || path.resolve(__dirname, "../../src/cockpit/static");
const source = fs.readFileSync(path.join(folder, "app.js"), "utf8");
const tick = () => new Promise((resolve) => setImmediate(resolve));

function historyWorld() {
  let now = 100000, redraws = 0;
  const requests = [];
  const context = vm.createContext({
    ui: { histories: new Map() }, HISTORY_STALE_MS: 15000,
    Date: { now: () => now }, console: { error: () => {} },
    safeRender: () => { redraws++; },
    api: (method, url) => new Promise((resolve, reject) => requests.push({ method, url, resolve, reject })),
    document: { querySelectorAll: () => [{ dataset: { item: "1" } }] },
  });
  const from = source.indexOf("function historyVersion("), to = source.indexOf("// A moment in a tooltip:", from);
  assert.ok(from >= 0 && to > from);
  vm.runInContext(source.slice(from, to), context);
  context.state = { items: [{ id: 1, updated_at: "v1" }], queue: [], ready_next: [], snoozed: [], with_agents: [], messages: [] };
  return { context, requests, run: (code) => vm.runInContext(code, context),
    advance: (ms) => { now += ms; }, redraws: () => redraws };
}

async function main() {
  const w = historyWorld();
  w.run("warmHistory(1, state)");
  w.requests[0].resolve({ entries: [], now: "first" });
  await tick();
  for (let i = 0; i < 4; i++) {
    w.advance(20000);
    w.run("warmHistory(1, state)");
  }
  assert.equal(w.requests.length, 1, "an unchanged upcoming card must keep its prefetched history across idle redraws");
  assert.equal(w.redraws(), 0, "prefetching must keep the shown card steady");
  w.run("historyOf(1, historyVersion(1, state))");
  assert.equal(w.requests.length, 2, "opening the stale upcoming card must immediately check for transcript changes");
  assert.equal(w.run("ui.histories.get(1).data.now"), "first", "the cached history remains available during the check");
  w.requests[1].resolve({ entries: [], now: "second", story: { text: "A new transcript answer" } });
  await tick();
  assert.equal(w.redraws(), 1, "the newly visible transcript answer must redraw");
  w.run('state.items[0].updated_at = "v2"; warmHistory(1, state)');
  assert.equal(w.requests.length, 3, "a changed upcoming item must still prefetch immediately");
  w.requests[2].resolve({ entries: [], now: "third" });
  await tick();
  w.advance(15000);
  w.run("refreshShownHistories()");
  assert.equal(w.requests.length, 4, "a shown history must retain the regular transcript check");
  w.requests[3].resolve({ entries: [], now: "fourth" });
  await tick();
  assert.equal(w.redraws(), 1, "an unchanged history must keep the card steady");
  console.log("PASS Upcoming histories stay cached, refresh when opened or changed, and shown histories retain transcript checks");

  const failed = historyWorld();
  failed.run("warmHistory(1, state)");
  failed.requests[0].reject(new Error("offline"));
  await tick();
  failed.run("warmHistory(1, state)");
  assert.equal(failed.requests.length, 1, "a failed prefetch must wait before retrying");
  failed.advance(15000);
  failed.run("warmHistory(1, state)");
  assert.equal(failed.requests.length, 2, "a failed prefetch must recover without an item edit");
  failed.run("historyOf(1, historyVersion(1, state))");
  failed.requests[1].resolve({ entries: [] });
  await tick();
  assert.equal(failed.redraws(), 1, "opening a prefetched history must make its recovery redraw the card");
  console.log("PASS Failed prefetches retry after 15 seconds and refresh the card when opened");

  let poll, reads = 0;
  const ui = { refreshing: false, refreshAgain: false }, document = { hidden: false };
  const context = vm.createContext({ ui, document, POLL_MS: 5000,
    setInterval: (callback) => { poll = callback; },
    refresh: () => {
      reads++;
      if (ui.refreshing) ui.refreshAgain = true;
      else ui.refreshing = true;
    },
  });
  const start = source.indexOf("setInterval(() => { if (!document.hidden) refresh(); }, POLL_MS);");
  const changed = source.indexOf("setInterval(() => { if (!document.hidden && !ui.refreshing) refresh(); }, POLL_MS);");
  const from = start >= 0 ? start : changed;
  assert.ok(from >= 0, "page polling interval is present");
  vm.runInContext(source.slice(from, source.indexOf("\n", from)), context);
  poll();
  poll();
  poll();
  assert.equal(reads, 1, "periodic ticks during a slow read must keep one request in flight");
  assert.equal(ui.refreshAgain, false, "periodic ticks must let the host rest after a slow read");
  ui.refreshing = false;
  document.hidden = true;
  poll();
  assert.equal(reads, 1, "background tabs must skip state polling");
  document.hidden = false;
  poll();
  assert.equal(reads, 2, "the next visible interval must resume polling");
  console.log("PASS Slow periodic state reads skip overlapping ticks and resume on the next visible interval");
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
