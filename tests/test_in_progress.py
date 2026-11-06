"""The sweeper's list of the work in progress that matters most (SPEC VIEW-17): what a put accepts, and what the
page gets once the work closes or comes to the user."""

from __future__ import annotations

import io

from fastapi.testclient import TestClient

from cockpit import cli, store
from tests.conftest import ORIGIN, World
from tests.test_cli import route_cli_to

LOOK_AGAIN = "2026-02-05T00:00:00Z"


def put_item(world: World, key: str, **fields) -> int:
    agent = TestClient(world.client.app, base_url=ORIGIN)
    response = agent.put("/api/items", json={"key": key, "title": key.title()} | fields, headers=world.agent())
    assert response.status_code == 200, response.text
    return response.json()["id"]


def put_list(world: World, entries: list[dict]) -> tuple[int, dict]:
    agent = TestClient(world.client.app, base_url=ORIGIN)
    response = agent.put("/api/in-progress", json={"entries": entries}, headers=world.agent())
    return response.status_code, response.json()


def shown(world: World) -> list[dict]:
    return world.client.get("/api/state").json()["in_progress"]


def test_the_list_shows_in_order_until_its_work_closes_or_comes_to_the_user(world: World) -> None:
    assert shown(world) == []
    review = put_item(world, "note:review", stage="review", waiting_on=[{"kind": "bot", "until": LOOK_AGAIN, "who": "review-bot"}])
    release = put_item(world, "note:release", stage="staging", waiting_on=[{"kind": "release", "until": LOOK_AGAIN, "who": "prod release"}])
    entries = [{"item_id": release, "line": "Waits on Tuesday's prod release"}, {"item_id": review, "line": " review-bot reviews it "}]
    assert put_list(world, entries) == (200, {"entries": [entries[0], {"item_id": review, "line": "review-bot reviews it"}]})
    assert shown(world) == [entries[0], {"item_id": review, "line": "review-bot reviews it"}]

    # A step for the user brings the review to them: it leaves the list.
    agent = TestClient(world.client.app, base_url=ORIGIN)
    step = {"item_key": "note:review", "need": "merge", "ask": "Merge it", "answers": ["Merge it"], "score": 60}
    assert agent.post("/api/steps", json=step, headers=world.agent()).status_code == 200
    assert shown(world) == [entries[0]]
    # The release goes out: done work leaves too.
    world.client.put("/api/items", json={"key": "note:release", "stage": "done"}, headers=world.user())
    assert shown(world) == []
    # An empty put clears the list.
    assert put_list(world, []) == (200, {"entries": []})


def test_a_put_names_what_is_wrong_and_keeps_the_list(world: World) -> None:
    open_item = put_item(world, "note:open", stage="building")
    done = put_item(world, "note:done")
    world.client.put("/api/items", json={"key": "note:done", "stage": "done"}, headers=world.user())
    archived = put_item(world, "note:archived", stage="building")
    world.client.put("/api/items", json={"key": "note:archived", "archived": True}, headers=world.user())
    assert put_list(world, [{"item_id": open_item, "line": "A session builds it"}])[0] == 200
    line = {"line": "Waits"}
    for entries, words in (
        ([{"item_id": open_item, "line": "x" * (store.IN_PROGRESS_LINE_MAX + 1)}], "one line of 1 to 90"),
        ([{"item_id": open_item, "line": "two\nlines"}], "one line"),
        ([{"item_id": open_item, "line": "two\rlines"}], "one line"),
        ([{"item_id": open_item, "line": "  "}], "one line"),
        ([{"item_id": done} | line], "is done"),
        ([{"item_id": archived} | line], "is archived"),
        ([{"item_id": open_item} | line, {"item_id": open_item} | line], "listed twice"),
        ([{"item_id": open_item} | line] * (store.IN_PROGRESS_MAX + 1), "at most 5"),
    ):
        status, body = put_list(world, entries)
        assert status == 400 and words in body["detail"], (entries, body)
    assert put_list(world, [{"item_id": 999, "line": "Waits"}])[0] == 404
    for item_id in (True, 2**63, 0):
        assert put_list(world, [{"item_id": item_id, "line": "Waits"}])[0] == 400, item_id
    assert shown(world) == [{"item_id": open_item, "line": "A session builds it"}]


def test_the_cli_puts_the_list_from_stdin_and_prints_it(world: World, monkeypatch, capsys) -> None:
    review = put_item(world, "note:review", stage="review", waiting_on=[{"kind": "bot", "until": LOOK_AGAIN, "who": "review-bot"}])
    route_cli_to(world, monkeypatch)
    monkeypatch.setattr("sys.stdin", io.StringIO(f'[{{"item_id": {review}, "line": "review-bot reviews it"}}]'))
    assert cli.main(["progress", "put", "--as", "sweep"]) == 0
    assert "stored the list (1 of at most 5)" in capsys.readouterr().out
    assert cli.main(["progress", "get"]) == 0
    assert capsys.readouterr().out.strip() == f"{review} Note:Review: review-bot reviews it"
