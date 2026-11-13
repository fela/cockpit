"""A snooze time the user typed in their own words (SPEC VIEW-29)."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from cockpit import db, snooze_words, store
from cockpit.parts import Event
from tests.conftest import ORIGIN, SESSION_A, World

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


STAND_IN = "2099-01-01T08:00:00Z"
READ = datetime(2099, 1, 2, 7, 55, tzinfo=UTC)
WORDS = {"text": " just before standup ", "tz": "Europe/Amsterdam"}


def snoozed_step(world: World, monkeypatch, read) -> dict:
    """A step the user snoozed in their own words, with `read` in place of Claude's reading."""
    monkeypatch.setattr(snooze_words, "read", read)
    agent = TestClient(world.client.app, base_url=ORIGIN)
    agent.put("/api/items", json={"key": "note:release-notes", "title": "Release notes", "sessions": [SESSION_A]},
              headers=world.agent())
    step = agent.post("/api/steps", json={"item_key": "note:release-notes", "need": "notes", "ask": "Write the notes"},
                      headers=world.agent()).json()["step"]
    response = world.client.post(f"/api/steps/{step['id']}/snooze", json={"until": STAND_IN, "words": WORDS},
                                 headers=world.user())
    assert response.status_code == 200, response.text
    assert response.json()["snoozed_until"] == STAND_IN
    return step


def state(world: World) -> dict:
    return world.client.get("/api/state", headers=world.user()).json()


def snoozed_until(world: World, step: dict) -> str | None:
    conn = db.connect(world.db_path)
    try:
        return store.get_step(conn, step["id"])["snoozed_until"]
    finally:
        conn.close()


def test_the_words_move_the_snooze_to_the_time_read_with_the_calendar(world: World, monkeypatch) -> None:
    seen = {}

    def read(text: str, zone: ZoneInfo, now: datetime, events: list) -> datetime:
        seen.update(text=text, zone=zone.key, events=events)
        return READ

    step = snoozed_step(world, monkeypatch, read)
    assert seen == {"text": "just before standup", "zone": "Europe/Amsterdam", "events": []}
    entry = state(world)["snooze_reads"][f"step:{step['id']}"]
    assert entry | {"at": "", "id": ""} == {
        "words": "just before standup", "status": "read", "until": "2099-01-02T07:55:00Z", "reason": "", "at": "", "id": "",
    }
    assert snoozed_until(world, step) == "2099-01-02T07:55:00Z"


def test_words_with_no_time_bring_the_step_back_with_the_reason(world: World, monkeypatch) -> None:
    def read(text: str, zone: ZoneInfo, now: datetime, events: list) -> datetime:
        raise snooze_words.Unread("no standup in the calendar")

    step = snoozed_step(world, monkeypatch, read)
    entry = state(world)["snooze_reads"][f"step:{step['id']}"]
    assert (entry["status"], entry["reason"]) == ("unread", "no standup in the calendar")
    assert snoozed_until(world, step) is None


def test_a_failed_read_brings_the_step_back(world: World, monkeypatch) -> None:
    def read(text: str, zone: ZoneInfo, now: datetime, events: list) -> datetime:
        raise RuntimeError("the provider crashed")

    step = snoozed_step(world, monkeypatch, read)
    assert state(world)["snooze_reads"][f"step:{step['id']}"]["reason"] == "Cockpit could not read the time"
    assert snoozed_until(world, step) is None


def test_a_read_that_lands_after_the_user_snoozed_again_changes_nothing(world: World, monkeypatch) -> None:
    step = snoozed_step(world, monkeypatch, lambda *args: READ)
    target = f"step:{step['id']}"
    started = datetime.now(UTC)
    later = "2099-03-01T08:00:00Z"
    conn = db.connect(world.db_path)
    try:
        read_id = store.start_snooze_read(conn, target, "friday", STAND_IN, started)
        conn.execute("UPDATE steps SET snoozed_until = ? WHERE id = ?", (later, step["id"]))
        store.settle_snooze_read(conn, target, read_id, None, "no friday", datetime.now(UTC))
        assert store.get_step(conn, step["id"])["snoozed_until"] == later
        assert target not in store.snooze_reads(conn, datetime.now(UTC))
    finally:
        conn.close()


def test_a_read_that_lands_after_the_step_closed_still_sets_its_time(world: World, monkeypatch) -> None:
    step = snoozed_step(world, monkeypatch, lambda *args: READ)
    target = f"step:{step['id']}"
    conn = db.connect(world.db_path)
    try:
        read_id = store.start_snooze_read(conn, target, "friday", STAND_IN, datetime.now(UTC))
        conn.execute("UPDATE steps SET snoozed_until = ?, status = 'done' WHERE id = ?", (STAND_IN, step["id"]))
        store.settle_snooze_read(conn, target, read_id, "2099-01-03T08:00:00Z", "", datetime.now(UTC))
        # An Undo of the close reopens the step as it was, so it comes back sleeping until the time read.
        assert store.get_step(conn, step["id"])["snoozed_until"] == "2099-01-03T08:00:00Z"
    finally:
        conn.close()


def test_another_snooze_forgets_how_the_words_were_read(world: World, monkeypatch) -> None:
    step = snoozed_step(world, monkeypatch, lambda *args: READ)
    world.client.post(f"/api/steps/{step['id']}/snooze", json={"until": None}, headers=world.user())
    assert f"step:{step['id']}" not in state(world)["snooze_reads"]


def test_a_warning_snoozed_in_words_moves_to_the_time_read(world: World, monkeypatch) -> None:
    monkeypatch.setattr(snooze_words, "read", lambda *args: READ)
    response = world.client.post("/api/warnings/snooze", headers=world.user(),
                                 json={"key": "claude:work", "until": STAND_IN, "words": WORDS})
    assert response.status_code == 200, response.text
    shown = state(world)
    assert shown["snoozed_warnings"]["claude:work"] == "2099-01-02T07:55:00Z"
    assert shown["snooze_reads"]["warning:claude:work"]["status"] == "read"


def test_words_need_a_stand_in_time_a_known_timezone_and_the_user(world: World, monkeypatch) -> None:
    step = snoozed_step(world, monkeypatch, lambda *args: READ)
    url = f"/api/steps/{step['id']}/snooze"
    assert world.client.post(url, json={"until": None, "words": WORDS}, headers=world.user()).status_code == 400
    mars = {"text": "tomorrow", "tz": "Mars/Base"}
    assert world.client.post(url, json={"until": STAND_IN, "words": mars}, headers=world.user()).status_code in (400, 422)
    agent = TestClient(world.client.app, base_url=ORIGIN)
    assert agent.post(url, json={"until": STAND_IN, "words": WORDS}, headers=world.agent()).status_code in (400, 403)


def test_the_prompt_lists_the_coming_events_in_the_users_timezone() -> None:
    events = [
        Event("Standup", datetime(2026, 2, 2, 9, 0, tzinfo=UTC), datetime(2026, 2, 2, 9, 15, tzinfo=UTC), False),
        Event("Holiday", datetime(2026, 2, 3, 0, 0, tzinfo=UTC), datetime(2026, 2, 4, 0, 0, tzinfo=UTC), True),
    ]
    assert snooze_words.calendar_text(events, ZONE) == (
        "Calendar for the coming week:\n- Standup: Mon 02 Feb 10:00 to 10:15\n- Holiday: Tue 03 Feb, all day"
    )
    assert snooze_words.calendar_text([], ZONE) == "Calendar: no events known."


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
    assert snooze_words.read("Friday morning", ZONE, NOW, []) == datetime(2026, 2, 6, 8, 0, tzinfo=UTC)
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
    assert snooze_words.read("Friday morning", ZONE, NOW, []) == datetime(2026, 2, 6, 8, 0, tzinfo=UTC)
