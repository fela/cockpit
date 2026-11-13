// The warnings on the main page as rows in Inbox (SPEC UI-52), at 1280 px in the light look, 1100 px in the dark with
// reduced motion, and 390 px in the dark: every kind at once (a signed-out Claude account, one whose sign-in ends
// soon, the account signed out, a session on the wrong model, a warning the server sends, an account problem). Checks
// that - no warning is a card above the step; only the question about the user's team stays there; - each warning is
// a row in Inbox, red ones first, in the page's own red: a pale red ground, red words and a mark   in the text colour
// (dark on the light page, light on the dark one), pulsing (still with reduced motion), all on   one clock that a
// redraw keeps; - with nothing picked and the first step not opened, Next up shows the first warning as a card with
// its fix, and   the rows head Inbox; the ready-next steps fold into its "N more" row; - once the user is on a step,
// the rows stay above that step's row and the change-order arrows on Next up are dark red, still for the warnings
// already there and pulsing once a new one comes, which their card names first; hovering them
// shows a small card with the next warning's few words, its button and how many more;   the card stays open on its
// button and across a redraw; pressing them opens that warning, also one with long   words fits the window; pressing
// a row opens its warning; - closing an opened warning while a step holds its place opens the next warning, not the
// step; - J/K move onto the warning rows and Enter opens the highlighted one; - nothing is wider than the window, and
// the page throws no errors.  Serves this checkout with serve_preview.py and fills each /api/state answer with the
// warnings, so the check needs no signed-out account. Prints one line per check, saves screenshots as queue-
// warnings-<width>-*.png in the work folder, and exits 1 when one failed.  Usage, from the checkout: node
// tests/browser/queue_warnings.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder, fullState } = require("./work.cjs");

// Browser dependencies can be selected through the environment (README.md).
const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;
const ROOT = path.resolve(__dirname, "..", "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const AGO = new Date(Date.now() - 20 * 60e3).toISOString();
// In the order the server sends them; the page puts red first.
const WARNINGS = {
  warnings: ["The sweep that started at 03:00 UTC last wrote at 03:05 UTC and stopped without finishing, and the restart Cockpit sent at 03:30 UTC has started no new sweep."],
  account_problems: [{ text: "The work account has stopped sharing skills with the personal account.", detail: "/home/u/.claude-work/skills\n/home/u/.claude/skills" }],
  team: null,
  codex_sign_ins: [],
  claude_sign_ins: [
    { account: "personal", signed_out: false, text: "The personal Claude account's sign-in ends Thu 15 Jan 10:00 UTC." },
    { account: "work", signed_out: true, text: "The work Claude account is signed out: its sessions answer \"Login expired\" and new sessions go to the other account." },
  ],
  wrong_models: [{
    session_id: "33333333-3333-4333-8333-333333333333", name: "Reminder fix", model: "claude-sonnet-5-5", intended: "claude-opus-5-5",
    first: AGO, last: AGO, calls: 10, back: false,
  }],
  // This fixture leaves blocked calls empty; tests/browser/blocked_warnings.cjs covers those warnings (PERM-25).
  permission_requests: [], settled_requests: [], waiting_sessions: [], codex_requests: [],
};
// Each row's words, top to bottom.
const ROWS = [
  "Claude work is signed out",
  "“Reminder fix” ran on claude-sonnet-5-5",
  "Claude personal's sign-in ends soon",
  "The sweep that started at 03:00 UTC",
  "The work account has stopped sharing skills",
];

let failures = 0;
function check(ok, words) {
  console.log(`${ok ? "ok  " : "FAIL"} ${words}`);
  if (!ok) failures += 1;
}

// The lightness of a computed oklch(), oklab() or rgb() colour, 0 to 1.
function lightness(color) {
  const oklch = /okl(?:ch|ab)\(([\d.]+)/.exec(color);
  if (oklch) return Number(oklch[1]);
  const [r, g, b] = (color.match(/[\d.]+/g) || []).map(Number);
  return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
}

async function main() {
  const work = workFolder("cockpit-queue-warnings-");
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser;
  try {
    browser = await chromium.launch({ executablePath: CHROMIUM });
    const info = await harnessUp(work, server);
    for (const [width, scheme, motion] of [[1280, "light", "no-preference"], [1100, "dark", "reduce"], [390, "dark", "no-preference"]]) {
      const phone = width < 600;
      const context = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: scheme, reducedMotion: motion });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      await context.route("**/api/state", async (route) => {
        // A poll still on its way when the context closes has nothing to answer.
        const full = await fullState(route).catch(() => null);
        if (!full) return;
        const state = await full.response.json();
        // The fixture can hold a question about time away, whose dialog would cover the page.
        await route.fulfill({ ...full, json: { ...state, ...WARNINGS, away: null } }).catch(() => {});
      });
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", (error) => errors.push(String(error)));
      await page.goto(`${info.origin}/`, { waitUntil: "load", timeout: 120e3 });
      const at = `${width} px ${scheme}${motion === "reduce" ? " reduced motion" : ""}`;
      await page.waitForSelector("#sidebar li.warn-row", { state: "attached", timeout: 60e3 });
      // Start from Next up with nothing picked and no step opened.
      await page.evaluate(() => { ui.placed.clear(); selectItem(null); });
      await sleep(400);

      const banner = await page.evaluate(() => [...document.querySelectorAll("#banner > *")].map((card) => card.className));
      check(banner.length === 1 && banner[0].includes("team-ask"), `${at}: above the step only the team question ${JSON.stringify(banner)}`);

      // On a phone the list is a screen of its own, and rows animate only while it shows.
      if (phone) await page.evaluate(() => setPhoneScreen(true, false));
      const rows = await page.evaluate(() => [...document.querySelectorAll("#sidebar .sidebar-group.queue li.warn-row")].map((row) => {
        const button = row.querySelector(".side-row");
        const style = getComputedStyle(button);
        const root = getComputedStyle(document.documentElement);
        return { words: row.textContent, background: style.backgroundColor,
          color: getComputedStyle(row.querySelector(".sidebar-title")).color,
          mark: getComputedStyle(row.querySelector(".sidebar-lead > .icon")).color,
          fail: root.getPropertyValue("--fail").trim(), pickedBg: root.getPropertyValue("--picked-bg").trim(),
          pickedFail: root.getPropertyValue("--picked-fail").trim(), picked: row.classList.contains("sel"),
          pulse: button.getAnimations().map((one) => one.animationName).join(",") };
      }));
      check(rows.length === ROWS.length && ROWS.every((words, index) => rows[index]?.words.startsWith(words)),
        `${at}: a row per warning in Inbox, red first ${JSON.stringify(rows.map((row) => row.words.slice(0, 32)))}`);
      const plain = rows.filter((row) => !row.picked);
      const dark = scheme === "dark";
      check(plain.length >= ROWS.length - 1 && plain.every((row) => (dark ? lightness(row.background) < 0.4 : lightness(row.background) > 0.85)
        && Math.abs(lightness(row.color) - lightness(row.fail)) < 0.03),
        `${at}: rows in the page's own red: a ${dark ? "dark" : "pale"} red ground and red words ${JSON.stringify(plain.slice(0, 1).map((row) => [row.background, row.color, row.fail]))}`);
      check(rows.every((row) => row.picked || (dark ? lightness(row.mark) > 0.85 : lightness(row.mark) < 0.35)),
        `${at}: each row's mark is ${dark ? "light on the dark page" : "dark on the light page"} ${JSON.stringify(rows.slice(0, 1).map((row) => row.mark))}`);
      const pickedRows = rows.filter((row) => row.picked);
      check(pickedRows.every((row) => row.pulse === "" && Math.abs(lightness(row.background) - lightness(row.pickedBg)) < 0.03
        && Math.abs(lightness(row.color) - lightness(row.pickedFail)) < 0.03),
        `${at}: a picked warning row holds still in the picked row's colours ${JSON.stringify(pickedRows.map((row) => [row.background, row.color, row.pulse]))}`);
      if (motion === "reduce") check(rows.every((row) => row.pulse === ""), `${at}: with reduced motion the rows hold still`);
      else {
        check(plain.every((row) => row.pulse === "warn-row-pulse"), `${at}: the rows pulse`);
        // A redraw draws new rows; their pulse carries on where the old ones were, all on the page's clock.
        const clock = await page.evaluate(async () => {
          const old = document.querySelector("#sidebar li.warn-row");
          ui.pendingRender = true;
          await refresh();
          const pulses = document.getAnimations().filter((one) => ["warn-pulse", "warn-row-pulse"].includes(one.animationName));
          return { replaced: document.querySelector("#sidebar li.warn-row") !== old, starts: [...new Set(pulses.map((one) => one.startTime))] };
        });
        check(clock.starts.length === 1 && clock.starts[0] === 0, `${at}: every pulse keeps the page's clock across a redraw ${JSON.stringify(clock)}`);
      }

      if (phone) {
        await page.screenshot({ path: path.join(work, `queue-warnings-${width}-list.png`) });
        await page.evaluate(() => setPhoneScreen(false, false));
      }
      // Nothing picked: the first warning is Next up, and the rows head Inbox.
      const card = await page.evaluate(() => {
        const next = document.querySelector("#next > .warn-step");
        const list = [...document.querySelectorAll("#sidebar .sidebar-group.queue .sidebar-list > li")];
        return { ask: next?.querySelector(".ask")?.textContent || "", button: next?.querySelector(".answers button")?.textContent || "",
          firstRow: list[0]?.classList.contains("warn-row") || false, sel: document.querySelector("#sidebar li.warn-row.sel")?.textContent || "",
          steps: list.filter((row) => row.dataset.step).length };
      });
      check(card.ask.startsWith("The work Claude account is signed out") && card.button === "Sign in to work",
        `${at}: Next up shows the first warning with its fix ${JSON.stringify(card)}`);
      check(card.firstRow && card.sel.startsWith(ROWS[0]), `${at}: the rows head Inbox, the shown one picked`);
      const waitingNext = await page.evaluate(() => ({
        group: Boolean(document.querySelector("#sidebar .sidebar-group.ready")),
        more: document.querySelector("#sidebar .sidebar-group.queue li.sidebar-more")?.textContent || null,
      }));
      check(!waitingNext.group && (waitingNext.more === null || /^Show \d+ more$/.test(waitingNext.more)),
        `${at}: the ready-next steps fold into Inbox's "Show N more" row ${JSON.stringify(waitingNext)}`);
      await page.screenshot({ path: path.join(work, `queue-warnings-${width}-next.png`) });

      if (card.steps === 0) check(false, `${at}: the preview's queue has a step to stand on`);
      else {
        // On a step: the rows stay above its row, and the arrows on Next up are dark red and pulse.
        const stepId = await page.evaluate(() => Number(document.querySelector("#sidebar .sidebar-group.queue li[data-step]").dataset.step));
        if (phone) await page.evaluate((id) => selectItem(ui.state.queue.find((step) => step.id === id).item_id), stepId);
        else await page.locator(`#sidebar .sidebar-group.queue li[data-step="${stepId}"] .side-row`).click();
        await page.waitForSelector("#next .warn-key", { timeout: 15e3 });
        // Let the card's scroll into place and the phone's screen change settle before the pointer moves.
        await sleep(600);
        const on = await page.evaluate(([id, warnings]) => {
          const list = [...document.querySelectorAll("#sidebar .sidebar-group.queue .sidebar-list > li")];
          const at = list.findIndex((row) => Number(row.dataset.step) === id);
          const key = document.querySelector("#next .warn-key");
          const style = getComputedStyle(key);
          return { above: at === warnings && list.slice(0, warnings).every((row) => row.classList.contains("warn-row")), card: document.querySelector("#next > .next-card")?.dataset.step,
            background: style.backgroundColor, color: style.color, pulse: key.getAnimations().map((one) => one.animationName).join(","),
            peekShown: getComputedStyle(document.querySelector("#next .warn-peek")).display !== "none" };
        }, [stepId, ROWS.length]);
        check(on.above && Number(on.card) === stepId, `${at}: on a step, the warning rows stay above its row ${JSON.stringify(on)}`);
        // Every warning was there before the step came up, so the arrows hold still.
        check(lightness(on.background) < 0.6 && lightness(on.color) > 0.9 && on.pulse === "",
          `${at}: the arrows are dark red with a light icon and hold still for warnings already there ${JSON.stringify([on.background, on.color, on.pulse])}`);
        check(!on.peekShown, `${at}: the small card stays hidden until the arrows are hovered`);
        await page.hover("#next .warn-key");
        await sleep(150);
        const peek = await page.evaluate(() => {
          const box = document.querySelector("#next .warn-peek");
          const rect = box.getBoundingClientRect();
          return { shown: getComputedStyle(box).display !== "none", words: box.querySelector(".warn-peek-title").textContent,
            button: box.querySelector(".warn-peek-actions button")?.textContent || "", more: box.querySelector(".warn-peek-more")?.textContent || "",
            text: box.textContent.length, left: rect.left, right: rect.right };
        });
        check(peek.shown && peek.words === ROWS[0] && peek.button === "Sign in to work" && peek.more === `+${ROWS.length - 1} more`,
          `${at}: hovering the arrows shows the next warning's few words and its button ${JSON.stringify(peek)}`);
        check(peek.text < 60, `${at}: the small card holds no explanation (${peek.text} characters)`);
        check(peek.left >= 0 && peek.right <= width, `${at}: the small card fits the window (${Math.round(peek.left)}–${Math.round(peek.right)} px)`);
        // A server warning's or account problem's words can be long; the card wraps them inside the window.
        const long = await page.evaluate(() => {
          const title = document.querySelector("#next .warn-peek-title");
          const words = title.lastChild.textContent;
          title.lastChild.textContent = "The work account has stopped sharing skills with the personal account and its plugins too";
          const rect = document.querySelector("#next .warn-peek").getBoundingClientRect();
          title.lastChild.textContent = words;
          return { left: rect.left, right: rect.right };
        });
        check(long.left >= 0 && long.right <= width, `${at}: a small card with long words fits the window (${Math.round(long.left)}–${Math.round(long.right)} px)`);
        // The pointer can move from the arrows onto the card's button.
        await page.hover("#next .warn-peek-actions button");
        await sleep(150);
        check(await page.locator("#next .warn-peek").isVisible(), `${at}: the small card stays open on its button`);
        await page.screenshot({ path: path.join(work, `queue-warnings-${width}-hover.png`) });
        // A redraw draws a new pin under the resting pointer; its card stays open, also after the frame that shuts a
        // card nothing rests on.
        const redrawn = await page.evaluate(async () => {
          const old = document.querySelector("#next .warn-pin");
          render(false);
          await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
          const peek = document.querySelector("#next .warn-peek");
          return { replaced: document.querySelector("#next .warn-pin") !== old, shown: Boolean(peek) && getComputedStyle(peek).display !== "none" };
        });
        check(redrawn.replaced && redrawn.shown, `${at}: the small card stays open across a redraw ${JSON.stringify(redrawn)}`);
        // With the pointer away and focus on the card's button, a redraw keeps the card open; focus leaving shuts it.
        await page.mouse.move(5, 890);
        // As Tab gets there: the arrows first, which shows the card, then its button.
        await page.focus("#next .warn-key");
        await page.focus("#next .warn-peek-actions button");
        const focused = await page.evaluate(async () => {
          ui.pendingRender = true;
          await refresh();
          await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
          const shown = () => getComputedStyle(document.querySelector("#next .warn-peek")).display !== "none";
          const kept = shown();
          const where = `${document.activeElement?.tagName}.${document.activeElement?.className} in pin: ${Boolean(document.activeElement?.closest(".warn-pin"))}`;
          document.activeElement?.blur();
          return { kept, shut: !shown(), where };
        });
        check(focused.kept && focused.shut, `${at}: a focused small card stays open across a redraw and shuts once focus leaves ${JSON.stringify(focused)}`);
        // A redraw that puts focus outside the new pin, as when the focused button's warning went: the card shuts.
        await page.focus("#next .warn-key");
        await page.focus("#next .warn-peek-actions button");
        const elsewhere = await page.evaluate(async () => {
          render(false);
          const outside = [...document.querySelectorAll("#next > .next-card button")].find((button) => button.checkVisibility());
          outside?.focus();
          await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
          return { shown: getComputedStyle(document.querySelector("#next .warn-peek")).display !== "none", moved: document.activeElement === outside };
        });
        check(elsewhere.moved && !elsewhere.shown, `${at}: a redraw that leaves focus outside the arrows shuts the small card ${JSON.stringify(elsewhere)}`);
        // A warning that comes while the user is on the step makes the arrows pulse and comes first in their small card;
        // once it goes, they hold still again.
        const NEWER = "The sweep that started at 04:20 UTC stopped without finishing.";
        // A poll already on its way when the warnings change brings the state from before, so each check waits for the
        // card to name the warning it expects.
        const shownFirst = (words) => page.waitForFunction((words) => {
          if (document.querySelector("#next .warn-peek-title")?.textContent === words) return true;
          ui.pendingRender = true;
          refresh();
          return false;
        }, words, { timeout: 30e3, polling: 500 }).catch(() => {});
        WARNINGS.warnings.push(NEWER);
        await shownFirst(NEWER);
        const arrived = await page.evaluate(async () => {
          const key = document.querySelector("#next .warn-key");
          return { pulse: key.getAnimations().map((one) => one.animationName).join(","), words: document.querySelector("#next .warn-peek-title").textContent,
            more: document.querySelector("#next .warn-peek-more")?.textContent || "", card: document.querySelector("#next > .next-card")?.dataset.step };
        });
        check(arrived.pulse === (motion === "reduce" ? "" : "warn-pulse") && arrived.words === NEWER && arrived.more === `+${ROWS.length} more`
          && Number(arrived.card) === stepId, `${at}: a warning that came after the step came up pulses the arrows and comes first ${JSON.stringify(arrived)}`);
        await page.hover("#next .warn-key");
        await sleep(150);
        await page.screenshot({ path: path.join(work, `queue-warnings-${width}-newer.png`) });
        await page.mouse.move(5, 890);
        WARNINGS.warnings.pop();
        await shownFirst(ROWS[0]);
        const gone = await page.evaluate(async () => {
          return { pulse: document.querySelector("#next .warn-key").getAnimations().length, words: document.querySelector("#next .warn-peek-title").textContent };
        });
        check(gone.pulse === 0 && gone.words === ROWS[0], `${at}: with the newer warning gone the arrows hold still again ${JSON.stringify(gone)}`);
        await page.locator("#next .warn-key").click();
        await page.waitForSelector("#next > .warn-step", { timeout: 15e3 });
        check((await page.locator("#next > .warn-step .ask").textContent()).startsWith("The work Claude account is signed out"),
          `${at}: pressing the arrows opens the next warning`);
      }

      // A warning's row opens it.
      if (phone) await page.evaluate(() => openWarning("claude:work"));
      else await page.locator("#sidebar li.warn-row", { hasText: "Claude work is signed out" }).locator(".side-row").click();
      await page.waitForFunction(() => document.querySelector("#next > .warn-step")?.dataset.warning === "claude:work", null, { timeout: 15e3 });
      check(await page.locator("#next > .warn-step .answers button", { hasText: "Sign in to work" }).count() === 1, `${at}: a row opens its warning, with its fix`);
      check((await page.locator("#sidebar li.warn-row.sel").textContent()).startsWith("Claude work"), `${at}: the opened warning's row is picked`);
      // The wrong-model warning keeps its close; the account problem its details.
      if (phone) await page.evaluate(() => openWarning("wrong-model:33333333-3333-4333-8333-333333333333:claude-sonnet-5-5"));
      else await page.locator("#sidebar li.warn-row", { hasText: "Reminder fix" }).locator(".side-row").click();
      await page.waitForSelector("#next > .warn-step .answers button", { timeout: 15e3 });
      check(await page.locator("#next > .warn-step .answers button", { hasText: "Close this warning" }).count() === 1, `${at}: the wrong-model warning keeps its close`);
      // The step picked earlier still holds its place; closing this warning goes on to the next warning.
      await page.locator("#next > .warn-step .answers button", { hasText: "Close this warning" }).click();
      await page.waitForFunction(() => !document.querySelector("#next > .warn-step[data-warning^='wrong-model']"), null, { timeout: 15e3 });
      const after = await page.evaluate(() => document.querySelector("#next > .next-card")?.dataset.warning || "");
      check(after === "claude:work", `${at}: closing a warning opens the next warning (${JSON.stringify(after)})`);
      if (!phone) {
        // J/K reach the warning rows; Enter opens the highlighted one.
        await page.evaluate(() => { selectItem(null); document.activeElement?.blur(); });
        await sleep(300);
        let reached = "";
        for (let press = 0; press < 4 && !reached; press++) {
          await page.keyboard.press("j");
          reached = await page.evaluate(() => document.querySelector("#sidebar li.warn-row.keyboard-current")?.dataset.warning || "");
        }
        check(reached !== "", `${at}: J reaches a warning row (${JSON.stringify(reached)})`);
        if (reached) {
          await page.keyboard.press("Enter");
          await sleep(300);
          const opened = await page.evaluate(() => document.querySelector("#next > .next-card")?.dataset.warning || "");
          check(opened === reached, `${at}: Enter opens the highlighted warning (${JSON.stringify(opened)})`);
        }
      }
      const wide = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      check(wide <= 0, `${at}: nothing wider than the window (${wide} px over)`);
      check(errors.length === 0, `${at}: no page errors ${errors.join("; ")}`);
      await page.screenshot({ path: path.join(work, `queue-warnings-${width}-open.png`) });
      await context.unrouteAll({ behavior: "ignoreErrors" });
      await context.close();
    }
  } finally {
    try {
      if (browser) await browser.close();
    } finally {
      server.kill();
      await new Promise((resolve) => (server.exitCode !== null || server.signalCode !== null ? resolve() : server.once("exit", resolve)));
    }
  }
  console.error(`${failures} failed; work dir ${work}`);
  process.exitCode = failures ? 1 : 0;
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

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
