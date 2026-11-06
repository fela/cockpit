// Controlled request ordering checks; no server, account files or browser profile needed.
// node tests/browser/reviewed_defects_state.cjs [static source directory]
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const folder = process.argv[2] || path.resolve(__dirname, "../../src/cockpit/static");
function part(file, from, to) {
  const text = fs.readFileSync(path.join(folder, file), "utf8");
  const start = text.indexOf(from), end = text.indexOf(to, start + from.length);
  assert.ok(start >= 0 && end > start, `${file}: missing function boundary ${from}`);
  return text.slice(start, end);
}
const noop = () => {};
const tick = () => new Promise((resolve) => setImmediate(resolve));
function gate() {
  let resolve, reject;
  const promise = new Promise((a, b) => { resolve = a; reject = b; });
  return { promise, resolve, reject };
}
function world(extra) {
  const context = vm.createContext({ console, logError: noop, ...extra });
  return { context, run: (text) => vm.runInContext(text, context) };
}
function commentsWorld(write) {
  const w = world({ write, draw: noop, onChange: noop, stepId: null });
  w.run('let comments = [{id:1},{id:2}]; let writes = 0; let error = "";');
  w.run(part("page-comments.js", "  const deletions =", "  function h("));
  w.run(part("page-comments.js", "    async function remove(comment)", "    function show(comment)"));
  return w;
}
const tests = [];
const check = (name, body) => tests.push([name, body]);

for (const action of ["sendToItem", "addAsItem"]) {
  check(`${action} reads the rebuilt column after waiting`, async () => {
    const deletion = gate(), sent = [];
    const w = commentsWorld(() => deletion.promise);
    w.run(`
      const PageComments = { settled };
      const oldComments = [...comments];
      let column = { stepId: null, mine: () => oldComments };
      const pages = [{id:9, item_title:"Report"}]; const shown = 9;
      let busy = false, replyError = "";
      const state = {checkouts:["/test"]};
      const replyKey = () => "draft", savedReply = () => "answer";
      const keepReply = () => {}, renderReply = () => {}, notice = () => {};
      const reload = async () => {};
    `);
    const removing = w.run("remove({id:1})");
    w.context.write = async (url, body) => { sent.push({ url, body }); return { id: 10, summary: "New item" }; };
    w.run(part("pages.js", `async function ${action}()`, action === "sendToItem" ? "// Add as new item:" : "function notice(text)"));
    const sending = w.run(`${action}()`);
    await tick();
    w.run("column = {stepId:null, mine:() => comments}");
    deletion.resolve({});
    await Promise.all([removing, sending]);
    assert.equal(JSON.stringify(sent[0].body.comment_ids), "[2]");
  });

  check(`${action} waits for deletion and keeps only remaining comments`, async () => {
    for (const outcome of ["deleted", "failed", "all deleted"]) {
      const deletion = gate(), sent = [];
      const w = commentsWorld(() => deletion.promise);
      w.run(`
        const PageComments = { settled };
        const column = { stepId: null, mine: () => comments };
        const pages = [{id:9, item_title:"Report"}]; const shown = 9;
        let busy = false, replyError = "";
        const state = {checkouts:["/test"]};
        const replyKey = () => "draft", savedReply = () => "answer";
        const renders = [];
        const keepReply = () => {}, renderReply = () => renders.push(busy), notice = () => {};
        const reload = async () => {};
      `);
      const removing = w.run("remove({id:1})");
      w.context.write = async (url, body) => { sent.push({ url, body }); return { id: 10, summary: "New item" }; };
      w.run(part("pages.js", `async function ${action}()`, action === "sendToItem" ? "// Add as new item:" : "function notice(text)"));
      const sending = w.run(`${action}()`);
      await tick();
      assert.equal(sent.length, 0, "action overtook pending deletion");
      if (outcome === "all deleted") w.run("comments = [{id:1}]");
      if (outcome === "failed") deletion.reject(new Error("deletion refused"));
      else deletion.resolve({});
      await Promise.all([removing, sending]);
      if (outcome === "deleted") {
        assert.equal(sent[0].url, `/api/item-pages/9/comments/${action === "sendToItem" ? "send" : "item"}`);
        assert.equal(JSON.stringify(sent[0].body.comment_ids), "[2]");
        assert.equal(w.run("replyError"), "");
      } else {
        assert.equal(sent.length, 0);
        if (outcome === "failed") assert.match(w.run("replyError"), /Not (sent|added): deletion refused/);
        assert.equal(w.run("busy"), false);
        assert.equal(w.run("renders.at(-1)"), false, "reply controls stayed busy");
      }
    }
  });
}

check("settled waits for a deletion started during its first wait", async () => {
  const first = gate(), second = gate();
  const w = commentsWorld((method, url) => url.endsWith("/1") ? first.promise : second.promise);
  const removingFirst = w.run("remove({id:1})");
  let finished = false;
  const waiting = w.run("settled(null)").then(() => { finished = true; });
  const removingSecond = w.run("remove({id:2})");
  first.resolve({});
  await removingFirst;
  await tick();
  const finishedEarly = finished;
  second.resolve({});
  await Promise.all([removingSecond, waiting]);
  assert.equal(finishedEarly, false, "settled overtook the second deletion");
  assert.equal(finished, true);
});

check("body labels accept only the declared paragraph labels", () => {
  const w = world({ Node: {TEXT_NODE: 3} });
  w.run(part("app.js", "const BODY_LABELS =", "function labelledBody("));
  w.run(part("app.js", "function labelledBody(", "// The words a menu's summary"));
  w.run(`
    const strong = {nodeName:"STRONG", nodeType:1, textContent:"constructor"};
    const body = {children:[{tagName:"P", childNodes:[strong, {nodeType:3, textContent:" details"}]}],
      querySelectorAll:() => []};
    const prose = () => body;
  `);
  assert.equal(w.run('labelledBody("test", "<p><strong>constructor</strong> details</p>") === body'), true);
  for (const text of ["constructor", "toString", "__proto__", "hasOwnProperty", "ordinary words"]) {
    w.context.text = text;
    assert.equal(w.run('isLabel({nodeName:"STRONG", textContent:text})'), false, text);
  }
  assert.equal(w.run('isLabel({nodeName:"STRONG", textContent:" What changes. "})'), true);
  assert.equal(w.run('isLabel({nodeName:"SPAN", textContent:"Ready."})'), false);
});

check("usage measures reject inherited names from the URL and storage", () => {
  for (const text of ["constructor", "toString", "__proto__", "hasOwnProperty"]) {
    for (const fromUrl of [true, false]) {
      const w = world({ URLSearchParams, location: {search: fromUrl ? `?measure=${text}` : ""},
        localStorage: {getItem: () => fromUrl ? "api" : text} });
      w.run('const percent = () => {}, dollars = () => {};');
      w.run(part("usage.js", "const MEASURES =", "const ui ="));
      w.run(part("usage.js", "function measureChosen()", "function fill("));
      assert.equal(w.run("measureChosen()"), fromUrl ? "api" : "week");
    }
  }
});

check("duplicate Delete shares one request, including across column redraws", async () => {
  const held = gate();
  let requests = 0;
  const w = commentsWorld(() => { requests += 1; return held.promise; });
  // A rebuilt column has its own comment list but shares the outstanding request.
  w.run('let commentsAgain = [{id:1},{id:2}];');
  w.run(part("page-comments.js", "    async function remove(comment)", "    function show(comment)")
    .replace("function remove(", "function removeAgain(")
    .replace("comments = comments.filter", "commentsAgain = commentsAgain.filter")
    .replace("onChange(comments)", "onChange(commentsAgain)"));
  const first = w.run("remove({id:1})"), second = w.run("removeAgain({id:1})");
  const waiting = w.run("settled(null)");
  assert.equal(requests, 1);
  held.resolve({});
  await Promise.all([first, second, waiting]);
  assert.equal(w.run("deletions.size"), 0);
  assert.equal(w.run("comments.some(c => c.id === 1)"), false);
  assert.equal(w.run("commentsAgain.some(c => c.id === 1)"), false);
  await w.run("settled(null)");
});

check("Delete 404 clears pending state and a failed Delete can be retried", async () => {
  for (const status of [404, 409]) {
    const w = commentsWorld(async () => { throw Object.assign(new Error("delete refused"), { status }); });
    await w.run("remove({id:1})");
    if (status === 409) {
      await assert.rejects(w.run("settled(null)"), /delete refused/);
      w.context.write = async () => ({});
      await w.run("remove({id:1})");
    }
    await w.run("settled(null)");
    assert.equal(w.run("deletions.size"), 0);
    assert.equal(w.run("comments.some(c => c.id === 1)"), false);
  }
});

for (const action of ["run", "allow"]) {
  check(`${action}: Take back waits for the press and skips a refused press`, async () => {
    for (const refused of [false, true]) {
      const held = gate(), requests = [];
      const ui = { busy: new Set(), inFlight: new Map(), removedByUser: new Map(), leftAt: new Map(),
        state: {}, sendCount: 0, sent: new Map() };
      const w = world({ ui, document: { querySelector: () => null },
        followQueue: noop, flightStart: noop, flyToAgents: noop, holdLeave: noop,
        refresh: async () => {}, toast: noop, choicesSaved: async () => {}, afterTimerPresses: async () => {},
        backKey: () => "reply", actToast: noop, tookBack: noop, refused: (_text, error) => error,
        ACTION_REFUSALS: { return: "Not taken back" },
        optimistic: async (apply, work) => work(),
        api: async (_method, url) => { requests.push(url); await held.promise; return {}; },
        apiWritten: async (_method, url) => { requests.push(url); return { stopped: true }; },
      });
      w.run(part("app.js", "async function run(key, work, success)", "// Optimistic changes:"));
      w.run(part("app.js", "function act(step, action, body", "  actToast(step, action, body, acting, outcome);") + "return acting; }");
      w.run(part("app.js", "function pressStepButton(", "function runCommand("));
      w.run('const step = {id:7,target:{session_id:"session"}};');
      const press = w.run(`pressStepButton(step, "${action}", {}, "Go", "Refused", null, null)`);
      await tick();
      const back = w.run('act(step,"return")');
      await tick();
      assert.deepEqual(requests, [`/api/steps/7/${action}`], "Take back overtook the press");
      if (refused) held.reject(new Error("press refused")); else held.resolve({});
      await Promise.all([press, back]);
      assert.deepEqual(requests, refused ? [`/api/steps/7/${action}`] : [`/api/steps/7/${action}`, "/api/steps/7/return"]);
      assert.equal(ui.inFlight.size, 0);
    }
  });
}

check("lightbox pauses page shortcuts and Escape closes it", () => {
  const lightbox = { hidden: false }, actions = [];
  let keydown;
  const w = world({
    document: {
      addEventListener: (type, handler) => { assert.equal(type, "keydown"); keydown = handler; },
      querySelector: (selector) => selector.startsWith("[data-key=") ? {} : null,
      getElementById: (id) => id === "lightbox" ? lightbox : { getBoundingClientRect: () => ({ top: 100, bottom: 400 }) },
    },
    ui: { next: { id: 7 } },
    timeDialog: () => ({ open: false }),
    closeLightbox: () => { lightbox.hidden = true; },
    openAddBox: () => actions.push("add"),
    openAddWithPart: () => actions.push("feedback"),
    update: () => actions.push("update"),
    nextStepAction: (key) => actions.push(key),
    focusDraft: () => actions.push("reply"),
    window: { innerHeight: 800, getSelection: () => null },
    header: { getBoundingClientRect: () => ({ bottom: 50 }) },
    catchingUp: () => false,
  });
  w.run(part("app.js", 'document.addEventListener("keydown", (event) => {', 'for (const type of ["pointerdown", "focusin"])'));
  const press = (key) => keydown({ key, target: {}, preventDefault: noop });
  for (const key of ["x", "e", "z", "r", "a", "f", "u"]) {
    press(key);
    assert.deepEqual(actions, [], `${key} acted behind the lightbox`);
    assert.equal(lightbox.hidden, false);
  }
  press("Escape");
  assert.equal(lightbox.hidden, true);
  assert.deepEqual(actions, []);
  press("x");
  assert.deepEqual(actions, ["x"], "step shortcuts did not resume after closing the lightbox");
});

check("closed-work search retries a failed term and caches a successful empty result", async () => {
  let requests = 0, callback;
  const w = world({ clearTimeout: noop, setTimeout: (fn) => { callback = fn; },
    AbortSignal, closedSwitch: { checked: true }, status: { textContent: "" },
    dialog: { open: false }, mode: "commands", query: "report",
    fetch: async () => { requests += 1; return { ok: requests > 1, status: 503, json: async () => ({ items: [] }) }; },
  });
  w.run(part("keyboard.js", "  let closedFound =", "  function paint("));
  w.run("loadClosed()"); await callback();
  assert.match(w.context.status.textContent, /could not be searched/);
  callback = null;
  w.run("loadClosed()");
  assert.ok(callback, "failed query was cached");
  await callback();
  assert.equal(requests, 2);
  callback = null;
  w.run("loadClosed()");
  assert.equal(callback, null, "successful empty result was not cached");
});

(async () => {
  let failures = 0;
  for (const [name, body] of tests) {
    try { await body(); console.log(`PASS ${name}`); }
    catch (error) { failures += 1; console.error(`FAIL ${name}: ${error.stack}`); }
  }
  assert.equal(failures, 0);
})().catch((error) => { console.error(error); process.exitCode = 1; });
