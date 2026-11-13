// Feedback picking reaches the parts the picker used to miss (SPEC FEEDBACK-7). Checks, on the page at /:
// - with Usage open in the center layer, the hover mark lands on the part inside the framed page;
// - a click there picks that part, named by the framed page (`page:/usage/...`) with its own words;
// - Wider from that part steps out of the framed page into the outer one, then the whole page (the frame itself is
//   passed when the framed page fills it, as the eye cannot tell them apart); Narrower steps back into the frame;
// - while picking, a press, a double click and the pointer coming over a part reach none of the page's own
//   handlers, and they do again once picking stops;
// - the Feedback key itself can be picked;
// - no page errors.
// Saves a screenshot of the pick inside Usage in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what
// it needs) and drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/pick_frames.cjs

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
  const work = workFolder("cockpit-pick-frames-");
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
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
    const page = await context.newPage();
    page.setDefaultTimeout(5000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`${info.origin}/`);
    await page.locator("#sidebar .side-row").first().waitFor({ timeout: 30e3 });

    // A part inside Usage, framed in the center layer.
    await page.evaluate(() => CockpitCenter.openHelper("/usage"));
    const frame = page.frameLocator("iframe.center-page");
    const heading = frame.locator("#limits-title");
    await heading.waitFor({ timeout: 20e3 });
    await sleep(500);
    await page.evaluate(() => startPicking("feedback"));
    const spot = await heading.boundingBox();
    const x = spot.x + 20;
    const y = spot.y + spot.height / 2;
    await page.mouse.move(x, y);
    await sleep(200);
    const hover = await heading.evaluate((el) => el.classList.contains("pick-hover"));
    check("the hover mark lands on the part inside the framed page", hover, "no pick-hover on #limits-title");
    await page.mouse.click(x, y);
    await sleep(300);
    const picked = await page.evaluate(() => ui.pick && { target: ui.pick.target, quote: ui.pick.quote, box: !document.getElementById("pick-box").hidden });
    check("a click picks the part inside the framed page, named by that page",
      picked?.target === "page:/usage/limits-title" && picked.quote === "Weekly limits" && picked.box, JSON.stringify(picked));
    await page.screenshot({ path: path.join(work, "pick-in-usage.png") });

    const way = await page.evaluate(() => {
      const steps = [];
      let part = ui.pick.el;
      for (let i = 0; i < 30 && part !== document.body; i++) {
        part = Pick.wider(part, PICKABLE, FEEDBACK_CONTROLS);
        steps.push(part === document.body ? "body" : `${part.ownerDocument === document ? "outer" : "framed"}:${part.localName}${part.className ? `.${String(part.className).split(" ")[0]}` : ""}`);
      }
      // The first part out in the outer page; Narrower from it goes back on the way in, into the framed page.
      let out = ui.pick.el;
      while (out !== document.body && out.ownerDocument !== document) out = Pick.wider(out, PICKABLE, FEEDBACK_CONTROLS);
      const back = Pick.narrower(out, ui.pick.from, PICKABLE, FEEDBACK_CONTROLS);
      return { steps, back: back && back.ownerDocument !== document ? back.localName : String(back) };
    });
    const out = way.steps.findIndex((step) => !step.startsWith("framed:"));
    check("Wider steps out of the framed page into the outer one, then on to the whole page",
      out > 0 && way.steps[out].startsWith("outer:") && way.steps.slice(out).every((step) => !step.startsWith("framed:")) && way.steps.at(-1) === "body", JSON.stringify(way.steps));
    check("Narrower from there steps back into the framed page", way.back !== "null" && way.back !== "undefined" && !way.back.startsWith("[object"), way.back);
    await page.keyboard.press("Escape");
    await page.evaluate(() => { if (ui.pick) closePick(); if (ui.picking) stopPicking(); CockpitCenter.close(); });
    await sleep(300);

    // A part with its own press and hover handlers, as a calendar event or a grip has: none of them hear the
    // pointer while picking. The test adds it, so no redraw replaces it.
    await page.evaluate(() => {
      window.heard = [];
      const probe = document.createElement("div");
      probe.id = "probe";
      probe.textContent = "Probe";
      probe.style.cssText = "position:fixed;left:600px;top:400px;width:160px;height:60px;z-index:50;background:#fde;padding:8px";
      for (const type of ["pointerdown", "pointerup", "mousedown", "mouseup", "dblclick", "pointerover", "mouseover", "pointerenter"]) {
        probe.addEventListener(type, () => window.heard.push(type));
      }
      document.body.append(probe);
    });
    const row = await page.locator("#probe").boundingBox();
    const rx = row.x + row.width / 2;
    const ry = row.y + row.height / 2;
    await page.mouse.move(rx, ry + 200);
    await page.evaluate(() => startPicking("feedback"));
    await page.mouse.move(rx, ry, { steps: 4 });
    await page.mouse.dblclick(rx, ry);
    await sleep(300);
    const whilePicking = await page.evaluate(() => ({ heard: [...new Set(window.heard)], picked: ui.guess?.parts[0].id }));
    check("while picking, the part's own press, double click and hover handlers hear nothing, and the click picks it",
      whilePicking.heard.length === 0 && whilePicking.picked === "probe", JSON.stringify(whilePicking));
    await page.evaluate(() => { if (ui.pick) closePick(); if (ui.picking) stopPicking(); });
    await page.mouse.move(rx, ry + 200);
    await page.mouse.move(rx, ry, { steps: 4 });
    await page.mouse.down();
    await page.mouse.up();
    await sleep(200);
    const after = await page.evaluate(() => [...new Set(window.heard)]);
    check("once picking stops, the part's handlers hear the pointer again",
      ["pointerdown", "mousedown", "pointerover"].every((type) => after.includes(type)), JSON.stringify(after));
    await page.evaluate(() => { if (ui.pick) closePick(); });

    // The Feedback key itself.
    await page.evaluate(() => startPicking("feedback"));
    await page.locator("#feedback-mode").click();
    await sleep(300);
    const key = await page.evaluate(() => ({ picked: ui.pick?.el.id, target: ui.pick?.target, picking: ui.picking }));
    check("the Feedback key can be picked", key.picked === "feedback-mode", JSON.stringify(key));

    check("no page errors", errors.length === 0, errors.join(" | "));
    await context.close();
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failed = results.filter((result) => !result.ok);
  console.error(`${results.length - failed.length}/${results.length} checks pass; screenshots in ${work}`);
  if (failed.length) process.exitCode = 1;
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
