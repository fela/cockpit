"use strict";

// The Settings page (SPEC CONTROL-33): the few things the user changes about how Cockpit works for them, in one
// place. When the morning briefing and the weekly digest come (settings.py), Cockpit's time zone (zones.py,
// CONTROL-32), the team the briefings follow (kept on the Briefing page) and this browser's Vim editing. Each change
// is saved as it is made; a refused one goes back to what Cockpit holds and says why.

const DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const VIM_KEY = "cockpit.vim";
// The digest waits this long for that day's briefing (briefing.DIGEST_WAIT_HOURS).
const DIGEST_WAIT_HOURS = 4;

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

const hourWords = (hour) => `${String(hour).padStart(2, "0")}:00`;

function hourPick(label, value) {
  return h("select", { class: "field settings-pick", "aria-label": label },
    Array.from({ length: 24 }, (_, hour) => h("option", { value: hour, selected: hour === value, text: hourWords(hour) })));
}

// A row: what it sets, the controls, and a line that says whether the last change was saved.
function row(title, about, ...controls) {
  const said = h("span", { class: "note settings-said", role: "status" });
  return { said, el: h("div", { class: "settings-row" },
    h("div", { class: "settings-what" }, h("h3", { text: title }), about && h("p", { class: "note", text: about })),
    h("div", { class: "settings-controls" }, ...controls, said)) };
}

async function save(said, send, undo) {
  said.textContent = "Saving…";
  said.classList.remove("fail");
  try {
    await send();
    said.textContent = "Saved";
  } catch (error) {
    logError("saving a setting", error);
    undo();
    said.textContent = `Not saved: ${error.message}`;
    said.classList.add("fail");
  }
}

function briefings(data) {
  const { schedule, zone } = data;
  const briefingHour = hourPick("Morning briefing hour", schedule.briefing_hour);
  const morning = row("Morning briefing", `The hour your briefing on the team comes each day, ${zone} time.`, briefingHour);
  briefingHour.addEventListener("change", () => {
    const before = schedule.briefing_hour;
    schedule.briefing_hour = Number(briefingHour.value);
    save(morning.said, () => api("PUT", "/api/settings", { briefing_hour: schedule.briefing_hour }),
      () => { schedule.briefing_hour = before; briefingHour.value = before; });
  });

  const day = h("select", { class: "field settings-pick", "aria-label": "Weekly digest day" },
    DAYS.map((name, index) => h("option", { value: index, selected: index === schedule.digest_day, text: name })),
    h("option", { value: "", selected: schedule.digest_day === null, text: "No digest" }));
  const digestHour = hourPick("Weekly digest hour", schedule.digest_hour);
  const digest = row("Weekly digest",
    `A catch-up on the week before the next, on that day's morning. It waits up to ${DIGEST_WAIT_HOURS} hours for that day's briefing.`,
    day, digestHour);
  const showDay = () => { digestHour.disabled = schedule.digest_day === null; };
  showDay();
  day.addEventListener("change", () => {
    const before = schedule.digest_day;
    schedule.digest_day = day.value === "" ? null : Number(day.value);
    showDay();
    save(digest.said, () => api("PUT", "/api/settings", { digest_day: schedule.digest_day }),
      () => { schedule.digest_day = before; day.value = before === null ? "" : String(before); showDay(); });
  });
  digestHour.addEventListener("change", () => {
    const before = schedule.digest_hour;
    schedule.digest_hour = Number(digestHour.value);
    save(digest.said, () => api("PUT", "/api/settings", { digest_hour: schedule.digest_hour }),
      () => { schedule.digest_hour = before; digestHour.value = before; });
  });

  const teamWords = data.team ? data.team.split("\n").map((line) => line.trim()).filter(Boolean) : [];
  const team = row("Your team", teamWords.length
    ? `Briefings follow ${teamWords.length === 1 ? "1 person" : `${teamWords.length} people`}.`
    : "No one yet, so no briefings come.",
  h("a", { class: "button small", href: "/briefing", text: teamWords.length ? "Change on the Briefing page" : "Name them on the Briefing page" }));

  return h("section", { class: "settings-section", "aria-labelledby": "briefings-title" },
    h("h2", { id: "briefings-title", text: "Briefings" }), morning.el, digest.el, team.el);
}

function timeZone(data) {
  const device = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const names = [...new Set([...(Intl.supportedValuesOf?.("timeZone") ?? []), data.zone, device])].sort();
  const pick = h("select", { class: "field settings-pick settings-zone", "aria-label": "Time zone" },
    names.map((name) => h("option", { value: name, selected: name === data.zone, text: name.replaceAll("_", " ") })));
  const own = h("button", { class: "button small", type: "button", text: `Use this device's zone (${device.replaceAll("_", " ")})` });
  const zone = row("Time zone", "Sets the hours above, scheduled prompts, the sweeper's work hours and the days in your time report.", pick, own);
  const showOwn = () => { own.hidden = pick.value === device || device.startsWith("Etc/") || device === "UTC"; };
  showOwn();
  const choose = (name) => {
    const before = data.zone;
    pick.value = name;
    showOwn();
    save(zone.said, async () => { data.zone = (await api("PUT", "/api/timezone", { zone: name })).zone; },
      () => { data.zone = before; pick.value = before; showOwn(); });
  };
  pick.addEventListener("change", () => choose(pick.value));
  own.addEventListener("click", () => choose(device));
  return h("section", { class: "settings-section", "aria-labelledby": "zone-title" },
    h("h2", { id: "zone-title", text: "Time" }), zone.el);
}

// Vim editing is this browser's own: the setting lives in its storage, and an open Cockpit tab follows through the
// storage event (editor-loader.js).
function thisBrowser() {
  const kept = () => { try { return localStorage.getItem(VIM_KEY) === "true"; } catch { return false; } };
  const box = h("input", { type: "checkbox", role: "switch", id: "vim-setting", class: "settings-switch" });
  box.checked = kept();
  const vim = row("Vim editing", "Vim keys in session prompts and replies. i enters Insert mode, Esc returns to Normal mode.",
    h("label", { class: "settings-toggle", for: "vim-setting" }, box, h("span", { text: "Vim editing" })));
  box.addEventListener("change", () => {
    try {
      localStorage.setItem(VIM_KEY, String(box.checked));
      vim.said.textContent = "Saved for this browser";
    } catch {
      box.checked = !box.checked;
      vim.said.textContent = "Not saved: this browser blocks storage";
    }
  });
  addEventListener("storage", (event) => { if (event.key === VIM_KEY || event.key === null) box.checked = kept(); });
  return h("section", { class: "settings-section", "aria-labelledby": "browser-title" },
    h("h2", { id: "browser-title", text: "This browser" }), vim.el);
}

function elsewhere() {
  return h("section", { class: "settings-section", "aria-labelledby": "elsewhere-title" },
    h("h2", { id: "elsewhere-title", text: "Elsewhere" }),
    h("p", { class: "note" }, "How your inbox is ranked and what Cockpit checks for new work are under ",
      h("a", { href: "/#home/goals", target: "_top", text: "Priorities and inbox" }), "."));
}

async function load() {
  const holder = document.getElementById("settings");
  try {
    const data = await api("GET", "/api/settings");
    Loading.settle(holder);
    holder.replaceChildren(briefings(data), timeZone(data), thisBrowser(), elsewhere());
    document.getElementById("banner").replaceChildren();
  } catch (error) {
    logError("loading the settings", error);
    Loading.failed(holder);
    document.getElementById("banner").replaceChildren(h("div", { class: "banner fail", text: `Cockpit could not load the settings: ${error.message}` }));
  }
}

load();
