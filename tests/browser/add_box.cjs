// The Add dialog (SPEC CONTROL-8, CONTROL-9, FEEDBACK-4). Checks, on the main page at four widths:
// - Add opens a dialog over the page with its text box focused, with no text above the box and no list of earlier
//   to-dos; the panel fits the screen with no sideways scroll;
// - Escape and a press beside the panel close it, and the draft is there when it opens again;
// - Attach a part steps the dialog aside, and a click on the page brings it back with the part attached;
// - in feedback mode, Wider from the dialog's text area reaches the whole page, Narrower comes back the same way, and
//   the feedback box carries no explanation line;
// - Add's arrow menu offers Leave feedback instead, which moves the words after any draft in the feedback box for the
//   part picked next;
// - focus stays in the dialog and goes back to Add when it closes;
// - Enter adds the item and closes the dialog;
// - on a phone (touch, no hover), Enter starts a new line and the Add key adds the item with both lines.
// Saves a screenshot of the open dialog per width in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database, fake sessions; README.md says what
// it needs) and drives a headless Chromium. Prints one line per check and exits 1 when one failed.
// A run takes about a minute.
//
// Usage, from the checkout: node tests/browser/add_box.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

// Browser dependencies can be selected through the environment (README.md).
const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-add-box-");
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
    for (const [width, scheme] of [[1280, "dark"], [1100, "light"], [640, "dark"], [390, "light"]]) {
      const context = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: scheme });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      // The fixture can open the "Back after" dialog over the page while the user's timer is idle; answer it
      // with Return whenever it shows, so it never takes the test's clicks.
      await page.addLocatorHandler(page.locator("dialog#idle[open]"), (dialog) => dialog.locator("button", { hasText: "Return" }).click()
        // The test may close its page while an answer is under way.
        .catch((error) => { if (!/has been closed/.test(error.message)) throw error; }));
      page.setDefaultTimeout(5000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(`${info.origin}/classic`);
      await page.locator(".layout > .primary").waitFor();
      const dialog = page.locator("#add-box");
      const area = page.locator('#add-box textarea[data-key="add:new"]');

      await page.locator("#add").click();
      await area.waitFor();
      const opened = await page.evaluate(() => {
        const panel = document.querySelector("#add-box .add-panel").getBoundingClientRect();
        return {
          role: document.getElementById("add-box").getAttribute("role"),
          focused: document.activeElement && document.activeElement.dataset.key === "add:new",
          above: [...document.querySelector("#add-box .add-panel").children].filter((el) => !el.matches(".add-inner, .add-foot, .add-recent")).map((el) => el.textContent),
          label: document.getElementById("add-box").getAttribute("aria-label"),
          list: document.querySelectorAll("#asked, .asked-row").length,
          inside: panel.left >= 0 && panel.right <= window.innerWidth,
          wide: document.documentElement.scrollWidth - window.innerWidth,
        };
      });
      check(`${width} px: Add opens a dialog on its focused text box, nothing above it, with no to-do list`,
        opened.role === "dialog" && opened.label === "Add to your work" && opened.focused && opened.above.length === 0 && opened.list === 0, JSON.stringify(opened));
      check(`${width} px: the panel fits, no sideways scroll`, opened.inside && opened.wide <= 0, JSON.stringify(opened));
      await sleep(250);
      const shot = path.join(work, `add-box-${width}-${scheme}.png`);
      await page.screenshot({ path: shot });
      console.error(`  screenshot ${shot}`);

      await page.locator("#add-box .pin-key").click();
      await page.keyboard.press("Shift+Tab");
      await page.keyboard.press("Shift+Tab");
      const kept = await page.evaluate(() => document.getElementById("add-box").contains(document.activeElement));
      check(`${width} px: focus stays in the dialog after a press on Pin and Shift+Tab`, kept, "focus left the dialog");
      await page.locator("#add-box .pin-key").click();
      await area.fill(`Dialog test ${width}`);
      await area.press("Escape");
      check(`${width} px: Escape closes it`, await dialog.isHidden(), "still open");
      // Opened with a click, closing lets go of the focus, so Add shows no ring; opened with the A key,
      // the focus goes back to Add.
      check(`${width} px: opened with a click, Escape leaves no key focused`, await page.evaluate(() => document.activeElement === document.body), await page.evaluate(() => document.activeElement.outerHTML.slice(0, 80)));
      await page.keyboard.press("a");
      await area.waitFor();
      await area.press("Escape");
      check(`${width} px: opened with the A key, focus goes back to Add`, await page.evaluate(() => document.activeElement.id === "add"), "elsewhere");
      await page.locator("#add").click();
      check(`${width} px: the draft is there again`, (await area.inputValue()) === `Dialog test ${width}`, await area.inputValue());
      await page.mouse.click(4, 890);
      check(`${width} px: a press beside the panel closes it`, await dialog.isHidden(), "still open");

      await page.locator("#add").click();
      await page.locator("#add-box .add-foot button[aria-label^='Attach part']").click();
      check(`${width} px: Attach steps the dialog aside`, await dialog.isHidden(), "still open");
      await page.locator(".layout > .primary section:not(:empty)").first().click({ position: { x: 20, y: 10 } });
      await page.locator("#add-box .add-part").waitFor();
      check(`${width} px: the picked part comes back attached`, await area.isVisible(), "no text box");
      await page.locator("#add-box .add-part button[aria-label^='Remove attachment']").click();

      // On a phone the feedback key sits in the menu; the mode is the same.
      await page.evaluate(() => startPicking("feedback"));
      await area.click();
      const target = () => page.evaluate(() => ui.pick && ui.pick.target);
      const outward = [await target()];
      const wider = page.locator("#pick-box button", { hasText: "Wider" });
      while (outward.length < 8 && (await wider.count()) && !(await wider.isDisabled())) {
        await wider.click();
        outward.push(await target());
      }
      check(`${width} px: Wider moves out from the dialog's text area to the whole page`,
        outward[0] === "page:add-box/textarea" && outward[outward.length - 1] === "page:whole page", outward.join(" > "));
      const narrower = page.locator("#pick-box button", { hasText: "Narrower" });
      const inward = [await target()];
      while (inward.length < 8 && (await narrower.count()) && !(await narrower.isDisabled())) {
        await narrower.click();
        inward.push(await target());
      }
      check(`${width} px: Narrower comes back the same way`, JSON.stringify(inward) === JSON.stringify([...outward].reverse()), inward.join(" > "));
      const kind = await page.locator("#pick-box").innerText();
      check(`${width} px: the feedback box names its part and has no explanation line`,
        kind.includes("Feedback") && !kind.includes("next sweep"), kind);
      await page.locator("#pick-box button[aria-label^='Close']").click();
      if (await page.evaluate(() => ui.picking)) await page.keyboard.press("Escape");

      await page.evaluate(() => { ui.drafts.set("fb:pick", "Older feedback"); });
      await page.locator("#add-box summary.send-more").click();
      await page.locator("#add-box .feedback-way").click();
      const moved = await page.evaluate(() => ({ adding: ui.adding, picking: ui.picking, words: ui.drafts.get("fb:pick") }));
      check(`${width} px: Leave feedback instead closes Add and starts feedback with the words`,
        !moved.adding && moved.picking === "feedback" && moved.words === `Older feedback\n\nDialog test ${width}`, JSON.stringify(moved));
      // A click from the keyboard picks the control under it, wherever the pick bar sits on a phone.
      await page.evaluate(() => document.querySelector(".layout > .primary section:not(:empty)").click());
      const carried = await page.locator("#pick-box textarea").inputValue();
      check(`${width} px: the feedback box holds the words after the older draft`, carried === `Older feedback\n\nDialog test ${width}`, carried);
      await page.evaluate(() => { ui.drafts.delete("fb:pick"); closePick(); if (ui.picking) stopPicking(); });

      await page.locator("#add").click();
      await area.fill(`Dialog add ${width}`);
      await area.press("Enter");
      check(`${width} px: Enter adds it and closes the dialog`, await dialog.isHidden(), "still open");
      // The Add goes out once its Undo is over, 6 s on (CONTROL-18).
      const made = await waitFor(() => page.evaluate(async (title) => (await (await fetch("/api/state")).json()).items.some((item) => item.title === title), `Dialog add ${width}`), Boolean, 10000);
      check(`${width} px: the item exists`, made, "no item");
      check(`${width} px: no page errors`, errors.length === 0, errors.join(" | "));
      await context.close();
    }

    // A phone's keyboard has no Shift+Enter: there Enter starts a new line, and the Add key adds the item.
    {
      const context = await browser.newContext({ viewport: { width: 390, height: 900 }, isMobile: true, hasTouch: true });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      await page.addLocatorHandler(page.locator("dialog#idle[open]"), (dialog) => dialog.locator("button", { hasText: "Return" }).click()
        .catch((error) => { if (!/has been closed/.test(error.message)) throw error; }));
      page.setDefaultTimeout(5000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(`${info.origin}/classic`);
      const dialog = page.locator("#add-box");
      const area = page.locator('#add-box textarea[data-key="add:new"]');
      await page.locator("#add, .sidebar-add").first().tap();
      await area.fill("Phone add");
      await area.press("Enter");
      await area.pressSequentially("second line");
      check("touch: Enter starts a new line", (await area.inputValue()) === "Phone add\nsecond line", await area.inputValue());
      check("touch: the dialog stays open", await dialog.isVisible(), "closed");
      check("touch: the keyboard shows its return key", (await area.getAttribute("enterkeyhint")) === "enter", await area.getAttribute("enterkeyhint"));
      await page.locator("#add-box .add-split button.send").tap();
      check("touch: the Add key adds it and closes the dialog", await dialog.isHidden(), "still open");
      const summary = await waitFor(() => page.evaluate(async () => (await (await fetch("/api/state")).json()).items
        .find((item) => item.title === "Phone add")?.summary), Boolean, 10000);
      check("touch: the item keeps both lines", summary === "Phone add\nsecond line", summary);
      check("touch: no page errors", errors.length === 0, errors.join(" | "));
      await context.close();
    }
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failedChecks = results.filter((result) => !result.ok);
  console.error(failedChecks.length ? `${failedChecks.length} of ${results.length} checks failed` : `all ${results.length} checks passed`);
  process.exitCode = failedChecks.length ? 1 : 0;
}

// The value `read` gives once `ok` holds it, or its last value after 3 s.
async function waitFor(read, ok, ms) {
  const end = Date.now() + ms;
  let value = await read();
  while (!ok(value) && Date.now() < end) {
    await sleep(100);
    value = await read();
  }
  return value;
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
