// Run with PLAYWRIGHT_MODULE=/path/to/node_modules/playwright node tests/browser/tell_box_size.cjs. Uses the keyboard
// fixture's isolated database; never touches live work or real agents. PERM-13: a blocked call's "Ask for it
// yourself" box opens showing its whole message, and a height the user drags its corner to stays through the polls
// that draw the card again, at desktop and phone width, in Next and in the classic view.
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");
const { spawn, spawnSync } = require("node:child_process");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(__dirname, "../..");
const work = workFolder("cockpit-tell-size-");
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
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "tell-size-test", "content-type": "application/json" };
// A long call, as a blocked remote edit is: its message wraps over many lines.
const LONG = "ssh -o BatchMode=yes tasks@203.0.113.7 \"docker exec tasks-app bash -c 'cd /srv/tasks/lists && cp shared.md /srv/tasks/shared.md.bak && python3 - <<EOF\n"
  + "p=\\\"shared.md\\\"; s=open(p).read()\nold=\\\"Reminders go out at nine in the morning for every list shared with the family, with the due date first.\\\"\n"
  + "new=\\\"Reminders go out at nine; each person picks their own time in settings.\\\"\nassert s.count(old)==1; open(p,\\\"w\\\").write(s.replace(old,new))\nEOF\nhead -c 400 shared.md'\"";
let browser;

// Ages a refusal past the hold (permissions.HOLD_SECONDS) and stores Claude's explanation, as explain_next would have.
function explained(id) {
  const stamp = new Date(Date.now() - 120_000).toISOString().replace(/\.\d+Z$/, "Z");
  const explanation = JSON.stringify({ plain: "The session wants to change a shared list's notes.", sentence: "", moved_on: "", count: 1 });
  const script = "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); "
    + "c.execute('UPDATE permission_requests SET created_at = ?, last_at = ?, explanation = ? WHERE id = ?', "
    + "(sys.argv[2], sys.argv[2], sys.argv[3], int(sys.argv[4]))); c.commit()";
  const result = spawnSync(python, ["-c", script, path.join(work, "cockpit.db"), stamp, explanation, String(id)]);
  assert.equal(result.status, 0, String(result.stderr));
}

(async () => {
  const runtime = path.join(work, "runtime.json");
  for (let at = 0; !fs.existsSync(runtime); at++) {
    if (at > 3000 || server.exitCode !== null) throw new Error(`Fixture failed: ${serverLog}`);
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
        tool_use_id: "", reason: "tell size test" }),
    });
    assert.equal(response.ok, true, await response.clone().text());
    const { id } = await response.json();
    explained(id);
    return id;
  };
  const id = await block(LONG);

  const executablePath = process.env.BROWSER_EXECUTABLE;
  browser = await chromium.launch({ headless: true, ...(executablePath ? { executablePath } : {}) });
  for (const view of ["/", "/classic"]) {
    for (const [width, height] of [[1280, 900], [390, 844]]) {
      const label = `${view} ${width}`;
      const context = await browser.newContext({ extraHTTPHeaders: { Cookie: cookie }, viewport: { width, height } });
      const page = await context.newPage();
      page.setDefaultTimeout(30000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(origin + view, { timeout: 60000 });
      const box = page.locator(`:is(#blocked, #next) .blocked-card[data-request="${id}"] .tell textarea`).first();
      // A poll may be drawing the card again just now; the box is back within a moment.
      const settled = async (act) => {
        for (let at = 0; ; at++) {
          try { return await act(); } catch (error) { console.error(`retry ${at}: ${error.message.split("\n")[0]}`); if (at > 20) throw error; await delay(200); }
        }
      };
      await box.waitFor();
      await settled(() => box.scrollIntoViewIfNeeded({ timeout: 10000 }));
      // The box opens showing its whole message, or as much as half the window holds.
      const opened = await settled(() => box.evaluate((el) => ({ client: el.clientHeight, scroll: el.scrollHeight, outer: el.offsetHeight, half: innerHeight / 2 }), null, { timeout: 10000 }));
      assert.ok(opened.scroll <= opened.client + 1 || Math.abs(opened.outer - opened.half) < 2,
        `${label}: the message fits the box as it opens (${JSON.stringify(opened)})`);
      await page.screenshot({ path: path.join(work, `tell-open-${width}${view === "/" ? "" : "-classic"}.png`) });

      // Drags the box's corner down by 200 pixels, again when a redraw cut the drag short. A cut drag can leave the
      // corner below the window, so each try brings the box into view first.
      let corner;
      let dragged;
      await settled(async () => {
        await box.scrollIntoViewIfNeeded({ timeout: 10000 });
        corner = await box.boundingBox({ timeout: 10000 });
        await page.mouse.move(corner.x + corner.width - 3, corner.y + corner.height - 3);
        await page.mouse.down();
        await page.mouse.move(corner.x + corner.width - 3, corner.y + corner.height + 197, { steps: 8 });
        await page.mouse.up();
        dragged = await box.evaluate((el) => el.getBoundingClientRect().height, null, { timeout: 10000 });
        assert.ok(dragged > corner.height + 150, `${label}: the drag made the box taller (${corner.height} to ${dragged})`);
      });
      // The page draws its sections again as a poll with new state does. The box is a new element after it, and
      // a focused box holds its section through polls, so the box lets go of focus as the user moves on.
      await page.evaluate(() => { document.activeElement?.blur(); render(false); });
      const after = await box.evaluate((el) => el.getBoundingClientRect().height);
      assert.ok(Math.abs(after - dragged) < 2, `${label}: the dragged height stays after a redraw (${dragged} then ${after})`);
      await page.screenshot({ path: path.join(work, `tell-dragged-${width}${view === "/" ? "" : "-classic"}.png`) });
      assert.deepEqual(errors, [], `${label}: no page errors`);
      await context.close();
    }
  }
  console.log(`tell box size: ok (screenshots in ${work})`);
})().catch((error) => {
  console.error(error);
  console.error(serverLog.slice(-2000));
  process.exitCode = 1;
}).finally(async () => {
  if (browser) await browser.close();
  server.kill();
});
