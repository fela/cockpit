// The account problems on the main page (SPEC SESSION-19, UI-52), at 1280 px in the light look and 390 px in the dark:
// each has a warning row in Your queue whose words say in plain words what is wrong, what it breaks and what to do,
// with no paths in them; the row opens the problem on the card, where the paths sit under a closed Details that opens
// on a click and stays open through a poll's redraw; nothing is wider than the window.
// Makes the problems with accounts.link_problems on scratch account folders (work: its own transcripts folder and
// settings file; spare: transcripts reached through a link), hands them to serve_preview.py in <work>/account-
// problems.json, and drives a headless Chromium. Prints one line per check, saves each width's opened problem as
// account-problem-<width>.png in the work folder, and exits 1 when one failed.  Usage, from the checkout: node
// tests/browser/account_problems.cjs

"use strict";

const { execFileSync, spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

// Browser dependencies can be selected through the environment (README.md).
const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;
const ROOT = path.resolve(__dirname, "..", "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Scratch folders broken as the page's two kinds of problem, checked by the real account check.
const MAKE_PROBLEMS = `
import json, sys
from pathlib import Path
from cockpit import accounts, config
from cockpit.config import SHARED_ENTRIES, Account
config.SETUP_PATH = Path(sys.argv[2])
base = Path(sys.argv[1])
home = base / "claude"
for name in SHARED_ENTRIES:
    (home / name).mkdir(parents=True)
folders = [Account(name="personal", letter="P", folder=home, state=base / "p.json", default=True)]
for name in ("work", "spare"):
    folder = base / f"claude-{name}"
    folder.mkdir()
    for entry in SHARED_ENTRIES:
        (folder / entry).symlink_to(home / entry)
    folders.append(Account(name=name, letter={"work": "W", "spare": "S"}[name], folder=folder, state=base / f"{name}.json", default=False))
(base / "claude-work" / "projects").unlink()
(base / "claude-work" / "projects").mkdir()
(base / "claude-work" / "settings.json").unlink()
(base / "claude-work" / "settings.json").write_text("{}")
# The warning fixture exercises Linux sharing diagnostics on every host.
sys.platform = "linux"
problems = accounts.link_problems(tuple(folders))
print(json.dumps([{"text": problem.text, "detail": problem.detail} for problem in problems]))
`;

let failures = 0;
function check(ok, words) {
  console.log(`${ok ? "ok  " : "FAIL"} ${words}`);
  if (!ok) failures += 1;
}

async function main() {
  const work = workFolder("cockpit-account-problems-");
  const scratch = workFolder("cockpit-account-folders-");
  const problems = execFileSync(PYTHON, ["-c", MAKE_PROBLEMS, scratch, path.join(ROOT, "tests", "setup.toml")], { env: { ...process.env, PYTHONPATH: path.join(ROOT, "src") }, timeout: 15e3 }).toString();
  check(JSON.parse(problems).length === 2, "the account check finds the two problems");
  fs.writeFileSync(path.join(work, "account-problems.json"), problems);
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser;
  try {
    browser = await chromium.launch({ executablePath: CHROMIUM });
    const info = await harnessUp(work, server);
    for (const [width, scheme] of [[1280, "light"], [390, "dark"]]) {
      const context = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: scheme });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", (error) => errors.push(String(error)));
      await page.goto(`${info.origin}/`, { waitUntil: "networkidle" });
      const at = `${width} px ${scheme}`;
      // On a phone the list is a screen of its own, opened from the item screen's bar.
      if (width <= 1000) await page.locator(".phone-bar .phone-back").click();
      const rows = page.locator('#sidebar li.warn-row[data-warning^="account-problem:"]');
      await rows.first().waitFor({ timeout: 15e3 });
      check(await rows.count() === 2, `${at}: a warning row per problem`);
      const words = await rows.evaluateAll((els) => els.map((el) => el.querySelector(".sidebar-title").textContent));
      check(words.every((text) => !text.includes("/") && !text.includes(".sh")), `${at}: no path or file name in the words`);
      check(words[0].startsWith("The work account has stopped sharing your transcripts and memory notes and your settings"),
        `${at}: the first says what the work account stopped sharing`);
      check(words.every((text) => text.includes("Ask a session to")), `${at}: each says what to do`);
      const key = await rows.first().getAttribute("data-warning");
      await rows.first().locator(".side-row").click();
      const card = () => page.locator(`#next .warn-step[data-warning="${key}"]`);
      await card().waitFor();
      check((await card().locator(".ask-text").textContent()) === words[0], `${at}: the row opens its problem on the card`);
      const paths = card().locator(".warn-paths").first();
      check(!(await paths.isVisible()), `${at}: the paths start hidden`);
      await card().locator(".warn-detail > summary").click();
      check(await paths.isVisible(), `${at}: Details shows the paths`);
      check((await card().locator(".warn-detail").textContent()).includes(path.join(scratch, "claude-work", "projects")), `${at}: the paths name the folder`);
      check((await card().locator(".warn-detail").textContent()).includes("scripts/second-account.sh"), `${at}: the details name the script`);
      await page.evaluate(() => { ui.pendingRender = true; return refresh(); });
      await sleep(300);
      check(await card().locator(".warn-paths").first().isVisible(), `${at}: a redraw keeps Details open`);
      const wide = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      check(wide <= 0, `${at}: nothing wider than the window (${wide} px over)`);
      check(errors.length === 0, `${at}: no page errors ${errors.join("; ")}`);
      await page.locator("#next").screenshot({ path: path.join(work, `account-problem-${width}.png`) });
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
