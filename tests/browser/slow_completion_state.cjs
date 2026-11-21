// Mutation completion releases controls independently of a delayed reconciliation read.
"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm"),path=require("node:path");
const source=fs.readFileSync(path.join(__dirname,"../../src/cockpit/static/app.js"),"utf8");
const run=source.slice(source.indexOf("async function run(key, work, success) {"),source.indexOf("// An undo pressed while"));
(async()=>{
 let releaseWrite,releaseRead,reads=0;
 const writing=new Promise(r=>releaseWrite=r),reading=new Promise(r=>releaseRead=r);
 const ui={busy:new Set(),actionRender:false};
 const scope={ui,logError(){},toast(){},refresh(){reads++;return reading;}};
 vm.createContext(scope);vm.runInContext(run+"\nthis.run=run;",scope);
 let finished=false;
 const action=scope.run("row",()=>writing).then(value=>{finished=value;});
 assert(ui.busy.has("row"));releaseWrite();
 await new Promise(r=>setImmediate(r));
 assert.equal(finished,true,"acknowledged action must settle while state read is still held");
 assert(!ui.busy.has("row"));assert.equal(reads,1);assert(ui.actionRender);
 releaseRead();await action;
 console.log("PASS write acknowledgement releases row before delayed state refresh");
})().catch(e=>{console.error(e);process.exitCode=1;});
