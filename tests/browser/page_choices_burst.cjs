// Real iframe teardown while text waits in the parent's choice queue; synthetic data, no live server.
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(__dirname, "../../src/cockpit/static");
(async () => {
  const browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE });
  try {
    for (const width of [1280, 390]) {
      const page = await browser.newPage({ viewport: { width, height: 844 } });
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.route("https://choices.test/pages/3/figure", (route) => route.fulfill({ contentType: "text/html", body:
        '<label>Comment<textarea name="comment"></textarea></label><label><input type="checkbox" name="notify" value="yes">Notify</label>' +
        `<script>${fs.readFileSync(path.join(root, "page-choices-frame.js"), "utf8")}</script>` }));
      await page.goto("about:blank");
      await page.evaluate(() => {
        window.TRY_MS = 30000;
        window.sent = [];
        window.holding = new Set();
        window.holdLeave = (work) => { holding.add(work); work.finally(() => holding.delete(work)); };
        window.replyData = ({data}) => data;
        window.logError = (_where, error) => { throw error; };
        window.readThroughRestart = async () => ({data: {choices: []}});
        window.sendAction = async (_url, options) => { sent.push(JSON.parse(options.body)); return {data: {}}; };
      });
      await page.addScriptTag({path: path.join(root, "page-choices.js")});
      await page.evaluate(() => {
        const frame = document.createElement("iframe");
        frame.src = "https://choices.test/pages/3/figure";
        frame.dataset.step = "7";
        document.body.append(frame);
      });
      const frame = page.frameLocator("iframe");
      await frame.locator("textarea").fill("Ready");
      await page.waitForFunction(() => holding.size === 1);
      // Remove the overlay's iframe before the quiet interval, then Send from the remaining main page.
      await page.evaluate(() => document.querySelector("iframe").remove());
      assert.equal(await page.evaluate(() => holding.size), 1);
      await page.evaluate(() => CockpitChoices.settled(7));
      assert.deepEqual(await page.evaluate(() => sent.map((body) => body.chosen)), ["Ready"]);
      assert.equal(await page.evaluate(() => holding.size), 0);
      assert.deepEqual(errors, []);
      console.log(`PASS ${width}px: queued text survives iframe removal and Send saves the final value`);
      await page.close();
    }
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
