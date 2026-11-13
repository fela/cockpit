"""Only the user closes a task (SPEC CONTROL-17): an agent's close of an item becomes a finish step the user presses."""

from __future__ import annotations

import logging
import subprocess
from typing import Any

import pytest

from cockpit import db, delivery, github, rank, store
from tests.conftest import SESSION_A, SESSION_B, World
from tests.test_api import hand_off, queue_ids
from tests.test_complete_task import ITEM, _agent, _finish, _item, _item_state, _step, _step_state, _user_step

LOOK_AGAIN = "2026-02-05T00:00:00Z"


def _put(world: World, **fields: Any) -> dict[str, Any]:
    response = _agent(world).put("/api/items", json={"key": ITEM} | fields, headers=world.agent())
    assert response.status_code == 200, response.text
    return response.json()


def _open_needs(world: World) -> list[str]:
    item = world.client.get("/api/items", params={"key": ITEM}).json()
    return [step["need"] for step in item["steps"] if step["status"] == "open" and not step["withdrawn_at"]]


def _write_finish(world: World, need: str = "complete", ask: str = "List sharing help is ready") -> int:
    """The finish step the session that closes the task writes before its close (SPEC CONTROL-19)."""
    body = {
        "item_key": ITEM, "need": need, "ask": ask, "body": "The sharing help explains how to invite Alex to a list.",
        "size": "small", "answers": [{"text": "Looks good", "closes": True}],
    }
    response = _agent(world).post("/api/steps", json=body, headers=world.agent())
    assert response.status_code == 200, response.text
    return response.json()["step"]["id"]


def _refused_close(world: World, **fields: Any) -> str:
    response = _agent(world).put("/api/items", json={"key": ITEM} | fields, headers=world.agent())
    assert response.status_code == 400, response.text
    return response.json()["detail"]


def test_an_agent_s_close_needs_the_finish_step_it_wrote(world: World) -> None:
    """SPEC CONTROL-19: the session that closes a task writes its finish step; Cockpit no longer writes one around
    the agent's note, and refuses the close until that step is open."""
    item_id = _item(world)
    for close in ({"stage": "done"}, {"archived": True}):
        detail = _refused_close(world, summary="Merged and live on prod", note="merged and released", **close)
        assert "need complete" in detail and "That step asks Sam to close the task" in detail and "GitHub" in detail
    item = _item_state(world, item_id)
    assert (item["stage"], item["archived"], item["summary"]) == ("review", False, "")
    assert _open_needs(world) == []
    finish = _write_finish(world)
    put = _put(world, stage="done", summary="Merged and live on prod", note="merged and released")
    assert put["finish_step"] == finish
    item = _item_state(world, item_id)
    assert (item["stage"], item["archived"], item["summary"]) == ("review", False, "Merged and live on prod")
    # The session's text stays as it wrote it.
    step = _step_state(world, finish)
    assert (step["ask"], step["body"]) == ("List sharing help is ready", "The sharing help explains how to invite Alex to a list.")
    # A second close finds the same step; nothing is closed in the Closed group.
    assert _put(world, stage="done", note="still merged")["finish_step"] == finish
    assert _open_needs(world) == ["complete"]
    assert world.client.get("/api/state").json()["closed_recently"]["items"] == []


def test_a_close_after_a_withdrawn_finish_step_names_that_need(world: World) -> None:
    _item(world)
    finish = _write_finish(world)
    assert _agent(world).post(f"/api/steps/{finish}/withdraw", json={}, headers=world.agent()).status_code == 200
    assert "need complete," in _refused_close(world, stage="done", note="merged")
    # Written again on the same need, it is open, and the close goes through.
    assert _write_finish(world) == finish
    assert _put(world, stage="done", note="merged")["finish_step"] == finish


def test_an_agent_s_archive_needs_the_finish_step_too(world: World) -> None:
    item_id = _item(world)
    finish = _write_finish(world, ask="Answered in Slack; nothing left here")
    put = _put(world, archived=True, note="answered in Slack")
    assert put["finish_step"] == finish and not _item_state(world, item_id)["archived"]
    # The user's press there closes it.
    assert _finish(world, item_id, finish, "close", None, "answered").status_code == 200
    assert _item_state(world, item_id)["archived"]


def test_a_new_item_put_at_stage_done_is_refused(world: World) -> None:
    """A new item has no step yet: the session puts it at the stage the work is in, then its finish step."""
    refusal = _refused_close(world, title="PR 20", stage="done", note="merged before Cockpit saw it")
    assert refusal.startswith("This item does not exist yet") and "need complete," in refusal
    assert world.client.get("/api/items", params={"key": ITEM}).status_code == 404
    # Followed as the refusal says, the item waits on that step for the user's close.
    assert _put(world, title="PR 20", stage="review")["stage"] == "review"
    _write_finish(world)
    assert _open_needs(world) == ["complete"]


def test_after_the_user_s_finish_a_new_close_takes_the_next_finish_need(world: World) -> None:
    item_id = _item(world)
    first = _write_finish(world)
    assert _put(world, stage="done", note="merged")["finish_step"] == first
    assert _finish(world, item_id, first, "complete", None, "").status_code == 200
    assert _item_state(world, item_id)["stage"] == "done"
    # The user finds work left and brings the task back; an agent may not (SPEC CONTROL-27).
    refused = _agent(world).put("/api/items", json={"key": ITEM, "stage": "building"}, headers=world.agent())
    assert refused.status_code == 409, refused.text
    assert world.client.put("/api/items", json={"key": ITEM, "stage": "building"}, headers=world.user()).status_code == 200
    assert "need complete:2," in _refused_close(world, stage="done", note="fix merged")
    again = _write_finish(world, need="complete:2", ask="Follow-up fix merged")
    assert _put(world, stage="done", note="fix merged")["finish_step"] == again


def test_a_note_goes_only_with_a_close(world: World) -> None:
    _item(world)
    refused = _agent(world).put("/api/items", json={"key": ITEM, "summary": "x", "note": "y"}, headers=world.agent())
    assert refused.status_code == 400


def test_an_agent_s_done_on_a_finish_step_it_was_not_handed_is_refused(world: World) -> None:
    _item(world)
    finish = _step(world, "complete", [{"text": "Task done", "closes": True}])
    for action in ("done", "dismiss"):
        refused = _agent(world).post(f"/api/steps/{finish['id']}/{action}", json={"note": "done"}, headers=world.agent())
        assert refused.status_code == 403, refused.text
    assert _step_state(world, finish["id"])["status"] == "open"
    # Withdrawing it, when work is left, stays the agent's.
    assert _agent(world).post(f"/api/steps/{finish['id']}/withdraw", json={}, headers=world.agent()).status_code == 200


def test_an_agent_s_done_on_a_handed_finish_step_returns_it_to_the_user(world: World) -> None:
    item_id = _item(world)
    finish = _step(world, "complete", [{"text": "Looks good", "closes": False}])
    hand_off(world, finish["id"], text="Looks good")
    done = _agent(world).post(f"/api/steps/{finish['id']}/done", json={"note": "the user confirmed it"}, headers=world.agent())
    assert done.status_code == 200, done.text
    assert "back in" in done.json()["returned"]
    step = _step_state(world, finish["id"])
    assert (step["status"], step["handoff_message_id"], step["check_note"]) == ("open", None, "the user confirmed it")
    assert queue_ids(world)[0][0] == finish["id"]
    assert _item_state(world, item_id)["stage"] == "review"


def test_a_handed_finish_step_comes_back_only_with_a_note_on_what_was_done(world: World) -> None:
    """SPEC CONTROL-15: return a finish step while its pull request remains open."""
    item_id = _item(world)
    finish = _step(world, "complete", [{"text": "Task done", "closes": True}])
    hand_off(world, finish["id"], text="Please ask Alex to check the reminder text")
    for action, body in (("return", {}), ("done", {}), ("dismiss", {}), ("return", {"note": "  "})):
        refused = _agent(world).post(f"/api/steps/{finish['id']}/{action}", json=body, headers=world.agent())
        assert refused.status_code == 400, refused.text
        detail = refused.json()["detail"]
        assert "Please ask Alex to check the reminder text" in detail and "waiting_on" in detail
    # Withdrawing it and putting it again would bring it back with no note: the session keeps it instead.
    refused = _agent(world).post(f"/api/steps/{finish['id']}/withdraw", json={}, headers=world.agent())
    assert refused.status_code == 403 and "waiting_on" in refused.json()["detail"]
    # Closing or archiving the item does not hand it back without a note either, and changes nothing on the item.
    for close in ({"stage": "done"}, {"archived": True}):
        refused = _agent(world).put("/api/items", json={"key": ITEM, "summary": "Shipped"} | close, headers=world.agent())
        assert refused.status_code == 400, refused.text
    assert _item_state(world, item_id)["summary"] != "Shipped"
    step = _step_state(world, finish["id"])
    assert step["handoff_message_id"] is not None and finish["id"] not in queue_ids(world)[0]
    # While the PR is pending, the session rewrites the step as what is left; it stays with the session.
    pending = {"item_key": ITEM, "need": "complete", "ask": "The reminder fix waits for the test run"}
    assert _agent(world).post("/api/steps", json=pending, headers=world.agent()).status_code == 200
    assert _step_state(world, finish["id"])["handoff_message_id"] is not None
    returned = _agent(world).post(
        f"/api/steps/{finish['id']}/return", json={"note": "The reminder fix passed its tests"}, headers=world.agent()
    )
    assert returned.status_code == 200, returned.text
    step = _step_state(world, finish["id"])
    assert (step["handoff_message_id"], step["check_note"]) == (None, "The reminder fix passed its tests")
    assert queue_ids(world)[0][0] == finish["id"]
    assert _item_state(world, item_id)["stage"] == "review"


def test_a_note_on_the_return_of_another_handed_step_is_its_check_note(world: World) -> None:
    _item(world)
    step = _step(world, "decide:memo", [])
    hand_off(world, step["id"], text="Which version?")
    rewrite = {"item_key": ITEM, "need": "decide:memo", "ask": "Send version B, which keeps the export?"}
    assert _agent(world).post("/api/steps", json=rewrite, headers=world.agent()).status_code == 200
    assert _agent(world).post(f"/api/steps/{step['id']}/return", json={}, headers=world.agent()).status_code == 200
    hand_off(world, step["id"], text="And the date?")
    rewrite["ask"] = "Send version B on Friday, which keeps the export?"
    assert _agent(world).post("/api/steps", json=rewrite, headers=world.agent()).status_code == 200
    assert _agent(world).post(
        f"/api/steps/{step['id']}/return", json={"note": "answered in the ask"}, headers=world.agent()
    ).status_code == 200
    shown = _step_state(world, step["id"])
    # The agent wrote the step again, so its text stays as the agent wrote it.
    assert (shown["check_note"], shown["ask"], shown["body"]) == (
        "answered in the ask", "Send version B on Friday, which keeps the export?", ""
    )


def test_a_returned_finish_step_opens_with_the_user_s_message_and_the_reply(world: World) -> None:
    """SPEC NEXT-37: put the session's answer on the returned step so the user sees it before acting."""
    item_id = _item(world)
    finish = _step_state(world, _write_finish(world, ask="The weekend list is shared with Alex and Ana"))
    _put(world, stage="done", summary="List sharing is ready", note="The weekend list is shared.")
    hand_off(world, finish["id"], text="Let only Ana edit the weekend list")
    _put(world, summary="Alex and Ana can both edit the weekend list")
    returned = _agent(world).post(
        f"/api/steps/{finish['id']}/done",
        json={"note": "Only Ana can edit the list now. Alex can still read the list."},
        headers=world.agent(),
    )
    assert returned.status_code == 200 and "back in" in returned.json()["returned"], returned.text
    shown = _step_state(world, finish["id"])
    handed = rank.parse_time(shown["updated_at"])
    assert (shown["ask"], shown["why"]) == ("Only Ana can edit the list now", "sweep: Alex can still read the list.")
    assert shown["body"].split("\n\n") == [
        f"**You wrote on {handed.day} {handed:%b}:** Let only Ana edit the weekend list",
        "**Where it stands:** Alex and Ana can both edit the weekend list",
    ]
    assert (shown["status"], shown["handoff_message_id"]) == ("open", None)
    assert queue_ids(world)[0][0] == finish["id"]
    conn = db.connect(world.db_path)
    try:
        kept = conn.execute("SELECT ask FROM step_versions WHERE step_id = ? ORDER BY id DESC", (finish["id"],)).fetchone()
    finally:
        conn.close()
    assert kept[0] == "Only Ana can edit the list now"
    # The same holds when the agent gives it back by closing the item again, and a one-sentence reply has a why
    # of its own.
    hand_off(world, finish["id"], text="Can Alex still read it?")
    _put(world, stage="done", note="Alex can read the weekend list")
    shown = _step_state(world, finish["id"])
    assert (shown["ask"], shown["why"]) == (
        "Alex can read the weekend list", "sweep replied to your message here. Your press closes the task."
    )
    assert shown["body"].startswith(f"**You wrote on {handed.day} {handed:%b}:** Can Alex still read it?\n\n**Where it stands:**")
    assert _item_state(world, item_id)["stage"] == "review"


def test_a_returned_finish_step_shows_a_reply_too_long_for_its_why_whole(world: World) -> None:
    """A short reply belongs in the ask and why; repeat it in the body only when the why truncates it."""
    _item(world)
    finish = _write_finish(world)
    hand_off(world, finish, text="What is left?")
    note = "Nothing is left. " + "The list has the requested sharing settings, and the help text explains how to change them. " * 3
    returned = _agent(world).post(f"/api/steps/{finish}/return", json={"note": note}, headers=world.agent())
    assert returned.status_code == 200, returned.text
    shown = _step_state(world, finish)
    assert shown["ask"] == "Nothing is left" and shown["why"].endswith("…")
    assert f"**sweep's reply:** {note.strip()}" in shown["body"].split("\n\n")


def test_a_returned_finish_step_drops_what_was_left_while_it_was_handed(world: World) -> None:
    """While the user's "not done yet" is pending, the session rewrites the finish step as what is left; its return
    writes the step again from the reply, so that ask does not come back to the user."""
    _item(world)
    finish = _write_finish(world)
    hand_off(world, finish, text="Check the export file first")
    assert _write_finish(world, ask="The export file still needs a check") == finish
    returned = _agent(world).post(f"/api/steps/{finish}/return", json={"note": "The export includes completed tasks."}, headers=world.agent())
    assert returned.status_code == 200, returned.text
    shown = _step_state(world, finish)
    assert (shown["ask"], shown["why"]) == ("The export includes completed tasks", "sweep replied to your message here. Your press closes the task.")
    assert "The export includes completed tasks" not in shown["body"]
    assert "The export file still needs a check" not in shown["body"]


def test_a_returned_finish_step_drops_the_answers_of_the_ask_it_replaces(world: World) -> None:
    """A session that asked "deploy now?" on its finish step and gives it back with "Deployed." leaves no "Deploy now"
    button: the reply is the new ask, so Task done is its key."""
    _item(world)
    finish = _write_finish(world)
    deciding = {
        "item_key": ITEM, "need": "complete", "ask": "Sharing built; deploy before the full tests?",
        "reply_hint": "Say when to deploy",
        "answers": [{"text": "Deploy now", "closes": False}, {"text": "Wait for the full suite", "closes": False}],
    }
    assert _agent(world).post("/api/steps", json=deciding, headers=world.agent()).status_code == 200
    hand_off(world, finish, text="Deploy now")
    returned = _agent(world).post(f"/api/steps/{finish}/return", json={"note": "Deployed."}, headers=world.agent())
    assert returned.status_code == 200, returned.text
    shown = _step_state(world, finish)
    assert (shown["ask"], shown["answers"], shown["reply_hint"]) == ("Deployed", [], "")


def test_another_handed_step_comes_back_only_with_its_answer_on_it(world: World) -> None:
    """SPEC NEXT-37: a step the user asked on comes back with the answer in its first lines. Left as it was, it needs
    the agent's reply as a note, which goes first on it; without one the return is refused with what to do."""
    _item(world)
    step = _step(world, "decide:memo", [])
    hand_off(world, step["id"], text="Which version keeps the export?")
    refused = _agent(world).post(f"/api/steps/{step['id']}/return", json={}, headers=world.agent())
    assert refused.status_code == 400, refused.text
    detail = refused.json()["detail"]
    assert "Which version keeps the export?" in detail and "cockpit step put" in detail and "--note" in detail
    assert _step_state(world, step["id"])["handoff_message_id"] is not None
    returned = _agent(world).post(
        f"/api/steps/{step['id']}/return", json={"note": "Version B keeps it."}, headers=world.agent()
    )
    assert returned.status_code == 200, returned.text
    shown = _step_state(world, step["id"])
    assert shown["ask"] == "Is the reply out?" and shown["handoff_message_id"] is None
    assert shown["body"].split("\n\n")[1:] == ["**sweep's reply:** Version B keeps it."]
    assert shown["body"].startswith("**You wrote on ") and "Which version keeps the export?" in shown["body"]


def test_a_handed_step_rewritten_in_the_second_of_the_hand_off_keeps_its_text(world: World) -> None:
    """All of this runs within one second: the hand-off notes the step's last version, so the agent's put of the
    body after it counts as the answer, and its put before it does not."""
    _item(world)
    step = _step(world, "decide:memo", [])
    rewritten = {"item_key": ITEM, "need": "decide:memo", "body": "Version A drops the export."}
    assert _agent(world).post("/api/steps", json=rewritten, headers=world.agent()).status_code == 200
    hand_off(world, step["id"], text="Which version keeps the export?")
    assert _agent(world).post(f"/api/steps/{step['id']}/return", json={}, headers=world.agent()).status_code == 400
    answered = {"item_key": ITEM, "need": "decide:memo", "body": "Version B keeps the export."}
    assert _agent(world).post("/api/steps", json=answered, headers=world.agent()).status_code == 200
    assert _agent(world).post(f"/api/steps/{step['id']}/return", json={}, headers=world.agent()).status_code == 200
    assert _step_state(world, step["id"])["body"] == "Version B keeps the export."


def test_the_hand_off_of_a_finish_step_tells_the_agent_to_reply_in_the_return_note(world: World) -> None:
    _item(world)
    finish = _step(world, "complete", [])
    message = hand_off(world, finish["id"], text="Did Ana see it?")
    conn = db.connect(world.db_path)
    try:
        delivered = delivery.compose(conn, store.get_message(conn, message["id"]))
    finally:
        conn.close()
    assert f'cockpit step return {finish["id"]} --as <your name> --note "<your reply>"' in delivered
    assert f"cockpit step done {finish['id']}" not in delivered


def test_an_agent_s_done_on_another_handed_step_still_closes_it_as_handled(world: World) -> None:
    item_id = _item(world)
    step = _step(world, "decide:memo", [])
    hand_off(world, step["id"], text="I will write it myself tomorrow")
    assert _agent(world).post(f"/api/steps/{step['id']}/done", json={"note": "the user writes it"}, headers=world.agent()).status_code == 200
    closed = world.client.get("/api/state").json()["closed_recently"]["steps"]
    assert [(entry["id"], entry["closed_by"]) for entry in closed] == [(step["id"], "agent:sweep")]
    # The task stays open: only the user's press closes it.
    item = _item_state(world, item_id)
    assert (item["stage"], item["archived"]) == ("review", False)
    assert SESSION_A in item["sessions"]


def test_an_agent_s_close_of_a_finished_task_asks_nothing_more(world: World) -> None:
    item_id = _item(world)
    first = _write_finish(world)
    assert _finish(world, item_id, first, "complete", None, "").status_code == 200
    put = _put(world, archived=True, note="done and released")
    assert (put["finish_step"], put["archived"], put["stage"]) == (None, False, "done")
    # A close that also names a stage leaves the finished task at stage done.
    put = _put(world, stage="prod", archived=True, note="released")
    assert (put["finish_step"], put["archived"], put["stage"]) == (None, False, "done")
    assert _open_needs(world) == []


def test_an_agent_s_new_close_returns_a_finish_step_the_user_sent_back(world: World) -> None:
    item_id = _item(world)
    first = _write_finish(world)
    hand_off(world, first, text="Not done yet: the release is still to go")
    again = _put(world, stage="done", note="released to prod")
    assert again["finish_step"] == first
    step = _step_state(world, first)
    assert (step["status"], step["handoff_message_id"], step["check_note"]) == ("open", None, "released to prod")
    assert queue_ids(world)[0][0] == first
    assert _item_state(world, item_id)["stage"] == "review"


def test_shipped_work_with_no_next_move_waits_for_the_agent_s_finish_step(world: World) -> None:
    """SPEC STATE-5, an agent's put that leaves an item on prod with nothing waiting and no open step is
    refused until that agent puts the finish step that names the change; Cockpit writes none itself."""
    item_id = _item(world)
    detail = _refused_close(world, stage="prod", waiting_on=[], summary="Reminders have shipped")
    assert "on prod with nothing in waiting_on" in detail and "need complete" in detail
    assert "names the change" in detail and "how you checked it, and a closing answer" in detail
    # Nothing of the refused put is kept.
    assert (_item_state(world, item_id)["stage"], _item_state(world, item_id)["summary"]) == ("review", "")
    assert _open_needs(world) == []
    finish = _write_finish(world, ask="Daily reminders work; check them and close")
    put = _put(world, stage="prod", waiting_on=[], summary="Reminders have shipped")
    assert (put["stage"], put["finish_step"]) == ("prod", None)
    assert _open_needs(world) == ["complete"] and finish in queue_ids(world)[0]
    # A withdrawn finish step's need is the one to put again.
    assert _agent(world).post(f"/api/steps/{finish}/withdraw", json={}, headers=world.agent()).status_code == 200
    assert _put(world, summary="Still on prod")["finish_step"] is None


def test_shipped_work_that_something_holds_goes_through(world: World) -> None:
    _item(world)
    # Waiting on a release, or with a step open, someone has the next move.
    release = [{"kind": "release", "until": LOOK_AGAIN, "who": "Tasks release"}]
    assert _put(world, stage="prod", waiting_on=release)["finish_step"] is None
    try_it = _step(world, "try:prod", [])
    assert _put(world, waiting_on=[])["finish_step"] is None
    assert _open_needs(world) == ["try:prod"]
    # An agent's close of the last open step would leave it with no next move, so it is refused, and the step stays.
    refused = _agent(world).post(f"/api/steps/{try_it['id']}/done", json={"note": "tried"}, headers=world.agent())
    assert refused.status_code == 400 and f"Closing step {try_it['id']} leaves" in refused.json()["detail"]
    assert _open_needs(world) == ["try:prod"]
    # A task shows one card at a time (SPEC NEXT-55), so the refusal has the step withdrawn first. With its finish
    # step put after that, the close goes through.
    assert f"first withdraw step {try_it['id']}" in refused.json()["detail"]
    assert _agent(world).post(f"/api/steps/{try_it['id']}/withdraw", json={}, headers=world.agent()).status_code == 200
    _write_finish(world)
    done = _agent(world).post(f"/api/steps/{try_it['id']}/done", json={"note": "tried"}, headers=world.agent())
    assert done.status_code == 200, done.text
    assert "finish_step" not in done.json() and _open_needs(world) == ["complete"]
    # Closing a step that was already withdrawn removes nothing that held the work, so it is not refused.
    old = _user_step(world, "decide:old", [])
    assert _agent(world).post(f"/api/steps/{old['id']}/withdraw", json={}, headers=world.agent()).status_code == 200
    closed = _agent(world).post(f"/api/steps/{old['id']}/dismiss", json={"note": "gone"}, headers=world.agent())
    assert closed.status_code == 200, closed.text
    # The user's own edit is not refused either.
    other = {"key": "gh:example-org/app#30", "title": "PR 30", "stage": "prod"}
    assert world.client.put("/api/items", json=other, headers=world.user()).json()["finish_step"] is None


def test_an_agent_s_refused_close_is_refused_while_the_user_is_on_the_step(world: World) -> None:
    """The close is checked before it is held, so the agent hears the refusal at once, not a held close that is
    dropped later."""
    _item(world)
    try_it = _step(world, "try:prod", [])
    _put(world, stage="prod", waiting_on=[])
    assert world.client.post(f"/api/steps/{try_it['id']}/timer/start", headers=world.user()).status_code == 200
    refused = _agent(world).post(f"/api/steps/{try_it['id']}/done", json={}, headers=world.agent())
    assert refused.status_code == 400, refused.text
    conn = db.connect(world.db_path)
    assert conn.execute("SELECT COUNT(*) FROM held_changes").fetchone()[0] == 0
    conn.close()
    assert _open_needs(world) == ["try:prod"]


def test_a_held_close_counts_as_closed(world: World) -> None:
    """A held close counts as closed."""
    _item(world)
    first = _step(world, "try:prod", [])
    second = _user_step(world, "decide:docs", [])  # only the user's own put adds a second card (SPEC NEXT-55)
    _put(world, stage="prod", waiting_on=[])
    assert world.client.post(f"/api/steps/{first['id']}/timer/start", headers=world.user()).status_code == 200
    held = _agent(world).post(f"/api/steps/{first['id']}/done", json={"note": "tried"}, headers=world.agent())
    assert held.status_code == 200 and held.json()["held"], held.text
    refused = _agent(world).post(f"/api/steps/{second['id']}/done", json={}, headers=world.agent())
    assert refused.status_code == 400 and f"Closing step {second['id']} leaves" in refused.json()["detail"]
    assert _agent(world).post(f"/api/steps/{second['id']}/withdraw", json={}, headers=world.agent()).status_code == 200
    _write_finish(world)
    assert _agent(world).post(f"/api/steps/{second['id']}/done", json={}, headers=world.agent()).status_code == 200
    assert world.client.post(f"/api/steps/{first['id']}/timer/pause", headers=world.user()).status_code == 200
    assert _open_needs(world) == ["complete"]


def test_finish_steps_take_freed_places_by_score(world: World) -> None:
    """SPEC RANK-18: the queue keeps four places; finish steps get no place of their own."""
    _item(world)
    strong = _agent(world).post(
        "/api/steps", json={"item_key": ITEM, "need": "review", "ask": "Review it?", "score": 95}, headers=world.agent()
    ).json()["step"]
    shipped = []
    for n in range(5):
        body = {"key": f"note:shipped-{n}", "title": f"Shipped {n}", "stage": "prod"}
        refused = _agent(world).put("/api/items", json=body, headers=world.agent())
        assert refused.status_code == 400 and "Put the item first at the stage before prod" in refused.text
        assert _agent(world).put("/api/items", json=body | {"stage": "staging"}, headers=world.agent()).status_code == 200
        finish = {"item_key": body["key"], "need": "complete", "ask": f"Change {n} is live; check it and close"}
        put = _agent(world).post("/api/steps", json=finish, headers=world.agent())
        assert put.status_code == 200, put.text
        shipped.append(put.json()["step"]["id"])
        assert _agent(world).put("/api/items", json=body, headers=world.agent()).status_code == 200
    queue, ready = queue_ids(world)
    assert queue == [strong["id"], *shipped[:3]] and ready == shipped[3:]
    # A freed place goes to the highest score waiting, here the older of two at the default.
    assert world.client.post(f"/api/steps/{strong['id']}/dismiss", json={}, headers=world.user()).status_code == 200
    queue, ready = queue_ids(world)
    assert shipped[3] in queue and ready == [shipped[4]]


def test_an_agent_s_close_of_a_merged_pr_closes_the_item_with_a_note(world: World, merged_pulls: set[str]) -> None:
    """SPEC CONTROL-15: a merge needs no finish step; the user's next step names it until they act."""
    item_id = _item(world)
    # A finish step from an earlier close is closed with it.
    earlier = _write_finish(world, ask="app#20 approved; Alex merges")
    merged_pulls.add("example-org/app#20")
    put = _put(world, stage="done", note="app#20 merged")
    assert (put["stage"], put["finish_step"], put["merged"]) == ("done", None, "example-org/app#20")
    assert _item_state(world, item_id)["stage"] == "done" and _open_needs(world) == []
    assert _step_state(world, earlier)["status"] == "done"
    state = world.client.get("/api/state").json()
    assert [(entry["id"], entry["note"]) for entry in state["closed_recently"]["items"]] == [(item_id, "app#20 merged")]
    assert [(entry["item_id"], entry["pull"], entry["title"], entry["closed_by"]) for entry in state["merged_closes"]] == [
        (item_id, "example-org/app#20", "PR 20", "agent:sweep"),
    ]
    # The user's next action on a step clears the line, also in the second the merge closed it.
    other = {"key": "note:other", "title": "Other", "stage": "review"}
    assert _agent(world).put("/api/items", json=other, headers=world.agent()).status_code == 200
    step = _agent(world).post("/api/steps", json={"item_key": "note:other", "need": "decide:x", "ask": "X?"}, headers=world.agent())
    assert world.client.post(f"/api/steps/{step.json()['step']['id']}/dismiss", json={}, headers=world.user()).status_code == 200
    assert world.client.get("/api/state").json()["merged_closes"] == []


def test_a_merge_does_not_close_a_finish_step_the_user_handed_back(world: World, merged_pulls: set[str]) -> None:
    """SPEC CONTROL-15: the merge is not all the user asked when they handed the finish step back; the step comes
    back only with a note on what was done, as any other close of the item."""
    item_id = _item(world)
    finish = _step(world, "complete", [{"text": "Task done", "closes": True}])
    hand_off(world, finish["id"], text="Merge the export fix and update its help text")
    merged_pulls.add("example-org/app#20")
    refused = _agent(world).put("/api/items", json={"key": ITEM, "stage": "done"}, headers=world.agent())
    assert refused.status_code == 400 and "update its help text" in refused.json()["detail"]
    step = _step_state(world, finish["id"])
    assert (step["status"], step["handoff_message_id"] is not None) == ("open", True)
    assert _item_state(world, item_id)["stage"] == "review"
    put = _put(world, stage="done", note="app#20 merged and on prod; help text updated")
    assert (put["merged"], put["finish_step"], put["stage"]) == (None, finish["id"], "review")
    step = _step_state(world, finish["id"])
    assert (step["status"], step["handoff_message_id"], step["check_note"]) == (
        "open", None, "app#20 merged and on prod; help text updated"
    )




def test_the_merge_line_shows_each_item_once_and_goes_on_a_take(world: World, merged_pulls: set[str]) -> None:
    item_id = _item(world)
    merged_pulls.add("example-org/app#20")
    assert _put(world, stage="done", note="app#20 merged")["merged"]
    # The user takes the item back to work and the agent closes it again: one line, for the latest close.
    assert world.client.put("/api/items", json={"key": ITEM, "stage": "building"}, headers=world.user()).status_code == 200
    assert _put(world, stage="done", note="app#20 merged, follow-up included")["merged"]
    assert [entry["item_id"] for entry in world.client.get("/api/state").json()["merged_closes"]] == [item_id]
    # Taking a ready-next step to the top is an action on a step too.
    for n in range(5):
        other = {"key": f"note:other-{n}", "title": f"Other {n}"}
        assert _agent(world).put("/api/items", json=other, headers=world.agent()).status_code == 200
        _agent(world).post("/api/steps", json={"item_key": other["key"], "need": "look", "ask": "X?"}, headers=world.agent())
    ready = queue_ids(world)[1]
    assert world.client.post(f"/api/steps/{ready[0]}/take", json={}, headers=world.user()).status_code == 200
    assert world.client.get("/api/state").json()["merged_closes"] == []


def test_a_merge_of_a_pr_the_item_no_longer_names_closes_nothing(
    world: World, merged_pulls: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """GitHub is asked before the put's transaction; a put in between that changes the item's PR wins."""
    item_id = _item(world)
    other_pr = {"label": "app#30", "url": "https://github.com/example-org/app/pull/30"}
    note_item = {"key": "note:linked", "title": "Linked", "stage": "review", "links": [{"label": "app#20", "url": "https://github.com/example-org/app/pull/20"}]}
    assert _agent(world).put("/api/items", json=note_item, headers=world.agent()).status_code == 200
    merged_pulls.add("example-org/app#20")

    def relinked(ref: str) -> bool:
        assert _agent(world).put("/api/items", json={"key": "note:linked", "links": [other_pr]}, headers=world.agent()).status_code == 200
        return ref in merged_pulls

    monkeypatch.setattr(github, "pull_merged", relinked)
    refused = _agent(world).put("/api/items", json={"key": "note:linked", "stage": "done", "note": "merged"}, headers=world.agent())
    assert refused.status_code == 400 and "No finish step is open" in refused.json()["detail"]
    linked = world.client.get("/api/items", params={"key": "note:linked"}).json()
    assert (linked["stage"], linked["links"]) == ("review", [other_pr])
    assert _item_state(world, item_id)["stage"] == "review"


def test_github_failures_are_github_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any failure to learn whether a PR merged is a GitHubError, which the put turns into a finish step."""
    answers = iter([
        subprocess.CompletedProcess([], 0, stdout="null\n", stderr=""),
        subprocess.CompletedProcess([], 0, stdout="not json", stderr=""),
        subprocess.CompletedProcess([], 1, stdout="", stderr="HTTP 502"),
        subprocess.CompletedProcess([], 0, stdout='{"__typename": "PullRequest", "merged": true}', stderr=""),
    ])

    def run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return next(answers)

    monkeypatch.setattr(github.subprocess, "run", run)
    for _ in range(3):
        with pytest.raises(github.GitHubError):
            github._number(github.MERGED_QUERY, "example-org/app#20")
    assert github._number(github.MERGED_QUERY, "example-org/app#20")["merged"] is True

    def missing(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("gh")

    monkeypatch.setattr(github.subprocess, "run", missing)
    with pytest.raises(github.GitHubError):
        github._number(github.MERGED_QUERY, "example-org/app#20")


def test_a_merged_pr_with_a_step_still_open_waits_for_that_step(world: World, merged_pulls: set[str]) -> None:
    """The open step is the task's one card (SPEC NEXT-55), so no finish step goes beside it: the merge closes the
    task once that step closes."""
    item_id = _item(world)
    reply = _step(world, "reply:1", [])
    merged_pulls.add("example-org/app#20")
    refused = _refused_close(world, stage="done", note="app#20 merged")
    assert "no other step is open" in refused and f"Step {reply['id']} (need reply:1) is its open card" in refused
    assert _put_finish(world).status_code == 409
    assert _item_state(world, item_id)["stage"] == "review"
    assert world.client.get("/api/state").json()["merged_closes"] == []
    assert _agent(world).post(f"/api/steps/{reply['id']}/done", json={"note": "sent"}, headers=world.agent()).status_code == 200
    put = _put(world, stage="done", note="app#20 merged")
    assert (put["merged"], put["finish_step"]) == ("example-org/app#20", None)
    assert _item_state(world, item_id)["stage"] == "done"


def test_when_github_does_not_answer_the_close_needs_a_finish_step(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def fails(ref: str) -> bool:
        raise github.GitHubError("gh exited with 1")

    monkeypatch.setattr(github, "pull_merged", fails)
    item_id = _item(world)
    with caplog.at_level(logging.ERROR):
        assert "GitHub confirms the merge" in _refused_close(world, stage="done", note="app#20 merged")
    assert "could not ask GitHub whether example-org/app#20 merged" in caplog.text
    finish = _write_finish(world, ask="app#20 merged and on dev")
    put = _put(world, stage="done", note="app#20 merged")
    assert (put["merged"], put["finish_step"]) == (None, finish)
    assert _item_state(world, item_id)["stage"] == "review"


def _handed_on_prod(world: World) -> dict[str, Any]:
    """An item on prod with nothing waiting, whose one open step the user handed to its session, and the message has
    reached that session."""
    _item(world)
    step = _step(world, "try:prod", [])
    assert _put(world, stage="prod", waiting_on=[])["finish_step"] is None
    message = hand_off(world, step["id"], text="The sharing checks pass; finish the task")
    _reach(world, message["id"])
    return step


def _sql(world: World, statement: str, *values: Any) -> None:
    conn = db.connect(world.db_path)
    try:
        with db.transaction(conn):
            conn.execute(statement, values)
    finally:
        conn.close()


def _reach(world: World, message_id: int) -> None:
    """The user's message on the step has reached its session."""
    _sql(world, "UPDATE messages SET state = 'sent', take_back_until = NULL WHERE id = ?", message_id)


def _finish_body(**fields: Any) -> dict[str, Any]:
    return {"item_key": ITEM, "need": "complete", "ask": "List sharing is ready; check it and close", "size": "small"} | fields


def _put_finish(world: World, **fields: Any) -> Any:
    return _agent(world).post("/api/steps", json=_finish_body(**fields), headers=world.agent())


def test_a_finish_step_put_closes_the_handed_last_step_it_replaces(world: World) -> None:
    """Replace the last handed step with a finish step in one update, preserving a next move throughout."""
    step = _handed_on_prod(world)
    refused = _agent(world).post(f"/api/steps/{step['id']}/done", json={"note": "moved"}, headers=world.agent())
    assert refused.status_code == 400
    detail = refused.json()["detail"]
    assert f"session_id {SESSION_A}" in detail and "Then do this again" not in detail

    put = _put_finish(world, session_id=SESSION_A)
    assert put.status_code == 200, put.text
    assert put.json()["closed_handed"] == step["id"]
    closed = _step_state(world, step["id"])
    assert (closed["status"], closed["handoff_message_id"]) == ("done", None)
    assert _open_needs(world) == ["complete"]
    assert put.json()["step"]["id"] in queue_ids(world)[0]
    assert _item_state(world, closed["item_id"])["waiting_on"] == []


def test_a_finish_step_put_by_another_session_leaves_the_handed_step(world: World) -> None:
    """Only the session holding the step closes it this way. The refusal says how."""
    step = _handed_on_prod(world)
    for session in ({}, {"session_id": SESSION_B}):
        refused = _put_finish(world, **session)
        assert refused.status_code == 409
        assert f"session_id {SESSION_A}" in refused.json()["detail"] and "that one put closes" in refused.json()["detail"]
    assert _open_needs(world) == ["try:prod"] and _step_state(world, step["id"])["handoff_message_id"] is not None


def test_a_failed_close_undoes_the_finish_step_put(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """The put and the close are one transaction: a close that fails keeps neither."""
    step = _handed_on_prod(world)

    def refuse(*_: Any) -> None:
        raise store.Invalid("no")

    monkeypatch.setattr(store, "step_action", refuse)
    assert _put_finish(world, session_id=SESSION_A).status_code == 400
    assert _open_needs(world) == ["try:prod"] and _step_state(world, step["id"])["handoff_message_id"] is not None


def test_a_finish_step_put_waits_with_the_close_while_the_user_s_timer_runs(world: World) -> None:
    """A close that waits for the user's timer is not reported as done: the response says it waits (`held`), and the
    finish step is open meanwhile, so the close that replays later is not refused."""
    step = _handed_on_prod(world)
    _sql(world, "INSERT INTO time_spans (step_id, started_at) VALUES (?, ?)", step["id"], db.now_iso())
    put = _put_finish(world, session_id=SESSION_A)
    assert put.status_code == 200, put.text
    assert put.json()["held"] and "closed_handed" not in put.json()
    assert sorted(_open_needs(world)) == ["complete", "try:prod"]


def test_a_hand_off_still_waiting_to_go_keeps_its_step_from_a_finish_step_put(world: World) -> None:
    """The session has not read the user's message, so its finish step answers nothing, and Take back still works."""
    _item(world)
    step = _step(world, "try:prod", [])
    assert _put(world, stage="prod", waiting_on=[])["finish_step"] is None
    message = hand_off(world, step["id"], text="One more thing to check first")
    # Both refusals say to wait for the message, or to return the step if it failed, not to try each other again.
    refused = _put_finish(world, session_id=SESSION_A)
    assert refused.status_code == 409 and "has not confirmed" in refused.json()["detail"]
    assert "that one put closes" not in refused.json()["detail"]
    closing = _agent(world).post(f"/api/steps/{step['id']}/done", json={}, headers=world.agent())
    assert closing.status_code == 400 and "has not confirmed" in closing.json()["detail"]
    assert "session_id" not in closing.json()["detail"]
    assert _open_needs(world) == ["try:prod"]
    # A message being sent can still go back to waiting, so it has not reached the session either.
    _sql(world, "UPDATE messages SET state = 'sending', take_back_until = NULL WHERE id = ?", message["id"])
    assert _put_finish(world, session_id=SESSION_A).status_code == 409
    assert _open_needs(world) == ["try:prod"]
    # A message that failed to send has the way out the refusal names.
    _sql(world, "UPDATE messages SET state = 'failed', take_back_until = NULL WHERE id = ?", message["id"])
    given_back = _agent(world).post(f"/api/steps/{step['id']}/return", json={"note": "Nothing arrived."}, headers=world.agent())
    assert given_back.status_code == 200, given_back.text
    assert _step_state(world, step["id"])["handoff_message_id"] is None


def test_a_finish_step_put_closes_the_handed_step_once_its_message_arrived(world: World) -> None:
    _item(world)
    step = _step(world, "try:prod", [])
    assert _put(world, stage="prod", waiting_on=[])["finish_step"] is None
    message = hand_off(world, step["id"], text="Those checks pass")
    assert _put_finish(world, session_id=SESSION_A).status_code == 409
    _reach(world, message["id"])
    assert _put_finish(world, session_id=SESSION_A).json()["closed_handed"] == step["id"]


def test_a_hand_off_to_a_session_with_no_id_yet_does_not_trap_its_session(world: World) -> None:
    """Delivery leaves `session_id` empty when it could not name the session it started. The refusals then name no
    session, and the put's session counts as the holder, so that session is not left between the two refusals."""
    step = _handed_on_prod(world)
    _sql(world, "UPDATE messages SET session_id = NULL WHERE id = ?", _step_state(world, step["id"])["handoff_message_id"])
    refused = _agent(world).post(f"/api/steps/{step['id']}/done", json={}, headers=world.agent())
    assert refused.status_code == 400 and "session_id <your session id>" in refused.json()["detail"]
    assert "session_id None" not in _put_finish(world).json()["detail"]
    assert _put_finish(world, session_id=SESSION_B).json()["closed_handed"] == step["id"]


def test_a_new_step_other_than_a_finish_step_still_waits_for_the_handed_last_step(world: World) -> None:
    _handed_on_prod(world)
    body = {"item_key": ITEM, "need": "decide:next", "ask": "Ship the next part?", "session_id": SESSION_A}
    assert _agent(world).post("/api/steps", json=body, headers=world.agent()).status_code == 409
    assert _open_needs(world) == ["try:prod"]


@pytest.mark.parametrize("where", ["off prod", "work waiting", "another step open"])
def test_where_the_close_alone_goes_through_the_finish_step_still_waits_for_it(world: World, where: str) -> None:
    """SPEC NEXT-37 stays as it was wherever the session can close the handed step first: a finish step put before
    that is refused and closes nothing, and closing first then putting works."""
    _item(world)
    step = _step(world, "try:it", [])
    if where == "another step open":
        other = _user_step(world, "decide:other", [])
    if where != "off prod":
        waits = [] if where == "another step open" else [{"kind": "agent", "who": SESSION_A}]
        assert _put(world, stage="prod", waiting_on=waits)["finish_step"] is None
    _reach(world, hand_off(world, step["id"], text="Looks right")["id"])
    refused = _put_finish(world, session_id=SESSION_A)
    assert refused.status_code == 409 and "or close it when nothing is left" in refused.json()["detail"]
    assert _step_state(world, step["id"])["status"] == "open"
    assert _agent(world).post(f"/api/steps/{step['id']}/done", json={"note": "fine"}, headers=world.agent()).status_code == 200
    if where == "another step open":
        # A task shows one card at a time (SPEC NEXT-55): the other card gives way to the finish step.
        assert _put_finish(world, session_id=SESSION_A).status_code == 409
        assert _agent(world).post(f"/api/steps/{other['id']}/withdraw", json={}, headers=world.agent()).status_code == 200
    assert _put_finish(world, session_id=SESSION_A).status_code == 200


def test_a_handed_finish_step_on_prod_goes_back_on_its_own_return(world: World) -> None:
    """Return the last handed finish step with a reply before accepting another finish step."""
    _item(world)
    finish = _write_finish(world)
    assert _put(world, stage="prod", waiting_on=[])["finish_step"] is None
    _reach(world, hand_off(world, finish, text="Check the bars first")["id"])
    refused = _put_finish(world, need="complete:2", session_id=SESSION_A)
    assert refused.status_code == 409 and f'cockpit step return {finish} --note' in refused.json()["detail"]
    done = _agent(world).post(f"/api/steps/{finish}/done", json={"note": "Bars checked."}, headers=world.agent())
    assert done.status_code == 200 and done.json()["returned"]
    assert _step_state(world, finish)["handoff_message_id"] is None and _open_needs(world) == ["complete"]


def test_a_finish_step_put_again_after_a_withdrawal_closes_the_handed_last_step(world: World) -> None:
    _item(world)
    finish = _write_finish(world)
    assert _agent(world).post(f"/api/steps/{finish}/withdraw", json={}, headers=world.agent()).status_code == 200
    step = _step(world, "try:prod", [])
    assert _put(world, stage="prod", waiting_on=[])["finish_step"] is None
    _reach(world, hand_off(world, step["id"], text="Those checks pass")["id"])
    put = _put_finish(world, session_id=SESSION_A)
    assert put.status_code == 200, put.text
    assert put.json()["closed_handed"] == step["id"] and put.json()["step"]["id"] == finish
    assert _open_needs(world) == ["complete"]
