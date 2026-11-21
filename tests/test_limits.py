from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from cockpit import accounts, config, db, limits, store
from cockpit.sessions import LimitStop, Usage
from tests.conftest import SESSION_A, SESSION_B, World, make_session

NOW = datetime.now(UTC).replace(microsecond=0)
STOP_A = (NOW - timedelta(minutes=40)).strftime("%Y-%m-%dT%H:%M:%S.250Z")
STOP_B = (NOW - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S.500Z")


@pytest.fixture(autouse=True)
def models() -> dict[str, str]:
    return {}


@pytest.fixture(autouse=True)
def signed_in(claude_accounts: tuple[config.Account, config.Account]) -> None:
    """The personal account's state file names the address its usage records come under."""
    claude_accounts[0].state.parent.mkdir(parents=True, exist_ok=True)
    claude_accounts[0].state.write_text(json.dumps({"oauthAccount": {"emailAddress": "a@example.test"}}))


@pytest.fixture(autouse=True)
def stopped(monkeypatch: pytest.MonkeyPatch) -> dict[str, LimitStop]:
    stops = {SESSION_A: LimitStop(cooldown_stops=0, at=STOP_A, resets=None), SESSION_B: LimitStop(cooldown_stops=0, at=STOP_B, resets=None)}
    monkeypatch.setattr(limits, "limit_stop", stops.get)
    return stops


@pytest.fixture(autouse=True)
def answers(monkeypatch: pytest.MonkeyPatch, models: dict[str, str]) -> dict[str, tuple[datetime, bool]]:
    """Claude's newest answer to each session, and whether it went through (sessions.last_answer)."""
    found: dict[str, tuple[datetime, bool]] = {}
    monkeypatch.setattr(
        limits, "transcript_usage", lambda session_id: Usage(
            stop=None, answer=found.get(session_id), model=models.get(session_id, "claude-fable-5-2"), effort="")
    )
    return found


def usage(world: World, captured: datetime, week: float, resets: datetime) -> None:
    world.claude_rate_limits.write_text(json.dumps({"accounts": {"a@example.test": {
        "account": {},
        "rate_limits": {"five_hour": {"used_percentage": 20, "resets_at": (NOW + timedelta(hours=2)).timestamp()},
                        "seven_day": {"used_percentage": week, "resets_at": resets.timestamp()}},
        "captured_at": captured.timestamp(),
        "session_id": SESSION_A,
        "config_dir": "/home/x/.claude",
    }}}))


def spent(world: World) -> None:
    usage(world, NOW - timedelta(minutes=45), 100, NOW + timedelta(days=3))


def sync(world: World) -> list[int]:
    conn = db.connect(world.db_path)
    try:
        readings = accounts.readings(accounts.in_use(config.ACCOUNTS, []), world.claude_rate_limits)
        return limits.sync(conn, world.cache.sessions, readings, NOW, world.checkout.parent)
    finally:
        conn.close()


def steps(world: World) -> list[dict]:
    conn = db.connect(world.db_path)
    try:
        return db.rows(conn.execute("SELECT * FROM steps").fetchall())
    finally:
        conn.close()


def messages(world: World) -> list[dict]:
    conn = db.connect(world.db_path)
    try:
        return db.rows(conn.execute("SELECT * FROM messages ORDER BY id").fetchall())
    finally:
        conn.close()


def test_sessions_the_limit_stopped_wait_without_a_step_while_usage_is_spent(world: World) -> None:
    world.cache.sessions = [make_session(SESSION_A, pid=None), make_session(SESSION_B, pid=101)]
    spent(world)
    assert sync(world) == []
    assert sync(world) == []
    assert steps(world) == []
    assert messages(world) == []


def test_once_a_call_goes_through_each_stopped_session_is_told_to_continue_once(world: World, answers: dict) -> None:
    other = "33333333-3333-4333-8333-333333333333"
    world.cache.sessions = [
        make_session(SESSION_A, pid=None), make_session(SESSION_B, pid=101), make_session(other, pid=102, state="working")
    ]
    spent(world)
    sync(world)
    answers[other] = (NOW - timedelta(minutes=2), True)
    sent = sync(world)
    assert len(sent) == 2
    told = messages(world)
    assert [message["session_id"] for message in told] == [SESSION_A, SESSION_B]
    assert {message["author"] for message in told} == {"cockpit"}
    assert {message["step_id"] for message in told} == {None}
    assert told[0]["text"].startswith(f"Claude's usage works again: a call went through at {NOW - timedelta(minutes=2):%H:%M} UTC.")
    assert sync(world) == []
    assert len(messages(world)) == 2
    assert steps(world) == []


def test_sessions_that_did_not_stop_on_the_limit_are_not_told(world: World, stopped: dict, answers: dict) -> None:
    del stopped[SESSION_B]
    world.cache.sessions = [make_session(SESSION_B, pid=None), make_session(SESSION_A, pid=101, state="working")]
    answers[SESSION_A] = (NOW - timedelta(minutes=2), True)
    assert sync(world) == []


def test_a_session_cockpit_closed_after_its_stop_is_told_too(world: World, answers: dict) -> None:
    """Resume idle-closed sessions after their usage limits reset."""
    world.cache.sessions = [make_session(SESSION_A, pid=None, state="stopped"), make_session(SESSION_B, pid=None, state="done")]
    answers[SESSION_B] = (NOW - timedelta(minutes=2), True)
    assert len(sync(world)) == 1
    assert [message["session_id"] for message in messages(world)] == [SESSION_A]


def test_a_stopped_session_the_user_archived_or_an_agent_closed_is_left(world: World, answers: dict) -> None:
    other = "33333333-3333-4333-8333-333333333333"
    world.cache.sessions = [
        make_session(SESSION_A, pid=None, state="stopped"), make_session(SESSION_B, pid=None, state="stopped"),
        make_session(other, pid=102, state="working"),
    ]
    answers[other] = (NOW - timedelta(minutes=2), True)
    conn = db.connect(world.db_path)
    try:
        item = store.upsert_item(conn, {"key": "note:limits", "title": "Limits"}, "test")["id"]
        step = store.upsert_step(conn, {"item_id": item, "need": "x", "ask": "Do x?"}, "test")["step"]["id"]
        conn.execute(
            "INSERT INTO session_archives (session_id, step_id, asked_at) VALUES (?, ?, ?)",
            (SESSION_A, step, db.now_iso()),
        )
        conn.execute(
            "INSERT INTO events (at, actor, kind, data) VALUES (?, 'sweep', 'session.closed', ?)",
            (db.now_iso(), json.dumps({"session_id": SESSION_B})),
        )
        conn.commit()
    finally:
        conn.close()
    assert sync(world) == []


def test_an_archive_from_before_the_stop_does_not_keep_the_session_waiting(world: World, answers: dict) -> None:
    """The session took up work again after it was archived, then stopped on the limit."""
    other = "33333333-3333-4333-8333-333333333333"
    world.cache.sessions = [make_session(SESSION_A, pid=None, state="stopped"), make_session(other, pid=102, state="working")]
    answers[other] = (NOW - timedelta(minutes=2), True)
    conn = db.connect(world.db_path)
    try:
        item = store.upsert_item(conn, {"key": "note:limits", "title": "Limits"}, "test")["id"]
        step = store.upsert_step(conn, {"item_id": item, "need": "x", "ask": "Do x?"}, "test")["step"]["id"]
        conn.execute(
            "INSERT INTO session_archives (session_id, step_id, asked_at, failure) VALUES (?, ?, ?, 'stop failed')",
            (SESSION_A, step, (NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        conn.commit()
    finally:
        conn.close()
    assert len(sync(world)) == 1


def test_an_answer_from_before_a_move_says_nothing_of_the_new_account(world: World, answers: dict) -> None:
    other = "33333333-3333-4333-8333-333333333333"
    world.cache.sessions = [make_session(SESSION_A, pid=None), make_session(other, pid=102, state="working")]
    answers[other] = (NOW - timedelta(minutes=2), True)
    conn = db.connect(world.db_path)
    try:
        conn.execute(
            "INSERT INTO session_moves (session_id, account, at) VALUES (?, 'personal', ?)",
            (other, (NOW - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")),
        )
        conn.commit()
    finally:
        conn.close()
    assert sync(world) == []
    answers[other] = (NOW, True)
    assert len(sync(world)) == 1


def test_a_stop_more_than_a_week_old_is_left(world: World, stopped: dict) -> None:
    world.cache.sessions = [make_session(SESSION_A, pid=None, state="stopped")]
    old = (NOW - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    stopped[SESSION_A] = LimitStop(cooldown_stops=0, at=old, resets=NOW - timedelta(days=7))
    assert sync(world) == []


def test_a_fresh_record_with_room_is_no_sign_that_a_call_went_through(world: World, answers: dict) -> None:
    """The status line writes the record again at the time it runs, with the numbers of the last answer before the
    limit:"""
    world.cache.sessions = [make_session(SESSION_A, pid=101), make_session(SESSION_B, pid=102)]
    usage(world, NOW - timedelta(minutes=1), 95, NOW + timedelta(days=3))
    assert sync(world) == []
    # An answer that ended on the limit again is no sign either.
    answers[SESSION_B] = (NOW - timedelta(minutes=1), False)
    assert sync(world) == []


def test_a_reset_of_the_used_up_window_counts_as_usage_back(world: World) -> None:
    world.cache.sessions = [make_session(SESSION_A, pid=None)]
    usage(world, NOW - timedelta(minutes=45), 100, NOW - timedelta(minutes=1))
    [message_id] = sync(world)
    assert messages(world)[0]["text"].startswith("Claude's usage works again: the limit has reset.")
    assert message_id == messages(world)[0]["id"]


def test_a_record_from_before_the_first_stop_is_no_sign_of_usage(world: World) -> None:
    world.cache.sessions = [make_session(SESSION_A, pid=None), make_session(SESSION_B, pid=101)]
    usage(world, NOW - timedelta(hours=1), 40, NOW + timedelta(days=3))
    assert sync(world) == []


def test_a_message_after_the_stop_means_the_session_no_longer_waits(world: World, answers: dict) -> None:
    world.cache.sessions = [make_session(SESSION_A, pid=None), make_session(SESSION_B, pid=101)]
    spent(world)
    conn = db.connect(world.db_path)
    try:
        store.create_message(conn, "user", "should work again now", {"session_id": SESSION_A}, world.checkout.parent)
    finally:
        conn.close()
    answers[SESSION_A] = (NOW - timedelta(minutes=1), True)
    assert len(sync(world)) == 1
    assert [message["session_id"] for message in messages(world)] == [SESSION_A, SESSION_B]


def test_unreadable_usage_counts_as_not_back(world: World, caplog: pytest.LogCaptureFixture) -> None:
    world.cache.sessions = [make_session(SESSION_A, pid=None)]
    world.claude_rate_limits.write_text("{not json")
    assert sync(world) == []
    assert "reading Claude usage" in caplog.text


def test_a_session_wakes_at_the_reset_its_stop_names_without_a_new_record(world: World, stopped) -> None:
    # The last record said 99 %, taken before the stop: no record can come while every session waits.
    usage(world, NOW - timedelta(hours=2), 99, NOW + timedelta(days=3))
    world.cache.sessions = [make_session(SESSION_A, pid=None)]
    stopped[SESSION_A] = LimitStop(cooldown_stops=0, at=STOP_A, resets=NOW + timedelta(minutes=5))
    assert sync(world) == []
    stopped[SESSION_A] = LimitStop(cooldown_stops=0, at=STOP_A, resets=NOW - timedelta(minutes=1))
    assert len(sync(world)) == 1
    assert "the limit it stopped on ended at" in messages(world)[0]["text"]


@pytest.mark.parametrize("resets_at", [None, "soon", {}])
def test_a_used_up_window_without_a_readable_reset_is_no_sign(resets_at, caplog) -> None:
    """The status line writes the record; a bad window is logged and the stop waits for another sign."""
    now = datetime(2026, 1, 24, 12, 30, tzinfo=UTC)
    window = {"used_percentage": 100} | ({} if resets_at is None else {"resets_at": resets_at})
    record = {"rate_limits": {"seven_day": window}}
    assert limits.usage_back(record, now - timedelta(hours=1), now, None) is None
    assert "reading Claude usage from a record failed" in caplog.text


def test_a_message_that_reaches_the_session_while_usage_is_read_takes_the_place_of_continue(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The user's move queues its own continue (or an archive its message) while sync reads the sessions' answers,
    # after sync found the session waiting: sync then queues none, so the session gets one message.
    world.cache.sessions = [make_session(SESSION_A, pid=None)]
    usage(world, NOW - timedelta(minutes=5), 100, NOW - timedelta(minutes=10))

    def meanwhile(session_id):
        conn = db.connect(world.db_path)
        try:
            with db.transaction(conn):
                store.create_message(conn, "cockpit", "moved; continue", {"session_id": SESSION_A}, world.checkout.parent)
        finally:
            conn.close()
        return Usage(stop=None, answer=None, model="claude-fable-5-2", effort="")

    monkeypatch.setattr(limits, "transcript_usage", meanwhile)
    assert sync(world) == []
    assert [message["text"] for message in messages(world)] == ["moved; continue"]


def test_quota_recovery_queues_only_the_work_that_fits_capacity(world, stopped, answers):
    ids = [f"{n:08x}-1111-4111-8111-111111111111" for n in range(12)]
    for sid in ids:
        stopped[sid] = LimitStop(cooldown_stops=0, at=STOP_A, resets=None)
    world.cache.sessions = [make_session(sid, None, state="stopped") for sid in ids]
    answers[ids[0]] = (NOW - timedelta(minutes=1), True)
    queued = sync(world)
    assert len(queued) == limits.RECOVERY_CAPACITY
    # Pending resumes reserve their places before processes appear in the listing.
    assert sync(world) == []
    conn = db.connect(world.db_path)
    try:
        conn.execute("UPDATE messages SET state = 'sent', sent_at = ?", (NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),))
    finally:
        conn.close()
    assert sync(world) == []  # Delivery alone does not prove the cached listing saw the turn.
    from dataclasses import replace
    world.cache.sessions = [replace(session, updated_at=(NOW + timedelta(seconds=1)).isoformat())
                            for session in world.cache.sessions]
    assert len(sync(world)) == 2


def test_working_codex_tasks_use_recovery_capacity_without_being_resumed_as_claude(world, answers):
    from dataclasses import replace
    world.cache.sessions = [make_session(SESSION_A, None, state="stopped")]
    world.cache.sessions += [replace(make_session(f"codex-{n}", n + 100, state="working"),
                                     provider="codex", tempo="active") for n in range(limits.RECOVERY_CAPACITY)]
    answers[SESSION_A] = (NOW - timedelta(minutes=1), True)
    assert sync(world) == []


def test_opus_success_does_not_resume_fable_on_the_same_account(world, answers, models):
    world.cache.sessions = [make_session(SESSION_A, None), make_session(SESSION_B, 102, state="working")]
    models[SESSION_B] = "claude-opus-5-5"
    answers[SESSION_B] = (NOW - timedelta(minutes=1), True)
    assert sync(world) == []
    models[SESSION_B] = "claude-fable-5-2"
    assert len(sync(world)) == 1


def test_a_newer_same_model_failure_outweighs_an_older_success(world, answers):
    other = "33333333-3333-4333-8333-333333333333"
    world.cache.sessions = [make_session(SESSION_A, None), make_session(SESSION_B, None),
                            make_session(other, 102, state="working")]
    answers[other] = (NOW - timedelta(minutes=35), True)
    answers[SESSION_B] = (datetime.fromisoformat(STOP_B), False)
    assert sync(world) == []
    answers[other] = (NOW - timedelta(minutes=1), True)
    assert len(sync(world)) == 2


def test_unknown_model_cannot_establish_same_model_recovery(world, answers, models):
    world.cache.sessions = [make_session(SESSION_A, None), make_session(SESSION_B, 102, state="working")]
    models.update({SESSION_A: "", SESSION_B: ""})
    answers[SESSION_B] = (NOW - timedelta(minutes=1), True)
    assert sync(world) == []


def test_success_on_another_account_cannot_resume_the_model(world, answers):
    world.cache.sessions = [make_session(SESSION_A, None),
                            make_session(SESSION_B, 102, state="working", account="work")]
    answers[SESSION_B] = (NOW - timedelta(minutes=1), True)
    assert sync(world) == []


@pytest.mark.parametrize(("failures", "minutes"), [(1, 10), (2, 20), (3, 40), (4, 80)])
def test_cooldown_retries_wait_for_growing_backoff_and_queue_once(world, stopped, failures, minutes):
    world.cache.sessions = [make_session(SESSION_A, None)]
    # An account reset is not evidence that the cooldown on the model has ended.
    usage(world, NOW - timedelta(hours=1), 100, NOW - timedelta(minutes=1))
    stop_at = NOW - timedelta(minutes=minutes) + timedelta(seconds=1)
    stopped[SESSION_A] = LimitStop(at=stop_at.isoformat(), resets=None, cooldown_stops=failures)
    assert sync(world) == []
    stop_at -= timedelta(seconds=1)
    stopped[SESSION_A] = LimitStop(at=stop_at.isoformat(), resets=None, cooldown_stops=failures)
    assert len(sync(world)) == 1
    assert messages(world)[0]["text"].startswith(f"Claude's model cooldown retry: {minutes} minutes")
    assert sync(world) == []


def test_same_model_success_can_free_cooldown_before_backoff(world, stopped, answers):
    world.cache.sessions = [make_session(SESSION_A, None), make_session(SESSION_B, 102, state="working")]
    stopped[SESSION_A] = LimitStop(at=(NOW - timedelta(minutes=2)).isoformat(), resets=None, cooldown_stops=4)
    answers[SESSION_B] = (NOW - timedelta(minutes=1), True)
    assert len(sync(world)) == 1
    assert "a call went through" in messages(world)[0]["text"]


def test_pending_and_recently_sent_cooldown_retries_reserve_capacity(world, stopped):
    from dataclasses import replace
    ids = [f"{n:08x}-1111-4111-8111-111111111111" for n in range(12)]
    for sid in ids:
        stopped[sid] = LimitStop(at=STOP_A, resets=None, cooldown_stops=1)
    world.cache.sessions = [make_session(sid, None, state="stopped") for sid in ids]
    assert len(sync(world)) == limits.RECOVERY_CAPACITY
    assert sync(world) == []
    conn = db.connect(world.db_path)
    try:
        conn.execute("UPDATE messages SET state = 'sent', sent_at = ?", (NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),))
    finally:
        conn.close()
    assert sync(world) == []
    world.cache.sessions = [replace(session, updated_at=(NOW + timedelta(seconds=1)).isoformat())
                            for session in world.cache.sessions]
    assert len(sync(world)) == 2


def test_recovery_model_and_answer_come_from_one_snapshot(world, monkeypatch):
    world.cache.sessions = [make_session(SESSION_A, None), make_session(SESSION_B, 102, state="working")]
    reads = []

    def changing_transcript(session_id):
        reads.append(session_id)
        # A switches from Fable to Opus between reads. A second read would see an Opus success.
        if session_id == SESSION_B and reads.count(SESSION_B) > 1:
            return Usage(stop=None, answer=(NOW, True), model="claude-opus-5-5", effort="")
        return Usage(stop=None, answer=None, model="claude-fable-5-2", effort="")

    monkeypatch.setattr(limits, "transcript_usage", changing_transcript)
    assert sync(world) == []
    assert reads.count(SESSION_B) == 1
    # On the next poll Opus's success still cannot free Fable.
    assert sync(world) == []


def owned_item(world, **fields):
    from tests.test_api import put_item
    return put_item(world, key="note:quota-work", title="Recover unfinished work", stage="building",
                    sessions=[SESSION_A], **fields)


def recoverable(world, stopped):
    world.cache.sessions = [make_session(SESSION_A, None, state="stopped", cwd=str(world.checkout))]
    stopped[SESSION_A] = LimitStop(at=STOP_A, resets=None, cooldown_stops=1)


@pytest.mark.parametrize("condition", ["done", "archived", "replaced", "removed", "decision", "complete", "result",
                                       "snoozed", "future", "external"])
def test_automatic_recovery_respects_current_task_state(world, stopped, condition):
    from tests.test_api import put_item, put_step, run_sql
    recoverable(world, stopped)
    item = owned_item(world)
    if condition in ("done", "archived"):
        column = {"done": "stage", "archived": "archived"}[condition]
        run_sql(world, f"UPDATE items SET {column} = ? WHERE id = ?", ("done" if condition == "done" else 1, item["id"]))
    elif condition in ("replaced", "removed"):
        conn = db.connect(world.db_path)
        store.create_message(conn, "user", "Original work", {"item_id": item["id"], "session_id": SESSION_A}, world.checkout.parent)
        conn.execute("UPDATE messages SET created_at = ?", ((NOW - timedelta(hours=2)).isoformat(),))
        conn.close()
        put_item(world, key=item["key"], sessions=[SESSION_B, SESSION_A] if condition == "replaced" else [SESSION_B])
    elif condition in ("decision", "complete", "snoozed", "result"):
        step = put_step(world, item_key=item["key"], need="read:result" if condition in ("snoozed", "result") else condition)["step"]
        if condition == "result":
            run_sql(world, "UPDATE items SET stage = 'prod' WHERE id = ?", (item["id"],))
        if condition == "snoozed":
            run_sql(world, "UPDATE steps SET snoozed_until = ? WHERE id = ?", ((NOW + timedelta(days=1)).isoformat(), step["id"]))
    else:
        put_item(world, key=item["key"], waiting_on=[{"kind": "agent" if condition == "future" else "bot",
                  "who": SESSION_A if condition == "future" else "CI", "until": (NOW + timedelta(hours=1)).isoformat(),
                  **({"why": "check tomorrow"} if condition == "future" else {})}])
    assert sync(world) == []
    assert not any(m["quota_recovery_stop"] for m in messages(world))


@pytest.mark.parametrize("assignment", ["primary", "due_wait", "handed", "another_open_task", "progress_read_with_due_wait", "progress_read"])
def test_unfinished_assignments_recover_after_the_limit(world, stopped, assignment):
    from tests.test_api import put_item, put_step, run_sql
    recoverable(world, stopped)
    item = owned_item(world)
    if assignment == "due_wait":
        put_item(world, key=item["key"], sessions=[SESSION_B, SESSION_A], waiting_on=[{
            "kind": "agent", "who": SESSION_A, "until": (NOW - timedelta(minutes=5)).isoformat(), "why": "check upload"}])
    elif assignment == "handed":
        put_item(world, key=item["key"], sessions=[SESSION_B, SESSION_A])
        step = put_step(world, item_key=item["key"], need="decide", session_id=SESSION_A)["step"]
        conn = db.connect(world.db_path)
        message = store.create_message(conn, "user", "Proceed", {"session_id": SESSION_A, "item_id": item["id"]}, world.checkout.parent)
        conn.execute("UPDATE messages SET state = 'sent', created_at = ? WHERE id = ?",
                     ((NOW - timedelta(hours=1)).isoformat(), message["id"]))
        conn.execute("UPDATE steps SET handoff_message_id = ? WHERE id = ?", (message["id"], step["id"]))
        conn.close()
    elif assignment == "another_open_task":
        run_sql(world, "UPDATE items SET stage = 'done' WHERE id = ?", (item["id"],))
        put_item(world, key="note:still-needed", title="Still unfinished work", stage="building", sessions=[SESSION_A])
    elif assignment == "progress_read":
        put_step(world, item_key=item["key"], need="read:progress")
    elif assignment == "progress_read_with_due_wait":
        put_step(world, item_key=item["key"], need="read:progress")
        put_item(world, key=item["key"], waiting_on=[{"kind": "agent", "who": SESSION_A, "until": (NOW - timedelta(minutes=5)).isoformat(), "why": "check results"}])
    [mid] = sync(world)
    assert messages(world)[-1]["id"] == mid
    assert messages(world)[-1]["quota_recovery_stop"] == datetime.fromisoformat(STOP_A).isoformat()


def test_queue_revalidation_preserves_history_and_snooze_expiry(world, stopped):
    from tests.test_api import put_step, run_sql
    recoverable(world, stopped)
    item = owned_item(world)
    [mid] = sync(world)
    step = put_step(world, item_key=item["key"], need="read:progress")["step"]
    run_sql(world, "UPDATE steps SET snoozed_until = ? WHERE id = ?", ((NOW + timedelta(hours=1)).isoformat(), step["id"]))
    # Delivery rechecks the state even before the next recovery poll prunes its queue.
    world.deliverer.send(mid)
    cancelled = messages(world)[0]
    assert cancelled["state"] == "failed" and cancelled["dismissed_by"] == "cockpit"
    assert cancelled["text"].startswith("Claude's model cooldown retry:")
    assert world.runner.calls == []
    assert sync(world) == []
    run_sql(world, "UPDATE steps SET snoozed_until = ? WHERE id = ?", ((NOW - timedelta(seconds=1)).isoformat(), step["id"]))
    from tests.test_api import put_item
    put_item(world, key=item["key"], waiting_on=[{"kind": "agent", "who": SESSION_A, "until": (NOW - timedelta(minutes=5)).isoformat(), "why": "check upload"}])
    [new_mid] = sync(world)
    assert new_mid != mid
    assert len(messages(world)) == 2  # the cancelled message stays as evidence


def test_a_new_message_replaces_a_queued_retry_even_in_the_same_second(world, stopped, monkeypatch):
    from tests.test_api import put_item
    recoverable(world, stopped)
    owned_item(world)
    monkeypatch.setattr(db, "now_iso", lambda: NOW.strftime("%Y-%m-%dT%H:%M:%SZ"))
    [mid] = sync(world)
    conn = db.connect(world.db_path)
    user = store.create_message(conn, "user", "Continue my new request", {"session_id": SESSION_A}, world.checkout.parent)
    conn.close()
    world.deliverer.send(mid)
    rows = messages(world)
    assert rows[0]["dismissed_at"] and "newer message" in rows[0]["dismiss_reason"]
    assert rows[1]["id"] == user["id"] and rows[1]["state"] == "queued" and not rows[1]["quota_recovery_stop"]
    assert world.runner.calls == []


def test_task_reassignment_during_transcript_read_prevents_queuing(world, stopped, monkeypatch):
    from tests.test_api import run_sql
    recoverable(world, stopped)
    item = owned_item(world)
    def meanwhile(sid):
        run_sql(world, "UPDATE items SET sessions = ? WHERE id = ?", (json.dumps([SESSION_B, SESSION_A]), item["id"]))
        return Usage(stop=None, answer=None, model="claude-fable-5-2", effort="")
    monkeypatch.setattr(limits, "transcript_usage", meanwhile)
    assert sync(world) == []
    assert messages(world) == []


def test_a_user_retry_of_a_recovery_message_becomes_an_explicit_request(world, stopped):
    recoverable(world, stopped)
    [mid] = sync(world)
    conn = db.connect(world.db_path)
    conn.execute("UPDATE messages SET state = 'failed' WHERE id = ?", (mid,))
    retried = store.retry_message(conn, mid, "user")
    conn.close()
    assert retried["state"] == "queued" and not retried["quota_recovery_stop"]


def test_a_pause_on_one_current_task_holds_generic_session_recovery(world, stopped):
    from tests.test_api import put_item
    recoverable(world, stopped)
    owned_item(world)
    put_item(world, key="note:resting", title="Wait until tomorrow", sessions=[SESSION_A], stage="building",
             waiting_on=[{"kind": "agent", "who": SESSION_A, "until": (NOW + timedelta(days=1)).isoformat(),
                          "why": "user requested tomorrow", "by_user": True}])
    assert sync(world) == []


def test_a_later_delivered_instruction_reopens_work_behind_an_old_card(world, stopped):
    from tests.test_api import put_step, run_sql
    recoverable(world, stopped)
    item = owned_item(world)
    step = put_step(world, item_key=item["key"], need="complete")["step"]
    run_sql(world, "UPDATE steps SET prepared_at = ? WHERE id = ?", ((NOW - timedelta(hours=2)).isoformat(), step["id"]))
    conn = db.connect(world.db_path)
    asked = store.create_message(conn, "user", "Please fix another case", {"session_id": SESSION_A, "item_id": item["id"]}, world.checkout.parent)
    conn.execute("UPDATE messages SET state='sent', created_at=? WHERE id=?", ((NOW - timedelta(hours=1)).isoformat(), asked["id"]))
    conn.close()
    assert len(sync(world)) == 1


def test_an_obsolete_queued_stop_is_cancelled_without_resuming(world, stopped):
    recoverable(world, stopped)
    [mid] = sync(world)
    stopped[SESSION_A] = LimitStop(at=(NOW - timedelta(minutes=5)).isoformat(), resets=None, cooldown_stops=2)
    world.deliverer.send(mid)
    assert messages(world)[0]["dismissed_at"]
    assert world.runner.calls == []
    # The newer failure still has its full backoff; the old queued retry cannot bypass it.
    assert sync(world) == []


def test_a_quota_move_advisory_does_not_block_automatic_reset_recovery(world, stopped):
    from tests.test_api import put_step
    recoverable(world, stopped)
    item = owned_item(world)
    put_step(world, item_key=item["key"], need=f"move:{SESSION_A}:work:20261115")
    assert len(sync(world)) == 1


def test_a_dismissed_uncertain_delivery_still_blocks_another_automatic_retry(world, stopped):
    recoverable(world, stopped)
    [mid] = sync(world)
    conn = db.connect(world.db_path)
    conn.execute("UPDATE messages SET state='failed', dismissed_at=?, detail='Delivery uncertain: socket closed' WHERE id=?",
                 (db.now_iso(), mid))
    conn.close()
    assert sync(world) == []


def test_removing_an_owner_without_messages_still_preserves_the_assignment_history(world, stopped):
    from tests.test_api import put_item
    recoverable(world, stopped)
    item = owned_item(world)
    put_item(world, key=item["key"], sessions=[SESSION_B])
    assert sync(world) == []
    assert messages(world) == []


def test_revalidation_and_claim_share_one_transaction(world, stopped, monkeypatch):
    import sqlite3
    from cockpit import delivery
    recoverable(world, stopped)
    task = owned_item(world)
    [mid] = sync(world)
    connect = db.connect
    observed = []
    class InspectConnection:
        def __init__(self, conn):
            self.conn = conn
        def __getattr__(self, name):
            return getattr(self.conn, name)
        def execute(self, sql, parameters=()):
            if sql.startswith("UPDATE messages SET state = 'sending'"):
                assert self.conn.in_transaction
                other = sqlite3.connect(world.db_path, timeout=0)
                try:
                    with pytest.raises(sqlite3.OperationalError, match="locked"):
                        other.execute("UPDATE items SET stage='done' WHERE id=?", (task["id"],))
                    observed.append("claim holds the write lock after revalidation")
                finally:
                    other.close()
            return self.conn.execute(sql, parameters)
    monkeypatch.setattr(db, "connect", lambda path: InspectConnection(connect(path)))
    monkeypatch.setattr(world.deliverer, "_deliver_in_place", lambda *args: delivery.Sent("socket", SESSION_A, "fake transport"))
    world.deliverer.send(mid)
    assert observed == ["claim holds the write lock after revalidation"]
    assert messages(world)[0]["state"] == "sent"


def test_a_queued_claude_retry_stays_undelivered_after_handover(world, stopped):
    recoverable(world, stopped)
    owned_item(world)
    [mid] = sync(world)
    conn = db.connect(world.db_path)
    transfer = store.create_message(conn, "user", "Hand over", {"session_id": SESSION_B}, world.checkout.parent)
    conn.execute("INSERT INTO session_handovers(source_session_id,message_id,target_session_id,created_at) VALUES(?,?,?,?)",
                 (SESSION_A, transfer["id"], SESSION_B, db.now_iso()))
    conn.close()
    world.deliverer.send(mid)
    row = messages(world)[0]
    assert row["dismissed_at"] and row["session_id"] == SESSION_A
    assert world.runner.calls == []


def test_snoozing_a_handed_step_holds_automatic_recovery_too(world, stopped):
    from tests.test_api import put_step
    recoverable(world, stopped)
    task = owned_item(world)
    step = put_step(world, item_key=task["key"], need="decide", session_id=SESSION_A)["step"]
    conn = db.connect(world.db_path)
    asked = store.create_message(conn, "user", "Proceed", {"session_id": SESSION_A, "item_id": task["id"]}, world.checkout.parent)
    conn.execute("UPDATE messages SET state='sent', created_at=? WHERE id=?", ((NOW - timedelta(hours=1)).isoformat(), asked["id"]))
    conn.execute("UPDATE steps SET handoff_message_id=? WHERE id=?", (asked["id"], step["id"]))
    conn.close()
    [mid] = sync(world)
    response = world.client.post(f"/api/steps/{step['id']}/snooze", json={"until": (NOW + timedelta(days=1)).isoformat()},
                                 headers=world.user())
    assert response.status_code == 200, response.text
    world.deliverer.send(mid)
    rows = messages(world)
    assert rows[0]["id"] == asked["id"] and rows[0]["state"] == "sent"
    assert rows[1]["id"] == mid and rows[1]["dismissed_at"]
    assert sync(world) == []
    assert world.runner.calls == []
