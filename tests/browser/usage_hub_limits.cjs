// The weekly limits of the CLIProxyAPI hub's accounts (SPEC UI-84). While a hub is configured the sidebar foot shows
// them alone: Fable's own weekly limit, the week left for Opus once Fable has used all its allowance, and Codex's
// week, inside a link to the Usage page. The machine's own signed-in accounts are left out. The hover card names each
// limit once, with one short line per account that has a figure for it, under that account's short name. A press opens
// the Usage page, whose Accounts section has one aligned row per hub account, with its controls (SPEC UI-85). When the
// hub stops answering, the last figures stay and a red sign joins them.
//
// Starts serve_hub.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) with its
// stand-in hub, and drives a headless Chromium: the Usage page alone at 1280 and 390 px, then Cockpit at 1440 px in
// both looks.
// Prints one line per check and exits 1 when one failed. The foot, the hover card, the foot with the hub down and the
// Usage page are saved as foot-*.png, card-*.png, down.png and usage-*.png in the work folder.
//
// Usage, from the checkout: node tests/browser/usage_hub_limits.cjs

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

// What the stand-in hub holds (serve_slow.py, hub.json): its accounts, each with the limit headers its provider sent
// on its last request. One Claude account has its week 40 % used and Fable's own limit 30 % used, which leaves the
// week after Fable's allowance 50 % used. A second has its week 80 % used and Fable's limit 60 % used, which leaves
// nothing after Fable's allowance, and resets a day sooner. A third Claude account has had no request. One Codex
// account has its week 40 % used.
function hubAccounts(now) {
  const seen = new Date(now).toISOString();
  const week = String(Math.round(now / 1000 + 3.5 * 86400));
  const sooner = String(Math.round(now / 1000 + 2.5 * 86400));
  return [
    { id: "claude-one@example.test.json", auth_index: 1, provider: "claude", email: "one@example.test", quota: { observed_at: seen, signals: {
      "Anthropic-Ratelimit-Unified-7d-Utilization": "0.4", "Anthropic-Ratelimit-Unified-7d-Reset": week,
      "Anthropic-Ratelimit-Unified-7d_oi-Utilization": "0.3", "Anthropic-Ratelimit-Unified-7d_oi-Reset": week,
    } } },
    { id: "claude-two@example.test.json", auth_index: 2, provider: "claude", email: "two@example.test", quota: { observed_at: seen, signals: {
      "Anthropic-Ratelimit-Unified-7d-Utilization": "0.8", "Anthropic-Ratelimit-Unified-7d-Reset": sooner,
      "Anthropic-Ratelimit-Unified-7d_oi-Utilization": "0.6", "Anthropic-Ratelimit-Unified-7d_oi-Reset": sooner,
    } } },
    { id: "claude-four@example.test.json", auth_index: 4, provider: "claude", email: "four@example.test" },
    { id: "codex-three@example.test-plus.json", auth_index: 3, provider: "codex", email: "three@example.test", id_token: { chatgpt_account_id: "acct-1", plan_type: "plus" }, quota: { observed_at: seen, signals: {
      "X-Codex-Plan-Type": "plus", "X-Codex-Secondary-Used-Percent": "40", "X-Codex-Secondary-Window-Minutes": "10080", "X-Codex-Secondary-Reset-At": week,
    } } },
  ];
}

// What the foot and the hover card show for hubAccounts: each family's bars, one per account, the third Claude
// account's an empty track, and the card's shares used in order.
const FOOT = [{ family: "claude", bars: 3 }, { family: "codex", bars: 1 }];
const USED = "40%,80%,30%,60%,50%,100%,40%";

// What the Usage page's row for each of hubAccounts says: its limits with their shares, or the line in their place.
// Cockpit asks the provider for the limits of an account that has none (hub.py _check), which the stand-in hub answers
// with a 404, so the third Claude account's line says either.
const CARDS = {
  "one@example.test": /^Week 40 %, Fable 30 %, Opus after Fable 50 %$/,
  "two@example.test": /^Week 80 %, Fable 60 %, Opus after Fable 100 %$/,
  "four@example.test": /^Its limits (show after its next request through the hub|are unknown: Claude answered 404 when asked for its limits)$/,
  "three@example.test": /^Week 40 %$/,
};

// The foot's families in order, each with how many bars its lane has.
function footNow() {
  return [...document.querySelectorAll("#usage .fam")].map((el) => ({
    family: [...el.classList].find((name) => name !== "fam" && name !== "bare"),
    bars: el.querySelectorAll(".wk").length,
  }));
}

// The hover card under the pointer, which goes to the middle of the foot's limits. The place is read in one go,
// since the foot is drawn again with every poll.
async function cardNow(page) {
  const middle = await page.evaluate(() => {
    const box = document.querySelector("#usage .usage.caps").getBoundingClientRect();
    return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  });
  await page.mouse.move(middle.x, middle.y);
  await page.waitForSelector(".tip.tip-card .hub-card", { timeout: 15e3 });
  return page.evaluate(() => {
    const tip = document.querySelector(".tip.tip-card");
    const card = tip.querySelector(".hub-card");
    return {
      fits: tip.scrollWidth <= tip.clientWidth && card.getBoundingClientRect().right <= tip.getBoundingClientRect().right,
      names: [...card.querySelectorAll(".hc-name")].map((el) => el.textContent.trim()),
      used: [...card.querySelectorAll(".hc-used")].map((el) => el.textContent),
      resets: card.querySelectorAll(".hc-resets").length,
      // Each limit's name sits on a line of its own, above its accounts, which all start at one left edge.
      headed: (() => {
        const names = [...card.querySelectorAll(".hc-name")].map((el) => el.getBoundingClientRect());
        const accounts = [...card.querySelectorAll(".hc-account")].map((el) => el.getBoundingClientRect());
        return accounts.length > 0 && names.every((name) => accounts.every((acct) => acct.top >= name.bottom || acct.bottom <= name.top))
          && new Set(accounts.map((acct) => Math.round(acct.left))).size === 1;
      })(),
      text: card.innerText.replace(/\s+/g, " ").trim(),
    };
  });
}

// The Usage page's Accounts section: its heading, whether the machine's own weekly limits are hidden, the notes under
// the rows, Add account (beside the Usage heading) and the rows' ⋯ menus, and each row by its account's address (on hover: motion.js moves a
// title to data-tip), with what it says.
function accountsNow(section) {
  const cards = {};
  for (const card of section.querySelectorAll(".hub-account")) {
    const rows = [...card.querySelectorAll(".hub-limit")].filter((row) => row.querySelector(".hub-limit-used")).map((row) => `${row.querySelector(".hub-limit-name").textContent} ${row.querySelector(".hub-limit-used").textContent}`);
    const line = card.querySelector(".hub-limits-at");
    cards[card.querySelector(".hub-account-who").dataset.tip] = rows.length ? rows.join(", ") : line.textContent;
  }
  return {
    title: section.querySelector(".section-head h2").textContent,
    ownHidden: document.getElementById("spend-limits").hidden,
    notes: [...section.querySelectorAll("#hub-account-list > .note")].map((el) => el.textContent),
    headers: [...section.querySelectorAll(".hub-account-columns [data-limit]")].map((el) => el.dataset.limit),
    opusHelp: (() => {
      const header = section.querySelector('.hub-account-columns [data-limit="opus"]');
      return header?.getAttribute("data-tip") || header?.getAttribute("title") || "";
    })(),
    add: section.ownerDocument.querySelectorAll(".hub-add").length,
    more: section.querySelectorAll(".hub-account .hub-more").length,
    cards,
  };
}

// The links of a list in order, as their addresses.
function hrefsOf(selector) {
  return [...document.querySelectorAll(selector)].map((el) => el.getAttribute("href"));
}

async function main() {
  const work = workFolder("cockpit-hub-limits-");
  const accounts = hubAccounts(Date.now());
  fs.writeFileSync(path.join(work, "hub.json"), JSON.stringify({ credentials: accounts }));
  const server = spawn(PYTHON, [path.join(__dirname, "serve_hub.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const info = await harnessUp(work, server);
    await hubRead(info);
    browser = await chromium.launch({ executablePath: CHROMIUM });
    const pageErrors = [];
    // The Usage page alone, wide and narrow, in both looks, while the hub answers: nothing runs past the window's
    // right edge.
    for (const [width, scheme] of [[1280, "light"], [1280, "dark"], [390, "light"]]) {
      const context = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: scheme, deviceScaleFactor: 2 });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const usage = await context.newPage();
      usage.on("pageerror", (error) => pageErrors.push(error.message));
      await usage.goto(`${info.origin}/usage`);
      await usage.locator("#hub-accounts .hub-account").first().waitFor({ timeout: 30e3 });
      await usage.waitForFunction(() => !document.querySelector("#spend-daily [data-ghost]"), null, { timeout: 60e3 });
      await usage.evaluate(() => document.fonts.ready);
      const wide = await usage.evaluate(() => ({
        page: document.documentElement.scrollWidth,
        cards: Math.max(...[...document.querySelectorAll(".hub-account")].map((el) => el.getBoundingClientRect().right)),
        window: document.documentElement.clientWidth,
      }));
      await usage.screenshot({ path: path.join(work, `usage-${width}${scheme === "dark" ? "-dark" : ""}.png`), fullPage: width < 640 });
      check(`the Usage page at ${width} px in the ${scheme} look fits the window`, wide.page <= wide.window && wide.cards <= wide.window, JSON.stringify(wide));
      await context.close();
    }
    for (const scheme of ["light", "dark"]) {
      const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: scheme, deviceScaleFactor: 3 });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      await routeMachine(page, LOW_MACHINE);
      page.on("pageerror", (error) => pageErrors.push(error.message));
      await page.goto(`${info.origin}/`);
      // The hub's first read finishes a moment after the page's first state.
      await page.waitForSelector("#usage .lane.claude .wk", { state: "attached", timeout: 30e3 });
      await page.evaluate(() => document.fonts.ready);
      await page.locator(".rail-foot").screenshot({ path: path.join(work, `foot-${scheme}.png`) });
      const foot = await page.evaluate(footNow);
      const said = await page.getAttribute("#usage a.usage.caps", "aria-label");
      const href = await page.getAttribute("#usage a.usage.caps", "href");
      check(`${scheme}: the foot shows Claude's week for all models and Codex's, without Fable's or Opus's, one bar per hub account, and no account of the machine's`,
        JSON.stringify(foot) === JSON.stringify(FOOT), JSON.stringify(foot));
      check(`${scheme}: its spoken words give each figure`, said.includes("All Claude models no figure and 40% and 80%; Codex 40%"), said);
      check(`${scheme}: it is a link to the Usage page`, href === "/usage", href);

      const card = await cardNow(page);
      await page.locator(".tip.tip-card").screenshot({ path: path.join(work, `card-${scheme}.png`) });
      check(`${scheme}: the hover card names each limit once, Claude's whole week first and then Fable's and Opus's, with one line per account`,
        String(card.names) === "All Claude models,Fable,Opus after Fable,Codex" && String(card.used) === USED && card.resets === 7, JSON.stringify(card));
      check(`${scheme}: each limit's name heads its own line, and every account's line starts at the same place`, card.headed, JSON.stringify(card));
      check(`${scheme}: the hover card is short, and names each line's account by its short name and plan`,
        card.text.length <= 250 && !/@/.test(card.text) && /Fable one .* two .*Codex plus · three .*Claude four has no figure until its next request\./.test(card.text), `${card.text.length} characters: ${card.text}`);
      check(`${scheme}: the hover card fits its box`, card.fits, JSON.stringify(card));
      await page.mouse.move(700, 450);

      if (scheme === "light") {
        const tabs = await page.evaluate(hrefsOf, ".page-tabs a");
        check("the page tabs have Usage and no Hub tab", tabs.includes("/usage") && !tabs.includes("/hub"), tabs.join(" "));
        await page.click("#usage a.usage.caps");
        const frame = page.frameLocator(".center-layer:not([hidden]) iframe.center-page");
        await frame.locator("#hub-accounts .hub-account").first().waitFor({ timeout: 30e3 });
        const listed = await frame.locator("#hub-accounts").evaluate(accountsNow);
        check("a press opens the Usage page in the centre, with the hub's Accounts in place of the machine's own weekly limits",
          listed.title === "Accounts" && listed.ownHidden, `${listed.title}, own limits hidden: ${listed.ownHidden}`);
        check("it has one row per hub account, each with its limits or why it has none",
          JSON.stringify(Object.keys(listed.cards).sort()) === JSON.stringify(Object.keys(CARDS).sort())
            && Object.entries(CARDS).every(([email, says]) => says.test(listed.cards[email])), JSON.stringify(listed.cards));
        check("the Usage page names active limit columns once, with the Opus explanation on its header",
          String(listed.headers) === "week,fable,opus" && listed.notes.length === 0 && listed.opusHelp.includes("Fable"), JSON.stringify(listed));
        check("the Usage page's accounts have their controls: Add account, and a ⋯ menu on each row",
          listed.add === 1 && listed.more === accounts.length, `${listed.add} Add account, ${listed.more} menus`);
        const menu = await frame.locator("body").evaluate(() => [...document.querySelectorAll(".brand-menu .menu-list a")].map((el) => el.getAttribute("href")));
        check("the Usage page's menu has Usage and no Hub entry", menu.includes("/usage") && !menu.includes("/hub"), menu.join(" "));
      } else {
        // The hub refuses Cockpit's key, as one whose key changed: Cockpit reads it again within a minute, keeps the
        // figures and shows a red sign.
        const keyFile = path.join(work, "hub", "management-key");
        const key = fs.readFileSync(keyFile, "utf8");
        fs.writeFileSync(keyFile, "a key the hub refuses");
        await page.waitForSelector("#usage .hub-fail", { timeout: 120e3 });
        const down = await page.evaluate(footNow);
        await page.locator(".rail-foot").screenshot({ path: path.join(work, "down.png") });
        check("with the hub down the foot keeps its figures", JSON.stringify(down) === JSON.stringify(FOOT), JSON.stringify(down));
        const downCard = await cardNow(page);
        check("with the hub down the hover card says so in one line", /Hub unreachable\. Figures from /.test(downCard.text) && String(downCard.used) === USED, downCard.text);
        fs.writeFileSync(keyFile, key);
      }
      await page.unrouteAll({ behavior: "wait" });
      await context.close();
    }
    // Cockpit fails the Usage page's next asks for the hub's accounts: the cards stay, and the hub's red line above the
    // page says so until the next answer. The page asks every 15 seconds, and is alone in its context here, since an
    // open Cockpit page asks whether the user is still there after two quiet minutes.
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
    const usage = await context.newPage();
    usage.on("pageerror", (error) => pageErrors.push(error.message));
    await usage.goto(`${info.origin}/usage`);
    await usage.locator("#hub-accounts .hub-account").first().waitFor({ timeout: 30e3 });
    const failing = (route) => route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "the test's failure" }) });
    await usage.route("**/api/hub", failing);
    const stale = usage.locator("#hub-banner .banner");
    await stale.waitFor({ timeout: 90e3 });
    const staleNote = await stale.innerText();
    const staleCards = await usage.locator("#hub-accounts .hub-account").count();
    check("a failed refresh of the Usage page keeps the cards and says in the hub's red line that the accounts did not load",
      staleCards === 4 && staleNote === "The hub's accounts did not load: the test's failure", `${staleCards} cards: ${staleNote}`);
    await usage.unroute("**/api/hub", failing);
    await stale.waitFor({ state: "detached", timeout: 90e3 });
    check("the next good refresh removes that line and keeps the cards", await usage.locator("#hub-accounts .hub-account").count() === 4, "cards missing");
    await context.close();
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
  const end = Date.now() + 180e3;
  while (Date.now() < end) {
    if (server.exitCode !== null) throw new Error(`serve_preview.py exited with ${server.exitCode}`);
    if (fs.existsSync(file)) {
      const info = JSON.parse(fs.readFileSync(file, "utf8"));
      const up = await fetch(`${info.origin}/healthz`).then((response) => response.ok, () => false);
      if (up) return info;
    }
    await sleep(200);
  }
  throw new Error("serve_preview.py did not come up within 180 s");
}

// Cockpit's first read of the stand-in hub, which finishes a while after it starts. A Usage page opened before then
// says it is reading the hub until its next ask, a minute later.
async function hubRead(info) {
  const end = Date.now() + 60e3;
  while (Date.now() < end) {
    const answer = await fetch(`${info.origin}/api/hub`, { headers: { cookie: `${info.name}=${info.value}` } }).then((response) => response.json());
    if (answer.hub.accounts) return;
    await sleep(500);
  }
  throw new Error("Cockpit did not read the stand-in hub within 60 s");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
