"""SPEC NEXT-60: a step that is useful only until a time, such as one whose sign-in link stops working, leads the
user's queue while the time runs, and its session hears once when the time passes with the step still open."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from cockpit import db, store
from tests.conftest import World
from tests.test_api import SESSION_A, agent_client, put_card, put_item, queue_ids, run_sql

CHECKOUTS = Path("/tmp")


def at(minutes: float) -> str:
    """A time that many minutes from now, as a session would send it."""
    return (datetime.now(UTC) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def told(world: World, minutes_from_now: float) -> list[dict]:
    """The messages Cockpit queues for the sessions of expired steps, as its loop would that many minutes from now."""
    conn = db.connect(world.db_path)
    try:
        sent = store.tell_expired(conn, CHECKOUTS, datetime.now(UTC) + timedelta(minutes=minutes_from_now))
        return [store.get_message(conn, message_id) for message_id in sent]
    finally:
        conn.close()


def test_a_step_with_a_time_leads_a_full_queue_and_another_step_makes_room(world: World) -> None:
    steps = [put_card(world, f"n{n}", ask=f"Step {n}", score=90 - n)["step"] for n in range(4)]
    link = put_card(world, "sign-in", ask="Open the sign-in link", score=10, expires_at=at(5))["step"]
    assert link["queued_at"] is not None and link["taken_at"] is None
    queue, ready = queue_ids(world)
    assert queue == [link["id"], *(step["id"] for step in steps[:3])]
    assert ready == [steps[3]["id"]]


def test_the_soonest_time_leads_and_a_taken_step_follows(world: World) -> None:
    steps = [put_card(world, f"n{n}", ask=f"Step {n}", score=90 - n)["step"] for n in range(4)]
    waiting = put_card(world, "waiting", ask="Waiting step", score=20)["step"]
    take = world.client.post(f"/api/steps/{waiting['id']}/take", json={}, headers=world.user())
    assert take.status_code == 200, take.text
    later = put_card(world, "later-link", ask="Open the later link", expires_at=at(30))["step"]
    sooner = put_card(world, "sooner-link", ask="Open the sooner link", expires_at=at(5))["step"]
    queue, _ready = queue_ids(world)
    assert queue[:3] == [sooner["id"], later["id"], waiting["id"]]
    assert len(queue) == store.QUEUE_LIMIT and steps[0]["id"] in queue


def test_a_stronger_step_does_not_take_the_place_while_the_time_runs(world: World) -> None:
    link = put_card(world, "sign-in", ask="Open the sign-in link", score=5, expires_at=at(5))["step"]
    for n in range(3):
        put_card(world, f"n{n}", ask=f"Step {n}", score=60)
    strong = put_card(world, "strong", ask="Strong step", score=95)["step"]
    queue, _ready = queue_ids(world)
    assert queue[0] == link["id"] and strong["id"] in queue  # the strong step took a weaker ordinary step's place
    refused = agent_client(world).post(f"/api/steps/{link['id']}/yield", json={}, headers=world.agent())
    assert refused.status_code == 409, refused.text


def test_after_its_time_the_step_is_ranked_by_its_score_again(world: World) -> None:
    link = put_card(world, "sign-in", ask="Open the sign-in link", score=5, expires_at=at(5))["step"]
    strong = put_card(world, "strong", ask="Strong step", score=95)["step"]
    assert queue_ids(world)[0] == [link["id"], strong["id"]]
    run_sql(world, "UPDATE steps SET expires_at = ? WHERE id = ?", (at(-1), link["id"]))
    assert queue_ids(world)[0] == [strong["id"], link["id"]]


def test_a_time_that_has_passed_or_has_no_utc_offset_is_refused(world: World) -> None:
    put_item(world, key="note:sign-in", title="Sign in", sessions=["11111111-1111-4111-8111-111111111111"])
    body = {"item_key": "note:sign-in", "need": "sign-in", "ask": "Open the sign-in link"}
    passed = agent_client(world).post("/api/steps", json=body | {"expires_at": at(-1)}, headers=world.agent())
    assert passed.status_code == 400 and "has passed already" in passed.text
    bare = agent_client(world).post("/api/steps", json=body | {"expires_at": "2099-01-01T10:00:00"}, headers=world.agent())
    assert bare.status_code == 400 and "needs a UTC offset" in bare.text


def test_a_step_with_a_time_needs_a_session_to_tell(world: World) -> None:
    put_item(world, key="note:sign-in", title="Sign in")
    body = {"item_key": "note:sign-in", "need": "sign-in", "ask": "Open the sign-in link", "expires_at": at(5)}
    refused = agent_client(world).post("/api/steps", json=body, headers=world.agent())
    assert refused.status_code == 400 and "needs a session" in refused.text
    own = agent_client(world).post(
        "/api/steps", json=body | {"session_id": "22222222-2222-4222-8222-222222222222"}, headers=world.agent()
    )
    assert own.status_code == 200, own.text
    cleared = agent_client(world).post(
        "/api/steps", json={"item_key": "note:sign-in", "need": "sign-in", "session_id": None}, headers=world.agent()
    )
    assert cleared.status_code == 400 and "needs a session" in cleared.text


def test_a_second_step_with_a_time_leaves_the_first_its_place(world: World) -> None:
    first = put_card(world, "first-link", ask="Open the first link", expires_at=at(5))["step"]
    steps = [put_card(world, f"n{n}", ask=f"Step {n}", score=90 - n)["step"] for n in range(3)]
    for step in steps:  # the user opened each of the other cards in its place; the last one's timer still runs
        assert world.client.post(f"/api/steps/{step['id']}/timer/start", headers=world.user()).status_code == 200
    second = put_card(world, "second-link", ask="Open the second link", expires_at=at(10))["step"]
    queue, ready = queue_ids(world)
    assert queue[:2] == [first["id"], second["id"]] and len(queue) == store.QUEUE_LIMIT
    assert ready == [steps[1]["id"]]  # the weakest paused step made room


def test_an_agent_s_return_gives_a_step_whose_time_runs_its_place(world: World) -> None:
    link = put_card(world, "sign-in", ask="Open the sign-in link", score=5, expires_at=at(5))["step"]
    handed = world.client.post(f"/api/steps/{link['id']}/hand-off", json={"text": "Make a new link"}, headers=world.user())
    assert handed.status_code == 200, handed.text
    for n in range(4):
        put_card(world, f"n{n}", ask=f"Step {n}", score=90)
    assert told(world, 6) == []  # the user answered; the step is with its agent
    put_card(world, "sign-in", ask="Open the new sign-in link", expires_at=at(10))
    back = agent_client(world).post(f"/api/steps/{link['id']}/return", json={"note": "The link is new."}, headers=world.agent())
    assert back.status_code == 200, back.text
    queue, _ready = queue_ids(world)
    assert queue[0] == link["id"] and len(queue) == store.QUEUE_LIMIT


def test_the_session_hears_once_when_the_time_passes(world: World) -> None:
    link = put_card(world, "sign-in", ask="Open the sign-in link", expires_at=at(5))["step"]
    assert told(world, 4) == []
    messages = told(world, 6)
    assert len(messages) == 1
    message = messages[0]
    assert message["author"] == "cockpit" and message["item_id"] == link["item_id"] and message["session_id"]
    assert f"Step {link['id']}" in message["text"] and "expires_at" in message["text"]
    assert told(world, 7) == []


def test_a_step_whose_task_lost_its_session_counts_as_told_only_once_a_session_is_told(world: World) -> None:
    link = put_card(world, "sign-in", ask="Open the sign-in link", expires_at=at(5))["step"]
    put_item(world, key="note:sign-in", title="sign-in", sessions=[])

    def card() -> dict:
        return next(step for step in world.client.get("/api/state").json()["queue"] if step["id"] == link["id"])

    assert told(world, 6) == []
    assert card()["expiry_told_at"] is None  # the card does not say that a session was asked
    put_item(world, key="note:sign-in", title="sign-in", sessions=[SESSION_A])
    messages = told(world, 7)
    assert [message["session_id"] for message in messages] == [SESSION_A]
    assert card()["expiry_told_at"] is not None
    assert told(world, 8) == []


def test_a_renewed_time_leads_again_and_is_told_again(world: World) -> None:
    link = put_card(world, "sign-in", ask="Open the sign-in link", score=5, expires_at=at(5))["step"]
    strong = put_card(world, "strong", ask="Strong step", score=95)["step"]
    run_sql(world, "UPDATE steps SET expires_at = ? WHERE id = ?", (at(-1), link["id"]))
    assert len(told(world, 0)) == 1
    assert queue_ids(world)[0] == [strong["id"], link["id"]]
    renewed = put_card(world, "sign-in", ask="Open the new sign-in link", expires_at=at(5))["step"]
    assert renewed["expiry_told_at"] is None
    assert queue_ids(world)[0] == [link["id"], strong["id"]]
    assert len(told(world, 6)) == 1


def test_a_step_freed_of_its_time_or_answered_is_not_told(world: World) -> None:
    freed = put_card(world, "freed", ask="Open the link", expires_at=at(5))["step"]
    put_card(world, "freed", expires_at=None)
    done = put_card(world, "done", ask="Open the other link", expires_at=at(5))["step"]
    closed = world.client.post(f"/api/steps/{done['id']}/done", json={}, headers=world.user())
    assert closed.status_code == 200, closed.text
    assert told(world, 6) == []
    state = world.client.get("/api/state").json()
    assert [step["expires_at"] for step in state["queue"] if step["id"] == freed["id"]] == [None]
