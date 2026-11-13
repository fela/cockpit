"""Errors Cockpit's own pages met, so none is lost in a browser console (SPEC CODE-2).

The pages' errors.js sends each error it meets. One row holds one error: the same
page, place and message (numbers aside) add to its count, so a loop of errors grows
one number and costs the sweep one line. A session takes the open errors to fix
them; an error that comes back from a page loaded after it was taken opens again,
because the fix did not hold.

An error from Cockpit being away (a 502, 503 or 504, or no connection) that a restart explains is not logged: the
page says how long ago it first met Cockpit away, and when Cockpit started or began to stop within RESTART_WINDOW
after that, the error was the restart's.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from cockpit import db, store

log = logging.getLogger(__name__)

# Rows kept: past this, the errors seen longest ago go.
KEEP = 200
# The listing shows this many errors, most recent first; the rest are counted.
SHOWN = 10
# The journal gets an error's first sighting, its reopening, and its count at each of these.
LOGGED_COUNTS = (10, 100, 1000, 10000, 100000)
# How long after a page first met Cockpit away a restart still explains it: a restart can take minutes to stop
# and start, and a page can meet Cockpit slow before it stops.
RESTART_WINDOW = timedelta(minutes=5)


def fingerprint(page: str, place: str, message: str) -> str:
    # Ids and counts in a place or message  would make every sighting a new error. The page's errors.js tells its
    # errors apart the same way.
    words = re.sub(r"\d+", "N", f"{place}\n{message}")
    return hashlib.sha1(f"{page}\n{words}".encode()).hexdigest()


def met_restart(away_since: datetime, started_at: datetime, stopping_at: datetime | None) -> bool:
    """Whether this server process starting at `started_at`, or stopping since `stopping_at`, explains a page
    meeting Cockpit away from `away_since` on."""
    if away_since <= started_at <= away_since + RESTART_WINDOW:
        return True
    return stopping_at is not None and away_since >= stopping_at - RESTART_WINDOW


def record(
    conn: sqlite3.Connection, page: str, place: str, message: str, stack: str, count: int, loaded_at: str,
    restart: bool,
) -> dict[str, Any] | None:
    """Adds the sightings to their error and returns its row; None for a `restart`'s, which are only noted."""
    if restart:
        log.info("page error on %s (%s) met Cockpit restarting, so it is not logged: %s", page, place, message)
        return None
    key = fingerprint(page, place, message)
    now = db.now_iso()
    with db.transaction(conn):
        before = conn.execute("SELECT * FROM page_errors WHERE fingerprint = ?", (key,)).fetchone()
        if before is None:
            conn.execute(
                "INSERT INTO page_errors (fingerprint, page, place, message, stack, count, first_at, last_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (key, page, place, message, stack, count, now, now),
            )
            conn.execute(
                "DELETE FROM page_errors WHERE id NOT IN (SELECT id FROM page_errors ORDER BY last_at DESC, id DESC"
                " LIMIT ?)",
                (KEEP,),
            )
        else:
            # A page loaded before the error was taken still runs the old code: its sightings only count.
            reopen = before["taken_at"] is not None and loaded_at > before["taken_at"]
            conn.execute(
                "UPDATE page_errors SET count = count + ?, last_at = ?, message = ?, stack = ?,"
                " taken_at = CASE WHEN ? THEN NULL ELSE taken_at END WHERE id = ?",
                (count, now, message, stack, reopen, before["id"]),
            )
        after = db.row(conn.execute("SELECT * FROM page_errors WHERE fingerprint = ?", (key,)).fetchone())
    assert after is not None
    where = f"{page} ({place})"
    if before is None:
        log.error("page error on %s: %s\n%s", where, message, stack)
    elif before["taken_at"] is not None and after["taken_at"] is None:
        log.error("page error on %s came back after it was taken (%s): %s", where, after["taken_note"], message)
    else:
        passed = [mark for mark in LOGGED_COUNTS if before["count"] < mark <= after["count"]]
        if passed:
            log.error("page error on %s seen %d times: %s", where, after["count"], message)
    return after


def listing(conn: sqlite3.Connection, include_taken: bool) -> dict[str, Any]:
    where = "" if include_taken else " WHERE taken_at IS NULL"
    total = conn.execute(f"SELECT COUNT(*) FROM page_errors{where}").fetchone()[0]
    shown = db.rows(
        conn.execute(f"SELECT * FROM page_errors{where} ORDER BY last_at DESC, id DESC LIMIT ?", (SHOWN,)).fetchall()
    )
    return {"total": total, "errors": shown}


def open_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM page_errors WHERE taken_at IS NULL").fetchone()[0]


def take(conn: sqlite3.Connection, ids: list[int], note: str) -> int:
    """Marks open errors as taken by whoever fixes them, with `note` saying who."""
    with db.transaction(conn):
        found = {
            row["id"]
            for row in conn.execute(
                f"SELECT id FROM page_errors WHERE taken_at IS NULL AND id IN ({','.join('?' * len(ids))})", ids
            ).fetchall()
        }
        missing = sorted(set(ids) - found)
        if missing:
            raise store.NotFound(f"no open page error with id {', '.join(map(str, missing))}")
        conn.execute(
            f"UPDATE page_errors SET taken_at = ?, taken_note = ? WHERE id IN ({','.join('?' * len(ids))})",
            [db.now_iso(), note, *ids],
        )
    return len(found)
