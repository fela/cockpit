// The step card's head and fold. Checks, on the item's one step, which the user handed off and its session
// rewrote and gave back:
// - the heading is the task's name, with the ask under it as a short line: the part before the semicolon in a
//   lighter colour than the next decision after it;
// - above the content, the story's one-sentence summary and the user's last message, cut after its first words
//   with a "N more words" button that opens the rest;
// - the rest of the history sits in a closed fold that opens on a click;
// - the reply box's placeholder is the step's reply hint;
// - Task done stands among the answer buttons;
// - at phone width nothing scrolls sideways, and the page logs no errors.
//
// Starts serve_preview.py (this checkout's Cockpit on a fixture database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed. Screenshots go to the
// folder given as the first argument, when one is given.
//
// Usage, from the checkout: node tests/browser/step_head.cjs [screenshot folder]

"use strict";

const { spawn, spawnSync } = require("node:child_process");
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
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "step-head-test", "content-type": "application/json" };
const TITLE = "Step head test";
const ASK = "weekly reminders added; check the sample list";
const HINT = "Say which reminder time to use";
const SUMMARY = "The user asked for weekly reminders in Tasks, and the session added a reminder selector.";
// Longer than the 24 words the card shows of it.
const MESSAGE = "Please let me set a weekly reminder on the grocery list and show the next reminder date beside its title, so I can check it before sharing the list with Ana.";
// Runs one SQL statement on the fixture database; `:now` is now, to the second.
const RUN_SQL = `
import sqlite3, sys
from datetime import datetime, timezone
now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
conn = sqlite3.connect(sys.argv[1])
conn.execute(sys.argv[2], {"now": now, "a": sys.argv[3], "b": sys.argv[4]})
conn.commit()
`;

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const shots = process.argv[2] || null;
  if (shots) fs.mkdirSync(shots, { recursive: true });
  const work = workFolder("cockpit-step-head-");
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

    // The fixture database's open steps are snoozed and its timer paused, so this run's step is the one on the page.
    const copied = await user("GET", "/api/state");
    if (copied.timer) await user("POST", `/api/steps/${copied.timer.step_id}/timer/pause`);
    await Promise.all([...copied.queue, ...copied.ready_next].map((step) => user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));

    const key = `note:step-head-${Date.now()}`;
    const item = await agent("PUT", "/api/items", { key, title: TITLE, priority: "urgent", sessions: [SESSION_B] });
    const step = { item_key: key, need: "decide:step-head", session_id: SESSION_B, score: 100, ask: "first build; check it" };
    const put = await agent("POST", "/api/steps", { ...step, body: "The first build." });
    const id = put.step.id;
    // Two exchanges, so the story has something before the last to tell: each time the user hands the step off,
    // and the session rewrites it in its own words and gives it back.
    for (const [text, rewrite] of [
      ["How would I choose a weekly reminder?", { ask: "showed three reminder times; pick one", body: "Choose a morning, afternoon or evening reminder." }],
      [MESSAGE, { ask: ASK, reply_hint: HINT, body: "The grocery list now shows its next reminder date." }],
    ]) {
      const handed = await user("POST", `/api/steps/${id}/hand-off`, { text });
      // The history counts the message once it went to the session; the harness's fake sessions never confirm one.
      const sent = sql(work, "UPDATE messages SET state = 'sent', sent_at = :now, take_back_until = NULL WHERE id = :a", String(handed.id), "");
      check("the hand-off message is marked sent", sent.status === 0, sent.stderr);
      await agent("POST", "/api/steps", { ...step, ...rewrite });
      await agent("POST", `/api/steps/${id}/return`, {});
    }
    // A story the summary model would write, newer than any other on the item.
    const story = `Summary: ${SUMMARY}\n2026-01-15 10:00: The user asked for weekly reminders in Tasks.`;
    const stored = sql(work, "INSERT INTO summaries (ref, at, text, summary, asked_at, made_at) VALUES (:a, '2099-01-01T00:00:00Z', '', :b, :now, :now)", `story:${item.id}:test`, story);
    check("the story went into the fixture database", stored.status === 0, stored.stderr);
    await user("POST", `/api/steps/${id}/timer/start`);

    browser = await chromium.launch({ executablePath: CHROMIUM });
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
    const page = await context.newPage();
    page.setDefaultTimeout(10000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
    await page.goto(`${info.origin}/classic`);
    const card = page.locator(`[data-step="${id}"]`).first();
    await card.locator(".hist-fold-card .fold-summary").waitFor();

    const heading = (await card.locator("h1.task-heading .task-heading-text").innerText()).trim();
    check("the heading is the task's name", heading === TITLE, heading);
    const done = (await card.locator(".short-line .short-done").innerText()).trim();
    const next = (await card.locator(".short-line .short-next").innerText()).trim();
    check("the short line splits at its semicolon", done.startsWith("weekly reminders added") && next.includes("check the sample list"), `${done} | ${next}`);
    const colours = await card.locator(".short-line").evaluate((line) => [".short-done", ".short-next"].map((part) => getComputedStyle(line.querySelector(part)).color));
    check("what happened is lighter than the next decision", colours[0] !== colours[1], colours.join(" vs "));

    const summary = (await card.locator(".fold-summary").innerText()).trim();
    check("the story's summary sentence shows above the content", summary === SUMMARY, summary);
    const said = card.locator(".fold-said");
    const short = await said.innerText();
    check("the user's last message shows cut, with how long ago", short.includes("Please let me set") && !short.includes("with Ana") && /ago|now/.test(short), short);
    const more = said.locator(".fold-said-more");
    check("a button says how many words are left", /\d+ more words/.test(await more.innerText()), await more.innerText());
    await more.click();
    check("the button opens the whole message", (await card.locator(".fold-said").innerText()).includes("with Ana"), await card.locator(".fold-said").innerText());

    const history = card.locator(".fold-all > .history, .fold-all section.history").first();
    check("the rest of the history starts closed", !(await history.isVisible().catch(() => false)), "it shows");
    await card.locator(".fold-all > summary").click();
    await history.waitFor();
    check("the fold opens the history", await history.isVisible(), "still hidden");

    const placeholder = await card.locator("textarea").first().getAttribute("placeholder");
    check("the reply box's placeholder is the reply hint", placeholder === HINT, placeholder);
    check("Task done stands among the answer buttons", await card.locator(".answers .task-done").count() === 1, "not in .answers");
    if (shots) await card.screenshot({ path: path.join(shots, "step-head-1440.png") });

    await page.setViewportSize({ width: 390, height: 900 });
    await sleep(500);
    const sideways = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    check("at 390 px nothing scrolls sideways", sideways <= 0, `${sideways}px wider`);
    if (shots) await card.screenshot({ path: path.join(shots, "step-head-390.png") });

    check("no page errors", errors.length === 0, errors.join(" | "));
    await user("POST", `/api/steps/${id}/timer/pause`);
    await context.close();
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failedChecks = results.filter((result) => !result.ok);
  console.error(failedChecks.length ? `${failedChecks.length} of ${results.length} checks failed` : `all ${results.length} checks passed`);
  process.exitCode = failedChecks.length ? 1 : 0;
}

function sql(work, statement, a, b) {
  return spawnSync(PYTHON, ["-c", RUN_SQL, path.join(work, "cockpit.db"), statement, a, b], { encoding: "utf8" });
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
  throw new Error("the harness did not come up within 60 s");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
