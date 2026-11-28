"""Sessions on the ACP agents, Gemini (Antigravity's `agy`) and Grok (SPEC SESSION-30), against a stand-in agent (fake_acp.py)."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from cockpit import acp_agent, config, db, parts, safe_shell, server, sweeper
from cockpit.acp_agent import PERMISSION_METHOD, AcpAgent, AcpError, Agents, agent_args, agent_env
from tests.conftest import SESSION_A, make_session
from tests.test_api import put_item, put_step
from tests.test_setup import write_setup

FAKE = Path(__file__).parent / "fake_acp.py"


def put_agents_on_path(folder: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`agy` and `grok` commands on PATH that run the stand-in agent with the tests' Python, as installed CLIs run."""
    folder.mkdir(exist_ok=True)
    for name in ("agy", "grok"):
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
    assert agent_args("gemini", "default") == ["agy", "acp"]
    assert agent_args("grok", "default") == ["grok", "agent", "stdio"]
    assert agent_args("grok", "grok-4.6") == ["grok", "agent", "--model", "grok-4.6", "stdio"]
    assert agent_args("gemini", "gemini-3-pro") == ["agy", "acp"]
    with pytest.raises(ValueError):
        agent_args("codex", "gpt-6.1-sol")


def test_antigravity_starts_on_its_own_sign_in_as_t3_code_starts_it() -> None:
    service = {"PATH": "/usr/bin", "HOME": "/srv/me", "GEMINI_API_KEY": "key", "google_cloud_project": "p",
               "GEMINI_HOME": "/elsewhere", "BROWSER": "firefox"}
    env = agent_env("gemini", service)
    assert {key: value for key, value in env.items() if key != "PATH"} == {
        "HOME": "/srv/me", "SSH_CONNECTION": "127.0.0.1 0 127.0.0.1 0", "AGY_ACP_FORCE_FILE_STORAGE": "1",
        "AGY_CLI_DISABLE_AUTO_UPDATE": "1", "PYTHONUNBUFFERED": "1", "ELECTRON_RUN_AS_NODE": "1"}
    assert env["PATH"].endswith(os.pathsep + "/usr/bin")
    over_ssh = {**service, "SSH_CONNECTION": "10.0.0.2 5 10.0.0.1 22"}
    assert agent_env("gemini", over_ssh)["SSH_CONNECTION"] == "10.0.0.2 5 10.0.0.1 22"
    # Grok keeps the service's environment.
    assert agent_env("grok", service)["GEMINI_API_KEY"] == "key"


def test_a_session_streams_its_turn_into_the_log(gemini: AcpAgent, tmp_path: Path, db_path: Path) -> None:
    sid = start(gemini, tmp_path, "Say hello", "default")
    wait_until(lambda: idle(gemini, sid))
    session = gemini.lookup(sid)
    # The words after the tool call are the turn's last message, which the session shows; the reply holds both.
    assert (session.provider, session.state, session.detail) == ("gemini", "done", "from the fake")
    started = received(tmp_path)[0]
    assert (started["args"], started["cwd"]) == (["acp"], str(tmp_path))
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
    sid = start(gemini, tmp_path, "[[ASK]] run the tests", "default")
    wait_until(lambda: gemini.requests())
    [request] = gemini.requests()
    assert request["method"] == PERMISSION_METHOD and request["provider"] == "gemini"
    assert request["params"] == {"threadId": sid, "reason": "Run make test", "cwd": str(tmp_path), "command": "make test",
                                 "shell": True, "paths": [], "diffs": [], "outsideSandbox": False,
                                 "availableDecisions": ["accept", "decline", "cancel"]}
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
    sid = start(gemini, tmp_path, "[[EDIT]] the notes", "default")
    wait_until(lambda: gemini.requests())
    [request] = gemini.requests()
    params = request["params"]
    assert (params["command"], params["shell"], params["paths"], params["availableDecisions"]) == (
        "", False, ["/work/notes.txt"], ["accept", "decline", "cancel"])
    assert params["diffs"] == [{"path": "/work/notes.txt",
                                "diff": "--- /work/notes.txt\n+++ /work/notes.txt\n@@ -1 +1 @@\n-draft\n+final\n"}]
    gemini.answer(request["id"], {"decision": "accept"})
    wait_until(lambda: idle(gemini, sid))
    assert gemini.lookup(sid).detail == "You picked yes."


def agy_edit(path: str, target: str, replacement: str, **more: str) -> dict:
    """An Antigravity file edit as its permission request sends it."""
    return {"kind": "edit", "title": f"File {Path(path).name}", "locations": [{"path": path}],
            "rawInput": {"Path": path, "Replacement": replacement, "Target": target, **more},
            "content": [{"type": "diff", "path": path, "oldText": target, "newText": replacement}]}


def test_an_antigravity_edit_shows_its_diff_with_the_lines_around_it(tmp_path: Path) -> None:
    notes = tmp_path / "notes.txt"
    notes.write_text("one\ntwo\ncolour: red\nfour\n")
    call = agy_edit(str(notes), "colour: red", "colour: blue")
    # The diff shows everything the edit asks, so the card shows no JSON.
    assert (acp_agent._command(call), acp_agent._reason(call, "")) == ("", "Edit notes.txt")
    assert acp_agent._diffs(call, str(tmp_path)) == [{"path": str(notes), "diff": (
        f"--- {notes}\n+++ {notes}\n@@ -1,4 +1,4 @@\n one\n two\n-colour: red\n+colour: blue\n four\n")}]
    # Text the file holds twice, or not at all, shows as the call sends it, each line on its own.
    notes.write_text("colour: red\ncolour: red\n")
    assert acp_agent._diffs(call, str(tmp_path))[0]["diff"] == f"--- {notes}\n+++ {notes}\n@@ -1 +1 @@\n-colour: red\n+colour: blue\n"
    # A field the diff does not show keeps the whole input on the card, since Allow approves all of it.
    extra = agy_edit(str(notes), "colour: red", "colour: blue", AllowMultiple="true")
    assert json.loads(acp_agent._command(extra)) == extra["rawInput"]
    # So does an input the diff does not match.
    other = agy_edit(str(notes), "colour: red", "colour: blue")
    other["content"][0]["newText"] = "colour: green"
    assert json.loads(acp_agent._command(other)) == other["rawInput"]
    # And an edit that names no text to replace.
    blank = agy_edit(str(notes), None, "colour: blue")
    assert json.loads(acp_agent._command(blank)) == blank["rawInput"]


def test_an_antigravity_write_shows_what_it_overwrites(tmp_path: Path) -> None:
    hello = tmp_path / "hello.py"
    call = {"kind": "edit", "title": "File hello.py", "rawInput": {"Content": "print('hi')\n", "Path": str(hello)},
            "content": [{"type": "diff", "path": str(hello), "newText": "print('hi')\n"}]}
    assert (acp_agent._command(call), acp_agent._reason(call, "")) == ("", "Write hello.py")
    assert acp_agent._diffs(call, str(tmp_path))[0]["diff"] == f"--- /dev/null\n+++ {hello}\n@@ -0,0 +1 @@\n+print('hi')\n"
    hello.write_text("print('hello')\n")
    assert acp_agent._diffs(call, str(tmp_path))[0]["diff"] == f"--- {hello}\n+++ {hello}\n@@ -1 +1 @@\n-print('hello')\n+print('hi')\n"
    # A change of line endings alone shows.
    hello.write_bytes(b"print('hi')\r\n")
    assert acp_agent._diffs(call, str(tmp_path))[0]["diff"] == f"--- {hello}\n+++ {hello}\n@@ -1 +1 @@\n-print('hi')\r\n+print('hi')\n"
    # A file it cannot read as text shows the new content alone, not as a new file.
    hello.write_bytes(b"\xff\xfe")
    assert acp_agent._diffs(call, str(tmp_path))[0]["diff"] == f"--- {hello}\n+++ {hello}\n@@ -0,0 +1 @@\n+print('hi')\n"
    # Nor does Cockpit read what is not a regular file, such as a pipe, which would wait for a writer.
    pipe = tmp_path / "pipe"
    os.mkfifo(pipe)
    write = {**call, "rawInput": {"Content": "x\n", "Path": str(pipe)},
             "content": [{"type": "diff", "path": str(pipe), "newText": "x\n"}]}
    assert acp_agent._diffs(write, str(tmp_path))[0]["diff"] == f"--- {pipe}\n+++ {pipe}\n@@ -0,0 +1 @@\n+x\n"
    # Or a folder, and either leaves no file open.
    opened = len(os.listdir("/proc/self/fd"))
    assert acp_agent._whole(str(tmp_path), None, "x\n", str(tmp_path)) is None
    assert len(os.listdir("/proc/self/fd")) == opened
    # Content that is not text keeps the whole input on the card.
    empty = {**call, "rawInput": {"Content": None, "Path": str(hello)}, "content": [{"type": "diff", "path": str(hello)}]}
    assert json.loads(acp_agent._command(empty)) == empty["rawInput"]


def test_an_edit_card_reads_no_file_outside_the_session_folder(tmp_path: Path) -> None:
    work, secret = tmp_path / "work", tmp_path / "id_ed25519"
    work.mkdir()
    secret.write_text("PRIVATE KEY\n")
    (work / "link").symlink_to(secret)
    # A file outside the folder, by its own path, a link inside the folder or a `..` out of it, shows the change as
    # the call sends it, never what the file holds.
    for path in (str(secret), str(work / "link"), f"{work}/../id_ed25519"):
        write = {"rawInput": {"Content": "x\n", "Path": path}, "content": [{"type": "diff", "path": path, "newText": "x\n"}]}
        assert acp_agent._diffs(write, str(work))[0]["diff"] == f"--- {path}\n+++ {path}\n@@ -0,0 +1 @@\n+x\n"
        edit = agy_edit(path, "KEY", "LOCK")
        assert acp_agent._diffs(edit, str(work))[0]["diff"] == f"--- {path}\n+++ {path}\n@@ -1 +1 @@\n-KEY\n+LOCK\n"
        assert "PRIVATE" not in acp_agent._command(edit)
    # A file inside the folder still shows its whole diff, also one below a folder of it.
    (work / "docs").mkdir()
    for notes in (work / "notes.txt", work / "docs" / "notes.txt"):
        notes.write_text("one\ntwo\n")
        assert acp_agent._diffs(agy_edit(str(notes), "two", "three"), str(work))[0]["diff"] == (
            f"--- {notes}\n+++ {notes}\n@@ -1,2 +1,2 @@\n one\n-two\n+three\n")


def test_an_edit_card_reads_nothing_through_a_folder_swapped_for_a_link(tmp_path: Path,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    work, outside = tmp_path / "work", tmp_path / "outside"
    work.mkdir()
    outside.mkdir()
    (outside / "id_ed25519").write_text("PRIVATE KEY\n")
    (work / "keys").symlink_to(outside)
    path = str(work / "keys" / "id_ed25519")
    # As though the link came after the check resolved the path: the check sees a path inside the folder, and the
    # read meets the link.
    monkeypatch.setattr(acp_agent.os.path, "realpath", lambda each: each)
    assert acp_agent._whole(path, "KEY", "LOCK", str(work)) is None


def test_an_edit_card_reads_nothing_through_a_session_folder_swapped_for_a_link(tmp_path: Path,
                                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path(os.path.realpath(tmp_path))
    work, outside = root / "work", root / "outside"
    outside.mkdir()
    (outside / "id_ed25519").write_text("PRIVATE KEY\n")
    work.symlink_to(outside)
    monkeypatch.setattr(acp_agent.os.path, "realpath", lambda each: each)
    assert acp_agent._whole(str(work / "id_ed25519"), "KEY", "LOCK", str(work)) is None


def test_an_edit_card_reads_nothing_at_the_session_folder_itself(tmp_path: Path) -> None:
    work = Path(os.path.realpath(tmp_path)) / "work"
    work.write_text("PRIVATE KEY\n")
    assert acp_agent._whole(str(work), "KEY", "LOCK", str(work)) is None


def test_an_edit_card_path_with_a_nul_byte_shows_the_change_as_sent(tmp_path: Path) -> None:
    assert acp_agent._whole(f"{tmp_path}/a\0b", "KEY", "LOCK", str(tmp_path)) is None


def test_a_write_too_long_for_its_diff_keeps_its_input_on_the_card(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = start(gemini, tmp_path, "[[BIG WRITE]] the file", "default")
    wait_until(lambda: gemini.requests())
    [request] = gemini.requests()
    params = request["params"]
    assert params["reason"] == "Write big.txt"
    assert "more characters of this diff not shown" in params["diffs"][0]["diff"]
    assert json.loads(params["command"]) == {"Content": "x" * 30000, "Path": "/work/big.txt"}
    gemini.answer(request["id"], {"decision": "decline"})
    wait_until(lambda: idle(gemini, sid))


def test_allow_never_falls_back_to_allow_always(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = start(gemini, tmp_path, "[[ALWAYS]] run the tests", "default")
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
    sid = start(gemini, tmp_path, "[[VAGUE]] do it", "default")
    wait_until(lambda: gemini.requests())
    [request] = gemini.requests()
    assert request["params"]["availableDecisions"] == ["decline", "cancel"]
    with pytest.raises(AcpError):
        gemini.answer(request["id"], {"decision": "accept"})
    gemini.answer(request["id"], {"decision": "cancel"})
    wait_until(lambda: idle(gemini, sid))
    assert gemini.lookup(sid).detail == "You picked cancelled."


def test_deny_picks_the_agents_reject_option(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = start(gemini, tmp_path, "[[ASK]] run the tests", "default")
    wait_until(lambda: gemini.requests())
    gemini.answer(gemini.requests()[0]["id"], {"decision": "decline"})
    wait_until(lambda: idle(gemini, sid))
    assert gemini.lookup(sid).detail == "You picked no."


def test_a_cancelled_turn_ends(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = start(gemini, tmp_path, "[[WAIT]] for ever", "default")
    assert gemini.lookup(sid).working_now
    gemini.stop(sid)
    assert not gemini.lookup(sid).working_now
    assert gemini.lookup(sid).detail == "Gemini stopped: cancelled"


def test_stop_cancels_an_open_request_and_no_answer_approves_it_after(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = start(gemini, tmp_path, "[[ASK]] run the tests", "default")
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
    sid = start(gemini, tmp_path, "[[WAIT]] for ever", "default")
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
    a = start(gemini, first, "Say hello", "default")
    b = start(gemini, second, "Say hello", "default")
    c = start(gemini, first, "Say hello", "default")
    wait_until(lambda: idle(gemini, a) and idle(gemini, b) and idle(gemini, c))
    assert sorted(gemini._connections) == [("agy acp", str(first)), ("agy acp", str(second))]
    assert [each["cwd"] for each in received(tmp_path) if "cwd" in each and "args" in each] == [str(first), str(second)]
    assert gemini._loaded[a] is gemini._loaded[c] is not gemini._loaded[b]


def test_concurrent_starts_wait_for_the_agent_to_initialize(gemini: AcpAgent, tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_ACP_SLOW_START", "0.5")
    got: list = []
    threads = [threading.Thread(target=lambda: got.append(gemini._connection("default", tmp_path))) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len({id(each) for each in got}) == 1 and got[0].capabilities == {"loadSession": True}
    assert len([each for each in received(tmp_path) if "args" in each]) == 1


def test_sessions_start_and_load_in_auto_mode(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = start(gemini, tmp_path, "Say hello", "default")
    wait_until(lambda: idle(gemini, sid))
    gemini._connections[("agy acp", str(tmp_path))].close()
    wait_until(lambda: sid not in gemini._loaded)
    gemini.send(sid, "Say hello again", restart=False)
    wait_until(lambda: idle(gemini, sid))
    # A loaded session starts in Antigravity's default mode again, so auto goes after the load too, before the prompt.
    sent = [(each["method"], each["params"]) for each in received(tmp_path)
            if each.get("method") in ("session/new", "session/load", "session/set_mode", "session/prompt")]
    auto = ("session/set_mode", {"sessionId": sid, "modeId": "auto"})
    assert [each[0] for each in sent] == ["session/new", "session/set_mode", "session/prompt",
                                          "session/load", "session/set_mode", "session/prompt"]
    assert sent[0][1] == {"cwd": str(tmp_path), "mcpServers": []}
    assert sent[3][1] == {"sessionId": sid, "cwd": str(tmp_path), "mcpServers": []}
    assert sent[1] == sent[4] == auto


def test_an_agent_without_auto_mode_stays_in_its_mode_and_logs_an_error(
        gemini: AcpAgent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv("FAKE_ACP_NO_AUTO", "1")
    with caplog.at_level(logging.ERROR, logger="cockpit.acp_agent"):
        sid = start(gemini, tmp_path, "Say hello", "default")
        wait_until(lambda: idle(gemini, sid))
    assert gemini.lookup(sid).state == "done"
    assert not any(each.get("method") == "session/set_mode" for each in received(tmp_path))
    assert any("offers no auto mode" in each.getMessage() for each in caplog.records)


def test_a_restarted_agent_loads_the_session_without_logging_its_replay(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = start(gemini, tmp_path, "Say hello", "default")
    wait_until(lambda: idle(gemini, sid))
    gemini._connections[("agy acp", str(tmp_path))].close()
    wait_until(lambda: sid not in gemini._loaded)
    gemini.send(sid, "Say hello again", restart=False)
    wait_until(lambda: idle(gemini, sid))
    texts = [part["text"] for part in gemini.log(sid)["parts"] if part["kind"] in ("instruction", "says")]
    assert texts == ["Say hello", "Hello ", "from the fake", "Say hello again", "Hello ", "from the fake"]


def test_an_agent_that_ends_mid_turn_fails_the_turn(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = start(gemini, tmp_path, "[[WAIT]] for ever", "default")
    gemini._connections[("agy acp", str(tmp_path))].close()
    wait_until(lambda: idle(gemini, sid))
    session = gemini.lookup(sid)
    assert session.state == "failed" and "ended" in session.detail


def test_a_server_restart_fails_the_turns_it_cut_off(db_path: Path) -> None:
    conn = db.connect(db_path)
    try:
        for sid, provider, state in (("cut", "gemini", "working"), ("over", "gemini", "done"), ("grok", "grok", "working")):
            conn.execute("INSERT INTO acp_sessions (session_id, provider, name, cwd, model, state, updated_at)"
                         " VALUES (?, ?, 'x', '/tmp', 'default', ?, ?)", (sid, provider, state, db.now_iso()))
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
    sid = start(gemini, tmp_path, "Say hello", "default")
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


@pytest.mark.parametrize("model", [None, "default"])
def test_a_message_starts_a_gemini_session_through_delivery(world, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                            model: str | None) -> None:
    setup = config.setup()
    monkeypatch.setattr(config, "setup", lambda: dataclasses.replace(setup, providers=("claude", "gemini")))
    monkeypatch.setenv("FAKE_ACP_ARGS", str(tmp_path / "args.jsonl"))
    put_agents_on_path(tmp_path / "bin", monkeypatch)
    agent = AcpAgent(world.db_path, "gemini")
    world.deliverer.codex = Agents(None, {"gemini": agent})
    world.deliverer.enabled_providers = ("claude", "gemini")
    try:
        target = {"new_cwd": str(world.checkout), "provider": "gemini"}
        if model is not None:
            target["model"] = model
        response = world.client.post("/api/messages", json={"text": "Say hello", "target": target},
                                     headers=world.user())
        assert response.status_code == 200, response.text
        sent = world.client.get(f"/api/messages/{response.json()['id']}").json()
        assert (sent["state"], sent["via"], sent["detail"]) == ("sent", "new", "started a Gemini session")
        sid = sent["session_id"]
        wait_until(lambda: idle(agent, sid))
        calls = [each for each in received(tmp_path)
                 if each.get("method") in ("session/set_config_option", "session/prompt")]
        if model is None:
            choose_model = calls.pop(0)
            assert choose_model["method"] == "session/set_config_option"
            assert choose_model["params"] == {"sessionId": sid, "configId": "model", "value": "gemini-4-neon"}
        assert [each["method"] for each in calls] == ["session/prompt"]
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
    sid = start(gemini, tmp_path, "[[SHELL]] run the tests", "default")
    wait_until(lambda: gemini.requests())
    [request] = gemini.requests()
    # The title is the command again, so the card says it once.
    assert (request["params"]["command"], request["params"]["reason"], request["params"]["availableDecisions"]) == (
        "make test TOKEN=s3cret", "", ["accept", "decline", "cancel"])
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
    sid = start(gemini, tmp_path, "[[ASK]] run the tests", "default")
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
    assert {"gemini.requested", "gemini.answered", "gemini.auto_allowed", "grok.requested", "grok.answered",
            "grok.auto_allowed"} <= set(sweeper.QUIET_KINDS)


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
                     " VALUES (?, 'gemini', 'g', '/tmp', 'default', 'done', ?)", (SESSION_A, db.now_iso()))
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
    assert (row["model"], row["settings"]["provider"], row["settings"]["model"]) == ("default", "gemini", "default")


def test_an_antigravity_shell_call_shows_its_command_line_and_summary(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = start(gemini, tmp_path, "[[NESTED]] run the tests", "default")
    wait_until(lambda: gemini.requests())
    [request] = gemini.requests()
    params = request["params"]
    # The call runs in its own folder, not the session's, so the card names that one.
    assert (params["command"], params["reason"], params["cwd"], params["shell"], params["availableDecisions"]) == (
        "git status && make test", "Check the tree and run the tests", "/srv/other", True,
        ["accept", "decline", "cancel"])
    gemini.answer(request["id"], {"decision": "accept"})
    wait_until(lambda: idle(gemini, sid))
    assert gemini.lookup(sid).detail == "You picked yes."


def test_only_a_nested_command_line_unpacks() -> None:
    def call(command: str) -> dict:
        return {"kind": "execute", "title": "t", "rawInput": {"command": command}}

    assert acp_agent._command(call('{"CommandLine": "ls", "toolSummary": "List"}')) == "ls"
    assert acp_agent._reason(call('{"CommandLine": "ls", "toolSummary": "  "}'), "ls") == "t"
    assert acp_agent._reason(call('{"CommandLine": "ls", "explanation": "List the folder"}'), "ls") == "List the folder"
    # A field the card does not show keeps the whole input, since Allow approves all of it.
    for other in ('{"Path": "/x"}', '{"CommandLine": 3}', "{not json", '{"a": 1} && ls',
                  '{"CommandLine": "ls", "Env": {"A": "1"}}', '{"CommandLine": "ls", "Cwd": 3}'):
        assert acp_agent._command(call(other)) == other


def test_a_shell_call_runs_in_its_own_folder_also_when_its_input_shows_whole() -> None:
    def call(command: str) -> dict:
        return {"kind": "execute", "title": "t", "rawInput": {"command": command}}

    assert acp_agent._cwd(call('{"CommandLine": "pwd", "Cwd": "/tmp"}'), "/home/sam") == "/tmp"
    # An extra field keeps the whole input on the card; the folder line still names the call's own folder.
    extra = call('{"CommandLine": "pwd", "Cwd": "/tmp", "Env": {"A": "1"}}')
    assert acp_agent._command(extra) == extra["rawInput"]["command"]
    assert acp_agent._cwd(extra, "/home/sam") == "/tmp"
    for other in ('{"CommandLine": "pwd"}', '{"CommandLine": "pwd", "Cwd": 3}', '{"CommandLine": "pwd", "Cwd": ""}',
                  '{"Cwd": "/tmp"}', "pwd", "{not json"):
        assert acp_agent._cwd(call(other), "/home/sam") == "/home/sam"


def test_an_antigravity_shell_call_with_its_input_at_the_top_shows_its_command_line(gemini: AcpAgent,
                                                                                     tmp_path: Path) -> None:
    sid = start(gemini, tmp_path, "[[FLAT]] run the tests", "default")
    wait_until(lambda: gemini.requests())
    [request] = gemini.requests()
    params = request["params"]
    assert (params["command"], params["reason"], params["cwd"], params["shell"]) == (
        "make test", "Run the tests", str(tmp_path), True)
    gemini.answer(request["id"], {"decision": "accept"})
    wait_until(lambda: idle(gemini, sid))
    assert params["outsideSandbox"] is False
    flat = {"kind": "execute", "title": "t", "rawInput": {"CommandLine": "pwd", "Cwd": "/tmp", "Env": {"A": "1"}}}
    # A field the card does not show keeps the whole input, as in the nested shape; the folder is still the call's.
    assert acp_agent._command(flat) == json.dumps(flat["rawInput"])
    assert acp_agent._cwd(flat, "/home/sam") == "/tmp"


def test_a_shell_call_auto_mode_sends_outside_the_sandbox_says_so(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = start(gemini, tmp_path, "[[BYPASS]] clean up", "default")
    wait_until(lambda: gemini.requests())
    [request] = gemini.requests()
    params = request["params"]
    assert (params["command"], params["reason"], params["shell"], params["outsideSandbox"]) == (
        "rm -rf /srv/old", "Delete the old folder", True, True)
    gemini.answer(request["id"], {"decision": "decline"})
    wait_until(lambda: idle(gemini, sid))
    # A BypassSandbox that is not true or false keeps the whole input on the card.
    odd = {"kind": "execute", "title": "t", "rawInput": {"CommandLine": "ls", "BypassSandbox": "yes"}}
    assert acp_agent._command(odd) == json.dumps(odd["rawInput"])
    assert acp_agent._outside_sandbox(odd) is False
    # A field the card does not unpack keeps the whole input, and the card still says the call leaves the sandbox,
    # whether the input comes flat or as JSON inside `command`.
    extra = {"CommandLine": "make clean", "BypassSandbox": True, "Env": {"LC_ALL": "C"}}
    for raw in (extra, {"command": json.dumps(extra)}):
        wide = {"kind": "execute", "title": "t", "rawInput": raw}
        assert acp_agent._nested(wide) == {}
        assert acp_agent._outside_sandbox(wide) is True
    assert acp_agent._outside_sandbox({"kind": "execute", "title": "t", "rawInput": {"command": "ls"}}) is False
    no_command = {"kind": "execute", "title": "t", "rawInput": {"command": json.dumps({"BypassSandbox": True})}}
    assert acp_agent._outside_sandbox(no_command) is False


def test_a_shell_call_that_only_reads_runs_without_a_card(gemini: AcpAgent, tmp_path: Path, db_path: Path) -> None:
    sid = start(gemini, tmp_path, "[[SAFE]] look around", "default")
    wait_until(lambda: idle(gemini, sid))
    assert gemini.requests() == []
    assert gemini.lookup(sid).detail == "You picked yes."
    conn = db.connect(db_path)
    try:
        [data] = [json.loads(row["data"]) for row in
                  conn.execute("SELECT data FROM events WHERE kind = 'gemini.auto_allowed' ORDER BY id")]
        asked = conn.execute("SELECT count(*) FROM events WHERE kind = 'gemini.requested'").fetchone()[0]
    finally:
        conn.close()
    # The event holds no command, as a card's events do not.
    assert (data, asked) == ({"session_id": sid, "tool": "execute"}, 0)


def test_a_shell_call_that_only_reads_outside_the_sandbox_shows_its_card(gemini: AcpAgent, tmp_path: Path,
                                                                          db_path: Path) -> None:
    sid = start(gemini, tmp_path, "[[SAFE OUTSIDE]] look around", "default")
    wait_until(lambda: gemini.requests())
    [request] = gemini.requests()
    params = request["params"]
    assert safe_shell.safe(params["command"], params["cwd"], str(config.HOME))
    assert (params["command"], params["outsideSandbox"]) == ("cockpit get | head -n 120", True)
    gemini.answer(request["id"], {"decision": "decline"})
    wait_until(lambda: idle(gemini, sid))
    conn = db.connect(db_path)
    try:
        allowed = conn.execute("SELECT count(*) FROM events WHERE kind = 'gemini.auto_allowed'").fetchone()[0]
    finally:
        conn.close()
    assert allowed == 0


@pytest.mark.parametrize("command", [
    "cockpit get | head -n 120", "git status", "git -C /srv/x log --oneline -5", "grep -E 'a|b' src/x.py",
    "grep -rn foo src 2>/dev/null | head", "ls -la 2>&1 | tail -5", "find . -name '*.py'", "sed -n 1,20p a.py",
    "gh pr view 12 --json title", "git branch -a", "cd /srv && ls", "sort -nr x", "ls 1>&2", "jq .a x.json",
    "jq .important imports.json", "grep '[ab]' x",
])
def test_reading_commands_are_safe(command: str) -> None:
    assert safe_shell.safe(command, "/home/sam/dev", "/home/sam")


@pytest.mark.parametrize("command", [
    "", "git status && make test", "git diff --output=x", "git diff --no-index /a /b", "cat foo#bar; rm x",
    "echo hi # ; rm -rf /", "echo x > file", "echo x >> file", "echo a 2> f", "ls &> f", "ls >&f", "cat < file",
    "cat <(id)", "cat ~/.ssh/id_rsa", "cat .env", "sleep 1 &", "echo $HOME", "echo `id`", "echo a\nrm b",
    "sort -o out in", "sort -uo out in", "find . -name x -exec rm {} ;", "sed -i s/a/b/ a.py", "gh pr comment 12",
    "gh api repos/x", "git branch new", "git push", "python3 -c 1", "awk '{print}' x", "xargs rm", "rg --pre cat x",
    "uniq a b", "git status; ", "cockpit spec get --out x", "cat \"unclosed", "A=1 ls",
    # A quoted separator is an argument, so its options are still the part's own.
    "sort x \";\" cat -o y", "sort --outp=x in", "uniq -c - -out", "git log --out=x", "file -C -m x",
    "rg --hostname-bin=x foo", "rg -z foo", "jq -n env", "grep -R x .",
    # A glob, the home folder or a hidden folder in it can reach credentials under another spelling.
    "cat ~/.ss?/id_*", "grep -r BEGIN ~", "cd ~ && grep -r x", "cat ~/.gemini/settings.json", "cat /proc/self/environ",
    "ls ~other", "echo {a,b}", "cd de?o && ls",
    # A stream number apart from its redirect, or quoted, is an argument: here uniq's output file.
    "uniq 1 >/dev/null /dev/null", "uniq x \"1\">/dev/null",
    "jq -n -f a.jq", "jq -nL. x", "jq 'include \"a\"; .' x", "jq -n 'env.HOME'", "ls -[[:alpha:]]*",
])
def test_commands_that_could_write_run_or_reach_secrets_still_ask(command: str) -> None:
    assert not safe_shell.safe(command, "/home/sam/dev", "/home/sam")


def test_a_command_in_a_credential_folder_still_asks() -> None:
    assert not safe_shell.safe("ls", "/home/sam/.ssh", "/home/sam")


def test_a_link_into_a_hidden_home_folder_still_asks(tmp_path: Path) -> None:
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "token").write_text("x")
    work = tmp_path / "work"
    work.mkdir()
    (work / "plain").write_text("x")
    (work / "link").symlink_to(tmp_path / ".hidden" / "token")
    home = str(tmp_path)
    assert safe_shell.safe("cat plain", str(work), home)
    assert not safe_shell.safe("cat link", str(work), home)
    assert not safe_shell.safe("cat l*", str(work), home)


def test_a_glob_that_matches_an_option_still_asks(tmp_path: Path) -> None:
    (tmp_path / "--hostname-bin=x").write_text("x")
    assert not safe_shell.safe("rg --host* foo", str(tmp_path), "/home/sam")


def _folder_with_env(tmp_path: Path) -> str:
    work = tmp_path / "work"
    (work / "src").mkdir(parents=True)
    for name in (".env", "a.txt", "b.txt", "src/x.py", "src/gateway_oauth.py"):
        (work / name).write_text("x")
    return str(work)


def test_a_glob_that_uniq_could_write_through_still_asks(tmp_path: Path) -> None:
    work = _folder_with_env(tmp_path)
    assert safe_shell.safe("uniq a.txt", work, str(tmp_path / "home"))
    # Bash expands it to `uniq a.txt b.txt`, which writes b.txt.
    assert not safe_shell.safe("uniq *.txt", work, str(tmp_path / "home"))


@pytest.mark.parametrize("command", [
    "grep -r SECRET .", "grep --recursive SECRET", "grep -d recurse SECRET .", "grep --directories=recurse x",
    "grep -r src", "grep -rn -A 3 foo", "grep -r -e foo", "diff -r . /tmp", "diff . src", "cd src && grep -r x ..",
])
def test_a_recursive_read_of_a_folder_holding_a_credential_still_asks(command: str, tmp_path: Path) -> None:
    assert not safe_shell.safe(command, _folder_with_env(tmp_path), str(tmp_path / "home"))


@pytest.mark.parametrize("command", [
    "grep -rn foo src", "grep -rn -A 3 foo src", "grep -r -e foo src", "diff a.txt b.txt", "grep -r foo -- src",
])
def test_a_recursive_read_that_stays_clear_of_credentials_is_safe(command: str, tmp_path: Path) -> None:
    # A source file's name, such as gateway_oauth.py, is no credential.
    assert safe_shell.safe(command, _folder_with_env(tmp_path), str(tmp_path / "home"))


needs_rg = pytest.mark.skipif(shutil.which("rg") is None, reason="rg is not installed")


@needs_rg
@pytest.mark.parametrize("command", [
    "rg --hidden SECRET", "rg -uu SECRET", "rg -. SECRET", "rg --unrestricted --unrestricted x", "rg -l --hidden x",
    "rg -lq. x", "rg -g '.en?' SECRET", "rg --type-add 'c:.e*' -tc SECRET", "rg --glob --count --glob '*' SECRET",
    "rg --iglob -c --iglob '*' SECRET", "rg --hidden --path-separator x SECRET",
])
def test_rg_that_would_read_a_credential_still_asks(command: str, tmp_path: Path) -> None:
    assert not safe_shell.safe(command, _folder_with_env(tmp_path), str(tmp_path / "home"))


@needs_rg
@pytest.mark.parametrize("command", [
    "rg foo", "rg -n foo src", "rg -l foo", "rg -c foo src", "rg --json foo", "rg -nA3 foo", "rg -e foo -g '*.py'",
    "rg -u foo", "rg foo *",
])
def test_rg_that_leaves_credentials_out_is_safe(command: str, tmp_path: Path) -> None:
    # rg leaves hidden files such as .env out unless told otherwise; -c, -l and --json only change what it prints.
    assert safe_shell.safe(command, _folder_with_env(tmp_path), str(tmp_path / "home"))


@needs_rg
def test_rg_asks_when_an_ignore_file_names_a_credential_back_in(tmp_path: Path) -> None:
    home = str(tmp_path / "home")
    work = _folder_with_env(tmp_path)
    Path(work, "ignored").write_text("!.env\n")
    assert safe_shell.safe("rg SECRET", work, home)
    assert not safe_shell.safe("rg --ignore-file ignored SECRET", work, home)
    subprocess.run(["git", "init", "-q", work], check=True)
    Path(work, ".git", "info", "exclude").write_text("!.env\n")
    assert not safe_shell.safe("rg SECRET", work, home)
    Path(work, ".git", "info", "exclude").write_text("")
    Path(work, ".ignore").write_text("!.env\n")
    assert not safe_shell.safe("rg SECRET", work, home)


@needs_rg
def test_rg_with_a_glob_skips_an_ignored_hidden_folder(tmp_path: Path) -> None:
    work = _folder_with_env(tmp_path)
    Path(work, ".venv", "lib").mkdir(parents=True)
    Path(work, ".venv", "lib", "cacert.pem").write_text("x")
    Path(work, ".gitignore").write_text(".venv\n")
    assert safe_shell.safe("rg -g '*.py' foo", work, str(tmp_path / "home"))


def test_the_edit_card_shows_a_change_to_the_final_newline(tmp_path: Path) -> None:
    notes = tmp_path / "f.txt"
    notes.write_text("hello\n")
    write = {"rawInput": {"Content": "hello", "Path": str(notes)},
             "content": [{"type": "diff", "path": str(notes), "newText": "hello"}]}
    assert acp_agent._diffs(write, str(tmp_path))[0]["diff"] == (
        f"--- {notes}\n+++ {notes}\n@@ -1 +1 @@\n-hello\n+hello\n\\ No newline at end of file\n")
    # Lines end at a newline alone, so a form feed inside the file gets no marker.
    notes.write_text("a\n\x0c\nb\n")
    assert acp_agent._diffs(agy_edit(str(notes), "b", "B"), str(tmp_path))[0]["diff"] == (
        f"--- {notes}\n+++ {notes}\n@@ -1,3 +1,3 @@\n a\n \x0c\n-b\n+B\n")


def test_rg_asks_when_it_is_not_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert not safe_shell.safe("rg foo", _folder_with_env(tmp_path), str(tmp_path / "home"))


def test_a_recursive_read_below_a_link_to_a_credential_or_of_a_large_folder_still_asks(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "key").write_text("x")
    work = tmp_path / "work"
    (work / "src").mkdir(parents=True)
    (work / "src" / "keys").symlink_to(home / ".ssh")
    assert not safe_shell.safe("grep -r foo src", str(work), str(home))
    big = tmp_path / "big"
    big.mkdir()
    for index in range(safe_shell._TREE_MAX + 1):
        (big / f"f{index}").touch()
    assert not safe_shell.safe("grep -r foo .", str(big), str(home))


def test_a_recursive_read_that_reaches_a_credential_another_way_still_asks(tmp_path: Path) -> None:
    home = str(tmp_path / "home")
    work = _folder_with_env(tmp_path)
    # A folder given after `=`.
    assert not safe_shell.safe("diff -r --from-file=. src", work, home)
    # diff -r follows a link to a folder that holds a credential.
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / ".env").write_text("x")
    for side in ("left", "right"):
        (tmp_path / "pair" / side).mkdir(parents=True)
        (tmp_path / "pair" / side / "link").symlink_to(tmp_path / "outside")
    assert not safe_shell.safe("diff -r left right", str(tmp_path / "pair"), home)
    # bash's cd takes `..` off `link/..` before following the link.
    (tmp_path / "start" / "project" / "sub").mkdir(parents=True)
    (tmp_path / "start" / "project" / "sub" / ".env").write_text("x")
    (tmp_path / "deep" / "elsewhere").mkdir(parents=True)
    (tmp_path / "start" / "link").symlink_to(tmp_path / "deep" / "elsewhere")
    assert not safe_shell.safe("cd link/../project && grep -r SECRET sub", str(tmp_path / "start"), home)
    # A folder named like a source file is still a credential's folder.
    (tmp_path / "named" / "credentials.py").mkdir(parents=True)
    (tmp_path / "named" / "credentials.py" / "data").write_text("x")
    assert not safe_shell.safe("grep -r foo .", str(tmp_path / "named"), home)


def test_git_grep_asks_when_a_tracked_file_holds_a_credentials_name(tmp_path: Path) -> None:
    home = str(tmp_path / "home")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.txt").write_text("x")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
    assert safe_shell.safe("git grep foo", str(repo), home)
    assert not safe_shell.safe("git grep --untracked foo", str(repo), home)
    (repo / ".env").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", ".env"], check=True)
    assert not safe_shell.safe("git grep foo", str(repo), home)
    assert not safe_shell.safe("git -C repo grep foo", str(tmp_path), home)


def test_git_grep_runs_no_program_the_repository_config_names(tmp_path: Path) -> None:
    repo, marker = tmp_path / "repo", tmp_path / "ran"
    repo.mkdir()
    (repo / "a.txt").write_text("x")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
    script = tmp_path / "fsmonitor.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\n")
    script.chmod(0o755)
    subprocess.run(["git", "-C", str(repo), "config", "core.fsmonitor", str(script)], check=True)
    subprocess.run(["git", "-C", str(repo), "ls-files"], check=True, capture_output=True)
    assert marker.exists()  # git runs it, so the check below means something
    marker.unlink()
    assert safe_shell.safe("git grep x", str(repo), str(tmp_path / "home"))
    assert not marker.exists()


def test_git_grep_from_a_subfolder_checks_the_whole_repository(tmp_path: Path) -> None:
    home = str(tmp_path / "home")
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    (repo / "sub" / "b.txt").write_text("x")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "sub/b.txt"], check=True)
    sub = str(repo / "sub")
    assert safe_shell.safe("git grep foo ..", sub, home)
    assert safe_shell.safe("git grep -e foo b.txt", sub, home)
    # A word that names no file may be a revision, whose files are not listed.
    assert not safe_shell.safe("git grep foo HEAD~1", sub, home)
    assert not safe_shell.safe("git grep -e foo -e -- HEAD~1", sub, home)
    # A `--` before the pattern is skipped by git, so HEAD~1 is a revision there too.
    assert not safe_shell.safe("git grep -n -- foo HEAD~1", sub, home)
    assert safe_shell.safe("git grep -n foo -- b.txt", sub, home)
    (repo / ".env").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", ".env"], check=True)
    for command in ("git grep foo", "git grep foo ..", "git grep foo -- :/", "git grep foo -- ':(top)'"):
        assert not safe_shell.safe(command, sub, home)


def test_a_blank_execute_title_is_no_command() -> None:
    assert acp_agent._command({"kind": "execute", "title": "   "}) == ""
    assert acp_agent._command({"kind": "execute", "title": "ls -la"}) == "ls -la"


def models(db_path: Path) -> list[tuple[str, str]]:
    conn = db.connect(db_path)
    try:
        return db.session_models(conn, "gemini")
    finally:
        conn.close()


def test_each_antigravity_process_gets_its_own_temporary_folder(gemini: AcpAgent, tmp_path: Path) -> None:
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    for folder in (first, second):
        sid = start(gemini, folder, "Say hello", "default")
        wait_until(lambda: idle(gemini, sid))
    temps = [Path(each["tmpdir"]) for each in received(tmp_path) if "args" in each]
    assert len(set(temps)) == 2 and all(each.parent == tmp_path / "agy-tmp" and each.is_dir() for each in temps)
    gemini._connections[("agy acp", str(first))].close()
    wait_until(lambda: not temps[0].exists())
    assert temps[1].is_dir()
    gemini.mark_interrupted()  # a server start clears what a killed server left
    assert list((tmp_path / "agy-tmp").iterdir()) == []


def test_sessions_learn_the_google_accounts_models_and_start_on_the_chosen_one(gemini: AcpAgent, tmp_path: Path,
                                                                               db_path: Path) -> None:
    assert models(db_path) == [("gemini-4-neon", "Gemini 4 Neon"), ("default", "Default")]
    with pytest.raises(AcpError, match="offers no model gemini-3-pro"):
        start(gemini, tmp_path, "Say hello", "gemini-3-pro")
    first = start(gemini, tmp_path, "Say hello", "default")
    wait_until(lambda: idle(gemini, first))
    assert models(db_path) == [("gemini-4-neon", "Gemini 4 Neon"), ("default", "Default"),
                               ("gemini-3-pro", "Gemini 3 Pro"),
                               ("gemini-3-flash", "Gemini 3 Flash"), ("claude-sonnet", "claude-sonnet")]
    second = start(gemini, tmp_path, "Say hello", "gemini-3-pro")
    wait_until(lambda: idle(gemini, second))
    chosen = [each["params"] for each in received(tmp_path) if each.get("method") == "session/set_config_option"]
    assert chosen == [{"sessionId": second, "configId": "model", "value": "gemini-3-pro"}]
    # A restarted agent loads the session and puts it back on its model.
    gemini._connections[("agy acp", str(tmp_path))].close()
    wait_until(lambda: second not in gemini._loaded)
    gemini.send(second, "Say hello again", restart=False)
    wait_until(lambda: idle(gemini, second))
    chosen = [each["params"] for each in received(tmp_path) if each.get("method") == "session/set_config_option"]
    assert chosen[1:] == [{"sessionId": second, "configId": "model", "value": "gemini-3-pro"}]


def test_a_model_the_google_account_no_longer_offers_is_refused(gemini: AcpAgent, tmp_path: Path,
                                                                 db_path: Path) -> None:
    conn = db.connect(db_path)
    try:
        db.kv_set(conn, db.models_key("gemini"), [["gemini-2-ultra", "Gemini 2 Ultra"]])
    finally:
        conn.close()
    with pytest.raises(AcpError, match="'gemini-2-ultra' is unavailable for this Google account"):
        start(gemini, tmp_path, "Say hello", "gemini-2-ultra")
    assert not any(each.get("method") in ("session/set_config_option", "session/prompt") for each in received(tmp_path))
    assert ("gemini-2-ultra", "Gemini 2 Ultra") not in models(db_path)  # the account's answer replaced the old list


def test_a_signed_out_antigravity_says_to_sign_it_in(gemini: AcpAgent, tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_ACP_SIGNED_OUT", "1")
    with pytest.raises(AcpError, match="Gemini is not signed in on this machine"):
        start(gemini, tmp_path, "Say hello", "default")


def test_an_antigravity_question_shows_as_a_question_card_and_takes_its_answer(gemini: AcpAgent, tmp_path: Path,
                                                                             db_path: Path) -> None:
    sid = start(gemini, tmp_path, "[[QUESTION]] pick a file", "default")
    wait_until(lambda: gemini.requests())
    [request] = gemini.requests()
    assert request["method"] == acp_agent.QUESTION_METHOD
    assert request["params"] == {"threadId": sid, "reason": "", "cwd": str(tmp_path), "questions": [{
        "id": "interaction_q1", "header": "", "question": "Which file?", "isOther": False, "isSecret": False,
        "options": [{"label": "notes.txt", "description": ""}, {"label": "todo.txt", "description": ""}]}]}
    for wrong in ({"answers": {"interaction_q1": {"answers": ["readme.txt"]}}}, {"answers": {}},
                  {"answers": {"interaction_q1": {"answers": ["a", "b"]}}}, {"decision": "accept"}):
        with pytest.raises(AcpError):
            gemini.answer(request["id"], wrong)
    assert len(gemini.requests()) == 1
    gemini.answer(request["id"], {"answers": {"interaction_q1": {"answers": ["todo.txt"]}}})  # by its label
    wait_until(lambda: idle(gemini, sid))
    assert gemini.lookup(sid).detail == "You picked b."
    gemini.send(sid, "[[QUESTION]] again", restart=False)
    wait_until(lambda: gemini.requests())
    gemini.answer(gemini.requests()[0]["id"], {"answers": {"interaction_q1": {"answers": ["a"]}}})  # by its id
    wait_until(lambda: idle(gemini, sid))
    assert gemini.lookup(sid).detail == "You picked a."
    gemini.send(sid, "[[QUESTION]] once more", restart=False)
    wait_until(lambda: gemini.requests())
    gemini.answer(gemini.requests()[0]["id"], {"decision": "cancel"})
    wait_until(lambda: idle(gemini, sid))
    assert gemini.lookup(sid).detail == "You picked cancelled."
    conn = db.connect(db_path)
    try:
        answered = [json.loads(row["data"])["decision"] for row in
                    conn.execute("SELECT data FROM events WHERE kind = 'gemini.answered' ORDER BY id")]
    finally:
        conn.close()
    assert answered == ["answer", "answer", "cancel"]


def test_a_question_whose_options_share_an_id_is_cancelled(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = start(gemini, tmp_path, "[[TWINS]] pick a file", "default")
    wait_until(lambda: idle(gemini, sid))
    assert gemini.requests() == [] and gemini.lookup(sid).detail == "You picked cancelled."


def test_a_late_reply_runs_as_a_turn_until_its_usage_update(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = start(gemini, tmp_path, "[[LATER]] run it in the background", "default")
    wait_until(lambda: idle(gemini, sid))
    assert gemini.lookup(sid).detail == "Started it."
    (tmp_path / "go").touch()
    wait_until(lambda: not idle(gemini, sid))
    time.sleep(0.2)
    assert gemini.lookup(sid).working_now  # until the reply's usage update
    (tmp_path / "go-end").touch()
    wait_until(lambda: idle(gemini, sid))
    session = gemini.lookup(sid)
    assert (session.state, session.detail) == ("done", "It finished.")
    assert gemini._late == {}


def test_stop_ends_a_late_reply(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = late_reply(gemini, tmp_path)
    gemini.stop(sid)
    assert idle(gemini, sid) and gemini._late == {}
    assert gemini.lookup(sid).detail == "It finished."
    wait_until(lambda: any(each.get("method") == "session/cancel" for each in received(tmp_path)))


def test_a_late_reply_that_goes_quiet_ends(gemini: AcpAgent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(acp_agent, "LATE_QUIET_SECONDS", 0.3)
    sid = start(gemini, tmp_path, "[[LATER]] run it in the background", "default")
    wait_until(lambda: idle(gemini, sid))
    (tmp_path / "go").touch()
    wait_until(lambda: not idle(gemini, sid))
    wait_until(lambda: idle(gemini, sid))
    assert gemini.lookup(sid).detail == "It finished." and gemini._late == {}


def test_grok_takes_no_late_reply(db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_ACP_ARGS", str(tmp_path / "args.jsonl"))
    put_agents_on_path(tmp_path / "bin", monkeypatch)
    grok = AcpAgent(db_path, "grok")
    try:
        sid = start(grok, tmp_path, "[[LATER]] run it in the background", "default")
        wait_until(lambda: idle(grok, sid))
        (tmp_path / "go").touch()
        time.sleep(0.3)
        assert idle(grok, sid) and grok.lookup(sid).detail == "Started it."
    finally:
        for connection in grok._connections.values():
            connection.close()


def test_a_question_card_answers_by_the_label_it_shows(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = start(gemini, tmp_path, "[[CROSSED]] pick", "default")
    wait_until(lambda: gemini.requests())
    gemini.answer(gemini.requests()[0]["id"], {"answers": {"interaction_q1": {"answers": ["Open"]}}})
    wait_until(lambda: idle(gemini, sid))
    assert gemini.lookup(sid).detail == "You picked a."
    gemini.send(sid, "[[NAMESAKES]] pick", restart=False)
    wait_until(lambda: gemini.requests())
    [request] = gemini.requests()
    assert [each["label"] for each in request["params"]["questions"][0]["options"]] == ["notes.txt (a)", "notes.txt (b)"]
    for wrong in ({"answers": [["notes.txt (b)"]]}, {"answers": {"interaction_q1": {"answers": "b"}}},
                  {"answers": {"interaction_q1": ["b"]}},
                  {"answers": {"interaction_q1": {"answers": ["notes.txt (b)"]}, "other": {"answers": []}}}):
        with pytest.raises(AcpError):
            gemini.answer(request["id"], wrong)
    gemini.answer(request["id"], {"answers": {"interaction_q1": {"answers": ["notes.txt (b)"]}}})
    wait_until(lambda: idle(gemini, sid))
    assert gemini.lookup(sid).detail == "You picked b."


def late_reply(gemini: AcpAgent, tmp_path: Path) -> str:
    """A session whose late reply, "It finished.", is running."""
    sid = start(gemini, tmp_path, "[[LATER]] run it in the background", "default")
    wait_until(lambda: idle(gemini, sid))
    (tmp_path / "go").touch()
    wait_until(lambda: not idle(gemini, sid) and gemini._reply.get(sid) == ["It finished."])
    return sid


def turns(db_path: Path, session_id: str) -> int:
    conn = db.connect(db_path)
    try:
        return conn.execute("SELECT count(*) FROM session_turns WHERE session_id = ?", (session_id,)).fetchone()[0]
    finally:
        conn.close()


def test_a_usage_update_racing_stop_ends_the_late_reply_once(gemini: AcpAgent, tmp_path: Path, db_path: Path) -> None:
    sid = late_reply(gemini, tmp_path)
    connection = gemini._loaded[sid]
    cancel = connection.notify

    def cancel_then_usage(method: str, params: dict) -> None:
        cancel(method, params)
        gemini._update(connection, sid, {"sessionUpdate": "usage_update", "used": 10, "size": 1000})

    connection.notify = cancel_then_usage
    before = turns(db_path, sid)
    gemini.stop(sid)
    assert idle(gemini, sid) and gemini.lookup(sid).detail == "It finished."
    assert turns(db_path, sid) == before + 1


def test_a_late_reply_that_begins_as_stop_lets_go_ends_with_it(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = late_reply(gemini, tmp_path)
    connection = gemini._loaded[sid]
    interrupt = gemini.interrupt

    def interrupt_then_more(session_id: str) -> None:
        interrupt(session_id)
        gemini._update(connection, sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "More."}})

    gemini.interrupt = interrupt_then_more
    gemini.stop(sid)
    assert idle(gemini, sid) and gemini._late == {}


def test_a_safe_call_in_a_late_reply_starts_its_quiet_again(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = late_reply(gemini, tmp_path)
    connection = gemini._loaded[sid]
    late = gemini._late[sid]
    late.last -= acp_agent.LATE_QUIET_SECONDS
    written: list[dict] = []
    connection.write = written.append
    gemini._auto_allow(connection, 7, sid, {"kind": "execute"}, [{"kind": "allow_once", "optionId": "yes"}])
    assert written == [{"id": 7, "result": {"outcome": {"outcome": "selected", "optionId": "yes"}}}]
    assert time.monotonic() - late.last < 5


def test_a_safe_call_that_comes_as_stop_lets_go_is_cancelled(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = start(gemini, tmp_path, "hello", "default")
    wait_until(lambda: idle(gemini, sid))
    connection = gemini._loaded[sid]
    written: list[dict] = []
    interrupt = gemini.interrupt

    def interrupt_then_ask(session_id: str) -> None:
        interrupt(session_id)
        write, connection.write = connection.write, written.append
        try:
            gemini._auto_allow(connection, 7, sid, {"kind": "execute"}, [{"kind": "allow_once", "optionId": "yes"}])
        finally:
            connection.write = write

    gemini.interrupt = interrupt_then_ask
    gemini.stop(sid)
    assert written == [{"id": 7, "result": {"outcome": {"outcome": "cancelled"}}}]
    assert idle(gemini, sid) and gemini._late == {}


def test_a_safe_call_while_stop_runs_is_cancelled(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = late_reply(gemini, tmp_path)
    connection = gemini._loaded[sid]
    written: list[dict] = []
    connection.write = written.append
    gemini._stopping.add(sid)
    gemini._auto_allow(connection, 7, sid, {"kind": "execute"}, [{"kind": "allow_once", "optionId": "yes"}])
    gemini._stopping.discard(sid)
    assert written == [{"id": 7, "result": {"outcome": {"outcome": "cancelled"}}}]


def test_a_stop_whose_interrupt_fails_keeps_the_session_for_another_stop(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = start(gemini, tmp_path, "hello", "default")
    wait_until(lambda: idle(gemini, sid))

    def fail(session_id: str) -> None:
        raise acp_agent.AcpError("no answer")

    gemini.interrupt = fail
    with pytest.raises(acp_agent.AcpError):
        gemini.stop(sid)
    assert sid in gemini._loaded and sid not in gemini._stopping


def test_a_late_reply_ends_when_its_cancel_cannot_be_sent(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = late_reply(gemini, tmp_path)
    connection = gemini._loaded[sid]

    def broken(method: str, params: dict) -> None:
        raise AcpError("the pipe is gone")

    connection.notify = broken
    with pytest.raises(AcpError):
        gemini.stop(sid)
    assert idle(gemini, sid) and gemini._active == {}


QUESTION = {"sessionId": None, "toolCall": {"toolCallId": "interaction_q9", "title": "Keep going?"},
            "options": [{"optionId": "yes", "name": "Yes", "kind": "allow_once"}]}


def test_a_late_question_keeps_its_reply_open_until_answered(gemini: AcpAgent, tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(acp_agent, "LATE_QUIET_SECONDS", 1.0)
    sid = late_reply(gemini, tmp_path)
    connection = gemini._loaded[sid]
    gemini._permission(connection, 99, {**QUESTION, "sessionId": sid})
    time.sleep(2.5)  # past the quiet time twice over
    assert gemini.lookup(sid).state == "blocked" and len(gemini.requests()) == 1  # the turn waits on the card
    gemini.answer(gemini.requests()[0]["id"], {"answers": {"interaction_q9": {"answers": ["Yes"]}}})
    wait_until(lambda: idle(gemini, sid))
    wait_until(lambda: any(each.get("id") == 99 for each in received(tmp_path)))
    [reply] = [each for each in received(tmp_path) if each.get("id") == 99]
    assert reply["result"] == {"outcome": {"outcome": "selected", "optionId": "yes"}}


def test_a_turn_that_ends_with_a_card_open_tells_the_agent_it_is_cancelled(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = late_reply(gemini, tmp_path)
    connection = gemini._loaded[sid]
    gemini._permission(connection, 98, {**QUESTION, "sessionId": sid})
    assert len(gemini.requests()) == 1
    (tmp_path / "go-end").touch()  # the reply's usage update ends it
    wait_until(lambda: idle(gemini, sid))
    wait_until(lambda: gemini.requests() == [])
    wait_until(lambda: any(each.get("id") == 98 for each in received(tmp_path)))
    [reply] = [each for each in received(tmp_path) if each.get("id") == 98]
    assert reply["result"] == {"outcome": {"outcome": "cancelled"}}


def test_a_load_answer_without_models_still_sets_the_saved_model(gemini: AcpAgent) -> None:
    sent = []

    class Connection:
        def request(self, method: str, params: dict) -> dict:
            sent.append((method, params))
            return {}

    gemini._choose_model(Connection(), "s1", "gemini-3-pro", {}, saved=True)
    assert sent == [("session/set_config_option", {"sessionId": "s1", "configId": "model", "value": "gemini-3-pro"})]


def test_a_saved_model_the_agent_no_longer_lists_still_loads(gemini: AcpAgent) -> None:
    sent = []

    class Connection:
        def request(self, method: str, params: dict) -> dict:
            sent.append((method, params))
            return {}

    listed = {"configOptions": [{"id": "model", "type": "select",
                                 "options": [{"value": "gemini-3.8-flash", "name": "Gemini 3.8 Flash"}]}]}
    # A new choice of a model the account does not list is refused before it reaches the agent.
    with pytest.raises(AcpError, match="'gemini-3.8-flash-retired' is unavailable"):
        gemini._choose_model(Connection(), "s1", "gemini-3.8-flash-retired", listed, saved=False)
    assert sent == []
    # A saved session's model goes to the agent, which still takes the old id.
    gemini._choose_model(Connection(), "s1", "gemini-3.8-flash-retired", listed, saved=True)
    assert sent == [("session/set_config_option",
                     {"sessionId": "s1", "configId": "model", "value": "gemini-3.8-flash-retired"})]


def test_a_saved_session_on_an_unlisted_model_takes_its_next_message(gemini: AcpAgent, tmp_path: Path,
                                                                     db_path: Path) -> None:
    conn = db.connect(db_path)
    try:
        conn.execute("INSERT INTO acp_sessions (session_id, provider, name, cwd, model, state, updated_at)"
                     " VALUES ('old', 'gemini', 'x', ?, 'gemini-3.8-flash-retired', 'done', ?)", (str(tmp_path), db.now_iso()))
    finally:
        conn.close()
    gemini.send("old", "Say hello", restart=False)
    wait_until(lambda: idle(gemini, "old"))
    assert gemini.lookup("old").state == "done"
    chosen = [each["params"] for each in received(tmp_path) if each.get("method") == "session/set_config_option"]
    assert chosen == [{"sessionId": "old", "configId": "model", "value": "gemini-3.8-flash-retired"}]


def test_question_labels_stay_unique_when_a_suffix_matches_another_option(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = start(gemini, tmp_path, "[[LOOKALIKES]] pick", "default")
    wait_until(lambda: gemini.requests())
    [request] = gemini.requests()
    labels = [each["label"] for each in request["params"]["questions"][0]["options"]]
    assert labels == ["File (a, 2)", "File (b)", "File (a)"]
    gemini.answer(request["id"], {"answers": {"interaction_q1": {"answers": ["File (a)"]}}})
    wait_until(lambda: idle(gemini, sid))
    assert gemini.lookup(sid).detail == "You picked c."
    gemini.send(sid, "[[LOOKALIKES]] again", restart=False)
    wait_until(lambda: gemini.requests())
    gemini.answer(gemini.requests()[0]["id"], {"answers": {"interaction_q1": {"answers": ["File (a, 2)"]}}})
    wait_until(lambda: idle(gemini, sid))
    assert gemini.lookup(sid).detail == "You picked a."


def test_unique_labels_for_any_input() -> None:
    for names in (["File", "File", "File (a)"], ["x", "x", "x (a)", "x (a, 2)", "x (a)"], ["a", "a", "a (a)", "a (a)"]):
        answers = [{"value": value, "label": name} for value, name in zip("abcdefgh", names)]
        acp_agent._unique_labels(answers)
        labels = [each["label"] for each in answers]
        assert len(set(labels)) == len(labels), labels


CHUNK = {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "More."}}
USAGE = {"sessionUpdate": "usage_update", "used": 10, "size": 1000}


def replies(db_path: Path, session_id: str) -> list[str]:
    conn = db.connect(db_path)
    try:
        return [row["reply"] for row in conn.execute(
            "SELECT reply FROM session_turns WHERE session_id = ? ORDER BY started_at, ended_at", (session_id,))]
    finally:
        conn.close()


def test_a_chunk_arriving_as_the_quiet_watch_ends_a_late_reply_is_kept(gemini: AcpAgent, tmp_path: Path,
                                                                       db_path: Path,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    sid = late_reply(gemini, tmp_path)
    connection = gemini._loaded[sid]
    late = gemini._late[sid]
    late.last -= acp_agent.LATE_QUIET_SECONDS  # quiet for the whole quiet time
    save = gemini._save
    arriving: list[threading.Thread] = []

    def save_as_a_chunk_arrives(session_id: str, **changes: Any) -> None:
        if changes.get("state") == "done" and not arriving:
            # The ending turn's words are kept and its reply read: the agent's next words arrive now.
            arriving.append(threading.Thread(target=gemini._update, args=(connection, sid, CHUNK)))
            arriving[0].start()
            arriving[0].join(0.5)
        save(session_id, **changes)

    monkeypatch.setattr(gemini, "_save", save_as_a_chunk_arrives)
    gemini._late_quiet(sid, late)
    arriving[0].join()
    # The words came after the turn ended, so they start the next late turn.
    assert gemini._reply.get(sid) == ["More."] and gemini._late[sid] is not late
    gemini._update(connection, sid, USAGE)
    assert idle(gemini, sid)
    says = [part["text"] for part in gemini.log(sid)["parts"] if part["kind"] == "says"]
    assert says[-2:] == ["It finished.", "More."]
    assert replies(db_path, sid)[-2:] == ["It finished.", "More."]


def test_a_stale_quiet_watch_leaves_a_newer_late_reply_alone(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = late_reply(gemini, tmp_path)
    connection = gemini._loaded[sid]
    first = gemini._late[sid]
    gemini._update(connection, sid, USAGE)  # the first late reply ends
    assert idle(gemini, sid)
    gemini._update(connection, sid, CHUNK)  # a second one begins
    second = gemini._late[sid]
    assert second is not first
    second.last -= acp_agent.LATE_QUIET_SECONDS
    gemini._late_quiet(sid, first)  # the first reply's watch fires late
    assert not idle(gemini, sid) and gemini._late[sid] is second
    gemini._late_quiet(sid, second)
    assert idle(gemini, sid) and gemini._late == {}


def test_a_call_that_finishes_after_its_turn_shows_finished(gemini: AcpAgent, tmp_path: Path) -> None:
    sid = start(gemini, tmp_path, "Say hello", "default")
    wait_until(lambda: idle(gemini, sid))
    connection = gemini._loaded[sid]
    gemini._update(connection, sid, {"sessionUpdate": "tool_call_update", "toolCallId": "c2", "status": "failed"})
    assert idle(gemini, sid)
    [call] = [each for each in gemini.log(sid)["parts"] if each["kind"] == "call"]
    assert call["error"] is True
