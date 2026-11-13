// Priorities as one line above Search (SPEC UI-65), and the Priorities and inbox view (SPEC UI-65, UI-70) that opens
// in the card's place. Checks, on a synthetic database:
// - in the rail, Priorities sit above Search;
// - with no priorities the line reads "Priorities [empty]";
// - a click opens Priorities and inbox in the card's place: the card is hidden, the Priorities row is marked current,
//   the focus is in the last priority, and the list is a numbered list of rows; Enter starts the next row; Save writes
//   the rows in order and goes back to the card, and the line shows them in that order, with every entry in its hover,
//   each clipped to 60 characters; a Save with only a priorities edit sends no inbox settings;
// - the line stays one row whatever the list's length, and shows the summary the server gives, not the list's start;
// - a row moves by dragging its grip or with the up and down arrows on the grip, and a moved priority keeps its id;
// - the pause key pauses a row in its place and out of the numbering; saved, the line leaves it out and the hover lists
//   it as paused; Resume holds it again; with every priority paused the line reads "[all paused]";
// - an edit kept as plain lines by an older page opens as rows;
// - Discard is hidden until an edit, then shows; it goes back to the card and drops the edit; Escape and Back go back
//   to the card, save nothing but keep the edit, so an orange dot follows the word Priorities, also after a reload,
//   and the view reopens on it; Save then sends it and the dot goes; going back puts the focus on the Priorities row;
// - after a refused save the view opens on the refused words, then on a newer edit kept after them; Discard drops
//   the refused words too, also those a closed page left in this browser;
// - the Inbox heading's key opens the same view on the inbox part, and does the same: Escape keeps the edit and puts
//   the dot on the key, also after a reload; the view reopens on it; Discard drops it and the dot goes; after a
//   refused save it opens on a newer edit, and Discard drops the refused words too;
// - one Save sends both a priorities edit and an inbox edit;
// - G P opens the view;
// - at phone width the box shows without sideways scroll;
// - no page errors.
// Saves a screenshot per width in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/goals_box.cjs
"use strict";

const { execFileSync, spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

// Browser dependencies can be selected through the environment (README.md).
const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-goals-box-");
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
    const headers = { cookie: `${info.name}=${info.value}`, origin: info.origin, "content-type": "application/json" };
    const call = async (method, url, body) => {
      const response = await fetch(`${info.origin}${url}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
      if (!response.ok) throw new Error(`${method} ${url}: HTTP ${response.status} ${await response.text()}`);
      return response.json();
    };
    const goals = async () => (await call("GET", "/api/goals")).map((goal) => goal.text);
    const putGoals = (texts) => call("PUT", "/api/goals", { goals: texts.map((text) => ({ id: null, text, paused: false })) });
    await putGoals([]);

    browser = await chromium.launch({ executablePath: CHROMIUM });
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    await context.setExtraHTTPHeaders({ cookie: headers.cookie });
    const page = await context.newPage();
    page.setDefaultTimeout(45000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const boxText = () => page.locator("#goals .goals-text").innerText();
    const view = page.locator("#settings-view");
    const viewOpen = () => page.evaluate(() => settingsView.open && document.body.classList.contains("viewing-settings"));
    // Waits until the view is open, or closed and empty, and its slide is over; false when it never got there.
    const viewSettled = (open) => page.waitForFunction((open) => settingsView.open === open && sliding === null
      && (open || document.getElementById("settings-view").childElementCount === 0), open).then(() => true, () => false);
    const openGoals = async () => { await page.click("#goals .goals-box"); return viewSettled(true); };
    const save = async () => { await view.locator("button[type=submit]").click(); return viewSettled(false); };
    const discard = async () => { await view.locator(".settings-discard").click(); return viewSettled(false); };
    const back = async () => { await view.locator(".settings-head button").first().click(); return viewSettled(false); };
    const escape = async () => { await page.keyboard.press("Escape"); return viewSettled(false); };
    const discardShown = () => view.locator(".settings-discard").isVisible();
    const rows = view.locator(".goal-entry");
    // The view's priorities as the user sees them: each row's place (or "paused") and words.
    const editor = () => page.$$eval("#settings-view .goal-entry", (items) => items.map((item) => ({
      rank: item.classList.contains("paused") ? "paused" : item.querySelector(".goal-entry-rank").textContent,
      text: item.querySelector(".goal-entry-text").value,
    })));
    const editorTexts = async () => (await editor()).map((row) => row.text);
    // Writes the list as the user would: one row left, its words, then Enter and the next priority's words.
    const writeList = async (lines) => {
      while ((await rows.count()) > 1) await rows.last().locator(".inbox-remove").click();
      await rows.first().locator(".goal-entry-text").fill(lines[0] ?? "");
      for (const line of lines.slice(1)) {
        await page.keyboard.press("End");
        await page.keyboard.press("Enter");
        await page.keyboard.type(line);
      }
    };
    // The saves the page sends, by path.
    const puts = [];
    page.on("request", (request) => { if (request.method() === "PUT") puts.push(new URL(request.url()).pathname); });

    await page.goto(`${info.origin}/`);
    await page.locator("#goals .goals-box").waitFor();
    // The fixture may hold the inactivity question, a modal that takes every click. The page asks
    // it on a one-second timer, so the check waits past a few of its rounds; Return answers it.
    await sleep(3000);
    const idle = page.locator("dialog#idle[open]");
    if (await idle.count()) {
      await idle.locator("button", { hasText: "Return" }).click();
      await page.waitForSelector("dialog#idle:not([open])", { state: "attached" });
    }
    const order = await page.evaluate(() => {
      const rail = document.querySelector(".rail");
      const goals = rail.querySelector("#goals");
      const search = rail.querySelector(".rail-search");
      return Boolean(goals && search && goals.compareDocumentPosition(search) & Node.DOCUMENT_POSITION_FOLLOWING);
    });
    check("in the rail, Priorities sit above Search", order, "Search comes first");
    const emptyLine = (await page.locator("#goals .goals-box").innerText()).replace(/\s+/g, " ").trim();
    check("with no priorities the line reads Priorities [empty]", emptyLine === "Priorities [empty]", emptyLine);

    const opened = await openGoals();
    const emptyEditor = await editor();
    const shownView = await page.evaluate(() => ({
      title: document.querySelector("#settings-view .settings-card .settings-head .settings-title")?.textContent,
      back: document.querySelector("#settings-view .settings-head button")?.textContent.trim(),
      inLast: document.activeElement === [...document.querySelectorAll("#settings-view .goal-entry-text")].at(-1),
    }));
    check("a click opens Priorities and inbox in the card's place, with one empty row to write in and the focus in it",
      opened && JSON.stringify(emptyEditor) === JSON.stringify([{ rank: "1.", text: "" }])
      && shownView.title === "Priorities and inbox" && shownView.back === "Back" && shownView.inLast,
      `${JSON.stringify(emptyEditor)} ${JSON.stringify(shownView)}`);
    const whileOpen = await page.evaluate(() => ({
      current: document.querySelector("#goals .goals-box").getAttribute("aria-current"),
      cardHidden: getComputedStyle(document.getElementById("next")).display === "none",
    }));
    check("while the view is open the Priorities row is marked current and the card is hidden",
      whileOpen.current === "true" && whileOpen.cardHidden, JSON.stringify(whileOpen));
    await writeList(["Service first", "", "  Ship A  ", "Reviews"]);
    const written = await editor();
    check("Enter in a row starts the next one, and the rows are numbered in order",
      JSON.stringify(written.map((row) => row.rank)) === JSON.stringify(["1.", "2.", "3.", "4."]), JSON.stringify(written));
    await page.screenshot({ path: path.join(work, "goals-editor-1280.png") });
    puts.length = 0;
    const savedBack = await save();
    const shown = await waitFor(boxText, (text) => text.includes("Reviews"));
    const saved = await waitFor(goals, (texts) => texts.length === 3);
    const closedView = await page.evaluate(() => ({
      current: document.querySelector("#goals .goals-box").getAttribute("aria-current"),
      cardShown: getComputedStyle(document.getElementById("next")).display !== "none",
    }));
    check("Save writes the rows in order, leaving out empty ones, goes back to the card, and the line shows them",
      savedBack && closedView.current === null && closedView.cardShown
      && JSON.stringify(saved) === JSON.stringify(["Service first", "Ship A", "Reviews"]) && shown === "Service first; Ship A; Reviews",
      `saved ${JSON.stringify(saved)}, shown ${JSON.stringify(shown)}, ${JSON.stringify(closedView)}`);
    check("a Save with only a priorities edit sends no inbox settings",
      puts.includes("/api/goals") && !puts.includes("/api/inbox-settings"), JSON.stringify(puts));
    const measure = () => page.evaluate(() => {
      const box = document.querySelector("#goals .goals-box");
      const text = box.querySelector(".goals-line");
      return { height: Math.round(box.getBoundingClientRect().height), cut: text.scrollWidth > text.clientWidth, title: box.dataset.tip || box.title };
    });
    const fits = await measure();
    check("a list that is short for the row fits it whole", !fits.cut && fits.height <= 32, JSON.stringify(fits));
    const longText = "make weekly reminders easier to find";
    await putGoals([longText]);
    await waitFor(boxText, (text) => text === longText);
    const waiting = await measure();
    check("until its summary is written, a longer priority stays one row, cut off", waiting.cut && waiting.height <= 32, JSON.stringify(waiting));
    writeSummary(path.join(work, "cockpit.db"), longText, "weekly reminders");
    await waitFor(boxText, (text) => text === "weekly reminders");
    const summarised = await measure();
    check("a longer priority shows its summary on the one row, with its own words in the hover",
      (await boxText()) === "weekly reminders" && !summarised.cut && summarised.height <= 32 && summarised.title === `1. ${longText}`,
      JSON.stringify(summarised));
    await page.screenshot({ path: path.join(work, "goals-box-summary-1280.png") });
    await putGoals(["Service first", "Ship A", "Reviews", "A long fourth priority that cannot fit on the line beside the others",
      "A fifth priority, also long enough that the list runs past three lines of the rail"]);
    await waitFor(boxText, (text) => text.includes("fifth"));
    const line = await measure();
    const hover = "1. Service first\n2. Ship A\n3. Reviews\n4. A long fourth priority that cannot fit on the line beside t…\n5. A fifth priority, also long enough that the list runs past …";
    check("a long list with no summary yet stays one row, cut off, with all five entries clipped to 60 characters in its hover",
      line.cut && line.height <= 32 && line.title === hover,
      JSON.stringify(line));
    await putGoals(["Service first", "Ship A", "Reviews"]);
    await waitFor(boxText, (text) => !text.includes("fifth"));
    await page.screenshot({ path: path.join(work, "goals-box-1280.png") });

    // Dragging a row by its grip, and moving it with the arrow keys.
    const before = await call("GET", "/api/goals");
    await openGoals();
    const grip = rows.nth(2).locator(".inbox-grip");
    const from = await grip.boundingBox();
    const to = await rows.first().boundingBox();
    await page.mouse.move(from.x + from.width / 2, from.y + from.height / 2);
    await page.mouse.down();
    await page.mouse.move(from.x + from.width / 2, to.y + 4, { steps: 8 });
    await page.mouse.up();
    const dragged = await editor();
    check("dragging a row by its grip moves it, and the places follow",
      JSON.stringify(dragged) === JSON.stringify([{ rank: "1.", text: "Reviews" }, { rank: "2.", text: "Service first" }, { rank: "3.", text: "Ship A" }]),
      JSON.stringify(dragged));
    await rows.nth(2).locator(".inbox-grip").focus();
    await page.keyboard.press("ArrowUp");
    const keyed = await editorTexts();
    check("the up arrow on a grip moves its row up", JSON.stringify(keyed) === JSON.stringify(["Reviews", "Ship A", "Service first"]), JSON.stringify(keyed));
    await page.keyboard.press("ArrowDown");
    const keyedDown = await editorTexts();
    check("the down arrow on a grip moves its row down", JSON.stringify(keyedDown) === JSON.stringify(["Reviews", "Service first", "Ship A"]), JSON.stringify(keyedDown));
    await page.keyboard.press("Control+Enter");
    const ctrlBack = await viewSettled(false);
    const moved = await waitFor(() => call("GET", "/api/goals"), (list) => list[0]?.text === "Reviews");
    const reviews = before.find((goal) => goal.text === "Reviews");
    check("Ctrl+Enter saves the new order and goes back to the card, and a moved priority keeps its id", moved[0].id === reviews.id && ctrlBack
      && JSON.stringify(moved.map((goal) => goal.text)) === JSON.stringify(["Reviews", "Service first", "Ship A"]), JSON.stringify(moved));
    // A priority reworded and moved in the same edit is still the same goal.
    await openGoals();
    await rows.nth(2).locator(".goal-entry-text").fill("Ship A and B");
    await rows.nth(2).locator(".inbox-grip").focus();
    await page.keyboard.press("ArrowUp");
    await page.keyboard.press("Control+Enter");
    await viewSettled(false);
    const reworded = await waitFor(() => call("GET", "/api/goals"), (list) => list[1]?.text === "Ship A and B");
    check("a priority reworded and moved at once keeps its id",
      JSON.stringify(reworded.map((goal) => goal.id)) === JSON.stringify([moved[0].id, moved[2].id, moved[1].id]), JSON.stringify(reworded));
    const twins = await putGoals(["Ship A", "Ship A"]);
    await waitFor(boxText, (text) => text === "Ship A; Ship A");
    await openGoals();
    await rows.nth(1).locator(".inbox-grip").focus();
    await page.keyboard.press("ArrowUp");
    await page.keyboard.press("Control+Enter");
    await viewSettled(false);
    const swappedTwins = await waitFor(() => call("GET", "/api/goals"), (list) => list[0]?.id === twins[1].id);
    check("two priorities with the same words swapped are saved in their new order",
      JSON.stringify(swappedTwins.map((goal) => goal.id)) === JSON.stringify([twins[1].id, twins[0].id]), JSON.stringify(swappedTwins));
    await putGoals(["Reviews", "Service first", "Ship A"]);
    await waitFor(boxText, (text) => text === "Reviews; Service first; Ship A");

    // Pausing a priority keeps its row in place, takes it out of the numbering and the line, and Resume brings it back.
    await openGoals();
    await rows.nth(1).locator(".goal-entry-pause").click();
    const pausedRows = await editor();
    const pressed = await rows.nth(1).locator(".goal-entry-pause").getAttribute("aria-pressed");
    check("the pause key pauses a row in its place, shows the pause mark and numbers the rest without it",
      JSON.stringify(pausedRows) === JSON.stringify([{ rank: "1.", text: "Reviews" }, { rank: "paused", text: "Service first" }, { rank: "2.", text: "Ship A" }])
      && pressed === "true", `${JSON.stringify(pausedRows)} ${pressed}`);
    await page.screenshot({ path: path.join(work, "goals-editor-paused-1280.png") });
    await save();
    const pausedSaved = await waitFor(() => call("GET", "/api/goals"), (list) => list[1]?.paused_at);
    await waitFor(boxText, (text) => !text.includes("Service"));
    const pausedLine = await measure();
    check("Save pauses it on the server, keeping its place; the line leaves it out and the hover lists it as paused",
      pausedSaved[1].text === "Service first" && pausedSaved[1].paused_at && !pausedSaved[0].paused_at && !pausedSaved[2].paused_at
      && (await boxText()) === "Reviews; Ship A" && pausedLine.title === "1. Reviews\n2. Ship A\nPaused: Service first",
      `${JSON.stringify(pausedSaved)} ${await boxText()} ${JSON.stringify(pausedLine.title)}`);
    await page.screenshot({ path: path.join(work, "goals-box-paused-1280.png") });
    await openGoals();
    const reopenedPaused = await editor();
    await rows.nth(1).locator(".goal-entry-pause").click();
    await save();
    const resumed = await waitFor(() => call("GET", "/api/goals"), (list) => list.every((goal) => !goal.paused_at));
    check("the view reopens with the paused row paused, and Resume holds it again",
      reopenedPaused[1].rank === "paused" && resumed.every((goal) => !goal.paused_at) && resumed[1].id === pausedSaved[1].id,
      `${JSON.stringify(reopenedPaused)} ${JSON.stringify(resumed)}`);
    await waitFor(boxText, (text) => text.includes("Service"));
    await call("PUT", "/api/goals", { goals: ["Reviews", "Service first", "Ship A"].map((text) => ({ id: null, text, paused: true })) });
    const allPaused = await waitFor(boxText, (text) => text === "[all paused]");
    check("with every priority paused the line reads [all paused]", allPaused === "[all paused]", allPaused);

    const savedTexts = ["Reviews", "Service first", "Ship A"];
    await putGoals(savedTexts);
    await waitFor(boxText, (text) => text.includes("Ship A"));
    const marked = () => page.locator("#goals .edit-dot").count();
    const onRow = () => page.evaluate(() => document.activeElement?.matches("#goals .goals-box") ?? false);
    await openGoals();
    const untouched = await discardShown();
    await writeList(["Something else"]);
    const touched = await discardShown();
    check("Discard is hidden until an edit, then shows", !untouched && touched, `${untouched} / ${touched}`);
    const discardedBack = await discard();
    const focusAfterDiscard = await onRow();
    await openGoals();
    const reopenedText = await editorTexts();
    check("Discard goes back to the card, changes nothing, marks nothing, and the view reopens on the saved list",
      discardedBack && focusAfterDiscard && JSON.stringify(await goals()) === JSON.stringify(savedTexts)
      && JSON.stringify(reopenedText) === JSON.stringify(savedTexts) && (await marked()) === 0,
      `${JSON.stringify(await goals())} ${JSON.stringify(reopenedText)} focus on the row: ${focusAfterDiscard}`);
    const edit = ["Reviews", "An unsaved reminder priority"];
    await writeList(edit);
    const escaped = await escape();
    const afterEscape = await goals();
    check("Escape goes back to the card and saves nothing",
      escaped && JSON.stringify(afterEscape) === JSON.stringify(savedTexts), JSON.stringify(afterEscape));
    check("going back puts the focus on the Priorities row", await onRow(), "focus elsewhere");
    const escapedLine = (await page.locator("#goals .goals-box").innerText()).replace(/\s+/g, " ").trim();
    const dotAfterLabel = await page.locator("#goals .goals-label + .edit-dot").count();
    const unsavedHover = await page.locator("#goals .goals-box").evaluate((box) => box.dataset.tip || box.title);
    check("after Escape an orange dot follows the word Priorities, and the hover says the edit is not saved",
      (await marked()) === 1 && dotAfterLabel === 1 && escapedLine.startsWith("Priorities Reviews") && unsavedHover.startsWith("Your edit is not saved yet"),
      `${escapedLine} / ${unsavedHover}`);
    await page.screenshot({ path: path.join(work, "goals-box-unsaved-1280.png") });
    await page.reload();
    await page.locator("#goals .goals-box").waitFor();
    await sleep(500);
    check("after a reload the dot is still there", (await marked()) === 1, "no dot");
    await openGoals();
    const reopenedAfterReload = await editorTexts();
    const note = await view.locator(".goals-draft-note").count();
    await page.screenshot({ path: path.join(work, "goals-editor-unsaved-1280.png") });
    check("after a reload the view reopens on the unsaved edit, with a note that it is not saved, and Discard shows",
      JSON.stringify(reopenedAfterReload) === JSON.stringify(edit) && note === 1 && (await discardShown()), JSON.stringify(reopenedAfterReload));
    await save();
    const savedEdit = await waitFor(goals, (texts) => texts.includes("An unsaved reminder priority"));
    await waitFor(marked, (count) => count === 0);
    check("Save sends the kept edit and the dot goes",
      JSON.stringify(savedEdit) === JSON.stringify(edit) && (await marked()) === 0, JSON.stringify(savedEdit));
    await putGoals(savedTexts);
    await waitFor(boxText, (text) => text.includes("Ship A"));

    // An edit kept in this browser before priorities could pause was plain lines; the view still opens on it.
    await page.evaluate(() => localStorage.setItem("cockpit.draft:goals", JSON.stringify({ text: "Kept as lines\nSecond line", at: "2026-11-12T08:00:00Z" })));
    await page.reload();
    await page.locator("#goals .goals-box").waitFor();
    await sleep(500);
    await openGoals();
    const fromLines = await editorTexts();
    await discard();
    check("an edit kept as plain lines by an older page opens as rows, and Discard drops it",
      JSON.stringify(fromLines) === JSON.stringify(["Kept as lines", "Second line"]) && (await marked()) === 0, JSON.stringify(fromLines));

    // A save the server refuses: a newer edit kept after it wins on reopen, and Discard drops both, also the refused
    // words a closed page left in this browser.
    await page.route("**/api/goals", (route) => route.request().method() === "PUT"
      ? route.fulfill({ status: 400, contentType: "application/json", body: JSON.stringify({ detail: "refused for the test" }) })
      : route.continue());
    await openGoals();
    await writeList(["Refused words"]);
    await save();
    await page.waitForFunction(() => ui.goalsUnsaved?.[0]?.text === "Refused words" && !ui.busy.has("goals:set"));
    await page.unroute("**/api/goals");
    await openGoals();
    const onRefused = await editorTexts();
    const discardOnRefused = await discardShown();
    await writeList(["A newer edit"]);
    const backed = await back();
    await openGoals();
    const onNewer = await editorTexts();
    check("after a refused save, the view opens on the refused words with Discard shown, and after Back on a newer edit kept after them",
      JSON.stringify(onRefused) === JSON.stringify(["Refused words"]) && discardOnRefused && backed
      && JSON.stringify(onNewer) === JSON.stringify(["A newer edit"]),
      `${JSON.stringify(onRefused)} / ${JSON.stringify(onNewer)} / Discard shown: ${discardOnRefused}`);
    await discard();
    await openGoals();
    const afterRefusedDiscard = await editorTexts();
    await escape();
    check("Discard after a refused save drops the refused words too: the view reopens on the saved list",
      JSON.stringify(afterRefusedDiscard) === JSON.stringify(savedTexts) && (await marked()) === 0 && (await page.evaluate(() => ui.goalsUnsaved)) === null,
      JSON.stringify(afterRefusedDiscard));
    await page.evaluate(() => localStorage.setItem("cockpit.unsaved:/api/goals", JSON.stringify({ goals: [{ text: "Left by a closed page", paused: true }] })));
    await page.reload();
    await page.locator("#goals .goals-box").waitFor();
    await sleep(500);
    await openGoals();
    const onLeft = await editor();
    await discard();
    await openGoals();
    const afterLeftDiscard = await editorTexts();
    await escape();
    const leftStored = await page.evaluate(() => JSON.parse(localStorage.getItem("cockpit.unsaved:/api/goals")));
    check("Discard drops refused words a closed page left in this browser: the view reopens on the saved list",
      JSON.stringify(onLeft) === JSON.stringify([{ rank: "paused", text: "Left by a closed page" }])
      && JSON.stringify(afterLeftDiscard) === JSON.stringify(savedTexts) && leftStored === null && (await marked()) === 0,
      `${JSON.stringify(onLeft)} / ${JSON.stringify(afterLeftDiscard)} / ${JSON.stringify(leftStored)}`);

    const inboxKey = page.locator(".sidebar-group.queue > summary .inbox-settings-open");
    const inboxDots = () => inboxKey.locator(".edit-dot").count();
    const sources = view.locator("#inbox-sources");
    const openInbox = async () => { await inboxKey.click(); const open = await viewSettled(true); await sources.waitFor(); return open; };
    const storedInbox = await call("GET", "/api/inbox-settings");
    const inboxOpened = await openInbox();
    const inboxShown = await page.evaluate(() => ({
      focus: document.activeElement?.id,
      current: document.querySelector(".sidebar-group.queue > summary .inbox-settings-open")?.getAttribute("aria-current"),
    }));
    check("Inbox settings: the Inbox heading's key opens the same view on the inbox part, marked current",
      inboxOpened && inboxShown.focus === "inbox-rules-title" && inboxShown.current === "true", JSON.stringify(inboxShown));
    const inboxUntouched = await discardShown();
    const inboxEdit = `${storedInbox.sources.text}\n- An unsaved check of the reminders list.`;
    await sources.fill(inboxEdit);
    const inboxTouched = await discardShown();
    check("Inbox settings: Discard is hidden until an edit, then shows",
      !inboxUntouched && inboxTouched, `${inboxUntouched} / ${inboxTouched}`);
    const inboxEscaped = await escape();
    const inboxAfterEscape = await call("GET", "/api/inbox-settings");
    check("Inbox settings: Escape goes back to the card, saves nothing, puts the orange dot on the Inbox heading's key and the focus on it",
      inboxEscaped && inboxAfterEscape.sources.text === storedInbox.sources.text && (await inboxDots()) === 1
      && await page.evaluate(() => document.activeElement?.matches(".inbox-settings-open") ?? false),
      JSON.stringify({ dots: await inboxDots() }));
    await page.screenshot({ path: path.join(work, "inbox-key-unsaved-1280.png") });
    await page.reload();
    await inboxKey.waitFor();
    await sleep(500);
    check("Inbox settings: after a reload the dot is still there", (await inboxDots()) === 1, "no dot");
    await openInbox();
    const inboxReopened = await sources.inputValue();
    const inboxNote = await view.locator(".inbox-draft-note").count();
    await page.screenshot({ path: path.join(work, "inbox-editor-unsaved-1280.png") });
    check("Inbox settings: after a reload the view reopens on the edit, with a note that it is not saved, and Discard shows",
      inboxReopened === inboxEdit && inboxNote === 1 && (await discardShown()), JSON.stringify(inboxReopened));
    await discard();
    await openInbox();
    const inboxAfterDiscard = await sources.inputValue();
    await escape();
    check("Inbox settings: Discard drops the edit, the dot goes, and the view reopens on the saved settings",
      inboxAfterDiscard === storedInbox.sources.text && (await inboxDots()) === 0
      && (await call("GET", "/api/inbox-settings")).sources.text === storedInbox.sources.text, JSON.stringify(inboxAfterDiscard));

    // The same for a refused save of Inbox settings.
    await page.route("**/api/inbox-settings", (route) => route.request().method() === "PUT"
      ? route.fulfill({ status: 400, contentType: "application/json", body: JSON.stringify({ detail: "refused for the test" }) })
      : route.continue());
    await openInbox();
    await sources.fill("Refused sources");
    await save();
    await page.waitForFunction(() => ui.inboxUnsaved?.sources.text === "Refused sources" && !ui.busy.has("inbox-settings:set"));
    await page.unroute("**/api/inbox-settings");
    await openInbox();
    const inboxOnRefused = await sources.inputValue();
    await sources.fill("Newer sources");
    await escape();
    await openInbox();
    const inboxOnNewer = await sources.inputValue();
    check("Inbox settings: after a refused save, the view opens on the refused words, and then on a newer edit kept after them",
      inboxOnRefused === "Refused sources" && inboxOnNewer === "Newer sources", `${JSON.stringify(inboxOnRefused)} / ${JSON.stringify(inboxOnNewer)}`);
    await discard();
    await openInbox();
    const inboxAfterRefusedDiscard = await sources.inputValue();
    await escape();
    check("Inbox settings: Discard after a refused save drops the refused words too: the view reopens on the saved settings",
      inboxAfterRefusedDiscard === storedInbox.sources.text && (await inboxDots()) === 0 && (await page.evaluate(() => ui.inboxUnsaved)) === null,
      JSON.stringify(inboxAfterRefusedDiscard));
    await page.evaluate((rules) => localStorage.setItem("cockpit.unsaved:/api/inbox-settings",
      JSON.stringify({ rules, sources: "Sources left by a closed page" })), storedInbox.rules.points);
    await page.reload();
    await inboxKey.waitFor();
    await sleep(500);
    await openInbox();
    const inboxOnLeft = await sources.inputValue();
    await discard();
    await openInbox();
    const inboxAfterLeftDiscard = await sources.inputValue();
    await escape();
    const inboxLeftStored = await page.evaluate(() => JSON.parse(localStorage.getItem("cockpit.unsaved:/api/inbox-settings")));
    check("Inbox settings: Discard drops refused words a closed page left in this browser: the view reopens on the saved settings",
      inboxOnLeft === "Sources left by a closed page" && inboxAfterLeftDiscard === storedInbox.sources.text && inboxLeftStored === null
      && (await inboxDots()) === 0,
      `${JSON.stringify(inboxOnLeft)} / ${JSON.stringify(inboxAfterLeftDiscard)} / ${JSON.stringify(inboxLeftStored)}`);

    // One Save sends the priorities and the inbox settings edited together.
    await openGoals();
    await writeList(["Saved with the inbox"]);
    const bothSources = `${storedInbox.sources.text}\n- Saved with the priorities.`;
    await sources.fill(bothSources);
    puts.length = 0;
    const bothBack = await save();
    const bothGoals = await waitFor(goals, (texts) => texts[0] === "Saved with the inbox");
    const bothInbox = await waitFor(() => call("GET", "/api/inbox-settings"), (got) => got.sources.text === bothSources);
    check("one Save sends both a priorities edit and an inbox edit, and goes back to the card",
      bothBack && JSON.stringify(bothGoals) === JSON.stringify(["Saved with the inbox"]) && bothInbox.sources.text === bothSources
      && puts.includes("/api/goals") && puts.includes("/api/inbox-settings") && (await marked()) === 0 && (await inboxDots()) === 0,
      JSON.stringify({ bothGoals, sources: bothInbox.sources.text, puts }));
    await call("PUT", "/api/inbox-settings", { rules: storedInbox.rules.points, sources: storedInbox.sources.text });
    await putGoals(savedTexts);
    await waitFor(boxText, (text) => text.includes("Ship A"));

    await page.locator(".card, main").first().click({ position: { x: 5, y: 5 } }).catch(() => {});
    await page.keyboard.press("g");
    await page.keyboard.press("p");
    const jumped = await viewSettled(true);
    check("G P opens the view", jumped && (await viewOpen()), "not open");
    await escape();

    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`${info.origin}/`);
    await page.locator("#goals .goals-box").waitFor({ state: "attached" });
    // The list of items is the phone's other screen, where the rail shows.
    await page.evaluate(() => document.body.classList.add("phone-list"));
    await sleep(300);
    const phone = await page.evaluate(() => ({
      sideways: document.documentElement.scrollWidth > document.documentElement.clientWidth,
      width: Math.round(document.querySelector("#goals .goals-box").getBoundingClientRect().width),
    }));
    await page.screenshot({ path: path.join(work, "goals-box-390.png") });
    check("at phone width the line shows without sideways scroll", !phone.sideways && phone.width > 200, JSON.stringify(phone));

    await putGoals([]);
    check("no page errors", errors.length === 0, errors.join(" | "));
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  console.error(`goals_box: ${results.filter((result) => result.ok).length}/${results.length} passed; screenshots in ${work}`);
  if (results.some((result) => !result.ok)) process.exitCode = 1;
}

// What history.summarize_next stores once Claude has written the label; the harness runs no Claude.
function writeSummary(database, listed, summary) {
  const script = "import hashlib, sys; import sqlite3; c = sqlite3.connect(sys.argv[1]); "
    + "ref = 'goals:' + hashlib.sha256(sys.argv[2].encode()).hexdigest()[:16]; "
    + "c.execute('INSERT OR REPLACE INTO summaries (ref, at, text, asked_at, summary, made_at) VALUES (?, ?, ?, ?, ?, ?)', (ref, '2026-01-15T10:00:00Z', sys.argv[2], '2026-01-15T10:00:00Z', sys.argv[3], '2026-01-15T10:00:00Z')); c.commit()";
  execFileSync(path.join(__dirname, "..", "..", ".venv", "bin", "python"), ["-c", script, database, listed, summary]);
}

async function waitFor(read, ok) {
  const end = Date.now() + 45000;
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
