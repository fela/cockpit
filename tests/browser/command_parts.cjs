// Run with PLAYWRIGHT_MODULE=/path/to/node_modules/playwright node tests/browser/command_parts.cjs. Uses the keyboard
// fixture's isolated database; never touches live work or real agents.  PERM-22: - shell.js splits a command into
// its parts, one per line, and names each word's kind; - at desktop and phone width, in light and dark, a blocked
// Bash call's card shows the command one part per line   with its words coloured, and the text copied from it is the
// command's own; - with a tool rule picked, the call's other commands show one per line, each cut to its first line;
// - the card's heading is Claude's plain one, with the session's own line in its tooltip, and the session is named
// by its item's title (PERM-3
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder, screenshotRedrawn } = require("./work.cjs");
const { spawn, spawnSync } = require("node:child_process");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const { lines } = require("../../src/cockpit/static/shell.js");
const root = path.resolve(__dirname, "../..");

// The parts of a line as "kind:text", the text-kind words left bare.
const shown = (command) => lines(command).map((line) => line.indent + line.tokens.map((token) => (token.kind === "text" ? token.text : `${token.kind}:${token.text}`)).join("|"));

assert.deepEqual(shown("cd ~/dev/app; df -h / | tail -1 && echo \"$T\" > out 2>&1"), [
  "command:cd| ~/dev/app",
  "command:df| |option:-h| / |operator:|",
  "command:tail| |option:-1| |operator:&&",
  "command:echo| |string:\"$T\"| |operator:>| out |operator:2>&1",
]);
assert.deepEqual(shown("{ awk '{print $1}' a; sort b; } | head"), [
  "operator:{",
  "  command:awk| |string:'{print $1}'| a",
  "  command:sort| b",
  "operator:}| |operator:|",
  "command:head",
]);
assert.deepEqual(shown("for f in *.py; do echo $f; done"), [
  "keyword:for| f |keyword:in| *.py",
  "keyword:do| |command:echo| |variable:$f",
  "keyword:done",
]);
assert.deepEqual(shown("cat > x <<'EOF'\na; b | c\nEOF\nwc -l x"), [
  "command:cat| |operator:>| x |operator:<<'EOF'",
  "heredoc:a; b | c\nEOF",
  "command:wc| |option:-l| x",
]);
assert.deepEqual(shown("ls \\\n  -la /tmp # all"), ["command:ls| |operator:\\", "  option:-la| /tmp |comment:# all"]);
assert.deepEqual(shown("find . -name '*.tmp' -exec rm {} \\;"), ["command:find| . |option:-name| |string:'*.tmp'| |option:-exec| rm {} \\;"]);
assert.deepEqual(shown("A=1 B=$(date +%s) make -j4"), ["variable:A=1| |variable:B=|variable:$(date +%s)| |command:make| |option:-j4"]);
// The layout keeps what the command does: these read back as commands that run the same.
const back = (command) => lines(command).map((line) => line.indent + line.tokens.map((token) => token.text).join("")).join("\n");
for (const [command, laid] of [
  ['echo "$(echo "a;b")"', 'echo "$(echo "a;b")"'],
  ["cat <<EOF; echo after\nbody\nEOF", "cat <<EOF; echo after\nbody\nEOF"],
  ["cat <<EOF | cat\nbody\nEOF", "cat <<EOF | cat\nbody\nEOF"],
  ["case x in x) echo yes;; esac", "case x in x) echo yes;;\nesac"],
  ["f() { echo yes; }; f", "f() {\n  echo yes\n}\nf"],
  ["((x=3)); echo $x", "((x=3))\necho $x"],
  ["[[ foo =~ (foo|bar) ]] && echo m", "[[ foo =~ (foo|bar) ]] &&\necho m"],
  ["echo }", "echo }"],
  ["echo { abc", "echo { abc"],
  ['cat <<"END.TAG"\na;b\nEND.TAG', 'cat <<"END.TAG"\na;b\nEND.TAG'],
  ["cat <<\\EOF\na;b\nEOF", "cat <<\\EOF\na;b\nEOF"],
  ["( cd x && ls ); echo done", "(\n  cd x &&\n  ls\n)\necho done"],
  ['echo "unclosed; x', 'echo "unclosed; x'],
  // an arithmetic for header lost its `;`, and `;&` was split from its `&`.
  ['for ((i=0;i<2;i++)); do echo "$i"; done', 'for ((i=0;i<2;i++))\ndo echo "$i"\ndone'],
  ["case a in a) echo 1;& b) echo 2;;& *) echo 3;; esac", "case a in a) echo 1;&\nb) echo 2;;&\n*) echo 3;;\nesac"],
  // Also with a `\` line end between `for` and the header, or no space.
  ['for((i=0;i<2;i++)); do echo "$i"; done', 'for((i=0;i<2;i++))\ndo echo "$i"\ndone'],
  ['for\\\n((i=0;i<2;i++)); do echo "$i"; done', 'for\\\n((i=0;i<2;i++))\ndo echo "$i"\ndone'],
  ['for\\\n  ((i=0;i<2;i++)); do echo "$i"; done', 'for\\\n ((i=0;i<2;i++))\ndo echo "$i"\ndone'],
  // A `\` line end inside a word stays in it: `a\` then `b` is the one word `ab`.
  ["echo a\\\nb", "echo a\\\nb"],
  ['for \\\n  ((i=0;i<2;i++)); do echo "$i"; done', 'for \\\n  ((i=0;i<2;i++))\ndo echo "$i"\ndone'],
]) assert.equal(back(command), laid, command);
console.log("PASS shell.js splits commands into parts and names their words");

const COMMIT = "git commit -q -m \"Tasks export columns\n\nThe body of the message.\"";
// A long other command must not widen the page on a phone.
const PUSH = `git push origin HEAD:refs/heads/${"long-branch-name-".repeat(12)}`;
const COMMAND = `cd ~/dev/app && git add -A src && ${COMMIT} && ${PUSH} 2>&1 | tail -2; echo "$HOME" # done`;
// The session's own line names a spec row, a feedback number and a time; Claude's heading does not (PERM-3).
const DESCRIPTION = "Commit the PERM-22 export change ";
const HEADING = "Commit and push the export columns";
const work = workFolder("cockpit-command-parts-");
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
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "command-parts-test", "content-type": "application/json" };
let browser;

// Ages a refusal past the hold and stores Claude's explanation, as explain_next would have.
function explained(id) {
  const stamp = new Date(Date.now() - 120 * 1000).toISOString().replace(/\.\d+Z$/, "Z");
  const explanation = JSON.stringify({ heading: HEADING, plain: "The session wants to commit its work and push it.", sentence: "", moved_on: "", count: 1 });
  const script = "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); "
    + "c.execute('UPDATE permission_requests SET created_at = ?, last_at = ?, explanation = ? WHERE id = ?', "
    + "(sys.argv[2], sys.argv[2], sys.argv[3], int(sys.argv[4]))); c.commit()";
  const result = spawnSync(python, ["-c", script, path.join(work, "cockpit.db"), stamp, explanation, String(id)]);
  assert.equal(result.status, 0, String(result.stderr));
}

(async () => {
  const runtime = path.join(work, "runtime.json");
  for (let at = 0; !fs.existsSync(runtime); at++) {
    if (at > 1200 || server.exitCode !== null) throw new Error(`Fixture failed: ${serverLog}`);
    await delay(100);
  }
  const { origin, cookie } = JSON.parse(fs.readFileSync(runtime, "utf8"));
  for (let at = 0; ; at++) {
    try { if ((await fetch(origin + "/healthz")).ok) break; } catch {}
    if (at > 600) throw new Error(`Fixture did not listen: ${serverLog}`);
    await delay(100);
  }
  const response = await fetch(origin + "/api/permissions/requests", {
    method: "POST", headers: AGENT,
    body: JSON.stringify({ kind: "denied", session_id: SESSION_A, cwd: work, tool_name: "Bash",
      tool_input: { command: COMMAND, description: DESCRIPTION }, tool_use_id: "", reason: "command parts test" }),
  });
  assert.equal(response.ok, true, await response.clone().text());
  const id = (await response.json()).id;
  explained(id);
  const state = await (await fetch(`${origin}/api/state`, { headers: { Cookie: cookie } })).json();
  const [request] = state.permission_requests;
  assert.ok(request.other_commands.length > 1, JSON.stringify(request.other_commands));
  // The card names the session by its item's short title.
  const item = state.items.find((one) => one.sessions[0] === SESSION_A) || state.items.find((one) => one.sessions.includes(SESSION_A));
  assert.ok(item, "the fixture has no item for the session");

  const executablePath = process.env.BROWSER_EXECUTABLE;
  browser = await chromium.launch({ headless: true, ...(executablePath ? { executablePath } : {}) });
  for (const [width, height] of [[1280, 900], [390, 844]]) {
    for (const scheme of ["light", "dark"]) {
      const context = await browser.newContext({ extraHTTPHeaders: { Cookie: cookie }, viewport: { width, height }, colorScheme: scheme });
      const page = await context.newPage();
      page.setDefaultTimeout(8000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
      await page.goto(origin + "/classic");
      const card = page.locator(`#blocked .blocked-card[data-request="${id}"]`);
      const line = page.locator(`#blocked [data-request="${id}"]`);
      await line.waitFor();
      if (await line.evaluate((el) => el.classList.contains("blocked-line"))) await line.click();
      await card.waitFor();
      assert.equal(await card.locator(".blocked-head .what").textContent(), HEADING);
      assert.equal(await card.locator(".blocked-head .what").getAttribute("data-tip"), DESCRIPTION.trim());
      assert.equal(await card.locator(".blocked-head .session-ref-name").textContent(), item.title);
      const call = card.locator("pre.call");
      // One part per line: the parts are the command's own text, joined by line breaks.
      const text = await call.textContent();
      assert.equal(text.split("\n")[0], "cd ~/dev/app &&");
      assert.equal(text.split("\n")[1], "git add -A src &&");
      assert.equal(text.replace(/\n/g, " ").replace(/\s+/g, " "), COMMAND.replace(/;/, "").replace(/\n/g, " ").replace(/\s+/g, " "));
      assert.equal(await call.locator(".sh-command").first().textContent(), "cd");
      const colour = (selector) => call.locator(selector).first().evaluate((el) => getComputedStyle(el).color);
      assert.notEqual(await colour(".sh-operator"), await colour(".sh-command"), "the joins look like the command words");
      assert.notEqual(await colour(".sh-string"), await colour(".sh-command"), "the quoted text looks like the command words");

      // The other commands of the call, one per line, the commit message cut to its first line.
      if (!(await card.locator(".rule-editor, .seg").count())) await card.getByRole("button", { name: /rule/i }).first().click();
      await card.getByRole("button", { name: "Tool rule" }).click();
      const others = card.locator(".rule-others li");
      await others.first().waitFor();
      const items = await others.allTextContents();
      assert.equal(items.length, request.other_commands.length);
      assert.ok(items.includes("git commit -q -m \"Tasks export columns …"), JSON.stringify(items));
      assert.equal(await others.filter({ hasText: "Tasks export columns" }).getAttribute("data-tip"), COMMIT);
      const oneLine = await others.evaluateAll((els) => els.every((el) => el.getBoundingClientRect().height < 24));
      assert.equal(oneLine, true, "an other command takes more than one line");

      const fits = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
      assert.equal(fits, true, `${width}: the page is wider than the window`);
      await screenshotRedrawn(card, { path: path.join(work, `card-${width}-${scheme}.png`) });
      assert.deepEqual(errors, []);
      console.log(`PASS ${width} ${scheme}: the command shows one part per line in colour, the other commands one per line`);
      await context.close();
    }
  }
  console.log(`Screenshots in ${work}`);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
}).finally(async () => {
  await browser?.close();
  server.kill();
});
