"""A dialog in a session's terminal keeps its card after Cockpit's hook stopped waiting on it, and the user's answer
then restarts the session with that answer (SPEC PERM-8)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cockpit import db, delivery
from cockpit.permissions import ASK_WAIT_SECONDS
from cockpit.sessions import Session
from tests.conftest import SESSION_A, World
from tests.test_api import agent_client, at_dialog
from tests.test_permission_requests import PUSH, answer, report, state

GIT_STATUS = {"command": "git status", "description": "Show the worktree"}


def dialog_now(cwd: Path) -> Session:
    """Session A, live at a dialog in its terminal that came just now, as the hook reports it."""
    return replace(at_dialog(SESSION_A), cwd=str(cwd), updated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"))


def hook_gives_up(world: World, request: dict[str, Any]) -> None:
    response = agent_client(world).post(
        f"/api/permissions/requests/{request['id']}/settle", json={"state": "expired"}, headers=world.agent("permission-hook")
    )
    assert response.status_code == 200, response.text


def restarts_on_answer(world: World, session: Session) -> None:
    """`claude stop` ends the process; the resume brings it back, past the dialog."""

    def runner(account, args, cwd):
        stopped = args[1] == "stop"
        world.cache.sessions = [replace(session, pid=None) if stopped else replace(session, pid=99, tempo="active")]
        return world.runner(account, args, cwd)

    world.deliverer.run = runner
    world.runner.output = f"backgrounded · {SESSION_A[:8]}\n"


def test_a_dialog_keeps_its_card_after_the_hook_gave_up_while_the_session_waits_at_it(world: World, tmp_path: Path) -> None:
    world.cache.sessions = [dialog_now(tmp_path)]
    asked = report(world, "asked", PUSH)
    hook_gives_up(world, asked)
    shown = state(world)
    [card] = shown["permission_requests"]
    assert (card["id"], card["state"]) == (asked["id"], "expired")
    assert card["hook_ended_at"] is not None
    assert shown["waiting_sessions"] == []
    # Answered in the terminal: the turn goes on, and the card goes.
    world.cache.sessions = [replace(dialog_now(tmp_path), tempo="active")]
    assert state(world)["permission_requests"] == []
    # At a later dialog that never reached Cockpit, the session waits with Restart, and the old card stays gone.
    later = replace(dialog_now(tmp_path), updated_at="2099-01-01T00:00:00Z")
    world.cache.sessions = [later]
    shown = state(world)
    assert shown["permission_requests"] == []
    assert [session["session_id"] for session in shown["waiting_sessions"]] == [SESSION_A]


def test_a_dialog_keeps_its_explained_card_when_the_job_file_changed_as_the_hook_gave_up(world: World, tmp_path: Path) -> None:
    # The job file changed when the hook gave up, 25 minutes after its report, so the explained card once gave way to
    # one headed with the raw command.
    session = dialog_now(tmp_path)
    world.cache.sessions = [session]
    asked = report(world, "asked", PUSH)
    hook_gives_up(world, asked)
    now = datetime.now(UTC)
    conn = db.connect(world.db_path)
    with conn:
        conn.execute(
            "UPDATE permission_requests SET created_at = ? WHERE id = ?",
            ((now - timedelta(seconds=ASK_WAIT_SECONDS + 12)).strftime("%Y-%m-%dT%H:%M:%SZ"), asked["id"]),
        )
    conn.close()
    gave_up = now - timedelta(seconds=10)
    later = replace(session, updated_at=gave_up.isoformat().replace("+00:00", "Z"))
    world.cache.sessions = [later]
    shown = state(world)
    assert [card["id"] for card in shown["permission_requests"]] == [asked["id"]]
    assert shown["waiting_sessions"] == []
    # A dialog that came well after the give-up is another one.
    world.cache.sessions = [replace(session, updated_at=(gave_up + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"))]
    assert state(world)["permission_requests"] == []
    world.cache.sessions = [later]
    # Answered in the terminal, with the session's next dialog just after: that one's report takes over the card.
    newer = report(world, "asked", GIT_STATUS)
    assert [card["id"] for card in state(world)["permission_requests"]] == [newer["id"]]
    assert answer(world, asked["id"], "allow", {"rule": None, "list": None}).status_code == 409
    conn = db.connect(world.db_path)
    with conn:
        conn.execute("DELETE FROM permission_requests WHERE id = ?", (newer["id"],))
    conn.close()
    # Its answer restarts the session, and no Restart card shows while the listing catches up.
    restarts_on_answer(world, later)
    response = answer(world, asked["id"], "allow", {"rule": None, "list": None})
    assert response.status_code == 200, response.text
    assert [args[:2] for args, _ in world.runner.calls][:1] == [["claude", "stop"]]
    world.cache.sessions = [later]
    assert state(world)["waiting_sessions"] == []


def test_while_the_hook_waits_an_answer_goes_to_the_hook(world: World, tmp_path: Path) -> None:
    world.cache.sessions = [dialog_now(tmp_path)]
    asked = report(world, "asked", PUSH)
    assert state(world)["permission_requests"][0]["hook_ended_at"] is None
    response = answer(world, asked["id"], "allow", {"rule": None, "list": None})
    assert response.status_code == 200, response.text
    assert (response.json()["state"], response.json()["message_id"]) == ("allowed", None)
    assert world.runner.calls == []


def test_a_dialog_user_just_answered_offers_no_restart_while_the_session_listing_still_shows_it(world: World, tmp_path: Path) -> None:
    world.cache.sessions = [dialog_now(tmp_path)]
    asked = report(world, "asked", PUSH)
    assert answer(world, asked["id"], "allow", {"rule": None, "list": None}).status_code == 200
    shown = state(world)
    assert (shown["permission_requests"], shown["waiting_sessions"]) == ([], [])
    # A later dialog that never reached Cockpit still gets its Restart card.
    world.cache.sessions = [replace(dialog_now(tmp_path), updated_at="2099-01-01T00:00:00Z")]
    assert [session["session_id"] for session in state(world)["waiting_sessions"]] == [SESSION_A]


def test_allowing_a_dialog_the_hook_gave_up_on_restarts_the_session_and_allows_its_call_once(world: World, tmp_path: Path) -> None:
    session = dialog_now(tmp_path)
    world.cache.sessions = [session]
    asked = report(world, "asked", PUSH)
    hook_gives_up(world, asked)
    restarts_on_answer(world, session)
    response = answer(world, asked["id"], "allow", {"rule": None, "list": None})
    assert response.status_code == 200, response.text
    allowed = response.json()
    assert allowed["state"] == "allowed" and allowed["message_id"]
    assert [args[:3] for args, _ in world.runner.calls] == [["claude", "stop", SESSION_A[:8]], ["claude", "--bg", "--resume"]]
    text = world.runner.calls[1][0][-1]
    assert text.startswith("Message from Cockpit itself")
    assert (
        f"Sam allowed the call that waited at a dialog in your terminal since {asked['created_at'][11:16]} UTC, from"
        " Cockpit:\n\ngit push origin user/x\n\nCockpit had stopped waiting on that dialog, so it could answer it only by"
        " restarting this session. Make the call again, unchanged: Cockpit allows its dialog at once."
    ) in text
    sent = world.client.get(f"/api/messages/{allowed['message_id']}").json()
    assert (sent["author"], sent["state"], sent["via"]) == ("cockpit", "sent", "resume")
    # The call made again is allowed as it is reported, once; another call still asks.
    assert report(world, "asked", GIT_STATUS)["state"] == "open"
    assert report(world, "asked", PUSH)["state"] == "allowed"
    assert report(world, "asked", PUSH)["state"] == "open"


def test_denying_a_dialog_the_hook_gave_up_on_restarts_the_session_with_the_denial(world: World, tmp_path: Path) -> None:
    session = dialog_now(tmp_path)
    world.cache.sessions = [session]
    asked = report(world, "asked", PUSH)
    hook_gives_up(world, asked)
    restarts_on_answer(world, session)
    response = answer(world, asked["id"], "deny")
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "denied" and response.json()["message_id"]
    text = world.runner.calls[1][0][-1]
    assert "Sam denied the call that waited at a dialog in your terminal" in text
    assert "Leave that call: go on without it, or say in your reply what you need it for." in text
    assert report(world, "asked", PUSH)["state"] == "open"


def test_an_answer_to_a_dialog_the_session_left_is_refused(world: World, tmp_path: Path) -> None:
    world.cache.sessions = [dialog_now(tmp_path)]
    asked = report(world, "asked", PUSH)
    hook_gives_up(world, asked)
    world.cache.sessions = [replace(dialog_now(tmp_path), tempo="active")]
    response = answer(world, asked["id"], "allow", {"rule": None, "list": None})
    assert response.status_code == 409
    assert "no longer waits at this dialog" in response.json()["detail"]
    assert answer(world, asked["id"], "deny").status_code == 409
    assert world.runner.calls == []


def test_a_restart_that_fails_leaves_the_answer_recorded_and_says_why(world: World, tmp_path: Path, monkeypatch) -> None:
    world.cache.sessions = [dialog_now(tmp_path)]
    asked = report(world, "asked", PUSH)
    hook_gives_up(world, asked)
    monkeypatch.setattr(delivery, "STOP_SETTLE_SECONDS", 0)
    response = answer(world, asked["id"], "allow", {"rule": None, "list": None})
    assert response.status_code == 200, response.text
    failed = world.client.get(f"/api/messages/{response.json()['message_id']}").json()
    assert failed["state"] == "failed" and "was still running" in failed["detail"]


def test_the_call_made_again_from_another_folder_still_asks(world: World, tmp_path: Path) -> None:
    session = dialog_now(tmp_path)
    world.cache.sessions = [session]
    asked = report(world, "asked", PUSH, cwd="/home/sam/dev/app/worktrees/staging")
    hook_gives_up(world, asked)
    restarts_on_answer(world, session)
    response = answer(world, asked["id"], "allow", {"rule": None, "list": None})
    assert response.status_code == 200, response.text
    # The same words in another folder are another call: the user's allow does not cover it.
    assert report(world, "asked", PUSH, cwd="/home/sam/dev/app/worktrees/prod")["state"] == "open"
    assert report(world, "asked", PUSH, cwd="/home/sam/dev/app/worktrees/staging")["state"] == "allowed"
