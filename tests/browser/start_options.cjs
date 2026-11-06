// The settings a new session starts with (SPEC SESSION-22). With Claude and Codex both on, checks:
// - the New session box shows a button per choice, Agent, Model, Effort and Account, each with its icon, starting on
//   Claude Opus 5.5 at high effort with the account picked automatically;
// - each button opens its own list, which marks the default; effort draws one to five bars, each account shows its
//   initial;
// - picking Codex resets the model and effort to Codex's own defaults and drops the account;
// - the message Send makes names the agent, model and effort picked, and the picks go with the draft;
// - a message to a running session shows no such line;
// - the Add dialog offers the same settings, and the item Add makes carries them;
// - the buttons and an open list fit at 1440 px in light mode and 390 px in dark mode, with no sideways scroll or page
//   errors.
//
// Starts serve_preview.py (this checkout's Cockpit on a copy of the live database, fake sessions; README.md says what
// it needs) with Codex turned on, and drives a headless Chromium. Prints one line per check and exits 1 when one
// failed. Screenshots go to the folder given as the first argument, when one is given.
//
// Usage, from the checkout: node tests/browser/start_options.cjs [screenshot folder]

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
  const shots = process.argv[2] || null;
  if (shots) fs.mkdirSync(shots, { recursive: true });
  const work = workFolder("cockpit-start-options-");
  fs.writeFileSync(path.join(work, "providers.json"), JSON.stringify(["claude", "codex"]));
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
    const errors = [];
    const open = async (options) => {
      const context = await browser.newContext(options);
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      page.setDefaultTimeout(5000);
      page.on("pageerror", (error) => { errors.push(error.message); console.error(`  page error: ${error.stack || error.message}`); });
      const posted = [];
      page.on("response", (response) => {
        const request = response.request();
        if (request.method() === "POST" && /\/api\/(messages|items\/add)$/.test(new URL(request.url()).pathname)) {
          posted.push({ path: new URL(request.url()).pathname, body: request.postDataJSON(), status: response.status() });
        }
      });
      await page.goto(`${info.origin}/`);
      await page.waitForFunction(() => typeof ui !== "undefined" && ui.state);
      return { context, page, posted };
    };
    const wide = (page) => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    // The start buttons under `scope` as they read, such as "Claude | Opus 5.5 | high | Auto", and their icons.
    const line = (page, scope) => page.evaluate((scope) => [...document.querySelectorAll(`${scope} .start-options .start-button`)]
      .map((button) => button.textContent.trim()).join(" | "), scope);
    const buttonIcons = (page, scope) => page.evaluate((scope) => [...document.querySelectorAll(`${scope} .start-options .start-button`)]
      .map((button) => [...button.querySelectorAll(".icon:not(.icon-chevron)")].map((each) => each.classList[1].slice("icon-".length))), scope);
    // Opens the list of the button `key` under `scope` and picks `value` ("" for Automatic).
    const choose = async (page, scope, key, value) => {
      await page.locator(`${scope} .start-button[data-key="${key}"]`).click();
      await page.locator(`${scope} .start-pick:has(> .start-button[data-key="${key}"]) .menu-list button[data-value="${value}"]`).click();
    };

    let { context, page, posted } = await open({ viewport: { width: 1440, height: 900 }, colorScheme: "light" });
    await page.evaluate(() => startKeyboardSession());
    const area = page.locator('#work textarea[data-key="msg:new-session"]');
    await area.fill("Look into the slow page");
    const box = page.locator("#work .composer:has(textarea[data-key='msg:new-session'])");
    await box.locator(".start-button").first().waitFor();
    check("the New session box shows a button per choice, each with its icon",
      (await line(page, "#work")) === "Claude | Opus 5.5 | high | Auto"
        && JSON.stringify(await buttonIcons(page, "#work")) === '[["claude"],[],["effort-3"],["meter"]]',
      `${await line(page, "#work")} ${JSON.stringify(await buttonIcons(page, "#work"))}`);
    const keys = await box.locator(".start-button").evaluateAll((buttons) => buttons.map((button) => button.dataset.key.split(":").pop()));
    check("the buttons are Agent, Model, Effort and Account", JSON.stringify(keys) === '["provider","model","effort","account"]',
      JSON.stringify(keys));
    const marked = await box.locator(".start-pick").evaluateAll((menus) =>
      menus.map((menu) => menu.querySelector(".menu-list button.picked")?.dataset.value));
    check("each list marks the default as picked", JSON.stringify(marked) === '["claude","claude-opus-5-5","high",""]',
      JSON.stringify(marked));
    await box.locator('.start-button[data-key="msg:new-session:effort"]').click();
    const efforts = await box.locator('.start-pick:has(> [data-key="msg:new-session:effort"]) .menu-list button').evaluateAll((buttons) =>
      buttons.map((button) => `${button.dataset.value}:${button.querySelector(".menu-label .icon").classList[1]}`));
    check("the effort list draws one to five bars for low to max",
      JSON.stringify(efforts) === '["low:icon-effort-1","medium:icon-effort-2","high:icon-effort-3","xhigh:icon-effort-4","max:icon-effort-5"]',
      JSON.stringify(efforts));
    if (shots) await page.screenshot({ path: path.join(shots, "new-session-effort-1440-light.png") });
    await page.keyboard.press("Escape");
    await box.locator('.start-button[data-key="msg:new-session:account"]').click();
    const letters = await box.locator('.start-pick:has(> [data-key="msg:new-session:account"]) .menu-list button').evaluateAll((buttons) =>
      buttons.map((button) => button.querySelector(".menu-label").textContent.trim()));
    check("the account list offers Automatic, then W and P for the work and personal accounts",
      JSON.stringify(letters) === '["Automatic","Ppersonal","Wwork"]', JSON.stringify(letters));
    if (shots) await page.screenshot({ path: path.join(shots, "new-session-account-1440-light.png") });
    await box.locator('.start-pick:has(> [data-key="msg:new-session:account"]) .menu-list button[data-value="work"]').click();
    check("a picked account shows its letter", (await line(page, "#work")) === "Claude | Opus 5.5 | high | W", await line(page, "#work"));
    check("the list closes on a pick", await page.evaluate(() => !document.querySelector("#work .start-pick[open]")), "still open");
    await choose(page, "#work", "msg:new-session:provider", "claude");
    check("picking the agent already picked keeps the other picks and focus on its button",
      (await line(page, "#work")) === "Claude | Opus 5.5 | high | W"
        && await page.evaluate(() => document.activeElement.dataset.key === "msg:new-session:provider"),
      await line(page, "#work"));
    await choose(page, "#work", "msg:new-session:account", "");

    await choose(page, "#work", "msg:new-session:provider", "codex");
    const codex = await box.locator(".start-button").evaluateAll((buttons) => buttons.map((button) => button.dataset.key.split(":").pop()));
    check("picking Codex resets the model and effort and drops the account",
      (await line(page, "#work")) === "Codex | GPT-6.1 Sol | high" && JSON.stringify(codex) === '["provider","model","effort"]',
      `${await line(page, "#work")} ${JSON.stringify(codex)}`);
    check("a button left out leaves no stray text", await page.evaluate(() =>
      [...document.querySelector("#work .start-options").childNodes].every((node) => node.nodeType === Node.ELEMENT_NODE)), "text beside the buttons");
    check("focus stays on the agent button", await page.evaluate(() => document.activeElement.dataset.key === "msg:new-session:provider"),
      "focus moved");
    await choose(page, "#work", "msg:new-session:model", "gpt-6.1-terra");
    await choose(page, "#work", "msg:new-session:effort", "xhigh");
    check("the buttons follow the picks", (await line(page, "#work")) === "Codex | GPT-6.1 Terra | xhigh", await line(page, "#work"));
    check("no sideways scroll at 1440 px", (await wide(page)) <= 0, `${await wide(page)} px`);
    if (shots) await page.screenshot({ path: path.join(shots, "new-session-codex-1440-light.png") });

    await box.locator("button[aria-label='Send']").click();
    await page.waitForFunction(() => !document.querySelector('#work textarea[data-key="msg:new-session"]')?.value, null, { timeout: 5000 })
      .catch(() => {});
    for (let waited = 0; waited < 50 && !posted.some((entry) => entry.path === "/api/messages"); waited += 1) await sleep(100);
    const sent = posted.find((entry) => entry.path === "/api/messages");
    check("Cockpit takes the message", sent && sent.status === 200, JSON.stringify(sent && sent.status));
    const target = sent && sent.body.target;
    check("Send names the agent, model and effort picked",
      target && target.new_cwd && target.provider === "codex" && target.model === "gpt-6.1-terra" && target.effort === "xhigh"
        && target.account === null, JSON.stringify(sent && sent.body));
    check("the picks are cleared once sent", await page.evaluate(() => !ui.starts.has("msg:new-session")),
      "still set");

    // A running session's box has no start line.
    await page.evaluate(() => {
      const running = (ui.state.sessions || []).find((session) => session.alive);
      if (running) { ui.expanded.add(`msg:session:${running.session_id}`); render(false); }
    });
    const hidden = await page.evaluate(() => [...document.querySelectorAll(".composer .start-options")]
      .filter((holder) => !holder.closest(".composer").querySelector('textarea[data-key="msg:new-session"]'))
      .every((holder) => holder.hidden || !holder.offsetParent));
    check("a box that writes to a running session shows no start line", hidden, "a start line shows");

    // Picks set back to the defaults name the agent, so Cockpit starts at the defaults rather than the item's picks from Add.
    await page.evaluate(() => startKeyboardSession());
    await area.fill("Back to the defaults");
    await choose(page, "#work", "msg:new-session:account", "work");
    await choose(page, "#work", "msg:new-session:account", "");
    posted.length = 0;
    await box.locator("button[aria-label='Send']").click();
    for (let waited = 0; waited < 50 && !posted.some((entry) => entry.path === "/api/messages"); waited += 1) await sleep(100);
    const reset = posted.find((entry) => entry.path === "/api/messages");
    const resetTarget = reset && reset.body.target;
    check("picks set back to the defaults send the agent, so the item's picks from Add stay out",
      resetTarget && resetTarget.provider === "claude" && resetTarget.account === null && resetTarget.model === null,
      JSON.stringify(reset && reset.body));

    // The Add dialog.
    await page.locator(".sidebar-add").click();
    const addArea = page.locator('#add-box textarea[data-key="add:new"]');
    await addArea.waitFor();
    check("Add offers the start buttons too", (await line(page, "#add-box")) === "Claude | Opus 5.5 | high | Auto", await line(page, "#add-box"));
    await choose(page, "#add-box", "add:new:model", "claude-sonnet-5-5");
    await choose(page, "#add-box", "add:new:effort", "low");
    await choose(page, "#add-box", "add:new:account", "work");
    const account = "work";
    await page.locator('#add-box .start-button[data-key="add:new:effort"]').click();
    await page.keyboard.press("Escape");
    check("Escape closes an open list in Add and leaves Add open",
      await page.evaluate(() => !document.querySelector("#add-box .start-pick[open]") && ui.adding
        && document.activeElement.dataset.key === "add:new:effort"), "Add closed or the list stayed open");
    await page.locator('#add-box .start-button[data-key="add:new:effort"]').click();
    await addArea.focus();
    await page.keyboard.press("Escape");
    check("Escape in Add's text box closes an open list first, and Add stays open",
      await page.evaluate(() => !document.querySelector("#add-box .start-pick[open]") && ui.adding), "Add closed or the list stayed open");
    if (shots) await page.screenshot({ path: path.join(shots, "add-1440-light.png") });
    await addArea.fill("Start options browser check");
    await addArea.press("Enter");
    await page.locator("#add-box").waitFor({ state: "hidden" });
    // The Add goes out once its Undo is no longer on offer, 6 s after Enter (UNDO_ADD_MS in app.js).
    for (let waited = 0; waited < 120 && !posted.some((entry) => entry.path === "/api/items/add"); waited += 1) await sleep(100);
    const added = posted.find((entry) => entry.path === "/api/items/add");
    check("the item Add makes carries the picks",
      added && added.status === 200 && added.body.model === "claude-sonnet-5-5" && added.body.effort === "low" && added.body.account === account
        && added.body.provider === null, JSON.stringify(added && added.body));

    // A refused Add keeps its picks with its words, also after a reload.
    await page.route("**/api/items/add", (route) => route.fulfill({ status: 400, contentType: "application/json",
      body: JSON.stringify({ detail: "refused for the check" }) }));
    await page.locator(".sidebar-add").click();
    await choose(page, "#add-box", "add:new:model", "claude-sonnet-5-5");
    await choose(page, "#add-box", "add:new:effort", "low");
    await addArea.fill("Refused add");
    await addArea.press("Enter");
    await page.waitForFunction(() => ui.drafts.get("add:new") === "Refused add", null, { timeout: 15000 });
    await page.unroute("**/api/items/add");
    await page.reload();
    await page.waitForFunction(() => typeof ui !== "undefined" && ui.state);
    // The reload's notice of the refusal may cover the button.
    await page.evaluate(() => { if (!ui.adding) openAddBox(); });
    await addArea.waitFor();
    check("a refused Add keeps its picks after a reload", (await line(page, "#add-box")) === "Claude | Sonnet 5.5 | low | Auto",
      await line(page, "#add-box"));
    await page.keyboard.press("Escape");

    // Another tab that saves its drafts keeps this tab's picks.
    await page.evaluate(() => startKeyboardSession());
    await area.fill("Two tabs");
    await choose(page, "#work", "msg:new-session:model", "claude-haiku-5-5");
    const other = await context.newPage();
    await other.goto(`${info.origin}/`);
    await other.waitForFunction(() => typeof ui !== "undefined" && ui.state);
    await other.evaluate(() => { ui.drafts.set("msg:new-session", "Two tabs, more"); saveDrafts(); });
    await page.waitForFunction(() => ui.drafts.get("msg:new-session") === "Two tabs, more");
    await other.evaluate(() => { ui.drafts.set("check:other", "words"); saveDrafts(); });
    await other.close();
    await page.reload();
    await page.waitForFunction(() => typeof ui !== "undefined" && ui.state);
    const kept = await page.evaluate(() => ui.starts.get("msg:new-session"));
    check("another tab's save keeps the picks", kept && kept.model === "claude-haiku-5-5", JSON.stringify(kept));

    // A send in another tab drops the picks in an idle tab, and in a tab where the user is in that box its line shows so.
    await page.evaluate(() => startKeyboardSession());
    await area.focus();
    const tab = async () => {
      const opened = await context.newPage();
      opened.on("pageerror", (error) => errors.push(error.message));
      await opened.goto(`${info.origin}/`);
      await opened.waitForFunction(() => typeof ui !== "undefined" && ui.state);
      return opened;
    };
    const idle = await tab();
    await idle.waitForFunction(() => ui.starts.get("msg:new-session")?.model === "claude-haiku-5-5");
    const sender = await tab();
    await sender.evaluate(() => takeDraft("msg:new-session"));
    await idle.waitForFunction(() => !ui.starts.has("msg:new-session")).catch(() => {});
    check("another tab's send drops the picks in an idle tab", await idle.evaluate(() => !ui.starts.has("msg:new-session")),
      "still there");
    await page.waitForFunction(() => !ui.starts.has("msg:new-session")).catch(() => {});
    const typed = await page.evaluate(() => ({
      line: [...document.querySelectorAll("#work .start-options .start-button")].map((button) => button.textContent.trim()).join(" | "), sends: startPicks("msg:new-session", null).model,
      focused: document.activeElement.dataset.key }));
    check("in the tab where the user types in that box, the line shows what Send would start, and focus stays",
      typed.line === "Claude | Opus 5.5 | high | Auto" && typed.sends === null && typed.focused === "msg:new-session", JSON.stringify(typed));

    // Another tab's switch to Codex takes the Account button away from under the user's focus; focus stays on the line.
    await box.locator('.start-button[data-key="msg:new-session:account"]').focus();
    await sender.evaluate(() => {
      ui.starts.set("msg:new-session", { provider: "codex", model: null, effort: null, account: null });
      saveDrafts();
    });
    await page.waitForFunction(() => !document.querySelector('#work .start-button[data-key="msg:new-session:account"]')).catch(() => {});
    const landed = await page.evaluate(() => ({ inLine: !!document.activeElement.closest("#work .start-options"),
      key: document.activeElement.dataset.key || document.activeElement.className }));
    check("when another tab's picks take away the focused button, focus moves to the line's first button", landed.inLine,
      JSON.stringify(landed));
    await sender.evaluate(() => { ui.starts.delete("msg:new-session"); saveDrafts(); });
    await page.waitForFunction(() => !ui.starts.has("msg:new-session"));

    // An open Add box shows the picks another tab made for it, and Add sends those, also with a start button focused;
    // a copy of a start line (a preview's) does not stop the redraw.
    await idle.evaluate(() => {
      openAddBox();
      document.querySelector("#add-box .start-button").focus();
      document.body.append(document.querySelector("#add-box .start-options").cloneNode(true));
    });
    await sender.evaluate(() => {
      ui.starts.set("add:new", { provider: null, model: "claude-sonnet-5-5", effort: "low", account: null });
      saveDrafts();
    });
    await idle.waitForFunction(() => document.querySelector("#add-box .start-options")?.textContent.includes("Sonnet")).catch(() => {});
    const sends = await idle.evaluate(() => ({ model: startPicks("add:new", null).model,
      focused: document.activeElement.classList.contains("start-button") }));
    check("an open Add box shows another tab's picks, the ones Add sends, with focus kept",
      (await line(idle, "#add-box")) === "Claude | Sonnet 5.5 | low | Auto" && sends.model === "claude-sonnet-5-5" && sends.focused,
      `${await line(idle, "#add-box")} / ${JSON.stringify(sends)}`);
    await sender.evaluate(() => { ui.starts.delete("add:new"); saveDrafts(); });
    await idle.waitForFunction(() => !ui.starts.has("add:new"));

    // A refusal this page receives while Add is open puts the refused picks back, and the open Add box shows them.
    const restored = await idle.evaluate(() => {
      putBack("add:new", "Refused words", null, { provider: "codex", model: "gpt-6.1-terra", effort: "xhigh", account: null });
      return [...document.querySelectorAll("#add-box .start-options .start-button")].map((button) => button.textContent.trim()).join(" | ");
    });
    check("a refusal received while Add is open shows the picks put back", restored === "Codex | GPT-6.1 Terra | xhigh", restored);
    // Its words show in the open box too, and typing there keeps them.
    const addField = idle.locator('#add-box textarea[data-key="add:new"]');
    const shownWords = await addField.inputValue();
    await idle.evaluate(() => window.CockpitEditor ? window.CockpitEditor.end(document.querySelector('#add-box textarea[data-key="add:new"]'))
      : document.querySelector('#add-box textarea[data-key="add:new"]').focus());
    await idle.keyboard.type(" more");
    const typedDraft = await idle.evaluate(() => ui.drafts.get("add:new"));
    check("a refusal received while Add is open shows its words there, and typing keeps them",
      shownWords.includes("Refused words") && typedDraft.includes("Refused words"), JSON.stringify({ shownWords, typedDraft }));
    await addField.fill("");
    await idle.evaluate(() => { ui.starts.delete("add:new"); ui.drafts.delete("add:new"); saveDrafts(); });

    // An open Add box follows a change in what the setup offers: with Codex turned off, its Codex picks show as the
    // defaults Send now uses, and focus stays on the line.
    const followed = await idle.evaluate(() => {
      ui.starts.set("add:new", { provider: "codex", model: "gpt-6.1-terra", effort: "xhigh", account: null });
      drawStartLines();
      const before = [...document.querySelectorAll("#add-box .start-options .start-button")].map((button) => button.textContent.trim()).join(" | ");
      document.querySelector("#add-box .start-button").focus();
      const codex = ui.state.session_options.codex;
      delete ui.state.session_options.codex;
      render(false);
      const after = [...document.querySelectorAll("#add-box .start-options .start-button")].map((button) => button.textContent.trim()).join(" | ");
      const focused = document.activeElement.classList.contains("start-button");
      ui.state.session_options.codex = codex;
      render(false);
      ui.starts.delete("add:new");
      return { before, after, focused };
    });
    check("an open Add box redraws when the setup stops offering its picks, with focus kept",
      followed.before === "Codex | GPT-6.1 Terra | xhigh" && followed.after === "Opus 5.5 | high | Auto" && followed.focused,
      JSON.stringify(followed));

    // Picks put back for an agent no longer enabled count as the defaults: the line shows them, Add sends them, and
    // the refused words still come back.
    const disabled = await idle.evaluate(() => {
      delete ui.state.session_options.codex;
      document.querySelector("#add-box .start-button").focus();
      let thrown = null;
      try {
        putBack("add:new", "Words for a disabled agent", null, { provider: "codex", model: "gpt-6.1-terra", effort: "xhigh", account: null });
      } catch (error) { thrown = error.message; }
      return { thrown, words: ui.drafts.get("add:new"), sends: startPicks("add:new", null),
        line: [...document.querySelectorAll("#add-box .start-options .start-button")].map((button) => button.textContent.trim()).join(" | ") };
    });
    check("picks for an agent no longer enabled count as the defaults, and the refused words still come back",
      disabled.thrown === null && (disabled.words || "").includes("Words for a disabled agent")
        && Object.values(disabled.sends).every((value) => value === null) && disabled.line === "Opus 5.5 | high | Auto",
      JSON.stringify(disabled));
    await idle.evaluate(() => { ui.starts.delete("add:new"); ui.drafts.delete("add:new"); saveDrafts(); });
    await idle.close();
    await sender.close();

    // A send a closed page left, refused on another page, puts its picks back with its words.
    await page.evaluate(() => {
      const key = "msg:new-session";
      localStorage.setItem("cockpit.drafts", "[]");
      localStorage.setItem("cockpit.draft-starts", "[]");
      localStorage.setItem("cockpit.outbox.start-test", JSON.stringify({
        path: "/api/messages", method: "POST", headers: {}, body: "{}", tryMs: 30000,
        words: { store: "cockpit.drafts", targets: "cockpit.draft-targets", starts: "cockpit.draft-starts", key, text: "Left words",
                 choice: null, picks: { provider: "codex", model: "gpt-6.1-terra", effort: "xhigh", account: null } },
        // Older than a day: the page that takes it reports it as not sent without sending it.
        at: Date.now() - 2 * 24 * 3600e3, n: 0, page: "closed-page",
      }));
    });
    await page.goto(`${info.origin}/spec`);
    await page.waitForFunction(() => (localStorage.getItem("cockpit.drafts") || "").includes("Left words"));
    const leftPicks = await page.evaluate(() => new Map(JSON.parse(localStorage.getItem("cockpit.draft-starts") || "[]")).get("msg:new-session"));
    check("a send refused on another page puts its picks back", leftPicks && leftPicks.model === "gpt-6.1-terra"
      && leftPicks.effort === "xhigh", JSON.stringify(leftPicks));
    await context.close();

    // A phone in the dark.
    ({ context, page, posted } = await open({ viewport: { width: 390, height: 844 }, colorScheme: "dark" }));
    await page.evaluate(() => startKeyboardSession());
    await page.locator('#work textarea[data-key="msg:new-session"]').fill("Phone check");
    await page.locator('#work .start-button[data-key="msg:new-session:account"]').click();
    await sleep(200);
    const fits = await page.evaluate(() => [...document.querySelectorAll("#work .start-button, #work .start-pick[open] > .menu-list")]
      .every((each) => each.getBoundingClientRect().left >= 0 && each.getBoundingClientRect().right <= window.innerWidth));
    check("at 390 px the buttons and the last button's open list fit", fits && (await wide(page)) <= 0, `${await wide(page)} px`);
    await page.locator("#work .start-pick[open] > .menu-list").scrollIntoViewIfNeeded();
    if (shots) await page.screenshot({ path: path.join(shots, "new-session-390-dark.png"), fullPage: false });
    await page.locator('#work textarea[data-key="msg:new-session"]').fill("");
    await sleep(200);
    check("a list whose buttons hide closes, so the page's shortcuts work again",
      await page.evaluate(() => !document.querySelector("details.menu[open]") && ![...ui.open].some((each) => each.startsWith("start:"))),
      "a list stayed open");
    await page.evaluate(() => { ui.open.add("start:msg:new-session:effort"); render(); });
    check("an empty box drawn again, as after Send, opens no list left open before",
      await page.evaluate(() => !document.querySelector("details.menu[open]") && !ui.open.has("start:msg:new-session:effort")), "a list opened");
    await page.evaluate(() => openAddBox());
    await page.locator('#add-box .start-button[data-key="add:new:account"]').click();
    await sleep(200);
    const addFits = await page.evaluate(() => [...document.querySelectorAll("#add-box .start-button, #add-box .start-pick[open] > .menu-list")]
      .every((each) => each.getBoundingClientRect().left >= 0 && each.getBoundingClientRect().right <= window.innerWidth));
    check("at 390 px Add's buttons and its open Account list fit", addFits && (await wide(page)) <= 0, `${await wide(page)} px`);
    if (shots) await page.screenshot({ path: path.join(shots, "add-390-dark.png"), fullPage: false });
    await page.setViewportSize({ width: 660, height: 844 });
    await sleep(200);
    const addFitsWider = await page.evaluate(() => {
      const list = document.querySelector("#add-box .start-pick[open] > .menu-list");
      const panel = document.querySelector("#add-box .add-foot").getBoundingClientRect();
      return list && list.getBoundingClientRect().left >= panel.left && list.getBoundingClientRect().right <= panel.right;
    });
    check("at 660 px Add's open Account list stays inside the dialog", addFitsWider && (await wide(page)) <= 0, `${await wide(page)} px`);
    await context.close();

    check("no page errors", errors.length === 0, errors.join(" | "));
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failed = results.filter((result) => !result.ok).length;
  console.error(failed ? `${failed} of ${results.length} checks failed` : `all ${results.length} checks passed`);
  process.exitCode = failed ? 1 : 0;
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
