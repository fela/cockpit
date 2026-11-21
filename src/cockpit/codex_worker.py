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

from cockpit import config, db, server_logs
from cockpit.codex_agent import CodexAgent, CodexError, WorkerRetired, mark_interrupted
from cockpit.sessions import Session

log = logging.getLogger(__name__)
METHODS = {"sessions", "lookup", "start", "start_for_message", "send", "stop", "requests", "answer", "log"}
# The calls that give Codex new work. Before each, an idle worker on older Cockpit code than is installed retires.
NEW_WORK = {"start", "start_for_message", "send"}
MAX_REQUEST = 4 * 1024 * 1024
# How long a caller waits for room in the worker's full listen queue before the call fails.
CONNECT_WAIT = 5
# How often a caller tries again to start the worker after a start failed.
LAUNCH_RETRY = 1
# A predecessor can take two 60 s drains plus up to 15 s to close Codex.
LOCK_WAIT = 150
START_WAIT = 15
# A retiring worker exits once it has had no call open for this long, so calls already queued on its socket get answers.
RETIRE_QUIET = 0.5
# A worker leaves code copies younger than this, which a worker starting at the same time may still be using.
COPY_MIN_AGE = 3600


def socket_path(db_path: Path) -> Path:
    return db_path.parent / "codex.sock"


def code_stamp(package: Path) -> list[tuple[str, int, int]]:
    """Each file of a Cockpit package by path, size and change time. A deploy's install changes it, and a worker's
    copy keeps the times of the package it was copied from."""
    return sorted((str(path.relative_to(package)), stat.st_size, stat.st_mtime_ns)
                  for path in package.rglob("*") if "__pycache__" not in path.parts and path.is_file()
                  for stat in [path.stat()])


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
    # The installed package, which the worker compares with its copy to notice a deploy.
    args = [sys.executable, "-c", bootstrap, str(release), str(db_path), command, str(Path(__file__).parent)]
    if sys.platform == "linux":
        database_id = hashlib.sha256(str(db_path.resolve()).encode()).hexdigest()[:12]
        unit = f"cockpit-codex-{database_id}-{release.name}"
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
            connection = self._wait_for_predecessor()
            if connection is not None:
                return connection
            error = self._launch()
            deadline = time.monotonic() + START_WAIT
            retry_at = time.monotonic() + LAUNCH_RETRY
            while time.monotonic() < deadline:
                try:
                    return self._connect()
                except (FileNotFoundError, ConnectionRefusedError):
                    time.sleep(0.05)
                if error is not None and time.monotonic() >= retry_at:
                    error, retry_at = self._launch(), time.monotonic() + LAUNCH_RETRY
            raise CodexError(f"Codex worker did not become ready: {error or 'check the worker log'}")

    def _wait_for_predecessor(self) -> socket.socket | None:
        """Reuse a socket that comes up, or wait until its previous owner lets go before launching."""
        deadline = time.monotonic() + LOCK_WAIT
        with (self.db_path.parent / "codex-worker.lock").open("a") as lock:
            while True:
                try:
                    return self._connect()
                except (FileNotFoundError, ConnectionRefusedError):
                    pass
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return None
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise CodexError("The previous Codex worker did not release its connection in time")
                    time.sleep(0.05)

    def _launch(self) -> Exception | None:
        """The error that kept the worker from starting, or None when it started."""
        try:
            # Deploy holds this same lock while replacing package files.
            # Take a consistent snapshot, including when startup races a deploy.
            with config.deploy_lock(self.db_path.parent).open("a") as deploy_lock:
                fcntl.flock(deploy_lock, fcntl.LOCK_EX)
                launch(self.db_path, self.command)
        except (OSError, subprocess.SubprocessError, CodexError) as exc:
            return exc
        return None

    def _call(self, method: str, args: list):
        result = self._call_once(method, args)
        if result.get("retired"):
            # The worker found newer Cockpit code installed and exited without doing anything; this starts one on it.
            result = self._call_once(method, args)
        if "error" in result:
            raise CodexError(result["error"], session_id=result.get("session_id"))
        return result["result"]

    def _call_once(self, method: str, args: list) -> dict:
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
        return result

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

    def __init__(self, path: Path, agent: CodexAgent, installed: Path) -> None:
        self.path, self.agent, self.installed = path, agent, installed
        self.own_stamp = code_stamp(Path(__file__).parent)
        self._open_calls = 0
        self._calls_changed = threading.Condition()
        self._retiring = False
        super().__init__(str(path), Handler)
        os.chmod(path, 0o600)

    def outdated(self) -> bool:
        try:
            return code_stamp(self.installed) != self.own_stamp
        except OSError:
            # A deploy is replacing the files; the next worker copies them under the deploy lock.
            log.warning("Could not read the installed Cockpit code; taking it as newer", exc_info=True)
            return True

    def process_request(self, request, client_address) -> None:
        with self._calls_changed:
            self._open_calls += 1
            self._calls_changed.notify_all()
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._calls_changed:
                self._open_calls -= 1
                self._calls_changed.notify_all()

    def wait_for_open_calls(self, quiet: float, timeout: float) -> None:
        """Return once no call has been open for `quiet` seconds, or after `timeout`."""
        deadline = time.monotonic() + timeout
        with self._calls_changed:
            while (left := deadline - time.monotonic()) > 0:
                if self._open_calls:
                    self._calls_changed.wait(left)
                # No call opened or closed during the quiet wait, so none is open.
                elif not self._calls_changed.wait(min(quiet, left)):
                    return
        log.error("Codex worker exits with %d calls still open", self._open_calls)

    def retire(self) -> None:
        """Take no more calls and exit. Removing the socket first sends every later call to a new worker; calls
        already queued on this one get their answers before it stops."""
        with self._calls_changed:
            if self._retiring:
                return
            self._retiring = True
        log.info("Codex is idle and newer Cockpit code is installed; this worker exits for one on that code")
        self.path.unlink(missing_ok=True)

        def stop() -> None:
            self.wait_for_open_calls(RETIRE_QUIET, timeout=60)
            self.shutdown()
        threading.Thread(target=stop, daemon=True).start()


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
            if method in NEW_WORK:
                self.server.agent.retire_if_idle(self.server.outdated)
            result = getattr(self.server.agent, method)(*args)
            if isinstance(result, Session):
                result = dataclasses.asdict(result)
            elif method == "sessions":
                result = [dataclasses.asdict(row) for row in result]
            response = {"result": result}
        except WorkerRetired as exc:
            response = {"error": str(exc), "retired": True}
            self.server.retire()
        except Exception as exc:
            log.exception("Codex worker request failed")
            response = {"error": str(exc), "session_id": getattr(exc, "session_id", None)}
        try:
            self.wfile.write((json.dumps(response) + "\n").encode())
        except OSError:
            # Execution and event handling continue when a web deploy drops the
            # caller. There is deliberately no retry or cancellation here.
            log.warning("Codex worker caller disconnected after request execution; delivery may be uncertain")


def serve(db_path: Path, command: str, installed: Path) -> None:
    db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (db_path.parent / "codex-worker.lock").open("a") as lock:
        # A retiring worker holds the lock until it has exited; any other running worker keeps it.
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return
                time.sleep(0.05)
        _remove_other_copies(db_path)
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
        retired = False
        try:
            with WorkerServer(path, agent, installed) as server:
                server.serve_forever(poll_interval=0.1)
                if server._retiring:
                    # Calls accepted in the last poll still get their answers before Codex closes.
                    server.wait_for_open_calls(0, timeout=60)
                    retired = True
        finally:
            agent.rpc.close()
            if not retired:
                # A retired worker removed its socket already; the path may now be its successor's.
                path.unlink(missing_ok=True)
            capture.stop()


def _remove_other_copies(db_path: Path) -> None:
    """With the worker lock held no other worker runs, so the code copies beside this worker's are unused once
    they are old enough not to belong to a worker still starting."""
    own = Path(__file__).resolve().parents[1]
    releases = (db_path.parent / "codex-worker-code").resolve()
    if own.parent != releases:
        return
    for other in releases.iterdir():
        if other != own:
            try:
                if time.time() - other.stat().st_mtime > COPY_MIN_AGE:
                    shutil.rmtree(other)
            except OSError:
                log.exception("Could not remove the unused Codex worker code copy %s", other)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    serve(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]))


if __name__ == "__main__":
    main()
