// The Mac's record on the page (TRACK-9, TRACK-10). Checks, at laptop and phone width, light and dark:
// - "Your time" offers "Connect a Mac" while none is connected, and pressing it shows the Terminal line with a one-time
//   code, which the installer trades for the Mac's key;
// - minutes the Mac sends with that key show under "On your Mac today": the bar, the totals, away time and the labels;
// - pressing a label's category moves it on to the next one, and Cockpit keeps the choice;
// - Pause sends the pause, and the panel then offers Resume;
// - a step's ⋯ menu shows where its timer time went on the Mac, from the item's breakdown;
// - on /, a #time link and the logo menu open Your time in the center layer;
// - no sideways scroll and no page errors.
//
// Starts serve_preview.py (README.md says what it needs) and drives a headless Chromium. The item's breakdown is
// answered by the test, since the timer spans it needs lie in the past; tests/test_activity.py covers the server's.
// Prints one line per check and exits 1 when one failed. Screenshots go to the folder given as the first argument.
//
// Usage, from the checkout: node tests/browser/mac_activity.cjs [screenshot folder]

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
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "mac-activity-test", "content-type": "application/json" };
const ZONE = "Asia/Jakarta";

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// A minute `back` minutes ago, as the Mac sends it (UTC, to the minute).
function minuteAgo(back) {
  return new Date(Date.now() - back * 60e3).toISOString().slice(0, 16);
}

async function main() {
  const shots = process.argv[2] || null;
  if (shots) fs.mkdirSync(shots, { recursive: true });
  const work = workFolder("cockpit-mac-activity-");
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

    const initial = await user("GET", "/api/state");
    if (initial.timer) await user("POST", `/api/steps/${initial.timer.step_id}/timer/pause`);
    await Promise.all([...initial.queue, ...initial.ready_next, ...initial.snoozed].map((step) => user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
    const key = "note:mac-activity";
    await agent("PUT", "/api/items", { key, title: "Mac record test", priority: "urgent" });
    const answer = await agent("POST", "/api/steps", {
      item_key: key, need: "review:mac-activity", ask: "Mac record test", score: 100, session_id: SESSION_B, body: "Mac record test body.",
    });
    const itemId = answer.step.item_id;
    // Some timer time on the item, so its menu has a time line for the note to sit under.
    await user("POST", `/api/steps/${answer.step.id}/timer/start`);
    await sleep(1500);
    await user("POST", `/api/steps/${answer.step.id}/timer/pause`);
    browser = await chromium.launch({ executablePath: CHROMIUM });

    let first = true;
    for (const [width, scheme] of [[1440, "light"], [1440, "dark"], [390, "light"]]) {
      const context = await browser.newContext({
        viewport: { width, height: 1000 }, isMobile: width < 600, hasTouch: width < 600, colorScheme: scheme, timezoneId: ZONE,
      });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      page.setDefaultTimeout(30000);
      // A minute without the mouse fades the idle veil in over the page; a move clears it.
      let moves = 0;
      await page.addLocatorHandler(page.locator("#idle-veil"), () => page.mouse.move(5 + (moves++ % 2), 5)
        .catch((error) => { if (!/has been closed/.test(error.message)) throw error; }));
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
      await page.route(`**/api/items/${itemId}/activity`, (route) => route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ labels: [
          { label: "Cursor", category: "work", minutes: 30 }, { label: "github.com", category: "work", minutes: 10 },
          { label: "Slack", category: "neutral", minutes: 4 }, { label: "youtube.com", category: "distraction", minutes: 2 },
          { label: "Finder", category: "neutral", minutes: 1 },
        ], away: 3 }),
      }));
      await page.goto(`${info.origin}/classic`, { timeout: 120e3 });
      const tag = `${width}-${scheme}`;
      const panel = page.locator("#time");
      const openPanel = async () => {
        if (!(await panel.locator("details").first().evaluate((el) => el.open))) await panel.locator("summary").first().click();
      };

      if (first) {
        await openPanel();
        const connect = panel.getByRole("button", { name: "Connect a Mac" });
        await connect.waitFor();
        check(`${tag}: "Connect a Mac" shows before a Mac is connected`, true, "");
        if (shots) await panel.screenshot({ path: path.join(shots, `connect-${tag}.png`) });
        await connect.click();
        const line = panel.locator(".mac-install code");
        await line.waitFor();
        const text = await line.textContent();
        const start = `curl -fsSL ${info.origin}/activity/install.sh | bash -s -- `;
        check(`${tag}: the Terminal line installs from Cockpit with a one-time code`, text.startsWith(start) && /^[A-Za-z0-9_-]{22}$/.test(text.slice(start.length)), text);
        check(`${tag}: the line is the only thing to copy`, (await panel.getByRole("button", { name: "Copy line" }).isVisible()) && (await panel.getByRole("button", { name: "Copy key" }).count()) === 0, "");
        const macKey = (await call(info.origin, { "content-type": "application/json" }, "POST", "/api/activity/claim", { code: text.slice(start.length) })).key;
        check(`${tag}: the installer's claim of the code gives the Mac's key`, Boolean(macKey), "");
        if (shots) await panel.screenshot({ path: path.join(shots, `install-${tag}.png`) });
        // The Mac sends 40 minutes: Cursor, github.com in two browsers, YouTube, a new app, then away.
        const minutes = [];
        for (let back = 45; back > 5; back -= 1) {
          let entry = { app: "Cursor", site: null, away: false };
          if (back <= 30) entry = { app: back % 2 ? "Safari" : "Google Chrome", site: "github.com", away: false };
          if (back <= 20) entry = { app: "Safari", site: "youtube.com", away: false };
          if (back <= 15) entry = { app: "Figma", site: null, away: false };
          if (back <= 10) entry = { app: "loginwindow", site: null, away: true };
          minutes.push({ minute: minuteAgo(back), ...entry });
        }
        const sent = await call(info.origin, { authorization: `Bearer ${macKey}`, "content-type": "application/json" }, "POST", "/api/activity/minutes", { minutes });
        check(`${tag}: Cockpit stores the Mac's minutes`, sent.stored === 40, JSON.stringify(sent));
        await page.getByRole("button", { name: "Reload" }).first().click();
        first = false;
      }

      await openPanel();
      const labels = panel.locator(".mac-labels li");
      await labels.first().waitFor();
      const names = await panel.locator(".mac-labels .time-name").allTextContents();
      check(`${tag}: one row per label, the longest first`, JSON.stringify(names.slice(0, 2)) === JSON.stringify(["Cursor", "github.com"]) && names.filter((name) => name === "github.com").length === 1, JSON.stringify(names));
      const totals = await panel.locator(".mac-totals").textContent();
      check(`${tag}: the totals name work, distraction and away`, /Work/.test(totals) && /Distraction/.test(totals) && /Away/.test(totals), totals);
      check(`${tag}: the bar shows the categories`, (await panel.locator(".mac-bar > span").count()) >= 3, await panel.locator(".mac-bar").innerHTML());
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth);
      check(`${tag}: the page has no sideways scroll`, overflow <= 0, `${overflow}px too wide`);
      if (shots) await panel.screenshot({ path: path.join(shots, `panel-${tag}.png`) });

      if (tag === "1440-light") {
        const figma = panel.locator(".mac-labels li", { hasText: "Figma" }).locator(".mac-category");
        check(`${tag}: a new app starts unsorted`, (await figma.textContent()) === "Unsorted", await figma.textContent());
        await figma.click();
        // The panel redraws once Cockpit has stored the press and answered the day again.
        await page.waitForFunction(() => [...document.querySelectorAll("#time .mac-labels li")]
          .some((row) => row.textContent.includes("Figma") && row.querySelector(".mac-category").textContent === "Work"), null, { timeout: 10e3 }).catch(() => {});
        const moved = await panel.locator(".mac-labels li", { hasText: "Figma" }).locator(".mac-category").textContent();
        const stored = (await user("GET", "/api/activity/day")).labels.find((row) => row.label === "Figma");
        check(`${tag}: pressing it makes it work, and Cockpit keeps that`, moved === "Work" && stored.category === "work", `${moved} / ${JSON.stringify(stored)}`);
        await panel.getByRole("button", { name: "Pause" }).click();
        await panel.getByRole("button", { name: "Resume" }).waitFor();
        const paused = (await user("GET", "/api/activity/now")).mac.paused;
        check(`${tag}: Pause pauses the Mac, then offers Resume`, paused === true, `paused ${paused}`);
        await panel.getByRole("button", { name: "Resume" }).click();
        await panel.getByRole("button", { name: "Pause" }).waitFor();
      }

      await page.locator("#next .step-menu > summary").first().click();
      const note = page.locator("#next .step-menu .activity-note");
      await note.waitFor({ state: "visible" });
      const words = await note.textContent();
      check(`${tag}: the step's menu says where its time went on the Mac`, words === "On your Mac: 30m Cursor, 10m github.com, 4m Slack, 2m youtube.com, 1m other, 3m away", words);
      // The page redraws the card when its state poll returns, which can swap the menu out mid-screenshot.
      for (let tries = 3; shots && tries > 0; tries -= 1) {
        const shot = await page.locator("#next .step-menu .menu-list").first().screenshot({ path: path.join(shots, `menu-${tag}.png`) }).then(() => true, (error) => error);
        if (shot === true) break;
        if (tries === 1) throw shot;
      }

      // On the page you use, /, "Your time" opens over the card in the center layer: from the step's "Open Your time"
      // link (#time, as when it is pressed on the page already open) and from the logo menu.
      await page.goto(`${info.origin}/`, { timeout: 120e3 });
      await page.locator("#next").waitFor({ state: "visible" });
      // A step's link chip opens other links in a new tab; this one opens the view on the page already open. The chip
      // is added and pressed at once, since a live update may redraw the card in between.
      const pagesBefore = page.context().pages().length;
      await page.evaluate((origin) => {
        window.samePage = true;
        const chip = Object.assign(document.createElement("a"), { href: `${origin}/#time`, target: "_blank", className: "link-chip", textContent: "Open Your time" });
        document.querySelector("#next .next-card").prepend(chip);
        chip.click();
      }, info.origin);
      const layer = page.locator(".center-layer:not([hidden])");
      await layer.locator(".center-time .mac-labels li").first().waitFor({ state: "visible" });
      check(`${tag}: the step's link opens Your time in the center layer, on the same page`, page.url() === `${info.origin}/`
        && (await layer.locator(".center-title").textContent()) === "Your time"
        && await page.evaluate(() => window.samePage === true) && page.context().pages().length === pagesBefore, page.url());
      const layerOverflow = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth);
      check(`${tag}: the layer has no sideways scroll`, layerOverflow <= 0, `${layerOverflow}px too wide`);
      if (shots) await page.screenshot({ path: path.join(shots, `center-${tag}.png`) });
      await layer.locator(".center-close").click();
      await page.locator(".center-layer").waitFor({ state: "hidden" });
      // On a phone the logo menu sits on the list screen, which the bar's "Your items" opens.
      if (width <= 1000) await page.locator(".phone-back").click();
      await page.locator(".brand-menu > summary").click();
      await page.locator('.brand-menu a[href="#time"]').click();
      await layer.locator(".center-time .mac-labels li").first().waitFor({ state: "visible" });
      check(`${tag}: the logo menu's "Your time" opens it too`, true, "");

      check(`${tag}: no page errors`, errors.length === 0, errors.join(" | "));
      await context.close();
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
