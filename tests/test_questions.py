from __future__ import annotations

import subprocess
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from cockpit import config, db, questions, sessions, store
from tests.conftest import ORIGIN, SESSION_A, SESSION_B, World, make_session

NEED_A = f"question:{SESSION_A}:2026-01-04T09:00:00Z"


@pytest.fixture(autouse=True)
def last_words(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(questions, "last_message", lambda session_id: "Commit aaaa111 has A.\n\nWhich one?")


def agent(world: World) -> TestClient:
    return TestClient(world.client.app, base_url=ORIGIN)


def sync(world: World) -> None:
    conn = db.connect(world.db_path)
    try:
        questions.sync(conn, world.cache.sessions, datetime.now(UTC))
    finally:
        conn.close()


def state(world: World) -> dict:
    return world.client.get("/api/state").json()


def put_item(world: World, key: str, sessions: list[str]) -> None:
    response = agent(world).put("/api/items", json={"key": key, "title": key, "sessions": sessions}, headers=world.agent())
    assert response.status_code == 200, response.text


def steps(world: World) -> list[tuple]:
    conn = db.connect(world.db_path)
    try:
        return conn.execute("SELECT id, need, status, withdrawn_at IS NOT NULL, ask FROM steps ORDER BY id").fetchall()
    finally:
        conn.close()


def test_a_session_s_question_is_a_ranked_step_on_its_item(world: World) -> None:
    put_item(world, "note:a", [SESSION_A])
    sync(world)
    sync(world)
    board = state(world)
    assert board["waiting_sessions"] == []
    [step] = board["queue"]
    assert step["item"]["key"] == "note:a"
    assert step["need"] == NEED_A
    assert step["ask"] == "Pick A or B"
    assert step["why"] == 'Session "session 1111" stopped to ask you this and waits for your answer.'
    assert step["body"] == "> Commit aaaa111 has A.\n>\n> Which one?"
    assert step["target"]["session_id"] == SESSION_A
    assert (step["score"], step["reasons"], step["updated_by"]) == (50, [], "cockpit")  # the sweeper scores it (SPEC RANK-18)


def test_a_session_without_an_item_gets_one_of_its_own(world: World) -> None:
    sync(world)
    [step] = state(world)["queue"]
    assert step["item"]["key"] == f"session:{SESSION_A}"
    assert step["item"]["title"] == "session 1111"
    assert step["item"]["sessions"] == [SESSION_A]


def test_a_step_on_the_session_s_item_that_arrives_after_its_question_closes_the_question(world: World) -> None:
    put_item(world, "note:a", [SESSION_A])
    sync(world)
    assert [step[1] for step in steps(world)] == [NEED_A]
    # The sweep puts the question on a step of the item, naming no session: as when it came first, only it shows.
    response = agent(world).post("/api/steps", json={"item_key": "note:a", "need": "decide:pick", "ask": "Pick A or B"},
                                 headers=world.agent())
    assert response.status_code == 200, response.text
    sync(world)
    assert [(step[1], step[2], step[3]) for step in steps(world)] == [(NEED_A, "open", True), ("decide:pick", "open", False)]


def test_a_question_step_whose_session_moved_on_closes_before_it_covers_another(world: World) -> None:
    world.cache.sessions = [make_session(SESSION_A, pid=None)]
    put_item(world, "note:a", [SESSION_A, SESSION_B])
    sync(world)
    sized = agent(world).post("/api/steps", json={"item_key": "note:a", "need": NEED_A, "size": "small"}, headers=world.agent())
    assert sized.status_code == 200, sized.text
    # A question of session B, which no longer asks, still names the item's sessions when this poll weighs A's.
    need_b = f"question:{SESSION_B}:2026-01-04T09:00:00Z"
    response = agent(world).post("/api/steps", json={"item_key": "note:a", "need": need_b, "ask": "Answer B"},
                                 headers=world.agent())
    assert response.status_code == 200, response.text
    sync(world)
    assert [(step[1], step[2], step[3]) for step in steps(world)] == [(NEED_A, "open", False), (need_b, "open", True)]
    conn = db.connect(world.db_path)
    try:
        assert conn.execute("SELECT size FROM steps WHERE need = ?", (NEED_A,)).fetchone()[0] == "small"
    finally:
        conn.close()


def test_question_steps_whose_items_share_sessions_close_none_of_each_other(world: World) -> None:
    session_c = "44444444-4444-4444-8444-444444444444"
    world.cache.sessions = [make_session(session_id, pid=None) for session_id in (SESSION_A, SESSION_B, session_c)]
    for key, listed in (("note:a", [SESSION_A]), ("note:b", [SESSION_B, SESSION_A]), ("note:c", [session_c, SESSION_B])):
        put_item(world, key, listed)
    sync(world)
    sized = agent(world).post("/api/steps", json={"item_key": "note:a", "need": NEED_A, "size": "small"}, headers=world.agent())
    assert sized.status_code == 200, sized.text
    # B's question names A through its item, and C's names B: neither closes the other's step.
    sync(world)
    assert [(step[2], step[3]) for step in steps(world)] == [("open", False)] * 3
    conn = db.connect(world.db_path)
    try:
        assert conn.execute("SELECT size FROM steps WHERE need = ?", (NEED_A,)).fetchone()[0] == "small"
    finally:
        conn.close()


def test_covered_sessions_dialogs_and_the_sweep_get_no_question_step(world: World) -> None:
    sweep = "33333333-3333-4333-8333-333333333333"
    dialog = replace(make_session(SESSION_B, pid=101, state="working"), job_state="working", tempo="blocked")
    world.cache.sessions = [make_session(SESSION_A, pid=None), dialog, replace(make_session(sweep, pid=None), name="Cockpit sweep")]
    put_item(world, "note:a", [SESSION_A])
    response = agent(world).post(
        "/api/steps", json={"item_key": "note:a", "need": "merge", "ask": "Merge it", "answers": ["Merge it"]}, headers=world.agent()
    )
    assert response.status_code == 200, response.text
    sync(world)
    assert [need for _, need, *_ in steps(world)] == ["merge"]
    assert [session["session_id"] for session in state(world)["waiting_sessions"]] == [SESSION_B]


def test_a_session_s_question_goes_on_the_step_the_user_handed_it(world: World) -> None:
    """SPEC NEXT-37: the user asked on the step, so the session's answer comes back there."""
    put_item(world, "note:a", [SESSION_A])
    put = agent(world).post(
        "/api/steps", json={"item_key": "note:a", "need": "complete", "ask": "Is this task done?"}, headers=world.agent()
    ).json()["step"]
    handed = world.client.post(f"/api/steps/{put['id']}/hand-off", json={"text": "Which reminder repeats daily?"},
                               headers=world.user())
    assert handed.status_code == 200, handed.text
    conn = db.connect(world.db_path)
    conn.execute("UPDATE messages SET sent_at = '2026-01-04T08:00:00Z' WHERE step_id = ?", (put["id"],))
    conn.commit()
    conn.close()
    sync(world)
    sync(world)
    [(step_id, need, status, withdrawn, ask)] = steps(world)
    assert (step_id, need, status, withdrawn, ask) == (put["id"], NEED_A, "open", 0, "Pick A or B")
    [step] = state(world)["queue"]
    assert step["id"] == put["id"] and step["body"] == "> Commit aaaa111 has A.\n>\n> Which one?"


def test_a_hand_off_sent_in_the_second_the_question_came_still_takes_it(world: World) -> None:
    # Cockpit stores whole seconds and a session's time has a fraction: 09:00:00Z is before 09:00:00.500Z.
    world.cache.sessions = [replace(session, updated_at="2026-01-04T09:00:00.500Z") for session in world.cache.sessions]
    put_item(world, "note:a", [SESSION_A])
    put = agent(world).post(
        "/api/steps", json={"item_key": "note:a", "need": "complete", "ask": "Is this task done?"}, headers=world.agent()
    ).json()["step"]
    handed = world.client.post(f"/api/steps/{put['id']}/hand-off", json={"text": "Which reminder repeats daily?"},
                               headers=world.user())
    assert handed.status_code == 200, handed.text
    conn = db.connect(world.db_path)
    conn.execute("UPDATE messages SET sent_at = '2026-01-04T09:00:00Z' WHERE step_id = ?", (put["id"],))
    conn.commit()
    conn.close()
    sync(world)
    [(step_id, _need, status, withdrawn, ask)] = steps(world)
    assert (step_id, status, withdrawn, ask) == (put["id"], "open", 0, "Pick A or B")


def handed_step(world: World, fields: dict, to: str) -> dict:
    """A step put on note:a and handed to session `to`, whose message reached it at 08:00."""
    put = agent(world).post("/api/steps", json={"item_key": "note:a"} | fields, headers=world.agent())
    assert put.status_code == 200, put.text
    step = put.json()["step"]
    handed = world.client.post(f"/api/steps/{step['id']}/hand-off", json={"text": "Which reminder repeats daily?", "session_id": to},
                               headers=world.user())
    assert handed.status_code == 200, handed.text
    conn = db.connect(world.db_path)
    conn.execute("UPDATE messages SET sent_at = '2026-01-04T08:00:00Z' WHERE step_id = ?", (step["id"],))
    conn.commit()
    conn.close()
    return step


def test_the_session_s_question_replaces_the_handed_step_s_actions_and_target(world: World) -> None:
    """The step asks the session's question, of the session that asked it, and nothing else."""
    put_item(world, "note:a", [SESSION_A])
    fields = {"need": "check", "ask": "Run the readback?", "answers": ["Ship it"], "command": {"run": "true", "cwd": "/tmp", "explain": "Does nothing."}}
    step = handed_step(world, fields, SESSION_B)
    world.cache.sessions = [make_session(SESSION_A, pid=101, state="working"), make_session(SESSION_B, pid=None)]
    sync(world)
    [shown] = state(world)["queue"]
    assert (shown["id"], shown["ask"], shown["answers"], shown["command"]) == (step["id"], "Pick A or B", [], None)
    conn = db.connect(world.db_path)
    try:
        assert conn.execute("SELECT session_id FROM steps WHERE id = ?", (step["id"],)).fetchone()[0] == SESSION_B
    finally:
        conn.close()


def test_the_session_s_question_drops_the_handed_step_s_tasks_to_close(world: World) -> None:
    """a question closes no tasks, so the list the earlier ask offered goes with its answers."""
    put_item(world, "note:a", [SESSION_A])
    put_item(world, "note:b", [])
    other = agent(world).get("/api/items", params={"key": "note:b"}, headers=world.agent()).json()["id"]
    fields = {"need": "close:done", "ask": "Close these finished tasks?", "closes": [{"item_id": other, "why": "Merged."}]}
    step = handed_step(world, fields, SESSION_B)
    world.cache.sessions = [make_session(SESSION_A, pid=101, state="working"), make_session(SESSION_B, pid=None)]
    sync(world)
    [shown] = [each for each in state(world)["queue"] if each["id"] == step["id"]]
    assert (shown["ask"], shown["closes"]) == ("Pick A or B", None)


def test_the_session_s_question_drops_the_handed_step_s_reply_hint(world: World) -> None:
    """The reply hint described the answer to the earlier ask, so the question takes the box's plain placeholder."""
    put_item(world, "note:a", [SESSION_A])
    fields = {"need": "check", "ask": "Did the deploy work?", "reply_hint": "Paste the tested URL"}
    step = handed_step(world, fields, SESSION_B)
    world.cache.sessions = [make_session(SESSION_A, pid=101, state="working"), make_session(SESSION_B, pid=None)]
    sync(world)
    [shown] = [each for each in state(world)["queue"] if each["id"] == step["id"]]
    assert (shown["ask"], shown["reply_hint"]) == ("Pick A or B", "")


def test_a_session_s_question_goes_on_its_handed_step_while_another_step_covers_it(world: World) -> None:
    """a second open step on the session's item covers the session, yet its answer comes back (NEXT-37)."""
    put_item(world, "note:a", [SESSION_A])
    step = handed_step(world, {"need": "complete", "ask": "Is this task done?"}, SESSION_A)
    # Only the user's own put adds a step beside a handed one (SPEC NEXT-55).
    other = world.client.post("/api/steps", json={"item_key": "note:a", "need": "other", "ask": "Read the notes"},
                              headers=world.user())
    assert other.status_code == 200, other.text
    sync(world)
    asks = {shown["id"]: shown["ask"] for shown in state(world)["queue"]}
    assert asks[step["id"]] == "Pick A or B"


def test_a_handed_merge_step_asks_the_question_without_its_merge_answer(world: World) -> None:
    """A handed merge step asks the question without its merge answer."""
    put_item(world, "note:a", [SESSION_A])
    step = handed_step(world, {"need": "merge", "ask": "Merge the export PR?", "answers": ["Merge it"]}, SESSION_A)
    sync(world)
    [shown] = state(world)["queue"]
    assert (shown["id"], shown["need"], shown["ask"], shown["answers"]) == (step["id"], NEED_A, "Pick A or B", [])


@pytest.mark.parametrize("need", ["check", "decide:export", "complete"])
def test_a_handed_step_back_with_the_question_asks_the_session_s_next_question(world: World, need: str) -> None:
    """a handed step that kept its own need under the session's question was weighed by no poll and covered
    the session, so its next question never showed."""
    put_item(world, "note:a", [SESSION_A])
    step = handed_step(world, {"need": need, "ask": "Run the readback?"}, SESSION_A)
    sync(world)
    world.cache.sessions = [replace(make_session(SESSION_A, pid=None), needs="pick C", updated_at="2026-01-04T10:00:00Z")]
    sync(world)
    sync(world)
    assert [(shown["id"], shown["need"], shown["ask"]) for shown in state(world)["queue"]] == [
        (step["id"], f"question:{SESSION_A}:2026-01-04T10:00:00Z", "Pick C")
    ]


def test_a_session_holding_two_handed_steps_asks_on_the_one_handed_last(world: World) -> None:
    """A session holding two handed steps asks on the one handed last."""
    put_item(world, "note:a", [SESSION_A])
    ids = []
    for need, sent_at in (("check:1", "2026-01-04T08:00:00Z"), ("check:2", "2026-01-04T08:30:00Z")):
        # Only the user's own put adds a step beside a handed one (SPEC NEXT-55).
        client, headers = (world.client, world.user()) if ids else (agent(world), world.agent())
        put = client.post("/api/steps", json={"item_key": "note:a", "need": need, "ask": f"Run {need}?"}, headers=headers)
        assert put.status_code == 200, put.text
        step_id = put.json()["step"]["id"]
        ids.append(step_id)
        handed = world.client.post(f"/api/steps/{step_id}/hand-off", json={"text": "Go on", "session_id": SESSION_A},
                                   headers=world.user())
        assert handed.status_code == 200, handed.text
        conn = db.connect(world.db_path)
        conn.execute("UPDATE messages SET sent_at = ? WHERE step_id = ?", (sent_at, step_id))
        conn.close()
    sync(world)
    sync(world)
    assert [(shown["id"], shown["ask"]) for shown in state(world)["queue"]] == [(ids[1], "Pick A or B")]
    assert [ask for step_id, _, _, _, ask in steps(world) if step_id == ids[0]] == ["Run check:1?"]


def test_an_agent_s_question_step_whose_need_ends_in_words_is_weighed_like_the_others(world: World) -> None:
    """An agent's question step whose need ends in words is weighed like the others."""
    put_item(world, "note:a", [])
    put = agent(world).post("/api/steps", json={"item_key": "note:a", "need": f"question:{SESSION_A}:follow-up",
                                                "ask": "Which export?", "session_id": SESSION_A}, headers=world.agent())
    assert put.status_code == 200, put.text
    sync(world)
    assert [step[1:4] for step in steps(world)] == [(f"question:{SESSION_A}:follow-up", "open", False)]
    world.cache.sessions = [make_session(SESSION_A, pid=101, state="working")]
    sync(world)
    assert steps(world)[0][2:4] == ("open", True)


def given_back(world: World) -> dict:
    """Session A's question step, handed to it at 09:30, rewritten and returned by it; A asks again at 10:00."""
    put_item(world, "note:a", [SESSION_A])
    sync(world)
    [step] = state(world)["queue"]
    handed = world.client.post(f"/api/steps/{step['id']}/hand-off", json={"text": "Why B?"}, headers=world.user())
    assert handed.status_code == 200, handed.text
    conn = db.connect(world.db_path)
    conn.execute("UPDATE messages SET sent_at = '2026-01-04T09:30:00Z' WHERE step_id = ?", (step["id"],))
    conn.close()
    rewrite = {"item_key": "note:a", "need": NEED_A, "ask": "B, because it keeps the export", "answers": ["Take B"]}
    assert agent(world).post("/api/steps", json=rewrite, headers=world.agent()).status_code == 200
    assert agent(world).post(f"/api/steps/{step['id']}/return", json={}, headers=world.agent()).status_code == 200
    world.cache.sessions = [replace(make_session(SESSION_A, pid=None), needs="pick C", updated_at="2026-01-04T10:00:00Z")]
    return step


def hand_on(world: World, step_id: int) -> None:
    """The user hands the step to session B."""
    on = world.client.post(f"/api/steps/{step_id}/hand-off", json={"text": "You take it", "session_id": SESSION_B},
                           headers=world.user())
    assert on.status_code == 200, on.text


def holding(world: World, step_id: int) -> tuple:
    """The step's need, ask and the session its hand-off went to."""
    conn = db.connect(world.db_path)
    try:
        return tuple(conn.execute(
            "SELECT s.need, s.ask, m.session_id FROM steps s LEFT JOIN messages m ON m.id = s.handoff_message_id"
            " WHERE s.id = ?", (step_id,)
        ).fetchone())
    finally:
        conn.close()


def test_a_question_step_the_session_gave_back_itself_asks_its_new_question(world: World) -> None:
    """A question step the session gave back itself asks its new question."""
    step = given_back(world)
    sync(world)
    sync(world)
    [shown] = state(world)["queue"]
    assert (shown["id"], shown["need"], shown["ask"], shown["answers"]) == (
        step["id"], f"question:{SESSION_A}:2026-01-04T10:00:00Z", "Pick C", []
    )


def test_a_shipped_item_s_question_step_asks_the_new_question_with_no_finish_step(world: World) -> None:
    """A shipped item's question step asks the new question with no finish step."""
    put_item(world, "note:a", [SESSION_A])
    sync(world)
    [step] = state(world)["queue"]
    response = agent(world).put("/api/items", json={"key": "note:a", "title": "note:a", "sessions": [SESSION_A],
                                                    "stage": "prod"}, headers=world.agent())
    assert response.status_code == 200, response.text
    handed = world.client.post(f"/api/steps/{step['id']}/hand-off", json={"text": "Why B?"}, headers=world.user())
    assert handed.status_code == 200, handed.text
    conn = db.connect(world.db_path)
    conn.execute("UPDATE messages SET sent_at = '2026-01-04T09:30:00Z' WHERE step_id = ?", (step["id"],))
    conn.close()
    # Left as it was, the step stays with the agent (SPEC NEXT-37) until the user takes it back.
    assert agent(world).post(f"/api/steps/{step['id']}/return", json={}, headers=world.agent()).status_code == 400
    assert world.client.post(f"/api/steps/{step['id']}/return", json={}, headers=world.user()).status_code == 200
    world.cache.sessions = [replace(make_session(SESSION_A, pid=None), needs="pick C", updated_at="2026-01-04T10:00:00Z")]
    sync(world)
    sync(world)
    assert [(shown["id"], shown["need"], shown["ask"]) for shown in state(world)["queue"]] == [
        (step["id"], f"question:{SESSION_A}:2026-01-04T10:00:00Z", "Pick C")
    ]


def test_a_refused_new_question_leaves_the_other_sessions_their_steps(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused new question leaves the other sessions their steps."""
    sync(world)
    upsert_step = store.upsert_step

    def refuse_a(conn, fields, actor):
        if fields["need"] == f"question:{SESSION_A}:2026-01-04T10:00:00Z":
            raise store.Invalid("figure of another item")
        return upsert_step(conn, fields, actor)

    monkeypatch.setattr(store, "upsert_step", refuse_a)
    world.cache.sessions = [
        replace(make_session(SESSION_A, pid=None), needs="pick C", updated_at="2026-01-04T10:00:00Z"),
        make_session(SESSION_B, pid=None),
    ]
    sync(world)
    assert sorted(need.split(":")[1] for _, need, status, _, _ in steps(world) if status == "open") == [SESSION_A, SESSION_B]


def test_a_new_question_waits_while_the_user_is_on_the_step(world: World) -> None:
    """A new question waits while the user is on the step."""
    sync(world)
    [step] = state(world)["queue"]
    world.client.post(f"/api/steps/{step['id']}/timer/start", headers=world.user())
    for needs, at in (("pick C", "2026-01-04T10:00:00Z"), ("pick D", "2026-01-04T11:00:00Z")):
        world.cache.sessions = [replace(make_session(SESSION_A, pid=None), needs=needs, updated_at=at)]
        sync(world)
        assert [(row[1], row[4]) for row in steps(world)] == [(NEED_A, "Pick A or B")]
    world.client.post(f"/api/steps/{step['id']}/timer/pause", headers=world.user())
    sync(world)
    assert [(row[0], row[1], row[2], row[4]) for row in steps(world)] == [
        (step["id"], f"question:{SESSION_A}:2026-01-04T11:00:00Z", "open", "Pick D")
    ]


def test_a_question_back_on_a_handed_step_covers_the_other_sessions_of_its_item(world: World) -> None:
    """A question back on a handed step covers the other sessions of its item."""
    put_item(world, "note:a", [SESSION_A, SESSION_B])
    step = handed_step(world, {"need": "merge", "ask": "Merge the export PR?", "answers": ["Merge it"]}, SESSION_A)
    world.cache.sessions = [make_session(SESSION_A, pid=None), make_session(SESSION_B, pid=None)]
    sync(world)
    assert [(row[0], row[2]) for row in steps(world)] == [(step["id"], "open")]


def test_a_handed_result_to_read_takes_the_question_s_need(world: World) -> None:
    """A handed result to read takes the question's need."""
    put_item(world, "note:a", [SESSION_A])
    sync(world)
    step = handed_step(world, {"need": "read:result", "ask": "Read: the export is ready"}, SESSION_A)
    world.cache.sessions = [replace(make_session(SESSION_A, pid=None), needs="pick C", updated_at="2026-01-04T10:00:00Z")]
    sync(world)
    sync(world)
    assert [(shown["id"], shown["need"], shown["ask"]) for shown in state(world)["queue"]] == [
        (step["id"], f"question:{SESSION_A}:2026-01-04T10:00:00Z", "Pick C")
    ]


def test_a_session_a_step_names_gets_no_item_of_its_own(world: World) -> None:
    """Check existing steps before creating a session item, so a covered session keeps its existing item."""
    put_item(world, "note:b", [])
    response = agent(world).post("/api/steps", json={"item_key": "note:b", "need": "decide", "ask": "Pick the export",
                                                     "session_id": SESSION_A}, headers=world.agent())
    assert response.status_code == 200, response.text
    sync(world)
    conn = db.connect(world.db_path)
    try:
        assert [key for (key,) in conn.execute("SELECT key FROM items ORDER BY id")] == ["note:b"]
    finally:
        conn.close()


def test_a_message_after_the_given_back_step_gives_way_to_the_new_question(world: World) -> None:
    """A message after the given back step gives way to the new question."""
    given_back(world)
    sent = world.client.post("/api/messages", json={"text": "Also check the export", "target": {"session_id": SESSION_A}},
                             headers=world.user())
    assert sent.status_code == 200, sent.text
    conn = db.connect(world.db_path)
    conn.execute("UPDATE messages SET sent_at = '2026-01-04T09:50:00Z' WHERE id = ?", (sent.json()["id"],))
    conn.close()
    sync(world)
    sync(world)
    assert [(shown["need"], shown["ask"]) for shown in state(world)["queue"]] == [
        (f"question:{SESSION_A}:2026-01-04T10:00:00Z", "Pick C")
    ]


def test_a_given_back_step_the_user_hands_on_while_the_poll_reads_it_keeps_what_it_holds(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A given back step the user hands on while the poll reads it keeps what it holds."""
    step = given_back(world)
    open_questions = questions._open_questions

    def handed_on_meanwhile(conn):
        read = open_questions(conn)
        hand_on(world, step["id"])
        return read

    monkeypatch.setattr(questions, "_open_questions", handed_on_meanwhile)
    sync(world)
    monkeypatch.setattr(questions, "_open_questions", open_questions)
    sync(world)
    assert holding(world, step["id"]) == (NEED_A, "B, because it keeps the export", SESSION_B)


def test_a_question_step_the_user_hands_on_keeps_what_that_agent_wrote(world: World) -> None:
    """A question step the user hands on keeps what that agent wrote."""
    step = given_back(world)
    sync(world)
    hand_on(world, step["id"])
    need = f"question:{SESSION_A}:2026-01-04T10:00:00Z"
    rewrite = {"item_key": "note:a", "need": need, "ask": "C, since B is gone"}
    assert agent(world).post("/api/steps", json=rewrite, headers=world.agent("b")).status_code == 200
    sync(world)
    assert holding(world, step["id"]) == (need, "C, since B is gone", SESSION_B)


@pytest.mark.parametrize("back", ["user takes it back", "agent returns it as it was", "agent wrote it before"])
def test_a_question_step_back_unchanged_from_the_hand_off_gives_way_to_the_new_question(world: World, back: str) -> None:
    """A question step back unchanged from the hand off gives way to the new question."""
    put_item(world, "note:a", [SESSION_A])
    sync(world)
    [step] = state(world)["queue"]
    if back == "agent wrote it before":
        rewrite = {"item_key": "note:a", "need": NEED_A, "ask": "B, because it keeps the export"}
        assert agent(world).post("/api/steps", json=rewrite, headers=world.agent()).status_code == 200
    handed = world.client.post(f"/api/steps/{step['id']}/hand-off", json={"text": "Why B?"}, headers=world.user())
    assert handed.status_code == 200, handed.text
    conn = db.connect(world.db_path)
    conn.execute("UPDATE messages SET sent_at = '2026-01-04T09:30:00Z' WHERE step_id = ?", (step["id"],))
    conn.execute("UPDATE step_versions SET at = '2026-01-04T09:10:00Z' WHERE step_id = ?", (step["id"],))
    conn.close()
    if back != "user takes it back":
        # The step reads as at the hand-off, so it stays with the agent (SPEC NEXT-37) until the user takes it back.
        refused = agent(world).post(f"/api/steps/{step['id']}/return", json={}, headers=world.agent())
        assert refused.status_code == 400 and "still reads as before" in refused.json()["detail"]
    returned = world.client.post(f"/api/steps/{step['id']}/return", json={}, headers=world.user())
    assert returned.status_code == 200, returned.text
    world.cache.sessions = [replace(make_session(SESSION_A, pid=None), needs="pick C", updated_at="2026-01-04T10:00:00Z")]
    sync(world)
    sync(world)
    assert [(shown["need"], shown["ask"]) for shown in state(world)["queue"]] == [
        (f"question:{SESSION_A}:2026-01-04T10:00:00Z", "Pick C")
    ]


def test_a_rewrite_before_the_last_hand_off_gives_way_to_the_new_question(world: World) -> None:
    """A rewrite before the last hand off gives way to the new question."""
    put_item(world, "note:a", [SESSION_A])
    sync(world)
    [step] = state(world)["queue"]
    for sent_at in ("2026-01-04T09:10:00Z", "2026-01-04T09:20:00Z"):
        handed = world.client.post(f"/api/steps/{step['id']}/hand-off", json={"text": "Why B?"}, headers=world.user())
        assert handed.status_code == 200, handed.text
        conn = db.connect(world.db_path)
        conn.execute("UPDATE messages SET sent_at = ? WHERE step_id = ? AND sent_at IS NULL", (sent_at, step["id"]))
        conn.close()
        if sent_at.endswith("09:10:00Z"):
            rewrite = {"item_key": "note:a", "need": NEED_A, "ask": "B, because it keeps the export"}
            assert agent(world).post("/api/steps", json=rewrite, headers=world.agent()).status_code == 200
            conn = db.connect(world.db_path)
            conn.execute("UPDATE step_versions SET at = '2026-01-04T09:15:00Z' WHERE step_id = ?", (step["id"],))
            conn.close()
            assert agent(world).post(f"/api/steps/{step['id']}/return", json={}, headers=world.agent()).status_code == 200
        else:
            # The second hand-off left the step as it was: the agent may not return it so (SPEC NEXT-37); the user
            # takes it back.
            refused = agent(world).post(f"/api/steps/{step['id']}/return", json={}, headers=world.agent())
            assert refused.status_code == 400, refused.text
            assert world.client.post(f"/api/steps/{step['id']}/return", json={}, headers=world.user()).status_code == 200
    world.cache.sessions = [replace(make_session(SESSION_A, pid=None), needs="pick C", updated_at="2026-01-04T10:00:00Z")]
    sync(world)
    sync(world)
    assert [(shown["need"], shown["ask"]) for shown in state(world)["queue"]] == [
        (f"question:{SESSION_A}:2026-01-04T10:00:00Z", "Pick C")
    ]


def test_a_step_handed_after_the_rewritten_question_step_takes_the_new_question(world: World) -> None:
    """A step handed after the rewritten question step takes the new question."""
    put_item(world, "note:a", [SESSION_A])
    sync(world)
    [question] = state(world)["queue"]
    handed = world.client.post(f"/api/steps/{question['id']}/hand-off", json={"text": "Why B?"}, headers=world.user())
    assert handed.status_code == 200, handed.text
    rewrite = {"item_key": "note:a", "need": NEED_A, "ask": "B, because it keeps the export"}
    assert agent(world).post("/api/steps", json=rewrite, headers=world.agent()).status_code == 200
    assert agent(world).post(f"/api/steps/{question['id']}/return", json={}, headers=world.agent()).status_code == 200
    merge = agent(world).post("/api/steps", json={"item_key": "note:a", "need": "merge", "ask": "Merge the export PR?",
                                                  "answers": ["Merge it"]}, headers=world.agent())
    assert merge.status_code == 200, merge.text
    merge_id = merge.json()["step"]["id"]
    handed = world.client.post(f"/api/steps/{merge_id}/hand-off", json={"text": "Rebase first", "session_id": SESSION_A},
                               headers=world.user())
    assert handed.status_code == 200, handed.text
    conn = db.connect(world.db_path)
    conn.execute("UPDATE messages SET sent_at = '2026-01-04T09:10:00Z' WHERE step_id = ?", (question["id"],))
    conn.execute("UPDATE step_versions SET at = '2026-01-04T09:15:00Z' WHERE step_id = ?", (question["id"],))
    conn.execute("UPDATE messages SET sent_at = '2026-01-04T09:20:00Z' WHERE step_id = ?", (merge_id,))
    conn.close()
    world.cache.sessions = [replace(make_session(SESSION_A, pid=None), needs="pick C", updated_at="2026-01-04T10:00:00Z")]
    sync(world)
    sync(world)
    assert [(shown["id"], shown["need"], shown["ask"]) for shown in state(world)["queue"]] == [
        (merge_id, f"question:{SESSION_A}:2026-01-04T10:00:00Z", "Pick C")
    ]


def test_a_step_the_user_hands_on_while_the_poll_reads_it_stays_with_the_new_session(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A step the user hands on while the poll reads it stays with the new session."""
    put_item(world, "note:a", [SESSION_A])
    step = handed_step(world, {"need": "merge", "ask": "Merge the export PR?", "answers": ["Merge it"]}, SESSION_A)
    handed_to = questions._handed_to

    def hand_on(conn, session):
        held = handed_to(conn, session)
        if held is not None:
            back = world.client.post(f"/api/steps/{step['id']}/return", json={}, headers=world.user())
            assert back.status_code == 200, back.text
            on = world.client.post(f"/api/steps/{step['id']}/hand-off", json={"text": "You take it", "session_id": SESSION_B},
                                   headers=world.user())
            assert on.status_code == 200, on.text
        return held

    monkeypatch.setattr(questions, "_handed_to", hand_on)
    sync(world)
    conn = db.connect(world.db_path)
    try:
        session_id, need, holder = conn.execute(
            "SELECT s.session_id, s.need, m.session_id FROM steps s JOIN messages m ON m.id = s.handoff_message_id"
            " WHERE s.id = ?", (step["id"],)
        ).fetchone()
    finally:
        conn.close()
    assert (session_id, need, holder) == (None, "merge", SESSION_B)


def test_a_refused_question_leaves_the_handed_step_as_it_was_and_comes_next_poll(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused question leaves the handed step as it was and comes next poll."""
    put_item(world, "note:a", [SESSION_A])
    step = handed_step(world, {"need": "merge", "ask": "Merge the export PR?", "answers": ["Merge it"]}, SESSION_A)
    upsert_step = store.upsert_step

    def refuse(*args, **kwargs):
        raise store.Invalid("refused once")

    monkeypatch.setattr(store, "upsert_step", refuse)
    sync(world)
    assert steps(world)[0][1] == "merge"
    monkeypatch.setattr(store, "upsert_step", upsert_step)
    sync(world)
    [shown] = state(world)["queue"]
    assert (shown["id"], shown["need"], shown["ask"]) == (step["id"], NEED_A, "Pick A or B")


@pytest.mark.parametrize("author", ["user", "cockpit"])
def test_a_question_back_on_its_handed_step_stays_while_another_step_names_the_session(world: World, author: str) -> None:
    """A question back on its handed step stays while another step names the session."""
    sync(world)
    [step] = state(world)["queue"]
    handed = world.client.post(f"/api/steps/{step['id']}/hand-off", json={"text": "Keep it"}, headers=world.user())
    assert handed.status_code == 200, handed.text
    conn = db.connect(world.db_path)
    conn.execute("UPDATE messages SET sent_at = '2026-01-04T09:30:00Z', author = ? WHERE step_id = ?", (author, step["id"]))
    conn.commit()
    conn.close()
    put_item(world, "note:b", [])
    response = agent(world).post("/api/steps", json={"item_key": "note:b", "need": "decide", "ask": "Pick the export",
                                                     "session_id": SESSION_A}, headers=world.agent())
    assert response.status_code == 200, response.text
    world.cache.sessions = [replace(make_session(SESSION_A, pid=None), needs="pick C", updated_at="2026-01-04T10:00:00Z")]
    sync(world)
    sync(world)
    assert {shown["id"]: shown["ask"] for shown in state(world)["queue"]}[step["id"]] == "Pick C"


def test_a_status_note_makes_no_question_step_and_withdraws_an_open_one(world: World, monkeypatch) -> None:
    sync(world)
    assert len(state(world)["queue"]) == 1
    note = "Nothing else has changed. Step 10, the approval to share the list, is in Sam's queue waiting for their answer."
    monkeypatch.setattr(questions, "last_message", lambda session_id: note)
    sync(world)
    assert state(world)["queue"] == []
    assert steps(world)[0][2:4] == ("open", True)






def test_a_message_asks_when_it_ends_in_a_question_or_says_what_input_it_needs() -> None:
    assert questions.asks_in("Commit aaaa111 has A.\n\nWhich one?")
    assert questions.asks_in("Which one do you want?\n\n1. Keep the export\n2. Drop it")
    assert questions.asks_in("Shall I merge **now?**")
    assert questions.asks_in("The reminder preview is ready.\n\nneeds input: Sam's choice of reminder wording.")
    assert not questions.asks_in("Was it the cache? No, the index.\n\nNext I post the comments.")
    assert not questions.asks_in("Done:\n- the fix\n- the test")
    assert questions.asks_in("Two decisions:\n1. Should I deploy?\n2. Which export format?")
    assert questions.asks_in("Two decisions:\n1. Should I deploy?\n   It is ready on staging.\n2. Which export format?\n   Both work.")
    assert not questions.asks_in("Was it the cache?\n No, it was the index; I fixed it.")
    assert not questions.asks_in("1. Was it the cache?\n\n No, it was the index; I fixed it.")
    assert not questions.asks_in("1.  Was it the cache?\n\n   No, it was the index; I fixed it.")
    assert questions.asks_in("Which environment should I use?\n1. Staging\n   - Uses test data\n\n   Recommended for checks.")
    assert not questions.asks_in("Should I? I checked.\n\n1. Is it cached?\n\nIt was not; I fixed it.")
    assert questions.asks_in("Should I run this command?\n\n```bash\ngit push origin main\n```")
    assert questions.asks_in("Which plan?\n\n| Plan | Cost |\n| --- | --- |\n| A | 3 |")
    assert not questions.asks_in("Done.\n\n```\nContinue?\n```")
    assert not questions.asks_in("Done.\n\n```\nContinue?\n```\n\n- the fix\n- the test")
    assert questions.asks_in("Should I run this?\n\n```bash\ngit push origin main\n```\n\n- Yes, push it\n- No, leave it local")
    assert not questions.asks_in("")


def test_user_s_reply_closes_the_question_once_the_session_moves_on(world: World) -> None:
    sync(world)
    [step] = state(world)["queue"]
    conn = db.connect(world.db_path)
    conn.execute(
        "INSERT INTO messages (author, session_id, step_id, text, state, created_at) VALUES ('user', ?, ?, 'A', 'sent', ?)",
        (SESSION_A, step["id"], db.now_iso()),
    )
    conn.close()
    sync(world)
    assert steps(world)[0][2] == "open"
    world.cache.sessions = [make_session(SESSION_A, pid=101, state="working")]
    sync(world)
    assert steps(world)[0][2:4] == ("done", False)
    assert [closed["id"] for closed in state(world)["closed_recently"]["steps"]] == [step["id"]]


def test_a_question_the_session_dropped_is_withdrawn_and_its_next_one_comes_back(world: World) -> None:
    sync(world)
    world.cache.sessions = [make_session(SESSION_A, pid=101, state="working")]
    sync(world)
    assert steps(world)[0][2:4] == ("open", True)
    assert state(world)["queue"] == []
    world.cache.sessions = [replace(make_session(SESSION_A, pid=None), needs="pick C", updated_at="2026-01-04T10:00:00Z")]
    sync(world)
    [step] = state(world)["queue"]
    assert (step["ask"], step["need"]) == ("Pick C", f"question:{SESSION_A}:2026-01-04T10:00:00Z")


def test_a_session_that_stopped_again_to_ask_gets_its_new_question(world: World) -> None:
    """a message from elsewhere moved the session from question A to question B between two polls. The
    step asks B in its place."""
    sync(world)
    [old] = state(world)["queue"]
    world.cache.sessions = [replace(make_session(SESSION_A, pid=None), needs="pick C", updated_at="2026-01-04T10:00:00Z")]
    sync(world)
    [new] = state(world)["queue"]
    assert (new["id"], new["ask"], new["need"]) == (old["id"], "Pick C", f"question:{SESSION_A}:2026-01-04T10:00:00Z")


def test_a_question_step_handed_to_the_session_keeps_its_next_question(world: World) -> None:
    """SPEC NEXT-37 with the question the session asks on its handed step is the step's question now."""
    sync(world)
    [step] = state(world)["queue"]
    handed = world.client.post(f"/api/steps/{step['id']}/hand-off", json={"text": "Keep it"}, headers=world.user())
    assert handed.status_code == 200, handed.text
    conn = db.connect(world.db_path)
    conn.execute("UPDATE messages SET sent_at = '2026-01-04T09:30:00Z' WHERE step_id = ?", (step["id"],))
    conn.commit()
    conn.close()
    world.cache.sessions = [replace(make_session(SESSION_A, pid=None), needs="pick C", updated_at="2026-01-04T10:00:00Z")]
    sync(world)
    sync(world)
    [(step_id, need, status, withdrawn, ask)] = steps(world)
    assert (step_id, need, status, withdrawn, ask) == (
        step["id"], f"question:{SESSION_A}:2026-01-04T10:00:00Z", "open", 0, "Pick C"
    )


def test_a_question_stays_open_while_its_session_is_stopped(world: World) -> None:
    # Cockpit's idle stop (SPEC SESSION-15) stops a session that waits an hour for the user's answer.
    sync(world)
    [step] = state(world)["queue"]
    world.cache.sessions = [make_session(SESSION_A, pid=None, state="stopped")]
    sync(world)
    assert [entry["id"] for entry in state(world)["queue"]] == [step["id"]]
    response = world.client.post(
        f"/api/steps/{step['id']}/hand-off", json={"text": "Take A", "session_id": SESSION_A}, headers=world.user()
    )
    assert response.status_code == 200, response.text
    assert [entry["id"] for entry in state(world)["with_agents"]] == [step["id"]]


def test_an_unanswered_question_user_is_on_stays_until_they_pause(world: World) -> None:
    sync(world)
    [step] = state(world)["queue"]
    world.client.post(f"/api/steps/{step['id']}/timer/start", headers=world.user())
    world.cache.sessions = [make_session(SESSION_A, pid=101, state="working")]
    sync(world)
    assert [entry["id"] for entry in state(world)["queue"]] == [step["id"]]
    world.client.post(f"/api/steps/{step['id']}/timer/pause", headers=world.user())
    sync(world)
    assert state(world)["queue"] == []


def test_a_result_to_read_leaves_the_session_s_question_a_step(world: World) -> None:
    """SPEC VIEW-2: a read step asks nothing, so it neither covers nor replaces the session's question."""
    put_item(world, "note:a", [SESSION_A])
    response = agent(world).post(
        "/api/steps",
        json={"item_key": "note:a", "need": "read:result", "ask": "Read: the export is ready", "session_id": SESSION_A},
        headers=world.agent(),
    )
    assert response.status_code == 200, response.text
    sync(world)
    sync(world)
    assert sorted(step["need"] for step in state(world)["queue"]) == [NEED_A, "read:result"]


def test_a_step_that_names_the_session_replaces_its_question(world: World) -> None:
    put_item(world, "note:a", [SESSION_A])
    sync(world)
    put_item(world, "note:b", [])
    response = agent(world).post(
        "/api/steps",
        json={"item_key": "note:b", "need": "decide", "ask": "Pick A or B for the export", "session_id": SESSION_A},
        headers=world.agent(),
    )
    assert response.status_code == 200, response.text
    sync(world)
    assert [step["ask"] for step in state(world)["queue"]] == ["Pick A or B for the export"]


def test_a_step_on_an_archived_item_leaves_the_session_s_question_open(world: World) -> None:
    put_item(world, "note:b", [])
    response = agent(world).post(
        "/api/steps",
        json={"item_key": "note:b", "need": "decide", "ask": "Pick A or B for the export", "session_id": SESSION_A},
        headers=world.agent(),
    )
    assert response.status_code == 200, response.text
    conn = db.connect(world.db_path)
    conn.execute("UPDATE items SET archived = 1 WHERE key = 'note:b'")
    conn.close()
    sync(world)
    [step] = state(world)["queue"]
    rewrite = {"item_id": step["item_id"], "need": NEED_A, "ask": "Keep the export?", "answers": ["Keep it", "Drop it"]}
    assert agent(world).post("/api/steps", json=rewrite, headers=world.agent()).status_code == 200
    sync(world)
    [step] = state(world)["queue"]
    assert (step["ask"], [answer["text"] for answer in step["answers"]]) == ("Keep the export?", ["Keep it", "Drop it"])


def test_the_sweep_rewrites_the_question_in_place_and_a_dismissed_one_stays_closed(world: World) -> None:
    sync(world)
    [step] = state(world)["queue"]
    rewrite = {"item_id": step["item_id"], "need": NEED_A, "ask": "Keep the export?", "answers": ["Keep it", "Drop it"]}
    assert agent(world).post("/api/steps", json=rewrite, headers=world.agent()).status_code == 200
    sync(world)
    [step] = state(world)["queue"]
    assert (step["ask"], [answer["text"] for answer in step["answers"]]) == ("Keep the export?", ["Keep it", "Drop it"])
    assert agent(world).post(f"/api/steps/{step['id']}/dismiss", json={}, headers=world.agent()).status_code == 200
    sync(world)
    assert [(need, status) for _, need, status, *_ in steps(world)] == [(NEED_A, "dismissed")]


def test_a_handed_off_question_leaves_the_session_s_next_question_to_a_new_step(world: World) -> None:
    sync(world)
    [step] = state(world)["queue"]
    world.client.post(f"/api/steps/{step['id']}/hand-off", json={"text": "Keep it"}, headers=world.user())
    world.cache.sessions = [replace(make_session(SESSION_A, pid=None), needs="pick C", updated_at="2026-01-04T10:00:00Z")]
    sync(world)
    board = state(world)
    assert [entry["id"] for entry in board["with_agents"]] == [step["id"]]
    assert [entry["ask"] for entry in board["queue"]] == ["Pick C"]


def test_the_ask_and_why_keep_to_the_limits_and_leave_ids_out(world: World) -> None:
    session = make_session(SESSION_A, pid=None)
    long = replace(session, needs="decide " + "whether to keep the export " * 8)
    conn = db.connect(world.db_path)
    try:
        assert len(questions._ask(conn, long)) <= 120
        assert questions._ask(conn, long).endswith("…")
        assert questions._ask(conn, replace(session, needs="merge aaaa111")) == 'Answer session "session 1111"'
        assert questions._ask(conn, replace(session, needs="")) == 'Answer session "session 1111"'
        assert questions._why(conn, replace(session, name="fix bbbb222")) == (
            "A session stopped to ask you this and waits for your answer.")
        why = questions._why(conn, replace(session, name="Review the list export and reminder settings " * 5))
        assert why == "A session stopped to ask you this and waits for your answer."
        assert len(why) <= store.TEXT_LIMITS["why"]
    finally:
        conn.close()


def test_a_failed_listing_is_marked_so_questions_are_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(account: config.Account, warnings: list[str]) -> list:
        raise subprocess.CalledProcessError(1, ["claude", "agents"])

    monkeypatch.setattr(sessions, "list_account", fail)
    cache = sessions.SessionCache(config.ACCOUNTS, dict, list)
    listing = cache.snapshot(wait=False)
    assert listing.sessions == []
    assert listing.failed


def test_a_question_skips_a_completed_item_for_the_session_s_own(world: World) -> None:
    # CONTROL-27: the user completed the item, so the session's next question goes on an item of its own.
    put_item(world, "note:a", [SESSION_A])
    conn = db.connect(world.db_path)
    conn.execute("UPDATE items SET stage = 'done' WHERE key = 'note:a'")
    conn.close()
    sync(world)
    [step] = state(world)["queue"]
    assert step["item"]["key"] == f"session:{SESSION_A}"


def test_a_question_puts_no_step_when_the_session_s_own_item_is_closed_too(world: World) -> None:
    put_item(world, "note:a", [SESSION_A])
    sync(world)  # its first question makes no item of its own: it goes on note:a
    conn = db.connect(world.db_path)
    conn.execute("DELETE FROM steps")
    with db.transaction(conn):
        store.upsert_item(conn, {"key": f"session:{SESSION_A}", "title": "Own item", "sessions": [SESSION_A]}, "cockpit")
    conn.execute("UPDATE items SET stage = 'done'")
    conn.close()
    sync(world)
    assert state(world)["queue"] == []
    assert steps(world) == []
