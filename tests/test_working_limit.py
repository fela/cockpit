"""At most WORKING_LIMIT sessions work at once (SPEC SESSION-23): a message that would put one more to work waits
for a place when an agent or Cockpit sent it by itself, and goes at once when it is the user's doing."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from cockpit import cli, db, delivery, store, sweeper
from cockpit.sessions import Session
from tests.conftest import ORIGIN, SESSION_A, SESSION_B, World, make_session
from tests.test_api import put_item, put_step, run_sql
from tests.test_delivery import listen, message, send, send_as_agent

SESSION_C = "33333333-3333-4333-8333-333333333333"
SESSION_D = "44444444-4444-4444-8444-444444444444"


def at_work(count: int) -> list[Session]:
    return [
        replace(make_session(f"c0c0c0{n:02d}-0000-4000-8000-000000000000", pid=5000 + n, state="working"), status="busy")
        for n in range(count)
    ]


def stopped(session_id: str, tmp_path: Path) -> Session:
    return make_session(session_id, pid=None, cwd=str(tmp_path))


def resumes(world: World, monkeypatch, shows_work: bool = True) -> None:
    """The fake `claude`: a resume gives the session a process. The listing shows it at work from then on, or still
    shows it idle (`shows_work` False), as the real listing does for the first seconds."""
    monkeypatch.setattr(delivery, "RESUME_POLL_SECONDS", 0.01)

    def runner(account, args, cwd):
        if "--resume" in args:
            target = args[args.index("--resume") + 1]
            world.cache.sessions = [
                replace(each, pid=99, state="working" if shows_work else each.state, status="busy" if shows_work else "idle")
                if each.session_id == target else each
                for each in world.cache.sessions
            ]
            world.runner.output = f"backgrounded · {target[:8]}\n"
        return world.runner(account, args, cwd)

    world.deliverer.run = runner


def places_due(world: World) -> list[int]:
    conn = db.connect(world.db_path)
    try:
        return world.deliverer.places_due(conn)
    finally:
        conn.close()


def held(sent: dict) -> bool:
    return sent["state"] == "queued" and sent["capacity_since"] is not None and sent["waits_since"] is None


def test_an_agents_message_waits_for_a_place_and_goes_when_one_is_free(world: World, tmp_path: Path, monkeypatch) -> None:
    resumes(world, monkeypatch)
    world.cache.sessions = [stopped(SESSION_A, tmp_path), *at_work(store.WORKING_LIMIT)]
    waiting = send_as_agent(world, {"session_id": SESSION_A})
    assert held(waiting), waiting
    assert world.runner.calls == []
    assert places_due(world) == []
    world.cache.sessions = [stopped(SESSION_A, tmp_path), *at_work(store.WORKING_LIMIT - 1)]
    assert places_due(world) == [waiting["id"]]
    world.deliverer.send(waiting["id"])
    sent = message(world, waiting["id"])
    assert sent["state"] == "sent" and sent["via"] == "resume", sent
    assert places_due(world) == []


def test_an_agents_message_goes_at_once_while_a_place_is_free(world: World, tmp_path: Path, monkeypatch) -> None:
    resumes(world, monkeypatch)
    world.cache.sessions = [stopped(SESSION_A, tmp_path), *at_work(store.WORKING_LIMIT - 1)]
    sent = send_as_agent(world, {"session_id": SESSION_A})
    assert sent["state"] == "sent" and sent["via"] == "resume", sent
    assert sent["capacity_since"] is None


def test_the_users_message_goes_at_once_past_the_limit(world: World, tmp_path: Path, monkeypatch) -> None:
    resumes(world, monkeypatch)
    world.cache.sessions = [stopped(SESSION_A, tmp_path), *at_work(store.WORKING_LIMIT)]
    sent = send(world, {"session_id": SESSION_A})
    assert sent["state"] == "sent" and sent["via"] == "resume", sent


def test_a_message_to_a_session_already_at_work_takes_no_place(world: World, tmp_path: Path) -> None:
    busy = replace(make_session(SESSION_A, pid=4242, state="working", cwd=str(tmp_path)), status="busy")
    world.cache.sessions = [busy, *at_work(store.WORKING_LIMIT)]
    with listen(tmp_path / "socks" / "4242.sock") as received:
        sent = send_as_agent(world, {"session_id": SESSION_A}, "Rebased")
    assert sent["state"] == "sent" and sent["via"] == "socket", sent
    assert b"Rebased" in received[0]
    assert world.deliverer._places == []


def test_the_sweepers_session_takes_no_place(world: World, tmp_path: Path, monkeypatch) -> None:
    resumes(world, monkeypatch)
    sweeper = replace(at_work(1)[0], session_id=SESSION_C, id=SESSION_C[:8], name=store.SWEEP_SESSION_NAME)
    world.cache.sessions = [stopped(SESSION_A, tmp_path), sweeper, *at_work(store.WORKING_LIMIT - 1)]
    assert len(store.working_sessions(world.cache.sessions)) == store.WORKING_LIMIT - 1
    sent = send_as_agent(world, {"session_id": SESSION_A})
    assert sent["state"] == "sent", sent
    # Its launch and a message to it start no counted work, so neither waits.
    assert not delivery.starts_work({"session_id": None, "new_name": store.SWEEP_SESSION_NAME}, None)
    assert not delivery.starts_work({"session_id": SESSION_C}, replace(sweeper, pid=None, state="done", status=""))
    assert delivery.starts_work({"session_id": None, "new_name": "Fix the form"}, None)
    assert delivery.starts_work({"session_id": SESSION_A}, stopped(SESSION_A, tmp_path))
    assert not delivery.starts_work({"session_id": SESSION_A}, None)  # the session is gone: the delivery fails


def test_what_the_user_did_goes_at_once_and_what_agents_and_cockpit_start_waits(world: World) -> None:
    put_item(world, sessions=[SESSION_A])
    put_step(world)
    conn = db.connect(world.db_path)
    try:
        def made(author: str) -> dict:
            return store.create_message(conn, author, "Text", {"session_id": SESSION_A}, world.settings.checkouts_dir)

        with db.transaction(conn):
            users, agents, wake, result, answer, run = (
                made("user"), made("agent:app-fix"), made("cockpit"), made("cockpit"), made("cockpit"), made("user")
            )
            step_id = conn.execute("SELECT id FROM steps").fetchone()[0]
            conn.execute(
                "INSERT INTO command_runs (step_id, session_id, command_text, cwd, started_at, result_message_id)"
                " VALUES (?, ?, 'make', '/', ?, ?)",
                (step_id, SESSION_A, db.now_iso(), result["id"]),
            )
            conn.execute(
                "INSERT INTO permission_requests (kind, state, session_id, cwd, tool_name, tool_input, tool_use_id,"
                " reason, count, created_at, last_at, message_id)"
                " VALUES ('asked', 'allowed', ?, '/', 'Bash', '{}', 'tool-1', '', 1, ?, ?, ?)",
                (SESSION_A, db.now_iso(), db.now_iso(), answer["id"]),
            )
            conn.execute(
                "INSERT INTO schedules (name, prompt, cwd, every, at, title, project, author, created_at, updated_at,"
                " last_slot, last_message_id) VALUES ('review', 'Review', '/', 'daily', '09:00', 'Review', 'cockpit',"
                " 'user', ?, ?, '2026-01-04', ?)",
                (db.now_iso(), db.now_iso(), run["id"]),
            )
        assert not delivery.may_wait(conn, users)
        assert delivery.may_wait(conn, agents)
        assert delivery.may_wait(conn, wake)
        assert not delivery.may_wait(conn, wake | {"as_prompt": 1})  # the prompt Cockpit sends on the user's press
        assert not delivery.may_wait(conn, agents | {"hands_off": 1})
        assert not delivery.may_wait(conn, result)  # the result of a command the user ran
        assert not delivery.may_wait(conn, answer)  # the answer to a permission the user allowed
        assert delivery.may_wait(conn, run)  # a scheduled run, though the user set the schedule
    finally:
        conn.close()


def test_the_longest_wait_goes_first_and_no_new_message_overtakes(world: World, tmp_path: Path, monkeypatch) -> None:
    resumes(world, monkeypatch, shows_work=False)
    waiting = [stopped(each, tmp_path) for each in (SESSION_A, SESSION_B, SESSION_C, SESSION_D)]
    world.cache.sessions = [*waiting, *at_work(store.WORKING_LIMIT)]
    first = send_as_agent(world, {"session_id": SESSION_A}, "First")
    second = send_as_agent(world, {"session_id": SESSION_B}, "Second")
    again = send_as_agent(world, {"session_id": SESSION_A}, "First again")
    third = send_as_agent(world, {"session_id": SESSION_C}, "Third")
    assert all(held(each) for each in (first, second, again, third))
    assert places_due(world) == []
    # One place is free: the longest wait takes it, and the second message to the same session goes along.
    world.cache.sessions = [*waiting, *at_work(store.WORKING_LIMIT - 1)]
    assert places_due(world) == [first["id"], again["id"]]
    # A new message does not take the free place from the ones that wait.
    late = send_as_agent(world, {"session_id": SESSION_D}, "Late")
    assert held(late), late
    assert world.runner.calls == []
    # A message whose session works by now, or is gone, needs no place and goes: the first reaches the working
    # session, the second fails and says so.
    working_b = replace(waiting[1], pid=4243, state="working", status="busy")
    world.cache.sessions = [waiting[0], working_b, waiting[3], *at_work(store.WORKING_LIMIT - 1)]
    assert places_due(world) == [second["id"], third["id"]]
    # The two for one session take one place between them.
    world.cache.sessions = [*waiting, *at_work(store.WORKING_LIMIT - 1)]
    with listen(tmp_path / "socks" / "99.sock") as received:
        world.deliverer.send(first["id"])
        world.deliverer.send(again["id"])
    assert message(world, first["id"])["via"] == "resume"
    assert message(world, again["id"])["via"] == "socket" and b"First again" in received[0]
    assert len(world.deliverer._places) == 1
    assert places_due(world) == []


def one_just_resumed(world: World, tmp_path: Path, monkeypatch) -> dict:
    """SESSION_A was just resumed into the last free place and the listing still shows it idle; the message to
    SESSION_B, which this returns, waits."""
    resumes(world, monkeypatch, shows_work=False)
    world.cache.sessions = [stopped(SESSION_A, tmp_path), stopped(SESSION_B, tmp_path), *at_work(store.WORKING_LIMIT - 1)]
    sent = send_as_agent(world, {"session_id": SESSION_A})
    assert sent["state"] == "sent" and sent["via"] == "resume", sent
    assert not next(each for each in world.cache.sessions if each.session_id == SESSION_A).working_now
    waiting = send_as_agent(world, {"session_id": SESSION_B})
    assert held(waiting), waiting
    assert places_due(world) == []
    return waiting


def test_a_session_just_resumed_counts_before_the_listing_shows_it_at_work(world: World, tmp_path: Path, monkeypatch) -> None:
    waiting = one_just_resumed(world, tmp_path, monkeypatch)
    # Once the listing shows it at work it counts once, not twice: with one other session done, a place is free.
    world.cache.sessions = [
        replace(each, state="working", status="busy") if each.session_id == SESSION_A else each
        for each in world.cache.sessions[:-1]
    ]
    assert len(store.working_sessions(world.cache.sessions)) == store.WORKING_LIMIT - 1
    assert places_due(world) == [waiting["id"]]
    assert world.deliverer._places == []


def test_a_resume_the_listing_never_shows_at_work_gives_its_place_back(world: World, tmp_path: Path, monkeypatch) -> None:
    waiting = one_just_resumed(world, tmp_path, monkeypatch)
    (place,) = world.deliverer._places
    assert place.session_id == SESSION_A
    place.sent_at -= delivery.PLACE_SECONDS
    assert places_due(world) == [waiting["id"]]


def test_a_session_keeps_its_one_place_over_a_second_delivery(world: World, tmp_path: Path, monkeypatch) -> None:
    one_just_resumed(world, tmp_path, monkeypatch)
    (place,) = world.deliverer._places
    place.sent_at -= delivery.PLACE_SECONDS - 5  # the place of the first delivery is about to end
    with listen(tmp_path / "socks" / "99.sock") as received:
        again = send_as_agent(world, {"session_id": SESSION_A}, "One more thing")
    assert again["state"] == "sent" and again["via"] == "socket", again
    assert b"One more thing" in received[0]
    # The same place, counted from the second delivery on.
    assert world.deliverer._places == [place]
    assert time.monotonic() - place.sent_at < delivery.PLACE_SECONDS - 5


def test_a_delivery_that_fails_keeps_its_place_for_a_while(world: World, tmp_path: Path) -> None:
    world.runner.returncode = 1
    world.cache.sessions = [stopped(SESSION_A, tmp_path), stopped(SESSION_B, tmp_path), *at_work(store.WORKING_LIMIT - 1)]
    failed = send_as_agent(world, {"session_id": SESSION_A})
    assert failed["state"] == "failed", failed
    # The session may have started all the same, so the place stays taken.
    (place,) = world.deliverer._places
    assert place.session_id == SESSION_A and place.sent_at is not None
    world.runner.returncode = 0
    waiting = send_as_agent(world, {"session_id": SESSION_B})
    assert held(waiting), waiting
    place.sent_at -= delivery.PLACE_SECONDS
    assert places_due(world) == [waiting["id"]]


def test_a_stall_wake_up_is_not_repeated_while_the_earlier_one_waits_for_a_place(world: World) -> None:
    agent = TestClient(world.client.app, base_url=ORIGIN)
    agent.put("/api/items", json={"key": "note:a", "title": "Work note:a", "sessions": [SESSION_C]}, headers=world.agent("sweep"))
    item_id = world.client.get("/api/state").json()["items"][0]["id"]
    items = [{"id": item_id, "title": "Work note:a", "priority": "normal", "stalled_since": "2026-02-04T17:00:00Z", "waiting_on": []}]
    conn = db.connect(world.db_path)
    try:
        now = datetime(2026, 2, 4, 19, 0, tzinfo=UTC)
        db.kv_set(conn, "sweeps_paused", {"at": "2026-02-04T18:40:00Z", "away_since": "2026-02-04T16:40:00Z"})
        (first,) = sweeper.stall_wakes(conn, items, Path("/tmp"), now)
        assert not store.wake_waits_for_place(conn, item_id)
        conn.execute("UPDATE messages SET capacity_since = ? WHERE id = ?", (db.now_iso(), first))
        assert store.wake_waits_for_place(conn, item_id)
        again = now + sweeper.STALL_WAKE_AGAIN
        assert sweeper.stall_wakes(conn, items, Path("/tmp"), again) == []
        conn.execute("UPDATE messages SET state = 'sent' WHERE id = ?", (first,))
        assert len(sweeper.stall_wakes(conn, items, Path("/tmp"), again)) == 1
    finally:
        conn.close()


def test_a_restart_keeps_the_messages_that_wait_for_a_place(world: World, tmp_path: Path) -> None:
    world.cache.sessions = [stopped(SESSION_A, tmp_path), *at_work(store.WORKING_LIMIT)]
    waiting = send_as_agent(world, {"session_id": SESSION_A})
    conn = db.connect(world.db_path)
    try:
        assert delivery.mark_interrupted(conn) == 0
    finally:
        conn.close()
    assert held(message(world, waiting["id"]))
    assert places_due(world) == []


def test_a_message_that_waits_for_a_place_can_be_set_aside(world: World, tmp_path: Path) -> None:
    world.cache.sessions = [stopped(SESSION_A, tmp_path), *at_work(store.WORKING_LIMIT)]
    waiting = send_as_agent(world, {"session_id": SESSION_A})
    agent = TestClient(world.client.app, base_url=ORIGIN)
    set_aside = agent.post(
        f"/api/messages/{waiting['id']}/dismiss", json={"reason": "relayed another way"}, headers=world.agent("sweep")
    )
    assert set_aside.status_code == 200, set_aside.text
    unsent = message(world, waiting["id"])
    assert unsent["state"] == "failed"
    assert unsent["detail"] == "Not sent: set aside while it waited for a free place."
    world.cache.sessions = [stopped(SESSION_A, tmp_path)]
    assert places_due(world) == []


def test_send_now_sends_a_message_that_waits_for_a_place_and_stops_no_session(
    world: World, tmp_path: Path, monkeypatch
) -> None:
    resumes(world, monkeypatch)
    world.cache.sessions = [stopped(SESSION_A, tmp_path), *at_work(store.WORKING_LIMIT)]
    waiting = send_as_agent(world, {"session_id": SESSION_A})
    assert held(waiting), waiting
    agent = TestClient(world.client.app, base_url=ORIGIN)
    assert agent.post(f"/api/messages/{waiting['id']}/send-now", headers=world.agent()).status_code == 403
    response = world.client.post(f"/api/messages/{waiting['id']}/send-now", headers=world.user())
    assert response.status_code == 200, response.text
    sent = message(world, waiting["id"])
    assert sent["state"] == "sent" and sent["via"] == "resume", sent
    assert [args[1] for args, _ in world.runner.calls] == ["--bg"]
    assert world.client.post(f"/api/messages/{waiting['id']}/send-now", headers=world.user()).status_code == 409


def test_the_page_counts_the_limit_and_the_messages_that_wait(world: World, tmp_path: Path) -> None:
    world.cache.sessions = [stopped(SESSION_A, tmp_path), *at_work(store.WORKING_LIMIT)]
    waiting = send_as_agent(world, {"session_id": SESSION_A})
    state = world.client.get("/api/state").json()
    counts = state["counts"]
    assert (counts["agents_working"], counts["working_limit"], counts["waiting_for_place"]) == (
        store.WORKING_LIMIT, store.WORKING_LIMIT, 1,
    )
    assert waiting["id"] in [each["id"] for each in state["messages"]]
    run_sql(world, "UPDATE messages SET state = 'sent' WHERE id = ?", (waiting["id"],))
    assert world.client.get("/api/state").json()["counts"]["waiting_for_place"] == 0


def test_cockpit_send_says_that_the_message_waits_for_a_place() -> None:
    said = cli.wait_for_delivery({"id": 7, "state": "queued", "capacity_since": "2026-01-04T09:00:00Z"})
    assert said == (
        f"waits: at most {store.WORKING_LIMIT} sessions work at once, and every place is taken."
        " Cockpit sends message 7 when a place is free."
    )
