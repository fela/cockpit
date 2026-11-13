// Every step keeps a key that closes it (SPEC CONTROL-15). A step to read (need `read:…`) whose only answer asks about
// the next action shows Task done beside that answer, and no Done that closes only the step; E presses Task done,
// which completes the task. On a synthetic database, at desktop (1280 px, light) and phone (390 px, dark) width,
// each on a new item. Saves a screenshot of the card per width in the work dir.
//
// Usage, from the checkout: node tests/browser/read_close_key.cjs   (it starts serve_preview.py)

"use strict";

const path = require("node:path");
const { startPreview } = require("./work.cjs");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;
const { chromium } = require(PLAYWRIGHT);

// The harness's agent token (tests/conftest.py TOKEN) and its fake session (SESSION_A).
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "read-close-key-test", "content-type": "application/json" };
const SESSION = "11111111-1111-4111-8111-111111111111";

let failures = 0;
function check(ok, words, detail) {
  console.log(`${ok ? "ok  " : "FAIL"} ${words}${ok ? "" : `: ${detail}`}`);
  if (!ok) failures += 1;
}

const wait = (ms) => new Promise((done) => setTimeout(done, ms));

async function call(origin, headers, method, url, body) {
  const response = await fetch(`${origin}${url}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await response.text();
  if (!response.ok) throw new Error(`${method} ${url}: ${response.status} ${text}`);
  return text ? JSON.parse(text) : null;
}

(async () => {
  const { work, info } = await startPreview("cockpit-read-close-key-");
  const user = { cookie: `${info.name}=${info.value}`, origin: info.origin, "content-type": "application/json" };
  const agent = (method, url, body) => call(info.origin, AGENT, method, url, body);
  // The synthetic steps wait for later, so the new step leads the queue.
  const state = await call(info.origin, user, "GET", "/api/state");
  await Promise.all([...state.queue, ...state.ready_next].map((step) =>
    call(info.origin, user, "POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));

  const browser = await chromium.launch({ executablePath: CHROMIUM });
  for (const [width, height, scheme] of [[1280, 900, "light"], [390, 844, "dark"]]) {
    const key = `note:read-close-key-${Date.now()}-${width}`;
    await agent("PUT", "/api/items", { key, title: `Release notes ${width}`, stage: "review", sessions: [SESSION] });
    const { step } = await agent("POST", "/api/steps", {
      item_key: key, need: "read:notes", ask: "the release notes are ready; read them", session_id: SESSION, score: 100,
      body: "The export names each column. Reminder settings have their own page.",
      answers: [{ text: "Add a short line on the reminders", closes: false }],
    });

    const context = await browser.newContext({ viewport: { width, height }, colorScheme: scheme });
    await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
    const page = await context.newPage();
    page.setDefaultTimeout(60e3);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`${info.origin}/`, { timeout: 120e3 });
    // The open card, on the main page at desktop width and on the step screen a phone opens to.
    const card = page.locator(`article.card[data-step="${step.id}"]:visible`).first();
    await card.waitFor();
    await wait(400);
    const keys = await card.evaluate((el) => ({
      taskDone: [...el.querySelectorAll(".button.task-done")].some((button) => !button.hidden && button.offsetParent),
      done: [...el.querySelectorAll("button")].some((button) => /^Done$/.test(button.textContent.trim()) && button.offsetParent),
      answer: [...el.querySelectorAll(".answers .button")].some((button) => button.textContent.includes("Add a short line")),
    }));
    check(keys.taskDone && keys.answer, `${width} px: a step to read with an answer shows Task done beside it`, JSON.stringify(keys));
    check(!keys.done, `${width} px: it shows no Done that closes only the step`, JSON.stringify(keys));
    await card.screenshot({ path: path.join(work, `read-close-key-${width}.png`) });

    // E, the card's key, presses Task done and completes the task.
    await page.locator("body").click({ position: { x: 2, y: 2 } });
    await page.keyboard.press("e");
    let after = null;
    for (let tries = 0; tries < 50; tries += 1) {
      after = await agent("GET", `/api/items?key=${encodeURIComponent(key)}`);
      if (after.stage === "done") break;
      await wait(200);
    }
    check(after.stage === "done", `${width} px: E completes the task`, after.stage);
    check(errors.length === 0, `${width} px: no page errors`, errors.join("; "));
    await context.close();
  }
  await browser.close();
  console.log(`screenshots in ${work}`);
  process.exit(failures ? 1 : 0);
})().catch((error) => { console.error(error); process.exit(1); });
