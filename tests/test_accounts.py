"""Two Claude accounts at once (SPEC SESSION-17): listing both, running each session on its own account, picking
the account for new work, waking limit stops per account, trust per account and the shared links."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cockpit import accounts, config, db, delivery, limits, removals, resets, sessions, usage
from cockpit.config import MOUNTED_ENTRIES, SHARED_ENTRIES, Account
from cockpit.sessions import LimitStop
from tests.conftest import ORIGIN, SESSION_A, SESSION_B, World, make_session
from tests.test_api import at_dialog
from tests.test_delivery import effort_claude, send

NOW = datetime(2026, 1, 19, 9, 0, tzinfo=UTC)
HOUR = 3600.0
DAY = 24 * HOUR


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_missing_secondary_account_folder_is_unused(claude_accounts, monkeypatch, platform):
    personal, work = claude_accounts
    monkeypatch.setattr(sys, "platform", platform)
    assert not work.folder.exists()
    assert not accounts.signed_in(work)
    assert accounts.signed_in(personal) == (platform == "darwin")
    assert accounts.in_use(claude_accounts, []) == [personal]
    work.folder.mkdir(parents=True)
    assert accounts.signed_in(work) == (platform == "darwin")


def sign_in(account: Account, email: str) -> None:
    """What `/login` leaves in an account's folder: a sign-in and a state file naming the address."""
    account.folder.mkdir(parents=True, exist_ok=True)
    (account.folder / ".credentials.json").write_text("{}")
    account.state.parent.mkdir(parents=True, exist_ok=True)
    account.state.write_text(json.dumps({"oauthAccount": {"emailAddress": email}, "projects": {}}))


def link_shared(personal: Account, work: Account) -> None:
    """What scripts/second-account.sh sets up. A test cannot bind-mount, so for a mounted entry the link runs the other
    way: the work entry is a real folder and the personal one links to it, which gives the same folder at both paths."""
    work.folder.mkdir(parents=True, exist_ok=True)
    personal.folder.mkdir(parents=True, exist_ok=True)
    for name in SHARED_ENTRIES:
        if name in MOUNTED_ENTRIES:
            (work.folder / name).mkdir()
            (personal.folder / name).symlink_to(work.folder / name)
        else:
            (work.folder / name).symlink_to(personal.folder / name)


def record(now: datetime, five: tuple[float, float] | None, week: tuple[float, float] | None, captured: float) -> dict[str, Any]:
    """A usage record: each window as (used %, hours from `now` until it resets)."""
    windows = {}
    for key, window in (("five_hour", five), ("seven_day", week)):
        if window is not None:
            windows[key] = {"used_percentage": window[0], "resets_at": now.timestamp() + window[1] * HOUR}
    return {"account": {}, "rate_limits": windows, "captured_at": captured}


# Listing


def fake_agents(monkeypatch: pytest.MonkeyPatch, rows: dict[str, list[dict[str, Any]]]) -> list[list[str]]:
    """`claude agents` answers with the rows of the folder its CLAUDE_CONFIG_DIR names ("" for the default)."""
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        folder = next((word.split("=", 1)[1] for word in command if word.startswith("CLAUDE_CONFIG_DIR=")), "")
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(rows[folder]), stderr="")

    monkeypatch.setattr(sessions.subprocess, "run", run)
    return commands


def row(session_id: str) -> dict[str, Any]:
    return {"id": session_id[:8], "sessionId": session_id, "state": "done", "cwd": "/"}


def test_sessions_of_both_folders_are_listed_each_with_its_account(
    claude_accounts: tuple[Account, Account], monkeypatch: pytest.MonkeyPatch
) -> None:
    personal, work = claude_accounts
    sign_in(work, "sam@work.test")
    commands = fake_agents(monkeypatch, {"": [row(SESSION_A)], str(work.folder): [row(SESSION_B)]})
    listed = sessions.list_sessions((personal, work), {}, [])[0]
    assert [(session.session_id, session.account) for session in listed] == [(SESSION_A, "personal"), (SESSION_B, "work")]
    assert ["-u", "CLAUDE_CONFIG_DIR"] == commands[0][commands[0].index("env") + 1:][:2]


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_accounts_without_credentials_files_can_use_the_macos_keychain(claude_accounts, monkeypatch, platform):
    personal, work = claude_accounts
    for account in claude_accounts:
        account.folder.mkdir(parents=True)
    monkeypatch.setattr(sys, "platform", platform)
    fake_agents(monkeypatch, {"": [row(SESSION_A)], str(work.folder): [row(SESSION_B)]})
    warnings = []
    listed = sessions.list_sessions(claude_accounts, {}, warnings)[0]
    assert [session.account for session in listed] == (["personal", "work"] if platform == "darwin" else ["personal"])
    assert accounts.signed_in(personal) == (platform == "darwin")
    assert not accounts.signed_out(personal, NOW)
    assert accounts.usable(accounts.in_use(claude_accounts, []), NOW) == (
        [personal, work] if platform == "darwin" else [personal]
    )
    assert bool(warnings) == (platform == "linux")


@pytest.mark.parametrize("oauth", [{"refreshToken": ""}, {"refreshTokenExpiresAt": 1}])
def test_macos_still_respects_an_explicitly_signed_out_file(claude_accounts, monkeypatch, oauth):
    personal, _ = claude_accounts
    sign_in(personal, "sam@example.test")
    (personal.folder / ".credentials.json").write_text(json.dumps({"claudeAiOauth": oauth}))
    monkeypatch.setattr(sys, "platform", "darwin")
    assert accounts.signed_out(personal, NOW)
    assert accounts.usable([personal], NOW) == []


def test_without_a_signed_in_work_folder_cockpit_runs_on_personal_alone(
    claude_accounts: tuple[Account, Account], monkeypatch: pytest.MonkeyPatch
) -> None:
    personal, work = claude_accounts
    fake_agents(monkeypatch, {"": [row(SESSION_A)]})
    warnings: list[str] = []
    assert [session.account for session in sessions.list_sessions((personal, work), {}, warnings)[0]] == ["personal"]
    assert warnings == []  # no folder yet: the normal state before the setup
    work.folder.mkdir(parents=True)
    assert [session.account for session in sessions.list_sessions((personal, work), {}, warnings)[0]] == ["personal"]
    assert len(warnings) == 1 and "has no sign-in" in warnings[0]


def test_an_account_signed_out_since_the_last_listing_lists_no_sessions(
    claude_accounts: tuple[Account, Account], monkeypatch: pytest.MonkeyPatch
) -> None:
    personal, work = claude_accounts
    sign_in(work, "sam@work.test")
    fake_agents(monkeypatch, {"": [row(SESSION_A)], str(work.folder): [row(SESSION_B)]})
    monkeypatch.setattr(sessions, "CACHE_SECONDS", 0)
    cache = sessions.SessionCache((personal, work), dict, list)
    assert {session.session_id for session in cache.snapshot(wait=True).sessions} == {SESSION_A, SESSION_B}
    (work.folder / ".credentials.json").unlink()
    cache.invalidate()
    assert [session.session_id for session in cache.snapshot(wait=True).sessions] == [SESSION_A]


# Each session runs on its own account


def on_work(session: Any) -> Any:
    return replace(session, account="work")


def test_a_message_resumes_a_work_session_on_the_work_account(world: World, tmp_path: Path, monkeypatch) -> None:
    world.cache.sessions = [on_work(make_session(SESSION_A, pid=None, cwd=str(tmp_path)))]
    monkeypatch.setattr(delivery, "RESUME_SETTLE_SECONDS", 0.05)
    monkeypatch.setattr(delivery, "RESUME_POLL_SECONDS", 0.01)
    send(world, {"session_id": SESSION_A})
    assert [args[:3] for args, _ in world.runner.calls] == [["claude", "--bg", "--resume"]]
    assert world.runner.accounts == ["work"]


def test_a_restart_stops_and_resumes_on_the_session_s_account(world: World, tmp_path: Path) -> None:
    held = on_work(replace(at_dialog(SESSION_A), cwd=str(tmp_path)))
    world.cache.sessions = [held]

    def runner(account: Account, args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        stopped = args[1] == "stop"
        world.cache.sessions = [replace(held, pid=None) if stopped else replace(held, pid=99, tempo="active")]
        return world.runner(account, args, cwd)

    world.deliverer.run = runner
    world.runner.output = f"backgrounded · {SESSION_A[:8]}\n"
    assert world.client.post(f"/api/sessions/{SESSION_A}/restart", headers=world.user()).status_code == 200
    assert [args[1] for args, _ in world.runner.calls] == ["stop", "--bg"]
    assert world.runner.accounts == ["work", "work"]


def test_an_effort_change_runs_on_the_session_s_account(world: World, tmp_path: Path, monkeypatch) -> None:
    effort_claude(world, on_work(replace(make_session(SESSION_A, pid=None, cwd=str(tmp_path)), effort="xhigh")), monkeypatch)
    send(world, {"session_id": SESSION_A})
    assert world.runner.calls[0][0][-1] == "/effort high"
    assert set(world.runner.accounts) == {"work"}


def test_a_new_session_starts_on_the_picked_account_and_is_found_in_its_list(
    world: World, claude_accounts: tuple[Account, Account]
) -> None:
    looked_up: list[str] = []
    world.deliverer.pick = lambda: (claude_accounts[1], "more room per hour")
    world.deliverer.short_id_to_uuid = lambda account, short_id: looked_up.append(account.name) or SESSION_B
    agent = TestClient(world.client.app, base_url=ORIGIN)
    item = agent.put("/api/items", json={"key": "note:a", "title": "Fix the thing"}, headers=world.agent()).json()
    sent = send(world, {"item_id": item["id"], "new_cwd": str(world.checkout)})
    assert sent["session_id"] == SESSION_B
    assert world.runner.accounts == ["work"] and looked_up == ["work"]
    assert sent["detail"].endswith("on the work account (more room per hour), claude-opus-5-5 at high effort")


def test_a_new_session_starts_on_the_account_the_user_picked(
    world: World, claude_accounts: tuple[Account, Account]
) -> None:
    # SPEC SESSION-22: the user's pick wins over the picker's, and a signed-out pick fails the message.
    personal, work = claude_accounts
    world.deliverer.pick = lambda: (personal, "more room per hour")
    world.deliverer.short_id_to_uuid = lambda account, short_id: SESSION_B
    sent = send(world, {"new_cwd": str(world.checkout), "account": "work"})
    assert sent["state"] == "failed"
    assert "the work Claude account you picked is signed out" in sent["detail"]
    assert world.runner.calls == []
    sign_in(work, "sam@work.test")
    sent = send(world, {"new_cwd": str(world.checkout), "account": "work"})
    assert sent["state"] == "sent" and world.runner.accounts == ["work"]
    assert sent["detail"].endswith("on the work account (you picked it), claude-opus-5-5 at high effort")


def test_trust_is_written_into_the_state_of_the_account_that_runs_the_session(
    world: World, claude_accounts: tuple[Account, Account], tmp_path: Path, monkeypatch
) -> None:
    personal, work = claude_accounts
    sign_in(personal, "sam@home.test")
    sign_in(work, "sam@work.test")
    checkouts = world.checkout.parent
    world.deliverer.trust = lambda account, cwd: delivery.trust_checkout(checkouts, account.state, cwd)
    world.cache.sessions = [on_work(make_session(SESSION_A, pid=None, cwd=str(world.checkout)))]
    monkeypatch.setattr(delivery, "RESUME_SETTLE_SECONDS", 0.05)
    monkeypatch.setattr(delivery, "RESUME_POLL_SECONDS", 0.01)
    send(world, {"session_id": SESSION_A})
    assert str(world.checkout) in json.loads(work.state.read_text())["projects"]
    assert json.loads(personal.state.read_text())["projects"] == {}


# The picker


def score(five: tuple[float, float] | None, week: tuple[float, float]) -> float:
    """pick_score of an account whose windows read as in `record`."""
    return accounts.usage.pick_score(accounts.usage.room(record(NOW, five, week, NOW.timestamp()), NOW.timestamp(), [], None, 0))


def test_the_account_whose_week_ends_first_gets_new_work(claude_accounts: tuple[Account, Account]) -> None:
    """an account with half the hours left and half the usage left should come
    before one with double both, as use comes unevenly; finish the account about to run out."""
    personal, work = claude_accounts
    found = {
        # 20 % left over 30 hours against 40 % over 60 hours: the same room per hour.
        "personal": record(NOW, (40, 3), (80, 30), NOW.timestamp()),
        "work": record(NOW, (10, 4), (60, 60), NOW.timestamp()),
    }
    assert pick(found, claude_accounts) == (personal, (
        "it scores 0.43 for new work: its week resets at 2026-01-20 15:00 UTC, in 30 h, with 20 % of it left, and its"
        " 5-hour window is on course for 88 %; the work account scores 0.29: its week resets at 2026-01-21 21:00 UTC,"
        " in 60 h, with 40 % of it left, and its 5-hour window is on course for 26 %"))
    # Even with far less room per hour: 10 % over 30 hours against 90 % over 60.
    found["work"] = record(NOW, (10, 4), (10, 60), NOW.timestamp())
    found["personal"] = record(NOW, (40, 3), (90, 30), NOW.timestamp())
    assert pick(found, claude_accounts)[0] == personal
    # Once personal's five-hour window is spent, it is skipped.
    found["personal"] = record(NOW, (100, 3), (90, 30), NOW.timestamp())
    assert pick(found, claude_accounts)[0] == work


def test_the_score_has_no_steps() -> None:
    """Small changes in use or time produce small score changes, while preserving room for a session."""
    for tenths in range(1000):
        for hours in (2, 6, 30):
            assert abs(score(None, ((tenths + 1) / 10, hours)) - score(None, (tenths / 10, hours))) < 0.13
        assert abs(score((((tenths + 1) / 10), 2.5), (50, 30)) - score((tenths / 10, 2.5), (50, 30))) < 0.05
    for tenths in range(10, 1680):
        assert abs(score(None, (50, (tenths + 1) / 10)) - score(None, (50, tenths / 10))) < 0.05


def test_the_score_falls_with_use_and_time_to_the_weekly_reset() -> None:
    # More used, later reset, a fuller 5-hour window: never a higher score.
    for week_used in range(100):
        assert score(None, (week_used + 1, 30)) <= score(None, (week_used, 30))
    for hours in range(1, 168):
        assert score(None, (50, hours + 1)) < score(None, (50, hours))
    for five_used in range(100):
        assert score((five_used + 1, 2.5), (50, 30)) <= score((five_used, 2.5), (50, 30))


def test_a_week_ending_much_sooner_counts_for_more() -> None:
    """The difference between weekly reset times influences which account takes new work."""
    soon = score(None, (50, 24))
    one_day, six_days = soon / score(None, (50, 48)), soon / score(None, (50, 168))
    assert 1.4 < one_day < 2 and six_days > 15
    # It takes a fuller 5-hour window to move work off a week ending six days sooner than off one a day sooner.
    def moves_at(other_hours: float) -> int:
        return next(used for used in range(101) if score((used, 2.5), (50, 24)) < score(None, (10, other_hours)))
    assert moves_at(48) < moves_at(168)


def test_a_nearly_used_week_still_gets_new_work(claude_accounts: tuple[Account, Account]) -> None:
    """Prefer a nearly used account when its weekly allowance resets soon and still has room for new work."""
    personal, work = claude_accounts
    found = {
        "personal": record(NOW, (1, 3), (91, 4), NOW.timestamp()),
        "work": record(NOW, (1, 3), (60, 36), NOW.timestamp()),
    }
    for used in (91, 96, 97):
        found["personal"] = record(NOW, (1, 3), (used, 4), NOW.timestamp())
        assert pick(found, claude_accounts)[0] == personal
    # A week a day away from its reset at 96 % still scores far above one six days away.
    assert score(None, (96, 24)) > 5 * score(None, (10, 150))
    # With too little left for a session, new work moves on and the sessions there use the rest.
    found["personal"] = record(NOW, (1, 3), (99, 4), NOW.timestamp())
    assert pick(found, claude_accounts)[0] == work


def test_the_sessions_running_on_an_account_count_against_its_week(claude_accounts: tuple[Account, Account]) -> None:
    """Count running sessions against an account's remaining weekly allowance."""
    personal, work = claude_accounts
    found = {
        "personal": record(NOW, (15, 1.5), (43, 144), NOW.timestamp()),
        "work": record(NOW, (28, 1.2), (98, 6), NOW.timestamp()),
    }
    assert accounts.pick([personal, work], found, NOW, None, {})[0] == work
    account, why = accounts.pick([personal, work], found, NOW, None, {"work": 6})
    assert account == personal
    assert why == (
        "it scores 0.0025 for new work: its week resets at 2026-01-25 09:00 UTC, in 144 h, with 57 % of it left, which at"
        " its rate so far runs out 112 h before then, and its 5-hour window is on course for 21 %; the work account"
        " scores 2.4e-20: its week resets at 2026-01-19 15:00 UTC,"
        " in 6 h, with 2 % of it left, -7.0 % after the 6 sessions running on it use 1.5 % each, and its 5-hour window"
        " is on course for 37 %")
    # Sessions on the other account leave this one's room alone, and one session already fills 2 %, even against a
    # week that at its first day's rate runs out 112 hours before its reset.
    assert accounts.pick([personal, work], found, NOW, None, {"personal": 6})[0] == work
    assert accounts.pick([personal, work], found, NOW, None, {"work": 1})[0] == personal
    # A week with room to spare keeps new work beside its running sessions: 20 % left less six sessions' 9 %.
    found["work"] = record(NOW, (28, 1.2), (80, 6), NOW.timestamp())
    assert accounts.pick([personal, work], found, NOW, None, {"work": 6})[0] == work


def test_a_week_on_course_to_run_out_long_before_its_reset_sends_new_sessions_elsewhere(
    claude_accounts: tuple[Account, Account],
) -> None:
    """Send new work to the account with room when the other is on course to exhaust its week early."""
    personal, work = claude_accounts
    found = {
        "personal": record(NOW, (2, 4), (40, 120), NOW.timestamp()),
        "work": record(NOW, (4, 4.5), (5, 156), NOW.timestamp()),
    }
    account, why = pick(found, claude_accounts)
    assert account == work
    assert "which at its rate so far runs out 48 h before then" in why
    assert score((2, 4), (40, 120)) < score((4, 4.5), (5, 156)) / 5
    # The week that resets first still wins while it lasts to its reset: 25 % used 48 hours into it.
    found["personal"] = record(NOW, (2, 4), (25, 120), NOW.timestamp())
    assert pick(found, claude_accounts)[0] == personal
    # And while it would run out only hours early: 30 % used runs out eight hours before its reset.
    found["personal"] = record(NOW, (2, 4), (30, 120), NOW.timestamp())
    assert pick(found, claude_accounts)[0] == personal
    # A week running out early still beats one whose running sessions already use what is left, resetting in 6 hours
    # against 156.
    found = {"personal": record(NOW, None, (43, 156), NOW.timestamp()), "work": record(NOW, None, (98, 6), NOW.timestamp())}
    assert accounts.pick([personal, work], found, NOW, None, {"work": 2})[0] == personal
    # With nothing left, an unused week and a barely used one score alike.
    assert accounts.usage.room(record(NOW, None, (0, 40), NOW.timestamp()), NOW.timestamp(), [], None, 67).week_runs_out_early == 40
    assert accounts.usage.room(record(NOW, None, (1e-9, 40), NOW.timestamp()), NOW.timestamp(), [], None, 67).week_runs_out_early == 40


def test_a_five_hour_window_on_course_to_run_out_sends_new_sessions_elsewhere(claude_accounts: tuple[Account, Account]) -> None:
    """Send new sessions elsewhere when the five-hour window is on course to run out."""
    personal, work = claude_accounts
    found = {
        "personal": record(NOW, (7, 3.4), (60, 87), NOW.timestamp()),
        "work": record(NOW, (10, 4.25), (13, 80), NOW.timestamp()),
    }
    assert pick(found, claude_accounts)[0] == work
    found["work"] = record(NOW, (78, 4.25), (13, 80), NOW.timestamp())
    assert pick(found, claude_accounts)[0] == personal
    # Early in the window a little use is no pace yet: 15 % used with 3 % gone, or 25 % with 10 % gone.
    for five in ((15, 4.85), (25, 4.5)):
        found["work"] = record(NOW, five, (13, 80), NOW.timestamp())
        assert pick(found, claude_accounts)[0] == work
    # A window on course to end below its limit keeps the account first: 60 % used with 70 % of it gone.
    found["work"] = record(NOW, (60, 1.5), (13, 80), NOW.timestamp())
    assert pick(found, claude_accounts)[0] == work


def test_a_reading_from_before_a_reset_counts_as_empty(claude_accounts: tuple[Account, Account]) -> None:
    personal, work = claude_accounts
    found = {
        # Used up, but the week reset an hour ago: a fresh week, resetting in 167 hours.
        "personal": record(NOW, None, (100, -1), NOW.timestamp() - 30 * HOUR),
        "work": record(NOW, None, (50, 170), NOW.timestamp()),
    }
    assert pick(found, claude_accounts)[0] == personal
    found["work"] = record(NOW, None, (30, 100), NOW.timestamp())
    assert pick(found, claude_accounts)[0] == work


def test_when_both_accounts_are_at_a_limit_the_one_free_soonest_gets_new_work(claude_accounts: tuple[Account, Account]) -> None:
    work = claude_accounts[1]
    found = {
        "personal": record(NOW, (100, 4), (60, 50), NOW.timestamp()),
        "work": record(NOW, (30, 1), (100, 2), NOW.timestamp()),
    }
    account, why = pick(found, claude_accounts)
    assert account == work
    assert why == "every account is at a limit; this one's ends first, at 2026-01-19 11:00 UTC"


def test_an_account_without_a_record_counts_as_a_fresh_week(claude_accounts: tuple[Account, Account]) -> None:
    personal, work = claude_accounts
    found = {"personal": record(NOW, None, (20, 100), NOW.timestamp()), "work": None}
    # A week resetting in 100 hours comes before a fresh one of 168, also nearly used, but not once no session fits.
    assert pick(found, claude_accounts)[0] == personal
    found["personal"] = record(NOW, None, (96, 30), NOW.timestamp())
    assert pick(found, claude_accounts)[0] == personal
    found["personal"] = record(NOW, None, (99.5, 6), NOW.timestamp())
    assert pick(found, claude_accounts)[0] == work


def test_an_account_holding_a_reset_that_expires_gets_new_work_until_its_5_hour_window_is_near(
    claude_accounts: tuple[Account, Account], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use an expiring reset while there is room for another session."""
    personal, work = claude_accounts
    sign_in(personal, "sam@home.test")
    sign_in(work, "sam@work.test")
    grant = {"label": "Launch offer", "left": 1, "total": 1, "ends_at": "2026-01-22T16:00:00+00:00",
             "usable_now": True, "needs_limit": False, "paused": False, "clears": ["five_hour", "seven_day"]}

    def bank(**changed: Any) -> None:
        answer = {"read_at": NOW.isoformat(), "grants": [grant | changed], "cooldown_until": None}
        monkeypatch.setattr(resets, "_known", {"sam@work.test": answer})

    bank()
    found = {
        # 80 % / 100 h = 0.8 per hour against work's 68 % / 100 h = 0.68.
        "personal": record(NOW, (10, 3), (20, 100), NOW.timestamp()),
        "work": record(NOW, (10, 3), (32, 100), NOW.timestamp()),
    }
    assert pick(found, claude_accounts) == (work, (
        "it holds a banked usage-limit reset that expires at 2026-01-22 16:00 UTC, so its week is used up first;"
        " its week resets at 2026-01-23 13:00 UTC, in 100 h, with 68 % of it left, and its 5-hour window is on course"
        " for 22 %"))
    # Near the end of its week it still gets new work: reaching the limit is when the reset pays.
    found["work"] = record(NOW, (10, 3), (95, 100), NOW.timestamp())
    assert pick(found, claude_accounts)[0] == work
    # Its 5-hour window on pace to fill, or a limit reached, sends new work elsewhere until it has room again.
    for five in ((78, 4.25), (100, 3)):
        found["work"] = record(NOW, five, (32, 100), NOW.timestamp())
        assert pick(found, claude_accounts)[0] == personal
    # A reset spent, paused, expired, without an end or one that leaves the week in place counts for nothing.
    found["work"] = record(NOW, (10, 3), (32, 100), NOW.timestamp())
    for spent in ({"left": 0}, {"paused": True}, {"ends_at": "2026-01-19T08:59:00+00:00"}, {"ends_at": None},
                  {"clears": ["five_hour"]}):
        bank(**spent)
        assert pick(found, claude_accounts)[0] == personal


def test_a_banked_reset_adds_no_room_and_ranks_its_account_first_once_all_are_at_a_limit(
    claude_accounts: tuple[Account, Account], monkeypatch: pytest.MonkeyPatch,
) -> None:
    personal, work = claude_accounts
    sign_in(personal, "sam@home.test")
    sign_in(work, "sam@work.test")
    grant = {"label": "Launch offer", "left": 1, "total": 1, "ends_at": "2026-01-22T16:00:00+00:00",
             "usable_now": True, "needs_limit": False, "paused": False, "clears": ["five_hour", "seven_day"]}

    def bank(cooldown_until: str | None = None, **changed: Any) -> None:
        answer = {"read_at": NOW.isoformat(), "grants": [grant | changed], "cooldown_until": cooldown_until}
        monkeypatch.setattr(resets, "_known", {"sam@work.test": answer})

    bank(ends_at=None)
    found = {
        "personal": record(NOW, (10, 3), (20, 100), NOW.timestamp()),
        "work": record(NOW, (10, 3), (30, 100), NOW.timestamp()),
    }
    assert pick(found, claude_accounts)[0] == personal
    bank()
    # Both at their weekly limit: personal's ends first, but work's reset lets the user go on now.
    found = {
        "personal": record(NOW, (10, 3), (100, 10), NOW.timestamp()),
        "work": record(NOW, (10, 3), (100, 50), NOW.timestamp()),
    }
    assert pick(found, claude_accounts) == (work, (
        "every account is at a limit; this one holds 1 banked usage-limit reset that ends it, to spend in Claude"
        " Code's limit dialog"))
    # A reset that leaves one of its limits in place, or one on cooldown, gets the account nowhere sooner.
    found["work"] = record(NOW, (100, 3), (100, 50), NOW.timestamp())
    for clears in (["five_hour"], ["seven_day"]):
        bank(clears=clears)
        assert pick(found, claude_accounts)[0] == personal
    bank(clears=["five_hour", "seven_day"])
    assert pick(found, claude_accounts)[0] == work
    bank(clears=["five_hour", "seven_day"], cooldown_until="2026-01-20T09:00:00+00:00")
    assert pick(found, claude_accounts)[0] == personal
    # Only its 5-hour window at its limit: a reset that clears it gets past one that ends sooner elsewhere.
    found = {
        "personal": record(NOW, (100, 1), (20, 100), NOW.timestamp()),
        "work": record(NOW, (100, 4), (20, 100), NOW.timestamp()),
    }
    bank(clears=["five_hour"])
    assert pick(found, claude_accounts)[0] == work
    for spent in ({"left": 0}, {"paused": True}, {"ends_at": "2026-01-19T08:59:00+00:00"}):
        bank(clears=["five_hour"], **spent)
        assert pick(found, claude_accounts)[0] == personal


def test_the_account_the_user_prefers_gets_new_work_until_its_time_ends(
    claude_accounts: tuple[Account, Account], tmp_path: Path,
) -> None:
    """A timed account preference takes precedence until its expiry."""
    personal, work = claude_accounts
    found = {
        "personal": record(NOW, (40, 3), (20, 100), NOW.timestamp()),
        "work": record(NOW, (10, 4), (60, 20), NOW.timestamp()),
    }
    conn = db.connect(tmp_path / "prefer.db")
    db.migrate(conn)
    accounts.prefer(conn, claude_accounts, "personal", 120, NOW)
    preference = accounts.preference(conn, NOW)
    assert preference == accounts.usage.Preference("personal", NOW + timedelta(hours=2), 120)
    assert accounts.pick([personal, work], found, NOW, preference, {}) == (
        personal, "you chose it for new sessions until 2026-01-19 11:00 UTC",
    )
    # Ahead of a 5-hour window way over, too: the choice is the user's.
    found["personal"] = record(NOW, (95, 3), (20, 100), NOW.timestamp())
    assert accounts.pick([personal, work], found, NOW, preference, {})[0] == personal
    # Not while it is at a limit, when a new session there would only wait.
    found["personal"] = record(NOW, (100, 3), (20, 100), NOW.timestamp())
    assert accounts.pick([personal, work], found, NOW, preference, {})[0] == work
    # Once its time is up, the usual order applies again.
    assert accounts.preference(conn, NOW + timedelta(hours=2)) is None
    accounts.prefer(conn, claude_accounts, None, 0, NOW)
    assert accounts.preference(conn, NOW) is None
    with pytest.raises(ValueError, match="no account folder is named other"):
        accounts.prefer(conn, claude_accounts, "other", 30, NOW)
    with pytest.raises(ValueError, match="not 45"):
        accounts.prefer(conn, claude_accounts, "work", 45, NOW)


def test_the_user_sets_and_clears_the_preferred_account_on_the_page(world: World) -> None:
    answer = world.client.post("/api/accounts/prefer", json={"account": "work", "minutes": 30}, headers=world.user())
    assert answer.status_code == 200 and answer.json()["account"] == "work"
    assert world.client.post("/api/accounts/prefer", json={"account": "work", "minutes": 30}, headers=world.agent()).status_code == 403
    assert world.client.post("/api/accounts/prefer", json={"account": "work", "minutes": 7}, headers=world.user()).status_code == 400
    answer = world.client.post("/api/accounts/prefer", json={"account": None, "minutes": 0}, headers=world.user())
    assert answer.json() == {"account": None, "until": None}


def pick(found: dict[str, Any], both: tuple[Account, Account]) -> tuple[Account, str]:
    return accounts.pick(list(both), found, NOW, None, {})


def test_readings_match_records_to_accounts_by_signed_in_address(claude_accounts: tuple[Account, Account], tmp_path: Path) -> None:
    personal, work = claude_accounts
    sign_in(personal, "sam@home.test")
    sign_in(work, "sam@work.test")
    path = tmp_path / "rate-limits.json"
    mine = record(NOW, (10, 2), None, NOW.timestamp())
    path.write_text(json.dumps({"accounts": {"sam@work.test": mine, "someone@else.test": mine}}))
    assert accounts.readings([personal, work], path) == {"personal": None, "work": mine}


# Limit wakes, per account


def test_a_limit_stop_wakes_when_its_own_account_has_room(
    world: World, claude_accounts: tuple[Account, Account], monkeypatch
) -> None:
    stop = (datetime.now(UTC) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    monkeypatch.setattr(limits, "limit_stop", {SESSION_A: LimitStop(stop, None), SESSION_B: LimitStop(stop, None)}.get)
    answering = "33333333-3333-4333-8333-333333333333"
    world.cache.sessions = [
        make_session(SESSION_A, pid=None), on_work(make_session(SESSION_B, pid=None)),
        on_work(make_session(answering, pid=103, state="working")),
    ]
    now = datetime.now(UTC)
    later = now.timestamp() - 60
    # A session on the work account got an answer after the stop: that says nothing about the personal account.
    monkeypatch.setattr(limits, "last_answer", lambda session_id, since: (now, True) if session_id == answering else None)
    readings = {
        # Personal is still spent.
        "personal": {"rate_limits": {"five_hour": {"used_percentage": 100, "resets_at": now.timestamp() + HOUR}},
                     "captured_at": later},
        "work": {"rate_limits": {"five_hour": {"used_percentage": 12, "resets_at": now.timestamp() + HOUR}},
                 "captured_at": later},
    }
    conn = db.connect(world.db_path)
    try:
        sent = limits.sync(conn, world.cache.sessions, readings, now, world.checkout.parent)
        told = db.rows(conn.execute("SELECT session_id FROM messages").fetchall())
    finally:
        conn.close()
    assert len(sent) == 1
    assert told == [{"session_id": SESSION_B}]


# The shared links


@pytest.fixture
def linux_sharing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sharing diagnostics use the Linux setup contract on every test host."""
    monkeypatch.setattr(accounts.sys, "platform", "linux")


@pytest.mark.usefixtures("linux_sharing")
def test_the_shared_links_are_checked_and_each_problem_is_named(
    claude_accounts: tuple[Account, Account],
) -> None:
    personal, work = claude_accounts
    assert accounts.link_problems((personal, work)) == []  # no work folder yet
    link_shared(personal, work)
    assert accounts.link_problems((personal, work)) == []  # dangling links to the right place are fine
    (work.folder / "settings.json").unlink()
    (work.folder / "settings.json").write_text("{}")
    (work.folder / "skills").unlink()
    (work.folder / "skills").symlink_to(work.folder / "elsewhere")
    (work.folder / "agents").unlink()
    [problem] = accounts.link_problems((personal, work))
    # The words say what is wrong, what it breaks and what to do, with no paths; the paths are in the details.
    assert problem.text == (
        "The work account has stopped sharing your settings, your skills and your subagents with the personal"
        " account. Changes to your permission rules, auto-mode sentences and Cockpit's hooks no longer reach its"
        " sessions, so they may ask before calls you allowed, and their permission dialogs may not show on Cockpit."
        " Skills you add or change no longer reach its sessions. Subagents you add or change no longer reach its"
        " sessions. Ask a session to repair it with the script named in the details."
    )
    assert problem.detail.splitlines() == [
        f"{work.folder / 'settings.json'} is a real file, not a link to {personal.folder / 'settings.json'}. Claude"
        " Code writes a new file in place of a link when it rewrites one, so it may hold changes made on the work"
        f" account: merge them into {personal.folder / 'settings.json'}, remove it, then run the script.",
        f"{work.folder / 'skills'} links to {work.folder / 'elsewhere'}, not to {personal.folder / 'skills'}; point it"
        " back, then run the script.",
        f"{work.folder / 'agents'} is missing; the script links it to {personal.folder / 'agents'}.",
        accounts.SCRIPT,
    ]


@pytest.mark.parametrize("platform", ["darwin", "freebsd"])
def test_sharing_diagnostics_are_unavailable_off_linux(claude_accounts, monkeypatch, platform):
    personal, work = claude_accounts
    link_shared(personal, work)
    (work.folder / "settings.json").unlink()
    monkeypatch.setattr(accounts.sys, "platform", platform)
    assert accounts.link_problems((personal, work)) == []


@pytest.mark.usefixtures("linux_sharing")
def test_projects_reached_through_a_link_is_named_as_the_cause_of_memory_dialogs(
    claude_accounts: tuple[Account, Account],
) -> None:
    personal, work = claude_accounts
    link_shared(personal, work)
    (personal.folder / "projects").unlink()
    (work.folder / "projects").rmdir()
    (personal.folder / "projects").mkdir()
    (work.folder / "projects").symlink_to(personal.folder / "projects")  # the setup before PERM-15
    assert accounts.link_problems((personal, work)) == [accounts.Problem(
        "Sessions on the work account ask you before every memory note they save, because they reach the shared"
        " transcripts folder through a link. Ask a session to mount the folder instead, with the script named in the"
        " details.",
        f"{work.folder / 'projects'} is a link to {personal.folder / 'projects'}; it should be a bind mount of it"
        f" (SPEC PERM-15).\n{accounts.SCRIPT}",
    )]


@pytest.mark.usefixtures("linux_sharing")
def test_a_broken_mount_shows_on_the_page_and_is_logged_as_an_error(
    claude_accounts: tuple[Account, Account], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    personal, work = claude_accounts
    link_shared(personal, work)
    (personal.folder / "projects").unlink()
    (personal.folder / "projects").mkdir()  # a mount that failed at boot: two separate folders
    fake_agents(monkeypatch, {"": []})
    cache = sessions.SessionCache((personal, work), dict, list)
    with caplog.at_level(logging.ERROR):
        listing = cache.snapshot(wait=False)
    [problem] = listing.account_problems
    assert problem.text.startswith(
        "The work account has stopped sharing your transcripts and memory notes with the personal account. A memory"
        " note saved on one account is missing on the other"
    )
    assert problem.detail.startswith(f"{work.folder / 'projects'} is a folder of its own, not a bind mount of")
    assert not any("stopped sharing" in warning for warning in listing.warnings)  # once, with its paths apart
    assert "Claude accounts no longer share what they should" in caplog.text


@pytest.mark.usefixtures("linux_sharing")
def test_an_entry_that_cannot_be_checked_is_named_instead_of_failing_the_page(
    claude_accounts: tuple[Account, Account], monkeypatch: pytest.MonkeyPatch
) -> None:
    personal, work = claude_accounts
    link_shared(personal, work)

    def unreadable(entry: Path, target: Path) -> bool:
        raise PermissionError(13, "Permission denied", str(entry))

    monkeypatch.setattr(accounts, "bind_mount_of", unreadable)
    [problem] = accounts.link_problems((personal, work))
    assert problem.text.startswith("Cockpit could not check what the work account shares with the personal account")
    assert problem.detail == (
        f"Checking {work.folder / 'projects'} failed: [Errno 13] Permission denied: '{work.folder / 'projects'}'"
    )


@pytest.mark.usefixtures("linux_sharing")
def test_a_link_that_loops_is_named_instead_of_failing_the_page(claude_accounts: tuple[Account, Account]) -> None:
    personal, work = claude_accounts
    link_shared(personal, work)
    (work.folder / "skills").unlink()
    (work.folder / "skills").symlink_to(work.folder / "skills")
    [problem] = accounts.link_problems((personal, work))
    assert problem.text.startswith("Cockpit could not check what the work account shares")
    assert problem.detail.startswith(f"Checking {work.folder / 'skills'} failed:")


def test_the_page_state_carries_account_problems_with_their_paths_apart(world: World) -> None:
    world.cache.account_problems = [accounts.Problem("The work account has stopped sharing your skills.", "a\nb")]
    state = world.client.get("/api/state").json()
    assert state["account_problems"] == [
        {"text": "The work account has stopped sharing your skills.", "detail": "a\nb"}
    ]


def test_only_the_folder_itself_counts_as_its_bind_mount(tmp_path: Path) -> None:
    target, other = tmp_path / "projects", tmp_path / "other"
    target.mkdir()
    other.mkdir()
    (tmp_path / "alias").symlink_to(tmp_path)
    (tmp_path / "link").symlink_to(target)
    # What a bind mount looks like: a path that is no symlink itself and is the same folder.
    assert accounts.bind_mount_of(tmp_path / "alias" / "projects", target)
    assert not accounts.bind_mount_of(tmp_path / "link", target)  # a symlink, which Claude Code refuses writes through
    assert not accounts.bind_mount_of(other, target)  # a folder of its own: the account no longer shares it
    assert not accounts.bind_mount_of(tmp_path / "missing", target)
    assert not accounts.bind_mount_of(other, tmp_path / "missing")


def test_a_stale_listing_answers_at_once_while_the_next_runs_in_the_background(
    claude_accounts: tuple[Account, Account], monkeypatch: pytest.MonkeyPatch
) -> None:
    personal, work = claude_accounts
    rows: dict[str, Any] = {"": [row(SESSION_A)]}
    commands = fake_agents(monkeypatch, rows)
    listed = threading.Event()
    hold = threading.Event()
    real_list = sessions.list_account

    def slow_list(account: Account, warnings: list[str]) -> list[sessions.Session]:
        hold.wait(5)
        result = real_list(account, warnings)
        listed.set()
        return result

    cache = sessions.SessionCache((personal, work), dict, list)
    hold.set()
    assert [session.session_id for session in cache.get()] == [SESSION_A]  # the first get waits for its listing
    monkeypatch.setattr(sessions, "list_account", slow_list)
    monkeypatch.setattr(sessions, "CACHE_SECONDS", 0)
    rows[""] = [row(SESSION_B)]
    hold.clear()
    listed.clear()
    # The listing is stale: the page gets the last list at once, and the next listing starts behind it.
    assert [session.session_id for session in cache.get()] == [SESSION_A]
    hold.set()
    assert listed.wait(5)
    monkeypatch.setattr(sessions, "CACHE_SECONDS", 3600)
    for _ in range(100):
        if [session.session_id for session in cache.get()] == [SESSION_B]:
            break
        time.sleep(0.01)
    assert [session.session_id for session in cache.get()] == [SESSION_B]
    count = len(commands)
    # After invalidate, get waits for a listing that started after it.
    rows[""] = [row(SESSION_A)]
    cache.invalidate()
    assert [session.session_id for session in cache.get()] == [SESSION_A]
    assert len(commands) == count + 1
    # The sweeper's snapshot waits for a new listing once the last one expired.
    monkeypatch.setattr(sessions, "CACHE_SECONDS", 0)
    rows[""] = [row(SESSION_B)]
    assert [session.session_id for session in cache.snapshot(wait=True).sessions] == [SESSION_B]
    assert len(commands) == count + 2


def test_a_listing_slower_than_the_cache_time_is_still_reused_after_it(
    claude_accounts: tuple[Account, Account], monkeypatch: pytest.MonkeyPatch
) -> None:
    personal, work = claude_accounts
    commands = fake_agents(monkeypatch, {"": [row(SESSION_A)]})
    real_list = sessions.list_account

    def slow_list(account: Account, warnings: list[str]) -> list[sessions.Session]:
        time.sleep(3)
        return real_list(account, warnings)

    monkeypatch.setattr(sessions, "list_account", slow_list)
    monkeypatch.setattr(sessions, "CACHE_SECONDS", 2)
    cache = sessions.SessionCache((personal, work), dict, list)
    cache.get()
    count = len(commands)
    # Its age counts from when it finished, so the next get answers from it and starts no listing.
    assert [session.session_id for session in cache.get()] == [SESSION_A]
    assert len(commands) == count and not cache._refreshing


def test_a_call_that_waits_behind_a_listing_takes_it_without_listing_again(
    claude_accounts: tuple[Account, Account], monkeypatch: pytest.MonkeyPatch
) -> None:
    personal, work = claude_accounts
    rows: dict[str, Any] = {"": [row(SESSION_A)]}
    commands = fake_agents(monkeypatch, rows)
    hold = threading.Event()
    real_list = sessions.list_account

    def held_list(account: Account, warnings: list[str]) -> list[sessions.Session]:
        hold.wait(5)
        return real_list(account, warnings)

    monkeypatch.setattr(sessions, "CACHE_SECONDS", 60)
    cache = sessions.SessionCache((personal, work), dict, list)
    cache.get()
    cache._finished -= 120  # the listing expired a minute ago
    monkeypatch.setattr(sessions, "list_account", held_list)
    rows[""] = [row(SESSION_B)]
    cache.get()  # the next listing starts in the background and waits on `hold`
    count = len(commands)
    # The sweeper's call waits behind that listing and takes it, as it finished after the call was made.
    answers: list[sessions.Listing] = []
    waiting = threading.Thread(target=lambda: answers.append(cache.snapshot(wait=True)))
    waiting.start()
    hold.set()
    waiting.join(10)
    assert [session.session_id for session in answers[0].sessions] == [SESSION_B]
    assert len(commands) == count + 1


def test_a_background_listing_queued_behind_a_waiting_call_does_not_list_again(
    claude_accounts: tuple[Account, Account], monkeypatch: pytest.MonkeyPatch
) -> None:
    personal, work = claude_accounts
    rows: dict[str, Any] = {"": [row(SESSION_A)]}
    commands = fake_agents(monkeypatch, rows)
    hold = threading.Event()
    started = threading.Event()
    real_list = sessions.list_account

    def held_list(account: Account, warnings: list[str]) -> list[sessions.Session]:
        started.set()
        hold.wait(5)
        return real_list(account, warnings)

    monkeypatch.setattr(sessions, "CACHE_SECONDS", 60)
    cache = sessions.SessionCache((personal, work), dict, list)
    cache.get()
    cache._finished -= 120  # the listing expired a minute ago
    monkeypatch.setattr(sessions, "list_account", held_list)
    rows[""] = [row(SESSION_B)]
    count = len(commands)
    waiting = threading.Thread(target=lambda: cache.snapshot(wait=True))
    waiting.start()  # the sweeper's call lists, held on `hold`
    assert started.wait(5)
    cache.get()  # a page poll meanwhile queues a background listing behind it
    hold.set()
    waiting.join(10)
    for _ in range(500):
        with cache._lock:
            if not cache._refreshing:
                break
        time.sleep(0.01)
    assert not cache._refreshing
    assert [session.session_id for session in cache.get()] == [SESSION_B]
    assert len(commands) == count + 1


def retire_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """The setup file retires the work account (SPEC UI-78)."""
    setup = replace(config.read_setup(config.SETUP_PATH), retired_accounts=("work",))
    monkeypatch.setattr(config, "setup", lambda: setup)


def test_a_retired_account_gets_no_new_work_and_no_sign_in_warning(
    claude_accounts: tuple[Account, Account], monkeypatch: pytest.MonkeyPatch,
) -> None:
    personal, work = claude_accounts
    sign_in(personal, "me@home.example")
    sign_in(work, "me@work.example")
    (work.folder / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"refreshToken": ""}}))
    assert [entry["account"] for entry in accounts.sign_ins(claude_accounts, NOW)] == ["work"]
    retire_work(monkeypatch)
    warnings: list[str] = []
    assert accounts.in_use(claude_accounts, warnings) == [personal]
    assert warnings == []
    assert accounts.sign_ins(claude_accounts, NOW) == []


def test_a_retired_folder_without_a_sign_in_raises_no_warning(
    claude_accounts: tuple[Account, Account], monkeypatch: pytest.MonkeyPatch,
) -> None:
    personal, work = claude_accounts
    work.folder.mkdir(parents=True, exist_ok=True)
    (work.folder / ".credentials.json").unlink(missing_ok=True)
    warnings: list[str] = []
    accounts.in_use(claude_accounts, warnings)
    assert warnings
    retire_work(monkeypatch)
    warnings = []
    assert accounts.listed(claude_accounts, warnings) == [personal, work]
    assert warnings == []


@pytest.mark.usefixtures("linux_sharing")
def test_a_retired_account_shares_nothing_worth_a_warning(
    claude_accounts: tuple[Account, Account], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, work = claude_accounts
    work.folder.mkdir(parents=True, exist_ok=True)
    assert accounts.link_problems(claude_accounts)
    retire_work(monkeypatch)
    assert accounts.link_problems(claude_accounts) == []


def test_a_stopped_session_of_a_retired_account_is_listed_on_the_default_one(
    claude_accounts: tuple[Account, Account], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _personal, work = claude_accounts
    sign_in(work, "me@work.example")
    fake_agents(monkeypatch, {"": [], str(work.folder): [{**row(SESSION_A), "state": "stopped"}]})
    retire_work(monkeypatch)
    listed, failed = sessions.list_sessions(claude_accounts, {}, [])
    assert failed == []
    assert [(session.session_id, session.account) for session in listed] == [(SESSION_A, "personal")]


def test_a_live_session_of_a_retired_account_stays_there_so_a_stop_reaches_it(
    claude_accounts: tuple[Account, Account], monkeypatch: pytest.MonkeyPatch,
) -> None:
    retire_work(monkeypatch)
    live = make_session(SESSION_A, 123, account="work")
    assert [session.account for session in sessions.merge([[live]], {}, [])] == ["work"]


def test_a_retired_account_leaves_the_usage_display(
    claude_accounts: tuple[Account, Account], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    personal, work = claude_accounts
    sign_in(personal, "me@home.example")
    sign_in(work, "me@work.example")
    saved = {"accounts": {
        "me@home.example": {**record(NOW, None, (10, 24), NOW.timestamp()), "session_id": SESSION_A},
        "me@work.example": {**record(NOW, None, (100, 24), NOW.timestamp()), "session_id": SESSION_B},
    }}
    path = tmp_path / "rate-limits.json"
    path.write_text(json.dumps(saved))
    shown = usage.read(path, NOW, accounts.in_use(claude_accounts, []), None, {})
    assert {each["email"] for each in shown["accounts"]} == {"me@home.example", "me@work.example"}
    retire_work(monkeypatch)
    shown = usage.read(path, NOW, accounts.in_use(claude_accounts, []), None, {})
    assert [each["email"] for each in shown["accounts"]] == ["me@home.example"]
    # The folder lost the address it held; the record still names the folder.
    work.state.write_text("{}")
    saved["accounts"]["me@work.example"]["config_dir"] = str(work.folder)
    path.write_text(json.dumps(saved))
    shown = usage.read(path, NOW, accounts.in_use(claude_accounts, []), None, {})
    assert [each["email"] for each in shown["accounts"]] == ["me@home.example"]


def test_removal_keeps_the_flags_of_the_newest_job_when_a_retired_account_holds_one(
    claude_accounts: tuple[Account, Account], monkeypatch: pytest.MonkeyPatch,
) -> None:
    personal, work = claude_accounts
    session = make_session(SESSION_A, None)
    for account, age in ((personal, 100), (work, 10)):
        job = account.jobs / session.id
        job.mkdir(parents=True)
        (job / "state.json").write_text("{}")
        os.utime(job / "state.json", (NOW.timestamp() - age, NOW.timestamp() - age))
    assert removals.holders(claude_accounts, session) == [personal, work]
    retire_work(monkeypatch)
    assert removals.holders(claude_accounts, session) == [work, personal]


def test_new_calls_for_a_retired_session_still_alive_name_the_default_account(
    claude_accounts: tuple[Account, Account], monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert accounts.current_name("work") == "work"
    retire_work(monkeypatch)
    assert accounts.current_name("work") == "personal"
    assert accounts.current_name("personal") == "personal"
