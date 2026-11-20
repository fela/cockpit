"use strict";

// A saved read is a server snapshot. Queued edits are projected over it until the server accepts them.
window.CockpitOffline = (() => {
  const user = document.documentElement.dataset.offlineUser || "";
  const until = Number(document.documentElement.dataset.offlineUntil || 0);
  const scope = `${user}-${until}`;
  const PREFIX = "cockpit-offline-v1-";
  const OWNER = "cockpit.offline-owner";
  const signedOut = "cockpit.offline-sign-out";
  const reads = new Map(), writes = new Map(), inflight = new Map();
  const stale = new Set();
  const readGenerations = new Map(), mutations = new Map();
  const touched = (paths) => { for (const path of paths) mutations.set(path, (mutations.get(path) || 0) + 1); };
  const channel = "BroadcastChannel" in window ? new BroadcastChannel("cockpit-offline") : null;
  let database = null, adapter = null, stopped = !/^[a-f0-9]{64}$/.test(user) || until * 1000 <= Date.now(), replaying = false, failure = null, shellReady = false;
  let serial = Promise.resolve();
  let saving = 0, replayAgain = false, shellPreparation = null;
  let replayAbort = null;
  let cleared = false;
  let materialized = null, changeTimer = null;
  const changedPaths = new Set();

  function key(path) {
    const url = new URL(path, location.origin);
    if (url.origin !== location.origin) return null;
    if (url.pathname === "/api/state") return "/api/state";
    if (!adapter?.saves(url.pathname)) return null;
    url.searchParams.sort();
    return url.pathname + url.search;
  }

  function status() {
    const queue = [...writes.values()];
    const waiting = queue.filter((entry) => !entry.error);
    return { online: navigator.onLine, pending: waiting.length,
      waiting: waiting.map(({ id, originalPath, originalBody, at }) => ({ id, path: originalPath, body: structuredClone(originalBody), at })),
      refused: queue.filter((entry) => entry.error).length, failures: queue.filter((entry) => entry.error).map(({ id, originalPath, error }) => ({ id, label: adapter ? adapter.label(originalPath) : owner(), error })),
      error: failure, stale: stale.size > 0, saved: reads.size > 0, shellReady, stopped, saving, replaying };
  }
  function emit() { dispatchEvent(new CustomEvent("cockpit:offline-status", { detail: status() })); }
  function announceCleared() {
    if (cleared) return;
    cleared = true;
    const announce = () => dispatchEvent(new CustomEvent("cockpit:offline-cleared", { detail: { user, until, scope } }));
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", announce, { once: true });
    else announce();
  }
  // The registered part's name, for the messages about its data and its replies.
  const owner = () => adapter?.name || "Cockpit";
  function problem(error) {
    failure = error?.message || String(error);
    window.logError?.(`saving ${owner()} data on this device`, error instanceof Error ? error : new Error(failure));
    emit();
    return error;
  }
  function requestResult(request) {
    return new Promise((resolve, reject) => { request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error); });
  }
  function removeDatabase(name) {
    return new Promise((resolve, reject) => {
      const request = indexedDB.deleteDatabase(name);
      request.onsuccess = () => resolve(); request.onerror = () => reject(request.error);
      request.onblocked = () => {
        window.logError?.("clearing old offline storage", new Error("Another Cockpit tab must close its old storage before deletion can finish"));
        resolve(); // The browser completes this deletion when the old connection closes; the new scope can start now.
      };
    });
  }
  function transaction(stores, run) {
    return new Promise((resolve, reject) => {
      const tx = database.transaction(stores, "readwrite");
      tx.oncomplete = () => resolve(); tx.onerror = tx.onabort = () => reject(tx.error || new Error("Offline storage did not save"));
      try { run(tx); } catch (error) { tx.abort(); reject(error); }
    });
  }
  function ordered() { return [...writes.values()].sort((a, b) => a.order - b.order); }
  function projected() {
    if (materialized) return materialized;
    // The adapter clones only the records it changes; peek and events clone at the caller boundary.
    const view = new Map([...reads].map(([path, entry]) => [path, entry.data]));
    if (adapter) for (const entry of ordered()) {
      try {
        const result = adapter.prepare(entry.originalPath, structuredClone(entry.originalBody), view, entry.at);
        if (result) for (const [path, data] of result.reads) view.set(path, data);
      } catch (error) { if (!entry.error) problem(error); }
    }
    materialized = view;
    return view;
  }
  function changed(paths) {
    materialized = null;
    for (const path of paths) changedPaths.add(path);
    if (changeTimer !== null) return;
    changeTimer = setTimeout(() => {
      changeTimer = null;
      const paths = [...changedPaths]; changedPaths.clear();
      if (stopped) return;
      const view = projected();
      for (const path of paths) if (view.has(path)) dispatchEvent(new CustomEvent("cockpit:offline-update", {
        detail: { path, data: structuredClone(view.get(path)), savedAt: reads.get(path)?.savedAt || Date.now(), cached: writes.size > 0 || stale.has(path) },
      }));
      document.dispatchEvent(new CustomEvent("cockpit:offline-changed", { detail: { paths } }));
      emit();
    }, 0);
  }
  function sanitize(data) {
    const clean = structuredClone(data);
    // Sign-in codes, approval payloads and remote tool arguments never become an offline snapshot.
    for (const name of ["claude_sign_ins", "codex_sign_ins", "permission_requests", "checker_refusals", "codex_requests", "moved_past", "part_warnings"]) clean[name] = [];
    clean.hub = null;
    for (const name of ["timer", "away", "input"]) clean[name] = null;
    const strip = (value) => {
      if (!value || typeof value !== "object") return;
      if (Array.isArray(value)) { for (const each of value) strip(each); return; }
      for (const name of ["signin", "permission", "command", "tool_input", "tool_use", "access_token", "refresh_token", "device_code", "user_code", "authorization", "token", "secret", "cookie"]) {
        if (name in value) value[name] = null;
      }
      for (const each of Object.values(value)) strip(each);
    };
    strip(clean);
    return clean;
  }
  async function workerMessage(message) {
    const registration = await navigator.serviceWorker.ready;
    const worker = registration.active;
    if (!worker) throw new Error("Offline shell is not ready");
    return new Promise((resolve, reject) => {
      const pipe = new MessageChannel();
      let timeout;
      const finish = (error, data) => {
        clearTimeout(timeout); pipe.port1.close();
        error ? reject(error) : resolve(data);
      };
      const waiting = () => {
        clearTimeout(timeout);
        timeout = setTimeout(() => finish(new Error("Offline shell did not finish saving")), 30000);
      };
      pipe.port1.onmessage = ({ data }) => {
        if (data.progress) { waiting(); return; }
        finish(data.error ? new Error(data.error) : null, data);
      };
      waiting();
      try { worker.postMessage(message, [pipe.port2]); }
      catch (error) { finish(error); }
    });
  }
  async function prepareShell() {
    if (stopped || !("serviceWorker" in navigator)) return;
    if (shellPreparation) return shellPreparation;
    shellPreparation = (async () => {
      try {
        await navigator.serviceWorker.register("/offline-worker.js", { scope: "/", updateViaCache: "none" });
        await navigator.serviceWorker.ready;
        if (stopped || !navigator.onLine) return;
        await workerMessage({ cockpitOffline: "prepare", user, until, supportsProgress: true });
        if (stopped) return;
        shellReady = true; emit();
      } catch (error) { if (!stopped && navigator.onLine) problem(error); }
    })().finally(() => { shellPreparation = null; });
    return shellPreparation;
  }

  const ready = (async () => {
    if (stopped) return;
    try {
      const old = localStorage.getItem(OWNER);
      if (old && old !== scope) {
        if (!navigator.onLine) { stopped = true; announceCleared(); emit(); return; }
        const attempt = new AbortController(), timeout = setTimeout(() => attempt.abort(), 5000);
        try {
          const response = await fetch("/api/offline/session", { credentials: "same-origin", cache: "no-store", signal: attempt.signal });
          const current = response.ok ? await response.json() : null;
          if (!current || current.user !== user || current.until !== until || current.until * 1000 <= Date.now()) {
            stopped = true; announceCleared(); emit(); return;
          }
        } finally { clearTimeout(timeout); }
        if (localStorage.getItem(OWNER) !== old) { stopped = true; announceCleared(); emit(); return; }
      }
      if (old && old !== scope) channel?.postMessage({ user, until, scope, kind: "switch" });
      if (old && old !== scope) removeDatabase(PREFIX + old).catch(problem);
      localStorage.setItem(OWNER, scope);
      const opening = indexedDB.open(PREFIX + scope, 1);
      opening.onupgradeneeded = () => { opening.result.createObjectStore("reads", { keyPath: "path" }); opening.result.createObjectStore("writes", { keyPath: "id" }); };
      database = await requestResult(opening);
      database.onversionchange = () => {
        database.close(); database = null; stopped = true; reads.clear(); writes.clear(); materialized = null;
        announceCleared(); emit();
      };
      for (const entry of await requestResult(database.transaction("reads").objectStore("reads").getAll())) reads.set(entry.path, entry);
      for (const entry of await requestResult(database.transaction("writes").objectStore("writes").getAll())) writes.set(entry.id, entry);
      if (localStorage.getItem(signedOut)) {
        await forget(false);
        if (navigator.onLine) {
          try {
            const credential = localStorage.getItem("cockpit.local-secret");
            const response = await fetch("/auth/sign-out", { method: "POST", credentials: "same-origin" });
            if (response.ok) {
              localStorage.removeItem(signedOut);
              if (localStorage.getItem("cockpit.local-secret") === credential) localStorage.removeItem("cockpit.local-secret");
            }
          }
          catch { /* Keep the local sign-out marker until the server can clear the cookie. */ }
        }
        location.replace("/");
        return;
      }
      emit();
    } catch (error) { stopped = true; problem(error); }
  })();

  async function peek(path) {
    await ready;
    const normalized = key(path);
    if (until * 1000 <= Date.now()) { await forget(); return null; }
    if (stopped || !normalized || !reads.has(normalized)) return null;
    const entry = reads.get(normalized);
    return { data: structuredClone(projected().get(normalized)), savedAt: entry.savedAt };
  }
  async function save(path, data) {
    await ready;
    const normalized = key(path);
    if (stopped || !normalized) return;
    if (normalized === "/api/state" && ("changes" in data || "removed" in data)) throw new Error("Offline state needs a complete snapshot");
    const entry = { path: normalized, data: normalized === "/api/state" ? sanitize(data) : structuredClone(data), savedAt: Date.now() };
    try {
      await transaction(["reads"], (tx) => tx.objectStore("reads").put(entry));
      if (stopped) return;
      reads.set(normalized, entry); stale.delete(normalized); failure = null;
      changed([normalized]);
      channel?.postMessage({ user, until, scope, kind: "changed" });
    } catch (error) { throw problem(error); }
  }
  function revalidate(path, networkFn, force) {
    const normalized = key(path);
    if (!force && inflight.has(normalized)) return inflight.get(normalized);
    const generation = (readGenerations.get(normalized) || 0) + 1;
    readGenerations.set(normalized, generation);
    const mutation = mutations.get(normalized) || 0;
    const request = Promise.resolve().then(networkFn).then(async (data) => {
      if (stopped) throw new Error("Sign in again before reopening saved data");
      if (readGenerations.get(normalized) === generation && (mutations.get(normalized) || 0) === mutation) {
        await save(path, data);
      } else if (force) {
        const newer = inflight.get(normalized);
        return newer && newer !== request ? newer : revalidate(path, networkFn, true);
      } else if (readGenerations.get(normalized) === generation && navigator.onLine) {
        setTimeout(() => readFresh(path, networkFn).catch(() => { stale.add(normalized); emit(); }), 0);
      }
      return (await peek(path))?.data ?? data;
    }).finally(() => { if (inflight.get(normalized) === request) inflight.delete(normalized); });
    inflight.set(normalized, request);
    return request;
  }
  async function readFresh(path, networkFn) {
    await ready;
    if (!key(path) || stopped) return networkFn();
    return revalidate(path, networkFn, true);
  }
  async function read(path, networkFn) {
    const normalized = key(path);
    const missing = () => Object.assign(new Error("Connect to Cockpit to open this view; it has not been saved on this device"), { offline: true });
    if (!normalized || stopped) { if (!navigator.onLine) throw missing(); return networkFn(); }
    const cached = await peek(path);
    if (cached) {
      stale.add(normalized); emit();
      if (navigator.onLine) revalidate(path, networkFn, false).catch((error) => { if (error.status === 401 || error.status === 403) forget(); else { stale.add(normalized); emit(); } });
      return cached.data;
    }
    if (!navigator.onLine) throw missing();
    return revalidate(path, networkFn, false);
  }
  async function write(path, body) {
    const at = Date.now();
    await ready;
    if (stopped || !adapter) return null;
    saving++; emit();
    let receipt;
    const queued = serial.then(async () => {
      const id = crypto.randomUUID();
      if (stopped || at >= until * 1000) throw new Error("Sign in again before saving an offline edit");
      const result = adapter.prepare(path, structuredClone(body), projected(), at);
      if (!result) return null;
      if (result.path !== adapter.send) throw new Error("This edit requires a connection");
      const entry = { id, at, originalPath: path, originalBody: structuredClone(body), path: result.path, body: result.body, error: null };
      await transaction(["writes"], (tx) => {
        const store = tx.objectStore("writes"), request = store.getAll();
        request.onsuccess = () => { entry.order = Math.max(0, ...request.result.map((each) => each.order || 0)) + 1; store.add(entry); };
      });
      if (stopped) throw new Error("The sign-in changed before this edit finished saving");
      writes.set(id, entry); touched(result.reads.keys()); failure = null; changed([...result.reads.keys()]);
      channel?.postMessage({ user, until, scope, kind: "changed" });
      receipt = { queued: true, action: id, at };
      return receipt;
    });
    serial = queued.catch(() => {});
    try { const result = await queued; if (result) replay(); return result; }
    catch (error) { throw problem(error); }
    finally { saving--; emit(); }
  }
  async function replay() {
    await ready;
    if (replaying) { replayAgain = true; return; }
    if (stopped || !navigator.onLine) return;
    const run = async () => {
      replaying = true;
      const abort = new AbortController();
      replayAbort = abort;
      try {
        await serial;
        let principal;
        try { principal = await fetch("/api/offline/session", { cache: "no-store", credentials: "same-origin", signal: AbortSignal.any([abort.signal, AbortSignal.timeout(30000)]) }); }
        catch { return; }
        if (principal.status === 401 || principal.status === 403) { await forget(); return; }
        if (!principal.ok) return;
        const session = await principal.json();
        if (session.user !== user || session.until !== until || session.until * 1000 <= Date.now() || until * 1000 <= Date.now()) { await forget(); return; }
        await reloadStorage();
        for (const entry of ordered()) {
          if (stopped || !navigator.onLine) break;
          if (entry.error) break;
          let response;
          try {
            response = await fetch(entry.path, { method: "POST", headers: { "Content-Type": "application/json", "x-cockpit-action": entry.id },
              body: JSON.stringify(entry.body), credentials: "same-origin", signal: AbortSignal.any([abort.signal, AbortSignal.timeout(30000)]) });
          } catch { break; }
          if (stopped) break;
          if (response.status === 401 || response.status === 403) { await forget(); break; }
          if (response.status >= 500 || [408, 424, 429].includes(response.status)) break;
          let result;
          try { result = await response.json(); }
          catch (error) {
            if (error.name === "AbortError" || error.name === "TimeoutError") break;
            if (response.ok) { problem(new Error(`${owner()}'s reply was incomplete; the saved edit will be checked again`, { cause: error })); break; }
            result = {};
          }
          if (response.ok && result?.ok !== true) { problem(new Error(`${owner()} did not confirm the saved edit; it will be checked again`)); break; }
          if (!response.ok) {
            entry.error = result.detail || `${owner()} refused this edit (${response.status})`;
            await transaction(["writes"], (tx) => tx.objectStore("writes").put(entry));
            writes.set(entry.id, entry); emit();
            dispatchEvent(new CustomEvent("cockpit:offline-refused", { detail: { id: entry.id, path: entry.originalPath, body: structuredClone(entry.originalBody), error: entry.error } }));
            channel?.postMessage({ user, until, scope, kind: "changed" });
            break;
          }
          // Keep the accepted local projection until an authenticated read replaces it.
          const view = new Map([...reads].map(([path, saved]) => [path, saved.data]));
          const affected = adapter?.prepare(entry.originalPath, entry.originalBody, view, entry.at)?.reads || new Map();
          touched(affected.keys());
          await transaction(["reads", "writes"], (tx) => {
            for (const [path, data] of affected) tx.objectStore("reads").put({ path, data, savedAt: Date.now() });
            tx.objectStore("writes").delete(entry.id);
          });
          if (stopped) break;
          for (const [path, data] of affected) reads.set(path, { path, data, savedAt: Date.now() });
          writes.delete(entry.id); changed([...affected.keys()]);
          dispatchEvent(new CustomEvent("cockpit:offline-sent", { detail: { id: entry.id, path: entry.originalPath, body: structuredClone(entry.originalBody), at: entry.at, result } }));
          channel?.postMessage({ user, until, scope, kind: "changed" });
        }
      } catch (error) { if (!stopped && error.name !== "AbortError" && error.name !== "TimeoutError") problem(error); }
      finally {
        if (replayAbort === abort) replayAbort = null;
        replaying = false; emit();
        if (replayAgain) { replayAgain = false; setTimeout(replay, 0); }
      }
    };
    if (navigator.locks) await navigator.locks.request(`cockpit-offline-${user}`, { ifAvailable: true }, (lock) => lock && run());
    else await run();
  }
  async function reloadStorage() {
    if (!database || stopped) return;
    await serial;
    if (!database || stopped) return;
    const tx = database.transaction(["reads", "writes"], "readonly");
    const [savedReads, savedWrites] = await Promise.all([
      requestResult(tx.objectStore("reads").getAll()), requestResult(tx.objectStore("writes").getAll()),
    ]);
    if (stopped) return;
    reads.clear(); writes.clear();
    for (const entry of savedReads) reads.set(entry.path, entry);
    for (const entry of savedWrites) writes.set(entry.id, entry);
    materialized = null;
  }
  async function forget(broadcast = true) {
    if (stopped && !database && !reads.size && !writes.size) { announceCleared(); return; }
    stopped = true; reads.clear(); writes.clear(); inflight.clear(); stale.clear(); shellReady = false;
    replayAbort?.abort();
    materialized = null; changedPaths.clear();
    announceCleared();
    if (database) { database.close(); database = null; }
    try {
      if (user) await removeDatabase(PREFIX + scope);
      if ("serviceWorker" in navigator && navigator.serviceWorker.controller) await workerMessage({ cockpitOffline: "forget", scope });
      if (localStorage.getItem(OWNER) === scope) localStorage.removeItem(OWNER);
    } catch (error) { problem(error); }
    if (broadcast) channel?.postMessage({ user, until, scope, kind: "forget" });
    emit();
  }
  async function retry(id) {
    await ready;
    const entry = writes.get(id);
    if (!entry) return;
    entry.error = null;
    await transaction(["writes"], (tx) => tx.objectStore("writes").put(entry));
    emit(); replay();
  }
  async function discard(id) {
    await ready;
    await transaction(["writes"], (tx) => tx.objectStore("writes").delete(id));
    writes.delete(id); changed([...reads.keys()]); channel?.postMessage({ user, until, scope, kind: "changed" }); replay();
  }
  channel && (channel.onmessage = async ({ data }) => {
    if (data?.kind === "switch" && data.scope !== scope) { await forget(false); return; }
    if (data?.scope !== scope) return;
    if (data?.kind === "forget") { await forget(false); return; }
    if (data?.kind === "changed") { await ready; await reloadStorage(); changed([...reads.keys()]); }
  });
  navigator.serviceWorker?.addEventListener("message", ({ data }) => { if (data?.cockpitOffline === "forgotten") forget(); });
  const originalFetch = window.fetch.bind(window);
  window.fetch = async (...args) => {
    const response = await originalFetch(...args);
    const url = new URL(args[0] instanceof Request ? args[0].url : args[0], location.href);
    if (url.origin === location.origin && url.pathname.startsWith("/api/")) {
      const found = response.headers.get("X-Cockpit-Offline-User");
      const lease = response.headers.get("X-Cockpit-Offline-Until");
      if ((found && found !== user) || (lease && Number(lease) !== until)) { await forget(); throw new Error("The sign-in changed. Reload Cockpit."); }
      if (response.status === 401 || response.status === 403) await forget();
    }
    return response;
  };
  addEventListener("submit", (event) => {
    if (event.target.getAttribute?.("action") !== "/auth/sign-out") return;
    event.preventDefault(); event.stopImmediatePropagation();
    localStorage.setItem(signedOut, "1");
    forget().then(async () => {
      try {
        const credential = localStorage.getItem("cockpit.local-secret");
        const response = await originalFetch("/auth/sign-out", { method: "POST", credentials: "same-origin" });
        if (response.ok) {
          localStorage.removeItem(signedOut);
          if (localStorage.getItem("cockpit.local-secret") === credential) localStorage.removeItem("cockpit.local-secret");
        }
      }
      catch { /* The local copy is gone; finish the cookie sign-out once connected. */ }
      location.assign("/");
    });
  }, true);
  addEventListener("online", () => { replayAbort?.abort(); replay(); prepareShell(); emit(); });
  addEventListener("offline", emit);
  addEventListener("beforeunload", (event) => {
    if (saving > 0) { event.preventDefault(); event.returnValue = ""; }
  });
  addEventListener("DOMContentLoaded", () => { ready.then(() => { prepareShell(); replay(); }); });
  setInterval(() => { if (until * 1000 <= Date.now()) forget(); else replay(); }, 15000);
  ready.catch(problem);
  // A part of this install keeps its reads and edits on the device by registering itself before the page's first
  // read: `name` names it in messages, `label(path)` names the kind of record an edit to `path` changes, the GETs under
  // `root` are its reads, `saves(pathname)` says which of them this device keeps, `send` is the route that takes its
  // queued edits, and `prepare(path, body, reads, at)` turns an edit into `{ path: send, body, reads }`, with the reads
  // the edit changes, or into null for an edit that goes online.
  function register(part) { adapter = part; ready.then(() => { changed([...reads.keys()]); replay(); }); }
  // Whether a GET is the registered part's read, which goes through read().
  const owns = (path) => Boolean(adapter && path.startsWith(adapter.root));
  return { user, until, scope, ready, read, readFresh, save, peek, cached: peek, write, replay, forget, retry, discard, status,
    register, owns };
})();
