"""Moving a session to another Claude account, only when the user says so (SPEC SESSION-18).

A session runs on the account it started on, and a session the usage limit stopped waits for that account
(limits.py). Moving it throws away its prompt cache, so Cockpit never moves one by itself. The user moves a
session from its step or the session list; Cockpit stops its process on the old account, records the move,
and the session's next resume (the user's next message, or limits.sync once the new account has room) runs
on the new account. The listing then puts the session on the account it was moved to (sessions.merge).

A move asked for while the session is busy (a turn, background work, a scheduled prompt or a dialog in it)
waits: Cockpit records it (wait) and carries it out once the session's turn is over (waits_due), so the user
asks once. The session list says what it waits for (Session.busy_with).

Cockpit recommends a move as a step, only for a session on an urgent or high-priority item that the limit
stopped while another account has plenty of room (ROOM). The step closes once the session no longer waits on
the limit, or no account has that room any more. A session resumed while its account is still at the limit
stops again on its first call, so its step stays through that resume (_held).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cockpit import db, limits, store, usage
from cockpit.config import Account
from cockpit.sessions import Move, Session

log = logging.getLogger(__name__)

ACTOR = "cockpit"
NEED = store.MOVE_NEED
# Room another account must have for a recommendation: no window at its limit, and at most this share used.
ROOM = {"seven_day": 70, "five_hour": 80}
# A message of the user's sent this long before the stop counts as the one the stopped turn worked on (_body).
TURN = timedelta(minutes=30)
# The step's one-press answers. The page carries out each one's `action` itself rather than hand the step to a
# session: the stopped session cannot answer, and a message to it would resume it on the account at its limit.
ANSWERS = [
    {"text": "Yes, move it", "closes": False, "action": "move"},
    {"text": "No, let it wait", "closes": False, "action": "dismiss"},
]


def record(conn: sqlite3.Connection, session_id: str, account: str) -> None:
    """Record that the user moved the session to `account`; its last move counts. The time takes the form of a
    job file's `updatedAt`, so the listing compares the two as text (sessions.merge)."""
    conn.execute(
        "INSERT INTO session_moves (session_id, account, at) VALUES (?, ?, ?)"
        " ON CONFLICT (session_id) DO UPDATE SET account = excluded.account, at = excluded.at",
        (session_id, account, db.now_iso_ms()),
    )


def wait(conn: sqlite3.Connection, session_id: str, account: str, actor: str) -> None:
    """Record a move `actor` asked for while the session was busy; a later ask replaces it."""
    conn.execute(
        "INSERT INTO session_move_waits (session_id, account, actor, asked_at) VALUES (?, ?, ?, ?)"
        " ON CONFLICT (session_id) DO UPDATE SET account = excluded.account, actor = excluded.actor,"
        " asked_at = excluded.asked_at",
        # To the microsecond: asked_at tells this ask from one a second later (standing_wait, end_wait).
        (session_id, account, actor, datetime.now(UTC).isoformat(timespec="microseconds")),
    )


def standing_wait(conn: sqlite3.Connection, session_id: str, account: str, asked_at: str) -> dict[str, Any] | None:
    """The wait recorded at `asked_at` for moving the session to `account`, while it still stands."""
    return db.row(conn.execute(
        "SELECT * FROM session_move_waits WHERE session_id = ? AND account = ? AND asked_at = ?",
        (session_id, account, asked_at),
    ).fetchone())


def end_wait(conn: sqlite3.Connection, session_id: str, asked_at: str | None) -> None:
    """End the session's waiting move: the one recorded at `asked_at`, or with None whichever there is."""
    if asked_at is None:
        conn.execute("DELETE FROM session_move_waits WHERE session_id = ?", (session_id,))
    else:
        conn.execute("DELETE FROM session_move_waits WHERE session_id = ? AND asked_at = ?", (session_id, asked_at))


def waits_due(listed: list[Session], db_path: Path) -> list[dict[str, Any]]:
    """The waiting moves to carry out now: those whose session's turn is over, or that has stopped. One whose
    session is missing from `listed` is due too: the move's own lookup tells a gone session (it fails, and the
    wait ends) from one the listing has not caught up with."""
    conn = db.connect(db_path)
    try:
        waits = db.rows(conn.execute("SELECT * FROM session_move_waits ORDER BY asked_at").fetchall())
    finally:
        conn.close()
    sessions = {session.session_id: session for session in listed}
    return [
        row for row in waits
        if (session := sessions.get(row["session_id"])) is None or not session.alive or session.turn_over
    ]


def moved(db_path: Path) -> dict[str, Move]:
    """Each moved session's last move, for the listing. `cockpit attach` reads it too, from a database the server
    may not have migrated yet; one without the table has no moves."""
    conn = db.connect(db_path)
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'session_moves'").fetchone() is None:
            return {}
        return {
            row["session_id"]: Move(account=row["account"], at=row["at"])
            for row in conn.execute("SELECT session_id, account, at FROM session_moves").fetchall()
        }
    finally:
        conn.close()


def has_room(record: dict[str, Any] | None, now: datetime) -> bool:
    """The account's latest usage record shows plenty of room (ROOM), and its 5-hour window is not way over, as new
    work also needs (usage.room); an account with no record has a fresh week."""
    if record is None:
        return True
    for key, most in ROOM.items():
        window = record["rate_limits"].get(key)
        if window and window["resets_at"] > now.timestamp() and float(window["used_percentage"]) > most:
            return False
    return not usage.room(record, now.timestamp(), [], None, 0).near


def sync(
    conn: sqlite3.Connection,
    sessions: list[Session],
    in_use: list[Account],
    readings: dict[str, dict[str, Any] | None],
    now: datetime,
) -> None:
    """Close the move steps that no longer hold, then put one for each limit-stopped session on an urgent or
    high-priority item while another account has room."""
    wanted: dict[str, tuple[Session, str, limits.Stop, dict[str, Any]]] = {}
    for session, stop in limits.waiting(conn, sessions, now):
        others = [account.name for account in in_use if account.name != session.account]
        roomy = [name for name in others if has_room(readings.get(name), now)]
        item = _urgent_item(conn, session.session_id)
        if roomy and item is not None:
            wanted[session.session_id] = (session, roomy[0], stop, item)
    listed = {session.session_id: session for session in sessions}
    open_steps: dict[tuple[int, str, str], dict[str, Any]] = {}
    for step in _open_moves(conn):
        found = NEED.match(step["need"])
        assert found is not None  # _open_moves keeps only these
        entry = wanted.get(found["session_id"])
        key = (step["item_id"], found["session_id"], found["account"])
        if key in open_steps:
            # A second step for the same move gives way to the first. One the user snoozed stays theirs.
            _close(conn, step)
        elif entry is None and _held(conn, listed.get(found["session_id"]), found["account"], step["item_id"], in_use, readings, now):
            open_steps[key] = step
        # A step on another item (one archived since, or no longer the most urgent) gives way to the wanted one.
        elif entry is None or entry[1] != found["account"] or entry[3]["id"] != step["item_id"]:
            _close(conn, step)
        else:
            open_steps[key] = step
    # A step put before the answers existed offers them too, also one kept while its session ran again (_held).
    for step in open_steps.values():
        if step["answers"] != ANSWERS and store.running_step(conn) != step["id"]:
            store.upsert_step(conn, {"item_id": step["item_id"], "need": step["need"], "answers": ANSWERS}, ACTOR)
    for session, account, stop, item in wanted.values():
        # An open step stays, also once the session stops on the limit again after a resume: putting it again would
        # bump it and undo the user's edits, and a new stop's need would put a second step beside it. Only its
        # reason follows the latest stop's reset time.
        step = open_steps.get((item["id"], session.session_id, account))
        if step is not None:
            text = {"why": _why(session, account, stop), "body": _body(conn, session, account, stop, readings, now)}
            changed = {key: value for key, value in text.items() if step[key] != value}
            # While the user's timer runs on it, the update would wait and pile up one per poll; it lands after.
            if changed and store.running_step(conn) != step["id"]:
                store.upsert_step(conn, {"item_id": item["id"], "need": step["need"], **changed}, ACTOR)
            continue
        try:
            _put(conn, session, account, stop, item, readings, now)
        except store.Invalid as exc:
            log.error("could not put the move step for session %s: %s", session.session_id, exc)


def close_steps(conn: sqlite3.Connection, session_id: str) -> None:
    """The user moved the session: its move steps are done."""
    for step in _open_moves(conn):
        if NEED.match(step["need"])["session_id"] == session_id:  # ty: ignore[not-subscriptable]
            store.step_action(conn, step["id"], "done", {}, ACTOR)


def _urgent_item(conn: sqlite3.Connection, session_id: str) -> dict[str, Any] | None:
    """The unarchived urgent or high-priority item the session works for, the most urgent first: one that lists it,
    or one whose open steps count it (store.sessions_of_steps), as the overview counts it."""
    by_steps = store.sessions_of_steps(store.open_steps_with_handoff(conn))
    for item in db.rows(conn.execute(
        "SELECT i.id, i.title, i.priority, i.sessions FROM items i WHERE i.archived = 0"
        " AND i.priority IN ('urgent', 'high') ORDER BY i.priority = 'urgent' DESC, i.id"
    ).fetchall()):
        if session_id in item.pop("sessions") + by_steps.get(item["id"], []):
            return item
    return None


def _held(
    conn: sqlite3.Connection,
    session: Session | None,
    account: str,
    item_id: int,
    in_use: list[Account],
    readings: dict[str, dict[str, Any] | None],
    now: datetime,
) -> bool:
    """The step's session no longer waits on the limit, as a message or Cockpit resumed it, but its last turn still
    ended on a limit that has not reset, so it stops again on its first call: the step stays while the move it asks
    still holds. A turn that ends without the limit ends the hold, as does the limit's reset. A session
    that stopped or ended does not hold it, as one closed on purpose is done with its limit."""
    if session is None or session.state not in ("working", "blocked") or not session.account:
        return False
    if account == session.account or account not in [other.name for other in in_use]:
        return False
    stop = limits.limit_stop(session.session_id)
    if stop is None or stop.resets is None or stop.resets <= now:
        return False
    item = _urgent_item(conn, session.session_id)
    return item is not None and item["id"] == item_id and has_room(readings.get(account), now)


def _open_moves(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = db.rows(conn.execute(
        f"SELECT s.* FROM steps s WHERE {store.OPEN} AND s.handoff_message_id IS NULL AND s.need LIKE 'move:%'"
    ).fetchall())
    return [step for step in rows if NEED.match(step["need"])]


def _close(conn: sqlite3.Connection, step: dict[str, Any]) -> None:
    if not step["snoozed_until"] and store.running_step(conn) != step["id"]:
        store.step_action(conn, step["id"], "withdraw", {}, ACTOR)


def _need(session: Session, account: str, stop: limits.Stop) -> str:
    """The stop time keeps a step the user dismissed closed for that stop only; while a step is open, sync keeps
    it for the session's later stops too."""
    return f"move:{session.session_id}:{account}:{stop.at:%Y%m%dT%H%M%S}"


def _why(session: Session, account: str, stop: limits.Stop) -> str:
    until = f" until {stop.resets:%a %H:%M} UTC" if stop.resets else ""
    return f"The {session.account} account's usage limit stopped it{until}; the {account} account has room."[
        : store.TEXT_LIMITS["why"]
    ]


def _body(
    conn: sqlite3.Connection,
    session: Session,
    account: str,
    stop: limits.Stop,
    readings: dict[str, dict[str, Any] | None],
    now: datetime,
) -> str:
    """What waits on the stopped session, when it goes on by itself, and what Move costs, in words."""
    until = f" until {stop.resets:%a %H:%M} UTC" if stop.resets else ""
    first = f"This item's session is stopped: the {session.account} account is at its usage limit{until}."
    message = conn.execute(
        "SELECT created_at, step_ask FROM messages WHERE session_id = ? AND author = 'user' AND state = 'sent'"
        " AND created_at >= ?"
        " ORDER BY created_at DESC LIMIT 1",
        (session.session_id, (stop.at - TURN).strftime("%Y-%m-%dT%H:%M:%SZ")),
    ).fetchone()
    if message is not None:
        on = f' on "{store._clip(message["step_ask"], 70)}"' if message["step_ask"] else ""
        first += f" It stopped while working on your message of {message['created_at'][11:16]} UTC{on}."
    week = usage.room(readings.get(account), now.timestamp(), [], None, 0).week_used
    return "\n\n".join([
        first,
        f"**Yes, move it** continues it now on the {account} account, whose week is {week:.0f} % used; its first turn"
        " there costs more, as the cache stays behind. **No, let it wait** closes this step, and the session goes on"
        " by itself once its account has room.",
    ])


def _put(
    conn: sqlite3.Connection,
    session: Session,
    account: str,
    stop: limits.Stop,
    item: dict[str, Any] | None,
    readings: dict[str, dict[str, Any] | None],
    now: datetime,
) -> None:
    assert item is not None  # sync wants only sessions with one
    fields = {
        "item_id": item["id"],
        "need": _need(session, account, stop),
        "ask": f"Move this item's session to the {account} Claude account, so it continues now?"[
            : store.TEXT_LIMITS["ask"]
        ],
        "why": _why(session, account, stop),
        "body": _body(conn, session, account, stop, readings, now),
        "answers": ANSWERS,
    }
    result = store.upsert_step(conn, fields, ACTOR)
    if result["skipped"] is None:
        log.info("put step %s recommending a move of session %s to %s", result["step"]["id"], session.session_id, account)
