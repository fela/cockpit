"""The user's Claude subscription usage: the rate-limit windows and the account (SPEC UI-12).

Claude Code hands its status line the account's rate limits after each API response.
An optional status-line capture script (README, "Claude usage meters") saves the latest ones per
account to `~/.claude/rate-limits.json`, shared by every Claude instance on the host:

    {"accounts": {"sam@example.com": {
        "account": {"emailAddress", "displayName", "organizationName", "organizationType",
                    "organizationRateLimitTier", "billingType", "hasExtraUsageEnabled"},
        "rate_limits": {"five_hour": {"used_percentage", "resets_at"}, "seven_day": {...}},
        "captured_at": <unix seconds>, "session_id": "<uuid>", "config_dir": "<path>"}}}

`account` is the `oauthAccount` of the instance's `.claude.json`. A window Claude Code
leaves out (it does once a window has reset, until the next response) is missing.

Each window is shown as the share used and the share of the window that has passed,
coloured by pace exactly as the status line colours it.

Each window's share passed counts over the window's own length, also while another account's week is
used up (SPEC UI-21, dropped)."""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cockpit import accounts, config, resets
from cockpit.config import Account

log = logging.getLogger(__name__)

# Claude Code's window keys, in display order: (label, name, length in seconds).
WINDOWS = {"five_hour": ("5h", "5-hour", 5 * 3600), "seven_day": ("7d", "7-day", 7 * 86400)}
# A 5-hour window counts as way over for new work (Room.near) once it is this many percent used, or once it is over
# pace (the status line's red) and its use so far, kept up, fills it before FILLS_BY of it has passed.
FIVE_HOUR_NEAR = 90
FILLS_BY = 0.8
# New work goes to the account at no limit with the highest score (pick_score): the product of four parts, each
# between 0 and 1 and smooth in its numbers, so a small change in use or time moves the pick only a little. How soon
# the week resets: exp(-hours / WEEK_HOURS); a week ending a day sooner weighs 1.6 times, six days 20 times.
WEEK_HOURS = 48
WEEK_LEFT_MIDPOINT = 2
WEEK_LEFT_SPREAD = 0.2
# What each session already running on the account is expected to use yet, in percent of its week: the average session's
# use above. The week left counts net of it (Room.week_left), so an account near its limit with sessions running takes
# no new one (SPEC SESSION-17).
RUNNING_SESSION_USE = 1.5
# The 5-hour window lasting: 1 / (1 + exp((projected - FIVE_HOUR_MIDPOINT) / FIVE_HOUR_SPREAD)), where projected is
# its use at its end at its rate so far; 80 % projected gives 0.95, 95 % 0.5, 110 % 0.05.
FIVE_HOUR_MIDPOINT = 95
FIVE_HOUR_SPREAD = 5
# The 5-hour window's rate counts over at least this share of it, so a fast first few minutes is no pace yet.
FIVE_HOUR_MIN_SPAN = 0.5
# The week lasting: WEEK_EARLY_FLOOR + (1 - WEEK_EARLY_FLOOR) / (1 + exp((early - WEEK_EARLY_MIDPOINT) /
# WEEK_EARLY_SPREAD)), where early is how many hours before its reset the week runs out at its rate so far
# (Room.week_runs_out_early); lasting to its reset gives 0.98, running out 12 hours early 0.89, a day early 0.53, two
# days or more about the floor. The floor keeps a week with room ahead of one with none (SPEC SESSION-17). A week that
# resets first but is on course to run out days before then gives new work to an account with room, while one nearly
# finished a day before its reset still wins.
WEEK_EARLY_MIDPOINT = 24
WEEK_EARLY_SPREAD = 6
WEEK_EARLY_FLOOR = 0.05
# The week's rate counts over at least this many hours, so a busy first day is the pace, not its first hour.
WEEK_MIN_HOURS = 24
PLAN_NAMES = {"claude_max": "Max", "claude_pro": "Pro", "claude_team": "Team", "claude_enterprise": "Enterprise"}


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def pace(used: float, elapsed_share: float) -> str:
    """How far use runs ahead of the clock, the status line's rule.

    lead = used% - elapsed%, against two thresholds that move linearly over the window:
    `under` (green) below +2 at the start to -5 at the end, `over` (red) above +20 to 0,
    `near` (orange) between them.
    """
    lead = used - elapsed_share * 100
    if lead > 20 - 20 * elapsed_share:
        return "over"
    if lead < 2 - 7 * elapsed_share:
        return "under"
    return "near"


def week_counted(email: str, used: float, resets_at: float, now: float) -> dict[str, Any]:
    """A 7-day window's share passed (`elapsed`) and `pace` at `now`, counted to the account's `last_use` (config.Setup) when that falls
    before the reset and is still ahead, else to the reset. `counted_until` names that earlier end (None without one)
    and `span` the counted length in percent of seven days, which the weekly lanes draw. The pace goes by `used`, the
    share the account reports: a banked usage-limit reset adds no room, since spending one is the user's choice."""
    length = WINDOWS["seven_day"][2]
    start = resets_at - length
    last_use = config.setup().last_use.get(email)
    ends = last_use if last_use is not None and now < last_use and start < last_use < resets_at else resets_at
    elapsed_share = min(1.0, max(0.0, (now - start) / (ends - start)))
    return {
        "elapsed": round(elapsed_share * 100),
        "pace": pace(used, elapsed_share),
        "counted_until": _iso(ends) if ends != resets_at else None,
        "span": round((ends - start) / length * 100, 2),
    }


def window(key: str, reported: dict[str, Any] | None, now: float, email: str) -> dict[str, Any]:
    """One limit window of `email`'s account at `now`, from its reported {used_percentage, resets_at} (None when not
    reported): its use, share of the time gone and pace, as the usage bars draw them."""
    label, name, length = WINDOWS[key]
    window: dict[str, Any] = {
        "key": key, "label": label, "name": name, "used": None, "elapsed": None, "pace": None, "resets_at": None,
        "counted_until": None, "span": 100,
    }
    # A window that has reset since the numbers were saved has unknown use.
    if reported is None or reported["resets_at"] <= now:
        return window
    used = float(reported["used_percentage"])
    if key == "seven_day":
        return window | week_counted(email, used, reported["resets_at"], now) | {"used": round(used), "resets_at": _iso(reported["resets_at"])}
    elapsed_share = min(1.0, max(0.0, 1 - (reported["resets_at"] - now) / length))
    return window | {
        "used": round(used),
        "elapsed": round(elapsed_share * 100),
        "pace": pace(used, elapsed_share),
        "resets_at": _iso(reported["resets_at"]),
    }


def model_week(limit: dict[str, Any], now: float, email: str) -> dict[str, Any]:
    """The weekly limit that counts one model's use alone on `email`'s account at `now`, from its entry in the shape
    resets.model_limits gives, as the usage bars draw a week, with the model's name as `model` and `label`. `used` is
    the share of this limit's own allowance. A limit without numbers, or one that has reset since they were read, has
    unknown use."""
    week: dict[str, Any] = {
        "key": "model_week", "model": limit["model"], "label": limit["model"], "name": f"{limit['model']} weekly",
        "used": None, "elapsed": None, "pace": None, "resets_at": None, "counted_until": None, "span": 100,
    }
    reported = limit["reported"]
    if reported is None or reported["resets_at"] <= now:
        return week
    used = float(reported["used_percentage"])
    return week | week_counted(email, used, reported["resets_at"], now) | {"used": round(used), "resets_at": _iso(reported["resets_at"])}


def _week_start(week: dict[str, Any] | None, now: float) -> float | None:
    """When the account's current 7-day window started. An account's windows follow one another without a gap
    (each resets at the same hour a week after the last, used or not), so a window that reset
    since its numbers were saved was followed by one that started at that reset. None without numbers."""
    if week is None:
        return None
    length = WINDOWS["seven_day"][2]
    resets = week["resets_at"]
    while resets <= now:
        resets += length
    return resets - length


def _plan(account: dict[str, Any]) -> str:
    name = PLAN_NAMES.get(account.get("organizationType") or "", account.get("organizationType") or "unknown plan")
    multiple = re.search(r"_(\d+x)$", account.get("organizationRateLimitTier") or "")
    return f"{name} {multiple[1]}" if multiple else name


@dataclass(frozen=True)
class Preference:
    """The user's choice on the page that new work goes to the account folder named `account` until `until`, which
    was `minutes` (one of accounts.PREFER_MINUTES) after the choice (accounts.prefer)."""

    account: str
    until: datetime
    minutes: int


@dataclass(frozen=True)
class Room:
    """An account's room for new work at one moment (SPEC SESSION-17)."""

    # The share of the week used (percent), when the week resets (Unix seconds) and the hours until then.
    week_used: float
    week_resets: float
    week_hours: float
    # The 5-hour window's use at its end (percent) if it goes on at its rate so far (FIVE_HOUR_MIN_SPAN); 0 for an
    # empty window.
    five_projected: float
    # Unix seconds when the last of its used-up windows resets; None while no window is used up.
    blocked_until: float | None
    # Why a new session there would soon stop on its 5-hour limit, in words; "" when it would not: the window is way
    # over, near full (FIVE_HOUR_NEAR) or over pace and on course to fill well before it resets (FILLS_BY).
    near: str
    # Of its banked usage-limit resets (resets.spendable), how many would end its limit now: those that clear every
    # window at its limit. They add no room: spending one is the user's choice in Claude Code's limit dialog, so
    # they only rank an account first among those at a limit (pick_key). 0 while no window is at its limit.
    banked: int
    # Unix seconds when its soonest banked reset that clears its week expires (resets.expiring), while it is at no
    # limit and its 5-hour window is not near one; None otherwise. Such an account gets new work first, so its
    # week is used up before the reset is lost (pick_key).
    reset_ends: float | None
    # How many sessions run on it now (sessions.running).
    running: int

    @property
    def week_left(self) -> float:
        """The share of the week left for a new session (percent): what is not used yet, less what the sessions
        running on it are expected to use (RUNNING_SESSION_USE); below 0 when they are expected to use more."""
        return 100 - self.week_used - self.running * RUNNING_SESSION_USE

    @property
    def week_runs_out_early(self) -> float:
        """How many hours before its reset the week runs out if use goes on at its rate since the week started
        (counted over at least WEEK_MIN_HOURS), from what is left for a new session (week_left); all its hours when
        nothing is left, below 0 when it lasts past its reset, -inf for an unused week with room."""
        if self.week_left <= 0:
            return self.week_hours
        if self.week_used <= 0:
            return -math.inf
        elapsed = max(WINDOWS["seven_day"][2] / 3600 - self.week_hours, WEEK_MIN_HOURS)
        return self.week_hours - max(self.week_left, 0.0) * elapsed / self.week_used


def room(
    record: dict[str, Any] | None, now: float, spendable: list[frozenset[str]], expiring: float | None, running: int,
) -> Room:
    """The room in an account's latest record, with `running` sessions on it now. A window that has reset since the
    record counts as empty, and the week then runs to the reset after it (_week_start); an account with no record
    counts as a fresh week."""
    limits = record["rate_limits"] if record else {}
    length = WINDOWS["seven_day"][2]
    week = limits.get("seven_day")
    if week is None:
        used, resets = 0.0, now + length
    elif week["resets_at"] <= now:
        used, resets = 0.0, (_week_start(week, now) or now) + length
    else:
        used, resets = float(week["used_percentage"]), week["resets_at"]
    spent = {
        key: window["resets_at"] for key, window in limits.items()
        if window and float(window["used_percentage"]) >= 100 and window["resets_at"] > now
    }
    hours = max(resets - now, 60) / 3600
    five = limits.get("five_hour")
    near = five_near = ""
    five_projected = 0.0
    if five and five["resets_at"] > now:
        five_used = float(five["used_percentage"])
        gone = min(1.0, max(0.0, 1 - (five["resets_at"] - now) / WINDOWS["five_hour"][2]))
        five_projected = five_used + five_used / max(gone, FIVE_HOUR_MIN_SPAN) * (1 - gone)
        if five_used >= FIVE_HOUR_NEAR or (pace(five_used, gone) == "over" and five_used >= 100 * gone / FILLS_BY):
            near = five_near = f"its 5-hour window is {five_used:.0f} % used with {gone * 100:.0f} % of it gone"
    banked = sum(1 for clears in spendable if spent.keys() <= clears) if spent else 0
    return Room(
        week_used=used, week_resets=resets, week_hours=hours, five_projected=five_projected, blocked_until=max(spent.values()) if spent else None, near=near, banked=banked,
        reset_ends=None if spent or five_near else expiring, running=running,
    )


def pick_score(room: Room) -> float:
    """How much an account at no limit suits new work, between 0 and 1: its week ending soon (WEEK_HOURS), its week's
    room for a session once the sessions running on it have had theirs (Room.week_left, WEEK_LEFT_MIDPOINT), its week
    lasting to its reset at its rate so far (Room.week_runs_out_early, WEEK_EARLY_MIDPOINT) and its 5-hour window
    lasting (FIVE_HOUR_MIDPOINT), multiplied. A week about to end is (almost) finished before its room is lost, a week
    nearly used up still gets work while a session fits beside those running, a week on course to run out days before
    its reset sends work elsewhere, and so does a 5-hour window on course to run out, the sooner the less the week
    matters."""
    soon = math.exp(-room.week_hours / WEEK_HOURS)
    fits = 1 / (1 + math.exp(min(50.0, (WEEK_LEFT_MIDPOINT - room.week_left) / WEEK_LEFT_SPREAD)))
    holds = WEEK_EARLY_FLOOR + (1 - WEEK_EARLY_FLOOR) / (
        1 + math.exp(min(50.0, (room.week_runs_out_early - WEEK_EARLY_MIDPOINT) / WEEK_EARLY_SPREAD))
    )
    lasts = 1 / (1 + math.exp(min(50.0, (room.five_projected - FIVE_HOUR_MIDPOINT) / FIVE_HOUR_SPREAD)))
    return soon * fits * holds * lasts


def pick_key(room: Room, preferred: bool) -> tuple[int, int, float]:
    """Sorts accounts for new work, best first: the one the user prefers for now (Preference), while it is at no
    limit; then one holding a banked reset that expires (Room.reset_ends), the soonest-expiring first, so its week is
    used up before the reset is lost; then those at no limit by pick_score, highest first; then those at a limit: one
    with a banked reset first (the user can spend it in Claude Code's limit dialog), then by the earliest end of the
    limit."""
    if room.blocked_until is not None:
        return (4, 0 if room.banked else 1, room.blocked_until)
    if preferred:
        return (-1, 0, 0.0)
    if room.reset_ends is not None:
        return (0, 0, room.reset_ends)
    return (1, 0, -pick_score(room))


def _preferred(folder: Account | None, preference: Preference | None) -> Preference | None:
    """The user's preference when it is for `folder`, else None."""
    if folder is None or preference is None or folder.name != preference.account:
        return None
    return preference


def _folder_fields(email: str, folder: Account | None, now: datetime) -> dict[str, Any]:
    """The folder signed in with `email`: its name, its sign-in end, whether it is signed out (accounts.signed_out)
    and its banked resets; empty for none."""
    if folder is None:
        return {"folder": "", "sign_in_ends": None, "signed_out": False, "resets": None}
    signed_in_until = accounts.sign_in_expires(folder)
    return {
        "folder": folder.name, "sign_in_ends": signed_in_until and signed_in_until.isoformat(),
        "signed_out": accounts.signed_out(folder, now), "resets": resets.known(email),
    }


def latest(address: str, record: dict[str, Any] | None) -> dict[str, Any] | None:
    """The newest usage of the account signed in with `address`: the status line's record, or Cockpit's own
    reading of the usage endpoint (resets.reading) where that is newer, as it is while no session runs on the
    account. The endpoint's reading holds only the windows; the record's account and session stay."""
    polled = resets.reading(address)
    if polled is None or (record is not None and record["captured_at"] >= polled["captured_at"]):
        return record
    return {"account": {}, "session_id": "", "config_dir": "", **(record or {}), **polled}


def read(
    path: Path, now: datetime, in_use: list[Account], preference: Preference | None, running: dict[str, int],
) -> dict[str, Any]:
    """The saved usage, each record labelled with the name of the folder in `in_use` signed in with its address
    (empty for a record no folder is signed in with now), that folder's sign-in end and its banked resets
    (resets.py), leaving out a retired account's record (accounts.retired). Each record is the account's newest usage (latest). A folder whose address has neither a record
    nor a reading yet gets one with unknown use and no report time. The accounts with a folder come first, in the
    order new work picks them (pick_key), then the others, reported most recently first. The account the user
    prefers for now (`preference`) carries when that ends as `preferred_until` and the length chosen as
    `preferred_minutes`. `running` counts the sessions on
    each folder by name (sessions.running); each folder's count is its `running`."""
    folders = {address: account for account in in_use if (address := accounts.email(account))}
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        data = {"accounts": {}}
    except (OSError, ValueError) as exc:
        log.error("reading Claude usage from %s failed: %s", path, exc)
        return {"accounts": [], "error": f"{path} is unreadable: {exc}"}
    seconds = now.timestamp()
    listed: list[dict[str, Any]] = []
    keys: dict[str, tuple[int, int, float]] = {}
    try:
        saved = data["accounts"]
        # The file's order first, so records reported at the same moment keep it.
        emails = [*saved, *(email for email in folders if email not in saved)]
        records = {email: record for email in emails if (record := latest(email, saved.get(email))) is not None}
        gone = [each for each in config.ACCOUNTS if accounts.retired(each)]
        retired_emails = {accounts.email(each) for each in gone}
        retired_folders = {str(each.folder) for each in gone}
        for email, record in records.items():
            # By folder too: a signed-out folder can lose the address it held.
            if email in retired_emails or record.get("config_dir") in retired_folders:
                continue
            account = record["account"]
            limits = record["rate_limits"]
            week_start = _week_start(limits.get("seven_day"), seconds)
            folder = folders.get(email)
            on_it = running.get(folder.name, 0) if folder else 0
            room_now = room(record, seconds, resets.spendable(email, now), resets.expiring(email, now), on_it)
            preferred = _preferred(folder, preference)
            keys[email] = pick_key(room_now, preferred is not None)
            listed.append({
                "email": email,
                **_folder_fields(email, folder, now),
                "preferred_until": preferred and _iso(preferred.until.timestamp()),
                "preferred_minutes": preferred and preferred.minutes,
                "pick_score": round(pick_score(room_now), 4),
                "running": on_it,
                "at_limit_until": room_now.blocked_until and _iso(room_now.blocked_until),
                "name": account.get("displayName") or "",
                "plan": _plan(account) if account else "",
                "organization": account.get("organizationName") or "",
                "extra_usage": bool(account.get("hasExtraUsageEnabled")),
                "captured_at": _iso(record["captured_at"]),
                "session_id": record["session_id"],
                "windows": [window(key, limits.get(key), seconds, email) for key in WINDOWS],
                "model_weeks": [model_week(each, seconds, email) for each in resets.model_limits_of(email)],
                "week_start": week_start and _iso(week_start),
            })
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        log.error("Claude usage in %s has an unexpected shape: %r", path, exc)
        return {"accounts": [], "error": f"{path} has an unexpected shape: {exc!r}"}
    for email, folder in folders.items():
        if email in records:
            continue
        on_it = running.get(folder.name, 0)
        room_now = room(None, seconds, resets.spendable(email, now), resets.expiring(email, now), on_it)
        preferred = _preferred(folder, preference)
        keys[email] = pick_key(room_now, preferred is not None)
        listed.append({
            "email": email,
            **_folder_fields(email, folder, now),
            "preferred_until": preferred and _iso(preferred.until.timestamp()),
            "preferred_minutes": preferred and preferred.minutes,
            "pick_score": round(pick_score(room_now), 4),
            "running": on_it,
            "at_limit_until": None,
            "name": "",
            "plan": "",
            "organization": "",
            "extra_usage": False,
            "captured_at": None,
            "session_id": "",
            "windows": [window(key, None, seconds, email) for key in WINDOWS],
            "model_weeks": [],
            "week_start": None,
        })
    listed.sort(key=lambda entry: entry["captured_at"] or "", reverse=True)
    # Ties go to the earlier folder in `in_use`, as accounts.pick breaks them; a signed-out folder takes no new work
    # (accounts.usable), so it comes after the others.
    order = {email: index for index, email in enumerate(folders)}
    out = {email for email, account in folders.items() if accounts.signed_out(account, now)}
    listed.sort(key=lambda entry: (
        (0, entry["email"] in out, *keys[str(entry["email"])], order[str(entry["email"])])
        if entry["folder"] else (1, False, 0, 0, 0)
    ))
    return {"accounts": listed, "error": ""}
