// Stopped sweeps and feedback mode: on a synthetic database
// with the state saying sweeps stopped, the Cockpit page's rail says so above its readouts, with Restart sweeps, and /classic shows Restart sweeps as an icon in its top bar, with no banner; a press restarts the sweeps. In feedback mode a
// click picks a banner's words, the button, and an element no pickable part holds.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what
// it needs) and drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/restart_sweeps.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

// Browser dependencies can be selected through the environment (README.md).
const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-restart-sweeps-");
  const server = spawn(path.join(__dirname, "..", "..", ".venv", "bin", "python"), [path.join(__dirname, "serve_preview.py"), work],
    { stdio: ["ignore", "inherit", "inherit"] });
  let browser = null;
  let context = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const info = await harnessUp(work, server);
    browser = await chromium.launch({ executablePath: CHROMIUM });
    context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
    // The state says sweeps stopped at 03:10, and carries one warning banner, until Restart sweeps is pressed.
    let paused = true;
    let resumed = 0;
    await context.route("**/api/state**", async (route) => {
      const response = await route.fetch();
      const state = await response.json();
      state.sweeps_paused = paused ? { at: "2026-01-15T10:00:00Z", away_since: "2026-01-15T08:00:00Z" } : null;
      state.warnings = ["A warning the server sends"];
      await route.fulfill({ response, json: state });
    });
    await context.route("**/api/sweeps/resume", async (route) => {
      resumed += 1;
      paused = false;
      await route.fulfill({ json: { sweeps_paused: null } });
    });
    const page = await context.newPage();
    page.setDefaultTimeout(5000);
    page.setDefaultNavigationTimeout(30000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));

    await page.goto(`${info.origin}/`);
    // The first state takes a few seconds on the synthetic database.
    await page.locator(".sidebar-groups").waitFor({ timeout: 30000 });
    const rail = await page.evaluate(() => ["update", "sweep-status"].map((id) => {
      const el = document.getElementById(id);
      return { id, inRail: Boolean(el.closest(".rail")), shown: Boolean(el.getClientRects().length) };
    }));
    check("on the Cockpit page the rail shows no Update or sweep menu", rail.every((one) => !one.inRail && !one.shown), JSON.stringify(rail));
    const notice = page.locator("#sweeps-paused");
    await notice.waitFor({ timeout: 30000 });
    const noticePlace = await notice.evaluate((el) => {
      const line = document.querySelector(".rail-line").getBoundingClientRect();
      const own = el.getBoundingClientRect();
      const list = document.getElementById("sidebar").getBoundingClientRect();
      return { words: el.innerText.replace(/\s+/g, " ").trim(), above: own.bottom <= line.top, clear: own.top >= list.bottom - 1 };
    });
    check("the rail says Sweeps paused with a Restart key, above the readouts and over no row of the list",
      noticePlace.words === "Sweeps paused Restart" && noticePlace.above && noticePlace.clear, JSON.stringify(noticePlace));
    await page.locator("#resume-sweeps").click();
    check("Restart restarts sweeps", await waitFor(() => Promise.resolve(resumed), (value) => value === 1), resumed);
    check("the notice leaves once sweeps run", await waitFor(() => notice.isVisible(), (value) => value === false), "still shown");
    paused = true;

    check("no banner says sweeps stopped", await page.locator(".warn-card", { hasText: "Sweeps stopped" }).count() === 0, "a banner remains");

    await page.goto(`${info.origin}/classic`);
    const button = page.locator("#resume-sweeps");
    await button.waitFor({ timeout: 30000 });
    const place = await button.evaluate((el) => ({ words: el.innerText.trim(), icon: Boolean(el.querySelector("svg.icon-play")) }));
    check("on /classic it says Restart sweeps, with a play icon", place.icon, JSON.stringify(place));

    // Feedback mode: the warning banner's words, the button, and an element outside every pickable part.
    await page.evaluate(() => startPicking("feedback"));
    await page.waitForFunction(() => document.body.classList.contains("picking"));
    const chosen = () => page.evaluate(() => {
      const el = document.querySelector(".pick-chosen");
      return el && (el.id || [...el.classList].filter((name) => !name.startsWith("pick-")).join(" ") || el.tagName.toLowerCase());
    });
    // Its words, at its left: the feedback bar floats over the middle of the page's top.
    await page.locator(".warn-title", { hasText: "A warning the server sends" }).click({ position: { x: 24, y: 12 } });
    check("a click on a warning's words picks its line", await waitFor(chosen, (value) => value === "warn-title"), await chosen());
    await page.keyboard.press("Escape");
    await page.evaluate(() => startPicking("feedback"));
    await page.waitForFunction(() => document.body.classList.contains("picking"));
    await button.click();
    check("a click on Restart sweeps picks it", await waitFor(chosen, (value) => value === "resume-sweeps"), await chosen());
    await page.keyboard.press("Escape");
    // A line of words in a boxed wrapper in a section, as a chart's title sits on Usage: the line itself, then
    // a click on the same spot steps out to the box around it, then to the section.
    await page.evaluate(() => {
      const section = document.createElement("section");
      section.className = "loose-section";
      Object.assign(section.style, { position: "fixed", left: "560px", top: "360px", padding: "24px", zIndex: 5 });
      const box = document.createElement("div");
      box.className = "loose-box";
      // Through the style object: the page's content policy drops style attributes.
      Object.assign(box.style, { padding: "12px", background: "white" });
      const line = document.createElement("div");
      line.className = "loose-words";
      line.textContent = "Words in no pickable part";
      box.append(line);
      section.append(box);
      document.body.append(section);
    });
    await page.evaluate(() => startPicking("feedback"));
    await page.waitForFunction(() => document.body.classList.contains("picking"));
    const spot = await page.locator(".loose-words").boundingBox();
    const steps = [];
    for (let at = 0; at < 3; at++) {
      await page.mouse.click(spot.x + 8, spot.y + spot.height / 2);
      await waitFor(chosen, (value) => value && value !== steps[steps.length - 1]);
      steps.push(await chosen());
    }
    check("a click on words no pickable part names picks their own line, then the box around it, then the section",
      steps.join(" > ") === "loose-words > loose-box > loose-section", steps.join(" > "));
    await page.keyboard.press("Escape");
    await page.keyboard.press("Escape");
    await page.evaluate(() => document.querySelector(".loose-section").remove());

    await page.waitForFunction(() => !document.body.classList.contains("picking"));
    resumed = 0;
    await button.click();
    check("pressing it restarts the sweeps", await waitFor(() => resumed, (count) => count === 1), `${resumed} presses reached the server`);
    check("the button goes once sweeps run", await waitFor(() => button.isHidden(), Boolean), "still shown");

    paused = true;
    await page.reload();
    await button.waitFor({ timeout: 30000 });
    const top = await button.evaluate((el) => ({ parent: el.parentElement.className, after: el.nextElementSibling?.className }));
    check("on /classic it sits in the top bar before Feedback and Update", top.parent === "top-right" && top.after === "key-tray", JSON.stringify(top));
    const icon = await button.evaluate((el) => ({
      width: el.getBoundingClientRect().width, label: getComputedStyle(el.querySelector(".label")).display,
      tip: el.dataset.tip || el.title, right: document.querySelector(".key-tray").getBoundingClientRect().right,
    }));
    check("there it is an icon with its words in its tooltip, and the top bar's keys stay on screen",
      icon.width <= 32 && icon.label === "none" && icon.tip.startsWith("Restart sweeps") && icon.right <= 1280, JSON.stringify(icon));
    check("no page errors", errors.length === 0, errors.join(" | "));
  } finally {
    // A state request still on its way when the page closes would end the run with its own error.
    if (context) await context.unrouteAll({ behavior: "ignoreErrors" });
    if (browser) await browser.close();
    server.kill();
  }
  const failedChecks = results.filter((result) => !result.ok);
  console.error(failedChecks.length ? `${failedChecks.length} of ${results.length} checks failed` : `all ${results.length} checks passed`);
  process.exitCode = failedChecks.length ? 1 : 0;
}

// The value `read` gives once `ok` holds it, or its last value after 3 s.
async function waitFor(read, ok) {
  const end = Date.now() + 3000;
  let value = await read();
  while (!ok(value) && Date.now() < end) {
    await sleep(100);
    value = await read();
  }
  return ok(value);
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
