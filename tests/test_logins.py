from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cockpit import accounts, config, db, logins, store
from tests.conftest import FAKE_CLAUDE, SESSION_A, SESSION_B, World, make_session
from tests.fake_claude import GOOD_CODE, LINK
from tests.test_session_archive import _step

NOW = datetime.now(UTC).replace(microsecond=0)
STOPPED = NOW - timedelta(hours=2)


def sign_in(account: config.Account, at: datetime, emptied: bool = False) -> None:
    """A sign-in file written at `at`; an emptied one is what Claude Code leaves when it fails to renew access."""
    account.folder.mkdir(parents=True, exist_ok=True)
    token = "" if emptied else "token"
    expires = int((NOW + timedelta(days=20)).timestamp() * 1000)
    path = account.folder / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": token, "refreshToken": token, "refreshTokenExpiresAt": expires}}))
    os.utime(path, (at.timestamp(), at.timestamp()))


def transcript(folder: Path, session_id: str, *entries: dict[str, Any]) -> None:
    (folder / "-home-x").mkdir(parents=True, exist_ok=True)
    (folder / "-home-x" / f"{session_id}.jsonl").write_text("".join(json.dumps(entry) + "\n" for entry in entries))


def login_expired(at: datetime) -> dict[str, Any]:
    return {
        "type": "assistant", "timestamp": at.strftime("%Y-%m-%dT%H:%M:%S.500Z"), "error": "authentication_failed",
        "isApiErrorMessage": True,
        "message": {"role": "assistant", "content": [{"type": "text", "text": "Login expired · Please run /login"}]},
    }


def answer(at: datetime) -> dict[str, Any]:
    return {"type": "assistant", "timestamp": at.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "message": {"content": []}}


def sync(world: World, sweeper: str | None = None) -> tuple[list[int], logins.Lost | None]:
    conn = db.connect(world.db_path)
    try:
        return logins.sync(conn, world.cache.sessions, sweeper, NOW, world.checkout.parent)
    finally:
        conn.close()


def messages(world: World) -> list[dict[str, Any]]:
    conn = db.connect(world.db_path)
    try:
        return db.rows(conn.execute("SELECT * FROM messages ORDER BY id").fetchall())
    finally:
        conn.close()


# The turns a sign-out cost


def test_once_the_account_is_signed_in_again_each_session_it_stopped_continues_once(
    world: World, transcripts: Path, claude_accounts: tuple[config.Account, config.Account],
) -> None:
    _, work = claude_accounts
    world.cache.sessions = [
        make_session(SESSION_A, pid=None, state="stopped", account="work"),
        make_session(SESSION_B, pid=101, state="blocked", account="work"),
    ]
    transcript(transcripts, SESSION_A, answer(STOPPED - timedelta(minutes=5)), login_expired(STOPPED))
    transcript(transcripts, SESSION_B, login_expired(STOPPED + timedelta(minutes=10)), {"type": "system"})
    # Still signed out: nothing goes.
    sign_in(work, STOPPED - timedelta(hours=1), emptied=True)
    assert sync(world) == ([], None)
    # Signed in again after both stops.
    sign_in(work, NOW - timedelta(minutes=1))
    sent, swept = sync(world)
    assert len(sent) == 2 and swept is None
    told = messages(world)
    assert [message["session_id"] for message in told] == [SESSION_A, SESSION_B]
    assert {message["author"] for message in told} == {"cockpit"}
    assert told[0]["text"].startswith(
        f"The work Claude account works again: it was signed in again at {NOW - timedelta(minutes=1):%H:%M} UTC."
    )
    # Each stop is answered once, also when Cockpit's message did not go out.
    conn = db.connect(world.db_path)
    try:
        conn.execute("UPDATE messages SET state = 'failed'")
        conn.commit()
    finally:
        conn.close()
    assert sync(world) == ([], None)


def test_a_message_from_someone_else_answers_the_stop_once_it_went_out_or_waits_to(
    world: World, transcripts: Path, claude_accounts: tuple[config.Account, config.Account],
) -> None:
    _, work = claude_accounts
    world.cache.sessions = [make_session(SESSION_A, pid=None, state="stopped", account="work")]
    transcript(transcripts, SESSION_A, login_expired(STOPPED))
    sign_in(work, NOW - timedelta(minutes=1))
    conn = db.connect(world.db_path)
    try:
        after = (STOPPED + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "INSERT INTO messages (author, session_id, text, state, created_at) VALUES ('user', ?, 'hi', 'failed', ?)",
            (SESSION_A, after),
        )
        conn.commit()
        # A message that failed to go out reached nobody.
        sent, _ = sync(world)
        assert len(sent) == 1
        conn.execute("DELETE FROM messages")
        conn.execute(
            "INSERT INTO messages (author, session_id, text, state, created_at) VALUES ('user', ?, 'hi', 'queued', ?)",
            (SESSION_A, db.now_iso()),
        )
        conn.commit()
    finally:
        conn.close()
    # One waiting to go will reach it.
    assert sync(world) == ([], None)


def test_a_session_whose_stop_came_after_the_last_sign_in_waits_for_the_next_one(
    world: World, transcripts: Path, claude_accounts: tuple[config.Account, config.Account],
) -> None:
    _, work = claude_accounts
    world.cache.sessions = [make_session(SESSION_A, pid=None, state="stopped", account="work")]
    sign_in(work, STOPPED - timedelta(minutes=1))
    transcript(transcripts, SESSION_A, login_expired(STOPPED))
    assert sync(world) == ([], None)


def test_sessions_that_did_not_lose_a_turn_or_are_working_or_archived_are_left_alone(
    world: World, transcripts: Path, claude_accounts: tuple[config.Account, config.Account],
) -> None:
    personal, _ = claude_accounts
    sign_in(personal, NOW - timedelta(minutes=1))
    world.cache.sessions = [
        make_session(SESSION_A, pid=101, state="working"),
        make_session(SESSION_B, pid=None, state="stopped"),
    ]
    transcript(transcripts, SESSION_A, login_expired(STOPPED))
    # The last assistant entry is an answer after the stop.
    transcript(transcripts, SESSION_B, login_expired(STOPPED), answer(STOPPED + timedelta(minutes=1)))
    assert sync(world) == ([], None)
    transcript(transcripts, SESSION_B, login_expired(STOPPED))
    step_id = _step(world, "review", SESSION_B)
    conn = db.connect(world.db_path)
    try:
        conn.execute(
            "INSERT INTO session_archives (session_id, step_id, message_id, asked_at, stopped_at) VALUES (?, ?, NULL, ?, ?)",
            (SESSION_B, step_id, db.now_iso(), db.now_iso()),
        )
        conn.commit()
    finally:
        conn.close()
    assert sync(world) == ([], None)
    # A message brought it back since: it counts again.
    conn = db.connect(world.db_path)
    try:
        conn.execute("UPDATE session_archives SET revived_at = ?", (db.now_iso(),))
        conn.commit()
    finally:
        conn.close()
    sent, _ = sync(world)
    assert len(sent) == 1


def test_a_session_an_agent_closed_after_its_stop_stays_closed(
    world: World, transcripts: Path, claude_accounts: tuple[config.Account, config.Account],
) -> None:
    personal, _ = claude_accounts
    sign_in(personal, NOW - timedelta(minutes=1))
    world.cache.sessions = [make_session(SESSION_B, pid=None, state="stopped")]
    transcript(transcripts, SESSION_B, login_expired(STOPPED))
    conn = db.connect(world.db_path)
    try:
        # The sweep closed it after the stop, its work being finished.
        conn.execute(
            "INSERT INTO events (at, kind, actor, data) VALUES (?, 'session.closed', 'agent:sweep', ?)",
            ((STOPPED + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"), json.dumps({"session_id": SESSION_B})),
        )
        conn.commit()
    finally:
        conn.close()
    assert sync(world) == ([], None)


def test_the_sweeper_gets_a_full_sweep_in_place_of_the_wake_up_it_lost(
    world: World, transcripts: Path, claude_accounts: tuple[config.Account, config.Account],
) -> None:
    _, work = claude_accounts
    world.cache.sessions = [make_session(SESSION_A, pid=101, state="blocked", account="work")]
    transcript(transcripts, SESSION_A, login_expired(STOPPED))
    sign_in(work, NOW - timedelta(minutes=1))
    sent, swept = sync(world, sweeper=SESSION_A)
    assert sent == [] and swept is not None and swept.session.session_id == SESSION_A
    text = logins.sweep_text(swept)
    assert text.startswith("Cockpit wake-up: the work Claude account was signed out")
    assert "run a full sweep" in text


# Signing in from the page


def test_a_sign_in_gives_claude_s_link_and_the_code_signs_the_account_in(
    claude_accounts: tuple[config.Account, config.Account],
) -> None:
    _, work = claude_accounts
    sign_in(work, STOPPED, emptied=True)
    assert accounts.signed_out(work, NOW)
    sign_ins = logins.SignIn([sys.executable, str(FAKE_CLAUDE), str(claude_accounts[0].folder)])
    started = sign_ins.start(work)
    assert started["url"] == LINK
    assert sign_ins.waiting() == ["work"]
    sign_ins.finish(work, started["attempt"], GOOD_CODE)
    assert not accounts.signed_out(work, datetime.now(UTC))
    assert sign_ins.waiting() == []


def test_a_wrong_code_says_what_claude_said_and_the_next_try_starts_over(
    claude_accounts: tuple[config.Account, config.Account],
) -> None:
    personal, _ = claude_accounts
    sign_ins = logins.SignIn([sys.executable, str(FAKE_CLAUDE), str(personal.folder)])
    attempt = sign_ins.start(personal)["attempt"]
    with pytest.raises(store.Invalid, match="Request failed with status code 400"):
        sign_ins.finish(personal, attempt, "wrong-code")
    with pytest.raises(store.Invalid, match="no sign-in of the personal account waits for this code"):
        sign_ins.finish(personal, attempt, GOOD_CODE)
    assert not (personal.folder / ".credentials.json").exists()


def test_a_code_goes_only_to_the_sign_in_whose_link_the_user_opened(
    claude_accounts: tuple[config.Account, config.Account],
) -> None:
    personal, _ = claude_accounts
    sign_ins = logins.SignIn([sys.executable, str(FAKE_CLAUDE), str(personal.folder)])
    first = sign_ins.start(personal)["attempt"]
    second = sign_ins.start(personal)["attempt"]
    with pytest.raises(store.Invalid, match="waits for this code"):
        sign_ins.finish(personal, first, GOOD_CODE)
    # The newer sign-in still waits, and takes its own code.
    assert sign_ins.waiting() == ["personal"]
    sign_ins.finish(personal, second, GOOD_CODE)
    assert (personal.folder / ".credentials.json").exists()


def test_the_link_counts_only_once_it_is_printed_in_full() -> None:
    printed = f"visit: \x1b]8;;{LINK}\x07\x1b[94m{LINK}\x1b[39m\x1b]8;;\x07\r\nPaste code here if prompted > "
    for cut in range(len(printed)):
        found = logins.LINK.search(logins.ESCAPES.sub("", printed[:cut]))
        assert found is None or found[0] == LINK
    assert logins.LINK.search(logins.ESCAPES.sub("", printed))[0] == LINK


def test_user_signs_an_account_in_from_the_page(
    world: World, claude_accounts: tuple[config.Account, config.Account],
) -> None:
    _, work = claude_accounts
    sign_in(work, STOPPED, emptied=True)
    [entry] = world.client.get("/api/state").json()["claude_sign_ins"]
    assert entry["account"] == "work" and entry["signed_out"] and not entry["waiting"]
    # Agents cannot sign an account in.
    assert world.client.post("/api/claude/work/sign-in", headers=world.agent()).status_code == 403
    started = world.client.post("/api/claude/work/sign-in", headers=world.user())
    assert started.status_code == 200 and started.json()["url"] == LINK
    assert world.client.get("/api/state").json()["claude_sign_ins"][0]["waiting"]
    code = {"code": GOOD_CODE, "attempt": started.json()["attempt"]}
    finished = world.client.post("/api/claude/work/code", json=code, headers=world.user())
    assert finished.status_code == 200 and finished.json() == {"account": "work", "signed_out": False}
    assert world.client.get("/api/state").json()["claude_sign_ins"] == []
    assert world.client.post("/api/claude/nobody/sign-in", headers=world.user()).status_code == 404
