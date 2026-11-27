from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from cockpit import config, db, handovers, limits, sessions, store
from cockpit.codex_agent import CONTROL_PLANE, CodexError
from tests.conftest import ORIGIN, SESSION_A, SESSION_B, make_session, write_prompt
from tests.test_api import put_item, put_step
from tests.test_codex_agent import agent  # noqa: F401


def source(world):
    world.cache.sessions = [make_session(SESSION_A, pid=None, state="stopped", cwd=str(world.checkout))]
    write_prompt(SESSION_A, "Preserve my first request and continue the unfinished changes.")
    return put_item(world, sessions=[SESSION_A], stage="building")


def handover(world, item):
    return world.client.post(f"/api/sessions/{SESSION_A}/handover", json={"item_id": item["id"]}, headers=world.user())


def test_full_context_and_all_open_ownership_move_before_codex_first_prompt(world, agent):
    item = source(world)
    other = put_item(world, key="note:other", title="Other work", sessions=[SESSION_A], stage="building",
                     waiting_on=[{"kind": "agent", "who": SESSION_A}])
    closed = put_item(world, key="note:closed", title="Closed work", sessions=[SESSION_A])
    step = put_step(world, item_key=other["key"], session_id=SESSION_A, answers=[{"text": "Continue", "closes": False}])["step"]
    conn = db.connect(world.db_path)
    with db.transaction(conn):
        conn.execute("UPDATE items SET stage = 'done' WHERE id = ?", (closed["id"],))
        sent = store.create_message(conn, "user", "Keep the earlier decision", {"item_id": other["id"], "step_id": step["id"]}, world.checkout)
        conn.execute("UPDATE messages SET state = 'sent' WHERE id = ?", (sent["id"],))
        conn.execute("UPDATE steps SET handoff_message_id = ? WHERE id = ?", (sent["id"], step["id"]))
    conn.close()
    for index in range(85):
        write_prompt(SESSION_A, f"Complete history message {index}")
    path = next(sessions.PROJECTS_DIR.glob(f"*/{SESSION_A}.jsonl"))
    with path.open("a") as handle:
        handle.write(json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "content": "tool-result-" + "z" * 20000}]}}) + "\n")
    request = agent.rpc.request

    def inspect_before_prompt(method, params):
        if method == "turn/start":
            conn = db.connect(world.db_path)
            assert store.get_item(conn, item["id"])["sessions"] == [SESSION_B, SESSION_A]
            transferred = store.get_item(conn, other["id"])
            assert transferred["sessions"] == [SESSION_B, SESSION_A]
            assert transferred["waiting_on"] == [{"kind": "agent", "who": SESSION_B}]
            assert store.get_step(conn, step["id"])["session_id"] == SESSION_B
            assert store.get_message(conn, sent["id"])["session_id"] == SESSION_B
            assert store.get_item(conn, closed["id"])["sessions"] == [SESSION_A]
            # Later replies can be recorded now, but wait for this original prompt to be submitted.
            follow = store.create_message(conn, "user", "A new reply", {"session_id": SESSION_A}, world.checkout)
            world.deliverer.send(follow["id"])
            assert store.get_message(conn, follow["id"])["state"] == "queued"
            assert store.get_message(conn, follow["id"])["waits_since"]
            conn.close()
        return request(method, params)

    agent.rpc.request = inspect_before_prompt
    response = handover(world, item)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["session_id"] == SESSION_B and result["state"] == "sent"
    assert result["item_ids"] == [item["id"], other["id"]]
    artifact = world.db_path.parent / "handovers" / SESSION_A / "transcript.md"
    text = artifact.read_text()
    assert "Preserve my first request" in text and "Complete history message 84" in text
    assert "tool-result-" + "z" * 20000 in text
    assert artifact.stat().st_mode & 0o777 == 0o600
    assert "Keep the earlier decision" in artifact.with_name("tasks.json").read_text()
    assert world.runner.calls == []
    assert handover(world, item).json()["message_id"] == result["message_id"]
    assert sum(method == "thread/start" for method, _ in agent.rpc.calls) == 1
    conn = db.connect(world.db_path)
    assert not limits.unreached(conn, world.cache.sessions[0], datetime.now(UTC) - timedelta(minutes=10))
    conn.close()


def test_first_prompt_failure_retries_existing_target_before_later_replies(world, agent):
    item = source(world)
    conn = db.connect(world.db_path)
    old = store.create_message(conn, "cockpit", "An older pending prompt", {"session_id": SESSION_A}, world.checkout)
    conn.execute("UPDATE messages SET as_prompt = 1 WHERE id = ?", (old["id"],))
    conn.close()
    request = agent.rpc.request

    def fail_turn(method, params):
        if method == "turn/start":
            raise CodexError("first prompt failed")
        return request(method, params)

    agent.rpc.request = fail_turn
    response = handover(world, item)
    assert response.status_code == 502 and response.json()["error"]
    first = response.json()
    assert first["state"] == "failed" and first["session_id"] == SESSION_B
    follow = world.client.post("/api/messages", json={"text": "Remember this", "target": {"session_id": SESSION_A}}, headers=world.user()).json()
    assert follow["session_id"] == SESSION_B
    assert world.client.get(f"/api/messages/{follow['id']}").json()["state"] == "queued"
    agent.rpc.request = request
    second = handover(world, item).json()
    assert second["state"] == "sent" and second["message_id"] == first["message_id"]
    assert sum(method == "thread/start" for method, _ in agent.rpc.calls) == 1


def test_thread_creation_failure_keeps_source_routes_and_reuses_message(world, agent):
    item = source(world)
    request = agent.rpc.request

    def fail_start(method, params):
        raise CodexError("worker unavailable")

    agent.rpc.request = fail_start
    first = handover(world, item).json()
    assert first["state"] == "failed" and first["session_id"] is None
    refused = world.client.post(f"/api/messages/{first['message_id']}/retry", headers=world.user())
    assert refused.status_code == 409 and "Hand to Codex" in refused.json()["detail"]
    conn = db.connect(world.db_path)
    assert store.get_item(conn, item["id"])["sessions"] == [SESSION_A]
    conn.close()
    write_prompt(SESSION_A, "A new decision before retry")
    agent.rpc.request = request
    second = handover(world, item).json()
    assert second["state"] == "sent" and second["message_id"] == first["message_id"]
    assert "A new decision before retry" in (world.db_path.parent / "handovers" / SESSION_A / "transcript.md").read_text()


def test_concurrent_presses_start_one_thread(world, agent):
    item = source(world)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: world.deliverer.handover(SESSION_A, item["id"]), range(2)))
    assert results[0]["message_id"] == results[1]["message_id"]
    assert sum(method == "thread/start" for method, _ in agent.rpc.calls) == 1


@pytest.mark.parametrize("state,status", [("working", "busy"), ("done", "busy")])
def test_active_turn_or_background_work_must_finish(world, agent, state, status):
    item = source(world)
    world.cache.sessions = [replace(world.cache.sessions[0], pid=12, state=state, status=status)]
    response = handover(world, item)
    assert response.status_code == 409 and "finish" in response.json()["detail"]
    assert not agent.rpc.calls and not world.runner.calls


def test_idle_source_is_stopped_without_a_summary_call(world, agent):
    item = source(world)
    idle = replace(world.cache.sessions[0], pid=12, state="blocked", status="idle")
    world.cache.sessions = [idle]

    def stop(account, args, cwd):
        assert args[:2] == ["claude", "stop"]
        world.cache.sessions = [replace(idle, pid=None, status="", state="stopped")]
        return world.runner(account, args, cwd)

    world.deliverer.run = stop
    assert handover(world, item).json()["state"] == "sent"
    assert len(world.runner.calls) == 1


def test_closed_task_during_thread_creation_never_gets_prompt_or_routing(world, agent):
    item = source(world)
    request = agent.rpc.request

    def close_during_start(method, params):
        if method == "thread/start":
            conn = db.connect(world.db_path)
            conn.execute("UPDATE items SET stage = 'done' WHERE id = ?", (item["id"],))
            conn.close()
        return request(method, params)

    agent.rpc.request = close_during_start
    result = handover(world, item).json()
    assert result["state"] == "failed" and result["session_id"] is None
    assert not any(method == "turn/start" for method, _ in agent.rpc.calls)
    conn = db.connect(world.db_path)
    assert store.get_item(conn, item["id"])["sessions"] == [SESSION_A]
    assert not conn.execute("SELECT 1 FROM codex_threads").fetchone()
    conn.close()
    assert handover(world, item).status_code == 409


def test_partial_close_preserves_obsolete_message_and_other_owner(world, agent):
    item = source(world)
    closed = put_item(world, key="note:closing", title="Closing work", sessions=[SESSION_A], stage="building")
    conn = db.connect(world.db_path)
    old = store.create_message(conn, "agent:helper", "An obsolete task message", {"item_id": closed["id"]}, world.checkout)
    conn.close()

    request = agent.rpc.request

    def close_one(method, params):
        if method == "thread/start":
            conn = db.connect(world.db_path)
            conn.execute("UPDATE items SET archived = 1 WHERE id = ?", (closed["id"],))
            conn.close()
        return request(method, params)

    agent.rpc.request = close_one
    result = handover(world, item).json()
    assert result["item_ids"] == [item["id"]]
    conn = db.connect(world.db_path)
    assert store.get_item(conn, closed["id"])["sessions"] == [SESSION_A]
    assert store.get_message(conn, old["id"])["session_id"] == SESSION_A
    world.deliverer.send(old["id"])
    obsolete = store.get_message(conn, old["id"])
    assert obsolete["state"] == "failed" and obsolete["session_id"] == SESSION_A
    assert "closed or reassigned" in obsolete["detail"]
    assert world.runner.calls == []
    conn.close()


def test_context_task_closed_can_retry_from_another_open_task(world, agent):
    item = source(world)
    other = put_item(world, key="note:remaining", title="Remaining work", sessions=[SESSION_A], stage="building")
    request = agent.rpc.request

    def close_context(method, params):
        if method == "thread/start":
            conn = db.connect(world.db_path)
            conn.execute("UPDATE items SET archived = 1 WHERE id = ?", (item["id"],))
            conn.close()
        return request(method, params)

    agent.rpc.request = close_context
    response = handover(world, item)
    assert response.status_code == 502
    first = response.json()
    assert first["session_id"] is None
    conn = db.connect(world.db_path)
    assert store.get_item(conn, item["id"])["sessions"] == [SESSION_A]
    assert store.get_item(conn, other["id"])["sessions"] == [SESSION_A]
    conn.close()
    agent.rpc.request = request
    second = handover(world, other).json()
    assert second["state"] == "sent" and second["message_id"] == first["message_id"]
    assert second["item_ids"] == [other["id"]]


def test_user_only_validation_and_enabled_provider(world, agent):
    item = source(world)
    other = put_item(world, key="note:unrelated", title="Unrelated work", sessions=[SESSION_B])
    assert handover(world, other).status_code == 409
    client = TestClient(world.client.app, base_url=ORIGIN)
    assert client.post(f"/api/sessions/{SESSION_A}/handover", json={"item_id": item["id"]}, headers=world.agent()).status_code == 403
    world.deliverer.enabled_providers = ("claude",)
    assert handover(world, item).status_code == 409
    world.deliverer.enabled_providers = ("claude", "codex")
    original = world.cache.sessions[0]
    world.cache.sessions = [replace(original, provider="codex")]
    assert handover(world, item).status_code == 409
    world.cache.sessions = [original]
    paths = list(sessions.PROJECTS_DIR.glob(f"*/{SESSION_A}.jsonl"))
    paths[0].unlink()
    assert handover(world, item).status_code == 409
    assert not agent.rpc.calls


def test_state_uses_codex_registry_model_and_unknown_claude_model(world, agent):
    item = source(world)
    assert handover(world, item).json()["state"] == "sent"
    world.cache.sessions.append(agent.lookup(SESSION_B))
    state = world.client.get("/api/state").json()
    rows = {row["session_id"]: row for row in state["sessions"]}
    assert rows[SESSION_B]["model"] == config.SESSION_OPTIONS["codex"]["models"][0][0]
    assert rows[SESSION_A]["model"] is None
    assert rows[SESSION_A]["handover"]["session_id"] == SESSION_B


def _said(role, content):
    return {"type": role, "message": {"role": role, "content": content}}


def test_claude_excerpts_keep_user_prompts_and_each_turns_last_answer(tmp_path):
    entries = [
        _said("user", "First request"),
        _said("assistant", [{"type": "text", "text": "Looking at it"}]),
        _said("assistant", [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]),
        _said("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "files"}]),
        _said("assistant", [{"type": "text", "text": "Answer one"}]),
        _said("user", [{"type": "text", "text": "Second request"}]),
        _said("assistant", [{"type": "thinking", "thinking": "hidden"}, {"type": "text", "text": "Answer two"}]),
        {**_said("user", "Summary of the earlier conversation"), "isCompactSummary": True},
        {**_said("user", "Claude Code's own note"), "isMeta": True},
        _said("assistant", [{"type": "text", "text": "Answer three"}]),
        _said("user", "Third request"),
        _said("assistant", [{"type": "text", "text": "y" * 3500}]),
        {**_said("assistant", [{"type": "text", "text": "API Error: Request was aborted."}]),
         "message": {"role": "assistant", "model": "<synthetic>",
                     "content": [{"type": "text", "text": "API Error: Request was aborted."}]}},
    ]
    path = sessions.PROJECTS_DIR / "-home-x" / f"{SESSION_A}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    session = make_session(SESSION_A, pid=None, state="stopped")
    _, instructions, answers = handovers._transcript(session, tmp_path / "out")
    assert instructions == ["First request", "Second request", "Third request"]
    assert answers == ["Answer one", "Answer three", "y" * 3000 + " [excerpt; full message in transcript]"]


def test_claude_excerpts_leave_out_rows_claude_code_wrote(tmp_path):
    # Rows shaped as Claude Code 2.1.331 writes a local command run before Cockpit's first prompt; the command and its
    # output are invented.
    caveat = ("<local-command-caveat>The command below was run directly in Claude Code, not sent to you as a request, "
              "and its output goes straight to the user. It's recorded here as context for later messages."
              "</local-command-caveat>")
    command = ("<command-name>/color</command-name>\n            <command-message>color</command-message>\n"
               "            <command-args>teal</command-args>")
    output = "<local-command-stdout>Session color set to teal</local-command-stdout>"
    notification = "<task-notification>\n<task-id>b1example</task-id>\n<status>completed</status>\n</task-notification>"
    entries = [
        {**_said("user", caveat), "isMeta": True, "rowSource": "harness"},
        {**_said("user", command), "rowSource": "local"},
        {**_said("user", output), "rowSource": "local"},
        {**_said("user", "Message from the sweeper via Cockpit: fix item 12"), "origin": {"kind": "human"},
         "promptSource": "typed"},
        _said("assistant", [{"type": "text", "text": "Started"}]),
        {**_said("user", command), "rowSource": "local"},
        {**_said("user", output), "rowSource": "local"},
        _said("assistant", [{"type": "text", "text": "Color noted"}]),
        {**_said("user", notification), "rowSource": "recorded", "promptSource": "system",
         "origin": {"kind": "task-notification"}},
        {**_said("user", "<task-notification> is what I pasted; why did it stop?"), "origin": {"kind": "human"},
         "promptSource": "typed"},
        _said("assistant", [{"type": "text", "text": "It finished"}]),
        # A `claude -p --resume` turn, as the emergency page runs one.
        {**_said("user", "Also check the backups"), "rowSource": "local", "promptSource": "sdk"},
        _said("assistant", [{"type": "text", "text": "Backups pass"}]),
    ]
    path = sessions.PROJECTS_DIR / "-home-x" / f"{SESSION_A}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    session = make_session(SESSION_A, pid=None, state="stopped")
    _, instructions, answers = handovers._transcript(session, tmp_path / "out")
    assert instructions == ["Message from the sweeper via Cockpit: fix item 12",
                            "<task-notification> is what I pasted; why did it stop?", "Also check the backups"]
    assert answers == ["Color noted", "It finished", "Backups pass"]



def test_claude_excerpts_leave_out_commands_with_neither_source_field(tmp_path):
    # Rows shaped as Claude Code 2.1.326 writes a local command run before the first prompt: none of them carries
    # rowSource or promptSource. The command and its output are invented.
    caveat = ("<local-command-caveat>The command below was run directly in Claude Code, not sent to you as a request, "
              "and its output goes straight to the user.</local-command-caveat>")
    command = ("<command-name>/color</command-name>\n            <command-message>color</command-message>\n"
               "            <command-args>teal</command-args>")
    output = "<local-command-stdout>Session color set to teal</local-command-stdout>"
    entries = [
        {**_said("user", caveat), "isMeta": True},
        _said("user", command),
        _said("user", output),
        {**_said("user", "Fix the login page"), "origin": {"kind": "human"}, "promptSource": "typed"},
        _said("assistant", [{"type": "text", "text": "Fixed"}]),
        _said("user", "<command-name>/theme</command-name>"),
        _said("user", [{"type": "text", "text": "<local-command-stdout>Theme set to dark</local-command-stdout>"}]),
        _said("assistant", [{"type": "text", "text": "Theme noted"}]),
        # A `claude -p --resume` turn, as the emergency page runs one.
        {**_said("user", "Also check the backups"), "rowSource": "local", "promptSource": "sdk"},
        _said("assistant", [{"type": "text", "text": "Backups pass"}]),
        # A prompt someone gave that happens to start with a command tag.
        {**_said("user", "<command-name> tags break the quote"), "rowSource": "local", "promptSource": "sdk"},
        _said("assistant", [{"type": "text", "text": "Quote checked"}]),
        # The same words typed while a turn ran, which Claude Code records as a queued prompt.
        {"type": "attachment", "timestamp": "2026-11-26T01:00:00Z",
         "attachment": {"type": "queued_command", "commandMode": "prompt", "prompt": "<command-name> again"}},
        _said("assistant", [{"type": "text", "text": "Again checked"}]),
    ]
    path = sessions.PROJECTS_DIR / "-home-x" / f"{SESSION_A}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    session = make_session(SESSION_A, pid=None, state="stopped")
    _, instructions, answers = handovers._transcript(session, tmp_path / "out")
    assert instructions == ["Fix the login page", "Also check the backups", "<command-name> tags break the quote",
                            "<command-name> again"]
    assert answers == ["Backups pass", "Quote checked", "Again checked"]

def test_codex_excerpts_leave_out_the_context_codex_adds(monkeypatch, tmp_path):
    home = tmp_path / "codex-home"
    monkeypatch.setattr(config, "CODEX_HOMES", (home,))

    def message(role, kind, *texts, phase=None):
        return {"type": "response_item", "payload": {"type": "message", "role": role, "phase": phase,
                                                     "content": [{"type": kind, "text": text} for text in texts]}}

    records = [
        {"type": "session_meta", "payload": {"id": SESSION_A, "cwd": "/"}},
        message("developer", "input_text", "You are Codex"),
        message("user", "input_text", "# AGENTS.md instructions for /\n\nrules", "<environment_context>\n</environment_context>"),
        message("user", "input_text", "Fix the bug" + CONTROL_PLANE + " Use the cockpit CLI."),
        {"type": "response_item", "payload": {"type": "reasoning", "summary": []}},
        message("assistant", "output_text", "Looking at it", phase="commentary"),
        message("assistant", "output_text", "Fixed it", phase="final_answer"),
        message("user", "input_text", '<codex_internal_context source="goal">Continue</codex_internal_context>'),
        message("assistant", "output_text", "Also done", phase="final_answer"),
        message("user", "input_text", "Now patch Y"),
        message("assistant", "output_text", "Checking the tests now, then I'll patch Y.", phase="commentary"),
        {"type": "event_msg", "payload": {"type": "turn_aborted"}},
    ]
    path = home / "sessions" / "2026" / "11" / "25" / f"rollout-test-{SESSION_A}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    session = replace(make_session(SESSION_A, pid=None, state="stopped"), provider="codex")
    _, instructions, answers = handovers._transcript(session, tmp_path / "out")
    assert instructions == ["Fix the bug", "Now patch Y"]
    assert answers == ["Fixed it", "Also done",
                       "[Progress note, written while working; not a final answer] Checking the tests now, then I'll patch Y."]
