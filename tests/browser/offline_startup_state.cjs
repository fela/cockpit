"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const owner = "a".repeat(64), until = Math.floor(Date.now() / 1000) + 3600;
const original = `${"b".repeat(64)}-${until}`, newer = `${"c".repeat(64)}-${until + 1}`;
const storage = new Map([["cockpit.offline-owner", original]]), broadcasts = [];
let release, opened = 0, removed = 0;
const context = vm.createContext({
  document: { documentElement: { dataset: { offlineUser: owner, offlineUntil: String(until) } }, readyState: "complete", querySelector: () => null, addEventListener() {} },
  navigator: { onLine: true }, location: { origin: "https://cockpit.test", href: "https://cockpit.test/" },
  localStorage: { getItem: (key) => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: (key) => storage.delete(key) },
  indexedDB: { open() { opened++; throw new Error("The stale page must not open a database"); }, deleteDatabase() { removed++; throw new Error("The stale page must not delete a database"); } },
  BroadcastChannel: class { postMessage(data) { broadcasts.push(data); } },
  fetch: () => new Promise((resolve) => { release = resolve; }), addEventListener() {}, dispatchEvent() {},
  setTimeout, clearTimeout, setInterval() {}, AbortController, CustomEvent, Request, Response, URL, Headers,
  structuredClone, crypto: require("node:crypto").webcrypto, console,
});
context.window = context;
vm.runInContext(fs.readFileSync(path.join(__dirname, "../../src/cockpit/static/offline.js"), "utf8"), context);
(async () => {
  assert.equal(typeof release, "function");
  // The old page's matching verification was sent first; another sign-in became current before it returned.
  storage.set("cockpit.offline-owner", newer);
  release(Response.json({ user: owner, until }));
  await context.CockpitOffline.ready;
  assert.equal(context.CockpitOffline.status().stopped, true);
  assert.equal(storage.get("cockpit.offline-owner"), newer);
  assert.equal(opened, 0); assert.equal(removed, 0); assert.equal(broadcasts.length, 0);
  context.navigator.onLine = false;
  await assert.rejects(context.CockpitOffline.read("/api/example/secret/now", () => new Promise(() => {})), /Connect to Cockpit/);
  console.log("pass: delayed old-session startup cannot replace/delete/broadcast over a newer owner; offline uncached reads fail immediately");
})().catch((error) => { console.error(error); process.exitCode = 1; });
