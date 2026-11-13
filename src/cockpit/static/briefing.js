"use strict";

// The Briefing page (SPEC VIEW-8): the morning briefing on the user's team, which a session writes each
// morning on that day's task, links to earlier days, and the team itself, which the user changes here. Opening
// a briefing marks it read, which takes the mark off the Briefing tab. While today's briefing is still
// being written, the page says so and checks again every minute.

const POLL_MS = 60e3;
const icon = (name) => Icons.icon(name);
let shownListing = "";
let loadNumber = 0;

// Overlay (SPEC NEXT-47): `&overlay=1` is this view inside the layer that opens a briefing over Cockpit's main page
// (page-overlay.js). It shows the briefing without the page's header and tabs; the layer's X, Escape and Back close
// it, and the layer shows its loading sheet until this view says the briefing has drawn, or that it could not.
const overlay = new URLSearchParams(location.search).get("overlay") === "1";
if (overlay) document.body.classList.add("briefing-overlay-mode");
let readySent = false;
function overlayReady() {
  if (!overlay || readySent) return;
  readySent = true;
  parent.postMessage({ cockpitOverlay: "ready" }, location.origin);
}
// The task a link names on Cockpit's page: a mention's `/#item/N`, or `/#home/item/N` (keyboard.js); else null.
// pages.js has the same.
function taskLinkId(link) {
  let url;
  try {
    url = new URL(link.href, location.href);
  } catch {
    return null;
  }
  if (url.origin !== location.origin || !["/", "/classic"].includes(url.pathname)) return null;
  const match = url.hash.match(/^#(?:(?:home|board)\/)?item\/(\d+)$/);
  return match ? Number(match[1]) : null;
}

function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [name, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (name === "class") el.className = value;
    else if (name === "text") el.textContent = value;
    else if (name === "html") { el.innerHTML = value; Icons.markMentions(el); }
    else el.setAttribute(name, value === true ? "" : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

async function api(method, path, body) {
  const options = {
    method,
    headers: body === undefined ? {} : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
  };
  // A read waits through a restart of Cockpit; an action is sent again until Cockpit has it (CODE-5).
  const { response, data } = method === "GET" ? await readThroughRestart(path, options) : await sendAction(path, options, TRY_MS, null);
  if (response.status === 401) {
    location.href = "/auth/google";
    throw Object.assign(new Error("Signed out"), { status: 401 });
  }
  if (!response.ok) throw Object.assign(new Error(data.detail || `Cockpit answered ${response.status}`), { status: response.status });
  return data;
}

function dayWords(day, weekday) {
  const [year, month, date] = day.split("-").map(Number);
  return new Date(year, month - 1, date).toLocaleDateString(undefined, { weekday, day: "numeric", month: "long" });
}

// A moment in this browser's time: "Wed 07:00".
function at(iso) {
  return new Date(iso).toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
}

// A time today in this browser's time: "10:00".
function clock(iso) {
  return new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
}

function showError(text) {
  document.getElementById("banner").replaceChildren(h("div", { class: "banner fail", text }));
}

// What happened to the last request for a briefing, when that day's briefing is not written yet.
function launchNote(launch) {
  if (!launch || launch.written) return null;
  const day = dayWords(launch.day, "long");
  if (launch.state === "failed") {
    return h("p", { class: "briefing-status fail" }, icon("alert"),
      h("span", { text: `The briefing for ${day} did not start: ${launch.detail || "the message did not reach a session"}. Cockpit asks again at ${clock(launch.retry_at)}.` }));
  }
  return h("p", { class: "briefing-status" }, icon("clock"),
    h("span", { text: `Cockpit asked for the briefing for ${day} at ${clock(launch.asked_at)}; the briefing session is writing it. It shows here when it is stored.` }));
}

// The people the briefing follows, in the user's words. The editor is drawn once; a read shows a newer
// team only while the box holds the saved one, is not being typed in and no save is under way. A read that
// started before a save ended may hold the team from before it, so `team.saves` counts each save's start
// and end and such a read is dropped.
// held: what the user was writing when they pressed Reload (UI-13), until the saved text has arrived under it.
const team = { saved: null, saving: false, saves: 0, field: null, save: null, said: null, held: window.PageReload?.take()?.team ?? null };
window.PageReload?.hold(() => {
  if (team.held !== null) return { team: team.held };
  return team.field && team.field.value !== team.saved ? { team: team.field.value } : null;
});

function renderTeam(text, savesAtRead) {
  if (!team.field) drawTeam();
  const { field } = team;
  if (savesAtRead !== team.saves || team.saving || field.value !== team.saved || document.activeElement === field) return;
  team.saved = text || "";
  field.value = team.saved;
  team.save.disabled = true;
  if (team.held !== null) {
    field.value = team.held;
    team.held = null;
    team.save.disabled = field.value.trim() === team.saved;
  }
}

function drawTeam() {
  team.field = h("textarea", {
    id: "team-field", class: "field", rows: 5, "aria-label": "The people you work with most closely",
    placeholder: "One person a line, with what they do: Ana works on Tasks reminders",
  });
  team.save = h("button", { class: "button small primary", type: "button", disabled: true, text: "Save" });
  team.said = h("span", { class: "note", role: "status" });
  team.field.value = team.saved = "";
  team.field.addEventListener("input", () => {
    team.save.disabled = team.saving || team.field.value.trim() === team.saved;
    team.said.textContent = "";
  });
  team.save.addEventListener("click", saveTeam);
  document.getElementById("team").replaceChildren(
    h("h2", { id: "team-title", text: "Your team" }),
    h("p", { class: "note", text: "One person per line, with their role; leave empty to stop briefings" }),
    team.field,
    h("div", { class: "briefing-team-actions" }, team.save, team.said),
  );
}

// Words typed while the save is under way stay in the box, for the next save.
async function saveTeam() {
  if (team.saving) return;
  const { field, save, said } = team;
  const sent = field.value;
  team.saving = true;
  team.saves += 1;
  save.disabled = true;
  try {
    const saved = await api("PUT", "/api/team", { team: sent });
    team.saved = saved.team;
    if (field.value === sent) field.value = saved.team;
    said.textContent = saved.team ? "Saved; the next briefing follows these people." : "Saved; no briefing comes until you name someone.";
  } catch (error) {
    logError("saving the team", error);
    said.textContent = `Not saved: ${error.message}`;
  } finally {
    team.saves += 1;
    team.saving = false;
    save.disabled = field.value.trim() === team.saved;
  }
  refresh();
}

function renderDays(listing, shownId) {
  const nav = document.getElementById("days");
  if (listing.briefings.length < 2) {
    nav.replaceChildren();
    return;
  }
  nav.replaceChildren(
    h("h2", { text: "Earlier briefings" }),
    h("ul", null, listing.briefings.map((each) => h("li", null, h("a", {
      href: `/briefing?id=${each.id}${overlay ? "&overlay=1" : ""}`,
      class: each.id > listing.read_id ? "unread" : null,
      "aria-current": each.id === shownId ? "page" : null,
      text: dayWords(each.day, "short"),
    })))),
  );
}

async function load() {
  const number = ++loadNumber;
  const savesAtRead = team.saves;
  const listing = await api("GET", "/api/briefings");
  if (number !== loadNumber) return;
  renderTeam(listing.team, savesAtRead);
  const wanted = Number(new URLSearchParams(location.search).get("id")) || null;
  const key = JSON.stringify([listing, wanted]);
  // The listing counts as shown once its briefing is on the page, so a briefing that failed to load is asked again.
  if (key === shownListing) return;
  const newest = listing.briefings[0];
  const shownId = wanted || (newest && newest.id);
  const article = document.getElementById("briefing");
  const status = wanted ? null : launchNote(listing.launch);
  if (!shownId) {
    const hour = `${String(listing.hour).padStart(2, "0")}:00`;
    Loading.settle(article);
    article.replaceChildren(h("h1", { text: "Morning briefing" }), status || h("p", { class: "note",
      text: listing.team
        ? `Your first briefing is due at ${hour} your time`
        : "Name your team below to start daily briefings" }));
    renderDays(listing, null);
    overlayReady();
    shownListing = key;
    return;
  }
  // The briefing's mentions of other items lead with their project's logo (SPEC UI-74), from the project list.
  const [shown, projects] = await Promise.all([api("GET", `/api/briefings/${shownId}`), api("GET", "/api/projects")]);
  if (number !== loadNumber) return;
  Icons.setProjects(projects);
  document.title = `Cockpit briefing ${dayWords(shown.day, "short")}`;
  const written = shown.updated_at === shown.created_at ? `written ${at(shown.created_at)}` : `written ${at(shown.created_at)}, rewritten ${at(shown.updated_at)}`;
  Loading.settle(article);
  article.replaceChildren(
    ...[status].filter(Boolean),
    h("header", { class: "briefing-head" },
      h("h1", { text: `Briefing for ${dayWords(shown.day, "long")}` }),
      h("p", { class: "note", text: `${at(shown.since)}–${at(shown.until)}, local time · ${written}` })),
    h("div", { class: "prose briefing-body", html: shown.body_html }),
  );
  renderDays(listing, shown.id);
  overlayReady();
  // The listing counts as shown once its briefing is marked read too, so a read mark that failed goes again. An
  // older briefing whose read step is still open is marked read again, which completes that step.
  if (shown.id > listing.read_id) await api("POST", `/api/briefings/${shown.id}/read`);
  if (number !== loadNumber) return;
  shownListing = key;
}

// The page reads the state when it opens and when it comes back into view: a read of the state counts as the user
// looking at the board, which keeps the automatic checks going (TECH section 7, Checks).
async function markSeen() {
  try {
    await api("GET", "/api/state");
  } catch (error) {
    logError("telling Cockpit you are looking", error);
  }
}

function refresh() {
  const loading = load();
  const number = loadNumber;
  loading.then(() => {
    if (number === loadNumber) document.getElementById("banner").replaceChildren();
  }).catch((error) => {
    if (number !== loadNumber) return;
    logError("loading the briefing", error);
    Loading.failed(document.getElementById("briefing"));
    showError(`Cockpit could not load the briefing: ${error.message}`);
    // In the layer, the failure shows in the briefing's place, with the X to close it.
    overlayReady();
  });
}

refresh();
markSeen();
setInterval(() => { if (!document.hidden) refresh(); }, POLL_MS);
// A connection that comes back shows the latest briefing at once, rather than at the next poll (CODE-5).
addEventListener("online", () => refresh());
// Actions a closed or reloaded page left behind went from this page (errors.js, CODE-5).
addEventListener("cockpit:sent-left", () => refresh());
document.addEventListener("visibilitychange", () => {
  if (document.hidden) return;
  refresh();
  markSeen();
});

if (overlay) {
  // Escape closes the layer, unless the user is writing in the team box.
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || event.defaultPrevented) return;
    if (event.target.closest && event.target.closest("textarea, input, select, [contenteditable]") && event.target.value) return;
    parent.postMessage({ cockpitOverlay: "close" }, location.origin);
  });
  // Another day's briefing takes this one's place in the layer, without a history entry of the frame's own, so one
  // Back still closes the layer.
  document.getElementById("days").addEventListener("click", (event) => {
    const link = event.target.closest && event.target.closest("a[href]");
    if (!link || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    location.replace(link.href);
  });
  // A task the briefing mentions opens on Cockpit's page under the layer, which closes (page-overlay.js), rather
  // than the whole of Cockpit loading inside the layer. The Pages view does the same with its links to a task.
  document.addEventListener("click", (event) => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest && event.target.closest("a[href]");
    const id = link && taskLinkId(link);
    if (!id) return;
    event.preventDefault();
    parent.postMessage({ cockpitOverlay: "item", id }, location.origin);
  });
  // The user's input on the layer is activity for the timer of the step that opened it (SPEC TRACK-5), which
  // Cockpit's page cannot hear: the layer passes it on, at most twice a second, as the Pages view does (pages.js).
  let activitySent = 0;
  const sendActivity = () => {
    const now = Date.now();
    if (now - activitySent < 500) return;
    activitySent = now;
    parent.postMessage({ cockpitOverlay: "activity" }, location.origin);
  };
  let pointerX = null;
  let pointerY = null;
  addEventListener("pointermove", (event) => {
    if (!event.isTrusted || (event.screenX === pointerX && event.screenY === pointerY)) return;
    pointerX = event.screenX;
    pointerY = event.screenY;
    sendActivity();
  }, { capture: true, passive: true });
  for (const type of ["wheel", "keydown", "pointerdown"]) {
    addEventListener(type, (event) => {
      if (event.isTrusted) sendActivity();
    }, { capture: true, passive: true });
  }
}
