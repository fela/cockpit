// Quotes the user makes with Comment (NEXT-33) read as quotes, not as ">" lines.
// At desktop light, desktop dark and phone width, on the first step card of Next up, checks:
// - after Comment and typing on, the reply box draws the quote line as a quote: its own letters are transparent over
//   a copy whose quote line has the quote style, the copy holds exactly the box's words, and its lines wrap where the
//   box's do (the same height);
// - a sent message with a quote shows it in the card's fold and in the history without a ">", and Show all opens it
//   as a block of its own.
// Saves screenshots of the reply box and of the fold in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/quote_display.cjs

"use strict";

const { spawn, execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;
const PYTHON = path.join(__dirname, "..", "..", ".venv", "bin", "python");
const QUOTE = "Export each list as a CSV file, with the completion date and the person who finished every task in it";
const REPLY = "Yes, and keep the time zone of the person who exported it.";
const SENT = `> ${QUOTE}\n\n${REPLY}`;

const VIEWS = [
  { name: "desktop-light", viewport: { width: 1280, height: 900 }, colorScheme: "light" },
  { name: "desktop-dark", viewport: { width: 1280, height: 900 }, colorScheme: "dark" },
  { name: "phone", viewport: { width: 390, height: 844 }, colorScheme: "light", isMobile: true, hasTouch: true },
];

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Screenshots an element the page may redraw under it (the fold redraws when its history arrives): tries again on
// the fresh element, five times at most.
async function shoot(locator, file) {
  for (let attempt = 1; ; attempt += 1) {
    try {
      await locator.scrollIntoViewIfNeeded({ timeout: 5e3 });
      await locator.screenshot({ path: file, timeout: 10e3 });
      return;
    } catch (error) {
      if (attempt === 5) throw error;
      await sleep(500);
    }
  }
}

// Runs SQL on the server's synthetic database.
function sql(work, statement, params) {
  const script = "import json, sqlite3, sys; c = sqlite3.connect(sys.argv[1]); c.execute(sys.argv[2], json.loads(sys.argv[3])); c.commit()";
  execFileSync(PYTHON, ["-c", script, path.join(work, "cockpit.db"), statement, JSON.stringify(params)]);
}

async function main() {
  const work = workFolder("cockpit-quote-display-");
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const info = await harnessUp(work, server);
    const cookie = `${info.name}=${info.value}`;
    const state = await fetch(`${info.origin}/api/state`, { headers: { cookie } }).then((response) => response.json());
    const step = state.queue[0];
    if (!step) throw new Error("no step in the fixture's queue");
    browser = await chromium.launch({ executablePath: CHROMIUM });

    const now = new Date().toISOString().replace(/\.\d+Z$/, "Z");
    sql(work, "INSERT INTO messages (author, item_id, session_id, step_id, text, state, created_at, sent_at)"
      + " VALUES ('user', ?, '22222222-2222-4222-8222-222222222222', ?, ?, 'sent', ?, ?)", [step.item_id, step.id, SENT, now, now]);

    for (const view of VIEWS) {
      const { name, ...options } = view;
      console.error(name);
      const context = await browser.newContext(options);
      await context.setExtraHTTPHeaders({ cookie });
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.addLocatorHandler(page.locator("dialog#idle[open]"), (dialog) => dialog.locator("button", { hasText: "Return" }).click());
      await page.goto(`${info.origin}/`, { timeout: 120e3 });
      const card = page.locator(`article.next-card[data-step="${step.id}"]`);
      await card.waitFor({ timeout: 30e3 });

      // A sent message with a quote, in the fold and in the history.
      const fold = page.locator(`.hist-fold-card[data-item="${step.item_id}"]:visible`).first();
      const said = fold.locator(".fold-said");
      const quote = said.locator(".said-quote");
      const shown = await quote.waitFor({ timeout: 20e3 }).then(() => true, () => false);
      const saidText = await said.innerText().catch(() => "");
      check("the fold shows the sent quote as a quote, with no >", shown && !saidText.includes(">") && saidText.includes(REPLY.split(" ")[0]), saidText);
      await shoot(said, path.join(work, `${name}-sent.png`));
      const more = said.locator(".fold-said-more");
      if (await more.count()) {
        await more.click();
        const block = await said.locator(".said-quote").evaluate((el) => getComputedStyle(el).display);
        check("opened, the quote is a block of its own", block === "block", block);
        await shoot(said, path.join(work, `${name}-sent-open.png`));
      }
      await fold.locator(".fold-all > summary").first().click();
      const you = fold.locator(".hist-you", { has: page.locator(".said-quote") }).first();
      const inHistory = await you.waitFor({ timeout: 5e3 }).then(() => true, () => false);
      check("the history shows the quote as a quote", inHistory && !(await you.innerText()).includes(">"), inHistory ? await you.innerText() : "no quote in the history");

      // Comment on the step's short line, then type on.
      await card.locator(".short-line").first().click({ clickCount: 3 });
      await page.locator(".quote-button").click();
      await sleep(300);
      await page.keyboard.type(REPLY);
      await sleep(400);
      const reply = card.locator(`textarea[data-key="msg:step:${step.id}"]`);
      const look = await reply.evaluate((area) => {
        const field = area.parentElement;
        const copy = field.querySelector(".quote-copy");
        const line = copy && copy.querySelector(".quote-line");
        return {
          quoting: field.classList.contains("quoting"),
          color: getComputedStyle(area).color,
          copyText: copy ? [...copy.children].map((row) => row.textContent.replace(/\u200b/g, "")).join("\n") : null,
          value: area.value,
          bar: line ? getComputedStyle(line).boxShadow : "",
          mark: line ? getComputedStyle(line.querySelector(".quote-mark")).color : "",
          heights: copy ? [copy.scrollHeight, area.scrollHeight] : [],
        };
      });
      check("the reply box draws its quote line through the copy", look.quoting && look.color === "rgba(0, 0, 0, 0)", JSON.stringify(look));
      check("the copy holds exactly the box's words", look.copyText === look.value, `${JSON.stringify(look.copyText)} vs ${JSON.stringify(look.value)}`);
      check("the quote line has its bar and an invisible >", look.bar.includes("inset") && look.mark === "rgba(0, 0, 0, 0)", `${look.bar} / ${look.mark}`);
      check("the copy's lines wrap where the box's do", Math.abs(look.heights[0] - look.heights[1]) <= 1, look.heights.join(" vs "));
      await reply.evaluate((area) => area.closest(".composer").scrollIntoView({ block: "center" }));
      await card.locator(".composer").first().screenshot({ path: path.join(work, `${name}-reply.png`) });
      check("no page errors", errors.length === 0, errors.join(" | "));
      await context.close();
    }
  } finally {
    if (browser) await browser.close();
    server.kill("SIGTERM");
  }
  const failed = results.filter((result) => !result.ok);
  console.error(`${results.length - failed.length}/${results.length} checks passed`);
  console.error(`screenshots kept in ${work}`);
  if (failed.length) process.exitCode = 1;
}

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
