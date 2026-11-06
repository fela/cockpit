"""Claude's usage limit: a message to each session it stopped, to continue once usage is back (SPEC SESSION-8).

When the account's usage runs out, Claude Code ends each running session's turn with an error
("You've hit your weekly limit · resets Oct 10, 12pm (UTC)"); the session is `blocked`, and `stopped` once
Cockpit closes it for being idle (delivery.py). A session waits on the limit until a message reaches it after its
stop, from Cockpit or anyone else, for at most a week, the longest a limit lasts. The user has nothing to do
here, so Cockpit puts no step for it.

Each session waits on the account it runs on (SPEC SESSION-17): a reading of the other account
says nothing about it. Usage is back for an account when Claude's newest answer to any of its sessions after the
first stop that waits on it went through (sessions.last_answer), or every window its latest record in
`~/.claude/rate-limits.json` (usage.py, accounts.readings) reported used up has reset since. A record's time is no
sign by itself: the status line writes the record again, at the time it runs, with the last numbers Claude Code
handed it, also after the limit stopped the turn . A session is also free once the reset time its stop names has passed ("resets 10:00am
(UTC)"), which frees it when no session on its account runs. Cockpit then sends each such session a message to
continue, and resumes it on its account.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from cockpit import db, rank, store
from cockpit.sessions import Session, last_answer, limit_stop

log = logging.getLogger(__name__)

ACTOR = "cockpit"
# A weekly limit lasts at most a week: a session that stopped on one longer ago and that no one has continued
# was left on purpose.
LONGEST_WAIT = timedelta(days=7)
CONTINUE = (
    "Claude's usage works again: {why}. Your last turn ended on the usage limit."
    " Continue the task you were on from where it stopped."
)


def sync(
    conn: sqlite3.Connection,
    sessions: list[Session],
    readings: dict[str, dict[str, Any] | None],
    now: datetime,
    checkouts_dir: Path,
) -> list[int]:
    """Once usage is back on an account, queue a message to continue to each session the limit stopped there.
    `readings` holds each account's latest usage record by account name (accounts.readings).

    Returns the ids of the queued messages, for the caller to deliver.
    """
    waiting_now = waiting(conn, sessions, now)
    message_ids = []
    for account in sorted({session.account for session, _ in waiting_now}):
        on_account = [(session, stop) for session, stop in waiting_now if session.account == account]
        since = min(stop.at for _, stop in on_account)
        answers = [
            answer for session in sessions if session.account == account
            if (answer := last_answer(session.session_id, since)) is not None
            # An answer from before the user moved the session here came on the account it was on then.
            and not ((moved := moved_at(conn, session.session_id)) is not None and answer[0] <= moved)
        ]
        back = usage_back(readings.get(account), since, now, max(answers) if answers else None)
        for session, stop in on_account:
            why = back
            if why is None and stop.resets is not None and stop.resets <= now:
                why = f"the limit it stopped on ended at {stop.resets.astimezone(now.tzinfo):%H:%M} UTC"
            if why is None:
                continue
            target = {"session_id": session.session_id}
            # Judged again in the message's transaction: a move's continue or an archive that came in while the
            # answers were read takes its place.
            with db.transaction(conn):
                if not unreached(conn, session, stop.at):
                    log.info("session %s got a message since it was found waiting on the limit", session.session_id)
                    continue
                message_ids.append(store.create_message(conn, ACTOR, CONTINUE.format(why=why), target, checkouts_dir)["id"])
            log.info(
                "usage is back for session %s on the %s account (%s); queued message %s",
                session.session_id, account, why, message_ids[-1],
            )
    return message_ids


def moved_at(conn: sqlite3.Connection, session_id: str) -> datetime | None:
    """When the user last moved the session to another account (moves.record), or None."""
    row = conn.execute("SELECT at FROM session_moves WHERE session_id = ?", (session_id,)).fetchone()
    return rank.parse_time(row["at"]) if row is not None else None


def ended_on_purpose(conn: sqlite3.Connection, session_id: str, since: datetime) -> bool:
    """After `since`, the user archived the stopped session and has not taken it back, or an agent closed it: it
    ended on purpose. Cockpit's own close of an idle session does not count."""
    after = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    archived = conn.execute(
        "SELECT 1 FROM session_archives WHERE session_id = ? AND asked_at > ? AND cancelled_at IS NULL"
        " AND revived_at IS NULL",
        (session_id, after),
    ).fetchone()
    closed = conn.execute(
        "SELECT 1 FROM events WHERE kind = 'session.closed' AND actor != ? AND json_extract(data, '$.session_id') = ?"
        " AND at > ?",
        (ACTOR, session_id, after),
    ).fetchone()
    return archived is not None or closed is not None


@dataclass(frozen=True)
class Stop:
    at: datetime
    resets: datetime | None


def usage_back(
    record: dict[str, Any] | None, since: datetime, now: datetime, answer: tuple[datetime, bool] | None
) -> str | None:
    """Why Claude's usage is back for an account, in words, or None while it is not: `answer` is Claude's newest
    answer to a session on the account after `since` and whether it went through (sessions.last_answer), and
    `record` the account's latest usage record."""
    if answer is not None and answer[1]:
        return f"a call went through at {answer[0].astimezone(since.tzinfo):%H:%M} UTC"
    if record is None:
        return None
    try:
        spent = [window for window in record["rate_limits"].values()
                 if window and float(window["used_percentage"]) >= 100]
        reset = bool(spent) and all(float(window["resets_at"]) <= now.timestamp() for window in spent)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        log.error("reading Claude usage from a record failed: %r (%s)", exc, record)
        return None
    return "the limit has reset" if reset else None


def waiting(conn: sqlite3.Connection, sessions: list[Session], now: datetime) -> list[tuple[Session, Stop]]:
    """(session, its stop) for each session the limit stopped in the last week that no message reached since. The
    session is `blocked`, or `stopped` once Cockpit closed it for being idle , but not one that ended on purpose."""
    found = []
    for session in sessions:
        if session.state not in ("blocked", "stopped"):
            continue
        stop = limit_stop(session.session_id)
        stopped_at = rank.parse_time(stop.at) if stop else None
        if stop is None or stopped_at is None or stopped_at < now - LONGEST_WAIT:
            continue
        if unreached(conn, session, stopped_at):
            found.append((session, Stop(at=stopped_at, resets=stop.resets)))
    return found


def unreached(conn: sqlite3.Connection, session: Session, stopped_at: datetime) -> bool:
    """No message reached the session since the limit stopped it at `stopped_at`, and it did not end on purpose."""
    reached = conn.execute(
        "SELECT 1 FROM messages WHERE session_id = ? AND created_at > ?",
        (session.session_id, stopped_at.strftime("%Y-%m-%dT%H:%M:%SZ")),
    ).fetchone()
    return reached is None and not (session.state == "stopped" and ended_on_purpose(conn, session.session_id, stopped_at))
