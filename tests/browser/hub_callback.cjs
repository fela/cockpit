// Claude's callback is a one-use code: send once, preserve a rejected paste, and
// report Cockpit's error or HTTP status even when the response body is empty.
// Uses the hub fixture (serve_hub.py) and fake login; no provider receives a real sign-in.
"use strict";

const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");

const ROOT = path.resolve(__dirname, "..", "..");
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-hub-callback-");
  fs.writeFileSync(path.join(work, "hub.json"), JSON.stringify({ credentials: [] }));
  const server = spawn(path.join(ROOT, ".venv/bin/python"), [path.join(__dirname, "serve_hub.py"), work],
    { stdio: ["ignore", "inherit", "inherit"] });
  let browser;
  try {
    const cookie = path.join(work, "cookie.json");
    for (const end = Date.now() + 180e3; ; await pause(200)) {
      assert.equal(server.exitCode, null, "fixture exited before starting");
      if (fs.existsSync(cookie)) {
        const info = JSON.parse(fs.readFileSync(cookie, "utf8"));
        if (await fetch(`${info.origin}/healthz`).then((response) => response.ok, () => false)) break;
      }
      assert.ok(Date.now() < end, "fixture did not start");
    }
    const info = JSON.parse(fs.readFileSync(cookie, "utf8"));
    browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE || undefined });
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
    await context.route("https://claude.ai/**", (route) => route.fulfill({ body: "Fake approval page" }));
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`${info.origin}/usage`);
    await page.click(".hub-add > summary");
    await page.click(".hub-add button:has-text('Claude account')");
    const field = page.locator(".hub-address");
    await field.waitFor();

    let sends = 0;
    page.on("request", (request) => {
      if (new URL(request.url()).pathname === "/api/hub/claude/callback") sends += 1;
    });
    const submit = async (address) => {
      const before = sends;
      await field.fill(address);
      await page.click(".hub-sign-in button[type=submit]");
      await page.waitForFunction(() => !hubUi.busy);
      await pause(600);
      assert.equal(sends, before + 1, "one-use callback was sent more than once");
    };
    const banner = () => page.locator("#hub-banner").innerText();

    // A real refusal from Cockpit keeps its useful explanation and the paste.
    const wrong = "http://localhost:54545/callback?code=fake-code&state=wrong-state";
    await submit(wrong);
    assert.match(await banner(), /that address belongs to another sign-in/);
    assert.equal(await field.inputValue(), wrong);
    console.log("PASS: real callback refusal shows Cockpit's explanation and preserves the paste");

    const address = "http://localhost:54545/callback?code=fake-code&state=state-0123456789abcdefghij";
    for (const status of [502, 500, 200]) {
      const handler = (route) => route.fulfill({ status, contentType: "text/plain", body: "" });
      await page.route("**/api/hub/claude/callback", handler);
      await submit(address);
      assert.match(await banner(), new RegExp(`Cockpit answered ${status}`));
      assert.doesNotMatch(await banner(), /Unexpected end|SyntaxError/);
      assert.equal(await field.inputValue(), address);
      if (status === 502) {
        for (const width of [1280, 390]) {
          await page.setViewportSize({ width, height: 900 });
          await page.screenshot({ path: path.join(work, `callback-error-${width}.png`) });
        }
      }
      await page.unroute("**/api/hub/claude/callback", handler);
      console.log(`PASS: empty ${status} reports status, preserves paste and sends once`);
    }

    // The manual retry reaches the fake login once and clears the sign-in.
    await submit(address);
    await page.waitForFunction(() => !document.querySelector(".hub-address"));
    assert.equal(await banner(), "");
    assert.equal(await page.evaluate(() => hubUi.address), "");
    assert.deepEqual(errors, []);
    console.log("PASS: successful callback clears the paste and finishes sign-in without page errors");
  } finally {
    await browser?.close();
    server.kill();
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
