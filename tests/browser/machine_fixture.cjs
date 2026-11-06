"use strict";

// Complete readings keep visual checks independent of the host's load readers.
const MACHINE = {
  cores: 8, load: 2, memory_used_gb: 13.6, memory_total_gb: 16, swap_used_gb: 0,
  disk_free_gb: 20, disk_total_gb: 1000, cpu: "under", memory: "near", disk: "over", level: "over",
};

const LOW_MACHINE = { ...MACHINE, memory_used_gb: 8, disk_free_gb: 400, memory: "under", disk: "under", level: "under" };

async function routeMachine(page, readings) {
  await page.route("**/api/state**", async (route) => {
    const response = await route.fetch({ maxRetries: 2 });
    const state = await response.json();
    state.usage.machine = readings;
    await route.fulfill({ response, json: state });
  });
}

module.exports = { MACHINE, LOW_MACHINE, routeMachine };
