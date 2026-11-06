"use strict";

// The Usage page (SPEC SESSION-9 to SESSION-16). It leads with the weekly meters: each Claude account's share of
// its weekly limit and each Codex account's, as they last reported them, then a graph of Claude use, Codex use and steps done on
// each of the last 30 days. Below come the period's core numbers, counted from the Claude Code transcripts on the
// host (spend.py) and Codex's own session files (codex.py): Claude's total, the cost of cache misses, the share of
// input read from the cache and Codex's total as tiles, the cost by kind of token, where the use went (sessions,
// skills, what started the work, Codex sessions) and the work with the costliest cache misses as ranked bars. The
// full tables per model, session, skill and turn, and per Codex session, sit behind one disclosure. Claude use shows
// in one measure the user picks: the share of one account's weekly limit (the default), the cost at API list prices,
// or that share at the plan's price (400 % = $200); hovers give all three. Codex has no price, so its use shows as a
// share of its own weekly limit in every measure (SESSION-10). The tables sort by any column; the page checks for
// new numbers every minute.

const POLL_MS = 60e3;
// While Cockpit has just started and is still reading the transcripts, the page asks again this soon.
const COUNTING_MS = 5e3;
// "week" runs from the start of the earlier of the accounts' current weeks; the others are days back from now.
const PERIODS = ["week", "1", "7", "30"];
const PERIOD_WORDS = { 1: "24 hours", 7: "7 days", 30: "30 days" };
const WEEK_MS = 7 * 86400e3;
const TOP_ROWS = 25;
// The rows of a ranked list.
const RANK_ROWS = 6;
// Token kinds in the order a call uses them, with what each one is.
const KINDS = [
  ["fresh", "Uncached input", "Input charged at full price"],
  ["cache_write", "Cache writes", "Input written to the prompt cache: 1.25 times the input price to keep it 5 minutes, 2 times to keep it an hour."],
  ["cache_read", "Cache reads", "Cached input, charged at 5% of the input price"],
  ["output", "Output", "Includes thinking tokens"],
];
// What started a turn (spend.py ORIGINS): its name on the page and what it covers.
const ORIGINS = {
  typed: ["Typed prompt", "in a terminal, not sent by Cockpit or an agent"],
  user_cockpit: ["Your message on Cockpit", "your answers and replies to steps"],
  sweep: ["Cockpit woke the sweeper", "sweeps, checks and pauses, also from Update"],
  sweeper_asks: ["The sweeper asked a session", "mostly to get an item ready"],
  session_asks: ["Another session asked", "through Cockpit or Claude Code"],
  cockpit: ["Cockpit's own notice", "Run results, usage back after a limit, archived steps"],
  script: ["A script", "claude -p runs"],
  unknown: ["Prompt not read", "such as a copy of a session"],
};
// A session's kind (spend.py KINDS_OF_SESSION): its name on the page and what it covers.
const SESSION_KINDS = {
  sweeper: ["The sweeper", "the session Cockpit sends sweeps, checks and pauses"],
  sweeper_started: ["Started by the sweeper", "first prompt from the sweeper"],
  user_cockpit: ["Started by you on Cockpit", "first prompt your message on Cockpit"],
  session_started: ["Started by another session", "first prompt from another agent"],
  cockpit_started: ["Started by Cockpit", "first prompt one of Cockpit's own notices"],
  terminal: ["Started in a terminal", "first prompt typed"],
  script: ["Scripts", "claude -p runs"],
  unknown: ["Prompt not read", "no prompt of it was read"],
};
// How a Codex session started (codex.py): `codex exec`, the terminal, or a subagent of another session.
const CODEX_SOURCES = { exec: "codex exec", cli: "Terminal", subagent: "Subagent" };
// The most expensive turns shown before "Show all".
const TOP_TURN_ROWS = 10;
// The measures Claude use can show in. Each turns list-price dollars into its unit: the share of a week is
// list dollars over the list-price use one account's week allows, and the plan's price is that share in plan dollars.
const MEASURES = {
  week: {
    label: "% of a week", words: "Claude, as a share of one account's weekly limit", title: "The share of one account's weekly limit",
    of: (usd, data) => (usd / data.week_list_usd) * 100, show: percent,
  },
  api: {
    label: "API price", words: "Claude at API list prices", title: null,
    of: (usd) => usd, show: dollars,
  },
  plan: {
    label: "Plan price", words: "Claude at the plan's price", title: "The share of a week at the plan's price: 400 % is $200",
    of: (usd, data) => (usd / data.week_list_usd) * 100 * data.plan_usd_per_percent, show: dollars,
  },
};
// The measure the user picked last, shared with the Sessions list on the Cockpit page (app.js).
const MEASURE_KEY = "cockpit:usage-measure";
const ui = {
  // `period` is the one the buttons chose; `shownPeriod` the one `data` covers, whose words label it until the
  // chosen period's data is in.
  period: periodFromUrl(), shownPeriod: null, measure: measureChosen(), data: null, loads: 0,
  // Whether the load line runs for a period switch; the latest load ends it.
  lineOn: false,
  sort: { models: "value", origins: "value", session_kinds: "value", sessions: "value", skills: "value" },
  all: { models: false, origins: false, session_kinds: false, top: false, sessions: false, skills: false, codex: false },
  // The rows whose breakdown is open, and the day whose figures show under the charts; a poll's redraw keeps them.
  opened: new Set(), day: null,
};
let timer = null;

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

async function api(path) {
  const { response, data } = await readThroughRestart(path, { credentials: "same-origin" });
  if (response.status === 401) {
    location.href = "/auth/google";
    throw Object.assign(new Error("Signed out"), { status: 401 });
  }
  if (!response.ok) throw Object.assign(new Error(data.detail || `Cockpit answered ${response.status}`), { status: response.status });
  return data;
}

function periodFromUrl() {
  const period = new URLSearchParams(location.search).get("period");
  return PERIODS.includes(period) ? period : "week";
}

// The link's measure, else the one picked last in this browser, else the share of a week.
function measureChosen() {
  const linked = new URLSearchParams(location.search).get("measure");
  if (Object.hasOwn(MEASURES, linked)) return linked;
  try {
    const stored = localStorage.getItem(MEASURE_KEY);
    if (Object.hasOwn(MEASURES, stored)) return stored;
  } catch { /* storage blocked */ }
  return "week";
}

function fill(id, ...children) {
  Loading.settle(document.getElementById(id));
  document.getElementById(id).replaceChildren(...children.flat().filter(Boolean));
}

// A section's heading, as on the Cockpit page, with a line on how to read the section beside it.
function head(id, title, hint) {
  return h("div", { class: "section-head" }, h("h2", { id, text: title }), hint && h("span", { class: "hint", text: hint }));
}

// $6,349 / $138 / $4.20 / $0.02: cents only where they matter.
function dollars(usd) {
  const digits = usd < 10 ? 2 : 0;
  return `$${usd.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

// 276 % / 4.3 % / 0.04 %.
function percent(value) {
  const digits = value < 1 ? 2 : value < 10 ? 1 : 0;
  return `${value.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })} %`;
}

// A meter's share as it reported it: whole percents as 4 %, a fraction as percent() writes it.
function meterPercent(value) {
  return Number.isInteger(value) ? `${value} %` : percent(value);
}

// 12.1 B / 420 M / 52.2 M / 175 k / 950.
function tokens(count) {
  for (const [size, unit] of [[1e9, "B"], [1e6, "M"], [1e3, "k"]]) {
    if (count >= size) {
      const value = count / size;
      return `${value.toLocaleString("en-US", { maximumFractionDigits: value < 100 ? 1 : 0 })} ${unit}`;
    }
  }
  return String(count);
}

function moment(iso) {
  return new Date(iso).toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false });
}

function clock(iso) {
  return new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false });
}

// How long until a time: "45 min", "4 h 12 min", "3 d 22 h".
function countdown(iso) {
  const minutes = Math.max(0, Math.round((new Date(iso).getTime() - Date.now()) / 60000));
  if (minutes < 60) return `${minutes} min`;
  const [big, small, unit] = minutes < 1440
    ? [`${Math.floor(minutes / 60)} h`, minutes % 60, "min"]
    : [`${Math.floor(minutes / 1440)} d`, Math.floor((minutes % 1440) / 60), "h"];
  return small ? `${big} ${small} ${unit}` : big;
}

// List-price dollars in the measure the user picked.
function measured(usd) {
  const measure = MEASURES[ui.measure];
  return measure.show(measure.of(usd, ui.data));
}

// A bar of cost split by token kind; `scale` is the cost that fills the whole width. The widths go through
// the style object: Cockpit's content policy refuses style attributes.
function kindBar(use, scale) {
  return h("span", { class: "kind-bar", "aria-hidden": "true" }, KINDS.map(([kind]) => {
    const part = h("span", { class: `kind-${kind}` });
    part.style.width = `${scale ? (use.usd_by_kind[kind] / scale) * 100 : 0}%`;
    return part;
  }));
}

// The measure's cell in a table (SESSION-11): a bar split by token kind, which the largest row fills, then the
// figure. Its hover gives the row's part of all Claude use in the period.
function measureCell(use, most, sorted) {
  const total = ui.data.total.usd;
  return h("td", { class: sorted ? "measure-cell sorted" : "measure-cell", title: `${percent(total ? (use.usd / total) * 100 : 0)} of all Claude use in this period` },
    h("span", { class: "measure" }, kindBar(use, most), h("span", { class: "measure-figure", text: measured(use.usd) })));
}

// A number cell tinted by how close it comes to its column's largest value, so the big ones stand out (SESSION-11).
function heatCell(text, value, most, sorted) {
  const cell = h("td", { class: sorted ? "heat sorted" : "heat", text });
  cell.style.setProperty("--heat", String(most ? value / most : 0));
  return cell;
}

function useTitle(use) {
  return [
    `${percent(use.week_percent)} of one account's weekly limit`,
    `${dollars(use.usd)} at API list prices`,
    `${dollars(use.plan_usd)} at the plan's price`,
    "",
    ...KINDS.map(([kind, label]) => `${label}: ${tokens(use.tokens[kind])} tokens, ${measured(use.usd_by_kind[kind])}`),
    `${use.calls.toLocaleString("en-US")} calls`,
    `Subagents: ${measured(use.subagent_usd)}; after background tasks: ${measured(use.woken_usd)}`,
  ].join("\n");
}

// The parts of a cost that subagents, the work after background tasks and cache misses make up, as table columns.
const SHARE_COLUMNS = [
  { key: "subagent_usd", label: "Subagents", value: (row) => row.use.subagent_usd, show: measured },
  {
    key: "woken_usd", label: "After tasks",
    title: "Spent after a background task woke the session",
    value: (row) => row.use.woken_usd, show: measured,
  },
  {
    key: "miss_usd", label: "Cache misses",
    title: "Extra cost of rewriting cached input",
    value: (row) => row.use.miss_usd, show: measured,
  },
];

// One weekly meter: the share used, a bar filled to it with a tick where the clock is, coloured by pace as in the
// header (usage.py pace), and when it resets. `used` is the figure the account reports; the bar shows `fill`, its
// share of all the account's room (SPEC UI-76).
function meter({ name, plan, used, fill: filled, elapsed, pace, line, title }) {
  const bar = h("span", { class: "meter-bar", "aria-hidden": "true" });
  if (used !== null) {
    const fill = h("span", { class: "meter-used" });
    fill.style.width = `${Math.min(100, filled)}%`;
    const tick = h("span", { class: "meter-tick" });
    tick.style.left = `${elapsed}%`;
    bar.append(fill, tick);
  }
  return h("div", { class: `meter ${pace || "unknown"}`, title },
    h("span", { class: "meter-name" }, h("span", { class: "meter-who", text: name }), plan && h("span", { class: "meter-plan", text: plan })),
    h("span", { class: "spend-figure", text: used === null ? "–" : meterPercent(used) }),
    bar,
    h("span", { class: "meter-line", text: line }));
}

// Several accounts' meters added up. Each account allows 100 % a week and each banked reset 100 % more.
function sumMeter(name, used, count, banked) {
  return h("div", {
    class: "meter sum",
  },
    h("span", { class: "meter-name" }, h("span", { class: "meter-who", text: name })),
    h("span", { class: "spend-figure", text: meterPercent(used) }),
    h("span", { class: "meter-line", text: `of ${(count + banked) * 100} % for ${count} accounts${banked ? ` and ${banked === 1 ? "1 banked reset" : `${banked} banked resets`}` : ""}` }));
}

function weekLine(week) {
  if (week.used === null) return "No use since its week reset";
  const gone = week.counted_until ? `${week.elapsed} % of the time until ${moment(week.counted_until)} gone` : `${week.elapsed} % of its week gone`;
  const room = week.banked ? `with ${week.banked === 1 ? "1 banked reset" : `${week.banked} banked resets`}, ${week.room_used} % of all its room · ` : "";
  return `${room}${gone} · resets ${moment(week.resets_at)}, in ${countdown(week.resets_at)}`;
}

function renderLimits(data) {
  const { accounts, error } = data.limits;
  const weeks = accounts.map((account) => ({ account, week: account.windows.find((window) => window.key === "seven_day") }));
  const known = weeks.filter(({ week }) => week.used !== null);
  const sum = known.reduce((total, { week }) => total + week.used, 0);
  const codex = data.codex.accounts;
  const codexSum = codex.reduce((total, { week }) => total + (week && week.used !== null ? week.used : 0), 0);
  // Claude's accounts on one row and Codex's on the next, each with its sum first.
  const claudeCards = [
    accounts.length > 1 && sumMeter("Claude, all accounts", sum, accounts.length, known.reduce((total, { week }) => total + week.banked, 0)),
    ...weeks.map(({ account, week }) => meter({
      name: account.email, plan: account.plan, used: week.used, fill: week.room_used, elapsed: week.elapsed, pace: week.pace,
      line: weekLine(week),
      title: `Reported ${moment(account.captured_at)}`,
    })),
  ];
  const codexCards = [
    codex.length > 1 && sumMeter("Codex, all accounts", codexSum, codex.length, codex.reduce((total, { week }) => total + (week ? week.banked : 0), 0)),
    ...codex.map(({ email, folder, week }) => week
      ? meter({
          name: `Codex · ${email || folder}`, plan: week.plan && `${week.plan[0].toUpperCase()}${week.plan.slice(1)}`, used: week.used, fill: week.room_used,
          elapsed: week.elapsed, pace: week.pace, line: weekLine(week),
          title: `Codex's own weekly limit\nReported ${moment(week.captured_at)}`,
        })
      : meter({
          name: `Codex · ${email || folder}`, plan: "", used: null, fill: null, elapsed: null, pace: null,
          line: "Not reported yet", title: `Codex's own weekly limit\nNo Codex session has read it yet`,
        })),
  ];
  fill("spend-limits",
    head("limits-title", "Weekly limits", null),
    error ? h("p", { class: "note", text: `The Claude meters did not load: ${error}` }) : null,
    !accounts.length && !error ? h("p", { class: "note", text: "Run a Claude session to report its limits" }) : null,
    h("div", { class: "meters" }, claudeCards),
    preferenceRow(accounts),
    codex.length ? h("div", { class: "meters" }, codexCards) : null);
}

// Which account new sessions start on (SPEC SESSION-17), and the user's choice to send them to one account for a
// while ahead of the usual order: the server lists that account first and says until when.
const PREFER_FOR = [[30, "30 min"], [120, "2 h"], [1440, "1 day"]];

function preferenceRow(accounts) {
  const folders = accounts.filter((account) => account.folder);
  if (folders.length < 2) return null;
  // The accounts come in the picker's order, signed-out ones last, so the first signed-in one is where new sessions
  // start; the chosen one is not first while it is at a limit or signed out (SPEC UI-37).
  const starting = folders.find((account) => !account.signed_out);
  const chosen = folders.find((account) => account.preferred_until);
  const until = (account) => `${clock(account.preferred_until)} (in ${countdown(account.preferred_until)})`;
  const status = !starting ? "No new session can start: every Claude account is signed out. Sign one in again."
    : starting === chosen ? `New sessions start on ${starting.folder}, your choice until ${until(chosen)}.`
    : chosen ? `New sessions start on ${starting.folder} while ${chosen.folder}, your choice until ${until(chosen)}, ${chosen.signed_out ? "is signed out" : "is at its usage limit"}.`
    : `New sessions start on ${starting.folder}, by the usual order.`;
  return h("div", { class: "prefer" },
    h("span", { class: "prefer-status", text: status }),
    folders.map((account) => h("span", { class: "prefer-account" },
      h("span", { class: "prefer-label", text: `Prefer ${account.folder} for` }),
      h("span", { class: "seg", role: "group", "aria-label": `Prefer ${account.folder} for` },
        // The length chosen shows pressed with a cross and a bar for how much of it has passed: pressing it again
        // undoes the choice, another replaces it.
        PREFER_FOR.map(([minutes, words]) => account.preferred_minutes === minutes
          ? h("button", {
            type: "button", class: "prefer-chosen", "aria-pressed": "true",
            title: `Undo: new sessions start by the usual order again`, onclick: () => prefer(null, 0),
          }, words, h("span", { class: "prefer-undo", "aria-hidden": "true", text: "×" }),
          preferPassed(account, "prefer-passed"))
          : h("button", {
            type: "button", text: words, "aria-pressed": "false",
            title: `New sessions start here for the next ${words}, while it has room`,
            onclick: () => prefer(account.folder, minutes),
          }))))),
    chosen && h("button", { type: "button", class: "link-button", text: "Back to the usual order", onclick: () => prefer(null, 0) }));
}

// The bar along the bottom of the chosen length's button, filled by how much of that length has passed.
function preferPassed(account, cls) {
  const left = (new Date(account.preferred_until).getTime() - Date.now()) / 60000;
  const strip = h("span", { class: cls, "aria-hidden": "true" });
  strip.style.setProperty("--passed", `${Math.min(100, Math.max(0, 100 * (1 - left / account.preferred_minutes))).toFixed(1)}%`);
  return strip;
}

// One choice goes at a time, in the order made, so a retried earlier one cannot land after a later one.
let preferring = Promise.resolve();
// A choice waiting here is in no outbox yet, so leaving asks first until it went.
function prefer(account, minutes) {
  preferring = preferring.then(() => sendPreference(account, minutes));
  holdLeave(preferring);
  return preferring;
}

async function sendPreference(account, minutes) {
  try {
    const { response, data } = await sendAction("/api/accounts/prefer", {
      method: "POST", credentials: "same-origin", headers: { "content-type": "application/json" },
      body: JSON.stringify({ account, minutes }),
    }, TRY_MS, null);
    if (!response.ok) throw Object.assign(new Error(data.detail || `Cockpit answered ${response.status}`), { status: response.status });
  } catch (error) {
    logError("choosing the account for new sessions", error);
    document.getElementById("banner").replaceChildren(h("div", { class: "banner fail", text: `The choice did not save: ${error.message}` }));
    return;
  }
  load();
}

// The weekly resets inside the period, per account, newest first: the account's week starts back to `since`.
function resetsIn(data) {
  const since = new Date(data.since).getTime();
  return data.limits.accounts.filter((account) => account.week_start).flatMap((account) => {
    const resets = [];
    for (let start = new Date(account.week_start).getTime(); start > since; start -= WEEK_MS) resets.push({ email: account.email, at: start });
    return resets;
  });
}

// Why a period's share of a week can pass 100 % per account, or how this week's estimate compares with the meters.
function periodNote(data, use) {
  const accounts = data.limits.accounts.length;
  if (ui.measure !== "week" || !accounts) return null;
  const estimate = MEASURES.week.of(use.usd, data);
  if (ui.shownPeriod === "week") {
    const meters = data.limits.accounts.map((account) => account.windows.find((window) => window.key === "seven_day").used);
    if (meters.some((used) => used === null)) return null;
    const sum = meters.reduce((total, used) => total + used, 0);
    return `The meters above add up to ${meterPercent(sum)}; the transcripts give ${percent(estimate)}, at ${dollars(data.week_list_usd)} of list-price use per 100 %.`;
  }
  const resets = resetsIn(data);
  if (!resets.length) return null;
  const perAccount = new Map();
  for (const reset of resets) perAccount.set(reset.email, [...(perAccount.get(reset.email) || []), reset.at]);
  const when = [...perAccount].map(([email, times]) => {
    const last = moment(new Date(times[0]).toISOString());
    return times.length === 1 ? `${email}'s on ${last}` : `${email}'s ${times.length} times, last on ${last}`;
  }).join("; ");
  return `Each account allows 100 % a week, and each weekly reset inside the period adds 100 % (${when}), `
    + "counted back a week at a time from each account's current week. This week compares directly with the meters.";
}

// What the accounts allowed in the period, in % of one account's week: 100 % per account, and 100 % more for each
// weekly reset inside a trailing period. This week counts the accounts' current weeks, as the meters do.
function available(data) {
  const accounts = data.limits.accounts.length;
  if (!accounts) return null;
  return (accounts + (ui.shownPeriod === "week" ? 0 : resetsIn(data).length)) * 100;
}

// The total's label. In the share of a week it says what the accounts allowed, so the share reads against it:
// 246 % of the 400 % two accounts allowed in 7 days with two resets in them.
function totalLabel(data) {
  const allowed = ui.measure === "week" && available(data)?.toLocaleString("en-US");
  if (!allowed) return MEASURES[ui.measure].words;
  return ui.shownPeriod === "week"
    ? `Claude, of the ${allowed} % the accounts allow this week`
    : `Claude, of the ${allowed} % the accounts allowed in these ${PERIOD_WORDS[ui.shownPeriod]}`;
}

// One tile of the period's core numbers: a large figure, what it is, and a line that says more. A tile with a
// breakdown (`more`, else null) is a button that opens it under its line, as the ranked rows do; `key` names it.
function tile(figure, label, line, more, key) {
  const parts = [h("span", { class: "spend-figure", text: figure }), h("span", { class: "spend-label", text: label }),
    h("span", { class: "tile-line", text: line })];
  if (!more) return h("div", { class: "spend-readout" }, parts);
  const open = ui.opened.has(key);
  return h("button", { type: "button", class: "spend-readout", "aria-expanded": String(open), "data-focus": key, onclick: () => toggle(key) },
    parts, open && h("span", { class: "breakdown", text: more }));
}

// Codex's use in the period as a share of its own weekly limit, which it often resets early, so a period can pass
// 100 %. The line gives its calls and how much of its input came from the cache.
function codexTile(codex) {
  const { fresh, cached, output } = codex.total;
  const line = codex.calls
    ? `${codex.calls.toLocaleString("en-US")} calls in ${codex.sessions.length} sessions; ${shareOf(cached, fresh + cached)} of the input read from the cache, ${tokens(output)} tokens out.`
    : "Choose a longer period to find Codex use";
  // Each Codex account allows 100 % a week, and Codex resets a week early at times, so a period can hold several
  // weeks: the label says what they allow together.
  const label = codex.weeks > 1
    ? `Codex, of the ${codex.weeks * 100} % ${codex.accounts.length > 1 ? `its ${codex.accounts.length} accounts' ${codex.weeks} weeks` : `its ${codex.weeks} weeks`} in this period allow`
    : "Codex, of its own weekly limit";
  const readout = tile(percent(codex.week_percent), label, line, [
    CODEX_SHARE, "",
    ...CODEX_KINDS.map(([kind, label]) => `${label}: ${tokens(codex.total[kind])} tokens`),
  ].join("\n"), "tile:codex");
  readout.classList.add("codex");
  return readout;
}

// The share a part makes of a whole, as a percent.
function shareOf(part, whole) {
  return percent(whole ? (part / whole) * 100 : 0);
}

function renderTotals(data) {
  const use = data.total;
  const turns = data.sessions.reduce((sum, row) => sum + row.turns, 0);
  const note = periodNote(data, use);
  const input = use.tokens.fresh + use.tokens.cache_write + use.tokens.cache_read;
  fill("spend-totals",
    h("div", { class: "tiles" },
      tile(measured(use.usd), totalLabel(data),
        `${use.calls.toLocaleString("en-US")} calls in ${turns.toLocaleString("en-US")} turns of ${data.sessions.length} sessions. `
        + `Subagents ${shareOf(use.subagent_usd, use.usd)} of it, work after background tasks ${shareOf(use.woken_usd, use.usd)}.`,
        useTitle(use), "tile:claude"),
      tile(measured(use.miss_usd), "lost to cache misses",
        `${use.misses.toLocaleString("en-US")} misses, ${shareOf(use.miss_usd, use.usd)} of all use; `
        + `${shareOf(use.paused_miss_usd, use.miss_usd)} of it after a pause of 5 minutes or more.`,
        `${tokens(use.miss_tokens)} tokens written to the cache again\n${dollars(use.miss_usd)} at API list prices over reading them back`, "tile:misses"),
      tile(shareOf(use.tokens.cache_read, input), "of the input read from the cache",
        `${tokens(use.tokens.cache_read)} tokens read back, ${tokens(use.tokens.cache_write)} written to it, ${tokens(use.tokens.fresh)} uncached.`, null, null),
      codexTile(data.codex)),
    note && h("p", { class: "spend-note", text: note }),
    h("div", { class: "kinds" },
      h("h3", { class: "card-title", text: "Cost by kind of token" }),
      kindBar(use, use.usd),
      h("ul", { class: "kinds-legend" }, KINDS.map(([kind, label, meaning]) => h("li", { title: meaning },
        h("span", { class: `kind-key kind-${kind}`, "aria-hidden": "true" }),
        h("span", { class: "kinds-name", text: label }),
        h("span", { class: "kinds-figure", text: `${measured(use.usd_by_kind[kind])} · ${tokens(use.tokens[kind])} tokens` }))))));
}

// Opens or closes the breakdown under a row. Breakdowns open on a press rather than in a hover, so they work on a
// phone and stay while the user reads them.
function toggle(key) {
  if (ui.opened.has(key)) ui.opened.delete(key);
  else ui.opened.add(key);
  render();
}

// A row's breakdown: lines of figures, a blank line between groups.
function breakdown(text) {
  return h("div", { class: "breakdown", text });
}

// A ranked list (SESSION-16): per entry its name and figure over a bar that the list's largest entry fills. Each
// row carries `value`, which sizes its bar, `figure`, the value as the list shows it, and `key` and `more`, its
// breakdown, which a press on the row opens.
function rankList(rows, empty) {
  if (!rows.length) return h("p", { class: "note", text: empty });
  const most = Math.max(...rows.map((row) => row.value));
  return h("ol", { class: "spend-rank" }, rows.map((row) => {
    const fillBar = h("span", { class: "rank-fill" });
    fillBar.style.width = `${most ? (row.value / most) * 100 : 0}%`;
    const open = ui.opened.has(row.key);
    return h("li", {},
      h("button", { type: "button", class: "rank-row", "aria-expanded": String(open), "data-focus": row.key, onclick: () => toggle(row.key) },
        h("span", { class: "rank-name", text: row.name }),
        h("span", { class: "rank-figure", text: row.figure }),
        h("span", { class: "rank-bar", "aria-hidden": "true" }, fillBar),
        row.meta && h("span", { class: "rank-meta", text: row.meta })),
      open && breakdown(row.more));
  }));
}

// A card with a small title; `table` names the section below that holds all its rows, `kind` styles it.
function card(title, body, table, kind) {
  return h("div", { class: kind ? `spend-card ${kind}` : "spend-card" },
    h("h3", { class: "card-title", text: title }),
    body,
    table && h("button", { type: "button", class: "link-button card-more", onclick: () => openTable(table) }, "All in the table"));
}

// Opens the tables and scrolls to one of them. The tables open at once rather than growing open (motion.css), since a
// growing section is still short when the page scrolls.
function openTable(id) {
  const more = document.getElementById("spend-more");
  more.classList.add("at-once");
  more.open = true;
  document.getElementById(id).scrollIntoView({ block: "start" });
  more.classList.remove("at-once");
}

function renderWhere(data) {
  const rows = (list, row) => list.slice(0, RANK_ROWS).map((entry) => ({
    value: entry.use.usd, figure: measured(entry.use.usd), more: useTitle(entry.use), ...row(entry),
  }));
  fill("spend-where",
    head("where-title", "Where it went", null),
    h("div", { class: "cards" },
      card("Sessions", rankList(rows(data.sessions, (row) => ({
        key: `where-session:${row.session_id}`, name: row.name || row.session_id,
        meta: `${(SESSION_KINDS[row.kind] || [row.kind])[0]} · ${row.turns} turns`,
      })), "Choose a longer period to find sessions"), "spend-sessions"),
      card("Skills", rankList(rows(data.skills, (row) => ({
        key: `where-skill:${row.skill}`, name: row.skill || "No skill loaded", meta: `${row.turns} turns in ${row.sessions} sessions`,
      })), "Choose a longer period to find skill use"), "spend-skills"),
      card("What started the work", rankList(rows(data.origins, (row) => ({
        key: `where-origin:${row.origin}`, name: (ORIGINS[row.origin] || [row.origin])[0], meta: `${row.turns} turns in ${row.sessions} sessions`,
      })), "Choose a longer period to find work"), "spend-origins"),
      card("Codex sessions", rankList(data.codex.sessions.slice(0, RANK_ROWS).map((row) => ({
        value: row.week_percent, figure: percent(row.week_percent), name: folder(row.cwd) || row.session_id,
        key: `where-codex:${row.session_id}`, more: codexTitle(row), meta: `${codexAccount(data.codex, row)}${CODEX_SOURCES[row.source] || row.source || "unknown start"} · ${row.calls} calls · from ${moment(row.first)}`,
      })), "Choose a longer period to find Codex use"), "spend-codex", "codex")));
}

// What a cache miss is, in the words spend.py counts it by.
const MISS_DEFINITION = "A cache miss is a call that wrote the conversation to the prompt cache again, at least half of what the call "
  + "before it had cached, instead of reading it back at a twentieth of the input price. It happens when the conversation paused "
  + "longer than the cache keeps it (5 minutes or an hour) or the start of the prompt changed. Its cost here is what writing "
  + "those tokens again cost over reading them back. A session's first call, and the first after a compaction, start a new "
  + "prompt and are no miss.";

function renderMisses(data) {
  const use = data.total;
  const names = new Map(data.sessions.map((row) => [row.session_id, row.name || row.session_id]));
  const sessions = data.sessions.filter((row) => row.use.misses).sort((a, b) => b.use.miss_usd - a.use.miss_usd).slice(0, RANK_ROWS);
  const missTitle = (missed) => `${missed.misses} cache misses, ${tokens(missed.miss_tokens)} tokens written again\n`
    + `${dollars(missed.miss_usd)} at API list prices over reading them back, ${dollars(missed.paused_miss_usd)} of it after a pause`;
  fill("spend-misses",
    head("misses-title", "Cache misses", `${use.misses.toLocaleString("en-US")} in this period, ${shareOf(use.miss_usd, use.usd)} of all Claude use.`),
    h("p", { class: "spend-define", text: MISS_DEFINITION }),
    h("div", { class: "cards two" },
      card("Sessions with the costliest misses", rankList(sessions.map((row) => ({
        value: row.use.miss_usd, figure: measured(row.use.miss_usd), name: row.name || row.session_id,
        key: `miss-session:${row.session_id}`, more: missTitle(row.use),
        meta: `${row.use.misses} misses · ${shareOf(row.use.miss_usd, row.use.usd)} of its use`,
      })), "No cache misses in this period.")),
      card("Turns with the costliest misses", rankList(data.miss_turns.slice(0, RANK_ROWS).map((turn) => ({
        value: turn.use.miss_usd, figure: measured(turn.use.miss_usd), name: names.get(turn.session_id) || turn.session_id,
        key: `miss-turn:${turn.session_id}:${turn.start}`, more: `${turn.prompt}\n\n${missTitle(turn.use)}`,
        meta: `${moment(turn.start)} · ${turn.use.misses} misses · ${promptLine(turn.prompt) || "no prompt"}`,
      })), "No cache misses in this period."))));
}

// The fields of a use, besides the token kinds, that add up.
const SUMMED = ["usd", "week_percent", "plan_usd", "calls", "subagent_usd", "woken_usd"];

// The hours summed into days of this browser's time zone, oldest first.
function localDays(hours) {
  const days = new Map();
  for (const { hour, use } of hours) {
    const start = new Date(hour);
    const key = `${start.getFullYear()}-${start.getMonth()}-${start.getDate()}`;
    if (!days.has(key)) {
      days.set(key, { start, use: { ...Object.fromEntries(SUMMED.map((field) => [field, 0])), tokens: Object.fromEntries(KINDS.map(([kind]) => [kind, 0])), usd_by_kind: Object.fromEntries(KINDS.map(([kind]) => [kind, 0])) } });
    }
    const day = days.get(key).use;
    for (const field of SUMMED) day[field] += use[field];
    for (const [kind] of KINDS) {
      day.tokens[kind] += use.tokens[kind];
      day.usd_by_kind[kind] += use.usd_by_kind[kind];
    }
  }
  return [...days.values()];
}

// The graph per day (SESSION-14): Claude use and the steps done on each of the last days, two charts over one
// row of days, so a day lines up in both. A day's hover gives its figures from both and lights its column in both.
function renderDaily(data) {
  const { since, hours, steps } = data.daily;
  const dayKey = (date) => `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
  // The day `since` falls in is counted only in part, so the graph starts at the next midnight here.
  const first = new Date(since);
  first.setHours(24, 0, 0, 0);
  const days = [];
  for (let start = first; start <= new Date(); start = new Date(start.getFullYear(), start.getMonth(), start.getDate() + 1)) {
    days.push({ start, use: null, codex: 0, done: 0, dismissed: 0 });
  }
  const byKey = new Map(days.map((day) => [dayKey(day.start), day]));
  for (const day of localDays(hours)) {
    const row = byKey.get(dayKey(day.start));
    if (row) row.use = day.use;
  }
  for (const hour of data.daily.codex_hours) {
    const row = byKey.get(dayKey(new Date(hour.hour)));
    if (row) row.codex += hour.week_percent;
  }
  for (const step of steps) {
    const row = byKey.get(dayKey(new Date(step.closed_at)));
    if (row) row[step.status === "done" ? "done" : "dismissed"] += 1;
  }
  const measure = MEASURES[ui.measure];
  const useOf = (day) => (day.use ? measure.of(day.use.usd, data) : 0);
  const columns = [];
  const light = (at) => { for (const column of columns) column.classList.toggle("lit", column.dataset.day === String(at)); };
  const chosen = days.find((day) => dayKey(day.start) === ui.day);
  const choose = (day) => { ui.day = dayKey(day.start) === ui.day ? null : dayKey(day.start); render(); };
  const chart = (kind, title, valueOf, tick) => {
    const top = niceTop(Math.max(0, ...days.map(valueOf)));
    return h("div", { class: `daily-chart ${kind}` },
      h("div", { class: "daily-title", text: title }),
      h("div", { class: "daily-plot" },
        h("div", { class: "daily-axis", "aria-hidden": "true" }, [top, top / 2, 0].map((at) => h("span", { text: tick(at) }))),
        h("div", { class: "daily-days", onpointerleave: () => light(-1) }, days.map((day, at) => {
          const bar = h("span", { class: "daily-bar" });
          bar.style.height = `${(valueOf(day) / top) * 100}%`;
          const column = h("button", {
            type: "button", class: at === days.length - 1 ? "daily-day today" : "daily-day", "data-day": at, "data-focus": `${kind}:${at}`,
            "aria-label": day.start.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" }),
            "aria-pressed": String(day === chosen), onpointerenter: () => light(at), onclick: () => choose(day),
          }, bar);
          columns.push(column);
          return column;
        }))));
  };
  fill("spend-daily",
    head("daily-title", `The last ${days.length} days`, "Local time"),
    h("div", { class: "daily" },
      chart("use", measure.words, useOf, (at) => (ui.measure === "week" ? `${axisFigure(at)} %` : `$${axisFigure(at)}`)),
      chart("codex", "Codex, as a share of its own weekly limit", (day) => day.codex, (at) => `${axisFigure(at)} %`),
      // A half step has no column of its own, so its gridline goes unlabelled.
      chart("steps", "Steps done", (day) => day.done, (at) => (Number.isInteger(at) ? String(at) : "")),
      h("div", { class: "daily-dates", "aria-hidden": "true" }, days.flatMap((day, at) => {
        if (day.start.getDay() !== 1) return [];
        const label = h("span", { class: at > days.length - 5 ? "end" : null, text: day.start.toLocaleDateString(undefined, { day: "numeric", month: "short" }) });
        label.style.setProperty("--at", String(at));
        label.style.setProperty("--days", String(days.length));
        return [label];
      })),
      chosen && breakdown(dayTitle(chosen))));
}

// A day's figures, shown under the charts once the user presses its column: its Claude use in each measure, Codex's, the steps closed, and use per step done.
function dayTitle(day) {
  const { use, codex, done, dismissed } = day;
  const today = day.start.toDateString() === new Date().toDateString();
  return [
    `${day.start.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" })}${today ? ", so far" : ""}`,
    use ? `Claude: ${percent(use.week_percent)} of a week, ${dollars(use.usd)} at API list prices` : "No Claude use",
    codex ? `Codex: ${percent(codex)} of its own week` : "No Codex use",
    `Steps done: ${done}; dismissed: ${dismissed}`,
    use && done ? `Claude per step done: ${percent(use.week_percent / done)} of a week, ${dollars(use.usd / done)} at API list prices` : null,
  ].filter(Boolean).join("\n");
}

// The top of a chart's scale: the largest value rounded up to 1, 2, 2.5, 4, 5, 6, 8 or 10 times a power of ten,
// so the tallest column fills most of the height and the half line still falls on a round figure.
function niceTop(value) {
  if (!(value > 0)) return 1;
  const power = 10 ** Math.floor(Math.log10(value));
  return [1, 2, 2.5, 4, 5, 6, 8, 10].map((step) => step * power).find((top) => top >= value * (1 - 1e-9));
}

// An axis figure: 2,000 / 25 / 2.5 / 0.25.
function axisFigure(value) {
  return value.toLocaleString("en-US", { maximumFractionDigits: value < 1 ? 2 : value < 10 ? 1 : 0 });
}

// A sortable table of rows that carry `use`; `label` heads the name column, `lead` gives a row's name cell,
// `extra` the columns that follow the measure. The measure comes first, as a bar, and the other numbers are
// tinted by size. The measure's columns sort by list price, which orders them the same way in every measure.
function useTable(name, label, rows, keyOf, lead, extra) {
  const measure = MEASURES[ui.measure];
  const columns = [
    { key: "value", label: measure.label, title: measure.title, value: (row) => row.use.usd, show: measured },
    ...extra,
    { key: "per_turn", label: "Per turn", value: (row) => row.use.usd / Math.max(1, row.turns), show: measured },
    ...SHARE_COLUMNS,
    ...KINDS.map(([kind, kindLabel, meaning]) => ({ key: kind, label: kindLabel, title: meaning, value: (row) => row.use.tokens[kind], show: tokens })),
  ];
  const most = new Map(columns.map((column) => [column, Math.max(0, ...rows.map(column.value))]));
  const sortBy = columns.find((column) => column.key === ui.sort[name]) || columns[0];
  const sorted = [...rows].sort((a, b) => sortBy.value(b) - sortBy.value(a));
  const shown = ui.all[name] ? sorted : sorted.slice(0, TOP_ROWS);
  const header = (column) => h("th", { scope: "col", title: column.title, "aria-sort": column === sortBy ? "descending" : null },
    h("button", { type: "button", class: "sort-key", onclick: () => { ui.sort[name] = column.key; render(); } }, column.label));
  const cell = (column, row) => column === columns[0]
    ? measureCell(row.use, most.get(column), column === sortBy)
    : heatCell(column.show(column.value(row)), column.value(row), most.get(column), column === sortBy);
  return [
    h("div", { class: "table-scroll" },
      h("table", { class: `spend-table ${name}-table` },
        h("thead", {}, h("tr", {}, h("th", { scope: "col", class: "name-col", text: label }), columns.map(header))),
        h("tbody", {}, shown.map((row) => openRow(`${name}:${keyOf(row)}`, columns.length + 1, useTitle(row.use), lead(row),
          columns.map((column) => cell(column, row))))))),
    showAll(name, rows.length, TOP_ROWS),
  ];
}

// A table row whose name cell opens its breakdown in a row of its own beneath it.
function openRow(key, width, more, name, cells) {
  const open = ui.opened.has(key);
  const cell = h("td", { colspan: width }, breakdown(more));
  return [
    h("tr", {}, h("th", { scope: "row", class: "name-col" },
      h("button", { type: "button", class: "row-open", "aria-expanded": String(open), "data-focus": key, onclick: () => toggle(key) }, name)), cells),
    open && h("tr", { class: "breakdown-row" }, cell),
  ];
}

function showAll(name, count, top) {
  return count > top && h("button", { type: "button", class: "link-button", onclick: () => { ui.all[name] = !ui.all[name]; render(); } },
    ui.all[name] ? "Show fewer" : `Show all ${count}`);
}

const TURNS_COLUMN = { key: "turns", label: "Turns", title: "Prompts that started work", value: (row) => row.turns, show: String };
const SESSIONS_COLUMN = { key: "sessions", label: "Sessions", value: (row) => row.sessions, show: String };

// A row's name and what it covers, from one of the label maps above.
function named(labels, key) {
  const [name, meaning] = labels[key] || [key, ""];
  return [h("span", { class: "row-name", text: name }), h("span", { class: "row-meta", text: meaning })];
}

// A model's name from the part of its id that prices it (spend.py PRICES): "haiku-4-5" is Haiku 4.5.
function modelName(model) {
  const [family, ...version] = model.split("-");
  return `${family[0].toUpperCase()}${family.slice(1)} ${version.join(".")}`;
}

// $4 / $0.10 / $0.01: a price per million tokens.
function perMillion(usd) {
  return `$${Number.isInteger(usd) ? usd : usd.toFixed(2)}`;
}

function renderModels(data) {
  fill("spend-models",
    head("models-title", "By model", "Prices are per million tokens; includes subagents"),
    data.models.length
      ? useTable("models", "Model", data.models, (row) => row.model,
          (row) => [h("span", { class: "row-name", text: modelName(row.model) }),
            h("span", { class: "row-meta", text: `${perMillion(row.prices.input)} in · ${perMillion(row.prices.output)} out · ${perMillion(row.prices.cache_read)} cache read` })],
          [{ ...TURNS_COLUMN, title: "Turns with a call to this model" }, SESSIONS_COLUMN])
      : h("p", { class: "note", text: "Choose a longer period to find Claude use" }));
}

function renderOrigins(data) {
  fill("spend-origins",
    head("origins-title", "What started the work", "Each turn counts under the prompt that started it, with its subagents and the work after its background tasks finished."),
    data.origins.length
      ? useTable("origins", "Started by", data.origins, (row) => row.origin, (row) => named(ORIGINS, row.origin), [TURNS_COLUMN, SESSIONS_COLUMN])
      : h("p", { class: "note", text: "Choose a longer period to find work" }));
}

function renderSessionKinds(data) {
  fill("spend-session-kinds",
    head("session-kinds-title", "Kinds of session", "A session's kind comes from its first prompt; the sweeper is the session Cockpit wakes."),
    data.session_kinds.length
      ? useTable("session_kinds", "Kind", data.session_kinds, (row) => row.kind, (row) => named(SESSION_KINDS, row.kind), [
          { key: "sessions", label: "Sessions", value: (row) => row.sessions, show: String },
          TURNS_COLUMN,
        ])
      : h("p", { class: "note", text: "Choose a longer period to find sessions" }));
}

// The prompt without the line Cockpit puts before a message, on one line, with a markdown link shown as
// its words, also when the stored prompt is cut inside the link's address.
function promptLine(prompt) {
  return prompt.replace(/^Message from [^\n]*\n\n/, "").replace(/\[([^\]\n]+)\]\([^)\s]*(\)|$)/g, "$1")
    .replace(/\s+/g, " ").trim();
}

function renderTop(data) {
  const measure = MEASURES[ui.measure];
  const names = new Map(data.sessions.map((row) => [row.session_id, row.name || row.session_id]));
  const turns = ui.all.top ? data.top_turns : data.top_turns.slice(0, TOP_TURN_ROWS);
  const most = Math.max(0, ...data.top_turns.map((turn) => turn.use.usd));
  const mostShare = new Map(SHARE_COLUMNS.map((column) => [column, Math.max(0, ...data.top_turns.map(column.value))]));
  const columns = [
    [measure.label, measure.title, null], ...SHARE_COLUMNS.map((column) => [column.label, column.title, null]),
    ["Started by", null, "text-col"], ["Skills", null, "text-col"],
  ];
  fill("spend-top",
    head("top-title", "Biggest turns", "One prompt and the work it started"),
    turns.length
      ? [
          h("div", { class: "table-scroll" },
            h("table", { class: "spend-table top-table" },
              h("thead", {}, h("tr", {}, h("th", { scope: "col", class: "name-col", text: "Turn" }),
                columns.map(([label, title, cls]) => h("th", { scope: "col", class: cls, title, text: label })))),
              h("tbody", {}, turns.map((turn) => openRow(`top:${turn.session_id}:${turn.start}`, columns.length + 1,
                `${turn.prompt}\n\n${useTitle(turn.use)}`,
                [h("span", { class: "row-name", text: names.get(turn.session_id) || turn.session_id }),
                  h("span", { class: "row-meta", text: `${moment(turn.start)} · ${promptLine(turn.prompt) || "no prompt"}` })],
                [measureCell(turn.use, most, true),
                SHARE_COLUMNS.map((column) => heatCell(column.show(column.value(turn)), column.value(turn), mostShare.get(column), false)),
                h("td", { class: "text-col", text: (ORIGINS[turn.origin] || [turn.origin])[0] }),
                h("td", { class: turn.skills.length ? "text-col" : "text-col none", text: turn.skills.join(", ") || "none" })]))))),
          showAll("top", data.top_turns.length, TOP_TURN_ROWS),
        ]
      : h("p", { class: "note", text: "Choose a longer period to find work" }));
}

function folder(cwd) {
  return cwd.split("/").filter(Boolean).pop() || cwd;
}

function renderSessions(data) {
  fill("spend-sessions",
    head("sessions-title", "Sessions", "Includes each session's subagents"),
    data.sessions.length
      ? useTable("sessions", "Session", data.sessions, (row) => row.session_id,
          (row) => [h("span", { class: "row-name", text: row.name || row.session_id }),
            h("span", { class: "row-meta", text: `${(SESSION_KINDS[row.kind] || [row.kind])[0]} · ${folder(row.cwd)} · ${row.session_id.slice(0, 8)} · last call ${moment(row.last)}` })],
          [TURNS_COLUMN])
      : h("p", { class: "note", text: "Choose a longer period to find sessions" }));
}

function renderSkills(data) {
  fill("spend-skills",
    head("skills-title", "Skills", "A skill counts the turns that loaded it, from that prompt to the next. A turn that loaded two skills splits its use between them."),
    data.skills.length
      ? useTable("skills", "Skill", data.skills, (row) => row.skill,
          (row) => [h("span", { class: row.skill ? "row-name" : "row-name none", text: row.skill || "No skill loaded" })],
          [{ ...TURNS_COLUMN, title: "Turns that loaded the skill" }, SESSIONS_COLUMN])
      : h("p", { class: "note", text: "Choose a longer period to find skill use" }));
}

// Codex use, counted apart from Claude's: its tokens per session. Cockpit has no list price for Codex's
// models, so it shows in tokens; its share of Codex's own weekly limit is the meter at the top.
const CODEX_KINDS = [
  ["fresh", "Uncached input", null],
  ["cached", "Cached input", null],
  ["output", "Output", "Includes reasoning tokens"],
  ["reasoning", "Reasoning", "Reasoning is included in Output"],
];
// How Codex's share of its weekly limit is counted (codex.py).
const CODEX_SHARE = "Codex's share of its own weekly limit, from its meter: each whole-percent rise is shared out over the "
  + "same account's Codex calls on the host since the rise before, by their tokens. Each account has its own meter, and "
  + "use of the same Codex account elsewhere counts here too.";

// The account a Codex session ran on, as the start of its row's line, when more than one account is signed in.
function codexAccount(codex, row) {
  return codex.accounts.length > 1 ? `${row.account} · ` : "";
}

// A Codex session's breakdown: where it ran, its share of the week and its tokens.
function codexTitle(row) {
  return [
    row.cwd, row.session_id, `Account ${row.account}`, `First call ${moment(row.first)}, last ${moment(row.last)}`, "",
    `${percent(row.week_percent)} of its Codex account's weekly limit`,
    ...CODEX_KINDS.map(([kind, label]) => `${label}: ${tokens(row.tokens[kind])} tokens`),
  ].join("\n");
}

function renderCodex(data) {
  const codex = data.codex;
  const rows = ui.all.codex ? codex.sessions : codex.sessions.slice(0, TOP_ROWS);
  const mostCalls = Math.max(0, ...codex.sessions.map((row) => row.calls));
  const mostShare = Math.max(0, ...codex.sessions.map((row) => row.week_percent));
  const most = Object.fromEntries(CODEX_KINDS.map(([kind]) => [kind, Math.max(0, ...codex.sessions.map((row) => row.tokens[kind]))]));
  fill("spend-codex",
    head("codex-title", "Codex", "Counted apart from Claude, from Codex's session files on the host. Cockpit has no price for Codex's models, so its use shows as a share of its own weekly limit and in tokens."),
    codex.calls
      ? [
          h("table", { class: "spend-table kinds-table codex-kinds" },
            h("thead", {}, h("tr", {}, h("th", { scope: "col", text: "Tokens" }), h("th", { scope: "col", text: "Count" }))),
            h("tbody", {}, CODEX_KINDS.map(([kind, label, meaning]) => h("tr", {},
              h("th", { scope: "row", title: meaning, text: label }), h("td", { text: tokens(codex.total[kind]) }))))),
          h("p", { class: "spend-count", text: `${codex.calls.toLocaleString("en-US")} model calls in ${codex.sessions.length} sessions, ${percent(codex.week_percent)} of Codex's own weekly limit. ${CODEX_SHARE}` }),
          h("div", { class: "table-scroll" },
            h("table", { class: "spend-table codex-table" },
              h("thead", {}, h("tr", {}, h("th", { scope: "col", class: "name-col", text: "Session" }),
                h("th", { scope: "col", "aria-sort": "descending", text: "Of its week" }), h("th", { scope: "col", text: "Calls" }),
                CODEX_KINDS.map(([, label, meaning]) => h("th", { scope: "col", title: meaning, text: label })))),
              h("tbody", {}, rows.map((row) => openRow(`codex:${row.session_id}`, CODEX_KINDS.length + 3, codexTitle(row),
                [h("span", { class: "row-name", text: folder(row.cwd) || row.session_id }),
                  h("span", { class: "row-meta", text: `${codexAccount(codex, row)}${CODEX_SOURCES[row.source] || row.source || "unknown start"} · ${row.session_id.slice(0, 8)} · last call ${moment(row.last)}` })],
                [heatCell(percent(row.week_percent), row.week_percent, mostShare, true),
                  heatCell(String(row.calls), row.calls, mostCalls, false),
                  CODEX_KINDS.map(([kind]) => heatCell(tokens(row.tokens[kind]), row.tokens[kind], most[kind], false))]))))),
          showAll("codex", codex.sessions.length, TOP_ROWS),
        ]
      : h("p", { class: "note", text: "Choose a longer period to find Codex use" }));
}

// One line: the period the figures cover and when they were read; how they are counted is under the figures.
function lede(data) {
  const updated = `Updated ${clock(data.ready_at)}.`;
  if (ui.shownPeriod === "week") return `This week, since ${moment(data.since)}. ${updated}`;
  return `The last ${PERIOD_WORDS[ui.shownPeriod]}, since ${moment(data.since)}. ${updated}`;
}

// A redraw replaces the page's buttons; the one that had focus (by its `data-focus`) gets it back, so a key press
// can open a breakdown and close it again.
function render() {
  const focused = document.activeElement?.dataset?.focus;
  draw();
  const again = focused && [...document.querySelectorAll("[data-focus]")].find((el) => el.dataset.focus === focused);
  if (again) again.focus();
}

function draw() {
  const data = ui.data;
  for (const button of document.querySelectorAll(".spend-periods button")) {
    button.setAttribute("aria-pressed", String(button.dataset.period === ui.period));
  }
  for (const button of document.querySelectorAll(".spend-measures button")) {
    button.setAttribute("aria-pressed", String(button.dataset.measure === ui.measure));
  }
  if (!data) return;
  renderLimits(data);
  renderDaily(data);
  fill("spend-lede", lede(data));
  renderTotals(data);
  renderWhere(data);
  renderMisses(data);
  renderModels(data);
  renderSessions(data);
  renderSkills(data);
  renderOrigins(data);
  renderSessionKinds(data);
  renderTop(data);
  renderCodex(data);
  fill("spend-method",
    `Claude Code's and Codex's calls, read from their session files on the host; this week begins when the earlier of the Claude accounts' current weeks began. `
    + `List prices per million tokens on Opus 5.5: $4 input, $20 output, $0.20 for a cache read. `
    + `The weekly share is an estimate using ${dollars(data.week_list_usd)} of list-price use as its reference budget. A turn runs from one prompt to the next; a subagent's calls count in the turn it started in, `
    + `and so does the work after a background command or subagent finished and woke the session.`);
  const banner = data.unpriced_models.length
    ? h("div", { class: "banner fail", text: `Calls to ${data.unpriced_models.join(", ")} are left out: Cockpit has no price for them.` })
    : null;
  document.getElementById("banner").replaceChildren(...[banner].filter(Boolean));
}

// A load started after this one (the user picked another period) wins: this one's answer and its next poll drop.
async function load() {
  clearTimeout(timer);
  const mine = ++ui.loads;
  let wait = POLL_MS;
  const period = ui.period;
  // Another period's figures fade back while this one's load, with the load line running (UI-48).
  const layout = document.querySelector(".spend-layout");
  const switching = ui.data !== null && period !== ui.shownPeriod;
  layout.classList.toggle("switching", switching);
  if (switching && !ui.lineOn) {
    ui.lineOn = true;
    Loading.start();
  }
  // The latest load ends the line, also when it is not a switch itself (the user picked the shown period again).
  const done = () => {
    layout.classList.remove("switching");
    if (ui.lineOn) Loading.stop();
    ui.lineOn = false;
  };
  try {
    const data = await api(`/api/spend?period=${period}`);
    if (mine !== ui.loads) return;
    done();
    if (data.ready_at) {
      ui.data = data;
      ui.shownPeriod = period;
      render();
    } else {
      fill("spend-lede", "Reading the session transcripts. Cockpit does this once after it starts, in about half a minute.");
      wait = COUNTING_MS;
    }
  } catch (error) {
    if (mine !== ui.loads) return;
    logError("loading usage", error);
    done();
    Loading.failed(layout);
    document.getElementById("banner").replaceChildren(h("div", { class: "banner fail", text: `Usage did not load: ${error.message}` }));
  }
  timer = setTimeout(load, wait);
}

function linkTo(name, value) {
  const url = new URL(location.href);
  url.searchParams.set(name, value);
  history.replaceState(null, "", url);
}

for (const button of document.querySelectorAll(".spend-periods button")) {
  button.addEventListener("click", () => {
    ui.period = button.dataset.period;
    linkTo("period", ui.period);
    render();
    load();
  });
}
for (const button of document.querySelectorAll(".spend-measures button")) {
  button.addEventListener("click", () => {
    ui.measure = button.dataset.measure;
    linkTo("measure", ui.measure);
    try { localStorage.setItem(MEASURE_KEY, ui.measure); } catch { /* storage blocked */ }
    render();
  });
}
render();
load();
