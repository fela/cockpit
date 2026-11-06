// The picked row in the list at the left of / stands out in the accent's family (SPEC UI-58): a deep burnt amber with
// light text on the light page, a light amber with dark text on the dark one. Checks, at desktop width, in both
// schemes:
// - the row picked when the page opens takes the --picked-bg ground and --picked-text words, and the ground is neither
//   the page's text colour nor its plain grey;
// - another item picked from the list has the same, and the other rows go back to a clear ground;
// - the picked row's age keeps at least 4.5:1 contrast on that ground, and its words at least 7:1;
// - an urgent title's red and the unsent-draft pencil keep at least 4.5:1 on it;
// - a just-added row (it pulses at the top of Busy for half an hour) keeps the picked colours when picked;
// - no page errors.
// Screenshots of the list in each scheme go to the work folder.
//
// Starts serve_preview.py (this checkout's Cockpit on a copy of the live database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/picked_row.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;
const ROOT = path.resolve(__dirname, "..", "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");

const { chromium } = require(PLAYWRIGHT);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-picked-row-");
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
    for (const scheme of ["light", "dark"]) {
      const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: scheme });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      // The fixture can open the "Back after" dialog over the page while the user's timer is idle; answer it
      // with Return whenever it shows, so it never takes the test's clicks.
      await page.addLocatorHandler(page.locator("dialog#idle[open]"), (dialog) => dialog.locator("button", { hasText: "Return" }).click()
        // The test may close its page while an answer is under way.
        .catch((error) => { if (!/has been closed/.test(error.message)) throw error; }));
      page.setDefaultTimeout(30e3);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(`${info.origin}/`);
      await page.locator(".sidebar-list li[data-item] > .side-row").first().waitFor();
      await sleep(500);

      const colours = () => page.evaluate(() => {
        // Each colour as sRGB through a canvas, so oklch() values compare and give a contrast ratio.
        const canvas = document.createElement("canvas");
        canvas.width = canvas.height = 1;
        const ctx = canvas.getContext("2d", { willReadFrequently: true });
        const rgb = (colour) => {
          ctx.clearRect(0, 0, 1, 1);
          ctx.fillStyle = "#000";
          ctx.fillStyle = colour;
          ctx.fillRect(0, 0, 1, 1);
          const [r, g, b, a] = ctx.getImageData(0, 0, 1, 1).data;
          return { r, g, b, a };
        };
        const lum = ({ r, g, b }) => [r, g, b].map((v) => v / 255).map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4))
          .reduce((sum, v, i) => sum + v * [0.2126, 0.7152, 0.0722][i], 0);
        const ratio = (x, y) => { const [hi, lo] = [lum(x), lum(y)].sort((p, q) => q - p); return (hi + 0.05) / (lo + 0.05); };
        const root = getComputedStyle(document.documentElement);
        const pickedBg = rgb(root.getPropertyValue("--picked-bg").trim());
        const pickedText = rgb(root.getPropertyValue("--picked-text").trim());
        const pageText = rgb(root.getPropertyValue("--text").trim());
        const same = (x, y) => Math.abs(x.r - y.r) + Math.abs(x.g - y.g) + Math.abs(x.b - y.b) <= 6;
        const of = (el) => {
          const style = getComputedStyle(el);
          const bg = rgb(style.backgroundColor);
          return { bg, text: rgb(style.color), clear: bg.a === 0 };
        };
        const picked = document.querySelector(".sidebar-list li.sel:not(.warn-row) > .side-row");
        const pickedOf = picked && of(picked);
        const age = picked && picked.querySelector(".sidebar-age");
        // A to-do added in the last half hour pulses at the top of Busy (CONTROL-18), so it is not counted here.
        const others = [...document.querySelectorAll(".sidebar-list li[data-item]:not(.sel, .fresh) > .side-row")].map(of);
        return {
          warm: !same(pickedBg, pageText) && pickedBg.r - pickedBg.b > 40,
          picked: Boolean(picked),
          pickedReversed: Boolean(pickedOf) && same(pickedOf.bg, pickedBg) && same(pickedOf.text, pickedText),
          textRatio: pickedOf ? ratio(pickedOf.bg, pickedOf.text) : 0,
          ageRatio: pickedOf && age ? ratio(pickedOf.bg, rgb(getComputedStyle(age).color)) : 0,
          othersClear: others.every((row) => row.clear),
          // An urgent title's red and the unsent-draft pencil on the picked ground (tokens, so no urgent row is needed).
          failRatio: ratio(pickedBg, rgb(root.getPropertyValue("--picked-fail").trim())),
          pencilRatio: ratio(pickedBg, rgb(root.getPropertyValue("--picked-accent").trim())),
        };
      });

      const start = await colours();
      check(`${scheme}: the row picked at the start is in the picked colours, a warm amber`, start.picked && start.pickedReversed && start.warm, JSON.stringify(start));
      await page.locator(".sidebar").screenshot({ path: path.join(work, `picked-row-${scheme}-next-up.png`) });

      // A row not picked yet, so the pick is a change from what the page opened on.
      const row = page.locator(".sidebar-list li[data-item]:not(.sel) > .side-row").nth(1);
      await row.click();
      await sleep(500);
      const after = await colours();
      check(`${scheme}: the picked item's row is in the picked colours`, after.picked && after.pickedReversed, JSON.stringify(after));
      check(`${scheme}: the other rows go back to a clear ground`, after.othersClear, JSON.stringify(after));
      check(`${scheme}: the picked row's words reach 7:1 and its age 4.5:1`, after.textRatio >= 7 && after.ageRatio >= 4.5,
        `words ${after.textRatio.toFixed(2)}, age ${after.ageRatio.toFixed(2)}`);
      check(`${scheme}: an urgent title's red and the pencil reach 4.5:1 on the picked row`, after.failRatio >= 4.5 && after.pencilRatio >= 4.5,
        `red ${after.failRatio.toFixed(2)}, pencil ${after.pencilRatio.toFixed(2)}`);
      await page.locator(".sidebar").screenshot({ path: path.join(work, `picked-row-${scheme}-item.png`) });
      // A to-do just added pulses at the top of Busy for half an hour; picked during that time, its row keeps the picked colours.
      await page.evaluate(() => document.querySelector(".sidebar-list li.sel[data-item]").classList.add("fresh"));
      await sleep(100);
      const arrived = await colours();
      check(`${scheme}: a just-added row, picked, keeps the picked colours`, arrived.pickedReversed && arrived.textRatio >= 7, JSON.stringify(arrived));
      check(`${scheme}: no page errors`, errors.length === 0, errors.join(" | "));
      await context.close();
    }
    console.error(`  screenshots in ${work}`);
  } finally {
    if (browser) await browser.close();
    server.kill("SIGTERM");
  }
  const failed = results.filter((result) => !result.ok);
  console.error(failed.length ? `${failed.length} of ${results.length} checks failed` : `all ${results.length} checks passed`);
  process.exitCode = failed.length ? 1 : 0;
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
