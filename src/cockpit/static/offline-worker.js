"use strict";

// Only the signed-in app shell and its public assets live here. Personal JSON and edits belong to offline.js.
const PREFIX = "cockpit-shell-v1-";
const STATIC = "cockpit-static-v1";
const SCOPE = /^[a-f0-9]{64}$/;
const DOWNLOAD_RETRY_MS = 2000;
let active = null;
let generation = 0;
let cacheChanges = Promise.resolve();
let clearing = 0;
const preparing = new Map();
let assetDownloads = 0;
// Safari's reload reaches this worker with no reload mark, so the page says a reload is coming (UI-13).
let reloadAnnounced = 0;
const assetWaiters = [];

function fillAssetSlots() {
  while (assetDownloads < 4) {
    const next = assetWaiters.findIndex(({ background }) => !background || assetDownloads < 3);
    if (next < 0) return;
    assetDownloads++;
    assetWaiters.splice(next, 1)[0].ready();
  }
}

async function assetSlot(background, run) {
  await new Promise((ready) => { assetWaiters.push({ background, ready }); fillAssetSlots(); });
  try { return await run(); }
  finally { assetDownloads--; fillAssetSlots(); }
}

async function readDownload(read) {
  try { return await read(); }
  catch (error) { throw Object.assign(error, { download: true }); }
}

async function saveWithRetry(what, progress, retrySpacing, save) {
  for (let attempt = 0; ; attempt++) {
    const startedAt = performance.now();
    try { return await save(); }
    catch (error) {
      const unavailable = [502, 503, 504].includes(error.status);
      const transient = unavailable || ["TypeError", "TimeoutError", "AbortError", "NetworkError"].includes(error.name);
      if (attempt || !transient) {
        throw Object.assign(new Error(`${what}: ${error.message}`, { cause: error }), { download: error.download });
      }
      progress();
      const wait = unavailable ? DOWNLOAD_RETRY_MS : retrySpacing - (performance.now() - startedAt);
      if (wait > 0) await new Promise((resolve) => setTimeout(resolve, wait));
    }
  }
}

async function current() {
  if (active) return active;
  const cache = await caches.open(STATIC);
  const stored = await cache.match(new URL("/__offline_owner__", self.location.origin));
  active = stored ? await stored.text() : null;
  return active;
}

function changeSavedCache(change) {
  const changed = cacheChanges.then(change);
  cacheChanges = changed.catch(() => {});
  return changed;
}

function forget(scope) {
  generation++;
  clearing++;
  return changeSavedCache(async () => {
    const present = await current();
    for (const name of await caches.keys()) if (name.startsWith(PREFIX) && (!scope || name === PREFIX + scope)) await caches.delete(name);
    if (!scope || scope === present) {
      active = null;
      await (await caches.open(STATIC)).delete(new URL("/__offline_owner__", self.location.origin));
    }
  }).finally(() => { clearing--; });
}

async function tellForgotten() {
  for (const client of await self.clients.matchAll({ type: "window" })) client.postMessage({ cockpitOffline: "forgotten" });
}

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("message", (event) => {
  const data = event.data;
  if (!data || !event.source?.url || new URL(event.source.url).origin !== self.location.origin) return;
  if (data.cockpitOffline === "reloading") {
    reloadAnnounced = Date.now() + 10000;
    event.ports[0]?.postMessage({ ok: true });
    return;
  }
  if (data.cockpitOffline === "forget") {
    event.waitUntil(forget(data.scope).then(() => event.ports[0]?.postMessage({ ok: true })));
    return;
  }
  if (data.cockpitOffline !== "prepare" || !SCOPE.test(data.user)) return;
  if (clearing) {
    event.ports[0]?.postMessage({ error: "The sign-in changed while saving the offline shell" });
    return;
  }
  // Tabs share a worker and an authenticated partition; one preparation serves all of them.
  const key = `${generation}:${data.user}-${data.until}`;
  let job = preparing.get(key);
  if (!job) {
    job = { ports: new Map(), version: data.version };
    const started = generation;
    job.promise = prepare(data, started, () => {
      for (const [port, page] of job.ports) if (page.supportsProgress) port.postMessage({ progress: true });
    }, (version) => {
      job.version = version;
      return !job.ports.size || [...job.ports.values()].some((page) => !page.version || page.version === version);
    })
      .finally(() => preparing.delete(key));
    preparing.set(key, job);
  }
  const port = event.ports[0];
  // Older pages treat every reply as final; each port explicitly opts in to progress.
  if (port) job.ports.set(port, { supportsProgress: data.supportsProgress === true, version: data.version });
  event.waitUntil(job.promise.then((result) => port?.postMessage(result),
    (error) => port?.postMessage({ error: error.message, download: error.download === true, version: job.version }))
    .finally(() => job.ports.delete(port)));
});

async function prepare(data, started, progress, requested) {
  const scope = `${data.user}-${data.until}`;
  if (generation !== started) throw new Error("The sign-in changed while saving the offline shell");
  // Re-fetch the shell with cookies. Its authenticated response must name this page's owner before caching it.
  const { body, init } = await saveWithRetry("Could not download the offline page", progress, DOWNLOAD_RETRY_MS, async () => {
    if (generation !== started) throw new Error("The sign-in changed while saving the offline shell");
    const shell = await readDownload(() => fetch("/", { cache: "no-store", redirect: "manual", signal: AbortSignal.timeout(20000) }));
    if ([502, 503, 504].includes(shell.status)) {
      throw Object.assign(new Error(`HTTP ${shell.status}`), { status: shell.status, download: true });
    }
    if (!shell.ok || shell.headers.get("X-Cockpit-Offline-User") !== data.user
        || Number(shell.headers.get("X-Cockpit-Offline-Until")) !== data.until
        || !(data.until * 1000 > Date.now())) throw new Error("Offline shell needs the current sign-in");
    const headers = new Headers(shell.headers);
    for (const name of ["Content-Encoding", "Content-Length", "Transfer-Encoding"]) headers.delete(name);
    const body = await readDownload(() => shell.blob());
    return { body, init: { headers, status: shell.status, statusText: shell.statusText } };
  });
  if (generation !== started) throw new Error("The sign-in changed while saving the offline shell");
  const shellText = await body.text();
  const version = shellText.match(/<meta name="cockpit-version" content="([^"]+)">/)?.[1];
  if (generation !== started) throw new Error("The sign-in changed while saving the offline shell");
  if (version && !requested(version)) return { skipped: true, version };
  progress();
  const old = await current();
  if (generation !== started) throw new Error("The sign-in changed while saving the offline shell");
  const changingOwner = old && old !== scope;
  const saving = started + (changingOwner ? 1 : 0);
  if (changingOwner) await forget();
  if (generation !== saving) throw new Error("The sign-in changed while saving the offline shell");
  const publicCache = await caches.open(STATIC);
  const assets = new Set([...shellText.matchAll(/(?:src|href)=["'](\/static\/[^"']+)["']/g)].map((match) => new URL(match[1], self.location.origin).href));
  const editor = [...assets].find((path) => new URL(path).pathname === "/static/editor-loader.js");
  if (editor) { const url = new URL(editor); url.pathname = "/static/prompt-editor.js"; assets.add(url.href); }
  // Follow local stylesheet dependencies, so the fonts belong to the fetched shell's own stylesheet version.
  const saved = new Set();
  while ([...assets].some((url) => !saved.has(url))) {
    const batch = [...assets].filter((url) => !saved.has(url)).slice(0, 3);
    // Leave one of the shared download slots free for a visible page.
    const results = await Promise.allSettled(batch.map((path) => saveWithRetry(
      `Could not save offline file ${new URL(path).pathname}`, progress, 0, () => assetSlot(true, async () => {
      if (generation !== saving) throw new Error("The sign-in changed while saving the offline shell");
      const url = new URL(path);
      if (url.origin !== self.location.origin || !url.pathname.startsWith("/static/")) return;
      const cached = url.searchParams.has("v") && await publicCache.match(url.href);
      const immutable = (response) => /(?:^|,)\s*immutable\s*(?:,|$)/.test(response.headers.get("Cache-Control") || "");
      const response = cached?.ok && immutable(cached) ? cached
        : await readDownload(() => fetch(url.href, { cache: "default", signal: AbortSignal.timeout(20000) }));
      if (!response.ok) {
        throw Object.assign(new Error(`Offline asset did not load (HTTP ${response.status})`), {
          status: response.status, download: true,
        });
      }
      if (url.searchParams.has("v") && !immutable(response)) {
        throw Object.assign(new Error("Cockpit restarted while saving offline files. Reload to save the current version."), {
          download: true,
        });
      }
      if (url.pathname.endsWith(".css")) {
        const text = response === cached ? await response.clone().text() : await readDownload(() => response.clone().text());
        for (const match of text.matchAll(/url\(\s*["']?([^"')\s]+)["']?\s*\)/g)) {
          const dependency = new URL(match[1], url);
          if (dependency.origin === self.location.origin && dependency.pathname.startsWith("/static/")) assets.add(dependency.href);
        }
      } else if (response !== cached) {
        // Finish the network read separately so Cache.put failures still identify local storage errors.
        await readDownload(() => response.clone().blob());
      }
      if (response !== cached) await publicCache.put(url.href, response);
      saved.add(path);
      progress();
    }))));
    const failures = results.filter((result) => result.status === "rejected").map((result) => result.reason);
    if (failures.length) throw failures.find((error) => !error.download) ?? failures[0];
  }
  await changeSavedCache(async () => {
    const stillSignedIn = () => {
      if (generation !== saving) throw new Error("The sign-in changed while saving the offline shell");
    };
    stillSignedIn();
    const shellCache = await caches.open(PREFIX + scope);
    const ownerKey = new URL("/__offline_owner__", self.location.origin);
    try {
      await saveWithRetry("Could not store the offline page", progress, 0, () => {
        stillSignedIn();
        return shellCache.put("/", new Response(body, init));
      });
      await saveWithRetry("Could not store the offline sign-in", progress, 0, () => {
        stillSignedIn();
        return publicCache.put(ownerKey, new Response(scope));
      });
      stillSignedIn();
      active = scope;
    } catch (error) {
      if (generation !== saving && old !== scope) {
        await caches.delete(PREFIX + scope);
        const marker = await publicCache.match(ownerKey);
        if (await marker?.text() === scope) await publicCache.delete(ownerKey);
        if (active === scope) active = null;
      }
      throw error;
    }
  });
  return version ? { ok: true, version } : { ok: true };
}

self.addEventListener("fetch", (event) => {
  const request = event.request, url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname === "/auth/sign-out") {
    event.respondWith((async () => {
      clearing++;
      try {
        await forget();
        await tellForgotten();
        try { return await fetch(request); }
        catch { return new Response("Signed out on this device. Connect again to finish signing out.", { status: 503 }); }
      } finally { clearing--; }
    })());
    return;
  }
  if (request.method !== "GET") return;
  if (request.mode === "navigate" && url.pathname === "/") {
    const started = generation;
    // A reload, by the page's Reload key (UI-13) or the browser's, asks for the running Cockpit. Chromium and Firefox
    // mark it with this cache mode; the page announces its own. The saved page is the version from before a deploy,
    // so a reload waits longer for a slow server.
    const reloading = request.cache === "no-cache" || request.cache === "reload" || Date.now() < reloadAnnounced;
    reloadAnnounced = 0;
    let allowSaved = !clearing;
    const online = (async () => {
        const response = await fetch(request, { redirect: "manual" });
        if (generation !== started) return response;
        const user = response.headers.get("X-Cockpit-Offline-User");
        if (response.type === "opaqueredirect" || response.redirected || (response.status >= 300 && response.status < 400) || response.status === 401 || response.status === 403 || (response.ok && !SCOPE.test(user || ""))) {
          allowSaved = false;
          await forget(); await tellForgotten();
        } else if (response.ok && SCOPE.test(user)) {
          const scope = `${user}-${response.headers.get("X-Cockpit-Offline-Until")}`;
          const old = await current();
          if (generation !== started) return response;
          if (old !== scope) generation++;
          if (old && old !== scope) { allowSaved = false; await forget(); await tellForgotten(); }
        }
        return response;
    })();
    // A late auth response still clears the saved page after a slow navigation has shown it.
    event.waitUntil(online.then(() => {}, () => {}));
    event.respondWith((async () => {
      const scope = allowSaved ? await current() : null;
      const cached = scope && await (await caches.open(PREFIX + scope)).match("/");
      const expires = Number(cached?.headers.get("X-Cockpit-Offline-Until"));
      const valid = cached && `${cached.headers.get("X-Cockpit-Offline-User")}-${expires}` === scope && expires * 1000 > Date.now();
      let timer;
      let unreachable = null;
      try {
        if (!valid) return await online;
        const wait = reloading ? 10000 : 1500;
        const response = await Promise.race([online, new Promise((resolve) => { timer = setTimeout(() => resolve(null), wait); })]);
        // The proxy's answer while Cockpit restarts or cannot be reached: the saved page is more use than its error page.
        if (response && response.status >= 502 && response.status <= 504) unreachable = response;
        else if (response) return response;
      } catch {
        // An unavailable network uses the same complete saved shell as the bounded wait.
      } finally {
        if (timer !== undefined) clearTimeout(timer);
      }
      if (valid && allowSaved && await current() === scope && !clearing && generation === started && expires * 1000 > Date.now()) return cached;
      if (unreachable) return unreachable;
      if (scope && !clearing && generation === started) await forget(scope);
      return new Response("Open Cockpit once while signed in and connected to save it for offline use.", { status: 503, headers: { "Content-Type": "text/plain; charset=utf-8" } });
    })());
  } else if (url.pathname.startsWith("/static/")) {
    event.respondWith((async () => {
      const cache = await caches.open(STATIC);
      const cached = await cache.match(request);
      if (cached) return cached;
      return assetSlot(false, async () => {
        // Preparation may have filled this entry while the request waited for a slot.
        const saved = await cache.match(request);
        if (saved) return saved;
        const response = await fetch(request, { signal: AbortSignal.any([request.signal, AbortSignal.timeout(20000)]) });
        if (response.ok) await cache.put(request, response.clone());
        return response;
      });
    })());
  }
});
