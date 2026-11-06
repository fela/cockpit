from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from cockpit import accounts, config, db, limits, store
from cockpit.sessions import LimitStop
from tests.conftest import SESSION_A, SESSION_B, World, make_session

NOW = datetime.now(UTC).replace(microsecond=0)
STOP_A = (NOW - timedelta(minutes=40)).strftime("%Y-%m-%dT%H:%M:%S.250Z")
STOP_B = (NOW - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S.500Z")


@pytest.fixture(autouse=True)
def signed_in(claude_accounts: tuple[config.Account, config.Account]) -> None:
    """The personal account's state file names the address its usage records come under."""
    claude_accounts[0].state.parent.mkdir(parents=True, exist_ok=True)
    claude_accounts[0].state.write_text(json.dumps({"oauthAccount": {"emailAddress": "a@example.test"}}))


@pytest.fixture(autouse=True)
def stopped(monkeypatch: pytest.MonkeyPatch) -> dict[str, LimitStop]:
    stops = {SESSION_A: LimitStop(at=STOP_A, resets=None), SESSION_B: LimitStop(at=STOP_B, resets=None)}
    monkeypatch.setattr(limits, "limit_stop", stops.get)
    return stops


@pytest.fixture(autouse=True)
def answers(monkeypatch: pytest.MonkeyPatch) -> dict[str, tuple[datetime, bool]]:
    """Claude's newest answer to each session, and whether it went through (sessions.last_answer)."""
    found: dict[str, tuple[datetime, bool]] = {}
    monkeypatch.setattr(
        limits, "last_answer", lambda session_id, since: answer if (answer := found.get(session_id)) and answer[0] > since else None
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
    stopped[SESSION_A] = LimitStop(at=old, resets=NOW - timedelta(days=7))
    assert sync(world) == []


def test_a_fresh_record_with_room_is_no_sign_that_a_call_went_through(world: World, answers: dict) -> None:
    """The status line writes the record again at the time it runs, with the numbers of the last answer before the
    limit:"""
    world.cache.sessions = [make_session(SESSION_A, pid=101), make_session(SESSION_B, pid=102)]
    usage(world, NOW - timedelta(minutes=1), 97, NOW + timedelta(days=3))
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
    stopped[SESSION_A] = LimitStop(at=STOP_A, resets=NOW + timedelta(minutes=5))
    assert sync(world) == []
    stopped[SESSION_A] = LimitStop(at=STOP_A, resets=NOW - timedelta(minutes=1))
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

    def meanwhile(session_id, since):
        conn = db.connect(world.db_path)
        try:
            with db.transaction(conn):
                store.create_message(conn, "cockpit", "moved; continue", {"session_id": SESSION_A}, world.checkout.parent)
        finally:
            conn.close()

    monkeypatch.setattr(limits, "last_answer", meanwhile)
    assert sync(world) == []
    assert [message["text"] for message in messages(world)] == ["moved; continue"]
