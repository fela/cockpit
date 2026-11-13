// A session's reply reaches a step card without any other change to the page. Checks, on a step the user handed off and
// its session gave back:
// - an idle page refetches the shown history within 20 s, and an unchanged history does not redraw the card;
// - a reply Cockpit reads meanwhile (a row in session_turns, as the turn reader writes it) shows on the card within 20
//   s, whole, not as a summary;
// - at phone width the whole reply wraps with no sideways scroll.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
// Screenshots go to the folder given as the first argument, when one is given.
//
// Usage, from the checkout: node tests/browser/history_refresh.cjs [screenshot folder]

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
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "history-refresh-test", "content-type": "application/json" };
// Longer than the 140 characters past which a text shows its summary; the answer is at its end.
const REPLY = `${"The Tasks sharing guide describes how Sam can invite Alex to a grocery list. ".repeat(6)}The sharing option is THE-ANSWER.`;
// Runs one SQL statement on the fixture database; `:now` and `:before` are now and two seconds ago, to the second.
// The harness's fake sessions never confirm a delivery, and its server reads no transcripts, so the test writes
// what Cockpit would: the message as sent, and the turn as `history.read_turns` keeps it.
const RUN_SQL = `
import sqlite3, sys
from datetime import datetime, timedelta, timezone
now = datetime.now(timezone.utc)
iso = lambda moment: moment.strftime("%Y-%m-%dT%H:%M:%SZ")
conn = sqlite3.connect(sys.argv[1])
conn.execute(sys.argv[2], {"now": iso(now), "before": iso(now - timedelta(seconds=2)), "a": sys.argv[3], "b": sys.argv[4]})
conn.commit()
`;

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const shots = process.argv[2] || null;
  if (shots) fs.mkdirSync(shots, { recursive: true });
  const work = workFolder("cockpit-history-refresh-");
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
    const initial = await user("GET", "/api/state");
    if (initial.timer) await user("POST", `/api/steps/${initial.timer.step_id}/timer/pause`);
    await Promise.all([...initial.queue, ...initial.ready_next, ...initial.snoozed].map((step) => user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));

    const key = `note:history-refresh-${Date.now()}`;
    await agent("PUT", "/api/items", { key, title: "History refresh test", priority: "urgent", sessions: [SESSION_B] });
    const answer = await agent("POST", "/api/steps", {
      item_key: key, need: "decide:history-refresh", ask: "History refresh test", score: 100, session_id: SESSION_B,
      body: "The step the session gave back.",
    });
    const id = answer.step.id;
    const handed = await user("POST", `/api/steps/${id}/hand-off`, { text: "How can Sam share a grocery list with Alex?" });
    // The history counts the message once it went to the session.
    const sent = sql(work, "UPDATE messages SET state = 'sent', sent_at = :now, take_back_until = NULL WHERE id = :a", String(handed.id), "");
    check("the hand-off message is marked sent", sent.status === 0, sent.stderr);
    // A handed step comes back with an answer on it (NEXT-37); the reply below comes from a later session turn.
    await agent("POST", `/api/steps/${id}/return`, { note: "The Tasks sharing guide describes the invitation." });
    await user("POST", `/api/steps/${id}/timer/start`);

    browser = await chromium.launch({ executablePath: CHROMIUM });
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
    const page = await context.newPage();
    page.setDefaultTimeout(5000);
    page.setDefaultNavigationTimeout(60000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
    const fetches = [];
    page.on("request", (request) => { if (request.url().endsWith(`/history`)) fetches.push(Date.now()); });
    await page.goto(`${info.origin}/classic`);
    const card = page.locator(`[data-step="${id}"]`).first();
    // The history sits folded under the step (NEXT-52); open it.
    await card.locator(".fold-all > summary").click();
    const history = card.locator("section.history");
    await history.waitFor();
    check("the step card shows its history", await history.count() === 1, "no history on the card");
    // Note who asks for each redraw: the fixture database's state changes between polls on its own, so `refresh`
    // redraws whatever this test does; a redraw asked by anything else here came from the history refetch.
    await page.evaluate(() => {
      window.redrawsBy = [];
      const original = window.safeRender;
      window.safeRender = (...args) => {
        window.redrawsBy.push(new Error().stack.split("\n")[2].trim());
        return original(...args);
      };
    });
    const before = fetches.length;
    await sleep(20000);
    check("an idle page refetches the shown history within 20 s", fetches.length > before, `${fetches.length - before} fetches`);
    const others = (await page.evaluate(() => window.redrawsBy)).filter((frame) => !frame.startsWith("at refresh "));
    check("an unchanged history does not redraw the page", others.length === 0, others.join(" | "));

    const added = sql(work, "INSERT INTO session_turns (session_id, started_at, ended_at, reply) VALUES (:a, :before, :now, :b)", SESSION_B, REPLY);
    check("the turn went into the fixture database", added.status === 0, added.stderr);
    const shown = await page.waitForFunction(({ stepId, reply }) => {
      const whole = document.querySelector(`[data-step="${stepId}"] .hist-text.whole`);
      return whole && whole.textContent.trim() === reply;
    }, { stepId: id, reply: REPLY }, { timeout: 20000, polling: 500 }).then(() => true, () => false);
    check("the reply shows on the card within 20 s, whole", shown, await card.locator("section.history").innerText().catch(() => "no history"));
    if (shots) await card.screenshot({ path: path.join(shots, "reply-1440.png") });

    await page.setViewportSize({ width: 390, height: 900 });
    await sleep(500);
    const sideways = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    check("at 390 px the whole reply wraps with no sideways scroll", sideways <= 0, `${sideways}px wider`);
    if (shots) await card.screenshot({ path: path.join(shots, "reply-390.png") });

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
  throw new Error("serve_preview.py did not come up within 60 s");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
