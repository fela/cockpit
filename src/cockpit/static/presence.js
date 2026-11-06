"use strict";

// The user's presence (SPEC SWEEP-11): input on the page tells the server the user is here, at most once a
// minute. Sweeps slow down after an hour without input or a write from the user, and stop after two. A
// tab left open sends nothing, so it does not keep the sweeps going. A pointer that only seems to
// move because the page moved under it is not input: the position has to change.
// Only input on a page that has focus counts: a pointer passing over a Cockpit window behind another one is not
// the user working on Cockpit. Such input is also activity for the running timer (SPEC TRACK-5): the page hears
// it as a `cockpit:input` event, and other Cockpit tabs read its time under ACTIVITY_KEY, written at most once a
// second. The page cancels the event while it checks a stretch without activity before the input, and until its
// stop of the timer for that stretch went through: the input then reaches other tabs and the server only when the
// page raises `cockpit:settled`, so what they hold meanwhile is other pages' activity.

(() => {
  const REPORT_MS = 60e3;
  const ACTIVITY_KEY = "cockpit.activity";
  const ACTIVITY_MS = 1e3;
  let reported = 0;
  let stored = 0;
  let x = null;
  let y = null;
  let held = false;

  function store() {
    const now = Date.now();
    if (now - stored < ACTIVITY_MS) return;
    stored = now;
    try {
      localStorage.setItem(ACTIVITY_KEY, String(now));
    } catch {
      // Private windows and blocked storage: other tabs see this one's activity only through the server then.
    }
  }

  // Held before the page hears of it: the page may settle while it handles the event.
  function report() {
    if (!document.hasFocus()) return;
    held = true;
    if (!dispatchEvent(new Event("cockpit:input", { cancelable: true }))) return;
    held = false;
    send();
  }

  function send() {
    store();
    const now = Date.now();
    if (now - reported < REPORT_MS) return;
    reported = now;
    fetchThroughRestart("/api/presence", { method: "POST", credentials: "same-origin" })
      .then((response) => {
        if (!response.ok) throw Object.assign(new Error(`HTTP ${response.status}`), { status: response.status });
        return response.json();
      })
      // The count of inputs the report made: the page's own input is not news to its stop of the timer.
      .then((answer) => {
        if (answer.input_count !== null) dispatchEvent(new CustomEvent("cockpit:counted", { detail: answer.input_count }));
      })
      .catch((error) => {
        reported = 0;
        logError("reporting your presence", error);
      });
  }

  // Input the page held back counts once it settled.
  addEventListener("cockpit:settled", () => {
    if (!held) return;
    held = false;
    send();
  });
  for (const type of ["keydown", "pointerdown", "wheel", "touchstart"]) {
    addEventListener(type, report, { capture: true, passive: true });
  }
  addEventListener("pointermove", (event) => {
    if (event.screenX === x && event.screenY === y) return;
    x = event.screenX;
    y = event.screenY;
    report();
  }, { capture: true, passive: true });
})();
