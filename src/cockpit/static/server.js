"use strict";
// The Server page (SPEC TRACK-16): how much processor time and memory each task and each part of it used on this
// machine, over the last day, week or month, and day by day since measuring began. Cockpit measures every program
// every 5 seconds and writes ten-minute rows (resources.py); this page reads them through /api/resources.

// Cockpit writes its figures once a minute.
const POLL_MS = 60e3;
const RANGE_SECONDS = { day: 86400, week: 7 * 86400, month: 30 * 86400 };
const RANGE_WORDS = { day: "the last 24 hours", week: "the last 7 days", month: "the last 30 days" };

// `data`: Cockpit's last answer for `range`. `loads` counts the reads, so an answer to an older one is dropped.
const ui = { range: rangeFromAddress(), data: undefined, loads: 0 };
let timer = null;

function rangeFromAddress() {
  const asked = new URLSearchParams(location.search).get("range");
  return asked in RANGE_SECONDS ? asked : "day";
}

function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [name, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (name === "class") el.className = value;
    else if (name === "text") el.textContent = value;
    else if (name.startsWith("on")) el.addEventListener(name.slice(2), value);
    else el.setAttribute(name, value === true ? "" : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

const SVG_NS = "http://www.w3.org/2000/svg";
function s(tag, props, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [name, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (name === "text") el.textContent = value;
    else el.setAttribute(name, String(value));
  }
  for (const child of children.flat(Infinity)) if (child) el.append(child);
  return el;
}

async function api(path) {
  const { response, data } = await readThroughRestart(path, { credentials: "same-origin" });
  if (response.status === 401) {
    location.href = "/auth/google";
    throw Object.assign(new Error("Signed out"), { status: 401 });
  }
  if (!response.ok) throw Object.assign(new Error(data.detail || `Cockpit answered ${response.status}`), { status: response.status });
  return data;
}

// What each part of the page was last drawn from: a part is drawn again only when that changed or the page's width
// did, so a reading that brought nothing new leaves the part and the pointer's place on it alone.
const drawn = {};
function show(id, from, build) {
  const part = document.getElementById(id);
  const key = JSON.stringify([from, part.clientWidth]);
  if (drawn[id] === key) return;
  drawn[id] = key;
  Loading.settle(part);
  part.replaceChildren(...[build(part.clientWidth)].flat(Infinity).filter(Boolean));
}

function head(id, title, hint) {
  return h("div", { class: "section-head" }, h("h2", { id, text: title }), hint && h("span", { class: "hint", text: hint }));
}

function fail(words) {
  document.getElementById("banner").replaceChildren(h("div", { class: "banner fail", text: words }));
}

// Numbers: one decimal below ten, whole above; two decimals for the smallest.
function figure(value) {
  if (value === null || value === undefined) return "–";
  if (value === 0) return "0";
  if (Math.abs(value) < 0.1) return value.toFixed(2);
  return Math.abs(value) < 10 ? value.toFixed(1) : Math.round(value).toLocaleString("en-US");
}

function percent(share) {
  return share === null || share === undefined ? "–" : `${Math.round(share * 100)}%`;
}

function clock(date) {
  return date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false });
}

function dayName(date) {
  return date.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
}

function famClass(data, family) {
  return `fam-${data.families.indexOf(family) + 1}`;
}

// A row's task: its item's title, linked; a session no item lists; or the machine's own programs.
function taskCell(row) {
  if (row.item_id) return h("a", { href: `/#item/${row.item_id}`, text: row.title || `Item ${row.item_id}` });
  const sessions = row.session_ids || [row.session_id];
  if (sessions.length === 1 && sessions[0] === "") return h("span", { class: "quiet", text: "No session: the machine's own programs" });
  return h("span", { class: "quiet", text: `Session ${sessions[0].slice(0, 8)}, on no task` });
}

// The machine over the range at a glance.

function readout(figureWords, label, title) {
  return h("div", { class: "spend-readout", title },
    h("span", { class: "spend-figure", text: figureWords }),
    h("span", { class: "spend-label", text: label }));
}

function renderMachine(data) {
  const m = data.machine;
  show("server-machine", m, () => [
    h("div", { class: "meters" },
      readout(m.busy === null ? "–" : `${figure(m.busy)} of ${m.cores}`, "cores busy on average",
        "The processor's cores running a program, on average over the time measured."),
      readout(m.used_gb === null ? "–" : `${figure(m.used_gb)} of ${figure(m.mem_total_gb)} GB`, "memory in use on average",
        "Memory held by programs and the kernel, without the files the kernel keeps for speed."),
      readout(percent(m.cpu_wait), "of the time a program waited for a core",
        "The share of time at least one program was ready to run while every core was busy (the kernel's pressure figure)."),
      readout(percent(m.mem_wait), "of the time a program waited for memory",
        "The share of time at least one program stalled while the kernel freed memory for it.")),
    h("p", { class: "spend-note" }, m.sampled_hours
      ? `Measured for ${figure(m.sampled_hours)} hours of ${RANGE_WORDS[data.range]}.${data.now ? ` Last written at ${clock(new Date(data.now.at))}.` : ""}`
      : "Nothing measured in this range yet. Cockpit writes its first figures a minute after it starts."),
  ]);
}

// The sessions that used most in the last minute.

function renderNow(data) {
  const now = data.now;
  show("server-now", now, () => {
    if (!now || !now.tasks.length) return [];
    return [
      head("now-title", "Right now", `The last minute, at ${clock(new Date(now.at))}`),
      h("div", { class: "table-scroll" }, h("table", { class: "spend-table" },
        h("thead", {}, h("tr", {}, h("th", { text: "Task" }), h("th", { text: "Cores" }), h("th", { text: "Memory" }), h("th", { class: "parts-col", text: "Busiest parts" }))),
        h("tbody", {}, now.tasks.map((row) => h("tr", {},
          h("th", { class: "name-col" }, taskCell(row)),
          h("td", { text: figure(row.cores) }),
          h("td", { text: `${figure(row.mem_mb / 1024)} GB` }),
          h("td", { class: "parts-col", text: row.parts.filter((p) => p.cores >= 0.01).map((p) => `${p.part} ${figure(p.cores)}`).join(", ") })))))),
    ];
  });
}

// Charts. One axis each; the families stacked in their fixed order, the machine's own figure as a line over them.

const PLOT = { top: 10, right: 12, bottom: 24, left: 44, height: 220 };

function niceStep(max, count) {
  const raw = max / count;
  const power = 10 ** Math.floor(Math.log10(raw));
  return [1, 2, 2.5, 5, 10].map((n) => n * power).find((n) => n >= raw);
}

// Time marks for the range, at whole local hours or days.
function timeTicks(begin, end, range) {
  const ticks = [];
  const at = new Date(begin);
  if (range === "day") {
    at.setMinutes(0, 0, 0);
    while (at.getTime() < begin || at.getHours() % 3) at.setHours(at.getHours() + 1);
    for (; at.getTime() <= end; at.setHours(at.getHours() + 3)) ticks.push([at.getTime(), clock(at)]);
  } else {
    const every = range === "week" ? 1 : 5;
    at.setHours(0, 0, 0, 0);
    while (at.getTime() < begin) at.setDate(at.getDate() + 1);
    for (; at.getTime() <= end; at.setDate(at.getDate() + every)) ticks.push([at.getTime(), at.toLocaleDateString(undefined, { day: "numeric", month: "short" })]);
  }
  return ticks;
}

function legend(items) {
  return h("ul", { class: "legend" }, items.map(([cls, words]) => h("li", {}, h("span", { class: `swatch ${cls}` }), words)));
}

// A stacked area over time. `values(point)` gives each family's figure; `total(point)` the machine's line;
// `limit` a dashed line at the machine's size. A missing slot breaks the areas and the line.
function areaChart(data, width, { values, total, totalWords, limit, limitWords, unit, label }) {
  const families = data.families;
  const stepMs = data.step_s * 1000;
  const end = Date.now();
  const begin = end - RANGE_SECONDS[data.range] * 1000;
  const points = data.series.map((point) => {
    const at = new Date(point.at).getTime();
    let sum = 0;
    const stack = families.map((family) => {
      const low = sum;
      sum += Math.max(0, values(point)[family] || 0);
      return [low, sum];
    });
    return { point, at, mid: at + stepMs / 2, stack, sum, total: total(point) };
  }).filter((p) => p.at + stepMs > begin);
  const runs = [];
  for (const p of points) {
    const run = runs[runs.length - 1];
    if (run && p.at - run[run.length - 1].at <= stepMs) run.push(p);
    else runs.push([p]);
  }
  const top = Math.max(limit, ...points.map((p) => Math.max(p.sum, p.total)), 0.001) * 1.04;
  const step = niceStep(top, 4);
  const yMax = Math.ceil(top / step) * step;
  const w = Math.max(280, width);
  const H = PLOT.top + PLOT.height + PLOT.bottom;
  const x = (t) => PLOT.left + ((t - begin) / (end - begin)) * (w - PLOT.left - PLOT.right);
  const y = (v) => PLOT.top + PLOT.height * (1 - v / yMax);
  // A run of one slot is drawn across its own ten minutes or hour, so it shows.
  const xs = (run) => (run.length === 1 ? [run[0].at, run[0].at + stepMs] : run.map((p) => p.mid));
  const runPoints = (run) => (run.length === 1 ? [run[0], run[0]] : run);
  const grid = [];
  for (let v = 0; v <= yMax + step / 2; v += step) {
    grid.push(s("line", { class: "grid", x1: PLOT.left, x2: w - PLOT.right, y1: y(v), y2: y(v) }),
      s("text", { class: "axis-label", x: PLOT.left - 6, y: y(v) + 4, "text-anchor": "end", text: figure(v) }));
  }
  const ticks = timeTicks(begin, end, data.range).map(([t, words]) =>
    s("text", { class: "axis-label", x: x(t), y: H - 6, "text-anchor": "middle", text: words }));
  const bands = runs.flatMap((run) => families.map((family, i) => {
    const at = xs(run);
    const ps = runPoints(run);
    const upper = ps.map((p, j) => `${x(at[j]).toFixed(1)},${y(p.stack[i][1]).toFixed(1)}`);
    const lower = ps.map((p, j) => `${x(at[j]).toFixed(1)},${y(p.stack[i][0]).toFixed(1)}`).reverse();
    if (ps.every((p) => p.stack[i][1] - p.stack[i][0] <= 0)) return null;
    return s("polygon", { class: `band fam-${i + 1}`, points: [...upper, ...lower].join(" ") });
  }));
  const lines = runs.map((run) => {
    const at = xs(run);
    return s("polyline", { class: "total-line", points: runPoints(run).map((p, j) => `${x(at[j]).toFixed(1)},${y(p.total).toFixed(1)}`).join(" ") });
  });
  const limitLine = s("line", { class: "limit-line", x1: PLOT.left, x2: w - PLOT.right, y1: y(limit), y2: y(limit) });
  const cross = s("line", { class: "cross", y1: PLOT.top, y2: PLOT.top + PLOT.height, visibility: "hidden" });
  const hit = s("rect", { class: "hit", x: PLOT.left, y: PLOT.top, width: w - PLOT.left - PLOT.right, height: PLOT.height });
  const svg = s("svg", { viewBox: `0 0 ${w} ${H}`, width: w, height: H, role: "img", "aria-label": label },
    grid, bands, limitLine, lines, ticks, cross, hit);
  const tip = h("div", { class: "chart-tip", hidden: true });
  const chart = h("div", { class: "chart" }, svg, tip);
  // The crosshair: the slot nearest the pointer, its families from the top of the stack down, then the line's figure.
  const point = (event) => {
    if (!points.length) return;
    const box = svg.getBoundingClientRect();
    const px = ((event.clientX - box.left) / box.width) * w;
    const t = begin + ((px - PLOT.left) / (w - PLOT.left - PLOT.right)) * (end - begin);
    const p = points.reduce((best, q) => (Math.abs(q.mid - t) < Math.abs(best.mid - t) ? q : best));
    const cx = x(p.mid);
    cross.setAttribute("x1", cx);
    cross.setAttribute("x2", cx);
    cross.setAttribute("visibility", "visible");
    const from = new Date(p.at);
    const to = new Date(p.at + stepMs);
    tip.replaceChildren(
      h("div", { class: "tip-when", text: data.range === "month" ? `${dayName(from)}, ${clock(from)} to ${clock(to)}` : `${data.range === "week" ? `${dayName(from)}, ` : ""}${clock(from)} to ${clock(to)}` }),
      ...families.map((family, i) => [family, i]).reverse().filter(([family]) => (values(p.point)[family] || 0) > 0).map(([family, i]) =>
        h("div", { class: "tip-row" }, h("span", { class: `swatch fam-${i + 1}` }), h("span", { text: family }), h("span", { text: `${figure(values(p.point)[family])} ${unit}` }))),
      h("div", { class: "tip-row tip-total" }, h("span", { class: "swatch line" }), h("span", { text: totalWords }), h("span", { text: `${figure(p.total)} ${unit}` })));
    tip.hidden = false;
    const left = (cx / w) * box.width;
    tip.style.top = `${PLOT.top}px`;
    tip.style.left = left > box.width / 2 ? "" : `${left + 12}px`;
    tip.style.right = left > box.width / 2 ? `${box.width - left + 12}px` : "";
  };
  hit.addEventListener("pointermove", point);
  hit.addEventListener("pointerdown", point);
  hit.addEventListener("pointerleave", () => {
    cross.setAttribute("visibility", "hidden");
    tip.hidden = true;
  });
  const table = h("details", { class: "chart-table" }, h("summary", { text: "Figures as a table" }),
    h("div", { class: "table-scroll" }, h("table", { class: "spend-table" },
      h("thead", {}, h("tr", {}, h("th", { text: "From" }), families.map((family) => h("th", { text: family })), h("th", { text: totalWords }))),
      h("tbody", {}, points.slice().reverse().map((p) => h("tr", {},
        h("th", { text: `${data.range === "day" ? "" : `${dayName(new Date(p.at))} `}${clock(new Date(p.at))}` }),
        families.map((family) => h("td", { text: figure(values(p.point)[family] || 0) })),
        h("td", { text: figure(p.total) })))))));
  return [
    legend([...families.map((family, i) => [`fam-${i + 1}`, family]), ["line", totalWords], ["dashed", limitWords]]),
    chart, table,
  ];
}

function renderCpu(data, width) {
  show("server-cpu", [data.series, data.range], () => [
    head("cpu-title", "Processor", "Cores in use, on average over each slot, by the kind of program"),
    areaChart(data, width, {
      values: (p) => p.cpu, total: (p) => p.busy, totalWords: "Whole machine", unit: "cores",
      limit: data.machine.cores, limitWords: `All ${data.machine.cores} cores`,
      label: `Cores in use over ${RANGE_WORDS[data.range]}, by kind of program`,
    }),
  ]);
}

function renderMemory(data, width) {
  show("server-memory", [data.series, data.range], () => [
    head("memory-title", "Memory", "GB held, on average over each slot, by the kind of program"),
    areaChart(data, width, {
      values: (p) => p.mem_gb, total: (p) => p.used_gb, totalWords: "Whole machine", unit: "GB",
      limit: data.machine.mem_total_gb, limitWords: `All ${figure(data.machine.mem_total_gb)} GB`,
      label: `Memory in use over ${RANGE_WORDS[data.range]}, by kind of program`,
    }),
  ]);
}

// Tables. A core-hour is one core busy for an hour; a GB-hour is one GB held for an hour.

function renderTasks(data) {
  show("server-tasks", data.tasks, () => {
    if (!data.tasks.length) return [];
    return [
      head("tasks-title", "Tasks", "A core-hour is one core busy for an hour; a GB-hour is one GB held for an hour. Idle is the share used while the task's agent was not working."),
      h("div", { class: "table-scroll" }, h("table", { class: "spend-table" },
        h("thead", {}, h("tr", {},
          h("th", { text: "Task" }), h("th", { text: "Core-hours" }), h("th", { text: "GB-hours" }), h("th", { text: "Most at once" }),
          h("th", { text: "Idle CPU" }), h("th", { text: "Idle memory" }), h("th", { class: "parts-col", text: "Parts" }))),
        h("tbody", {}, data.tasks.map((task) => h("tr", {},
          h("th", { class: "name-col" }, taskCell(task)),
          h("td", { class: "sorted", text: figure(task.core_hours) }),
          h("td", { text: figure(task.gb_hours) }),
          h("td", { text: `${figure(task.peak_gb)} GB` }),
          h("td", { text: percent(task.idle_cpu_share) }),
          h("td", { text: percent(task.idle_mem_share) }),
          h("td", { class: "parts-col", text: task.parts.slice(0, 4).map((p) => `${p.part} ${figure(p.core_hours)}`).join(", ") })))))),
    ];
  });
}

function renderParts(data) {
  show("server-parts", data.parts, () => {
    if (!data.parts.length) return [];
    return [
      head("parts-title", "Parts", "Every task's programs added up by what they are"),
      h("div", { class: "table-scroll" }, h("table", { class: "spend-table" },
        h("thead", {}, h("tr", {},
          h("th", { text: "Part" }), h("th", { text: "Core-hours" }), h("th", { text: "GB-hours" }), h("th", { text: "Most at once" }), h("th", { text: "Idle CPU" }))),
        h("tbody", {}, data.parts.map((part) => h("tr", {},
          h("th", { class: "name-col" }, h("span", { class: `swatch ${famClass(data, part.family)}`, title: part.family }), part.part),
          h("td", { class: "sorted", text: figure(part.core_hours) }),
          h("td", { text: figure(part.gb_hours) }),
          h("td", { text: `${figure(part.peak_gb)} GB` }),
          h("td", { text: percent(part.idle_cpu_share) })))))),
    ];
  });
}

// Every recorded day: core-hours per family as stacked bars.
function renderDaily(data, width) {
  const days = data.daily;
  show("server-daily", [days, data.families], () => {
    if (!days.length) return [];
    const families = data.families;
    const w = Math.max(280, width);
    const H = PLOT.top + 160 + PLOT.bottom;
    const plotW = w - PLOT.left - PLOT.right;
    const slot = plotW / Math.max(days.length, 7);
    const barW = Math.min(36, slot * 0.7);
    const sums = days.map((day) => families.reduce((sum, family) => sum + (day.core_hours[family] || 0), 0));
    const step = niceStep(Math.max(...sums, 0.001) * 1.04, 4);
    const yMax = Math.ceil((Math.max(...sums, 0.001) * 1.04) / step) * step;
    const y = (v) => PLOT.top + 160 * (1 - v / yMax);
    const grid = [];
    for (let v = 0; v <= yMax + step / 2; v += step) {
      grid.push(s("line", { class: "grid", x1: PLOT.left, x2: w - PLOT.right, y1: y(v), y2: y(v) }),
        s("text", { class: "axis-label", x: PLOT.left - 6, y: y(v) + 4, "text-anchor": "end", text: figure(v) }));
    }
    const tip = h("div", { class: "chart-tip", hidden: true });
    const labelEvery = Math.ceil(days.length / Math.max(1, Math.floor(plotW / 64)));
    const bars = days.map((day, d) => {
      const cx = PLOT.left + slot * (d + 0.5);
      let sum = 0;
      const rects = families.map((family, i) => {
        const value = day.core_hours[family] || 0;
        if (value <= 0) return null;
        const rect = s("rect", { class: `bar fam-${i + 1}`, x: cx - barW / 2, y: y(sum + value), width: barW, height: Math.max(0, y(sum) - y(sum + value)) });
        sum += value;
        return rect;
      });
      const date = new Date(`${day.day}T12:00:00`);
      const hit = s("rect", { class: "hit", x: cx - slot / 2, y: PLOT.top, width: slot, height: 160 });
      const showTip = () => {
        tip.replaceChildren(
          h("div", { class: "tip-when", text: `${dayName(date)}, measured ${figure(day.sampled_hours)} of 24 hours` }),
          ...families.map((family, i) => [family, i]).reverse().filter(([family]) => (day.core_hours[family] || 0) > 0).map(([family, i]) =>
            h("div", { class: "tip-row" }, h("span", { class: `swatch fam-${i + 1}` }), h("span", { text: family }), h("span", { text: `${figure(day.core_hours[family])} core-h` }))),
          h("div", { class: "tip-row tip-total" }, h("span", {}), h("span", { text: "All" }), h("span", { text: `${figure(sums[d])} core-h` })));
        tip.hidden = false;
        tip.style.top = `${PLOT.top}px`;
        const left = (cx / w) * chartBox.clientWidth;
        tip.style.left = cx > w / 2 ? "" : `${left + barW}px`;
        tip.style.right = cx > w / 2 ? `${chartBox.clientWidth - left + barW}px` : "";
      };
      hit.addEventListener("pointerenter", showTip);
      hit.addEventListener("pointerdown", showTip);
      hit.addEventListener("pointerleave", () => { tip.hidden = true; });
      const label = d % labelEvery === 0 && s("text", { class: "axis-label", x: cx, y: H - 6, "text-anchor": "middle", text: date.toLocaleDateString(undefined, { day: "numeric", month: "short" }) });
      return [rects, label, hit];
    });
    const svg = s("svg", { viewBox: `0 0 ${w} ${H}`, width: w, height: H, role: "img", "aria-label": "Core-hours each day, by kind of program" }, grid, bars);
    const chartBox = h("div", { class: "chart" }, svg, tip);
    return [
      head("daily-title", "Day by day", "Core-hours each day since measuring began, kept for good"),
      legend(families.map((family, i) => [`fam-${i + 1}`, family])),
      chartBox,
      h("details", { class: "chart-table" }, h("summary", { text: "Figures as a table" }),
        h("div", { class: "table-scroll" }, h("table", { class: "spend-table" },
          h("thead", {}, h("tr", {}, h("th", { text: "Day" }), h("th", { text: "Hours measured" }), families.map((family) => h("th", { text: `${family}, core-h` })), h("th", { text: "Memory, GB-h" }))),
          h("tbody", {}, days.slice().reverse().map((day) => h("tr", {},
            h("th", { text: dayName(new Date(`${day.day}T12:00:00`)) }),
            h("td", { text: figure(day.sampled_hours) }),
            families.map((family) => h("td", { text: figure(day.core_hours[family] || 0) })),
            h("td", { text: figure(Object.values(day.gb_hours).reduce((a, b) => a + b, 0)) }))))))),
    ];
  });
}

function render() {
  for (const button of document.querySelectorAll(".server-ranges button")) {
    button.setAttribute("aria-pressed", String(button.dataset.range === ui.range));
  }
  const data = ui.data;
  if (!data) return;
  const width = document.getElementById("server-cpu").clientWidth || document.querySelector(".server-layout").clientWidth;
  renderMachine(data);
  renderNow(data);
  renderCpu(data, width);
  renderMemory(data, width);
  renderTasks(data);
  renderParts(data);
  renderDaily(data, width);
}

async function load() {
  clearTimeout(timer);
  const mine = ++ui.loads;
  try {
    const data = await api(`/api/resources?range=${ui.range}`);
    if (mine !== ui.loads) return;
    ui.data = data;
    document.getElementById("banner").replaceChildren();
    render();
  } catch (error) {
    if (mine !== ui.loads) return;
    logError("loading the server's figures", error);
    Loading.failed(document.querySelector(".server-layout"));
    fail(`The server's figures did not load: ${error.message}`);
  }
  if (mine === ui.loads) timer = setTimeout(load, POLL_MS);
}

document.addEventListener("DOMContentLoaded", () => {
  for (const button of document.querySelectorAll(".server-ranges button")) {
    button.addEventListener("click", () => {
      ui.range = button.dataset.range;
      const address = new URL(location.href);
      address.searchParams.set("range", ui.range);
      history.replaceState(null, "", address);
      render();
      load();
    });
  }
  let resizing = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizing);
    resizing = setTimeout(render, 150);
  });
  render();
  load();
});
