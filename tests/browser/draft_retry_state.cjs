"use strict";

const assert = require("node:assert/strict");
const { randomUUID } = require("node:crypto");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname, "../../src/cockpit/static/draft-sync.js"), "utf8");
const storage = new Map();
const entry = "drafts/msg:step:1";
const original = JSON.stringify("Before the clear");
const replacement = JSON.stringify("Replacement words");

function makeTab(responses) {
  const timers = new Map(), requests = [], errors = [];
  let timerId = 0;
  const tab = vm.createContext({
    document: { addEventListener() {} },
    navigator: { onLine: true },
    localStorage: { setItem: (key, value) => storage.set(key, value), removeItem: (key) => storage.delete(key) },
    readStored: (key, fallback) => storage.has(key) ? JSON.parse(storage.get(key)) : fallback,
    writeStored: (key, value) => storage.set(key, JSON.stringify(value)),
    DRAFTS_KEY: "drafts", TARGETS_KEY: "choices", STARTS_KEY: "starts", REPLACING_KEY: "replacing", PICKS_KEY: "picks",
    ADD_MAX: 1000, PICK_KEY: "feedback", SECTIONS: [],
    ui: { drafts: new Map(), choices: new Map(), starts: new Map(), replacing: new Map(), picks: new Map() },
    typingKey: () => null,
    drawStartLines() {},
    addEventListener() {},
    crypto: { randomUUID },
    setTimeout(callback, delay) { const id = ++timerId; timers.set(id, { callback, delay }); return id; },
    clearTimeout: (id) => timers.delete(id),
    jsonRequest: (_method, body) => ({ body: JSON.stringify(body) }),
    answerIsAway: (status) => status === 502,
    drainAnswer() {},
    answerJson: (response) => response.json(),
    fetch: async (_url, { body }) => {
      requests.push(JSON.parse(body));
      return responses.shift();
    },
    logError: (_place, error) => errors.push(error.message),
  });
  tab.window = tab;
  vm.runInContext(source, tab);
  return {
    sync: tab.CockpitDraftSync, requests, errors,
    runTimer(delay) {
      assert.equal(timers.size, 1);
      const [id, timer] = timers.entries().next().value;
      if (delay !== undefined) assert.equal(timer.delay, delay);
      timers.delete(id);
      return timer.callback();
    },
  };
}

const answer = (value, version, at = version) => Response.json({
  entries: { [entry]: [value, version] }, taken: [entry], at,
});
const synced = () => JSON.parse(storage.get("cockpit.drafts-synced"));

(async () => {
  const first = makeTab([new Response(null, { status: 502 }), answer("", 2, 4), answer(replacement, 5)]);
  const second = makeTab([answer("", 2), answer(replacement, 3)]);

  first.sync.take({ at: 1, entries: { [entry]: [original, 1] } }, Date.now());
  storage.set("drafts", "[]");
  first.sync.changed();
  const clearing = first.runTimer(800);
  await new Promise((resolve) => setImmediate(resolve));

  // The clear reached Cockpit. The second tab settles its cached answer while the first waits to retry.
  second.sync.changed();
  await second.runTimer(800);
  assert.deepEqual(second.requests, first.requests);
  assert.deepEqual(first.requests[0].changes, [{ key: entry, value: "", base: 1 }]);
  assert.deepEqual(synced().unanswered, []);
  storage.set("drafts", JSON.stringify([["msg:step:1", JSON.parse(replacement)]]));
  second.sync.changed();
  await second.runTimer(800);
  assert.deepEqual(synced().entries, [[entry, [replacement, 3]]]);

  // Another device clears the replacement before the first tab receives its cached answer to the original clear.
  const remoteClear = { at: 4, entries: {} };
  first.runTimer(2000);
  await clearing;
  assert.deepEqual(first.requests[1], first.requests[0]);
  const adoptedClear = first.sync.take(remoteClear, Date.now());
  await first.runTimer();

  assert.equal(first.requests.length, 2, "The first tab must not resend the cleared replacement as new text");
  assert.equal(adoptedClear, true, "The newer clear must apply against the saved replacement version");
  assert.deepEqual(JSON.parse(storage.get("drafts")), []);
  assert.deepEqual([...first.errors, ...second.errors], []);
  console.log("pass: a late reply to an already settled clear cannot resurrect text cleared on another device");
})().catch((error) => { console.error(error); process.exitCode = 1; });
