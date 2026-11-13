"""SPEC NEXT-42: an early look, a session's early result or question while its work goes on, takes a place
in the user's queue only while no other step waits for one, and gives the place up when another step does."""

from __future__ import annotations

from datetime import UTC, datetime

from cockpit import db, store, sweeper
from tests.conftest import World
from tests.test_api import agent_client, put_card, put_item, put_step, queue_ids, run_sql


def close(world: World, step_id: int) -> None:
    done = agent_client(world).post(f"/api/steps/{step_id}/done", json={"note": "Done."}, headers=world.agent())
    assert done.status_code == 200, done.text


def test_an_early_look_takes_a_free_place_when_nothing_else_waits(world: World) -> None:
    early = put_card(world, "early:first-numbers", ask="First numbers are in", early=True)["step"]
    assert early["early"] == 1
    assert queue_ids(world)[0] == [early["id"]]


def test_a_free_place_goes_to_an_ordinary_step_before_an_early_look(world: World) -> None:
    steps = [put_card(world, f"n{n}", ask=f"Step {n}", score=70 - n)["step"] for n in range(4)]
    early = put_card(world, "early:numbers", ask="First numbers are in", score=95, early=True)["step"]
    ordinary = put_card(world, "later", ask="Later step", score=20)["step"]
    queue, ready = queue_ids(world)
    assert queue == [step["id"] for step in steps]  # the early look's high score takes no place
    close(world, steps[0]["id"])
    queue, ready = queue_ids(world)
    assert ordinary["id"] in queue and ready == [early["id"]]
    close(world, steps[1]["id"])
    assert early["id"] in queue_ids(world)[0]


def test_an_ordinary_step_takes_an_early_look_s_place_whatever_the_scores(world: World) -> None:
    early = put_card(world, "early:numbers", ask="First numbers are in", score=90, early=True)["step"]
    steps = [put_card(world, f"n{n}", ask=f"Step {n}", score=70 - n)["step"] for n in range(3)]
    assert early["id"] in queue_ids(world)[0]
    weak = put_card(world, "weak", ask="Weak step", score=10)["step"]
    queue, ready = queue_ids(world)
    assert set(queue) == {weak["id"], *(step["id"] for step in steps)} and ready == [early["id"]]


def test_an_early_look_becomes_ordinary_when_its_session_says_so(world: World) -> None:
    steps = [put_card(world, f"n{n}", ask=f"Step {n}", score=50 - n)["step"] for n in range(4)]
    early = put_card(world, "early:numbers", ask="First numbers are in", score=90, early=True)["step"]
    assert early["id"] in queue_ids(world)[1]
    put_card(world, "early:numbers", ask="First numbers are in", score=90, early=False)
    queue, ready = queue_ids(world)
    assert early["id"] in queue and steps[3]["id"] in ready


def test_a_waiting_early_look_leaves_ordinary_steps_to_swap_by_score(world: World) -> None:
    steps = [put_card(world, f"n{n}", ask=f"Step {n}", score=50 + n)["step"] for n in range(4)]
    early = put_card(world, "early:numbers", ask="First numbers are in", score=99, early=True)["step"]
    strong = put_card(world, "strong", ask="Strong step", score=80)["step"]
    queue, ready = queue_ids(world)
    assert strong["id"] in queue and steps[0]["id"] in ready and early["id"] in ready


def test_a_step_put_while_a_sweep_runs_takes_an_early_look_s_place(world: World) -> None:
    early = put_card(world, "early:numbers", ask="First numbers are in", score=90, early=True)["step"]
    steps = [put_card(world, f"n{n}", ask=f"Step {n}", score=60)["step"] for n in range(3)]
    assert agent_client(world).post("/api/sweeps/start", json={"reorder": False}, headers=world.agent()).status_code == 200
    late = put_card(world, "late", ask="Late step", score=10)["step"]
    queue, ready = queue_ids(world)
    assert set(queue) == {late["id"], *(step["id"] for step in steps)} and ready == [early["id"]]


def test_a_take_sends_the_early_look_back_rather_than_another_step(world: World) -> None:
    early = put_card(world, "early:numbers", ask="First numbers are in", score=90, early=True)["step"]
    _steps = [put_card(world, f"n{n}", ask=f"Step {n}", score=60 - n)["step"] for n in range(3)]
    _waiting = put_card(world, "waiting", ask="Waiting step", score=10)["step"]
    later = put_card(world, "later", ask="Later step", score=5)["step"]
    assert world.client.post(f"/api/steps/{later['id']}/take", json={}, headers=world.user()).status_code == 200
    queue, ready = queue_ids(world)
    assert early["id"] in ready and later["id"] in queue and len(queue) == 4


def test_undo_leaves_no_early_look_in_a_place_another_step_waits_for(world: World) -> None:
    early = put_card(world, "early:numbers", ask="First numbers are in", score=90, early=True)["step"]
    steps = [put_card(world, f"n{n}", ask=f"Step {n}", score=60 - n)["step"] for n in range(3)]
    run_sql(world, "UPDATE steps SET queued_at = '2026-01-05T08:00:00Z'")  # placed before this morning's work
    world.client.post(f"/api/steps/{steps[0]['id']}/done", json={}, headers=world.user())
    newer = put_card(world, "newer", ask="Newer step", score=20)["step"]
    world.client.post(f"/api/steps/{steps[0]['id']}/reopen", json={}, headers=world.user())
    queue, ready = queue_ids(world)
    assert set(queue) == {newer["id"], *(step["id"] for step in steps)} and ready == [early["id"]]


def test_restoring_an_item_leaves_no_early_look_in_a_place_another_step_waits_for(world: World) -> None:
    item = put_item(world, key="note:early", title="Early experiment")
    early = put_step(world, item_key="note:early", need="early:numbers", ask="First numbers are in", score=90,
                     early=True)["step"]
    assert world.client.patch(f"/api/items/{item['id']}", json={"archived": True}, headers=world.user()).status_code == 200
    steps = [put_card(world, f"n{n}", ask=f"Step {n}", score=70 - n)["step"] for n in range(5)]
    assert world.client.patch(f"/api/items/{item['id']}", json={"archived": False}, headers=world.user()).status_code == 200
    queue, ready = queue_ids(world)
    assert early["id"] in ready and steps[4]["id"] in ready
    assert queue == [step["id"] for step in steps[:4]]  # the four places stay four


def test_a_queue_of_only_early_looks_still_asks_the_sweeper_for_work(world: World) -> None:
    early = put_card(world, "early:numbers", ask="First numbers are in", score=90, early=True)["step"]
    world.client.get("/api/state")  # the user looks at the page
    conn = db.connect(world.db_path)
    try:
        message_id = sweeper.fill_once(conn, world.checkout, lambda session_id: False)
        assert message_id is not None
        assert "it holds at most one step besides early looks" in store.get_message(conn, message_id)["text"]
    finally:
        conn.close()
    put_card(world, "ordinary", ask="Ordinary step", score=10)
    put_card(world, "ordinary:2", ask="Another ordinary step", score=12)
    conn = db.connect(world.db_path)
    try:
        assert not sweeper.fill_due(conn, datetime.now(UTC))  # two steps wait for the user
    finally:
        conn.close()
    assert early["id"] in queue_ids(world)[0]
