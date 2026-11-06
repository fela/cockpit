// The Usage page with Codex beside Claude, compact (SPEC SESSION-10), at 1280 and 390 px in both themes: a meter
// for each Codex account signed in on the host among the weekly limits, with their sum when there are several, its chart in the last 30 days, its tile among the totals, its card under "Where it
// went" and the "Of its week" column in its table; thin bars and little padding; nothing wider than the window.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs)
// and drives a headless Chromium. The page waits for Cockpit's first count of the transcripts, which takes minutes
// on synthetic records. Prints one line per check, saves the weekly limits at 1280 px in the light look as
// limits.png in the work folder, and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/usage_codex.cjs

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
// Two synthetic accounts, each with its own weekly reading and session.
const CODEX_ACCOUNTS = 2;
const COUNTED_MS = 30e3;

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// What the page shows of Codex, and how compact it is.
function pageNow() {
  const text = (el) => (el ? el.textContent.replace(/\s+/g, " ").trim() : "");
  const px = (el, property) => (el ? parseFloat(getComputedStyle(el)[property]) : NaN);
  const card = document.querySelector(".spend-card.codex");
  return {
    meters: [...document.querySelectorAll("#spend-limits .meter")].map(text).filter((line) => line.startsWith("Codex")),
    chart: text(document.querySelector(".daily-chart.codex .daily-title")),
    tile: text(document.querySelector(".spend-readout.codex")),
    cardRows: card ? card.querySelectorAll(".spend-rank li").length : 0,
    cardPadding: px(card, "paddingTop"),
    rankBar: px(document.querySelector(".spend-rank .rank-bar"), "height"),
    column: [...document.querySelectorAll(".codex-table th")].map(text),
    overflow: document.documentElement.scrollWidth - window.innerWidth,
  };
}

async function main() {
  const work = workFolder("cockpit-usage-codex-");
  const now = Date.now();
  const entries = Array.from({ length: CODEX_ACCOUNTS }, (_, n) => {
    const id = `aaaaaaaa-aaaa-4aaa-8aaa-${String(n).padStart(12, "0")}`;
    const counts = { input_tokens: 1000, cached_input_tokens: 100, output_tokens: 100, total_tokens: 1100 };
    return [
      { type: "session_meta", timestamp: new Date(now - 60000).toISOString(),
        payload: { id, cwd: "/home/sam/dev/app", source: "exec" } },
      { type: "event_msg", timestamp: new Date(now - 50000).toISOString(), payload: {
        type: "token_count", info: { total_token_usage: counts, last_token_usage: counts },
        rate_limits: { limit_id: "codex", plan_type: "pro", primary: {
          used_percent: 20 + n * 10, window_minutes: 10080, resets_at: Math.round(now / 1000 + 4 * 86400),
        }, secondary: null },
      } },
    ];
  });
  fs.writeFileSync(path.join(work, "codex-usage.json"), JSON.stringify(entries));
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
    for (const width of [1280, 390]) {
      for (const scheme of ["light", "dark"]) {
        const context = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: scheme, isMobile: width < 600 });
        await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
        const page = await context.newPage();
        page.on("pageerror", (error) => pageErrors.push(error.message));
        await page.goto(`${info.origin}/usage`);
        await page.waitForSelector(".spend-card.codex", { timeout: COUNTED_MS });
        // Cockpit's first count reads the Codex accounts one after another, and the page asks again every minute.
        await page.waitForFunction((count) => [...document.querySelectorAll("#spend-limits .meter")]
          .filter((el) => el.textContent.startsWith("Codex · ") && /%/.test(el.querySelector(".spend-figure").textContent)).length >= count,
        CODEX_ACCOUNTS, { timeout: COUNTED_MS }).catch(() => {});
        await page.evaluate(() => { document.getElementById("spend-more").open = true; });
        await page.evaluate(() => document.fonts.ready);
        const now = await page.evaluate(pageNow);
        if (width === 1280 && scheme === "light") await page.locator("#spend-limits").screenshot({ path: path.join(work, "limits.png") });
        await context.close();
        const where = `${width} px ${scheme}`;
        const accounts = now.meters.filter((line) => line.startsWith("Codex · "));
        check(`${where}: a weekly meter for each Codex account`, accounts.length === CODEX_ACCOUNTS && accounts.every((line) => /%/.test(line)),
          now.meters.join(" | "));
        check(`${where}: the Codex accounts' sum when there are several`,
          (CODEX_ACCOUNTS > 1) === now.meters.some((line) => line.startsWith("Codex, all accounts")), now.meters.join(" | "));
        check(`${where}: Codex's chart in the last 30 days`, now.chart === "Codex, as a share of its own weekly limit", now.chart);
        check(`${where}: Codex's tile`, /%.*Codex, of .*(own weekly limit|weeks in this period allow)/.test(now.tile), now.tile);
        check(`${where}: Codex sessions card with rows`, now.cardRows > 0, `${now.cardRows} rows`);
        check(`${where}: "Of its week" column in the Codex table`, now.column.includes("Of its week"), now.column.join(", "));
        check(`${where}: cards padded at most 8 px, rank bars at most 3 px`, now.cardPadding <= 8 && now.rankBar <= 3,
          `padding ${now.cardPadding}, bar ${now.rankBar}`);
        check(`${where}: nothing wider than the window`, now.overflow <= 0, `${now.overflow} px over`);
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
