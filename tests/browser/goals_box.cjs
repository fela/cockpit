// Priorities as one line above Search (SPEC UI-65). Checks, on a copy of the live database:
// - in the rail, Priorities sit above Search;
// - with no priorities the line reads "Priorities [empty]";
// - a click opens the editor as a modal; Save writes one priority per line, in order, and the line shows them in that
//   order, with every entry in its hover, each clipped to 60 characters;
// - the line stays one row whatever the list's length, and shows the summary the server gives, not the list's start;
// - an edit that moves a line keeps that priority's id; Cancel and Escape change nothing, and the focus goes back to
//   the box;
// - G P opens the editor;
// - at phone width the box shows without sideways scroll;
// - no page errors.
// Saves a screenshot per width in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a copy of the live database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/goals_box.cjs
"use strict";

const { execFileSync, spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

// Browser dependencies can be selected through the environment (README.md).
const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-goals-box-");
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
    const headers = { cookie: `${info.name}=${info.value}`, origin: info.origin, "content-type": "application/json" };
    const call = async (method, url, body) => {
      const response = await fetch(`${info.origin}${url}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
      if (!response.ok) throw new Error(`${method} ${url}: HTTP ${response.status} ${await response.text()}`);
      return response.json();
    };
    const goals = async () => (await call("GET", "/api/goals")).map((goal) => goal.text);
    await call("PUT", "/api/goals", { texts: [] });

    browser = await chromium.launch({ executablePath: CHROMIUM });
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    await context.setExtraHTTPHeaders({ cookie: headers.cookie });
    const page = await context.newPage();
    page.setDefaultTimeout(45000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const boxText = () => page.locator("#goals .goals-text").innerText();
    const dialogOpen = () => page.evaluate(() => document.getElementById("goals-dialog").open);

    await page.goto(`${info.origin}/`);
    await page.locator("#goals .goals-box").waitFor();
    // The fixture may hold the inactivity question, a modal that takes every click. The page asks
    // it on a one-second timer, so the check waits past a few of its rounds; Return answers it.
    await sleep(3000);
    const idle = page.locator("dialog#idle[open]");
    if (await idle.count()) {
      await idle.locator("button", { hasText: "Return" }).click();
      await page.waitForSelector("dialog#idle:not([open])", { state: "attached" });
    }
    const order = await page.evaluate(() => {
      const rail = document.querySelector(".rail");
      const goals = rail.querySelector("#goals");
      const search = rail.querySelector(".rail-search");
      return Boolean(goals && search && goals.compareDocumentPosition(search) & Node.DOCUMENT_POSITION_FOLLOWING);
    });
    check("in the rail, Priorities sit above Search", order, "Search comes first");
    const emptyLine = (await page.locator("#goals .goals-box").innerText()).replace(/\s+/g, " ").trim();
    check("with no priorities the line reads Priorities [empty]", emptyLine === "Priorities [empty]", emptyLine);

    await page.click("#goals .goals-box");
    check("a click opens the editor as a modal", await dialogOpen(), "no dialog");
    await page.locator("#goals-dialog textarea").fill("Service first\n\n  Ship A  \nReviews");
    await page.screenshot({ path: path.join(work, "goals-editor-1280.png") });
    await page.click("#goals-dialog button[type=submit]");
    const shown = await waitFor(boxText, (text) => text.includes("Reviews"));
    const saved = await waitFor(goals, (texts) => texts.length === 3);
    check("Save writes one priority per line, in order, and the line shows them",
      !(await dialogOpen()) && JSON.stringify(saved) === JSON.stringify(["Service first", "Ship A", "Reviews"]) && shown === "Service first; Ship A; Reviews",
      `saved ${JSON.stringify(saved)}, shown ${JSON.stringify(shown)}`);
    const measure = () => page.evaluate(() => {
      const box = document.querySelector("#goals .goals-box");
      const text = box.querySelector(".goals-line");
      return { height: Math.round(box.getBoundingClientRect().height), cut: text.scrollWidth > text.clientWidth, title: box.dataset.tip || box.title };
    });
    const fits = await measure();
    check("a list that is short for the row fits it whole", !fits.cut && fits.height <= 32, JSON.stringify(fits));
    const longText = "make weekly reminders easier to find";
    await call("PUT", "/api/goals", { texts: [longText] });
    await waitFor(boxText, (text) => text === longText);
    const waiting = await measure();
    check("until its summary is written, a longer priority stays one row, cut off", waiting.cut && waiting.height <= 32, JSON.stringify(waiting));
    writeSummary(path.join(work, "cockpit.db"), longText, "weekly reminders");
    await waitFor(boxText, (text) => text === "weekly reminders");
    const summarised = await measure();
    check("a longer priority shows its summary on the one row, with its own words in the hover",
      (await boxText()) === "weekly reminders" && !summarised.cut && summarised.height <= 32 && summarised.title === `1. ${longText}`,
      JSON.stringify(summarised));
    await page.screenshot({ path: path.join(work, "goals-box-summary-1280.png") });
    await call("PUT", "/api/goals", { texts: ["Service first", "Ship A", "Reviews", "A long fourth priority that cannot fit on the line beside the others",
      "A fifth priority, also long enough that the list runs past three lines of the rail"] });
    await waitFor(boxText, (text) => text.includes("fifth"));
    const line = await measure();
    const hover = "1. Service first\n2. Ship A\n3. Reviews\n4. A long fourth priority that cannot fit on the line beside t…\n5. A fifth priority, also long enough that the list runs past …";
    check("a long list with no summary yet stays one row, cut off, with all five entries clipped to 60 characters in its hover",
      line.cut && line.height <= 32 && line.title === hover,
      JSON.stringify(line));
    await call("PUT", "/api/goals", { texts: ["Service first", "Ship A", "Reviews"] });
    await waitFor(boxText, (text) => !text.includes("fifth"));
    await page.screenshot({ path: path.join(work, "goals-box-1280.png") });

    const before = await call("GET", "/api/goals");
    await page.click("#goals .goals-box");
    await page.locator("#goals-dialog textarea").fill("Reviews\nService first\nShip A");
    await page.keyboard.press("Control+Enter");
    const moved = await waitFor(() => call("GET", "/api/goals"), (list) => list[0]?.text === "Reviews");
    const reviews = before.find((goal) => goal.text === "Reviews");
    check("Ctrl+Enter saves, and a moved line keeps its priority", moved[0].id === reviews.id && !(await dialogOpen()), JSON.stringify(moved));

    await page.click("#goals .goals-box");
    await page.locator("#goals-dialog textarea").fill("Something else");
    await page.click("#goals-dialog .button.ghost");
    await page.click("#goals .goals-box");
    const reopenedText = await page.locator("#goals-dialog textarea").inputValue();
    const savedTexts = ["Reviews", "Service first", "Ship A"];
    check("Cancel changes nothing, and the editor reopens on the saved list",
      JSON.stringify(await goals()) === JSON.stringify(savedTexts) && reopenedText === savedTexts.join("\n"),
      `${JSON.stringify(await goals())} ${JSON.stringify(reopenedText)}`);
    await page.locator("#goals-dialog textarea").fill("An unsaved reminder priority");
    await page.keyboard.press("Escape");
    await sleep(300);
    const afterEscape = await goals();
    check("Escape discards the unsaved edit and keeps the saved list",
      JSON.stringify(afterEscape) === JSON.stringify(savedTexts) && !(await dialogOpen()), JSON.stringify(afterEscape));
    const focused = await page.evaluate(() => document.activeElement?.matches("#goals .goals-box") ?? false);
    check("closing the editor puts the focus back on the box", focused, "focus elsewhere");
    await page.click("#goals .goals-box");
    const reopenedAfterEscape = await page.locator("#goals-dialog textarea").inputValue();
    check("after Escape the editor reopens on the saved list", reopenedAfterEscape === savedTexts.join("\n"), JSON.stringify(reopenedAfterEscape));
    await page.click("#goals-dialog .button.ghost");

    await page.locator(".card, main").first().click({ position: { x: 5, y: 5 } }).catch(() => {});
    await page.keyboard.press("g");
    await page.keyboard.press("p");
    const jumped = await waitFor(dialogOpen, (open) => open);
    check("G P opens the editor", jumped, "no dialog");
    await page.keyboard.press("Escape");

    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`${info.origin}/`);
    await page.locator("#goals .goals-box").waitFor({ state: "attached" });
    // The list of items is the phone's other screen, where the rail shows.
    await page.evaluate(() => document.body.classList.add("phone-list"));
    await sleep(300);
    const phone = await page.evaluate(() => ({
      sideways: document.documentElement.scrollWidth > document.documentElement.clientWidth,
      width: Math.round(document.querySelector("#goals .goals-box").getBoundingClientRect().width),
    }));
    await page.screenshot({ path: path.join(work, "goals-box-390.png") });
    check("at phone width the line shows without sideways scroll", !phone.sideways && phone.width > 200, JSON.stringify(phone));

    await call("PUT", "/api/goals", { texts: [] });
    check("no page errors", errors.length === 0, errors.join(" | "));
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  console.error(`goals_box: ${results.filter((result) => result.ok).length}/${results.length} passed; screenshots in ${work}`);
  if (results.some((result) => !result.ok)) process.exitCode = 1;
}

// What history.summarize_next stores once Claude has written the label; the harness runs no Claude.
function writeSummary(database, listed, summary) {
  const script = "import hashlib, sys; import sqlite3; c = sqlite3.connect(sys.argv[1]); "
    + "ref = 'goals:' + hashlib.sha256(sys.argv[2].encode()).hexdigest()[:16]; "
    + "c.execute('INSERT OR REPLACE INTO summaries (ref, at, text, asked_at, summary, made_at) VALUES (?, ?, ?, ?, ?, ?)', (ref, '2026-01-15T10:00:00Z', sys.argv[2], '2026-01-15T10:00:00Z', sys.argv[3], '2026-01-15T10:00:00Z')); c.commit()";
  execFileSync(path.join(__dirname, "..", "..", ".venv", "bin", "python"), ["-c", script, database, listed, summary]);
}

async function waitFor(read, ok) {
  const end = Date.now() + 45000;
  let value = await read();
  while (!ok(value) && Date.now() < end) {
    await sleep(100);
    value = await read();
  }
  return value;
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
