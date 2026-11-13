"""Exercise the process boundary with a local fake app-server; no model calls."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cockpit import db
from cockpit.app import create_app
from cockpit.codex_agent import CodexError, ProviderSessions
from cockpit.codex_worker import WorkerClient, launch, socket_path
from tests.conftest import SESSION_B
from tests.test_api import put_item

FAKE = '''#!/usr/bin/env python3
import json, sys
from pathlib import Path
sid = "22222222-2222-4222-8222-222222222222"
def emit(message):
    print(json.dumps(message), flush=True)
start_id = None
for line in sys.stdin:
    m = json.loads(line)
    method = m.get("method")
    if method == "thread/start":
        cwd = Path(m["params"]["cwd"])
        result = {"thread": {"id": sid}}
    elif method == "turn/start":
        with (cwd / "executed").open("a") as f:
            f.write("once\\n")
        start_id = m["id"]
        emit({"method": "turn/started", "params": {"threadId": sid, "turn": {"id": "turn1"}}})
        emit({"id": "original-request", "method": "item/commandExecution/requestApproval",
              "params": {"threadId": sid, "turnId": "turn1", "command": "echo approved"}})
        continue
    elif "result" in m:
        assert m["id"] == "original-request", m
        (cwd / "answer").write_text(json.dumps(m))
        emit({"id": start_id, "result": {"turn": {"id": "turn1"}}})
        emit({"method": "item/completed", "params": {"threadId": sid,
              "item": {"id": "reply", "type": "agentMessage", "text": "Finished after restart"}}})
        emit({"method": "turn/completed", "params": {"threadId": sid,
              "turn": {"id": "turn1", "status": "completed"}}})
        continue
    elif method == "initialized":
        continue
    else:
        result = {}
    emit({"id": m["id"], "result": result})
'''


def wait_until(check):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.02)
    raise AssertionError("timed out waiting for worker")


@pytest.fixture
def worker(world, monkeypatch):
    command = world.checkout / "fake-codex"
    command.write_text(FAKE)
    command.chmod(0o700)
    processes = []

    def start_worker(db_path, command):
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
        process = subprocess.Popen([sys.executable, "-m", "cockpit.codex_worker", str(db_path), command],
                                   env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
        processes.append(process)

    monkeypatch.setattr("cockpit.codex_worker.launch", start_worker)
    client = WorkerClient(world.db_path, str(command))
    # Start via the real client's lazy connection path.
    assert client.sessions() == []
    try:
        yield client, processes
    finally:
        for process in processes:
            process.terminate()
            process.wait(timeout=5)
            process.stderr.close()


def test_active_turn_and_original_request_survive_web_process_loss(world, worker):
    client, processes = worker
    # Send without retaining the caller, as when a web process dies waiting for
    # turn/start's response. The worker continues execution and keeps the request.
    caller = subprocess.Popen([sys.executable, "-c", '''
import sys
from pathlib import Path
from cockpit.codex_worker import WorkerClient
WorkerClient(Path(sys.argv[1]), sys.argv[2]).start(Path(sys.argv[3]), "test", "do work", "gpt-6.1-sol", "high", "standard")
''', str(world.db_path), client.command, str(world.checkout)],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
    try:
        original = wait_until(client.requests)[0]
        caller.kill()
        caller.wait(timeout=5)
        # A new web app and client must not mark the worker's turn interrupted.
        reconnected = WorkerClient(world.db_path, client.command)
        world.deliverer.codex = reconnected
        create_app(world.settings, world.deliverer, ProviderSessions(None, reconnected), world.spend_index, world.codex_index, [])
        assert reconnected.requests() == [original]
        assert reconnected.lookup(SESSION_B).state == "blocked"
        assert reconnected.lookup(SESSION_B).pid == client.lookup(SESSION_B).pid
        assert len(processes) == 1
        reconnected.answer(original["id"], {"decision": "accept"})
        wait_until(lambda: reconnected.lookup(SESSION_B).state == "done")
        assert reconnected.requests() == []
        assert (world.checkout / "executed").read_text() == "once\n"
        assert json.loads((world.checkout / "answer").read_text())["id"] == "original-request"
        conn = db.connect(world.db_path)
        try:
            assert conn.execute("SELECT reply FROM session_turns WHERE session_id = ?", (SESSION_B,)).fetchone()[0] == "Finished after restart"
        finally:
            conn.close()
    finally:
        if caller.poll() is None:
            caller.kill()
            caller.wait(timeout=5)


def test_new_thread_attached_before_lost_start_response(world, worker):
    client, _ = worker
    item = put_item(world, key="note:worker-identity", title="Preserve conversation")
    conn = db.connect(world.db_path)
    try:
        conn.execute("INSERT INTO messages (text, author, state, created_at, new_cwd, new_name, item_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     ("work", "user", "sending", db.now_iso(), str(world.checkout), "test", item["id"]))
        message_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    finally:
        conn.close()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(str(socket_path(world.db_path)))
        connection.sendall((json.dumps({"method": "start_for_message", "args": [message_id, str(world.checkout),
            "test", "work", "gpt-6.1-sol", "high", "standard"]}) + "\n").encode())
    request = wait_until(client.requests)[0]
    conn = db.connect(world.db_path)
    try:
        assert conn.execute("SELECT session_id FROM messages WHERE id = ?", (message_id,)).fetchone()[0] == SESSION_B
        assert json.loads(conn.execute("SELECT sessions FROM items WHERE id = ?", (item["id"],)).fetchone()[0]) == [SESSION_B]
        from cockpit.delivery import mark_interrupted
        mark_interrupted(conn)
        message = conn.execute("SELECT state, detail FROM messages WHERE id = ?", (message_id,)).fetchone()
        assert message["state"] == "failed" and "Delivery uncertain" in message["detail"]
    finally:
        conn.close()
    client.answer(request["id"], {"decision": "accept"})
    wait_until(lambda: client.lookup(SESSION_B).state == "done")


def test_transport_failure_after_write_is_uncertain_and_never_replayed(tmp_path):
    path = socket_path(tmp_path / "test.db")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        listener.listen()
        client = WorkerClient(tmp_path / "test.db", "unused")
        import threading
        received = []

        def drop():
            connection, _ = listener.accept()
            with connection, connection.makefile("rb") as stream:
                received.append(json.loads(stream.readline()))

        thread = threading.Thread(target=drop)
        thread.start()
        with pytest.raises(CodexError, match="Delivery uncertain"):
            client.send(SESSION_B, "partly executed")
        thread.join(timeout=5)
        assert len(received) == 1


def test_linux_worker_uses_separate_service_and_inherits_provider_config(world, monkeypatch):
    calls = []
    monkeypatch.setattr("cockpit.codex_worker.sys.platform", "linux")
    monkeypatch.setenv("CODEX_HOME", "/private/provider-codex-home")
    monkeypatch.setattr("cockpit.codex_worker.subprocess.run", lambda args, **kwargs:
                        calls.append(args) or subprocess.CompletedProcess(args, 0, "", ""))
    launch(world.db_path, "codex")
    [args] = calls
    assert args[:2] == ["systemd-run", "--user"]
    assert "--scope" not in args
    assert "--setenv=CODEX_HOME" in args and "--setenv=PATH" in args
    assert "/private/provider-codex-home" not in args
    assert args[-2:] == [str(world.db_path), "codex"]
    assert (Path(args[-3]) / "cockpit" / "codex_worker.py").exists()



def test_failed_linux_launch_removes_its_code_copy(world, monkeypatch):
    monkeypatch.setattr("cockpit.codex_worker.sys.platform", "linux")
    monkeypatch.setattr("cockpit.codex_worker.subprocess.run", lambda args, **kwargs:
                        subprocess.CompletedProcess(args, 1, "", "Unit cockpit-codex-x.service was already loaded"))
    with pytest.raises(CodexError, match="already loaded"):
        launch(world.db_path, "codex")
    assert list((world.db_path.parent / "codex-worker-code").iterdir()) == []


def test_launch_refused_while_old_unit_stops_is_tried_again(world, monkeypatch):
    """systemd refuses a worker under the unit name of one still stopping; the caller starts it once that is gone."""
    command = world.checkout / "fake-codex"
    command.write_text(FAKE)
    command.chmod(0o700)
    attempts, processes = [], []

    def start_worker(db_path, command):
        attempts.append(time.monotonic())
        if len(attempts) == 1:
            raise CodexError("Cannot start independent Codex worker: Unit was already loaded or has a fragment file.")
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
        processes.append(subprocess.Popen([sys.executable, "-m", "cockpit.codex_worker", str(db_path), command],
                                          env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                          start_new_session=True))

    monkeypatch.setattr("cockpit.codex_worker.launch", start_worker)
    monkeypatch.setattr("cockpit.codex_worker.LAUNCH_RETRY", 0.1)
    try:
        assert WorkerClient(world.db_path, str(command)).sessions() == []
        assert len(attempts) == 2
    finally:
        for process in processes:
            process.terminate()
            process.wait(timeout=5)

def test_macos_worker_is_a_separate_launchd_job(world, monkeypatch):
    import plistlib

    calls = []
    monkeypatch.setattr("cockpit.codex_worker.sys.platform", "darwin")
    monkeypatch.setattr("cockpit.codex_worker.subprocess.run", lambda args, **kwargs:
                        calls.append(args) or subprocess.CompletedProcess(args, 0, "", ""))
    launch(world.db_path, "codex")
    [args] = calls
    assert args[:2] == ["launchctl", "bootstrap"]
    path = Path(args[-1])
    assert path.stat().st_mode & 0o777 == 0o600
    job = plistlib.loads(path.read_bytes())
    assert job["Label"].startswith("cockpit.codex.")
    assert job["EnvironmentVariables"]["PATH"] == os.environ["PATH"]
    assert job["ProgramArguments"][-2:] == [str(world.db_path), "codex"]


def test_worker_loss_fails_active_turn_without_replaying_it(world, worker):
    client, processes = worker
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(str(socket_path(world.db_path)))
        connection.sendall((json.dumps({"method": "start", "args": [str(world.checkout),
            "test", "work", "gpt-6.1-sol", "high", "standard"]}) + "\n").encode())
    wait_until(client.requests)
    processes[0].terminate()
    processes[0].wait(timeout=5)
    reconnected = WorkerClient(world.db_path, client.command)
    assert reconnected.lookup(SESSION_B).state == "failed"
    assert "worker stopped" in reconnected.lookup(SESSION_B).detail
    assert reconnected.requests() == []
    assert (world.checkout / "executed").read_text() == "once\n"


def fill_listen_queue(path: Path) -> list[socket.socket]:
    """Connections that wait unaccepted until the listener's queue is full, as when the worker is slow to accept."""
    waiting = []
    while True:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(1)
        try:
            connection.connect(str(path))
        except BlockingIOError:
            connection.close()
            return waiting
        waiting.append(connection)
        assert len(waiting) < 1000, "the listen queue never filled"


def test_worker_queues_many_callers_while_slow_to_accept(world):
    from cockpit.codex_worker import WorkerServer
    path = socket_path(world.db_path)
    # The server is made but never accepts, as when a busy host starves its accept loop.
    with WorkerServer(path, agent=None) as _server:
        waiting = fill_listen_queue(path)
        try:
            # socketserver's default queue held 5, and the 7th caller failed with EAGAIN.
            assert len(waiting) > 100
        finally:
            for connection in waiting:
                connection.close()


def test_full_listen_queue_waits_for_room(tmp_path, monkeypatch):
    import threading
    import types
    from cockpit import codex_worker
    refused = threading.Event()

    def sleep(seconds):
        refused.set()
        time.sleep(seconds)

    # The listener makes room only once the client has been refused, so the call passes only by waiting for room.
    monkeypatch.setattr(codex_worker, "time", types.SimpleNamespace(monotonic=time.monotonic, sleep=sleep))
    path = socket_path(tmp_path / "test.db")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        listener.listen(1)
        listener.settimeout(10)
        waiting = fill_listen_queue(path)

        def accept_after_refusal():
            if not refused.wait(timeout=10):
                return
            for _ in waiting:
                listener.accept()[0].close()
            connection, _ = listener.accept()
            with connection, connection.makefile("rwb") as stream:
                stream.readline()
                stream.write(b'{"result": []}\n')

        thread = threading.Thread(target=accept_after_refusal, daemon=True)
        thread.start()
        try:
            assert WorkerClient(tmp_path / "test.db", "unused").sessions() == []
            assert refused.is_set()
        finally:
            thread.join(timeout=10)
            for connection in waiting:
                connection.close()
        assert not thread.is_alive()


class FlakyCodex:
    """A Codex worker that lists once, then cannot be reached."""

    def __init__(self, session) -> None:
        self.listed, self.session = [session], session

    def sessions(self):
        if not self.listed:
            raise CodexError("Codex worker unavailable; no request was sent: [Errno 11] Resource temporarily unavailable")
        return [self.listed.pop()]


def test_pages_keep_last_codex_sessions_when_worker_is_busy(world, caplog):
    from fastapi.testclient import TestClient
    from tests.conftest import ORIGIN, make_session
    item = put_item(world, key="note:busy-worker", title="Busy worker")
    codex = FlakyCodex(make_session(SESSION_B, pid=None, cwd=str(world.checkout)))
    cache = ProviderSessions(None, codex)
    client = TestClient(create_app(world.settings, world.deliverer, cache, world.spend_index, world.codex_index, []),
                        base_url=ORIGIN)
    assert [s.session_id for s in cache.get()] == [SESSION_B]
    response = client.get(f"/api/items/{item['id']}/history", headers=world.agent())
    assert response.status_code == 200
    assert [s.session_id for s in cache.last()] == [SESSION_B]
    errors = [r for r in caplog.records if r.levelname == "ERROR" and "last Codex sessions" in r.getMessage()]
    assert len(errors) == 2
    # Deciding whether a session exists, or how many work for delivery's limit, still needs the worker's answer.
    with pytest.raises(CodexError):
        cache.snapshot(wait=True)
    with pytest.raises(CodexError):
        cache.current()


def test_no_codex_sessions_listed_yet_is_an_error(world):
    codex = FlakyCodex(None)
    codex.listed = []
    with pytest.raises(CodexError):
        ProviderSessions(None, codex).get()
