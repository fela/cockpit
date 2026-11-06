// Take back after Send says what became of the message, and gives its words back. Checks, at laptop and phone width:
// - Send on Next up hands the step off, and the notice offers Take back;
// - a notice the pointer rests on keeps the message waiting on Cockpit;
// - Take back draws the step at the top of the queue at once, where Cockpit puts it, not in Ready next until Cockpit
//   answers;
// - Take back brings the step back with its reply box open, holding the words that were not sent, and the notice says
//   the message was not sent and its words are back in the box;
// - no failed receipt stays under the box, and on Cockpit the message is set aside unsent;
// - Send from the box again hands the step off with the added words.
// - Send pressed while a Take back is still on its way to Cockpit waits for it, and hands the step off with its receipt
//   kept; its notice keeps Take back, and says so when the Take back found the earlier message already gone.
//
// Starts serve_preview.py (this checkout's Cockpit on a copy of the live database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
// Screenshots go to the folder given as the first argument, when one is given.
//
// Usage, from the checkout: node tests/browser/take_back.cjs [screenshot folder]

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

// Browser dependencies can be selected through the environment (README.md).
const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const ROOT = path.resolve(__dirname, "..", "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
// The harness's agent token and second fake session (tests/conftest.py).
const SESSION_B = "22222222-2222-4222-8222-222222222222";
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "take-back-test", "content-type": "application/json" };

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const shots = process.argv[2] || null;
  if (shots) fs.mkdirSync(shots, { recursive: true });
  const work = workFolder("cockpit-take-back-");
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const info = await harnessUp(work, server);
    const cookie = { cookie: `${info.name}=${info.value}`, origin: info.origin, "content-type": "application/json" };
    const agent = (method, url, body) => call(info.origin, AGENT, method, url, body);
    const user = (method, url, body) => call(info.origin, cookie, method, url, body);

    // The fixture database's open steps are snoozed and its timer paused, so this run's step is Next up.
    const copied = await user("GET", "/api/state");
    if (copied.timer) await user("POST", `/api/steps/${copied.timer.step_id}/timer/pause`);
    await Promise.all([...copied.queue, ...copied.ready_next, ...copied.snoozed].map((step) => user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
    browser = await chromium.launch({ executablePath: CHROMIUM });

    for (const width of [1440, 390]) {
      const key = `note:take-back-${Date.now()}`;
      await agent("PUT", "/api/items", { key, title: "Take back test", priority: "urgent", sessions: [SESSION_B] });
      const answer = await agent("POST", "/api/steps", {
        item_key: key, need: `review:take-back-${width}`, ask: "Take back test", score: 100, session_id: SESSION_B,
        body: "Take back test body.",
      });
      const id = answer.step.id;

      const context = await browser.newContext({ viewport: { width, height: 900 }, isMobile: width < 600, hasTouch: width < 600 });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      page.setDefaultTimeout(20000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
      await page.goto(`${info.origin}/classic`, { timeout: 90000 });
      const card = page.locator(`#next article[data-step="${id}"]`).first();
      // The first drawing of a synthetic board can take long on a busy host.
      await card.waitFor({ timeout: 90000 });
      // What the page draws at the press, before Cockpit answers: the step taken back leads the queue, as Cockpit puts
      // it, rather than waiting in Ready next until Cockpit's answer moves it.
      const placed = await page.evaluate((id) => {
        const step = ui.state.queue.find((other) => other.id === id);
        const old = "2026-01-01T00:00:00Z";
        const other = (n, fields) => ({ ...step, id: 1e9 + n, score: 50 - n, queued_at: old, taken_at: null, started_in_place: false, timer: null, early: false, snoozed_until: null, handoff: null, handoff_message_id: null, ...fields });
        const away = (fields) => ({ ...step, handoff: { text: "x" }, handoff_message_id: 1, queued_at: null, timer: null, snoozed_until: null, ...fields });
        const board = (queue, handed) => recount({ ...ui.state, queue, ready_next: [], snoozed: [], with_agents: [handed] });
        const where = (state) => ({ queue: state.queue.map((s) => s.id), ready: state.ready_next.map((s) => s.id), snoozed: state.snoozed.map((s) => s.id) });
        const four = [1, 2, 3, 4].map((n) => other(n));
        const full = applyAction(board(four, away()), away(), "return");
        const over = applyAction(board([...four, other(5)], away()), away(), "return");
        const confirmed = board(four, away());
        const already = { ...confirmed, queue: [{ ...step, handoff: null, queued_at: old, taken_at: old }, ...four.slice(0, 3)], with_agents: [] };
        const snoozedBack = applyAction(board(four, away({ snoozed_until: "2099-01-01T00:00:00Z" })), away(), "return");
        const timed = applyAction(board(four, away({ timer: { started_at: old } })), away(), "return");
        return {
          full: where(full), firstHandoff: full.queue[0].handoff, over: where(over),
          replay: applyAction(already, step, "return") === already, snoozed: where(snoozedBack),
          timerStarted: timed.queue.find((s) => s.id === id).started_in_place,
        };
      }, id);
      const wanted = (list) => JSON.stringify(list);
      check(`${width}: Take back draws the step at the top of a full queue at once`,
        wanted(placed.full.queue) === wanted([id, 1e9 + 1, 1e9 + 2, 1e9 + 3]) && wanted(placed.full.ready) === wanted([1e9 + 4]) && !placed.firstHandoff, JSON.stringify(placed.full));
      check(`${width}: Take back on a queue over four places leaves four`, placed.over.queue.length === 4 && placed.over.queue[0] === id, JSON.stringify(placed.over));
      check(`${width}: a Take back Cockpit already made is left as Cockpit placed it`, placed.replay, "the replay changed the state");
      check(`${width}: a snoozed step taken back stays snoozed and moves no other step`,
        wanted(placed.snoozed.snoozed) === wanted([id]) && placed.snoozed.queue.length === 4 && placed.snoozed.ready.length === 0, JSON.stringify(placed.snoozed));
      check(`${width}: a step taken back has no time in its new place`, placed.timerStarted === false, String(placed.timerStarted));
      const box = card.locator(`textarea[data-key="msg:step:${id}"]`);
      const words = `Create a summary of the changes ${width}`;
      await box.fill(words);
      await card.locator(".composer:not(.feedback-box) button.send").first().click();
      const takeBack = page.locator("#toast button", { hasText: "Take back" });
      await takeBack.waitFor();
      check(`${width}: Send hands the step off and the notice offers Take back`, true, "");
      // The pointer keeps the notice up, so Cockpit's wait starts again before its limit ends under it.
      const holding = page.waitForRequest((request) => /\/api\/messages\/\d+\/hold$/.test(request.url()), { timeout: 25000 }).catch(() => null);
      await page.locator("#toast").hover();
      check(`${width}: a notice the pointer rests on holds the message on Cockpit`, (await holding) !== null, "no hold in 25 s");
      await takeBack.click();

      await card.waitFor();
      const back = card.locator(`textarea[data-key="msg:step:${id}"]`);
      await back.waitFor();
      // The step shows at the press; its words come back once Cockpit says the message was stopped.
      await page.waitForFunction(([key, words]) => document.querySelector(`#next textarea[data-key="${key}"]`)?.value === words,
        [`msg:step:${id}`, words], { timeout: 20000 }).catch(() => {});
      check(`${width}: the reply box holds the words that were not sent`, (await back.inputValue()) === words, await back.inputValue());
      await page.locator("#toast .toast-text", { hasText: "Taken back" }).waitFor().catch(() => {});
      const toastText = await page.locator("#toast .toast-text").textContent();
      check(`${width}: the notice says the message was not sent and its words are back`,
        toastText === "Taken back. Your message was not sent; its words are back in the step's reply box to add to and send.", toastText);
      check(`${width}: no failed receipt stays under the box`, (await card.locator(".receipt.failed").count()) === 0, "a failed receipt shows");
      const state = await user("GET", "/api/state");
      const message = state.messages.find((other) => other.step_id === id);
      check(`${width}: Cockpit set the message aside unsent`, message && message.state === "failed" && Boolean(message.dismissed_at), JSON.stringify(message));
      if (shots) await card.screenshot({ path: path.join(shots, `taken-back-${width}.png`) });

      await back.press("End");
      await back.type(" and add a link to the PR.");
      await card.locator(".composer:not(.feedback-box) button.send").first().click();
      await page.locator("#toast button", { hasText: "Take back" }).waitFor();
      await sleep(500);
      const handed = (await user("GET", "/api/state")).with_agents.find((step) => step.id === id);
      check(`${width}: Send again hands the step off with the added words`,
        handed && handed.handoff.text === `${words} and add a link to the PR.`, JSON.stringify(handed && handed.handoff.text));

      // Take back slow to reach Cockpit, and Send pressed before it answers: the Send waits for it, and goes (regression check).
      let slowed = false;
      await page.route(`**/api/steps/${id}/return`, async (route) => {
        if (slowed) return route.fallback();
        slowed = true;
        await sleep(1500);
        await route.fallback();
      });
      await page.locator("#toast button", { hasText: "Take back" }).click();
      const quick = card.locator(`textarea[data-key="msg:step:${id}"]`);
      await quick.waitFor();
      await quick.fill(`Sent before the Take back landed ${width}`);
      const releases = [];
      const onRelease = (request) => { if (/\/api\/messages\/\d+\/release$/.test(request.url())) releases.push(request.url()); };
      page.on("request", onRelease);
      await card.locator(".composer:not(.feedback-box) button.send").first().click();
      await sleep(4000);
      // The Take back's own notice leaves the Send's in place, so the Send can still be taken back.
      const notice = await page.locator("#toast").evaluate((el) => !el.hidden && [...el.querySelectorAll("button")].some((button) => button.textContent === "Take back"));
      check(`${width}: the Send that waited keeps its notice with Take back`, notice && releases.length === 0,
        `notice with Take back: ${notice}; releases: ${releases.join(", ")}`);
      page.off("request", onRelease);
      const quickly = (await user("GET", "/api/state")).with_agents.find((step) => step.id === id);
      const refusal = await page.locator("#toast .toast-text").textContent().catch(() => "");
      check(`${width}: Send pressed while Take back is on its way waits for it and hands the step off`,
        quickly && quickly.handoff.text.includes(`Sent before the Take back landed ${width}`) && !/Not sent/.test(refusal),
        `${JSON.stringify(quickly && quickly.handoff.text)}; notice: ${refusal}`);
      // The Take back it waited for leaves the Send's receipt in place (CODE-3).
      const mark = await page.evaluate((stepId) => { const sent = ui.sent.get(stepId); return sent ? sent.text : null; }, id);
      check(`${width}: the Send that waited keeps its receipt for when the step comes back`,
        mark === `Sent before the Take back landed ${width}`, JSON.stringify(mark));
      await page.unroute(`**/api/steps/${id}/return`);

      // A Take back that finds its message already gone, answering after a newer Send: the newer notice keeps Take back
      // and adds that the earlier message had gone. The page gets Cockpit's answer with
      // `stopped` false, as when the message reached its session before the Take back.
      let answeredLate = false;
      await page.route(`**/api/steps/${id}/return`, async (route) => {
        const response = await route.fetch();
        const answer = await response.json();
        await sleep(1500);
        answeredLate = true;
        await route.fulfill({ response, json: { ...answer, stopped: false, unsent: null, unsent_to: null } });
      });
      const lateReleases = [];
      const onLateRelease = (request) => { if (/\/api\/messages\/\d+\/release$/.test(request.url())) lateReleases.push(request.url()); };
      page.on("request", onLateRelease);
      await page.locator("#toast button", { hasText: "Take back" }).click();
      const again = card.locator(`textarea[data-key="msg:step:${id}"]`);
      await again.waitFor();
      await again.fill(`A correction ${width}`);
      await card.locator(".composer:not(.feedback-box) button.send").first().click();
      check(`${width}: the correction goes while the Take back is on its way`, !answeredLate, "the Take back had already answered");
      await sleep(2500);
      const lateNotice = await page.locator("#toast").evaluate((el) => ({
        shown: !el.hidden, text: (el.querySelector(".toast-text") || {}).textContent || "",
        tip: (el.querySelector(".toast-text") || { dataset: {} }).dataset.tip || "",
        takeBack: [...el.querySelectorAll("button")].some((button) => button.textContent === "Take back"),
      }));
      check(`${width}: the correction's notice keeps Take back and its tooltip, and says the earlier message had gone`,
        lateNotice.shown && lateNotice.takeBack && lateNotice.text.includes("Your earlier message had already gone to the session.")
          && lateNotice.tip.startsWith("Your message goes once this notice is gone") && lateReleases.length === 0, JSON.stringify({ ...lateNotice, lateReleases }));
      // Add more rewords the notice, and the sentence stays (CODE-3).
      await page.locator("#toast button.toast-more").click();
      await page.locator("#toast .toast-follow textarea").fill(`More for the correction ${width}`);
      await page.locator("#toast button.toast-follow-send").click();
      await sleep(500);
      const reworded = await page.locator("#toast .toast-text").textContent();
      check(`${width}: after Add more the notice still says the earlier message had gone`,
        reworded.includes("Added for") && reworded.includes("; it goes with your message once this notice is gone. Your earlier message had already gone to the session.")
          && lateReleases.length === 0, JSON.stringify({ reworded, lateReleases }));
      page.off("request", onLateRelease);
      await page.unroute(`**/api/steps/${id}/return`);

      check(`${width}: no page errors`, errors.length === 0, errors.join(" | "));
      await context.close();
    }
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failedChecks = results.filter((result) => !result.ok);
  console.error(failedChecks.length ? `${failedChecks.length} of ${results.length} checks failed` : `all ${results.length} checks passed`);
  process.exitCode = failedChecks.length ? 1 : 0;
}

async function call(origin, headers, method, url, body) {
  const response = await fetch(`${origin}${url}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await response.text();
  if (!response.ok) throw new Error(`${method} ${url}: ${response.status} ${text}`);
  return text ? JSON.parse(text) : null;
}

// The harness's cookie file, once it is written and the server answers.
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
