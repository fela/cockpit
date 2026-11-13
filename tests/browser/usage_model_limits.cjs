// A Claude account's weekly limit that counts one model's use alone, beside the week all models share (SPEC UI-12,
// SESSION-10), on local accounts: the hover card names both weeks, "All models" and the model, each with its
// own bar and lines; an account without such a limit keeps its one unnamed bar; the display's spoken words give both
// figures; and the Usage page has a meter for the model's limit after the account's own.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) and drives a
// headless Chromium. Prints one line per check and exits 1 when one failed. The hover card and the Usage page's meters
// are saved as card.png and limits.png in the work folder.
//
// Usage, from the checkout: node tests/browser/usage_model_limits.cjs

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
// The harness's two signed-in accounts (serve_slow.py): the first has a limit for Fable alone, the second has none.
const WITH_MODEL = "sam@example.test";
const WITHOUT = "alex@example.test";

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// The hover card's accounts: each one's address, the names of its weeks, how many bars it has and its words.
function cardNow() {
  return [...document.querySelectorAll(".tip.tip-card .uc-account")].filter((el) => el.querySelector(".acct-bar")).map((el) => ({
    email: el.querySelector(".uc-email").textContent,
    labels: [...el.querySelectorAll(".uc-label")].map((label) => label.textContent),
    bars: [...el.querySelectorAll(".acct-bar")].map((bar) => getComputedStyle(bar).getPropertyValue("--used").trim()),
    widths: [...el.querySelectorAll(".acct-bar")].map((bar) => Math.round(bar.getBoundingClientRect().width)),
    text: el.innerText,
  }));
}

async function main() {
  const work = workFolder("cockpit-model-limits-");
  const now = Date.now();
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const info = await harnessUp(work, server);
    // Half a week gone: 38 % used is under an even pace, 62 % over it.
    fs.writeFileSync(path.join(work, "model-limits.json"), JSON.stringify({
      [WITH_MODEL]: [{ model: "Fable", reported: { used_percentage: 38, resets_at: Math.round(now / 1000 + 3.5 * 86400) } }],
    }));
    const rates = JSON.parse(fs.readFileSync(path.join(work, "rate-limits.json"), "utf8"));
    for (const record of Object.values(rates.accounts)) {
      record.rate_limits.seven_day = { used_percentage: 62, resets_at: Math.round(now / 1000 + 3.5 * 86400) };
      record.captured_at = Math.round(now / 1000);
    }
    fs.writeFileSync(path.join(work, "rate-limits.json"), JSON.stringify(rates));
    browser = await chromium.launch({ executablePath: CHROMIUM });
    const pageErrors = [];
    for (const scheme of ["light", "dark"]) {
      const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: scheme });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      await routeMachine(page, LOW_MACHINE);
      page.on("pageerror", (error) => pageErrors.push(error.message));
      await page.goto(`${info.origin}/classic`);
      await page.waitForSelector("#usage .lane.claude .wk.over", { state: "attached", timeout: 30e3 });
      const said = await page.getAttribute("#usage .usage.caps", "aria-label");
      await page.hover("#usage .lane.claude");
      await page.waitForSelector(".tip.tip-card .uc-weeks", { timeout: 15e3 });
      await page.evaluate(() => document.fonts.ready);
      const card = await page.evaluate(cardNow);
      if (scheme === "light") await page.locator(".tip.tip-card").screenshot({ path: path.join(work, "card.png") });
      await page.unrouteAll({ behavior: "wait" });
      await context.close();

      const one = card.find((account) => account.email === WITH_MODEL);
      const other = card.find((account) => account.email === WITHOUT);
      check(`${scheme}: the spoken words give the model's figure beside the week's`,
        /personal 62% \(Fable 38%\)/.test(said) && /work 62%$/.test(said), said);
      check(`${scheme}: an account with a model's limit names both weeks`, one && String(one.labels) === "All models,Fable", JSON.stringify(one));
      // Each bar is as wide as the single bar of an account with one week, within a pixel of rounding.
      check(`${scheme}: each of its weeks has its own bar, filled to its share`,
        one && other && String(one.bars) === "62%,38%" && one.widths.every((width) => Math.abs(width - other.widths[0]) <= 1), JSON.stringify([one, other]));
      check(`${scheme}: each of its weeks says its use and its reset`,
        one && /62% used[^]*Resets [^]*38% used[^]*Resets /.test(one.text), one && one.text);
      check(`${scheme}: an account without one keeps its single week`, other && String(other.labels) === "7d" && other.bars.length === 1, JSON.stringify(other));
    }

    // The Usage page: the model's meter follows its account's, named for the model.
    for (const width of [1280, 390]) {
      const context = await browser.newContext({ viewport: { width, height: 900 }, isMobile: width < 600 });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      page.on("pageerror", (error) => pageErrors.push(error.message));
      await page.goto(`${info.origin}/usage`);
      await page.waitForSelector("#spend-limits .meter", { timeout: 120e3 });
      await page.evaluate(() => document.fonts.ready);
      const meters = await page.evaluate(() => [...document.querySelectorAll("#spend-limits .meter")].map((el) => el.innerText.replace(/\s+/g, " ").trim()));
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      await page.locator("#spend-limits").screenshot({ path: path.join(work, width === 1280 ? "limits.png" : "limits-phone.png") });
      await context.close();
      const own = meters.findIndex((line) => line.startsWith(WITH_MODEL));
      check(`${width} px: the model's meter follows its account's`, own >= 0 && (meters[own + 1] || "").startsWith(`Fable · ${WITH_MODEL}`) && /38/.test(meters[own + 1]),
        meters.join(" | "));
      check(`${width} px: one model meter, for the account that has the limit`, meters.filter((line) => line.startsWith("Fable · ")).length === 1, meters.join(" | "));
      check(`${width} px: nothing wider than the window`, overflow <= 0, `${overflow} px over`);
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
