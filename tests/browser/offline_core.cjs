"use strict";
const assert = require("node:assert/strict");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const { startPreview } = require("./work.cjs");

// Use a deterministic safe adapter to isolate durable queue behavior from a part's data rules.
async function adapter(page) {
  await page.evaluate(() => CockpitOffline.register({ name: "Example", label: () => "Counter", root: "/api/example/", send: "/api/example/queue", saves: (pathname) => ["/api/example/counter", "/api/example/log"].includes(pathname), prepare(path, body, reads, at) {
    if (path !== "/api/example/counter/value") return null;
    const before = reads.get("/api/example/counter") || { score: 0 };
    return { path: "/api/example/queue", body: { kind: "counter_value", score: body.score, at },
      reads: new Map([["/api/example/counter", { ...before, score: body.score }]]) };
  } }));
}

(async () => {
  const { info } = await startPreview("cockpit-offline-core-");
  const browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE });
  const context = await browser.newContext({ serviceWorkers: "allow" });
  await context.addCookies([{ name: info.name, value: info.value, url: info.origin.replace("http:", "https:"), secure: true }]);
  const page = await context.newPage();
  page.setDefaultTimeout(30000);
  const calls = []; let mode = "refuse-second";
  await page.route("**/api/example/queue", async (route) => {
    const request = route.request(), body = request.postDataJSON();
    calls.push({ action: request.headers()["x-cockpit-action"], body });
    if (mode === "malformed") await route.fulfill({ status: 200, contentType: "application/json", body: "{" });
    else if (mode === "refuse-second" && body.score === 2) await route.fulfill({ status: 422, contentType: "application/json", body: '{"detail":"This value is no longer editable"}' });
    else await route.fulfill({ json: { ok: true } });
  });
  await page.route("**/auth/google", (route) => route.fulfill({ contentType: "text/html", body: "<h1>Signed out</h1>" }));
  try {
    await page.goto(info.origin);
    await page.waitForFunction(() => Boolean(ui.state) && CockpitOffline.status().shellReady);
    await adapter(page);
    await page.evaluate(async () => {
      const state = structuredClone(ui.state);
      state.claude_sign_ins = [{ code: "NEVER_SAVE_AUTH" }];
      state.codex_sign_ins = [{ user_code: "NEVER_SAVE_AUTH" }];
      state.hub = { client_keys: ["NEVER_SAVE_AUTH"] };
      state.permission_requests = [{ tool_input: "NEVER_SAVE_AUTH" }];
      state.queue.push({ signin: { code: "NEVER_SAVE_AUTH" }, permission: "NEVER_SAVE_AUTH", command: "NEVER_SAVE_AUTH" });
      state.timer = { id: "NEVER_SAVE_AUTH" };
      await CockpitOffline.save("/api/state", state);
      await CockpitOffline.save("/api/example/counter", { score: 0 });
      await CockpitOffline.save("/api/permissions", { allow: ["NEVER_SAVE_AUTH"] });
      await CockpitOffline.save("/api/example/secret/now", { token: "NEVER_SAVE_AUTH" });
    });
    assert.equal(await page.evaluate(async () => JSON.stringify(await CockpitOffline.peek("/api/state")).includes("NEVER_SAVE_AUTH")), false);
    assert.equal(await page.evaluate(() => CockpitOffline.peek("/api/permissions")), null);
    assert.equal(await page.evaluate(() => CockpitOffline.peek("/api/example/secret/now")), null);
    // Remove the synthetic incomplete card before using the board snapshot on the next reload.
    await page.evaluate(() => CockpitOffline.save("/api/state", ui.state));
    await page.evaluate(async () => {
      await CockpitOffline.read("/api/example/counter", () => new Promise((resolve) => { window.releaseOldCounterRead = resolve; }));
      await CockpitOffline.readFresh("/api/example/counter", async () => ({ score: 4 }));
      window.releaseOldCounterRead({ score: 0 });
    });
    await page.waitForFunction(async () => (await CockpitOffline.peek("/api/example/counter")).data.score === 4);
    await page.evaluate(async () => {
      await CockpitOffline.save("/api/example/counter", { score: 0 });
    });
    await context.setOffline(true);
    for (const path of ["/api/example/secret/now", "/api/example/log?limit=99"]) {
      const message = await page.evaluate((path) => CockpitOffline.read(path, () => new Promise(() => {})).then(() => "unexpected success", (error) => error.message), path);
      assert.match(message, /Connect to Cockpit/);
    }
    const receipts = await page.evaluate(async () => [await CockpitOffline.write("/api/example/counter/value", { score: 1 }), await CockpitOffline.write("/api/example/counter/value", { score: 2 })]);
    assert.notEqual(receipts[0].action, receipts[1].action);
    assert.equal(await page.evaluate(async () => (await CockpitOffline.peek("/api/example/counter")).data.score), 2);
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => CockpitOffline.status().pending === 2);
    await adapter(page);
    await page.evaluate(() => {
      let calls = 0;
      window.beforeAckRead = CockpitOffline.readFresh("/api/example/counter", () => ++calls === 1
        ? new Promise((resolve) => { window.releaseBeforeAckRead = resolve; }) : Promise.resolve({ score: 1 }));
    });
    assert.equal(await page.evaluate(async () => (await CockpitOffline.peek("/api/example/counter")).data.score), 2);
    const quick = await page.evaluate(() => Promise.race([
      CockpitOffline.read("/api/example/counter", () => new Promise(() => {})),
      new Promise((_, reject) => setTimeout(() => reject(new Error("Cached read waited for network")), 100)),
    ]));
    assert.equal(quick.score, 2);
    await context.setOffline(false);
    await page.evaluate(() => CockpitOffline.replay());
    await page.waitForFunction(() => CockpitOffline.status().refused === 1 && CockpitOffline.status().pending === 0);
    await page.evaluate(async () => { window.releaseBeforeAckRead({ score: 0 }); await window.beforeAckRead; });
    assert.deepEqual(calls.slice(0, 2).map((entry) => entry.action), receipts.map((entry) => entry.action));
    const refused = await page.evaluate(() => CockpitOffline.status().failures[0]);
    await page.evaluate((id) => CockpitOffline.discard(id), refused.id);
    assert.equal(await page.evaluate(async () => (await CockpitOffline.peek("/api/example/counter")).data.score), 1, "Accepting the first edit must not commit the later refused projection");
    mode = "malformed";
    const third = await page.evaluate(() => CockpitOffline.write("/api/example/counter/value", { score: 3 }));
    await page.waitForFunction(() => CockpitOffline.status().error?.includes("incomplete"));
    assert.equal(await page.evaluate(() => CockpitOffline.status().pending), 1);
    mode = "ok";
    await page.evaluate(() => CockpitOffline.replay());
    await page.waitForFunction(() => CockpitOffline.status().pending === 0);
    assert.deepEqual(calls.slice(-2).map((entry) => entry.action), [third.action, third.action]);
    console.log("pass: cached reads, secret stripping, reload-surviving ordered edits, refusal/discard isolation and truncated-ACK retry");
    await page.evaluate(() => {
      window.replayTimeoutTest = { fetch: window.fetch.bind(window), timeout: AbortSignal.timeout, mode: "principal", principal: 0, posts: [] };
      AbortSignal.timeout = (ms) => window.replayTimeoutTest.timeout(ms === 30000 ? 50 : ms);
      window.fetch = (input, options) => {
        const test = window.replayTimeoutTest, path = new URL(input instanceof Request ? input.url : input, location.href).pathname;
        const hold = () => new Promise((_, reject) => options.signal.addEventListener("abort", () => reject(options.signal.reason), { once: true }));
        if (path === "/api/offline/session") {
          if (test.mode === "principal") { test.principal++; return hold(); }
          return Promise.resolve(Response.json({ user: CockpitOffline.user, until: CockpitOffline.until }));
        }
        if (path === "/api/example/queue" && test.mode === "post") { test.posts.push(options.headers["x-cockpit-action"]); return hold(); }
        return test.fetch(input, options);
      };
    });
    const fourth = await page.evaluate(() => CockpitOffline.write("/api/example/counter/value", { score: 4 }));
    await page.waitForFunction(() => window.replayTimeoutTest.principal > 0 && !CockpitOffline.status().replaying);
    assert.equal(await page.evaluate(() => CockpitOffline.status().pending), 1);
    await page.evaluate(async () => { window.replayTimeoutTest.mode = "post"; await CockpitOffline.replay(); });
    assert.equal(await page.evaluate(() => CockpitOffline.status().pending), 1);
    assert.equal(await page.evaluate(() => CockpitOffline.status().refused), 0);
    assert.deepEqual(await page.evaluate(() => window.replayTimeoutTest.posts), [fourth.action]);
    await page.evaluate(() => { window.fetch = window.replayTimeoutTest.fetch; AbortSignal.timeout = window.replayTimeoutTest.timeout; });
    await page.evaluate(() => CockpitOffline.replay());
    await page.waitForFunction(() => CockpitOffline.status().pending === 0);
    assert.equal(calls.at(-1).action, fourth.action);
    console.log("pass: principal and queued POST deadlines retain the edit and retry its stable action id");
    await context.setOffline(true);
    await page.evaluate(() => localStorage.setItem("cockpit.local-secret", "synthetic-local-credential"));
    await page.locator('form[action="/auth/sign-out"]').evaluate((form) => form.requestSubmit());
    await page.waitForURL(info.origin + "/");
    await page.waitForFunction(() => !window.CockpitOffline);
    assert.match(await page.locator("body").innerText(), /signed in and connected/);
    assert.equal(await page.evaluate(async () => (await caches.keys()).some((name) => name.startsWith("cockpit-shell-v1-"))), false);
    assert.equal(await page.evaluate(() => localStorage.getItem("cockpit.offline-sign-out")), "1");
    assert.equal(await page.evaluate(() => localStorage.getItem("cockpit.local-secret")), "synthetic-local-credential");
    console.log("pass: explicit offline sign-out clears saved shell and personal storage before navigation");
    await context.setOffline(false);
    await page.goto(info.origin);
    await page.getByRole("heading", { name: "Signed out" }).waitFor();
    assert.equal(await page.evaluate(() => localStorage.getItem("cockpit.offline-sign-out")), null);
    assert.equal(await page.evaluate(() => localStorage.getItem("cockpit.local-secret")), null);
    assert.ok(!(await context.cookies()).some((cookie) => cookie.name === info.name));
    console.log("pass: reconnecting after offline sign-out clears the server cookie before reopening the account");
  } finally { await context.close(); await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
