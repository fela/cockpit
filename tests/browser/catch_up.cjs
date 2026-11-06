// Run with PLAYWRIGHT_MODULE=/path/to/node_modules/playwright node tests/browser/catch_up.cjs. Uses the keyboard
// fixture's isolated database; never touches live work or real agents.  UI-60: on the sidebar page and the classic
// page, - a tab back after less than a poll (5 s) away shows its cards as they are; - a tab back after longer, while
// a step it shows left, veils the open card, Blocked and waiting and the queue   (blurred and faded under a moving
// line
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");
const { spawn, spawnSync } = require("node:child_process");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(__dirname, "../..");
const work = workFolder("cockpit-catch-up-");
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
  const SWEEP = { authorization: AGENT.authorization, "x-cockpit-as": "catch-up-test", "content-type": "application/json" };
  const queued = async (name) => {
    const key = `note:catch-up-${name}`;
    const item = await fetch(origin + "/api/items", { method: "PUT", headers: SWEEP, body: JSON.stringify({ key, title: "Catch-up check" }) });
    assert.equal(item.ok, true, await item.clone().text());
    const response = await fetch(origin + "/api/steps", {
      method: "POST", headers: SWEEP, body: JSON.stringify({ item_key: key, need: "decide", ask: `Keep the ${name} check?`, score: 100 }),
    });
    assert.equal(response.ok, true, await response.clone().text());
    return (await response.json()).step;
  };
  const withdrawStep = async (id) => {
    const pulled = await fetch(`${origin}/api/steps/${id}/withdraw`, { method: "POST", headers: SWEEP, body: "{}" });
    assert.equal(pulled.ok, true, await pulled.clone().text());
  };
  const noted = async () => (await (await fetch(`${origin}/api/flashes?days=1`, { headers: { Cookie: cookie } })).json()).flashes;
  const executablePath = process.env.BROWSER_EXECUTABLE;
  browser = await chromium.launch({ headless: true, ...(executablePath ? { executablePath } : {}) });
  for (const address of ["/", "/classic"]) {
    const name = address === "/" ? "side" : "classic";
    const refusal = await refuse(`ls catch-up ${name}`);
    const step = await queued(name);
    // The step that leaves while the tab is away: another than the one picked, as agents' writes to a step the user
    // is on wait.
    const gone = await queued(`${name}-gone`);
    const context = await browser.newContext({ extraHTTPHeaders: { Cookie: cookie }, viewport: { width: 1280, height: 900 } });
    // Headless Chromium keeps a page in view: the page's view of it is set here instead.
    await context.addInitScript(() => {
      let state = "visible";
      Object.defineProperty(document, "visibilityState", { get: () => state, configurable: true });
      Object.defineProperty(document, "hidden", { get: () => state !== "visible", configurable: true });
      window.setShown = (shownNow) => { state = shownNow ? "visible" : "hidden"; document.dispatchEvent(new Event("visibilitychange")); };
    });
    const page = await context.newPage();
    page.setDefaultTimeout(20000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => { if (message.type() === "error" && !/Failed to load resource/.test(message.text())) errors.push(message.text()); });
    await page.goto(origin + address);
    const stepRow = (id) => (address === "/" ? page.locator(`#sidebar .sidebar-group.queue li[data-step="${id}"]`)
      : page.locator(`#next [data-step="${id}"], #queue [data-step="${id}"]`).first());
    const stepCard = stepRow(step.id);
    const goneCard = stepRow(gone.id);
    // The rail page shows a blocked call as a warning row in Your queue (PERM-25); the classic page in Blocked and waiting.
    const blockedShown = address === "/" ? `#sidebar .sidebar-group.queue [data-request="${refusal}"]` : `#blocked [data-request="${refusal}"]`;
    const blockedCard = page.locator(blockedShown).first();
    await stepCard.waitFor();
    await goneCard.waitFor();
    await blockedCard.waitFor();
    // On the rail page a warning takes Next up until the user picks a step (PERM-25): the step is picked, so Next up's
    // keys have a step to act on.
    if (address === "/") {
      await stepCard.click();
      await page.locator(`#next [data-step="${step.id}"]`).first().waitFor();
      // Picking may focus Next up's reply box, which would keep its part in view; the checks below start from no box.
      await page.evaluate(() => document.activeElement?.blur());
    }
    const catching = () => page.evaluate(() => document.body.classList.contains("catching-up"));
    const seen = (locator) => locator.evaluate((el) => el.checkVisibility({ visibilityProperty: true }));
    // The redraw that follows may still be moving the card into place (motion.js), or draw it anew: it shows, in
    // focus, within a few seconds.
    const comesBack = (selector, why) => page.waitForFunction((one) => {
      const el = document.querySelector(one);
      if (!el || !el.checkVisibility({ visibilityProperty: true })) return false;
      for (let at = el; at; at = at.parentElement) if (getComputedStyle(at).filter.includes("blur")) return false;
      return true;
    }, selector, { timeout: 3000 }).catch(() => assert.fail(why));
    // A card is veiled when it or a part around it is blurred (style.css, sidebar.css).
    const veiled = (locator) => locator.evaluate((el) => {
      for (let at = el; at; at = at.parentElement) if (getComputedStyle(at).filter.includes("blur")) return true;
      return false;
    });
    const waitVeiled = (selector, why) => page.waitForFunction((one) => {
      for (let at = document.querySelector(one); at; at = at.parentElement) if (getComputedStyle(at).filter.includes("blur")) return true;
      return false;
    }, selector, { timeout: 3000 }).catch(() => assert.fail(why));
    // The moving line over a veiled part.
    const line = (selector) => page.evaluate((one) => getComputedStyle(document.querySelector(one), "::before").animationName, selector);
    // Whether the page veiled anything since the last call.
    const everVeiled = () => page.evaluate(() => {
      const was = window.veiledSeen;
      window.veiledSeen = false;
      return was;
    });
    await page.evaluate(() => {
      window.veiledSeen = false;
      new MutationObserver(() => { if (document.body.classList.contains("catch-up-veiled")) window.veiledSeen = true; })
        .observe(document.body, { attributes: true, attributeFilter: ["class"] });
    });

    // A tab away for less than a poll is as current as one in view: no "Updating…".
    await page.evaluate(() => window.setShown(false));
    await delay(1000);
    await page.evaluate(() => window.setShown(true));
    assert.equal(await catching(), false, "a page away for a second showed Updating…");

    // Away for longer, while the step left: back, the page veils its cards until the state comes.
    const flashesBefore = (await noted()).length;
    let open;
    const gate = new Promise((resolve) => { open = resolve; });
    await page.route("**/api/state**", async (route) => { await gate; await route.continue(); });
    await page.evaluate(() => window.setShown(false));
    await delay(6000);
    await withdrawStep(gone.id);
    await page.evaluate(() => window.setShown(true));
    assert.equal(await catching(), true, "a page back after six seconds away did not catch up");
    assert.equal(await goneCard.count(), 1, "the withdrawn step should still be drawn, veiled, until the state comes");
    // The quick list says the step left: the veil comes at once, without waiting out the grace.
    await waitVeiled(address === "/" ? `#sidebar .sidebar-group.queue li[data-step="${gone.id}"]` : `[data-step="${gone.id}"]`,
      "the step that left was not veiled while the page caught up");
    assert.equal(await veiled(blockedCard), true, "the card in Blocked and waiting was not veiled while the page caught up");
    assert.equal(await line("#next"), "catch-up-line", "Next up showed no moving line while veiled");
    if (address === "/") {
      assert.equal(await line("#sidebar .sidebar-group.queue .sidebar-list"), "catch-up-line", "the sidebar's queue showed no moving line");
    }
    assert.equal(await page.evaluate(() => document.getElementById("next").getAttribute("aria-busy")), "true");
    // The parts take no clicks, focus or keyboard rows meanwhile.
    assert.equal(await page.evaluate(() => document.getElementById("next").inert), true, "Next up was not inert while veiled");
    if (address === "/") {
      assert.equal(await page.evaluate(() => document.querySelector("#sidebar .sidebar-group.queue .sidebar-list").inert), true,
        "the sidebar's queue was not inert while veiled");
    }
    await page.locator("body").press("e");
    await page.locator("#toast", { hasText: "Updating" }).waitFor();
    await delay(1500);
    assert.equal(await catching(), true, "the page stopped catching up before its state came");
    open();
    await page.waitForFunction(() => !document.body.classList.contains("catching-up"));
    await goneCard.waitFor({ state: "detached" });
    await comesBack(blockedShown, "the card in Blocked and waiting did not come back");
    assert.equal(await page.evaluate(() => [...document.querySelectorAll("[inert]")].length), 0, "a part stayed inert after the page was current");
    await page.unroute("**/api/state**");
    await delay(1500);
    assert.equal((await noted()).length, flashesBefore, "the step that left while the tab was away was noted as a flash");

    // Away for longer while nothing shown left: the quick list says so, and the cards show before the state comes,
    // never veiled.
    await everVeiled();
    let openQuiet;
    const quietGate = new Promise((resolve) => { openQuiet = resolve; });
    await page.route("**/api/state**", async (route) => { await quietGate; await route.continue(); });
    await page.evaluate(() => window.setShown(false));
    await delay(6000);
    await page.evaluate(() => window.setShown(true));
    await page.waitForFunction(() => !document.body.classList.contains("catching-up"), null, { timeout: 5000 })
      .catch(() => assert.fail("a page back with nothing gone waited for the full state"));
    assert.equal(await seen(blockedCard), true, "the card in Blocked and waiting stayed out of sight with nothing gone");
    assert.equal(await everVeiled(), false, "a page back with nothing gone veiled its cards");
    assert.equal(await page.evaluate(() => document.getElementById("next").hasAttribute("aria-busy")), false);
    const stateCame = page.waitForResponse((response) => response.url().includes("/api/state"));
    openQuiet();
    await stateCame;
    await page.unroute("**/api/state**");

    // A state Cockpit refuses shows the cards again (the banner says they are old).
    await page.route("**/api/state**", (route) => route.fulfill({ status: 500, contentType: "application/json", body: "{\"detail\": \"test refusal\"}" }));
    await page.evaluate(() => window.setShown(false));
    await delay(6000);
    await page.evaluate(() => window.setShown(true));
    await page.waitForFunction(() => !document.body.classList.contains("catching-up"), null, { timeout: 5000 });
    await comesBack(blockedShown, "the cards stayed out of sight after the state failed");
    await page.locator("#banner", { hasText: "Cockpit could not refresh" }).waitFor({ timeout: 3000 });
    await page.unroute("**/api/state**");
    // The refused state's own error is the point of that check.
    errors.splice(0, errors.length, ...errors.filter((text) => !/refreshing the page|test refusal|500/.test(text)));

    // A box the user types in keeps its part of the page in view; the other parts catch up.
    const box = page.locator("#next textarea").first();
    await box.click();
    await box.fill("half a reply");
    let openTyped;
    const typedGate = new Promise((resolve) => { openTyped = resolve; });
    await page.route("**/api/state**", async (route) => { await typedGate; await route.continue(); });
    // The quick list would end the wait at once, as nothing left: it is held back to see the wait.
    await page.route("**/api/listed**", async (route) => { await typedGate; await route.continue(); });
    await page.evaluate(() => window.setShown(false));
    await delay(6000);
    await page.evaluate(() => window.setShown(true));
    assert.equal(await catching(), true, "the parts without the box did not catch up");
    assert.equal(await seen(box), true, "the box in use went out of sight");
    // The quick list is held, so the veil comes once the grace passes.
    await waitVeiled(blockedShown, "Blocked and waiting was not veiled while the page caught up beside a box in use");
    assert.equal(await veiled(box), false, "the box in use was veiled");
    openTyped();
    await page.waitForFunction(() => !document.body.classList.contains("catching-up"));
    await page.unroute("**/api/state**");
    await page.unroute("**/api/listed**");
    assert.equal(await box.inputValue(), "half a reply");
    await box.fill("");
    await page.evaluate(() => document.activeElement?.blur());

    // A tab in the background for over half a minute while a step left draws the state meanwhile: back, nothing is
    // veiled and the step is gone.
    const later = await queued(`${name}-later`);
    await stepRow(later.id).waitFor();
    await everVeiled();
    await page.evaluate(() => window.setShown(false));
    await delay(2000);
    await withdrawStep(later.id);
    const drawnHidden = page.waitForResponse((response) => response.url().includes("/api/state"), { timeout: 40000 });
    await drawnHidden;
    await delay(1000);
    await page.evaluate(() => window.setShown(true));
    await page.waitForFunction(() => !document.body.classList.contains("catching-up"), null, { timeout: 5000 });
    assert.equal(await stepRow(later.id).count(), 0, "the step that left while the tab was hidden was still drawn on its return");
    assert.equal(await everVeiled(), false, "a page kept current in the background veiled its cards on return");

    assert.deepEqual(errors, []);
    console.log(`PASS ${address}: back after a poll's time away with a step gone, the cards are veiled until the state comes; not after a second away; Next up's keys wait; nothing gone shows the cards before the state, never veiled; a box in use stays; a failed state shows the cards under the banner; a tab hidden for half a minute is drawn current and veils nothing on return`);
    await context.close();
    await fetch(`${origin}/api/permissions/requests/${refusal}/withdraw`, { method: "POST", headers: AGENT, body: JSON.stringify({ session_id: SESSION_A }) });
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
}).finally(async () => {
  await browser?.close();
  server.kill();
});
