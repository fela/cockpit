// SPEC UI-13 on the pages other than the main one, which has its own Reload. An installed app has no address bar and
// resumes on the page it was left on, so each page says itself when a newer Cockpit is running, and takes the key away
// if that Cockpit goes back to the one that sent the page. It never reloads by itself.
//
// Unsent writing that lives only in a page's memory crosses the reload in sessionStorage. A page gives PageReload.hold
// a function returning that writing, or null when there is none, and reads it back once with PageReload.take.
(() => {
  const heldKey = () => `cockpit.reload-held:${location.pathname}${location.search}`;
  let holder = null;

  window.PageReload = {
    hold(read) {
      holder = read;
    },
    take() {
      // In the layer over the main page there is no Reload of its own, so nothing was held for it.
      if (window.self !== window.top) return null;
      try {
        const kept = sessionStorage.getItem(heldKey());
        if (kept === null) return null;
        sessionStorage.removeItem(heldKey());
        return JSON.parse(kept);
      } catch {
        return null;
      }
    },
  };

  // In the layer over the main page, the main page's own Reload already covers this.
  if (window.self !== window.top) return;

  const CHECK_EVERY_MS = 60_000;
  const loaded = document.querySelector('meta[name="cockpit-version"]')?.content || null;
  let running = null;
  let button = null;

  const outdated = () => Boolean(loaded && running && running !== loaded);
  window.pageOutdated = outdated;

  const placeKey = () => `cockpit.reload-place:${location.pathname}${location.search}`;

  // A part pinned to the window stays where it is however the page scrolls, so it cannot mark a place.
  const movesWithPage = (part) => {
    for (let each = part; each; each = each.parentElement) {
      if (/fixed|sticky/.test(getComputedStyle(each).position)) return false;
    }
    return true;
  };

  // The part in the middle of the window, so the place survives text above it growing in the new build.
  const keepPlace = () => {
    try {
      const found = document.elementFromPoint(innerWidth / 2, innerHeight / 2)?.closest("[id]");
      const part = found && movesWithPage(found) ? found : null;
      sessionStorage.setItem(placeKey(), JSON.stringify({
        y: scrollY, id: part?.id ?? null, at: part?.getBoundingClientRect().top ?? 0,
      }));
    } catch {
      // Blocked storage: the page comes back at the top.
    }
  };

  // These pages draw after their first fetch, so the browser's own restoring finds nothing to scroll to yet. The part
  // may be an empty container that is there from the start and grows, or sit below text still to come, so this keeps
  // correcting until the part has stayed put for a second.
  const returnToPlace = () => {
    let place = null;
    try {
      place = JSON.parse(sessionStorage.getItem(placeKey()));
      sessionStorage.removeItem(placeKey());
    } catch {
      return;
    }
    if (!place) return;
    history.scrollRestoration = "manual";
    const TICK_MS = 50;
    const SETTLED_TICKS = 20;
    const GIVE_UP_AFTER_MS = 8_000;
    const USER_MOVES = ["wheel", "touchstart", "keydown", "mousedown"];
    const started = performance.now();
    let settled = 0;
    let timer = null;
    const stop = () => {
      clearInterval(timer);
      USER_MOVES.forEach((name) => removeEventListener(name, stop));
      history.scrollRestoration = "auto";
    };
    const distance = () => {
      if (!place.id) return place.y - scrollY;
      const part = document.getElementById(place.id);
      return part ? part.getBoundingClientRect().top - place.at : null;
    };
    const tryPlace = () => {
      const away = distance();
      if (away !== null && Math.abs(away) < 1) settled += 1;
      else settled = 0;
      if (away !== null) scrollBy(0, away);
      if (settled >= SETTLED_TICKS) {
        stop();
      } else if (performance.now() - started > GIVE_UP_AFTER_MS) {
        // The part is gone from the new build, so the same distance down is the nearest thing left.
        if (away === null) scrollTo(0, place.y);
        stop();
      }
    };
    USER_MOVES.forEach((name) => addEventListener(name, stop, { passive: true }));
    timer = setInterval(tryPlace, TICK_MS);
    tryPlace();
  };
  returnToPlace();

  const reloadKeepingWriting = () => {
    try {
      const writing = holder ? holder() : null;
      if (writing !== null) sessionStorage.setItem(heldKey(), JSON.stringify(writing));
    } catch {
      if (!confirm("Your unsent writing could not be kept. Reload and lose it?")) return;
    }
    keepPlace();
    location.reload();
  };

  const drawReload = () => {
    const bar = document.querySelector(".top-inner");
    if (!bar) return null;
    let right = bar.querySelector(".top-right");
    if (!right) {
      right = document.createElement("div");
      right.className = "top-right";
      bar.append(right);
    }
    const key = document.createElement("button");
    key.id = "reload";
    key.type = "button";
    key.className = "button accent icon-only";
    key.title = "A newer Cockpit is running; reloading keeps your unsent writing";
    if (window.Icons) key.append(window.Icons.icon("retry"));
    const label = document.createElement("span");
    label.className = "label";
    label.textContent = "New version · Reload";
    key.append(label);
    key.addEventListener("click", reloadKeepingWriting);
    right.append(key);
    return key;
  };

  const showReload = () => {
    if (!button && outdated()) button = drawReload();
    if (button) button.hidden = !outdated();
  };

  const check = async () => {
    if (document.visibilityState !== "visible") return;
    try {
      const response = await fetch("/api/version", { cache: "no-store" });
      window.noteAnswered?.();
      if (!response.ok) return;
      const { version } = await response.json();
      if (!version) return;
      running = version;
      showReload();
    } catch {
      // Offline or restarting: the next check tries again.
    }
  };

  document.addEventListener("visibilitychange", check);
  addEventListener("pageshow", (event) => { if (event.persisted) check(); });
  addEventListener("online", check);
  setInterval(check, CHECK_EVERY_MS);
  check();
})();
