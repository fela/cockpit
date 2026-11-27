"""The CLIProxyAPI hub's accounts at the foot of the page, signing Codex and Claude accounts into it (SPEC UI-81), and
the read and controls of the hub's part of the Usage page (SPEC UI-85), against a stand-in hub on a local port and
stand-ins for its two logins, the tunnel's state and the restarts."""

from __future__ import annotations

import json
import logging
import socket
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cockpit import config, db, hub
from tests.conftest import lose_the_hub_key, use_hub
from tests.fake_hub import FakeHub, error_log

FAKE_LOGIN = Path(__file__).parent / "fake_hub_login.py"
FAKE_CLAUDE_LOGIN = Path(__file__).parent / "fake_hub_claude_login.py"
KEY = "m" * 32
NOW = datetime(2026, 11, 9, 12, tzinfo=UTC)
CODEX = {
    "id": "codex-me@example.com-plus.json", "auth_index": 1, "provider": "codex", "email": "me@example.com",
    "id_token": {"chatgpt_account_id": "acct-1", "plan_type": "plus"},
}
CLAUDE = {"id": "claude-you@example.com.json", "auth_index": 2, "provider": "claude", "email": "you@example.com"}
# What the hub keeps of each account's last request: the provider's limit headers, and when they came.
CODEX_SEEN = {"observed_at": "2026-11-09T11:50:00.123456789Z", "signals": {
    "X-Codex-Plan-Type": "pro",
    "X-Codex-Primary-Used-Percent": "25", "X-Codex-Primary-Window-Minutes": "300",
    "X-Codex-Primary-Reset-At": str(int(NOW.timestamp()) + 3600),
    "X-Codex-Secondary-Used-Percent": "40", "X-Codex-Secondary-Window-Minutes": "10080",
    "X-Codex-Secondary-Reset-At": str(int(NOW.timestamp()) + 86400),
}}
CLAUDE_SEEN = {"observed_at": "2026-11-09T11:00:00Z", "signals": {
    "Anthropic-Ratelimit-Unified-5h-Utilization": "0.1",
    "Anthropic-Ratelimit-Unified-5h-Reset": str(int(datetime(2026, 11, 9, 14, tzinfo=UTC).timestamp())),
    "Anthropic-Ratelimit-Unified-7d-Utilization": "0.7",
    "Anthropic-Ratelimit-Unified-7d-Reset": str(int(datetime(2026, 11, 12, 12, tzinfo=UTC).timestamp())),
    # The weekly limit that counts Fable's use alone.
    "Anthropic-Ratelimit-Unified-7d_oi-Utilization": "0.38",
    "Anthropic-Ratelimit-Unified-7d_oi-Reset": str(int(datetime(2026, 11, 12, 12, tzinfo=UTC).timestamp())),
    "Anthropic-Ratelimit-Unified-Status": "allowed",
}}
# The providers' answers to Cockpit's check of an account's limits (Hub._check), for an account that has done little:
# Codex's usage endpoint, and Claude's, whose 5-hour window has no use or reset time yet.
CODEX_ANSWER = {"plan_type": "pro", "rate_limit": {
    "primary_window": {"used_percent": 0, "limit_window_seconds": 18000, "reset_at": int(NOW.timestamp()) + 18000},
    "secondary_window": {"used_percent": 3, "limit_window_seconds": 604800, "reset_at": int(NOW.timestamp()) + 86400},
}}
CLAUDE_ANSWER = {
    "five_hour": {"utilization": None, "resets_at": None},
    "seven_day": {"utilization": 12.0, "resets_at": "2026-11-14T09:00:00.500000+00:00"},
    "limits": [{"kind": "weekly_scoped", "group": "weekly", "percent": 5, "resets_at": "2026-11-14T09:00:00+00:00",
                "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}}],
}


def until(check, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = check()
        if found:
            return found
        time.sleep(0.02)
    raise AssertionError("it did not happen in time")


TUNNEL_ACTIVE = ("echo", "active")
NOWHERE = "http://127.0.0.1:9"

# Each test runs with a [hub] table in the setup whose key file is missing, until it supplies a stand-in hub.
pytestmark = pytest.mark.usefixtures("hub_on")


def NO_ACTION(accounts: list[dict[str, Any]], now: datetime) -> None:
    """A reader's action after a read, for tests that look at the read alone."""


@pytest.fixture
def fake_hub(tmp_path: Path):
    found = FakeHub(KEY)
    yield found
    found.server.shutdown()


@pytest.fixture(autouse=True)
def claude_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An installed Claude Code, whose release names the requests Cockpit makes as Claude Code does (resets.headers)."""
    release = tmp_path / "claude-code" / "versions" / "2.1.10"
    release.parent.mkdir(parents=True)
    release.write_text("")
    (tmp_path / "claude-code" / "claude").symlink_to(release)
    monkeypatch.setattr(hub.resets, "CLAUDE_COMMAND", tmp_path / "claude-code" / "claude")


def database(tmp_path: Path) -> Path:
    path = tmp_path / "cockpit.db"
    conn = db.connect(path)
    db.migrate(conn)
    conn.close()
    return path


def reader(fake: FakeHub, tmp_path: Path, key: str = KEY) -> hub.Hub:
    path = tmp_path / "management-key"
    path.write_text(key + "\n")
    logins = {"codex": (sys.executable, str(FAKE_LOGIN), str(tmp_path)),
              "claude": (sys.executable, str(FAKE_CLAUDE_LOGIN), str(tmp_path))}
    return hub.Hub(fake.url, path, logins, TUNNEL_ACTIVE, {}, database(tmp_path), NO_ACTION)


def checks_kept(tmp_path: Path) -> Any:
    """Each account's last check as the database keeps it (hub.LIMIT_CHECKS_KEY)."""
    conn = db.connect(tmp_path / "cockpit.db")
    try:
        return db.kv_get(conn, hub.LIMIT_CHECKS_KEY)
    finally:
        conn.close()


def test_a_machine_without_the_key_file_has_no_hub(tmp_path):
    found = hub.Hub(NOWHERE, tmp_path / "missing", {"codex": ("true",)}, TUNNEL_ACTIVE, {}, database(tmp_path), NO_ACTION)
    assert found.read(NOW, True) is None


def test_each_account_shows_its_state_and_both_windows(fake_hub, tmp_path):
    fake_hub.credentials = [CODEX | {"quota": CODEX_SEEN}, CLAUDE | {"quota": CLAUDE_SEEN}]
    found = reader(fake_hub, tmp_path).refresh(NOW)
    assert found["error"] is None
    claude, codex = found["accounts"]
    # The plan is the one Codex named on the account's last request, which is newer than the sign-in's.
    assert (codex["provider"], codex["email"], codex["plan"], codex["state"]) == ("codex", "me@example.com", "pro", "working")
    assert [(each["key"], each["used"]) for each in codex["windows"]] == [("five_hour", 25), ("seven_day", 40)]
    assert codex["week"]["used"] == 40 and codex["limits_at"] == "2026-11-09T11:50:00Z"
    assert [(each["key"], each["used"]) for each in claude["windows"]] == [("five_hour", 10), ("seven_day", 70)]
    assert claude["week"]["resets_at"].startswith("2026-11-12T12:00")
    # Each window carries the time of the request its numbers came with.
    assert {each["captured_at"] for each in claude["windows"]} == {"2026-11-09T11:00:00Z"} == {claude["limits_at"]}
    # Claude's weekly limit for one model alone stands beside the week all models share; Codex has none.
    assert [(week["model"], week["used"], week["resets_at"]) for week in claude["model_weeks"]] == [("Fable", 38, "2026-11-12T12:00:00Z")]
    assert claude["model_weeks"][0]["captured_at"] == claude["limits_at"]
    assert codex["model_weeks"] == []
    codex_cred = CODEX | {"quota": CODEX_SEEN}
    assert hub.limits_now(codex_cred, {}, NOW.timestamp())["models"] == {}


@pytest.mark.parametrize(("plan", "label"), [("promax", "Pro $500"), ("pro", "pro"), ("plus", "plus"), ("future", "future")])
def test_codex_plan_labels_identify_the_500_plan(plan, label):
    credential = CODEX | {"id_token": {"plan_type": plan}}
    limits = hub.limits_now(credential, {}, NOW.timestamp())
    found = hub.account(credential, limits, NOW.timestamp(), "")
    assert found["plan"] == plan
    assert found["plan_label"] == label
    # A fresh provider reading replaces the plan recorded at sign-in.
    found = hub.account(credential, limits | {"plan": "promax"}, NOW.timestamp(), "")
    assert found["plan_label"] == "Pro $500"


def test_a_weekly_limit_for_a_model_cockpit_cannot_name_is_logged_and_left_out(caplog):
    unnamed = CLAUDE_SEEN["signals"] | {"Anthropic-Ratelimit-Unified-7d_zz-Utilization": "0.2", "Anthropic-Ratelimit-Unified-7d_zz-Reset": "1"}
    cred = CLAUDE | {"quota": CLAUDE_SEEN | {"signals": unnamed}}
    claude = hub.account(cred, hub.limits_now(cred, {}, NOW.timestamp()), NOW.timestamp(), "")
    assert [week["model"] for week in claude["model_weeks"]] == ["Fable"]
    assert "7d_zz" in caplog.text


def fable_weeks(account: dict[str, Any]) -> list[tuple[str, int | None, str | None]]:
    return [(week["model"], week["used"], week["resets_at"]) for week in account["model_weeks"]]


def without_fable(observed_at: str) -> dict[str, Any]:
    """What the hub keeps of a Claude request whose headers name no one-model limit."""
    signals = {name: value for name, value in CLAUDE_SEEN["signals"].items() if "7d_oi" not in name}
    return {"observed_at": observed_at, "signals": signals}


def at_minutes(minutes: int) -> datetime:
    return datetime.fromtimestamp(NOW.timestamp() + minutes * 60, UTC)


def test_a_model_limit_the_last_request_did_not_name_is_kept_until_it_resets(fake_hub, tmp_path):
    found = reader(fake_hub, tmp_path)
    fake_hub.credentials = [CLAUDE | {"quota": CLAUDE_SEEN}]
    assert fable_weeks(found.refresh(NOW)["accounts"][0]) == [("Fable", 38, "2026-11-12T12:00:00Z")]
    # The account's next request carries only the limits all models share.
    fake_hub.credentials = [CLAUDE | {"quota": without_fable("2026-11-09T11:30:00Z")}]
    [claude] = found.refresh(at_minutes(1))["accounts"]
    assert fable_weeks(claude) == [("Fable", 38, "2026-11-12T12:00:00Z")]
    assert claude["model_weeks"][0]["captured_at"] == "2026-11-09T11:00:00Z"
    assert claude["limits_at"] == "2026-11-09T11:30:00Z"
    # A later request names it again: its headers win.
    named = CLAUDE_SEEN | {"observed_at": "2026-11-09T11:45:00Z",
                           "signals": CLAUDE_SEEN["signals"] | {"Anthropic-Ratelimit-Unified-7d_oi-Utilization": "0.5"}}
    fake_hub.credentials = [CLAUDE | {"quota": named}]
    [claude] = found.refresh(at_minutes(2))["accounts"]
    assert fable_weeks(claude) == [("Fable", 50, "2026-11-12T12:00:00Z")]
    assert claude["model_weeks"][0]["captured_at"] == "2026-11-09T11:45:00Z"
    # Once the limit has reset, a request that does not name it leaves no week for it.
    fake_hub.credentials = [CLAUDE | {"quota": without_fable("2026-11-12T12:30:00Z")}]
    after_reset = datetime(2026, 11, 12, 13, tzinfo=UTC)
    assert found.refresh(after_reset)["accounts"][0]["model_weeks"] == []


def test_a_kept_model_limit_outlasts_a_restart_of_cockpit(fake_hub, tmp_path):
    fake_hub.credentials = [CLAUDE | {"quota": CLAUDE_SEEN}]
    assert fable_weeks(reader(fake_hub, tmp_path).refresh(NOW)["accounts"][0]) == [("Fable", 38, "2026-11-12T12:00:00Z")]
    fake_hub.credentials = [CLAUDE | {"quota": without_fable("2026-11-09T11:30:00Z")}]
    [claude] = reader(fake_hub, tmp_path).refresh(at_minutes(1))["accounts"]
    assert fable_weeks(claude) == [("Fable", 38, "2026-11-12T12:00:00Z")]
    assert claude["model_weeks"][0]["captured_at"] == "2026-11-09T11:00:00Z"


def test_an_account_that_left_the_hub_loses_its_kept_limits(fake_hub, tmp_path):
    found = reader(fake_hub, tmp_path)
    fake_hub.credentials = [CODEX | {"quota": CODEX_SEEN}, CLAUDE | {"quota": CLAUDE_SEEN}]
    assert fable_weeks(found.refresh(NOW)["accounts"][0]) == [("Fable", 38, "2026-11-12T12:00:00Z")]
    fake_hub.credentials = []
    assert found.refresh(at_minutes(1))["accounts"] == []
    fake_hub.credentials = [CODEX | {"quota": {"signals": {}}}, CLAUDE | {"quota": without_fable("2026-11-09T11:30:00Z")}]
    claude, codex = found.refresh(at_minutes(2))["accounts"]
    assert claude["model_weeks"] == []
    assert codex["limits_at"] is None and [each["used"] for each in codex["windows"]] == [None, None]


def test_a_limit_only_one_models_record_names_is_read_from_that_record(fake_hub, tmp_path):
    # The account's last request was for another model, so its own record names no Fable limit; the hub's record of
    # the last Fable request does.
    fable = CLAUDE_SEEN | {"observed_at": "2026-11-09T10:00:00Z"}
    quotas = {"quota": without_fable("2026-11-09T11:30:00Z"), "model_quotas": {"claude-fable-5-2": fable}}
    fake_hub.credentials = [CLAUDE | quotas]
    [claude] = reader(fake_hub, tmp_path).refresh(NOW)["accounts"]
    assert fable_weeks(claude) == [("Fable", 38, "2026-11-12T12:00:00Z")]
    assert claude["model_weeks"][0]["captured_at"] == "2026-11-09T10:00:00Z"
    # The week all models share comes from the newer of the two records.
    assert (claude["week"]["used"], claude["week"]["captured_at"], claude["limits_at"]) == (70, "2026-11-09T11:30:00Z", "2026-11-09T11:30:00Z")


@pytest.mark.parametrize("observed_at", ["2026-11-09T12:30:00+01:00", "2026-11-09T11:30:00"])
def test_a_limit_is_dated_in_utc_whatever_offset_the_hub_wrote(fake_hub, tmp_path, observed_at):
    # A time without an offset is in UTC.
    fake_hub.credentials = [CLAUDE | {"quota": CLAUDE_SEEN | {"observed_at": observed_at}}]
    [claude] = reader(fake_hub, tmp_path).refresh(NOW)["accounts"]
    assert (claude["week"]["captured_at"], claude["limits_at"]) == ("2026-11-09T11:30:00Z", "2026-11-09T11:30:00Z")


def test_limits_outlast_a_hub_that_restarted_and_holds_no_record(fake_hub, tmp_path):
    found = reader(fake_hub, tmp_path)
    fake_hub.credentials = [CODEX | {"quota": CODEX_SEEN}, CLAUDE | {"quota": CLAUDE_SEEN}]
    found.refresh(NOW)
    # A restarted hub lists the accounts with empty records.
    fake_hub.credentials = [CODEX | {"quota": {"signals": {}}}, CLAUDE]
    claude, codex = found.refresh(at_minutes(1))["accounts"]
    assert (codex["week"]["used"], codex["week"]["captured_at"], codex["plan"]) == (40, "2026-11-09T11:50:00Z", "pro")
    assert (claude["week"]["used"], claude["week"]["captured_at"]) == (70, "2026-11-09T11:00:00Z")
    assert fable_weeks(claude) == [("Fable", 38, "2026-11-12T12:00:00Z")]
    # A kept limit is gone once it has reset: Codex's 5-hour window an hour after NOW, its week a day after.
    [_, codex] = found.refresh(at_minutes(61))["accounts"]
    assert [(each["key"], each["used"]) for each in codex["windows"]] == [("five_hour", None), ("seven_day", 40)]
    [_, codex] = found.refresh(at_minutes(24 * 60 + 1))["accounts"]
    assert codex["week"]["used"] is None and codex["limits_at"] is None


def test_a_read_that_fails_keeps_the_last_accounts_and_says_why(fake_hub, tmp_path, caplog):
    found = reader(fake_hub, tmp_path)
    fake_hub.credentials = [CODEX | {"quota": CODEX_SEEN}, CLAUDE | {"quota": CLAUDE_SEEN}]
    found.refresh(NOW)
    found._key_path.write_text("wrong")
    with caplog.at_level(logging.ERROR):
        failed = found.refresh(at_minutes(5))
    assert failed["error"] == "the hub answered 401 to /v8/management/credentials" and failed["read_at"] == "2026-11-09T12:00:00Z"
    assert failed["service"] is None and failed["tunnel"] == "active"
    assert "reading the CLIProxyAPI hub failed" in caplog.text
    claude, codex = failed["accounts"]
    assert (codex["week"]["used"], claude["week"]["used"], fable_weeks(claude)) == (40, 70, [("Fable", 38, "2026-11-12T12:00:00Z")])
    # The next read that works clears the failure.
    found._key_path.write_text(KEY)
    assert found.refresh(at_minutes(6))["error"] is None


def test_a_hub_out_of_reach_is_a_warning_until_it_stays_out_of_reach_for_five_minutes(fake_hub, tmp_path, caplog):
    found = reader(fake_hub, tmp_path)
    fake_hub.credentials = [CODEX | {"quota": CODEX_SEEN}, CLAUDE | {"quota": CLAUDE_SEEN}]
    found.refresh(NOW)
    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        nowhere = f"http://127.0.0.1:{closed.getsockname()[1]}"
    found._url = nowhere
    caplog.set_level(logging.WARNING)

    def levels() -> list[str]:
        return [each.levelname for each in caplog.records if each.name == hub.log.name]

    failed = found.refresh(at_minutes(1))
    assert failed["error"].startswith("the hub could not be reached") and len(failed["accounts"]) == 2
    found.refresh(at_minutes(5))
    assert levels() == ["WARNING", "WARNING"]
    caplog.clear()
    found.refresh(at_minutes(6))
    assert levels() == ["ERROR"] and "failed for 5 minutes" in caplog.text
    # A read that works starts the count again.
    found._url = fake_hub.url
    found.refresh(at_minutes(7))
    found._url = nowhere
    caplog.clear()
    found.refresh(at_minutes(8))
    assert levels() == ["WARNING"]


def test_the_week_left_for_opus_is_the_half_of_the_week_fables_allowance_leaves(fake_hub, tmp_path):
    fake_hub.credentials = [CODEX | {"quota": CODEX_SEEN}, CLAUDE | {"quota": CLAUDE_SEEN}]
    claude, codex = reader(fake_hub, tmp_path).refresh(NOW)["accounts"]
    # The week stands at 70% and Fable's allowance, half of the week, at 38%: Fable covers 19% of the week, so the
    # other models used 51%, more than the half that is theirs.
    assert (claude["opus_week"]["used"], claude["opus_week"]["resets_at"]) == (100, claude["week"]["resets_at"])
    assert codex["opus_week"] is None
    lighter = CLAUDE_SEEN["signals"] | {"Anthropic-Ratelimit-Unified-7d-Utilization": "0.4",
                                        "Anthropic-Ratelimit-Unified-7d_oi-Utilization": "0.7"}
    cred = CLAUDE | {"quota": CLAUDE_SEEN | {"signals": lighter}}
    week = hub.account(cred, hub.limits_now(cred, {}, NOW.timestamp()), NOW.timestamp(), "")["opus_week"]
    # A week at 40% with Fable's allowance at 70%: Fable covers 35% of the week, the other models used 5%, a tenth of
    # their half.
    assert (week["key"], week["model"], week["used"], week["captured_at"]) == ("opus_week", "Opus", 10, "2026-11-09T11:00:00Z")
    # Without Fable's limit the week left for Opus is unknown.
    cred = CLAUDE | {"quota": without_fable("2026-11-09T11:30:00Z")}
    assert hub.account(cred, hub.limits_now(cred, {}, NOW.timestamp()), NOW.timestamp(), "")["opus_week"]["used"] is None


def test_a_read_asks_the_hub_about_itself_and_sends_the_providers_nothing(fake_hub, tmp_path):
    fake_hub.credentials = [CODEX | {"quota": CODEX_SEEN}, CLAUDE | {"quota": CLAUDE_SEEN}]
    reader(fake_hub, tmp_path).refresh(NOW)
    assert fake_hub.asked == [
        "/v8/management/credentials", "/v0/management/latest-version", "/v0/management/routing/strategy",
        "/v0/management/api-keys", "/v0/management/request-error-logs",
    ]
    # Nothing but reads: both accounts' limits came with their last requests, so no request for the hub to pass on to
    # a provider.
    assert fake_hub.changes == [] and fake_hub.relayed == []


def test_a_window_the_last_request_did_not_count_against_stays_empty_without_an_earlier_figure():
    week_only = {name: value for name, value in CLAUDE_SEEN["signals"].items() if "-5h-" not in name}
    cred = CLAUDE | {"quota": CLAUDE_SEEN | {"signals": week_only}}
    claude = hub.account(cred, hub.limits_now(cred, {}, NOW.timestamp()), NOW.timestamp(), "")
    assert [(each["key"], each["used"]) for each in claude["windows"]] == [("five_hour", None), ("seven_day", 70)]
    # Codex names a window it does not have with a length of 0 minutes.
    no_second = CODEX_SEEN["signals"] | {"X-Codex-Primary-Window-Minutes": "10080", "X-Codex-Secondary-Window-Minutes": "0"}
    cred = CODEX | {"quota": CODEX_SEEN | {"signals": no_second}}
    codex = hub.account(cred, hub.limits_now(cred, {}, NOW.timestamp()), NOW.timestamp(), "")
    assert [(each["key"], each["used"]) for each in codex["windows"]] == [("five_hour", None), ("seven_day", 25)]


def test_a_claude_accounts_banked_reset_adds_no_room_to_its_week(monkeypatch):
    """70 % of a week with 3 of its 7 days left runs over its pace though the account holds a reset that clears the
    week: spending one is the user's choice."""
    grant = {"label": "Free", "left": 1, "total": 1, "ends_at": None, "usable_now": True, "needs_limit": False,
             "paused": False, "clears": ["seven_day"]}
    monkeypatch.setattr(hub.resets, "known", lambda address: {"read_at": "x", "grants": [grant], "cooldown_until": None})
    cred = CLAUDE | {"email": "me@example.com", "quota": CLAUDE_SEEN}
    claude = hub.account(cred, hub.limits_now(cred, {}, NOW.timestamp()), NOW.timestamp(), "")
    assert (claude["week"]["used"], claude["week"]["elapsed"], claude["week"]["pace"]) == (70, 57, "over")


@pytest.mark.parametrize(("fields", "state"), [
    ({"disabled": True}, "disabled"),
    ({"requires_sign_in": True}, "signed_out"),
    ({"unavailable": True, "next_retry_after": "2026-11-09T13:00:00Z"}, "cooling"),
    ({"quota_blocked": True}, "cooling"),
])
def test_the_hubs_flags_give_the_state(fields, state):
    assert hub.state_of(CODEX | fields)[0] == state


def test_a_hold_on_some_models_alone_leaves_the_account_working():
    # A provider's short rate limit: the hub holds two models on the account and sets quota_blocked, with no time on
    # the account itself. Other models still go to the account, so it works, with the held models named.
    cooldowns = [
        {"scope": "model", "model_key": "gpt-6.2-astra", "reason": "transient_rate_limit", "http_status": 429,
         "retry_at": "2026-11-18T10:20:00.123456789Z"},
        {"scope": "model", "model_key": "gpt-6.2-sol", "reason": "transient_rate_limit", "http_status": 429,
         "retry_at": "2026-11-18T10:15:00.5Z"},
    ]
    held = CODEX | {"quota_blocked": True, "cooldowns": cooldowns}
    assert hub.state_of(held) == ("working", None)
    assert [(each["model"], each["until"], each["limit"]) for each in hub.held_models(held)] == [
        ("gpt-6.2-sol", "2026-11-18T10:15:00.5Z", True), ("gpt-6.2-astra", "2026-11-18T10:20:00.123456789Z", True)]
    # A hold on the account itself rests it, and then no model is named apart.
    account_wide = CODEX | {"quota_blocked": True, "cooldowns": [*cooldowns, {
        "scope": "credential", "reason": "quota_exhausted", "retry_at": "2026-11-19T04:00:00Z"}]}
    assert hub.state_of(account_wide) == ("cooling", "2026-11-19T04:00:00Z")
    assert hub.held_models(account_wide) == []


def test_an_account_no_request_has_gone_through_has_no_limits_yet(fake_hub, tmp_path):
    # The hub lists a new account with empty headers and no time.
    fake_hub.credentials = [CODEX | {"quota": {"signals": {}}}, CLAUDE]
    claude, codex = reader(fake_hub, tmp_path).refresh(NOW)["accounts"]
    for each in (claude, codex):
        assert each["limits_at"] is None
        assert all(window["used"] is None and window["captured_at"] is None for window in each["windows"])
    # Without a request's word, the plan is the sign-in's.
    assert codex["plan"] == "plus"
    # Asked through the hub, the providers did not give them either, and the page says so.
    assert [each["authIndex"] for each in fake_hub.relayed] == ["1", "2"]
    assert codex["limits_error"] == "Codex answered 404 when asked for its limits"


# The limits Cockpit keeps of each account, and its one check of an account it knows no limits of.

def fable_said(observed_at: str, utilization: str) -> dict[str, Any]:
    """What the hub keeps of a Claude request whose headers give Fable's weekly limit as `utilization`."""
    return {"observed_at": observed_at,
            "signals": CLAUDE_SEEN["signals"] | {"Anthropic-Ratelimit-Unified-7d_oi-Utilization": utilization}}


def test_of_the_records_that_name_a_model_limit_the_newest_counts():
    older, newer = fable_said("2026-11-09T10:00:00Z", "0.25"), fable_said("2026-11-09T11:30:00Z", "0.5")
    for quota, record in ((older, newer), (newer, older)):
        cred = CLAUDE | {"quota": quota, "model_quotas": {"claude-fable-5-2": record}}
        fable = hub.limits_now(cred, {}, NOW.timestamp())["models"][hub.FABLE]
        assert (fable["used_percentage"], fable["captured_at"]) == (50.0, "2026-11-09T11:30:00Z")
    # A figure from an earlier read that is newer than both, such as a check's, stays.
    checked = {"used_percentage": 60.0, "resets_at": NOW.timestamp() + 3600, "captured_at": "2026-11-09T11:55:00Z"}
    assert hub.limits_now(cred, {"models": {hub.FABLE: checked}}, NOW.timestamp())["models"] == {hub.FABLE: checked}


def test_a_restart_of_the_hub_leaves_the_limits_cockpit_kept_with_their_age(fake_hub, tmp_path):
    fake_hub.credentials = [CODEX | {"quota": CODEX_SEEN}, CLAUDE | {"quota": CLAUDE_SEEN}]
    reader(fake_hub, tmp_path).refresh(NOW)
    # The restarted hub lists the accounts with empty headers and no time, and Cockpit has restarted too.
    fake_hub.credentials = [CODEX | {"quota": {"signals": {}}}, CLAUDE | {"quota": {"signals": {}}}]
    claude, codex = reader(fake_hub, tmp_path).refresh(at_minutes(1))["accounts"]
    assert [(each["key"], each["used"], each["captured_at"]) for each in codex["windows"]] == [
        ("five_hour", 25, "2026-11-09T11:50:00Z"), ("seven_day", 40, "2026-11-09T11:50:00Z")]
    assert (codex["plan"], codex["limits_at"]) == ("pro", "2026-11-09T11:50:00Z")
    assert [(each["key"], each["used"], each["captured_at"]) for each in claude["windows"]] == [
        ("five_hour", 10, "2026-11-09T11:00:00Z"), ("seven_day", 70, "2026-11-09T11:00:00Z")]
    assert fable_weeks(claude) == [("Fable", 38, "2026-11-12T12:00:00Z")]
    # Cockpit knows their limits, so it asks neither provider.
    assert fake_hub.relayed == [] and (claude["limits_error"], codex["limits_error"]) == ("", "")


def test_the_hubs_figure_replaces_only_the_windows_its_record_names(fake_hub, tmp_path):
    found = reader(fake_hub, tmp_path)
    fake_hub.credentials = [CLAUDE | {"quota": CLAUDE_SEEN}]
    found.refresh(NOW)
    # The next request counted against the week alone.
    week_only = {name: value for name, value in CLAUDE_SEEN["signals"].items() if "-5h-" not in name}
    week_only["Anthropic-Ratelimit-Unified-7d-Utilization"] = "0.75"
    fake_hub.credentials = [CLAUDE | {"quota": {"observed_at": "2026-11-09T11:30:00Z", "signals": week_only}}]
    [claude] = found.refresh(at_minutes(1))["accounts"]
    assert [(each["key"], each["used"], each["captured_at"]) for each in claude["windows"]] == [
        ("five_hour", 10, "2026-11-09T11:00:00Z"), ("seven_day", 75, "2026-11-09T11:30:00Z")]
    assert claude["limits_at"] == "2026-11-09T11:30:00Z"


def test_an_account_cockpit_knows_no_limits_of_is_asked_once_through_the_hub(fake_hub, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    fake_hub.credentials = [CODEX, CLAUDE]
    fake_hub.relay_answers = {"1": (200, CODEX_ANSWER), "2": (200, CLAUDE_ANSWER)}
    found = reader(fake_hub, tmp_path)
    for minutes in (0, 1, 2, 120):
        found.refresh(at_minutes(minutes))
    # A restarted Cockpit keeps the answers' limits and asks no more either.
    claude, codex = reader(fake_hub, tmp_path).refresh(at_minutes(180))["accounts"]
    assert [each["authIndex"] for each in fake_hub.relayed] == ["1", "2"]
    assert claude["limits_at"] == codex["limits_at"] == "2026-11-09T12:00:00Z"
    assert "asked codex through the hub for the limits of the hub account codex-me@example.com-plus.json" in caplog.text
    assert "asked claude through the hub for the limits of the hub account claude-you@example.com.json" in caplog.text


def test_codex_is_asked_as_its_own_client_asks_and_its_answer_gives_the_windows_and_the_plan(fake_hub, tmp_path):
    fake_hub.credentials = [CODEX]
    fake_hub.relay_answers = {"1": (200, CODEX_ANSWER)}
    [codex] = reader(fake_hub, tmp_path).refresh(NOW)["accounts"]
    # The hub puts the account's own token where the request says $TOKEN$.
    assert fake_hub.relayed == [{"authIndex": "1", "method": "GET", "url": "https://chatgpt.com/backend-api/wham/usage",
                                 "header": {"Authorization": "Bearer $TOKEN$", "Content-Type": "application/json",
                                            "User-Agent": hub.CODEX_AGENT, "Chatgpt-Account-Id": "acct-1"}}]
    assert [(each["key"], each["used"], each["captured_at"]) for each in codex["windows"]] == [
        ("five_hour", 0, "2026-11-09T12:00:00Z"), ("seven_day", 3, "2026-11-09T12:00:00Z")]
    assert codex["week"]["resets_at"].startswith("2026-11-10T12:00")
    # The answer's plan is newer than the sign-in's.
    assert (codex["plan"], codex["limits_at"], codex["limits_error"]) == ("pro", "2026-11-09T12:00:00Z", "")


def test_claude_is_asked_as_claude_code_asks_and_its_answer_gives_the_windows_and_the_model_limits(fake_hub, tmp_path):
    fake_hub.credentials = [CLAUDE]
    fake_hub.relay_answers = {"2": (200, CLAUDE_ANSWER)}
    [claude] = reader(fake_hub, tmp_path).refresh(NOW)["accounts"]
    assert fake_hub.relayed == [{"authIndex": "2", "method": "GET", "url": hub.resets.URL, "header": {
        "anthropic-beta": "oauth-2025-04-20", "x-app": "cli", "anthropic-client-platform": "cli",
        "User-Agent": "claude-cli/2.1.10 (external, cli)", "Authorization": "Bearer $TOKEN$"}}]
    # A window the answer gives no use or reset time for stays empty.
    assert [(each["key"], each["used"], each["captured_at"]) for each in claude["windows"]] == [
        ("five_hour", None, None), ("seven_day", 12, "2026-11-09T12:00:00Z")]
    assert fable_weeks(claude) == [("Fable", 5, "2026-11-14T09:00:00Z")]
    assert claude["model_weeks"][0]["captured_at"] == claude["limits_at"] == "2026-11-09T12:00:00Z"


def test_a_refused_check_is_logged_and_asked_again_no_sooner_than_an_hour_later(fake_hub, tmp_path, caplog):
    fake_hub.credentials = [CLAUDE]
    fake_hub.relay_answers = {"2": (429, {"error": {"type": "rate_limit_error", "message": "Rate limited"}})}
    found = reader(fake_hub, tmp_path)
    [claude] = found.refresh(NOW)["accounts"]
    assert len(fake_hub.relayed) == 1
    assert claude["limits_error"] == "Claude answered 429 when asked for its limits"
    assert [each.getMessage() for each in caplog.records if each.levelno == logging.ERROR] == [
        "asking claude through the hub for the limits of the hub account claude-you@example.com.json failed "
        "(status 429): Claude answered 429 when asked for its limits"]
    # Within the hour neither this Cockpit nor a restarted one asks again, and the page still says why.
    found.refresh(at_minutes(30))
    [claude] = reader(fake_hub, tmp_path).refresh(at_minutes(59))["accounts"]
    assert len(fake_hub.relayed) == 1 and claude["limits_error"] == "Claude answered 429 when asked for its limits"
    # An hour after the question, it asks again.
    fake_hub.relay_answers = {"2": (200, CLAUDE_ANSWER)}
    [claude] = reader(fake_hub, tmp_path).refresh(at_minutes(60))["accounts"]
    assert len(fake_hub.relayed) == 2
    assert (claude["limits_error"], claude["limits_at"]) == ("", "2026-11-09T13:00:00Z")


def test_a_check_that_does_not_reach_the_hub_is_a_warning_asked_again_until_five_minutes_pass(
        fake_hub, tmp_path, caplog, monkeypatch):
    fake_hub.credentials = [CLAUDE]
    fake_hub.relay_answers = {"2": (200, CLAUDE_ANSWER)}
    found = reader(fake_hub, tmp_path)
    call = found._call

    def timed_out(method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        if path == hub.RELAY:
            raise hub.HubUnreached("the hub could not be reached: timed out")
        return call(method, path, body)

    def levels() -> list[str]:
        return [each.levelname for each in caplog.records if "limits of the hub account" in each.getMessage()]

    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(found, "_call", timed_out)
    found.refresh(NOW)
    assert levels() == ["WARNING"] and "asks again on its next read" in caplog.text
    # The next read asks again, and a question that reaches the hub ends the count.
    monkeypatch.setattr(found, "_call", call)
    [claude] = found.refresh(at_minutes(1))["accounts"]
    assert len(fake_hub.relayed) == 1 and claude["limits_at"] == "2026-11-09T12:01:00Z"
    # A question kept out of reach for five minutes is an error and waits the hour of a failed check.
    fake_hub.credentials, fake_hub.relay_answers = [CODEX], {}
    monkeypatch.setattr(found, "_call", timed_out)
    caplog.clear()
    for minutes in (2, 3, 7):
        found.refresh(at_minutes(minutes))
    assert levels() == ["WARNING", "WARNING", "ERROR"]
    [codex] = found.refresh(at_minutes(8))["accounts"]
    assert levels() == ["WARNING", "WARNING", "ERROR"]
    assert codex["limits_error"] == "the hub could not be reached: timed out"


def test_a_hub_error_answer_ends_the_count_of_questions_that_missed_the_hub(fake_hub, tmp_path, monkeypatch):
    found = reader(fake_hub, tmp_path)
    answers: list[Exception] = []

    def failing(method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        raise answers.pop(0)

    monkeypatch.setattr(found, "_call", failing)
    answers.append(hub.HubUnreached("the hub could not be reached: timed out"))
    assert found._check(CLAUDE, NOW)[2] is True
    answers.append(hub.HubError("the hub answered 503"))
    assert found._check(CLAUDE, at_minutes(1))[2] is False
    # An hour later, one question that misses the hub is a fresh warning, not the end of five minutes out of reach.
    answers.append(hub.HubUnreached("the hub could not be reached: timed out"))
    assert found._check(CLAUDE, at_minutes(62))[2] is True


def test_a_check_cockpit_stopped_during_counts_as_asked(fake_hub, tmp_path, monkeypatch):
    class StoppedError(Exception):
        pass

    def stop(credential: dict[str, Any], now: datetime) -> None:
        raise StoppedError

    fake_hub.credentials = [CLAUDE]
    found = reader(fake_hub, tmp_path)
    monkeypatch.setattr(found, "_check", stop)
    with pytest.raises(StoppedError):
        found.refresh(NOW)
    # A restarted Cockpit waits the hour from when that question went out.
    [claude] = reader(fake_hub, tmp_path).refresh(at_minutes(1))["accounts"]
    assert fake_hub.relayed == [] and claude["limits_error"] == hub.CHECK_CUT_OFF


def test_an_answered_check_is_kept_when_cockpit_stops_later_in_the_same_read(fake_hub, tmp_path, monkeypatch):
    class StoppedError(Exception):
        pass

    fake_hub.credentials = [CODEX, CLAUDE]
    fake_hub.relay_answers = {"1": (200, CODEX_ANSWER), "2": (200, CLAUDE_ANSWER)}
    found = reader(fake_hub, tmp_path)
    check = found._check

    def stop_at_claude(credential: dict[str, Any], now: datetime) -> Any:
        if credential["provider"] == "claude":
            raise StoppedError
        return check(credential, now)

    monkeypatch.setattr(found, "_check", stop_at_claude)
    with pytest.raises(StoppedError):
        found.refresh(NOW)
    # A restarted Cockpit has Codex's answer and asks neither account.
    claude, codex = reader(fake_hub, tmp_path).refresh(at_minutes(1))["accounts"]
    assert [each["authIndex"] for each in fake_hub.relayed] == ["1"]
    assert (codex["limits_at"], codex["limits_error"]) == ("2026-11-09T12:00:00Z", "")
    assert claude["limits_error"] == hub.CHECK_CUT_OFF


def test_an_account_the_hub_leaves_out_of_a_read_is_not_asked_again_within_the_hour(fake_hub, tmp_path):
    fake_hub.credentials = [CLAUDE]
    fake_hub.relay_answers = {"2": (429, {})}
    found = reader(fake_hub, tmp_path)
    found.refresh(NOW)
    fake_hub.credentials = []
    assert found.refresh(at_minutes(1))["accounts"] == []
    fake_hub.credentials = [CLAUDE]
    [claude] = found.refresh(at_minutes(2))["accounts"]
    assert len(fake_hub.relayed) == 1 and claude["limits_error"] == "Claude answered 429 when asked for its limits"


def test_an_answered_account_that_left_the_hub_and_returned_is_asked_no_sooner(fake_hub, tmp_path):
    fake_hub.credentials = [CODEX]
    fake_hub.relay_answers = {"1": (200, CODEX_ANSWER)}
    found = reader(fake_hub, tmp_path)
    found.refresh(NOW)
    # Away for half an hour, the account loses its kept limits, and its check's record keeps it from a second question.
    fake_hub.credentials = []
    found.refresh(at_minutes(30))
    fake_hub.credentials = [CODEX]
    [codex] = found.refresh(at_minutes(31))["accounts"]
    assert len(fake_hub.relayed) == 1 and (codex["limits_at"], codex["limits_error"]) == (None, "")
    # Once the check is an hour old, the account is asked again and its usage shows.
    [codex] = found.refresh(at_minutes(61))["accounts"]
    assert len(fake_hub.relayed) == 2 and codex["limits_at"]


@pytest.mark.parametrize("fields", [{"disabled": True}, {"requires_sign_in": True}])
def test_an_account_that_cannot_take_requests_is_not_asked(fake_hub, tmp_path, fields):
    fake_hub.credentials = [CODEX | fields, CLAUDE | fields]
    accounts = reader(fake_hub, tmp_path).refresh(NOW)["accounts"]
    assert fake_hub.relayed == [] and [each["limits_error"] for each in accounts] == ["", ""]


def test_a_claude_check_without_claude_code_installed_fails_and_says_so(fake_hub, tmp_path, monkeypatch):
    monkeypatch.setattr(hub.resets, "CLAUDE_COMMAND", tmp_path / "no-claude")
    fake_hub.credentials = [CLAUDE]
    [claude] = reader(fake_hub, tmp_path).refresh(NOW)["accounts"]
    assert fake_hub.relayed == []
    assert claude["limits_error"] == "Cockpit could not make Claude Code's request: FileNotFoundError"


@pytest.mark.parametrize(("provider", "status", "body", "error"), [
    ("claude", 401, "", "Claude answered 401 when asked for its limits"),
    ("claude", 200, "<html>busy</html>", "Claude's answer about its limits was not a JSON object"),
    ("claude", 200, json.dumps(["five_hour"]), "Claude's answer about its limits was not a JSON object"),
    # The words of the answer stay out of the error.
    ("codex", 200, json.dumps({"rate_limit": {"primary_window": {
        "limit_window_seconds": 18000, "used_percent": "a-secret", "reset_at": 1}}}),
     "Codex's answer about its limits has a shape Cockpit cannot read (ValueError)"),
    ("claude", 200, json.dumps({"seven_day": {"utilization": "a-secret", "resets_at": "2026-11-12T04:00:00+00:00"}}),
     "Claude's answer about its limits has a shape Cockpit cannot read (ValueError)"),
    ("claude", 200, json.dumps({"limits": [{"kind": "weekly_scoped", "scope": "a-secret"}]}),
     "Claude's answer about its limits has a shape Cockpit cannot read (ValueError)"),
])
def test_a_check_without_an_answer_cockpit_can_read_fails_with_why(provider, status, body, error):
    assert hub.check_answer(provider, status, body, NOW) == (None, error)


@pytest.mark.parametrize(("provider", "body", "limits"), [
    ("claude", {"five_hour": None, "seven_day": None}, {"windows": {}, "models": {}, "plan": ""}),
    ("codex", {"plan_type": "free", "rate_limit": None}, {"windows": {}, "models": {}, "plan": "free"}),
])
def test_an_answer_that_names_no_limit_window_is_an_answer(provider, body, limits):
    assert hub.check_answer(provider, 200, json.dumps(body), NOW) == (limits, "")


def test_an_answer_without_a_limit_window_is_logged_and_the_account_is_asked_again_an_hour_on(fake_hub, tmp_path, caplog):
    fake_hub.credentials = [CLAUDE]
    fake_hub.relay_answers = {"2": (200, {"five_hour": None, "seven_day": None})}
    found = reader(fake_hub, tmp_path)
    [claude] = found.refresh(NOW)["accounts"]
    assert (claude["limits_at"], claude["limits_error"]) == (None, "")
    assert checks_kept(tmp_path) == {hub.limits_key(CLAUDE): {"at": "2026-11-09T12:00:00Z", "error": "", "week_reset": False}}
    assert [each.getMessage() for each in caplog.records if each.levelno >= logging.WARNING] == [
        "claude answered when asked through the hub for the limits of the hub account claude-you@example.com.json, "
        "and named no limit window; Cockpit asks again in an hour unless a request through the hub brings them"]
    # Within the hour it is not asked; an hour on it is.
    found.refresh(at_minutes(59))
    assert len(fake_hub.relayed) == 1
    [claude] = found.refresh(at_minutes(61))["accounts"]
    assert len(fake_hub.relayed) == 2 and claude["limits_error"] == ""


def test_an_idle_account_is_asked_again_once_the_answers_limits_have_reset(fake_hub, tmp_path):
    fake_hub.credentials = [CODEX, CLAUDE]
    fake_hub.relay_answers = {"1": (200, CODEX_ANSWER), "2": (200, CLAUDE_ANSWER)}
    found = reader(fake_hub, tmp_path)
    found.refresh(NOW)
    # While the answers' limits stand, neither account is asked again (Codex's week resets a day on).
    found.refresh(at_minutes(23 * 60))
    assert [each["authIndex"] for each in fake_hub.relayed] == ["1", "2"]
    # Eight days on, every limit the answers gave has reset with no request since: a restarted Cockpit asks both
    # once, and their usage shows again.
    later = int(NOW.timestamp()) + 9 * 86400
    fake_hub.relay_answers = {
        "1": (200, {"plan_type": "pro", "rate_limit": {"secondary_window": {
            "used_percent": 0, "limit_window_seconds": 604800, "reset_at": later}}}),
        "2": (200, CLAUDE_ANSWER | {"seven_day": {"utilization": 0.0, "resets_at": "2026-11-18T09:00:00+00:00"}}),
    }
    found = reader(fake_hub, tmp_path)
    found.refresh(at_minutes(8 * 24 * 60))
    accounts = found.refresh(at_minutes(8 * 24 * 60 + 2))["accounts"]
    assert [each["authIndex"] for each in fake_hub.relayed] == ["1", "2", "1", "2"]
    assert all(each["limits_at"] for each in accounts)


def test_a_read_of_the_hub_that_fails_asks_no_provider(fake_hub, tmp_path):
    fake_hub.credentials = [CLAUDE | {"quota": CLAUDE_SEEN}]
    found = reader(fake_hub, tmp_path)
    found.refresh(NOW)
    # Eight days on, the limits of the account's last request have reset, and the hub refuses the read.
    found._key_path.write_text("wrong")
    failed = found.refresh(at_minutes(8 * 24 * 60))
    assert failed["error"] == "the hub answered 401 to /v8/management/credentials"
    assert fake_hub.relayed == [] and checks_kept(tmp_path) is None
    # The next read the hub answers asks.
    found._key_path.write_text(KEY)
    found.refresh(at_minutes(8 * 24 * 60 + 1))
    assert len(fake_hub.relayed) == 1


def test_cockpit_checks_a_working_account_without_a_limit_window_at_most_hourly():
    none = {"windows": {}, "models": {}, "plan": ""}
    at = NOW.timestamp()
    answered = {"at": "2026-11-09T12:00:00Z", "error": ""}
    failed = {"at": "2026-11-09T12:00:00Z", "error": "Claude answered 429 when asked for its limits"}
    assert hub.needs_check(CLAUDE, none, None, at) and hub.needs_check(CODEX, none, None, at)
    # An account left without a window after an answer is asked again once the hour is over, as after a failure.
    assert not hub.needs_check(CLAUDE, none, answered, at + hub.CHECK_RETRY_SECONDS - 1)
    assert hub.needs_check(CLAUDE, none, answered, at + hub.CHECK_RETRY_SECONDS)
    assert not hub.needs_check(CLAUDE, none, failed, at + hub.CHECK_RETRY_SECONDS - 1)
    assert hub.needs_check(CLAUDE, none, failed, at + hub.CHECK_RETRY_SECONDS)
    # Disabled or signed-out accounts, other providers, and working accounts with a window are not asked.
    for fields in ({"disabled": True}, {"requires_sign_in": True}, {"provider": "gemini"}):
        assert not hub.needs_check(CLAUDE | fields, none, None, at)
    seen = CLAUDE | {"quota": CLAUDE_SEEN}
    assert not hub.needs_check(seen, hub.limits_now(seen, {}, at), None, at)
    # Nor while a model's own limit is current.
    model = {"used_percentage": 100, "resets_at": at + 3600, "captured_at": at}
    assert not hub.needs_check(CLAUDE, none | {"models": {"fable": model}}, answered, at + hub.CHECK_RETRY_SECONDS)


def test_a_wrong_key_shows_the_hubs_refusal_and_no_accounts(fake_hub, tmp_path, caplog):
    fake_hub.credentials = [CODEX]
    found = reader(fake_hub, tmp_path, key="wrong").refresh(NOW)
    assert found == {"accounts": [], "error": "the hub answered 401 to /v8/management/credentials",
                     "read_at": "2026-11-09T12:00:00Z", "service": None, "tunnel": "active"}
    assert "wrong" not in caplog.text and KEY not in caplog.text


def test_an_unreachable_hub_says_so_with_the_tunnels_state(tmp_path):
    path = tmp_path / "key"
    path.write_text(KEY)
    found = hub.Hub(NOWHERE, path, {"codex": ("true",)}, ("echo", "failed"), {}, database(tmp_path), NO_ACTION).refresh(NOW)
    assert found["accounts"] == [] and found["error"].startswith("the hub could not be reached")
    assert found["service"] is None and found["tunnel"] == "failed"


def test_a_tunnel_state_that_cannot_be_asked_is_unknown(fake_hub, tmp_path, caplog):
    path = tmp_path / "key"
    path.write_text(KEY)
    found = hub.Hub(fake_hub.url, path, {"codex": ("true",)}, (str(tmp_path / "no-such-command"),), {},
                    database(tmp_path), NO_ACTION).refresh(NOW)
    assert found["tunnel"] == "unknown" and found["error"] is None
    assert "state of the tunnel" in caplog.text


def test_a_provider_error_shows_as_its_sentence():
    refusal = ('{"type":"error","error":{"type":"rate_limit_error","message":"This request would exceed your '
               'account\'s rate limit. Please try again later."},"request_id":"req_1"}')
    assert hub.state_words(refusal) == "This request would exceed your account's rate limit. Please try again later."
    for other in ("", "rate limited by the provider", '{"error":"unauthorized"}', '{"error":{"message":""}}', "[1]", "429"):
        assert hub.state_words(other) == other


def test_each_account_shows_its_requests_by_span_and_its_token(fake_hub, tmp_path):
    spans = [{"time": "23:50-00:00", "success": 4, "failed": 1}, {"time": "00:00-00:10", "success": 2, "failed": 0}]
    fake_hub.credentials = [CODEX | {
        "success": 113, "failed": 2, "recent_requests": spans, "status_message": "rate limited by the provider",
        "access_token_expires_at": "2026-11-10T13:14:56Z",
    }]
    just_past_midnight = datetime(2026, 11, 10, 0, 4, tzinfo=UTC)
    [codex] = reader(fake_hub, tmp_path).refresh(just_past_midnight)["accounts"]
    assert (codex["id"], codex["index"]) == ("codex-me@example.com-plus.json", "1")
    assert (codex["succeeded"], codex["failed"], codex["said"]) == (113, 2, "rate limited by the provider")
    assert codex["token_expires_at"] == "2026-11-10T13:14:56Z"
    # The span that began before midnight belongs to the day before.
    assert codex["traffic"] == [
        {"at": "2026-11-09T23:50:00Z", "succeeded": 4, "failed": 1},
        {"at": "2026-11-10T00:00:00Z", "succeeded": 2, "failed": 0},
    ]


def test_the_hubs_own_state_is_read_with_the_accounts(fake_hub, tmp_path):
    fake_hub.routing = "fill-first"
    fake_hub.error_logs = {
        "error-oldest.log": (NOW.timestamp() - 180, error_log("HEAD", "/api/hello", 404)),
        "error-older.log": (NOW.timestamp() - 120, error_log("POST", "/v1/messages?beta=true", 502)),
        "error-newer.log": (NOW.timestamp() - 60, error_log("HEAD", "/api/hello", 404)),
    }
    found = reader(fake_hub, tmp_path)
    service = found.refresh(NOW)["service"]
    # The same request failing the same way is one line, dated by its last time.
    assert service == {
        "version": "8.0.53", "latest_version": "8.0.63", "routing": "fill-first", "client_keys": 2, "error": None,
        "failed_requests": {"count": 3, "shown": [
            {"method": "HEAD", "path": "/api/hello", "status": 404, "at": "2026-11-09T11:59:00Z", "times": 2},
            {"method": "POST", "path": "/v1/messages", "status": 502, "at": "2026-11-09T11:58:00Z", "times": 1},
        ]},
    }
    # The caller's key and what it asked stay in the hub's log.
    assert "caller-key-in-the-log" not in str(service) and "what the caller asked" not in str(service)
    # A second read asks for neither the newest release nor a log it has read.
    asked = len(fake_hub.asked)
    found.refresh(NOW)
    again = fake_hub.asked[asked:]
    assert "/v0/management/latest-version" not in again
    assert not [path for path in again if path.startswith("/v0/management/request-error-logs/")]


def test_only_the_newest_failed_requests_are_shown(fake_hub, tmp_path):
    fake_hub.error_logs = {f"error-{number}.log": (NOW.timestamp() - number, error_log("GET", f"/v1/{number}", 500))
                           for number in range(hub.FAILED_SHOWN + 3)}
    failed = reader(fake_hub, tmp_path).refresh(NOW)["service"]["failed_requests"]
    assert failed["count"] == hub.FAILED_SHOWN + 3
    assert [each["path"] for each in failed["shown"]] == [f"/v1/{number}" for number in range(hub.FAILED_SHOWN)]


def test_a_failed_requests_file_the_hub_deleted_after_listing_it_is_left_out(fake_hub, tmp_path):
    fake_hub.error_logs = {
        "error-older.log": (NOW.timestamp() - 120, error_log("POST", "/v1/messages", 502)),
        "error-newer.log": (NOW.timestamp() - 60, error_log("HEAD", "/api/hello", 404)),
    }
    fake_hub.gone = {"/v0/management/request-error-logs/error-newer.log"}
    service = reader(fake_hub, tmp_path).refresh(NOW)["service"]
    # The rest of the hub's own state is still read.
    assert (service["error"], service["routing"], service["client_keys"]) == (None, fake_hub.routing, 2)
    assert service["failed_requests"] == {"count": 2, "shown": [
        {"method": "POST", "path": "/v1/messages", "status": 502, "at": "2026-11-09T11:58:00Z", "times": 1},
    ]}


def test_a_hub_that_will_not_tell_its_own_state_still_shows_its_accounts(fake_hub, tmp_path, caplog):
    fake_hub.credentials = [CODEX]
    fake_hub.gone = {"/v0/management/api-keys"}
    read = reader(fake_hub, tmp_path).refresh(NOW)
    assert len(read["accounts"]) == 1 and read["error"] is None
    assert read["service"] == {"version": "8.0.53", "latest_version": None, "routing": None, "client_keys": None,
                               "failed_requests": None, "error": "the hub answered 404 to /v0/management/api-keys"}
    assert "own state failed" in caplog.text


def test_the_page_gets_the_last_read_and_a_stale_one_starts_the_next(fake_hub, tmp_path):
    fake_hub.credentials = [CODEX]
    found = reader(fake_hub, tmp_path)
    first = found.read(NOW, True)
    assert first["accounts"] is None
    assert len(until(lambda: found.read(NOW, True)["accounts"])) == 1
    fake_hub.credentials = [CODEX, CLAUDE]
    # Within the minute the page keeps the read it has; after it, the next read brings the new account.
    assert len(found.read(NOW, True)["accounts"]) == 1
    later = datetime.fromtimestamp(NOW.timestamp() + hub.REFRESH_SECONDS, UTC)
    until(lambda: len(found.read(later, True)["accounts"]) == 2)


def test_the_first_read_shows_its_accounts_before_the_hubs_own_state(fake_hub, tmp_path, monkeypatch):
    fake_hub.credentials = [CODEX]
    found = reader(fake_hub, tmp_path)
    service, told = found._service, threading.Event()
    monkeypatch.setattr(found, "_service", lambda now: told.wait(10) and service(now))
    assert found.read(NOW, True)["accounts"] is None
    # The hub has listed its accounts and not yet told its own state.
    early = until(lambda: found.read(NOW, True)["accounts"] and found.read(NOW, True))
    assert len(early["accounts"]) == 1 and early["service"] is None and early["error"] is None
    told.set()
    assert until(lambda: found.read(NOW, True)["service"])["version"] == "8.0.53"


@pytest.fixture
def hub_world(fake_hub, tmp_path, monkeypatch, request):
    """The app on a machine with the stand-in hub and the stand-in device login."""
    path = tmp_path / "hub-key"
    path.write_text(KEY)
    use_hub(monkeypatch, config.HubSetup(
        url=fake_hub.url,
        key_file=path,
        sign_in={
            "codex": (sys.executable, str(FAKE_LOGIN), str(tmp_path)),
            "claude": (sys.executable, str(FAKE_CLAUDE_LOGIN), str(tmp_path)),
        },
        # Each stand-in restart leaves a file by its name; the tunnel's fails with SSH's kind of last line.
        restarts={
            "hub": ("touch", str(tmp_path / "restarted-hub")),
            "tunnel": ("sh", "-c", "echo 'Warning: something first' >&2; echo 'Unit hub-tunnel.service not found.' >&2; exit 5"),
        },
        tunnel_state=TUNNEL_ACTIVE,
    ))
    world = request.getfixturevalue("world")
    world.folder = tmp_path
    return world


def hub_state(world, headers) -> dict[str, Any]:
    return world.client.get("/api/state", headers=headers).json()["hub"]


def test_a_machine_without_a_hub_shows_none(world):
    lose_the_hub_key()
    assert world.client.get("/api/state", headers=world.user()).json()["hub"] is None


def test_the_usage_page_gets_the_hubs_accounts_as_the_state_has_them(hub_world, fake_hub):
    world = hub_world
    fake_hub.credentials = [CODEX]
    until(lambda: (read := hub_state(world, world.user()))["accounts"] and read["service"])
    assert world.client.get("/api/hub", headers=world.user()).json()["hub"] == hub_state(world, world.user())


def test_the_usage_page_on_a_machine_without_a_hub_gets_none(world):
    lose_the_hub_key()
    assert world.client.get("/api/hub", headers=world.user()).json() == {"hub": None}


def test_signing_in_shows_the_link_and_code_and_the_new_account(hub_world, fake_hub, caplog):
    caplog.set_level(logging.INFO)
    world = hub_world
    assert world.client.post("/api/hub/codex/sign-in", headers=world.user()).status_code == 200
    waiting = hub_state(world, world.user())["sign_in"]["codex"]["waiting"]
    assert waiting["url"] == "https://auth.openai.com/codex/device" and waiting["code"] == "WXYZ-1234"
    # A second press keeps the login that waits.
    assert world.client.post("/api/hub/codex/sign-in", headers=world.user()).status_code == 200
    fake_hub.credentials = [CODEX]
    (world.folder / "finish").write_text("")
    done = until(lambda: (found := hub_state(world, world.user()))["sign_in"]["codex"]["finished_at"] and found)
    assert done["sign_in"]["codex"]["waiting"] is None and done["sign_in"]["codex"]["error"] == ""
    until(lambda: [each["email"] for each in hub_state(world, world.user())["accounts"] or []] == ["me@example.com"])
    assert "WXYZ-1234" not in caplog.text and KEY not in caplog.text


def test_agents_get_no_code_and_cannot_start_a_sign_in(hub_world):
    world = hub_world
    assert world.client.post("/api/hub/codex/sign-in", headers=world.agent()).status_code == 403
    world.client.post("/api/hub/codex/sign-in", headers=world.user())
    assert set(hub_state(world, world.agent())["sign_in"]["codex"]["waiting"]) == {"ends_at"}
    (world.folder / "finish").write_text("")
    until(lambda: hub_state(world, world.user())["sign_in"]["codex"]["finished_at"])


def test_a_failed_sign_in_reports_the_hubs_words_without_the_code(hub_world):
    world = hub_world
    world.client.post("/api/hub/codex/sign-in", headers=world.user())
    (world.folder / "fail").write_text("")
    error = until(lambda: hub_state(world, world.user())["sign_in"]["codex"]["error"])
    assert "expired" in error and "WXYZ-1234" not in error
    assert hub_state(world, world.agent())["sign_in"]["codex"]["error"] == ""


def test_no_link_is_refused_with_the_hubs_words(hub_world):
    world = hub_world
    (world.folder / "no-link").write_text("")
    response = world.client.post("/api/hub/codex/sign-in", headers=world.user())
    assert response.status_code == 400
    assert "403" in response.json()["detail"]


CLAUDE_STATE = "state-0123456789abcdefghij"


def callback(code: str, state: str = CLAUDE_STATE) -> str:
    return f"http://localhost:54545/callback?code={code}&state={state}"


@pytest.mark.parametrize(("address", "parts"), [
    (callback("abc"), ("abc", CLAUDE_STATE, "")),
    (f"?code=abc&state={CLAUDE_STATE}", ("abc", CLAUDE_STATE, "")),
    (f"http://localhost:54545/callback#code=abc&state={CLAUDE_STATE}", ("abc", CLAUDE_STATE, "")),
    (f"abc#{CLAUDE_STATE}", ("abc", CLAUDE_STATE, "")),
    ("http://localhost:54545/callback?error=access_denied&error_description=You+said+no&state=s", ("", "s", "You said no")),
    ("http://localhost:54545/callback", ("", "", "")),
])
def test_the_pasted_address_gives_its_code_state_and_error(address, parts):
    assert hub.callback_parts(address) == parts


@pytest.mark.parametrize(("text", "shown"), [
    ("error=temporarily_unavailable", "error=temporarily_unavailable"),
    ("bad code 4/0ExampleExampleExampleExa", "bad code …"),
    ("oauth_token=abcdefghij_klmnopqrstuv", "…"),
])
def test_an_error_name_shows_and_a_long_run_that_may_be_a_credential_does_not(text, shown):
    assert hub.hide_long_runs(text) == shown


def test_claude_signs_in_with_the_address_its_page_sent_the_browser_to(hub_world, fake_hub, caplog):
    caplog.set_level(logging.INFO)
    world = hub_world
    response = world.client.post("/api/hub/claude/sign-in", headers=world.user())
    assert response.status_code == 200 and response.json()["url"].startswith("https://claude.ai/oauth/authorize?")
    waiting = hub_state(world, world.user())["sign_in"]["claude"]["waiting"]
    assert waiting["url"] == response.json()["url"] and waiting["checking"] is False
    assert set(hub_state(world, world.agent())["sign_in"]["claude"]["waiting"]) == {"ends_at"}
    # An agent cannot finish it, and an address from another sign-in or without a code never reaches the login.
    assert world.client.post("/api/hub/claude/callback", json={"address": callback("good-code")},
                             headers=world.agent()).status_code == 403
    refused = world.client.post("/api/hub/claude/callback", json={"address": callback("good-code", "other")},
                                headers=world.user())
    assert refused.status_code == 400 and "another sign-in" in refused.json()["detail"]
    refused = world.client.post("/api/hub/claude/callback", json={"address": "http://localhost:54545/callback"},
                                headers=world.user())
    assert refused.status_code == 400 and "no sign-in code" in refused.json()["detail"]
    assert hub_state(world, world.user())["sign_in"]["claude"]["waiting"] is not None
    fake_hub.credentials = [CLAUDE]
    secret = "good-code-0123456789abcdefghij"
    assert world.client.post("/api/hub/claude/callback", json={"address": callback(secret)},
                             headers=world.user()).status_code == 200
    found = hub_state(world, world.user())["sign_in"]["claude"]
    assert found["waiting"] is None and found["error"] == "" and found["finished_at"]
    until(lambda: [each["email"] for each in hub_state(world, world.user())["accounts"] or []] == ["you@example.com"])
    assert secret not in caplog.text and CLAUDE_STATE not in caplog.text


def test_a_refused_claude_address_reports_the_hubs_words_without_the_code(hub_world):
    world = hub_world
    world.client.post("/api/hub/claude/sign-in", headers=world.user())
    secret = "refused-0123456789abcdefghij"
    response = world.client.post("/api/hub/claude/callback", json={"address": callback(secret)}, headers=world.user())
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "invalid_grant" in detail and secret not in detail and CLAUDE_STATE not in detail
    found = hub_state(world, world.user())["sign_in"]["claude"]
    assert found["waiting"] is None and "invalid_grant" in found["error"] and secret not in found["error"]


def test_a_claude_sign_in_whose_account_the_hub_could_not_save_is_no_sign_in(hub_world):
    world = hub_world
    world.client.post("/api/hub/claude/sign-in", headers=world.user())
    response = world.client.post("/api/hub/claude/callback", json={"address": callback("unsaved-code")},
                                 headers=world.user())
    assert response.status_code == 400 and "failed to save auth" in response.json()["detail"]
    assert not hub_state(world, world.user())["sign_in"]["claude"]["finished_at"]


def test_an_address_with_no_claude_sign_in_waiting_is_refused(hub_world):
    world = hub_world
    response = world.client.post("/api/hub/claude/callback", json={"address": callback("abc")}, headers=world.user())
    assert response.status_code == 400 and "no Claude sign-in waits" in response.json()["detail"]


def test_an_unknown_provider_has_no_sign_in(hub_world):
    world = hub_world
    for path in ("/api/hub/antigravity/sign-in", "/api/hub/antigravity/callback"):
        assert world.client.post(path, json={"address": "x"}, headers=world.user()).status_code == 404


# The hub's part of the Usage page and its controls (SPEC UI-85).

COOLING = {"unavailable": True, "next_retry_after": "2026-11-09T13:00:00Z"}


def hub_page_read(world, headers) -> dict[str, Any]:
    return world.client.get("/api/hub", headers=headers).json()["hub"]


def with_both_accounts(world, fake_hub) -> None:
    """The page has read the hub, its own state too, with its Codex account resting and its Claude account working."""
    fake_hub.credentials = [CODEX | COOLING, CLAUDE]
    until(lambda: (read := hub_page_read(world, world.user()))["accounts"] and read["service"])


def states(read: dict[str, Any]) -> dict[str, str]:
    return {each["provider"]: each["state"] for each in read["accounts"]}


def test_the_hub_address_opens_the_usage_page_which_reads_the_hub(hub_world, fake_hub):
    world = hub_world
    moved = world.client.get("/hub", headers=world.user(), follow_redirects=False)
    assert moved.status_code == 308 and moved.headers["location"] == "/usage"
    assert world.client.get("/usage", headers=world.user()).status_code == 200
    with_both_accounts(world, fake_hub)
    read = hub_page_read(world, world.user())
    assert states(read) == {"codex": "cooling", "claude": "working"}
    assert read["tunnel"] == "active" and read["service"]["routing"] == "round-robin"
    assert set(read["sign_in"]) == {"codex", "claude"}
    assert hub_page_read(world, world.agent())["service"]["version"] == "8.0.53"


def test_a_machine_without_a_hub_has_an_empty_hub_page(world):
    lose_the_hub_key()
    assert world.client.get("/api/hub", headers=world.user()).json() == {"hub": None}


def test_turning_an_account_off_and_on_changes_the_hub_and_the_page_at_once(hub_world, fake_hub, caplog):
    caplog.set_level(logging.INFO)
    world = hub_world
    with_both_accounts(world, fake_hub)
    off = world.client.post("/api/hub/accounts/disabled", json={"id": CLAUDE["id"], "disabled": True}, headers=world.user())
    assert off.status_code == 200 and states(off.json()["hub"])["claude"] == "disabled"
    assert fake_hub.changes == [("PATCH", "/v0/management/auth-files/status", {"name": CLAUDE["id"], "disabled": True})]
    # The page's next poll shows the same read, with no minute's wait.
    assert states(hub_page_read(world, world.user()))["claude"] == "disabled"
    on = world.client.post("/api/hub/accounts/disabled", json={"id": CLAUDE["id"], "disabled": False}, headers=world.user())
    assert states(on.json()["hub"])["claude"] == "working"
    assert "turning off the claude account you@example.com" in caplog.text


def test_ending_a_rest_gives_the_account_requests_again(hub_world, fake_hub):
    world = hub_world
    with_both_accounts(world, fake_hub)
    ended = world.client.post("/api/hub/accounts/end-rest", json={"id": CODEX["id"]}, headers=world.user())
    assert ended.status_code == 200 and states(ended.json()["hub"])["codex"] == "working"
    assert fake_hub.changes == [("POST", "/v8/management/routing/cooldown/reset", {"auth_index": "1"})]


def test_signing_an_account_out_removes_it_from_the_hub(hub_world, fake_hub):
    world = hub_world
    with_both_accounts(world, fake_hub)
    out = world.client.post("/api/hub/accounts/sign-out", json={"id": CODEX["id"]}, headers=world.user())
    assert out.status_code == 200 and states(out.json()["hub"]) == {"claude": "working"}
    assert fake_hub.changes == [("DELETE", "/v0/management/auth-files", CODEX["id"])]


def test_the_routing_is_one_of_the_hubs_three(hub_world, fake_hub):
    world = hub_world
    with_both_accounts(world, fake_hub)
    chosen = world.client.post("/api/hub/routing", json={"routing": "fill-first"}, headers=world.user())
    assert chosen.status_code == 200 and chosen.json()["hub"]["service"]["routing"] == "fill-first"
    assert fake_hub.changes == [("PUT", "/v0/management/routing/strategy", {"value": "fill-first"})]
    assert world.client.post("/api/hub/routing", json={"routing": "random"}, headers=world.user()).status_code == 400
    assert len(fake_hub.changes) == 1


def test_an_account_the_hub_does_not_have_is_not_found_and_nothing_changes(hub_world, fake_hub):
    world = hub_world
    with_both_accounts(world, fake_hub)
    for path, body in (("disabled", {"id": "gone.json", "disabled": True}), ("sign-out", {"id": "gone.json"}),
                       ("end-rest", {"id": "gone.json"})):
        response = world.client.post(f"/api/hub/accounts/{path}", json=body, headers=world.user())
        assert response.status_code == 404 and "gone.json" in response.json()["detail"]
    assert fake_hub.changes == []


def test_a_change_the_hub_refuses_says_so(hub_world, fake_hub, caplog):
    world = hub_world
    with_both_accounts(world, fake_hub)
    # The hub lost the account since the page's read.
    fake_hub.credentials = [CLAUDE]
    response = world.client.post("/api/hub/accounts/disabled", json={"id": CODEX["id"], "disabled": True}, headers=world.user())
    assert response.status_code == 400
    assert response.json()["detail"] == ("turning off the codex account me@example.com failed: "
                                         "the hub answered 404 to /v0/management/auth-files/status")
    assert "failed" in caplog.text and KEY not in caplog.text


def test_restarting_the_hub_runs_its_command_and_reads_the_hub_again(hub_world, fake_hub, tmp_path):
    world = hub_world
    with_both_accounts(world, fake_hub)
    fake_hub.credentials = [CLAUDE]
    restarted = world.client.post("/api/hub/restart", json={"what": "hub"}, headers=world.user())
    assert restarted.status_code == 200 and states(restarted.json()["hub"]) == {"claude": "working"}
    assert (tmp_path / "restarted-hub").exists()


def test_a_restart_that_fails_says_the_commands_last_line(hub_world, fake_hub):
    world = hub_world
    with_both_accounts(world, fake_hub)
    response = world.client.post("/api/hub/restart", json={"what": "tunnel"}, headers=world.user())
    assert response.status_code == 400
    assert response.json()["detail"] == "restarting the tunnel failed: Unit hub-tunnel.service not found."
    assert world.client.post("/api/hub/restart", json={"what": "machine"}, headers=world.user()).status_code == 400


def test_agents_cannot_use_the_hubs_controls(hub_world, fake_hub, tmp_path):
    world = hub_world
    with_both_accounts(world, fake_hub)
    for path, body in (
        ("accounts/disabled", {"id": CLAUDE["id"], "disabled": True}), ("accounts/sign-out", {"id": CLAUDE["id"]}),
        ("accounts/end-rest", {"id": CODEX["id"]}), ("routing", {"routing": "fill-first"}), ("restart", {"what": "hub"}),
    ):
        assert world.client.post(f"/api/hub/{path}", json=body, headers=world.agent()).status_code == 403
    assert fake_hub.changes == [] and not (tmp_path / "restarted-hub").exists()


def test_a_control_pressed_while_a_sign_in_waits_leaves_its_code_out_of_the_table_of_past_presses(hub_world, fake_hub):
    world = hub_world
    with_both_accounts(world, fake_hub)
    world.client.post("/api/hub/codex/sign-in", headers={**world.user(), "x-cockpit-action": "sign-in-press-1"})
    assert hub_page_read(world, world.user())["sign_in"]["codex"]["waiting"]["code"] == "WXYZ-1234"
    presses = (
        ("accounts/disabled", {"id": CLAUDE["id"], "disabled": True}), ("accounts/end-rest", {"id": CODEX["id"]}),
        ("routing", {"routing": "fill-first"}), ("restart", {"what": "hub"}), ("accounts/sign-out", {"id": CODEX["id"]}),
    )
    for n, (path, body) in enumerate(presses):
        answer = world.client.post(f"/api/hub/{path}", json=body, headers={**world.user(), "x-cockpit-action": f"hub-press-{n}"})
        assert answer.status_code == 200 and "sign_in" not in answer.json()["hub"] and "WXYZ-1234" not in answer.text
    conn = db.connect(world.db_path)
    try:
        kept = json.dumps([[str(value) for value in row] for row in conn.execute("SELECT * FROM page_actions")])
    finally:
        conn.close()
    assert all(f"hub-press-{n}" in kept for n in range(len(presses))) and "sign-in-press-1" in kept
    assert "WXYZ-1234" not in kept
    # The page's own read still gives the code.
    assert hub_page_read(world, world.user())["sign_in"]["codex"]["waiting"]["code"] == "WXYZ-1234"
    (world.folder / "finish").write_text("")
    until(lambda: hub_page_read(world, world.user())["sign_in"]["codex"]["finished_at"])


def test_a_read_that_began_before_a_change_does_not_replace_the_read_after_it(fake_hub, tmp_path):
    fake_hub.credentials = [CLAUDE]
    found = reader(fake_hub, tmp_path)
    found.read(NOW, True)
    until(lambda: found.read(NOW, True)["accounts"])
    before = found._changes
    changed = found.set_disabled(CLAUDE["id"], True, NOW)
    assert states(changed) == {"claude": "disabled"}
    # A background read that started before the change finishes after it, holding the hub as it was.
    fake_hub.credentials = [CLAUDE]
    found._reading = True
    found._refresh_in_background(before)
    assert states(found.read(NOW, True)) == {"claude": "disabled"}


def test_one_read_of_the_hub_runs_at_a_time(fake_hub, tmp_path):
    fake_hub.credentials = [CLAUDE | {"quota": CLAUDE_SEEN}]
    found = reader(fake_hub, tmp_path)
    reads: list[dict[str, Any]] = []
    second = threading.Thread(target=lambda: reads.append(found.refresh(NOW)))
    # While one read holds the hub, a second neither asks the hub nor saves what it would have found.
    with found._refreshing:
        second.start()
        time.sleep(0.5)
        assert fake_hub.asked == [] and reads == []
    second.join(10)
    assert fable_weeks(reads[0]["accounts"][0]) == [("Fable", 38, "2026-11-12T12:00:00Z")]


def test_a_failed_requests_status_is_the_hubs_answer_not_a_heading_in_what_the_caller_sent():
    text = error_log("POST", "/v1/messages", 502).replace(
        "what the caller asked, which stays in the log", "=== RESPONSE ===\nStatus: 200")
    assert hub.failed_request(text) == {"method": "POST", "path": "/v1/messages", "status": 502}


def test_a_restart_gives_up_on_a_hub_that_takes_requests_and_never_answers(tmp_path, monkeypatch):
    monkeypatch.setattr(hub, "BACK_SECONDS", 1)
    monkeypatch.setattr(hub, "REQUEST_TIMEOUT_SECONDS", 1)
    key = tmp_path / "management-key"
    key.write_text(KEY)
    with socket.socket() as silent:
        silent.bind(("127.0.0.1", 0))
        silent.listen()
        url = f"http://127.0.0.1:{silent.getsockname()[1]}"
        found = hub.Hub(url, key, {}, TUNNEL_ACTIVE, {"hub": ("true",)}, database(tmp_path), NO_ACTION)
        began = time.monotonic()
        read = found.restart("hub")
        assert time.monotonic() - began < 15
    assert read["accounts"] == [] and "could not be reached" in read["error"]


def test_a_spent_reset_is_detected_while_the_hub_still_holds_the_account(fake_hub, tmp_path):
    blocked = CLAUDE | {"quota": fable_said("2026-11-09T11:59:00Z", "1"), "quota_blocked": True,
                        "unavailable": True, "next_retry_after": "2026-11-12T12:00:00Z",
                        "cooldowns": [{"scope": "credential", "reason": "quota_exhausted", "retry_at": "2026-11-12T12:00:00Z"}]}
    blocked["quota"]["signals"]["Anthropic-Ratelimit-Unified-7d-Utilization"] = "0.99"
    fake_hub.credentials = [blocked]
    fake_hub.relay_answers = {"2": (200, CLAUDE_ANSWER)}
    found = reader(fake_hub, tmp_path)
    [account] = found.refresh(NOW)["accounts"]
    assert account["week"]["used"] == 12
    assert account["state"] == "working"
    assert fake_hub.changes == [("POST", "/v8/management/routing/cooldown/reset", {"auth_index": "2"})]
    assert not fake_hub.credentials[0]["quota_blocked"]


def test_a_genuinely_exhausted_account_stays_held_and_is_rechecked_after_five_minutes(fake_hub, tmp_path):
    fake_hub.credentials = [CLAUDE | {"quota": CLAUDE_SEEN, "quota_blocked": True, "unavailable": True}]
    fake_hub.relay_answers = {"2": (200, CLAUDE_ANSWER | {"seven_day": {"utilization": 100, "resets_at": "2026-11-12T12:00:00Z"}})}
    found = reader(fake_hub, tmp_path)
    found.refresh(NOW)
    reader(fake_hub, tmp_path).refresh(at_minutes(4))
    assert len(fake_hub.relayed) == 1 and fake_hub.changes == []
    fake_hub.relay_answers = {"2": (200, CLAUDE_ANSWER)}
    [account] = reader(fake_hub, tmp_path).refresh(at_minutes(5))["accounts"]
    assert len(fake_hub.relayed) == 2 and account["state"] == "working"


def test_a_week_reset_seen_while_the_five_hour_limit_is_full_releases_the_hold_once_it_has_room(fake_hub, tmp_path):
    signals = CLAUDE_SEEN["signals"] | {"Anthropic-Ratelimit-Unified-7d-Utilization": "1",
                                        "Anthropic-Ratelimit-Unified-5h-Utilization": "1"}
    fake_hub.credentials = [CLAUDE | {"quota_blocked": True, "unavailable": True,
                                      "quota": {"observed_at": "2026-11-09T11:59:00Z", "signals": signals}}]
    five_hour_full = {"utilization": 100, "resets_at": "2026-11-09T14:00:00Z"}
    fake_hub.relay_answers = {"2": (200, CLAUDE_ANSWER | {"five_hour": five_hour_full})}
    [account] = reader(fake_hub, tmp_path).refresh(NOW)["accounts"]
    # The week was reset by hand, but the five-hour limit is still full: the hold stays, and the week shows 12%.
    assert account["state"] == "cooling" and account["week"]["used"] == 12 and fake_hub.changes == []
    assert checks_kept(tmp_path)[hub.limits_key(CLAUDE)]["week_reset"] is True
    # Five minutes on the five-hour limit has room, and the week is a little more used than the 12% saved.
    fake_hub.relay_answers = {"2": (200, CLAUDE_ANSWER | {
        "five_hour": {"utilization": 0, "resets_at": "2026-11-09T17:00:00Z"},
        "seven_day": {"utilization": 13.0, "resets_at": "2026-11-14T09:00:00+00:00"}})}
    [account] = reader(fake_hub, tmp_path).refresh(at_minutes(5))["accounts"]
    assert account["state"] == "working" and account["week"]["used"] == 13
    assert fake_hub.changes == [("POST", "/v8/management/routing/cooldown/reset", {"auth_index": "2"})]
    assert checks_kept(tmp_path)[hub.limits_key(CLAUDE)]["week_reset"] is False


def test_a_week_reset_seen_in_an_earlier_hold_releases_no_later_hold(fake_hub, tmp_path):
    held = CLAUDE | {"quota_blocked": True, "unavailable": True}
    fake_hub.credentials = [held]
    fake_hub.relay_answers = {"2": (200, CLAUDE_ANSWER | {"seven_day": {"utilization": 0, "resets_at": "2026-11-14T09:00:00Z"},
                                                          "five_hour": {"utilization": 100, "resets_at": "2026-11-09T14:00:00Z"}})}
    reader(fake_hub, tmp_path).refresh(NOW)
    assert checks_kept(tmp_path)[hub.limits_key(CLAUDE)]["week_reset"] is True
    # The hub ends that hold itself, then holds the account again later.
    fake_hub.credentials = [CLAUDE]
    reader(fake_hub, tmp_path).refresh(at_minutes(1))
    assert checks_kept(tmp_path)[hub.limits_key(CLAUDE)]["week_reset"] is False
    fake_hub.credentials = [held]
    fake_hub.relay_answers = {"2": (200, CLAUDE_ANSWER | {"seven_day": {"utilization": 50, "resets_at": "2026-11-14T09:00:00Z"},
                                                          "five_hour": {"utilization": 0, "resets_at": "2026-11-09T17:00:00Z"}})}
    [account] = reader(fake_hub, tmp_path).refresh(at_minutes(10))["accounts"]
    assert account["state"] == "cooling" and fake_hub.changes == []


@pytest.mark.parametrize("fields", [{"disabled": True}, {"requires_sign_in": True}, {"quota_blocked": False}])
def test_recovery_preserves_user_account_choices_and_non_quota_holds(fake_hub, tmp_path, fields):
    fake_hub.credentials = [CLAUDE | {"quota_blocked": True, "unavailable": True} | fields]
    fake_hub.relay_answers = {"2": (200, CLAUDE_ANSWER)}
    reader(fake_hub, tmp_path).refresh(NOW)
    assert fake_hub.relayed == [] and fake_hub.changes == []


@pytest.mark.parametrize("scope", ["five_hour", "model"])
def test_reset_recovery_keeps_an_omitted_unexpired_exhausted_limit(fake_hub, tmp_path, scope):
    signals = CLAUDE_SEEN["signals"].copy()
    signals["Anthropic-Ratelimit-Unified-7d-Utilization"] = "0.99"
    signals["Anthropic-Ratelimit-Unified-5h-Utilization"] = "1" if scope == "five_hour" else "0.1"
    if scope == "model":
        signals["Anthropic-Ratelimit-Unified-7d_oi-Utilization"] = "1"
    fake_hub.credentials = [CLAUDE | {"quota_blocked": True, "unavailable": True,
        "quota": {"observed_at": "2026-11-09T11:59:00Z", "signals": signals}}]
    answer = CLAUDE_ANSWER | {"seven_day": {"utilization": 0, "resets_at": "2026-11-14T09:00:00Z"}}
    if scope == "model":
        answer = answer | {"limits": []}
    fake_hub.relay_answers = {"2": (200, answer)}
    [account] = reader(fake_hub, tmp_path).refresh(NOW)["accounts"]
    assert account["state"] == "cooling" and fake_hub.changes == []
    held = account["windows"][0]["used"] if scope == "five_hour" else account["model_weeks"][0]["used"]
    assert held == 100
