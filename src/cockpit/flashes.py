"""Cards that left the user's page within a minute of showing, without the user's press (SPEC PERM-24).

The page's flashes.js notes each card that appeared while the page was in view and went within a minute, and
sends it here; this module adds what took it away, as Cockpit's own records tell, and keeps it. A step the user
closed, handed off, snoozed or ran while it showed went by the user's press, so it is left out here, as the page
leaves out the cards in Blocked and waiting pressed on it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from cockpit import db, store

log = logging.getLogger(__name__)

# A card gone sooner than this after it showed is a flash.
BRIEF_SECONDS = 60
# Rows kept: past this, the oldest go.
KEEP = 500
# Two tabs see the same card come and go: a report whose times are both this close to a kept one's is that one.
SAME_SECONDS = 15
# The page's clock and Cockpit's differ by this much at most once the page corrects for it (ui.clockOffset).
CLOCK_SLACK_SECONDS = 10

REQUEST_OUTCOMES = {
    "open": "still open in Cockpit: only the page stopped showing it",
    "withdrawn": "withdrawn: the session got past it without the user",
    "answered": "the dialog closed in its session",
    "expired": "expired",
    "allowed": "allowed, by a rule or a press the page did not see",
    "denied": "denied, by a press the page did not see",
    "dismissed": "dismissed, by a press the page did not see",
}
# The user's presses that take a step out of the queue. A snooze takes it out only when it sets the step aside: waking
# a step records `step.snooze` too, without `aside`.
USER_REMOVALS = frozenset({"step.done", "step.dismiss", "step.hand_off", "step.ran", "step.allowed"})


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _request_outcome(conn: sqlite3.Connection, ref: str) -> str:
    row = conn.execute("SELECT state FROM permission_requests WHERE id = ?", (int(ref),)).fetchone()
    return "no such request in Cockpit" if row is None else REQUEST_OUTCOMES[row["state"]]


def _removes(change: sqlite3.Row) -> bool:
    if change["kind"] == "step.snooze":
        return bool(json.loads(change["data"] or "{}").get("aside"))
    return change["kind"] in USER_REMOVALS


def _step_outcome(conn: sqlite3.Connection, ref: str, shown_at: str, gone_at: str, now: datetime) -> str | None:
    """What took the step out of the queue, or None when the user's own press did."""
    step = db.row(conn.execute("SELECT * FROM steps WHERE id = ?", (int(ref),)).fetchone())
    if step is None:
        return "no such step in Cockpit"
    slack = timedelta(seconds=CLOCK_SLACK_SECONDS)
    changes = conn.execute(
        "SELECT kind, actor, data FROM events WHERE step_id = ? AND at >= ? AND at <= ? ORDER BY id",
        (step["id"], _stamp(_time(shown_at) - slack), _stamp(_time(gone_at) + slack)),
    ).fetchall()
    if any(change["actor"] == "user" and _removes(change) for change in changes):
        return None
    if step["status"] != "open":
        where = f"closed ({step['status']})"
    elif step["withdrawn_at"]:
        where = "withdrawn"
    elif step["snoozed_until"] and _time(step["snoozed_until"]) > now:
        where = "snoozed"
    elif step["queued_at"] is None:
        where = "moved below the four places in Inbox"
    else:
        where = "still in the queue: only the page stopped showing it"
    last = changes[-1] if changes else None
    return f"{where}; last change: {last['kind']} by {last['actor']}" if last else where


def record(
    conn: sqlite3.Connection, kind: str, ref: str, label: str, page: str, shown_at: str, gone_at: str, now: datetime
) -> dict[str, Any] | None:
    """Keeps one card that went soon after showing, with what took it away.

    Returns the stored row, the row already stored when another tab saw the same card come and go, or None when
    the user's own press took the step away.
    """
    seconds = (_time(gone_at) - _time(shown_at)).total_seconds()
    if not 0 <= seconds < BRIEF_SECONDS + CLOCK_SLACK_SECONDS:
        raise store.Invalid(f"a flash goes within {BRIEF_SECONDS} s of showing; this one took {seconds:.0f} s")
    if kind == "request":
        outcome: str | None = _request_outcome(conn, ref)
    elif kind == "step":
        outcome = _step_outcome(conn, ref, shown_at, gone_at, now)
    elif kind == "session":
        outcome = "the session left its dialog"
    else:
        outcome = "the Codex question went"
    if outcome is None:
        return None
    with db.transaction(conn):
        near = timedelta(seconds=SAME_SECONDS)
        same = conn.execute(
            "SELECT * FROM flashes WHERE kind = ? AND ref = ? AND shown_at BETWEEN ? AND ? AND gone_at BETWEEN ? AND ?",
            (kind, ref, _stamp(_time(shown_at) - near), _stamp(_time(shown_at) + near),
             _stamp(_time(gone_at) - near), _stamp(_time(gone_at) + near)),
        ).fetchone()
        if same is not None:
            return db.row(same)
        cursor = conn.execute(
            "INSERT INTO flashes (kind, ref, label, page, shown_at, gone_at, outcome, recorded_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, ref, label, page, shown_at, gone_at, outcome, _stamp(now)),
        )
        conn.execute("DELETE FROM flashes WHERE id NOT IN (SELECT id FROM flashes ORDER BY id DESC LIMIT ?)", (KEEP,))
        stored = db.row(conn.execute("SELECT * FROM flashes WHERE id = ?", (cursor.lastrowid,)).fetchone())
    log.warning("%s %s left the page %.0f s after showing (%s): %s", kind, ref, seconds, outcome, label)
    return stored


def listing(conn: sqlite3.Connection, days: int, now: datetime) -> list[dict[str, Any]]:
    since = _stamp(now - timedelta(days=days))
    return db.rows(conn.execute("SELECT * FROM flashes WHERE gone_at >= ? ORDER BY id DESC", (since,)).fetchall())
