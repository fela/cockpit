// Steps useful only until a time (SPEC NEXT-60). Four ordinary steps take the queue's places, then a session puts a
// sign-in step that works for five more minutes, scored lowest: the server gives it a place at once and it leads the
// queue. Checks, at desktop light, desktop dark with reduced motion, phone light and phone dark:
// - its Inbox row comes first, marked as a step whose time runs, with the time left counting down in place of its age;
// - the shown row holds still in the picked colours; once the user is on another step, the row pulses in the warnings'
//   red, and holds still with reduced motion;
// - its card opens with a band that says until when it works and counts what is left;
// - at phone width nothing scrolls sideways, and the page logs no errors.
// Then the session puts the step again with a time a few seconds ahead, and the page's state is held still: once the
// time runs out, the page draws the row and the band in the look after the time on its own, and once Cockpit asked
// the session to renew the step, the band says so.
//
// Starts serve_preview.py (this checkout's Cockpit on a fixture database; README.md says what it needs) and drives a
// headless Chromium. Each /api/state answer goes to the page without warnings, so only this run's steps lead Inbox;
// the shots with a warning add one. Prints one line per check and exits 1 when one failed. Screenshots go to the
// folder given as the first argument, when one is given: a-*.png for each look, a-with-warning-*.png beside a
// warning, a-expired-*.png after the time.
//
// Usage, from the checkout: node tests/browser/expiring_steps.cjs [screenshot folder]

"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { startPreview, fullState } = require("./work.cjs");

// Browser dependencies can be selected through the environment (README.md).
const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

// The harness's agent token and second fake session (tests/conftest.py).
const SESSION_B = "22222222-2222-4222-8222-222222222222";
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "expiring-steps-test", "content-type": "application/json" };
const ORDINARY = [
  { title: "Approve the CSV export", ask: "The CSV export now has the reminder column: ship it to everyone?", score: 80 },
  { title: "Review reminder settings", ask: "Look over the new reminder settings page before it goes out", score: 70 },
  { title: "Check list sharing", ask: "Shared lists now open read-only for guests: is that what you want?", score: 60 },
  { title: "Choose the weekly reminder", ask: "Send the weekly reminder on Monday 09:00 or on Friday 16:00?", score: 50 },
];
const TIMED = {
  title: "Connect Slack reminders",
  ask: "Open the Slack sign-in link and paste the address back",
  body: "Slack's sign-in link works for ten minutes from when I made it. Open it, sign in to the work Slack, and paste the address the browser lands on.",
};
const WARNING = "The sweep that started at 03:00 UTC last wrote at 03:05 UTC and stopped without finishing.";
// Every warning the state can carry, empty.
const QUIET = {
  warnings: [], account_problems: [], codex_sign_ins: [], claude_sign_ins: [], wrong_models: [],
  permission_requests: [], settled_requests: [], waiting_sessions: [], codex_requests: [],
};
const LOOKS = [
  { name: "desktop", width: 1280, height: 900, colorScheme: "light", reducedMotion: "no-preference", phone: false },
  { name: "desktop-dark", width: 1280, height: 900, colorScheme: "dark", reducedMotion: "reduce", phone: false },
  { name: "phone", width: 390, height: 844, colorScheme: "light", reducedMotion: "no-preference", phone: true },
  { name: "phone-dark", width: 390, height: 844, colorScheme: "dark", reducedMotion: "no-preference", phone: true },
];

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const shots = process.argv[2] || null;
  if (shots) fs.mkdirSync(shots, { recursive: true });
  const { info } = await startPreview("cockpit-expiring-steps-");
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const cookie = { cookie: `${info.name}=${info.value}`, origin: info.origin, "content-type": "application/json" };
    const agent = (method, url, body) => call(info.origin, AGENT, method, url, body);
    const user = (method, url, body) => call(info.origin, cookie, method, url, body);

    // The fixture's open steps go and its timer stops, so this run's steps are the queue.
    const initial = await user("GET", "/api/state");
    if (initial.timer) await user("POST", `/api/steps/${initial.timer.step_id}/timer/pause`);
    for (const step of [...initial.queue, ...initial.ready_next]) await user("POST", `/api/steps/${step.id}/dismiss`, {});

    // One step on each task, as a task shows one card at a time.
    const ordinary = [];
    for (const [index, one] of ORDINARY.entries()) {
      const key = `note:expiring-steps-${index}-${Date.now()}`;
      const item = await agent("PUT", "/api/items", { key, title: one.title, stage: "building", sessions: [SESSION_B] });
      const put = await agent("POST", "/api/steps", { item_key: key, need: `decide:expiring-${index}`, ask: one.ask, session_id: SESSION_B, score: one.score });
      ordinary.push({ item: item.id, step: put.step.id });
    }
    const timedKey = `note:expiring-steps-slack-${Date.now()}`;
    const timedItem = (await agent("PUT", "/api/items", { key: timedKey, title: TIMED.title, stage: "building", sessions: [SESSION_B] })).id;
    const timed = { item_key: timedKey, need: "sign-in:slack", ask: TIMED.ask, body: TIMED.body, session_id: SESSION_B, score: 40 };
    const firstEnds = secondsAhead(300);
    const id = (await agent("POST", "/api/steps", { ...timed, expires_at: firstEnds })).step.id;

    const state = await user("GET", "/api/state");
    check("the server gives the timed step a place at once and leads the queue with it",
      state.queue[0]?.id === id && state.ready_next.some((step) => step.id === ordinary[3].step),
      JSON.stringify({ queue: state.queue.map((step) => step.id), ready: state.ready_next.map((step) => step.id), id }));

    browser = await chromium.launch({ executablePath: CHROMIUM });
    for (const look of LOOKS) {
      const { context, page, errors, tweak } = await openPage(browser, info, look, id);
      await page.waitForSelector("#sidebar li.expiring-row", { state: "attached" });
      // Start from Next up with nothing picked and no step opened.
      await page.evaluate(() => { ui.placed.clear(); selectItem(null); });
      if (look.phone) await page.evaluate(() => setPhoneScreen(false, false));
      await sleep(400);

      const row = page.locator(`#sidebar li[data-step="${id}"]`);
      const first = await page.evaluate(() => document.querySelector("#sidebar .sidebar-group.queue li")?.dataset.step);
      check(`${look.name}: the timed step's row comes first in Inbox`, first === String(id), `first row's step ${first}, timed step ${id}`);
      const marked = await rowLook(page, id);
      check(`${look.name}: the row is marked as a step whose time runs`, marked.timed && marked.lead, marked.className);

      const age = row.locator(".sidebar-age");
      const before = await age.textContent();
      // The page reads the server's clock from each poll, so a slow answer can hold the count for a moment.
      await page.waitForFunction(([step, was]) => document.querySelector(`#sidebar li[data-step="${step}"] .sidebar-age`)?.textContent !== was,
        [id, before], { timeout: 10000 }).catch(() => {});
      const after = await age.textContent();
      check(`${look.name}: the row counts down the time left in place of its age`, /^\d:\d\d$/.test(before) && before !== after, `${before} then ${after}`);

      const card = page.locator(`#next article.next-card[data-step="${id}"]`);
      const band = card.locator(".expiry-banner:not(.ended)");
      const until = await page.evaluate((iso) => clock(iso), firstEnds);
      const words = (await band.innerText().catch(() => "")).replace(/\s+/g, " ").trim();
      check(`${look.name}: the card opens with a band that says until when it works and what is left`,
        new RegExp(`^Works until ${until} \\d:\\d\\d left$`).test(words), `"${words}" (until ${until})`);
      if (look.name === "desktop") {
        const own = await page.evaluate(() => {
          const at = "2026-01-01T00:00:00Z";
          const one = (id, score, more) => ({ id, score, created_at: at, queued_at: at, taken_at: null, ...more });
          const soon = new Date(Date.now() + ui.clockOffset + 300000).toISOString();
          const lists = { snoozed: [], with_agents: [], counts: {}, now: at };
          const waiting = [one(5, 59, { queued_at: null }), one(6, 55, { queued_at: null })];
          const closed = resorted({ ...lists, queue: [one(1, 1, { expires_at: soon }), one(2, 70), one(3, 60), one(4, 50)], ready_next: waiting },
            { ...lists, queue: [one(1, 1, { expires_at: soon }), one(3, 60), one(4, 50)], ready_next: waiting }, 0);
          const newest = one(1, 1, { expires_at: soon, queued_at: "2026-01-02T00:00:00Z" });
          const undone = givenBack({ ...lists, queue: [one(2, 70), one(3, 60), one(4, 50), newest], ready_next: [] },
            { ...lists, queue: [one(2, 70), one(3, 60), one(4, 50), newest, one(7, 40)], ready_next: [] });
          const was = ui.clockOffset;
          ui.clockOffset = 600000;
          const ahead = new Date(Date.now() + 300000).toISOString();
          const behind = { runs: expiring({ expires_at: ahead }), left: secondsLeft(ahead) };
          ui.clockOffset = was;
          return { closed: closed.queue.map((step) => step.id), undone: undone.queue.map((step) => step.id), behind,
            kept: expiryBanner({ expires_at: soon, left: true }) };
        });
        check("desktop: a close and an undo, drawn before the server answers, leave the timed step its place",
          own.closed.includes(1) && own.closed.includes(5) && !own.closed.includes(4) && own.undone.includes(1) && own.undone.length === 4, JSON.stringify(own));
        check("desktop: with the browser's clock ten minutes behind the server's, a time that passed five minutes ago counts as passed",
          !own.behind.runs && own.behind.left === 0, JSON.stringify(own.behind));
        check("desktop: a card kept after its step closed has no band", own.kept === null, String(own.kept));
      }
      const picked = await rowLook(page, id);
      check(`${look.name}: the shown row holds still in the picked colours`, picked.sel && picked.pulse.length === 0
        && Math.abs(lightness(picked.title) - lightness(picked.pickedFail)) < 0.03 && Math.abs(lightness(picked.back) - lightness(picked.pickedBack)) < 0.03,
        JSON.stringify(picked));
      if (look.phone) {
        const sideways = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
        check(`${look.name}: nothing scrolls sideways`, sideways <= 0, `${sideways}px wider`);
      }
      await shoot(page, shots, `a-${look.name}.png`);

      if (!look.phone) {
        tweak.warnings = [WARNING];
        await page.evaluate(() => refresh());
        await page.waitForSelector("#sidebar li.warn-row", { state: "attached" });
        await page.evaluate(() => selectItem(null));
        await sleep(300);
        const lead = await page.evaluate((step) => ({
          rows: [...document.querySelectorAll("#sidebar .sidebar-group.queue li")].slice(0, 2).map((node) => node.dataset.step || node.className),
          card: document.querySelector("#next article.next-card")?.dataset.step,
        }), id);
        check(`${look.name}: beside a warning, the warning row leads and Next up keeps the timed step`,
          lead.rows[0].includes("warn-row") && lead.rows[1] === String(id) && lead.card === String(id), JSON.stringify(lead));
        await shoot(page, shots, `a-with-warning-${look.name}.png`);
        tweak.warnings = [];
        await page.evaluate(() => refresh());
        await page.waitForSelector("#sidebar li.warn-row", { state: "detached" });
      }

      // The user on another step: the timed step's row is no longer the shown one.
      await page.evaluate((item) => selectItem(item), ordinary[0].item);
      if (look.phone) await page.evaluate(() => setPhoneScreen(true, false));
      await sleep(400);
      const plain = await rowLook(page, id);
      const still = look.reducedMotion === "reduce";
      check(`${look.name}: on another step, the row ${still ? "holds still with reduced motion" : "pulses"} in the warnings' red`,
        !plain.sel && (still ? plain.pulse.length === 0 : plain.pulse.includes("warn-row-pulse")) && Math.abs(lightness(plain.title) - lightness(plain.fail)) < 0.03,
        JSON.stringify(plain));
      await shoot(page, shots, look.phone ? `a-${look.name}-list.png` : `a-${look.name}-on-another.png`);

      check(`${look.name}: no page errors`, errors.length === 0, errors.join(" | "));
      await context.unrouteAll({ behavior: "ignoreErrors" });
      await context.close();
    }

    // The session puts the step again with a time a few seconds ahead, enough for the page to load.
    const ends = secondsAhead(15);
    await agent("POST", "/api/steps", { ...timed, expires_at: ends });
    for (const look of LOOKS.filter((one) => !one.phone)) {
      const { context, page, errors, tweak } = await openPage(browser, info, look, id);
      const row = page.locator(`#sidebar li[data-step="${id}"]`);
      const band = page.locator(`#next article.next-card[data-step="${id}"] .expiry-banner`);
      await page.waitForSelector(`#sidebar li[data-step="${id}"]`, { state: "attached" });
      await page.evaluate((item) => selectItem(item), timedItem);
      const until = await page.evaluate((iso) => clock(iso), ends);
      if (look.name === "desktop") {
        check(`${look.name}: the step put again counts down from its new time`, (await row.evaluate((node) => node.classList.contains("expiring-row")))
          && /^0:[01]\d$/.test(await row.locator(".sidebar-age").textContent()), await row.locator(".sidebar-age").textContent());
        // From here the page gets the same state on every poll, so only the page's own count can change it.
        tweak.frozen = true;
        // The user types in the card: a redraw leaves such a card alone, so the count itself changes the band.
        await page.locator(`#next article.next-card[data-step="${id}"] textarea`).first().focus();
        await page.waitForSelector(`#sidebar li[data-step="${id}"] .sidebar-expired`, { state: "attached", timeout: 25000 });
        const ended = await row.evaluate((node) => ({ timed: node.classList.contains("expiring-row"),
          words: node.querySelector(".sidebar-expired").getAttribute("aria-label"), age: node.querySelector(".sidebar-age").dataset.expires }));
        check(`${look.name}: once the time runs out, the row is an ordinary one with a small stopwatch that says when`,
          !ended.timed && ended.words === `Its time ran out at ${until}` && ended.age === undefined, JSON.stringify(ended));
        const words = (await band.innerText()).replace(/\s+/g, " ").trim();
        const typing = await page.evaluate(() => document.activeElement?.tagName);
        check(`${look.name}: the band says the time ran out, in the muted look, while the user types in the card`,
          (await band.evaluate((node) => node.classList.contains("ended"))) && words === `Its time ran out at ${until}.` && typing === "TEXTAREA",
          `"${words}", focus on ${typing}`);
        // The user leaves the field, so the next poll draws the card again.
        await page.evaluate(() => document.activeElement?.blur());
        tweak.frozen = false;
      }
      tweak.told = new Date().toISOString();
      await page.evaluate(() => refresh());
      await page.waitForFunction(() => /renew/.test(document.querySelector("#next .expiry-banner")?.textContent || ""));
      const told = (await band.innerText()).replace(/\s+/g, " ").trim();
      check(`${look.name}: once Cockpit asked the session to renew the step, the band says so`,
        told === `Its time ran out at ${until}. The session was asked to renew it.`, `"${told}"`);
      await sleep(300);
      await shoot(page, shots, `a-expired-${look.name}.png`);
      check(`${look.name} after the time: no page errors`, errors.length === 0, errors.join(" | "));
      await context.unrouteAll({ behavior: "ignoreErrors" });
      await context.close();
    }
  } finally {
    if (browser) await browser.close();
  }
  const failedChecks = results.filter((result) => !result.ok);
  console.error(failedChecks.length ? `${failedChecks.length} of ${results.length} checks failed` : `all ${results.length} checks passed`);
  process.exitCode = failedChecks.length ? 1 : 0;
}

// A page in `look` whose /api/state answers carry `tweak.warnings` as their only warnings; while `tweak.frozen`
// holds, the last answer again; with `tweak.told`, step `id` as one whose session Cockpit asked to renew it then.
async function openPage(browser, info, look, id) {
  const context = await browser.newContext({ viewport: { width: look.width, height: look.height }, colorScheme: look.colorScheme,
    reducedMotion: look.reducedMotion, isMobile: look.phone, hasTouch: look.phone });
  await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
  const tweak = { warnings: [], frozen: false, told: null };
  let last = null;
  let lastAt = 0;
  await context.route("**/api/state", async (route) => {
    if (tweak.frozen && last) {
      // The same state with the server's clock moved on, as the page counts a step's time by that clock.
      const now = new Date(Date.parse(last.now) + Date.now() - lastAt).toISOString();
      await route.fulfill({ json: { ...last, now } }).catch(() => {});
      return;
    }
    // A poll still on its way when the context closes has nothing to answer.
    const full = await fullState(route).catch(() => null);
    if (!full) return;
    const state = await full.response.json();
    const told = (step) => (step.id === id && tweak.told ? { ...step, expiry_told_at: tweak.told } : step);
    // The fixture can hold a question about time away, whose dialog would cover the page.
    lastAt = Date.now();
    last = { ...state, ...QUIET, warnings: tweak.warnings, away: null, queue: state.queue.map(told), ready_next: state.ready_next.map(told) };
    await route.fulfill({ ...full, json: last }).catch(() => {});
  });
  const page = await context.newPage();
  page.setDefaultTimeout(30000);
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => { if (message.type() === "error") errors.push(`${message.text()} (${message.location().url})`); });
  await page.goto(`${info.origin}/`, { waitUntil: "load", timeout: 120e3 });
  return { context, page, errors, tweak };
}

// The step's Inbox row read in one go: a redraw can replace the row between a locator's lookup and its evaluation,
// which reads a detached node.
function rowLook(page, id) {
  return page.evaluate((step) => {
    const node = document.querySelector(`#sidebar li[data-step="${step}"]`);
    return { className: node.className, timed: node.classList.contains("expiring-row"), sel: node.classList.contains("sel"),
      lead: node.querySelector(".sidebar-lead .icon") !== null,
      pulse: node.querySelector(".side-row").getAnimations().map((one) => one.animationName),
      title: getComputedStyle(node.querySelector(".sidebar-title")).color,
      back: getComputedStyle(node.querySelector(".side-row")).backgroundColor,
      fail: getComputedStyle(document.documentElement).getPropertyValue("--fail").trim(),
      pickedFail: getComputedStyle(document.documentElement).getPropertyValue("--picked-fail").trim(),
      pickedBack: getComputedStyle(document.documentElement).getPropertyValue("--picked-bg").trim() };
  }, id);
}

// The lightness of a computed oklch(), oklab() or rgb() colour, 0 to 1.
function lightness(color) {
  const oklch = /okl(?:ch|ab)\(([\d.]+)/.exec(color);
  if (oklch) return Number(oklch[1]);
  const [r, g, b] = (color.match(/[\d.]+/g) || []).map(Number);
  return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
}

async function shoot(page, shots, name) {
  if (shots) await page.screenshot({ path: path.join(shots, name), timeout: 30000 });
}

function secondsAhead(seconds) {
  return new Date(Date.now() + seconds * 1000).toISOString();
}

async function call(origin, headers, method, url, body) {
  const response = await fetch(`${origin}${url}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await response.text();
  if (!response.ok) throw new Error(`${method} ${url}: ${response.status} ${text}`);
  return text ? JSON.parse(text) : null;
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
