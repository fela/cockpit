"use strict";
// The center layer: one view at a time over the main page beside the rail, and the whole screen on a
// phone. It holds Cockpit's helper pages (Briefing, Pages, Usage, Server, Spec, Skills) in a frame and the views that
// scripts register (registerCenterView), so nothing opens in a separate app or a separate menu, and each is one or two
// clicks from the main page.
//
// Task and view selections add browser-history destinations; Back and Forward restore them. Close and Escape
// return to the task underneath the view.
//
// The helper pages by path, each with the layer's heading. app.js adds the ones the install's parts serve, from the
// state's `helper_pages`.
const CENTER_HELPERS = { "/briefing": "Briefing", "/pages": "Pages", "/usage": "Usage", "/server": "Server", "/spec": "Spec", "/skills": "Skills" };
// Same-origin helper frames use this registry when following a link to another helper.
window.cockpitHelperPage = (path) => Object.hasOwn(CENTER_HELPERS, path);
// The views scripts register, by name. Each has its `title`, the layer's heading, and `draw(tools, body)`, which fills
// the layer's tools and body. A view that keeps something running while it shows has `leave()` too, which the layer
// calls when it stops showing the view, by opening another view or closing. A script may keep more fields of its own.
const CENTER_VIEWS = new Map();

function registerCenterView(name, view) {
  if (CENTER_VIEWS.has(name)) throw new Error(`center view ${name} is registered twice`);
  CENTER_VIEWS.set(name, view);
}

// Whether the layer still shows `view` with `node` in it, so an answer that arrives after the user left draws nothing.
function centerShows(view, node) {
  return center.view === view && node.isConnected;
}

const center = {
  layer: null,
  view: null,
  opener: null,
};

// Browser history keeps destinations, never form text. Drafts belong to this signed-in page's memory.
const cockpitNavigation = {
  entries: new Map(), current: null, end: 0, restoring: false, applying: false, epoch: 0, initialized: false, scrollWatch: null,
  principal: `${document.documentElement.dataset.offlineUser || ""}-${document.documentElement.dataset.offlineUntil || ""}`,
  chain: null,
};

function keepNavigationEnd() {
  try { sessionStorage.setItem(`cockpit.navigation.${cockpitNavigation.principal}.${cockpitNavigation.chain}`, String(cockpitNavigation.end)); } catch { /* This tab still navigates when storage is blocked. */ }
}

function taskDestination() {
  return { kind: "task", itemId: selectedItem(), warningKey: ui.warningOpen, sessionId: sessionView.id,
    sessionItem: sessionView.item, screen: phoneListAsked() ? "list" : "item" };
}

function navigationMetadata(entry) {
  const state = { ...history.state, cockpitNavigation: { id: entry.id, index: entry.index, chain: cockpitNavigation.chain, principal: cockpitNavigation.principal, route: entry.route } };
  if (entry.route.kind === "center") state.cockpitCenter = entry.route.view;
  else delete state.cockpitCenter;
  state.cockpitScreen = (entry.route.base || entry.route).screen;
  return state;
}

function saveNavigation(writeHistory) {
  cockpitNavigation.scrollWatch?.(); cockpitNavigation.scrollWatch = null;
  const entry = cockpitNavigation.current;
  if (!entry || cockpitNavigation.applying) return;
  if (entry.route.kind === "center") {
    entry.scroll = centerParts().body.scrollTop;
    const frame = center.layer?.querySelector("iframe.center-page");
    if (frame && entry.route.href && writeHistory) {
      try {
        const url = new URL(frame.contentWindow.location.href);
        if (url.origin === location.origin && CENTER_HELPERS[url.pathname]) entry.route.href = url.pathname + url.search + url.hash;
      } catch (error) { if (error.name !== "SecurityError") throw error; }
    }
  } else { if (writeHistory) entry.route = taskDestination(); entry.scroll = window.scrollY; }
  if (writeHistory) history.replaceState(navigationMetadata(entry), "");
}

function initializeNavigation() {
  if (cockpitNavigation.initialized || !ui.state) return;
  cockpitNavigation.initialized = true;
  history.scrollRestoration = "manual";
  const saved = history.state?.cockpitNavigation;
  const restore = saved?.principal === cockpitNavigation.principal && saved.route;
  cockpitNavigation.chain = restore ? saved.chain : crypto.randomUUID();
  const route = restore ? saved.route : taskDestination();
  const index = restore ? saved.index : history.state?.cockpitScreen === "item" && onPhone() ? 1 : 0;
  if (!restore && index === 1) cockpitNavigation.entries.set("phone-list", { id: "phone-list", index: 0, route: { ...route, screen: "list" }, state: {}, scroll: 0 });
  const entry = { id: restore ? saved.id : crypto.randomUUID(), index, route, state: { day: route.day ?? null }, scroll: 0 };
  cockpitNavigation.current = entry; cockpitNavigation.end = index;
  if (restore) {
    try {
      const end = Number(sessionStorage.getItem(`cockpit.navigation.${cockpitNavigation.principal}.${cockpitNavigation.chain}`));
      if (Number.isSafeInteger(end) && end > index) cockpitNavigation.end = end;
    } catch { /* This tab still navigates when storage is blocked. */ }
  }
  keepNavigationEnd();
  cockpitNavigation.entries.set(entry.id, entry);
  history.replaceState(navigationMetadata(entry), "");
  if (restore) restoreNavigation(entry);
}

function recordDestination(route, operation) {
  initializeNavigation();
  if (!cockpitNavigation.current || cockpitNavigation.applying) return;
  cockpitNavigation.restoring = false;
  if (JSON.stringify(cockpitNavigation.current.route) === JSON.stringify(route)) return;
  const current = cockpitNavigation.current;
  if (operation === "replace") {
    current.route = route; current.state = {}; current.scroll = 0;
    history.replaceState(navigationMetadata(current), "");
  } else {
    for (const [id, entry] of cockpitNavigation.entries) if (entry.index > current.index) cockpitNavigation.entries.delete(id);
    const entry = { id: crypto.randomUUID(), index: current.index + 1, route, state: {}, scroll: 0 };
    cockpitNavigation.current = entry; cockpitNavigation.end = entry.index;
    cockpitNavigation.entries.set(entry.id, entry);
    history[operation === "push" ? "pushState" : "replaceState"](navigationMetadata(entry), "");
    keepNavigationEnd();
  }
  renderNavigation();
}

function navigationControls() {
  return h("nav", { class: "destination-navigation", "aria-label": "Navigation history" },
    h("button", { class: "button small ghost icon-only navigation-back", type: "button", "aria-label": "Back", title: "Back", onclick: () => history.back() }, icon("arrow")),
    h("button", { class: "button small ghost icon-only navigation-forward", type: "button", "aria-label": "Forward", title: "Forward", onclick: () => history.forward() }, icon("arrow")));
}

function renderNavigation() {
  if (!ui.state) return;
  initializeNavigation();
  // Back and Forward step through every task and view, so they sit in the top bar: beside the logo in the rail, and
  // beside Add in the phone bar. The center layer's head holds a pair of its own for the phone, where it covers the bar.
  const brand = document.querySelector(".rail-top > .brand-menu");
  if (brand && !brand.nextElementSibling?.matches(".destination-navigation")) brand.after(navigationControls());
  const add = document.querySelector(".phone-bar > .phone-add");
  if (add && !add.previousElementSibling?.matches(".destination-navigation")) add.before(navigationControls());
  const index = cockpitNavigation.current?.index || 0;
  for (const button of document.querySelectorAll(".navigation-back")) button.disabled = index === 0;
  for (const button of document.querySelectorAll(".navigation-forward")) button.disabled = index >= cockpitNavigation.end;
}

function restoreNavigationScroll(entry) {
  const centered = entry.route.kind === "center";
  const root = centered ? centerParts().body : document.querySelector("main.layout");
  const apply = () => {
    if (cockpitNavigation.current !== entry) { stop(); return; }
    if (centered) root.scrollTop = entry.scroll;
    else window.scrollTo({ top: entry.scroll });
    if ((centered ? root.scrollTop : window.scrollY) >= entry.scroll) stop();
  };
  const observer = new MutationObserver(apply);
  const stop = () => { observer.disconnect(); clearTimeout(timer); removeEventListener("pointerdown", stop, true); removeEventListener("keydown", stop, true); };
  const timer = setTimeout(stop, 10000);
  observer.observe(root, { childList: true, subtree: true });
  addEventListener("pointerdown", stop, true); addEventListener("keydown", stop, true);
  cockpitNavigation.scrollWatch = stop;
  requestAnimationFrame(apply);
}

async function restoreNavigation(entry) {
  const epoch = ++cockpitNavigation.epoch;
  cockpitNavigation.restoring = true;
  cockpitNavigation.current = entry;
  const base = entry.route.base || entry.route;
  try {
    let session;
    cockpitNavigation.applying = true;
    try {
      closeCenter(false);
      setPhoneScreen(base.screen === "list", false);
      showOnCard(base.itemId, base.warningKey);
      if (base.sessionId) session = openSessionView(base.sessionId, base.sessionItem);
    } finally { cockpitNavigation.applying = false; }
    if (session) await session;
    if (cockpitNavigation.current !== entry || cockpitNavigation.epoch !== epoch) return;
    cockpitNavigation.applying = true;
    try {
      if (entry.route.kind === "center") {
        if (entry.route.href) openCenterHelper(entry.route.href);
        else openCenter(entry.route.view);
      }
    } finally { cockpitNavigation.applying = false; }
  } finally {
    if (cockpitNavigation.current === entry && cockpitNavigation.epoch === epoch) {
      cockpitNavigation.restoring = false;
      renderNavigation(); restoreNavigationScroll(entry);
    }
  }
}

function beginNavigationIntent() {
  const interrupted = cockpitNavigation.restoring;
  cockpitNavigation.epoch++; cockpitNavigation.restoring = false;
  initializeNavigation(); saveNavigation(!interrupted);
}

// A view supplies its complete small state. Only its date enters browser history; its drafts stay in memory.
function centerSetViewState(state) {
  const entry = cockpitNavigation.current;
  if (!entry || entry.route.kind !== "center") return;
  entry.state = structuredClone(state);
  entry.route.day = /^\d{4}-\d{2}-\d{2}$/.test(state.day || "") ? state.day : null;
  history.replaceState(navigationMetadata(entry), "");
}

window.CockpitNavigation = {
  get restoring() { return cockpitNavigation.applying; },
  capture() { return { epoch: cockpitNavigation.epoch, id: cockpitNavigation.current?.id, restoring: cockpitNavigation.applying }; },
  current(token) { return token.epoch === cockpitNavigation.epoch && token.id === cockpitNavigation.current?.id; },
  before() { if (!cockpitNavigation.applying) { beginNavigationIntent(); closeCenter(false); } },
  task(itemId, warningKey, replace) {
    recordDestination({ ...taskDestination(), itemId, warningKey, sessionId: null, sessionItem: null }, replace ? "replace" : "push");
  },
  session(sessionId, itemId, replace) {
    recordDestination({ ...taskDestination(), itemId: itemId ?? selectedItem(), sessionId, sessionItem: itemId }, replace ? "replace" : "push");
  },
  phone() { if (!cockpitNavigation.applying) recordDestination(taskDestination(), "adopt"); },
  list() {
    beginNavigationIntent();
    const before = [...cockpitNavigation.entries.values()].find((entry) => entry.index === cockpitNavigation.current.index - 1);
    if (before?.route.kind === "task" && before.route.screen === "list") history.back();
    else {
      recordDestination({ ...taskDestination(), screen: "list" }, "push");
      setPhoneScreen(true, true);
    }
  },
  save() { saveNavigation(true); },
};

document.addEventListener("cockpit:redraw", renderNavigation);
addEventListener("cockpit:offline-cleared", (event) => {
  if (event.detail?.scope && event.detail.scope !== cockpitNavigation.principal) return;
  cockpitNavigation.entries.clear(); cockpitNavigation.current = null; cockpitNavigation.end = 0;
  cockpitNavigation.epoch++; cockpitNavigation.restoring = false;
  cockpitNavigation.scrollWatch?.();
  try { sessionStorage.removeItem(`cockpit.navigation.${cockpitNavigation.principal}.${cockpitNavigation.chain}`); } catch { /* The memory snapshots are already cleared. */ }
  const state = { ...history.state }; delete state.cockpitNavigation; delete state.cockpitCenter;
  history.replaceState(state, "");
});

function centerLayer() {
  if (center.layer) return center.layer;
  const title = h("h2", { class: "center-title" });
  const close = iconButton("x", "Close", { class: "button small ghost center-close", onclick: () => closeCenter(true) });
  center.layer = h("section", { class: "center-layer", "aria-label": "Opened view", hidden: true },
    h("header", { class: "center-head" }, navigationControls(), title, h("div", { class: "center-tools" }), offlineNoteKey(), close),
    h("div", { class: "center-body" }));
  document.body.append(center.layer);
  return center.layer;
}

function centerParts() {
  const layer = centerLayer();
  return { layer, title: layer.querySelector(".center-title"), tools: layer.querySelector(".center-tools"), body: layer.querySelector(".center-body") };
}

function showCenter(view, title) {
  const { layer, title: heading, tools, body } = centerParts();
  if (layer.hidden) center.opener = document.activeElement;
  CENTER_VIEWS.get(center.view)?.leave?.();
  center.view = view;
  heading.textContent = title;
  tools.replaceChildren();
  body.replaceChildren();
  body.className = `center-body center-${view.startsWith("/") ? "helper" : view.split(":")[0]}`;
  layer.hidden = false;
  document.documentElement.classList.add("center-open");
  layer.querySelector(".center-close").focus({ preventScroll: true });
  window.closeOfflineNote?.();
  window.renderOfflineStatus?.();
  renderNavigation();
  return { tools, body };
}

// `goBack` records an explicit Close/Escape; false hides the view during another navigation.
function closeCenter(goBack) {
  if (!center.layer || center.layer.hidden) return;
  if (goBack && !cockpitNavigation.applying) {
    beginNavigationIntent();
    const base = cockpitNavigation.current?.route.base || taskDestination();
    recordDestination(base, "push");
  }
  // Removing the frame also drops its active page before another destination opens.
  center.layer.querySelector(".center-body").replaceChildren();
  center.layer.hidden = true;
  CENTER_VIEWS.get(center.view)?.leave?.();
  center.view = null;
  document.documentElement.classList.remove("center-open");
  window.closeOfflineNote?.();
  window.renderOfflineStatus?.();
  if (center.opener?.isConnected) center.opener.focus({ preventScroll: true });
  center.opener = null;
}

function openCenter(view) {
  const shown = CENTER_VIEWS.get(view);
  if (!shown) throw new Error(`no center view ${view}`);
  if (!cockpitNavigation.applying) {
    if (center.view === view) return;
    beginNavigationIntent();
    const base = cockpitNavigation.current?.route.base || taskDestination();
    recordDestination({ kind: "center", view, base }, "push");
  }
  const { tools, body } = showCenter(view, shown.title);
  shown.draw(tools, body, cockpitNavigation.current?.state || {});
}

// A helper page in a frame, without its own header. Returns false for a link this layer leaves to the browser.
function openCenterHelper(href) {
  const url = new URL(href, location.href);
  const name = CENTER_HELPERS[url.pathname];
  if (!name || url.origin !== location.origin) return false;
  url.searchParams.set("center", "1");
  if (!cockpitNavigation.applying) {
    beginNavigationIntent();
    const base = cockpitNavigation.current?.route.base || taskDestination();
    recordDestination({ kind: "center", view: url.pathname, href: url.pathname + url.search + url.hash, base }, "push");
  }
  const { body } = showCenter(url.pathname, name);
  const frame = h("iframe", { class: "center-page", src: url.pathname + url.search + url.hash, title: name });
  // A link on the page can lead to another helper page, which stays in the frame (center-frame.js): the layer's title
  // then names that page.
  frame.addEventListener("load", () => {
    try {
      const shown = CENTER_HELPERS[frame.contentWindow.location.pathname];
      if (!shown || !frame.isConnected) return;
      frame.title = shown;
      centerParts().title.textContent = shown;
    } catch (error) { if (error.name !== "SecurityError") throw error; }
  });
  body.append(frame);
  return true;
}

// The registered view a link names, as "time" for /#time on this origin; null for any other link.
function centerViewOf(href) {
  const url = new URL(href, location.href);
  const name = url.hash.slice(1);
  if (url.origin !== location.origin || url.pathname !== "/") return null;
  return CENTER_VIEWS.has(name) ? name : null;
}

window.CockpitCenter = { open: openCenter, openHelper: openCenterHelper, close: () => closeCenter(true) };

function wireCenter() {
  addEventListener("popstate", (event) => {
    saveNavigation(false);
    const saved = event.state?.cockpitNavigation;
    if (saved?.principal === cockpitNavigation.principal && saved.route) {
      const entry = cockpitNavigation.entries.get(saved.id) || { ...saved, state: { day: saved.route.day ?? null }, scroll: 0 };
      cockpitNavigation.entries.set(entry.id, entry);
      restoreNavigation(entry);
    } else if (event.state?.cockpitScreen === "list" && cockpitNavigation.entries.has("phone-list")) {
      const entry = cockpitNavigation.entries.get("phone-list");
      history.replaceState(navigationMetadata(entry), ""); restoreNavigation(entry);
    } else closeCenter(false);
  }, true);
  addEventListener("message", (event) => {
    const data = event.data;
    if (event.origin === location.origin && data?.cockpitCenter === "close") { closeCenter(true); return; }
    // A row of a helper page that names a task: the layer closes and the main page opens the task (app.js) once the
    // close's Back has come, so the task's address goes on the main page's own history entry.
    const page = center.layer?.querySelector("iframe.center-page");
    if (event.origin === location.origin && page && event.source === page.contentWindow && data?.cockpitOverlay === "item"
        && Number.isInteger(data.id) && data.id > 0) {
      dispatchEvent(new CustomEvent("cockpit:open-item", { detail: data.id }));
    }
  });
  // Before keyboard.js, which listens on the document. A field, an open menu or a dialog keeps its own Escape.
  addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !center.layer || center.layer.hidden || event.defaultPrevented) return;
    if (event.target.closest?.("input, textarea, select, [contenteditable]")) return;
    if (window.shownOfflineNote?.() || document.querySelector("details.menu[open], dialog[open]")) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    closeCenter(true);
  }, true);
  // A plain click on a helper page's link (the logo menu) opens it here. page-overlay.js takes a page's own link first.
  // A link to a registered view on the main page, such as /#time on a step's link chip, opens that view here too, even
  // when the link would open a new tab: the page the user is on shows it, with no second copy of Cockpit loading.
  document.addEventListener("click", (event) => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest?.("a[href]");
    if (!link || link.closest(".center-layer")) return;
    const view = centerViewOf(link.href);
    if (view) openCenter(view);
    else if (link.target || !openCenterHelper(link.href)) return;
    event.preventDefault();
    link.closest("details.menu")?.removeAttribute("open");
  });
}
wireCenter();
