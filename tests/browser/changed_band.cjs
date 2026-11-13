// The changed band on a step card (SPEC NEXT-61). An agent puts a step, the user's timer runs on it once for five
// minutes, ending two hours ago (the user read it), and the agent rewrites it with a `changed` note twelve minutes
// ago. Checks, at desktop light, desktop dark and phone width:
// - the band's title reads "Changed 12 min ago";
// - under it, in grey, "You read the earlier version for 5 min, until 2 h ago.";
// - under it, the rewrite's note;
// - at phone width nothing scrolls sideways, and the page logs no errors.
//
// Starts serve_preview.py (this checkout's Cockpit on a fixture database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed. Screenshots of the card's top
// go to the folder given as the first argument, when one is given.
//
// Usage, from the checkout: node tests/browser/changed_band.cjs [screenshot folder]

"use strict";

const { spawn, spawnSync } = require("node:child_process");
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
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "changed-band-test", "content-type": "application/json" };
const TITLE = "Changed band test";
const ASK = "Build failure time found; check the answer";
const NOTE = "The answer now says the build failed at 16:00";
const LOOKS = [
  { name: "desktop-light", width: 1440, height: 900, colorScheme: "light" },
  { name: "desktop-dark", width: 1440, height: 900, colorScheme: "dark" },
  { name: "phone", width: 390, height: 844, colorScheme: "light", phone: true },
];
// The part of the card each screenshot keeps, from its top.
const SHOT_HEIGHT = 360;

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const shots = process.argv[2] || null;
  if (shots) fs.mkdirSync(shots, { recursive: true });
  const work = workFolder("cockpit-changed-band-");
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

    // The fixture database's open steps are snoozed and its timer paused, so this run's step is the one on the page.
    const initial = await user("GET", "/api/state");
    if (initial.timer) await user("POST", `/api/steps/${initial.timer.step_id}/timer/pause`);
    await Promise.all([...initial.queue, ...initial.ready_next].map((step) => user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));

    const key = `note:changed-band-${Date.now()}`;
    const item = await agent("PUT", "/api/items", { key, title: TITLE, priority: "urgent", sessions: [SESSION_B] });
    const step = { item_key: key, need: "decide:changed-band", session_id: SESSION_B, score: 100, ask: ASK };
    const put = await agent("POST", "/api/steps", { ...step, body: "The build failed some time after the merge." });
    const id = put.step.id;
    // The user read it: their timer ran on the step once, which leaves a time_spans row, moved back to five minutes
    // ending two hours ago, since a shorter look is a glance and shows no band.
    await user("POST", `/api/steps/${id}/timer/start`);
    await user("POST", `/api/steps/${id}/timer/pause`);
    sql(work, "UPDATE time_spans SET started_at = ?, ended_at = ? WHERE step_id = ?", stamp(7500e3), stamp(7200e3), id);
    const rewrite = await agent("POST", "/api/steps", { ...step, body: "The build failed at 16:00.", changed: NOTE });
    check("the rewrite went through without waiting", rewrite.step.id === id && !rewrite.held, JSON.stringify(rewrite).slice(0, 200));
    // The rewrite came twelve minutes ago.
    sql(work, "UPDATE step_changes SET at = ? WHERE step_id = ?", stamp(720e3), id);

    const state = await user("GET", "/api/state");
    const full = (state.items || []).find((entry) => entry.id === item.id);
    const changes = full ? full.changes.filter((change) => change.step_id === id) : [];
    check("the server recorded one change with the note", changes.length === 1 && changes[0].note === NOTE, JSON.stringify(changes));

    browser = await chromium.launch({ executablePath: CHROMIUM });
    for (const look of LOOKS) {
      const context = await browser.newContext({ viewport: { width: look.width, height: look.height }, colorScheme: look.colorScheme,
        isMobile: !!look.phone, hasTouch: !!look.phone });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      page.setDefaultTimeout(30000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
      await page.goto(`${info.origin}/classic`);
      // The Next up card; its row in the queue below carries the same data-step.
      const card = page.locator(`article.next-card[data-step="${id}"]`);
      const band = card.locator(".changed-banner");
      await band.waitFor();

      const title = (await band.locator(".changed-title").innerText()).trim();
      // The looks run one after another, so the check accepts a rewrite 12 to 19 min old.
      check(`${look.name}: the band reads "Changed 12 min ago"`, /^Changed 1[2-9] min ago$/.test(title), title);
      const why = (await band.locator(".changed-why").innerText()).trim();
      const expected = "You read the earlier version for 5\u00a0min, until 2 h ago.";
      check(`${look.name}: under it, how long and until when the user read the earlier version`, why === expected, `${why} (expected ${expected})`);
      const colours = await band.evaluate((node) => [".changed-title", ".changed-why"].map((part) => getComputedStyle(node.querySelector(part)).color));
      check(`${look.name}: the reading line is in a different colour from the title`, colours[0] !== colours[1], colours.join(" vs "));
      const notes = (await band.locator(".changed-note").allInnerTexts()).map((text) => text.trim());
      check(`${look.name}: the note line shows under it`, notes.length === 1 && notes[0] === NOTE, JSON.stringify(notes));

      if (look.phone) {
        await sleep(300);
        const sideways = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
        check(`${look.name}: at ${look.width} px nothing scrolls sideways`, sideways <= 0, `${sideways}px wider`);
      }
      if (shots) {
        // The card's place on the whole page, so a sticky header cannot cover its top.
        const box = await card.evaluate((node) => {
          const rect = node.getBoundingClientRect();
          return { x: rect.left + window.scrollX, y: rect.top + window.scrollY, width: rect.width, height: rect.height };
        });
        await page.screenshot({ path: path.join(shots, `changed-band-${look.name}.png`), fullPage: true,
          clip: { x: box.x, y: box.y, width: box.width, height: Math.min(box.height, SHOT_HEIGHT) } });
      }
      check(`${look.name}: no page errors`, errors.length === 0, errors.join(" | "));
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

// A UTC time `ms` ago, to the second, as the server writes it.
function stamp(ms) {
  return new Date(Date.now() - ms).toISOString().replace(/\.\d+Z$/, "Z");
}

// Runs one SQL statement with text parameters on the fixture database.
function sql(work, statement, ...params) {
  const script = "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); c.execute(sys.argv[2], sys.argv[3:]); c.commit()";
  const result = spawnSync(PYTHON, ["-c", script, path.join(work, "cockpit.db"), statement, ...params.map(String)]);
  if (result.status !== 0) throw new Error(`${statement}: ${result.stderr}`);
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
  // The wait covers building the fixture database on a busy machine.
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
  throw new Error("the harness did not come up within 180 s");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
