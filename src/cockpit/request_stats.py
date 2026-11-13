"""How Cockpit's routes answered, per day, route, status and caller (SPEC TRACK-13).

Every request counts: how many, how long they took to their first byte in all and at most, and how many took over a
second. The page alone asks thousands of times an hour, so counts gather in memory and a thread writes them once
a minute; a restart loses at most that minute.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from cockpit import db

KEEP_DAYS = 90
SLOW_MS = 1000
FLUSH_SECONDS = 60
# The listing shows the busiest routes, and every route that failed or was slow.
SHOWN = 25


@dataclass
class Tally:
    count: int = 0
    total_ms: int = 0
    max_ms: int = 0
    slow: int = 0


class Recorder:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.tallies: dict[tuple[str, str, str, int, str], Tally] = {}

    def add(self, now: datetime, method: str, route: str, status: int, caller: str, ms: int) -> None:
        key = (now.astimezone(UTC).strftime("%Y-%m-%d"), method, route, status, caller)
        with self.lock:
            tally = self.tallies.setdefault(key, Tally())
            tally.count += 1
            tally.total_ms += ms
            tally.max_ms = max(tally.max_ms, ms)
            tally.slow += ms > SLOW_MS

    def flush(self, conn: sqlite3.Connection, now: datetime) -> int:
        with self.lock:
            tallies, self.tallies = self.tallies, {}
        try:
            with db.transaction(conn):
                cutoff = (now - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
                conn.execute("DELETE FROM request_stats WHERE day < ?", (cutoff,))
                for (day, method, route, status, caller), tally in tallies.items():
                    conn.execute(
                        "INSERT INTO request_stats (day, method, route, status, caller, count, total_ms, max_ms, slow)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (day, method, route, status, caller) DO UPDATE"
                        " SET count = count + excluded.count, total_ms = total_ms + excluded.total_ms,"
                        " max_ms = MAX(max_ms, excluded.max_ms), slow = slow + excluded.slow",
                        (day, method, route, status, caller, tally.count, tally.total_ms, tally.max_ms, tally.slow),
                    )
        except BaseException:
            # Kept for the next flush rather than lost with a busy database.
            with self.lock:
                for key, tally in tallies.items():
                    kept = self.tallies.setdefault(key, Tally())
                    kept.count += tally.count
                    kept.total_ms += tally.total_ms
                    kept.max_ms = max(kept.max_ms, tally.max_ms)
                    kept.slow += tally.slow
            raise
        return len(tallies)


def report(conn: sqlite3.Connection, days: int, now: datetime) -> dict[str, Any]:
    """Per route over the last `days` days: requests by caller, failures (5xx), refusals (4xx), time and slow ones."""
    since = (now.astimezone(UTC) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    records = conn.execute(
        "SELECT method, route, status, caller, SUM(count) AS count, SUM(total_ms) AS total_ms, MAX(max_ms) AS max_ms,"
        " SUM(slow) AS slow FROM request_stats WHERE day >= ? GROUP BY method, route, status, caller",
        (since,),
    ).fetchall()
    routes: dict[tuple[str, str], dict[str, Any]] = {}
    for row in records:
        entry = routes.setdefault(
            (row["method"], row["route"]),
            {"method": row["method"], "route": row["route"], "count": 0, "by_caller": {}, "failed": 0, "refused": 0,
             "statuses": {}, "total_ms": 0, "max_ms": 0, "slow": 0},
        )
        entry["count"] += row["count"]
        entry["by_caller"][row["caller"]] = entry["by_caller"].get(row["caller"], 0) + row["count"]
        entry["statuses"][str(row["status"])] = entry["statuses"].get(str(row["status"]), 0) + row["count"]
        entry["failed"] += row["count"] if row["status"] >= 500 else 0
        entry["refused"] += row["count"] if 400 <= row["status"] < 500 else 0
        entry["total_ms"] += row["total_ms"]
        entry["max_ms"] = max(entry["max_ms"], row["max_ms"])
        entry["slow"] += row["slow"]
    listing = sorted(routes.values(), key=lambda entry: (-entry["count"], entry["route"], entry["method"]))
    for entry in listing:
        entry["mean_ms"] = round(entry.pop("total_ms") / entry["count"])
    shown = listing[:SHOWN] + [entry for entry in listing[SHOWN:] if entry["failed"] or entry["slow"]]
    first = conn.execute("SELECT MIN(day) FROM request_stats").fetchone()[0]
    return {
        "days": days, "since": since, "tracked_since": first, "routes": shown,
        "requests": sum(entry["count"] for entry in listing),
        "failed": sum(entry["failed"] for entry in listing),
        "slow": sum(entry["slow"] for entry in listing),
    }
