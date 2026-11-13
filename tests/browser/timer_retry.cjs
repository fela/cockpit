// A timer press goes through a connection that is not back yet.
// Checks, on the main page:
// - a play whose first two requests fail to connect shows at once, reaches Cockpit, and shows no failure;
// A pause is the page's own (idle, or another step taken up; the card has no Pause since VIEW-29).
// - presses made offline (pause one step, play another) show at once and reach Cockpit in the order made, each counted
//   from when it was made;
// - play, pause and play made offline leave the step running; pause, play and pause leave none running;
// - a play still offline a minute after it was made is given up unsent once online, shows "Timer not started" with how
//   long the page was away and Start now, goes back, is not logged as a page error, and Start now starts it;
// - a play Cockpit never receives shows "Timer not started" after about 30 s of tries, and the play goes back;
// - a pause still offline a minute after it was made shows as waiting, reaches Cockpit once online, and stops the timer
//   when it was pressed;
// - a pause Cockpit refuses shows "Timer not paused" with a Pause button, which pauses it;
// - a pause held offline while another page paused and started the step again moves the other page's pause back to its
//   press and leaves the new timer running.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
// A run takes about three minutes.
//
// Usage, from the checkout: node tests/browser/timer_retry.cjs

"use strict";

const { spawn, spawnSync } = require("node:child_process");
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
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "timer-test", "content-type": "application/json" };

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-timer-");
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
    const serverTimer = async () => ((await user("GET", "/api/state")).timer || {}).step_id || null;
    // Waits up to `ms` for Cockpit's running timer to be `want` (a step id, or null).
    const timerBecomes = async (want, ms) => {
      const end = Date.now() + ms;
      let now = await serverTimer();
      while (now !== want && Date.now() < end) {
        await sleep(250);
        now = await serverTimer();
      }
      return now;
    };

    // The fixture database's open steps are snoozed, so this run's two steps take the user's places.
    const initial = await user("GET", "/api/state");
    await Promise.all([...initial.queue, ...initial.ready_next, ...initial.snoozed].map((step) => user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
    const run = Date.now();
    const steps = {};
    for (const name of ["one", "two"]) {
      await agent("PUT", "/api/items", { key: `note:timer-${run}-${name}`, title: `Timer test: ${name}`, priority: "urgent" });
      const answer = await agent("POST", "/api/steps", {
        item_key: `note:timer-${run}-${name}`, need: `timer-${name}-${run}`, ask: `Timer test: ${name}`, score: 100,
        body: "Timer test body: the overlay over it is the timer's play.", session_id: SESSION_B,
      });
      steps[name] = answer.step.id;
    }

    browser = await chromium.launch({ executablePath: CHROMIUM });
    const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
    await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
    // Every toast the page shows, in order.
    await context.addInitScript(() => {
      window.__toasts = [];
      addEventListener("DOMContentLoaded", () => {
        const el = document.getElementById("toast");
        new MutationObserver(() => { if (!el.hidden) window.__toasts.push(el.textContent); }).observe(el, { childList: true, subtree: true, attributes: true });
      });
    });
    const page = await context.newPage();
    page.setDefaultTimeout(5000);
    const reports = [];
    page.on("request", (request) => {
      if (request.url().endsWith("/api/page-errors") && request.method() === "POST") reports.push(JSON.parse(request.postData()).message);
    });
    await page.goto(`${info.origin}/classic`);
    const card = (id) => page.locator(`[data-step="${id}"]`).first();
    await card(steps.one).waitFor();
    // The page starts Next up's timer itself on the user's first input; marked as started already, it leaves the
    // starts to this test's presses, so the press that opens the menu does not start it before Start the timer.
    await page.evaluate((id) => { ui.autoStarted = id; }, steps.one);
    // A step below Next up is a folded row: its title opens it, and Start the timer is in the step's ⋯ menu (VIEW-29).
    const play = async (id) => {
      const folded = card(id).locator('.row-title[aria-expanded="false"]');
      if (await folded.count()) await folded.click();
      await card(id).locator(".step-menu > summary").click();
      await card(id).locator('.step-menu [data-timer="start"]').click();
    };
    // The card has no Pause (VIEW-29): the page pauses a step itself, when the user goes idle or moves to another
    // step, through the same call; like a press, it returns once the page has made it, not when Cockpit answers.
    const pause = (id) => page.evaluate((stepId) => { void setTimer(stepId, false); }, id);
    // Answers Cockpit gave to timer presses, and a wait for their count to reach `count`.
    let answered = 0;
    page.on("response", (response) => { if (/\/timer\/(start|pause)$/.test(response.url())) answered += 1; });
    const answersReach = async (count, ms) => {
      const end = Date.now() + ms;
      while (answered < count && Date.now() < end) await sleep(100);
    };
    const runningOn = (id) => card(id).locator('.step-menu [data-timer="running"]').count().then((n) => n > 0);
    const toasts = () => page.evaluate(() => window.__toasts.slice());
    const failed = async () => (await toasts()).filter((text) => /Timer not/.test(text));

    // 1. The first two tries fail to connect, as on a phone whose connection is not back yet.
    let refusedStarts = 0;
    await page.route("**/timer/start", (route) => {
      if (refusedStarts < 2) { refusedStarts += 1; return route.abort("internetdisconnected"); }
      return route.continue();
    });
    await play(steps.one);
    await sleep(300);
    check("a play shows at once while its tries fail", await runningOn(steps.one), "the step shows no running timer");
    const afterRetry = await timerBecomes(steps.one, 15e3);
    check("the play reaches Cockpit once the connection is back", afterRetry === steps.one, `Cockpit's timer is on ${afterRetry}`);
    check("the failed tries made two requests", refusedStarts === 2, `${refusedStarts} were refused`);
    check("no failure shows", (await failed()).length === 0, (await failed()).join(" | "));
    await page.unroute("**/timer/start");

    // 2. Offline: pause step one, then play step two; once online they reach Cockpit in that order.
    await context.setOffline(true);
    await pause(steps.one);
    await sleep(1500);
    await play(steps.two);
    await sleep(300);
    check("presses made offline show at once", !(await runningOn(steps.one)) && await runningOn(steps.two), "the cards do not show step two running");
    await sleep(4000);
    check("nothing reaches Cockpit while offline", await serverTimer() === steps.one, `Cockpit's timer is on ${await serverTimer()}`);
    let before = answered;
    await context.setOffline(false);
    await answersReach(before + 2, 15e3);
    check("back online, step two runs", await serverTimer() === steps.two, `Cockpit's timer is on ${await serverTimer()}`);
    const [oneSpan] = spans(work, steps.one).slice(-1);
    check("the pause landed before the play", oneSpan.ended_by === "pause", `step one's span ended by ${oneSpan.ended_by}`);
    await sleep(1500);
    check("step two still runs on the page", await runningOn(steps.two), "the page lost the running timer");

    // 3. Offline: play, pause and play step one; once all reached Cockpit, it runs. Opening step one's ⋯ menu for
    // its first play is a press outside step two, which leaves step two's timer running (TRACK-6): the page sends no
    // pause for it, and Cockpit ends step two's span itself when step one's first play arrives. Three presses in all.
    const twoSpans = spans(work, steps.two).length;
    await context.setOffline(true);
    await play(steps.one);
    await sleep(300);
    await pause(steps.one);
    await sleep(300);
    await play(steps.one);
    await sleep(300);
    before = answered;
    await context.setOffline(false);
    await answersReach(before + 3, 15e3);
    await sleep(1500);
    check("play, pause and play made offline leave step one running", await serverTimer() === steps.one && await runningOn(steps.one),
      `Cockpit's timer is on ${await serverTimer()}`);
    check("each of the three reached Cockpit", answered === before + 3, `${answered - before} answers`);
    const twoEnded = spans(work, steps.two).slice(twoSpans - 1).map((span) => span.ended_by);
    check("step one's first play ended step two's span as a switch", JSON.stringify(twoEnded) === '["switch"]',
      `step two's spans since ended by ${JSON.stringify(twoEnded)}`);

    // 4. Offline: pause, then play and pause step one; once all three reached Cockpit, no timer runs.
    await context.setOffline(true);
    await pause(steps.one);
    await sleep(300);
    await play(steps.one);
    await sleep(300);
    await pause(steps.one);
    await sleep(300);
    before = answered;
    await context.setOffline(false);
    await answersReach(before + 3, 15e3);
    await sleep(1500);
    const lastTwo = spans(work, steps.one).slice(-2).map((span) => span.ended_by);
    check("pause, play and pause made offline leave no timer running", await serverTimer() === null && !(await runningOn(steps.one)),
      `Cockpit's timer is on ${await serverTimer()}`);
    check("the last play counted until the last pause", JSON.stringify(lastTwo) === '["pause","pause"]', `step one's last spans ended by ${lastTwo}`);

    // 5. Offline for over a minute after a play: once online the play is given up, unsent, and shows as failed, with how
    // long the page was away and Start now; it is the device's doing, not Cockpit's, so it is not logged.
    const reportsAway = reports.length;
    await context.setOffline(true);
    await play(steps.one);
    await sleep(62e3);
    before = answered;
    await context.setOffline(false);
    await sleep(2000);
    check("a play older than a minute is not sent", answered === before && await serverTimer() === null,
      `${answered - before} answers; Cockpit's timer is on ${await serverTimer()}`);
    check("the given-up play shows as failed", (await failed()).length === 1, (await failed()).join(" | ") || "no failure");
    check("the given-up play goes back", !(await runningOn(steps.one)), "step one still shows a running timer");
    check("the given-up play says how long the page was away", /^Timer not started: offline 1m/.test((await failed())[0] || ""),
      (await failed()).join(" | "));
    check("the given-up play is not logged as a page error", !reports.slice(reportsAway).some((message) => /Timer not started/.test(message)), JSON.stringify(reports));
    // Start now, on the toast, starts the timer afresh.
    await page.locator("#toast .toast-action", { hasText: "Start now" }).click();
    const startedNow = await timerBecomes(steps.one, 10e3);
    check("Start now starts the timer", startedNow === steps.one && await runningOn(steps.one), `Cockpit's timer is on ${startedNow}`);
    await pause(steps.one);
    await timerBecomes(null, 10e3);
    await sleep(1500);

    // 6. Cockpit never receives the play: after about 30 s of tries the failure shows and the play goes back.
    await page.route("**/timer/start", (route) => route.abort("connectionrefused"));
    const pressed = Date.now();
    const reportsBefore = reports.length;
    await play(steps.two);
    await sleep(300);
    check("the play shows while it is tried", await runningOn(steps.two), "the step shows no running timer");
    const end = Date.now() + 45e3;
    while ((await failed()).length < 2 && Date.now() < end) await sleep(500);
    const seconds = Math.round((Date.now() - pressed) / 1000);
    const shown = (await failed()).slice(1);
    check("the failure shows after the tries", shown.length === 1 && seconds >= 25, `after ${seconds} s: ${shown.join(" | ") || "no failure"}`);
    await sleep(1000);
    check("the play goes back", !(await runningOn(steps.two)), "step two still shows a running timer");
    check("the failure reaches the error log", reports.slice(reportsBefore).some((message) => /Timer not started: Failed to fetch/.test(message)), JSON.stringify(reports));
    await page.unroute("**/timer/start");

    // 7. Offline for over a minute after a pause: it waits, then reaches Cockpit and stops the timer at the press. A play
    // of step two made just after it waits behind the pause; once the pause lands that play is over a minute old, so it
    // is given up without a try. The page was offline all that time, so it says so and does not log it.
    await play(steps.one);
    check("step one runs again", await timerBecomes(steps.one, 15e3) === steps.one, `Cockpit's timer is on ${await serverTimer()}`);
    const failedBefore = (await failed()).length;
    const reportsQueued = reports.length;
    await context.setOffline(true);
    await pause(steps.one);
    const pausedAt = Date.now();
    await sleep(300);
    await play(steps.two);
    await sleep(65e3);
    const waiting = await page.locator("#unsent").evaluate((el) => el.hidden ? "" : el.textContent).catch(() => "");
    check("the pause shows as waiting to send", waiting === "1 waiting to send", `the header shows "${waiting}"`);
    check("Cockpit's timer runs while the pause is offline", await serverTimer() === steps.one, `Cockpit's timer is on ${await serverTimer()}`);
    await context.setOffline(false);
    const afterPause = await timerBecomes(null, 15e3);
    check("back online, the pause reaches Cockpit", afterPause === null, `Cockpit's timer is on ${afterPause}`);
    const [pausedSpan] = spans(work, steps.one).slice(-1);
    const late = Math.abs(Date.parse(pausedSpan.ended_at) - pausedAt) / 1000;
    check("the timer stopped when the pause was pressed", pausedSpan.ended_by === "pause" && late <= 3,
      `the span ended by ${pausedSpan.ended_by} at ${pausedSpan.ended_at}, ${late} s from the press`);
    await sleep(2000);
    const queuedFailed = (await failed()).slice(failedBefore);
    check("the late pause shows no failure, and the play behind it says the page was offline", queuedFailed.length === 1
      && /^Timer not started: offline 1m/.test(queuedFailed[0]), queuedFailed.join(" | ") || "no failure");
    check("the play behind the pause is not sent", await serverTimer() === null && !(await runningOn(steps.two)),
      `Cockpit's timer is on ${await serverTimer()}`);
    check("the play behind the pause is not logged as a page error",
      !reports.slice(reportsQueued).some((message) => /Timer not started/.test(message)), JSON.stringify(reports));
    check("the page shows step one paused", !(await runningOn(steps.one)), "step one still shows a running timer");

    // 8. Cockpit refuses a pause: the failure offers Pause, which pauses the timer.
    await play(steps.one);
    check("step one runs once more", await timerBecomes(steps.one, 15e3) === steps.one, `Cockpit's timer is on ${await serverTimer()}`);
    await page.route("**/timer/pause", (route) => route.fulfill({ status: 500, contentType: "application/json", body: '{"detail": "test refusal"}' }));
    await pause(steps.one);
    const retry = page.locator("#toast button", { hasText: "Pause" });
    const offered = await retry.waitFor({ timeout: 10e3 }).then(() => true, () => false);
    check("a refused pause shows the failure with Pause", offered && (await failed()).some((text) => /Timer not paused/.test(text)),
      (await failed()).join(" | ") || "no failure");
    check("the refused pause goes back", await runningOn(steps.one), "step one shows no running timer");
    await page.unroute("**/timer/pause");
    if (offered) await retry.click();
    const retried = await timerBecomes(null, 15e3);
    check("Pause on the failure pauses the timer", retried === null, `Cockpit's timer is on ${retried}`);

    // 9. A pause held offline while another page pauses and starts the step again: the span that ran at the press
    // ends at the press, and the one started since runs on.
    await play(steps.one);
    check("step one runs for the last time", await timerBecomes(steps.one, 15e3) === steps.one, `Cockpit's timer is on ${await serverTimer()}`);
    await context.setOffline(true);
    await pause(steps.one);
    const heldAt = Date.now();
    await sleep(5000);
    await user("POST", `/api/steps/${steps.one}/timer/pause`);
    await user("POST", `/api/steps/${steps.one}/timer/start`);
    const elsewhere = spans(work, steps.one).length;
    const heldAnswer = page.waitForResponse(/\/timer\/pause$/, { timeout: 15e3 }).then((response) => response.status(), () => "none");
    await context.setOffline(false);
    const heldStatus = await heldAnswer;
    await sleep(1500);
    const [atPress, since] = spans(work, steps.one).slice(-2);
    const heldLate = Math.abs(Date.parse(atPress.ended_at) - heldAt) / 1000;
    check("the held pause went through", heldStatus === 200, `Cockpit answered ${heldStatus}`);
    check("the held pause ends the span that ran at the press then", atPress.ended_by === "pause" && heldLate <= 2,
      `the span ended by ${atPress.ended_by} at ${atPress.ended_at}, ${heldLate} s from the press`);
    check("the held pause leaves the timer started elsewhere running", await serverTimer() === steps.one && spans(work, steps.one).length === elsewhere
      && since.ended_at === null, `Cockpit's timer is on ${await serverTimer()}`);
    // The page draws the confirmed pause until a state asked for after it arrives (it polls every 5 s).
    const shownEnd = Date.now() + 8000;
    while (!(await runningOn(steps.one)) && Date.now() < shownEnd) await sleep(250);
    check("the page shows step one running again", await runningOn(steps.one), "step one shows no running timer");
    console.error(`  toasts: ${(await toasts()).join(" | ")}`);
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failedChecks = results.filter((result) => !result.ok);
  console.error(failedChecks.length ? `${failedChecks.length} of ${results.length} checks failed` : `all ${results.length} checks passed`);
  process.exitCode = failedChecks.length ? 1 : 0;
}

// The step's time spans in the harness's synthetic database, oldest first.
function spans(work, stepId) {
  const script = "import json, sqlite3, sys; conn = sqlite3.connect(sys.argv[1]); conn.row_factory = sqlite3.Row; "
    + "print(json.dumps([dict(r) for r in conn.execute('SELECT started_at, ended_at, ended_by FROM time_spans WHERE step_id = ? ORDER BY id', (int(sys.argv[2]),))]))";
  const out = spawnSync(PYTHON, ["-c", script, path.join(work, "cockpit.db"), String(stepId)], { encoding: "utf8" });
  if (out.status !== 0) throw new Error(`reading the spans failed: ${out.stderr}`);
  return JSON.parse(out.stdout);
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
