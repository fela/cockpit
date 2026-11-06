// Snoozing a warning in Inbox (SPEC UI-72), at 1280 px in the light look and 390 px in the dark: claude:personal signed out and a
// signed-out Claude account. Checks that
// - the opened warning's card has a ⋯ menu at its top right with Snooze, a step's four times and "Other time…", whose
//   dialog names the warning and what stays broken;
// - a time asks once more, in the menu, naming the time and what stays broken; Cancel goes back to the times and
//   nothing is snoozed;
// - confirming takes the row out of Inbox at once, also while Cockpit has not answered yet and polls come in, and the card on to the next warning, with Undo in the toast; Undo brings
//   it back;
// - a snooze holds after a reload (Cockpit keeps it), and the other warning stays;
// - the snoozed warning is a row under Waiting's Snoozed by you with a clock; it opens the card, which says until when
//   and whose menu holds Unsnooze; Unsnooze brings the row back to Inbox;
// - nothing is wider than the window, and the page throws no errors.
//
// Serves this checkout with serve_preview.py and fills each /api/state answer with the two warnings. Prints one line
// per check, saves screenshots as warning-snooze-<width>-*.png in the work folder, and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/warning_snooze.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;
const ROOT = path.resolve(__dirname, "..", "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const AGO = new Date(Date.now() - 20 * 60e3).toISOString();
const WARNINGS = {
  warnings: [], account_problems: [], team: null,
  codex_sign_ins: [],
  claude_sign_ins: [{ account: "personal", signed_out: true, text: "The personal Claude account is signed out." }, { account: "work", signed_out: true, text: "The work Claude account is signed out." }],
  wrong_models: [], permission_requests: [], settled_requests: [], waiting_sessions: [], codex_requests: [],
};

let failures = 0;
function check(ok, words) {
  console.log(`${ok ? "ok  " : "FAIL"} ${words}`);
  if (!ok) failures += 1;
}

const rowWords = (page) => page.evaluate(() => [...document.querySelectorAll("#sidebar li.warn-row")].map((row) => row.textContent.trim()));

async function main() {
  const work = workFolder("cockpit-warning-snooze-");
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser;
  try {
    browser = await chromium.launch({ executablePath: CHROMIUM });
    const info = await harnessUp(work, server);
    for (const [width, scheme] of [[1280, "light"], [390, "dark"]]) {
      const phone = width < 600;
      const at = `${width} px ${scheme}`;
      // Each width starts with nothing snoozed.
      await fetch(`${info.origin}/api/warnings/snooze`, { method: "POST", headers: { "content-type": "application/json", origin: info.origin, cookie: `${info.name}=${info.value}` },
        body: JSON.stringify({ key: "claude:personal", until: null }) });
      const context = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: scheme });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      await context.route("**/api/state", async (route) => {
        const response = await route.fetch().catch(() => null);
        if (!response) return;
        const state = await response.json();
        await route.fulfill({ response, json: { ...state, ...WARNINGS, away: null } }).catch(() => {});
      });
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", (error) => errors.push(String(error)));
      await page.goto(`${info.origin}/`, { waitUntil: "load", timeout: 120e3 });
      await page.waitForSelector("#sidebar li.warn-row", { state: "attached", timeout: 240e3 });
      await page.evaluate(() => { ui.placed.clear(); openWarning("claude:personal"); });
      await sleep(400);
      if (phone) await page.evaluate(() => setPhoneScreen(false, false));
      await sleep(300);

      const card = '#next article.warn-step[data-warning="claude:personal"]';
      const key = `${card} .warn-menu > summary`;
      check(await page.locator(key).isVisible(), `${at}: the claude:personal card has a ⋯ menu`);
      const place = await page.evaluate((selector) => {
        const cardBox = document.querySelector(selector).getBoundingClientRect();
        const keyBox = document.querySelector(`${selector} .warn-menu > summary`).getBoundingClientRect();
        return { right: cardBox.right - keyBox.right, top: keyBox.top - cardBox.top };
      }, card);
      check(place.right >= 0 && place.right < 40 && place.top >= 0 && place.top < 40, `${at}: the ⋯ sits at the card's top right ${JSON.stringify(place)}`);
      await page.click(key);
      await page.click(`${card} .warn-menu .menu-opens`);
      const times = await page.evaluate((selector) => [...document.querySelectorAll(`${selector} .warn-menu .menu-choices .menu-label`)].map((one) => one.textContent), card);
      check(JSON.stringify(times) === JSON.stringify(["5 minutes", "2 hours", "12 hours", "3 days", "Other time…"]), `${at}: Snooze holds a step's choices ${JSON.stringify(times)}`);
      await page.click(`${card} .warn-menu .menu-choices button:nth-child(5)`);
      await sleep(300);
      const dialog = await page.evaluate(() => {
        const open = document.querySelector("dialog[open]");
        return open ? open.textContent : "";
      });
      check(dialog.includes("Snooze until") && dialog.includes("Claude personal is signed out") && dialog.includes("moves to Waiting") && dialog.includes("The problem stays until it is fixed."),
        `${at}: Other time… asks in a dialog, naming what stays broken ${JSON.stringify(dialog.slice(0, 160))}`);
      await page.click("dialog[open] .time-actions .ghost");
      await sleep(200);
      check((await rowWords(page)).some((words) => words.startsWith("Claude personal")), `${at}: cancelling the dialog snoozes nothing`);
      await page.click(key);
      if (!(await page.locator(`${card} .warn-menu .menu-choices`).isVisible())) await page.click(`${card} .warn-menu .menu-opens`);
      await page.click(`${card} .warn-menu .menu-choices button:nth-child(2)`);
      await sleep(200);
      const ask = await page.evaluate((selector) => document.querySelector(`${selector} .warn-snooze-confirm`)?.textContent || "", card);
      check(ask.includes("Hide this warning for 2 hours?") && ask.includes("The problem stays until it is fixed.") && ask.includes("Snooze 2 hours") && ask.includes("Cancel"),
        `${at}: a time asks once more, naming what stays broken ${JSON.stringify(ask)}`);
      check((await rowWords(page)).some((words) => words.startsWith("Claude personal")), `${at}: nothing snoozed before the confirmation`);
      await page.screenshot({ path: path.join(work, `warning-snooze-${width}-confirm.png`) });
      await page.click(`${card} .warn-snooze-confirm button.ghost`);
      await sleep(200);
      const back = await page.evaluate((selector) => ({ confirm: Boolean(document.querySelector(`${selector} .warn-snooze-confirm`)),
        open: document.querySelector(`${selector} .warn-menu`)?.open }), card);
      check(!back.confirm && back.open, `${at}: Cancel goes back to the times with the menu open ${JSON.stringify(back)}`);

      await page.click(`${card} .warn-menu .menu-choices button:nth-child(2)`);
      await page.click(`${card} .warn-snooze-confirm button.primary`);
      await sleep(800);
      if (phone) await page.evaluate(() => setPhoneScreen(true, false));
      let rows = await rowWords(page);
      check(!rows.some((words) => words.startsWith("Claude personal")) && rows.some((words) => words.startsWith("Claude work")),
        `${at}: confirmed, the claude:personal row leaves Inbox and the other stays ${JSON.stringify(rows)}`);
      const shown = await page.evaluate(() => document.querySelector("#next article.warn-step")?.dataset.warning || null);
      check(shown === "claude:work", `${at}: the card moves on to the next warning (${shown})`);
      const toastText = await page.evaluate(() => document.getElementById("toast").textContent);
      check(toastText.includes("Snoozed “Claude personal is signed out” for 2 hours") && toastText.includes("Undo"), `${at}: the toast says so with Undo ${JSON.stringify(toastText)}`);
      // Pressed at once: the toast leaves on its own after a while, sooner than a busy machine's click lands.
      await page.evaluate(() => document.querySelector("#toast button").click());
      await page.waitForFunction(() => [...document.querySelectorAll("#sidebar li.warn-row")].some((row) => row.dataset.warning === "claude:personal"),
        null, { timeout: 15e3 }).catch(() => {});
      rows = await rowWords(page);
      check(rows.some((words) => words.startsWith("Claude personal")), `${at}: Undo brings the row back ${JSON.stringify(rows)}`);

      // Snooze again, then reload: Cockpit keeps it.
      if (phone) await page.evaluate(() => setPhoneScreen(false, false));
      await page.evaluate(() => openWarning("claude:personal"));
      await sleep(300);
      await page.click(key);
      await page.click(`${card} .warn-menu .menu-opens`);
      // This snooze reaches Cockpit only after polls came in: the row stays away meanwhile.
      await context.route("**/api/warnings/snooze", async (route) => { await sleep(7000); await route.continue().catch(() => {}); });
      let polls = 0;
      page.on("response", (response) => { if (response.url().endsWith("/api/state")) polls += 1; });
      await page.click(`${card} .warn-menu .menu-choices button:nth-child(3)`);
      await page.click(`${card} .warn-snooze-confirm button.primary`);
      await sleep(6000);
      const meanwhile = await rowWords(page);
      check(polls > 0 && !meanwhile.some((words) => words.startsWith("Claude personal")), `${at}: while the snooze is on its way, ${polls} polls leave the row away`);
      await sleep(2500);
      await page.reload({ waitUntil: "load", timeout: 180e3 });
      await page.waitForSelector("#sidebar li.warn-row", { state: "attached", timeout: 240e3 });
      await sleep(500);
      if (phone) await page.evaluate(() => setPhoneScreen(true, false));
      rows = await rowWords(page);
      check(!rows.some((words) => words.startsWith("Claude personal")) && rows.length === 1, `${at}: after a reload the snooze holds ${JSON.stringify(rows)}`);

      // Waiting lists it under Snoozed by you, with a clock; it opens the card, whose menu holds Unsnooze.
      const waiting = await page.evaluate(() => {
        const row = document.querySelector('#sidebar li.warn-snoozed[data-warning="claude:personal"]');
        if (!row) return null;
        for (let group = row.closest("details"); group; group = group.parentElement.closest("details")) group.open = true;
        let heading = row.previousElementSibling;
        while (heading && !heading.classList.contains("sidebar-sub")) heading = heading.previousElementSibling;
        return { heading: heading?.textContent || "", group: row.closest(".sidebar-group")?.classList.contains("waiting"),
          clock: Boolean(row.querySelector(".sidebar-snoozed")), time: row.querySelector(".sidebar-age")?.textContent };
      });
      check(waiting && waiting.group && waiting.heading.startsWith("Snoozed by you") && waiting.clock && /^1[12]h$/.test(waiting.time),
        `${at}: the snoozed warning sits in Waiting under Snoozed by you ${JSON.stringify(waiting)}`);
      await page.evaluate(() => document.querySelector('#sidebar li.warn-snoozed[data-warning="claude:personal"] .side-row').click());
      await sleep(400);
      if (phone) await page.evaluate(() => setPhoneScreen(false, false));
      const note = await page.evaluate((selector) => document.querySelector(`${selector} .warn-snoozed-note`)?.textContent || "", card);
      check(note.startsWith("Snoozed until"), `${at}: its card says until when ${JSON.stringify(note)}`);
      await page.click(key);
      await sleep(200);
      const entries = await page.evaluate((selector) => [...document.querySelectorAll(`${selector} .warn-menu .menu-list [role=menuitem]`)].map((one) => one.textContent), card);
      check(JSON.stringify(entries) === JSON.stringify(["Unsnooze"]), `${at}: its menu holds Unsnooze ${JSON.stringify(entries)}`);
      await page.screenshot({ path: path.join(work, `warning-snooze-${width}-after.png`) });
      await page.click(`${card} .warn-menu .menu-list [role=menuitem]`);
      await page.waitForFunction(() => [...document.querySelectorAll("#sidebar li.warn-row")].some((row) => row.dataset.warning === "claude:personal"),
        null, { timeout: 15e3 }).catch(() => {});
      rows = await rowWords(page);
      const gone = await page.evaluate(() => !document.querySelector("#sidebar li.warn-snoozed"));
      check(rows.some((words) => words.startsWith("Claude personal")) && gone, `${at}: Unsnooze brings the row back to Inbox ${JSON.stringify(rows)}`);

      const wide = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      check(wide <= 0, `${at}: nothing wider than the window (${wide} px over)`);
      check(errors.length === 0, `${at}: no page errors ${JSON.stringify(errors)}`);
      await context.close();
    }
  } finally {
    try {
      if (browser) await browser.close();
    } finally {
      server.kill();
      await new Promise((resolve) => (server.exitCode !== null || server.signalCode !== null ? resolve() : server.once("exit", resolve)));
    }
  }
  console.log(`screenshots in ${work}`);
  process.exitCode = failures ? 1 : 0;
}

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
