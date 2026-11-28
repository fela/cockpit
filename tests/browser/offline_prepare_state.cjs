"use strict";
const assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const origin = "https://cockpit.test", owner = "a".repeat(64), until = Math.floor(Date.now() / 1000) + 3600;
const listeners = {}, stores = new Map(), fetched = [];
let pending = 0, peak = 0, unavailable = 0, version = "one", refuse = "", held = null, stall = "";
let dropShell = 0, dropShellBody = 0, dropAssetBody = 0, offlineNavigation = false, readingOwner = null, writingOwner = null;
let assetGate = null;
let workerNow = 0, shellFailureTime = 0, holdRetry = false, bootFailures = 0;
const cacheFailures = new Set();
const stalled = [], deadlines = [], shellRetries = [];
const absolute = (input) => new URL(typeof input === "string" || input instanceof URL ? input : input.url, origin).href;
const caches = {
  async open(name) {
    if (!stores.has(name)) stores.set(name, new Map());
    const values = stores.get(name);
    return { async match(input) {
      const url = absolute(input);
      if (url === origin + "/__offline_owner__" && readingOwner) await readingOwner();
      return values.get(url)?.clone();
    }, async put(input, response) {
      const url = absolute(input);
      if (cacheFailures.delete(url)) throw new TypeError("Cache.put() encountered a network error");
      if (url === origin + "/__offline_owner__" && writingOwner) await writingOwner();
      const saved = response.clone();
      await response.arrayBuffer();
      values.set(url, saved);
    }, async delete(input) { return values.delete(absolute(input)); } };
  }, async keys() { return [...stores.keys()]; }, async delete(name) { return stores.delete(name); },
};
const context = vm.createContext({
  self: { location: new URL(origin), addEventListener(name, fn) { listeners[name] = fn; }, skipWaiting() {}, clients: { claim() {}, async matchAll() { return []; } } },
  caches, URL, Response, Headers, Request, Date, Set, Promise, console, AbortSignal: { any: AbortSignal.any.bind(AbortSignal), timeout(ms) {
    assert.equal(ms, 20000);
    const controller = new AbortController(); deadlines.push(controller); return controller.signal;
  } },
  setTimeout(callback, ms) {
    if (ms === 2000 && holdRetry) shellRetries.push(callback);
    else return setTimeout(callback, ms === 2000 ? 0 : ms);
  }, clearTimeout, performance: { now: () => workerNow },
  async fetch(input, options) {
    const url = new URL(absolute(input)); fetched.push(url.href);
    if (input.mode === "navigate" && offlineNavigation) throw new TypeError("Failed to fetch");
    if (url.pathname === "/" && dropShell) {
      dropShell--;
      workerNow += shellFailureTime;
      throw new TypeError("Failed to fetch");
    }
    if (url.pathname === "/") {
      const response = new Response(`<meta name="cockpit-version" content="${version}">` +
        Array.from({ length: 48 }, (_, i) => `<script src="/static/file-${i}.js?v=${version}"></script>`).join(""),
        { headers: { "X-Cockpit-Offline-User": owner, "X-Cockpit-Offline-Until": String(until) } });
      if (dropShellBody > 0) {
        dropShellBody--;
        response.blob = async () => { throw new TypeError("Failed to fetch"); };
      }
      return response;
    }
    pending++; peak = Math.max(peak, pending);
    try {
      if (url.pathname === stall) await new Promise((resolve, reject) => {
        stalled.push(options.signal);
        options.signal.addEventListener("abort", () => reject(options.signal.reason), { once: true });
      });
      if (held) await held;
      if (assetGate) await assetGate(url);
      await new Promise((resolve) => setTimeout(resolve, 5));
      if (url.pathname === "/static/file-0.js" && bootFailures) {
        bootFailures--;
        return new Response("bad gateway", { status: 502 });
      }
      if (pending > 8 || url.pathname === refuse) { unavailable++; return new Response("overloaded", { status: 503 }); }
      if (url.pathname === "/static/file-0.js" && dropAssetBody > 0) {
        dropAssetBody--;
        const body = new ReadableStream({ start(controller) { controller.error(new TypeError("Interrupted asset")); } });
        return new Response(body, { headers: { "Cache-Control": "public, immutable" } });
      }
      return new Response("asset", { headers: { "Cache-Control": "public, immutable" } });
    } finally { pending--; }
  },
});
vm.runInContext(fs.readFileSync(process.env.OFFLINE_WORKER_SOURCE || path.join(__dirname, "../../src/cockpit/static/offline-worker.js"), "utf8"), context);
function message(data) {
  let done, result, progress = 0;
  listeners.message({ data, source: { url: origin + "/" }, ports: [{ postMessage(value) { if (value.progress) progress++; else result = value; } }], waitUntil(value) { done = value; } });
  return { get progress() { return progress; }, done: () => Promise.resolve(done).then(() => ({ ...result, progress })) };
}
const prepare = (pageVersion) => message({
  cockpitOffline: "prepare", user: owner, until, version: pageVersion, supportsProgress: true,
});
function legacyPrepare(capability) {
  const data = { cockpitOffline: "prepare", user: owner, until };
  if (capability !== undefined) data.supportsProgress = capability;
  const state = { settled: false, ready: false, error: null };
  let done;
  listeners.message({ data, source: { url: origin + "/" }, ports: [{ postMessage(value) {
    // Old offline.js treats the first non-error message as final success and closes its port.
    if (state.settled) return;
    state.settled = true; state.ready = !value.error; state.error = value.error || null;
  } }], waitUntil(value) { done = value; } });
  return { state, done: () => done };
}
(async () => {
  const results = await Promise.all([prepare().done(), prepare().done(), prepare().done()]);
  assert.ok(results.every((result) => result.ok), `All tabs must save without capacity errors: ${JSON.stringify({ peak, unavailable, results })}`);
  assert.equal(peak, 3); assert.equal(unavailable, 0);
  assert.equal(fetched.filter((url) => url.includes("/static/")).length, 48);
  assert.equal(fetched.filter((url) => url === origin + "/").length, 1);
  console.log("pass: three tabs share one shell preparation; 48 dependencies use at most three concurrent downloads");
  const shellCache = await caches.open(`cockpit-shell-v1-${owner}-${until}`);
  const count = fetched.length;
  assert.equal((await prepare().done()).ok, true);
  assert.equal(fetched.length - count, 1, "An unchanged shell reuses its immutable versioned dependencies");
  console.log("pass: repeat preparation verifies the authenticated shell and makes zero asset network requests");
  let release;
  for (const fails of [false, true]) {
    version = fails ? "mixed-failure" : "mixed-success";
    refuse = fails ? "/static/file-0.js" : "";
    held = new Promise((resolve) => { release = resolve; });
    const count = fetched.length;
    const old = legacyPrepare(undefined), disabled = legacyPrepare(false), modern = prepare();
    for (let i = 0; i < 50 && pending !== 3; i++) await new Promise((resolve) => setTimeout(resolve, 1));
    assert.equal(pending, 3);
    assert.ok(modern.progress > 0, "A modern client receives progress while assets are still pending");
    assert.equal(old.state.settled, false, "An old client must keep waiting instead of treating progress as success");
    assert.equal(disabled.state.settled, false, "An explicit false capability must also receive final replies only");
    assert.equal(fetched.slice(count).filter((url) => url === origin + "/").length, 1, "Mixed clients share one preparation");
    held = null; release();
    const [result] = await Promise.all([modern.done(), old.done(), disabled.done()]);
    if (fails) {
      assert.match(result.error, /file-0\.js.*HTTP 503/);
      assert.equal(old.state.ready, false); assert.equal(disabled.state.ready, false);
      assert.equal(old.state.error, result.error); assert.equal(disabled.state.error, result.error);
    } else {
      assert.equal(result.ok, true);
      assert.equal(old.state.ready, true); assert.equal(disabled.state.ready, true);
      assert.equal(old.state.error, null); assert.equal(disabled.state.error, null);
    }
    console.log(`pass: mixed old/new clients share preparation; only opted-in ports receive progress and all receive final ${fails ? "failure" : "success"}`);
  }
  const beforeDeploy = await (await shellCache.match("/")).text();
  assert.match(beforeDeploy, /mixed-success/, "A later failed mixed-client save preserves the complete successful shell");
  version = "deployed"; refuse = "";
  const fetchesBeforeDeploy = fetched.length;
  assert.deepEqual(await prepare("mixed-success").done(), { skipped: true, version, progress: 0 });
  assert.deepEqual(fetched.slice(fetchesBeforeDeploy), [origin + "/"], "A stale tab stops before downloading assets");
  assert.equal(await (await shellCache.match("/")).text(), beforeDeploy, "Skipping retains the last complete page");
  const requestedBeforeDeploy = fetched.length;
  const [stale, current] = await Promise.all([prepare("mixed-success").done(), prepare(version).done()]);
  assert.equal(stale.version, "deployed");
  assert.equal(current.ok, true, "A current tab can save while a stale tab is checking");
  assert.equal(fetched.slice(requestedBeforeDeploy).filter((url) => url === origin + "/").length, 1);
  const previous = await (await shellCache.match("/")).text();
  assert.match(previous, /deployed/);
  console.log("pass: a lone stale tab skips the offline save; a current tab can share the same job and save");

  version = "two"; refuse = "/static/file-5.js";
  const failed = await prepare().done();
  assert.match(failed.error, /file-5\.js.*HTTP 503/);
  assert.equal(failed.download, true);
  assert.equal(failed.version, "two", "An asset download failure names the shell it was serving");
  assert.equal(await (await shellCache.match("/")).text(), previous);
  console.log("pass: genuine HTTP503 reports its status and preserves the previous complete shell");
  version = "stalled"; refuse = ""; stall = "/static/file-0.js";
  const stuck = prepare();
  while (!stalled.length) await new Promise((resolve) => setTimeout(resolve, 1));
  const expire = (signal) => deadlines.find((controller) => controller.signal === signal)
    .abort(new DOMException("Asset timed out", "TimeoutError"));
  expire(stalled[0]);
  while (stalled.length < 2) await new Promise((resolve) => setTimeout(resolve, 1));
  expire(stalled[1]);
  assert.match((await stuck.done()).error, /timed out/);
  assert.equal(await (await shellCache.match("/")).text(), previous);
  assert.equal(pending, 0);
  stall = "";
  assert.equal((await prepare().done()).ok, true, "A timed-out fetch releases its network slot for a retry");
  console.log("pass: a stalled fetch deadline reports failure, keeps the complete shell, releases all network slots, and allows retry");
  version = "proxy-recovered"; bootFailures = 1; holdRetry = true;
  assetGate = async (url) => {
    if (bootFailures && url.pathname === "/static/file-0.js") workerNow += 3000;
  };
  const proxyAsset = `${origin}/static/file-0.js?v=${version}`;
  const savingThroughProxy = prepare();
  for (let i = 0; i < 50 && !shellRetries.length; i++) await new Promise((resolve) => setTimeout(resolve, 1));
  assert.equal(fetched.filter((url) => url === proxyAsset).length, 1, "A 502 must pause before the retry");
  assert.equal(shellRetries.length, 1);
  shellRetries.shift()();
  assert.equal((await savingThroughProxy.done()).ok, true);
  assert.equal(fetched.filter((url) => url === proxyAsset).length, 2);
  assetGate = null;
  console.log("pass: proxy 502 pauses before retrying the offline asset even after a long first attempt");
  version = "recovered"; dropShell = 1;
  const recoveredAsset = `${origin}/static/file-0.js?v=${version}`;
  const ownerMarker = origin + "/__offline_owner__";
  cacheFailures.add(recoveredAsset);
  cacheFailures.add(ownerMarker);
  const recovering = fetched.length;
  const retrying = prepare();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(fetched.slice(recovering).filter((url) => url === origin + "/").length, 1);
  assert.equal(shellRetries.length, 1, "The second fetch must wait instead of hitting the same brief outage");
  const olderTab = legacyPrepare();
  assert.equal(olderTab.state.settled, false);
  shellRetries.shift()();
  assert.equal((await retrying.done()).ok, true);
  await olderTab.done();
  assert.equal(olderTab.state.ready, true);
  assert.equal(fetched.slice(recovering).filter((url) => url === origin + "/").length, 2);
  assert.equal(fetched.slice(recovering).filter((url) => url === recoveredAsset).length, 2);
  assert.match(await (await shellCache.match("/")).text(), /recovered/);
  assert.equal(cacheFailures.size, 0);
  assert.equal(await vm.runInContext("active = null; current()", context), `${owner}-${until}`);
  console.log("pass: old and new tabs share one delayed retry and receive success; interrupted cache writes recover");
  dropShell = 2;
  const beforeFailure = await (await shellCache.match("/")).text(), missing = prepare(version);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(shellRetries.length, 1);
  shellRetries.shift()();
  const missingResult = await missing.done();
  assert.match(missingResult.error, /Could not download the offline page: Failed to fetch/);
  assert.equal(missingResult.download, true);
  assert.equal(missingResult.version, version, "A failure before the page loads names the initiating tab's build");
  assert.equal(shellRetries.length, 0, "A second failed fetch is reported");
  assert.equal(await (await shellCache.match("/")).text(), beforeFailure);
  console.log("pass: a second failed root fetch reports the problem and preserves the last complete page");
  dropShell = 1; shellFailureTime = 20000;
  const slowLegacy = legacyPrepare();
  await slowLegacy.done();
  assert.equal(shellRetries.length, 0);
  assert.equal(slowLegacy.state.ready, true, "A slow first attempt must not add a wait to the retry");
  shellFailureTime = 0; holdRetry = false;
  console.log("pass: a slow first fetch does not take extra time from the old page's wait limit");
  dropShellBody = 2;
  const interrupted = await prepare().done();
  assert.match(interrupted.error, /Could not download the offline page/);
  assert.equal(interrupted.download, true, "An interrupted response body is a download failure");
  assert.equal(await (await shellCache.match("/")).text(), beforeFailure);
  version = "interrupted-asset"; dropAssetBody = 2;
  const assetInterrupted = await prepare(version).done();
  assert.match(assetInterrupted.error, /file-0\.js.*Interrupted asset/);
  assert.equal(assetInterrupted.download, true, "A broken script response body is a download failure too");
  assert.equal(assetInterrupted.version, version);
  assert.equal(await (await shellCache.match("/")).text(), beforeFailure);
  version = "priority";
  const heldRetries = new Promise((resolve) => { release = resolve; });
  const attempts = new Map();
  assetGate = async (url) => {
    if (!/^\/static\/file-[012]\.js$/.test(url.pathname)) return;
    const count = (attempts.get(url.pathname) || 0) + 1;
    attempts.set(url.pathname, count);
    if (count === 1) throw new TypeError("Failed to fetch");
    await heldRetries;
  };
  const orderStart = fetched.length, background = prepare();
  for (let i = 0; i < 50 && [...attempts.values()].filter((count) => count === 2).length < 3; i++) await new Promise((resolve) => setTimeout(resolve, 1));
  assert.equal([...attempts.values()].filter((count) => count === 2).length, 3);
  assert.equal(pending, 3);
  const live = Array.from({ length: 12 }, (_, i) => {
    let response;
    listeners.fetch({ request: new Request(`${origin}/static/live-${i}.js?v=priority`), respondWith(value) { response = value; } });
    return response;
  });
  let liveDeadline;
  try {
    await Promise.race([Promise.all(live), new Promise((_, reject) => {
      liveDeadline = setTimeout(() => reject(new Error("Visible scripts waited for background retries")), 1000);
    })]);
  } finally { clearTimeout(liveDeadline); release(); }
  assetGate = null;
  assert.equal((await background.done()).ok, true);
  const order = fetched.slice(orderStart);
  assert.ok(order.indexOf(`${origin}/static/live-0.js?v=priority`) < order.indexOf(`${origin}/static/file-4.js?v=priority`),
    "A visible script waits only for the current preparation batch, not all48 background downloads");
  assert.equal(peak, 4);
  console.log("pass: visible scripts can finish while all background retries are still waiting");
  version = "three"; held = new Promise((resolve) => { release = resolve; });
  const saving = prepare();
  for (let i = 0; i < 50 && pending !== 3; i++) await new Promise((resolve) => setTimeout(resolve, 1));
  await message({ cockpitOffline: "forget", scope: `${owner}-${until}` }).done();
  held = null; release();
  assert.match((await saving.done()).error, /sign-in changed/);
  assert.equal(peak, 4);
  assert.ok(!(await caches.keys()).some((name) => name.startsWith("cockpit-shell-v1-")));
  assert.equal(await (await caches.open("cockpit-static-v1")).match("/__offline_owner__"), undefined);
  console.log("pass: sign-out during preparation leaves no shell or owner marker");

  let releaseOwner;
  readingOwner = () => {
    readingOwner = null;
    return new Promise((resolve) => { releaseOwner = resolve; });
  };
  const checkingOwner = prepare();
  while (!releaseOwner) await new Promise((resolve) => setTimeout(resolve, 1));
  await message({ cockpitOffline: "forget", scope: `${owner}-${until}` }).done();
  releaseOwner();
  assert.match((await checkingOwner.done()).error, /sign-in changed/);
  assert.ok(!(await caches.keys()).some((name) => name.startsWith("cockpit-shell-v1-")));
  console.log("pass: a save already checking the owner cannot commit after sign-out");

  assert.equal((await prepare().done()).ok, true);
  version = "committing";
  let releaseMarker;
  writingOwner = () => {
    writingOwner = null;
    return new Promise((resolve) => { releaseMarker = resolve; });
  };
  const committing = prepare();
  while (!releaseMarker) await new Promise((resolve) => setTimeout(resolve, 1));
  const forgetting = message({ cockpitOffline: "forget", scope: `${owner}-${until}` }).done();
  try {
    assert.match((await prepare().done()).error, /sign-in changed/);
    offlineNavigation = true;
    let navigation;
    listeners.fetch({ request: { url: origin + "/", method: "GET", mode: "navigate" }, waitUntil() {},
      respondWith(value) { navigation = value; } });
    assert.equal((await navigation).status, 503, "Sign-out blocks offline fallback before waiting for cache cleanup");
  } finally {
    offlineNavigation = false;
    releaseMarker();
  }
  const [cancelled, forgotten] = await Promise.all([committing.done(), forgetting]);
  assert.match(cancelled.error, /sign-in changed/);
  assert.equal(forgotten.ok, true);
  assert.ok(!(await caches.keys()).some((name) => name.startsWith("cockpit-shell-v1-")));
  assert.equal(await (await caches.open("cockpit-static-v1")).match(ownerMarker), undefined);
  console.log("pass: sign-out blocks offline reads and writes while a previous save is being cleared");
})().catch((error) => { console.error(error); process.exitCode = 1; });
