// Send from a session's conversation hands the step off (SPEC CONTROL-5). Puts an item whose step goes to the harness's
// second fake session, opens that session's view from the page, writes in its box and presses Send, then checks:
// - the box shows the step's reply hint, so it reads as the step's reply;
// - Send hands the step off: the server holds it under You handed these, with the message as its hand-off;
// - the step leaves the user's queue, and the box redraws as a plain message box;
// - Take back puts the words back in the conversation's box, which answers the step again;
// - with a second step for the same session scored higher, the box keeps answering the step it opened on;
// - no page errors.
//
// Starts serve_preview.py (this checkout's Cockpit on a copy of the live database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/session_view_hands_off.cjs

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
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "hand-off-test", "content-type": "application/json" };
const HINT = "Choose daily or weekly reminders";
const TEXT = "Sent from the conversation (hand-off check)";

const { chromium } = require(PLAYWRIGHT);

async function main() {
  const work = workFolder("cockpit-session-hand-off-");
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

    for (const width of [1280, 390]) {
      const key = `note:session-hand-off-${width}-${Date.now()}`;
      const item = await agent("PUT", "/api/items", { key, title: "Hand-off test", priority: "urgent", sessions: [SESSION_B] });
      const { step } = await agent("POST", "/api/steps", { item_key: key, need: "decide:reminder", ask: "How often should Tasks remind Sam?",
        body: "The grocery list can have a daily or weekly reminder.", reply_hint: HINT, score: 60, session_id: SESSION_B });
      const context = await browser.newContext({ viewport: { width, height: 900 }, isMobile: width < 600, hasTouch: width < 600 });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      page.setDefaultTimeout(90000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      const posts = [];
      page.on("request", (request) => { if (request.method() === "POST") posts.push(new URL(request.url()).pathname); });

      await page.goto(`${info.origin}/`);
      // The page holds the item: the list may draw it under a folded group, and the view opens by call below.
      await page.waitForFunction((id) => typeof ui !== "undefined" && ui.state && ui.state.items.some((one) => one.id === id), item.id);
      await page.evaluate(([id, itemId]) => window.openSessionView(id, itemId), [SESSION_B, item.id]);
      const area = page.locator("#session-composer textarea");
      await area.waitFor();
      const hint = await area.getAttribute("placeholder");
      check(`${width}: the box shows the step's reply hint`, hint === HINT, hint);
      await area.fill(TEXT);
      await page.locator("#session-composer button.send").click();
      const end = Date.now() + 20000;
      let held = null;
      while (Date.now() < end) {
        const state = await agent("GET", "/api/state");
        held = state.with_agents.find((one) => one.id === step.id) || null;
        if (held) break;
        await new Promise((resolve) => setTimeout(resolve, 300));
      }
      check(`${width}: Send went to the step's hand-off`, posts.includes(`/api/steps/${step.id}/hand-off`), posts.join(", "));
      check(`${width}: the step is under You handed these`, Boolean(held), "not in with_agents");
      check(`${width}: the hand-off carries the message`, held && JSON.stringify(held.handoff || "").includes(TEXT), held && JSON.stringify(held.handoff));
      const state = await agent("GET", "/api/state");
      check(`${width}: the step left the queue`, !state.queue.some((one) => one.id === step.id), "still queued");
      await page.waitForFunction(() => {
        const box = document.querySelector("#session-composer textarea");
        return box && box.placeholder === "Message…";
      }, null, { timeout: 15000 }).catch(() => {});
      const after = await area.getAttribute("placeholder").catch(() => null);
      check(`${width}: the box then reads as a plain message box`, after === "Message…", after);
      await page.screenshot({ path: path.join(work, `session-hand-off-${width}.png`) });

      // Take back while the message still waits: its words come back into the conversation's box, which answers the
      // step again.
      await page.evaluate((id) => act(drawn(ui.state).with_agents.find((one) => one.id === id), "return"), step.id);
      await page.waitForFunction((text) => {
        const box = document.querySelector("#session-composer textarea");
        return box && box.value.includes(text);
      }, TEXT, { timeout: 15000 }).catch(() => {});
      const back = await page.evaluate(() => {
        const box = document.querySelector("#session-composer textarea");
        return box ? { value: box.value, hint: box.placeholder } : null;
      });
      check(`${width}: Take back puts the words back in the conversation's box`, back && back.value.includes(TEXT), JSON.stringify(back));
      check(`${width}: the box answers the step again`, back && back.hint === HINT, JSON.stringify(back));
      await page.evaluate(() => { const box = document.querySelector("#session-composer textarea"); box.value = ""; box.dispatchEvent(new Event("input", { bubbles: true })); });

      // A second step of the item for the same session, scored above the first (100 against 60): the box keeps answering the step the
      // card showed when the conversation opened.
      await agent("POST", "/api/steps", { item_key: key, need: "decide:other", ask: "the other question?", body: "Another step.",
        reply_hint: "Other step's hint", score: 100, session_id: SESSION_B });
      await page.evaluate(() => refresh());
      await page.waitForTimeout(3000);
      const kept = await page.locator("#session-composer textarea").getAttribute("placeholder");
      check(`${width}: with two steps the box keeps answering the one it opened on`, kept === HINT, kept);
      check(`${width}: no page errors`, errors.length === 0, errors.join(" | "));
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
