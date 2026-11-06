"""To-do items (SPEC CONTROL-16): an agent marks an item that is clearly a plain to-do, as Add does."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import ORIGIN, World


def test_an_agent_marks_a_new_item_as_a_to_do_and_its_first_step_asks_what_needs_doing(world: World) -> None:
    agent = TestClient(world.client.app, base_url=ORIGIN)
    body = {"key": "note:renew-passport", "title": "Renew the passport", "todo": True}
    response = agent.put("/api/items", json=body, headers=world.agent())
    assert response.status_code == 200, response.text
    assert response.json()["todo"] == 1
    step = {"item_key": "note:renew-passport", "need": "define", "ask": "What does renewing the passport take?"}
    assert agent.post("/api/steps", json=step, headers=world.agent()).status_code == 200

    item = next(item for item in world.client.get("/api/state").json()["items"] if item["key"] == "note:renew-passport")
    assert item["todo"] == 1
    # It has its step, so it does not wait for the sweeper as an Add with no session does.
    assert item["untriaged"] is False

    # A later put that leaves `todo` out keeps it; one that sends false unmarks it.
    agent.put("/api/items", json={"key": "note:renew-passport", "summary": "Form found"}, headers=world.agent())
    assert agent.get("/api/items", params={"key": "note:renew-passport"}, headers=world.agent()).json()["todo"] == 1
    agent.put("/api/items", json={"key": "note:renew-passport", "todo": False}, headers=world.agent())
    assert agent.get("/api/items", params={"key": "note:renew-passport"}, headers=world.agent()).json()["todo"] == 0
