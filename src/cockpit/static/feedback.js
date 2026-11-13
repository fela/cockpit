"use strict";

// Feedback mode on Cockpit's other tabs (SPEC FEEDBACK-6): Usage, Briefing, Pages, Spec and Skills. As on the Cockpit
// page (app.js, FEEDBACK-3 and FEEDBACK-4), the user presses the Feedback key or F, clicks any part of the page and
// says what should change. The feedback names the tab and the part (`page:/usage/spend-sessions/td`) and quotes
// what the part said, so the reader knows what the user meant after the page changes. This script adds the key, its
// entry in the phone menu, the bar and the box to the page; style.css styles them as on the Cockpit page. It is
// app.js's picker without what only that page has: the step timer the feedback holds and the Add box.
(() => {
  const { icon } = Icons;
  const TOUCH = window.matchMedia("(hover: none)");
  const TOAST_MS = 7000;
  // What a pick lands on: the nearest of these around the element under the pointer.
  const PICKABLE = "input, textarea, select, button, a, summary, h1, h2, h3, p, li, th, td, tr, table, section, article, details, blockquote, pre, iframe, "
    + "header, nav, main, aside, footer, .banner, .hint, .row-name, .row-meta, .spend-readout, .meter, .req, .seg, [data-time]";
  // The feedback key can be picked too (SPEC FEEDBACK-7): while picking, the bar's Stop, Escape and F stop.
  const CONTROLS = "#pick-bar, #pick-box, #feedback-menu";
  const PAGE = location.pathname.replace(/\/+$/, "") || "/";
  const DRAFT_KEY = `cockpit:feedback:${PAGE}`;
  // `guess`: the last pick's click, {x, y, parts, index}: the guesses at that spot and the one shown (pick.js).
  const ui = { picking: false, pick: null, guess: null, since: null, sending: false };
  let hovered = null;
  let toastTimer = 0;
  // Refused words whose part's box gave way to another part's: each waits with its own part, words and time, and
  // its box reopens once the open box is closed with no words left in it (`reopenWaiting`).
  const waiting = [];

  function h(tag, props, ...children) {
    const el = document.createElement(tag);
    for (const [name, value] of Object.entries(props || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (name === "class") el.className = value;
      else if (name === "text") el.textContent = value;
      else if (name.startsWith("on")) el.addEventListener(name.slice(2), value);
      else el.setAttribute(name, value === true ? "" : String(value));
    }
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return el;
  }

  // The draft outlives a reload; private windows and blocked storage keep it in this tab only.
  let draft = "";
  try { draft = localStorage.getItem(DRAFT_KEY) || ""; } catch { /* storage blocked */ }
  function saveDraft(text) {
    draft = text;
    try { if (text) localStorage.setItem(DRAFT_KEY, text); else localStorage.removeItem(DRAFT_KEY); } catch { /* storage blocked */ }
  }

  function clip(text, length) {
    return text.length > length ? `${text.slice(0, length - 1)}…` : text;
  }

  // The page's own toast when it has one (the Spec page), else one of this script's. `link`: {label, href}
  // for a link after the words, or null.
  function toast(text, fail, link) {
    let el = document.getElementById("toast");
    if (!el) {
      el = h("div", { id: "toast", class: "toast", role: "status", hidden: true });
      document.body.append(el);
    }
    el.replaceChildren(...[fail && icon("alert"), h("span", { text }), link && h("a", { href: link.href, text: link.label })].filter(Boolean));
    el.className = fail ? "toast fail" : "toast";
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, TOAST_MS);
  }

  // An action a closed or reloaded page left behind that Cockpit did not take after all (errors.js, CODE-5): the
  // toast says so, and words from this page's box go back into it.
  addEventListener("cockpit:not-sent", (event) => {
    const { words, message } = event.detail;
    if (words && words.store === DRAFT_KEY) {
      event.preventDefault();
      // A box open on another part, or closed with words kept for the next part, keeps its own words and part;
      // these wait for it to close empty. They name no part.
      if (ui.pick || draft.trim()) { wait(null, words.text, 0, `Not sent from the page you left: ${event.detail.reason}`); return; }
      putBack(words.text);
    }
    toast(message, true, null);
  });

  // Puts words that did not go at the top of the box, above what the user wrote since, also in an open box.
  function putBack(text) {
    const open = box.querySelector("textarea");
    const now = open ? open.value : draft;
    if (now.includes(text.trim())) return;
    saveDraft(now.trim() ? `${text.trim()}\n\n${now}` : text);
    if (open) open.value = draft;
  }

  // Signed out while sending: the words go back into the box before the sign-in, so they wait there after it.
  function keepForSignIn(sent) {
    putBack(sent);
    location.href = "/auth/google";
  }

  // Leaving the page (to sign in after any refused request, the host page's own included, a reload, a closed tab):
  // the stored box takes the waiting words ahead of its own, so they wait there on the next load. The page itself
  // keeps them waiting: come back from the browser's back-forward cache, it is the same page, and storage goes back
  // to what the page holds, unless another tab wrote the box since (`leftDraft`, what this page wrote).
  const boxWords = () => { const open = box.querySelector("textarea"); return open ? open.value : draft; };
  let leftDraft = null;
  addEventListener("pagehide", () => {
    if (!waiting.length) return;
    const words = [...waiting.map((each) => each.text.trim()), boxWords().trim()].filter(Boolean).join("\n\n");
    try { localStorage.setItem(DRAFT_KEY, words); leftDraft = words; } catch { /* storage blocked */ }
  });
  addEventListener("pageshow", (event) => {
    let unchanged = false;
    try { unchanged = leftDraft !== null && localStorage.getItem(DRAFT_KEY) === leftDraft; } catch { /* storage blocked */ }
    if (event.persisted && unchanged) saveDraft(boxWords());
    leftDraft = null;
  });

  // The hover mark draws a box around the part under the pointer, labelled with the place the feedback would name,
  // so the user sees what a click picks before clicking. The chosen part has its own mark.
  function markHover(el) {
    const part = el === ui.pick?.el ? null : el;
    if (part === hovered) return;
    if (hovered) hovered.classList.remove("pick-hover");
    hovered = part;
    if (part) part.classList.add("pick-hover");
    Pick.mark("hover", part, part ? pickPlace(part).replace(/^page:/, "") : null);
  }

  const key = h("button", {
    id: "feedback-mode", class: "button icon-only", type: "button", "data-key": "F",
    title: "Feedback", "aria-label": "Feedback",
    onclick: () => (ui.picking ? stopPicking() : startPicking()),
  }, icon("pick"), h("span", { class: "feedback-count", hidden: true }));
  // On a phone the header has no room for the key; the brand menu holds it instead (UI-20).
  const entry = h("button", {
    id: "feedback-menu", class: "phone-only", type: "button",
    onclick: (event) => { event.target.closest("details").open = false; startPicking(); },
  }, h("span", { class: "menu-label" }, icon("pick"), "Feedback on a part of the page",
    h("span", { class: "feedback-count", hidden: true })));
  const bar = h("div", { id: "pick-bar", class: "pick-bar", hidden: true });
  const box = h("div", { id: "pick-box", class: "pick-box", hidden: true });

  const header = document.querySelector("header.top .top-inner");
  let right = header.querySelector(".top-right");
  if (!right) {
    right = h("div", { class: "top-right" });
    header.append(right);
  }
  // The key stands in a tray as on the Cockpit page, where Update and the sweep's menu share it (UI-31).
  right.prepend(h("div", { class: "key-tray" }, key));
  document.querySelector(".brand-menu .menu-list").prepend(entry);
  document.body.append(bar, box);

  // The feedback no sweep has read yet, as a number on the key and its menu entry (SPEC FEEDBACK-9), as on the
  // Cockpit page: read when the page opens, each minute and on coming back to it while it shows, and after each
  // feedback sent from it. The read waits through a restart of Cockpit (CODE-5), so only a failure that outlasts one
  // reaches the error log. As a read may wait that long, one read goes at a time: a call meanwhile reads once more
  // after it, so an older answer never lands last.
  let reading = false;
  let readAgain = false;
  async function showCount() {
    if (reading) {
      readAgain = true;
      return;
    }
    reading = true;
    try {
      do {
        readAgain = false;
        await readCount();
      } while (readAgain);
    } finally {
      reading = false;
    }
  }

  async function readCount() {
    let count;
    try {
      const { response, data } = await readThroughRestart("/api/feedback", { credentials: "same-origin" });
      if (!response.ok) throw Object.assign(new Error(`Cockpit answered ${response.status}`), { status: response.status });
      count = data.length;
    } catch (error) {
      logError("reading the unread feedback", error);
      return;
    }
    key.classList.toggle("counted", count > 0);
    for (const el of document.querySelectorAll(".feedback-count")) {
      el.hidden = count === 0;
      el.textContent = count || "";
    }
    key.title = count > 0 ? `Feedback: ${count} not picked up yet` : "Feedback";
  }
  showCount();
  setInterval(() => { if (!document.hidden) showCount(); }, 60e3);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) showCount(); });

  function startPicking() {
    ui.picking = true;
    ui.since = ui.since || Date.now();
    document.body.classList.add("picking");
    key.classList.add("on");
    renderBar();
    bar.hidden = false;
  }

  // Once a part is picked, the bar says that a click on the same spot tries the next guess there.
  function renderBar() {
    const tap = TOUCH.matches ? "Tap" : "Click";
    bar.replaceChildren(
      icon("pick"),
      h("span", { text: ui.pick ? `${tap} it again for another guess` : `${tap} the part your feedback is about` }),
      h("button", {
        class: "button small ghost whole-page", type: "button",
        onclick: () => choosePick(document.body, document.body, null),
      }, icon("wider"), h("span", { class: "label", text: "Whole page" })),
      h("button", { class: "button small ghost", type: "button", onclick: stopPicking }, TOUCH.matches ? "Stop" : "Stop (Esc)"));
  }

  function stopPicking() {
    ui.picking = false;
    document.body.classList.remove("picking");
    key.classList.remove("on");
    bar.hidden = true;
    markHover(null);
    // Leaving feedback mode without a part ends the feedback's time.
    if (!ui.pick) ui.since = null;
  }

  // Where the part is: the tab, the nearest named area around it, and the part's kind.
  function pickPlace(el) {
    if (el === document.body) return `page:${PAGE}/whole page`;
    const area = el.closest("[id]");
    // A part of a page framed in this one is named by that page.
    const parts = [Pick.framedPath(el) || PAGE, area ? area.id : "page"];
    const kind = [...el.classList].find((name) => !name.startsWith("pick-")) || el.tagName.toLowerCase();
    if (el !== area) parts.push(kind);
    return `page:${parts.join("/")}`;
  }

  // A pick: its place, the words it shows, and `detail`, how a session finds the part again (pick.js).
  function pickTarget(el) {
    const detail = Pick.describe(el);
    if (el === document.body) return { el, target: pickPlace(el), quote: "", words: "", detail };
    const text = Pick.words(el).replace(/\s+/g, " ").trim();
    // `words`: the whole passage, for a writing example (the server takes up to 20,000 characters).
    return { el, target: pickPlace(el), quote: clip(text, 2000), words: clip(text, 20000), detail };
  }

  // `from`: the part the user clicked, or last went Sideways to. `side`: the parts Sideways goes through, or null
  // (pick.js step).
  // Feedback mode stays on until the user writes, so a click on the same spot tries the next guess there.
  function choosePick(el, from, side) {
    if (ui.pick) ui.pick.el.classList.remove("pick-chosen");
    ui.pick = { ...pickTarget(el), from, side };
    el.classList.add("pick-chosen");
    markHover(null);
    if (ui.picking) renderBar();
    renderPick();
    box.querySelector("textarea").focus();
  }

  // A step (pick.js step) works while the page still shows the part; the page redraws its numbers every minute.
  // The part comes into view when the step went to one mostly out of it.
  function stepPick(next) {
    if (next && Pick.alive(next.part)) { Pick.reveal(next.part); choosePick(next.part, next.from, next.side); return; }
    toast("The page redrew since you picked; pick the part again", true, null);
    startPicking();
  }

  // The draft stays when the box closes, so a comment survives picking another part; its time ends.
  function closePick() {
    if (ui.pick) ui.pick.el.classList.remove("pick-chosen");
    ui.pick = null;
    ui.since = null;
    renderPick();
    reopenWaiting();
  }

  // Opens the box on the part `pick` with the words that did not go, and the time spent on them. With no part
  // (words from a page the user left), the words only go back into the closed box.
  function reopen(pick, text, seconds) {
    if (text.trim()) putBack(text);
    if (!pick) return;
    ui.since = Date.now() - seconds * 1000;
    if (pick.el.isConnected) pick.el.classList.add("pick-chosen");
    ui.pick = pick;
    renderPick();
  }

  // With the box closed and empty, the oldest refused feedback that waits gets its box back on its own part.
  function reopenWaiting() {
    if (ui.pick || draft.trim() || !waiting.length) return;
    const { pick, text, seconds, notice } = waiting.shift();
    reopen(pick, text, seconds);
    toast(`${notice}; it is back in its box`, true, null);
  }

  // A refusal while another part's box is open: that box keeps its own words and part, and these wait for it to
  // close. `notice` says what was not saved.
  function wait(pick, text, seconds, notice) {
    waiting.push({ pick, text, seconds, notice });
    toast(`${notice}. Your words wait, and come back on their part once this box is sent or closed empty`, true, null);
  }

  // The box closes at once; if the server refuses, it opens again with the words and the part.
  async function sendPick() {
    const pick = ui.pick;
    const text = draft.trim();
    if (!text) { toast("Write the feedback first", true, null); return; }
    if (ui.sending) return;
    ui.sending = true;
    const seconds = Math.round((Date.now() - (ui.since || Date.now())) / 1000);
    const sent = draft;
    saveDraft("");
    toast("Feedback saved; a sweep reads it within minutes", false, null);
    closePick();
    try {
      const { response, data } = await sendAction("/api/feedback", {
        method: "POST", credentials: "same-origin", headers: { "content-type": "application/json" },
        body: JSON.stringify({ text, seconds, target: pick.target, quote: pick.quote, detail: pick.detail, step_id: null, item_id: null }),
      }, TRY_MS, { store: DRAFT_KEY, key: null, text: sent });
      if (response.status === 401) { keepForSignIn(sent); return; }
      if (!response.ok) throw Object.assign(new Error(data.detail || `Cockpit answered ${response.status}`), { status: response.status });
      showCount();
    } catch (error) {
      logError("saving feedback", error);
      const notice = `Feedback not saved: ${error.message}`;
      if (ui.pick || draft.trim()) { wait(pick, sent, seconds, notice); return; }
      reopen(pick, sent, seconds);
      toast(notice, true, null);
    } finally {
      ui.sending = false;
    }
  }

  // Good writing and Hard to understand (SPEC FEEDBACK-8): the picked part is kept as an example of writing to
  // reproduce (`verdict` good) or to avoid (bad), with the words the user saw and the box's words, if any, as the
  // note on why. The box closes at once; if the server refuses, it opens again with the words and the part.
  async function markWriting(verdict) {
    const name = verdict === "good" ? "good writing" : "hard to understand";
    const pick = ui.pick;
    if (ui.sending) return;
    ui.sending = true;
    const sent = draft;
    saveDraft("");
    closePick();
    if (ui.picking) stopPicking();
    try {
      const { response, data } = await sendAction("/api/writing", {
        method: "POST", credentials: "same-origin", headers: { "content-type": "application/json" },
        body: JSON.stringify({
          verdict, kind: "part", target: pick.target, quote: pick.words, note: sent.trim(),
          step_id: null, item_id: null, page_id: null, session_id: null,
        }),
      }, TRY_MS, sent.trim() ? { store: DRAFT_KEY, key: null, text: sent } : null);
      if (response.status === 401) { keepForSignIn(sent); return; }
      if (!response.ok) throw Object.assign(new Error(data.detail || `Cockpit answered ${response.status}`), { status: response.status });
      toast(`Saved as ${name}`, false, { label: "See all", href: "/writing" });
    } catch (error) {
      logError(`saving ${name}`, error);
      const notice = `Not saved as ${name}: ${error.message}`;
      if (ui.pick || draft.trim()) { wait(pick, sent, 0, notice); return; }
      reopen(pick, sent, 0);
      toast(notice, true, null);
    } finally {
      ui.sending = false;
    }
  }

  function renderPick() {
    const pick = ui.pick;
    if (!pick) {
      box.hidden = true;
      box.replaceChildren();
      Pick.mark("chosen", null, null);
      Pick.dock(box, null, 0);
      return;
    }
    const area = h("textarea", { rows: 5, placeholder: "What should change here?" });
    area.value = draft;
    area.addEventListener("input", () => {
      saveDraft(area.value);
      if (ui.picking) stopPicking();
    });
    area.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); sendPick(); }
    });
    const whole = pick.el === document.body;
    const step = (name, title) => h("button", {
      class: "button ghost", type: "button", title, disabled: !Pick.step(name, pick, PICKABLE, CONTROLS),
      onclick: () => stepPick(Pick.step(name, ui.pick, PICKABLE, CONTROLS)),
    }, icon(name), h("span", { class: "label", text: title }));
    const narrower = step("narrower", "Narrower");
    box.replaceChildren(...[
      h("div", { class: "pick-head" }, h("span", { class: "kind-chip note" }, icon("feedback"), h("span", { text: "Feedback" })),
        h("strong", { text: "on" }), h("span", { class: "meta", text: pick.target.replace(/^page:/, ""), title: pickTitle(pick) }),
        h("button", { class: "button ghost small close icon-only", type: "button", title: "Close", "aria-label": "Close", onclick: closePick }, icon("x"))),
      preview(pick),
      area,
      h("div", { class: "actions" },
        whole ? !narrower.disabled && narrower : [step("wider", "Wider"), narrower, step("sideways", "Sideways")],
        h("button", { class: "button ghost", type: "button", onclick: startPicking },
          icon("pick"), h("span", { class: "label", text: "Pick again" })),
        h("button", { class: "button primary send", type: "button", onclick: sendPick }, icon("send"), "Send")),
      !whole && h("div", { class: "pick-writing" },
        h("span", { class: "meta", text: "Keep as an example of" }),
        h("button", { class: "button small ghost", type: "button", onclick: () => markWriting("good"),
          title: "Good writing" },
        icon("writing-good"), "Good writing"),
        h("button", { class: "button small ghost", type: "button", onclick: () => markWriting("bad"),
          title: "Keep as an example of writing to avoid" },
        icon("writing-bad"), "Hard to understand")),
    ].filter(Boolean));
    box.hidden = false;
    // The chosen part keeps its mark while the user writes, and the box stands beside it, the page not darkened.
    Pick.mark("chosen", pick.el, pick.target.replace(/^page:/, ""));
    Pick.dock(box, pick.el, Math.max(16, (bar.hidden ? document.querySelector("header.top") : bar).getBoundingClientRect().bottom + 8));
  }

  // What a hover over the pick's name or copy shows: everything the feedback sends to find the part.
  function pickTitle(pick) {
    return `${pick.target}\n${pick.detail}`;
  }

  // The picked part as the user sees it, scaled into the box where it fits (pick.js), with its words under a
  // copy too small to read; else its words.
  function preview(pick) {
    const copy = Pick.copy(pick.el, Math.min(688, window.innerWidth - 64), 180);
    if (copy) {
      // Set through the style object: the page's content policy drops style attributes.
      const frame = h("div", { class: "pick-preview", title: pickTitle(pick) }, copy);
      frame.style.setProperty("--ground", Pick.ground(pick.el));
      if (!copy.classList.contains("small") || !pick.quote) return frame;
      return h("div", { class: "pick-shown" }, frame, h("blockquote", { class: "pick-quote", text: clip(pick.quote, 300) }));
    }
    return pick.quote && h("blockquote", { class: "pick-quote", text: clip(pick.quote, 300) });
  }

  // F starts and stops picking, Escape stops it or closes the box. These run before the page's own keys,
  // so the Spec page's Escape (which closes its editors) waits while feedback mode has it.
  window.addEventListener("keydown", (event) => {
    if (event.defaultPrevented || event.isComposing || document.querySelector("dialog[open]")) return;
    if (event.metaKey || event.ctrlKey || event.altKey || event.repeat) return;
    const typing = Boolean(event.target.closest && event.target.closest("input, textarea, select, [contenteditable]"));
    // Escape in one of the page's own boxes is the page's; in the feedback box or outside any box it is this one's.
    if (event.key === "Escape" && (ui.picking || (ui.pick && (box.contains(event.target) || !typing)))) {
      event.preventDefault();
      event.stopPropagation();
      if (ui.picking) stopPicking(); else closePick();
      return;
    }
    if (typing) return;
    if (event.key === "f") {
      event.preventDefault();
      if (ui.picking) stopPicking(); else startPicking();
    }
  }, true);

  // While picking, a click picks the part instead of acting on it: it runs first (capture) and stops the
  // click before any button, link or row sees it. The picker's own controls work as usual. A click within
  // 8 px of the last one takes the next of that spot's guesses (pick.js); one elsewhere starts over. A click
  // from the keyboard has no spot: it picks the focused control, and again on the same one takes the next guess.
  // A click on the page that finds no part still acts on nothing.
  document.addEventListener("click", (event) => {
    if (!ui.picking || event.target.closest(CONTROLS)) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    const keyboard = event.detail === 0;
    const first = keyboard ? Pick.of(event.target, PICKABLE) : Pick.at(event.clientX, event.clientY, PICKABLE, CONTROLS);
    if (!first) return;
    const last = ui.guess;
    const same = last && (keyboard ? last.target === event.target : !last.target && Math.hypot(event.clientX - last.x, event.clientY - last.y) <= 8);
    const again = same && last.parts.every((part) => part.isConnected);
    if (again) last.index = (last.index + 1) % last.parts.length;
    else ui.guess = { x: event.clientX, y: event.clientY, target: keyboard ? event.target : null, parts: Pick.guesses(first, PICKABLE, CONTROLS), index: 0 };
    const el = ui.guess.parts[ui.guess.index];
    choosePick(el, el, null);
  }, true);

  // The mark follows the pointer once a frame: a disabled control lets the pointer through, so moving onto
  // one brings no pointerover. It listens on the window before the page's hold, which keeps pointer moves from
  // the page.
  let hoverFrame = 0;
  addEventListener("pointermove", (event) => {
    if (!ui.picking || event.pointerType === "touch") return;
    cancelAnimationFrame(hoverFrame);
    hoverFrame = requestAnimationFrame(() => { if (ui.picking) markHover(Pick.at(event.clientX, event.clientY, PICKABLE, CONTROLS)); });
  }, { capture: true, passive: true });

  // While picking, a press or the pointer's arrival on the page reaches none of its own handlers; the click
  // that follows picks (pick.js).
  Pick.holdPage(() => ui.picking, () => CONTROLS);
})();
