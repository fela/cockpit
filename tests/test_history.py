"""An item's history: each thing the user asked or did, what came back, and how the time between went
(SPEC VIEW-10, VIEW-13, history.py)."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cockpit import config, db, history, store
from tests.conftest import SESSION_A, SESSION_B, World
from tests.test_api import put_item, put_step

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
LONG = "Plan a Weekend list in Tasks with shopping, reading, errands and reminders, " * 3


def connect(world: World) -> sqlite3.Connection:
    return db.connect(world.db_path)


def versions(conn: sqlite3.Connection, step_id: int) -> list[dict[str, Any]]:
    return db.rows(conn.execute("SELECT * FROM step_versions WHERE step_id = ? ORDER BY id", (step_id,)).fetchall())


def test_a_put_that_changes_the_text_keeps_a_version_and_a_score_change_does_not(world: World) -> None:
    put_item(world)
    step = put_step(world)["step"]
    put_step(world, score=80)
    put_step(world, ask="Approve PR 10 now", details=[{"title": "Checks", "body": "CI green"}])
    conn = connect(world)
    kept = versions(conn, step["id"])
    assert [version["ask"] for version in kept] == ["Approve PR 10", "Approve PR 10 now"]
    assert kept[1]["details"] == [{"title": "Checks", "body": "CI green"}]
    assert kept[1]["actor"] == "agent:sweep"


def test_a_put_that_changes_only_the_reply_hint_keeps_a_version(world: World) -> None:
    put_item(world)
    step = put_step(world, reply_hint="Say yes or no")["step"]
    put_step(world, reply_hint="Say which PR to merge")
    kept = versions(connect(world), step["id"])
    assert [version["reply_hint"] for version in kept] == ["Say yes or no", "Say which PR to merge"]


def backdate(conn: sqlite3.Connection, step_id: int, at: str) -> None:
    """The step was put, as its first version, at `at`."""
    conn.execute("UPDATE step_versions SET at = ? WHERE step_id = ?", (at, step_id))
    conn.execute("UPDATE steps SET created_at = ? WHERE id = ?", (at, step_id))


def turn(at: str, ms: int) -> dict[str, Any]:
    return {"type": "system", "subtype": "turn_duration", "durationMs": ms, "timestamp": at, "sessionId": SESSION_A}


def reply(at: str, text: str, message_id: str) -> dict[str, Any]:
    return {
        "type": "assistant", "timestamp": at, "isSidechain": False,
        "message": {"id": message_id, "role": "assistant", "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": text}]},
    }


def transcript(folder: Path, *entries: dict[str, Any]) -> Path:
    project = folder / "-home-sam-dev-app"
    project.mkdir(parents=True, exist_ok=True)
    path = project / f"{SESSION_A}.jsonl"
    with path.open("a") as handle:
        handle.write("".join(json.dumps(entry, separators=(",", ":")) + "\n" for entry in entries))
    return path


def test_turns_are_read_once_each_with_the_reply_that_ended_them(world: World, transcripts: Path) -> None:
    conn = connect(world)
    path = transcript(
        transcripts,
        reply("2026-01-15T10:00:00.000Z", "Created the Weekend list.", "m1"),
        turn("2026-01-15T10:00:01.000Z", 61_000),
        reply("2026-01-15T10:05:00.000Z", "Still working", "m2"),
    )
    assert history.read_turns(conn, SESSION_A, path) == 1
    transcript(transcripts, turn("2026-01-15T10:05:02.000Z", 2_000))
    assert history.read_turns(conn, SESSION_A, path) == 1
    assert history.read_turns(conn, SESSION_A, path) == 0
    turns = db.rows(conn.execute("SELECT started_at, ended_at, reply FROM session_turns ORDER BY id").fetchall())
    assert turns == [
        {"started_at": "2026-01-15T09:59:00Z", "ended_at": "2026-01-15T10:00:01Z", "reply": "Created the Weekend list."},
        {"started_at": "2026-01-15T10:05:00Z", "ended_at": "2026-01-15T10:05:02Z", "reply": "Still working"},
    ]


def test_a_reply_past_the_limit_says_where_it_was_cut(world: World, transcripts: Path) -> None:
    conn = connect(world)
    path = transcript(transcripts, reply("2026-01-15T10:00:00.000Z", "a" * (history.REPLY_CHARS + 5), "m1"),
                      turn("2026-01-15T10:00:01.000Z", 1_000))
    assert history.read_turns(conn, SESSION_A, path) == 1
    kept = conn.execute("SELECT reply FROM session_turns").fetchone()[0]
    assert kept == "a" * history.REPLY_CHARS + "\n\n[Cut at 20,000 characters; the rest is in the session.]"


def message(conn: sqlite3.Connection, item_id: int, step_id: int | None, text: str, at: str) -> int:
    return conn.execute(
        "INSERT INTO messages (author, item_id, session_id, step_id, text, state, created_at, sent_at)"
        " VALUES ('user', ?, ?, ?, ?, 'sent', ?, ?)",
        (item_id, SESSION_A, step_id, text, at, at),
    ).lastrowid


def test_each_action_pairs_with_the_rewrite_of_its_step_or_else_the_agents_reply(world: World, transcripts: Path) -> None:
    item = put_item(world, sessions=[SESSION_A])
    step = put_step(world)["step"]
    conn = connect(world)
    backdate(conn, step["id"], "2026-01-15T08:00:00Z")
    message(conn, item["id"], step["id"], LONG, "2026-01-15T09:00:00Z")
    conn.execute(
        "INSERT INTO step_versions (step_id, at, actor, ask, why, body, details, draft, answers, command, permission)"
        " VALUES (?, '2026-01-15T09:20:00Z', 'agent:sweep', 'Choose the first reminder', 'Four tasks are ready', ?, '[]',"
        " '', '[]', 'null', 'null')",
        (step["id"], "**What changes.** " + "Each task has a title and a reminder. " * 4),
    )
    message(conn, item["id"], step["id"], "Start with shopping", "2026-01-15T10:00:00Z")
    path = transcript(
        transcripts,
        reply("2026-01-15T09:10:00.000Z", "Added four tasks.", "m1"),
        turn("2026-01-15T09:10:00.000Z", 600_000),
        reply("2026-01-15T10:30:00.000Z", "Started on it; a step follows.", "m2"),
        turn("2026-01-15T10:30:05.000Z", 300_000),
    )
    history.read_turns(conn, SESSION_A, path)

    found = history.item_history(conn, item["id"], NOW, set())
    first, second = found["entries"]
    assert first["you"] == {"kind": "message", "text": LONG}
    assert first["answer"]["kind"] == "step"
    assert first["answer"]["text"] == "Choose the first reminder"
    assert second["answer"] == {
        "kind": "reply", "at": "2026-01-15T10:30:05Z", "session_id": SESSION_A,
        "text": "Started on it; a step follows.", "text_html": "Started on it; a step follows.", "summary": None,
    }
    # From the first message to the second: the agent's turn, then the rewritten step waits on the user.
    assert first["after"]["time"]["seconds"] == {"you": 0, "agent": 600, "waiting_on_you": 2400, "waiting": 600}
    # The long step waits for a summary; the short texts show whole, and the user's message always shows in its own
    # words.
    waiting = [record["ref"] for record in conn.execute("SELECT ref FROM summaries WHERE ref NOT LIKE 'story:%' ORDER BY ref")]
    assert waiting == ["version:2"]

    def ask(kind: str, text: str) -> str:
        return f"{kind}: {text[:20]}"

    while history.summarize_next(conn, ask) is not None:
        pass
    again = history.item_history(conn, item["id"], NOW, set())
    assert again["entries"][0]["you"] == {"kind": "message", "text": LONG}
    assert again["entries"][0]["answer"]["summary"] == "version: Choose the first rem"


def test_the_users_message_shows_in_their_own_words(world: World) -> None:
    # The user wants their actual message, not an AI summary; a started idea keeps the sweeper's
    # attribution too.
    item = put_item(world, sessions=[SESSION_A])
    step = put_step(world)["step"]
    conn = connect(world)
    for at, text in (("2026-01-15T09:00:00Z", LONG), ("2026-01-15T10:00:00Z", history.STARTED_IDEA + LONG)):
        message(conn, item["id"], step["id"], text, at)
    entries = history.item_history(conn, item["id"], NOW, set())["entries"]
    assert [entry["you"] for entry in entries] == [{"kind": "message", "text": LONG},
                                                   {"kind": "message", "text": history.STARTED_IDEA + LONG}]
    assert conn.execute("SELECT COUNT(*) FROM summaries WHERE ref LIKE 'message:%'").fetchone()[0] == 0


def test_a_press_with_nothing_back_but_a_closure_folds_into_the_entry_before_it(world: World) -> None:
    item = put_item(world)
    step = put_step(world)["step"]
    conn = connect(world)
    backdate(conn, step["id"], "2026-01-15T08:00:00Z")
    message(conn, item["id"], step["id"], "Why is this needed?", "2026-01-15T09:00:00Z")
    conn.execute(
        "INSERT INTO events (at, actor, kind, item_id, step_id) VALUES ('2026-01-15T10:00:00Z', 'user', 'step.ran', ?, ?)",
        (item["id"], step["id"]),
    )
    conn.execute(
        "INSERT INTO closures (kind, ref_id, item_id, action, closed_by, closed_at, note)"
        " VALUES ('step', ?, ?, 'done', 'agent:sweep', '2026-01-15T10:05:00Z', 'posted in the thread')",
        (step["id"], item["id"]),
    )
    found = history.item_history(conn, item["id"], NOW, set())
    (entry,) = found["entries"]
    assert entry["you"]["text"] == "Why is this needed?"
    assert entry["notes"] == ["Then you ran the step's command, and the step closed as done: posted in the thread"]
    # The entry's time runs on to now, through the press: one stretch of waiting, then the wait on the user.
    time = entry["after"]["time"]
    assert (time["from"], time["to"]) == ("2026-01-15T09:00:00Z", "2026-01-15T12:00:00Z")
    assert time["seconds"] == {"you": 0, "agent": 0, "waiting_on_you": 6900, "waiting": 3900}
    assert [segment[2] for segment in time["segments"]] == ["waiting", "waiting_on_you"]
    # The press keeps its time and step, for the card's current exchange and the story's input.
    assert entry["folded"] == [{"at": "2026-01-15T10:00:00Z", "step_id": step["id"], "note": entry["notes"][0]}]
    steps = {step["id"]: db.rows(conn.execute("SELECT * FROM steps WHERE id = ?", (step["id"],)).fetchall())[0]}
    told = history.story_text(conn, store.get_item(conn, item["id"]), None, found["entries"], steps)
    assert "2026-01-15 10:00 UTC: " in told and "ran the step's command, and the step closed as done" in told
    # A step put up after the closure does not answer the press: the closure came back first.
    later = put_step(world, need="complete", ask="Is this task done?")["step"]
    conn.execute("UPDATE step_versions SET at = '2026-01-15T11:00:00Z' WHERE step_id = ?", (later["id"],))
    (entry,) = history.item_history(conn, item["id"], NOW, set())["entries"]
    assert entry["notes"] == ["Then you ran the step's command, and the step closed as done: posted in the thread"]
    conn.execute("DELETE FROM steps WHERE id = ?", (later["id"],))
    # A new version of the pressed step itself after the closure (reopened and rewritten) is the press's answer.
    conn.execute(
        "INSERT INTO step_versions (step_id, at, actor, ask, why, body, details, draft, answers, command, permission)"
        " SELECT step_id, '2026-01-15T10:30:00Z', actor, 'Review the failure', why, body, details, draft, answers,"
        " command, permission FROM step_versions WHERE step_id = ?",
        (step["id"],),
    )
    pressed = history.item_history(conn, item["id"], NOW, set())["entries"]
    assert [(entry["you"]["kind"], entry["answer"]["kind"]) for entry in pressed] == [("message", "none"), ("ran", "step")]
    conn.execute("DELETE FROM step_versions WHERE at = '2026-01-15T10:30:00Z'")
    # A press the agent has yet to answer stays an entry of its own.
    conn.execute("DELETE FROM closures")
    assert [entry["you"]["kind"] for entry in history.item_history(conn, item["id"], NOW, set())["entries"]] == [
        "message", "ran"]


@pytest.mark.parametrize(("action", "description", "folded_description"), [
    ("done", "Marked the step done", "Then you marked the step done"),
    ("dismiss", "Dismissed the step", "Then you dismissed the step"),
])
def test_the_user_s_done_and_dismiss_are_actions_in_the_history(
    world: World, action: str, description: str, folded_description: str
) -> None:
    """The user's closes are events (store.step_action records closures only for agents). A close waits for no
    answer: with nothing back it folds into the entry before it."""
    item = put_item(world)
    step = put_step(world)["step"]
    assert world.client.post(f"/api/steps/{step['id']}/{action}", json={}, headers=world.user()).status_code == 200
    (entry,) = history.item_history(connect(world), item["id"], datetime.now(UTC), set())["entries"]
    assert (entry["you"]["kind"], entry["you"]["text"], entry["step_id"]) == ("closed", description, step["id"])
    assert entry["answer"]["kind"] == "none"
    conn = connect(world)
    message(conn, item["id"], step["id"], "Is this still needed?", "2026-01-15T09:00:00Z")
    (entry,) = history.item_history(conn, item["id"], datetime.now(UTC), set())["entries"]
    assert entry["you"]["text"] == "Is this still needed?"
    assert [press["step_id"] for press in entry["folded"]] == [step["id"]]
    assert entry["notes"] == [folded_description]
    assert [press["note"] for press in entry["folded"]] == [folded_description]


def test_a_run_is_answered_only_by_the_session_it_went_to(world: World) -> None:
    item = put_item(world, sessions=[SESSION_A, SESSION_B])
    step = put_step(world)["step"]
    conn = connect(world)
    backdate(conn, step["id"], "2026-01-15T08:00:00Z")
    conn.execute(
        "INSERT INTO messages (author, item_id, session_id, step_id, text, state, created_at, sent_at, hands_off)"
        " VALUES ('cockpit', ?, ?, ?, 'Ran it', 'sent', '2026-01-15T09:00:00Z', '2026-01-15T09:00:01Z', 1)",
        (item["id"], SESSION_A, step["id"]),
    )
    conn.execute(
        "INSERT INTO events (at, actor, kind, item_id, step_id) VALUES ('2026-01-15T09:00:00Z', 'user', 'step.ran', ?, ?)",
        (item["id"], step["id"]),
    )
    for session_id, ended, text in ((SESSION_B, "10:00", "Other work done."), (SESSION_A, "10:30", "The run passed.")):
        conn.execute(
            "INSERT INTO session_turns (session_id, started_at, ended_at, reply) VALUES (?, ?, ?, ?)",
            (session_id, "2026-01-15T09:00:05Z", f"2026-01-15T{ended}:00Z", text),
        )
    (entry,) = history.item_history(conn, item["id"], NOW, set())["entries"]
    assert (entry["you"]["kind"], entry["answer"]["session_id"], entry["answer"]["text"]) == (
        "ran", SESSION_A, "The run passed."
    )


def test_a_codex_turn_stamped_to_the_millisecond_reads_as_whole_seconds(world: World) -> None:
    """A Codex turn's ended_at has milliseconds (codex_agent.py); the history compares times as text in whole
    seconds, so it reads the turn as ending in its second, as a turn stamped without them does."""
    item = put_item(world, sessions=[SESSION_A])
    step = put_step(world)["step"]
    conn = connect(world)
    backdate(conn, step["id"], "2026-01-15T08:00:00Z")
    message(conn, item["id"], step["id"], "Is it merged?", "2026-01-15T09:00:00Z")
    message(conn, item["id"], step["id"], "And deployed?", "2026-01-15T10:00:00Z")
    turn = conn.execute(
        "INSERT INTO session_turns (session_id, started_at, ended_at, reply) VALUES (?, ?, ?, ?)",
        (SESSION_A, "2026-01-15T09:00:00Z", "2026-01-15T10:00:00.500Z", "Merged."),
    ).lastrowid
    stamped = history.item_history(conn, item["id"], NOW, set())["entries"]
    conn.execute("UPDATE session_turns SET ended_at = '2026-01-15T10:00:00Z' WHERE id = ?", (turn,))
    assert stamped == history.item_history(conn, item["id"], NOW, set())["entries"]
    assert stamped[0]["answer"]["at"] == "2026-01-15T10:00:00Z"


def test_the_wait_on_the_user_ends_when_an_agent_closes_the_item_s_last_step(world: World) -> None:
    """SPEC VIEW-13: with no step open, the item waits on no one in the user's queue."""
    item = put_item(world)
    step = put_step(world)["step"]
    conn = connect(world)
    backdate(conn, step["id"], "2026-01-15T08:00:00Z")
    message(conn, item["id"], step["id"], "Is it merged?", "2026-01-15T09:00:00Z")
    conn.execute(
        "UPDATE steps SET status = 'done', closed_at = '2026-01-15T10:00:00Z' WHERE id = ?", (step["id"],)
    )
    conn.execute(
        "INSERT INTO closures (kind, ref_id, item_id, action, closed_by, closed_at, note)"
        " VALUES ('step', ?, ?, 'done', 'agent:sweep', '2026-01-15T10:00:00Z', 'merged')",
        (step["id"], item["id"]),
    )
    (entry,) = history.item_history(conn, item["id"], NOW, set())["entries"]
    assert entry["answer"]["kind"] == "closed"
    assert entry["after"]["time"]["seconds"] == {"you": 0, "agent": 0, "waiting_on_you": 0, "waiting": 10800}


def test_an_item_with_an_open_step_and_an_earlier_exchange_gets_its_story_kept_until_a_newer_one_is_written(
    world: World,
) -> None:
    item = put_item(world)
    step = put_step(world)["step"]
    conn = connect(world)
    backdate(conn, step["id"], "2026-01-15T08:00:00Z")
    message(conn, item["id"], step["id"], "Why is this needed?", "2026-01-15T09:00:00Z")
    # One exchange: nothing before it to tell, so no story.
    assert history.item_history(conn, item["id"], NOW, set())["story"] is None

    message(conn, item["id"], step["id"], "Merge it", "2026-01-15T10:00:00Z")
    found = history.item_history(conn, item["id"], NOW, set())
    assert found["story"] == {"summary": None, "lines": None, "made_at": None, "current": False, "failed": None}
    asked = conn.execute("SELECT text FROM summaries WHERE ref LIKE 'story:%'").fetchone()["text"]
    assert "Task: PR 10" in asked and "Why is this needed?" in asked and "Merge it" in asked

    stories: list[str] = []

    def ask(kind: str, text: str) -> str:
        stories.append(kind)
        return f"Summary: Task {len(stories)} so far.\n2026-01-15 09:00: You asked why\n2026-01-15 10:00 UTC: Story {len(stories)}"

    while history.summarize_next(conn, ask) is not None:
        pass
    written = history.item_history(conn, item["id"], NOW, set())["story"]
    assert written["summary"] == "Task 1 so far."
    assert written["lines"] == [
        {"at": "2026-01-15T09:00:00Z", "text": "You asked why"}, {"at": "2026-01-15T10:00:00Z", "text": "Story 1"}]

    # The history changes twice before Claude gets to it: the last story shows meanwhile, and only the newest
    # history waits for a new one.
    message(conn, item["id"], step["id"], "Also the tests", "2026-01-15T11:00:00Z")
    history.item_history(conn, item["id"], NOW, set())
    message(conn, item["id"], step["id"], "And the docs", "2026-01-15T11:30:00Z")
    again = history.item_history(conn, item["id"], NOW, set())["story"]
    assert (again["lines"][-1]["text"], again["current"]) == ("Story 1", False)
    assert conn.execute("SELECT COUNT(*) FROM summaries WHERE ref LIKE 'story:%' AND summary IS NULL").fetchone()[0] == 1
    while history.summarize_next(conn, ask) is not None:
        pass
    assert history.item_history(conn, item["id"], NOW, set())["story"]["lines"][-1]["text"] == "Story 2"
    assert stories == ["story", "story"]

    # Once its last step closes, the item row shows the last story as it is, and Claude writes no new one. A story
    # still waiting when the step closes is dropped.
    message(conn, item["id"], step["id"], "Thanks", "2026-01-15T12:00:00Z")
    history.item_history(conn, item["id"], NOW, set())
    assert conn.execute("SELECT COUNT(*) FROM summaries WHERE ref LIKE 'story:%' AND summary IS NULL").fetchone()[0] == 1
    conn.execute("UPDATE steps SET status = 'done' WHERE id = ?", (step["id"],))
    closed = history.item_history(conn, item["id"], NOW, set())["story"]
    assert (closed["lines"][-1]["text"], closed["current"], closed["failed"]) == ("Story 2", True, None)
    assert conn.execute("SELECT COUNT(*) FROM summaries WHERE ref LIKE 'story:%' AND summary IS NULL").fetchone()[0] == 0

    # An item that never had a story while it had a step gets none.
    other = put_item(world, key="session:other", title="PR 20")
    later = put_step(world, item_key="session:other")["step"]
    message(conn, other["id"], later["id"], "Why is this needed?", "2026-01-15T09:00:00Z")
    message(conn, other["id"], later["id"], "Merge it", "2026-01-15T10:00:00Z")
    conn.execute("UPDATE steps SET status = 'done' WHERE id = ?", (later["id"],))
    assert history.item_history(conn, other["id"], NOW, set())["story"] is None
    assert conn.execute("SELECT COUNT(*) FROM summaries WHERE ref LIKE ?", (f"story:{other['id']}:%",)).fetchone()[0] == 0


def test_an_install_without_claude_asks_for_no_story_or_summary(world: World, monkeypatch) -> None:
    # on a Codex-only install nothing writes them, so the card waited on "Writing the history…" for ever.
    setup = config.setup()
    monkeypatch.setattr(config, "setup", lambda: dataclasses.replace(setup, providers=("codex",)))
    item = put_item(world)
    step = put_step(world)["step"]
    conn = connect(world)
    backdate(conn, step["id"], "2026-01-15T08:00:00Z")
    message(conn, item["id"], step["id"], "Why is this needed? " * 40, "2026-01-15T09:00:00Z")
    message(conn, item["id"], step["id"], "Merge it", "2026-01-15T10:00:00Z")
    found = history.item_history(conn, item["id"], NOW, set())
    assert found["story"] is None
    assert conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 0


def test_a_story_without_a_time_on_each_line_is_not_kept(world: World) -> None:
    item = put_item(world)
    step = put_step(world)["step"]
    conn = connect(world)
    message(conn, item["id"], step["id"], "Why is this needed?", "2026-01-15T09:00:00Z")
    message(conn, item["id"], step["id"], "Merge it", "2026-01-15T10:00:00Z")
    history.item_history(conn, item["id"], NOW, set())

    def paragraph(kind: str, text: str) -> str:
        return "15 Jan: You asked why it is needed.\n2026-01-15 10:00: You asked to merge it."

    while history.summarize_next(conn, paragraph) is not None:
        pass
    story = history.item_history(conn, item["id"], NOW, set())["story"]
    assert story["lines"] is None
    assert story["failed"].startswith("a line of the story has no time first: 15 Jan")


def test_a_story_without_its_summary_line_first_is_not_kept(world: World) -> None:
    item = put_item(world)
    step = put_step(world)["step"]
    conn = connect(world)
    message(conn, item["id"], step["id"], "Why is this needed?", "2026-01-15T09:00:00Z")
    message(conn, item["id"], step["id"], "Merge it", "2026-01-15T10:00:00Z")
    history.item_history(conn, item["id"], NOW, set())

    def lines_only(kind: str, text: str) -> str:
        return "2026-01-15 09:00: You asked why it is needed.\n2026-01-15 10:00: You asked to merge it."

    while history.summarize_next(conn, lines_only) is not None:
        pass
    story = history.item_history(conn, item["id"], NOW, set())["story"]
    assert (story["summary"], story["lines"]) == (None, None)
    assert story["failed"].startswith("the story has no summary line first")


def test_a_failed_summary_is_kept_with_its_error_and_not_asked_again(world: World) -> None:
    item = put_item(world)
    step = put_step(world, body=LONG)["step"]
    conn = connect(world)
    backdate(conn, step["id"], "2026-01-15T09:30:00Z")
    message(conn, item["id"], step["id"], "Merge it", "2026-01-15T09:00:00Z")
    history.item_history(conn, item["id"], NOW, set())

    def broken(kind: str, text: str) -> str:
        raise history.SummaryError("claude exited with 1")

    assert history.summarize_next(conn, broken) is not None
    assert history.summarize_next(conn, broken) is None
    record = conn.execute("SELECT summary, failed FROM summaries").fetchone()
    assert (record["summary"], record["failed"]) == (None, "claude exited with 1")


def test_the_time_between_counts_each_moment_once_strongest_first() -> None:
    split = history.time_split(
        {
            "you": [("2026-01-15T10:10:00Z", "2026-01-15T10:20:00Z")],
            "agent": [("2026-01-15T10:00:00Z", "2026-01-15T10:15:00Z"), ("2026-01-15T10:14:00Z", "2026-01-15T10:30:00Z")],
            "waiting_on_you": [("2026-01-15T10:30:00Z", "2026-01-15T11:00:00Z")],
        },
        "2026-01-15T10:05:00Z",
        "2026-01-15T11:30:00Z",
    )
    assert [segment[2] for segment in split["segments"]] == ["agent", "you", "agent", "waiting_on_you", "waiting"]
    assert split["seconds"] == {"you": 600, "agent": 900, "waiting_on_you": 1800, "waiting": 1800}


def test_the_page_reads_an_items_history_and_a_missing_item_is_404(world: World) -> None:
    item = put_item(world)
    put_step(world)
    response = world.client.get(f"/api/items/{item['id']}/history")
    assert response.status_code == 200, response.text
    assert response.json()["entries"] == []
    assert world.client.get("/api/items/999/history").status_code == 404


def test_the_backfill_leaves_the_database_free_for_other_writers_while_it_assembles(
    world: World, monkeypatch: Any
) -> None:
    # A transaction around each item's assembly held the write lock for the whole backfill, one item after another,
    # so the other workers' writes waited past the busy timeout at every start.
    item = put_item(world)
    step = put_step(world, body=LONG)["step"]
    conn = connect(world)
    backdate(conn, step["id"], "2026-01-15T09:30:00Z")
    message(conn, item["id"], step["id"], "Merge it", "2026-01-15T09:00:00Z")
    assembled = history.item_history
    writes: list[int] = []

    def assemble_and_write_beside(conn: sqlite3.Connection, item_id: int, now: datetime, working: set[str]) -> Any:
        found = assembled(conn, item_id, now, working)
        other = sqlite3.connect(world.db_path, timeout=0, isolation_level=None)
        other.execute("BEGIN IMMEDIATE")
        other.execute("COMMIT")
        other.close()
        writes.append(item_id)
        return found

    monkeypatch.setattr(history, "item_history", assemble_and_write_beside)
    assert history.backfill(conn, NOW) == 1
    assert writes == [item["id"]]
    assert conn.execute("SELECT COUNT(*) FROM summaries WHERE summary IS NULL").fetchone()[0] == 1


def test_a_message_waiting_for_its_session_shows_at_once_and_leaves_the_turn_under_way_to_the_one_before(
    world: World, transcripts: Path
) -> None:
    """the user's message showed in the history only once it reached the busy session, after its earlier turn ended."""
    item = put_item(world, sessions=[SESSION_A])
    step = put_step(world)["step"]
    conn = connect(world)
    backdate(conn, step["id"], "2026-01-15T08:00:00Z")
    message(conn, item["id"], step["id"], "Wait for the reminder preview", "2026-01-15T09:00:00Z")
    conn.execute(
        "INSERT INTO messages (author, item_id, session_id, step_id, text, state, created_at)"
        " VALUES ('user', ?, ?, ?, 'Merge it into main', 'queued', '2026-01-15T11:00:00Z')",
        (item["id"], SESSION_A, step["id"]),
    )
    path = transcript(
        transcripts,
        reply("2026-01-15T11:30:00.000Z", "Reminder preview added.", "m1"),
        turn("2026-01-15T11:30:05.000Z", 9_000_000),
    )
    history.read_turns(conn, SESSION_A, path)

    first, second = history.item_history(conn, item["id"], datetime(2026, 1, 15, 11, 31, tzinfo=UTC), set())["entries"]
    assert first["answer"]["kind"] == "reply"
    assert first["answer"]["text"] == "Reminder preview added."
    assert second["at"] == "2026-01-15T11:00:00Z"
    assert second["you"] == {"kind": "message", "text": "Merge it into main"}
    assert second["answer"]["kind"] == "unsent"
    # The time runs on in the exchange before it, to now; the waiting message holds none until delivered.
    assert (first["after"]["time"]["from"], first["after"]["time"]["to"]) == ("2026-01-15T09:00:00Z", "2026-01-15T11:31:00Z")
    assert sum(second["after"]["time"]["seconds"].values()) == 0

    conn.execute("UPDATE messages SET state = 'sent', sent_at = '2026-01-15T11:30:06Z' WHERE state = 'queued'")
    _, delivered = history.item_history(conn, item["id"], datetime(2026, 1, 15, 11, 31, tzinfo=UTC), set())["entries"]
    assert delivered["at"] == "2026-01-15T11:30:06Z"
    assert delivered["answer"]["kind"] == "waiting"


def test_a_message_taken_back_before_it_went_out_stays_out_of_the_history(world: World) -> None:
    item = put_item(world, sessions=[SESSION_A])
    conn = connect(world)
    conn.execute(
        "INSERT INTO messages (author, item_id, session_id, text, state, created_at, dismissed_at)"
        " VALUES ('user', ?, ?, 'Never mind', 'failed', '2026-01-15T11:00:00Z', '2026-01-15T11:00:05Z')",
        (item["id"], SESSION_A),
    )
    assert history.item_history(conn, item["id"], NOW, set())["entries"] == []


def test_a_press_after_a_waiting_message_folds_into_the_delivered_exchange_and_stays_in_the_story(world: World) -> None:
    item = put_item(world, sessions=[SESSION_A])
    step = put_step(world)["step"]
    conn = connect(world)
    backdate(conn, step["id"], "2026-01-15T08:00:00Z")
    message(conn, item["id"], step["id"], "Why is this needed?", "2026-01-15T09:00:00Z")
    conn.execute(
        "INSERT INTO messages (author, item_id, session_id, step_id, text, state, created_at)"
        " VALUES ('user', ?, ?, ?, 'Merge it into main', 'queued', '2026-01-15T09:30:00Z')",
        (item["id"], SESSION_A, step["id"]),
    )
    conn.execute(
        "INSERT INTO events (at, actor, kind, item_id, step_id) VALUES ('2026-01-15T10:00:00Z', 'user', 'step.ran', ?, ?)",
        (item["id"], step["id"]),
    )
    conn.execute(
        "INSERT INTO closures (kind, ref_id, item_id, action, closed_by, closed_at, note)"
        " VALUES ('step', ?, ?, 'done', 'agent:sweep', '2026-01-15T10:05:00Z', 'posted in the thread')",
        (step["id"], item["id"]),
    )
    found = history.item_history(conn, item["id"], NOW, set())
    first, waiting = found["entries"]
    assert first["notes"] == ["Then you ran the step's command, and the step closed as done: posted in the thread"]
    assert (first["after"]["time"]["from"], first["after"]["time"]["to"]) == ("2026-01-15T09:00:00Z", "2026-01-15T12:00:00Z")
    assert first["after"]["time"]["seconds"]["waiting_on_you"] == 6900
    assert waiting["answer"]["kind"] == "unsent"
    steps = {step["id"]: db.rows(conn.execute("SELECT * FROM steps WHERE id = ?", (step["id"],)).fetchall())[0]}
    delivered = [entry for entry in found["entries"] if entry["answer"]["kind"] != "unsent"]
    told = history.story_text(conn, store.get_item(conn, item["id"]), None, delivered, steps)
    assert "ran the step's command" in told and "Merge it into main" not in told


def test_a_list_of_priorities_that_fits_the_line_needs_no_summary(world: World) -> None:
    conn = connect(world)
    assert history.goals_summary(conn, ["Lists first", "Ship A"]) is None
    assert history.goals_summary(conn, []) is None
    assert conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 0


def test_a_longer_list_of_priorities_gets_a_summary_once_and_a_changed_list_a_new_one(world: World) -> None:
    conn = connect(world)
    texts = ["finish list sharing and reminder settings"]
    assert history.goals_summary(conn, texts) is None
    assert history.goals_summary(conn, texts) is None
    asked: list[tuple[str, str]] = []

    def ask(kind: str, text: str) -> str:
        asked.append((kind, text))
        return "Sharing and reminders" if text == texts[0] else "Sharing and reviews"

    while history.summarize_next(conn, ask) is not None:
        pass
    assert asked == [("goals", "finish list sharing and reminder settings")]
    assert history.goals_summary(conn, texts) == "Sharing and reminders"
    changed = [*texts, "reviews"]
    assert history.goals_summary(conn, changed) is None
    while history.summarize_next(conn, ask) is not None:
        pass
    assert asked == [("goals", texts[0]), ("goals", "\n".join(changed))]
    assert history.goals_summary(conn, changed) == "Sharing and reviews"
    assert history.goals_summary(conn, texts) == "Sharing and reminders"


def test_a_priorities_label_too_long_for_the_line_is_not_kept(world: World) -> None:
    conn = connect(world)
    texts = ["finish list sharing and reminder settings"]
    history.goals_summary(conn, texts)
    asked: list[str] = []

    def ask(kind: str, text: str) -> str:
        asked.append(text)
        return "finish list sharing and reminder settings"

    while history.summarize_next(conn, ask) is not None:
        pass
    assert len(asked) == history.ATTEMPTS
    assert history.goals_summary(conn, texts) is None
    failed = conn.execute("SELECT failed FROM summaries").fetchone()["failed"]
    assert "over 26 characters" in failed


def test_a_failed_priorities_summary_is_asked_for_again_at_the_retry_interval(world: World, monkeypatch) -> None:
    now = NOW
    monkeypatch.setattr(db, "now_iso", lambda: now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    conn = connect(world)
    texts = ["finish list sharing and reminder settings"]
    history.goals_summary(conn, texts)

    def out_of_usage(kind: str, text: str) -> str:
        raise history.SummaryError("both accounts are at their limit")

    while history.summarize_next(conn, out_of_usage) is not None:
        pass
    history.goals_summary(conn, texts)
    assert history.summarize_next(conn, lambda kind, text: "Sharing and reminders") is None

    now = NOW + history.GOALS_RETRY - timedelta(seconds=1)
    assert history.goals_summary(conn, texts) is None
    assert history.summarize_next(conn, lambda kind, text: "Sharing and reminders") is None

    now = NOW + history.GOALS_RETRY
    assert history.goals_summary(conn, texts) is None
    while history.summarize_next(conn, lambda kind, text: "Sharing and reminders") is not None:
        pass
    assert history.goals_summary(conn, texts) == "Sharing and reminders"


def test_an_install_without_claude_asks_for_no_priorities_summary(world: World, monkeypatch) -> None:
    setup = config.setup()
    monkeypatch.setattr(config, "setup", lambda: dataclasses.replace(setup, providers=("codex",)))
    conn = connect(world)
    assert history.goals_summary(conn, ["finish list sharing and reminder settings"]) is None
    assert conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 0
