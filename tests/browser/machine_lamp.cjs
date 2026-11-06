// The host's rack in the header (SPEC UI-69, UI-73): three slots after the weekly-limit lanes, centred on them, in
// one colour on the smooth green-to-orange scale; its hover card names the three readings, each in its own colour. Checked in both themes on the main page, the classic page and a phone, with
// screenshots of the header and the card.
// Fixed machine readings keep these layout checks independent of the host's load readers.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs)
// and drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/machine_lamp.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");
const { MACHINE, routeMachine } = require("./machine_fixture.cjs");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const ROOT = path.resolve(__dirname, "..", "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const rowColoursMatch = (rows, machine) => rows.length === 3
  && [machine.cpu, machine.memory, machine.disk].every((level, i) => rows[i][0].split(" ").includes(level));

function cardRows(els, crossedRow) {
  const original = els[crossedRow]?.className;
  try {
    // A crossed reading uses the processor's status for one other row.
    if (crossedRow !== null) els[crossedRow].className = els[0].className;
    return els.map((el) => [el.className, el.textContent]);
  } finally {
    if (crossedRow !== null) els[crossedRow].className = original;
  }
}

async function main() {
  const work = workFolder("cockpit-machine-");
  // Both usage families supply the lane-and-divider geometry beside the host indicator.
  const now = Date.now();
  fs.writeFileSync(path.join(work, "codex-usage.json"), JSON.stringify([[
    { type: "session_meta", timestamp: new Date(now - 60000).toISOString(),
      payload: { id: "aaaaaaaa-aaaa-4aaa-8aaa-000000000000", cwd: "/tmp/tasks", source: "exec" } },
    { type: "event_msg", timestamp: new Date(now - 50000).toISOString(), payload: {
      type: "token_count", info: {
        total_token_usage: { input_tokens: 1000, cached_input_tokens: 100, output_tokens: 100, total_tokens: 1100 },
      },
      rate_limits: { limit_id: "codex", plan_type: "pro", primary: {
        used_percent: 20, window_minutes: 10080, resets_at: Math.round(now / 1000 + 4 * 86400),
      }, secondary: null },
    } },
  ]]));
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
    const pageErrors = [];
    for (const scheme of ["light", "dark"]) {
      // The main page keeps the lanes in its rail's foot, which a phone hides; the classic page has them in its top bar.
      const desktop = { width: 1440, height: 900 };
      for (const [size, viewport, pagePath] of [["desktop", desktop, "/"], ["classic", desktop, "/classic"], ["phone", { width: 390, height: 844 }, "/classic"]]) {
        const where = `${scheme}, ${size}`;
        const context = await browser.newContext({ viewport, colorScheme: scheme, deviceScaleFactor: 2 });
        await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
        const page = await context.newPage();
        await routeMachine(page, MACHINE);
        // Pointer activity clears the fixture timer's idle veil before a hover lands.
        await page.addLocatorHandler(page.locator("#idle-veil:popover-open"), async () => {
          await page.mouse.move(10, 10);
          await page.mouse.move(20, 20);
        }, { noWaitAfter: true });
        page.on("pageerror", (error) => pageErrors.push(error.message));
        await page.goto(`${info.origin}${pagePath}`);
        await page.waitForSelector("#usage .machine-lamp", { state: "attached", timeout: 20e3 });
        await page.evaluate(() => document.fonts.ready);
        const machine = await page.evaluate(async () => (await (await fetch("/api/state")).json()).usage.machine);
        check(`${where}: later state requests keep the fixed readings`, JSON.stringify(machine) === JSON.stringify(MACHINE), JSON.stringify(machine));
        await page.waitForSelector("#usage .usage.caps .machine-lamp", { timeout: 5e3 });
        const lamp = await page.evaluate(() => {
          const el = document.querySelector("#usage .machine-lamp");
          const rack = el.querySelector(".machine-rack").getBoundingClientRect();
          const lane = document.querySelector("#usage .lane");
          const middle = (box) => box.top + box.height / 2;
          return {
            classes: el.className,
            colour: getComputedStyle(el).color,
            ramp: el.style.getPropertyValue("--ramp-light"),
            slots: el.querySelectorAll(".machine-rack rect[fill-opacity]").length,
            middle: middle(rack),
            laneMiddle: lane ? middle(lane.getBoundingClientRect()) : null,
            label: el.getAttribute("aria-label"),
            headerWidth: document.querySelector(".top-inner").scrollWidth, pageWidth: document.documentElement.clientWidth,
          };
        });
        check(`${where}: rack has three slots`, lamp.slots === 3, String(lamp.slots));
        check(`${where}: rack coloured on the scale (${lamp.ramp})`, lamp.classes.split(" ").includes("ramp") && /^oklch/.test(lamp.ramp), lamp.colour);
        check(`${where}: rack centred on the lanes (${lamp.middle}/${lamp.laneMiddle})`,
          lamp.laneMiddle === null || Math.abs(lamp.middle - lamp.laneMiddle) < 1, JSON.stringify(lamp));
        check(`${where}: header fits the page`, lamp.headerWidth <= lamp.pageWidth, `${lamp.headerWidth} > ${lamp.pageWidth}`);
        if (size === "desktop") {
          // The rail's foot stays one line with everything in it, Reload included, and its parts stand evenly apart
          // across the whole line, with Reload and without: the gaps between the visible marks, dividers, rack, cloud
          // and Reload are equal, and the last part ends at the line's end.
          const feet = await page.evaluate(() => {
            const reload = document.getElementById("reload");
            const hidden = reload.hidden;
            const measure = (showing) => {
              reload.hidden = !showing;
              const line = document.querySelector(".rail-line").getBoundingClientRect();
              // A family runs from its mark to its lane's end, so a collapsed lane shows in its width.
              const lanes = [...document.querySelectorAll(".rail-foot .lane")].map((lane) => Math.round(lane.getBoundingClientRect().width));
              const parts = [...document.querySelectorAll(".rail-foot :is(.fam, .fam-sep, .machine-rack), #reload:not([hidden])")]
                .map((el) => {
                  const box = el.getBoundingClientRect();
                  const lane = el.matches(".fam") && el.querySelector(".lane").getBoundingClientRect();
                  return lane ? { left: el.querySelector(".fam-mark").getBoundingClientRect().left, right: lane.right, width: lane.right - box.left } : box;
                }).filter((box) => box.width > 0);
              const gaps = parts.slice(1).map((box, i) => Math.round((box.left - parts[i].right) * 10) / 10);
              return { showing, height: line.height, lanes, gaps, end: Math.round((line.right - parts[parts.length - 1].right) * 10) / 10 };
            };
            const out = [measure(true), measure(false)];
            reload.hidden = hidden;
            return out;
          });
          for (const foot of feet) {
            const label = foot.showing ? "with Reload" : "without Reload";
            if (foot.showing) check(`${where}: the rail's foot is one line with Reload showing (${foot.height} px)`, foot.height <= 34, String(foot.height));
            check(`${where}: the foot's parts evenly spaced ${label} (${foot.gaps.join(", ")})`,
              foot.gaps.length >= 4 && foot.gaps.every((gap) => Math.abs(gap - foot.gaps[0]) <= 1), JSON.stringify(foot));
            check(`${where}: the foot fills its line ${label} (${foot.end} px left)`, Math.abs(foot.end) <= 1, JSON.stringify(foot));
            check(`${where}: the lanes keep their length ${label} (${foot.lanes.join(", ")} px)`, foot.lanes.length >= 1 && foot.lanes.every((width) => width >= 40), JSON.stringify(foot));
          }
        }
        check(`${where}: label names the readings`, /Processor/.test(lamp.label) && /Memory/.test(lamp.label) && /Disk/.test(lamp.label), lamp.label);
        await page.locator("#usage").screenshot({ path: path.join(work, `header-${scheme}-${size}.png`) });
        if (size === "desktop") {
          await page.hover("#usage .machine-lamp");
          await page.waitForSelector(".tip .machine-card", { timeout: 5e3 });
          for (const [index, reading] of [[1, "memory"], [2, "disk"]]) {
            const broken = await page.$$eval(".tip .machine-card .mc-row", cardRows, index);
            check(`${where}: colour check rejects ${reading} using the processor's status`,
              !rowColoursMatch(broken, MACHINE), JSON.stringify(broken));
          }
          const rows = await page.$$eval(".tip .machine-card .mc-row", cardRows, null);
          check(`${where}: card has processor, memory and disk rows`, rows.length === 3 && /Processor/.test(rows[0][1]) && /Memory/.test(rows[1][1]) && /Disk/.test(rows[2][1]), JSON.stringify(rows));
          check(`${where}: each row in its reading's colour`,
            rowColoursMatch(rows, MACHINE), JSON.stringify(rows));
          console.error(`    ${rows.map((row) => row[1]).join(" | ")}`);
          const box = await page.locator(".tip").boundingBox();
          const head = await page.locator("#usage").boundingBox();
          const x = Math.max(0, Math.min(head.x, box.x) - 12);
          const y = Math.max(0, Math.min(head.y, box.y) - 12);
          const right = Math.max(head.x + head.width, box.x + box.width) + 12;
          const bottom = Math.max(head.y + head.height, box.y + box.height) + 12;
          await page.screenshot({ path: path.join(work, `card-${scheme}.png`), clip: { x, y, width: right - x, height: bottom - y } });
        }
        await page.unrouteAll({ behavior: "wait" });
        await context.close();
      }
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
  const end = Date.now() + 120e3;
  while (Date.now() < end) {
    if (server.exitCode !== null) throw new Error(`serve_preview.py exited with ${server.exitCode}`);
    if (fs.existsSync(file)) {
      const info = JSON.parse(fs.readFileSync(file, "utf8"));
      const up = await fetch(`${info.origin}/healthz`).then((response) => response.ok, () => false);
      if (up) return info;
    }
    await sleep(200);
  }
  throw new Error("serve_preview.py did not come up within 120 s");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
