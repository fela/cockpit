"use strict";
// The allowance left on the Usage page (SPEC UI-87): a row for each provider's week, the share left averaged over its
// accounts, over the last seven days and the next seven (hub_left.py), all rows on one time scale. Ahead a row draws
// two lines: the share left when nothing more is used, which only the resets move, and the share left when use goes
// on at the recent pace. The row says the share left now and what comes next; the pointer or the keys read any other
// time in a tooltip, with each account of the row being read. A table under "All the tables" says the same in
// figures. Loads after hub-accounts.js; `api` and `fill` are usage.js's.

const LEFT_POLL_MS = 300e3;
// How soon to ask again while the hub has no reading to draw yet, and after a load that failed.
const LEFT_FIRST_MS = 15e3;
// How far an arrow key moves the reading.
const LEFT_KEY_SECONDS = 6 * 3600;
const LEFT_DAY = 86400;
const LEFT_SVG = "http://www.w3.org/2000/svg";
// What each line says, in the legend, the table and to a screen reader.
const LEFT_LINES = { past: "So far", idle: "If nothing more is used", pace: "At the recent pace" };
// The same over a column of the tooltip, beside the line's sample.
const LEFT_HEADS = { idle: "If unused", pace: "At pace" };
// A share under this is nothing left: the lines' shares are rounded to a tenth.
const LEFT_NOTHING = 0.5;

// `left`: Cockpit's last answer, null while there is nothing to draw. `tightness`: how tight each provider's week is
// (tightness.py), {} without a reading. `failed`: why the latest load brought no answer, "" when it brought one. `at`:
// the time under the pointer or the keys, in epoch seconds, null without a reading on show. `row`: the row being read,
// whose accounts the tooltip lists. `said`: the reading in words for a screen reader, made once and kept across
// redraws.
const leftUi = { left: null, tightness: {}, failed: "", at: null, row: 0, said: null };
let leftTimer = null;

function leftName(group) {
  if (group.provider !== "claude") return hubProvider(group.provider);
  return `Claude ${group.model || "all models"}`;
}

// An account of a group by the short name the accounts list gives it.
function leftAccount(group, account) {
  const known = hubUi.hub && hubUi.hub.accounts && hubUi.hub.accounts.find((each) => each.provider === group.provider && each.id === account.id);
  return known ? hubShortName(known) : account.id.split("@")[0];
}

function leftShare(value) {
  return value === null ? "–" : `${Math.round(value)} %`;
}

function leftDay(seconds) {
  return new Date(seconds * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

// A length of time in the tooltip: "5 h", "3 d", "3 d 6 h".
function leftSpan(seconds) {
  const hours = Math.max(1, Math.round(seconds / 3600));
  if (hours < 24) return `${hours} h`;
  return hours % 24 ? `${Math.floor(hours / 24)} d ${hours % 24} h` : `${hours / 24} d`;
}

// The share left at a time on a line of [seconds, share] points: between two points the straight line through them,
// at a reset (two points at one time) the share after it, and null before the line's first point or after its last.
function leftAt(points, seconds) {
  if (!points.length || seconds < points[0][0] || seconds > points[points.length - 1][0]) return null;
  let last = 0;
  for (let at = 1; at < points.length && points[at][0] <= seconds; at += 1) last = at;
  const [fromTime, from] = points[last];
  if (last === points.length - 1) return from;
  const [toTime, to] = points[last + 1];
  return from + (to - from) * (seconds - fromTime) / (toTime - fromTime);
}

function leftSvg(tag, attributes) {
  const el = document.createElementNS(LEFT_SVG, tag);
  for (const [name, value] of Object.entries(attributes)) el.setAttribute(name, value);
  return el;
}

// A plot is 1000 wide and 100 high whatever its size on the page; its lines keep their width (hub-left.css).
function leftX(left, seconds) {
  return (seconds - left.start) / (left.end - left.start) * 1000;
}

function leftPath(left, points) {
  return points.map(([seconds, share], at) => `${at ? "L" : "M"}${leftX(left, seconds).toFixed(1)} ${(100 - share).toFixed(1)}`).join("");
}

// A mark at a time and a share, placed in percent so it keeps its shape at any width.
function leftMark(cls, left, seconds, share) {
  const mark = h("span", { class: cls });
  mark.style.left = `${leftX(left, seconds) / 10}%`;
  if (share !== null) mark.style.top = `${100 - share}%`;
  return mark;
}

// A line's look, as the legend and the tooltip's columns show it.
function leftSample(line) {
  const sample = leftSvg("svg", { viewBox: "0 0 24 4", class: "left-sample", "aria-hidden": "true" });
  sample.append(leftSvg("path", { d: "M1 2H23", class: `left-line ${line}` }));
  return sample;
}

function leftLegend() {
  return h("span", { class: "left-legend", "aria-hidden": "true" },
    Object.entries(LEFT_LINES).map(([line, words]) => h("span", { class: "left-key" }, leftSample(line), words)));
}

// A time on the graph, as short as its axis writes a day: a clock today, a weekday and clock on a nearby day, and
// the weekday with its date beyond that.
function leftWhen(seconds) {
  const then = new Date(seconds * 1000);
  if (Math.abs(then.getTime() - Date.now()) < 6 * LEFT_DAY * 1000) return hubWhen(then.getTime());
  return `${then.toLocaleDateString(undefined, { weekday: "short" })} ${then.getDate()}, ${clock(then.getTime())}`;
}

// What comes next for a group, in a line under its share: that nothing is left and until when, when the recent pace
// uses it up, or else its next reset, with the share the recent pace leaves by then in the hover text.
function leftVerdict(left, group) {
  if (group.now < LEFT_NOTHING) {
    const back = group.idle.find(([, share]) => share >= LEFT_NOTHING);
    return { warn: true, words: back ? `Used up until ${leftWhen(back[0])}` : "Used up", title: null };
  }
  const out = group.pace.find(([, share]) => share < LEFT_NOTHING);
  if (out) return { warn: true, words: `Runs out ${leftWhen(out[0])} at this pace`, title: `Nothing is left by ${moment(out[0] * 1000)} if use goes on as it has lately` };
  const resets = group.accounts.map((account) => account.resets_at).filter((at) => at !== null && at > left.now);
  if (!resets.length) return { warn: false, words: "", title: null };
  const next = Math.min(...resets);
  return {
    warn: false, words: `Next reset ${leftWhen(next)}`,
    title: `At the recent pace ${leftShare(leftAt(group.pace, next - 60))} is left just before it`,
  };
}

// One group's row: its name, the share left now and what comes next, then its plot: the line so far over a wash, the
// two lines ahead, and a rule and a dot at now.
function leftRow(left, group, index) {
  const svg = leftSvg("svg", { viewBox: "0 0 1000 100", preserveAspectRatio: "none", class: "left-lines", "aria-hidden": "true" });
  if (group.past.length) {
    const from = leftX(left, group.past[0][0]).toFixed(1);
    const to = leftX(left, group.past[group.past.length - 1][0]).toFixed(1);
    svg.append(leftSvg("path", { d: `${leftPath(left, group.past)}L${to} 100L${from} 100Z`, class: "left-wash" }));
  }
  for (const line of ["pace", "idle", "past"]) svg.append(leftSvg("path", { d: leftPath(left, group[line]), class: `left-line ${line}` }));
  const verdict = leftVerdict(left, group);
  const [provider, ...model] = leftName(group).split(" ");
  return h("div", { class: `left-row ${group.provider}${group.model === "Fable" ? " fable" : ""}${index === left.groups.length - 1 ? " last" : ""}`, "data-row": index },
    h("div", { class: "left-label" },
      h("span", { class: "left-name" }, h("b", { text: provider }), model.length ? ` ${model.join(" ")}` : null),
      h("span", { class: "left-figure" }, h("b", { text: leftShare(group.now) }), " left"),
      h("span", { class: `left-verdict${verdict.warn ? " warn" : ""}`, text: verdict.words, title: verdict.title })),
    h("div", { class: "left-plot" }, svg, leftMark("left-now-rule", left, left.now, null), leftMark("left-dot", left, left.now, group.now)));
}

// The rows' one time scale: each midnight by its weekday and date, and now. A date too close to "Now" to stand beside
// it is left out, at any width or on a narrow page (hub-left.css), where every other date goes too.
function leftAxis(left) {
  const span = left.end - left.start;
  const ticks = [];
  const day = new Date(left.start * 1000);
  day.setHours(24, 0, 0, 0);
  for (let at = 0; day.getTime() / 1000 < left.end; day.setDate(day.getDate() + 1), at += 1) {
    const seconds = day.getTime() / 1000;
    const edge = Math.min(seconds - left.start, left.end - seconds) / span;
    if (edge < 0.03) continue;
    const apart = Math.abs(seconds - left.now) / span;
    const tick = leftMark(`left-tick${at % 2 ? " odd" : ""}${apart < 0.045 ? " by-now" : apart < 0.11 ? " near-now" : ""}`, left, seconds, null);
    tick.textContent = `${day.toLocaleDateString(undefined, { weekday: "short" })} ${day.getDate()}`;
    ticks.push(tick);
  }
  const now = leftMark("left-tick now", left, left.now, null);
  now.textContent = "Now";
  return h("div", { class: "left-axis", "aria-hidden": "true" }, ticks, now);
}

// The time under the pointer, to the hour, and at now when the pointer is that close to it; the row under the
// pointer is the one being read. Left of the plots there is no time to read.
function leftPoint(event) {
  const left = leftUi.left;
  const rows = event.currentTarget;
  const box = rows.querySelector(".left-plot").getBoundingClientRect();
  if (event.clientX < box.left - 4) {
    if (event.pointerType === "mouse") leftRead(null, leftUi.row);
    return;
  }
  const span = left.end - left.start;
  const pointed = left.start + Math.min(1, Math.max(0, (event.clientX - box.left) / box.width)) * span;
  const seconds = Math.abs(pointed - left.now) < span * 0.006 ? left.now : Math.min(left.end, Math.max(left.start, Math.round(pointed / 3600) * 3600));
  const over = event.target.closest(".left-row");
  leftRead(seconds, over ? Number(over.dataset.row) : leftUi.row);
}

function leftKey(event) {
  const left = leftUi.left;
  const from = leftUi.at === null ? left.now : leftUi.at;
  const row = { ArrowUp: leftUi.row - 1, ArrowDown: leftUi.row + 1 }[event.key];
  const to = {
    ArrowLeft: from - LEFT_KEY_SECONDS, ArrowRight: from + LEFT_KEY_SECONDS, Home: left.start, End: left.end, Escape: null,
    ArrowUp: from, ArrowDown: from,
  }[event.key];
  if (to === undefined) return;
  event.preventDefault();
  leftRead(
    to === null ? null : Math.min(left.end, Math.max(left.start, to)),
    row === undefined ? leftUi.row : Math.min(left.groups.length - 1, Math.max(0, row)));
}

// The lines that say a time: the one so far up to now, the two ahead after it.
function leftLinesAt(left, seconds) {
  return seconds <= left.now ? ["past"] : ["idle", "pace"];
}

// A reading's time and how far it is from now.
function leftTipWhen(left, seconds) {
  if (seconds === left.now) return "Now";
  return `${moment(seconds * 1000)}, ${seconds < left.now ? `${leftSpan(left.now - seconds)} ago` : `in ${leftSpan(seconds - left.now)}`}`;
}

// An account's next reset, in the words that follow its name.
function leftResets(left, account) {
  return account.resets_at !== null && account.resets_at > left.now ? `, resets ${leftWhen(account.resets_at)}` : "";
}

// The tooltip's words: the time and how far it is from now, then a row for each group with its share by each line,
// and under the group being read a row for each of its accounts with its next reset.
function leftTip(left, seconds, row) {
  const lines = leftLinesAt(left, seconds);
  const when = leftTipWhen(left, seconds);
  const figures = (each, cls) => lines.map((line) => h("span", { class: `left-tip-figure${cls}`, text: leftShare(leftAt(each[line], seconds)) }));
  return [
    h("div", { class: "tip-when", text: when }),
    h("div", { class: `left-tip-grid${lines.length > 1 ? " ahead" : ""}` },
      lines.length > 1 && [h("span", {}), lines.map((line) => h("span", { class: "left-tip-head" }, leftSample(line), LEFT_HEADS[line]))],
      left.groups.map((group, index) => [
        h("span", { class: `left-tip-name${index === row ? " on" : ""}`, text: leftName(group) }),
        figures(group, index === row ? " on" : ""),
        index === row && group.accounts.map((account) => [
          h("span", { class: "left-tip-account" },
            leftAccount(group, account), leftResets(left, account)),
          figures(account, " account"),
        ]),
      ])),
  ];
}

// The same for a screen reader, in a sentence.
function leftSaid(left, seconds, row) {
  const lines = leftLinesAt(left, seconds);
  const shares = (each) => lines.map((line) => `${leftShare(leftAt(each[line], seconds))} ${line === "past" ? "left" : LEFT_LINES[line].toLowerCase()}`).join(", ");
  const group = left.groups[row];
  return `${leftTipWhen(left, seconds)}: ${left.groups.map((each) => `${leftName(each)} ${shares(each)}`).join("; ")}. `
    + `${leftName(group)} by account: ${group.accounts.map((account) => `${leftAccount(group, account)}${leftResets(left, account)}, ${shares(account)}`).join("; ")}.`;
}

// The tooltip stands beside the reading's rule, on the side with room for it, level with the row being read and
// inside the rows. Where neither side has room, on a phone, it stands under the rows' dates and follows the rule.
function leftPlaceTip(tip, rows, left, seconds, row) {
  const plot = rows.querySelectorAll(".left-plot")[row];
  const x = plot.offsetLeft + leftX(left, seconds) / 1000 * plot.offsetWidth;
  const [width, height] = [tip.offsetWidth, tip.offsetHeight];
  const gap = 14;
  let at = x + gap;
  let top = Math.max(0, Math.min(plot.offsetTop - 6, rows.offsetHeight - height));
  if (at + width > rows.offsetWidth) at = x - gap - width;
  if (at < 0) {
    at = Math.max(0, Math.min(x - width / 2, rows.offsetWidth - width));
    top = rows.offsetHeight + 26;
  }
  tip.style.left = `${Math.round(at)}px`;
  tip.style.top = `${Math.round(top)}px`;
}

// The reading at a time: a rule through every plot with a dot on each line, and the tooltip beside it. Null takes
// the reading away.
function leftRead(seconds, row) {
  const left = leftUi.left;
  const rows = document.querySelector("#hub-left .left-rows");
  const tip = rows.querySelector(".left-tip");
  if (seconds === leftUi.at && row === leftUi.row && tip.hidden === (seconds === null)) return;
  leftUi.at = seconds;
  leftUi.row = row;
  for (const el of rows.querySelectorAll(".left-rule, .left-at")) el.remove();
  tip.hidden = seconds === null;
  if (seconds === null) {
    leftUi.said.textContent = "";
    return;
  }
  for (const [index, plot] of [...rows.querySelectorAll(".left-plot")].entries()) {
    const group = left.groups[index];
    plot.append(leftMark("left-rule", left, seconds, null));
    for (const line of leftLinesAt(left, seconds)) {
      const share = leftAt(group[line], seconds);
      if (share !== null) plot.append(leftMark(`left-at ${line}`, left, seconds, share));
    }
  }
  tip.replaceChildren(...leftTip(left, seconds, row).flat(Infinity).filter(Boolean));
  leftPlaceTip(tip, rows, left, seconds, row);
  leftUi.said.textContent = leftSaid(left, seconds, row);
}

// The same in figures, a day apart: so far, then ahead by each line.
function leftTable(left) {
  const row = (name, seconds, line) => {
    const shares = left.groups.map((group) => leftAt(group[line], seconds));
    return shares.some((share) => share !== null) && h("tr", {},
      h("th", { scope: "row", text: name }), shares.map((share) => h("td", { text: leftShare(share) })));
  };
  const days = [1, 2, 3, 4, 5, 6, 7];
  const part = (line, rows) => h("tbody", {},
    h("tr", { class: "left-part" }, h("th", { scope: "rowgroup", colspan: left.groups.length + 1, text: LEFT_LINES[line] })), rows);
  return h("div", { class: "table-scroll" },
    h("table", { class: "spend-table left-table" },
      h("thead", {}, h("tr", {}, h("th", { scope: "col", text: "When" }), left.groups.map((group) => h("th", { scope: "col", text: leftName(group) })))),
      part("past", [...[...days].reverse().map((day) => row(leftDay(left.now - day * LEFT_DAY), left.now - day * LEFT_DAY, "past")), row("Now", left.now, "past")]),
      ["idle", "pace"].map((line) => part(line, days.map((day) => row(leftDay(left.now + day * LEFT_DAY), left.now + day * LEFT_DAY, line))))));
}

// One provider's tightness in a line under the graph: "Claude: tight, runs out in about 20 h, before the Wed 11:00
// reset", with why in the hover text. A tight week wears the same lamp as a row that runs out.
function leftTightLine(provider, tight) {
  const reset = tight.next_reset === null ? null : `the ${leftWhen(Date.parse(tight.next_reset) / 1000)} reset`;
  let words;
  if (tight.reading !== "tight") {
    words = reset ? `lasts to ${reset}${tight.reading === "room" ? " with a quarter to spare" : ""}` : "lasts";
  } else if (tight.hours_left > 0) {
    words = `runs out in about ${leftSpan(tight.hours_left * 3600)}${reset ? `, before ${reset}` : ""}`;
  } else {
    words = reset ? `until ${reset}` : "";
  }
  return h("p", { class: `left-tight${tight.reading === "tight" ? " warn" : ""}`, title: tight.why },
    h("b", { text: `${hubProvider(provider)}:` }), ` ${tight.reading}${words ? `, ${words}` : ""}`);
}

// Both parts hide while there is nothing to draw: a machine without a hub, or a hub without a reading yet.
function renderLeft() {
  const left = leftUi.left;
  const part = document.getElementById("hub-left");
  const table = document.getElementById("spend-left");
  part.hidden = table.hidden = !left;
  if (!left) return;
  leftUi.said = leftUi.said || h("div", { class: "left-said", role: "status" });
  // A redraw under the keys keeps the focus and the reading; one under the pointer waits for the pointer's next move.
  const focused = part.querySelector(".left-rows") === document.activeElement;
  const at = leftUi.at;
  const heading = head("left-title", "Allowance left", leftLegend());
  heading.querySelector("h2").title = "Share of each week's allowance left, averaged over a provider's accounts";
  const rows = h("div", {
    class: "left-rows", tabindex: "0", role: "group", "data-arrow-keys": true,
    "aria-label": "Allowance left, a row for each week's allowance. Left and right arrows read other times, up and down the rows.",
    onpointermove: leftPoint, onpointerdown: leftPoint,
    onpointerleave: (event) => { if (event.pointerType === "mouse") leftRead(null, leftUi.row); },
    onfocus: () => { if (leftUi.at === null) leftRead(leftUi.left.now, leftUi.row); },
    // A redraw takes the focused rows off the page; the reading goes with them.
    onblur: (event) => { if (event.currentTarget.isConnected) leftRead(null, leftUi.row); },
    onkeydown: leftKey,
  }, left.groups.map((group, index) => leftRow(left, group, index)), h("div", { class: "chart-tip left-tip", hidden: true, "aria-hidden": "true" }));
  part.replaceChildren(
    heading,
    // An older answer stays on show under the reason the newer one is missing, with the time its "now" stands for.
    leftUi.failed && h("p", { class: "left-old", text: `Not updated since ${hubWhen(left.now * 1000)}: ${leftUi.failed}` }),
    rows, leftAxis(left),
    h("div", { class: "left-tights" }, Object.entries(leftUi.tightness).map(([provider, tight]) => leftTightLine(provider, tight))),
    leftUi.said);
  fill("spend-left", head("left-table-title", "Allowance left", "The graph's figures, a day apart"), leftTable(left));
  leftUi.at = null;
  leftUi.row = Math.min(leftUi.row, left.groups.length - 1);
  leftUi.said.textContent = "";
  if (!focused) return;
  rows.focus();
  leftRead(Math.min(left.end, Math.max(left.start, at === null ? left.now : at)), leftUi.row);
}

async function loadLeft() {
  clearTimeout(leftTimer);
  let wait = LEFT_POLL_MS;
  try {
    const { left, tightness } = await api("/api/hub/left");
    leftUi.left = left && left.groups.length ? left : null;
    leftUi.tightness = tightness || {};
    leftUi.failed = "";
    renderLeft();
    // A machine without a hub has nothing to wait for; a hub read for the first time soon has.
    if (!leftUi.left) wait = hubUi.hub === null ? null : LEFT_FIRST_MS;
  } catch (error) {
    logError("loading the allowance left", error);
    leftUi.failed = error.message;
    renderLeft();
    wait = LEFT_FIRST_MS;
  }
  if (wait !== null) leftTimer = setTimeout(loadLeft, wait);
}

document.addEventListener("DOMContentLoaded", loadLeft);
// A page that changes width moves the plots, so a reading on show takes its place again.
window.addEventListener("resize", () => {
  const rows = document.querySelector("#hub-left .left-rows");
  if (rows && leftUi.left && leftUi.at !== null) leftPlaceTip(rows.querySelector(".left-tip"), rows, leftUi.left, leftUi.at, leftUi.row);
});
