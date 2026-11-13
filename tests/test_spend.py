from __future__ import annotations

import json
import logging
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cockpit import cli, config, db, spend, store
from cockpit.app import create_app
from tests.conftest import ORIGIN, SESSION_A, SESSION_B, World
from tests.test_api import put_card

NOW = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
SESSION = "33333333-3333-4333-8333-333333333333"
COPY = "44444444-4444-4444-8444-444444444444"
FOLDER = "-home-sam-dev-app"


def at(minutes: int) -> str:
    """A time `minutes` after 10:00 on NOW's day, as Claude Code writes it."""
    return (NOW.replace(hour=10) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def usage(fresh: int = 0, write_5m: int = 0, write_1h: int = 0, read: int = 0, output: int = 0) -> dict[str, Any]:
    return {
        "input_tokens": fresh, "cache_creation_input_tokens": write_5m + write_1h, "cache_read_input_tokens": read,
        "output_tokens": output, "service_tier": "standard",
        "cache_creation": {"ephemeral_1h_input_tokens": write_1h, "ephemeral_5m_input_tokens": write_5m},
    }


def call(message_id: str, minutes: int, tokens: dict[str, Any], content: list[dict[str, Any]] | None = None,
         model: str = "claude-opus-5-5") -> dict[str, Any]:
    """An assistant entry with its keys in the order Claude Code writes them."""
    return {
        "parentUuid": "p", "isSidechain": False,
        "message": {
            "model": model, "id": message_id, "type": "message", "role": "assistant",
            "content": content or [{"type": "text", "text": "done"}], "stop_reason": "end_turn", "usage": tokens,
        },
        "type": "assistant", "uuid": f"u-{message_id}", "timestamp": at(minutes), "cwd": "/home/sam/dev/app",
    }


def asks_tool(entry: dict[str, Any]) -> dict[str, Any]:
    """The call, ending its reply by asking for a tool's result rather than ending the turn."""
    return entry | {"message": entry["message"] | {"stop_reason": "tool_use"}}


def peer(minutes: int, text: str) -> dict[str, Any]:
    """A message Cockpit or another session sent over the session's socket."""
    return prompt(minutes, [{"type": "text", "text": f"Another Claude session sent a message:\n{text}"}], isMeta=True)


def skill_call(message_id: str, minutes: int, skill: str) -> dict[str, Any]:
    block = {"type": "tool_use", "id": f"toolu_{message_id}", "name": "Skill", "input": {"skill": skill}}
    return call(message_id, minutes, usage(output=100), [block])


def prompt(minutes: int, text: Any, **extra: Any) -> dict[str, Any]:
    return {
        "parentUuid": "p", "isSidechain": False, "type": "user", "message": {"role": "user", "content": text},
        "uuid": f"prompt-{minutes}", "timestamp": at(minutes), "cwd": "/home/sam/dev/app", **extra,
    }


def tool_result(minutes: int) -> dict[str, Any]:
    content = [{"tool_use_id": "toolu_x", "type": "tool_result", "content": "ok"}]
    return prompt(minutes, content)


def write(path: Path, entries: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, separators=(",", ":")) + "\n")
    return path


def transcript(projects: Path, session_id: str, entries: list[dict[str, Any]]) -> Path:
    return write(projects / FOLDER / f"{session_id}.jsonl", entries)


def subagent(projects: Path, session_id: str, name: str, entries: list[dict[str, Any]]) -> Path:
    return write(projects / FOLDER / session_id / "subagents" / f"agent-{name}.jsonl", entries)


def index_of(projects: Path) -> spend.SpendIndex:
    index = spend.SpendIndex(projects)
    index.refresh(NOW)
    return index


def session_row(summary: dict[str, Any], session_id: str) -> dict[str, Any]:
    return next(row for row in summary["sessions"] if row["session_id"] == session_id)


def test_each_kind_of_token_costs_its_list_price(tmp_path: Path) -> None:
    million = 1_000_000
    transcript(tmp_path, SESSION, [
        prompt(0, "Fix the parser"),
        call("msg_1", 1, usage(fresh=million, write_5m=million, write_1h=million, read=million, output=million)),
    ])
    use = index_of(tmp_path).summary(NOW - timedelta(days=7))["total"]
    # Opus 5.5: $4 input, 1.25 times that for a 5-minute cache write and 2 times for an hour, $0.20 a cache read,
    # $20 output.
    assert use["tokens"] == {"fresh": million, "cache_write": 2 * million, "cache_read": million, "output": million}
    assert use["usd_by_kind"] == {"fresh": 4.0, "cache_write": 13.0, "cache_read": 0.2, "output": 20.0}
    assert use["usd"] == 37.2
    assert use["week_percent"] == round(37.2 / spend.WEEK_LIST_USD * 100, 2)
    assert use["plan_usd"] == round(37.2 / spend.WEEK_LIST_USD * 100 * spend.PLAN_USD_PER_PERCENT, 2)
    assert use["calls"] == 1


def test_fable_5_2_costs_its_own_list_price(tmp_path: Path) -> None:
    million = 1_000_000
    transcript(tmp_path, SESSION, [
        prompt(0, "Fix the parser"),
        call("msg_1", 1, usage(fresh=million, read=million, output=million), model="claude-fable-5-2"),
    ])
    use = index_of(tmp_path).summary(NOW - timedelta(days=7))["total"]
    # Fable 5.2: $8 input, $0.20 a cache read, $40 output, below Fable 5.1's $10, $0.25 and $50.
    assert use["usd_by_kind"] == {"fresh": 8.0, "cache_write": 0.0, "cache_read": 0.2, "output": 40.0}


def test_a_haiku_5_5_call_with_a_prompt_over_100_000_tokens_costs_five_times_as_much(tmp_path: Path) -> None:
    million = 1_000_000

    def haiku(message_id: str, minutes: int, tokens: dict[str, Any]) -> dict[str, Any]:
        return call(message_id, minutes, tokens, model="claude-haiku-5-5")

    def total(projects: Path, entries: list[dict[str, Any]]) -> float:
        """The cost of a session whose subagent made the calls in `entries`."""
        transcript(projects, SESSION, [prompt(0, "Find the config file")])
        subagent(projects, SESSION, "a1", entries)
        return index_of(projects).summary(NOW - timedelta(days=7))["total"]["usd"]

    # A prompt is the input read fresh, written to the cache and read from it. Haiku 5.5 up to 100,000 tokens of it:
    # $0.10 input, $0.01 a cache read, $0.50 output.
    short = usage(fresh=50_000, write_5m=25_000, read=25_000, output=million)
    assert total(tmp_path / "short", [haiku("msg_1", 1, short)]) == round(
        (50_000 + 25_000 * 1.25) * 0.10 / 1e6 + 25_000 * 0.01 / 1e6 + 0.50, 2)
    # Above 100,000 tokens: $0.50 input, $0.05 a cache read, $2.50 output.
    long = usage(fresh=50_001, write_5m=25_000, read=25_000, output=million)
    assert total(tmp_path / "long", [haiku("msg_1", 1, long)]) == round(
        (50_001 + 25_000 * 1.25) * 0.50 / 1e6 + 25_000 * 0.05 / 1e6 + 2.50, 2)
    # A later entry of a subagent's reply counts the output it adds at the price of the whole call's prompt.
    growing = [haiku("msg_1", 1, usage(fresh=200_000, output=4)), haiku("msg_1", 2, usage(fresh=200_000, output=million))]
    assert total(tmp_path / "growing", growing) == round(200_000 * 0.50 / 1e6 + 2.50, 2)


def test_a_call_counts_once_across_its_blocks_and_a_copied_session(tmp_path: Path) -> None:
    first = call("msg_1", 1, usage(output=1000))
    transcript(tmp_path, SESSION, [prompt(0, "Fix the parser"), first, first | {"uuid": "u-second-block"}])
    # A copy of a session repeats the history it was copied from under the same message ids.
    transcript(tmp_path, COPY, [prompt(0, "Fix the parser"), first, call("msg_2", 5, usage(output=1000))])
    summary = index_of(tmp_path).summary(NOW - timedelta(days=7))
    assert summary["total"]["calls"] == 2
    assert summary["total"]["tokens"]["output"] == 2000


def test_a_subagent_reply_counts_the_output_of_its_last_entry(tmp_path: Path) -> None:
    # In a subagent's transcript the entries of one reply carry a growing output count.
    transcript(tmp_path, SESSION, [prompt(0, "Fix the parser"), call("msg_1", 1, usage(output=10))])
    first = call("msg_sub", 2, usage(read=5000, output=4))
    subagent(tmp_path, SESSION, "a1", [first, first | {"uuid": "u-b"}, call("msg_sub", 3, usage(read=5000, output=900))])
    use = index_of(tmp_path).summary(NOW - timedelta(days=7))["total"]
    assert (use["calls"], use["tokens"]["cache_read"], use["tokens"]["output"]) == (2, 5000, 910)
    assert use["subagent_usd"] == round(5000 * 0.2 / 1e6 + 900 * 20 / 1e6, 2)


def test_a_turn_runs_from_one_prompt_to_the_next(tmp_path: Path) -> None:
    transcript(tmp_path, SESSION, [
        prompt(0, "Message from Sam via Cockpit: fix the parser"),
        call("msg_1", 1, usage(output=1000)),
        tool_result(2),
        call("msg_2", 3, usage(output=1000)),
        prompt(4, "<task-notification>the tests finished</task-notification>"),
        call("msg_3", 5, usage(output=1000)),
        prompt(6, [{"type": "text", "text": "<system-reminder>a reminder</system-reminder>"}], isMeta=True),
        prompt(7, [{"type": "text", "text": "Another Claude session sent a message: rebase"}], isMeta=True),
        call("msg_4", 8, usage(output=1000)),
        prompt(9, "and the docs"),
        call("msg_5", 10, usage(output=1000)),
    ])
    row = session_row(index_of(tmp_path).summary(NOW - timedelta(days=7)), SESSION)
    assert row["turns"] == 3
    assert row["use"]["calls"] == 5
    assert row["first_prompt"] == "Message from Sam via Cockpit: fix the parser"


def test_work_after_a_background_task_woke_the_session_continues_its_turn(tmp_path: Path) -> None:
    transcript(tmp_path, SESSION, [
        prompt(0, "Review the PR with a subagent in the background"),
        asks_tool(call("msg_1", 1, usage(output=1000))),
        tool_result(2),
        # The reply ends the turn while the subagent works.
        call("msg_2", 3, usage(output=1000)),
        peer(4, '<agent-message from="a1">The review found nothing</agent-message>'),
        asks_tool(call("msg_3", 5, usage(output=1000))),
        tool_result(6),
        call("msg_4", 7, usage(output=1000)),
        prompt(8, "Now the docs"),
        asks_tool(call("msg_5", 9, usage(output=1000))),
        # A notice that arrives while the session works does not wake it.
        prompt(10, "<task-notification>the tests finished</task-notification>"),
        call("msg_6", 11, usage(output=1000)),
    ])
    row = session_row(index_of(tmp_path).summary(NOW - timedelta(days=7)), SESSION)
    assert row["turns"] == 2
    assert row["use"]["calls"] == 6
    # msg_3 and msg_4: 2,000 output tokens at $20 per million.
    assert row["use"]["woken_usd"] == 0.04


def test_a_call_that_writes_the_cached_prompt_again_is_a_cache_miss(tmp_path: Path) -> None:
    transcript(tmp_path, SESSION, [
        prompt(0, "Fix the parser"),
        # The first call writes the prompt to the cache: a new prompt, no miss.
        asks_tool(call("msg_1", 1, usage(write_1h=100_000, output=100))),
        tool_result(2),
        # Reads what msg_1 cached and writes the end of the prompt: no miss.
        call("msg_2", 3, usage(read=100_000, write_1h=600, output=100)),
        prompt(4, "and the docs"),
        # Two minutes later the start of the prompt changed: 60,000 of the 100,600 cached tokens written again.
        asks_tool(call("msg_3", 6, usage(read=40_600, write_5m=60_000, output=100))),
        tool_result(7),
        # After a pause of over an hour the cache expired: all of it written again, 1,000 of it fresh.
        asks_tool(call("msg_4", 80, usage(fresh=1_000, write_1h=99_600, output=100))),
    ])
    summary = index_of(tmp_path).summary(NOW - timedelta(days=7))
    use = summary["total"]
    assert use["misses"] == 2
    assert use["miss_tokens"] == 60_000 + 100_600
    # Opus 5.5: msg_3 writes at $5 per million against $0.20 a cache read; msg_4 pays $4 for a fresh token and $8
    # for an hour's write, $7.96 on average, against $0.20.
    miss_3 = 60_000 * (5.0 - 0.2) / 1e6
    miss_4 = 100_600 * ((1_000 * 4.0 + 99_600 * 8.0) / 100_600 - 0.2) / 1e6
    assert use["miss_usd"] == round(miss_3 + miss_4, 2)
    assert use["paused_miss_usd"] == round(miss_4, 2)
    # The turn with the costliest misses first.
    assert [turn["prompt"] for turn in summary["miss_turns"]] == ["and the docs"]
    assert summary["miss_turns"][0]["use"]["misses"] == 2


def test_a_compaction_or_a_subagent_starts_a_new_prompt_without_a_miss(tmp_path: Path) -> None:
    transcript(tmp_path, SESSION, [
        prompt(0, "Fix the parser"),
        call("msg_1", 1, usage(write_1h=150_000, output=100)),
        {"parentUuid": None, "isSidechain": False, "type": "system", "subtype": "compact_boundary",
         "content": "Conversation compacted", "timestamp": at(2)},
        prompt(2, "This session is being continued from a previous conversation that ran out of context."),
        call("msg_2", 3, usage(read=20_000, write_1h=30_000, output=100)),
    ])
    subagent(tmp_path, SESSION, "a1", [call("msg_3", 4, usage(write_5m=50_000, output=100))])
    use = index_of(tmp_path).summary(NOW - timedelta(days=7))["total"]
    assert use["misses"] == 0
    assert use["miss_usd"] == 0


@pytest.mark.parametrize(("text", "script", "expected"), [
    ("Fix the parser", False, "typed"),
    ("Message from Sam via Cockpit:\n\nMerge it", False, "user_cockpit"),
    ("Message from Cockpit itself (automatic, not from Sam or an agent):\n\nCockpit wake-up: 2 steps changed", False, "sweep"),
    ("Message from Cockpit itself (automatic, not from Sam or an agent):\n\nCockpit pause: Sam is away", False, "sweep"),
    (f"Message from Sam via Cockpit:\n\n{config.SWEEP_PROMPT}", False, "sweep"),
    ("Message from Sam via Cockpit:\n\nCockpit check: Sam pressed Update.", False, "sweep"),
    ("Message from agent sweep via Cockpit (another agent, not Sam):\n\nGet item 5 ready", False, "sweeper_asks"),
    ("Message from agent check via Cockpit (another agent, not Sam):\n\nStep 3 is stale", False, "sweeper_asks"),
    ("Message from agent app-10 via Cockpit (another agent, not Sam):\n\nRebase", False, "session_asks"),
    ('<cross-session-message from="x">Rebase</cross-session-message>', False, "session_asks"),
    ("Message from Cockpit itself (automatic, not from Sam or an agent):\n\nClaude's usage works again", False, "cockpit"),
    ("Summarize this ticket", True, "script"),
])
def test_a_turn_names_what_started_it(text: str, script: bool, expected: str) -> None:
    assert spend.origin(text, script) == expected


def test_use_splits_by_what_started_the_turn_and_the_kind_of_session(tmp_path: Path) -> None:
    sweeper, started, typed = SESSION, COPY, "55555555-5555-4555-8555-555555555555"
    transcript(tmp_path, sweeper, [
        peer(0, "Message from Cockpit itself (automatic, not from Sam or an agent):\n\nCockpit wake-up: a step changed"),
        call("msg_1", 1, usage(output=1000)),
        peer(2, "Message from Sam via Cockpit:\n\nWhy is step 5 first?"),
        call("msg_2", 3, usage(output=2000)),
    ])
    transcript(tmp_path, started, [
        prompt(0, "Message from agent sweep via Cockpit (another agent, not Sam):\n\nGet item 5 ready"),
        call("msg_3", 1, usage(output=8000)),
        peer(2, "Message from Sam via Cockpit:\n\nMerge it"),
        call("msg_4", 3, usage(output=4000)),
    ])
    transcript(tmp_path, typed, [prompt(0, "Fix the parser"), call("msg_5", 1, usage(output=500))])
    subagent(tmp_path, typed, "abc", [call("msg_sub", 2, usage(output=3000)) | {"isSidechain": True}])
    write(tmp_path / FOLDER / "66666666-6666-4666-8666-666666666666.jsonl",
          [prompt(0, "Summarize this ticket", entrypoint="sdk-cli"), call("msg_6", 1, usage(output=100))])
    summary = index_of(tmp_path).summary(NOW - timedelta(days=7))
    output = {row["origin"]: (row["turns"], row["use"]["tokens"]["output"]) for row in summary["origins"]}
    assert output == {
        "sweep": (1, 1000), "user_cockpit": (2, 6000), "sweeper_asks": (1, 8000), "typed": (1, 3500), "script": (1, 100),
    }
    kinds = {row["kind"]: (row["sessions"], row["use"]["tokens"]["output"]) for row in summary["session_kinds"]}
    assert kinds == {"sweeper": (1, 3000), "sweeper_started": (1, 12000), "terminal": (1, 3500), "script": (1, 100)}
    assert session_row(summary, started)["kind"] == "sweeper_started"
    assert session_row(summary, typed)["use"]["subagent_usd"] == 0.06
    # The most expensive turns first, each with its session, what started it and the start of its prompt.
    top = summary["top_turns"][0]
    assert (top["session_id"], top["origin"], top["use"]["tokens"]["output"]) == (started, "sweeper_asks", 8000)
    assert top["prompt"].endswith("Get item 5 ready")
    assert [turn["use"]["tokens"]["output"] for turn in summary["top_turns"]] == [8000, 4000, 3500, 2000, 1000, 100]


def test_a_skill_counts_the_turns_that_loaded_it(tmp_path: Path) -> None:
    transcript(tmp_path, SESSION, [
        prompt(0, "Review the PR"),
        skill_call("msg_1", 1, "example-skill"),
        call("msg_2", 2, usage(output=10_000)),
        prompt(3, "<command-name>/cockpit</command-name>\n<command-args></command-args>"),
        skill_call("msg_3", 4, "writing-style"),
        prompt(5, "thanks"),
        call("msg_4", 6, usage(output=10_000)),
    ])
    skills = {row["skill"]: row for row in index_of(tmp_path).summary(NOW - timedelta(days=7))["skills"]}
    assert skills["example-skill"]["turns"] == 1
    assert skills["example-skill"]["sessions"] == 1
    assert skills["example-skill"]["use"]["tokens"]["output"] == 10_100
    # A turn that loaded two skills splits its use between them.
    assert skills["cockpit"]["use"]["tokens"]["output"] == 50
    assert skills["writing-style"]["use"]["tokens"]["output"] == 50
    # A turn that loaded none counts under no skill.
    assert skills[""]["use"]["tokens"]["output"] == 10_000


def test_a_subagent_counts_in_the_turn_it_started_in(tmp_path: Path) -> None:
    transcript(tmp_path, SESSION, [
        prompt(0, "Fix the parser"),
        call("msg_1", 1, usage(output=1000)),
        prompt(10, "Now review it"),
        call("msg_2", 11, usage(output=1000)),
        prompt(20, "Ship it"),
        call("msg_3", 21, usage(output=1000)),
    ])
    reviewer = call("msg_sub_1", 12, usage(output=5000)) | {"isSidechain": True, "agentId": "abc"}
    subagent(tmp_path, SESSION, "abc", [
        prompt(12, "Review the diff") | {"isSidechain": True},
        reviewer,
        skill_call("msg_sub_2", 13, "writing-style"),
    ])
    summary = index_of(tmp_path).summary(NOW - timedelta(days=7))
    row = session_row(summary, SESSION)
    assert row["turns"] == 3
    assert row["use"]["tokens"]["output"] == 3000 + 5100
    skills = {row["skill"]: row for row in summary["skills"]}
    assert skills["writing-style"]["use"]["tokens"]["output"] == 1000 + 5100
    assert skills[""]["use"]["tokens"]["output"] == 2000


def test_the_index_reads_only_what_was_appended(tmp_path: Path) -> None:
    path = transcript(tmp_path, SESSION, [prompt(0, "Fix the parser"), call("msg_1", 1, usage(output=1000))])
    index = index_of(tmp_path)
    write(path, [call("msg_2", 2, usage(output=1000))])
    # A line Claude Code is still writing waits for the next refresh.
    partial = json.dumps(call("msg_3", 3, usage(output=1000)), separators=(",", ":"))
    with path.open("a") as handle:
        handle.write(partial[:50])
    index.refresh(NOW)
    assert index.summary(NOW - timedelta(days=7))["total"]["calls"] == 2
    with path.open("a") as handle:
        handle.write(partial[50:] + "\n")
    index.refresh(NOW)
    assert index.summary(NOW - timedelta(days=7))["total"]["calls"] == 3


def test_the_period_counts_the_turns_started_in_it(tmp_path: Path) -> None:
    transcript(tmp_path, SESSION, [
        prompt(-3 * 24 * 60, "Old work"),
        call("msg_1", -3 * 24 * 60 + 1, usage(output=1000)),
        prompt(0, "New work"),
        call("msg_2", 1, usage(output=2000)),
    ])
    index = index_of(tmp_path)
    assert index.summary(NOW - timedelta(days=1))["total"]["tokens"]["output"] == 2000
    assert index.summary(NOW - timedelta(days=7))["total"]["tokens"]["output"] == 3000
    assert [hour["hour"] for hour in index.hours(NOW - timedelta(days=7))] == [
        at(-3 * 24 * 60)[:13] + ":00:00Z", at(0)[:13] + ":00:00Z",
    ]


def test_a_session_keeps_its_kind_in_a_period_that_leaves_out_its_first_turns(tmp_path: Path) -> None:
    transcript(tmp_path, SESSION, [
        peer(-2 * 24 * 60, "Message from Cockpit itself (automatic, not from Sam or an agent):\n\nCockpit wake-up"),
        call("msg_1", -2 * 24 * 60 + 1, usage(output=1000)),
        peer(0, "Message from Sam via Cockpit:\n\nWhy is step 5 first?"),
        call("msg_2", 1, usage(output=2000)),
    ])
    index = index_of(tmp_path)
    for days in (1, 7):
        assert session_row(index.summary(NOW - timedelta(days=days)), SESSION)["kind"] == "sweeper"


def test_a_session_started_by_a_sweep_that_made_no_call_is_still_a_sweeper(tmp_path: Path) -> None:
    transcript(tmp_path, SESSION, [
        peer(-5, "Message from Cockpit itself (automatic, not from Sam or an agent):\n\nCockpit wake-up"),
        peer(0, "Message from Sam via Cockpit:\n\nWhy is step 5 first?"),
        call("msg_1", 1, usage(output=2000)),
    ])
    assert session_row(index_of(tmp_path).summary(NOW - timedelta(days=1)), SESSION)["kind"] == "sweeper"


def test_an_entry_laid_out_otherwise_is_read_whole(tmp_path: Path) -> None:
    entry = call("msg_1", 1, usage(output=1000))
    reordered = {"type": "assistant", "timestamp": entry["timestamp"],
                 "message": {"id": "msg_1", "model": "claude-opus-5-5", "usage": usage(output=1000), "content": []}}
    transcript(tmp_path, SESSION, [prompt(0, "Fix the parser"), reordered])
    assert index_of(tmp_path).summary(NOW - timedelta(days=7))["total"]["tokens"]["output"] == 1000


def test_a_broken_line_is_skipped_with_a_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = transcript(tmp_path, SESSION, [prompt(0, "Fix the parser")])
    good = json.dumps(call("msg_2", 2, usage(output=1000)), separators=(",", ":"))
    # The start of a long user entry, then a whole assistant entry, on one line.
    cut = json.dumps(prompt(1, "x" * 1000), separators=(",", ":"))[:600]
    broken = cut + json.dumps(call("msg_1", 1, usage(output=7)), separators=(",", ":"))
    with path.open("a") as handle:
        handle.write(broken + "\n" + good + "\n")
    with caplog.at_level(logging.WARNING):
        summary = index_of(tmp_path).summary(NOW - timedelta(days=7))
    assert summary["total"]["tokens"]["output"] == 1000
    assert "is not one transcript entry, skipped" in caplog.text


def test_use_splits_by_model_with_subagents_included(tmp_path: Path) -> None:
    transcript(tmp_path, SESSION, [
        prompt(0, "Fix the parser"),
        call("msg_1", 1, usage(output=1000)),
        prompt(10, "Now find the config file"),
        call("msg_2", 11, usage(output=1000)),
    ])
    subagent(tmp_path, SESSION, "abc", [
        prompt(12, "Find the config file") | {"isSidechain": True},
        call("msg_sub_1", 12, usage(fresh=1_000_000, output=2000), model="claude-haiku-5-5"),
        call("msg_sub_2", 13, usage(output=2000), model="claude-haiku-5-5"),
    ])
    summary = index_of(tmp_path).summary(NOW - timedelta(days=7))
    models = {row["model"]: row for row in summary["models"]}
    # The most expensive first: Haiku's million input tokens cost more than Opus's 2,000 output tokens.
    assert list(models) == ["haiku-5-5", "opus-5-5"]
    assert (models["opus-5-5"]["turns"], models["opus-5-5"]["use"]["tokens"]["output"]) == (2, 2000)
    # Haiku 5.5, all of it in the subagent: $0.50 input and $2.50 output per million tokens for the call with a prompt
    # over 100,000 tokens, $0.50 output for the other.
    haiku = models["haiku-5-5"]
    assert (haiku["turns"], haiku["sessions"], haiku["use"]["calls"]) == (1, 1, 2)
    assert haiku["use"]["usd"] == haiku["use"]["subagent_usd"] == round(0.50 + 2000 * 2.50 / 1e6 + 2000 * 0.50 / 1e6, 2)
    assert haiku["prices"] == {"input": 0.10, "output": 0.50, "cache_read": 0.01}
    assert sum(row["use"]["usd"] for row in summary["models"]) == pytest.approx(summary["total"]["usd"], abs=0.01)


def test_a_model_without_a_price_is_named(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    transcript(tmp_path, SESSION, [prompt(0, "Fix the parser"), call("msg_1", 1, usage(output=1000), model="claude-x-9")])
    summary = index_of(tmp_path).summary(NOW - timedelta(days=7))
    assert summary["unpriced_models"] == ["claude-x-9"]
    assert summary["total"]["calls"] == 0
    assert "no price for Claude model 'claude-x-9'" in caplog.text


def test_a_non_claude_model_is_left_out_without_an_error(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A call on a model that is not Claude's is no Claude use."""
    transcript(tmp_path, SESSION, [
        prompt(0, "Fix the parser"),
        call("msg_1", 1, usage(output=1000), model="other-model-1"),
        call("msg_2", 2, usage(output=1000)),
    ])
    summary = index_of(tmp_path).summary(NOW - timedelta(days=7))
    assert summary["unpriced_models"] == []
    assert [row["model"] for row in summary["models"]] == ["opus-5-5"]
    assert (summary["total"]["calls"], summary["total"]["tokens"]["output"]) == (1, 1000)
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


def test_the_page_shows_use_per_session_and_skill(world: World) -> None:
    projects = world.spend_index.projects_dir
    transcript(projects, SESSION_A, [
        prompt(0, "Fix the parser"), skill_call("msg_1", 1, "example-skill"), call("msg_2", 2, usage(read=1_000_000)),
    ])
    transcript(projects, SESSION, [{"type": "custom-title", "customTitle": "Parser fix"}, prompt(0, "x"),
                                   call("msg_3", 1, usage(output=1000))])
    world.spend_index.refresh(datetime.now(UTC))
    body = world.client.get("/api/spend", params={"period": "35"}).json()
    names = {row["session_id"]: row["name"] for row in body["sessions"]}
    # A session Cockpit lists shows Cockpit's name for it; others their own title or first prompt.
    assert names == {SESSION_A: f"session {SESSION_A[:4]}", SESSION: "Parser fix"}
    assert [row["skill"] for row in body["skills"]] == ["example-skill", ""]
    assert [row["origin"] for row in body["origins"]] == ["typed"]
    assert [row["kind"] for row in body["session_kinds"]] == ["terminal"]
    assert [row["model"] for row in body["models"]] == ["opus-5-5"]
    assert len(body["top_turns"]) == 2
    assert world.client.get("/api/spend", params={"period": "36"}).status_code == 400
    [row] = [row for row in world.client.get("/api/state").json()["sessions"] if row["session_id"] == SESSION_A]
    assert row["spend"]["usd"] == 0.2
    assert row["spend"]["tokens"]["cache_read"] == 1_000_000


def test_the_graph_per_day_counts_use_per_hour_and_closed_steps(world: World) -> None:
    projects = world.spend_index.projects_dir
    transcript(projects, SESSION, [prompt(0, "x"), call("msg_1", 1, usage(output=1000)),
                                   prompt(70, "y"), call("msg_2", 71, usage(output=2000))])
    world.spend_index.refresh(datetime.now(UTC))
    done, dismissed, _open = (put_card(world, f"review@{n}")["step"] for n in "abc")
    assert world.client.post(f"/api/steps/{done['id']}/done", json={}, headers=world.user()).status_code == 200
    assert world.client.post(f"/api/steps/{dismissed['id']}/dismiss", json={}, headers=world.user()).status_code == 200
    # The graph covers its own 30 days, whatever period the page shows.
    daily = world.client.get("/api/spend", params={"period": "1"}).json()["daily"]
    assert daily["since"] < (datetime.now(UTC) - timedelta(days=29)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert [row["hour"] for row in daily["hours"]] == [at(0)[:13] + ":00:00Z", at(70)[:13] + ":00:00Z"]
    assert [row["use"]["tokens"]["output"] for row in daily["hours"]] == [1000, 2000]
    assert [row["status"] for row in daily["steps"]] == ["done", "dismissed"]


def test_this_week_starts_with_the_earliest_account_week(world: World) -> None:
    def account(email: str, resets_in_hours: int) -> dict[str, Any]:
        week = {"used_percentage": 40, "resets_at": NOW.timestamp() + resets_in_hours * 3600}
        return {"account": {"emailAddress": email}, "rate_limits": {"seven_day": week},
                "captured_at": NOW.timestamp(), "session_id": SESSION, "config_dir": "/home/sam/.claude"}

    world.claude_rate_limits.write_text(json.dumps({"accounts": {
        "sam@example.com": account("sam@example.com", 30), "sam.home@example.com": account("sam.home@example.com", 100),
    }}))
    body = world.client.get("/api/spend", params={"period": "week"}).json()
    assert body["since"] == (NOW + timedelta(hours=30) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert [entry["email"] for entry in body["limits"]["accounts"]] == ["sam@example.com", "sam.home@example.com"]
    assert body["codex"] == {"total": dict.fromkeys(["fresh", "cached", "output", "reasoning"], 0), "calls": 0,
                             "sessions": [], "accounts": [], "week_percent": 0, "weeks": 0}
    assert body["daily"]["codex_hours"] == []


def test_the_usage_page_is_served(world: World) -> None:
    response = world.client.get("/usage")
    assert response.status_code == 200
    assert "usage.js" in response.text


class CountedLock:
    """A lock that counts how often it was taken."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.taken = 0

    def __enter__(self) -> None:
        self.lock.acquire()
        self.taken += 1

    def __exit__(self, *exc: object) -> None:
        self.lock.release()

    def locked(self) -> bool:
        return self.lock.locked()


def test_a_refresh_counts_what_a_transcript_gained_under_one_lock(tmp_path: Path) -> None:
    # A refresh that takes the lock once per line lets the page's polls take it in between every two lines, so the
    # refresh holds the lock once for all the lines a transcript gained.
    transcript(tmp_path, SESSION, [prompt(0, "x"), call("msg_0", 1, usage(output=10))])
    index = index_of(tmp_path)
    transcript(tmp_path, SESSION, [call(f"msg_{number}", 2, usage(output=10)) for number in range(1, 51)])
    index._lock = CountedLock()  # type: ignore[assignment]
    index.refresh(NOW)
    assert index._lock.taken == 1  # type: ignore[attr-defined]
    assert index.per_session(NOW)[SESSION]["calls"] == 51


def test_page_polls_sum_the_use_once_per_change(tmp_path: Path, monkeypatch) -> None:
    transcript(tmp_path, SESSION, [prompt(0, "x"), call("msg_1", 1, usage(output=10)),
                                   call("msg_2", 2, usage(output=10), model="claude-sonnet-5-5")])
    index = index_of(tmp_path)
    sessions = index._sessions
    calls = []

    def counted() -> dict[str, list[spend.Transcript]]:
        calls.append(1)
        return sessions()

    monkeypatch.setattr(index, "_sessions", counted)
    for _ in range(3):
        assert index.per_session(NOW)[SESSION]["calls"] == 2
        assert len(index.wrong_models(NOW, {})) == 1
    assert len(calls) == 2
    transcript(tmp_path, SESSION, [call("msg_3", 3, usage(output=10))])
    index.refresh(NOW)
    assert index.per_session(NOW)[SESSION]["calls"] == 3
    assert index.wrong_models(NOW, {})[0]["back"] is True
    assert len(calls) == 4


def test_lines_counted_before_an_unexpected_error_reach_the_page(tmp_path: Path) -> None:
    path = transcript(tmp_path, SESSION, [prompt(0, "x"), call("msg_1", 1, usage(output=10))])
    index = index_of(tmp_path)
    assert index.per_session(NOW)[SESSION]["calls"] == 1
    write(path, [call("msg_2", 2, usage(output=10)), {"type": "user", "message": ["bad"], "timestamp": at(3)}])
    with pytest.raises(AttributeError):
        index.refresh(NOW)
    assert index.per_session(NOW)[SESSION]["calls"] == 2


def test_wrong_models_read_the_settings_outside_the_lock(tmp_path: Path, monkeypatch) -> None:
    # A settings read made while holding the lock can fail with "too many open files" and leave the lock held.
    transcript(tmp_path, SESSION, [prompt(0, "x"), call("msg_1", 1, usage(output=10), model="claude-sonnet-5-5"),
                                   call("msg_2", 2, usage(output=10))])
    index = index_of(tmp_path)
    read = index._settings_models
    held = []

    def watched(cwd: str) -> list[str]:
        held.append(index._lock.locked())
        return read(cwd)

    monkeypatch.setattr(index, "_settings_models", watched)
    assert len(index.wrong_models(NOW, {})) == 1
    assert held == [False]


def test_recent_use_counts_the_last_ten_minutes_of_a_session_and_its_subagents(tmp_path: Path) -> None:
    index = spend.SpendIndex(tmp_path)
    transcript(tmp_path, SESSION, [prompt(0, "x"), call("msg_1", 1, usage(read=1_000_000)),
                                   call("msg_2", 30, usage(read=1_000_000)), call("msg_3", 38, usage(output=10_000))])
    write(tmp_path / FOLDER / SESSION / "subagents" / "agent-a.jsonl", [call("msg_4", 39, usage(output=10_000))])
    index.refresh(datetime.now(UTC))
    # At 10:40 the last ten minutes hold msg_2 to msg_4: 0.2 + 0.2 + 0.2 dollars at Opus 5.5's prices.
    use = index.per_session(NOW.replace(hour=10, minute=40))[SESSION]
    assert use["recent_usd"] == 0.6
    assert index.per_session(NOW.replace(hour=11, minute=0))[SESSION]["recent_usd"] == 0


def test_a_session_that_answered_on_another_model_is_named(tmp_path: Path) -> None:
    script, old = COPY, "55555555-5555-4555-8555-555555555555"
    transcript(tmp_path, SESSION, [
        {"type": "custom-title", "customTitle": "Parser fix"},
        prompt(0, "Fix the parser"),
        call("msg_1", 1, usage(output=100)),
        call("msg_2", 2, usage(output=100), model="claude-sonnet-5-5"),
        call("msg_3", 3, usage(output=100), model="claude-sonnet-5-5"),
        # Claude Code's own note, such as an API error, names no model the session ran on.
        call("msg_4", 4, usage(output=0), model="<synthetic>"),
    ])
    # A subagent and a `claude -p` run name their own model.
    subagent(tmp_path, SESSION, "abc", [call("msg_sub", 5, usage(output=100), model="claude-haiku-5-5")])
    write(tmp_path / FOLDER / f"{script}.jsonl", [
        prompt(0, "Summarize this ticket", entrypoint="sdk-cli"), call("msg_5", 1, usage(output=100), model="claude-haiku-5-5"),
    ])
    # Two days ago is past the day the page shows it for.
    transcript(tmp_path, old, [prompt(-2880, "Fix the export format"), call("msg_6", -2879, usage(output=100), model="claude-sonnet-5-5")])
    index = index_of(tmp_path)
    assert index.wrong_models(NOW, {}) == [{
        "session_id": SESSION, "title": "Parser fix", "first_prompt": "Fix the parser", "model": "claude-sonnet-5-5",
        "intended": "claude-opus-5-5", "first": at(2), "last": at(3), "calls": 2, "back": False,
    }]
    transcript(tmp_path, SESSION, [prompt(10, "Go on"), call("msg_7", 11, usage(output=100))])
    index.refresh(NOW)
    assert [(row["calls"], row["back"]) for row in index.wrong_models(NOW, {})] == [(2, True)]
    # A session the user started on Sonnet (SPEC SESSION-22) is wrong on Opus, and back once it is on Sonnet again.
    assert [(row["model"], row["intended"], row["calls"], row["back"])
            for row in index.wrong_models(NOW, {SESSION: "claude-sonnet-5-5"})] == [
        ("claude-opus-5-5", "claude-sonnet-5-5", 2, False)]


def test_the_latest_model_is_the_one_of_the_sessions_own_latest_call(tmp_path: Path) -> None:
    transcript(tmp_path, SESSION, [
        prompt(0, "Sweep"), call("msg_1", 1, usage(output=100)),
        call("msg_2", 2, usage(output=100), model="claude-opus-4-8"),
        # Claude Code's own note names no model the session ran on.
        call("msg_3", 3, usage(output=0), model="<synthetic>"),
    ])
    # A subagent names its own model.
    subagent(tmp_path, SESSION, "abc", [call("msg_sub", 4, usage(output=100), model="claude-haiku-5-5")])
    index = index_of(tmp_path)
    assert index.latest_model(SESSION) == "claude-opus-4-8"
    assert index.latest_model(COPY) is None


def test_a_wrong_model_counts_by_the_turn_it_answered(tmp_path: Path) -> None:
    started_by_script = "55555555-5555-4555-8555-555555555555"
    transcript(tmp_path, SESSION, [
        prompt(0, "Fix the parser"),
        call("msg_1", 1, usage(output=100), model="claude-sonnet-5-5"),
        # A terminal session resumed by `claude -p` with a model of its own.
        prompt(2, "Summarize the session", entrypoint="sdk-cli"),
        call("msg_2", 3, usage(output=100), model="claude-haiku-5-5"),
    ])
    # A `claude -p --fork-session` copy repeats the session's reply, whichever transcript is read first.
    write(tmp_path / "-home-sam" / f"{COPY}.jsonl", [
        prompt(0, "Fix the parser"), call("msg_1", 1, usage(output=100), model="claude-sonnet-5-5"),
        prompt(4, "Summarize the session", entrypoint="sdk-cli"), call("msg_3", 5, usage(output=100)),
    ])
    # A session a `claude -p` run started and someone then resumed in the terminal.
    transcript(tmp_path, started_by_script, [
        prompt(0, "Summarize this ticket", entrypoint="sdk-cli"),
        call("msg_4", 1, usage(output=100), model="claude-haiku-5-5"),
        prompt(2, "Now fix it", entrypoint="cli"),
        call("msg_5", 3, usage(output=100), model="claude-sonnet-5-5"),
    ])
    # All three started from Cockpit on the main model.
    started = dict.fromkeys((SESSION, COPY, started_by_script), config.MAIN_MODEL)
    rows = index_of(tmp_path).wrong_models(NOW, started)
    assert sorted((row["session_id"], row["model"], row["calls"]) for row in rows) == [
        (SESSION, "claude-sonnet-5-5", 1), (COPY, "claude-sonnet-5-5", 1), (started_by_script, "claude-sonnet-5-5", 1),
    ]


def test_a_session_started_elsewhere_is_judged_by_the_model_it_started_on(tmp_path: Path) -> None:
    # Another session started this one on Haiku on purpose (`claude --bg --model claude-haiku-5-5`), outside Cockpit.
    transcript(tmp_path, SESSION, [
        prompt(0, "Reply with the single word: ready"), call("msg_1", 1, usage(output=10), model="claude-haiku-5-5"),
    ])
    index = index_of(tmp_path)
    assert index.wrong_models(NOW, {}) == []
    # A later reply on another model is still a change from the one it started on.
    transcript(tmp_path, SESSION, [prompt(2, "Go on"), call("msg_2", 3, usage(output=10), model="claude-sonnet-5-5")])
    index.refresh(NOW)
    assert [(row["model"], row["intended"], row["back"]) for row in index.wrong_models(NOW, {})] == [
        ("claude-sonnet-5-5", "claude-haiku-5-5", False)]
    # Started from Cockpit on the main model, its first reply on Haiku is already the wrong one.
    assert [(row["model"], row["calls"]) for row in index.wrong_models(NOW, {SESSION: config.MAIN_MODEL})] == [
        ("claude-sonnet-5-5", 1), ("claude-haiku-5-5", 1)]


def test_a_model_picked_with_model_in_the_session_is_intended(tmp_path: Path) -> None:
    transcript(tmp_path, SESSION, [
        prompt(0, "Fix the parser"), call("msg_1", 1, usage(output=100)),
        prompt(2, "<command-name>/model</command-name>\n<command-message>model</command-message>\n<command-args>fable</command-args>"),
        prompt(3, "Go on"), call("msg_2", 4, usage(output=100), model="claude-fable-5-2"),
    ])
    index = index_of(tmp_path)
    assert index.wrong_models(NOW, {}) == []
    # A later switch nobody picked still warns, and the picked model counts as back.
    transcript(tmp_path, SESSION, [call("msg_3", 5, usage(output=100), model="claude-sonnet-5-5")])
    index.refresh(NOW)
    assert [(row["model"], row["back"]) for row in index.wrong_models(NOW, {})] == [("claude-sonnet-5-5", False)]
    transcript(tmp_path, SESSION, [call("msg_4", 6, usage(output=100), model="claude-fable-5-2")])
    index.refresh(NOW)
    assert [(row["model"], row["back"]) for row in index.wrong_models(NOW, {})] == [("claude-sonnet-5-5", True)]


def test_a_model_named_in_the_folders_claude_settings_is_intended(tmp_path: Path) -> None:
    folder = tmp_path / "checkout"
    (folder / ".claude").mkdir(parents=True)
    transcript(tmp_path / "projects", SESSION, [
        prompt(0, "Fix the parser", cwd=str(folder)), call("msg_1", 1, usage(output=100)),
        call("msg_2", 2, usage(output=100), model="claude-fable-5-2"),
        call("msg_3", 3, usage(output=100), model="claude-sonnet-5-5"),
    ])
    index = index_of(tmp_path / "projects")
    assert [row["model"] for row in index.wrong_models(NOW, {})] == ["claude-sonnet-5-5", "claude-fable-5-2"]
    # Claude Code moves a running session to the model its folder's settings name, by full id or alias.
    (folder / ".claude" / "settings.json").write_text(json.dumps({"model": "claude-fable-5-2"}))
    (folder / ".claude" / "settings.local.json").write_text(json.dumps({"model": "sonnet[1m]"}))
    assert index.wrong_models(NOW, {}) == []
    # An unreadable settings file names no model, and the error is logged.
    (folder / ".claude" / "settings.local.json").write_text("{")
    assert [row["model"] for row in index.wrong_models(NOW, {})] == ["claude-sonnet-5-5"]
    assert index.unreadable == {folder / ".claude" / "settings.local.json"}


def test_wrong_model_checks_read_each_checkout_once_and_notice_settings_edits(tmp_path: Path, monkeypatch) -> None:
    folders = [tmp_path / "first", tmp_path / "second"]
    for folder in folders:
        (folder / ".claude").mkdir(parents=True)
        (folder / ".claude" / "settings.json").write_text('{"model":"opus"}')
    for number, session_id in enumerate((SESSION, COPY, SESSION_A)):
        transcript(tmp_path / "projects", session_id, [
            prompt(0, "Fix the parser", cwd=str(folders[number % 2])),
            call(f"opus-{number}", 1, usage(output=100)),
            call(f"sonnet-{number}", 2, usage(output=100), model="claude-sonnet-5-5"),
        ])
    index = index_of(tmp_path / "projects")
    read = index._settings_models
    calls = []

    def counted(cwd: str) -> list[str]:
        calls.append(cwd)
        return read(cwd)

    monkeypatch.setattr(index, "_settings_models", counted)
    assert len(index.wrong_models(NOW, {})) == 3
    assert sorted(calls) == sorted(str(folder) for folder in folders)
    calls.clear()
    (folders[0] / ".claude" / "settings.local.json").write_text('{"model":"sonnet"}')
    assert [row["session_id"] for row in index.wrong_models(NOW, {})] == [COPY]
    assert sorted(calls) == sorted(str(folder) for folder in folders)


def test_a_settings_alias_names_its_family_only() -> None:
    assert spend.names_model("fable", "claude-fable-5-2")
    assert spend.names_model("opus[1m]", "claude-opus-5-5")
    assert spend.names_model("claude-opus-5-5", "claude-opus-5-5")
    assert not spend.names_model("opus", "claude-fable-5-2")
    assert not spend.names_model("claude-opus-5", "claude-opus-5-5")


def test_the_page_names_a_session_on_another_model(world: World) -> None:
    projects = world.spend_index.projects_dir
    transcript(projects, SESSION_A, [
        prompt(0, "Fix the parser"), call("msg_0", 0, usage(output=100), model="claude-opus-5-5"),
        call("msg_1", 1, usage(output=100), model="claude-fable-5-2"),
    ])
    world.spend_index.refresh(datetime.now(UTC))
    state = world.client.get("/api/state").json()
    assert config.MAIN_MODEL == "claude-opus-5-5"
    assert [(row["name"], row["model"], row["intended"], row["back"]) for row in state["wrong_models"]] == [
        (f"session {SESSION_A[:4]}", "claude-fable-5-2", "claude-opus-5-5", False),
    ]
    # The sweeper reads it in `cockpit get` too.
    assert (f"WARNING: session {SESSION_A} (session {SESSION_A[:4]}) ran on claude-fable-5-2 instead of claude-opus-5-5."
            f" It made 1 call there, the last at {at(1)}. `/model claude-opus-5-5` moves it back.") in cli.summary(state)


def test_a_closed_wrong_model_banner_stays_closed_while_the_session_stays_on_that_model(world: World) -> None:
    projects = world.spend_index.projects_dir
    now = datetime.now(UTC)

    def stamp(moment: datetime) -> str:
        return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # The first call, on the intended model, comes before the wrong one at any time of day (it failed before 11:00 UTC
    # while the first call sat at 10:00 and the wrong one an hour before now).
    first = call("msg_0", 0, usage(output=100), model="claude-opus-5-5") | {"timestamp": stamp(now - timedelta(hours=2))}
    before = call("msg_1", 0, usage(output=100), model="claude-fable-5-2") | {"timestamp": stamp(now - timedelta(hours=1))}
    transcript(projects, SESSION_A, [prompt(0, "Fix the parser"), first, before])
    world.spend_index.refresh(now)
    assert len(world.client.get("/api/state").json()["wrong_models"]) == 1
    closed = world.client.post("/api/wrong-models/dismiss", headers=world.user(),
                               json={"session_id": SESSION_A, "model": "claude-fable-5-2"})
    assert closed.status_code == 200, closed.text
    assert world.client.get("/api/state").json()["wrong_models"] == []
    # Only the user closes it.
    refused = world.client.post("/api/wrong-models/dismiss", headers=world.agent(),
                                json={"session_id": SESSION_A, "model": "claude-fable-5-2"})
    assert refused.status_code == 403
    # Another block of the same reply, written after the close, keeps it closed.
    block = call("msg_1", 0, usage(output=100), model="claude-fable-5-2") | {"timestamp": stamp(now + timedelta(minutes=1))}
    transcript(projects, SESSION_A, [block])
    world.spend_index.refresh(now)
    assert world.client.get("/api/state").json()["wrong_models"] == []
    # Further replies on that model keep it closed: a session stuck on a model keeps answering there.
    later = call("msg_2", 0, usage(output=100), model="claude-fable-5-2") | {"timestamp": stamp(now + timedelta(minutes=5))}
    transcript(projects, SESSION_A, [later])
    world.spend_index.refresh(now)
    assert world.client.get("/api/state").json()["wrong_models"] == []
    # Another model in the same session is news, so it warns.
    other = call("msg_3", 0, usage(output=100), model="claude-sonnet-5-5") | {"timestamp": stamp(now + timedelta(minutes=6))}
    transcript(projects, SESSION_A, [other])
    world.spend_index.refresh(now)
    assert [row["model"] for row in world.client.get("/api/state").json()["wrong_models"]] == ["claude-sonnet-5-5"]


def test_a_wrong_model_close_goes_once_its_calls_leave_the_window(world: World) -> None:
    now = datetime.now(UTC)
    conn = db.connect(world.db_path)
    row = {"session_id": SESSION_A, "model": "claude-opus-4-8"}
    other = {"session_id": SESSION_A, "model": "claude-fable-5-2"}
    store.dismiss_wrong_model(conn, SESSION_A, "claude-opus-4-8", [row, other], now)
    assert store.not_dismissed(conn, [row, other]) == [other]
    # A later close, when the first model's calls are all older than the window, drops the first close.
    store.dismiss_wrong_model(conn, SESSION_A, "claude-fable-5-2", [other], now)
    assert store.not_dismissed(conn, [row, other]) == [row]


def test_a_wrong_model_close_goes_when_a_read_finds_its_calls_left_the_window(world: World) -> None:
    now = datetime.now(UTC)
    conn = db.connect(world.db_path)
    row = {"session_id": SESSION_A, "model": "claude-opus-4-8"}
    store.dismiss_wrong_model(conn, SESSION_A, "claude-opus-4-8", [row], now)
    assert store.not_dismissed(conn, [row]) == []
    # A day with no call on that model: the row leaves, and the read drops the close with no other close needed.
    assert store.not_dismissed(conn, []) == []
    assert store.not_dismissed(conn, [row]) == [row]


def test_a_wrong_model_close_survives_a_restart_while_the_index_is_still_counting(world: World) -> None:
    # After a restart the index starts empty and takes a while to read every transcript. Reads and
    # closes in that time keep every close, rather than only the ones found in the still empty rows.
    now = datetime.now(UTC)
    conn = db.connect(world.db_path)
    row = {"session_id": SESSION_A, "model": "claude-opus-4-8"}
    other = {"session_id": SESSION_B, "model": "claude-opus-4-8"}
    store.dismiss_wrong_model(conn, SESSION_A, "claude-opus-4-8", [row, other], now)
    assert store.not_dismissed(conn, None) == []
    store.dismiss_wrong_model(conn, SESSION_B, "claude-opus-4-8", None, now)
    assert store.not_dismissed(conn, [row, other]) == []
    assert spend.SpendIndex(world.spend_index.projects_dir).wrong_models(now, {}) is None
    investigate = world.client.post("/api/wrong-models/investigate", headers=world.user(),
                                    json={"session_id": SESSION_A, "model": "claude-opus-4-8"})
    assert investigate.status_code == 400, investigate.text


def test_investigate_on_a_wrong_model_banner_starts_a_to_do_in_cockpits_checkout(world: World) -> None:
    projects = world.spend_index.projects_dir
    now = datetime.now(UTC)

    def stamp(moment: datetime) -> str:
        return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    first = call("msg_0", 0, usage(output=100), model="claude-opus-5-5") | {"timestamp": stamp(now - timedelta(hours=2))}
    wrong = call("msg_1", 0, usage(output=100), model="claude-fable-5-2") | {"timestamp": stamp(now - timedelta(hours=1))}
    transcript(projects, SESSION_A, [prompt(0, "Fix the parser"), first, wrong])
    world.spend_index.refresh(now)
    # The investigation runs in Cockpit's own checkout; the fixture's spec repo sits outside the checkouts.
    app = create_app(replace(world.settings, spec_repo=world.checkout), world.deliverer, world.cache, world.spend_index,
                     world.codex_index, [])
    browser = TestClient(app, base_url=ORIGIN)
    browser.cookies = world.client.cookies
    body = {"session_id": SESSION_A, "model": "claude-fable-5-2"}
    # Only the user starts one, and only for a banner Cockpit shows.
    assert browser.post("/api/wrong-models/investigate", headers=world.agent(), json=body).status_code == 403
    unknown = browser.post("/api/wrong-models/investigate", headers=world.user(), json=body | {"model": "claude-haiku-5-5"})
    assert unknown.status_code == 404, unknown.text

    made = browser.post("/api/wrong-models/investigate", headers=world.user(), json=body)
    assert made.status_code == 200, made.text
    item = made.json()
    assert item["key"].startswith("todo-")
    assert item["summary"].startswith(
        f"Investigate why session “session {SESSION_A[:4]}” ran on claude-fable-5-2 instead of claude-opus-5-5")
    assert SESSION_A in item["summary"]
    # A new session started in Cockpit's checkout with that text, and the banner closed, as the to-do follows it now.
    args, cwd = world.runner.calls[-1]
    assert cwd == world.checkout
    assert any("Investigate why session" in arg for arg in args)
    assert browser.get("/api/state").json()["wrong_models"] == []


def test_a_message_on_a_warning_starts_a_to_do_in_cockpits_checkout_and_hands_it_the_warning(world: World) -> None:
    # The session runs in Cockpit's own checkout; the fixture's spec repo sits outside the checkouts.
    app = create_app(replace(world.settings, spec_repo=world.checkout), world.deliverer, world.cache, world.spend_index,
                     world.codex_index, [])
    browser = TestClient(app, base_url=ORIGIN)
    browser.cookies = world.client.cookies
    quote = "Codex ~/.codex (sam@example.com) is signed out Codex jobs on this account fail until it signs in."
    key = "codex-sign-in:.codex"
    body = {"key": key, "text": "Take the old account out of Cockpit",
            "part": {"target": "page:next/warning", "quote": quote, "detail": ""}}
    # Only the user sends one, with the warning's key, words and the warning, and nothing else.
    assert browser.post("/api/warnings/message", headers=world.agent(), json=body).status_code == 403
    for wrong in ({**body, "text": ""}, {**body, "key": ""}, {k: v for k, v in body.items() if k != "key"},
                  {**body, "part": None}, {**body, "extra": 1},
                  {**body, "part": {"target": "next/warning", "quote": quote, "detail": ""}}):
        refused = browser.post("/api/warnings/message", headers=world.user(), json=wrong)
        assert refused.status_code == 400, (wrong, refused.text)

    assert browser.get("/api/state").json()["handed_warnings"] == {}
    made = browser.post("/api/warnings/message", headers=world.user(), json=body)
    assert made.status_code == 200, made.text
    item = made.json()["item"]
    # The warning is handed to the to-do, and stays out of Inbox while the to-do is open.
    assert made.json()["handed_warnings"] == {key: item["id"]}
    assert browser.get("/api/state").json()["handed_warnings"] == {key: item["id"]}
    assert item["key"].startswith("todo-")
    assert item["summary"].startswith("Take the old account out of Cockpit")
    assert "Attached part of the page (page:next/warning)" in item["summary"]
    assert quote in item["summary"]
    # A new session started in Cockpit's checkout with the message and the warning.
    args, cwd = world.runner.calls[-1]
    assert cwd == world.checkout
    assert any("Take the old account out of Cockpit" in arg and "signed out" in arg for arg in args)

    conn = db.connect(world.db_path)
    # A to-do merged into another hands the warning on to the item it joined.
    main = store.add_item(conn, "The Codex accounts", None, False,
                          dict.fromkeys(("provider", "model", "effort", "account")), False, None)
    store.merge_item(conn, item["id"], main["id"], "Both are about the Codex accounts.", "agent:test")
    assert browser.get("/api/state").json()["handed_warnings"] == {key: main["id"]}
    # Once that item is done the warning is back in Inbox while its cause holds; so it is once one is archived.
    conn.execute("UPDATE items SET stage = 'done' WHERE id = ?", (main["id"],))
    conn.commit()
    assert browser.get("/api/state").json()["handed_warnings"] == {}
    again = browser.post("/api/warnings/message", headers=world.user(), json=body).json()["item"]
    assert store.handed_warnings(conn) == {key: again["id"]}
    conn.execute("UPDATE items SET archived = 1 WHERE id = ?", (again["id"],))
    conn.commit()
    assert store.handed_warnings(conn) == {}


@pytest.mark.parametrize("period", ["week", "1", "7", "30"])
def test_usage_keeps_a_complete_reading_while_both_indexes_restart(world: World, monkeypatch, period: str) -> None:
    projects = world.spend_index.projects_dir
    transcript(projects, SESSION, [prompt(0, "Fix parser"), call("msg_saved", 1, usage(output=1000))])
    world.spend_index.refresh(datetime.now(UTC))
    world.codex_index.refresh(datetime.now(UTC))
    saved = world.client.get("/api/spend", params={"period": period}).json()
    assert saved["ready_at"] and saved["refreshing"] is False
    with db.connect(world.db_path) as conn:
        assert db.kv_get(conn, f"spend-report-v1:{period}") == saved
    for restarting in (world.spend_index, world.codex_index):
        ready_at = restarting.ready_at
        restarting.ready_at = None
        with monkeypatch.context() as patch:
            def counting(*args):
                raise AssertionError("a saved reading must bypass indexing and session listing")
            patch.setattr(world.spend_index, "summary", counting)
            patch.setattr(world.cache, "get", counting)
            assert world.client.get("/api/spend", params={"period": period}).json() == saved | {"refreshing": True}
        restarting.ready_at = ready_at
    transcript(projects, SESSION, [prompt(0, "Fix parser"), call("msg_saved", 1, usage(output=1000)),
                                   call("msg_new", 2, usage(output=2000))])
    world.spend_index.refresh(datetime.now(UTC))
    updated = world.client.get("/api/spend", params={"period": period}).json()
    assert updated["refreshing"] is False
    assert updated["total"]["tokens"]["output"] == 3000
    with db.connect(world.db_path) as conn:
        assert db.kv_get(conn, f"spend-report-v1:{period}") == updated


def test_usage_without_a_saved_reading_still_waits_for_its_first_count(world: World) -> None:
    reading = world.client.get("/api/spend", params={"period": "week"}).json()
    assert reading["ready_at"] is None
    assert reading["refreshing"] is True
    with db.connect(world.db_path) as conn:
        assert db.kv_get(conn, "spend-report-v1:week") is None
