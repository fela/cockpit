"use strict";

// Choices on shared pages (SPEC NEXT-48), on Cockpit's side: every framed page (`/pages/N/figure`) tells this page
// when it loads and when the user chooses in it (page-choices-frame.js). This sends the frame the choices saved
// before and saves each new one, with the step the frame shows the page for (its `data-step`), so the choice goes
// with the user's next answer to that step. A step that no longer takes it gets the choice saved on the page alone.
//
// The page's own scripts can post the same messages, so a message counts only from a frame of a page, and only in
// the shape below; the worst a forged one does is save a choice on its own page.
(() => {
  const NAME_MAX = 200;
  const VALUE_MAX = 2000;
  const VALUES_MAX = 100;

  const frameOf = (source) => [...document.querySelectorAll("iframe")].find((frame) => frame.contentWindow === source) || null;
  const pageOf = (frame) => Number(/\/pages\/(\d+)\/figure/.exec(frame.getAttribute("src") || "")?.[1]) || null;
  const stepOf = (frame) => Number(frame.dataset.step) || null;
  const isValue = (value) => typeof value === "string" && value.length <= VALUE_MAX;
  const validChosen = (chosen) => chosen === null || isValue(chosen)
    || (Array.isArray(chosen) && chosen.length <= VALUES_MAX && chosen.every(isValue));

  async function put(pageId, body) {
    const { response, data } = await sendAction(`/api/item-pages/${pageId}/choices`, {
      method: "PUT", credentials: "same-origin", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
    }, TRY_MS, null);
    if (!response.ok) throw Object.assign(new Error(data.detail || `Cockpit answered ${response.status}`), { status: response.status });
    return data;
  }

  // Tells the page the choice was saved: the `cockpit:choice` event, which the pages showing a step listen to.
  const saved = (detail) => dispatchEvent(new CustomEvent("cockpit:choice", { detail }));

  // Saves go through the topmost Cockpit window, the main page when the Pages view is its layer (page-overlay.js),
  // so a save outlives a layer closed while it runs and one queue orders the saves of every view. For one page and
  // name they go one at a time, so the last choice is the one saved: a choice made while one saves waits, and a
  // newer one replaces it while it waits. `settled(stepId)` resolves once every save is done, tries once more each
  // choice made for that step whose last save failed, and fails while one still fails; an answer to the step waits
  // for it, so it carries the choices just made, or does not go. A failed save for another step holds up no other
  // answer, and a choice on a page since removed holds up none, as it has nowhere to go.
  const queue = (() => {
    try {
      if (window.top !== window && window.top.CockpitChoices) return window.top.CockpitChoices;
    } catch {
      // Not framed by Cockpit: this window keeps its own queue.
    }
    const waiting = new Map();
    const running = new Set();
    // Per page and name, the save that failed last, to try again, and why; a later save that works clears it.
    const failed = new Map();
    return {
      save,
      async settled(stepId) {
        while (running.size) await Promise.all(running);
        for (const failure of failed.values()) if (failure.body.step_id === stepId) save(failure.pageId, failure.body, failure.done);
        while (running.size) await Promise.all(running);
        const failure = [...failed.values()].find((entry) => entry.body.step_id === stepId);
        if (failure) throw new Error(`A choice on a page was not saved (${failure.error}); choose it again, then send`);
      },
    };
    function save(pageId, body, done) {
      const key = `${pageId} ${body.name}`;
      const queued = waiting.has(key);
      waiting.set(key, { body, done });
      if (queued) return;
      const work = (async () => {
        while (waiting.get(key)) {
          const next = waiting.get(key);
          waiting.set(key, null);
          const error = await saveOne(pageId, next.body, next.done);
          if (error) failed.set(key, { pageId, ...next, error });
          else failed.delete(key);
        }
        waiting.delete(key);
      })();
      running.add(work);
      // Later keystrokes wait only in memory until the earlier save finishes; keep leaving guarded through the loop.
      holdLeave(work);
      work.finally(() => running.delete(work));
    }
  })();
  window.CockpitChoices = queue;

  function choose(frame, pageId, message) {
    if (!isValue(message.name) || !message.name || message.name.length > NAME_MAX || !validChosen(message.chosen)) return;
    // A page loading ahead, hidden (pages.js): its choice waits until the user opens the page.
    if (window.cockpitHold) {
      window.cockpitHold.push(() => choose(frame, pageId, message));
      return;
    }
    const body = { name: message.name, chosen: message.chosen, label: isValue(message.label) ? message.label : "", step_id: stepOf(frame) };
    queue.save(pageId, body, saved);
  }

  // `done` tells the view the choice came from; that view may be gone by then. Returns why the save failed, or null
  // when it was saved or its page is gone.
  async function saveOne(pageId, body, done) {
    const tell = (detail) => {
      try {
        done(detail);
      } catch {
        // The layer that showed the page closed meanwhile.
      }
      // And this window, whose steps list the choices that go with their answers.
      dispatchEvent(new CustomEvent("cockpit:choice-saved", { detail }));
    };
    try {
      await put(pageId, body);
      tell({ pageId, stepId: body.step_id, name: body.name, ok: true });
      return null;
    } catch (error) {
      // The step went to its session or closed while the page was open: the choice stays on the page.
      if (error.status === 409 && body.step_id !== null) {
        try {
          await put(pageId, { ...body, step_id: null });
          tell({ pageId, stepId: null, name: body.name, ok: true });
          return null;
        } catch (again) {
          error = again;
        }
      }
      logError("saving a choice on a page", error);
      tell({ pageId, stepId: body.step_id, name: body.name, ok: false, error: error.message });
      return error.status === 404 ? null : error.message;
    }
  }

  async function restore(frame, pageId) {
    try {
      const { response, data } = await readThroughRestart(`/api/item-pages/${pageId}/choices`, { credentials: "same-origin" });
      if (!response.ok) throw new Error(data.detail || `Cockpit answered ${response.status}`);
      const choices = Object.fromEntries(data.choices.map((choice) => [choice.name, choice.chosen]));
      frame.contentWindow?.postMessage({ cockpitChoices: { type: "set", choices } }, "*");
    } catch (error) {
      logError("reading a page's choices", error);
    }
  }

  addEventListener("message", (event) => {
    const message = event.data && event.data.cockpitChoices;
    if (!message || typeof message !== "object") return;
    const frame = frameOf(event.source);
    const pageId = frame && pageOf(frame);
    if (!pageId) return;
    if (message.type === "ready") restore(frame, pageId);
    else if (message.type === "chose") choose(frame, pageId, message);
  });

})();
