"""SPEC RANK-18 : the sweeper's score alone places steps in the user's queue. Priority, finish
steps, answers and an agent's return give a step no place of their own; the user's take, snooze and a timer that ran
in the place still hold."""

from __future__ import annotations

from pathlib import Path

from cockpit import db
from tests.conftest import SESSION_A, World
from tests.test_api import add_item, agent_client, put_card, put_item, put_step, queue_ids, run_sql, start_timer
from tests.test_db import database_at


def returned(world: World, step_id: int) -> None:
    handed = world.client.post(f"/api/steps/{step_id}/hand-off", json={"text": "Check it"}, headers=world.user())
    assert handed.status_code == 200, handed.text
    back = agent_client(world).post(f"/api/steps/{step_id}/return", json={"note": "Checked."}, headers=world.agent())
    assert back.status_code == 200, back.text


def set_priority(world: World, key: str, priority: str) -> None:
    edited = world.client.put("/api/items", json={"key": key, "priority": priority}, headers=world.user())
    assert edited.status_code == 200, edited.text


def test_a_step_scored_well_above_a_queued_one_takes_its_place(world: World) -> None:
    steps = [put_card(world, f"n{n}", ask=f"Step {n}", score=70 - n)["step"] for n in range(4)]
    close = put_card(world, "close", ask="Close call", score=77)["step"]  # within PLACE_MARGIN of the lowest
    assert close["id"] in queue_ids(world)[1]
    strong = put_card(world, "strong", ask="Strong step", score=90)["step"]
    queue, ready = queue_ids(world)
    assert queue[0] == strong["id"] and steps[3]["id"] in ready and len(queue) == 4


def test_priority_and_kind_of_step_give_no_place_of_their_own(world: World) -> None:
    steps = [put_card(world, f"n{n}", ask=f"Step {n}", score=90 - n)["step"] for n in range(4)]
    put_item(world, key="note:sharing", title="List sharing")
    set_priority(world, "note:sharing", "urgent")
    urgent = put_step(world, item_key="note:sharing", need="reply:1", ask="Share the list with Alex?", score=20)["step"]
    question = add_item(world, "How do the usage bars work?")
    put_item(world, key=question["key"], title="Usage bars")
    answer = put_step(world, item_key=question["key"], need="read:usage-bars", ask="Read how the bars work", score=20)["step"]
    put_item(world, key="note:done", title="Done work")
    finish = put_step(world, item_key="note:done", need="complete", ask="Shipped: live on prod since Tuesday")["step"]["id"]
    closed = agent_client(world).put("/api/items", json={"key": "note:done", "stage": "done", "note": "Shipped."},
                                     headers=world.agent())
    assert closed.json()["finish_step"] == finish, closed.text
    queue, ready = queue_ids(world)
    assert queue == [step["id"] for step in steps]
    assert {urgent["id"], answer["id"], finish} <= set(ready)


def test_the_sweeper_s_score_brings_urgent_work_in(world: World) -> None:
    steps = [put_card(world, f"n{n}", ask=f"Step {n}", score=70 - n)["step"] for n in range(4)]
    put_item(world, key="note:sharing", title="List sharing")
    set_priority(world, "note:sharing", "high")
    pressing = put_step(world, item_key="note:sharing", need="reply:1", ask="Share the list with Alex?")["step"]
    assert pressing["id"] in queue_ids(world)[1]  # unscored, at STEP_DEFAULTS' 50
    put_step(world, item_key="note:sharing", need="reply:1", score=95)  # the sweep's put
    queue, ready = queue_ids(world)
    assert queue[0] == pressing["id"] and ready == [steps[3]["id"]]


def test_an_agent_s_return_takes_a_place_by_score(world: World) -> None:
    steps = [put_card(world, f"n{n}", ask=f"Step {n}", score=70 - n)["step"] for n in range(4)]
    handed = world.client.post(f"/api/steps/{steps[0]['id']}/hand-off", json={"text": "Check it"}, headers=world.user())
    assert handed.status_code == 200, handed.text
    put_item(world, key="note:other", title="Other work")
    other = put_step(world, item_key="note:other", need="read:1", ask="Read the note", score=40)["step"]
    assert other["id"] in queue_ids(world)[0]  # took the free place
    weak = put_card(world, "read:2", ask="Read the other note", score=10)["step"]
    back = agent_client(world).post(f"/api/steps/{steps[0]['id']}/return", json={"note": "Checked."}, headers=world.agent())
    assert back.status_code == 200, back.text
    state = world.client.get("/api/state").json()
    assert [step["id"] for step in state["queue"]] == [steps[0]["id"], steps[1]["id"], steps[2]["id"], steps[3]["id"]]
    assert state["queue"][0]["taken_at"] is None
    assert [step["id"] for step in state["ready_next"]] == [other["id"], weak["id"]]


def test_the_user_s_take_back_puts_the_step_at_the_top(world: World) -> None:
    steps = [put_card(world, f"n{n}", ask=f"Step {n}", score=70 - n)["step"] for n in range(4)]
    handed = world.client.post(f"/api/steps/{steps[3]['id']}/hand-off", json={"text": "Check it"}, headers=world.user())
    assert handed.status_code == 200, handed.text
    back = world.client.post(f"/api/steps/{steps[3]['id']}/return", json={}, headers=world.user())
    assert back.status_code == 200, back.text
    assert queue_ids(world)[0][0] == steps[3]["id"]


def test_the_user_s_own_take_and_a_started_step_keep_their_places(world: World) -> None:
    steps = [put_card(world, f"n{n}", ask=f"Step {n}", score=40 - n)["step"] for n in range(4)]
    weak = put_card(world, "weak", ask="Weak step", score=10)["step"]
    assert world.client.post(f"/api/steps/{weak['id']}/take", json={}, headers=world.user()).status_code == 200
    start_timer(world, steps[1]["id"])
    assert world.client.post(f"/api/steps/{steps[1]['id']}/timer/pause", headers=world.user()).status_code == 200
    first = put_card(world, "reply:1", ask="Share the list with Alex?", score=90)["step"]
    second = put_card(world, "reply:2", ask="Invite Ana too?", score=90)["step"]
    third = put_card(world, "reply:3", ask="Invite Ana too?", score=90)["step"]
    queue, ready = queue_ids(world)
    assert weak["id"] in queue and steps[1]["id"] in queue
    assert first["id"] in queue and second["id"] in queue and third["id"] in ready


def test_a_returned_step_started_in_its_place_keeps_it(world: World) -> None:
    steps = [put_card(world, f"n{n}", ask=f"Step {n}", score=40 - n)["step"] for n in range(4)]
    for step in steps:
        returned(world, step["id"])
    # The user opens the returned step a moment after its return, not in the same second.
    run_sql(world, "UPDATE steps SET queued_at = '2026-01-27T08:00:00Z'")
    start_timer(world, steps[3]["id"])
    assert world.client.post(f"/api/steps/{steps[3]['id']}/timer/pause", headers=world.user()).status_code == 200
    strong = [put_card(world, f"reply:{n}", ask=f"Ask {n}", score=90)["step"] for n in range(4)]
    queue, ready = queue_ids(world)
    assert steps[3]["id"] in queue and strong[3]["id"] in ready


def test_a_step_worked_on_before_its_hand_off_gives_way_once_returned(world: World) -> None:
    steps = [put_card(world, f"n{n}", ask=f"Step {n}", score=40 - n)["step"] for n in range(4)]
    start_timer(world, steps[3]["id"])
    for step in steps:
        returned(world, step["id"])
    # The hand-off ended the timer; the agent's return came later, as it does outside a test.
    run_sql(world, "UPDATE time_spans SET started_at = '2026-01-27T07:00:00Z', ended_at = '2026-01-27T07:30:00Z'")
    strong = [put_card(world, f"reply:{n}", ask=f"Ask {n}", score=90)["step"] for n in range(4)]
    queue = queue_ids(world)[0]
    assert sorted(queue) == sorted(step["id"] for step in strong)


def test_a_quick_return_after_a_hand_off_holds_no_place(world: World) -> None:
    # The timer the hand-off ended belongs to the place before, also when the return comes in the same second.
    steps = [put_card(world, f"n{n}", ask=f"Step {n}", score=10)["step"] for n in range(4)]
    start_timer(world, steps[0]["id"])
    returned(world, steps[0]["id"])
    run_sql(world, "UPDATE time_spans SET started_at = '2026-01-27T07:00:00Z'")  # worked on long before the hand-off
    strong = [put_card(world, f"reply:{n}", ask=f"Ask {n}", score=90)["step"] for n in range(4)]
    assert sorted(queue_ids(world)[0]) == sorted(step["id"] for step in strong)


def test_a_finish_step_the_closing_session_wrote_takes_a_place_by_its_score(world: World) -> None:
    steps = [put_card(world, f"n{n}", ask=f"Step {n}", score=10)["step"] for n in range(4)]
    put_item(world, key="note:shipped", title="Shipped")
    finish = put_step(world, item_key="note:shipped", need="complete", ask="Released: live on prod")["step"]["id"]
    closed = agent_client(world).put(
        "/api/items", json={"key": "note:shipped", "stage": "done", "note": "Released."}, headers=world.agent()
    )
    assert closed.json()["finish_step"] == finish, closed.text
    queue, ready = queue_ids(world)
    assert finish in queue and len(queue) == 4 and len(ready) == 1 and ready[0] in {step["id"] for step in steps}


def test_a_sweep_s_scores_swap_places_once_it_finishes(world: World) -> None:
    item = put_item(world)
    quick = put_card(world, "quick", ask="Quick to-do", score=75)["step"]
    others = [put_card(world, f"n{n}", ask=f"Step {n}", score=70)["step"] for n in range(3)]
    waiting = put_card(world, "read", ask="Read the result", score=50)["step"]
    sweep = agent_client(world).post("/api/sweeps/start", json={"reorder": False}, headers=world.agent()).json()
    put_card(world, "read", score=86)
    # Another session's put in the middle of the sweep swaps nothing either.
    other = agent_client(world).post("/api/steps", json={"item_key": item["key"], "need": "other",
                                                         "ask": "Another session's ask"}, headers=world.agent("app-1"))
    assert other.status_code == 200, other.text
    put_card(world, "quick", score=82)
    assert quick["id"] in queue_ids(world)[0] and waiting["id"] in queue_ids(world)[1]
    finished = agent_client(world).post("/api/sweeps/finish", json={"sweep_id": sweep["id"], "summary": "Light: scores",
                                                                     "error": ""}, headers=world.agent())
    assert finished.status_code == 200, finished.text
    queue, ready = queue_ids(world)
    assert quick["id"] in queue and waiting["id"] in queue and others[0]["id"] in ready


def test_the_migration_ends_only_the_takes_an_agent_s_return_made(tmp_path: Path) -> None:
    version = next(n for n, script in enumerate(db.MIGRATIONS) if "e.kind = 'step.return' AND e.actor != 'user'" in script)
    conn = database_at(tmp_path / "old.db", version)
    at = "2026-01-27T09:00:00Z"
    conn.execute(
        "INSERT INTO items (id, key, title, created_at, updated_at, updated_by) VALUES (1, 'note:1', 'T', ?, ?, 'agent:x')",
        (at, at),
    )
    for step_id in (11, 12, 13, 14):
        conn.execute(
            "INSERT INTO steps (id, item_id, need, ask, queued_at, taken_at, created_at, updated_at, updated_by)"
            " VALUES (?, 1, ?, 'a', ?, ?, ?, ?, 'agent:x')",
            (step_id, f"n{step_id}", at, at, at, at),
        )
    events = [
        ("step.return", "agent:app", 11),  # the agent's return made the take: it ends
        ("step.return", "user", 12),  # the user's Take back
        ("step.return", "agent:app", 13),
        ("step.take", "user", 13),  # the user's Take after an agent's return
    ]  # step 14 has no event and keeps its take
    for kind, actor, step_id in events:
        conn.execute("INSERT INTO events (at, actor, kind, item_id, step_id) VALUES (?, ?, ?, 1, ?)", (at, actor, kind, step_id))
    conn.close()
    conn = db.connect(tmp_path / "old.db")
    try:
        db.migrate(conn)
        taken = {row["id"]: row["taken_at"] for row in conn.execute("SELECT id, taken_at FROM steps")}
    finally:
        conn.close()
    assert taken == {11: None, 12: at, 13: at, 14: at}


def test_undo_of_a_snooze_brings_the_step_back(world: World) -> None:
    steps = [put_card(world, f"n{n}", ask=f"Step {n}", score=70 - n)["step"] for n in range(4)]
    world.client.post(f"/api/steps/{steps[0]['id']}/snooze", json={"until": "2099-01-01T08:00:00Z"}, headers=world.user())
    later = put_card(world, "later", ask="Later step", score=20)["step"]
    assert later["id"] in queue_ids(world)[0]
    world.client.post(f"/api/steps/{steps[0]['id']}/snooze", json={"until": None}, headers=world.user())
    queue, ready = queue_ids(world)
    assert steps[0]["id"] in queue and ready == [later["id"]]


def test_a_step_worked_on_in_its_place_keeps_it_after_a_closing_answer_is_undone(world: World) -> None:
    item = put_item(world, sessions=[SESSION_A])
    closing = {"text": "I've sent it myself; close", "closes": True}
    step = put_step(world, need="reply:1", ask="Is the reply out?", answers=[closing], score=10)["step"]
    start_timer(world, step["id"])
    run_sql(world, "UPDATE time_spans SET started_at = '2026-01-27T08:00:00Z'")  # worked on it a while in its place
    run_sql(world, "UPDATE steps SET queued_at = '2026-01-27T07:00:00Z'")
    body = {"step_id": step["id"], "how": "complete", "answer": closing["text"], "note": "", "reason": "done", "session_id": SESSION_A}
    assert world.client.post(f"/api/items/{item['id']}/finish", json=body, headers=world.user()).status_code == 200
    assert world.client.post(f"/api/items/{item['id']}/reopen", headers=world.user()).status_code == 200
    for n in range(4):
        put_card(world, f"reply:{n}", ask=f"Ask {n}", score=90)
    assert step["id"] in queue_ids(world)[0]


def test_add_takes_the_priority_from_the_user_s_words(world: World) -> None:
    def added(text: str) -> dict:
        response = world.client.post("/api/items/add", json={"text": text, "part": None, "pinned": False},
                                     headers=world.user())
        assert response.status_code == 200, response.text
        return response.json()

    high = added("The reminder repeats twice, can you check? High priority.")
    assert high["priority"] == "high" and "priority" in high["user_fields"]
    assert added("This is urgent: the export fails")["priority"] == "urgent"
    assert added("Look at the sharing page, not urgent")["priority"] == "normal"
    assert added("Low priority: rename the chip")["priority"] == "low"
    assert added("Why is the export slow?")["priority"] == "normal"


def test_add_leaves_the_priority_alone_for_a_mention_or_a_negation(world: World) -> None:
    def priority(text: str) -> str:
        response = world.client.post("/api/items/add", json={"text": text, "part": None, "pinned": False},
                                     headers=world.user())
        assert response.status_code == 200, response.text
        return response.json()["priority"]

    assert priority("Can you check the empty export file; Ana has an example,"
                    " or I can create one. High priority.") == "high"
    assert priority("Urgent: the export fails") == "urgent"
    assert priority("Not high priority: rename the chip") == "normal"
    assert priority("Non-urgent: tidy the README") == "normal"
    assert priority("This is not particularly urgent") == "normal"
    assert priority("Check why high priority steps wait behind finish steps") == "normal"
    assert priority("This is not\nurgent. Clean up the README.") == "normal"
    assert priority("Not\nhigh priority: rename the chip.") == "normal"
    assert priority("This is very urgent. Check the reminder.") == "urgent"
    assert priority("It’s urgent: the export fails.") == "urgent"
    assert priority("Investigate the reminder." + "\n" * 100_000 + "Could you check?") == "normal"


def test_the_priority_picked_in_the_add_form_sets_the_item_s_priority(world: World) -> None:
    # SPEC CONTROL-10: the pick is the user's own value, and it wins over words in the text.
    def added(text: str, priority: object) -> dict:
        response = world.client.post("/api/items/add", json={"text": text, "part": None, "pinned": False,
                                                              "priority": priority}, headers=world.user())
        assert response.status_code == 200, response.text
        return response.json()

    high = added("Renew the passport", "high")
    assert high["priority"] == "high" and "priority" in high["user_fields"]
    assert added("Urgent: the export fails", "low")["priority"] == "low"
    assert added("Urgent: the export fails", None)["priority"] == "urgent"
    assert added("Tidy the README", None)["priority"] == "normal"
    refused = world.client.post("/api/items/add", json={"text": "Tidy", "part": None, "pinned": False,
                                                         "priority": "top"}, headers=world.user())
    assert refused.status_code == 400


def test_the_user_s_close_lets_a_higher_waiting_step_replace_a_lower_one_whatever_the_gap(world: World) -> None:
    # SPEC RANK-19: between presses a waiting step needs PLACE_MARGIN to take a place; the user's close
    # re-sorts the queue once, so every waiting step scored above a movable queued one takes its place.
    steps = [put_card(world, f"n{n}", ask=f"Step {n}", score=score)["step"] for n, score in enumerate((90, 80, 79, 70))]
    first = put_card(world, "w1", ask="Waiting one", score=76)["step"]
    second = put_card(world, "w2", ask="Waiting two", score=74)["step"]
    assert queue_ids(world)[1] == [first["id"], second["id"]]  # within PLACE_MARGIN of the 70
    world.client.post(f"/api/steps/{steps[0]['id']}/done", json={}, headers=world.user())
    queue, ready = queue_ids(world)
    assert queue == [steps[1]["id"], steps[2]["id"], first["id"], second["id"]] and ready == [steps[3]["id"]]


def test_a_hand_off_re_sorts_but_an_agent_s_close_does_not(world: World) -> None:
    steps = [put_card(world, f"n{n}", ask=f"Step {n}", score=score)["step"] for n, score in enumerate((90, 85, 80, 70))]
    first = put_card(world, "w1", ask="Waiting one", score=78)["step"]
    second = put_card(world, "w2", ask="Waiting two", score=75)["step"]
    third = put_card(world, "w3", ask="Waiting three", score=74)["step"]
    closed = agent_client(world).post(f"/api/steps/{steps[0]['id']}/done", json={"note": "Merged."}, headers=world.agent())
    assert closed.status_code == 200, closed.text
    queue, ready = queue_ids(world)
    assert first["id"] in queue and steps[3]["id"] in queue and ready == [second["id"], third["id"]]
    handed = world.client.post(f"/api/steps/{steps[1]['id']}/hand-off", json={"text": "Check it"}, headers=world.user())
    assert handed.status_code == 200, handed.text
    queue, ready = queue_ids(world)
    assert {second["id"], third["id"]} <= set(queue) and ready == [steps[3]["id"]]


def test_a_close_leaves_a_step_the_user_started_in_its_place(world: World) -> None:
    steps = [put_card(world, f"n{n}", ask=f"Step {n}", score=score)["step"] for n, score in enumerate((90, 80, 79, 70))]
    start_timer(world, steps[3]["id"])
    assert world.client.post(f"/api/steps/{steps[3]['id']}/timer/pause", headers=world.user()).status_code == 200
    first = put_card(world, "w1", ask="Waiting one", score=76)["step"]
    second = put_card(world, "w2", ask="Waiting two", score=74)["step"]
    world.client.post(f"/api/steps/{steps[0]['id']}/done", json={}, headers=world.user())
    queue, ready = queue_ids(world)
    assert steps[3]["id"] in queue and first["id"] in queue and ready == [second["id"]]


def test_closing_a_waiting_step_leaves_the_queue_as_it_is(world: World) -> None:
    # The press frees no place, and the page draws no move (`resorted` in app.js), so the server makes none either.
    steps = [put_card(world, f"n{n}", ask=f"Step {n}", score=score)["step"] for n, score in enumerate((90, 80, 79, 70))]
    first = put_card(world, "w1", ask="Waiting one", score=76)["step"]
    second = put_card(world, "w2", ask="Waiting two", score=72)["step"]
    world.client.post(f"/api/steps/{second['id']}/dismiss", json={}, headers=world.user())
    queue, ready = queue_ids(world)
    assert queue == [step["id"] for step in steps] and ready == [first["id"]]


def test_closing_a_snoozed_step_leaves_the_queue_as_it_is(world: World) -> None:
    # A snoozed step keeps its place time while it sleeps, but shows in no queue place, so its close frees none.
    steps = [put_card(world, f"n{n}", ask=f"Step {n}", score=score)["step"] for n, score in enumerate((90, 80, 79, 70))]
    slept = world.client.post(f"/api/steps/{steps[0]['id']}/snooze", json={"until": "2099-01-01T08:00:00Z"}, headers=world.user())
    assert slept.status_code == 200, slept.text
    filler = put_card(world, "n4", ask="Step 4", score=69)["step"]
    first = put_card(world, "w1", ask="Waiting one", score=76)["step"]
    second = put_card(world, "w2", ask="Waiting two", score=74)["step"]
    before = queue_ids(world)
    assert filler["id"] in before[0] and before[1] == [first["id"], second["id"]]
    world.client.post(f"/api/steps/{steps[0]['id']}/dismiss", json={}, headers=world.user())
    assert queue_ids(world) == before
