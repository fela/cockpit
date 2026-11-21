"""Auto mode's checker gives no verdict: Cockpit asks the session to try again, then shows one card (SPEC PERM-32).

When auto mode's checker fails (its model is unavailable, or its answer ends before the verdict), Claude Code refuses
the call with the reason "Classifier unavailable" and tells the session it may try once more. The refusal judges
nothing, so rules or sentences on a card would not help the user. Cockpit holds these refusals off the page and
sends the session a message to try them again, RETRY_DELAYS apart. A refusal that did not come back after Cockpit's
message reached the session folds away with the calls the sessions got past, as does one Claude read in the
transcript that the session got past. Refusals that still come back after the last try, or once RETRY_WINDOW has
passed, share one card on the page, for the whole outage, which offers Try again and Dismiss.

Each try is kept per call (the checker_tries table), with the call's refusal count when Cockpit queued it: a refusal
that comes back raises the count, however soon after the message it comes. Only tries queued since the call's own
outage began count, so a call refused again in a later outage gets its tries afresh. A session's outage starts when the
checker's refusals began for its oldest open refusal of this kind (`no_verdict_since`, which an earlier refusal of
the call for another reason does not move): dismissing them all starts the count again.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from cockpit import db, limits, permissions, store

log = logging.getLogger(__name__)

ACTOR = "cockpit"
NO_VERDICT = "Classifier unavailable"
# The wait before each try, counted from the call's latest refusal, or from the try before if that came later.
RETRY_DELAYS = (timedelta(minutes=2), timedelta(minutes=5), timedelta(minutes=10))
# Past this since a session's outage began, Cockpit sends no more tries on its own and shows the refusals.
RETRY_WINDOW = timedelta(hours=1)
# Refusals this old have left the page (permissions.DENIED_SHOWN_HOURS).
SHOWN_FOR = timedelta(hours=24)
RETRY_PREFIX = "Auto mode's checker gave no verdict"
HEADINGS_SHOWN = 5


@dataclass(frozen=True)
class Retries:
    """An open no-verdict refusal's tries: when its session's outage began (`since`), how many messages Cockpit
    queued asking the session to make the call again (`tries`), and for the newest of them, when Cockpit queued it
    (`queued_at`), the call's refusal count then (`count`), the message's state, and when it reached the session
    (`sent_at`)."""

    since: str
    tries: int
    queued_at: str | None
    count: int | None
    state: str | None
    sent_at: str | None

    def asked_again(self, request: dict[str, Any]) -> bool:
        """Cockpit's message to try this call again reached the session, and auto mode has not refused it since."""
        return self.state == "sent" and request["count"] == self.count

    def unsent(self) -> bool:
        """Cockpit's newest message to try this call again waits for delivery."""
        return self.state in ("queued", "sending")

    def waits(self, request: dict[str, Any], now: datetime) -> bool:
        """The refusal stays off the page: the session got past it on its own or after Cockpit asked, or, within
        RETRY_WINDOW of the outage's start, a try is still to come or the last one has not reached the session."""
        if self.asked_again(request) or permissions.moved_past(request):
            return False
        return now - datetime.fromisoformat(self.since) < RETRY_WINDOW and (
            self.unsent() or self.tries < len(RETRY_DELAYS)
        )

    def due(self, request: dict[str, Any], now: datetime) -> bool:
        """The next try waited its delay, and no message waits for delivery."""
        start = max(request["last_at"], self.queued_at or request["last_at"])
        return (
            not self.unsent() and self.waits(request, now)
            and now >= datetime.fromisoformat(start) + RETRY_DELAYS[self.tries]
        )

    def shown(self, request: dict[str, Any], now: datetime) -> bool:
        """The refusal is on the outage card: Cockpit stopped holding it, and the session did not get past it."""
        return not self.waits(request, now) and not self.asked_again(request) and not permissions.moved_past(request)


def no_verdict(request: dict[str, Any]) -> bool:
    """Auto mode refused the call because its checker gave no verdict."""
    return request["kind"] == "denied" and request["reason"] == NO_VERDICT


def retries(conn: sqlite3.Connection, requests: list[dict[str, Any]]) -> dict[int, Retries]:
    """The tries of each open no-verdict refusal among `requests`, by request id."""
    refusals = [request for request in requests if no_verdict(request) and request["state"] == "open"]
    since: dict[str, str] = {}
    for request in refusals:
        session_id = request["session_id"]
        start = request["no_verdict_since"]
        since[session_id] = min(since.get(session_id, start), start)
    tries: dict[int, list[sqlite3.Row]] = {}
    ids = [request["id"] for request in refusals]
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        for row in conn.execute(
            "SELECT tries.request_id, tries.count, messages.created_at, messages.state, messages.sent_at"
            " FROM checker_tries AS tries JOIN messages ON messages.id = tries.message_id"
            f" WHERE tries.request_id IN ({', '.join('?' * len(chunk))}) ORDER BY tries.message_id",
            chunk,
        ).fetchall():
            tries.setdefault(row["request_id"], []).append(row)
    found = {}
    for request in refusals:
        # A refusal row is reused for the same call, so tries from an earlier outage of it may be on file: only the
        # tries queued since this outage of the call began count.
        rows = [row for row in tries.get(request["id"], []) if row["created_at"] >= request["no_verdict_since"]]
        last = rows[-1] if rows else None
        found[request["id"]] = Retries(
            since=since[request["session_id"]],
            tries=len(rows),
            queued_at=None if last is None else last["created_at"],
            count=None if last is None else last["count"],
            state=None if last is None else last["state"],
            sent_at=None if last is None else last["sent_at"],
        )
    return found


def open_refusals(conn: sqlite3.Connection, now: datetime) -> list[dict[str, Any]]:
    """The open no-verdict refusals the page can still show."""
    return db.rows(conn.execute(
        "SELECT * FROM permission_requests WHERE kind = 'denied' AND state = 'open' AND reason = ? AND last_at >= ?"
        " ORDER BY id",
        (NO_VERDICT, _iso(now - SHOWN_FOR)),
    ).fetchall())


def sync(conn: sqlite3.Connection, now: datetime, checkouts_dir: Path) -> list[int]:
    """Queue a message to try again to each session with a no-verdict refusal whose next try waited its delay,
    covering every refusal of that session Cockpit still holds. Returns the ids of the queued messages, for the
    caller to deliver."""
    if not open_refusals(conn, now):
        return []
    message_ids = []
    # One write transaction from reading the tries to queueing the messages, so two callers cannot both ask.
    with db.transaction(conn):
        refusals = open_refusals(conn, now)
        found = retries(conn, refusals)
        for session_id in dict.fromkeys(request["session_id"] for request in refusals):
            mine = [request for request in refusals if request["session_id"] == session_id]
            if not any(found[request["id"]].due(request, now) for request in mine):
                continue
            since = datetime.fromisoformat(found[mine[0]["id"]].since)
            if limits.ended_on_purpose(conn, session_id, since):
                continue
            pending = [
                request for request in mine
                if not found[request["id"]].unsent() and found[request["id"]].waits(request, now)
            ]
            message_ids.append(ask_again(conn, session_id, pending, now, checkouts_dir))
    return message_ids


def retry_now(conn: sqlite3.Connection, now: datetime, checkouts_dir: Path) -> list[int]:
    """The user's Try again on the outage card: a message to try again to each session with a refusal on the card,
    whatever its delay, unless one already waits for delivery. Returns the ids of the queued messages, for the caller
    to deliver."""
    message_ids = []
    with db.transaction(conn):
        refusals = open_refusals(conn, now)
        found = retries(conn, refusals)
        for session_id in dict.fromkeys(request["session_id"] for request in refusals):
            shown = [
                request for request in refusals
                if request["session_id"] == session_id and found[request["id"]].shown(request, now)
                and not found[request["id"]].unsent()
            ]
            if shown:
                message_ids.append(ask_again(conn, session_id, shown, now, checkouts_dir))
    return message_ids


def ask_again(
    conn: sqlite3.Connection, session_id: str, refusals: list[dict[str, Any]], now: datetime, checkouts_dir: Path
) -> int:
    """Queue the message asking the session to try these refused calls again, and keep it on each call."""
    with db.transaction(conn):
        message = store.create_message(conn, ACTOR, retry_text(refusals, now), {"session_id": session_id}, checkouts_dir)
        conn.executemany(
            "INSERT INTO checker_tries (request_id, message_id, count) VALUES (?, ?, ?)",
            [(request["id"], message["id"], request["count"]) for request in refusals],
        )
    log.info("asked session %s to try %d calls again that auto mode's checker gave no verdict on; queued message %s",
             session_id, len(refusals), message["id"])
    return message["id"]


def retry_text(refusals: list[dict[str, Any]], now: datetime) -> str:
    """The message asking the session to try its refused calls again, naming each."""
    one = len(refusals) == 1
    newest = max(datetime.fromisoformat(request["last_at"]) for request in refusals)
    names = [f"- {name_of(request)}" for request in refusals[:HEADINGS_SHOWN]]
    if len(refusals) > HEADINGS_SHOWN:
        names.append(f"- and {len(refusals) - HEADINGS_SHOWN} more")
    calls, they, them = ("this call", "it", "it") if one else (f"these {len(refusals)} calls", "they", "them")
    return "\n".join([
        f"{RETRY_PREFIX} on {calls}, last at {newest.astimezone(now.tzinfo):%H:%M} UTC, so {they} did not run:",
        *names,
        f"The checker failed; it did not judge {them}. Try {them} again now, unchanged, unless you already made"
        f" {them} again and {they} ran. If the checker still gives no verdict, go on with work that does not need"
        f" {them}.",
    ])


def name_of(request: dict[str, Any]) -> str:
    """The call in a few words: Claude's heading once read, the call's description (a Bash command's, or a subagent's
    task), its command, or the tool."""
    tool_input = request["tool_input"]
    words = [(request["explanation"] or {}).get("heading"), tool_input.get("description"), tool_input.get("command")]
    named = next((word for word in words if isinstance(word, str) and word.strip()), request["tool_name"])
    return named.strip().splitlines()[0][:160]


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
