"""Codex sessions owned by Cockpit, driven through the supported stdio app-server protocol.

The user's Codex CLI login and model configuration are reused. Only threads started by
Cockpit are controlled: another terminal or desktop app may already own its live threads.
The independent Codex worker keeps turns and live requests across web restarts.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cockpit.codex_worker import WorkerClient

from cockpit import config, db, patterns
from cockpit.sessions import (
    SESSION_LOG_PARTS,
    SESSION_LOG_RESULT_MAX,
    SESSION_LOG_TEXT_MAX,
    Listing,
    Session,
    instruction,
)

log = logging.getLogger(__name__)


class CodexError(Exception):
    """An app-server request failed; a timed-out mutation may have reached Codex."""

    def __init__(self, message: str, session_id: str | None = None) -> None:
        super().__init__(message)
        self.session_id = session_id


def installed_version(command: str) -> str | None:
    """The version of the npm package that `command` runs; None when the executable is installed another way."""
    found = shutil.which(command)
    manifest = Path(os.path.realpath(found)).parent.parent / "package.json" if found else None
    if manifest is None:
        return None
    try:
        return json.loads(manifest.read_text(encoding="utf-8"))["version"]
    except FileNotFoundError:
        return None


class AppServer:
    """Multiplex responses, notifications and server requests without blocking the reader."""

    def __init__(self, command: str, on_event) -> None:
        self.command, self.on_event = command, on_event
        self._start_lock = threading.Lock()
        self._write_lock = threading.Lock()
        # State and waiters share this lock; pipe writes and event callbacks run outside it.
        self._lock = threading.Lock()
        self._next_id = 0
        self._waiting: dict[int, queue.Queue] = {}
        self.process: subprocess.Popen | None = None
        self._connected = False
        self.version: str | None = None
        self._reader: threading.Thread | None = None

    def start(self) -> subprocess.Popen:
        with self._start_lock:
            with self._lock:
                if self._connected and self.process is not None and self.process.poll() is None:
                    return self.process
            # EOF can end the reader while the child is still alive. Reap that child
            # and finish its disconnect callback before publishing a new connection.
            self.close()
            if self._reader is not None and self._reader.is_alive():
                raise CodexError("Codex is still cleaning up its previous connection; try again shortly")
            try:
                self.version = installed_version(self.command)
                process = subprocess.Popen(
                    [self.command, "app-server", "--enable", "fast_mode", "--enable", "ultrafast_mode"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, bufsize=1,
                    env={**os.environ, "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")},
                )
            except OSError as exc:
                raise CodexError(f"cannot start {self.command} app-server: {exc}; check codex --version and codex login") from exc
            with self._lock:
                self.process = process
                self._connected = True
            self._reader = threading.Thread(target=self._read, args=(process,), daemon=True)
            self._reader.start()
            try:
                self._request("initialize", {"clientInfo": {"name": "cockpit", "title": "Cockpit", "version": "0.1.0"}}, process)
                self._write(process, {"method": "initialized", "params": {}})
            except Exception:
                self.close()
                raise
            return process

    def is_connected(self, process: subprocess.Popen | None) -> bool:
        with self._lock:
            return self._connected and process is self.process and process is not None and process.poll() is None

    def outdated(self) -> bool:
        """Whether another Codex was installed since this app-server started: it keeps the version it started on. A
        missing manifest, as midway through npm's install, is no new version yet."""
        installed = installed_version(self.command)
        running = self.is_connected(self.process)
        return running and installed is not None and installed != self.version

    def write(self, message: dict) -> None:
        self._write(self.process, message)

    def _write(self, process: subprocess.Popen | None, message: dict) -> None:
        with self._write_lock:
            with self._lock:
                if (not self._connected or process is not self.process or process is None
                        or process.poll() is not None or process.stdin is None):
                    raise CodexError("Codex app-server disconnected; no request was sent")
            try:
                process.stdin.write(json.dumps(message) + "\n")
                process.stdin.flush()
            except OSError as exc:
                raise CodexError("Delivery uncertain: connection to Codex broke; check the thread before retrying") from exc

    def request(self, method: str, params: dict) -> dict:
        return self.request_on(self.start(), method, params)

    def request_on(self, process: subprocess.Popen, method: str, params: dict) -> dict:
        """Send on this connection; a dependent request must fail if it disconnected."""
        return self._request(method, params, process)

    def _request(self, method: str, params: dict, process: subprocess.Popen) -> dict:
        answer: queue.Queue = queue.Queue(maxsize=1)
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            self._waiting[request_id] = answer
        try:
            self._write(process, {"id": request_id, "method": method, "params": params})
            try:
                result = answer.get(timeout=60)
            except queue.Empty as exc:
                raise CodexError(f"Delivery uncertain: Codex {method} timed out; check the thread before retrying") from exc
            if "error" in result:
                raise CodexError(str(result["error"].get("message", result["error"])))
            return result.get("result", {})
        finally:
            with self._lock:
                self._waiting.pop(request_id, None)

    def _read(self, process: subprocess.Popen) -> None:
        try:
            assert process.stdout is not None
            for line in process.stdout:
                message = json.loads(line)
                if "method" in message:
                    self.on_event(message)
                else:
                    with self._lock:
                        waiting = self._waiting.get(message.get("id"))
                    if waiting is not None:
                        waiting.put_nowait(message)
        except Exception:
            log.exception("reading Codex app-server failed")
            process.terminate()
        finally:
            with self._lock:
                if self.process is process:
                    self._connected = False
                for waiting in self._waiting.values():
                    if waiting.empty():
                        waiting.put_nowait({"error": {"message": "Delivery uncertain: Codex app-server disconnected"}})
            self.on_event({"method": "cockpit/disconnected", "params": {}})

    def close(self) -> None:
        with self._lock:
            process = self.process
            self._connected = False
        if process is not None:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if self._reader is not None and self._reader is not threading.current_thread():
            self._reader.join(timeout=5)


# The most of a request's own words kept in its event.
CALL_MAX = 2000
APPROVAL_METHODS = {"item/commandExecution/requestApproval", "item/fileChange/requestApproval",
                    "item/permissions/requestApproval", "item/tool/requestUserInput"}


def mark_interrupted(conn) -> int:
    """At worker start, fail turns whose previous owner was lost. Web restarts keep the worker alive."""
    with db.transaction(conn):
        return conn.execute(
            "UPDATE codex_threads SET state = 'failed', detail = ?, updated_at = ? WHERE state = 'working'",
            ("Codex worker stopped during this turn; check before continuing", db.now_iso()),
        ).rowcount


# What send adds after the words of every prompt; the session view leaves it out.
CONTROL_PLANE = "\n\nCockpit is your control plane."


def _log_part(entry: dict[str, Any]) -> dict[str, Any] | None:
    """One thread item of thread/items/list as a part of sessions.session_log: a user message as an instruction, the
    agent's message as its words, a command, file change or MCP call as a call. None for its reasoning and the rest."""
    item = entry.get("item") or {}
    started = entry.get("startedAtMs")
    at = datetime.fromtimestamp(started / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if isinstance(started, int) else ""
    kind = item.get("type")
    if kind == "userMessage":
        text = "\n".join(part.get("text") or "" for part in item.get("content") or [] if part.get("type") == "text")
        found = instruction(text.split(CONTROL_PLANE)[0], at, False)
        return found and {**found, "kind": "instruction"}
    if kind == "agentMessage":
        text = item.get("text") or ""
        return {"at": at, "kind": "says", "text": text[:SESSION_LOG_TEXT_MAX]} if text.strip() else None
    if kind == "commandExecution":
        name, title, text = "Bash", "", item.get("command") or ""
        result = item.get("aggregatedOutput") or ""
        error = item.get("exitCode") not in (0, None) or item.get("status") in ("failed", "declined")
    elif kind == "fileChange":
        changes = item.get("changes") or []
        name, title, text = "Edit", ", ".join(change.get("path", "") for change in changes), ""
        result, error = "\n".join(change.get("diff", "") for change in changes), item.get("status") in ("failed", "declined")
    elif kind == "mcpToolCall":
        name, title, text = f"{item.get('server')}.{item.get('tool')}", "", json.dumps(item.get("arguments"), ensure_ascii=False)
        # A tool that reports its own failure comes back as status "failed" with its result and no error.
        error = bool(item.get("error")) or item.get("status") == "failed"
        result = json.dumps(item.get("error") or item.get("result"), ensure_ascii=False)
    else:
        return None
    if item.get("status") == "declined":
        # A command or change the user refused: its proposed command or diff, marked as never run.
        result = "Declined; not run.\n\n" + result
    home = str(Path.home())
    cut = result if len(result) <= SESSION_LOG_RESULT_MAX else result[:SESSION_LOG_RESULT_MAX] + " …"
    return {"at": at, "kind": "call", "id": item.get("id") or "", "tool_name": name, "title": title.replace(home, "~"),
            "text": text.replace(home, "~")[:SESSION_LOG_TEXT_MAX], "result": cut, "error": error}


# Every Cockpit thread writes inside its folder and reaches the network without asking: the cockpit CLI talks to
# Cockpit's local server, and gh, git fetch and curl need the network too. Without network access each of those
# calls stops on an approval card. Codex keeps .git read-only in workspace-write, so a commit, a restart of a user
# service or a write outside the folder asks to leave the sandbox: Codex's auto review decides those in place of the
# user, and each verdict is logged as a codex.reviewed event.
SANDBOX = {"approvalPolicy": "on-request", "sandbox": "workspace-write", "approvalsReviewer": "auto_review"}
NETWORK = {"sandbox_workspace_write": {"network_access": True}}


def _thread_params(model: str | None, effort: str | None, speed: str) -> dict[str, Any]:
    """A thread's sandbox, model, effort and speed. Older threads keep the user's model and effort defaults.

    The app-server uses priority for Fast; the CLI's service_tier also accepts the alias fast.
    """
    return {
        **SANDBOX,
        **({"model": config.codex_model(model, speed)} if model else {}),
        "serviceTier": {"standard": "default", "fast": "priority", "ultrafast": "ultrafast"}[speed],
        "config": NETWORK | ({"model_reasoning_effort": effort} if effort else {}) | (
            {"features": {"fast_mode": True, "ultrafast_mode": True}} if speed == "ultrafast" else
            {"features": {"fast_mode": True}} if speed == "fast" else {}),
    }


def _check_speed(result: dict[str, Any], speed: str) -> None:
    expected = {"standard": "default", "fast": "priority", "ultrafast": "ultrafast"}[speed]
    applied = result.get("serviceTier") or "default"
    if applied != expected:
        raise CodexError(f"Codex did not apply {speed} speed (it selected {applied}); no prompt was sent. Check the model catalogue and provider account eligibility.")


class CodexAgent:
    def __init__(self, db_path: Path, command: str = "codex") -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._loaded: dict[str, subprocess.Popen] = {}
        self._active: dict[str, tuple[str, str]] = {}
        self._pending: dict[str, dict] = {}
        self._items: dict[str, dict] = {}
        self._ended = threading.Condition(self._lock)
        # Starts, messages and their interrupts go one at a time, so none uses the app-server between
        # _restart_if_updated finding Codex idle and its close. Not self._lock: the reader thread needs that to handle the close's disconnect.
        self._sending = threading.RLock()
        self.rpc = AppServer(command, self.event)

    def _save(self, session_id: str, **changes) -> None:
        conn = db.connect(self.db_path)
        try:
            # To the millisecond: a turn that ends in the second Cockpit's message was sent still ends after it
            # (delivery.archives_due).
            changes["updated_at"] = db.now_iso_ms()
            conn.execute(f"UPDATE codex_threads SET {', '.join(k + ' = ?' for k in changes)} WHERE session_id = ?",
                         [*changes.values(), session_id])
        finally:
            conn.close()

    def owns(self, session_id: str) -> bool:
        conn = db.connect(self.db_path)
        try:
            return conn.execute("SELECT 1 FROM codex_threads WHERE session_id = ?", (session_id,)).fetchone() is not None
        finally:
            conn.close()

    def sessions(self) -> list[Session]:
        conn = db.connect(self.db_path)
        try:
            rows = conn.execute("SELECT * FROM codex_threads ORDER BY updated_at DESC").fetchall()
        finally:
            conn.close()
        with self._lock:
            pending = {p["params"].get("threadId") for p in self._pending.values()}
            return [Session(
                id=r["session_id"][:8], session_id=r["session_id"], name=r["name"],
                state="blocked" if r["session_id"] in pending else (
                    "working" if r["session_id"] in self._active else (
                        r["state"] if r["state"] in {"failed", "blocked"} else "done")),
                pid=self.rpc.process.pid if r["session_id"] in self._loaded and self.rpc.process is not None else None,
                status="busy" if r["session_id"] in self._active else "idle",
                tempo="blocked" if r["session_id"] in pending else "active",
                job_state="working" if r["session_id"] in self._active else (
                    "blocked" if r["state"] == "blocked" else ""), in_flight=[],
                cwd=r["cwd"], detail=r["detail"], needs="Answer the Codex request on Cockpit" if r["session_id"] in pending else "",
                intent="", updated_at=r["updated_at"], links=[], effort="", version="", account="", provider="codex",
            ) for r in rows]

    def lookup(self, session_id: str) -> Session | None:
        return next((s for s in self.sessions() if s.session_id == session_id), None)

    def _restart_if_updated(self) -> None:
        """Restart an app-server older than the installed Codex while no turn or request of Codex waits on it, so each
        new thread and message runs on the new version (SPEC SESSION-15). Its threads resume on their next message."""
        with self._lock:
            idle = not self._active and not self._pending
        if idle and self.rpc.outdated():
            log.info("Codex was updated since its app-server started; restarting it")
            self.rpc.close()

    def start(self, cwd: Path, name: str, text: str, model: str, effort: str, speed: str) -> str:
        """Start a thread on the picked model, effort and speed and send it `text`; resumes keep those picks."""
        with self._sending:
            config.validate_speed("codex", model, speed)
            return self._start(cwd, name, text, model, effort, speed, None)

    def start_for_message(self, message_id: int, cwd: Path, name: str, text: str, model: str, effort: str, speed: str) -> str:
        """Attach the created thread before sending a prompt, even if the web caller disappears."""
        with self._sending:
            config.validate_speed("codex", model, speed)
            return self._start(cwd, name, text, model, effort, speed, message_id)

    def _start(self, cwd: Path, name: str, text: str, model: str, effort: str, speed: str,
               message_id: int | None) -> str:
        self._restart_if_updated()
        connection = self.rpc.start()
        result = self.rpc.request_on(connection, "thread/start", {
            "cwd": str(cwd), **_thread_params(model, effort, speed),
        })
        thread = result["thread"]
        session_id = thread["id"]
        conn = db.connect(self.db_path)
        try:
            with db.transaction(conn):
                conn.execute("INSERT INTO codex_threads (session_id, name, cwd, updated_at, model, effort, speed) VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (session_id, name, str(cwd), db.now_iso(), model, effort, speed))
                if message_id is not None:
                    from cockpit.delivery import _after_send

                    message = db.row(conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone())
                    if message is None or message["session_id"] is not None:
                        raise CodexError("new Codex message is missing or already has a thread; no prompt was sent")
                    conn.execute("UPDATE messages SET session_id = ? WHERE id = ?", (session_id, message_id))
                    if message["item_id"] is not None:
                        _after_send(conn, message, True, session_id)
        finally:
            conn.close()
        try:
            _check_speed(result, speed)
            with self._lock:
                if not self.rpc.is_connected(connection):
                    raise CodexError("Codex app-server disconnected; no prompt was sent")
                self._loaded[session_id] = connection
            self.rpc.request_on(connection, "thread/name/set", {"threadId": session_id, "name": name})
            self.send(session_id, text)
        except Exception as exc:
            self._save(session_id, state="failed", detail=str(exc))
            raise CodexError(f"Codex thread {session_id} was created; {exc}. Continue that thread rather than starting a duplicate.",
                             session_id=session_id) from exc
        return session_id

    def send(self, session_id: str, text: str, restart: bool = False) -> None:
        if not self.owns(session_id):
            raise CodexError(f"Codex thread {session_id} is not owned by Cockpit")
        with self._sending:
            if restart:
                self.interrupt(session_id)
            self._send(session_id, text)

    def _send(self, session_id: str, text: str) -> None:
        self._restart_if_updated()
        with self._lock:
            if session_id in self._active:
                raise CodexError("Codex is still working; wait for the current turn or use Send now")
        connection = self.rpc.start()
        with self._lock:
            loaded = self._loaded.get(session_id) is connection
        if not loaded:
            conn = db.connect(self.db_path)
            try:
                chosen = conn.execute("SELECT model, effort, speed FROM codex_threads WHERE session_id = ?", (session_id,)).fetchone()
            finally:
                conn.close()
            result = self.rpc.request_on(connection, "thread/resume", {
                "threadId": session_id, **_thread_params(chosen["model"], chosen["effort"], chosen["speed"]),
            })
            try:
                _check_speed(result, chosen["speed"])
                with self._lock:
                    # The reader clears loaded threads under this same lock. Check
                    # identity here so its disconnect cannot precede a stale insert.
                    if not self.rpc.is_connected(connection):
                        raise CodexError("Codex app-server disconnected; no prompt was sent")
                    self._loaded[session_id] = connection
            except CodexError as exc:
                self._save(session_id, state="failed", detail=str(exc))
                raise
        prompt = (text + CONTROL_PLANE + " Use the cockpit CLI to update items and put steps when you need"
                  " the user. Run cockpit --help and cockpit send --help for its contract. Your session UUID is "
                  + session_id + ".")
        # Reserve the turn before writing. The response and turn/started notification
        # may arrive in either order, and a fast turn can finish before the response.
        with self._lock:
            if session_id in self._active:
                raise CodexError("Codex is still working; no new prompt was sent")
            self._active[session_id] = ("starting", db.now_iso())
            self._items[session_id] = {}
        try:
            self._save(session_id, state="working")
        except Exception:
            # Nothing went to Codex: the reservation goes, so the thread is not busy for later messages.
            with self._lock:
                if self._active.get(session_id, (None,))[0] == "starting":
                    self._active.pop(session_id)
            raise
        try:
            result = self.rpc.request_on(connection, "turn/start", {"threadId": session_id, "input": [{"type": "text", "text": prompt}]})
            turn_id = result.get("turn", {}).get("id")
            if not turn_id:
                raise CodexError("Delivery uncertain: Codex returned no turn ID")
        except CodexError as exc:
            with self._lock:
                if self._active.get(session_id, (None,))[0] == "starting":
                    self._active.pop(session_id)
            self._save(session_id, state="failed", detail=str(exc))
            raise
        with self._lock:
            if self._active.get(session_id, (None,))[0] == "starting":
                self._active[session_id] = (turn_id, self._active[session_id][1])

    def interrupt(self, session_id: str) -> None:
        with self._lock:
            active = self._active.get(session_id)
        if active is None:
            return
        self.rpc.request("turn/interrupt", {"threadId": session_id, "turnId": active[0]})
        with self._ended:
            if not self._ended.wait_for(lambda: session_id not in self._active, timeout=30):
                raise CodexError("Codex has not confirmed the interrupted turn ended; no new prompt was sent")

    def stop(self, session_id: str) -> None:
        self.interrupt(session_id)
        with self._lock:
            loaded = session_id in self._loaded
        if loaded:
            self.rpc.request("thread/unsubscribe", {"threadId": session_id})
            with self._lock:
                self._loaded.pop(session_id, None)

    def log(self, session_id: str) -> dict[str, Any]:
        """The end of a Cockpit thread's conversation in sessions.session_log's shape, for the session view (SPEC
        VIEW-26): Codex keeps the thread's items, read newest first a page at a time until the parts fill
        SESSION_LOG_PARTS. `total` counts the parts read, more than shown when older items remain."""
        if not self.owns(session_id):
            raise CodexError(f"Codex thread {session_id} is not owned by Cockpit")
        parts: list[dict[str, Any]] = []
        cursor = None
        while True:
            page = self.rpc.request("thread/items/list", {"threadId": session_id, "limit": 100, "sortDirection": "desc",
                                                          "cursor": cursor})
            parts.extend(part for part in map(_log_part, page.get("data") or []) if part is not None)
            cursor = page.get("nextCursor")
            if len(parts) > SESSION_LOG_PARTS or not cursor:
                return {"parts": parts[:SESSION_LOG_PARTS][::-1], "total": len(parts)}

    def requests(self) -> list[dict]:
        with self._lock:
            return [dict(p) for p in self._pending.values()]

    def _record(self, kind: str, actor: str, pending: dict, data: dict[str, Any]) -> None:
        """Log a request for the user's approval or its answer (SPEC PERM-20): the pending request itself lives in
        memory and goes when it ends."""
        self._log(kind, actor, {"request": pending["id"], "session_id": pending["params"].get("threadId"),
                                "method": pending["method"]} | data)

    def _log(self, kind: str, actor: str, data: dict[str, Any]) -> None:
        conn = db.connect(self.db_path)
        try:
            with db.transaction(conn):
                patterns.record(conn, kind, actor, None, None, None, data)
        finally:
            conn.close()

    def answer(self, key: str, result: dict) -> None:
        """The user's answer to a pending request, sent to Codex."""
        with self._lock:
            pending = self._pending.get(key)
            if pending is None:
                raise CodexError("this Codex request has already ended")
            decision = result.get("decision")
            method, params = pending["method"], pending["params"]
            if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
                if not isinstance(result.get("decision"), str) or result["decision"] not in {"accept", "decline", "cancel"}:
                    raise CodexError("choose accept, decline or cancel")
                available = params.get("availableDecisions")
                if available is not None and result["decision"] not in available:
                    raise CodexError("that decision is not offered by Codex")
                result = {"decision": result["decision"]}
            elif method == "item/permissions/requestApproval":
                if not isinstance(result.get("decision"), str) or result["decision"] not in {"accept", "decline"}:
                    raise CodexError("choose accept or decline")
                result = {"permissions": params["permissions"] if result["decision"] == "accept" else {}, "scope": "turn"}
            elif method == "item/tool/requestUserInput":
                answers = result.get("answers")
                questions = {q["id"] for q in params.get("questions", [])}
                if (not isinstance(answers, dict) or set(answers) != questions or any(
                    not isinstance(v, dict) or not isinstance(v.get("answers"), list) or not v["answers"]
                    or any(not isinstance(a, str) or not a.strip() for a in v["answers"]) for v in answers.values()
                )):
                    raise CodexError("answer every Codex question")
                result = {"answers": answers}
            self.rpc.write({"id": pending["request_id"], "result": result})
            del self._pending[key]
            self._record("codex.answered", "user", pending, {"decision": decision})

    def event(self, message: dict[str, Any]) -> None:
        method, params = message["method"], message.get("params", {})
        session_id = params.get("threadId")
        with self._ended:
            if method == "cockpit/disconnected":
                for sid in self._active:
                    self._save(sid, state="failed", detail="Codex disconnected during this turn; check before continuing")
                self._active.clear()
                self._loaded.clear()
                self._pending.clear()
                self._ended.notify_all()
            elif "id" in message:
                if method not in APPROVAL_METHODS:
                    self.rpc.write({"id": message["id"], "error": {"code": -32601,
                                   "message": f"Cockpit does not support {method}; request declined"}})
                    return
                key = uuid.uuid4().hex
                item = self._items.get(session_id, {}).get(params.get("itemId"))
                self._pending[key] = {"id": key, "request_id": message["id"], "method": method,
                                      "params": params | ({"item": item} if item else {})}
                self._record("codex.requested", "codex", self._pending[key], {
                    "call": json.dumps({k: v for k, v in params.items() if k not in ("threadId", "turnId", "itemId")},
                                       ensure_ascii=False)[:CALL_MAX]})
            elif method == "turn/started":
                self._active[session_id] = (params["turn"]["id"], db.now_iso())
                self._items[session_id] = {}
                self._save(session_id, state="working")
            elif method in {"item/started", "item/completed"}:
                item = params.get("item", {})
                items = self._items.setdefault(session_id, {})
                if method == "item/started" or item.get("type") == "agentMessage":
                    # A request names the item under way; the turn's reply is its agent messages.
                    items[item.get("id")] = item
                else:
                    # A finished tool call, with its whole output, is needed no more: a long turn would hold them all.
                    items.pop(item.get("id"), None)
                if method == "item/completed" and item.get("type") == "agentMessage":
                    self._save(session_id, detail=item.get("text", "")[-12000:])
            elif method == "turn/completed":
                turn = params["turn"]
                active = self._active.pop(session_id, None)
                failed = turn.get("status") == "failed"
                reply = "\n\n".join(i.get("text", "") for i in self._items.get(session_id, {}).values()
                                      if i.get("type") == "agentMessage")
                if failed:
                    self._save(session_id, state="failed", detail=(turn.get("error") or {}).get("message", "Codex turn failed"))
                else:
                    from cockpit.questions import asks_in

                    self._save(session_id, state="blocked" if asks_in(reply) else "done")
                if active:
                    conn = db.connect(self.db_path)
                    try:
                        conn.execute("INSERT OR IGNORE INTO session_turns (session_id, started_at, ended_at, reply) VALUES (?, ?, ?, ?)",
                                     (session_id, active[1], db.now_iso_ms(), reply[:12000]))
                    finally:
                        conn.close()
                self._items.pop(session_id, None)
                self._pending = {k: p for k, p in self._pending.items() if p["params"].get("threadId") != session_id}
                self._ended.notify_all()
            elif method == "item/autoApprovalReview/completed":
                review = params.get("review", {})
                self._log("codex.reviewed", "codex", {
                    "session_id": session_id, "status": review.get("status"), "risk": review.get("riskLevel"),
                    "rationale": (review.get("rationale") or "")[:CALL_MAX],
                    "action": json.dumps(params.get("action"), ensure_ascii=False)[:CALL_MAX]})
            elif method == "serverRequest/resolved":
                self._pending = {k: p for k, p in self._pending.items()
                                 if not (p["request_id"] == params.get("requestId") and p["params"].get("threadId") == session_id)}


class ProviderSessions:
    """Merge enabled providers while preserving errors and last known Claude and Codex state.

    `get`, `last` and `snapshot(wait=False)` show what is there, so when the Codex worker cannot answer they keep
    the last Codex sessions it listed and log an error. `current` counts the sessions at work for delivery's limit
    and `snapshot(wait=True)` decides whether a session exists, so both raise; so does every call before the
    worker's first listing, when "none yet" would read as "none"."""

    def __init__(self, claude, codex: CodexAgent | WorkerClient | None) -> None:
        self.claude, self.codex = claude, codex
        self._codex_last: list[Session] | None = None

    def get(self) -> list[Session]:
        return self.snapshot(wait=False).sessions

    def current(self) -> list[Session]:
        claude = self.claude.get() if self.claude is not None else []
        return claude + self._codex(keep_last=False)

    def last(self) -> list[Session]:
        claude = self.claude.last() if self.claude is not None else []
        return claude + self._codex(keep_last=True)

    def snapshot(self, wait: bool) -> Listing:
        claude = self.claude.snapshot(wait) if self.claude is not None else Listing([], [], [], frozenset())
        codex = self._codex(keep_last=not wait)
        return Listing(claude.sessions + codex, claude.warnings, claude.account_problems, claude.failed)

    def _codex(self, keep_last: bool) -> list[Session]:
        if self.codex is None:
            return []
        try:
            listed = self.codex.sessions()
        except CodexError as exc:
            if not keep_last or self._codex_last is None:
                raise
            log.error("Listing Codex sessions failed; showing the last Codex sessions listed: %s", exc)
            return self._codex_last
        self._codex_last = listed
        return listed

    def invalidate(self) -> None:
        if self.claude is not None:
            self.claude.invalidate()
