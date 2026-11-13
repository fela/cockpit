// A step's head. A plain to-do carries no tag of its own (SPEC CONTROL-16);
// one added with Fire and forget keeps its tag (CONTROL-24). Cockpit's own work
// shows the Cockpit key alone at the end of the title (UI-43), and the priority sits in the step's menu (CONTROL-7).
// Checks, on the main page at 1280, 390 and 320 px and on /classic at 1280 and 390 px:
// - a plain to-do the user added shows no "To-do" chip on its step, and no "To-do" chip shows anywhere;
// - a Fire and forget to-do's step shows its "Fire and forget" chip;
// - a Cockpit step's head has the key right after the title, without the word, and no priority icon;
// - with a long title, the key follows its last word wherever it wraps; marks sit above a heading or level with a row's first line;
// - the menu's Priority opens the four levels, the current one checked, and choosing one sets the item's priority
//   (on the main page and the phone);
// - the page has no page errors.
// Saves a screenshot per width in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs)
// and drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/head_marks.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const ROOT = path.resolve(__dirname, "..", "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
const SESSION_B = "22222222-2222-4222-8222-222222222222";
const TOKEN = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "step-head-test" };

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function longTitleGeometry(el, breakLayout) {
  const heading = el.querySelector(".task-heading");
  const title = el.querySelector(".task-heading-text, .step-context > .title-line > .item-title");
  const marks = el.querySelector(".step-context > .context-marks");
  if (!title || !marks) return null;
  const range = document.createRange();
  range.selectNodeContents(title);
  const rects = [...range.getClientRects()];
  if (!rects.length) return null;
  const original = marks.getAttribute("style");
  try {
    // The deliberate break moves the controls onto the heading's first word.
    if (breakLayout) {
      const box = marks.querySelector(".step-menu > summary").getBoundingClientRect();
      marks.style.transform = `translate(${rects[0].left - box.left}px, ${rects[0].top - box.top}px)`;
    }
    const tag = title.nextElementSibling;
    const key = tag.getBoundingClientRect();
    const line = tag.parentElement.getBoundingClientRect();
    const strip = marks.getBoundingClientRect();
    const controls = [...marks.querySelectorAll(":scope > :not(.step-menu), :scope > .step-menu > summary")]
      .map((node) => node.getBoundingClientRect()).filter((box) => box.width > 0 && box.height > 0);
    const first = Math.min(...rects.map((rect) => rect.top));
    const last = rects.at(-1);
    const hits = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
    const above = Boolean(heading) || getComputedStyle(marks).order !== "0";
    return {
      headed: Boolean(heading),
      lines: new Set(rects.map((rect) => Math.round(rect.top))).size,
      clipped: !tag.matches(".chip.cockpit") || getComputedStyle(tag.parentElement).display !== "contents" && key.bottom > line.bottom + 1,
      keyLine: Math.round((key.top + key.bottom) / 2 - (last.top + last.bottom) / 2),
      marksTop: Math.round(strip.top - first),
      controls: controls.length,
      menu: Boolean(marks.querySelector(".step-menu > summary")?.checkVisibility()),
      updated: Boolean(marks.querySelector(".updated-mark")?.checkVisibility()),
      // Headed cards and separate control rows place the whole strip above the title.
      marksPlaced: above
        ? strip.bottom <= (heading ? heading.getBoundingClientRect().top : first) + 1
          && controls.every((box) => box.bottom <= (heading ? heading.getBoundingClientRect().top : first) + 1)
        : strip.top - first >= -8 && strip.top - first <= 12,
      overlaps: controls.filter((box) => rects.some((rect) => hits(box, rect))).length,
      gap: Math.round(key.left - last.right),
    };
  } finally {
    if (original === null) marks.removeAttribute("style");
    else marks.setAttribute("style", original);
  }
}

const longTitleFits = (lines) => Boolean(lines) && !lines.clipped && Math.abs(lines.keyLine) <= 4
  && lines.controls > 0 && lines.menu && lines.marksPlaced && (!lines.headed || lines.updated && lines.overlaps === 0)
  && lines.gap >= 0 && lines.gap <= 12;

async function main() {
  const work = workFolder("head-marks-");
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const info = await harnessUp(work, server);
    const cookie = { cookie: `${info.name}=${info.value}`, origin: info.origin };
    const agent = (method, url, body) => call(info.origin, { ...TOKEN, "content-type": "application/json" }, method, url, JSON.stringify(body));
    const user = (method, url, body) => call(info.origin, { ...cookie, "content-type": "application/json" }, method, url,
      body === undefined ? undefined : JSON.stringify(body));

    const initial = await user("GET", "/api/state");
    await Promise.all([...initial.queue, ...initial.ready_next, ...initial.snoozed].map((step) => user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
    const run = Date.now();
    const add = async (title, fireAndForget, score) => {
      const item = await user("POST", "/api/items/add", { text: `${title} ${run}`, part: null, pinned: false, fire_and_forget: fireAndForget });
      const answer = await agent("POST", "/api/steps", {
        item_id: item.id, need: `define-${run}`, ask: `${title}?`, score, session_id: SESSION_B, body: "Say what done looks like.",
      });
      return { item: item.id, step: answer.step.id };
    };
    const plain = await add("Renew the passport", false, 100);
    const fire = await add("Fix the CSV exporter", true, 95);
    const toolKey = `note:step-head-${run}`;
    const toolItem = await agent("PUT", "/api/items", { key: toolKey, title: "Tighter card spacing", project: "cockpit", priority: "normal" });
    const tool = (await agent("POST", "/api/steps", {
      item_key: toolKey, need: `look-${run}`, ask: "Keep the tighter spacing?", score: 90, session_id: SESSION_B, body: "Say yes or no.",
    })).step.id;
    // The long title wraps on a phone, ending in the key with the controls above its heading.
    const longKey = `note:step-head-long-${run}`;
    const longItem = await agent("PUT", "/api/items", { key: longKey, title: "Review screenshot alignment improvements", project: "cockpit", priority: "normal" });
    const long = (await agent("POST", "/api/steps", {
      item_key: longKey, need: `look-${run}`, ask: "Keep the new alignment?", score: 85, session_id: SESSION_B, body: "Say yes or no.",
    })).step.id;

    browser = await chromium.launch({ executablePath: CHROMIUM });
    for (const [pagePath, width] of [["/", 1280], ["/", 390], ["/", 320], ["/classic", 1280], ["/classic", 390]]) {
      const at = `${pagePath} at ${width} px`;
      const context = await browser.newContext({ viewport: { width, height: 900 } });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(`${info.origin}${pagePath}`);
      await page.waitForSelector(`[data-step="${plain.step}"]`, { state: "attached", timeout: 30e3 });
      await sleep(1500);
      // The main page opens a task by its row in the sidebar, which a phone folds away: the test picks it as the row
      // does. /classic opens a step by a click on its row.
      const open = async (step, item) => {
        if (pagePath === "/") await page.evaluate((id) => selectItem(id), item);
        else await page.locator(`[data-step="${step}"]`).first().click({ position: { x: 40, y: 12 } });
        await sleep(800);
      };
      const chips = (selector) => page.$$eval(`${selector} .chip-words`, (nodes) => nodes.map((node) => node.textContent.trim()));
      // Next up, the one open card, holds each step in turn.
      for (const [step, item, want, label] of [[plain.step, plain.item, false, "plain to-do"], [fire.step, fire.item, true, "Fire and forget"]]) {
        await open(step, item);
        const words = await chips(`[data-step="${step}"]`);
        check(`${at}: ${label} step ${want ? "keeps its tag" : "shows no To-do tag"}`,
          want ? words.includes("Fire and forget") : !words.includes("To-do"), words.join(", ") || "no tags");
        await page.screenshot({ path: path.join(work, `head-marks-${pagePath === "/" ? "main" : "classic"}-${width}-${step}.png`) });
      }
      // Cockpit's own work: the key alone after the title, and no priority icon in the head.
      await open(tool, toolItem.id);
      const head = page.locator(`[data-step="${tool}"] .step-context:visible`).first();
      await head.waitFor({ state: "visible", timeout: 10e3 });
      await sleep(300);
      // The title is Next up's heading, or the head's line on a row; either way it ends in the key.
      const card = page.locator(`:is(.next-card, .step-row)[data-step="${tool}"]:visible`).first();
      const shape = await settled(card, (el) => {
        const tag = el.querySelector(".task-heading > .chip.cockpit, .step-context > .title-line > .chip.cockpit");
        if (!tag || !tag.previousElementSibling.getClientRects().length) return null;
        const box = tag.getBoundingClientRect();
        return {
          after: Boolean(tag && tag.previousElementSibling && tag.previousElementSibling.matches(".item-title, .task-heading-text")),
          wordless: Boolean(tag && getComputedStyle(tag.querySelector(".chip-words")).display === "none"),
          seen: Boolean(box && box.width >= 14 && box.width <= 16),
          prio: el.querySelectorAll(".task-heading .prio, .step-context .prio").length,
          // From the end of the title's last word to the key, in px.
          gap: tag ? Math.round(box.left - (() => {
            const range = document.createRange();
            range.selectNodeContents(tag.previousElementSibling);
            return [...range.getClientRects()].at(-1).right;
          })()) : null,
          named: tag ? tag.getAttribute("aria-label") : null,
        };
      });
      check(`${at}: Cockpit key right after the title, without the word`,
        shape.after && shape.wordless && shape.seen && shape.gap >= 0 && shape.gap <= 12, JSON.stringify(shape));
      check(`${at}: the key is named Cockpit for screen readers`, shape.named === "Cockpit", JSON.stringify(shape));
      check(`${at}: no priority icon in the head`, shape.prio === 0, `${shape.prio} found`);
      if (!(width === 1280 && pagePath === "/classic")) {
        const level = pagePath === "/" ? "high" : "low";
        await head.locator(".step-menu > summary").click();
        await head.locator(".step-menu .menu-opens[data-priority]").click();
        const levels = await head.locator(".step-menu [role=menuitemradio]").evaluateAll((nodes) => nodes
          .filter((node) => node.getClientRects().length)
          .map((node) => `${node.textContent.trim()}:${node.getAttribute("aria-checked")}`));
        check(`${at}: Priority opens four levels, the current checked`,
          levels.length === 4 && levels.filter((line) => line.endsWith(":true")).length === 1, levels.join(", "));
        await page.screenshot({ path: path.join(work, `head-marks-${pagePath === "/" ? "main" : "classic"}-${width}-menu.png`) });
        await head.locator(".step-menu [role=menuitemradio]", { hasText: level[0].toUpperCase() + level.slice(1) }).click();
        let stored = null;
        for (let tries = 0; tries < 20 && stored !== level; tries++) {
          await sleep(250);
          stored = (await user("GET", "/api/state")).items.find((entry) => entry.id === toolItem.id)?.priority;
        }
        check(`${at}: choosing ${level} sets the item's priority`, stored === level, `stored ${stored}`);
      }
      // A long title ends in the key, with controls above a heading or level with a row's first line.
      await open(long, longItem.id);
      await sleep(300);
      // The updated status exercises the strip alongside its menu on headed cards.
      await page.evaluate((id) => { ui.seenText.set(id, "Earlier wording"); render(false); }, long);
      const longCard = page.locator(`:is(.next-card, .step-row)[data-step="${long}"]:visible`).first();
      const shapeBefore = await settled(longCard, longTitleGeometry);
      if (shapeBefore.headed) {
        const broken = await longCard.evaluate(longTitleGeometry, true);
        check(`${at}: heading check rejects controls moved onto its text`,
          broken.overlaps > 0 && !longTitleFits(broken), JSON.stringify(broken));
      }
      const lines = await settled(longCard, longTitleGeometry);
      check(`${at}: a long title ends in the key, with controls above its heading or level with its row's first line`,
        longTitleFits(lines), JSON.stringify(lines));
      const anywhere = await page.$$eval(".chip-words", (nodes) => nodes.filter((node) => node.textContent.trim() === "To-do").length);
      check(`${at}: no To-do tag anywhere on the page`, anywhere === 0, `${anywhere} found`);
      await page.screenshot({ path: path.join(work, `head-marks-${pagePath === "/" ? "main" : "classic"}-${width}.png`), fullPage: false });
      check(`${at}: no page errors`, errors.length === 0, errors.join(" | "));
      await context.close();
    }
  } finally {
    if (browser) await browser.close();
    server.kill("SIGTERM");
  }
  const failed = results.filter((result) => !result.ok).length;
  console.error(`${results.length - failed}/${results.length} passed; screenshots in ${work}`);
  process.exitCode = failed ? 1 : 0;
}

// The page redraws a step's card as it opens; a measurement taken mid-redraw finds the title without boxes and
// returns null, so it is taken again, for up to 5 s.
async function settled(locator, measure) {
  for (let tries = 0; tries < 20; tries++) {
    const result = await locator.evaluate(measure);
    if (result !== null) return result;
    await sleep(250);
  }
  // Still not there: say what the element held instead.
  return { missing: await locator.evaluate((el) => `${el.tagName}.${el.className}: ${el.innerHTML.slice(0, 400)}`) };
}

async function call(origin, headers, method, url, body) {
  const response = await fetch(`${origin}${url}`, { method, headers, body });
  const text = await response.text();
  if (!response.ok) throw new Error(`${method} ${url}: ${response.status} ${text}`);
  return text ? JSON.parse(text) : null;
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
