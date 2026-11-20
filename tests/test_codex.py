from __future__ import annotations

import base64
import dataclasses
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cockpit import cli, codex, config, parts, usage
from tests.conftest import SESSION_A, World
from tests.test_api import put_card

NOW = datetime(2026, 1, 12, 10, 0, tzinfo=UTC)
SESSION = "a0a0a001-0000-4000-8000-000000000001"
CHILD = "a0a0a002-0000-4000-8000-000000000002"


def at(hours: float) -> str:
    return (NOW - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def meta(hours: float, source: Any, cwd: str = "/home/sam/dev/app") -> dict[str, Any]:
    return {"timestamp": at(hours), "type": "session_meta", "payload": {"id": SESSION, "cwd": cwd, "source": source}}


def count(hours: float, total_input: int, cached: int, output: int, reasoning: int = 0, used: float = 3.0,
          resets: datetime = NOW + timedelta(days=5)) -> dict[str, Any]:
    usage = {"input_tokens": total_input, "cached_input_tokens": cached, "output_tokens": output,
             "reasoning_output_tokens": reasoning, "total_tokens": total_input + output}
    return {"timestamp": at(hours), "type": "event_msg", "payload": {
        "type": "token_count", "info": {"total_token_usage": usage, "last_token_usage": usage},
        "rate_limits": {"limit_id": "codex", "primary": {"used_percent": used, "window_minutes": 10080,
                                                         "resets_at": int(resets.timestamp())},
                        "secondary": None, "plan_type": "pro"}}}


def rollout(home: Path, session_id: str, entries: list[dict[str, Any]]) -> Path:
    path = home / "sessions" / "2026" / "01" / "12" / f"rollout-2026-01-12T10-00-00-{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        for entry in entries:
            # Codex writes compact JSON, which the index looks for before parsing a line.
            handle.write(json.dumps(entry, separators=(",", ":")) + "\n")
    return path


def indexed(*homes: Path) -> codex.CodexIndex:
    index = codex.CodexIndex(homes)
    index.refresh(NOW)
    return index


def test_account_switch_drops_cached_week_until_a_new_reading(tmp_path, monkeypatch):
    address = "first@example.test"
    monkeypatch.setattr(codex, "email", lambda _home: address)
    monkeypatch.setattr(codex, "ask_limits", lambda _home, _command: _answer(70, NOW + timedelta(days=5)))
    rollout(tmp_path, SESSION, [meta(3, "exec"), count(3, 100, 0, 10, used=70)])
    index = codex.AccountIndex(tmp_path)
    index.refresh(NOW)
    assert index.week(NOW) is None
    index.ask("unused", NOW)
    assert index.week(NOW)["used"] == 70
    index.refresh(NOW)
    assert index.week(NOW)["used"] == 70
    address = "second@example.test"
    index.refresh(NOW)
    assert index.email == address
    assert index.week(NOW) is None
    index.refresh(NOW)
    assert index.week(NOW) is None
    rollout(tmp_path, SESSION, [count(1, 200, 0, 20, used=10)])
    index.refresh(NOW)
    assert index.week(NOW) is None
    monkeypatch.setattr(codex, "ask_limits", lambda _home, _command: _answer(10, NOW + timedelta(days=5)))
    index.ask("unused", NOW)
    assert index.week(NOW)["used"] == 10


@pytest.mark.parametrize("replay_hours", [3, 2])
@pytest.mark.parametrize("appended", [False, True])
def test_each_count_adds_what_grew_since_the_last(tmp_path: Path, replay_hours: int, appended: bool) -> None:
    rollout(tmp_path, SESSION, [
        meta(3, "exec"),
        count(3, 1000, 600, 50, reasoning=20),
        # Codex can repeat a count; it adds nothing.
        count(3, 1000, 600, 50, reasoning=20),
        count(2, 2500, 1800, 90, reasoning=30),
    ])
    index = codex.CodexIndex((tmp_path,))
    if appended:
        index.refresh(NOW)
    rollout(tmp_path, SESSION, [
        # An older or same-time lower snapshot is a replay, even after an incremental refresh.
        count(replay_hours, 1000, 600, 50, reasoning=20),
        count(2, 2500, 1800, 90, reasoning=30),
    ])
    index.refresh(NOW)
    summary = index.summary(NOW - timedelta(days=1), NOW, [])
    assert summary["total"] == {"fresh": 700, "cached": 1800, "output": 90, "reasoning": 30}
    [row] = summary["sessions"]
    assert (row["session_id"], row["source"], row["cwd"], row["calls"]) == (SESSION, "exec", "/home/sam/dev/app", 2)


def test_the_period_counts_what_grew_in_it(tmp_path: Path) -> None:
    rollout(tmp_path, SESSION, [meta(30, "cli"), count(30, 1000, 0, 10), count(2, 3000, 0, 40)])
    index = indexed(tmp_path)
    assert index.summary(NOW - timedelta(days=1), NOW, [])["total"]["fresh"] == 2000
    assert index.summary(NOW - timedelta(days=7), NOW, [])["total"]["fresh"] == 3000


def test_a_count_below_the_last_starts_again(tmp_path: Path) -> None:
    rollout(tmp_path, SESSION, [
        meta(3, "exec"), count(3, 5000, 0, 100), count(2, 800, 0, 20),
        # An old higher count after a reset keeps the reset's baseline too.
        count(3, 5000, 0, 100), count(1, 900, 0, 30),
    ])
    index = indexed(tmp_path)
    assert index.summary(NOW - timedelta(days=1), NOW, [])["total"]["fresh"] == 5900
    # A zero at the latest count's time still rebases a later count above the old total.
    rollout(tmp_path, SESSION, [count(1, 0, 0, 0), count(0.5, 1100, 0, 40)])
    index.refresh(NOW)
    assert index.summary(NOW - timedelta(days=1), NOW, [])["total"]["fresh"] == 7000


def test_a_subagent_session_says_so(tmp_path: Path) -> None:
    source = {"subagent": {"thread_spawn": {"parent_thread_id": SESSION, "depth": 1}}}
    rollout(tmp_path, CHILD, [meta(3, source), count(3, 100, 0, 10)])
    assert indexed(tmp_path).summary(NOW - timedelta(days=1), NOW, [])["sessions"][0]["source"] == "subagent"


def test_the_weekly_meter_is_the_latest_count(tmp_path: Path) -> None:
    rollout(tmp_path, SESSION, [meta(5, "exec"), count(5, 100, 0, 10, used=40.0), count(1, 200, 0, 20, used=42.0)])
    rollout(tmp_path, CHILD, [meta(3, "exec"), count(3, 100, 0, 10, used=41.0)])
    index = indexed(tmp_path)
    [account] = index.summary(NOW - timedelta(days=1), NOW, [])["accounts"]
    week = account["week"]
    assert (week["used"], week["plan"], week["captured_at"]) == (42.0, "pro", at(1))
    assert week["resets_at"] == (NOW + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Two of the week's seven days are gone; 42 % used is near the pace, as the status line counts it.
    assert (week["elapsed"], week["pace"]) == (29, "near")
    # Once the week has reset, its use is unknown until Codex counts again.
    later = index.summary(NOW - timedelta(days=1), NOW + timedelta(days=6), [])["accounts"][0]["week"]
    assert (later["used"], later["elapsed"], later["pace"]) == (None, None, None)


def test_the_usage_display_gets_each_accounts_week(tmp_path: Path) -> None:
    """The display at the foot of the page (SPEC UI-57) lists every Codex account's latest week, with its pace, the same
    week the Usage page shows, and an account that has not read its meter yet with no week."""
    first, second = tmp_path / ".codex", tmp_path / ".codex-2"
    rollout(first, SESSION, [meta(5, "exec"), count(5, 100, 0, 10, used=40.0), count(1, 200, 0, 20, used=42.0)])
    (second / "sessions").mkdir(parents=True)
    index = indexed(first, second)
    weeks = index.weeks(NOW)
    assert [row["folder"] for row in weeks] == [".codex"]
    assert weeks[0]["week"] == index.summary(NOW - timedelta(days=1), NOW, [])["accounts"][0]["week"]
    assert (weeks[0]["week"]["used"], weeks[0]["week"]["pace"]) == (42.0, "near")


def test_a_codex_week_counts_to_the_accounts_last_use(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex's week counts as Claude's does (usage.week_counted), by the account's address (SPEC UI-75)."""
    home = tmp_path / ".codex"
    rollout(home, SESSION, [meta(1, "exec"), count(1, 100, 0, 10, used=40.0)])
    index = indexed(home)
    [account] = index.accounts
    account.email = "sam@example.com"
    resets_at = datetime.fromisoformat(account.week(NOW)["resets_at"]).timestamp()
    setup = dataclasses.replace(
        config.read_setup(config.SETUP_PATH, parts.setup_tables()), last_use={"sam@example.com": resets_at - 86400}
    )
    monkeypatch.setattr(config, "setup", lambda: setup)
    week = account.week(NOW)
    assert week["counted_until"] is not None
    assert week["span"] < 100
    assert week == week | usage.week_counted("sam@example.com", 40.0, resets_at, NOW.timestamp())


def test_the_usage_display_has_each_accounts_week_before_the_first_count(tmp_path: Path) -> None:
    """The first full count runs for minutes after a restart; the latest meter comes from the end of the newest
    files at once, and the full count, reading older lines, keeps it."""
    old = rollout(tmp_path, SESSION, [meta(5, "exec"), count(5, 100, 0, 10, used=40.0)])
    newest = rollout(tmp_path, CHILD, [meta(3, "exec"), count(3, 100, 0, 10, used=41.0)])
    # A long line before the end, as Codex writes megabytes of a session between its counts.
    with newest.open("a") as handle:
        handle.write(json.dumps({"timestamp": at(2), "type": "response_item", "payload": {"text": "x" * (2 * codex.LATEST_TAIL_BYTES)}}) + "\n")
        handle.write(json.dumps(count(1, 200, 0, 20, used=42.0), separators=(",", ":")) + "\n")
        handle.write('{"timestamp": "unfinished')
    os.utime(old, (NOW.timestamp() - 3600, NOW.timestamp() - 3600))
    index = codex.CodexIndex((tmp_path,))
    [row] = index.weeks(NOW)
    assert (row["week"]["used"], row["week"]["captured_at"]) == (42.0, at(1))
    index.refresh(NOW)
    assert index.weeks(NOW)[0]["week"]["captured_at"] == at(1)


def test_the_index_reads_only_what_was_appended(tmp_path: Path) -> None:
    path = rollout(tmp_path, SESSION, [meta(3, "exec"), count(3, 1000, 0, 10)])
    index = indexed(tmp_path)
    rollout(tmp_path, SESSION, [count(2, 1500, 0, 20)])
    with path.open("a") as handle:
        handle.write('{"timestamp": "unfinished')
    index.refresh(NOW)
    assert index.summary(NOW - timedelta(days=1), NOW, [])["total"]["fresh"] == 1500


def called(stamp: str, total_input: int, last_input: int) -> dict[str, Any]:
    """A call at `stamp` whose own input, cached part included, was `last_input`."""
    entry = count(0, total_input, 0, 10)
    entry["payload"]["info"]["last_token_usage"] = {"input_tokens": last_input, "cached_input_tokens": last_input // 2,
                                                    "output_tokens": 10, "reasoning_output_tokens": 0, "total_tokens": last_input + 10}
    return entry | {"timestamp": stamp}


def compacted(hours: float) -> dict[str, Any]:
    return {"timestamp": at(hours), "type": "compacted", "payload": {"message": "A summary of the work so far.", "replacement_history": []}}


def test_the_cache_is_the_input_and_time_of_the_sessions_latest_call(tmp_path: Path) -> None:
    first, second = tmp_path / "codex", tmp_path / "codex-2"
    rollout(first, SESSION, [meta(3, "exec"), called(at(3), 50_000, 50_000), called(at(1), 135_000, 85_000)])
    # The same session read on the other account, earlier, does not win.
    rollout(second, SESSION, [meta(5, "exec"), called(at(5), 40_000, 40_000)])
    rollout(second, CHILD, [child_meta(4), called(at(4), 30_000, 30_000)])
    index = indexed(first, second)
    assert index.caches() == {
        SESSION: {"tokens": 85_000, "warm_until": "2026-01-12T11:00:00Z", "lifetime_minutes": 120,
                  "lifetime_estimated": True, "cold_usd": None, "model": None},
        CHILD: {"tokens": 30_000, "warm_until": "2026-01-12T08:00:00Z", "lifetime_minutes": 120,
                "lifetime_estimated": True, "cold_usd": None, "model": None},
    }


def test_a_compaction_clears_the_cache_until_the_next_call(tmp_path: Path) -> None:
    rollout(tmp_path, SESSION, [meta(3, "exec"), called(at(2), 85_000, 85_000)])
    index = indexed(tmp_path)
    assert index.caches()[SESSION]["tokens"] == 85_000
    # Codex follows a compaction with a count whose own input is 0; the compaction alone clears the cache.
    rollout(tmp_path, SESSION, [compacted(1)])
    index.refresh(NOW)
    assert index.caches() == {}
    rollout(tmp_path, SESSION, [called(at(0.5), 105_000, 20_000)])
    index.refresh(NOW)
    assert (index.caches()[SESSION]["tokens"], index.caches()[SESSION]["warm_until"]) == (20_000, "2026-01-12T11:30:00Z")


def test_a_compaction_in_the_newer_copy_of_a_session_on_another_account_leaves_no_cache(tmp_path: Path) -> None:
    first, second = tmp_path / "codex", tmp_path / "codex-2"
    rollout(first, SESSION, [meta(3, "exec"), called(at(3), 85_000, 85_000)])
    rollout(second, SESSION, [meta(2, "exec"), called(at(2), 100_000, 100_000), compacted(1)])
    assert indexed(first, second).caches() == {}


def test_a_count_without_the_calls_own_tokens_still_counts(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    def without_last(hours: float, total_input: int) -> dict[str, Any]:
        entry = count(hours, total_input, 0, 10)
        del entry["payload"]["info"]["last_token_usage"]
        return entry

    path = rollout(tmp_path, SESSION, [
        meta(3, "exec"), called(at(2), 85_000, 85_000), without_last(1, 170_000), without_last(0.5, 200_000),
    ])
    index = indexed(tmp_path)
    assert index.summary(NOW - timedelta(days=1), NOW, [])["total"]["fresh"] == 200_000
    assert index.caches() == {}
    errors = [record for record in caplog.records if "last_token_usage" in record.getMessage()]
    assert [(record.levelname, path.name in record.getMessage()) for record in errors] == [("ERROR", True)]


def test_a_codex_step_shows_the_cache_in_the_page_and_in_cockpit_get(world: World) -> None:
    def ago(minutes: int) -> str:
        return (datetime.now(UTC) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def cache_of(state: dict[str, Any]) -> dict[str, Any] | None:
        [step] = [step for group in ("queue", "ready_next", "snoozed", "with_agents") for step in state[group]]
        return step["cache"]

    put_card(world, "review@a")
    world.cache.sessions = [dataclasses.replace(world.cache.sessions[0], provider="codex")]
    home = world.codex_index.accounts[0].home
    expired = ago(180)
    rollout(home, SESSION_A, [called(expired, 85_000, 85_000)])
    world.codex_index.refresh(datetime.now(UTC))
    state = world.client.get("/api/state").json()
    until = (datetime.fromisoformat(expired) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert (cache_of(state)["warm_until"], cache_of(state)["minutes_left"], cache_of(state)["cold_usd"]) == (until, 0, None)
    assert "     cache likely cold, 85K tokens\n" in cli.summary(state)
    stamp = ago(1)
    rollout(home, SESSION_A, [called(stamp, 170_000, 85_000)])
    world.codex_index.refresh(datetime.now(UTC))
    state = world.client.get("/api/state").json()
    cache = cache_of(state)
    assert (cache["tokens"], cache["lifetime_minutes"], cache["lifetime_estimated"]) == (85_000, 120, True)
    assert cache["warm_until"] == (datetime.fromisoformat(stamp) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # The test's own pace sets the minutes left, so a range: about 119 when it runs at full speed.
    assert 105 <= cache["minutes_left"] <= 120
    assert f"     cache likely warm {cache['minutes_left']} more min, 85K tokens\n" in cli.summary(state)


def child_meta(hours: float) -> dict[str, Any]:
    return {"timestamp": at(hours), "type": "session_meta", "payload": {"id": CHILD, "cwd": "/home/sam/dev/app", "source": "exec"}}


def test_each_rise_of_the_meter_is_shared_out_over_the_calls_since_the_last_by_tokens(tmp_path: Path) -> None:
    rollout(tmp_path, SESSION, [
        meta(5, "exec"),
        # The first reading on record is where the meter stood; the calls before it are not counted.
        count(5, 100, 0, 0, used=10.0),
        count(4, 400, 0, 0, used=10.0),
        # A session running beside another can read the meter a percent behind it, which adds nothing.
        count(3, 600, 0, 0, used=11.0),
    ])
    rollout(tmp_path, CHILD, [
        child_meta(4),
        # Up 2 since 10 %: 300 tokens of SESSION's and 100 of this one's.
        count(3.5, 100, 0, 0, used=12.0),
        # Up 1: 200 of SESSION's since the last rise and 200 of this one's.
        count(2, 300, 0, 0, used=13.0),
    ])
    index = indexed(tmp_path)
    summary = index.summary(NOW - timedelta(days=1), NOW, [])
    assert {row["session_id"]: row["week_percent"] for row in summary["sessions"]} == {SESSION: 2.0, CHILD: 1.0}
    assert summary["week_percent"] == 3.0
    # The hours of the graph per day carry the same shares.
    hours = {hour["hour"]: hour for hour in index.hours(NOW - timedelta(days=1), [])}
    assert (hours["2026-01-12T06:00:00Z"]["calls"], hours["2026-01-12T06:00:00Z"]["week_percent"]) == (2, 2.0)
    assert hours["2026-01-12T06:00:00Z"]["tokens"]["fresh"] == 400
    assert sum(hour["week_percent"] for hour in hours.values()) == 3.0


def test_a_new_week_starts_from_nothing_and_a_late_reading_of_the_last_adds_nothing(tmp_path: Path) -> None:
    week, early = NOW + timedelta(days=1), NOW + timedelta(days=6)
    rollout(tmp_path, SESSION, [
        meta(10, "exec"),
        count(10, 100, 0, 0, used=50.0, resets=week),
        # The same week, its reset named a few seconds off.
        count(9, 300, 0, 0, used=52.0, resets=week + timedelta(seconds=4)),
        # Codex reset the week early: the new one starts from 0 %.
        count(8, 400, 0, 0, used=1.0, resets=early),
    ])
    rollout(tmp_path, CHILD, [child_meta(7), count(7, 100, 0, 0, used=52.0, resets=week)])
    summary = indexed(tmp_path).summary(NOW - timedelta(days=1), NOW, [])
    assert {row["session_id"]: row["week_percent"] for row in summary["sessions"]} == {SESSION: 3.0, CHILD: 0.0}
    # The period holds two of Codex's weeks: the page says it allowed 200 %.
    assert summary["weeks"] == 2


def test_a_rise_goes_to_the_calls_of_its_own_week_and_same_moment_readings_count_once(tmp_path: Path) -> None:
    """Exclude old-week calls from a new week's rise, and share simultaneous readings' highest rise regardless of read order."""
    week, early = NOW + timedelta(days=1), NOW + timedelta(days=6)
    rollout(tmp_path, SESSION, [
        meta(10, "exec"),
        count(10, 100, 0, 0, used=10.0, resets=week),
        count(9, 1100, 0, 0, used=10.0, resets=week),
        count(5, 1200, 0, 0, used=11.0, resets=week),
    ])
    # Counts are running totals: each call adds 100 tokens but the one at minute 9, which adds 1,000.
    rollout(tmp_path, CHILD, [
        child_meta(8),
        count(8, 100, 0, 0, used=1.0, resets=early),
        count(5, 200, 0, 0, used=12.0, resets=week),
    ])
    summary = indexed(tmp_path).summary(NOW - timedelta(days=1), NOW, [])
    shares = {row["session_id"]: row["week_percent"] for row in summary["sessions"]}
    # The new week's 1 % is the child's; the old week's rise from 10 % to 12 % goes over the 1,000 tokens at
    # minute 9 and the two calls at minute 5, 1,200 tokens in all.
    assert shares == {SESSION: round(2 * 1100 / 1200, 3), CHILD: round(1 + 2 * 100 / 1200, 3)}
    assert summary["week_percent"] == 3.0


def test_a_reading_that_does_not_parse_skips_its_line_and_the_rest_still_counts(tmp_path: Path) -> None:
    broken = count(9, 200, 0, 0, used=12.0)
    broken["payload"]["rate_limits"]["primary"]["resets_at"] = None
    rollout(tmp_path, SESSION, [meta(10, "exec"), count(10, 100, 0, 0, used=10.0), broken, count(8, 300, 0, 0, used=12.0)])
    summary = indexed(tmp_path).summary(NOW - timedelta(days=1), NOW, [])
    assert (summary["calls"], summary["week_percent"]) == (2, 2.0)


def test_the_meter_reads_only_the_codex_limit(tmp_path: Path) -> None:
    other = count(1, 200, 0, 20, used=0.0, resets=NOW + timedelta(hours=1))
    other["payload"]["rate_limits"]["limit_id"] = "codex_bengalfox"
    rollout(tmp_path, SESSION, [meta(3, "exec"), count(3, 100, 0, 10, used=40.0), other])
    summary = indexed(tmp_path).summary(NOW - timedelta(days=1), NOW, [])
    assert summary["accounts"][0]["week"]["used"] == 40.0
    assert summary["calls"] == 2


def signed_in(home: Path, email: str) -> None:
    """A Codex sign-in whose ID token names `email`; the index reads nothing else of it."""
    claims = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {
        "id_token": f"header.{claims}.signature", "access_token": "secret", "refresh_token": "secret"}}))


def test_each_account_keeps_its_own_meter_and_the_use_adds_up(tmp_path: Path, monkeypatch) -> None:
    """Each Codex account's meter rises with only its own calls."""
    first, second, unused = tmp_path / ".codex", tmp_path / ".codex-2", tmp_path / ".codex-3"
    signed_in(first, "sam@example.com")
    signed_in(second, "sam.work@example.com")
    rollout(first, SESSION, [meta(5, "exec"), count(5, 100, 0, 0, used=10.0), count(2, 400, 0, 0, used=13.0)])
    rollout(second, CHILD, [child_meta(4), count(4, 100, 0, 0, used=0.0), count(1, 200, 0, 0, used=1.0)])
    index = indexed(first, second, unused)
    monkeypatch.setattr(codex, "ask_limits", lambda home, _command: _answer(13 if home == first else 1, NOW + timedelta(days=5)))
    for account in index.accounts[:2]:
        account.ask("unused", NOW)
    summary = index.summary(NOW - timedelta(days=1), NOW, [])
    assert [(account["email"], account["folder"], account["week"]["used"]) for account in summary["accounts"]] == [
        ("sam@example.com", ".codex", 13.0), ("sam.work@example.com", ".codex-2", 1.0)]
    # The first account's 3 % rise is its own session's, the second's 1 % its own.
    assert [(row["session_id"], row["account"], row["week_percent"]) for row in summary["sessions"]] == [
        (SESSION, "sam@example.com", 3.0), (CHILD, "sam.work@example.com", 1.0)]
    assert (summary["week_percent"], summary["weeks"], summary["calls"]) == (4.0, 2, 4)
    assert summary["total"]["fresh"] == 600
    assert sum(hour["week_percent"] for hour in index.hours(NOW - timedelta(days=1), [])) == 4.0


def test_an_account_without_a_sign_in_is_named_by_its_folder(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    rollout(home, SESSION, [meta(3, "exec"), count(3, 100, 0, 10, used=40.0)])
    (home / "auth.json").write_text("{not json")
    summary = indexed(home).summary(NOW - timedelta(days=1), NOW, [])
    assert [(account["email"], account["folder"]) for account in summary["accounts"]] == [("", ".codex")]
    assert summary["sessions"][0]["account"] == ".codex"


def test_banked_resets_are_read_from_codex_and_listed_with_the_week_they_add_no_room_to(tmp_path: Path) -> None:
    """90 % of a week with 3 days left is over its pace with two banked resets as without: spending one is the user's
    choice. The week lists them for the usage display's hover."""
    account = codex.AccountIndex(tmp_path)
    resets_at = NOW + timedelta(days=3)
    account._week = {"used": 90.0, "resets_at": resets_at.strftime("%Y-%m-%dT%H:%M:%SZ"), "plan": "pro", "captured_at": "x"}
    assert (account.week(NOW)["pace"], account.week(NOW)["resets"]) == ("over", None)
    credit = {"title": "Full reset", "status": "available", "resetType": "codexRateLimits", "expiresAt": (NOW + timedelta(days=30)).timestamp()}
    answer = codex.parse_resets({"credits": [credit, credit, credit | {"status": "used"}, credit | {"resetType": "other"}]}, NOW)
    account.set_resets(account.email, answer)
    week = account.week(NOW)
    assert (week["used"], week["pace"]) == (90.0, "over")
    assert len(week["resets"]["grants"]) == 2
    # A folder signed in to another account starts without the old account's resets.
    account.email = "other@example.com"
    assert account.week(NOW)["resets"] is None


def test_reading_banked_resets_asks_a_codex_app_server(tmp_path):
    """The real exchange with a stand-in server that answers each question as Codex's does and waits for more."""
    fake = tmp_path / "codex"
    fake.write_text(
        "#!/bin/sh\nread a\necho '{\"id\":1,\"result\":{}}'\nread b\nread c\n"
        "echo '{\"id\":2,\"result\":{\"rateLimitResetCredits\":{\"credits\":[{\"title\":\"Full reset\",\"status\":\"available\",\"resetType\":\"codexRateLimits\",\"expiresAt\":1893456000}]}}}'\nsleep 60\n"
    )
    fake.chmod(0o755)
    started = time.monotonic()
    answer = codex.read_resets(tmp_path, str(fake), NOW)
    assert answer["grants"][0]["left"] == 1
    # The wait does not last until the stand-in's own child, the sleep, ends, as `codex` also runs its server as a child.
    assert time.monotonic() - started < 30


def test_a_codex_that_gives_no_resets_answer_keeps_the_last_reading(tmp_path, caplog):
    fake = tmp_path / "codex"
    fake.write_text("#!/bin/sh\nread a\necho 'not json'\n")
    fake.chmod(0o755)
    assert codex.read_resets(tmp_path, str(fake), NOW) is None
    assert codex.read_resets(tmp_path, str(tmp_path / "missing"), NOW) is None
    assert "banked resets" in caplog.text


def _answer(used: float, resets_at: datetime) -> dict:
    window = {"usedPercent": used, "windowDurationMins": codex.WEEK_MINUTES, "resetsAt": int(resets_at.timestamp())}
    return {"result": {"rateLimits": {"limitId": "codex", "planType": "pro", "primary": window, "secondary": None},
                       "rateLimitResetCredits": {"credits": []}}}


def _signed_in(home: Path, address: str) -> None:
    def part(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    (home / "auth.json").write_text(json.dumps({"tokens": {"id_token": f"{part({'alg': 'none'})}.{part({'email': address})}.x"}}))


def test_a_folder_signed_in_to_another_account_drops_the_old_account_s_meter(tmp_path, monkeypatch):
    """SPEC UI-77: the page must not put the old account's use under the new address."""
    _signed_in(tmp_path, "old@example.com")
    account = codex.AccountIndex(tmp_path)
    resets_at = NOW + timedelta(days=3)
    monkeypatch.setattr(codex, "ask_limits", lambda home, command: _answer(90.0, resets_at))
    account.ask("codex", NOW)
    assert account.week(NOW)["used"] == 90.0
    # Sign-in again removes the file first; the meter stays for the same account coming back.
    (tmp_path / "auth.json").unlink()
    account.refresh(NOW)
    _signed_in(tmp_path, "old@example.com")
    account.refresh(NOW)
    assert account.week(NOW)["used"] == 90.0

    (tmp_path / "auth.json").unlink()
    account.refresh(NOW)
    _signed_in(tmp_path, "new@example.com")
    account.refresh(NOW)
    assert account.email == "new@example.com"
    assert account.week(NOW) is None


def test_asking_codex_reads_the_weekly_meter_of_the_account_signed_in(tmp_path, monkeypatch):
    """A restart reads the last session file's meter, which can be the old account's; the first answer replaces it."""
    _signed_in(tmp_path, "new@example.com")
    account = codex.AccountIndex(tmp_path)
    account._week = {"used": 90.0, "resets_at": (NOW + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"), "plan": "pro",
                     "captured_at": "2026-01-01T00:00:00.000Z"}
    resets_at = NOW + timedelta(days=6)
    monkeypatch.setattr(codex, "ask_limits", lambda home, command: _answer(10.0, resets_at))
    account.ask("codex", NOW)
    week = account.week(NOW)
    assert (week["used"], week["plan"]) == (10.0, "pro")
    assert week["resets_at"] == resets_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert account.email == "new@example.com"


def test_an_answer_without_a_readable_meter_keeps_the_one_known(tmp_path, monkeypatch, caplog):
    _signed_in(tmp_path, "new@example.com")
    account = codex.AccountIndex(tmp_path)
    resets_at = NOW + timedelta(days=6)
    monkeypatch.setattr(codex, "ask_limits", lambda home, command: _answer(10.0, resets_at))
    account.ask("codex", NOW)
    monkeypatch.setattr(codex, "ask_limits", lambda home, command: {"result": {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 1}, "secondary": None}, "rateLimitResetCredits": {"credits": []}}})
    account.ask("codex", NOW + timedelta(minutes=15))
    assert account.week(NOW)["used"] == 10.0
    assert "weekly meter" in caplog.text


def on_hub(home: Path) -> Path:
    """A Codex folder that runs on the install's model hub, as `model_provider` in its config.toml says."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text('model_provider = "hub"\n\n[model_providers.hub]\nname = "hub"\nbase_url = "http://hub.test/v1"\n')
    return home


def hub_count(hours: float, total_input: int) -> dict[str, Any]:
    """A token count as Codex on a provider of its own logs it: without rate limits."""
    entry = count(hours, total_input, 0, 0)
    entry["payload"]["rate_limits"] = None
    return entry


def reading(account: str, hours: float, used: float, resets: datetime) -> codex.HubReading:
    return codex.HubReading(account, (NOW - timedelta(hours=hours)).timestamp(), used, resets.timestamp())


def test_a_folder_on_the_hub_shares_out_the_hubs_readings(tmp_path: Path) -> None:
    """The user's Codex runs through the hub and its session files carry no meter, so its share of the weekly limit read
    0 % everywhere; each rise of a hub account's week is shared out over the calls since its rise before."""
    home = on_hub(tmp_path / ".codex")
    rollout(home, SESSION, [meta(5, "exec"), hub_count(5, 1000), hub_count(3, 4000), hub_count(1.5, 4500)])
    index = indexed(home)
    week, next_week = NOW + timedelta(days=3), NOW + timedelta(days=10)
    hub = [
        # The first reading on record is where counting starts, so its 10 % counts toward no call.
        reading("codex/one", 6, 10, week),
        # 4 % over the call at 5 h; 6 % over the call at 3 h.
        reading("codex/one", 4, 14, week + timedelta(seconds=20)),
        reading("codex/one", 2, 20, week),
        # A lower reading of the same week adds nothing.
        reading("codex/one", 1.8, 19, week),
        # The account's next week starts from 0 %: 3 % over the call at 1.5 h.
        reading("codex/one", 1, 3, next_week),
        # A second account's 2 % rise is shared by tokens over the calls at 5 h (1,000) and 3 h (3,000).
        reading("codex/two", 6, 50, week),
        reading("codex/two", 2.5, 52, week),
    ]
    summary = index.summary(NOW - timedelta(days=1), NOW, hub)
    assert summary["week_percent"] == 15
    assert summary["accounts"] == []
    assert summary["weeks"] == 3
    assert [(hour["hour"], hour["week_percent"]) for hour in index.hours(NOW - timedelta(days=1), hub)] == [
        ("2026-01-12T05:00:00Z", 4.5), ("2026-01-12T07:00:00Z", 7.5), ("2026-01-12T08:00:00Z", 3)]
    # New readings share out again.
    later = [*hub, reading("codex/one", 0.5, 5, next_week)]
    assert index.summary(NOW - timedelta(days=1), NOW, later)["week_percent"] == 15
    rollout(home, SESSION, [hub_count(0.75, 5500)])
    index.refresh(NOW)
    assert index.summary(NOW - timedelta(days=1), NOW, later)["week_percent"] == 17


def test_a_hub_weeks_first_rise_counts_only_the_calls_after_the_week_before_reset(tmp_path: Path) -> None:
    """The calls between a week's last rise and its reset count toward nothing: before, the next week's first rise
    was shared over them too."""
    home = on_hub(tmp_path / ".codex")
    rollout(home, SESSION, [meta(8, "exec"), hub_count(7, 1000), hub_count(5.5, 2000), hub_count(4, 3000)])
    resets = NOW - timedelta(hours=5)
    hub = [
        reading("codex/one", 8, 40, resets),
        # 10 % over the call at 7 h.
        reading("codex/one", 6, 50, resets),
        # The week resets 5 h ago; the next one is first read at 0 %, then rises 2 % over the call at 4 h alone.
        reading("codex/one", 3, 0, resets + timedelta(days=7)),
        reading("codex/one", 2, 2, resets + timedelta(days=7)),
    ]
    assert [(hour["hour"], hour["week_percent"]) for hour in indexed(home).hours(NOW - timedelta(days=1), hub)] == [
        ("2026-01-12T03:00:00Z", 10), ("2026-01-12T04:00:00Z", 0), ("2026-01-12T06:00:00Z", 2)]


def test_a_folder_with_its_own_sign_in_keeps_its_meter(tmp_path: Path) -> None:
    rollout(tmp_path, SESSION, [meta(5, "exec"), count(5, 1000, 0, 0, used=10), count(3, 2000, 0, 0, used=14)])
    hub = [reading("codex/one", 6, 10, NOW + timedelta(days=3)), reading("codex/one", 1, 90, NOW + timedelta(days=3))]
    assert indexed(tmp_path).summary(NOW - timedelta(days=1), NOW, hub)["week_percent"] == 4


def test_hub_weeks_count_each_accounts_weeks_in_the_period() -> None:
    first, second = NOW + timedelta(days=1), NOW + timedelta(days=8)
    hub = [
        # Codex reset this week early: the account's next week was first read before this one's end.
        reading("codex/one", 200, 10, NOW + timedelta(days=5)),
        reading("codex/one", 40, 5, first),
        reading("codex/one", 30, 9, first + timedelta(minutes=30)),
        reading("codex/two", 10, 1, second),
    ]
    count_in = lambda days: codex._hub_weeks(hub, (NOW - timedelta(days=days)).timestamp(), NOW.timestamp())
    # The first week ended when the next was first read, 40 hours ago.
    assert count_in(1) == 2
    assert count_in(2) == 3


def test_a_hub_reading_goes_to_the_nearest_week(tmp_path: Path) -> None:
    """A new week first said to reset just over SAME_WEEK_SECONDS after the old one, then a little earlier, is still
    the new week."""
    home = on_hub(tmp_path / ".codex")
    rollout(home, SESSION, [meta(5, "exec"), hub_count(5, 1000), hub_count(3, 2000)])
    week = NOW + timedelta(days=3)
    new_week = week + timedelta(seconds=codex.SAME_WEEK_SECONDS + 1)
    hub = [reading("codex/one", 6, 50, week), reading("codex/one", 4, 3, new_week),
           reading("codex/one", 2, 7, new_week - timedelta(seconds=2))]
    assert indexed(home).summary(NOW - timedelta(days=1), NOW, hub)["week_percent"] == 7


def test_hub_weeks_end_when_the_latest_reading_says() -> None:
    resets = NOW - timedelta(hours=2)
    hub = [reading("codex/one", 100, 10, resets), reading("codex/one", 50, 20, resets + timedelta(hours=0.5))]
    since = (NOW - timedelta(hours=1.75)).timestamp()
    assert codex._hub_weeks(hub, since, NOW.timestamp()) == 1


@pytest.mark.parametrize("config_text", ['model_providers = 42\nmodel_provider = "hub"\n', 'model_provider = ["hub"]\n'])
def test_a_provider_codex_cannot_read_is_no_provider(tmp_path: Path, config_text: str) -> None:
    (tmp_path / "config.toml").write_text(config_text)
    assert codex.on_own_provider(tmp_path) is False
