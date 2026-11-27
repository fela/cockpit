// The hub part's script (hub-foot.js) loads before the main page starts, and supplies only that page's hooks. The hub's part of the
// Usage page runs in a browser in hub_page.cjs, usage_hub_limits.cjs and usage_hub_left.cjs.
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const folder = path.resolve(__dirname, "../../src/cockpit/static");

function page(file, supplied) {
  const context = { ...supplied };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(folder, file), "utf8"), context);
  return context;
}

const fills = [];
const notices = [];
let hasLane = false;
const main = page("hub-foot.js", {
  document: { querySelector: () => hasLane },
  h: (tag, props, ...children) => ({ tag, props, children }),
  fill: (...args) => fills.push(args),
  letterMark: (letter) => letter,
  codexMark: () => "Codex",
  claudeMark: () => "Claude",
  machineLamp: () => null,
  ui: { busy: new Set() },
  toast: (text) => notices.push(text),
});
assert.equal(main.cockpitPart.renderUsage({ usage: {} }), false);
const hub = { accounts: [], sign_in: { claude: {}, codex: { finished_at: null } } };
assert.equal(main.cockpitPart.renderUsage({ hub, usage: {} }), true);
assert.equal(fills.length, 1);
hasLane = true;
assert.equal(main.cockpitPart.renderUsage({ hub: { accounts: null }, usage: {} }), true);
assert.equal(fills.length, 1, "a pending first read replaces the existing limits");
assert.equal(main.cockpitPart.usageKey({ hub }), hub);
const board = (value) => JSON.stringify(main.cockpitPart.boardState(value));
assert.equal(board({ hub }), board({ hub: { ...hub, read_at: "later" } }), "usage alone redraws the board");
assert.notEqual(board({ hub }), board({ hub: { ...hub, error: "unreachable" } }));
assert.equal(main.cockpitPart.warnings({ hub }).length, 2);
hub.sign_in.codex.waiting = { code: "ABCD-1234" };
assert.equal(main.cockpitPart.signInWaitsOn({ hub }, "hub-sign-in:codex"), true);
assert.equal(main.cockpitPart.signInWaitsOn({ hub }, "another-warning"), false);
hub.sign_in.codex.waiting = null;
main.cockpitPart.notice({ hub });
hub.sign_in.codex.finished_at = "2026-11-12T00:00:00Z";
main.cockpitPart.notice({ hub });
assert.equal(notices.length, 1);

// Codex requests failing in numbers across the hub's accounts warn; a span before the last 20 minutes counts for nothing.
const span = (at, succeeded, failed) => ({ at, succeeded, failed });
const codex = (id, traffic) => ({ id, provider: "codex", state: "working", traffic });
const busy = {
  accounts: [
    codex("a", [span("2026-11-18T08:10:00Z", 0, 500), span("2026-11-18T08:30:00Z", 30, 12), span("2026-11-18T08:40:00Z", 20, 8)]),
    codex("b", [span("2026-11-18T08:40:00Z", 10, 4)]),
  ],
  read_at: "2026-11-18T08:45:00Z", sign_in: { claude: {}, codex: {} },
};
const failing = (state) => main.cockpitPart.warnings({ hub: state }).filter((each) => each.key.startsWith("hub-failing-"));
const [codexFailing] = failing(busy);
assert.equal(codexFailing.key, "hub-failing-codex");
assert.equal(codexFailing.tone, "fail");
assert.match(codexFailing.more, /^24 of 84 Codex requests failed in the last 20 minutes/);
busy.accounts[1].traffic = [span("2026-11-18T08:40:00Z", 60, 4)];
assert.equal(failing(busy).length, 0, "a quarter of the requests failing, at least, is the bar");
busy.accounts = [codex("a", [span("2026-11-18T08:40:00Z", 1, 19)])];
assert.equal(failing(busy).length, 0, "fewer than 20 failures are a passing hiccup");
console.log("PASS the hub part's main-page hooks and early script loading");
