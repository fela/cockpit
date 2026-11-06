// The emergency page (SPEC SESSION-21): plain DOM calls and fetch, no other script, so it keeps working when the
// main page's scripts break. It reads /api/emergency every few seconds, faster while the session answers.
"use strict";

const ACCOUNT_KEY = "cockpit-emergency-account";
const AGENT_KEY = "cockpit-emergency-agent";
const MODEL_KEY = "cockpit-emergency-model";
const EFFORT_KEY = "cockpit-emergency-effort";
const FAST_MS = 2000;
const SLOW_MS = 15000;
// How long a read and an action wait for Cockpit: Stop and Close wait up to 3 s for a turn to end.
const READ_WAIT_MS = 20000;
const ACT_WAIT_MS = 30000;

const turnsBox = document.getElementById("turns");
const accountsBox = document.getElementById("accounts");
const agentsBox = document.getElementById("agents");
const modelBox = document.getElementById("model");
const effortBox = document.getElementById("effort");
const effortBarsBox = document.getElementById("effort-bars");
const form = document.getElementById("compose");
const textBox = document.getElementById("text");
const sendButton = document.getElementById("send");
const stopButton = document.getElementById("stop");
const closeButton = document.getElementById("close");
const statusLine = document.getElementById("status");
const where = document.getElementById("where");

let timer = null;
let shown = "";
// The session and its turn count as last drawn: Stop and Close name them, so they act on what the user saw.
let seen = { session_id: null, count: 0 };
// Reads and actions are counted. A read is drawn only when it is the latest and no action overlapped it, so a read
// that Cockpit answered before an action took effect cannot redraw over what the action's own answer shows.
let reads = 0;
let actions = 0;
let acting = 0;

function remembered(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}

function remember(key, value) {
  try { localStorage.setItem(key, value); } catch { /* the choice is only a convenience */ }
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// The agent's mark and the effort's bars, drawn as icons.js draws them for the main page, which this page does not
// load: Claude's spark, Codex's terminal prompt, and one to five lit bars for low to max.
const MARKS = {
  claude: `<path d="M8 1.75v12.5M1.75 8h12.5M3.6 3.6l8.8 8.8M12.4 3.6l-8.8 8.8"/>`,
  codex: `<rect x="1.75" y="2.75" width="12.5" height="10.5" rx="2.25" fill="currentColor" fill-opacity=".16"/><path d="M4.75 6.25L6.75 8l-2 1.75M8.75 10h2.5"/>`,
};
const EFFORTS = ["low", "medium", "high", "xhigh", "max"];
const effortMarks = (lit) => [10.5, 8.5, 6.5, 4.5, 2.5].map((y, i) =>
  `<rect x="${1.25 + 3 * i}" y="${y}" width="2" height="${13.5 - y}" rx=".75" fill="currentColor" stroke="none"${i < lit ? "" : ` opacity=".3"`}/>`).join("");

function mark(markup) {
  const template = document.createElement("template");
  template.innerHTML = `<svg class="mark" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${markup}</svg>`;
  return template.content.firstChild;
}

// The effort's bars beside its list, for the effort it shows.
function drawEffortBars() {
  effortBarsBox.replaceChildren(mark(effortMarks(EFFORTS.indexOf(effortBox.value) + 1)));
}

function say(text, problem) {
  statusLine.textContent = text;
  statusLine.className = problem ? "problem" : "";
}

// The agent of a session yet to start, as the user picked it, else Claude.
function pickedAgent(view) {
  return view.provider || agentsBox.querySelector("input:checked")?.value || remembered(AGENT_KEY) || "claude";
}

const AGENT_NAMES = { claude: "Claude", codex: "Codex" };

// Before the first message, the agent: Claude or Codex, while both are on (SPEC SESSION-22). The session keeps it.
function renderAgents(view) {
  const offered = Object.keys(view.options);
  const picked = pickedAgent(view);
  agentsBox.hidden = Boolean(view.session_id) || offered.length < 2;
  for (const label of agentsBox.querySelectorAll("label")) label.remove();
  for (const name of offered) {
    const label = element("label");
    const input = element("input");
    input.type = "radio";
    input.name = "agent";
    input.value = name;
    input.checked = name === picked;
    input.addEventListener("change", () => { remember(AGENT_KEY, name); render(lastView); });
    label.append(input, mark(MARKS[name]), AGENT_NAMES[name]);
    agentsBox.append(label);
  }
}

// The model and effort of the next message, for the session's agent; a pick is remembered per agent.
function renderPicks(view) {
  const agent = view.options[pickedAgent(view)] ? pickedAgent(view) : Object.keys(view.options)[0];
  const offered = view.options[agent];
  const fill = (select, values, key, fallback) => {
    // Filled again only when the agent or what it offers changes, so a read every few seconds leaves an open list and
    // the user's pick alone; a pick no longer offered goes back to the default.
    const offers = JSON.stringify(values);
    if (select.dataset.agent === agent && select.dataset.offers === offers) return;
    const kept = select.dataset.agent === agent ? select.value : null;
    const choice = [kept, remembered(`${key}-${agent}`)].find((value) => value && values.some(([each]) => each === value)) || fallback;
    select.replaceChildren(...values.map(([value, words]) => {
      const option = element("option", null, words);
      option.value = value;
      option.selected = value === choice;
      return option;
    }));
    select.dataset.agent = agent;
    select.dataset.offers = offers;
  };
  fill(modelBox, offered.models.map((model, at) => [model.name, `${model.label}${at === 0 ? " (default)" : ""}`]), MODEL_KEY, offered.models[0].name);
  fill(effortBox, offered.efforts.map((effort) => [effort, `${effort}${effort === offered.effort ? " (default)" : ""}`]), EFFORT_KEY, offered.effort);
  drawEffortBars();
  accountsBox.hidden = agent !== "claude";
  return agent;
}

function renderAccounts(view) {
  const picked = accountsBox.querySelector("input:checked")?.value;
  const last = view.turns.length ? view.turns[view.turns.length - 1].account : null;
  const names = view.accounts.map((account) => account.name);
  const choice = [picked, remembered(ACCOUNT_KEY), last].find((name) => name && names.includes(name))
    || (view.accounts.find((account) => account.signed_in) || view.accounts[0]).name;
  for (const label of accountsBox.querySelectorAll("label")) label.remove();
  for (const account of view.accounts) {
    const label = element("label");
    const input = element("input");
    input.type = "radio";
    input.name = "account";
    input.value = account.name;
    input.checked = account.name === choice;
    input.addEventListener("change", () => remember(ACCOUNT_KEY, account.name));
    label.append(input, element("span", "account-letter", account.letter), account.name);
    if (!account.signed_in) label.append(element("span", "signed-out", "signed out"));
    accountsBox.append(label);
  }
}

function renderTurns(view) {
  // Redraw only when something changed, so a reader's place and selection stay put between reads.
  const key = JSON.stringify(view.turns);
  if (key === shown) return;
  const atBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 40;
  shown = key;
  turnsBox.replaceChildren();
  if (!view.turns.length) {
    turnsBox.append(element("p", "about", "No messages yet. Write what is broken below, and a new session starts on it."));
  }
  for (const turn of view.turns) {
    const box = element("article", "turn");
    const ran = [turn.model && `${turn.model}${turn.effort ? ` at ${turn.effort} effort` : ""}`,
      turn.account && `on the ${turn.account} account`].filter(Boolean).join(", ");
    box.append(element("p", "who", `You${ran ? `, ${ran}` : ""}, ${turn.at.slice(0, 16).replace("T", " ")} UTC`));
    box.append(element("p", "you", turn.text));
    const replies = element("div", "replies");
    for (const event of turn.events) {
      if (event.kind === "call") replies.append(element("p", "call", event.text));
      else if (event.kind === "refused") replies.append(element("p", "refused", `Refused: ${event.text}`));
      else if (event.kind === "error") replies.append(element("p", "error", event.text));
      else if (event.kind === "output") replies.append(element("pre", "output", event.text));
      else replies.append(element("p", "says", event.text));
    }
    if (turn.running) replies.append(element("p", "working", `${AGENT_NAMES[view.provider] || "It"} is working…`));
    if (replies.childElementCount) box.append(replies);
    turnsBox.append(box);
  }
  if (atBottom) window.scrollTo(0, document.body.scrollHeight);
}

let lastView = null;

function render(view) {
  lastView = view;
  const running = view.turns.some((turn) => turn.running);
  where.textContent = view.session_id
    ? `${AGENT_NAMES[view.provider]} session ${view.session_id}, working in ${view.cwd}.`
    : "No session is open. Your next message starts a new one.";
  renderAgents(view);
  renderPicks(view);
  renderAccounts(view);
  renderTurns(view);
  seen = { session_id: view.session_id, count: view.count };
  sendButton.disabled = running;
  stopButton.hidden = !running;
  closeButton.disabled = !view.session_id;
  return running;
}

async function call(method, path, body, waitMs) {
  let response;
  try {
    response = await fetch(path, {
      method,
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
      credentials: "same-origin",
      signal: AbortSignal.timeout(waitMs),
    });
  } catch (error) {
    throw new Error(error.name === "TimeoutError" ? `no answer within ${waitMs / 1000} seconds` : error.message);
  }
  if (response.status === 401) {
    window.location.href = "/";
    throw new Error("Sign in again");
  }
  const data = await response.json().catch(() => ({ detail: `Cockpit answered ${response.status}` }));
  if (!response.ok) throw new Error(data.detail || `Cockpit answered ${response.status}`);
  return data;
}

async function refresh() {
  clearTimeout(timer);
  const ticket = ++reads;
  const quiet = acting === 0;
  const actionsBefore = actions;
  let wait = FAST_MS;
  try {
    const view = await call("GET", "/api/emergency", null, READ_WAIT_MS);
    if (ticket === reads && quiet && acting === 0 && actions === actionsBefore) {
      wait = render(view) ? FAST_MS : SLOW_MS;
      if (statusLine.className === "problem" && statusLine.dataset.from === "read") say("");
    }
  } catch (error) {
    if (ticket === reads && acting === 0) {
      say(`Cockpit did not answer: ${error.message}. Trying again.`, true);
      statusLine.dataset.from = "read";
      wait = SLOW_MS;
    }
  } finally {
    // The latest read always sets the next one, whatever happened to it, so reading never stops.
    if (ticket === reads) {
      clearTimeout(timer);
      timer = setTimeout(refresh, wait);
    }
  }
}

async function act(method, path, body, doing) {
  say(doing);
  statusLine.dataset.from = "act";
  const ticket = ++actions;
  acting += 1;
  try {
    const view = await call(method, path, body, ACT_WAIT_MS);
    if (ticket === actions) render(view);
    say("");
    return true;
  } catch (error) {
    say(error.message, true);
    return false;
  } finally {
    acting -= 1;
    clearTimeout(timer);
    timer = setTimeout(refresh, FAST_MS);
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = textBox.value.trim();
  // The agent, model and effort come from the session's first read; the text waits in the box until then.
  if (!lastView) {
    say("Cockpit has not answered yet, so the agent and model are not known; send again once the session shows.", true);
    return;
  }
  const provider = renderPicks(lastView);
  const account = provider === "claude" ? accountsBox.querySelector("input:checked")?.value : null;
  if (!text || (provider === "claude" && !account)) return;
  sendButton.disabled = true;
  // The session as the page shows it: should it have changed since, the message waits in the box.
  const body = { ...seen, text, provider, account, model: modelBox.value, effort: effortBox.value };
  if (await act("POST", "/api/emergency/send", body, "Sending…")) {
    // Only the message sent: anything written while it went stays.
    if (textBox.value.trim() === text) textBox.value = "";
    window.scrollTo(0, document.body.scrollHeight);
  } else {
    sendButton.disabled = false;
  }
});

textBox.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) form.requestSubmit();
});

modelBox.addEventListener("change", () => remember(`${MODEL_KEY}-${modelBox.dataset.agent}`, modelBox.value));
effortBox.addEventListener("change", () => { remember(`${EFFORT_KEY}-${effortBox.dataset.agent}`, effortBox.value); drawEffortBars(); });

stopButton.addEventListener("click",() => act("POST", "/api/emergency/stop", seen, "Stopping…"));

closeButton.addEventListener("click", () => {
  const agent = AGENT_NAMES[lastView && lastView.provider] || "The agent";
  const answering = !stopButton.hidden ? ` ${agent} stops answering,` : "";
  if (window.confirm(`Close this session?${answering} ${agent} forgets the conversation and it leaves Cockpit's list; its transcript stays on the host. Your next message starts a new session.`)) {
    act("POST", "/api/emergency/close", { ...seen, end_answer: true }, "Closing…");
  }
});

document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
refresh();
