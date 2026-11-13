// A message to a session waits for a hand-off to that session still on its way (regression check), and Done sends the
// session the page showed as the step's target (regression check). Puts three steps for the harness's second fake session,
// then checks:
// - with the first step's hand-off held back before its request (its page choices still saving) and a second
//   step's hand-off to the same session going at once, a message sent to that session meanwhile goes only after
//   Cockpit answered both hand-offs;
// - Done on the second step sends `session_id`, the session its card showed;
// - no page errors.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs)
// and drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/handoff_order.cjs

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
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "handoff-order-test", "content-type": "application/json" };

const { chromium } = require(PLAYWRIGHT);

async function main() {
  const work = workFolder("cockpit-handoff-order-");
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
    // One task shows one card at a time, so each step has its own item; all three belong to the same session.
    const stamp = Date.now();
    const keys = { first: `note:handoff-order-first-${stamp}`, second: `note:handoff-order-second-${stamp}`, other: `note:handoff-order-other-${stamp}` };
    for (const [name, key] of Object.entries(keys)) {
      await agent("PUT", "/api/items", { key, title: `Hand-off order ${name}`, priority: "urgent", sessions: [SESSION_B] });
    }
    const { step: first } = await agent("POST", "/api/steps", { item_key: keys.first, need: "decide:first", ask: "first question?",
      body: "Hand-off order check.", score: 60, session_id: SESSION_B });
    const { step: second } = await agent("POST", "/api/steps", { item_key: keys.second, need: "decide:second", ask: "second question?",
      body: "Done check.", score: 50, session_id: SESSION_B });
    const { step: other } = await agent("POST", "/api/steps", { item_key: keys.other, need: "decide:other", ask: "other question?",
      body: "Second hand-off.", score: 40, session_id: SESSION_B });

    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
    const page = await context.newPage();
    page.setDefaultTimeout(90000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const events = [];
    page.on("request", (request) => {
      const where = new URL(request.url()).pathname;
      if (request.method() === "POST") events.push({ what: "request", where, body: request.postDataJSON() });
    });
    page.on("response", (response) => {
      const where = new URL(response.url()).pathname;
      if (response.request().method() === "POST") events.push({ what: "answer", where });
    });

    await page.goto(`${info.origin}/`);
    await page.waitForFunction((ids) => typeof ui !== "undefined" && ui.state
      && ids.every((id) => [...ui.state.queue, ...ui.state.ready_next].some((one) => one.id === id)), [first.id, second.id, other.id]);

    // The first step's page choices still save: its hand-off request waits for them. The other step's hand-off to
    // the same session goes at once, and Cockpit answers it before the message is sent.
    await page.evaluate((firstId) => {
      window.releaseChoices = null;
      const held = new Promise((resolve) => { window.releaseChoices = resolve; });
      window.choicesSaved = (step) => (step.id === firstId ? held : Promise.resolve());
    }, first.id);
    await page.evaluate(([id, otherId, session]) => {
      const steps = [...ui.state.queue, ...ui.state.ready_next];
      handOffStep(steps.find((one) => one.id === id), "Hand-off words (order check)", { session_id: session }, [], "the test session", null, null, null);
      handOffStep(steps.find((one) => one.id === otherId), "Other hand-off (order check)", { session_id: session }, [], "the test session", null, null, null);
    }, [first.id, other.id, SESSION_B]);
    const otherEnd = Date.now() + 20000;
    while (Date.now() < otherEnd && !events.some((one) => one.what === "answer" && one.where === `/api/steps/${other.id}/hand-off`)) await page.waitForTimeout(200);
    await page.evaluate((session) => {
      ui.drafts.set("order-check", "Follow-up words (order check)");
      sendMessage("order-check", {}, { session_id: session }, "the test session", null);
    }, SESSION_B);
    await page.waitForTimeout(1500);
    const early = events.filter((one) => one.what === "request" && one.where === "/api/messages").length;
    check("the message waits while the hand-off waits for its choices", early === 0, JSON.stringify(events));
    await page.evaluate(() => window.releaseChoices());
    const end = Date.now() + 20000;
    while (Date.now() < end && !events.some((one) => one.what === "request" && one.where === "/api/messages")) await page.waitForTimeout(200);
    const handAnswer = events.findIndex((one) => one.what === "answer" && one.where === `/api/steps/${first.id}/hand-off`);
    const message = events.findIndex((one) => one.what === "request" && one.where === "/api/messages");
    const otherAnswer = events.findIndex((one) => one.what === "answer" && one.where === `/api/steps/${other.id}/hand-off`);
    check("the message goes after Cockpit answered both hand-offs", handAnswer >= 0 && otherAnswer >= 0 && message > handAnswer && message > otherAnswer, JSON.stringify(events));

    // Done on the second step sends the session its card showed.
    await page.evaluate((id) => {
      const step = [...ui.state.queue, ...ui.state.ready_next].find((one) => one.id === id);
      act(step, "done");
    }, second.id);
    const doneEnd = Date.now() + 20000;
    let done = null;
    while (Date.now() < doneEnd && !done) {
      done = events.find((one) => one.what === "request" && one.where === `/api/steps/${second.id}/done`) || null;
      if (!done) await page.waitForTimeout(200);
    }
    check("Done sends the session the card showed", done && done.body && done.body.session_id === SESSION_B, JSON.stringify(done));
    await page.waitForTimeout(2000);
    const state = await agent("GET", `/api/items?key=${encodeURIComponent(keys.second)}`).catch(() => null);
    const closed = state && (state.steps || []).find((one) => one.id === second.id);
    check("Cockpit took the Done", Boolean(closed) && closed.state !== "open", JSON.stringify(closed && closed.state));
    check("no page errors", errors.length === 0, errors.join(" | "));
    await context.close();
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
