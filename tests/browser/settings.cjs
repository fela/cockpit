// The Settings page (SPEC CONTROL-35), with the Settings page and the weekly digest on (extra-setup.toml in the work
// folder). It checks:
// - the logo menu on the main page holds one Settings entry, above Vim editing, and a click on it opens the page in
//   the layer over the main page;
// - the page shows the briefing's hour and the digest's day and hour at their defaults;
// - two changes made while the first save is slow end on the second: the page and Cockpit both hold it, and the page
//   says Saved;
// - No digest turns the digest's hour off, and Cockpit holds no digest day;
// - a refused save puts back what Cockpit holds and says why;
// - no page errors and no Content Security Policy errors.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) and drives
// headless Chromium at desktop width in light and phone width. Prints one line per check, saves screenshots in
// SHOTS_DIR (default: a temporary directory) and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/settings.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;
const ROOT = path.resolve(__dirname, "..", "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
const SHOTS = process.env.SHOTS_DIR || fs.mkdtempSync(path.join(os.tmpdir(), "cockpit-settings-shots-"));

const { chromium } = require(PLAYWRIGHT);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-settings-");
  fs.writeFileSync(path.join(work, "extra-setup.toml"), 'features = ["weekly_digest", "settings_page"]\n');
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "ignore", "inherit"] });
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const info = await harnessUp(work, server);
    const cookie = { cookie: `${info.name}=${info.value}`, origin: info.origin, "content-type": "application/json" };
    const user = (method, url, body) => call(info.origin, cookie, method, url, body);
    browser = await chromium.launch({ executablePath: CHROMIUM });
    const host = new URL(info.origin).hostname;
    const errors = [];
    const open = async (width, height) => {
      const context = await browser.newContext({ viewport: { width, height }, colorScheme: "light" });
      await context.addCookies([{ name: info.name, value: info.value, domain: host, path: "/", secure: true, httpOnly: true, sameSite: "Lax" }]);
      const page = await context.newPage();
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("console", (message) => {
        if (message.type() === "error" && /Content Security Policy/.test(message.text())) errors.push(message.text());
      });
      return page;
    };

    // The entry in the logo menu, and the page over the main page.
    const main = await open(1280, 800);
    await main.goto(`${info.origin}/`);
    await main.locator(".brand-menu summary").click();
    const entries = await main.locator('.brand-menu .menu-list a[href="/settings"]').count();
    const order = await main.locator(".brand-menu .menu-list > *").evaluateAll((items) => items.map((item) => item.textContent.trim()));
    check("the logo menu holds one Settings entry, above Vim editing", entries === 1
      && order.indexOf("Settings") >= 0 && order.indexOf("Settings") + 1 === order.findIndex((text) => /Vim/.test(text)), JSON.stringify(order));
    await main.screenshot({ path: path.join(SHOTS, "settings-menu.png") });
    await main.locator('.brand-menu .menu-list a[href="/settings"]').click();
    const frame = main.locator(".center-layer iframe");
    await frame.waitFor({ timeout: 10e3 });
    await main.frameLocator(".center-layer iframe").locator('select[aria-label="Morning briefing hour"]').waitFor({ timeout: 10e3 });
    check("the entry opens the page in the layer over the main page", new URL(main.url()).pathname === "/"
      && /\/settings/.test(await frame.getAttribute("src")), main.url());
    await main.close();

    // The page at desktop width, its defaults, and two quick changes while the first save is slow.
    const page = await open(1280, 800);
    let puts = 0;
    await page.route("**/api/settings", async (route) => {
      if (route.request().method() === "PUT" && ++puts === 1) await sleep(1500);
      await route.continue();
    });
    await page.goto(`${info.origin}/settings`);
    const briefing = page.locator('select[aria-label="Morning briefing hour"]');
    const day = page.locator('select[aria-label="Weekly digest day"]');
    const hour = page.locator('select[aria-label="Weekly digest hour"]');
    await briefing.waitFor();
    const shown = [await briefing.inputValue(), await day.inputValue(), await hour.inputValue()];
    check("the page shows the default schedule", JSON.stringify(shown) === JSON.stringify(["7", "6", "8"]), JSON.stringify(shown));
    await briefing.selectOption("9");
    await sleep(200);
    await briefing.selectOption("10");
    const said = page.locator(".settings-said");
    await page.waitForFunction(() => document.querySelector(".settings-said")?.textContent === "Saved", null, { timeout: 10e3 });
    const held = (await user("GET", "/api/settings")).schedule;
    check("two quick changes end on the second, on the page and in Cockpit", await briefing.inputValue() === "10"
      && held.briefing_hour === 10 && puts === 2, `page ${await briefing.inputValue()}, Cockpit ${held.briefing_hour}, ${puts} saves`);

    await day.selectOption("");
    await page.waitForFunction(() => document.querySelector(".settings-said")?.textContent === "Saved", null, { timeout: 10e3 });
    const off = (await user("GET", "/api/settings")).schedule;
    check("No digest turns the hour off and Cockpit holds no digest day", await hour.isDisabled() && off.digest_day === null, JSON.stringify(off));
    await day.selectOption("0");
    await hour.selectOption("9");
    await page.waitForFunction(() => document.querySelector(".settings-said")?.textContent === "Saved", null, { timeout: 10e3 });

    // A refused save: the page goes back to what Cockpit holds and says why.
    await page.route("**/api/settings", (route) => route.request().method() === "PUT"
      ? route.fulfill({ status: 400, contentType: "application/json", body: JSON.stringify({ detail: "refused for the check" }) })
      : route.continue());
    await briefing.selectOption("11");
    await page.waitForFunction(() => /^Not saved/.test(document.querySelector(".settings-said")?.textContent || ""), null, { timeout: 10e3 });
    check("a refused save puts back what Cockpit holds and says why", await briefing.inputValue() === "10"
      && /refused for the check/.test(await said.textContent()), `${await briefing.inputValue()} ${await said.textContent()}`);
    await page.unroute("**/api/settings");
    await page.reload();
    await briefing.waitFor();
    await page.screenshot({ path: path.join(SHOTS, "settings-desktop-light.png") });
    await page.close();

    const phone = await open(390, 844);
    await phone.goto(`${info.origin}/settings`);
    await phone.locator('select[aria-label="Morning briefing hour"]').waitFor();
    const wide = await phone.evaluate(() => document.documentElement.scrollWidth);
    check("the page fits a phone's width", wide <= 390, `${wide}px`);
    await phone.screenshot({ path: path.join(SHOTS, "settings-phone.png") });
    await phone.close();

    check("no page errors and no Content Security Policy errors", errors.length === 0, errors.join(" | "));
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  console.error(`screenshots in ${SHOTS}`);
  if (results.some((result) => !result.ok)) process.exitCode = 1;
}

async function call(origin, headers, method, url, body) {
  const response = await fetch(`${origin}${url}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await response.text();
  if (!response.ok) throw new Error(`${method} ${url}: ${response.status} ${text}`);
  return text ? JSON.parse(text) : null;
}

async function harnessUp(work, server) {
  const file = path.join(work, "cookie.json");
  const end = Date.now() + 120e3;
  while (Date.now() < end) {
    if (server.exitCode !== null) throw new Error(`serve_preview.py exited with ${server.exitCode}`);
    if (fs.existsSync(file)) {
      const info = JSON.parse(fs.readFileSync(file, "utf8"));
      const up = await fetch(`${info.origin}/healthz`).then((response) => response.ok, () => false);
      if (up) return info;
    }
    await sleep(200);
  }
  throw new Error("serve_preview.py did not come up within 120 s");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
