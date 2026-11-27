// A signed-out account offers "Sign in again" only when the setup gives its provider a sign-in command (SPEC UI-85).
// Uses the hub fixture (serve_hub.py) with a Codex sign-in alone and a signed-out Codex and Claude account; the
// stand-in login answers, and no provider receives a real sign-in.
"use strict";

const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");

const ROOT = path.resolve(__dirname, "..", "..");
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const CODEX = "codex-three@example.test-plus.json";
const CLAUDE = "claude-four@example.test.json";

async function main() {
  const work = workFolder("cockpit-hub-sign-in-again-");
  fs.writeFileSync(path.join(work, "hub.json"), JSON.stringify({
    sign_in: ["codex"],
    credentials: [
      { id: CODEX, auth_index: 1, provider: "codex", email: "three@example.test", requires_sign_in: true },
      { id: CLAUDE, auth_index: 2, provider: "claude", email: "four@example.test", requires_sign_in: true },
    ],
  }));
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
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const answers = [];
    page.on("response", (response) => {
      if (new URL(response.url()).pathname.startsWith("/api/hub/")) answers.push(`${new URL(response.url()).pathname} ${response.status()}`);
    });
    await page.goto(`${info.origin}/usage`);
    const card = (id) => `#${id.replace(/[@.]/g, (c) => `\\${c}`)}`;
    const keys = (id) => page.locator(`${card(`account-${id}`)} .hub-keys button`).allInnerTexts();
    await page.waitForFunction((id) => document.getElementById(`account-${id}`), CLAUDE, { timeout: 120e3 });
    await page.waitForFunction((id) => document.getElementById(`account-${id}`), CODEX);

    assert.deepEqual(await keys(CODEX), ["Sign in again"]);
    console.log("PASS: a signed-out Codex account, whose provider the setup signs in, offers Sign in again");
    assert.deepEqual(await keys(CLAUDE), []);
    console.log("PASS: a signed-out Claude account, whose provider the setup gives no sign-in, offers no Sign in again");
    await page.click(".hub-add > summary");
    assert.deepEqual(await page.locator(".hub-add button").allInnerTexts(), ["Codex account"]);
    await page.keyboard.press("Escape");
    console.log("PASS: Add account lists Codex alone");
    await page.screenshot({ path: path.join(work, "sign-in-again.png") });

    await page.click(`${card(`account-${CODEX}`)} .hub-keys button`);
    await page.waitForFunction(() => document.querySelector(".hub-sign-in"));
    assert.ok(answers.includes("/api/hub/codex/sign-in 200"), answers.join(", "));
    assert.ok(!answers.some((answer) => answer.endsWith(" 404")), answers.join(", "));
    assert.deepEqual(errors, []);
    console.log("PASS: Sign in again starts the Codex sign-in, with no 404 and no page errors");

    // The Inbox reads every provider's sign-in for its warnings; this setup has no Claude sign-in at all.
    await page.goto(`${info.origin}/`);
    await page.waitForFunction(() => document.body.innerText.includes("Signing a Codex account into the hub")
      || document.body.innerText.includes("Enter this code on OpenAI's page"), null, { timeout: 60e3 });
    assert.deepEqual(errors, []);
    console.log("PASS: the Inbox shows the Codex sign-in warning, with no page errors, when only Codex signs in");
  } finally {
    await browser?.close();
    server.kill();
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
