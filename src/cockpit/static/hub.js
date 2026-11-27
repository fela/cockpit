"use strict";
// The hub's part of the Usage page (SPEC UI-85): the CLIProxyAPI hub, which lends the user's Claude and Codex accounts
// to their machines. A line under the page's head says whether the hub answers, and this machine's tunnel to it when
// the setup checks one, and how many accounts work. Each account has a row (hub-accounts.js) with its state, its
// limits, the requests sent through it and its controls: the one that fits its state as a key, the others in its ⋯
// menu. Add account signs a new one in, for each provider the setup gives a sign-in. A fold at the page's end holds
// the hub's service: how it shares requests, its version and client keys, the restarts the setup gives (of the hub,
// of the tunnel), and the requests the hub failed when there are any. Cockpit reads the hub once a minute
// (hub.py); this page asks Cockpit more often, so a change shows soon. On a machine without a hub these parts stay
// hidden and usage.js draws the machine's own weekly limits. The file loads before usage.js: it puts the hub's places
// into the page as it runs (hubPlaces), and usage.js starts it through window.cockpitUsagePart.

const HUB_POLL_MS = 15e3;
// On a machine without a hub, where only a new key file changes the answer.
const NO_HUB_POLL_MS = 60e3;
// While Cockpit's first reading of the hub is under way.
const FIRST_READ_MS = 2e3;
// A restart waits for its command and then for the hub to answer again (hub.py, RESTART_SECONDS and BACK_SECONDS).
const RESTART_TRY_MS = 120e3;
// The hub's ways of sharing requests among one provider's accounts.
const ROUTINGS = [
  { value: "round-robin", name: "Round robin", about: "Each request goes to the next account in turn." },
  { value: "weighted-round-robin", name: "Weighted round robin", about: "In turn, with more requests to an account with a higher weight. A weight is a number in the account's file on the hub." },
  { value: "fill-first", name: "Fill first", about: "Every request goes to the first account until it is unavailable, then to the next." },
];

// `hub`: Cockpit's last answer, undefined before the first, null on a machine without a hub. `loads` counts the reads
// and the changes, so an answer that was asked for before a newer one is dropped. `busy` says in words which change
// is on its way. `address`: what the user typed into the Claude sign-in's field so far. `none`: whether usage.js was
// last told that the machine has no hub, undefined before the first answer.
// `unread`: Cockpit's last answer did not arrive, which the hub's red line says until one does.
const hubUi = { hub: undefined, loads: 0, busy: "", address: "", none: undefined, unread: false };
let hubTimer = null;

function answered(response, data) {
  if (response.status === 401) {
    location.href = "/auth/google";
    throw Object.assign(new Error("Signed out"), { status: 401 });
  }
  if (!response.ok) throw Object.assign(new Error(data.detail || `Cockpit answered ${response.status}`), { status: response.status });
  return data;
}

function jsonPost(body) {
  return { method: "POST", credentials: "same-origin", headers: { "content-type": "application/json" }, body: JSON.stringify(body) };
}

function confirmDialog() {
  return document.getElementById("hub-confirm");
}

// What each part of the page was last drawn from: a part is drawn again only when that changed, so a reading that
// brought nothing new leaves the part, a field being typed in and the pointer's place on it alone. A part with an open
// menu, and every part while the question dialog is open, waits: the menu's toggle and the dialog's close draw it.
const drawn = {};
function show(id, from, build) {
  const key = JSON.stringify(from);
  if (drawn[id] === key) return;
  const part = document.getElementById(id);
  if (part.querySelector("details.menu[open]") || confirmDialog().open) return;
  const focused = part.contains(document.activeElement) && document.activeElement;
  const account = focused && focused.closest(".hub-account");
  const control = account && (focused.matches(".hub-more > summary") ? ".hub-more > summary" : focused.matches(".hub-keys > button") ? ".hub-keys > button" : null);
  drawn[id] = key;
  Loading.settle(part);
  part.replaceChildren(...[build()].flat(Infinity).filter(Boolean));
  if (control) {
    const row = document.getElementById(account.id);
    const next = row && row.querySelector(control);
    (next && !next.disabled ? next : row && row.querySelector(".hub-more > summary"))?.focus({ preventScroll: true });
  }
}

function count(number) {
  return number.toLocaleString("en-US");
}

function plural(number, one, many) {
  return `${count(number)} ${number === 1 ? one : many}`;
}

// The hub's red line above the page, apart from the usage's own (usage.js, showBanners); no words take it away.
function hubBanner(words) {
  document.getElementById("hub-banner").replaceChildren(...(words ? [h("div", { class: "banner fail", text: words })] : []));
}

// A menu on the page: its list of entries under a summary key. Each entry closes the menu before it acts, so the
// change's redraw is not held back by it.
function menu(className, summary, entries) {
  const el = h("details", { class: `menu ${className}`, ontoggle: () => { if (!el.open) renderHub(); } });
  el.append(summary, h("div", { class: "menu-list" }, entries.filter(Boolean).map((entry) => h("button", {
    type: "button", title: entry.title, disabled: entry.disabled,
    onclick: () => { el.open = false; entry.press(); },
  }, h("span", { class: "menu-label" }, Icons.icon(entry.icon), entry.text)))));
  return el;
}

// A question before a change that cannot be taken back: `question` as the title, `consequence` under it, `verb` on
// the key that makes the change with `then`, and Cancel, which has the focus. `back` finds the key that asked, in the
// page as drawn when the dialog closes, which takes the focus back.
let confirmBack = "";
function confirmChange(question, consequence, verb, back, then) {
  const dialog = confirmDialog();
  confirmBack = back;
  const cancel = h("button", { class: "button ghost", type: "button", onclick: () => dialog.close() }, "Cancel");
  dialog.replaceChildren(h("form", {
    class: "time-panel", tabindex: "-1",
    onsubmit: (event) => {
      event.preventDefault();
      dialog.close();
      then();
    },
  },
  h("h2", { id: "hub-confirm-title", text: question }),
  h("p", { class: "time-note", text: consequence }),
  h("div", { class: "time-actions" }, h("button", { class: "button primary", type: "submit" }, verb), cancel)));
  dialog.showModal();
  cancel.focus();
}

// The hub at a glance: when all is well, that it is up and how its accounts stand; otherwise what is wrong first.

function troubles(hub) {
  const found = [];
  if (hub.error) found.push(`Cockpit could not read the hub: ${hub.error}`);
  if (hub.tunnel_checked && (hub.tunnel === null || hub.tunnel === "unknown")) found.push("This machine's tunnel to the hub: its state did not read");
  else if (hub.tunnel_checked && hub.tunnel !== "active") found.push(`This machine's tunnel to the hub is down: systemd calls it ${hub.tunnel}`);
  if (hub.service && hub.service.error) found.push(`The hub's own state did not read: ${hub.service.error}`);
  if (!hub.error) {
    for (const provider of new Set(hub.accounts.map((account) => account.provider))) {
      const theirs = hub.accounts.filter((account) => account.provider === provider);
      if (!theirs.some((account) => account.state === "working")) found.push(`No ${hubProvider(provider)} account is working`);
    }
  }
  return found;
}

function accountsWords(accounts) {
  if (!accounts.length) return ["no accounts"];
  const inState = (state) => accounts.filter((account) => account.state === state);
  const working = inState("working").length;
  const resting = inState("cooling");
  const ends = resting.filter((account) => account.state_until).map((account) => Date.parse(account.state_until));
  const until = ends.length ? hubWhen(new Date(Math.min(...ends)).toISOString()) : "";
  const restingWords = resting.length === 1
    ? `1 resting${until ? ` until ${until}` : ""}`
    : `${resting.length} resting${until ? `, the first until ${until}` : ""}`;
  return [
    `${working} of ${plural(accounts.length, "account", "accounts")} working`,
    resting.length > 0 && restingWords,
    inState("disabled").length > 0 && `${inState("disabled").length} turned off`,
    inState("signed_out").length > 0 && `${inState("signed_out").length} signed out`,
  ].filter(Boolean);
}

function renderStatus(hub) {
  const states = hub.error ? [] : hub.accounts.map((account) => [account.provider, account.state, account.state_until]);
  // The day is part of it: a rest's end reads as its clock today and with its date on another day.
  show("hub-health", [hub.error, hub.tunnel, hub.service && hub.service.error, states, new Date().toDateString()], () => {
    const wrong = troubles(hub);
    const words = [...wrong, ...(wrong.length ? [] : ["Up"]), ...(hub.error ? [] : accountsWords(hub.accounts))];
    return h("p", { class: `hub-status ${wrong.length ? "trouble" : "ok"}` },
      h("span", { class: "hub-lamp", "aria-hidden": "true" }),
      h("span", { class: "hub-status-words", text: words.join(", ") }));
  });
}

// The accounts.

// The requests the hub sent through an account in each of its last ten-minute spans: one column a span, as tall as
// the span's share of the busiest one, the failed ones on top in red. The summary is on hover.
function trafficChart(spans) {
  if (!spans.length) return h("div", { class: "hub-traffic", title: "No recent requests", "aria-label": "No recent requests" });
  const succeeded = spans.reduce((sum, span) => sum + span.succeeded, 0);
  const failed = spans.reduce((sum, span) => sum + span.failed, 0);
  const most = Math.max(1, ...spans.map((span) => span.succeeded + span.failed));
  const words = `${plural(succeeded + failed, "request", "requests")} since ${clock(spans[0].at)}${failed ? `, ${count(failed)} failed` : ""}`;
  // A span with a request shows at least 2 pixels, however busy the busiest span was.
  const tall = (requests) => (requests ? `max(2px, ${(requests / most) * 100}%)` : "0");
  const columns = succeeded + failed === 0 ? [] : spans.map((span) => {
    const good = h("span", { class: "hub-span-succeeded" });
    good.style.height = tall(span.succeeded);
    const bad = h("span", { class: "hub-span-failed" });
    bad.style.height = tall(span.failed);
    return h("span", { class: "hub-span", title: hubTip(`Ten minutes from ${clock(span.at)}: ${count(span.succeeded)} succeeded, ${count(span.failed)} failed`) }, bad, good);
  });
  return h("div", { class: "hub-traffic", title: hubTip(words), "aria-label": words },
    h("div", { class: "hub-spans", role: "img", "aria-label": words }, columns),
    h("span", { class: "hub-traffic-failed", text: failed ? `${count(failed)} failed` : "" }));
}

// An access token that ran out; one that still runs says nothing.
function tokenLine(account) {
  if (!account.token_expires_at || new Date(account.token_expires_at).getTime() > Date.now()) return null;
  return h("span", { class: "hub-fact", title: moment(account.token_expires_at), text: `Access token ran out ${hubWhen(account.token_expires_at)}` });
}

// The key for the change the account's state calls for, or null for a working account: ending its rest, turning it on,
// or signing it in again, which only a provider the setup gives a sign-in command offers. `hub`: the hub as read.
function mainKey(account, hub) {
  const short = hubShortName(account);
  const key = (text, title, disabled, press) => h("button", { class: "button small primary", type: "button", title, disabled, onclick: press }, text);
  if (account.state === "cooling") {
    return key("End the rest", "The hub sends it requests again at once", Boolean(hubUi.busy),
      () => change(`Ending the rest of ${short}`, "/api/hub/accounts/end-rest", { id: account.id }, TRY_MS));
  }
  if (account.state === "disabled") {
    return key("Turn on", "The hub sends it requests again", Boolean(hubUi.busy),
      () => change(`Turning on ${short}`, "/api/hub/accounts/disabled", { id: account.id, disabled: false }, TRY_MS));
  }
  if (account.state === "signed_out" && account.provider in HUB_PROVIDERS && account.provider in hub.sign_in) {
    return key("Sign in again", `Starts a new ${hubProvider(account.provider)} sign-in`,
      Boolean(hubUi.busy) || signingIn(hub, account.provider),
      () => startSignIn(account.provider));
  }
  return null;
}

// The account's other controls, at the row's end: turning it off while it is on, and signing it out
// after a question.
function moreMenu(account) {
  const short = hubShortName(account);
  const back = `#${CSS.escape(`account-${account.id}`)} .hub-more > summary`;
  return menu("hub-more",
    h("summary", { class: "button small icon-only", title: "More", "aria-label": `More for ${short}` }, Icons.icon("more")),
    [
      account.state !== "disabled" && {
        icon: "ban", text: "Turn off", title: "The hub sends it no requests and keeps its sign-in", disabled: Boolean(hubUi.busy),
        press: () => change(`Turning off ${short}`, "/api/hub/accounts/disabled", { id: account.id, disabled: true }, TRY_MS),
      },
      {
        icon: "logout", text: "Sign out", title: "The hub deletes its sign-in", disabled: Boolean(hubUi.busy),
        press: () => confirmChange(`Sign ${short} out of the hub?`, "The hub deletes its sign-in. Only a new sign-in brings it back.", "Sign out", back,
          () => change(`Signing out ${short}`, "/api/hub/accounts/sign-out", { id: account.id }, TRY_MS)),
      },
    ]);
}

// Traffic holds the same column in every row; an expired token stays with its account.
function accountFoot(account) {
  return [
    trafficChart(account.traffic),
    tokenLine(account),
  ];
}

function signingIn(hub, provider) {
  return Boolean(hub.sign_in[provider] && hub.sign_in[provider].waiting);
}

// Add account stays beside Usage; pending sign-ins sit above the allowance graph.
function renderAccountActions(hub) {
  show("hub-add-account", [hub.error, hub.sign_in, hubUi.busy], () => {
    const providers = Object.keys(HUB_PROVIDERS).filter((provider) => provider in hub.sign_in);
    return !hub.error && providers.length > 0 && menu("hub-add",
      h("summary", { class: "button small primary" }, Icons.icon("plus"), "Add account"),
      providers.map((provider) => ({
        icon: provider, text: `${hubProvider(provider)} account`, title: `Signs a ${hubProvider(provider)} account into the hub`,
        disabled: Boolean(hubUi.busy) || signingIn(hub, provider), press: () => startSignIn(provider),
      })));
  });
  show("hub-sign-ins", [hub.error, hub.sign_in], () => {
    const open = hub.error ? [] : Object.keys(HUB_PROVIDERS)
      .filter((provider) => hub.sign_in[provider] && (hub.sign_in[provider].waiting || hub.sign_in[provider].error));
    return open.length ? h("div", { class: "cards hub-sign-ins" },
      open.map((provider) => signInCard(provider, hub.sign_in[provider]))) : [];
  });
}

function renderAccountList(hub) {
  const waiting = Object.keys(HUB_PROVIDERS).map((provider) => signingIn(hub, provider));
  // The minute is part of what the rows are drawn from: a countdown, limits growing old and a token running out
  // change with the clock alone.
  show("hub-account-list", [hub.error, hub.accounts, hub.read_at, waiting, Math.floor(Date.now() / 60e3), hubUi.busy], () => {
    if (!hub.accounts.length) return h("p", { class: "note", text: hub.error ? "Unknown while the hub does not answer." : "The hub holds no account." });
    const columns = HUB_LIMIT_COLUMNS.filter((column) => hub.accounts.some((account) => hubLimits(account).some((row) => row.key === column.key)));
    const list = h("div", { class: "hub-accounts" });
    // Even an all-unknown list reserves one track for its explanation.
    const tracks = columns.length ? columns.map((column) => column.key === "opus" ? "minmax(100px, 1.1fr)" : "minmax(78px, 1fr)") : ["minmax(0, 1fr)"];
    list.style.setProperty("--hub-columns", `minmax(190px, 2fr) ${tracks.join(" ")} minmax(100px, 1fr) 132px`);
    list.style.setProperty("--hub-limit-end", String(tracks.length + 2));
    list.append(h("div", { class: "hub-account-columns" },
      h("span", { class: "hub-column-account", text: "Account" }),
      columns.length ? columns.map((column) => h("span", { "data-limit": column.key, title: column.title, text: column.name })) : h("span"),
      h("span", { class: "hub-column-requests", title: "Each bar is ten minutes, the newest at the right", text: "Requests" }), h("span")));
    for (const account of hub.accounts) {
      const key = !hub.error && mainKey(account, hub);
      list.append(hubAccountCard(account,
        hub.error ? null : [key && h("div", { class: "hub-keys" }, key), moreMenu(account)],
        accountFoot(account), columns));
    }
    return [
      // While the hub does not answer, the rows of Cockpit's last reading show without their controls.
      hub.error && h("p", { class: "note hub-old", text: `The hub does not answer, so these are the figures from ${moment(hub.read_at)}.` }),
      list,
    ];
  });
}

// Signing an account in.

function copyKey(code) {
  return async (event) => {
    const pressed = event.currentTarget;
    try {
      await navigator.clipboard.writeText(code);
    } catch (error) {
      logError("copying the Codex code", error);
      hubBanner("The code did not copy: select it and copy it by hand");
      return;
    }
    if (pressed.tagName === "BUTTON") pressed.textContent = "Copied the code";
  };
}

function codexWaiting(waiting) {
  const field = h("input", { class: "field codex-code", type: "text", readonly: true, "data-credential": true, autocomplete: "off",
    spellcheck: "false", "aria-label": "Enter this code on OpenAI's page to sign a Codex account into the hub", onfocus: (event) => event.target.select() });
  field.value = waiting.code;
  const copy = copyKey(waiting.code);
  return h("div", { class: "codex-sign-in" },
    h("p", { class: "codex-sign-in-note", text: "Enter this code on OpenAI's page." }),
    field,
    h("div", { class: "codex-sign-in-keys" },
      h("a", { class: "button primary", href: waiting.url, target: "_blank", rel: "noopener noreferrer", text: "Copy the code and open OpenAI's page", onclick: copy }),
      h("button", { class: "button ghost", type: "button", onclick: copy }, "Copy the code")),
    h("p", { class: "codex-sign-in-note", text: `Device-code sign-in must be on in ChatGPT's security settings. OpenAI signs in whichever account your browser has open. The code ends at ${clock(waiting.ends_at)}.` }));
}

function claudeWaiting(waiting) {
  if (waiting.checking) return h("p", { class: "codex-sign-in-note", text: "The hub is checking the address from Claude's page." });
  const field = h("input", { class: "field hub-address", type: "text", "data-credential": true, autocomplete: "off", spellcheck: "false",
    "aria-label": "Address of the page Claude sent you to", placeholder: "http://localhost:…",
    oninput: (event) => { hubUi.address = event.target.value; } });
  field.value = hubUi.address;
  return h("form", { class: "codex-sign-in", onsubmit: (event) => { event.preventDefault(); sendAddress(field.value.trim()); } },
    h("p", { class: "codex-sign-in-note", text: "Sign in on Claude's page and press Authorize. Claude then sends you to a localhost page that does not load: paste its whole address here." }),
    field,
    h("div", { class: "codex-sign-in-keys" },
      h("button", { class: "button primary", type: "submit" }, "Send the address to the hub"),
      h("a", { class: "button ghost", href: waiting.url, target: "_blank", rel: "noopener noreferrer", text: "Open Claude's page again" })),
    h("p", { class: "codex-sign-in-note", text: `The sign-in ends at ${clock(waiting.ends_at)}.` }));
}

// A sign-in that waits for the user, or how the last one ended.
function signInCard(provider, status) {
  const name = hubProvider(provider);
  const waiting = status.waiting;
  return h("div", { class: "spend-card hub-sign-in" },
    h("div", { class: "hub-account-who", text: `Signing in a ${name} account` }),
    waiting ? (provider === "codex" ? codexWaiting(waiting) : claudeWaiting(waiting))
      : h("p", { class: "hub-said", text: `The last ${name} sign-in ended: ${status.error}` }));
}

// Starts the hub's sign-in for a provider. Claude's page opens in a tab made at the press, which the browser lets
// through; the link goes into it once the hub gave it. Codex's code then shows on its card.
async function startSignIn(provider) {
  const tab = provider === "claude" ? window.open("", "_blank") : null;
  const data = await change(`Starting the ${hubProvider(provider)} sign-in`, `/api/hub/${provider}/sign-in`, {}, TRY_MS);
  if (!tab) return;
  if (!data) {
    tab.close();
    return;
  }
  tab.opener = null;
  tab.location.href = data.url;
}

// The address Claude sent the browser to holds the sign-in's code, which works once: it is sent once from this page
// and never kept in the browser's storage for a later try, the way other changes are.
async function sendAddress(address) {
  if (!address) return;
  hubUi.busy = "Sending the address to the hub";
  renderHub();
  try {
    const response = await fetch("/api/hub/claude/callback", jsonPost({ address }));
    answered(response, await answerJson(response));
    hubUi.address = "";
    hubBanner("");
  } catch (error) {
    logError("sending the hub the address from Claude's page", error);
    hubBanner(`The hub did not take the address: ${error.message}`);
  }
  hubUi.busy = "";
  loadHub();
}

// The requests the hub answered with an error, when there are any, under the hub's service in its fold.

function renderFailed(hub) {
  const failed = hub.service && hub.service.failed_requests;
  show("hub-failed", failed, () => {
    if (!failed || !failed.count) return [];
    const read = failed.shown.reduce((sum, each) => sum + each.times, 0);
    const shown = read < failed.count ? `, newest ${read} shown` : "";
    return [
      head("failed-title", "Failed requests", `${count(failed.count)} in the hub's log${shown}`),
      h("div", { class: "table-scroll" },
        h("table", { class: "hub-failed" },
          h("thead", null, h("tr", null, h("th", { text: "Last" }), h("th", { text: "Times" }), h("th", { text: "Answer" }), h("th", { text: "Request" }))),
          h("tbody", null, failed.shown.map((each) => h("tr", null,
            h("td", { title: moment(each.at), text: hubWhen(each.at) }),
            h("td", { class: "hub-times", text: count(each.times) }),
            h("td", { class: "hub-answer", text: each.status ?? "–" }),
            h("td", { class: "hub-path", text: [each.method, each.path].filter(Boolean).join(" ") || "–" })))))),
    ];
  });
}

// The hub's service, in the fold of that name: how it shares requests among accounts, its version, its client keys,
// and the restarts the setup gives. Its state is null while Cockpit's first read after a start has the accounts and not yet the hub's own state, and
// unknown while the hub does not answer; the restarts are there all the same.

function routingRow(routing) {
  const now = ROUTINGS.find((each) => each.value === routing);
  return [
    h("div", { class: "seg hub-routing", role: "group", "aria-label": "How the hub shares requests among accounts" },
      ROUTINGS.map((each) => h("button", {
        type: "button", class: each.value === routing && "on", "aria-pressed": String(each.value === routing), title: each.about,
        disabled: Boolean(hubUi.busy),
        onclick: () => each.value !== routing && change(`Sharing requests by ${each.name.toLowerCase()}`, "/api/hub/routing", { routing: each.value }, TRY_MS),
      }, each.name))),
    h("p", { class: "note hub-routing-about", text: now ? now.about : `The hub shares requests by ${routing}, which this page does not know.` }),
  ];
}

// Says something only when the hub does not run its newest release.
function versionLine(service) {
  if (service.error || service.latest_version === service.version) return "";
  return service.latest_version ? `${service.latest_version} is out` : "Newest release unknown";
}

// The restarts the setup gives a command for, in the order of hub.restarts.
function restartKeys(restarts) {
  const key = (text, what, question, consequence) => h("button", {
    class: "button small", type: "button", "data-restart": what, disabled: Boolean(hubUi.busy),
    onclick: () => confirmChange(question, consequence, text, `[data-restart="${what}"]`,
      () => change(`Restarting the ${what}`, "/api/hub/restart", { what }, RESTART_TRY_MS)),
  }, text);
  const keys = {
    hub: () => key("Restart the hub", "hub", "Restart the hub's service?",
      "Requests on their way through the hub are cut off, also those of sessions on any machine that run through it now."),
    tunnel: () => key("Restart the tunnel", "tunnel", "Restart this machine's tunnel to the hub?",
      "This machine's requests on their way through the hub are cut off."),
  };
  return h("div", { class: "hub-restart" }, restarts.map((what) => keys[what]()));
}

function renderService(hub) {
  const service = hub.error ? null : hub.service;
  show("hub-service", [Boolean(hub.error), service && [service.routing, service.version, service.latest_version, service.client_keys, service.error], hub.restarts, hubUi.busy], () => {
    const row = (name, ...value) => h("div", { class: "hub-service-row" }, h("dt", { text: name }), h("dd", null, value));
    const newer = service && versionLine(service);
    return h("dl", { class: "hub-service" },
      service && service.routing && row("Sharing", routingRow(service.routing)),
      service && row("Version", service.version || "–", newer && h("span", { class: "hub-version-new", text: newer })),
      service && service.client_keys !== null && row("Client keys", count(service.client_keys)),
      hub.restarts.length > 0 && row("Restart", restartKeys(hub.restarts)));
  });
}

function renderHub() {
  const hub = hubUi.hub;
  const read = document.getElementById("hub-read");
  if (hub === undefined) return;
  for (const part of document.querySelectorAll(".hub-part")) part.hidden = hub === null;
  if (hubUi.none !== (hub === null)) {
    hubUi.none = hub === null;
    redrawLimits();
  }
  if (hub === null) return;
  if (hub.accounts === null) {
    read.textContent = "Reading the hub";
    return;
  }
  read.textContent = hubUi.busy ? `${hubUi.busy}…` : `Read ${clock(hub.read_at)}`;
  read.classList.toggle("busy", Boolean(hubUi.busy));
  renderStatus(hub);
  renderAccountActions(hub);
  renderAccountList(hub);
  renderFailed(hub);
  renderService(hub);
}

// One change at a time: each waits for Cockpit's answer to the one before. Cockpit answers with the hub as read after
// the change, its sign-ins left out, which the page shows at once. Gives Cockpit's answer, or null when the change
// failed.
let changing = Promise.resolve();
function change(what, path, body, tryMs) {
  const sent = changing.then(() => sendChange(what, path, body, tryMs));
  changing = sent;
  holdLeave(sent);
  return sent;
}

async function sendChange(what, path, body, tryMs) {
  hubUi.busy = what;
  renderHub();
  let data = null;
  try {
    const sent = await sendAction(path, jsonPost(body), tryMs, null);
    data = answered(sent.response, sent.data);
    hubBanner("");
  } catch (error) {
    logError(`${what.toLowerCase()} on the hub`, error);
    hubBanner(`${what} failed: ${error.message}`);
  }
  hubUi.busy = "";
  if (data && data.hub) {
    // Counted as a read, so the answer to a read asked before the change is dropped.
    hubUi.loads += 1;
    // The answer leaves the sign-ins out, so the ones the page holds stay until its next read.
    hubUi.hub = { ...data.hub, sign_in: hubUi.hub.sign_in };
    renderHub();
    again(HUB_POLL_MS);
  } else {
    loadHub();
  }
  return data;
}

function again(wait) {
  clearTimeout(hubTimer);
  hubTimer = setTimeout(loadHub, wait);
}

async function loadHub() {
  clearTimeout(hubTimer);
  const mine = ++hubUi.loads;
  let wait = HUB_POLL_MS;
  try {
    const data = await api("/api/hub");
    if (mine !== hubUi.loads) return;
    hubUi.hub = data.hub;
    if (data.hub === null) wait = NO_HUB_POLL_MS;
    else if (data.hub.accounts === null) wait = FIRST_READ_MS;
    if (hubUi.unread) hubBanner("");
    hubUi.unread = false;
    renderHub();
  } catch (error) {
    if (mine !== hubUi.loads) return;
    logError("loading the hub", error);
    for (const part of document.querySelectorAll(".hub-part")) Loading.failed(part);
    hubUi.unread = true;
    hubBanner(`The hub's accounts did not load: ${error.message}`);
  }
  if (mine === hubUi.loads) again(wait);
}

// The question dialog: Escape, a click beside its panel and Cancel close it without the change. Escape is taken here,
// so a page shown in Cockpit's centre frame stays open (center-frame.js). Its close draws what the poll brought
// meanwhile and gives the focus back to the key that asked.
function wireConfirm() {
  const dialog = confirmDialog();
  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    dialog.close();
  });
  dialog.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    event.preventDefault();
    dialog.close();
  });
  dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.close(); });
  dialog.addEventListener("close", () => {
    renderHub();
    const back = confirmBack && document.querySelector(confirmBack);
    if (back) back.focus();
  });
}

// A press outside a menu in the page closes it; keyboard.js closes it on Escape.
document.addEventListener("click", (event) => {
  const path = event.composedPath();
  for (const open of document.querySelectorAll("main details.menu[open]")) {
    if (!path.includes(open)) open.open = false;
  }
});

// The hub's places on the Usage page, among the page's own. Those in the page's main column have class usage-part, so
// a period switch does not fade them and a failed usage load leaves their ghosts alone (usage.js, usage.css). The
// machine's own limits wait, hidden, until the hub says whether there is one.
function hubPlaces() {
  document.getElementById("banner").insertAdjacentHTML("afterend", '<div id="hub-banner" class="banner-area"></div>');
  document.querySelector(".spend-head").insertAdjacentHTML("beforeend",
    '<div id="hub-add-account" class="hub-part"></div>');
  const limits = document.getElementById("spend-limits");
  limits.insertAdjacentHTML("beforebegin", `
    <div class="hub-top hub-part usage-part">
      <section id="hub-health" aria-label="The hub's state"><div class="ghost-box ghost-usage" data-ghost role="status" aria-label="Loading the hub's state"><span class="ghost-bar w-80"></span><p class="note ghost-note"></p></div></section>
      <span id="hub-read" class="hub-read"></span>
    </div>
    <section id="hub-sign-ins" class="hub-part usage-part" aria-label="Account sign-ins" aria-live="polite"></section>
    <section id="hub-left" class="hub-left usage-part" aria-labelledby="left-title" hidden></section>
    <section id="hub-accounts" class="hub-part usage-part" aria-labelledby="accounts-title">
      <div class="section-head"><h2 id="accounts-title">Accounts</h2></div>
      <div id="hub-account-list"><div class="ghost-box ghost-usage" data-ghost role="status" aria-label="Loading the hub's accounts"><span class="ghost-bar tall w-30"></span><span class="ghost-gap"></span><span class="ghost-bar w-90"></span><span class="ghost-bar w-80"></span><span class="ghost-bar w-90"></span><p class="note ghost-note"></p></div></div>
    </section>`);
  limits.hidden = true;
  limits.replaceChildren();
  document.getElementById("spend-codex").insertAdjacentHTML("afterend",
    '<section id="spend-left" aria-labelledby="left-table-title" hidden></section>');
  document.querySelector(".spend-method-fold").insertAdjacentHTML("beforebegin", `
    <details class="spend-more hub-fold hub-part usage-part">
      <summary>Hub service</summary>
      <section id="hub-service" aria-label="The hub's service"></section>
      <section id="hub-failed" aria-labelledby="failed-title"></section>
    </details>`);
  document.body.insertAdjacentHTML("beforeend",
    '<dialog id="hub-confirm" class="time-dialog" aria-labelledby="hub-confirm-title"></dialog>');
}

hubPlaces();
window.cockpitUsagePart = {
  ownsLimits: () => hubUi.hub !== null,
  start: () => {
    wireConfirm();
    loadHub();
  },
};
