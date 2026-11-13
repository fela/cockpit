"use strict";

// Which controls the user presses, and which hover texts they stop on (SPEC UI-45), so a control used rarely
// can move out of the way and one never used can go. A control is named by the part of the page it sits in and
// what it says: `work/row chip "Fire and forget"`. Words that differ per item (an answer, a title, a session's name) are
// left out of the name and never sent, so every step's answer button counts as one control. A key pressed
// outside a text field counts as `key "a"`. Once a day, each control the page shows counts as seen, so the
// report can name those never pressed. Counts go to Cockpit in batches every 15 s and when the page is
// hidden; a batch Cockpit did not answer goes again under the same id, and one it refused is dropped. Each press
// also goes as itself, with its time and the card and task it was on (SPEC TRACK-14), so the order of what the user
// did can be followed.

(() => {
  const CONTROLS = "button, a[href], summary, select, input[type=checkbox], input[type=radio], [role=button], [data-tip], [data-tip-card]";
  const PRESSABLE = "button, a[href], summary, select, input[type=checkbox], input[type=radio], [role=button]";
  // The parts of the page a control is named by, nearest first.
  const PARTS = ".card, .row, .side-row, .tip, dialog, section[id], aside[id], header, nav, [id]";
  const SEND_MS = 15e3;
  // A keepalive request may carry at most 64 KiB, and all of a page's keepalive requests share those 64 KiB.
  const BATCH_BYTES = 48e3;
  const SEEN_MS = 60e3;
  // A hover text shown this long was read, not passed over on the way elsewhere.
  const READ_MS = 700;
  const page = location.pathname;

  const counts = new Map();
  // Presses not yet sent, oldest first.
  const presses = [];
  const seenToday = new Set();
  let seenDay = "";

  function words(el) {
    const said = el.getAttribute("aria-label") || (el.dataset.tip || "").split("\n")[0] || el.textContent || "";
    return said.replace(/\s+/g, " ").trim();
  }

  function partName(el) {
    const names = [];
    for (let part = el.parentElement && el.parentElement.closest(PARTS); part && names.length < 2;
      part = part.parentElement && part.parentElement.closest(PARTS)) {
      names.unshift(part.id || part.classList[0] || part.tagName.toLowerCase());
      if (part.id) break;
    }
    return names.join("/") || "page";
  }

  // Classes that say how a control looks, not which it is.
  const LOOKS = new Set(["button", "small", "ghost", "icon-only", "phone-only", "active", "on"]);
  // Lists of items and answers: the words there are the item's own, so they do not name the control.
  const OWN_WORDS = ".row, .side-row, .sidebar-sessions, .session-log, .answers, .prose, .row-body, .task-body, .hist-body";
  // A menu on an item's row can hold the user's own words (a goal), so its choices are named by place.
  const MENU = ".menu, [role=menu], [role=listbox]";

  function named(el, said) {
    if (!el.matches(PRESSABLE) || el.closest(OWN_WORDS)) return false;
    if (el.matches("a") && !el.closest("header, nav")) return false;
    return said.split(" ").length <= 3 && said.length <= 32;
  }

  function nameOf(el) {
    // A control marked with `data-use` is named by it alone: its words are a session's name or the like.
    if (el.dataset.use) return { control: `${partName(el)} ${el.dataset.use}` };
    // An icon-only control's name comes before any colon: "Session: release-notes" is "Session".
    const said = el.classList.contains("icon-only") ? words(el).split(/[:.]/)[0].slice(0, 32) : words(el);
    const kind = [...el.classList].find((name) => !LOOKS.has(name)) || el.tagName.toLowerCase();
    const menu = el.closest(MENU);
    if (menu && el.closest(OWN_WORDS) && el.matches("button")) {
      const choices = [...menu.querySelectorAll("button")];
      return { control: `${partName(menu)} ${menu.classList[menu.classList.length - 1]} choice ${choices.indexOf(el) + 1}` };
    }
    if (!el.classList.contains("icon-only") && !named(el, said)) return { control: `${partName(el)} ${kind}` };
    // A count in a label ("3 comments") varies; a choice's number ("7 days") is the choice.
    const label = el.hasAttribute("aria-pressed") ? said : said.replace(/\d+/g, "#");
    return { control: `${partName(el)} ${kind} "${label}"` };
  }

  function add(control, kind, n) {
    const key = `${kind}\n${control}`;
    const entry = counts.get(key) || { control, kind, count: 0 };
    entry.count += n;
    counts.set(key, entry);
  }

  function markSeen(control) {
    if (seenToday.has(control)) return;
    seenToday.add(control);
    add(control, "seen", 1);
  }

  function pressed(control, el) {
    const step = el && el.closest("[data-step]");
    const item = el && el.closest("[data-item]");
    const id = (value) => (/^\d+$/.test(value || "") ? Number(value) : null);
    presses.push({ at: new Date().toISOString(), control, item_id: id(item && item.dataset.item), step_id: id(step && step.dataset.step) });
  }

  function count(el, kind) {
    const { control } = nameOf(el);
    add(control, kind, 1);
    if (kind === "press") pressed(control, el);
    // A menu choice pressed between two scans was on the page too.
    markSeen(control);
  }

  function noteSeen() {
    // A Pages view loading ahead, hidden (pages.js): the user has seen none of its controls yet.
    if (window.cockpitHold) return;
    const day = new Date().toISOString().slice(0, 10);
    if (day !== seenDay) { seenDay = day; seenToday.clear(); }
    for (const el of document.querySelectorAll(CONTROLS)) {
      // A page loading ahead, laid out but hidden and inert (page-overlay.js), shows the user nothing.
      if (!el.getClientRects().length || el.closest("[inert]")) continue;
      markSeen(nameOf(el).control);
    }
  }

  // The batch on its way: sent again, under the same id, until Cockpit answers, which counts it once.
  let pending = null;
  // A send that fails is no error yet, as the batch goes again at the next send: a request with no answer at all (a
  // phone waking before its network is back, a dropped wifi) or a proxy's 502 while Cockpit restarts. Sends still
  // failing after this long are an error (SPEC CODE-2). The time counts only while the page is visible and online, so
  // a phone that wakes for a moment, fails one send and sleeps again stays quiet however long it sleeps. A send with no
  // answer counts only up to Cockpit's last answer to any request of the page before it went: the browser stays
  // online on a wifi with no way out, so only an answer shows that the page's connection worked while a send failed.
  // The batch is held in memory only, so a page closed during the wait loses it, as a closed page always has.
  const AWAY_QUIET_MS = 120e3;
  // `awayClock()` (errors.js) at the first failed send since Cockpit last answered, or null.
  let failingSince = null;
  const clock = () => (window.awayClock ? window.awayClock() : { at: performance.now(), away: 0, answered: null });

  // How long sends have failed, in ms of the page visible and online, from when the first failed send went out (`sentAt`
  // for this one: a request with no answer can hang for minutes before it fails) to `until`, a reading after it.
  function failingFor(sentAt, until) {
    failingSince ??= sentAt;
    return until.at - failingSince.at - (until.away - failingSince.away);
  }

  function nextBatch() {
    const batch = { batch: crypto.randomUUID(), page, uses: [], presses: [] };
    let bytes = new Blob([JSON.stringify(batch)]).size;
    for (const use of counts.values()) {
      bytes += new Blob([JSON.stringify(use)]).size + 1;
      if (batch.uses.length === 500 || bytes > BATCH_BYTES) break;
      batch.uses.push(use);
    }
    for (const use of batch.uses) counts.delete(`${use.kind}\n${use.control}`);
    for (const press of presses) {
      bytes += new Blob([JSON.stringify(press)]).size + 1;
      if (batch.presses.length === 500 || bytes > BATCH_BYTES) break;
      batch.presses.push(press);
    }
    presses.splice(0, batch.presses.length);
    return JSON.stringify(batch);
  }

  function send() {
    if (pending === null && !counts.size && !presses.length) return;
    const body = pending || nextBatch();
    pending = body;
    const sentAt = clock();
    // Only a send while the page is hidden needs to outlive it: the page may close without another word. Chromium
    // counts a keepalive request's body against the page's 64 KiB until the page reads Cockpit's answer, or for as
    // long as the request hangs: a few batches sent that way leave too little room, and the browser refuses every
    // send after them at once, as a failed request (SPEC CODE-2). So a send while the page shows is an ordinary
    // request, and each answer is read.
    const keepalive = document.visibilityState === "hidden";
    fetch("/api/control-uses", { method: "POST", headers: { "Content-Type": "application/json" }, body, keepalive })
      .then((answer) => {
        window.noteAnswered?.();
        answer.arrayBuffer().catch(() => {});
        // A refused batch would be refused again, so only a failed request or a server error sends it again.
        if ((answer.ok || answer.status < 500) && pending === body) pending = null;
        if (!window.answerIsAway?.(answer.status)) failingSince = null;
        else if (failingFor(sentAt, clock()) < AWAY_QUIET_MS) return;
        if (!answer.ok) throw new Error(`control uses: Cockpit answered ${answer.status}`);
      }, (failure) => {
        const ms = failingFor(sentAt, clock());
        // A send failing while the browser is offline is the user's network, however long sends have failed.
        if (!navigator.onLine || !sentAt.answered || failingFor(sentAt, sentAt.answered) < AWAY_QUIET_MS) return;
        throw new TypeError(`control uses: ${failure.message}, with no answer for ${Math.round(ms / 1000)} s`, { cause: failure });
      })
      // A request cut off as the page goes away is sent again from the next page; one that fails while the
      // page is open is an error.
      .catch((error) => { if (window.logError && document.visibilityState === "visible") window.logError("control uses", error); });
  }

  const picking = () => document.body.classList.contains("picking");

  // A key that presses a control (Enter on a focused one, a shortcut calling .click()) sends an untrusted click.
  let keyAt = -Infinity;
  const editing = (el) => el instanceof Element && el.closest("input, textarea, select, [contenteditable]");
  // Keys count by name; in a text field only a key held with Ctrl or Cmd is a shortcut (Ctrl+Enter sends).
  document.addEventListener("keydown", (event) => {
    if (!event.isTrusted || picking()) return;
    keyAt = performance.now();
    const held = event.ctrlKey || event.metaKey;
    if (event.altKey || (editing(event.target) && !held)) return;
    if (!(event.key.length === 1 && event.key !== " ") && !(held && event.key === "Enter")) return;
    const control = `key "${held ? "ctrl+" : ""}${event.key.toLowerCase()}"`;
    add(control, "press", 1);
    // A key's task is the one holding the focus, if any.
    pressed(control, document.activeElement instanceof Element ? document.activeElement : null);
  }, true);

  document.addEventListener("click", (event) => {
    const byKey = performance.now() - keyAt < 300;
    if (!(event.isTrusted || byKey) || picking() || !(event.target instanceof Element)) return;
    const el = event.target.closest(PRESSABLE);
    if (el) count(el, "press");
  }, true);

  // motion.js tells when a hover text shows and hides.
  let shown = null;
  document.addEventListener("cockpit:tip", (event) => {
    const { el, open } = event.detail;
    if (open) { shown = { el, at: performance.now() }; return; }
    if (shown && performance.now() - shown.at >= READ_MS && !picking()) count(shown.el, "hover");
    shown = null;
  });

  // Menus and dialogs come and go between the minute's scans, so a change to the page, or a menu that
  // opens, scans it soon after.
  let scanSoon = null;
  new MutationObserver(() => {
    if (!scanSoon) scanSoon = setTimeout(() => { scanSoon = null; noteSeen(); }, 500);
  }).observe(document.body, { childList: true, subtree: true, attributeFilter: ["open", "hidden"] });
  setInterval(noteSeen, SEEN_MS);
  setTimeout(noteSeen, 5e3);
  setInterval(send, SEND_MS);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") send(); });
})();
