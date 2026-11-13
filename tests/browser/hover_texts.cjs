// Hover texts stay short (SPEC UI-46, UI-27): on a synthetic database, every hover text the main page and the
// classic page give, with their folded parts open and the first items' cards shown, is a name or a short result of at
// most two lines of 80 characters (a name and one detail, UI-27). Values (an item's or step's own words, a rule) may
// run longer; the tooltip shows at most six lines of 120 characters of them.

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

const LINES = 2;
const LINE = 80;
// Elements whose hover text is a value written by an agent or by the user, or a session's own name and folder, shown
// in full up to the tooltip's own limit.
const VALUES = [".side-row", ".row-title", ".row-sub", ".hist-tag", ".rule-sentence", ".time-part", ".checked",
  ".chip.blocks", ".closed-by", ".composer-target select"];

// Every element's hover text: its title, or the data-tip the page moved it to.
const harvest = (values) => [...document.querySelectorAll("[title], [data-tip]")].map((el) => ({
  text: el.getAttribute("title") || el.dataset.tip || "",
  what: `${el.tagName.toLowerCase()}.${[...el.classList].join(".")}`,
  value: values.some((selector) => el.matches(selector)),
})).filter((found) => found.text);

async function main() {
  const work = workFolder("cockpit-hover-texts-");
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
    browser = await chromium.launch({ executablePath: CHROMIUM });
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
    const page = await context.newPage();
    page.setDefaultTimeout(30000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const found = new Map();
    const collect = async () => {
      await page.evaluate(() => document.querySelectorAll("details").forEach((folded) => { folded.open = true; }));
      await sleep(300);
      for (const text of await page.evaluate(harvest, VALUES)) found.set(`${text.what}\u0000${text.text}`, text);
    };
    for (const where of ["/classic", "/"]) {
      await page.goto(`${info.origin}${where}`);
      await page.locator(".side-row, .row").first().waitFor();
      await sleep(1500);
      await collect();
    }
    // The first items' cards, as a press on their rows shows them.
    const rows = await page.locator("li.side-item[data-item] > button.side-row").count();
    for (let row = 0; row < Math.min(rows, 15); row += 1) {
      await page.evaluate((n) => document.querySelectorAll("li.side-item[data-item] > button.side-row")[n]?.click(), row);
      await sleep(500);
      await collect();
    }
    const texts = [...found.values()];
    console.error(`  ${texts.length} hover texts`);
    const long = texts.filter((text) => !text.value)
      .filter((text) => text.text.split("\n").length > LINES || text.text.split("\n").some((line) => line.length > LINE));
    check(`each hover text that is not a value has at most ${LINES} lines of ${LINE} characters`, long.length === 0,
      long.map((text) => `\n    ${text.what}: ${text.text.replace(/\n/g, " ⏎ ")}`).join(""));

    // A value that runs long shows only its start: the tooltip is drawn with at most six lines of 120 characters,
    // an exact time among them. A fixed box of the test's own, which no redraw replaces, holds eight long lines.
    for (const time of [false, true]) {
      await page.mouse.move(1, 1);
      await page.evaluate((withTime) => {
        document.getElementById("long-tip")?.remove();
        const box = Object.assign(document.createElement("button"), { id: "long-tip", textContent: "long" });
        Object.assign(box.style, { position: "fixed", left: "600px", top: "450px", zIndex: 99999 });
        box.dataset.tip = `${"word ".repeat(80)}\n${"two ".repeat(60)}\nthree\nfour\nfive\nsix\nseven\neight`;
        if (withTime) box.dataset.time = new Date().toISOString();
        document.body.append(box);
      }, time);
      await page.locator("#long-tip").hover({ timeout: 5000 });
      const shown = await waitFor(() => page.evaluate(() => {
        const tip = document.querySelector(".tip");
        return tip && !tip.hidden ? [...tip.children].map((part) => part.textContent) : null;
      }), (lines) => Array.isArray(lines) && /^word word/.test(lines[0] || ""));
      check(`a long value's tooltip${time ? " with an exact time" : ""} shows six lines of at most 120 characters`,
        Array.isArray(shown) && shown.length === 6 && shown.every((line) => line.length <= 120) && shown[0].endsWith("…")
          && (time ? !shown.includes("six") : shown[5] === "six"), JSON.stringify(shown));
    }
    check("no page errors", errors.length === 0, errors.join(" | "));
    await context.close();
  } finally {
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
  return value;
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
