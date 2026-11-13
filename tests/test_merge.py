"""SPEC NEXT-55: one task shows the user one card. An agent merges two items that are one task, and its second card
on an item is refused."""

from __future__ import annotations

from typing import Any

import pytest

from cockpit import cli, db, store
from tests.conftest import SESSION_A, SESSION_B, World
from tests.test_api import agent_client, hand_off, run_sql
from tests.test_cli import route_cli_to

MAIN = "note:weekly-reminder"
SIDE = "note:reminder-time"
NOTE = "Both are about the same weekly reminder."
BEFORE = "2026-01-01T00:00:00Z"


def _put_item(world: World, key: str, title: str, **fields: Any) -> dict[str, Any]:
    response = agent_client(world).put("/api/items", json={"key": key, "title": title} | fields, headers=world.agent())
    assert response.status_code == 200, response.text
    return response.json()


def _put_step(world: World, key: str, need: str, ask: str, **fields: Any):
    body = {"item_key": key, "need": need, "ask": ask} | fields
    return agent_client(world).post("/api/steps", json=body, headers=world.agent())


def _step(world: World, key: str, need: str, ask: str) -> dict[str, Any]:
    response = _put_step(world, key, need, ask)
    assert response.status_code == 200, response.text
    return response.json()["step"]


def _merge(world: World, side: str = SIDE, into: str = MAIN, note: str = NOTE):
    return agent_client(world).post("/api/items/merge", json={"side": side, "into": into, "note": note}, headers=world.agent())


def _two_tasks(world: World) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """The weekly reminder's item and card, and a side item about its time with a card of its own."""
    main = _put_item(world, MAIN, "Weekly reminder", sessions=[SESSION_A],
                     links=[{"label": "the draft", "url": "https://docs.example/draft"}])
    side = _put_item(world, SIDE, "Reminder time", sessions=[SESSION_B, SESSION_A],
                     links=[{"label": "the draft", "url": "https://docs.example/draft"},
                            {"label": "the poll", "url": "https://docs.example/poll"}])
    main_card = _step(world, MAIN, "review", "send the weekly reminder?")
    side_card = _step(world, SIDE, "decide:time", "send it Monday at 9?")
    return main, side, main_card, side_card


def _get(world: World, sql: str, *params: Any) -> list[dict[str, Any]]:
    conn = db.connect(world.db_path)
    try:
        return db.rows(conn.execute(sql, params).fetchall())
    finally:
        conn.close()


def _merged_into(world: World, item_id: int) -> int | None:
    return _get(world, "SELECT merged_into FROM items WHERE id = ?", item_id)[0]["merged_into"]


def _state_item(world: World, item_id: int) -> dict[str, Any]:
    return next(item for item in world.client.get("/api/state").json()["items"] if item["id"] == item_id)


def test_a_merge_moves_the_side_task_into_the_main_one(world: World) -> None:
    main, side, main_card, side_card = _two_tasks(world)
    run_sql(world, "INSERT INTO messages (author, item_id, step_id, text, state, created_at, sent_at)"
            " VALUES ('user', ?, ?, 'Not at 9, people commute', 'sent', ?, ?)", (side["id"], side_card["id"], BEFORE, BEFORE))

    merged = _merge(world)

    assert merged.status_code == 200, merged.text
    result = merged.json()
    assert result["held"] is None
    assert result["merged"]["from"] == side["id"] and result["merged"]["into"] == main["id"]
    assert result["merged"]["steps"] == [side_card["id"]]
    # The side item is archived and points at the item it joined.
    assert _get(world, "SELECT archived, merged_into FROM items WHERE id = ?", side["id"])[0] == {
        "archived": 1, "merged_into": main["id"],
    }
    # Its open card is withdrawn and lives on the joined item under a need of its own, so the main item's needs stay free.
    moved = _get(world, "SELECT item_id, need, merged_from, withdrawn_at FROM steps WHERE id = ?", side_card["id"])[0]
    assert (moved["item_id"], moved["need"], moved["merged_from"]) == (main["id"], f"decide:time:from-{side['id']}", side["id"])
    assert moved["withdrawn_at"] is not None
    # The user's words come along, and the joined item has both items' sessions and links once each.
    assert _get(world, "SELECT item_id FROM messages WHERE text = 'Not at 9, people commute'")[0]["item_id"] == main["id"]
    item = result["item"]
    assert item["sessions"] == [SESSION_A, SESSION_B]
    assert [link["url"] for link in item["links"]] == ["https://docs.example/draft", "https://docs.example/poll"]
    # The main item's own card stays open; the merge's line on it names the side task and the card it had open.
    assert [step["id"] for step in result["open_steps"]] == [main_card["id"]]
    merges = _state_item(world, main["id"])["merges"]
    assert [(merge["from_title"], merge["note"], merge["moved_steps"]) for merge in merges] == [
        ("Reminder time", NOTE, [{"id": side_card["id"], "ask": "send it Monday at 9?"}])
    ]


def test_a_handed_step_stays_with_its_agent_on_the_joined_task(world: World) -> None:
    main, _, _, side_card = _two_tasks(world)
    hand_off(world, side_card["id"])

    assert _merge(world).status_code == 200

    step = _get(world, "SELECT item_id, need, status, withdrawn_at, handoff_message_id FROM steps WHERE id = ?", side_card["id"])[0]
    assert (step["item_id"], step["need"], step["status"], step["withdrawn_at"]) == (main["id"], "decide:time", "open", None)
    assert step["handoff_message_id"] is not None
    # Its agent still answers it where it now lives.
    back = agent_client(world).post(f"/api/steps/{side_card['id']}/return", json={"note": "Monday at 10."}, headers=world.agent())
    assert back.status_code == 200, back.text


def test_a_moved_step_takes_a_free_need_when_the_main_task_has_its_need(world: World) -> None:
    main = _put_item(world, MAIN, "Weekly reminder")
    side = _put_item(world, SIDE, "Reminder time")
    _step(world, MAIN, "review", "send the weekly reminder?")
    side_card = _step(world, SIDE, "review", "send the reminder at 9?")
    run_sql(world, "UPDATE steps SET need = ? WHERE item_id = ? AND need = 'review'", (f"review:from-{side['id']}", main["id"]))

    assert _merge(world).status_code == 200

    assert _get(world, "SELECT need FROM steps WHERE id = ?", side_card["id"])[0]["need"] == f"review:from-{side['id']}-2"


@pytest.mark.parametrize("on", ["main", "side"])
def test_a_merge_waits_while_the_user_is_on_a_step_of_either_task(world: World, on: str) -> None:
    main, side, main_card, side_card = _two_tasks(world)
    timed = main_card if on == "main" else side_card
    assert world.client.post(f"/api/steps/{timed['id']}/timer/start", headers=world.user()).status_code == 200

    held = _merge(world)

    assert held.status_code == 200 and held.json()["held"], held.text
    assert _merged_into(world, side["id"]) is None
    assert world.client.post(f"/api/steps/{timed['id']}/timer/pause", headers=world.user()).status_code == 200
    assert _merged_into(world, side["id"]) == main["id"]


def test_a_merge_runs_at_once_while_the_user_is_on_another_task(world: World) -> None:
    main, side, _, _ = _two_tasks(world)
    _put_item(world, "note:other", "Other task")
    other = _step(world, "note:other", "decide", "keep the old export?")
    assert world.client.post(f"/api/steps/{other['id']}/timer/start", headers=world.user()).status_code == 200

    merged = _merge(world)

    assert merged.status_code == 200 and merged.json()["held"] is None, merged.text
    assert _merged_into(world, side["id"]) == main["id"]


def test_writes_to_a_merged_task_go_to_the_task_it_joined(world: World) -> None:
    main, _, _, _ = _two_tasks(world)
    assert _merge(world).status_code == 200

    put = agent_client(world).put("/api/items", json={"key": SIDE, "summary": "Monday at 10"}, headers=world.agent())
    assert put.status_code == 409 and f"merged into item {main['id']}" in put.json()["detail"]
    step = _put_step(world, SIDE, "decide:day", "Monday or Tuesday?")
    assert step.status_code == 409 and f"Put this on item {main['id']}" in step.json()["detail"]
    again = _merge(world)
    assert again.status_code == 409 and "already merged" in again.json()["detail"]
    into_merged = _merge(world, side=MAIN, into=SIDE)
    assert into_merged.status_code == 409 and "merge into that one" in into_merged.json()["detail"]
    # The user's own edit of the archived item still goes through.
    assert world.client.put("/api/items", json={"key": SIDE, "priority": "low"}, headers=world.user()).status_code == 200


def test_an_item_cannot_merge_into_itself_or_from_a_closed_task(world: World) -> None:
    main, _, _, _ = _two_tasks(world)
    itself = _merge(world, side=MAIN, into=str(main["id"]))
    assert itself.status_code == 400 and "into itself" in itself.json()["detail"]
    assert world.client.put("/api/items", json={"key": SIDE, "archived": True}, headers=world.user()).status_code == 200
    closed = _merge(world)
    assert closed.status_code == 409 and "is closed" in closed.json()["detail"]
    assert _merge(world, side="note:none").status_code == 404


def test_an_agent_s_second_card_on_a_task_is_refused(world: World) -> None:
    _put_item(world, MAIN, "Weekly reminder")
    card = _step(world, MAIN, "review", "send the weekly reminder?")

    second = _put_step(world, MAIN, "decide:time", "send it Monday at 9?")

    assert second.status_code == 409, second.text
    assert f"step {card['id']} (need review" in second.json()["detail"]
    # Rewriting the open card goes through, and so does a new card once the old one is withdrawn.
    assert _put_step(world, MAIN, "review", "send the weekly reminder Monday at 10?").status_code == 200
    assert agent_client(world).post(f"/api/steps/{card['id']}/withdraw", json={}, headers=world.agent()).status_code == 200
    assert _put_step(world, MAIN, "decide:time", "send it Monday at 9?").status_code == 200


def test_a_card_on_another_task_or_beside_a_handed_step_is_not_a_second_card(world: World) -> None:
    _put_item(world, MAIN, "Weekly reminder", sessions=[SESSION_A])
    _put_item(world, SIDE, "Reminder time")
    card = _step(world, MAIN, "review", "send the weekly reminder?")
    # A separate task takes its own card.
    assert _put_step(world, SIDE, "decide:time", "send it Monday at 9?").status_code == 200
    # A handed step is with its agent, not on the user's page: the one-card rule leaves it to the handed-step rule.
    hand_off(world, card["id"])
    refused = _put_step(world, MAIN, "decide:day", "Monday or Tuesday?")
    assert refused.status_code == 409 and "already has an open card" not in refused.json()["detail"]


def test_a_permission_ask_or_a_card_closing_after_the_timer_is_not_a_second_card(world: World) -> None:
    _put_item(world, MAIN, "Weekly reminder", sessions=[SESSION_A])
    card = _step(world, MAIN, "review", "send the weekly reminder?")
    # A permission ask is answered with Allow; it is not a card about the work.
    allow = _put_step(world, MAIN, "allow:ls", "Let the session list build files",
                      permission={"list": "allow", "rule": "Bash(ls build *)"})
    assert allow.status_code == 200, allow.text
    # The user is on the card, so the agent's close of it waits for the timer; the card is gone once the user
    # pauses, and the task's next card goes through now.
    assert world.client.post(f"/api/steps/{card['id']}/timer/start", headers=world.user()).status_code == 200
    closing = agent_client(world).post(f"/api/steps/{card['id']}/done", json={"note": "sent"}, headers=world.agent())
    assert closing.status_code == 200 and closing.json()["held"], closing.text
    assert _put_step(world, MAIN, "decide:time", "send it Monday at 9?").status_code == 200


@pytest.mark.parametrize("act", ["message", "press"])
def test_the_merge_line_stays_until_the_user_acts_on_the_joined_task(world: World, act: str) -> None:
    main, _, main_card, _ = _two_tasks(world)
    assert _merge(world).status_code == 200
    run_sql(world, "UPDATE item_merges SET at = ? WHERE item_id = ?", (BEFORE, main["id"]))
    assert len(_state_item(world, main["id"])["merges"]) == 1

    if act == "message":
        run_sql(world, "INSERT INTO messages (author, item_id, step_id, text, state, created_at, sent_at)"
                " VALUES ('user', ?, ?, 'Monday at 10', 'sent', ?, ?)", (main["id"], main_card["id"], db.now_iso(), db.now_iso()))
    else:
        assert world.client.post(f"/api/steps/{main_card['id']}/done", json={}, headers=world.user()).status_code == 200

    assert _state_item(world, main["id"])["merges"] == []


def test_the_history_says_which_task_a_moved_step_came_from(world: World) -> None:
    main, side, main_card, side_card = _two_tasks(world)
    assert _merge(world).status_code == 200

    steps = world.client.get(f"/api/items/{main['id']}/history").json()["steps"]

    assert steps[str(side_card["id"])]["merged_from"] == {"item_id": side["id"], "title": "Reminder time"}
    assert steps[str(main_card["id"])]["merged_from"] is None


def test_the_cli_merge_says_what_moved_and_the_next_move(world: World, monkeypatch, capsys) -> None:
    main, side, main_card, _ = _two_tasks(world)
    route_cli_to(world, monkeypatch)

    assert cli.main(["item", "merge", SIDE, "--into", MAIN, "--note", NOTE, "--as", "two-cards"]) == 0

    out = capsys.readouterr().out
    assert f"merged: item {side['id']} into item {main['id']} (Weekly reminder); 1 steps moved." in out
    assert f"step {main_card['id']} (review, open): send the weekly reminder?" in out
    assert f"Next: one card on item {main['id']} covers both tasks." in out


def test_a_held_merge_says_it_waits_in_the_cli(world: World, monkeypatch, capsys) -> None:
    _, side, main_card, _ = _two_tasks(world)
    assert world.client.post(f"/api/steps/{main_card['id']}/timer/start", headers=world.user()).status_code == 200
    route_cli_to(world, monkeypatch)

    assert cli.main(["item", "merge", SIDE, "--into", MAIN, "--note", NOTE, "--as", "two-cards"]) == 0

    assert capsys.readouterr().out.startswith("waits: ")
    conn = db.connect(world.db_path)
    try:
        assert store.get_item(conn, side["id"])["merged_into"] is None
    finally:
        conn.close()


@pytest.mark.parametrize("held", [False, True])
def test_a_snoozed_side_card_closes_with_the_merge(world: World, held: bool) -> None:
    main, side, main_card, side_card = _two_tasks(world)
    snoozed = world.client.post(f"/api/steps/{side_card['id']}/snooze", json={"until": "2099-01-01T08:00:00Z"}, headers=world.user())
    assert snoozed.status_code == 200, snoozed.text
    if held:
        assert world.client.post(f"/api/steps/{main_card['id']}/timer/start", headers=world.user()).status_code == 200

    merged = _merge(world)

    assert merged.status_code == 200, merged.text
    if held:
        assert world.client.post(f"/api/steps/{main_card['id']}/timer/pause", headers=world.user()).status_code == 200
    assert _merged_into(world, side["id"]) == main["id"]
    card = _get(world, "SELECT withdrawn_at, snoozed_until FROM steps WHERE id = ?", side_card["id"])[0]
    assert card["withdrawn_at"] is not None and card["snoozed_until"] is None
    assert _state_item(world, main["id"])["merges"][0]["moved_steps"] == [{"id": side_card["id"], "ask": "send it Monday at 9?"}]


def test_a_held_merge_that_can_no_longer_apply_is_an_error(world: World, caplog) -> None:
    main, side, main_card, _ = _two_tasks(world)
    assert world.client.post(f"/api/steps/{main_card['id']}/timer/start", headers=world.user()).status_code == 200
    assert _merge(world).json()["held"]
    run_sql(world, "UPDATE items SET archived = 1 WHERE id = ?", (main["id"],))

    assert world.client.post(f"/api/steps/{main_card['id']}/timer/pause", headers=world.user()).status_code == 200

    assert _merged_into(world, side["id"]) is None
    errors = [record.getMessage() for record in caplog.records if record.levelname == "ERROR"]
    assert [f"merge of item {side['id']} into item {main['id']}" in message for message in errors] == [True]
