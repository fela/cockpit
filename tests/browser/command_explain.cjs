// Run with PLAYWRIGHT_MODULE=/path/to/node_modules/playwright node tests/browser/command_explain.cjs.
// Uses the keyboard fixture's isolated database; never touches live work or real agents.
//
// NEXT-40:
// - a step's command shows what it does, in the session's words, above the command;
// - the command shows one part per line, its words coloured (PERM-22), and Copy copies the command as put;
// NEXT-44: the Run block comes right under the ask and
// why, above the step's body and details.
// - at desktop and phone width, in light and dark, nothing is wider than the window and the page logs no error.
// Saves a screenshot of the Run block per width and look to the work folder it prints.
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");
const { spawn } = require("node:child_process");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(__dirname, "../..");

// A command with its explanation on a step
const RUN = "mkdir -p /tmp/tasks-preview && python -m http.server 15432 --bind 127.0.0.1 --directory /tmp/tasks-preview"
  + " > /tmp/tasks-preview.log 2>&1 &\n"
  + "sleep 2 && curl -sS -o /dev/null -w 'Preview answered HTTP %{http_code}\\n' http://127.0.0.1:15432/";
const EXPLAIN = "It starts a local preview for a Tasks export and checks that the page answers.\n\n"
  + "1. `mkdir` creates a temporary folder for the preview files.\n"
  + "2. `python -m http.server` serves that folder on loopback and writes its output to a temporary log.\n"
  + "3. `sleep` and `curl` wait briefly and check the preview page.";
const work = workFolder("cockpit-command-explain-");
const python = path.join(root, ".venv/bin/python");
const server = spawn(python, [path.join(__dirname, "serve_keyboard.py"), work], {
  cwd: root, stdio: ["ignore", "pipe", "pipe"],
});
let serverLog = "";
server.stdout.on("data", (data) => { serverLog += data; });
server.stderr.on("data", (data) => { serverLog += data; });
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
// The fixture's session (tests/conftest.py) and agent token.
const SESSION_A = "11111111-1111-4111-8111-111111111111";
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "command-explain-test", "content-type": "application/json" };
let browser;

(async () => {
  const runtime = path.join(work, "runtime.json");
  for (let at = 0; !fs.existsSync(runtime); at++) {
    if (at > 1200 || server.exitCode !== null) throw new Error(`Fixture failed: ${serverLog}`);
    await delay(100);
  }
  const { origin, cookie } = JSON.parse(fs.readFileSync(runtime, "utf8"));
  for (let at = 0; ; at++) {
    try { if ((await fetch(origin + "/healthz")).ok) break; } catch {}
    if (at > 600) throw new Error(`Fixture did not listen: ${serverLog}`);
    await delay(100);
  }
  const send = async (method, url, body) => {
    const response = await fetch(origin + url, { method, headers: AGENT, body: JSON.stringify(body) });
    assert.equal(response.ok, true, await response.clone().text());
    return response.json();
  };
  await send("PUT", "/api/items", { key: "note:command-explain", title: "Preview a Tasks export", sessions: [SESSION_A] });
  // Without its explanation the put is refused.
  const bare = await fetch(origin + "/api/steps", { method: "POST", headers: AGENT,
    body: JSON.stringify({ item_key: "note:command-explain", need: "run", ask: "Press Run", command: { run: RUN, cwd: work } }) });
  assert.equal(bare.status, 400);
  const { step } = await send("POST", "/api/steps", { item_key: "note:command-explain", need: "run", session_id: SESSION_A,
    ask: "Press Run to start the local export preview", why: "The export layout is ready to view in a browser.", score: 100,
    body: "The Tasks CSV export has a new column order. The local preview shows the sample export.",
    details: [{ title: "The situation from scratch", body: "Tasks exports lists as CSV files. The sample file is served from a temporary folder." }],
    command: { run: RUN, cwd: work, explain: EXPLAIN } });

  const executablePath = process.env.BROWSER_EXECUTABLE;
  browser = await chromium.launch({ headless: true, ...(executablePath ? { executablePath } : {}) });
  // The sidebar layout (/) and the classic one; the step is the only one, so it is Next up in both.
  for (const view of ["/", "/classic"]) for (const [width, height] of [[1280, 900], [390, 844]]) {
    for (const scheme of ["light", "dark"]) {
      const context = await browser.newContext({ extraHTTPHeaders: { Cookie: cookie }, viewport: { width, height }, colorScheme: scheme });
      await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin });
      const page = await context.newPage();
      page.setDefaultTimeout(8000);
      page.setDefaultNavigationTimeout(60000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
      await page.goto(origin + view);
      const card = page.locator(`#next [data-step="${step.id}"]`);
      const block = card.locator(".step-run");
      await block.waitFor();
      // The explanation comes first, as Markdown: its list and code words render.
      const explain = block.locator(".step-run-explain");
      assert.equal(await explain.locator("li").count(), 3);
      assert.equal(await explain.locator("code").first().textContent(), "mkdir");
      const order = await block.evaluate((el) => [...el.children].map((child) => child.className));
      assert.ok(order.indexOf("prose step-run-explain") < order.indexOf("step-run-text"), JSON.stringify(order));
      // The Run block sits above the body and details (NEXT-44).
      const above = await card.evaluate((card) => {
        const run = card.querySelector(".step-run");
        const follows = (a, b) => Boolean(a && b && (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING));
        return { body: follows(run, card.querySelector(".prose.body")),
          details: follows(run, card.querySelector(".details")) };
      });
      assert.deepEqual(above, { body: true, details: true });
      // The command one part per line, coloured.
      const text = await block.locator("pre.step-run-text").textContent();
      assert.ok(text.split("\n").length >= 4, text);
      assert.equal(text.replace(/\s+/g, " "), RUN.replace(/\s+/g, " "));
      assert.equal(await block.locator("pre .sh-command").first().textContent(), "mkdir");
      // Copy copies the command as the session put it.
      await block.getByRole("button", { name: "Copy command" }).click();
      assert.equal(await page.evaluate(() => navigator.clipboard.readText()), RUN);
      const fits = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
      assert.equal(fits, true, `${width}: the page is wider than the window`);
      // The card of the whole step: the Run block at its top. Copy's feedback can redraw the card mid-shot, so a
      // detached element gets one more try.
      const shot = { path: path.join(work, `run${view.replace("/", "-")}-${width}-${scheme}.png`) };
      await card.screenshot(shot).catch(() => card.screenshot(shot));
      assert.deepEqual(errors, []);
      console.log(`PASS ${view} ${width} ${scheme}: the Run block leads the card; the explanation shows above the command, laid out one part per line`);
      await context.close();
    }
  }
  console.log(`Screenshots in ${work}`);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
}).finally(async () => {
  await browser?.close();
  server.kill();
});
