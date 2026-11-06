"use strict";

// Shared keyboard layer for every Cockpit page. Page scripts supply their existing
// actions; this module owns navigation, focus, the command menu and shortcut help.
(() => {
  const mac = /Mac|iPhone|iPad/.test(navigator.platform);
  const modifier = mac ? "⌘" : "Ctrl";
  const editing = "input, textarea, select, [contenteditable]:not([contenteditable='false']), [role='textbox']";
  // Each place with its page's icon, as the rail shows it (sidebar.js PAGE_ICONS).
  const destinations = [
    ["home", "Cockpit", "/", "h", "home next up inbox", "step"],
    ["sessions", "Sessions", "/classic#sessions", "s", "all work board projects items agents conversations in progress done", "terminal"],
    ["messages", "Messages", "/classic#board/messages", "m", "replies delivery", "mail"],
    ["priorities", "Priorities", "/#home/goals", "p", "goals", "target"],
    ["briefing", "Briefing", "/briefing", "r", "morning team", "log"],
    ["pages", "Pages", "/pages", "a", "html reports charts plans shared", "chart"],
    ["usage", "Usage", "/usage", "u", "tokens costs limits", "meter"],
    ["spec", "Spec", "/spec", "d", "documentation requirements", "doc"],
    ["skills", "Skills", "/skills", "k", "instructions", "bolt"],
  ];
  const SHOW_KEY = "cockpit.showSection";
  // The Cockpit page with the list of items at its left (VIEW-26), and the page from before it that still holds the
  // list of sessions.
  const COCKPIT_PAGES = new Set(["/", "/classic"]);
  // The routes into the list of work, which only /classic has.
  const WORK_ROUTES = new Set(["sessions", "messages", "failed", "work"]);
  let bridge = {};
  let cachedState = null;
  let cachedAt = 0;
  let fetching = null;
  let selected = null;
  let selectedNode = null;
  let prefixTimer = null;
  let previousFocus = null;
  let mode = "commands";
  let query = "";
  let index = 0;
  let results = [];
  let routeHandled = "";
  let loadError = false;

  const el = (tag, attrs = {}, text = "") => {
    const node = document.createElement(tag);
    for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, value);
    node.textContent = text;
    return node;
  };
  const keycap = (key) => el("kbd", {}, key);
  // A row inside a folded group keeps its box in Chrome; checkVisibility sees the fold.
  // A part that waits for the page to be current is inert (app.js, UI-60): its rows take no keys.
  const visible = (node) => Boolean(node?.isConnected && node.checkVisibility({ visibilityProperty: true }) && !node.closest("[inert]"));
  const busy = () => Boolean(bridge.busy?.() || document.body.classList.contains("picking")
    || visible(document.getElementById("pick-box")));
  const identity = (node) => {
    // An item can have a line in the sidebar and a row in a search's matches below the card.
    if (node.closest("#sidebar")) return node.dataset.warning ? `sidebar-warning:${node.dataset.warning}` : `sidebar:${node.dataset.item}`;
    for (const attr of ["data-step", "data-item", "data-session", "data-block", "data-page", "id", "href"]) {
      if (node.hasAttribute(attr)) return `${attr}:${node.getAttribute(attr)}`;
    }
    return null;
  };
  // With the sidebar the main column always shows the picked row's task, so it takes no mark of being picked:
  // J/K and a click move through the rows, never onto the card, a conversation or their column.
  const shownCard = (node) => document.body.dataset.layout === "sidebar" && node.matches("#next, #next > .next-card, #session-view, #session-view > .session-card");
  // R replies on the highlighted row; with the sidebar and nothing highlighted, on the task the card shows.
  const replyTarget = () => {
    if (current() || document.body.dataset.layout !== "sidebar") return current();
    const card = document.querySelector("#next > .next-card");
    return visible(card) ? card : null;
  };
  const candidates = () => {
    const rows = [...document.querySelectorAll(
      "#sidebar .sidebar-list > li[data-item], #sidebar .sidebar-list > li[data-warning], #next [data-step], #queue .row[data-step], #work .row[data-item], #work .row[data-session], "
      + "#doc [data-block], #briefing section[id], #days a[href], #pages-list a[data-page]",
    )].filter((node) => visible(node) && !node.closest(".motion-ghost, .left") && !shownCard(node));
    return rows.length ? rows : [...document.querySelectorAll("main section[id], main article[id]")]
      .filter((node) => visible(node) && !shownCard(node));
  };

  function current() {
    if (!selected) return null;
    return candidates().find((node) => identity(node) === selected) || null;
  }

  function clearSelection() {
    selectedNode?.classList.remove("keyboard-current");
    selected = selectedNode = null;
  }

  function highlight(node, focus = true) {
    selectedNode?.classList.remove("keyboard-current");
    selectedNode = node;
    selected = identity(node);
    node.classList.add("keyboard-current");
    if (!node.hasAttribute("tabindex")) node.setAttribute("tabindex", "-1");
    if (focus) {
      node.focus({ preventScroll: true });
      node.scrollIntoView({ block: "nearest", behavior: "instant" });
    }
  }

  function move(delta) {
    const rows = candidates();
    if (!rows.length) return;
    const at = rows.findIndex((node) => identity(node) === selected);
    // With no row selected, J starts at the first row on screen and K at the last, so the page stays where the
    // user is: lower on the page in the list of work, J starts there (VIEW-3).
    const top = document.querySelector("header")?.getBoundingClientRect().bottom || 0;
    const onScreen = (node) => { const box = node.getBoundingClientRect(); return box.bottom > top && box.top < window.innerHeight; };
    const shown = delta > 0 ? rows.findIndex(onScreen) : rows.findLastIndex(onScreen);
    const start = shown >= 0 ? shown : delta > 0 ? 0 : rows.length - 1;
    const next = at < 0 ? start : Math.max(0, Math.min(rows.length - 1, at + delta));
    highlight(rows[next]);
  }

  function openSelected() {
    const node = current();
    if (!node) return;
    const button = node.querySelector("[role='button'][aria-expanded], .row-head, .side-row, summary, a[href]");
    if (button) button.click();
    else if (node.matches("a[href]")) node.click();
    else node.querySelector("textarea, input, button")?.focus();
  }

  function resetPrefix() {
    clearTimeout(prefixTimer);
    prefixTimer = null;
    hint.hidden = true;
  }

  function navigate(url) {
    resetPrefix();
    clearSelection();
    const target = new URL(url, location.origin);
    routeHandled = "";
    // Going to a section shows it, also one the user folded; a route a load restores keeps the fold. From
    // another page the ask waits in this tab's storage for the Cockpit page to load.
    const section = COCKPIT_PAGES.has(target.pathname) ? target.hash.match(/^#(?:board|home)\/(\w+)$/) : null;
    if (target.pathname === location.pathname && target.search === location.search) {
      if (section) bridge.showSection?.(section[1]);
      const changing = target.hash !== location.hash;
      if (changing) location.hash = target.hash;
      else followRoute();
      // Sessions is the list of work lower on the page (VIEW-3): G S scrolls down to it, and on /classic views.js
      // shows and focuses it at once rather than at the hashchange, which can come after the keys typed since.
      if (target.hash === "#sessions") {
        if (!(changing && window.showWorkList?.())) document.getElementById("work")?.scrollIntoView({ block: "start" });
      } else if (!target.hash) window.scrollTo(0, 0);
    } else {
      if (section) try { sessionStorage.setItem(SHOW_KEY, section[1]); } catch { /* blocked storage: it shows as left */ }
      location.assign(target.href);
    }
  }

  // The section a navigation from another page asked to show, once.
  function takeShowAsk() {
    try {
      const asked = sessionStorage.getItem(SHOW_KEY);
      sessionStorage.removeItem(SHOW_KEY);
      return asked;
    } catch {
      return null;
    }
  }

  function followRoute() {
    if (!COCKPIT_PAGES.has(location.pathname) || routeHandled === location.href) return;
    const match = location.hash.match(/^#(?:board|home)\/(sessions|messages|failed|goals|work|patterns|time|permissions|item\/\d+|session\/[0-9a-f-]+)$/);
    if (!match) return;
    const route = match[1];
    // The page with the list of items at its left (VIEW-26) has no list of work: its routes go on to /classic.
    if (WORK_ROUTES.has(route) && document.body.dataset.layout === "sidebar") {
      location.replace(`/classic${location.hash}`);
      return;
    }
    if (route.startsWith("item/")) {
      if (!bridge.state?.() || !bridge.openItem) return;
      routeHandled = location.href;
      bridge.openItem(Number(route.slice(5)));
    } else if (route.startsWith("session/")) {
      if (!bridge.state?.() || !bridge.openSession) return;
      routeHandled = location.href;
      queueMicrotask(() => bridge.openSession(route.slice(8)));
    } else {
      if (!bridge.state?.()) return;
      routeHandled = location.href;
      // Messages is the list of work sorted by Your latest message, so it always sorts the list.
      if (takeShowAsk() === route || route === "messages" || route === "failed") bridge.showSection?.(route);
      // A task lets the page finish this hashchange before the scroll. Sessions is the list of work (VIEW-3).
      setTimeout(() => document.getElementById(route === "sessions" || route === "messages" ? "work" : route)?.scrollIntoView({ block: "start" }));
    }
  }

  function commands() {
    const state = bridge.state?.() || cachedState;
    const liveItems = (state?.items || []).filter((item) => !item.archived);
    // A to-do's summary holds the user's own words, so either finds the work, and the sessions on it (SESSION-3
    const itemWords = (item) => `${item.title} ${item.summary || ""} ${item.agent_summary || ""}`;
    const openWork = liveItems.filter((item) => item.stage !== "done");
    // A session works on an item it is listed on or that one of the item's steps goes to.
    const worksOn = (item, sessionId) => item.sessions.includes(sessionId) || item.step_sessions.includes(sessionId);
    return [
      ...destinations.map(([id, title, url, key, words, mark]) => ({
        id, title, subtitle: "Go to", mark, words, keys: ["G", key.toUpperCase()], run: () => navigate(url),
      })),
      ...(bridge.actions?.() || []).map((action) => ({ subtitle: "Action", mark: "bolt", ...action })),
      { id: "help", title: "Keyboard shortcuts", subtitle: "Help", mark: "info", words: "keys bindings shortcuts", keys: ["?"], run: showHelp },
      ...liveItems.map((item) => ({
        id: `item:${item.id}`, title: item.title, subtitle: "Work", project: item.project, words: `${item.key} ${itemWords(item)}`,
        run: () => {
          if (bridge.openItem) bridge.openItem(item.id);
          else navigate(`/#home/item/${item.id}`);
        },
      })),
      ...(state?.sessions || []).map((session) => ({
        id: `session:${session.session_id}`, title: sessionLabel(session), subtitle: "Session", state: sessionState(session),
        words: `${session.provider || "claude"} ${session.cwd || ""} ${session.session_id}`,
        context: openWork.filter((item) => worksOn(item, session.session_id)).map(itemWords).join(" "),
        items: openWork.filter((item) => worksOn(item, session.session_id)).map((item) => `item:${item.id}`),
        run: () => {
          if (bridge.openSession) bridge.openSession(session.session_id);
          else navigate(`/#board/session/${session.session_id}`);
        },
      })),
    ];
  }

  // A session's name as the rail's session rows show it (app.js sessionLabel): its name, else the repo it runs in.
  function sessionLabel(session) {
    if (session.name) return session.name;
    const repo = (session.cwd || "").replace(/\/\.claude\/worktrees\/.*$/, "").split("/").filter(Boolean).pop();
    return repo ? `a session in ${repo}` : "a session";
  }

  // The state its dot shows, as in the rail (app.js sessionState): a working session waiting on a dialog is
  // "blocked", one whose turn ended "idle"; a session the state lists without one is "idle" while alive.
  function sessionState(session) {
    if (!session.state) return session.alive ? "idle" : "stopped";
    if (session.state !== "working" || session.working_now) return session.state;
    return session.tempo === "blocked" ? "blocked" : "idle";
  }

  // Prefer title matches and word starts, then allow a subsequence for short fuzzy queries. `withContext` also
  // searches a session's `context`, the words of the work it is on.
  function score(command, term, withContext) {
    const title = command.title.toLowerCase();
    const words = `${title} ${command.subtitle} ${command.words || ""} ${withContext ? command.context : ""}`.toLowerCase();
    const tokens = term.toLowerCase().trim().split(/\s+/).filter(Boolean);
    let total = 0;
    for (const token of tokens) {
      if (title.startsWith(token)) total += 100;
      else if (title.includes(token)) total += 60;
      else if (words.includes(token)) total += 30;
      else {
        const matches = words.split(/\s+/).some((word) => {
          let at = 0;
          for (const char of token) {
            at = word.indexOf(char, at);
            if (at < 0) return false;
            at += 1;
          }
          return true;
        });
        if (!matches) return -1;
        total += 1;
      }
    }
    return total;
  }

  function choose() {
    const command = results[index];
    if (!command) return;
    close();
    command.run();
  }

  function selectResult(next) {
    if (!results.length) return;
    index = (next + results.length) % results.length;
    for (const [at, option] of [...list.querySelectorAll("[role='option']")].entries()) {
      option.setAttribute("aria-selected", String(at === index));
    }
    input.setAttribute("aria-activedescendant", `cockpit-command-${index}`);
    document.getElementById(`cockpit-command-${index}`)?.scrollIntoView({ block: "nearest" });
  }

  // Search's filter on the page (VIEW-21): the first result, once the words find work or a session, shows the
  // matches on Cockpit's page itself, filtered; it comes second when a place or an action starts with the words.
  // With Include closed on, the closed work the server finds for the words lists too, after the rest.
  // `counts` is what the page's filter shows for the words, when this is the main page.
  function findCommand(term, counts) {
    const text = term.trim();
    const closed = closedSwitch.checked;
    const kind = closed ? "Filter, closed too" : "Filter";
    const many = (count, one) => `${count} ${one}${count === 1 ? "" : "s"}`;
    return {
      id: "find", title: "Show matches on the page", mark: "pick",
      subtitle: counts ? `${kind}: ${many(counts.tasks, "task")}, ${many(counts.sessions, "session")}` : kind,
      run: () => {
        if (bridge.find) bridge.find(text, closed, null);
        else navigate(`/#find${closed ? "-closed" : ""}/${encodeURIComponent(text)}`);
      },
    };
  }

  function closedCommands() {
    if (!closedSwitch.checked || closedFound.query !== query.trim()) return [];
    return closedFound.items.map((item) => ({
      id: `closed:${item.id}`, title: item.title, subtitle: item.action === "archived" ? "Closed" : "Done", project: item.project, found: true,
      run: () => {
        const route = `${encodeURIComponent(query.trim())}/${item.id}`;
        if (bridge.find) bridge.find(query.trim(), true, item.id);
        else navigate(`/#find-closed/${route}`);
      },
    }));
  }

  let closedFound = { query: "", items: [] };
  let closedTimer = null;
  function loadClosed() {
    clearTimeout(closedTimer);
    const term = query.trim();
    if (!closedSwitch.checked || !term || closedFound.query === term) return;
    closedTimer = setTimeout(async () => {
      try {
        const response = await fetch(`/api/items/closed?q=${encodeURIComponent(term)}`, { headers: { Accept: "application/json" }, signal: AbortSignal.timeout(5000) });
        if (!response.ok) throw new Error(`Cockpit answered ${response.status}`);
        closedFound = { query: term, items: (await response.json()).items };
      } catch (error) {
        status.textContent = `Closed work could not be searched: ${error.message}`;
        return;
      }
      if (dialog.open && mode === "commands" && query.trim() === term) paint(true);
    }, 200);
  }

  function paint(keepSelection = false) {
    const keep = keepSelection ? results[index]?.id : null;
    const scored = [...commands(), ...closedCommands()].map((command, order) => ({ command, order, score: score(command, query, false) }));
    // The server matched closed work on words this menu does not hold (its summary): it lists after the rest.
    for (const entry of scored) if (entry.command.found) entry.score = 0;
    const byId = new Map(scored.map((entry) => [entry.command.id, entry.score]));
    // A session found only through the words of its work ranks just after that work, so Enter opens the work.
    for (const entry of scored) {
      if (entry.score >= 0 || !entry.command.context) continue;
      const full = score(entry.command, query, true);
      if (full < 0) continue;
      const best = Math.max(-1, ...entry.command.items.map((id) => byId.get(id)));
      entry.score = best >= 0 ? Math.min(full, best - 0.5) : full;
    }
    const found = scored.filter((entry) => entry.score >= 0).sort((a, b) => b.score - a.score || a.order - b.order);
    // A session lists right under the found work it is on, the best-ranked one when it is on several, as the rail
    // lists an item's sessions; the group takes the place of its best-ranked member.
    const ranked = found.map((entry) => entry.command);
    const workAt = new Map(ranked.map((command, at) => [command.id, at]).filter(([, at]) => ranked[at].subtitle === "Work"));
    const parent = new Map();
    for (const command of ranked) {
      const ats = (command.items || []).filter((id) => workAt.has(id)).map((id) => workAt.get(id));
      if (command.subtitle === "Session" && ats.length) parent.set(command, ranked[Math.min(...ats)]);
    }
    const grouped = new Set();
    for (const command of ranked) {
      const work = parent.get(command) || (command.subtitle === "Work" ? command : null);
      if (!work) { grouped.add(command); continue; }
      grouped.add(work);
      for (const session of ranked) if (parent.get(session) === work) grouped.add(session);
    }
    const listed = [...grouped];
    results = listed.slice(0, 60);
    const term = query.trim().toLowerCase();
    // Showing the matches on the page is there when the words find work or sessions, here or in the page's filter,
    // which counts them. It comes first, after a page or action the words name, or the one task or session they find.
    const counts = term ? bridge.findCount?.(term) : null;
    const work = results.some((command) => ["Work", "Session", "Closed", "Done"].includes(command.subtitle));
    if (term && (work || (counts && counts.tasks + counts.sessions > 0))) {
      const top = results[0]?.subtitle;
      const named = ["Go to", "Action"].includes(top) && results[0].title.toLowerCase().startsWith(term);
      // Counted before the first 60 are cut, so a task with many sessions does not hide the other tasks found.
      const onlyOne = ["Work", "Session"].includes(top) && listed.filter((command) => command.subtitle === top).length === 1;
      // After the one task found, below its sessions, which stay right under it.
      let at = named || onlyOne ? 1 : 0;
      while (onlyOne && parent.get(results[at]) === results[0]) at += 1;
      results.splice(at, 0, findCommand(query, counts));
    }
    index = Math.max(0, results.findIndex((command) => command.id === keep));
    list.replaceChildren();
    for (const [at, command] of results.entries()) {
      // One line per result: a mark that says what it is, as the rail draws it, then its title.
      // Work leads with the Cockpit key on Cockpit's own work, as its row in the list does, and a
      // plain grey mark otherwise; a session leads with its state's dot, indented under the work it is on. Places
      // and actions lead with their icon and end with their keys; the other kinds end with a short word only where
      // the mark leaves it open.
      const kind = command.subtitle === "Work" ? "work" : command.subtitle === "Session" ? "session" : ["Closed", "Done"].includes(command.subtitle) ? "closed" : "other";
      const under = parent.has(command);
      const option = el("li", {
        role: "option", id: `cockpit-command-${at}`, "aria-selected": String(at === index),
        class: `command-${kind}${under ? " under" : ""}`, "aria-label": `${command.subtitle}: ${command.title}`,
      });
      const label = el("span", { class: "command-label" });
      // Cockpit's own work carries the Cockpit tag (SPEC UI-43) in the mark's place.
      const cockpit = Icons.cockpitTag(command.project);
      const mark = kind === "session" ? el("span", { class: `dot ${command.state}` })
        : cockpit ? cockpit
        : kind === "other" && command.mark ? Icons.icon(command.mark) : el("span", { class: "command-mark" });
      if (mark !== cockpit) mark.setAttribute("aria-hidden", "true");
      label.append(mark, el("span", { class: "command-title" }, command.title));
      option.append(label);
      if (command.keys) {
        const keys = el("span", { class: "command-keys", "aria-hidden": "true" });
        keys.append(...command.keys.map(keycap));
        option.append(keys);
      } else if (kind === "closed" || kind === "other") {
        option.append(el("small", { class: "command-kind", "aria-hidden": "true" }, command.subtitle));
      }
      option.addEventListener("mousedown", (event) => event.preventDefault());
      option.addEventListener("click", () => { index = at; choose(); });
      list.append(option);
    }
    if (!results.length) list.append(el("li", { class: "command-empty", role: "presentation" }, "No matching commands, work, or sessions."));
    input.setAttribute("aria-expanded", "true");
    if (results.length) input.setAttribute("aria-activedescendant", `cockpit-command-${index}`);
    else input.removeAttribute("aria-activedescendant");
    status.textContent = fetching ? "Loading work and sessions…" : loadError ? "Work and sessions could not be loaded. Page navigation is available."
      : `${found.length} result${found.length === 1 ? "" : "s"}${found.length > 60 ? " · showing the first 60" : ""}`;
  }

  async function loadState() {
    if (bridge.state?.() || (cachedState && Date.now() - cachedAt < 30000)) return;
    if (!fetching) fetching = (async () => {
      const response = await fetch("/api/state", { headers: { Accept: "application/json" }, signal: AbortSignal.timeout(5000) });
      if (!response.ok) throw new Error("Could not load state");
      cachedState = await response.json();
      cachedAt = Date.now();
    })();
    status.textContent = "Loading work and sessions…";
    try { await fetching; loadError = false; } catch { loadError = true; } finally { fetching = null; }
    if (dialog.open && mode === "commands") paint(true);
  }

  function otherModal() {
    return [...document.querySelectorAll("dialog[open], [role='dialog']:not([hidden])")].some((node) => node !== dialog);
  }

  function open() {
    if (otherModal() || busy()) return;
    if (dialog.open && mode === "commands") { close(); return; }
    resetPrefix();
    if (!dialog.open) previousFocus = document.activeElement;
    mode = "commands";
    help.hidden = true;
    search.hidden = list.hidden = false;
    dialog.setAttribute("aria-label", "Command menu");
    title.textContent = "Jump to anything";
    query = input.value = "";
    try { closedSwitch.checked = JSON.parse(localStorage.getItem("cockpit:find-closed")) === true; } catch { /* blocked storage: off */ }
    paint();
    if (!dialog.open) dialog.showModal();
    input.focus();
    loadState();
  }

  function close() {
    if (!dialog.open) return;
    dialog.close();
    resetPrefix();
    focusBack(previousFocus);
    previousFocus = null;
  }

  // A redraw while the menu was open swaps a reply box for a new one under the same draft key; focus goes to
  // that one, so typing on stays in the step.
  function focusBack(was) {
    if (was?.isConnected) { was.focus({ preventScroll: true }); return; }
    const key = was?.dataset?.key;
    const box = key && document.querySelector(`textarea[data-key="${CSS.escape(key)}"]`);
    if (!box) return;
    if (window.CockpitEditor) window.CockpitEditor.focus(box, { preventScroll: true });
    else box.focus({ preventScroll: true });
    if (!box.hidden && typeof was.selectionStart === "number") box.setSelectionRange(was.selectionStart, was.selectionEnd);
  }

  function showHelp() {
    if (otherModal() || busy()) return;
    resetPrefix();
    if (!dialog.open) previousFocus = document.activeElement;
    mode = "help";
    search.hidden = list.hidden = true;
    help.hidden = false;
    dialog.setAttribute("aria-label", "Keyboard shortcuts");
    title.textContent = "Keyboard shortcuts";
    status.textContent = "Single-key shortcuts pause while you type or use another dialog.";
    if (!dialog.open) dialog.showModal();
    closeButton.focus();
  }

  const dialog = el("dialog", { class: "command-dialog", "aria-label": "Command menu" });
  const header = el("header", { class: "command-header" });
  const title = el("h2", {}, "Jump to anything");
  const closeButton = el("button", { type: "button", "aria-label": "Close command menu", class: "command-close" }, "Esc");
  closeButton.addEventListener("click", close);
  header.append(title, closeButton);
  const search = el("div", { class: "command-search" });
  const input = el("input", {
    type: "search", placeholder: "Search commands, work, and sessions…", "aria-label": "Search commands, work, and sessions",
    role: "combobox", "aria-autocomplete": "list", "aria-controls": "cockpit-command-results", autocomplete: "off",
  });
  input.addEventListener("input", () => { query = input.value; paint(); loadClosed(); });
  // Include closed (VIEW-21): the page's filter keeps the same choice, in this browser.
  const closedLabel = el("label", { class: "command-closed" });
  const closedSwitch = el("input", { type: "checkbox" });
  closedLabel.append(closedSwitch, document.createTextNode("Include closed"));
  closedSwitch.addEventListener("change", () => {
    try { localStorage.setItem("cockpit:find-closed", JSON.stringify(closedSwitch.checked)); } catch { /* blocked storage: this menu keeps it */ }
    paint(true);
    loadClosed();
    input.focus();
  });
  search.append(input, closedLabel);
  const list = el("ul", { id: "cockpit-command-results", role: "listbox", "aria-label": "Commands and destinations" });
  const help = el("div", { class: "shortcut-help", hidden: "" });
  for (const [heading, bindings] of [
    ["Move around", [[`${modifier} K`, "Search commands, work, and sessions"], ["J / K · ↓ / ↑", "Next / previous row"],
      ["Enter / O", "Open the highlighted row"], ["R", "Reply to the highlighted row, or Next up"], ["Esc", "Close a menu or clear the highlight"], ["?", "Show this guide"]]],
    ["Go to", destinations.map(([, label, , key]) => [`G then ${key.toUpperCase()}`, label])],
    ["Work on Cockpit", [["A / C", "Add work"], ["U", "Update the board"], ["F", "Feedback on part of the page"],
      ["T", "Take the highlighted step below your four places to the top"], ["E", "Mark Next up done"], ["X", "Dismiss Next up"], ["Z", "Snooze Next up for two hours"], [`${modifier} Enter`, "Send a reply from its composer"]]],
    ["Vim editor (when enabled)", [["i / a", "Insert before / after the cursor"], ["Esc", "Return to Normal mode"],
      ["h / j / k / l", "Move in the prompt"], ["v · d w · u", "Visual selection · delete word · undo"], ["Tab", "Leave the editor"]]],
  ]) {
    const section = el("section");
    section.append(el("h3", {}, heading));
    for (const [keys, description] of bindings) {
      const row = el("div", { class: "shortcut-binding" });
      row.append(el("span", {}, description), keycap(keys));
      section.append(row);
    }
    help.append(section);
  }
  const footer = el("footer", { class: "command-footer" });
  const status = el("span", { role: "status", "aria-live": "polite" });
  footer.append(status, el("span", { class: "command-instructions" }, "↑ ↓ navigate · Enter open · Esc close"));
  dialog.append(header, search, list, help, footer);
  dialog.addEventListener("cancel", (event) => { event.preventDefault(); close(); });
  dialog.addEventListener("click", (event) => { if (event.target === dialog) close(); });
  const hint = el("div", { class: "go-hint", hidden: "", role: "status", "aria-live": "polite" });
  hint.append(el("strong", {}, "Go to"));
  for (const [, label, , key] of destinations) {
    const option = el("span");
    option.append(keycap(key.toUpperCase()), document.createTextNode(` ${label}`));
    hint.append(option);
  }
  document.body.append(dialog, hint);
  const trigger = el("button", { class: "command-trigger button ghost", type: "button",
    "aria-label": "Search commands", "aria-keyshortcuts": mac ? "Meta+k" : "Control+k" });
  trigger.append(el("span", {}, "Search"), keycap(`${modifier}K`));
  trigger.addEventListener("click", open);
  const host = document.querySelector(".top-inner");
  if (host) host.insertBefore(trigger, host.querySelector(".top-right"));

  const consume = (event) => { event.preventDefault(); event.stopImmediatePropagation(); };
  document.addEventListener("keydown", (event) => {
    if (event.isComposing || event.keyCode === 229) { resetPrefix(); return; }
    const key = event.key.toLowerCase();
    const commandModifier = mac ? event.metaKey && !event.ctrlKey : event.ctrlKey && !event.metaKey;
    if (commandModifier && !event.altKey && !event.shiftKey && !event.repeat && key === "k") {
      consume(event);
      open();
      return;
    }
    if (dialog.open) {
      if (event.key === "Escape") { consume(event); close(); }
      else if (mode === "commands") {
        const ctrl = event.ctrlKey && !event.metaKey && !event.altKey;
        if (event.key === "ArrowDown" || (ctrl && ["j", "n"].includes(key))) { consume(event); selectResult(index + 1); }
        else if (event.key === "ArrowUp" || (ctrl && ["k", "p"].includes(key))) { consume(event); selectResult(index - 1); }
        else if (event.key === "Enter" && event.target !== closeButton) { consume(event); choose(); }
      }
      event.stopImmediatePropagation();
      return;
    }
    if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey || otherModal() || busy()
        || event.target.closest?.(editing)) { resetPrefix(); return; }
    if (prefixTimer) {
      consume(event);
      resetPrefix();
      if (event.key !== "Escape") {
        const destination = destinations.find((entry) => entry[3] === key);
        if (destination) navigate(destination[2]);
      }
      return;
    }
    if (event.repeat && !["j", "k", "ArrowDown", "ArrowUp"].includes(event.key)) return;
    if (event.key === "Escape") {
      const menu = document.querySelector("details.menu[open]");
      // The browser fires the menu's toggle later; firing it now records the close before a redraw could open the
      // menu again from what the page remembers as open.
      if (menu) { consume(event); menu.open = false; menu.dispatchEvent(new Event("toggle")); menu.querySelector("summary")?.focus(); }
      // Clear the highlight and let Escape also close what else is open (the add box, a tooltip, an editor).
      else if (selected) clearSelection();
      return;
    }
    if (document.querySelector("details.menu[open]") || window.getSelection()?.toString()) return;
    if (event.key === "?") { consume(event); showHelp(); }
    else if (event.shiftKey) return;
    else if (key === "g") {
      consume(event);
      hint.hidden = false;
      prefixTimer = setTimeout(resetPrefix, 1500);
    } else if (key === "j" || event.key === "ArrowDown") { consume(event); move(1); }
    else if (key === "k" || event.key === "ArrowUp") { consume(event); move(-1); }
    else if ((event.key === "Enter" || key === "o") && current()
        && !(event.key === "Enter" && event.target.closest?.("button, a, summary, [role='button']"))) { consume(event); openSelected(); }
    else if (key === "r" && replyTarget() && bridge.reply) { consume(event); bridge.reply(replyTarget()); }
    else if (key === "t" && current() && bridge.take?.(current())) consume(event);
    else if (key === "c" && bridge.add) { consume(event); bridge.add(); }
    else if (event.key === "/") { consume(event); open(); }
  }, true);

  document.addEventListener("pointerdown", (event) => {
    resetPrefix();
    if (dialog.open || event.target.closest?.(".command-trigger")) return;
    const row = candidates().find((node) => node.contains(event.target));
    if (row) highlight(row, false); else clearSelection();
  }, true);
  document.addEventListener("focusin", (event) => {
    if (dialog.open || event.target.closest?.(".command-trigger")) return;
    resetPrefix();
  });
  window.addEventListener("blur", resetPrefix);
  document.addEventListener("visibilitychange", resetPrefix);
  // Hash navigation emits popstate before hashchange. A user can already move
  // in the new view between them; do not erase that fresh selection afterward.
  window.addEventListener("hashchange", () => { routeHandled = ""; followRoute(); });
  function restoreHighlight() {
    if (!selected) return;
    if (selectedNode?.isConnected && selectedNode.classList.contains("keyboard-current")) return;
    const replacement = current();
    if (replacement && (replacement !== selectedNode || !replacement.classList.contains("keyboard-current"))) {
      // Keep focus on the redrawn row without scrolling to it: the user may have scrolled away since.
      highlight(replacement, false);
      // After the redraw has put the focus back on the control it was on, if it can (app.js restoreFocus).
      queueMicrotask(() => { if (!dialog.open && document.activeElement === document.body) replacement.focus({ preventScroll: true }); });
    }
    // Motion can temporarily detach rows between render phases. Keep the logical
    // identity until a replacement appears or the user changes their selection.
  }
  document.addEventListener("cockpit:redraw", () => { followRoute(); restoreHighlight(); });
  new MutationObserver(restoreHighlight).observe(document.querySelector("main") || document.body, {
    subtree: true, childList: true, attributes: true, attributeFilter: ["class"],
  });

  window.CockpitKeys = { open, help: showHelp, selection: current, highlight, chording: () => prefixTimer !== null,
    connect: (options) => { bridge = options; followRoute(); } };
})();
