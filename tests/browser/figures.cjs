// Figures in a step (SPEC NEXT-17): a step's body shows one of its item's shared pages in a sandboxed frame
// and one of its item images, each with its words under it. Checks, on the main page at phone and desktop
// width, for the step in Next up:
// - the frame takes the height its page reports, within 80 to 900 px, and keeps it through a redraw;
// - the page in the frame reaches neither Cockpit's page nor the cookie, and a height message from
//   anywhere but a figure's frame changes nothing;
// - a page whose body is as tall as its frame (min-height: 100vh) stays its content's height, and content that
//   grows inside a body of fixed height grows the frame;
// - the second of two updates to the page within a second shows in the frame at the next redraw;
// - the image shows in the body and opens full size on a click.
// Saves a screenshot of the step per width in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what
// it needs) and drives a headless Chromium. Prints one line per check and exits 1 when one failed.
// A run takes about half a minute.
//
// Usage, from the checkout: node tests/browser/figures.cjs

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
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "figures-test", "content-type": "application/json" };
// A chart 420 px high and one far taller than a figure may be.
const CHART = `<!doctype html><style>body{margin:8px;font:14px sans-serif}.bar{height:40px;margin:10px 0;background:#5b7cfa}</style>
<h3>Exports by week</h3>${[20, 40, 60, 80, 100].map((width) => `<div class="bar" style="width:${width * 2}px"></div>`).join("")}
<div style="height:100px"></div>`;
const TALL = '<!doctype html><div style="height:3000px">tall</div>';
const FULL = "<!doctype html><style>body{min-height:100vh}</style><p>One short line</p>";
const GROW = `<!doctype html><style>html,body{height:100%;margin:0}</style><div id="grow" style="height:30px"></div>
<script>setTimeout(() => { document.getElementById("grow").style.height = "240px"; }, 1500);</script>`;

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-figures-");
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
    const agent = (method, url, body) => call(info.origin, AGENT, method, url, JSON.stringify(body));
    const user = (method, url, body) => call(info.origin, { ...cookie, "content-type": "application/json" }, method, url,
      body === undefined ? undefined : JSON.stringify(body));

    // The fixture database's open steps are snoozed, so this run's step is Next up.
    const copied = await user("GET", "/api/state");
    await Promise.all([...copied.queue, ...copied.ready_next, ...copied.snoozed].map((step) => user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
    const run = Date.now();
    const key = `note:figures-${run}`;
    const item = await agent("PUT", "/api/items", { key, title: "Figures test", priority: "urgent" });
    const chart = await agent("POST", `/api/items/${item.id}/pages`, { title: "Exports by week", html: CHART });
    const tall = await agent("POST", `/api/items/${item.id}/pages`, { title: "Tall", html: TALL });
    const full = await agent("POST", `/api/items/${item.id}/pages`, { title: "Full height", html: FULL });
    const grow = await agent("POST", `/api/items/${item.id}/pages`, { title: "Growing", html: GROW });
    const image = await call(info.origin, { ...cookie, "content-type": "image/png" }, "POST", `/api/items/${item.id}/images`, png(240, 90));
    const answer = await agent("POST", "/api/steps", {
      item_key: key, need: `figures-${run}`, ask: "Ship the export change?", score: 100, session_id: SESSION_B,
      body: `Exports rose each week since the change.\n\n![Exports by week, last five weeks](page:${chart.id})\n\n`
        + `![The new form](image:${image.id})`,
      details: [{ title: "A tall page", body: `![Tall](page:${tall.id})\n\n![Full height](page:${full.id})\n\n![Growing](page:${grow.id})` }],
    });
    const stepId = answer.step.id;

    browser = await chromium.launch({ executablePath: CHROMIUM });
    for (const [width, height] of [[390, 844], [1280, 900]]) {
      const context = await browser.newContext({ viewport: { width, height }, colorScheme: "dark" });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      page.setDefaultTimeout(30000);
      page.setDefaultNavigationTimeout(60000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(`${info.origin}/classic`);
      const card = page.locator(`[data-step="${stepId}"]`).first();
      await card.waitFor();
      const frame = card.locator(`iframe.figure-frame[src^="/pages/${chart.id}/figure?"]`);
      await frame.scrollIntoViewIfNeeded();
      check(`${width} px: the frame is sandboxed without the same origin`,
        (await frame.getAttribute("sandbox")) === "allow-scripts allow-popups allow-popups-to-escape-sandbox", await frame.getAttribute("sandbox"));
      const fitted = await waitFor(() => frame.evaluate((el) => el.getBoundingClientRect().height), (value) => Math.abs(value - 420) <= 30);
      check(`${width} px: the frame takes its page's height`, Math.abs(fitted - 420) <= 30, `${fitted} px`);
      check(`${width} px: the words under the frame say what it shows`,
        (await card.locator(".figure-caption").first().textContent()) === "Exports by week, last five weeks", await card.locator(".figure-caption").first().textContent());

      const inChart = async () => (await frame.elementHandle()).contentFrame();
      const inside = await inChart();
      const reach = await inside.evaluate(() => {
        const tries = {};
        try { tries.parent = String(parent.document.title); } catch (error) { tries.parent = "blocked"; }
        try { tries.cookie = document.cookie; } catch (error) { tries.cookie = "blocked"; }
        return tries;
      });
      check(`${width} px: the page in the frame reaches neither Cockpit's page nor the cookie`,
        reach.parent === "blocked" && reach.cookie === "blocked", JSON.stringify(reach));

      // A height message from Cockpit's own window, not a figure's frame, changes nothing.
      await page.evaluate(() => window.postMessage({ cockpitFigure: 33 }, "*"));
      await sleep(200);
      const after = await frame.evaluate((el) => el.getBoundingClientRect().height);
      check(`${width} px: a height message from elsewhere changes nothing`, after === fitted, `${after} px`);

      // A redraw draws the step anew and keeps the frame with its page as loaded, at its height.
      // A poll may have redrawn the step already: a frame drawn anew would have dropped the handle.
      check(`${width} px: the frame's page stayed loaded since it was found`,
        await inside.evaluate(() => { window.loadedOnce = true; return true; }).catch(() => false), "reloaded");
      // A poll under way when the check asks makes the redraw follow it, so the check waits for the redraw.
      await card.locator(".prose.body").first().evaluate((el) => { el.dataset.old = "1"; });
      await page.evaluate(() => { ui.pendingRender = true; return refresh(); });
      const redrawn = await waitFor(() => card.locator(".prose.body").first().getAttribute("data-old"), (value) => value === null);
      check(`${width} px: the redraw drew the step's text anew`, redrawn === null, "same text");
      check(`${width} px: the redraw kept the frame's page as loaded`, await inside.evaluate(() => window.loadedOnce === true).catch(() => false), "reloaded");
      check(`${width} px: the frame keeps its height through the redraw`,
        (await frame.evaluate((el) => el.style.height)) === `${Math.round(fitted)}px`, await frame.evaluate((el) => el.style.height));
      check(`${width} px: the image strip leaves out the image the text shows`,
        (await card.locator(`.thumbs img[src="/item-images/${image.id}"]`).count()) === 0, "shown twice");

      await sleep(500);
      const shot = path.join(work, `figures-${width}.png`);
      await card.screenshot({ path: shot });
      console.error(`  screenshot ${shot}`);

      // A page taller than a figure may be stops at 900 px. The frames load as they scroll into view and
      // move the summary, so the check scrolls to it first, to the middle, clear of the step's sticky keys.
      const fold = card.locator("summary", { hasText: "A tall page" });
      await fold.evaluate((el) => el.scrollIntoView({ block: "center" }));
      await sleep(300);
      await fold.click();
      const tallFrame = card.locator(`iframe.figure-frame[src^="/pages/${tall.id}/figure?"]`);
      await tallFrame.scrollIntoViewIfNeeded();
      const tallHeight = await waitFor(() => tallFrame.evaluate((el) => el.getBoundingClientRect().height), (value) => value === 900);
      check(`${width} px: a taller page stops at 900 px`, tallHeight === 900, `${tallHeight} px`);
      const fullFrame = card.locator(`iframe.figure-frame[src^="/pages/${full.id}/figure?"]`);
      await fullFrame.scrollIntoViewIfNeeded();
      const fullHeight = await waitFor(() => fullFrame.evaluate((el) => el.getBoundingClientRect().height), (value) => value < 120);
      await sleep(1000);
      const fullLater = await fullFrame.evaluate((el) => el.getBoundingClientRect().height);
      check(`${width} px: a full-height body stays its content's height`, fullHeight < 120 && fullLater === fullHeight, `${fullHeight} then ${fullLater} px`);
      // Each width loads the page anew, so its content grows 1.5 s after the frame loads.
      const growFrame = card.locator(`iframe.figure-frame[src^="/pages/${grow.id}/figure?"]`);
      await growFrame.scrollIntoViewIfNeeded();
      const grown = await waitFor(() => growFrame.evaluate((el) => el.getBoundingClientRect().height), (value) => value === 240);
      check(`${width} px: content growing in a fixed-height body grows the frame`, grown === 240, `${grown} px`);

      // Keep the chart in view so its lazy frame loads the new revision.
      await frame.scrollIntoViewIfNeeded();

      // The second of two updates to the page within a second shows in the frame once the poll brings it.
      const words = `Updated at ${width} px`;
      await agent("PUT", `/api/item-pages/${chart.id}`, { title: "Exports by week", html: CHART.replace("Exports by week", "First update") });
      await agent("PUT", `/api/item-pages/${chart.id}`, { title: "Exports by week", html: CHART.replace("Exports by week", words) });
      const updated = await waitFor(async () => {
        const now = await inChart();
        return now ? now.evaluate(() => document.body.textContent).catch(() => "") : "";
      }, (text) => text.includes(words), 12000);
      check(`${width} px: the second of two quick updates shows in the frame`, updated.includes(words), updated.slice(0, 80));

      const picture = card.locator(`img.figure-image[src="/item-images/${image.id}"]`);
      await picture.scrollIntoViewIfNeeded();
      check(`${width} px: the image shows`, await picture.evaluate((el) => el.complete && el.naturalWidth === 240), "not loaded");
      await picture.click();
      check(`${width} px: a click opens it full size`, await page.locator("#lightbox").isVisible(), "no lightbox");
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
