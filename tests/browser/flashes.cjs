// Run with PLAYWRIGHT_MODULE=/path/to/node_modules/playwright node tests/browser/flashes.cjs. Uses the keyboard
// fixture's isolated database; never touches live work or real agents.  PERM-24: on the classic page and the sidebar
// page, - a refusal that shows and is withdrawn by its session within a minute is noted, with what took it away; - so
// is one already there when the page loaded: it came into view then; - one the user pressed in before it went is not;
// - a step that takes a place in the queue and is withdrawn by an agent within a minute is noted; - the page logs no
// error.
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");
const { spawn, spawnSync } = require("node:child_process");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(__dirname, "../..");
const work = workFolder("cockpit-flashes-");
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

// The user's press on a card holds a withdrawal for some minutes (PERM-9), and the server's loop closes it after;
// the fixture runs no such loop, so this closes it as the loop would.
function leftCard(id) {
  const script = "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); "
    + "c.execute(\"UPDATE permission_requests SET state = 'withdrawn', resolved_at = strftime('%Y-%m-%dT%H:%M:%SZ') WHERE id = ?\", (int(sys.argv[2]),)); c.commit()";
  const result = spawnSync(python, ["-c", script, path.join(work, "cockpit.db"), String(id)]);
  assert.equal(result.status, 0, String(result.stderr));
}

// A refusal shows once it is a minute old and Claude has explained it (PERM-18): this makes it so.
function shown(id) {
  const stamp = new Date(Date.now() - 120e3).toISOString().replace(/\.\d+Z$/, "Z");
  const explanation = JSON.stringify({ plain: "The session wants to list a folder.", sentence: "", moved_on: "", count: 1 });
  const script = "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); "
    + "c.execute('UPDATE permission_requests SET created_at = ?, last_at = ?, explanation = ? WHERE id = ?', "
    + "(sys.argv[2], sys.argv[2], sys.argv[3], int(sys.argv[4]))); c.commit()";
  const result = spawnSync(python, ["-c", script, path.join(work, "cockpit.db"), stamp, explanation, String(id)]);
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
  const refuse = async (command) => {
    const response = await fetch(origin + "/api/permissions/requests", {
      method: "POST", headers: AGENT,
      body: JSON.stringify({ kind: "denied", session_id: SESSION_A, cwd: work, tool_name: "Bash",
        tool_input: { command, description: command }, tool_use_id: "", reason: "" }),
    });
    assert.equal(response.ok, true, await response.clone().text());
    const { id } = await response.json();
    shown(id);
    return id;
  };
  const withdraw = async (id) => {
    const response = await fetch(`${origin}/api/permissions/requests/${id}/withdraw`, {
      method: "POST", headers: AGENT, body: JSON.stringify({ session_id: SESSION_A }),
    });
    assert.equal(response.ok, true, await response.clone().text());
  };
  const SWEEP = { authorization: AGENT.authorization, "x-cockpit-as": "flash-test", "content-type": "application/json" };
  const queued = async (address) => {
    const key = `note:flash${address.replace(/\W/g, "-")}`;
    const item = await fetch(origin + "/api/items", { method: "PUT", headers: SWEEP, body: JSON.stringify({ key, title: "Flash check" }) });
    assert.equal(item.ok, true, await item.clone().text());
    const response = await fetch(origin + "/api/steps", {
      method: "POST", headers: SWEEP, body: JSON.stringify({ item_key: key, need: "decide", ask: "Keep the flash check?", score: 100 }),
    });
    assert.equal(response.ok, true, await response.clone().text());
    return (await response.json()).step;
  };
  const noted = async () => (await (await fetch(`${origin}/api/flashes?days=1`, { headers: { Cookie: cookie } })).json()).flashes;
  const executablePath = process.env.BROWSER_EXECUTABLE;
  browser = await chromium.launch({ headless: true, ...(executablePath ? { executablePath } : {}) });
  for (const address of ["/classic", "/"]) {
    const before = await refuse(`ls before-load ${address}`);
    const context = await browser.newContext({ extraHTTPHeaders: { Cookie: cookie }, viewport: { width: 1280, height: 900 } });
    const page = await context.newPage();
    page.setDefaultTimeout(20000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
    await page.goto(origin + address);
    // The page with the rail shows each refusal as a red row in Your queue (PERM-25).
    const card = (id) => page.locator(`:is(#blocked, #sidebar) [data-request="${id}"]`).first();
    await card(before).waitFor();
    const brief = await refuse(`ls brief ${address}`);
    await card(brief).waitFor();
    const pressed = await refuse(`ls pressed ${address}`);
    await card(pressed).waitFor();
    await card(pressed).dispatchEvent("pointerdown");
    await card(pressed).click({ position: { x: 4, y: 4 } });
    // Past flashes.js's PRESS_MS, so the cards that go next went without the press.
    await delay(3500);
    const step = await queued(address);
    const state = await (await fetch(`${origin}/api/state`, { headers: { Cookie: cookie } })).json();
    assert.ok(state.queue.some((one) => one.id === step.id), "the new step has no place in the queue");
    const stepShown = address === "/classic" ? page.locator(`#next [data-step="${step.id}"], #queue [data-step="${step.id}"]`)
      : page.locator(`#sidebar .sidebar-group.queue li[data-step="${step.id}"]`);
    await stepShown.first().waitFor();
    const count = (await noted()).length;
    const pulled = await fetch(`${origin}/api/steps/${step.id}/withdraw`, { method: "POST", headers: SWEEP, body: "{}" });
    assert.equal(pulled.ok, true, await pulled.clone().text());
    for (const id of [before, brief, pressed]) await withdraw(id);
    await delay(500);
    leftCard(pressed);
    for (const id of [before, brief, pressed]) await card(id).waitFor({ state: "detached" });
    let found = [];
    await stepShown.first().waitFor({ state: "detached" });
    for (let at = 0; at < 50 && found.length < count + 3; at++) { found = await noted(); await delay(100); }
    await delay(1000);
    found = await noted();
    const mine = found.slice(0, found.length - count);
    const withdrawn = "withdrawn: the session got past it without the user";
    const order = (a, b) => a[0].localeCompare(b[0]) || a[1] - b[1];
    assert.deepEqual(mine.map((flash) => [flash.kind, Number(flash.ref), flash.page, flash.outcome]).sort(order),
      [["request", before, address, withdrawn], ["request", brief, address, withdrawn],
        ["step", step.id, address, "withdrawn; last change: step.withdraw by agent:flash-test"]]);
    for (const flash of mine.filter((one) => one.kind === "request")) assert.match(flash.label, address === "/classic" ? /The session wants to list a folder\./ : /The session wants to list a folder\.|^ls (before-load|brief) \/$/);
    // Cards "Show fewer" folds away went by that press, though the press was outside them. The page with the rail
    // has no "+N more": every refusal has its row.
    const folded = [];
    if (address === "/classic") {
    for (let n = 0; n < 4; n++) folded.push(await refuse(`ls folded ${n} ${address}`));
    const drawnIds = () => page.evaluate(() => [...document.querySelectorAll("#blocked [data-request]")].map((el) => el.dataset.request));
    await page.locator("#blocked .more-row", { hasText: /^\+\d+ more$/ }).waitFor();
    const top = await drawnIds();
    await page.locator("#blocked .more-row", { hasText: /^\+\d+ more$/ }).click();
    await page.locator("#blocked .more-row", { hasText: "Show fewer" }).waitFor();
    const extra = (await drawnIds()).filter((id) => !top.includes(id));
    assert.ok(extra.length > 0, "\"+N more\" showed no more cards");
    await page.locator("#blocked .more-row", { hasText: "Show fewer" }).click();
    for (const id of extra) await card(id).waitFor({ state: "detached" });
    await delay(1500);
    assert.equal((await noted()).length, found.length, "a card \"Show fewer\" folded away was noted");
    }
    if (address === "/") {
      // A side-list row is one item's: its switch to the item's other step is no card going.
      const key = "note:flash-two-steps";
      const item = await fetch(origin + "/api/items", { method: "PUT", headers: SWEEP, body: JSON.stringify({ key, title: "Two steps" }) });
      assert.equal(item.ok, true, await item.clone().text());
      const put = async (need, score) => (await (await fetch(origin + "/api/steps", {
        method: "POST", headers: SWEEP, body: JSON.stringify({ item_key: key, need, ask: `Answer ${need}?`, score }),
      })).json()).step;
      const first = await put("first", 90);
      const row = page.locator(`#sidebar .sidebar-group.queue li[data-item="${first.item_id}"]`);
      await page.locator(`#sidebar .sidebar-group.queue li[data-step="${first.id}"]`).waitFor();
      const second = await put("second", 100);
      const order = await (await fetch(`${origin}/api/state`, { headers: { Cookie: cookie } })).json();
      const lead = order.queue.filter((one) => one.item_id === first.item_id).map((one) => one.id);
      assert.deepEqual(lead.sort(), [first.id, second.id].sort(), "both steps should hold queue places");
      const showsBefore = await row.getAttribute("data-step");
      const pulled = await fetch(`${origin}/api/steps/${showsBefore}/withdraw`, { method: "POST", headers: SWEEP, body: "{}" });
      assert.equal(pulled.ok, true, await pulled.clone().text());
      const other = String(showsBefore) === String(first.id) ? second.id : first.id;
      await page.locator(`#sidebar .sidebar-group.queue li[data-step="${other}"]`).waitFor();
      await delay(1500);
      assert.equal((await noted()).length, found.length, "a side-list row that switched to its item's other step was noted");
    }
    assert.deepEqual(errors, []);
    console.log(`PASS ${address}: refusals and a step withdrawn within a minute of coming into view are noted; a card pressed, or folded away by \"Show fewer\", is not`);
    await context.close();
    for (const id of folded) await withdraw(id);
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
}).finally(async () => {
  await browser?.close();
  server.kill();
});
