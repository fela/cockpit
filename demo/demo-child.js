// The demo's side of each Cockpit page: it runs first in every page the demo shows, and gives the page's own
// scripts what a server and a normal tab would. Requests, stored settings and moves to another page go to the
// demo's outer page (demo-shell.js) as messages; the page's address and history live here, one for each page.
//
// The pages run in frames without an origin of their own (their sandbox has no allow-same-origin), so they cannot
// reach the outer page's storage or address, and anything they would load from a server would come from the host
// that serves the demo. Nothing here asks that host for anything.
(() => {
  "use strict";
  const boot = window.__demoBoot;
  const ORIGIN = boot.origin;
  // Where relative addresses in this document resolve: the demo file's own address. A link or request there is one
  // to Cockpit, never to the host.
  const BASE_ORIGIN = (() => {
    try {
      const origin = new URL(document.baseURI).origin;
      return origin === "null" ? "" : origin;
    } catch {
      return "";
    }
  })();
  const realOpen = window.open.bind(window);
  const realFetch = window.fetch.bind(window);

  // The outer page, `depth` frames up: a page the shell shows is one down, a page framed in that page two.
  let shell = window;
  for (let step = 0; step < boot.depth; step += 1) shell = shell.parent;

  let counter = 0;
  const waiting = new Map();
  function rpc(op, args = {}) {
    return new Promise((resolve, reject) => {
      counter += 1;
      const id = `${boot.depth}-${Date.now().toString(36)}-${counter}`;
      waiting.set(id, { resolve, reject });
      shell.postMessage({ demo: "rpc", id, op, ...args }, "*");
    });
  }
  function tell(op, args = {}) {
    shell.postMessage({ demo: "rpc", id: null, op, ...args }, "*");
  }

  const storage = {
    local: storageArea("local", boot.storage?.local),
    session: storageArea("session", boot.storage?.session),
  };

  // Before any of the page's own listeners: the demo's messages are not the page's to see.
  window.addEventListener("message", (event) => {
    const data = event.data;
    if (!data || typeof data !== "object" || typeof data.demo !== "string" || event.source !== shell) return;
    event.stopImmediatePropagation();
    if (data.demo === "reply") {
      const call = waiting.get(data.id);
      if (!call) return;
      waiting.delete(data.id);
      if (data.ok) call.resolve(data.value);
      else call.reject(new Error(String(data.value)));
    } else if (data.demo === "poke") {
      try {
        window.refresh?.();
      } catch (error) {
        console.warn("demo: refresh failed", error);
      }
    } else if (data.demo === "storage") {
      storage[data.area]?.heard(data.key, data.value, data.old);
    }
  }, true);

  // A page in one of this page's frames asking to show another address in that frame (__demo.replaceFrame).
  window.addEventListener("message", (event) => {
    const data = event.data;
    if (!data || typeof data !== "object" || data.demo !== "reframe" || typeof data.src !== "string") return;
    const holder = [...document.querySelectorAll("iframe")].find((element) => element.contentWindow === event.source);
    if (!holder) return;
    event.stopImmediatePropagation();
    frame(holder, data.src);
  }, true);

  // A tab in front already has focus at its first press; the shell's frame gets it only from that press, after the
  // page's own listeners heard it, and Cockpit counts input only on a page with focus (presence.js). Unheard, the
  // press that opens a step starts no timer, and the press that comes back from a figure's choice starts it in the
  // middle of that click: its redraw takes the button away before the click lands. The press focuses the frame
  // first, so the page hears it as a tab would.
  if (boot.depth === 1) {
    for (const type of ["pointerdown", "keydown", "touchstart"]) {
      window.addEventListener(type, () => {
        if (!document.hasFocus()) window.focus();
      }, { capture: true, passive: true });
    }
  }

  // Stored settings and drafts: kept by the shell for the visit, shared by every page of the demo, gone on Start
  // over or a reload of the demo.
  function storageArea(area, initial) {
    const map = new Map(Object.entries(initial || {}));
    const api = {
      get length() { return map.size; },
      key(index) { return [...map.keys()][index] ?? null; },
      getItem(key) {
        key = String(key);
        return map.has(key) ? map.get(key) : null;
      },
      setItem(key, value) {
        key = String(key);
        value = String(value);
        const old = map.has(key) ? map.get(key) : null;
        map.set(key, value);
        tell("storage", { area, key, value, old });
      },
      removeItem(key) {
        key = String(key);
        if (!map.has(key)) return;
        const old = map.get(key);
        map.delete(key);
        tell("storage", { area, key, value: null, old });
      },
      clear() {
        map.clear();
        tell("storage", { area, key: null, value: null, old: null });
      },
    };
    return {
      api,
      snapshot: () => Object.fromEntries(map),
      heard(key, value, old) {
        if (key === null) map.clear();
        else if (value === null) map.delete(key);
        else map.set(key, value);
        window.dispatchEvent(new StorageEvent("storage", { key, oldValue: old, newValue: value, url: here.href() }));
      },
    };
  }
  Object.defineProperty(window, "localStorage", { get: () => storage.local.api, configurable: true });
  Object.defineProperty(window, "sessionStorage", { get: () => storage.session.api, configurable: true });
  // Locks need an origin; without them the pages coordinate as a browser without locks would.
  Object.defineProperty(navigator, "locks", { value: undefined, configurable: true });
  if (!crypto.randomUUID) {
    crypto.randomUUID = () => {
      const bytes = crypto.getRandomValues(new Uint8Array(16));
      bytes[6] = (bytes[6] & 0x0f) | 0x40;
      bytes[8] = (bytes[8] & 0x3f) | 0x80;
      const hex = [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
      return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
    };
  }
  // A sandboxed frame has no dialogs: a question the page asks before acting reads as yes.
  window.confirm = () => true;

  function toast(text) {
    tell("toast", { text });
  }

  // ---- The page's address and history ----

  const here = {
    pathname: boot.path || "/",
    search: boot.search || "",
    hash: boot.hash || "",
    href() { return ORIGIN + this.pathname + this.search + this.hash; },
  };

  function parse(raw) {
    try {
      return new URL(String(raw), here.href());
    } catch {
      return null;
    }
  }
  function isApp(url) {
    return url && (url.origin === ORIGIN || (BASE_ORIGIN && url.origin === BASE_ORIGIN));
  }
  function told() {
    if (boot.depth === 1) tell("where", { path: here.pathname, search: here.search, hash: here.hash });
  }

  function hashTo(hash, push = true) {
    if (hash && !hash.startsWith("#")) hash = `#${hash}`;
    if (hash === "#") hash = "";
    if (hash === here.hash) return;
    const oldURL = here.href();
    if (push) history.pushState(null, "", hash || here.pathname + here.search);
    else {
      here.hash = hash;
      entries[at] = { ...entries[at], hash };
      told();
    }
    const newURL = here.href();
    setTimeout(() => {
      window.dispatchEvent(new PopStateEvent("popstate", { state: null }));
      window.dispatchEvent(new HashChangeEvent("hashchange", { oldURL, newURL }));
      const target = hash && document.getElementById(decodeURIComponent(hash.slice(1)));
      target?.scrollIntoView();
    }, 0);
  }

  // A move to another address: within the page when only the hash differs, otherwise the shell shows the page.
  function go(raw, replacing = false, newTab = false) {
    const text = String(raw);
    if (text.startsWith("#")) {
      hashTo(text);
      return;
    }
    const url = parse(text);
    if (!url) return;
    if (url.protocol === "blob:" || url.protocol === "data:") {
      realOpen(url.href, "_blank", "noopener");
      return;
    }
    if (!isApp(url)) {
      outside(url);
      return;
    }
    if (url.pathname.startsWith("/auth/")) {
      toast("Demo: nobody signs in here; the work belongs to an invented person");
      return;
    }
    if (!newTab && url.pathname === here.pathname && url.search === here.search && url.hash) {
      hashTo(url.hash);
      return;
    }
    tell("nav", { path: url.pathname, search: url.search, hash: url.hash, replace: replacing, tab: newTab, depth: boot.depth });
  }

  function outside(url) {
    if (/^(mailto|tel|sms):$/.test(url.protocol) || /(^|\.)example\.(com|org|net|test)$/.test(url.hostname)) {
      toast("Demo link: it points at an invented address");
      return;
    }
    if (url.protocol !== "http:" && url.protocol !== "https:") return;
    // Opened without "noopener" so a refusal shows: a host that forbids new windows (a public page) returns null,
    // and the user gets a note instead of a dead click. The new window loses its way back here at once.
    let opened = null;
    try {
      opened = realOpen(url.href, "_blank");
    } catch {
      opened = null;
    }
    if (opened) {
      try {
        opened.opener = null;
      } catch {
        // A window that already went to another site.
      }
    } else {
      toast(`Demo: this copy of the demo cannot open other sites. The link goes to ${url.host}`);
    }
  }

  const loc = {
    get origin() { return ORIGIN; },
    get protocol() { return "https:"; },
    get host() { return new URL(ORIGIN).host; },
    get hostname() { return new URL(ORIGIN).hostname; },
    get port() { return ""; },
    get pathname() { return here.pathname; },
    get search() { return here.search; },
    get hash() { return here.hash; },
    set hash(value) { hashTo(String(value)); },
    get href() { return here.href(); },
    set href(value) { go(value); },
    assign(value) { go(value); },
    replace(value) { go(value, true); },
    reload() { tell("nav", { path: here.pathname, search: here.search, hash: here.hash, reload: true, depth: boot.depth }); },
    toString() { return here.href(); },
  };
  window.__demoLoc = loc;

  // This page's own entries; the demo's Back past the first goes to the page shown before it.
  const entries = [{ state: null, pathname: here.pathname, search: here.search, hash: here.hash }];
  let at = 0;
  function settle(entry) {
    here.pathname = entry.pathname;
    here.search = entry.search;
    here.hash = entry.hash;
    told();
  }
  function entryFor(state, url) {
    let place = { pathname: here.pathname, search: here.search, hash: here.hash };
    if (url !== undefined && url !== null && String(url) !== "") {
      const target = parse(url);
      if (target && isApp(target)) place = { pathname: target.pathname, search: target.search, hash: target.hash };
    }
    return { state: state === undefined ? null : structuredClone(state), ...place };
  }
  const history = {
    get state() { return entries[at].state === null ? null : structuredClone(entries[at].state); },
    get length() { return entries.length; },
    pushState(state, title, url) {
      entries.splice(at + 1);
      entries.push(entryFor(state, url));
      at += 1;
      settle(entries[at]);
    },
    replaceState(state, title, url) {
      entries[at] = entryFor(state, url);
      settle(entries[at]);
    },
    back() { this.go(-1); },
    forward() { this.go(1); },
    go(delta = 0) {
      if (!delta) {
        loc.reload();
        return;
      }
      const to = at + delta;
      if (to < 0) {
        if (boot.depth === 1) tell("back");
        return;
      }
      if (to >= entries.length) return;
      setTimeout(() => {
        const oldURL = here.href();
        const before = entries[at];
        at = to;
        const after = entries[at];
        settle(after);
        window.dispatchEvent(new PopStateEvent("popstate", { state: this.state }));
        if (before.pathname === after.pathname && before.search === after.search && before.hash !== after.hash) {
          window.dispatchEvent(new HashChangeEvent("hashchange", { oldURL, newURL: here.href() }));
        }
      }, 0);
    },
  };
  window.__demoHist = history;

  window.open = (url, target) => {
    if (url) go(url, false, true);
    return null;
  };

  // ---- Requests ----

  function abortError(signal) {
    return signal?.reason instanceof Error ? signal.reason : new DOMException("The operation was aborted.", "AbortError");
  }

  async function bodyOf(body) {
    if (body === undefined || body === null) return null;
    if (typeof body === "string") return body;
    if (body instanceof URLSearchParams) return body.toString();
    if (body instanceof FormData) {
      return {
        form: [...body.entries()].map(([name, value]) => [name, typeof value === "string" ? value : { file: value.name, type: value.type, size: value.size }]),
      };
    }
    if (body instanceof Blob) return { blob: true, type: body.type, size: body.size };
    if (body instanceof ArrayBuffer || ArrayBuffer.isView(body)) return { blob: true, type: "", size: body.byteLength };
    return String(body);
  }

  window.fetch = async function demoFetch(input, init = {}) {
    let raw;
    let method = init.method;
    let body = init.body;
    let signal = init.signal;
    if (input instanceof Request) {
      raw = input.url;
      method = method || input.method;
      signal = signal || input.signal;
      if (body === undefined && !["GET", "HEAD"].includes(input.method)) body = await input.clone().text();
    } else raw = String(input);
    const url = parse(raw);
    if (!isApp(url)) return realFetch(input, init);
    if (signal?.aborted) throw abortError(signal);
    const asked = rpc("fetch", {
      method: String(method || "GET").toUpperCase(),
      path: url.pathname + url.search,
      body: await bodyOf(body),
    });
    const reply = await (signal
      ? new Promise((resolve, reject) => {
        const stop = () => reject(abortError(signal));
        signal.addEventListener("abort", stop, { once: true });
        asked.then(resolve, reject).finally(() => signal.removeEventListener("abort", stop));
      })
      : asked);
    const empty = reply.status === 204 || reply.status === 304;
    return new Response(empty ? null : reply.text, {
      status: reply.status,
      headers: { "content-type": reply.ct || "application/json" },
    });
  };

  if (navigator.clipboard?.writeText) {
    const realWrite = navigator.clipboard.writeText.bind(navigator.clipboard);
    navigator.clipboard.writeText = async (text) => {
      try {
        await realWrite(text);
      } catch {
        if (!copyByHand(String(text))) toast("Demo: this browser kept the clipboard from the demo");
      }
    };
  }
  function copyByHand(text) {
    const had = document.activeElement;
    const box = document.createElement("textarea");
    box.value = text;
    box.setAttribute("readonly", "");
    box.style.cssText = "position:fixed;top:0;left:0;opacity:0;pointer-events:none";
    document.body.append(box);
    box.select();
    let copied = false;
    try {
      copied = document.execCommand("copy");
    } catch {
      copied = false;
    }
    box.remove();
    had?.focus?.({ preventScroll: true });
    return copied;
  }

  // ---- Frames ----

  const FIGURE = /^\/pages\/\d+\/figure$/;
  // The views a frame can show: a shared page or a briefing in the main page's layer, and the helper pages the center
  // layer opens. The skills and writing pages are the spec view, which reads its path, as on the host.
  const VIEWS = ["pages", "briefing", "usage", "spec"];
  function viewFor(pathname) {
    if (VIEWS.includes(pathname.slice(1))) return pathname.slice(1);
    return /^\/(skills|writing)(\/|$)/.test(pathname) ? "spec" : null;
  }
  const templates = new Map();
  const forTemplate = [];

  function bootFor(path, search) {
    const next = {
      origin: ORIGIN, path, search, hash: "", depth: boot.depth + 1,
      storage: { local: storage.local.snapshot(), session: storage.session.snapshot() },
      logos: boot.logos, figures: boot.figures,
    };
    return `window.__demoBoot = ${JSON.stringify(next).replace(/</g, "\\u003c")};`;
  }
  function framedDocument(view, path, search) {
    return templates.get(view).replace("@@DEMO:boot@@", () => bootFor(path, search));
  }
  function note(text) {
    return `<!doctype html><meta charset="utf-8"><meta name="color-scheme" content="light dark"><style>body{margin:0;display:grid;place-items:center;min-height:100vh;font:14px/1.4 system-ui,sans-serif;color:#6b655c;background:transparent}</style><p>${text}</p>`;
  }

  // A frame shows its page from the demo's own copies: the browser would ask the host for the address. The frame
  // keeps the address as its src, which the page reads back, and the srcdoc decides what it shows.
  function frame(element, src) {
    const raw = String(src || "");
    const url = parse(raw);
    let html = null;
    if (url && isApp(url) && FIGURE.test(url.pathname)) {
      const comments = url.searchParams.get("comments");
      html = boot.figures?.[`${url.pathname}?comments=${comments === "1" || comments === "true" ? 1 : 0}`] || null;
    } else if (url && isApp(url) && viewFor(url.pathname)) {
      const view = viewFor(url.pathname);
      if (templates.has(view)) html = framedDocument(view, url.pathname, url.search);
      else {
        html = note("Loading…");
        forTemplate.push({ element, raw, view, path: url.pathname, search: url.search });
        wantTemplate(view);
      }
    }
    element.srcdoc = html || note("This part of Cockpit is not in the demo.");
    element.setAttribute("src", raw);
  }

  const templatesAsked = new Set();
  function wantTemplate(view) {
    if (templatesAsked.has(view)) return;
    templatesAsked.add(view);
    rpc("view", { name: view }).then((template) => {
      templates.set(view, template);
      for (const waiting of forTemplate.filter((entry) => entry.view === view)) {
        forTemplate.splice(forTemplate.indexOf(waiting), 1);
        const { element, raw, path, search } = waiting;
        if (element.getAttribute("src") === raw) element.srcdoc = framedDocument(view, path, search);
      }
    }, (error) => console.warn(`demo: the ${view} view did not arrive`, error));
  }
  // The main page opens shared pages and briefings in a layer; their views come ahead, so the layer opens at once.
  if (boot.depth === 1 && (here.pathname === "/" || here.pathname === "/classic")) {
    for (const view of VIEWS) wantTemplate(view);
  }

  // A frame added some other way would load from the host: it shows the demo's copy instead, and says so.
  new MutationObserver((records) => {
    for (const record of records) {
      for (const node of record.addedNodes) {
        if (node.nodeType !== 1) continue;
        const frames = node.localName === "iframe" ? [node] : node.querySelectorAll("iframe");
        for (const found of frames) {
          const src = found.getAttribute("src");
          if (found.hasAttribute("srcdoc") || !src || /^(about|data|blob|javascript):/i.test(src)) continue;
          console.warn("demo: a frame was added without __demo.frame", src);
          frame(found, src);
        }
      }
    }
  }).observe(document.documentElement, { childList: true, subtree: true });

  // ---- Links and forms ----

  function linkOf(event) {
    for (const node of event.composedPath()) {
      if (node instanceof Element && (node.localName === "a" || node.localName === "area") && node.hasAttribute("href")) return node;
    }
    return null;
  }
  function submitterOf(target) {
    const control = target instanceof Element ? target.closest("button, input") : null;
    if (!control || !control.form || control.disabled) return null;
    return control.type === "submit" || control.type === "image" ? control : null;
  }

  // A link's address and its parts, and a frame's, as the page reads them: against the page's own address. The
  // browser would resolve them against the demo file's address, which is no address of Cockpit's.
  function resolved(element, name) {
    const raw = element.getAttribute(name);
    if (raw === null) return "";
    try {
      return new URL(raw, here.href()).href;
    } catch {
      return raw;
    }
  }
  for (const proto of [HTMLAnchorElement.prototype, HTMLAreaElement.prototype]) {
    Object.defineProperty(proto, "href", {
      get() { return resolved(this, "href"); },
      set(value) { this.setAttribute("href", value); },
      configurable: true,
    });
    for (const part of ["origin", "protocol", "host", "hostname", "port", "pathname", "search", "hash"]) {
      Object.defineProperty(proto, part, {
        get() {
          try {
            return new URL(resolved(this, "href"))[part];
          } catch {
            return "";
          }
        },
        set(value) {
          const url = new URL(resolved(this, "href"));
          url[part] = value;
          this.setAttribute("href", url.href);
        },
        configurable: true,
      });
    }
  }
  Object.defineProperty(HTMLIFrameElement.prototype, "src", {
    get() { return resolved(this, "src"); },
    set(value) { frame(this, value); },
    configurable: true,
  });

  // A click's default would take a link to the demo file's host, or do nothing for a submit button: the sandbox
  // sends no forms. The demo takes the default from every such click before the page sees it, and does what the
  // browser would have done once the click is over, unless the page itself prevented it. The page reads its own
  // prevention only, so its handlers decide as they would in a tab.
  const realPrevent = Event.prototype.preventDefault;
  const prevented = Object.getOwnPropertyDescriptor(Event.prototype, "defaultPrevented").get;
  Event.prototype.preventDefault = function preventDefault() {
    this.__demoByPage = true;
    return realPrevent.call(this);
  };
  Object.defineProperty(Event.prototype, "defaultPrevented", {
    get() { return this.__demoOurs ? Boolean(this.__demoByPage) : prevented.call(this); },
    configurable: true,
  });
  Object.defineProperty(Event.prototype, "returnValue", {
    get() { return !this.defaultPrevented; },
    set(value) { if (!value) this.preventDefault(); },
    configurable: true,
  });

  function held(event) {
    if (event.button !== 0 && event.type === "click") return;
    const link = linkOf(event);
    const submitter = link ? null : event.type === "click" && submitterOf(event.target);
    if (!link && !submitter) return;
    if (prevented.call(event)) event.__demoByPage = true;
    event.__demoOurs = true;
    realPrevent.call(event);
    if (submitter && implicit?.button === submitter) implicit = null;
    setTimeout(() => {
      if (event.__demoByPage) return;
      if (submitter) {
        submit(submitter.form, submitter);
        return;
      }
      const href = link.getAttribute("href");
      if (link.hasAttribute("download")) {
        if (/^(blob|data):/i.test(href)) {
          const save = document.createElement("a");
          save.href = href;
          save.download = link.getAttribute("download");
          save.click();
        } else toast("Demo: there is no file to download here");
        return;
      }
      const newTab = event.type === "auxclick" || link.target === "_blank" || event.metaKey || event.ctrlKey || event.shiftKey;
      go(href, false, newTab);
    }, 0);
  }
  window.addEventListener("click", held, true);
  window.addEventListener("auxclick", (event) => {
    if (event.button === 1) held(event);
  }, true);

  function submit(form, submitter) {
    const skipCheck = form.noValidate || submitter?.formNoValidate;
    if (!skipCheck && !form.reportValidity()) return;
    const unhandled = form.dispatchEvent(new SubmitEvent("submit", { bubbles: true, cancelable: true, submitter: submitter || null }));
    if (!unhandled) return;
    // No script took the form, so a browser would send it to its action, as Sign out does.
    const action = submitter?.getAttribute("formaction") || form.getAttribute("action");
    const method = (submitter?.getAttribute("formmethod") || form.getAttribute("method") || "get").toLowerCase();
    const url = action ? parse(action) : null;
    if (url && (method === "get" || !isApp(url) || url.pathname.startsWith("/auth/"))) go(url.href);
    else toast("Demo: this form has nowhere to go");
  }
  HTMLFormElement.prototype.requestSubmit = function requestSubmit(submitter) {
    submit(this, submitter || null);
  };
  HTMLFormElement.prototype.submit = function quietSubmit() {
    toast("Demo: this form has nowhere to go");
  };

  // Enter in a field sends its form: through the form's first submit button when it has one, as a browser does.
  let implicit = null;
  const TYPING = new Set(["text", "search", "email", "url", "tel", "password", "number", "date", "time", "datetime-local", "month", "week"]);
  window.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.defaultPrevented || event.isComposing) return;
    const field = event.target;
    if (!(field instanceof HTMLInputElement) || !TYPING.has(field.type) || !field.form) return;
    const button = [...field.form.elements].find((control) => (control.type === "submit" || control.type === "image") && control.localName !== "fieldset");
    if (!button) {
      event.preventDefault();
      submit(field.form, null);
      return;
    }
    if (button.disabled) return;
    implicit = { button };
    setTimeout(() => {
      if (implicit?.button !== button) return;
      implicit = null;
      button.click();
    }, 0);
  });

  window.__demo = {
    async commit(change, answer) {
      if (answer && answer.__demo_own) return;
      const state = await rpc("lock");
      let next = state;
      try {
        next = change(state);
      } catch (error) {
        console.warn("demo: the page's change did not apply to the demo's state", error);
      }
      await rpc("unlock", { state: next });
    },
    frame,
    // The path of the page a frame shows, read from the address the frame keeps as its src: the frame has no origin
    // of its own, so its window is closed to this page.
    path(element) {
      return parse(element.getAttribute("src") || "")?.pathname;
    },
    // A page moving itself to another address without a history entry, as location.replace would: a page in a
    // frame stays in its frame, which the page holding the frame then shows the new address in.
    replaceFrame(raw) {
      const url = boot.depth > 1 ? parse(raw) : null;
      if (!url || !isApp(url)) {
        go(raw, true);
        return;
      }
      window.parent.postMessage({ demo: "reframe", src: url.pathname + url.search + url.hash }, "*");
    },
    asset(url) {
      const match = /^\/projects\/([^/]+)\/logo\.svg/.exec(String(url));
      if (!match) return "";
      let name = match[1];
      try {
        name = decodeURIComponent(name);
      } catch {
        // Kept as written.
      }
      return boot.logos?.[name] || boot.logos?.[match[1]] || "";
    },
    toast,
    // Cockpit's page keeps a comment its Pages layer left unfinished; frames without an origin cannot share it.
    unfinished: { get: () => [], set: () => {} },
    reach(frame, name) {
      try {
        frame?.contentWindow?.[name]?.();
      } catch {
        // The frame has no origin of its own, so its window is closed to this page.
      }
    },
    ours(origin) {
      return origin === "null" || origin === ORIGIN || (Boolean(BASE_ORIGIN) && origin === BASE_ORIGIN);
    },
  };
})();
