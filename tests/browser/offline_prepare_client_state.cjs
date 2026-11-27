"use strict";
const assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const owner = "a".repeat(64), until = Math.floor(Date.now() / 1000) + 3600;
const listeners = {}, timers = new Map(), messages = [], errors = [], storage = new Map();
let now = 0, timerId = 0, registered = 0, outdated = false, runningVersion = "page-build", sessionLookupFails = false;
let proxyRefusals = 0;
const request = (result) => {
  const found = {};
  queueMicrotask(() => { found.result = result; found.onsuccess?.(); });
  return found;
};
const context = vm.createContext({
  document: { documentElement: { dataset: { offlineUser: owner, offlineUntil: String(until) } }, readyState: "complete", querySelector: () => ({ content: "page-build" }), addEventListener() {} },
  navigator: { onLine: true, serviceWorker: { ready: Promise.resolve({ active: { postMessage(data, ports) { messages.push({ data, port: ports[0] }); } } }),
    async register() { registered++; }, addEventListener() {} } },
  location: { origin: "https://cockpit.test", href: "https://cockpit.test/" },
  localStorage: { getItem: (key) => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: (key) => storage.delete(key) },
  indexedDB: { open: () => request({ transaction: () => ({ objectStore: () => ({ getAll: () => request([]) }) }), close() {} }), deleteDatabase: () => request(undefined) },
  fetch: async () => {
    if (proxyRefusals > 0) {
      proxyRefusals--;
      return new Response("Proxy refused the request", { status: 403 });
    }
    if (sessionLookupFails) throw new TypeError("Failed to fetch");
    return Response.json({ user: owner, until }, { headers: { "X-Cockpit-Version": runningVersion } });
  },
  addEventListener(name, fn) { listeners[name] = fn; }, dispatchEvent() {},
  setTimeout(fn, ms) { const id = ++timerId; timers.set(id, { fn, at: now + ms }); return id; }, clearTimeout(id) { timers.delete(id); }, setInterval() {},
  MessageChannel: class { constructor() { const port = this.port1 = { closed: false, close() { this.closed = true; } }; this.port2 = { postMessage(data) { if (!port.closed) port.onmessage?.({ data }); }, owner: port }; } },
  AbortController, AbortSignal, CustomEvent, Request, Response, URL, Headers, structuredClone, crypto: require("node:crypto").webcrypto, console,
  pageOutdated: () => outdated,
  logError(place, error) { errors.push({ place, message: error.message }); },
});
context.window = context;
vm.runInContext(fs.readFileSync(path.join(__dirname, "../../src/cockpit/static/offline.js"), "utf8"), context);
const tick = () => new Promise((resolve) => setImmediate(resolve));
function advance(ms) {
  now += ms;
  for (const [id, timer] of [...timers]) if (timer.at <= now) { timers.delete(id); timer.fn(); }
}
(async () => {
  await context.CockpitOffline.ready;
  outdated = true;
  listeners.online(); await tick();
  assert.equal(registered, 0, "A page that already knows it is stale skips preparation");
  outdated = false;
  runningVersion = "new-build";
  listeners.DOMContentLoaded(); await tick();
  assert.equal(registered, 0, "The session check finds a deploy before the first board state");
  assert.equal(messages.length, 0, "A stale tab starts no download");
  assert.equal(context.CockpitOffline.status().shellReady, false);
  assert.equal(errors.length, 0);
  for (const download of [true, undefined]) {
    runningVersion = "page-build";
    listeners.DOMContentLoaded(); await tick();
    runningVersion = "new-build";
    messages.shift().port.postMessage({ error: "Could not download the offline page: Failed to fetch", download }); await tick();
    assert.equal(errors.length, 0, "A deploy during the download skips the report even with an older worker");
  }
  runningVersion = "page-build";
  listeners.online(); await tick();
  runningVersion = "new-build";
  messages.shift().port.postMessage({ error: "Could not store the offline page: Quota exceeded", download: false }); await tick();
  assert.match(errors.shift().message, /Quota exceeded/, "A deploy does not hide a storage failure");
  runningVersion = "page-build";
  proxyRefusals = 1;
  listeners.DOMContentLoaded(); await tick();
  assert.equal(context.CockpitOffline.status().stopped, false, "A failed optional version check cannot clear offline data");
  assert.equal(messages[0].data.version, "page-build");
  messages.shift().port.postMessage({ error: "Could not save offline file /static/app.js: HTTP 503", version: "previous-build", download: true });
  await tick();
  assert.equal(messages.length, 1, "A current tab asks again after joining an older save");
  assert.equal(context.CockpitOffline.status().shellReady, false, "An older save cannot confirm the current version");
  messages.shift().port.postMessage({ skipped: true, version: "new-build" }); await tick();
  assert.equal(messages.length, 0, "A version mismatch retries only once");
  assert.equal(errors.length, 0);
  runningVersion = "page-build";
  sessionLookupFails = true;
  registered = 0;
  listeners.online(); await tick();
  listeners.online(); listeners.online(); await tick();
  assert.equal(messages.length, 1); assert.equal(registered, 1);
  assert.equal(messages[0].data.supportsProgress, true, "The modern client opts in before accepting progress replies");
  advance(25000); messages[0].port.postMessage({ progress: true }); await tick();
  assert.equal(context.CockpitOffline.status().shellReady, false, "Progress alone cannot mark an incomplete shell ready");
  advance(25000); messages[0].port.postMessage({ progress: true }); await tick();
  assert.equal(errors.length, 0, "A preparation making progress for50s is still active");
  messages[0].port.postMessage({ ok: true }); await tick();
  assert.equal(context.CockpitOffline.status().shellReady, true); assert.equal(timers.size, 0);
  sessionLookupFails = false;
  console.log("pass: repeated online events share preparation; 50s of progress stays pending until complete without a false30s error");
  listeners.online(); await tick();
  advance(30000); await tick();
  assert.equal(errors.length, 1); assert.match(errors[0].message, /did not finish saving/);
  assert.equal(messages[1].port.owner.closed, true);
  console.log("pass: 30s without progress reports an error and closes the reply port");
  listeners.online(); await tick();
  await context.CockpitOffline.forget();
  messages[2].port.postMessage({ ok: true }); await tick();
  assert.equal(context.CockpitOffline.status().shellReady, false);
  assert.equal(context.CockpitOffline.status().stopped, true);
  assert.equal(timers.size, 0);
  console.log("pass: a late preparation reply after sign-out cannot restore shell-ready status");
})().catch((error) => { console.error(error); process.exitCode = 1; });
