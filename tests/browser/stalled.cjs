// Stalled agent work on a synthetic board (SPEC SWEEP-20): a high item's card and search row carry a red Stalled chip
// and the row a red edge; a normal item's are grey, with no edge. Each runs at desktop and phone width.

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder, fullState } = require("./work.cjs");

// Browser dependencies can be selected through the environment (README.md).
const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const SESSION_B = "22222222-2222-4222-8222-222222222222";
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "browser-check", "content-type": "application/json" };

async function call(info, headers, method, url, body) {
  const response = await fetch(`${info.origin}${url}`, { method, headers: { ...headers, connection: "close" },
    body: body === undefined ? undefined : JSON.stringify(body) });
  if (!response.ok) throw new Error(`${method} ${url}: HTTP ${response.status} ${await response.text()}`);
  return response.json();
}

async function main() {
  const work = workFolder("cockpit-stalled-");
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
    // Each check creates its own Tasks work, independent of the harness's initial board.
    const high = await call(info, AGENT, "PUT", "/api/items", {
      key: "note:stalled-reminders", title: "Build reminder picker", stage: "building", sessions: [SESSION_B],
    });
    const normal = await call(info, AGENT, "PUT", "/api/items", {
      key: "note:stalled-exports", title: "Add list export", stage: "building", sessions: [SESSION_B],
    });
    const since = new Date(Date.now() - 4 * 3600e3).toISOString().replace(/\.\d+Z$/, "Z");

    browser = await chromium.launch({ executablePath: CHROMIUM });
    for (const width of [1280, 390]) {
      const context = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: "dark" });
      await context.setExtraHTTPHeaders({ cookie });
      const page = await context.newPage();
      page.setDefaultTimeout(45000);
      page.setDefaultNavigationTimeout(60000);
      const errors = [];
      page.on("pageerror", (error) => { errors.push(error.message); console.error(`page error: ${error.message}`); });
      await page.route("**/api/state*", async (route) => {
        // A poll still under way when the context closes has nothing left to answer.
        const full = await fullState(route).catch(() => null);
        if (!full) return;
        const body = await full.response.json();
        for (const item of body.items) {
          if (item.id === high.id) Object.assign(item, { priority: "high", stalled_since: since });
          else if (item.id === normal.id) Object.assign(item, { priority: "normal", stalled_since: since });
          else item.stalled_since = null;
        }
        body.counts.stalled = 2;
        await route.fulfill({ ...full, json: body }).catch(() => {});
      });

      await page.goto(`${info.origin}/`);
      // A phone opens on the item screen, with the list out of view.
      await page.locator("#sidebar .side-item").first().waitFor({ state: "attached" });
      const chipLook = (chip) => chip.evaluate((el) => ({
        chip: el.textContent, raised: el.classList.contains("raised"), colour: getComputedStyle(el).color,
        connected: el.isConnected, card: el.closest("article")?.className,
      }));
      // Each item's card carries the chip: red on the high item, grey on the normal one.
      const cardChip = async (item, name) => {
        await page.evaluate((id) => showInSidebar(id), item.id);
        const chip = page.locator(`#next article.item-card[data-item="${item.id}"] .chip.stalled`);
        await chip.waitFor();
        // The poll can redraw the card between finding the chip and reading it, which reads no colour.
        const seen = await waitFor(() => chipLook(chip), (now) => now.colour !== "");
        // The poll redraws the card, so the screenshot takes the column that holds it.
        await page.locator("#next").screenshot({ path: path.join(work, `stalled-${name}-card-${width}.png`) });
        return seen;
      };
      const highCard = await cardChip(high, "high");
      const normalCard = await cardChip(normal, "normal");
      check(`${width} px: the high item's card says it stalled`, /^Stalled \S/.test(highCard.chip), highCard.chip);
      check(`${width} px: the high item's chip is red`, highCard.raised, JSON.stringify(highCard));
      check(`${width} px: the normal item's card says it stalled`, /^Stalled \S/.test(normalCard.chip), normalCard.chip);
      check(`${width} px: the normal item's chip is grey`, !normalCard.raised && normalCard.colour !== highCard.colour, JSON.stringify(normalCard));

      // A search lists the item as a row: the high row has the red edge, the normal row none.
      const look = (row) => row.evaluate((el) => {
        const chip = el.querySelector(".chip.stalled");
        return {
          stalledRow: el.classList.contains("stalled"), classes: el.className,
          edge: getComputedStyle(el, "::before").boxShadow,
          chip: chip ? chip.textContent : "",
          raised: chip ? chip.classList.contains("raised") : false,
          colour: chip ? getComputedStyle(chip).color : "",
        };
      });
      const foundRow = async (item, words, name) => {
        await page.evaluate((text) => setFind(text, false, null, true), words);
        const row = page.locator(`#work .row[data-item="${item.id}"]`).first();
        await row.waitFor();
        const seen = await waitFor(() => look(row), (now) => now.chip !== "" && (item !== high || /inset/.test(now.edge)));
        await page.locator("#work").screenshot({ path: path.join(work, `stalled-${name}-row-${width}.png`) });
        return seen;
      };
      const highLook = await foundRow(high, "reminder picker", "high");
      const normalLook = await foundRow(normal, "list export", "normal");
      check(`${width} px: the high row says it stalled`, /^Stalled \S/.test(highLook.chip), highLook.chip);
      check(`${width} px: the high row's chip is red`, highLook.raised, JSON.stringify(highLook));
      check(`${width} px: the high row has the red edge`, highLook.stalledRow && /inset/.test(highLook.edge), JSON.stringify(highLook));
      check(`${width} px: the normal row says it stalled`, /^Stalled \S/.test(normalLook.chip), normalLook.chip);
      check(`${width} px: the normal row's chip is grey`, !normalLook.raised && normalLook.colour !== highLook.colour, JSON.stringify(normalLook));
      check(`${width} px: the normal row has no edge`, !normalLook.stalledRow, JSON.stringify(normalLook));
      check(`${width} px: no page errors`, errors.length === 0, errors.join(" | "));
      await page.unrouteAll({ behavior: "ignoreErrors" });
      await context.close();
    }
    console.error(`  screenshots in ${work}`);
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failedChecks = results.filter((result) => !result.ok);
  console.error(failedChecks.length ? `${failedChecks.length} of ${results.length} checks failed` : `all ${results.length} checks passed`);
  process.exitCode = failedChecks.length ? 1 : 0;
}

// The value `read` gives once `ok` holds it, or its last value after 5 s.
async function waitFor(read, ok) {
  const end = Date.now() + 5000;
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
