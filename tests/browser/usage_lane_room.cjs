// A week whose use runs past the end of its lane still shows the room it has left (SPEC UI-57): a lane ends
// half a week after now, so 90 % used a tenth of the way into a week reaches past that end. Its lane then runs on to
// the week's reset, the bar ends there, and the bar's last tenth stays empty. A week that resets later still is cut
// at the lane's end, and its outline fades out there with no end of its own. The lane keeps the now line inside it and
// the rail foot still fits its one line.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) and drives a
// headless Chromium. Prints one line per check and exits 1 when one failed. The rail foot is saved as foot-light.png
// and foot-dark.png in the work folder.
//
// Usage, from the checkout: node tests/browser/usage_lane_room.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");
const { LOW_MACHINE, routeMachine } = require("./machine_fixture.cjs");

// Browser dependencies can be selected through the environment (README.md).
const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const ROOT = path.resolve(__dirname, "..", "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
// The harness's two signed-in accounts (serve_slow.py): the first nearly spends its week early on, the second has
// just begun its week, so it resets after the first and its bar is cut at the lane's end.
const EARLY = "sam@example.test";
const LATE = "alex@example.test";

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// The Claude lane, its bars and the solid part of the bar that runs ahead of now, in pixels.
function laneNow() {
  const lane = document.querySelector("#usage .lane.claude");
  const box = (el) => el.getBoundingClientRect();
  const over = lane.querySelector(".wk.over");
  const ahead = over && over.querySelector("i.a");
  const late = [...lane.querySelectorAll(".wk")].find((bar) => bar !== over);
  const outline = (bar) => bar && getComputedStyle(bar, "::before");
  const foot = document.querySelector(".rail-foot");
  return {
    lane: { left: box(lane).left, right: box(lane).right },
    now: box(lane.querySelector(".lane-now")).left,
    bar: over && { left: box(over).left, right: box(over).right },
    ahead: ahead && { left: box(ahead).left, right: box(ahead).right },
    bars: lane.querySelectorAll(".wk").length,
    overCut: over && over.classList.contains("cut"),
    late: late && { cut: late.classList.contains("cut"), right: box(late).right, fade: outline(late).maskImage || outline(late).webkitMaskImage },
    fits: foot.scrollWidth <= foot.clientWidth,
  };
}

async function main() {
  const work = workFolder("cockpit-lane-room-");
  const now = Date.now() / 1000;
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const info = await harnessUp(work, server);
    const weeks = {
      [EARLY]: { used_percentage: 90, resets_at: Math.round(now + 6.3 * 86400) },
      [LATE]: { used_percentage: 3, resets_at: Math.round(now + 6.65 * 86400) },
    };
    const rates = JSON.parse(fs.readFileSync(path.join(work, "rate-limits.json"), "utf8"));
    for (const [address, record] of Object.entries(rates.accounts)) {
      record.rate_limits.seven_day = weeks[address];
      record.captured_at = Math.round(now);
    }
    fs.writeFileSync(path.join(work, "rate-limits.json"), JSON.stringify(rates));
    browser = await chromium.launch({ executablePath: CHROMIUM });
    const pageErrors = [];
    for (const scheme of ["light", "dark"]) {
      const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: scheme, deviceScaleFactor: 3 });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      await routeMachine(page, LOW_MACHINE);
      page.on("pageerror", (error) => pageErrors.push(error.message));
      await page.goto(`${info.origin}/`);
      await page.waitForSelector("#usage .lane.claude .wk.over i.a", { state: "attached", timeout: 30e3 });
      await page.evaluate(() => document.fonts.ready);
      await page.locator(".rail-foot").screenshot({ path: path.join(work, `foot-${scheme}.png`) });
      const lane = await page.evaluate(laneNow);
      await page.unrouteAll({ behavior: "wait" });
      await context.close();

      const detail = JSON.stringify(lane);
      check(`${scheme}: the lane has both accounts' weeks`, lane.bars === 2, detail);
      // The bar that is 90 % used runs to its reset, which is where the lane now ends.
      check(`${scheme}: the early week's bar runs to the lane's end`, lane.bar && Math.abs(lane.bar.right - lane.lane.right) <= 1, detail);
      const share = lane.bar && lane.ahead && (lane.bar.right - lane.ahead.right) / (lane.bar.right - lane.bar.left);
      check(`${scheme}: the last tenth of that bar stays empty (${share && (share * 100).toFixed(1)} %)`, share > 0.08 && share < 0.12, detail);
      check(`${scheme}: the early week ends at its reset, uncut`, lane.overCut === false, detail);
      check(`${scheme}: the later week is cut at the lane's end`, lane.late && lane.late.cut && Math.abs(lane.late.right - lane.lane.right) <= 1, detail);
      check(`${scheme}: the cut week's outline fades out`, lane.late && /linear-gradient/.test(lane.late.fade || ""), detail);
      check(`${scheme}: the now line stays inside the lane`, lane.now > lane.lane.left && lane.now < lane.lane.right, detail);
      check(`${scheme}: the rail foot still fits its line`, lane.fits, detail);
    }
    check("no page errors", pageErrors.length === 0, pageErrors.join(" | "));
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failed = results.filter((result) => !result.ok);
  console.error(`${results.length - failed.length}/${results.length} checks passed; work dir ${work}`);
  process.exitCode = failed.length ? 1 : 0;
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
