"use strict";

// The installed app's launch (SPEC UI-80); launch.css draws it. When Cockpit opens as an installed app, the phone
// shows its own launch screen first: the icon on the manifest's background_color, graphite. This page takes over
// from it at its first paint with the same graphite and the same marker, so the hand-over does not show, and a glint
// runs along a line through the marker while the main page waits for its first state. Once the page has drawn
// (app.js calls `ready`), the screen splits open along the line at once; the launch never holds the page back.
// It shows only on a cold start of the installed app: not in a browser tab, not on a reload, not on a move between
// Cockpit's own pages. This file loads in the head without `defer`, so it decides before the first paint.
window.CockpitLaunch = (() => {
  // The split's length in launch.css, with a frame to spare.
  const OUT_MS = 300;
  // A page that never draws still opens after this long, onto its ghosts or its error.
  const GIVE_UP_MS = 8000;
  const GRAPHITE = "#141210";
  const LAUNCHED = "cockpit-launched";

  const root = document.documentElement;
  const coldStart = () => {
    const standalone = window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true;
    if (!standalone || document.visibilityState === "hidden") return false;
    const entry = performance.getEntriesByType("navigation")[0];
    if (entry && entry.type !== "navigate") return false;
    // Cockpit sends no referrer, so a move from another of its pages looks like a fresh open; the window's own
    // session storage tells them apart. An app launch opens a new window with empty storage, and moves within the
    // window keep it. Without storage the launch is skipped rather than replayed on every move.
    try {
      if (sessionStorage.getItem(LAUNCHED)) return false;
      sessionStorage.setItem(LAUNCHED, "1");
      return true;
    } catch {
      return false;
    }
  };
  if (!coldStart()) return { ready() {} };

  root.classList.add("launching");
  // The phone's status bar keeps the launch's graphite until the page shows.
  const bars = [...document.querySelectorAll('meta[name="theme-color"]')].map((meta) => [meta, meta.content]);
  for (const [meta] of bars) meta.content = GRAPHITE;

  let opened = false;
  const giveUp = setTimeout(ready, GIVE_UP_MS);

  function ready() {
    if (opened) return;
    opened = true;
    clearTimeout(giveUp);
    for (const [meta, content] of bars) meta.content = content;
    root.classList.add("launch-out");
    setTimeout(() => {
      document.getElementById("launch")?.remove();
      root.classList.remove("launching", "launch-out");
    }, OUT_MS);
  }

  return { ready };
})();
