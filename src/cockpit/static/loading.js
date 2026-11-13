"use strict";

// What a page shows while it waits (SPEC UI-48); loading.css draws it. Every page loads this file.
// - Ghosts: grey shapes where content will come. A page's first ghosts are in its HTML; `ghost` makes more.
//   They show only once a wait lasts GHOST_MS, so a fast load shows nothing.
// - The load line: a thin line along the top of the page while the page leaves for another (any navigation
//   to another document) or waits for a read the user asked for (`start`, `stop`).
// - Arrival: content that takes the place of ghosts fades in once: `settle` for ghosts in a part of the page,
//   `waiting` and `arrive` for ghosts a redraw makes and replaces.
window.Loading = (() => {
  // In step with --ghost-delay and the shine's 1.6s in loading.css.
  const GHOST_MS = 200;
  const SHINE_MS = 1600;
  // A move to another page that turns into a download leaves this page with no event to say so: the line gives
  // up after this long.
  const NAVIGATION_MS = 20000;
  let line = null;
  let waits = 0;

  function lineEl() {
    if (!line) {
      line = document.createElement("div");
      line.className = "load-line";
      line.setAttribute("aria-hidden", "true");
      line.append(document.createElement("span"));
      document.body.append(line);
    }
    return line;
  }

  function start() {
    waits += 1;
    if (waits > 1) return;
    const el = lineEl();
    el.classList.remove("on", "done");
    // A style read between the two classes, so the line starts from nothing each time.
    void el.offsetWidth;
    el.classList.add("on");
  }

  function stop() {
    if (waits === 0) return;
    waits -= 1;
    if (waits > 0) return;
    const el = lineEl();
    el.classList.remove("on");
    el.classList.add("done");
  }

  // When each ghost `ghost` made appeared on the page; the ghosts in a page's HTML count from the page's start.
  const made = new WeakMap();

  // A ghost: `bars` lists its shapes top to bottom, "|" between them, each a set of loading.css classes for one
  // bar ("tall w-60", "w-90") or "gap". `label` says what is loading, for a screen reader.
  function ghost(bars, label, cls) {
    const el = document.createElement("div");
    el.className = cls ? `ghost-box ${cls}` : "ghost-box";
    el.dataset.ghost = "";
    el.setAttribute("role", "status");
    el.setAttribute("aria-label", label);
    // The shine keeps the page's beat, so a ghost drawn later shines in step with the others.
    const beat = `${-(performance.now() % SHINE_MS)}ms`;
    for (const bar of bars.split("|")) {
      const shape = document.createElement("span");
      shape.className = bar === "gap" ? "ghost-gap" : `ghost-bar ${bar}`;
      shape.style.animationDelay = beat;
      el.append(shape);
    }
    made.set(el, performance.now());
    return el;
  }

  function fadeIn(el) {
    el.classList.add("arrive");
    el.addEventListener("animationend", () => el.classList.remove("arrive"), { once: true });
  }

  // Before a part draws what it waited for: its ghosts go, and when they were on show (out for GHOST_MS), the part
  // fades in, once. Called with no ghosts left in `scope`, it does nothing.
  function settle(scope) {
    const ghosts = [...scope.querySelectorAll("[data-ghost]")];
    if (!ghosts.length) return;
    const shown = ghosts.some((el) => performance.now() - (made.get(el) || 0) >= GHOST_MS);
    for (const el of ghosts) el.remove();
    if (shown) fadeIn(scope);
  }

  // A part a redraw draws anew each time, named by `key`: `waiting` marks the ghosts it drew while it waited,
  // and `arrive` fades in what it draws next, once, when the ghosts were on show. Both return what they are given
  // (`arrive` takes an element, a list of them, or null).
  const waitingSince = new Map();
  function waiting(key, el) {
    if (!waitingSince.has(key)) waitingSince.set(key, performance.now());
    return el;
  }
  function arrive(key, content) {
    const since = waitingSince.get(key);
    waitingSince.delete(key);
    if (since !== undefined && performance.now() - since >= GHOST_MS) {
      for (const el of [content].flat()) if (el instanceof Element) fadeIn(el);
    }
    return content;
  }

  // A wait that failed: the ghosts in `scope` stop shining, and `text`, when given, says why below them; without it
  // the page's banner says why.
  function failed(scope, text) {
    for (const el of scope.querySelectorAll("[data-ghost]")) {
      el.classList.add("failed");
      if (!text) continue;
      let note = el.querySelector(".ghost-note");
      if (!note) {
        note = document.createElement("p");
        note.className = "note ghost-note";
        el.append(note);
      }
      note.textContent = text;
    }
  }

  // A frame (a shared page) shines until its page has loaded, then fades in.
  function frame(el) {
    const since = performance.now();
    el.classList.add("frame-loading");
    el.addEventListener("load", () => {
      el.classList.remove("frame-loading");
      if (performance.now() - since < GHOST_MS) return;
      el.classList.add("frame-arrived");
      el.addEventListener("animationend", () => el.classList.remove("frame-arrived"), { once: true });
    }, { once: true });
  }

  // Leaving for another page: the line runs until the next page takes over. A change of address within the page
  // (a hash, history.replaceState) and a download leave it alone.
  if (window.navigation) {
    let giveUp = null;
    window.navigation.addEventListener("navigate", (event) => {
      if (event.destination.sameDocument || event.hashChange || event.downloadRequest !== null) return;
      start();
      clearTimeout(giveUp);
      giveUp = setTimeout(() => { waits = Math.min(waits, 1); stop(); }, NAVIGATION_MS);
    });
    // A navigation that failed or stopped leaves the user on this page.
    window.navigation.addEventListener("navigateerror", () => {
      waits = Math.min(waits, 1);
      stop();
    });
  }
  // A page the browser kept and shows again on Back starts with no wait of its own.
  addEventListener("pageshow", (event) => {
    if (!event.persisted || !line) return;
    waits = 0;
    line.classList.remove("on", "done");
  });

  return { start, stop, ghost, settle, waiting, arrive, failed, frame };
})();
