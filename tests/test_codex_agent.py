from __future__ import annotations

import dataclasses
import sys
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from cockpit import config, db, delivery
from cockpit.app import create_app
from cockpit.codex_agent import AppServer, CodexAgent, CodexError, ProviderSessions
from tests.conftest import SESSION_B


class FakeRpc:
    def __init__(self, agent):
        self.agent = agent
        self.calls = []
        self.writes = []
        self.process = SimpleNamespace(pid=999)
        self.updated = False
        self.closed = 0

    def outdated(self):
        return self.updated

    def close(self):
        self.closed += 1
        self.updated = False
        self.agent.event({"method": "cockpit/disconnected", "params": {}})

    def request(self, method, params):
        self.calls.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": SESSION_B}}
        if method == "turn/start":
            self.agent.event({"method": "turn/started", "params": {"threadId": params["threadId"], "turn": {"id": "turn1"}}})
            return {"turn": {"id": "turn1"}}
        if method == "turn/interrupt":
            self.agent.event({"method": "turn/completed", "params": {"threadId": params["threadId"],
                                                                       "turn": {"id": "turn1", "status": "interrupted"}}})
        return {}

    def write(self, message):
        self.writes.append(message)


@pytest.fixture
def agent(world, monkeypatch):
    setup = config.setup()
    monkeypatch.setattr(config, "setup", lambda: dataclasses.replace(setup, providers=("claude", "codex")))
    agent = CodexAgent(world.db_path)
    agent.rpc = FakeRpc(agent)
    world.deliverer.codex = agent
    world.deliverer.default_provider = "codex"
    world.deliverer.enabled_providers = ("claude", "codex")
    return agent


def finish(agent, text="All done", status="completed"):
    agent.event({"method": "item/completed", "params": {"threadId": SESSION_B,
                 "item": {"id": "reply", "type": "agentMessage", "text": text}}})
    agent.event({"method": "turn/completed", "params": {"threadId": SESSION_B,
                 "turn": {"id": "turn1", "status": status}}})


def test_codex_messages_use_the_shared_queue_and_keep_thread_identity(world, agent):
    response = world.client.post("/api/messages", json={"text": "Do some work", "target": {
        "new_cwd": str(world.checkout), "provider": "codex"}}, headers=world.user())
    assert response.status_code == 200
    message_id = response.json()["id"]
    sent = world.client.get(f"/api/messages/{message_id}").json()
    assert (sent["state"], sent["session_id"], sent["via"]) == ("sent", SESSION_B, "new")
    assert world.runner.calls == []
    assert agent.lookup(SESSION_B).working_now
    queued = world.client.post("/api/messages", json={"text": "Follow up", "target": {"session_id": SESSION_B}},
                               headers=world.user()).json()
    assert world.client.get(f"/api/messages/{queued['id']}").json()["waits_since"]
    finish(agent)
    conn = db.connect(world.db_path)
    try:
        assert delivery.waiting_due(conn, agent.sessions(), datetime.now(UTC)) == [queued["id"]]
        assert conn.execute("SELECT reply FROM session_turns WHERE session_id = ?", (SESSION_B,)).fetchone()[0] == "All done"
    finally:
        conn.close()
    world.deliverer.send(queued["id"])
    assert world.client.get(f"/api/messages/{queued['id']}").json()["state"] == "sent"
    assert sum(method == "thread/start" for method, _ in agent.rpc.calls) == 1


def test_a_new_codex_session_runs_on_the_model_and_effort_the_user_picked(world, agent):
    # SPEC SESSION-22: the picks reach the thread, and a resume after a restart keeps them.
    response = world.client.post("/api/messages", json={"text": "hello", "target": {
        "new_cwd": str(world.checkout), "provider": "codex", "model": "gpt-6-luna", "effort": "low"}}, headers=world.user())
    assert response.status_code == 200
    [started] = [params for method, params in agent.rpc.calls if method == "thread/start"]
    assert started["model"] == "gpt-6-luna" and started["config"] == {"model_reasoning_effort": "low"}
    finish(agent)
    restarted = CodexAgent(world.db_path)
    restarted.rpc = FakeRpc(restarted)
    restarted.send(SESSION_B, "continue")
    method, resumed = restarted.rpc.calls[0]
    assert method == "thread/resume" and resumed["model"] == "gpt-6-luna"
    assert resumed["config"] == {"model_reasoning_effort": "low"}


def test_saved_threads_resume_after_restart_and_send_now_interrupts(world, agent):
    agent.start(world.checkout, "test", "hello", "gpt-6.1-sol", "high")
    finish(agent)
    restarted = CodexAgent(world.db_path)
    restarted.rpc = FakeRpc(restarted)
    assert restarted.owns(SESSION_B)
    assert not restarted.lookup(SESSION_B).alive
    restarted.send(SESSION_B, "continue")
    assert restarted.rpc.calls[0][0] == "thread/resume"
    restarted.send(SESSION_B, "new direction", restart=True)
    assert [m for m, _ in restarted.rpc.calls][-2:] == ["turn/interrupt", "turn/start"]


def test_an_update_restarts_the_app_server_once_no_turn_needs_it(world, agent):
    # SPEC SESSION-15: new threads and messages run on the Codex version installed, not the one Cockpit started.
    agent.start(world.checkout, "test", "hello", "gpt-6.1-sol", "high")
    agent.rpc.updated = True
    with pytest.raises(CodexError, match="still working"):
        agent.send(SESSION_B, "too soon")
    assert agent.rpc.closed == 0
    finish(agent)
    agent.send(SESSION_B, "continue")
    assert agent.rpc.closed == 1
    assert [m for m, _ in agent.rpc.calls][-2:] == ["thread/resume", "turn/start"]


def test_a_turn_cockpit_stopped_during_shows_failed_after_restart(world, agent):
    # the turn's state stays 'working' when Cockpit is killed before the disconnect event.
    agent.start(world.checkout, "test", "hello", "gpt-6.1-sol", "high")
    restarted = CodexAgent(world.db_path)
    restarted.rpc = FakeRpc(restarted)
    create_app(world.settings, world.deliverer, world.cache, world.spend_index, world.codex_index)
    session = restarted.lookup(SESSION_B)
    assert session.state == "failed" and not session.alive
    assert "stopped during this turn" in session.detail


def test_approvals_require_the_user_and_are_bound_to_the_live_rpc_request(world, agent):
    agent.start(world.checkout, "test", "hello", "gpt-6.1-sol", "high")
    agent.event({"id": "approval1", "method": "item/commandExecution/requestApproval", "params": {
        "threadId": SESSION_B, "turnId": "turn1", "itemId": "cmd1", "command": "git push"}})
    request = agent.requests()[0]
    url = f"/api/codex/requests/{request['id']}/answer"
    assert world.client.post(url, json={"decision": "accept"}, headers=world.agent()).status_code == 403
    assert world.client.post(url, json={"decision": "acceptForSession"}, headers=world.user()).status_code == 409
    assert world.client.post(url, json={"decision": "accept"}, headers=world.user()).status_code == 200
    assert agent.rpc.writes[-1] == {"id": "approval1", "result": {"decision": "accept"}}
    assert world.client.post(url, json={"decision": "accept"}, headers=world.user()).status_code == 409


def test_questions_and_permission_grants_do_not_expand_the_request(world, agent):
    agent.start(world.checkout, "test", "hello", "gpt-6.1-sol", "high")
    agent.event({"id": 12, "method": "item/tool/requestUserInput", "params": {
        "threadId": SESSION_B, "questions": [{"id": "choice", "question": "Which one?"}]}})
    key = agent.requests()[0]["id"]
    with pytest.raises(CodexError, match="every Codex question"):
        agent.answer(key, {"answers": {}})
    agent.answer(key, {"answers": {"choice": {"answers": ["A"]}}})
    assert agent.rpc.writes[-1]["result"] == {"answers": {"choice": {"answers": ["A"]}}}
    permissions = {"network": {"enabled": True}}
    agent.event({"id": 13, "method": "item/permissions/requestApproval", "params": {
        "threadId": SESSION_B, "permissions": permissions}})
    agent.answer(agent.requests()[0]["id"], {"decision": "accept", "permissions": {"filesystem": "everything"}})
    assert agent.rpc.writes[-1]["result"] == {"permissions": permissions, "scope": "turn"}


def test_disconnect_and_turn_end_clear_stale_approvals(world, agent):
    agent.start(world.checkout, "test", "hello", "gpt-6.1-sol", "high")
    agent.event({"id": 1, "method": "item/fileChange/requestApproval", "params": {"threadId": SESSION_B}})
    agent.event({"method": "serverRequest/resolved", "params": {"threadId": SESSION_B, "requestId": 1}})
    assert agent.requests() == []
    agent.event({"method": "cockpit/disconnected", "params": {}})
    session = agent.lookup(SESSION_B)
    assert session.state == "failed" and not session.alive
    assert "disconnected" in session.detail
    with pytest.raises(CodexError, match="not owned"):
        agent.send("another-apps-thread", "hello")


def test_provider_cache_needs_no_claude_when_only_codex_is_enabled(world, agent):
    agent.start(world.checkout, "test", "hello", "gpt-6.1-sol", "high")
    cache = ProviderSessions(None, agent)
    listing = cache.snapshot(wait=False)
    assert [s.provider for s in listing.sessions] == ["codex"]
    assert not listing.failed and not listing.warnings


def test_turn_response_before_notification_is_already_busy(world, agent):
    agent.start(world.checkout, "test", "hello", "gpt-6.1-sol", "high")
    finish(agent)
    real_request = agent.rpc.request

    def response_first(method, params):
        if method == "turn/start":
            return {"turn": {"id": "turn2"}}
        return real_request(method, params)

    agent.rpc.request = response_first
    agent.send(SESSION_B, "continue")
    assert agent.lookup(SESSION_B).working_now
    with pytest.raises(CodexError, match="still working"):
        agent.send(SESSION_B, "must wait")


def test_fast_turn_completion_before_response_stays_done(world, agent):
    agent.start(world.checkout, "test", "hello", "gpt-6.1-sol", "high")
    finish(agent)
    real_request = agent.rpc.request

    def finish_first(method, params):
        result = real_request(method, params)
        if method == "turn/start":
            finish(agent)
        return result

    agent.rpc.request = finish_first
    agent.send(SESSION_B, "continue")
    assert not agent.lookup(SESSION_B).working_now


def test_failed_first_turn_retry_continues_the_created_thread(world, agent):
    real_request = agent.rpc.request

    def reject_turn(method, params):
        if method == "turn/start":
            raise CodexError("not signed in")
        return real_request(method, params)

    agent.rpc.request = reject_turn
    item = world.client.put("/api/items", json={"key": "note:codex-retry", "title": "Retry me"}, headers=world.agent())
    response = world.client.post("/api/messages", json={"text": "hello", "target": {
        "item_id": item.json()["id"], "new_cwd": str(world.checkout), "provider": "codex"}}, headers=world.user())
    message_id = response.json()["id"]
    failed = world.client.get(f"/api/messages/{message_id}").json()
    assert failed["state"] == "failed" and failed["session_id"] == SESSION_B
    agent.rpc.request = real_request
    world.client.post(f"/api/messages/{message_id}/retry", headers=world.user())
    assert world.client.get(f"/api/messages/{message_id}").json()["state"] == "sent"
    assert sum(m == "thread/start" for m, _ in agent.rpc.calls) == 1
    # The retry is still the thread's first prompt, so it carries the new session's context.
    [retried] = [params["input"][0]["text"] for m, params in agent.rpc.calls if m == "turn/start"]
    assert "hello" in retried and "Cockpit context: you are the session for item" in retried and "Retry me" in retried


def test_a_failed_state_write_before_the_turn_leaves_the_thread_free(world, agent):
    agent.start(world.checkout, "test", "hello", "gpt-6.1-sol", "high")
    finish(agent)
    real_save = agent._save

    def locked(session_id, **changes):
        raise db.sqlite3.OperationalError("database is locked")

    agent._save = locked
    with pytest.raises(db.sqlite3.OperationalError):
        agent.send(SESSION_B, "continue")
    agent._save = real_save
    agent.send(SESSION_B, "continue")
    assert [m for m, _ in agent.rpc.calls].count("turn/start") == 2


def test_two_turns_that_end_within_one_second_are_both_kept(world, agent):
    agent.start(world.checkout, "test", "hello", "gpt-6.1-sol", "high")
    finish(agent, "First")
    agent.send(SESSION_B, "continue")
    finish(agent, "Second")
    conn = db.connect(world.db_path)
    try:
        replies = [row[0] for row in conn.execute("SELECT reply FROM session_turns WHERE session_id = ? ORDER BY id",
                                                  (SESSION_B,)).fetchall()]
    finally:
        conn.close()
    assert replies == ["First", "Second"]


def test_disabling_codex_does_not_reroute_its_thread_to_claude(world, agent):
    agent.start(world.checkout, "test", "hello", "gpt-6.1-sol", "high")
    finish(agent)
    world.deliverer.codex = None
    response = world.client.post("/api/messages", json={"text": "continue", "target": {"session_id": SESSION_B}},
                                 headers=world.user())
    message = world.client.get(f"/api/messages/{response.json()['id']}").json()
    assert message["state"] == "failed" and "not enabled" in message["detail"]
    assert world.runner.calls == []


def test_the_session_view_shows_a_codex_threads_conversation(world, agent, monkeypatch):
    # the sidebar's session view read only Claude transcripts, so a Codex session showed no conversation.
    agent.start(world.checkout, "test", "hello", "gpt-6.1-sol", "high")
    finish(agent)
    at = 1768471200000  # 2026-01-15T10:00:00Z
    prompt = ("Message from Sam via Cockpit:\nRun the tests\n---\nCockpit context: step 10"
              f"\n\nCockpit is your control plane. Your session UUID is {SESSION_B}.")
    newest_first = [
        {"startedAtMs": at + 4000, "item": {"type": "agentMessage", "text": "All **green**"}},
        {"startedAtMs": at + 3500, "item": {"type": "mcpToolCall", "id": "c3", "server": "linear", "tool": "get_issue",
                                            "arguments": {"id": "ENG-123"}, "status": "failed", "error": None,
                                            "result": {"content": [{"type": "text", "text": "not found"}]}}},
        {"startedAtMs": at + 3000, "item": {"type": "reasoning", "summary": [], "content": []}},
        {"startedAtMs": at + 2000, "item": {"type": "commandExecution", "id": "c2", "command": "pytest -q",
                                            "aggregatedOutput": "1 failed", "exitCode": 1, "status": "completed"}},
        {"startedAtMs": at + 1500, "item": {"type": "fileChange", "id": "c1", "status": "declined",
                                            "changes": [{"path": "src/b.py", "diff": "-y = 2", "kind": {"type": "delete"}}]}},
        {"startedAtMs": at + 1000, "item": {"type": "fileChange", "id": "c0", "status": "completed",
                                            "changes": [{"path": "src/a.py", "diff": "+x = 1", "kind": {"type": "add"}}]}},
        {"startedAtMs": at, "item": {"type": "userMessage", "content": [{"type": "text", "text": prompt}]}},
    ]
    pages = {None: (newest_first[:3], "next"), "next": (newest_first[3:], None)}
    request = agent.rpc.request

    def listing(method, params):
        if method != "thread/items/list":
            return request(method, params)
        assert (params["threadId"], params["sortDirection"]) == (SESSION_B, "desc")
        data, cursor = pages[params["cursor"]]
        return {"data": data, "nextCursor": cursor}

    monkeypatch.setattr(agent.rpc, "request", listing)
    log = world.client.get(f"/api/sessions/{SESSION_B}/log", headers=world.user()).json()
    assert [part["kind"] for part in log["parts"]] == ["instruction", "call", "call", "call", "call", "says"]
    first, edit, declined, command, tool, says = log["parts"]
    assert (first["author"], first["text"], first["at"]) == ("user", "Run the tests", "2026-01-15T10:00:00Z")
    assert (edit["tool_name"], edit["title"], edit["result"], edit["error"]) == ("Edit", "src/a.py", "+x = 1", False)
    # A change the user refused shows as refused, not as made; each call keeps Codex's id for the view's open state.
    assert (declined["result"], declined["error"]) == ("Declined; not run.\n\n-y = 2", True)
    assert (command["tool_name"], command["text"], command["result"], command["error"]) == ("Bash", "pytest -q", "1 failed", True)
    # A tool that reported its own failure has status "failed" and no error: it shows as failed.
    assert (tool["tool_name"], tool["text"], tool["error"]) == ("linear.get_issue", '{"id": "ENG-123"}', True)
    assert [edit["id"], declined["id"], command["id"], tool["id"]] == ["c0", "c1", "c2", "c3"]
    assert "<strong>green</strong>" in says["html"]


def test_stdio_transport_handshake_responses_and_server_requests(tmp_path):
    executable = tmp_path / "fake-codex"
    executable.write_text(f"#!{sys.executable}\n" + '''import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        print(json.dumps({"id": msg["id"], "result": {"userAgent": "test"}}), flush=True)
    elif msg.get("method") == "ping":
        print(json.dumps({"id": 99, "method": "approve", "params": {}}), flush=True)
        print(json.dumps({"id": msg["id"], "result": {"ok": True}}), flush=True)
''')
    executable.chmod(0o700)
    events = []
    rpc = AppServer(str(executable), events.append)
    try:
        assert rpc.request("ping", {}) == {"ok": True}
        assert events[0]["id"] == 99 and events[0]["method"] == "approve"
        assert rpc.version is None and not rpc.outdated()
    finally:
        rpc.close()


def test_an_app_server_is_outdated_once_npm_installs_another_codex(tmp_path):
    package = tmp_path / "node_modules" / "@openai" / "codex"
    (package / "bin").mkdir(parents=True)
    (package / "package.json").write_text('{"version": "0.165.0"}')
    script = package / "bin" / "codex.js"
    script.write_text(f"#!{sys.executable}\n" + '''import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if "id" in msg:
        print(json.dumps({"id": msg["id"], "result": {}}), flush=True)
''')
    script.chmod(0o700)
    (tmp_path / "codex").symlink_to(script)
    rpc = AppServer(str(tmp_path / "codex"), lambda event: None)
    try:
        assert not rpc.outdated()
        rpc.request("ping", {})
        assert rpc.version == "0.165.0" and not rpc.outdated()
        (package / "package.json").unlink()  # npm moves the old package aside before unpacking the new one
        assert not rpc.outdated()
        (package / "package.json").write_text('{"version": "0.166.0"}')
        assert rpc.outdated()
    finally:
        rpc.close()
    assert not rpc.outdated()


def test_a_finished_tool_call_s_output_is_not_held_for_the_rest_of_the_turn(agent):
    agent.event({"method": "turn/started", "params": {"threadId": SESSION_B, "turn": {"id": "turn1"}}})
    command = {"id": "cmd1", "type": "commandExecution", "command": "make test"}
    agent.event({"method": "item/started", "params": {"threadId": SESSION_B, "item": command}})
    assert agent._items[SESSION_B]["cmd1"] == command
    agent.event({"method": "item/completed", "params": {"threadId": SESSION_B,
                 "item": command | {"aggregatedOutput": "x" * 100_000}}})
    assert "cmd1" not in agent._items[SESSION_B]
    agent.event({"method": "item/completed", "params": {"threadId": SESSION_B,
                 "item": {"id": "reply", "type": "agentMessage", "text": "Tests pass"}}})
    assert list(agent._items[SESSION_B]) == ["reply"]
