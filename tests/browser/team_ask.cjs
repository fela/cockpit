// The first-use question for the team and the Briefing page's team section (SPEC VIEW-8), on a preview
// server whose database has no team yet: Cockpit asks, a poll keeps what the user typed and where they
// were typing, Save stores it and the question goes away, and the Briefing page shows it and saves a change.
//
// Usage, from the checkout: node tests/browser/team_ask.cjs   (it starts serve_preview.py and clears the fixture's team)

const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { startPreview } = require("./work.cjs");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;
const { chromium } = require(PLAYWRIGHT);

let failures = 0;
function check(ok, words) {
  console.log(`${ok ? "ok  " : "FAIL"} ${words}`);
  if (!ok) failures += 1;
}

(async () => {
  const { work, info: cookie } = await startPreview("cockpit-team-ask-");
  // The fixture supports team settings: this check starts as a first visit.
  execFileSync(path.join(__dirname, "../../.venv/bin/python"), ["-c",
    "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); c.execute(\"DELETE FROM kv WHERE key = 'team'\"); c.commit()",
    path.join(work, "cockpit.db")]);
  const browser = await chromium.launch({ executablePath: CHROMIUM });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await context.setExtraHTTPHeaders({ cookie: `${cookie.name}=${cookie.value}` });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (err) => errors.push(String(err)));

  await page.goto(cookie.origin + "/classic", { waitUntil: "networkidle" });
  const ask = page.locator(".team-ask");
  check(await ask.isVisible(), "Cockpit asks for the team while none is set");
  const field = ask.locator("textarea");
  const save = ask.getByRole("button", { name: "Save" });
  check(await save.isDisabled(), "Save waits for a name");
  await field.click();
  await field.type("Ana works on Tasks reminders");
  check(await save.isEnabled(), "Save is ready once a name is typed");
  // A poll that redraws draws the banner anew (it is not one of the sections a poll leaves alone while
  // the user types in them): the words and the focus stay.
  // A poll already on its way when the check asks runs this one after it, so the check waits for the redraw.
  await page.evaluate(() => { document.querySelector(".team-ask textarea").dataset.old = "1"; ui.pendingRender = true; return refresh(); });
  const redrawn = await page.waitForFunction(() => document.querySelector(".team-ask textarea:not([data-old])"), null, { timeout: 10e3 })
    .then(() => true, () => false);
  check(redrawn, "the banner was drawn anew");
  check(await page.locator(".team-ask textarea").inputValue() === "Ana works on Tasks reminders", "a redraw keeps the words");
  check(await page.evaluate(() => document.activeElement.dataset.key === "team-ask"), "a redraw keeps the focus in the box");
  await page.keyboard.type("\nAlex: list sharing");
  // While the save is on its way the box takes no words, which would go with the question once it is saved
  // (regression check).
  // The save is held until the check has read the box, so a slow browser cannot see it land first.
  let release;
  const held = new Promise((done) => { release = done; });
  await page.route("**/api/team", async (route) => { await held; await route.continue(); });
  await page.locator(".team-ask").getByRole("button", { name: "Save" }).click();
  check(await page.locator(".team-ask textarea").evaluate((area) => area.readOnly), "the box takes no words while its save is on its way");
  release();
  await page.waitForSelector(".team-ask", { state: "detached", timeout: 5000 });
  await page.unroute("**/api/team");
  check(true, "Save stores the team and the question goes away");
  const state = await page.evaluate(() => fetch("/api/state").then((r) => r.json()));
  check(state.team === "Ana works on Tasks reminders\nAlex: list sharing", "the state holds the team as typed");

  await page.goto(cookie.origin + "/briefing", { waitUntil: "networkidle" });
  const box = page.locator("#team textarea");
  check(await box.inputValue() === state.team, "the Briefing page shows the team");
  await box.fill("Alex: list sharing");
  await page.locator("#team").getByRole("button", { name: "Save" }).click();
  await page.waitForFunction(() => document.querySelector("#team [role=status]").textContent.startsWith("Saved"));
  const after = await page.evaluate(() => fetch("/api/briefings").then((r) => r.json()));
  check(after.team === "Alex: list sharing", "the Briefing page saves a change");
  // The confirming read and later polls keep the editor and its confirmation.
  await page.evaluate(() => load());
  check((await page.locator("#team [role=status]").textContent()).startsWith("Saved"), "the confirmation stays after a read");
  // Words typed while a save is under way stay in the box.
  await page.route("**/api/team", async (route) => { await new Promise((done) => setTimeout(done, 800)); await route.continue(); });
  await box.fill("Sam: exports");
  await page.locator("#team").getByRole("button", { name: "Save" }).click();
  await box.press("End");
  await box.type(", part time");
  await page.waitForFunction(() => document.querySelector("#team [role=status]").textContent.startsWith("Saved"));
  check(await box.inputValue() === "Sam: exports, part time", "words typed during a save stay");
  check(await page.locator("#team").getByRole("button", { name: "Save" }).isEnabled(), "Save is ready for those words");
  await page.unroute("**/api/team");
  // A read that started during a save, with the team from before it, and answered after: the box keeps
  // the saved team.
  await page.route("**/api/team", async (route) => { await new Promise((done) => setTimeout(done, 400)); await route.continue(); });
  await page.route("**/api/briefings", async (route) => {
    const response = await route.fetch();
    await new Promise((done) => setTimeout(done, 1200));
    await route.fulfill({ response });
  });
  await box.fill("Ana: reminders");
  await page.locator("#team").getByRole("button", { name: "Save" }).click();
  const staleRead = page.evaluate(() => load());
  await staleRead;
  check(await box.inputValue() === "Ana: reminders", "a read from before a save ended leaves the saved team");
  await page.unroute("**/api/team");
  await page.unroute("**/api/briefings");
  check(errors.length === 0, `no page errors${errors.length ? `: ${errors.join("; ")}` : ""}`);
  await browser.close();
  process.exit(failures ? 1 : 0);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
