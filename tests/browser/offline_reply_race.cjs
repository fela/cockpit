"use strict";
// A read that arrives while an offline edit's reply is on its way can lack the record the edit changed.
// The accepted edit still leaves the queue once, without an error in either tab, and later edits still send.
const assert = require("node:assert/strict");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const { startPreview } = require("./work.cjs");
const counter = "/api/example/counter";

async function register(page) {
  await page.evaluate(() => {
    CockpitOffline.register({ name: "Example", label: () => "Counter", root: "/api/example/", send: "/api/example/queue", saves: (pathname) => pathname === "/api/example/counter", prepare(path, body, reads, at) {
      if (path !== "/api/example/counter/value") return null;
      if (reads.get("/api/example/counter")?.gone) throw Object.assign(new Error("Open the counter before changing it offline"), { offlineEditUnavailable: true });
      return { path: "/api/example/queue", body: { kind: "counter_value", score: body.score, at },
        reads: new Map([["/api/example/counter", { score: body.score }]]) };
    } });
  });
}

(async () => {
  const { info } = await startPreview("cockpit-offline-reply-race-");
  const browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE });
  const context = await browser.newContext({ serviceWorkers: "allow" });
  await context.addCookies([{ name: info.name, value: info.value, url: info.origin.replace("http:", "https:"), secure: true }]);
  const page = await context.newPage(), sent = [];
  let confirm;
  const confirmation = new Promise((resolve) => { confirm = resolve; });
  await page.route("**/api/example/queue", async (route) => {
    const request = route.request();
    sent.push({ score: request.postDataJSON().score, action: request.headers()["x-cockpit-action"] });
    if (sent.length === 1) await confirmation;
    await route.fulfill({ json: { ok: true } });
  });
  try {
    await page.goto(info.origin);
    await page.waitForFunction(() => Boolean(ui.state) && CockpitOffline.status().shellReady);
    await register(page);
    await page.evaluate((path) => CockpitOffline.save(path, { score: 0 }), counter);
    await context.setOffline(true);
    const first = await page.evaluate(() => CockpitOffline.write("/api/example/counter/value", { score: 2 }));
    const post = page.waitForRequest((request) => request.url().endsWith("/api/example/queue"));
    await context.setOffline(false);
    await post;
    const other = await context.newPage();
    await other.addInitScript(() => {
      window.queueErrors = [];
      addEventListener("cockpit:offline-status", ({ detail }) => { if (detail.error) window.queueErrors.push(detail.error); });
    });
    await other.goto(info.origin);
    await register(other);
    await other.waitForFunction(async (path) => (await CockpitOffline.peek(path))?.data.score === 2, counter);
    // The new read has dropped the counter, but the server's confirmation is still on its way.
    await page.evaluate(async (path) => { await CockpitOffline.save(path, { gone: true }); await CockpitOffline.peek(path); }, counter);
    await other.waitForFunction(async (path) => (await CockpitOffline.peek(path))?.data.gone === true, counter);
    const observed = await other.evaluate(() => ({ pending: CockpitOffline.status().pending, errors: window.queueErrors }));
    confirm();
    await other.waitForFunction(() => CockpitOffline.status().pending === 0);
    assert.deepEqual(observed, { pending: 1, errors: [] }, "the second tab showed an error while the confirmation was on its way");
    await other.close();
    await page.waitForFunction(() => !CockpitOffline.status().replaying && CockpitOffline.status().pending === 0, null, { timeout: 10000 });
    assert.deepEqual(sent, [{ score: 2, action: first.action }], "the accepted edit was sent more than once");
    const status = await page.evaluate(() => CockpitOffline.status());
    assert.equal(status.refused, 0);
    assert.equal(status.error, null, "a read without the record turned the accepted edit into a page error");
    assert.deepEqual((await page.evaluate(() => CockpitOffline.peek("/api/example/counter"))).data, { gone: true }, "the fresh read was replaced by the local projection");
    await page.evaluate(() => CockpitOffline.save("/api/example/counter", { score: 5 }));
    const second = await page.evaluate(() => CockpitOffline.write("/api/example/counter/value", { score: 6 }));
    await page.waitForFunction(() => !CockpitOffline.status().replaying && CockpitOffline.status().pending === 0, null, { timeout: 10000 });
    assert.deepEqual(sent.at(-1), { score: 6, action: second.action }, "a later edit waited behind the accepted one");
    assert.equal(sent.length, 2);
    console.log("pass: an edit accepted while a read dropped its record leaves both tabs' queues once, without a page error, and later edits send");
  } finally { confirm(); await context.close(); await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
