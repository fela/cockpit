"use strict";
// Account rows on Usage (SPEC UI-84, UI-85). Classic scripts share this scope;
// these helpers load before hub.js and usage.js.
const HUB_PROVIDERS = { claude: "Claude", codex: "Codex" };
const HUB_STATES = { working: "Working", disabled: "Turned off", signed_out: "Signed out" };
const HUB_STALE_MS = 3600e3;
const HUB_LIMIT_COLUMNS = [
  { key: "five_hour", name: "5 hours", title: "Used of the 5-hour limit" },
  { key: "week", name: "Week", title: "Used of the weekly limit; Claude includes all models" },
  { key: "fable", name: "Fable", title: "Used of Fable's weekly limit" },
  { key: "opus", name: "Opus after Fable", title: "Used of the week left after Fable's allowance.\nFable's allowance takes half the week; other models share the other half." },
];

function hubShortName(account) {
  return (account.email || account.id).split("@")[0];
}

function hubProvider(provider) {
  return HUB_PROVIDERS[provider] || provider;
}

// Hover texts fit the page's two lines of 80 characters, including long addresses.
function hubTip(words) {
  const lines = words.split("\n").flatMap((line) => {
    const wrapped = [];
    while (line.length > 80) {
      const space = line.lastIndexOf(" ", 80);
      const end = space > 0 ? space : 80;
      wrapped.push(line.slice(0, end));
      line = line.slice(end).trimStart();
    }
    return [...wrapped, line];
  });
  if (lines.length > 2) lines[1] = `${lines[1].slice(0, 79)}…`;
  return lines.slice(0, 2).join("\n");
}

// Today reads as a clock; nearby days as a weekday and clock; other days include a date.
function hubWhen(iso) {
  const then = new Date(iso);
  if (then.toDateString() === new Date().toDateString()) return clock(iso);
  if (Math.abs(then.getTime() - Date.now()) < 6 * 86400e3) return `${then.toLocaleDateString(undefined, { weekday: "short" })} ${clock(iso)}`;
  return moment(iso);
}

function hubStateWords(account) {
  if (account.state !== "cooling") return HUB_STATES[account.state];
  return account.state_until ? `Resting until ${hubWhen(account.state_until)}` : "Resting";
}

// A working account's models the hub holds back for now, such as one the provider rate-limited for ten minutes.
function hubHeldWords(account) {
  return (account.held || []).filter((hold) => Date.parse(hold.until) > Date.now())
    .map((hold) => `${hold.label} held until ${hubWhen(hold.until)}`).join(", ");
}

function hubHeldTip(account) {
  const limited = (account.held || []).some((hold) => hold.limit);
  return `${limited ? "The provider rate-limited" : "Errors came from"} these models on this account. Until then the hub sends them to other accounts; every other model still uses this one.`;
}

// The same keys and order feed the shared headers, each account, and the phone's labels.
function hubLimits(account) {
  const found = {
    five_hour: account.windows.find((window) => window.key === "five_hour"),
    week: account.week,
    fable: account.provider === "claude" && account.model_weeks.find((week) => week.model === "Fable"),
    opus: account.provider === "claude" && account.opus_week,
  };
  return HUB_LIMIT_COLUMNS.filter((column) => found[column.key] && found[column.key].used !== null)
    .map((column) => ({ ...column, found: found[column.key] }));
}

function hubOld(iso) {
  return Boolean(iso) && Date.now() - new Date(iso).getTime() > HUB_STALE_MS;
}

// A limit at 80 % or more says how long until its reset. Limits that follow each other and reset together say it
// once, under the last of them.
function hubCountdowns(limits) {
  const near = (row) => row.found.used >= 80;
  const together = (a, b) => Math.abs(new Date(a.found.resets_at) - new Date(b.found.resets_at)) < 60e3;
  return limits.map((row, at) => {
    const next = limits[at + 1];
    return near(row) && !(next && near(next) && together(row, next)) ? countdown(row.found.resets_at) : "";
  });
}

function hubLimitRow(row, line) {
  const found = row.found;
  const stale = hubOld(found.captured_at);
  const details = [stale && `As of ${moment(found.captured_at)}`,
    row.key === "opus" && "Week left after Fable's allowance"].filter(Boolean).join("; ");
  const reset = `Resets in ${countdown(found.resets_at)}, ${moment(found.resets_at)}`;
  const fill = h("span", { class: "hub-limit-fill" });
  fill.style.width = `${Math.max(0, Math.min(100, found.used))}%`;
  let tick = null;
  if (Number.isFinite(found.elapsed)) {
    tick = h("span", { class: "hub-limit-tick" });
    tick.style.left = `${Math.max(0, Math.min(100, found.elapsed))}%`;
  }
  return h("div", { class: `hub-limit ${found.pace || "unknown"}${stale ? " stale" : ""}`,
    "data-limit": row.key, role: "group", "aria-label": `${row.name}: ${found.used} %`,
    title: hubTip([reset, details].filter(Boolean).join("\n")) },
    h("span", { class: "hub-limit-name", text: row.name }),
    h("span", { class: "hub-limit-bar", "aria-hidden": "true" }, fill, tick),
    h("span", { class: "hub-limit-used", text: `${found.used} %` }),
    h("span", { class: "hub-limit-line", text: line }));
}

function hubLimitsLine(account) {
  return account.limits_error ? `Its limits are unknown: ${account.limits_error}` : "Its limits show after its next request through the hub";
}

// Every account spans the parent's columns. Empty cells hold their place without
// inventing a figure; unknown limits span the whole group. Controls remain hub.js's.
function hubAccountCard(account, headEnd, foot, columns) {
  const who = account.email || account.id;
  const plan = account.plan_label ? `${account.plan_label[0].toUpperCase()}${account.plan_label.slice(1)}` : "";
  const pro500 = account.provider === "codex" && account.plan === "promax";
  const limits = hubLimits(account);
  const lines = hubCountdowns(limits);
  const held = account.state === "working" ? hubHeldWords(account) : "";
  const state = account.state !== "working" ? hubStateWords(account) : held;
  // A resting account's row says until when; what the hub said about it is on hover.
  const said = account.state !== "cooling" && account.said;
  return h("article", { class: `hub-account ${account.state}`, id: `account-${account.id}` },
    h("div", { class: "hub-account-head" },
      h("span", { class: "hub-lamp", role: "img", title: hubStateWords(account), "aria-label": hubStateWords(account) }),
      h("span", { class: "hub-account-who", title: hubTip(who), text: hubShortName(account) }),
      h("span", { class: "hub-account-kind", title: pro500 ? "Ultrafast available for Sol and Astra" : null,
        text: [hubProvider(account.provider), plan].filter(Boolean).join(" ") }),
      h("p", { class: "hub-account-state",
        title: hubTip([account.state_until && `Until ${moment(account.state_until)}`, held && hubHeldTip(account), account.said].filter(Boolean).join("\n")) || null },
        state && h("span", { class: "hub-state-words", text: state }),
        said && h("span", { class: "hub-said", text: said }))),
    limits.length ? h("div", { class: "hub-limits" }, columns.map((column) => {
      const at = limits.findIndex((row) => row.key === column.key);
      return at >= 0 ? hubLimitRow(limits[at], lines[at])
        : h("span", { class: "hub-limit-empty", "data-limit": column.key, "aria-hidden": "true" });
    })) : h("p", { class: "hub-limits-at", text: hubLimitsLine(account) }),
    foot,
    h("div", { class: "hub-account-end" }, headEnd));
}
