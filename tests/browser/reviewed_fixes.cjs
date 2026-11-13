// Isolated browser checks for delayed saves, permission-rule ordering and unavailable machine readings.
// Requests are held and answered with page.route, as in feedback_waits.cjs; no live data or sign-in is used.
// Usage: node tests/browser/reviewed_fixes.cjs
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;
const staticDir = process.argv[2] || path.join(__dirname, "../../src/cockpit/static");
const source = (name) => fs.readFileSync(path.join(staticDir, name), "utf8");
const between = (text, start, end) => text.slice(text.indexOf(start), text.indexOf(end, text.indexOf(start)));

async function setup(browser) {
  const page = await browser.newPage();
  await page.route("http://127.0.0.1:8790/**", (route) => route.fulfill({ contentType: "text/html", body: "<!doctype html><title>Test</title><button>Act</button>" }));
  await page.goto("http://127.0.0.1:8790/");
  await page.addScriptTag({ content: source("errors.js") });
  await page.addScriptTag({ content: source("reply.js") });
  return page;
}

async function rules(browser) {
  const page = await setup(browser);
  const requests = [];
  let release;
  const held = new Promise((resolve) => { release = resolve; });
  const enabled = new Set();
  await page.route("**/api/permissions/rules**", async (route) => {
    const { rule, old } = route.request().postDataJSON();
    const action = route.request().url().split("/").pop();
    requests.push(action);
    if (requests.length === 1) await held;
    if (action === "rules") enabled.add(rule);
    else if (action === "remove") enabled.delete(rule);
    else { enabled.delete(old); enabled.add(rule); }
    await route.fulfill({ contentType: "application/json", body: JSON.stringify({ allow: [...enabled], auto: [], added: true }) });
  });
  await ruleFunctions(page);
  await page.evaluate(() => {
    window.added = changeRules("allow", "Bash(test)", "add", null, null);
    window.removed = changeRules("allow", "Bash(test)", "remove", null, null);
    window.changed = changeRuleBack("allow", "Bash(test)", "Bash(old)");
  });
  for (let i = 0; requests.length === 0 && i < 100; i++) await page.waitForTimeout(10);
  await page.waitForTimeout(150);
  assert.deepEqual(requests, ["rules"], "later presses wait for the delayed Add");
  release();
  await page.evaluate(() => Promise.all([added, removed, changed]));
  assert.deepEqual(requests, ["rules", "remove", "edit"], "all changes reach the server in press order");
  assert.deepEqual([...enabled], ["Bash(old)"]);
  assert.deepEqual(await page.evaluate(() => ui.permissions.allow), ["Bash(old)"]);
  await page.close();
  console.log("ok rule changes wait in press order, including change back");
}

async function ruleFunctions(page) {
  const app = source("app.js");
  await page.addScriptTag({ content: `
    const ui = { permissions: { allow: [], auto: [] }, sendCount: 0, busy: new Set(), drafts: new Map() };
    function renderPermissions() {} function refresh() {} function toast() {}
    async function api(method, path, body) {
      const response = await fetch(path, { method, headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
      if (!response.ok) throw new Error('refused');
      return response.json();
    }
    ${between(app, "async function run(key, work, success)", "// An undo pressed")}
    ${between(app, "let ruleChanges =", "function removeRule(")}
  ` });
}

async function refusedRules(browser) {
  for (const back of [false, true]) {
    const page = await setup(browser);
    let release;
    const held = new Promise((resolve) => { release = resolve; });
    const requests = [];
    await page.route("**/api/permissions/rules**", async (route) => {
      requests.push(route.request().url().split("/").pop());
      if (requests.length === 1) await held;
      await route.fulfill({ status: 409, contentType: "application/json", body: '{}' });
    });
    await ruleFunctions(page);
    const shown = await page.evaluate(async (back) => {
      window.first = changeRules("allow", "Bash(first)", "add", null, null);
      await Promise.resolve();
      const immediate = [...ui.permissions.allow];
      window.second = back ? changeRuleBack("allow", "Bash(first)", "Bash(old)")
        : changeRules("allow", "Bash(second)", "add", null, null);
      return immediate;
    }, back);
    assert.deepEqual(shown, ["Bash(first)"], "an unqueued change shows before the server answers");
    release();
    await page.evaluate(() => Promise.all([first, second]));
    assert.deepEqual(requests, ["rules", back ? "edit" : "rules"]);
    assert.deepEqual(await page.evaluate(() => ui.permissions.allow), [], "two refused changes restore the original rules");
    await page.close();
  }
  console.log("ok refused adds and change back each roll back to the preceding outcome");
}

async function preferences(browser) {
  for (const refused of [false, true]) {
    const page = await setup(browser);
    const requests = [];
    let release;
    const held = new Promise((resolve) => { release = resolve; });
    await page.route("**/api/accounts/prefer", async (route) => {
      requests.push(route.request().postDataJSON());
      if (requests.length === 1) await held;
      await route.fulfill({ status: refused && requests.length === 1 ? 409 : 200,
        contentType: "application/json", body: '{}' });
    });
    const app = source("app.js");
    await page.addScriptTag({ content: `
      const ui = { busy: new Set() };
      function toast() {} function refresh() {}
      async function api(method, path, body) {
        const response = await fetch(path, { method, body: JSON.stringify(body) });
        if (!response.ok) throw new Error('refused');
        return response.json();
      }
      ${between(app, "async function run(key, work, success)", "// An undo pressed")}
      ${between(app, "// The toast shows at once;", "function renderUsage(state)")}
    ` });
    await page.evaluate(() => { window.first = preferAccount("personal", 30, "30m"); });
    await page.waitForFunction(() => ui.busy.has("prefer"));
    await page.evaluate(() => {
      window.second = preferAccount("work", 120, "2h");
      window.third = preferAccount(null, 0, "");
    });
    for (let i = 0; requests.length === 0 && i < 100; i++) await page.waitForTimeout(10);
    assert.equal(requests.length, 1, "later choices wait for the first response");
    release();
    await page.evaluate(() => Promise.all([first, second, third]));
    assert.deepEqual(requests, [{ account: "personal", minutes: 30 }, { account: "work", minutes: 120 },
      { account: null, minutes: 0 }], "every choice reaches the server in press order, also after a refusal");
    await page.close();
  }
  console.log("ok header account choices wait in press order, including after a refusal");
}

async function saves(browser) {
  for (const name of ["spec", "skill"]) {
    const page = await setup(browser);
    let release;
    const held = new Promise((resolve) => { release = resolve; });
    await page.route("**/api/document", async (route) => {
      await held;
      await route.fulfill({ status: 409, contentType: "application/json", body: '{"detail":"document changed"}' });
    });
    const spec = source("spec.js");
    await page.addScriptTag({ content: `
      const ui = { busy: false, fingerprint: '', cellsNext: [], cellChoices: new Map() };
      const DOC = { name: '${name}' };
      function holdEditors() {} function render() {} function saveNextCell() {}
      function toast(text) { window.said = text; }
      window.reloadGate = new Promise((resolve) => { window.finishReload = resolve; });
      async function load() { await window.reloadGate; }
      ${between(spec, "function save(work, done)", "// The oldest waiting chip choice")}
      window.leavingWarns = () => !dispatchEvent(new Event('beforeunload', { cancelable: true }));
    ` });
    await page.evaluate(() => {
      window.saving = save(async () => {
        const response = await fetch('/api/document', { method: 'PUT', body: 'kept edit' });
        if (!response.ok) throw new Error('document changed');
        return response.json();
      }, () => { throw new Error('refused edits must stay open'); });
    });
    assert.equal(await page.evaluate(() => leavingWarns()), true, `${name}: warns while save waits`);
    release();
    await page.waitForFunction(() => window.said === 'document changed');
    assert.equal(await page.evaluate(() => leavingWarns()), true, `${name}: warns while refusal handling reloads`);
    await page.evaluate(() => { window.finishReload(); });
    await page.evaluate(() => window.saving);
    assert.equal(await page.evaluate(() => leavingWarns()), false, `${name}: resolved outcome releases leave hold`);
    await page.close();
  }
  console.log("ok spec and skill saves hold leaving through the refused outcome");
}

async function machine(browser) {
  const page = await setup(browser);
  const app = source("app.js");
  await page.addScriptTag({ content: between(app, "function machineLamp(machine)", "function machineStrain(") });
  assert.equal(await page.evaluate(() => machineLamp({ unavailable: "Load readings are not available on this system" })), null);
  await page.close();
  console.log("ok unavailable machine readings leave the rack out");
}

async function commentDeletions(browser) {
  for (const refused of [false, true]) {
    const page = await setup(browser);
    const comment = { id: 1, step_id: 7, step_open: true, state: "unsent", text: "Remove this comment",
      anchor: { kind: "text", quote: "words", prefix: "", suffix: "" } };
    let comments = [comment];
    let release;
    const held = new Promise((resolve) => { release = resolve; });
    let deleting = false;
    const handed = [];
    await page.route("**/api/**", async (route) => {
      const request = route.request();
      if (request.method() === "DELETE") {
        deleting = true;
        await held;
        if (refused) return route.fulfill({ status: 409, contentType: "application/json", body: '{"detail":"delete refused"}' });
        comments = [];
      } else if (request.url().endsWith("/hand-off")) {
        handed.push(comments.map((entry) => entry.id));
      }
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({ id: 9, comments }) });
    });
    await page.addScriptTag({ content: `
      const Icons = { icon: () => document.createElement('span') };
      function logError() {}
      let busy = false, replyError = '', sent = null;
      const TAKE_BACK_SHOWN_MS = 60000;
      const CockpitChoices = { settled: async () => {} };
      const theStep = () => ({ id: 7 });
      const stepTakes = () => true;
      const replyKey = () => 'reply';
      const savedReply = () => 'Keep these words';
      function keepReply() {} function notice() {} function release() {}
      function renderReply() { document.getElementById('reply').textContent = replyError; }
      async function reload() { renderReply(); }
      async function write(path, body) {
        const response = await fetch(path, { method: 'POST', body: JSON.stringify(body) });
        return response.json();
      }
      document.body.innerHTML = '<iframe></iframe><aside></aside><p id="reply" role="alert"></p>';
      ${between(source("pages.js"), "async function send()", "async function release()")}
    ` });
    await page.addScriptTag({ content: source("page-comments.js") });
    await page.evaluate(() => {
      window.column = PageComments.attach({ frame: document.querySelector('iframe'), page: { id: 3, revision: 1 },
        stepId: 7, column: document.querySelector('aside'), onPlace: null, onChange: () => {}, onStale: () => {},
        onUnsaved: () => {}, onPick: () => {}, carried: '', kept: null });
    });
    await page.getByRole("button", { name: "Delete comment" }).click();
    for (let i = 0; !deleting && i < 100; i++) await page.waitForTimeout(10);
    assert.equal(deleting, true);
    await page.evaluate(() => { window.sending = send(); });
    await page.waitForTimeout(100);
    assert.deepEqual(handed, [], "Send waits while Delete is pending");
    release();
    await page.evaluate(() => window.sending);
    if (refused) {
      assert.deepEqual(handed, [], "a refused deletion stops the hand-off");
      assert.match(await page.locator('#reply').textContent(), /Not sent: delete refused/);
      await page.evaluate(() => send());
      assert.deepEqual(handed, [], "the refusal also stops a later Send");
    } else {
      assert.deepEqual(handed, [[]], "the deleted comment is absent from the hand-off");
    }
    await page.close();
  }
  console.log("ok Pages Send waits for deletions and shows refused deletions in the reply area");
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROMIUM });
  try { await rules(browser); await refusedRules(browser); await preferences(browser); await saves(browser); await machine(browser); await commentDeletions(browser); }
  finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
