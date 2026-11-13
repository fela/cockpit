"""SPEC NEXT-17: a step's round on its item begins when the item's step put before it was put. Its card shows the
agents' images from its round in the strip and older ones under a closed "Other images"."""

from __future__ import annotations

from cockpit import db, store
from tests.conftest import World
from tests.test_api import agent_client, put_item, put_step


def two_rounds(world: World) -> tuple[int, int]:
    """An item whose first step was answered and whose second is open."""
    put_item(world, key="note:rounds", title="Rounds")
    first = put_step(world, item_key="note:rounds", need="first", ask="Which layout?")["step"]["id"]
    done = agent_client(world).post(f"/api/steps/{first}/done", json={"note": "answered"}, headers=world.agent())
    assert done.status_code == 200, done.text
    second = put_step(world, item_key="note:rounds", need="second", ask="Ship it?")["step"]["id"]
    return first, second


def put_at(world: World, times: dict[int, str]) -> dict[int, str]:
    conn = db.connect(world.db_path)
    with conn:
        for step_id, at in times.items():
            conn.execute("UPDATE steps SET created_at = ? WHERE id = ?", (at, step_id))
    return store._round_starts(conn, list(times))


def test_a_step_s_round_begins_when_the_step_before_it_was_put(world: World) -> None:
    first, second = two_rounds(world)
    assert put_at(world, {first: "2026-11-12T10:00:00Z", second: "2026-11-12T11:00:00Z"}) == {second: "2026-11-12T10:00:00Z"}


def test_a_withdrawn_step_put_again_starts_a_later_round_under_its_old_id(world: World) -> None:
    # A withdrawn step put again keeps its id and takes a new time, so the lower id is the later step.
    first, second = two_rounds(world)
    assert put_at(world, {first: "2026-11-12T12:00:00Z", second: "2026-11-12T11:00:00Z"}) == {first: "2026-11-12T11:00:00Z"}


def test_steps_put_in_the_same_second_take_their_order_from_their_ids(world: World) -> None:
    first, second = two_rounds(world)
    assert put_at(world, {first: "2026-11-12T10:00:00Z", second: "2026-11-12T10:00:00Z"}) == {second: "2026-11-12T10:00:00Z"}
