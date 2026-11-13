// After Send on the page with the list at its left, the Undo sits in Busy, not on a notice at the foot. On a synthetic
// database, at 1440 px:
// - Send shows no notice at the foot;
// - Busy, folded, lights up, its count rolls up by one, and an Undo sits at its heading's right;
// - while the Undo is on offer the message waits (no release request);
// - that Undo takes the step back to the top of the queue with its words in the reply box, the message unsent, and Busy
//   stays folded;
// - with Busy open, the step's row is the Undo, and it takes the step back;
// - once the Undo time ends the heading is plain again and the message goes to the session then (its release);
// - no page errors.
// Saves screenshots of the heading and the row, light and dark, in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/send_undo_busy.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// The harness's agent token (tests/conftest.py TOKEN) and its second fake session, which the message goes to.
const AGENT_TOKEN = "t".repeat(40);
const SESSION = "22222222-2222-4222-8222-222222222222";

async function main() {
  const work = workFolder("cockpit-send-undo-");
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
    const agent = { authorization: `Bearer ${AGENT_TOKEN}`, "x-cockpit-as": "send-undo-test", "content-type": "application/json" };
    const call = async (method, url, headers, body) => {
      const response = await fetch(`${info.origin}${url}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
      const text = await response.text();
      if (!response.ok) throw new Error(`${method} ${url}: HTTP ${response.status}: ${text}`);
      return JSON.parse(text);
    };
    // The fixture database's open steps are snoozed, so this run's step is the queue's first.
    const initial = await call("GET", "/api/state", user);
    await Promise.all([...initial.queue, ...initial.ready_next, ...initial.snoozed].map((step) => call("POST", `/api/steps/${step.id}/snooze`, user, { until: "2099-01-01T08:00:00Z" })));
    const key = `note:send-undo-${Date.now()}`;
    const title = "Send undo test";
    const itemId = (await call("PUT", "/api/items", agent, { key, title, stage: "building", priority: "urgent", sessions: [SESSION] })).id;
    const step = (await call("POST", "/api/steps", agent, { item_key: key, need: "decide:send-undo", ask: "Decide the send undo test", score: 100, session_id: SESSION })).step;
    const stateNow = () => call("GET", "/api/state", user);
    const settle = async (want) => {
      let state = null;
      for (let tries = 0; tries < 40; tries += 1) {
        state = await stateNow();
        if (want(state)) break;
        await sleep(250);
      }
      return state;
    };
    const backInQueue = (state) => state.queue.some((one) => one.id === step.id) && !state.with_agents.some((one) => one.id === step.id);

    browser = await chromium.launch({ executablePath: CHROMIUM });
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 2 });
    await context.setExtraHTTPHeaders({ cookie: user.cookie });
    const page = await context.newPage();
    page.setDefaultTimeout(8000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const releases = [];
    page.on("request", (request) => { if (/\/api\/messages\/\d+\/release$/.test(request.url())) releases.push(Date.now()); });
    const released = [];
    page.on("response", (response) => { if (/\/api\/messages\/\d+\/release$/.test(response.url())) released.push(response.status()); });
    await page.goto(`${info.origin}/`);
    await page.locator("#sidebar .sidebar-groups").waitFor({ timeout: 60000 });

    const busy = page.locator("#sidebar .sidebar-group.busy");
    const heading = busy.locator(":scope > summary");
    const headingUndo = heading.locator("button.busy-undo");
    // The notice after Send read "Sent to …" with Take back; a "Taken back" notice from the Undo before may still show.
    const toastShown = () => page.evaluate(() => {
      const el = document.getElementById("toast");
      return !el.hidden && (/^Sent to /.test(el.textContent) || [...el.querySelectorAll("button")].some((b) => b.textContent === "Take back"));
    });
    const send = async (words) => {
      await page.waitForFunction((id) => ui.state && ui.state.queue.some((one) => one.id === id), step.id, { timeout: 20000 });
      await page.evaluate((id) => selectItem(id), itemId);
      const card = page.locator(`#next [data-step="${step.id}"]`).first();
      const box = card.locator(`textarea[data-key="msg:step:${step.id}"]`);
      await box.waitFor();
      await box.fill(words);
      const at = Date.now();
      await card.locator(".composer:not(.feedback-box) button.send").first().click();
      return at;
    };
    // Opens or folds Busy as the user does, with a click on its name: the page remembers the choice on that click.
    // Setting `open` alone is remembered on the later toggle event, and a redraw before it draws the group as it was.
    // The click runs inside the page, in one turn of its script, so no redraw comes between reading and clicking.
    const setOpen = (open) => busy.evaluate((el, want) => { if (el.open !== want) el.querySelector(":scope > summary .sidebar-group-name").click(); }, open);
    const wordsBack = (words) => page.waitForFunction(([key, words]) => document.querySelector(`#next textarea[data-key="${key}"]`)?.value === words,
      [`msg:step:${step.id}`, words], { timeout: 20000 }).then(() => true, () => false);

    // 1. Send with Busy folded: no notice; Busy's heading lights up with the Undo.
    await page.locator("#sidebar .sidebar-group.busy").waitFor({ timeout: 20000 }).catch(() => {});
    if (await busy.count()) await setOpen(false);
    const first = "First words for the undo test";
    await send(first);
    await headingUndo.waitFor();
    check("Send shows no notice at the foot", !(await toastShown()), await page.locator("#toast").textContent());
    check("Busy's heading lights up", await heading.evaluate((el) => el.classList.contains("holding")), await heading.getAttribute("class"));
    const counts = await heading.locator(".count-roll").evaluate((el) => [el.querySelector(".count-was").textContent, el.querySelector(".count-now").textContent]);
    check("its count rolls up by one", Number(counts[1]) === Number(counts[0]) + 1, JSON.stringify(counts));
    check("the Undo names who the message goes to", /^Take back your message to /.test(await headingUndo.getAttribute("aria-label")), await headingUndo.getAttribute("aria-label"));
    check("the message waits while the Undo is on offer", releases.length === 0, `${releases.length} releases`);
    await headingUndo.click();
    let state = await settle(backInQueue);
    check("the heading's Undo takes the step back to the queue", backInQueue(state), "not back");
    check("its words are back in the reply box", await wordsBack(first), "not back");
    check("Busy stays folded", !(await busy.evaluate((el) => el.open)), "it opened");
    check("the heading's Undo is gone", (await headingUndo.count()) === 0, "still shown");
    const message = state.messages.find((one) => one.step_id === step.id);
    check("Cockpit did not send the message", releases.length === 0 && message && message.state === "failed", JSON.stringify(message && message.state));

    // 2. With Busy open, the step's row is the Undo.
    await setOpen(true);
    const second = "Second words for the undo test";
    await send(second);
    const row = busy.locator(`li.held-hand[data-step="${step.id}"] > button.undo-add`);
    await row.waitFor();
    check("the open group's heading shows no Undo", !(await headingUndo.isVisible().catch(() => false)), "visible");
    check("the row names the task", (await row.locator(".sidebar-title").textContent()) === title, await row.textContent());
    check("Send shows no notice with Busy open either", !(await toastShown()), await page.locator("#toast").textContent());
    const shot = async (file) => {
      for (let tries = 0; ; tries += 1) {
        try { return await row.screenshot({ path: path.join(work, file), timeout: 2000 }); } catch (error) { if (tries >= 3) throw error; }
      }
    };
    await shot("row-light.png");
    await page.emulateMedia({ colorScheme: "dark" });
    await sleep(200);
    await shot("row-dark.png");
    await page.emulateMedia({ colorScheme: "light" });
    await row.click();
    state = await settle(backInQueue);
    check("the row's Undo takes the step back", backInQueue(state), "not back");
    check("its words are back too", await wordsBack(second), "not back");

    // 3. The Undo time ends: plain heading; the message goes only then.
    await setOpen(false);
    const pressedAt = await send("Third words, which go");
    await headingUndo.waitFor();
    // The pictures of the heading are taken here, where no press has to land within the Undo time: on a slow machine
    // they take seconds. The count has finished rolling after 1.8 s.
    await sleep(1800);
    await page.screenshot({ path: path.join(work, "page-1440.png") });
    await heading.screenshot({ path: path.join(work, "heading-light.png") });
    await page.emulateMedia({ colorScheme: "dark" });
    await sleep(200);
    await heading.screenshot({ path: path.join(work, "heading-dark.png") });
    await page.emulateMedia({ colorScheme: "light" });
    await sleep(Math.max(0, pressedAt + 4000 - Date.now()));
    check("the message still waits after 4 s", releases.every((at) => at - pressedAt >= 4000), `released after ${releases[0] - pressedAt} ms`);
    await headingUndo.waitFor({ state: "detached", timeout: 12000 });
    check("after the Undo time the heading is plain", await heading.evaluate((el) => !el.classList.contains("holding")), await heading.getAttribute("class"));
    for (let tries = 0; tries < 20 && !releases.length; tries += 1) await sleep(250);
    check("then the message goes to the session", releases.length === 1 && releases[0] - pressedAt >= 7000, JSON.stringify(releases.map((at) => at - pressedAt)));
    for (let tries = 0; tries < 20 && !released.length; tries += 1) await sleep(250);
    check("Cockpit took the release", released.length === 1 && released[0] === 200, JSON.stringify(released));
    state = await stateNow();
    check("the step stays handed", state.with_agents.some((one) => one.id === step.id), "not with agents");
    await setOpen(true);
    const plainRow = busy.locator(`li[data-step="${step.id}"]`);
    await plainRow.waitFor();
    check("the step's row in Busy is plain again", !(await plainRow.evaluate((el) => el.classList.contains("held-hand"))), await plainRow.getAttribute("class"));

    // 4. An item with another step in Inbox: the list draws the item there, not in Busy, so the notice offers Take back.
    // The other step is a permission ask: an item shows one card at a time (SPEC NEXT-55), and a permission ask is not
    // a card of its own.
    const key2 = `${key}-two`;
    const itemId2 = (await call("PUT", "/api/items", agent, { key: key2, title: "Send undo two steps", stage: "building", priority: "urgent", sessions: [SESSION] })).id;
    const other = (await call("POST", "/api/steps", agent, {
      item_key: key2, need: "allow:send-undo-other", ask: "Let the session list the other part", score: 90, session_id: SESSION,
      permission: { list: "allow", rule: "Bash(ls /tmp/send-undo-other *)" },
    })).step;
    const step2 = (await call("POST", "/api/steps", agent, { item_key: key2, need: "decide:send-undo-2", ask: "Decide the send undo test again", score: 100, session_id: SESSION })).step;
    await page.waitForFunction(([a, b]) => ui.state && [a, b].every((id) => ui.state.queue.some((one) => one.id === id)), [other.id, step2.id], { timeout: 20000 });
    await page.evaluate((id) => selectItem(id), itemId2);
    const card2 = page.locator(`#next [data-step="${step2.id}"]`).first();
    const box2 = card2.locator(`textarea[data-key="msg:step:${step2.id}"]`);
    const shownCard = await box2.waitFor({ timeout: 8000 }).then(() => true, () => false);
    if (shownCard) {
      await box2.fill("Words for the step whose item stays in Inbox");
      await card2.locator(".composer:not(.feedback-box) button.send").first().click();
      const notice = page.locator("#toast button", { hasText: "Take back" });
      check("with another step of the item in Inbox, the notice offers Take back", await notice.waitFor().then(() => true, () => false), await page.locator("#toast").textContent());
      await notice.click().catch(() => {});
      // 5. Another step of the item already with an agent: Busy draws that one's row, so the notice offers Take back.
      await settle((state) => state.queue.some((one) => one.id === step2.id));
      await call("POST", `/api/steps/${other.id}/hand-off`, user, { text: "Handed first", session_id: SESSION });
      await page.waitForFunction((id) => ui.state && ui.state.with_agents.some((one) => one.id === id), other.id, { timeout: 20000 });
      await page.evaluate(() => { const el = document.getElementById("toast"); el.hidden = true; });
      await page.evaluate((id) => selectItem(id), itemId2);
      await box2.waitFor();
      await box2.fill("Words for the step whose sibling is handed");
      await card2.locator(".composer:not(.feedback-box) button.send").first().click();
      check("with another step of the item handed already, the notice offers Take back",
        await notice.waitFor().then(() => true, () => false), await page.locator("#toast").textContent());
      await notice.click().catch(() => {});
    } else {
      check("with another step of the item in Inbox, the notice offers Take back", false, "the step's card did not show");
    }
    check("no page errors", errors.length === 0, errors.join(" | "));
    console.error(`  screenshots in ${work}`);
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
  const end = Date.now() + 120e3;
  while (Date.now() < end) {
    if (server.exitCode !== null) throw new Error(`serve_preview.py exited with ${server.exitCode}`);
    if (fs.existsSync(file)) {
      const info = JSON.parse(fs.readFileSync(file, "utf8"));
      const up = await fetch(`${info.origin}/healthz`).then((response) => response.ok, () => false);
      if (up) return info;
    }
    await sleep(200);
  }
  throw new Error("serve_preview.py did not come up within 120 s");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
