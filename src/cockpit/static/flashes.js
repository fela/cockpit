"use strict";

// Cards that leave the page within a minute of showing (SPEC PERM-24). The user saw cards in Blocked and waiting, and
// steps, show and go again before they could read them. After each drawing, this reads which cards the page shows:
// each request, waiting session and Codex question in Blocked and waiting, and each step of the user's queue (Next up
// and the queue's rows, or the sidebar's queue group). A card is in view from when it is drawn while the page is in
// view and not folded away, or from when the page comes into view or its group opens; one that goes within a minute
// goes to Cockpit, which adds what took it away. Left out here: a card in Blocked and waiting the user pressed or
// typed in, and any card that went within a few seconds of a press elsewhere on the page ("Show fewer", folding a
// group, picking another item, switching a phone's screen), since the user took it away. Cockpit leaves out a step
// the user's own press took away.

(() => {
  const BRIEF_MS = 60e3;
  // A card gone this soon after a press went by that press.
  const PRESS_MS = 3e3;
  // On the page with the rail they are red rows in Inbox (BLOCKED_ROWS), and the one opened is the card on Next up.
  const BLOCKED = ":is(#blocked, #next) :is([data-request], .waiting-card[data-session], [data-codex])";
  const BLOCKED_ROWS = "#sidebar li.warn-row:is([data-request], [data-session], [data-codex])";
  // Cards in view, by key ("step:10"): which card it is, what it said, when it came into view (performance.now()),
  // and whether the user pressed in it since.
  let shown = new Map();
  let pressedAt = -Infinity;

  function blockedKey(el) {
    if (el.dataset.request) return `request:${el.dataset.request}`;
    if (el.dataset.codex) return `codex:${el.dataset.codex}`;
    return `session:${el.dataset.session}`;
  }

  function drawn() {
    const found = new Map();
    const add = (key, el, kind, ref) => {
      if (found.has(key) || el.closest(".motion-ghost") || (el.checkVisibility && !el.checkVisibility())) return;
      found.set(key, { kind, ref, label: el.textContent.replace(/\s+/g, " ").trim().slice(0, 300) });
    };
    // The open card first, so a request noted says what its card said rather than its row's few words.
    for (const el of [...document.querySelectorAll(BLOCKED), ...document.querySelectorAll(BLOCKED_ROWS)]) {
      const key = blockedKey(el);
      add(key, el, key.slice(0, key.indexOf(":")), key.slice(key.indexOf(":") + 1));
    }
    for (const el of document.querySelectorAll("#next [data-step], #queue > .list [data-step]")) add(`step:${el.dataset.step}`, el, "step", el.dataset.step);
    // A side-list row is one item's: when the item has two queued steps, the row shows either, so the row is the
    // card, noted as the step it showed last.
    for (const el of document.querySelectorAll("#sidebar .sidebar-group.queue li[data-step]")) add(`row:${el.dataset.item}`, el, "step", el.dataset.step);
    return found;
  }

  function send(card, goneAt) {
    // Both times from one reading of the server's clock, so a new reading cannot stretch or shrink the span.
    const gone = Date.now() + ui.clockOffset;
    const shownAt = gone - (goneAt - card.at);
    window.fetchThroughRestart("/api/flashes", {
      method: "POST",
      credentials: "same-origin",
      keepalive: true,
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ kind: card.kind, ref: card.ref, label: card.label, page: location.pathname,
        shown_at: new Date(shownAt).toISOString(), gone_at: new Date(gone).toISOString() }),
    }).then((response) => {
      if (!response.ok) console.warn(`Cockpit did not note a card that went at once: it answered ${response.status}`);
    }, (failure) => console.warn("Cockpit did not note a card that went at once:", failure));
  }

  function look() {
    // A hidden tab shows nothing, nor does a page that came back and stands "Updating…" in place of its cards (UI-60).
    if (document.visibilityState !== "visible" || document.body.classList.contains("catching-up")) { shown = new Map(); return; }
    const now = performance.now();
    const present = drawn();
    for (const [key, card] of shown) {
      if (present.has(key)) continue;
      shown.delete(key);
      if (now - card.at < BRIEF_MS && !card.pressed && now - pressedAt > PRESS_MS) send(card, now);
    }
    for (const [key, card] of present) {
      const known = shown.get(key);
      // A row that switched steps says what it shows now; any other card keeps the words it had when it came into view.
      if (known && known.ref !== card.ref) Object.assign(known, { ref: card.ref, label: card.label });
      else if (!known) shown.set(key, { ...card, at: now, pressed: false });
    }
  }

  document.addEventListener("cockpit:redraw", look);
  document.addEventListener("visibilitychange", look);
  document.addEventListener("cockpit:caught-up", look);
  // A group opening shows its cards; one folding hides them (`toggle` does not bubble).
  document.addEventListener("toggle", look, true);
  look();

  function press(event) {
    if (!event.target.closest) return;
    const card = event.target.closest(`${BLOCKED}, ${BLOCKED_ROWS}`);
    const entry = card && shown.get(blockedKey(card));
    if (entry) entry.pressed = true;
    // Typing in a field takes no other card away.
    if (event.type === "pointerdown" || !event.target.closest("input, textarea, [contenteditable]")) pressedAt = performance.now();
  }
  document.addEventListener("pointerdown", press, true);
  document.addEventListener("keydown", press, true);
  // Switching between a phone's list and item screens (sidebar.js) hides one screen's cards and shows the other's.
  document.addEventListener("cockpit:screen", () => { pressedAt = performance.now(); look(); });
})();
