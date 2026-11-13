from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from cockpit import cli
from tests.conftest import ORIGIN, World


def send(world: World, uses: list[dict[str, Any]], batch: str) -> Any:
    return world.client.post("/api/control-uses", json={"batch": batch, "page": "/", "uses": uses}, headers=world.user())


def use(control: str, kind: str, count: int = 1) -> dict[str, Any]:
    return {"control": control, "kind": kind, "count": count}


def test_presses_add_up_and_seen_counts_once_a_day(world: World) -> None:
    assert send(world, [use('card step-keys "Snooze"', "press", 2), use('card step-keys "Snooze"', "seen")], "b1").status_code == 200
    send(world, [use('card step-keys "Snooze"', "press", 3), use('card step-keys "Snooze"', "seen"),
                 use("header button", "seen"), use("card answer", "hover")], "b2")
    report = world.client.get("/api/control-uses?days=7").json()
    by_name = {c["control"]: c for c in report["controls"]}
    assert by_name['card step-keys "Snooze"']["press"] == 5
    assert by_name['card step-keys "Snooze"']["seen"] == 1
    assert by_name["header button"] == by_name["header button"] | {"press": 0, "hover": 0, "seen": 1}
    assert by_name["card answer"]["hover"] == 1
    assert report["controls"][0]["control"] == 'card step-keys "Snooze"'


def test_the_report_prints_one_line_per_control(world: World) -> None:
    send(world, [use("work/row chip", "press", 4)], "b1")
    text = cli.control_uses_text(world.client.get("/api/control-uses?days=30").json())
    assert "     4      0     0  /  work/row chip" in text


def test_only_the_users_page_records_uses(world: World) -> None:
    agent = TestClient(world.client.app, base_url=ORIGIN)
    body = {"batch": "b1", "page": "/", "uses": [use("x", "press")]}
    assert agent.post("/api/control-uses", json=body, headers=world.agent()).status_code == 403


def test_a_batch_sent_again_counts_once(world: World) -> None:
    assert send(world, [use("header button", "press", 2)], "b1").json() == {"recorded": 1}
    assert send(world, [use("header button", "press", 2)], "b1").json() == {"recorded": 0}
    report = world.client.get("/api/control-uses?days=1").json()
    assert report["controls"][0]["press"] == 2
