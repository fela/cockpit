"""The Usage page's graph of how much of each provider's weekly allowance is left (SPEC UI-87): over the last week as
the hub accounts' limits said it, and over the next week if nothing more is used and at the last week's pace.

Each read of the hub (hub.Hub._accounts) keeps every change in an account's limits as a row of hub_limit_readings
(record): a limit without a row, or a later reading whose share used moved by USED_STEP or more or whose reset time
moved by more than RESET_JITTER_SECONDS from that limit's last row. Rows stay KEEP_SECONDS.

The graph is drawn from those rows (graph). "Left" is 100 less the share used, in percent. A line is a list of
(time, left) points joined by straight segments, and a reset is two points at the same time: the value before it,
then 100. Each account's weekly limit has three lines (account_lines):

- past, over the last PAST_SECONDS (past_line): 100 at each window's start, a week before its reset; each reading;
  the last reading of a window held until its reset, then 100 until the next window starts; and the value now.
- idle, over the next AHEAD_SECONDS (idle_line): the value now held until the reset, then 100.
- pace (pace_lines): from the value now, falling at the account's rate, how fast the past line fell since the limit's
  first kept reading (pace_rate), and never below 0, then 100 at the reset and falling again. The stretch before that
  first reading, which runs straight from 100 at the window's start, stays out of the rate: it is set by the reset
  time, not by any use seen, and a provider can clear a window's use without moving its reset. While an account has
  nothing left, its rate moves in equal parts to the group's accounts that have some left, as the hub sends work to an
  account with allowance left; so a group's pace falls at one rate, however far off its accounts' resets are.

The graph's groups (GROUPS) are Claude's week, Claude's weekly limit for Fable alone and Codex's week. A group's lines
are the mean of its accounts' lines (mean), its past line only from the time every account's is known.
"""

from __future__ import annotations

import bisect
import itertools
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from cockpit import codex, db

WEEK = 7 * 86400
PAST_SECONDS = WEEK
AHEAD_SECONDS = WEEK
# The shortest time a pace is measured over, so a window known for an hour does not set the pace for a week.
PACE_FLOOR_SECONDS = 86400
# Two readings of a limit belong to the same window when their reset times differ by this much or less: a provider's
# reset time for one window moves by a little between readings.
RESET_JITTER_SECONDS = 120
# The smallest change in the share used, in percent points, that keeps a row.
USED_STEP = 0.5
KEEP_SECONDS = 30 * 86400
# The rows the graph reads: the window that ran at the graph's left edge started at most a week before that edge.
READ_SECONDS = PAST_SECONDS + WEEK
# A weekly limit that counts one model alone is kept as this and the model's name (hub.limits_now's `models`).
MODEL = "model:"
# The graph's groups, in order: the provider, the limit and the model the limit counts alone (None for the week).
GROUPS = (("claude", "seven_day", None), ("claude", MODEL + "Fable", "Fable"), ("codex", "seven_day", None))

Point = tuple[float, float]


@dataclass(frozen=True)
class Reading:
    """A row of hub_limit_readings: an account's limit as its provider said it at `at`, in epoch seconds."""

    account: str
    limit_key: str
    at: float
    used: float
    resets_at: float


@dataclass(frozen=True)
class Lines:
    """One account's weekly limit on the graph: the share left now, its reset (None once it passed), its lines so far and
    if nothing more is used, and its rate of use in percent points per second (pace_rate)."""

    left_now: float
    resets_at: float | None
    past: list[Point]
    idle: list[Point]
    rate: float


# hub_limit_readings holds each change in a hub account's limits: the account ("provider/id", hub.limits_key), the
# limit ("seven_day", "five_hour" or MODEL and the model's name), when the provider said it and what it said, the share
# used in percent and the reset time, both times in epoch seconds.
TABLE = """
CREATE TABLE IF NOT EXISTS hub_limit_readings (
    account TEXT NOT NULL,
    limit_key TEXT NOT NULL,
    at REAL NOT NULL,
    used REAL NOT NULL,
    resets_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS hub_limit_readings_by_limit ON hub_limit_readings(account, limit_key, at);
"""


def create_table(conn: sqlite3.Connection) -> None:
    """Create hub_limit_readings when it is missing (hub.Hub, when the setup has a [hub] table)."""
    conn.executescript(TABLE)


def record(conn: sqlite3.Connection, kept: dict[str, dict[str, Any]], now: float) -> None:
    """Keep each reading in `kept` (each account's limits as hub.limits_now gives them, by hub.limits_key) that changed
    from its account and limit's last row, and drop the rows older than KEEP_SECONDS with them. A read that changed
    nothing writes nothing."""
    latest = {
        (each["account"], each["limit_key"]): each
        for each in conn.execute(
            "SELECT account, limit_key, MAX(at) AS at, used, resets_at FROM hub_limit_readings GROUP BY account, limit_key"
        )
    }
    changed = []
    for account, limits in kept.items():
        named = [*limits["windows"].items(), *((MODEL + model, each) for model, each in limits["models"].items())]
        for limit_key, reading in named:
            at = datetime.fromisoformat(reading["captured_at"]).timestamp()
            used, resets_at = reading["used_percentage"], reading["resets_at"]
            last = latest.get((account, limit_key))
            if last is None or (
                at > last["at"]
                and (abs(used - last["used"]) >= USED_STEP or abs(resets_at - last["resets_at"]) > RESET_JITTER_SECONDS)
            ):
                changed.append((account, limit_key, at, used, resets_at))
    if not changed:
        return
    with db.transaction(conn):
        conn.executemany(
            "INSERT INTO hub_limit_readings (account, limit_key, at, used, resets_at) VALUES (?, ?, ?, ?, ?)", changed
        )
        conn.execute("DELETE FROM hub_limit_readings WHERE at < ?", (now - KEEP_SECONDS,))


def rows(conn: sqlite3.Connection, now: float) -> list[Reading]:
    """The rows of hub_limit_readings the graph at `now` is drawn from, oldest first: those of the last READ_SECONDS."""
    found = conn.execute(
        "SELECT account, limit_key, at, used, resets_at FROM hub_limit_readings WHERE at >= ? ORDER BY at",
        (now - READ_SECONDS,),
    )
    return [Reading(*each) for each in found]


def codex_weeks(conn: sqlite3.Connection) -> list[codex.HubReading]:
    """Every kept row of the Codex accounts' weekly limits, oldest first, for the Codex use of a folder on the hub
    (codex.AccountIndex._hub_shares)."""
    found = conn.execute(
        "SELECT account, at, used, resets_at FROM hub_limit_readings WHERE account LIKE 'codex/%' AND limit_key = 'seven_day'"
        " ORDER BY at"
    )
    return [codex.HubReading(*each) for each in found]


def left(used: float) -> float:
    """The share of a limit left, in percent, from the share used."""
    return min(100.0, max(0.0, 100.0 - used))


def left_now(last: Reading, now: float) -> float:
    """The share left at `now` after the limit's `last` reading: 100 once that reading's window has reset."""
    return left(last.used) if last.resets_at > now else 100.0


def same_window(earlier: Reading, later: Reading) -> bool:
    return abs(later.resets_at - earlier.resets_at) <= RESET_JITTER_SECONDS


def past_line(readings: list[Reading], now: float) -> list[Point]:
    """One account's limit from its first window's start to `now`, from its `readings` oldest first, none after `now`.
    A window's start is added only where the line stands before it: before the first window, or after the last
    window's reset. When a window's readings come before the last window's reset (the provider moved the reset), the
    line goes on from the last reading, with the start only when it lies after that reading."""
    line: list[Point] = []
    previous: Reading | None = None
    for reading in readings:
        if previous is None or not same_window(previous, reading):
            since = float("-inf")
            if previous is not None and reading.at >= previous.resets_at:
                line += [(previous.resets_at, left(previous.used)), (previous.resets_at, 100.0)]
                since = previous.resets_at
            elif previous is not None:
                since = previous.at
            start = reading.resets_at - WEEK
            if since < start <= reading.at:
                line.append((start, 100.0))
        line.append((reading.at, left(reading.used)))
        previous = reading
    last = readings[-1]
    if last.resets_at <= now:
        line += [(last.resets_at, left(last.used)), (last.resets_at, 100.0)]
    line.append((now, left_now(last, now)))
    return line


def value_at(line: list[Point], t: float) -> tuple[float, float]:
    """The line's value at `t`, which lies within its span, as (from before, from after): the two differ where the
    line jumps at `t`."""
    first = bisect.bisect_left(line, t, key=lambda point: point[0])
    last = bisect.bisect_right(line, t, key=lambda point: point[0])
    if first < last:
        return line[first][1], line[last - 1][1]
    if first == 0 or first == len(line):
        raise ValueError(f"{t} lies outside the line from {line[0][0]} to {line[-1][0]}")
    (t0, v0), (t1, v1) = line[first - 1], line[first]
    value = v0 + (v1 - v0) * (t - t0) / (t1 - t0)
    return value, value


def clip(line: list[Point], start: float, end: float) -> list[Point]:
    """The part of `line` from `start` to `end`, with a point where it crosses either; a jump at its end stays."""
    first, last = max(line[0][0], start), min(line[-1][0], end)
    inner = [point for point in line if first < point[0] < last]
    before, after = value_at(line, last)
    return [(first, value_at(line, first)[1]), *inner, *dict.fromkeys([(last, before), (last, after)])]


def pace_rate(past: list[Point], now: float) -> float:
    """How fast the share left fell along the `past` line, in percent points per second: what it fell by (rises left
    out) over the time since the line starts, PACE_FLOOR_SECONDS at the least. A line of one point fell by nothing."""
    fallen = sum(max(0.0, earlier[1] - later[1]) for earlier, later in itertools.pairwise(past))
    return fallen / max(now - past[0][0], PACE_FLOOR_SECONDS)


def idle_line(left_now: float, resets_at: float | None, now: float, end: float) -> list[Point]:
    """The share left from `now` to `end` if nothing more is used."""
    if resets_at is None:
        return [(now, left_now), (end, left_now)]
    return clip([(now, left_now), (resets_at, left_now), (resets_at, 100.0), (max(resets_at, end), 100.0)], now, end)


def pace_lines(accounts: list[Lines], now: float, end: float) -> list[list[Point]]:
    """The share left of each of a group's `accounts` from `now` to `end` if use goes on at their rates: each account
    falls at its own rate plus an equal part of the rates of the accounts with nothing left, stays at 0 once there, and
    starts afresh at 100 at its reset."""
    lefts = [each.left_now for each in accounts]
    resets = [each.resets_at for each in accounts]
    lines: list[list[Point]] = [[(now, value)] for value in lefts]
    slopes: list[float | None] = [None] * len(accounts)
    t = now
    while t < end:
        with_some = sum(1 for value in lefts if value > 0)
        moved = sum(each.rate for each, value in zip(accounts, lefts, strict=True) if value <= 0)
        speeds = [
            each.rate + moved / with_some if value > 0 else 0.0 for each, value in zip(accounts, lefts, strict=True)
        ]
        empty_at = [
            t + value / speed if speed > 0 else float("inf") for value, speed in zip(lefts, speeds, strict=True)
        ]
        then = min([end, *empty_at, *(each for each in resets if each is not None and each > t)])
        for i, line in enumerate(lines):
            lefts[i] = 0.0 if empty_at[i] <= then else lefts[i] - speeds[i] * (then - t)
            # A point on the straight run from the one before replaces it.
            if slopes[i] == speeds[i]:
                line.pop()
            line.append((then, lefts[i]))
            slopes[i] = speeds[i]
            if resets[i] == then:
                line.append((then, 100.0))
                lefts[i], resets[i], slopes[i] = 100.0, None, None
        t = then
    return lines


def account_lines(readings: list[Reading], now: float) -> Lines:
    """One account's weekly limit on the graph at `now`, from its `readings` oldest first, none after `now`."""
    last = readings[-1]
    resets_at = last.resets_at if last.resets_at > now else None
    share = left_now(last, now)
    drawn = past_line(readings, now)
    past = clip(drawn, now - PAST_SECONDS, now)
    # The pace is measured from the first kept reading on: before it the line is a guess from the reset time.
    measured = clip(drawn, max(now - PAST_SECONDS, readings[0].at), now)
    end = now + AHEAD_SECONDS
    return Lines(share, resets_at, past, idle_line(share, resets_at, now, end), pace_rate(measured, now))


def mean(lines: list[list[Point]], start: float, end: float) -> list[Point]:
    """The mean of `lines` from `start` to `end`, a span each of them covers: a point at each time any of them has one,
    two where any jumps (the mean before, then the mean after)."""
    times = sorted({t for line in lines for t, _ in line if start <= t <= end} | {start, end})
    found: list[Point] = []
    for t in times:
        values = [value_at(line, t) for line in lines]
        before = sum(each[0] for each in values) / len(values)
        after = sum(each[1] for each in values) / len(values)
        found += dict.fromkeys([(t, before), (t, after)])
    return found


def rounded(line: list[Point]) -> list[list[float]]:
    """`line` for the page: times in whole seconds, shares to a tenth, without a point that repeats the one before."""
    found: list[list[float]] = []
    for t, value in line:
        point: list[float] = [round(t), round(value, 1)]
        if not found or point != found[-1]:
            found.append(point)
    return found


def graph(readings: list[Reading], accounts: list[str], now: float) -> dict[str, Any]:
    """The graph at `now` of the `accounts` (by hub.limits_key, in the order the graph lists them) from their
    `readings` (rows), by group (GROUPS). A group holds the accounts with a reading of its limit, and the graph the
    groups with any; the 5-hour window is left out."""
    by_limit: dict[tuple[str, str], list[Reading]] = {}
    for reading in sorted(readings, key=lambda each: each.at):
        if reading.at <= now:
            by_limit.setdefault((reading.account, reading.limit_key), []).append(reading)
    end = now + AHEAD_SECONDS
    groups = []
    for provider, limit_key, model in GROUPS:
        found = []
        for key in accounts:
            owner, account_id = key.split("/", 1)
            if owner == provider and (key, limit_key) in by_limit:
                found.append((account_id, account_lines(by_limit[key, limit_key], now)))
        if not found:
            continue
        lines = [each for _, each in found]
        paces = pace_lines(lines, now, end)
        known_from = max(each.past[0][0] for each in lines)
        groups.append(
            {
                "provider": provider,
                "model": model,
                "now": round(sum(each.left_now for each in lines) / len(lines), 1),
                "past": rounded(mean([each.past for each in lines], known_from, now)),
                "idle": rounded(mean([each.idle for each in lines], now, end)),
                "pace": rounded(mean(paces, now, end)),
                "accounts": [
                    {
                        "id": account_id,
                        "now": round(each.left_now, 1),
                        "resets_at": None if each.resets_at is None else round(each.resets_at),
                        "past": rounded(each.past),
                        "idle": rounded(each.idle),
                        "pace": rounded(pace),
                    }
                    for (account_id, each), pace in zip(found, paces, strict=True)
                ],
            }
        )
    return {"now": round(now), "start": round(now - PAST_SECONDS), "end": round(end), "groups": groups}
