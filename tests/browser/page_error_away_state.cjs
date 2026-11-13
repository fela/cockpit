// Run the real error-reporting code against a deterministic clock and check what each report says about Cockpit
// being away (`away_ms`), without a browser or sockets.
// node tests/browser/page_error_away_state.cjs [directory containing errors.js]
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const folder = process.argv[2] || path.resolve(__dirname, "../../src/cockpit/static");
const source = fs.readFileSync(path.join(folder, "errors.js"), "utf8");
const tick = () => new Promise((resolve) => setImmediate(resolve));

// A page whose requests to `/api/state` get the statuses in `statuses`, in turn, then 200.
function page(statuses) {
  let now = 0;
  let nextId = 0;
  const timers = new Map();
  const reports = [];
  const events = new EventTarget();
  const document = Object.assign(new EventTarget(), { hidden: false, readyState: "loading" });
  const later = (fn, ms) => {
    const id = ++nextId;
    timers.set(id, { at: now + ms, fn });
    return id;
  };
  const answer = (status) => ({ status, ok: status < 400, bodyUsed: true, text: async () => "{}" });
  const context = vm.createContext({
    document, navigator: { onLine: true }, TypeError, Error, AbortController,
    Date: class extends Date { static now() { return now; } },
    performance: { now: () => now, timeOrigin: 0 },
    crypto: { randomUUID: () => "test-page" },
    location: { origin: "http://example.test", pathname: "/" },
    console: { error() {}, warn() {} },
    setTimeout: later, clearTimeout: (id) => timers.delete(id),
    setInterval: () => 1, clearInterval() {},
    addEventListener: events.addEventListener.bind(events),
    removeEventListener: events.removeEventListener.bind(events),
    fetch: (url, { body }) => {
      if (url === "/api/page-errors") reports.push(JSON.parse(body));
      return Promise.resolve(answer(url === "/api/state" && statuses.length ? statuses.shift() : 200));
    },
  });
  context.window = context;
  vm.runInContext(source, context);
  context.noteAnswered();
  // Runs every timer due by `to` and leaves the clock there.
  async function until(to) {
    await tick();
    for (;;) {
      const next = [...timers].filter(([, timer]) => timer.at <= to).sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0];
      if (!next) break;
      const [id, timer] = next;
      now = timer.at;
      timers.delete(id);
      timer.fn();
      await tick();
    }
    now = to;
  }
  const answered = (status) => Object.assign(new Error(`Cockpit answered ${status}`), { status });
  return { context, reports, until, answered };
}

(async () => {
  {
    // A 500 and a 503 at one place count as one error: the 500 met Cockpit there, so their report says no time away.
    const { context, reports, until, answered } = page([]);
    context.logError("loading presence", answered(500));
    await until(1000);
    context.logError("loading presence", answered(500));
    await until(2000);
    context.logError("loading presence", answered(503));
    await until(61000);
    assert.deepEqual(reports.map((each) => [each.count, each.away_ms]), [[1, null], [2, null]]);
    // Sightings that all met Cockpit away report when it went away for the latest of them.
    await until(70000);
    context.logError("loading presence", answered(503));
    await until(80000);
    context.logError("loading presence", answered(502));
    await until(121000);
    assert.deepEqual(reports.slice(2).map((each) => [each.count, each.away_ms]), [[2, 40000]]);
    console.log("pass: a sighting met with Cockpit there is never covered by a restart");
  }
  {
    // Cockpit answered 503 and then, half a second later, 200: away from 0 to 500 ms.
    const { context, reports, until, answered } = page([503]);
    context.readThroughRestart("/api/state", {});
    await until(1000);
    // A request whose last try met that time away, logged just after: it reports the time away it met.
    context.logError("reading the unread feedback", answered(503));
    await until(20000);
    assert.equal(reports.at(-1).away_ms, 1000);
    // A 503 ten minutes later is a new time away, whatever the start of the last one.
    await until(600000);
    context.logError("saving the credential", answered(503));
    await until(600000);
    assert.equal(reports.at(-1).away_ms, 0);
    console.log("pass: an error long after a time away does not take its start");
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
