// The card that takes Next up after a Send shows whole and still (SPEC UI-16, UI-62): the steps after the card load
// ahead (app.js warmNext), so the next card draws with its history, figure images and figure pages at their sizes,
// rather than with a history placeholder that the history then pushes down, and images and frames that grow. Checks,
// at 1440 px, after Send on Next up's reply box: - the next card's first drawing shows no history placeholder, and
// each figure image in it has its height; - for 3 s after, while the same step holds the card, none of its parts
// changes top or height by more than 2 px.  The fixture database's open steps are snoozed, and the test puts two steps
// of its own: the one it sends on, and the next, whose body holds a paragraph, a figure page 420 px high and an
// image, so each would push the reply box down as it arrived. Starts serve_preview.py (this checkout's Cockpit on a
// synthetic database; README.md says what it needs) and drives a headless Chromium. Exits 1 when a check
// failed. A run takes about half a minute.  Usage, from the checkout: node tests/browser/smooth_next.cjs

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
const STILL_MS = 3000;
// The harness's agent token and second fake session (tests/conftest.py).
const SESSION_B = "22222222-2222-4222-8222-222222222222";
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "smooth-next-test", "content-type": "application/json" };
const CHART = `<!doctype html><style>body{margin:8px;font:14px sans-serif}.bar{height:40px;margin:10px 0;background:#5b7cfa}</style>
<h3>Exports by week</h3>${[20, 40, 60, 80, 100].map((width) => `<div class="bar" style="width:${width * 2}px"></div>`).join("")}
<div style="height:100px"></div>`;

const { chromium } = require(PLAYWRIGHT);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function harnessUp(work, server) {
  const file = path.join(work, "cookie.json");
  for (let i = 0; i < 600; i++) {
    if (server.exitCode !== null) throw new Error(`serve_preview.py exited with ${server.exitCode}`);
    if (fs.existsSync(file)) {
      const info = JSON.parse(fs.readFileSync(file, "utf8"));
      if (await fetch(`${info.origin}/healthz`).then((r) => r.ok, () => false)) return info;
    }
    await sleep(200);
  }
  throw new Error("serve_preview.py did not come up");
}

// Runs in the page: each animation frame, the live Next up card and the top and height of its parts.
function recorder() {
  window.__frames = [];
  window.__record = () => {
    const start = performance.now();
    const tick = () => {
      const card = document.querySelector("#next > [data-step]:not(.motion-ghost)");
      const parts = card ? [...card.querySelectorAll("section.hist-fold-card, section.history, .hist-ghost, .prose, img.figure-image, iframe.figure-frame, .composer")]
        .filter((el) => !el.matches("section.history") || !el.closest(".hist-fold-card"))
        .map((el, index) => {
          const box = el.getBoundingClientRect();
          return { key: `${index}:${el.tagName}.${el.classList[0]}`, item: el.dataset.item,
            src: el.getAttribute("src") || el.dataset.src, text: el.textContent,
            top: Math.round(box.top), width: box.width, height: Math.round(box.height) };
        }) : [];
      window.__frames.push({ t: Math.round(performance.now() - start), step: card ? card.dataset.step : null, parts });
      if (performance.now() - start < 8000) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  };
}

async function main() {
  const work = workFolder("cockpit-smooth-next-");
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser = null;
  const failures = [];
  const check = (name, ok, detail) => {
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
    if (!ok) failures.push(name);
  };
  try {
    const info = await harnessUp(work, server);
    const cookie = { cookie: `${info.name}=${info.value}`, origin: info.origin };
    const agent = (method, url, body) => call(info.origin, AGENT, method, url, JSON.stringify(body));
    const user = (method, url, body) => call(info.origin, { ...cookie, "content-type": "application/json" }, method, url,
      body === undefined ? undefined : JSON.stringify(body));
    const copied = await user("GET", "/api/state");
    await Promise.all([...copied.queue, ...copied.ready_next, ...copied.snoozed].map((step) => user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
    const run = Date.now();
    const first = await agent("PUT", "/api/items", { key: `note:smooth-first-${run}`, title: "Smooth test first", priority: "urgent" });
    const second = await agent("PUT", "/api/items", { key: `note:smooth-next-${run}`, title: "Smooth test next", priority: "urgent" });
    const chart = await agent("POST", `/api/items/${second.id}/pages`, { title: "Exports by week", html: CHART });
    const image = await call(info.origin, { ...cookie, "content-type": "image/png" }, "POST", `/api/items/${second.id}/images`, png(240, 90));
    const sentStep = (await agent("POST", "/api/steps", {
      item_id: first.id, need: `send-${run}`, ask: "Reply to this step", score: 100, session_id: SESSION_B, body: "A step to send on.",
    })).step.id;
    const nextStep = (await agent("POST", "/api/steps", {
      item_id: second.id, need: `next-${run}`, ask: "Ship the export change?", score: 90, session_id: SESSION_B,
      body: `Exports rose each week since the change.\n\n![Exports by week](page:${chart.id})\n\n![The new form](image:${image.id})\n\nThe form is shorter now.`,
    })).step.id;
    browser = await chromium.launch({ executablePath: CHROMIUM });
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
    await context.addInitScript(recorder);
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`${info.origin}/`);
    await page.waitForSelector("#next > [data-step]", { timeout: 60e3 });
    // Time for the warm-up after the first drawing to load what the next card shows.
    await page.waitForLoadState("networkidle", { timeout: 60e3 });
    await sleep(1500);
    const card = page.locator("#next > [data-step]:not(.motion-ghost)").first();
    const sent = await card.getAttribute("data-step");
    if (Number(sent) !== sentStep) throw new Error(`Next up shows step ${sent}, not the test's step ${sentStep}`);
    await card.locator("textarea").first().fill("Test reply from smooth_next.cjs");
    await page.evaluate(() => window.__record());
    await card.locator("button.send").first().click();
    await sleep(8500);
    const frames = await page.evaluate(() => window.__frames);
    const at = frames.findIndex((frame) => frame.step && frame.step !== sent);
    if (at < 0) throw new Error(`no other step took Next up within 8 s of the Send on step ${sent}`);
    const next = frames[at];
    console.error(`  sent step ${sent}; step ${next.step} took the card ${next.t} ms after`);
    check("the expected step takes the next card", Number(next.step) === nextStep, `expected ${nextStep}, got ${next.step}`);
    const history = next.parts.filter((part) => part.item === String(second.id));
    const sized = (parts) => parts.length === 1 && parts[0].width > 0 && parts[0].height > 0;
    check("the next card's first drawing has its history", sized(history)
      && history[0].text.includes("Ship the export change?") && !next.parts.some((part) => part.key.endsWith(".hist-ghost")),
      JSON.stringify(history));
    const images = next.parts.filter((part) => part.key.includes("IMG.") && part.src === `/item-images/${image.id}`);
    check("the known figure image has positive dimensions when it shows", sized(images), JSON.stringify(images));
    const framesShown = next.parts.filter((part) => part.key.includes("IFRAME.") && part.src?.startsWith(`/pages/${chart.id}/figure?v=`));
    check("the known figure frame has positive dimensions when it shows", sized(framesShown), JSON.stringify(framesShown));
    const still = frames.slice(at).filter((frame) => frame.t - next.t <= STILL_MS && frame.step === next.step);
    const moved = [];
    for (const frame of still) {
      const before = new Map(next.parts.map((part) => [part.key, part]));
      const keys = frame.parts.map((part) => part.key).join(" ");
      if (keys !== next.parts.map((part) => part.key).join(" ")) { moved.push(`${frame.t} ms: parts ${keys}`); break; }
      const shifted = frame.parts.find((part) => Math.abs(part.top - before.get(part.key).top) > 2 || Math.abs(part.height - before.get(part.key).height) > 2);
      if (shifted) { moved.push(`${frame.t} ms: ${JSON.stringify(shifted)} was ${JSON.stringify(before.get(shifted.key))}`); break; }
    }
    check(`nothing on the next card moves in the ${STILL_MS / 1000} s after it shows`, moved.length === 0, moved.join("; "));
    check("no page errors", errors.length === 0, errors.join("; "));
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  if (failures.length) {
    console.error(`${failures.length} check(s) failed`);
    process.exit(1);
  }
  console.error("all checks passed");
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

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
