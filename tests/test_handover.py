from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from cockpit import config, db, limits, sessions, store
from cockpit.codex_agent import CodexError
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
