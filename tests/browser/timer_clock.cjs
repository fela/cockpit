// The page has no timer chip; a step's own clock shows its time, and play when it is stopped (TRACK-8, VIEW-29).
// Checks, on the page at / and at /classic, at laptop and phone width:
// - while a step's timer runs, the page has no timer chip, and the step's clock on Next up shows the time ("20:03"),
//   ticks and holds no Pause;
// - once the page stops the timer (as going idle does), Cockpit has no timer and the stopped clock shows the time
//   beside play;
// - the page has no sideways scroll;
// - on the page at /, play on the stopped clock starts the timer again; showing another item (as a row, Search or the
//   keyboard does, all through selectItem) stops it on that step with no notice (the shown item's step may start its
//   own timer, on its own card, as opening a step does), and showing the timed item again runs its timer on its own, so
//   no timer runs out of sight; on a phone, the browser's back gesture from the item screen to the list stops it the
//   same way, and forward to the item screen runs it again; an ask for the list taken back before its slide lands
//   leaves the item screen and its timer as they are; feedback that ends on the list leaves the step it held paused;
//   Return on the question after a stretch without activity, asked while the list shows, brings the item screen back
//   with the timer running; an opening that focuses shows the item screen at once during a slide, with one history
//   entry for both; at laptop width, narrowing the window onto a list screen kept from before stops the timer too.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
// Screenshots go to the folder given as the first argument, when one is given.
//
// Usage, from the checkout: node tests/browser/timer_clock.cjs [screenshot folder]

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
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "timer-clock-test", "content-type": "application/json" };

const CLOCK = /^(\d+:)?\d+:\d\d$/;

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const shots = process.argv[2] || null;
  if (shots) fs.mkdirSync(shots, { recursive: true });
  const work = workFolder("cockpit-timer-clock-");
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
    const initial = await user("GET", "/api/state");
    if (initial.timer) await user("POST", `/api/steps/${initial.timer.step_id}/timer/pause`);
    await Promise.all([...initial.queue, ...initial.ready_next, ...initial.snoozed].map((step) => user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
    browser = await chromium.launch({ executablePath: CHROMIUM });

    for (const [where, width] of [["/", 1440], ["/", 390], ["/classic", 1440], ["/classic", 390]]) {
      const name = `${where} ${width}`;
      const tag = `${where.slice(1) || "root"}-${width}`;
      const key = `note:timer-clock-${Date.now()}`;
      await agent("PUT", "/api/items", { key, title: "Timer clock test", priority: "urgent" });
      const answer = await agent("POST", "/api/steps", {
        item_key: key, need: `review:timer-clock-${tag}`, ask: "Timer clock test", score: 100, session_id: SESSION_B,
        body: "Timer clock test body.",
      });
      await user("POST", `/api/steps/${answer.step.id}/timer/start`);
      // 20 minutes and some seconds on the item, so the step's clock has something to show.
      await user("POST", `/api/items/${answer.step.item_id}/time/add`, { seconds: 1203 });

      const context = await browser.newContext({ viewport: { width, height: 900 }, isMobile: width < 600, hasTouch: width < 600 });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      page.setDefaultTimeout(5000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
      await page.goto(`${info.origin}${where}`);

      const running = page.locator(`#next [data-step="${answer.step.id}"] .step-menu [data-timer="running"]`);
      await running.waitFor({ state: "attached", timeout: 15000 }).catch(async (error) => {
        if (shots) await page.screenshot({ path: path.join(shots, `missing-${tag}.png`) });
        throw new Error(`${error.message}; page errors: ${errors.join(" | ")}`);
      });
      const cardClock = running.locator(".timer-clock");
      const before = await cardClock.textContent();
      await sleep(2200);
      const after = await cardClock.textContent();
      const chips = await page.locator("#timer, .timer-chip").count();
      check(`${name}: the page has no timer chip`, chips === 0, `${chips} found`);
      check(`${name}: the step's clock shows the time`, CLOCK.test(after) && after.startsWith("20:"), after);
      check(`${name}: the step's clock ticks`, after !== before, `${before} then ${after}`);
      const keys = await page.locator(`#next [data-step="${answer.step.id}"] :is(.step-menu [data-timer="start"], [aria-label*="Pause"])`).count();
      check(`${name}: the running timer has no Pause and no play`, keys === 0, `${keys} keys`);
      const head = await page.evaluate((id) => [...document.querySelectorAll(`#next [data-step="${id}"] > .step-context .timer-clock`)]
        .filter((clock) => !clock.closest(".step-menu")).length, answer.step.id);
      check(`${name}: the card's head shows no clock; it is in the ⋯ menu`, head === 0, `${head} on the head`);
      // The ⋯ key carries no dot, whether the timer runs or not.
      const dotOn = (id) => page.evaluate((stepId) => {
        const key = document.querySelector(`#next [data-step="${stepId}"] .step-menu > summary`);
        return getComputedStyle(key, "::before").content !== "none";
      }, id);
      check(`${name}: ⋯ carries no dot while the timer runs`, !(await dotOn(answer.step.id)), "a dot");
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth);
      check(`${name}: the page has no sideways scroll`, overflow <= 0, `${overflow}px too wide`);
      if (shots) {
        await page.locator(`#next [data-step="${answer.step.id}"] .step-menu > summary`).click();
        await sleep(600);
        await page.screenshot({ path: path.join(shots, `running-${tag}.png`) });
        await page.keyboard.press("Escape");
      }

      // The page's own stop, which going idle, leaving the step or snoozing it uses.
      await page.evaluate((id) => setTimer(id, false), answer.step.id);
      const stopped = page.locator(`#next [data-step="${answer.step.id}"] .step-menu [data-timer="stopped"]`);
      await stopped.waitFor({ state: "attached" });
      await sleep(500);
      const state = await user("GET", "/api/state");
      check(`${name}: the stop reaches Cockpit`, !state.timer, JSON.stringify(state.timer));
      check(`${name}: ⋯ carries no dot once the timer stops`, !(await dotOn(answer.step.id)), "a dot");
      const clockText = (await stopped.locator(".menu-time").textContent()).trim();
      check(`${name}: the stopped clock shows the time`, CLOCK.test(clockText), clockText);
      const menu = page.locator(`#next [data-step="${answer.step.id}"] .step-menu > summary`);
      const play = page.locator(`#next [data-step="${answer.step.id}"] .step-menu [data-timer="start"]`);
      const playLabel = (await play.textContent()).trim();
      check(`${name}: the stopped timer has Start the timer`, (await play.count()) === 1 && /start/i.test(playLabel), playLabel);
      await menu.click();
      await sleep(600);
      if (shots) await page.screenshot({ path: path.join(shots, `paused-${tag}.png`) });
      await page.keyboard.press("Escape");

      if (where === "/") {
        await menu.click();
        await play.click();
        await running.waitFor({ state: "attached" });
        const other = `note:timer-clock-other-${Date.now()}`;
        await agent("PUT", "/api/items", { key: other, title: "Timer clock other", priority: "low" });
        const second = await agent("POST", "/api/steps", {
          item_key: other, need: `review:timer-clock-other-${tag}`, ask: "Timer clock other", score: 10, session_id: SESSION_B,
          body: "Timer clock other body.",
        });
        await page.waitForFunction((id) => ui.state && ui.state.items.some((item) => item.id === id), second.step.item_id, { timeout: 15000 });
        await page.evaluate((id) => selectItem(id), second.step.item_id);
        await page.locator(`#next [data-step="${second.step.id}"]`).waitFor();
        await sleep(800);
        const away = await user("GET", "/api/state");
        check(`${name}: showing another item stops the timer on the step left`, !away.timer || away.timer.step_id === second.step.id, JSON.stringify(away.timer));
        const shownClock = away.timer ? await page.locator(`#next [data-step="${second.step.id}"].on-clock`).count() : 1;
        check(`${name}: a running timer shows on the card in sight`, shownClock === 1, JSON.stringify(away.timer));
        const toastText = await page.locator("#toast .toast-text").textContent().catch(() => "");
        check(`${name}: showing another item shows no "left the step" notice `, !/left the step/.test(toastText || ""), toastText);
        // Back on the timed item, its step starts again on its own, as opening a step does: the stop was not the user's pause.
        await page.evaluate((id) => selectItem(id), answer.step.item_id);
        await running.waitFor({ state: "attached", timeout: 15000 });
        await sleep(800);
        const back = await user("GET", "/api/state");
        check(`${name}: showing the timed item again runs its timer`,
          back.timer && back.timer.step_id === answer.step.id, JSON.stringify(back.timer));
        // Away and back before the page's next check of the timer: the step still starts again.
        await page.evaluate(([away, home]) => { selectItem(away); selectItem(home); }, [second.step.item_id, answer.step.item_id]);
        await sleep(2500);
        const quick = await user("GET", "/api/state");
        check(`${name}: away and straight back runs the timer again`,
          quick.timer && quick.timer.step_id === answer.step.id, JSON.stringify(quick.timer));
        if (width >= 600) {
          // A press on the list at the left that shows no other item leaves the card in sight and its timer running.
          await page.evaluate(() => { for (const el of document.querySelectorAll("#toast .toast-text")) el.textContent = ""; });
          const spots = await page.evaluate(() => {
            const plain = (x, y) => {
              const el = document.elementFromPoint(x, y);
              return el && !el.closest("button, a, input, textarea, select, summary, label, [data-item], [data-step], [role=button], li");
            };
            const find = (box) => {
              for (let y = box.bottom - 4; y > box.top; y -= 6) {
                for (let x = box.left + 4; x < box.right; x += 12) if (plain(x, y)) return { x, y };
              }
              return null;
            };
            return { list: find(document.getElementById("sidebar").getBoundingClientRect()) };
          });
          for (const [where, spot] of Object.entries(spots)) {
            if (!spot) { check(`${name}: a plain spot on the ${where} to press`, false, "none found"); continue; }
            await page.mouse.click(spot.x, spot.y);
            await sleep(800);
            const pressed = await user("GET", "/api/state");
            const toastText = await page.locator("#toast .toast-text").textContent().catch(() => "");
            check(`${name}: a press on the ${where} leaves the timer running, with no "left the step" notice`,
              pressed.timer && pressed.timer.step_id === answer.step.id && !/left the step/.test(toastText || ""),
              JSON.stringify({ timer: pressed.timer, toastText }));
          }
        }
        // Opening another item's session (Search and the keyboard do it without a press on the page) puts its
        // conversation in the card's place, which leaves the step as showing that item does.
        await page.evaluate(([session, id]) => openSessionView(session, id), [SESSION_B, second.step.item_id]);
        await sleep(1200);
        const conversing = await user("GET", "/api/state");
        check(`${name}: another item's conversation stops the timer`, !conversing.timer, JSON.stringify(conversing.timer));
        await page.evaluate((id) => { closeSessionView(true); selectItem(id); }, answer.step.item_id);
        await user("POST", `/api/steps/${answer.step.id}/timer/start`);
        await running.waitFor({ state: "attached", timeout: 15000 });
        if (width < 600) {
          // On a phone the browser's back gesture goes from the item screen to the list, which hides the card: that
          // pauses its timer as the bar's key does, with no press on the page (regression check). The last toast's words are
          // blanked first, so the words read below are the back gesture's.
          await page.evaluate(() => { for (const el of document.querySelectorAll("#toast .toast-text")) el.textContent = ""; });
          await page.goBack();
          await page.waitForFunction(() => document.body.classList.contains("phone-list"));
          await sleep(1200);
          const listed = await user("GET", "/api/state");
          check(`${name}: back to the list stops the timer`, !listed.timer, JSON.stringify(listed.timer));
          const listToast = await page.locator("#toast .toast-text").textContent().catch(() => "");
          check(`${name}: back to the list shows no "left the step" notice`, !/left the step/.test(listToast || ""), listToast);
          await page.goForward();
          await running.waitFor({ state: "attached", timeout: 15000 });
          await sleep(800);
          const resumed = await user("GET", "/api/state");
          check(`${name}: forward to the item screen runs its timer again`,
            resumed.timer && resumed.timer.step_id === answer.step.id && !(await page.evaluate(() => document.body.classList.contains("phone-list"))),
            JSON.stringify(resumed.timer));
          // An ask for the list that a second ask takes back before its slide lands changes nothing: the item screen
          // stays and its timer runs (regression check).
          await page.evaluate(() => { setPhoneScreen(true, true); setPhoneScreen(false, true); });
          await sleep(1200);
          const kept = await user("GET", "/api/state");
          const listShown = await page.evaluate(() => document.body.classList.contains("phone-list"));
          check(`${name}: a list taken back before its slide leaves the item screen and its timer`,
            !listShown && kept.timer && kept.timer.step_id === answer.step.id, JSON.stringify({ listShown, timer: kept.timer }));
          // Writing feedback holds the step's timer and runs it again when the feedback ends; the list hiding the card
          // meanwhile leaves the step, so it stays paused.
          await page.evaluate(() => holdForFeedback());
          await sleep(800);
          await page.goBack();
          await page.waitForFunction(() => document.body.classList.contains("phone-list"));
          await page.evaluate(() => releaseFeedbackHold());
          await sleep(1200);
          const afterFeedback = await user("GET", "/api/state");
          check(`${name}: feedback ended on the list leaves the hidden step paused`, !afterFeedback.timer, JSON.stringify(afterFeedback.timer));
          await page.goForward();
          await page.waitForFunction(() => !document.body.classList.contains("phone-list"));
          await user("POST", `/api/steps/${answer.step.id}/timer/start`);
          await running.waitFor({ state: "attached", timeout: 15000 });
          // The question after a stretch without activity can wait while the back gesture shows the list behind it;
          // Return runs the timer on with its item screen in sight again.
          const counted = (await user("GET", "/api/state")).input.count;
          await sleep(1200);
          await page.evaluate((seen) => {
            const timer = drawn(ui.state).timer;
            ui.idleStop = stopIdle(timer, Date.parse(timer.started_at) + 500, seen);
          }, counted);
          await page.locator("dialog#idle[open]").waitFor();
          await page.goBack();
          await page.waitForFunction(() => document.body.classList.contains("phone-list"));
          await page.locator("dialog#idle[open] button", { hasText: "Return" }).click();
          const itemAgain = await page.waitForFunction(() => !document.body.classList.contains("phone-list"), null, { timeout: 5000 })
            .then(() => true, () => false);
          await sleep(800);
          const returned = await user("GET", "/api/state");
          check(`${name}: Return from the list shows the item with its timer running`,
            itemAgain && returned.timer && returned.timer.step_id === answer.step.id, JSON.stringify({ itemAgain, timer: returned.timer }));
          if (!itemAgain) await page.goForward();
          await running.waitFor({ state: "attached", timeout: 15000 });
        } else {
          // A list screen kept from a phone-width moment shows nothing at this width, so the timer runs on; narrowing
          // the window then hides the card under the list, which pauses it (regression check).
          await page.evaluate(() => setPhoneScreen(true, false));
          await sleep(800);
          const wide = await user("GET", "/api/state");
          check(`${name}: a list kept at this width leaves the timer running`,
            wide.timer && wide.timer.step_id === answer.step.id, JSON.stringify(wide.timer));
          await page.setViewportSize({ width: 390, height: 900 });
          await sleep(1200);
          const narrowed = await user("GET", "/api/state");
          check(`${name}: narrowing to the phone's list stops the timer`, !narrowed.timer, JSON.stringify(narrowed.timer));
          await page.setViewportSize({ width, height: 900 });
          await page.evaluate(() => setPhoneScreen(false, false));
          await user("POST", `/api/steps/${answer.step.id}/timer/start`);
          await running.waitFor({ state: "attached", timeout: 15000 });
        }
        await user("POST", `/api/steps/${answer.step.id}/timer/pause`);
        await stopped.waitFor({ state: "attached", timeout: 15000 });
        await user("POST", `/api/steps/${second.step.id}/snooze`, { until: "2099-01-01T08:00:00Z" });
        if (width < 600) {
          // An opening that focuses at once (the keyboard's) shows the item screen at once, also while a press's
          // slide to it is on its way, and the two make one history entry, so one back shows the list again.
          await page.evaluate(() => showPhoneList());
          await page.waitForFunction(() => document.body.classList.contains("phone-list"));
          const atOnce = await page.evaluate(() => {
            showPhoneItem(true);
            showPhoneItem(false);
            return !document.body.classList.contains("phone-list");
          });
          await sleep(800);
          const stays = await page.evaluate(() => !document.body.classList.contains("phone-list"));
          check(`${name}: an opening that focuses shows the item screen during a slide`, atOnce && stays, JSON.stringify({ atOnce, stays }));
          await page.goBack();
          const backed = await page.waitForFunction(() => document.body.classList.contains("phone-list"), null, { timeout: 3000 })
            .then(() => true, () => false);
          check(`${name}: one back from that opening shows the list`, backed, "the list did not show");
        }
      }

      check(`${name}: no page errors`, errors.length === 0, errors.join(" | "));
      await context.close();
      // Snoozed, the step leaves Next up to the next run's step.
      await user("POST", `/api/steps/${answer.step.id}/snooze`, { until: "2099-01-01T08:00:00Z" });
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
