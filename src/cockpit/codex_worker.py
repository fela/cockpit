"""Local Codex owner, independent of the web server's lifetime.

Only this process owns app-server and its live request IDs. The web process uses
one Unix socket connection per call; losing that connection never repeats a call.
"""

from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import json
import logging
import os
import plistlib
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from cockpit import db, server_logs
from cockpit.codex_agent import CodexAgent, CodexError, mark_interrupted
from cockpit.sessions import Session

log = logging.getLogger(__name__)
METHODS = {"sessions", "lookup", "start", "start_for_message", "send", "stop", "requests", "answer", "log"}
MAX_REQUEST = 4 * 1024 * 1024
# How long a caller waits for room in the worker's full listen queue before the call fails.
CONNECT_WAIT = 5
# How often a caller tries again to start the worker after a start failed.
LAUNCH_RETRY = 1


def socket_path(db_path: Path) -> Path:
    return db_path.parent / "codex.sock"


def launch(db_path: Path, command: str) -> None:
    """Use a separate systemd service on Linux, and a separate launchd job on macOS.

    systemd-run's service mode gives the worker its own cgroup. setsid alone
    leaves children in cockpit.service, where its restart would kill them.
    """
    # Deploy replaces the installed package in place. Keep lazy imports on the
    # worker's original code, while children retain the normal CLI environment.
    releases = db_path.parent / "codex-worker-code"
    releases.mkdir(mode=0o700, parents=True, exist_ok=True)
    release = Path(tempfile.mkdtemp(dir=releases))
    shutil.copytree(Path(__file__).parent, release / "cockpit", ignore=shutil.ignore_patterns("__pycache__"))
    bootstrap = ("import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); "
                 "runpy.run_module('cockpit.codex_worker',run_name='__main__')")
    args = [sys.executable, "-c", bootstrap, str(release), str(db_path), command]
    if sys.platform == "linux":
        unit = "cockpit-codex-" + hashlib.sha256(str(db_path.resolve()).encode()).hexdigest()[:12]
        # Pass the caller's environment by name, keeping values out of argv. In
        # particular CODEX_HOME and proxy settings keep the CLI's model routing.
        env = [f"--setenv={key}" for key in os.environ
               if key not in {"NOTIFY_SOCKET", "INVOCATION_ID", "JOURNAL_STREAM", "SYSTEMD_EXEC_PID"}]
        args = ["systemd-run", "--user", "--collect", "--quiet", f"--unit={unit}",
                "--property=Type=exec", "--expand-environment=no", *env, "--", *args]
        result = subprocess.run(args, capture_output=True, text=True, timeout=15, check=False)
        if result.returncode:
            # The service manager refused the start, so no worker uses this copy.
            shutil.rmtree(release)
            raise CodexError(f"Cannot start independent Codex worker: {result.stderr.strip()}")
    elif sys.platform == "darwin":
        logs = db_path.parent / "logs"
        logs.mkdir(mode=0o700, exist_ok=True)
        # A separate launchd job survives kickstart/bootout of the web job.
        # A fresh label also lets a stopped worker be replaced without unloading
        # any other job. Each launch's plist stays beside its package snapshot.
        label = "cockpit.codex." + release.name
        plist = release / "worker.plist"
        with open(plist, "xb", opener=lambda path, flags: os.open(path, flags, 0o600)) as handle:
            plistlib.dump({"Label": label, "ProgramArguments": args, "RunAtLoad": True,
                          "EnvironmentVariables": dict(os.environ),
                          "StandardOutPath": str(logs / "codex-worker.log"),
                          "StandardErrorPath": str(logs / "codex-worker.log")}, handle)
        result = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)],
                                capture_output=True, text=True, timeout=15, check=False)
        if result.returncode:
            # The service manager refused the start, so no worker uses this copy.
            shutil.rmtree(release)
            raise CodexError(f"Cannot start independent Codex worker: {result.stderr.strip()}")
    else:
        raise CodexError(f"Independent Codex worker is unsupported on {sys.platform}")


class WorkerClient:
    """CodexAgent's web-facing methods, with no ownership of its subprocess."""

    def __init__(self, db_path: Path, command: str) -> None:
        self.db_path, self.command = db_path, command
        self._launch_lock = threading.Lock()

    def owns(self, session_id: str) -> bool:
        conn = db.connect(self.db_path)
        try:
            return conn.execute("SELECT 1 FROM codex_threads WHERE session_id = ?", (session_id,)).fetchone() is not None
        finally:
            conn.close()

    def _connect(self) -> socket.socket:
        """A connection to the running worker. With a timeout set, Python's connect on a Unix socket raises
        BlockingIOError at once when the worker's listen queue is full instead of waiting for room, so a worker
        slow to accept, as under heavy host load, gets CONNECT_WAIT seconds to make room before this raises."""
        deadline = time.monotonic() + CONNECT_WAIT
        while True:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(150)
            try:
                connection.connect(str(socket_path(self.db_path)))
                return connection
            except BlockingIOError:
                connection.close()
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
            except BaseException:
                connection.close()
                raise

    def _ready(self) -> socket.socket:
        try:
            return self._connect()
        except (FileNotFoundError, ConnectionRefusedError):
            pass
        with self._launch_lock, (self.db_path.parent / "codex-launch.lock").open("a") as launch_lock:
            fcntl.flock(launch_lock, fcntl.LOCK_EX)
            try:
                return self._connect()
            except (FileNotFoundError, ConnectionRefusedError):
                pass
            error = self._launch()
            deadline = time.monotonic() + 15
            retry_at = time.monotonic() + LAUNCH_RETRY
            while time.monotonic() < deadline:
                try:
                    return self._connect()
                except (FileNotFoundError, ConnectionRefusedError):
                    time.sleep(0.05)
                # A stopping worker's unit stays loaded for a few seconds, and systemd refuses a new one under its
                # name until it is gone. A launch that failed is tried again until one starts or the wait ends.
                if error is not None and time.monotonic() >= retry_at:
                    error, retry_at = self._launch(), time.monotonic() + LAUNCH_RETRY
            raise CodexError(f"Codex worker did not become ready: {error or 'check the worker log'}")

    def _launch(self) -> Exception | None:
        """The error that kept the worker from starting, or None when it started."""
        try:
            # Deploy holds this same lock while replacing package files.
            # Take a consistent snapshot, including when startup races a deploy.
            with (self.db_path.parent / "deploy.lock").open("a") as deploy_lock:
                fcntl.flock(deploy_lock, fcntl.LOCK_EX)
                launch(self.db_path, self.command)
        except (OSError, subprocess.SubprocessError, CodexError) as exc:
            # Another process's worker may own the unit name, or a stopping one still holds it.
            return exc
        return None

    def _call(self, method: str, args: list):
        try:
            connection = self._ready()
        except (OSError, CodexError) as exc:
            raise CodexError(f"Codex worker unavailable; no request was sent: {exc}") from exc
        with connection:
            try:
                connection.sendall((json.dumps({"method": method, "args": args}) + "\n").encode())
                with connection.makefile("rb") as response:
                    line = response.readline()
                if not line:
                    raise EOFError("worker closed the connection")
                result = json.loads(line)
            except (OSError, EOFError, ValueError) as exc:
                raise CodexError("Delivery uncertain: connection to Codex worker broke; check the thread before retrying") from exc
        if "error" in result:
            raise CodexError(result["error"], session_id=result.get("session_id"))
        return result["result"]

    def sessions(self) -> list[Session]:
        return [Session(**row) for row in self._call("sessions", [])]

    def lookup(self, session_id: str) -> Session | None:
        row = self._call("lookup", [session_id])
        return Session(**row) if row else None

    def start(self, cwd: Path, name: str, text: str, model: str, effort: str, speed: str) -> str:
        return self._call("start", [str(cwd), name, text, model, effort, speed])

    def start_for_message(self, message_id: int, cwd: Path, name: str, text: str, model: str, effort: str, speed: str) -> str:
        return self._call("start_for_message", [message_id, str(cwd), name, text, model, effort, speed])

    def send(self, session_id: str, text: str, restart: bool = False) -> None:
        self._call("send", [session_id, text, restart])

    def stop(self, session_id: str) -> None:
        self._call("stop", [session_id])

    def requests(self) -> list[dict]:
        return self._call("requests", [])

    def answer(self, key: str, result: dict) -> None:
        self._call("answer", [key, result])

    def log(self, session_id: str) -> dict:
        return self._call("log", [session_id])


class WorkerServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    # socketserver's default queue of 5 fills when several pages call at once while the host is busy; the kernel
    # caps this at net.core.somaxconn.
    request_queue_size = 128

    def __init__(self, path: Path, agent: CodexAgent) -> None:
        self.agent = agent
        super().__init__(str(path), Handler)
        os.chmod(path, 0o600)


class Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        # A partial web request cannot hold a worker thread indefinitely.
        self.connection.settimeout(150)
        try:
            line = self.rfile.readline(MAX_REQUEST + 1)
            if not line.endswith(b"\n") or len(line) > MAX_REQUEST:
                raise ValueError("invalid or oversized worker request")
            request = json.loads(line)
            method, args = request["method"], request["args"]
            if method not in METHODS:
                raise ValueError("unknown worker method")
            if method == "start":
                args[0] = Path(args[0])
            elif method == "start_for_message":
                args[1] = Path(args[1])
            assert isinstance(self.server, WorkerServer)
            result = getattr(self.server.agent, method)(*args)
            if isinstance(result, Session):
                result = dataclasses.asdict(result)
            elif method == "sessions":
                result = [dataclasses.asdict(row) for row in result]
            response = {"result": result}
        except Exception as exc:
            log.exception("Codex worker request failed")
            response = {"error": str(exc), "session_id": getattr(exc, "session_id", None)}
        try:
            self.wfile.write((json.dumps(response) + "\n").encode())
        except OSError:
            # Execution and event handling continue when a web deploy drops the
            # caller. There is deliberately no retry or cancellation here.
            log.warning("Codex worker caller disconnected after request execution; delivery may be uncertain")


def serve(db_path: Path, command: str) -> None:
    db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (db_path.parent / "codex-worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        conn = db.connect(db_path)
        try:
            db.migrate(conn)
            _, capture, _ = server_logs.install(db_path, db.now_iso())
            stopped = mark_interrupted(conn)
            if stopped:
                log.error("%d Codex turns lost their worker; check before continuing", stopped)
        finally:
            conn.close()
        path = socket_path(db_path)
        path.unlink(missing_ok=True)
        agent = CodexAgent(db_path, command)
        try:
            with WorkerServer(path, agent) as server:
                server.serve_forever(poll_interval=0.1)
        finally:
            agent.rpc.close()
            path.unlink(missing_ok=True)
            capture.stop()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    serve(Path(sys.argv[1]), sys.argv[2])


if __name__ == "__main__":
    main()
