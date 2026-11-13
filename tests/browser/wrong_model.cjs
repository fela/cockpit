// The wrong-model banners on the main page (SPEC SESSION-20), at 1280 px in the light look and 390 px in the dark:
// one red banner per session and model, naming the session, the model it ran on, the one it was started on, how many
// calls and the last one's time; a session still on the other model says how to move it back, one back on the main
// model says so; a long first prompt is cut; nothing is wider than the window. On the main page each is a row in Your
// queue (SPEC UI-52), the rail still starts at the top, and the close on an opened one drops it there and on later
// polls. Each offers Investigate before its close; pressing it names the session and model, drops the warning and
// says in a toast that a new session is investigating.
//
// Serves this checkout with serve_preview.py and adds two rows to each /api/state answer, so the check needs no
// transcript on another model. Prints one line per check, saves each width's banners as wrong-model-<width>.png in
// the work folder, and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/wrong_model.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder, fullState } = require("./work.cjs");

// Browser dependencies can be selected through the environment (README.md).
const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;
const ROOT = path.resolve(__dirname, "..", "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const LAST = new Date(Date.now() - 20 * 60e3).toISOString();
const ROWS = [
  {
    session_id: "33333333-3333-4333-8333-333333333333", name: "Reminder fix", model: "claude-sonnet-5-5", intended: "claude-opus-5-5",
    first: new Date(Date.now() - 40 * 60e3).toISOString(), last: LAST, calls: 10, back: false,
  },
  {
    session_id: "44444444-4444-4444-8444-444444444444",
    name: "Plan Tasks exports for completed lists, recurring reminders, shared lists and CSV files",
    model: "claude-haiku-5-5", intended: "claude-opus-5-5", first: LAST, last: LAST, calls: 1, back: true,
  },
];

let failures = 0;
function check(ok, words) {
  console.log(`${ok ? "ok  " : "FAIL"} ${words}`);
  if (!ok) failures += 1;
}

async function main() {
  const work = workFolder("cockpit-wrong-model-");
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser;
  try {
    browser = await chromium.launch({ executablePath: CHROMIUM });
    const info = await harnessUp(work, server);
    for (const [width, scheme] of [[1280, "light"], [390, "dark"]]) {
      const context = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: scheme });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      // The closes the page sends reach the server; the rows it closed leave the later answers, as the server's do.
      const closed = [];
      await context.route("**/api/wrong-models/dismiss", async (route) => {
        closed.push(route.request().postDataJSON());
        await route.continue();
      });
      // Investigate answers with the to-do it added, as the server does, and closes the warning there too.
      const investigated = [];
      await context.route("**/api/wrong-models/investigate", async (route) => {
        const body = route.request().postDataJSON();
        investigated.push(body);
        closed.push(body);
        await route.fulfill({ json: { id: 999999, key: "todo-999999", summary: "Investigate why" } });
      });
      await context.route("**/api/state", async (route) => {
        // A poll still on its way when the context closes has nothing to answer.
        const full = await fullState(route).catch(() => null);
        if (!full) return;
        const state = await full.response.json();
        const rows = ROWS.filter((row) => !closed.some((one) => one.session_id === row.session_id && one.model === row.model));
        // The fixture can hold a question about time away, whose dialog would cover the page.
        await route.fulfill({ ...full, json: { ...state, wrong_models: rows, away: null } }).catch(() => {});
      });
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", (error) => errors.push(String(error)));
      await page.goto(`${info.origin}/classic`, { waitUntil: "load", timeout: 120e3 });
      const at = `${width} px ${scheme}`;
      await page.waitForSelector(".warn-card.fail .warn-text", { timeout: 15e3 });
      const words = await page.locator("#banner .warn-card.fail .warn-text").allTextContents();
      const mine = words.filter((text) => text.includes("instead of claude-opus-5-5"));
      check(mine.length === 2, `${at}: a banner per session and model`);
      check(mine[0].startsWith("Session “Reminder fix” ran on claude-sonnet-5-5 instead of claude-opus-5-5: 10 calls, the last at"),
        `${at}: the first names the session, both models and the calls`);
      check(mine[0].includes("/model claude-opus-5-5 in that session moves it back."), `${at}: one still off says how to move it back`);
      check(mine[1].includes("1 call,") && mine[1].includes("It is back on claude-opus-5-5."), `${at}: one back says so`);
      check(mine[1].includes("…") && !mine[1].includes("CSV files"), `${at}: a long name is cut`);
      const wide = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      check(wide <= 0, `${at}: nothing wider than the window (${wide} px over)`);
      const investigates = await page.locator("#banner .warn-card.fail button", { hasText: "Investigate" }).count();
      check(investigates === 2, `${at}: each banner has an Investigate key (${investigates})`);
      await page.evaluate(() => { ui.pendingRender = true; return refresh(); });
      await sleep(300);
      check((await page.locator("#banner .warn-text", { hasText: "instead of claude-opus-5-5" }).count()) === 2, `${at}: a redraw keeps them`);
      check(errors.length === 0, `${at}: no page errors ${errors.join("; ")}`);
      await page.locator("#banner").screenshot({ path: path.join(work, `wrong-model-${width}.png`) });

      // The main page: a row per warning in Inbox, and the rail starts at the top of the page.
      await page.goto(`${info.origin}/`, { waitUntil: "load", timeout: 120e3 });
      const ROW = "#sidebar li.warn-row[data-warning^='wrong-model:']";
      await page.waitForFunction((row) => document.querySelectorAll(row).length === 2, ROW, { timeout: 60e3 });
      check((await page.locator("#banner .warn-card.fail").count()) === 0, `${at}: main page: no banner above the step`);
      const railTop = await page.evaluate(() => document.querySelector(".rail").getBoundingClientRect().top);
      check(railTop === 0, `${at}: main page: the rail starts at the top (${railTop} px)`);
      await page.evaluate((key) => openWarning(key), `wrong-model:${ROWS[0].session_id}:${ROWS[0].model}`);
      const answers = await page.locator("#next > .warn-step .answers button").allTextContents();
      check(answers.length === 2 && answers[0].includes("Investigate") && answers[1].includes("Close this warning"),
        `${at}: main page: the opened warning offers Investigate, then Close ${JSON.stringify(answers)}`);
      await page.screenshot({ path: path.join(work, `wrong-model-investigate-${width}.png`) });
      await page.locator("#next > .warn-step .answers button", { hasText: "Close this warning" }).click();
      await page.waitForFunction((row) => document.querySelectorAll(row).length === 1, ROW, { timeout: 15e3 });
      check(closed.length === 1 && closed[0].session_id === ROWS[0].session_id && closed[0].model === ROWS[0].model,
        `${at}: the close names the session and model ${JSON.stringify(closed)}`);
      await page.evaluate(() => { ui.pendingRender = true; return refresh(); });
      await sleep(300);
      const left = await page.evaluate((row) => [...document.querySelectorAll(row)].map((one) => one.dataset.warning), ROW);
      check(left.length === 1 && left[0] === `wrong-model:${ROWS[1].session_id}:${ROWS[1].model}`,
        `${at}: a poll after the close keeps the other warning only ${JSON.stringify(left)}`);
      await page.screenshot({ path: path.join(work, `wrong-model-main-${width}.png`) });
      // Investigate on the other one names its session and model, drops the warning and says a session started.
      await page.evaluate((key) => openWarning(key), `wrong-model:${ROWS[1].session_id}:${ROWS[1].model}`);
      await page.locator("#next > .warn-step .answers button", { hasText: "Investigate" }).click();
      await page.waitForFunction((row) => document.querySelectorAll(row).length === 0, ROW, { timeout: 15e3 });
      check(investigated.length === 1 && investigated[0].session_id === ROWS[1].session_id
        && investigated[0].model === ROWS[1].model, `${at}: Investigate names the session and model ${JSON.stringify(investigated)}`);
      const said = await page.locator("#toast").textContent();
      check(said.includes("A new session is investigating") && said.includes("Open"), `${at}: the toast says so, with Open (${said})`);
      await page.screenshot({ path: path.join(work, `wrong-model-investigated-${width}.png`) });
      check(errors.length === 0, `${at}: main page: no page errors ${errors.join("; ")}`);
      // A state poll still on its way would throw once the context is gone.
      await context.unrouteAll({ behavior: "ignoreErrors" });
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
  console.error(`${failures} failed; work dir ${work}`);
  process.exitCode = failures ? 1 : 0;
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
