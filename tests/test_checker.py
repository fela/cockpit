"""Auto mode's checker gives no verdict: Cockpit asks the session to try again, then shows one card (SPEC PERM-32,
checker.py)."""

from __future__ import annotations

from datetime import UTC, datetime

from cockpit import checker, cli, db, explain
from tests.conftest import SESSION_A, SESSION_B, World
from tests.test_api import agent_client
from tests.test_permission_requests import state

PUSH = {"command": "git push origin feature", "description": "Push the feature branch"}
REVIEW = {"description": "Start a privacy review", "prompt": "Review the outgoing changes."}


def refuse(
    world: World, tool_input: dict, session_id: str = SESSION_A, tool_name: str = "Bash",
    reason: str = checker.NO_VERDICT,
) -> dict:
    """Auto mode refuses the call, by default because its checker gave no verdict."""
    body = {
        "kind": "denied", "session_id": session_id, "cwd": "/home/sam/dev/app", "tool_name": tool_name,
        "tool_input": tool_input, "tool_use_id": "toolu_1", "reason": reason,
    }
    response = agent_client(world).post("/api/permissions/requests", json=body, headers=world.agent("permission-hook"))
    assert response.status_code == 200, response.text
    return response.json()


def later(world: World, minutes: int) -> None:
    """`minutes` pass: the refusals and Cockpit's messages so far are that much older."""
    conn = db.connect(world.db_path)
    try:
        shift = f"-{minutes} minutes"
        with db.transaction(conn):
            conn.execute(
                "UPDATE permission_requests SET created_at = strftime('%Y-%m-%dT%H:%M:%SZ', created_at, ?),"
                " last_at = strftime('%Y-%m-%dT%H:%M:%SZ', last_at, ?),"
                " no_verdict_since = strftime('%Y-%m-%dT%H:%M:%SZ', no_verdict_since, ?)",
                (shift, shift, shift),
            )
            conn.execute(
                "UPDATE messages SET created_at = strftime('%Y-%m-%dT%H:%M:%SZ', created_at, ?),"
                " sent_at = strftime('%Y-%m-%dT%H:%M:%SZ', sent_at, ?)",
                (shift, shift),
            )
    finally:
        conn.close()


def sync(world: World) -> list[int]:
    conn = db.connect(world.db_path)
    try:
        return checker.sync(conn, datetime.now(UTC), world.checkout.parent)
    finally:
        conn.close()


def tries(world: World) -> list[dict]:
    """Cockpit's messages asking a session to try again."""
    conn = db.connect(world.db_path)
    try:
        return db.rows(conn.execute(
            "SELECT * FROM messages WHERE author = 'cockpit' AND text LIKE ? ORDER BY id", (f"{checker.RETRY_PREFIX}%",)
        ).fetchall())
    finally:
        conn.close()


def delivered(world: World, state: str = "sent") -> None:
    """Cockpit's messages so far reached their sessions, or with `state` "failed", did not."""
    conn = db.connect(world.db_path)
    try:
        with db.transaction(conn):
            conn.execute(
                "UPDATE messages SET state = ?, sent_at = CASE WHEN ? = 'sent' THEN created_at END"
                " WHERE state NOT IN ('sent', ?)",
                (state, state, state),
            )
    finally:
        conn.close()


def retry(world: World) -> list[int]:
    """The user presses Try again on the outage card."""
    response = world.client.post("/api/permissions/checker/retry", headers=world.user())
    assert response.status_code == 200, response.text
    return response.json()["messages"]


def test_cockpit_holds_the_refusal_and_asks_the_session_to_try_again(world: World) -> None:
    request = refuse(world, PUSH)
    page = state(world)
    # Off the page and out of the Blocked count, while Cockpit waits to ask the session again.
    assert [card["id"] for card in page["held_requests"]] == [request["id"]]
    assert page["permission_requests"] == page["checker_refusals"] == [] and page["counts"]["blocked"] == 0
    # The session still finds its call in its own list.
    assert f"request {request['id']}," in cli.permission_list(page, SESSION_A)
    later(world, 1)
    assert sync(world) == []
    later(world, 1)
    [message_id] = sync(world)
    [message] = tries(world)
    assert message["id"] == message_id and message["session_id"] == SESSION_A
    assert "on this call" in message["text"] and "- Push the feature branch" in message["text"]
    assert "Try it again now, unchanged, unless you already made it again and it ran." in message["text"]
    # Until the message reaches the session, Cockpit sends no other and the refusal stays held.
    later(world, 30)
    assert sync(world) == []
    assert [card["id"] for card in state(world)["held_requests"]] == [request["id"]]
    # Once it did and auto mode has not refused the call since, the card folds away with the calls the sessions got
    # past, saying when Cockpit asked.
    delivered(world)
    page = state(world)
    [card] = page["moved_past"]
    assert card["id"] == request["id"] and card["asked_again_at"] == tries(world)[0]["sent_at"]
    assert page["held_requests"] == [] and page["counts"]["blocked"] == 0
    assert sync(world) == []


def test_refusals_after_the_last_try_share_one_card(world: World) -> None:
    first = refuse(world, PUSH)
    second = refuse(world, REVIEW, SESSION_A, "Agent")
    for delay in checker.RETRY_DELAYS:
        later(world, int(delay.total_seconds() // 60))
        assert len(sync(world)) == 1
        assert {card["id"] for card in state(world)["held_requests"]} == {first["id"], second["id"]}
        delivered(world)
        # The session tried both again, and the checker still gave no verdict.
        later(world, 1)
        refuse(world, PUSH)
        refuse(world, REVIEW, SESSION_A, "Agent")
    [*_, last] = tries(world)
    assert "on these 2 calls" in last["text"] and "- Start a privacy review" in last["text"]
    page = state(world)
    # Both on one card, which counts once under Blocked, with no card of their own.
    assert {card["id"] for card in page["checker_refusals"]} == {first["id"], second["id"]}
    assert page["permission_requests"] == page["held_requests"] == [] and page["counts"]["blocked"] == 1
    assert sync(world) == []
    # Try again asks the session once more, at once, and the calls fold away.
    assert len(retry(world)) == 1 and len(tries(world)) == len(checker.RETRY_DELAYS) + 1
    delivered(world)
    page = state(world)
    assert page["checker_refusals"] == [] and {card["id"] for card in page["moved_past"]} == {first["id"], second["id"]}


def test_after_the_retry_window_the_refusal_shows_without_more_tries(world: World) -> None:
    request = refuse(world, PUSH)
    later(world, int(checker.RETRY_WINDOW.total_seconds() // 60) + 1)
    refuse(world, PUSH)
    page = state(world)
    assert [card["id"] for card in page["checker_refusals"]] == [request["id"]]
    # Past the window, Cockpit no longer asks on its own.
    assert sync(world) == []


def test_each_session_gets_its_own_try_and_other_refusals_keep_their_cards(world: World) -> None:
    refuse(world, PUSH, SESSION_A)
    refuse(world, PUSH, SESSION_B)
    body = {
        "kind": "denied", "session_id": SESSION_A, "cwd": "/home/sam/dev/app", "tool_name": "Bash",
        "tool_input": {"command": "rm -rf build"}, "tool_use_id": "toolu_2", "reason": "[Irreversible Local Destruction]",
    }
    judged = agent_client(world).post("/api/permissions/requests", json=body, headers=world.agent("permission-hook")).json()
    page = state(world)
    assert [card["id"] for card in page["permission_requests"]] == [judged["id"]] and page["counts"]["blocked"] == 1
    later(world, 2)
    assert len(sync(world)) == 2
    assert sorted(message["session_id"] for message in tries(world)) == sorted([SESSION_A, SESSION_B])


def test_a_call_refused_again_before_the_message_was_marked_sent_is_not_taken_as_got_past(world: World) -> None:
    request = refuse(world, PUSH)
    later(world, 2)
    assert len(sync(world)) == 1
    # The session reads the message and makes the call again at once, before delivery records that it was sent.
    refuse(world, PUSH)
    delivered(world)
    page = state(world)
    assert page["moved_past"] == [] and [card["id"] for card in page["held_requests"]] == [request["id"]]
    later(world, 5)
    assert len(sync(world)) == 1 and len(tries(world)) == 2


def test_a_call_the_transcript_shows_the_session_got_past_is_not_tried_again(world: World) -> None:
    request = refuse(world, PUSH)
    conn = db.connect(world.db_path)
    try:
        with db.transaction(conn):
            explain.save(conn, request["id"], {"heading": "Push the branch", "moved_on": "It pushed alone.", "count": 1})
    finally:
        conn.close()
    later(world, 2)
    assert sync(world) == [] and tries(world) == []
    page = state(world)
    assert [card["id"] for card in page["moved_past"]] == [request["id"]] and page["held_requests"] == []


def test_a_failed_try_for_one_call_leaves_the_call_the_session_got_past_folded(world: World) -> None:
    first = refuse(world, PUSH)
    second = refuse(world, REVIEW, SESSION_A, "Agent")
    later(world, 2)
    assert len(sync(world)) == 1
    delivered(world)
    # The session got past the push; the review is refused again.
    later(world, 1)
    refuse(world, REVIEW, SESSION_A, "Agent")
    later(world, 5)
    assert len(sync(world)) == 1
    assert "on this call" in tries(world)[-1]["text"] and "- Start a privacy review" in tries(world)[-1]["text"]
    delivered(world, "failed")
    page = state(world)
    assert [card["id"] for card in page["moved_past"]] == [first["id"]]
    assert [card["id"] for card in page["held_requests"]] == [second["id"]]
    later(world, 10)
    assert len(sync(world)) == 1 and "on this call" in tries(world)[-1]["text"]


def test_a_try_still_waiting_for_delivery_after_the_window_shows_the_card(world: World) -> None:
    request = refuse(world, PUSH)
    later(world, 2)
    assert len(sync(world)) == 1
    later(world, int(checker.RETRY_WINDOW.total_seconds() // 60))
    page = state(world)
    assert [card["id"] for card in page["checker_refusals"]] == [request["id"]] and page["held_requests"] == []
    # The message still waits for the session, so Try again queues no second one.
    assert retry(world) == [] and sync(world) == [] and len(tries(world)) == 1


def test_try_again_twice_queues_one_message(world: World) -> None:
    refuse(world, PUSH)
    later(world, int(checker.RETRY_WINDOW.total_seconds() // 60) + 1)
    refuse(world, PUSH)
    conn = db.connect(world.db_path)
    try:
        # The second press reads the tries in its own write transaction, after the first queued its message.
        assert len(checker.retry_now(conn, datetime.now(UTC), world.checkout.parent)) == 1
        assert checker.retry_now(conn, datetime.now(UTC), world.checkout.parent) == []
    finally:
        conn.close()
    assert len(tries(world)) == 1


def test_a_call_refused_for_another_reason_long_before_the_outage_is_still_tried_again(world: World) -> None:
    earlier = refuse(world, PUSH, reason="Pushes need the user's approval")
    later(world, 120)
    request = refuse(world, PUSH)
    assert request["id"] == earlier["id"]
    assert [card["id"] for card in state(world)["held_requests"]] == [request["id"]]
    later(world, 2)
    assert len(sync(world)) == 1


def test_a_new_outage_of_the_same_call_gets_its_tries_afresh(world: World) -> None:
    request = refuse(world, PUSH)
    for delay in checker.RETRY_DELAYS:
        later(world, int(delay.total_seconds() // 60))
        assert len(sync(world)) == 1
        delivered(world)
        later(world, 1)
        refuse(world, PUSH)
    # The checker is back and refuses the call for another reason; later it gives no verdict again.
    assert refuse(world, PUSH, reason="Pushes need the user's approval")["id"] == request["id"]
    later(world, 30)
    assert refuse(world, PUSH)["id"] == request["id"]
    # The new outage holds the refusal and tries again, unaffected by the earlier outage's three tries.
    page = state(world)
    assert [card["id"] for card in page["held_requests"]] == [request["id"]] and page["checker_refusals"] == []
    later(world, 2)
    assert len(sync(world)) == 1 and len(tries(world)) == len(checker.RETRY_DELAYS) + 1


def test_the_message_names_at_most_five_calls() -> None:
    now = datetime(2026, 11, 13, 8, 0, tzinfo=UTC)
    refusals = [
        {"explanation": None, "tool_name": "Bash", "tool_input": {"command": f"make step{n}", "description": f"Step {n}"},
         "last_at": "2026-11-13T07:58:00Z"}
        for n in range(7)
    ]
    text = checker.retry_text(refusals, now)
    assert text.startswith(f"{checker.RETRY_PREFIX} on these 7 calls, last at 07:58 UTC, so they did not run:")
    assert "- Step 4\n- and 2 more\n" in text and "- Step 5" not in text
    named = {"explanation": {"heading": "Push the branch"}, "tool_name": "Bash", "tool_input": {"command": "git push"}}
    assert checker.name_of(named) == "Push the branch"
    assert checker.name_of(named | {"explanation": None}) == "git push"
    assert checker.name_of(named | {"explanation": None, "tool_name": "Read", "tool_input": {}}) == "Read"
