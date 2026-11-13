// The priority pick in the Add dialog (SPEC CONTROL-10). Checks, on the main page at 1280 px dark and 390 px light:
// - the dialog's foot has a Priority button that shows Normal when nothing is picked;
// - it opens a list of Urgent, High, Normal and Low, Normal marked as the default and checked;
// - Escape closes the list alone, with the dialog still open and the focus back on the button;
// - picking High shows High on the button, and the Add sends priority "high", which the server stores as the user's;
// - the next Add starts at Normal again, and sends no priority, so the words of the text still set it;
// - nothing is wider than the screen.
// Saves screenshots of the closed and open pick per width in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on synthetic data, fake sessions; README.md says what it needs)
// and drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/add_priority.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-add-priority-");
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
    for (const [width, scheme] of [[1280, "dark"], [390, "light"]]) {
      const context = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: scheme });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      await page.addLocatorHandler(page.locator("dialog#idle[open]"), (dialog) => dialog.locator("button", { hasText: "Return" }).click()
        .catch((error) => { if (!/has been closed/.test(error.message)) throw error; }));
      page.setDefaultTimeout(5000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      const adds = [];
      page.on("response", async (response) => {
        const request = response.request();
        if (request.method() === "POST" && new URL(request.url()).pathname === "/api/items/add") {
          adds.push({ sent: JSON.parse(request.postData()), got: await response.json().catch(() => null) });
        }
      });
      await page.goto(`${info.origin}/classic`, { timeout: 30000 });
      await page.locator(".layout > .primary").waitFor({ timeout: 30000 });
      const area = page.locator('#add-box textarea[data-key="add:new"]');
      const button = page.locator("#add-box .add-priority > summary");
      const list = page.locator("#add-box .add-priority .menu-list");
      const wide = () => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);

      await page.locator("#add").click();
      await area.waitFor();
      check(`${width} px: the Priority button shows Normal with nothing picked`,
        (await button.getAttribute("aria-label")) === "Priority: Normal priority" && (await button.innerText()).trim() === "Normal",
        `${await button.getAttribute("aria-label")} / ${await button.innerText()}`);
      await sleep(250);
      await page.screenshot({ path: path.join(work, `add-priority-closed-${width}-${scheme}.png`) });

      await button.click();
      await list.waitFor();
      const levels = await list.locator("button").evaluateAll((buttons) => buttons.map((b) => ({
        level: b.dataset.priority, text: b.innerText.replace(/\s+/g, " ").trim(), current: b.getAttribute("aria-current") })));
      check(`${width} px: the list holds Urgent, High, Normal and Low, Normal the default and checked`,
        levels.map((each) => each.level).join() === "urgent,high,normal,low"
          && levels[2].text.includes("default") && levels[2].current === "true" && levels.filter((each) => each.current).length === 1,
        JSON.stringify(levels));
      const box = await list.boundingBox();
      check(`${width} px: the open list fits the screen`, box.x >= 0 && box.x + box.width <= width && (await wide()) <= 0,
        JSON.stringify({ box, wide: await wide() }));
      await sleep(250);
      await page.screenshot({ path: path.join(work, `add-priority-open-${width}-${scheme}.png`) });

      await page.keyboard.press("Escape");
      check(`${width} px: Escape closes the list and leaves the dialog open, focus on the button`,
        (await list.isHidden()) && (await area.isVisible())
          && (await page.evaluate(() => document.activeElement && document.activeElement.matches("#add-box .add-priority > summary"))),
        await page.evaluate(() => document.activeElement && document.activeElement.outerHTML.slice(0, 80)));

      await button.click();
      await list.locator('button[data-priority="high"]').click();
      check(`${width} px: picking High shows High on the button`,
        (await list.isHidden()) && (await button.innerText()).trim() === "High" && (await button.getAttribute("data-priority")) === "high",
        await button.innerText());
      await sleep(250);
      await page.screenshot({ path: path.join(work, `add-priority-picked-${width}-${scheme}.png`) });

      const text = `Renew the passport ${width}`;
      await area.fill(text);
      await area.press("Enter");
      const end = Date.now() + 20e3;
      while (!adds.some((each) => each.sent.text === text) && Date.now() < end) await sleep(200);
      const first = adds.find((each) => each.sent.text === text);
      check(`${width} px: the Add sends priority high and the server stores it as the user's`,
        first && first.sent.priority === "high" && first.got && first.got.priority === "high" && first.got.user_fields.includes("priority"),
        JSON.stringify(first && { sent: first.sent.priority, got: first.got && first.got.priority, fields: first.got && first.got.user_fields }));

      await page.locator("#add").click();
      await area.waitFor();
      check(`${width} px: the next Add starts at Normal`, (await button.innerText()).trim() === "Normal", await button.innerText());
      const urgent = `Urgent: the export fails ${width}`;
      await area.fill(urgent);
      await area.press("Enter");
      const end2 = Date.now() + 20e3;
      while (!adds.some((each) => each.sent.text === urgent) && Date.now() < end2) await sleep(200);
      const second = adds.find((each) => each.sent.text === urgent);
      check(`${width} px: with nothing picked the Add sends no priority and the words set it`,
        second && second.sent.priority === null && second.got && second.got.priority === "urgent",
        JSON.stringify(second && { sent: second.sent.priority, got: second.got && second.got.priority }));
      check(`${width} px: no page errors`, errors.length === 0, errors.join("; "));
      await context.close();
    }
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  console.error(`screenshots in ${work}`);
  const failed = results.filter((each) => !each.ok);
  console.error(`${results.length - failed.length} of ${results.length} checks passed`);
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
