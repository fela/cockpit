// The usage display's pace colours (SPEC UI-12, UI-37, UI-57, UI-73, UI-76) in both themes: each Claude account's week bar
// is drawn in its pace's colour (green under, orange near, red over), every colour meets 3:1 on the ground behind
// the lane, the orange stands apart from the amber and the red, old numbers keep their colours with the bars
// dimmed, no bar changes width (UI-16), Claude's mark is in Claude's orange, and each family's mark sits right beside its lane
// with a hairline between Claude and Codex and another between Codex and the host.
// Fixed machine readings exercise the host indicator; unavailable readings leave the page usable without it.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what
// it needs) and drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/usage_colours.cjs

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

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// The usage colours, each bar's track and the tokens they should match, read off the page as sRGB.
function usageNow() {
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = 1;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  const rgba = (colour) => {
    ctx.clearRect(0, 0, 1, 1);
    ctx.fillStyle = "#000";
    ctx.fillStyle = colour;
    ctx.fillRect(0, 0, 1, 1);
    const [r, g, b, a] = ctx.getImageData(0, 0, 1, 1).data;
    return [r, g, b, a / 255];
  };
  const onto = (top, under) => [0, 1, 2].map((i) => top[i] * top[3] + under[i] * (1 - top[3])).concat(1);
  // The backgrounds from the element up to the first opaque one, laid over each other.
  const ground = (el) => {
    const layers = [];
    for (let node = el; node; node = node.parentElement) {
      const colour = rgba(getComputedStyle(node).backgroundColor);
      if (colour[3] > 0) layers.push(colour);
      if (colour[3] >= 1) break;
    }
    return layers.reduceRight((under, top) => onto(top, under), [255, 255, 255, 1]);
  };
  const token = (name) => {
    const probe = document.createElement("span");
    probe.style.color = `var(${name})`;
    document.body.append(probe);
    const colour = rgba(getComputedStyle(probe).color);
    probe.remove();
    return colour;
  };
  // A bar's pace colour, read through a probe inside it.
  const paceOf = (bar) => {
    const probe = document.createElement("span");
    probe.style.color = "var(--pace)";
    bar.append(probe);
    const colour = rgba(getComputedStyle(probe).color);
    probe.remove();
    return colour;
  };
  // The gap from each family's mark to the first bar in its lane, and whether a hairline stands between the families.
  const families = [...document.querySelectorAll("#usage .fam")].map((fam) => {
    const mark = fam.querySelector(".fam-mark").getBoundingClientRect();
    const bars = [...fam.querySelectorAll(".wk")].map((bar) => bar.getBoundingClientRect().left);
    return { family: fam.classList.contains("claude") ? "claude" : "codex", gap: Math.min(...bars) - mark.right };
  });
  const codex = document.querySelector("#usage .fam.codex");
  const claude = document.querySelector("#usage .fam.claude");
  const host = document.querySelector("#usage .machine-lamp");
  const separator = (el) => el?.matches(".fam-sep") && el.checkVisibility()
    && el.getBoundingClientRect().width > 0 && el.getBoundingClientRect().height > 0;
  const claudeMark = document.querySelector("#usage .fam-mark.claude");
  return {
    claudeMark: claudeMark && { colour: rgba(getComputedStyle(claudeMark).color), ground: ground(claudeMark) },
    stale: Boolean(document.querySelector("#usage .lane.claude .wk.stale")),
    tokens: { under: token("--lamp-green"), near: token("--lamp-orange-fill"), over: token("--lamp-red"), amber: token("--lamp-amber") },
    families,
    subjects: [claude, codex, host].map((el) => Boolean(el?.checkVisibility()
      && el.getBoundingClientRect().width > 0 && el.getBoundingClientRect().height > 0)),
    divided: Boolean(claude && codex && separator(codex.previousElementSibling)
      && codex.previousElementSibling.previousElementSibling === claude),
    dividedAfter: Boolean(codex && host && separator(codex.nextElementSibling)
      && codex.nextElementSibling.nextElementSibling === host),
    windows: [...document.querySelectorAll("#usage .lane.claude .wk")].map((bar) => ({
      pace: ["under", "near", "over"].find((pace) => bar.classList.contains(pace)) || null,
      lit: paceOf(bar),
      ground: ground(bar),
      barOpacity: Number(getComputedStyle(bar).opacity),
      width: bar.getBoundingClientRect().width,
    })),
  };
}

// WCAG contrast and the OKLab distance of two sRGB colours.
function linear(channel) {
  const v = channel / 255;
  return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
}
function contrast(a, b) {
  const lum = ([r, g, b2]) => 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b2);
  const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}
function oklab([r, g, b]) {
  const [lr, lg, lb] = [r, g, b].map(linear);
  const l = Math.cbrt(0.4122214708 * lr + 0.5363325363 * lg + 0.0514459929 * lb);
  const m = Math.cbrt(0.2119034982 * lr + 0.6806995451 * lg + 0.1073969566 * lb);
  const s = Math.cbrt(0.0883024619 * lr + 0.2817188376 * lg + 0.6299787005 * lb);
  return [0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s, 1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s, 0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s];
}
const distance = (a, b) => Math.hypot(...oklab(a).map((v, i) => v - oklab(b)[i]));
const same = (a, b) => a.slice(0, 3).every((v, i) => Math.abs(v - b[i]) <= 2);

// The same report for each account the harness signed in (serve_slow.preview_accounts), the week half
// gone: 30 % used is "under", 55 % "near" and 70 % "over" (usage.pace). `ago`: how many seconds before now
// it was reported.
function writeUsage(work, fiveHour, sevenDay, ago) {
  const folders = path.join(work, "accounts");
  const addresses = fs.readdirSync(folders).filter((file) => file.endsWith(".json"))
    .map((file) => JSON.parse(fs.readFileSync(path.join(folders, file))).oauthAccount.emailAddress);
  const accounts = {};
  for (const address of addresses) accounts[address] = usageRecord(address, fiveHour, sevenDay, ago);
  fs.writeFileSync(path.join(work, "rate-limits.json"), JSON.stringify({ accounts }));
}

function usageRecord(address, fiveHour, sevenDay, ago) {
  const now = Date.now() / 1000;
  return {
    account: { emailAddress: address, displayName: "Usage test", organizationType: "claude_max" },
    rate_limits: {
      five_hour: { used_percentage: fiveHour, resets_at: Math.round(now + 2.5 * 3600) },
      seven_day: { used_percentage: sevenDay, resets_at: Math.round(now + 3.5 * 86400) },
    },
    captured_at: Math.round(now - ago),
    session_id: "00000000-0000-4000-8000-000000000000",
    config_dir: "/nonexistent",
  };
}

async function main() {
  const work = workFolder("cockpit-usage-");
  const now = Date.now();
  const counts = { input_tokens: 1000, cached_input_tokens: 100, output_tokens: 100, total_tokens: 1100 };
  fs.writeFileSync(path.join(work, "codex-usage.json"), JSON.stringify([[
    { type: "session_meta", timestamp: new Date(now - 60000).toISOString(),
      payload: { id: "aaaaaaaa-aaaa-4aaa-8aaa-000000000000", cwd: "/tmp/tasks", source: "exec" } },
    { type: "event_msg", timestamp: new Date(now - 50000).toISOString(), payload: {
      type: "token_count", info: { total_token_usage: counts, last_token_usage: counts },
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

    // Fresh reports with the week under and near, then a report 40 minutes old with the week over, in
    // each theme. The 5-hour figure is in the report but not on the display.
    const schemes = ["light", "dark"];
    const rounds = [
      { name: "fresh, under", usage: [90, 30, 0], pace: "under" },
      { name: "fresh, near", usage: [10, 55, 0], pace: "near" },
      { name: "40 min old", usage: [10, 70, 40 * 60], pace: "over" },
    ];
    const widths = {};
    for (const round of rounds) {
      writeUsage(work, ...round.usage);
      for (const scheme of schemes) {
        const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: scheme });
        await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
        const page = await context.newPage();
        await routeMachine(page, LOW_MACHINE);
        page.on("pageerror", (error) => pageErrors.push(error.message));
        await page.goto(`${info.origin}/classic`);
        await page.waitForSelector(`#usage .lane.claude .wk.${round.pace}`, { state: "attached", timeout: 15e3 });
        await page.evaluate(() => document.fonts.ready);
        const usage = await page.evaluate(usageNow);
        await page.unrouteAll({ behavior: "wait" });
        await context.close();
        const where = `${scheme}, ${round.name}`;
        check(`usage ${where}: Claude, Codex and the host are visible`, usage.subjects.every(Boolean), JSON.stringify(usage.subjects));
        const paces = usage.windows.map((win) => win.pace);
        // Each signed-in account has its bar, all in the round's pace.
        check(`usage ${where}: every Claude bar ${round.pace}`, paces.length >= 1 && paces.every((pace) => pace === round.pace), JSON.stringify(paces));
        const figures = [];
        for (const win of usage.windows) {
          const want = usage.tokens[win.pace];
          const fillRatio = contrast(win.lit, win.ground);
          figures.push(`${win.pace} bar ${fillRatio.toFixed(2)}:1 on rgb(${win.ground.slice(0, 3).map(Math.round).join(",")})`);
          check(`usage ${where}: ${win.pace} bar in its colour`, same(win.lit, want), `fill ${win.lit} token ${want}`);
          check(`usage ${where}: ${win.pace} bar meets 3:1 (${fillRatio.toFixed(2)}:1)`, fillRatio >= 3, `${fillRatio.toFixed(2)}:1`);
        }
        console.error(`    ${where}: ${figures.join("; ")}`);
        const { near, over, amber } = usage.tokens;
        check(`usage ${where}: each mark beside its lane (${usage.families.map((fam) => `${fam.family} ${fam.gap.toFixed(1)} px`).join(", ")})`,
          usage.families.length >= 1 && usage.families.every((fam) => fam.gap >= 0 && fam.gap <= 6), JSON.stringify(usage.families));
        // Claude's mark is in Claude's orange (UI-73): an OKLab hue between 30 and 60 degrees, strong enough to read as orange,
        // and at 2.5:1 against the ground behind it: Claude's own #d97757 stands at 2.97:1 on the light top bar.
        const [, ma, mb] = oklab(usage.claudeMark.colour);
        const markHue = (Math.atan2(mb, ma) * 180) / Math.PI;
        const markRatio = contrast(usage.claudeMark.colour, usage.claudeMark.ground);
        check(`usage ${where}: Claude's mark orange (hue ${markHue.toFixed(0)}, ${markRatio.toFixed(2)}:1)`,
          markHue >= 30 && markHue <= 60 && Math.hypot(ma, mb) >= 0.1 && markRatio >= 2.5, JSON.stringify(usage.claudeMark));
        check(`usage ${where}: a hairline between Claude and Codex`, usage.divided, "no .fam-sep before Codex");
        check(`usage ${where}: a hairline between Codex and the host`, usage.dividedAfter, "no .fam-sep after Codex");
        check(`usage ${where}: orange bar apart from amber (${distance(near, amber).toFixed(3)}) and red (${distance(near, over).toFixed(3)})`,
          distance(near, amber) >= 0.085 && distance(near, over) >= 0.085, `OKLab distance to amber ${distance(near, amber)}, to red ${distance(near, over)}`);
        const stale = !round.name.startsWith("fresh");
        check(`usage ${where}: ${stale ? "old numbers keep their colours, bars dimmed" : "fresh numbers, bars at full strength"}`,
          usage.stale === stale && usage.windows.every((win) => win.barOpacity === (stale ? 0.4 : 1)), JSON.stringify(usage.windows.map((win) => win.barOpacity)));
        const key = scheme;
        const now = usage.windows.map((win) => win.width);
        if (widths[key]) check(`usage ${key}: no bar changes width`, String(widths[key]) === String(now), `${widths[key]} then ${now}`);
        widths[key] = now;
      }
    }

    // A banked reset adds no room (UI-76): 70 % of a half-gone week is over its pace though the account holds a reset
    // that clears the week. The hover card lists the reset, and its bar fills to the reported 70 %.
    writeUsage(work, 10, 70, 0);
    const signedIn = fs.readdirSync(path.join(work, "accounts")).filter((file) => file.endsWith(".json"))
      .map((file) => JSON.parse(fs.readFileSync(path.join(work, "accounts", file))).oauthAccount.emailAddress);
    const grant = { label: "Free limit reset", left: 1, total: 1, ends_at: null, usable_now: true, needs_limit: false, paused: false, clears: ["seven_day"] };
    fs.writeFileSync(path.join(work, "banked-resets.json"), JSON.stringify(Object.fromEntries(
      signedIn.map((address) => [address, { read_at: new Date().toISOString(), grants: [grant], cooldown_until: null }]))));
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
    const page = await context.newPage();
    await routeMachine(page, LOW_MACHINE);
    page.on("pageerror", (error) => pageErrors.push(error.message));
    await page.goto(`${info.origin}/classic`);
    await page.waitForSelector("#usage .lane.claude .wk.over", { state: "attached", timeout: 15e3 });
    await page.hover("#usage .lane.claude");
    await page.waitForSelector(".tip.tip-card .uc-window", { timeout: 15e3 });
    const card = await page.locator(".tip.tip-card").innerText();
    check("usage card: the reported figure", /70% used/.test(card), card.slice(0, 400));
    check("usage card: the banked reset listed", /banked/i.test(card) && !/No banked usage-limit resets/.test(card), card.slice(0, 400));
    const barUsed = await page.evaluate(() => getComputedStyle(document.querySelector(".tip.tip-card .uc-graphic > *")).getPropertyValue("--used").trim());
    check("usage card: the bar fills to the reported share", barUsed === "70%", barUsed);
    await page.unrouteAll({ behavior: "wait" });
    await context.close();

    // An unavailable load reader leaves the usage lanes and the rest of the page usable.
    const unavailableContext = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    await unavailableContext.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
    const unavailablePage = await unavailableContext.newPage();
    unavailablePage.on("pageerror", (error) => pageErrors.push(error.message));
    await routeMachine(unavailablePage, { unavailable: "Load readings are not available on this system" });
    await unavailablePage.goto(`${info.origin}/classic`);
    await unavailablePage.waitForSelector("#usage .lane.claude .wk", { timeout: 15e3 });
    check("unavailable machine: the indicator is absent", await unavailablePage.locator("#usage .machine-lamp").count() === 0, "indicator present");
    check("unavailable machine: usage and work still render",
      await unavailablePage.locator("#usage .lane.claude").isVisible()
        && await unavailablePage.locator("#usage .lane.codex").isVisible()
        && await unavailablePage.locator(".card").count() > 0,
      "missing usage lanes or work cards");
    await unavailablePage.evaluate(() => { ui.pendingRender = true; return refresh(); });
    check("unavailable machine: a later state request keeps the indicator absent",
      await unavailablePage.locator("#usage .machine-lamp").count() === 0, "indicator present after refresh");
    await unavailablePage.unrouteAll({ behavior: "wait" });
    await unavailableContext.close();

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
