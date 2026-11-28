// Gemini's weekly limits in the sidebar foot, while Gemini is enabled: after a hairline, Gemini's mark and a lane with one
// bar per group of models Antigravity runs (gemini_usage.py), and a hover card with each group's week and five hours.
// It shows with and without the CLIProxyAPI hub, and the foot still fits its one line. Before the first reading the
// mark stands alone; a failed reading says so in the card.
//
// Starts serve_preview.py (no hub) and serve_hub.py (with its stand-in hub) from this checkout (README.md says what they
// need), puts sample figures into the page's state, and drives a headless Chromium at 1440 px in both looks and at
// 390 px. Prints one line per check and exits 1 when one failed. The foot and the hover card are saved as
// foot-*.png and card-*.png in each work folder.
//
// Usage, from the checkout: node tests/browser/usage_gemini.cjs

"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { workFolder, startPreview, fullState, screenshotRedrawn } = require("./work.cjs");
const { LOW_MACHINE } = require("./machine_fixture.cjs");

// Browser dependencies can be selected through the environment (README.md).
const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const { chromium } = require(PLAYWRIGHT);

const HOUR = 3600e3;

// A limit as usage.window gives it, `used` percent spent with `elapsed` percent of its span gone.
function limit(key, used, elapsed, resetsIn, pace) {
  const [label, name] = key === "seven_day" ? ["7d", "7-day"] : ["5h", "5-hour"];
  return { key, label, name, used, elapsed, pace, resets_at: new Date(Date.now() + resetsIn).toISOString().replace(/\.\d+Z$/, "Z"), counted_until: null, span: 100 };
}

// Antigravity's two groups: Gemini's own models a third into their week with 45 % spent, and the Claude and GPT models
// it also offers, unused.
function reading() {
  return {
    groups: [
      { name: "Gemini Models", week: limit("seven_day", 45, 30, 118 * HOUR, "over"), five_hour: limit("five_hour", 30, 55, 2.25 * HOUR, "under") },
      { name: "Claude and GPT models", week: limit("seven_day", 0, 0, 167 * HOUR, "under"), five_hour: limit("five_hour", 0, 0, 5 * HOUR, "under") },
    ],
    captured_at: new Date().toISOString().replace(/\.\d+Z$/, "Z"),
    error: "",
  };
}

// A window as usage.window gives it without figures: Antigravity left it out, or it reset since the last reading.
function unknown(key) {
  return { ...limit(key, null, null, 0, null), elapsed: null, resets_at: null };
}

// Gemini's models with no five-hour row, and the Claude and GPT models' week reset since the last reading.
function gaps() {
  const gemini = reading();
  gemini.groups[0].five_hour = unknown("five_hour");
  gemini.groups[1].week = unknown("seven_day");
  return gemini;
}

const NONE_YET = { groups: [], captured_at: null, error: "" };
const FAILED = { groups: [], captured_at: null, error: "agy -p /quota exited 1: not signed in" };

async function routeGemini(page, gemini) {
  await page.route("**/api/state**", async (route) => {
    const full = await fullState(route);
    const state = await full.response.json();
    state.usage.machine = LOW_MACHINE;
    state.usage.gemini = gemini;
    await route.fulfill({ ...full, json: state });
  });
}

// Both groups with use: the Claude and GPT models a fifth spent too.
function bothUsed() {
  const gemini = reading();
  gemini.groups[1].week = limit("seven_day", 20, 30, 118 * HOUR, "under");
  return gemini;
}

// The foot as drawn: whether it fits its line, Gemini's lane's bars, whether a hairline stands right before Gemini, the
// length per percent of Gemini's lane and of the first other lane, and the spoken words of the group that holds it.
function footNow() {
  const foot = document.querySelector(".rail-foot");
  const fam = document.querySelector("#usage .fam.gemini");
  const caps = fam && fam.closest(".usage.caps");
  const before = fam && (fam.previousElementSibling || (caps.querySelector(".fam") === fam ? caps.previousElementSibling : null));
  const perUnit = (lane) => (lane ? lane.getBoundingClientRect().width / Number(lane.dataset.units) : 0);
  return {
    fits: foot.clientWidth > 0 && foot.scrollWidth <= foot.clientWidth,
    gemini: !!fam,
    bare: !!fam && fam.classList.contains("bare"),
    bars: fam ? fam.querySelectorAll(".wk").length : 0,
    hairline: !!before && before.matches(".fam-sep"),
    geminiScale: perUnit(fam && fam.querySelector(".lane")),
    otherScale: perUnit(document.querySelector("#usage .fam:not(.gemini) .lane")),
    said: caps ? caps.getAttribute("aria-label") : "",
  };
}

async function cardNow(page) {
  const middle = await page.evaluate(() => {
    const box = document.querySelector("#usage .fam.gemini").getBoundingClientRect();
    return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  });
  await page.mouse.move(middle.x, middle.y);
  await page.waitForSelector(".tip.tip-card .usage-card", { timeout: 15e3 });
  return page.evaluate(() => {
    const tip = document.querySelector(".tip.tip-card");
    const account = [...tip.querySelectorAll(".uc-account")].find((el) => el.querySelector(".uc-email")?.textContent === "Gemini");
    return {
      fits: tip.scrollWidth <= tip.clientWidth,
      groups: account ? [...account.querySelectorAll(".uc-group-name")].map((el) => el.textContent) : [],
      text: account ? account.innerText.replace(/\s+/g, " ").trim() : "",
    };
  });
}

// serve_hub.py with a stand-in hub that holds one Codex account, its week 40 % used (hub.json, as usage_hub_limits.cjs
// writes it). Started as startServer starts a server, with hub.json in the work folder first.
async function startHub() {
  const work = workFolder("cockpit-gemini-hub-");
  const week = String(Math.round(Date.now() / 1000 + 3.5 * 86400));
  fs.writeFileSync(path.join(work, "hub.json"), JSON.stringify({ credentials: [
    { id: "codex-three@example.test-plus.json", auth_index: 3, provider: "codex", email: "three@example.test", id_token: { chatgpt_account_id: "acct-1", plan_type: "plus" }, quota: { observed_at: new Date().toISOString(), signals: {
      "X-Codex-Plan-Type": "plus", "X-Codex-Secondary-Used-Percent": "40", "X-Codex-Secondary-Window-Minutes": "10080", "X-Codex-Secondary-Reset-At": week,
    } } },
  ] }));
  const server = spawn(path.join(__dirname, "..", "..", ".venv", "bin", "python"), [path.join(__dirname, "serve_hub.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  server.unref();
  const file = path.join(work, "cookie.json");
  for (const end = Date.now() + 180e3; Date.now() < end; await new Promise((done) => setTimeout(done, 200))) {
    if (server.exitCode !== null) throw new Error(`serve_hub.py exited with ${server.exitCode}`);
    if (!fs.existsSync(file)) continue;
    const info = JSON.parse(fs.readFileSync(file, "utf8"));
    if (await fetch(`${info.origin}/healthz`).then((response) => response.ok, () => false)) return { work, info };
  }
  throw new Error("serve_hub.py did not come up within 180 s");
}

async function main() {
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  const pageErrors = [];
  const browser = await chromium.launch({ executablePath: CHROMIUM });
  try {
    const servers = {
      plain: await startPreview("cockpit-gemini-plain-"),
      hub: await startHub(),
    };
    for (const [variant, { work, info }] of Object.entries(servers)) {
      for (const [width, scheme] of [[1440, "light"], [1440, "dark"], [390, "light"]]) {
        const context = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: scheme, deviceScaleFactor: 2 });
        await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
        const page = await context.newPage();
        page.on("pageerror", (error) => pageErrors.push(error.message));
        await routeGemini(page, reading());
        await page.goto(`${info.origin}/`);
        // A phone shows the rail as its list screen, which the bar's Your items opens.
        if (width < 640) await page.locator(".phone-bar .phone-back").click({ timeout: 20e3 });
        await page.waitForSelector("#usage .fam.gemini .wk", { state: "attached", timeout: 60e3 });
        await page.waitForSelector("#usage .fam:not(.gemini) .lane", { state: "attached", timeout: 60e3 });
        await page.evaluate(() => document.fonts.ready);
        const tag = `${variant}-${width}${scheme === "dark" ? "-dark" : ""}`;
        const foot = await page.evaluate(footNow);
        await screenshotRedrawn(page.locator(".rail-foot"), { path: path.join(work, `foot-${tag}.png`) });
        await page.screenshot({ path: path.join(work, `page-${tag}.png`) });
        check(`${tag}: the foot has Gemini's lane with one bar, for the one group used`, foot.gemini && !foot.bare && foot.bars === 1, JSON.stringify(foot));
        check(`${tag}: no hairline before Gemini`, !foot.hairline, JSON.stringify(foot));
        // Within a third: the marks beside the lanes take room the lanes' spans do not count.
        check(`${tag}: Gemini's lane keeps about the other lanes' length per percent`,
          foot.otherScale > 0 && Math.abs(foot.geminiScale / foot.otherScale - 1) < 0.35, JSON.stringify(foot));
        check(`${tag}: its spoken words give each group's week`, foot.said.includes("Gemini: Gemini models 45%, Claude and GPT models 0%"), foot.said);
        check(`${tag}: the foot fits its line`, foot.fits, JSON.stringify(foot));
        if (width >= 640) {
          const card = await cardNow(page);
          await screenshotRedrawn(page.locator(".tip.tip-card"), { path: path.join(work, `card-${tag}.png`) });
          check(`${tag}: the hover card has Gemini's groups, each with its week and five hours`,
            String(card.groups) === "Gemini models,Claude and GPT models" && /7-day|7d/.test(card.text) && /5-hour|5h/.test(card.text), JSON.stringify(card));
          check(`${tag}: the hover card fits its box`, card.fits, JSON.stringify(card));
          await page.mouse.move(700, 450);
        }
        await page.unrouteAll({ behavior: "wait" });
        await context.close();
      }
      // Before the first reading the mark stands alone; a failed reading says why in the card.
      for (const [name, gemini] of [["none yet", NONE_YET], ["failed", FAILED]]) {
        const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 });
        await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
        const page = await context.newPage();
        page.on("pageerror", (error) => pageErrors.push(error.message));
        await routeGemini(page, gemini);
        await page.goto(`${info.origin}/`);
        await page.waitForSelector("#usage .fam.gemini", { state: "attached", timeout: 60e3 });
        const foot = await page.evaluate(footNow);
        check(`${variant}, ${name}: the foot shows Gemini's mark alone and fits`, foot.bare && foot.bars === 0 && foot.fits, JSON.stringify(foot));
        const card = await cardNow(page);
        const words = name === "failed" ? "Antigravity's limits could not be read: agy -p /quota exited 1: not signed in" : "Reading Antigravity's limits…";
        check(`${variant}, ${name}: the hover card says "${words}"`, card.text.includes(words), card.text);
        if (name === "failed") await screenshotRedrawn(page.locator(".tip.tip-card"), { path: path.join(work, "card-failed.png") });
        await page.unrouteAll({ behavior: "wait" });
        await context.close();
      }
      // Once both groups are used, the lane has a bar for each.
      {
        const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 });
        await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
        const page = await context.newPage();
        page.on("pageerror", (error) => pageErrors.push(error.message));
        await routeGemini(page, bothUsed());
        await page.goto(`${info.origin}/`);
        await page.waitForSelector("#usage .fam.gemini .wk", { state: "attached", timeout: 60e3 });
        const foot = await page.evaluate(footNow);
        check(`${variant}, both used: Gemini's lane has a bar per group and the foot fits`, foot.bars === 2 && foot.fits, JSON.stringify(foot));
        await screenshotRedrawn(page.locator(".rail-foot"), { path: path.join(work, "foot-both-used.png") });
        await page.unrouteAll({ behavior: "wait" });
        await context.close();
      }
      // Windows without figures read as not reported, never as unused.
      {
        const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 });
        await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
        const page = await context.newPage();
        page.on("pageerror", (error) => pageErrors.push(error.message));
        await routeGemini(page, gaps());
        await page.goto(`${info.origin}/`);
        await page.waitForSelector("#usage .fam.gemini .wk", { state: "attached", timeout: 60e3 });
        const foot = await page.evaluate(footNow);
        check(`${variant}, gaps: the spoken words call the reset week not reported`,
          foot.said.includes("Gemini: Gemini models 45%, Claude and GPT models not reported yet"), foot.said);
        const card = await cardNow(page);
        check(`${variant}, gaps: the hover card says "Not reported yet" for both missing windows`,
          (card.text.match(/Not reported yet/g) || []).length === 2 && !card.text.includes("No use since it reset"), card.text);
        await screenshotRedrawn(page.locator(".tip.tip-card"), { path: path.join(work, "card-gaps.png") });
        await page.unrouteAll({ behavior: "wait" });
        await context.close();
      }
      console.error(`${variant}: screenshots in ${work}`);
    }
    check("no page errors", pageErrors.length === 0, pageErrors.join(" | "));
  } finally {
    await browser.close();
  }
  const failed = results.filter((result) => !result.ok);
  console.error(`${results.length - failed.length}/${results.length} checks passed`);
  process.exitCode = failed.length ? 1 : 0;
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
