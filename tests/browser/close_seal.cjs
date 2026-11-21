// Closing a task is always explicit (SPEC CONTROL-17): it goes through a solid plum key, plays the close animation, and
// Finished this week's heading in the list on the left offers its Undo.
// Checks, on the main page at desktop width, for the four ways a user closes a task: a closing answer, Task done on the
// last step, Close item in its box, and Close on the card of an item with no step:
// - the key is solid plum (the page's --complete);
// - the pressed key releases a ring and plays one of twelve close animations; its pieces, or the GPU canvas, have
//   `data-close`; the pieces disappear within three seconds, and no checkmark appears;
// - Finished this week's folded heading offers Undo, and the server counts one more close each time;
// - that Undo brings the item back out of Finished this week; closing a task that Task done already finished counts
//   no second close, also after Undo.
// It also saves a screenshot of each close key, and of the item card's Close key at phone width, for the eye.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
// A run takes about a minute.
//
// Usage, from the checkout: node tests/browser/close_seal.cjs

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
// The harness's agent token and second fake session (tests/conftest.py); a closing answer needs a session to send to.
const SESSION_B = "22222222-2222-4222-8222-222222222222";
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "close-test", "content-type": "application/json" };
const CLOSING = "Sent it myself; close";

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-close-");
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

    // The fixture database's open steps are snoozed, so this run's steps take the user's places.
    const initial = await user("GET", "/api/state");
    await Promise.all([...initial.queue, ...initial.ready_next, ...initial.snoozed].map((step) => user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
    const run = Date.now();
    const steps = {};
    const items = {};
    for (const [name, score, answers] of [["answer", 100, [{ text: CLOSING, closes: true }, "Not yet"]], ["done", 90, []], ["close", 80, []]]) {
      const key = `note:close-${run}-${name}`;
      items[name] = (await agent("PUT", "/api/items", { key, title: `Close test: ${name}`, priority: "urgent" })).id;
      const answer = await agent("POST", "/api/steps", { item_key: key, need: `close-${name}`, ask: `Close test: ${name}`, score, answers, session_id: SESSION_B });
      steps[name] = answer.step.id;
      await sleep(1100);
    }
    const row = await agent("PUT", "/api/items", { key: `note:close-${run}-row`, title: "Close test: row", priority: "urgent" });

    browser = await chromium.launch({ executablePath: CHROMIUM });
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
    const page = await context.newPage();
    page.setDefaultTimeout(5000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`${info.origin}/`);
    await page.locator(`#next [data-step="${steps.answer}"]`).first().waitFor();
    // The GPU closes join the pick once their shaders compile; without WebGL2 the other seven play.
    await page.waitForFunction(() => window.GpuCloses.ready(), null, { timeout: 30000 }).catch(() => {});
    await page.evaluate(() => {
      const probe = document.createElement("span");
      probe.id = "plum-probe";
      probe.style.background = "var(--complete)";
      document.body.append(probe);
      window.__plum = (el) => getComputedStyle(el).backgroundColor === getComputedStyle(probe).backgroundColor;
    });

    // The step's card, picked in the list on the left, with its timer running. selectItem picks the item without
    // opening Finished this week, whose folded heading carries a close's Undo.
    async function card(name) {
      const id = steps[name];
      await user("POST", `/api/steps/${id}/timer/start`);
      await page.locator(`#sidebar li.side-item[data-item="${items[name]}"]`).waitFor({ state: "attached" });
      await page.evaluate((item) => selectItem(item), items[name]);
      const shown = page.locator(`#next .next-card[data-step="${id}"]`);
      await shown.locator(".actions").first().waitFor();
      return shown;
    }
    // A screenshot of one element into the work folder. The page redraws on every poll, which can swap the element
    // for a new one mid-shot. Scroll and read its page box in one DOM call, then shoot the page.
    async function shoot(locator, name) {
      const clip = await locator.evaluate((el) => {
        el.scrollIntoView({ behavior: "instant", block: "center", inline: "nearest" });
        const box = el.getBoundingClientRect();
        return { x: box.x + window.scrollX, y: box.y + window.scrollY, width: Math.max(box.width, 1), height: Math.max(box.height, 1) };
      });
      await page.screenshot({ path: path.join(work, name), clip });
    }
    // The Undo on Finished this week's folded heading.
    const headingUndo = page.locator("#sidebar details.sidebar-group.done > summary button.busy-undo");
    // `gone`: when the close took the card or row off the page, as a function and its argument (the page's
    // policy refuses string predicates).
    const stepGone = (id) => ({ check: (step) => !document.querySelector(`[data-step="${step}"]:not(.motion-ghost)`), arg: id });
    const serverCount = async () => (await user("GET", "/api/state")).counts.closed_by_user;
    const base = await serverCount();
    let closes = 0;

    // One close: the key is plum, the press plays the close, Finished this week offers its Undo, and the server counts it.
    async function close(name, key, gone) {
      // The pointer rests where the last key was pressed, often on this key's spot: off it, the key shows its own colour, not its hover.
      await page.mouse.move(0, 0);
      check(`${name}: the key is solid plum`, await key.evaluate((el) => window.__plum(el)),
        await key.evaluate((el) => {
          const rules = [];
          const walk = (list, sheet) => { for (const rule of list) { if (rule.cssRules && !rule.selectorText) walk(rule.cssRules, sheet); else if (rule.selectorText && /background|transition/.test(rule.style.cssText) && el.matches(rule.selectorText)) rules.push(`${sheet.split("/").pop()}: ${rule.selectorText} -> ${rule.style.cssText.slice(0, 120)}`); } };
          for (const sheet of document.styleSheets) walk(sheet.cssRules, sheet.href);
          return `${getComputedStyle(el).backgroundColor}; rules: ${rules.join(" || ")}`;
        }));
      await page.evaluate(() => {
        window.__close = { ring: false, names: [], seal: false };
        new MutationObserver(() => {
          const seen = window.__close;
          seen.ring ||= Boolean(document.querySelector("body > .press-ring"));
          for (const el of document.querySelectorAll("body > [data-close]")) if (!seen.names.includes(el.dataset.close)) seen.names.push(el.dataset.close);
          seen.seal ||= Boolean(document.querySelector("body > .seal"));
        }).observe(document.body, { childList: true });
      });
      await key.click();
      closes += 1;
      const left = await page.waitForFunction(gone.check, gone.arg).then(() => true, () => false);
      if (!left) {
        await page.screenshot({ path: path.join(work, `stuck-${closes}.png`) });
        throw new Error(`${name}: still on the page (screenshot in ${work})`);
      }
      await sleep(120);
      const seen = await page.evaluate(() => window.__close);
      check(`${name}: a ring leaves the key`, seen.ring, JSON.stringify(seen));
      const kinds = ["tear", "stamp", "slingshot", "confetti", "coin", "portal", "burn", "ignite", "snap", "vortex", "blast"];
      check(`${name}: one close animation plays`, seen.names.length === 1 && kinds.includes(seen.names[0]), JSON.stringify(seen));
      check(`${name}: no checkmark`, !seen.seal, JSON.stringify(seen));
      check(`${name}: its pieces are gone within three seconds`,
        await page.waitForFunction(() => !document.querySelector("body > [data-close]"), null, { timeout: 3000 }).then(() => true, () => false));
      const offered = await headingUndo.waitFor({ timeout: 4000 }).then(() => true, () => false);
      check(`${name}: Finished this week's heading offers Undo`, offered,
        await page.locator("#sidebar details.sidebar-group.done > summary").textContent().catch(() => "no Finished this week"));
      const counted = await waitFor(serverCount, (count) => count === base + closes);
      check(`${name}: the server counts it`, counted === base + closes, `${counted} for ${base + closes}`);
    }

    const answerCard = await card("answer");
    await shoot(answerCard.locator(".answers button.answer.closes").first(), "key-answer.png");
    await close("Closing answer", answerCard.locator(".answers button.answer.closes").first(), stepGone(steps.answer));
    await shoot(page.locator("#sidebar details.sidebar-group.done > summary"), "heading-undo.png");

    const doneCard = await card("done");
    const doneKey = doneCard.locator(".actions button.task-done").first();
    await shoot(doneKey, "key-done.png");
    await close("Task done", doneKey, stepGone(steps.done));

    const closeCard = await card("close");
    await closeCard.locator("details.step-menu > summary").first().click();
    await closeCard.locator("details.step-menu .menu-list button").filter({ hasText: "Close task" }).first().click();
    const box = page.locator(`[data-step="${steps.close}"] .close-box`).first();
    await box.locator("textarea").fill("Close test: not needed");
    await shoot(box, "key-close.png");
    // E on Next up waits while a close reason is typed: it would otherwise finish the task past it.
    const onNext = (await page.locator(`#next [data-step="${steps.close}"]`).count()) > 0;
    await page.locator("#next").first().click({ position: { x: 2, y: 2 } });
    await page.keyboard.press("e");
    await sleep(800);
    const held = { onNext, count: await serverCount(), there: await page.locator(`[data-step="${steps.close}"] .close-box textarea`).count() };
    check("E with a close reason typed: the task stays open", onNext && held.count === base + closes && held.there === 1, JSON.stringify(held));
    await close("Close item", box.locator("button.close-item"), stepGone(steps.close));

    // An item with no step: its card, picked in the list, has its own Close key.
    const itemCard = async (id) => {
      await page.locator(`#sidebar li.side-item[data-item="${id}"]`).waitFor({ state: "attached" });
      await page.evaluate((item) => selectItem(item), id);
      const key = page.locator(`#next .item-card[data-item="${id}"] button.close-item`).first();
      await key.waitFor();
      return key;
    };
    // Whether the list shows the item outside Finished this week.
    const outsideDone = (id) => [...document.querySelectorAll(`#sidebar li.side-item[data-item="${id}"]`)].some((el) => !el.closest(".sidebar-group.done"));
    const rowKey = await itemCard(row.id);
    await shoot(rowKey, "key-row.png");
    // The closed item moves to Finished this week (VIEW-20), so its row leaves the open groups.
    await close("Close on an item's card", rowKey, { check: (id) => ![...document.querySelectorAll(`#sidebar li.side-item[data-item="${id}"]`)]
      .some((el) => !el.closest(".sidebar-group.done")), arg: row.id });
    await headingUndo.click();
    closes -= 1;
    const back = await page.waitForFunction(outsideDone, row.id, { timeout: 4000 }).then(() => true, () => false);
    check("Close on an item's card: Undo brings its row back", back, "the row did not come back");
    await sleep(800);
    const item = (await user("GET", "/api/state")).items.find((other) => other.id === row.id);
    check("Close on an item's card: Undo reaches the server", Boolean(item) && !item.archived, JSON.stringify(item && item.archived));

    // Close on the card of the item Task done completed: it already counts, so the count stays, also after Undo.
    await (await itemCard(items.done)).click();
    const offered = await headingUndo.waitFor({ timeout: 4000 }).then(() => true, () => false);
    check("Close on a completed item: Finished this week's heading offers Undo", offered, "no Undo");
    await sleep(800);
    check("Close on a completed item: the server still counts it once", (await serverCount()) === base + closes, `${await serverCount()} for ${base + closes}`);
    await headingUndo.click();
    await sleep(800);
    check("Close on a completed item: after Undo it still counts", (await serverCount()) === base + closes, `${await serverCount()} for ${base + closes}`);
    check("no page errors", errors.length === 0, errors.join(" | "));

    // The phone's look of an item card's Close key.
    await page.setViewportSize({ width: 390, height: 844 });
    await page.evaluate(() => window.scrollTo(0, 0));
    await itemCard(row.id);
    await shoot(page.locator(`#next .item-card[data-item="${row.id}"] .item-actions`).first(), "phone-row.png");
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

// The value `read` gives once `ok` holds it, or its last value after 4 s.
async function waitFor(read, ok) {
  const end = Date.now() + 4000;
  let value = await read();
  while (!ok(value) && Date.now() < end) {
    await sleep(200);
    value = await read();
  }
  return value;
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
