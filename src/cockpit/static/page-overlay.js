"use strict";

// A shared page opens over Cockpit's page (SPEC NEXT-47): a click on a link to a page (`/pages/N`, or the Pages view's
// `/pages?id=N&step=M` that a step's links use) shows it in a layer over everything, with only the page and its few
// buttons, and the X, Escape or the browser's Back closes it to where the user was. The layer holds the Pages view
// in its overlay form (`&overlay=1`, pages.js), so comments, their quotes and the page's choices go to the step that
// opened it exactly as in the Pages view. A click with a modifier key, or the middle button, still opens a new tab.
// The layer takes its place at once on the click; until the Pages view in it says its page has drawn, it shows a
// loading sheet in the page's place, with the page's name, and the X, Escape and Back close it as they close a page.
// The pages linked from the first two step cards load ahead in hidden layers, so a click on one shows it at once.
// A link to a briefing (`/briefing?id=N`) opens the same way, with the Briefing view in its overlay form
// (`&overlay=1`, briefing.js) in the layer: the briefing alone, without the Briefing page's header and tabs. It
// never loads ahead, as opening a briefing marks it read on the Briefing tab.
//
// Beside the step: on a wide screen, a page opened from a step's card slides in from the right and the
// card moves to a column at the left, with the rest of Cockpit's page out of view. The card stays live: the user
// reads the page beside the question it answers, and the comments they leave on the page show in the card's reply
// box, whose Send takes them with the answer (`&beside=1` leaves the Pages view's own reply box out). When the card
// leaves (Send, or another step takes its place), the page closes with it. Elsewhere, and on a phone, the page
// slides over the whole window: from the right on a wide screen, from the bottom on a phone.
(() => {
  // How long the loading sheet says only "Loading" before it adds that Cockpit is slow, and before it says the page
  // did not load, with a link to open it in a tab of its own.
  const SLOW_MS = 8e3;
  const FAILED_MS = 45e3;
  // How many pages load ahead, and how long after Cockpit's page last redrew: its first draws come in a burst.
  const PRELOADS = 2;
  const PRELOAD_MS = 1500;
  // The comments the Pages view kept as its layer closed (pages.js), as [page id, comment] pairs: the next layer
  // takes them as it starts, so none loads ahead while one waits. They live in this page, which outlives the layer's
  // frame; the tab's sessionStorage carries them across a reload where the browser lets it.
  const UNFINISHED_KEY = "cockpit-unfinished-comments";
  let unfinished = [];
  try {
    unfinished = JSON.parse(sessionStorage.getItem(UNFINISHED_KEY) || "[]");
  } catch {
    // Storage blocked or its value broken: no comment waits from before the reload.
  }
  window.cockpitUnfinished = {
    get: () => unfinished,
    set: (list) => {
      unfinished = list;
      try {
        if (list.length) sessionStorage.setItem(UNFINISHED_KEY, JSON.stringify(list));
        else sessionStorage.removeItem(UNFINISHED_KEY);
      } catch {
        // Storage blocked or full: the comments wait in this page only, until a reload.
      }
    },
  };
  // How long a close waits for its own Back to arrive.
  const UNWIND_MS = 1500;
  // From this width a page opened from a step's card shows beside it; below it the page takes the whole window.
  const WIDE = matchMedia("(min-width: 1100px)");
  const PHONE = matchMedia("(max-width: 700px)");
  const STILL = matchMedia("(prefers-reduced-motion: reduce)");
  const EASE = "cubic-bezier(.2, .8, .2, 1)";
  const IN_MS = 320;
  const OUT_MS = 220;
  // The sections of Cockpit's page whose step cards a page can open beside.
  const CARD_HOMES = ["next", "queue"];
  let dialog = null;
  // The layers loading ahead, hidden, by their frame's address; a click on a link to one shows it.
  const preloads = new Map();
  let preloadTimer = 0;
  // The pages the shown layer has shown, whose layers loading ahead load again once it closes.
  const shownPages = new Set();
  // What had the keyboard when the user opened the page, which gets it back when the page closes; and where its link
  // sits on a step's card ({stepId, home, href}), to find it again after a redraw of Cockpit's page drew it anew.
  let opener = null;
  let openerPlace = null;
  // Whether opening pushed a history entry, which closing takes back off.
  let pushed = false;
  // A close's own Back has not arrived yet; a page opened meanwhile waits for it (`queued`), so that Back leaves it.
  let unwinding = false;
  let unwindTimer = 0;
  let queued = null;
  // What waits for that Back too: a task to open on the main page (whenUnwound).
  const afterUnwind = [];
  // The step the shown or opening page sits beside, {stepId, home}, or null for a page over the whole window.
  let beside = null;
  // Whether Cockpit's page is laid out with the step's card at the left (html.page-beside).
  let besideShown = false;
  // Where the user had scrolled Cockpit's page before the card moved to the left, for when it moves back; and where
  // they have scrolled the column at the left, which a redraw of the card keeps.
  let scrolled = 0;
  let columnTop = 0;
  let observer = null;

  // The overlay's address for a link, or null when the link is not to a shared page or a briefing. A briefing's is
  // `briefing=N`, apart from the pages' ids, which name what the layer has shown (shownPages).
  function overlaySrc(link) {
    let url;
    try {
      url = new URL(link.href, location.href);
    } catch {
      return null;
    }
    if (url.origin !== location.origin) return null;
    const own = /^\/pages\/(\d+)$/.exec(url.pathname);
    const params = new URLSearchParams();
    if (url.pathname === "/briefing" && /^\d+$/.test(url.searchParams.get("id") || "")) params.set("briefing", url.searchParams.get("id"));
    else if (own && !url.search && !url.hash) params.set("id", own[1]);
    else if (url.pathname === "/pages" && /^\d+$/.test(url.searchParams.get("id") || "")) {
      params.set("id", url.searchParams.get("id"));
      for (const name of ["step", "comment"]) if (url.searchParams.has(name)) params.set(name, url.searchParams.get(name));
    } else return null;
    params.set("overlay", "1");
    return params;
  }

  // The step's card in a section of Cockpit's page, leaving out the copies a close or Send flies off the page.
  function cardIn(home, stepId) {
    const section = document.getElementById(home);
    if (!section) return null;
    return [...section.querySelectorAll(`[data-step="${stepId}"]`)].find((el) => !el.closest(".motion-ghost, .close-copy")) || null;
  }

  // The step a link sits in, when the page it opens can show beside that step's card: a wide screen, the link in
  // the card itself, and the page opened for that step.
  function besideFor(link, params) {
    const stepId = params.get("step");
    if (!WIDE.matches || !stepId) return null;
    for (const home of CARD_HOMES) {
      const card = cardIn(home, stepId);
      if (card && card.contains(link)) return { stepId, home };
    }
    return null;
  }

  // Marks the card and the elements that hold it, so the stylesheet shows only them in the column at the left.
  // A redraw of Cockpit's page draws the card anew, so this runs again after each one; it returns the card.
  function markPath() {
    const marked = document.querySelector(".beside-card");
    if (marked && beside && marked.dataset.step === beside.stepId && marked.closest(`#${beside.home}`)) return marked;
    unmark();
    const card = beside && cardIn(beside.home, beside.stepId);
    if (!card) return null;
    card.classList.add("beside-card");
    const main = card.closest("main");
    for (let el = card.parentElement; el && main.contains(el); el = el.parentElement) el.classList.add("beside-path");
    return card;
  }

  function unmark() {
    for (const el of document.querySelectorAll(".beside-path, .beside-card")) el.classList.remove("beside-path", "beside-card");
  }

  function animate(el, frames, ms, id) {
    if (!el || STILL.matches) return Promise.resolve();
    return el.animate(frames, { duration: ms, easing: EASE, id }).finished.catch(() => {});
  }

  // Moves the card between its place on Cockpit's page and the column at the left, sliding from where it was.
  function layOut(on) {
    if (on === besideShown) return;
    const before = beside && cardIn(beside.home, beside.stepId);
    const from = before && before.getBoundingClientRect();
    if (on) scrolled = scrollY;
    besideShown = on;
    document.documentElement.classList.toggle("page-beside", on);
    dispatchEvent(new CustomEvent("cockpit:layout-moved"));
    const card = on ? markPath() : before;
    if (!on) {
      unmark();
      scrollTo(0, scrolled);
    }
    if (!card || !from) return;
    const to = card.getBoundingClientRect();
    animate(card, [{ transform: `translate(${from.left - to.left}px, ${from.top - to.top}px)` }, { transform: "none" }], IN_MS, "beside-slide");
  }

  // The card's column follows each redraw of Cockpit's page, keeping where the user scrolled it. app.js calls this
  // inside its redraw, so the new card is in the column before anything measures it.
  function placeCard() {
    if (!besideShown || !beside || !markPath()) return;
    const column = document.querySelector("main.layout");
    if (column && column.scrollTop !== columnTop) column.scrollTop = columnTop;
  }

  // The observer catches every other change; once the card is gone (Send, Dismiss, another step in its place), the
  // page goes with it.
  function follow() {
    if (!beside) return;
    if (besideShown ? markPath() : cardIn(beside.home, beside.stepId)) placeCard();
    else close(true);
  }

  function watchCard() {
    observer = new MutationObserver(follow);
    observer.observe(document.querySelector("main") || document.body, { childList: true, subtree: true });
  }

  function stopWatching() {
    observer?.disconnect();
    observer = null;
  }

  // The frame the layer shows, and the one loading to take its place (a swap), or null.
  const shownFrame = () => dialog && dialog.querySelector("iframe.page-overlay-frame:not(.page-overlay-next)");
  const nextFrame = () => dialog && dialog.querySelector("iframe.page-overlay-next");

  function makeFrame(params, title) {
    const frame = document.createElement("iframe");
    frame.className = "page-overlay-frame";
    // The page inside may go full screen (NEXT-56); each frame around it has to allow that.
    frame.allow = "fullscreen";
    frame.src = params.has("briefing") ? `/briefing?id=${params.get("briefing")}&overlay=1` : `/pages?${params}`;
    // The frame's name for screen readers; a title would show as a hover label over the whole page (motion.js).
    frame.setAttribute("aria-label", title || "Shared page");
    return frame;
  }

  // Another page from the card the shown page sits beside: it loads hidden in the same layer, under the loading
  // line along the layer's top, and takes the shown one's place once it has drawn. The shown page keeps a comment
  // being written first, so the new one starts with it; a page still loading from an earlier click gives way.
  function swapTo(params, title, href) {
    shownPages.add(params.get("id"));
    nextFrame()?.remove();
    keepComment(shownFrame());
    const frame = makeFrame(params, title);
    // A layer still loading its first page loads the new one in its place, under the same loading sheet.
    if (dialog.classList.contains("page-overlay-loading-page")) {
      shownFrame().replaceWith(frame);
      dialog.querySelector(".page-overlay-name").textContent = title || "Shared page";
      dialog.setAttribute("aria-label", title || "Shared page");
      // The sheet starts over for the new page: its own times, and its own link should it not load.
      dialog.timers?.forEach(clearTimeout);
      dialog.querySelector(".page-overlay-loading")?.classList.remove("failed");
      dialog.querySelector(".page-overlay-note").textContent = "Loading the page";
      startWaiting(dialog, href);
      return;
    }
    frame.classList.add("page-overlay-next");
    dialog.append(frame);
    dialog.classList.add("page-overlay-swapping");
  }

  function swapped(frame) {
    const old = shownFrame();
    // What the user wrote on the shown page while this one loaded goes over too.
    keepComment(old);
    try {
      frame.contentWindow?.cockpitPagesTake?.();
    } catch {
      // A frame Cockpit cannot reach: it keeps the comments it read at its start.
    }
    frame.classList.remove("page-overlay-next");
    dialog.classList.remove("page-overlay-swapping");
    old?.remove();
    dialog.setAttribute("aria-label", frame.getAttribute("aria-label"));
    if (keyboardInLayer(dialog)) frame.focus();
    animate(frame, [{ opacity: 0 }, { opacity: 1 }], OUT_MS);
  }

  // Whether the keyboard is in the layer or nowhere, so moving it to the page takes nothing from the user, who may
  // have gone on to the card's reply box beside it meanwhile.
  function keyboardInLayer(layer) {
    const focus = document.activeElement;
    return !focus || focus === document.body || layer.contains(focus);
  }

  // A page from the same card that loaded ahead takes the shown page's place at once: the layer loading ahead shows
  // where the shown one was, which goes, and the history entry and the card's column stay as they are.
  function promote(layer, params, title, href) {
    const old = dialog;
    old.timers?.forEach(clearTimeout);
    shownPages.add(params.get("id"));
    nameLayer(layer, title);
    layer.classList.remove("page-overlay-preload");
    layer.inert = false;
    for (const frame of layer.querySelectorAll("iframe")) frame.contentWindow?.postMessage({ cockpitOverlay: "shown" }, location.origin);
    const focus = keyboardInLayer(old);
    const outside = document.activeElement;
    dialog = layer;
    // Showing the layer moves the keyboard into it, which goes back to where the user had it outside the layer.
    layer.show();
    old.remove();
    if (!focus) outside?.focus({ preventScroll: true });
    if (layer.classList.contains("page-overlay-loading-page")) {
      if (focus) layer.querySelector(".page-overlay-close").focus();
      startWaiting(layer, href);
    } else if (focus) shownFrame().focus();
    animate(layer, [{ opacity: 0 }, { opacity: 1 }], OUT_MS);
  }

  // A layer loaded ahead was named by the first link to its page on the card; the link clicked names it as it shows.
  // The loading sheet, with its own line for the name, is gone once the page has drawn.
  function nameLayer(layer, title) {
    const name = title || "Shared page";
    layer.setAttribute("aria-label", name);
    layer.querySelector("iframe.page-overlay-frame").setAttribute("aria-label", name);
    const sheetName = layer.querySelector(".page-overlay-name");
    if (sheetName) sheetName.textContent = name;
  }

  function el(tag, className, text) {
    const made = document.createElement(tag);
    if (className) made.className = className;
    if (text) made.textContent = text;
    return made;
  }

  // The sheet in the page's place while it loads: the page's name, a line on what happens, and the outline of a
  // page drawing itself, a heading, lines of text and a chart, which shimmer and rise until the page is there.
  function loadingSheet(title) {
    const sheet = el("div", "page-overlay-loading");
    sheet.setAttribute("role", "status");
    const body = el("div", "page-overlay-sheet");
    const note = el("p", "page-overlay-note", "Loading the page");
    const sketch = el("div", "page-overlay-sketch");
    sketch.setAttribute("aria-hidden", "true");
    sketch.append(el("span", "sketch-line sketch-wide"), el("span", "sketch-line"), el("span", "sketch-line sketch-short"));
    const chart = el("span", "sketch-chart");
    for (let i = 0; i < 7; i += 1) {
      const bar = el("i");
      bar.style.setProperty("--n", i);
      chart.append(bar);
    }
    sketch.append(chart, el("span", "sketch-line"), el("span", "sketch-line sketch-short"));
    body.append(el("p", "page-overlay-name", title || "Shared page"), note, sketch);
    sheet.append(body);
    return sheet;
  }

  // A layer for a page: its frame, the loading sheet over it, the loading line along its top and the X. It loads
  // hidden ahead of a click, or shows at once on one.
  function makeLayer(params, title) {
    const layer = el("dialog", `page-overlay page-overlay-loading-page${params.has("beside") ? " page-overlay-beside" : ""}`);
    layer.setAttribute("aria-label", title || "Shared page");
    const shut = el("button", "button small ghost page-overlay-close");
    shut.type = "button";
    shut.title = "Close (Esc)";
    shut.setAttribute("aria-label", "Close the page");
    shut.append(Icons.icon("x"));
    shut.addEventListener("click", () => close(true));
    layer.append(makeFrame(params, title), loadingSheet(title), el("div", "page-overlay-bar"), shut);
    // Escape on Cockpit's own page; the layer's frame sends its own Escape as a message.
    layer.addEventListener("cancel", (event) => {
      event.preventDefault();
      close(true);
    });
    return layer;
  }

  // The page in a layer has drawn: the sheet fades off it, and the keyboard moves to the page unless the user has
  // gone on to something else meanwhile (the card's reply box beside it).
  function loaded(layer) {
    if (!layer.classList.contains("page-overlay-loading-page")) return;
    layer.timers?.forEach(clearTimeout);
    layer.classList.remove("page-overlay-loading-page");
    const sheet = layer.querySelector(".page-overlay-loading");
    if (layer !== dialog) {
      sheet?.remove();
      return;
    }
    if (keyboardInLayer(layer)) shownFrame().focus();
    animate(sheet, [{ opacity: 1 }, { opacity: 0 }], OUT_MS).then(() => sheet?.remove());
  }

  // A layer still loading when it shows says so after a while, and later that it did not load.
  function startWaiting(layer, href) {
    const note = layer.querySelector(".page-overlay-note");
    if (!note) return;
    layer.timers = [
      setTimeout(() => { note.textContent = "Still loading: Cockpit is slow right now"; }, SLOW_MS),
      setTimeout(() => {
        const tab = el("a", "page-overlay-tab", "Open it in a new tab");
        tab.href = href;
        tab.target = "_blank";
        tab.rel = "noopener";
        tab.addEventListener("click", () => close(true));
        note.textContent = "Cockpit could not load this page. ";
        note.append(tab);
        layer.querySelector(".page-overlay-loading")?.classList.add("failed");
      }, FAILED_MS),
    ];
  }

  function open(params, title, href, nextBeside, place) {
    if (unwinding) {
      queued = [params, title, href, nextBeside, place];
      return;
    }
    if (dialog && beside && nextBeside && nextBeside.stepId === beside.stepId && nextBeside.home === beside.home) {
      params.set("beside", "1");
      // The shown page keeps a comment being written first; with one kept, the new page loads fresh to take it.
      keepComment(shownFrame());
      const key = params.toString();
      const ahead = preloads.get(key);
      preloads.delete(key);
      if (ahead && ahead.isConnected && !keptComment()) promote(ahead, params, title, href);
      else {
        ahead?.remove();
        swapTo(params, title, href);
      }
      return;
    }
    // Another page in place of the shown one keeps its history entry: one Back still closes the layer.
    if (dialog) {
      const had = pushed;
      close(false);
      pushed = had;
    }
    beside = nextBeside;
    if (beside) params.set("beside", "1");
    opener = document.activeElement;
    openerPlace = place;
    shownPages.add(params.get("id"));
    const key = params.toString();
    let layer = preloads.get(key);
    preloads.delete(key);
    // A comment kept from the last layer goes to the next one, which reads it as it starts.
    if (layer && (!layer.isConnected || keptComment())) {
      layer.remove();
      layer = null;
    }
    if (layer) {
      nameLayer(layer, title);
      layer.classList.remove("page-overlay-preload");
      layer.inert = false;
      for (const frame of layer.querySelectorAll("iframe")) frame.contentWindow?.postMessage({ cockpitOverlay: "shown" }, location.origin);
    } else {
      layer = makeLayer(params, title);
      document.body.append(layer);
    }
    dialog = layer;
    document.documentElement.classList.add("page-overlay-open");
    // Beside the step the layer is not modal, so the card at the left takes clicks and typing.
    if (beside) layer.show(); else layer.showModal();
    if (layer.classList.contains("page-overlay-loading-page")) {
      layer.querySelector(".page-overlay-close").focus();
      startWaiting(layer, href);
    } else shownFrame().focus();
    if (beside) {
      if (!observer) watchCard();
      layOut(true);
    }
    const from = beside || !PHONE.matches ? "translateX(100%)" : "translateY(100%)";
    animate(layer, [{ transform: from }, { transform: "none" }], IN_MS);
    if (!pushed) {
      history.pushState({ cockpitPageOverlay: true }, "", location.href);
      pushed = true;
    }
  }

  // A link names the layer it opens by its words, or by the page's caption when its words are an action.
  function linkName(link) {
    return link.dataset.pageName || link.textContent.trim();
  }

  function keptComment() {
    return unfinished.length > 0;
  }

  // The pages linked from the first step cards on Cockpit's page, in order, as the layer would open them: these
  // load ahead, hidden, at the size they show at, so a chart measures the window it will show in.
  function preload() {
    if (dialog || unwinding) return;
    const wanted = new Map();
    if (!keptComment()) {
      for (const home of CARD_HOMES) {
        for (const link of document.querySelectorAll(`#${home} [data-step] a[href]`)) {
          if (wanted.size >= PRELOADS) break;
          if (link.hasAttribute("download") || link.closest(".motion-ghost, .close-copy") || link.matches(".figure-comment")) continue;
          const params = overlaySrc(link);
          if (!params || params.has("briefing")) continue;
          if (besideFor(link, params)) params.set("beside", "1");
          const key = params.toString();
          if (!wanted.has(key)) wanted.set(key, [params, linkName(link)]);
        }
      }
    }
    for (const [key, layer] of preloads) {
      if (wanted.has(key)) continue;
      layer.remove();
      preloads.delete(key);
    }
    for (const [key, [params, title]] of wanted) {
      if (preloads.has(key)) continue;
      const layer = makeLayer(params, title);
      layer.classList.add("page-overlay-preload");
      layer.inert = true;
      document.body.append(layer);
      preloads.set(key, layer);
    }
  }

  function preloadSoon() {
    clearTimeout(preloadTimer);
    preloadTimer = setTimeout(preload, PRELOAD_MS);
  }

  // The Pages view keeps a comment being written for the next layer on its page (pages.js).
  function keepComment(frame) {
    try {
      frame?.contentWindow?.cockpitPagesLeaving?.();
    } catch (error) {
      logError("keeping a comment as the page closed", error);
    }
  }

  // `back`: the close came from the X or Escape, so the history entry opening pushed goes too; Back took it already.
  function close(back) {
    if (!dialog) return;
    const leaving = dialog;
    dialog = null;
    leaving.timers?.forEach(clearTimeout);
    const wasBeside = besideShown;
    stopWatching();
    keepComment(leaving.querySelector("iframe.page-overlay-frame:not(.page-overlay-next)"));
    // A page still loading goes at once, so it cannot start during the slide away and take the comment just kept,
    // which belongs to the next layer; its loading sheet slides away in its place.
    leaving.querySelector("iframe.page-overlay-next")?.remove();
    if (leaving.classList.contains("page-overlay-loading-page")) leaving.querySelector("iframe")?.remove();
    const to = wasBeside || !PHONE.matches ? "translateX(100%)" : "translateY(100%)";
    const gone = () => {
      if (leaving.open) leaving.close();
      leaving.remove();
      if (!dialog) document.documentElement.classList.remove("page-overlay-open");
    };
    // The layer takes no more clicks while it slides away.
    leaving.inert = true;
    // What the user chose or commented there shows on the same pages loading ahead once they load again.
    for (const [key, layer] of preloads) {
      if (!shownPages.has(new URLSearchParams(key).get("id"))) continue;
      layer.remove();
      preloads.delete(key);
    }
    shownPages.clear();
    animate(leaving, [{ transform: "none" }, { transform: to }], OUT_MS).then(gone);
    layOut(false);
    beside = null;
    // The keyboard goes back to the link the page was opened from, while the main page still has it; a redraw may
    // have drawn that link anew on its card.
    const link = opener && opener.isConnected ? opener : openerPlace && [...(cardIn(openerPlace.home, openerPlace.stepId)
      ?.querySelectorAll(`a[href="${CSS.escape(openerPlace.href)}"]`) || [])].find((each) => each.getClientRects().length > 0);
    if (link) link.focus({ preventScroll: true });
    opener = null;
    openerPlace = null;
    if (back && pushed) {
      unwinding = true;
      history.back();
      // A page's own script can add a history entry in its frame, which that Back then takes instead, with no
      // popstate here: a page opened meanwhile waits no longer than this.
      clearTimeout(unwindTimer);
      unwindTimer = setTimeout(() => unwindLate(1), UNWIND_MS);
    }
    pushed = false;
    // What the user sent or chose in the layer shows on the step now. A choice still saving goes on in this
    // window (page-choices.js), and shows once saved.
    dispatchEvent(new CustomEvent("cockpit:page-closed"));
    preloadSoon();
  }

  document.addEventListener("click", (event) => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    // While the user picks a part of Cockpit's page (app.js), a click on a link picks the link; app.js's own
    // listener comes later on the same node, so it cannot stop this one first.
    if (document.body.classList.contains("picking")) return;
    const link = event.target.closest && event.target.closest("a[href]");
    // The loading sheet's own link, once the page did not load, opens it in a tab of its own.
    if (!link || link.hasAttribute("download") || link.classList.contains("page-overlay-tab")) return;
    // Links inside an element marked data-center-links open in the center layer (center.js), in the main area
    // between the side columns. A part's side panel marks itself so.
    if (link.closest("[data-center-links]")) return;
    const params = overlaySrc(link);
    if (!params) return;
    event.preventDefault();
    const card = link.closest("[data-step]");
    const home = card && CARD_HOMES.find((id) => card.closest(`#${id}`));
    const place = home ? { stepId: card.dataset.step, home, href: link.getAttribute("href") } : null;
    // A briefing's layer is named for what it holds, not for its link's words.
    open(params, params.has("briefing") ? "Morning briefing" : linkName(link), link.href, besideFor(link, params), place);
  }, true);

  addEventListener("popstate", () => {
    if (!unwinding) {
      close(false);
      return;
    }
    unwound();
  });

  // A close's Back did not arrive: while the layer's own entry is still the current one, Back again takes it, so
  // the history is as before the layer opened; after a few tries, a page opened meanwhile waits no longer.
  function unwindLate(tries) {
    if (!unwinding) return;
    logError("closing a page", new Error("Back took a history entry of the page's frame, not the layer's"));
    if (history.state?.cockpitPageOverlay && tries < 3) {
      history.back();
      unwindTimer = setTimeout(() => unwindLate(tries + 1), UNWIND_MS);
    } else unwound();
  }

  // Runs `then` once the history is as before the layer opened: at once, or when the close's Back has come.
  function whenUnwound(then) {
    if (unwinding) afterUnwind.push(then);
    else then();
  }

  function unwound() {
    unwinding = false;
    clearTimeout(unwindTimer);
    for (const then of afterUnwind.splice(0)) then();
    if (queued) {
      const [params, title, href, nextBeside, place] = queued;
      queued = null;
      open(params, title, href, nextBeside, place);
    } else preloadSoon();
  }

  // The layer's Pages view says its page has drawn, asks to close (Escape pressed in it or in the page it frames),
  // or says the user's comments on the page changed, which the step's card beside it shows. A layer loading ahead
  // only says it has drawn.
  addEventListener("message", (event) => {
    if (event.origin !== location.origin || !event.data) return;
    const ahead = [...preloads.values()].find((layer) => layer.querySelector("iframe")?.contentWindow === event.source);
    if (ahead) {
      if (event.data.cockpitOverlay === "ready") loaded(ahead);
      return;
    }
    if (!dialog) return;
    const frame = [...dialog.querySelectorAll("iframe")].find((each) => event.source === each.contentWindow);
    if (!frame) return;
    const next = frame.classList.contains("page-overlay-next");
    if (event.data.cockpitOverlay === "ready") {
      if (next) swapped(frame);
      else loaded(dialog);
    } else if (event.data.cockpitOverlay === "close") close(true);
    // A link to a task in the layer, a briefing's mention of it or a step's task above its page: the layer closes
    // and Cockpit's page opens the task (app.js) once the close's Back has come, so the task's address goes on
    // the main page's own history entry, not on the layer's. It opens after every listener on that Back has run,
    // sidebar.js's among them, so none of them redraws the page from the address the task's opening replaced.
    else if (event.data.cockpitOverlay === "item" && !next && Number.isInteger(event.data.id) && event.data.id > 0) {
      const id = event.data.id;
      close(true);
      whenUnwound(() => setTimeout(() => dispatchEvent(new CustomEvent("cockpit:open-item", { detail: id })), 0));
    } else if (event.data.cockpitOverlay === "changed") dispatchEvent(new CustomEvent("cockpit:page-changed"));
    // Words selected or a part picked on the page beside the step, quoted into the card's reply box (app.js).
    else if (event.data.cockpitOverlay === "quote" && !next && beside && typeof event.data.text === "string") {
      dispatchEvent(new CustomEvent("cockpit:page-quote", { detail: { stepId: Number(beside.stepId), text: event.data.text } }));
    }
    // The user's input over the layer: activity for the timer of the step that opened the page (app.js), named
    // by the frame's own address, which Cockpit set.
    else if (event.data.cockpitOverlay === "activity") {
      const step = Number(new URL(frame.src).searchParams.get("step")) || null;
      dispatchEvent(new CustomEvent("cockpit:page-activity", { detail: step }));
    }
  });

  // A comment deleted from the card's reply box leaves the page's comment column too, and that of a page loading
  // in its place.
  addEventListener("cockpit:page-comments-changed", () => {
    for (const frame of dialog ? dialog.querySelectorAll("iframe") : []) frame.contentWindow?.postMessage({ cockpitOverlay: "reload" }, location.origin);
  });

  // The column at the left keeps where the user scrolled it through redraws of the card (follow).
  document.addEventListener("scroll", (event) => {
    if (besideShown && event.target instanceof Element && event.target.matches("main.layout")) columnTop = event.target.scrollTop;
  }, { capture: true, passive: true });

  // Beside the step the layer is not modal, so Escape on the card closes it, unless the user is writing or a menu
  // on the card takes it.
  document.addEventListener("keydown", (event) => {
    if (!besideShown || event.key !== "Escape" || event.defaultPrevented) return;
    if (event.target.closest && event.target.closest("textarea, input, select, [contenteditable], details[open]")) return;
    close(true);
  });

  // A window narrowed below the width for two columns: the page takes the whole window.
  WIDE.addEventListener("change", () => {
    if (!WIDE.matches && beside) close(true);
    // A page loading ahead beside its card would now open over the whole window, or the other way round.
    preloadSoon();
  });

  window.PageOverlay = {
    // The step whose card a page shows beside, or null.
    besideStep: () => (besideShown && beside ? Number(beside.stepId) : null),
    // Cockpit's page has drawn its sections anew (app.js, inside the redraw): the card keeps its column, and the
    // pages loading ahead follow the cards now first.
    redrawn: () => {
      placeCard();
      preloadSoon();
    },
  };
})();
