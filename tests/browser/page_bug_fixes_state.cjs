// Page recovery and request ordering checks use invented data and controlled requests.
// node tests/browser/page_bug_fixes_state.cjs [static source directory]
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const folder = process.argv[2] || path.resolve(__dirname, "../../src/cockpit/static");
const source = (file) => fs.readFileSync(path.join(folder, file), "utf8");
function part(file, from, to) {
  const text = source(file), start = text.indexOf(from), end = text.indexOf(to, start + from.length);
  assert.ok(start >= 0 && end > start, `${file}: missing boundary ${from}`);
  return text.slice(start, end);
}
const noop = () => {};
const tick = () => new Promise((resolve) => setImmediate(resolve));
const gate = () => {
  let resolve, reject;
  const promise = new Promise((a, b) => { resolve = a; reject = b; });
  return { promise, resolve, reject };
};
function world(extra = {}, storage = new Map()) {
  const events = [];
  const context = vm.createContext({ console, CustomEvent, logError: noop,
    localStorage: { getItem: (key) => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, value), removeItem: (key) => storage.delete(key) },
    dispatchEvent: (event) => { events.push(event); return true; }, ...extra });
  context.window = context;
  return { context, events, storage, run: (text) => vm.runInContext(text, context) };
}
function replayWorld(storage) {
  const w = world({ stored: new Set(), KEEP_MS: 86400e3, writesElsewhere: () => false,
    forget: noop, putBack: noop, throughRestart: async () => ({ response: { ok: false, status: 409 }, data: { detail: "Edit refused" } }),
    withJson: noop }, storage);
  w.run(part("errors.js", "  async function sendOne(action, entry)", "  // Whether the box"));
  return w;
}
const tests = [];
const check = (name, body) => tests.push([name, body]);

check("P1: refused settings and priorities survive replay and another reload", async () => {
  for (const [url, body, words] of [
    ["/api/goals", { goals: [{ text: "Finish tasks", paused: false }, { text: "Review reminders", paused: true }] },
      ["Finish tasks", "(paused) Review reminders"]],
    // A save queued by a page from before priorities could pause.
    ["/api/goals", { texts: ["Finish tasks", "Review reminders"] }, ["Finish tasks", "Review reminders"]],
    ["/api/inbox-settings", { rules: ["Help Ana first", "Review tasks"], sources: "Check the tasks list" },
      ["Help Ana first", "Review tasks", "Check the tasks list"]],
  ]) {
    const w = replayWorld();
    w.context.entry = { path: url, method: "PUT", body: JSON.stringify(body), at: Date.now(), words: null };
    await w.run('sendOne("edit", entry)');
    const notice = w.events.find((event) => event.type === "cockpit:not-sent");
    assert.ok(notice, "refused replay has no notice");
    for (const text of words) assert.ok(notice.detail.message.includes(text), `notice loses ${text}`);
    const reopened = world({}, w.storage);
    assert.deepEqual(JSON.parse(reopened.storage.get(`cockpit.unsaved:${url}`)), body, "reload loses the refused edit");
  }
});

check("P1: an edited retry clears its recovered draft and keeps a later refused edit", async () => {
  for (const laterFailure of [false, true]) {
    const key = "cockpit.unsaved:/api/goals";
    const original = JSON.stringify({goals:[{text:"Finish tasks",paused:false}]});
    const later = JSON.stringify({goals:[{text:"Review reminders",paused:false}]});
    const held = gate(), w = replayWorld(new Map([[key,original]]));
    w.context.crypto = {randomUUID: () => "retry"};
    w.context.sending = new Set();
    w.context.leftSent = Promise.resolve();
    w.context.keep = noop;
    w.context.withJson = async (response) => ({response,data:{}});
    w.context.throughRestart = async (_url, _options, read) => { await held.promise; return read({ok:true}); };
    w.run(part("errors.js", "  function sendAction(path, options, tryMs, words)", "  // An upload's body"));
    const retry = w.run('sendAction("/api/goals", {method:"PUT",body:JSON.stringify({goals:[{text:"Finish tasks and reminders",paused:false}]})}, 30000, null)');
    await tick();
    if (laterFailure) w.storage.set(key,later);
    held.resolve();
    await retry;
    assert.equal(w.storage.get(key), laterFailure ? later : undefined);
  }
});

function commentsWorld(write) {
  const w = world({ write, draw: noop, onChange: noop, stepId: null });
  w.run('let comments = [{id:1},{id:2}]; let writes = 0; let error = "";');
  w.run(part("page-comments.js", "  const deletions =", "  function h("));
  w.run(part("page-comments.js", "    async function remove(comment)", "    function show(comment)"));
  return w;
}
check("P2: a revision rebuild keeps deleted ids out of Send and Add as new item", async () => {
  for (const action of ["sendToItem", "addAsItem"]) {
    const deletion = gate(), sent = [];
    const w = commentsWorld(() => deletion.promise);
    w.run(`
      const PageComments = {settled};
      let rebuilt = [{id:1},{id:2}];
      const mine = () => true;
      const column = {stepId:null, mine:${source("page-comments.js").match(/mine: (\(\) => [^\n]+),/)[1].replace(/comments/g, "rebuilt")}};
      const pages = [{id:9,item_title:"Tasks"}], shown = 9;
      const state = {checkouts:["/tasks"]};
      let busy = false, replyError = "";
      const replyKey = () => "draft", savedReply = () => "Answer";
      const keepReply = () => {}, renderReply = () => {}, notice = () => {}, reload = async () => {};
    `);
    const removing = w.run("remove({id:1})");
    w.context.write = async (url, body) => { sent.push({ url, body }); return {id:10,summary:"Review tasks"}; };
    w.run(part("pages.js", `async function ${action}()`, action === "sendToItem" ? "// Add as new item:" : "function notice(text)"));
    const sending = w.run(`${action}()`);
    await tick();
    assert.equal(sent.length, 0, "Send overtakes deletion");
    deletion.resolve({});
    await Promise.all([removing, sending]);
    assert.equal(JSON.stringify(sent[0].body.comment_ids), "[2]", "rebuilt list sends a deleted comment");
    assert.equal(w.run("replyError"), "");
  }
});

check("P3: Send waits for a deletion begun while a page choice saves", async () => {
  for (const refuses of [false, true]) {
    const choice = gate(), deletion = gate(), requests = [];
    const w = commentsWorld(() => deletion.promise);
    w.run(`
      const PageComments = {settled};
      const CockpitChoices = {settled:() => choice.promise};
      const step = {id:7,item:{title:"Tasks"}};
      const theStep = () => step, stepTakes = () => true;
      let busy = false, replyError = "", sent = null;
      const savedReply = () => "Review tasks", replyKey = () => "draft";
      const keepReply = () => {}, renderReply = () => {}, reload = async () => {}, notice = () => {}, release = () => {};
      const TAKE_BACK_SHOWN_MS = 8000;
    `);
    w.context.choice = choice;
    w.context.setTimeout = noop;
    w.context.write = async (url) => { requests.push(url); return {id:10}; };
    w.run(part("pages.js", "async function send()", "async function release()"));
    const sending = w.run("send()");
    await tick();
    w.context.write = (method, url) => {
      if (method === "DELETE") return deletion.promise;
      requests.push(method);
      return Promise.resolve({id:10});
    };
    w.context.stepId = 7;
    const removing = w.run("remove({id:1})");
    choice.resolve();
    await tick();
    const overtook = requests.length;
    if (refuses) deletion.reject(new Error("Delete refused")); else deletion.resolve({});
    await Promise.all([sending, removing]);
    assert.equal(overtook, 0, "Send overtakes a deletion begun during the choice save");
    assert.equal(requests.length, refuses ? 0 : 1);
    if (refuses) assert.match(w.run("replyError"), /Delete refused/);
    else assert.equal(w.run("error"), "", "successful Delete shows an error");
  }
});

check("P4: Take back expires during hover", async () => {
  const timers = new Map();
  let now = 1000, next = 0;
  const button = {disabled:false};
  const toast = {hidden:false, matches: () => true, querySelector: () => button};
  const w = world({ ui: {toastShown:3}, document: {getElementById: () => toast},
    Date: {now: () => now, parse: Date.parse}, api: async () => ({take_back_until:new Date(61000).toISOString()}),
    setTimeout: (fn, ms) => { timers.set(++next, {fn, at:now + ms}); return next; },
    clearTimeout: (id) => timers.delete(id) });
  w.run(part("app.js", "function holdMessage(mark)", "// This box belongs"));
  w.run('const mark = {id:9,notice:3};');
  await w.run("holdMessage(mark)");
  assert.equal(button.disabled, false);
  now = 61000;
  for (const timer of [...timers.values()]) if (timer.at <= now) timer.fn();
  assert.equal(button.disabled, true, "hover leaves expired Take back enabled");
  assert.equal(toast.hidden, false, "expiry hides unsent follow-up text");
});

check("F7: an exact time reads on the 24-hour clock in a 12-hour locale", () => {
  class USDate extends Date { toLocaleString(_locale, options) { return super.toLocaleString("en-US", options); } }
  const w = world({ Date: USDate });
  w.run(part("motion.js", "  function exactTime(iso)", "  // A control's shortcut"));
  const year = new Date().getFullYear();
  for (const [hour, clock] of [[14, "14:05"], [0, "00:05"]]) {
    const shown = w.run(`exactTime(${JSON.stringify(new Date(year, 10, 11, hour, 5).toISOString())})`);
    assert.ok(shown.includes(clock), `${shown} lacks ${clock}`);
    assert.doesNotMatch(shown, /AM|PM/);
  }
});

check("F8: words in a session view opened without an item mark no item and stay in that view", () => {
  const drafts = new Map([["msg:view:S1", "Loose thought"]]);
  const state = { items: [{ id: 5, sessions: ["S1"] }], queue: [], ready_next: [], with_agents: [], snoozed: [] };
  const w = world({ ui: { navigations: 0, state, drafts, next: null, shownItem: 5 }, sessionView: { turn: 0, timer: null }, closeSettings: noop,
    hasDraft: () => false, addMoreKey: (id) => `more:${id}`, onPhone: () => false, phoneListAsked: () => false,
    drawn: () => ({ timer: null }), leaveTimedStep: noop, leaveFeedbackHoldStep: noop, slideView: (_way, show) => show(),
    showPhoneItem: noop, clearTimeout: noop, document: { body: { classList: { add: noop } } }, keepSelected: noop,
    drawSessionView: noop, render: noop, scrollTo: noop, loadSessionLog: noop, saveDrafts: noop });
  w.run(part("sidebar.js", "function itemHasDraft(itemId)", "// Typing shows and hides"));
  w.run(part("sidebar.js", "function openSessionView(sessionId, itemId)", "// Back gives the focus"));
  assert.equal(w.run("itemHasDraft(5)"), false, "the item the session leads shows a pencil");
  w.run("openSessionView('S1', 5)");
  assert.deepEqual([...drafts], [["msg:view:S1", "Loose thought"]], "the view opened for the item takes the words over");
  drafts.set("msg:view:S1:5", "Item thought");
  assert.equal(w.run("itemHasDraft(5)"), true, "words in the view opened for the item show no pencil");
});

(async () => {
  const failures = [];
  for (const [name, body] of tests) {
    try { await body(); console.log(`PASS ${name}`); }
    catch (error) { failures.push(name); console.error(`FAIL ${name}: ${error.stack}`); }
  }
  assert.deepEqual(failures, []);
})().catch((error) => { console.error(error); process.exitCode = 1; });
