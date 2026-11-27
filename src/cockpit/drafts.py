"""The words waiting unsent in the page's boxes, kept on the server so they follow the user between devices.

The page keeps its drafts in the browser and sends each change here (`put`); every state it reads carries the
drafts back (`state`), so another device's page takes them up. A key names one stored value of the page, such as
`drafts/msg:step:12` for a step's reply box; the value is the page's own JSON text, and an empty value means the
box was emptied. Each change takes the next number of one counter, so a page tells a newer value from one it
already has, and `at`, the latest number, tells it that a draft missing from the state was emptied since.

A change names the version it was made on. One made on an older version than the stored words is not taken: two
devices wrote the same box, and the page joins the words and sends them again.

Each request has an id, and Cockpit keeps which words it took for each one (ANSWER_DAYS). A page that never heard the
answer, because the network or the page went away, sends the same request again and gets the first answer, so it
knows whether its words reached Cockpit before another device changed them. A request sent while earlier ones were
on their way names them (`follows`): its words were typed over theirs, so a box the newest of them to write it wrote
counts as seen, and a box it writes before one of them arrives keeps its words over that one's older ones.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from cockpit import db, store

KEY_CHARS = 300
# A full Add box (store.ADD_MAX_CHARS) as JSON text, where an escaped character takes at most six.
VALUE_CHARS = 6 * store.ADD_MAX_CHARS + 2
CHANGES = 200
REQUEST_CHARS = 64
# Requests one request may name in `follows`; the page names its newest ones.
FOLLOWS = 100
# A request older than this, sent again, is taken as new.
ANSWER_DAYS = 30


def state(conn: sqlite3.Connection) -> dict:
    """The drafts that hold words, as key → [value, version], and the latest version."""
    rows = conn.execute("SELECT key, value, version FROM drafts WHERE value != ''").fetchall()
    return {"at": _latest(conn), "entries": {row["key"]: [row["value"], row["version"]] for row in rows}}


def put(conn: sqlite3.Connection, request: str | None, follows: list[str], changes: list[tuple[str, str, int]]) -> dict:
    """Store each (key, value, base) of request `request` whose base is the stored version (0 for a key never
    stored), whose base is 0 over an emptied box, or whose stored value the newest of the requests `follows` that took
    the key wrote. A key that a later request following this one changed first is not taken.

    Returns every changed key's [value, version]: for a taken key the one this request stored, for another the stored
    one. Also returns the keys taken and the latest version. A value equal to the stored one is taken without a new
    version. A request already answered gets the same keys taken at the same versions. A request without an id, from
    a page loaded before requests had ids, is taken as new each time.
    """
    with db.transaction(conn):
        answered = _answer(conn, request) if request is not None else None
        if answered is not None:
            entries = {key: answered[key] if key in answered else list(_stored(conn, key)) for key, _, _ in changes}
            return {"at": _latest(conn), "entries": entries, "taken": [key for key, _, _ in changes if key in answered]}
        # The requests followed, newest first, with the keys each took; None for one Cockpit has not received yet.
        earlier = [(other, _answer(conn, other)) for other in reversed(follows)]
        followed = {row["key"] for row in conn.execute("SELECT key FROM draft_followed WHERE request = ?", (request,))}
        latest = _latest(conn)
        now = db.now_iso()
        entries: dict[str, list] = {}
        taken = []
        for key, value, base in changes:
            stored = _stored(conn, key)
            # The page keeps no version of an emptied box, so words written over one with base 0 are taken. Words made
            # on a version the box had before it was emptied are not: the page then holds both and joins them.
            seen = key not in followed and (
                base == stored[1] or (base == 0 and not stored[0])
                or next((answer[key][1] for _, answer in earlier if answer and key in answer), None) == stored[1])
            # A followed request still on its way holds older words of the key than these, taken or not: they are not
            # taken after these arrived.
            conn.executemany(
                "INSERT OR IGNORE INTO draft_followed(request, key, created_at) VALUES (?, ?, ?)",
                [(other, key, now) for other, answer in earlier if answer is None],
            )
            if not seen:
                entries[key] = list(stored)
                continue
            taken.append(key)
            if value == stored[0]:
                entries[key] = list(stored)
                continue
            latest += 1
            conn.execute(
                "INSERT INTO drafts(key, value, version, updated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value, version = excluded.version,"
                " updated_at = excluded.updated_at",
                (key, value, latest, now),
            )
            entries[key] = [value, latest]
        oldest = (datetime.now(UTC) - timedelta(days=ANSWER_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute("DELETE FROM draft_answers WHERE created_at < ?", (oldest,))
        conn.execute("DELETE FROM draft_followed WHERE created_at < ?", (oldest,))
        if request is not None:
            conn.execute(
                "INSERT INTO draft_answers(id, taken, created_at) VALUES (?, ?, ?)",
                (request, json.dumps({key: entries[key] for key in taken}), now),
            )
    return {"at": latest, "entries": entries, "taken": taken}


def _answer(conn: sqlite3.Connection, request: str) -> dict | None:
    """The keys request `request` took, as key → [value, version], or None when Cockpit has no answer for it."""
    row = conn.execute("SELECT taken FROM draft_answers WHERE id = ?", (request,)).fetchone()
    return json.loads(row["taken"]) if row else None


def _stored(conn: sqlite3.Connection, key: str) -> tuple[str, int]:
    row = conn.execute("SELECT value, version FROM drafts WHERE key = ?", (key,)).fetchone()
    return (row["value"], row["version"]) if row else ("", 0)


def _latest(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(MAX(version), 0) AS latest FROM drafts").fetchone()["latest"]
