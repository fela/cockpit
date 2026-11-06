// A long text in the Add box (SPEC CONTROL-8). Checks, on the main page at desktop and phone width:
// - a pasted text of about 4,800 characters is added, with no refusal toast;
// - the new item keeps the text's start as its summary and holds the whole text as a page of its own, which shows every
//   line;
// - the item's open row, opened as a search result opens it, shows the cut line and the page.
// Saves a screenshot of the open row per width in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a copy of the live database, fake sessions; README.md says what
// it needs) and drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/add_long_text.cjs

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
const escapeHtml = (text) => text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

async function main() {
  const work = workFolder("cockpit-add-long-");
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
    for (const width of [1280, 390]) {
      const context = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: "dark" });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      page.setDefaultTimeout(60000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(`${info.origin}/classic`);
      await page.locator(".layout > .primary").waitFor();
      await page.locator("#add").click();
      const title = `Read this pasted log ${width}`;
      const lines = Array.from({ length: 150 }, (_, n) => `line ${String(n).padStart(3, "0")} <b>pasted</b> & logged`);
      const text = `${title}\n${lines.join("\n")}`;
      const box = page.locator('#add-box [data-key="add:new"]');
      await box.fill(text);
      await box.press("Enter");

      let item = null;
      const end = Date.now() + 60e3;
      while (!item && Date.now() < end) {
        const state = await page.evaluate(async () => (await fetch("/api/state")).json());
        item = state.items.find((each) => each.title === title && typeof each.id === "number") || null;
        if (!item) await sleep(200);
      }
      const toasts = await page.locator(".toast").allInnerTexts().catch(() => []);
      check(`${width}: ${text.length} characters added`, item !== null && !toasts.some((t) => t.startsWith("Not added")),
        `item ${item ? "made" : "missing"}, toasts ${JSON.stringify(toasts)}`);
      if (!item) { await context.close(); continue; }
      const [whole] = item.pages || [];
      check(`${width}: summary keeps the start and names the page`,
        item.summary.length < 2300 && item.summary.startsWith(title) && whole && item.summary.includes(`page get ${whole.id}`),
        `${item.summary.length} characters, pages ${JSON.stringify(item.pages)}`);
      const shown = whole ? await page.evaluate(async (id) => (await fetch(`/pages/${id}`)).text(), whole.id) : "";
      check(`${width}: the page holds every line`, lines.every((line) => shown.includes(escapeHtml(line))),
        `${shown.length} characters of HTML`);

      // The page opens only items its own state holds, which its next poll brings.
      await page.waitForFunction((id) => ui.state?.items.some((entry) => entry.id === id), item.id);
      await page.evaluate((id) => openKeyboardItem(id), item.id);
      const cut = page.getByText(`The text continues on this item's page "Your whole text"`).first();
      const shownCut = await cut.waitFor({ state: "visible" }).then(() => true, () => false);
      check(`${width}: the open row shows the cut line`, shownCut, "no visible cut line");
      const row = shownCut ? cut.locator("xpath=ancestor::*[contains(@class,'row')][1]") : page.locator("body");
      const wide = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      check(`${width}: no sideways scroll`, wide <= 0, `${wide}px`);
      await row.screenshot({ path: path.join(work, `long-text-${width}.png`) }).catch(() => {});
      check(`${width}: no page errors`, errors.length === 0, errors.join("; "));
      await context.close();
    }
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  console.error(`screenshots in ${work}`);
  if (results.some((result) => !result.ok)) process.exitCode = 1;
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
