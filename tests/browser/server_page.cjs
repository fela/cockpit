// The Server page (SPEC TRACK-16): the machine over a range, what runs right now, processor and memory charts by kind
// of program, the tasks and parts that used most, and the day-by-day totals. The ranges switch, a chart's hover names
// the slot and its figures, the task names link to their tasks, and nothing is wider than the window.
//
// Starts serve_resources.py (serve_preview.py's synthetic board plus eight days of made-up readings) and drives a
// headless Chromium. Prints one line per check and exits 1 when one failed. The page is saved as server-1280.png,
// server-1280-dark.png and server-390.png in the work folder.
//
// Usage, from the checkout: node tests/browser/server_page.cjs

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

const overflow = () => document.documentElement.scrollWidth - window.innerWidth;

// What the page drew: each section's heading, the charts' bands and bars, and the tables' first column.
function drawn() {
  const rows = (id) => [...document.querySelectorAll(`#${id} tbody tr`)].map((row) => row.querySelector("th, td").innerText.trim());
  return {
    headings: [...document.querySelectorAll("main h2")].map((el) => el.textContent),
    machine: document.getElementById("server-machine").innerText,
    now: rows("server-now"),
    cpuBands: document.querySelectorAll("#server-cpu .band").length,
    memoryBands: document.querySelectorAll("#server-memory .band").length,
    bars: document.querySelectorAll("#server-daily .bar").length,
    tasks: rows("server-tasks"),
    taskLinks: [...document.querySelectorAll("#server-tasks .name-col a")].map((a) => a.getAttribute("href")),
    parts: rows("server-parts"),
    legend: [...document.querySelectorAll("#server-cpu .legend li")].map((li) => li.innerText.trim()),
    pressed: document.querySelector(".server-ranges [aria-pressed='true']")?.dataset.range || "",
  };
}

async function main() {
  const work = workFolder("cockpit-server-page-");
  const server = spawn(PYTHON, [path.join(__dirname, "serve_resources.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const info = await harnessUp(work, server);
    browser = await chromium.launch({ executablePath: CHROMIUM });
    const pageErrors = [];
    for (const [name, viewport, scheme] of [["1280", { width: 1280, height: 900 }, "light"], ["1280-dark", { width: 1280, height: 900 }, "dark"], ["390", { width: 390, height: 844 }, "light"]]) {
      const context = await browser.newContext({ viewport, colorScheme: scheme, deviceScaleFactor: 2 });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      page.on("pageerror", (error) => pageErrors.push(error.message));
      await page.goto(`${info.origin}/server`);
      await page.waitForSelector("#server-daily .bar", { timeout: 30e3 });
      await page.evaluate(() => document.fonts.ready);
      const day = await page.evaluate(drawn);
      check(`${name}: the 24 hours range is pressed`, day.pressed === "day", day.pressed);
      check(`${name}: the machine section reads its hours measured`, /Measured for/.test(day.machine), day.machine);
      check(`${name}: processor and memory charts draw a band per kind of program (${day.cpuBands}, ${day.memoryBands})`,
        day.cpuBands >= 4 && day.memoryBands >= 4, JSON.stringify(day));
      check(`${name}: the legend names the families`, day.legend.some((words) => /Claude agents/.test(words)) && day.legend.some((words) => /Tests/.test(words)), day.legend.join(" | "));
      check(`${name}: right now lists the busiest task`, day.now.length >= 2, day.now.join(" | "));
      check(`${name}: the tasks table names the fixture's tasks and links them`, day.tasks.length >= 3 && day.taskLinks.length >= 1, JSON.stringify(day.tasks));
      check(`${name}: the parts table leads with the tests`, day.parts[0] === "Python tests", day.parts.join(" | "));
      check(`${name}: day by day draws a bar per day`, day.bars >= 8, String(day.bars));
      check(`${name}: nothing is wider than the window`, (await page.evaluate(overflow)) <= 0, String(await page.evaluate(overflow)));
      await page.screenshot({ path: path.join(work, `server-${name}.png`), fullPage: true });

      if (name === "1280") {
        const box = await page.locator("#server-cpu .hit").boundingBox();
        await page.mouse.move(box.x + box.width * 0.9, box.y + box.height / 2);
        const tip = await page.locator("#server-cpu .chart-tip").innerText();
        check("hovering the processor chart names the slot, each family and the cores in use",
          /to/.test(tip) && /cores/i.test(tip) && /Tests|Cockpit/.test(tip) && !/object/.test(tip), tip);
        await page.screenshot({ path: path.join(work, "server-1280-hover.png"), clip: { x: 0, y: box.y - 60, width: 1280, height: box.height + 160 } });

        for (const range of ["week", "month"]) {
          await page.click(`.server-ranges [data-range='${range}']`);
          await page.waitForFunction((wanted) => document.querySelector(".server-ranges [aria-pressed='true']")?.dataset.range === wanted
            && new URLSearchParams(location.search).get("range") === wanted, range, { timeout: 15e3 });
          await page.waitForSelector("#server-cpu .band", { timeout: 15e3 });
          const other = await page.evaluate(drawn);
          check(`the ${range} range draws its charts and keeps the tasks`, other.cpuBands >= 4 && other.tasks.length >= 3, JSON.stringify(other));
        }
      }
      await context.close();
    }
    check("no page errors", pageErrors.length === 0, pageErrors.join(" | "));
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failedChecks = results.filter((result) => !result.ok);
  console.error(`${results.length - failedChecks.length}/${results.length} checks passed; work dir ${work}`);
  process.exitCode = failedChecks.length ? 1 : 0;
}

// The harness's cookie file, once it is written and the server answers.
async function harnessUp(work, server) {
  const file = path.join(work, "cookie.json");
  const end = Date.now() + 180e3;
  while (Date.now() < end) {
    if (server.exitCode !== null) throw new Error(`serve_resources.py exited with ${server.exitCode}`);
    if (fs.existsSync(file)) {
      const info = JSON.parse(fs.readFileSync(file, "utf8"));
      const up = await fetch(`${info.origin}/healthz`).then((response) => response.ok, () => false);
      if (up) return info;
    }
    await sleep(200);
  }
  throw new Error("serve_resources.py did not come up within 180 s");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
