// Run with PLAYWRIGHT_MODULE=/path/to/node_modules/playwright node tests/browser/allow_note.cjs. Uses the keyboard
// fixture's isolated database; never touches live work or real agents.  PERM-7: a blocked call's card takes a short
// note, at desktop and phone width, and Add rule sends it with the retry as the user's own message; a card with no
// note sends Cockpit's retry alone. The rule editor opens on Sentence, and Send as you is the card's first answer
// (PERM-13).
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");
const { spawn, spawnSync } = require("node:child_process");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(__dirname, "../..");
const work = workFolder("cockpit-note-");
const python = path.join(root, ".venv/bin/python");
const server = spawn(python, [path.join(__dirname, "serve_keyboard.py"), work], {
  cwd: root, stdio: ["ignore", "pipe", "pipe"],
});
let serverLog = "";
server.stdout.on("data", (data) => { serverLog += data; });
server.stderr.on("data", (data) => { serverLog += data; });
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
// The fixture's session (tests/conftest.py) and agent token.
const SESSION_A = "11111111-1111-4111-8111-111111111111";
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "note-test", "content-type": "application/json" };
let browser;

// Ages a refusal past the hold (permissions.HOLD_SECONDS) and stores Claude's explanation, as explain_next would have.
function explained(id) {
  const stamp = new Date(Date.now() - 120_000).toISOString().replace(/\.\d+Z$/, "Z");
  const explanation = JSON.stringify({ plain: "The session wants to print a word.", sentence: "", moved_on: "", count: 1 });
  const script = "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); "
    + "c.execute('UPDATE permission_requests SET created_at = ?, last_at = ?, explanation = ? WHERE id = ?', "
    + "(sys.argv[2], sys.argv[2], sys.argv[3], int(sys.argv[4]))); c.commit()";
  const result = spawnSync(python, ["-c", script, path.join(work, "cockpit.db"), stamp, explanation, String(id)]);
  assert.equal(result.status, 0, String(result.stderr));
}

(async () => {
  const runtime = path.join(work, "runtime.json");
  for (let at = 0; !fs.existsSync(runtime); at++) {
    if (at > 100 || server.exitCode !== null) throw new Error(`Fixture failed: ${serverLog}`);
    await delay(100);
  }
  const { origin, cookie } = JSON.parse(fs.readFileSync(runtime, "utf8"));
  for (let at = 0; ; at++) {
    try { if ((await fetch(origin + "/healthz")).ok) break; } catch {}
    if (at > 50) throw new Error(`Fixture did not listen: ${serverLog}`);
    await delay(100);
  }
  const block = async (command) => {
    const response = await fetch(origin + "/api/permissions/requests", {
      method: "POST", headers: AGENT,
      body: JSON.stringify({ kind: "denied", session_id: SESSION_A, cwd: work, tool_name: "Bash", tool_input: { command },
        tool_use_id: "", reason: "note test" }),
    });
    assert.equal(response.ok, true, await response.clone().text());
    const { id } = await response.json();
    explained(id);
    return id;
  };
  const user = { Cookie: cookie, Origin: origin };
  const sent = async (requestId) => {
    for (let at = 0; at < 50; at++) {
      const request = await (await fetch(`${origin}/api/permissions/requests/${requestId}`, { headers: user })).json();
      if (request.message_id) return (await fetch(`${origin}/api/messages/${request.message_id}`, { headers: user })).json();
      await delay(100);
    }
    throw new Error(`request ${requestId} sent no message`);
  };

  const executablePath = process.env.BROWSER_EXECUTABLE;
  browser = await chromium.launch({ headless: true, ...(executablePath ? { executablePath } : {}) });
  for (const [width, height] of [[1280, 900], [390, 844]]) {
    const told = await block(`echo told-${width}`);
    const noted = await block(`echo note-${width}`);
    const plain = await block(`echo plain-${width}`);
    const context = await browser.newContext({ extraHTTPHeaders: { Cookie: cookie }, viewport: { width, height } });
    const page = await context.newPage();
    page.setDefaultTimeout(8000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(origin + "/classic");
    // The top request shows as a card, the next ones as a line that opens it, the rest behind "+N more".
    const open = async (id) => {
      const shown = page.locator(`#blocked [data-request="${id}"]`);
      await page.locator("#blocked .blocked-card").first().waitFor();
      if (!(await shown.count())) await page.locator("#blocked").getByText(/^\+\d+ more$/).click();
      if (await shown.evaluate((el) => el.classList.contains("blocked-line"))) await shown.click();
      return page.locator(`#blocked .blocked-card[data-request="${id}"]`);
    };
    // Adds a sentence for the card's call and tells the session to retry.
    const addRule = async (shown, sentence) => {
      await shown.getByRole("button", { name: "Add rule…" }).click();
      const kind = shown.getByRole("group", { name: "Kind of rule" });
      assert.equal(await kind.getByRole("button", { name: "Sentence" }).getAttribute("aria-pressed"), "true", `${width}: the editor opens on Sentence`);
      if (!(await shown.getByRole("textbox", { name: "Your sentence" }).count())) await shown.getByRole("button", { name: "Write my own" }).click();
      await shown.getByRole("textbox", { name: "Your sentence" }).fill(sentence);
      await shown.getByRole("button", { name: "Add rule and retry" }).click();
    };
    const card = await open(noted);
    await card.getByRole("button", { name: "Send as you" }).waitFor();
    await card.getByRole("button", { name: "Add note" }).click();
    const box = card.getByRole("textbox", { name: "Note for the session" });
    await box.waitFor();
    assert.equal(await box.evaluate((el) => el === document.activeElement), true, "the note box takes focus");
    const fits = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
    assert.equal(fits, true, `${width}: the card fits the page`);
    await page.screenshot({ path: path.join(work, `note-${width}.png`) });
    await box.fill("Use task title and due date as the CSV headings");
    await addRule(card, `Sessions may print the word note-${width} with echo`);
    const message = await sent(noted);
    assert.equal(message.author, "user");
    assert.match(message.text, /echo note-\d+\n\nSam's note with this answer: Use task title and due date as the CSV headings$/);
    const other = await open(plain);
    assert.equal(await other.getByRole("textbox", { name: "Note for the session" }).count(), 0, "another card's note stays closed");
    await addRule(other, `Sessions may print the word plain-${width} with echo`);
    const retry = await sent(plain);
    assert.equal(retry.author, "cockpit");
    assert.doesNotMatch(retry.text, /note with this answer/);
    // Send as you takes a note written beside it at the message's end.
    const tellCard = await open(told);
    await tellCard.getByRole("button", { name: "Add note" }).click();
    await tellCard.getByRole("textbox", { name: "Note for the session" }).fill("Then stop");
    await tellCard.getByRole("button", { name: "Send as you" }).click();
    const own = await sent(told);
    assert.equal(own.author, "user");
    assert.match(own.text, /^I want this done: go ahead with this call[\s\S]*echo told-\d+\n```\n\nThen stop$/);
    assert.deepEqual(errors, []);
    console.log(`PASS ${width}: a note on Add rule or Send as you goes to the session as the user's words; none goes without one`);
    await context.close();
  }
  console.log(`Screenshots in ${work}`);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
}).finally(async () => {
  await browser?.close();
  server.kill();
});
