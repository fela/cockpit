"""Sessions on the ACP agents, Gemini and Grok (SPEC SESSION-30), against a stand-in agent (fake_acp.py)."""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from cockpit import config, db, parts, server, sweeper
from cockpit.acp_agent import PERMISSION_METHOD, AcpAgent, AcpError, Agents, agent_args
from tests.conftest import SESSION_A, make_session
from tests.test_api import put_item, put_step
from tests.test_setup import write_setup

FAKE = Path(__file__).parent / "fake_acp.py"


def put_agents_on_path(folder: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`gemini` and `grok` commands on PATH that run the stand-in agent with the tests' Python, as installed CLIs run."""
    folder.mkdir(exist_ok=True)
    for name in ("gemini", "grok"):
        command = folder / name
        command.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
        command.chmod(0o755)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ['PATH']}")


def wait_until(check, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.02)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "cockpit.db"
    conn = db.connect(path)
    db.migrate(conn)
    conn.close()
    return path


@pytest.fixture
def gemini(db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FAKE_ACP_ARGS", str(tmp_path / "args.jsonl"))
    put_agents_on_path(tmp_path / "bin", monkeypatch)
    agent = AcpAgent(db_path, "gemini")
    yield agent
    for connection in agent._connections.values():
        connection.close()


def start(agent: AcpAgent, cwd: Path, text: str, model: str) -> str:
    """A new session for a new message, as delivery starts one."""
    conn = db.connect(agent.db_path)
    try:
        message_id = conn.execute("INSERT INTO messages (author, text, state, created_at, new_cwd, provider)"
                                  " VALUES ('user', ?, 'queued', ?, ?, ?)",
                                  (text, db.now_iso(), str(cwd), agent.provider)).lastrowid
    finally:
        conn.close()
    return agent.start_for_message(message_id, cwd, "a session", text, model, None, "standard")


def received(tmp_path: Path) -> list[dict]:
    """What the stand-in agents recorded: each start's words and folder, then each message they received."""
    return [json.loads(line) for line in (tmp_path / "args.jsonl").read_text().splitlines()]


def idle(agent: AcpAgent, session_id: str) -> bool:
    return not agent.lookup(session_id).working_now


def test_each_agent_starts_in_acp_mode_on_its_model() -> None:
    assert agent_args("gemini", "pro") == ["gemini", "--acp", "--approval-mode", "auto_edit", "--model", "pro"]
    assert agent_args("grok", "default") == ["grok", "agent", "stdio"]
    assert agent_args("grok", "grok-4.6") == ["grok", "agent", "--model", "grok-4.6", "stdio"]
    with pytest.raises(ValueError):
        agent_args("codex", "gpt-6.1-sol")


def test_a_session_streams_its_turn_into_the_log(gemini: AcpAgent, tmp_path: Path, db_path: Path) -> None:
    sid = start(gemini, tmp_path, "Say hello", "flash")
    wait_until(lambda: idle(gemini, sid))
    session = gemini.lookup(sid)
    # The words after the tool call are the turn's last message, which the session shows; the reply holds both.
    assert (session.provider, session.state, session.detail) == ("gemini", "done", "from the fake")
    assert received(tmp_path)[0] == {"args": ["--acp", "--approval-mode", "auto_edit", "--model", "flash"],
                                     "cwd": str(tmp_path)}
    log = gemini.log(sid)
    assert [part["kind"] for part in log["parts"]] == ["instruction", "says", "call", "says"]
    assert log["parts"][0]["text"] == "Say hello"  # the control-plane words Cockpit adds stay out
    call = log["parts"][2]
    assert {key: call[key] for key in ("tool_name", "title", "error")} == {
        "tool_name": "read", "title": "done", "error": False}
    assert "text" not in call and "result" not in call  # a call's input and output stay out of the database
    conn = db.connect(db_path)
    try:
        turn = conn.execute("SELECT reply FROM session_turns WHERE session_id = ?", (sid,)).fetchone()
        stored = " ".join(row["part"] for row in conn.execute("SELECT part FROM acp_log"))
    finally:
        conn.close()
    assert turn["reply"] == "Hello\n\nfrom the fake"
    assert "# Readme" not in stored and "README.md\"}" not in stored


def test_a_permission_request_reaches_the_page_and_its_answer_the_agent(gemini: AcpAgent, tmp_path: Path, db_path: Path) -> None:
    sid = start(gemini, tmp_path, "[[ASK]] run the tests", "auto")
    wait_until(lambda: gemini.requests())
    [request] = gemini.requests()
    assert request["method"] == PERMISSION_METHOD and request["provider"] == "gemini"
    assert request["params"] == {"threadId": sid, "reason": "Run make test", "cwd": str(tmp_path), "command": "make test",
                                 "paths": [], "diffs": [], "availableDecisions": ["accept", "decline", "cancel"]}
    session = gemini.lookup(sid)
    assert session.waits_on_dialog and session.needs == "Answer the Gemini request on Cockpit"
    with pytest.raises(AcpError):
        gemini.answer(request["id"], {"decision": "maybe"})
    gemini.answer(request["id"], {"decision": "accept"})
    wait_until(lambda: idle(gemini, sid))
    assert gemini.requests() == []
    assert gemini.lookup(sid).detail == "You picked yes."
    with pytest.raises(AcpError):
        gemini.answer(request["id"], {"decision": "accept"})
    conn = db.connect(db_path)
    try:
        events = [(row["kind"], row["data"]) for row in conn.execute("SELECT kind, data FROM events ORDER BY id")]
        stored = " ".join(row["part"] for row in conn.execute("SELECT part FROM acp_log"))
    finally:
        conn.close()
    assert [kind for kind, _ in events][-2:] == ["gemini.requested", "gemini.answered"]
    # Backups keep events and the session log: the call's command and output are in neither.
    assert json.loads(events[-2][1]) == {"request": request["id"], "session_id": sid, "method": PERMISSION_METHOD,
                                         "tool": "execute"}
    assert "rawInput" not in stored and '"command"' not in stored


def test_an_edit_request_shows_its_file_and_diff(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = start(gemini, tmp_path, "[[EDIT]] the notes", "auto")
    wait_until(lambda: gemini.requests())
    [request] = gemini.requests()
    params = request["params"]
    assert (params["command"], params["paths"], params["availableDecisions"]) == (
        "", ["/work/notes.txt"], ["accept", "decline", "cancel"])
    assert params["diffs"] == [{"path": "/work/notes.txt",
                                "diff": "--- /work/notes.txt\n+++ /work/notes.txt\n@@ -1 +1 @@\n-draft\n+final\n"}]
    gemini.answer(request["id"], {"decision": "accept"})
    wait_until(lambda: idle(gemini, sid))
    assert gemini.lookup(sid).detail == "You picked yes."


def test_allow_never_falls_back_to_allow_always(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = start(gemini, tmp_path, "[[ALWAYS]] run the tests", "auto")
    wait_until(lambda: gemini.requests())
    [request] = gemini.requests()
    assert request["params"]["availableDecisions"] == ["decline", "cancel"]
    with pytest.raises(AcpError, match="no one-time option"):
        gemini.answer(request["id"], {"decision": "accept"})
    assert [each["id"] for each in gemini.requests()] == [request["id"]]  # the card stays
    gemini.answer(request["id"], {"decision": "decline"})
    wait_until(lambda: idle(gemini, sid))
    assert gemini.lookup(sid).detail == "You picked no."


def test_a_call_that_says_nothing_of_what_it_does_cannot_be_allowed(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = start(gemini, tmp_path, "[[VAGUE]] do it", "auto")
    wait_until(lambda: gemini.requests())
    [request] = gemini.requests()
    assert request["params"]["availableDecisions"] == ["decline", "cancel"]
    with pytest.raises(AcpError):
        gemini.answer(request["id"], {"decision": "accept"})
    gemini.answer(request["id"], {"decision": "cancel"})
    wait_until(lambda: idle(gemini, sid))
    assert gemini.lookup(sid).detail == "You picked cancelled."


def test_deny_picks_the_agents_reject_option(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = start(gemini, tmp_path, "[[ASK]] run the tests", "auto")
    wait_until(lambda: gemini.requests())
    gemini.answer(gemini.requests()[0]["id"], {"decision": "decline"})
    wait_until(lambda: idle(gemini, sid))
    assert gemini.lookup(sid).detail == "You picked no."


def test_a_cancelled_turn_ends(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = start(gemini, tmp_path, "[[WAIT]] for ever", "auto")
    assert gemini.lookup(sid).working_now
    gemini.stop(sid)
    assert not gemini.lookup(sid).working_now
    assert gemini.lookup(sid).detail == "Gemini stopped: cancelled"


def test_stop_cancels_an_open_request_and_no_answer_approves_it_after(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = start(gemini, tmp_path, "[[ASK]] run the tests", "auto")
    wait_until(lambda: gemini.requests())
    [request] = gemini.requests()
    with gemini._lock:
        gemini._cancelling.add(sid)  # Stop has begun: an Allow arriving now loses
    with pytest.raises(AcpError, match="being stopped"):
        gemini.answer(request["id"], {"decision": "accept"})
    with gemini._lock:
        gemini._cancelling.discard(sid)
    gemini.stop(sid)
    assert gemini.lookup(sid).detail == "You picked cancelled."
    assert gemini.requests() == [] and gemini._cancelling == set()
    with pytest.raises(AcpError, match="already ended"):
        gemini.answer(request["id"], {"decision": "accept"})
    cancels = [each for each in received(tmp_path) if each.get("method") == "session/cancel"]
    assert cancels == [{"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}}]


class Other:
    """A process other than the one running the session's turn."""

    def __init__(self) -> None:
        self.written: list[dict] = []

    def write(self, message: dict) -> None:
        self.written.append(message)


def test_requests_and_updates_from_another_process_are_refused(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = start(gemini, tmp_path, "[[WAIT]] for ever", "auto")
    other = Other()
    gemini._permission(other, 7, {"sessionId": sid, "toolCall": {"title": "Run rm", "rawInput": {"command": "rm -r ."}},
                                  "options": [{"optionId": "yes", "kind": "allow_once"}]})
    assert other.written == [{"id": 7, "result": {"outcome": {"outcome": "cancelled"}}}]
    assert gemini.requests() == []
    before = gemini.log(sid)["parts"]
    gemini._update(other, sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "forged"}})
    gemini.stop(sid)
    assert gemini.log(sid)["parts"] == before


def test_each_folder_gets_its_own_agent_process(gemini: AcpAgent, tmp_path: Path) -> None:
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    a = start(gemini, first, "Say hello", "auto")
    b = start(gemini, second, "Say hello", "auto")
    c = start(gemini, first, "Say hello", "auto")
    wait_until(lambda: idle(gemini, a) and idle(gemini, b) and idle(gemini, c))
    assert sorted(gemini._connections) == [("auto", str(first)), ("auto", str(second))]
    assert [each["cwd"] for each in received(tmp_path) if "cwd" in each and "args" in each] == [str(first), str(second)]
    assert gemini._loaded[a] is gemini._loaded[c] is not gemini._loaded[b]


def test_concurrent_starts_wait_for_the_agent_to_initialize(gemini: AcpAgent, tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_ACP_SLOW_START", "0.5")
    got: list = []
    threads = [threading.Thread(target=lambda: got.append(gemini._connection("auto", tmp_path))) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len({id(each) for each in got}) == 1 and got[0].capabilities == {"loadSession": True}
    assert len([each for each in received(tmp_path) if "args" in each]) == 1


def test_sessions_start_and_load_without_an_auto_mode(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = start(gemini, tmp_path, "Say hello", "auto")
    wait_until(lambda: idle(gemini, sid))
    gemini._connections[("auto", str(tmp_path))].close()
    wait_until(lambda: sid not in gemini._loaded)
    gemini.send(sid, "Say hello again", restart=False)
    wait_until(lambda: idle(gemini, sid))
    sent = {each["method"]: each["params"] for each in received(tmp_path) if each.get("method") in ("session/new", "session/load")}
    assert sent == {"session/new": {"cwd": str(tmp_path), "mcpServers": []},
                    "session/load": {"sessionId": sid, "cwd": str(tmp_path), "mcpServers": []}}


def test_a_restarted_agent_loads_the_session_without_logging_its_replay(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = start(gemini, tmp_path, "Say hello", "auto")
    wait_until(lambda: idle(gemini, sid))
    gemini._connections[("auto", str(tmp_path))].close()
    wait_until(lambda: sid not in gemini._loaded)
    gemini.send(sid, "Say hello again", restart=False)
    wait_until(lambda: idle(gemini, sid))
    texts = [part["text"] for part in gemini.log(sid)["parts"] if part["kind"] in ("instruction", "says")]
    assert texts == ["Say hello", "Hello ", "from the fake", "Say hello again", "Hello ", "from the fake"]


def test_an_agent_that_ends_mid_turn_fails_the_turn(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = start(gemini, tmp_path, "[[WAIT]] for ever", "auto")
    gemini._connections[("auto", str(tmp_path))].close()
    wait_until(lambda: idle(gemini, sid))
    session = gemini.lookup(sid)
    assert session.state == "failed" and "ended" in session.detail


def test_a_server_restart_fails_the_turns_it_cut_off(db_path: Path) -> None:
    conn = db.connect(db_path)
    try:
        for sid, provider, state in (("cut", "gemini", "working"), ("over", "gemini", "done"), ("grok", "grok", "working")):
            conn.execute("INSERT INTO acp_sessions (session_id, provider, name, cwd, model, state, updated_at)"
                         " VALUES (?, ?, 'x', '/tmp', 'auto', ?, ?)", (sid, provider, state, db.now_iso()))
    finally:
        conn.close()
    agent = AcpAgent(db_path, "gemini")
    agent.mark_interrupted()
    states = {each.session_id: (each.state, each.detail) for each in agent.sessions()}
    assert states == {"cut": ("failed", "Cockpit restarted during this turn; check before continuing"), "over": ("done", "")}
    assert AcpAgent(db_path, "grok").lookup("grok").state == "done"  # its own agent marks it


def test_a_model_the_agent_does_not_offer_is_refused(gemini: AcpAgent, tmp_path: Path) -> None:
    with pytest.raises(AcpError, match="offers no model"):
        start(gemini, tmp_path, "hi", "gpt-6.1-sol")
    assert gemini.sessions() == []


def test_a_missing_command_says_what_to_check(db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    agent = AcpAgent(db_path, "grok")
    with pytest.raises(AcpError, match="installed and signed in"):
        start(agent, tmp_path, "hi", "default")


class FakeCodex:
    """Just enough of CodexAgent for the router."""

    def __init__(self) -> None:
        self.started: list[tuple] = []
        self.pending = [{"id": "codex-1", "method": "item/commandExecution/requestApproval", "params": {"threadId": "t"}}]

    def owns(self, session_id: str) -> bool:
        return session_id == "t"

    def sessions(self) -> list:
        return []

    def requests(self) -> list[dict[str, Any]]:
        return self.pending

    def answer(self, key: str, result: dict) -> None:
        self.pending = []

    def start_for_message(self, *args: Any) -> str:
        self.started.append(args)
        return "t"


def test_the_router_sends_each_call_to_the_agent_that_owns_the_session(gemini: AcpAgent, tmp_path: Path) -> None:
    codex = FakeCodex()
    agents = Agents(codex, {"gemini": gemini})
    assert agents.start_for_message(1, tmp_path, "c", "hi", "gpt-6.1-sol", "high", "standard", provider="codex") == "t"
    sid = start(gemini, tmp_path, "Say hello", "auto")
    wait_until(lambda: idle(gemini, sid))
    assert agents.owns(sid) and agents.owns("t") and not agents.owns("nobody")
    assert agents.lookup(sid).provider == "gemini"
    assert agents.log(sid)["parts"][0]["text"] == "Say hello"
    assert [each["provider"] for each in agents.requests()] == ["codex"]
    agents.answer("codex-1", {"decision": "accept"})
    assert codex.pending == []
    with pytest.raises(Exception, match="Grok is not enabled"):
        agents.start_for_message(2, tmp_path, "g", "hi", "default", None, "standard", provider="grok")


def test_the_setup_enables_the_acp_agents(tmp_path: Path) -> None:
    setup = config.read_setup(write_setup(tmp_path, {"providers": ["claude", "gemini", "grok"]}), parts.setup_tables())
    assert setup.providers == ("claude", "gemini", "grok")
    with pytest.raises(config.SetupError, match="providers must be"):
        config.read_setup(write_setup(tmp_path, {"providers": ["claude", "llama"]}), parts.setup_tables())


def test_a_message_starts_a_gemini_session_through_delivery(world, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    setup = config.setup()
    monkeypatch.setattr(config, "setup", lambda: dataclasses.replace(setup, providers=("claude", "gemini")))
    put_agents_on_path(tmp_path / "bin", monkeypatch)
    agent = AcpAgent(world.db_path, "gemini")
    world.deliverer.codex = Agents(None, {"gemini": agent})
    world.deliverer.enabled_providers = ("claude", "gemini")
    try:
        response = world.client.post("/api/messages", json={"text": "Say hello", "target": {
            "new_cwd": str(world.checkout), "provider": "gemini", "model": "pro"}}, headers=world.user())
        assert response.status_code == 200, response.text
        sent = world.client.get(f"/api/messages/{response.json()['id']}").json()
        assert (sent["state"], sent["via"], sent["detail"]) == ("sent", "new", "started a Gemini session")
        sid = sent["session_id"]
        wait_until(lambda: idle(agent, sid))
        assert world.runner.calls == []  # no claude command ran
        log = world.client.get(f"/api/sessions/{sid}/log", headers=world.user()).json()
        assert [part["kind"] for part in log["parts"]] == ["instruction", "says", "call", "says"]
        again = world.client.post("/api/messages", json={"text": "Once more", "target": {"session_id": sid}},
                                  headers=world.user())
        assert world.client.get(f"/api/messages/{again.json()['id']}").json()["detail"] == "sent a prompt to Gemini"
        wait_until(lambda: idle(agent, sid))
    finally:
        for connection in agent._connections.values():
            connection.close()


def test_a_call_without_a_kind_is_named_by_its_title() -> None:
    from cockpit.acp_agent import _tool_name

    assert _tool_name({"kind": "read", "title": "Read README.md"}) == "read"
    assert _tool_name({"title": "Execute `cat hello.txt`"}) == "execute"  # Grok sends no kind
    assert _tool_name({}) == "tool"


def test_without_an_acp_provider_nothing_of_them_runs_or_shows(world, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_agent(*args: Any) -> None:
        raise AssertionError("an ACP agent was made")

    monkeypatch.setattr(server.acp_agent, "AcpAgent", no_agent)
    codex = object()
    assert server.session_agents(("claude", "codex"), codex) == (codex, {})
    assert server.session_agents(("claude",), None) == (None, {})
    state = world.client.get("/api/state", headers=world.user()).json()
    assert set(state["session_options"]) <= {"claude", "codex"}
    assert not isinstance(world.deliverer.codex, Agents)


def test_a_gemini_shell_call_shows_its_command_and_keeps_it_out_of_the_log(gemini: AcpAgent, tmp_path: Path,
                                                                            db_path: Path) -> None:
    sid = start(gemini, tmp_path, "[[SHELL]] run the tests", "auto")
    wait_until(lambda: gemini.requests())
    [request] = gemini.requests()
    assert (request["params"]["command"], request["params"]["availableDecisions"]) == (
        "make test TOKEN=s3cret", ["accept", "decline", "cancel"])
    gemini.answer(request["id"], {"decision": "accept"})
    wait_until(lambda: idle(gemini, sid))
    [call] = [part for part in gemini.log(sid)["parts"] if part["kind"] == "call"]
    assert (call["tool_name"], call["title"]) == ("execute", "done")
    conn = db.connect(db_path)
    try:
        stored = " ".join(row["part"] for row in conn.execute("SELECT part FROM acp_log"))
        events = " ".join(row["data"] for row in conn.execute("SELECT data FROM events"))
    finally:
        conn.close()
    assert "s3cret" not in stored and "s3cret" not in events


def test_an_answer_is_written_under_the_lock_stop_takes(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = start(gemini, tmp_path, "[[ASK]] run the tests", "auto")
    wait_until(lambda: gemini.requests())
    [request] = gemini.requests()
    real = gemini._pending_on[request["id"]]
    held: list[bool] = []

    class Watched:
        def write(self, message: dict) -> None:
            held.append(gemini._lock._is_owned())
            real.write(message)

    gemini._pending_on[request["id"]] = Watched()
    gemini.answer(request["id"], {"decision": "accept"})
    wait_until(lambda: idle(gemini, sid))
    assert held == [True]


def test_the_sweeper_ignores_the_acp_agents_requests_and_answers() -> None:
    assert {"gemini.requested", "gemini.answered", "grok.requested", "grok.answered"} <= set(sweeper.QUIET_KINDS)


def test_a_codex_start_goes_to_codex_while_an_acp_agent_is_on(world, monkeypatch: pytest.MonkeyPatch) -> None:
    setup = config.setup()
    monkeypatch.setattr(config, "setup", lambda: dataclasses.replace(setup, providers=("claude", "codex", "gemini")))
    codex = FakeCodex()
    world.deliverer.codex = Agents(codex, {"gemini": AcpAgent(world.db_path, "gemini")})
    world.deliverer.enabled_providers = ("claude", "codex", "gemini")
    response = world.client.post("/api/messages", json={"text": "Say hello", "target": {
        "new_cwd": str(world.checkout), "provider": "codex"}}, headers=world.user())
    assert response.status_code == 200, response.text
    sent = world.client.get(f"/api/messages/{response.json()['id']}").json()
    assert (sent["state"], sent["detail"], sent["session_id"]) == ("sent", "started a Codex thread", "t")
    [args] = codex.started
    assert args[3].endswith("Say hello")


def test_a_codex_thread_is_not_delivered_while_only_an_acp_agent_is_on(world) -> None:
    conn = db.connect(world.db_path)
    try:
        conn.execute("INSERT INTO codex_threads (session_id, name, cwd, updated_at) VALUES (?, 'c', '/tmp', ?)",
                     (SESSION_A, db.now_iso()))
    finally:
        conn.close()
    world.deliverer.codex = Agents(None, {"gemini": AcpAgent(world.db_path, "gemini")})
    response = world.client.post("/api/messages", json={"text": "hi", "target": {"session_id": SESSION_A}},
                                 headers=world.user())
    sent = world.client.get(f"/api/messages/{response.json()['id']}").json()
    assert sent["state"] == "failed" and "Codex is not enabled" in sent["detail"]
    assert world.runner.calls == []


def test_the_board_shows_a_step_for_a_gemini_session(world) -> None:
    conn = db.connect(world.db_path)
    try:
        conn.execute("INSERT INTO acp_sessions (session_id, provider, name, cwd, model, state, updated_at)"
                     " VALUES (?, 'gemini', 'g', '/tmp', 'pro', 'done', ?)", (SESSION_A, db.now_iso()))
    finally:
        conn.close()
    world.cache.sessions = [dataclasses.replace(make_session(SESSION_A, pid=None), provider="gemini")]
    put_item(world)
    put_step(world, session_id=SESSION_A)
    response = world.client.get("/api/state", headers=world.user())
    assert response.status_code == 200, response.text
    state = response.json()
    step = state["queue"][0]
    assert (step["target"]["provider"], step["cache"]) == ("gemini", None)
    [row] = [each for each in state["sessions"] if each["session_id"] == SESSION_A]
    assert (row["model"], row["settings"]["provider"], row["settings"]["model"]) == ("pro", "gemini", "pro")
