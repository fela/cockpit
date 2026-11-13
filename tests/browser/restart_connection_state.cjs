// Run the real retry and error-reporting code against a deterministic clock, without a browser or sockets.
// node tests/browser/restart_connection_state.cjs [directory containing errors.js]
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const folder = process.argv[2] || path.resolve(__dirname, "../../src/cockpit/static");
const source = fs.readFileSync(path.join(folder, "errors.js"), "utf8");
const tick = () => new Promise((resolve) => setImmediate(resolve));

async function run({ answers, recoveryAt, offline, deadline }) {
  let now = 0;
  let nextId = 0;
  let tries = 0;
  let connected = false;
  let result;
  const timers = new Map();
  const reports = [];
  const events = new EventTarget();
  const document = Object.assign(new EventTarget(), { hidden: false, readyState: "loading" });
  const navigator = { onLine: true };
  const later = (fn, ms) => {
    const id = ++nextId;
    timers.set(id, { at: now + ms, fn });
    return id;
  };
  const context = vm.createContext({
    document, navigator, TypeError, Error, AbortController,
    Date: class extends Date { static now() { return now; } },
    performance: { now: () => now, timeOrigin: 0 },
    crypto: { randomUUID: () => "test-page" },
    location: { origin: "http://example.test", pathname: "/" },
    console: { error() {}, warn() {} },
    setTimeout: later, clearTimeout: (id) => timers.delete(id),
    setInterval: () => 1, clearInterval() {},
    addEventListener: events.addEventListener.bind(events),
    removeEventListener: events.removeEventListener.bind(events),
    fetch: (url, { signal, body }) => {
      const reply = { status: 200, ok: true, bodyUsed: true, text: async () => "{}" };
      if (url === "/api/page-errors") {
        reports.push(JSON.parse(body));
        return Promise.resolve(reply);
      }
      tries++;
      if (connected) return Promise.resolve(reply);
      return new Promise((_resolve, reject) => {
        signal.addEventListener("abort", () => reject(signal.reason), { once: true });
        if (Number.isFinite(recoveryAt)) later(() => reject(new TypeError("Failed to fetch")), 0);
      });
    },
  });
  context.window = context;
  vm.runInContext(source, context);
  context.noteAnswered();
  for (const at of answers) later(() => context.noteAnswered(), at);
  if (Number.isFinite(recoveryAt)) later(() => { connected = true; }, recoveryAt);
  if (offline) {
    later(() => {
      navigator.onLine = false;
      events.dispatchEvent(new Event("offline"));
    }, 10000);
    later(() => {
      navigator.onLine = true;
      connected = true;
      events.dispatchEvent(new Event("online"));
    }, 610000);
  }
  const request = Number.isFinite(deadline)
    ? context.readWithin("/api/state", {}, deadline, context.awayClock())
    : context.readThroughRestart("/api/state", {});
  request.then(
    () => { result = { ok: true }; },
    (error) => {
      result = { ok: false, unreached: error.unreached };
      context.logError("refreshing the page", error);
    },
  );
  await tick();
  for (let turns = 0; !result && turns < 100; turns++) {
    const next = [...timers].sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0];
    assert.ok(next, "request is pending with no timer left");
    const [id, timer] = next;
    now = timer.at;
    timers.delete(id);
    timer.fn();
    await tick();
  }
  assert.ok(result, "request settled within the simulated timer limit");
  return { ...result, tries, at: now, reports: reports.length };
}

(async () => {
  const cases = [
    ["silent network", [], Infinity, false, Infinity, false, true, 0, 7, 240500],
    ["early answer remains evidence of a working connection", [1000], Infinity, false, Infinity, false, false, 1, 7, 240500],
    ["answer near the end of the first try remains evidence", [29000], Infinity, false, Infinity, false, false, 1, 7, 240500],
    ["other routes keep answering", [60000, 120000, 180000, 240000], Infinity, false, Infinity, false, false, 1, 7, 240500],
    ["reconnect during the final try", [220000], Infinity, false, Infinity, false, true, 0, 7, 240500],
    ["deadline retains an early answer", [500], Infinity, false, 1000, false, false, 1, 1, 30500],
    ["deadline without an answer", [], Infinity, false, 1000, false, true, 0, 1, 30500],
    ["short restart recovers", [], 2500, false, Infinity, true, undefined, 0, 4, 3500],
    ["ten minutes offline recover", [], Infinity, true, Infinity, true, undefined, 0, 2, 610000],
  ];
  for (const [name, answers, recoveryAt, offline, deadline, ok, unreached, reports, tries, at] of cases) {
    const actual = await run({ answers, recoveryAt, offline, deadline });
    assert.deepEqual(actual, { ok, ...(ok ? {} : { unreached }), reports, tries, at }, name);
    console.log("pass: " + name);
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
