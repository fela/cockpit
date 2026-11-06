// Run with node tests/browser/dialog_card.cjs (PLAYWRIGHT_MODULE and BROWSER_EXECUTABLE choose the browser).
// A synthetic database through serve_slow.py; nothing reaches a real session.
//
// A dialog that never reached Cockpit gets a card headed in words, with its command apart, at desktop and
// phone width: not one headed with Claude Code's whole "approve Bash: ..." line.
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");
const { spawn } = require("node:child_process");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(__dirname, "../..");
const work = workFolder("cockpit-dialog-card-");
const COMMAND = 'cd /tmp/tasks-preview; mkdir -p exports; printf "title,completed\\nBuy bread,false\\n" > exports/tasks.csv';
fs.writeFileSync(path.join(work, "dialog.json"), JSON.stringify({ needs: `approve Bash: ${COMMAND}`, updated_at: new Date().toISOString().replace(/\.\d+Z$/, "Z") }));
const server = spawn(path.join(root, ".venv/bin/python"), [path.join(__dirname, "serve_slow.py"), work], { cwd: root, stdio: ["ignore", "pipe", "pipe"] });
let log = "";
server.stdout.on("data", (data) => { log += data; });
server.stderr.on("data", (data) => { log += data; });
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;
let browser;

(async () => {
  const runtime = path.join(work, "cookie.json");
  for (let at = 0; !fs.existsSync(runtime); at++) {
    if (at > 3000 || server.exitCode !== null) throw new Error(`Server failed: ${log}`);
    await delay(100);
  }
  const { origin, name, value } = JSON.parse(fs.readFileSync(runtime, "utf8"));
  for (let at = 0; ; at++) {
    try { if ((await fetch(origin + "/healthz")).ok) break; } catch {}
    if (at > 600) throw new Error(`Server did not listen: ${log}`);
    await delay(100);
  }
  const executablePath = process.env.BROWSER_EXECUTABLE || (fs.existsSync(CHROMIUM) ? CHROMIUM : undefined);
  browser = await chromium.launch({ headless: true, ...(executablePath ? { executablePath } : {}) });
  for (const [width, height] of [[1280, 900], [390, 844]]) {
    const context = await browser.newContext({ extraHTTPHeaders: { Cookie: `${name}=${value}` }, viewport: { width, height } });
    const page = await context.newPage();
    page.setDefaultTimeout(60000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(origin + "/");
    const row = page.locator("#sidebar li.warn-row").filter({ hasText: "asks to run a command" }).first();
    await page.waitForFunction(() => document.querySelector("#sidebar li.warn-row") !== null, null, { timeout: 120000 });
    if (width < 600) await page.evaluate(() => setPhoneScreen(true, false));
    await row.click();
    const card = page.locator(".waiting-card").first();
    await card.waitFor();
    assert.equal((await card.locator(".what").textContent()).trim(), "The session asks to run a command");
    assert.match((await card.locator("pre.call").textContent()).replace(/\s+/g, " "), /printf "title,completed/);
    assert.equal(await page.locator("#sidebar").getByText("approve Bash").count(), 0, `${width}: the raw line shows in the row`);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, `${width}: wider than the window`);
    await card.screenshot({ path: path.join(process.env.SCREENSHOTS || work, `dialog-card-${width}.png`) });
    assert.deepEqual(errors, []);
    console.log(`PASS ${width}`);
    await context.close();
  }
  console.log(work);
})().catch((error) => { console.error(error); process.exitCode = 1; }).finally(async () => {
  if (browser) await browser.close();
  server.kill();
});
