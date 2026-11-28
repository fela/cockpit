from __future__ import annotations

import logging
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from cockpit import auth, gemini_usage, machine
from cockpit.app import create_app
from cockpit.views import read as read_view
from tests.conftest import COOKIE_KEY, EMAIL, ORIGIN, World

NOW = datetime(2026, 11, 26, 10, 0, tzinfo=UTC)
QUOTA = (
    "Gemini Models\tWeekly Limit Remaining\t88%\t2026-12-02T19:40:00Z\n"
    "Gemini Models\tFive Hour Limit Remaining\t64%\t2026-11-26T11:50:00Z\n"
    "Claude and GPT models\tWeekly Limit Remaining\t97%\t2026-12-03T05:15:00Z\n"
    "Claude and GPT models\tFive Hour Limit Remaining\t90%\t2026-11-26T14:35:00Z\n"
)


def settled(quota: gemini_usage.Quota, now: datetime) -> dict:
    """The reading at `now` once the thread the first read started has finished."""
    quota.read(now)
    thread = quota._thread
    assert thread is not None
    thread.join(5)
    return quota.read(now)


def test_the_output_parses_into_groups_of_limits() -> None:
    groups = gemini_usage.parse(QUOTA)
    assert [group["name"] for group in groups] == ["Gemini Models", "Claude and GPT models"]
    assert groups[0]["limits"]["seven_day"] == {
        "used_percentage": 12, "resets_at": datetime(2026, 12, 2, 19, 40, 0, tzinfo=UTC).timestamp()}
    assert groups[0]["limits"]["five_hour"]["used_percentage"] == 36


@pytest.mark.parametrize("text", [
    "", "Please sign in to Antigravity\n", "Gemini Models\tDaily Limit Remaining\t5%\t2026-12-02T19:40:00Z\n",
    *(f"Gemini Models\tWeekly Limit Remaining\t{share}%\t2026-12-02T19:40:00Z\n" for share in ["nan", "inf", "101", "-1"]),
])
def test_output_it_cannot_read_is_an_error(text: str) -> None:
    with pytest.raises(ValueError):
        gemini_usage.parse(text)


def test_a_reading_gives_each_groups_week_and_five_hours() -> None:
    reading = settled(gemini_usage.Quota(lambda: QUOTA), NOW)
    gemini, other = reading["groups"]
    assert gemini["name"] == "Gemini Models"
    assert (gemini["week"]["used"], gemini["week"]["key"], gemini["week"]["span"]) == (12, "seven_day", 100)
    assert gemini["week"]["resets_at"] == "2026-12-02T19:40:00Z"
    assert (gemini["week"]["elapsed"], gemini["week"]["pace"]) == (9, "near")
    assert (gemini["five_hour"]["used"], gemini["five_hour"]["elapsed"]) == (36, 63)
    assert other["week"]["used"] == 3
    assert reading["error"] == "" and reading["captured_at"] is not None


def test_a_week_past_its_reset_has_unknown_use() -> None:
    quota = gemini_usage.Quota(lambda: QUOTA)
    settled(quota, NOW)
    later = quota.read(datetime(2026, 12, 2, 22, tzinfo=UTC))
    assert later["groups"][0]["week"]["used"] is None
    assert later["groups"][1]["week"]["used"] == 3


def test_it_reads_again_only_after_the_refresh_time() -> None:
    calls = []
    quota = gemini_usage.Quota(lambda: calls.append(1) or QUOTA)
    settled(quota, NOW)
    settled(quota, NOW + timedelta(seconds=gemini_usage.REFRESH_SECONDS - 1))
    assert len(calls) == 1
    settled(quota, NOW + timedelta(seconds=gemini_usage.REFRESH_SECONDS))
    assert len(calls) == 2
    # A clock set back makes the reading due rather than waiting for the old time to come round again.
    settled(quota, NOW - timedelta(hours=2))
    assert len(calls) == 3


def test_a_failed_reading_keeps_the_last_numbers_and_says_why(caplog: pytest.LogCaptureFixture) -> None:
    answers = iter([QUOTA, RuntimeError("agy -p /quota exited 2: no quota for an API key")])

    def run() -> str:
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    quota = gemini_usage.Quota(run)
    first = settled(quota, NOW)
    with caplog.at_level(logging.ERROR):
        failed = settled(quota, NOW + timedelta(seconds=gemini_usage.REFRESH_SECONDS))
    assert failed["groups"][0]["week"]["used"] == 12
    assert failed["captured_at"] == first["captured_at"]
    assert failed["error"] == "agy -p /quota exited 2: no quota for an API key"
    assert "could not be read" in caplog.text


def test_the_state_carries_geminis_limits_when_it_is_enabled(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(machine, "read", lambda: {"level": "under"})
    monkeypatch.setattr(gemini_usage, "run_quota", lambda: QUOTA)
    app = create_app(replace(world.settings, providers=("claude", "gemini")), world.deliverer, world.cache,
                     world.spend_index, world.codex_index, [], read_view)
    browser = TestClient(app, base_url=ORIGIN)
    browser.cookies.set(auth.SESSION_COOKIE, auth.session_cookie_value(COOKIE_KEY, EMAIL, int(time.time()) + 3600))
    browser.get("/api/state")
    deadline = time.monotonic() + 5
    while not (found := browser.get("/api/state").json()["usage"]["gemini"])["groups"] and time.monotonic() < deadline:
        time.sleep(0.02)
    assert [group["name"] for group in found["groups"]] == ["Gemini Models", "Claude and GPT models"]
