// A close's Undo sits in Finished this week. On a synthetic database, on the page with the list at its left, at
// 1440 px:
// - with Finished this week folded, Task done shows no notice at the foot; the group's count pulses once and rolls up
//   by one, Slingshot and Coin aim at it, and the heading lights up with a plum Undo, which reopens the task and its
//   step and leaves the group folded;
// - with the group open, the heading shows no Undo; the task's row is the Undo, plum and named for the task, and it
//   reopens the task and its step;
// - after Task done with a reply typed, the last message waits while the Undo is on offer; after the Undo time the
//   heading is plain, the last message goes to the session once, at least 7 s after the press, and the task stays
//   as a plain row;
// - no page errors.
// Saves screenshots of the row and the heading, light and dark, and of the page, in the work folder.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) and drives
// a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/close_undo_list.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// The harness's agent token (tests/conftest.py TOKEN) and its second fake session, which a last message goes to.
const AGENT_TOKEN = "t".repeat(40);
const SESSION = "22222222-2222-4222-8222-222222222222";

async function main() {
  const work = workFolder("cockpit-close-undo-");
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
    const agent = { authorization: `Bearer ${AGENT_TOKEN}`, "x-cockpit-as": "close-undo-test", "content-type": "application/json" };
    const call = async (method, url, headers, body) => {
      const response = await fetch(`${info.origin}${url}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
      const text = await response.text();
      if (!response.ok) throw new Error(`${method} ${url}: HTTP ${response.status}: ${text}`);
      return JSON.parse(text);
    };
    // The fixture database's open steps are snoozed, so this run's step is the queue's first.
    const initial = await call("GET", "/api/state", user);
    await Promise.all([...initial.queue, ...initial.ready_next, ...initial.snoozed].map((step) => call("POST", `/api/steps/${step.id}/snooze`, user, { until: "2099-01-01T08:00:00Z" })));
    const key = `note:close-undo-${Date.now()}`;
    const title = "Close undo test";
    const itemId = (await call("PUT", "/api/items", agent, { key, title, stage: "building", priority: "urgent", sessions: [SESSION] })).id;
    const step = (await call("POST", "/api/steps", agent, { item_key: key, need: "complete", ask: "Confirm the close undo test is done", score: 100, session_id: SESSION })).step;
    const itemNow = () => call("GET", `/api/items?key=${encodeURIComponent(key)}`, agent);
    const settle = async (want) => {
      let item = null;
      for (let tries = 0; tries < 40; tries += 1) {
        item = await itemNow();
        if (want(item)) break;
        await sleep(250);
      }
      return item;
    };
    const reopened = (item) => item.stage === "building" && item.steps.some((one) => one.id === step.id && one.status === "open");

    browser = await chromium.launch({ executablePath: CHROMIUM });
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 2 });
    await context.setExtraHTTPHeaders({ cookie: user.cookie });
    const page = await context.newPage();
    page.setDefaultTimeout(8000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const releases = [];
    page.on("request", (request) => { if (/\/api\/messages\/\d+\/release$/.test(request.url())) releases.push(Date.now()); });
    await page.goto(`${info.origin}/`);
    await page.locator("#sidebar .sidebar-groups").waitFor({ timeout: 60000 });
    await page.evaluate(() => {
      const probe = document.createElement("span");
      probe.id = "plum-probe";
      probe.style.color = "var(--complete)";
      document.body.append(probe);
    });
    const plum = () => page.evaluate(() => getComputedStyle(document.getElementById("plum-probe")).color);

    const doneGroup = page.locator("#sidebar .sidebar-group.done");
    const heading = doneGroup.locator(":scope > summary");
    const headingUndo = heading.locator("button.busy-undo");
    const press = async () => {
      await page.waitForFunction((id) => ui.state && ui.state.queue.some((one) => one.id === id), step.id, { timeout: 20000 });
      await page.evaluate((id) => selectItem(id), itemId);
      const done = page.locator(`#next [data-step="${step.id}"] .button.task-done`).first();
      await done.waitFor();
      await done.click();
    };
    const setOpen = (open) => doneGroup.evaluate((el, want) => { el.open = want; }, open);

    // 1. Task done with Finished this week folded: no notice at the foot; the heading lights up with an Undo.
    await setOpen(false);
    // The count's pulse runs from the press until 3.45 s after it (sidebar.css count-pulse-close), and on a busy machine
    // the press's click can return seconds later, so the page reads the count's pulse at the first frame it shows one.
    await page.evaluate(() => {
      window.pulseSeen = [];
      const look = () => {
        const count = document.querySelector("#sidebar .sidebar-group.done > summary .count-now");
        const pulses = count ? count.getAnimations().filter((one) => one.animationName === "count-pulse-close") : [];
        if (pulses.length) window.pulseSeen = pulses.map((one) => one.effect.getTiming().iterations);
        else requestAnimationFrame(look);
      };
      requestAnimationFrame(look);
    });
    await press();
    await headingUndo.waitFor();
    // Read in the next frame, after that frame's look at the count, which was asked for first.
    const pulse = await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => resolve(window.pulseSeen))));
    check("its count pulses once, up and back", pulse.length === 1 && pulse[0] === 2, JSON.stringify(pulse));
    // This section takes no pictures: its Undo press has to land inside the Undo time.
    await sleep(600);
    check("no Closed! notice shows at the foot", await page.evaluate(() => {
      const el = document.getElementById("toast");
      return el.hidden || !el.classList.contains("closed");
    }), await page.locator("#toast").innerHTML());
    const aim = await page.evaluate(() => {
      const mark = noticeMark();
      const count = document.querySelector("#sidebar .sidebar-group.done > summary .group-count").getBoundingClientRect();
      return { x: mark.x, y: mark.y, el: mark.el && mark.el.className, count: { left: count.left, right: count.right, top: count.top, bottom: count.bottom } };
    });
    check("Slingshot and Coin aim at Finished this week's count",
      aim.x >= aim.count.left && aim.x <= aim.count.right && aim.y >= aim.count.top && aim.y <= aim.count.bottom, JSON.stringify(aim));
    check("Finished this week's heading lights up", await heading.evaluate((el) => el.classList.contains("holding")), await heading.getAttribute("class"));
    check("its Undo is plum", (await headingUndo.locator(".icon").evaluate((el) => getComputedStyle(el).color)) === await plum(),
      await headingUndo.locator(".icon").evaluate((el) => getComputedStyle(el).color));
    const counts = await heading.locator(".count-roll").evaluate((el) => [el.querySelector(".count-was").textContent, el.querySelector(".count-now").textContent]);
    check("its count rolls up by one", Number(counts[1]) === Number(counts[0]) + 1, JSON.stringify(counts));
    await settle((item) => item.stage === "done");
    await headingUndo.click();
    let item = await settle(reopened);
    check("the heading's Undo reopens the task and its step", reopened(item), JSON.stringify({ stage: item.stage }));
    check("Finished this week stays folded", !(await doneGroup.evaluate((el) => el.open)), "it opened");
    check("the heading's Undo is gone", (await headingUndo.count()) === 0, "still shown");

    // 2. With Finished this week open, the task's row is the Undo.
    await setOpen(true);
    await press();
    const row = doneGroup.locator(`li.held-close[data-item="${itemId}"] > button.undo-close`);
    await row.waitFor();
    check("the open group's heading shows no Undo", !(await headingUndo.isVisible().catch(() => false)), "visible");
    // A redraw can replace the row just as it shows, and the replaced row has no style; read the row that stays.
    const want = await plum();
    let color = "";
    for (let tries = 0; tries < 20 && color !== want; tries += 1) {
      if (tries) await sleep(50);
      color = await row.locator(".sidebar-lead .icon").evaluate((el) => (el.isConnected ? getComputedStyle(el).color : "")).catch(() => "");
    }
    check("the row's Undo is plum", color === want, `its icon is ${color || "unstyled"}`);
    check("the row names the task", (await row.locator(".sidebar-title").textContent()) === title, await row.textContent());
    // A redraw can replace the row while it is shot; the Undo time is short, so shoot at once and retry a replaced row.
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
    await settle((one) => one.stage === "done");
    await row.click();
    item = await settle(reopened);
    check("the row's Undo reopens the task and its step", reopened(item), JSON.stringify({ stage: item.stage }));

    // 3. The Undo time ends: plain heading and row; a last message goes only then.
    await setOpen(false);
    await page.waitForFunction((id) => ui.state && ui.state.queue.some((one) => one.id === id), step.id, { timeout: 20000 });
    await page.evaluate((id) => selectItem(id), itemId);
    const box = page.locator(`#next [data-step="${step.id}"] textarea`).first();
    await box.waitFor();
    await box.fill("Thanks, all good");
    const pressedAt = Date.now();
    await page.locator(`#next [data-step="${step.id}"] .button.task-done`).first().click();
    await headingUndo.waitFor();
    // The pictures of the lit heading and the page are taken here, once the count has rolled after 1.8 s, as no press
    // has to land inside this Undo time. A redraw can replace the heading while it is shot: retry a replaced one.
    await sleep(1800);
    const shotHeading = async (file) => {
      for (let tries = 0; ; tries += 1) {
        try { return await heading.screenshot({ path: path.join(work, file), timeout: 2000 }); } catch (error) { if (tries >= 3) throw error; }
      }
    };
    await shotHeading("heading-light.png");
    await page.emulateMedia({ colorScheme: "dark" });
    await sleep(200);
    await shotHeading("heading-dark.png");
    await page.emulateMedia({ colorScheme: "light" });
    await page.screenshot({ path: path.join(work, "closed-1440.png") });
    await sleep(Math.max(0, pressedAt + 4000 - Date.now()));
    check("the last message waits while the Undo is on offer", releases.every((at) => at - pressedAt >= 4000), `released after ${releases[0] - pressedAt} ms`);
    await headingUndo.waitFor({ state: "detached", timeout: 12000 });
    const plain = await heading.evaluate((el) => !el.classList.contains("holding"));
    check("after the Undo time the heading is plain", plain, await heading.getAttribute("class"));
    for (let tries = 0; tries < 20 && !releases.length; tries += 1) await sleep(250);
    check("then the last message goes to the session", releases.length === 1 && releases[0] - pressedAt >= 7000, JSON.stringify(releases.map((at) => at - pressedAt)));
    await setOpen(true);
    const normal = doneGroup.locator(`li[data-item="${itemId}"]`);
    await normal.waitFor();
    check("the task stays in Finished this week as a plain row", !(await normal.evaluate((el) => el.classList.contains("held-close"))), await normal.getAttribute("class"));
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
