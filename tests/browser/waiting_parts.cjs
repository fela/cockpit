// Waiting's parts by what the work waits for. On the page with the list at its left, at 1280 px dark and
// 390 px light, checks:
// - opened, Waiting shows a small heading per kind of wait, in the page's order (WAIT_PARTS), each with its count;
// - every item row sits under the heading of the part the server gives it (`wait`), a snoozed step under Snoozed by you;
// - the headings' counts add up to Waiting's own count, which counts rows only;
// - the keyboard's J skips the headings.
// - a resting agent's row says on hover why it rests, and a rest the user chose sits under Snoozed by you
//   with a clock.
// Saves a screenshot of each in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what
// it needs) and drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/waiting_parts.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const ORDER = ["stuck", "blocked", "person", "bot", "staging", "dev", "release", "resumes", "snoozed"];


const SESSION_A = "11111111-1111-4111-8111-111111111111";
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "browser-check", "content-type": "application/json" };

async function call(info, headers, method, url, body) {
  const response = await fetch(`${info.origin}${url}`, { method, headers: { ...headers, connection: "close" },
    body: body === undefined ? undefined : JSON.stringify(body) });
  if (!response.ok) throw new Error(`${method} ${url}: HTTP ${response.status} ${await response.text()}`);
  return response.json();
}

async function main() {
  const work = workFolder("cockpit-waiting-parts-");
  const server = spawn(path.join(__dirname, "..", "..", ".venv", "bin", "python"), [path.join(__dirname, "serve_preview.py"), work],
    { stdio: ["ignore", "inherit", "inherit"] });
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const info = await harnessUp(work, server);
    const cookie = `${info.name}=${info.value}`;
    const until = new Date(Date.now() + 24 * 3600e3).toISOString();
    const examples = [
      ["choose", "Choose a list colour", "planning", [], []],
      ["idle", "Build list exports", "building", [SESSION_A], []],
      ["person", "Review list sharing", "review", [], [{ kind: "person", who: "Alex", what: "review", until }]],
      ["bot", "Check reminder tests", "review", [], [{ kind: "bot", who: "test-bot", until }]],
      ["staging", "Try shared lists", "staging", [], [{ kind: "release", who: "Tasks update", until }]],
      ["dev", "Try reminder picker", "dev", [], [{ kind: "release", who: "Tasks update", until }]],
      ["release", "Publish export option", "review", [], [{ kind: "release", who: "Tasks update", until }]],
      ["resumes", "Prepare weekly reminders", "building", [SESSION_A], [
        { kind: "agent", who: SESSION_A, until, why: "Check the reminder after a day" },
      ]],
      ["snoozed", "Check a shared list", "building", [SESSION_A], [
        { kind: "agent", who: SESSION_A, until, why: "Sam asked to check tomorrow", by_user: true },
      ]],
    ];
    for (const [key, title, stage, sessions, waiting_on] of examples) {
      await call(info, AGENT, "PUT", "/api/items", { key: `note:waiting-${key}`, title, stage, sessions, waiting_on });
    }
    const state = await fetch(`${info.origin}/api/state`, { headers: { cookie } }).then((response) => response.json());
    const waits = Object.fromEntries(state.items.map((item) => [String(item.id), item.wait]));
    const whys = Object.fromEntries(state.items.filter((item) => item.why).map((item) => [String(item.id), item.why]));

    browser = await chromium.launch({ executablePath: CHROMIUM });
    for (const [width, height, scheme] of [[1280, 900, "dark"], [390, 844, "light"]]) {
      const label = `${width} px ${scheme}`;
      const context = await browser.newContext({ viewport: { width, height }, colorScheme: scheme });
      await context.setExtraHTTPHeaders({ cookie });
      const page = await context.newPage();
      await page.addLocatorHandler(page.locator("dialog#idle[open]"), (dialog) => dialog.locator("button", { hasText: "Return" }).click()
        .catch((error) => { if (!/has been closed/.test(error.message)) throw error; }));
      page.setDefaultTimeout(45000);
      page.setDefaultNavigationTimeout(60000);
      const errors = [];
      page.on("pageerror", (error) => { errors.push(error.message); console.error(`page error: ${error.message}`); });
      await page.goto(`${info.origin}/`);
      const group = page.locator("#sidebar details.sidebar-group.waiting");
      await group.waitFor({ state: "attached" });
      // On a phone the list is a screen of its own, which the top bar's key opens.
      if (width === 390 && !(await group.isVisible())) await page.locator(".phone-back").click();
      await group.locator(":scope > summary").click();
      const seen = await group.evaluate((el) => {
        const parts = [];
        let current = null;
        const stray = [];
        for (const li of el.querySelectorAll(":scope > .sidebar-list > li")) {
          if (li.classList.contains("sidebar-sub")) {
            current = { kind: li.dataset.glide.split(" ").pop(), name: li.querySelector(".sidebar-sub-name").textContent,
              count: Number(li.querySelector(".group-count").textContent), rows: [] };
            parts.push(current);
          } else if (current) current.rows.push({ item: li.dataset.item, snoozed: li.classList.contains("snoozed"),
            hover: li.querySelector(".side-row")?.dataset.tip || li.querySelector(".side-row")?.title || "", clock: Boolean(li.querySelector(".sidebar-snoozed")) });
          else stray.push(li.dataset.item);
        }
        return { parts, stray, total: Number(el.querySelector(":scope > summary .group-count").textContent) };
      });
      check(`${label}: Waiting has headed parts`, seen.parts.length > 0, JSON.stringify(seen));
      check(`${label}: no row before the first heading`, seen.stray.length === 0, seen.stray.join(", "));
      const order = seen.parts.map((part) => ORDER.indexOf(part.kind));
      check(`${label}: parts in the page's order`, order.every((at, i) => at >= 0 && (i === 0 || at > order[i - 1])),
        seen.parts.map((part) => part.kind).join(", "));
      const wrong = seen.parts.flatMap((part) => part.rows
        .filter((row) => (part.kind === "snoozed" ? !row.snoozed : row.snoozed || waits[row.item] !== part.kind))
        .map((row) => `${row.item} under ${part.kind} (server: ${waits[row.item]})`));
      check(`${label}: each row under its own part`, wrong.length === 0, wrong.join("; "));
      const resting = seen.parts.filter((part) => part.kind === "resumes" || part.kind === "snoozed").flatMap((part) => part.rows)
        .filter((row) => !row.hover.startsWith("Snoozed until") && waits[row.item] && whys[row.item] !== undefined);
      const unexplained = resting.filter((row) => !row.hover.split("\n").includes(whys[row.item]));
      check(`${label}: ${resting.length} resting rows say why on hover`, resting.length > 0 && unexplained.length === 0,
        unexplained.map((row) => `${row.item}: ${JSON.stringify(row.hover)}`).join("; ") || "none with a why");
      const reminders = (seen.parts.find((part) => part.kind === "snoozed")?.rows || []).filter((row) => waits[row.item] === "snoozed");
      check(`${label}: ${reminders.length} reminders under Snoozed by you with a clock`,
        reminders.length > 0 && reminders.every((row) => row.clock), JSON.stringify(reminders));
      const counted = seen.parts.every((part) => part.count === part.rows.length);
      check(`${label}: each heading counts its rows`, counted, JSON.stringify(seen.parts.map((part) => [part.kind, part.count, part.rows.length])));
      const sum = seen.parts.reduce((total, part) => total + part.rows.length, 0);
      check(`${label}: Waiting's count is its rows (${seen.total})`, sum === seen.total, `${sum} rows, heading says ${seen.total}`);
      console.error(`    ${label}: ${seen.parts.map((part) => `${part.name} ${part.count}`).join(" | ")}`);
      // A poll can redraw the list while the shot is taken; try again on the new one.
      for (let attempt = 0; attempt < 3; attempt += 1) {
        try {
          await group.screenshot({ path: path.join(work, `waiting-${width}-${scheme}.png`) });
          break;
        } catch (error) {
          if (!/not attached/.test(error.message) || attempt === 2) throw error;
        }
      }
      if (width === 1280) {
        // J walks the rows; on the heading it would land on nothing. Walk the whole list and see no heading picked.
        await page.mouse.click(5, height - 5);
        let landedOnHeading = false;
        for (let i = 0; i < 60; i += 1) {
          await page.keyboard.press("j");
          landedOnHeading ||= await page.evaluate(() => Boolean(document.querySelector("#sidebar li.sidebar-sub.keyboard-current")));
        }
        check(`${label}: J never lands on a heading`, !landedOnHeading, "landed");
      }
      check(`${label}: no page errors`, errors.length === 0, errors.join(" | "));
      await context.close();
    }
    console.error(`  screenshots in ${work}`);
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failedChecks = results.filter((result) => !result.ok);
  console.error(failedChecks.length ? `${failedChecks.length} of ${results.length} checks failed` : `all ${results.length} checks passed`);
  process.exitCode = failedChecks.length ? 1 : 0;
}

// The harness's cookie file, once it is written and the server answers.
async function harnessUp(work, server) {
  const file = path.join(work, "cookie.json");
  const end = Date.now() + 120e3;
  while (Date.now() < end) {
    if (server.exitCode !== null) throw new Error(`serve_preview.py exited with ${server.exitCode}`);
    if (fs.existsSync(file)) {
      const info = JSON.parse(fs.readFileSync(file, "utf8"));
      const up = await fetch(`${info.origin}/healthz`).then((response) => response.ok, () => false);
      if (up) return info;
    }
    await sleep(200);
  }
  throw new Error("serve_preview.py did not come up within 120 s");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
