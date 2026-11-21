"""Sessions on agents Cockpit drives over the Agent Client Protocol: the Gemini CLI and Grok Build (SPEC SESSION-30).

ACP is JSON-RPC 2.0 on the agent's stdin and stdout. Cockpit starts one agent process per model and session folder,
started in that folder, so what an agent loads from a checkout (Gemini reads its .env) stays with that checkout's
sessions: `initialize`, then `session/new`, then a `session/prompt` per message, which answers when the turn ends.
While it runs, `session/update` notifications stream the agent's words and tool calls, which Cockpit keeps in acp_log
because ACP has no call that lists a session's past items; a call is kept by its kind and title, without its input or
output. `session/request_permission` asks before a tool runs, which reaches the page as a request card shaped like a
Codex one (CodexAgent.requests), showing the command, paths and diffs the request names. The agents reuse their CLI's
own login and settings. Gemini approves file edits by itself and asks for the rest; Grok asks for every call.

The processes are the web server's children, unlike Codex threads, which an independent worker keeps through web
restarts: a restart ends their turns (mark_interrupted fails them), and the session's next message loads it again with
`session/load` where the agent offers it.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cockpit import config, db, patterns
from cockpit.codex_agent import CONTROL_PLANE, CodexError
from cockpit.sessions import SESSION_LOG_PARTS, SESSION_LOG_TEXT_MAX, Session, instruction

log = logging.getLogger(__name__)

# The ACP version Cockpit speaks.
PROTOCOL_VERSION = 1
# How long a request other than a prompt may take before Cockpit gives up on it.
REQUEST_SECONDS = 60
# The most of a tool call's title kept in the session log.
TITLE_MAX = 300
# The most of one diff a permission card shows; a longer one says how much is left out.
DIFF_MAX = 20000
# The method a permission request carries on the page: the request card offers Allow once and Deny for it.
PERMISSION_METHOD = "session/request_permission"


class AcpError(CodexError):
    """An ACP request failed; a timed-out mutation may have reached the agent. A CodexError, so delivery and the
    page treat it as they treat Codex's."""


def agent_args(provider: str, model: str) -> list[str]:
    """The command that starts `provider`'s agent in ACP mode on `model`, found on the service's PATH."""
    if provider == "gemini":
        return ["gemini", "--acp", "--approval-mode", "auto_edit", "--model", model]
    if provider == "grok":
        return ["grok", "agent", *(["--model", model] if model != "default" else []), "stdio"]
    raise ValueError(f"{provider} is not an ACP provider")


class Connection:
    """One agent process: requests and their answers, the agent's own requests and its notifications, read on a
    thread of its own so a slow handler never stalls the pipe."""

    def __init__(self, args: list[str], cwd: Path, on_message: Callable[[Connection, dict[str, Any]], None],
                 name: str) -> None:
        self.args, self.cwd, self.on_message, self.name = args, cwd, on_message, name
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._next_id = 0
        self._waiting: dict[int, Callable[[dict[str, Any]], None]] = {}
        self.process: subprocess.Popen | None = None
        self.capabilities: dict[str, Any] = {}
        # Set once the agent's stdout ends: a process that closed it but lives on answers nothing more.
        self.closed = False

    def start(self) -> None:
        try:
            self.process = subprocess.Popen(
                self.args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                bufsize=1, cwd=str(self.cwd), start_new_session=True,
                env={**os.environ, "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")},
            )
        except OSError as exc:
            raise AcpError(f"cannot start {self.name}: {exc}; check that it is installed and signed in") from exc
        threading.Thread(target=self._read, args=(self.process,), name=f"acp-{self.name}", daemon=True).start()
        answer = self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
            "clientInfo": {"name": "cockpit", "title": "Cockpit", "version": "0.1.0"},
        })
        self.capabilities = answer.get("agentCapabilities") or {}

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None and not self.closed

    def write(self, message: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.poll() is not None or process.stdin is None or self.closed:
            raise AcpError(f"{self.name} has ended; nothing was sent")
        with self._write_lock:
            try:
                process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
                process.stdin.flush()
            except OSError as exc:
                raise AcpError(f"Delivery uncertain: the connection to {self.name} broke") from exc

    def send(self, method: str, params: dict[str, Any], on_answer: Callable[[dict[str, Any]], None]) -> None:
        """Send a request without waiting: `on_answer` gets its answer, or an error once the agent ends."""
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            self._waiting[request_id] = on_answer
        try:
            self.write({"id": request_id, "method": method, "params": params})
        except AcpError:
            with self._lock:
                self._waiting.pop(request_id, None)
            raise

    def request(self, method: str, params: dict[str, Any], timeout: float = REQUEST_SECONDS) -> dict[str, Any]:
        answers: queue.Queue = queue.Queue(maxsize=1)
        self.send(method, params, answers.put_nowait)
        try:
            answer = answers.get(timeout=timeout)
        except queue.Empty as exc:
            raise AcpError(f"Delivery uncertain: {self.name} did not answer {method} in time") from exc
        if "error" in answer:
            error = answer["error"]
            raise AcpError(f"{self.name} refused {method}: {error.get('message', error) if isinstance(error, dict) else error}")
        return answer.get("result") or {}

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.write({"method": method, "params": params})

    def _read(self, process: subprocess.Popen) -> None:
        try:
            assert process.stdout is not None
            for line in process.stdout:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue  # an agent's stray log line on stdout
                if not isinstance(message, dict):
                    continue
                if "method" in message:
                    try:
                        self.on_message(self, message)
                    except Exception:
                        log.exception("handling %s's %s failed", self.name, message.get("method"))
                else:
                    with self._lock:
                        waiting = self._waiting.pop(message.get("id"), None)
                    if waiting is not None:
                        waiting(message)
        finally:
            with self._lock:
                self.closed = True
                waiting, self._waiting = list(self._waiting.values()), {}
            for each in waiting:
                each({"error": {"message": f"Delivery uncertain: {self.name} ended"}})
            self.on_message(self, {"method": "cockpit/disconnected", "params": {}})

    def close(self) -> None:
        process = self.process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _diffs(call: dict[str, Any]) -> list[dict[str, str]]:
    """The file changes a tool call's content proposes, each as its path and a unified diff of it."""
    diffs = []
    for each in call.get("content") or []:
        if not isinstance(each, dict) or each.get("type") != "diff" or not isinstance(each.get("path"), str):
            continue
        path, old, new = each["path"], str(each.get("oldText") or ""), str(each.get("newText") or "")
        diff = "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                            "/dev/null" if each.get("oldText") is None else path, path))
        if len(diff) > DIFF_MAX:
            diff = diff[:DIFF_MAX] + f"\n… {len(diff) - DIFF_MAX} more characters of this diff not shown"
        diffs.append({"path": path, "diff": diff})
    return diffs


def _paths(call: dict[str, Any], diffs: list[dict[str, str]]) -> list[str]:
    """Every file a tool call names: its locations and the files its diffs change, each once."""
    named = [each.get("path") for each in call.get("locations") or [] if isinstance(each, dict)]
    return list(dict.fromkeys(path for path in [*named, *(each["path"] for each in diffs)] if isinstance(path, str)))


def _tool_name(call: dict[str, Any]) -> str:
    """What kind of call it is, as the session view groups calls: its ACP kind, else the first word of its title, as an
    agent that sends no kind (Grok) titles its calls "Execute `…`" or "Write `…`"."""
    title = str(call.get("title") or "").split()
    return str(call.get("kind") or (title[0].lower() if title else "tool"))


def _command(call: dict[str, Any]) -> str:
    """What a tool call does, in its own words: the command it runs, else its input as JSON."""
    raw = call.get("rawInput")
    if isinstance(raw, dict) and isinstance(raw.get("command"), str):
        return raw["command"]
    return json.dumps(raw, ensure_ascii=False) if raw else ""


class AcpAgent:
    """The sessions Cockpit runs on one ACP provider, with CodexAgent's methods, so delivery, the page and the session
    listing treat them as they treat Codex threads."""

    def __init__(self, db_path: Path, provider: str) -> None:
        if provider not in config.ACP_PROVIDERS:
            raise ValueError(f"{provider} is not an ACP provider")
        self.db_path, self.provider = db_path, provider
        self.label = config.PROVIDER_NAMES[provider]
        self._lock = threading.RLock()
        self._ended = threading.Condition(self._lock)
        # One process per model and session folder, started on first use; a start holds _starting until the agent
        # answered `initialize`, so a second caller waits for it rather than using it half started.
        self._connections: dict[tuple[str, str], Connection] = {}
        self._starting = threading.Lock()
        # The connection each session is loaded on, and the sessions whose turn runs, with its start.
        self._loaded: dict[str, Connection] = {}
        self._active: dict[str, str] = {}
        # Per session in a turn: its words so far, those not yet kept as a part, and its tool calls' acp_log rows.
        self._reply: dict[str, list[str]] = {}
        self._unsaid: dict[str, list[str]] = {}
        self._calls: dict[tuple[str, str], int] = {}
        # Sessions being loaded: the agent replays their history, which acp_log holds already.
        self._loading: set[str] = set()
        self._pending: dict[str, dict[str, Any]] = {}
        self._pending_on: dict[str, Connection] = {}
        # Sessions whose turn Stop is cancelling: their permission requests are answered cancelled, never approved.
        self._cancelling: set[str] = set()

    # The database

    def _db(self):
        return db.connect(self.db_path)

    def _save(self, session_id: str, **changes: Any) -> None:
        changes["updated_at"] = db.now_iso_ms()
        conn = self._db()
        try:
            conn.execute(f"UPDATE acp_sessions SET {', '.join(key + ' = ?' for key in changes)} WHERE session_id = ?",
                         [*changes.values(), session_id])
        finally:
            conn.close()

    def _add_part(self, session_id: str, part: dict[str, Any], call_id: str | None = None) -> int:
        conn = self._db()
        try:
            return conn.execute("INSERT INTO acp_log (session_id, call_id, part) VALUES (?, ?, ?)",
                                (session_id, call_id, json.dumps(part, ensure_ascii=False))).lastrowid
        finally:
            conn.close()

    def _change_part(self, row: int, changes: dict[str, Any]) -> None:
        conn = self._db()
        try:
            found = conn.execute("SELECT part FROM acp_log WHERE id = ?", (row,)).fetchone()
            if found is not None:
                conn.execute("UPDATE acp_log SET part = ? WHERE id = ?",
                             (json.dumps(json.loads(found["part"]) | changes, ensure_ascii=False), row))
        finally:
            conn.close()

    def _row(self, session_id: str) -> dict[str, Any] | None:
        conn = self._db()
        try:
            return db.row(conn.execute("SELECT * FROM acp_sessions WHERE session_id = ? AND provider = ?",
                                       (session_id, self.provider)).fetchone())
        finally:
            conn.close()

    def owns(self, session_id: str) -> bool:
        return self._row(session_id) is not None

    def mark_interrupted(self) -> None:
        """At server start, fail the turns the previous server's agents were running: they ended with it."""
        conn = self._db()
        try:
            conn.execute("UPDATE acp_sessions SET state = 'failed', detail = ?, updated_at = ?"
                         " WHERE provider = ? AND state = 'working'",
                         ("Cockpit restarted during this turn; check before continuing", db.now_iso_ms(), self.provider))
        finally:
            conn.close()

    def sessions(self) -> list[Session]:
        conn = self._db()
        try:
            rows = conn.execute("SELECT * FROM acp_sessions WHERE provider = ? ORDER BY updated_at DESC",
                                (self.provider,)).fetchall()
        finally:
            conn.close()
        with self._lock:
            pending = {each["params"]["threadId"] for each in self._pending.values()}
            listed = []
            for r in rows:
                sid = r["session_id"]
                working, blocked = sid in self._active, sid in pending
                connection = self._loaded.get(sid)
                listed.append(Session(
                    id=sid[:8], session_id=sid, name=r["name"],
                    state="blocked" if blocked else "working" if working else (
                        r["state"] if r["state"] in {"failed", "blocked"} else "done"),
                    pid=connection.process.pid if connection is not None and connection.process is not None else None,
                    status="busy" if working else "idle", tempo="blocked" if blocked else "active",
                    job_state="working" if working else ("blocked" if r["state"] == "blocked" else ""), in_flight=[],
                    cwd=r["cwd"], detail=r["detail"],
                    needs=f"Answer the {self.label} request on Cockpit" if blocked else "", intent="",
                    updated_at=r["updated_at"], links=[], effort="", version="", account="", provider=self.provider,
                ))
            return listed

    def lookup(self, session_id: str) -> Session | None:
        return next((each for each in self.sessions() if each.session_id == session_id), None)

    # Starting and messaging sessions

    def _connection(self, model: str, cwd: Path) -> Connection:
        """The live, initialized process for `model` in the folder `cwd`, started if it is not running."""
        key = (model, str(cwd))
        with self._starting:
            with self._lock:
                connection = self._connections.get(key)
            if connection is not None and connection.alive():
                return connection
            if connection is not None:
                connection.close()  # closed its stdout but may live on
            connection = Connection(agent_args(self.provider, model), cwd, self._message, f"{self.label} ({model})")
            try:
                connection.start()
            except Exception:
                connection.close()
                raise
            with self._lock:
                self._connections[key] = connection
            return connection

    def start_for_message(self, message_id: int, cwd: Path, name: str, text: str, model: str, effort: str | None,
                          speed: str) -> str:
        """Start a session for the message `message_id`, attached to it before the prompt goes, even if the web caller
        disappears. `effort` and `speed` are what delivery passes every agent; an ACP agent picks neither."""
        offered = [each for each, _ in config.SESSION_OPTIONS[self.provider]["models"]]
        if model not in offered:
            raise AcpError(f"{self.label} offers no model {model}; pick one of {', '.join(offered)}")
        connection = self._connection(model, cwd)
        result = connection.request("session/new", {"cwd": str(cwd), "mcpServers": []})
        session_id = result.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise AcpError(f"{self.label} started a session without an id; no prompt was sent")
        conn = self._db()
        try:
            with db.transaction(conn):
                conn.execute("INSERT INTO acp_sessions (session_id, provider, name, cwd, model, updated_at)"
                             " VALUES (?, ?, ?, ?, ?, ?)", (session_id, self.provider, name, str(cwd), model, db.now_iso()))
                from cockpit.delivery import _after_send

                message = db.row(conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone())
                if message is None or message["session_id"] is not None:
                    raise AcpError(f"the new {self.label} message is missing or already has a session; no prompt was sent")
                conn.execute("UPDATE messages SET session_id = ? WHERE id = ?", (session_id, message_id))
                if message["item_id"] is not None:
                    _after_send(conn, message, True, session_id)
        finally:
            conn.close()
        try:
            with self._lock:
                if not connection.alive():
                    raise AcpError(f"{self.label} ended; no prompt was sent")
                self._loaded[session_id] = connection
            self.send(session_id, text, restart=False)
        except Exception as exc:
            self._save(session_id, state="failed", detail=str(exc))
            raise AcpError(f"{self.label} session {session_id} was created; {exc}. Continue that session rather than"
                           " starting a duplicate.", session_id=session_id) from exc
        return session_id

    def _ensure_loaded(self, session_id: str, row: dict[str, Any]) -> Connection:
        """The connection the session is loaded on, loading it again with `session/load` after the agent restarted."""
        with self._lock:
            connection = self._loaded.get(session_id)
            if connection is not None and connection.alive():
                return connection
        connection = self._connection(row["model"], Path(row["cwd"]))
        if not connection.capabilities.get("loadSession"):
            raise AcpError(f"{self.label} restarted since this session's last turn and cannot load a past session;"
                           " start a new session")
        with self._lock:
            self._loading.add(session_id)
        try:
            connection.request("session/load", {"sessionId": session_id, "cwd": row["cwd"], "mcpServers": []})
        except AcpError as exc:
            self._save(session_id, state="failed", detail=str(exc))
            raise
        finally:
            with self._lock:
                self._loading.discard(session_id)
        with self._lock:
            self._loaded[session_id] = connection
        return connection

    def send(self, session_id: str, text: str, restart: bool) -> None:
        row = self._row(session_id)
        if row is None:
            raise AcpError(f"{self.label} session {session_id} is not owned by Cockpit")
        if restart:
            self.interrupt(session_id)
        connection = self._ensure_loaded(session_id, row)
        at = _now()
        with self._lock:
            if session_id in self._active:
                raise AcpError(f"{self.label} is still working; wait for the current turn or use Send now")
            self._active[session_id] = db.now_iso()
            self._reply[session_id], self._unsaid[session_id] = [], []
        said = instruction(text, at, False)
        if said is not None:
            self._add_part(session_id, {**said, "kind": "instruction"})
        prompt = (text + CONTROL_PLANE + " Use the cockpit CLI to update items and put steps when you need the user."
                  " Run cockpit --help and cockpit send --help for its contract. Your session UUID is " + session_id + ".")
        try:
            self._save(session_id, state="working")
            connection.send("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": prompt}]},
                            lambda answer: self._turn_ended(session_id, answer))
        except Exception as exc:
            with self._lock:
                self._active.pop(session_id, None)
            self._save(session_id, state="failed", detail=str(exc))
            raise

    def _flush(self, session_id: str) -> None:
        """Keep the agent's words since the last part as a part of their own, before a tool call or the turn's end."""
        with self._lock:
            words = "".join(self._unsaid.get(session_id) or [])
            self._unsaid[session_id] = []
        if words.strip():
            self._add_part(session_id, {"at": _now(), "kind": "says", "text": words[:SESSION_LOG_TEXT_MAX]})

    def _turn_ended(self, session_id: str, answer: dict[str, Any]) -> None:
        """The prompt's answer: the turn ended, by its stop reason, or failed. The session's state is saved before the
        turn counts as over, so whoever waits for its end (interrupt) reads the state it ended in."""
        self._flush(session_id)
        with self._lock:
            started = self._active.get(session_id)
            # The turn's messages, one per stretch of words between tool calls: all of them make the turn's reply, and
            # the last is what the session shows, as for a Codex turn.
            messages = [each.strip() for each in self._reply.get(session_id, []) if each.strip()]
        reply, last = "\n\n".join(messages), (messages[-1] if messages else "")
        if "error" in answer:
            error = answer["error"]
            self._save(session_id, state="failed",
                       detail=str(error.get("message", error) if isinstance(error, dict) else error)[:12000])
        else:
            from cockpit.questions import asks_in

            reason = (answer.get("result") or {}).get("stopReason")
            detail = last[-12000:] if last else (f"{self.label} stopped: {reason}" if reason != "end_turn" else "")
            self._save(session_id, state="blocked" if asks_in(reply) else "done", detail=detail)
        if started is not None:
            conn = self._db()
            try:
                conn.execute("INSERT OR IGNORE INTO session_turns (session_id, started_at, ended_at, reply) VALUES (?, ?, ?, ?)",
                             (session_id, started, db.now_iso_ms(), reply[:12000]))
            finally:
                conn.close()
        with self._ended:
            self._active.pop(session_id, None)
            self._reply.pop(session_id, None)
            self._unsaid.pop(session_id, None)
            self._cancelling.discard(session_id)
            self._calls = {key: row for key, row in self._calls.items() if key[0] != session_id}
            for key in [key for key, each in self._pending.items() if each["params"]["threadId"] == session_id]:
                self._pending.pop(key)
                self._pending_on.pop(key, None)
            self._ended.notify_all()

    def interrupt(self, session_id: str) -> None:
        """Cancel the session's turn: its open permission requests are answered as cancelled, as ACP asks, and from
        here until the turn ends no answer approves one, so an Allow racing Stop loses."""
        with self._lock:
            connection = self._loaded.get(session_id)
            if session_id not in self._active or connection is None:
                return
            self._cancelling.add(session_id)
            keys = [key for key, each in self._pending.items() if each["params"]["threadId"] == session_id]
            open_requests = [(self._pending.pop(key), self._pending_on.pop(key)) for key in keys]
        for pending, on in open_requests:
            try:
                on.write({"id": pending["request_id"], "result": {"outcome": {"outcome": "cancelled"}}})
            except AcpError:
                log.warning("%s ended before its request %s was cancelled", self.label, pending["id"])
        connection.notify("session/cancel", {"sessionId": session_id})
        with self._ended:
            if not self._ended.wait_for(lambda: session_id not in self._active, timeout=30):
                raise AcpError(f"{self.label} has not confirmed the cancelled turn ended; no new prompt was sent")

    def stop(self, session_id: str) -> None:
        self.interrupt(session_id)
        with self._lock:
            self._loaded.pop(session_id, None)

    def log(self, session_id: str) -> dict[str, Any]:
        """The end of the session's conversation in sessions.session_log's shape, for the session view (VIEW-26)."""
        if not self.owns(session_id):
            raise AcpError(f"{self.label} session {session_id} is not owned by Cockpit")
        conn = self._db()
        try:
            total = conn.execute("SELECT count(*) FROM acp_log WHERE session_id = ?", (session_id,)).fetchone()[0]
            rows = conn.execute("SELECT part FROM acp_log WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                                (session_id, SESSION_LOG_PARTS)).fetchall()
        finally:
            conn.close()
        return {"parts": [json.loads(row["part"]) for row in reversed(rows)], "total": total}

    # Permission requests

    def requests(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(each) for each in self._pending.values()]

    def _record(self, kind: str, actor: str, data: dict[str, Any]) -> None:
        """An event in the audit log, which backups keep: names and ids only, never a call's input or output."""
        conn = self._db()
        try:
            with db.transaction(conn):
                patterns.record(conn, kind, actor, None, None, None, data)
        finally:
            conn.close()

    def answer(self, key: str, result: dict[str, Any]) -> None:
        """The user's answer to a permission request: accept picks the agent's allow-once option, decline its
        reject-once option, cancel neither. Only one-time options are ever picked: a request without the one asked
        for, or whose call the card could not show, is refused and stays open."""
        decision = result.get("decision")
        if decision not in {"accept", "decline", "cancel"}:
            raise AcpError("choose accept, decline or cancel")
        with self._lock:
            pending = self._pending.get(key)
            if pending is None:
                raise AcpError(f"this {self.label} request has already ended")
            if pending["params"]["threadId"] in self._cancelling:
                raise AcpError(f"this {self.label} turn is being stopped; its request is cancelled")
            if decision not in pending["params"]["availableDecisions"]:
                raise AcpError(f"{self.label} cannot be answered {decision} here: it offers no one-time option for it,"
                               " or did not say what the call does")
            if decision == "cancel":
                outcome: dict[str, Any] = {"outcome": "cancelled"}
            else:
                kind = "allow_once" if decision == "accept" else "reject_once"
                chosen = next(each for each in pending["options"] if each.get("kind") == kind)
                outcome = {"outcome": "selected", "optionId": chosen["optionId"]}
            self._pending.pop(key)
            connection = self._pending_on.pop(key)
        connection.write({"id": pending["request_id"], "result": {"outcome": outcome}})
        self._record(f"{self.provider}.answered", "user", {"request": key, "session_id": pending["params"]["threadId"],
                                                           "decision": decision})

    # What the agent sends

    def _message(self, connection: Connection, message: dict[str, Any]) -> None:
        method, params = message["method"], message.get("params") or {}
        if method == "cockpit/disconnected":
            self._disconnected(connection)
        elif "id" in message:
            if method == PERMISSION_METHOD:
                self._permission(connection, message["id"], params)
            else:
                # Cockpit offers the agent no files or terminals of its own (initialize said so).
                connection.write({"id": message["id"], "error": {"code": -32601,
                                                                 "message": f"Cockpit does not support {method}"}})
        elif method == "session/update":
            self._update(connection, params.get("sessionId"), params.get("update") or {})

    def _disconnected(self, connection: Connection) -> None:
        """The agent ended: its turns failed, saved so before they count as over, and its sessions load again on their
        next message."""
        with self._lock:
            lost = [sid for sid, each in self._loaded.items() if each is connection]
            ended = [sid for sid in lost if sid in self._active]
        for sid in ended:
            self._save(sid, state="failed", detail=f"{self.label} ended during this turn; check before continuing")
        with self._ended:
            for sid in lost:
                if self._loaded.get(sid) is connection:
                    self._loaded.pop(sid)
            for sid in ended:
                self._active.pop(sid, None)
                self._reply.pop(sid, None)
                self._unsaid.pop(sid, None)
                self._cancelling.discard(sid)
            for key in [key for key, each in self._pending_on.items() if each is connection]:
                self._pending.pop(key, None)
                self._pending_on.pop(key)
            self._ended.notify_all()

    def _permission(self, connection: Connection, request_id: Any, params: dict[str, Any]) -> None:
        """A request to run a tool, which becomes a card on the page while it comes from the process running the
        session's turn, and is answered cancelled otherwise. The card holds what the request says the call does (its
        command or input, the files it names, the diffs it proposes), in memory only. Allow shows only for an
        allow-once option and a call the card can show."""
        session_id = params.get("sessionId")
        call = params.get("toolCall") or {}
        row = self._row(session_id) if isinstance(session_id, str) else None
        options = [each for each in params.get("options") or [] if isinstance(each, dict) and "optionId" in each]
        kinds = {each.get("kind") for each in options}
        command, diffs = _command(call), _diffs(call)
        paths = _paths(call, diffs)
        offered = [*(["accept"] if "allow_once" in kinds and (command or paths) else []),
                   *(["decline"] if "reject_once" in kinds else []), "cancel"]
        key = uuid.uuid4().hex
        pending = {
            "id": key, "request_id": request_id, "method": PERMISSION_METHOD, "provider": self.provider,
            "options": options,
            "params": {"threadId": session_id, "reason": str(call.get("title") or ""), "cwd": row["cwd"] if row else "",
                       "availableDecisions": offered, "command": command, "paths": paths, "diffs": diffs},
        }
        with self._lock:
            ours = (row is not None and self._loaded.get(session_id) is connection and session_id in self._active
                    and session_id not in self._cancelling)
            if ours:
                self._pending[key] = pending
                self._pending_on[key] = connection
        if not ours:
            connection.write({"id": request_id, "result": {"outcome": {"outcome": "cancelled"}}})
            return
        self._record(f"{self.provider}.requested", self.provider, {
            "request": key, "session_id": session_id, "method": PERMISSION_METHOD, "tool": _tool_name(call)})

    def _update(self, connection: Connection, session_id: Any, update: dict[str, Any]) -> None:
        """One streamed update of a turn: the agent's words, or a tool call starting or changing. Updates replayed
        while a session loads, those of sessions Cockpit did not start, and those from another process than the one
        running the session's turn are left out."""
        if not isinstance(session_id, str):
            return
        with self._lock:
            if (session_id in self._loading or session_id not in self._active
                    or self._loaded.get(session_id) is not connection):
                return
        kind = update.get("sessionUpdate")
        home = str(Path.home())
        if kind == "agent_message_chunk":
            content = update.get("content") or {}
            if content.get("type") == "text" and isinstance(content.get("text"), str):
                with self._lock:
                    messages = self._reply.setdefault(session_id, [])
                    if not messages:
                        messages.append("")
                    messages[-1] += content["text"]
                    self._unsaid.setdefault(session_id, []).append(content["text"])
        elif kind == "tool_call":
            self._flush(session_id)
            with self._lock:
                messages = self._reply.get(session_id)
                if messages and messages[-1].strip():
                    messages.append("")  # the words after this call are a message of their own
            call_id = str(update.get("toolCallId") or "")
            row = self._add_part(session_id, {
                "at": _now(), "kind": "call", "id": call_id, "tool_name": _tool_name(update),
                "title": str(update.get("title") or "").replace(home, "~")[:TITLE_MAX],
                "error": update.get("status") == "failed",
            }, call_id)
            with self._lock:
                self._calls[(session_id, call_id)] = row
        elif kind == "tool_call_update":
            call_id = str(update.get("toolCallId") or "")
            with self._lock:
                row = self._calls.get((session_id, call_id))
            if row is None:
                return
            changes: dict[str, Any] = {}
            if update.get("title"):
                changes["title"] = str(update["title"]).replace(home, "~")[:TITLE_MAX]
            if update.get("status") in {"failed", "completed"}:
                changes["error"] = update["status"] == "failed"
            if changes:
                self._change_part(row, changes)


class Agents:
    """Codex and the ACP agents behind CodexAgent's methods, each call going to the one that owns the session, so
    delivery and the page need only one non-Claude agent (`Deliverer.codex`)."""

    def __init__(self, codex: Any | None, acp: dict[str, AcpAgent]) -> None:
        self.codex, self.acp = codex, acp

    def _all(self) -> list[Any]:
        return [*([self.codex] if self.codex is not None else []), *self.acp.values()]

    def _owner(self, session_id: str) -> Any | None:
        return next((agent for agent in self._all() if agent.owns(session_id)), None)

    def owns(self, session_id: str) -> bool:
        return self._owner(session_id) is not None

    def sessions(self) -> list[Session]:
        return [session for agent in self._all() for session in agent.sessions()]

    def lookup(self, session_id: str) -> Session | None:
        agent = self._owner(session_id)
        return agent.lookup(session_id) if agent is not None else None

    def start_for_message(self, message_id: int, cwd: Path, name: str, text: str, model: str, effort: str | None,
                          speed: str, provider: str) -> str:
        agent = self.codex if provider == "codex" else self.acp.get(provider)
        if agent is None:
            raise CodexError(f"{config.PROVIDER_NAMES.get(provider, provider)} is not enabled in setup.toml")
        return agent.start_for_message(message_id, cwd, name, text, model, effort, speed)

    def send(self, session_id: str, text: str, restart: bool) -> None:
        agent = self._owner(session_id)
        if agent is None:
            raise CodexError(f"session {session_id} is not owned by Cockpit")
        agent.send(session_id, text, restart=restart)

    def stop(self, session_id: str) -> None:
        agent = self._owner(session_id)
        if agent is not None:
            agent.stop(session_id)

    def log(self, session_id: str) -> dict[str, Any]:
        agent = self._owner(session_id)
        if agent is None:
            raise CodexError(f"session {session_id} is not owned by Cockpit")
        return agent.log(session_id)

    def requests(self) -> list[dict[str, Any]]:
        return [{"provider": "codex", **each} if agent is self.codex else each
                for agent in self._all() for each in agent.requests()]

    def answer(self, key: str, result: dict[str, Any]) -> None:
        for agent in self._all():
            if any(each["id"] == key for each in agent.requests()):
                agent.answer(key, result)
                return
        raise CodexError("this request has already ended")
