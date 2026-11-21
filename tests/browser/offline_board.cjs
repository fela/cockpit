"use strict";
const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const { startPreview } = require("./work.cjs");
(async () => {
  const { work, info } = await startPreview("cockpit-offline-board-");
  const browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE });
  try {
    for (const width of [1280,390]) {
      const context = await browser.newContext({ viewport:{width,height:900}, serviceWorkers:"allow" });
      await context.addCookies([{name:info.name,value:info.value,url:info.origin.replace("http:","https:"),secure:true}]);
      const page = await context.newPage(); page.setDefaultTimeout(25000); console.log(`starting ${width}`);
      const errors=[];page.on("pageerror",e=>{errors.push(e.message);console.error("PAGE",e.message);}); page.on("console",m=>{if(m.type()==="error")console.error(m.text());});
      await page.goto(info.origin); console.log("loaded online");
      await page.waitForFunction(()=>Boolean(ui.state));console.log("state drawn");
      await page.waitForFunction(()=>window.CockpitOffline?.status().shellReady,{},{timeout:45000});
      await page.waitForFunction(async()=>Boolean(await CockpitOffline.peek("/api/state")));
      const original = await page.evaluate(()=>({ ids:ui.state.items.map(x=>x.id), now:ui.state.now }));
      assert(original.ids.length>0);
      console.log("shell saved");await context.setOffline(true);
      await page.reload({waitUntil:"domcontentloaded"});
      await page.waitForFunction(()=>Boolean(ui.state && ui.savedState));
      assert.deepEqual(await page.evaluate(()=>ui.state.items.map(x=>x.id)),original.ids);
      assert.equal(await page.evaluate(()=>ui.lastOk),null,"cached board must not claim a successful current read");
      assert.equal(await page.evaluate(()=>ui.clockOffset),0,"cached server clock must not shift current timers");
      assert.equal(await page.evaluate(()=>ui.state.timer),null,"cache cannot resurrect a timer");
      const sensitive = await page.evaluate(async()=>{
        const before=localStorage.length;
        try {await api("POST","/api/permissions/requests/146/allow",{list:"allow"});return {accepted:true};}
        catch(e){return {message:e.message,unchanged:before===localStorage.length};}
      });
      assert.match(sensitive.message,/connect|connection|online/i);
      assert(sensitive.unchanged,"offline grants must never enter local outbox");
      await page.evaluate(()=>{ui.stateAskedAt=1;startCatchUp();veilCatchUp();});
      assert.equal(await page.locator("#next").evaluate(el=>el.inert),false,"slow catchup leaves navigation usable");
      await page.screenshot({path:path.join(work,`offline-${width}.png`),fullPage:true});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
      await context.setOffline(false);
      await page.evaluate(()=>refresh());
      await page.waitForFunction(()=>!ui.savedState && Boolean(ui.lastOk));
      assert.deepEqual(errors,[]);
      await context.close();
      console.log(`pass ${width}: true offline reload, safe stale clock, no queued grants, usable navigation, reconnect`);
    }
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
