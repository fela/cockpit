"""What Cockpit's server logs at warning and above, kept where sessions read it (SPEC TRACK-12).

The journal holds every line, but access lines far outnumber the rest there, and nothing reads it. So each warning
and error also counts in `log_counts`, one row per day and kind of line: the same logger, level and words, with
ids and numbers aside. An error also goes to the page errors (SPEC CODE-2) under page "server", so the sweep sends
it to a fix session like any error on a page, and one seen again after a restart opens again. A line that is no
bug, only a condition worth knowing, belongs at warning: it is counted and stays off the fix list. An error logged as
the server stops is counted and kept in the journal, but stays off the fix list too: the stop ends the server's
reads and runs midway, and each logs what it lost.

Logging calls come from any thread, the event loop's too, so the handler on the loggers only queues each line; one
thread writes them. A line that cannot be written goes to stderr, which the journal keeps.
"""

from __future__ import annotations

import hashlib
import logging
import logging.handlers
import queue
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cockpit import db, page_errors

KEEP_DAYS = 90
# The listing shows the most frequent kinds of line.
SHOWN = 30
MESSAGE_CHARS = 200
# A stop's signal reaches the server's children, such as an SSH read, as it reaches the server, so a thread can log
# what the stop cut off just before the server notes the stop.
STOP_SLACK = timedelta(seconds=2)
# Session ids, commit hashes and other ids, then any number: they differ per sighting of the same line.
IDS = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|\b(?=[0-9a-f]*\d)[0-9a-f]{7,64}\b|\d+", re.IGNORECASE)


def shape(message: str) -> str:
    """The first line of a message, ids and numbers replaced by N, so its sightings count as one kind of line.

    A traceback's last line, the exception, joins it: uvicorn logs every failed request with the same first line,
    and two different exceptions are two problems."""
    lines = [line for line in message.split("\n") if line.strip()]
    if not lines:
        return ""
    words = lines[0] if len(lines) == 1 else f"{lines[0]} | {lines[-1].strip()}"
    return IDS.sub("N", words)[:MESSAGE_CHARS]


def fingerprint(level: str, logger: str, shaped: str) -> str:
    return hashlib.sha1(f"{level}\n{logger}\n{shaped}".encode()).hexdigest()


def record_line(
    conn: sqlite3.Connection, level: int, logger: str, place: str, message: str, now: datetime, started_at: str,
    stopping: bool,
) -> None:
    """Counts one logged line, and sends an error to the page errors with its full text and traceback as its stack.

    `started_at` is when this server started: an error taken for fixing before then opens again when seen. An error
    logged while the server `stopping` is only noted there."""
    name = logging.getLevelName(level)
    shaped = shape(message)
    day = now.astimezone(UTC).strftime("%Y-%m-%d")
    with db.transaction(conn):
        conn.execute("DELETE FROM log_counts WHERE day < ?", ((now - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d"),))
        conn.execute(
            "INSERT INTO log_counts (day, fingerprint, level, logger, message, count, last_at)"
            " VALUES (?, ?, ?, ?, ?, 1, ?)"
            " ON CONFLICT (day, fingerprint) DO UPDATE SET count = count + 1, last_at = MAX(last_at, excluded.last_at)",
            (day, fingerprint(name, logger, shaped), name, logger, shaped, now.strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
    if level >= logging.ERROR:
        page_errors.record(conn, "server", place, shaped, message, 1, started_at, stopping)


def report(conn: sqlite3.Connection, days: int, now: datetime) -> dict[str, Any]:
    """The kinds of line logged at warning and above over the last `days` days, most frequent first."""
    since = (now.astimezone(UTC) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    lines = db.rows(
        conn.execute(
            "SELECT level, logger, message, SUM(count) AS count, COUNT(*) AS days, MAX(last_at) AS last_at"
            " FROM log_counts WHERE day >= ? GROUP BY fingerprint ORDER BY count DESC, last_at DESC",
            (since,),
        ).fetchall()
    )
    totals = {
        level: sum(line["count"] for line in lines if line["level"] == level) for level in ("WARNING", "ERROR", "CRITICAL")
    }
    first = conn.execute("SELECT MIN(day) FROM log_counts").fetchone()[0]
    return {"days": days, "since": since, "tracked_since": first, "totals": totals, "kinds": len(lines),
            "lines": lines[:SHOWN]}


class Capture(logging.Handler):
    """Writes each queued line to the database, on the queue's one thread."""

    def __init__(self, db_path: Path, started_at: str) -> None:
        super().__init__(logging.WARNING)
        self.db_path = db_path
        self.started_at = started_at
        # When the server began to stop, set by server.StopNoting.
        self.stopping_at: datetime | None = None
        self.conn: sqlite3.Connection | None = None

    def filter(self, record: logging.LogRecord) -> bool | logging.LogRecord:
        # A page error's own lines report what page_errors already holds.
        return record.name != page_errors.__name__ and super().filter(record)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self.conn is None:
                self.conn = db.connect(self.db_path)
            # The queue's handler has formatted the line, so its message carries any traceback.
            place = f"{record.name} {record.funcName}:{record.lineno}"
            logged = datetime.fromtimestamp(record.created, UTC)
            stopping = self.stopping_at is not None and logged >= self.stopping_at - STOP_SLACK
            record_line(self.conn, record.levelno, record.name, place, record.getMessage(), logged, self.started_at,
                        stopping)
        except (sqlite3.Error, OSError):
            if self.conn is not None:
                self.conn.close()
                self.conn = None
            self.handleError(record)


def install(
    db_path: Path, started_at: str
) -> tuple[logging.handlers.QueueHandler, logging.handlers.QueueListener, Capture]:
    """Adds the capture to the root logger and starts its writer.

    Loggers that keep their lines to themselves, as uvicorn's do, take the returned handler too. Stopping the
    returned listener writes the lines still queued. The capture itself is returned to be told when the server
    stops."""
    lines: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
    handler = logging.handlers.QueueHandler(lines)
    handler.setLevel(logging.WARNING)
    capture = Capture(db_path, started_at)
    listener = logging.handlers.QueueListener(lines, capture, respect_handler_level=True)
    listener.start()
    logging.getLogger().addHandler(handler)
    return handler, listener, capture
