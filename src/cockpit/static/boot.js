"use strict";

// The main page asks for its first state and its permission rules here, before the stylesheets and the page's
// scripts load, so Cockpit builds the answer while the browser reads them (SPEC UI-39). app.js takes each
// answer once, for its first drawing (`bootOr`), and drops the rest after it. A failure reaches app.js
// there; until then it counts as handled, so it is not logged twice.
window.CockpitBoot = Object.fromEntries([["state", "/api/state"], ["permissions", "/api/permissions"]].map(([name, path]) => {
  const answer = readThroughRestart(path, { method: "GET", headers: {}, credentials: "same-origin" });
  answer.catch(() => {});
  return [name, answer];
}));
