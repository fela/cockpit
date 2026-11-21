"use strict";

// The Settings page (SPEC CONTROL-35): the hour the morning briefing comes and, with the weekly digest on, the
// digest's day and hour or no digest (briefing.schedule). Each change sends the whole schedule. One save is in
// flight at a time; a change made meanwhile goes out once it returns, so the page ends on what Cockpit holds after
// the last save.

const DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [name, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (name === "class") el.className = value;
    else if (name === "text") el.textContent = value;
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

function hourPick(label) {
  return h("select", { class: "field settings-pick", "aria-label": label },
    Array.from({ length: 24 }, (_, hour) => h("option", { value: hour, text: `${String(hour).padStart(2, "0")}:00` })));
}

// A setting: what it does at the left, its controls at the right.
function row(title, about, ...controls) {
  return h("div", { class: "settings-row" },
    h("div", { class: "settings-what" }, h("h2", { text: title }), h("p", { class: "note", text: about })),
    h("div", { class: "settings-controls" }, ...controls));
}

function form(data) {
  let held = data.schedule;
  let sending = false;
  let again = false;
  const briefingHour = hourPick("Morning briefing hour");
  const digestDay = h("select", { class: "field settings-pick", "aria-label": "Weekly digest day" },
    DAYS.map((name, index) => h("option", { value: index, text: name })), h("option", { value: "", text: "No digest" }));
  const digestHour = hourPick("Weekly digest hour");
  const said = h("p", { class: "note settings-said", role: "status" });

  const show = (schedule) => {
    briefingHour.value = String(schedule.briefing_hour);
    digestDay.value = schedule.digest_day === null ? "" : String(schedule.digest_day);
    digestHour.value = String(schedule.digest_hour);
    digestHour.disabled = schedule.digest_day === null;
  };
  // With the digest off its controls are not on the page, and the schedule keeps what Cockpit holds for it.
  const chosen = () => ({
    briefing_hour: Number(briefingHour.value),
    digest_day: data.digest ? (digestDay.value === "" ? null : Number(digestDay.value)) : held.digest_day,
    digest_hour: data.digest ? Number(digestHour.value) : held.digest_hour,
  });
  async function save() {
    if (sending) {
      again = true;
      return;
    }
    sending = true;
    said.textContent = "Saving…";
    said.classList.remove("fail");
    let failed = null;
    try {
      held = (await api("PUT", "/api/settings", chosen())).schedule;
    } catch (error) {
      logError("saving the settings", error);
      failed = error;
    }
    sending = false;
    if (again) {
      again = false;
      save();
      return;
    }
    show(held);
    said.textContent = failed ? `Not saved: ${failed.message}` : "Saved";
    said.classList.toggle("fail", failed !== null);
  }

  show(held);
  for (const pick of [briefingHour, digestDay, digestHour]) pick.addEventListener("change", save);
  digestDay.addEventListener("change", () => { digestHour.disabled = digestDay.value === ""; });

  const zone = data.zone.replaceAll("_", " ");
  // The digest's wait is briefing.DIGEST_WAIT_HOURS.
  return [
    row("Morning briefing", `The hour your briefing on the team comes each day, ${zone} time.`, briefingHour),
    data.digest && row("Weekly digest",
      "The day and hour of your catch-up on the week. It waits up to four hours for that day's briefing.",
      digestDay, digestHour),
    said,
  ];
}

async function load() {
  const holder = document.getElementById("settings");
  try {
    const data = await api("GET", "/api/settings");
    Loading.settle(holder);
    holder.replaceChildren(...form(data).filter(Boolean));
    document.getElementById("banner").replaceChildren();
  } catch (error) {
    logError("loading the settings", error);
    Loading.failed(holder);
    document.getElementById("banner").replaceChildren(
      h("div", { class: "banner fail", text: `Cockpit could not load the settings: ${error.message}` }));
  }
}

load();
