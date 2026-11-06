"""A snooze time the user typed in their own words (SPEC VIEW-29)."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from cockpit import snooze_words
from tests.conftest import World

ZONE = ZoneInfo("Europe/Amsterdam")
NOW = datetime(2026, 2, 1, 4, 0, tzinfo=UTC)


def test_the_answer_is_read_in_the_users_timezone() -> None:
    at = snooze_words.moment({"at": "2026-02-06T09:00", "unclear": ""}, ZONE, NOW)
    assert at == datetime(2026, 2, 6, 8, 0, tzinfo=UTC)


@pytest.mark.parametrize(("answer", "why"), [
    ({"at": "", "unclear": "no day or time named"}, "no day or time named"),
    ({"at": "", "unclear": ""}, "the words name no time"),
    ({"at": "Friday", "unclear": ""}, "the answer was not a time"),
    ({"at": "2026-01-31T09:00", "unclear": ""}, "that time has passed"),
    ({"at": "2028-01-01T09:00", "unclear": ""}, "that is more than a year away"),
])
def test_an_answer_that_is_no_usable_time_says_why(answer: dict[str, str], why: str) -> None:
    with pytest.raises(snooze_words.Unread, match=why):
        snooze_words.moment(answer, ZONE, NOW)


def test_a_time_past_the_calendars_end_is_refused_with_a_reason() -> None:
    with pytest.raises(snooze_words.Unread, match="the answer was not a time"):
        snooze_words.moment({"at": "9999-12-31T23:59", "unclear": ""}, ZoneInfo("America/New_York"), NOW)


def test_a_time_the_clocks_skip_is_refused() -> None:
    with pytest.raises(snooze_words.Unread, match="the clocks skip that time"):
        snooze_words.moment({"at": "2027-03-28T02:30", "unclear": ""}, ZONE, datetime(2027, 1, 15, 10, tzinfo=UTC))


def test_a_time_that_comes_twice_is_the_next_one_still_ahead() -> None:
    first = datetime(2027, 10, 31, 0, 30, tzinfo=UTC)
    second = datetime(2027, 10, 31, 1, 30, tzinfo=UTC)
    answer = {"at": "2027-10-31T02:30", "unclear": ""}
    assert snooze_words.moment(answer, ZONE, datetime(2027, 10, 31, 0, 0, tzinfo=UTC)) == first
    assert snooze_words.moment(answer, ZONE, datetime(2027, 10, 31, 0, 45, tzinfo=UTC)) == second


def test_the_page_gets_the_time_in_utc(world: World, monkeypatch) -> None:
    seen = {}

    def read(text: str, zone: ZoneInfo, now: datetime) -> datetime:
        seen.update(text=text, zone=zone.key)
        return datetime(2026, 2, 6, 8, 0, tzinfo=UTC)

    monkeypatch.setattr(snooze_words, "read", read)
    response = world.client.post("/api/snooze/read", json={"text": " friday morning ", "tz": "Europe/Amsterdam"}, headers=world.user())
    assert response.status_code == 200
    assert response.json() == {"until": "2026-02-06T08:00:00Z"}
    assert seen == {"text": "friday morning", "zone": "Europe/Amsterdam"}


def test_words_with_no_time_are_refused_with_the_reason(world: World, monkeypatch) -> None:
    def read(text: str, zone: ZoneInfo, now: datetime) -> datetime:
        raise snooze_words.Unread("no day or time named")

    monkeypatch.setattr(snooze_words, "read", read)
    response = world.client.post("/api/snooze/read", json={"text": "soon-ish", "tz": "Europe/Amsterdam"}, headers=world.user())
    assert response.status_code == 400
    assert "no day or time named" in response.text


def test_an_unknown_timezone_is_refused(world: World) -> None:
    response = world.client.post("/api/snooze/read", json={"text": "tomorrow", "tz": "Mars/Base"}, headers=world.user())
    assert response.status_code == 400


def test_only_the_user_reads_snooze_words(world: World) -> None:
    response = world.client.post("/api/snooze/read", json={"text": "tomorrow", "tz": "Europe/Amsterdam"})
    assert response.status_code in (401, 403)


def test_codex_only_install_reads_words_in_an_ephemeral_thread(tmp_path, monkeypatch):
    import dataclasses
    import json

    from cockpit import config

    setup = dataclasses.replace(config.setup(), providers=("codex",), default_provider="codex")
    monkeypatch.setattr(config, "setup", lambda: setup)
    monkeypatch.setattr(snooze_words, "WORK_DIR", tmp_path)
    monkeypatch.setattr(snooze_words.accounts, "in_use", lambda *args: pytest.fail("Claude is disabled"))
    calls = []

    class Rpc:
        def __init__(self, command, event):
            assert command == setup.codex_command
            self.event = event

        def request(self, method, params):
            calls.append((method, params))
            if method == "thread/start":
                return {"thread": {"id": "temporary"}}
            self.event({"method": "item/completed", "params": {"threadId": "temporary", "item": {
                "type": "agentMessage", "text": json.dumps({"at": "2026-02-06T09:00", "unclear": ""})}}})
            self.event({"method": "turn/completed", "params": {"threadId": "temporary", "turn": {"status": "completed"}}})
            return {}

        def close(self):
            calls.append(("close", {}))

    monkeypatch.setattr(snooze_words.codex_agent, "AppServer", Rpc)
    assert snooze_words.read("Friday morning", ZONE, NOW) == datetime(2026, 2, 6, 8, 0, tzinfo=UTC)
    start, turn, close = calls
    assert start[1]["ephemeral"] is True
    assert start[1]["sandbox"] == "read-only" and start[1]["approvalPolicy"] == "never"
    assert "Use no tools" in start[1]["baseInstructions"]
    assert turn[1]["outputSchema"] == snooze_words.SCHEMA
    assert "Friday morning" in turn[1]["input"][0]["text"]
    assert close[0] == "close"


def test_claude_stays_preferred_when_both_providers_are_enabled(monkeypatch):
    import dataclasses

    from cockpit import config

    setup = dataclasses.replace(config.setup(), providers=("codex", "claude"))
    monkeypatch.setattr(config, "setup", lambda: setup)
    monkeypatch.setattr(snooze_words.accounts, "usable", lambda *args: [config.ACCOUNTS[0]])
    monkeypatch.setattr(snooze_words, "ask", lambda *args: {"at": "2026-02-06T09:00", "unclear": ""})
    monkeypatch.setattr(snooze_words, "ask_codex", lambda *args: pytest.fail("Claude is enabled"))
    assert snooze_words.read("Friday morning", ZONE, NOW) == datetime(2026, 2, 6, 8, 0, tzinfo=UTC)
