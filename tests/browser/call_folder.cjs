// Run with PLAYWRIGHT_MODULE=/path/to/node_modules/playwright node tests/browser/call_folder.cjs.
// Uses the keyboard fixture's isolated database; never touches live work or real agents.
//
// PERM-26: at desktop and phone width, in light and dark,
// - a blocked call's card says under the call which folder it runs in, the home folder shown as `~`, the whole
//   path in its tooltip;
// - a folder outside the user's home folder, such as another user's, shows whole;
// - nothing is wider than the window, and the page logs no error.
// Saves a screenshot of the card per width and look to the work folder it prints.
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");
const { spawn } = require("node:child_process");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(__dirname, "../..");
const work = workFolder("cockpit-call-folder-");
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
// A second session, so its call is not one that SESSION_A moved past (PERM-18).
const SESSION_B = "22222222-2222-4222-8222-222222222222";
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "permission-hook", "content-type": "application/json" };
// The fixture server runs as this user, so its home folder is this one.
const WORKTREE = path.join(require("node:os").homedir(), "dev/app/.claude/worktrees/reminders");
// Another user's home folder shows whole, not as `~`.
const OTHER = "/home/alex/dev/app";
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
  const ask = async (session_id, cwd, command) => {
    const response = await fetch(origin + "/api/permissions/requests", {
      method: "POST", headers: AGENT,
      body: JSON.stringify({ kind: "asked", session_id, cwd, tool_name: "Bash",
        tool_input: { command, description: "Show the Tasks list files" }, tool_use_id: "", reason: "" }),
    });
    assert.equal(response.ok, true, await response.clone().text());
    return (await response.json()).id;
  };
  // The newer call shows as the open card on top; the older one as a line that opens on a press.
  const outside = await ask(SESSION_B, OTHER, "ls lists");
  await delay(1100);
  const inHome = await ask(SESSION_A, WORKTREE, "ls reminders");
  const executablePath = process.env.BROWSER_EXECUTABLE;
  browser = await chromium.launch({ headless: true, ...(executablePath ? { executablePath } : {}) });
  for (const [width, height] of [[1280, 900], [390, 844]]) {
    for (const scheme of ["light", "dark"]) {
      const context = await browser.newContext({ extraHTTPHeaders: { Cookie: cookie }, viewport: { width, height }, colorScheme: scheme });
      const page = await context.newPage();
      page.setDefaultTimeout(8000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
      await page.goto(origin + "/classic");
      const card = page.locator(`#blocked .blocked-card[data-request="${inHome}"]`);
      await card.waitFor();
      const folder = card.locator(".call-folder");
      assert.equal((await folder.textContent()).trim(), "Runs in ~/dev/app/.claude/worktrees/reminders");
      // Cockpit's tooltips move a title into data-tip once shown.
      assert.equal(await folder.getAttribute("title") ?? await folder.getAttribute("data-tip"), WORKTREE);
      // The line sits right under the call, its left edge on the call's.
      const [callBox, folderBox] = [await card.locator(".call").boundingBox(), await folder.boundingBox()];
      assert.equal(Math.round(folderBox.x), Math.round(callBox.x), `${width} ${scheme}: the folder is not aligned with the call`);
      assert.ok(folderBox.y >= callBox.y + callBox.height - 1, `${width} ${scheme}: the folder is not below the call`);
      await card.screenshot({ path: path.join(work, `card-${width}-${scheme}.png`) });
      // Opening the older call's line shows its card, which names its own folder.
      await page.locator(`#blocked .blocked-line[data-request="${outside}"]`).click();
      const other = page.locator(`#blocked .blocked-card[data-request="${outside}"] .call-folder`);
      await other.waitFor();
      assert.equal((await other.textContent()).trim(), `Runs in ${OTHER}`);
      const fits = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
      assert.equal(fits, true, `${width}: the page is wider than the window`);
      assert.deepEqual(errors, []);
      console.log(`PASS ${width} ${scheme}: the card names the folder the call runs in`);
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
