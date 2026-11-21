"use strict";

const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const { startPreview } = require("./work.cjs");

async function main() {
  const { info } = await startPreview("cockpit-timer-overlay-recovery-");
  const browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE });
  const agent = async (method, path, body) => {
    const response = await fetch(info.origin + path, { method, headers: {
      authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "timer-overlay-test", "content-type": "application/json",
    }, body: JSON.stringify(body) });
    assert.ok(response.ok, `${method} ${path}: ${response.status}`);
    return response.json();
  };
  try {
    const key = "note:timer-overlay-recovery";
    const item = await agent("PUT", "/api/items", { key, title: "Timer and overlay test", priority: "urgent" });
    const document = await agent("POST", `/api/items/${item.id}/pages`, {
      title: "Test page", html: "<h1>Read this page</h1>",
    });
    const { step } = await agent("POST", "/api/steps", { item_key: key, need: "test", ask: "Read the page",
      body: `[Open the test page](${document.url}).` });
    for (const width of [1280, 390]) {
      const context = await browser.newContext({ viewport: { width, height: 844 }, serviceWorkers: "block",
        extraHTTPHeaders: { cookie: `${info.name}=${info.value}` } });
      const page = await context.newPage();
      const errors = [];
      page.on("console", (message) => {
        if (message.text().includes("keeping a comment as the page closed")) errors.push(message.text());
      });
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(`${info.origin}/#home/item/${item.id}`);
      const link = page.getByRole("link", { name: "Open the test page", exact: true }).locator("visible=true").first();
      await link.waitFor();
      await page.evaluate((id) => { ui.autoStarted = id; }, step.id);
      if (width === 1280) {
        const tries = [];
        await page.route(`**/api/steps/${step.id}/timer/start`, async (route) => {
          const response = await route.fetch();
          tries.push({ code: response.status(), answer: await response.json(), press: route.request().headers()["x-cockpit-action"] });
          if (tries.length === 1) await route.abort("failed");
          else await route.fulfill({ response });
        });
        assert.equal(await page.evaluate((id) => sendTimer(id, true), step.id), true);
        assert.equal(tries.length, 2);
        assert.equal(tries[0].code, 200);
        assert.equal(tries[0].answer.started, true, "The server saved the start before its reply was lost");
        assert.ok(tries[0].press);
        assert.deepEqual(tries[1], tries[0], "The repeat got the first answer and kept the same press");
        assert.equal(await page.evaluate(async () => (await fetch("/api/state").then((response) => response.json())).timer.step_id), step.id);
        console.log("pass: a committed timer start with a lost response is confirmed by its retry");
      }
      await link.click();
      const overlay = page.locator("dialog.page-overlay[open]");
      const frame = overlay.locator("iframe.page-overlay-frame");
      await frame.evaluate((node) => new Promise((resolve) => {
        node.addEventListener("load", resolve, { once: true });
        node.setAttribute("sandbox", "");
        node.src = "/healthz";
      }));
      assert.equal(await frame.evaluate((node) => node.contentDocument === null), true, "The frame is isolated from Cockpit");
      await overlay.locator(".page-overlay-close").click();
      await overlay.waitFor({ state: "hidden" });
      assert.deepEqual(errors, [], "Closing an isolated frame must not try to read its draft hook");
      console.log(`pass ${width}: an isolated frame closes with no page errors`);

      await page.evaluate(async (id) => {
        if (ui.state.timer?.step_id === id) await sendTimer(id, false);
        const clock = performance.now.bind(performance);
        let release;
        ui.timerSent = new Promise((resolve) => { release = resolve; });
        const delayed = sendTimer(id, true);
        performance.now = () => clock() + TIMER_PRESS_MS + 1;
        try {
          release(null);
          await delayed;
        } finally { performance.now = clock; }
      }, step.id);
      const recovery = page.locator("#toast").getByRole("button", { name: "Start now" });
      await recovery.waitFor();
      if (process.env.SHOTS_DIR) {
        await page.locator("#toast").screenshot({ path: path.join(process.env.SHOTS_DIR, `timer-start-${width}.png`), animations: "disabled" });
      }
      console.log(`pass ${width}: an expired start offers Start now`);
      await context.close();
    }
  } finally {
    await browser.close();
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
