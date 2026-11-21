"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const origin = "https://cockpit.test", owner = "a".repeat(64), until = Math.floor(Date.now() / 1000) + 3600;
const listeners = {}, stores = new Map(), network = new Map(), fetched = [];
const timers = new Map(); let nextTimer = 0;
const absolute = (input) => new URL(typeof input === "string" || input instanceof URL ? input : input.url, origin).href;
const cacheAPI = {
  async open(name) {
    if (!stores.has(name)) stores.set(name, new Map());
    const values = stores.get(name);
    return { async match(input) { return values.get(absolute(input))?.clone(); }, async put(input, response) { values.set(absolute(input), response.clone()); }, async delete(input) { return values.delete(absolute(input)); } };
  }, async keys() { return [...stores.keys()]; }, async delete(name) { return stores.delete(name); },
};
const context = vm.createContext({
  self: { location: new URL(origin), addEventListener: (name, fn) => { listeners[name] = fn; }, skipWaiting() {}, clients: { claim() {}, async matchAll() { return []; } } },
  caches: cacheAPI, URL, Response, Headers, Request, Date, Set, Promise, console, AbortSignal, performance,
  setTimeout: (callback, ms) => { const id = ++nextTimer; timers.set(id, { callback, ms }); return id; }, clearTimeout: (id) => timers.delete(id),
  fetch: async (input) => { const url = absolute(input); fetched.push(url); const answer = await network.get(url); if (answer instanceof Error) throw answer; return answer?.clone() || new Response("missing", { status: 404 }); },
});
vm.runInContext(fs.readFileSync(path.join(__dirname, "../../src/cockpit/static/offline-worker.js"), "utf8"), context);
function shell(version, asset) {
  return new Response(`<html><script src="/static/${asset}.js?v=${version}"></script><script src="/static/editor-loader.js?v=${version}"></script><link href="/static/look.css?v=${version}"></html>`,
    { headers: { "X-Cockpit-Offline-User": owner, "X-Cockpit-Offline-Until": String(until) } });
}
async function prepare() {
  let result, done;
  listeners.message({ data: { cockpitOffline: "prepare", user: owner, until, assets: ["/static/old-document.js?v=old"] },
    source: { url: origin + "/" }, ports: [{ postMessage: (value) => { result = value; } }], waitUntil: (promise) => { done = promise; } });
  await done; return result;
}
function navigate() {
  let response; const background = [];
  listeners.fetch({ request: { url: origin + "/", method: "GET", mode: "navigate" }, respondWith: (promise) => { response = promise; }, waitUntil: (promise) => background.push(promise) });
  return { response, background: () => Promise.all(background) };
}
const tick = () => new Promise((resolve) => setImmediate(resolve));
async function deadline() {
  for (let count = 0; count < 20 && !timers.size; count++) await tick();
  assert.equal(timers.size, 1);
  const [id, timer] = [...timers][0]; timers.delete(id); assert.equal(timer.ms, 1500); timer.callback();
}
(async () => {
  network.set(origin + "/", shell("new", "new-build"));
  for (const asset of ["new-build.js?v=new", "editor-loader.js?v=new", "prompt-editor.js?v=new", "font-new.woff2"]) network.set(origin + "/static/" + asset, new Response("asset", { headers: { "Cache-Control": "public, immutable" } }));
  network.set(origin + "/static/look.css?v=new", new Response('@font-face{src:url("/static/font-new.woff2")}', { headers: { "Cache-Control": "public, immutable" } }));
  assert.equal((await prepare()).ok, true);
  assert.ok(!fetched.some((url) => url.includes("old-document")), "A stale document's supplied assets must be ignored");
  const publicCache = await cacheAPI.open("cockpit-static-v1");
  for (const asset of ["new-build.js?v=new", "prompt-editor.js?v=new", "font-new.woff2"]) assert.ok(await publicCache.match("/static/" + asset));
  const saved = await cacheAPI.open(`cockpit-shell-v1-${owner}-${until}`);
  const before = await (await saved.match("/")).text();
  assert.match(before, /new-build/);
  // The new HTML is never committed when one of its own dependencies fails.
  network.set(origin + "/", shell("interrupted", "interrupted-build"));
  network.set(origin + "/static/interrupted-build.js?v=interrupted", new Response("new asset", { headers: { "Cache-Control": "public, immutable" } }));
  const interrupted = await prepare();
  assert.match(interrupted.error, /did not load/);
  assert.equal(await (await saved.match("/")).text(), before);
  network.set(origin + "/", shell("restarted", "new-build"));
  network.set(origin + "/static/new-build.js?v=restarted", new Response("replacement server's asset", { headers: { "Cache-Control": "no-cache" } }));
  await publicCache.put("/static/new-build.js?v=restarted", new Response("wrong cached version", { headers: { "Cache-Control": "no-cache" } }));
  const requestCount = fetched.length;
  const restarted = await prepare();
  assert.ok(fetched.slice(requestCount).includes(origin + "/static/new-build.js?v=restarted"), "A nonimmutable cached asset must be checked on the network");
  assert.match(restarted.error, /restarted/);
  assert.equal(await (await saved.match("/")).text(), before);
  // A normal online navigation also cannot overwrite the usable cached shell ahead of precaching.
  await navigate().response;
  assert.equal(await (await saved.match("/")).text(), before);
  console.log("pass: fetched-build assets/fonts/editor match the shell; interrupted precaching and online navigation preserve the previous complete shell");

  let finishSlow;
  network.set(origin + "/", new Promise((resolve) => { finishSlow = resolve; }));
  const slow = navigate(); await deadline();
  assert.equal(await (await slow.response).text(), before);
  finishSlow(shell("new", "new-build")); await slow.background();
  console.log("pass: a stalled cached navigation shows the complete unexpired saved shell after the bounded wait");

  let finishRejected;
  network.set(origin + "/", new Promise((resolve) => { finishRejected = resolve; }));
  const rejected = navigate(); await deadline(); await rejected.response;
  finishRejected(new Response("sign in", { status: 401 })); await rejected.background();
  assert.ok(!(await cacheAPI.keys()).some((name) => name.startsWith("cockpit-shell-v1-")));
  console.log("pass: a late authentication refusal purges the saved page after fallback");

  let finishFirst;
  network.set(origin + "/", new Promise((resolve) => { finishFirst = resolve; }));
  const firstVisit = navigate(); let settled = false;
  firstVisit.response.then(() => { settled = true; }); await tick(); await tick();
  assert.equal(timers.size, 0); assert.equal(settled, false);
  finishFirst(shell("new", "new-build")); assert.equal((await firstVisit.response).status, 200); await firstVisit.background();
  console.log("pass: an uncached navigation waits for its network response without a saved-page deadline");

  network.set(origin + "/", shell("new", "new-build"));
  network.set(origin + "/static/new-build.js?v=new", new Response("asset", { headers: { "Cache-Control": "public, immutable" } }));
  assert.equal((await prepare()).ok, true);
  network.set(origin + "/", new Response(null, { status: 302, headers: { Location: "/auth/google" } }));
  const redirect = navigate(); assert.equal((await redirect.response).status, 302); await redirect.background();
  assert.ok(!(await cacheAPI.keys()).some((name) => name.startsWith("cockpit-shell-v1-")));
  assert.equal(timers.size, 0);
  console.log("pass: an authentication redirect before the deadline is returned and purges the saved page");

  network.set(origin + "/", shell("new", "new-build")); assert.equal((await prepare()).ok, true);
  let finishOld;
  network.set(origin + "/", new Promise((resolve) => { finishOld = resolve; }));
  const olderNavigation = navigate(); await tick();
  const newerOwner = "b".repeat(64);
  network.set(origin + "/", new Response("newer account shell", { headers: { "X-Cockpit-Offline-User": newerOwner, "X-Cockpit-Offline-Until": String(until) } }));
  let prepared, preparing;
  listeners.message({ data: { cockpitOffline: "prepare", user: newerOwner, until }, source: { url: origin + "/" },
    ports: [{ postMessage: (value) => { prepared = value; } }], waitUntil: (promise) => { preparing = promise; } });
  await preparing; assert.equal(prepared.ok, true);
  await deadline(); assert.equal((await olderNavigation.response).status, 503);
  finishOld(new Response("old sign-in refused", { status: 401 })); await olderNavigation.background();
  assert.equal(await (await (await cacheAPI.open(`cockpit-shell-v1-${newerOwner}-${until}`)).match("/")).text(), "newer account shell");
  console.log("pass: an old navigation deadline and late auth refusal preserve a newer account's saved shell");
})().catch((error) => { console.error(error); process.exitCode = 1; });
