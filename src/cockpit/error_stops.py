"""Sessions whose turn ended on an API error: resend once the cause is gone, and tell the user of what stays broken
(SPEC SWEEP-30).

Claude Code ends a turn the API refused with an error entry of its own (sessions.error_stop), such as "API Error:
400 Claude Code 2.1.280 does not support this model" when the client version is too old for it. The usage limit and
a sign-out have their own recovery (limits.py, logins.py) and are left to it. The session then waits for someone
to type again; nothing in the sweep wakes it.

Each poll, Cockpit sends such a session one message to continue (sync) once the cause looks gone: another session
on the same account and model had a call go through after the stop, or an hour passed, and the message itself is
the test. Each stop gets one message, and only within LOOKBACK of it: an older stop was left on purpose or by
someone who knows. The message works when the session answers after it; it fails when the session ends on an API
error again, or the message cannot be delivered. Failures go on one high-priority item for the user, which every
later failure joins until the user closes it, so an incident across many sessions makes one item. The fixed ones
go to the next morning briefing as context it may mention (fixed).

These messages go out with the rest of the poll's, whether sweeps are paused or memory is low.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from cockpit import db, limits, projects, rank, store
from cockpit.sessions import ErrorStop, Session, error_stop, transcript_usage

log = logging.getLogger(__name__)

ACTOR = "cockpit"
# A stop older than this that no one answered was left on purpose, or by someone who knows of it.
LOOKBACK = timedelta(hours=12)
# With no other session on the account and model to show the cause is gone, the resend itself tests it after this.
FALLBACK_WAIT = timedelta(hours=1)
ERROR_CHARS = 300
RESEND = (
    'Your last turn ended on an API error at {at:%H:%M} UTC: "{error}". {why}. Continue the task you were on from'
    " where it stopped; if that turn was answering a message, answer it now."
)
ITEM_PREFIX = "note:error-stops-"
ITEM_TITLE = "Sessions stuck on API errors"
NEED = "look"
SESSIONS_CHARS = store.DETAIL_CHARS


@dataclass(frozen=True)
class Waiting:
    session: Session
    stop: ErrorStop
    why: str


def sync(conn: sqlite3.Connection, sessions: list[Session], now: datetime, checkouts_dir: Path) -> list[int]:
    """Judge the messages sent before, then queue one to each session an API error stopped whose cause looks gone.
    `sessions` are the listed Claude sessions. Returns the ids of the queued messages, for the caller to deliver."""
    judge(conn, now)
    # A failure a report could not put on an item stays unreported, and the next poll tries it again.
    unreported = db.rows(conn.execute(
        "SELECT * FROM error_stops WHERE outcome = 'failed' AND item_id IS NULL ORDER BY id"
    ).fetchall())
    if unreported:
        report(conn, unreported, now)
    message_ids = []
    for found in waiting(conn, sessions, now):
        stopped = _iso(found.stop.at)
        text = RESEND.format(at=found.stop.at.astimezone(now.tzinfo), error=_short(found.stop.text), why=found.why)
        with db.transaction(conn):
            # Judged again in the message's transaction: someone's message since takes its place.
            if not _unanswered(conn, found.session.session_id, found.stop.at):
                continue
            message_id = store.create_message(conn, ACTOR, text, {"session_id": found.session.session_id}, checkouts_dir)["id"]
            conn.execute(
                "INSERT INTO error_stops (session_id, name, account, model, stopped_at, kind, error, message_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (found.session.session_id, found.session.name, found.session.account,
                 transcript_usage(found.session.session_id).model, stopped, found.stop.kind, found.stop.text,
                 message_id),
            )
        message_ids.append(message_id)
        log.info("session %s stopped on an API error at %s (%s); %s; queued message %s",
                 found.session.session_id, stopped, _short(found.stop.text), found.why, message_id)
    return message_ids


def waiting(conn: sqlite3.Connection, sessions: list[Session], now: datetime) -> list[Waiting]:
    """Each session whose last turn ended on an API error in the last LOOKBACK that no message reached since, and
    why its cause looks gone; one whose cause may persist is left out. A working or archived session, one an agent
    closed, a session whose last message's outcome is not known yet, and a stop that already had its message or
    follows a failed one, are left alone. Another session's answer counts only after it last moved account."""
    usages = {session.session_id: transcript_usage(session.session_id) for session in sessions}
    moved = {session.session_id: limits.moved_at(conn, session.session_id) for session in sessions}
    found = []
    for session in sessions:
        if session.state == "working" or session.working_now:
            continue
        if conn.execute("SELECT 1 FROM error_stops WHERE session_id = ? AND outcome = ''",
                        (session.session_id,)).fetchone():
            continue
        stop = error_stop(session.session_id)
        if stop is None or stop.at < now - LOOKBACK:
            continue
        if conn.execute("SELECT 1 FROM error_stops WHERE session_id = ? AND stopped_at = ?",
                        (session.session_id, _iso(stop.at))).fetchone():
            continue
        if not _unanswered(conn, session.session_id, stop.at):
            continue
        model = usages[session.session_id].model
        through = [
            answer[0] for other in sessions if other.session_id != session.session_id
            and other.account == session.account and model and usages[other.session_id].model == model
            and (answer := usages[other.session_id].answer) is not None and answer[1] and answer[0] > stop.at
            and ((other_moved := moved[other.session_id]) is None or answer[0] > other_moved)
        ]
        if through:
            why = f"A call on the same account and model went through at {max(through).astimezone(now.tzinfo):%H:%M} UTC"
        elif now - stop.at >= FALLBACK_WAIT:
            why = "An hour has passed since, and no other session showed whether the error is gone; this message tests it"
        else:
            continue
        found.append(Waiting(session, stop, why))
    return found


def judge(conn: sqlite3.Connection, now: datetime) -> None:
    """Settle each resend whose outcome shows: fixed once the session answered after it, failed once it ended on an
    API error again (that stop is kept as its repeat, which gets no message) or the message could not be delivered.
    A usage-limit or sign-out stop after it is left to its own recovery. What the session did counts from the stop:
    nothing reached it between the stop and the message (_unanswered), and delivery stamps its send time only after
    the session may already have answered or failed. A resend whose delivery Cockpit could not confirm waits
    store.CONFIRM_WINDOW for its session's transcript to show it."""
    confirming = {message["id"] for message in store.confirming(conn, now)}
    for row in db.rows(conn.execute(
        "SELECT e.*, m.state AS message_state, m.detail AS message_detail FROM error_stops e"
        " JOIN messages m ON m.id = e.message_id WHERE e.outcome = '' AND m.state IN ('sent', 'failed')"
    ).fetchall()):
        again = None
        if row["message_id"] in confirming:
            continue  # its session's transcript may still show it arrived (store.confirm_uncertain)
        if row["message_state"] == "failed":
            outcome, tried = "failed", f"Cockpit's message could not be delivered: {_short(row['message_detail'])}"
        else:
            # stopped_at keeps whole seconds; the stop itself falls within its second.
            stopped = rank.parse_time(row["stopped_at"])
            assert stopped is not None, f"error stop {row['id']} has no time"
            sent = stopped + timedelta(seconds=1)
            again = error_stop(row["session_id"])
            answer = transcript_usage(row["session_id"]).answer
            if again is not None and again.at > sent and (again.answered is None or again.answered <= sent):
                outcome, tried = "failed", f'Cockpit resent its last message; it ended on "{_short(again.text)}" again'
            elif again is not None and again.at > sent or answer is not None and answer[0] > sent and answer[1]:
                outcome, tried = "fixed", ""
            elif answer is not None and answer[0] > sent:
                outcome, tried = "other", ""
            else:
                continue
        with db.transaction(conn):
            conn.execute("UPDATE error_stops SET outcome = ?, tried = ?, settled_at = ? WHERE id = ?",
                         (outcome, tried, _iso(now), row["id"]))
            if outcome == "failed" and again is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO error_stops (session_id, name, account, model, stopped_at, kind, error,"
                    " outcome, settled_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'repeat', ?)",
                    (row["session_id"], row["name"], row["account"], row["model"], _iso(again.at), again.kind,
                     again.text, _iso(now)),
                )
        log.info("the resend to session %s after its API error at %s: %s", row["session_id"], row["stopped_at"], outcome)


def report(conn: sqlite3.Connection, failed: list[dict[str, Any]], now: datetime) -> None:
    """Put the failed resends on the open incident item, or on a new one when the user closed the last: one
    high-priority item with one step that names each stopped session, its task, the error and what was tried."""
    with db.transaction(conn):
        item_id = open_incident(conn)
        if item_id is None:
            project = projects.resolve(conn, store.COCKPIT_PROJECT)
            item_id = store.upsert_item(conn, {
                # Each incident's key is its first failure's, so a new one never lands on a closed one.
                "key": f"{ITEM_PREFIX}{failed[0]['id']}", "title": ITEM_TITLE, "priority": "high",
                "summary": "Sessions stayed stopped on an API error after Cockpit resent their last message",
            } | ({"project": project} if project else {}), ACTOR)["id"]
        conn.executemany("UPDATE error_stops SET item_id = ? WHERE id = ?", [(item_id, row["id"]) for row in failed])
        rows = db.rows(conn.execute(
            "SELECT * FROM error_stops WHERE item_id = ? ORDER BY stopped_at", (item_id,)
        ).fetchall())
        store.upsert_step(conn, {"item_id": item_id, "need": NEED} | step_text(conn, rows), ACTOR)
    log.error("%d sessions stayed stopped on an API error after Cockpit resent their message; on item %s",
              len(failed), item_id)


def open_incident(conn: sqlite3.Connection) -> int | None:
    """The item the last failures went on, while the user has not closed it or its step."""
    row = conn.execute(
        "SELECT i.id FROM error_stops e JOIN items i ON i.id = e.item_id JOIN steps s ON s.item_id = i.id"
        f" WHERE s.need = ? AND {store.OPEN} AND i.archived = 0 AND i.stage IS NOT 'done'"
        " ORDER BY e.id DESC LIMIT 1",
        (NEED,),
    ).fetchone()
    return row["id"] if row is not None else None


def step_text(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The incident step for its failed resends `rows`: the errors in the body, each session in a detail."""
    errors: dict[str, int] = {}
    for row in rows:
        errors[_short(row["error"])] = errors.get(_short(row["error"]), 0) + 1
    count = f"{len(rows)} session{'s' if len(rows) != 1 else ''}"
    # The error texts go in a detail: a body refuses some of what they hold, such as a hexadecimal request id.
    body = (
        f"{count} stopped on an API error, and Cockpit's resend of the last message did not get "
        f"{'them' if len(rows) != 1 else 'it'} going: the error came back or the message could not be delivered."
        " Cockpit sends each stop one message, so these wait for you. The errors and the sessions are below."
    )
    shown = ""
    for error, times in sorted(errors.items(), key=lambda pair: -pair[1]):
        line = f'- "{error}"' + (f" ({times} sessions)" if times > 1 else "")
        if len(shown) + len(line) > SESSIONS_CHARS - 30:
            shown += "\n- and others"
            break
        shown += ("\n" if shown else "") + line
    lines = []
    for row in rows:
        task = conn.execute(
            "SELECT i.id, i.title FROM items i, json_each(i.sessions) s WHERE s.value = ? AND i.archived = 0"
            " ORDER BY i.id DESC LIMIT 1", (row["session_id"],)
        ).fetchone()
        lines.append(
            f"- {row['name'] or 'unnamed'} ({row['session_id']})"
            + (f", on task {task['id']} \"{task['title']}\"" if task else ", on no task")
            + f": stopped at {row['stopped_at'][11:16]} UTC; {row['tried']}"
        )
    sessions = ""
    for at, line in enumerate(lines):
        if len(sessions) + len(line) > SESSIONS_CHARS - 30:
            sessions += f"\n- and {len(lines) - at} more"
            break
        sessions += ("\n" if sessions else "") + line
    return {
        "ask": f"{count} still stopped on API errors; look?",
        "why": "Cockpit resent their last message once and they did not recover; each waits until someone answers it.",
        "body": body,
        "details": [{"title": "The errors", "body": shown}, {"title": "The sessions", "body": sessions}],
        "size": "medium",
        "progress": 0,
    }


def fixed(conn: sqlite3.Connection, since: datetime, until: datetime) -> list[str]:
    """One line for each session a resend got going again, of those settled between `since` and `until`, for the
    morning briefing to mention as it sees fit."""
    return [
        f"{row['name'] or row['session_id']} ({row['session_id'][:8]}) stopped at {row['stopped_at'][11:16]} UTC on"
        f' "{_short(row["error"])}"; Cockpit resent its last message and it went on'
        for row in conn.execute(
            "SELECT * FROM error_stops WHERE outcome = 'fixed' AND settled_at >= ? AND settled_at < ? ORDER BY stopped_at",
            (_iso(since), _iso(until)),
        ).fetchall()
    ]


def _unanswered(conn: sqlite3.Connection, session_id: str, stopped_at: datetime) -> bool:
    """No message reached the session since its stop at `stopped_at`, it is not archived, and no agent closed it."""
    if conn.execute("SELECT 1 FROM messages WHERE session_id = ? AND created_at > ?",
                    (session_id, _iso(stopped_at))).fetchone():
        return False
    if conn.execute("SELECT 1 FROM session_archives WHERE session_id = ? AND cancelled_at IS NULL"
                    " AND revived_at IS NULL", (session_id,)).fetchone():
        return False
    return not limits.ended_on_purpose(conn, session_id, stopped_at)


def _short(text: str) -> str:
    words = " ".join(text.split())
    return words if len(words) <= ERROR_CHARS else words[:ERROR_CHARS - 1] + "…"


def _iso(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")
