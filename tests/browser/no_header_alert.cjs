// The header shows no failure alert (UI-26).
// Checks, on the classic page and the page with the rail, at 1440, 1280, 1100, 1001 and 390 px:
// - the header holds no alert count;
// - the header fits its width (on the page with the rail, the rail holds its controls) and the page has no sideways
//   scroll;
// - the page draws without errors.
//
// Starts serve_preview.py (this checkout's Cockpit on a copy of the live database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
// Screenshots go to the folder given as the first argument, when one is given.
//
// Usage, from the checkout: node tests/browser/no_header_alert.cjs [screenshot folder]

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
  const shots = process.argv[2] || null;
  if (shots) fs.mkdirSync(shots, { recursive: true });
  const work = workFolder("cockpit-no-header-alert-");
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const info = await harnessUp(work, server);
    browser = await chromium.launch({ executablePath: CHROMIUM });
    for (const where of ["/classic", "/"]) {
      for (const width of [1440, 1280, 1100, 1001, 390]) {
        const name = `${where} at ${width}`;
        const context = await browser.newContext({ viewport: { width, height: 900 }, isMobile: width < 600, hasTouch: width < 600 });
        await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
        const page = await context.newPage();
        // Counts the page's redraws, so the checks run on a header drawn from the state.
        await page.addInitScript(() => {
          window.redraws = 0;
          document.addEventListener("cockpit:redraw", () => { window.redraws += 1; });
        });
        page.setDefaultTimeout(10000);
        const errors = [];
        page.on("pageerror", (error) => errors.push(error.message));
        page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
        await page.goto(`${info.origin}${where}`);
        await page.waitForFunction(() => window.redraws > 0);
        await page.evaluate(() => document.fonts.ready);
        // On a phone the rail is the list screen, which the bar's "Your items" opens (UI-56).
        const phoneBack = page.locator(".phone-back");
        if (await phoneBack.isVisible()) await phoneBack.click();
        // The header fits itself on the next frame after a redraw.
        await sleep(500);
        const seen = await page.evaluate(() => {
          const shown = (el) => el && el.offsetParent !== null && getComputedStyle(el).display !== "none";
          const rail = document.querySelector(".rail");
          const box = shown(rail) ? rail : document.querySelector(".top-inner");
          if (!shown(box)) return { box: null };
          // How far the line's content or any control in it reaches past its edges.
          const edge = box.getBoundingClientRect();
          let over = box.scrollWidth - box.clientWidth;
          for (const el of box.querySelectorAll("*")) {
            const r = el.getBoundingClientRect();
            if (r.width && r.height) over = Math.max(over, r.right - edge.right, edge.left - r.left);
          }
          return {
            box: box.className,
            counts: document.querySelectorAll("#counts, .count.fail").length,
            over: Math.round(over),
            sideways: document.documentElement.scrollWidth - innerWidth,
          };
        });
        check(`${name}: the header is drawn`, seen.box !== null, "neither the rail nor the header line shows");
        check(`${name}: the header holds no alert count`, seen.counts === 0, `${seen.counts} found`);
        check(`${name}: the ${seen.box} fits its width`, seen.over <= 0, `${seen.over}px too wide`);
        check(`${name}: the page has no sideways scroll`, seen.sideways <= 0, `${seen.sideways}px too wide`);
        check(`${name}: no page errors`, errors.length === 0, errors.join(" | "));
        if (shots) await page.screenshot({ path: path.join(shots, `${where === "/" ? "rail" : "classic"}-${width}.png`) });
        await context.close();
      }
    }
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failedChecks = results.filter((result) => !result.ok);
  console.error(failedChecks.length ? `${failedChecks.length} of ${results.length} checks failed` : `all ${results.length} checks passed`);
  process.exitCode = failedChecks.length ? 1 : 0;
}

// The harness's cookie file, once it is written and the server answers.
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
