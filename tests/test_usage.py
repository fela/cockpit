from __future__ import annotations

import dataclasses
import io
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cockpit import accounts, config, machine, resets, usage
from tests.conftest import World

NOW = datetime(2026, 1, 5, 8, 30, tzinfo=UTC)
FIVE_HOURS = 5 * 3600
SEVEN_DAYS = 7 * 86400

ACCOUNT = {
    "emailAddress": "sam@example.com",
    "displayName": "Sam",
    "organizationName": "sam@example.com's Organization",
    "organizationType": "claude_max",
    "organizationRateLimitTier": "default_claude_max_20x",
    "billingType": "stripe_subscription",
    "hasExtraUsageEnabled": False,
}


def record(account: dict[str, Any], rate_limits: dict[str, Any], captured_at: float) -> dict[str, Any]:
    return {
        "account": account,
        "rate_limits": rate_limits,
        "captured_at": captured_at,
        "session_id": "a0a0a005-0000-4000-8000-000000000005",
        "config_dir": "/home/sam/.claude",
    }


def window_ending(used: float, elapsed_share: float, length: int) -> dict[str, float]:
    return {"used_percentage": used, "resets_at": NOW.timestamp() + (1 - elapsed_share) * length}


def save(path: Path, accounts: dict[str, Any]) -> None:
    path.write_text(json.dumps({"accounts": accounts}))


@pytest.mark.parametrize(
    ("used", "elapsed_share", "expected"),
    [
        # Start of the window: under below +2, over above +20.
        (1, 0.0, "under"), (3, 0.0, "near"), (20, 0.0, "near"), (21, 0.0, "over"),
        # Halfway: under below -1.5, over above +10.
        (48, 0.5, "under"), (55, 0.5, "near"), (61, 0.5, "over"),
        # End: under below -5, over above 0.
        (94, 1.0, "under"), (99, 1.0, "near"), (100.5, 1.0, "over"),
    ],
)
def test_pace_follows_the_status_line_thresholds(used: float, elapsed_share: float, expected: str) -> None:
    assert usage.pace(used, elapsed_share) == expected


def test_windows_show_used_and_elapsed_share(tmp_path: Path) -> None:
    path = tmp_path / "rate-limits.json"
    limits = {"five_hour": window_ending(70, 0.16, FIVE_HOURS), "seven_day": window_ending(33.4, 0.26, SEVEN_DAYS)}
    save(path, {"sam@example.com": record(ACCOUNT, limits, NOW.timestamp() - 60)})
    [account] = usage.read(path, NOW, [], None, {})["accounts"]
    assert account["email"] == "sam@example.com"
    assert account["plan"] == "Max 20x"
    assert account["captured_at"] == "2026-01-05T08:29:00Z"
    five, seven = account["windows"]
    assert (five["label"], five["used"], five["elapsed"], five["pace"]) == ("5h", 70, 16, "over")
    assert (seven["label"], seven["used"], seven["elapsed"], seven["pace"]) == ("7d", 33, 26, "near")
    assert seven["resets_at"].endswith("Z")


def test_a_window_left_out_or_reset_since_has_unknown_use(tmp_path: Path) -> None:
    path = tmp_path / "rate-limits.json"
    reset = {"used_percentage": 70, "resets_at": NOW.timestamp() - 1}
    save(path, {"sam@example.com": record(ACCOUNT, {"seven_day": reset}, NOW.timestamp() - 3600)})
    five, seven = usage.read(path, NOW, [], None, {})["accounts"][0]["windows"]
    assert five["used"] is None and five["pace"] is None and five["resets_at"] is None
    assert seven["used"] is None


def test_the_most_recently_reported_account_comes_first(tmp_path: Path) -> None:
    path = tmp_path / "rate-limits.json"
    other = ACCOUNT | {"emailAddress": "sam.home@example.com", "organizationType": "claude_pro", "organizationRateLimitTier": "default_claude_ai"}
    limits = {"five_hour": window_ending(10, 0.5, FIVE_HOURS)}
    save(path, {
        "sam.home@example.com": record(other, limits, NOW.timestamp() - 7200),
        "sam@example.com": record(ACCOUNT, limits, NOW.timestamp() - 60),
    })
    accounts = usage.read(path, NOW, [], None, {})["accounts"]
    assert [(entry["email"], entry["plan"]) for entry in accounts] == [("sam@example.com", "Max 20x"), ("sam.home@example.com", "Pro")]


OTHER = ACCOUNT | {"emailAddress": "sam.home@example.com", "displayName": "Sam"}
HOUR = 3600


def week_resetting(used: float, hours_from_now: float) -> dict[str, float]:
    return {"used_percentage": used, "resets_at": NOW.timestamp() + hours_from_now * HOUR}


def test_the_week_counts_its_own_seven_days_while_another_account_is_used_up(tmp_path: Path) -> None:
    path = tmp_path / "rate-limits.json"
    save(path, {
        "sam@example.com": record(ACCOUNT, {"seven_day": week_resetting(100, 3)}, NOW.timestamp() - 60),
        "sam.home@example.com": record(OTHER, {"seven_day": week_resetting(84, 33)}, NOW.timestamp()),
    })
    listed = {entry["email"]: entry for entry in usage.read(path, NOW, [], None, {})["accounts"]}
    seven = listed["sam.home@example.com"]["windows"][1]
    assert (seven["used"], seven["elapsed"], seven["pace"]) == (84, 80, "near")
    assert seven["resets_at"] == usage._iso(NOW.timestamp() + 33 * HOUR)
    assert listed["sam@example.com"]["windows"][1]["elapsed"] == 98


def test_the_week_started_seven_days_before_it_resets(tmp_path: Path) -> None:
    path = tmp_path / "rate-limits.json"
    third = ACCOUNT | {"emailAddress": "sam@example.org"}
    save(path, {
        "sam@example.com": record(ACCOUNT, {"seven_day": week_resetting(40, 30)}, NOW.timestamp()),
        # Its week reset 10 hours ago, after its numbers were saved: the next week started then.
        "sam.home@example.com": record(OTHER, {"seven_day": week_resetting(100, -10)}, NOW.timestamp() - 50 * HOUR),
        "sam@example.org": record(third, {}, NOW.timestamp() - 60 * HOUR),
    })
    starts = {account["email"]: account["week_start"] for account in usage.read(path, NOW, [], None, {})["accounts"]}
    assert starts == {
        "sam@example.com": usage._iso(NOW.timestamp() + 30 * HOUR - SEVEN_DAYS),
        "sam.home@example.com": usage._iso(NOW.timestamp() - 10 * HOUR),
        "sam@example.org": None,
    }


def test_no_file_yet_is_no_usage(tmp_path: Path) -> None:
    assert usage.read(tmp_path / "missing.json", NOW, [], None, {}) == {"accounts": [], "error": ""}


@pytest.mark.parametrize("text", ["{not json", json.dumps({"accounts": {"sam@example.com": {"account": {}}}})])
def test_a_broken_file_is_reported(tmp_path: Path, text: str) -> None:
    path = tmp_path / "rate-limits.json"
    path.write_text(text)
    result = usage.read(path, NOW, [], None, {})
    assert result["accounts"] == []
    assert str(path) in result["error"]


def test_the_state_carries_the_usage(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(machine, "read", lambda: {"level": "under"})
    assert world.client.get("/api/state").json()["usage"] == {"accounts": [], "error": "", "codex": [], "machine": {"level": "under"}}
    now = datetime.now(UTC).timestamp()
    limits = {"five_hour": {"used_percentage": 12, "resets_at": now + 3600}}
    save(world.claude_rate_limits, {"sam@example.com": record(ACCOUNT, limits, now)})
    [account] = world.client.get("/api/state").json()["usage"]["accounts"]
    assert account["email"] == "sam@example.com"
    assert (account["windows"][0]["used"], account["windows"][0]["elapsed"]) == (12, 80)


def test_a_record_carries_its_folder_sign_in_end_and_banked_resets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "claude-work"
    folder.mkdir()
    oauth = {"emailAddress": "sam@example.com", "refreshTokenExpiresAt": int(datetime(2026, 2, 15, 10, tzinfo=UTC).timestamp() * 1000)}
    (folder / ".credentials.json").write_text(json.dumps({"claudeAiOauth": oauth}))
    (tmp_path / "claude-work.json").write_text(json.dumps({"oauthAccount": oauth}))
    work = config.Account(name="work", letter="W", folder=folder, state=tmp_path / "claude-work.json", default=False)
    banked = {"read_at": NOW.isoformat(), "grants": [], "cooldown_until": None}
    monkeypatch.setattr(resets, "_known", {"sam@example.com": banked})
    path = tmp_path / "rate-limits.json"
    save(path, {"sam@example.com": record(ACCOUNT, {}, NOW.timestamp() - 60)})
    [account] = usage.read(path, NOW, [work], None, {})["accounts"]
    assert (account["folder"], account["sign_in_ends"], account["resets"]) == ("work", "2026-02-15T10:00:00+00:00", banked)


def test_a_signed_out_folder_is_listed_after_the_ones_new_work_can_go_to(tmp_path: Path) -> None:
    folders = []
    for name, email, refresh in (("personal", "ana@example.com", ""), ("work", "sam@example.com", "token")):
        folder = tmp_path / f"claude-{name}"
        folder.mkdir()
        oauth = {"emailAddress": email, "refreshToken": refresh, "refreshTokenExpiresAt": int(datetime(2026, 2, 15, 10, tzinfo=UTC).timestamp() * 1000)}
        (folder / ".credentials.json").write_text(json.dumps({"claudeAiOauth": oauth}))
        (tmp_path / f"claude-{name}.json").write_text(json.dumps({"oauthAccount": oauth}))
        folders.append(config.Account(name=name, letter=name[0].upper(), folder=folder, state=tmp_path / f"claude-{name}.json", default=False))
    path = tmp_path / "rate-limits.json"
    save(path, {})
    first, second = usage.read(path, NOW, folders, None, {})["accounts"]
    assert (first["folder"], second["folder"]) == ("work", "personal")
    assert second["sign_in_ends"] not in (None, "2026-02-15T10:00:00+00:00")  # ended when Claude emptied it
    # The page names a signed-out account as such, not as one at its limit.
    assert (first["signed_out"], second["signed_out"]) == (False, True)


def test_a_signed_in_account_with_no_record_yet_gets_a_line_of_unknown_use(tmp_path: Path) -> None:
    folder = tmp_path / "claude-work"
    folder.mkdir()
    oauth = {"emailAddress": "ana@example.com", "refreshTokenExpiresAt": int(datetime(2026, 2, 15, 10, tzinfo=UTC).timestamp() * 1000)}
    (folder / ".credentials.json").write_text(json.dumps({"claudeAiOauth": oauth}))
    (tmp_path / "claude-work.json").write_text(json.dumps({"oauthAccount": oauth}))
    work = config.Account(name="work", letter="W", folder=folder, state=tmp_path / "claude-work.json", default=False)
    path = tmp_path / "rate-limits.json"
    save(path, {"sam@example.com": record(ACCOUNT, {}, NOW.timestamp() - 60)})
    first, second = usage.read(path, NOW, [work], None, {})["accounts"]
    assert (first["email"], first["folder"], first["captured_at"]) == ("ana@example.com", "work", None)
    assert [window["used"] for window in first["windows"]] == [None, None]
    assert (second["email"], second["folder"]) == ("sam@example.com", "")


def test_banked_resets_and_windows_are_kept_by_address_so_a_folder_signed_in_anew_starts_with_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "claude-work"
    folder.mkdir()
    (folder / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "x", "expiresAt": 9e12}}))
    work = config.Account(name="work", letter="W", folder=folder, state=tmp_path / "claude-work.json", default=False)
    body = {"cedar_ember": {"eligible": True, "grants": [GRANT], "cooldown_until": None},
            "five_hour": None, "seven_day": {"utilization": 1.0, "resets_at": "2026-01-24T12:00:00.500000+00:00"}}
    monkeypatch.setattr(resets, "_known", {})
    monkeypatch.setattr(resets, "_readings", {})
    monkeypatch.setattr(resets, "fetch", lambda account, now: body)

    def stop(seconds: float) -> None:
        raise SystemExit

    monkeypatch.setattr(resets.time, "sleep", stop)
    with pytest.raises(SystemExit):
        resets.refresh_forever(lambda: [(work, "sam@example.com")])
    banked, reading = resets.known("sam@example.com"), resets.reading("sam@example.com")
    assert banked is not None and [grant["label"] for grant in banked["grants"]] == ["Launch offer"]
    assert reading is not None and reading["rate_limits"] == {
        "seven_day": {"used_percentage": 1.0, "resets_at": datetime(2026, 1, 24, 12, 0, 0, 500000, tzinfo=UTC).timestamp()},
    }
    assert (resets.known("ana@example.com"), resets.reading("ana@example.com")) == (None, None)


def test_an_unreadable_window_is_logged_and_keeps_the_last_reading(caplog: pytest.LogCaptureFixture) -> None:
    assert resets.windows({"seven_day": {"utilization": 3, "resets_at": "Friday"}}, NOW) is None
    assert "unexpected shape" in caplog.text


def _work_folder(tmp_path: Path, address: str) -> config.Account:
    folder = tmp_path / "claude-work"
    folder.mkdir()
    (tmp_path / "claude-work.json").write_text(json.dumps({"oauthAccount": {"emailAddress": address}}))
    return config.Account(name="work", letter="W", folder=folder, state=tmp_path / "claude-work.json", default=False)


def test_a_newer_endpoint_reading_replaces_an_old_record_s_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work = _work_folder(tmp_path, "sam@example.com")
    path = tmp_path / "rate-limits.json"
    old = {"seven_day": window_ending(80, 0.5, SEVEN_DAYS)}
    save(path, {"sam@example.com": record(ACCOUNT, old, NOW.timestamp() - 2 * 86400)})
    fresh = {"seven_day": window_ending(10, 0.2, SEVEN_DAYS)}
    monkeypatch.setattr(resets, "_readings", {"sam@example.com": {"rate_limits": fresh, "captured_at": NOW.timestamp() - 300}})
    [account] = usage.read(path, NOW, [work], None, {})["accounts"]
    assert (account["captured_at"], account["windows"][1]["used"], account["plan"]) == ("2026-01-05T08:25:00Z", 10, "Max 20x")
    assert accounts_readings(work, path)["rate_limits"] == fresh


def test_a_record_newer_than_the_endpoint_reading_stays(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work = _work_folder(tmp_path, "sam@example.com")
    path = tmp_path / "rate-limits.json"
    saved = {"seven_day": window_ending(80, 0.5, SEVEN_DAYS)}
    save(path, {"sam@example.com": record(ACCOUNT, saved, NOW.timestamp() - 60)})
    stale = {"seven_day": window_ending(10, 0.2, SEVEN_DAYS)}
    monkeypatch.setattr(resets, "_readings", {"sam@example.com": {"rate_limits": stale, "captured_at": NOW.timestamp() - 900}})
    [account] = usage.read(path, NOW, [work], None, {})["accounts"]
    assert account["windows"][1]["used"] == 80
    assert accounts_readings(work, path)["rate_limits"] == saved


def test_an_account_no_session_ran_on_shows_its_endpoint_reading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work = _work_folder(tmp_path, "ana@example.com")
    fresh = {"five_hour": window_ending(0, 0.1, FIVE_HOURS), "seven_day": window_ending(1, 0.3, SEVEN_DAYS)}
    monkeypatch.setattr(resets, "_readings", {"ana@example.com": {"rate_limits": fresh, "captured_at": NOW.timestamp() - 60}})
    [account] = usage.read(tmp_path / "rate-limits.json", NOW, [work], None, {})["accounts"]
    assert (account["email"], account["folder"], account["plan"]) == ("ana@example.com", "work", "")
    assert [window["used"] for window in account["windows"]] == [0, 1]


def test_accounts_with_equal_room_keep_the_picker_s_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work = _work_folder(tmp_path, "ana@example.com")
    personal = config.Account(name="personal", letter="P", folder=tmp_path, state=tmp_path / "personal.json", default=True)
    (tmp_path / "personal.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "sam@example.com"}}))
    monkeypatch.setattr(resets, "_readings", {
        "sam@example.com": {"rate_limits": {}, "captured_at": NOW.timestamp() - 600},
        "ana@example.com": {"rate_limits": {}, "captured_at": NOW.timestamp() - 60},
    })
    listed = usage.read(tmp_path / "rate-limits.json", NOW, [personal, work], None, {})["accounts"]
    assert [entry["folder"] for entry in listed] == ["personal", "work"]
    # The account the user prefers for now goes first, and says until when.
    preference = usage.Preference("work", datetime(2026, 1, 5, 9, 0, tzinfo=UTC), 30)
    listed = usage.read(tmp_path / "rate-limits.json", NOW, [personal, work], preference, {})["accounts"]
    assert [(entry["folder"], entry["preferred_until"], entry["preferred_minutes"]) for entry in listed] == [
        ("work", "2026-01-05T09:00:00Z", 30), ("personal", None, None),
    ]


def test_the_sessions_running_on_an_account_lower_its_place(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SPEC SESSION-17: a week nearly used up comes first while a session fits, and after the other account once the
    sessions running on it are expected to use the rest."""
    work = _work_folder(tmp_path, "ana@example.com")
    personal = config.Account(name="personal", letter="P", folder=tmp_path, state=tmp_path / "personal.json", default=True)
    (tmp_path / "personal.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "sam@example.com"}}))
    monkeypatch.setattr(resets, "_readings", {
        "sam@example.com": {"rate_limits": {"seven_day": window_ending(95, 0.95, SEVEN_DAYS)}, "captured_at": NOW.timestamp() - 60},
        "ana@example.com": {"rate_limits": {"seven_day": window_ending(40, 0.2, SEVEN_DAYS)}, "captured_at": NOW.timestamp() - 60},
    })
    listed = usage.read(tmp_path / "rate-limits.json", NOW, [personal, work], None, {})["accounts"]
    assert [(entry["folder"], entry["running"]) for entry in listed] == [("personal", 0), ("work", 0)]
    listed = usage.read(tmp_path / "rate-limits.json", NOW, [personal, work], None, {"personal": 3, "work": 1})["accounts"]
    assert [(entry["folder"], entry["running"]) for entry in listed] == [("work", 1), ("personal", 3)]


def accounts_readings(account: config.Account, path: Path) -> dict[str, Any]:
    found = accounts.readings([account], path)[account.name]
    assert found is not None
    return found


def test_a_failed_account_listing_does_not_stop_the_reset_reads(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    sleeps = []

    def listing() -> list[tuple[config.Account, str]]:
        raise PermissionError("claude-work")

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise SystemExit

    monkeypatch.setattr(resets.time, "sleep", sleep)
    with pytest.raises(SystemExit):
        resets.refresh_forever(listing)
    assert sleeps == [resets.EVERY_SECONDS, resets.EVERY_SECONDS]
    assert "listing the signed-in Claude accounts" in caplog.text


def test_a_record_no_folder_is_signed_in_with_has_no_sign_in_or_resets(tmp_path: Path) -> None:
    path = tmp_path / "rate-limits.json"
    save(path, {"sam@example.com": record(ACCOUNT, {}, NOW.timestamp() - 60)})
    [account] = usage.read(path, NOW, [], None, {})["accounts"]
    assert (account["folder"], account["sign_in_ends"], account["resets"]) == ("", None, None)


GRANT = {
    "id": "g1", "label": "Launch offer", "resets_total": 1, "resets_left": 1, "starts_at": "2026-01-15T16:00:00Z",
    "ends_at": "2026-01-22T16:00:00Z", "clears": ["five_hour"], "paused": False, "usable_now": True,
    "use_requires_limit": False, "unavailable_reason": None,
}


def test_banked_resets_keep_what_the_page_shows() -> None:
    parsed = resets.parse({"eligible": True, "grants": [GRANT], "cooldown_until": "2026-01-19T12:00:00Z"}, NOW)
    assert parsed == {
        "read_at": NOW.isoformat(),
        "grants": [{"label": "Launch offer", "left": 1, "total": 1, "ends_at": "2026-01-22T16:00:00Z",
                    "usable_now": True, "needs_limit": False, "paused": False, "clears": ["five_hour"]}],
        "cooldown_until": "2026-01-19T12:00:00Z",
    }


def test_an_account_not_eligible_has_no_banked_resets() -> None:
    parsed = resets.parse({"eligible": False, "ineligible_reason": "plan", "grants": [GRANT]}, NOW)
    assert parsed is not None and parsed["grants"] == []


def test_an_answer_that_did_not_take_cockpit_for_claude_code_is_an_error(caplog: pytest.LogCaptureFixture) -> None:
    parsed = resets.parse({"eligible": False, "ineligible_reason": "surface", "grants": []}, NOW)
    assert parsed is not None and parsed["grants"] == []
    assert [entry.levelname for entry in caplog.records] == ["ERROR"] and "surface" in caplog.text


def test_the_request_names_the_installed_claude_code_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release = tmp_path / "versions" / "2.1.10"
    release.parent.mkdir()
    release.write_text("")
    command = tmp_path / "claude"
    command.symlink_to(release)
    monkeypatch.setattr(resets, "CLAUDE_COMMAND", command)
    assert resets.headers()["User-Agent"] == "claude-cli/2.1.10 (external, cli)"


def test_an_unreadable_answer_is_logged_not_shown(caplog: pytest.LogCaptureFixture) -> None:
    assert resets.parse({"eligible": True, "grants": [{"label": "x"}]}, NOW) is None
    assert "unexpected shape" in caplog.text


def test_an_expired_access_token_is_not_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "sk-ant-oat-secret-value"
    oauth = {"accessToken": secret, "expiresAt": NOW.timestamp() * 1000 - 1}
    (tmp_path / ".credentials.json").write_text(json.dumps({"claudeAiOauth": oauth}))
    account = config.Account(name="work", letter="W", folder=tmp_path, state=tmp_path / "state.json", default=False)

    def no_call(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the endpoint was called")

    monkeypatch.setattr(resets.urllib.request, "urlopen", no_call)
    assert resets.fetch(account, NOW) is None
    assert "expired" in caplog.text and secret not in caplog.text


def test_a_broken_sign_in_file_logs_no_token(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    (tmp_path / ".credentials.json").write_text('{"claudeAiOauth": {"accessToken": "sk-ant-oat-secret-value"')
    account = config.Account(name="work", letter="W", folder=tmp_path, state=tmp_path / "state.json", default=False)
    assert resets.fetch(account, NOW) is None
    assert "JSONDecodeError" in caplog.text and "secret" not in caplog.text


@pytest.fixture
def keychain(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        assert args == ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"]
        assert kwargs == {
            "stdin": subprocess.DEVNULL, "capture_output": True, "text": True, "check": True, "timeout": 3,
        }
        oauth = {"accessToken": "invented-access-token", "expiresAt": 9e12}
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"claudeAiOauth": oauth}), stderr="")

    def no_request(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the usage endpoint is not expected")

    monkeypatch.setattr(resets.sys, "platform", "darwin")
    monkeypatch.setattr(resets.subprocess, "run", run)
    monkeypatch.setattr(resets.urllib.request, "urlopen", no_request)
    monkeypatch.setattr(resets, "headers", dict)
    return calls


def test_keychain_login_supplies_usage_meter_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, keychain: list[list[str]],
) -> None:
    account = dataclasses.replace(_work_folder(tmp_path, "ana@example.com"), default=True)
    before = set(tmp_path.rglob("*"))
    body = {
        "cedar_ember": {"eligible": False},
        "five_hour": {"utilization": 12, "resets_at": "2026-01-05T10:00:00Z"},
        "seven_day": {"utilization": 34, "resets_at": "2026-01-10T08:30:00Z"},
    }

    def response(request: Any, timeout: int) -> io.BytesIO:
        assert request.get_header("Authorization") == "Bearer invented-access-token"
        assert timeout == resets.TIMEOUT_SECONDS
        return io.BytesIO(json.dumps(body).encode())

    def stop(seconds: float) -> None:
        raise SystemExit

    monkeypatch.setattr(resets.urllib.request, "urlopen", response)
    monkeypatch.setattr(resets, "_known", {})
    monkeypatch.setattr(resets, "_readings", {})
    monkeypatch.setattr(resets.time, "sleep", stop)
    with pytest.raises(SystemExit):
        resets.refresh_forever(lambda: [(account, "ana@example.com")])
    [meter] = usage.read(tmp_path / "rate-limits.json", NOW, [account], None, {})["accounts"]
    assert [window["used"] for window in meter["windows"]] == [12, 34]
    assert len(keychain) == 1
    assert set(tmp_path.rglob("*")) == before
    assert "invented-access-token" not in caplog.text


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_missing_sign_in_file_uses_keychain_only_on_macos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keychain: list[list[str]], platform: str,
) -> None:
    monkeypatch.setattr(resets.sys, "platform", platform)
    account = dataclasses.replace(_work_folder(tmp_path, "ana@example.com"), default=True)
    assert resets.fetch(account, NOW) is None
    assert keychain == []


@pytest.mark.parametrize("contents", ["expired", "broken", "unreadable"])
def test_existing_sign_in_file_takes_precedence_over_keychain(
    tmp_path: Path, keychain: list[list[str]], contents: str,
) -> None:
    account = dataclasses.replace(_work_folder(tmp_path, "ana@example.com"), default=True)
    path = account.folder / ".credentials.json"
    if contents == "unreadable":
        path.mkdir()
    elif contents == "broken":
        path.write_text("{")
    else:
        path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "invented-file-token", "expiresAt": 0}}))
    assert resets.fetch(account, NOW) is None
    assert keychain == []


@pytest.mark.parametrize("error", [
    subprocess.CalledProcessError(44, "security", output="invented-access-token", stderr="invented-access-token"),
    subprocess.CalledProcessError(36, "security", output="invented-access-token", stderr="invented-access-token"),
    subprocess.TimeoutExpired("security", 3, output="invented-access-token", stderr="invented-access-token"),
    FileNotFoundError("invented-access-token"),
], ids=["missing-item", "refused", "timeout", "missing-command"])
def test_keychain_failure_leaves_usage_unknown_and_logs_no_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    keychain: list[list[str]], error: Exception,
) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(resets.subprocess, "run", fail)
    account = dataclasses.replace(_work_folder(tmp_path, "ana@example.com"), default=True)
    assert resets.fetch(account, NOW) is None
    assert type(error).__name__ in caplog.text
    assert "invented-access-token" not in caplog.text


@pytest.mark.parametrize("output", [
    '{"claudeAiOauth": {"accessToken": "invented-access-token"',
    "null", "{}", '{"claudeAiOauth": []}',
    '{"claudeAiOauth": {"accessToken": "invented-access-token", "expiresAt": 0}}',
], ids=["broken-json", "null", "missing-oauth", "wrong-oauth-shape", "expired"])
def test_unusable_keychain_login_leaves_usage_unknown_and_logs_no_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    keychain: list[list[str]], output: str,
) -> None:
    monkeypatch.setattr(resets.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=output))
    account = dataclasses.replace(_work_folder(tmp_path, "ana@example.com"), default=True)
    assert resets.fetch(account, NOW) is None
    assert caplog.records
    assert "invented-access-token" not in caplog.text


def test_keychain_serves_only_the_default_folder_s_login(tmp_path: Path, keychain: list[list[str]]) -> None:
    account = _work_folder(tmp_path, "ana@example.com")
    assert resets.fetch(account, NOW) is None
    assert keychain == []


def last_use(monkeypatch: pytest.MonkeyPatch, ends: dict[str, float]) -> None:
    """The setup with `ends` as its last_use."""
    setup = dataclasses.replace(config.read_setup(config.SETUP_PATH), last_use=ends)
    monkeypatch.setattr(config, "setup", lambda: setup)


def test_a_week_counts_to_the_accounts_last_use_while_that_comes_before_its_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two days of a week with five days left, and an end three days from now: 2/5 of the time is gone, not 2/7, so the
    same use reads as closer to its pace, and the lanes draw the shorter week (SPEC UI-75)."""
    now = NOW.timestamp()
    day = 86400
    last_use(monkeypatch, {"sam@example.com": now + 3 * day})
    resets = now + 5 * day
    assert usage.week_counted("sam@example.com", 35, resets, now, 0) == {
        "elapsed": 40, "pace": "under", "counted_until": "2026-01-08T08:30:00Z", "span": 71.43, "banked": 0, "room_used": 35,
    }
    # Another account, an end after the reset and an end already gone all count to the reset.
    plain = {"elapsed": 29, "pace": "near", "counted_until": None, "span": 100.0, "banked": 0, "room_used": 35}
    assert usage.week_counted("other@example.com", 35, resets, now, 0) == plain
    last_use(monkeypatch, {"sam@example.com": now + 6 * day})
    assert usage.week_counted("sam@example.com", 35, resets, now, 0) == plain
    last_use(monkeypatch, {"sam@example.com": now - day})
    assert usage.week_counted("sam@example.com", 35, resets, now, 0) == plain


def test_only_the_seven_day_window_counts_to_the_last_use(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    last_use(monkeypatch, {"sam@example.com": NOW.timestamp() + 86400})
    path = tmp_path / "rate-limits.json"
    limits = {"five_hour": window_ending(70, 0.16, FIVE_HOURS), "seven_day": window_ending(33, 0.26, SEVEN_DAYS)}
    save(path, {"sam@example.com": record(ACCOUNT, limits, NOW.timestamp() - 60)})
    [account] = usage.read(path, NOW, [], None, {})["accounts"]
    five, seven = account["windows"]
    assert (five["elapsed"], five["counted_until"], five["span"]) == (16, None, 100)
    assert seven["counted_until"] == "2026-01-06T08:30:00Z"
    assert seven["elapsed"] > 26
    assert seven["used"] == 33


def test_a_banked_reset_that_clears_the_week_adds_a_week_of_room(monkeypatch):
    """A Claude account's banked resets count as Codex's do (SPEC UI-76); one that clears only 5 hours adds none."""
    now = NOW.timestamp()
    grant = {"label": "Free", "left": 1, "total": 1, "ends_at": None, "usable_now": True, "needs_limit": False, "paused": False, "clears": ["seven_day"]}
    answer = {"read_at": "x", "grants": [grant, grant | {"clears": ["five_hour"]}, grant | {"paused": True}], "cooldown_until": None}
    monkeypatch.setattr(usage.resets, "known", lambda address: answer)
    window = usage._window("seven_day", {"used_percentage": 100, "resets_at": now + 86400}, now, "sam@example.com")
    assert (window["used"], window["banked"], window["room_used"]) == (100, 1, 50)
