"""What the user sets on the Settings page (SPEC CONTROL-33): when the morning briefing and the weekly digest come.

The schedule is kept in the kv store under KEY, holding only what the user changed; each value left out takes its
default. The time zone (zones.py) and the team (briefing.py) keep their own places; the page shows them too, as it
shows this browser's Vim editing, which the browser keeps.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from cockpit import db, store

KEY = "schedule"
# The morning briefing's hour, the weekly digest's day (datetime.weekday: Monday is 0) or None for no digest, and
# its hour, in the user's time zone.
DEFAULTS: dict[str, int | None] = {"briefing_hour": 7, "digest_day": 6, "digest_hour": 8}
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def schedule(conn: sqlite3.Connection) -> dict[str, int | None]:
    """The schedule as it stands: each value the user set, else its default."""
    return DEFAULTS | (db.kv_get(conn, KEY) or {})


def put_schedule(conn: sqlite3.Connection, changes: dict[str, Any]) -> dict[str, int | None]:
    """Change the values in `changes`, leaving the others as they are; returns the whole schedule."""
    for name, value in changes.items():
        if name not in DEFAULTS:
            raise store.Invalid(f"{name!r} is not part of the schedule")
        if name == "digest_day":
            if value is not None and (type(value) is not int or not 0 <= value < len(DAYS)):
                raise store.Invalid("the digest's day is 0 (Monday) to 6 (Sunday), or null for no digest")
        elif type(value) is not int or not 0 <= value <= 23:
            raise store.Invalid(f"{name.replace('_', ' ')} is an hour from 0 to 23")
    with db.transaction(conn):
        kept = (db.kv_get(conn, KEY) or {}) | changes
        db.kv_set(conn, KEY, {name: value for name, value in kept.items() if value != DEFAULTS[name]})
    return schedule(conn)
