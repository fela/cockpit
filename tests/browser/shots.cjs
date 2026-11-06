// Screenshots of a Cockpit served by serve_preview.py, for working on the look.
//
// Usage, from the checkout:
//   node tests/browser/shots.cjs <work-dir> <out-dir> [options]
// Options:
//   --path /spec          page to open (default /)
//   --width 1440          viewport width (default 1440); --height 900 (default 900)
//   --scheme dark|light   prefers-color-scheme (default light)
//   --full                capture the whole page, not only the viewport
//   --name board          file name stem (default derived from the options)
//   --wait 1200           ms to wait after load, for fonts and entry animations (default 1200)
//   --eval "js"           run this in the page before the shot (e.g. open a step, hover a row)
//   --clip "selector"     capture only this element
//   --scale 2             device scale factor (default 1; use 2 to check the marks up close)
// Prints the path of each file it writes.

"use strict";

const fs = require("node:fs");
const path = require("node:path");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;
const { chromium } = require(PLAYWRIGHT);

function parse(argv) {
  const [work, out, ...rest] = argv;
  if (!work || !out) {
    console.error("usage: node tests/browser/shots.cjs <work-dir> <out-dir> [--path /] [--width 1440] [--scheme light] [--full] [--eval js] [--clip sel]");
    process.exit(2);
  }
  const opts = { work, out, path: "/", width: 1440, height: 900, scheme: "light", full: false, wait: 1200, scale: 1 };
  for (let i = 0; i < rest.length; i++) {
    const key = rest[i].replace(/^--/, "");
    if (key === "full") opts.full = true;
    else opts[key] = rest[++i];
  }
  opts.width = Number(opts.width);
  opts.height = Number(opts.height);
  opts.wait = Number(opts.wait);
  opts.scale = Number(opts.scale);
  return opts;
}

// The file name stem: --name as given, else derived from the options.
function stem(opts) {
  if (opts.name) return opts.name;
  const page = opts.path === "/" ? "board" : opts.path.replace(/[^a-z0-9]+/gi, "-").replace(/^-|-$/g, "");
  return `${page}-${opts.width}-${opts.scheme}${opts.full ? "-full" : ""}`;
}

(async () => {
  const opts = parse(process.argv.slice(2));
  const cookie = JSON.parse(fs.readFileSync(path.join(opts.work, "cookie.json"), "utf8"));
  fs.mkdirSync(opts.out, { recursive: true });
  const browser = await chromium.launch({ executablePath: CHROMIUM });
  await shoot(browser, opts, cookie);
  await browser.close();
})().catch((err) => {
  console.error(err);
  process.exit(1);
});

async function shoot(browser, opts, cookie) {
  const context = await browser.newContext({
    viewport: { width: opts.width, height: opts.height },
    deviceScaleFactor: opts.scale,
    colorScheme: opts.scheme,
    isMobile: opts.width < 600,
    hasTouch: opts.width < 600,
  });
  await context.setExtraHTTPHeaders({ cookie: `${cookie.name}=${cookie.value}` });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (err) => errors.push(String(err)));
  page.on("console", (msg) => { if (msg.type() === "error") errors.push(msg.text()); });
  await page.goto(cookie.origin + opts.path, { waitUntil: "networkidle" });
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(opts.wait);
  if (opts.eval) {
    await page.evaluate(opts.eval);
    await page.waitForTimeout(600);
  }
  const file = path.join(opts.out, `${stem(opts)}.png`);
  if (opts.clip) {
    await page.locator(opts.clip).first().screenshot({ path: file });
  } else {
    await page.screenshot({ path: file, fullPage: opts.full });
  }
  console.log(file);
  if (errors.length) console.log("page errors:\n  " + errors.join("\n  "));
  await context.close();
}
