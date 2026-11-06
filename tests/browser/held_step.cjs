// A step the user opened or started answering keeps its place. On a synthetic database, with the seeded steps
// snoozed, puts four steps on a normal item, presses Next up without starting its timer, and types in a queue row's
// reply box. Then urgent steps scored 95 arrive: Next up and the row keep their places on the page and on the server,
// and Next up shows Resorted, whose press shows the urgent step first. Then an opened row shows Resorted while urgent
// steps wait in Ready next, and its press takes one to Next up. Saves screenshots of Next up with Resorted (desktop
// light and dark, phone) to the work folder.  Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) and drives a headless Chromium. Prints one line per check and exits 1 when
// one failed.  Usage, from the checkout: node tests/browser/held_step.cjs
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
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "held-step", "content-type": "application/json" };

async function main() {
  const work = workFolder("cockpit-held-step-");
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
    const call = async (who, method, url, body) => {
      const response = await fetch(`${info.origin}${url}`, { method, headers: who, body: body === undefined ? undefined : JSON.stringify(body) });
      if (!response.ok) throw new Error(`${method} ${url}: HTTP ${response.status} ${await response.text()}`);
      return response.json();
    };
    const copied = await call(user, "GET", "/api/state");
    await Promise.all([...copied.queue, ...copied.ready_next, ...copied.snoozed].map((step) => call(user, "POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
    const stamp = Date.now();
    const key = `note:held-step-${stamp}`;
    await call(AGENT, "PUT", "/api/items", { key, title: "Add recurring reminders", stage: "building", sessions: [SESSION_B] });
    const asks = ["Change the batch job order?", "Merge the export fix?", "Read the export result", "Reply to Alex?"];
    const steps = [];
    for (const [n, ask] of asks.entries()) {
      steps.push((await call(AGENT, "POST", "/api/steps", { item_key: key, need: `n${n}`, ask, session_id: SESSION_B, score: 60 - 10 * n })).step);
    }
    const queued = async () => (await call(user, "GET", "/api/state")).queue.map((step) => step.id);

    browser = await chromium.launch({ executablePath: CHROMIUM });
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, colorScheme: "light" });
    await context.setExtraHTTPHeaders({ cookie: user.cookie });
    const page = await context.newPage();
    page.setDefaultTimeout(5000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`${info.origin}/classic`);
    await page.locator(`#next [data-step="${steps[0].id}"]`).waitFor();

    // Opening: a press on Next up's ask, with no timer.
    await page.locator("#next .ask").click();
    const timer = await page.evaluate(() => Boolean(ui.state.timer));
    check("pressing Next up starts no timer", !timer, "a timer runs");
    // Opening the second step's row from the keyboard (J to it, O to open) holds it too; moving the highlight alone does not.
    let highlighted = false;
    for (let n = 0; n < 6 && !highlighted; n++) {
      await page.keyboard.press("j");
      highlighted = await page.evaluate((id) => Boolean(document.querySelector(`#queue .row[data-step="${id}"].keyboard-current`)), steps[1].id);
    }
    const heldByMove = await page.evaluate((id) => ui.placed.has(id), steps[1].id);
    await page.keyboard.press("o");
    await sleep(300);
    const heldByOpen = await page.evaluate((id) => ui.placed.has(id), steps[1].id);
    check("J moves the highlight without holding; O opens the row and holds it", highlighted && !heldByMove && heldByOpen,
      `highlighted ${highlighted}, held by the move ${heldByMove}, by the open ${heldByOpen}`);
    // Answering: typing in the second step's reply box.
    const box = page.locator(`#queue textarea[data-key="msg:step:${steps[1].id}"]`);
    await box.waitFor();
    await box.fill("Looks right; one question first");
    await sleep(500);

    const urgentKey = `note:held-step-urgent-${stamp}`;
    await call(AGENT, "PUT", "/api/items", { key: urgentKey, title: "Reminder dates missing", stage: "building", sessions: [SESSION_B] });
    const urgentItem = (await call(user, "GET", "/api/state")).items.find((item) => item.key === urgentKey);
    await call(user, "PATCH", `/api/items/${urgentItem.id}`, { priority: "urgent" });
    const urgent = [];
    for (let n = 0; n < 4; n++) {
      urgent.push((await call(AGENT, "POST", "/api/steps", { item_key: urgentKey, need: `u${n}`, ask: `Restore the reminder date? (${n})`, session_id: SESSION_B, score: 95 })).step);
    }
    const queue = await queued();
    check("the pressed and the answered step keep their places on the server", queue.includes(steps[0].id) && queue.includes(steps[1].id), queue.join(","));
    check("the steps the user did not touch gave way", !queue.includes(steps[2].id) && !queue.includes(steps[3].id), queue.join(","));

    // A poll leaves the queue alone while the user types in it; back on Next up, it redraws, once a poll
    // brought all four urgent steps.
    await page.locator("#next .ask").click();
    await page.waitForFunction((ids) => ids.every((id) => [...ui.state.queue, ...ui.state.ready_next].some((s) => s.id === id)),
      urgent.map((step) => step.id), { timeout: 20e3 });
    const resorted = "#next > .resorted-pin > .reorder.resorted";
    const shown = await page.waitForFunction((id) => ui.next && ui.next.id === id && document.querySelector("#next > .resorted-pin > .reorder.resorted"),
      steps[0].id, { timeout: 20e3 }).then(() => true, () => false);
    check("Next up stays the pressed step and shows Resorted", shown,
      await page.evaluate(() => `order ${ui.order.join(",")}; state ${ui.state.queue.map((s) => s.id).join(",")}; placed ${[...ui.placed].join(";")}; active ${document.activeElement && document.activeElement.dataset.key}`));
    const words = shown ? await page.locator(resorted).evaluate((el) => `${el.getAttribute("aria-label")}|${el.innerText.trim()}`) : "";
    check("the key is the arrows icon, named Resorted", words.startsWith("Resorted:") && words.endsWith("|"), words);
    const tip = shown ? await page.locator(resorted).evaluate((el) => el.getAttribute("title") || el.dataset.tip || "") : "";
    check("its tooltip names the stronger step and its score", tip.includes("Reminder dates missing") && tip.includes("score 95"),
      `${tip} | ${await page.evaluate(() => JSON.stringify({ first: ui.state.queue[0].item, ids: ui.state.queue.map((s) => s.id) }))}`);
    const order = await page.evaluate(() => ui.order);
    check("the answered row keeps its place under Next up", order[1] === steps[1].id, order.join(","));
    const look = shown ? await page.locator(resorted).evaluate((el) => [getComputedStyle(el).color,
      getComputedStyle(document.querySelector("#next .info-mark") || document.body).color]) : ["", ""];
    check("Resorted is orange, unlike the grey marks beside it", look[0] !== "" && look[0] !== look[1], look.join(" vs "));

    const stray = await page.evaluate(() => { const walk = document.createTreeWalker(document.querySelector("#next"), NodeFilter.SHOW_TEXT);
      const found = []; while (walk.nextNode()) if (["false", "true", "null", "undefined"].includes(walk.currentNode.data.trim())) found.push(walk.currentNode.parentElement.className);
      return found; });
    check("Next up shows no stray false or null text", stray.length === 0, stray.join(","));

    // Scrolled down a long step, with only part of the card on screen, the key stays in sight below the header.
    if (shown) {
      await page.screenshot({ path: path.join(work, "resorted-top.png") });
      await page.setViewportSize({ width: 1280, height: 420 });
      const kept = await page.evaluate(async (sel) => {
        const card = document.querySelector("#next .next-card");
        const header = document.querySelector(".top").getBoundingClientRect().bottom;
        window.scrollBy(0, card.getBoundingClientRect().top - header + 120);
        await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
        const key = document.querySelector(sel).getBoundingClientRect();
        const box = card.getBoundingClientRect();
        return { cardTop: Math.round(box.top), cardBottom: Math.round(box.bottom), header: Math.round(header),
          keyTop: Math.round(key.top), keyBottom: Math.round(key.bottom), height: innerHeight };
      }, resorted);
      check("scrolled with the card's top out of sight, Resorted stays below the header",
        kept.cardTop < kept.header && kept.cardBottom > kept.keyBottom && kept.keyTop >= kept.header && kept.keyBottom <= kept.height, JSON.stringify(kept));
      await page.screenshot({ path: path.join(work, "resorted-scrolled.png") });
      const end = await page.evaluate(async (sel) => {
        const card = document.querySelector("#next .next-card");
        const header = document.querySelector(".top").getBoundingClientRect().bottom;
        window.scrollBy(0, card.getBoundingClientRect().bottom - header - 30);
        await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
        const key = document.querySelector(sel).getBoundingClientRect();
        return { cardBottom: Math.round(card.getBoundingClientRect().bottom), header: Math.round(header), keyTop: Math.round(key.top), keyBottom: Math.round(key.bottom) };
      }, resorted);
      check("with only 30 px of the card on screen, Resorted still shows", end.keyBottom > end.header && end.keyTop < end.cardBottom, JSON.stringify(end));
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.setViewportSize({ width: 1280, height: 900 });
    }

    if (shown) {
      await page.locator("#next").screenshot({ path: path.join(work, "resorted-light.png") });
      await page.emulateMedia({ colorScheme: "dark" });
      await sleep(300);
      await page.locator("#next").screenshot({ path: path.join(work, "resorted-dark.png") });
      await page.setViewportSize({ width: 390, height: 844 });
      await sleep(300);
      await page.locator("#next").screenshot({ path: path.join(work, "resorted-phone.png") });
      const wide = await page.evaluate(() => {
        const next = document.querySelector("#next");
        const card = next.querySelector(".next-card").getBoundingClientRect();
        const over = [...next.querySelectorAll(".step-context *")].filter((el) => el.getBoundingClientRect().right > Math.min(card.right, window.innerWidth) + 1)
          .map((el) => `${el.tagName.toLowerCase()}.${[...el.classList].join(".")} ${Math.round(el.getBoundingClientRect().right)}/${Math.round(card.right)}/${window.innerWidth}`);
        return { page: document.documentElement.scrollWidth - window.innerWidth, over: over.slice(0, 8) };
      });
      const hit = await page.evaluate((sel) => {
        const key = document.querySelector(sel).getBoundingClientRect();
        const top = document.elementFromPoint(key.left + key.width / 2, key.top + key.height / 2);
        return top && top.closest(sel) ? "key" : top ? `${top.tagName.toLowerCase()}.${[...top.classList].join(".")}` : "nothing";
      }, resorted);
      check("at phone width a tap on Resorted's centre reaches it", hit === "key", hit);
      check("at phone width nothing on Next up runs past its edge", wide.page <= 0 && wide.over.length === 0, JSON.stringify(wide));
      await page.setViewportSize({ width: 1280, height: 900 });
      // Two urgent steps wait in Ready next while the two opened steps hold their places: the press takes one.
      const urgentIds = urgent.map((step) => step.id);
      const waitingIds = await page.evaluate((ids) => ui.state.ready_next.map((s) => s.id).filter((id) => ids.includes(id)), urgentIds);
      await page.locator(resorted).click({ noWaitAfter: true, timeout: 15e3 });
      const first = await page.waitForFunction((ids) => ids.includes(ui.order[0]) && document.querySelector(`#next [data-step="${ui.order[0]}"]`),
        waitingIds, { timeout: 5000 }).then(() => true, () => false);
      check("a press on Resorted takes a waiting urgent step to Next up", first && waitingIds.length > 0,
        `order ${await page.evaluate(() => ui.order.join(","))}; waiting ${waitingIds}; urgent ${urgentIds}`);
      const after = await queued();
      check("the pressed step stays in the queue after Resorted", after.includes(steps[0].id), after.join(","));

      // The first opened step still holds its place, and an urgent step waits in Ready next. Opening that
      // step's row shows Resorted on it, and its press takes the waiting urgent step to Next up.
      await page.locator(`#queue .row[data-step="${steps[0].id}"] .row-title`).click();
      const rowKey = `#queue .row[data-step="${steps[0].id}"] .reorder.resorted`;
      const rowShown = await page.waitForSelector(rowKey, { timeout: 15e3 }).then(() => true, () => false);
      check("with urgent work waiting in Ready next, the opened row shows Resorted", rowShown,
        await page.evaluate(() => `order ${ui.order.join(",")}; ready ${ui.state.ready_next.map((s) => s.id).join(",")}; placed ${[...ui.placed].join(";")}`));
      if (rowShown) {
        await page.locator(rowKey).click({ noWaitAfter: true, timeout: 15e3 });
        const waiting = await page.evaluate((ids) => ui.state.ready_next.map((s) => s.id).filter((id) => ids.includes(id)), urgentIds);
        const took = await page.waitForFunction((ids) => ids.includes(ui.order[0]), waiting, { timeout: 5000 }).then(() => true, () => false);
        check("its press takes the waiting urgent step to Next up", took && waiting.length > 0,
          `order ${await page.evaluate(() => ui.order.join(","))}; waiting ${waiting}`);
      }
    }
    check("no page errors", errors.length === 0, errors.join("; "));
    console.error(`  screenshots in ${work}`);
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  if (results.some((r) => !r.ok)) process.exitCode = 1;
}

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
