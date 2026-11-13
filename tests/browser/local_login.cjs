// Local sign-in and the origin-scoped fetch credential, using the shipped scripts and isolated HTTP fixtures.
// Usage: node tests/browser/local_login.cjs
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;
const staticDir = process.argv[2] || path.join(__dirname, "../../src/cockpit/static");
const origin = "http://127.0.0.1:8790";
const elsewhere = "http://127.0.0.1:15432";
const secret = "test-page-secret";

(async () => {
  const browser = await chromium.launch({ executablePath: CHROMIUM });
  try {
    const context = await browser.newContext();
    const requests = [];
    let used = false;
    await context.route("**/*", async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      requests.push({ url: request.url(), method: request.method(), header: request.headers()["x-cockpit-local"] });
      if (url.pathname.startsWith("/static/")) {
        return route.fulfill({ contentType: "text/javascript", body: fs.readFileSync(path.join(staticDir, path.basename(url.pathname)), "utf8") });
      }
      if (url.pathname === "/auth/local" && request.method() === "POST") {
        assert.deepEqual(request.postDataJSON(), { code: "test-one-time-code" });
        if (used) return route.fulfill({ status: 403, contentType: "application/json", body: '{}' });
        used = true;
        return route.fulfill({ contentType: "application/json", headers: { "set-cookie": "cockpit_local_session=test-session; Path=/; HttpOnly; SameSite=Strict" }, body: JSON.stringify({ ok: true, page_secret: secret }) });
      }
      if (url.pathname === "/api/state") {
        const ok = request.headers()["x-cockpit-local"] === secret;
        return route.fulfill({ status: ok ? 200 : 401, contentType: "application/json", headers: { "X-Cockpit-Auth": "local" }, body: JSON.stringify(ok ? { ok: true } : { detail: "sign in first" }) });
      }
      if (url.pathname === "/echo") {
        return route.fulfill({ contentType: "application/json", headers: { "Access-Control-Allow-Origin": origin }, body: "{}" });
      }
      const login = url.pathname === "/auth/local";
      const html = `<!doctype html><script src='/static/local-auth.js'></script><p id='status'>Run cockpit login in your terminal.</p>${login ? "<script src='/static/local-login.js'></script>" : ""}`;
      return route.fulfill({ contentType: "text/html", body: html });
    });
    const page = await context.newPage();
    await page.goto(origin + "/auth/local#test-one-time-code");
    await page.waitForURL(origin + "/");
    assert.equal(await page.evaluate(() => localStorage.getItem("cockpit.local-secret")), secret);
    assert.equal(await page.evaluate(async () => (await fetch("/api/state")).status), 200);
    assert.equal(requests.filter((r) => r.url === origin + "/api/state").at(-1).header, secret);
    console.log("ok sign-in stores the returned secret and API fetch sends it");

    const next = await context.newPage();
    await next.goto(origin + "/spec");
    assert.equal(await next.evaluate(async () => (await fetch(new Request("/api/state", { headers: { "X-Test": "kept" } }))).status), 200);
    await next.evaluate(async () => { await fetch("/echo", { keepalive: true }); });
    assert.equal(requests.filter((r) => r.url === origin + "/echo").at(-1).header, secret);
    console.log("ok another page finds the secret, including Request and keepalive calls");

    await page.evaluate(async (url) => { await fetch(url); }, elsewhere + "/echo");
    assert.equal(requests.filter((r) => r.url === elsewhere + "/echo").at(-1).header, undefined);
    const attacker = await context.newPage();
    await attacker.goto(elsewhere + "/");
    assert.equal(await attacker.evaluate(() => localStorage.getItem("cockpit.local-secret")), null);
    console.log("ok another port neither receives the fetch header nor shares localStorage");

    await next.evaluate(() => localStorage.removeItem("cockpit.local-secret"));
    await next.evaluate(() => { fetch("/api/state"); });
    await next.waitForURL(origin + "/auth/local");
    await next.waitForTimeout(200);
    assert.equal(await next.locator("#status").textContent(), "Run cockpit login in your terminal.");
    assert.equal(next.url(), origin + "/auth/local");
    assert.equal(requests.filter((r) => r.url === origin + "/auth/local" && r.method === "GET").length, 2, "missing token leaves the login page waiting for cockpit login");
    console.log("ok a cookie without the page secret returns to sign-in without a token loop");

    await next.goto("about:blank");
    await next.goto(origin + "/auth/local#test-one-time-code");
    await next.waitForFunction(() => document.getElementById("status").textContent.includes("Run cockpit login again"));
    assert.equal(next.url(), origin + "/auth/local", "the used code is removed from history too");
    assert.equal(await next.evaluate(() => localStorage.getItem("cockpit.local-secret")), null);
    console.log("ok a used or expired code stays signed out and asks for cockpit login again");
  } finally {
    await browser.close();
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
