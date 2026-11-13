// Run with PLAYWRIGHT_MODULE=/path/to/node_modules/playwright node tests/browser/allow_final.cjs. Uses the keyboard
// fixture's isolated database; never touches live work or real agents.  NEXT-41.
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");
const { spawn } = require("node:child_process");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(__dirname, "../..");

const RULE = "Bash(ls /tmp/tasks-export *)";
const work = workFolder("cockpit-allow-final-");
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
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "allow-final-test", "content-type": "application/json" };
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
  await send("PUT", "/api/items", { key: "note:allow-final", title: "Read sample export files", sessions: [SESSION_A] });
  const { step } = await send("POST", "/api/steps", { item_key: "note:allow-final", need: "allow", session_id: SESSION_A,
    ask: "Let the session list sample exports", score: 100, permission: { list: "allow", rule: RULE } });

  const executablePath = process.env.BROWSER_EXECUTABLE;
  browser = await chromium.launch({ headless: true, ...(executablePath ? { executablePath } : {}) });
  let pressed = false;
  for (const [width, height] of [[1280, 900], [390, 844]]) {
    for (const scheme of ["light", "dark"]) {
      const context = await browser.newContext({ extraHTTPHeaders: { Cookie: cookie }, viewport: { width, height }, colorScheme: scheme });
      const page = await context.newPage();
      page.setDefaultTimeout(8000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
      await page.goto(origin + "/classic");
      const block = page.locator(`[data-step="${step.id}"] [aria-label="Permission"]`).first();
      await block.waitFor();
      if (!pressed) {
        // The first look presses Allow; the session then returns the step.
        await block.getByRole("button", { name: "Allow" }).click();
        for (let at = 0; ; at++) {
          const state = await (await fetch(origin + "/api/state", { headers: { Cookie: cookie } })).json();
          if (state.with_agents.some((entry) => entry.id === step.id)) break;
          if (at > 100) throw new Error("Allow did not hand the step to its session");
          await delay(100);
        }
        await send("POST", `/api/steps/${step.id}/return`, { note: "Done: the rule is in" });
        pressed = true;
        await page.reload();
        await block.waitFor();
      }
      await block.getByText("You allowed this").waitFor();
      assert.equal(await block.getByRole("button", { name: "Allow" }).count(), 0, "a returned step offers no second Allow");
      assert.equal(await block.locator(".step-run-text").textContent(), RULE);
      const fits = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
      assert.equal(fits, true, `${width}: the page is wider than the window`);
      await block.screenshot({ path: path.join(work, `allowed-${width}-${scheme}.png`) });
      assert.deepEqual(errors, []);
      console.log(`PASS ${width} ${scheme}: the returned step says the user allowed it, with no Allow button`);
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
