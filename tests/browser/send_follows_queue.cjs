// After the user answers the step of a task they picked, the card shows the queue's first step, and a Back that
// returns to the same history entry keeps it there (regression check). A page layer pushes its own entry when it
// opens and goes Back when it closes; before the fix, that Back showed the task the user had just answered again,
// on its Busy card. At 1440 px and at 390 px it checks:
// - after an answer on a picked task's step, the card shows the queue's first step;
// - a page layer's entry pushed and taken back leaves the card on the queue's first step;
// - Back from there still goes to the task picked before;
// - an answer sent while a page is open (beside the card) stays on the queue's first step when the page closes.
//
// Starts serve_preview.py through startPreview (README.md says what it needs) and drives headless Chromium.
//
// Usage, from the checkout: node tests/browser/send_follows_queue.cjs

"use strict";
const assert = require("node:assert/strict");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const { startPreview } = require("./work.cjs");

(async () => {
  const { info } = await startPreview("cockpit-send-follows-queue-");
  const browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE });
  try {
    for (const width of [1440, 390]) {
      const context = await browser.newContext({ viewport: { width, height: 900 }, reducedMotion: "reduce" });
      await context.addCookies([{ name: info.name, value: info.value, url: info.origin.replace("http:", "https:"), secure: true }]);
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(info.origin);
      await page.waitForFunction(() => Boolean(ui.state) && cockpitNavigation.initialized && ui.state.queue.length >= 3);
      const [before, answered] = await page.evaluate(() => ui.state.queue.slice(1, 3).map((step) => step.item_id));
      await page.evaluate((id) => selectItem(id), before);
      await page.evaluate((id) => selectItem(id), answered);
      // What every answer, hand-off, snooze or close on the picked step does to the card (app.js).
      await page.evaluate((id) => followQueue(ui.state.queue.find((step) => step.item_id === id)), answered);
      assert.equal(await page.evaluate(() => selectedItem()), null);

      // A page layer opening and closing (page-overlay.js): its own entry, then Back.
      await page.evaluate(() => history.pushState({ cockpitPageOverlay: true }, "", location.href));
      await page.goBack();
      await page.waitForFunction(() => !history.state?.cockpitPageOverlay && !cockpitNavigation.restoring);
      assert.equal(await page.evaluate(() => selectedItem()), null, "a closing page put the answered task back on the card");
      console.log(`pass ${width}: after an answer, a page closing leaves the card on the queue's first step`);

      await page.goBack();
      await page.waitForFunction((id) => selectedItem() === id, before);
      console.log(`pass ${width}: Back still returns to the task picked before`);

      // A page open beside the card while the user sends from the card, then closing.
      await page.evaluate((id) => selectItem(id), answered);
      await page.evaluate(() => history.pushState({ cockpitPageOverlay: true }, "", location.href));
      await page.evaluate((id) => followQueue(ui.state.queue.find((step) => step.item_id === id)), answered);
      assert.equal(await page.evaluate(() => selectedItem()), null);
      await page.goBack();
      await page.waitForFunction(() => !history.state?.cockpitPageOverlay && !cockpitNavigation.restoring);
      assert.equal(await page.evaluate(() => selectedItem()), null, "a page open during the answer put the task back on closing");
      console.log(`pass ${width}: an answer sent while a page is open stays on the queue's first step when the page closes`);
      assert.deepEqual(errors, []);
      await context.close();
    }
  } finally {
    await browser.close();
  }
})().catch((error) => { console.error(error); process.exit(1); });
