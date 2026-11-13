// A browser test's work folder under the temporary folder, cleared when the test's process ends: after a pass, a
// failure, an uncaught error, Ctrl-C, SIGTERM or SIGHUP. The screenshots (*.png) at its top level stay for the
// session to look at; everything else goes, chiefly the temporary database that serve_slow.py
// puts there, and the folder goes too when it held no screenshot. This keeps repeated runs from accumulating databases
"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const folders = [];

function workFolder(prefix) {
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), prefix));
  folders.push(folder);
  return folder;
}

function clear(folder) {
  let names;
  try {
    names = fs.readdirSync(folder);
  } catch (error) {
    // A test may end before it made the folder's contents, or after the server it started removed the folder (parent_watch.py).
    if (error.code === "ENOENT") return;
    throw error;
  }
  const kept = names.filter((name) => {
    if (name.endsWith(".png")) return true;
    fs.rmSync(path.join(folder, name), { recursive: true, force: true });
    return false;
  });
  if (kept.length) console.error(`screenshots kept in ${folder}`);
  // Recursive, so a file the server wrote meanwhile goes too.
  else fs.rmSync(folder, { recursive: true, force: true });
}

// The server a test started stops by itself once the test's process is gone (parent_watch.py), so the immediate exit
// below needs no test's own cleanup to stop it.
process.on("exit", () => {
  for (const folder of folders.splice(0)) {
    try {
      clear(folder);
    } catch (error) {
      console.error(`could not clear ${folder}: ${error.message}`);
      process.exitCode = 1;
    }
  }
});
// A handler replaces the signal's default exit, so each exits with the code the default would give, which runs the
// exit handler above.
for (const [signal, code] of [["SIGINT", 130], ["SIGTERM", 143], ["SIGHUP", 129]]) {
  process.on(signal, () => process.exit(code));
}

// Starts serve_preview.py (this checkout's Cockpit on a synthetic database, fake sessions) in a new work folder
// and gives back that folder and the harness's cookie file ({name, value, origin}) once the server answers (it waits up to
// three minutes, since a machine busy with other work starts the server slowly). The server
// stops with the test's process (parent_watch.py), and does not keep that process running once the test is done.
async function startPreview(prefix) {
  const work = workFolder(prefix);
  const python = path.join(__dirname, "..", "..", ".venv", "bin", "python");
  const server = spawn(python, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  server.unref();
  const file = path.join(work, "cookie.json");
  for (const end = Date.now() + 180e3; Date.now() < end; await new Promise((done) => setTimeout(done, 200))) {
    if (server.exitCode !== null) throw new Error(`serve_preview.py exited with ${server.exitCode}`);
    if (!fs.existsSync(file)) continue;
    const info = JSON.parse(fs.readFileSync(file, "utf8"));
    if (await fetch(`${info.origin}/healthz`).then((response) => response.ok, () => false)) return { work, info };
  }
  throw new Error("serve_preview.py did not come up within 180 s");
}

// A screenshot of the node `locator` finds. A poll can redraw the node while the shot is taken (Playwright then says it is
// not attached); the next try finds the node drawn anew.
async function screenshotRedrawn(locator, options) {
  for (let tries = 1; ; tries++) {
    try {
      return await locator.screenshot(options);
    } catch (error) {
      if (tries === 3 || !/not attached/.test(error.message)) throw error;
    }
  }
}

// A test that rewrites state needs a complete response. Its edited JSON has no server checkpoint,
// so the next real read must also be complete when the interception ends.
async function fullState(route) {
  const response = await route.fetch({ headers: { ...route.request().headers(), "x-cockpit-since": "" } });
  return { response, headers: { ...response.headers(), "x-cockpit-state": "" } };
}

module.exports = { workFolder, startPreview, screenshotRedrawn, fullState };
