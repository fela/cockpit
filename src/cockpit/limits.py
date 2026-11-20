"""Claude's usage limit: a message to each session it stopped, to continue once usage is back (SPEC SESSION-8).

When the account's usage runs out, Claude Code ends each running session's turn with an error
("You've hit your weekly limit · resets Oct 10, 12pm (UTC)"); the session is `blocked`, and `stopped` once
Cockpit closes it for being idle (delivery.py). A session waits on the limit until a message reaches it after its
stop, from Cockpit or anyone else, for at most a week, the longest a limit lasts. Automatic recovery resumes
current unfinished assignments; completed or replaced owners, user decisions, snoozes and future rests wait.
Standalone sessions keep their recovery. Each queued retry records the stop it answers and is rechecked at delivery. The user has nothing to do
here, so Cockpit puts no step for it.

The newest answer on the same account and model frees older stops only when it succeeded. A quota stop can also resume when its stated reset or the account's exhausted windows have reset.
A model cooldown stop has no quota reset time: Cockpit retries after 10, 20, 40, then 80 minutes,
capped at 80, counting consecutive failures in the transcript. Account-wide readings cannot free these stops.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from cockpit import db, handovers, patterns, rank, store
from cockpit.sessions import Session, limit_stop, transcript_usage

log = logging.getLogger(__name__)

ACTOR = "cockpit"
# A weekly limit lasts at most a week: a session that stopped on one longer ago and that no one has continued
# was left on purpose.
LONGEST_WAIT = timedelta(days=7)
# Quota recovery shares ten working places with sessions already running.
RECOVERY_CAPACITY = 10
RECOVERY_START_SECONDS = 120
CONTINUE = (
    "Claude's usage works again: {why}. Your last turn ended on the usage limit."
    " Continue the task you were on from where it stopped."
)

RETRY = (
    "Claude's model cooldown retry: {why}. Your last turn ended on a rate limit that names no end: a cooldown on the"
    " model, or the account's rate limit."
    " Try continuing the task from where it stopped."
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
    by_id = {session.session_id: session for session in sessions}
    for message in db.rows(conn.execute(
        "SELECT * FROM messages WHERE quota_recovery_stop IS NOT NULL AND state = 'queued'"
    ).fetchall()):
        cancel_stale(conn, message, by_id.get(message["session_id"]), now)
    waiting_now = [(session, stop) for session, stop in waiting(conn, sessions, now)
                   if resume_refusal(conn, session.session_id, now) is None]
    message_ids = []
    queued = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE author = 'cockpit' AND state IN ('queued', 'sending')"
        " AND (text LIKE 'Claude''s usage works again:%' OR text LIKE 'Claude''s model cooldown retry:%')"
    ).fetchone()[0]
    # Socket delivery returns before the cached session listing observes the resumed turn.
    sent = conn.execute(
        "SELECT session_id, sent_at FROM messages WHERE author = 'cockpit' AND state = 'sent'"
        " AND (text LIKE 'Claude''s usage works again:%' OR text LIKE 'Claude''s model cooldown retry:%') AND sent_at >= ?",
        ((now - timedelta(seconds=RECOVERY_START_SECONDS)).strftime("%Y-%m-%dT%H:%M:%SZ"),),
    ).fetchall()
    for row in sent:
        session = by_id.get(row["session_id"])
        if session is None:
            queued += 1
        elif not session.working_now:
            updated, delivered = rank.parse_time(session.updated_at), rank.parse_time(row["sent_at"])
            if updated is None or delivered is None or updated <= delivered:
                queued += 1
    room = max(0, RECOVERY_CAPACITY - sum(session.working_now for session in sessions) - queued)
    if not waiting_now or not room:
        return message_ids
    # Keep model and answer from one transcript snapshot, including a model switch during this poll.
    usages = {session.session_id: transcript_usage(session.session_id) for session in sessions
              if session.provider == "claude"}
    models = {sid: usage.model for sid, usage in usages.items()}
    for account, model in sorted({(session.account, models[session.session_id]) for session, _ in waiting_now}):
        on_model = [(session, stop) for session, stop in waiting_now
                    if session.account == account and models[session.session_id] == model]
        since = min(stop.at for _, stop in on_model)
        answers = [
            answer for session in sessions if session.provider == "claude" and session.account == account
            and model and models[session.session_id] == model
            if (answer := usages[session.session_id].answer) is not None and answer[0] > since
            # An answer from before the user moved the session here came on the account it was on then.
            and not ((moved := moved_at(conn, session.session_id)) is not None and answer[0] <= moved)
        ]
        for session, stop in on_model:
            if len(message_ids) >= room:
                return message_ids
            # A newer failure outweighs an older success: that model is still limited.
            newer = [answer for answer in answers if answer[0] > stop.at]
            why = usage_back(None if stop.cooldown_stops else readings.get(account), stop.at, now,
                             max(newer) if newer else None)
            template = CONTINUE
            if why is None and stop.cooldown_stops:
                delay = timedelta(minutes=10 * 2 ** (stop.cooldown_stops - 1))
                if stop.at + delay <= now:
                    why = f"{int(delay.total_seconds() // 60)} minutes have passed since the last rate-limit stop"
                    template = RETRY
            if why is None and stop.resets is not None and stop.resets <= now:
                why = f"the limit it stopped on ended at {stop.resets.astimezone(now.tzinfo):%H:%M} UTC"
            if why is None:
                continue
            target = {"session_id": session.session_id}
            # Judged again in the message's transaction: a move's continue or an archive that came in while the
            # answers were read takes its place.
            with db.transaction(conn):
                if not unreached(conn, session, stop.at) or resume_refusal(conn, session.session_id, now) is not None:
                    log.info("session %s no longer needs automatic quota recovery", session.session_id)
                    continue
                message_id = store.create_message(conn, ACTOR, template.format(why=why), target, checkouts_dir)["id"]
                conn.execute("UPDATE messages SET quota_recovery_stop = ? WHERE id = ?", (stop.at.isoformat(), message_id,))
                message_ids.append(message_id)
            log.info(
                "quota recovery for session %s on the %s account (%s); queued message %s",
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
    cooldown_stops: int


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
    session is `blocked`, or `stopped` once Cockpit closed it for being idle, but not one that ended on purpose."""
    found = []
    for session in sessions:
        if session.provider != "claude" or session.state not in ("blocked", "stopped"):
            continue
        stop = limit_stop(session.session_id)
        stopped_at = rank.parse_time(stop.at) if stop else None
        if stop is None or stopped_at is None or stopped_at < now - LONGEST_WAIT:
            continue
        if unreached(conn, session, stopped_at):
            found.append((session, Stop(at=stopped_at, resets=stop.resets, cooldown_stops=stop.cooldown_stops)))
    return found


def unreached(conn: sqlite3.Connection, session: Session, stopped_at: datetime) -> bool:
    """No message reached the session since the limit stopped it at `stopped_at`, and it did not end on purpose."""
    if handovers.held(conn, session.session_id):
        return False
    reached = conn.execute(
        "SELECT 1 FROM messages WHERE session_id = ? AND created_at > ?"
        " AND NOT (quota_recovery_stop IS NOT NULL AND dismissed_at IS NOT NULL AND detail LIKE 'Not sent:%')",
        (session.session_id, stopped_at.strftime("%Y-%m-%dT%H:%M:%SZ")),
    ).fetchone()
    return reached is None and not ended_on_purpose(conn, session.session_id, stopped_at)


def resume_refusal(conn: sqlite3.Connection, session_id: str, now: datetime) -> str | None:
    """Why automatic quota recovery would resume work that no longer belongs to this session now.

    A session with task history needs a current assignment: primary owner, an explicit agent wait, or a
    handed step. Secondary session ids retain history. Standalone unfinished sessions still recover.
    """
    if handovers.held(conn, session_id):
        return "the session has transferred to another agent"
    items = db.rows(conn.execute(
        "SELECT i.* FROM items i WHERE EXISTS (SELECT 1 FROM json_each(i.sessions) WHERE value = ?)"
        " OR EXISTS (SELECT 1 FROM json_each(i.waiting_on) WHERE json_extract(value, '$.kind') = 'agent'"
        " AND json_extract(value, '$.who') = ?)"
        " OR i.id IN (SELECT item_id FROM steps WHERE session_id = ?)"
        " OR i.id IN (SELECT item_id FROM messages WHERE session_id = ?)"
        " OR i.id IN (SELECT item_id FROM item_agent_history WHERE session_id = ?)",
        (session_id, session_id, session_id, session_id, session_id),
    ).fetchall())
    if not items:
        return None
    actionable = False
    for item in items:
        if item["archived"] or item["stage"] == "done" or item.get("merged_into"):
            continue
        steps = db.rows(conn.execute(
            f"SELECT s.*, m.session_id AS handed_to FROM steps s LEFT JOIN messages m ON m.id = s.handoff_message_id"
            f" WHERE s.item_id = ? AND {store.OPEN}", (item["id"],),
        ).fetchall())
        waits = [entry for entry in item["waiting_on"] if entry["kind"] == "agent" and entry["who"] == session_id]
        handed = any(step["handed_to"] == session_id for step in steps)
        primary = item["sessions"][:1] == [session_id]
        if not (primary or waits or handed):
            continue
        rests = waits or ([entry for entry in item["waiting_on"] if entry["kind"] == "agent"] if primary else [])
        if any((until := rank.parse_time(entry.get("until"))) is not None and until > now for entry in rests):
            return "a current assignment rests until a future time"
        for step in steps:
            until = rank.parse_time(step["snoozed_until"])
            if until is not None and until > now:
                return "a current assignment is snoozed by the user"
            if step["handoff_message_id"] is not None or step["need"].startswith("move:"):
                continue
            # A fresh instruction can ask for more work while an earlier card stays open. Ranking edits
            # update updated_at, so prepared_at gives the time of the agent's actual question instead.
            instructed = conn.execute(
                "SELECT 1 FROM messages WHERE author = 'user' AND session_id = ? AND state = 'sent'"
                " AND (item_id = ? OR item_id IS NULL) AND julianday(created_at) > julianday(?)",
                (session_id, item["id"], step["prepared_at"] or step["created_at"]),
            ).fetchone()
            progress = store.is_read(step) and item["stage"] in (None, "planning", "building", "review")
            if instructed is None and not (progress or store.is_read(step) and (waits or handed)):
                return "a current assignment waits on the user"
        # A primary owner with a future/external wait has no immediate work. An explicitly assigned agent
        # or handed step can progress independently of another wait on the same task.
        if item["waiting_on"] and not waits and not handed:
            continue
        actionable = True
    return None if actionable else "its tasks are finished, reassigned, or waiting for their next move"


def cancel_stale(conn: sqlite3.Connection, message: dict[str, Any], session: Session | None, now: datetime) -> bool:
    """Recheck an automatic retry before delivery; retain a dismissed record when it no longer has work.

    Explicit user messages and user-requested account moves carry no quota_recovery_stop marker.
    """
    if not message["quota_recovery_stop"]:
        return False
    with db.transaction(conn):
        reason = resume_refusal(conn, message["session_id"], now)
        stop = limit_stop(message["session_id"])
        if session is None or session.state not in ("blocked", "stopped") or stop is None or (
            rank.parse_time(stop.at) != rank.parse_time(message["quota_recovery_stop"])
        ):
            reason = "the quota stop this retry answered is no longer current"
        stopped = rank.parse_time(message["quota_recovery_stop"])
        if stopped is not None and ended_on_purpose(conn, message["session_id"], stopped):
            reason = "the session was closed intentionally"
        if conn.execute(
            "SELECT 1 FROM messages WHERE session_id = ? AND id > ? AND dismissed_at IS NULL",
            (message["session_id"], message["id"]),
        ).fetchone():
            reason = "a newer message takes the place of this automatic retry"
        if reason is None:
            return False
        changed = conn.execute(
            "UPDATE messages SET state = 'failed', detail = ?, dismissed_at = ?, dismissed_by = 'cockpit',"
            " dismiss_reason = ?, waits_since = NULL, capacity_since = NULL"
            " WHERE id = ? AND state = 'queued' AND quota_recovery_stop IS NOT NULL",
            (f"Not sent: {reason}.", db.now_iso(), reason, message["id"]),
        )
        if changed.rowcount:
            patterns.record(conn, "message.dismissed", ACTOR, message["item_id"], message["step_id"], None,
                                  {"message_id": message["id"], "reason": reason})
            log.info("automatic quota recovery %s left undelivered: %s", message["id"], reason)
        return bool(changed.rowcount)
