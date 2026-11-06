// The brand (SPEC UI-28): Graphite marker, the amber marker on a graphite tile. Checks, in both schemes at
// desktop width and on a phone:
// - the header key shows the marker (icons.js's brand mask) on the graphite tile;
// - the key keeps the logo's colours while steps wait for the user: the synthetic database has
//   some, and the check says so;
// - on the rail, pointing at the key turns it into the menu's three lines on the same tile;
// - on a phone the list screen's key is the menu's three lines in amber on the graphite tile;
// - the tab icon is Graphite marker's drawing;
// - the Cockpit tag (UI-43) shows the amber marker on the graphite tile;
// - no page errors.
// Saves a close-up screenshot of the key in each state in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database) and drives a headless Chromium.
// Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/brand_logo.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;
const { chromium } = require(PLAYWRIGHT);

const GRAPHITE = "rgb(44, 41, 38)";

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-brand-");
  const server = spawn(path.join(__dirname, "..", "..", ".venv", "bin", "python"), [path.join(__dirname, "serve_preview.py"), work],
    { stdio: ["ignore", "inherit", "inherit"] });
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const info = await harnessUp(work, server);
    const cookie = `${info.name}=${info.value}`;
    const icon = await fetch(`${info.origin}/static/icon.svg`).then((response) => response.text());
    check("the tab icon is Graphite marker's drawing", icon.includes("#ffbe5c") && icon.includes("#2c2926") && icon.includes("<circle"), icon.slice(0, 120));
    const counts = await fetch(`${info.origin}/api/state`, { headers: { cookie } }).then((response) => response.json()).then((state) => state.counts);
    check("steps wait for the user while the key is read", counts.waiting_on_user > 0, JSON.stringify(counts));

    browser = await chromium.launch({ executablePath: CHROMIUM });
    for (const [width, scheme] of [[1440, "light"], [1440, "dark"], [390, "dark"], [390, "light"]]) {
      const context = await browser.newContext({ viewport: { width, height: 800 }, colorScheme: scheme, deviceScaleFactor: 3 });
      await context.setExtraHTTPHeaders({ cookie });
      context.setDefaultTimeout(180e3);
      // On a phone the rail, with the key, shows on the list screen (sidebar.js).
      if (width <= 640) await context.addInitScript(() => { try { sessionStorage.setItem("cockpit.phone.screen", "list"); } catch { /* none */ } });
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", (error) => errors.push(String(error)));
      // The load event waits for every stylesheet, look.css last, so the first read sees the finished look.
      await page.goto(info.origin + "/", { waitUntil: "load" });
      const tag = `${width}-${scheme}`;
      const key = page.locator(".brand-mark:visible").first();
      await key.waitFor();
      await sleep(400);
      // The key's colours ease between states; each read waits for the key's transitions, its marker's too, to end.
      const look = () => key.evaluate(async (element) => {
        await Promise.all(element.getAnimations({ subtree: true }).map((animation) => animation.finished.catch(() => null)));
        const tile = getComputedStyle(element);
        const glyph = getComputedStyle(element, "::after");
        return { image: tile.backgroundImage, color: tile.backgroundColor, glyph: glyph.backgroundImage, mask: glyph.maskImage || glyph.webkitMaskImage };
      });
      const shoot = async (name) => {
        const box = await key.boundingBox();
        await page.screenshot({ path: path.join(work, `brand-${tag}-${name}.png`), clip: { x: Math.max(0, box.x - 10), y: Math.max(0, box.y - 10), width: 200, height: box.height + 20 } });
      };
      const quiet = await look();
      if (width > 640) {
        check(`${tag}: the key is the graphite tile`, quiet.image.includes(GRAPHITE), quiet.image);
      } else {
        check(`${tag}: the phone key is the menu on the graphite tile`, quiet.image.includes(GRAPHITE) && quiet.mask.includes("svg"), JSON.stringify(quiet));
      }
      check(`${tag}: the marker is amber`, quiet.glyph.includes("rgb(255, 190, 92)"), quiet.glyph);
      await shoot("quiet");
      if (width > 640 && await page.locator(".rail .brand").count()) {
        await page.locator(".rail .brand").hover({ force: true });
        await sleep(250);
        const hover = await look();
        check(`${tag}: pointing at the key shows the menu on the same tile`, hover.image === quiet.image && hover.mask !== quiet.mask, hover.image);
        await shoot("hover");
      }
      const chip = await page.evaluate(() => {
        const made = window.Icons.cockpitTag("cockpit");
        made.id = "brand-test-chip";
        made.style.cssText = "position: fixed; top: 12px; left: 400px; z-index: 9999";
        document.body.append(made);
        const mark = made.querySelector(".cockpit-mark");
        return { image: getComputedStyle(mark).backgroundImage, glyph: getComputedStyle(mark, "::after").backgroundColor };
      });
      check(`${tag}: the Cockpit tag is the amber marker on the graphite tile`, chip.image.includes(GRAPHITE) && chip.glyph === "rgb(247, 175, 70)", JSON.stringify(chip));
      await page.locator("#brand-test-chip").screenshot({ path: path.join(work, `brand-${tag}-chip.png`) });
      check(`${tag}: no page errors`, errors.length === 0, errors.join("; "));
      await context.close();
    }
    console.error(`screenshots in ${work}`);
  } finally {
    if (browser) await browser.close();
    server.kill("SIGTERM");
  }
  if (results.some((one) => !one.ok)) process.exitCode = 1;
}

async function harnessUp(work, server) {
  const file = path.join(work, "cookie.json");
  const end = Date.now() + 180e3;
  while (Date.now() < end) {
    if (server.exitCode !== null) throw new Error(`serve_preview.py exited with ${server.exitCode}`);
    if (fs.existsSync(file)) {
      const info = JSON.parse(fs.readFileSync(file, "utf8"));
      const up = await fetch(`${info.origin}/healthz`).then((response) => response.ok, () => false);
      if (up) return info;
    }
    await sleep(200);
  }
  throw new Error("serve_preview.py did not come up within 180 s");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
