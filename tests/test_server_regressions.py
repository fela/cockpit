from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from cockpit import codex, commands, db, delivery, store
from tests.conftest import SESSION_A, World
from tests.test_api import put_item, put_step
from tests.test_codex import NOW, SESSION, _answer, count, meta, rollout
from tests.test_spend import NOW as SPEND_NOW
from tests.test_spend import SESSION as SPEND_SESSION
from tests.test_spend import call, index_of, prompt, subagent, transcript, usage


def test_recovery_keeps_a_surviving_command_group(world: World, tmp_path: Path) -> None:
    command = "while [ ! -f done ]; do sleep 0.05; done"
    put_item(world, key="note:tasks", sessions=[SESSION_A])
    step = put_step(world, item_key="note:tasks", command={
        "run": command, "cwd": str(tmp_path), "explain": "Wait for a file.",
    })["step"]
    conn = db.connect(world.db_path)
    try:
        run_id = conn.execute(
            "INSERT INTO command_runs (step_id, session_id, command_text, cwd, started_at) VALUES (?, ?, ?, ?, ?)",
            (step["id"], SESSION_A, command, str(tmp_path), db.now_iso()),
        ).lastrowid
        assert run_id is not None
        recovered = []
        observed = []
        repeated = []

        def deliver(message_id: int) -> None:
            if message_id != 0:
                return
            try:
                run = store.get_run(conn, run_id)
                os.killpg(run["process_id"], 0)
                recovered.extend(commands.interrupted(conn, tmp_path))
                observed.append(store.get_run(conn, run_id))
                repeated.append(commands.interrupted(conn, tmp_path))
            finally:
                (tmp_path / "done").touch()

        commands.execute(world.db_path, tmp_path, run_id, 0, deliver)
        assert len(recovered) == 1
        assert observed[0]["process_id"]
        assert observed[0]["finished_at"] is None
        assert "still running" in observed[0]["output"]
        assert repeated == [[]]
        assert "still running" in store.get_message(conn, recovered[0])["text"]
        assert store.get_run(conn, run_id)["exit_code"] == 0
    finally:
        conn.close()


def test_recovery_checks_the_group_after_its_leader_exits(world: World, tmp_path: Path) -> None:
    put_item(world, key="note:tasks", sessions=[SESSION_A])
    step = put_step(world, item_key="note:tasks")["step"]
    process = subprocess.Popen(["sh", "-c", "sleep 30 &"], start_new_session=True)
    conn = db.connect(world.db_path)
    try:
        assert process.wait(timeout=5) == 0
        run_id = conn.execute(
            "INSERT INTO command_runs (step_id, session_id, command_text, cwd, started_at, process_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (step["id"], SESSION_A, "sleep 30 &", str(tmp_path), db.now_iso(), process.pid),
        ).lastrowid
        [message_id] = commands.interrupted(conn, tmp_path)
        run = store.get_run(conn, run_id)
        assert run["process_id"] == process.pid
        assert run["finished_at"] is None
        assert "still running" in store.get_message(conn, message_id)["text"]
        os.killpg(run["process_id"], signal.SIGTERM)
    finally:
        conn.close()
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def test_recovery_reports_an_absent_process_as_unknown(world: World, tmp_path: Path, monkeypatch) -> None:
    put_item(world, key="note:tasks", sessions=[SESSION_A])
    step = put_step(world, item_key="note:tasks")["step"]
    conn = db.connect(world.db_path)
    checked = []

    def gone(pid, sig):
        checked.append(pid)
        raise ProcessLookupError

    monkeypatch.setattr(commands.os, "killpg", gone)
    try:
        run_id = conn.execute(
            "INSERT INTO command_runs (step_id, session_id, command_text, cwd, started_at, process_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (step["id"], SESSION_A, "true", str(tmp_path), db.now_iso(), 12345),
        ).lastrowid
        [message_id] = commands.interrupted(conn, tmp_path)
        assert checked == [12345]
        assert store.get_run(conn, run_id)["finished_at"] is not None
        text = store.get_message(conn, message_id)["text"]
        assert "outcome is unknown" in text
        assert "stopped it" not in text
    finally:
        conn.close()


def test_restart_does_not_reassign_an_old_accounts_meter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    address = "ana@example.test"
    monkeypatch.setattr(codex, "email", lambda _home: address)
    monkeypatch.setattr(codex, "_changed", lambda _home: None)
    monkeypatch.setattr(codex, "ask_limits", lambda _home, _command: _answer(70, NOW + timedelta(days=5)))
    rollout(tmp_path, SESSION, [meta(3, "exec", "/srv/tasks"), count(3, 100, 0, 10, used=70)])
    account = codex.AccountIndex(tmp_path)
    account.ask("unused", NOW)
    assert account.week(NOW)["used"] == 70
    address = "lee@example.test"
    restarted = codex.AccountIndex(tmp_path)
    assert restarted.week(NOW) is None
    restarted.refresh(NOW)
    assert restarted.week(NOW) is None
    monkeypatch.setattr(codex, "ask_limits", lambda _home, _command: _answer(10, NOW + timedelta(days=5)))
    restarted.ask("unused", NOW)
    assert restarted.week(NOW)["used"] == 10
    assert codex.AccountIndex(tmp_path).week(NOW)["used"] == 10
    address = "ana@example.test"
    assert codex.AccountIndex(tmp_path).week(NOW)["used"] == 70


def test_subagent_spend_uses_its_start_prompt_before_its_first_answer(tmp_path: Path) -> None:
    transcript(tmp_path, SPEND_SESSION, [
        prompt(0, "Check the tasks"), call("parent-a", 1, usage(output=1000)),
        prompt(10, "Review the result"), call("parent-b", 11, usage(output=2000)),
    ])
    path = subagent(tmp_path, SPEND_SESSION, "helper", [
        prompt(2, "Check the task list", isSidechain=True),
    ])
    index = index_of(tmp_path)
    with path.open("a") as handle:
        handle.write(json.dumps(call("helper-a", 12, usage(output=5000)), separators=(",", ":")) + "\n")
    index.refresh(SPEND_NOW)
    summary = index.summary(SPEND_NOW - timedelta(days=7))
    turns = {row["prompt"]: row for row in summary["top_turns"]}
    assert turns["Check the tasks"]["use"]["tokens"]["output"] == 6000
    assert turns["Review the result"]["use"]["tokens"]["output"] == 2000
    assert summary["total"]["tokens"]["output"] == 8000
    assert summary["total"]["calls"] == 3


def test_long_delivery_uses_stdin_and_preserves_the_full_text(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = db.connect(world.db_path)
    try:
        batch = [store.create_message(conn, "user", f"Task {i}: " + "é" * 20_000,
                                      {"session_id": SESSION_A}, tmp_path) for i in range(7)]
        assert all(message["state"] == "queued" for message in batch)
        text = delivery.compose_batch(conn, batch)
    finally:
        conn.close()
    reader = "import sys; print(len(sys.argv)); print(sys.stdin.read(), end='')"
    seen = []

    def command(_account, args):
        seen.extend(args)
        return [sys.executable, "-c", reader, *args[1:]]

    monkeypatch.setattr(delivery, "claude_command", command)
    result = delivery.run_command(None, ["claude", "--bg", "--resume", SESSION_A, text], tmp_path)
    assert result.returncode == 0
    assert result.stdout.split("\n", 1)[1] == text
    assert text not in seen
