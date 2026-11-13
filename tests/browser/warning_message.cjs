// A message on a warning in Inbox (SPEC UI-82), at 1280 px in the light look and 390 px in the dark: the opened
// "Codex ~/.codex is signed out" warning shows a message box under its Sign in button, with Send off until it has words;
// Ctrl+Enter sends the words with the warning's title and explanation attached, the box empties, a toast says a new
// session has the message, with Open, and the warning leaves Inbox while the to-do is open, also after a poll; once the
// to-do closes it is back. A refused send keeps the warning in Inbox and puts the words back.
//
// Serves this checkout with serve_preview.py and adds a signed-out Codex account to each /api/state answer. Prints one
// line per check, saves each width's card as warning-message-<width>.png in the work folder, and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/warning_message.cjs

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

const SIGNED_OUT = {
  folder: ".codex", email: "sam@example.com", held: "sam@example.com", problem: "signed_out",
  signing_in: null, doubled: false, error: null, finished: null,
};
const KEY = "codex-sign-in:.codex";
const CARD = `#next > article[data-warning="${KEY}"]`;

let failures = 0;
function check(ok, words) {
  console.log(`${ok ? "ok  " : "FAIL"} ${words}`);
  if (!ok) failures += 1;
}

async function main() {
  const work = workFolder("cockpit-warning-message-");
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser;
  try {
    browser = await chromium.launch({ executablePath: CHROMIUM });
    const info = await harnessUp(work, server);
    for (const [width, scheme] of [[1280, "light"], [390, "dark"]]) {
      const context = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: scheme });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      // The first send is refused, the second answers with the to-do it added and the warning handed to it, as the
      // server does; each state then has the hand-off until the test closes the to-do.
      const sent = [];
      let handed = {};
      await context.route("**/api/warnings/message", async (route) => {
        sent.push(route.request().postDataJSON());
        if (sent.length === 1) {
          await route.fulfill({ status: 409, json: { detail: "Cockpit refused it" } });
          return;
        }
        handed = { [KEY]: 999999 };
        await route.fulfill({ json: { item: { id: 999999, key: "todo-999999", summary: sent.at(-1).text }, handed_warnings: handed } });
      });
      await context.route("**/api/state", async (route) => {
        // A poll still on its way when the context closes has nothing to answer.
        const full = await fullState(route).catch(() => null);
        if (!full) return;
        const state = await full.response.json();
        // The fixture can hold a question about time away, whose dialog would cover the page.
        await route.fulfill({ ...full, json: { ...state, codex_sign_ins: [SIGNED_OUT], handed_warnings: handed, away: null } }).catch(() => {});
      });
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", (error) => errors.push(String(error)));
      const at = `${width} px ${scheme}`;
      await page.goto(`${info.origin}/`, { waitUntil: "load", timeout: 120e3 });
      const row = page.locator(`#sidebar li.warn-row[data-warning="${KEY}"]`);
      await page.waitForSelector(`#sidebar li.warn-row[data-warning="${KEY}"]`, { state: "attached", timeout: 60e3 });
      await page.evaluate((key) => openWarning(key), KEY);
      await page.waitForSelector(CARD, { timeout: 15e3 });
      const answers = await page.locator(`${CARD} .answers button`).allTextContents();
      check(answers.length === 1 && answers[0].includes("Sign in to ~/.codex"), `${at}: the card keeps its Sign in ${JSON.stringify(answers)}`);
      const area = page.locator(`${CARD} .warn-message textarea`);
      const send = page.locator(`${CARD} .warn-message button.send`);
      check(await area.count() === 1, `${at}: the card has a message box`);
      check(await send.isDisabled(), `${at}: Send is off while the box is empty`);
      const words = "Take the old account out of Cockpit";
      await area.fill(words);
      check(!(await send.isDisabled()), `${at}: Send is on once the box has words`);
      const wide = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      check(wide <= 0, `${at}: nothing wider than the window (${wide} px over)`);
      await shoot(page, CARD, path.join(work, `warning-message-${width}.png`));

      // A refused send keeps the warning in Inbox and puts the words back in its box.
      await area.press("Control+Enter");
      await page.waitForFunction(() => document.querySelector("#toast")?.textContent.includes("Not sent"), null, { timeout: 15e3 });
      check(sent.length === 1, `${at}: Ctrl+Enter sends (${sent.length})`);
      check(await row.count() === 1, `${at}: a refused send keeps the warning in Inbox`);
      await page.evaluate((key) => openWarning(key), KEY);
      await page.waitForFunction(([card, typed]) => document.querySelector(`${card} .warn-message textarea`)?.value.includes(typed),
        [CARD, words], { timeout: 15e3 });
      check(true, `${at}: a refused send puts the words back`);

      // Sent: the words with the warning attached, a toast with Open, and the warning out of Inbox with the card moved on.
      await page.locator(`${CARD} .warn-message button.send`).click();
      await page.waitForFunction(() => document.querySelector("#toast")?.textContent.includes("A new session has your message"), null, { timeout: 15e3 });
      const body = sent.at(-1);
      check(sent.length === 2 && body.text === words && body.key === KEY,`${at}: Send sends the words ${JSON.stringify(body)}`);
      check(body.part.target === "page:next/warning" && body.part.quote.startsWith("Codex ~/.codex (sam@example.com) is signed out")
        && body.part.quote.includes("Codex jobs on this account fail until it signs in."), `${at}: the warning's words go with them`);
      const said = await page.locator("#toast").textContent();
      check(said.includes("Open"), `${at}: the toast offers Open (${said})`);
      // A row or card that leaves fades out first (motion.js).
      check(await gone(page, `#sidebar li.warn-row[data-warning="${KEY}"]`), `${at}: the warning leaves Inbox`);
      check(await gone(page, CARD), `${at}: the card moves on`);
      check(await page.evaluate((key) => ui.drafts.get(`msg:warning:${key}`) || "", KEY) === "", `${at}: the box's words are gone`);
      await page.evaluate(() => { ui.pendingRender = true; return refresh(); });
      await sleep(300);
      check(await row.count() === 0, `${at}: the warning stays out of Inbox after a poll`);
      await page.screenshot({ path: path.join(work, `warning-message-sent-${width}.png`) });

      // The to-do closes: the warning still holds, so it is back.
      handed = {};
      await page.evaluate(() => { ui.pendingRender = true; return refresh(); });
      await page.waitForSelector(`#sidebar li.warn-row[data-warning="${KEY}"]`, { state: "attached", timeout: 15e3 });
      check(true, `${at}: the warning is back once the to-do closes`);
      check(errors.length === 0, `${at}: no page errors ${errors.join("; ")}`);
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

// A picture of the element `selector` matches. A poll can draw the card again while the picture is taken, so a
// detached element is tried once more.
async function shoot(page, selector, file) {
  try {
    await page.locator(selector).screenshot({ path: file });
  } catch (error) {
    if (!String(error).includes("not attached")) throw error;
    await page.locator(selector).screenshot({ path: file });
  }
}

// Whether nothing matches `selector` within 5 s.
async function gone(page, selector) {
  return page.waitForFunction((found) => !document.querySelector(found), selector, { timeout: 5e3 }).then(() => true, () => false);
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
