"""The emergency session (SPEC SESSION-21): a turn per message on the account the user picks, its replies read back
from Claude's stream of events, and the page and its calls served without the database."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from cockpit import config, emergency
from tests.conftest import World

# Stands in for `claude -p`: records its arguments, account and prompt, then answers in stream-json. A prompt
# holding "slow" keeps it running until stopped; "stubborn" also ignores SIGTERM; "leaves" starts a process that
# ignores SIGTERM and outlives it; "escapes" does that in a process group of its own; "lingers" runs on after its
# final result.
FAKE = """#!/bin/sh
prompt=$(cat)
printf '%s\\n' "$*" > "$EMERGENCY_LOG.args"
printf '%s\\n' "${CLAUDE_CONFIG_DIR:-default}" "$COCKPIT_EMERGENCY" > "$EMERGENCY_LOG.env"
case "$prompt" in
  *slow*) sleep 60;;
  *stubborn*) trap '' TERM; sleep 60;;
  *leaves*) sh -c "trap '' TERM; sleep 60" & echo $! > "$EMERGENCY_LOG.child"; sleep 60;;
  *escapes*) setsid sh -c "trap '' TERM; sleep 60" & echo $! > "$EMERGENCY_LOG.child"; sleep 60;;
  *lingers*) echo '{"type":"result","subtype":"success","is_error":false,"result":"done"}'; sleep 60;;
esac
echo '{"type":"system","subtype":"init"}'
echo '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"systemctl --user status cockpit"}}]}}'
echo '{"type":"user","message":{"content":[{"type":"tool_result","is_error":true,"content":"auto mode blocked this"}]}}'
echo '{"type":"assistant","message":{"content":[{"type":"text","text":"Cockpit is running again."}]}}'
echo '{"type":"result","subtype":"success","is_error":false,"result":"Cockpit is running again."}'
"""


# Stands in for `codex exec --json`: records its arguments and prompt, then names its thread and answers.
FAKE_CODEX = """#!/bin/sh
cat > "$EMERGENCY_LOG.codex-prompt"
printf '%s\\n' "$*" > "$EMERGENCY_LOG.codex-args"
echo '{"type":"thread.started","thread_id":"thread-1"}'
echo '{"type":"turn.started"}'
echo '{"type":"item.started","item":{"id":"item_0","type":"command_execution","command":"systemctl --user status cockpit"}}'
echo '{"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"Cockpit is up."}}'
echo '{"type":"turn.completed","usage":{}}'
"""

# What a Claude message sends besides its words and account: the agent, model and effort picked on the page.
CLAUDE = {"provider": "claude", "model": "claude-opus-5-5", "effort": "high"}


@pytest.fixture
def fake_claude(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "claude").write_text(FAKE)
    (bin_dir / "claude").chmod(0o755)
    (bin_dir / "codex").write_text(FAKE_CODEX)
    (bin_dir / "codex").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("EMERGENCY_LOG", str(tmp_path / "fake"))
    monkeypatch.setattr(emergency, "STATE_PATH", tmp_path / "emergency.json")
    monkeypatch.setattr(emergency, "OUTPUT_DIR", tmp_path / "emergency")
    monkeypatch.setattr(emergency, "SCOPED", False)
    monkeypatch.setattr(emergency, "END_WAIT_SECONDS", 0.5)
    return tmp_path / "fake"


def seen(world: World) -> dict:
    """The session and turn count as the emergency page last read them, as Stop and Close send them."""
    view = world.client.get("/api/emergency", headers=world.user()).json()
    return {"session_id": view["session_id"], "count": view["count"]}


def stop(world: World):
    return world.client.post("/api/emergency/stop", json=seen(world), headers=world.user())


def close(world: World):
    return world.client.post("/api/emergency/close", json={**seen(world), "end_answer": True}, headers=world.user())


def alive(pid: int) -> bool:
    return emergency._started(pid) is not None


def wait_until_answered(world: World) -> dict:
    for _ in range(100):
        view = world.client.get("/api/emergency", headers=world.user()).json()
        if view["turns"] and not view["turns"][-1]["running"]:
            return view
        time.sleep(0.05)
    raise AssertionError("the turn never finished")


def test_user_sends_a_message_on_the_account_they_pick_and_reads_the_reply(world: World, fake_claude: Path) -> None:
    sent = world.client.post(
        "/api/emergency/send", json={**seen(world), "text": "The page is blank", "account": "work", **CLAUDE}, headers=world.user()
    )
    assert sent.status_code == 200
    view = wait_until_answered(world)
    session_id = view["session_id"]
    assert view["cwd"] == str(world.settings.checkouts_dir)
    [turn] = view["turns"]
    assert turn["account"] == "work"
    assert turn["text"] == "The page is blank"
    assert turn["events"] == [
        {"kind": "call", "text": "Bash: systemctl --user status cockpit"},
        {"kind": "refused", "text": "auto mode blocked this"},
        {"kind": "says", "text": "Cockpit is running again."},
    ]
    args = Path(f"{fake_claude}.args").read_text()
    assert f"--session-id {session_id}" in args and "--model claude-opus-5-5" in args
    folder, flag = Path(f"{fake_claude}.env").read_text().split()
    assert folder == str(config.account("work").folder) and flag == "1"


def test_the_next_message_resumes_the_session_on_another_account(world: World, fake_claude: Path) -> None:
    world.client.post("/api/emergency/send", json={**seen(world), "text": "first", "account": "work", **CLAUDE}, headers=world.user())
    session_id = wait_until_answered(world)["session_id"]
    transcript = config.account("personal").folder / "projects" / "-home" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")
    world.client.post("/api/emergency/send", json={**seen(world), "text": "second", "account": "personal", **CLAUDE}, headers=world.user())
    view = wait_until_answered(world)
    assert view["session_id"] == session_id
    assert [turn["account"] for turn in view["turns"]] == ["work", "personal"]
    assert f"--resume {session_id}" in Path(f"{fake_claude}.args").read_text()
    assert Path(f"{fake_claude}.env").read_text().split()[0] == "default"


def test_the_main_page_lists_the_session_once_it_has_a_message(world: World, fake_claude: Path) -> None:
    assert world.client.get("/api/state").json()["emergency"] is None
    world.client.post("/api/emergency/send", json={**seen(world), "text": "slow", "account": "work", **CLAUDE}, headers=world.user())
    listed = world.client.get("/api/state").json()["emergency"]
    assert listed["text"] == "slow" and listed["account"] == "work" and listed["running"]
    stop(world)
    wait_until_answered(world)
    assert not world.client.get("/api/state").json()["emergency"]["running"]


def test_an_unreadable_session_leaves_the_main_page_working(
    world: World, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> None:
        raise PermissionError("emergency.json")

    monkeypatch.setattr(emergency, "summary", broken)
    state = world.client.get("/api/state")
    assert state.status_code == 200 and state.json()["emergency"] is None


def test_a_running_turn_refuses_another_message_until_stopped(world: World, fake_claude: Path) -> None:
    world.client.post("/api/emergency/send", json={**seen(world), "text": "slow", "account": "personal", **CLAUDE}, headers=world.user())
    view = world.client.get("/api/emergency", headers=world.user()).json()
    assert view["turns"][0]["running"]
    busy = world.client.post("/api/emergency/send", json={**seen(world), "text": "again", "account": "personal", **CLAUDE}, headers=world.user())
    assert busy.status_code == 409
    stop(world)
    view = wait_until_answered(world)
    assert view["turns"][0]["events"] == [{"kind": "error", "text": "The turn ended before it finished."}]


# "stubborn" ignores SIGTERM, so Close falls back to SIGKILL; "leaves" ends on SIGTERM but leaves a process behind.
@pytest.mark.parametrize("prompt", ["slow", "stubborn", "leaves"])
def test_close_ends_every_process_of_a_running_answer_and_forgets_the_session(
    world: World, fake_claude: Path, prompt: str
) -> None:
    world.client.post("/api/emergency/send", json={**seen(world), "text": prompt, "account": "personal", **CLAUDE}, headers=world.user())
    turn = dict(emergency._read_state()["turns"][0])
    assert emergency._alive(turn)
    child = Path(f"{fake_claude}.child")
    if prompt == "leaves":
        for _ in range(100):
            if child.exists() and child.read_text().strip():
                break
            time.sleep(0.05)
    output = Path(turn["output"])
    closed = close(world)
    assert closed.status_code == 200
    assert closed.json()["session_id"] is None and closed.json()["turns"] == []
    assert not emergency._alive(turn)
    if prompt == "leaves":
        pid = int(child.read_text())
        for _ in range(40):
            if not alive(pid):
                break
            time.sleep(0.05)
        assert not alive(pid)
    assert not output.exists() and not output.with_suffix(".prompt").exists()
    assert world.client.get("/api/state").json()["emergency"] is None
    sent = world.client.post("/api/emergency/send", json={**seen(world), "text": "hello", "account": "work", **CLAUDE}, headers=world.user())
    assert sent.status_code == 200 and sent.json()["session_id"] not in (None, closed.json()["session_id"])


def _systemd_user_reason() -> str:
    if sys.platform != "linux":
        return "needs a Linux systemd user session"
    try:
        status = subprocess.run(
            ["systemctl", "--user", "is-system-running"], capture_output=True, text=True, check=False, timeout=5,
        )
    except FileNotFoundError:
        return "needs a systemd user session: systemctl is unavailable"
    except subprocess.TimeoutExpired:
        return "needs a systemd user session: systemctl did not answer within 5 seconds"
    if status.returncode in (0, 1) and status.stdout.strip() in ("running", "degraded"):
        return ""
    return f"needs a systemd user session: {(status.stderr or status.stdout).strip()}"


SYSTEMD_USER_REASON = _systemd_user_reason()


@pytest.mark.skipif(bool(SYSTEMD_USER_REASON), reason=SYSTEMD_USER_REASON)
def test_in_a_systemd_unit_close_and_a_damaged_state_file_end_every_process_of_the_turn(
    world: World, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(emergency, "SCOPED", True)
    # The units are named after the test's own state file, so the damaged file below never ends a real turn.
    child = Path(f"{fake_claude}.child")

    def started(prompt: str) -> int:
        child.unlink(missing_ok=True)
        sent = world.client.post("/api/emergency/send", json={**seen(world), "text": prompt, "account": "personal", **CLAUDE}, headers=world.user())
        assert sent.status_code == 200
        for _ in range(100):
            if child.exists() and child.read_text().strip():
                return int(child.read_text())
            time.sleep(0.05)
        raise AssertionError("the turn never started its process")

    def ended(pid: int) -> bool:
        for _ in range(60):
            if not alive(pid):
                return True
            time.sleep(0.05)
        return False

    pid = started("escapes")
    assert emergency._read_state()["turns"][0]["unit"].startswith(emergency._unit_prefix())
    assert close(world).status_code == 200
    assert ended(pid)
    pid = started("escapes")
    emergency.STATE_PATH.write_text("{")
    assert world.client.get("/api/emergency", headers=world.user()).json()["turns"] == []
    assert ended(pid)
    assert emergency._active_units() == []


def test_stop_ends_a_turn_that_ignores_sigterm(world: World, fake_claude: Path) -> None:
    world.client.post("/api/emergency/send", json={**seen(world), "text": "stubborn", "account": "personal", **CLAUDE}, headers=world.user())
    turn = dict(emergency._read_state()["turns"][0])
    assert stop(world).status_code == 200
    assert not emergency._alive(turn)
    assert not world.client.get("/api/emergency", headers=world.user()).json()["turns"][0]["running"]


def test_a_turn_runs_while_its_process_does_whatever_its_output_says(world: World, fake_claude: Path) -> None:
    world.client.post("/api/emergency/send", json={**seen(world), "text": "lingers", "account": "personal", **CLAUDE}, headers=world.user())
    time.sleep(0.3)
    assert world.client.get("/api/emergency", headers=world.user()).json()["turns"][0]["running"]
    busy = world.client.post("/api/emergency/send", json={**seen(world), "text": "again", "account": "personal", **CLAUDE}, headers=world.user())
    assert busy.status_code == 409
    stop(world)
    assert not world.client.get("/api/emergency", headers=world.user()).json()["turns"][0]["running"]


def test_stop_and_close_from_an_old_page_leave_a_newer_session_alone(world: World, fake_claude: Path) -> None:
    world.client.post("/api/emergency/send", json={**seen(world), "text": "first", "account": "personal", **CLAUDE}, headers=world.user())
    wait_until_answered(world)
    old = seen(world)
    assert close(world).status_code == 200
    world.client.post("/api/emergency/send", json={**seen(world), "text": "slow", "account": "personal", **CLAUDE}, headers=world.user())
    for path, body in (("stop", old), ("close", {**old, "end_answer": True})):
        refused = world.client.post(f"/api/emergency/{path}", json=body, headers=world.user())
        assert refused.status_code == 412 and "changed" in refused.json()["detail"]
    assert world.client.get("/api/emergency", headers=world.user()).json()["turns"][0]["running"]
    # A page that saw the session before its last message cannot stop or close it either.
    earlier = {**seen(world), "count": 0, "end_answer": True}
    assert world.client.post("/api/emergency/close", json=earlier, headers=world.user()).status_code == 412
    close(world)


def test_a_message_from_a_page_that_saw_another_session_is_refused(world: World, fake_claude: Path) -> None:
    world.client.post("/api/emergency/send", json={**seen(world), "text": "first", "account": "personal", **CLAUDE}, headers=world.user())
    wait_until_answered(world)
    old = seen(world)
    close(world)
    # Another tab closed it: the message would otherwise start a new session without the old one's context.
    refused = world.client.post("/api/emergency/send", json={**old, "text": "more", "account": "personal", **CLAUDE}, headers=world.user())
    assert refused.status_code == 409 and "changed" in refused.json()["detail"]
    # Nor does a page that saw no session join the one another tab started.
    world.client.post("/api/emergency/send", json={**seen(world), "text": "new", "account": "personal", **CLAUDE}, headers=world.user())
    wait_until_answered(world)
    blank = {"session_id": None, "count": 0, "text": "hi", "account": "personal", **CLAUDE}
    assert world.client.post("/api/emergency/send", json=blank, headers=world.user()).status_code == 409
    assert world.client.get("/api/emergency", headers=world.user()).json()["count"] == 1


def test_close_ends_an_answer_only_when_the_user_agreed(world: World, fake_claude: Path) -> None:
    world.client.post("/api/emergency/send", json={**seen(world), "text": "slow", "account": "personal", **CLAUDE}, headers=world.user())
    refused = world.client.post("/api/emergency/close", json={**seen(world), "end_answer": False}, headers=world.user())
    assert refused.status_code == 409
    assert world.client.get("/api/emergency", headers=world.user()).json()["turns"][0]["running"]
    assert close(world).status_code == 200


def test_a_first_turn_that_cannot_start_leaves_no_session(
    world: World, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise OSError("no such program")

    monkeypatch.setattr(emergency.subprocess, "Popen", broken)
    failed = world.client.post("/api/emergency/send", json={**seen(world), "text": "hi", "account": "work", **CLAUDE}, headers=world.user())
    assert failed.status_code == 500
    assert emergency._load() == emergency.EMPTY


@pytest.mark.skipif(bool(SYSTEMD_USER_REASON), reason=SYSTEMD_USER_REASON)
def test_ending_a_turn_whose_unit_is_not_there_yet_ends_its_launcher(
    world: World, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A launcher that has not made its unit: systemd knows no such unit, so only the group signal reaches it.
    monkeypatch.setattr(emergency, "SCOPED", True)
    process = subprocess.Popen(["sleep", "60"], start_new_session=True)
    threading.Thread(target=process.wait, daemon=True).start()
    turn = {"unit": f"{emergency._unit_prefix()}never-made", "pid": process.pid, "started": emergency._started(process.pid)}
    emergency._end(turn)
    assert not alive(process.pid)


def test_recovery_ends_units_that_are_still_stopping(monkeypatch: pytest.MonkeyPatch) -> None:
    prefix = emergency._unit_prefix()
    listing = (
        f"{prefix}a-1.scope loaded active running x\n{prefix}b-1.scope loaded deactivating stop-sigterm x\n"
        f"{prefix}c-1.scope loaded inactive dead x\n{prefix}d-1.scope loaded failed failed x\n"
    )
    monkeypatch.setattr(emergency, "SCOPED", True)
    monkeypatch.setattr(emergency, "_systemctl", lambda *args: subprocess.CompletedProcess(args, 0, listing, ""))
    assert emergency._active_units() == [f"{prefix}a-1", f"{prefix}b-1"]


def test_closing_a_session_that_is_already_closed_does_nothing(world: World, fake_claude: Path) -> None:
    world.client.post("/api/emergency/send", json={**seen(world), "text": "first", "account": "personal", **CLAUDE}, headers=world.user())
    wait_until_answered(world)
    old = seen(world)
    assert close(world).status_code == 200
    again = world.client.post("/api/emergency/close", json={**old, "end_answer": True}, headers=world.user())
    assert again.status_code == 200 and again.json()["session_id"] is None


def test_a_turn_that_ends_early_says_so_after_what_claude_said(world: World, fake_claude: Path) -> None:
    world.client.post("/api/emergency/send", json={**seen(world), "text": "slow", "account": "personal", **CLAUDE}, headers=world.user())
    output = Path(emergency._read_state()["turns"][0]["output"])
    output.write_text('{"type":"assistant","message":{"content":[{"type":"text","text":"Restarting Cockpit."}]}}\n')
    stop(world)
    view = wait_until_answered(world)
    assert view["turns"][0]["events"] == [
        {"kind": "says", "text": "Restarting Cockpit."},
        {"kind": "error", "text": "The turn ended before it finished."},
    ]


def test_the_session_resumes_from_a_transcript_only_the_work_account_holds(world: World, fake_claude: Path) -> None:
    world.client.post("/api/emergency/send", json={**seen(world), "text": "first", "account": "work", **CLAUDE}, headers=world.user())
    session_id = wait_until_answered(world)["session_id"]
    transcript = config.account("work").folder / "projects" / "-home" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")
    world.client.post("/api/emergency/send", json={**seen(world), "text": "second", "account": "work", **CLAUDE}, headers=world.user())
    wait_until_answered(world)
    assert f"--resume {session_id}" in Path(f"{fake_claude}.args").read_text()


TURN = {"at": "2026-01-29T00:00:00Z", "account": "work", "text": "hi", "pid": None, "started": None, "output": "/x"}


@pytest.mark.parametrize("damaged", [
    "{",
    '{"session_id": null, "turns": [{}]}',
    # pid 0 would signal Cockpit's own process group.
    json.dumps({"session_id": None, "turns": [{**TURN, "pid": 0}]}),
    json.dumps({"session_id": None, "turns": [{**TURN, "account": None}]}),
    '{"turns": []}',
    json.dumps({"session_id": None, "turns": [TURN]}),
    json.dumps({"session_id": "s", "turns": [{**TURN, "account": ""}]}),
    json.dumps({"session_id": "s", "turns": [{key: value for key, value in TURN.items() if key != "pid"}]}),
    json.dumps({"session_id": "s", "turns": [{key: value for key, value in TURN.items() if key != "started"}]}),
    # A lone surrogate, which cannot go out as UTF-8.
    json.dumps({"session_id": "s", "turns": [{**TURN, "text": "\ud800"}]}),
])
def test_a_damaged_state_file_is_set_aside_and_the_page_keeps_working(
    world: World, fake_claude: Path, damaged: str
) -> None:
    emergency.STATE_PATH.write_text(damaged)
    assert world.client.get("/api/state").json()["emergency"] is None
    view = world.client.get("/api/emergency", headers=world.user())
    assert view.status_code == 200 and view.json()["turns"] == []
    assert [path.read_text() for path in emergency.STATE_PATH.parent.glob("emergency.broken-*.json")] == [damaged]
    sent = world.client.post("/api/emergency/send", json={**seen(world), "text": "help", "account": "personal", **CLAUDE}, headers=world.user())
    assert sent.status_code == 200


def test_output_with_line_separators_and_lone_surrogates_reads_as_written(world: World, fake_claude: Path) -> None:
    world.client.post("/api/emergency/send", json={**seen(world), "text": "first", "account": "personal", **CLAUDE}, headers=world.user())
    wait_until_answered(world)
    output = Path(emergency._read_state()["turns"][0]["output"])
    output.write_text(
        '{"type":"assistant","message":{"content":[{"type":"text","text":"one\u2028two \\ud800"}]}}\n'
        '{"type":"result","subtype":"success","is_error":false,"result":"done"}\n'
    )
    view = world.client.get("/api/emergency", headers=world.user())
    assert view.status_code == 200
    assert view.json()["turns"][0]["events"] == [{"kind": "says", "text": "one\u2028two ?"}]


def test_a_long_output_is_read_from_its_end(world: World, fake_claude: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(emergency, "OUTPUT_READ_BYTES", 200)
    world.client.post("/api/emergency/send", json={**seen(world), "text": "first", "account": "personal", **CLAUDE}, headers=world.user())
    wait_until_answered(world)
    output = Path(emergency._read_state()["turns"][0]["output"])
    output.write_text("x" * 5000 + '\n{"type":"result","subtype":"success","is_error":false,"result":"done"}\n')
    [turn] = world.client.get("/api/emergency", headers=world.user()).json()["turns"]
    assert turn["events"] == [
        {"kind": "output", "text": "The start of this turn's output is left out: it is too long to show."}
    ]


def test_an_output_cockpit_cannot_read_shows_why(world: World, fake_claude: Path) -> None:
    world.client.post("/api/emergency/send", json={**seen(world), "text": "first", "account": "personal", **CLAUDE}, headers=world.user())
    wait_until_answered(world)
    output = Path(emergency._read_state()["turns"][0]["output"])
    output.unlink()
    output.mkdir()
    view = world.client.get("/api/emergency", headers=world.user())
    assert view.status_code == 200
    assert view.json()["turns"][0]["events"][0]["text"].startswith("Cockpit could not read this turn's output")
    assert close(world).status_code == 200


def test_an_output_folder_cockpit_cannot_open_shows_why(world: World, fake_claude: Path) -> None:
    world.client.post("/api/emergency/send", json={**seen(world), "text": "first", "account": "personal", **CLAUDE}, headers=world.user())
    wait_until_answered(world)
    emergency._parsed.clear()
    emergency.OUTPUT_DIR.chmod(0)
    try:
        view = world.client.get("/api/emergency", headers=world.user())
    finally:
        emergency.OUTPUT_DIR.chmod(0o755)
    assert view.status_code == 200
    assert view.json()["turns"][0]["events"][0]["text"].startswith("Cockpit could not read this turn's output")


def test_only_the_shown_turns_stay_read(world: World, fake_claude: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(emergency, "SHOWN_TURNS", 1)
    for text in ("first", "second"):
        world.client.post("/api/emergency/send", json={**seen(world), "text": text, "account": "personal", **CLAUDE}, headers=world.user())
        wait_until_answered(world)
    assert list(emergency._parsed) == [emergency._read_state()["turns"][-1]["output"]]


def test_a_turn_stays_recorded_while_systemd_does_not_answer(
    world: World, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(emergency, "SCOPED", True)
    emergency.STATE_PATH.write_text(json.dumps({"session_id": "s", "turns": [{**TURN, "unit": "u", "pid": 99999999}]}))

    def unreachable(*args: str) -> None:
        raise emergency.Unreachable("systemctl show did not answer")

    monkeypatch.setattr(emergency, "_systemctl", unreachable)
    seen_now = {"session_id": "s", "count": 1, "end_answer": True}
    for answer in (
        world.client.get("/api/emergency", headers=world.user()),
        world.client.post("/api/emergency/close", json=seen_now, headers=world.user()),
        world.client.post("/api/emergency/send", json={"session_id": "s", "count": 1, "text": "hi", "account": "work", **CLAUDE}, headers=world.user()),
    ):
        assert answer.status_code == 503 and "systemd" in answer.json()["detail"]
    assert "u" not in emergency._ended_units
    assert emergency._read_state()["session_id"] == "s"
    # The main page asks systemd nothing.
    assert world.client.get("/api/state").json()["emergency"]["running"] is False


def test_a_damaged_state_file_stays_while_its_turns_cannot_be_ended(
    world: World, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreachable() -> list[str]:
        raise emergency.Unreachable("systemctl list-units did not answer")

    monkeypatch.setattr(emergency, "_active_units", unreachable)
    emergency.STATE_PATH.write_text("{")
    assert world.client.get("/api/emergency", headers=world.user()).status_code == 503
    assert emergency.STATE_PATH.read_text() == "{"


def test_a_message_that_is_not_valid_text_is_refused(world: World, fake_claude: Path) -> None:
    refused = world.client.post(
        "/api/emergency/send", content='{"session_id": null, "count": 0, "text": "\\ud800", "account": "work", "provider": "claude", "model": "claude-opus-5-5", "effort": "high"}',
        headers={**world.user(), "content-type": "application/json"},
    )
    assert refused.status_code in (400, 422)
    assert world.client.get("/api/emergency", headers=world.user()).json()["session_id"] is None


def test_an_unknown_account_is_refused(world: World, fake_claude: Path) -> None:
    refused = world.client.post("/api/emergency/send", json={**seen(world), "text": "hi", "account": "other", **CLAUDE}, headers=world.user())
    assert refused.status_code == 400
    assert "no Claude account 'other'" in refused.json()["detail"]


def test_the_page_lists_each_account_and_whether_it_is_signed_in(
    world: World, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(emergency.accounts, "signed_in", lambda account: account.name == "personal")
    monkeypatch.setattr(emergency.accounts, "signed_out", lambda account, now: False)
    view = world.client.get("/api/emergency", headers=world.user()).json()
    assert [(account["name"], account["letter"], account["signed_in"]) for account in view["accounts"]] == [
        ("personal", "P", True), ("work", "W", False)
    ]
    assert view["session_id"] is None
    page = world.client.get("/emergency")
    assert page.status_code == 200
    assert '<script src="/static/emergency.js"' in page.text and "app.js" not in page.text


def test_the_page_works_while_the_database_is_broken(world: World, fake_claude: Path) -> None:
    world.db_path.write_text("not a database")
    assert world.client.get("/api/emergency", headers=world.user()).status_code == 200
    sent = world.client.post("/api/emergency/send", json={**seen(world), "text": "help", "account": "personal", **CLAUDE}, headers=world.user())
    assert sent.status_code == 200


def test_only_the_signed_in_user_reaches_the_emergency_session(world: World, anonymous, fake_claude: Path) -> None:
    assert anonymous.get("/emergency").status_code == 302
    assert anonymous.get("/api/emergency").status_code == 401
    assert anonymous.post("/api/emergency/send", json={**seen(world), "text": "x", "account": "personal", **CLAUDE}).status_code in (401, 403)


def test_the_permission_hook_leaves_dialogs_to_an_emergency_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    from cockpit import cli

    monkeypatch.setenv("COCKPIT_EMERGENCY", "1")
    payload = {"session_id": "s", "cwd": "/", "tool_name": "Bash", "tool_input": {"command": "ls"}}
    assert cli.permission_ask(payload, "permission-hook") is None


@pytest.fixture
def codex_on(monkeypatch: pytest.MonkeyPatch) -> None:
    setup = config.setup()
    monkeypatch.setattr(config, "setup", lambda: dataclasses.replace(setup, providers=("claude", "codex")))


def test_each_message_runs_on_the_model_and_effort_the_user_picks(world: World, fake_claude: Path) -> None:
    sent = world.client.post("/api/emergency/send", headers=world.user(), json={**seen(world),
        "text": "first", "account": "work", "provider": "claude", "model": "claude-sonnet-5-5", "effort": "low"})
    assert sent.status_code == 200
    view = wait_until_answered(world)
    args = Path(f"{fake_claude}.args").read_text()
    assert "--model claude-sonnet-5-5" in args and "--effort low" in args
    assert (view["turns"][0]["model"], view["turns"][0]["effort"]) == ("claude-sonnet-5-5", "low")
    assert view["provider"] == "claude" and list(view["options"]) == ["claude"]
    refused = world.client.post("/api/emergency/send", headers=world.user(), json={**seen(world),
        "text": "second", "account": "work", "provider": "claude", "model": "gpt-6.1-sol", "effort": "low"})
    assert refused.status_code == 400 and "claude offers no model gpt-6.1-sol" in refused.json()["detail"]


def test_codex_runs_here_only_once_it_is_on(world: World, fake_claude: Path) -> None:
    refused = world.client.post("/api/emergency/send", headers=world.user(), json={**seen(world),
        "text": "hi", "account": None, "provider": "codex", "model": "gpt-6.1-sol", "effort": "high"})
    assert refused.status_code == 400 and "codex is not enabled" in refused.json()["detail"]


def test_a_codex_session_starts_a_thread_and_resumes_it(world: World, fake_claude: Path, codex_on: None) -> None:
    codex = {"account": None, "provider": "codex", "model": "gpt-6-luna", "effort": "medium"}
    assert world.client.post("/api/emergency/send", json={**seen(world), "text": "first", **codex}, headers=world.user()).status_code == 200
    view = wait_until_answered(world)
    [turn] = view["turns"]
    assert view["provider"] == "codex" and turn["account"] is None
    assert turn["events"] == [
        {"kind": "call", "text": "shell: systemctl --user status cockpit"},
        {"kind": "says", "text": "Cockpit is up."},
    ]
    args = Path(f"{fake_claude}.codex-args").read_text()
    assert args.startswith("exec --json") and "-m gpt-6-luna" in args and 'model_reasoning_effort="medium"' in args
    assert 'sandbox_mode="workspace-write"' in args
    # The thread's first message carries the note about this page, as Codex takes no system prompt.
    prompt = Path(f"{fake_claude}.codex-prompt").read_text()
    assert "emergency page" in prompt and prompt.endswith("first")
    world.client.post("/api/emergency/send", json={**seen(world), "text": "second", **codex}, headers=world.user())
    wait_until_answered(world)
    args = Path(f"{fake_claude}.codex-args").read_text()
    assert args.startswith("exec resume --json") and "thread-1 -" in args
    assert Path(f"{fake_claude}.codex-prompt").read_text() == "second"
    # The session keeps its agent; a Claude message waits for a new session.
    switched = world.client.post("/api/emergency/send", json={**seen(world), "text": "third", "account": "work", **CLAUDE}, headers=world.user())
    assert switched.status_code == 409 and "runs on codex" in switched.json()["detail"]
    summary = emergency.summary()
    assert summary is not None and (summary["provider"], summary["account"]) == ("codex", None)


def test_a_codex_turn_names_no_account_and_a_claude_turn_one(world: World, fake_claude: Path, codex_on: None) -> None:
    for body in ({"account": "work", "provider": "codex", "model": "gpt-6.1-sol", "effort": "high"},
                 {"account": None, **CLAUDE}):
        refused = world.client.post("/api/emergency/send", json={**seen(world), "text": "hi", **body}, headers=world.user())
        assert refused.status_code == 400 and "names its account" in refused.json()["detail"]


def test_a_session_from_before_agents_and_models_reads_as_claude(world: World, fake_claude: Path) -> None:
    output = emergency.OUTPUT_DIR / "old-1.jsonl"
    output.parent.mkdir(parents=True)
    output.write_text('{"type":"result","subtype":"success","is_error":false,"result":"ok"}\n')
    emergency.STATE_PATH.write_text(json.dumps({"session_id": "old", "cwd": "/", "turns": [
        {"at": "2026-01-28T10:00:00Z", "account": "work", "text": "hi", "unit": None, "pid": None, "started": None,
         "output": str(output)}]}))
    view = world.client.get("/api/emergency", headers=world.user()).json()
    assert view["provider"] == "claude"
    assert (view["turns"][0]["model"], view["turns"][0]["effort"]) == (None, None)


def test_a_page_loaded_before_agents_and_models_still_sends_on_claude(world: World, fake_claude: Path) -> None:
    sent = world.client.post("/api/emergency/send", json={**seen(world), "text": "hi", "account": "work"}, headers=world.user())
    assert sent.status_code == 200, sent.text
    [turn] = wait_until_answered(world)["turns"]
    assert (turn["model"], turn["effort"], turn["account"]) == ("claude-opus-5-5", "high", "work")
    half = world.client.post("/api/emergency/send", json={**seen(world), "text": "hi", "account": "work", "model": "claude-sonnet-5-5"},
                             headers=world.user())
    assert half.status_code == 400 and "go together" in half.json()["detail"]


def test_a_codex_session_keeps_its_thread_when_an_output_is_gone(world: World, fake_claude: Path, codex_on: None) -> None:
    codex = {"account": None, "provider": "codex", "model": "gpt-6-luna", "effort": "medium"}
    world.client.post("/api/emergency/send", json={**seen(world), "text": "first", **codex}, headers=world.user())
    wait_until_answered(world)
    world.client.post("/api/emergency/send", json={**seen(world), "text": "second", **codex}, headers=world.user())
    wait_until_answered(world)
    state = json.loads(emergency.STATE_PATH.read_text())
    assert state["thread"] == "thread-1"
    # The second message found the thread and the state keeps it, so a first output gone since changes nothing.
    Path(state["turns"][0]["output"]).unlink()
    world.client.post("/api/emergency/send", json={**seen(world), "text": "third", **codex}, headers=world.user())
    wait_until_answered(world)
    assert "thread-1 -" in Path(f"{fake_claude}.codex-args").read_text()


def test_a_codex_thread_that_cannot_be_read_is_not_started_again(world: World, fake_claude: Path, codex_on: None) -> None:
    codex = {"account": None, "provider": "codex", "model": "gpt-6-luna", "effort": "medium"}
    world.client.post("/api/emergency/send", json={**seen(world), "text": "first", **codex}, headers=world.user())
    wait_until_answered(world)
    Path(json.loads(emergency.STATE_PATH.read_text())["turns"][0]["output"]).chmod(0)
    refused = world.client.post("/api/emergency/send", json={**seen(world), "text": "second", **codex}, headers=world.user())
    assert refused.status_code == 400 and "find the Codex thread" in refused.json()["detail"]
    assert len(json.loads(emergency.STATE_PATH.read_text())["turns"]) == 1
