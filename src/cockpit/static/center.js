"use strict";
// The center layer: one view at a time over the main page beside the rail, and the whole screen on a
// phone. It holds Cockpit's helper pages (Briefing, Pages, Usage, Server, Spec, Skills) in a frame and the views that
// scripts register (registerCenterView), so nothing opens in a separate app or a separate menu, and each is one or two
// clicks from the main page.
//
// Opening a view adds one entry to the browser's history, so Back closes the layer. X, Escape and a helper page's
// Escape (center-frame.js) close it too.
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
  // Whether this layer added the history entry it sits on, so closing it goes back past that entry.
  pushed: false,
  opener: null,
};

function centerLayer() {
  if (center.layer) return center.layer;
  const title = h("h2", { class: "center-title" });
  const close = iconButton("x", "Close", { class: "button small ghost center-close", onclick: () => closeCenter(true) });
  center.layer = h("section", { class: "center-layer", "aria-label": "Opened view", hidden: true },
    h("header", { class: "center-head" }, title, h("div", { class: "center-tools" }), close),
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
  if (layer.hidden) {
    center.opener = document.activeElement;
    history.pushState({ ...history.state, cockpitCenter: view }, "");
    center.pushed = true;
  } else {
    history.replaceState({ ...history.state, cockpitCenter: view }, "");
  }
  CENTER_VIEWS.get(center.view)?.leave?.();
  center.view = view;
  heading.textContent = title;
  tools.replaceChildren();
  body.replaceChildren();
  body.className = `center-body center-${view.startsWith("/") ? "helper" : view.split(":")[0]}`;
  layer.hidden = false;
  document.documentElement.classList.add("center-open");
  layer.querySelector(".center-close").focus({ preventScroll: true });
  return { tools, body };
}

// `goBack` is false when the browser already went back past the layer's entry.
function closeCenter(goBack) {
  if (!center.layer || center.layer.hidden) return;
  // The helper page's frame goes first, so the entries it added to the history go with it before the step back.
  center.layer.querySelector(".center-body").replaceChildren();
  center.layer.hidden = true;
  CENTER_VIEWS.get(center.view)?.leave?.();
  center.view = null;
  document.documentElement.classList.remove("center-open");
  const backOver = goBack && center.pushed && history.state?.cockpitCenter;
  center.pushed = false;
  if (backOver) history.back();
  if (center.opener?.isConnected) center.opener.focus({ preventScroll: true });
  center.opener = null;
}

function openCenter(view) {
  const shown = CENTER_VIEWS.get(view);
  if (!shown) throw new Error(`no center view ${view}`);
  const { tools, body } = showCenter(view, shown.title);
  shown.draw(tools, body);
}

// A helper page in a frame, without its own header. Returns false for a link this layer leaves to the browser.
function openCenterHelper(href) {
  const url = new URL(href, location.href);
  const name = CENTER_HELPERS[url.pathname];
  if (!name || url.origin !== location.origin || document.body.dataset.layout !== "sidebar") return false;
  url.searchParams.set("center", "1");
  const { body } = showCenter(url.pathname, name);
  const frame = h("iframe", { class: "center-page", src: url.pathname + url.search + url.hash, title: name });
  // A link on the page can lead to another helper page, which stays in the frame (center-frame.js): the layer's title
  // then names that page.
  frame.addEventListener("load", () => {
    const shown = CENTER_HELPERS[frame.contentWindow.location.pathname];
    if (!shown || !frame.isConnected) return;
    frame.title = shown;
    centerParts().title.textContent = shown;
  });
  body.append(frame);
  return true;
}

// The registered view a link names, as "time" for /#time on this origin; null for any other link.
function centerViewOf(href) {
  const url = new URL(href, location.href);
  const name = url.hash.slice(1);
  if (url.origin !== location.origin || url.pathname !== "/" || document.body.dataset.layout !== "sidebar") return null;
  return CENTER_VIEWS.has(name) ? name : null;
}

window.CockpitCenter = { open: openCenter, openHelper: openCenterHelper, close: () => closeCenter(true) };

function wireCenter() {
  addEventListener("popstate", (event) => {
    if (!event.state?.cockpitCenter) closeCenter(false);
  });
  addEventListener("message", (event) => {
    const data = event.data;
    if (event.origin === location.origin && data?.cockpitCenter === "close") { closeCenter(true); return; }
    // A row of a helper page that names a task: the layer closes and the main page opens the task (app.js) once the
    // close's Back has come, so the task's address goes on the main page's own history entry.
    const page = center.layer?.querySelector("iframe.center-page");
    if (event.origin === location.origin && page && event.source === page.contentWindow && data?.cockpitOverlay === "item"
        && Number.isInteger(data.id) && data.id > 0) {
      const open = () => setTimeout(() => dispatchEvent(new CustomEvent("cockpit:open-item", { detail: data.id })), 0);
      const back = center.pushed && history.state?.cockpitCenter;
      closeCenter(true);
      if (back) addEventListener("popstate", open, { once: true });
      else open();
    }
  });
  // Before keyboard.js, which listens on the document. A field, an open menu or a dialog keeps its own Escape.
  addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !center.layer || center.layer.hidden || event.defaultPrevented) return;
    if (event.target.closest?.("input, textarea, select, [contenteditable]")) return;
    if (document.querySelector("details.menu[open], dialog[open]")) return;
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
