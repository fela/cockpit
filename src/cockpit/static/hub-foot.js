"use strict";

const HUB_PROVIDERS = { codex: "Codex", claude: "Claude" };

// The weekly limits the CLIProxyAPI hub's accounts have (SPEC UI-84, hub.py). While a hub is configured the foot shows
// these alone: Claude's week for all models and Codex's week. The hover card adds Fable's own weekly limit and the week
// left for Opus once Fable has used all its allowance. `week` picks a family's week from one of its provider's
// accounts.
const HUB_FABLE = { key: "fable", name: "Fable", mark: () => letterMark("F", "fable"), provider: "claude", week: (account) => account.model_weeks.find((week) => week.model === "Fable") };
const HUB_OPUS = { key: "opus", name: "Opus after Fable", mark: () => letterMark("O", "opus"), provider: "claude", week: (account) => account.opus_week };
const HUB_CLAUDE = { key: "claude", name: "All Claude models", mark: () => claudeMark(), provider: "claude", week: (account) => account.week };
const HUB_CODEX = { key: "codex", name: "Codex", mark: () => codexMark(), provider: "codex", week: (account) => account.week };
const HUB_FOOT = [HUB_CLAUDE, HUB_CODEX];

// Each of a family's accounts with its week. An account the hub has sent no request through, or whose week has reset
// since its last one, has no figure: its week is null or has no elapsed share.
function hubAccountWeeks(hub, fam) {
  return (hub.accounts || []).filter((account) => account.provider === fam.provider)
    .map((account) => ({ account, week: fam.week(account) }));
}

const hubKnown = (week) => Boolean(week) && Number.isFinite(week.elapsed);

// A family's weeks that have a figure, each with its account.
function hubWeeks(hub, fam) {
  return hubAccountWeeks(hub, fam).filter(({ week }) => hubKnown(week));
}

// The foot while a hub is configured: a link to the Usage page holding Claude's and Codex's marks and lanes,
// then a hairline and the host's lamp. Each lane has one bar per hub account of its provider, so an account stays in
// the foot while its figure is missing. A family the hub has no account for is its mark alone. A week keeps
// its colours however old its figure is, since only a request through the hub changes it; the week of an account the
// hub is resting or cannot use is dimmed, and an account without a figure is a dimmed empty track. A red sign follows
// the lanes while the hub cannot be reached.
function hubCaps(hub, machine) {
  const families = HUB_FOOT.map((fam) => ({ fam, weeks: hubAccountWeeks(hub, fam) }));
  const share = (week) => (hubKnown(week) ? `${week.used}%` : "no figure");
  const said = families.map(({ fam, weeks }) => `${fam.name} ${weeks.length ? weeks.map(({ week }) => share(week)).join(" and ") : "no account"}`).join("; ");
  const pill = h("a", { class: "usage caps", href: "/usage", "aria-label": `Weekly limits. ${said}.${hub.error ? " The hub cannot be reached." : ""} Opens Usage.`, "data-tip-card": "usage" },
    families.map(({ fam, weeks }) => (weeks.length
      ? family(fam.key, fam.mark(), weekLane(fam.key, weeks.map(({ account, week }) => ({ week, stale: account.state !== "working" || !hubKnown(week) })), 0.46))
      : h("span", { class: `fam ${fam.key} bare` }, fam.mark()))),
    hub.error && h("span", { class: "hub-fail" }, icon("alert")));
  pill.tipCard = () => hubCard(hub);
  const lamp = machineLamp(machine);
  return lamp ? [pill, h("span", { class: "fam-sep" }), lamp] : [pill];
}

// An account in few letters: its address up to the @.
function hubShortName(account) {
  const name = (account.email || account.id).split("@")[0];
  return account.plan_label ? `${account.plan_label} · ${name}` : name;
}

// The foot's hover card while a hub is configured: each weekly limit's mark and name on a heading line of its own,
// then one line per account that has a figure for it, in the order of the foot's bars: the account's short name, its
// bar, the share used and how long until it resets. A limit no account has a figure for says so on its heading line.
// Below, one short line for each thing that is wrong: the hub out of reach, or an account it is not using.
function hubCard(hub) {
  const accounts = hub.accounts || [];
  const rows = [];
  for (const fam of accounts.length ? [HUB_CLAUDE, HUB_FABLE, HUB_OPUS, HUB_CODEX] : []) {
    const weeks = hubWeeks(hub, fam);
    const head = rows.length ? "hc-group" : "";
    rows.push(h("span", { class: `hc-mark ${head}` }, fam.mark()), h("span", { class: `hc-name ${head}`, text: fam.name }));
    if (!weeks.length) rows.push(h("span", { class: `hc-none ${head}`, text: "no figure yet" }));
    for (const { account, week } of weeks) {
      rows.push(
        h("span", { class: "hc-account", text: hubShortName(account) }),
        usageBar(week),
        h("span", { class: `hc-used ${week.used >= 100 ? "full" : week.pace}`, text: `${week.used}%` }),
        h("span", { class: "hc-resets", text: countdown(new Date(week.resets_at)) }));
    }
  }
  const notes = [];
  if (hub.error) notes.push(accounts.length ? `Hub unreachable. Figures from ${ago(hub.read_at)}.` : "Hub unreachable.");
  else if (hub.accounts === null) notes.push("Reading the hub…");
  else if (!accounts.length) notes.push("No accounts on the hub yet.");
  for (const account of accounts) {
    const who = `${HUB_PROVIDERS[account.provider] || account.provider} ${hubShortName(account)}`;
    if (account.state === "cooling") notes.push(account.state_until ? `${who} rests until ${clock(account.state_until)}.` : `${who} is resting.`);
    else if (account.state === "disabled") notes.push(`${who} is turned off on the hub.`);
    else if (account.state === "signed_out") notes.push(`${who} is signed out. Sign it in from Inbox.`);
    else if (HUB_FOOT.some((fam) => fam.provider === account.provider && !hubKnown(fam.week(account)))) {
      notes.push(`${who} has no figure until its next request.`);
    }
  }
  return h("div", { class: "usage-card hub-card" },
    rows.length > 0 && h("div", { class: "hc-rows" },
      h("span", { class: "hc-head hc-head-used", text: "used" }), h("span", { class: "hc-head", text: "resets in" }), rows),
    notes.map((words) => h("span", { class: "uc-warn", text: words })));
}

// The warning of the hub's sign-in for `provider`, "codex" or "claude", and what its start is busy under.
function hubSignInKey(provider) {
  return `hub-sign-in:${provider}`;
}

const HUB_CLAUDE_FIELD = "Address of the page Claude sent you to";

// While the hub's Codex device login waits: OpenAI's link and code, for the account Codex on this machine reaches
// through the hub (SPEC UI-81). The hub takes the account in when the code is entered, and the card closes.
function hubCodexSignInWarning(hub) {
  const waiting = hub.sign_in.codex && hub.sign_in.codex.waiting;
  if (!waiting || !waiting.code) return null;
  return { key: hubSignInKey("codex"), tone: "soon", mark: "clock", tip: null, more: null, fix: null, close: null,
    short: "Signing a Codex account into the hub",
    title: "Enter this code on OpenAI's page to sign a Codex account into the hub",
    extra: signInCodeBlock("Code for the hub's Codex sign-in", waiting.url, waiting.code,
      `OpenAI signs in whichever account your browser has open. The code ends at ${clock(waiting.ends_at)}. This card closes when the hub has the account.`) };
}

// While the hub's Claude login waits: Claude sends the browser back to an address on the hub's own machine
// (localhost:54545), which does not open in the user's browser. The user copies that address into this card's field
// (withSignIn), and the hub signs the account in with the code it carries.
function hubClaudeSignInWarning(hub) {
  const waiting = hub.sign_in.claude && hub.sign_in.claude.waiting;
  if (!waiting || !waiting.url) return null;
  const key = hubSignInKey("claude");
  return { key, tone: "soon", mark: "clock", tip: null, close: null, extra: null,
    short: waiting.checking ? "The hub is signing a Claude account in" : "Signing a Claude account into the hub",
    title: waiting.checking ? "The hub is checking the address from Claude's page" : "Copy the address Claude sends you to into this card",
    more: `On Claude's page, sign in with the account the hub should use and press Authorize. Claude then sends you to a page on localhost that does not load: copy that page's whole address from the address bar into this card. The sign-in ends at ${clock(waiting.ends_at)}.`,
    fix: waiting.checking ? null : { words: "Paste the address", primary: true, busy: false, onclick: () => waitForHubClaude(waiting.url) } };
}

function waitForHubClaude(url) {
  waitForCode({
    key: hubSignInKey("claude"), url, page: "Claude's page", label: HUB_CLAUDE_FIELD,
    send: async (address) => {
      try {
        await apiCredential("/api/hub/claude/callback", { address });
      } catch (error) {
        throw refused("The hub did not sign the Claude account in", error);
      }
      return "Signed in: the hub has the new Claude account";
    },
  });
}

// A hub account whose sign-in OpenAI or Anthropic ended: the hub renews every sign-in by itself, so this shows only
// once a renewal was refused. The account signs in again from the warning.
function hubSignedOutWarning(hub, account) {
  const name = account.email || account.id;
  const provider = HUB_PROVIDERS[account.provider] || account.provider;
  const offered = account.provider in hub.sign_in;
  const waiting = Boolean(offered && hub.sign_in[account.provider].waiting);
  const key = hubSignInKey(account.provider);
  const again = !offered ? "" : account.provider === "codex"
    ? (waiting ? " Enter the code from the hub's sign-in warning on OpenAI's page."
      : " While this page is open, Cockpit starts the new sign-in by itself and shows the code to enter on OpenAI's page.")
    : account.provider === "claude" && waiting ? " Finish the sign-in from the hub's Claude sign-in warning." : "";
  return { key: `hub-signed-out:${account.id}`, tone: "fail", mark: "alert", tip: null, close: null, extra: null,
    short: `${provider} ${name} is signed out of the hub`,
    title: `${provider} ${name} is signed out of the hub`,
    more: `The hub could not renew its sign-in, so tools reaching it through the hub use its other accounts.${again}`,
    fix: offered && !waiting
      ? { words: ui.busy.has(key) ? "Starting…" : "Sign in again", primary: true, busy: ui.busy.has(key), onclick: () => startHubSignIn(account.provider, true) }
      : null };
}

// While the hub holds no account of a provider the setup gives a sign-in, a way in from Inbox rather than only
// from the hover card.
const HUB_NO_ACCOUNT_HOW = {
  codex: "Sign one in so Codex can reach it through the hub. First turn on device-code sign-in in ChatGPT's security settings.",
  claude: "Sign one in so Claude Code can reach it through the hub.",
};

function hubNoAccountWarning(hub, provider) {
  if (!(provider in hub.sign_in)) return null;
  const { waiting, error } = hub.sign_in[provider];
  if (hub.error || !hub.accounts || waiting || hub.accounts.some((account) => account.provider === provider)) return null;
  const name = HUB_PROVIDERS[provider];
  const key = hubSignInKey(provider);
  return { key: `hub-no-${provider}`, tone: "soon", mark: "alert", tip: null, close: null, extra: null,
    short: `The hub has no ${name} account`,
    title: `The hub has no ${name} account yet`,
    more: `${error ? `The last sign-in ended: ${error} ` : ""}${HUB_NO_ACCOUNT_HOW[provider]}`,
    fix: { words: ui.busy.has(key) ? "Starting…" : `Sign in a ${name} account`, primary: true, busy: ui.busy.has(key), onclick: () => startHubSignIn(provider, true) } };
}

// Requests through the hub failing in numbers, as when one session runs many calls at once and the provider limits
// every account: sessions get refused while each account still looks signed in. The hub keeps traffic in 10-minute
// spans; the last 20 minutes are the span under way and the one before.
const HUB_FAILING_MS = 20 * 60 * 1000;
const HUB_FAILING_COUNT = 20;
const HUB_FAILING_SHARE = 0.25;

function hubFailingWarning(hub, provider) {
  if (!hub.accounts || !hub.read_at) return null;
  const since = Date.parse(hub.read_at) - HUB_FAILING_MS;
  let succeeded = 0, failed = 0;
  for (const account of hub.accounts) {
    if (account.provider !== provider) continue;
    for (const span of account.traffic || []) {
      if (Date.parse(span.at) < since) continue;
      succeeded += span.succeeded;
      failed += span.failed;
    }
  }
  if (failed < HUB_FAILING_COUNT || failed < HUB_FAILING_SHARE * (succeeded + failed)) return null;
  const name = HUB_PROVIDERS[provider];
  return { key: `hub-failing-${provider}`, tone: "fail", mark: "alert", tip: null, close: null, extra: null,
    short: `${name} requests are failing`,
    title: `${name} requests through the hub are failing`,
    more: `${failed} of ${succeeded + failed} ${name} requests failed in the last 20 minutes, so sessions using ${name} get refused. `
      + `Usually one session runs many calls at once and the provider limits every account; that session should run fewer at a time.`,
    fix: { words: "Open Usage", primary: true, busy: false, onclick: () => { location.href = "/usage"; } } };
}

function hubWarnings(hub) {
  if (!hub) return [];
  const list = [hubCodexSignInWarning(hub), hubClaudeSignInWarning(hub)];
  if (hub.accounts) list.push(...hub.accounts.filter((account) => account.state === "signed_out").map((account) => hubSignedOutWarning(hub, account)));
  list.push(...Object.keys(HUB_PROVIDERS).map((provider) => hubNoAccountWarning(hub, provider)));
  list.push(...Object.keys(HUB_PROVIDERS).map((provider) => hubFailingWarning(hub, provider)));
  return list.filter(Boolean);
}

// `open`: whether the sign-in's warning opens once it starts; a press opens it, a start Cockpit makes by itself leaves
// the page as the user has it. A Claude sign-in opens Claude's page in a new tab, which the press itself opens, since a
// browser blocks a tab opened after the answer comes back; the field for the address then waits on its warning.
function startHubSignIn(provider, open) {
  const key = hubSignInKey(provider);
  if (ui.busy.has(key)) return;
  const tab = provider === "claude" ? window.open("", "_blank") : null;
  return run(key, async () => {
    let url;
    try {
      ({ url } = await api("POST", `/api/hub/${provider}/sign-in`));
      if (tab) { tab.opener = null; tab.location.href = url; }
    } catch (error) {
      if (tab) tab.close();
      throw refused(`The hub's ${HUB_PROVIDERS[provider]} sign-in did not start`, error);
    }
    if (provider === "claude") waitForHubClaude(url);
    else if (open) openWarning(key);
  });
}

// A Codex sign-in on the hub that finished since the last state, told once. A Claude sign-in finishes in the press
// that sent its address, which says so itself.
let hubFinishedSeen;
// When this page last started a hub sign-in by itself, as Date.now().
let hubAutoStartedAt = 0;
// A code lasts 15 minutes on the hub (hub.py WAIT_SECONDS); a page starts at most one per that time.
const HUB_AUTO_START_MS = 15 * 60 * 1000;

// A signed-out Codex account on the hub gets its new sign-in started while the user has this page in front of them,
// so the code is waiting when they look. Starting leaves the old sign-in file in place, and the hub keeps one login
// at a time, so several open pages start one. A Claude sign-in needs the user on Claude's page, so it starts from a
// press only.
function noticeHubSignIn(state) {
  const hub = state.hub;
  const codex = hub ? hub.sign_in.codex : undefined;
  const finished = codex ? codex.finished_at : null;
  if (hubFinishedSeen !== undefined && finished && finished !== hubFinishedSeen) toast("Signed in: the hub has the new Codex account");
  hubFinishedSeen = finished;
  const signedOut = hub && hub.accounts && hub.accounts.some((account) => account.provider === "codex" && account.state === "signed_out");
  if (signedOut && codex && !codex.waiting && document.visibilityState === "visible" && !ui.busy.has(hubSignInKey("codex"))
    && Date.now() - hubAutoStartedAt >= HUB_AUTO_START_MS) {
    hubAutoStartedAt = Date.now();
    startHubSignIn("codex", false);
  }
}

// Each part adds to window.cockpitPart, whichever loads first, and the warnings of each add up.
{
  const before = window.cockpitPart || {};
  window.cockpitPart = { ...before,
    // Limits redraw the foot. Sign-ins, failures and account states also redraw the board's warnings.
    boardState({ hub, ...rest }) {
      return [rest, hub && [hub.sign_in, hub.error,
        hub.accounts && hub.accounts.map((account) => [account.id, account.provider, account.state])]];
    },
    usageKey: (state) => state.hub,
    renderUsage(state) {
      if (!state.hub) return false;
      if (state.hub.accounts === null && document.querySelector("#usage .lane")) return true;
      fill("usage", hubCaps(state.hub, state.usage.machine));
      return true;
    },
    notice: noticeHubSignIn,
    warnings: (state) => [...(before.warnings?.(state) || []), ...hubWarnings(state.hub)],
    signInWaitsOn: (state, key) => Boolean(state.hub && Object.keys(HUB_PROVIDERS).some(
      (provider) => key === hubSignInKey(provider) && state.hub.sign_in[provider] && state.hub.sign_in[provider].waiting)),
  };
}
