"use strict";

// The item list sits beside the step card (SPEC VIEW-26). Selecting an item shows its step or work row;
// selecting a session shows its conversation. These functions share app.js globals: the file loads before app.js and
// uses them only at call time or after DOMContentLoaded.

const SIDEBAR_SELECTED_KEY = "cockpit.sidebar.item";
const SESSION_LOG_MS = 4000;
const SESSION_LOG_IDLE_MS = 15000;

const sidebar = { restored: false };

// The rail holds the brand menu, Reload while a newer Cockpit runs, Add, Feedback, priorities, Search, pinned work
// and item list. Its foot shows account usage, connection status and paused sweeps with a Restart key. Existing element ids
// keep their behavior when moved here, and the hidden header keeps page navigation state.
function buildRail() {
  const byId = (id) => document.getElementById(id);
  const div = (cls, ...parts) => {
    const el = document.createElement("div");
    el.className = cls;
    el.append(...parts.filter(Boolean));
    return el;
  };
  const brand = document.querySelector(".brand-menu");
  // Sessions joins the pages the menu lists on a phone, which the rail's menu lists at every width.
  const sessions = document.createElement("a");
  sessions.href = "/classic#sessions";
  const label = document.createElement("span");
  label.className = "menu-label";
  label.append(Icons.icon("terminal"), "Sessions");
  sessions.append(label);
  brand.querySelector(".menu-list").prepend(sessions);
  // Reload while a newer Cockpit runs, then Feedback, then Add. Reload sits here so the foot's usage keeps one line
  // (UI-13). Feedback and Add both make a tracked item, Feedback with a part of the page picked first, and Feedback is
  // styled as the +. A session on no item starts from Search; while its message box is open, an x beside them closes
  // it (renderSidebar).
  const add = document.createElement("button");
  add.className = "button small ghost icon-only sidebar-add";
  add.type = "button";
  add.setAttribute("aria-label", "Add to your work");
  add.setAttribute("aria-keyshortcuts", "C");
  add.title = "Add a to-do; a session starts on it";
  add.append(Icons.icon("plus"));
  add.addEventListener("click", () => (ui.adding ? closeAddBox() : openAddBox()));
  const feedback = byId("feedback-mode");
  feedback.classList.add("small", "ghost");
  const resume = byId("resume-sweeps");
  resume.classList.add("small");
  resume.querySelector(".label").textContent = "Restart";
  const pausedWords = document.createElement("span");
  pausedWords.className = "sweeps-paused-words";
  pausedWords.textContent = "Sweeps paused";
  const paused = div("sweeps-paused", Icons.icon("pause"), pausedWords, resume);
  paused.id = "sweeps-paused";
  paused.setAttribute("role", "status");
  paused.hidden = true;
  const rail = document.createElement("aside");
  rail.className = "rail";
  rail.setAttribute("aria-label", "Menu, priorities and your items");
  rail.append(
    div("rail-top", brand, div("rail-actions", byId("reload"), feedback, add)),
    byId("goals"),
    div("rail-search", document.querySelector(".command-trigger")),
    byId("pinned"), byId("sidebar"),
    div("rail-foot", paused, div("rail-line", byId("usage"), div("rail-keys", byId("live"), byId("unsent")))));
  document.querySelector("main.layout").prepend(rail);
  // The notices above the foot stand on its height, which grows when its line wraps.
  const foot = rail.querySelector(".rail-foot");
  new ResizeObserver(() => document.body.style.setProperty("--rail-foot-h", `${foot.offsetHeight}px`)).observe(foot);
  // Warnings head the card's column, so the rail keeps the top of the page.
  document.querySelector(".layout > .primary").prepend(byId("banner"));
  // The header's Add stays hidden in the header: the rail's + opens the same box.
  byId("add").hidden = true;
  byId("reload").setAttribute("aria-label", "Reload");
}
if (document.body.dataset.layout === "sidebar") buildRail();

// The item the user picked in the sidebar, kept for this tab across a reload; null shows the first step in the queue.
function selectedItem() {
  if (!sidebar.restored) {
    sidebar.restored = true;
    try { ui.selItem = JSON.parse(sessionStorage.getItem(SIDEBAR_SELECTED_KEY)); } catch { ui.selItem = null; }
  }
  return ui.selItem ?? null;
}

// Picking an item closes a warning the user opened.
function keepSelected(itemId) {
  ui.selItem = itemId;
  ui.warningOpen = null;
  try { sessionStorage.setItem(SIDEBAR_SELECTED_KEY, JSON.stringify(itemId)); } catch { /* blocked storage: this tab keeps it */ }
}

// On a phone the list of items and the item it shows are two screens. The page opens on the item screen, which shows Next up; the bar at its top goes to
// the list, and picking a row there comes back to the item screen. Each screen is a step in the browser's history, so
// the phone's own back gesture goes from an item to the list. `body.phone-list` marks the list screen, which
// sidebar.css draws only at phone width, so a window widened past it shows both again.
const ONE_COLUMN = matchMedia("(max-width: 1000px)");
const PHONE_SCREEN_KEY = "cockpit.phone.screen";
const onPhone = () => document.body.dataset.layout === "sidebar" && ONE_COLUMN.matches;

// The screen last asked for, true for the list; null until the first ask, when the page's class says which shows. A
// slide changes the screen a frame or more after the ask, so a second ask within that time compares with this and
// the first one's change, superseded, does nothing. An ask without a slide, whose caller focuses
// something on the screen at once, shows it at once, also while a slide to it is on its way.
let phoneScreenAsked = null;
const phoneListShown = () => document.body.classList.contains("phone-list");
const phoneListAsked = () => phoneScreenAsked ?? phoneListShown();

// Shows one screen; `slide` slides it in, and is false where the caller slides the page itself.
function setPhoneScreen(list, slide) {
  if (phoneListAsked() === list && (slide || phoneListShown() === list)) return;
  phoneScreenAsked = list;
  const change = () => {
    if (phoneScreenAsked !== list || phoneListShown() === list) return;
    document.body.classList.toggle("phone-list", list);
    window.scrollTo({ top: 0 });
    if (list) pauseHiddenTimer();
    // A command drawn on the card while the list hid it measured nothing; it is measured again now it shows. A step
    // the list left starts again now its card shows (leaveTimedStep).
    else { ui.leftStepId = null; markClippedCalls(); }
    // The cards of the screen left go by the user's own move, which flashes.js does not report.
    document.dispatchEvent(new Event("cockpit:screen"));
  };
  try { sessionStorage.setItem(PHONE_SCREEN_KEY, list ? "list" : "item"); } catch { /* blocked storage: this tab keeps it */ }
  if (slide && onPhone()) slideView(list ? "back" : "in", change);
  else change();
}

// The list hides the item's card, so a timer on it stops as showing another item does (TRACK-8): when the list
// shows, also by the browser's back gesture, which presses nothing on the page, and when narrowing the
// window to phone width hides the card under a list shown before. A step that waits for the feedback being written
// stays paused when the feedback ends.
function pauseHiddenTimer() {
  if (!onPhone() || !document.body.classList.contains("phone-list")) return;
  leaveFeedbackHoldStep(null);
  const timer = ui.state && drawn(ui.state).timer;
  if (timer) leaveTimedStep(timer);
}

// From the item screen to the list: back through history when the list screen is the entry before, so the phone's
// forward gesture and the bar's key agree; otherwise the list takes the item screen's entry.
function showPhoneList() {
  if (history.state?.cockpitScreen === "item") { history.back(); return; }
  history.replaceState({ ...history.state, cockpitScreen: "list" }, "");
  setPhoneScreen(true, true);
}

// From the list to the item screen: a new history entry, which the back gesture leaves. One already asked for and
// on its way keeps its entry.
function showPhoneItem(slide) {
  if (phoneListAsked()) {
    history.replaceState({ ...history.state, cockpitScreen: "list" }, "");
    history.pushState({ ...history.state, cockpitScreen: "item" }, "");
  } else if (!phoneListShown()) return;
  setPhoneScreen(false, slide);
}

// A page reloaded on the list, or brought back to it, stays there: an item or search link in the address, which the
// page opens again, picks the item without leaving the list, until the user's next press or key.
let phoneRestoring = false;
// When the user last pressed with a pointer and last pressed a key. Only a press slides the item screen in: a key
// goes on to focus what it opened, which a slide would show a moment later.
const phonePress = { pointer: -Infinity, key: -Infinity };

if (document.body.dataset.layout === "sidebar") {
  let saved = null;
  try { saved = sessionStorage.getItem(PHONE_SCREEN_KEY); } catch { /* blocked storage: the item screen */ }
  if ((history.state?.cockpitScreen || saved) === "list") {
    document.body.classList.add("phone-list");
    phoneRestoring = true;
  } else if (!history.state?.cockpitScreen && ONE_COLUMN.matches) {
    // A page opened on the item screen puts the list's entry before it, so the back gesture shows the list.
    history.replaceState({ ...history.state, cockpitScreen: "list" }, "");
    history.pushState({ ...history.state, cockpitScreen: "item" }, "");
  }
  window.addEventListener("popstate", (event) => {
    const screen = event.state?.cockpitScreen;
    if (!screen) return;
    // Back to the list may change the address, which the page then opens again; that keeps the list too.
    phoneRestoring = screen === "list";
    setPhoneScreen(screen === "list", true);
  });
  // On the window, which hears a press before keyboard.js, on the document, takes the key for itself.
  window.addEventListener("pointerdown", () => { phoneRestoring = false; phonePress.pointer = performance.now(); }, true);
  window.addEventListener("keydown", () => { phoneRestoring = false; phonePress.key = performance.now(); }, true);
  // Narrowing the window to phone width hides one screen's cards, and widening it shows them, by the user's move.
  ONE_COLUMN.addEventListener("change", () => {
    // Widening shows the card again, so a step the list left starts again (leaveTimedStep).
    if (!onPhone()) ui.leftStepId = null;
    pauseHiddenTimer();
    document.dispatchEvent(new Event("cockpit:screen"));
  });
}

// Shows the item screen for what the user opened in the list. `focuses` is true where the caller then focuses
// something on that screen, which must show at once.
function phoneOpen(focuses) {
  if (!onPhone() || phoneRestoring) return;
  showPhoneItem(!focuses && phonePress.pointer > phonePress.key && performance.now() - phonePress.pointer < 2000);
}

// The bar at the top of the item screen: the list's key, which counts the steps waiting for the user, Reload while a
// newer Cockpit runs (UI-13), and Add (CONTROL-8). Hidden above phone width and on the list screen. It is built once
// app.js, whose helpers it uses, has loaded.
// The list's key stands in for the list's folded headings (UI-24): while an Add's, a close's or a hand-off's Undo is
// on offer, it lights up as Busy's or Finished this week's heading does, in the close's plum for a close, keeps its
// name and count, and the Undo with the bar that runs down over its time sits at its right (`phoneHeld`).
function buildPhoneBar() {
  const count = h("span", { class: "phone-count" });
  const back = h("button", { class: "button ghost phone-back", type: "button", "aria-label": "Your items", title: "Your items", onclick: showPhoneList },
    icon("back"), h("span", { class: "label", text: "Your items" }), count);
  const items = h("div", { class: "phone-items" }, back);
  const reload = h("button", { class: "button small accent icon-only phone-reload", type: "button", hidden: true, title: "A newer Cockpit is running; reloading keeps your drafts",
    onclick: () => document.getElementById("reload").click() }, icon("retry"), h("span", { class: "label", text: "Reload" }));
  const add = h("button", { class: "button small ghost icon-only phone-add", type: "button", "aria-label": "Add to your work",
    title: "Add a to-do; a session starts on it", onclick: () => (ui.adding ? closeAddBox() : openAddBox()) }, icon("plus"));
  const bar = h("div", { class: "phone-bar" }, items, reload, add);
  document.querySelector(".layout > .primary").prepend(bar);
  // The hold shown, so a redraw keeps its Undo, and the focus on it, while the same hold lasts.
  let shown = null;
  document.addEventListener("cockpit:redraw", () => {
    // The state as the page draws it, with pending changes such as a held Add's row (app.js `drawn`).
    const state = ui.state && drawn(ui.state);
    const waiting = state ? state.queue.length : 0;
    count.textContent = waiting ? String(waiting) : "";
    count.hidden = !waiting;
    back.setAttribute("aria-label", waiting ? `Your items, ${plural(waiting, "step", "steps")} in your queue` : "Your items");
    const held = state ? phoneHeld(state) : null;
    if (held?.hold === shown?.hold) return;
    shown = held;
    items.querySelector(":scope > .busy-undo")?.remove();
    items.classList.toggle("holding", Boolean(held));
    items.classList.toggle("close", Boolean(held?.close));
    if (!held) return;
    items.style.setProperty("--since", `-${Date.now() - held.at}ms`);
    items.append(heldUndo(held));
  });
  // Reload shows when the header's does, which the page sets after its redraw.
  const original = document.getElementById("reload");
  const follow = () => { reload.hidden = original.hidden; };
  new MutationObserver(follow).observe(original, { attributes: true, attributeFilter: ["hidden"] });
  follow();
}
if (document.body.dataset.layout === "sidebar") document.addEventListener("DOMContentLoaded", buildPhoneBar);

function selectItem(itemId) {
  showOnCard(itemId, null);
}

// A warning's row, or the red change-order arrows, open the warning on the card (SPEC UI-52).
function openWarning(key) {
  showOnCard(null, key);
}

function showOnCard(itemId, warningKey) {
  ui.navigations++;
  // Showing another item takes the timer along, however the user got there (a row, Search, the keyboard): the step
  // left stops without a notice and starts again when it shows once more (TRACK-8, leaveTimedStep). Each showing
  // ends the hold on the step left before, so coming back to it starts it, however quickly.
  ui.leftStepId = null;
  const timer = ui.state && drawn(ui.state).timer;
  if (timer && timer.item_id !== itemId) leaveTimedStep(timer);
  leaveFeedbackHoldStep(itemId);
  phoneOpen(false);
  closeSessionView(false);
  closeSettings(false);
  keepSelected(itemId);
  ui.warningOpen = warningKey;
  // Opening a queued step holds its place, as opening it from search does (NEXT-16).
  const step = ui.state && ui.state.queue.find((one) => one.item_id === itemId);
  if (step) holdPlace(step.id);
  render(false);
  window.scrollTo({ top: 0 });
}

// Shows an item that search, the pinned list or the keyboard asks for: picks it, opens its group, and returns its
// line in the sidebar; null when the page does not list it.
function showInSidebar(itemId) {
  if (!ui.state || !(ui.state.items.some((item) => item.id === itemId) || closedEntry(ui.state, itemId))) return null;
  selectItem(itemId);
  const row = document.querySelector(`#sidebar li[data-item="${itemId}"]`);
  for (let group = row?.closest("details"); group; group = group.parentElement.closest("details")) group.open = true;
  row?.scrollIntoView({ block: "nearest" });
  return row;
}

// A task closed as not needed this week, which Finished this week lists beside the done ones.
function closedEntry(state, itemId) {
  return state.closed_recently.items.find((entry) => entry.action === "archived" && entry.id === itemId) || null;
}

// After the user answers, hands off, snoozes or closes the step they picked, the card shows the queue's first step
// again, as it does without the sidebar.
function followQueue(step) {
  if (!SIDEBAR || selectedItem() !== step.item_id) return;
  keepSelected(null);
}

// Resorted and Take it show the queue's first step on the card, whichever item the user picked in the list. The card
// then follows the queue again, as after the user answers a step (followQueue). A step kept as "Left your queue" at the
// top of that order moves below the first live step, keeping its draft, so it does not take the card. Both keys sit on
// the card, so the card is already on screen.
function showQueueTop() {
  if (!SIDEBAR) return;
  ui.navigations++;
  keepSelected(null);
  for (const place of ui.left.values()) if (place.list === "queue") place.index = Math.max(place.index, 1);
}

// The step of an item on the card: its step in the queue, Ready next or Snoozed, in that order.
function stepOfItem(itemId, lists) {
  for (const [group, steps] of [["next", lists.queue], ["ready", lists.ready], ["snoozed", lists.snoozed]]) {
    const step = steps.find((one) => one.item_id === itemId && !one.left);
    if (step) return { step, group };
  }
  return null;
}

// The card: the warning the user opened; else the picked item's step; else the step the user handed to an agent, as
// it was, ending in what the agent is doing (`workingNote`); else the item's open row; with nothing picked, the queue's first step, as Next up shows it without the sidebar. Warnings rank above
// every step but the one the user is on (SPEC UI-52): with nothing picked, the first warning takes Next up unless
// the user opened, pressed or took the queue's first step, or its time runs (NEXT-60).
function renderSelected(state, lists) {
  let itemId = selectedItem();
  const items = new Map(state.items.map((item) => [item.id, item]));
  const found = itemId !== null ? stepOfItem(itemId, lists) : null;
  const closed = itemId !== null && !found && !items.has(itemId) ? closedEntry(state, itemId) : null;
  if (itemId !== null && !found && !items.has(itemId) && !closed) { keepSelected(null); itemId = null; }
  const warnings = warningList(state);
  // A warning that went, fixed or closed, leaves the card to the next warning, as warnings rank above every step.
  // A snoozed warning shows when the user opened it from Waiting (SPEC UI-72).
  const opened = ui.warningOpen === null ? null
    : warnings.find((warning) => warning.key === ui.warningOpen)
      || snoozedWarnings(state).find((warning) => warning.key === ui.warningOpen) || warnings[0] || null;
  ui.warningOpen = opened ? opened.key : null;
  ui.shownWarning = null;
  const first = lists.queue[0];
  const onFirst = Boolean(first) && (first.left || ui.placed.has(first.id) || taken(first) || expiring(first));
  const warning = opened || (itemId === null && !onFirst ? warnings[0] : null);
  if (warning) {
    ui.shownItem = null;
    ui.shownWarning = warning.key;
    ui.next = null;
    fill("next", warningCard(warning));
    return;
  }
  if (itemId === null) {
    ui.shownItem = first ? first.item_id : null;
    renderNext(first, state);
    return;
  }
  ui.shownItem = itemId;
  if (found && found.group !== "snoozed") { renderNext(found.step, state); return; }
  ui.next = null;
  if (found) {
    fill("next", h("article", { class: "card next-card", "data-step": found.step.id, "data-parts": true }, stepBody(found.step, state, "snoozed")));
    return;
  }
  if (closed) {
    fill("next", h("article", { class: "card next-card item-card", "data-item": itemId }, closedItemRow(closed, state)));
    return;
  }
  const item = items.get(itemId);
  const handed = state.with_agents.find((step) => step.item_id === itemId);
  if (handed) {
    fill("next", h("article", { class: "card next-card working-card", "data-step": handed.id, "data-parts": true, "data-drop-item": item.todo ? itemId : null },
      stepBody(handed, state, "working")));
    return;
  }
  fill("next", h("article", { class: "card next-card item-card", "data-item": itemId, "data-drop-item": item.todo ? itemId : null },
    itemCard(item, state)));
}

// An item with no step for the user, laid out as a step on the card: its context line, its title where a step has its
// ask, where it stands where a step has its why, then its history, links and sessions, its controls, and the message
// box last, which writes to the item's first session as a step's reply box writes to the step's.
function itemCard(item, state) {
  const sessions = sessionMap(state);
  const linked = itemSessions(item).map((sessionId) => [sessionId, sessions.get(sessionId)]);
  const primary = linked[0];
  // On an item the user added, the summary is the user's own words and the session's latest status says where it
  // stands (VIEW-16); on other items the summary says where it stands.
  const standsField = item.agent_summary ? "agent_summary" : item.summary !== item.title ? "summary" : null;
  const stands = standsField && item[standsField] ? summaryWords(item, standsField) : null;
  const own = item.agent_summary && item.summary && item.summary !== item.title ? summaryWords(item, "summary") : null;
  const ended = item.ball === "done" || (item.stage === "done" && item.ended) ? endedHow(item, state) : null;
  return [
    h("div", { class: "step-context" },
      item.project && h("span", { class: "item-title item-project-name", title: item.key },
        item.project !== "cockpit" && Icons.projectMark(item.project), item.project),
      // Waiting names go with the tags, which wrap, so the age stays in view on a phone.
      h("span", { class: "context-tags" }, cockpitChip(item.project), stalledChip(item), fireAndForgetChip(item), blocksChip(item),
        item.stage === "review" ? [] : waitingChips(item)),
      h("span", { class: "context-marks" }, itemStageChip(item, sessions),
        h("span", { class: "row-age", "data-time": item.updated_at, title: `Updated by ${actorWords(item.updated_by)}`, text: age(item.updated_at) }))),
    h("h1", { class: "ask" }, h("span", { class: "ask-text", text: item.title })),
    ended ? h("p", { class: "why" }, endedLine(ended)) : stands && h("p", { class: "why" }, stands),
    own && h("p", { class: "item-note" }, own),
    thumbs(shownImages(item), "small"),
    historyBlock(item, state, null, false),
    linkList(`item:${item.id}`, itemLinks(item, null), ""),
    sessionList(linked, item, null, state),
    h("div", { class: "actions item-actions" }, itemControls(item, state, `${item.id}`), h("button", {
      class: "button close-item", type: "button",
      onclick: (event) => closeItemRow(item, event.currentTarget),
    }, icon("archive"), h("span", { class: "label", text: "Close" }))),
    askBlock(item.id, null, state),
    composer(`msg:item:${item.id}`, {
      session: primary ? {
        session_id: primary[0],
        name: primary[1] ? primary[1].name : "",
        state: primary[1] ? primary[1].state : "unknown",
        alive: primary[1] ? primary[1].alive : false,
        working_now: primary[1] ? primary[1].working_now : false,
      } : null,
      context: { item_id: item.id },
      project: item.project,
      step: null,
      handOff: false,
      toSession: true,
    }),
  ];
}

// The end of a handed step's card (`stepBody`'s "working"): the agent that has it and what it is doing, its latest
// status, and Take back. The user's own words with the step already head the card, as their latest message on the
// task (`saidLine`); words Cockpit sent for a Run or an Allow show here, folded to a few lines. The light runs while
// the agent's session works, in the foreground or the background.
const WORKING_ACTIVITY = new Set(["Working now", "Background work"]);

function workingNote(step, state) {
  const handoff = step.handoff;
  const item = state.items.find((one) => one.id === step.item_id) || step.item;
  const session = handoff.session_id ? sessionMap(state).get(handoff.session_id) : null;
  const agent = handoff.session_id ? sessionLabel(session, "") : `a new session in ${repoOf(handoff.new_cwd)}`;
  const activity = item.activity || "";
  const runs = WORKING_ACTIVITY.has(activity);
  const named = session && itermLink(session, { class: "working-agent" }, h("span", { text: agent }));
  // Where it stands, as the item's card says it (`itemCard`): the session's latest status on an item the user added,
  // the summary on others.
  const standsField = item.agent_summary ? "agent_summary" : item.summary && item.summary !== item.title ? "summary" : null;
  const status = standsField ? summaryWords(item, standsField) : null;
  const foldKey = `working:${step.id}`;
  const open = ui.expanded.has(foldKey);
  const sent = handoff.author === "cockpit";
  const long = handoff.text.length > 280 || handoff.text.split("\n").length > 4;
  return h("section", { class: `working-note${runs ? " runs" : ""}`, "data-handed": step.id, "aria-label": "What the agent is doing" },
    h("p", { class: "working-head" },
      h("span", { class: "working-light", "aria-hidden": "true" }),
      h("strong", { text: runs ? "Working…" : activity || "With the agent" }),
      h("span", { class: "working-who" }, " · ", named || agent, activity === "Background work" ? " · in the background" : null)),
    item.doing && h("p", { class: "working-doing", text: item.doing }),
    status && h("p", { class: "working-status" },
      h("span", { class: "working-label", text: "Where it stands" }), status),
    sent && h("div", { class: "working-sent" },
      h("span", { class: "working-label", text: `Cockpit sent, ${ago(handoff.created_at)}` }),
      h("p", { class: `working-text${long && !open ? " folded" : ""}`, text: handoff.text }),
      long && h("button", {
        class: "button small ghost working-fold", type: "button", "aria-expanded": String(open),
        onclick: () => { if (open) ui.expanded.delete(foldKey); else ui.expanded.add(foldKey); render(false); },
      }, open ? "Show less" : "Show all")),
    h("button", { class: "button small ghost working-back", type: "button", title: "Take back: to the top of your queue", onclick: () => takeBack(step) },
      icon("back"), "Take back"));
}

// Busy, Waiting, Background and Finished this week are folded when the page opens.
const RAIL_FOLDED = new Set(["busy", "waiting", "background", "done"]);
// Each group's heading leads with an icon in its colour, which says whose move it is in place of a dot on every row:
// amber for the user's own, blue for agents, green for finished, grey for the rest.
const GROUP_ICONS = {
  emergency: "alert", queue: "stack",
  busy: "terminal", waiting: "hourglass", background: "shield", done: "done",
};

// One line per item, grouped by who moves next: the user's steps in queue order, then Busy, which starts with the
// steps the user handed to an agent, then Waiting, which also holds the user's snoozed steps, each with a clock
// (SPEC VIEW-27, VIEW-32). Some groups start folded (RAIL_FOLDED).
function renderSidebar(state) {
  const items = new Map(state.items.map((item) => [item.id, item]));
  const sessions = sessionMap(state);
  const shown = new Set();
  const take = (itemId) => {
    if (shown.has(itemId) || !items.has(itemId)) return false;
    shown.add(itemId);
    return true;
  };
  // Each row names its step, which flashes.js reads (SPEC PERM-24).
  const stepEntries = (steps, mark) => steps.filter((step) => !step.left && take(step.item_id))
    .map((step) => {
      const hand = handHeld(step.id);
      return {
        // Held Add and hand-off rows return before sidebarRow's selection class is set.
        selected: ui.shownItem === step.item_id && !hand && !ui.addsHeld.get(step.item_id)?.row,
        build: () => {
          const row = hand ? handHeldRow(items.get(step.item_id), hand) : sidebarRow(items.get(step.item_id), mark, sessions);
          row.dataset.step = step.id;
          if (!hand && step.expires_at && (mark === "queue" || mark === "ready")) markExpiry(row, step, items.get(step.item_id));
          return row;
        },
      };
    });
  const stepRows = (steps, mark) => stepEntries(steps, mark).map((entry) => entry.build());
  // Inbox lists the queue in its ranked order, also while the card holds the step the user opened in its place
  // (NEXT-16): a newer step that ranks above it moves up here at once, and the rows glide to their new places
  // (motion.js), while the card stays on the user's step and shows Resorted.
  const queue = state.queue;
  // The warnings head Inbox, above the step the user is on (SPEC UI-52).
  const queueRows = [...warningList(state).map(warningRow), ...stepRows(queue, "queue")];
  // Every item shows once, in its first group: Inbox (the queue, then the ready-next steps), the steps handed to agents
  // (first in Busy), the snoozed steps (in Waiting, after its handed steps, the soonest to wake first), then Busy and
  // Waiting by the item's group. A snoozed step takes its item before Busy and Waiting do.
  // Inbox shows also with nothing in it, as one row the card's empty Inbox picks.
  // Inbox counts its own rows and the ready-next steps folded under them.
  const readyRows = stepEntries(state.ready_next, "ready");
  const mine = [["queue", "Inbox", [...(queueRows.length ? queueRows : [inboxEmptyRow()]), ...moreRows(readyRows)],
    true, queueRows.length + readyRows.length]];
  // A handed step shows in its item's group: in Waiting once its agent stopped more than an hour ago.
  const handedIn = (group) => stepRows(state.with_agents.filter((step) => (items.get(step.item_id)?.group === "waiting") === (group === "waiting")), group);
  const handed = { busy: handedIn("busy"), waiting: handedIn("waiting") };
  // The steps each group lists as handed, for its heading's Undo after a Send (`handsHeading`).
  const handedIds = (group) => new Set(handed[group].map((row) => Number(row.dataset.step)));
  // Snoozed warnings that still hold go among them by their time (SPEC UI-72).
  const snoozed = [
    ...[...state.snoozed].sort((a, b) => a.snoozed_until.localeCompare(b.snoozed_until))
      .filter((step) => !step.left && take(step.item_id))
      .map((step) => ({ until: step.snoozed_until, row: snoozedRow(step, items.get(step.item_id), sessions) })),
    ...snoozedWarnings(state).map((warning) => ({ until: warning.snoozedUntil, row: snoozedWarningRow(warning) })),
  ].sort((a, b) => a.until.localeCompare(b.until)).map((entry) => entry.row);
  snoozed.forEach((row) => { row.dataset.wait = "snoozed"; });
  // What runs without the user. A task with a step for the user shows in Inbox, which took it above; a
  // to-do held for Add's Undo stays in Busy, where that Undo is.
  const backgroundRows = (state.background || []).filter((row) => row.item_id === null
    || (!ui.addsHeld.get(row.item_id)?.row && take(row.item_id)))
    .map((row) => (row.item_id === null ? backgroundSessionRow(row, sessions) : sidebarRow(items.get(row.item_id), "busy", sessions)));
  // To-dos the user added in the last half hour that have no step for them yet, newest first, stay at the top of Busy.
  // Only rows that came into Busy in the last minute pulse (leadArrivals).
  const fresh = freshTodos(state).filter((item) => take(item.id)).map((item) => sidebarRow(item, "busy", sessions));
  // The tasks Finished this week lists, for its heading's Undo.
  const doneIds = new Set();
  const byGroup = GROUPS.filter(([group]) => group !== "you").map(([group, label]) => {
    const rows = state.items.filter((item) => sidebarGroup(item, state) === group && !item.archived && take(item.id))
      .map((item) => ({ id: item.id, at: endedAt(item), row: sidebarRow(item, group, sessions) }));
    if (group === "done") {
      for (const entry of state.closed_recently.items) {
        if (entry.action === "archived" && !shown.has(entry.id)) rows.push({ id: entry.id, at: endedAt(entry), row: sidebarRow(entry, "done", sessions) });
      }
      rows.sort((a, b) => b.at.localeCompare(a.at));
      for (const entry of rows) doneIds.add(entry.id);
    }
    // A rest the user chose, such as a reminder they asked an agent for, shows as their snooze.
    if (group === "waiting") {
      for (const row of [...handed.waiting, ...rows.map((entry) => entry.row)]) {
        const item = items.get(Number(row.dataset.item));
        if (item?.wait === "snoozed" && !row.classList.contains("snoozed")) markReminder(row, item);
      }
    }
    const listed = [...(group === "busy" ? fresh : []), ...(handed[group] || []), ...(group === "waiting" ? snoozed : []),
      ...rows.map((entry) => entry.row)];
    const all = group === "busy" ? leadArrivals(listed, items) : listed;
    return [group, label, group === "waiting" ? waitParts(all, items) : all, !RAIL_FOLDED.has(group), all.length];
  });
  // Background goes before Finished this week, the last group.
  const finished = byGroup.findIndex(([group]) => group === "done");
  const groups = [...mine, ...byGroup.slice(0, finished), ["background", "Background", backgroundRows, !RAIL_FOLDED.has("background")], ...byGroup.slice(finished)];
  // While a new session's message box is open, an x beside Add closes it.
  const starting = ui.expanded.has("msg:new-session");
  const actions = document.querySelector(".rail-actions");
  const close = actions.querySelector(".close-new-session");
  if (starting && !close) {
    actions.append(h("button", { class: "button small ghost icon-only close-new-session", type: "button", "aria-expanded": "true",
      "aria-label": "Close the new session's message box", title: "Close the new session's message box",
      onclick: () => toggle("msg:new-session") }, icon("x")));
  } else if (!starting && close) close.remove();
  const emergency = ["emergency", "Emergency session", state.emergency ? emergencyRows(state.emergency) : [], true];
  // The groups sit in one frame of motion.js, and each heading moves with the rows (data-glide): when a row changes
  // group, it glides there and the rows and headings between make room.
  // While Busy is folded, a to-do held for its Undo time lights up Busy's heading: its count rolls up to take the
  // to-do in, and its Undo sits at the heading's right (headingUndo).
  // Likewise, while Finished this week is folded, a close whose Undo is on offer lights up its heading, with that
  // Undo at the heading's right (closeHeld). And while the group a sent step went to is folded, the hand-off's Undo
  // sits on that heading (handsHeading); Busy shows the newest of an Add's, a Send's and a reopened task's.
  const newer = (a, b) => (!a || (b && b.at > a.at) ? b : a);
  const reopened = ui.reopenedHeld;
  const held = {
    busy: newer(newer(busyHeld(state), handsHeading(handedIds("busy"))), reopened),
    waiting: handsHeading(handedIds("waiting")),
    done: doneHeld(doneIds),
  };
  fill("sidebar", h("div", { class: "sidebar-groups" }, [emergency, ...groups].filter(([, , rows]) => rows.length).map(([key, label, rows, open, count = rows.length]) => {
    const holding = held[key];
    const group = disclosure(`sidebar:${key}`, `sidebar-group ${key}`, [icon(GROUP_ICONS[key]), h("span", { class: "sidebar-group-name", text: label }),
      holding && holding === reopened ? reopenedHeading(count) : holding ? headingUndo(holding, count) : h("span", { class: "group-count", text: count }),
      key === "queue" && inboxSettingsKey()],
      h("ul", { class: "sidebar-list" }, rows), open);
    const summary = group.querySelector(":scope > summary");
    summary.dataset.glide = `sidebar ${key}`;
    if (holding) {
      summary.classList.add("holding");
      summary.style.setProperty("--since", `-${Date.now() - holding.at}ms`);
    }
    return group;
  })));
  showArrived();
}

// Inbox's settings (SPEC UI-70): how the sweeper ranks the work, and what it checks for new work, which open in
// Priorities and inbox (openSettings). An edit of them left there without Save puts the orange dot on the key.
function inboxSettingsKey() {
  const edited = inboxDraft() !== null;
  const words = ["Inbox settings", edited && EDIT_DOT_WORDS, "How your inbox is ranked, and what the sweeper checks for new work"];
  const key = iconButton("tune", words.filter(Boolean).join("\n"),
    { class: `button small ghost inbox-settings-open${edited ? " edited" : ""}`, "aria-current": settingsView.open ? "true" : null,
      onclick: (event) => { event.preventDefault(); openSettings("inbox"); } });
  if (edited) key.append(editDot());
  return key;
}

// The ready-next steps are ranked work that waits for one of the user's four places. They show under Inbox's own rows,
// folded into one "Show N more" row that shows them all; the same row at their end folds them again. While folded, the
// picked item's row stays, so search and links still find it. The page opens with these steps folded, including after
// Reload via reloadKeepingPlace.
function moreRows(rows) {
  if (!rows.length) return [];
  const open = ui.open.has("sidebar:more");
  const picked = rows.filter((entry) => entry.selected);
  const hidden = rows.length - picked.length;
  const shown = (open ? rows : picked).map((entry) => entry.build());
  const more = h("li", { class: "sidebar-more", "data-glide": "sidebar more" },
    // The name stays the same when the row opens or folds, so focus stays on it after the redraw (restoreFocus).
    h("button", { class: "side-row", type: "button", "aria-expanded": String(open), "aria-label": `${hidden} more steps`,
      title: "Ranked steps that wait for one of your four places",
      onclick: () => {
        if (open) ui.open.delete("sidebar:more"); else ui.open.add("sidebar:more");
        render(false);
      } }, h("span", { class: "sidebar-more-words", text: open ? "Show fewer" : `Show ${hidden} more` })));
  return shown.length === rows.length && !open ? shown : [...shown, more];
}

// The parts of Waiting, by what the work waits for, in store.WAIT_KINDS's order, the user's own snoozed steps last. Each heading's hover says more.
const WAIT_PARTS = [
  ["stuck", "Stuck", "Nothing moves this work and you have no step for it: no one has the next move, or its agent stopped over an hour ago. The next sweep starts or wakes an agent, or puts you a step; low-priority work waits until the queue is short"],
  ["blocked", "Agent blocked", "Its agent is waiting for approval, a provider limit, or recovery from an error"],
  ["person", "A person", "Another person's review, answer or work"],
  ["bot", "A bot", "A review bot such as review-bot"],
  ["staging", "On staging", "Shipped to staging; waits for the prod release"],
  ["dev", "On dev", "Shipped to dev; waits for the staging release"],
  ["release", "A release", "A release of another kind"],
  ["resumes", "Agent resumes later", "Its agent rests until a set time, for the reason its row's hover gives"],
  ["snoozed", "Snoozed by you", "Your snoozed steps and warnings, which come back to your Inbox when they wake,\nand the reminders you asked an agent for"],
];

// Waiting's rows under a small heading per part (WAIT_PARTS), each part in the order its rows came. A row's part is
// its own `data-wait` (a snoozed step) or its item's `wait` from the server.
function waitParts(rows, items) {
  const parts = new Map(WAIT_PARTS.map(([kind]) => [kind, []]));
  for (const row of rows) {
    const kind = row.dataset.wait || items.get(Number(row.dataset.item))?.wait;
    (parts.get(kind) || parts.get("stuck")).push(row);
  }
  return WAIT_PARTS.flatMap(([kind, label, hover]) => {
    const part = parts.get(kind);
    return part.length ? [h("li", { class: "sidebar-sub", "data-glide": `sidebar wait ${kind}`, title: hover },
      h("span", { class: "sidebar-sub-name", text: label }), h("span", { class: "group-count", text: part.length })), ...part] : [];
  });
}

// The item's group in the list. A to-do the user added on this page goes to Busy, also in the moment between the server
// making it and its session starting, when the server still counts it as Waiting. A poll can bring the server's item
// before the answer to the Add, which `addedItemIn` matches.
function sidebarGroup(item, state) {
  if (item.group !== "waiting" || item.sessions.length || item.open_steps) return item.group;
  const added = ui.addsSent.some((one) => one.added.id === item.id
    || (typeof one.added.id !== "number" && addedItemIn(state, one)?.id === item.id));
  return added ? "busy" : item.group;
}

// When each row in Busy came into it, by item id, as this browser saw it: the rows that came in the last ARRIVED_MS
// lead Busy, newest first, with a pulsing background (`li.fresh`), and every other row keeps its place. A row that
// leaves Busy and comes back counts as arriving again. The times are kept in the browser (BUSY_SEEN_KEY), so a reload
// keeps a row lit for the rest of its minute.
const BUSY_SEEN_KEY = "cockpit.busy.seen";
let busySeen = readBusySeen();
let arrivalTimer = null;

function readBusySeen() {
  try {
    const seen = JSON.parse(localStorage.getItem(BUSY_SEEN_KEY));
    if (seen && typeof seen.at === "number" && seen.ids && typeof seen.ids === "object") return { at: seen.at, ids: seen.ids, restored: true };
  } catch { /* storage off or the value broken: start afresh */ }
  return { at: null, ids: {}, restored: false };
}

// A row new to Busy since the last drawing came in now when the page has read the state without a gap since that
// drawing (`ui.watchedSince`), or, on the first drawing after a reload, when the drawing before the reload was under
// ARRIVED_MS ago. Otherwise it moved while the page was away, at an unknown time, and stays plain. An item made in the
// last ARRIVED_MS came in when it was made, so a to-do keeps one minute when its server id replaces the id it showed
// under while on its way. A to-do still held for its Undo (`li.held`) has its own look and counts once it is sent.
function busyArrivals(rows, items) {
  const now = Date.now();
  const restored = busySeen.restored && busySeen.at !== null && now - busySeen.at <= ARRIVED_MS;
  const watched = restored || (busySeen.at !== null && ui.watchedSince !== null && busySeen.at >= ui.watchedSince);
  const ids = {};
  for (const row of rows) {
    const id = row.dataset.item;
    if (row.classList.contains("held") || id in ids) continue;
    // A task the user's message just reopened came in now, also when it was made a moment ago.
    if (ui.reopenedHeld && String(ui.reopenedHeld.id) === id) { ids[id] = ui.reopenedHeld.at; continue; }
    if (id in busySeen.ids) { ids[id] = busySeen.ids[id]; continue; }
    const made = Date.parse(items.get(Number(id))?.created_at || "") - (ui.clockOffset || 0);
    ids[id] = now - made <= ARRIVED_MS ? Math.min(made, now) : watched ? now : 0;
  }
  busySeen = { at: now, ids, restored: false };
  try { localStorage.setItem(BUSY_SEEN_KEY, JSON.stringify({ at: now, ids })); } catch { /* storage off: this page still tracks */ }
  return ids;
}

// Busy's rows with those that came in the last ARRIVED_MS first and lit. As each one's minute ends, its light goes at
// once, also while a selection holds the page's redraw back (safeRender), and the redraw puts the row back in its place.
function leadArrivals(rows, items) {
  const arrived = busyArrivals(rows, items);
  const now = Date.now();
  const at = (row) => arrived[row.dataset.item] || 0;
  const lit = rows.filter((row) => now - at(row) < ARRIVED_MS).sort((a, b) => at(b) - at(a));
  for (const row of lit) row.classList.add("fresh");
  clearTimeout(arrivalTimer);
  arrivalTimer = null;
  if (lit.length) {
    arrivalTimer = setTimeout(() => {
      for (const row of lit) if (Date.now() - at(row) >= ARRIVED_MS) row.classList.remove("fresh");
      safeRender();
    }, Math.min(...lit.map(at)) + ARRIVED_MS - now + 50);
  }
  return [...lit, ...rows.filter((row) => !lit.includes(row))];
}

// The to-dos the user added with Add in the last JUST_ADDED_MS, open, newest first; the one still on its way to the
// server shows under its local id. Those without a step for the user lead Busy after the rows that just came in.
function freshTodos(state) {
  const since = new Date(Date.now() - JUST_ADDED_MS).toISOString();
  return state.items
    .filter((item) => /^todo-/.test(item.key) && item.created_at >= since && !item.archived && item.ball !== "done")
    .sort((a, b) => b.created_at.localeCompare(a.created_at));
}

// After the redraw that first shows the to-do just added, its place scrolls into view, once: its row while Busy is
// open, else Busy's heading, which carries the Undo then. Busy stays as the user left it, folded by default. motion.js
// puts the page back where the reader was once the list is drawn, and a scroll of the page's own stops that, so the
// place is looked up in the next frame.
function showArrived() {
  const arrived = ui.arrived;
  if (!arrived || arrived.scrolled) return;
  requestAnimationFrame(() => {
    if (ui.arrived !== arrived || arrived.scrolled) return;
    const row = arrivedPlace(arrived.id);
    if (!row) return;
    arrived.scrolled = true;
    ui.arrived = null;
    // The phone bar's Your items stays at the screen's top.
    if (row.closest(".phone-bar")) return;
    row.scrollIntoView({ block: "nearest" });
    // On a phone the list scrolls with the page, and the top bar covers the page's top edge and can grow taller after
    // the scroll: the row goes to the middle of the screen.
    if (row.getBoundingClientRect().top < document.getElementById("header").getBoundingClientRect().bottom + 8) {
      row.scrollIntoView({ block: "center" });
    }
  });
}

// Where the to-do just added shows in the list: its row while every group around it is open, else the heading of
// the folded group that holds it; on a phone's item screen, which hides the list, the bar's Your items, which holds
// its Undo. Null when it shows nowhere.
function arrivedPlace(id) {
  if (onPhone() && !phoneListShown()) return document.querySelector(".phone-items.holding");
  const row = document.querySelector(`#sidebar li[data-item="${CSS.escape(String(id))}"]`);
  const folded = row && row.closest("details:not([open])");
  return folded ? folded.querySelector(":scope > summary") : row;
}

// The newest to-do held for its Undo time whose Undo is a row in Busy (`hold.row`), as Busy's heading shows it
// (`headingUndo`); null when none is.
function busyHeld(state) {
  const items = state.items.filter((item) => ui.addsHeld.get(item.id)?.row && sidebarGroup(item, state) === "busy");
  const item = items.sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
  if (!item) return null;
  const hold = ui.addsHeld.get(item.id);
  return {
    at: hold.at, ms: UNDO_ADD_MS, added: 1, undo: hold.undo,
    title: `Undo: take “${item.title}” back into the Add box`, label: `Undo adding ${item.title}`,
  };
}

// The newest close whose Undo is on offer (app.js `closedNotice`) and that ended the task; null when none is.
function closeHeld(itemId) {
  return [...ui.closesHeld].reverse().find((hold) => hold.itemIds.includes(itemId)) || null;
}

// The newest close with its Undo on offer, as Finished this week's heading shows it (`headingUndo`); null when none
// is. Its count rolls up by the tasks of that close the group lists. `doneIds`: the tasks the group lists.
function doneHeld(doneIds) {
  const hold = [...ui.closesHeld].reverse().find((one) => one.itemIds.some((id) => doneIds.has(id)));
  if (!hold) return null;
  const added = hold.itemIds.filter((id) => doneIds.has(id)).length;
  return {
    at: hold.at, ms: UNDO_CLOSE_MS, added, undo: hold.undo,
    title: added > 1 ? `Undo: reopen these ${added} tasks` : "Undo: reopen the task you just closed",
    label: added > 1 ? `Undo closing ${added} tasks` : "Undo closing the task",
  };
}


// The newest Add, close or hand-off with its Undo on offer, as the phone bar's Your items shows it (`buildPhoneBar`);
// null when none is. `hold` is the app.js hold it shows; `close` whether it is a close.
function phoneHeld(state) {
  const closes = [...ui.closesHeld];
  const close = doneHeld(new Set(closes.flatMap((hold) => hold.itemIds)));
  const hand = handsHeading(new Set([...ui.handsHeld].map((hold) => hold.stepId)));
  const add = busyHeld(state);
  const newest = [close, hand, add].reduce((a, b) => (!a || (b && b.at > a.at) ? b : a), null);
  return newest && { ...newest, close: newest === close, hold: newest.undo };
}

// The newest hand-off of this step whose Undo is on offer (app.js `handedNotice`); null when none is.
function handHeld(stepId) {
  return [...ui.handsHeld].reverse().find((hold) => hold.stepId === stepId) || null;
}

// The newest hand-off with its Undo on offer among the steps a group lists, as its heading shows it (`headingUndo`);
// null when none is. The sent step's item leaves Inbox for this group, so the count rolls up by one.
function handsHeading(stepIds) {
  const hold = [...ui.handsHeld].reverse().find((one) => stepIds.has(one.stepId));
  if (!hold) return null;
  return {
    at: hold.at, ms: UNDO_HAND_MS, added: 1, undo: hold.undo,
    title: `Undo: take back your message to ${hold.to}; it goes once this time ends`,
    label: `Take back your message to ${hold.to}`,
  };
}

// The emergency session (SESSION-21) once it has a message, first in the list: the user's last message, a link to its
// own page, where it goes on working when this page does not, and Close
function emergencyRow(session) {
  // A Codex session runs on no Claude account (SESSION-22).
  const agent = session.provider === "codex" ? "Codex" : "Claude";
  const on = session.account ? `${session.account} account` : agent;
  const sub = session.running ? `${agent} is answering${session.account ? `, on the ${on}` : ""}`
    : `${capitalize(on)}, ${ago(session.at)}`;
  return h("li", { class: "emergency-row" },
    // Like the items' rows, it shows only its name; where it stands is in its title.
    h("a", { class: "side-row", href: "/emergency", title: `Open the emergency session\n${sub}` },
      h("span", { class: "sidebar-lead" }),
      h("span", { class: "sidebar-title", text: session.text }),
      session.running && workingMark()),
    iconButton("x", `Close the emergency session\n${agent} forgets the conversation`,
      { class: "button small ghost emergency-close", onclick: () => closeEmergency(session) }));
}

// A row that cannot be drawn leaves the rest of the list working: the emergency page is still in the top-left menu.
function emergencyRows(session) {
  try {
    return [emergencyRow(session)];
  } catch (error) {
    console.error("emergency session: could not draw its row", error);
    return [];
  }
}

// Only an answer under way asks first, as closing ends it. The list's idea of that can be behind, so Cockpit
// refuses to end an answer the user did not agree to (409), and the close asks then.
async function closeEmergency(session) {
  const ask = () => window.confirm(`${session.provider === "codex" ? "Codex" : "Claude"} is still answering in the emergency session. Stop it and close the session?`);
  const seen = { session_id: session.session_id, count: session.count };
  if (session.running && !ask()) return;
  try {
    try {
      await api("POST", "/api/emergency/close", { ...seen, end_answer: session.running });
    } catch (error) {
      if (error.status !== 409) throw error;
      if (!ask()) return;
      await api("POST", "/api/emergency/close", { ...seen, end_answer: true });
    }
    toast("Closed the emergency session");
  } catch (error) {
    toast(`Could not close the emergency session: ${error.message}`, true);
  } finally {
    refresh();
  }
}

// A warning's line in Inbox (SPEC UI-52): dark red with light text, pulsing, with the warning's few words; it
// opens the warning on the card. It goes once the warning is fixed or closed.
function warningRow(warning) {
  const selected = ui.shownWarning === warning.key;
  return h("li", { class: `side-item warn-row${selected ? " sel" : ""}`, "data-warning": warning.key, "data-glide": `warning ${warning.key}`, ...(warning.row || {}) },
    h("button", { class: "side-row", type: "button", title: warning.title, "aria-current": selected && !sessionView.id && !settingsView.open ? "true" : null,
      onclick: () => openWarning(warning.key) },
    h("span", { class: "sidebar-lead" }, icon(warning.mark)),
    h("span", { class: "sidebar-title", text: warning.short })));
}

// A background session opens its conversation, with a dot while it works.
function backgroundSessionRow(row, sessions) {
  const session = sessions.get(row.session_id);
  const selected = sessionView.id === row.session_id;
  const lead = row.kind === "standing"
    ? (row.logo ? h("img", { class: "standing-mark", src: row.logo, alt: "" }) : icon("shield"))
    : icon(row.kind === "sweeper" ? "radar" : "terminal");
  return h("li", { class: `side-item background-session${selected ? " sel" : ""}`, "data-glide": `background ${row.session_id}` },
    h("button", { class: "side-row", type: "button", title: row.name, "aria-current": selected ? "true" : null,
      onclick: () => openSessionView(row.session_id, null) },
    h("span", { class: "sidebar-lead" }, lead),
    h("span", { class: "sidebar-title", text: row.name }),
    session?.working_now && workingMark()));
}

// The empty Inbox's one row: picked while the card shows the empty Inbox, which it does with nothing else picked.
function inboxEmptyRow() {
  const selected = ui.shownItem === null && ui.shownWarning === null;
  return h("li", { class: `side-item inbox-empty${selected ? " sel" : ""}`, "data-glide": "inbox-empty" },
    h("button", { class: "side-row", type: "button", "aria-current": selected && !sessionView.id && !settingsView.open ? "true" : null,
      title: "Nothing waits for you", onclick: () => showOnCard(null, null) },
    h("span", { class: "sidebar-lead" }, icon("check")),
    h("span", { class: "sidebar-title", text: "Nothing waiting" })));
}

// The pulsing dot at the end of a row while one of its sessions works (UI-49), the only dot a row has.
function workingMark() {
  return h("span", { class: "sidebar-working", role: "img", "aria-label": "A session is working on this", title: "A session is working on this" });
}

// An item's line: the Cockpit key on Cockpit's own work, its title, a pulsing dot while a session works on it, and
// how long ago it last changed, which keeps the list clean. Whose move it is shows in the group's heading. The item's
// sessions open from the menu on the step's card (`stepMenu`), not from the row.
function sidebarRow(item, group, sessions) {
  const hold = ui.addsHeld.get(item.id);
  if (hold && hold.row) return heldRow(item, hold);
  const closing = group === "done" ? closeHeld(item.id) : null;
  if (closing) return closedHeldRow(item, closing);
  const selected = ui.shownItem === item.id;
  // A task closed as not needed lists no sessions.
  const linked = item.step_sessions ? itemSessions(item).map((id) => sessions.get(id) || { session_id: id }) : [];
  const working = linked.some((session) => session.working_now);
  const raised = raisedPriority(item) ? ` ${item.priority}` : "";
  // `side-item` leads the classes, so motion.js knows the row as the same one when it is picked or its priority changes.
  const row = h("li", { "data-item": item.id, class: `side-item${selected ? " sel" : ""}${raised}` },
    // A to-do on its way to the server opens once the server made it (`openWhenMade`).
    h("button", { class: "side-row", type: "button", "aria-current": selected && !sessionView.id && !settingsView.open ? "true" : null,
      // In Busy and Waiting the hover line under the title says what the agent works on or what the item waits for.
      // The title in full up to 100 characters: a to-do's title can be the user's whole message (SPEC UI-46).
      // A resting agent's row also says why its work picks up only then.
      title: (group === "busy" || group === "waiting") && workTip(item)
        ? [clip(item.title, 100), item.why, clip(workTip(item), 60)].filter(Boolean).join("\n") : clip(item.title, 100),
      onclick: () => { if (typeof item.id === "number") selectItem(item.id); else openWhenMade(item.id); } },
    h("span", { class: "sidebar-lead" }, Icons.projectMark(item.project)),
    h("span", { class: "sidebar-title", text: item.title }),
    (group === "busy" || (group === "waiting" && item.wait === "blocked")) && item.activity
      && h("span", { class: "sidebar-activity", text: item.activity }),
    mergedSideMark(item),
    working && workingMark(),
    h("span", { class: "sidebar-draft", role: "img", "aria-label": "Unsent draft", title: "Unsent draft", "data-draft-item": item.id,
      hidden: typeof item.id !== "number" || !itemHasDraft(item.id) }, icon("pencil")),
    h("span", { class: "sidebar-age", text: age(group === "done" ? endedAt(item) : item.updated_at) })));
  return row;
}

// Another task merged into this one (SPEC NEXT-55), until the user's next message or press on it: the joining arrows
// after the title in the merge pink, naming the tasks that joined on hover.
function mergedSideMark(item) {
  if (!item.merges || !item.merges.length) return null;
  const words = `Merged in: ${item.merges.map((merge) => merge.from_title).join(", ")}`;
  return h("span", { class: "sidebar-merged", role: "img", "aria-label": words, title: words }, icon("joined"));
}

// The Inbox row of a step useful only until a time (SPEC NEXT-60): while the time runs, a warning's red and pulse with
// a stopwatch in the lead and the time left counting down in place of the row's age; once it passed, an ordinary row
// with a small stopwatch after the title that says when the time ran out.
function markExpiry(row, step, item) {
  const time = row.querySelector(".sidebar-age");
  if (!time) return;
  const until = clock(step.expires_at);
  if (!expiring(step)) {
    const words = `Its time ran out at ${until}`;
    row.querySelector(".sidebar-title").after(h("span", { class: "sidebar-expired", role: "img", "aria-label": words, title: words }, icon("timer")));
    return;
  }
  row.classList.add("expiring-row");
  row.querySelector(".sidebar-lead").replaceChildren(icon("timer"));
  row.querySelector(".side-row").title = `${clip(item.title, 100)}\nWorks until ${until}`;
  time.textContent = timerClock(secondsLeft(step.expires_at));
  time.dataset.expires = step.expires_at;
}

// A snoozed step's row in Waiting: the item's line with a clock before its time, and the time is how long
// until the step wakes back into the queue; hovering it says when. While Cockpit reads the words the user snoozed it
// with (VIEW-29), the time says "Reading".
function snoozedRow(step, item, sessions) {
  const row = sidebarRow(item, "snoozed", sessions);
  row.dataset.step = step.id;
  row.classList.add("snoozed");
  const reading = readingWords(ui.state, `step:${step.id}`);
  const wakes = reading
    ? `Cockpit is reading when it comes back; until then, snoozed until ${when(new Date(step.snoozed_until))}`
    : `Snoozed until ${when(new Date(step.snoozed_until))}`;
  const button = row.querySelector(".side-row");
  button.title = `${item.title}\n${wakes}`;
  const time = row.querySelector(".sidebar-age");
  time.textContent = reading ? "Reading" : untilWords(step.snoozed_until);
  time.before(h("span", { class: "sidebar-snoozed", role: "img", "aria-label": wakes }, icon("clock")));
  return row;
}

// The row of an item whose agent rests until a time the user chose, in Snoozed by you: like a snoozed
// step's, a clock before how long until the agent picks it up again.
function markReminder(row, item) {
  const time = row.querySelector(".sidebar-age");
  if (!time || !item.tip_at) return;
  row.classList.add("snoozed");
  time.textContent = untilWords(item.tip_at);
  time.before(h("span", { class: "sidebar-snoozed", role: "img", "aria-label": `Snoozed until ${when(new Date(item.tip_at))}` }, icon("clock")));
}

// A snoozed warning's row in Waiting (SPEC UI-72), like a snoozed step's: its mark, its few words and a clock before
// how long until it wakes. It opens the warning on the card, whose menu holds Unsnooze.
function snoozedWarningRow(warning) {
  const selected = ui.shownWarning === warning.key;
  const reading = readingWords(ui.state, `warning:${warning.key}`);
  const wakes = reading
    ? `Cockpit is reading when it comes back; until then, snoozed until ${when(new Date(warning.snoozedUntil))}`
    : `Snoozed until ${when(new Date(warning.snoozedUntil))}`;
  return h("li", { class: `side-item snoozed warn-snoozed${selected ? " sel" : ""}`, "data-warning": warning.key,
    "data-glide": `warning ${warning.key}` },
  h("button", { class: "side-row", type: "button", title: `${warning.title}\n${wakes}`,
    "aria-current": selected && !sessionView.id && !settingsView.open ? "true" : null, onclick: () => openWarning(warning.key) },
  h("span", { class: "sidebar-lead" }, icon(warning.mark)),
  h("span", { class: "sidebar-title", text: warning.short }),
  h("span", { class: "sidebar-snoozed", role: "img", "aria-label": wakes }, icon("clock")),
  h("span", { class: "sidebar-age", text: reading ? "Reading" : untilWords(warning.snoozedUntil) })));
}

// How long until a moment, as short as `age` puts how long ago: "5m", "3h", "2d".
function untilWords(iso) {
  const minutes = Math.ceil(Math.max(0, new Date(iso).getTime() - Date.now()) / 60000);
  if (minutes < 60) return `${minutes}m`;
  if (minutes < 2880) return `${Math.floor(minutes / 60)}h`;
  return `${Math.floor(minutes / 1440)}d`;
}

// The row of a to-do whose Add waits for its Undo time: the whole row is an Undo button, which takes the
// to-do back into the Add box.
function heldRow(item, hold) {
  return h("li", { "data-item": item.id, class: "side-item held" }, undoAddButton(item, hold, "side-row undo-add", hold.undo));
}

// While Busy is folded, the newest held to-do lights up Busy's heading, and while Finished this week is folded, the
// newest close with its Undo on offer lights up that heading in the close's plum. For Busy: the whole line takes the
// accent's tint; Busy's name stays; its count rolls slowly from the number before the Add to the new one, which then
// stays bold in the accent and pulses; and a small Undo with the seconds left sits at the heading's right, the bar
// that runs down over the Undo time inside it, not across the line. The roll, the light and the bar are timed from
// the Add (`hold.at`, `--since` on the heading), so a redraw picks them up where they were rather than playing them
// again. A press on Undo undoes and leaves Busy folded. Once the user opens Busy, the heading is plain again
// (sidebar.css) and the row in the list is the Undo
function headingUndo(held, count) {
  const roll = h("span", { class: "group-count count-roll" },
    h("span", { class: "count-was", "aria-hidden": "true", text: Math.max(0, count - held.added) }),
    h("span", { class: "count-now", text: count }));
  return [roll, heldUndo(held)];
}

// The small Undo of a held Add, close or hand-off (`busyHeld`, `doneHeld`, `handsHeading`), with the seconds left and
// the bar that runs down over the Undo time inside it.
function heldUndo(held) {
  const left = Math.max(0, held.at + held.ms - Date.now());
  const fill = h("span", { class: "undo-add-fill busy-undo-fill", "aria-hidden": "true" });
  fill.style.setProperty("--undo-ms", `${held.ms}ms`);
  fill.style.setProperty("--undo-delay", `-${held.ms - left}ms`);
  const undo = h("button", { class: "busy-undo", type: "button", onclick: (event) => { event.preventDefault(); held.undo(); },
    title: held.title, "aria-label": held.label },
    fill, icon("back"),
    h("span", { class: "undo-add-label", text: "Undo" }),
    h("span", { class: "undo-add-secs", "aria-hidden": "true", "data-until": String(held.at + held.ms), text: undoSeconds(left) }));
  tickUndo();
  return undo;
}

// Busy's folded heading just after a message took a finished task back to Busy (SPEC CONTROL-28): the count rolls up as
// for an Add, and the word at its right says what came in. `ui.reopenedHeld` ends it; there is nothing to undo.
function reopenedHeading(count) {
  return [
    h("span", { class: "group-count count-roll" },
      h("span", { class: "count-was", "aria-hidden": "true", text: Math.max(0, count - 1) }),
      h("span", { class: "count-now", text: count })),
    h("span", { class: "busy-reopened", text: "Reopened" }),
  ];
}

// The row of a task in Finished this week while its close's Undo is on offer: the whole row is an Undo
// button in the close's plum, its bar running down over the Undo time, which reopens the task.
function closedHeldRow(item, hold) {
  const left = Math.max(0, hold.at + UNDO_CLOSE_MS - Date.now());
  const button = h("button", { class: "side-row undo-add undo-close", type: "button", onclick: hold.undo,
    title: `Undo: reopen “${item.title}”`, "aria-label": `Undo closing ${item.title}` },
    h("span", { class: "undo-add-fill", "aria-hidden": "true" }),
    h("span", { class: "sidebar-lead" }, icon("back")),
    h("span", { class: "undo-add-label", text: "Undo" }),
    h("span", { class: "sidebar-title", text: item.title }),
    h("span", { class: "undo-add-secs", "aria-hidden": "true", "data-until": String(hold.at + UNDO_CLOSE_MS), text: undoSeconds(left) }));
  button.style.setProperty("--undo-ms", `${UNDO_CLOSE_MS}ms`);
  button.style.setProperty("--undo-delay", `-${UNDO_CLOSE_MS - left}ms`);
  tickUndo();
  return h("li", { "data-item": item.id, class: "side-item held-close" }, button);
}

// The row of a step the user just sent while its hand-off's Undo is on offer: the whole row is an Undo
// button, its bar running down over the time the message waits, which takes the step back as Take back does.
function handHeldRow(item, hold) {
  const left = Math.max(0, hold.at + UNDO_HAND_MS - Date.now());
  const button = h("button", { class: "side-row undo-add", type: "button", onclick: hold.undo,
    title: `Undo: take back your message to ${hold.to}; it goes once this time ends`, "aria-label": `Take back your message to ${hold.to}` },
    h("span", { class: "undo-add-fill", "aria-hidden": "true" }),
    h("span", { class: "sidebar-lead" }, icon("back")),
    h("span", { class: "undo-add-label", text: "Undo" }),
    h("span", { class: "sidebar-title", text: item.title }),
    h("span", { class: "undo-add-secs", "aria-hidden": "true", "data-until": String(hold.at + UNDO_HAND_MS), text: undoSeconds(left) }));
  button.style.setProperty("--undo-ms", `${UNDO_HAND_MS}ms`);
  button.style.setProperty("--undo-delay", `-${UNDO_HAND_MS - left}ms`);
  tickUndo();
  return h("li", { "data-item": item.id, class: "side-item held-hand" }, button);
}

// The Undo of a to-do whose Add waits for its Undo time: a tint fills it and runs down from right to left as the time
// passes, and the seconds left count down at its end. Each drawing picks the bar up where the last one left it.
function undoAddButton(item, hold, cls, onclick) {
  const left = Math.max(0, hold.at + UNDO_ADD_MS - Date.now());
  const button = h("button", { class: cls, type: "button", onclick,
    title: `Undo: take “${item.title}” back into the Add box`, "aria-label": `Undo adding ${item.title}` },
    h("span", { class: "undo-add-fill", "aria-hidden": "true" }),
    h("span", { class: "sidebar-lead" }, icon("back")),
    h("span", { class: "undo-add-label", text: "Undo" }),
    h("span", { class: "sidebar-title", text: item.title }),
    h("span", { class: "undo-add-secs", "aria-hidden": "true", "data-until": String(hold.at + UNDO_ADD_MS), text: undoSeconds(left) }));
  // Set through the style object: the page's Content-Security-Policy refuses style attributes.
  button.style.setProperty("--undo-ms", `${UNDO_ADD_MS}ms`);
  button.style.setProperty("--undo-delay", `-${UNDO_ADD_MS - left}ms`);
  tickUndo();
  return button;
}

function undoSeconds(left) {
  return `${Math.ceil(left / 1000)} s`;
}

// Counts the held rows' seconds down while any shows.
function tickUndo() {
  if (ui.undoTick) return;
  ui.undoTick = setInterval(() => {
    const shown = document.querySelectorAll(":is(#sidebar, .phone-bar) .undo-add-secs");
    if (!shown.length) { clearInterval(ui.undoTick); ui.undoTick = null; return; }
    for (const secs of shown) secs.textContent = undoSeconds(Math.max(0, Number(secs.dataset.until) - Date.now()));
  }, 200);
}

// The Add box flies into the new to-do's place in Busy: a panel the size of the box shrinks and moves onto its row, or
// onto Busy's heading while Busy is folded, then fades, so the eye follows the to-do to where it went. It runs after
// showArrived scrolled that place into view; on a phone's item screen it lands on the bar's Your items. No flight with
// reduced motion, or when the place is out of sight.
function flyToRow(from, id, title) {
  if (!from || !from.width || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  requestAnimationFrame(() => {
    const row = arrivedPlace(id);
    const to = row && row.getBoundingClientRect();
    if (!to || !to.width || to.bottom < 0 || to.top > window.innerHeight) return;
    const ghost = h("div", { class: "add-flight", "aria-hidden": "true" }, h("span", { text: title }));
    document.body.append(ghost);
    const at = (rect) => ({ left: `${rect.left}px`, top: `${rect.top}px`, width: `${rect.width}px`, height: `${rect.height}px` });
    row.classList.add("landing");
    const flight = ghost.animate([
      { ...at(from), opacity: 0.95 },
      { ...at(to), opacity: 1, offset: 0.8 },
      { ...at(to), opacity: 0 },
    ], { duration: 520, easing: "cubic-bezier(.3,.7,.2,1)", fill: "both" });
    const land = () => {
      ghost.remove();
      for (const shown of document.querySelectorAll(`#sidebar li[data-item="${CSS.escape(String(id))}"], #sidebar .sidebar-group > summary.landing, .phone-items.landing`)) shown.classList.remove("landing");
    };
    flight.finished.then(land, land);
  });
}

// Whether the user left words unsent on an item: in its own message box, in Add more, in a reply,
// feedback or close reason on one of its open steps, or in a session's conversation view opened for it (`sessionKey`).
// Its row then shows a pencil in the user's amber.
function itemHasDraft(itemId) {
  const typed = (key) => Boolean((ui.drafts.get(key) || "").trim());
  const steps = [ui.state.queue, ui.state.ready_next, ui.state.with_agents, ui.state.snoozed].flat();
  return typed(`msg:item:${itemId}`) || typed(addMoreKey(itemId))
    || steps.some((step) => step.item_id === itemId && (hasDraft(step.id) || typed(`close:${step.id}`)))
    || [...ui.drafts.keys()].some((key) => key.startsWith("msg:view:") && key.endsWith(`:${itemId}`) && typed(key));
}

// Typing shows and hides the rows' pencils without a redraw.
function showItemDraftMarks() {
  if (!ui.state) return;
  for (const mark of document.querySelectorAll("[data-draft-item]")) mark.hidden = !itemHasDraft(Number(mark.dataset.draftItem));
}

// The session view: the conversation of one session in place of the card, with its own message box. It reads the
// session's transcript every few seconds while it shows.
// `item` is the item the user opened it from, which its Back and its messages go to; `turn` counts the openings, so a
// reading that comes back after the view closed or opened again is dropped.
const sessionView = { id: null, item: null, from: null, drawnFor: null, turn: 0, log: null, error: "", timer: null, printed: "", open: new Set() };

function sessionViewButton(session, props, ...children) {
  return h("button", {
    ...props, type: "button", title: "Open conversation",
    // It opens the view, not iTerm2, so its name says so whatever the caller's label said.
    "aria-label": `Show the conversation of ${sessionLabel(session, session.cwd)}`, "data-opens": session.session_id,
    // Counted as one control whichever session it opens (SPEC UI-45).
    "data-use": "session-key",
    onclick: (event) => { event.preventDefault(); event.stopPropagation(); openSessionView(session.session_id, openedFrom(event.currentTarget, session.session_id)); },
  }, ...children);
}

// The item a session's key belongs to: the step card or row it sits in (with no `button`, as from the keyboard,
// none), else the item the card shows when it lists the session, else the first item that lists it.
function openedFrom(button, sessionId) {
  if (!ui.state) return null;
  const host = button ? button.closest("[data-item], [data-step]") : null;
  const stepId = host && !host.dataset.item ? Number(host.dataset.step) : null;
  const step = stepId !== null ? [ui.next, ...ui.state.queue, ...ui.state.ready_next, ...ui.state.snoozed, ...ui.state.with_agents].find((one) => one && one.id === stepId) : null;
  if (host && host.dataset.item) return Number(host.dataset.item);
  if (step) return step.item_id;
  const lists = (item) => itemSessions(item).includes(sessionId);
  const shown = ui.state.items.find((item) => item.id === ui.shownItem);
  if (shown && lists(shown)) return shown.id;
  const first = ui.state.items.find(lists);
  return first ? first.id : null;
}

function viewedSession() {
  const sessions = ui.state ? sessionMap(ui.state) : new Map();
  return sessions.get(sessionView.id) || { session_id: sessionView.id };
}

function viewedItem() {
  return ui.state ? ui.state.items.find((item) => item.id === sessionView.item) || null : null;
}

// Opening a session slides its view in from the right as the card moves off to the left, and Back slides the card
// back in. sidebar.css draws the slide; with reduced motion the two cross-fade, and a browser without view
// transitions changes at once. The browser runs `change` a moment later, after it has pictured the old page; the
// promise resolves once it has run. A second slide started meanwhile replaces the first: the browser skips the first
// one's animation, still runs its `change`, and rejects its `ready`. A failing `change` shows through the promise
// returned here and through `finished`, so `ready` is settled here without a report.
let sliding = null;

function slideView(direction, change) {
  if (!document.startViewTransition) {
    change();
    return Promise.resolve();
  }
  const root = document.documentElement;
  root.dataset.slide = direction;
  const transition = document.startViewTransition(change);
  sliding = transition;
  transition.ready.catch(() => {});
  transition.finished.finally(() => {
    if (sliding !== transition) return;
    sliding = null;
    delete root.dataset.slide;
  });
  return transition.updateCallbackDone;
}

function openSessionView(sessionId, itemId) {
  ui.navigations++;
  // From a session's line in the phone's list: the conversation shows on the item screen.
  const fromList = onPhone() && phoneListAsked();
  // The conversation takes the card's place: one of another item's session leaves the step on the clock, as showing
  // that item does (selectItem, TRACK-8), however the user opened it (a line, Search, the keyboard).
  const timer = ui.state && drawn(ui.state).timer;
  if (timer && timer.item_id !== itemId) leaveTimedStep(timer);
  leaveFeedbackHoldStep(itemId);
  return slideView("in", () => {
    if (fromList) showPhoneItem(false);
    closeSettings(false);
    clearTimeout(sessionView.timer);
    // The step the card showed for this item and session, which the conversation's Send then answers (viewedStep).
    const from = ui.next && ui.next.item_id === itemId && ui.next.target && ui.next.target.session_id === sessionId ? ui.next.id : null;
    Object.assign(sessionView, { id: sessionId, item: itemId, from, turn: sessionView.turn + 1, log: null, error: "", printed: "", open: new Set() });
    document.body.classList.add("viewing-session");
    if (itemId !== null && ui.shownItem !== itemId) keepSelected(itemId);
    drawSessionView();
    render(false);
    window.scrollTo({ top: 0 });
    loadSessionLog();
  });
}

// Back gives the focus to the key the user opened the view from: the session's key on the card, or the session's
// line in the sidebar.
function closeSessionView(redraw) {
  if (!sessionView.id) return;
  const id = sessionView.id;
  clearTimeout(sessionView.timer);
  Object.assign(sessionView, { id: null, item: null, turn: sessionView.turn + 1 });
  document.body.classList.remove("viewing-session");
  // The card shows again, so a step the conversation left starts again (leaveTimedStep).
  ui.leftStepId = null;
  fill("session-view");
  if (!redraw) return;
  // Back from the conversation: the user went to the card (`openWhenMade`).
  ui.navigations++;
  render(false);
  // The control that opened it: the session's button, or the ⋯ of the step menu it was opened from (`stepMenu`).
  const opener = [...document.querySelectorAll(`#next [data-opens="${CSS.escape(id)}"], #next [data-sessions~="${CSS.escape(id)}"], #sidebar [data-opens="${CSS.escape(id)}"]`)]
    .find((element) => element.getClientRects().length > 0);
  opener?.focus({ preventScroll: true });
}

function drawSessionView() {
  const session = viewedSession();
  const item = viewedItem();
  const iterm = itermAnchor(session, { class: "button small ghost" }, icon("terminal"), h("span", { class: "label", text: "iTerm2" }));
  fill("session-view", h("article", { class: "card session-card" },
    h("header", { class: "session-head" },
      h("button", { class: "button small ghost", type: "button", onclick: () => slideView("back", () => closeSessionView(true)) }, icon("back"),
        h("span", { class: "label", text: item ? item.title : "Back" })),
      h("div", { class: "session-name", id: "session-name" }),
      iterm),
    h("div", { class: "session-log", id: "session-log", "aria-live": "off" }),
    h("div", { class: "session-composer", id: "session-composer" })));
  drawSessionHead();
  drawSessionLog(true);
  drawSessionComposer();
}

// The step a message from the conversation answers: the viewed item's step that waits for the user and goes to this
// session. Send hands it off as the step's own reply box does (CONTROL-5), so it leaves the user's queue. Of several
// such steps it is the one the card showed when the conversation opened, else the first in the order the queue shows,
// then Waiting next, then Snoozed.
function viewedStep() {
  const item = viewedItem();
  if (!item) return null;
  const lists = drawn(ui.state);
  const queued = new Map(lists.queue.map((step) => [step.id, step]));
  const steps = [...shownOrder(lists.queue).map((id) => queued.get(id)), ...(lists.ready_next || []), ...(lists.snoozed || [])]
    .filter((step) => step && step.item_id === item.id && !step.left && step.target && step.target.session_id === sessionView.id);
  return steps.find((step) => step.id === sessionView.from) || steps[0] || null;
}

// One box per session and item the view was opened for: a session that works on two items keeps each item's words
// with that item, so Take back and Send stay with the step the view shows. A view opened without an item keeps its
// words under the session alone, which marks no item (`itemHasDraft`).
const sessionKey = () => (sessionView.item === null ? `msg:view:${sessionView.id}` : `msg:view:${sessionView.id}:${sessionView.item}`);

// What the box shows besides its words: the step it answers and the page comments and choices that go with it.
function sessionComposerShape(step) {
  if (!step) return "";
  return JSON.stringify([step.id, stepPageComments(step, ui.state), stepPageChoices(step, ui.state)]);
}

// The message box under the conversation, with its own draft. It is drawn again when its words changed outside it, as
// after Send or Take back, and when what it shows besides them changed, as once Send handed the step off. The focus and the caret stay.
function drawSessionComposer() {
  const el = document.getElementById("session-composer");
  if (!el) return;
  const session = viewedSession();
  const item = viewedItem();
  const step = viewedStep();
  sessionView.drawnFor = sessionComposerShape(step);
  const before = el.querySelector("textarea");
  const focused = before && document.activeElement === before ? [before.selectionStart, before.selectionEnd] : null;
  const made = composer(sessionKey(), {
    session: { session_id: session.session_id, name: session.name || "", state: session.state || "stopped", alive: Boolean(session.alive), working_now: Boolean(session.working_now), provider: session.provider },
    context: item ? (step ? { item_id: item.id, step_id: step.id } : { item_id: item.id }) : {}, project: item ? item.project : null,
    step, handOff: Boolean(step), toSession: true,
  });
  el.replaceChildren(made);
  const after = el.querySelector("textarea");
  if (focused && after) { after.focus({ preventScroll: true }); after.setSelectionRange(...focused); }
}

// The session's name and one line of where it stands: what it does now, what it waits on, and when it last wrote.
function drawSessionHead() {
  const el = document.getElementById("session-name");
  if (!el) return;
  const session = viewedSession();
  const state = session.state ? sessionState(session) : "stopped";
  const last = sessionView.log && sessionView.log.parts.at(-1);
  const doing = session.waits_on_dialog ? `at a dialog in its terminal: ${session.needs || "it waits for an answer"}`
    : state === "idle" && (session.busy_with || []).length ? `idle, waits on ${session.busy_with.join(", ")}`
    : AGENT_DOING[state] || state;
  const lastWords = last && last.at ? `last activity ${ago(last.at)}` : "";
  el.replaceChildren(stateDot(state, doing), h("strong", { text: sessionLabel(session, "") }),
    h("span", { class: "session-meta", "data-time": last && last.at ? last.at : null,
      text: [capitalize(doing), lastWords, session.account && `${session.account} account`, repoOf(session.cwd || "")].filter(Boolean).join(" · ") }));
}

async function loadSessionLog() {
  clearTimeout(sessionView.timer);
  const { id, turn } = sessionView;
  if (!id) return;
  const session = viewedSession();
  // A Codex session's conversation comes from Codex, a Claude session's from its transcript.
  try {
    const log = await api("GET", `/api/sessions/${encodeURIComponent(id)}/log`);
    if (sessionView.turn !== turn) return;
    sessionView.log = log;
    sessionView.error = "";
  } catch (error) {
    if (sessionView.turn !== turn) return;
    console.error("a session's conversation did not load", error);
    sessionView.error = error.message;
  }
  drawSessionHead();
  drawSessionLog(false);
  const wait = session.working_now ? SESSION_LOG_MS : SESSION_LOG_IDLE_MS;
  clearTimeout(sessionView.timer);
  sessionView.timer = setTimeout(function again() {
    if (sessionView.turn !== turn) return;
    if (document.hidden) sessionView.timer = setTimeout(again, wait);
    else loadSessionLog();
  }, wait);
}

// The end of the conversation is the session card's bottom, where its message box sits; the panels below the card
// are not part of it. A reader is at the end while that bottom shows on screen, not once they scrolled past it.
function cardBottom() {
  const card = document.querySelector(".session-card");
  return card ? card.getBoundingClientRect().bottom : 0;
}
const atCardEnd = () => cardBottom() > 0 && cardBottom() <= window.innerHeight + 120;
const toCardEnd = (behavior) => window.scrollTo({ top: Math.max(0, window.scrollY + cardBottom() - window.innerHeight), behavior });

// The conversation, oldest first: the user's and agents' messages, the session's words, and its tool calls folded
// into one line per run of calls. A redraw keeps the calls the reader opened open. A reader at the end stays at the
// end; one reading higher up stays there, and "New activity" takes them down.
function drawSessionLog(first) {
  const el = document.getElementById("session-log");
  if (!el) return;
  const session = viewedSession();
  const log = sessionView.log;
  const chats = (ui.state ? ui.state.side_chats || [] : []).filter((chat) => chat.session_id === session.session_id);
  const printed = JSON.stringify([log, chats, session.working_now, sessionView.error]);
  if (!first && printed === sessionView.printed) return;
  const grew = Boolean(sessionView.printed) && printed !== sessionView.printed;
  sessionView.printed = printed;
  const atEnd = atCardEnd();
  // A redraw replaces the tool calls; the one the keyboard was on takes the focus back.
  const focused = el.contains(document.activeElement) ? document.activeElement.closest("details[data-call]")?.dataset.call : null;
  const nodes = [];
  if (sessionView.error) nodes.push(h("p", { class: "note fail-text", text: `Could not read its conversation: ${sessionView.error}` }));
  else if (!log) nodes.push(h("p", { class: "note", text: "Reading its conversation…" }));
  else if (!log.parts.length) nodes.push(h("p", { class: "note", text: "Send a message to start" }));
  else {
    if (log.total > log.parts.length) nodes.push(h("p", { class: "note log-start", text: `Last ${log.parts.length} entries; ${session.provider === "codex" ? "Codex keeps the earlier ones" : "open iTerm2 for earlier messages"}` }));
    let calls = [];
    const flush = () => { if (calls.length) nodes.push(callRun(calls)); calls = []; };
    for (const part of log.parts) {
      if (part.kind === "call") { calls.push(part); continue; }
      flush();
      if (part.kind === "instruction") {
        const you = !part.author || part.author === "You" || part.author === "user";
        nodes.push(h("div", { class: `log-instruction${you ? "" : " other"}` },
          h("span", { class: "log-who", "data-time": part.at || null, text: `${you ? "You" : part.author}${part.at ? `, ${clock(part.at)}` : ""}` }),
          h("div", { class: "log-text", text: clip(part.text, 4000) })));
      } else {
        nodes.push(h("div", { class: "log-says prose", html: part.html || "" }));
        if (part.at) nodes.push(h("p", { class: "log-time", "data-time": part.at, text: clock(part.at) }));
      }
    }
    flush();
  }
  // A quick question with Ask (MSG-11) shows here until its question and answer reach the session, whose own log
  // then holds them.
  for (const chat of chats.filter((each) => each.sent_state !== "sent")) {
    nodes.push(h("div", { class: "log-aside" },
      h("span", { class: "log-who", text: chat.state === "answered"
        ? "Asked with Ask: a copy of the session answered; the session gets both when its current turn ends"
        : "Asked with Ask: a copy of the session answers" }),
      h("p", { class: "log-question", text: chat.question }),
      chat.state === "asking" ? h("p", { class: "note", text: "A copy is answering…" })
        : chat.state === "failed" ? h("p", { class: "note fail-text", text: `No answer: ${chat.error}` })
          : h("div", { class: "prose", html: chat.answer_html || "" })));
  }
  if (session.working_now) {
    const last = log && log.parts.at(-1);
    const now = last && last.kind === "call" ? `: ${last.tool_name} ${clip(callTitle(last), 80)}`.trimEnd() : "";
    nodes.push(h("p", { class: "log-working" }, h("span", { class: "dot working", "aria-hidden": "true" }), `Working${now}`));
  }
  el.replaceChildren(...nodes);
  if (focused) el.querySelector(`details[data-call="${CSS.escape(focused)}"] > summary`)?.focus({ preventScroll: true });
  if (first || atEnd) { toCardEnd("instant"); showNewActivity(false); }
  else if (grew) showNewActivity(true);
}

function showNewActivity(show) {
  const card = document.querySelector(".session-card");
  if (!card) return;
  let button = card.querySelector(".new-activity");
  if (!show) { if (button) button.remove(); return; }
  if (button) return;
  button = h("button", { class: "button small accent new-activity", type: "button",
    onclick: () => { toCardEnd(matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth"); button.remove(); } }, icon("arrow"), "New activity");
  card.append(button);
}

window.addEventListener("scroll", () => { if (sessionView.id && atCardEnd()) showNewActivity(false); }, { passive: true });

// What a tool call did, as a counted noun: "6 reads · 2 edits · 1 command".
const CALL_KINDS = {
  Read: ["read", "reads"], Edit: ["edit", "edits"], MultiEdit: ["edit", "edits"], Write: ["edit", "edits"], NotebookEdit: ["edit", "edits"],
  Bash: ["command", "commands"], Grep: ["search", "searches"], Glob: ["search", "searches"], WebSearch: ["web search", "web searches"],
  WebFetch: ["page read", "pages read"], Agent: ["subagent", "subagents"], Task: ["subagent", "subagents"],
};

// A call's line: its title, or the first line of its input (a command run without a description).
const callTitle = (call) => call.title || (call.text || "").split("\n", 1)[0];

// A tool call's key, so a redraw keeps the reader's open calls open: its id, since two calls can share a second,
// a tool and a title.
const callKey = (call) => call.id;

function keptOpen(key, el) {
  el.dataset.call = key;
  el.open = sessionView.open.has(key);
  el.addEventListener("toggle", () => { if (el.open) sessionView.open.add(key); else sessionView.open.delete(key); });
  return el;
}

// A run of tool calls as one folded line counting what they did, each call inside with its input and result. A
// failed call shows in the line.
function callRun(calls) {
  const counts = new Map();
  for (const call of calls) {
    const [one, many] = CALL_KINDS[call.tool_name] || [call.tool_name, call.tool_name];
    const seen = counts.get(one) || { many, count: 0 };
    seen.count += 1;
    counts.set(one, seen);
  }
  const words = [...counts].map(([one, { many, count }]) => plural(count, one, many)).join(" · ");
  const failed = calls.filter((call) => call.error).length;
  return keptOpen(`run|${callKey(calls[0])}`, h("details", { class: `log-calls${failed ? " failed" : ""}` },
    h("summary", {}, h("span", { class: "log-calls-names", text: words }),
      failed > 0 && h("span", { class: "fail-text", text: `${failed} failed` })),
    calls.map((call) => keptOpen(callKey(call), h("details", { class: `log-call${call.error ? " failed" : ""}` },
      h("summary", {}, h("span", { class: "log-tool", text: call.tool_name }), h("span", { class: "log-call-title", text: callTitle(call) })),
      call.text && h("pre", { class: "log-in", text: call.text }),
      call.result && h("pre", { class: "log-out", text: call.result }))))));
}

// The session view follows the state: its head, its "Working" line, and the message box once Send took its words.
document.addEventListener("cockpit:redraw", () => {
  if (!SIDEBAR || !sessionView.id) return;
  drawSessionHead();
  drawSessionLog(false);
  const step = viewedStep();
  const area = document.querySelector(`#session-composer textarea[data-key="${CSS.escape(sessionKey())}"]`);
  if (!area || area.value !== (ui.drafts.get(sessionKey()) || "") || sessionComposerShape(step) !== sessionView.drawnFor) drawSessionComposer();
});
