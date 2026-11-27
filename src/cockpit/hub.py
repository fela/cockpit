"""The user's CLIProxyAPI hub: its accounts for the usage display at the foot of the page (SPEC UI-81), and all of it
with its controls for the Usage page (SPEC UI-85).

CLIProxyAPI (github.com/router-for-me/CLIProxyAPI) serves the user's Claude and Codex subscriptions to tools that point
at it, on this machine or another one this machine reaches, perhaps through a tunnel. The setup file's [hub] table
(config.HubSetup) gives its address and the file holding its management key; Cockpit reads the hub through its
management API with that key. The key stays in that file: Cockpit sends it to the hub and keeps it out of its database,
its log and its pages.

The management API has two sets of routes, under `/v8/management` and `/v0/management`. `/v8/management/credentials`
lists the accounts, each with the requests the hub sent through it in each of the last ten-minute spans, and the limit
headers the provider sent on the account's last request through the hub (`quota`: `signals` and `observed_at`, when
they came) and on its last request for each model (`model_quotas`, one such record per model). Those are the account's
limits here, so they are as old as the account's last request through the hub.

The hub holds those records in memory and each request replaces them, so a record can lack a limit an earlier one
named: Claude names Fable's weekly limit only on requests for Fable, and a restarted hub holds no records at all.
Cockpit therefore keeps each account's last reading of every limit in its database (LIMITS_KEY) until that limit
resets, and a newer record from the hub replaces it (limits_now). Each reading carries the time the provider said it.
Every change in a reading is also kept as a row of its own (hub_left.py), for the Usage page's graph of the allowance
left (SPEC UI-87).

An account that has carried no request yet has no limits in those records. A working Claude or Codex account without
any limit window gets one question to its provider through the hub's relay (`requests/api-call`, which puts that
account's own token where the request says `$TOKEN$`): Claude's usage endpoint (resets.URL) or Codex's (CODEX_USAGE).
Cockpit keeps a record of each account's check in its database (LIMIT_CHECKS_KEY), so its own restarts keep to this
too. Working accounts with a successful check use request headers for subsequent readings:
its next request through the hub brings them. An account left without a limit window, because its provider's answer
named none or because every limit it gave has reset with no request through the hub since, is asked again once its
last check is CHECK_RETRY_SECONDS old: an idle account's usage shows again after each reset, at the cost of one
question per reset. The record outlasts the account's absence from the hub's list, so an account that returns is
asked no sooner either. After a check that failed Cockpit asks again no sooner than CHECK_RETRY_SECONDS later. A question that did not reach the hub is asked again on the next read, logged as a warning
until that account's questions have not reached it for UNREACHED_ALERT_SECONDS. Quota-held accounts are checked every
five minutes, so a reset spent at the provider refreshes the displayed usage. A fresh answer showing a used-up week was
reset and room in every reported limit releases that stale quota hold; a reset seen while another limit is still full is remembered in the check's record
until a later answer shows room in every limit. Failed checks back off for an hour; disabled and signed-out accounts stay untouched.

The hub program's own state comes from `/v0/management`: how it shares requests among accounts (`routing/strategy`),
how many client keys it accepts (`api-keys`), the requests it answered with an error (`request-error-logs`) and the
newest release (`latest-version`). Every answer names the version that runs in its `X-Cpa-Version` header.

The page polls the state every few seconds; a read older than REFRESH_SECONDS starts a new one in a thread and the
page gets the last finished read meanwhile. A read that fails gives the accounts of the last read that worked, with
their kept limits and the failure's words, so the page keeps its numbers while the hub cannot be reached. Before the
first read that works, a failed read gives no accounts. A failed read is logged as an error, except while the hub has
been out of reach for less than UNREACHED_ALERT_SECONDS: a restart of the hub or a stall of the tunnel loses a read or
two, and the next read a minute later works, so those are warnings.

Manual controls belong to the user (the routes in app.py enforce this); confirmed quota-reset recovery runs
automatically. Each manual control changes the hub, reads it again and
gives the page that read: turning an account off or on (`auth-files/status`), signing one out (deleting its file,
`auth-files`), ending an account's rest early (`routing/cooldown/reset`), choosing how requests are shared
(`routing/strategy`), and restarting the hub's service or this machine's tunnel to it, each with the command the setup
gives for it ([hub.restarts]).

Accounts sign in with the hub's own logins, each run with the command the setup gives for its provider ([hub.sign_in]),
which may reach the hub's machine over SSH. Each saves the sign-in in the hub's folder of accounts and exits, and the
hub picks the new file up itself. Neither signs anything out.

- Codex: the device login (`-codex-device-login`) prints OpenAI's link and a one-time code, and waits until the user
  enters the code on OpenAI's page.
- Claude: the login (`-claude-login`) prints Claude's link and waits for Claude to send the browser back to
  localhost:54545 on the hub, which the user's browser cannot reach. The user copies that address from the browser
  instead; Cockpit checks it and types it into the login (`submit_callback`), which reads a pasted address from
  15 seconds after it starts. Claude's login gives up after 5 minutes.

The code and the address stay in this process's memory and reach only the user's page and the login.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, quote, urlsplit
from weakref import WeakValueDictionary

from cockpit import config, db, hub_left, resets, store, usage
from cockpit import model_capacity as capacity

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

REFRESH_SECONDS = 60
PROVIDER_AVAILABILITY_KEY = "hub_provider_availability"
# The app's existing readers, so a background sweeper can keep their snapshots current without a page open.
_availability_readers: WeakValueDictionary[str, Hub] = WeakValueDictionary()
REQUEST_TIMEOUT_SECONDS = 20
V8 = "/v8/management"
V0 = "/v0/management"
# The ways the hub shares requests among a provider's accounts, by the hub's own names.
ROUTINGS = ("round-robin", "weighted-round-robin", "fill-first")
# The hub counts each account's requests in spans this long.
SPAN_SECONDS = 600
# The newest failed requests the page lists.
FAILED_SHOWN = 10
# The hub asks GitHub for its newest release at each `latest-version`, so Cockpit asks once in this long.
LATEST_SECONDS = 3600
TUNNEL_STATE_SECONDS = 5
# How long the hub stays out of reach before a failed read is an error rather than a warning.
UNREACHED_ALERT_SECONDS = 5 * 60
RESTART_SECONDS = 60
# How long Cockpit waits for the hub to answer again after a restart.
BACK_SECONDS = 30
# Codex's headers give each window's length in minutes; these are the two Cockpit draws, by usage.WINDOWS key.
CODEX_WINDOWS = {300: "five_hour", 10080: "seven_day"}
# What Claude's headers call the two windows.
CLAUDE_WINDOWS = {"five_hour": "5h", "seven_day": "7d"}
# Claude's headers name a weekly limit that counts one model's use alone by a code after "7d_". The model each
# code stands for, as Claude's usage answer names the same limit.
FABLE = "Fable"
CLAUDE_MODEL_WINDOWS = {"oi": FABLE}
CLAUDE_MODEL_WINDOW = re.compile(r"anthropic-ratelimit-unified-7d_(\w+)-utilization")
# The key-value entry that holds each account's limits as limits_now gave them on the last read, by "provider/id".
LIMITS_KEY = "hub_limits"
# The share of a Claude account's week that Fable's own weekly limit lets Fable use: the plan caps Fable at half of
# the week. Claude's headers do not state this share, so it is a fixed rule of the plan.
FABLE_SHARE_OF_WEEK = 0.5
# The key-value entry that holds, by "provider/id", each account's last check (Hub._check): {"at": when it asked,
# "error": why it failed, "week_reset": whether a check during the account's quota hold saw its used-up week reset},
# the error "" for a check the provider answered. Records written before `week_reset` existed lack it.
LIMIT_CHECKS_KEY = "hub_limit_checks"
# How long after a failed check Cockpit asks that account's provider again.
CHECK_RETRY_SECONDS = 3600
# A quota hold can outlast a reset the user spent at the provider.
COOLING_CHECK_SECONDS = 5 * 60
# The providers Cockpit asks for an account's limits when it knows none.
CHECKED_PROVIDERS = ("claude", "codex")
# What a check's record says from just before Cockpit sends the question until the answer comes.
CHECK_CUT_OFF = "Cockpit stopped before the answer came"
# The hub's relay sends a request through one account and puts that account's own token where it says $TOKEN$.
RELAY = f"{V8}/requests/api-call"
TOKEN_HEADER = {"Authorization": "Bearer $TOKEN$"}
CODEX_USAGE = "https://chatgpt.com/backend-api/wham/usage"
# The headers the hub's Management Center sends (its codex quota request), which ChatGPT answers.
CODEX_AGENT = "codex-tui/0.149.1 (Mac OS 26.5.2; arm64) iTerm.app/3.6.11 (codex-tui; 0.149.1)"
# Codex's usage answer gives each window's length in seconds.
CODEX_ANSWER_WINDOWS = {18000: "five_hour", 604800: "seven_day"}
LINK_TIMEOUT_SECONDS = 60
# How long each login waits for the user: OpenAI's device code lasts 15 minutes, Claude's login 5.
WAIT_SECONDS = {"codex": 15 * 60, "claude": 5 * 60}
# A login still running this long after its wait ends is stopped.
KILL_GRACE_SECONDS = 60
# How long Cockpit waits for Claude's login to finish once it has the address.
SUBMIT_SECONDS = 45
ESCAPES = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
CODEX_LINK = re.compile(r"Codex device URL:\s*(https://\S+)")
CODEX_CODE = re.compile(r"Codex device code:\s*(\S+)")
CLAUDE_LINK = re.compile(r"https://claude\.ai/oauth/authorize\S+")
# What Claude's login reads from its input: the address in the form its own callback server would have received.
CLAUDE_CALLBACK = "http://localhost:54545/callback?code={code}&state={state}\n"
# What each of the hub's logins prints, lower-cased, once it saved the account: "Codex device authentication successful!"
# or "Claude authentication successful!". Before saving, the logins print the same words without the "!", and a failed
# save after that still exits 0.
SIGNED_IN = "authentication successful!"
LONG_RUN = re.compile(r"[A-Za-z0-9_\-.~+/=]{20,}")
# A long run of lower-case words joined by underscores, such as OAuth's `temporarily_unavailable`, alone or after a
# log's `error=`, names an error.
ERROR_NAME = re.compile(r"(?:error=)?[a-z]+(?:_[a-z]+)+")


def hide_long_runs(text: str) -> str:
    """`text` with every LONG_RUN that may be a credential, one that is no ERROR_NAME, replaced by an ellipsis."""
    return LONG_RUN.sub(lambda run: run[0] if ERROR_NAME.fullmatch(run[0]) else "…", text)


def _iso(moment: datetime) -> str:
    """`moment` in UTC, to the second. A time without an offset is in UTC."""
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class HubError(Exception):
    """The hub did not answer as expected; its words are safe for the page."""


class HubNotFound(HubError):
    """The hub answered 404: it does not have what the request named."""


class HubUnreached(HubError):
    """The request did not reach the hub, or its answer did not come back."""


def state_of(credential: dict[str, Any]) -> tuple[str, str | None]:
    """Whether the account works: "working", "disabled", "cooling" (the hub rests the whole account after a limit or
    errors, until the time given) or "signed_out" (its sign-in needs renewing). A hold the hub keeps on some models
    alone, such as a provider's ten-minute rate limit on one model, leaves the account working: the hub keeps sending
    it the other models, and held_models names the held ones. The hub's `quota_blocked` is true for such a hold too,
    so it marks a rest only together with an account-wide hold or when the hub names no hold at all."""
    if credential.get("disabled"):
        return "disabled", None
    if credential.get("requires_sign_in"):
        return "signed_out", None
    holds = credential.get("cooldowns") or []
    account_ends = [each["retry_at"] for each in holds if each.get("scope") != "model" and each.get("retry_at")]
    if credential.get("unavailable") or account_ends or (credential.get("quota_blocked") and not holds):
        # The hub writes times with a varying number of fraction digits; their seconds order them.
        return "cooling", credential.get("next_retry_after") or min(account_ends, key=lambda end: end[:19], default=None)
    return "working", None


def held_models(credential: dict[str, Any]) -> list[dict[str, Any]]:
    """The models the hub holds back on a working account (state_of), soonest end first: each one's `model` as the
    hub names it, `label` as Cockpit's model menu names it (the hub's name when Cockpit offers no such model),
    `until`, and whether a `limit` (a 429 from the provider) or an error caused it."""
    if state_of(credential)[0] != "working":
        return []
    labels = {model: label for offered in config.SESSION_OPTIONS.values() for model, label in offered["models"]}
    holds = [each for each in credential.get("cooldowns") or [] if each.get("scope") == "model" and each.get("retry_at")]
    return [{"model": str(each.get("model_key") or ""), "label": labels.get(each.get("model_key"), str(each.get("model_key") or "")),
             "until": each["retry_at"], "limit": each.get("http_status") == 429}
            for each in sorted(holds, key=lambda each: each["retry_at"][:19])]


def register_availability_reader(db_path: Path, reader: Hub) -> None:
    """Record the app's hub reader for its database: the hub's part, built when the server starts, registers it, and
    every reader of the hub's data asks reader_for whether this database has a hub."""
    _availability_readers[str(db_path.resolve())] = reader


def reader_for(conn: sqlite3.Connection) -> Hub | None:
    """The hub reader the hub's part registered for this connection's database: None when the server was built
    without the hub's part, whatever the setup file says since."""
    database = next((row["file"] for row in conn.execute("PRAGMA database_list") if row["name"] == "main"), "")
    return _availability_readers.get(str(Path(database).resolve())) if database else None


def provider_availability(conn: sqlite3.Connection, now: datetime) -> dict[str, bool] | None:
    """Use recent availability, completing a refresh before routing when the saved reading has expired."""
    reader = reader_for(conn)
    if reader is None or not reader.configured():
        return None
    try:
        snapshot = db.kv_get(conn, PROVIDER_AVAILABILITY_KEY)
    except ValueError:
        log.exception("reading the hub's provider availability failed")
        return {}
    observed_at = snapshot.get("observed_at") if isinstance(snapshot, dict) else None
    fresh = isinstance(observed_at, (int, float)) and 0 <= now.timestamp() - observed_at <= 3 * REFRESH_SECONDS
    if fresh:
        reader.read(now, False)
    else:
        reader._refresh_safely(now)
        snapshot = db.kv_get(conn, PROVIDER_AVAILABILITY_KEY)
        observed_at = snapshot.get("observed_at") if isinstance(snapshot, dict) else None
        fresh = isinstance(observed_at, (int, float)) and 0 <= now.timestamp() - observed_at <= 3 * REFRESH_SECONDS
    if not fresh:
        return {}
    return {provider: snapshot[provider] for provider in CHECKED_PROVIDERS if type(snapshot.get(provider)) is bool}


def _available_providers(credentials: list[dict[str, Any]], limits: dict[str, Any],
                         checks: dict[str, Any], now: float) -> dict[str, bool]:
    """Any working account with room in its general limits makes its provider available; a failed limits check
    without known windows leaves that account unknown. Model-specific limits leave other models available."""
    available = dict.fromkeys(CHECKED_PROVIDERS, False)
    unknown = set()
    for credential in credentials:
        provider = credential.get("provider")
        if provider not in available or state_of(credential)[0] != "working":
            continue
        key = limits_key(credential)
        windows = limits[key]["windows"]
        if any(each["used_percentage"] >= 100 and each["resets_at"] > now for each in windows.values()):
            continue
        if not windows and checks.get(key, {}).get("error"):
            unknown.add(provider)
        else:
            available[provider] = True
    return {provider: usable for provider, usable in available.items() if usable or provider not in unknown}


def model_capacity(conn: sqlite3.Connection, now: datetime) -> dict[str, dict[str, Any]] | None:
    """Use the existing account snapshot and saved limits; reading the composer starts no refresh or quota check."""
    reader = reader_for(conn)
    if reader is None or not reader.configured():
        return None
    with reader._lock:
        listed = reader._listed
    if listed is None:
        return {}
    credentials, observed_at = listed
    if now.timestamp() - datetime.fromisoformat(observed_at).timestamp() > 3 * REFRESH_SECONDS:
        return {}
    kept = db.kv_get(conn, LIMITS_KEY) or {}
    found = {}
    for provider, offered in config.SESSION_OPTIONS.items():
        for model, _label in offered["models"]:
            routes = []
            for credential in credentials:
                state, until = state_of(credential)
                if credential.get("provider") != provider or state in ("disabled", "signed_out"):
                    continue
                if state == "cooling":
                    if until is not None and datetime.fromisoformat(until) <= now:
                        routes.append(capacity.unknown())
                        continue
                    routes.append({"state": "limited", "resets_at": until, "remaining_percent": 0,
                                   "reason": "Account is temporarily unavailable"})
                    continue
                held = next((each["until"] for each in held_models(credential) if each["model"] == model), None)
                if held is not None and datetime.fromisoformat(held) > now:
                    routes.append({"state": "limited", "resets_at": held, "remaining_percent": 0,
                                   "reason": "Model is temporarily unavailable on this account"})
                    continue
                known = limits_now(credential, kept.get(limits_key(credential), {}), now.timestamp())
                routes.append(capacity.account(capacity.model_windows(model, list(known["windows"].values()),
                                                                     known["models"]), now))
            found[model] = capacity.combine(routes)
    return found


def codex_windows(signals: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Codex's limit headers, by lower-case name, as {usage.WINDOWS key: {used_percentage, resets_at}}."""
    found = {}
    for part in ("primary", "secondary"):
        key = CODEX_WINDOWS.get(int(signals.get(f"x-codex-{part}-window-minutes") or 0))
        used, resets_at = signals.get(f"x-codex-{part}-used-percent"), signals.get(f"x-codex-{part}-reset-at")
        if key and used and resets_at:
            found[key] = {"used_percentage": float(used), "resets_at": float(resets_at)}
    return found


def claude_windows(signals: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Claude's limit headers, by lower-case name, as {usage.WINDOWS key: {used_percentage, resets_at}}. Claude gives
    the share used as a fraction of one, and only the windows that request counted against."""
    found = {}
    for key, name in CLAUDE_WINDOWS.items():
        used = signals.get(f"anthropic-ratelimit-unified-{name}-utilization")
        resets_at = signals.get(f"anthropic-ratelimit-unified-{name}-reset")
        if used and resets_at:
            found[key] = {"used_percentage": float(used) * 100, "resets_at": float(resets_at)}
    return found


def claude_model_limits(signals: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Claude's weekly limits that count one model's use alone, from its limit headers by lower-case name, as
    {model name: {used_percentage, resets_at}}. A limit whose code Cockpit has no model for is logged and left out."""
    found = {}
    for name, used in signals.items():
        match = CLAUDE_MODEL_WINDOW.fullmatch(name)
        if match is None:
            continue
        code = match[1]
        if code not in CLAUDE_MODEL_WINDOWS:
            log.error("Claude's headers name a weekly limit Cockpit has no model for: 7d_%s", code)
            continue
        resets_at = signals.get(f"anthropic-ratelimit-unified-7d_{code}-reset")
        if used and resets_at:
            found[CLAUDE_MODEL_WINDOWS[code]] = {"used_percentage": float(used) * 100, "resets_at": float(resets_at)}
    return found


def _records(credential: dict[str, Any]) -> list[tuple[dict[str, str], str]]:
    """What the hub holds of the account's limit headers, oldest first: those of its last request (`quota`) and of its
    last request for each model (`model_quotas`), each as (the headers by lower-case name, when the provider sent
    them). A record without a time is one no request has filled."""
    records = [credential.get("quota") or {}, *(credential.get("model_quotas") or {}).values()]
    timed = [(datetime.fromisoformat(each["observed_at"]), each) for each in records if each.get("observed_at")]
    timed.sort(key=lambda pair: pair[0])
    return [({name.lower(): value for name, value in (each.get("signals") or {}).items()}, _iso(seen)) for seen, each in timed]


def limits_now(credential: dict[str, Any], earlier: dict[str, Any], now: float) -> dict[str, Any]:
    """The account's limits at `now`: {"windows": {usage.WINDOWS key: reading}, "models": {model name: reading}, "plan"},
    each reading {used_percentage, resets_at, captured_at} with `captured_at` when the provider said it. `models` holds
    a Claude account's weekly limits for one model alone, and `plan` the plan Codex named last ("" for none).
    `earlier` is what this gave for the same account on the previous read ({} for none): a limit the hub's records no
    longer name keeps its earlier reading until it resets."""
    provider = credential.get("provider")
    windows, models, plan = dict(earlier.get("windows") or {}), dict(earlier.get("models") or {}), earlier.get("plan") or ""
    for signals, seen_at in _records(credential):
        named = [(windows, codex_windows(signals) if provider == "codex" else claude_windows(signals))]
        if provider == "claude":
            named.append((models, claude_model_limits(signals)))
        for kept, readings in named:
            for key, reported in readings.items():
                if key not in kept or kept[key]["captured_at"] <= seen_at:
                    kept[key] = reported | {"captured_at": seen_at}
        plan = signals.get("x-codex-plan-type") or plan
    return {
        "windows": {key: each for key, each in windows.items() if each["resets_at"] > now},
        "models": {model: each for model, each in models.items() if each["resets_at"] > now},
        "plan": plan,
    }


def opus_week(limits: dict[str, Any], now: float, email: str) -> dict[str, Any]:
    """A Claude account's week as it stands for the other models when Fable uses all its own weekly allowance, as the
    usage bars draw a week (usage.model_week). Fable's allowance is a share of the week (FABLE_SHARE_OF_WEEK); the
    rest of the week is what the other models can count on, and `used` is the share of that rest already gone: the
    week's use that Fable's allowance does not cover, over the rest. A week at 40% with Fable's allowance 70% used has
    5% of the week used by other models, which is 10% of their half. It resets with the week, and `captured_at` is
    the older of the two readings it is made of. Use is unknown while the week or Fable's limit is."""
    week, fable = limits["windows"].get("seven_day"), limits["models"].get(FABLE)
    reported, captured_at = None, None
    if week is not None and fable is not None:
        used = (week["used_percentage"] - fable["used_percentage"] * FABLE_SHARE_OF_WEEK) / (1 - FABLE_SHARE_OF_WEEK)
        reported = {"used_percentage": min(100.0, max(0.0, used)), "resets_at": week["resets_at"]}
        captured_at = min(week["captured_at"], fable["captured_at"])
    found = usage.model_week({"model": "Opus", "reported": reported}, now, email)
    return found | {"key": "opus_week", "name": "Opus after Fable", "captured_at": captured_at}


def limits_key(credential: dict[str, Any]) -> str:
    """The name of a hub account in LIMITS_KEY and LIMIT_CHECKS_KEY: "provider/id"."""
    return f"{credential.get('provider') or ''}/{credential.get('id') or ''}"


def needs_check(credential: dict[str, Any], limits: dict[str, Any], check: dict[str, Any] | None, now: float) -> bool:
    """Whether Cockpit asks the account's provider for its limits on the read at `now` (Hub._check): a working Claude
    or Codex account without a current limit window or model limit (`limits`, as limits_now gives them) that was never checked (`check`, its
    record in LIMIT_CHECKS_KEY or None), or whose last check, answered or failed, is CHECK_RETRY_SECONDS old or older;
    and a quota-held account every COOLING_CHECK_SECONDS, for a manual reset."""
    if credential.get("provider") not in CHECKED_PROVIDERS:
        return False
    if state_of(credential)[0] == "cooling" and credential.get("quota_blocked"):
        gap = CHECK_RETRY_SECONDS if check and check["error"] else COOLING_CHECK_SECONDS
        return check is None or now - datetime.fromisoformat(check["at"]).timestamp() >= gap
    return (state_of(credential)[0] == "working"
            and not limits["windows"] and not limits["models"] and (check is None or not check_holds(check, now)))


def quota_held(credential: dict[str, Any]) -> bool:
    """Whether the hub rests the account because a limit of it is used up."""
    return state_of(credential)[0] == "cooling" and bool(credential.get("quota_blocked"))


def week_reset(earlier: dict[str, Any], fresh: dict[str, Any]) -> bool:
    """The provider's fresh answer shows a used-up week was reset: no use in it, or less than a week used up before."""
    old_week = earlier["windows"].get("seven_day")
    week = fresh["windows"].get("seven_day")
    return bool(week and (week["used_percentage"] == 0 or (
        old_week and old_week["used_percentage"] >= 99 and week["used_percentage"] < old_week["used_percentage"])))


def quota_reset(credential: dict[str, Any], earlier: dict[str, Any], fresh: dict[str, Any], reset_seen: bool) -> bool:
    """A provider confirmed a used-up week was reset, now (week_reset) or on an earlier check during the same quota
    hold (`reset_seen`), with room in every limit it reports now."""
    reported = [*(earlier["windows"] | fresh["windows"]).values(),
                *(earlier["models"] | fresh["models"]).values()]
    return bool(
        quota_held(credential) and fresh["windows"].get("seven_day")
        and (reset_seen or week_reset(earlier, fresh))
        and reported and all(reading["used_percentage"] < 100 for reading in reported)
    )


def check_holds(check: dict[str, Any], now: float) -> bool:
    """Whether a check's record in LIMIT_CHECKS_KEY is younger than CHECK_RETRY_SECONDS at `now`."""
    return now - datetime.fromisoformat(check["at"]).timestamp() < CHECK_RETRY_SECONDS


def account_id(credential: dict[str, Any]) -> str:
    """The ChatGPT account a Codex sign-in belongs to, which ChatGPT wants with the request."""
    claims = credential.get("id_token")
    return str((claims or {}).get("chatgpt_account_id") or "") if isinstance(claims, dict) else ""


def check_request(credential: dict[str, Any]) -> dict[str, Any]:
    """The request that asks a Claude or Codex account's provider for its limits, in the shape the hub's relay takes:
    the request Claude Code makes for its usage (resets.URL, resets.headers), or Codex's for its own."""
    if credential.get("provider") == "codex":
        header = TOKEN_HEADER | {"Content-Type": "application/json", "User-Agent": CODEX_AGENT}
        if account_id(credential):
            header["Chatgpt-Account-Id"] = account_id(credential)
        return {"method": "GET", "url": CODEX_USAGE, "header": header}
    return {"method": "GET", "url": resets.URL, "header": resets.headers() | TOKEN_HEADER}


def codex_answer_windows(answer: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Codex's `wham/usage` answer as {usage.WINDOWS key: {used_percentage, resets_at}}."""
    limits = answer.get("rate_limit") or {}
    found = {}
    for part in (limits.get("primary_window"), limits.get("secondary_window")):
        key = part and CODEX_ANSWER_WINDOWS.get(part.get("limit_window_seconds"))
        if key and part.get("used_percent") is not None and part.get("reset_at"):
            found[key] = {"used_percentage": float(part["used_percent"]), "resets_at": float(part["reset_at"])}
    return found


def answer_limits(provider: str, answer: dict[str, Any], now: datetime) -> dict[str, Any]:
    """The limits a Claude or Codex account's provider gave in its answer to a check, said at `now`, in limits_now's
    shape. An answer with a shape Cockpit cannot read raises."""
    said = {"captured_at": _iso(now)}
    if provider == "codex":
        windows, models, plan = codex_answer_windows(answer), {}, str(answer.get("plan_type") or "")
    else:
        read, limited = resets.windows(answer, now), resets.model_limits(answer)
        if read is None or limited is None:
            raise ValueError("Claude's answer has a shape resets cannot read")
        windows = read["rate_limits"]
        models = {each["model"]: each["reported"] for each in limited if each["reported"]}
        plan = ""
    return {"windows": {key: each | said for key, each in windows.items()},
            "models": {model: each | said for model, each in models.items()}, "plan": plan}


def check_answer(provider: str, status: Any, body: Any, now: datetime) -> tuple[dict[str, Any] | None, str]:
    """What the provider's answer to a check gives, from its status and its body as the hub's relay passes them on (the
    body as text): the limits as answer_limits reads them, or None and why the check failed. An answer Cockpit can
    read that names no limit window is an answer too."""
    who = provider.capitalize()
    if isinstance(body, str):
        try:
            body = json.loads(body) if body.strip() else None
        except json.JSONDecodeError:
            body = None
    if status != 200:
        return None, f"{who} answered {status} when asked for its limits"
    if not isinstance(body, dict):
        return None, f"{who}'s answer about its limits was not a JSON object"
    try:
        return answer_limits(provider, body, now), ""
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        # The exception's words may quote the answer, so only its kind is kept.
        return None, f"{who}'s answer about its limits has a shape Cockpit cannot read ({type(exc).__name__})"


def state_words(message: str) -> str:
    """The hub's words for an account's state. When a provider refuses a request, the hub passes on the provider's
    whole JSON error as the message; the page shows the sentence in it. Any other message stays as it is."""
    try:
        told = json.loads(message)
    except ValueError:
        return message
    error = told.get("error") if isinstance(told, dict) else None
    words = error.get("message") if isinstance(error, dict) else None
    return words if isinstance(words, str) and words else message


def account(credential: dict[str, Any], limits: dict[str, Any], now: float, limits_error: str) -> dict[str, Any]:
    """One hub account for the page: who it is, its state with the hub's own words for it (`said`), the requests the
    hub sent through it (`succeeded`, `failed`, `traffic`), the models it holds back on a working account (`held`,
    as held_models gives them), and its limit windows as the usage bars draw them, from
    `limits` as limits_now gives them. `week` is its 7-day window, `model_weeks` a Claude account's weekly limits for one
    model alone (usage.model_week) and `opus_week` its week once Fable's allowance is used up (None for Codex). Each
    carries `captured_at`, when the provider said that limit (None for one without numbers). `limits_at` is the newest
    of those times, None for an account without a limit. `limits_error` says why the last check of an account without
    limit windows failed (needs_check), "" when none did."""
    provider = str(credential.get("provider") or "")
    email = str(credential.get("email") or credential.get("account") or credential.get("label") or credential.get("name") or "")
    state, until = state_of(credential)
    windows = []
    for key in usage.WINDOWS:
        reading = limits["windows"].get(key)
        windows.append(usage.window(key, reading, now, email) | {"captured_at": reading and reading["captured_at"]})
    model_weeks = [usage.model_week({"model": model, "reported": reading}, now, email) | {"captured_at": reading["captured_at"]}
                   for model, reading in limits["models"].items()]
    claims = credential.get("id_token")
    plan = limits["plan"] or (claims.get("plan_type") if isinstance(claims, dict) else "")
    times = [each["captured_at"] for each in (*limits["windows"].values(), *limits["models"].values())]
    return {
        "id": str(credential.get("id") or ""), "index": str(credential.get("auth_index") or ""),
        "provider": provider, "email": email, "plan": str(plan or ""),
        "plan_label": "Pro $500" if provider == "codex" and plan == "promax" else str(plan or ""),
        "state": state, "state_until": until, "held": held_models(credential), "said": state_words(str(credential.get("status_message") or "")),
        "windows": windows, "week": next(each for each in windows if each["key"] == "seven_day"),
        "limits_at": max(times, default=None), "limits_error": limits_error,
        "model_weeks": model_weeks, "opus_week": opus_week(limits, now, email) if provider == "claude" else None,
        "succeeded": int(credential.get("success") or 0), "failed": int(credential.get("failed") or 0),
        "traffic": traffic(credential.get("recent_requests") or [], datetime.fromtimestamp(now, UTC)),
        "token_expires_at": credential.get("access_token_expires_at"),
    }


def traffic(spans: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """The requests the hub sent through an account in each of its last spans, oldest first, each dated by its start.
    The hub names a span by its clock times in UTC ("05:10-05:20"), so the date is the latest one that puts the span's
    start before `now`."""
    found = []
    for span in spans:
        hour, minute = str(span["time"]).split("-")[0].split(":")
        start = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
        # The span under way starts before `now` unless the hub's clock runs ahead of this machine's.
        if start > now + timedelta(seconds=SPAN_SECONDS):
            start -= timedelta(days=1)
        found.append({"at": _iso(start), "succeeded": int(span["success"]), "failed": int(span["failed"])})
    return found


REQUEST_LINE = re.compile(r"^(URL|Method): (.*)$", re.MULTILINE)
RESPONSE_STATUS = re.compile(r"^=== RESPONSE ===\nStatus: (\d{3})$", re.MULTILINE)


def failed_request(text: str) -> dict[str, Any]:
    """What the page shows of one request the hub answered with an error, from the hub's log of it: the method, the
    path without its query and the status. The log's headers and bodies, which hold the caller's key and what was
    asked, go no further than this function. The status is the last response section's: the hub writes its own
    answer after the request's body, which is the caller's text and may hold a response heading too."""
    head = text.split("=== HEADERS ===")[0]
    said = {name: value.strip() for name, value in REQUEST_LINE.findall(head)}
    statuses = RESPONSE_STATUS.findall(text)
    return {
        "method": said.get("Method", ""), "path": said.get("URL", "").split("?")[0][:200],
        "status": int(statuses[-1]) if statuses else None,
    }


def callback_parts(address: str) -> tuple[str, str, str]:
    """The code, the state and Claude's error in what the user pasted: the whole address the browser landed on, its
    query alone, or Claude's `code#state`."""
    text = address.strip()
    query = text.split("?", 1)[1] if "?" in text else text
    query, _, fragment = query.partition("#")
    fields = parse_qs(fragment) | parse_qs(query)

    def first(name: str) -> str:
        return (fields.get(name) or [""])[0].strip()

    if not fields and "#" in text:
        code, _, state = text.partition("#")
        return code.strip(), state.strip(), ""
    return first("code"), first("state"), first("error_description") or first("error")


@dataclass
class _Login:
    """One login running on the hub. `code` is the code Codex's login shows; `state` the state Claude's link carries,
    which the address the user brings back must carry too, and `sent` the code of that address."""

    provider: str
    process: subprocess.Popen[str]
    timer: threading.Timer
    ends_at: str
    text: str = ""
    url: str = ""
    code: str = ""
    state: str = ""
    sent: str = ""
    checking: bool = False
    returncode: int | None = None
    signed: bool = False
    ended: threading.Event = field(default_factory=threading.Event)

    def ready(self) -> bool:
        """Whether the login has shown what the user needs: OpenAI's link and code, or Claude's link."""
        return bool(self.url and (self.code if self.provider == "codex" else self.state))

    def spot(self) -> None:
        """Take the link, and the code or state, from what the login printed so far."""
        if self.provider == "codex":
            link, code = CODEX_LINK.search(self.text), CODEX_CODE.search(self.text)
            if link and code:
                self.url, self.code = link[1], code[1]
            return
        link = CLAUDE_LINK.search(self.text)
        state = link and (parse_qs(urlsplit(link[0]).query).get("state") or [""])[0]
        if link and state:
            self.url, self.state = link[0], state


class Hub:
    """The hub as last read, its controls, and for each provider the one sign-in that may wait for the user.

    `url` is the hub's address, `key_path` the file with its management key, `logins` the sign-in command for each
    provider that has one, `tunnel_state` the command that prints the tunnel's state (None: nothing is checked),
    `restarts` the command behind each restart the setup gives and `db_path` Cockpit's database, which keeps each account's limits and its last check between reads.
    `after_read` gets the accounts of each read the hub answered, with the read's time.
    """

    def __init__(self, url: str, key_path: Path, logins: dict[str, tuple[str, ...]], tunnel_state: tuple[str, ...] | None,
                 restarts: dict[str, tuple[str, ...]], db_path: Path,
                 after_read: Callable[[list[dict[str, Any]], datetime], None]) -> None:
        self._url = url
        self._db_path = db_path
        conn = db.connect(db_path)
        try:
            hub_left.create_table(conn)
        finally:
            conn.close()
        self._after_read = after_read
        self._key_path = key_path
        self._commands = logins
        self._tunnel_state = tunnel_state
        self._restarts = restarts
        self._lock = threading.Lock()
        # Held for a whole read of the hub, so reads, their checks and the limits each saves follow one another.
        self._refreshing = threading.Lock()
        self._read: dict[str, Any] | None = None
        # The hub's list of accounts from the last read that worked, and when that was; None before one.
        self._listed: tuple[list[dict[str, Any]], str] | None = None
        self._reading = False
        self._read_started = 0.0
        # Counts the reads a control made, so a background read that started before one does not replace it.
        self._changes = 0
        self._version = ""
        self._latest: tuple[float, str] | None = None
        self._failed_requests: dict[str, dict[str, Any]] = {}
        self._logins: dict[str, _Login] = {}
        self._starting: set[str] = set()
        self._failures: dict[str, str] = {}
        self._finished: dict[str, str] = {}
        # For each kind of read ("accounts", "service", "check <account>"), when its first failure to reach the hub
        # since it last worked came.
        self._unreached: dict[str, float] = {}

    def configured(self) -> bool:
        """Whether this machine has a hub: the management key's file exists."""
        return self._key_path.is_file()

    def _send(self, method: str, path: str, body: dict[str, Any] | None, timeout: float) -> bytes:
        """The hub's answer to one management request, waited for at most `timeout` seconds. Errors name the path
        without its query, which may hold an account's name."""
        key = self._key_path.read_text().strip()
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"{self._url}{path}", data=data, method=method, headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        })
        route = path.split("?")[0]
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                self._version = response.headers.get("X-Cpa-Version") or ""
                return response.read()
        except urllib.error.HTTPError as exc:
            kind = HubNotFound if exc.code == 404 else HubError
            raise kind(f"the hub answered {exc.code} to {route}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise HubUnreached(f"the hub could not be reached: {reason}") from None

    def _call(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        """The hub's JSON answer to one management request."""
        answer = self._send(method, path, body, REQUEST_TIMEOUT_SECONDS)
        try:
            found = json.loads(answer)
        except json.JSONDecodeError:
            found = None
        if not isinstance(found, dict):
            raise HubError(f"the hub's answer to {path.split('?')[0]} was not a JSON object")
        return found

    def _get(self, path: str, name: str) -> Any:
        """One field of the hub's answer to a GET, which every answer to that route carries."""
        answer = self._call("GET", path, None)
        if name not in answer:
            raise HubError(f"the hub's answer to {path} had no {name}")
        return answer[name]

    def _tunnel(self) -> str | None:
        """The state of this machine's tunnel to the hub in systemd's word ("active", "inactive", "failed"), "unknown"
        when the command that tells it did not run, or None when the setup gives no such command."""
        if self._tunnel_state is None:
            return None
        try:
            said = subprocess.run(list(self._tunnel_state), check=False, capture_output=True, text=True, timeout=TUNNEL_STATE_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.error("asking for the state of the tunnel to the CLIProxyAPI hub failed: %s", exc)
            return "unknown"
        return said.stdout.strip() or "unknown"

    def _failed_read(self, kind: str, words: str, exc: HubError, now: float) -> None:
        """Logs a read of `kind` that failed with `exc`, `words` saying what was read: as a warning while that read has
        not reached the hub for less than UNREACHED_ALERT_SECONDS, otherwise as an error."""
        if isinstance(exc, HubUnreached):
            since = self._unreached.setdefault(kind, now)
            if now - since < UNREACHED_ALERT_SECONDS:
                log.warning("%s failed: %s; an error follows when it stays out of reach for %d minutes", words, exc,
                            UNREACHED_ALERT_SECONDS // 60)
                return
            log.error("%s failed for %d minutes: %s", words, (now - since) // 60, exc)
            return
        log.error("%s failed: %s", words, exc)

    def _latest_version(self, now: float) -> str | None:
        """The hub's newest release, asked once in LATEST_SECONDS; None when the hub could not find it out."""
        if self._latest is None or now - self._latest[0] >= LATEST_SECONDS:
            try:
                self._latest = (now, str(self._get(f"{V0}/latest-version", "latest-version")).removeprefix("v"))
            except HubError as exc:
                log.warning("the CLIProxyAPI hub gave no newest release: %s", exc)
                return None
        return self._latest[1]

    def _failed(self) -> dict[str, Any]:
        """The requests the hub answered with an error: how many it logged, and the newest FAILED_SHOWN of them, the
        ones with the same method, path and status as one line that says how many times and when last. The hub keeps
        one log file for each request; Cockpit reads each shown file once. The hub keeps only so many files and
        deletes the oldest when a new one arrives, so a file it listed can be gone by the time Cockpit asks for it;
        that request is left out."""
        files = sorted(self._get(f"{V0}/request-error-logs", "files"), key=lambda each: each["modified"], reverse=True)
        read: dict[str, dict[str, Any]] = {}
        for each in files[:FAILED_SHOWN]:
            name = str(each["name"])
            known = self._failed_requests.get(name)
            if known is None:
                log_path = f"{V0}/request-error-logs/{quote(name, safe='')}"
                try:
                    text = self._send("GET", log_path, None, REQUEST_TIMEOUT_SECONDS).decode(errors="replace")
                except HubNotFound:
                    continue
                known = failed_request(text) | {"at": _iso(datetime.fromtimestamp(each["modified"], UTC))}
            read[name] = known
        self._failed_requests = read
        shown: dict[tuple[Any, ...], dict[str, Any]] = {}
        for each in read.values():
            same = shown.setdefault((each["method"], each["path"], each["status"]), each | {"times": 0})
            same["times"] += 1
        return {"count": len(files), "shown": list(shown.values())}

    def _service(self, now: float) -> dict[str, Any]:
        """The hub program's own state: the version that runs and the newest release, how it shares requests among
        accounts, how many client keys it accepts and the requests it failed. `error` says why the rest is unknown."""
        try:
            service = {
                "version": self._version, "latest_version": self._latest_version(now),
                "routing": str(self._get(f"{V0}/routing/strategy", "strategy")),
                "client_keys": len(self._get(f"{V0}/api-keys", "api-keys")),
                "failed_requests": self._failed(), "error": None,
            }
        except HubError as exc:
            self._failed_read("service", "reading the CLIProxyAPI hub's own state", exc, now)
            return {"version": self._version, "latest_version": None, "routing": None, "client_keys": None,
                    "failed_requests": None, "error": str(exc)}
        self._unreached.pop("service", None)
        return service

    def _check(self, credential: dict[str, Any], now: datetime) -> tuple[dict[str, Any] | None, str, bool]:
        """Ask the account's provider for its limits through the hub's relay: the limits as answer_limits reads them,
        or None and why the check failed, logged; and whether to ask again on the next read. A question that did not
        reach the hub is asked again on the next read, logged as a warning until that account's questions have not
        reached the hub for UNREACHED_ALERT_SECONDS; then it is an error and a failed check."""
        provider, name, status = str(credential.get("provider") or ""), str(credential.get("id") or ""), None
        unreached = f"check {limits_key(credential)}"
        try:
            request = check_request(credential)
            result = self._call("POST", RELAY, {"authIndex": str(credential.get("auth_index")), **request})
        except HubUnreached as exc:
            since = self._unreached.setdefault(unreached, now.timestamp())
            if now.timestamp() - since < UNREACHED_ALERT_SECONDS:
                log.warning("asking %s through the hub for the limits of the hub account %s did not reach the hub: %s;"
                            " Cockpit asks again on its next read, and an error follows when it stays out of reach for"
                            " %d minutes", provider, name, exc, UNREACHED_ALERT_SECONDS // 60)
                return None, str(exc), True
            del self._unreached[unreached]
            found, error = None, str(exc)
        except HubError as exc:
            # The hub answered, so a later question that misses it starts its five minutes afresh.
            self._unreached.pop(unreached, None)
            found, error = None, str(exc)
        except OSError as exc:
            # Only resets.headers, which names the installed Claude Code, raises it here; its words may name a path.
            found, error = None, f"Cockpit could not make Claude Code's request: {type(exc).__name__}"
        else:
            self._unreached.pop(unreached, None)
            status = result.get("status_code")
            found, error = check_answer(provider, status, result.get("body"), now)
        if found is None:
            log.error("asking %s through the hub for the limits of the hub account %s failed (status %s): %s",
                      provider, name, status, error)
        elif not found["windows"]:
            log.warning("%s answered when asked through the hub for the limits of the hub account %s, and named no"
                        " limit window; Cockpit asks again in an hour unless a request through the hub brings them",
                        provider, name)
        else:
            log.info("asked %s through the hub for the limits of the hub account %s", provider, name)
        return found, error, False

    def refresh(self, now: datetime) -> dict[str, Any]:
        """Read the tunnel's state, every account with its limits (limits_now) and the hub's own state now; on a read
        the hub answered, an account without a limit window gets its check (needs_check). `read_at` is when the hub
        last gave its accounts. A read that fails gives the accounts of the last read that worked, with their kept
        limits as they stand at `now`, and the failure as `error`; before any read worked it gives none. One read runs
        at a time: a read that asked the hub earlier also saves its limits earlier.

        The first read after a start shows its accounts to `read` as soon as the hub has listed them, with `service`
        None until the checks and the hub's own state follow, since that state takes longer on a first read than the
        accounts do."""
        with self._refreshing:
            tunnel = self._tunnel()
            try:
                credentials = self._get(f"{V8}/credentials", "files")
            except HubError as exc:
                self._invalidate_availability(now)
                self._failed_read("accounts", "reading the CLIProxyAPI hub", exc, now.timestamp())
                if self._listed is None:
                    return {"accounts": [], "error": str(exc), "read_at": _iso(now), "service": None, "tunnel": tunnel}
                credentials, read_at = self._listed
                return {"accounts": self._accounts(credentials, now.timestamp(), False), "error": str(exc), "read_at": read_at,
                        "service": None, "tunnel": tunnel}
            self._listed = (credentials, _iso(now))
            self._unreached.pop("accounts", None)
            if self._read is None:
                listed = {"accounts": self._accounts(credentials, now.timestamp(), False), "error": None, "read_at": _iso(now),
                          "service": None, "tunnel": tunnel}
                with self._lock:
                    if self._read is None:
                        self._read = listed
            self._check_limits(credentials, now)
            accounts = self._accounts(credentials, now.timestamp(), True)
            try:
                self._after_read(accounts, now)
            except Exception:
                log.exception("acting on a read of the CLIProxyAPI hub failed")
            return {"accounts": accounts, "error": None, "read_at": _iso(now),
                    "service": self._service(now.timestamp()), "tunnel": tunnel}

    def _check_limits(self, credentials: list[dict[str, Any]], now: datetime) -> None:
        """Ask the provider of each of the hub's `credentials` that needs it for its limits (needs_check). The answer's
        limits go to LIMITS_KEY, where _accounts finds them, and each check's record to LIMIT_CHECKS_KEY. A record
        stays when the list no longer names its account, so the account's return brings no second question."""
        at = now.timestamp()
        conn = db.connect(self._db_path)
        try:
            limits = db.kv_get(conn, LIMITS_KEY) or {}
            checks = db.kv_get(conn, LIMIT_CHECKS_KEY) or {}
            for credential in credentials:
                key = limits_key(credential)
                known = limits_now(credential, limits.get(key, {}), at)
                # A reset seen during a quota hold counts only until that hold ends.
                reset_seen = quota_held(credential) and bool(checks.get(key, {}).get("week_reset"))
                if checks.get(key, {}).get("week_reset") and not reset_seen:
                    checks[key] = checks[key] | {"week_reset": False}
                    db.kv_set(conn, LIMIT_CHECKS_KEY, checks)
                if not needs_check(credential, known, checks.get(key), at):
                    continue
                earlier = checks.get(key)
                # Saved before the question goes out, so a stop meanwhile still counts as a check.
                checks[key] = {"at": _iso(now), "error": CHECK_CUT_OFF, "week_reset": reset_seen}
                db.kv_set(conn, LIMIT_CHECKS_KEY, checks)
                found, error, again = self._check(credential, now)
                if again:
                    # The question never reached the hub, so the next read asks it as if it had not been asked.
                    if earlier is None:
                        del checks[key]
                    else:
                        checks[key] = earlier
                    db.kv_set(conn, LIMIT_CHECKS_KEY, checks)
                    continue
                if found is not None:
                    if quota_reset(credential, known, found, reset_seen):
                        # Clear only a quota hold whose exhausted allowance the provider says was reset.
                        # Disabled and signed-out accounts never reach this branch.
                        self._call("POST", f"{V8}/routing/cooldown/reset", {"auth_index": str(credential["auth_index"])})
                        credential.update(unavailable=False, quota_blocked=False, next_retry_after=None,
                                          status_message="",
                                          cooldowns=[each for each in credential.get("cooldowns") or []
                                                     if each.get("scope") == "model"])
                        log.warning("released a stale quota hold after the provider confirmed a reset for %s", key)
                    reset_seen = quota_held(credential) and (reset_seen or week_reset(known, found))
                    limits[key] = {"windows": known["windows"] | found["windows"], "models": known["models"] | found["models"],
                                   "plan": found["plan"] or known["plan"]}
                    db.kv_set(conn, LIMITS_KEY, limits)
                checks[key] = {"at": _iso(now), "error": error, "week_reset": reset_seen}
                db.kv_set(conn, LIMIT_CHECKS_KEY, checks)
        finally:
            conn.close()

    def _accounts(self, credentials: list[dict[str, Any]], now: float, refreshed: bool) -> list[dict[str, Any]]:
        """The hub's `credentials` as the page's accounts at `now`, keeping each one's limits in the database for the
        next read. An account the list no longer names loses its kept limits. A refreshed list after its limits
        checks also saves the anonymous provider availability used by the sweeper."""
        accounts = []
        kept: dict[str, dict[str, Any]] = {}
        conn = db.connect(self._db_path)
        try:
            earlier = db.kv_get(conn, LIMITS_KEY) or {}
            checks = db.kv_get(conn, LIMIT_CHECKS_KEY) or {}
            for credential in credentials:
                key = limits_key(credential)
                kept[key] = limits_now(credential, earlier.get(key, {}), now)
                limits_error = checks[key]["error"] if key in checks and not kept[key]["windows"] else ""
                accounts.append(account(credential, kept[key], now, limits_error))
            if kept != earlier:
                db.kv_set(conn, LIMITS_KEY, kept)
            hub_left.record(conn, kept, now)
            if refreshed:
                db.kv_set(conn, PROVIDER_AVAILABILITY_KEY,
                          {"observed_at": now, **_available_providers(credentials, kept, checks, now)})
        finally:
            conn.close()
        accounts.sort(key=lambda each: (each["provider"], each["email"]))
        return accounts

    def _invalidate_availability(self, now: datetime) -> None:
        conn = db.connect(self._db_path)
        try:
            db.kv_set(conn, PROVIDER_AVAILABILITY_KEY, {"observed_at": now.timestamp()})
        finally:
            conn.close()

    def _refresh_safely(self, now: datetime) -> dict[str, Any]:
        try:
            return self.refresh(now)
        except Exception as exc:
            log.exception("reading the CLIProxyAPI hub failed")
            self._invalidate_availability(now)
            earlier = self._read or {"accounts": [], "read_at": _iso(now)}
            return {"accounts": earlier["accounts"], "error": f"reading the hub failed: {exc}", "read_at": earlier["read_at"],
                    "service": None, "tunnel": None}

    def _refresh_in_background(self, changes: int) -> None:
        found = self._refresh_safely(datetime.now(UTC))
        with self._lock:
            if changes == self._changes:
                self._read = found
            self._reading = False

    def read(self, now: datetime, show_code: bool) -> dict[str, Any] | None:
        """The last finished read with the sign-ins under way, or None on a machine without a hub. A read older than
        REFRESH_SECONDS starts the next one. Before the first has listed the hub's accounts, `accounts` is None; from
        then until it finishes, `service` is None."""
        if not self.configured():
            return None
        with self._lock:
            stale = self._read is None or (now.timestamp() - self._read_started) >= REFRESH_SECONDS
            if stale and not self._reading:
                self._reading = True
                self._read_started = now.timestamp()
                threading.Thread(target=self._refresh_in_background, args=(self._changes,), name="hub-read", daemon=True).start()
            found = self._read or {"accounts": None, "error": None, "read_at": None, "service": None, "tunnel": None}
        return found | self._offers() | {"sign_in": self.sign_in_status(show_code)}

    def _offers(self) -> dict[str, Any]:
        """What the setup lets the page show and do beyond the hub's read: the restarts it gives a command for, in
        config.HUB_RESTARTS order, and whether the tunnel's state is checked at all."""
        return {"restarts": [what for what in config.HUB_RESTARTS if what in self._restarts],
                "tunnel_checked": self._tunnel_state is not None}

    # The user's controls. Each returns the hub as read after the change, for the user's page. The sign-ins under way
    # stay out of it: Cockpit keeps a control's answer for its repeats (actions.KEEP), and a sign-in's link and code
    # reach the page only through `read`.

    def _read_after_change(self, now: datetime) -> dict[str, Any]:
        with self._lock:
            self._changes += 1
            changes = self._changes
        found = self._refresh_safely(now)
        with self._lock:
            if changes == self._changes:
                self._read = found
                self._read_started = now.timestamp()
        return found | self._offers()

    def _account(self, account_id: str) -> dict[str, Any]:
        """The account with this id in the last read."""
        with self._lock:
            accounts = (self._read or {}).get("accounts") or []
        for each in accounts:
            if each["id"] == account_id:
                return each
        raise store.NotFound(f"the hub has no account {account_id}")

    def _change(self, what: str, method: str, path: str, body: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
        """Send the hub one change and read it again; `what` names the change in the log and in a refusal."""
        if not self.configured():
            raise store.Invalid("this machine has no CLIProxyAPI hub")
        try:
            self._call(method, path, body)
        except HubError as exc:
            log.error("%s on the CLIProxyAPI hub failed: %s", what, exc)
            raise store.Invalid(f"{what} failed: {exc}") from None
        log.info("%s on the CLIProxyAPI hub from the page", what)
        return self._read_after_change(now)

    def set_disabled(self, account_id: str, disabled: bool, now: datetime) -> dict[str, Any]:
        """Turn an account off, so the hub sends it no requests and keeps its sign-in, or on again."""
        found = self._account(account_id)
        what = f"turning {'off' if disabled else 'on'} the {found['provider']} account {found['email']}"
        return self._change(what, "PATCH", f"{V0}/auth-files/status", {"name": account_id, "disabled": disabled}, now)

    def sign_out(self, account_id: str, now: datetime) -> dict[str, Any]:
        """Sign an account out of the hub: the hub deletes its file, and only a new sign-in brings it back."""
        found = self._account(account_id)
        what = f"signing out the {found['provider']} account {found['email']}"
        return self._change(what, "DELETE", f"{V0}/auth-files?name={quote(account_id, safe='')}", None, now)

    def end_rest(self, account_id: str, now: datetime) -> dict[str, Any]:
        """End the rest the hub gave an account after a limit or errors, so it gets requests again at once."""
        found = self._account(account_id)
        what = f"ending the rest of the {found['provider']} account {found['email']}"
        return self._change(what, "POST", f"{V8}/routing/cooldown/reset", {"auth_index": found["index"]}, now)

    def set_routing(self, routing: str, now: datetime) -> dict[str, Any]:
        """Choose how the hub shares requests among a provider's accounts: one of ROUTINGS."""
        if routing not in ROUTINGS:
            raise store.Invalid(f"the hub shares requests by one of {', '.join(ROUTINGS)}, not {routing}")
        return self._change(f"sharing requests by {routing}", "PUT", f"{V0}/routing/strategy", {"value": routing}, now)

    def restart(self, what: str) -> dict[str, Any]:
        """Restart the hub's service ("hub") or this machine's tunnel to it ("tunnel"), wait up to BACK_SECONDS for the
        hub to answer again, and read it."""
        if what not in self._restarts:
            raise store.NotFound(f"the hub has no restart called {what}")
        if not self.configured():
            raise store.Invalid("this machine has no CLIProxyAPI hub")
        try:
            ran = subprocess.run(list(self._restarts[what]), check=False, capture_output=True, text=True, timeout=RESTART_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.error("restarting the CLIProxyAPI %s failed: %s", what, exc)
            raise store.Invalid(f"restarting the {what} failed: {exc}") from None
        if ran.returncode != 0:
            said = (ran.stderr.strip() or ran.stdout.strip()).splitlines()[-1:] or [f"exit {ran.returncode}"]
            log.error("restarting the CLIProxyAPI %s failed: %s", what, said[0])
            raise store.Invalid(f"restarting the {what} failed: {said[0][:300]}")
        log.info("restarted the CLIProxyAPI %s from the page", what)
        end = time.monotonic() + BACK_SECONDS
        while time.monotonic() < end:
            left = max(1.0, end - time.monotonic())
            try:
                self._send("GET", f"{V8}/credentials", None, min(REQUEST_TIMEOUT_SECONDS, left))
                break
            except HubError:
                time.sleep(1)
        return self._read_after_change(datetime.now(UTC))

    # Signing accounts into the hub.

    def start_sign_in(self, provider: str, now: datetime) -> str:
        """Start the hub's login for `provider` and wait for its link, which it returns; a second press while one
        waits keeps that one."""
        if provider not in self._commands:
            raise store.NotFound(f"the hub has no sign-in for {provider} accounts")
        if not self.configured():
            raise store.Invalid("this machine has no CLIProxyAPI hub")
        with self._lock:
            waiting = self._logins.get(provider)
            if waiting is not None:
                return waiting.url
            if provider in self._starting:
                raise store.Conflict("the hub's sign-in is starting: wait for its link")
            self._starting.add(provider)
        try:
            return self._run(provider, now).url
        finally:
            with self._lock:
                self._starting.discard(provider)

    def _run(self, provider: str, now: datetime) -> _Login:
        try:
            # Input is a pipe: Claude's login reads the address from it.
            process = subprocess.Popen(
                list(self._commands[provider]), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace", start_new_session=True,
            )
        except OSError as exc:
            raise store.Invalid(f"the hub's sign-in did not start: {exc}") from exc
        timer = threading.Timer(WAIT_SECONDS[provider] + KILL_GRACE_SECONDS, _stop, args=(process,))
        timer.daemon = True
        timer.start()
        login = _Login(provider, process, timer, ends_at=_iso(now + timedelta(seconds=WAIT_SECONDS[provider])))
        linked = threading.Event()
        threading.Thread(target=self._follow, args=(login, linked), name=f"hub-sign-in-{provider}", daemon=True).start()
        linked.wait(LINK_TIMEOUT_SECONDS)
        if not login.ready():
            ended = process.poll()
            _stop(process)
            raise store.Invalid(f"the hub gave no sign-in link: {_said(login, ended)}")
        with self._lock:
            if process.poll() is None:
                self._logins[provider] = login
        log.info("started a %s sign-in on the CLIProxyAPI hub from the page", provider)
        return login

    def _follow(self, login: _Login, linked: threading.Event) -> None:
        assert login.process.stdout is not None
        for line in login.process.stdout:
            login.text += ESCAPES.sub("", line).replace("\r", "")
            if not login.ready():
                login.spot()
                if login.ready():
                    linked.set()
        returncode = login.process.wait()
        login.timer.cancel()
        linked.set()
        with self._lock:
            login.returncode = returncode
            # The hub's logins exit 0 after a failure too; only their closing line says that the account signed in.
            login.signed = returncode == 0 and SIGNED_IN in login.text.lower()
            if login.ready():  # Before the link, `_run` reports the failure.
                if self._logins.get(login.provider) is login:
                    del self._logins[login.provider]
                if login.signed:
                    self._failures[login.provider] = ""
                    self._finished[login.provider] = _iso(datetime.now(UTC))
                    # The next read shows the new account.
                    self._read_started = 0.0
                    log.info("signed a %s account into the CLIProxyAPI hub from the page", login.provider)
                else:
                    self._failures[login.provider] = _said(login, returncode)
                    log.warning("a %s sign-in on the CLIProxyAPI hub ended without signing in: exit %s", login.provider, returncode)
        login.ended.set()

    def submit_callback(self, address: str) -> None:
        """Give Claude's waiting login the address Claude sent the user's browser to, and wait until it signs the
        account in. An address without a code, or from another sign-in, is refused before it reaches the login,
        which ends at the first wrong one."""
        code, state, error = callback_parts(address)
        with self._lock:
            login = self._logins.get("claude")
            if login is None:
                raise store.Invalid("no Claude sign-in waits on the hub: start a new one")
            if login.checking:
                raise store.Conflict("the hub is already checking an address for this sign-in")
            if not code:
                raise store.Invalid(f"Claude did not sign you in: {hide_long_runs(error)[:300]}" if error
                                    else "that address holds no sign-in code: copy the whole address of the page Claude sent you to")
            if state != login.state:
                raise store.Invalid("that address belongs to another sign-in: open Claude's page from this card and copy the address it sends you to")
            login.checking = True
            login.sent = code
        assert login.process.stdin is not None
        try:
            login.process.stdin.write(CLAUDE_CALLBACK.format(code=quote(code, safe=""), state=quote(state, safe="")))
            login.process.stdin.flush()
        except (OSError, ValueError):
            raise store.Invalid("the hub's sign-in ended before the address reached it: start a new one") from None
        if not login.ended.wait(SUBMIT_SECONDS):
            raise store.Invalid(f"the hub was still signing the account in after {SUBMIT_SECONDS} seconds; the card says when it ends")
        if not login.signed:
            with self._lock:
                failure = self._failures.get("claude", "")
            raise store.Invalid(f"the hub did not sign the account in: {failure}")

    def sign_in_status(self, show_code: bool) -> dict[str, dict[str, Any]]:
        """For each provider: the sign-in under way, how the last one failed and when the last one finished. Only the
        user's page gets the link, Codex's code and the failure; Claude's `checking` says the login has the address."""
        found = {}
        with self._lock:
            for provider in self._commands:
                login = self._logins.get(provider)
                waiting = None
                if login is not None and not show_code:
                    waiting = {"ends_at": login.ends_at}
                elif login is not None and provider == "codex":
                    waiting = {"url": login.url, "code": login.code, "ends_at": login.ends_at}
                elif login is not None:
                    waiting = {"url": login.url, "ends_at": login.ends_at, "checking": login.checking}
                found[provider] = {"waiting": waiting, "error": self._failures.get(provider, "") if show_code else "",
                                   "finished_at": self._finished.get(provider)}
        return found


def _stop(process: subprocess.Popen[str]) -> None:
    """End the login's whole process group: SSH, and with it the login on the hub."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _said(login: _Login, returncode: int | None) -> str:
    """The login's last error line for the user's page, without the code, the state or a long run that may be a
    credential."""
    errors = [line.strip() for line in login.text.splitlines() if "error" in line.lower() or "fail" in line.lower()]
    if not errors:
        return "it was still running after a minute" if returncode is None else f"it exited with {returncode}"
    said = errors[-1]
    for secret in (login.code, login.state, login.sent):
        if secret:
            said = said.replace(secret, "…")
    return hide_long_runs(said)[:300]
