// Check that a step's reply box has no message receipt, whether delivery succeeds or fails.
// Checks, at laptop and phone width:
// - after Send on a step, while the message is delivered, nothing shows under the box and "You sent" is nowhere;
// - when the message did not arrive, nothing shows under the box either.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what
// it needs) and drives a headless Chromium. Prints one line per check and exits 1 when one failed.
// Screenshots go to the folder given as the first argument, when one is given.
//
// Usage, from the checkout: node tests/browser/no_receipt.cjs [screenshot folder]

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
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "receipt-test", "content-type": "application/json" };
const LONG_NAME = "Prepare shared lists, exports and weekly reminders for Tasks";

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const shots = process.argv[2] || null;
  if (shots) fs.mkdirSync(shots, { recursive: true });
  const work = workFolder("cockpit-receipt-names-");
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

    const copied = await user("GET", "/api/state");
    if (copied.timer) await user("POST", `/api/steps/${copied.timer.step_id}/timer/pause`);
    await Promise.all([...copied.queue, ...copied.ready_next, ...copied.snoozed].map((step) => user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
    browser = await chromium.launch({ executablePath: CHROMIUM });

    for (const width of [1440, 390]) {
      const key = `note:receipt-${width}-${Date.now()}`;
      await agent("PUT", "/api/items", { key, title: "Receipt test", priority: "urgent", sessions: [SESSION_B] });
      const answer = await agent("POST", "/api/steps", {
        item_key: key, need: `review:receipt-${width}`, ask: "Receipt test", score: 100, session_id: SESSION_B, body: "Receipt test body.",
      });
      const id = answer.step.id;

      const context = await browser.newContext({ viewport: { width, height: 900 }, isMobile: width < 600, hasTouch: width < 600 });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      page.setDefaultTimeout(20000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(`${info.origin}/classic`, { timeout: 90000 });
      const card = page.locator(`#next article[data-step="${id}"]`).first();
      await card.waitFor({ timeout: 90000 });
      // A message sent from the step, delivered: nothing under the box.
      const words = `Please also check the shared list export before lunch ${width}`;
      const messageId = 10 + width;
      await page.evaluate(([id, words, name, messageId]) => {
        const step = ui.state.queue.find((other) => other.id === id);
        ui.state.messages = [{ id: messageId, author: "user", text: words, state: "sent", created_at: new Date().toISOString(),
          detail: "", step_id: id, session_id: null }, ...ui.state.messages];
        ui.sent.set(id, { text: words, at: new Date().toISOString(), version: stepVersion(step), to: name, id: messageId });
        render();
      }, [id, words, LONG_NAME, messageId]);
      await sleep(300);
      check(`${width}: a delivered message leaves no line under the box`, (await card.locator(".receipt").count()) === 0, "a line shows");
      check(`${width}: the delivered message's words are not on the card`, !(await card.textContent()).includes("shared list export before lunch"), "words shown");
      check(`${width}: "You sent" is nowhere on the card`, !(await card.textContent()).includes("You sent"), "found");
      if (shots) await card.screenshot({ path: path.join(shots, `delivered-${width}.png`) });

      // The same message did not arrive: still nothing under the box.
      await page.evaluate(([messageId]) => {
        const message = ui.state.messages.find((other) => other.id === messageId);
        message.state = "failed";
        message.detail = "The session did not answer";
        render();
      }, [messageId]);
      await sleep(300);
      const after = await card.evaluate((el) => ({ lines: el.querySelectorAll(".receipt").length, text: el.textContent }));
      check(`${width}: a failed message leaves no line under the box`, after.lines === 0, "a line shows");
      check(`${width}: the failed message's words are not on the card`, !after.text.includes("shared list export before lunch"), "words shown");
      if (shots) await card.screenshot({ path: path.join(shots, `failed-${width}.png`) });
      check(`${width}: no page errors`, errors.length === 0, errors.join("; "));
      await context.close();
      // The next width's step takes Next up.
      await user("POST", `/api/steps/${id}/snooze`, { until: "2099-01-01T08:00:00Z" });
    }
  } finally {
    if (browser) await browser.close();
    server.kill("SIGTERM");
  }
  const failed = results.filter((result) => !result.ok).length;
  console.error(failed ? `${failed} of ${results.length} checks failed` : `all ${results.length} checks passed`);
  if (failed) process.exitCode = 1;
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
    await sleep(200);
  }
  throw new Error("serve_preview.py did not come up within 60 s");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
