from __future__ import annotations

import dataclasses
import json
import queue
import sys
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from cockpit import config, db, delivery
from cockpit.codex_agent import AppServer, CodexAgent, CodexError, ProviderSessions
from tests.conftest import SESSION_B


class FakeRpc:
    def __init__(self, agent):
        self.agent = agent
        self.calls = []
        self.writes = []
        self.process = SimpleNamespace(pid=999)
        self.connected = True
        self.updated = False
        self.closed = 0

    def outdated(self):
        return self.updated

    def start(self):
        if not self.connected:
            self.process = SimpleNamespace(pid=999)
            self.connected = True
        return self.process

    def is_connected(self, process):
        return self.connected and process is self.process

    def request_on(self, process, method, params):
        if not self.is_connected(process):
            raise CodexError("Codex app-server disconnected")
        return self.request(method, params)

    def close(self):
        self.closed += 1
        self.updated = False
        self.connected = False
        self.agent.event({"method": "cockpit/disconnected", "params": {}})

    def request(self, method, params):
        self.calls.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": SESSION_B}, "serviceTier": params.get("serviceTier")}
        if method == "thread/resume":
            return {"serviceTier": params.get("serviceTier")}
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
    assert started["model"] == "gpt-6-luna" and started["config"] == {
        "sandbox_workspace_write": {"network_access": True}, "model_reasoning_effort": "low"}
    # The cockpit CLI, gh and git fetch reach the network without an approval card.
    assert started["sandbox"] == "workspace-write" and started["approvalPolicy"] == "on-request"
    finish(agent)
    restarted = CodexAgent(world.db_path)
    restarted.rpc = FakeRpc(restarted)
    restarted.send(SESSION_B, "continue")
    method, resumed = restarted.rpc.calls[0]
    assert method == "thread/resume" and resumed["model"] == "gpt-6-luna"
    assert resumed["config"] == {"sandbox_workspace_write": {"network_access": True}, "model_reasoning_effort": "low"}
    assert resumed["sandbox"] == "workspace-write" and resumed["approvalPolicy"] == "on-request"
    assert started["approvalsReviewer"] == resumed["approvalsReviewer"] == "auto_review"


@pytest.mark.parametrize(("speed", "tier"), [("standard", "default"), ("fast", "priority"), ("ultrafast", "ultrafast")])
def test_codex_speed_reaches_start_and_survives_restart(world, agent, speed, tier):
    response = world.client.post("/api/messages", json={"text": "hello", "target": {
        "new_cwd": str(world.checkout), "provider": "codex", "speed": speed}}, headers=world.user())
    assert response.status_code == 200, response.text
    [started] = [params for method, params in agent.rpc.calls if method == "thread/start"]
    assert started["serviceTier"] == tier
    assert started["model"] == "gpt-6.1-sol"
    conn = db.connect(world.db_path)
    try:
        chosen = conn.execute("SELECT model, speed FROM codex_threads WHERE session_id = ?", (SESSION_B,)).fetchone()
        assert tuple(chosen) == ("gpt-6.1-sol", speed)
    finally:
        conn.close()
    finish(agent)
    restarted = CodexAgent(world.db_path)
    restarted.rpc = FakeRpc(restarted)
    restarted.send(SESSION_B, "continue")
    assert restarted.rpc.calls[0][1]["serviceTier"] == tier
    assert restarted.rpc.calls[0][1]["model"] == started["model"]
    if speed != "standard":
        assert started["config"]["features"]["fast_mode"] is True
    if speed == "ultrafast":
        assert started["config"]["features"]["ultrafast_mode"] is True


@pytest.mark.parametrize(("provider", "model", "speed"), [
    ("claude", "claude-opus-5-5", "fast"),
    ("codex", "gpt-6-luna", "ultrafast"),
    ("codex", "gpt-6.1-terra", "ultrafast"),
    ("codex", "gpt-6.1-sol", "turbo"),
])
def test_speed_is_validated_for_the_provider_and_model(world, agent, provider, model, speed):
    response = world.client.post("/api/messages", json={"text": "hello", "target": {
        "new_cwd": str(world.checkout), "provider": provider, "model": model, "speed": speed}}, headers=world.user())
    assert response.status_code == 400 and "offers no speed" in response.json()["detail"]
    assert not agent.rpc.calls


def test_speed_picked_on_add_reaches_the_first_session(world, agent):
    added = world.client.post("/api/items/add", json={"text": "Try Sol Ultrafast", "part": None, "pinned": False,
        "provider": "codex", "speed": "ultrafast"}, headers=world.user())
    assert added.status_code == 200, added.text
    sent = world.client.post("/api/messages", json={"text": "start", "target": {
        "item_id": added.json()["id"], "new_cwd": str(world.checkout)}}, headers=world.user())
    assert sent.status_code == 200, sent.text
    [started] = [params for method, params in agent.rpc.calls if method == "thread/start"]
    assert started["serviceTier"] == "ultrafast"


def test_codex_dropping_requested_speed_fails_before_sending_a_prompt(world, agent):
    request = agent.rpc.request

    def drops_speed(method, params):
        result = request(method, params)
        if method in ("thread/start", "thread/resume"):
            result["serviceTier"] = None
        return result

    agent.rpc.request = drops_speed
    response = world.client.post("/api/messages", json={"text": "hello", "target": {
        "new_cwd": str(world.checkout), "provider": "codex", "speed": "ultrafast"}}, headers=world.user())
    message = world.client.get(f"/api/messages/{response.json()['id']}").json()
    assert message["state"] == "failed" and "did not apply ultrafast" in message["detail"]
    assert message["session_id"] == SESSION_B
    assert not any(method == "turn/start" for method, _ in agent.rpc.calls)
    with pytest.raises(CodexError, match="did not apply ultrafast"):
        restarted = CodexAgent(world.db_path)
        restarted.rpc = FakeRpc(restarted)
        restarted.rpc.request = drops_speed
        restarted.send(SESSION_B, "continue")


def test_saved_threads_resume_after_restart_and_send_now_interrupts(world, agent):
    agent.start(world.checkout, "test", "hello", "gpt-6.1-sol", "high", "standard")
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
    agent.start(world.checkout, "test", "hello", "gpt-6.1-sol", "high", "standard")
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
    agent.start(world.checkout, "test", "hello", "gpt-6.1-sol", "high", "standard")
    restarted = CodexAgent(world.db_path)
    restarted.rpc = FakeRpc(restarted)
    from cockpit.codex_agent import mark_interrupted

    conn = db.connect(world.db_path)
    try:
        mark_interrupted(conn)
    finally:
        conn.close()
    session = restarted.lookup(SESSION_B)
    assert session.state == "failed" and not session.alive
    assert "stopped during this turn" in session.detail


def test_approvals_require_the_user_and_are_bound_to_the_live_rpc_request(world, agent):
    agent.start(world.checkout, "test", "hello", "gpt-6.1-sol", "high", "standard")
    agent.event({"id": "approval1", "method": "item/commandExecution/requestApproval", "params": {
        "threadId": SESSION_B, "turnId": "turn1", "itemId": "cmd1", "command": "git push"}})
    request = agent.requests()[0]
    url = f"/api/codex/requests/{request['id']}/answer"
    assert world.client.post(url, json={"decision": "accept"}, headers=world.agent()).status_code == 403
    assert world.client.post(url, json={"decision": "acceptForSession"}, headers=world.user()).status_code == 409
    assert world.client.post(url, json={"decision": "accept"}, headers=world.user()).status_code == 200
    assert agent.rpc.writes[-1] == {"id": "approval1", "result": {"decision": "accept"}}
    assert world.client.post(url, json={"decision": "accept"}, headers=world.user()).status_code == 409


def test_each_request_for_approval_and_its_answer_is_logged(world, agent):
    agent.start(world.checkout, "test", "hello", "gpt-6.1-sol", "high", "standard")
    agent.event({"id": "approval1", "method": "item/commandExecution/requestApproval", "params": {
        "threadId": SESSION_B, "turnId": "turn1", "itemId": "cmd1", "command": "git push"}})
    command = agent.requests()[0]["id"]
    agent.answer(command, {"decision": "decline"})
    permissions = {"network": {"enabled": True}}
    agent.event({"id": 13, "method": "item/permissions/requestApproval", "params": {
        "threadId": SESSION_B, "permissions": permissions}})
    grant = agent.requests()[0]["id"]
    agent.answer(grant, {"decision": "accept"})
    conn = db.connect(world.db_path)
    try:
        logged = db.rows(conn.execute("SELECT actor, kind, data FROM events WHERE kind LIKE 'codex.%' ORDER BY id").fetchall())
    finally:
        conn.close()
    assert [(event["actor"], event["kind"], event["data"]) for event in logged] == [
        ("codex", "codex.requested", {"request": command, "session_id": SESSION_B,
                                      "method": "item/commandExecution/requestApproval", "call": '{"command": "git push"}'}),
        ("user", "codex.answered", {"request": command, "session_id": SESSION_B,
                                    "method": "item/commandExecution/requestApproval", "decision": "decline"}),
        ("codex", "codex.requested", {"request": grant, "session_id": SESSION_B, "method": "item/permissions/requestApproval",
                                      "call": '{"permissions": {"network": {"enabled": true}}}'}),
        ("user", "codex.answered", {"request": grant, "session_id": SESSION_B,
                                    "method": "item/permissions/requestApproval", "decision": "accept"}),
    ]
    assert world.client.get("/api/patterns?days=7").json()["permissions"]["codex_requests"] == 2


def test_auto_review_decides_escalations_and_each_verdict_is_logged(world, agent):
    agent.start(world.checkout, "test", "hello", "gpt-6.1-sol", "high", "standard")
    started = next(params for method, params in agent.rpc.calls if method == "thread/start")
    assert started["approvalsReviewer"] == "auto_review"
    agent.event({"method": "item/autoApprovalReview/completed", "params": {
        "threadId": SESSION_B, "reviewId": "r1", "action": {"type": "command", "command": "git commit -m x"},
        "review": {"status": "approved", "riskLevel": "low", "rationale": "commits inside the checkout"}}})
    conn = db.connect(world.db_path)
    try:
        logged = db.rows(conn.execute("SELECT actor, data FROM events WHERE kind = 'codex.reviewed'").fetchall())
    finally:
        conn.close()
    assert [(event["actor"], event["data"]) for event in logged] == [
        ("codex", {"session_id": SESSION_B, "status": "approved", "risk": "low", "rationale": "commits inside the checkout",
                   "action": '{"type": "command", "command": "git commit -m x"}'})]
    assert agent.requests() == []


def test_questions_and_permission_grants_do_not_expand_the_request(world, agent):
    agent.start(world.checkout, "test", "hello", "gpt-6.1-sol", "high", "standard")
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
    agent.start(world.checkout, "test", "hello", "gpt-6.1-sol", "high", "standard")
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
    agent.start(world.checkout, "test", "hello", "gpt-6.1-sol", "high", "standard")
    cache = ProviderSessions(None, agent)
    listing = cache.snapshot(wait=False)
    assert [s.provider for s in listing.sessions] == ["codex"]
    assert not listing.failed and not listing.warnings


def test_turn_response_before_notification_is_already_busy(world, agent):
    agent.start(world.checkout, "test", "hello", "gpt-6.1-sol", "high", "standard")
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
    agent.start(world.checkout, "test", "hello", "gpt-6.1-sol", "high", "standard")
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
    agent.start(world.checkout, "test", "hello", "gpt-6.1-sol", "high", "standard")
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
    agent.start(world.checkout, "test", "hello", "gpt-6.1-sol", "high", "standard")
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
    agent.start(world.checkout, "test", "hello", "gpt-6.1-sol", "high", "standard")
    finish(agent)
    world.deliverer.codex = None
    response = world.client.post("/api/messages", json={"text": "continue", "target": {"session_id": SESSION_B}},
                                 headers=world.user())
    message = world.client.get(f"/api/messages/{response.json()['id']}").json()
    assert message["state"] == "failed" and "not enabled" in message["detail"]
    assert world.runner.calls == []


def test_the_session_view_shows_a_codex_threads_conversation(world, agent, monkeypatch):
    # the sidebar's session view read only Claude transcripts, so a Codex session showed no conversation.
    agent.start(world.checkout, "test", "hello", "gpt-6.1-sol", "high", "standard")
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


class SyntheticCodexProcess:
    """An in-memory app-server whose stdout can end while its process stays alive."""

    def __init__(self):
        self.output = queue.Queue()
        self.stdout = iter(self.output.get, None)
        self.stdin = SimpleNamespace(write=self.write, flush=lambda: None)
        self.pid = 999
        self.returncode = None
        self.output_ended = False
        self.terminated = False
        self.reaped = False
        self.loaded = False
        self.calls = []

    def poll(self):
        return self.returncode

    def write(self, line):
        if self.output_ended:
            raise BrokenPipeError("stdout already ended")
        message = json.loads(line)
        method = message["method"]
        self.calls.append(method)
        if "id" not in message:
            return
        result = {}
        if method == "pending":
            self.end_output()
            return
        if method in {"thread/start", "thread/resume"}:
            self.loaded = True
            result = {"thread": {"id": SESSION_B}, "serviceTier": "default"}
        elif method == "turn/start":
            if not self.loaded:
                self.output.put(json.dumps({"id": message["id"], "error": {"message": "thread not loaded"}}))
                return
            result = {"turn": {"id": "turn1"}}
        self.output.put(json.dumps({"id": message["id"], "result": result}))

    def end_output(self):
        if not self.output_ended:
            self.output_ended = True
            self.output.put(None)

    def terminate(self):
        self.terminated = True
        self.returncode = 0
        self.end_output()

    def wait(self, timeout):
        assert self.returncode is not None
        self.reaped = True
        return self.returncode


def synthetic_transport(monkeypatch, on_event):
    processes = []

    def spawn(*args, **kwargs):
        if processes:
            assert processes[-1].terminated and processes[-1].reaped
        process = SyntheticCodexProcess()
        processes.append(process)
        return process

    monkeypatch.setattr("cockpit.codex_agent.subprocess.Popen", spawn)
    return AppServer("/synthetic-codex", on_event), processes


@pytest.mark.parametrize("method", ["thread/resume", "thread/start"])
@pytest.mark.parametrize("reconnect", [False, True])
def test_thread_response_followed_by_eof_cannot_restore_loaded_state(world, agent, monkeypatch, method, reconnect):
    if method == "thread/resume":
        agent.start(world.checkout, "test", "hello", "gpt-6.1-sol", "high", "standard")
        finish(agent)
        agent.rpc.close()
    agent.rpc, processes = synthetic_transport(monkeypatch, agent.event)
    request = agent.rpc._request

    def disconnect_after_response(request_method, params, *args):
        result = request(request_method, params, *args)
        if request_method == method and len(processes) == 1:
            processes[0].end_output()
            agent.rpc._reader.join(timeout=2)
            assert not agent.rpc._reader.is_alive()
            if reconnect:
                agent.rpc.start()
        return result

    monkeypatch.setattr(agent.rpc, "_request", disconnect_after_response)
    try:
        with pytest.raises(CodexError):
            if method == "thread/start":
                agent.start(world.checkout, "test", "hello", "gpt-6.1-sol", "high", "standard")
            else:
                agent.send(SESSION_B, "continue")
        assert SESSION_B not in agent._loaded
        assert "turn/start" not in processes[0].calls
        agent.send(SESSION_B, "retry")
        assert processes[1].calls == ["initialize", "initialized", "thread/resume", "turn/start"]
        assert agent.lookup(SESSION_B).working_now
    finally:
        agent.rpc.close()


@pytest.mark.parametrize("load_method", ["start", "resume", "loaded"])
def test_turn_start_uses_the_connection_that_loaded_the_thread(world, agent, monkeypatch, load_method):
    if load_method != "start":
        agent.start(world.checkout, "test", "hello", "gpt-6.1-sol", "high", "standard")
        finish(agent)
        agent.rpc.close()
    agent.rpc, processes = synthetic_transport(monkeypatch, agent.event)
    if load_method == "loaded":
        agent.send(SESSION_B, "load")
        finish(agent)
    save = agent._save

    def reconnect_before_turn(session_id, **changes):
        save(session_id, **changes)
        if changes.get("state") == "working":
            agent.rpc.close()
            agent.rpc.start()

    monkeypatch.setattr(agent, "_save", reconnect_before_turn)
    try:
        with pytest.raises(CodexError):
            if load_method == "start":
                agent.start(world.checkout, "test", "hello", "gpt-6.1-sol", "high", "standard")
            else:
                agent.send(SESSION_B, "continue")
        assert processes[1].calls == ["initialize", "initialized"]
        assert SESSION_B not in agent._loaded
        assert SESSION_B not in agent._active
        monkeypatch.setattr(agent, "_save", save)
        agent.send(SESSION_B, "retry")
        assert processes[1].calls[-2:] == ["thread/resume", "turn/start"]
    finally:
        agent.rpc.close()


def test_eof_with_a_live_process_fails_pending_requests_and_replaces_it(monkeypatch):
    events = []
    rpc, processes = synthetic_transport(monkeypatch, events.append)
    try:
        rpc.start()
        with pytest.raises(CodexError, match="disconnected"):
            rpc.request("pending", {})
        rpc._reader.join(timeout=2)
        assert not rpc._reader.is_alive()
        assert rpc._waiting == {}
        assert events[-1]["method"] == "cockpit/disconnected"
        assert processes[0].poll() is None
        rpc.start()
        assert rpc.process is not processes[0]
        assert processes[0].terminated and processes[0].reaped
        assert rpc.request("ping", {}) == {}
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
