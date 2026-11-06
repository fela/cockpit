"""What keeps happening on Cockpit, read from the events log.

Every write that says something about how well Cockpit serves the user appends an
event: steps created, closed, withdrawn or reopened, the user's pins and snoozes,
their feedback, messages sent or failed, sweeps. `summary` turns
the last days into counts per kind of step and a list of repeats, so the user and
the sweep can see a pattern after a few occurrences and change the skill.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any

from cockpit import db, flashes, page_errors, rank
from cockpit.config import user_name
from cockpit.failures import failure_reason

# A count at or above this within the window is listed as a repeat.
REPEAT_AT = 3

# Step event kinds as the summary labels them, in display order.
STEP_OUTCOMES = (
    "created", "done", "closed by agents", "dismissed", "withdrawn", "reopened", "snoozed", "handed off",
)
_ACTION_OUTCOME = {
    "dismiss": "dismissed",
    "withdraw": "withdrawn",
    "reopen": "reopened",
    "snooze": "snoozed",
    "hand_off": "handed off",
    "take": "taken to the top",
}


def record(
    conn: sqlite3.Connection,
    kind: str,
    actor: str,
    item_id: int | None,
    step_id: int | None,
    need: str | None,
    data: dict[str, Any],
) -> None:
    """Log an event. The user's event on an item marks the item seen (SPEC RANK-14); `aside` in its data says
    the event set the item aside (a snooze, a lowered priority)."""
    at = db.now_iso()
    conn.execute(
        "INSERT INTO events (at, actor, kind, item_id, step_id, need, data) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (at, actor, kind, item_id, step_id, need, json.dumps(data)),
    )
    if actor == "user" and item_id is not None:
        mark_seen(conn, item_id, at, bool(data.get("aside")))


def mark_seen(conn: sqlite3.Connection, item_id: int, at: str, aside: bool) -> None:
    """The user saw the item at `at`: they acted on it, or played one of its steps (SPEC RANK-14)."""
    conn.execute("UPDATE items SET seen_at = ?, set_aside = ? WHERE id = ?", (at, int(aside), item_id))


def record_step(conn: sqlite3.Connection, action: str, actor: str, step: dict[str, Any]) -> None:
    """A step event, carrying what the summary needs after the step itself is gone. The user's snooze sets
    the item aside; waking the step early does not."""
    source = conn.execute("SELECT key FROM items WHERE id = ?", (step["item_id"],)).fetchone()
    created = rank.parse_time(step["created_at"])
    minutes_open = round((datetime.now(UTC) - created).total_seconds() / 60) if created else None
    data = {
        "source": source["key"].split(":", 1)[0] if source else None,
        "score": step["score"],
        "minutes_open": minutes_open,
    }
    if action == "snooze" and step["snoozed_until"] is not None:
        data["aside"] = True
    record(conn, f"step.{action}", actor, step["item_id"], step["id"], step["need"], data)


def need_kind(need: str) -> str:
    """`decide:no-row-fallback` → `decide`, `review@abc` → `review`, `merge` → `merge`."""
    return re.split(r"[:@]", need, maxsplit=1)[0]


def _outcome(event: dict[str, Any]) -> str | None:
    action = event["kind"].removeprefix("step.")
    if action in ("updated", "checked", "yield"):
        return None
    # "done" and "dismissed" are the user's own closes; an agent's or Cockpit's close of either kind is one
    # count, so a sweeper reading "dismissed by the user" never sees an agent's clean-up as the user's verdict.
    if action in ("done", "dismiss") and event["actor"] != "user":
        return "closed by agents"
    if action == "done":
        return "done"
    return _ACTION_OUTCOME.get(action, action)


def _sweeps(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    started = {event["data"]["sweep_id"]: event for event in events if event["kind"] == "sweep.started"}
    finished = {event["data"]["sweep_id"]: event for event in events if event["kind"] == "sweep.finished"}
    sweeps = []
    for sweep_id, start in started.items():
        end = finished.get(sweep_id)
        start_at = rank.parse_time(start["at"])
        end_at = rank.parse_time(end["at"]) if end else None
        puts = [
            rank.parse_time(event["at"])
            for event in events
            if event["kind"] in ("step.created", "step.updated")
            and event["actor"] == start["actor"]
            and event["at"] >= start["at"]
            and (end is None or event["at"] <= end["at"])
        ]
        third = puts[2] if len(puts) >= 3 else None
        sweeps.append(
            {
                "id": sweep_id,
                "by": start["actor"],
                "started_at": start["at"],
                "minutes": round((end_at - start_at).total_seconds() / 60) if end_at else None,  # ty: ignore[unsupported-operator]
                "steps_put": len(puts),
                "minutes_to_first_step": round((puts[0] - start_at).total_seconds() / 60) if puts else None,  # ty: ignore[unsupported-operator]
                "minutes_to_third_step": round((third - start_at).total_seconds() / 60) if third else None,  # ty: ignore[unsupported-operator]
                "error": end["data"]["error"] if end else "not finished",
                "summary": end["data"].get("summary", "") if end else "",
            }
        )
    return sweeps[::-1]


# The window of sweep times on the page (SPEC SWEEP-16).
SWEEP_TIME_DAYS = 7


# A full sweep reads every source (GitHub, Linear, Slack, Notion); Cockpit asks for one when none finished in this many
# hours (SPEC SCOPE-3), since light sweeps leave most of them out.
FULL_SWEEP_HOURS = 3


def sweep_kind(summary: str) -> str:
    """A finished sweep's kind, from its summary's first word: "full", "fill", "reorder" or "light"."""
    if summary.startswith("Full"):
        return "full"
    if summary.startswith("Fill"):
        return "fill"
    return "reorder" if summary.startswith("Reorder") else "light"


def full_sweep_due(conn: sqlite3.Connection, now: datetime) -> bool:
    """No full sweep finished without an error in the last FULL_SWEEP_HOURS hours."""
    since = (now - timedelta(hours=FULL_SWEEP_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for (data,) in conn.execute("SELECT data FROM events WHERE at >= ? AND kind = 'sweep.finished'", (since,)).fetchall():
        finished = json.loads(data)
        if not finished["error"] and sweep_kind(finished.get("summary", "")) == "full":
            return False
    return True


def sweep_times(conn: sqlite3.Connection, days: int, now: datetime) -> dict[str, dict[str, int] | None]:
    """How long sweeps took over the last `days` by kind, from `cockpit sweep start` to its finish, so slow ones show
    (SPEC SWEEP-16). The kind is the summary's first word ("Full:", "Fill:", else light). A sweep that failed or has
    not finished is left out."""
    started: dict[str, datetime] = {}
    seconds: dict[str, list[int]] = {"full": [], "light": [], "fill": [], "reorder": []}
    since = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for event in conn.execute(
        # By time, so the events_at index serves it rather than a scan of the whole log.
        "SELECT kind, at, data FROM events WHERE at >= ? AND kind IN ('sweep.started', 'sweep.finished') ORDER BY at, id",
        (since,),
    ).fetchall():
        data = json.loads(event["data"])
        if event["kind"] == "sweep.started":
            started[data["sweep_id"]] = rank.parse_time(event["at"])  # ty: ignore[invalid-assignment]
        elif data["sweep_id"] in started and not data["error"]:
            kind = sweep_kind(data.get("summary", ""))
            seconds[kind].append(round((rank.parse_time(event["at"]) - started[data["sweep_id"]]).total_seconds()))  # ty: ignore[unsupported-operator]
    return {
        kind: {"count": len(times), "median_seconds": round(median(times)), "slowest_seconds": max(times)} if times else None
        for kind, times in seconds.items()
    }


def summary(conn: sqlite3.Connection, days: int, now: datetime) -> dict[str, Any]:
    since = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    events = db.rows(conn.execute("SELECT * FROM events WHERE at >= ? ORDER BY id", (since,)).fetchall())

    by_kind: dict[str, Counter[str]] = defaultdict(Counter)
    by_source: dict[str, Counter[str]] = defaultdict(Counter)
    minutes_to_done = []
    for event in events:
        if not event["kind"].startswith("step."):
            continue
        outcome = _outcome(event)
        if outcome is None:
            continue
        by_kind[need_kind(event["need"])][outcome] += 1
        by_source[event["data"]["source"] or "?"][outcome] += 1
        if outcome == "done" and event["data"]["minutes_open"] is not None:
            minutes_to_done.append(event["data"]["minutes_open"])

    messages = [event for event in events if event["kind"].startswith("message.")]
    failures = [event for event in messages if event["kind"] == "message.failed"]
    feedback = db.rows(
        conn.execute(
            "SELECT feedback.created_at, feedback.text, steps.ask FROM feedback"
            " LEFT JOIN steps ON steps.id = feedback.step_id WHERE feedback.created_at >= ?"
            " ORDER BY feedback.id DESC",
            (since,),
        ).fetchall()
    )

    repeats = []
    for kind, counts in sorted(by_kind.items()):
        for outcome, words in (
            ("dismissed", f"dismissed by {user_name()}"),
            ("withdrawn", f"withdrawn before {user_name()} got to them"),
            ("reopened", f"reopened by {user_name()} after an agent closed them"),
        ):
            if counts[outcome] >= REPEAT_AT:
                repeats.append(f"{counts[outcome]} {kind} steps {words}")
    failed_by_reason = Counter(failure_reason(event["data"]["detail"]) for event in failures)
    for reason, count in failed_by_reason.most_common():
        if count >= REPEAT_AT:
            repeats.append(f"{count} messages failed: {reason}")
    # Every error a page met is a bug, so one open error is worth a line, and it stays one line however many.
    open_errors = page_errors.open_count(conn)
    if open_errors:
        repeats.append(f"{open_errors} errors on Cockpit's pages that no session has taken: `cockpit errors list`")
    # Cards that went before the user could read them (PERM-24): each one is worth a look.
    flashed = flashes.listing(conn, days, now)
    if flashed:
        repeats.append(
            f"{len(flashed)} cards left {user_name()}'s page within a minute of showing: `cockpit flashes --days {days}`"
        )
    if len(feedback) >= REPEAT_AT:
        repeats.append(f"{len(feedback)} pieces of feedback from {user_name()}: read them together for a common thread")

    def table(counts: dict[str, Counter[str]]) -> list[dict[str, Any]]:
        return [
            {"name": name} | {outcome: counter[outcome] for outcome in STEP_OUTCOMES}
            for name, counter in sorted(counts.items(), key=lambda pair: -pair[1]["created"])
        ]

    return {
        "days": days,
        "since": since,
        "repeats": repeats,
        "by_kind": table(by_kind),
        "by_source": table(by_source),
        "median_minutes_to_done": round(median(minutes_to_done)) if minutes_to_done else None,
        "messages": {
            "sent": sum(event["kind"] == "message.sent" for event in messages),
            "failed": len(failures),
            "recent_failures": [{"at": event["at"], "detail": event["data"]["detail"]} for event in failures[-5:]],
        },
        "feedback": feedback[:10],
        "sweeps": _sweeps(events)[:10],
        "sweep_times": sweep_times(conn, days, now),
    }
