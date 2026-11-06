// Feedback on the page at /: the crosshair key before + at the rail's top does what + does, with a part
// of the page picked first. Checks, at phone and desktop width:
// - the rail's top line holds Feedback, then +, as two keys of the same size, Feedback drawn as the crosshair;
// - pressing Feedback starts picking and keeps the Add dialog closed, Feedback lit;
// - a click on a part opens the Add dialog with that part attached;
// - Add makes the item, and its summary carries the part;
// - Escape before a pick leaves no Add dialog open, and the F key does the same as the key;
// - + clicked and closed leaves no focus ring on it; opened with the A key, focus returns to +;
// - no page errors.
// Saves a screenshot of the rail's top and of the dialog per width in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what
// it needs) and drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/feedback_add.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

// Browser dependencies can be selected through the environment (README.md).
const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const ROOT = path.resolve(__dirname, "..", "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-feedback-add-");
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const info = await harnessUp(work, server);
    const cookie = { cookie: `${info.name}=${info.value}`, origin: info.origin };
    const user = (method, url) => call(info.origin, cookie, method, url);
    browser = await chromium.launch({ executablePath: CHROMIUM });
    for (const [width, height] of [[390, 844], [1280, 900]]) {
      const context = await browser.newContext({ viewport: { width, height } });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      page.setDefaultTimeout(5000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(`${info.origin}/`);
      await page.locator(".rail-actions #feedback-mode").waitFor({ state: "attached", timeout: 30e3 });
      // On a phone the rail is the list screen, apart from the item's.
      if (width < 640) await page.evaluate(() => setPhoneScreen(true, false));
      await page.locator("#sidebar .side-row").first().waitFor({ timeout: 30e3 });

      const keys = await page.evaluate(() => [...document.querySelector(".rail-actions").children].map((el) => {
        const box = el.getBoundingClientRect();
        return { id: el.id || el.className, w: Math.round(box.width), h: Math.round(box.height), icon: el.querySelector("svg")?.getAttribute("class") };
      }));
      check(`${width} px: Feedback comes first, then +`, keys[0]?.id === "feedback-mode" && keys[1]?.id.includes("sidebar-add"), JSON.stringify(keys));
      check(`${width} px: Feedback is drawn as the crosshair`, keys[0]?.icon === "icon icon-pick", JSON.stringify(keys[0]));
      check(`${width} px: Feedback and + are the same size`, keys[0]?.w === keys[1]?.w && keys[0]?.h === keys[1]?.h && keys[0]?.w > 0, JSON.stringify(keys));
      await page.locator(".rail-top").screenshot({ path: path.join(work, `rail-top-${width}.png`) });

      // Escape before a pick leaves nothing open.
      await page.locator("#feedback-mode").click();
      const picking = await page.evaluate(() => ({
        picking: ui.picking, bar: !document.getElementById("pick-bar").hidden, add: !document.getElementById("add-box").hidden,
        lit: document.getElementById("feedback-mode").classList.contains("on"), words: document.getElementById("pick-bar").innerText,
      }));
      check(`${width} px: Feedback starts picking with the Add dialog closed`,
        picking.picking === "add" && picking.bar && !picking.add && picking.lit && picking.words.includes("your to-do is about"), JSON.stringify(picking));
      await page.keyboard.press("Escape");
      await sleep(200);
      const after = await page.evaluate(() => ({ picking: ui.picking, adding: ui.adding, add: !document.getElementById("add-box").hidden }));
      check(`${width} px: Escape before a pick leaves no dialog open`, !after.picking && !after.adding && !after.add, JSON.stringify(after));

      // The F key does the same; a pick opens Add with the part attached.
      await page.evaluate(() => document.activeElement && document.activeElement.blur());
      await page.keyboard.press("f");
      check(`${width} px: the F key starts the same pick`, await page.evaluate(() => ui.picking === "add" && ui.adding), "not picking");
      const row = page.locator("#sidebar .side-row").first();
      const rowText = (await row.innerText()).split("\n")[0].trim();
      await row.click();
      const area = page.locator('#add-box textarea[data-key="add:new"]');
      await area.waitFor();
      const attached = await page.evaluate(() => ({ part: document.querySelector("#add-box .add-part")?.innerText || "", picking: ui.picking }));
      check(`${width} px: picking a part opens Add with the part attached`, attached.part.includes("Attached") && !attached.picking, JSON.stringify(attached));
      await sleep(250);
      await page.locator("#add-box .add-panel").screenshot({ path: path.join(work, `add-with-part-${width}.png`), timeout: 30e3 });

      const title = `Feedback key test ${width} ${Date.now()}`;
      await area.fill(title);
      await page.locator("#add-box .add-foot button.primary").click();
      const summary = await waitFor(async () => {
        const state = await user("GET", "/api/state");
        const item = state.items.find((one) => one.title === title);
        return item ? item.summary : null;
      }, (value) => value !== null, 20e3);
      check(`${width} px: Add makes the item with the part in its summary`, Boolean(summary) && summary.includes("Attached part of the page"),
        `${summary} (row ${rowText})`);

      // + opened with a click and closed with Escape leaves no ring on +; opened with the A key, the
      // focus goes back to it.
      if (width < 640) await page.evaluate(() => setPhoneScreen(true, false));
      await page.locator(".rail-actions .sidebar-add").click();
      await page.locator('#add-box textarea[data-key="add:new"]').waitFor();
      await page.keyboard.press("Escape");
      await sleep(200);
      const ring = await page.evaluate(() => ({
        open: !document.getElementById("add-box").hidden,
        ringed: [...document.querySelectorAll(".rail-actions > *")].filter((el) => el.matches(":focus-visible")).map((el) => el.id || el.className),
      }));
      check(`${width} px: + clicked, then Escape, leaves no ring on the rail's keys`, !ring.open && ring.ringed.length === 0, JSON.stringify(ring));
      await page.keyboard.press("a");
      await page.locator('#add-box textarea[data-key="add:new"]').waitFor();
      await page.keyboard.press("Escape");
      await sleep(200);
      check(`${width} px: opened with A, Escape gives + the focus back`,
        await page.evaluate(() => document.activeElement?.classList.contains("sidebar-add")), await page.evaluate(() => document.activeElement?.outerHTML.slice(0, 80)));
      check(`${width} px: no page errors`, errors.length === 0, errors.join(" | "));
      await context.close();
    }
    console.error(`  screenshots in ${work}`);
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failedChecks = results.filter((result) => !result.ok);
  console.error(failedChecks.length ? `${failedChecks.length} of ${results.length} checks failed` : `all ${results.length} checks passed`);
  process.exitCode = failedChecks.length ? 1 : 0;
}

// The value `read` gives once `ok` holds it, or its last value after `ms` (5 s unless given).
async function waitFor(read, ok, ms = 5000) {
  const end = Date.now() + ms;
  let value = await read();
  while (!ok(value) && Date.now() < end) {
    await sleep(100);
    value = await read();
  }
  return value;
}

async function call(origin, headers, method, url, body) {
  const response = await fetch(`${origin}${url}`, { method, headers, body });
  const text = await response.text();
  if (!response.ok) throw new Error(`${method} ${url}: ${response.status} ${text}`);
  return text ? JSON.parse(text) : null;
}

async function harnessUp(work, server) {
  const file = path.join(work, "cookie.json");
  const end = Date.now() + 60e3;
  while (Date.now() < end) {
    if (server.exitCode !== null) throw new Error(`serve_preview.py exited with ${server.exitCode}`);
    if (fs.existsSync(file)) {
      const info = JSON.parse(fs.readFileSync(file, "utf8"));
      const up = await fetch(`${info.origin}/healthz`).then((response) => response.ok, () => false);
      if (up) return info;
    }
    await sleep(200);
  }
  throw new Error("serve_preview.py did not come up within 60 s");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
