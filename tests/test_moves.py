"""Moving a session to another Claude account only when the user says so (SPEC SESSION-18), the listing that
merges the accounts' lists, the sign-in expiry warning (SPEC UI-37) and the near-limit floor of the pick."""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cockpit import accounts, codex_agent, config, db, limits, moves, server, sessions, store, usage
from cockpit.config import Account
from cockpit.sessions import LimitStop, Move, Session
from tests.conftest import ORIGIN, SESSION_A, SESSION_B, World, make_session
from tests.test_accounts import fake_agents, record, row, sign_in

NOW = datetime.now(UTC).replace(microsecond=0)
STOP = (NOW - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def at(minutes_ago: int) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# The listing


def copy(account: str, updated: str, pid: int | None = None) -> Session:
    return replace(make_session(SESSION_A, pid=pid, account=account), updated_at=updated)


def test_a_session_in_both_lists_belongs_to_the_account_it_ran_on_last() -> None:
    merged = sessions.merge([[copy("personal", at(60))], [copy("work", at(5))]], {}, [])
    assert [session.account for session in merged] == ["work"]


def test_the_live_copy_wins_over_a_newer_stopped_one() -> None:
    merged = sessions.merge([[copy("personal", at(60), pid=7)], [copy("work", at(5))]], {}, [])
    assert [session.account for session in merged] == ["personal"]


def test_a_move_newer_than_the_last_run_puts_the_session_on_the_new_account() -> None:
    merged = sessions.merge([[copy("personal", at(60))]], {SESSION_A: Move("work", at(10))}, [])
    assert [session.account for session in merged] == ["work"]
    # Once it ran again on the old account (the user moved it back by hand, say), that run counts.
    merged = sessions.merge([[copy("personal", at(1))]], {SESSION_A: Move("work", at(10))}, [])
    assert [session.account for session in merged] == ["personal"]


def test_two_live_copies_show_on_the_page_and_log_an_error(caplog: pytest.LogCaptureFixture) -> None:
    warnings: list[str] = []
    with caplog.at_level(logging.ERROR):
        sessions.merge([[copy("personal", at(60), pid=7)], [copy("work", at(5), pid=8)]], {}, warnings)
    assert len(warnings) == 1 and "more than one Claude account" in warnings[0]
    assert "runs on more than one Claude account" in caplog.text


def test_one_failing_account_keeps_its_last_list_and_the_others_list(
    claude_accounts: tuple[Account, Account], monkeypatch: pytest.MonkeyPatch
) -> None:
    personal, work = claude_accounts
    sign_in(work, "sam@work.test")
    rows: dict[str, Any] = {"": [row(SESSION_A)], str(work.folder): [row(SESSION_B)]}
    fake_agents(monkeypatch, rows)
    cache = sessions.SessionCache((personal, work), dict, list)
    assert {session.account for session in cache.get()} == {"personal", "work"}
    real_run = sessions.subprocess.run

    def work_fails(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if f"CLAUDE_CONFIG_DIR={work.folder}" in command:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="daemon down")
        return real_run(command, **kwargs)

    monkeypatch.setattr(sessions.subprocess, "run", work_fails)
    rows[""] = []
    cache.invalidate()
    listing = cache.snapshot(wait=False)
    assert [(session.session_id, session.account) for session in listing.sessions] == [(SESSION_B, "work")]
    assert listing.failed
    assert any("work account's Claude sessions failed" in warning for warning in listing.warnings)


def test_find_session_raises_when_the_session_may_be_in_a_failed_list(
    claude_accounts: tuple[Account, Account], monkeypatch: pytest.MonkeyPatch
) -> None:
    personal, work = claude_accounts
    sign_in(work, "sam@work.test")

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if f"CLAUDE_CONFIG_DIR={work.folder}" in command:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="daemon down")
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps([row(SESSION_A)]), stderr="")

    monkeypatch.setattr(sessions.subprocess, "run", run)
    assert sessions.find_session((personal, work), {}, [], SESSION_A) is not None
    with pytest.raises(subprocess.SubprocessError):
        sessions.find_session((personal, work), {}, [], SESSION_B)
    # A moved session may run on the account whose listing failed, whatever the other list says.
    with pytest.raises(subprocess.SubprocessError):
        sessions.find_session((personal, work), {SESSION_A: Move("work", at(10))}, [], SESSION_A)


# Sign-in expiry


def signed_in_until(account: Account, expires: datetime) -> None:
    account.folder.mkdir(parents=True, exist_ok=True)
    oauth = {"accessToken": "secret", "refreshTokenExpiresAt": int(expires.timestamp() * 1000)}
    (account.folder / ".credentials.json").write_text(json.dumps({"claudeAiOauth": oauth}))


def test_a_sign_in_ending_within_three_days_shows_on_the_page(claude_accounts: tuple[Account, Account]) -> None:
    personal, work = claude_accounts
    signed_in_until(personal, NOW + timedelta(days=20))
    signed_in_until(work, NOW + timedelta(days=2))
    [entry] = accounts.sign_ins((personal, work), NOW)
    assert entry["account"] == "work" and not entry["signed_out"]
    assert entry["text"].startswith("The work Claude account's sign-in ends ")
    assert entry["command"] == f"env CLAUDE_CONFIG_DIR={work.folder} claude auth login"
    assert "secret" not in str(entry)
    # Once it has ended, the account is signed out.
    signed_in_until(work, NOW - timedelta(hours=1))
    assert accounts.signed_out(work, NOW)
    [entry] = accounts.sign_ins((personal, work), NOW)
    assert entry["signed_out"] and entry["text"].startswith("The work Claude account is signed out: ")


def test_an_account_without_a_sign_in_file_has_no_expiry(claude_accounts: tuple[Account, Account]) -> None:
    assert accounts.sign_in_expires(claude_accounts[1]) is None
    assert accounts.sign_ins(claude_accounts, NOW) == []


def signed_out(account: Account, expires: datetime) -> None:
    """What Claude Code leaves when it fails to renew an account's access for good: blank tokens, the old expiry."""
    account.folder.mkdir(parents=True, exist_ok=True)
    oauth = {"accessToken": "", "refreshToken": "", "expiresAt": 0,
             "refreshTokenExpiresAt": int(expires.timestamp() * 1000)}
    (account.folder / ".credentials.json").write_text(json.dumps({"claudeAiOauth": oauth}))


def test_a_sign_in_claude_emptied_counts_as_signed_out_and_shows_on_the_page(
    claude_accounts: tuple[Account, Account],
) -> None:
    personal, work = claude_accounts
    signed_in_until(personal, NOW + timedelta(days=20))
    signed_out(work, NOW + timedelta(days=25))
    assert accounts.signed_out(work, NOW) and not accounts.signed_out(personal, NOW)
    [entry] = accounts.sign_ins((personal, work), NOW)
    assert entry["signed_out"] and entry["text"].startswith("The work Claude account is signed out: ")
    assert "new sessions go to the other account" in entry["text"]
    assert entry["command"] == f"env CLAUDE_CONFIG_DIR={work.folder} claude auth login"
    ended = accounts.sign_in_expires(work)
    assert ended is not None and ended <= datetime.now(UTC)


def test_new_work_goes_only_to_an_account_that_is_not_signed_out(claude_accounts: tuple[Account, Account]) -> None:
    personal, work = claude_accounts
    signed_in_until(personal, NOW + timedelta(days=20))
    signed_out(work, NOW + timedelta(days=25))
    assert accounts.usable([personal, work], NOW) == [personal]
    # With every account signed out, new work starts nowhere, and the page says so.
    signed_out(personal, NOW + timedelta(days=20))
    assert accounts.usable([personal, work], NOW) == []
    entries = accounts.sign_ins((personal, work), NOW)
    assert len(entries) == 2
    assert all("new sessions start nowhere until one is signed in" in entry["text"] for entry in entries)
    assert entries[0]["command"] == "env -u CLAUDE_CONFIG_DIR claude auth login"


# The pick


def test_new_work_skips_an_account_near_its_limit_unless_all_are(claude_accounts: tuple[Account, Account]) -> None:
    personal, work = claude_accounts
    stamp = NOW.timestamp()
    # Personal has more room per hour over its week, but its 5-hour window is at 95 %.
    found = {"personal": record(NOW, (95, 1), (10, 100), stamp), "work": record(NOW, (10, 4), (60, 30), stamp)}
    assert accounts.pick([personal, work], found, NOW, None, {})[0] == work
    found["work"] = record(NOW, (92, 4), (60, 100), stamp)
    account, why = accounts.pick([personal, work], found, NOW, None, {})
    # Both on course to run out: the one with the less full window and the sooner weekly reset.
    assert account == personal and why.startswith("it scores 0.0011 for new work")


def test_a_window_that_reset_is_not_near_its_limit() -> None:
    gone = {"rate_limits": {"five_hour": {"used_percentage": 99, "resets_at": NOW.timestamp() - 60}}}
    assert not usage.room(gone, NOW.timestamp(), [], None, 0).near


# Moving


@pytest.fixture
def work_signed_in(claude_accounts: tuple[Account, Account]) -> Account:
    sign_in(claude_accounts[1], "sam@work.test")
    return claude_accounts[1]


def move(world: World, body: dict[str, str], agent: str = "") -> Any:
    """The user's Move on the page, or an agent's `cockpit session move`."""
    if not agent:
        return world.client.post(f"/api/sessions/{SESSION_A}/move", json=body, headers=world.user())
    client = TestClient(world.client.app, base_url=ORIGIN)
    return client.post(f"/api/sessions/{SESSION_A}/move", json=body, headers=world.agent(agent))


def stopping(world: World, session: Session) -> None:
    """The fake `claude`: a stop leaves the session without a process."""
    world.cache.sessions = [session]

    def runner(account, args, cwd):
        if args[1] == "stop":
            world.cache.sessions = [replace(world.cache.sessions[0], pid=None, status="", state="done")]
        return world.runner(account, args, cwd)

    world.deliverer.run = runner


def moved_rows(world: World) -> list[dict]:
    conn = db.connect(world.db_path)
    try:
        return db.rows(conn.execute("SELECT * FROM session_moves").fetchall())
    finally:
        conn.close()


def test_moving_an_idle_live_session_stops_it_on_its_old_account_first(
    world: World, work_signed_in: Account, tmp_path: Path
) -> None:
    stopping(world, replace(make_session(SESSION_A, pid=4242, state="done", cwd=str(tmp_path)), job_state="done", tempo="idle"))
    response = move(world, {"account": "work"})
    assert response.status_code == 200, response.text
    assert response.json()["moved"] is True and response.json()["message_id"] is None
    assert [(args, account) for (args, _), account in zip(world.runner.calls, world.runner.accounts, strict=True)] == [
        (["claude", "stop", SESSION_A[:8]], "personal")
    ]
    [moved] = moved_rows(world)
    assert (moved["session_id"], moved["account"]) == (SESSION_A, "work")


def waiting_rows(world: World) -> list[dict]:
    conn = db.connect(world.db_path)
    try:
        return db.rows(conn.execute("SELECT session_id, account, actor FROM session_move_waits").fetchall())
    finally:
        conn.close()


def busy(state: str, status: str, tempo: str, in_flight: list[str], cwd: Path) -> Session:
    return replace(make_session(SESSION_A, pid=4242, state=state, cwd=str(cwd)), job_state="working", status=status,
                   tempo=tempo, in_flight=in_flight)


def test_a_busy_session_names_what_keeps_it_busy(tmp_path: Path) -> None:
    assert busy("working", "busy", "active", [], tmp_path).busy_with == ["a turn under way"]
    assert busy("done", "busy", "idle", ["local_bash", "local_agent", "local_bash"], tmp_path).busy_with == [
        "a background command or Monitor", "a subagent"]
    assert busy("working", "idle", "idle", ["session_cron"], tmp_path).busy_with == ["a scheduled prompt or wakeup"]
    dialog = replace(busy("blocked", "idle", "blocked", [], tmp_path), needs="allow Bash")
    assert dialog.busy_with == ["a dialog that waits for an answer (allow Bash)"]
    # The reminder session
    stale = busy("working", "idle", "idle", [], tmp_path)
    assert stale.turn_over and stale.busy_with == []
    assert sessions.as_dict(busy("working", "busy", "active", ["local_agent"], tmp_path))["busy_with"] == [
        "a turn under way", "a subagent"]


def test_a_move_of_a_busy_session_waits_and_happens_once_its_turn_is_over(
    world: World, work_signed_in: Account, tmp_path: Path
) -> None:
    world.cache.sessions = [busy("done", "busy", "idle", ["local_bash"], tmp_path)]
    response = move(world, {"account": "work"})
    assert response.status_code == 200, response.text
    assert response.json()["moved"] is False and response.json()["waits"] is True
    assert response.json()["detail"] == (
        f"session {SESSION_A[:8]} still has a background command or Monitor; Cockpit moves it to the work account"
        " once that is over")
    assert moved_rows(world) == [] and world.runner.calls == []
    assert waiting_rows(world) == [{"session_id": SESSION_A, "account": "work", "actor": "user"}]
    [shown] = [s for s in world.client.get("/api/state", headers=world.user()).json()["sessions"] if s["session_id"] == SESSION_A]
    assert shown["move_waits"] == "work" and shown["busy_with"] == ["a background command or Monitor"]

    assert moves.waits_due(world.cache.sessions, world.db_path) == []
    stopping(world, replace(world.cache.sessions[0], status="idle", in_flight=[]))
    [due] = moves.waits_due(world.cache.sessions, world.db_path)
    server.move_waiting(world.deliverer, world.cache, due)
    [moved] = moved_rows(world)
    assert (moved["session_id"], moved["account"]) == (SESSION_A, "work")
    assert [args for args, _ in world.runner.calls] == [["claude", "stop", SESSION_A[:8]]]
    assert waiting_rows(world) == []
    # A second worker the next poll started for the same wait finds it ended, and does nothing.
    server.move_waiting(world.deliverer, world.cache, due)
    assert len(world.runner.calls) == 1 and len(moved_rows(world)) == 1


def test_a_waiting_move_that_fails_ends_its_wait(
    world: World, work_signed_in: Account, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    world.cache.sessions = [busy("done", "busy", "idle", ["local_bash"], tmp_path)]
    assert move(world, {"account": "work"}).json()["waits"] is True
    world.cache.sessions = [replace(world.cache.sessions[0], status="idle", in_flight=[])]

    def failing(account, args, cwd):
        return subprocess.CompletedProcess(args, 1, "", "no such session")

    world.deliverer.run = failing
    [due] = moves.waits_due(world.cache.sessions, world.db_path)
    # The user asked again meanwhile: a worker for the older wait leaves the newer one standing.
    newer = dict(due, asked_at="2099-01-01T00:00:00Z")
    conn = db.connect(world.db_path)
    try:
        conn.execute("UPDATE session_move_waits SET asked_at = ?", (newer["asked_at"],))
    finally:
        conn.close()
    server.move_waiting(world.deliverer, world.cache, due)
    assert len(waiting_rows(world)) == 1
    with caplog.at_level(logging.ERROR):
        server.move_waiting(world.deliverer, world.cache, newer)
    assert waiting_rows(world) == [] and moved_rows(world) == [] and "asked for failed" in caplog.text


def test_a_move_back_to_the_current_account_ends_the_wait(world: World, work_signed_in: Account, tmp_path: Path) -> None:
    world.cache.sessions = [busy("working", "busy", "active", [], tmp_path)]
    assert move(world, {"account": "work"}).json()["waits"] is True
    response = move(world, {"account": "personal"})
    assert response.json()["moved"] is False and "already on the personal account" in response.json()["detail"]
    assert waiting_rows(world) == []


def test_a_waiting_move_whose_session_is_gone_ends_with_an_error(
    world: World, work_signed_in: Account, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    world.cache.sessions = [busy("working", "busy", "active", [], tmp_path)]
    assert move(world, {"account": "work"}).json()["waits"] is True
    # A listing without the session makes the wait due; the move's own lookup then finds it gone.
    [due] = moves.waits_due([], world.db_path)
    world.cache.sessions = []
    with caplog.at_level(logging.ERROR):
        server.move_waiting(world.deliverer, world.cache, due)
    assert waiting_rows(world) == [] and "no Claude session" in caplog.text


def test_an_account_without_a_sign_in_takes_no_session(world: World) -> None:
    response = move(world, {"account": "work"})
    assert response.status_code == 409
    assert "has no sign-in" in response.json()["detail"]


def test_the_sweeper_may_not_move_a_session(world: World, work_signed_in: Account) -> None:
    response = move(world, {"account": "work"}, "sweep")
    assert response.status_code == 403
    assert moved_rows(world) == []


def test_a_limit_stopped_session_is_told_to_continue_on_the_new_account(
    world: World, work_signed_in: Account, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(limits, "limit_stop", {SESSION_A: LimitStop(STOP, None)}.get)
    world.cache.sessions = [make_session(SESSION_A, pid=None, cwd=str(tmp_path), state="blocked")]
    # The server's lookup: the listing merged with the recorded moves.
    world.deliverer.lookup = lambda session_id: next(
        (s for s in sessions.merge([world.cache.sessions], moves.moved(world.db_path), []) if s.session_id == session_id),
        None,
    )

    def runner(account, args, cwd):
        if "--resume" in args:
            world.cache.sessions = [replace(world.cache.sessions[0], pid=4242, state="working", account=account.name)]
        return world.runner(account, args, cwd)

    world.deliverer.run = runner
    world.runner.output = f"backgrounded · {SESSION_A[:8]}\n"
    response = move(world, {"account": "work"}, "export-checks")
    assert response.status_code == 200, response.text
    conn = db.connect(world.db_path)
    try:
        message = store.get_message(conn, response.json()["message_id"])
    finally:
        conn.close()
    assert message["session_id"] == SESSION_A
    assert "moved you to the work account" in message["text"]
    assert message["state"] == "sent", message["detail"]
    calls = [(args[:3], account) for (args, _), account in zip(world.runner.calls, world.runner.accounts, strict=True)]
    assert calls == [(["claude", "--bg", "--resume"], "work")]


# The recommendation


def item(world: World, priority: str) -> int:
    conn = db.connect(world.db_path)
    try:
        fields = {"key": "export-check", "title": "export check", "priority": priority, "sessions": [SESSION_A]}
        return store.upsert_item(conn, fields, "sweep")["id"]
    finally:
        conn.close()


def recommend(world: World, readings: dict[str, Any]) -> list[dict]:
    conn = db.connect(world.db_path)
    try:
        in_use = accounts.in_use(config.ACCOUNTS, [])
        moves.sync(conn, world.cache.sessions, in_use, readings, NOW)
        return db.rows(conn.execute("SELECT * FROM steps WHERE need LIKE 'move:%' ORDER BY id").fetchall())
    finally:
        conn.close()


@pytest.fixture
def limit_stopped(world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(limits, "limit_stop", {SESSION_A: LimitStop(STOP, NOW + timedelta(hours=3))}.get)
    world.cache.sessions = [make_session(SESSION_A, pid=None, cwd=str(tmp_path))]


ROOMY = {"personal": record(NOW, (100, 3), (40, 50), NOW.timestamp()), "work": record(NOW, (10, 2), (30, 60), NOW.timestamp())}


def test_a_limit_stopped_session_on_a_high_item_gets_a_move_step(
    world: World, work_signed_in: Account, limit_stopped: None
) -> None:
    item(world, "high")
    [step] = recommend(world, ROOMY)
    assert step["ask"] == "Move this item's session to the work Claude account, so it continues now?"
    assert step["session_id"] is None
    assert step["need"].startswith(f"move:{SESSION_A}:work:")
    assert "the personal account is at its usage limit until" in step["body"]
    assert "whose week is 30 % used" in step["body"]


def test_the_move_step_offers_yes_and_no_as_one_press_answers(
    world: World, work_signed_in: Account, limit_stopped: None
) -> None:
    """Offer direct answers to move a session or let it wait."""
    item(world, "high")
    [step] = recommend(world, ROOMY)
    assert step["answers"] == [
        {"text": "Yes, move it", "closes": False, "action": "move"},
        {"text": "No, let it wait", "closes": False, "action": "dismiss"},
    ]
    assert "**Yes, move it** continues it now" in step["body"]
    assert "**No, let it wait** closes this step." in step["body"]


def test_an_open_move_step_without_its_answers_gets_them_back(
    world: World, work_signed_in: Account, limit_stopped: None
) -> None:
    """A step put before the answers existed offers them on the next poll, also while its session runs again on the
    account at its limit (moves._held)."""
    item(world, "high")
    [step] = recommend(world, ROOMY)

    def strip() -> None:
        conn = db.connect(world.db_path)
        try:
            conn.execute("UPDATE steps SET answers = '[]' WHERE id = ?", (step["id"],))
            conn.commit()
        finally:
            conn.close()

    strip()
    [again] = recommend(world, ROOMY)
    assert again["id"] == step["id"]
    assert again["answers"] == moves.ANSWERS
    strip()
    world.cache.sessions = [replace(world.cache.sessions[0], state="working")]
    [held] = recommend(world, ROOMY)
    assert (held["id"], held["withdrawn_at"]) == (step["id"], None)
    assert held["answers"] == moves.ANSWERS


def test_an_agent_may_not_replace_a_move_step_s_answers(
    world: World, work_signed_in: Account, limit_stopped: None
) -> None:
    """A plain answer on a move step would message the stopped session, which then resumes on the account at its
    limit; only Cockpit sets them. Other fields stay the agent's to rewrite."""
    item(world, "high")
    [step] = recommend(world, ROOMY)
    agent = TestClient(world.client.app, base_url=ORIGIN)
    refused = agent.post(
        "/api/steps", json={"item_key": "export-check", "need": step["need"], "answers": ["Yes, move it"]}, headers=world.agent()
    )
    assert refused.status_code == 400 and "Cockpit sets the answers of a move step" in refused.text
    assert agent.post(
        "/api/steps", json={"item_key": "export-check", "need": step["need"], "score": 90}, headers=world.agent()
    ).status_code == 200


def test_an_agent_may_not_give_an_answer_an_action(world: World) -> None:
    item(world, "high")
    agent = TestClient(world.client.app, base_url=ORIGIN)
    response = agent.post(
        "/api/steps",
        json={"item_key": "export-check", "need": "decide:x", "ask": "Pick one?",
              "answers": [{"text": "Yes", "closes": False, "action": "move"}]},
        headers=world.agent(),
    )
    assert response.status_code == 400 and "action" in response.text


def message(world: World, created_at: datetime, step_ask: str | None) -> None:
    conn = db.connect(world.db_path)
    try:
        conn.execute(
            "INSERT INTO messages (author, session_id, text, state, step_ask, created_at) VALUES ('user', ?, 'go', 'sent', ?, ?)",
            (SESSION_A, step_ask, created_at.strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        conn.commit()
    finally:
        conn.close()


def test_the_step_names_the_user_s_message_the_stopped_turn_worked_on(
    world: World, work_signed_in: Account, limit_stopped: None
) -> None:
    """Name the message a stopped session was working on so the move has context."""
    item(world, "high")
    message(world, NOW - timedelta(minutes=33), "Run the Codex test now?")
    [step] = recommend(world, ROOMY)
    sent = (NOW - timedelta(minutes=33)).strftime("%H:%M")
    assert f'It stopped while working on your message of {sent} UTC on "Run the Codex test now?".' in step["body"]


def test_a_resume_while_the_account_is_still_at_its_limit_keeps_the_step(
    world: World, work_signed_in: Account, limit_stopped: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the move step through a resume while its account remains at the usage limit."""
    item(world, "high")
    [step] = recommend(world, ROOMY)
    message(world, NOW - timedelta(minutes=1), None)
    world.cache.sessions = [replace(world.cache.sessions[0], state="working")]
    [kept] = recommend(world, ROOMY)
    assert (kept["id"], kept["withdrawn_at"]) == (step["id"], None)
    # It stops again: the same step stays, its reason on the new reset time.
    world.cache.sessions = [replace(world.cache.sessions[0], state="blocked")]
    again = LimitStop(NOW.strftime("%Y-%m-%dT%H:%M:%S.000Z"), NOW + timedelta(hours=4))
    monkeypatch.setattr(limits, "limit_stop", {SESSION_A: again}.get)
    [still] = recommend(world, ROOMY)
    assert (still["id"], still["withdrawn_at"]) == (step["id"], None)
    assert f"until {NOW + timedelta(hours=4):%a %H:%M} UTC" in still["why"]


def test_a_resumed_session_that_stopped_or_has_a_duplicate_step_keeps_one_step_at_most(
    world: World, work_signed_in: Account, limit_stopped: None
) -> None:
    item(world, "high")
    [step] = recommend(world, ROOMY)
    conn = db.connect(world.db_path)
    try:
        store.upsert_step(conn, {"item_id": step["item_id"], "need": f"move:{SESSION_A}:work:20260115T100000", "ask": "Move?"}, "cockpit")
    finally:
        conn.close()
    message(world, NOW - timedelta(minutes=1), None)
    world.cache.sessions = [replace(world.cache.sessions[0], state="working")]
    assert [row["withdrawn_at"] is None for row in recommend(world, ROOMY)] == [True, False]
    # A session closed after the resume holds no step, though its account is still at the limit.
    world.cache.sessions = [replace(world.cache.sessions[0], state="stopped")]
    assert [row["withdrawn_at"] is None for row in recommend(world, ROOMY)] == [False, False]


def test_the_step_withdraws_once_the_resumed_session_ends_a_turn_without_the_limit(
    world: World, work_signed_in: Account, limit_stopped: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    item(world, "high")
    [step] = recommend(world, ROOMY)
    message(world, NOW - timedelta(minutes=1), None)
    world.cache.sessions = [replace(world.cache.sessions[0], state="working")]
    monkeypatch.setattr(limits, "limit_stop", {}.get)
    [after] = recommend(world, ROOMY)
    assert after["id"] == step["id"] and after["withdrawn_at"] is not None


def test_the_step_withdraws_once_the_limit_of_the_resumed_session_resets(
    world: World, work_signed_in: Account, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(limits, "limit_stop", {SESSION_A: LimitStop(STOP, NOW - timedelta(minutes=1))}.get)
    world.cache.sessions = [make_session(SESSION_A, pid=None, cwd=str(tmp_path))]
    item(world, "high")
    [step] = recommend(world, ROOMY)
    message(world, NOW - timedelta(minutes=1), None)
    world.cache.sessions = [replace(world.cache.sessions[0], state="working")]
    [after] = recommend(world, ROOMY)
    assert after["id"] == step["id"] and after["withdrawn_at"] is not None


def test_a_session_that_asks_on_a_high_item_without_being_listed_gets_a_move_step(
    world: World, work_signed_in: Account, limit_stopped: None
) -> None:
    conn = db.connect(world.db_path)
    try:
        item_id = store.upsert_item(conn, {"key": "export-check", "title": "export check", "priority": "high"}, "sweep")["id"]
        store.upsert_step(conn, {"item_id": item_id, "need": "decide", "ask": "Which run?", "session_id": SESSION_A}, "exports")
    finally:
        conn.close()
    [step] = [step for step in recommend(world, ROOMY) if step["need"].startswith("move:")]
    assert step["item_id"] == item_id


def test_a_later_poll_leaves_the_open_step_as_it_is(
    world: World, work_signed_in: Account, limit_stopped: None
) -> None:
    item(world, "high")
    [step] = recommend(world, ROOMY)
    [again] = recommend(world, ROOMY)
    assert again == step
    conn = db.connect(world.db_path)
    try:
        kinds = [row[0] for row in conn.execute("SELECT kind FROM events WHERE kind LIKE 'step.%'")]
    finally:
        conn.close()
    assert kinds.count("step.updated") == 0


def test_a_new_stop_after_a_resume_keeps_the_open_step(
    world: World, work_signed_in: Account, limit_stopped: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reuse the open move step after a resumed session stops at its usage limit again."""
    item(world, "high")
    [step] = recommend(world, ROOMY)
    later = LimitStop((NOW - timedelta(minutes=25)).strftime("%Y-%m-%dT%H:%M:%S.000Z"), NOW + timedelta(hours=5))
    monkeypatch.setattr(limits, "limit_stop", {SESSION_A: later}.get)
    [kept] = recommend(world, ROOMY)
    assert (kept["id"], kept["need"]) == (step["id"], step["need"])
    assert f"until {NOW + timedelta(hours=5):%a %H:%M} UTC" in kept["why"]
    # A second open step for the same move, as the bug left one, gives way to the first.
    duplicate = f"move:{SESSION_A}:work:20260115T110000"
    conn = db.connect(world.db_path)
    try:
        store.upsert_step(conn, {"item_id": step["item_id"], "need": duplicate, "ask": "Move?"}, "cockpit")
    finally:
        conn.close()
    assert [row["withdrawn_at"] is None for row in recommend(world, ROOMY)] == [True, False]
    conn = db.connect(world.db_path)
    try:
        store.step_action(conn, step["id"], "dismiss", {}, "user")
    finally:
        conn.close()
    # Dismissed for its stop; the session's next stop gets a step again.
    [_, _, again] = recommend(world, ROOMY)
    assert again["need"] not in (step["need"], duplicate) and again["withdrawn_at"] is None


def test_the_step_follows_the_session_to_its_other_item_once_the_first_is_archived(
    world: World, work_signed_in: Account, limit_stopped: None
) -> None:
    first = item(world, "urgent")
    conn = db.connect(world.db_path)
    try:
        fields = {"key": "export-report", "title": "export report", "priority": "high", "sessions": [SESSION_A]}
        second = store.upsert_item(conn, fields, "sweep")["id"]
    finally:
        conn.close()
    [step] = recommend(world, ROOMY)
    assert step["item_id"] == first
    conn = db.connect(world.db_path)
    try:
        conn.execute("UPDATE items SET archived = 1 WHERE id = ?", (first,))
        conn.commit()
    finally:
        conn.close()
    steps = recommend(world, ROOMY)
    assert [(s["item_id"], s["withdrawn_at"] is None) for s in steps] == [(first, False), (second, True)]


def test_a_normal_item_gets_no_move_step(world: World, work_signed_in: Account, limit_stopped: None) -> None:
    item(world, "normal")
    assert recommend(world, ROOMY) == []


def test_no_step_while_the_other_account_has_little_room(
    world: World, work_signed_in: Account, limit_stopped: None
) -> None:
    item(world, "urgent")
    tight = ROOMY | {"work": record(NOW, (10, 2), (85, 60), NOW.timestamp())}
    assert recommend(world, tight) == []


def test_no_step_while_the_other_account_s_5_hour_window_is_way_over(
    world: World, work_signed_in: Account, limit_stopped: None
) -> None:
    """Under the 80 % cap, but 78 % used with 15 % of the window gone fills it within minutes."""
    item(world, "urgent")
    tight = ROOMY | {"work": record(NOW, (78, 4.25), (32, 60), NOW.timestamp())}
    assert recommend(world, tight) == []


def test_the_step_withdraws_once_the_other_account_runs_low(
    world: World, work_signed_in: Account, limit_stopped: None
) -> None:
    item(world, "urgent")
    [step] = recommend(world, ROOMY)
    tight = ROOMY | {"work": record(NOW, (85, 2), (30, 60), NOW.timestamp())}
    [after] = recommend(world, tight)
    assert after["id"] == step["id"] and after["withdrawn_at"] is not None


def test_moving_closes_the_move_step(
    world: World, work_signed_in: Account, limit_stopped: None
) -> None:
    item(world, "high")
    recommend(world, ROOMY)
    assert move(world, {"account": "work"}).status_code == 200
    conn = db.connect(world.db_path)
    try:
        [step] = db.rows(conn.execute("SELECT * FROM steps WHERE need LIKE 'move:%'").fetchall())
    finally:
        conn.close()
    assert step["status"] == "done"


def test_no_new_session_starts_while_every_account_is_signed_out(claude_accounts: tuple[Account, Account]) -> None:
    personal, work = claude_accounts
    signed_out(personal, NOW + timedelta(days=20))
    signed_out(work, NOW + timedelta(days=25))
    with pytest.raises(accounts.SignedOut, match="every Claude account is signed out"):
        server.pick_account(codex_agent.ProviderSessions(None, None))


def test_a_signed_out_account_says_new_sessions_start_nowhere_when_the_other_has_no_sign_in(
    claude_accounts: tuple[Account, Account],
) -> None:
    personal, _ = claude_accounts
    signed_out(personal, NOW + timedelta(days=20))
    [entry] = accounts.sign_ins(claude_accounts, NOW)
    assert "new sessions start nowhere until one is signed in" in entry["text"]
