// Images sent with any message (SPEC MSG-18). Puts its own item, whose step asks for the harness's working fake session,
// into the user's queue, then checks at desktop width in the light scheme (the step's card) and at phone width in the
// dark one (the item's row, once the first width's Send handed the step to the session):
// - the menu of the arrow beside Send shows Attach images;
// - two images picked through it wait above the box as thumbnails, and Remove takes one away;
// - Send uploads the one left to the item and the message carries its id, so the session is told about it;
// - the box is empty of images after Send;
// - on the step's card, Take back puts the image back above the box, and sending again carries it without a copy;
// - on another item's last step, Task done + send carries a staged image with the last message, and Undo puts it back;
// - no sideways scroll and no page errors.
// Saves screenshots of the open menu and of the box with its images, per width, in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) and drives a
// headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/attach_images.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

// Browser dependencies can be selected through the environment (README.md).
const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;
// serve_slow.py's working session, and the harness's agent token (tests/conftest.py).
const SESSION_B = "22222222-2222-4222-8222-222222222222";
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "attach-images-test", "content-type": "application/json" };

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-attach-images-");
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
    const cookie = `${info.name}=${info.value}`;
    const getState = () => fetch(`${info.origin}/api/state`, { headers: { cookie } }).then((response) => response.json());
    const call = async (method, url, headers, body) => {
      const response = await fetch(info.origin + url, { method, headers, body: JSON.stringify(body) });
      if (!response.ok) throw new Error(`${method} ${url}: HTTP ${response.status}: ${await response.text()}`);
      return response.json();
    };
    // The fixture database's open steps are snoozed, so this run's step takes Next up.
    const user = { cookie, origin: info.origin, "content-type": "application/json" };
    const initial = await getState();
    await Promise.all([...initial.queue, ...initial.ready_next, ...initial.snoozed].map((one) => call("POST", `/api/steps/${one.id}/snooze`, user, { until: "2099-01-01T08:00:00Z" })));
    const itemKey = `note:attach-images-${Date.now()}`;
    await call("PUT", "/api/items", AGENT, { key: itemKey, title: "Tasks export layout", sessions: [SESSION_B] });
    const { step: own } = await call("POST", "/api/steps", AGENT,
      { item_key: itemKey, need: "decide:layout", ask: "Which export layout should Tasks ship?", score: 100, session_id: SESSION_B });

    browser = await chromium.launch({ executablePath: CHROMIUM });
    // Two small pictures to attach, drawn by the browser.
    const painter = await browser.newPage({ viewport: { width: 240, height: 160 } });
    const picture = async (colour, words) => {
      await painter.setContent(`<body style="margin:0;background:${colour};font:28px sans-serif;color:#fff;display:grid;place-items:center;height:160px">${words}</body>`);
      return painter.screenshot();
    };
    const files = [
      { name: "layout-a.png", mimeType: "image/png", buffer: await picture("#2d6a9f", "Layout A") },
      { name: "layout-b.png", mimeType: "image/png", buffer: await picture("#9f4a2d", "Layout B") },
    ];
    await painter.close();

    for (const [width, scheme] of [[1280, "light"], [390, "dark"]]) {
      const state = await getState();
      const queued = state.queue.some((one) => one.id === own.id);
      if (!queued && !state.with_agents.some((one) => one.id === own.id)) throw new Error(`step ${own.id} is neither queued nor handed`);
      const itemId = own.item_id;

      const context = await browser.newContext({ viewport: { width, height: 1000 }, colorScheme: scheme });
      await context.setExtraHTTPHeaders({ cookie });
      const page = await context.newPage();
      page.setDefaultTimeout(90000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(`${info.origin}/classic`);
      let row;
      let key;
      if (queued) {
        await page.locator(`#next [data-step], #queue .row[data-step]`).first().waitFor({ state: "attached" });
        await page.evaluate((id) => window.openItemStep(id), itemId);
        row = page.locator(`#next [data-step="${own.id}"], #queue .row.open[data-step="${own.id}"]`).first();
        await row.waitFor();
        key = `msg:step:${own.id}`;
      } else {
        row = page.locator(`#work .row[data-item="${itemId}"]`).first();
        await row.waitFor({ state: "attached" });
        const group = page.locator("#work details.group", { has: page.locator(`.row[data-item="${itemId}"]`) }).first();
        if (!(await group.evaluate((el) => el.open))) await group.locator(":scope > summary").click();
        await row.scrollIntoViewIfNeeded();
        await row.locator(".row-head").click();
        key = `msg:item:${itemId}`;
      }
      const composer = row.locator(".composer").first();
      const words = `Like this one (${width})`;
      await row.locator(`textarea[data-key="${key}"]`).fill(words);
      await composer.locator("summary.send-more").click({ noWaitAfter: true });
      const attach = composer.locator("button.attach-way");
      check(`${width}: Attach images shows in Send's menu`, await attach.isVisible() && (await attach.innerText()).includes("Attach images"),
        `${await attach.count()} found`);
      // The menu opens above the box, so the shot takes the page around both.
      const menuShot = async () => {
        const boxes = [await composer.boundingBox(), await composer.locator("details:has(> summary.send-more) .menu-list").boundingBox()];
        const x = Math.max(0, Math.min(...boxes.map((one) => one.x)) - 8);
        const y = Math.max(0, Math.min(...boxes.map((one) => one.y)) - 8);
        const right = Math.max(...boxes.map((one) => one.x + one.width)) + 8;
        const bottom = Math.max(...boxes.map((one) => one.y + one.height)) + 8;
        await page.screenshot({ path: path.join(work, `menu-${width}-${scheme}.png`), clip: { x, y, width: right - x, height: bottom - y } });
      };
      await menuShot().catch(menuShot).catch((error) => console.error(`  (no menu screenshot at ${width}: ${error.message.split("\n")[0]})`));
      await composer.locator("summary.send-more").click({ noWaitAfter: true });

      await composer.locator('input[type="file"]').setInputFiles(files);
      const strip = composer.locator(".composer-images");
      await strip.locator(".thumb img").nth(1).waitFor();
      check(`${width}: the picked images wait above the box`, await strip.isVisible() && await strip.locator(".thumb").count() === 2,
        `${await strip.locator(".thumb").count()} thumbnails`);
      await composer.screenshot({ path: path.join(work, `staged-${width}-${scheme}.png`) });
      await strip.locator(".thumb-remove").nth(1).click();
      check(`${width}: Remove takes an image away`, await strip.locator(".thumb").count() === 1, `${await strip.locator(".thumb").count()} left`);

      const imagesOf = async () => (await getState()).items.find((one) => one.id === itemId).images;
      const before = new Set((await imagesOf()).map((one) => one.id));
      // A step card's Send hands the step off; an item row's sends a message. Both answer with the message.
      const answered = page.waitForResponse((response) => response.request().method() === "POST"
        && (/\/api\/messages$/.test(response.url()) || /\/api\/steps\/\d+\/hand-off$/.test(response.url())));
      await composer.locator("button.send").first().click();
      const response = await answered;
      const sent = response.ok() ? await call("GET", `/api/messages/${(await response.json()).id}`, user) : null;
      const added = (await imagesOf()).filter((one) => !before.has(one.id));
      check(`${width}: Send puts the image on the task`, added.length === 1, JSON.stringify(added));
      check(`${width}: the message carries the image`, Boolean(sent) && JSON.stringify(sent.images) === JSON.stringify(added.map((one) => one.id)),
        JSON.stringify(sent && { id: sent.id, images: sent.images }));
      check(`${width}: the box is empty of images after Send`, await strip.locator(".thumb").count() === 0 && await strip.isHidden(),
        `${await strip.locator(".thumb").count()} left`);

      if (queued) {
        // Take back on the hand-off's notice puts the image back above the box; sending again carries the same image.
        const taken = page.waitForResponse((one) => one.request().method() === "POST" && /\/api\/steps\/\d+\/return$/.test(one.url()));
        await page.locator("#toast button.toast-action", { hasText: "Take back" }).click();
        await taken;
        const back = page.locator(`[data-staged="${key}"] .thumb img`).first();
        await back.waitFor();
        check(`${width}: Take back puts the image back above the box`, await page.locator(`[data-staged="${key}"] .thumb`).count() === 1,
          `${await page.locator(`[data-staged="${key}"] .thumb`).count()} thumbnails`);
        const again = page.waitForResponse((one) => one.request().method() === "POST" && /\/api\/steps\/\d+\/hand-off$/.test(one.url()));
        await page.locator(`.composer:has(textarea[data-key="${key}"]) button.send`).first().click();
        const resent = await call("GET", `/api/messages/${(await (await again).json()).id}`, user);
        const after = (await imagesOf()).filter((one) => !before.has(one.id));
        check(`${width}: sending again carries the same image, not a copy`,
          after.length === 1 && JSON.stringify(resent.images) === JSON.stringify(added.map((one) => one.id)),
          JSON.stringify({ added: after.map((one) => one.id), images: resent.images }));
      }

      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      check(`${width}: no sideways scroll`, overflow <= 0, `${overflow} px`);
      check(`${width}: no page errors`, errors.length === 0, errors.join("; "));
      await context.close();
    }

    // Task done + send on an item's last step: the staged image goes with the last message, and Undo puts it back.
    {
      const doneKey = `note:attach-images-done-${Date.now()}`;
      await call("PUT", "/api/items", AGENT, { key: doneKey, title: "Tasks export wording", sessions: [SESSION_B] });
      const { step: last } = await call("POST", "/api/steps", AGENT,
        { item_key: doneKey, need: "decide:wording", ask: "Is the export wording right?", score: 100, session_id: SESSION_B });
      const context = await browser.newContext({ viewport: { width: 1280, height: 1000 } });
      await context.setExtraHTTPHeaders({ cookie });
      const page = await context.newPage();
      page.setDefaultTimeout(90000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(`${info.origin}/classic`);
      await page.locator(`#next [data-step], #queue .row[data-step]`).first().waitFor({ state: "attached" });
      await page.evaluate((id) => window.openItemStep(id), last.item_id);
      const row = page.locator(`#next [data-step="${last.id}"], #queue .row.open[data-step="${last.id}"]`).first();
      await row.waitFor();
      const key = `msg:step:${last.id}`;
      const composer = row.locator(".composer").first();
      await row.locator(`textarea[data-key="${key}"]`).fill("Ship it with this wording");
      await composer.locator('input[type="file"]').setInputFiles([files[0]]);
      await page.locator(`[data-staged="${key}"] .thumb img`).first().waitFor();
      const finishing = page.waitForRequest((request) => request.method() === "POST" && /\/api\/items\/\d+\/finish$/.test(request.url()));
      const finished = page.waitForResponse((response) => response.request().method() === "POST" && /\/api\/items\/\d+\/finish$/.test(response.url()));
      await row.locator(".task-done").click();
      const body = JSON.parse((await finishing).postData());
      const response = await finished;
      const message = response.ok() ? (await response.json()).message : null;
      check("Task done + send carries the staged image with the last message", body.images.length === 1
        && JSON.stringify(message && message.images) === JSON.stringify(body.images),
        JSON.stringify({ status: response.status(), sent: body.images, message: message && { id: message.id, images: message.images } }));
      const undone = page.waitForResponse((one) => one.request().method() === "POST" && /\/api\/items\/\d+\/reopen$/.test(one.url()));
      await page.locator("#toast button", { hasText: "Undo" }).click();
      await undone;
      const back = page.locator(`[data-staged="${key}"] .thumb`);
      await back.first().waitFor();
      check("Undo of Task done puts the image back above the box", await back.count() === 1, `${await back.count()} thumbnails`);
      check("Task done: no page errors", errors.length === 0, errors.join("; "));
      await context.close();
    }
    console.error(`screenshots in ${work}`);
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failed = results.filter((result) => !result.ok);
  console.error(`${results.length - failed.length} of ${results.length} checks passed`);
  if (failed.length) process.exitCode = 1;
}

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
