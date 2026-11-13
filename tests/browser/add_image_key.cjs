// The Add dialog's image key (SPEC CONTROL-8): a key in the dialog's foot opens the browser's file picker, and the
// images picked wait beside the text like pasted ones, then go onto the new item. Checks, at phone and desktop width:
// - the key shows in the foot, named for what it does;
// - two images picked through it show as thumbnails in the dialog, and focus stays on the key;
// - Add makes the item with both images on it;
// - no page errors.
// Saves a screenshot of the dialog per width in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
// A run takes about half a minute.
//
// Usage, from the checkout: node tests/browser/add_image_key.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");
const zlib = require("node:zlib");

// Browser dependencies can be selected through the environment (README.md).
const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const ROOT = path.resolve(__dirname, "..", "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-add-image-");
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
      await page.goto(`${info.origin}/classic`);
      // The first drawing can take a while on a busy host.
      await page.locator(".layout > .primary").waitFor({ timeout: 30e3 });
      await page.locator("#add").click({ timeout: 30e3 });
      const area = page.locator('#add-box textarea[data-key="add:new"]');
      await area.waitFor();

      const key = page.locator("#add-box .add-foot button[aria-label^='Add images from your files']");
      check(`${width} px: the image key shows in the dialog's foot`, await key.isVisible(), "not shown");
      const title = `Image key test ${width} ${Date.now()}`;
      await area.fill(title);
      const [chooser] = await Promise.all([page.waitForEvent("filechooser"), key.click()]);
      check(`${width} px: the key opens a picker for several images`, chooser.isMultiple(), "one file only");
      await chooser.setFiles([
        { name: "first.png", mimeType: "image/png", buffer: png(200, 90) },
        { name: "second.png", mimeType: "image/png", buffer: png(120, 90) },
      ]);
      const staged = await waitFor(() => page.locator("#add-box .add-inner .thumbs img").count(), (count) => count === 2);
      check(`${width} px: both picked images wait in the dialog`, staged === 2, `${staged} shown`);
      check(`${width} px: focus stays on the key`,
        await page.evaluate(() => (document.activeElement.getAttribute("aria-label") || "").startsWith("Add images")), "elsewhere");
      await sleep(250);
      const shot = path.join(work, `add-image-${width}.png`);
      await page.locator("#add-box .add-panel").screenshot({ path: shot, timeout: 30e3 });
      console.error(`  screenshot: ${shot}`);

      await page.locator("#add-box .add-foot button.primary").click();
      const images = await waitFor(async () => {
        const state = await user("GET", "/api/state");
        const item = state.items.find((one) => one.title === title.split("\n")[0] || one.summary === title);
        return item ? item.images.length : -1;
      }, (count) => count === 2, 20e3);
      check(`${width} px: Add makes the item with both images`, images === 2, `${images} images (-1: no item)`);
      check(`${width} px: no page errors`, errors.length === 0, errors.join(" | "));
      await context.close();
    }
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

// A PNG of a blue-to-green gradient.
function png(width, height) {
  const rows = [];
  for (let y = 0; y < height; y += 1) {
    const row = Buffer.alloc(1 + width * 3);
    for (let x = 0; x < width; x += 1) row.set([40, Math.round((x / width) * 200), 220], 1 + x * 3);
    rows.push(row);
  }
  const chunk = (type, data) => {
    const length = Buffer.alloc(4);
    length.writeUInt32BE(data.length);
    const body = Buffer.concat([Buffer.from(type), data]);
    const crc = Buffer.alloc(4);
    crc.writeUInt32BE(zlib.crc32(body));
    return Buffer.concat([length, body, crc]);
  };
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width, 0);
  header.writeUInt32BE(height, 4);
  header.set([8, 2, 0, 0, 0], 8);
  return Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), chunk("IHDR", header),
    chunk("IDAT", zlib.deflateSync(Buffer.concat(rows))), chunk("IEND", Buffer.alloc(0))]);
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
