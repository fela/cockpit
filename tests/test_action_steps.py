"""A step that asks the user to let the session act offers that action as an answer (SPEC NEXT-21)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import ORIGIN, World

ITEM = "gh:example-org/app#20"


def put(world: World, **fields):
    agent = TestClient(world.client.app, base_url=ORIGIN)
    return agent.post("/api/steps", json={"item_key": ITEM} | fields, headers=world.agent())


def test_a_merge_or_approve_step_needs_an_answer_that_does_the_action(world: World) -> None:
    agent = TestClient(world.client.app, base_url=ORIGIN)
    agent.put("/api/items", json={"key": ITEM, "title": "PR 20"}, headers=world.agent())

    refused = put(world, need="merge", ask="Merge the export PR?")
    assert refused.status_code == 400
    assert '"Merge it"' in refused.json()["detail"]
    assert put(world, need="approve-description:20", ask="Approve the description?").status_code == 400

    step = put(world, need="merge", ask="Merge the export PR?", answers=["Merge it", "I merged it on GitHub"]).json()["step"]
    assert put(world, need="merge", ask="Merge the export PR now?").status_code == 200  # the stored answers stay
    assert put(world, need="merge", answers=[]).status_code == 400

    agent.post(f"/api/steps/{step['id']}/withdraw", json={}, headers=world.agent())
    assert put(world, need="merge", ask="Merge the export PR?").status_code == 400  # it comes back as new

    assert put(world, need="merged-check", ask="Is the merge on dev?").status_code == 200
    assert put(world, need="decide:order", ask="Change the job order?").status_code == 200


def test_a_merge_step_has_no_closing_answer(world: World) -> None:
    """: after the merge the session follows the change to prod, so "I merged it" hands the step off."""
    agent = TestClient(world.client.app, base_url=ORIGIN)
    agent.put("/api/items", json={"key": ITEM, "title": "PR 20"}, headers=world.agent())
    closing = {"text": "I merged it on GitHub; close", "closes": True}

    refused = put(world, need="merge", ask="Merge the export PR?", answers=["Merge it", closing])
    assert refused.status_code == 400
    assert "configured release stages" in refused.json()["detail"]
    assert put(world, need="merge:dev", ask="Merge the export PR?", answers=["Merge it", closing]).status_code == 400

    step = put(world, need="merge", ask="Merge the export PR?", answers=["Merge it", "I merged it on GitHub"]).json()["step"]
    assert [answer["closes"] for answer in step["answers"]] == [False, False]
    assert put(world, need="merge", answers=["Merge it", closing]).status_code == 400  # also on an update

    approve = put(world, need="approve-description:20", ask="Approve the description?", answers=["Approve it", closing])
    assert approve.status_code == 200
