"use strict";

// Cockpit's icons (UI-34): line drawings on a 16 x 16 grid, 1.5 units wide with round ends and joins,
// in the text colour; a few parts are filled (dots, the play triangle, the priority bars). One drawing
// per meaning, the same wherever it shows (UI-25).
//
// Two uses, one set:
// - Icons.icon("check") gives an inline <svg> with the classes "icon icon-check". style.css sizes it:
//   16 px inside keys, 12 px beside text.
// - Every drawing is also a CSS mask on :root, --mark-NAME, for marks drawn by a stylesheet in a
//   pseudo-element (a section's emblem, a group's mark, a stage). style.css paints the mask in a theme
//   colour through --glyph. This script loads in <head> without defer, so the masks are there at the
//   first paint.
//
// Concepts (UI-25): a session is a dot in its state's colour; agents are the bot, Claude the spark and Codex the
// terminal prompt; a step is the stairs; an
// item is the page with a folded corner; a goal is the target; a message is the envelope; feedback is the
// speech bubble; good writing is lines of text with a check, in green, and writing that is hard to understand lines
// ending in a scribble, in red (style.css colours them);
// picking a part of the page is the crosshair; the sweep is the
// radar. Who has the next move: the person (the user), two people (another person), the bug (a bot), the
// merge (a merge to the next stage), the bot (agents), a dashed circle (no one). A stage is a ring that
// fills as the work moves on.

(() => {
  const dot = (x, y, r = 1) => `<circle cx="${x}" cy="${y}" r="${r}" fill="currentColor" stroke="none"/>`;
  const filled = (d) => `<path d="${d}" fill="currentColor" stroke="none"/>`;
  const bar = (x, y, opacity) => `<rect x="${x}" y="${y}" width="3" height="${13.5 - y}" rx="1" fill="currentColor" stroke="none"${opacity ? ` opacity="${opacity}"` : ""}/>`;
  const bars = (lit) => [[1.5, 9], [6.5, 6], [11.5, 2.5]].map(([x, y], i) => bar(x, y, i < lit ? 0 : 0.3)).join("");
  const effort = (lit) => [10.5, 8.5, 6.5, 4.5, 2.5].map((y, i) =>
    `<rect x="${1.25 + 3 * i}" y="${y}" width="2" height="${13.5 - y}" rx=".75" fill="currentColor" stroke="none"${i < lit ? "" : ` opacity=".3"`}/>`).join("");
  // Duotone: a closed shape drawn twice, a soft fill of the line's colour under the line itself.
  const TINT = `fill="currentColor" fill-opacity=".16"`;
  const soft = (d) => `<path d="${d}" ${TINT} stroke="none"/><path d="${d}"/>`;
  const softRing = (cx, cy, r) => `<circle cx="${cx}" cy="${cy}" r="${r}" ${TINT}/>`;
  const softRect = (x, y, w, hgt, rx) => `<rect x="${x}" y="${y}" width="${w}" height="${hgt}" rx="${rx}" ${TINT}/>`;
  const BUBBLE = "M3.5 2.5h9a1.5 1.5 0 0 1 1.5 1.5v6a1.5 1.5 0 0 1-1.5 1.5H7.5L4.5 14v-2.5h-1A1.5 1.5 0 0 1 2 10V4a1.5 1.5 0 0 1 1.5-1.5z";
  const RING = `<circle cx="8" cy="8" r="6"/>`;
  const SOFT_RING = softRing(8, 8, 6);
  // A stage's ring with the share of the work done filled in its middle, clockwise from the top.
  const pie = (share) => {
    if (share >= 1) return dot(8, 8, 3);
    const angle = share * 2 * Math.PI;
    const x = (8 + 3 * Math.sin(angle)).toFixed(2);
    const y = (8 - 3 * Math.cos(angle)).toFixed(2);
    return filled(`M8 8V5A3 3 0 ${share > 0.5 ? 1 : 0} 1 ${x} ${y}z`);
  };

  const GLYPHS = {
    // Keys and lines
    check: `<path d="M3 8.5l3.2 3.2L13 4.8"/>`,
    x: `<path d="M4 4l8 8M12 4l-8 8"/>`,
    clock: `${SOFT_RING}<path d="M8 4.8V8l2.2 1.6"/>`,
    pause: `<path d="M5.5 3.5v9M10.5 3.5v9"/>`,
    play: `<path d="M5 3.3v9.4L12.5 8z" fill="currentColor"/>`,
    up: `<path d="M8 13V3M4 7l4-4 4 4"/>`,
    down: `<path d="M8 3v10M4 9l4 4 4-4"/>`,
    more: dot(3.5, 8, 1.15) + dot(8, 8, 1.15) + dot(12.5, 8, 1.15),
    send: `${soft("M14 2l-4.3 12-2.5-5.2L2 6.3z")}<path d="M14 2L7.2 8.8"/>`,
    handoff: `<path d="M6.5 9.5L13 3M8.5 3H13v4.5"/><path d="M11 9.5V12a1.5 1.5 0 0 1-1.5 1.5h-5.5A1.5 1.5 0 0 1 2.5 12V6.5A1.5 1.5 0 0 1 4 5h2.5"/>`,
    feedback: soft(BUBBLE),
    "writing-good": `<path d="M2.5 3.5h11M2.5 7h11M2.5 10.5h5"/><path d="M9.3 11.3l1.6 1.7 3.1-3.5"/>`,
    "writing-bad": `<path d="M2.5 3.5h11M2.5 7h7"/><path d="M2.5 11c.75-1.2 1.5-1.2 2 0s1.25 1.2 2 0 1.25-1.2 2 0 1.25 1.2 2 0 1.25-1.2 2 0"/>`,
    chat: `${soft(BUBBLE)}<path d="M5.5 6h5M5.5 8.5h3"/>`,
    copy: `${softRect(5.5, 5.5, 8.5, 8.5, 1.5)}<path d="M10.5 5.5V3.5A1.5 1.5 0 0 0 9 2H3.5A1.5 1.5 0 0 0 2 3.5V9a1.5 1.5 0 0 0 1.5 1.5h2"/>`,
    back: `<path d="M5.5 3L2.5 6l3 3"/><path d="M2.5 6h7.25a3.75 3.75 0 0 1 0 7.5H7"/>`,
    reply: `<path d="M6 3.5L2.5 7 6 10.5"/><path d="M2.5 7H9a4.5 4.5 0 0 1 4.5 4.5v1"/>`,
    // Close these tasks (CONTROL-23): a stack of cards, the front one checked.
    stack: `<path d="M4.5 2h7M3 4.25h10"/><rect x="1.75" y="6.5" width="12.5" height="7.5" rx="1.5"/><path d="M5.5 10.2l1.7 1.7 3.3-3.4"/>`,
    archive: `${softRect(1.75, 2.5, 12.5, 3.5, 1)}<path d="M3 6v6.5A1.5 1.5 0 0 0 4.5 14h7a1.5 1.5 0 0 0 1.5-1.5V6M6.5 9h3"/>`,
    retry: `<path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9l1.6 1.6"/><path d="M13.5 2.5v3.2h-3.2"/>`,
    plus: `<path d="M8 3v10M3 8h10"/>`,
    shield: `${soft("M8 1.8l5.5 2.2v4c0 3.2-2.4 5.3-5.5 6.2-3.1-.9-5.5-3-5.5-6.2V4z")}<path d="M5.6 8l1.7 1.7 3.2-3.2"/>`,
    ban: `<circle cx="8" cy="8" r="5.75"/><path d="M3.9 3.9l8.2 8.2"/>`,
    alert: `${soft("M6.7 2.8a1.5 1.5 0 0 1 2.6 0l5 8.7a1.5 1.5 0 0 1-1.3 2.2H3a1.5 1.5 0 0 1-1.3-2.2z")}<path d="M8 6.2v3"/>${dot(8, 11.2, 0.85)}`,
    layers: `${soft("M8 2.5l6 3.25-6 3.25-6-3.25z")}<path d="M2 9.25l6 3.25 6-3.25"/>`,
    pin: `${soft("M6.5 2v4.2L4 9.5h8L9.5 6.2V2z")}<path d="M5.5 2h5M8 9.5V14"/>`,
    target: `${SOFT_RING}<circle cx="8" cy="8" r="2.75"/>`,
    chevron: `<path d="M4.5 6.25L8 9.75l3.5-3.5"/>`,
    "chevron-right": `<path d="M6.25 4.5L9.75 8l-3.5 3.5"/>`,
    wider: `<path d="M9.5 2.5h4v4M13.5 2.5L9.5 6.5M6.5 13.5h-4v-4M2.5 13.5l4-4"/>`,
    narrower: `<path d="M13.5 2.5l-4 4M9.5 3v3.5H13M2.5 13.5l4-4M3 9.5h3.5V13"/>`,
    info: `${SOFT_RING}<path d="M8 7.5V11"/>${dot(8, 5.25, 0.85)}`,
    error: `${SOFT_RING}<path d="M8 4.8v3.7"/>${dot(8, 11, 0.85)}`,
    arrow: `<path d="M3 8h10M9 4l4 4-4 4"/>`,
    terminal: `<path d="M3 4l4 4-4 4M8.5 12.5H13"/>`,
    link: `<path d="M6.5 9.5l3-3"/><path d="M7.3 4.2l.9-.9a2.8 2.8 0 0 1 4 4l-.9.9M8.7 11.8l-.9.9a2.8 2.8 0 0 1-4-4l.9-.9"/>`,
    unlinked: `<path d="M7.3 4.2l.9-.9a2.8 2.8 0 0 1 4 4l-.9.9M8.7 11.8l-.9.9a2.8 2.8 0 0 1-4-4l.9-.9M2.5 5.5H4M5.5 2.5V4M13.5 10.5H12M10.5 13.5V12"/>`,
    pencil: `${soft("M10.8 2.2l3 3L5.5 13.5 2 14l.5-3.5z")}<path d="M9.2 3.8l3 3"/>`,
    bolt: soft("M9 1.5L3.5 9H8l-1 5.5L12.5 7H8z"),
    blocks: `${softRect(1.75, 4, 12.5, 5, 1)}<path d="M6.5 4L9.5 9M4.5 9v4.5M11.5 9v4.5"/>`,
    sort: `<path d="M5 13V3M2.5 5.5L5 3l2.5 2.5M11 3v10M8.5 10.5L11 13l2.5-2.5"/>`,
    grip: [4, 8, 12].flatMap((y) => [dot(6, y), dot(10, y)]).join(""),
    "bars-low": bars(1),
    "bars-normal": bars(2),
    "bars-high": bars(3),
    // A new session's agent and reasoning effort (SPEC SESSION-22): Claude's spark, Codex's terminal prompt, and one
    // to five lit bars for low, medium, high, xhigh and max.
    claude: `<path d="M8 1.75v12.5M1.75 8h12.5M3.6 3.6l8.8 8.8M12.4 3.6l-8.8 8.8"/>`,
    codex: `${softRect(1.75, 2.75, 12.5, 10.5, 2.25)}<path d="M4.75 6.25L6.75 8l-2 1.75M8.75 10h2.5"/>`,
    "effort-1": effort(1),
    "effort-2": effort(2),
    "effort-3": effort(3),
    "effort-4": effort(4),
    "effort-5": effort(5),
    slack: `<path d="M6.3 2.5L5 13.5M11 2.5L9.7 13.5M2.8 6H13.5M2.5 10h10.7"/>`,
    // A link's source shows its own logo: GitHub's and Linear's marks from Simple Icons (CC0),
    // drawn on their 24-unit grid and fitted into 14 of the 16 units, in the line's colour.
    github: `<g transform="translate(1 1) scale(.5833)">${filled("M12 .297c-6.63 0-12 5.373-12 12 0 5.303 3.438 9.8 8.205 11.385.6.113.82-.258.82-.577 0-.285-.01-1.04-.015-2.04-3.338.724-4.042-1.61-4.042-1.61C4.422 18.07 3.633 17.7 3.633 17.7c-1.087-.744.084-.729.084-.729 1.205.084 1.838 1.236 1.838 1.236 1.07 1.835 2.809 1.305 3.495.998.108-.776.417-1.305.76-1.605-2.665-.3-5.466-1.332-5.466-5.93 0-1.31.465-2.38 1.235-3.22-.135-.303-.54-1.523.105-3.176 0 0 1.005-.322 3.3 1.23.96-.267 1.98-.399 3-.405 1.02.006 2.04.138 3 .405 2.28-1.552 3.285-1.23 3.285-1.23.645 1.653.24 2.873.12 3.176.765.84 1.23 1.91 1.23 3.22 0 4.61-2.805 5.625-5.475 5.92.42.36.81 1.096.81 2.22 0 1.606-.015 2.896-.015 3.286 0 .315.21.69.825.57C20.565 22.092 24 17.592 24 12.297c0-6.627-5.373-12-12-12")}</g>`,
    linear: `<g transform="translate(1 1) scale(.5833)">${filled("M2.886 4.18A11.982 11.982 0 0 1 11.99 0C18.624 0 24 5.376 24 12.009c0 3.64-1.62 6.903-4.18 9.105L2.887 4.18ZM1.817 5.626l16.556 16.556c-.524.33-1.075.62-1.65.866L.951 7.277c.247-.575.537-1.126.866-1.65ZM.322 9.163l14.515 14.515c-.71.172-1.443.282-2.195.322L0 11.358a12 12 0 0 1 .322-2.195Zm-.17 4.862 9.823 9.824a12.02 12.02 0 0 1-9.824-9.824Z")}</g>`,
    log: `<path d="M6 4h7.5M6 8h7.5M6 12h7.5"/>${dot(3, 4)}${dot(3, 8)}${dot(3, 12)}`,
    tune: `<path d="M2 5h6.2M11.8 5H14"/><circle cx="10" cy="5" r="1.8"/><path d="M2 11h2.2M7.8 11H14"/><circle cx="6" cy="11" r="1.8"/>`,
    meter: `<path d="M2.5 11.5a5.5 5.5 0 1 1 11 0"/><path d="M8 11.5l2.8-3.4"/>${dot(8, 11.5, 1.1)}<path d="M2 14h12"/>`,
    doc: `${soft("M4.5 1.75h5l3 3v8.75a1 1 0 0 1-1 1h-7a1 1 0 0 1-1-1V2.75a1 1 0 0 1 1-1z")}<path d="M9.5 1.75v3h3M6 8.5h4M6 11h4"/>`,
    pick: `<circle cx="8" cy="8" r="4.5"/><path d="M8 1.5v3M8 11.5v3M1.5 8h3M11.5 8h3"/>${dot(8, 8, 0.85)}`,
    step: `<path d="M2.5 13.5H6V10h3.5V6.5H13V3"/>`,
    radar: `${SOFT_RING}<circle cx="8" cy="8" r="3"/><path d="M8 8l4.2-4.2"/>`,
    image: `${softRect(1.75, 2.75, 12.5, 10.5, 1.5)}${dot(5.5, 6.25, 1.25)}<path d="M2.5 12.5L6 9l2.5 2.5 2-2 3.25 3.25"/>`,
    menu: `<path d="M2.75 4.25h10.5M2.75 8h10.5M2.75 11.75h10.5"/>`,
    logout: `<path d="M6.5 2.5H4A1.5 1.5 0 0 0 2.5 4v8A1.5 1.5 0 0 0 4 13.5h2.5M10 5l3 3-3 3M13 8H6.5"/>`,

    // Marks that lead a section, a group or a row
    // Graphite marker, the logo (UI-28): a ring with wings and a fin, square-ended. The drawing fills its tile, so draw
    // it at the tile's size. brand-small is its heavier cut for tiles of 16 px and less.
    brand: `<g stroke-width=".625" stroke-linecap="butt"><circle cx="8" cy="8.125" r="1.875"/><path d="M2.25 8.125h3.875M9.875 8.125h3.875M8 6.25V4.875"/></g>`,
    "brand-small": `<g stroke-width="1.1" stroke-linecap="butt"><circle cx="8" cy="8.125" r="2.05"/><path d="M2.25 8.125h3.7M10.05 8.125h3.7M8 6.075V4.75"/></g>`,
    mail: `${softRect(1.75, 3.25, 12.5, 9.5, 1.5)}<path d="M2.5 4.5L8 8.75l5.5-4.25"/>`,
    chart: `<path d="M2 13.5h12M4.5 11V8M8 11V3.5M11.5 11V6"/>`,
    hourglass: `<path d="M4 2h8M4 14h8M5.25 2v1.75L8 8l2.75-4.25V2M5.25 14v-1.75L8 8l2.75 4.25V14"/>`,
    key: `${softRing(5.25, 10.75, 3.25)}<path d="M7.6 8.4L13.5 2.5M10.8 5.2l1.8 1.8M12.4 3.6l1.6 1.6"/>`,
    person: `${softRing(8, 5, 2.75)}<path d="M2.75 14c.5-2.75 2.6-4.5 5.25-4.5s4.75 1.75 5.25 4.5"/>`,
    bot: `${softRect(2.25, 5, 11.5, 8.5, 2.25)}<path d="M8 5V2.75M6 8.5v1.5M10 8.5v1.5"/>${dot(8, 2.25, 0.9)}`,
    bug: `${softRect(5, 5, 6, 9, 3)}<path d="M6.25 5.2v-.7a1.75 1.75 0 0 1 3.5 0v.7M2.5 9.5H5M11 9.5h2.5M3 5.5l2 1.5M13 5.5L11 7M3 13.5l2-1.5M13 13.5L11 12"/>`,
    people: `${softRing(6, 5.25, 2.5)}<path d="M1.5 13.75c.4-2.5 2.2-4.25 4.5-4.25s4.1 1.75 4.5 4.25"/><path d="M10.25 2.9a2.5 2.5 0 0 1 0 4.7M12 10c1.3.6 2.2 1.9 2.5 3.75"/>`,
    box: `${soft("M8 1.75l5.75 3.1v6.3L8 14.25l-5.75-3.1v-6.3z")}<path d="M2.5 5L8 8l5.5-3M8 8v6"/>`,
    merge: `${softRing(4.5, 3.5, 1.75)}${softRing(4.5, 12.5, 1.75)}${softRing(11.5, 8.5, 1.75)}<path d="M4.5 5.25v5.5M4.5 5.25c0 2.25 1.75 3.25 5.25 3.25"/>`,
    fork: `${softRing(4.5, 12.5, 1.75)}${softRing(4.5, 3.5, 1.75)}${softRing(11.5, 3.5, 1.75)}<path d="M4.5 10.75v-5.5M4.5 10.75c0-2.25 1.75-3.25 5.25-3.25 1.25 0 1.75-.75 1.75-2.25"/>`,
    map: `${soft("M1.75 4l4.25-1.75 4 1.75 4.25-1.75v9.75L10 13.75 6 12l-4.25 1.75z")}<path d="M6 2.25V12M10 4v9.75"/>`,
    nobody: `<circle cx="8" cy="8" r="5.75" stroke-dasharray="2.26 2.26"/>`,
    done: `${RING}<path d="M5.4 8.2l1.8 1.8 3.4-3.6"/>`,
    flag: `${soft("M3.5 2.5h9l-2 3.25 2 3.25h-9z")}<path d="M3.5 14V2"/>`,
    "stage-planning": `<circle cx="8" cy="8" r="6" stroke-dasharray="2.36 2.36"/>`,
    "stage-building": RING + pie(0.2),
    "stage-review": RING + pie(0.4),
    "stage-dev": RING + pie(0.6),
    "stage-staging": RING + pie(0.8),
    "stage-prod": RING + pie(1),
    "stage-done": `${RING}<path d="M5.4 8.2l1.8 1.8 3.4-3.6"/>`,
    "prio-bars": bars(3),
    "prio-urgent": `<path fill-rule="evenodd" d="M4 1.5h8A2.5 2.5 0 0 1 14.5 4v8a2.5 2.5 0 0 1-2.5 2.5H4A2.5 2.5 0 0 1 1.5 12V4A2.5 2.5 0 0 1 4 1.5zM7.1 4h1.8l-.25 5.25h-1.3zM8 10.4a1.05 1.05 0 1 1 0 2.1 1.05 1.05 0 0 1 0-2.1z" fill="currentColor" stroke="none"/>`,
  };
  const ATTRS = `viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"`;

  // The masks: currentColor is black inside an image, which a mask reads as fully drawn.
  const style = document.documentElement.style;
  for (const [name, markup] of Object.entries(GLYPHS)) {
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" ${ATTRS}>${markup}</svg>`;
    style.setProperty(`--mark-${name}`, `url("data:image/svg+xml,${encodeURIComponent(svg)}")`);
  }

  const template = document.createElement("template");
  function icon(name) {
    const markup = GLYPHS[name];
    // An unknown name draws an empty icon; the error names it.
    if (markup === undefined) logError("icons", new Error(`no icon named ${name}`));
    template.innerHTML = `<svg class="icon icon-${name}" ${ATTRS} aria-hidden="true">${markup || ""}</svg>`;
    const svg = template.content.firstChild;
    svg.remove();
    return svg;
  }

  // The Cockpit tag (SPEC UI-43) on an item about Cockpit itself and on each of its steps is the same on every page:
  // the header's key in small (the amber marker on the graphite tile) and the word, so a card about the tool reads
  // apart from the work's. `project` is the item's project as the list carries it; other work gets null.
  function cockpitTag(project) {
    if (project !== "cockpit") return null;
    const tag = document.createElement("span");
    tag.className = "chip cockpit";
    tag.title = "Cockpit";
    // Named for screen readers also where a page shows the key alone and hides the word.
    tag.setAttribute("role", "img");
    tag.setAttribute("aria-label", "Cockpit");
    const mark = document.createElement("span");
    mark.className = "cockpit-mark";
    mark.setAttribute("aria-hidden", "true");
    const words = document.createElement("span");
    words.className = "chip-words";
    words.textContent = "Cockpit";
    tag.append(mark, words);
    return tag;
  }

  // Projects (SPEC VIEW-34): every task is in one, and each has a logo Cockpit designs. The page hands over the list
  // with each state it draws (setProjects); projectMark then gives the small logo that leads a task's row: Cockpit's
  // own tag for Cockpit's work, the project's logo as an image once it has one, until then the project's first letter
  // on a grey square, and nothing for a task in no project yet. The logo's address carries the time it was stored, so
  // a new logo shows at once and an old one comes from the browser's cache.
  let projects = new Map();
  function setProjects(list) {
    projects = new Map(list.map((project) => [project.name.toLowerCase(), project]));
  }
  function projectMark(name) {
    if (!name) return null;
    if (name === "cockpit") return cockpitTag(name);
    const project = projects.get(name.toLowerCase());
    if (project && project.has_logo) {
      const logo = document.createElement("img");
      logo.className = "project-logo";
      logo.src = `/projects/${encodeURIComponent(project.name)}/logo.svg?v=${encodeURIComponent(project.logo_at)}`;
      logo.alt = "";
      logo.width = 14;
      logo.height = 14;
      logo.decoding = "async";
      logo.title = project.name;
      return logo;
    }
    const letter = document.createElement("span");
    letter.className = "project-logo project-letter";
    letter.setAttribute("aria-hidden", "true");
    letter.title = name;
    letter.textContent = [...name.trim()][0]?.toUpperCase() || "";
    return letter;
  }

  // A mention of another item in text the server rendered (SPEC UI-74) is a link that names the item on hover and
  // carries its project (render.py); its project's mark leads it here: Cockpit's key for Cockpit's work, otherwise
  // the project's logo or letter as projectMark draws it. The link's own tooltip names the item, so the mark gives
  // none of its own.
  function markMentions(root) {
    for (const link of root.querySelectorAll("a.item-ref[data-project]")) {
      if (link.querySelector(".ref-mark")) continue;
      const project = link.dataset.project;
      let mark;
      if (project === "cockpit") {
        mark = document.createElement("span");
        mark.className = "cockpit-mark";
      } else {
        mark = projectMark(project);
      }
      mark.classList.add("ref-mark");
      mark.removeAttribute("title");
      mark.setAttribute("aria-hidden", "true");
      link.prepend(mark);
    }
  }

  window.Icons = Object.freeze({ icon, cockpitTag, setProjects, projectMark, markMentions });
})();
