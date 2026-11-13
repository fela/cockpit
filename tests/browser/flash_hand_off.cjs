// A step the user's own send took off the page is no flash, though Cockpit has not heard of the send yet (PERM-24).
// On the classic and the sidebar page, with no press the flash recorder saw:
// - a queued step whose hand-off is held back before its request (its page choices still saving) leaves the page,
//   and the page sends no flash report for it;
// - a queued step an agent withdraws within a minute is still reported;
// - no page errors.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs)
// and drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/flash_hand_off.cjs

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
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "flash-hand-off-test", "content-type": "application/json" };
// flashes.js's PRESS_MS, and a little more.
const PAST_PRESS_MS = 3500;

const { chromium } = require(PLAYWRIGHT);

async function main() {
  const work = workFolder("cockpit-flash-hand-off-");
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const info = await harnessUp(work, server);
    const agent = (method, url, body) => call(info.origin, AGENT, method, url, body);
    browser = await chromium.launch({ executablePath: CHROMIUM });
    for (const address of ["/classic", "/"]) {
      const key = `note:flash-hand-off${address.replace(/\W/g, "-")}-${Date.now()}`;
      const otherKey = `${key}-other`;
      await agent("PUT", "/api/items", { key, title: "Flash hand-off test", priority: "urgent", sessions: [SESSION_B] });
      await agent("PUT", "/api/items", { key: otherKey, title: "Flash withdraw test", priority: "urgent" });
      const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      page.setDefaultTimeout(90000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      const reports = [];
      page.on("request", (request) => {
        if (request.method() === "POST" && new URL(request.url()).pathname === "/api/flashes") reports.push(request.postDataJSON());
      });
      await page.goto(`${info.origin}${address}`);
      await page.waitForFunction(() => typeof ui !== "undefined" && ui.state);
      // Both steps come in while the page is open, so each card shows now, and takes a queue place: the handed one
      // as the card on top, the pulled one in the list below it. (The classic page keeps a top card that left until
      // the user moves on, so a withdrawn top card is no flash there.)
      const { step: handed } = await agent("POST", "/api/steps", { item_key: key, need: "decide:handed", ask: "Handed question?",
        body: "Flash hand-off check.", score: 100, session_id: SESSION_B });
      const { step: pulled } = await agent("POST", "/api/steps", { item_key: otherKey, need: "decide:pulled", ask: "Pulled question?",
        body: "Flash withdraw check.", score: 90 });
      const shown = (id) => page.locator(`[data-step="${id}"]:visible`).first();
      // Gone from view: the page may keep a hidden copy of a list row.
      const gone = (id) => page.waitForFunction((one) => ![...document.querySelectorAll(`[data-step="${one}"]`)]
        .some((el) => el.checkVisibility()), id);
      const about = (id) => reports.filter((one) => one.kind === "step" && Number(one.ref) === id);
      await page.evaluate(() => refresh());
      await shown(handed.id).waitFor();
      await shown(pulled.id).waitFor();
      await page.waitForTimeout(PAST_PRESS_MS);

      // A step an agent takes away is reported: the recorder is on.
      await agent("POST", `/api/steps/${pulled.id}/withdraw`, {});
      await page.evaluate(() => refresh());
      await gone(pulled.id);
      const end = Date.now() + 10000;
      while (Date.now() < end && about(pulled.id).length === 0) await page.waitForTimeout(200);
      check(`${address}: a step an agent withdrew is reported`, about(pulled.id).length === 1, JSON.stringify(reports));

      // The send goes the way a reply box sends it, with no press the recorder saw, and stays on the page's side.
      await page.evaluate((id) => {
        window.releaseChoices = null;
        const held = new Promise((resolve) => { window.releaseChoices = resolve; });
        window.choicesSaved = (step) => (step.id === id ? held : Promise.resolve());
      }, handed.id);
      await page.evaluate(([id, session]) => {
        const step = [...ui.state.queue, ...ui.state.ready_next].find((one) => one.id === id);
        handOffStep(step, "Hand-off words (flash check)", { session_id: session }, [], "the test session", null, null, null);
      }, [handed.id, SESSION_B]);
      await gone(handed.id);
      await page.waitForTimeout(2000);
      check(`${address}: a step the user sent away is not reported while its hand-off waits`, about(handed.id).length === 0,
        JSON.stringify(about(handed.id)));
      await page.evaluate(() => window.releaseChoices());
      await page.waitForTimeout(2000);
      check(`${address}: the sent step is not reported once its hand-off went`, about(handed.id).length === 0,
        JSON.stringify(about(handed.id)));
      check(`${address}: no page errors`, errors.length === 0, errors.join(" | "));
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
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error("serve_preview.py did not come up within 60 s");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
