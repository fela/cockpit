// Activity labels stay visible on desktop and phone; blocked work belongs in Waiting.
"use strict";
const { spawn } = require("node:child_process");
const fs = require("node:fs"), path = require("node:path"), assert = require("node:assert/strict");
const { workFolder } = require("./work.cjs");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const work = workFolder("cockpit-busy-reasons-");
const blocked = "33333333-3333-4333-8333-333333333333";
fs.writeFileSync(path.join(work, "sessions.json"), JSON.stringify([{
 id:blocked.slice(0,8), session_id:blocked, name:"Blocked test", state:"blocked", pid:103,
 status:"idle", tempo:"blocked", job_state:"blocked", in_flight:[], cwd:work,
 detail:"API Error 429: rate_limit_error", needs:"rate limited", intent:"",
 updated_at:new Date().toISOString(), links:[], effort:"high", version:"test", account:"personal", provider:"claude"
}]));
const server=spawn(path.join(__dirname,"../../.venv/bin/python"),[path.join(__dirname,"serve_preview.py"),work],{stdio:["ignore","ignore","inherit"]});
const pause=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{
 let browser;
 try {
  const file=path.join(work,"cookie.json");
  for(let n=0;!fs.existsSync(file)&&n<120;n++){if(server.exitCode!==null)throw Error("fixture exited");await pause(250);}
  const info=JSON.parse(fs.readFileSync(file,"utf8")), cookie=`${info.name}=${info.value}`;
  const headers={authorization:`Bearer ${"t".repeat(40)}`,"x-cockpit-as":"browser-check","content-type":"application/json"};
  for(const [key,title,sid] of [["working","Prepare weekly reminders","22222222-2222-4222-8222-222222222222"],["blocked","Build list exports",blocked]]){
   const r=await fetch(`${info.origin}/api/items`,{method:"PUT",headers,body:JSON.stringify({key:`note:${key}`,title,stage:"building",sessions:[sid]})}); assert(r.ok,await r.text());
  }
  browser=await chromium.launch({executablePath:process.env.BROWSER_EXECUTABLE});
  for(const width of [1280,390]){
   const context=await browser.newContext({viewport:{width,height:900},colorScheme:"light",extraHTTPHeaders:{cookie}});
   const page=await context.newPage(), errors=[];page.on("pageerror",e=>errors.push(e.message));
   await page.addLocatorHandler(page.locator("dialog#idle[open]"),d=>d.locator("button",{hasText:"Return"}).click());
   await page.goto(info.origin);const busy=page.locator("#sidebar .sidebar-group.busy");await busy.waitFor({state:"attached"});
   if(width===390&&!await busy.isVisible())await page.locator(".phone-back").click();
   for(const name of ["busy","waiting"]){const g=page.locator(`#sidebar .sidebar-group.${name}`);if(!await g.evaluate(e=>e.open))await g.locator(":scope > summary").click();}
   await page.locator("#sidebar .sidebar-activity",{hasText:"Working now"}).first().waitFor();
   await page.locator("#sidebar .sidebar-group.waiting .sidebar-activity",{hasText:"Claude rate limit"}).waitFor();
   assert.equal(await page.locator("#sidebar .sidebar-group.busy .sidebar-activity",{hasText:"Claude rate limit"}).count(),0);
   assert.deepEqual(errors,[]);assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),"page overflows");
   await page.screenshot({path:path.join(work,`busy-reasons-${width}.png`)});await context.close();
  }
  console.log("Activity labels and blocked placement passed at desktop and phone widths. Screenshots:",work);
 }finally{if(browser)await browser.close();server.kill();}
})().catch(e=>{console.error(e);process.exitCode=1;});
