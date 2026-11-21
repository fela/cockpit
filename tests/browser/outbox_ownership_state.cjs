// An outbox scan leaves live pages' actions alone and preserves changes made while it checks a page lock.
// node tests/browser/outbox_ownership_state.cjs [directory containing errors.js]
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const folder = process.argv[2] || path.resolve(__dirname, "../../src/cockpit/static");
const source = fs.readFileSync(path.join(folder, "errors.js"), "utf8");
const scan = source.slice(source.indexOf("  async function takeLeft()"), source.indexOf("  // Sends one action left behind;"));
const user = "a".repeat(64), until = 2000000000;
const prefix = `cockpit.outbox.${user}.${until}.`;
const entry = (page, at, n) => JSON.stringify({ user, until, page, at, n, body: "words to send" });

async function run(change) {
  const values = new Map([
    [prefix + "first", entry("closed", 10, 2)],
    [prefix + "later", entry("closed", 5, 1)],
    [prefix + "live", entry("open", 1, 1)],
    [prefix + "own", entry("this-page", 1, 1)],
    [prefix + "new", entry("closed", 100, 1)],
  ]);
  const errors = [];
  let checked = 0;
  const context = vm.createContext({
    OUTBOX: prefix, PAGE_ID: "this-page", PAGE_LOCK: "cockpit-page:", scope: user, scopeUntil: until,
    Date: { now: () => 100 },
    localStorage: {
      get length() { return values.size; },
      key: (i) => [...values.keys()][i],
      getItem: (key) => values.get(key) ?? null,
      setItem: (key, value) => values.set(key, value),
    },
    navigator: { locks: { request: async (name, options, callback) => {
      assert.deepEqual(JSON.parse(JSON.stringify(options)), { ifAvailable: true });
      if (++checked === 1) change(values);
      return callback(name === "cockpit-page:open" ? null : { name });
    } } },
    logError: (where, failure) => errors.push([where, failure.message]),
  });
  vm.runInContext(scan + "; globalThis.scanOutbox = takeLeft;", context);
  const adopted = Array.from(await context.scanOutbox(), ([id]) => id);
  assert.deepEqual(errors, [], "storage changes during a lock check keep the scan running");
  return { adopted, values };
}

(async () => {
  const normal = await run(() => {});
  assert.deepEqual(normal.adopted, ["later", "first"], "only closed pages' older actions are adopted, oldest first");
  assert.equal(JSON.parse(normal.values.get(prefix + "live")).page, "open");
  console.log("pass: live pages retain their actions and closed pages' actions retain their order");

  const finished = await run((values) => values.delete(prefix + "first"));
  assert.deepEqual(finished.adopted, ["later"]);
  assert.equal(finished.values.has(prefix + "first"), false, "an action completed during the check stays removed");
  console.log("pass: a completed action is never restored by an awaiting scan");

  const removed = await run((values) => values.delete(prefix + "later"));
  assert.deepEqual(removed.adopted, ["first"], "a deleted later key does not stop the scan");
  console.log("pass: deletion of another action keeps the scan running");

  const updated = await run((values) => values.set(prefix + "first", entry("new-owner", 20, 3)));
  assert.deepEqual(updated.adopted, ["later"]);
  assert.equal(JSON.parse(updated.values.get(prefix + "first")).page, "new-owner");
  console.log("pass: a changed action keeps its new owner and contents");
})().catch((error) => { console.error(error); process.exitCode = 1; });
