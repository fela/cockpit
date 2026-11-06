"""The user's time, from what Cockpit records (SPEC TRACK-3).

Three records feed it. Step timers (`time_spans`, SPEC TRACK-2) say when the user
worked on a step. Each feedback's `seconds` say how long the user wrote it, up to
when they sent it; while they write, the page pauses the step's timer, and
where both still ran the step's timer counts. Input minutes (`input_minutes`) say when the user used a
Cockpit page: each minute with a key, a click or tap, the wheel or the pointer
moving. Input minutes at most `ACTIVE_GAP` apart make one stretch of active
time, which ends a minute after its last input. Worked time is active time and
timer and feedback time together; "active without a timer" is active time
while neither ran. Days are the user's days, in the time zone the page sends. A step's kind
is the first word of its need: review, merge, decide, qa. A step's size is how big its session
judged the user's part (SPEC NEXT-35); the timers' time per size shows how well the sizes fit.

A step's progress is how much of the user's own time on the item's work its session judged to be behind them
. Once an item finished (stage done), each
estimate given on it is checked against the share of the user's timer time on the item's steps, up to the
finish, that had passed when it was given. The page takes the estimates' typical lead off the bar once enough
of them are checked (progress_correction).
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime, time, timedelta
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from cockpit import db

# Input minutes this close make one stretch: reading a step for a few minutes without touching the
# page is still work.
ACTIVE_GAP = timedelta(minutes=5)
MINUTE = timedelta(minutes=1)
MINUTE_FORMAT = "%Y-%m-%dT%H:%M"
SIZES = ("small", "medium", "large")
# Checked estimates of the user's time, and finished items they come from, before the bar is corrected by them.
CORRECTION_FROM = {"estimates": 8, "items": 3}

Interval = tuple[datetime, datetime]


def note_input(conn: sqlite3.Connection, now: datetime) -> None:
    """Record the minute of the user's input on a page; more input in the same minute adds nothing."""
    conn.execute("INSERT OR IGNORE INTO input_minutes (minute) VALUES (?)", (now.strftime(MINUTE_FORMAT),))


def _merged(intervals: list[Interval]) -> list[Interval]:
    merged: list[Interval] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _clipped(intervals: list[Interval], start: datetime, end: datetime) -> list[Interval]:
    return [(max(a, start), min(b, end)) for a, b in intervals if a < end and b > start]


def _without(intervals: list[Interval], cuts: list[Interval]) -> list[Interval]:
    """`intervals` minus `cuts`; both merged and sorted."""
    left: list[Interval] = []
    for start, end in intervals:
        for cut_start, cut_end in cuts:
            if cut_end <= start or cut_start >= end:
                continue
            if cut_start > start:
                left.append((start, cut_start))
            start = cut_end
            if start >= end:
                break
        if start < end:
            left.append((start, end))
    return left


def _seconds(intervals: list[Interval]) -> int:
    return round(sum((end - start).total_seconds() for start, end in intervals))


def step_kind(need: str) -> str:
    """The first word of a need: "review@a1", "merge-app-40" and "qa:x" are review, merge and qa."""
    return re.match(r"[a-z]*", need.lower()).group() or need  # ty: ignore[unresolved-attribute]


def _time(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(UTC)


def active_stretches(minutes: list[datetime]) -> list[Interval]:
    """Stretches of input minutes at most ACTIVE_GAP apart, each ending a minute after its last input."""
    stretches: list[list[datetime]] = []
    for minute in sorted(minutes):
        if stretches and minute - stretches[-1][1] <= ACTIVE_GAP:
            stretches[-1][1] = minute
        else:
            stretches.append([minute, minute])
    return [(first, last + MINUTE) for first, last in stretches]


def report(conn: sqlite3.Connection, days: int, now: datetime, zone: ZoneInfo) -> dict[str, Any]:
    """Per day of the last `days`, today first: time worked, on steps and active without a timer; and
    the timers' time per task and per kind of step over those days."""
    today = now.astimezone(zone).date()
    starts = [datetime.combine(today - timedelta(days=back), time(), zone).astimezone(UTC) for back in range(days)]
    window = (starts[-1], now)
    minutes = [
        datetime.strptime(row["minute"], MINUTE_FORMAT).replace(tzinfo=UTC)
        for row in conn.execute(
            "SELECT minute FROM input_minutes WHERE minute >= ?", ((window[0] - ACTIVE_GAP).strftime(MINUTE_FORMAT),)
        )
    ]
    spans = [
        (
            row["step_id"], row["size"], row["item_id"], row["title"], row["need"],
            (_time(row["started_at"]), _time(row["ended_at"]) if row["ended_at"] else now),
        )
        for row in conn.execute(
            "SELECT t.started_at, t.ended_at, t.step_id, s.size, s.need, s.item_id, i.title FROM time_spans t"
            " JOIN steps s ON s.id = t.step_id JOIN items i ON i.id = s.item_id"
            " WHERE t.ended_at IS NULL OR t.ended_at > ?",
            (window[0].strftime("%Y-%m-%dT%H:%M:%SZ"),),
        )
    ]
    timed = _merged(_clipped([interval for *_, interval in spans], *window))
    writing = [
        (sent - timedelta(seconds=row["seconds"]), sent)
        for row in conn.execute(
            "SELECT created_at, seconds FROM feedback WHERE seconds > 0 AND created_at > ?",
            (window[0].strftime("%Y-%m-%dT%H:%M:%SZ"),),
        )
        for sent in [_time(row["created_at"])]
    ]
    on_feedback = _without(_merged(_clipped(writing, *window)), timed)
    untimed = _without(_without(_merged(_clipped(active_stretches(minutes), *window)), timed), on_feedback)

    day_rows = []
    for back, start in enumerate(starts):
        end = starts[back - 1] if back > 0 else now
        on_steps = _seconds(_clipped(timed, start, end))
        feedback = _seconds(_clipped(on_feedback, start, end))
        without_timer = _seconds(_clipped(untimed, start, end))
        day_rows.append({
            "date": (today - timedelta(days=back)).isoformat(),
            "worked": on_steps + feedback + without_timer,
            "on_steps": on_steps,
            "on_feedback": feedback,
            "active_without_timer": without_timer,
        })

    by_item: dict[int, dict[str, Any]] = {}
    by_kind: dict[str, int] = defaultdict(int)
    by_step: dict[str, dict[int, int]] = {size: defaultdict(int) for size in SIZES}
    for step_id, size, item_id, title, need, interval in spans:
        seconds = _seconds(_clipped([interval], *window))
        if seconds:
            by_item.setdefault(item_id, {"item_id": item_id, "title": title, "seconds": 0})["seconds"] += seconds
            by_kind[step_kind(need)] += seconds
            if size:
                by_step[size][step_id] += seconds

    return {
        "days": day_rows,
        "totals": {key: sum(row[key] for row in day_rows) for key in ("worked", "on_steps", "on_feedback", "active_without_timer")},  # ty: ignore[no-matching-overload]
        "items": sorted(by_item.values(), key=lambda item: -item["seconds"]),
        "kinds": [{"kind": kind, "seconds": seconds} for kind, seconds in sorted(by_kind.items(), key=lambda pair: -pair[1])],
        "sizes": [
            {"size": size, "steps": len(steps), "seconds": sum(steps.values()), "median": round(median(steps.values()))}
            for size, steps in by_step.items()
            if steps
        ],
        "progress": progress_check(conn, True),
        "progress_before": progress_check(conn, False),
        "progress_correction": progress_correction(conn),
        "input_since": db.kv_get(conn, "input_minutes_since"),
    }


def progress_check(conn: sqlite3.Connection, of_user_time: bool) -> dict[str, Any] | None:
    """How the progress estimates on finished items compare with the share of the user's timer time on each item
    that had passed when each was given: the items and estimates counted, the median gap in percentage points
    (positive: the estimate was ahead of the user's time) and the median distance either way. None before any
    finished item has an estimate. `of_user_time` picks the estimates given as the user's time , or the earlier ones, given as how far the item's work was.

    An item finished when it last moved to stage done (`done_at`); estimates and timer time after that are left
    out, and so is an item the user's timer never ran on."""
    finished = conn.execute(
        "SELECT i.id, i.done_at AS finished_at FROM items i WHERE i.stage = 'done'"
        " AND EXISTS (SELECT 1 FROM progress_marks WHERE item_id = i.id AND of_user_time = ?)",
        (int(of_user_time),),
    ).fetchall()
    gaps = []
    items = 0
    for item in finished:
        end = _time(item["finished_at"])
        timed = _merged(_clipped([
            (_time(row["started_at"]), _time(row["ended_at"]) if row["ended_at"] else end)
            for row in conn.execute(
                "SELECT t.started_at, t.ended_at FROM time_spans t JOIN steps s ON s.id = t.step_id WHERE s.item_id = ?",
                (item["id"],),
            )
        ], datetime.min.replace(tzinfo=UTC), end))
        total = _seconds(timed)
        if not total:
            continue
        marks = conn.execute(
            "SELECT at, progress FROM progress_marks WHERE item_id = ? AND of_user_time = ? AND at <= ?",
            (item["id"], int(of_user_time), item["finished_at"]),
        ).fetchall()
        if marks:
            items += 1
        for mark in marks:
            behind = _seconds(_clipped(timed, datetime.min.replace(tzinfo=UTC), _time(mark["at"])))
            gaps.append(mark["progress"] - behind / total * 100)
    if not gaps:
        return None
    return {
        "items": items,
        "estimates": len(gaps),
        "median_gap": round(median(gaps)),
        "median_off": round(median(abs(gap) for gap in gaps)),
    }


def progress_correction(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """The check of the estimates of the user's time, once it holds enough of them (CORRECTION_FROM) for the page
    to take their typical gap off each step's progress; None before."""
    check = progress_check(conn, True)
    if check is None or any(check[key] < least for key, least in CORRECTION_FROM.items()):
        return None
    return check
