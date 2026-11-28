"""Sessions on agents Cockpit drives over the Agent Client Protocol: Google's Antigravity CLI (`agy`), which is
Cockpit's "gemini" provider, and Grok Build (SPEC SESSION-30).

ACP is JSON-RPC 2.0 on the agent's stdin and stdout. Cockpit starts one agent process per session folder, started in
that folder, so what an agent loads from a checkout (an agent can read its .env) stays with that checkout's sessions.
Antigravity's process serves that folder's sessions on every model, since each session sets its own model; Grok takes
its model on its command line, so it gets one process per model and folder. On a process: `initialize`, then
`session/new`, then a `session/prompt` per message, which answers when the turn ends.
While it runs, `session/update` notifications stream the agent's words and tool calls, which Cockpit keeps in acp_log
because ACP has no call that lists a session's past items; a call is kept by its kind and title, without its input or
output. `session/request_permission` asks before a tool runs, which reaches the page as a request card shaped like a
Codex one (CodexAgent.requests), showing the command, paths and diffs the request names. The agents reuse their CLI's
own login and settings (agent_env), and ask before the tool calls those settings leave to the user.

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
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cockpit import config, db, patterns, safe_shell
from cockpit.codex_agent import CONTROL_PLANE, CodexError
from cockpit.sessions import SESSION_LOG_PARTS, SESSION_LOG_TEXT_MAX, Session, instruction

log = logging.getLogger(__name__)

# The ACP version Cockpit speaks.
PROTOCOL_VERSION = 1
# How long a request other than a prompt may take before Cockpit gives up on it.
REQUEST_SECONDS = 60
# What the session log says of a call in place of its title, which can hold the command and its secrets (the Gemini CLI
# titles a shell call with its command): the call's ACP status in words.
STATUS_LABELS = {"pending": "pending", "in_progress": "running", "completed": "done", "failed": "failed"}
# The most of one diff a permission card shows; a longer one says how much is left out.
DIFF_MAX = 20000
# The largest file a diff reads whole (_whole), in bytes.
FILE_MAX = 1_000_000
# The method a permission request carries on the page: the request card offers Allow once and Deny for it.
PERMISSION_METHOD = "session/request_permission"
# Antigravity asks the user a question as a permission request whose tool call id starts with QUESTION_PREFIX, its
# options the answers (as T3 Code reads it, apps/server/src/provider/acp/AntigravityAcpSupport.ts). It reaches the
# page as QUESTION_METHOD, the method of a Codex question, so the card shows the question and its answers.
QUESTION_PREFIX = "interaction_"
QUESTION_METHOD = "item/tool/requestUserInput"
# The JSON-RPC error code ACP answers with when the agent is not signed in.
AUTH_REQUIRED = -32000
# Antigravity can go on after a turn ended, when a task it left running finishes: those updates make a late turn of
# their own, which its usage_update ends, or this many seconds without an update, as T3 Code waits.
LATE_QUIET_SECONDS = 15 * 60
# The updates that start a late turn: the agent's words and a new tool call. Others (its usage, its commands) do not.
LATE_UPDATES = frozenset({"agent_message_chunk", "tool_call"})


@dataclass
class LateTurn:
    """One late turn (LATE_QUIET_SECONDS): its own object, so a quiet watch acts only on the turn it was started for,
    with when the agent last sent one of its updates."""

    last: float


class AcpError(CodexError):
    """An ACP request failed; a timed-out mutation may have reached the agent. A CodexError, so delivery and the
    page treat it as they treat Codex's."""


def _refusal(name: str, error: Any) -> str:
    """What an agent's error answer says, in words: a sign-in it lacks as what to do about it."""
    message = str(error.get("message", error) if isinstance(error, dict) else error)
    if isinstance(error, dict) and (error.get("code") == AUTH_REQUIRED or "authentication required" in message.lower()):
        return (f"{name} is not signed in on this machine, so it ran nothing: sign its CLI in on the machine Cockpit"
                " runs on, then send this again")
    return message


def offered_models(result: dict[str, Any]) -> list[tuple[str, str]] | None:
    """The models a session/new or session/load answer offers, each as (id, label), from its `model` config option,
    whose choices may come in groups; None when it has no such option."""
    option = next((each for each in result.get("configOptions") or []
                   if isinstance(each, dict) and each.get("id") == "model"), None)
    if option is None or option.get("type") != "select":
        return None
    choices = [inner for each in option.get("options") or [] if isinstance(each, dict)
               for inner in ([each] if "value" in each else each.get("options") or []) if isinstance(inner, dict)]
    return [(choice["value"], str(choice.get("name") or "").strip() or choice["value"])
            for choice in choices if isinstance(choice.get("value"), str) and choice["value"]]


def agent_args(provider: str, model: str) -> list[str]:
    """The command that starts `provider`'s agent in ACP mode on `model`, found on the service's PATH. Antigravity
    starts the same way on every model: each session sets its own (AcpAgent._choose_model)."""
    if provider == "gemini":
        return ["agy", "acp"]
    if provider == "grok":
        return ["grok", "agent", *(["--model", model] if model != "default" else []), "stdio"]
    raise ValueError(f"{provider} is not an ACP provider")


# Google credentials and Antigravity settings a launch drops from the service's environment, so Antigravity signs in
# with the account its own sign-in stored, as T3 Code launches it (apps/server/src/provider/antigravityAuthSupport.ts).
AGY_DROPPED_ENV = frozenset({
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION", "GOOGLE_CLOUD_QUOTA_PROJECT", "GOOGLE_GENAI_USE_VERTEXAI", "GCLOUD_PROJECT",
    "CLOUDSDK_CORE_PROJECT", "AGY_ACP_CCPA_PROJECT", "AGY_ACP_ENABLE_OAUTH", "GEMINI_HOME",
    "AGY_ACP_FORCE_FILE_STORAGE", "AGY_CLI_DISABLE_AUTO_UPDATE", "BROWSER", "PYTHONUNBUFFERED", "ELECTRON_RUN_AS_NODE",
    "CHROME_CRASHPAD_PIPE_NAME",
})


def agent_env(provider: str, environ: dict[str, str]) -> dict[str, str]:
    """The environment `provider`'s agent starts with: the service's, with Cockpit's Python first on PATH. Antigravity
    also gets T3 Code's launch settings. Without a keyring it keeps its Google sign-in in ~/.gemini, which the 1.5 CLI
    does only when it believes it runs over SSH, hence SSH_CONNECTION; it stays at its installed version."""
    env = {**environ, "PATH": str(Path(sys.executable).parent) + os.pathsep + environ.get("PATH", "")}
    if provider != "gemini":
        return env
    env = {key: value for key, value in env.items() if key.upper() not in AGY_DROPPED_ENV}
    return {**env, "SSH_CONNECTION": environ.get("SSH_CONNECTION") or "127.0.0.1 0 127.0.0.1 0",
            "AGY_ACP_FORCE_FILE_STORAGE": "1", "AGY_CLI_DISABLE_AUTO_UPDATE": "1", "PYTHONUNBUFFERED": "1",
            "ELECTRON_RUN_AS_NODE": "1"}


class Connection:
    """One agent process: requests and their answers, the agent's own requests and its notifications, read on a
    thread of its own so a slow handler never stalls the pipe."""

    def __init__(self, args: list[str], env: dict[str, str], cwd: Path,
                 on_message: Callable[[Connection, dict[str, Any]], None], name: str, temp_dir: Path | None) -> None:
        """`temp_dir` is the process's own temporary folder, removed once it ended, or None for none."""
        self.args, self.env, self.cwd, self.on_message, self.name = args, env, cwd, on_message, name
        self.temp_dir = temp_dir
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
                env=self.env,
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
            raise AcpError(f"{self.name} refused {method}: {_refusal(self.name, answer['error'])}")
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
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass  # closed its stdout but lives on: close() removes its folder once it ends
            else:
                self._remove_temp()

    def close(self) -> None:
        process = self.process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        self._remove_temp()

    def _remove_temp(self) -> None:
        if self.temp_dir is not None:
            shutil.rmtree(self.temp_dir, ignore_errors=True)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _diffs(call: dict[str, Any], folder: str) -> list[dict[str, str]]:
    """The file changes a tool call's content proposes, each as its path and a unified diff of it, measured on the whole
    file where it can be read inside the session's folder (_whole), so the diff has the file's line numbers and the
    lines around the change."""
    diffs = []
    for each in call.get("content") or []:
        if not isinstance(each, dict) or each.get("type") != "diff" or not isinstance(each.get("path"), str):
            continue
        path, old, new = each["path"], each.get("oldText"), str(each.get("newText") or "")
        whole = _whole(path, old, new, folder)
        if whole is not None:
            old, new = whole
        # Lines end at "\n" alone, as in git; str.splitlines would also end them at a form feed or a lone "\r".
        before, after = (re.findall(r"[^\n]*\n|[^\n]+", str(old or "")), re.findall(r"[^\n]*\n|[^\n]+", new))
        created = old is None and not (os.path.isabs(path) and os.path.lexists(path))
        lines = difflib.unified_diff(before, after, "/dev/null" if created else path, path)
        # In a diff of the whole file, a last line without a newline gets the standard marker, so a change to the
        # final newline alone shows. A diff of the call's own text shows each line on its own.
        marker = "\\ No newline at end of file\n" if whole is not None or old is None else ""
        diff = "".join(line if line.endswith("\n") else line + "\n" + marker for line in lines)
        if len(diff) > DIFF_MAX:
            diff = diff[:DIFF_MAX] + f"\n… {len(diff) - DIFF_MAX} more characters of this diff not shown"
        diffs.append({"path": path, "diff": diff})
    return diffs


def _whole(path: str, old: Any, new: str, folder: str) -> tuple[str, str] | None:
    """The whole file before and after a change, where the change names only part of it: Antigravity's edit sends the
    text it replaces and its replacement, and its write sends no old text even when it overwrites a file. None when the
    path is not absolute, lies outside `folder`, the session's own folder, once links resolve (the card would otherwise
    show any file the agent names, such as a key), the file is missing, unreadable as text or over FILE_MAX bytes, or
    holds the replaced text other than exactly once; the diff then shows the change as the call sends it."""
    if not os.path.isabs(path):
        return None
    real, base = os.path.realpath(path), os.path.realpath(folder)
    if real == base or os.path.commonpath([real, base]) != base:
        return None
    *folders, name = real.split(os.sep)[1:]
    try:
        # The file opens one part of its real path at a time from the root, each with O_NOFOLLOW, so a folder or file
        # swapped for a link after the check above, `folder` itself included, ends the read. O_NONBLOCK, since opening
        # a pipe waits for a writer, and O_NOCTTY, so a terminal never becomes Cockpit's; only a regular file is read.
        # newline="" keeps the file's line endings, so a change of them shows in the diff.
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in folders:
                parent, fd = fd, os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(parent)
            parent, fd = fd, os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(parent)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > FILE_MAX:
                return None
            with open(fd, encoding="utf-8", newline="", closefd=False) as file:
                text = file.read()
        finally:
            os.close(fd)
    except (OSError, ValueError):
        return None
    if old is None:
        return text, new
    if not isinstance(old, str) or not old or text.count(old) != 1:
        return None
    return text, text.replace(old, new)


def _paths(call: dict[str, Any], diffs: list[dict[str, str]]) -> list[str]:
    """Every file a tool call names: its locations and the files its diffs change, each once."""
    named = [each.get("path") for each in call.get("locations") or [] if isinstance(each, dict)]
    return list(dict.fromkeys(path for path in [*named, *(each["path"] for each in diffs)] if isinstance(path, str)))


def _tool_name(call: dict[str, Any]) -> str:
    """What kind of call it is, as the session view groups calls: its ACP kind, else the first word of its title, as an
    agent that sends no kind (Grok) titles its calls "Execute `…`" or "Write `…`"."""
    title = str(call.get("title") or "").split()
    return str(call.get("kind") or (title[0].lower() if title else "tool"))


# The fields of Antigravity's shell call input that the card shows (CommandLine, toolSummary or explanation, Cwd,
# BypassSandbox) or that only time the call. A call with any other field keeps its whole input on the card, since Allow
# approves all of it.
_NESTED_KEYS = {"CommandLine", "toolSummary", "explanation", "Cwd", "BypassSandbox", "WaitMsBeforeAsync",
                "NotificationTimeoutSeconds", "Blocking"}


def _parsed(call: dict[str, Any]) -> dict[str, Any]:
    """Antigravity's shell call input, which it sends either as the call's input itself (`CommandLine` and the rest at
    its top) or as a JSON object inside the input's `command`; empty for any other call."""
    raw = call.get("rawInput")
    if isinstance(raw, dict) and isinstance(raw.get("CommandLine"), str):
        return raw
    if not (isinstance(raw, dict) and isinstance(raw.get("command"), str) and raw["command"].startswith("{")):
        return {}
    try:
        nested = json.loads(raw["command"])
    except ValueError:
        return {}
    return nested if isinstance(nested, dict) else {}


def _nested(call: dict[str, Any]) -> dict[str, Any]:
    """Antigravity's shell call input (_parsed): `CommandLine`, `toolSummary` (what the call is for, in a few words),
    sometimes its own `Cwd`, `BypassSandbox` (true when auto mode asks to run the command outside Antigravity's
    sandbox), and its timing settings. Empty for any other call, and for one with a field outside _NESTED_KEYS."""
    nested = _parsed(call)
    if not (isinstance(nested.get("CommandLine"), str) and set(nested) <= _NESTED_KEYS):
        return {}
    if "Cwd" in nested and not isinstance(nested["Cwd"], str):
        return {}
    if "BypassSandbox" in nested and not isinstance(nested["BypassSandbox"], bool):
        return {}
    return nested


def _outside_sandbox(call: dict[str, Any]) -> bool:
    """Whether Antigravity's auto mode asks to run this shell call outside its sandbox (`BypassSandbox: true`). It reads
    the input before _nested, so a call whose other fields keep the whole input on the card still says so."""
    parsed = _parsed(call)
    return isinstance(parsed.get("CommandLine"), str) and parsed.get("BypassSandbox") is True


def _shown_by_diff(call: dict[str, Any]) -> bool:
    """Whether a call is Antigravity's file edit or write and its diff shows all of its input: the file (Path), the text
    the edit replaces (Target) and the text that takes its place (Replacement, or a write's Content). The card then
    shows the diff alone. A call with any other field keeps its whole input on the card, as with _NESTED_KEYS."""
    raw = call.get("rawInput")
    if not (isinstance(raw, dict) and isinstance(raw.get("Path"), str)):
        return False
    if set(raw) == {"Path", "Target", "Replacement"} and isinstance(raw["Target"], str):
        old, new = raw["Target"], raw["Replacement"]
    elif set(raw) == {"Path", "Content"}:
        old, new = None, raw["Content"]
    else:
        return False
    if not isinstance(new, str):
        return False
    return any(isinstance(each, dict) and each.get("type") == "diff" and each.get("path") == raw["Path"]
               and each.get("oldText") == old and each.get("newText") == new for each in call.get("content") or [])


def _cwd(call: dict[str, Any], session_cwd: str) -> str:
    """The folder a call runs in: the `Cwd` Antigravity names in a shell call's input, also when the card shows that
    input whole, else the session's folder."""
    nested = _parsed(call)
    cwd = nested.get("Cwd")
    return cwd if isinstance(nested.get("CommandLine"), str) and isinstance(cwd, str) and cwd else session_cwd


def _command(call: dict[str, Any]) -> str:
    """What a tool call does, in its own words: the command it runs, else its input as JSON. The Gemini CLI sends a
    shell call with no input, kind `execute` and the command as its title (its ShellToolInvocation.getDisplayTitle);
    Antigravity nests its command line in JSON (_nested). Empty for a file edit or write its diff shows whole
    (_shown_by_diff)."""
    raw = call.get("rawInput")
    if _shown_by_diff(call):
        return ""
    if nested := _nested(call):
        return nested["CommandLine"]
    if isinstance(raw, dict) and isinstance(raw.get("command"), str):
        return raw["command"]
    title = call.get("title")
    if not raw and call.get("kind") == "execute" and isinstance(title, str) and title.strip():
        return title
    return json.dumps(raw, ensure_ascii=False) if raw else ""


def _reason(call: dict[str, Any], command: str) -> str:
    """What the card says the call is for: Antigravity's summary of a shell call, else the call's title, unless the
    title is only the command again, as Antigravity's and the Gemini CLI's shell calls title it. Antigravity titles a
    file edit or write "File notes.txt", so the card names what it does to the file: "Edit notes.txt", "Write notes.txt"."""
    if _shown_by_diff(call):
        return f"{'Edit' if 'Target' in call['rawInput'] else 'Write'} {Path(call['rawInput']['Path']).name}"
    nested = _nested(call)
    for summary in (nested.get("toolSummary"), nested.get("explanation")):
        if isinstance(summary, str) and summary.strip():
            return " ".join(summary.split())
    title = str(call.get("title") or "")
    return "" if title.strip() == command.strip() else title


def _unique_labels(answers: list[dict[str, str]]) -> None:
    """Give every answer a label no other answer has, in place: a label only one answer has stays, and answers sharing
    one get their id after it, "File (b)", with a count after the id while that is taken too, "File (a, 2)"."""
    names = [each["label"] for each in answers]
    used = {name for name in names if names.count(name) == 1}
    for each in answers:
        if names.count(each["label"]) == 1:
            continue
        label, count = f"{each['label']} ({each['value']})", 2
        while label in used:
            label, count = f"{each['label']} ({each['value']}, {count})", count + 1
        used.add(label)
        each["label"] = label


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
        # One process per command (agent_args) and session folder, started on first use; a start holds _starting until
        # the agent answered `initialize`, so a second caller waits for it rather than using it half started.
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
        # Sessions being stopped: from Stop's interrupt until it lets the session go, no late turn starts, so no
        # permission request in between is allowed.
        self._stopping: set[str] = set()
        self._pending: dict[str, dict[str, Any]] = {}
        self._pending_on: dict[str, Connection] = {}
        # Sessions whose turn Stop is cancelling: their permission requests are answered cancelled, never approved.
        self._cancelling: set[str] = set()
        # Sessions in a late turn (LATE_QUIET_SECONDS).
        self._late: dict[str, LateTurn] = {}
        # Held while an update or permission request is handled and while a turn ends, so an update arriving as a turn
        # ends is either part of that turn or starts the next one. Taken before _lock, never while holding it.
        self._turn_lock = threading.RLock()

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

    def _temp_root(self) -> Path:
        """Where each Antigravity process gets a temporary folder of its own: its bundle unpacks about 1 GB there on
        every start, which T3 Code also keeps apart per process."""
        return self.db_path.parent / "agy-tmp"

    def mark_interrupted(self) -> None:
        """At server start, fail the turns the previous server's agents were running: they ended with it. Their
        temporary folders go too."""
        if self.provider == "gemini" and self._temp_root().is_dir():
            for each in self._temp_root().iterdir():
                shutil.rmtree(each, ignore_errors=True)
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
        """The live, initialized process for `model` in the folder `cwd`, started if it is not running. Models that
        start the same command (all of Antigravity's) share one."""
        args = agent_args(self.provider, model)
        key = (" ".join(args), str(cwd))
        with self._starting:
            with self._lock:
                connection = self._connections.get(key)
            if connection is not None and connection.alive():
                return connection
            if connection is not None:
                connection.close()  # closed its stdout but may live on
            env, temp = agent_env(self.provider, dict(os.environ)), None
            if self.provider == "gemini":
                self._temp_root().mkdir(parents=True, exist_ok=True)
                temp = Path(tempfile.mkdtemp(prefix="agy-", dir=self._temp_root()))
                env = {**env, "TMPDIR": str(temp)}
            connection = Connection(args, env, cwd, self._message, self.label, temp)
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
        conn = self._db()
        try:
            offered = [each for each, _ in db.session_models(conn, self.provider)]
        finally:
            conn.close()
        if model not in offered:
            raise AcpError(f"{self.label} offers no model {model}; pick one of {', '.join(offered)}")
        connection = self._connection(model, cwd)
        result = connection.request("session/new", {"cwd": str(cwd), "mcpServers": []})
        session_id = result.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise AcpError(f"{self.label} started a session without an id; no prompt was sent")
        self._choose_model(connection, session_id, model, result, saved=False)
        self._choose_mode(connection, session_id, result)
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

    def _choose_model(self, connection: Connection, session_id: str, model: str, result: dict[str, Any],
                      saved: bool) -> None:
        """Put a new or loaded Antigravity session on `model`, as T3 Code does, and keep the models its answer
        (`result`) offers for the page's menu (db.session_models). "default" leaves the model Antigravity's settings
        chose. A new session's model (`saved` False) that the signed-in Google account does not offer is refused. A
        loaded session's saved model (`saved` True) goes to Antigravity also when its list leaves it out, as T3 Code's
        allowUnlistedValue passes a saved value: the CLI still takes model ids it no longer lists. An answer without the
        list leaves the refusal to Antigravity."""
        if self.provider != "gemini":
            return
        models = offered_models(result)
        if models is not None:
            conn = self._db()
            try:
                db.kv_set(conn, db.models_key(self.provider), [list(each) for each in models])
            finally:
                conn.close()
        if model == "default":
            return
        if not saved and models is not None and model not in [each for each, _ in models]:
            raise AcpError(f"Antigravity model '{model}' is unavailable for this Google account. Select an available"
                           " model; no prompt was sent")
        connection.request("session/set_config_option", {"sessionId": session_id, "configId": "model", "value": model})

    def _choose_mode(self, connection: Connection, session_id: str, result: dict[str, Any]) -> None:
        """Put a new or loaded Antigravity session in its `auto` mode (SESSION-30): file edits and commands its own
        check judges safe and reversible run without asking, inside Antigravity's sandbox or, when the sandbox is too
        narrow, outside it; irreversible ones still come to Cockpit as requests. A loaded session starts in `default`
        again, so both get it. An answer (`result`) that offers no `auto` mode leaves the session in the mode it has,
        and the error log says so."""
        if self.provider != "gemini":
            return
        modes = result.get("modes")
        offered = [each.get("id") for each in modes.get("availableModes") or [] if isinstance(each, dict)] \
            if isinstance(modes, dict) else []
        if "auto" not in offered:
            log.error("%s offers no auto mode (it offers %s); session %s asks before every edit and command",
                      self.label, offered or "no modes", session_id)
            return
        connection.request("session/set_mode", {"sessionId": session_id, "modeId": "auto"})

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
            result = connection.request("session/load", {"sessionId": session_id, "cwd": row["cwd"], "mcpServers": []})
            self._choose_model(connection, session_id, row["model"], result, saved=True)
            self._choose_mode(connection, session_id, result)
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
        turn counts as over, so whoever waits for its end (interrupt) reads the state it ended in. Holds _turn_lock, so
        no update lands between the turn's words being kept and the turn ending."""
        with self._turn_lock:
            self._end_turn(session_id, answer)

    def _end_turn(self, session_id: str, answer: dict[str, Any]) -> None:
        self._flush(session_id)
        with self._lock:
            started = self._active.get(session_id)
            # The turn's messages, one per stretch of words between tool calls: all of them make the turn's reply, and
            # the last is what the session shows, as for a Codex turn.
            messages = [each.strip() for each in self._reply.get(session_id, []) if each.strip()]
        reply, last = "\n\n".join(messages), (messages[-1] if messages else "")
        if "error" in answer:
            error = answer["error"]
            self._save(session_id, state="failed", detail=_refusal(self.label, error)[:12000])
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
            self._late.pop(session_id, None)
            keys = [key for key, each in self._pending.items() if each["params"]["threadId"] == session_id]
            left_open = [(self._pending.pop(key), self._pending_on.pop(key)) for key in keys]
            self._ended.notify_all()
        for pending, on in left_open:
            # The agent still waits for these; the turn they belonged to is over.
            try:
                on.write({"id": pending["request_id"], "result": {"outcome": {"outcome": "cancelled"}}})
            except AcpError:
                log.warning("%s ended before its request %s was cancelled", self.label, pending["id"])

    def _begin_late(self, connection: Connection, session_id: str) -> bool:
        """Start a late turn: Antigravity replies by itself, with no prompt open, when a command or scheduled wake it
        left running finishes, as T3 Code expects of it. The turn holds the session as working until the reply's
        closing usage_update, or LATE_QUIET_SECONDS without an update. True when the session is in a turn after it."""
        if self.provider != "gemini":
            return False
        with self._lock:
            if session_id in self._active:
                return True
            if (session_id in self._loading or session_id in self._stopping
                    or self._loaded.get(session_id) is not connection):
                return False
            self._active[session_id] = db.now_iso()
            self._reply[session_id], self._unsaid[session_id] = [], []
            self._late[session_id] = late = LateTurn(time.monotonic())
        self._save(session_id, state="working")
        self._watch_late(session_id, late, LATE_QUIET_SECONDS)
        return True

    def _watch_late(self, session_id: str, late: LateTurn, seconds: float) -> None:
        timer = threading.Timer(seconds, self._late_quiet, (session_id, late))
        timer.daemon = True
        timer.start()

    def _late_quiet(self, session_id: str, late: LateTurn) -> None:
        """The watch on the late turn `late`: it ends once the agent sent nothing for LATE_QUIET_SECONDS, and looks
        again later while the agent still sends or waits on a card for the user. A watch whose turn already ended does
        nothing, also when a newer late turn runs. It decides and ends the turn under _turn_lock, so an update arriving
        meanwhile either is part of the turn or starts the next one."""
        with self._turn_lock:
            with self._lock:
                if self._late.get(session_id) is not late:
                    return
                if any(each["params"]["threadId"] == session_id for each in self._pending.values()):
                    # The agent waits for the user's answer: quiet from here on is the user's, not the agent's.
                    late.last = time.monotonic()
                left = late.last + LATE_QUIET_SECONDS - time.monotonic()
                if left <= 0:
                    self._late.pop(session_id)
            if left > 0:
                self._watch_late(session_id, late, left)
                return
            log.warning("%s sent nothing for %d minutes in a late reply; its turn ends", self.label,
                        LATE_QUIET_SECONDS // 60)
            self._turn_ended(session_id, {"result": {"stopReason": "end_turn"}})

    def interrupt(self, session_id: str) -> None:
        """Cancel the session's turn: its open permission requests are answered as cancelled, as ACP asks, and from
        here until the turn ends no answer approves one, so an Allow racing Stop loses."""
        with self._lock:
            connection = self._loaded.get(session_id)
            if session_id not in self._active or connection is None:
                return
            self._cancelling.add(session_id)
            # Taken off here, so neither its usage update nor the quiet watch ends the turn as well.
            late = self._late.pop(session_id, None) is not None
            keys = [key for key, each in self._pending.items() if each["params"]["threadId"] == session_id]
            open_requests = [(self._pending.pop(key), self._pending_on.pop(key)) for key in keys]
        for pending, on in open_requests:
            try:
                on.write({"id": pending["request_id"], "result": {"outcome": {"outcome": "cancelled"}}})
            except AcpError:
                log.warning("%s ended before its request %s was cancelled", self.label, pending["id"])
        try:
            connection.notify("session/cancel", {"sessionId": session_id})
        finally:
            if late:
                # A late turn has no prompt whose answer would end it; it ends here also when the cancel fails.
                self._turn_ended(session_id, {"result": {"stopReason": "cancelled"}})
        if late:
            return
        with self._ended:
            if not self._ended.wait_for(lambda: session_id not in self._active, timeout=30):
                raise AcpError(f"{self.label} has not confirmed the cancelled turn ended; no new prompt was sent")

    def stop(self, session_id: str) -> None:
        """Interrupt, then let go of the session. No late reply begins between the two (_stopping), so Cockpit allows
        no call on its own meanwhile (_auto_allow); one already running ends here, since its updates are ignored from
        now on. The user's answer to a card can still land before interrupt begins cancelling (answer)."""
        with self._lock:
            self._stopping.add(session_id)
        try:
            self.interrupt(session_id)
        except BaseException:
            # A failed interrupt keeps the session, so Stop can be pressed again.
            with self._lock:
                self._stopping.discard(session_id)
            raise
        with self._lock:
            self._stopping.discard(session_id)
            self._loaded.pop(session_id, None)
            late = self._late.pop(session_id, None) is not None
            self._calls = {key: row for key, row in self._calls.items() if key[0] != session_id}
        if late:
            self._turn_ended(session_id, {"result": {"stopReason": "cancelled"}})

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
        """The user's answer to a permission request or to one of Antigravity's questions (_answer_question). For a
        permission request, accept picks the agent's allow-once option, decline its reject-once option, cancel neither. Only one-time options are ever picked: a request without the one asked
        for, or whose call the card could not show, is refused and stays open."""
        with self._lock:
            pending = self._pending.get(key)
            if pending is None:
                raise AcpError(f"this {self.label} request has already ended")
            if pending["params"]["threadId"] in self._cancelling:
                raise AcpError(f"this {self.label} turn is being stopped; its request is cancelled")
            if pending["method"] == QUESTION_METHOD:
                self._answer_question(key, pending, result)
                return
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
            # Written under the lock interrupt takes to begin cancelling (_cancelling) and to cancel the open
            # requests, so an answer is either sent before that or refused. An Allow can land after Stop marked the
            # session _stopping and before interrupt marks it _cancelling: the agent gets the Allow, then interrupt
            # cancels the turn, as when the Allow came just before Stop.
            self._pending.pop(key)
            self._pending_on.pop(key).write({"id": pending["request_id"], "result": {"outcome": outcome}})
            self._answered(pending)
        self._record(f"{self.provider}.answered", "user", {"request": key, "session_id": pending["params"]["threadId"],
                                                           "decision": decision})

    def _answered(self, pending: dict[str, Any]) -> None:
        """A late turn's quiet starts again when the user answers, so the agent has its full quiet time to go on.
        Called under the lock."""
        late = self._late.get(pending["params"]["threadId"])
        if late is not None:
            late.last = time.monotonic()

    def _answer_question(self, key: str, pending: dict[str, Any], result: dict[str, Any]) -> None:
        """The user's answer to one of Antigravity's questions, in the Codex question card's shape (`answers`), or
        cancel (`decision`). It picks the option whose label is the answer, else the one whose id is; anything else
        is refused and the question stays open. Called under the lock."""
        if result.get("decision") == "cancel":
            outcome: dict[str, Any] = {"outcome": "cancelled"}
        else:
            question_id = pending["params"]["questions"][0]["id"]
            answers = result.get("answers")
            given = answers.get(question_id) if isinstance(answers, dict) and set(answers) == {question_id} else None
            given = given.get("answers") if isinstance(given, dict) else None
            if not isinstance(given, list) or len(given) != 1 or not isinstance(given[0], str):
                raise AcpError(f"answer {self.label}'s question with one of its options")
            # The card sends the label it shows, unique on the card (_question); an option id answers too.
            by_label = [each for each in pending["options"] if each["label"] == given[0]]
            chosen = by_label or [each for each in pending["options"] if each["value"] == given[0]]
            if not chosen:
                raise AcpError(f"{self.label} offers no single option {given[0]!r}; choose one of "
                               + ", ".join(each["label"] for each in pending["options"]))
            outcome = {"outcome": "selected", "optionId": chosen[0]["value"]}
        self._pending.pop(key)
        self._pending_on.pop(key).write({"id": pending["request_id"], "result": {"outcome": outcome}})
        self._answered(pending)
        self._record(f"{self.provider}.answered", "user", {"request": key, "session_id": pending["params"]["threadId"],
                                                           "decision": "cancel" if "optionId" not in outcome else "answer"})

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
        next message. Under _turn_lock, so a turn the quiet watch is ending ends first."""
        with self._turn_lock:
            self._lose(connection)

    def _lose(self, connection: Connection) -> None:
        with self._lock:
            lost = [sid for sid, each in self._loaded.items() if each is connection]
            ended = [sid for sid in lost if sid in self._active]
        for sid in ended:
            self._save(sid, state="failed", detail=f"{self.label} ended during this turn; check before continuing")
        with self._ended:
            for sid in lost:
                if self._loaded.get(sid) is connection:
                    self._loaded.pop(sid)
            self._calls = {key: row for key, row in self._calls.items() if key[0] not in lost}
            for sid in ended:
                self._active.pop(sid, None)
                self._reply.pop(sid, None)
                self._unsaid.pop(sid, None)
                self._cancelling.discard(sid)
                self._late.pop(sid, None)
            for key in [key for key, each in self._pending_on.items() if each is connection]:
                self._pending.pop(key, None)
                self._pending_on.pop(key)
            self._ended.notify_all()

    def _permission(self, connection: Connection, request_id: Any, params: dict[str, Any]) -> None:
        """A request to run a tool, which becomes a card on the page while it comes from the process running the
        session's turn, and is answered cancelled otherwise. The card holds what the request says the call does (its
        command or input, the files it names, the diffs it proposes), in memory only. Allow shows only for an
        allow-once option and a call the card can show. Under _turn_lock, so a card never lands on a turn that is
        ending."""
        with self._turn_lock:
            self._take_permission(connection, request_id, params)

    def _take_permission(self, connection: Connection, request_id: Any, params: dict[str, Any]) -> None:
        session_id = params.get("sessionId")
        call = params.get("toolCall") or {}
        row = self._row(session_id) if isinstance(session_id, str) else None
        if row is None:
            connection.write({"id": request_id, "result": {"outcome": {"outcome": "cancelled"}}})
            return
        options = [each for each in params.get("options") or [] if isinstance(each, dict) and "optionId" in each]
        key = uuid.uuid4().hex
        call_id = str(call.get("toolCallId") or "")
        if self.provider == "gemini" and call_id.startswith(QUESTION_PREFIX):
            pending = self._question(key, request_id, session_id, row, call_id, call, options)
            if pending is None:
                connection.write({"id": request_id, "result": {"outcome": {"outcome": "cancelled"}}})
                return
        else:
            kinds = {each.get("kind") for each in options}
            command, diffs = _command(call), _diffs(call, row["cwd"])
            if _shown_by_diff(call) and any(len(each["diff"]) > DIFF_MAX for each in diffs):
                # A diff cut at DIFF_MAX no longer shows all the call does, so the card shows its input whole.
                command = json.dumps(call["rawInput"], ensure_ascii=False)
            paths = _paths(call, diffs)
            cwd = _cwd(call, row["cwd"])
            if (_tool_name(call) == "execute" and "allow_once" in kinds and command and not _outside_sandbox(call)
                    and safe_shell.safe(command, cwd, str(config.HOME))):
                self._auto_allow(connection, request_id, session_id, call, options)
                return
            offered =[*(["accept"] if "allow_once" in kinds and (command or paths) else []),
                       *(["decline"] if "reject_once" in kinds else []), "cancel"]
            pending = {
                "id": key, "request_id": request_id, "method": PERMISSION_METHOD, "provider": self.provider,
                "options": options,
                "params": {"threadId": session_id, "reason": _reason(call, command),
                           "cwd": cwd,
                           "availableDecisions": offered, "command": command,
                           "shell": _tool_name(call) == "execute", "paths": paths, "diffs": diffs,
                           "outsideSandbox": _outside_sandbox(call)},
            }
        self._begin_late(connection, session_id)
        with self._lock:
            ours = (self._loaded.get(session_id) is connection and session_id in self._active
                    and session_id not in self._cancelling)
            if ours:
                self._pending[key] = pending
                self._pending_on[key] = connection
        if not ours:
            connection.write({"id": request_id, "result": {"outcome": {"outcome": "cancelled"}}})
            return
        self._record(f"{self.provider}.requested", self.provider, {
            "request": key, "session_id": session_id, "method": pending["method"], "tool": _tool_name(call)})

    def _auto_allow(self, connection: Connection, request_id: Any, session_id: Any, call: dict[str, Any],
                    options: list[dict[str, Any]]) -> None:
        """Allow once, without a card, a shell call that only reads or only touches Cockpit (safe_shell.safe) and runs
        inside Antigravity's sandbox, when it comes on one of the session's turns; cancelled otherwise, as a card would
        be. A call outside the sandbox always shows its card, since a read such as `git status` can run programs a
        repository's config names, which the agent can write from inside the sandbox. The event names the session and
        the kind of call, never the command."""
        self._begin_late(connection, session_id)
        with self._lock:
            ours = (self._loaded.get(session_id) is connection and session_id in self._active
                    and session_id not in self._cancelling and session_id not in self._stopping)
            if ours:
                chosen = next(each for each in options if each.get("kind") == "allow_once")
                connection.write({"id": request_id, "result": {"outcome": {
                    "outcome": "selected", "optionId": chosen["optionId"]}}})
                # A late turn's quiet starts again, as when the user answers a card.
                late = self._late.get(session_id)
                if late is not None:
                    late.last = time.monotonic()
        if not ours:
            connection.write({"id": request_id, "result": {"outcome": {"outcome": "cancelled"}}})
            return
        self._record(f"{self.provider}.auto_allowed", self.provider, {"session_id": session_id,
                                                                      "tool": _tool_name(call)})

    def _question(self, key: str, request_id: Any, session_id: Any, row: dict[str, Any], call_id: str,
                  call: dict[str, Any], options: list[dict[str, Any]]) -> dict[str, Any] | None:
        """One of Antigravity's own questions, which it asks as a permission request whose call id starts
        QUESTION_PREFIX: the call's title is the question and each option an answer, shown on Codex's question card,
        each under a label of its own (_unique_labels); the answer picks the option by its id. None when two options
        share an id or one has none, which T3 Code also refuses."""
        # Runs of spaces collapse, as the page shows them.
        answers = [{"value": str(each["optionId"]), "label": " ".join(str(each.get("name") or "").split()) or str(each["optionId"])}
                    for each in options]
        values = [each["value"] for each in answers]
        _unique_labels(answers)
        if not answers or "" in [each.strip() for each in values] or len(set(values)) != len(values):
            log.error("%s asked a question with %d options Cockpit cannot tell apart; it was cancelled", self.label,
                      len(answers))
            return None
        return {
            "id": key, "request_id": request_id, "method": QUESTION_METHOD, "provider": self.provider, "options": answers,
            "params": {"threadId": session_id, "reason": "", "cwd": row["cwd"], "questions": [{
                "id": call_id, "header": "", "question": str(call.get("title") or "").strip() or "Choose an option.",
                "options": [{"label": each["label"], "description": ""} for each in answers],
                "isOther": False, "isSecret": False}]},
        }

    def _update(self, connection: Connection, session_id: Any, update: dict[str, Any]) -> None:
        """One streamed update of a turn: the agent's words, or a tool call starting or changing. Updates replayed
        while a session loads, those of sessions Cockpit did not start, and those from another process than the one
        running the session's turn are left out. Under _turn_lock, so an update arriving as a turn ends is either part
        of that turn or starts the next one."""
        if not isinstance(session_id, str):
            return
        with self._turn_lock:
            self._take_update(connection, session_id, update)

    def _take_update(self, connection: Connection, session_id: str, update: dict[str, Any]) -> None:
        kind = update.get("sessionUpdate")
        with self._lock:
            if session_id in self._loading or self._loaded.get(session_id) is not connection:
                return
            turn = self._late.get(session_id)
            late = turn is not None
            if turn is not None:
                turn.last = time.monotonic()
            idle = session_id not in self._active
        # A call's update after its turn ended changes its row and starts no turn.
        if idle and kind != "tool_call_update" and not (kind in LATE_UPDATES and self._begin_late(connection, session_id)):
            return
        if late and kind == "usage_update":
            # Antigravity closes each reply with its usage, also one it sends with no prompt open.
            with self._lock:
                ends = self._late.pop(session_id, None) is not None
            if ends:
                self._turn_ended(session_id, {"result": {"stopReason": "end_turn"}})
            return
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
                "title": STATUS_LABELS.get(update.get("status"), "pending"),
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
            if update.get("status") in STATUS_LABELS:
                changes["title"] = STATUS_LABELS[update["status"]]
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
