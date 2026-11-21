// Run with node tests/browser/checker_card.cjs (PLAYWRIGHT_MODULE and BROWSER_EXECUTABLE override the host's).
// Uses the keyboard fixture's isolated database; never touches live work or real agents.  PERM-32: calls auto mode
// refused because its checker gave no verdict, past the hour Cockpit asks their sessions to try again, at 1280 px light
// and 390 px dark, - show as one red row in Inbox, not a row each; - the row opens one card on Next up that names the
// calls and their sessions, with Try again and Dismiss and no rule or sentence; - Try again takes the card off at once
// and asks the session; - a new refused call brings the row back, and Dismiss on its card closes the calls; -
// nothing is wider than the window, and the page logs no error.
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");
const { spawn, spawnSync } = require("node:child_process");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(__dirname, "../..");
const work = workFolder("cockpit-checker-card-");
const python = path.join(root, ".venv/bin/python");
const server = spawn(python, [path.join(__dirname, "serve_keyboard.py"), work], {
  cwd: root, stdio: ["ignore", "pipe", "pipe"],
});
let serverLog = "";
server.stdout.on("data", (data) => { serverLog += data; });
server.stderr.on("data", (data) => { serverLog += data; });
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
// The fixture's session (tests/conftest.py) and agent token.
const SESSION_A = "11111111-1111-4111-8111-111111111111";
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "permission-hook", "content-type": "application/json" };
const NO_VERDICT = "Classifier unavailable";
let browser;

// The refusals so far came over an hour ago, past the hour Cockpit asks the session to try again by itself.
function anHourAgo() {
  const code = `
import sqlite3, sys
from pathlib import Path
for path in Path(sys.argv[1]).rglob("*.db"):
    conn = sqlite3.connect(path)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'permission_requests'").fetchone():
        with conn:
            conn.execute("UPDATE permission_requests SET created_at = strftime('%Y-%m-%dT%H:%M:%SZ', created_at, '-61 minutes'),"
                         " last_at = strftime('%Y-%m-%dT%H:%M:%SZ', last_at, '-61 minutes'),"
                         " no_verdict_since = strftime('%Y-%m-%dT%H:%M:%SZ', no_verdict_since, '-61 minutes') WHERE state = 'open'")
    conn.close()
`;
  const done = spawnSync(python, ["-c", code, work], { encoding: "utf8" });
  assert.equal(done.status, 0, done.stderr);
}

(async () => {
  const runtime = path.join(work, "runtime.json");
  for (let at = 0; !fs.existsSync(runtime); at++) {
    if (at > 1200 || server.exitCode !== null) throw new Error(`Fixture failed: ${serverLog}`);
    await delay(100);
  }
  const { origin, cookie } = JSON.parse(fs.readFileSync(runtime, "utf8"));
  for (let at = 0; ; at++) {
    try { if ((await fetch(origin + "/healthz")).ok) break; } catch {}
    if (at > 600) throw new Error(`Fixture did not listen: ${serverLog}`);
    await delay(100);
  }
  const executablePath = process.env.BROWSER_EXECUTABLE || undefined;
  browser = await chromium.launch({ headless: true, ...(executablePath ? { executablePath } : {}) });
  const refuse = async (toolName, toolInput) => {
    const response = await fetch(origin + "/api/permissions/requests", {
      method: "POST", headers: AGENT,
      body: JSON.stringify({ kind: "denied", session_id: SESSION_A, cwd: work, tool_name: toolName,
        tool_input: toolInput, tool_use_id: "", reason: NO_VERDICT }),
    });
    assert.equal(response.ok, true, await response.clone().text());
    return (await response.json()).id;
  };
  for (const [width, height, scheme] of [[1280, 900, "light"], [390, 844, "dark"]]) {
    const phone = width < 600;
    const push = await refuse("Bash", { command: `git push origin tasks-export-${width}`, description: "Push the Tasks export branch" });
    const review = await refuse("Agent", { description: "Review the reminder copy", prompt: "Read the reminder texts." });
    anHourAgo();

    const context = await browser.newContext({ extraHTTPHeaders: { Cookie: cookie }, viewport: { width, height }, colorScheme: scheme });
    const page = await context.newPage();
    page.setDefaultTimeout(10000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
    await page.goto(origin + "/");
    const row = page.locator("#sidebar li.warn-row[data-checker]");
    await row.waitFor({ state: "attached" });
    assert.equal(await page.locator(`#sidebar li.warn-row:is([data-request="${push}"], [data-request="${review}"])`).count(), 0,
      `${width}: the calls have rows of their own`);
    assert.equal((await row.textContent()).trim(), "Checker gave no verdict");

    if (phone) await page.evaluate(() => setPhoneScreen(true, false));
    await row.locator(".side-row").click();
    const card = page.locator("#next > .checker-card:not(.motion-ghost)");
    await card.waitFor();
    assert.equal((await card.locator(".what").textContent()).trim(), "Auto mode's checker gave no verdict on 2 calls");
    assert.deepEqual(await card.locator(".checker-call").allTextContents(), ["Review the reminder copy", "Push the Tasks export branch"]);
    assert.deepEqual(await card.locator(".actions button").evaluateAll((buttons) => buttons.map((button) => button.textContent.trim() || button.getAttribute("aria-label"))),
      ["Try again", "Dismiss: the sessions are not told"]);
    assert.equal(await card.locator(".rule-draft, .sentence-option").count(), 0, `${width}: the card offers a rule`);
    const fits = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
    assert.equal(fits, true, `${width}: the page is wider than the window`);
    await delay(400);  // The phone slides from Inbox to Next up.
    await page.screenshot({ path: path.join(work, `card-${width}-${scheme}.png`) });

    // Try again: the card goes at once, and Cockpit queues one message to the session.
    const retried = page.waitForResponse((response) => response.url().endsWith("/api/permissions/checker/retry"));
    await card.locator("button.primary", { hasText: "Try again" }).click();
    await card.waitFor({ state: "detached", timeout: 1000 });
    const answer = await retried;
    assert.equal(answer.status(), 200, await answer.text());
    assert.equal((await answer.json()).messages.length, 1);

    // A new refused call brings the row back; Dismiss on its card closes the calls.
    await refuse("Bash", { command: `git push origin tasks-export-${width}`, description: "Push the Tasks export branch" });
    anHourAgo();
    await page.goto(origin + "/");
    await row.waitFor({ state: "attached" });
    if (phone) await page.evaluate(() => setPhoneScreen(true, false));
    await row.locator(".side-row").click();
    await card.waitFor();
    const open = await page.evaluate(() => ui.state.checker_refusals.map((request) => request.id));
    const dismissed = Promise.all(open.map((id) => page.waitForResponse((response) => response.url().endsWith(`/api/permissions/requests/${id}/dismiss`))));
    await card.locator("button[aria-label^='Dismiss']").click();
    await card.waitFor({ state: "detached", timeout: 1000 });
    for (const response of await dismissed) assert.equal(response.status(), 200, await response.text());
    assert.deepEqual(errors, []);
    // Whatever the retry left (a message the fixture's session never takes), so the next width starts clear.
    await page.evaluate(async () => {
      await refresh();
      await Promise.all([...ui.state.checker_refusals, ...ui.state.held_requests]
        .map((request) => api("POST", `/api/permissions/requests/${request.id}/dismiss`, undefined)
          .catch((error) => { if (!/already dismissed/.test(error.message)) throw error; })));
    });
    console.log(`PASS ${width} ${scheme}: one card for the calls the checker gave no verdict on`);
    await context.close();
  }
  console.log(`Screenshots in ${work}`);
})().catch((error) => {
  console.error(error);
  console.error(serverLog.slice(-4000));
  process.exitCode = 1;
}).finally(async () => {
  await browser?.close();
  server.kill();
});
