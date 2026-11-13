"""An agent that puts a finish step with no screenshot in its body hears how to place one (SPEC CONTROL-25).
The put stands either way."""

from __future__ import annotations

import json
from io import StringIO
from typing import Any

import pytest

from cockpit import cli
from tests.conftest import SESSION_A, World
from tests.test_api import PNG, REPORT, add_image, add_page, agent_client, put_item

KEY = "note:list-sharing"


def _item(world: World) -> int:
    return put_item(world, key=KEY, title="List sharing", sessions=[SESSION_A])["id"]


def _put(world: World, **fields: Any) -> dict[str, Any]:
    body = {"item_key": KEY, "need": "complete", "ask": "List sharing is ready; check it and close", "size": "small"}
    response = agent_client(world).post("/api/steps", json=body | fields, headers=world.agent("app-pr"))
    assert response.status_code == 200, response.text
    return response.json()


def test_a_finish_step_without_a_screenshot_goes_through_with_a_hint(world: World) -> None:
    item_id = _item(world)
    put = _put(world, body="Alex can now be invited to a list.")
    assert put["step"]["status"] == "open"
    assert "shows no screenshot" in put["hint"]
    assert f"`cockpit image add {item_id} --file PATH --as NAME`" in put["hint"]

    # The item's images are named, so a session that took screenshots and left them unplaced sees which to place.
    first = add_image(world, item_id, PNG, "image/png").json()["id"]
    second = add_image(world, item_id, PNG, "image/png").json()["id"]
    assert f"Item {item_id} has images {first}, {second}." in _put(world, body="Alex can now be invited.")["hint"]


def test_a_placed_screenshot_or_a_linked_page_needs_no_hint(world: World) -> None:
    item_id = _item(world)
    image = add_image(world, item_id, PNG, "image/png").json()["id"]
    shown = _put(world, body=f"Alex can now be invited to a list.\n\n![The invite box with Alex's name](image:{image})")
    assert shown["hint"] is None

    page = add_page(world, item_id, "app-pr", "Tasks by list", REPORT).json()["id"]
    assert _put(world, body=f"The report is ready: [Tasks by list](page:{page}).")["hint"] is None


def test_other_steps_get_no_hint(world: World) -> None:
    _item(world)
    assert _put(world, need="decide:invites", ask="invites work; allow guests too?", body="Guests are off today.")["hint"] is None


def test_the_cli_prints_the_hint_beside_the_put_result(world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _item(world)

    def call(method: str, path: str, body: Any, agent: str) -> Any:
        response = agent_client(world).request(method, path, json=body, headers=world.agent(agent))
        response.raise_for_status()
        return response.json()

    monkeypatch.setattr(cli, "call", call)
    step = {"item_key": KEY, "need": "complete", "ask": "List sharing is ready; check it and close", "size": "small"}
    monkeypatch.setattr(cli.sys, "stdin", StringIO(json.dumps(step | {"body": "Alex can now be invited to a list."})))
    assert cli.main(["step", "put", "--as", "app-pr"]) == 0
    printed = capsys.readouterr()
    assert json.loads(printed.out)["step"]["status"] == "open"
    assert printed.err.startswith("hint: this finish step's body shows no screenshot.")
