"""Sessions on the ACP agents, Gemini and Grok (SPEC SESSION-30), against a stand-in agent (fake_acp.py)."""

from __future__ import annotations

import dataclasses
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from cockpit import config, db, parts
from cockpit.acp_agent import PERMISSION_METHOD, AcpAgent, AcpError, Agents, agent_args
from tests.test_setup import write_setup

FAKE = Path(__file__).parent / "fake_acp.py"


def fake_command(tmp_path: Path) -> str:
    """A command that runs the stand-in agent with the tests' Python, as an installed CLI would run."""
    command = tmp_path / "fake-agent"
    command.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    command.chmod(0o755)
    return str(command)


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
    agent = AcpAgent(db_path, "gemini", fake_command(tmp_path))
    yield agent
    for connection in agent._connections.values():
        connection.close()


def idle(agent: AcpAgent, session_id: str) -> bool:
    return not agent.lookup(session_id).working_now


def test_each_agent_starts_in_acp_mode_on_its_model() -> None:
    assert agent_args("gemini", "pro") == ["--acp", "--approval-mode", "auto_edit", "--model", "pro"]
    assert agent_args("grok", "default") == ["agent", "stdio"]
    assert agent_args("grok", "grok-4.6") == ["agent", "--model", "grok-4.6", "stdio"]
    with pytest.raises(ValueError):
        agent_args("codex", "gpt-6.1-sol")


def test_a_session_streams_its_turn_into_the_log(gemini: AcpAgent, tmp_path: Path, db_path: Path) -> None:
    sid = gemini.start(tmp_path, "greet", "Say hello", "flash")
    wait_until(lambda: idle(gemini, sid))
    session = gemini.lookup(sid)
    # The words after the tool call are the turn's last message, which the session shows; the reply holds both.
    assert (session.provider, session.state, session.detail) == ("gemini", "done", "from the fake")
    assert json.loads((tmp_path / "args.jsonl").read_text().splitlines()[0])[-2:] == ["--model", "flash"]
    log = gemini.log(sid)
    assert [part["kind"] for part in log["parts"]] == ["instruction", "says", "call", "says"]
    assert log["parts"][0]["text"] == "Say hello"  # the control-plane words Cockpit adds stay out
    call = log["parts"][2]
    assert (call["tool_name"], call["title"], call["result"], call["error"]) == ("read", "Read README.md", "# Readme", False)
    conn = db.connect(db_path)
    try:
        turn = conn.execute("SELECT reply FROM session_turns WHERE session_id = ?", (sid,)).fetchone()
    finally:
        conn.close()
    assert turn["reply"] == "Hello\n\nfrom the fake"


def test_a_permission_request_reaches_the_page_and_its_answer_the_agent(gemini: AcpAgent, tmp_path: Path, db_path: Path) -> None:
    sid = gemini.start(tmp_path, "tests", "[[ASK]] run the tests", "auto")
    wait_until(lambda: gemini.requests())
    [request] = gemini.requests()
    assert request["method"] == PERMISSION_METHOD and request["provider"] == "gemini"
    assert request["params"] == {"threadId": sid, "reason": "Run make test", "cwd": str(tmp_path), "command": "make test",
                                 "availableDecisions": ["accept", "decline", "cancel"]}
    session = gemini.lookup(sid)
    assert session.waits_on_dialog and session.needs == "Answer the Gemini request on Cockpit"
    with pytest.raises(AcpError):
        gemini.answer(request["id"], {"decision": "maybe"})
    gemini.answer(request["id"], {"decision": "accept"})
    wait_until(lambda: idle(gemini, sid))
    assert gemini.requests() == []
    assert gemini.lookup(sid).detail == "You picked yes."
    call = next(part for part in gemini.log(sid)["parts"] if part["kind"] == "call")
    assert (call["text"], call["result"]) == ("make test", "picked yes")
    with pytest.raises(AcpError):
        gemini.answer(request["id"], {"decision": "accept"})
    conn = db.connect(db_path)
    try:
        kinds = [row["kind"] for row in conn.execute("SELECT kind FROM events ORDER BY id")]
    finally:
        conn.close()
    assert kinds[-2:] == ["gemini.requested", "gemini.answered"]


def test_deny_picks_the_agents_reject_option(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = gemini.start(tmp_path, "tests", "[[ASK]] run the tests", "auto")
    wait_until(lambda: gemini.requests())
    gemini.answer(gemini.requests()[0]["id"], {"decision": "decline"})
    wait_until(lambda: idle(gemini, sid))
    assert gemini.lookup(sid).detail == "You picked no."


def test_a_cancelled_turn_ends(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = gemini.start(tmp_path, "slow", "[[WAIT]] for ever", "auto")
    assert gemini.lookup(sid).working_now
    gemini.stop(sid)
    assert not gemini.lookup(sid).working_now
    assert gemini.lookup(sid).detail == "Gemini stopped: cancelled"


def test_a_restarted_agent_loads_the_session_without_logging_its_replay(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = gemini.start(tmp_path, "greet", "Say hello", "auto")
    wait_until(lambda: idle(gemini, sid))
    gemini._connections["auto"].close()
    wait_until(lambda: sid not in gemini._loaded)
    gemini.send(sid, "Say hello again")
    wait_until(lambda: idle(gemini, sid))
    texts = [part["text"] for part in gemini.log(sid)["parts"] if part["kind"] in ("instruction", "says")]
    assert texts == ["Say hello", "Hello ", "from the fake", "Say hello again", "Hello ", "from the fake"]


def test_an_agent_that_ends_mid_turn_fails_the_turn(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = gemini.start(tmp_path, "slow", "[[WAIT]] for ever", "auto")
    gemini._connections["auto"].close()
    wait_until(lambda: idle(gemini, sid))
    session = gemini.lookup(sid)
    assert session.state == "failed" and "ended" in session.detail


def test_a_model_the_agent_does_not_offer_is_refused(gemini: AcpAgent, tmp_path: Path) -> None:
    with pytest.raises(AcpError, match="offers no model"):
        gemini.start(tmp_path, "x", "hi", "gpt-6.1-sol")
    assert gemini.sessions() == []


def test_a_missing_command_says_what_to_check(db_path: Path, tmp_path: Path) -> None:
    agent = AcpAgent(db_path, "grok", str(tmp_path / "no-such-grok"))
    with pytest.raises(AcpError, match="installed and signed in"):
        agent.start(tmp_path, "x", "hi", "default")


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
    assert agents.start_for_message(1, tmp_path, "c", "hi", "gpt-6.1-sol", "high", "standard") == "t"
    sid = gemini.start(tmp_path, "greet", "Say hello", "auto")
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
    setup = config.read_setup(write_setup(tmp_path, {"providers": ["claude", "gemini", "grok"], "gemini_command": "/opt/gemini"}),
                              parts.setup_tables())
    assert (setup.providers, setup.gemini_command, setup.grok_command) == (("claude", "gemini", "grok"), "/opt/gemini", "grok")
    with pytest.raises(config.SetupError, match="providers must be"):
        config.read_setup(write_setup(tmp_path, {"providers": ["claude", "llama"]}), parts.setup_tables())


def test_a_message_starts_a_gemini_session_through_delivery(world, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    setup = config.setup()
    monkeypatch.setattr(config, "setup", lambda: dataclasses.replace(setup, providers=("claude", "gemini")))
    agent = AcpAgent(world.db_path, "gemini", fake_command(tmp_path))
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
