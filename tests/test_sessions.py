from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from cockpit import sessions
from cockpit.config import Account
from tests.conftest import SESSION_A, SESSION_B, make_session

SESSION = "33333333-3333-4333-8333-333333333333"


def assistant(message_id: str, *blocks: dict) -> dict:
    return {"type": "assistant", "message": {"id": message_id, "role": "assistant", "content": list(blocks)}}


def text(value: str) -> dict:
    return {"type": "text", "text": value}


def test_last_message_is_the_whole_last_assistant_message(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sessions, "PROJECTS_DIR", tmp_path)
    entries = [
        assistant("m1", text("old answer")),
        {"type": "user", "message": {"role": "user", "content": "go on"}},
        assistant("m2", text("Part one: " + "x" * 5000)),
        assistant("m2", {"type": "tool_use", "id": "t", "name": "Bash", "input": {}}),
        assistant("m2", text("Part two: pick A or B?")),
        {"type": "system", "subtype": "turn_duration"},
    ]
    project = tmp_path / "-home-x"
    project.mkdir()
    (project / f"{SESSION}.jsonl").write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")
    message = sessions.last_message(SESSION)
    assert message.startswith("Part one: ")
    assert message.endswith("Part two: pick A or B?")
    assert "old answer" not in message


def test_last_message_without_transcript_is_empty(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sessions, "PROJECTS_DIR", tmp_path)
    assert sessions.last_message(SESSION) == ""


def write_transcript(tmp_path: Path, entries: list[dict]) -> None:
    project = tmp_path / "-home-x"
    project.mkdir(exist_ok=True)
    (project / f"{SESSION}.jsonl").write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")


def limit_error(stamp: str) -> dict:
    return assistant("m3", text("You've hit your weekly limit · resets Jan 10, 12pm (UTC)")) | {
        "timestamp": stamp, "error": "rate_limit", "isApiErrorMessage": True,
    }


def test_limit_stop_is_the_time_of_a_turn_that_ended_on_the_usage_limit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sessions, "PROJECTS_DIR", tmp_path)
    write_transcript(tmp_path, [
        assistant("m1", text("working on it")),
        {"type": "user", "message": {"role": "user", "content": "go on"}},
        limit_error("2026-01-06T10:00:00.500Z"),
        {"type": "system", "subtype": "turn_duration"},
    ])
    assert sessions.limit_stop(SESSION) == sessions.LimitStop(
        cooldown_stops=0, at="2026-01-06T10:00:00.500Z", resets=datetime(2026, 1, 10, 12, tzinfo=ZoneInfo("UTC"))
    )


@pytest.mark.parametrize(("words", "stopped", "resets"), [
    ("You've hit your session limit · resets 12pm (UTC)", "2026-01-06T10:00:00+00:00", "2026-01-06T12:00:00+00:00"),
    ("You've hit your session limit · resets 9am (UTC)", "2026-01-06T10:00:00+00:00", "2026-01-07T09:00:00+00:00"),
    ("You've hit your weekly limit · resets 6am (UTC)", "2026-01-22T23:00:00+00:00", "2026-01-23T06:00:00+00:00"),
    ("You've hit your weekly limit · resets Jan 10, 12pm (UTC)", "2026-01-06T10:00:00+00:00", "2026-01-10T12:00:00+00:00"),
    ("You've hit your weekly limit · resets Jan 2, 12am (UTC)", "2026-12-30T10:00:00+00:00", "2027-01-02T00:00:00+00:00"),
    ("You've hit your limit", "2026-01-06T10:00:00+00:00", None),
    ("You've hit your weekly limit · resets Feb 30, 12pm (UTC)", "2026-01-06T10:00:00+00:00", None),
    ("You've hit your session limit · resets 3:75pm (UTC)", "2026-01-06T10:00:00+00:00", None),
])
def test_limit_resets_reads_the_time_the_stop_names(words: str, stopped: str, resets: str | None) -> None:
    found = sessions.limit_resets(words, datetime.fromisoformat(stopped))
    assert (found.isoformat() if found else None) == (datetime.fromisoformat(resets).isoformat() if resets else None)


def test_limit_stop_is_none_once_a_later_turn_answered(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sessions, "PROJECTS_DIR", tmp_path)
    write_transcript(tmp_path, [limit_error("2026-01-06T10:00:00.500Z"), assistant("m4", text("Continuing."))])
    assert sessions.limit_stop(SESSION) is None
    assert sessions.limit_stop("44444444-4444-4444-8444-444444444444") is None


def test_last_answer_is_claude_s_newest_answer_after_a_time_and_whether_it_went_through(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(sessions, "PROJECTS_DIR", tmp_path)
    since = datetime.fromisoformat("2026-01-24T07:00:00+00:00")
    assert sessions.last_answer(SESSION, since) is None
    answered = assistant("m4", text("Continuing.")) | {"timestamp": "2026-01-24T07:10:00.000Z"}
    write_transcript(tmp_path, [answered, {"type": "system", "subtype": "turn_duration"}])
    assert sessions.last_answer(SESSION, since) == (datetime.fromisoformat("2026-01-24T07:10:00+00:00"), True)
    write_transcript(tmp_path, [answered, limit_error("2026-01-24T07:15:00.500Z")])
    assert sessions.last_answer(SESSION, since) == (datetime.fromisoformat("2026-01-24T07:15:00.500+00:00"), False)
    # An answer from before the time is none.
    assert sessions.last_answer(SESSION, datetime.fromisoformat("2026-01-24T08:00:00+00:00")) is None
    # A note Claude Code wrote itself made no call: the answer before it counts.
    note = assistant("m5", text("No response requested.")) | {"timestamp": "2026-01-24T07:20:00.000Z"}
    note["message"]["model"] = "<synthetic>"
    write_transcript(tmp_path, [answered, note])
    assert sessions.last_answer(SESSION, since) == (datetime.fromisoformat("2026-01-24T07:10:00+00:00"), True)


def test_a_transcript_is_read_again_only_once_it_changed_or_its_read_failed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sessions, "PROJECTS_DIR", tmp_path)
    write_transcript(tmp_path, [limit_error("2026-01-06T10:00:00.500Z")])
    reads = []
    failing = [True]
    read = sessions._read_tail

    def counted(path: Path, size: int) -> list | None:
        reads.append(path.name)
        return None if failing.pop() else read(path, size)

    monkeypatch.setattr(sessions, "_read_tail", counted)
    failing[:] = [False, False, True]  # popped from the end: the first read fails
    assert sessions.limit_stop(SESSION) is None
    assert sessions.limit_stop(SESSION) is not None
    since = datetime.fromisoformat("2026-01-06T09:00:00+00:00")
    assert sessions.last_answer(SESSION, since) == (datetime.fromisoformat("2026-01-06T10:00:00.500+00:00"), False)
    assert len(reads) == 2
    write_transcript(tmp_path, [limit_error("2026-01-06T10:00:00.500Z"), assistant("m4", text("Continuing."))])
    assert sessions.limit_stop(SESSION) is None
    assert len(reads) == 3


def ended(message_id: str, *blocks: dict, stop_reason: str = "end_turn") -> dict:
    entry = assistant(message_id, *blocks)
    entry["message"]["stop_reason"] = stop_reason
    return entry


TOOL_RESULT = {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t"}]}}
CALL = {"type": "tool_use", "id": "t", "name": "Bash", "input": {}}


@pytest.mark.parametrize(
    ("entries", "over"),
    [
        # Claude Code's note of the turn's length ends every turn, after the last reply.
        ([ended("m1", text("Watcher started; review-bot reviews.")), {"type": "system", "subtype": "turn_duration"}], True),
        ([ended("m1", text("Done.")), {"type": "attachment"}], True),
        ([ended("m1", CALL, stop_reason="tool_use")], False),  # a tool runs, or a dialog asks about it
        ([ended("m1", CALL, stop_reason="tool_use"), TOOL_RESULT, {"type": "attachment"}], False),
        ([ended("m1", text("Done.")), {"type": "system", "subtype": "turn_duration"}, TOOL_RESULT], False),
        ([{"type": "user", "message": {"role": "user", "content": "go on"}}], False),  # a prompt starts a turn
    ],
)
def test_turn_ended_reads_the_newest_entry_of_the_transcript(
    tmp_path: Path, monkeypatch, entries: list[dict], over: bool
) -> None:
    monkeypatch.setattr(sessions, "PROJECTS_DIR", tmp_path)
    write_transcript(tmp_path, entries)
    assert sessions.turn_ended(SESSION) is over


def test_turn_ended_is_false_without_a_transcript(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sessions, "PROJECTS_DIR", tmp_path)
    assert sessions.turn_ended(SESSION) is False


def test_a_session_lists_the_background_work_its_job_file_names(claude_accounts: tuple[Account, Account]) -> None:
    personal = claude_accounts[0]
    tmp_path = personal.jobs
    (tmp_path / "33333333").mkdir(parents=True)
    job = {"state": "working", "tempo": "idle", "inFlight": {"tasks": 2, "queued": 0, "kinds": ["local_bash", "local_agent"]}}
    (tmp_path / "33333333" / "state.json").write_text(json.dumps(job))
    row = {"id": "33333333", "sessionId": SESSION, "pid": 7, "status": "busy", "state": "working"}
    session = sessions._session(personal, row, [])
    assert session.in_flight == ["local_bash", "local_agent"] and session.runs_subagent
    (tmp_path / "33333333" / "state.json").write_text(json.dumps({"state": "done", "tempo": "idle"}))
    assert sessions._session(personal, row, []).in_flight == []


def test_a_session_s_effort_is_the_one_its_options_say(claude_accounts: tuple[Account, Account]) -> None:
    personal = claude_accounts[0]
    tmp_path = personal.jobs
    (tmp_path / "33333333").mkdir(parents=True)
    row = {"id": "33333333", "sessionId": SESSION, "pid": 7, "status": "idle", "state": "done"}
    for flags, effort in (
        (["--effort", "xhigh", "--name", "x", "--model", "opus"], "xhigh"),
        (["--name", "x", "--model", "opus", "--effort", "high"], "high"),
        (["--agent", "claude", "--model", "opus"], ""),
        (["--name", "x", "--effort"], ""),
    ):
        (tmp_path / "33333333" / "state.json").write_text(json.dumps({"state": "done", "respawnFlags": flags}))
        assert sessions._session(personal, row, []).effort == effort


def test_claude_runs_in_its_own_scope_on_the_account_it_names(
    monkeypatch, claude_accounts: tuple[Account, Account]
) -> None:
    monkeypatch.setattr(sessions.sys, "platform", "linux")
    personal, work = claude_accounts
    command = sessions.claude_command(personal, ["claude", "agents"])
    assert command[:3] == ["systemd-run", "--user", "--scope"]
    # One process killed for memory leaves the rest of the scope running: it can hold the daemon and its sessions.
    assert command[command.index("-p") + 1] == "OOMPolicy=continue"
    # The default folder runs with CLAUDE_CONFIG_DIR unset: set to ~/.claude, Claude reads another state file.
    assert command[-5:] == ["env", "-u", "CLAUDE_CONFIG_DIR", "claude", "agents"]
    assert sessions.claude_command(work, ["claude", "agents"])[-4:] == [
        "env", f"CLAUDE_CONFIG_DIR={work.folder}", "claude", "agents",
    ]


def test_claude_on_macos_needs_no_systemd(monkeypatch, claude_accounts: tuple[Account, Account]) -> None:
    monkeypatch.setattr(sessions.sys, "platform", "darwin")
    personal, _ = claude_accounts
    assert sessions.claude_command(personal, ["claude", "agents"]) == ["env", "-u", "CLAUDE_CONFIG_DIR", "claude", "agents"]


# The latest instruction a session got, which explains a block (SPEC PERM-13)

STAMP = "2026-01-09T10:00:00.500Z"


def typed(stamp: str, value: str | list[dict]) -> dict:
    return {"type": "user", "timestamp": stamp, "message": {"role": "user", "content": value}}


def from_peer(stamp: str, value: str) -> dict:
    return typed(stamp, [text(sessions.PEER_PREFIX + value)]) | {"isMeta": True, "origin": {"kind": "peer"}}


def queued(stamp: str, value: str) -> dict:
    return {"type": "attachment", "timestamp": stamp, "attachment": {"type": "queued_command", "commandMode": "prompt", "prompt": value}}


def test_last_instruction_is_the_latest_prompt_without_cockpits_header_and_context(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sessions, "PROJECTS_DIR", tmp_path)
    write_transcript(tmp_path, [
        typed("2026-01-09T09:00:00.000Z", "Check the text export."),
        from_peer(STAMP, (
            "Message from agent sweep via Cockpit (another agent, not Sam):\n\nAdd completed tasks to the export.\n\n"
            "---\nCockpit context:\nThis is about item 10."
        )),
        assistant("m1", {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}),
        typed("2026-01-09T10:10:00.000Z", [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]),
        typed("2026-01-09T10:20:00.000Z", "<task-notification>the tests finished</task-notification>"),
        typed("2026-01-09T10:20:30.000Z", [text("Caveat: written by Claude Code")]) | {"isMeta": True},
        from_peer("2026-01-09T10:30:00.000Z", '<agent-message from="explore">found it</agent-message>'),
        assistant("m2", text("Adding completed tasks now.")),
    ])
    assert sessions.last_instruction(SESSION) == {
        "author": "agent:sweep", "at": "2026-01-09T10:00:00Z", "text": "Add completed tasks to the export.",
    }
    assert sessions.last_instruction("44444444-4444-4444-8444-444444444444") is None


def test_a_batched_instruction_keeps_every_reply_without_cockpits_context(world, transcripts: Path) -> None:
    from cockpit import db, delivery, store
    from tests.test_api import put_card, put_item, put_step

    put_item(world, sessions=[SESSION_A])
    first = put_step(world, need="first", ask="Deploy version A?")["step"]
    second = put_card(world, "second", ask="Ready to ship?")["step"]
    replies = ["Use version B.\n\n* * *\n\nKeep the rollout small.", "Hold off until I approve.", "Check the logs first."]
    conn = db.connect(world.db_path)
    try:
        batch = [
            store.create_message(conn, "user", words, {"session_id": SESSION_A} | target, world.checkout.parent)
            for words, target in zip(replies, [{"step_id": first["id"]}, {"step_id": second["id"]}, {}], strict=True)
        ]
        prompt = delivery.compose_batch(conn, batch)
    finally:
        conn.close()
    transcripts.mkdir()
    write_transcript(transcripts, [typed(STAMP, prompt)])
    expected = "\n\n".join(replies)
    assert sessions.last_instruction(SESSION) == {"author": "user", "at": "2026-01-09T10:00:00Z", "text": expected}
    assert sessions.session_log(SESSION)["parts"][0]["text"] == expected
    # A batch that cut the session's turn short ends with the stop's notice after the batch's note.
    cut = sessions.instruction(prompt + "\n\n" + delivery.CUT_SHORT, STAMP, False)
    assert cut is not None and cut["text"] == expected
    # One message that quotes a sender line between rules is not a batch: every word of it stays.
    quoted = "Quote:\n\n* * *\n\nMessage from Ana via Cockpit:\nold words\n\n* * *\n\nDo not deploy until I approve."
    single = sessions.instruction("Message from Sam via Cockpit:\n\n" + quoted, STAMP, False)
    assert single is not None and single["text"] == quoted


@pytest.mark.parametrize(("entry", "author", "words"), [
    (typed(STAMP, "Message from Sam via Cockpit:\n\nPush it.\n\n---\nCockpit context:\nStep 9."), "user", "Push it."),
    (typed(STAMP, [text("Push it, please.")]), "user", "Push it, please."),
    (
        queued(STAMP, "Message from Cockpit itself (automatic, not from Sam or an agent):\nRetry the call."),
        "cockpit", "Retry the call.",
    ),
    (from_peer(STAMP, "Can you review my branch?"), "session", "Can you review my branch?"),
])
def test_an_instructions_author_is_named_by_cockpits_header_or_by_where_it_came_from(
    tmp_path: Path, monkeypatch, entry: dict, author: str, words: str
) -> None:
    monkeypatch.setattr(sessions, "PROJECTS_DIR", tmp_path)
    write_transcript(tmp_path, [entry, assistant("m1", text("On it."))])
    assert sessions.last_instruction(SESSION) == {"author": author, "at": "2026-01-09T10:00:00Z", "text": words}


def test_the_sessions_running_on_each_account_are_those_whose_turn_is_not_over() -> None:
    """SPEC SESSION-17: the picker counts the sessions that go on using an account's week."""
    turn = make_session(SESSION_A, pid=10, state="working", account="work")
    listed = [
        turn,
        replace(turn, session_id=SESSION_B),
        # A turn that ended on a question, a stopped session and a Codex session use nothing now.
        make_session(SESSION, pid=11, state="blocked", account="work"),
        make_session(SESSION, pid=None, state="working", account="personal"),
        replace(turn, account=""),
        # Background work keeps its turn going.
        replace(make_session(SESSION, pid=12, state="done", account="personal"), status="busy", in_flight=["local_bash"]),
    ]
    assert sessions.running(listed) == {"work": 2, "personal": 1}


def test_session_log_is_the_conversation_oldest_first_with_each_call_and_its_result(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sessions, "PROJECTS_DIR", tmp_path)
    stamp = {"timestamp": "2026-01-28T15:00:00.000Z"}
    write_transcript(tmp_path, [
        {"type": "user", "message": {"role": "user", "content": "Build the preview"}} | stamp,
        assistant("m1", text("Reading the prototype first."),
                  {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls docs", "description": "List the docs"}},
                  {"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "/x/missing.md"}}) | stamp,
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "REDESIGN-PLAN.md"},
            {"type": "tool_result", "tool_use_id": "t2", "content": [{"type": "text", "text": "File does not exist."}], "is_error": True},
        ]}} | stamp,
        assistant("m2", text("Done: the page is at /next.")) | stamp,
    ])
    log = sessions.session_log(SESSION)
    assert log["total"] == 5
    assert [part["kind"] for part in log["parts"]] == ["instruction", "says", "call", "call", "says"]
    assert log["parts"][0]["text"] == "Build the preview"
    assert log["parts"][2] | {"at": None} == {"at": None, "kind": "call", "id": "t1", "tool_name": "Bash", "title": "List the docs", "text": "ls docs",
                                              "result": "REDESIGN-PLAN.md", "error": False}
    assert (log["parts"][3]["title"], log["parts"][3]["result"], log["parts"][3]["error"]) == ("/x/missing.md", "File does not exist.", True)
    assert log["parts"][4]["text"] == "Done: the page is at /next."


def test_session_log_without_transcript_is_empty(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sessions, "PROJECTS_DIR", tmp_path)
    assert sessions.session_log(SESSION) == {"parts": [], "total": 0}


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\u0085"])
def test_jsonl_tail_preserves_unicode_line_separators(tmp_path: Path, monkeypatch, separator) -> None:
    monkeypatch.setattr(sessions, "PROJECTS_DIR", tmp_path)
    project = tmp_path / "-home-x"
    project.mkdir()
    words = f"Before{separator}after"
    entry = assistant("m1", text(words))
    (project / f"{SESSION}.jsonl").write_text(json.dumps(entry, ensure_ascii=False) + "\n")
    assert sessions.last_message(SESSION) == words


def cooldown_error(stamp: str, model: str) -> dict:
    entry = assistant("cooldown", text(
        f'API Error: 429 {{"error": {{"message": "All credentials for model {model} '
        'are cooling down (reset after 5m0s)"}}}'
    )) | {"timestamp": stamp, "error": "rate_limit", "isApiErrorMessage": True}
    entry["message"]["model"] = sessions.SYNTHETIC_MODEL
    return entry


def test_provider_cooldown_has_no_quota_reset_or_parse_error(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(sessions, "PROJECTS_DIR", tmp_path)
    entry = cooldown_error("2026-11-12T00:00:00Z", "claude-fable-5-2")
    write_transcript(tmp_path, [entry])
    assert sessions.limit_stop(SESSION) == sessions.LimitStop(
        at=entry["timestamp"], resets=None, cooldown_stops=1)
    assert sessions.last_model(SESSION) == "claude-fable-5-2"
    assert "no reset time" not in caplog.text


@pytest.mark.parametrize("failures", [1, 2, 3, 4, 5, 12])
def test_cooldown_streak_survives_cache_loss_and_caps_at_four(tmp_path, monkeypatch, failures):
    monkeypatch.setattr(sessions, "PROJECTS_DIR", tmp_path)
    entries = [cooldown_error(f"2026-11-12T00:{n:02d}:00Z", "claude-fable-5-2") for n in range(failures)]
    write_transcript(tmp_path, entries)
    assert sessions.limit_stop(SESSION).cooldown_stops == min(failures, 4)
    sessions._usages.clear()  # A server restart loses this cache, but retains the transcript.
    assert sessions.limit_stop(SESSION).cooldown_stops == min(failures, 4)


def test_cooldown_streak_resets_only_on_success_of_that_model(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "PROJECTS_DIR", tmp_path)
    fable = "claude-fable-5-2"
    first = cooldown_error("2026-11-12T00:00:00Z", fable)
    second = cooldown_error("2026-11-12T00:20:00Z", fable)
    success = assistant("success", text("working")) | {"timestamp": "2026-11-12T00:10:00Z"}
    success["message"]["model"] = "claude-opus-5-5"
    write_transcript(tmp_path, [first, success, second])
    assert sessions.limit_stop(SESSION).cooldown_stops == 2
    success["message"]["model"] = fable
    write_transcript(tmp_path, [first, success, second])
    assert sessions.limit_stop(SESSION).cooldown_stops == 1


def test_other_model_cooldowns_and_duplicate_blocks_do_not_grow_the_streak(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "PROJECTS_DIR", tmp_path)
    entry = cooldown_error("2026-11-12T00:10:00Z", "claude-fable-5-2")
    write_transcript(tmp_path, [cooldown_error("2026-11-12T00:00:00Z", "claude-opus-5-5"), entry, entry])
    assert sessions.limit_stop(SESSION).cooldown_stops == 1


def test_cooldown_streak_reads_past_a_large_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(sessions, "TRANSCRIPT_TAIL_BYTES", 600)
    write_transcript(tmp_path, [
        cooldown_error("2026-11-12T00:00:00Z", "claude-fable-5-2"),
        {"type": "user", "message": {"content": "x" * 2000}},
        cooldown_error("2026-11-12T00:20:00Z", "claude-fable-5-2"),
    ])
    assert sessions.limit_stop(SESSION).cooldown_stops == 2


def test_success_quoting_a_cooldown_keeps_the_actual_answer_model(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "PROJECTS_DIR", tmp_path)
    entry = assistant("success", text("All credentials for model claude-fable-5-2 are cooling down"))
    entry["message"]["model"] = "claude-opus-5-5"
    write_transcript(tmp_path, [entry])
    assert sessions.last_model(SESSION) == "claude-opus-5-5"


def test_same_model_success_resets_backoff_after_restart_and_tail_expansion(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(sessions, "TRANSCRIPT_TAIL_BYTES", 600)
    model = "claude-fable-5-2"
    success = assistant("success", text("working")) | {"timestamp": "2026-11-12T00:15:00Z"}
    success["message"]["model"] = model
    write_transcript(tmp_path, [
        cooldown_error("2026-11-12T00:00:00Z", model),
        cooldown_error("2026-11-12T00:10:00Z", model),
        success,
        {"type": "user", "message": {"content": "x" * 2000}},
        cooldown_error("2026-11-12T00:20:00Z", model),
    ])
    sessions._usages.clear()
    assert sessions.limit_stop(SESSION).cooldown_stops == 1


def rate_limit_error(stamp: str) -> dict:
    entry = assistant("rate", text(
        "API Error: Request rejected (429) · This request would exceed your account's rate limit. Please try again later."
    )) | {"timestamp": stamp, "error": "rate_limit", "isApiErrorMessage": True}
    entry["message"]["model"] = sessions.SYNTHETIC_MODEL
    return entry


def test_a_plain_rate_limit_is_retried_like_a_cooldown_with_no_parse_error(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(sessions, "PROJECTS_DIR", tmp_path)
    first = rate_limit_error("2026-11-12T09:40:00Z")
    write_transcript(tmp_path, [first])
    assert sessions.limit_stop(SESSION) == sessions.LimitStop(at=first["timestamp"], resets=None, cooldown_stops=1)
    assert "no reset time" not in caplog.text
    # A cooldown on the model follows it, and both count toward the backoff.
    write_transcript(tmp_path, [first, cooldown_error("2026-11-12T09:41:00Z", "claude-opus-5-5")])
    assert sessions.limit_stop(SESSION).cooldown_stops == 2
    # Any model's answer after it ends the streak.
    success = assistant("success", text("working")) | {"timestamp": "2026-11-12T09:50:00Z"}
    success["message"]["model"] = "claude-opus-5-5"
    write_transcript(tmp_path, [first, success, rate_limit_error("2026-11-12T10:00:00Z")])
    assert sessions.limit_stop(SESSION).cooldown_stops == 1
