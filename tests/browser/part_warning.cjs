// A warning an install's part gives (the state's `part_warnings`, parts.py) as an Inbox warning (SPEC UI-52), at
// 1280 px in the light look and 390 px in the dark: a row in Inbox with the part's words, which opens a card saying
// what the part said more, whose one answer opens the part's page; nothing is wider than the window. On a phone the
// row is pressed on the list screen.
//
// Serves this checkout with serve_preview.py, which has no parts, and adds one part's warning to each /api/state
// answer. Prints one line per check, saves each width's Inbox as part-warning-<width>.png and its opened card as
// part-warning-card-<width>.png in the work folder, and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/part_warning.cjs

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

const KEY = "a-part:account";
const TEXT = "The part's account is signed out";
const MORE = "The part does nothing until its account is signed in again.";
const WARNING = { key: KEY, tone: "fail", text: TEXT, more: MORE, fix: { words: "Open the part's page", href: "/a-part-page" } };
const ROW = `#sidebar li.warn-row[data-warning='${KEY}']`;

let failures = 0;
function check(ok, words) {
  console.log(`${ok ? "ok  " : "FAIL"} ${words}`);
  if (!ok) failures += 1;
}

async function main() {
  const work = workFolder("cockpit-part-warning-");
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser;
  try {
    browser = await chromium.launch({ executablePath: CHROMIUM });
    const info = await harnessUp(work, server);
    for (const [width, scheme] of [[1280, "light"], [390, "dark"]]) {
      const context = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: scheme });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      await context.route("**/api/state", async (route) => {
        // A poll still on its way when the context closes has nothing to answer.
        const response = await route.fetch().catch(() => null);
        if (!response) return;
        const state = await response.json();
        // The fixture can hold a question about time away, whose dialog would cover the page.
        await route.fulfill({ response, json: { ...state, part_warnings: [WARNING], away: null } }).catch(() => {});
      });
      // No part serves a page here; the press only has to land on it.
      await context.route("**/a-part-page", (route) => route.fulfill({ contentType: "text/html", body: "<title>A part's page</title>" }));
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", (error) => errors.push(String(error)));
      await page.goto(`${info.origin}/`, { waitUntil: "load", timeout: 120e3 });
      const at = `${width} px ${scheme}`;
      await page.waitForSelector(ROW, { state: "attached", timeout: 60e3 });
      // On a phone the item and the list are two screens (UI-56): the item screen's bar opens the list.
      if (await page.locator(".phone-back").isVisible()) await page.locator(".phone-back").click();
      await page.waitForSelector(ROW, { timeout: 15e3 });
      const row = await page.locator(`${ROW} .sidebar-title`).textContent();
      check(row === TEXT, `${at}: Inbox has the warning in the part's words (${row})`);
      const wide = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      check(wide <= 0, `${at}: nothing wider than the window (${wide} px over)`);
      await sleep(500);
      await page.screenshot({ path: path.join(work, `part-warning-${width}.png`) });

      await page.locator(`${ROW} .side-row`).click();
      const card = `#next > .warn-step[data-warning='${KEY}']`;
      await page.waitForSelector(card, { timeout: 15e3 });
      const why = await page.locator(`${card} .why`).textContent();
      check(why === MORE, `${at}: the card says what the part said more (${why})`);
      const answers = await page.locator(`${card} .answers button`).allTextContents();
      check(answers.length === 1 && answers[0] === WARNING.fix.words, `${at}: its one answer opens the part's page ${JSON.stringify(answers)}`);
      const wideCard = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      check(wideCard <= 0, `${at}: the opened card is no wider than the window (${wideCard} px over)`);
      check(errors.length === 0, `${at}: no page errors ${errors.join("; ")}`);
      await sleep(500);
      await page.screenshot({ path: path.join(work, `part-warning-card-${width}.png`) });
      await page.locator(`${card} .answers button`, { hasText: WARNING.fix.words }).click();
      await page.waitForURL(`${info.origin}/a-part-page`, { timeout: 15e3 }).catch(() => {});
      check(page.url() === `${info.origin}/a-part-page`, `${at}: the press lands on the part's page (${page.url()})`);
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
