// A press on Next up's card that the page's own timer start meets halfway still lands (UI-19, TRACK-2).
// The page starts the timer of the step on the card itself, within a second of the user's first input. That input is
// often the press on one of the card's buttons, and the start redrew the card while the finger or mouse button was
// still down: the button under it was a new one by the release, and the press did nothing.
// Checks, with the mouse at desktop width and a touch at phone width:
// - a press on the card's answer held through the page's timer start still sends the answer;
// - the start reached Cockpit all the same, from the press;
// - a press held on the card's text, which presses nothing, shows the running timer once it ends;
// - with no press, the timer shows running within two seconds of the user's input, as before;
// - a refusal of an earlier action that comes while the user holds an answer on the card waits for the press to end:
//   the button under the press stays the same, the answer is sent, and the refused step comes back once the press ends;
// - the refusal's notice waits too while the user holds the button of the notice that shows (Take back is one): the
//   press lands, and the refusal's notice shows once it ended;
// - a press on a notice's button held past the page's 3 s limit holds notices back no longer: a notice that waited
//   for it shows at the limit, the press still down, and one that comes after the limit shows at once;
// - a notice that comes while the user holds the shown notice's text waits, and shows as soon as the press ends;
// - a notice that comes while the user holds Add more on a hand-off's notice keeps waiting behind the box the press
//   opens, so the hand-off's notice stays, and shows once the box closes;
// - a notice that waited behind that box, still waiting after Send, does not come back after a newer one;
// - the page threw no errors.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) and drives
// a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/timer_start_press.cjs

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
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "timer-press-test", "content-type": "application/json" };

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-timer-press-");
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
    browser = await chromium.launch({ executablePath: CHROMIUM });
    for (const how of ["mouse", "touch"]) await round({ browser, info, agent, user, check, work, how });
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failed = results.filter((result) => !result.ok);
  console.error(failed.length ? `${failed.length} of ${results.length} checks failed` : `all ${results.length} checks passed`);
  process.exitCode = failed.length ? 1 : 0;
}

async function round({ browser, info, agent, user, check, work, how }) {
  const named = (words) => `${how}: ${words}`;
  // The fixture's open steps are snoozed, so this round's steps take the user's places; a timer left running stops.
  const initial = await user("GET", "/api/state");
  if (initial.timer) await user("POST", `/api/steps/${initial.timer.step_id}/timer/pause`, { pressed_at: new Date().toISOString() }).catch(() => {});
  await Promise.all([...initial.queue, ...initial.ready_next, ...initial.snoozed].map((step) =>
    user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
  await Promise.all(initial.permission_requests.map((request) =>
    agent("POST", `/api/permissions/requests/${request.id}/withdraw`, { session_id: request.session_id })));
  const stamp = `${how}-${Date.now()}`;
  const put = async (name, body) => {
    const key = `note:timer-press-${stamp}-${name}`;
    await agent("PUT", "/api/items", { key, title: `Shared list ${name}`, stage: "building", sessions: [SESSION_B] });
    return (await agent("POST", "/api/steps", { item_key: key, need: `n-${name}`, session_id: SESSION_B, ...body })).step;
  };
  const answered = await put("export", { ask: "Show shared lists in exports?", score: 90,
    body: "The export selector now includes shared lists. Choose which lists to include in the CSV.",
    answers: [{ text: "Show it", closes: false }] });
  const held = await put("reminders", { ask: "Keep weekly reminders on Mondays?", score: 80,
    body: "Weekly reminders go out on Mondays at nine. Moving them to Sundays would reach people before the week starts." });
  const quiet = await put("sharing", { ask: "Allow sharing with view-only links?", score: 70,
    body: "A view-only link lets someone read a list without an account. Nobody can change the list through it.",
    answers: [{ text: "Allow it", closes: false }] });
  // Further down the queue: the step whose Done Cockpit refuses in check 4.
  const later = await put("labels", { ask: "Rename tags to labels?", score: 60,
    body: "Labels is the word the help pages use. The rename changes the menu and the export's column name." });

  const phone = how === "touch";
  const context = await browser.newContext(phone
    ? { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true }
    : { viewport: { width: 1280, height: 900 } });
  await context.setExtraHTTPHeaders({ cookie: cookie(info) });
  const page = await context.newPage();
  page.setDefaultTimeout(8000);
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const starts = [];
  page.on("request", (request) => {
    const found = /\/api\/steps\/(\d+)\/timer\/start$/.exec(request.url());
    if (found && request.method() === "POST") starts.push(Number(found[1]));
  });
  const client = phone ? await context.newCDPSession(page) : null;
  // A redraw can swap `target` for a new node just as it is measured, which reads as no box; ask again.
  const boxOf = async (target) => {
    const end = Date.now() + 5000;
    for (;;) {
      const box = await target.boundingBox().catch(() => null);
      if (box || Date.now() > end) return box;
      await sleep(100);
    }
  };
  // Down on the middle of `target`, held `ms`, then up, with the mouse or a finger. `midway`, when given, runs halfway
  // through the press.
  const pressHeld = async (target, ms, midway) => {
    const box = await boxOf(target);
    const x = box.x + box.width / 2;
    const y = box.y + box.height / 2;
    if (phone) {
      await client.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x, y }] });
      await holdFor(ms, midway);
      await client.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    } else {
      await page.mouse.down();
      await holdFor(ms, midway);
      await page.mouse.up();
    }
  };
  // The press lasts `ms` in all, and at least 200 ms after `midway`.
  const holdFor = async (ms, midway) => {
    if (!midway) return sleep(ms);
    const start = Date.now();
    await sleep(ms / 2);
    await midway();
    await sleep(Math.max(200, ms - (Date.now() - start)));
  };
  // The page as the user finds it on coming back: the card shows, nothing pressed or moved for a while, so the page
  // starts no timer until the next input. Its own start is held while the pointer moves into place.
  const quietOn = async (step, target) => {
    await page.evaluate((id) => { ui.autoStarted = id; }, step.id);
    if (!phone) {
      const box = await boxOf(target);
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    }
    await sleep(300);
    await page.evaluate(() => { ui.lastInput = 0; ui.autoStarted = null; });
    await sleep(1200);
  };
  const card = (step) => page.locator(`#next .next-card[data-step="${step.id}"]`);
  const running = (step) => card(step).evaluate((el) => el.classList.contains("on-clock")).catch(() => false);
  const handed = async (step) => (await user("GET", "/api/state")).with_agents.some((other) => other.id === step.id);
  const until = async (test, ms) => {
    const end = Date.now() + ms;
    while (!(await test()) && Date.now() < end) await sleep(150);
    return test();
  };
  // Whether the page shows step `id` somewhere, other than as a row playing its exit or in a notice.
  const shows = (id) => page.evaluate((stepId) => [...document.querySelectorAll(`[data-step="${stepId}"]`)]
    .some((el) => !el.closest(".motion-ghost, .close-copy, #toast")), id);
  // Waits until nothing on the page moves: the layout stays the same for 300 ms.
  const still = async () => {
    let before = "";
    const end = Date.now() + 5000;
    while (Date.now() < end) {
      const now = await page.evaluate(() => JSON.stringify([...document.querySelectorAll("#next, #queue, #toast")]
        .map((el) => { const box = el.getBoundingClientRect(); return [box.x, box.y, box.width, box.height]; })));
      if (now === before) return;
      before = now;
      await sleep(300);
    }
  };
  // Cockpit refuses the user's Done on `step` once `refuse` is called, as it does a step snoozed meanwhile; `refused`
  // then tells whether the refusal has gone to the page.
  const refuseDone = async (step) => {
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    const state = { refused: false, refuse: () => release() };
    await page.route(`**/api/steps/${step.id}/done`, async (route) => {
      await gate;
      await route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ detail: `step ${step.id} is snoozed` }) });
      state.refused = true;
    });
    // Not awaited: the Done settles only once Cockpit refuses it.
    await page.evaluate((id) => { act([...ui.state.queue, ...ui.state.ready_next].find((one) => one.id === id), "done"); }, step.id);
    return state;
  };
  const toastText = () => page.locator("#toast").innerText().catch(() => "");
  // The step leaves the card as the user would move it: its timer paused, then snoozed.
  const aside = async (step) => {
    await page.evaluate((id) => { ui.autoStarted = id; return setTimer(id, false); }, step.id);
    await until(async () => !(await user("GET", "/api/state")).timer, 4000);
    await user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" });
  };
  try {
    await page.goto(`${info.origin}/`, { timeout: 60000 });
    await card(answered).waitFor({ timeout: 30000 });
    await sleep(800);

    // 1. The press on the answer starts the timer, and the start comes while the press is held.
    const answer = card(answered).locator(".answers button", { hasText: "Show it" }).first();
    await quietOn(answered, answer);
    check(named("no timer runs before the press"), !(await running(answered)) && !starts.includes(answered.id),
      `starts sent: ${starts.join(", ") || "none"}`);
    await pressHeld(answer, 1600);
    check(named("the page started the timer during the press"), starts.includes(answered.id),
      `starts sent: ${starts.join(", ") || "none"}`);
    const sent = await until(() => handed(answered), 6000);
    check(named("the press held through the start sends the answer"), sent, "the step is not with its session");
    await page.screenshot({ path: path.join(work, `${how}-1-after-answer.png`) });
    // A lost press leaves the step on the card; it moves aside so the next checks still run.
    if (!sent) await aside(answered);

    // 2. A press that presses nothing: the running timer shows once it ends.
    await card(held).waitFor({ timeout: 10000 });
    const text = card(held).locator(".task-heading-text").first();
    await quietOn(held, text);
    await pressHeld(text, 1600);
    check(named("the start during a held press reached Cockpit"), starts.includes(held.id), `starts sent: ${starts.join(", ") || "none"}`);
    check(named("the running timer shows once the press ends"), await until(() => running(held), 2500), "the card shows no running timer");
    await page.screenshot({ path: path.join(work, `${how}-2-after-hold.png`) });

    // 3. No press: input alone starts the timer, which shows at once.
    await aside(held);
    await card(quiet).waitFor({ timeout: 15000 });
    await quietOn(quiet, card(quiet));
    if (phone) await page.mouse.wheel(0, 1);
    else await page.mouse.move(20, 20);
    check(named("with no press, the timer shows within two seconds"), await until(() => running(quiet), 2000),
      "the card shows no running timer");

    // 4. The user closed a step further down the queue, and Cockpit refuses that Done while the user holds an answer
    // on the card. The timer already runs, so the page starts none during this press: the refusal is the only redraw.
    const done = await refuseDone(later);
    await still();
    check(named("the closed step left the page"), !(await shows(later.id)), "the step still shows");
    const allow = card(quiet).locator(".answers button", { hasText: "Allow it" }).first();
    if (!phone) {
      const box = await boxOf(allow);
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    }
    const during = { refused: false, held: null, same: null, back: null };
    await pressHeld(allow, 1600, async () => {
      done.refuse();
      during.refused = await until(async () => done.refused && (await toastText()).includes("Not closed"), 1500);
      during.held = await page.evaluate(() => pressHeld());
      during.same = await page.evaluate(() => {
        const on = ui.pressedOn;
        return Boolean(on?.isConnected && on.closest(".answers button")?.textContent.includes("Allow it"));
      });
      during.back = await shows(later.id);
    });
    check(named("Cockpit's refusal came while the press was held"), during.refused && during.held === true,
      `toast: ${JSON.stringify(await toastText())}, held: ${during.held}`);
    check(named("the button under the press stays the same through the refusal"), during.same === true && during.back === false,
      `button still on the page: ${during.same}, refused step drawn during the press: ${during.back}`);
    check(named("the press held through the refusal sends the answer"), await until(() => handed(quiet), 6000),
      "the step is not with its session");
    check(named("the refused step comes back once the press ends"), await until(() => shows(later.id), 4000),
      "the page does not show the step");
    await page.unroute(`**/api/steps/${later.id}/done`);
    await page.screenshot({ path: path.join(work, `${how}-4-after-refusal.png`) });

    // 5. A refusal that comes while the user holds the button of the notice that shows. Take back is such a button,
    // and the notice it is on shows only for some layouts (handedNotice), so this one is made for the test.
    await still();
    const notice = await refuseDone(later);
    await page.evaluate(() => {
      window.noticePressed = false;
      toast("Sent to the test session", false, { label: "Take it back", run: () => { window.noticePressed = true; } });
    });
    const button = page.locator("#toast .toast-action", { hasText: "Take it back" });
    await button.waitFor({ timeout: 4000 });
    await still();
    if (!phone) {
      const box = await boxOf(button);
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    }
    const pressing = { refused: false, same: null, text: "", downAt: null };
    await pressHeld(button, 1600, async () => {
      pressing.downAt = await page.evaluate(() => ui.pressedAt);
      notice.refuse();
      pressing.refused = await until(async () => notice.refused
        && await page.evaluate(() => ui.toastWaiting?.text.includes("Not closed") === true), 1500);
      pressing.same = await page.evaluate(() => {
        const on = ui.pressedOn;
        return Boolean(on?.isConnected && on.closest("#toast .toast-action")?.textContent.includes("Take it back"));
      });
      pressing.text = await toastText();
    });
    check(named("the refusal's notice waits while the notice's button is held"),
      pressing.refused && pressing.same === true && pressing.text.includes("Sent to the test session"),
      `refused: ${pressing.refused}, button still on the page: ${pressing.same}, notice: ${JSON.stringify(pressing.text)}`);
    check(named("the press on the notice's button lands"), await until(() => page.evaluate(() => window.noticePressed), 2000),
      "the button's action did not run");
    // Before the limit (PRESS_HOLD_MS, 3 s from the press going down), which would show it without the press's end.
    const endShown = await until(async () => (await toastText()).includes("Not closed"), 2000)
      ? await page.evaluate((downAt) => performance.now() - downAt, pressing.downAt) : null;
    check(named("the refusal's notice shows once the press ends, before the limit"), endShown !== null && endShown < 3000,
      `notice: ${JSON.stringify(await toastText())}, shown ${endShown === null ? "never" : `${Math.round(endShown)} ms`} after the press went down`);
    await page.unroute(`**/api/steps/${later.id}/done`);

    // 6. A press on a notice's button held past the page's limit (PRESS_HOLD_MS, 3 s) holds notices back no longer.
    // A notice that comes 1.9 s into the press waits, and shows at the limit with the press still down. In a second
    // such press, a notice that comes after the limit shows at once.
    const pressNotice = async (midway) => {
      await still();
      await page.evaluate(() => { toast("Sent to the test session", false, { label: "Take it back", run: () => {} }); });
      const long = page.locator("#toast .toast-action", { hasText: "Take it back" });
      await long.waitFor({ timeout: 4000 });
      await still();
      if (!phone) {
        const box = await boxOf(long);
        await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      }
      await pressHeld(long, 3800, midway);
    };
    const pastLimit = async () => {
      const since = await page.evaluate(() => performance.now() - ui.pressedAt);
      await sleep(Math.max(0, 3300 - since));
    };
    const past = { waited: null, before: "", atLimit: "", held: null };
    await pressNotice(async () => {
      past.waited = await page.evaluate(() => { toast("First notice"); return ui.toastWaiting?.text ?? null; });
      past.before = await toastText();
      await pastLimit();
      past.atLimit = await toastText();
      past.held = await page.evaluate(() => ui.pressedAt !== null);
    });
    check(named("a notice that waited for a press shows at the limit, the press still down"),
      past.waited === "First notice" && past.before.includes("Sent to the test session") && past.atLimit.includes("First notice") && past.held === true,
      `waited: ${JSON.stringify(past.waited)}, shown then: ${JSON.stringify(past.before)}, at the limit: ${JSON.stringify(past.atLimit)}, press still down: ${past.held}`);
    const late = { shown: "", held: null };
    await pressNotice(async () => {
      await pastLimit();
      // Read in the same task as the notice comes: "at once" means before anything else runs.
      Object.assign(late, await page.evaluate(() => {
        toast("Second notice");
        return { shown: document.querySelector("#toast .toast-text")?.textContent ?? "", held: ui.pressedAt !== null };
      }));
    });
    check(named("a notice that comes after the limit shows at once, the press still down"),
      late.shown.includes("Second notice") && late.held === true, `notice: ${JSON.stringify(late.shown)}, press still down: ${late.held}`);

    // A second press restarts the limit. The first press on the notice ends without a click, as a long press on a
    // touch screen can, and the second goes down before the page counts the first as over (PRESS_CLICK_WAIT_MS). A
    // notice that came during the first waits until the limit of the second. Real input cannot end a press without a
    // click in both rounds, so these presses are dispatched to the notice's button.
    await still();
    await page.evaluate(() => { toast("Sent to the test session", false, { label: "Take it back", run: () => {} }); });
    await page.locator("#toast .toast-action", { hasText: "Take it back" }).waitFor({ timeout: 4000 });
    await still();
    const dispatch = (type) => page.evaluate((kind) => {
      document.querySelector("#toast .toast-action").dispatchEvent(new PointerEvent(kind, { bubbles: true, composed: true }));
      return ui.pressedAt;
    }, type);
    const sleepUntil = async (at) => {
      const now = await page.evaluate(() => performance.now());
      await sleep(Math.max(0, at - now));
    };
    const firstDown = await dispatch("pointerdown");
    await page.evaluate(() => toast("Waiting notice"));
    await sleep(1000);
    // The release and the second press go in one call into the page: two calls apart can take longer than the wait
    // on a loaded machine, and the page would count the first press as over.
    const secondDown = await page.evaluate(async () => {
      const button = document.querySelector("#toast .toast-action");
      button.dispatchEvent(new PointerEvent("pointerup", { bubbles: true, composed: true }));
      await new Promise((resolve) => setTimeout(resolve, 100));
      button.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, composed: true }));
      return ui.pressedAt;
    });
    await sleepUntil(firstDown + 3300);
    const atFirst = await page.evaluate(() => ({
      waiting: ui.toastWaiting?.text ?? null,
      shown: document.querySelector("#toast .toast-text")?.textContent ?? "",
    }));
    await sleepUntil(secondDown + 3300);
    const atSecond = await toastText();
    // The waiting notice, now shown, has no button: the press ends on the page.
    await page.evaluate(() => document.dispatchEvent(new PointerEvent("pointercancel")));
    check(named("a second press restarts the limit for a notice that waits"),
      atFirst.waiting === "Waiting notice" && atFirst.shown.includes("Sent to the test session") && atSecond.includes("Waiting notice"),
      `3.3 s after the first press: waiting ${JSON.stringify(atFirst.waiting)}, shown ${JSON.stringify(atFirst.shown)}; after the second: ${JSON.stringify(atSecond)}`);

    // 7. A press on the notice's text, which presses nothing: a notice that comes meanwhile waits, and shows as soon as
    // the press ends. Only the press's end shows it this soon; the limit would show it 1.4 s later.
    await still();
    await page.evaluate(() => { toast("Sent to the test session", false, { label: "Take it back", run: () => {} }); });
    const words = page.locator("#toast .toast-text", { hasText: "Sent to the test session" });
    await words.waitFor({ timeout: 4000 });
    await still();
    if (!phone) {
      const box = await boxOf(words);
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    }
    const onText = { waited: null, shown: "" };
    await pressHeld(words, 1600, async () => {
      onText.waited = await page.evaluate(() => { toast("Later notice"); return ui.toastWaiting?.text ?? null; });
      onText.shown = await toastText();
    });
    check(named("a notice that comes during a press on the notice's text waits"),
      onText.waited === "Later notice" && onText.shown.includes("Sent to the test session"),
      `waited: ${JSON.stringify(onText.waited)}, shown: ${JSON.stringify(onText.shown)}`);
    check(named("it shows as soon as that press ends"), await until(async () => (await toastText()).includes("Later notice"), 900),
      `notice: ${JSON.stringify(await toastText())}`);

    // 8. Add more on a hand-off's notice (offerFollowUp), held while another notice comes. The press opens the box, and
    // the notice that came waits behind it: shown, it would take the hand-off notice's place, and the message would go.
    await still();
    await page.evaluate(() => {
      window.noticesGone = [];
      const shown = toast("Sent to the test session", false,
        { label: "Take it back", run: () => {}, gone: () => window.noticesGone.push("hand-off") });
      offerFollowUp(shown, new Promise(() => {}), {}, "the test session", 0);
    });
    const more = page.locator("#toast .toast-more");
    await more.waitFor({ timeout: 4000 });
    await still();
    if (!phone) {
      const box = await boxOf(more);
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    }
    await pressHeld(more, 1600, async () => {
      await page.evaluate(() => {
        toast("Later notice", false, { label: "Look", run: () => {}, gone: () => window.noticesGone.push("later") });
      });
    });
    // Past the press's end and its limit, which would show a notice that still waited for the press.
    await sleep(2000);
    const behind = await page.evaluate(() => ({
      box: Boolean(document.querySelector("#toast:not([hidden]) .toast-follow")),
      waiting: ui.toastWaiting?.text ?? null,
      gone: window.noticesGone.slice(),
    }));
    check(named("a notice that came during the press on Add more waits behind the box it opened"),
      behind.box && behind.waiting === "Later notice" && behind.gone.length === 0,
      `box open: ${behind.box}, waiting: ${JSON.stringify(behind.waiting)}, left: ${JSON.stringify(behind.gone)}`);
    if (behind.box) await page.locator("#toast .toast-follow-cancel").click();
    check(named("it shows once the box closes"), await until(async () => (await toastText()).includes("Later notice"), 2000),
      `notice: ${JSON.stringify(await toastText())}`);

    // 9. A notice that waited behind the box still waits after Send, behind the hand-off's notice that says where the
    // words went. A newer notice then shows, and the one that waited leaves: it must not come back once that one goes.
    await still();
    await page.evaluate(() => {
      window.noticesGone = [];
      const shown = toast("Sent to the test session", false, { label: "Take it back", run: () => {} });
      offerFollowUp(shown, new Promise(() => {}), {}, "the test session", 0);
    });
    await page.locator("#toast .toast-more").click();
    await page.locator("#toast .toast-follow textarea").fill("More words for the test");
    await page.evaluate(() => {
      toast("Older notice", false, { label: "Look", run: () => {}, gone: () => window.noticesGone.push("older") });
    });
    await page.locator("#toast .toast-follow-send").click();
    await page.evaluate(() => toast("Newer notice"));
    const newer = await toastText();
    const later2 = await page.evaluate(() => { hideToast(); return document.getElementById("toast").hidden ? "" : document.getElementById("toast").innerText; });
    const left = await page.evaluate(() => window.noticesGone.slice());
    check(named("a notice that waited behind the box does not come back after a newer one"),
      newer.includes("Newer notice") && !later2.includes("Older notice") && left.includes("older"),
      `shown: ${JSON.stringify(newer)}, after it went: ${JSON.stringify(later2)}, left: ${JSON.stringify(left)}`);
    check(named("the page threw no errors"), errors.length === 0, errors.join(" | "));
  } finally {
    await context.close();
  }
}

function cookie(info) {
  return `${info.name}=${info.value}`;
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
