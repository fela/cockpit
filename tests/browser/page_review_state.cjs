// Companion state checks for page_review.cjs, runnable where Chromium/socket creation is blocked.
// Executes the page's own functions with controlled requests and small DOM/storage substitutes.
// node tests/browser/page_review_state.cjs [directory containing a previous copy of the static JS]
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const folder = process.argv[2] || path.resolve(__dirname, "../../src/cockpit/static");
const source = (file) => fs.readFileSync(path.join(folder, file), "utf8");
function part(file, from, to) {
  const text = source(file);
  const start = text.indexOf(from);
  const end = text.indexOf(to, start + from.length);
  assert.ok(start >= 0 && end > start, `${file}: missing function boundary ${from}`);
  return text.slice(start, end);
}
const gate = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const tick = () => new Promise((resolve) => setImmediate(resolve));
const noop = () => {};
function world(extra) {
  const handlers = new Map();
  const storage = new Map();
  const context = vm.createContext({
    console, setTimeout, clearTimeout, setInterval: () => 1, clearInterval: noop, URLSearchParams, CustomEvent, performance,
    holding: new Set(), TRY_MS: 30000, errors: [],
    ui: { busy: new Set(), finishing: new Set(), removedByUser: new Map(), tookAway: new Map(), leftAt: new Map(), state: {},
      snoozing: new Map(), inFlight: new Map(), closesSkip: new Map(), sendCount: 0 },
    refresh: async () => {}, render: noop, toast: noop, logError: noop,
    PageComments: { settled: async () => {} }, Icons: { setProjects: noop },
    optimistic: async (_apply, work, undo) => { try { return await work(); } catch (e) { if (undo) undo(); throw e; } },
    refused: (_label, error) => error,
    addEventListener: (name, handler) => { handlers.set(name, [...(handlers.get(name) || []), handler]); },
    dispatchEvent: (event) => { for (const handler of handlers.get(event.type) || []) handler(event); return !event.defaultPrevented; },
    localStorage: { getItem: (key) => storage.get(key) || null, setItem: (key, value) => storage.set(key, value), removeItem: (key) => storage.delete(key) },
    ...extra,
  });
  context.window = context;
  vm.runInContext(part("errors.js", "  function holdLeave(sent)", "  function fetchThroughRestart"), context);
  // The shared reader of Cockpit's replies; an older copy has each script read its own.
  if (fs.existsSync(path.join(folder, "reply.js"))) vm.runInContext(source("reply.js"), context);
  return { context, handlers, storage, run: (text, options) => vm.runInContext(text, context, options) };
}
function actionWorld(extra) {
  const w = world(extra);
  w.run(part("app.js", "async function run(key, work, success)", "// Optimistic changes:"));
  return w;
}
const tests = [];
const check = (id, test) => tests.push([id, test]);

check("P1", async () => {
  const first = gate();
  const sent = [];
  const w = actionWorld({
    followQueue: noop, drawn: (s) => s, itemOthers: () => 0, beforeFinish: () => [], beforeIds: () => [],
    closeAnimation: noop, stepCard: () => null, track: noop, closedWords: (s) => s, closedNotice: () => null,
    sessionEndWords: () => "", choicesSaved: async () => {}, uploadStaged: async () => [],
    api: async (_method, url) => { sent.push(url); if (sent.length === 1) await first.promise; return {}; },
  });
  w.run(part("app.js", "let finishRequests =", "// The words of the notice after"));
  const a = w.run('finishItem({id:1,item_id:1,item:{title:"A"}}, "complete", null, "", [], "", null, null)');
  const b = w.run('finishItem({id:2,item_id:2,item:{title:"B"}}, "complete", null, "", [], "", null, null)');
  await tick();
  try {
    assert.equal(sent.length, 1);
    assert.ok(w.context.holding.size, "queued finish has no leave guard");
  } finally { first.resolve(); await Promise.all([a, b]); }
  assert.equal(sent.length, 2);
  assert.equal(w.context.holding.size, 0);
});

check("P2", async () => {
  for (const refuse of [false, true]) {
    const deletion = gate();
    const w = actionWorld({ api: () => deletion.promise, CockpitChoices: { settled: async () => {} } });
    w.run(part("app.js", "async function choicesSaved(step)", "// Hand off: like Send"));
    // The declaration was added with the fix; the old function needs no such map.
    const text = source("app.js");
    const start = text.includes("const commentDeletions =") ? "const commentDeletions =" : "function deletePageComment(comment)";
    w.run(part("app.js", start, "// A delivery state as a mark"));
    const removing = w.run("deletePageComment({id:1,step_id:7})");
    let went = false;
    const answer = w.run("choicesSaved({id:7})").then(() => { went = true; }, (e) => e);
    await tick();
    try {
      assert.equal(went, false, "answer overtook deletion");
      assert.ok(w.context.holding.size);
    } finally { if (refuse) deletion.reject(new Error("test refusal")); else deletion.resolve({}); }
    await removing;
    const result = await answer;
    if (refuse) {
      assert.equal(went, false);
      assert.match(result.message, /not deleted/);
      // A finish whose earlier turn held it back must see the failure too; trying Delete again clears it.
      await assert.rejects(w.run("choicesSaved({id:7})"), /not deleted/);
      await w.run("choicesSaved({id:8})");
      w.context.api = async () => ({});
      await w.run("deletePageComment({id:1,step_id:7})");
      await w.run("choicesSaved({id:7})");
    }
    else assert.equal(went, true);
  }
  for (const action of ["run", "allow"]) {
    const deletion = gate();
    let answers = 0;
    const w = actionWorld({ followQueue: noop, flightStart: noop, flyToAgents: noop,
      api: (method) => { if (method === "DELETE") return deletion.promise; answers += 1; return Promise.resolve({}); },
      CockpitChoices: { settled: async () => {} } });
    w.run(part("app.js", "async function choicesSaved(step)", "// Hand off: like Send"));
    const start = source("app.js").includes("const commentDeletions =") ? "const commentDeletions =" : "function deletePageComment(comment)";
    w.run(part("app.js", start, "// A delivery state as a mark"));
    w.run(part("app.js", "function pressStepButton(", "function runCommand("));
    w.run(part("app.js", "function takenByUser(id)", "// A held step missing from the state"));
    const removing = w.run("deletePageComment({id:1,step_id:7})");
    const pressing = w.run(`pressStepButton({id:7,target:{session_id:"test"}}, ${JSON.stringify(action)}, {}, "Go", "Refused", null, null)`);
    await tick();
    try { assert.equal(answers, 0, `${action} overtook deletion`); }
    finally { deletion.resolve({}); await Promise.all([removing, pressing]); }
    assert.equal(answers, 1);
  }
});

check("P4", async () => {
  const box = { value: "Follow-up words" };
  const w = world({ document: { querySelector: () => box } });
  w.run(part("app.js", "const FOLLOW_HOLD_MS =", "function offerFollowUp"));
  const prevented = () => {
    let result = false;
    for (const handle of w.handlers.get("beforeunload") || []) handle({ preventDefault: () => { result = true; } });
    return result;
  };
  assert.equal(prevented(), true, "follow-up typing has no leave guard");
  box.value = "";
  assert.equal(prevented(), false);
});

check("P5", async () => {
  const first = gate();
  const values = [];
  const w = world({ document: {}, sendAction: async (_url, options) => {
    values.push(JSON.parse(options.body).chosen);
    if (values.length === 1) await first.promise;
    return { response: { ok: true }, data: {} };
  } });
  w.run(source("page-choices.js"));
  w.run('CockpitChoices.save(1,{name:"field",chosen:"first",step_id:1},()=>{}); CockpitChoices.save(1,{name:"field",chosen:"latest",step_id:1},()=>{})');
  await tick();
  try { assert.ok(w.context.holding.size, "queued choice has no leave guard"); }
  finally { first.resolve(); await w.run("CockpitChoices.settled(1)"); }
  assert.deepEqual(values, ["first", "latest"]);
  assert.equal(w.context.holding.size, 0);
});

// A page's frame tells Cockpit's page of choices the server cannot keep: each shows as not saved and holds back the
// step's answer. A label goes up as its first 4000 characters, the most the server takes.
function choicesWorld() {
  const sent = [], notes = [];
  const frame = { contentWindow: {}, getAttribute: () => "/pages/3/figure", dataset: { step: "7" } };
  const w = world({ document: { querySelectorAll: () => [frame] }, sendAction: async (_url, options) => {
    sent.push(JSON.parse(options.body));
    return { response: { ok: true, status: 200 }, data: { choices: [] } };
  } });
  w.run(source("page-choices.js"));
  w.context.addEventListener("cockpit:choice", (event) => notes.push(event.detail));
  // Resolves to the error that holds back the step's answer, or null when the answer may go.
  const choose = async (chosen, label) => {
    const event = { source: frame.contentWindow, data: { cockpitChoices: { type: "chose", name: "field", chosen, label } } };
    for (const handle of w.handlers.get("message")) handle(event);
    return w.run("CockpitChoices.settled(7)").then(() => null, (error) => error);
  };
  return { sent, notes, choose };
}
check("F3: a choice the server would refuse shows as not saved and holds the answer", async () => {
  const refused = { 5: /text, a list of texts or empty/, '{"text":"x"}': /text, a list of texts or empty/,
    '["a",5]': /text, a list of texts or empty/, [JSON.stringify("x".repeat(2001))]: /at most 100 values of 2000 characters/,
    [JSON.stringify(Array(101).fill("a"))]: /at most 100 values of 2000 characters/ };
  for (const [chosen, reason] of Object.entries(refused)) {
    const { sent, notes, choose } = choicesWorld();
    const held = await choose(JSON.parse(chosen), "");
    const shown = chosen.slice(0, 30);
    assert.equal(notes.at(-1)?.ok, false, `${shown}: no "Choice not saved"`);
    assert.match(notes.at(-1).error, reason);
    assert.match(held?.message || "", /was not saved/, `${shown}: the answer went without the choice`);
    assert.equal(sent.length, 0);
  }
  const { sent, notes, choose } = choicesWorld();
  await choose("x".repeat(2001), "");
  assert.equal(await choose("x".repeat(2000), "Long words ".repeat(300)), null, "a good choice did not clear the failure");
  assert.equal(notes.at(-1).ok, true);
  assert.equal(sent.at(-1).label.length, 3300);
  await choose("again", "x".repeat(4001));
  assert.equal(sent.at(-1).label.length, 4000);
  await choose("once more", 5);
  assert.equal(sent.at(-1).label, "");
  // The limits count characters as the server does: an emoji is one.
  assert.equal(await choose("😀".repeat(2000), "😀".repeat(4001)), null, "2000 emoji were refused");
  assert.equal(sent.at(-1).chosen, "😀".repeat(2000));
  assert.equal(sent.at(-1).label, "😀".repeat(4000));
  assert.match((await choose("😀".repeat(2001), ""))?.message || "", /was not saved/);
});

// With the sign-in run out, Cockpit answers a choice's save, and the read of a page's saved choices, with a 401: the
// page goes to sign in, as it does from the Pages view, page comments and the Spec page.
check("F9: a page choice met with a 401 sends the page to sign in", async () => {
  for (const type of ["chose", "ready"]) {
    const frame = { contentWindow: { postMessage: noop }, getAttribute: () => "/pages/3/figure", dataset: { step: "7" } };
    const signedOut = async () => ({ response: { ok: false, status: 401 }, data: { detail: "sign in first" } });
    const w = world({ location: {}, document: { querySelectorAll: () => [frame] }, sendAction: signedOut, readThroughRestart: signedOut });
    w.run(source("page-choices.js"));
    const event = { source: frame.contentWindow, data: { cockpitChoices: { type, name: "field", chosen: "a", label: "" } } };
    for (const handle of w.handlers.get("message")) handle(event);
    await w.run("CockpitChoices.settled(7)").catch(noop);
    await tick();
    assert.equal(w.context.location.href, "/auth/google", `${type}: the page did not go to sign in`);
  }
});

function repliesWorld(extra) {
  const w = world({ stepId: 7, shown: 1, commenting: false, busy: false, sent: { text: "original", timer: 1 },
    renderReply: noop, reload: async () => {}, replyError: "", notice: noop, ...extra });
  w.run(part("pages.js", "const replies =", "// Under the page: the step's reply box"));
  return w;
}
check("P6", async () => {
  let metadata;
  const w = repliesWorld({ sendAction: async (_path, _options, _try, words) => {
    metadata = words;
    return { response: { ok: true, status: 200 }, data: { stopped: true, unsent: "recovered" } };
  } });
  w.run(part("pages.js", "async function write(path, body", '// "Today", "Yesterday"'));
  w.run(part("pages.js", "async function takeBack()", "// Without a step that takes them"));
  await w.run("takeBack()");
  assert.ok(metadata, "return action has no reply identity");
  assert.equal(metadata.store, "cockpit.pages.reply:7");
  assert.equal(metadata.key, null);
  // A new page reads the empty draft before errors.js replays the return.
  const next = repliesWorld();
  next.run('savedReply(replyKey(true)); keepReply(replyKey(true), "newer words")');
  const event = { detail: { words: metadata, back: "server words" }, preventDefault: noop };
  for (const handle of next.handlers.get("cockpit:left-answered") || []) handle(event);
  assert.equal(next.run("savedReply(replyKey(true))"), "server words\n\nnewer words");
});

check("P7", async () => {
  for (const stopped of [true, false]) {
    let notice = "";
    const w = repliesWorld({ write: async () => ({ stopped, unsent: stopped ? "server words" : null }),
      notice: (text) => { notice = text; } });
    w.run(part("pages.js", "async function takeBack()", "// Without a step that takes them"));
    await w.run("takeBack()");
    assert.equal(w.run("savedReply(replyKey(true))"), stopped ? "server words" : "");
    if (!stopped) assert.match(notice, /already reached/);
  }
});

check("P8", async () => {
  const body = {};
  const w = world({ document: { body }, alive: (el) => Boolean(el && el.isConnected) });
  // Test the real traversal with Wider's terminal body and a chain that skips the requested element.
  w.run(part("pick.js", "  const WAY_LIMIT =", "  // How alike"));
  w.context.wider = () => body;
  w.context.el = { isConnected: false, contains: () => true };
  w.context.from = { isConnected: false };
  assert.equal(w.run('toward(el,from,"div")', { timeout: 1000 }), null);
  w.context.el.isConnected = w.context.from.isConnected = true;
  assert.equal(w.run('toward(el,from,"div")', { timeout: 1000 }), null);
  w.context.wider = () => w.context.el;
  assert.equal(w.run('toward(el,from,"div")', { timeout: 1000 }), w.context.from);
  w.context.wider = () => w.context.from;
  assert.equal(w.run('toward(el,from,"div")', { timeout: 1000 }), null);
});

check("P9", async () => {
  // Exercise each await boundary: listing, article, then the read mark and shownListing update.
  for (const boundary of ["listing", "body", "read"]) {
    const held = gate();
    const article = { replaceChildren: () => { drawn.push(currentBody); } };
    const drawn = [], marked = [];
    let listings = 0, currentBody = null;
    const w = world({ team: { saves: 0 }, location: { search: "" }, document: { getElementById: () => article },
      renderTeam: noop, renderDays: noop, launchNote: () => null, dayWords: (day) => day, at: (text) => text,
      h: (_tag, props) => { if (props?.class === "prose briefing-body") currentBody = props.html; return {}; },
      Loading: { settle: noop }, api: async (method, url) => {
        if (url === "/api/briefings") {
          const id = ++listings;
          if (id === 1 && boundary === "listing") await held.promise;
          return { briefings: [{ id }], read_id: 0 };
        }
        const id = Number(url.split("/")[3]);
        if (method === "POST") { marked.push(id); if (id === 1 && boundary === "read") await held.promise; return {}; }
        if (id === 1 && boundary === "body") await held.promise;
        return { id, day: String(id), body_html: String(id) };
      } });
    w.run('let shownListing = ""; let loadNumber = 0;');
    // Not in the layer (no `overlay=1` in the search), so overlayReady sends nothing.
    w.run(part("briefing.js", "const overlay =", "// The task a link names"));
    w.run(part("briefing.js", "async function load()", "// The page reads the state when it opens"));
    const old = w.run("load()");
    await tick();
    await w.run("load()");
    const latest = w.run("shownListing");
    held.resolve();
    await old;
    assert.equal(drawn.at(-1), "2", `${boundary}: older article replaced newer`);
    assert.equal(w.run("shownListing"), latest, `${boundary}: older listing marked shown`);
    if (boundary !== "read") assert.deepEqual(marked, [2], `${boundary}: obsolete briefing marked read`);
  }
});

check("Q1: Send keeps words recovered while it waits for Take back", async () => {
  for (const recover of [true, false]) {
    const replay = gate();
    const requests = [];
    const w = repliesWorld({ sent: null, theStep: () => ({ id: 7 }), stepTakes: () => true,
      TAKE_BACK_SHOWN_MS: 60000, setTimeout: () => 1,
      CockpitChoices: { settled: () => replay.promise },
      write: async (_path, body) => { requests.push(body.text); return { id: 8 }; } });
    w.run(part("pages.js", "async function send()", "async function release()"));
    w.run('keepReply(replyKey(true), "new answer")');
    const sending = w.run("send()");
    await tick();
    assert.deepEqual(requests, [], "Send must still be waiting");
    if (recover) w.context.dispatchEvent({ type: "cockpit:left-answered",
      detail: { words: { store: "cockpit.pages.reply:7", key: null }, back: "stopped words" }, preventDefault: noop });
    replay.resolve();
    await sending;
    assert.deepEqual(requests, ["new answer"]);
    const expected = recover ? "stopped words\n\nnew answer" : "";
    assert.equal(w.run("savedReply(replyKey(true))"), expected);
    assert.equal(w.storage.get("cockpit.pages.reply:7") || "", expected);
  }
});

check("Q3: DELETE 404 counts as a deleted comment", async () => {
  let changed = 0;
  const w = actionWorld({ api: async () => { throw Object.assign(new Error("already gone"), { status: 404 }); },
    CockpitChoices: { settled: async () => {} } });
  w.run(part("app.js", "async function choicesSaved(step)", "// Hand off: like Send"));
  w.run(part("app.js", "const commentDeletions =", "// A delivery state as a mark"));
  w.context.addEventListener("cockpit:page-comments-changed", () => { changed += 1; });
  assert.equal(await w.run("deletePageComment({id:1,step_id:7})"), true);
  assert.equal(w.run("commentDeletions.size"), 0);
  await w.run("choicesSaved({id:7})");
  assert.equal(changed, 1);
});

check("Q3: refreshed missing comments drop retained deletion failures", async () => {
  // Also cover an answer already waiting when the failed Delete's refresh discovers the absence.
  for (const missingBeforeRefusal of [false, true]) {
    const deletion = gate();
    const comment = { id: 1, step_id: 7 };
    let state = { page_comments: [comment], permission_requests: [], now: new Date().toISOString(), usage: {}, queue: [] };
    const logged = [];
    const w = actionWorld({ document: { hidden: false }, ADDS_WAIT_MS: 1000, ARRIVED_MS: 10000, RERENDER_MS: 1000,
      readState: async () => state, usageGeneration: 0, safeRender: noop, renderStatus: noop, renderUsage: noop, fitHeader: noop,
      forgetSentMarks: noop, settleUploads: noop, noticeSideAnswers: noop, noticeCodexSignIns: noop, noticeSnoozeReads: noop, caughtUp: noop,
      logError: (label, error) => { logged.push([label, error]); },
      api: () => deletion.promise, CockpitChoices: { settled: async () => {} } });
    Object.assign(w.context.ui, { pending: new Map(), handing: new Map(), tick: 0, error: "", state, renderedAt: 0, catchUpFrom: null, openedHidden: new Set() });
    w.run(part("app.js", "async function refresh()", "// Resolves to whether the work went through."));
    w.run(part("app.js", "async function choicesSaved(step)", "// Hand off: like Send"));
    w.run(part("app.js", "const commentDeletions =", "// A delivery state as a mark"));
    const removing = w.run("deletePageComment({id:1,step_id:7})");
    const answering = w.run("choicesSaved({id:7})").then(() => null, (error) => error);
    if (missingBeforeRefusal) {
      state = { ...state, page_comments: [] };
      await w.run("refresh()");
      assert.equal(w.run("commentDeletions.size"), 1, "refresh dropped a deletion still on its way");
    }
    deletion.reject(new Error("test refusal"));
    await removing;
    const result = await answering;
    if (missingBeforeRefusal) assert.equal(result, null, "waiting answer rejected a deletion already cleared by refresh");
    else assert.match(result.message, /not deleted/);
    state = { ...state, page_comments: [] };
    await w.run("refresh()");
    assert.equal(w.run("commentDeletions.size"), 0, "missing comment kept its failed deletion");
    await w.run("choicesSaved({id:7})");
    assert.deepEqual(logged.map(([label]) => label), ["action page-comment:1"], "refresh errors must surface");
  }
});

check("Q4: every queued bulk close guards leaving through success and failure", async () => {
  for (const failure of ["none", "first", "second"]) {
    const held = [gate(), gate()];
    const requests = [];
    const w = actionWorld({ STEP_LISTS: ["queue"], followQueue: noop, drawn: (s) => s,
      beforeFinish: (_state, task) => ({ lists: { queue: [] }, itemId: task.item_id }), beforeIds: () => [],
      closeAnimation: noop, closedWords: (s) => s, closedNotice: () => null,
      plural: (n, _one, many) => `${n} ${many}`,
      api: async (_method, url) => { const index = requests.length; requests.push(url); return held[index].promise; } });
    w.context.ui.state.queue = [];
    w.run(part("app.js", "let finishRequests =", "// `answer`:"));
    w.run(part("app.js", "function finishStep(step)", "// Close these tasks (CONTROL-23)"));
    w.run(part("app.js", "function closeListed(", "// Undo of Close these tasks:"));
    const first = w.run('closeListed({id:7,item_id:70}, [{id:1,title:"A"}], null, null)');
    const second = w.run('closeListed({id:8,item_id:80}, [{id:2,title:"B"}], null, null)');
    await tick();
    let guarded, guardedSecond;
    try {
      guarded = w.context.holding.size;
      assert.equal(requests.length, 1, "second bulk close overtook the first");
    } finally {
      if (failure === "first") held[0].reject(new Error("test refusal"));
      else held[0].resolve({ finished: [1], refused: [], step: null });
      await first;
      await tick();
      guardedSecond = w.context.holding.size;
      if (failure === "second") held[1].reject(new Error("test refusal"));
      else held[1].resolve({ finished: [2], refused: [], step: null });
      await second;
    }
    assert.equal(guarded, 2, "both bulk closes need their own leave guard");
    assert.equal(guardedSecond, 1, "second bulk close lost its leave guard before settling");
    assert.equal(requests.length, 2, "queue did not continue after the first close");
    assert.equal(w.context.holding.size, 0, "bulk close leave guards did not drain");
  }
});

(async () => {
  const failures = [];
  for (const [id, test] of tests) {
    try { await test(); console.log(`PASS ${id}`); }
    catch (error) { failures.push(id); console.error(`FAIL ${id}: ${error.stack}`); }
  }
  assert.deepEqual(failures, []);
})().catch((error) => { console.error(error); process.exitCode = 1; });
