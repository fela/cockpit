// After a snooze on the page with the list at its left, the Undo sits in Waiting, as a Send's sits in Busy
// (send_undo_busy.cjs). On a synthetic database, at 1440 px:
// - Z on Next up shows no notice at the foot, and a copy of the card glides into Waiting;
// - Waiting, folded, lights up, its count rolls up by one, and an Undo sits at its heading's right;
// - that Undo unsnoozes the step, back into the queue, and Waiting stays folded;
// - with Waiting open, the step's row under Snoozed by you is the Undo, already before Cockpit answers the snooze,
//   and it unsnoozes the step;
// - once the Undo time ends the heading is plain again and the step stays snoozed;
// - an item with another step in Inbox has no row in Waiting for the snoozed step, so the notice offers Undo;
// - so does an item with another step already snoozed, whose row Waiting may show in its place, and a snooze typed
//   in words, whose time Cockpit has yet to read;
// - on a phone, the Undo sits on the bar's Your items and unsnoozes the step;
// - no page errors.
// Saves screenshots of the heading, the row and the glide, light and dark, in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/snooze_undo_waiting.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// The harness's agent token (tests/conftest.py TOKEN) and its second fake session.
const AGENT_TOKEN = "t".repeat(40);
const SESSION = "22222222-2222-4222-8222-222222222222";

async function main() {
  const work = workFolder("cockpit-snooze-undo-");
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
    const agent = { authorization: `Bearer ${AGENT_TOKEN}`, "x-cockpit-as": "snooze-undo-test", "content-type": "application/json" };
    const call = async (method, url, headers, body) => {
      const response = await fetch(`${info.origin}${url}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
      const text = await response.text();
      if (!response.ok) throw new Error(`${method} ${url}: HTTP ${response.status}: ${text}`);
      return JSON.parse(text);
    };
    // The fixture database's open steps are snoozed, so this run's step is the queue's first.
    const copied = await call("GET", "/api/state", user);
    await Promise.all([...copied.queue, ...copied.ready_next].map((step) => call("POST", `/api/steps/${step.id}/snooze`, user, { until: "2099-01-01T08:00:00Z" })));
    const key = `note:snooze-undo-${Date.now()}`;
    const title = "Snooze undo test";
    const itemId = (await call("PUT", "/api/items", agent, { key, title, stage: "building", priority: "urgent", sessions: [SESSION] })).id;
    const step = (await call("POST", "/api/steps", agent, { item_key: key, need: "decide:snooze-undo", ask: "Decide the snooze undo test", score: 100, session_id: SESSION })).step;
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
    const inQueue = (state) => state.queue.some((one) => one.id === step.id);
    const snoozedNow = (state) => state.snoozed.some((one) => one.id === step.id);

    browser = await chromium.launch({ executablePath: CHROMIUM });
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 2 });
    await context.setExtraHTTPHeaders({ cookie: user.cookie });
    const page = await context.newPage();
    page.setDefaultTimeout(8000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`${info.origin}/`, { timeout: 60000 });
    await page.locator("#sidebar .sidebar-groups").waitFor({ timeout: 60000 });

    const waiting = page.locator("#sidebar .sidebar-group.waiting");
    const heading = waiting.locator(":scope > summary");
    const headingUndo = heading.locator("button.busy-undo");
    const toastShown = () => page.evaluate(() => {
      const el = document.getElementById("toast");
      return !el.hidden && /^Snoozed/.test(el.textContent);
    });
    // Z on Next up with the step there, as the user presses it.
    const snooze = async () => {
      await page.waitForFunction((id) => ui.state && ui.state.queue.some((one) => one.id === id), step.id, { timeout: 20000 });
      await page.evaluate((id) => selectItem(id), itemId);
      await page.locator(`#next > [data-step="${step.id}"]`).first().waitFor();
      await page.evaluate(() => document.activeElement?.blur());
      const at = Date.now();
      await page.keyboard.press("z");
      return at;
    };
    // Opens or folds Waiting as the user does, with a click on its name (send_undo_busy.cjs says why).
    const setOpen = (open) => waiting.evaluate((el, want) => { if (el.open !== want) el.querySelector(":scope > summary .sidebar-group-name").click(); }, open);

    // The screenshots are taken while the Undo runs; on a busy machine they may outlast it, so a missed one only says so.
    const shot = (target, file) => target.screenshot({ path: path.join(work, file), timeout: 3000 }).catch(() => console.error(`  (no ${file}: the screenshot timed out)`));
    const shots = async (target, name) => {
      await shot(target, `${name}-light.png`);
      await page.emulateMedia({ colorScheme: "dark" });
      await sleep(200);
      await shot(target, `${name}-dark.png`);
      await page.emulateMedia({ colorScheme: "light" });
    };

    // 1. Z with Waiting folded: no notice; the card glides off; Waiting's heading lights up with the Undo.
    await waiting.waitFor({ timeout: 20000 });
    await setOpen(false);
    await page.evaluate(() => {
      window.glided = false;
      new MutationObserver((changes) => {
        if (changes.some((change) => [...change.addedNodes].some((node) => node.classList && node.classList.contains("send-away")))) window.glided = true;
      }).observe(document.body, { childList: true });
    });
    await snooze();
    await headingUndo.waitFor();
    check("a copy of the card glides away", await page.evaluate(() => window.glided), "no copy");
    check("the snooze shows no notice at the foot", !(await toastShown()), await page.locator("#toast").textContent());
    check("Waiting's heading lights up", await heading.evaluate((el) => el.classList.contains("holding")), await heading.getAttribute("class"));
    const counts = await heading.locator(".count-roll").evaluate((el) => [el.querySelector(".count-was").textContent, el.querySelector(".count-now").textContent]);
    check("its count rolls up by one", Number(counts[1]) === Number(counts[0]) + 1, JSON.stringify(counts));
    // motion.js moves a tooltip from `title` to `data-tip`.
    const tip = await headingUndo.evaluate((el) => el.dataset.tip || el.title);
    check("the Undo says it unsnoozes", /^Undo: unsnooze; snoozed until /.test(tip), tip);
    // A screenshot only while there is time left for the click after it.
    const left = await headingUndo.evaluate((el) => Number(el.querySelector(".undo-add-secs").dataset.until) - Date.now()).catch(() => 0);
    if (left > 4000) await shot(heading, "heading-first.png");
    await headingUndo.click();
    let state = await settle(inQueue);
    check("the heading's Undo unsnoozes the step", inQueue(state), "not in the queue");
    check("Waiting stays folded", !(await waiting.evaluate((el) => el.open)), "it opened");
    check("the heading's Undo is gone", (await headingUndo.count()) === 0, "still shown");

    // 2. With Waiting open, the step's row under Snoozed by you is the Undo, from the press on: Cockpit's answer to
    // the snooze waits here until the row shows.
    await setOpen(true);
    let answer = null;
    const answered = new Promise((resolve) => { answer = resolve; });
    const snoozeCall = /\/api\/steps\/\d+\/snooze$/;
    await page.route(snoozeCall, async (route) => { await answered; await route.continue(); });
    await snooze();
    const row = waiting.locator(`li.held-snooze[data-step="${step.id}"] > button.undo-add`);
    check("the row's Undo shows before Cockpit answers the snooze", await row.waitFor().then(() => true, () => false), "no row while the snooze waited");
    const snoozeAnswer = page.waitForResponse(snoozeCall);
    answer();
    await snoozeAnswer;
    await page.unroute(snoozeCall);
    await settle(snoozedNow);
    check("the open group's heading shows no Undo", !(await headingUndo.isVisible().catch(() => false)), "visible");
    check("the row names the task", (await row.locator(".sidebar-title").textContent()) === title, await row.textContent());
    const under = await waiting.evaluate((el, id) => {
      const rows = [...el.querySelectorAll(":scope > .sidebar-list > li")];
      const at = rows.findIndex((li) => li.dataset.step === String(id));
      const sub = rows.slice(0, at).reverse().find((li) => li.classList.contains("sidebar-sub"));
      return sub ? sub.textContent : "";
    }, step.id);
    check("the row sits under Snoozed by you", /^Snoozed by you/.test(under), under);
    check("no notice with Waiting open either", !(await toastShown()), await page.locator("#toast").textContent());
    await row.click();
    state = await settle(inQueue);
    check("the row's Undo unsnoozes the step", inQueue(state), "not in the queue");

    // 3. The Undo time ends: plain heading; the step stays snoozed.
    await snooze();
    await row.waitFor();
    await sleep(1000);
    await shots(row, "row");
    await setOpen(false);
    await headingUndo.waitFor().catch(() => {});
    await shot(page, "page-1440.png");
    await shots(heading, "heading");
    await headingUndo.waitFor({ state: "detached", timeout: 12000 });
    check("after the Undo time the heading is plain", await heading.evaluate((el) => !el.classList.contains("holding")), await heading.getAttribute("class"));
    state = await stateNow();
    check("the step stays snoozed", snoozedNow(state), "not snoozed");
    await setOpen(true);
    const plainRow = waiting.locator(`li[data-step="${step.id}"]`);
    await plainRow.waitFor();
    check("its row in Waiting is plain again", await plainRow.evaluate((el) => el.classList.contains("snoozed") && !el.classList.contains("held-snooze")), await plainRow.getAttribute("class"));

    // 4. An item with another step in Inbox: the list draws the item there, so the notice offers Undo.
    const key2 = `${key}-two`;
    const itemId2 = (await call("PUT", "/api/items", agent, { key: key2, title: "Snooze undo two steps", stage: "building", priority: "urgent", sessions: [SESSION] })).id;
    const other = (await call("POST", "/api/steps", agent, {
      item_key: key2, need: "allow:snooze-undo-other", ask: "Let the session list the other part", score: 90, session_id: SESSION,
      permission: { list: "allow", rule: "Bash(ls /tmp/snooze-undo-other *)" },
    })).step;
    const step2 = (await call("POST", "/api/steps", agent, { item_key: key2, need: "decide:snooze-undo-2", ask: "Decide the snooze undo test again", score: 100, session_id: SESSION })).step;
    await page.waitForFunction(([a, b]) => ui.state && [a, b].every((id) => ui.state.queue.some((one) => one.id === id)), [other.id, step2.id], { timeout: 20000 });
    await page.evaluate((id) => selectItem(id), itemId2);
    const shownCard = await page.locator(`#next > [data-step="${step2.id}"]`).first().waitFor({ timeout: 20000 }).then(() => true, () => false);
    if (shownCard) {
      await page.evaluate(() => document.activeElement?.blur());
      await page.keyboard.press("z");
      const notice = page.locator("#toast button", { hasText: "Undo" });
      check("with another step of the item in Inbox, the notice offers Undo", await notice.waitFor().then(() => true, () => false), await page.locator("#toast").textContent());
      check("and Waiting's heading holds no Undo", (await headingUndo.count()) === 0, "shown");
      await notice.click().catch(() => {});
    } else {
      check("with another step of the item in Inbox, the notice offers Undo", false, "the step's card did not show");
    }

    // 4b. An item with another step already snoozed, to wake sooner: Waiting draws the item by that one, so the
    // notice offers Undo.
    const key3 = `${key}-three`;
    const itemId3 = (await call("PUT", "/api/items", agent, { key: key3, title: "Snooze undo snoozed twice", stage: "building", priority: "urgent", sessions: [SESSION] })).id;
    const sooner = (await call("POST", "/api/steps", agent, {
      item_key: key3, need: "allow:snooze-undo-sooner", ask: "Let the session list the sooner part", score: 90, session_id: SESSION,
      permission: { list: "allow", rule: "Bash(ls /tmp/snooze-undo-sooner *)" },
    })).step;
    await call("POST", `/api/steps/${sooner.id}/snooze`, user, { until: new Date(Date.now() + 20 * 60e3).toISOString().replace(/\.\d+Z$/, "Z") });
    const step3 = (await call("POST", "/api/steps", agent, { item_key: key3, need: "decide:snooze-undo-3", ask: "Decide the snooze undo test a third time", score: 100, session_id: SESSION })).step;
    await page.waitForFunction(([a, b]) => ui.state && ui.state.snoozed.some((one) => one.id === a) && ui.state.queue.some((one) => one.id === b), [sooner.id, step3.id], { timeout: 20000 });
    await page.evaluate((id) => selectItem(id), itemId3);
    const thirdCard = await page.locator(`#next > [data-step="${step3.id}"]`).first().waitFor({ timeout: 20000 }).then(() => true, () => false);
    if (thirdCard) {
      await page.evaluate(() => document.activeElement?.blur());
      await page.keyboard.press("z");
      const notice = page.locator("#toast button", { hasText: "Undo" });
      check("with another step of the item snoozed, the notice offers Undo", await notice.waitFor().then(() => true, () => false), await page.locator("#toast").textContent());
      check("and Waiting holds no Undo row for it", (await waiting.locator("li.held-snooze").count()) === 0, "shown");
      await notice.click().catch(() => {});
    } else {
      check("with another step of the item snoozed, the notice offers Undo", false, "the step's card did not show");
    }
    // 4c. A snooze typed in words: its time is Cockpit's to read, so the notice offers Undo, and Waiting holds none.
    await call("POST", `/api/steps/${step.id}/snooze`, user, { until: null });
    await page.waitForFunction((id) => ui.state && ui.state.queue.some((one) => one.id === id), step.id, { timeout: 20000 });
    await page.evaluate((id) => {
      const shown = ui.state.queue.find((one) => one.id === id);
      act(shown, "snooze", { until: isoUtc(new Date(Date.now() + SNOOZE_READ_MINUTES * 60000)), words: { text: "friday morning", tz: "UTC" } });
    }, step.id);
    const wordsNotice = page.locator("#toast", { hasText: "Cockpit reads" });
    check("a snooze in words keeps its notice", await wordsNotice.waitFor().then(() => true, () => false), await page.locator("#toast").textContent());
    check("and Waiting holds no Undo for it", (await waiting.locator("li.held-snooze").count()) === 0 && (await headingUndo.count()) === 0, "shown");
    await page.locator("#toast button", { hasText: "Undo" }).click().catch(() => {});

    // 5. On a phone's item screen the Undo sits on the bar's Your items, and it unsnoozes the step.
    await call("POST", `/api/steps/${step.id}/snooze`, user, { until: null });
    const phoneContext = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
    await phoneContext.setExtraHTTPHeaders({ cookie: user.cookie });
    const phone = await phoneContext.newPage();
    phone.setDefaultTimeout(8000);
    phone.on("pageerror", (error) => errors.push(`phone: ${error.message}`));
    await phone.goto(`${info.origin}/`, { timeout: 60000 });
    await phone.waitForFunction((id) => ui.state && ui.state.queue.some((one) => one.id === id), step.id, { timeout: 30000 });
    await phone.evaluate((id) => selectItem(id), itemId);
    await phone.locator(`#next > [data-step="${step.id}"]`).first().waitFor();
    await phone.evaluate(() => document.activeElement?.blur());
    await phone.keyboard.press("z");
    const phoneUndo = phone.locator(".phone-items.holding button.busy-undo");
    check("on a phone the Undo sits on Your items", await phoneUndo.waitFor().then(() => true, () => false), await phone.locator(".phone-bar").textContent());
    await phone.locator(".phone-bar").screenshot({ path: path.join(work, "phone-bar.png"), timeout: 3000 }).catch(() => console.error("  (no phone-bar.png: the screenshot timed out)"));
    await phoneUndo.click().catch(() => {});
    state = await settle(inQueue);
    check("the phone's Undo unsnoozes the step", inQueue(state), "not in the queue");
    await phoneContext.close();
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
