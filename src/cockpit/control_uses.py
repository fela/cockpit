"""Which controls the user presses and which hover texts they stop on (SPEC UI-45).

The page's uses.js names each control by where it sits and what it says, and sends counts in batches. A
control the page showed on a day counts once as seen that day. Each press also goes to `page_presses` with its time
and the task and card it was on (SPEC TRACK-14), so a session can follow what the user did in turn, and on what."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from cockpit import db

PRESSES_KEPT_DAYS = 90
# The trail listing shows at most this many presses, the latest.
PRESSES_SHOWN = 2000


def record(
    conn: sqlite3.Connection, batch: str, page: str, uses: list[dict[str, Any]], presses: list[dict[str, Any]],
    now: datetime,
) -> int:
    """Adds a batch's counts and presses once: a batch sent again after its answer was lost counts 0."""
    day = now.astimezone(UTC).strftime("%Y-%m-%d")
    with db.transaction(conn):
        conn.execute("DELETE FROM control_batches WHERE day < ?", ((now - timedelta(days=2)).strftime("%Y-%m-%d"),))
        taken = conn.execute("INSERT INTO control_batches (id, day) VALUES (?, ?) ON CONFLICT (id) DO NOTHING", (batch, day))
        if not taken.rowcount:
            return 0
        for use in uses:
            count = 1 if use["kind"] == "seen" else use["count"]
            conn.execute(
                "INSERT INTO control_uses (day, page, control, kind, count) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (day, page, control, kind) DO UPDATE SET"
                " count = CASE WHEN excluded.kind = 'seen' THEN 1 ELSE count + excluded.count END",
                (day, page, use["control"], use["kind"], count),
            )
        cutoff = (now - timedelta(days=PRESSES_KEPT_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute("DELETE FROM page_presses WHERE at < ?", (cutoff,))
        conn.executemany(
            # A card names its step alone; its task is the step's.
            "INSERT INTO page_presses (at, page, control, item_id, step_id)"
            " VALUES (?, ?, ?, COALESCE(?, (SELECT item_id FROM steps WHERE id = ?)), ?)",
            [(press["at"], page, press["control"], press["item_id"], press["step_id"], press["step_id"]) for press in presses],
        )
    return len(uses)


def report(conn: sqlite3.Connection, days: int, now: datetime) -> dict[str, Any]:
    """Per page and control: presses, hover stops and days seen over the last `days` days."""
    since = (now.astimezone(UTC) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    records = conn.execute(
        "SELECT page, control, kind, SUM(count) AS total, MIN(day) AS first_day"
        " FROM control_uses WHERE day >= ? GROUP BY page, control, kind",
        (since,),
    ).fetchall()
    controls: dict[tuple[str, str], dict[str, Any]] = {}
    for record_ in records:
        key = (record_["page"], record_["control"])
        entry = controls.setdefault(
            key, {"page": key[0], "control": key[1], "press": 0, "hover": 0, "seen": 0, "since": ""}
        )
        entry[record_["kind"]] = record_["total"]
        entry["since"] = min(filter(None, [entry["since"], record_["first_day"]]))
    first = conn.execute("SELECT MIN(day) FROM control_uses").fetchone()[0]
    listing = sorted(controls.values(), key=lambda c: (-c["press"], -c["hover"], -c["seen"], c["page"], c["control"]))
    return {"days": days, "since": since, "tracked_since": first, "controls": listing}


def presses(conn: sqlite3.Connection, days: int, item_id: int | None, now: datetime) -> dict[str, Any]:
    """The user's presses over the last `days` days, oldest first, on one item when `item_id` names one."""
    since = (now.astimezone(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    where, params = ("at >= ? AND item_id = ?", (since, item_id)) if item_id is not None else ("at >= ?", (since,))
    total = conn.execute(f"SELECT COUNT(*) FROM page_presses WHERE {where}", params).fetchone()[0]
    shown = db.rows(
        conn.execute(
            f"SELECT at, page, control, item_id, step_id FROM page_presses WHERE {where} ORDER BY at DESC, id DESC LIMIT ?",
            (*params, PRESSES_SHOWN),
        ).fetchall()
    )
    return {"days": days, "since": since, "total": total, "presses": shown[::-1]}
