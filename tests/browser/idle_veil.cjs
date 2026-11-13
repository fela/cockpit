// A step's timer starts when it opens, runs while Cockpit is in front, and stops when the user is away (UI-19,
// TRACK-5).

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
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "idle-veil-test", "content-type": "application/json" };
const BODY = "Idle veil test body: the whole step shows at once.";
const REPORT = `<!doctype html><html><body><h1>Idle veil report</h1>${"<p>A line of the report.</p>".repeat(40)}</body></html>`;

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const shots = process.argv[2] || null;
  if (shots) fs.mkdirSync(shots, { recursive: true });
  const work = workFolder("cockpit-idle-veil-");
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
    // The item's time, with the running span up to now (`seconds` counts the ended spans only).
    const itemSeconds = async (key) => {
      const spent = (await user("GET", "/api/state")).items.find((item) => item.key === key).time_spent;
      return spent.seconds + (spent.started_at ? (Date.now() - Date.parse(spent.started_at)) / 1000 : 0);
    };
    // The server's timer, polled until `done` holds or five seconds pass.
    const timerUntil = async (done) => {
      let state = null;
      for (let n = 0; n < 25; n += 1) {
        state = await user("GET", "/api/state");
        if (done(state.timer)) break;
        await sleep(200);
      }
      return state.timer;
    };

    // The fixture database's open steps are snoozed, so this run's step is Next up.
    const initial = await user("GET", "/api/state");
    await Promise.all([...initial.queue, ...initial.ready_next, ...initial.snoozed].map((step) => user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
    browser = await chromium.launch({ executablePath: CHROMIUM });

    for (const width of [1440, 390]) {
      const key = `note:idle-veil-${width}-${Date.now()}`;
      const item = await agent("PUT", "/api/items", { key, title: "Idle veil test", priority: "urgent" });
      const report = await agent("POST", `/api/items/${item.id}/pages`, { title: "Idle veil report", html: REPORT });
      const answer = await agent("POST", "/api/steps", {
        item_key: key, need: `review:idle-veil-${width}`, ask: "Idle veil test", score: 100, session_id: SESSION_B,
        body: `${BODY} Read [the report](${report.url}).`,
      });
      const id = answer.step.id;
      const open = async () => {
        const context = await browser.newContext({ viewport: { width, height: 900 }, isMobile: width < 600, hasTouch: width < 600 });
        await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
        const page = await context.newPage();
        page.setDefaultTimeout(30000);
        const errors = [];
        page.on("pageerror", (error) => errors.push(error.message));
        page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
        await page.clock.install();
        await page.goto(`${info.origin}/classic`);
        await page.locator(`#next [data-step="${id}"]`).waitFor({ timeout: 60000 });
        return { context, page, errors };
      };
      // One more tab in the same browser, sharing its storage and its clock.
      const another = async (context, errors) => {
        const page = await context.newPage();
        page.setDefaultTimeout(30000);
        page.on("pageerror", (error) => errors.push(error.message));
        await page.goto(`${info.origin}/classic`);
        await page.locator(`#next [data-step="${id}"]`).waitFor({ timeout: 60000 });
        return page;
      };
      // The page out of sight (another tab, a minimized window) or back on screen, as the browser tells it.
      const setHidden = (page, hidden) => page.evaluate((value) => {
        Object.defineProperty(document, "visibilityState", { configurable: true, get: () => (value ? "hidden" : "visible") });
        Object.defineProperty(document, "hidden", { configurable: true, get: () => value });
        document.dispatchEvent(new Event("visibilitychange"));
      }, hidden);
      // Another window has the focus, with the page still on screen behind it.
      const setFocus = (page, focused) => page.evaluate((value) => { document.hasFocus = () => value; }, focused);
      const box = (page) => page.locator("dialog#idle[open]");
      const veiled = (page) => page.evaluate(() => document.getElementById("idle-veil").matches(":popover-open"));
      // Whether the question showed on the page since `watchBox`, for a moment too.
      const watchBox = (page) => page.evaluate(() => {
        window.boxShown = Boolean(document.querySelector("dialog#idle[open]"));
        new MutationObserver(() => { if (document.querySelector("dialog#idle[open]")) window.boxShown = true; })
          .observe(document.body, { subtree: true, attributes: true, attributeFilter: ["open"], childList: true });
      });
      const boxShown = (page) => page.evaluate(() => window.boxShown);
      // The timer's start as the page draws it.
      const drawnStart = (page) => page.evaluate(() => { const timer = drawn(ui.state).timer; return timer && timer.started_at; });

      const first = await open();
      const page = first.page;
      const card = page.locator(`#next [data-step="${id}"]`);
      check(`${width}: Next up shows the whole step at once`, (await card.innerText()).includes(BODY), await card.innerText());
      await page.mouse.move(40, 300);
      await page.mouse.move(80, 320);
      const started = await timerUntil((timer) => timer && timer.step_id === id);
      check(`${width}: the first move on the page starts the step's timer`, started && started.step_id === id, JSON.stringify(started));

      // Five minutes in front with the mouse moving every 50 seconds, and no other input.
      for (let n = 0; n < 6; n += 1) {
        await page.clock.runFor("00:50");
        await page.mouse.move(200 + n * 10, 400);
      }
      await sleep(1500);
      const inFront = await user("GET", "/api/state");
      check(`${width}: five minutes in front with the mouse moving now and then leave the timer running`,
        inFront.timer && inFront.timer.step_id === id && inFront.timer.started_at === started.started_at, JSON.stringify(inFront.timer));
      check(`${width}: and show no veil and ask nothing`, !(await veiled(page)) && !(await box(page).count()), "veil or box shown");

      // In front with the mouse still: the user may be doing something else.
      await page.clock.runFor("01:10");
      check(`${width}: in front, a minute with the mouse still starts the veil`, await veiled(page), "no veil");
      await page.mouse.move(260, 380);
      check(`${width}: a move clears it`, !(await veiled(page)), "veil stays");

      await setFocus(page, false);
      await page.clock.runFor("01:10");
      check(`${width}: behind another window, a minute without the mouse starts the veil`, await veiled(page), "no veil");
      if (shots) await page.screenshot({ path: path.join(shots, `veil-${width}.png`) });
      await page.mouse.move(300, 400);
      await page.mouse.move(320, 420);
      check(`${width}: a move of the mouse clears the veil`, !(await veiled(page)), "veil stays");
      await page.clock.runFor("01:30");
      await sleep(1500);
      const moved = await user("GET", "/api/state");
      check(`${width}: after the move the timer runs on`,
        moved.timer && moved.timer.started_at === started.started_at && !(await box(page).count()), JSON.stringify(moved.timer));
      await page.clock.runFor("00:20");
      if (shots) {
        // The fade runs on the real clock, not the test's; show it as it looks 50 s in.
        await page.evaluate(() => { document.getElementById("idle-veil").style.animationDelay = "-50000ms"; });
        await page.screenshot({ path: path.join(shots, `veil-late-${width}.png`) });
      }
      await page.clock.runFor("00:20");
      const aside = await box(page).waitFor().then(() => true, () => false);
      check(`${width}: behind another window, two minutes without the mouse ask "Back after …"`, aside, "no box");
      check(`${width}: the question takes the veil away`, !(await veiled(page)), "veil stays");
      if (aside) await box(page).getByRole("button", { name: "Return" }).click();
      // Return's toast offers Undo, which counts the time away after all. The pointer on the toast holds it, as on the
      // way to its button.
      const toastBox = page.locator("#toast:not([hidden])");
      const offered = await toastBox.waitFor().then(() => toastBox.innerText(), () => "");
      const boxAfterReturn = await box(page).count();
      check(`${width}: Return closes the box`, !boxAfterReturn, `box still open; toast: ${JSON.stringify(offered)}`);
      await toastBox.hover({ timeout: 5000 }).catch(() => {});
      const running = await timerUntil((timer) => timer && timer.step_id === id && timer.started_at !== started.started_at);
      check(`${width}: Return runs the timer on`, running && running.step_id === id, JSON.stringify(running));
      check(`${width}: Return's toast says how long was left out and offers Undo`,
        /^Left out \S.* away/.test(offered.trim()) && (await toastBox.getByRole("button", { name: "Undo" }).count()) === 1, offered);
      if (shots) await page.screenshot({ path: path.join(shots, `returned-${width}.png`) });
      const undone = page.waitForResponse((response) => response.url().endsWith("/timer/answer/undo"));
      await toastBox.getByRole("button", { name: "Undo" }).click();
      const undoAnswer = await (await undone).json();
      check(`${width}: Undo counts the time away`, undoAnswer.undone === true, JSON.stringify(undoAnswer));
      check(`${width}: Undo closes the toast`, !(await page.locator("#toast:not([hidden])").getByRole("button", { name: "Undo" }).count()), "Undo still shown");
      const afterUndo = await user("GET", "/api/state");
      check(`${width}: Undo leaves the timer running`, afterUndo.timer && afterUndo.timer.started_at === running.started_at, JSON.stringify(afterUndo.timer));
      await setFocus(page, true);

      await setHidden(page, true);
      await page.clock.runFor("02:10");
      await box(page).waitFor();
      const title = (await box(page).locator("h2").innerText()).trim();
      check(`${width}: two minutes out of sight ask "Back after …"`, /^Back after \S/.test(title), title);
      const text = (await box(page).innerText()).trim().split(/\s*\n\s*/).filter(Boolean);
      check(`${width}: the box says only the time away and its two buttons`, text.length === 3, JSON.stringify(text));
      const keys = await box(page).locator("button").allInnerTexts();
      check(`${width}: the box offers Return and Add`, keys.length === 2 && keys[0].trim() === "Return" && /^Add \+\S/.test(keys[1].trim()), JSON.stringify(keys));
      const stopped = await timerUntil((timer) => timer === null);
      check(`${width}: the timer stopped`, stopped === null, JSON.stringify(stopped));
      const backdrop = await page.evaluate(() => getComputedStyle(document.getElementById("idle"), "::backdrop").backgroundColor);
      check(`${width}: the dialog's backdrop dims the page`, !/^(transparent|rgba\(0, 0, 0, 0\))$/.test(backdrop), backdrop);
      await setHidden(page, false);
      if (shots) await page.screenshot({ path: path.join(shots, `asked-${width}.png`) });

      await box(page).getByRole("button", { name: /^Add/ }).click();
      const added = await timerUntil((timer) => timer && timer.step_id === id);
      check(`${width}: Add closes the box`, !(await box(page).count()), "still open");
      check(`${width}: Add runs the stopped timer on, so the time without activity counts`,
        added && added.started_at === running.started_at, `${JSON.stringify(added)} after ${running.started_at}`);

      // The page sleeps ten minutes with no check run, and wakes to a move of the mouse: first its state has Add's
      // input, as it would after the two minutes a stretch takes.
      const counted = (await user("GET", "/api/state")).input.count;
      await page.waitForFunction((count) => ui.state.input.count >= count, counted);
      // On a loaded machine Playwright's pauseAt can move the clock to its target and still refuse ("Cannot
      // fast-forward to the past"); it goes through from the new time.
      let now = 0;
      for (let tries = 0; ; tries += 1) {
        now = await page.evaluate(() => Date.now());
        const refused = await page.clock.pauseAt(now + 30000).then(() => null, (error) => error);
        if (!refused) break;
        if (tries >= 3) throw refused;
      }
      await page.clock.setSystemTime(now + 10 * 60 * 1000);
      await page.mouse.move(120, 500);
      await page.clock.resume();
      const woke = await box(page).waitFor().then(() => true, () => false);
      check(`${width}: an input after a gap asks about the gap`, woke, "no box");
      await timerUntil((timer) => timer === null);
      await page.reload({ timeout: 60000 });
      const reloaded = await box(page).waitFor({ timeout: 60000 }).then(() => true, () => false);
      check(`${width}: a reload asks the same question again`, reloaded, "no box after the reload");
      await box(page).getByRole("button", { name: "Return" }).click();
      const back = await timerUntil((timer) => timer && timer.step_id === id);
      check(`${width}: Return after the reload runs the timer on`, back && back.step_id === id, JSON.stringify(back));

      // A Cockpit page opened from the step counts as the step: beside its card at laptop width, over the whole
      // window (a modal dialog) at phone width.
      {
        await card.locator(`a[href*="/pages?id=${report.id}"]:visible`).first().click();
        const layer = page.locator("dialog.page-overlay[open] iframe.page-overlay-frame:not(.page-overlay-next)");
        await layer.waitFor();
        await sleep(1500);
        const frame = await layer.boundingBox();
        // Return's own toast may still show: only a toast the click raised counts.
        const toasts = await page.evaluate(() => ui.toastCount);
        await page.mouse.click(frame.x + frame.width / 2, frame.y + frame.height / 2);
        await sleep(500);
        const clicked = await user("GET", "/api/state");
        const raised = await page.evaluate((count) => ui.toastCount > count && document.getElementById("toast").textContent, toasts);
        check(`${width}: a click in the page leaves the timer running`,
          clicked.timer && clicked.timer.started_at === back.started_at && !raised,
          `${JSON.stringify(clicked.timer)} after ${back.started_at}, toast ${JSON.stringify(raised)}`);
        const focused = await page.evaluate(() => document.hasFocus() && document.activeElement.matches("dialog.page-overlay iframe"));
        check(`${width}: the focus is in the page`, focused, "focus elsewhere");
        // In steps with real time between them: the page in the layer reads Cockpit's state as it goes, and a jump of
        // minutes would time its requests out. The mouse moves over the page every 45 seconds, and the wheel turns
        // over it once.
        for (let n = 0; n < 100; n += 1) {
          await page.clock.runFor("00:03");
          await sleep(300);
          if (n % 15 === 14) await page.mouse.move(frame.x + frame.width / 2 + (n % 30), frame.y + frame.height / 3);
          if (n === 50) await page.mouse.wheel(0, 200);
        }
        await sleep(1500);
        const reading = await user("GET", "/api/state");
        check(`${width}: five minutes reading the page, the mouse moving now and then, leave the timer running, with no veil`,
          reading.timer && reading.timer.started_at === back.started_at && !(await veiled(page)) && !(await box(page).count()),
          JSON.stringify(reading.timer));
        if (shots) await page.screenshot({ path: path.join(shots, `page-${width}.png`) });

        // The mouse still over the page: the veil comes over the page too, and a move over the page clears it.
        for (let n = 0; n < 24; n += 1) {
          await page.clock.runFor("00:03");
          await sleep(300);
        }
        check(`${width}: with the mouse still over the page, a minute starts the veil`, await veiled(page), "no veil");
        if (shots) await page.screenshot({ path: path.join(shots, `page-veil-${width}.png`) });
        await page.mouse.move(frame.x + frame.width / 2 + 10, frame.y + frame.height / 2 + 10);
        await page.mouse.move(frame.x + frame.width / 2 + 30, frame.y + frame.height / 2 + 30);
        check(`${width}: a move of the mouse over the page clears the veil`, !(await veiled(page)), "veil stays");

        // A key pressed in the page, the mouse still, counts as well: 45 s, a key, 45 s more show no veil.
        for (let n = 0; n < 30; n += 1) {
          await page.clock.runFor("00:03");
          await sleep(300);
          if (n === 14) await page.keyboard.press("ArrowDown");
        }
        check(`${width}: a key pressed in the page keeps the veil away`, !(await veiled(page)), "veil shown");

        // Back to the window by the keyboard, with no move: the veil goes at once, before the next second's check.
        await page.mouse.move(frame.x + frame.width / 2 - 20, frame.y + frame.height / 2);
        await setFocus(page, false);
        for (let n = 0; n < 24; n += 1) {
          await page.clock.runFor("00:03");
          await sleep(300);
        }
        const before = await veiled(page);
        await setFocus(page, true);
        await page.evaluate(() => window.dispatchEvent(new FocusEvent("focus")));
        check(`${width}: back to the window, the veil goes at once`, before && !(await veiled(page)), `veiled before: ${before}`);
        const after = await user("GET", "/api/state");
        check(`${width}: and the timer runs on`, after.timer && after.timer.started_at === back.started_at, JSON.stringify(after.timer));
        await page.keyboard.press("Escape");
      }
      check(`${width}: no page errors`, first.errors.length === 0, first.errors.join(" | "));
      await first.context.close();

      // A page opens on the timer left running, its clock ten minutes on, and gets an input before Cockpit's state came.
      const late = await browser.newContext({ viewport: { width, height: 900 }, isMobile: width < 600, hasTouch: width < 600 });
      await late.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const latePage = await late.newPage();
      latePage.setDefaultTimeout(30000);
      // The same device as the first browser: its activity is under the key other tabs read.
      await latePage.addInitScript((at) => localStorage.setItem("cockpit.activity", String(at)), Date.now());
      await latePage.clock.install({ time: Date.now() + 10 * 60 * 1000 });
      // Only the first state comes late: taking the route away while a request waits on it fails in Playwright.
      let slow = true;
      await late.route("**/api/state", async (route) => {
        if (slow) await sleep(3000);
        await route.continue();
      });
      await latePage.goto(`${info.origin}/classic`, { waitUntil: "domcontentloaded" });
      await latePage.mouse.move(60, 200);
      await latePage.mouse.move(90, 240);
      const lateAsked = await box(latePage).waitFor().then(() => true, () => false);
      check(`${width}: an input before the page's first state still asks about the time before it`, lateAsked, "no box");
      slow = false;
      if (lateAsked) await box(latePage).getByRole("button", { name: "Return" }).click();
      await timerUntil((timer) => timer && timer.step_id === id);
      await late.close();

      // A new browser with two tabs, so its clock starts with the server's again.
      const second = await open();
      const other = await another(second.context, second.errors);
      await second.page.bringToFront();
      await second.page.mouse.move(40, 300);
      await second.page.mouse.move(80, 320);
      const secondStart = await timerUntil((timer) => timer && timer.step_id === id);
      await second.page.clock.fastForward("03:10");
      await box(second.page).waitFor();
      await other.bringToFront();
      const both = await box(other).waitFor().then(() => true, () => false);
      check(`${width}: the other tab asks too`, both, "no box in the other tab");
      const answers = [second.page, other].map((tab) => tab.waitForResponse((response) => response.url().endsWith("/timer/answer"))
        .then((response) => response.json()));
      await Promise.all([second.page, other].map((tab) => box(tab).getByRole("button", { name: /^Add/ }).click()));
      const taken = (await Promise.all(answers)).map((answer) => answer.answered);
      check(`${width}: Add in both tabs counts the time once`, taken.length === 2 && taken.filter(Boolean).length === 1, JSON.stringify(taken));
      const closed = await Promise.all([second.page, other].map((tab) => tab.waitForFunction(() => !document.querySelector("dialog#idle[open]"))
        .then(() => true, () => false)));
      check(`${width}: Add closes the question in both tabs`, closed.every(Boolean), JSON.stringify(closed));
      // Both tabs' states have the Add, as they would after the two minutes the next stretch takes.
      const addCounted = (await user("GET", "/api/state")).input.count;
      await Promise.all([second.page, other].map((tab) => tab.waitForFunction((count) => ui.state.input.count >= count, addCounted)));

      // Cockpit takes the next stops (one from each tab) only once the test lets them through, and the test notes what
      // reaches it.
      let release = null;
      const held = new Promise((resolve) => { release = resolve; });
      const sent = [];
      await second.context.route("**/timer/away", async (route) => {
        sent.push("away");
        await held;
        await route.continue();
      });
      await second.context.route(/\/timer\/(answer|start)$/, async (route) => {
        sent.push(route.request().url().split("/").pop());
        await route.continue();
      });
      await second.page.bringToFront();
      await second.page.clock.fastForward("03:10");
      await box(second.page).waitFor();
      if (shots) await second.page.screenshot({ path: path.join(shots, `asked-again-${width}.png`) });
      const beforeReturn = await itemSeconds(key);
      const returnedAt = Date.now();
      await box(second.page).getByRole("button", { name: "Return" }).click();
      await sleep(1000);
      check(`${width}: Return waits for the stop on its way`, sent.length > 0 && sent.every((name) => name === "away"), JSON.stringify(sent));
      release();
      const returned = await timerUntil((timer) => timer && timer.step_id === id && timer.started_at !== secondStart.started_at);
      check(`${width}: then Return runs the timer on from now`, returned && returned.step_id === id && returned.started_at !== secondStart.started_at,
        JSON.stringify(returned));
      check(`${width}: the stop reached Cockpit before the answer`, sent.at(-1) === "answer" && sent.slice(0, -1).every((name) => name === "away"),
        JSON.stringify(sent));
      // The test's clock moved 3:10 in the page only, so the server's timer ran just the real seconds that passed.
      const returnedSeconds = (await itemSeconds(key)) - beforeReturn;
      const passed = (Date.now() - returnedAt) / 1000;
      check(`${width}: Return counts only the time that passed`, returnedSeconds >= -5 && returnedSeconds <= passed + 2,
        `${Math.round(returnedSeconds)} s in ${Math.round(passed)} s`);
      check(`${width}: no page errors on the second browser`, second.errors.length === 0, second.errors.join(" | "));
      await second.context.close();

      // Back on another device: the question waits on the first, and the second asks nothing, before an input or after.
      const near = await open();
      await near.page.mouse.move(40, 300);
      await near.page.mouse.move(80, 320);
      const nearStart = await timerUntil((timer) => timer && timer.step_id === id);
      await near.page.clock.fastForward("03:10");
      await box(near.page).waitFor();
      await timerUntil((timer) => timer === null);
      // Its clock back with the other devices', as two devices' clocks agree: three minutes ahead, it would take the
      // input on another device for a new stretch away.
      await near.page.clock.setSystemTime(Date.now());
      const far = await open();
      await watchBox(far.page);
      await sleep(2000);
      check(`${width}: another device shows no question before the user is back on it`, !(await boxShown(far.page)), "box shown");
      const farFrom = new Date(Date.now() - 1000).toISOString();
      await far.page.mouse.move(40, 300);
      const farDrawn = await drawnStart(far.page);
      check(`${width}: back on another device, the timer runs from now at once`, farDrawn !== null && farDrawn >= farFrom, String(farDrawn));
      const farTimer = await timerUntil((timer) => timer && timer.step_id === id && timer.started_at !== nearStart.started_at);
      check(`${width}: and Cockpit has it running from then`, farTimer && farTimer.started_at >= farFrom, JSON.stringify(farTimer));
      const farToast = far.page.locator("#toast:not([hidden])");
      const farOffered = await farToast.waitFor().then(() => farToast.innerText(), () => "");
      check(`${width}: its toast says how long was left out and offers Undo`,
        /^Left out \S.* away/.test(farOffered.trim()) && (await farToast.getByRole("button", { name: "Undo" }).count()) === 1, farOffered);
      if (shots) await far.page.screenshot({ path: path.join(shots, `other-device-${width}.png`) });
      await sleep(1500);
      check(`${width}: with no question there, even for a moment`, !(await boxShown(far.page)), "box shown");
      const nearClosed = await near.page.waitForFunction(() => !document.querySelector("dialog#idle[open]")).then(() => true, () => false);
      check(`${width}: the question on the first device goes`, nearClosed, "box stays");
      check(`${width}: no page errors on the two devices`, near.errors.length + far.errors.length === 0, [...near.errors, ...far.errors].join(" | "));
      await near.context.close();

      // More than an hour away on the device the user was on: the question it showed goes, and the next input runs
      // the timer from then.
      await far.page.clock.fastForward("03:10");
      await box(far.page).waitFor();
      await timerUntil((timer) => timer === null);
      // The user back at the question after seven minutes, and gone again without answering.
      await far.page.clock.fastForward("07:00");
      await far.page.mouse.move(100, 450);
      await sleep(1500);
      check(`${width}: back within the hour, the question stays`, (await box(far.page).count()) === 1, "box gone");
      await far.page.clock.fastForward("51:00");
      const gone = await far.page.waitForFunction(() => !document.querySelector("dialog#idle[open]")).then(() => true, () => false);
      check(`${width}: after an hour the question goes`, gone, "box stays");
      await watchBox(far.page);
      const hourFrom = new Date(Date.now() - 1000).toISOString();
      await far.page.mouse.move(120, 500);
      const hourTimer = await timerUntil((timer) => timer && timer.step_id === id);
      check(`${width}: and the next input runs the timer from then`, hourTimer && hourTimer.started_at >= hourFrom, JSON.stringify(hourTimer));
      check(`${width}: with no question`, !(await boxShown(far.page)), "box shown");

      // The page asleep for more than an hour, waking to an input: no question, and the timer runs from then at once.
      const hourCounted = (await user("GET", "/api/state")).input.count;
      await far.page.waitForFunction((count) => ui.state.input.count >= count, hourCounted);
      let farNow = 0;
      for (let tries = 0; ; tries += 1) {
        farNow = await far.page.evaluate(() => Date.now());
        const refused = await far.page.clock.pauseAt(farNow + 30000).then(() => null, (error) => error);
        if (!refused) break;
        if (tries >= 3) throw refused;
      }
      await far.page.clock.setSystemTime(farNow + 61 * 60 * 1000);
      const sleptFrom = new Date(Date.now() - 1000).toISOString();
      // Cockpit hears of the stop and the answer only once the page shows the timer running: nothing visible waits on
      // them.
      let letStopsThrough = null;
      const stopsHeld = new Promise((resolve) => { letStopsThrough = resolve; });
      const timerCalls = /\/timer\/(away|answer)$/;
      await far.context.route(timerCalls, async (route) => {
        await stopsHeld;
        await route.continue();
      });
      await far.page.mouse.move(160, 520);
      await far.page.clock.resume();
      const sleptDrawn = await far.page.waitForFunction((from) => {
        const timer = drawn(ui.state).timer;
        return timer && Date.parse(timer.started_at) >= from;
      }, farNow + 61 * 60 * 1000 - 1000).then(() => true, () => false);
      check(`${width}: woken after more than an hour, the page shows the timer running from then before Cockpit answers`, sleptDrawn,
        String(await drawnStart(far.page)));
      letStopsThrough();
      const slept = await timerUntil((timer) => timer && timer.step_id === id && timer.started_at !== hourTimer.started_at);
      await far.context.unroute(timerCalls);
      check(`${width}: and Cockpit runs it from then`, slept && slept.started_at >= sleptFrom, JSON.stringify(slept));
      await sleep(1500);
      check(`${width}: with no question, even for a moment`, !(await boxShown(far.page)), "box shown");

      // Asleep for more than an hour again, and the page's check stops the timer before any input: an input while that
      // stop is on its way still runs the timer from then.
      let letStopThrough = null;
      const stopHeld = new Promise((resolve) => { letStopThrough = resolve; });
      await far.context.route("**/timer/away", async (route) => {
        await stopHeld;
        await route.continue();
      });
      const awakeCounted = (await user("GET", "/api/state")).input.count;
      await far.page.waitForFunction((count) => ui.state.input.count >= count, awakeCounted);
      for (let tries = 0; ; tries += 1) {
        farNow = await far.page.evaluate(() => Date.now());
        const refused = await far.page.clock.pauseAt(farNow + 30000).then(() => null, (error) => error);
        if (!refused) break;
        if (tries >= 3) throw refused;
      }
      await far.page.clock.setSystemTime(farNow + 61 * 60 * 1000);
      await far.page.clock.resume();
      const stopping = await far.page.waitForFunction(() => ui.stopping).then(() => true, () => false);
      const stopFrom = new Date(Date.now() - 1000).toISOString();
      const movedAt = await far.page.evaluate(() => Date.now());
      await far.page.mouse.move(200, 540);
      const runningFirst = await far.page.waitForFunction((at) => {
        const timer = drawn(ui.state).timer;
        return timer && Date.parse(timer.started_at) >= at - 1000;
      }, movedAt, { timeout: 5000 }).then(() => true, () => false);
      check(`${width}: the page shows it running from then before its stop went through`, runningFirst,
        String(await drawnStart(far.page)));
      letStopThrough();
      const during = await timerUntil((timer) => timer && timer.step_id === id && timer.started_at !== slept.started_at);
      await far.context.unroute("**/timer/away");
      check(`${width}: an input while the page's stop is on its way runs the timer from then`,
        stopping && during && during.started_at >= stopFrom, `stopping ${stopping}, ${JSON.stringify(during)}`);
      await sleep(1500);
      check(`${width}: with no question then either`, !(await boxShown(far.page)), "box shown");
      check(`${width}: no page errors after the hour`, far.errors.length === 0, far.errors.join(" | "));
      await far.context.close();

      // Back on another device whose page gets the input before Cockpit's state came: it asks nothing either.
      const home = await open();
      await home.page.mouse.move(40, 300);
      await home.page.mouse.move(80, 320);
      const homeStart = await timerUntil((timer) => timer && timer.step_id === id && timer.started_at !== during.started_at);
      await home.page.clock.fastForward("03:10");
      await box(home.page).waitFor();
      await timerUntil((timer) => timer === null);
      await home.page.clock.setSystemTime(Date.now());
      const early = await browser.newContext({ viewport: { width, height: 900 }, isMobile: width < 600, hasTouch: width < 600 });
      await early.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const earlyPage = await early.newPage();
      earlyPage.setDefaultTimeout(30000);
      const earlyErrors = [];
      earlyPage.on("pageerror", (error) => earlyErrors.push(error.message));
      await earlyPage.addInitScript(() => {
        window.boxShown = false;
        addEventListener("DOMContentLoaded", () => new MutationObserver(() => {
          if (document.querySelector("dialog#idle[open]")) window.boxShown = true;
        }).observe(document.body, { subtree: true, attributes: true, attributeFilter: ["open"], childList: true }));
      });
      let earlySlow = true;
      await early.route("**/api/state", async (route) => {
        if (earlySlow) await sleep(3000);
        await route.continue();
      });
      await earlyPage.goto(`${info.origin}/classic`, { waitUntil: "domcontentloaded" });
      const earlyFrom = new Date(Date.now() - 1000).toISOString();
      await earlyPage.mouse.move(60, 200);
      await earlyPage.mouse.move(90, 240);
      earlySlow = false;
      const earlyTimer = await timerUntil((timer) => timer && timer.step_id === id && timer.started_at !== homeStart.started_at);
      check(`${width}: an input before the first state on another device runs the timer from then`,
        earlyTimer && earlyTimer.started_at >= earlyFrom, JSON.stringify(earlyTimer));
      await sleep(1500);
      check(`${width}: and that device shows no question, even for a moment`, !(await boxShown(earlyPage)), "box shown");
      check(`${width}: no page errors on the device opened late`, earlyErrors.length + home.errors.length === 0,
        [...earlyErrors, ...home.errors].join(" | "));
      await early.close();
      await home.context.close();

      // Back within the hour on another device, whose own check stops the timer: an input there while that stop is on
      // its way shows the timer running at once, with no question.
      const work = await open();
      await work.page.mouse.move(40, 300);
      await work.page.mouse.move(80, 320);
      await timerUntil((timer) => timer && timer.step_id === id && timer.started_at !== earlyTimer.started_at);
      await work.context.close();
      const idler = await open();
      await watchBox(idler.page);
      let letAsideStop = null;
      const asideHeld = new Promise((resolve) => { letAsideStop = resolve; });
      await idler.context.route("**/timer/away", async (route) => {
        await asideHeld;
        await route.continue();
      });
      await idler.page.clock.fastForward("03:10");
      const asideStopping = await idler.page.waitForFunction(() => ui.stopping).then(() => true, () => false);
      const asideFrom = new Date(Date.now() - 1000).toISOString();
      const asideAt = await idler.page.evaluate(() => Date.now());
      await idler.page.mouse.move(60, 300);
      const asideRunning = await idler.page.waitForFunction((at) => {
        const timer = drawn(ui.state).timer;
        return timer && Date.parse(timer.started_at) >= at - 1000;
      }, asideAt, { timeout: 5000 }).then(() => true, () => false);
      check(`${width}: back on another device while its stop is on its way, the timer shows running at once`,
        asideStopping && asideRunning, `stopping ${asideStopping}, drawn ${await drawnStart(idler.page)}`);
      await sleep(1500);
      check(`${width}: with no question while the stop waits`, !(await boxShown(idler.page)), "box shown");
      letAsideStop();
      const asideTimer = await timerUntil((timer) => timer && timer.step_id === id && timer.started_at >= asideFrom);
      await idler.context.unroute("**/timer/away");
      check(`${width}: and Cockpit runs it from then`, Boolean(asideTimer), JSON.stringify(asideTimer));
      await sleep(1500);
      check(`${width}: with no question after the stop either`, !(await boxShown(idler.page)), "box shown");
      check(`${width}: no page errors on that device`, idler.errors.length + work.errors.length === 0,
        [...idler.errors, ...work.errors].join(" | "));
      await idler.context.close();

      // Back on another device with two tabs open: while the Return from one is on its way, the other asks nothing.
      const desk = await open();
      await desk.page.mouse.move(40, 300);
      await desk.page.mouse.move(80, 320);
      const deskStart = await timerUntil((timer) => timer && timer.step_id === id && timer.started_at !== asideTimer.started_at);
      await desk.page.clock.fastForward("03:10");
      await box(desk.page).waitFor();
      await timerUntil((timer) => timer === null);
      await desk.page.clock.setSystemTime(Date.now());
      const laptop = await open();
      const laptopTab = await another(laptop.context, laptop.errors);
      for (const tab of [laptop.page, laptopTab]) {
        await tab.waitForFunction(() => Boolean(ui.state.away));
        await watchBox(tab);
      }
      let letAnswerThrough = null;
      const answerHeld = new Promise((resolve) => { letAnswerThrough = resolve; });
      await laptop.context.route("**/timer/answer", async (route) => {
        await answerHeld;
        await route.continue();
      });
      // The user comes back to it a while after leaving the first device.
      await laptop.page.clock.fastForward("03:10");
      const laptopFrom = new Date(Date.now() - 1000).toISOString();
      await laptop.page.mouse.move(60, 300);
      await sleep(2500);
      check(`${width}: back on a device with two tabs, the other tab asks nothing while the Return is on its way`,
        !(await boxShown(laptopTab)) && !(await boxShown(laptop.page)), `tab ${await boxShown(laptopTab)}, page ${await boxShown(laptop.page)}`);
      letAnswerThrough();
      const laptopTimer = await timerUntil((timer) => timer && timer.step_id === id && timer.started_at >= laptopFrom
        && timer.started_at !== deskStart.started_at);
      await laptop.context.unroute("**/timer/answer");
      check(`${width}: and the Return runs the timer from then`, Boolean(laptopTimer), JSON.stringify(laptopTimer));
      await sleep(1500);
      check(`${width}: with no question in either tab after it`, !(await boxShown(laptopTab)) && !(await boxShown(laptop.page)), "box shown");
      check(`${width}: no page errors on the two-tab device`, laptop.errors.length + desk.errors.length === 0,
        [...laptop.errors, ...desk.errors].join(" | "));
      await laptop.context.close();
      await desk.context.close();

      await user("POST", `/api/steps/${id}/timer/pause`);
      // Snoozed, it leaves Next up to the next width's step.
      await user("POST", `/api/steps/${id}/snooze`, { until: "2099-01-01T08:00:00Z" });
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
  const end = Date.now() + 180e3;
  while (Date.now() < end) {
    if (server.exitCode !== null) throw new Error(`serve_preview.py exited with ${server.exitCode}`);
    if (fs.existsSync(file)) {
      const info = JSON.parse(fs.readFileSync(file, "utf8"));
      const up = await fetch(`${info.origin}/healthz`).then((response) => response.ok, () => false);
      if (up) return info;
    }
    await sleep(200);
  }
  throw new Error("serve_preview.py did not come up within 180 s");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
