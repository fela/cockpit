// A message the user sends shows in the task's history at once, also while its session is busy and the message
// waits to reach it.
// On the first step card of Next up, checks:
// - a message that waits for its session shows as the user's latest message in the card's fold, within seconds,
//   with no reload;
// - the full history shows it as the current exchange, with "Not with the session yet" as its answer;
// - once it reaches the session it still shows, now without that line.
// Saves a screenshot of the opened history in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/unsent_message.cjs

"use strict";

const { spawn, execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;
const PYTHON = path.join(__dirname, "..", "..", ".venv", "bin", "python");
const TEXT = "Merge all current changes into main, please";

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Runs SQL on the server's synthetic database.
function sql(work, statement, params) {
  const script = "import json, sqlite3, sys; c = sqlite3.connect(sys.argv[1]); c.execute(sys.argv[2], json.loads(sys.argv[3])); c.commit()";
  execFileSync(PYTHON, ["-c", script, path.join(work, "cockpit.db"), statement, JSON.stringify(params)]);
}

async function main() {
  const work = workFolder("cockpit-unsent-message-");
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const info = await harnessUp(work, server);
    const cookie = `${info.name}=${info.value}`;
    const state = await fetch(`${info.origin}/api/state`, { headers: { cookie } }).then((response) => response.json());
    const step = state.queue[0];
    if (!step) throw new Error("no step in the copy's queue");

    browser = await chromium.launch({ executablePath: CHROMIUM });
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, colorScheme: "light" });
    await context.setExtraHTTPHeaders({ cookie });
    const page = await context.newPage();
    await page.addLocatorHandler(page.locator("dialog#idle[open]"), (dialog) => dialog.locator("button", { hasText: "Return" }).click());
    await page.goto(`${info.origin}/`);
    const card = page.locator(`.hist-fold-card[data-item="${step.item_id}"]`).first();
    await card.waitFor({ timeout: 30e3 });

    const now = new Date().toISOString().replace(/\.\d+Z$/, "Z");
    // Waits for the working fake session's turn to end, as a message to a busy session does.
    sql(work, "INSERT INTO messages (author, item_id, session_id, step_id, text, state, created_at, waits_since)"
      + " VALUES ('user', ?, '22222222-2222-4222-8222-222222222222', ?, ?, 'queued', ?, ?)", [step.item_id, step.id, TEXT, now, now]);
    const started = Date.now();
    const said = card.locator(".fold-said-text", { hasText: TEXT });
    const shown = await said.waitFor({ timeout: 20e3 }).then(() => true, () => false);
    check("the waiting message shows as the latest one in the fold, with no reload", shown,
      `fold shows: ${await card.locator(".fold-said").textContent().catch(() => "nothing")}`);
    if (shown) console.error(`    (after ${((Date.now() - started) / 1000).toFixed(1)} s)`);

    await card.locator(".fold-all > summary").first().click();
    const current = card.locator(".hist-entry.current").first();
    await current.waitFor({ timeout: 5e3 });
    const currentText = await current.textContent();
    check("the full history's current exchange is the waiting message", currentText.includes(TEXT), currentText);
    check("its answer says it has not reached the session", currentText.includes("Not with the session yet"), currentText);
    await card.screenshot({ path: path.join(work, "unsent-message.png") });

    const sent = new Date().toISOString().replace(/\.\d+Z$/, "Z");
    sql(work, "UPDATE messages SET state = 'sent', sent_at = ?, waits_since = NULL WHERE text = ? AND state = 'queued'", [sent, TEXT]);
    const delivered = await page.waitForFunction(({ item, text }) => {
      const entry = document.querySelector(`.hist-fold-card[data-item="${item}"] .hist-entry.current`);
      return entry && entry.textContent.includes(text) && !entry.textContent.includes("Not with the session yet");
    }, { item: step.item_id, text: TEXT }, { timeout: 30e3 }).then(() => true, () => false);
    check("once delivered it stays, without the not-delivered line", delivered,
      await current.textContent().catch(() => "nothing"));
  } finally {
    if (browser) await browser.close();
    server.kill("SIGTERM");
  }
  const failed = results.filter((result) => !result.ok);
  console.error(`${results.length - failed.length}/${results.length} checks passed`);
  if (failed.length) process.exitCode = 1;
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
