// Feedback stays below the picking bar/header and inside the window as the layout changes.
// node tests/browser/feedback_dock.cjs
"use strict";
const assert = require("node:assert/strict");
const path = require("node:path");
const { startPreview } = require("./work.cjs");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");

(async () => {
  const { work, info } = await startPreview("cockpit-feedback-dock-");
  const browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE || undefined });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    await context.addCookies([{ name: info.name, value: info.value, domain: new URL(info.origin).hostname,
      path: "/", secure: true, httpOnly: true, sameSite: "Lax" }]);
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(info.origin + "/usage", { waitUntil: "networkidle" });
    await page.locator("#feedback-mode").click();
    await page.locator(".spend-head h1").click();
    const box = page.locator("#pick-box");
    const area = box.locator("textarea");
    await area.fill("Keep the feedback controls in view");

    async function fits(name) {
      await page.waitForFunction(() => {
        const box = document.getElementById("pick-box").getBoundingClientRect();
        const bar = document.getElementById("pick-bar");
        const header = document.querySelector("header.top").getBoundingClientRect();
        const top = !bar.hidden ? bar.getBoundingClientRect().bottom + 8 : header.width >= innerWidth / 2 ? header.bottom + 8 : 16;
        return box.top >= Math.max(16, top) - 1 && box.bottom <= innerHeight - 15
          && box.left >= 15 && box.right <= innerWidth - 15;
      });
      console.log("pass: " + name);
    }

    await fits("typing leaves feedback within the desktop window");
    await page.screenshot({ path: path.join(work, "feedback-desktop.png") });
    await box.getByRole("button", { name: "Pick again" }).click();
    await fits("Pick again moves the box below the picking bar");
    await page.keyboard.press("Escape");
    for (const [width, height] of [[1279, 900], [1280, 900], [1440, 360], [390, 844]]) {
      await page.setViewportSize({ width, height });
      await fits(`feedback fits at ${width} by ${height}`);
      await box.getByRole("button", { name: "Good writing", exact: true }).scrollIntoViewIfNeeded();
      assert.equal(await area.inputValue(), "Keep the feedback controls in view");
      await page.screenshot({ path: path.join(work, `feedback-${width}-${height}.png`) });
    }
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
