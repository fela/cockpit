// Run with PLAYWRIGHT_MODULE=/path/to/node_modules/playwright node tests/browser/settled_card.cjs. Uses the keyboard
// fixture's isolated database; never touches live work or real agents.  PERM-23: at desktop and phone width, - a
// dialog shows at once as a card with Run, and counts in Blocked; - once the hook sees it close in its session, the
// card stays in place below a newer open dialog, marked closed,   with nothing to press, and leaves the Blocked
// count; - a minute after it closed, it has gone; - nothing is wider than the window, and the page logs no error.
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");
const { spawn, spawnSync } = require("node:child_process");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(__dirname, "../..");
const work = workFolder("cockpit-settled-card-");
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
let browser;

// Moves the dialog's close back by `seconds`, as if it had closed that long ago.
function closedAgo(id, seconds) {
  const stamp = new Date(Date.now() - seconds * 1000).toISOString().replace(/\.\d+Z$/, "Z");
  const result = spawnSync(python, ["-c", "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); "
    + "c.execute('UPDATE permission_requests SET resolved_at = ? WHERE id = ?', (sys.argv[2], int(sys.argv[3]))); c.commit()",
  path.join(work, "cockpit.db"), stamp, String(id)]);
  assert.equal(result.status, 0, String(result.stderr));
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
  const state = async () => (await fetch(`${origin}/api/state`, { headers: { Cookie: cookie } })).json();
  const executablePath = process.env.BROWSER_EXECUTABLE;
  browser = await chromium.launch({ headless: true, ...(executablePath ? { executablePath } : {}) });
  for (const [width, height] of [[1280, 900], [390, 844]]) {
    const ask = async (command, description) => {
      const response = await fetch(origin + "/api/permissions/requests", {
        method: "POST", headers: AGENT,
        body: JSON.stringify({ kind: "asked", session_id: SESSION_A, cwd: work, tool_name: "Bash",
          tool_input: { command, description }, tool_use_id: "", reason: "" }),
      });
      assert.equal(response.ok, true, await response.clone().text());
      return (await response.json()).id;
    };
    const id = await ask(`rm exports/preview-${width}.csv`, "Remove the Tasks export preview");
    await delay(1100);
    const newer = await ask(`cat reminders/weekly-${width}.txt`, "Read the Tasks reminder draft");
    const order = () => page.locator("#blocked .blocked-list > [data-request]").evaluateAll(
      (cards) => cards.map((card) => Number(card.dataset.request)));

    const context = await browser.newContext({ extraHTTPHeaders: { Cookie: cookie }, viewport: { width, height } });
    const page = await context.newPage();
    page.setDefaultTimeout(8000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
    await page.goto(origin + "/classic");
    const card = page.locator(`#blocked .blocked-card[data-request="${newer}"]`);
    await card.waitFor();
    assert.equal(await card.locator("button", { hasText: "Run" }).count() > 0, true, "the open dialog has no Run");
    assert.equal((await state()).counts.blocked, 2);
    assert.deepEqual(await order(), [newer, id]);
    await page.locator("#blocked").screenshot({ path: path.join(work, `open-${width}.png`) });

    const settled = await fetch(`${origin}/api/permissions/requests/${id}/settle`, {
      method: "POST", headers: AGENT, body: JSON.stringify({ state: "answered" }),
    });
    assert.equal(settled.ok, true, await settled.clone().text());
    await page.locator(`#blocked .blocked-card.settled[data-request="${id}"]`).waitFor({ timeout: 20000 });
    const closed = page.locator(`#blocked .blocked-card.settled[data-request="${id}"]`);
    assert.equal(await closed.locator("button").count(), 0, "the closed card offers a button");
    assert.match((await closed.locator(".settled-note").textContent()).trim(),
      /^The dialog closed in the session at .+, answered there or on its own, and the session went on\. Nothing to do here\.$/);
    assert.equal((await state()).counts.blocked, 1);
    // The closed card stays where it was, below the newer open one.
    assert.deepEqual(await order(), [newer, id]);
    const fits = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
    assert.equal(fits, true, `${width}: the page is wider than the window`);
    await page.locator("#blocked").screenshot({ path: path.join(work, `closed-${width}.png`) });

    closedAgo(id, 61);
    await page.locator(`#blocked [data-request="${id}"]`).waitFor({ state: "detached", timeout: 20000 });
    assert.deepEqual(await order(), [newer]);
    const done = await fetch(`${origin}/api/permissions/requests/${newer}/settle`, {
      method: "POST", headers: AGENT, body: JSON.stringify({ state: "answered" }),
    });
    assert.equal(done.ok, true, await done.clone().text());
    closedAgo(newer, 61);
    await page.locator(`#blocked [data-request="${newer}"]`).waitFor({ state: "detached", timeout: 20000 });
    assert.deepEqual(errors, []);
    console.log(`PASS ${width}: a dialog that closed in its session stays a minute, marked closed, then goes`);
    await context.close();
  }
  console.log(`Screenshots in ${work}`);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
}).finally(async () => {
  await browser?.close();
  server.kill();
});
