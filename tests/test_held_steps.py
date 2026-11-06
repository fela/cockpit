"""SPEC NEXT-16: keep an opened or partially answered step at the top of the queue
while newer, stronger steps arrive, even with no timer running."""

from __future__ import annotations

from tests.conftest import SESSION_A, World
from tests.test_api import agent_client, put_item, put_step, queue_ids
from tests.test_queue_order import returned


def hold(world: World, step_id: int) -> dict:
    held = world.client.post(f"/api/steps/{step_id}/hold", headers=world.user())
    assert held.status_code == 200, held.text
    return held.json()


def test_a_step_the_user_opened_keeps_its_place_when_stronger_work_arrives(world: World) -> None:
    put_item(world)
    steps = [put_step(world, need=f"n{n}", ask=f"Step {n}", score=70 - n)["step"] for n in range(4)]
    assert hold(world, steps[3]["id"]) == {"held": True}
    put_item(world, key="note:sharing", title="Tasks: share a grocery list")
    pressing = [put_step(world, item_key="note:sharing", need=f"reply:{n}", ask=f"Ask {n}", score=95)["step"] for n in range(4)]
    queue, ready = queue_ids(world)
    assert steps[3]["id"] in queue
    assert sorted(queue) == sorted([steps[3]["id"], *(step["id"] for step in pressing[:3])])
    assert pressing[3]["id"] in ready


def test_a_step_the_user_opened_keeps_its_place_against_a_much_stronger_step(world: World) -> None:
    put_item(world)
    steps = [put_step(world, need=f"n{n}", ask=f"Step {n}", score=20 + n)["step"] for n in range(4)]
    for step in steps:
        hold(world, step["id"])
    strong = put_step(world, need="strong", ask="Strong step", score=95)["step"]
    queue, ready = queue_ids(world)
    assert sorted(queue) == sorted(step["id"] for step in steps) and ready == [strong["id"]]


def test_a_hold_needs_a_place_and_the_user(world: World) -> None:
    put_item(world)
    for n in range(4):
        put_step(world, need=f"n{n}", ask=f"Step {n}", score=70)
    waiting = put_step(world, need="weak", ask="Weak step", score=10)["step"]
    assert hold(world, waiting["id"]) == {"held": False}
    refused = agent_client(world).post(f"/api/steps/{waiting['id']}/hold", headers=world.agent())
    assert refused.status_code == 403


def test_a_step_opened_before_its_hand_off_gives_way_once_returned(world: World) -> None:
    put_item(world, sessions=[SESSION_A])
    steps = [put_step(world, need=f"n{n}", ask=f"Step {n}", score=70 - n)["step"] for n in range(4)]
    for step in steps:
        hold(world, step["id"])
        returned(world, step["id"])
    put_item(world, key="note:sharing", title="Tasks: share a grocery list")
    pressing = [put_step(world, item_key="note:sharing", need=f"reply:{n}", ask=f"Ask {n}", score=95)["step"] for n in range(4)]
    queue, _ = queue_ids(world)
    assert sorted(queue) == sorted(step["id"] for step in pressing)


def test_a_step_opened_right_after_its_return_keeps_its_place(world: World) -> None:
    put_item(world, sessions=[SESSION_A])
    steps = [put_step(world, need=f"n{n}", ask=f"Step {n}", score=70 - n)["step"] for n in range(4)]
    for step in steps:
        returned(world, step["id"])
        assert hold(world, step["id"]) == {"held": True}
    put_item(world, key="note:sharing", title="Tasks: share a grocery list")
    pressing = put_step(world, item_key="note:sharing", need="reply:1", ask="Ask", score=95)["step"]
    queue, ready = queue_ids(world)
    assert sorted(queue) == sorted(step["id"] for step in steps) and ready == [pressing["id"]]


def test_a_snooze_lets_the_hold_go(world: World) -> None:
    put_item(world)
    steps = [put_step(world, need=f"n{n}", ask=f"Step {n}", score=70 - n)["step"] for n in range(4)]
    hold(world, steps[0]["id"])
    world.client.post(f"/api/steps/{steps[0]['id']}/snooze", json={"until": "2099-01-01T08:00:00Z"}, headers=world.user())
    world.client.post(f"/api/steps/{steps[0]['id']}/snooze", json={"until": None}, headers=world.user())
    put_item(world, key="note:sharing", title="Tasks: share a grocery list")
    for n in range(4):
        put_step(world, item_key="note:sharing", need=f"reply:{n}", ask=f"Ask {n}", score=95)
    queue, _ = queue_ids(world)
    assert steps[0]["id"] not in queue


def test_an_agent_cannot_yield_a_step_the_user_opened(world: World) -> None:
    put_item(world)
    for n in range(3):
        put_step(world, need=f"n{n}", ask=f"Step {n}", score=70 - n)
    opened = put_step(world, need="weak", ask="Weak step", score=40)["step"]
    put_step(world, need="strong", ask="Strong step", score=48)
    hold(world, opened["id"])
    yielded = agent_client(world).post(f"/api/steps/{opened['id']}/yield", json={}, headers=world.agent())
    assert yielded.status_code == 409 and "keeps its place" in yielded.text
    assert opened["id"] in queue_ids(world)[0]


def test_a_press_that_arrives_after_the_snooze_holds_nothing(world: World) -> None:
    put_item(world)
    steps = [put_step(world, need=f"n{n}", ask=f"Step {n}", score=70 - n)["step"] for n in range(4)]
    world.client.post(f"/api/steps/{steps[0]['id']}/snooze", json={"until": "2099-01-01T08:00:00Z"}, headers=world.user())
    assert hold(world, steps[0]["id"]) == {"held": False}
