// "Other time…" under Snooze snoozes at once and Cockpit reads the words behind it (VIEW-29). Checks, at laptop width
// in light and phone width in dark:
// - the dialog closes as soon as the words are sent, while the server takes seconds to answer;
// - the toast says the step is snoozed while Cockpit reads the words, and the snoozed row says "Reading";
// - words with a time move the snooze there, and the toast says until when;
// - words with no time bring the step back, with the reason in a toast and on its card;
// - no page errors and nothing wider than the page.
//
// Starts serve_slow.py (this checkout's Cockpit on a synthetic database, writes delayed two seconds, the reading of
// typed times faked: "in N minutes" reads, other words name no time) and drives a headless Chromium. Prints one line
// per check and exits 1 when one failed. Screenshots go to the folder given as the first argument, when one is given.
//
// Usage, from the checkout: node tests/browser/snooze_words.cjs [screenshot folder]

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
// The harness's agent token and second fake session (tests/conftest.py).
const SESSION_B = "22222222-2222-4222-8222-222222222222";
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "snooze-words-test", "content-type": "application/json" };

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const shots = process.argv[2] || null;
  if (shots) fs.mkdirSync(shots, { recursive: true });
  const work = workFolder("cockpit-snooze-words-");
  const server = spawn(PYTHON, [path.join(__dirname, "serve_slow.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const info = await harnessUp(work, server);
    const cookie = { cookie: `${info.name}=${info.value}`, origin: info.origin, "content-type": "application/json" };
    const agent = (method, url, body) => call(info.origin, AGENT, method, url, body);
    const user = (method, url, body) => call(info.origin, cookie, method, url, body);

    // The fixture database's open steps are snoozed and its timer paused, so this run's step is the one in front.
    const initial = await user("GET", "/api/state");
    if (initial.timer) await user("POST", `/api/steps/${initial.timer.step_id}/timer/pause`);
    await Promise.all([...initial.queue, ...initial.ready_next].map((step) => user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
    browser = await chromium.launch({ executablePath: CHROMIUM });

    for (const [width, scheme] of [[1280, "light"], [390, "dark"]]) {
      const context = await browser.newContext({ viewport: { width, height: 900 }, isMobile: width < 600, hasTouch: width < 600, colorScheme: scheme });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      page.setDefaultTimeout(5000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
      const toastText = () => page.locator("#toast").evaluate((el) => (el.hidden ? "" : el.textContent));
      const waitForToast = async (words, ms) => {
        const end = Date.now() + ms;
        let text = "";
        while (Date.now() < end) {
          text = await toastText();
          if (text.includes(words)) return text;
          await sleep(100);
        }
        return text;
      };

      // A step snoozed in words that name a time.
      const read = await newStep(agent, width, "read");
      await page.goto(`${info.origin}/`, { timeout: 20000 });
      // The server takes two seconds to answer a write: what shows before its answer showed at once.
      let answered = false;
      const onAnswer = (response) => { if (response.url().endsWith(`/api/steps/${read}/snooze`)) answered = true; };
      page.on("response", onAnswer);
      await snoozeInWords(page, read, "in 40 minutes");
      const closed = await page.locator("#time-dialog").evaluate((el) => !el.open);
      const first = await waitForToast("Snoozed while Cockpit reads “in 40 minutes”", 1500);
      const early = !answered;
      page.off("response", onAnswer);
      check(`${width}: the dialog closes as soon as the words are sent`, closed, "the dialog is still open");
      check(`${width}: the toast says it is snoozed while Cockpit reads the words`, first.includes("Snoozed while Cockpit reads “in 40 minutes”"), first);
      check(`${width}: both before the server answered the snooze`, early, "the server answered first");
      // The step's row in Waiting, under Snoozed by you, in the list at the left.
      const row = page.locator(`.side-item.snoozed[data-step="${read}"]`).first();
      let age = "";
      for (let n = 0; n < 40 && age !== "Reading"; n += 1) {
        await sleep(150);
        age = await row.locator(".sidebar-age").first().textContent({ timeout: 300 }).catch(() => "").then((text) => (text || "").trim());
      }
      check(`${width}: the snoozed row says "Reading" while Cockpit reads`, age === "Reading", age);
      if (shots) await page.screenshot({ path: path.join(shots, `reading-${width}-${scheme}.png`) });
      const settled = await waitForToast("Snoozed until", 12000);
      check(`${width}: once read, the toast says until when`, /^Snoozed until /.test(settled), settled);
      if (shots) await page.screenshot({ path: path.join(shots, `read-${width}-${scheme}.png`) });
      const moved = (await user("GET", "/api/state")).snoozed.find((step) => step.id === read);
      const minutes = moved ? (Date.parse(moved.snoozed_until) - Date.now()) / 60000 : NaN;
      check(`${width}: the snooze moved to 40 minutes from now`, minutes > 36 && minutes <= 40, `${minutes} minutes`);
      await user("POST", `/api/steps/${read}/snooze`, { until: "2099-01-01T08:00:00Z" });

      // A step snoozed in words that name no time Cockpit can find.
      const unread = await newStep(agent, width, "unread");
      await page.reload({ timeout: 20000 });
      await snoozeInWords(page, unread, "just before standup");
      const back = await waitForToast("Not snoozed", 14000);
      check(`${width}: words with no time bring the step back with the reason`, back.includes("Not snoozed: no such event in the calendar"), back);
      check(`${width}: the toast offers Other time…`, await page.locator("#toast button", { hasText: "Other time…" }).count() === 1, "no button");
      await sleep(600);
      const state = await user("GET", "/api/state");
      check(`${width}: the step is back in the queue`, state.queue.some((step) => step.id === unread), JSON.stringify(state.queue.map((step) => step.id)));
      const card = page.locator(`#next [data-step="${unread}"]`).first();
      const note = card.locator(".snooze-unread");
      const noted = await note.textContent().catch(() => "");
      check(`${width}: its card says why it is back`, (noted || "").includes("Not snoozed: no such event in the calendar"), noted);
      const wide = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      check(`${width}: nothing is wider than the page`, wide <= 0, `${wide} px`);
      if (shots) await page.screenshot({ path: path.join(shots, `unread-${width}-${scheme}.png`) });
      // Other time… in the toast opens the dialog with the words typed.
      await page.locator("#toast button", { hasText: "Other time…" }).click();
      const again = await page.locator("#time-dialog .snooze-words").inputValue();
      check(`${width}: Other time… opens the dialog with the words`, again === "just before standup", again);
      await page.keyboard.press("Escape");

      check(`${width}: no page errors`, errors.length === 0, errors.join(" | "));
      await user("POST", `/api/steps/${unread}/snooze`, { until: "2099-01-01T08:00:00Z" });
      await context.close();
    }
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failedChecks = results.filter((result) => !result.ok);
  console.error(failedChecks.length ? `${failedChecks.length} of ${results.length} checks failed` : `all ${results.length} checks passed`);
  process.exitCode = failedChecks.length ? 1 : 0;
}

// An urgent step on an item of its own, in front of the queue; its id.
async function newStep(agent, width, name) {
  const key = `note:snooze-words-${name}-${width}-${Date.now()}`;
  await agent("PUT", "/api/items", { key, title: `Snooze words ${name}`, priority: "urgent" });
  const answer = await agent("POST", "/api/steps", {
    item_key: key, need: `review:${name}`, ask: `Snooze words ${name}`, score: 100, session_id: SESSION_B,
    body: "The step snoozed in the user's own words.",
  });
  return answer.step.id;
}

// Opens the step's ⋯ menu, Snooze, Other time…, types the words and sends them. A redraw from the page's poll can
// replace the menu under the pointer, so the menu is opened again, up to three times.
async function snoozeInWords(page, id, words) {
  const card = page.locator(`#next [data-step="${id}"]`).first();
  await card.waitFor({ timeout: 20000 });
  const field = page.locator("#time-dialog .snooze-words");
  for (let attempt = 1; ; attempt += 1) {
    try {
      const menu = card.locator(".step-menu").first();
      if (!(await menu.evaluate((el) => el.open))) await menu.locator("> summary").click();
      const other = menu.locator("button", { hasText: "Other time…" }).first();
      if (!(await other.isVisible())) await menu.locator(".menu-opens", { hasText: "Snooze" }).first().click();
      await other.click();
      await field.waitFor();
      break;
    } catch (error) {
      if (attempt === 3) throw error;
      await page.keyboard.press("Escape");
      await sleep(300);
    }
  }
  await field.fill(words);
  await field.press("Enter");
  await sleep(150);
}

async function call(origin, headers, method, url, body) {
  const response = await fetch(`${origin}${url}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await response.text();
  if (!response.ok) throw new Error(`${method} ${url}: ${response.status} ${text}`);
  return text ? JSON.parse(text) : null;
}

// The harness's cookie file, once it is written and the server answers.
async function harnessUp(work, server) {
  const file = path.join(work, "cookie.json");
  const end = Date.now() + 60e3;
  while (Date.now() < end) {
    if (server.exitCode !== null) throw new Error(`serve_slow.py exited with ${server.exitCode}`);
    if (fs.existsSync(file)) {
      const info = JSON.parse(fs.readFileSync(file, "utf8"));
      const up = await fetch(`${info.origin}/healthz`).then((response) => response.ok, () => false);
      if (up) return info;
    }
    await sleep(200);
  }
  throw new Error("serve_slow.py did not come up within 60 s");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
