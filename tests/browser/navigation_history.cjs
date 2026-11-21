"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const { startPreview } = require("./work.cjs");

(async () => {
  const { info } = await startPreview("cockpit-navigation-");
  const browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE });
  try {
    for (const width of [1440, 390]) {
      const context = await browser.newContext({ viewport: { width, height: 900 }, reducedMotion: "reduce" });
      await context.addCookies([{ name: info.name, value: info.value, url: info.origin.replace("http:", "https:"), secure: true }]);
      const page = await context.newPage();
      await page.goto(info.origin);
      await page.waitForFunction(() => Boolean(ui.state) && cockpitNavigation.initialized);
      const install = () => page.evaluate(() => {
        for (const name of ["navigation:notes", "navigation:files"]) registerCenterView(name, {
          title: name.endsWith("notes") ? "Notes" : "Files",
          draw(tools, body, state) {
            const date = h("input", { type: "date", "aria-label": "Navigation date", value: state.day || "2026-11-17" });
            const note = h("input", { "aria-label": "Navigation draft", value: state.draft || "" });
            const remember = () => centerSetViewState({ day: date.value, draft: note.value });
            date.oninput = remember; note.oninput = remember;
            body.append(date, note, ...Array.from({ length: 60 }, (_, index) => h("p", { text: `A line of the selected view ${index}` })));
          },
        });
      });
      await install();
      const ids = await page.evaluate(() => ui.state.items.slice(0, 2).map((item) => item.id));
      assert.equal(ids.length, 2);
      await page.evaluate((id) => selectItem(id), ids[0]);
      await page.evaluate(() => openCenter("navigation:notes"));
      await page.getByLabel("Navigation date").fill("2026-11-15");
      await page.getByLabel("Navigation draft").fill("Private unsaved note");
      await page.evaluate(() => { centerParts().body.scrollTop = 350; });
      assert.ok(!(await page.evaluate(() => JSON.stringify(history.state))).includes("Private unsaved note"));
      await page.evaluate(() => openCenter("navigation:files"));
      await page.evaluate((id) => selectItem(id), ids[1]);
      assert.equal(await page.evaluate(() => center.view), null);
      assert.equal(await page.evaluate(() => selectedItem()), ids[1]);
      await page.goBack(); await page.waitForFunction(() => center.view === "navigation:files");
      await page.goBack(); await page.waitForFunction(() => center.view === "navigation:notes");
      assert.equal(await page.getByLabel("Navigation date").inputValue(), "2026-11-15");
      assert.equal(await page.getByLabel("Navigation draft").inputValue(), "Private unsaved note");
      await page.waitForFunction(() => centerParts().body.scrollTop >= 350);
      await page.goBack(); await page.waitForFunction((id) => !center.view && selectedItem() === id, ids[0]);
      await page.goForward(); await page.waitForFunction(() => center.view === "navigation:notes");
      await page.evaluate((id) => selectItem(id), ids[1]);
      const bar = width === 390 ? ".phone-bar" : ".rail-top";
      assert.equal(await page.locator(`${bar} > .destination-navigation .navigation-forward`).isDisabled(), true);
      assert.equal(await page.locator("#next .destination-navigation").count(), 0, "The task's title stands without arrows beside it");
      console.log(`pass ${width}: tasks and views restore through Back/Forward, including date, memory draft and scroll; new navigation truncates the branch`);

      await page.evaluate(() => openCenterHelper("/pages?navigation=check#details"));
      await page.locator("iframe.center-page").waitFor();
      // Beside the rail an opened view leaves the rail's arrows in view; on a phone it covers the bar and shows its own.
      assert.equal(await page.locator(".center-head .navigation-back").isVisible(), width === 390);
      if (width !== 390) assert.equal(await page.locator(".rail-top .navigation-back").isVisible(), true);
      await page.evaluate((id) => selectItem(id), ids[0]);
      await page.goBack(); await page.waitForFunction(() => center.view === "/pages");
      assert.match(await page.locator("iframe.center-page").getAttribute("src"), /navigation=check.*#details/);
      await page.evaluate(() => closeCenter(true));
      assert.equal(await page.evaluate(() => center.view), null);
      await page.goBack(); await page.waitForFunction(() => center.view === "/pages");
      console.log(`pass ${width}: helper query/fragment survive navigation; Close returns to the task and Back reopens the helper`);

      await page.evaluate((id) => selectItem(id), ids[1]);
      await page.goBack(); await page.waitForFunction(() => center.view === "/pages");
      await page.reload(); await page.waitForFunction(() => center.view === "/pages");
      assert.equal(await page.locator(".center-head .navigation-forward").isDisabled(), false);
      await page.goForward(); await page.waitForFunction((id) => !center.view && selectedItem() === id, ids[1]);
      console.log(`pass ${width}: reload preserves a known forward destination`);

      await page.evaluate(() => {
        registerCenterView("navigation:sensitive", { title: "Private frame", draw(tools, body) {
          const frame = h("iframe", { class: "center-page", title: "Private frame" });
          Object.defineProperty(frame, "contentWindow", { get() { throw new Error("A private frame URL was inspected"); } });
          body.append(frame);
        } });
        openCenter("navigation:sensitive");
      });
      await page.evaluate((id) => selectItem(id), ids[0]);
      assert.equal(await page.evaluate(() => center.view), null);
      await page.evaluate(() => openCenterHelper("/pages?navigation=check#details"));
      await page.locator("iframe.center-page").waitFor();
      await page.evaluate(() => Object.defineProperty(document.querySelector("iframe.center-page"), "contentWindow", {
        get() { throw new DOMException("A redirected helper has another origin", "SecurityError"); },
      }));
      await page.evaluate((id) => selectItem(id), ids[1]);
      assert.equal(await page.evaluate(() => center.view), null);
      console.log(`pass ${width}: leaving private or redirected frames reads no private URL and tolerates a cross-origin helper`);

      const sessionId = await page.evaluate(() => ui.state.sessions[0].session_id);
      for (const next of ["forward", "task", "center"]) {
        await page.evaluate((id) => selectItem(id), ids[0]);
        await page.evaluate(({ sessionId, itemId }) => openSessionView(sessionId, itemId), { sessionId, itemId: ids[0] });
        await page.evaluate((id) => selectItem(id), ids[1]);
        await page.evaluate(() => {
          window.originalNavigationTransition = document.startViewTransition;
          document.startViewTransition = (change) => {
            let finish;
            const done = new Promise((resolve) => { finish = resolve; });
            window.releaseNavigationTransition = async () => { await change(); finish(); };
            return { ready: Promise.resolve(), finished: done, updateCallbackDone: done };
          };
        });
        await page.goBack(); await page.waitForFunction(() => Boolean(window.releaseNavigationTransition) && cockpitNavigation.restoring);
        if (next === "forward") await page.goForward();
        else if (next === "task") await page.evaluate((id) => selectItem(id), ids[1]);
        else await page.evaluate(() => openCenter("time"));
        await page.waitForFunction((next) => next === "center" ? center.view === "time" : sessionView.id === null && !cockpitNavigation.restoring, next);
        await page.evaluate(async () => {
          await window.releaseNavigationTransition();
          document.startViewTransition = window.originalNavigationTransition;
          delete window.releaseNavigationTransition;
        });
        const destination = await page.evaluate(() => ({ item: selectedItem(), session: sessionView.id, route: cockpitNavigation.current.route, view: center.view }));
        assert.equal(destination.session, null);
        if (next === "center") assert.equal(destination.view, "time");
        else { assert.equal(destination.item, ids[1]); assert.equal(destination.route.itemId, ids[1]); assert.equal(destination.route.sessionId, null); }
      }
      console.log(`pass ${width}: a delayed session restore cannot undo Forward, a new task, or a new center view`);

      if (width === 390) {
        await page.evaluate((id) => selectItem(id), ids[1]);
        await page.getByRole("button", { name: /^Your items/ }).click();
        await page.waitForFunction(() => phoneListShown());
        await page.goBack(); await page.waitForFunction(() => !phoneListShown());
        await page.goForward(); await page.waitForFunction(() => phoneListShown());
        await page.evaluate((id) => { phoneRestoring = false; selectItem(id); }, ids[0]);
        await page.waitForFunction(() => !phoneListShown());
        await page.goBack(); await page.waitForFunction(() => phoneListShown());
        await page.goForward(); await page.waitForFunction((id) => !phoneListShown() && selectedItem() === id, ids[0]);
        console.log("pass 390: Your items stays distinct; phone list/item Back/Forward retain their screen and task");
      }
      await page.evaluate((id) => {
        selectItem(id);
        const title = "Review the next set of changes and keep the unsaved answer while moving between work and notes";
        ui.state.items.find((item) => item.id === id).title = title;
        for (const step of [...ui.state.queue, ...ui.state.ready_next, ...ui.state.snoozed, ...ui.state.with_agents]) if (step.item_id === id) step.item.title = title;
        render(false);
      }, ids[0]);
      fs.mkdirSync("/tmp/cockpit-navigation-shots", { recursive: true });
      await page.waitForTimeout(500);
      await page.screenshot({ path: `/tmp/cockpit-navigation-shots/task-${width}.png` });
      const cachedTimer = await page.evaluate(() => {
        const saved = ui.savedState;
        ui.savedState = true; ui.lastInput = Date.now(); ui.autoStarted = null;
        startOpened(drawn(ui.state));
        ui.savedState = saved;
        return ui.autoStarted;
      });
      assert.equal(cachedTimer, null, "Restoring a saved task leaves automatic timer starts suppressed");
      await page.evaluate(() => { document.getElementById("next").replaceChildren(); renderNavigation(); });
      assert.equal(await page.locator(`${width === 390 ? ".phone-bar" : ".rail-top"} > .destination-navigation .navigation-back`).isVisible(), true);
      await page.waitForTimeout(150);
      await page.screenshot({ path: `/tmp/cockpit-navigation-shots/empty-${width}.png` });
      await page.evaluate(() => dispatchEvent(new CustomEvent("cockpit:offline-cleared", { detail: { scope: "another-login" } })));
      assert.ok(await page.evaluate(() => cockpitNavigation.entries.size > 0));
      await page.evaluate(() => dispatchEvent(new CustomEvent("cockpit:offline-cleared")));
      assert.equal(await page.evaluate(() => cockpitNavigation.entries.size), 0);
      await context.close();
    }
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
