// Refused feedback keeps its own part (regression check), on the Cockpit page (app.js) and on Usage (feedback.js): feedback
// sent on one part and refused while the user writes on another part waits; the open box keeps its own words and
// part, and the refused words come back on their own part once that box is sent. Kept words of a closed box hold
// the refused ones back too, and a Good writing note refused the same way waits as well. Words a page the user left
// did not send wait the same way behind kept words, and waiting words outlive leaving the page (to sign in).
//
// Usage, from the checkout: node tests/browser/feedback_waits.cjs   (it starts serve_preview.py)

const path = require("node:path");
const { startPreview } = require("./work.cjs");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;
const { chromium } = require(PLAYWRIGHT);

let failures = 0;
function check(ok, words) {
  console.log(`${ok ? "ok  " : "FAIL"} ${words}`);
  if (!ok) failures += 1;
}

const wait = (ms) => new Promise((done) => setTimeout(done, ms));

// Picks the element `selector` in feedback mode. A click on the spot of the last pick takes its next guess (pick.js),
// so the second pick of a part may land on a part around it.
async function pick(page, selector) {
  await page.keyboard.press("Escape");
  await page.evaluate(() => document.activeElement && document.activeElement.blur());
  // The main page picks for feedback through its own call; feedback.js, on the other pages, through its key.
  await page.evaluate(() => typeof startPicking === "function" ? startPicking("feedback") : document.getElementById("feedback-mode").click());
  await page.locator(selector).first().click();
  await page.waitForSelector("#pick-box:not([hidden]) textarea");
}

const box = (page) => page.evaluate(() => ({
  open: !document.getElementById("pick-box").hidden,
  target: (document.querySelector("#pick-box .pick-head .meta") || {}).textContent || "",
  text: (document.querySelector("#pick-box textarea") || {}).value || "",
}));

// Holds each feedback or writing request until the check lets it go, then refuses it if `refuse()` held when it came,
// else lets it through. Gives back `answer(count)`: once `count` requests wait, it answers them and gives the page a
// moment to take the answers in, so the user's words and picks land before the answer however slow the browser is.
async function holdRequests(page, refuse) {
  let waiting = [];
  for (const url of ["**/api/feedback", "**/api/writing"]) {
    await page.route(url, async (route) => {
      if (route.request().method() !== "POST") { await route.continue(); return; }
      const no = refuse();
      await new Promise((go) => waiting.push(go));
      if (no) await route.fulfill({ status: 422, contentType: "application/json", body: JSON.stringify({ detail: "refused for the test" }) });
      else await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ id: 1, ok: true }) });
    });
  }
  return async (count) => {
    for (const end = Date.now() + 15e3; waiting.length < count && Date.now() < end;) await wait(100);
    if (waiting.length !== count) throw new Error(`${waiting.length} feedback requests wait, not ${count}`);
    const go = waiting;
    waiting = [];
    go.forEach((release) => release());
    await wait(800);
  };
}

async function run(page, origin, where, [partA, partB]) {
  await page.goto(origin + where, { waitUntil: "networkidle" });
  let refusing = true;
  let answer = await holdRequests(page, () => refusing);

  // Feedback on part A is refused while the user writes on part B.
  await pick(page, partA);
  const a = (await box(page)).target;
  await page.keyboard.type("words about A");
  await page.keyboard.press("Control+Enter");
  await pick(page, partB);
  const b = (await box(page)).target;
  check(a !== b, `${where}: two different parts picked (${a} / ${b})`);
  await page.keyboard.type("words about B");
  await answer(1);
  let now = await box(page);
  check(now.open && now.target === b && now.text === "words about B", `${where}: the open box keeps part B and only its words after A's refusal`);

  // Sending B brings A back on its own part, with its own words; B's send then goes through.
  refusing = false;
  await page.keyboard.press("Control+Enter");
  await wait(300);
  now = await box(page);
  check(now.open && now.target === a && now.text === "words about A", `${where}: after B is sent, A's box is back on part A with its words`);

  // Closed with kept words, the box holds a refused Good writing note back until it is empty.
  await answer(1);
  refusing = true;
  await page.locator("#pick-box textarea").fill("a note on A");
  await page.locator("#pick-box").getByRole("button", { name: "Good writing" }).click();
  await pick(page, partB);
  await page.keyboard.type("kept words");
  await page.keyboard.press("Escape");
  await answer(1);
  now = await box(page);
  check(!now.open, `${where}: no box opens over the kept words of a closed box`);
  await pick(page, partB);
  now = await box(page);
  check(now.target !== a && now.text === "kept words", `${where}: the next pick holds only the kept words`);
  await page.locator("#pick-box textarea").fill("");
  await page.locator("#pick-box textarea").dispatchEvent("input");
  await page.keyboard.press("Escape");
  await wait(300);
  now = await box(page);
  check(now.open && now.target === a && now.text === "a note on A", `${where}: closed empty, the refused note is back on part A`);
  await page.unroute("**/api/feedback");
  await page.unroute("**/api/writing");

  // Words a page the user left did not send (errors.js) wait behind kept words too, and once the box is closed
  // empty they go into it: they name no part, so the next pick holds them.
  await page.locator("#pick-box textarea").fill("");
  await page.locator("#pick-box textarea").dispatchEvent("input");
  await page.keyboard.press("Escape");
  await pick(page, partB);
  await page.keyboard.type("kept words");
  await page.keyboard.press("Escape");
  await page.evaluate(({ store, key }) => dispatchEvent(new CustomEvent("cockpit:not-sent", { cancelable: true, detail: {
    words: { store, key, text: "words from the page left" }, message: "Not sent", reason: "refused for the test",
  } })), stores[where]);
  await pick(page, partB);
  now = await box(page);
  check(now.text === "kept words", `${where}: words from a page left wait behind the kept words`);
  await page.locator("#pick-box textarea").fill("");
  await page.locator("#pick-box textarea").dispatchEvent("input");
  await page.keyboard.press("Escape");
  await wait(300);
  await pick(page, partA);
  now = await box(page);
  check(now.text === "words from the page left", `${where}: closed empty, the next pick holds the words from the page left`);

  // Refused words that wait behind an open box outlive leaving the page, as the sign-in after any refused request
  // does: they are in the stored box on the next load.
  await page.locator("#pick-box textarea").fill("");
  await page.locator("#pick-box textarea").dispatchEvent("input");
  await page.keyboard.press("Escape");
  refusing = true;
  answer = await holdRequests(page, () => refusing);
  await pick(page, partA);
  await page.keyboard.type("waiting at sign-in");
  await page.keyboard.press("Control+Enter");
  await pick(page, partB);
  await page.keyboard.type("open at sign-in");
  await answer(1);
  now = await box(page);
  check(now.text === "open at sign-in", `${where}: the refused words wait behind the open box before the sign-in`);
  // The page then leaves for another site, as the sign-in does (Google's). With no requests routed, Chromium keeps
  // the page in its back-forward cache: back from it, the page is the one left, the open box holds only its own
  // words, and what is typed in it keeps the waiting words waiting.
  await page.unroute("**/api/feedback");
  await page.unroute("**/api/writing");
  await page.evaluate(() => addEventListener("pageshow", (event) => { window.restoredFromCache = event.persisted; }));
  await page.goto(elsewhere(origin));
  // Another tab writes the box while this page is away; coming back leaves its words in storage.
  const other = await page.context().newPage();
  await other.goto(origin + where, { waitUntil: "networkidle" });
  await pick(other, partB);
  await other.keyboard.type(" edited in another tab");
  await wait(300);
  await page.goBack({ waitUntil: "commit" });
  await wait(500);
  check((await storedBox(page, where)).includes("edited in another tab"), `${where}: back from the cache, another tab's words stay in storage`);
  await other.close();
  check(await page.evaluate(() => window.restoredFromCache === true), `${where}: Back restores the page from the back-forward cache`);
  now = await box(page);
  check(now.open && now.text === "open at sign-in", `${where}: back from the cache, the open box holds only its own words`);
  await page.locator("#pick-box textarea").press("End");
  await page.keyboard.type(" and more");
  await page.goto(elsewhere(origin));
  await page.goto(origin + where, { waitUntil: "networkidle" });
  const stored = await storedBox(page, where);
  check(stored.includes("waiting at sign-in"), `${where}: words that waited at the sign-in are in the stored box after it`);
  await page.evaluate(({ store }) => localStorage.removeItem(store), stores[where]);
}

// The words of the page's stored feedback box.
const storedBox = (page, where) => page.evaluate(({ store, key }) => {
  const kept = localStorage.getItem(store) || "";
  return key ? (new Map(JSON.parse(kept || "[]")).get(key) || "") : kept;
}, stores[where]);

// Another site than the preview's, to leave for: the same server under another host name.
const elsewhere = (origin) => `${origin.replace("127.0.0.1", "localhost")}/static/icon.svg`;

// Where each page keeps its feedback box: app.js keeps every box's draft in one stored list, under PICK_KEY;
// feedback.js keeps its one box under its own key.
const stores = {
  "/classic": { store: "cockpit.drafts", key: "fb:pick" },
  "/usage": { store: "cockpit:feedback:/usage", key: null },
};

(async () => {
  const { info: cookie } = await startPreview("cockpit-feedback-waits-");
  // Playwright turns the back-forward cache off unless asked.
  const browser = await chromium.launch({ executablePath: CHROMIUM, ignoreDefaultArgs: ["--disable-back-forward-cache"] });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await context.setExtraHTTPHeaders({ cookie: `${cookie.name}=${cookie.value}` });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (err) => errors.push(String(err)));
  await run(page, cookie.origin, "/classic", ["#goals .goals-box", "#next h1"]);
  await run(page, cookie.origin, "/usage", ["h1", ".spend-periods button[data-period='7']"]);
  check(errors.length === 0, `no page errors${errors.length ? `: ${errors.join("; ")}` : ""}`);
  await browser.close();
  process.exit(failures ? 1 : 0);
})();
