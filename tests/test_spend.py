from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cockpit import cli, config, spend
from tests.conftest import SESSION_A, World
from tests.test_api import put_item, put_step

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
    ("Message from agent check via Cockpit (another agent, not Sam):\n\nStep 12 is stale", False, "sweeper_asks"),
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
        skill_call("msg_1", 1, "review-pr"),
        call("msg_2", 2, usage(output=10_000)),
        prompt(3, "<command-name>/cockpit</command-name>\n<command-args></command-args>"),
        skill_call("msg_3", 4, "writing-style"),
        prompt(5, "thanks"),
        call("msg_4", 6, usage(output=10_000)),
    ])
    skills = {row["skill"]: row for row in index_of(tmp_path).summary(NOW - timedelta(days=7))["skills"]}
    assert skills["review-pr"]["turns"] == 1
    assert skills["review-pr"]["sessions"] == 1
    assert skills["review-pr"]["use"]["tokens"]["output"] == 10_100
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
    # As seen in real transcripts: the start of a long user entry, then a whole assistant entry, on one line.
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
    # Haiku 5.5: $0.10 input and $0.50 output per million tokens, all of it in the subagent.
    haiku = models["haiku-5-5"]
    assert (haiku["turns"], haiku["sessions"], haiku["use"]["calls"]) == (1, 1, 2)
    assert haiku["use"]["usd"] == haiku["use"]["subagent_usd"] == round(0.10 + 4000 * 0.50 / 1e6, 2)
    assert haiku["prices"] == {"input": 0.10, "output": 0.50, "cache_read": 0.01}
    assert sum(row["use"]["usd"] for row in summary["models"]) == pytest.approx(summary["total"]["usd"], abs=0.01)


def test_a_model_without_a_price_is_named(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    transcript(tmp_path, SESSION, [prompt(0, "Fix the parser"), call("msg_1", 1, usage(output=1000), model="claude-x-9")])
    summary = index_of(tmp_path).summary(NOW - timedelta(days=7))
    assert summary["unpriced_models"] == ["claude-x-9"]
    assert summary["total"]["calls"] == 0
    assert "no price for Claude model 'claude-x-9'" in caplog.text


def test_the_page_shows_use_per_session_and_skill(world: World) -> None:
    projects = world.spend_index.projects_dir
    transcript(projects, SESSION_A, [
        prompt(0, "Fix the parser"), skill_call("msg_1", 1, "review-pr"), call("msg_2", 2, usage(read=1_000_000)),
    ])
    transcript(projects, SESSION, [{"type": "custom-title", "customTitle": "Parser fix"}, prompt(0, "x"),
                                   call("msg_3", 1, usage(output=1000))])
    world.spend_index.refresh(datetime.now(UTC))
    body = world.client.get("/api/spend", params={"period": "35"}).json()
    names = {row["session_id"]: row["name"] for row in body["sessions"]}
    # A session Cockpit lists shows Cockpit's name for it; others their own title or first prompt.
    assert names == {SESSION_A: f"session {SESSION_A[:4]}", SESSION: "Parser fix"}
    assert [row["skill"] for row in body["skills"]] == ["review-pr", ""]
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
    put_item(world)
    done, dismissed, _open = (put_step(world, need=f"review@{n}")["step"] for n in "abc")
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


def test_the_page_names_a_session_on_another_model(world: World) -> None:
    projects = world.spend_index.projects_dir
    transcript(projects, SESSION_A, [
        prompt(0, "Fix the parser"), call("msg_0", 0, usage(output=100)),
        call("msg_1", 1, usage(output=100), model="claude-fable-5-1"),
    ])
    world.spend_index.refresh(datetime.now(UTC))
    state = world.client.get("/api/state").json()
    assert config.MAIN_MODEL == "claude-opus-5-5"
    assert [(row["name"], row["model"], row["intended"], row["back"]) for row in state["wrong_models"]] == [
        (f"session {SESSION_A[:4]}", "claude-fable-5-1", "claude-opus-5-5", False),
    ]
    # The sweeper reads it in `cockpit get` too.
    assert (f"WARNING: session {SESSION_A} (session {SESSION_A[:4]}) ran on claude-fable-5-1 instead of claude-opus-5-5."
            f" It made 1 call there, the last at {at(1)}. `/model claude-opus-5-5` moves it back.") in cli.summary(state)


def test_a_closed_wrong_model_banner_stays_closed_until_the_session_answers_there_again(world: World) -> None:
    projects = world.spend_index.projects_dir
    now = datetime.now(UTC)

    def stamp(moment: datetime) -> str:
        return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # The first call, on the intended model, comes before the wrong one at any time of day (it failed before 11:00 UTC
    # while the first call sat at 10:00 and the wrong one an hour before now).
    first = call("msg_0", 0, usage(output=100)) | {"timestamp": stamp(now - timedelta(hours=2))}
    before = call("msg_1", 0, usage(output=100), model="claude-fable-5-1") | {"timestamp": stamp(now - timedelta(hours=1))}
    transcript(projects, SESSION_A, [prompt(0, "Fix the parser"), first, before])
    world.spend_index.refresh(now)
    assert len(world.client.get("/api/state").json()["wrong_models"]) == 1
    closed = world.client.post("/api/wrong-models/dismiss", headers=world.user(),
                               json={"session_id": SESSION_A, "model": "claude-fable-5-1", "calls": 1})
    assert closed.status_code == 200, closed.text
    assert world.client.get("/api/state").json()["wrong_models"] == []
    # Only the user closes it.
    refused = world.client.post("/api/wrong-models/dismiss", headers=world.agent(),
                                json={"session_id": SESSION_A, "model": "claude-fable-5-1", "calls": 1})
    assert refused.status_code == 403
    # Another block of the same reply, written after the close, keeps it closed.
    block = call("msg_1", 0, usage(output=100), model="claude-fable-5-1") | {"timestamp": stamp(now + timedelta(minutes=1))}
    transcript(projects, SESSION_A, [block])
    world.spend_index.refresh(now)
    assert world.client.get("/api/state").json()["wrong_models"] == []
    # A reply on that model after the close brings the banner back.
    later = call("msg_2", 0, usage(output=100), model="claude-fable-5-1") | {"timestamp": stamp(now + timedelta(minutes=5))}
    transcript(projects, SESSION_A, [later])
    world.spend_index.refresh(now)
    assert [row["calls"] for row in world.client.get("/api/state").json()["wrong_models"]] == [2]
