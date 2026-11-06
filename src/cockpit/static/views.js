"use strict";

// The Sessions tab (VIEW-3) and the stage counts (VIEW-2, VIEW-6) at the page's top.  Cockpit holds the user's work
// in one place (VIEW-18): Blocked and waiting, Next up, the queue with what is out of the user's hands, then one list
// of all the work and its sessions, which is the board, with Priorities beside it and Patterns, Your time and
// Permissions in a band under it. The Sessions tab, #sessions and the older #board scroll down to the list, and the
// tab reads as the current one while the list fills the view.

(() => {
  const still = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
  // The page with the list of items at its left (VIEW-26) has no list of work: its Sessions tab goes to /classic.
  // This script runs before the body exists, so it asks when it needs to know.
  const sidebar = () => document.body.dataset.layout === "sidebar";

  // The tab that reads as current: Sessions once the list's top is in the upper half of the view.
  function markTabs() {
    const list = document.getElementById("work");
    const top = list && list.getClientRects().length ? list.getBoundingClientRect().top : Infinity;
    // A short page cannot scroll the list up to the header; at the page's end with the list in view, that is Sessions.
    const atEnd = window.scrollY > 0 && window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 2;
    const view = !sidebar() && (top < window.innerHeight / 2 || (atEnd && top < window.innerHeight)) ? "sessions" : "cockpit";
    for (const tab of document.querySelectorAll(".page-tabs a[data-view]")) {
      if (tab.dataset.view === view) tab.setAttribute("aria-current", "page");
      else tab.removeAttribute("aria-current");
    }
  }

  // Scrolls down to the list. On a load with #sessions the list fills only once app.js drew the state, so the
  // scroll waits for its first redraw.
  function showList(smooth) {
    const list = document.getElementById("work");
    if (!list) return;
    if (!list.childElementCount) {
      document.addEventListener("cockpit:redraw", () => showList(false), { once: true });
      return;
    }
    list.scrollIntoView({ behavior: smooth && !still() ? "smooth" : "auto", block: "start" });
    // Focus moves with the view, so Tab goes on into the list rather than along the header's tabs.
    list.focus({ preventScroll: true });
  }
  const LIST_HASHES = new Set(["#sessions", "#board"]);

  // The stage counts.

  // Which holders' lamps are lit on a plate: an agent's or no one's item only while one of its sessions works.
  const LIT = { you: true, people: true, bots: true, merge: true, agents: false, nobody: false };
  // Where work stands (VIEW-6), in plain words: work with no PR is research, a PR's work goes from building to
  // review, dev, staging and prod, and Cockpit's own work, which goes live without dev or staging, has a
  // group of its own. `word` tags an entry with its stage on the plates of the people and the bots.
  const STAGES = [
    { id: "research", label: "Research and questions", mark: "Research", word: "research", hint: "No PR: a question, an analysis or a plan" },
    { id: "building", label: "Building", mark: "Build", word: "building" },
    { id: "review", label: "In review", mark: "Review", word: "in review" },
    { id: "dev", label: "On dev", mark: "Dev", word: "on dev" },
    { id: "staging", label: "On staging", mark: "Staging", word: "on staging" },
    { id: "prod", label: "In prod", mark: "Prod", word: "in prod" },
    { id: "done", label: "Done", mark: "Done", word: "done", hint: "Finished; something on it is still open" },
    { id: "cockpit", label: "Cockpit", mark: "Cockpit", word: "Cockpit", hint: "Changes to Cockpit itself, live as soon as they are built" },
  ];
  // A Cockpit item's tag on its stage's plate: where its work is.
  const COCKPIT_WORDS = {
    planning: "planning", building: "building", review: "in review", dev: "live", staging: "live", prod: "live", done: "done",
  };
  const STAGE_AT = new Map(STAGES.map((stage) => [stage.id, stage]));
  let drawnPrint = "";

  // The sessions that work on the item: its own, and those its open steps name or went to (VIEW-2).
  const workersOf = (item) => [...new Set([...item.sessions, ...item.step_sessions])];

  // Whether one of the item's sessions works: uses Claude now, or waits for a command or a subagent it
  // started. A session at a dialog in its terminal waits for the user, and a session that only waits for a
  // scheduled prompt rests; neither counts.
  function busy(item, sessions) {
    return workersOf(item).some((id) => {
      const session = sessions.get(id);
      if (!session || session.waits_on_dialog) return false;
      // A stopped session's job file can still list work in flight; it counts only while the session runs.
      return session.working_now || (session.alive && session.in_flight.some((kind) => BACKGROUND_WORK.has(kind)));
    });
  }
  // The background work a session waits for (its job file's in-flight kinds): a command or a subagent.
  const BACKGROUND_WORK = new Set(["local_bash", "local_agent"]);

  // How fast the item's lamp pulses while one of its sessions uses Claude now (VIEW-11): 0 when none does,
  // else 1 to 3 by what its sessions' calls cost in the last ten minutes, so expensive models and long
  // contexts count for more than a quick, cheap call.
  function pulseOf(item, sessions) {
    let using = false;
    let recent = 0;
    for (const id of workersOf(item)) {
      const session = sessions.get(id);
      if (!session) continue;
      using = using || sessionState(session) === "working";
      recent += session.spend ? session.spend.recent_usd || 0 : 0;
    }
    if (!using) return 0;
    return recent >= PULSE_FAST_USD ? 3 : recent >= PULSE_MEDIUM_USD ? 2 : 1;
  }

  // How much Claude use the item's sessions took so far, in percent of one account's week (VIEW-11), and
  // the lamp size that shows it: 1 below 5 %, 2 (a tall lamp) below 20 %, 3 (a big one) from 20 %.
  function useOf(item, sessions) {
    return item.sessions.reduce((sum, id) => {
      const session = sessions.get(id);
      return sum + (session && session.spend ? session.spend.week_percent : 0);
    }, 0);
  }

  const sizeOf = (use) => (use >= 20 ? 3 : use >= 5 ? 2 : 1);
  // List-price dollars of Claude use in a session's last ten minutes from which its lamp pulses faster.
  const PULSE_MEDIUM_USD = 2;
  const PULSE_FAST_USD = 4;

  // A `gh:` key names a PR or a GitHub issue; it counts as a PR unless a link shows it is an issue.
  const PULL_URL = /^https:\/\/github\.com\/[^/]+\/[^/]+\/pull\/\d+/;
  const ISSUE_URL = /^https:\/\/github\.com\/[^/]+\/[^/]+\/issues\/\d+/;
  const hasPull = (item) => Boolean(item.pull) || item.links.some((link) => PULL_URL.test(link.url))
    || (item.key.startsWith("gh:") && !item.links.some((link) => ISSUE_URL.test(link.url)));

  function stageOf(item) {
    if (item.project === "cockpit") return "cockpit";
    const early = !item.stage || ["planning", "building", "review"].includes(item.stage);
    if (early && !hasPull(item)) return "research";
    if (!item.stage || item.stage === "planning") return "building";
    return item.stage;
  }

  const named = (item, kind) => item.waiting_on.filter((entry) => entry.kind === kind).map((entry) => entry.who).join(", ");

  // An entry's tag on its stage's plate: on Cockpit's, where its work is; elsewhere who holds it, where its
  // lamp's colour does not say.
  function whoOf(item) {
    if (stageOf(item) === "cockpit") return COCKPIT_WORDS[item.stage] || "";
    if (item.ball === "people") return named(item, "person");
    if (item.ball === "bots") return named(item, "bot");
    if (item.ball === "merge") return named(item, "release");
    return "";
  }

  // What changed since the user last looked (VIEW-7). This browser keeps, for each open item, the stage it was in
  // when the user last looked at that stage's plate (`cockpit-overview-seen-3` in localStorage). A stage with an item
  // new or moved since has its count in the accent, and the item's plate entry says where it was. A plate that stayed
  // open SEEN_AFTER_MS marks its entries seen when it closes. A browser with nothing kept, or without storage, starts
  // from what the page shows. Renamed when the strip by holder went.
  const SEEN_KEY = "cockpit-overview-seen-3";
  const SEEN_AFTER_MS = 800;
  let seen = null;
  let looking = null;

  function readSeen() {
    try {
      const value = JSON.parse(localStorage.getItem(SEEN_KEY) || "null");
      if (value && value.stages) return value;
    } catch {
      // Storage blocked: the page starts from what it shows.
    }
    return null;
  }

  // Keeps the record of open items only, so a closed item leaves it.
  function keepSeen(entries) {
    const open = new Set(entries.map((entry) => String(entry.id)));
    for (const id of Object.keys(seen.stages)) if (!open.has(id)) delete seen.stages[id];
    try {
      localStorage.setItem(SEEN_KEY, JSON.stringify(seen));
    } catch {
      // As above: the record lasts as long as the page.
    }
  }

  // Where an item was when the user last looked: "new", "was <where>", or "" when it has not moved.
  function movedFrom(record, id, now, words) {
    if (!(id in record)) return "new";
    return record[id] === now ? "" : `was ${words(record[id])}`;
  }

  // Marks a plate's entries seen at their stage as it is now.
  function markSeen(entries, all) {
    for (const entry of entries) seen.stages[entry.id] = entry.stage;
    keepSeen(all);
    drawnPrint = "";
    drawLights();
  }

  // A plate counts as read once it stayed open SEEN_AFTER_MS: from the pointer coming onto its group to
  // the pointer leaving, or from the focus coming in to the focus leaving (a tap on a phone, where the
  // touch's own pointer events end at once and are left out).
  function watchLooks(cell, id, onSeen) {
    const start = () => {
      if (!looking || looking.id !== id) looking = { id, at: Date.now() };
    };
    const end = () => {
      if (!looking || looking.id !== id) return;
      const read = Date.now() - looking.at >= SEEN_AFTER_MS;
      looking = null;
      if (read) onSeen();
    };
    cell.addEventListener("pointerenter", (event) => { if (event.pointerType !== "touch") start(); });
    cell.addEventListener("pointerleave", (event) => { if (event.pointerType !== "touch") end(); });
    cell.addEventListener("focusin", start);
    cell.addEventListener("focusout", (event) => { if (!cell.contains(event.relatedTarget)) end(); });
  }

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  // The plate's header: the stage's name and count, and what the stage holds where its name does not say.
  function plateHead(group) {
    const head = el("div", "light-head");
    const naming = el("span", "light-naming");
    naming.append(el("span", "light-name", group.stage.label));
    if (group.stage.hint) naming.append(el("span", "light-detail", group.stage.hint));
    head.append(naming, el("b", "light-count", String(group.entries.length)));
    return head;
  }

  // A lamp's size says how much Claude use its item took so far; it pulses while one of the item's
  // sessions uses Claude now (VIEW-11).
  const lampSize = (entry) => `${entry.size > 1 ? ` size-${entry.size}` : ""}${entry.pulse ? ` pulse-${entry.pulse}` : ""}`;

  function entryButton(entry) {
    const moved = entry.movedStage;
    const button = el("button", `light-item${entry.lit ? "" : " dim"}${moved ? " moved" : ""}${entry.stalled ? " stalled" : ""}`);
    button.type = "button";
    const use = [entry.pulse ? "using Claude now" : entry.working && "waiting for a command it ran", entry.use && `used ${entry.use}`]
      .filter(Boolean).join(", ");
    const stalled = entry.stalledAt ? `stalled ${age(entry.stalledAt)}` : "";
    button.setAttribute("aria-label", `${entry.title}${stalled ? `, ${stalled}` : ""}${moved ? `, ${moved}` : ""}${use ? `, ${use}` : ""}: open it`);
    const when = el("span", "light-age", entry.age);
    if (entry.at) when.dataset.time = entry.at;
    button.append(el("span", `lamp ball-${entry.ball}${entry.stalled ? " stalled" : ""}${lampSize(entry)}`), el("span", "light-title", entry.title));
    // A stage plate leaves the Cockpit tag out: Cockpit work is its own stage (VIEW-6).
    if (stalled) button.append(el("span", `light-stalled${entry.stalled ? " raised" : ""}`, stalled));
    if (moved) button.append(el("span", "light-moved", moved));
    if (entry.who) button.append(el("span", "entry-tag", entry.who));
    button.append(when);
    button.addEventListener("click", () => openInWork(entry.id, entry.group));
    return button;
  }

  // A plate drops under each stage: its name, what it holds where the name does not say, and its count as its
  // header, then each item's lamp, title, tags and how long since it changed.
  function plate(group) {
    const list = el("div", "light-list");
    const part = el("div", "holder");
    for (const entry of group.entries) part.append(entryButton(entry));
    list.append(plateHead(group), part);
    return list;
  }

  function drawStrip(groups, all, sum) {
    const panel = el("div", "lights stages");
    for (const group of groups) {
      const moved = group.entries.filter((entry) => entry.movedStage).length;
      const cell = el("div", `light-group stage-group stage-${group.stage.id}${moved ? " moved" : ""}`);
      cell.dataset.group = group.id;
      cell.tabIndex = 0;
      cell.setAttribute("role", "group");
      cell.setAttribute("aria-label", `${group.stage.label}: ${group.entries.length}${moved ? `, ${moved} new or moved since you looked` : ""}`);
      const name = el("span", "stage-mark", group.stage.mark);
      const count = el("b", "stage-count", String(group.entries.length));
      name.setAttribute("aria-hidden", "true");
      count.setAttribute("aria-hidden", "true");
      cell.append(name, count, plate(group));
      watchLooks(cell, group.id, () => markSeen(group.entries, all));
      panel.append(cell);
    }
    panel.append(sum);
    return panel;
  }

  function drawLights() {
    const box = document.getElementById("overview");
    const state = typeof ui === "object" && ui.state;
    if (!box || !state) return;
    const sessions = new Map(state.sessions.map((session) => [session.session_id, session]));
    const queued = new Set(state.queue.map((step) => step.item_id));
    const order = BALLS.map(([ball]) => ball).filter((ball) => ball !== "done");
    // An item the user just completed or closed has left already, while the server confirms (CONTROL-15).
    const open = state.items.filter((item) => order.includes(item.ball) && !ui.finishing.has(item.id));
    if (seen === null) {
      seen = readSeen();
      if (seen === null) {
        seen = { stages: {} };
        for (const item of open) seen.stages[item.id] = stageOf(item);
        keepSeen(open);
      }
    }
    const entries = open.map((item) => {
      const stage = stageOf(item);
      const working = busy(item, sessions);
      const pulse = working ? pulseOf(item, sessions) : 0;
      const use = useOf(item, sessions);
      return {
        id: item.id,
        title: item.title,
        ball: item.ball,
        group: item.group,
        stage,
        lit: item.ball === "you" ? queued.has(item.id) : LIT[item.ball] || (item.ball !== "nobody" && working),
        // Urgent or high work whose agents stopped half an hour ago or more (SPEC SWEEP-20): a red lamp.
        // Every stalled item says so; urgent and high ones also turn their lamp red.
        stalled: Boolean(item.stalled_since) && ["urgent", "high"].includes(item.priority),
        stalledAt: item.stalled_since,
        working,
        pulse,
        size: sizeOf(use),
        use: use >= 1 ? `${Math.round(use)}% of a week` : use > 0 ? "under 1% of a week" : "",
        age: item.updated_at ? age(item.updated_at) : "",
        at: item.updated_at,
        who: whoOf(item),
        movedStage: movedFrom(seen.stages, item.id, stage, (was) => (STAGE_AT.has(was) ? STAGE_AT.get(was).word : was)),
      };
    });
    // On a stage the lamps stand by who holds them, in All work's order, the lit ones first; each holder keeps the
    // newest first.
    const byLit = (one, other) => other.lit - one.lit;
    const stages = STAGES.map((stage) => {
      const mine = entries.filter((entry) => entry.stage === stage.id)
        .sort((one, other) => order.indexOf(one.ball) - order.indexOf(other.ball) || byLit(one, other));
      return { id: `stage:${stage.id}`, stage, entries: mine };
    }).filter((group) => group.entries.length > 0);
    const print = JSON.stringify(stages);
    if (print === drawnPrint && box.firstChild) return;
    drawnPrint = print;
    const total = entries.length;
    const sum = el("span", "lights-sum", total ? `${total} open` : "Nothing open");
    // A redraw keeps the focus where it was: on a stage, or on an item in its plate.
    const focused = box.contains(document.activeElement) ? document.activeElement : null;
    const groupId = focused && focused.closest(".light-group") ? focused.closest(".light-group").dataset.group : null;
    const itemAt = focused && focused.matches(".light-item") ? [...focused.closest(".light-list").querySelectorAll(".light-item")].indexOf(focused) : -1;
    box.replaceChildren(drawStrip(stages, entries, sum));
    layOut();
    if (groupId) {
      const cell = box.querySelector(`.light-group[data-group="${groupId}"]`);
      const items = cell ? cell.querySelectorAll(".light-item") : [];
      const target = itemAt >= 0 && items.length ? items[Math.min(itemAt, items.length - 1)] : cell;
      if (target) target.focus();
    }
  }

  // A strip wider than the screen first drops its sum and narrows its cells, then wraps, so every stage stays in
  // view. The plates are left out of the measure: placeLists moves them.
  function fitStrips() {
    for (const strip of document.querySelectorAll("#overview .lights")) {
      strip.classList.remove("tight", "wrap");
      for (const step of ["tight", "wrap"]) {
        const right = strip.getBoundingClientRect().right;
        if ([...strip.children].every((part) => part.getBoundingClientRect().right <= right + 0.5)) break;
        strip.classList.add(step);
      }
    }
  }

  function layOut() {
    fitStrips();
    placeLists();
  }

  // Each group's plate drops from the group's left edge, moved left as far as it needs to end inside
  // its strip, so no plate reaches past the screen's edge, shown or hidden. All reads come before any
  // write, so the page is laid out once, not once per plate.
  function placeLists() {
    const lists = [...document.querySelectorAll("#overview .light-list")];
    for (const list of lists) list.style.removeProperty("left");
    const shifts = lists.map((list) => {
      const bounds = list.closest(".lights").getBoundingClientRect();
      const left = list.parentElement.getBoundingClientRect().left;
      const over = left + list.getBoundingClientRect().width - bounds.right;
      return over > 0 ? Math.min(Math.ceil(over), Math.floor(left - bounds.left)) : 0;
    });
    lists.forEach((list, at) => {
      if (shifts[at]) list.style.left = `${-shifts[at]}px`;
    });
  }

  // Opens an item's row in All work: its group opens (Matters most, or the one for who has the next move), then
  // its row, which comes to the middle of the screen.
  // Returns what it showed, or null.
  async function openInWork(itemId, inGroup) {
    // An item search's filter hides shows again (app.js, VIEW-21).
    window.revealItem?.(itemId);
    // Work that waits for the user has no row; app.js opens its step instead (VIEW-18).
    if (!document.querySelector(`#work .row[data-item="${itemId}"]`)) {
      const step = window.openItemStep(itemId);
      if (step) return step;
    }
    const listed = document.querySelector(`#work .row[data-item="${itemId}"]`);
    const group = listed ? listed.closest("details.group") : document.querySelector(`#work details.group.${inGroup}`);
    if (group && !group.open) {
      group.open = true;
      // app.js records the open group on its toggle event, which fires after this task.
      await new Promise((resolve) => setTimeout(resolve));
    }
    const row = document.querySelector(`#work .row[data-item="${itemId}"]`);
    if (row && !row.classList.contains("open")) row.querySelector(".row-head").click();
    const shown = document.querySelector(`#work .row[data-item="${itemId}"]`);
    if (shown) shown.scrollIntoView({ block: "center" });
    return shown;
  }
  // app.js's Pinned list opens its items here too.
  window.openInWork = openInWork;

  function start() {
    for (const tab of document.querySelectorAll(".page-tabs a[data-view]")) {
      tab.addEventListener("click", (event) => {
        if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        // Each page keeps its own tab in place: / its Cockpit tab, /classic its Sessions tab. The other tab is a
        // link to the other page.
        if (tab.dataset.view !== (sidebar() ? "cockpit" : "sessions")) return;
        event.preventDefault();
        if (tab.dataset.view === "sessions") {
          if (location.hash !== "#sessions") history.pushState(null, "", "#sessions");
          showList(true);
        } else {
          if (location.hash) history.pushState(null, "", `${location.pathname}${location.search}`);
          window.scrollTo({ top: 0, behavior: still() ? "auto" : "smooth" });
        }
      });
    }
    // The page with the list of items at its left sends a link to the list of work on to /classic, which has it.
    const toList = (smooth) => {
      if (!LIST_HASHES.has(location.hash)) return;
      if (sidebar()) location.replace(`/classic${location.hash}`);
      else showList(smooth);
    };
    toList(false);
    // A hash changes at once and its hashchange comes later: on a busy machine after keys typed since, such as G S
    // and then Ctrl+K to open a session's message box. So G S (keyboard.js) shows the list as it changes the hash,
    // and its hashchange, one per G S and in their order, finds its address shown; a hashchange to an address the
    // page has left meanwhile is followed by the one to where it went, or the page left by Find's replaceState.
    // Either way the late hashchange leaves focus where the user put it.
    const shown = [];
    window.showWorkList = () => {
      if (sidebar()) return false;
      shown.push(location.href);
      showList(false);
      return true;
    };
    window.addEventListener("hashchange", (event) => {
      const handled = shown[0] === event.newURL;
      if (handled) shown.shift();
      if (!handled && event.newURL === location.href) toList(true);
    });
    let marking = false;
    window.addEventListener("scroll", () => {
      if (marking) return;
      marking = true;
      requestAnimationFrame(() => { marking = false; markTabs(); });
    }, { passive: true });
    document.addEventListener("cockpit:redraw", markTabs);
    markTabs();

    // The lights follow each of app.js's redraws.
    if (document.getElementById("overview")) {
      document.addEventListener("cockpit:redraw", drawLights);
      drawLights();
      window.addEventListener("resize", layOut);
      document.fonts.ready.then(layOut);
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
