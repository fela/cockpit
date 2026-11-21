// Scoped outbox replay, online-only writes and independent recovery. Runs the real errors.js without a server.
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.resolve(__dirname, "../../src/cockpit/static/errors.js"), "utf8");
const USER = "a".repeat(64), OTHER = "b".repeat(64), UNTIL = 2000000000, NOW = 1800000000000;
const prefix = (user = USER, until = UNTIL) => `cockpit.outbox.${user}.${until}.`;
const tick = () => new Promise((resolve) => setImmediate(resolve));
const reply = (data = {}, status = 200) => ({ status, ok: status >= 200 && status < 300, headers: new Headers(), bodyUsed: true, text: async () => JSON.stringify(data) });
function saved(resource, fields = {}) {
  return JSON.stringify({ user: USER, until: UNTIL, path: `/api/items/${resource}`, method: "PATCH", headers: { "content-type": "application/json", "x-cockpit-action": `old-${resource}` },
    body: '{"priority":"high"}', tryMs: 30000, words: null, at: NOW - 1000, n: 1, page: "closed", ...fields });
}
function world({ online = true, values = new Map(), scoped = true, helper = false, lease = UNTIL, channels = [], partActions = [], fetchAction = async () => reply(), session = { user: USER, until: UNTIL } } = {}) {
  const events = new EventTarget(), calls = [], reports = [];
  const element = () => ({ children: [], classList: { add() {} }, append(...nodes) { this.children.push(...nodes); }, replaceChildren(...nodes) { this.children = nodes; } });
  const body = element(); body.dataset = {}; body.append({ textContent: "Private helper writing" });
  const document = Object.assign(new EventTarget(), { readyState: "loading", hidden: false,
    documentElement: { dataset: scoped ? { offlineUser: USER, offlineUntil: String(lease), offlineHelper: helper ? "1" : "" } : {} },
    body, createElement: element, getElementById: (id) => id === "offline-lock" ? body.children.find((node) => node.id === id) : ({ hidden: true, setAttribute() {} }) });
  const navigator = { onLine: online, locks: { request: async (name, options, callback) => {
    if (typeof options === "function") return options({ name });
    return callback({ name });
  } } };
  let uuid = 0;
  const context = vm.createContext({ document, navigator, TypeError, Error, AbortController, Headers,
    BroadcastChannel: class { constructor() { channels.push(this); } postMessage(data) { for (const channel of channels) if (channel !== this) channel.onmessage?.({ data }); } },
    Date: class extends Date { static now() { return NOW; } }, performance, console: { error() {}, warn() {} },
    crypto: { randomUUID: () => `uuid-${++uuid}` }, location: { origin: "http://example.test", pathname: "/" },
    localStorage: { get length() { return values.size; }, key: (i) => [...values.keys()][i] ?? null,
      getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: (key) => values.delete(key) },
    sessionStorage: { getItem: () => null, setItem() {} },
    setTimeout, clearTimeout, setInterval: () => 1, clearInterval() {},
    addEventListener: events.addEventListener.bind(events), removeEventListener: events.removeEventListener.bind(events),
    dispatchEvent: events.dispatchEvent.bind(events),
    CustomEvent: class extends Event { constructor(type, options) { super(type, options); this.detail = options?.detail; } },
    fetch: async (url, options) => {
      calls.push({ url, options });
      if (url === "/api/page-errors") { reports.push(JSON.parse(options.body)); return reply(); }
      if (url === "/api/offline/session") return typeof session === "function" ? session(options) : reply(session);
      return fetchAction(url, options, navigator);
    },
  });
  context.window = context;
  // What a part's first script (Part.first_scripts) sets before errors.js runs.
  context.cockpitPartActions = partActions;
  vm.runInContext(source, context);
  return { context, values, calls, reports, document, navigator, events,
    start: () => document.dispatchEvent(new Event("DOMContentLoaded")),
    clear: () => events.dispatchEvent(new Event("cockpit:offline-cleared")),
    send: (url, method = "POST", body = "{}") => context.sendAction(url, { method, body, credentials: "same-origin", headers: { "content-type": "application/json" } }, 30000, null) };
}
async function until(check) { for (let n = 0; n < 30 && !check(); n++) await tick(); assert.ok(check()); }
(async () => {
  const sensitive = ["/api/permissions/rules", "/api/steps/1/allow", "/api/steps/1/secret", "/api/steps/1/timer/play",
    "/api/claude/main/sign-in", "/api/example/grant/1", "/api/example/ops", "/api/example/link/connect", "/api/item-pages/1/public", "/api/new-route"];
  const blocked = world({ online: false });
  for (const url of sensitive) await assert.rejects(blocked.send(url), /Connect to Cockpit/);
  assert.equal(blocked.calls.length, 0);
  assert.equal(blocked.values.size, 0);
  console.log("pass: offline grants, secrets, sign-in, timer starts, publishing and unknown routes never send or persist");

  const once = world({ fetchAction: async () => { throw new TypeError("network lost"); } });
  for (const url of sensitive) await assert.rejects(once.send(url), /network lost/);
  assert.equal(once.calls.length, sensitive.length);
  assert.ok(once.calls.every(({ options }) => !options.headers["x-cockpit-action"]));
  assert.equal(once.values.size, 0);
  console.log("pass: online sensitive writes make exactly one attempt and keep no action credentials");

  const zero = world(); zero.start(); await tick();
  assert.equal(zero.calls.length, 0);
  const asleep = world({ online: false, values: new Map([[prefix() + "old", saved(1)]]) }); asleep.start(); await tick();
  assert.equal(asleep.calls.length, 0);
  console.log("pass: no replay authentication requests for an empty queue or an offline page");

  const foreign = new Map([["cockpit.outbox.legacy", saved(1)], [prefix(OTHER) + "other", saved(2, { user: OTHER })], [prefix(USER, UNTIL + 1) + "new-login", saved(3, { until: UNTIL + 1 })]]);
  const isolated = world({ values: foreign }); isolated.start(); await tick();
  assert.equal(isolated.calls.length, 0); assert.equal(foreign.size, 3);
  console.log("pass: legacy, another user and another login's queues remain quarantined");

  const wrong = world({ values: new Map([[prefix() + "old", saved(1)]]), session: { user: OTHER, until: UNTIL } });
  wrong.start(); await until(() => wrong.calls.length > 0); await tick();
  assert.deepEqual(wrong.calls.map(({ url }) => url), ["/api/offline/session"]);
  assert.equal(wrong.values.size, 0);
  console.log("pass: a changed authenticated user clears the old login's queue before any replay");

  const relogged = world({ values: new Map([[prefix() + "old", saved(1)]]), session: { user: USER, until: UNTIL + 1 } });
  relogged.start(); await tick(); await tick();
  assert.deepEqual(relogged.calls.map(({ url }) => url), ["/api/offline/session"]);
  assert.equal(relogged.values.size, 0);
  const poisoned = world({ values: new Map([[prefix() + "permission", saved(1, { path: "/api/permissions/rules", method: "POST" })]]) });
  poisoned.start(); await tick(); await tick();
  assert.ok(poisoned.calls.every(({ url }) => url === "/api/offline/session"));
  console.log("pass: a new login lease and an unsupported saved grant cannot replay");

  const unavailable = world({ values: new Map([[prefix() + "old", saved(1)]]), session: async () => { throw new TypeError("offline wifi"); } });
  unavailable.start(); await tick(); await tick();
  assert.equal(unavailable.reports.length, 0); assert.equal(unavailable.values.size, 1);
  console.log("pass: failed connection verification preserves ordinary queued edits without reporting an application fault");

  let expireCheck, checks = 0;
  const slowSignIn = world({ helper: true, session: ({ signal }) => {
    if (++checks > 1) return reply({ user: USER, until: UNTIL });
    return new Promise((_resolve, reject) => {
      expireCheck = () => reject(new DOMException("signal is aborted without reason", "AbortError"));
      signal.addEventListener("abort", expireCheck, { once: true });
    });
  } });
  const timers = [];
  slowSignIn.context.setTimeout = (callback, ms) => {
    if (ms === 30000) { timers.push(callback); return 0; }
    return setTimeout(callback, ms);
  };
  const holdPath = "/api/steps/9/hold", pausePath = "/api/steps/9/timer/pause";
  const hold = slowSignIn.send(holdPath);
  await until(() => expireCheck);
  const pause = slowSignIn.context.sendPressed(pausePath, NOW - 1000, "press-1");
  await until(() => timers.length === 3);
  timers[1](); // Only the shared sign-in verification runs out of time.
  await Promise.all([hold, pause]);
  assert.ok(checks >= 2);
  for (const path of [holdPath, pausePath]) assert.equal(slowSignIn.calls.filter(({ url }) => url === path).length, 1);
  console.log("pass: a shared sign-in timeout retries a hold and pause without sending either before verification");

  let releaseOld;
  const held = new Promise((resolve) => { releaseOld = resolve; });
  const backlog = world({ values: new Map([[prefix() + "old", saved(1)]]), fetchAction: async (url, options) => {
    if (options.headers["x-cockpit-action"] === "old-1") return held;
    return reply();
  } });
  backlog.start(); await until(() => backlog.calls.some(({ url }) => url === "/api/items/1"));
  const independent = backlog.send("/api/items/2", "PATCH", '{"priority":"urgent"}');
  await independent;
  assert.ok(backlog.calls.some(({ url }) => url === "/api/items/2"));
  let sameDone = false;
  const same = backlog.send("/api/items/1", "PATCH", '{"priority":"low"}').then(() => { sameDone = true; });
  await tick(); assert.equal(sameDone, false);
  assert.equal(backlog.calls.filter(({ url }) => url === "/api/items/1").length, 1);
  releaseOld(reply()); await same;
  assert.equal(backlog.calls.filter(({ url }) => url === "/api/items/1").length, 2);
  console.log("pass: unrelated new edits bypass a stalled recovered write while same-item edits keep their order");

  let finishFirst;
  const parallel = world({ values: new Map([[prefix() + "first", saved(1)], [prefix() + "second", saved(2)]]),
    fetchAction: async (url) => url === "/api/items/1" ? new Promise((resolve) => { finishFirst = resolve; }) : reply() });
  parallel.start(); await until(() => parallel.calls.some(({ url }) => url === "/api/items/2"));
  assert.ok(parallel.values.has(prefix() + "first")); finishFirst(reply());
  await tick();
  console.log("pass: independent recovered item queues also send without waiting for a stalled item");

  let finishStep;
  const step = world({ values: new Map([[prefix() + "old-step", saved(1, { path: "/api/steps/9/done", method: "POST", resource: "items:1" })]]),
    fetchAction: async (url) => url === "/api/steps/9/done" ? new Promise((resolve) => { finishStep = resolve; }) : reply() });
  step.start(); await until(() => finishStep);
  let itemSent = false;
  const item = step.send("/api/items/1", "PATCH").then(() => { itemSent = true; });
  await tick(); assert.equal(itemSent, false); finishStep(reply()); await item;
  console.log("pass: a recovered step answer retains its item ordering across different routes");

  const unsigned = world({ scoped: false }); await unsigned.send("/api/messages");
  assert.equal(unsigned.values.size, 0);
  console.log("pass: pages without authenticated scope keep ordinary actions in memory only");

  const helperEdits = [
    ["/api/item-pages/1/choices", "PUT"], ["/api/item-pages/1/comments", "POST"],
    ["/api/item-pages/1/comments/send", "POST"], ["/api/item-pages/1/comments/item", "POST"],
    ["/api/part-notes", "POST"], ["/api/part-notes/1", "PUT"],
    ["/api/spec", "PUT"], ["/api/spec/part", "POST"], ["/api/skills/writing/part", "POST"], ["/api/skills/writing", "PUT"],
    ["/api/team", "PUT"], ["/api/timezone", "PUT"], ["/api/timezone/report", "POST"], ["/api/timezone/keep", "POST"],
    ["/api/briefings/1/read", "POST"], ["/api/accounts/prefer", "POST"], ["/api/writing/1", "DELETE"],
  ];
  const helperValues = new Map(helperEdits.map(([url, method], index) => [prefix() + `helper-${index}`,
    saved(1, { path: url, method, headers: { "x-cockpit-action": `helper-${index}` } })]));
  const partActions = [{ method: "POST", path: /^\/api\/part-notes$/, resource: (match) => match[0] },
    { method: "PUT", path: /^\/api\/part-notes\/(\d+)$/, resource: (match) => `part-notes:${match[1]}` }];
  const recoveredHelper = world({ helper: true, values: helperValues, partActions });
  recoveredHelper.start(); await until(() => helperValues.size === 0);
  assert.deepEqual(recoveredHelper.calls.filter(({ url }) => url !== "/api/offline/session").map(({ url, options }) => [url, options.method]).sort(), helperEdits.slice().sort());
  console.log("pass: scoped helper pages recover comments, choices, a part's actions, document edits and ordinary preferences after closing");

  const channels = [], oldHelper = world({ helper: true, channels });
  const privateBody = oldHelper.document.body.children[0];
  world({ helper: true, lease: UNTIL + 1, channels, session: { user: USER, until: UNTIL + 1 } });
  new oldHelper.context.BroadcastChannel("cockpit-offline").postMessage({ user: USER, until: UNTIL + 1, kind: "switch" });
  await until(() => oldHelper.document.body.children[0].id === "offline-lock");
  await assert.rejects(oldHelper.send("/api/item-pages/1/comments"), /sign in again/);
  assert.equal(oldHelper.calls.length, 0);
  assert.equal(privateBody.hidden, true); assert.equal(privateBody.inert, true);
  assert.equal(oldHelper.document.body.children.length, 1);
  assert.equal(oldHelper.document.body.children[0].id, "offline-lock");
  assert.equal(oldHelper.document.body.children[0].children[1].href, "/");
  console.log("pass: the main page's new-login broadcast revokes the old helper and conceals its writing");

  const forgotChannels = [], forgottenHelper = world({ helper: true, channels: forgotChannels });
  new forgottenHelper.context.BroadcastChannel("cockpit-offline").postMessage({ user: USER, until: UNTIL, kind: "forget" });
  assert.equal(forgottenHelper.document.body.children[0].id, "offline-lock");
  await assert.rejects(forgottenHelper.send("/api/item-pages/1/comments"), /sign in again/);
  const changedCookie = world({ helper: true, session: { user: USER, until: UNTIL + 1 } });
  await assert.rejects(changedCookie.send("/api/item-pages/1/comments"), /sign in again|sign-in ended/);
  assert.deepEqual(changedCookie.calls.map(({ url }) => url), ["/api/offline/session"]);
  assert.equal(changedCookie.document.body.children[0].id, "offline-lock");
  assert.equal(changedCookie.values.size, 0);
  console.log("pass: sign-out conceals helper text; each helper action verifies the lease before sending");

  const delayedChannels = [];
  const currentHelper = world({ helper: true, channels: delayedChannels, lease: UNTIL + 1, session: { user: USER, until: UNTIL + 1 } });
  world({ helper: true, channels: delayedChannels, session: { user: USER, until: UNTIL + 1 } });
  await tick();
  assert.notEqual(currentHelper.document.body.children[0].id, "offline-lock");
  assert.equal(currentHelper.calls.length, 0);
  console.log("pass: loading an older helper emits no switch that could revoke a newer login");

  let started;
  const cancellable = world({ fetchAction: (url, { signal }) => new Promise((_resolve, reject) => {
    started = true; signal.addEventListener("abort", () => reject(new TypeError("aborted")), { once: true });
  }) });
  const sending = cancellable.send("/api/messages");
  await until(() => started); cancellable.clear();
  await assert.rejects(sending, /sign-in ended/); assert.equal(cancellable.values.size, 0);
  console.log("pass: clearing authentication aborts active retryable writes and erases only this login's queue");

  let replayStarted = false, restored = 0;
  const cancelledReplay = world({ values: new Map([[prefix() + "old", saved(1)]]), fetchAction: (url, { signal }) => new Promise((_resolve, reject) => {
    replayStarted = true; signal.addEventListener("abort", () => reject(new TypeError("aborted")), { once: true });
  }) });
  cancelledReplay.events.addEventListener("cockpit:not-sent", () => { restored++; });
  cancelledReplay.start(); await until(() => replayStarted);
  cancelledReplay.events.dispatchEvent(new cancelledReplay.context.CustomEvent("cockpit:offline-cleared", { detail: { user: OTHER, until: UNTIL } }));
  assert.equal(cancelledReplay.values.size, 1);
  cancelledReplay.clear(); await tick(); await tick();
  assert.equal(cancelledReplay.values.size, 0); assert.equal(restored, 0);
  console.log("pass: auth clearing during replay never resurrects its queue or restores another session's draft");
})().catch((error) => { console.error(error); process.exitCode = 1; });
