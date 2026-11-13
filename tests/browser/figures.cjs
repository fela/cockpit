// Figures in a step (SPEC NEXT-17): a step's body shows one of its item's shared pages in a sandboxed frame
// and one of its item images, each with its words under it. Checks, on the main page at phone and desktop
// width, for the step in Next up:
// - the frame takes the height its page reports, from 80 px, and keeps it through a redraw;
// - a page taller than 900 px, which would scroll inside its frame, shows as a button that opens the page
//   (SPEC NEXT-57), in every step that draws it and through a redraw, and so does a link to a page on a line of
//   its own;
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
    const initial = await user("GET", "/api/state");
    await Promise.all([...initial.queue, ...initial.ready_next, ...initial.snoozed].map((step) => user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
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
        + `![The new form](image:${image.id})\n\nThe chart alone is [the weekly chart](page:${chart.id}), in a sentence.\n\n`
        + `[The whole export report](page:${tall.id})\n\n[The report by its address](${info.origin}/pages/${tall.id})`,
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
      // On a slow machine the run outlasts the user's idle minute while the step's timer runs. The "Back after"
      // dialog is answered with Return whenever it shows, so it never takes the test's clicks.
      await page.addLocatorHandler(page.locator("dialog#idle[open]"), (dialog) => dialog.locator("button", { hasText: "Return" }).click()
        // The test may close its page while an answer is under way.
        .catch((error) => { if (!/has been closed/.test(error.message)) throw error; }));
      // A minute without the mouse fades the idle veil in over the page; a move clears it.
      let moves = 0;
      await page.addLocatorHandler(page.locator("#idle-veil"), () => page.mouse.move(5 + (moves++ % 2), 5)
        .catch((error) => { if (!/has been closed/.test(error.message)) throw error; }));
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
      const lone = card.locator(".prose.body > p > a.page-button").first();
      const byAddress = card.locator(".prose.body > p > a.page-button").nth(1);
      check(`${width} px: a link by the page's web address on a line of its own is a button too`,
        (await byAddress.locator(".page-button-name").textContent()) === "The report by its address"
          && (await byAddress.getAttribute("href")).includes(String(tall.id)), await byAddress.evaluate((el) => el.outerHTML));
      const inSentence = card.locator(".prose.body a", { hasText: "the weekly chart" });
      check(`${width} px: a link to a page inside a sentence stays a link, with the page icon before its words`,
        (await card.locator(".prose.body a.page-button").count()) === 2
          && (await inSentence.evaluate((el) => el.className)) === "page-link" && (await inSentence.locator("svg.icon-doc").count()) === 1
          && (await inSentence.textContent()) === "the weekly chart",
        await inSentence.evaluate((el) => el.outerHTML));
      check(`${width} px: a link to a page on a line of its own is a button with the page's name and Open page`,
        (await lone.locator(".page-button-name").textContent()) === "The whole export report"
          && (await lone.locator(".page-button-open").textContent()).trim() === "Open page"
          && (await lone.getAttribute("href")).includes(String(tall.id)), await lone.evaluate((el) => el.outerHTML));
      const loneBox = await lone.boundingBox();
      check(`${width} px: the button is a row at least 44 px high`, loneBox.height >= 44 && loneBox.width >= 240, JSON.stringify(loneBox));
      const open = card.locator(".figure").first().locator("a.figure-open");
      check(`${width} px: under the frame a link opens the page full size`,
        (await open.textContent()).trim() === "Open full size" && /\/pages(\/|\?id=)/.test(await open.getAttribute("href"))
          && (await open.getAttribute("href")).includes(String(chart.id)), await open.getAttribute("href"));
      const fullscreenEnabled = await (await frame.elementHandle()).contentFrame().then((inner) => inner.evaluate(() => document.fullscreenEnabled));
      check(`${width} px: the frame lets its page go full screen`, fullscreenEnabled === true, fullscreenEnabled);

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

      // A page taller than a figure may be shows as a button in its frame's place. The frames load as they scroll
      // into view and move the summary, so the check scrolls to it first, to the middle, clear of the step's
      // sticky keys.
      const fold = card.locator("summary", { hasText: "A tall page" });
      await fold.evaluate((el) => el.scrollIntoView({ block: "center" }));
      await sleep(300);
      await fold.click();
      const tallFigure = card.locator(".details .figure").first();
      const tallButton = tallFigure.locator("a.page-button.figure-open");
      await tallButton.waitFor({ timeout: 10000 }).catch(() => {});
      const tallFrames = () => page.locator(`iframe.figure-frame[src^="/pages/${tall.id}/figure?"], iframe.figure-frame[data-src^="/pages/${tall.id}/figure?"]`).count();
      check(`${width} px: a page too tall for a frame shows as a button named after it, with no frame`,
        (await tallButton.count()) === 1 && (await tallButton.locator(".page-button-name").textContent()) === "Tall"
          && (await tallFigure.locator(".figure-caption").count()) === 0 && (await tallFrames()) === 0,
        await tallFigure.evaluate((el) => el.outerHTML));
      check(`${width} px: the button opens the page for the step and stands alone, with no links under it`,
        new RegExp(`/pages\\?id=${tall.id}&step=${stepId}$`).test(await tallButton.getAttribute("href"))
          && (await tallFigure.locator(".figure-links").count()) === 0, await tallButton.getAttribute("href"));
      // A poll may redraw the step while the picture is taken, so the picture tries again once.
      const tallShot = () => tallFigure.screenshot({ path: path.join(work, `figures-tall-${width}.png`) });
      await tallShot().catch(() => sleep(500).then(tallShot));
      // A redraw draws the button at once, without loading the page to measure it again.
      let tallLoads = 0;
      const countTall = (request) => { if (request.url().includes(`/pages/${tall.id}/figure`)) tallLoads += 1; };
      page.on("request", countTall);
      await page.evaluate(() => { ui.pendingRender = true; return refresh(); });
      await sleep(1500);
      page.off("request", countTall);
      check(`${width} px: a redraw keeps the button and loads no frame for the page`,
        (await tallButton.count()) === 1 && (await tallFrames()) === 0 && tallLoads === 0, `${await tallFrames()} frames, ${tallLoads} loads`);
      // The button opens the page over Cockpit, in a layer named after the page.
      await tallButton.click();
      const tallLayer = page.locator("dialog.page-overlay[open]:not(.page-overlay-preload)");
      await tallLayer.locator("iframe.page-overlay-frame").waitFor({ state: "visible", timeout: 10000 }).catch(() => {});
      const tallLayerName = await tallLayer.getAttribute("aria-label").catch(() => null);
      check(`${width} px: the button opens the page over Cockpit, in a layer named after the page`, tallLayerName === "Tall", tallLayerName);
      await tallLayer.locator(".page-overlay-close").click();
      await tallLayer.waitFor({ state: "detached", timeout: 10000 }).catch(() => {});
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
      await page.keyboard.press("Escape");
      const openLink = card.locator("a.figure-open").first();
      await openLink.scrollIntoViewIfNeeded();
      await openLink.click();
      // The layer the click opened, not one of the hidden layers that load pages ahead.
      const shown = page.locator("dialog.page-overlay[open]:not(.page-overlay-preload) iframe.page-overlay-frame");
      await shown.waitFor({ state: "visible", timeout: 10000 }).catch(() => {});
      const box = await shown.boundingBox().catch(() => null);
      check(`${width} px: Open full size shows the page over Cockpit, ${width < 700 ? "filling the screen" : "beside the step"}`,
        box !== null && box.height >= height * 0.8 && (width >= 700 || box.width >= width - 2), JSON.stringify(box));
      const layerName = await page.locator("dialog.page-overlay[open]:not(.page-overlay-preload)").getAttribute("aria-label").catch(() => null);
      check(`${width} px: the layer is named after the page, not the link`, !!layerName && layerName !== "Open full size", layerName);
      const loadedAhead = await page.$$eval("dialog.page-overlay-preload iframe", (frames) => frames.map((each) => new URL(each.src).searchParams.get("id")));
      check(`${width} px: no page loads ahead twice`, new Set(loadedAhead).size === loadedAhead.length, loadedAhead.join(","));
      await page.screenshot({ path: path.join(work, `figures-open-${width}.png`) });
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
