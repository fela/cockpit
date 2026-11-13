"use strict";

const { fullState } = require("./work.cjs");

// Complete readings keep visual checks independent of the host's load readers.
const MACHINE = {
  cores: 8, load: 2, memory_used_gb: 13.6, memory_total_gb: 16, swap_used_gb: 0,
  disk_free_gb: 20, disk_total_gb: 1000, cpu: "under", memory: "near", disk: "over", level: "over",
};

const LOW_MACHINE = { ...MACHINE, memory_used_gb: 8, disk_free_gb: 400, memory: "under", disk: "under", level: "under" };

async function routeMachine(page, readings) {
  await page.route("**/api/state**", async (route) => {
    const full = await fullState(route);
    const state = await full.response.json();
    state.usage.machine = readings;
    await route.fulfill({ ...full, json: state });
  });
}

module.exports = { MACHINE, LOW_MACHINE, routeMachine };
