// Run with PLAYWRIGHT_MODULE=/path/to/node_modules/playwright node tests/browser/catch_up.cjs. Uses the keyboard
// fixture's isolated database; never touches live work or real agents.  UI-60: on the sidebar page and the classic
// page, - a tab back after less than a poll (5 s) away shows its cards as they are; - a tab back after longer keeps
// its cards as drawn and in use while it asks the quick list (/api/listed) which still stand; - when the quick list
// names a step that left, only that step's card is veiled (blurred, faded and inert) under a moving line until the
// state comes, and the other cards and Next up stay sharp and in use; - when the quick list has not answered within
// the grace, every part is veiled and inert and Next up's keys wait, and the quick list's late answer narrows the veil
// to the card that left.  The page's grace is 2.5 s; the test serves the page's script with a longer one, so a slow
// host's quick list still answers inside it
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
// The grace the test's pages run with, in place of the page's own.
const PAGE_GRACE = "const CATCH_UP_GRACE_MS = 2500;";
const GRACE = 8000;
const pageScript = fs.readFileSync(path.join(root, "src/cockpit/static/app.js"), "utf8");
assert.equal(pageScript.split(PAGE_GRACE).length, 2, "the page's script no longer sets its grace as the test expects");
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
    await context.route("**/static/app.js*", (route) => route.fulfill({
      body: pageScript.replace(PAGE_GRACE, `const CATCH_UP_GRACE_MS = ${GRACE};`), contentType: "text/javascript",
    }));
    const page = await context.newPage();
    page.setDefaultTimeout(20000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    // The state this test refuses ("test refusal") is reported by the page whenever its answer lands, so it is left
    // out here and not at one moment of the run.
    page.on("console", (message) => {
      if (message.type() === "error" && !/Failed to load resource|test refusal/.test(message.text())) errors.push(message.text());
    });
    await page.goto(origin + address);
    const rowShown = (id) => (address === "/" ? `#sidebar .sidebar-group.queue li[data-step="${id}"]`
      : `#next [data-step="${id}"], #queue [data-step="${id}"]`);
    const stepRow = (id) => page.locator(rowShown(id)).first();
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
    const waitVeiled = (selector, timeout, why) => page.waitForFunction((one) => {
      for (let at = document.querySelector(one); at; at = at.parentElement) if (getComputedStyle(at).filter.includes("blur")) return true;
      return false;
    }, selector, { timeout }).catch(() => assert.fail(why));
    // A card takes no clicks, focus or keys when it or a part around it is inert.
    const inert = (locator) => locator.evaluate((el) => Boolean(el.closest("[inert]")));
    // The card the quick list named as gone: blurred by its own class, and inert.
    const markedGone = (locator) => locator.evaluate((el) => el.classList.contains("catch-up-gone") && el.inert);
    const bodyHas = (name) => page.evaluate((one) => document.body.classList.contains(one), name);
    const nextWaits = () => page.evaluate(() => {
      const next = document.getElementById("next");
      return { busy: next.getAttribute("aria-busy") === "true", inert: next.inert };
    });
    const sidebarInert = () => page.evaluate(() => document.querySelector("#sidebar .sidebar-group.queue .sidebar-list").inert);
    // The moving line over a veiled part.
    const line = (selector) => page.evaluate((one) => getComputedStyle(document.querySelector(one), "::before").animationName, selector);
    // Whether the page veiled anything since the last call.
    const everVeiled = () => page.evaluate(() => {
      const was = window.veiledSeen;
      window.veiledSeen = false;
      return was;
    });
    // The observer also notes when the page last began veiling every part, on the page's own clock.
    await page.evaluate(() => {
      window.veiledSeen = false;
      window.veiledAt = null;
      let was = false;
      new MutationObserver(() => {
        const now = document.body.classList.contains("catch-up-veiled");
        if (now && !was) window.veiledAt = performance.now();
        if (now) window.veiledSeen = true;
        was = now;
      }).observe(document.body, { attributes: true, attributeFilter: ["class"] });
    });

    // A tab away for less than a poll is as current as one in view: no "Updating…".
    await page.evaluate(() => window.setShown(false));
    await delay(1000);
    await page.evaluate(() => window.setShown(true));
    assert.equal(await catching(), false, "a page away for a second showed Updating…");

    // Away for longer, while a step in the queue left: back, the quick list names it, and only its card is veiled
    // until the state comes. The step is neither Next up's nor the queue's top (the steps of score 100 rank by age, and
    // the sidebar page's pick is the older one), so Next up stays in use.
    const flashesBefore = (await noted()).length;
    await everVeiled();
    let open;
    const gate = new Promise((resolve) => { open = resolve; });
    await page.route("**/api/state**", async (route) => { await gate; await route.continue(); });
    await page.evaluate(() => window.setShown(false));
    await delay(6000);
    await withdrawStep(gone.id);
    const listedAnswer = page.waitForResponse((response) => response.url().includes("/api/listed"));
    await page.evaluate(() => window.setShown(true));
    const backA = Date.now();
    assert.equal(await catching(), true, "a page back after six seconds away did not catch up");
    assert.equal(await goneCard.count(), 1, "the withdrawn step should still be drawn, veiled, until the state comes");
    // The checks below need the quick list's answer inside the grace; a host too slow for that fails here, by name.
    await listedAnswer;
    const answered = Date.now() - backA;
    assert.ok(answered < GRACE, `the quick list took ${answered} ms to answer, past the grace: the host is too slow for this check`);
    // The quick list says the step left: its veil comes at once, without waiting out the grace.
    await waitVeiled(rowShown(gone.id), 2000, "the step that left was not veiled while the page caught up");
    assert.equal(await markedGone(goneCard), true, "the step that left was not marked gone and inert");
    assert.equal(await bodyHas("catch-up-noted"), true, "the page did not note that it catches up");
    assert.equal(await bodyHas("catch-up-veiled"), false, "a quick list that named the step that left veiled every card");
    for (const [card, what] of [[stepCard, "the step that still stands"], [blockedCard, "the card in Blocked and waiting"]]) {
      assert.equal(await veiled(card), false, `${what} was veiled beside the step that left`);
      assert.equal(await inert(card), false, `${what} was inert beside the step that left`);
    }
    assert.equal(await line("#next"), "catch-up-line", "Next up showed no moving line while the page caught up");
    if (address === "/") {
      assert.equal(await line("#sidebar .sidebar-group.queue .sidebar-list"), "catch-up-line", "the sidebar's queue showed no moving line");
      assert.equal(await sidebarInert(), false, "the sidebar's queue was inert beside the step that left");
    }
    assert.deepEqual(await nextWaits(), { busy: false, inert: false }, "Next up waited though its card and the queue's top stand");
    // The grace passes without veiling the rest: the quick list answered.
    await delay(GRACE + 500);
    assert.equal(await catching(), true, "the page stopped catching up before its state came");
    assert.equal(await everVeiled(), false, "the page veiled every card after the quick list named the step that left");
    open();
    await page.waitForFunction(() => !document.body.classList.contains("catching-up"));
    await goneCard.waitFor({ state: "detached" });
    await comesBack(blockedShown, "the card in Blocked and waiting did not come back");
    assert.equal(await page.evaluate(() => [...document.querySelectorAll("[inert]")].length), 0, "a part stayed inert after the page was current");
    await page.unroute("**/api/state**");
    await delay(1500);
    assert.equal((await noted()).length, flashesBefore, "the step that left while the tab was away was noted as a flash");

    // Away for longer, while a step left and the quick list is slow: while it is asked, the cards stay as drawn and in
    // use, and Next up's keys work. Once the grace passes without its answer, every part is veiled and inert, and Next
    // up's keys wait. Its answer then narrows the veil to the step that left.
    const late = await queued(`${name}-late`);
    await stepRow(late.id).waitFor();
    await everVeiled();
    let openListed;
    let openSlow;
    const listedGate = new Promise((resolve) => { openListed = resolve; });
    const slowGate = new Promise((resolve) => { openSlow = resolve; });
    await page.route("**/api/state**", async (route) => { await slowGate; await route.continue(); });
    await page.route("**/api/listed**", async (route) => { await listedGate; await route.continue(); });
    await page.evaluate(() => window.setShown(false));
    await delay(6000);
    await withdrawStep(late.id);
    // The page is read in the same call that shows it, before any timer or answer can run, so a slow host cannot
    // carry these checks past the grace. R goes to the page's key handler as a key press would.
    const asked = await page.evaluate(({ lateRow, blockedRow }) => {
      window.setShown(true);
      window.backAt = performance.now();
      const blurred = (one) => {
        for (let at = document.querySelector(one); at; at = at.parentElement) if (getComputedStyle(at).filter.includes("blur")) return true;
        return false;
      };
      const next = document.getElementById("next");
      const seen = {
        catching: document.body.classList.contains("catching-up"),
        waiting: document.querySelectorAll("[inert], .catch-up-gone").length,
        veiledAll: document.body.classList.contains("catch-up-veiled"),
        noted: document.body.classList.contains("catch-up-noted"),
        next: { busy: next.getAttribute("aria-busy") === "true", inert: next.inert },
        lateVeiled: blurred(lateRow),
        blockedVeiled: blurred(blockedRow),
      };
      document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "r", bubbles: true, cancelable: true }));
      seen.replyOpen = Boolean(document.activeElement?.closest("#next"));
      document.activeElement?.blur();
      return seen;
    }, { lateRow: rowShown(late.id), blockedRow: blockedShown });
    assert.equal(asked.catching, true, "a page back after six seconds away did not catch up");
    assert.equal(asked.waiting, 0, "a card or part waited while the quick list was asked");
    assert.equal(asked.veiledAll, false, "the page veiled its cards before the grace passed");
    assert.equal(asked.noted, false, "the page showed the moving line before the quick list answered");
    assert.deepEqual(asked.next, { busy: false, inert: false }, "Next up waited while the quick list was asked");
    assert.equal(asked.lateVeiled, false, "the step that left was veiled while the quick list was asked");
    assert.equal(asked.blockedVeiled, false, "the card in Blocked and waiting was veiled while the quick list was asked");
    // R opens Next up's reply box, as Next up's keys do while it is in use.
    assert.equal(asked.replyOpen, true, "R did not open Next up's reply box while the quick list was asked");
    assert.equal(await everVeiled(), false, "the page veiled its cards before the grace passed");
    await waitVeiled(blockedShown, GRACE + 6000, "Blocked and waiting was not veiled once the grace passed without the quick list");
    const graced = await page.evaluate(() => window.veiledAt - window.backAt);
    assert.ok(graced >= GRACE - 500, `the page veiled its cards ${Math.round(graced)} ms after the return, before the grace passed`);
    assert.equal(await bodyHas("catch-up-veiled"), true, "the page did not veil every part once the grace passed");
    assert.equal(await veiled(stepCard), true, "the step that still stands was not veiled once the grace passed");
    assert.equal(await line("#next"), "catch-up-line", "Next up showed no moving line while veiled");
    assert.deepEqual(await nextWaits(), { busy: true, inert: true }, "Next up did not wait once the grace passed");
    if (address === "/") assert.equal(await sidebarInert(), true, "the sidebar's queue was not inert once the grace passed");
    await page.locator("body").press("e");
    await page.locator("#toast", { hasText: "Updating" }).waitFor();
    // The quick list's late answer names the step that left: only its card stays veiled.
    openListed();
    await page.waitForFunction(() => !document.body.classList.contains("catch-up-veiled"), null, { timeout: 3000 })
      .catch(() => assert.fail("the quick list's late answer did not lift the veil from the cards that stand"));
    assert.equal(await catching(), true, "the quick list's late answer ended the catch-up though a step left");
    assert.equal(await bodyHas("catch-up-noted"), true, "the moving line went with the quick list's late answer");
    assert.equal(await markedGone(stepRow(late.id)), true, "the step that left was not marked gone after the quick list's late answer");
    assert.deepEqual(await nextWaits(), { busy: false, inert: false }, "Next up still waited after the quick list's late answer");
    if (address === "/") assert.equal(await sidebarInert(), false, "the sidebar's queue stayed inert after the quick list's late answer");
    await comesBack(blockedShown, "the card in Blocked and waiting stayed veiled after the quick list's late answer");
    assert.equal(await inert(blockedCard), false, "the card in Blocked and waiting stayed inert after the quick list's late answer");
    assert.equal(await inert(stepCard), false, "the step that still stands stayed inert after the quick list's late answer");
    openSlow();
    await page.waitForFunction(() => !document.body.classList.contains("catching-up"));
    await stepRow(late.id).waitFor({ state: "detached" });
    assert.equal(await page.evaluate(() => [...document.querySelectorAll("[inert]")].length), 0, "a part stayed inert after the page was current");
    await page.unroute("**/api/state**");
    await page.unroute("**/api/listed**");

    // Away for longer while nothing shown left: the quick list says so, and the cards show before the state comes,
    // never veiled.
    await everVeiled();
    let openQuiet;
    const quietGate = new Promise((resolve) => { openQuiet = resolve; });
    await page.route("**/api/state**", async (route) => { await quietGate; await route.continue(); });
    await page.evaluate(() => window.setShown(false));
    await delay(6000);
    await page.evaluate(() => window.setShown(true));
    await page.waitForFunction(() => !document.body.classList.contains("catching-up"), null, { timeout: GRACE })
      .catch(() => assert.fail("a page back with nothing gone waited for the full state"));
    assert.equal(await seen(blockedCard), true, "the card in Blocked and waiting stayed out of sight with nothing gone");
    assert.equal(await everVeiled(), false, "a page back with nothing gone veiled its cards");
    assert.equal(await page.evaluate(() => document.getElementById("next").hasAttribute("aria-busy")), false);
    const stateCame = page.waitForResponse((response) => response.url().includes("/api/state"));
    openQuiet();
    await stateCame;
    await page.unroute("**/api/state**");

    // A state Cockpit refuses shows the cards again (the banner says they are old). The quick list is held back: its
    // answer that nothing left would end the catch-up before the state fails, and the banner would then wait.
    let openRefused;
    const refusedGate = new Promise((resolve) => { openRefused = resolve; });
    await page.route("**/api/listed**", async (route) => { await refusedGate; await route.continue(); });
    await page.route("**/api/state**", (route) => route.fulfill({ status: 500, contentType: "application/json", body: "{\"detail\": \"test refusal\"}" }));
    await page.evaluate(() => window.setShown(false));
    await delay(6000);
    await page.evaluate(() => window.setShown(true));
    await page.waitForFunction(() => !document.body.classList.contains("catching-up"), null, { timeout: 5000 });
    await comesBack(blockedShown, "the cards stayed out of sight after the state failed");
    await page.locator("#banner", { hasText: "Cockpit could not refresh" }).waitFor({ timeout: 5000 });
    openRefused();
    await page.unroute("**/api/state**");
    await page.unroute("**/api/listed**");

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
    await waitVeiled(blockedShown, GRACE + 6000, "Blocked and waiting was not veiled while the page caught up beside a box in use");
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
    console.log(`PASS ${address}: not after a second away; back after a poll's time away with a step gone, only its card is veiled until the state comes and Next up stays in use; while the quick list is asked the cards stay in use and Next up's keys work, past the grace every part is veiled and Next up's keys wait, and the late answer narrows the veil to the step that left; nothing gone shows the cards before the state, never veiled; a box in use stays; a failed state shows the cards under the banner; a tab hidden for half a minute is drawn current and veils nothing on return`);
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
