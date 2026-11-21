"use strict";
const assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm"), path = require("node:path");
const source = fs.readFileSync(path.join(__dirname, "../../src/cockpit/static/app.js"), "utf8");
const extract = (name, prefix) => {
  const start = source.indexOf(`${prefix}function ${name}(`);
  assert.ok(start >= 0, name);
  return source.slice(start, source.indexOf("\n}", start) + 2);
};
const ok = () => ({ status: 200, ok: true, json: async () => ({ input_count: 1 }) });
const refusal = (status, detail, extra = {}) => ({ status, ok: false, json: async () => ({ detail, ...extra }) });
let now = 0, connected = true;
const requests = [], limits = [], retries = [];
let replies = [];
const context = vm.createContext({
  Date, performance: { now: () => now }, crypto: require("node:crypto").webcrypto,
  AbortSignal: { timeout(ms) { limits.push(ms); return {}; } },
  setTimeout(callback, ms) { retries.push(ms); now += ms; callback(); },
  ui: { timerPresses: 0, timerSent: Promise.resolve() }, TIMER_PRESS_MS: 60000,
  TIMER_START_TRY_MS: 8000, TIMER_START_RETRY_MS: 500,
  requireConnection() { if (!connected) throw new Error("Connect to Cockpit before starting"); },
  run: (_key, action) => action(), holdLeave() {}, noteOwnInput() {},
  optimistic: async (_change, action) => action(),
  async answerJson(response) { return response.json(); },
  async fetch(requestPath, options) {
    requests.push({ requestPath, press: options.headers["x-cockpit-action"] });
    assert.equal(JSON.parse(options.body).press, requests.at(-1).press);
    assert.ok(replies.length, "Unexpected timer request");
    return replies.shift()();
  },
  location: {},
});
for (const [name, prefix] of [["apiStart", "async "], ["answer", ""], ["refused", ""], ["sendTimer", ""]]) {
  vm.runInContext(extract(name, prefix), context);
}
(async () => {
  replies = [() => { now += 8000; throw new DOMException("signal timed out", "TimeoutError"); }, ok];
  await context.sendTimer(621, true);
  assert.deepEqual(requests[0], requests[1], "A retry must reuse the entire press identity");
  assert.deepEqual(limits, [8000, 8000]);
  assert.deepEqual(retries, [500]);
  console.log("pass: a stalled start gets two short attempts with the same press id");

  replies = [() => ({ ...ok(), json: async () => { throw new TypeError("Body read failed"); } }), ok];
  await context.sendTimer(621, true);
  assert.deepEqual(requests[2], requests[3]);
  console.log("pass: a lost success body can be recovered by retrying the original press id");

  replies = [() => refusal(502), () => refusal(504)];
  await assert.rejects(context.sendTimer(621, true), (error) => {
    assert.equal(error.message, "Timer start not confirmed: Cockpit answered 504");
    assert.equal(error.cause.status, 504);
    return true;
  });
  assert.equal(requests.length, 6);
  console.log("pass: two gateway errors leave the start unconfirmed with no third request");

  for (const reply of [() => ({ ...refusal(409), json: async () => { throw new TypeError("Body read failed"); } }),
    () => refusal(409)]) {
    const sent = requests.length;
    replies = [() => { throw new TypeError("Failed to fetch"); }, reply];
    await assert.rejects(context.sendTimer(621, true), /^Error: Timer start not confirmed:/);
    assert.equal(requests.length, sent + 2);
  }
  console.log("pass: an unreadable conflict reply to a retry preserves uncertainty");

  const sent = requests.length;
  replies = [() => { throw new TypeError("Failed to fetch"); }, () => refusal(500, "Cockpit failed on this action")];
  await assert.rejects(context.sendTimer(621, true), { message: "Timer start not confirmed: Cockpit failed on this action" });
  assert.equal(requests.length, sent + 2);
  console.log("pass: a server error after a lost reply does not claim the timer never started");

  replies = [() => refusal(409, "Cockpit stopped during this action", { cut_off: true })];
  await assert.rejects(context.sendTimer(621, true), { message: "Timer start not confirmed: Cockpit stopped during this action" });
  assert.equal(requests.length, 13);
  replies = [() => refusal(403, "Permission required")];
  await assert.rejects(context.sendTimer(621, true), { message: "Timer not started: Permission required" });
  assert.equal(requests.length, 14);
  connected = false;
  await assert.rejects(context.sendTimer(621, true), { message: "Timer not started: Connect to Cockpit before starting" });
  assert.equal(requests.length, 14);
  console.log("pass: interrupted actions, authorization refusals and offline presses do not send retries");

  connected = true;
  replies = [() => { now += 1200; throw new TypeError("Failed to fetch"); }];
  await assert.rejects(context.apiStart("/api/steps/621/timer/start", "near-deadline", now + 1500),
    (error) => error.timerUnconfirmed && error.message === "Failed to fetch");
  assert.equal(limits.at(-1), 1500);
  assert.equal(requests.length, 15);
  console.log("pass: attempts honor the original deadline, even when a shorter attempt fails");

  let release;
  context.ui.timerSent = new Promise((resolve) => { release = resolve; });
  const expired = context.sendTimer(621, true);
  now += 60000;
  release(null);
  await assert.rejects(expired, (error) => {
    assert.equal(error.message, "Timer not started: press expired");
    assert.equal(error.retry?.label, "Start now");
    return true;
  });
  assert.equal(requests.length, 15);
  console.log("pass: a queued press that expires sends nothing and offers Start now");
})().catch((error) => { console.error(error); process.exitCode = 1; });
