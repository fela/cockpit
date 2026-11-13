"""The emergency session (SPEC SESSION-21): one Claude or Codex session the user can talk to from a minimal page of
its own, for when the rest of Cockpit's page breaks.

The page (static/emergency.html and .js) loads none of the main page's scripts, and this module reads neither the
database nor the session list: its state is one JSON file (STATE_PATH) holding the session's id and its turns.
Each message the user sends is one `claude -p` turn on the account the user picked for it, `--session-id` for the
first and `--resume` after, so the conversation goes on across turns and accounts (both share the transcripts),
and no process waits between them for Cockpit to stop or archive. The user picks each turn's model and reasoning
effort, and the agent with the session's first message (SPEC SESSION-22): a Codex session's turns are `codex exec`
runs, the first starting a thread and the rest resuming it, on the Codex login in ~/.codex. The turn runs in a systemd scope of its own,
named after the session and the turn, which outlives a Cockpit restart and holds every process the turn starts. The
state file names that unit before the turn starts, so Stop and Close can always find the turn and end all of it,
SIGTERM and then SIGKILL. The turn reads the user's message from a file and writes Claude's stream of events to
another file the page reads. A turn runs until its processes end, through Claude finishing, Stop, or Close, which
also forgets the session.

The user cannot answer a permission dialog from this page, so COCKPIT_EMERGENCY makes the PermissionRequest hook
leave every dialog to the turn at once, instead of waiting for an answer on the main page (cli.permission_ask);
in a `claude -p` turn a call that would ask is refused, and the page shows that refusal.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cockpit import accounts, config

log = logging.getLogger(__name__)

STATE_PATH = config.DATA_DIR / "emergency.json"
OUTPUT_DIR = config.DATA_DIR / "emergency"
# Each turn runs in a systemd scope of its own on Linux; elsewhere, and in tests, as a process group of its own.
SCOPED = sys.platform == "linux"
# The page shows this many of the latest turns.
SHOWN_TURNS = 30
# A call's input as the page shows it, and the end of a failed turn's output.
CALL_CHARS = 300
OUTPUT_TAIL_CHARS = 2000
# The page reads at most the last this many bytes of a turn's output.
OUTPUT_READ_BYTES = 8_000_000
# How long Stop and Close wait for a turn to end after SIGTERM before they send SIGKILL.
END_WAIT_SECONDS = 3

EMPTY: dict[str, Any] = {"session_id": None, "provider": None, "thread": None, "turns": []}
# Codex turns run with the sandbox and network access Cockpit's own Codex sessions have (codex_agent.py).
CODEX_SANDBOX = "workspace-write"

# Held by every read of the state file that may move a damaged one aside, and by every write. The main page's
# summary reads without it and changes nothing, so it never waits on Stop or Close.
_lock = threading.Lock()
# Units known to have ended, so a finished turn costs no `systemctl` call on later reads.
_ended_units: set[str] = set()
# Each output file's events as last read, by path, with the size and change time they were read at.
_parsed: dict[str, tuple[tuple[int, int], list[dict[str, Any]], bool]] = {}


class Busy(Exception):
    """A turn is still running."""


class Changed(Exception):
    """The session is no longer the one the user saw."""


class Unreachable(Exception):
    """systemd did not say whether a turn runs, or did not end it."""


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(value: Any, *, empty: bool) -> bool:
    """Whether `value` is text that goes out as UTF-8 (a lone surrogate from a JSON escape does not), or None where
    `empty` allows it."""
    if value is None:
        return empty
    if not isinstance(value, str):
        return False
    try:
        value.encode()
    except UnicodeEncodeError:
        return False
    return True


def _valid(state: Any) -> bool:
    if not isinstance(state, dict) or not isinstance(state.get("turns"), list) or "session_id" not in state:
        return False
    if not (_text(state["session_id"], empty=True) and _text(state.get("cwd"), empty=True)):
        return False
    # A session has an agent, and no session none; sessions from before Codex could run here are Claude's.
    state.setdefault("provider", "claude" if state["session_id"] else None)
    if state["provider"] not in (config.SESSION_OPTIONS if state["session_id"] else (None,)):
        return False
    # A Codex session's thread, once a later message found it in a turn's output (_codex_thread).
    state.setdefault("thread", None)
    if not _text(state["thread"], empty=True) or (state["thread"] is not None and state["provider"] != "codex"):
        return False
    # A session has turns, and turns have a session: Close forgets a session only by its id.
    if (state["session_id"] is None) != (not state["turns"]):
        return False
    for turn in state["turns"]:
        if not isinstance(turn, dict):
            return False
        if not all(_text(turn.get(key), empty=False) and turn[key] for key in ("at", "text", "output")):
            return False
        # A Codex turn has no Claude account. Turns written before turns had units name none, and run as a process
        # group; turns from before the user picked their model and effort name neither.
        turn.setdefault("unit", None)
        turn.setdefault("model", None)
        turn.setdefault("effort", None)
        turn.setdefault("speed", "standard")
        if turn["speed"] not in ("standard", "fast", "ultrafast"):
            return False
        if not all(_text(turn.get(key), empty=True) for key in ("account", "model", "effort")):
            return False
        if (state["provider"] == "claude") != bool(turn.get("account")):
            return False
        if "pid" not in turn or "started" not in turn:
            return False
        if not (_text(turn["unit"], empty=True) and _text(turn["started"], empty=True)):
            return False
        # Above 1: os.killpg(0) and (1) would signal Cockpit's own process group and init's.
        pid = turn.get("pid")
        if pid is not None and (type(pid) is not int or pid <= 1):
            return False
    return True


def _load() -> dict[str, Any] | None:
    """The session and its turns; None when the file cannot be read as them."""
    try:
        state = json.loads(STATE_PATH.read_text())
    except FileNotFoundError:
        return dict(EMPTY, turns=[])
    except ValueError:
        return None
    return state if _valid(state) else None


def _read_state() -> dict[str, Any]:
    """The session and its turns. A file that cannot be read as them is moved aside for a look later, and the page
    starts a new session rather than stop working. Its turns could no longer be watched or stopped from the page,
    so any that still run end first; should that fail, the file stays, and the next read tries again."""
    state = _load()
    if state is not None:
        return state
    for unit in _active_units():
        log.error("emergency session: ending %s, a turn of the session that could not be read", unit)
        _end({"unit": unit, "pid": None, "started": None})
    aside = STATE_PATH.with_name(f"emergency.broken-{datetime.now(UTC):%Y%m%dT%H%M%S%f}.json")
    STATE_PATH.replace(aside)
    log.error("emergency session: %s could not be read; moved it to %s and started a new session", STATE_PATH, aside)
    return dict(EMPTY, turns=[])


def _write_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=1))
    temporary.replace(STATE_PATH)


def _system_prompt(provider: str) -> str:
    refused = ("cannot answer permission dialogs from it: a call that would ask is refused" if provider == "claude"
               else f"cannot approve commands from it: they run in the {CODEX_SANDBOX} sandbox, and what it blocks fails")
    return (
        f"{config.user_name()} writes to you from Cockpit's emergency page, which they use when the main Cockpit"
        f" page is broken or unreachable. They read only your text replies there, and {refused}."
        " Keep replies short and plain."
    )


def _started(pid: int) -> str | None:
    """When the process `pid` started, so a pid used again later is told apart; None once it has ended (a zombie
    included). Linux reads it from /proc, in clock ticks since boot; macOS asks `ps`, to the second."""
    if sys.platform != "linux":
        found = subprocess.run(
            ["ps", "-o", "stat=,lstart=", "-p", str(pid)], capture_output=True, text=True, check=False
        )
        state, _, start = found.stdout.strip().partition(" ")
        return None if found.returncode or not start or state.startswith("Z") else start.strip()
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
    # ProcessLookupError: the process ended between opening the file and reading it.
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return None
    # The fields after the command's name start at the third: state, then the start time as the 22nd.
    return None if fields[0] == "Z" else fields[19]


def _leader_alive(turn: dict[str, Any]) -> bool:
    return turn["pid"] is not None and turn["started"] is not None and _started(turn["pid"]) == turn["started"]


def _unit_prefix() -> str:
    """The start of this Cockpit's turn units: named after its state file too, so a second Cockpit on the machine (a
    preview, a test) never takes another's units for its own."""
    return f"cockpit-emergency-{hashlib.sha256(str(STATE_PATH).encode()).hexdigest()[:8]}-"


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, check=False, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Unreachable(f"systemctl {args[0]} did not answer: {exc}") from exc


def _active_units() -> list[str]:
    """This Cockpit's turn units that have not ended, including those still stopping."""
    if not SCOPED:
        return []
    found = _systemctl("list-units", "--type=scope", "--all", "--plain", "--no-legend", f"{_unit_prefix()}*")
    if found.returncode:
        raise Unreachable(f"systemctl list-units failed: {found.stderr.strip()}")
    # Columns: unit, load state, active state, sub-state, description.
    rows = [line.split() for line in found.stdout.splitlines() if line.strip()]
    return [row[0].removesuffix(".scope") for row in rows if len(row) > 2 and row[2] not in ("inactive", "failed")]


def _unit_runs(unit: str) -> bool:
    found = _systemctl("show", "--property=ActiveState", "--value", f"{unit}.scope")
    state = found.stdout.strip()
    # A unit systemd does not know (never started, or collected after it ended) shows as inactive.
    if found.returncode == 0 and state in ("inactive", "failed"):
        return False
    if found.returncode == 0 and state in ("active", "activating", "deactivating", "reloading"):
        return True
    raise Unreachable(f"systemd did not say whether {unit} runs: {state or found.stderr.strip()}")


def _alive(turn: dict[str, Any]) -> bool:
    """Whether any process of the turn still runs: any in its unit, or else its first process. Raises Unreachable
    when systemd does not say, rather than take the turn for ended."""
    unit = turn["unit"]
    if unit is None or unit in _ended_units:
        return unit is None and _leader_alive(turn)
    if _leader_alive(turn) or _unit_runs(unit):
        return True
    # Only once the turn has started: before, its unit is not there yet either.
    if turn["pid"] is not None:
        _ended_units.add(unit)
    return False


def _signal(turn: dict[str, Any], number: signal.Signals) -> None:
    """Signal the turn's process group, while its first process lives (a launcher that has not made its unit yet
    included), and every process in its unit."""
    if turn["pid"] is not None and (turn["unit"] is None or _leader_alive(turn)):
        try:
            os.killpg(turn["pid"], number)
        except ProcessLookupError:
            pass  # it ended meanwhile
    if turn["unit"] is not None:
        sent = _systemctl("kill", f"--signal={number.name}", f"{turn['unit']}.scope")
        # A unit that ended meanwhile, or never started, cannot be signalled; one that still runs must be. The
        # first process is not asked about here: the group signal above reached it, and it may take a moment to end.
        if sent.returncode and _unit_runs(turn["unit"]):
            raise Unreachable(f"systemd did not send {number.name} to {turn['unit']}: {sent.stderr.strip()}")


def _end(turn: dict[str, Any]) -> None:
    """End every process of the turn: SIGTERM, then SIGKILL to whatever still runs after END_WAIT_SECONDS. A turn
    without a unit gets SIGKILL in any case, for processes its first one left behind in its group. Raises
    Unreachable when the turn still runs after all, so the caller does not forget it."""
    _signal(turn, signal.SIGTERM)
    deadline = time.monotonic() + END_WAIT_SECONDS
    while _alive(turn) and time.monotonic() < deadline:
        time.sleep(0.1)
    if turn["unit"] is None or _alive(turn):
        _signal(turn, signal.SIGKILL)
    deadline = time.monotonic() + END_WAIT_SECONDS
    while _alive(turn):
        if time.monotonic() > deadline:
            raise Unreachable(f"the turn of {turn.get('at')} still runs after SIGKILL")
        time.sleep(0.1)


def _events(path: Path) -> tuple[list[dict[str, Any]], bool]:
    """What the turn's output shows the user, in order, and whether it holds Claude's final result."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return [], False
    except OSError as exc:
        return [{"kind": "error", "text": f"Cockpit could not read this turn's output: {exc}"}], False
    key = (stat.st_size, stat.st_mtime_ns)
    cached = _parsed.get(str(path))
    if cached is not None and cached[0] == key:
        return list(cached[1]), cached[2]
    cut = stat.st_size > OUTPUT_READ_BYTES
    try:
        with path.open("rb") as file:
            if cut:
                file.seek(-OUTPUT_READ_BYTES, os.SEEK_END)
            data = file.read()
    except OSError as exc:
        return [{"kind": "error", "text": f"Cockpit could not read this turn's output: {exc}"}], False
    # On newlines only: a JSON string may hold U+2028 and other characters str.splitlines() also splits on.
    lines = data.decode(errors="replace").split("\n")
    shown: list[dict[str, Any]] = []
    if cut:
        lines = lines[1:]
        shown.append({"kind": "output", "text": "The start of this turn's output is left out: it is too long to show."})
    other: list[str] = []
    finished = False
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            other.append(line)
            continue
        if not isinstance(event, dict):
            continue
        message = event.get("message")
        message = message if isinstance(message, dict) else {}
        blocks = message.get("content")
        blocks = blocks if isinstance(blocks, list) else []
        if event.get("type") == "assistant":
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and str(block.get("text", "")).strip():
                    shown.append({"kind": "says", "text": str(block["text"])})
                elif block.get("type") == "tool_use":
                    shown.append({"kind": "call", "text": f"{block.get('name')}: {_call_words(block.get('input'))}"})
        elif event.get("type") == "user":
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                    shown.append({"kind": "refused", "text": _result_words(block.get("content"))})
        elif event.get("type") == "result":
            finished = True
            if event.get("is_error"):
                shown.append({"kind": "error", "text": str(event.get("result") or event.get("subtype"))})
        # Codex's events (`codex exec --json`): what it says and runs as items, then the turn's end.
        elif event.get("type") in ("item.started", "item.completed") and isinstance(event.get("item"), dict):
            shown.extend(_codex_item(event["type"], event["item"]))
        elif event.get("type") in ("turn.completed", "turn.failed"):
            finished = True
            if event["type"] == "turn.failed":
                error = event.get("error") if isinstance(event.get("error"), dict) else {}
                shown.append({"kind": "error", "text": str(error.get("message") or "the turn failed")})
        elif event.get("type") == "error":
            shown.append({"kind": "error", "text": str(event.get("message"))})
    if other and not finished:
        shown.append({"kind": "output", "text": "\n".join(other)[-OUTPUT_TAIL_CHARS:]})
    # A lone surrogate from a JSON escape cannot go out as UTF-8: it shows as a replacement mark.
    shown = [{**event, "text": event["text"].encode(errors="replace").decode()} for event in shown]
    _parsed[str(path)] = (key, shown, finished)
    return list(shown), finished


def _codex_item(kind: str, item: dict[str, Any]) -> list[dict[str, Any]]:
    """What one Codex item shows: its reply once complete, a command or tool call as it starts, an edit once made."""
    if kind == "item.completed" and item.get("type") == "agent_message" and str(item.get("text", "")).strip():
        return [{"kind": "says", "text": str(item["text"])}]
    if kind == "item.started" and item.get("type") == "command_execution":
        return [{"kind": "call", "text": f"shell: {str(item.get('command'))[:CALL_CHARS]}"}]
    if kind == "item.started" and item.get("type") == "mcp_tool_call":
        return [{"kind": "call", "text": f"{item.get('server')}.{item.get('tool')}"}]
    if kind == "item.completed" and item.get("type") == "file_change":
        paths = [str(change.get("path")) for change in item.get("changes") or [] if isinstance(change, dict)]
        return [{"kind": "call", "text": f"edit: {', '.join(paths)[:CALL_CHARS]}"}]
    if kind == "item.completed" and item.get("type") == "error":
        return [{"kind": "error", "text": str(item.get("message"))}]
    return []


def _call_words(tool_input: Any) -> str:
    if isinstance(tool_input, dict):
        for key in ("command", "file_path", "pattern", "url", "description", "prompt"):
            if isinstance(tool_input.get(key), str):
                return tool_input[key][:CALL_CHARS]
    return json.dumps(tool_input, ensure_ascii=False)[:CALL_CHARS]


def _result_words(content: Any) -> str:
    if isinstance(content, list):
        content = "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return str(content)[:CALL_CHARS]


def _signed_in(account: config.Account, now: datetime) -> bool:
    return accounts.signed_in(account) and not accounts.signed_out(account, now)


def view() -> dict[str, Any]:
    """The accounts to pick from, the session, how many turns it has, and its latest turns as the page shows them.
    Only the last turn can be running: a turn starts only once the one before has ended."""
    with _lock:
        state = _read_state()
        last = _running(state)
    turns = []
    shown = state["turns"][-SHOWN_TURNS:]
    # Only the turns shown stay read, so a long session does not grow Cockpit's memory.
    for path in set(_parsed) - {turn["output"] for turn in shown}:
        _parsed.pop(path, None)
    for turn in shown:
        events, finished = _events(Path(turn["output"]))
        running = turn is last
        if not finished and not running:
            events.append({"kind": "error", "text": "The turn ended before it finished."})
        turns.append({
            "at": turn["at"], "account": turn["account"], "model": turn["model"], "effort": turn["effort"], "speed": turn["speed"],
            "text": turn["text"], "running": running, "events": events,
        })
    now = datetime.now(UTC)
    return {
        "session_id": state["session_id"],
        "count": len(state["turns"]),
        "cwd": state.get("cwd"),
        "provider": state["provider"],
        "accounts": [{"name": each.name, "letter": each.letter, "signed_in": _signed_in(each, now)} for each in config.ACCOUNTS if not accounts.retired(each)],
        # What a turn may run on, per enabled agent: its models, the default first, its efforts and default effort.
        "options": {
            provider: {
                "models": [{"name": name, "label": label, "speeds": config.session_speeds(provider, name)}
                           for name, label in config.SESSION_OPTIONS[provider]["models"]],
                "efforts": config.SESSION_OPTIONS[provider]["efforts"],
                "effort": config.SESSION_OPTIONS[provider]["effort"],
            }
            for provider in config.setup().providers
        },
        "turns": turns,
    }


def summary() -> dict[str, Any] | None:
    """The session as the main page's list shows it: the user's last message, when and on which account it went,
    whether Claude is still answering it, and the session and turn count that Close names; None before the first
    message. A file that cannot be read shows as None here and is moved aside by the emergency page's next read.
    Whether Claude answers is its first process's: this read asks systemd nothing, so a slow systemd cannot hold up
    the main page."""
    state = _load()
    if state is None or not state["turns"]:
        return None
    last = state["turns"][-1]
    return {
        "text": last["text"], "at": last["at"], "provider": state["provider"], "account": last["account"],
        "running": _leader_alive(last),
        "session_id": state["session_id"], "count": len(state["turns"]),
    }


def _running(state: dict[str, Any]) -> dict[str, Any] | None:
    """The last turn while any process of it runs, whatever its output says."""
    return state["turns"][-1] if state["turns"] and _alive(state["turns"][-1]) else None


def _expect(state: dict[str, Any], session_id: str | None, count: int) -> None:
    if state["session_id"] != session_id or len(state["turns"]) != count:
        raise Changed("The emergency session changed since this page last read it. Look at it again first.")


def _transcript_exists(session_id: str) -> bool:
    # Each account's own folder, should the folders stop sharing transcripts: `--resume` then fails loudly on an
    # account that cannot see it, where `--session-id` would quietly start a second conversation under the same id.
    return any(any((each.folder / "projects").glob(f"*/{session_id}.jsonl")) for each in config.ACCOUNTS)


def _codex_thread(state: dict[str, Any]) -> str | None:
    """The Codex thread the session's turns run in: the one the state records, else the first a turn's output names;
    None while no turn started one. An output that cannot be read raises ValueError rather than starting a second
    thread in the same session."""
    if state["thread"] is not None:
        return state["thread"]
    for number, turn in enumerate(state["turns"], 1):
        try:
            with open(turn["output"], "rb") as file:
                for line in file:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict) and event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
                        return event["thread_id"]
        except OSError as exc:
            raise ValueError(f"Cockpit cannot read turn {number}'s output to find the Codex thread: {exc}") from exc
    return None


def send(
    session_id: str | None, count: int, text: str, provider: str, account_name: str | None, model: str, effort: str, speed: str,
    cwd: Path,
) -> None:
    """Start a turn with the user's `text` in the session the user saw (`session_id`, `count` turns; None for none)
    on `model` at `effort`, in the session's folder (`cwd` for a new session):
    a Claude turn on the account `account_name`, a Codex turn with none. `provider` is the session's agent, which
    only its first message picks."""
    if provider not in config.setup().providers:
        raise ValueError(f"{provider} is not enabled in Cockpit's setup")
    offered = config.SESSION_OPTIONS[provider]
    if model not in [name for name, _ in offered["models"]]:
        raise ValueError(f"{provider} offers no model {model}")
    if effort not in offered["efforts"]:
        raise ValueError(f"{provider} offers no effort {effort}")
    if provider == "codex" or speed != "standard":
        config.validate_speed(provider, model, speed)
    if (provider == "claude") != (account_name is not None):
        raise ValueError("A Claude turn names its account, and a Codex turn none")
    account = config.account(account_name) if account_name is not None else None
    if account is not None and accounts.retired(account):
        raise ValueError(f"The {account.name} Claude account is retired; pick another")
    if not _text(text, empty=False):
        raise ValueError("The message holds characters that are not valid text")
    with _lock:
        state = _read_state()
        _expect(state, session_id, count)
        if _running(state):
            raise Busy("The session is still answering; wait for it or stop it first")
        if state["session_id"] and state["provider"] != provider:
            raise Changed(f"The emergency session runs on {state['provider']}; close it to start one on {provider}.")
        session_id = session_id or str(uuid.uuid4())
        folder = Path(state.get("cwd") or cwd)
        number = len(state["turns"]) + 1
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        output = OUTPUT_DIR / f"{session_id}-{number}.jsonl"
        prompt = output.with_suffix(".prompt")
        # From a file rather than a pipe, so a turn that is slow to read its message cannot hold up the call.
        thread = None
        if account is not None:
            start = ["--resume", session_id] if _transcript_exists(session_id) else ["--session-id", session_id]
            command = [
                *accounts.claude_env(account), "COCKPIT_EMERGENCY=1",
                "claude", "-p", *start, "--model", model, "--effort", effort,
                "--append-system-prompt", _system_prompt(provider), "--output-format", "stream-json", "--verbose",
            ]
            prompt.write_text(text)
        else:
            thread = _codex_thread(state)
            tier = "default" if speed == "standard" else speed
            features = ["-c", "features.fast_mode=true"] if speed != "standard" else []
            if speed == "ultrafast":
                features += ["-c", "features.ultrafast_mode=true"]
            command = [
                config.setup().codex_command, "exec", *(["resume"] if thread else []), "--json", "--skip-git-repo-check",
                "-m", config.codex_model(model, speed), "-c", f'model_reasoning_effort="{effort}"', "-c", f'sandbox_mode="{CODEX_SANDBOX}"',
                "-c", f'service_tier="{tier}"', *features,
                "-c", "sandbox_workspace_write.network_access=true", *([thread] if thread else []), "-",
            ]
            # Codex takes no system prompt on its command line, so the thread's first message carries the note.
            prompt.write_text(text if thread else f"{_system_prompt(provider)}\n\n{text}")
        unit = f"{_unit_prefix()}{session_id}-{number}" if SCOPED else None
        turn = {"at": _now(), "account": account.name if account else None, "model": model, "effort": effort, "speed": speed,
                "text": text, "unit": unit, "pid": None, "started": None, "output": str(output)}
        before = {**state, "turns": list(state["turns"])}
        state.update(session_id=session_id, cwd=str(folder), provider=provider, thread=thread)
        state["turns"].append(turn)
        # Recorded before it starts: should Cockpit stop before it records the process, the unit still finds it. A
        # launcher that has not yet made its unit then ends with Cockpit, as systemd stops the service's whole
        # control group (KillMode=control-group).
        _write_state(state)
        scope = ["systemd-run", "--user", "--scope", "--collect", "--quiet", f"--unit={unit}", "--"] if unit else []
        args = [*scope, *command]
        try:
            with prompt.open() as message, output.open("w") as out:
                process = subprocess.Popen(
                    args, cwd=folder, stdin=message, stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
                )
        except OSError:
            _write_state(before)
            raise
        turn.update(pid=process.pid, started=_started(process.pid))
        try:
            _write_state(state)
        except OSError:
            # Without its process recorded, a launcher that has not made its unit yet could not be found later.
            _end(turn)
            raise
        # Reaps the process once it ends, so it does not stay a zombie; the turn is recorded either way.
        try:
            threading.Thread(target=process.wait, daemon=True).start()
        except RuntimeError:
            log.exception("emergency session: could not watch the turn's process; it shows as running until it ends")
        log.info("emergency session %s: turn %d started with %s at %s effort%s", session_id, number, model, effort,
                 f" on {account.name}" if account else "")


def stop(session_id: str | None, count: int) -> bool:
    """End the running turn of the session the user saw (`session_id`, `count` turns); False when none runs."""
    with _lock:
        state = _read_state()
        _expect(state, session_id, count)
        running = _running(state)
        if running is None:
            return False
        _end(running)
        log.info("emergency session: stopped the turn of %s", running["at"])
        return True


def close(session_id: str | None, count: int, end_answer: bool) -> None:
    """Close the session the user saw (`session_id`, `count` turns): end Claude's answer if one runs and the user
    agreed to that (`end_answer`; Busy otherwise), then forget the session and its output files, so the main page's
    list drops it and the next message starts a new one. Claude's transcripts stay. Closing a session that is
    already closed does nothing."""
    with _lock:
        state = _read_state()
        if state["session_id"] is None:
            return  # _valid: no session, no turns
        _expect(state, session_id, count)
        running = _running(state)
        if running is not None and not end_answer:
            raise Busy("Claude is still answering in the emergency session")
        if running is not None:
            # Ended before the session is forgotten, so no turn runs on that nobody can see or stop.
            _end(running)
        _write_state(dict(EMPTY, turns=[]))
        for turn in state["turns"]:
            _parsed.pop(turn["output"], None)
        log.info("emergency session: closed %s", state["session_id"])
        for leftover in OUTPUT_DIR.glob(f"{state['session_id']}-*"):
            try:
                leftover.unlink(missing_ok=True)
            except OSError:
                # The session is closed all the same; the file only takes room.
                log.exception("emergency session: could not remove %s", leftover)
