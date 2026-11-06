// Writing in the step's own reply box keeps its timer running (TRACK-6), and so does working elsewhere on the page.
// Checks:
// - clicking into the reply box of the step on the clock and typing leaves its timer running, with no "Paused: you left
//   the step" toast;
// - selecting words in that step and pressing Comment, which quotes them into its reply box, leaves the timer running
//   too, though the Comment button floats outside the card, and typing on at once goes into that box;
// - opening the command menu from the reply box, a redraw of the step while it is open, and Escape put the cursor back
//   in the new reply box, so typing on keeps the timer running;
// - focus in a field outside the step, or a click outside it, leaves the timer running too, with no toast.
//
// Starts serve_preview.py (this checkout's Cockpit on a copy of the live database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
// Screenshots go to the folder given as the first argument, when one is given.
//
// Usage, from the checkout: node tests/browser/reply_keeps_timer.cjs [screenshot folder]

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
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "reply-timer-test", "content-type": "application/json" };

const PHRASE = "Completed tasks appear in the CSV export";
const PAUSED_TOAST = "Paused: you left the step";

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const shots = process.argv[2] || null;
  if (shots) fs.mkdirSync(shots, { recursive: true });
  const work = workFolder("cockpit-reply-timer-");
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

    // The fixture database's open steps are snoozed and its timer paused, so this run's step is Next up.
    const copied = await user("GET", "/api/state");
    if (copied.timer) await user("POST", `/api/steps/${copied.timer.step_id}/timer/pause`);
    await Promise.all([...copied.queue, ...copied.ready_next, ...copied.snoozed].map((step) => user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));

    const key = `note:reply-timer-${Date.now()}`;
    await agent("PUT", "/api/items", { key, title: "Reply timer test", priority: "urgent" });
    const answer = await agent("POST", "/api/steps", {
      item_key: key, need: "decide:reply-timer", ask: "Include completed tasks in the export?", score: 100, session_id: SESSION_B,
      body: `${PHRASE}, with a separate column for the completion date.`,
    });
    const id = answer.step.id;
    await user("POST", `/api/steps/${id}/timer/start`);
    const running = async () => (await user("GET", "/api/state")).timer?.step_id === id;

    browser = await chromium.launch({ executablePath: CHROMIUM });
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
    const page = await context.newPage();
    page.setDefaultTimeout(5000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
    await page.goto(`${info.origin}/classic`);

    const card = page.locator(`#next [data-step="${id}"]`);
    await card.locator('.step-menu [data-timer="running"]').waitFor({ state: "attached" });
    const toastShown = () => page.locator("#toast").evaluate((el, text) => !el.hidden && el.textContent.includes(text), PAUSED_TOAST);

    // Typing in the reply box.
    const reply = card.locator(`textarea[data-key="msg:step:${id}"]`);
    await reply.click();
    await reply.pressSequentially("Include the completion date for each task", { delay: 20 });
    await sleep(800);
    check("the reply box holds what was typed", (await reply.inputValue()).includes("Include the completion date for each task"), await reply.inputValue());
    check("typing in the reply box keeps the timer running", await running(), "the server's timer stopped");
    check("the card's clock still runs after typing", await card.locator('.step-menu [data-timer="running"]').count() === 1, "no running clock on the card");
    check("typing in the reply box toasts no pause", !(await toastShown()), "the pause toast showed");
    if (shots) await page.screenshot({ path: path.join(shots, "typing.png") });

    // Selecting words in the step and pressing Comment, then typing on at once.
    await card.getByText(PHRASE).click({ clickCount: 3 });
    const comment = page.locator(".quote-button");
    await comment.waitFor({ state: "visible" });
    check("the Comment button sits outside the step's card", !(await comment.evaluate((el, step) => Boolean(el.closest(`[data-step="${step}"]`)), id)), "it is inside the card");
    await comment.click();
    await sleep(300);
    const focusedKey = () => page.evaluate(() => document.activeElement?.dataset?.key || document.activeElement?.tagName);
    check("Comment puts the cursor in the reply box", await focusedKey() === `msg:step:${id}`, await focusedKey());
    await page.keyboard.type("Agreed", { delay: 20 });
    await sleep(800);
    const quoted = await reply.inputValue();
    check("Comment quotes the words into the reply box", quoted.includes(`> ${PHRASE}`), quoted);
    check("typing right after Comment lands in the reply box", quoted.trimEnd().endsWith("Agreed"), quoted);
    check("Comment keeps the timer running", await running(), "the server's timer stopped");
    check("Comment toasts no pause", !(await toastShown()), "the pause toast showed");
    if (shots) await page.screenshot({ path: path.join(shots, "commented.png") });

    // The command menu opened from the reply box, a redraw of the step while it is open, then Escape:
    // the cursor goes back to the new reply box and typing on keeps the timer running.
    await reply.click();
    await page.keyboard.press("Control+End");
    await page.evaluate((box) => { window.replyBefore = document.querySelector(`textarea[data-key="${box}"]`); }, `msg:step:${id}`);
    await page.keyboard.press("Control+k");
    await page.locator("dialog[open]").waitFor();
    await agent("POST", "/api/steps", { item_key: key, need: "decide:reply-timer", why: `Redrawn at ${Date.now()}.` });
    await page.waitForFunction(() => !window.replyBefore.isConnected, null, { timeout: 15000 });
    await page.keyboard.press("Escape");
    await sleep(300);
    check("closing the command menu after a redraw puts the cursor back in the reply box", await focusedKey() === `msg:step:${id}`, await focusedKey());
    await page.keyboard.type(" a", { delay: 20 });
    await sleep(800);
    check("typing after the command menu lands in the reply box", (await reply.inputValue()).trimEnd().endsWith("Agreed a"), await reply.inputValue());
    check("the command menu and a redraw keep the timer running", await running(), "the server's timer stopped");
    check("the command menu toasts no pause", !(await toastShown()), "the pause toast showed");

    // Focus in a field outside the step, here the Add box's from the keyboard, leaves the timer running.
    await card.getByText(PHRASE).click();
    await page.keyboard.press("a");
    await sleep(800);
    check("the Add box's field outside the step has the cursor", !(await page.evaluate((step) => Boolean(document.activeElement.closest(`[data-step="${step}"]`)), id)), await focusedKey());
    check("focus in a field outside the step keeps the timer running", await running(), "the server's timer stopped");
    check("focus outside the step toasts no pause", !(await toastShown()), "the pause toast showed");
    await page.keyboard.press("Escape");

    // A click outside the step leaves the timer running.
    await page.locator("#goals").click({ position: { x: 10, y: 10 } });
    await sleep(800);
    check("a click outside the step keeps the timer running", await running(), "the server's timer stopped");
    check("a click outside the step toasts no pause", !(await toastShown()), "the pause toast showed");

    check("no page errors", errors.length === 0, errors.join(" | "));
    await context.close();
    await user("POST", `/api/steps/${id}/snooze`, { until: "2099-01-01T08:00:00Z" });
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
