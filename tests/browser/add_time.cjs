// The user adds time to a running timer by hand (TRACK-7), from the step's clock's "Correct the time"; the page has no
// timer chip (TRACK-8). Checks, at laptop and phone width:
// - the page has no timer chip and no sideways scroll;
// - in "Correct the time", "+20m" offers "Add 20m", and pressing it adds 20 minutes on Cockpit while the timer runs on,
//   with no question about time counted while away;
// - "Correct the time" takes a higher total ("Adds …", "Set to …") as well as a lower one.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
// Screenshots go to the folder given as the first argument, when one is given.
//
// Usage, from the checkout: node tests/browser/add_time.cjs [screenshot folder]

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
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "add-time-test", "content-type": "application/json" };

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const shots = process.argv[2] || null;
  if (shots) fs.mkdirSync(shots, { recursive: true });
  const work = workFolder("cockpit-add-time-");
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
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
    // The item's time on Cockpit now, in seconds, counting a running span.
    const itemSeconds = async (key) => {
      const item = (await user("GET", "/api/state")).items.find((entry) => entry.key === key);
      const spent = item.time_spent;
      return spent.seconds + (spent.started_at ? (Date.now() - Date.parse(spent.started_at)) / 1000 : 0);
    };

    // The fixture database's open steps are snoozed and its timer paused, so this run's step is Next up.
    const initial = await user("GET", "/api/state");
    if (initial.timer) await user("POST", `/api/steps/${initial.timer.step_id}/timer/pause`);
    await Promise.all([...initial.queue, ...initial.ready_next, ...initial.snoozed].map((step) => user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
    browser = await chromium.launch({ executablePath: CHROMIUM });

    for (const width of [1440, 390]) {
      const key = `note:add-time-${Date.now()}`;
      await agent("PUT", "/api/items", { key, title: "Add time test", priority: "urgent" });
      const answer = await agent("POST", "/api/steps", {
        item_key: key, need: `review:add-time-${width}`, ask: "Add time test", score: 100, session_id: SESSION_B,
        body: "Add time test body.",
      });
      await user("POST", `/api/steps/${answer.step.id}/timer/start`);

      const context = await browser.newContext({ viewport: { width, height: 900 }, isMobile: width < 600, hasTouch: width < 600 });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      page.setDefaultTimeout(5000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
      await page.goto(`${info.origin}/classic`);
      const clockButton = page.locator('#next .step-menu [data-timer="running"]');
      await clockButton.waitFor({ state: "attached" });
      // The clock is in the card's ⋯ menu (VIEW-29), which opens first.
      const openClock = async () => { await page.locator("#next .step-menu > summary").click(); await clockButton.click(); };
      const chips = await page.locator("#timer, .timer-chip").count();
      check(`${width}: the page has no timer chip`, chips === 0, `${chips} found`);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth);
      check(`${width}: the page has no sideways scroll`, overflow <= 0, `${overflow}px too wide`);
      if (shots) await page.locator("header").first().screenshot({ path: path.join(shots, `header-${width}.png`) });

      const dialog = page.locator("#time-dialog");
      const field = dialog.locator("input.time-field");
      const submit = dialog.locator('button[type="submit"]');
      const hint = dialog.locator(".time-hint");

      await openClock();
      check(`${width}: the clock opens "Correct the time"`, (await dialog.locator("h2").textContent()) === "Correct the time", await dialog.locator("h2").textContent());
      await field.fill("+25h");
      check(`${width}: "+25h" reads "Adds at most 24 hours at once" and Set is off`,
        (await hint.textContent()) === "Adds at most 24 hours at once" && await submit.isDisabled(), await hint.textContent());
      await field.fill("+20m");
      check(`${width}: "+20m" reads "Adds 20m" and offers "Add 20m"`,
        (await hint.textContent()) === "Adds 20m" && (await submit.textContent()) === "Add 20m", `${await hint.textContent()} / ${await submit.textContent()}`);
      if (shots) await dialog.screenshot({ path: path.join(shots, `add-${width}.png`) });
      const before = await itemSeconds(key);
      await submit.click();
      await sleep(800);
      const after = await itemSeconds(key);
      check(`${width}: Cockpit counts 20 more minutes`, Math.abs(after - before - 1200) < 5, `${Math.round(after - before)} s more`);
      const state = await user("GET", "/api/state");
      check(`${width}: the timer runs on and asks nothing`, state.timer && state.timer.step_id === answer.step.id && !state.away && !(await page.locator("dialog#idle[open]").count()),
        JSON.stringify({ timer: state.timer, away: state.away }));
      const clock = await page.locator('#next .step-menu [data-timer="running"] .timer-clock').textContent();
      check(`${width}: the step's clock shows the added time`, clock.startsWith("20:"), clock);

      await openClock();
      await field.fill("1h");
      check(`${width}: a higher total reads "Adds 39m" or "Adds 40m" (the clock ran on)`, /^Adds (39|40)m$/.test(await hint.textContent()) && (await submit.textContent()) === "Set to 1h 0m", `${await hint.textContent()} / ${await submit.textContent()}`);
      await field.fill("5m");
      check(`${width}: a lower total reads "Removes 15m"`, (await hint.textContent()) === "Removes 15m", await hint.textContent());
      await field.fill("1h");
      await submit.click();
      await sleep(800);
      const set = await itemSeconds(key);
      check(`${width}: Cockpit counts 1h after "Set to 1h 0m"`, Math.abs(set - 3600) < 5, `${Math.round(set)} s`);

      check(`${width}: no page errors`, errors.length === 0, errors.join(" | "));
      await user("POST", `/api/steps/${answer.step.id}/timer/pause`);
      await context.close();
      // A paused step keeps its place as Next up; snoozed, it leaves Next up to the next width's step.
      await user("POST", `/api/steps/${answer.step.id}/snooze`, { until: "2099-01-01T08:00:00Z" });
    }
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failedChecks = results.filter((result) => !result.ok);
  console.error(failedChecks.length ? `${failedChecks.length} of ${results.length} checks failed` : `all ${results.length} checks passed`);
  process.exitCode = failedChecks.length ? 1 : 0;
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
    if (server.exitCode !== null) throw new Error(`serve_preview.py exited with ${server.exitCode}`);
    if (fs.existsSync(file)) {
      const info = JSON.parse(fs.readFileSync(file, "utf8"));
      const up = await fetch(`${info.origin}/healthz`).then((response) => response.ok, () => false);
      if (up) return info;
    }
    await sleep(200);
  }
  throw new Error("serve_preview.py did not come up within 60 s");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
