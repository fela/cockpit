// The time zone question and picker (SPEC CONTROL-32), at desktop light and phone dark, on a synthetic database
// through serve_preview.py. Cockpit is set to Jakarta; the browser runs in Tokyo, seen there for seven hours. Desktop
// keeps Jakarta time and checks the card goes, then that a page left open reports and offers the device's new zone
// once it moves; phone switches through the brand menu's picker and checks the setup.
"use strict";
const { spawn, spawnSync } = require("node:child_process");
const fs = require("node:fs"), path = require("node:path"), assert = require("node:assert/strict");
const { workFolder } = require("./work.cjs");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const work = workFolder("cockpit-zone-ask-");
const python = path.join(__dirname, "../../.venv/bin/python");
const server = spawn(python, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "ignore", "inherit"] });
const pause = (ms) => new Promise((r) => setTimeout(r, ms));

// Tokyo first reported seven hours ago, as a laptop on a trip would have.
function seedTrip() {
  const first = new Date(Date.now() - 7 * 3600e3).toISOString().replace(/\.\d+Z$/, "Z");
  const last = new Date(Date.now() - 60e3).toISOString().replace(/\.\d+Z$/, "Z");
  const code = "import sys, json; from pathlib import Path; from cockpit import db\n"
    + "c = db.connect(Path(sys.argv[1]))\nwith db.transaction(c): db.kv_set(c, 'browser_zones', json.loads(sys.argv[2]))";
  const done = spawnSync(python, ["-c", code, path.join(work, "cockpit.db"),
    JSON.stringify({ "Asia/Tokyo": { first, last, kept: false } })], { encoding: "utf8" });
  assert.equal(done.status, 0, done.stderr);
}

(async () => {
  let browser;
  try {
    const file = path.join(work, "cookie.json");
    for (let n = 0; !fs.existsSync(file) && n < 600; n++) { if (server.exitCode !== null) throw Error("fixture exited"); await pause(250); }
    const info = JSON.parse(fs.readFileSync(file, "utf8")), cookie = `${info.name}=${info.value}`;
    // cookie.json comes before the server listens.
    for (let n = 0; n < 240; n++) { try { await fetch(info.origin); break; } catch { await pause(250); } }
    const set = await fetch(`${info.origin}/api/timezone`, { method: "PUT", headers: { cookie, origin: info.origin, "content-type": "application/json" }, body: JSON.stringify({ zone: "Asia/Jakarta" }) });
    assert(set.ok, await set.text());
    browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE });
    for (const [width, scheme] of [[1280, "light"], [390, "dark"]]) {
      seedTrip();
      const context = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: scheme, timezoneId: "Asia/Tokyo", extraHTTPHeaders: { cookie } });
      const page = await context.newPage(), errors = [];
      page.on("pageerror", (e) => errors.push(e.message));
      await page.addLocatorHandler(page.locator("dialog#idle[open]"), (d) => d.locator("button", { hasText: "Return" }).click());
      await page.goto(info.origin);
      const card = page.locator(".zone-ask");
      await card.waitFor();
      assert.match(await card.innerText(), /Your browser has shown Tokyo time for 7 hours/);
      assert.match(await card.innerText(), /in Jakarta/);
      await page.screenshot({ path: path.join(work, `zone-ask-${width}-${scheme}.png`) });
      if (width === 1280) {
        await card.locator("button", { hasText: "Keep Jakarta time" }).click();
        await card.waitFor({ state: "detached" });
        await page.locator(".toast", { hasText: "Cockpit stays on Jakarta time" }).waitFor();
        // The device moves to Paris while the page stays open: the next report and the picker follow it.
        await page.evaluate(() => {
          const real = Intl.DateTimeFormat.prototype.resolvedOptions;
          Intl.DateTimeFormat.prototype.resolvedOptions = function () { return { ...real.call(this), timeZone: "Europe/Paris" }; };
        });
        const reported = page.waitForRequest((r) => r.method() === "POST" && r.url().endsWith("/api/timezone/report"));
        await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
        assert.equal(JSON.parse((await reported).postData()).zone, "Europe/Paris");
        const menu = page.locator(".brand-menu");
        await menu.locator(":scope > summary").click();
        await menu.locator(".zone-setting").click();
        const dialog = page.locator("#zone-dialog[open]");
        await dialog.locator(".zone-own", { hasText: "Use this device's zone: Paris" }).waitFor();
        await dialog.locator("button", { hasText: "Cancel" }).click();
      } else {
        // On a phone the brand menu sits in the list behind "Your items".
        await page.locator(".phone-back").click();
        const menu = page.locator(".brand-menu");
        await menu.locator(":scope > summary").click();
        const entry = menu.locator(".zone-setting");
        assert.match(await entry.innerText(), /Time zone\s*Jakarta/);
        await entry.click();
        const dialog = page.locator("#zone-dialog[open]");
        await dialog.locator(".zone-own", { hasText: "Use this device's zone: Tokyo" }).click();
        assert.equal(await dialog.locator("select").inputValue(), "Asia/Tokyo");
        await page.screenshot({ path: path.join(work, `zone-dialog-${width}-${scheme}.png`) });
        await dialog.locator("button", { hasText: "Save" }).click();
        await page.locator(".toast", { hasText: "Cockpit now runs on Tokyo time" }).waitFor();
        await card.waitFor({ state: "detached" });
        assert.match(fs.readFileSync(path.join(work, "setup.toml"), "utf8"), /^timezone = "Asia\/Tokyo"$/m);
      }
      assert.deepEqual(errors, []);
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "page overflows");
      await context.close();
    }
    console.log("Time zone question and picker passed at desktop light and phone dark. Screenshots:", work);
  } finally { if (browser) await browser.close(); server.kill(); }
})().catch((e) => { console.error(e); process.exitCode = 1; });
