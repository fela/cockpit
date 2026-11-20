"use strict";
const assert = require("node:assert/strict");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const { startPreview } = require("./work.cjs");
(async () => {
  const { info } = await startPreview("cockpit-offline-refusal-order-");
  const browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE });
  const context = await browser.newContext({ serviceWorkers: "allow" });
  await context.addCookies([{ name: info.name, value: info.value, url: info.origin.replace("http:", "https:"), secure: true }]);
  const page = await context.newPage(), sent = [];
  let refusedScore = 2;
  await page.route("**/api/example/queue", async (route) => {
    const request = route.request(), body = request.postDataJSON(); sent.push({ score: body.score, action: request.headers()["x-cockpit-action"] });
    if (body.score === refusedScore) await route.fulfill({ status: 422, json: { detail: "This edit was refused" } });
    else await route.fulfill({ json: { ok: true } });
  });
  try {
    await page.goto(info.origin);
    await page.waitForFunction(() => Boolean(ui.state) && CockpitOffline.status().shellReady);
    const register = () => page.evaluate(async () => {
      CockpitOffline.register({ name: "Example", label: () => "Counter", root: "/api/example/", send: "/api/example/queue", saves: (pathname) => pathname === "/api/example/counter", prepare(path, body, reads, at) {
        if (path !== "/api/example/counter/value") return null;
        return { path: "/api/example/queue", body: { kind: "counter_value", score: body.score, at },
          reads: new Map([["/api/example/counter", { score: body.score }]]) };
      } });
    });
    await register();
    await page.evaluate(() => CockpitOffline.save("/api/example/counter", { score: 0 }));
    await context.setOffline(true);
    const receipts = await page.evaluate(async () => [
      await CockpitOffline.write("/api/example/counter/value", { score: 2 }),
      await CockpitOffline.write("/api/example/counter/value", { score: 3 }),
    ]);
    await context.setOffline(false);
    await page.evaluate(() => CockpitOffline.replay());
    await page.waitForFunction(() => CockpitOffline.status().refused === 1 && CockpitOffline.status().pending === 1 && !CockpitOffline.status().replaying);
    assert.deepEqual(sent, [{ score: 2, action: receipts[0].action }]);
    assert.equal(await page.evaluate(async () => (await CockpitOffline.peek("/api/example/counter")).data.score), 3, "Latest pending intent stays visible");
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => CockpitOffline.status().refused === 1 && CockpitOffline.status().pending === 1 && !CockpitOffline.status().replaying);
    await register();
    await page.evaluate(() => CockpitOffline.replay());
    assert.deepEqual(sent, [{ score: 2, action: receipts[0].action }], "A stored refusal still blocks later writes after reload");
    assert.equal(await page.evaluate(async () => (await CockpitOffline.peek("/api/example/counter")).data.score), 3);
    const snapshot = () => page.evaluate(async () => {
      const database = await new Promise((resolve) => { const request = indexedDB.open(`cockpit-offline-v1-${CockpitOffline.scope}`); request.onsuccess = () => resolve(request.result); });
      const base = await new Promise((resolve) => { const request = database.transaction("reads").objectStore("reads").get("/api/example/counter"); request.onsuccess = () => resolve(request.result); });
      database.close();
      return { acceptedBase: base.data.score, displayed: (await CockpitOffline.peek("/api/example/counter")).data.score };
    });
    assert.equal((await snapshot()).acceptedBase, 0, "The pending score has not been accepted on the server");
    await page.evaluate((id) => CockpitOffline.discard(id), receipts[0].action);
    await page.waitForFunction(() => CockpitOffline.status().pending === 0 && CockpitOffline.status().refused === 0 && !CockpitOffline.status().replaying);
    assert.deepEqual(sent, [{ score: 2, action: receipts[0].action }, { score: 3, action: receipts[1].action }]);
    assert.deepEqual(await snapshot(), { acceptedBase: 3, displayed: 3 });
    console.log("pass: a refusal blocks later writes across reload; latest pending value remains visible; Discard immediately sends the next edit");
    refusedScore = 4;
    await context.setOffline(true);
    const retry = await page.evaluate(async () => [
      await CockpitOffline.write("/api/example/counter/value", { score: 4 }),
      await CockpitOffline.write("/api/example/counter/value", { score: 5 }),
    ]);
    await context.setOffline(false);
    await page.evaluate(() => CockpitOffline.replay());
    await page.waitForFunction(() => CockpitOffline.status().refused === 1 && CockpitOffline.status().pending === 1 && !CockpitOffline.status().replaying);
    await page.evaluate((id) => CockpitOffline.retry(id), retry[0].action);
    await page.waitForFunction(() => CockpitOffline.status().refused === 1 && CockpitOffline.status().pending === 1 && !CockpitOffline.status().replaying);
    assert.deepEqual(sent.slice(2), [{ score: 4, action: retry[0].action }, { score: 4, action: retry[0].action }]);
    await page.evaluate((id) => CockpitOffline.discard(id), retry[0].action);
    await page.waitForFunction(() => CockpitOffline.status().pending === 0 && CockpitOffline.status().refused === 0 && !CockpitOffline.status().replaying);
    assert.deepEqual(sent.at(-1), { score: 5, action: retry[1].action });
    assert.deepEqual(await snapshot(), { acceptedBase: 5, displayed: 5 });
    console.log("pass: Retry keeps the refused action id and remaining order; a repeated refusal stays blocked until Discard");
  } finally { await context.close(); await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
