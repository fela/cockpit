"""Finished this week (SPEC VIEW-20): work the user marked done and work closed as not needed show in one list of
what ended this week, each row saying how it ended and who ended it (VIEW-4 for agents' closes)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from tests.conftest import World
from tests.test_api import run_sql
from tests.test_complete_task import CLOSING, ITEM, _finish, _item, _step
from tests.test_only_user_closes import LOOK_AGAIN, _put


def _step_on_done(world: World) -> dict[str, Any]:
    """A finish step on an item an agent's merge already closed. No agent puts a new step on a finished task (SPEC
    CONTROL-27), so the user's own put stands in for whatever brings the user to Task done."""
    body = {"item_key": ITEM, "need": "complete", "ask": "Is the reply out?", "answers": [CLOSING]}
    response = world.client.post("/api/steps", json=body, headers=world.user())
    assert response.status_code == 200, response.text
    return response.json()["step"]


def _state(world: World) -> dict[str, Any]:
    return world.client.get("/api/state").json()


def _board(world: World, item_id: int) -> dict[str, Any]:
    return next(item for item in _state(world)["items"] if item["id"] == item_id)


def _in_one_second(world: World, item_id: int) -> None:
    """Puts everything that happened to the item in one second, as quick presses and puts can."""
    at = _ago(minutes=1)
    run_sql(world, "UPDATE events SET at = ? WHERE item_id = ?", (at, item_id))
    run_sql(world, "UPDATE item_finishes SET at = ? WHERE item_id = ?", (at, item_id))
    run_sql(world, "UPDATE closures SET closed_at = ? WHERE item_id = ?", (at, item_id))
    run_sql(world, "UPDATE items SET done_at = ?, updated_at = ? WHERE id = ?", (at, at, item_id))


def _ago(**delta: float) -> str:
    return (datetime.now(UTC) - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_task_done_says_the_user_ended_it_with_the_closing_answer(world: World) -> None:
    item_id = _item(world)
    step = _step(world, "complete", [CLOSING])
    assert _finish(world, item_id, step["id"], "complete", CLOSING["text"], "").status_code == 200
    item = _board(world, item_id)
    assert (item["ball"], item["ended"]["by"], item["ended"]["note"]) == ("done", "user", CLOSING["text"])


def test_task_done_says_the_user_ended_it_with_the_last_message(world: World) -> None:
    item_id = _item(world)
    step = _step(world, "complete", [])
    assert _finish(world, item_id, step["id"], "complete", None, "", note="Thanks, all good").status_code == 200
    item = _board(world, item_id)
    assert (item["ball"], item["ended"]["by"], item["ended"]["note"]) == ("done", "user", "Thanks, all good")


def test_an_agent_s_close_of_a_merged_pr_says_who_closed_it_and_why(world: World, merged_pulls: set[str]) -> None:
    item_id = _item(world)
    merged_pulls.add("example-org/app#20")
    _put(world, stage="done", note="app#20 merged")
    ended = _board(world, item_id)["ended"]
    assert (ended["by"], ended["note"]) == ("agent:sweep", "app#20 merged")


def test_an_item_moved_to_done_some_other_way_has_no_ender(world: World) -> None:
    item_id = _item(world)
    world.client.patch(f"/api/items/{item_id}", json={"stage": "done"}, headers=world.user())
    assert _board(world, item_id)["ended"] is None
    assert _board(world, _put(world, key="note:open", title="Open work", stage="review")["id"])["ended"] is None


def test_closed_as_not_needed_stays_listed_for_the_week_with_the_user_s_reason(world: World) -> None:
    item_id = _item(world)
    step = _step(world, "complete", [])
    assert _finish(world, item_id, step["id"], "close", None, "Alex took it over").status_code == 200
    closed = _state(world)["closed_recently"]["items"]
    assert [(entry["id"], entry["action"], entry["closed_by"], entry["note"]) for entry in closed] == [
        (item_id, "archived", "user", "Alex took it over")
    ]
    # An archive just inside seven days stays listed with an empty reason.
    other = _put(world, key="note:other", title="Other work", stage="review")["id"]
    world.client.patch(f"/api/items/{other}", json={"archived": True}, headers=world.user())
    run_sql(world, "UPDATE items SET archived_at = ? WHERE id = ?", (_ago(days=6, hours=23), other))
    old = _put(world, key="note:old", title="Old work", stage="review")["id"]
    world.client.patch(f"/api/items/{old}", json={"archived": True}, headers=world.user())
    run_sql(world, "UPDATE items SET archived_at = ? WHERE id = ?", (_ago(days=7, hours=1), old))
    closed = _state(world)["closed_recently"]["items"]
    assert [(entry["id"], entry["note"]) for entry in closed] == [(item_id, "Alex took it over"), (other, "")]
    # Unarchived, it leaves the list and is open work again.
    world.client.patch(f"/api/items/{item_id}", json={"archived": False}, headers=world.user())
    assert [entry["id"] for entry in _state(world)["closed_recently"]["items"]] == [other]
    assert _board(world, item_id)["key"] == ITEM


def test_an_agent_s_stage_done_stays_in_the_closes_for_a_day_only(world: World) -> None:
    """The sweeper checks agents' closes of the day from `closed_recently` (VIEW-4); the week's done items show
    from `items` with their `ended`."""
    item_id = _put(world, key="note:other", title="Other work", stage="review")["id"]
    at = _ago(days=2)
    run_sql(world, "UPDATE items SET stage = 'done', done_at = ? WHERE id = ?", (at, item_id))
    run_sql(
        world, "INSERT INTO closures (kind, ref_id, item_id, action, closed_by, closed_at, note)"
        " VALUES ('item', ?, ?, 'stage done', 'agent:app-1', ?, 'merged')", (item_id, item_id, at),
    )
    run_sql(
        world, "INSERT INTO events (at, actor, kind, item_id, data) VALUES (?, 'agent:app-1', 'item.merged', ?, ?)",
        (at, item_id, '{"pull": "example-org/app#10", "note": "merged"}'),
    )
    assert _state(world)["closed_recently"]["items"] == []
    assert _board(world, item_id)["ended"] == {"by": "agent:app-1", "note": "merged", "at": at}


def test_a_close_reason_does_not_follow_the_item_past_unarchive(world: World) -> None:
    item_id = _item(world)
    step = _step(world, "complete", [])
    assert _finish(world, item_id, step["id"], "close", None, "Alex took it over").status_code == 200
    world.client.patch(f"/api/items/{item_id}", json={"archived": False}, headers=world.user())
    world.client.patch(f"/api/items/{item_id}", json={"archived": True}, headers=world.user())
    assert [(entry["id"], entry["note"]) for entry in _state(world)["closed_recently"]["items"]] == [(item_id, "")]


def test_a_new_close_reason_holds_after_an_unarchive_in_the_same_second(world: World) -> None:
    item_id = _item(world)
    step = _step(world, "complete", [])
    assert _finish(world, item_id, step["id"], "close", None, "Alex took it over").status_code == 200
    world.client.patch(f"/api/items/{item_id}", json={"archived": False}, headers=world.user())
    step = _step(world, "complete:again", [])
    assert _finish(world, item_id, step["id"], "close", None, "Not needed after all").status_code == 200
    _in_one_second(world, item_id)
    closed = _state(world)["closed_recently"]["items"]
    assert [(entry["id"], entry["note"]) for entry in closed] == [(item_id, "Not needed after all")]


def test_only_the_latest_move_to_done_says_how_it_ended(world: World, merged_pulls: set[str]) -> None:
    item_id = _item(world)
    merged_pulls.add("example-org/app#20")
    _put(world, stage="done", note="app#20 merged")
    run_sql(world, "UPDATE closures SET closed_at = ? WHERE item_id = ?", (_ago(hours=2), item_id))
    run_sql(world, "UPDATE events SET at = ? WHERE item_id = ?", (_ago(hours=2), item_id))
    run_sql(world, "UPDATE items SET done_at = ? WHERE id = ?", (_ago(hours=2), item_id))
    # The work went on, then the user moved it to done by hand: the agent's old close no longer describes it.
    world.client.patch(f"/api/items/{item_id}", json={"stage": "building"}, headers=world.user())
    world.client.patch(f"/api/items/{item_id}", json={"stage": "done"}, headers=world.user())
    assert _board(world, item_id)["ended"] is None


def test_in_one_second_the_later_of_the_user_s_finish_and_the_agent_s_close_counts(
    world: World, merged_pulls: set[str]
) -> None:
    item_id = _item(world)
    merged_pulls.add("example-org/app#20")
    _put(world, stage="done", note="app#20 merged")
    step = _step_on_done(world)
    assert _finish(world, item_id, step["id"], "complete", CLOSING["text"], "").status_code == 200
    _in_one_second(world, item_id)
    ended = _board(world, item_id)["ended"]
    assert (ended["by"], ended["note"]) == ("user", CLOSING["text"])
    # The work went on and the agent closed it again in that second: the agent's close describes it now.
    world.client.patch(f"/api/items/{item_id}", json={"stage": "building"}, headers=world.user())
    _put(world, stage="done", note="app#20 merged again")
    _in_one_second(world, item_id)
    ended = _board(world, item_id)["ended"]
    assert (ended["by"], ended["note"]) == ("agent:sweep", "app#20 merged again")


def test_a_done_item_still_waiting_on_someone_keeps_who_closed_it(world: World, merged_pulls: set[str]) -> None:
    item_id = _item(world)  # waits on Ana
    merged_pulls.add("example-org/app#20")
    _put(world, stage="done", note="app#20 merged")
    item = _board(world, item_id)
    assert (item["ball"], item["ended"]["by"], item["ended"]["note"]) == ("done", "agent:sweep", "app#20 merged")


def test_undo_of_task_done_gives_the_agent_s_close_back(world: World, merged_pulls: set[str]) -> None:
    item_id = _item(world)
    merged_pulls.add("example-org/app#20")
    _put(world, stage="done", note="app#20 merged")
    step = _step_on_done(world)
    assert _finish(world, item_id, step["id"], "complete", CLOSING["text"], "").status_code == 200
    assert world.client.post(f"/api/items/{item_id}/reopen", headers=world.user()).status_code == 200
    item = _board(world, item_id)
    assert (item["stage"], item["ended"]["by"], item["ended"]["note"]) == ("done", "agent:sweep", "app#20 merged")


def test_a_close_reason_holds_past_the_week_while_the_item_stays_listed(world: World) -> None:
    item_id = _item(world)
    step = _step(world, "complete", [])
    assert _finish(world, item_id, step["id"], "close", None, "Alex took it over").status_code == 200
    long_ago = _ago(days=8)
    run_sql(world, "UPDATE events SET at = ? WHERE item_id = ?", (long_ago, item_id))
    run_sql(world, "UPDATE item_finishes SET at = ? WHERE item_id = ?", (long_ago, item_id))
    # An agent's later put refreshes the archived item, which lists it again.
    _put(world, summary="Alex has it now")
    assert [(entry["id"], entry["note"]) for entry in _state(world)["closed_recently"]["items"]] == [
        (item_id, "Alex took it over")
    ]


def test_merged_pr_with_leftover_waiter_reaches_the_finished_group(world: World, merged_pulls: set[str]) -> None:
    item_id = _item(world)
    _put(world, waiting_on=[{"kind": "bot", "who": "reviewer", "until": LOOK_AGAIN}])
    merged_pulls.add("example-org/app#20")
    _put(world, stage="done", note="merged")
    item = _board(world, item_id)
    assert [(entry["kind"], entry["who"]) for entry in item["waiting_on"]] == [("bot", "reviewer")]
    assert (item["ball"], item["group"]) == ("done", "done")


def test_a_put_on_an_archived_item_leaves_when_and_who_closed_it(world: World) -> None:
    # An agent's summary preserves the closed task's closing time and reason.
    item_id = _item(world)
    step = _step(world, "complete", [])
    assert _finish(world, item_id, step["id"], "close", None, "Sam took it over").status_code == 200
    closed_at = _state(world)["closed_recently"]["items"][0]["closed_at"]
    at = _ago(days=6, hours=23)
    run_sql(world, "UPDATE items SET archived_at = ? WHERE id = ?", (at, item_id))
    run_sql(world, "UPDATE events SET at = ? WHERE item_id = ? AND kind = 'item.closed'", (at, item_id))
    run_sql(world, "UPDATE item_finishes SET at = ? WHERE item_id = ?", (at, item_id))
    assert closed_at > at
    _put(world, summary="Handed to Sam, who ships it Friday")
    assert [(entry["id"], entry["closed_at"], entry["closed_by"], entry["note"]) for entry in _state(world)["closed_recently"]["items"]] == [
        (item_id, at, "user", "Sam took it over")
    ]
    # Past the week, the same put does not bring it back.
    run_sql(world, "UPDATE items SET archived_at = ? WHERE id = ?", (_ago(days=7, hours=1), item_id))
    _put(world, summary="Shipped")
    assert _state(world)["closed_recently"]["items"] == []


def test_a_put_on_old_done_work_does_not_bring_it_back_to_all_work(world: World) -> None:
    # All work shows what was done this week, not what an agent last wrote on.
    item_id = _put(world, key="note:other", title="Other work", stage="review")["id"]
    world.client.patch(f"/api/items/{item_id}", json={"stage": "done"}, headers=world.user())
    assert _board(world, item_id)["stage"] == "done"
    run_sql(world, "UPDATE items SET done_at = ? WHERE id = ?", (_ago(days=6, hours=23), item_id))
    _put(world, key="note:other", summary="Shipped this week")
    assert _board(world, item_id)["stage"] == "done"
    run_sql(world, "UPDATE items SET done_at = ? WHERE id = ?", (_ago(days=7, hours=1), item_id))
    _put(world, key="note:other", summary="Shipped long ago")
    assert item_id not in [item["id"] for item in _state(world)["items"]]
