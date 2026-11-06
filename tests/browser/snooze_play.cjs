// Play on a snoozed step wakes it and starts its timer. Checks, at laptop and phone width:
// - a snoozed step's row opens under Snoozed with a play named "Unsnooze and start the timer";
// - the play brings the step back into the queue, starts its timer and shows the content;
// - two seconds later the timer still runs.
//
// Starts serve_preview.py (this checkout's Cockpit on a copy of the live database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
// Screenshots go to the folder given as the first argument, when one is given.
//
// Usage, from the checkout: node tests/browser/snooze_play.cjs [screenshot folder]

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
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "snooze-play-test", "content-type": "application/json" };

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const shots = process.argv[2] || null;
  if (shots) fs.mkdirSync(shots, { recursive: true });
  const work = workFolder("cockpit-snooze-play-");
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

    // The fixture database's open steps are snoozed and its timer paused, so this run's step is the only one to wake.
    const copied = await user("GET", "/api/state");
    if (copied.timer) await user("POST", `/api/steps/${copied.timer.step_id}/timer/pause`);
    await Promise.all([...copied.queue, ...copied.ready_next, ...copied.snoozed].map((step) => user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
    browser = await chromium.launch({ executablePath: CHROMIUM });

    for (const width of [1440, 390]) {
      const key = `note:snooze-play-${Date.now()}`;
      await agent("PUT", "/api/items", { key, title: "Snooze play test", priority: "urgent" });
      const answer = await agent("POST", "/api/steps", {
        item_key: key, need: `review:snooze-play-${width}`, ask: "Snooze play test", score: 100, session_id: SESSION_B,
        body: "The whole description of the snoozed step.",
      });
      const id = answer.step.id;
      await user("POST", `/api/steps/${id}/snooze`, { until: "2099-01-02T08:00:00Z" });

      const context = await browser.newContext({ viewport: { width, height: 900 }, isMobile: width < 600, hasTouch: width < 600 });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      page.setDefaultTimeout(5000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
      await page.goto(`${info.origin}/classic`);
      const group = page.locator("details.snoozed");
      await group.locator("summary").first().click();
      const row = page.locator(`.step-row[data-step="${id}"]`).first();
      await row.locator(".row-head").click();
      // The timer's start is in the step's ⋯ menu (VIEW-29).
      const playButton = row.locator('.step-menu [data-timer="start"]');
      await playButton.waitFor({ state: "attached" });
      const playWords = (await playButton.textContent()).trim();
      check(`${width}: the snoozed row's menu names its start "Unsnooze and start the timer"`,
        playWords === "Unsnooze and start the timer", playWords);
      await row.locator(".step-menu > summary").click();
      if (shots) await row.screenshot({ path: path.join(shots, `snoozed-${width}.png`) });

      await playButton.click();
      // The wake answers first, then the timer starts: wait for both, up to five seconds.
      let state = null;
      for (let n = 0; n < 25; n += 1) {
        await sleep(200);
        state = await user("GET", "/api/state");
        if (state.timer && state.timer.step_id === id) break;
      }
      check(`${width}: the step is back in the queue`, state.queue.some((step) => step.id === id) && !state.snoozed.some((step) => step.id === id),
        JSON.stringify(state.snoozed.map((step) => step.id)));
      check(`${width}: its timer runs`, state.timer && state.timer.step_id === id, JSON.stringify(state.timer));
      const card = page.locator(`[data-step="${id}"]`).first();
      const shown = await card.evaluate((el) => el.textContent.includes("The whole description of the snoozed step."));
      check(`${width}: the content shows`, shown, "no content");
      await sleep(2000);
      const later = await user("GET", "/api/state");
      check(`${width}: two seconds later the timer still runs`, later.timer && later.timer.step_id === id, JSON.stringify(later.timer));
      if (shots) await page.screenshot({ path: path.join(shots, `woken-${width}.png`) });

      check(`${width}: no page errors`, errors.length === 0, errors.join(" | "));
      await user("POST", `/api/steps/${id}/timer/pause`);
      await user("POST", `/api/steps/${id}/snooze`, { until: "2099-01-01T08:00:00Z" });
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
