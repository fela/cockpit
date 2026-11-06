from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from cockpit import cli, db
from tests.conftest import World
from tests.test_api import agent_client, put_item, put_step
from tests.test_permission_requests import report


def stamp(seconds_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def flash(world: World, kind: str, ref: Any, shown_ago: float, gone_ago: float, label: str = "a card") -> Any:
    body = {"kind": kind, "ref": str(ref), "label": label, "page": "/", "shown_at": stamp(shown_ago),
            "gone_at": stamp(gone_ago)}
    return world.client.post("/api/flashes", json=body, headers=world.user())


def caught(world: World) -> list[dict[str, Any]]:
    return world.client.get("/api/flashes?days=1").json()["flashes"]


def test_a_refusal_withdrawn_soon_after_it_showed_is_kept_once_with_what_took_it_away(world: World) -> None:
    request = report(world, "denied", {"command": "rm -rf build", "description": "Remove the build"})
    conn = db.connect(world.db_path)
    with db.transaction(conn):
        conn.execute("UPDATE permission_requests SET state = 'withdrawn' WHERE id = ?", (request["id"],))
    first = flash(world, "request", request["id"], 20, 8, "Remove the build")
    assert first.status_code == 200, first.text
    # A second tab saw the same card go.
    again = flash(world, "request", request["id"], 18, 6, "Remove the build")
    assert again.json()["id"] == first.json()["id"]
    [kept] = caught(world)
    assert kept["outcome"] == "withdrawn: the session got past it without the user"
    assert "request" in cli.flashes_text([kept]) and "gone after 12 s" in cli.flashes_text([kept])
    assert "1 cards left Sam's page within a minute of showing: `cockpit flashes --days 1`" in (
        world.client.get("/api/patterns?days=1").json()["repeats"]
    )


def test_a_step_an_agent_withdrew_is_kept_and_one_the_user_closed_is_left_out(world: World) -> None:
    put_item(world)
    withdrawn = put_step(world, need="decide:a", ask="Keep the export?")["step"]
    closed = put_step(world, need="decide:b", ask="Ship it?")["step"]
    agent_client(world).post(f"/api/steps/{withdrawn['id']}/withdraw", json={}, headers=world.agent("app-pr"))
    world.client.post(f"/api/steps/{closed['id']}/done", json={}, headers=world.user())
    assert flash(world, "step", withdrawn["id"], 30, 2).json()["id"] is not None
    assert flash(world, "step", closed["id"], 30, 2).json() == {"id": None}
    [kept] = caught(world)
    assert (kept["ref"], kept["outcome"]) == (str(withdrawn["id"]), "withdrawn; last change: step.withdraw by agent:app-pr")


def test_a_step_the_user_took_and_an_agent_then_withdrew_is_kept(world: World) -> None:
    put_item(world)
    # Four steps fill the queue, so the fifth waits in Ready next until the user takes it.
    step = [put_step(world, need=f"decide:{n}", ask="Keep the export?", score=60 - n)["step"] for n in range(5)][-1]
    assert world.client.post(f"/api/steps/{step['id']}/take", json={}, headers=world.user()).status_code == 200
    withdrawn = agent_client(world).post(f"/api/steps/{step['id']}/withdraw", json={}, headers=world.agent("app-pr"))
    assert withdrawn.status_code == 200, withdrawn.text
    assert flash(world, "step", step["id"], 30, 2).json()["id"] is not None
    [kept] = caught(world)
    assert (kept["ref"], kept["outcome"]) == (str(step["id"]), "withdrawn; last change: step.withdraw by agent:app-pr")


def test_a_step_the_user_woke_and_an_agent_then_withdrew_is_kept(world: World) -> None:
    put_item(world)
    step = put_step(world, need="decide:a", ask="Keep the export?")["step"]
    later = (datetime.now(UTC) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    world.client.post(f"/api/steps/{step['id']}/snooze", json={"until": later}, headers=world.user())
    assert flash(world, "step", step["id"], 30, 2).json() == {"id": None}
    conn = db.connect(world.db_path)
    with db.transaction(conn):
        # The snooze lies outside the next appearance's span.
        conn.execute("UPDATE events SET at = ? WHERE step_id = ? AND kind = 'step.snooze'", (stamp(600), step["id"]))
    world.client.post(f"/api/steps/{step['id']}/snooze", json={"until": None}, headers=world.user())
    withdrawn = agent_client(world).post(f"/api/steps/{step['id']}/withdraw", json={}, headers=world.agent("app-pr"))
    assert withdrawn.status_code == 200, withdrawn.text
    assert flash(world, "step", step["id"], 20, 2).json()["id"] is not None
    [kept] = caught(world)
    assert (kept["ref"], kept["outcome"]) == (str(step["id"]), "withdrawn; last change: step.withdraw by agent:app-pr")


def test_two_dialogs_of_one_session_are_two_flashes(world: World) -> None:
    session = "11111111-1111-4111-8111-111111111111"
    first = flash(world, "session", session, 50, 40).json()["id"]
    second = flash(world, "session", session, 30, 20).json()["id"]
    assert first != second
    assert len(caught(world)) == 2


def test_only_the_users_page_notes_flashes_and_only_brief_ones(world: World) -> None:
    body = {"kind": "session", "ref": "11111111-1111-4111-8111-111111111111", "label": "x", "page": "/",
            "shown_at": stamp(10), "gone_at": stamp(5)}
    assert agent_client(world).post("/api/flashes", json=body, headers=world.agent()).status_code == 403
    assert flash(world, "session", "11111111-1111-4111-8111-111111111111", 300, 5).status_code == 400
    assert flash(world, "step", "abc", 10, 5).status_code == 400
    assert caught(world) == []
