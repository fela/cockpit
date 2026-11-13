"""A hand-off the user sent waits for Take back before it goes (SPEC MSG-8)."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cockpit import db, delivery, store
from tests.conftest import SESSION_A, World
from tests.test_api import agent_client, put_item, put_step, run_sql
from tests.test_delivery import live, message, send, stops_and_resumes


def hand_off(world: World) -> tuple[dict, dict]:
    put_item(world, sessions=[SESSION_A])
    step = put_step(world)["step"]
    response = world.client.post(f"/api/steps/{step['id']}/hand-off", json={"text": "Merge it"}, headers=world.user())
    assert response.status_code == 200, response.text
    return step, response.json()


def release(world: World, message_id: int) -> dict:
    response = world.client.post(f"/api/messages/{message_id}/release", headers=world.user())
    assert response.status_code == 200, response.text
    return response.json()


def take_back_due(world: World, now: datetime) -> list[int]:
    conn = db.connect(world.db_path)
    try:
        return delivery.take_back_due(conn, now)
    finally:
        conn.close()


def test_take_back_while_the_notice_shows_stops_the_hand_off_for_certain(
    world: World, tmp_path: Path, monkeypatch
) -> None:
    # The session's turn is over, so a released message would stop and resume it at once.
    stops_and_resumes(world, live("done", "idle", str(tmp_path)), monkeypatch)
    step, handed = hand_off(world)
    held = message(world, handed["id"])
    assert held["state"] == "queued" and held["waits_since"] is None
    until = datetime.fromisoformat(held["take_back_until"])
    assert timedelta(seconds=55) < until - datetime.now(UTC) <= store.TAKE_BACK_LIMIT
    assert world.runner.calls == []
    # A delivery that runs anyway, as a stray Retry or wake-up would, leaves it waiting.
    world.deliverer.send(handed["id"])
    assert message(world, handed["id"])["state"] == "queued" and world.runner.calls == []

    returned = world.client.post(f"/api/steps/{step['id']}/return", json={}, headers=world.user())
    assert returned.status_code == 200, returned.text
    unsent = message(world, handed["id"])
    assert (unsent["state"], unsent["dismissed_by"], unsent["take_back_until"]) == ("failed", "user", None)
    assert unsent["detail"] == "Not sent: Sam took the step back first."
    assert (returned.json()["stopped"], returned.json()["unsent"]) == (True, "Merge it")  # back in the reply box
    # The notice goes after Take back, and its time limit passes: nothing reaches the session.
    assert release(world, handed["id"])["state"] == "failed"
    assert take_back_due(world, datetime.now(UTC) + timedelta(minutes=5)) == []
    assert world.runner.calls == []


def test_take_back_says_where_the_unsent_words_were_going(world: World) -> None:
    # The page writes the box to that place again, also after a reload or in another tab.
    put_item(world, sessions=[SESSION_A])
    step = put_step(world)["step"]
    target = {"new_cwd": str(world.checkout), "provider": "claude", "model": "claude-sonnet-5-5", "effort": "low"}
    handed = world.client.post(f"/api/steps/{step['id']}/hand-off", json={"text": "Merge it"} | target, headers=world.user())
    assert handed.status_code == 200, handed.text
    returned = world.client.post(f"/api/steps/{step['id']}/return", json={}, headers=world.user()).json()
    assert returned["unsent_to"] == {
        "session_id": None, "new_cwd": str(world.checkout), "provider": "claude",
        "options": {"model": "claude-sonnet-5-5", "effort": "low", "account": None},
    }


def test_the_message_goes_once_the_notice_is_gone(world: World, tmp_path: Path, monkeypatch) -> None:
    stops_and_resumes(world, live("done", "idle", str(tmp_path)), monkeypatch)
    _, handed = hand_off(world)
    assert agent_client(world).post(f"/api/messages/{handed['id']}/release", headers=world.agent()).status_code == 403
    release(world, handed["id"])
    sent = message(world, handed["id"])
    assert (sent["state"], sent["via"], sent["take_back_until"]) == ("sent", "resume", None)
    assert [args[1] for args, _ in world.runner.calls] == ["stop", "--bg"]
    # A second release, from a second tab, sends nothing more.
    release(world, handed["id"])
    assert len(world.runner.calls) == 2
    assert world.client.post("/api/messages/999/release", headers=world.user()).status_code == 404


def test_a_hold_that_comes_while_the_wake_loop_releases_keeps_the_hand_off_waiting(
    world: World, tmp_path: Path, monkeypatch
) -> None:
    """the user writes more (MSG-10) between the wake loop's read of the due hand-offs and its release."""
    stops_and_resumes(world, live("done", "idle", str(tmp_path)), monkeypatch)
    _, handed = hand_off(world)
    conn = db.connect(world.db_path)
    try:
        conn.execute("UPDATE messages SET take_back_until = '2026-01-04T09:00:00Z' WHERE id = ?", (handed["id"],))
    finally:
        conn.close()

    class HeldMeanwhile(datetime):
        @classmethod
        def fromisoformat(cls, text: str) -> datetime:
            conn = db.connect(world.db_path)
            try:
                assert store.hold_message(conn, handed["id"])
            finally:
                conn.close()
            return datetime.fromisoformat(text)

    monkeypatch.setattr(delivery, "datetime", HeldMeanwhile)
    assert take_back_due(world, datetime.now(UTC)) == []
    assert message(world, handed["id"])["take_back_until"] is not None


def test_the_wake_loop_sends_a_hand_off_whose_page_closed_while_the_notice_showed(
    world: World, tmp_path: Path, monkeypatch
) -> None:
    stops_and_resumes(world, live("done", "idle", str(tmp_path)), monkeypatch)
    _, handed = hand_off(world)
    assert take_back_due(world, datetime.now(UTC)) == []
    assert take_back_due(world, datetime.now(UTC) + store.TAKE_BACK_LIMIT) == [handed["id"]]
    assert message(world, handed["id"])["take_back_until"] is None
    world.deliverer.send(handed["id"])
    assert message(world, handed["id"])["state"] == "sent"


def test_a_restart_keeps_a_hand_off_that_waits_for_take_back(world: World) -> None:
    _, handed = hand_off(world)
    conn = db.connect(world.db_path)
    try:
        assert delivery.mark_interrupted(conn) == 0
    finally:
        conn.close()
    kept = message(world, handed["id"])
    assert kept["state"] == "queued" and kept["take_back_until"] is not None


def test_send_now_skips_the_wait_for_take_back(world: World, tmp_path: Path, monkeypatch) -> None:
    # Mid-turn: Send now stops the session at once, as it does for a message that waits for the turn.
    stops_and_resumes(world, live("working", "busy", str(tmp_path)), monkeypatch)
    _, handed = hand_off(world)
    response = world.client.post(f"/api/messages/{handed['id']}/send-now", headers=world.user())
    assert response.status_code == 200, response.text
    sent = message(world, handed["id"])
    assert sent["state"] == "sent" and sent["take_back_until"] is None
    assert sent["detail"] == "stopped the session at once and resumed it with the message as its prompt"


def follow_up(world: World, message_id: int, text: str):
    return world.client.post(f"/api/messages/{message_id}/follow-up", json={"text": text}, headers=world.user())


def test_a_follow_up_while_the_notice_shows_joins_the_first_message(world: World, tmp_path: Path, monkeypatch) -> None:
    # SPEC MSG-10: the session reads the first message and the follow-up together, in order.
    stops_and_resumes(world, live("done", "idle", str(tmp_path)), monkeypatch)
    _, handed = hand_off(world)
    added = follow_up(world, handed["id"], "  and delete the branch  ")
    assert added.status_code == 200, added.text
    assert (added.json()["id"], added.json()["joined"]) == (handed["id"], True)
    assert world.runner.calls == []
    release(world, handed["id"])
    sent = message(world, handed["id"])
    assert (sent["state"], sent["text"]) == ("sent", "Merge it\n\nand delete the branch")
    assert [args[1] for args, _ in world.runner.calls] == ["stop", "--bg"]


def test_a_follow_up_that_fails_part_way_leaves_the_first_message_as_it_was(world: World, monkeypatch) -> None:
    # The page sends the action again after a failure, so a half-made follow-up would join twice.
    _, handed = hand_off(world)

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store.patterns, "record", locked)
    conn = db.connect(world.db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            store.add_follow_up(conn, handed["id"], "and delete the branch", world.checkout.parent)
    finally:
        conn.close()
    assert message(world, handed["id"])["text"] == "Merge it"


def test_a_follow_up_after_the_first_went_goes_after_it_to_the_same_session(
    world: World, tmp_path: Path, monkeypatch
) -> None:
    stops_and_resumes(world, live("done", "idle", str(tmp_path)), monkeypatch)
    step, handed = hand_off(world)
    release(world, handed["id"])
    added = follow_up(world, handed["id"], "and delete the branch").json()
    assert added["joined"] is False and added["id"] != handed["id"]
    second = message(world, added["id"])
    assert (second["session_id"], second["step_id"], second["text"], second["author"]) == (
        SESSION_A, step["id"], "and delete the branch", "user")
    assert message(world, handed["id"])["text"] == "Merge it"


def test_a_follow_up_needs_words_the_users_own_message_and_one_not_taken_back(world: World) -> None:
    step, handed = hand_off(world)
    assert follow_up(world, handed["id"], "   ").status_code == 400
    agent = agent_client(world).post(f"/api/messages/{handed['id']}/follow-up", json={"text": "x"}, headers=world.agent())
    assert agent.status_code == 403
    assert world.client.post(f"/api/steps/{step['id']}/return", json={}, headers=world.user()).status_code == 200
    taken_back = follow_up(world, handed["id"], "and delete the branch")
    assert taken_back.status_code == 409 and "was not sent" in taken_back.json()["detail"]
    assert follow_up(world, 999, "x").status_code == 404


def test_a_follow_up_that_joins_while_delivery_waits_for_the_session_goes_with_the_first(
    world: World, tmp_path: Path, monkeypatch
) -> None:
    # Delivery read the first message, then waited for the session's lock; the follow-up joined meanwhile.
    stops_and_resumes(world, live("done", "idle", str(tmp_path)), monkeypatch)
    _, handed = hand_off(world)
    run_sql(world, "UPDATE messages SET take_back_until = NULL WHERE id = ?", (handed["id"],))
    lock = world.deliverer._lock

    def joins_first(key: str):
        conn = db.connect(world.db_path)
        try:
            store.add_follow_up(conn, handed["id"], "and delete the branch", world.checkout.parent)
        finally:
            conn.close()
        monkeypatch.setattr(world.deliverer, "_lock", lock)
        return lock(key)

    monkeypatch.setattr(world.deliverer, "_lock", joins_first)
    world.deliverer.send(handed["id"])
    sent = message(world, handed["id"])
    assert (sent["state"], sent["text"]) == ("sent", "Merge it\n\nand delete the branch")
    prompt = world.runner.calls[-1][0]
    assert "and delete the branch" in " ".join(prompt)


def test_a_follow_up_to_a_new_session_that_never_started_says_so(world: World) -> None:
    # The first message was to start a new session and failed, so no session is there to follow.
    _, handed = hand_off(world)
    run_sql(world, "UPDATE messages SET state = 'failed', session_id = NULL, new_cwd = ?, detail = 'claude exited 1'"
            " WHERE id = ?", ("/home/sam/dev/app", handed["id"]))
    added = follow_up(world, handed["id"], "and delete the branch")
    assert added.status_code == 409 and "has not reached a session: claude exited 1" in added.json()["detail"]


def test_writing_more_keeps_the_message_waiting_for_take_back(world: World) -> None:
    # SPEC MSG-10: the page holds its notice while the user writes more, and the server holds the message with it.
    _, handed = hand_off(world)
    run_sql(world, "UPDATE messages SET take_back_until = ? WHERE id = ?",
            ((datetime.now(UTC) + timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ"), handed["id"]))
    held = world.client.post(f"/api/messages/{handed['id']}/hold", headers=world.user())
    assert held.status_code == 200, held.text
    until = datetime.fromisoformat(held.json()["take_back_until"])
    assert until - datetime.now(UTC) > timedelta(seconds=55)
    assert agent_client(world).post(f"/api/messages/{handed['id']}/hold", headers=world.agent()).status_code == 403
    # A released message stays released.
    release(world, handed["id"])
    assert world.client.post(f"/api/messages/{handed['id']}/hold", headers=world.user()).json()["take_back_until"] is None


def test_a_follow_up_joined_to_a_message_that_waited_goes_with_the_batch(
    world: World, tmp_path: Path, monkeypatch
) -> None:
    # Two of the user's messages wait for the session's turn (SPEC MSG-7); a follow-up joins the second after
    # delivery claimed the first and just before it claims the others. The batch reads it once claimed. (Listing
    # and claiming the others is one transaction, so a follow-up lands before it or waits for it.)
    busy = live("working", "busy", str(tmp_path))
    stops_and_resumes(world, busy, monkeypatch)
    first = send(world, {"session_id": SESSION_A}, "First")
    second = send(world, {"session_id": SESSION_A}, "Second")
    world.cache.sessions = [replace(busy, state="done", status="idle")]
    claim = delivery._claim_waiting

    class JoinsBeforeTheClaim:
        """The delivery's connection: the follow-up joins the second message just before the claim's transaction."""

        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def execute(self, sql, params=()):
            if sql == "BEGIN IMMEDIATE":
                follower = db.connect(world.db_path)
                try:
                    store.add_follow_up(follower, second["id"], "and the second's follow-up", world.checkout.parent)
                finally:
                    follower.close()
            return self.conn.execute(sql, params)

    monkeypatch.setattr(delivery, "_claim_waiting", lambda conn, message: claim(JoinsBeforeTheClaim(conn), message))
    world.deliverer.send(first["id"])
    assert [message(world, each["id"])["state"] for each in (first, second)] == ["sent", "sent"]
    assert "and the second's follow-up" in world.runner.calls[-1][0][-1]


def test_a_follow_up_waits_for_the_new_session_its_first_message_is_starting(world: World, monkeypatch) -> None:
    # Delivery claimed the first message to start a new session; the follow-up goes to that session once it is known,
    # not refused
    _, handed = hand_off(world)
    cwd = "/home/sam/dev/app"
    run_sql(world, "UPDATE messages SET state = 'sending', session_id = NULL, new_cwd = ?, take_back_until = NULL"
            " WHERE id = ?", (cwd, handed["id"]))
    waited = []

    def started(at: str) -> None:
        waited.append(at)
        run_sql(world, "UPDATE messages SET state = 'sent', session_id = ? WHERE id = ?", (SESSION_A, handed["id"]))

    monkeypatch.setattr(world.deliverer, "wait_for_start", started)
    monkeypatch.setattr(world.deliverer, "send", lambda message_id: None)
    added = follow_up(world, handed["id"], "and delete the branch")
    assert added.status_code == 200, added.text
    assert waited == [cwd]
    assert (added.json()["joined"], message(world, added.json()["id"])["session_id"]) == (False, SESSION_A)


def test_a_follow_up_goes_to_the_new_session_that_started_while_it_was_refused(world: World, monkeypatch) -> None:
    # The new session started between the follow-up's refusal and the route's second look: it goes there.
    _, handed = hand_off(world)
    run_sql(world, "UPDATE messages SET state = 'sending', session_id = NULL, new_cwd = ?, take_back_until = NULL"
            " WHERE id = ?", ("/home/sam/dev/app", handed["id"]))
    add = store.add_follow_up
    calls = []

    def refused_then_started(conn, message_id, text, checkouts_dir):
        calls.append(message_id)
        if len(calls) == 1:
            try:
                return add(conn, message_id, text, checkouts_dir)
            finally:
                run_sql(world, "UPDATE messages SET state = 'sent', session_id = ? WHERE id = ?", (SESSION_A, handed["id"]))
        return add(conn, message_id, text, checkouts_dir)

    monkeypatch.setattr(store, "add_follow_up", refused_then_started)
    monkeypatch.setattr(world.deliverer, "wait_for_start", lambda at: calls.append("waited"))
    monkeypatch.setattr(world.deliverer, "send", lambda message_id: None)
    added = follow_up(world, handed["id"], "and delete the branch")
    assert added.status_code == 200, added.text
    assert calls == [handed["id"], handed["id"]]
    assert message(world, added.json()["id"])["session_id"] == SESSION_A


def test_take_back_after_the_message_went_gives_no_words_back(world: World, tmp_path: Path, monkeypatch) -> None:
    """: only words that never reached the session come back to the reply box."""
    stops_and_resumes(world, live("done", "idle", str(tmp_path)), monkeypatch)
    step, handed = hand_off(world)
    release(world, handed["id"])
    assert message(world, handed["id"])["state"] == "sent"
    returned = world.client.post(f"/api/steps/{step['id']}/return", json={}, headers=world.user())
    assert returned.status_code == 200, returned.text
    assert (returned.json()["stopped"], returned.json()["unsent"]) == (False, None)
