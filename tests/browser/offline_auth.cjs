"use strict";
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const { startPreview } = require("./work.cjs");

(async () => {
  const { info } = await startPreview("cockpit-offline-auth-");
  const browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE });
  const context = await browser.newContext({ serviceWorkers: "allow" });
  const putCookie = (value) => context.addCookies([{ name: info.name, value, url: info.origin.replace("http:", "https:"), secure: true }]);
  await putCookie(info.value);
  const first = await context.newPage();
  let sent = 0, helperSent = 0;
  await context.route("**/api/example/queue", (route) => { sent++; return route.fulfill({ json: { ok: true } }); });
  await context.route("**/api/item-pages/1/comments", (route) => {
    if (route.request().method() !== "POST") return route.continue();
    helperSent++; return route.fulfill({ json: { ok: true } });
  });
  // The preview's fake Google sign-in stays entirely local.
  await context.route("**/auth/google", (route) => route.fulfill({ contentType: "text/html", body: "<h1>Signed out</h1>" }));
  try {
    await first.goto(info.origin);
    await first.waitForFunction(() => CockpitOffline.status().shellReady && Boolean(ui.state));
    const original = await first.evaluate(() => ({ scope: CockpitOffline.scope, until: CockpitOffline.until }));
    const oldHelper = await context.newPage();
    await oldHelper.goto(info.origin + "/pages");
    await oldHelper.waitForFunction(() => Boolean(window.sendAction));
    assert.equal(await oldHelper.evaluate(() => Number(document.documentElement.dataset.offlineUntil)), original.until);
    await oldHelper.evaluate(() => { const privateText = document.createElement("p"); privateText.textContent = "PRIVATE_OLDER_HELPER_TEXT"; document.body.append(privateText); });
    await first.evaluate(async () => {
      CockpitOffline.register({ name: "Example", label: () => "Counter", root: "/api/example/", send: "/api/example/queue", saves: (pathname) => pathname === "/api/example/counter", prepare(path, body, reads, at) {
        if (path !== "/api/example/counter/value") return null;
        return { path: "/api/example/queue", body: { kind: "counter_value", score: body.score, at },
          reads: new Map([["/api/example/counter", { score: body.score }]]) };
      } });
      await CockpitOffline.save("/api/example/counter", { score: 4 });
      await new Promise((resolve, reject) => {
        const request = indexedDB.open(`cockpit-offline-v1-${CockpitOffline.scope}`);
        request.onsuccess = () => { window.oldDatabaseBlocker = request.result; resolve(); };
        request.onerror = () => reject(request.error);
      });
    });
    await context.setOffline(true);
    await first.evaluate(() => CockpitOffline.write("/api/example/counter/value", { score: 8 }));
    assert.equal(await first.evaluate(() => CockpitOffline.status().pending), 1);
    // This is the same fake authentication key/email that serve_preview imports from tests/conftest.py.
    const renewed = original.until + 3600;
    const signature = crypto.createHmac("sha256", "k".repeat(32)).update(`cockpit-session\0sam@example.com\0${renewed}`).digest("hex");
    await putCookie(`${renewed}.${signature}`);
    await context.setOffline(false);
    const second = await context.newPage();
    await second.goto(info.origin);
    await second.waitForFunction(() => CockpitOffline.status().shellReady && Boolean(ui.state));
    await first.waitForFunction(() => CockpitOffline.status().stopped && ui.state === null);
    await oldHelper.getByRole("link", { name: "Reopen Cockpit" }).waitFor();
    assert.ok(!(await oldHelper.locator("body").innerText()).includes("PRIVATE_OLDER_HELPER_TEXT"));
    const helperRefused = await oldHelper.evaluate(async () => {
      try { await sendAction("/api/item-pages/1/comments", { method: "POST", headers: { "content-type": "application/json" }, body: '{"body":"Private writing"}' }, 30000, null); return false; }
      catch { return true; }
    });
    assert.equal(helperRefused, true); assert.equal(helperSent, 0);
    assert.equal(sent, 0, "An earlier authenticated session's saved edit must not replay under a renewed login");
    assert.equal(await second.evaluate(() => CockpitOffline.until), renewed);
    assert.equal(await second.evaluate(() => {
      CockpitOffline.register({ name: "Example", label: () => "Counter", root: "/api/example/", send: "/api/example/queue", saves: (pathname) => pathname === "/api/example/counter", prepare: () => null });
      return CockpitOffline.peek("/api/example/counter");
    }), null);
    const current = await second.evaluate(() => CockpitOffline.scope);
    assert.notEqual(current, original.scope);
    const shells = await second.evaluate(() => caches.keys());
    assert.ok(shells.includes(`cockpit-shell-v1-${current}`));
    assert.ok(!shells.includes(`cockpit-shell-v1-${original.scope}`));
    const databases = await second.evaluate(() => indexedDB.databases());
    assert.ok(databases.some((entry) => entry.name === `cockpit-offline-v1-${original.scope}`), "The deliberately held old connection is still awaiting cleanup");
    await first.evaluate(() => window.oldDatabaseBlocker.close());
    await second.waitForFunction(async (old) => !(await indexedDB.databases()).some((entry) => entry.name === `cockpit-offline-v1-${old}`), original.scope);
    console.log("pass: renewed session has isolated storage; old tab locks, old queued edit never sends, newer shell survives old cleanup");
    console.log("pass: old helper tab conceals its writing and sends no mutation under a renewed login");
    const freshHelper = await context.newPage();
    await freshHelper.goto(info.origin + "/pages");
    await freshHelper.waitForFunction(() => Boolean(window.sendAction));
    await second.locator('form[action="/auth/sign-out"]').evaluate((form) => form.requestSubmit());
    await second.getByRole("heading", { name: "Signed out" }).waitFor();
    await freshHelper.getByRole("link", { name: "Reopen Cockpit" }).waitFor();
    assert.equal(await freshHelper.getByRole("link", { name: "Reopen Cockpit" }).evaluate((link) => link.closest("[inert]") === null), true);
    assert.ok(!(await second.evaluate(() => caches.keys())).some((name) => name.startsWith("cockpit-shell-v1-")));
    await context.setOffline(true);
    await second.goto(info.origin);
    assert.match(await second.locator("body").innerText(), /signed in and connected/);
    console.log("pass: online sign-out follows authentication redirect and later offline navigation cannot restore the old shell");
  } finally { await context.close(); await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
