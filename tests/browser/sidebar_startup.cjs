"use strict";

const assert = require("node:assert/strict");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const { startPreview } = require("./work.cjs");

async function main() {
  const { info } = await startPreview("cockpit-sidebar-startup-");
  const browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE || undefined });
  let releaseApp;
  const app = new Promise((resolve) => { releaseApp = resolve; });
  try {
    const page = await browser.newPage({
      viewport: { width: 1440, height: 900 }, serviceWorkers: "block",
      extraHTTPHeaders: { Cookie: `${info.name}=${info.value}` },
    });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route(/\/static\/app\.js(?:\?|$)/, async (route) => {
      await app;
      await route.continue();
    });
    await page.goto(info.origin, { waitUntil: "commit" });
    const add = page.locator(".sidebar-add");
    await add.waitFor({ state: "visible" });
    assert.equal(await page.evaluate(() => typeof ui), "undefined");
    const bounds = await add.boundingBox();
    await page.mouse.click(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
    assert.deepEqual(errors, [], "clicking the rail before app.js runs must not throw");
    assert.equal(await add.isDisabled(), true);
    const dialog = page.locator("#add-box");
    assert.equal(await dialog.isHidden(), true);

    releaseApp();
    await add.click();
    await dialog.waitFor({ state: "visible" });
    await page.keyboard.press("Escape");
    await dialog.waitFor({ state: "hidden" });
    assert.deepEqual(errors, []);
    console.log("PASS Rail Add waits for app.js, then opens and closes the composer");
  } finally {
    releaseApp();
    await browser.close();
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
