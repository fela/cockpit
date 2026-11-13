// Snoozed steps sit in Waiting with a clock (SPEC VIEW-32). On the page with the list at its left, at laptop and phone
// width, checks:
// - the list has no Snoozed group;
// - a step snoozed for three hours shows as its item's row in Waiting, with a clock and "2h" (the time until it wakes),
//   and its hover says when it wakes;
// - a click on the row shows the snoozed step on the card;
// - after Unsnooze the row leaves Waiting.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
// Screenshots go to the folder given as the first argument, when one is given.
//
// Usage, from the checkout: node tests/browser/snoozed_waiting.cjs [screenshot folder]

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const ROOT = path.resolve(__dirname, "..", "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
const SESSION_B = "22222222-2222-4222-8222-222222222222";
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "snoozed-waiting-test", "content-type": "application/json" };

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const shots = process.argv[2] || null;
  if (shots) fs.mkdirSync(shots, { recursive: true });
  const work = workFolder("cockpit-snoozed-waiting-");
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
    browser = await chromium.launch({ executablePath: CHROMIUM });

    for (const [width, scheme] of [[1280, "dark"], [390, "light"]]) {
      const key = `note:snoozed-waiting-${width}-${Date.now()}`;
      const title = `Snoozed in waiting ${width}`;
      await agent("PUT", "/api/items", { key, title });
      const answer = await agent("POST", "/api/steps", {
        item_key: key, need: "review:snoozed-waiting", ask: "Snoozed in Waiting test", session_id: SESSION_B,
        body: "The whole description of the snoozed step.",
      });
      const id = answer.step.id;
      const until = new Date(Date.now() + 3 * 3600e3 - 60e3).toISOString().replace(/\.\d{3}Z$/, "Z");
      await user("POST", `/api/steps/${id}/snooze`, { until });

      const context = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: scheme, isMobile: width < 600, hasTouch: width < 600 });
      await context.setExtraHTTPHeaders({ cookie: cookie.cookie });
      const page = await context.newPage();
      await page.addLocatorHandler(page.locator("dialog#idle[open]"), (dialog) => dialog.locator("button", { hasText: "Return" }).click()
        .catch((error) => { if (!/has been closed/.test(error.message)) throw error; }));
      page.setDefaultTimeout(30000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(`${info.origin}/`, { timeout: 60000 });
      await page.locator("#sidebar details.sidebar-group").first().waitFor({ state: "attached", timeout: 60000 });
      // A phone opens on the item screen; the list is the screen behind its bar's key.
      if (width === 390) await page.locator(".phone-back").click();
      const waiting = page.locator("#sidebar details.sidebar-group.waiting");
      await waiting.waitFor({ state: "attached" });
      const snoozedGroup = await page.locator("#sidebar details.sidebar-group.snoozed").count();
      check(`${width}: no Snoozed group in the list`, snoozedGroup === 0, `${snoozedGroup} found`);
      if (!(await waiting.evaluate((el) => el.open))) await waiting.locator(":scope > summary").click();
      const row = waiting.locator(`li[data-step="${id}"]`);
      check(`${width}: the snoozed step's row is in Waiting`, (await row.count()) === 1, `${await row.count()} rows`);
      const seen = await row.evaluate((el) => ({
        title: el.querySelector(".sidebar-title").textContent,
        clock: Boolean(el.querySelector(".sidebar-snoozed .icon")),
        time: el.querySelector(".sidebar-age").textContent,
        hover: el.querySelector(".side-row").title || el.querySelector(".side-row").dataset.tip || "",
        label: el.querySelector(".sidebar-snoozed").getAttribute("aria-label"),
      }));
      check(`${width}: the row reads the item's title`, seen.title === title, seen.title);
      check(`${width}: the row has a clock`, seen.clock, JSON.stringify(seen));
      check(`${width}: the row's time is how long until it wakes`, seen.time === "2h", seen.time);
      check(`${width}: the clock says when it wakes`, /^Snoozed until /.test(seen.label), seen.label);
      check(`${width}: the row's hover says when it wakes`, seen.hover.includes("Snoozed until "), seen.hover);
      if (shots) await waiting.screenshot({ path: path.join(shots, `waiting-${width}.png`) });

      await row.locator(".side-row").click();
      const card = page.locator(`#next [data-step="${id}"]`).first();
      await card.waitFor({ state: "visible" });
      const shown = await card.evaluate((el) => el.textContent.includes("The whole description of the snoozed step."));
      check(`${width}: a click shows the snoozed step on the card`, shown, "no content");
      if (shots) await page.screenshot({ path: path.join(shots, `card-${width}.png`) });

      await user("POST", `/api/steps/${id}/snooze`, { until: null });
      let gone = false;
      for (let n = 0; n < 50 && !gone; n += 1) {
        await sleep(200);
        gone = (await page.locator(`#sidebar details.sidebar-group.waiting li[data-step="${id}"]`).count()) === 0;
      }
      check(`${width}: after Unsnooze the row leaves Waiting`, gone, "still in Waiting");
      check(`${width}: no page errors`, errors.length === 0, errors.join(" | "));
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
