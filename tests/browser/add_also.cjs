// Add's Also pick (SPEC SESSION-31): the same words on other models. Checks, on the main page at 1280 px dark and
// 390 px light, with Claude, Codex and Gemini on:
// - the Add line has an Also button, its icon alone while nothing is picked;
// - its list holds every model of every agent, the main line's model marked "main" and disabled;
// - ticking Gemini and a Codex model keeps the list open and shows "Also Gemini 4 Neon +1" on the button;
// - with three ticked the rest are disabled, and unticking one enables them again;
// - picking Gemini on the main line marks it "main" in Also and leaves it out of the picks;
// - Escape closes the list alone, the dialog still open;
// - the Add sends `also`, the server makes one to-do per model, each titled with its model, and each to-do's session
//   starts with its own summary, which ends with the note;
// - the next Add starts with nothing picked; nothing is wider than the screen; no page errors.
// Saves screenshots of the open list and of Search showing the three to-dos, per width, in the work dir.
// The harness runs no Codex or Gemini program, so those sessions log a failed start after Cockpit accepted it.
//
// Starts serve_preview.py (this checkout's Cockpit on synthetic data, fake sessions; README.md says what it needs)
// and drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/add_also.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const NOTE = "(multiple agents are working on this with the exact same initial prompt";

async function main() {
  const work = workFolder("cockpit-add-also-");
  fs.writeFileSync(path.join(work, "providers.json"), JSON.stringify(["claude", "codex", "gemini"]));
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
      const starts = [];
      page.on("response", async (response) => {
        const request = response.request();
        const where = new URL(request.url()).pathname;
        if (request.method() !== "POST") return;
        if (where === "/api/items/add") adds.push({ sent: JSON.parse(request.postData()), got: await response.json().catch(() => null) });
        if (where === "/api/messages") starts.push({ sent: JSON.parse(request.postData()), status: response.status() });
      });
      await page.goto(`${info.origin}/`, { timeout: 30000 });
      await page.locator(".layout > .primary").waitFor({ timeout: 30000 });
      const area = page.locator('#add-box textarea[data-key="add:new"]');
      const button = page.locator("#add-box .add-also-pick > summary");
      const list = page.locator("#add-box .add-also-pick .menu-list");
      const rows = () => list.locator("button").evaluateAll((buttons) => buttons.map((b) => ({
        value: b.dataset.value, text: b.innerText.replace(/\s+/g, " ").trim(), disabled: b.disabled, pressed: b.getAttribute("aria-pressed") })));
      const wide = () => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);

      await page.locator(width <= 1000 ? ".phone-add" : ".sidebar-add").click();
      await area.waitFor();
      check(`${width} px: the Also button shows its icon alone with nothing picked`,
        (await button.getAttribute("aria-label")) === "Also run on other models, to compare" && (await button.innerText()).trim() === "",
        `${await button.getAttribute("aria-label")} / ${await button.innerText()}`);

      await button.click();
      await list.waitFor();
      let shown = await rows();
      const agents = new Set(shown.map((each) => each.value.split(":")[0]));
      const main = shown.filter((each) => each.text.endsWith("main"));
      check(`${width} px: the list holds every agent's models, the main model marked and disabled`,
        ["claude", "codex", "gemini"].every((each) => agents.has(each)) && main.length === 1 && main[0].disabled
          && main[0].value.startsWith("claude:") && shown.some((each) => each.value === "gemini:gemini-4-neon" && each.text === "Gemini 4 Neon"),
        JSON.stringify(shown));

      const codex = shown.find((each) => each.value.startsWith("codex:")).value;
      await list.locator('button[data-value="gemini:gemini-4-neon"]').click();
      await list.locator(`button[data-value="${codex}"]`).click();
      check(`${width} px: ticking Gemini and a Codex model keeps the list open and names them on the button`,
        (await list.isVisible()) && (await button.innerText()).trim() === "Also Gemini 4 Neon +1",
        await button.innerText());
      const third = (await rows()).find((each) => !each.disabled && each.pressed === "false").value;
      await list.locator(`button[data-value="${third}"]`).click();
      shown = await rows();
      check(`${width} px: with three ticked the rest are disabled`,
        shown.filter((each) => each.pressed === "true").length === 3 && shown.every((each) => each.pressed === "true" || each.disabled),
        JSON.stringify(shown));
      await list.locator(`button[data-value="${third}"]`).click();
      shown = await rows();
      check(`${width} px: unticking one enables the rest again`,
        shown.filter((each) => each.disabled).length === 1, JSON.stringify(shown));
      const box = await list.boundingBox();
      check(`${width} px: the open list fits the screen`, box.x >= 0 && box.x + box.width <= width && (await wide()) <= 0,
        JSON.stringify({ box, wide: await wide() }));
      await sleep(250);
      await page.screenshot({ path: path.join(work, `add-also-open-${width}-${scheme}.png`) });

      await page.keyboard.press("Escape");
      check(`${width} px: Escape closes the list and leaves the dialog open`,
        (await list.isHidden()) && (await area.isVisible()), "");

      // Gemini on the main line: Also marks it main and leaves it out.
      const agent = page.locator('#add-box .start-pick[data-menu="start:add:new:provider"]');
      await agent.locator("summary").click();
      await agent.locator('button[data-value="gemini"]').click();
      check(`${width} px: Gemini on the main line leaves Gemini out of Also`,
        (await button.innerText()).trim() === "Also GPT-6.2 Sol" || /^Also [^+]+$/.test((await button.innerText()).trim()),
        await button.innerText());
      await button.click();
      shown = await rows();
      check(`${width} px: Also marks Gemini as the main model`,
        shown.find((each) => each.value === "gemini:gemini-4-neon").disabled && shown.find((each) => each.value === "gemini:gemini-4-neon").text.endsWith("main"),
        JSON.stringify(shown));
      await page.keyboard.press("Escape");
      // Back to Claude, so Gemini counts again.
      await agent.locator("summary").click();
      await agent.locator('button[data-value="claude"]').click();
      check(`${width} px: back on Claude, Also again holds Gemini`, (await button.innerText()).trim() === "Also Gemini 4 Neon +1", await button.innerText());

      const text = `Speed up the export ${width}\nIt takes a minute on big boards`;
      await area.fill(text);
      await area.press("Enter");
      const end = Date.now() + 20e3;
      while (!adds.some((each) => each.sent.text === text) && Date.now() < end) await sleep(200);
      const sent = adds.find((each) => each.sent.text === text);
      const made = sent && sent.got ? [sent.got, ...(sent.got.also || [])] : [];
      check(`${width} px: the Add sends Also and the server makes one to-do per model, titled with its model`,
        sent && sent.sent.also.length === 2 && made.length === 3
          && made[0].title === `Opus 5.5 · Speed up the export ${width}` && made[1].title === `Gemini 4 Neon · Speed up the export ${width}`
          && made.every((each) => each.summary.includes(NOTE)),
        JSON.stringify(sent && { also: sent.sent.also, titles: made.map((each) => each.title) }));
      const end2 = Date.now() + 20e3;
      const ids = made.map((each) => each.id);
      while (starts.filter((each) => ids.includes(each.sent.target.item_id)).length < 3 && Date.now() < end2) await sleep(200);
      const started = starts.filter((each) => ids.includes(each.sent.target.item_id));
      check(`${width} px: each to-do's session starts with its own summary`,
        started.length === 3 && started.every((each) => each.status === 200 && each.sent.text.includes(NOTE)),
        JSON.stringify(started.map((each) => ({ id: each.sent.target.item_id, status: each.status }))));
      await sleep(800);
      await page.keyboard.press("Control+k");
      await page.locator(".command-dialog input[type=search]").fill(`Speed up the export ${width}`);
      const found = page.locator(".command-dialog li[role=option]").filter({ hasText: `Speed up the export ${width}` });
      await found.nth(2).waitFor().catch(() => {});
      const titles = await found.allInnerTexts();
      await sleep(250);
      await page.screenshot({ path: path.join(work, `add-also-rows-${width}-${scheme}.png`) });
      check(`${width} px: the three to-dos show in Search, each with its model`,
        titles.length === 3 && ["Opus 5.5 · ", "Gemini 4 Neon · ", " · "].every((each) => titles.some((title) => title.includes(each))),
        JSON.stringify(titles));
      await page.keyboard.press("Escape");

      await page.locator(".sidebar-add:visible, .phone-add:visible").first().click();
      await area.waitFor();
      check(`${width} px: the next Add starts with nothing in Also`, (await button.innerText()).trim() === "", await button.innerText());
      await page.keyboard.press("Escape");
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
