// The picker's steps go by what the user sees, and the box stays out of the way (SPEC FEEDBACK-7). Checks, on the
// page at / with a row of three cards the test adds (A, B, C, each a title, a line and a key):
// - the hover mark shows on the part under the pointer, labelled with the place the feedback would name;
// - a click on B's title picks it, and Wider steps out to card B, not to a box that looks the same;
// - Sideways from card B goes to A, then C, then back to B, also when a redraw replaces the cards mid-round;
// - after Sideways and then Wider, Sideways starts a new round among the wider parts;
// - a redraw that reorders rows does not hand the pick to the row now in its place;
// - Sideways from B's title goes to the titles of the cards next to it, and from the Priorities box to nothing in a
//   look-alike column across the page (a stand-in side panel) nor to an unlike part far down its own column;
// - Narrower from a card always lands on a part inside it;
// - the chosen part keeps its mark, and the box stands beside it without covering it or darkening the page;
// - the box shows a copy of the part, with the place and its detail on hover;
// - the box is 720 px wide and its field grows with the words;
// - while picking, Enter on a focused key and Space on a focused row reach none of the page's own handlers and pick
//   the part instead;
// - the feedback sent carries `detail`, how a session finds the part again;
// - no page errors.
// Saves screenshots of the hover label, the docked box and its preview in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what
// it needs) and drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/pick_steps.cjs

"use strict";

const { spawn } = require("node:child_process");
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

// Three cards in a row on their own layer, so no redraw replaces them. The page's content policy drops style
// attributes, so the styles go through the style object.
function addCards() {
  window.heard = [];
  const row = document.createElement("section");
  row.id = "probe-row";
  for (const [name, value] of Object.entries({
    position: "fixed", left: "80px", top: "160px", display: "flex", gap: "24px", padding: "16px", "z-index": "20",
    background: "var(--surface)", "border-radius": "12px",
  })) row.style.setProperty(name, value);
  for (const letter of ["A", "B", "C"]) {
    const card = document.createElement("div");
    card.className = "card";
    card.id = `probe-${letter.toLowerCase()}`;
    for (const [name, value] of Object.entries({ width: "180px", padding: "12px", background: "var(--surface-pop)", "border-radius": "8px" })) {
      card.style.setProperty(name, value);
    }
    const title = document.createElement("h3");
    title.textContent = `Card ${letter}`;
    const line = document.createElement("p");
    line.textContent = `What card ${letter} says`;
    const key = document.createElement("button");
    key.className = "button";
    key.type = "button";
    key.textContent = `Open ${letter}`;
    key.addEventListener("keydown", () => window.heard.push(`key-${letter}`));
    key.addEventListener("click", () => window.heard.push(`click-${letter}`));
    card.append(title, line, key);
    row.append(card);
  }
  // A row that opens on Enter or Space, as a list's rows do.
  const opener = document.createElement("div");
  opener.id = "probe-opener";
  opener.tabIndex = 0;
  opener.className = "row";
  opener.textContent = "A row that opens";
  for (const [name, value] of Object.entries({ position: "fixed", left: "80px", top: "420px", width: "300px", padding: "12px", "z-index": "20", background: "var(--surface-pop)" })) {
    opener.style.setProperty(name, value);
  }
  opener.addEventListener("keydown", () => window.heard.push("row-key"));
  document.body.append(row, opener);
}

async function main() {
  const work = workFolder("cockpit-pick-steps-");
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
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
    const page = await context.newPage();
    page.setDefaultTimeout(10e3);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const sent = [];
    page.on("request", (request) => {
      if (request.method() === "POST" && new URL(request.url()).pathname === "/api/feedback") sent.push(request.postDataJSON());
    });
    await page.goto(`${info.origin}/`, { timeout: 60e3 });
    await page.locator("#sidebar .side-row").first().waitFor({ timeout: 30e3 });
    await page.evaluate(addCards);
    const pickedId = () => page.evaluate(() => ui.pick && (ui.pick.el.id || `${ui.pick.el.localName} in ${ui.pick.el.closest(".card")?.id}`));

    // Hover: the mark and its label.
    await page.evaluate(() => startPicking("feedback"));
    const cardA = await page.locator("#probe-a p").boundingBox();
    await page.mouse.move(cardA.x + 10, cardA.y + cardA.height / 2, { steps: 3 });
    await sleep(200);
    const hover = await page.evaluate(() => {
      const mark = document.querySelector(".pick-mark.hover");
      const el = document.querySelector("#probe-a p");
      const r = el.getBoundingClientRect();
      const m = mark && mark.getBoundingClientRect();
      return {
        shown: Boolean(mark && !mark.hidden),
        label: mark?.querySelector(".pick-mark-label").textContent,
        want: pickPlace(el).target.replace(/^page:/, ""),
        fits: Boolean(m && Math.abs(m.left - r.left) < 3 && Math.abs(m.top - r.top) < 3 && Math.abs(m.width - r.width) < 3),
      };
    });
    check("the hover mark shows on the part under the pointer, labelled with its place",
      hover.shown && hover.fits && hover.label === hover.want, JSON.stringify(hover));
    await page.screenshot({ path: path.join(work, "hover-label.png"), clip: { x: 40, y: 100, width: 760, height: 300 } });

    // Click B's title, then Wider.
    const titleB = await page.locator("#probe-b h3").boundingBox();
    await page.mouse.click(titleB.x + 10, titleB.y + titleB.height / 2);
    await sleep(300);
    check("a click on B's title picks the title", (await pickedId()) === "h3 in probe-b", await pickedId());
    const titleDetail = await page.evaluate(() => ui.pick.detail);
    check("the title's detail names no heading over it but its own words", !titleDetail.includes("heading"), titleDetail);
    await page.locator("#pick-box button[data-tip=Wider]").click();
    await sleep(200);
    check("Wider from the title steps out to card B", (await pickedId()) === "probe-b", await pickedId());

    // Sideways, three times.
    const way = [];
    for (let i = 0; i < 3; i++) {
      await page.locator("#pick-box button[data-tip=Sideways]").click();
      await sleep(200);
      way.push(await pickedId());
    }
    check("Sideways from card B goes to A, then C, then back to B",
      JSON.stringify(way) === JSON.stringify(["probe-a", "probe-c", "probe-b"]), JSON.stringify(way));

    // The same round with a redraw after the first press: the page puts new cards in the old ones' places.
    const redrawn = [];
    for (let i = 0; i < 3; i++) {
      await page.locator("#pick-box button[data-tip=Sideways]").click();
      await sleep(200);
      if (i === 0) await page.evaluate(() => { for (const card of document.querySelectorAll("#probe-row > .card")) card.replaceWith(card.cloneNode(true)); });
      redrawn.push(await pickedId());
    }
    const onPage = await page.evaluate(() => document.contains(ui.pick.el));
    check("Sideways comes back to card B when a redraw replaced the cards mid-round",
      JSON.stringify(redrawn) === JSON.stringify(["probe-a", "probe-c", "probe-b"]) && onPage, JSON.stringify({ redrawn, onPage }));

    // Cousins: the same part in the cards next to it, never a look-alike across the page.
    const cousins = await page.evaluate(() => {
      const named = (part) => `${part.localName} in ${part.closest(".card, aside, nav")?.id || part.closest(".card, aside, nav")?.className}`;
      const title = Pick.siblings(document.querySelector("#probe-b h3"), PICKABLE, FEEDBACK_CONTROLS).map(named);
      // A side panel of lines in a column of its own, right of the main column and as tall as the window.
      const layout = document.querySelector(".layout");
      const columns = layout.style.getPropertyValue("grid-template-columns");
      layout.style.setProperty("grid-template-columns", "var(--rail-w) minmax(0, 1fr) 300px");
      const panel = document.createElement("aside");
      panel.id = "probe-info";
      for (const [name, value] of Object.entries({ position: "sticky", top: "0", height: "100dvh" })) panel.style.setProperty(name, value);
      for (const words of ["First line", "Second line", "Third line"]) {
        const line = document.createElement("button");
        line.type = "button";
        line.className = "probe-line";
        line.textContent = words;
        for (const [name, value] of Object.entries({ display: "block", width: "240px", height: "32px" })) line.style.setProperty(name, value);
        panel.append(line);
      }
      document.querySelector(".layout > .primary").after(panel);
      const panelBox = panel.getBoundingClientRect();
      const primaryBox = document.querySelector(".layout > .primary").getBoundingClientRect();
      const beside = panelBox.left >= primaryBox.right && panelBox.top === primaryBox.top && panelBox.width === 300;
      // A part far down the same column, unlike the box in tag and class, as the column's foot is.
      const goalsBox = document.querySelector("button.goals-box");
      const foot = document.createElement("p");
      foot.id = "probe-foot";
      foot.textContent = "Foot of the column";
      foot.style.setProperty("margin-top", "300px");
      Pick.wider(goalsBox, PICKABLE, FEEDBACK_CONTROLS).append(foot);
      const goals = Pick.siblings(goalsBox, PICKABLE, FEEDBACK_CONTROLS).map((part) => part.id === "probe-foot" ? "probe-foot" : named(part));
      panel.remove();
      foot.remove();
      layout.style.setProperty("grid-template-columns", columns);
      return { title, goals, beside };
    });
    check("the probe's side panel stands in its own column right of the main column", cousins.beside);
    check("Sideways from B's title goes to the titles of cards A and C",
      cousins.title.includes("h3 in probe-a") && cousins.title.includes("h3 in probe-c"), JSON.stringify(cousins.title));
    check("Sideways from the Priorities box skips the look-alike lines across the page",
      cousins.goals.length > 0 && !cousins.goals.some((name) => name.includes("probe-info")), JSON.stringify(cousins.goals));
    check("Sideways from the Priorities box skips a part far down its column that looks unlike it",
      !cousins.goals.includes("probe-foot"), JSON.stringify(cousins.goals));

    // Sideways, Wider, Sideways: the second Sideways goes among the cards, not back among the titles.
    const turned = await page.evaluate(() => {
      const named = (part) => part && (part.id || `${part.localName} in ${part.closest(".card")?.id}`);
      const title = document.querySelector("#probe-b h3");
      let pick = { el: title, from: title, side: null };
      const way = [];
      for (const name of ["sideways", "wider", "sideways"]) {
        const next = Pick.step(name, pick, PICKABLE, FEEDBACK_CONTROLS);
        way.push(named(next?.part));
        if (!next) break;
        pick = { el: next.part, from: next.from, side: next.side };
      }
      return way;
    });
    check("after Sideways and Wider, Sideways goes to the next card",
      JSON.stringify(turned) === JSON.stringify(["h3 in probe-a", "probe-a", "probe-b"]), JSON.stringify(turned));

    // Rows built as Usage's table is: the session on the name's button, a cost cell beside it with the share on hover.
    // A redraw in the same order finds the picked name and cell again, even with new words on hover; one in a new
    // order hands neither to the row now in its place.
    const rows = await page.evaluate(() => {
      const table = document.createElement("table");
      table.id = "probe-table";
      const draw = (order, share) => table.replaceChildren(...order.map((name) => {
        const row = document.createElement("tr");
        const button = document.createElement("button");
        button.dataset.focus = name;
        button.innerHTML = `<span>Session ${name}</span>`;
        const nameCell = document.createElement("td");
        nameCell.append(button);
        const cost = document.createElement("td");
        cost.dataset.tip = `${share}% of the week`;
        cost.textContent = "$3";
        row.append(nameCell, cost);
        return row;
      }));
      document.body.append(table);
      const picked = () => [table.rows[1].querySelector("span"), table.rows[1].cells[1]];
      const said = (part) => part && part.closest("tr").querySelector("button").dataset.focus;
      draw(["x", "y", "z"], 10);
      let [span, cost] = picked();
      Pick.now(span);
      Pick.now(cost);
      draw(["x", "y", "z"], 12);
      const same = [said(Pick.now(span)), said(Pick.now(cost))];
      [span, cost] = picked();
      Pick.now(span);
      Pick.now(cost);
      draw(["y", "x", "z"], 12);
      const moved = [said(Pick.now(span)) || null, said(Pick.now(cost)) || null];
      table.remove();
      return { same, moved };
    });
    check("a redraw in the same order finds the row's name and cell again, one in a new order hands them to no other row",
      JSON.stringify(rows) === JSON.stringify({ same: ["y", "y"], moved: [null, null] }), JSON.stringify(rows));

    // Narrower from each card lands inside it.
    const inside = await page.evaluate(() => ["a", "b", "c"].map((letter) => {
      const card = document.getElementById(`probe-${letter}`);
      const part = Pick.narrower(card, card, PICKABLE, FEEDBACK_CONTROLS);
      return Boolean(part && part !== card && card.contains(part));
    }));
    check("Narrower from a card the user picked lands on a part inside it", inside.every(Boolean), JSON.stringify(inside));
    const disabled = await page.locator("#pick-box button[data-tip=Narrower]").isDisabled();
    check("the Narrower key works on a picked card", !disabled, "Narrower is disabled");

    // The box beside the part, the page not darkened.
    const placed = await page.evaluate(() => {
      const box = document.getElementById("pick-box").getBoundingClientRect();
      const card = document.getElementById("probe-b").getBoundingClientRect();
      const mark = document.querySelector(".pick-mark.chosen");
      const m = mark && mark.getBoundingClientRect();
      const overlaps = !(box.right <= card.left || box.left >= card.right || box.bottom <= card.top || box.top >= card.bottom);
      // Anything over the whole window, as a dark veil would be.
      const veil = [...document.querySelectorAll("body *, .pick-mark")].some((el) => {
        const r = el.getBoundingClientRect();
        const look = getComputedStyle(el);
        return look.position === "fixed" && r.width >= innerWidth - 2 && r.height >= innerHeight - 2 && look.visibility !== "hidden"
          && look.display !== "none" && look.backgroundColor !== "rgba(0, 0, 0, 0)";
      });
      return {
        docked: document.getElementById("pick-box").classList.contains("docked"), overlaps, veil,
        marked: Boolean(m && !mark.hidden && Math.abs(m.left - card.left) < 3 && Math.abs(m.width - card.width) < 3),
      };
    });
    check("the chosen card keeps its mark", placed.marked, JSON.stringify(placed));
    check("the box stands beside the card without covering it, and nothing darkens the page",
      placed.docked && !placed.overlaps && !placed.veil, JSON.stringify(placed));

    // The preview: a copy of the card, and everything sent on hover.
    const preview = await page.evaluate(() => {
      const frame = document.querySelector("#pick-box .pick-preview");
      return { copy: Boolean(frame?.querySelector(".pick-copy h3")), title: frame?.dataset.tip || "", detail: ui.pick.detail };
    });
    check("the box shows a copy of the card, with its place and detail on hover",
      preview.copy && preview.title.includes("#probe-b") && preview.title.includes(preview.detail), JSON.stringify(preview));
    check("the card's detail names it by its own heading", preview.detail.includes('with the heading "Card B"'), preview.detail);
    await page.screenshot({ path: path.join(work, "docked-box.png") });
    await page.locator("#pick-box").screenshot({ path: path.join(work, "preview.png") });

    // The box is at least as big as the Add box, and its field grows with the words.
    const sized = async () => page.evaluate(() => {
      const r = document.querySelector("#pick-box textarea").getBoundingClientRect();
      return { box: Math.round(document.getElementById("pick-box").getBoundingClientRect().width), width: Math.round(r.width), height: Math.round(r.height) };
    });
    const empty = await sized();
    await page.locator("#pick-box textarea").fill(Array.from({ length: 14 }, (_, i) => `Line ${i + 1} of a long note`).join("\n"));
    await sleep(300);
    const full = await sized();
    check("the feedback box is 720 px wide, opens at least 160 px tall and grows with the words",
      empty.box === 720 && empty.height >= 160 && full.height > empty.height + 40, JSON.stringify({ empty, full }));

    // Send: the detail goes with the feedback.
    await page.locator("#pick-box textarea").fill("This card reads wrong");
    await page.locator("#pick-box .actions .send").click();
    await sleep(800);
    const body = sent.at(-1);
    check("the feedback sent carries the detail that finds the part",
      Boolean(body && body.detail.includes("#probe-b") && body.detail === preview.detail), JSON.stringify(body));

    // A press on one of the page's keys while picking picks the key and reaches none of its handlers.
    await page.evaluate(() => {
      if (ui.pick) closePick();
      window.heard = [];
      const key = document.querySelector("#probe-a button");
      for (const name of ["pointerdown", "mousedown", "pointerup", "mouseup", "pointerover", "mouseenter"]) {
        key.addEventListener(name, () => window.heard.push(name));
      }
      startPicking("feedback");
    });
    const keyA = await page.locator("#probe-a button").boundingBox();
    await page.mouse.move(keyA.x + keyA.width / 2, keyA.y + keyA.height / 2, { steps: 4 });
    await page.mouse.click(keyA.x + keyA.width / 2, keyA.y + keyA.height / 2);
    await sleep(300);
    const pressed = await page.evaluate(() => ({ heard: window.heard, picked: ui.pick?.el.textContent }));
    check("a click on a page's key while picking picks it and reaches none of its handlers",
      pressed.heard.length === 0 && pressed.picked === "Open A", JSON.stringify(pressed));

    // Keys while picking.
    await page.evaluate(() => { if (ui.pick) closePick(); if (ui.picking) stopPicking(); window.heard = []; });
    await page.locator("#probe-c button").focus();
    await page.evaluate(() => startPicking("feedback"));
    await page.keyboard.press("Enter");
    await sleep(300);
    const onKey = await page.evaluate(() => ({ heard: window.heard, picked: ui.guess?.parts[0]?.textContent || ui.pick?.el.textContent }));
    check("Enter on a focused key, while picking, reaches none of its handlers and picks it",
      onKey.heard.length === 0 && onKey.picked === "Open C", JSON.stringify(onKey));
    await page.evaluate(() => { if (ui.pick) closePick(); if (ui.picking) stopPicking(); window.heard = []; });
    await page.locator("#probe-opener").focus();
    await page.evaluate(() => startPicking("feedback"));
    await page.keyboard.press(" ");
    await sleep(300);
    const onRow = await page.evaluate(() => ({ heard: window.heard, picked: ui.pick?.el.id }));
    check("Space on a focused row, while picking, reaches none of its handlers and picks it",
      onRow.heard.length === 0 && onRow.picked === "probe-opener", JSON.stringify(onRow));

    check("no page errors", errors.length === 0, errors.join(" | "));
    await context.close();
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failed = results.filter((result) => !result.ok);
  console.error(`${results.length - failed.length}/${results.length} checks pass; screenshots in ${work}`);
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
