// A step card's images (SPEC NEXT-17): the card shows the images its text places, where the text places them; the
// user's own images stay in the strip under the body; the agents' other images on the item wait under a closed "Other
// images on this task". Checks, on the main page at phone and desktop width, for the step in Next up:
// - the image the body places shows in the body and not in the strip;
// - the user's image shows in the strip;
// - the two agent images the text does not place are under the closed section, which counts them, and show once it is
//   opened; it stays open through a redraw;
// - no page errors.
// Saves a screenshot of the step per width in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a copy of the live database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
// A run takes about half a minute.
//
// Usage, from the checkout: node tests/browser/step_images.cjs

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
// The harness's agent token and second fake session (tests/conftest.py).
const SESSION_B = "22222222-2222-4222-8222-222222222222";
const TOKEN = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "step-images-test" };

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-step-images-");
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
    const agent = (method, url, body) => call(info.origin, { ...TOKEN, "content-type": "application/json" }, method, url, JSON.stringify(body));
    const user = (method, url, body) => call(info.origin, { ...cookie, "content-type": "application/json" }, method, url,
      body === undefined ? undefined : JSON.stringify(body));

    // The fixture database's open steps are snoozed, so this run's step is Next up.
    const copied = await user("GET", "/api/state");
    await Promise.all([...copied.queue, ...copied.ready_next, ...copied.snoozed].map((step) => user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
    const run = Date.now();
    const key = `note:step-images-${run}`;
    const item = await agent("PUT", "/api/items", { key, title: "Step images test", priority: "urgent" });
    const addImage = (headers, width) => call(info.origin, { ...headers, "content-type": "image/png" }, "POST", `/api/items/${item.id}/images`, png(width, 90));
    // Two images from an earlier round, the one this step places, and one the user pasted.
    const older = [await addImage(TOKEN, 200), await addImage(TOKEN, 210)];
    const placed = await addImage(TOKEN, 240);
    const pasted = await addImage(cookie, 160);
    const answer = await agent("POST", "/api/steps", {
      item_key: key, need: `images-${run}`, ask: "Ship the new form?", score: 100, session_id: SESSION_B,
      body: `The export form now asks which lists to include.\n\n![The new form](image:${placed.id})\n\nSay yes and I merge it.`,
      details: [{ title: "The situation from scratch", body: "The form chooses which Tasks lists to export." }],
    });
    const stepId = answer.step.id;

    browser = await chromium.launch({ executablePath: CHROMIUM });
    for (const [width, height] of [[390, 844], [1280, 900]]) {
      const context = await browser.newContext({ viewport: { width, height } });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      page.setDefaultTimeout(5000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(`${info.origin}/classic`);
      const card = page.locator(`[data-step="${stepId}"]`).first();
      // The first drawing can take a while on a busy host.
      await card.waitFor({ timeout: 30e3 });
      const src = (image) => `/item-images/${image.id}`;
      const strip = card.locator(".thumbs").first();

      check(`${width} px: the image the body places shows in the body`,
        await card.locator(`.prose.body img.figure-image[src="${src(placed)}"]`).isVisible(), "not in the body");
      check(`${width} px: the strip leaves out the placed image`,
        (await card.locator(`.thumbs img[src="${src(placed)}"]`).count()) === 0, "shown twice");
      check(`${width} px: the user's image shows in the strip`,
        await strip.locator(`img[src="${src(pasted)}"]`).isVisible(), "not in the strip");
      const section = card.locator("details.detail", { hasText: "Other images on this task" });
      check(`${width} px: the older images wait under a closed section that counts them`,
        (await section.count()) === 1 && !(await section.evaluate((el) => el.open))
          && (await section.locator("summary").textContent()).includes("(2)")
          && !(await card.locator(`img[src="${src(older[0])}"]`).isVisible()) && !(await card.locator(`img[src="${src(older[1])}"]`).isVisible()),
        `${await section.count()} sections`);
      check(`${width} px: the section follows the step's details`,
        await card.evaluate((el) => {
          const folds = [...el.querySelectorAll("details.detail")].map((fold) => fold.querySelector("summary").textContent);
          return folds.indexOf("The situation from scratch") === folds.findIndex((words) => words.startsWith("Other images")) - 1;
        }), "out of order");

      await section.locator("summary").click();
      check(`${width} px: opened, it shows both older images`,
        await section.locator(`img[src="${src(older[0])}"]`).isVisible() && await section.locator(`img[src="${src(older[1])}"]`).isVisible(), "hidden");
      await card.locator(".prose.body").first().evaluate((el) => { el.dataset.old = "1"; });
      await page.evaluate(() => { ui.pendingRender = true; return refresh(); });
      await waitFor(() => card.locator(".prose.body").first().getAttribute("data-old"), (value) => value === null);
      check(`${width} px: it stays open through a redraw`,
        await card.locator("details.detail", { hasText: "Other images on this task" }).evaluate((el) => el.open), "closed");

      await sleep(300);
      const shot = path.join(work, `step-images-${width}.png`);
      await card.screenshot({ path: shot, timeout: 30e3 });
      console.error(`  screenshot: ${shot}`);
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
