"""A session's question to the user, as a step in the ranked queue (SPEC PERM-8).

A session that ended its turn waiting for the user's answer is `blocked`; it asks the user
something when its last message ends in a question or says what input it needs. A
status note, such as one saying a step waits in the user's queue, asks nothing. When no step in
The user's view covers it (the same rule the page used for its waiting cards: neither a
step's own session nor a session of the step's item), Cockpit puts a step for it: on
the session's item, or on an item of its own (`session:<uuid>`) when it has none. The
ask is the session's own line on what it needs, the body quotes its last message in
full, and the step names the session, so the user's reply goes to it. The step ranks like
any question for the user, and the sweep rewrites it in place with answers and a
recommendation.

A session that stopped to ask while it holds a step the user handed it asks on that step: Cockpit writes the
question into it and returns it to the user, so the answer comes back where the user asked (SPEC NEXT-37).

When the session moves on, or another step names it, Cockpit closes the
step: done when the user answered on it, else withdrawn. A step the user handed the session keeps the question
it came back with also while another step names the session. A step the user snoozed, or whose timer
runs right now, stays for the user. Each time a session stops to ask counts as its own question:
the need carries the time the session stopped, so a question the sweep dismissed stays
closed while the session waits on it. A session that stops to ask again asks its new question on its open
question step, in place, which keeps the step's place and history; a step the user handed the session takes it
instead, and the old question step closes. That time moves only with the session's turns while it
waits, and also when Cockpit's idle stop stops it, so a stopped session keeps its step.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime
from typing import Any

from markdown_it import MarkdownIt

from cockpit import db, rank, store
from cockpit.sessions import Session, last_message

log = logging.getLogger(__name__)

ACTOR = "cockpit"
NEED = re.compile(r"^question:(?P<session_id>[0-9a-f-]{36}):(?P<stopped_at>.*)$")
# The line in which a background session says what it needs from the user.
NEEDS_INPUT = re.compile(r"^\W*needs input:", re.IGNORECASE | re.MULTILINE)
# Parses a message into blocks, to find the last one (`asks_in`).
MARKDOWN = MarkdownIt("commonmark").enable("table")
SUPPORTING = ("fence", "code_block", "table_open")


def sync(conn: sqlite3.Connection, sessions: list[Session], now: datetime) -> None:
    """Close the question steps whose session moved on, then put one for each session that asks uncovered."""
    by_id = {session.session_id: session for session in sessions}
    for read in _open_questions(conn):
        try:
            _weigh(conn, read, by_id)
        except store.Invalid as exc:
            log.error("could not weigh question step %s: %s", read["id"], exc)
    for session in sessions:
        if not asks(session):
            continue
        try:
            # A step the user handed the session takes its question also while another step covers it.
            handed = _handed_to(conn, session)
            if handed is not None:
                _ask_on(conn, handed, session)
            else:
                _put(conn, session)
        except store.Invalid as exc:
            log.error("could not put the question of session %s as a step: %s", session.session_id, exc)


def _weigh(conn: sqlite3.Connection, read: dict[str, Any], by_id: dict[str, Session]) -> None:
    """Keep, renew or close one open question step, in one transaction on the step as it is now: the user may have
    handed it on since the list was read."""
    with db.transaction(conn):
        step = db.row(conn.execute(
            f"SELECT s.* FROM steps s WHERE s.id = ? AND {store.OPEN} AND s.handoff_message_id IS NULL AND s.need = ?",
            (read["id"], read["need"]),
        ).fetchone())
        if step is None:
            return
        need = NEED.match(step["need"])
        session_id = need.group("session_id")  # ty: ignore[unresolved-attribute]
        session = by_id.get(session_id)
        asks_again = (
            session is not None and asks(session) and _stopped_at(step) is not None
            and session.updated_at != need.group("stopped_at")  # ty: ignore[unresolved-attribute]
        )
        if asks_again and store.running_step(conn) == step["id"]:
            return  # The user is on the step; the poll after they pause it asks the new question.
        # A step the user handed the session takes its new question; else this step asks it in place.
        if asks_again and _handed_to(conn, session) is None and _ask_in_place(conn, step, session):
            log.info("step %s asks the new question of session %s", step["id"], session_id)
            return
        handed_back, answered = _spoken_on(conn, step)
        if (
            session is None
            or not (asks(session) or stopped_asking(session) or _only_asks_since(conn, step, session_id))
            or asks_again
            or (_named_elsewhere(conn, step, session_id) and not handed_back)
        ):
            _close(conn, step, answered)


def asks(session: Session) -> bool:
    """The session stopped to ask the user something; a dialog in its terminal shows under Blocked and waiting."""
    return (
        session.state == "blocked" and not session.waits_on_dialog and session.name not in store.SWEEP_SESSION_NAMES
        and asks_in(session.detail if session.provider == "codex" else last_message(session.session_id))
    )


def stopped_asking(session: Session) -> bool:
    """The session was stopped while it asked, as Cockpit's idle stop does after an hour (SPEC SESSION-15): its
    question stays open, and the user's answer resumes it."""
    return (
        session.state == "stopped" and session.name not in store.SWEEP_SESSION_NAMES
        and asks_in(session.detail if session.provider == "codex" else last_message(session.session_id))
    )


def asks_in(text: str) -> bool:
    """The message asks the user something: its last line of prose, or an item of a list that ends the message
    (such as numbered questions) or the line before that list, ends in a question mark, or a line says what input
    it needs. Code blocks and tables support the words around them and count as none. Markdown's own parse finds
    where each block starts, so a line belongs to a list item exactly when Markdown shows it in one."""
    if NEEDS_INPUT.search(text):
        return True
    blocks = [token for token in MARKDOWN.parse(text) if token.level == 0 and token.map is not None]
    # Code and tables support the words around them, such as the command a question asks to run.
    blocks = [block for block in blocks if block.type not in SUPPORTING] or blocks
    if not blocks:
        return False
    lines = text.splitlines()
    last = blocks[-1]
    if last.type in ("bullet_list_open", "ordered_list_open"):
        candidates = lines[last.map[0]:last.map[1]] + ([lines[blocks[-2].map[1] - 1]] if len(blocks) > 1 else [])  # ty: ignore[not-subscriptable]  # Blocks with no map were filtered out above.
    else:
        candidates = [lines[last.map[1] - 1]]  # ty: ignore[not-subscriptable]
    return any(line.rstrip(" *_`\")").endswith("?") for line in candidates)


def _open_questions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Open question steps still in the user's view; one the user handed to its session is that session's to close."""
    rows = db.rows(conn.execute(
        f"SELECT s.* FROM steps s WHERE {store.OPEN} AND s.handoff_message_id IS NULL AND s.need LIKE 'question:%'"
    ).fetchall())
    return [step for step in rows if NEED.match(step["need"])]


def _named_elsewhere(conn: sqlite3.Connection, step: dict[str, Any], session_id: str) -> bool:
    """Another step in the user's view that is no question step speaks for the session (`_covered`): it or the sweep
    put the question there, so the question step closes as it would not have been put. Question steps close none:
    one that closed another could itself close in the same poll, and the other come back without its fields."""
    return session_id in _covered(conn, step["id"], False)


def _stopped_at(step: dict[str, Any]) -> datetime | None:
    """When the question step's session stopped to ask. An agent may put a question step whose need ends in words, not
    a time: it has none, so it stays while its session asks, and every word of the user's on it answers it."""
    try:
        return rank.parse_time(NEED.match(step["need"]).group("stopped_at"))  # ty: ignore[unresolved-attribute]
    except ValueError:
        return None


def _spoken_on(conn: sqlite3.Connection, step: dict[str, Any]) -> tuple[bool, bool]:
    """Whether the user spoke on the question step before its session stopped to ask, as on a step they handed it
    (in their own words, or by Run or Allow, which Cockpit writes), which the question came back on (`_handed_to`);
    and whether the user wrote on it since, which answers the question."""
    stopped_at = _stopped_at(step)
    before = since = False
    for author, sent_at in conn.execute(
        "SELECT author, sent_at FROM messages WHERE step_id = ? AND (author = 'user' OR hands_off = 1)", (step["id"],)
    ):
        sent = rank.parse_time(sent_at)
        if sent is not None and stopped_at is not None and sent <= stopped_at:
            before = True
        elif author == "user":
            since = True
    return before, since


def _only_asks_since(conn: sqlite3.Connection, step: dict[str, Any], session_id: str) -> bool:
    """Since the session stopped to ask, it got Ask's questions and answers and nothing else (SPEC MSG-11): they
    leave its question open for the user, whatever it said after them. What counts is when a message reached the
    session, not when it was written: an answer may wait for the question that followed it. `sent_at` holds whole
    seconds, so the stop's second counts whole too."""
    since = (_stopped_at(step) or rank.parse_time(step["created_at"])).replace(microsecond=0)  # ty: ignore[unresolved-attribute]  # Steps always have a creation timestamp.
    got = [
        bool(from_ask) for sent_at, from_ask in conn.execute(
            "SELECT sent_at, id IN (SELECT sent_message_id FROM side_chats WHERE sent_message_id IS NOT NULL)"
            " FROM messages WHERE session_id = ? AND state IN ('sending', 'sent')",
            (session_id,),
        )
        if sent_at is None or rank.parse_time(sent_at) >= since  # ty: ignore[unsupported-operator]  # A recorded send time is a timestamp.
    ]
    return bool(got) and all(got)


def _close(conn: sqlite3.Connection, step: dict[str, Any], answered: bool) -> None:
    if answered:
        store.step_action(conn, step["id"], "done", {}, ACTOR)
    elif not step["snoozed_until"] and store.running_step(conn) != step["id"]:
        # Every poll weighs it again, so a question the user is on goes once they pause it.
        store.step_action(conn, step["id"], "withdraw", {}, ACTOR)


def _covered(conn: sqlite3.Connection, except_id: int | None, questions: bool) -> set[str]:
    """Sessions a step in the user's view other than step `except_id` speaks for: its own session and every session
    of its item; question steps count only when `questions`. A result to read speaks for none: a question its
    session asks later still becomes a step. The question's put and the weighing of an open question step both ask
    this, so the order in which a question and a covering step that is no question step arrive leaves the same steps
    open."""
    covered: set[str] = set()
    for session_id, item_sessions in conn.execute(
        f"SELECT s.session_id, i.sessions FROM steps s JOIN items i ON i.id = s.item_id"
        f" WHERE {store.OPEN} AND {store.ASKS} AND s.handoff_message_id IS NULL AND i.archived = 0 AND s.id IS NOT ?"
        " AND (? OR s.need NOT LIKE 'question:%')",
        (except_id, questions),
    ).fetchall():
        if session_id:
            covered.add(session_id)
        covered.update(json.loads(item_sessions))
    return covered


def _put(conn: sqlite3.Connection, session: Session) -> None:
    # One transaction, so a step that covers the session, or a hand-off, cannot land between the check and the put.
    # A covered session gets no item of its own either.
    with db.transaction(conn):
        if session.session_id in _covered(conn, None, True):
            return
        item_id = _item_id(conn, session)
        if item_id is None:
            log.info("session %s asks a question, but every item it is on is closed; no step", session.session_id)
            return
        fields = {
            "item_id": item_id,
            "need": f"question:{session.session_id}:{session.updated_at}",
            "session_id": session.session_id,
            "ask": _ask(conn, session),
            "why": _why(conn, session),
            "body": _quoted(session.detail if session.provider == "codex" else last_message(session.session_id)),
        }
        # The user handed the step of this question to an agent: it comes back with what that agent wrote.
        if conn.execute(
            f"SELECT 1 FROM steps s WHERE s.item_id = ? AND s.need = ? AND {store.OPEN} AND s.handoff_message_id IS NOT NULL",
            (fields["item_id"], fields["need"]),
        ).fetchone():
            return
        result = store.upsert_step(conn, fields, ACTOR)
    if result["skipped"] is None:
        log.info("put step %s for the question of session %s", result["step"]["id"], session.session_id)


def _handed_to(conn: sqlite3.Connection, session: Session) -> dict[str, Any] | None:
    """The step of the last hand-off that reached the session before it stopped, while the session still holds it: the
    question answers that hand-off. Once that step went back to the user, an older step the session holds stays."""
    return db.row(conn.execute(
        f"SELECT s.* FROM steps s WHERE {store.OPEN} AND s.handoff_message_id = ("
        " SELECT id FROM messages WHERE session_id = ? AND hands_off = 1 AND sent_at IS NOT NULL"
        " AND julianday(sent_at) <= julianday(?)"
        " ORDER BY julianday(sent_at) DESC, id DESC LIMIT 1)",
        (session.session_id, session.updated_at),
    ).fetchone())


def _ask_on(conn: sqlite3.Connection, step: dict[str, Any], session: Session) -> None:
    """The session's question goes on the step the user handed it, which goes back to the user ."""
    # One transaction, so a refused question leaves the step as it was and the next poll tries again. The user may
    # have taken the step back, or handed it on, since it was read: then it is no longer this session's to answer.
    with db.transaction(conn):
        holds = conn.execute("SELECT handoff_message_id FROM steps WHERE id = ?", (step["id"],)).fetchone()[0]
        if holds != step["handoff_message_id"]:
            log.info("step %s left session %s before its question went on it", step["id"], session.session_id)
            return
        if not _ask_in_place(conn, step, session):
            raise store.Invalid(f"step {step['id']}'s item already has a step for this question")
        store.step_action(conn, step["id"], "return", {}, ACTOR)
    log.info("put the question of session %s on step %s, handed to it, and returned it", session.session_id, step["id"])


def _ask_in_place(conn: sqlite3.Connection, step: dict[str, Any], session: Session) -> bool:
    """The step asks the session's question in place of what it asked, inside the caller's transaction; False when
    its item already has a step for that question. Every step that takes a question takes its need, so the next poll
    weighs it like any question step (a handed `decide:` step that kept its need was weighed by no poll and
    covered the session, so its next question never showed); a merge or approve step's action needs an answer that
    offers it, a result to read covers no session (`_covered`), and a finish step ranks first. The question
    replaces what the step offered (its answers, Run, Allow, tasks to close, draft, reply hint and details), which answered the earlier ask, and
    the step names the session that asks it, so the user's answer goes there."""
    need = f"question:{session.session_id}:{session.updated_at}"
    if need != step["need"]:
        if conn.execute("SELECT 1 FROM steps WHERE item_id = ? AND need = ?", (step["item_id"], need)).fetchone():
            return False
        conn.execute("UPDATE steps SET need = ? WHERE id = ?", (need, step["id"]))
    fields = {
        "item_id": step["item_id"],
        "need": need,
        "session_id": session.session_id,
        "ask": _ask(conn, session),
        "why": _why(conn, session),
        "body": _quoted(session.detail if session.provider == "codex" else last_message(session.session_id)),
        "details": [],
        "draft": "",
        "reply_hint": "",
        "command": None,
        "permission": None,
        "answers": [],
        # A question closes no tasks: the list the earlier ask offered goes with it.
        "closes": None,
    }
    store.upsert_step(conn, fields, ACTOR)
    return True


def _item_id(conn: sqlite3.Connection, session: Session) -> int | None:
    """The open item the session is primary on, else the first open one listing it, else its own item; None when its
    own item is closed too. A task the user completed or closed takes no new ask (SPEC CONTROL-27):"""
    row = conn.execute(
        "SELECT i.id FROM items i WHERE i.archived = 0 AND i.stage IS NOT 'done'"
        " AND EXISTS (SELECT 1 FROM json_each(i.sessions) WHERE json_each.value = ?)"
        " ORDER BY json_extract(i.sessions, '$[0]') = ? DESC, i.id LIMIT 1",
        (session.session_id, session.session_id),
    ).fetchone()
    if row is not None:
        return row[0]
    key = f"session:{session.session_id}"
    own = conn.execute("SELECT archived, stage FROM items WHERE key = ?", (key,)).fetchone()
    if own is not None and (own["archived"] or own["stage"] == "done"):
        return None
    fields = {"key": key, "title": session.name or f"Session {session.id}", "sessions": [session.session_id]}
    return store.upsert_item(conn, fields, ACTOR)["id"]


def _label(conn: sqlite3.Connection, session: Session) -> str:
    """The session's name in quotes, or "a session" when it has none or the name holds an id."""
    name = store.shown_session_names(conn, [session])[session.session_id]
    if name and not store.HEX_ID.search(name):
        return f'session "{name}"'
    return "a session"


def _ask(conn: sqlite3.Connection, session: Session) -> str:
    text = " ".join(session.needs.split())
    if not text or store.HEX_ID.search(text):
        text = f"Answer {_label(conn, session)}"
    return _clip(text[0].upper() + text[1:], store.TEXT_LIMITS["ask"])


def _why(conn: sqlite3.Connection, session: Session) -> str:
    label = _label(conn, session)
    why = f"{label[0].upper() + label[1:]} stopped to ask you this and waits for your answer."
    return why if len(why) <= store.TEXT_LIMITS["why"] else "A session stopped to ask you this and waits for your answer."


def _quoted(text: str) -> str:
    """The session's own words, quoted: shown in full and free of the body's limits (SPEC NEXT-24)."""
    return "\n".join(f"> {line}" if line.strip() else ">" for line in text.strip().splitlines())


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"
