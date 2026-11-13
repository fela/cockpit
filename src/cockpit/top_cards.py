"""Top cards: what the user should do now that is not a step on a task, such as a meeting that starts soon
(SPEC NEXT-65).

A top card sits above Inbox with the warnings and takes none of its four places, so no card displaces a step and the
ranking stays as it is (SPEC RANK-18). A session puts one by its `need`, a name that holds what
the card is about and when, such as `standup:2026-01-15`. Putting that need again changes the card, unless the card
has closed: a closed need stays closed. A card leaves when the user answers it, when its session closes it, or when
its `ends_at` passes, so a card from a session that has stopped still leaves on time.

An answer goes to the session named on the card as an ordinary message, and `cockpit top list` shows it too. Answers
are buttons only, so a card collects no free text. `seen_at` is when a page first showed the card on a screen the user
had in front of them, so a session can fall back to another channel only for a card nobody saw.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from cockpit import db, store

TITLE_LIMIT = 120
BODY_LIMIT = 800
ANSWER_LIMIT = 40
MAX_ANSWERS = 4
# How long `cockpit top list` keeps a closed card, so a session can see whether its card was answered.
RECENT = timedelta(days=2)
STAMP = "%Y-%m-%dT%H:%M:%SZ"


def _shown(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row) | {"answers": json.loads(row["answers"])}


def _when(text: str, field: str) -> str:
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as error:
        raise store.Invalid(f"{field} must be an ISO time such as 2026-01-15T12:00:00Z, not {text!r}") from error
    if moment.tzinfo is None:
        raise store.Invalid(f"{field} must say its time zone, as in 2026-01-15T12:00:00Z")
    return moment.astimezone(UTC).strftime(STAMP)


def put(conn: sqlite3.Connection, body: dict[str, Any], author: str) -> dict[str, Any]:
    """Create or change the card with this need; a closed need stays closed and raises Conflict."""
    need = str(body.get("need") or "").strip()
    title = str(body.get("title") or "").strip()
    text = str(body.get("body") or "").strip()
    answers = [str(answer).strip() for answer in body.get("answers") or []]
    if not need:
        raise store.Invalid("a top card needs a `need`, such as standup:2026-01-15")
    if not title or len(title) > TITLE_LIMIT:
        raise store.Invalid(f"a top card needs a title of at most {TITLE_LIMIT} characters")
    if len(text) > BODY_LIMIT:
        raise store.Invalid(f"a top card's body takes at most {BODY_LIMIT} characters")
    if len(answers) > MAX_ANSWERS or not all(answers) or any(len(answer) > ANSWER_LIMIT for answer in answers):
        raise store.Invalid(f"a top card takes at most {MAX_ANSWERS} answers of at most {ANSWER_LIMIT} characters each")
    ends_at = _when(body["ends_at"], "ends_at") if body.get("ends_at") else None
    # The check and the write share one transaction, so an answer that closes the card cannot land between them.
    with db.transaction(conn):
        held = conn.execute("SELECT done_at FROM top_cards WHERE need = ?", (need,)).fetchone()
        if held is not None and held["done_at"]:
            raise store.Conflict(f"top card {need} closed at {held['done_at']}; a closed need stays closed")
        conn.execute(
            "INSERT INTO top_cards (need, title, body, answers, ends_at, session_id, created_by, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (need) DO UPDATE SET title = excluded.title, body = excluded.body,"
            " answers = excluded.answers, ends_at = excluded.ends_at, session_id = excluded.session_id",
            (need, title, text, json.dumps(answers), ends_at, body.get("session_id"), author, db.now_iso()),
        )
    return _shown(conn.execute("SELECT * FROM top_cards WHERE need = ?", (need,)).fetchone())


def showing(conn: sqlite3.Connection, now: datetime) -> list[dict[str, Any]]:
    """The cards on the page now, oldest first. An ended card goes by this filter, without a job to end it."""
    moment = now.astimezone(UTC).strftime(STAMP)
    return [_shown(row) for row in conn.execute(
        "SELECT * FROM top_cards WHERE done_at IS NULL AND (ends_at IS NULL OR ends_at > ?) ORDER BY id", (moment,)
    )]


def recent(conn: sqlite3.Connection, now: datetime) -> list[dict[str, Any]]:
    """Every card still open or closed lately, newest first, for the session that put them."""
    since = (now - RECENT).astimezone(UTC).strftime(STAMP)
    return [_shown(row) for row in conn.execute(
        "SELECT * FROM top_cards WHERE done_at IS NULL OR done_at > ? ORDER BY id DESC", (since,)
    )]


def close(conn: sqlite3.Connection, card_id: int, who: str, answer: str | None) -> dict[str, Any]:
    """Close the card; closing it again changes nothing. An answer must be one the card offers."""
    card = conn.execute("SELECT * FROM top_cards WHERE id = ?", (card_id,)).fetchone()
    if card is None:
        raise store.NotFound(f"no top card {card_id}")
    if answer is not None and answer not in json.loads(card["answers"]):
        raise store.Invalid(f"top card {card_id} does not offer the answer {answer!r}")
    if not card["done_at"]:
        with db.transaction(conn):
            conn.execute(
                "UPDATE top_cards SET done_at = ?, done_by = ?, answer = ? WHERE id = ?",
                (db.now_iso(), who, answer, card_id),
            )
    return _shown(conn.execute("SELECT * FROM top_cards WHERE id = ?", (card_id,)).fetchone())


def seen(conn: sqlite3.Connection, card_id: int) -> dict[str, Any]:
    """Note the first time a page showed the card to the user; later sightings keep that time."""
    if conn.execute("SELECT 1 FROM top_cards WHERE id = ?", (card_id,)).fetchone() is None:
        raise store.NotFound(f"no top card {card_id}")
    with db.transaction(conn):
        conn.execute("UPDATE top_cards SET seen_at = ? WHERE id = ? AND seen_at IS NULL", (db.now_iso(), card_id))
    return _shown(conn.execute("SELECT * FROM top_cards WHERE id = ?", (card_id,)).fetchone())
