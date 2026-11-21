"use strict";
const assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const owner = "a".repeat(64), until = Math.floor(Date.now() / 1000) + 3600;
const listeners = {}, timers = new Map(), messages = [], errors = [], storage = new Map();
let now = 0, timerId = 0, registered = 0;
const request = (result) => {
  const found = {};
  queueMicrotask(() => { found.result = result; found.onsuccess?.(); });
  return found;
};
const context = vm.createContext({
  document: { documentElement: { dataset: { offlineUser: owner, offlineUntil: String(until) } }, readyState: "complete", addEventListener() {} },
  navigator: { onLine: true, serviceWorker: { ready: Promise.resolve({ active: { postMessage(data, ports) { messages.push({ data, port: ports[0] }); } } }),
    async register() { registered++; }, addEventListener() {} } },
  location: { origin: "https://cockpit.test", href: "https://cockpit.test/" },
  localStorage: { getItem: (key) => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: (key) => storage.delete(key) },
  indexedDB: { open: () => request({ transaction: () => ({ objectStore: () => ({ getAll: () => request([]) }) }), close() {} }), deleteDatabase: () => request(undefined) },
  fetch: async () => Response.json({ user: owner, until }),
  addEventListener(name, fn) { listeners[name] = fn; }, dispatchEvent() {},
  setTimeout(fn, ms) { const id = ++timerId; timers.set(id, { fn, at: now + ms }); return id; }, clearTimeout(id) { timers.delete(id); }, setInterval() {},
  MessageChannel: class { constructor() { const port = this.port1 = { closed: false, close() { this.closed = true; } }; this.port2 = { postMessage(data) { if (!port.closed) port.onmessage?.({ data }); }, owner: port }; } },
  AbortController, AbortSignal, CustomEvent, Request, Response, URL, Headers, structuredClone, crypto: require("node:crypto").webcrypto, console,
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
  listeners.DOMContentLoaded(); await tick();
  listeners.online(); listeners.online(); await tick();
  assert.equal(messages.length, 1); assert.equal(registered, 1);
  assert.equal(messages[0].data.supportsProgress, true, "The modern client opts in before accepting progress replies");
  advance(25000); messages[0].port.postMessage({ progress: true }); await tick();
  assert.equal(context.CockpitOffline.status().shellReady, false, "Progress alone cannot mark an incomplete shell ready");
  advance(25000); messages[0].port.postMessage({ progress: true }); await tick();
  assert.equal(errors.length, 0, "A preparation making progress for50s is still active");
  messages[0].port.postMessage({ ok: true }); await tick();
  assert.equal(context.CockpitOffline.status().shellReady, true); assert.equal(timers.size, 0);
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
