// After a close, Next up shows the queue's first step by rank and keeps it, also while the server is slow to answer
// (SPEC NEXT-16). On a copy of the live database, each round snoozes the steps there, puts four queued steps, the first
// a finish step on an item of its own, and a fifth, higher-scored step that waits in Ready next because no queued step
// is more than 10 below it. It starts the first step's timer, presses Task done and checks:
// - Next up shows the step that ranks first once the freed place is filled, and no other, until the close's answer came
//   and the page polled: the step that waited, which takes the freed place;
// - the page asks the server to hold that step: before the close's answer comes when the step had a place already
//   (round three), and within a few seconds after it when the step takes the freed place, which it has only then;
// - Next up shows no Resorted key, since nothing ranks above it;
// - the close re-sorts the queue (SPEC RANK-19): a sixth step, scored 72, waits within 10 of the lowest queued step it
//   can replace and takes that step's place with the close, and the list at the left changes once from the press to the
//   server's answer and after.
// Each round also withdraws the blocked calls the copy holds, which Next up would show first.
// Round one keeps the page focused, so the next step's timer starts too, also before the close's answer, and runs on
// the server after it. Round two takes the page's focus away right after the press, so no timer starts. Round three,
// focused, has the user open the second queued step before a higher step joins the queue below it; Next up then shows
// the higher step.
//
// Starts serve_slow.py (this checkout's Cockpit on a copy of the live database, every non-GET /api/ request held 2 s;
// README.md says what it needs) and drives a headless Chromium at 1280 px on the page at /. Prints one line per check
// and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/close_next_holds.cjs
"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

// Browser dependencies can be selected through the environment (README.md).
const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const SESSION_B = "22222222-2222-4222-8222-222222222222";
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "close-next-holds", "content-type": "application/json" };

async function main() {
  const work = workFolder("cockpit-close-next-");
  const server = spawn(path.join(__dirname, "..", "..", ".venv", "bin", "python"), [path.join(__dirname, "serve_slow.py"), work],
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
    const call = async (who, method, url, body) => {
      const response = await fetch(`${info.origin}${url}`, { method, headers: who, body: body === undefined ? undefined : JSON.stringify(body) });
      if (!response.ok) throw new Error(`${method} ${url}: HTTP ${response.status} ${await response.text()}`);
      return response.json();
    };
    browser = await chromium.launch({ executablePath: CHROMIUM });
    for (const [round, focused, opened] of [["focused", true, false], ["unfocused", false, false], ["opened", true, true]]) {
      await closeRound({ browser, info, user, call, check, work, round, focused, opened });
    }
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failed = results.filter((result) => !result.ok).length;
  console.error(`${results.length - failed}/${results.length} checks pass`);
  process.exitCode = failed ? 1 : 0;
}

async function closeRound({ browser, info, user, call, check, work, round, focused, opened }) {
  const named = (words) => `${round}: ${words}`;
  const copied = await call(user, "GET", "/api/state");
  // Steps the copy has snoozed already are snoozed for longer too, so none wakes during the round.
  await Promise.all([...copied.queue, ...copied.ready_next, ...copied.snoozed].map((step) => call(user, "POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
  // A blocked call seeded in the fixture database would show on Next up above the queue.
  await Promise.all(copied.permission_requests.map((request) =>
    call(AGENT, "POST", `/api/permissions/requests/${request.id}/withdraw`, { session_id: request.session_id })));
  const stamp = `${round}-${Date.now()}`;
  const keys = { finish: `note:close-next-finish-${stamp}`, work: `note:close-next-work-${stamp}`, late: `note:close-next-late-${stamp}`,
    jump: `note:close-next-jump-${stamp}` };
  await call(AGENT, "PUT", "/api/items", { key: keys.finish, title: "Weekly reminder selector", stage: "prod", sessions: [SESSION_B] });
  await call(AGENT, "PUT", "/api/items", { key: keys.work, title: "Shared list exports", stage: "building", sessions: [SESSION_B] });
  await call(AGENT, "PUT", "/api/items", { key: keys.late, title: "Reply to Alex", stage: "building", sessions: [SESSION_B] });
  await call(AGENT, "PUT", "/api/items", { key: keys.jump, title: "Export button disabled", stage: "building", sessions: [SESSION_B] });
  const put = async (body) => (await call(AGENT, "POST", "/api/steps", { session_id: SESSION_B, ...body })).step;
  const finish = await put({ item_key: keys.finish, need: "complete", ask: "Weekly reminders are ready: confirm it is done", score: 90 });
  const second = await put({ item_key: keys.work, need: "n1", ask: "Show shared lists in exports?", score: 70 });
  await put({ item_key: keys.work, need: "n2", ask: "Export completed tasks too?", score: 69 });
  // Round three puts the rest once the user opened the second step.
  const rest = async () => {
    const jump = opened ? await put({ item_key: keys.jump, need: "fix", ask: "Enable the export button?", score: 85 }) : null;
    await put({ item_key: keys.work, need: "n3", ask: "Include reminder dates in exports?", score: 68 });
    const late = await put({ item_key: keys.late, need: "reply", ask: "Post this reply to Alex?", score: 75 });
    const resort = await put({ item_key: keys.late, need: "thanks", ask: "Thank Alex for the review?", score: 72 });
    const before = await call(user, "GET", "/api/state");
    check(named("the sixth step waits in Ready next"), before.ready_next.some((step) => step.id === resort.id),
      JSON.stringify(before.queue.map((step) => [step.id, step.score])));
    check(named("the fifth step waits in Ready next"), before.ready_next.some((step) => step.id === late.id) && !before.queue.some((step) => step.id === late.id),
      JSON.stringify(before.queue.map((step) => [step.id, step.score])));
    if (jump) check(named("the higher step joined the queue"), before.queue.some((step) => step.id === jump.id), JSON.stringify(before.queue.map((step) => step.id)));
    return { late, jump, resort };
  };
  let placed = opened ? null : await rest();

  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, colorScheme: "light" });
  await context.setExtraHTTPHeaders({ cookie: user.cookie });
  const page = await context.newPage();
  page.setDefaultTimeout(8000);
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  // When the close's answer comes and when the page asks to hold the next step, on the test's clock (the page's is the same).
  const times = { closeAnswered: null, holdAsked: null };
  // The step Next up should show after the close: the one that waited, or in round three the higher step.
  let expected = null;
  page.on("request", (request) => {
    if (expected && request.method() === "POST" && request.url().endsWith(`/api/steps/${expected.id}/hold`) && !times.holdAsked) times.holdAsked = Date.now();
  });
  page.on("response", (response) => {
    // Task done on an item's last step finishes the item (CONTROL-15).
    if (response.request().method() === "POST" && response.url().endsWith(`/api/items/${finish.item_id}/finish`)) times.closeAnswered = Date.now();
  });
  try {
    // The synthetic data takes a while to draw on a loaded host.
    await page.goto(`${info.origin}/`, { timeout: 60000 });
    await page.locator(`#next [data-step="${finish.id}"]`).waitFor({ timeout: 30000 });
    if (opened) {
      // The user opens the second step from the list, then comes back to the first; the higher step arrives after.
      await page.locator(`#sidebar li[data-item="${second.item_id}"]`).first().click();
      await page.waitForFunction((id) => ui.placed.has(id), second.id, { timeout: 10000 });
      await page.locator(`#sidebar li[data-item="${finish.item_id}"]`).first().click();
      await page.locator(`#next [data-step="${finish.id}"]`).waitFor({ timeout: 10000 });
      placed = await rest();
      // The page polled the higher step into its queue, below the step the user opened.
      await page.waitForFunction((id) => ui.order.includes(id), placed.jump.id, { polling: 500, timeout: 30000 });
      const order = await page.evaluate(() => ui.order);
      check(named("before the close, the opened step sits above the higher step"), order.indexOf(second.id) < order.indexOf(placed.jump.id), JSON.stringify(order));
    }
    expected = placed.jump || placed.late;
    // The user works on the finish step: its timer runs, on the page at once and on the server 2 s later.
    await page.evaluate((id) => setTimer(id, true), finish.id);
    await page.waitForFunction(async (id) => {
      const state = await (await fetch("/api/state", { credentials: "same-origin" })).json();
      return state.timer && state.timer.step_id === id;
    }, finish.id, { polling: 500, timeout: 20000 });
    times.closeAnswered = null;

    // From the press on, the page notes each step Next up shows and when a timer first runs on the page for another step.
    await page.mouse.move(400, 300);
    await page.evaluate((id) => {
      window.__seen = [];
      window.__orders = [JSON.stringify(ui.order)];
      window.__started = null;
      window.__looking = setInterval(() => {
        const shown = ui.next && ui.next.id;
        if (shown && shown !== id && window.__seen.at(-1) !== shown) window.__seen.push(shown);
        const order = JSON.stringify(ui.order);
        if (window.__orders.at(-1) !== order) window.__orders.push(order);
        const timer = drawn(ui.state).timer;
        if (!window.__started && timer && timer.step_id !== id) window.__started = { step: timer.step_id, at: Date.now() };
      }, 50);
    }, finish.id);
    // Round two: the user switches to another window with the press, so the page has no focus from then on (set
    // before the press, so the page's one-second tick cannot start a timer in between).
    if (!focused) await page.evaluate(() => { document.hasFocus = () => false; });
    await page.locator(`#next [data-step="${finish.id}"] .actions button.task-done`).first().click();
    // The close and the hold are through, and the page polled after them.
    await sleep(15000);
    const seen = await page.evaluate(() => {
      clearInterval(window.__looking);
      return { steps: window.__seen, started: window.__started, orders: window.__orders.map((order) => JSON.parse(order)) };
    });
    check(named("Next up shows the first step by rank and nothing else after the close"), seen.steps.length === 1 && seen.steps[0] === expected.id,
      `Next up showed ${JSON.stringify(seen.steps)}; the first step by rank is ${expected.id}`);
    check(named("the close's answer came"), Boolean(times.closeAnswered), "no answer seen");
    if (opened) {
      check(named("the page asks to hold that step before the close's answer comes"),
        Boolean(times.holdAsked) && Boolean(times.closeAnswered) && times.holdAsked < times.closeAnswered, JSON.stringify(times));
    } else {
      check(named("the page asks to hold that step once the server gives it the freed place"),
        Boolean(times.holdAsked) && Boolean(times.closeAnswered) && times.holdAsked >= times.closeAnswered && times.holdAsked - times.closeAnswered < 6000,
        JSON.stringify(times));
    }
    const after = await call(user, "GET", "/api/state");
    if (focused) {
      check(named("its timer starts on the page before the close's answer comes"),
        Boolean(seen.started) && seen.started.step === expected.id && Boolean(times.closeAnswered) && seen.started.at < times.closeAnswered,
        JSON.stringify({ ...seen.started, ...times }));
      check(named("its timer runs on the server"), Boolean(after.timer) && after.timer.step_id === expected.id, JSON.stringify(after.timer));
    } else {
      check(named("no timer starts while the page has no focus"), !seen.started && !after.timer, JSON.stringify({ page: seen.started, server: after.timer }));
    }
    check(named("the step that waited took the freed place"), after.queue.some((step) => step.id === placed.late.id), JSON.stringify(after.queue.map((step) => step.id)));
    check(named("the server's queue leads with the step Next up shows"), Boolean(after.queue[0]) && after.queue[0].id === expected.id, JSON.stringify(after.queue.map((step) => step.id)));
    const lowest = after.ready_next.find((step) => step.score < placed.resort.score);
    check(named("the close let the sixth step take a lower step's place"), after.queue.some((step) => step.id === placed.resort.id) && Boolean(lowest)
      && after.queue.length === 4, JSON.stringify({ queue: after.queue.map((step) => [step.id, step.score]), ready: after.ready_next.map((step) => [step.id, step.score]) }));
    const queued = after.queue.map((step) => step.id);
    check(named("the list at the left changes once, to the server's queue"), seen.orders.length === 2
      && JSON.stringify([...seen.orders[1]].sort()) === JSON.stringify([...queued].sort()), JSON.stringify({ orders: seen.orders, server: queued }));
    const resorted = await page.evaluate(() => {
      const key = document.querySelector("#next .reorder.resorted");
      return key ? key.getAttribute("title") || key.getAttribute("aria-label") || key.textContent : null;
    });
    check(named("Next up shows no Resorted key"), !resorted, String(resorted));
    await page.screenshot({ path: path.join(work, `close-next-holds-${round}.png`) });
    check(named("the page threw no errors"), errors.length === 0, errors.join(" | "));
    // The next round starts with no timer running.
    if (after.timer) await page.evaluate((id) => setTimer(id, false), after.timer.step_id);
    await sleep(3000);
  } finally {
    await context.close();
  }
}

// The harness's cookie file, once it is written and the server answers.
async function harnessUp(work, server) {
  const file = path.join(work, "cookie.json");
  const end = Date.now() + 90e3;
  while (Date.now() < end) {
    if (server.exitCode !== null) throw new Error(`serve_slow.py exited with ${server.exitCode}`);
    if (fs.existsSync(file)) {
      const info = JSON.parse(fs.readFileSync(file, "utf8"));
      const up = await fetch(`${info.origin}/healthz`).then((response) => response.ok, () => false);
      if (up) return info;
    }
    await sleep(200);
  }
  throw new Error("serve_slow.py did not come up within 90 s");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
