// Close these tasks (SPEC CONTROL-23). On a synthetic database, at 1440 px, on the page with the list at its left:
// - the list at the left has no key of its own that closes finished tasks;
// - a session's step that lists tasks to close shows each with the session's why, all ticked;
// - a tick keeps keyboard focus when Space toggles it, and an unticked task stays open;
// - one press on the plum key with the stack icon closes the ticked tasks and the step, with no notice at the foot;
// - the Undo sits on Finished this week's heading, and reopens the tasks and the step, also when pressed
//   before the close answered;
// - a finish step with a list shows no Done, Task done or Archive session key, lists its own task first, and one
//   press closes the listed task and its own;
// - no page errors.
// Saves screenshots of both steps at 1440 and 390 px in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/close_finished.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder, screenshotRedrawn } = require("./work.cjs");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// The harness's agent token (tests/conftest.py TOKEN) and its fake session that reports its work done (SESSION_A).
const AGENT_TOKEN = "t".repeat(40);
const AGENT = "check-close-finished";
const SESSION = "11111111-1111-4111-8111-111111111111";

async function main() {
  const work = workFolder("cockpit-close-finished-");
  const server = spawn(path.join(__dirname, "..", "..", ".venv", "bin", "python"), [path.join(__dirname, "serve_preview.py"), work],
    { stdio: ["ignore", "inherit", "inherit"] });
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const info = await harnessUp(work, server);
    const user = { cookie: `${info.name}=${info.value}`, origin: info.origin, "content-type": "application/json" };
    const agent = { authorization: `Bearer ${AGENT_TOKEN}`, "x-cockpit-as": AGENT, "content-type": "application/json" };
    const call = async (method, url, headers, body) => {
      const response = await fetch(`${info.origin}${url}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
      const text = await response.text();
      if (!response.ok) throw new Error(`${method} ${url}: HTTP ${response.status}: ${text}`);
      return JSON.parse(text);
    };
    const itemOf = (key) => call("GET", `/api/items?key=${encodeURIComponent(key)}`, agent);
    const stageOf = async (key) => (await itemOf(key)).stage;
    // A with a finish step, B with no step at all, C to untick; L is the cleanup session's own item.
    const keys = { a: "note:check-close-a", b: "note:check-close-b", c: "note:check-close-c", l: "note:check-close-list", f: "note:check-close-export" };
    const titles = { a: "Tasks weekly reminders", b: "Tasks list sharing", c: "Tasks CSV export", l: "Tasks finished work", f: "Tasks export rework" };
    const reasons = { a: "Sam receives a reminder every Monday", b: "Alex can open the shared grocery list", c: "The CSV contains completed tasks" };
    const ids = {};
    for (const name of Object.keys(keys)) {
      ids[name] = (await call("PUT", "/api/items", agent, { key: keys[name], title: titles[name], stage: "building", sessions: [SESSION] })).id;
    }
    await call("POST", "/api/steps", agent, { item_key: keys.a, need: "complete", ask: "Is this task done?" });
    const list = async () => (await call("POST", "/api/steps", agent, {
      item_key: keys.l, need: `close:check-${Date.now()}`, ask: "Close these three finished tasks?",
      closes: ["a", "b", "c"].map((name) => ({ item_id: ids[name], why: reasons[name] })),
    })).step;
    let listing = await list();

    browser = await chromium.launch({ executablePath: CHROMIUM });
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    await context.setExtraHTTPHeaders({ cookie: user.cookie });
    const page = await context.newPage();
    page.setDefaultTimeout(8000);
    page.setDefaultNavigationTimeout(60000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`${info.origin}/`);
    await page.locator("#sidebar").waitFor({ timeout: 20000 });
    check("the list has no key that closes finished tasks", (await page.locator("#sidebar .close-many-key").count()) === 0, "found one");

    const open = async (step, itemId = ids.l) => {
      await page.waitForFunction((id) => typeof selectItem === "function" && ui.state && ui.state.items.some((item) => item.id === id), itemId, { timeout: 20000 });
      await page.evaluate((id) => selectItem(id), itemId);
      const card = page.locator(`#next [data-step="${step.id}"]`);
      await card.locator(".close-list").waitFor();
      return card;
    };
    let card = await open(listing);
    const rows = card.locator(".close-list-row");
    const shown = await rows.locator(".close-list-title").allTextContents();
    check("the step lists each task", JSON.stringify(shown) === JSON.stringify([titles.a, titles.b, titles.c]), JSON.stringify(shown));
    const why = await rows.locator(".close-list-why").allTextContents();
    check("each task shows the session's why", JSON.stringify(why) === JSON.stringify([reasons.a, reasons.b, reasons.c]), JSON.stringify(why));
    const ticks = await rows.locator("input[type=checkbox]").evaluateAll((boxes) => boxes.map((box) => box.checked));
    check("all start ticked", ticks.every(Boolean), JSON.stringify(ticks));
    const go = card.locator(".close-list-go");
    check("the plum key has the stack icon", (await go.locator("svg").count()) === 1, "no icon");
    const fill = await go.evaluate((el) => getComputedStyle(el).backgroundColor);
    const plain = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
    check("the key is filled, not plain", fill !== plain && fill !== "rgba(0, 0, 0, 0)", fill);
    const box = await go.boundingBox();
    check("the plum key is a full-size key", box && box.height >= 28, JSON.stringify(box));
    await screenshotRedrawn(card, { path: path.join(work, "close-list-1440.png") });
    console.error(`  screenshots in ${work}`);

    const tickC = rows.filter({ hasText: titles.c }).locator("input[type=checkbox]");
    await tickC.focus();
    await page.keyboard.press("Space");
    const focus = await page.evaluate(() => ({ tag: document.activeElement.tagName, key: document.activeElement.dataset.key || "", checked: document.activeElement.checked }));
    check("a tick keeps focus when Space toggles it", focus.tag === "INPUT" && focus.key.startsWith("closes:") && !focus.checked, JSON.stringify(focus));
    check("the plum key names how many close", (await go.textContent()).trim() === "Close 2 tasks", await go.textContent());
    await go.click({ timeout: 20000 });
    // Finished this week starts folded, so the close's Undo sits on its heading.
    const undo = page.locator("#sidebar .sidebar-group.done > summary button.busy-undo");
    check("Finished this week's heading offers Undo", await undo.waitFor().then(() => true, () => false), "no Undo on the heading");
    check("no Closed! notice shows at the foot", await page.evaluate(() => {
      const el = document.getElementById("toast");
      return el.hidden || !el.classList.contains("closed");
    }), await page.locator("#toast").innerHTML());
    const settle = async (want) => {
      let stages = [];
      for (let tries = 0; tries < 40; tries += 1) {
        stages = await Promise.all([keys.a, keys.b].map(stageOf));
        if (stages.every((stage) => stage === want)) break;
        await sleep(250);
      }
      return stages;
    };
    let stages = await settle("done");
    check("one press closes both ticked tasks", stages.every((stage) => stage === "done"), JSON.stringify(stages));
    check("the unticked task stays open", (await stageOf(keys.c)) === "building", await stageOf(keys.c));
    const stepOf = async (step) => (await itemOf(keys.l)).steps.find((one) => one.id === step.id);
    check("the press closes the step", (await stepOf(listing)).status === "done", (await stepOf(listing)).status);
    check("the step leaves the page", (await page.locator(`#next [data-step="${listing.id}"]`).count()) === 0, "still shown");

    await undo.click();
    stages = await settle("building");
    check("Undo reopens both", stages.every((stage) => stage === "building"), JSON.stringify(stages));
    let back = null;
    for (let tries = 0; tries < 20 && (!back || back.status !== "open"); tries += 1) { back = await stepOf(listing); await sleep(200); }
    check("Undo reopens the step", back.status === "open", back.status);

    // Undo pressed before the close answered: the close goes through, then the tasks and the step reopen.
    await undo.waitFor({ state: "detached", timeout: 20000 }).catch(() => {});
    await page.route("**/close-tasks", async (route) => { await sleep(1500); await route.continue(); });
    card = await open(listing);
    await card.locator(".close-list-row").filter({ hasText: titles.c }).locator("input[type=checkbox]").uncheck();
    await card.locator(".close-list-go").click({ timeout: 20000 });
    await undo.click();
    await sleep(1500);
    stages = await settle("building");
    check("an Undo pressed before the close answered reopens both", stages.every((stage) => stage === "building"), JSON.stringify(stages));
    back = null;
    for (let tries = 0; tries < 20 && (!back || back.status !== "open"); tries += 1) { back = await stepOf(listing); await sleep(200); }
    check("and the step", back.status === "open", back.status);
    await page.unroute("**/close-tasks");
    check("no page errors", errors.length === 0, errors.join(" | "));

    await page.setViewportSize({ width: 390, height: 900 });
    await sleep(500);
    if (await open(listing).then(() => true, () => false)) {
      await sleep(500);
      await page.screenshot({ path: path.join(work, "close-list-390.png") });
    }

    // A finish step with a list: its one close key, the list's, also completes its own task (CONTROL-23).
    await page.setViewportSize({ width: 1440, height: 1000 });
    ids.f = (await call("PUT", "/api/items", agent, { key: keys.f, title: titles.f, stage: "building", sessions: [SESSION] })).id;
    const finishing = (await call("POST", "/api/steps", agent, {
      item_key: keys.f, need: "complete", ask: "Is the export done, and the CSV task with it?",
      closes: [{ item_id: ids.c, why: reasons.c }],
    })).step;
    card = await open(finishing, ids.f);
    check("a finish card with a list shows no Archive session", (await card.locator(".archive-key").count()) === 0, "found one");
    check("nor a Done or Task done key", (await card.locator(".actions .step-keys, .actions .task-done").count()) === 0, "found one");
    const own = card.locator(".close-list-row.own");
    check("its own task heads the list", (await own.locator(".close-list-why").textContent()) === "This task", await own.textContent());
    check("the key counts the own task", (await card.locator(".close-list-go").textContent()).trim() === "Close 2 tasks", await card.locator(".close-list-go").textContent());
    await screenshotRedrawn(card, { path: path.join(work, "close-finish-1440.png") });
    await page.setViewportSize({ width: 390, height: 900 });
    await sleep(500);
    await page.screenshot({ path: path.join(work, "close-finish-390.png") });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await sleep(500);
    await card.locator(".close-list-go").click({ timeout: 20000 });
    let ends = [];
    for (let tries = 0; tries < 40 && !(ends.length && ends.every((stage) => stage === "done")); tries += 1) {
      ends = await Promise.all([keys.f, keys.c].map(stageOf));
      if (!ends.every((stage) => stage === "done")) await sleep(250);
    }
    check("one press closes the listed task and the card's own", ends.every((stage) => stage === "done"), JSON.stringify(ends));
    check("no page errors on the finish card", errors.length === 0, errors.join(" | "));
    await context.close();
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failedChecks = results.filter((result) => !result.ok);
  console.error(failedChecks.length ? `${failedChecks.length} of ${results.length} checks failed` : `all ${results.length} checks passed`);
  process.exitCode = failedChecks.length ? 1 : 0;
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
