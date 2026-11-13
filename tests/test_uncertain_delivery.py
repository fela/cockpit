"""A message whose delivery Cockpit could not confirm (SPEC MSG-5) counts as sent once its session's transcript shows
it arrived: Cockpit looks for an hour after its last delivery attempt on its own, and once more whenever the step it
handed over is closed."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cockpit import db, delivery, sessions, store
from tests.conftest import SESSION_A, World, make_session
from tests.test_api import hand_off
from tests.test_complete_task import _agent, _item, _step
from tests.test_error_stops import api_error, ok, query, sync, transcript
from tests.test_only_user_closes import _open_needs, _put, _put_finish, _sql

UNCERTAIN = "Delivery uncertain: the session did not show it within 30 seconds. Check the session before retrying."


def arrived(text: str, at: datetime, author: str = "user") -> dict[str, Any]:
    """The prompt entry a delivered message leaves in its session's transcript."""
    return {"type": "user", "timestamp": at.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "message": {"role": "user", "content": delivery.prefixed(author, text)}}


def _iso(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _uncertain(world: World, message_id: int, created: datetime, failed: datetime, dismissed: bool) -> None:
    """The message, written at `created`, failed as uncertain at `failed`, as delivery records it."""
    _sql(world, "UPDATE messages SET state = 'failed', detail = ?, created_at = ?, take_back_until = NULL,"
                " dismissed_at = ?, dismissed_by = ?, dismiss_reason = ? WHERE id = ?",
         UNCERTAIN, _iso(created), _iso(created) if dismissed else None, "user" if dismissed else None,
         "already handled" if dismissed else None, message_id)
    _sql(world, "INSERT INTO events (at, actor, kind, data) VALUES (?, 'cockpit', 'message.failed', ?)",
         _iso(failed), json.dumps({"message_id": message_id, "detail": UNCERTAIN}))


def _message(world: World, text: str, created: datetime) -> int:
    """A message the user wrote to session A that Cockpit sent by resuming it."""
    conn = db.connect(world.db_path)
    try:
        with db.transaction(conn):
            return conn.execute(
                "INSERT INTO messages (author, session_id, text, state, via, created_at) VALUES ('user', ?, ?, 'sending',"
                " 'resume', ?)", (SESSION_A, text, _iso(created)),
            ).lastrowid
    finally:
        conn.close()


def _confirm(world: World, now: datetime) -> None:
    conn = db.connect(world.db_path)
    try:
        store.confirm_uncertain(conn, now)
    finally:
        conn.close()


def test_received_finds_the_message_after_its_send_time_only(transcripts: Path) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    sent = delivery.prefixed("user", "Add it")
    transcript(transcripts, SESSION_A, arrived("Add it", now - timedelta(minutes=5)), ok(now - timedelta(minutes=4)),
               arrived("Something else", now - timedelta(minutes=1)))
    assert sessions.received(SESSION_A, sent, _iso(now - timedelta(minutes=10))) == _iso(now - timedelta(minutes=5))
    # A prompt from before the message was written is an earlier copy of the same words.
    assert sessions.received(SESSION_A, sent, _iso(now - timedelta(minutes=3))) is None
    assert sessions.received(SESSION_A, delivery.prefixed("user", "Never sent"), _iso(now - timedelta(hours=1))) is None
    # The same words from an agent carry another sender line.
    assert sessions.received(SESSION_A, delivery.prefixed("agent:sweep", "Add it"), _iso(now - timedelta(hours=1))) is None


def test_received_takes_only_the_whole_message(world: World, transcripts: Path) -> None:
    """A longer message that begins with the same words is another message; one in a batch, or followed by what
    Cockpit adds, is the message."""
    now = datetime.now(UTC).replace(microsecond=0)
    since = _iso(now - timedelta(hours=1))
    transcript(transcripts, SESSION_A, arrived("Add it tomorrow", now - timedelta(minutes=5)))
    assert sessions.received(SESSION_A, delivery.prefixed("user", "Add it"), since) is None
    quoting = "Look at this prompt:\n\n" + delivery.prefixed("user", "Add it")
    transcript(transcripts, SESSION_A, arrived(quoting, now - timedelta(minutes=5)))
    assert sessions.received(SESSION_A, delivery.prefixed("user", "Add it"), since) is None
    batch = sessions.COCKPIT_BATCH_SEPARATOR.join([delivery.prefixed("user", "First"), delivery.prefixed("user", "Add it")])
    transcript(transcripts, SESSION_A, {**arrived("", now - timedelta(minutes=4)),
                                        "message": {"role": "user", "content": batch}})
    assert sessions.received(SESSION_A, delivery.prefixed("user", "Add it"), since) == _iso(now - timedelta(minutes=4))

    # A message that holds a batch separator itself, and one Cockpit stopped the session for.
    ruled = "Review these:\n\n* * *\n\nThen ship it"
    transcript(transcripts, SESSION_A, arrived(ruled, now - timedelta(minutes=3)))
    assert sessions.received(SESSION_A, delivery.prefixed("user", ruled), since) == _iso(now - timedelta(minutes=3))
    transcript(transcripts, SESSION_A, arrived("Ship it\n\n" + delivery.AFTER_TURN, now - timedelta(minutes=2)))
    assert sessions.received(SESSION_A, delivery.prefixed("user", "Ship it"), since) == _iso(now - timedelta(minutes=2))


def test_an_uncertain_message_its_transcript_shows_is_recorded_as_sent(world: World, transcripts: Path) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    shown, set_aside, missing, old = (_message(world, f"Message {n}", now) for n in range(4))
    for message_id in (shown, missing):
        _uncertain(world, message_id, now - timedelta(minutes=10), now - timedelta(minutes=10), dismissed=False)
    _uncertain(world, set_aside, now - timedelta(minutes=10), now - timedelta(minutes=10), dismissed=True)
    _uncertain(world, old, now - timedelta(hours=2), now - timedelta(hours=2), dismissed=False)
    transcript(transcripts, SESSION_A, arrived("Message 3", now - timedelta(minutes=110)),
               arrived("Message 0", now - timedelta(minutes=9)), arrived("Message 1", now - timedelta(minutes=8)))
    _confirm(world, now)
    rows = {row["id"]: row for row in query(world, "SELECT * FROM messages")}
    for message_id, at in ((shown, now - timedelta(minutes=9)), (set_aside, now - timedelta(minutes=8))):
        row = rows[message_id]
        assert (row["state"], row["sent_at"], row["dismissed_at"], row["dismissed_by"]) == ("sent", _iso(at), None, None)
        assert row["detail"] == f"Confirmed later: the session's transcript shows it arrived at {_iso(at)}."
    # Not in the transcript, or failed longer ago than the hour Cockpit keeps looking on its own: still failed.
    assert [rows[message_id]["state"] for message_id in (missing, old)] == ["failed", "failed"]
    confirmed = query(world, "SELECT data FROM events WHERE kind = 'message.sent' AND data LIKE '%confirmed%'")
    assert sorted(row["data"]["message_id"] for row in confirmed) == [shown, set_aside]


def test_a_retried_message_gets_its_own_hour(world: World, transcripts: Path) -> None:
    """A message written two hours ago and retried ten minutes ago is looked for from its retry."""
    now = datetime.now(UTC).replace(microsecond=0)
    retried = _message(world, "Retried", now)
    _uncertain(world, retried, now - timedelta(hours=2), now - timedelta(minutes=10), dismissed=False)
    transcript(transcripts, SESSION_A, arrived("Retried", now - timedelta(minutes=9)))
    _confirm(world, now)
    [row] = query(world, "SELECT state FROM messages WHERE id = ?", retried)
    assert row == {"state": "sent"}


def test_a_handed_step_whose_message_shows_in_the_transcript_closes(world: World, transcripts: Path) -> None:
    """A delivered message marked uncertain: its session could not close the step."""
    now = datetime.now(UTC).replace(microsecond=0)
    _item(world)
    step = _step(world, "try:prod", [])
    assert _put(world, stage="prod", waiting_on=[])["finish_step"] is None
    message = hand_off(world, step["id"], text="Add it")
    _uncertain(world, message["id"], now - timedelta(days=1), now - timedelta(days=1), dismissed=True)
    conn = db.connect(world.db_path)
    try:
        delivered = delivery.compose(conn, store.get_message(conn, message["id"]))
    finally:
        conn.close()
    transcript(transcripts, SESSION_A, ok(now - timedelta(days=2)))
    refused = _agent(world).post(f"/api/steps/{step['id']}/done", json={}, headers=world.agent())
    assert refused.status_code == 400 and "has not confirmed" in refused.json()["detail"]
    assert _put_finish(world, session_id=SESSION_A).status_code == 409
    assert _open_needs(world) == ["try:prod"]
    # The session got it after all, with what Cockpit adds to it, long after the hour Cockpit looks on its own.
    transcript(transcripts, SESSION_A, {**arrived("", now - timedelta(days=1) + timedelta(seconds=40)),
                                        "message": {"role": "user", "content": delivered}})
    assert _put_finish(world, session_id=SESSION_A).json()["closed_handed"] == step["id"]
    [row] = query(world, "SELECT state, dismissed_at FROM messages WHERE id = ?", message["id"])
    assert row == {"state": "sent", "dismissed_at": None}


def test_a_handed_step_closes_by_done_once_its_message_shows(world: World, transcripts: Path) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    _item(world)
    step = _step(world, "try:prod", [])
    message = hand_off(world, step["id"], text="Ship it")
    _uncertain(world, message["id"], now - timedelta(minutes=5), now - timedelta(minutes=5), dismissed=False)
    transcript(transcripts, SESSION_A, arrived("Ship it", now - timedelta(minutes=4)))
    closed = _agent(world).post(f"/api/steps/{step['id']}/done", json={"note": "shipped"}, headers=world.agent())
    assert closed.status_code == 200, closed.text


def test_an_uncertain_resend_waits_for_the_transcript_before_it_is_judged(world: World, transcripts: Path) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    stopped = now - timedelta(hours=2)
    world.cache.sessions = [make_session(SESSION_A, pid=None, state="stopped", account="work")]
    transcript(transcripts, SESSION_A, ok(stopped - timedelta(minutes=3)), api_error(stopped))
    [resend] = sync(world, now)
    _uncertain(world, resend, now, now, dismissed=False)
    assert sync(world, now + timedelta(minutes=10)) == []
    assert query(world, "SELECT outcome FROM error_stops") == [{"outcome": ""}]
    # Past the hour with nothing in the transcript, it counts as not delivered.
    sync(world, now + store.CONFIRM_WINDOW + timedelta(minutes=1))
    [row] = query(world, "SELECT outcome, tried FROM error_stops")
    assert row["outcome"] == "failed" and row["tried"].startswith("Cockpit's message could not be delivered: Delivery")
