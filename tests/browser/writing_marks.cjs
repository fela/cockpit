// Good writing and Hard to understand are two checkable entries in a step card's ⋯ menu, one press each (SPEC
// FEEDBACK-8, VIEW-29). Checks, at 320, 390 and 1280 px in the light and the dark theme:
// - Next up's ⋯ menu lists both, visible and not clipped once it is open; the card shows no writing keys outside it;
// - every key of the card's action row stays inside the card, with short answers and with three long ones;
// - a press on each keeps the step as an example with that verdict, with no notice;
// - a press shows at once: the entry is checked while Cockpit has the request, settles once it kept the mark, with
//   Good writing's icon in green and Hard to understand's in red (checked or not), and the other verdict replaces it (one example per
//   step text); choosing the checked entry again clears the mark; a refused press comes back unchecked
//   with an error; after a reload the kept verdict shows checked;
// - the page does not scroll sideways, and has no page errors.
// Saves a screenshot of the top of the page per width and theme in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs)
// and drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/writing_marks.cjs

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
const TOKEN = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "writing-marks-test" };

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("writing-marks-");
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

    // The fixture database's open steps are snoozed, so this run's step is Next up.
    const initial = await user("GET", "/api/state");
    await Promise.all([...initial.queue, ...initial.ready_next, ...initial.snoozed].map((step) => user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
    const run = Date.now();
    const key = `note:writing-marks-${run}`;
    await agent("PUT", "/api/items", { key, title: "Rate this step's writing", project: "cockpit", priority: "urgent" });
    const answer = await agent("POST", "/api/steps", {
      item_key: key, need: `marks-${run}`, ask: "Is this step easy to read?", score: 100, session_id: SESSION_B,
      body: "Say yes or no.", answers: ["Yes", "No"],
    });
    const stepId = answer.step.id;
    // A second item whose step has three long answers, the case that crowds the row most beside Task done.
    const longKey = `note:writing-marks-long-${run}`;
    await agent("PUT", "/api/items", { key: longKey, title: "Rate long answers", project: "cockpit", priority: "high" });
    const longAnswer = await agent("POST", "/api/steps", {
      item_key: longKey, need: `marks-long-${run}`, ask: "Which version reads better?", score: 90, session_id: SESSION_B,
      body: "Pick one.", answers: ["The first version, with the table", "The second version, with the list", "Neither; write it again"],
    });
    const longStep = longAnswer.step.id;

    browser = await chromium.launch({ executablePath: CHROMIUM });
    for (const [width, height, scheme] of [[320, 740, "light"], [390, 844, "light"], [390, 844, "dark"], [641, 900, "light"], [1001, 900, "light"], [1280, 900, "light"], [1280, 900, "dark"]]) {
      const context = await browser.newContext({ viewport: { width, height }, colorScheme: scheme });
      const at = `${width} px, ${scheme}`;
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      // Each action waits for the page to settle after it, which takes minutes on a busy host.
      page.setDefaultTimeout(180e3);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      // The fixture can open the "Back after" dialog over the page while the user's timer is idle; answer it
      // as the user would, so it does not cover the menu.
      await page.addLocatorHandler(page.locator("dialog#idle[open]"), (dialog) => dialog.locator("button", { hasText: "Return" }).click());
      await page.goto(`${info.origin}/classic`);
      const card = page.locator(`#next [data-step="${stepId}"]`).first();
      // The first drawing can take a while on a busy host.
      await card.waitFor({ timeout: 120e3 });
      await page.screenshot({ path: path.join(work, `writing-marks-${width}-${scheme}.png`), timeout: 60e3 });

      const [good, bad] = [mark(card, "Good writing"), mark(card, "Hard to understand")];
      check(`${at}: the card shows no writing keys outside its menu`,
        await card.locator(".writing-keys, .step-keys button[aria-label='Good writing']").count() === 0, "old keys shown");
      await openMenu(card);
      check(`${at}: the open menu lists Good writing`, await shows(good), await describe(good));
      check(`${at}: the open menu lists Hard to understand`, await shows(bad), await describe(bad));
      check(`${at}: Hard to understand's tooltip says what a press keeps`, /writing to avoid|again to clear/.test(await tip(bad)), await tip(bad));
      await closeMenu(card);
      if (width === 390 || width === 1280) await pressChecks(page, at, card, stepId, user, check, work);
      else {
        // Each width leaves the step marked Good writing, where the press checks start.
        if ((await user("GET", "/api/state")).queue.find((step) => step.id === stepId).writing_verdict !== "bad") {
          await choose(card, bad);
          await card.locator('.step-menu [role=menuitemcheckbox].writing-bad[aria-checked="true"]:not(.writing-pending)').waitFor({ state: "attached" });
        }
        await choose(card, good);
        await card.locator('.step-menu [role=menuitemcheckbox].writing-good[aria-checked="true"]:not(.writing-pending)').waitFor({ state: "attached" });
        const kept = (await user("GET", "/api/writing")).filter((example) => example.step_id === stepId && example.kind === "step");
        check(`${at}: a press on each keeps one example of the step, with the last verdict`,
          kept.length === 1 && kept[0].verdict === "good", JSON.stringify(kept.map((example) => example.verdict)));
      }
      check(`${at}: every key of Next up's action row stays inside the card`, ...await inside(card));
      const sideways = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      check(`${at}: the page does not scroll sideways`, sideways <= 0, `${sideways} px`);
      check(`${at}: no page errors`, errors.length === 0, errors.join("; "));
      await context.close();
    }
    // The step with three long answers as Next up: the short one is snoozed.
    await user("POST", `/api/steps/${stepId}/snooze`, { until: "2099-01-01T08:00:00Z" });
    for (const [width, height] of [[320, 740], [390, 844], [641, 900], [1001, 900], [1280, 900]]) {
      const context = await browser.newContext({ viewport: { width, height } });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      // Each action waits for the page to settle after it, which takes minutes on a busy host.
      page.setDefaultTimeout(180e3);
      await page.goto(`${info.origin}/classic`);
      const card = page.locator(`#next [data-step="${longStep}"]`).first();
      await card.locator(".step-menu").waitFor({ timeout: 60e3 });
      await page.screenshot({ path: path.join(work, `writing-marks-long-${width}.png`), timeout: 60e3 });
      check(`${width} px: with three long answers every key stays inside the card`, ...await inside(card));
      const sideways = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      check(`${width} px: with three long answers the page does not scroll sideways`, sideways <= 0, `${sideways} px`);
      await context.close();
    }
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failed = results.filter((result) => !result.ok).length;
  console.error(`${results.length - failed} passed, ${failed} failed; screenshots in ${work}`);
  if (failed) process.exitCode = 1;
}

async function pressChecks(page, at, card, stepId, user, check, work) {
  const [good, bad] = [mark(card, "Good writing"), mark(card, "Hard to understand")];
  // The entry's checked state and, once checked, whether its icon has the theme's green (Good writing) or red.
  const look = (entry) => entry.evaluate((el) => {
    const theme = (name) => {
      const probe = document.createElement("span");
      probe.style.color = `var(${name})`;
      document.body.append(probe);
      const colour = getComputedStyle(probe).color;
      probe.remove();
      return colour;
    };
    const svg = el.querySelector(".menu-label svg");
    const colour = svg && getComputedStyle(svg).color;
    return {
      pressed: el.getAttribute("aria-checked"), pending: el.classList.contains("writing-pending"),
      themed: colour === theme(el.classList.contains("writing-good") ? "--ok" : "--fail"),
    };
  });
  const kept = async () => (await user("GET", "/api/writing")).filter((example) => example.step_id === stepId && example.kind === "step");
  // While `holding`, each POST to /api/writing waits in `held` until the test lets it through; when `refuse` is set,
  // it is refused. A try the page gives up on after 30 s is sent again (CODE-5) and waits again. `letThrough` sends
  // on the ones held and keeps holding; `releaseHeld` sends them on and stops holding, so a late try goes straight on.
  let posts = 0;
  const held = [];
  let holding = false;
  let refuse = false;
  await page.route("**/api/writing", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    posts += 1;
    if (refuse) return route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "test refusal" }) });
    if (holding) await new Promise((resolve) => { held.push({ verdict: JSON.parse(route.request().postData()).verdict, resolve }); });
    return route.continue().catch(() => {});
  });
  const letThrough = () => { for (const request of held.splice(0)) request.resolve(); };
  const releaseHeld = () => { holding = false; letThrough(); };
  const heldVerdicts = () => [...new Set(held.map((request) => request.verdict))];
  // A refresh after a press can take a minute on a busy host.
  const until = async (test, ms = 120e3) => {
    for (const end = Date.now() + ms; Date.now() < end; await sleep(100)) if (await test()) return true;
    return false;
  };
  // The step holds Good writing (or no verdict, on the first width): press Hard to understand first, then Good writing.
  const start = (await kept()).map((example) => example.verdict);
  // Both icons have their colour also while unchecked.
  const unchecked = await look(start.length ? bad : good);
  check(`${at}: the unchecked entry's icon has its colour too`, unchecked.pressed === "false" && unchecked.themed, JSON.stringify(unchecked));
  holding = true;
  await choose(card, bad);
  const pending = await until(async () => { const shown = await look(bad); return shown.pressed === "true" && shown.pending; }, 3e3);
  check(`${at}: Hard to understand shows checked at once, before Cockpit answers`, pending, JSON.stringify(await look(bad)));
  await openMenu(card);
  await page.screenshot({ path: path.join(work, `press-pending-${at.replace(/\W+/g, "-")}.png`) }).catch(() => {});
  await closeMenu(card);
  await until(async () => held.length > 0);
  releaseHeld();
  const settled = await until(async () => { const shown = await look(bad); return shown.pressed === "true" && !shown.pending; });
  const settledLook = await look(bad);
  check(`${at}: it settles once Cockpit kept it, its icon red`, settled && settledLook.themed, JSON.stringify(settledLook));
  await openMenu(card);
  await page.screenshot({ path: path.join(work, `press-settled-${at.replace(/\W+/g, "-")}.png`) }).catch(() => {});
  await closeMenu(card);
  check(`${at}: Good writing stays unchecked`, (await look(good)).pressed === "false", JSON.stringify(await look(good)));
  // The checked entry is the confirmation: no notice.
  const notice = await page.locator("#toast").evaluate((el) => (el.hidden ? "" : el.innerText));
  check(`${at}: a kept mark shows no notice`, !/Saved as/.test(notice), notice);

  holding = true;
  await choose(card, good);
  const swapped = await until(async () => (await look(good)).pressed === "true" && (await look(bad)).pressed === "false", 3e3);
  check(`${at}: Good writing replaces it on the card at once`, swapped, `${JSON.stringify(await look(good))} ${JSON.stringify(await look(bad))}`);
  await until(async () => held.length > 0);
  releaseHeld();
  await until(async () => !(await look(good)).pending, 120e3);
  check(`${at}: Good writing's icon shows green`, (await look(good)).themed, JSON.stringify(await look(good)));
  const after = (await kept()).map((example) => example.verdict);
  check(`${at}: Cockpit keeps one example of the step's text, with the last verdict`,
    after.length === 1 && after[0] === "good", `before ${JSON.stringify(start)}, after ${JSON.stringify(after)}`);

  // Hard to understand, then Good writing before Cockpit answered the first: both show at once, and Good writing,
  // sent once the first has its answer, is the one kept.
  holding = true;
  await choose(card, bad);
  await until(async () => held.length > 0);
  await choose(card, good);
  const shownLast = await until(async () => (await look(good)).pressed === "true" && (await look(bad)).pressed === "false", 3e3);
  // Good writing waits in the page until Cockpit answered Hard to understand.
  await sleep(1000);
  const waited = JSON.stringify(heldVerdicts()) === JSON.stringify(["bad"]);
  letThrough();
  await until(async () => heldVerdicts().includes("good"));
  releaseHeld();
  await until(async () => !(await look(good)).pending, 120e3);
  const last = (await kept()).map((example) => example.verdict);
  check(`${at}: the other key pressed while the first is on its way shows at once and is kept`,
    shownLast && waited && last.length === 1 && last[0] === "good", `shown ${shownLast}, waited ${waited}, kept ${JSON.stringify(last)}`);

  // Choosing the checked entry again clears the mark; a press after that marks the step again.
  check(`${at}: the checked entry's tooltip says a press clears it`, /again to clear/.test(await tip(good)), await tip(good));
  await choose(card, good);
  const cleared = await until(async () => (await look(good)).pressed === "false" && (await look(bad)).pressed === "false", 3e3);
  const gone = await until(async () => (await kept()).length === 0);
  check(`${at}: choosing the checked entry again clears the mark`, cleared && gone, `${JSON.stringify(await look(good))}, kept ${JSON.stringify(await kept())}`);
  await choose(card, good);
  await until(async () => (await look(good)).pressed === "true" && !(await look(good)).pending, 120e3);

  refuse = true;
  // The error shows for a few seconds: watch for it from the press.
  const errorShown = page.getByText("Not saved as hard to understand").first().waitFor({ timeout: 60e3 }).then(() => true, () => false);
  await choose(card, bad);
  const backUp = await until(async () => (await look(bad)).pressed === "false" && (await look(good)).pressed === "true");
  const error = await errorShown;
  check(`${at}: a refused press comes back up with an error, and the kept verdict shows again`, backUp && error,
    `${JSON.stringify(await look(bad))}, error shown: ${error}`);
  refuse = false;
  await page.unroute("**/api/writing");

  await page.reload();
  await card.waitFor({ timeout: 60e3 });
  const reloaded = await until(async () => (await look(good)).pressed === "true" && !(await look(good)).pending);
  check(`${at}: after a reload the kept verdict shows checked`, reloaded, JSON.stringify(await look(good)));
}

// The card's ⋯ menu and its writing entries. A choice closes the menu, so each press opens it first.
function mark(card, words) {
  return card.locator(".step-menu [role=menuitemcheckbox]", { hasText: words }).first();
}

async function openMenu(card) {
  const menu = card.locator(".step-menu").first();
  if (!(await menu.evaluate((el) => el.open))) await menu.locator(":scope > summary").click();
}

async function closeMenu(card) {
  const menu = card.locator(".step-menu").first();
  // Escape closes it as the page does (keyboard.js); once the page scrolls, the sticky header can cover ⋯.
  if (!(await menu.evaluate((el) => el.open))) return;
  await card.page().keyboard.press("Escape");
  await menu.evaluate((el) => new Promise((done) => { const at = Date.now(); (function wait() { if (!el.open || Date.now() - at > 5000) done(); else setTimeout(wait, 50); })(); }));
}

async function choose(card, entry) {
  await openMenu(card);
  await entry.click();
}

// motion.js moves a title into data-tip, which the page's own tooltip shows.
async function tip(entry) {
  return (await entry.getAttribute("data-tip")) || (await entry.getAttribute("title")) || "";
}

// Visible, and inside every ancestor that clips, within 15 s: the card's entrance can clip it for a moment.
async function shows(target) {
  for (const end = Date.now() + 15e3; Date.now() < end; await sleep(200)) {
    if (await showsNow(target)) return true;
  }
  return false;
}

async function showsNow(target) {
  if (await target.count() !== 1) return false;
  return target.evaluate((el) => {
    if (!el.checkVisibility({ visibilityProperty: true, opacityProperty: true })) return false;
    const own = el.getBoundingClientRect();
    // A fixed box, such as the menu's sheet on a phone, is clipped by no box around it: the walk stops there.
    for (let at = el.parentElement; at; at = at.parentElement) {
      const style = getComputedStyle(at);
      if (style.overflowX !== "visible" || style.overflowY !== "visible") {
        const box = at.getBoundingClientRect();
        if (own.left < box.left - 0.5 || own.right > box.right + 0.5 || own.top < box.top - 0.5 || own.bottom > box.bottom + 0.5) return false;
      }
      if (style.position === "fixed") break;
    }
    return true;
  });
}

// [ok, detail]: every key in the card's action row lies inside the card, left to right.
async function inside(card) {
  const outside = await card.evaluate((el) => {
    const box = el.getBoundingClientRect();
    const keys = [...el.querySelectorAll(".actions .button, .actions summary")]
      .filter((key) => key.checkVisibility())
      .map((key) => ({ name: key.getAttribute("aria-label") || key.textContent.trim(), r: key.getBoundingClientRect() }));
    const out = keys
      .filter(({ r }) => r.left < box.left - 0.5 || r.right > box.right + 0.5)
      .map(({ name, r }) => `${name} at ${Math.round(r.left)}–${Math.round(r.right)} in ${Math.round(box.left)}–${Math.round(box.right)}`);
    // Joined keys share a 1 px frame, so an overlap counts from 2 px.
    const over = (a, b) => Math.min(a.right, b.right) - Math.max(a.left, b.left) > 1.5 && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 1.5;
    for (let i = 0; i < keys.length; i++) {
      for (let j = i + 1; j < keys.length; j++) {
        if (over(keys[i].r, keys[j].r)) out.push(`${keys[i].name} overlaps ${keys[j].name}`);
      }
    }
    return out;
  });
  return [outside.length === 0, outside.join("; ")];
}

async function describe(target) {
  if (await target.count() === 0) return "no button";
  return target.first().evaluate((el) => {
    const own = el.getBoundingClientRect();
    let clip = null;
    for (let at = el.parentElement; at && !clip; at = at.parentElement) {
      const style = getComputedStyle(at);
      if (style.overflowX !== "visible" || style.overflowY !== "visible") {
        const box = at.getBoundingClientRect();
        if (own.left < box.left - 0.5 || own.right > box.right + 0.5 || own.top < box.top - 0.5 || own.bottom > box.bottom + 0.5) {
          clip = { cls: at.className, box };
        }
      }
      if (style.position === "fixed") break;
    }
    return JSON.stringify({ box: own, seen: el.checkVisibility(), clip });
  });
}

async function call(origin, headers, method, url, body) {
  const response = await fetch(`${origin}${url}`, { method, headers, body });
  const text = await response.text();
  if (!response.ok) throw new Error(`${method} ${url}: ${response.status} ${text}`);
  return text ? JSON.parse(text) : null;
}

// The harness's cookie file, once it is written and the server answers.
async function harnessUp(work, server) {
  const file = path.join(work, "cookie.json");
  // Allow time for fixture startup on a busy host.
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
