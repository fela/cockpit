// The Usage page's choice of account for new sessions (SPEC SESSION-17), at 1280 and 390 px: the row under the Claude
// meters says where new sessions start; pressing "2 h" for work makes it say work is the user's choice until a time,
// and the hover card in the header says the same; "Back to the usual order" clears it. Nothing is wider than the
// window, and the header's usage card has no line explaining the bars.
// The header's hover card makes the same choice: the card stays open while the pointer moves from the
// usage bars onto it, "30 min" for personal there makes personal the choice and shows pressed in another colour,
// "2 h" changes it, pressing the chosen "2 h" again undoes it, the chosen length carries a bar for how
// much of it has passed, and the card closes once the pointer leaves it.
// In a 390 × 400 window the card fits in the window and scrolls inside itself, staying open, so its last button, "1 day"
// for work, can be pressed (regression check). Of two choices made in a row on the Usage page, the first slow to reach Cockpit,
// the later one stays (regression check).
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs)
// and drives a headless Chromium. Prints one line per check, saves the row at each width as prefer-<width>.png in
// the work folder, and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/prefer_account.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

// Browser dependencies can be selected through the environment (README.md).
const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;
const PYTHON = path.join(path.resolve(__dirname, "..", ".."), ".venv", "bin", "python");
// Cockpit's first count of the fixture transcripts, before the page shows the limits.
const COUNTED_MS = 30 * 60e3;

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-prefer-");
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
      const context = await browser.newContext({ viewport: { width, height: 900 }, isMobile: width < 600 });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      page.on("pageerror", (error) => pageErrors.push(error.message));
      await page.goto(`${info.origin}/usage`);
      await page.waitForSelector(".prefer", { timeout: COUNTED_MS });
      // The fixture may hold a choice already.
      if (await page.getByRole("button", { name: "Back to the usual order" }).count()) {
        await page.getByRole("button", { name: "Back to the usual order" }).click();
        await page.waitForFunction(() => /usual order/.test(document.querySelector(".prefer-status").textContent), null, { timeout: 30e3 }).catch(() => {});
      }
      const status = () => page.locator(".prefer-status").textContent();
      check(`${width} px: the row says where new sessions start`, /^New sessions start on \w+, by the usual order\.$/.test(await status()), await status());
      await page.getByRole("group", { name: "Prefer work for" }).getByRole("button", { name: "2 h" }).click();
      await page.waitForFunction(() => /your choice/.test(document.querySelector(".prefer-status").textContent), null, { timeout: 30e3 }).catch(() => {});
      check(`${width} px: after 2 h for work, work is the choice until a time`,
        /^New sessions start on work, your choice until \d\d:\d\d \(in (1 h 59 min|2 h)\)\.$/.test(await status()), await status());
      const pressed = await page.locator(".prefer .prefer-chosen[aria-pressed=true]").evaluateAll((buttons) => buttons.map((button) => button.closest(".prefer-account").textContent));
      check(`${width} px: work's 2 h shows pressed`, pressed.length === 1 && /^Prefer work for.*2 h/.test(pressed[0]), JSON.stringify(pressed));
      const bar = await page.evaluate(() => {
        const strip = document.querySelector(".prefer .prefer-chosen .prefer-passed");
        return strip && { width: strip.offsetWidth, filled: parseFloat(getComputedStyle(strip, "::before").width) };
      });
      check(`${width} px: work's 2 h has an empty bar for how much of it has passed`, bar && bar.width > 20 && bar.filled < 2, JSON.stringify(bar));
      await page.locator(".prefer").screenshot({ path: path.join(work, `prefer-${width}.png`) });
      check(`${width} px: nothing wider than the window`,
        await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth) <= 0, "overflow");
      if (width === 1280) {
        await page.goto(`${info.origin}/classic`);
        await closeAway(page);
        await page.locator(".usage.caps").hover();
        const card = await page.locator(".tip.tip-card").textContent({ timeout: 30e3 }).catch(() => "");
        check("the header's card names the choice", /New sessions start here, your choice until \d\d:\d\d/.test(card), card);
        check("the header's card has no line explaining the bars", !/Bars fill/.test(card), card);
        await chooseInCard(page, check, work);
        await chooseByKeyboard(page, check);
        await page.goto(`${info.origin}/usage`);
        await page.waitForSelector(".prefer", { timeout: COUNTED_MS });
      }
      // The header's card already went back to the usual order at 1280 px.
      if (await page.getByRole("button", { name: "Back to the usual order" }).count()) {
        await page.getByRole("button", { name: "Back to the usual order" }).click();
      }
      await page.waitForFunction(() => /usual order/.test(document.querySelector(".prefer-status").textContent), null, { timeout: 30e3 }).catch(() => {});
      check(`${width} px: back to the usual order`, /by the usual order/.test(await status()), await status());
      if (width === 1280) {
        // Two choices in a row, the first slow to reach Cockpit: the later one is what stays (regression check).
        let slowed = false;
        await page.route("**/api/accounts/prefer", async (route) => {
          if (slowed) return route.fallback();
          slowed = true;
          await sleep(1500);
          await route.fallback();
        });
        await page.getByRole("group", { name: "Prefer work for" }).getByRole("button", { name: "2 h" }).click();
        await page.getByRole("group", { name: "Prefer personal for" }).getByRole("button", { name: "30 min" }).click();
        // The later choice waits behind the first, in no outbox yet, so leaving asks first (CODE-3).
        const asks = await page.evaluate(() => {
          const leaving = new Event("beforeunload", { cancelable: true });
          dispatchEvent(leaving);
          return leaving.defaultPrevented;
        });
        check(`${width} px: leaving while the later choice waits asks first`, asks, "leaving did not ask");
        await sleep(5000);
        check(`${width} px: of two choices made in a row, the later one stays`, /^New sessions start on personal, your choice/.test(await status()), await status());
        await page.unroute("**/api/accounts/prefer");
        await page.getByRole("button", { name: "Back to the usual order" }).click();
        await page.waitForFunction(() => /usual order/.test(document.querySelector(".prefer-status").textContent), null, { timeout: 30e3 }).catch(() => {});
      }
      await context.close();
    }
    await shortWindow(browser, info, check, work, pageErrors);
    check("no page errors", pageErrors.length === 0, pageErrors.join(" | "));
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failed = results.filter((result) => !result.ok);
  console.error(`${results.length - failed.length}/${results.length} checks passed; work dir ${work}`);
  process.exitCode = failed.length ? 1 : 0;
}

// The choice made on the header's hover card: the pointer moves from the bars onto the card in small steps, as a hand
// does, crossing the gap between them; the card is filled again as the choice lands.
async function chooseInCard(page, check, work) {
  const card = page.locator(".tip.tip-card");
  const cardText = () => card.textContent().catch(() => "");
  const personal = card.getByRole("button", { name: "Start new sessions on personal for 30 min" });
  const box = await personal.boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, { steps: 12 });
  await page.waitForTimeout(600);
  check("the card stays open as the pointer moves onto it", await card.isVisible(), "the card closed");
  await page.mouse.down();
  await page.mouse.up();
  await page.waitForFunction(() => /You chose personal for new sessions until \d\d:\d\d/.test(document.querySelector(".tip.tip-card")?.textContent || ""), null, { timeout: 30e3 }).catch(() => {});
  check("after 30 min for personal on the card, the card says personal is the choice",
    /You chose personal for new sessions until \d\d:\d\d/.test(await cardText()), await cardText());
  const starts = await page.locator(".tip.tip-card .uc-account.starts .uc-folder").textContent().catch(() => "");
  check("the card marks personal as where new sessions start", starts === "Claude, personal", starts);
  // The length chosen shows pressed in another colour; another length changes it, and pressing the
  // chosen one again undoes the choice.
  const chosen = () => page.evaluate(() => [...document.querySelectorAll(".tip.tip-card .uc-chosen")].map((button) => ({
    label: button.getAttribute("aria-label"), pressed: button.getAttribute("aria-pressed"),
    fill: getComputedStyle(button).backgroundColor,
    other: getComputedStyle(button.parentElement.querySelector("button:not(.uc-chosen)")).backgroundColor,
  })));
  let marked = await chosen();
  check("the card shows personal's 30 min pressed, in another colour than the other lengths",
    marked.length === 1 && /personal for 30 min/.test(marked[0].label) && marked[0].pressed === "true" && marked[0].fill !== marked[0].other,
    JSON.stringify(marked));
  // A bar along the chosen length's bottom shows how much of it has passed: none yet, half 15 minutes in.
  const bar = await page.evaluate(() => {
    const strip = document.querySelector(".tip.tip-card .uc-chosen .uc-passed");
    return strip && { width: strip.offsetWidth, filled: parseFloat(getComputedStyle(strip, "::before").width) };
  });
  check("the chosen length has a bar for how much of it has passed, empty just after the choice",
    bar && bar.width > 20 && bar.filled < 2, JSON.stringify(bar));
  const half = await page.evaluate(() => preferPassed({ preferred_until: new Date(Date.now() + 15 * 60e3).toISOString(), preferred_minutes: 30 }, "uc-passed").style.getPropertyValue("--passed"));
  check("the bar is half full halfway through", half === "50.0%", half);
  await card.screenshot({ path: path.join(work, "prefer-card.png") });
  await card.getByRole("button", { name: "Start new sessions on personal for 2 h" }).click();
  await page.waitForFunction(() => /personal for 2 h/.test(document.querySelector(".tip.tip-card .uc-chosen")?.getAttribute("aria-label") || ""), null, { timeout: 30e3 }).catch(() => {});
  marked = await chosen();
  check("2 h for personal on the card changes the choice to 2 h", marked.length === 1 && /personal for 2 h/.test(marked[0].label), JSON.stringify(marked));
  await card.locator(".uc-chosen").click();
  await page.waitForFunction(() => /Choose where new sessions start for a while/.test(document.querySelector(".tip.tip-card")?.textContent || ""), null, { timeout: 30e3 }).catch(() => {});
  check("pressing the chosen length again undoes the choice",
    /Choose where new sessions start for a while/.test(await cardText()) && (await chosen()).length === 0, await cardText());
  await page.mouse.move(4, 880, { steps: 4 });
  await page.waitForTimeout(800);
  check("the card closes once the pointer leaves it", !(await card.isVisible()), "still open");
}

// The same choice by keyboard: focus and the card survive a redraw of the bars, Tab on the focused bars steps into
// the card, Enter chooses and focus stays in the card
// through its refill, Escape goes back to the bars and a second Escape closes the card.
async function chooseByKeyboard(page, check) {
  const card = page.locator(".tip.tip-card");
  const focused = () => page.evaluate(() => document.activeElement?.getAttribute("aria-label") || "");
  // The mouse's Back to the usual order left focus on the bars when the card dropped that button.
  await page.evaluate(() => document.activeElement?.blur());
  await page.keyboard.press("Shift");
  await page.locator(".usage.caps").focus();
  await card.waitFor({ state: "visible", timeout: 5e3 }).catch(() => {});
  check("focus on the bars opens the card", await card.isVisible(), "no card");
  // A poll that redraws only the usage puts new bars in the old ones' place: focus and the card move to them.
  await page.evaluate(() => renderUsage(ui.state));
  await page.waitForTimeout(100);
  const kept = await page.evaluate(() => Boolean(document.activeElement?.matches(".usage.caps")));
  check("a redraw of the bars keeps focus on them and the card open", kept && await card.isVisible(), `on bars: ${kept}`);
  await page.keyboard.press("Tab");
  check("Tab on the bars steps into the card", /^Start new sessions on \w+ for 30 min$/.test(await focused()), await focused());
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => /You chose \w+ for new sessions/.test(document.querySelector(".tip.tip-card")?.textContent || ""), null, { timeout: 30e3 }).catch(() => {});
  const inCard = await page.evaluate(() => Boolean(document.activeElement?.closest(".tip")));
  check("after Enter the card shows the choice and focus stays in it",
    inCard && /You chose/.test(await card.textContent().catch(() => "")), `focus in card: ${inCard}, on ${await focused()}`);
  await page.keyboard.press("Escape");
  const onBars = await page.evaluate(() => Boolean(document.activeElement?.matches(".usage.caps")));
  check("Escape on a card control goes back to the bars", onBars && await card.isVisible(), `on bars: ${onBars}`);
  await page.keyboard.press("Escape");
  check("Escape on the bars closes the card", !(await card.isVisible()), "still open");
}

// The header's card in a short window, in the dark scheme: the card ends inside the window, and scrolling it to the
// work account's "1 day" keeps it open so the button can be pressed.
async function shortWindow(browser, info, check, work, pageErrors) {
  const context = await browser.newContext({ viewport: { width: 390, height: 400 }, colorScheme: "dark" });
  await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
  const page = await context.newPage();
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await page.goto(`${info.origin}/classic`);
  await closeAway(page);
  await page.locator(".usage.caps").hover();
  const card = page.locator(".tip.tip-card");
  await card.waitFor({ state: "visible", timeout: 30e3 });
  const box = await card.boundingBox();
  check("390 × 400: the card ends inside the window", box.y >= 0 && box.y + box.height <= 400, JSON.stringify(box));
  const day = card.getByRole("button", { name: "Start new sessions on work for 1 day" });
  await day.scrollIntoViewIfNeeded();
  await page.waitForTimeout(300);
  check("390 × 400: the card stays open while it scrolls", await card.isVisible(), "the card closed");
  const tall = await card.evaluate((el) => el.scrollHeight > el.clientHeight);
  await card.screenshot({ path: path.join(work, "prefer-card-short.png") });
  await day.click();
  await page.waitForFunction(() => /You chose work for new sessions until/.test(document.querySelector(".tip.tip-card")?.textContent || ""), null, { timeout: 30e3 }).catch(() => {});
  check("390 × 400: the card scrolls inside itself and \"1 day\" for work makes work the choice",
    tall && /You chose work for new sessions until/.test(await card.textContent().catch(() => "")),
    `scrolls: ${tall}; ${await card.textContent().catch(() => "")}`);
  await card.getByRole("button", { name: "Back to the usual order" }).click();
  await context.close();
}

// The fixture may hold a timer running while the user was away: its dialog asks once per stretch
// away and covers the header; closing it leaves it closed for that stretch. A timer that ran on without activity
// brings the "No activity" dialog, which comes back until answered: Return answers it on the copy.
async function closeAway(page) {
  await page.waitForSelector("#usage .usage", { state: "attached", timeout: COUNTED_MS });
  const dialog = page.locator(":is(#time-dialog, #idle)[open]").first();
  if (!(await dialog.waitFor({ timeout: 3e3 }).then(() => true, () => false))) return;
  if (await page.locator("#idle[open]").count()) {
    await page.locator("#idle[open]").getByRole("button", { name: "Return" }).click();
    await page.waitForFunction(() => !document.querySelector("#idle[open]"), null, { timeout: 30e3 });
  }
  await page.evaluate(() => document.getElementById("time-dialog").open && document.getElementById("time-dialog").close());
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
