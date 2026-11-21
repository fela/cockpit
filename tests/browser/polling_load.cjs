// Real-page slow polling and stale prefetched transcript recovery on a synthetic board.
"use strict";
const assert = require("node:assert/strict");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const { startPreview } = require("./work.cjs");

async function main() {
  const { info } = await startPreview("cockpit-polling-load-");
  const browser = await chromium.launch({ headless: true, executablePath: process.env.BROWSER_EXECUTABLE || undefined });
  const held = [];
  const waitHeld = async () => {
    for (const end = Date.now() + 10000; Date.now() < end; await new Promise((resolve) => setTimeout(resolve, 10))) {
      if (held.length) return;
    }
    throw new Error("The expected state request did not arrive within 10 seconds");
  };
  try {
    const context = await browser.newContext({ serviceWorkers: "block", extraHTTPHeaders: { Cookie: `${info.name}=${info.value}` } });
    // Drive the actual five-second polling callback ourselves while controlling each state response.
    await context.addInitScript(() => {
      const interval = window.setInterval;
      window.setInterval = (callback, ms, ...args) => {
        if (ms === 5000 && String(callback).includes("refresh();")) {
          window.testStatePoll = callback;
          return 0;
        }
        return interval(callback, ms, ...args);
      };
    });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(info.origin);
    await page.locator("#next [data-step]").first().waitFor();
    await page.waitForFunction(() => !ui.refreshing && [...ui.histories.values()].every((history) => !history.loading));
    const state = await page.evaluate(() => structuredClone(stateSnapshot));
    let reads = 0, gateReads = true;
    await page.route("**/api/state*", async (route) => {
      reads++;
      if (gateReads) await new Promise((resolve) => held.push(resolve));
      await route.fulfill({ json: state });
    });
    await page.evaluate(() => { void refresh(); });
    await page.waitForFunction(() => ui.refreshing);
    await waitHeld();
    await page.evaluate(() => { testStatePoll(); testStatePoll(); testStatePoll(); });
    held.shift()();
    await page.waitForFunction(() => !ui.refreshing);
    assert.equal(reads, 1, "three periodic ticks during a slow read must leave no queued request");
    console.log("PASS A slow state read finishes without an automatic queued read");

    await page.evaluate(() => { testStatePoll(); refresh(); refresh(); });
    await waitHeld();
    held.shift()();
    await waitHeld();
    assert.equal(reads, 3, "actions during a slow read must still request one immediate follow-up");
    held.shift()();
    await page.waitForFunction(() => !ui.refreshing);
    gateReads = false;
    console.log("PASS Explicit refreshes during a slow read retain one immediate follow-up");

    const prefetched = await page.evaluate(() => {
      const shown = new Set([...document.querySelectorAll("section:is(.history, .hist-fold-card)[data-item]")].map((el) => Number(el.dataset.item)));
      return [...ui.histories].find(([id, known]) => !shown.has(id) && known.data && !known.error)?.[0];
    });
    assert.ok(prefetched, "the fixture must have an upcoming card with a prefetched history");
    const oldHistory = await page.evaluate((id) => structuredClone(ui.histories.get(id).data), prefetched);
    const reply = "Transcript answer received while this card was waiting";
    const entry = {
      at: state.now, step_id: null, folded: [], you: { kind: "message", text: "What changed?" },
      answer: { kind: "reply", at: state.now, text: reply, text_html: reply }, notes: [],
      after: { others: [], others_line: "", time: { from: state.now, to: state.now } },
    };
    let histories = 0;
    await page.route(`**/api/items/${prefetched}/history`, async (route) => {
      histories++;
      await route.fulfill({ json: { ...oldHistory, entries: [...oldHistory.entries, entry] } });
    });
    await page.evaluate(() => {
      for (const known of ui.histories.values()) known.at -= 60000;
      for (let i = 0; i < 5; i++) warmNext(drawn(ui.state));
    });
    await page.waitForFunction(() => [...ui.histories.values()].every((history) => !history.loading));
    assert.equal(histories, 0, "idle redraws must reuse unchanged upcoming histories");
    await page.evaluate((id) => {
      selectItem(id);
    }, prefetched);
    await page.locator("#next .fold-all > summary").first().click();
    await page.getByText(reply, { exact: true }).first().waitFor();
    assert.equal(histories, 1, "opening the stale card must fetch its transcript immediately");
    console.log("PASS Upcoming histories avoid idle re-fetches and recover a transcript change when opened");
    assert.deepEqual(errors, []);
    await context.close();
  } finally {
    for (const release of held) release();
    await browser.close();
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
