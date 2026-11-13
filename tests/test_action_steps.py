"""A step that asks the user to let the session act offers that action as an answer (SPEC NEXT-21)."""

from __future__ import annotations

import json
import sqlite3

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

    checked = put(world, need="merged-check", ask="Is the merge on dev?")
    assert checked.status_code == 200
    # One task shows one card (SPEC NEXT-55), so the check goes before the next ask.
    agent.post(f"/api/steps/{checked.json()['step']['id']}/withdraw", json={}, headers=world.agent())
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

    # One task shows one card (SPEC NEXT-55), so the merge step goes before the approve step.
    agent.post(f"/api/steps/{step['id']}/withdraw", json={}, headers=world.agent())
    approve = put(world, need="approve-description:20", ask="Approve the description?", answers=["Approve it", closing])
    assert approve.status_code == 200


def test_an_answer_that_keeps_the_task_open_does_not_start_with_done(world: World) -> None:
    """: it would stand beside Task done as a second key that says done (SPEC NEXT-59)."""
    agent = TestClient(world.client.app, base_url=ORIGIN)
    agent.put("/api/items", json={"key": ITEM, "title": "PR 20"}, headers=world.agent())

    for text in ("Done; sharing works", "done, something fails", "All done"):
        refused = put(world, need="do:share", ask="Add the setting, then share the page", answers=[text])
        assert refused.status_code == 400, text
        assert "closes: true" in refused.json()["detail"]

    closing = {"text": "Done; sharing works", "closes": True}
    answers = [closing, "Something doesn't work", "I added it; carry on", "Donegal is fine"]
    step = put(world, need="do:share", ask="Add the setting, then share the page", answers=answers).json()["step"]
    assert [answer["closes"] for answer in step["answers"]] == [True, False, False, False]
    assert put(world, need="do:share", answers=["Done; carry on"]).status_code == 400  # also on an update

    # A step written before the rule keeps its answers until a put sends answers again.
    with sqlite3.connect(world.db_path) as conn:
        stored = json.dumps([{"text": "Done; carry on", "closes": False}])
        conn.execute("UPDATE steps SET answers = ? WHERE id = ?", (stored, step["id"]))
    assert put(world, need="do:share", ask="Add the setting, then share it").status_code == 200
