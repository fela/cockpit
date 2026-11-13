// Run with PLAYWRIGHT_MODULE=/path/to/node_modules/playwright node tests/browser/moved_past.cjs.
// Uses the keyboard fixture's isolated database; never touches live work or real agents.
//
// PERM-18: at desktop and phone width,
// - a refusal under a minute old stays off the page;
// - a refusal the session still needs shows as a card under Blocked and waiting, and counts in Blocked;
// - a refusal the session got past folds under one line at the end, out of the count, and opens to its card,
//   which says first how the session got past it;
// - nothing is wider than the window, and the page logs no error.
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");
const { spawn, spawnSync } = require("node:child_process");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(__dirname, "../..");
const work = workFolder("cockpit-moved-past-");
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
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "moved-past-test", "content-type": "application/json" };
const MOVED_ON = "It read the file with the Read tool instead, and that went through.";
let browser;

// Ages a refusal past the hold and stores Claude's explanation, as explain_next would have.
function explained(id, movedOn) {
  const stamp = (seconds) => new Date(Date.now() - seconds * 1000).toISOString().replace(/\.\d+Z$/, "Z");
  const explanation = JSON.stringify({
    plain: "The session wants to print a log file to see why a test failed.", sentence: "", moved_on: movedOn,
    count: 1,
  });
  const script = "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); "
    + "c.execute('UPDATE permission_requests SET created_at = ?, last_at = ?, explanation = ? WHERE id = ?', "
    + "(sys.argv[2], sys.argv[2], sys.argv[3], int(sys.argv[4]))); c.commit()";
  const result = spawnSync(python, ["-c", script, path.join(work, "cockpit.db"), stamp(120), explanation, String(id)]);
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
  const block = async (command) => {
    const response = await fetch(origin + "/api/permissions/requests", {
      method: "POST", headers: AGENT,
      body: JSON.stringify({ kind: "denied", session_id: SESSION_A, cwd: work, tool_name: "Bash", tool_input: { command },
        tool_use_id: "", reason: "moved past test" }),
    });
    assert.equal(response.ok, true, await response.clone().text());
    return (await response.json()).id;
  };
  const executablePath = process.env.BROWSER_EXECUTABLE;
  browser = await chromium.launch({ headless: true, ...(executablePath ? { executablePath } : {}) });
  for (const [width, height] of [[1280, 900], [390, 844]]) {
    const fresh = await block(`cat fresh-${width}.log`);
    const needed = await block(`cat needed-${width}.log`);
    explained(needed, "");
    const past = await block(`cat past-${width}.log`);
    explained(past, MOVED_ON);

    const context = await browser.newContext({ extraHTTPHeaders: { Cookie: cookie }, viewport: { width, height } });
    const page = await context.newPage();
    page.setDefaultTimeout(8000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
    await page.goto(origin + "/classic");
    await page.locator(`#blocked [data-request="${needed}"]`).waitFor();
    assert.equal(await page.locator(`#blocked [data-request="${fresh}"]`).count(), 0, "a refusal under a minute old shows");
    assert.equal(await page.locator(`#blocked [data-request="${past}"]`).count(), 0, "the call got past shows before it is opened");
    const state = await (await fetch(`${origin}/api/state`, { headers: { Cookie: cookie } })).json();
    assert.deepEqual(state.permission_requests.map((request) => request.id), [needed]);
    assert.deepEqual(state.moved_past.map((request) => request.id), [past]);
    assert.equal(state.counts.blocked, 1);

    const fold = page.locator("#blocked button.more-row", { hasText: "got past without you" });
    assert.equal((await fold.textContent()).trim(), "1 blocked call the sessions got past without you");
    await page.locator("#blocked").screenshot({ path: path.join(work, `folded-${width}.png`) });
    await fold.click();
    const line = page.locator(`#blocked [data-request="${past}"]`);
    await line.waitFor();
    if (await line.evaluate((el) => el.classList.contains("blocked-line"))) await line.click();
    const card = page.locator(`#blocked .blocked-card[data-request="${past}"]`);
    await card.waitFor();
    const note = card.locator(".explain-note").first();
    assert.equal((await note.textContent()).trim(), `Already handled: ${MOVED_ON}`);
    const fits = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
    assert.equal(fits, true, `${width}: the page is wider than the window`);
    await page.locator("#blocked").screenshot({ path: path.join(work, `opened-${width}.png`) });
    assert.deepEqual(errors, []);
    console.log(`PASS ${width}: a call the session got past folds under one line and opens with how it got past`);

    // Each width starts clean: the next width's three refusals are the only ones open.
    for (const id of [fresh, needed, past]) {
      const done = spawnSync(python, ["-c", "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); "
        + "c.execute(\"UPDATE permission_requests SET state = 'dismissed' WHERE id = ?\", (int(sys.argv[2]),)); c.commit()",
      path.join(work, "cockpit.db"), String(id)]);
      assert.equal(done.status, 0, String(done.stderr));
    }
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
