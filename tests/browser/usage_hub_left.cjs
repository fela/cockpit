// The allowance left on the Usage page (SPEC UI-87): above the Accounts a row per provider's week, its name, the share
// left now and what comes next beside a plot from seven days back to seven days ahead, every plot on the one time
// scale under the rows. The line so far is followed by two lines ahead: what is left when nothing more is used, which
// the resets lift, and what is left at the recent pace. The pointer, a tap or the arrow keys read every plot at one
// time in a tooltip beside the rule, which also lists the accounts of the row being read. "All the tables" holds the
// same figures as a table. Under the time scale a line a provider says how tight its week is (SESSION-28). Nothing is
// wider than the window on a desktop or a phone, and on a machine without a hub neither the graph nor its table shows.
//
// Starts serve_hub.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) with a
// stand-in hub on a local port, and drives a headless Chromium. Prints one line per check and exits 1 when one failed.
// The graph is saved as left-1280.png, left-1280-read.png, left-1280-dark.png, left-1280-dark-read.png, left-390.png
// and left-390-read.png in the work folder.
//
// Usage, from the checkout: node tests/browser/usage_hub_left.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

// Browser dependencies can be selected through the environment (README.md).
const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const ROOT = path.resolve(__dirname, "..", "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// What the stand-in hub holds (serve_slow.py, hub.json). One Claude account has its week 40 % used and Fable's own
// week 30 % used, and resets in three and a half days. A second has its week 80 % used and Fable's 60 % used, and
// resets in two and a half days. One Codex account has its week 40 % used and resets in three and a half days. So
// now 40 % of Claude's week is left across its accounts, 55 % of Fable's and 60 % of Codex's; three days ahead, with
// nothing more used, the second Claude account has reset and 80 %, 85 % and 60 % are left; seven days ahead every
// account has reset.
function standIn(now) {
  const seen = new Date(now).toISOString();
  const later = String(Math.round(now / 1000 + 3.5 * 86400));
  const sooner = String(Math.round(now / 1000 + 2.5 * 86400));
  const claude = (name, index, week, fable, reset) => ({
    id: `claude-${name}@example.test.json`, auth_index: index, provider: "claude", email: `${name}@example.test`,
    quota: { observed_at: seen, signals: {
      "Anthropic-Ratelimit-Unified-7d-Utilization": week, "Anthropic-Ratelimit-Unified-7d-Reset": reset,
      "Anthropic-Ratelimit-Unified-7d_oi-Utilization": fable, "Anthropic-Ratelimit-Unified-7d_oi-Reset": reset,
    } },
  });
  return { credentials: [
    claude("one", 1, "0.4", "0.3", later),
    claude("two", 2, "0.8", "0.6", sooner),
    { id: "codex-three@example.test-plus.json", auth_index: 3, provider: "codex", email: "three@example.test", id_token: { chatgpt_account_id: "acct-1", plan_type: "plus" }, quota: { observed_at: seen, signals: {
      "X-Codex-Plan-Type": "plus", "X-Codex-Secondary-Used-Percent": "40", "X-Codex-Secondary-Window-Minutes": "10080", "X-Codex-Secondary-Reset-At": later,
    } } },
  ] };
}

// The graph as the page shows it: its heading and legend, each row's words, lines with a path, marks of a reading and
// boxes, where the scale and the rules stand, and the tooltip's rows: a name with its figures, by group or by account.
function graphNow() {
  const part = document.getElementById("hub-left");
  const box = (el) => {
    const { left, top, right, bottom, width } = el.getBoundingClientRect();
    return { left: Math.round(left), top: Math.round(top), right: Math.round(right), bottom: Math.round(bottom), width: Math.round(width) };
  };
  const centre = (el) => { const { left, width } = el.getBoundingClientRect(); return left + width / 2; };
  const tip = part.querySelector(".left-tip");
  const cells = [];
  for (const cell of tip ? tip.querySelectorAll(".left-tip-grid > *") : []) {
    if (cell.matches(".left-tip-name, .left-tip-account")) {
      cells.push({ name: cell.textContent, on: cell.classList.contains("on"), account: cell.classList.contains("left-tip-account"), figures: [] });
    } else if (cell.matches(".left-tip-figure")) cells[cells.length - 1].figures.push(cell.textContent);
  }
  const rule = part.querySelector(".left-rule");
  return {
    shown: part.checkVisibility(),
    title: part.querySelector("h2") ? part.querySelector("h2").textContent : "",
    legend: [...part.querySelectorAll(".section-head .left-key")].map((el) => el.textContent),
    said: part.querySelector(".left-said") ? part.querySelector(".left-said").textContent : "",
    rows: [...part.querySelectorAll(".left-row")].map((row) => {
      const verdict = row.querySelector(".left-verdict");
      return {
        name: row.querySelector(".left-name").textContent,
        figure: row.querySelector(".left-figure").textContent,
        verdict: verdict.textContent,
        warn: verdict.classList.contains("warn"),
        hover: verdict.dataset.tip || verdict.getAttribute("title") || "",
        lines: [...row.querySelectorAll(".left-line")].filter((line) => line.getAttribute("d")).map((line) => line.getAttribute("class").replace("left-line ", "")).sort(),
        rules: row.querySelectorAll(".left-rule").length,
        dots: row.querySelectorAll(".left-at").length,
        label: box(row.querySelector(".left-label")),
        plot: box(row.querySelector(".left-plot")),
      };
    }),
    area: box(part.querySelector(".left-rows")),
    axis: box(part.querySelector(".left-axis")),
    nowTick: centre(part.querySelector(".left-tick.now")),
    nowRule: centre(part.querySelector(".left-now-rule")),
    lastRules: [...part.querySelectorAll(".left-row.last :is(.left-now-rule, .left-rule)")].map((el) => Math.round(el.getBoundingClientRect().bottom)),
    rule: rule ? centre(rule) : null,
    tip: tip && !tip.hidden ? {
      when: tip.querySelector(".tip-when").textContent,
      heads: [...tip.querySelectorAll(".left-tip-head")].map((el) => el.textContent),
      rows: cells,
      box: box(tip),
    } : null,
  };
}

const overflow = () => document.documentElement.scrollWidth - window.innerWidth;
const NAMES = "Claude all models,Claude Fable,Codex";
const NOW = "40 % left,55 % left,60 % left";
const names = (graph) => graph.rows.map((row) => row.name);
const figures = (graph) => graph.rows.map((row) => row.figure);
const share = (figure) => Number(figure.split(" ")[0]);
const one = (values) => new Set(values).size === 1;
const groups = (tip) => tip.rows.filter((row) => !row.account);
const accounts = (tip) => tip.rows.filter((row) => row.account);
const column = (rows, at) => rows.map((row) => row.figures[at]);
// A reading on show: a rule through every plot and the tooltip.
const read = (graph) => graph.tip !== null && graph.rows.every((row) => row.rules === 1);
const clear = (graph) => graph.tip === null && graph.said === "" && graph.rows.every((row) => row.rules === 0 && row.dots === 0);
// The tooltip stands inside the rows' width and wholly on one side of the reading's rule.
const beside = (graph) => graph.tip.box.left >= graph.area.left - 1 && graph.tip.box.right <= graph.area.right + 1
  && (graph.tip.box.left > graph.rule || graph.tip.box.right < graph.rule);

async function main() {
  const work = workFolder("cockpit-usage-left-");
  fs.writeFileSync(path.join(work, "hub.json"), JSON.stringify(standIn(Date.now())));
  const server = spawn(PYTHON, [path.join(__dirname, "serve_hub.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const info = await harnessUp(work, server);
    browser = await chromium.launch({ executablePath: CHROMIUM });
    const pageErrors = [];
    const open = async (width, scheme) => {
      const context = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: scheme, isMobile: width < 600, hasTouch: width < 600 });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      page.on("pageerror", (error) => pageErrors.push(error.message));
      await page.goto(`${info.origin}/usage`);
      // The graph asks again a quarter of a minute later when Cockpit has not read the hub yet.
      await page.waitForSelector("#hub-left .left-row", { timeout: 90e3 });
      await page.evaluate(() => document.fonts.ready);
      return { context, page };
    };
    const press = async (page, key, times) => { for (let at = 0; at < times; at += 1) await page.keyboard.press(key); };

    const { context, page } = await open(1280, "light");
    const rest = await page.evaluate(graphNow);
    await page.locator("#hub-left").screenshot({ path: path.join(work, "left-1280.png") });
    const order = await page.evaluate(() => [...document.querySelectorAll("#hub-accounts, #hub-left, #spend-totals")].map((el) => el.id));
    check("the graph comes before the Accounts and the spend", String(order) === "hub-left,hub-accounts,spend-totals", String(order));
    check("it is headed Allowance left and names its three lines once, in the heading's line",
      rest.shown && rest.title === "Allowance left" && String(rest.legend) === "So far,If nothing more is used,At the recent pace", JSON.stringify([rest.title, rest.legend]));
    check("a row each for Claude's week, Fable's week and Codex's week, with the share left now across the provider's accounts",
      String(names(rest)) === NAMES && String(figures(rest)) === NOW, JSON.stringify(rest.rows.map((row) => [row.name, row.figure])));
    check("under the share each row says its next reset, and on hover what the recent pace leaves by then",
      rest.rows.every((row) => /^Next reset \S/.test(row.verdict) && !row.warn && /^At the recent pace \d+ % is left just before it$/.test(row.hover) && row.hover.length <= 80),
      JSON.stringify(rest.rows.map((row) => [row.verdict, row.warn, row.hover])));
    const verdicts = await page.evaluate(() => {
      const now = leftUi.left.now;
      const left = { now };
      return [
        leftVerdict(left, { now: 0, idle: [[now, 0], [now + 30 * 3600, 0], [now + 30 * 3600, 50]], pace: [[now, 0]], accounts: [] }),
        leftVerdict(left, { now: 0, idle: [[now, 0]], pace: [[now, 0]], accounts: [] }),
        leftVerdict(left, { now: 20, idle: [[now, 20]], pace: [[now, 20], [now + 86400, 0]], accounts: [{ resets_at: now + 2 * 86400 }] }),
        leftVerdict(left, { now: 50, idle: [[now, 50]], pace: [[now, 50]], accounts: [{ resets_at: null }] }),
      ];
    });
    check("a week with nothing left says until when, one the recent pace uses up says when, and one without a known reset says nothing",
      /^Used up until \S/.test(verdicts[0].words) && verdicts[0].warn && verdicts[1].words === "Used up" && verdicts[1].warn
        && /^Runs out \S.* at this pace$/.test(verdicts[2].words) && verdicts[2].warn && verdicts[3].words === "" && !verdicts[3].warn, JSON.stringify(verdicts));
    // Both providers' weeks last far past their resets at the stand-in's pace, so both readings have room.
    const tights = await page.evaluate(() => [...document.querySelectorAll("#hub-left .left-tight")].map((el) => [el.textContent, el.classList.contains("warn"), el.dataset.tip || el.title]));
    check("under the dates a quiet line a provider says how tight its week is, and on hover why",
      tights.length === 2 && /^Claude: room, lasts to the \S+ \d\d:\d\d reset with a quarter to spare$/.test(tights[0][0])
        && /^Codex: room, lasts to the \S+ \d\d:\d\d reset with a quarter to spare$/.test(tights[1][0])
        && tights.every(([, warn, why]) => !warn && why.startsWith("The week left lasts until the resets")), JSON.stringify(tights));
    const tightWords = await page.evaluate(() => {
      const reset = new Date((leftUi.left.now + 2 * 86400) * 1000).toISOString();
      return [
        leftTightLine("claude", { reading: "tight", hours_left: 20.2, next_reset: reset, why: "" }),
        leftTightLine("codex", { reading: "tight", hours_left: 0, next_reset: reset, why: "" }),
        leftTightLine("codex", { reading: "tight", hours_left: 0, next_reset: null, why: "" }),
        leftTightLine("claude", { reading: "normal", hours_left: null, next_reset: reset, why: "" }),
      ].map((el) => [el.textContent, el.classList.contains("warn")]);
    });
    check("a tight week says when it runs out and the reset after, or until when it stays tight, behind the lamp; a normal one which reset it lasts to",
      /^Claude: tight, runs out in about 20 h, before the \S+ \d\d:\d\d reset$/.test(tightWords[0][0]) && /^Codex: tight, until the \S+ \d\d:\d\d reset$/.test(tightWords[1][0])
        && tightWords[2][0] === "Codex: tight" && /^Claude: normal, lasts to the \S+ \d\d:\d\d reset$/.test(tightWords[3][0])
        && tightWords.map(([, warn]) => warn).join() === "true,true,true,false", JSON.stringify(tightWords));
    check("each plot draws the line so far and both lines ahead, and at rest no reading",
      rest.rows.every((row) => String(row.lines) === "idle,pace,past") && clear(rest), JSON.stringify([rest.rows, rest.tip, rest.said]));
    check("1280 px: the rows stand one under the other, their labels on one edge and their plots on one edge at one width",
      new Set(rest.rows.map((row) => row.plot.top)).size === 3 && one(rest.rows.map((row) => row.label.left)) && one(rest.rows.map((row) => row.plot.left))
        && one(rest.rows.map((row) => row.plot.width)) && rest.rows.every((row) => row.label.right < row.plot.left && Math.abs(row.label.top - row.plot.top) <= 1),
      JSON.stringify(rest.rows.map((row) => [row.label, row.plot])));
    check("the time scale spans the plots, and its Now stands under the rule at now, which ends above it",
      rest.axis.left === rest.rows[0].plot.left && rest.axis.width === rest.rows[0].plot.width && Math.abs(rest.nowTick - rest.nowRule) <= 1
        && rest.lastRules.length === 1 && rest.lastRules[0] <= rest.axis.top,
      JSON.stringify([rest.axis, rest.rows[0].plot, rest.nowTick, rest.nowRule, rest.lastRules]));
    check("1280 px: nothing wider than the window", await page.evaluate(overflow) <= 0, String(await page.evaluate(overflow)));

    // The keys: the focus reads now, left and right move six hours, up and down choose the row whose accounts show.
    await page.locator("#hub-left .left-rows").focus();
    const focused = await page.evaluate(graphNow);
    check("the focus on the rows reads now in a tooltip: a row a week, one figure each, a rule and a dot in every plot",
      read(focused) && focused.tip.when === "Now" && focused.tip.heads.length === 0 && String(groups(focused.tip).map((row) => row.name)) === NAMES
        && String(column(groups(focused.tip), 0)) === "40 %,55 %,60 %" && focused.rows.every((row) => row.dots === 1), JSON.stringify([focused.tip, focused.rows]));
    check("under the row being read the tooltip lists its accounts, each with its share and its next reset",
      groups(focused.tip)[0].on && accounts(focused.tip).length === 2 && /^one, resets \S/.test(accounts(focused.tip)[0].name) && /^two, resets \S/.test(accounts(focused.tip)[1].name)
        && String(column(accounts(focused.tip), 0)) === "60 %,20 %", JSON.stringify(focused.tip));
    check("a screen reader hears the reading, each account with its next reset",
      /^Now: Claude all models 40 % left; Claude Fable 55 % left; Codex 60 % left\. Claude all models by account: one, resets [^;]+, 60 % left; two, resets [^;]+, 20 % left\./.test(focused.said), focused.said);
    await press(page, "ArrowRight", 12);
    const ahead = await page.evaluate(graphNow);
    await page.locator("#hub-left").screenshot({ path: path.join(work, "left-1280-read.png") });
    check("three days ahead the tooltip heads two columns, and the first says what is left if nothing more is used, with the reset of one Claude account in it",
      read(ahead) && String(ahead.tip.heads) === "If unused,At pace" && String(column(groups(ahead.tip), 0)) === "80 %,85 %,60 %"
        && String(column(accounts(ahead.tip), 0)) === "60 %,100 %" && /, in 3 d$/.test(ahead.tip.when) && ahead.rows.every((row) => row.dots === 2), JSON.stringify([ahead.tip, ahead.rows]));
    check("the second says what is left at the recent pace, which is no more than that",
      ahead.tip.rows.every((row) => row.figures.length === 2 && share(row.figures[1]) <= share(row.figures[0])), JSON.stringify(ahead.tip.rows));
    check("the rows keep the share left now while another time is read, and the tooltip stands beside the rule, inside the rows",
      String(figures(ahead)) === NOW && beside(ahead), JSON.stringify([figures(ahead), ahead.tip.box, ahead.rule, ahead.area]));
    check("the reading's rule ends above the time scale, and a screen reader hears how far ahead it is",
      ahead.lastRules.length === 2 && ahead.lastRules.every((bottom) => bottom <= ahead.axis.top) && /^\S.*, in 3 d: Claude all models /.test(ahead.said),
      JSON.stringify([ahead.lastRules, ahead.axis, ahead.said]));
    await page.keyboard.press("ArrowDown");
    const second = await page.evaluate(graphNow);
    check("the down arrow reads the next row: its accounts stand in the tooltip and are said",
      groups(second.tip)[1].on && !groups(second.tip)[0].on && String(column(accounts(second.tip), 0)) === "70 %,100 %"
        && /Claude Fable by account: one(, resets [^;]+)?, 70 % if nothing more is used/.test(second.said), JSON.stringify([second.tip, second.said]));
    await press(page, "ArrowDown", 3);
    const third = await page.evaluate(graphNow);
    check("the down arrow stops at the last row, Codex with its one account",
      groups(third.tip)[2].on && accounts(third.tip).length === 1 && /^three, resets \S/.test(accounts(third.tip)[0].name) && String(column(accounts(third.tip), 0)) === "60 %", JSON.stringify(third.tip));
    await press(page, "ArrowUp", 3);
    await page.keyboard.press("End");
    const end = await page.evaluate(graphNow);
    check("End reads seven days ahead, when every account has reset, with the tooltip on the rule's other side",
      read(end) && end.tip.rows.length === 5 && end.tip.rows.every((row) => row.figures[0] === "100 %") && beside(end) && end.tip.box.right < end.rule, JSON.stringify([end.tip, end.rule]));
    await page.keyboard.press("Escape");
    const cleared = await page.evaluate(graphNow);
    check("Escape takes the reading away", clear(cleared) && String(figures(cleared)) === NOW, JSON.stringify([cleared.tip, cleared.rows]));
    await press(page, "ArrowLeft", 4);
    const before = await page.evaluate(graphNow);
    check("a day back the tooltip says what was left then, more than now, in one column",
      read(before) && /, 1 d ago$/.test(before.tip.when) && before.tip.heads.length === 0 && before.tip.rows.every((row) => row.figures.length === 1)
        && groups(before.tip).every((row, at) => share(row.figures[0]) > share(figures(rest)[at]) && share(row.figures[0]) < 100) && before.rows.every((row) => row.dots === 1),
      JSON.stringify([before.tip, before.rows]));
    await page.keyboard.press("Home");
    const start = await page.evaluate(graphNow);
    check("Home reads seven days back, before the accounts' weeks began, where there is no figure and no dot",
      read(start) && String(column(groups(start.tip), 0)) === "–,–,–" && start.rows.every((row) => row.dots === 0) && beside(start), JSON.stringify([start.tip, start.rows]));
    await page.evaluate(() => document.activeElement.blur());
    const blurred = await page.evaluate(graphNow);
    check("the focus leaving the rows takes the reading away", clear(blurred), JSON.stringify([blurred.tip, blurred.rows]));

    // The pointer reads the time under it in every plot, and the accounts of the row it is over.
    const plot = await page.locator("#hub-left .left-plot").nth(2).boundingBox();
    await page.mouse.move(plot.x + plot.width * 0.9, plot.y + plot.height / 2);
    const hovered = await page.evaluate(graphNow);
    check("the pointer over one plot reads all three at its time and lists that row's accounts",
      read(hovered) && groups(hovered.tip)[2].on && accounts(hovered.tip).length === 1 && String(hovered.tip.heads) === "If unused,At pace"
        && String(column(groups(hovered.tip), 0)) === "100 %,100 %,100 %" && beside(hovered), JSON.stringify([hovered.tip, hovered.rule, hovered.area]));
    const label = await page.locator("#hub-left .left-label").nth(2).boundingBox();
    await page.mouse.move(label.x + label.width / 2, label.y + label.height / 2);
    const labelled = await page.evaluate(graphNow);
    check("the pointer over a row's words reads nothing", clear(labelled), JSON.stringify([labelled.tip, labelled.rows]));
    await page.mouse.move(plot.x + plot.width / 2, plot.y + plot.height / 2);
    const again = await page.evaluate(graphNow);
    await page.mouse.move(plot.x + plot.width / 2, rest.area.top - 80);
    const gone = await page.evaluate(graphNow);
    check("the pointer leaving the rows takes the reading away", read(again) && clear(gone) && String(figures(gone)) === NOW, JSON.stringify([again.tip, gone.tip, gone.rows]));

    // The same figures as a table.
    await page.evaluate(() => { document.getElementById("spend-more").open = true; });
    // The fold grows open, and its content counts as shown once that has begun.
    await page.waitForFunction(() => document.getElementById("spend-left").checkVisibility(), null, { timeout: 10e3 }).catch(() => {});
    const table = await page.evaluate(() => {
      const part = document.getElementById("spend-left");
      return {
        shown: part.checkVisibility(),
        heads: [...part.querySelectorAll("thead th")].map((el) => el.textContent),
        parts: [...part.querySelectorAll(".left-part th")].map((el) => el.textContent),
        now: [...part.querySelectorAll("tbody tr")].filter((row) => row.cells[0].textContent === "Now").map((row) => [...row.cells].slice(1).map((cell) => cell.textContent)),
        last: [...part.querySelectorAll("tbody")].map((body) => [...body.rows[body.rows.length - 1].cells].slice(1).map((cell) => cell.textContent)),
      };
    });
    check("All the tables holds the graph's figures: a column a row of the graph, a row group a line",
      table.shown && String(table.heads) === `When,${NAMES}` && String(table.parts) === "So far,If nothing more is used,At the recent pace", JSON.stringify(table));
    check("the table's Now row is the rows' figures, and its last day ahead is after every reset",
      String(table.now) === "40 %,55 %,60 %" && String(table.last[1]) === "100 %,100 %,100 %", JSON.stringify(table));

    // A load that fails keeps the graph and says how old it is.
    const old = () => {
      const note = document.querySelector("#hub-left .left-old");
      return { note: note ? note.textContent : "", rows: document.querySelectorAll("#hub-left .left-row").length };
    };
    await page.route("**/api/hub/left", (route) => route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "the graph is out" }) }));
    await page.evaluate(() => loadLeft());
    const stale = await page.evaluate(old);
    check("a load that fails keeps the three rows under a line that says since when they are old, and why",
      stale.rows === 3 && /^Not updated since .+: .*the graph is out/.test(stale.note), JSON.stringify(stale));
    await page.unrouteAll({ behavior: "wait" });
    await page.locator("#hub-left .left-rows").focus();
    await press(page, "ArrowRight", 4);
    await page.evaluate(() => loadLeft());
    const fresh = await page.evaluate(old);
    const kept = await page.evaluate(graphNow);
    const focusKept = await page.evaluate(() => document.activeElement.matches("#hub-left .left-rows"));
    check("the next load that works takes the line away", fresh.rows === 3 && fresh.note === "", JSON.stringify(fresh));
    check("a redraw under the keys keeps the focus and the reading", focusKept && read(kept) && /, in 1 d$/.test(kept.tip.when), JSON.stringify([focusKept, kept.tip]));
    await page.setViewportSize({ width: 960, height: page.viewportSize().height });
    // The page hears of the new width at its next frame, so the reading moves after that.
    await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))));
    const narrower = await page.evaluate(graphNow);
    check("a narrower window keeps the reading, its tooltip beside the rule where that now stands",
      read(narrower) && narrower.rule !== kept.rule && beside(narrower), JSON.stringify([kept.rule, narrower.rule, narrower.tip, narrower.area]));
    await context.close();

    const dark = await open(1280, "dark");
    await dark.page.locator("#hub-left").screenshot({ path: path.join(work, "left-1280-dark.png") });
    await dark.page.locator("#hub-left .left-rows").focus();
    await press(dark.page, "ArrowRight", 12);
    await dark.page.locator("#hub-left").screenshot({ path: path.join(work, "left-1280-dark-read.png") });
    await dark.context.close();

    const phone = await open(390, "light");
    const small = await phone.page.evaluate(graphNow);
    await phone.page.locator("#hub-left").screenshot({ path: path.join(work, "left-390.png") });
    check("390 px: each row's words stand over its plot, the plots one under the other on one edge and as wide as the page",
      one(small.rows.map((row) => row.plot.left)) && small.rows.every((row) => row.plot.width >= 340 && row.label.bottom <= row.plot.top && row.label.left === row.plot.left)
        && String(figures(small)) === NOW, JSON.stringify(small.rows.map((row) => [row.label, row.plot, row.figure])));
    const tapped = await phone.page.locator("#hub-left .left-plot").nth(1).boundingBox();
    await phone.page.touchscreen.tap(tapped.x + tapped.width * 0.75, tapped.y + tapped.height / 2);
    const touched = await phone.page.evaluate(graphNow);
    await phone.page.locator("#hub-left").scrollIntoViewIfNeeded();
    await phone.page.screenshot({ path: path.join(work, "left-390-read.png") });
    check("390 px: a tap on a plot reads that time in every plot and lists the tapped row's accounts",
      read(touched) && groups(touched.tip)[1].on && accounts(touched.tip).length === 2 && String(touched.tip.heads) === "If unused,At pace", JSON.stringify(touched.tip));
    check("390 px: the tooltip stays on the page, beside the rule or under the rows",
      touched.tip !== null && touched.tip.box.left >= 0 && touched.tip.box.right <= 390
        && (touched.tip.box.left > touched.rule || touched.tip.box.right < touched.rule || touched.tip.box.top >= touched.area.bottom), JSON.stringify([touched.tip && touched.tip.box, touched.rule, touched.area]));
    check("390 px: nothing wider than the window", await phone.page.evaluate(overflow) <= 0, String(await phone.page.evaluate(overflow)));
    await phone.context.close();

    // A machine without a hub.
    const bare = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    await bare.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
    const none = await bare.newPage();
    none.on("pageerror", (error) => pageErrors.push(error.message));
    await none.route("**/api/hub", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ hub: null }) }));
    await none.route("**/api/hub/left", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ left: null }) }));
    await none.goto(`${info.origin}/usage`);
    await none.waitForSelector("#spend-limits #limits-title", { timeout: 60e3 });
    await none.evaluate(() => { document.getElementById("spend-more").open = true; });
    const without = await none.evaluate(() => ["hub-left", "spend-left"].filter((id) => document.getElementById(id).checkVisibility()));
    check("on a machine without a hub neither the graph nor its table shows", without.length === 0, String(without));
    await none.unrouteAll({ behavior: "wait" });
    await bare.close();
    check("no page errors", pageErrors.length === 0, pageErrors.join(" | "));
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failedChecks = results.filter((result) => !result.ok);
  console.error(`${results.length - failedChecks.length}/${results.length} checks passed; work dir ${work}`);
  process.exitCode = failedChecks.length ? 1 : 0;
}

// The harness's cookie file, once it is written and the server answers.
async function harnessUp(work, server) {
  const file = path.join(work, "cookie.json");
  const end = Date.now() + 180e3;
  while (Date.now() < end) {
    if (server.exitCode !== null) throw new Error(`serve_preview.py exited with ${server.exitCode}`);
    if (fs.existsSync(file)) {
      const info = JSON.parse(fs.readFileSync(file, "utf8"));
      const up = await fetch(`${info.origin}/healthz`).then((response) => response.ok, () => false);
      if (up) return info;
    }
    await sleep(200);
  }
  throw new Error("serve_preview.py did not come up within 180 s");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
