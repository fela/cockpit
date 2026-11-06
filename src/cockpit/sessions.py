"""Live Claude Code sessions on this machine.

`claude agents --json --all` names every session and whether its process is
alive; `<account folder>/jobs/<id>/state.json` adds the background session's one-line
activity, whether it is doing work now, what it is blocked on, its starting
prompt and the PRs it linked. Its last message is read from the end of its
transcript.

Each Claude account in use (accounts.py, SPEC SESSION-17) has a list of its own, so the
listing runs once per account, with that account's folder, and tags each session with
the account it runs on. Transcripts are shared: every account's `projects` links to
~/.claude/projects.

Cockpit runs `claude` in a transient systemd scope of its own. The first `claude`
call after a reboot starts the daemon that hosts every background session; in
Cockpit's own cgroup, a restart of cockpit.service would kill all of them.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cockpit import accounts
from cockpit.config import Account

log = logging.getLogger(__name__)

PROJECTS_DIR = Path.home() / ".claude" / "projects"
# How long one `claude agents` listing is reused before the next one starts in the background; the page polls
# more often. A listing takes 4 to 14 s on a busy host (SPEC UI-39), so no page request waits for one.
CACHE_SECONDS = 10
# A transcript is read from its end; the last message fits in this many bytes.
TRANSCRIPT_TAIL_BYTES = 1024 * 1024
# The latest instruction before a blocked call is looked for this far back from the transcript's end.
INSTRUCTION_SEARCH_BYTES = 16 * 1024 * 1024
INSTRUCTION_MAX = 4000
# The transcript Claude reads to explain a blocked call (transcript_before): the first read of its end, and how
# long each message, call or result in it may be.
TRANSCRIPT_EXPLAIN_BYTES = 4 * 1024 * 1024
TRANSCRIPT_PART_MAX = 3000
# How Claude Code shows a message another session sent, and the header and context block Cockpit puts
# around a message it delivers (delivery.py).
PEER_PREFIX = "Another Claude session sent a message:\n"
COCKPIT_HEADER = re.compile(
    r"Message from (?:(?P<cockpit>Cockpit itself)|agent (?P<agent>[a-z0-9-]+) via Cockpit|[^\n]+? via Cockpit:)[^\n]*\n+"
)
COCKPIT_CONTEXT = "\n---\nCockpit context:"
COCKPIT_BATCH_SEPARATOR = "\n\n* * *\n\n"
# The note `delivery.compose_batch` ends a batch of waiting messages with.
COCKPIT_BATCH_END = re.compile(
    r"\n\n\* \* \*\n\nThese \d+ messages from [^\n]+ waited for your turn to end\. They come together, oldest first\."
    # After the note, a stop's notice to the session (delivery.CUT_SHORT, AFTER_TURN).
    r" Act on each of them\.(?:\n\nCockpit stopped this session [^\n]+)?\s*$"
)
SCOPE = ["systemd-run", "--user", "--scope", "--collect", "--quiet", "--"]


def claude_command(account: Account, args: list[str]) -> list[str]:
    """`args` (starting with `claude`) wrapped to run on `account`, in its own systemd scope on Linux."""
    wrapped = [*accounts.claude_env(account), *args]
    return [*SCOPE, *wrapped] if sys.platform == "linux" else wrapped


# Each kind of work a job file lists in flight (Session.in_flight), in words (Session.busy_with). Claude Code lists
# a Monitor as local_bash too, and a wakeup (ScheduleWakeup) as session_cron with the cron jobs.
IN_FLIGHT_WORDS = {
    "local_bash": "a background command or Monitor",
    "local_agent": "a subagent",
    "session_cron": "a scheduled prompt or wakeup",
    "local_workflow": "a workflow",
    "monitor_mcp": "a Monitor",
    "monitor_ws": "a Monitor",
    "remote_agent": "a remote agent",
    "in_process_teammate": "a teammate",
    "mcp_task": "an MCP task",
}


@dataclass(frozen=True)
class Session:
    id: str  # the short id `claude agents` prints
    session_id: str  # the full UUID that `--resume` takes
    name: str
    state: str  # working | blocked | done | stopped | failed
    pid: int | None  # set while a process runs; it can take a message right now
    status: str  # busy | idle while alive, empty otherwise
    tempo: str  # active | idle | blocked, from the job file; empty for a session without one
    job_state: str  # the job file's own state: it stays working while a turn is under way; empty without one
    # The kinds of background work the job file lists as running: local_bash (a command), local_agent (a
    # subagent), session_cron (a scheduled prompt), artifact_watch, ...; empty without a job file.
    in_flight: list[str]
    cwd: str
    detail: str  # the session's own one-line summary of what it is doing
    needs: str  # what it is blocked on, when blocked
    intent: str  # the prompt it started with
    updated_at: str  # ISO time of the last state change
    links: list[dict[str, str]]  # PRs and other children it linked
    # The effort its options say (`--effort` in the job file's respawnFlags): a resume starts it at this one, and
    # `/effort` changes it. Empty without one: the session then runs at the default in Claude's settings.
    effort: str
    # The Claude Code release its process runs (running_version); empty while stopped.
    version: str
    # The name of the Claude account it runs on (config.ACCOUNTS): the one whose `claude agents` lists it; empty
    # for a Codex session.
    account: str
    provider: str = "claude"
    # When Cockpit took the finished session off `claude agents` (removals.py); empty for a listed one. Cockpit lists
    # it on as it was, stopped, and a message resumes it with the options it had.
    removed_at: str = ""

    @property
    def alive(self) -> bool:
        return self.pid is not None

    @property
    def working_now(self) -> bool:
        """Doing work now. A session whose turn ended stays `working` while a background task or a timer
        can wake it; its job file then says `idle`. A session without a job file has only its state."""
        return self.state == "working" and self.tempo in ("active", "")

    @property
    def waits_on_dialog(self) -> bool:
        """A dialog in its terminal waits for an answer (a permission, a question): its turn is under way
        and it waits for input. Messages wait in its queue until the dialog is answered; `needs` says
        what the dialog asks."""
        return self.alive and self.job_state == "working" and self.tempo == "blocked"

    @property
    def turn_over(self) -> bool:
        """A live session whose turn has ended with nothing left to wake it, so stopping it cuts nothing off
        (SPEC MSG-3). Background work shows as status busy with state done, and a scheduled prompt as status
        idle with state working; both live only in the running process, and the job file lists both in flight.
        A turn can also end with state working while its job file says idle and lists nothing in flight."""
        if not self.alive or self.status != "idle" or self.waits_on_dialog:
            return False
        return self.state in ("done", "blocked") or (self.tempo == "idle" and not self.in_flight)

    @property
    def busy_with(self) -> list[str]:
        """What keeps a live session's turn from being over (turn_over), in words; empty once it is over, and for
        a stopped session."""
        if not self.alive or self.turn_over:
            return []
        if self.waits_on_dialog:
            return [f"a dialog that waits for an answer ({self.needs})" if self.needs else "a dialog that waits for an answer"]
        held = list(dict.fromkeys(IN_FLIGHT_WORDS.get(kind, kind) for kind in self.in_flight))
        if self.working_now or (self.status == "busy" and not held):
            held.insert(0, "a turn under way")
        return held or [f"a state Cockpit cannot read (Claude Code lists it as {self.state or 'no state'}, {self.status or 'no status'})"]

    @property
    def asks_user(self) -> bool:
        """Its turn ended on a question for the user, so the user's answer is what it waits for (SPEC MSG-7). The job
        file says `working` while a turn is under way, so `blocked` there means the turn is over; status stays
        busy while background work it started still runs."""
        return self.alive and self.job_state == "blocked"

    @property
    def runs_subagent(self) -> bool:
        """A subagent it started runs in the background; stopping the session would lose its work."""
        return "local_agent" in self.in_flight


def _read_json(path: Path, warnings: list[str]) -> dict[str, Any]:
    # Interactive sessions have no job directory, so a missing file is normal.
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.error("could not read %s: %s", path, exc)
        warnings.append(f"Could not read {path}: {exc}")
        return {}
    if not isinstance(document, dict):
        log.error("%s is not a JSON object", path)
        warnings.append(f"{path} is not a JSON object")
        return {}
    return document


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _links(children: Any) -> list[dict[str, str]]:
    """The job's children as `{label, url}`, the same shape items use."""
    if not isinstance(children, list):
        return []
    return [
        {"label": " ".join(filter(None, (_text(child.get("kind")).upper(), _text(child.get("id"))))) or child["href"],
         "url": child["href"]}
        for child in children
        if isinstance(child, dict)
        and isinstance(child.get("href"), str)
        and child["href"].startswith(("https://", "http://"))
    ]


def _assistant_text(entry: Any) -> tuple[str, str]:
    """(message id, text) of a transcript entry written by the assistant; ('', '') otherwise."""
    if not isinstance(entry, dict) or entry.get("type") != "assistant" or not isinstance(entry.get("message"), dict):
        return "", ""
    content = entry["message"].get("content")
    blocks = content if isinstance(content, list) else []
    text = "\n\n".join(
        block["text"] for block in blocks if isinstance(block, dict) and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    )
    return _text(entry["message"].get("id")), text


def _tail_entries(session_id: str, size: int) -> list[Any]:
    """The entries in the last `size` bytes of the session's transcript, newest first; [] without one."""
    paths = list(PROJECTS_DIR.glob(f"*/{session_id}.jsonl"))
    if not paths:
        return []
    return _read_tail(max(paths, key=lambda candidate: candidate.stat().st_mtime), size) or []


def _read_tail(path: Path, size: int) -> list[Any] | None:
    """The entries in the last `size` bytes of the transcript at `path`, newest first; None when it cannot be read."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - size))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError as exc:
        log.error("could not read %s: %s", path, exc)
        return None
    entries = []
    for line in reversed(tail.split("\n")):
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue  # the first line of a tail read is usually cut
    return entries


def last_message(session_id: str) -> str:
    """The session's last assistant message (Markdown), whole, from the end of its transcript, or ''."""
    # One message can span several entries that share its id, one per content block.
    parts: list[str] = []
    message_id = None
    for entry in _tail_entries(session_id, TRANSCRIPT_TAIL_BYTES):
        entry_id, text = _assistant_text(entry)
        if message_id is not None and entry_id != message_id:
            break
        if text.strip():
            message_id = entry_id
            parts.append(text)
    return "\n\n".join(reversed(parts))


def last_model(session_id: str) -> str:
    """The model of the session's last assistant entry, from the end of its transcript, or '' without one."""
    for entry in _tail_entries(session_id, TRANSCRIPT_TAIL_BYTES):
        if isinstance(entry, dict) and entry.get("type") == "assistant" and isinstance(entry.get("message"), dict):
            model = _text(entry["message"].get("model"))
            # Claude Code writes "<synthetic>" for the notes it adds itself, such as an API error.
            if model and not model.startswith("<"):
                return model
    return ""


def _instruction(entry: Any) -> dict[str, str] | None:
    """A transcript entry as an instruction the session got: who gave it, when, and its words without
    Cockpit's header and context block; None for any other entry.

    An instruction is a prompt typed in the session, delivered to it by Cockpit, or sent by another session.
    One that comes while a turn is under way is queued, and the transcript records it as an attachment.
    Tool results, notices of finished background tasks, a subagent's result, skills' text, images, notes
    Claude Code adds (`isMeta`) and the summary of a compacted conversation are not.
    """
    if not isinstance(entry, dict) or entry.get("isCompactSummary"):
        return None
    origin = entry.get("origin")
    kind = origin.get("kind") if isinstance(origin, dict) else origin
    if entry.get("type") == "attachment":
        queued = entry.get("attachment")
        if not isinstance(queued, dict) or queued.get("type") != "queued_command" or queued.get("commandMode") != "prompt":
            return None
        content: Any = queued.get("prompt")
    elif entry.get("type") == "user" and kind in (None, "human", "peer") and not (entry.get("isMeta") and kind != "peer"):
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
    else:
        return None
    if isinstance(content, list):
        content = "\n".join(
            block["text"] for block in content
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
        )
    if not isinstance(content, str):
        return None
    return instruction(content.removeprefix(PEER_PREFIX), _text(entry.get("timestamp"))[:19] + "Z", kind == "peer")


def instruction(text: str, at: str, peer: bool) -> dict[str, str] | None:
    """A prompt's words as an instruction (`_instruction`), for a Claude transcript and a Codex thread alike: who
    gave it, by Cockpit's header, and each message's words without its header and context block. None for an empty
    prompt or one Claude Code wrote in tags."""
    if not text.strip() or text.startswith("<"):
        return None
    header = COCKPIT_HEADER.match(text)
    if header is not None:
        author = "cockpit" if header["cockpit"] else f"agent:{header['agent']}" if header["agent"] else "user"
        sections = [text[header.end():]]
        batch_end = COCKPIT_BATCH_END.search(text)
        if batch_end is not None:
            # A batch: each message after its own separator and sender line, then the batch's note.
            sections, start = [], header.end()
            for following in COCKPIT_HEADER.finditer(text, start, batch_end.start()):
                end = following.start() - len(COCKPIT_BATCH_SEPARATOR)
                if text[end:following.start()] == COCKPIT_BATCH_SEPARATOR:
                    sections.append(text[start:end])
                    start = following.end()
            sections.append(text[start:batch_end.start()])
        words = "\n\n".join(section.split(COCKPIT_CONTEXT)[0].strip() for section in sections)
    else:
        author = "session" if peer else "user"
        words = text.split(COCKPIT_CONTEXT)[0].strip()
    return {"author": author, "at": at, "text": words[:INSTRUCTION_MAX]}


def last_instruction(session_id: str) -> dict[str, str] | None:
    """The latest instruction the session got (`_instruction`), from the end of its transcript; None when
    there is none in its last INSTRUCTION_SEARCH_BYTES. A long turn of tool calls can lie between it and
    the end, so the search widens step by step."""
    size = TRANSCRIPT_TAIL_BYTES
    while True:
        for entry in _tail_entries(session_id, size):
            found = _instruction(entry)
            if found is not None:
                return found
        if size >= INSTRUCTION_SEARCH_BYTES:
            return None
        size *= 4


def _tool_calls(entry: Any) -> list[dict[str, Any]]:
    """The tool calls in a transcript entry written by the assistant, in order, each {id, at, tool_name, tool_input}."""
    if not isinstance(entry, dict) or entry.get("type") != "assistant" or not isinstance(entry.get("message"), dict):
        return []
    content = entry["message"].get("content")
    return [
        {"id": _text(block.get("id")), "at": _text(entry.get("timestamp"))[:19] + "Z", "tool_name": _text(block.get("name")),
         "tool_input": block["input"]}
        for block in (content if isinstance(content, list) else [])
        if isinstance(block, dict) and block.get("type") == "tool_use" and isinstance(block.get("input"), dict)
    ]


def calls_before(session_id: str, tool_use_id: str, since: str) -> list[dict[str, Any]]:
    """The session's tool calls from `since` (ISO UTC) up to the call `tool_use_id`, newest first, each
    {at, tool_name, tool_input}, from the end of its transcript (SPEC PERM-13). Calls after `tool_use_id`, which a
    message can make beside it, are left out. The search widens as in last_instruction until it reaches `since`. Empty
    when `tool_use_id` is not there, as for a subagent's call, which has its own transcript: the calls found then may
    have come after it, and a later call cannot be what auto mode judged it by."""
    size = TRANSCRIPT_TAIL_BYTES
    read = -1
    while True:
        entries = _tail_entries(session_id, size)
        calls: list[dict[str, Any]] = []
        found = False
        reached = False
        for entry in entries:
            at = _text(entry.get("timestamp")) if isinstance(entry, dict) else ""
            if at and at[:19] < since[:19]:
                reached = True
                break
            for call in reversed(_tool_calls(entry)):
                if call["id"] == tool_use_id:
                    calls = []
                    found = True
                else:
                    calls.append({key: call[key] for key in ("at", "tool_name", "tool_input")})
        # The whole transcript is read once a wider read finds no more entries.
        if reached or size >= INSTRUCTION_SEARCH_BYTES or len(entries) == read:
            return calls if found else []
        read = len(entries)
        size *= 4


def transcript_before(session_id: str, tool_use_id: str, budget: int) -> list[dict[str, str]] | None:
    """The session's transcript up to and with the call `tool_use_id`, oldest first, for Claude to explain the call
    (SPEC PERM-3): each part {at, kind, text}, where kind is `instruction` (with `author`, as in _instruction),
    `says` (the session's words), `call` (with `tool_name`; text is the call's input as JSON) or `result`. Each
    text is cut to TRANSCRIPT_PART_MAX characters. It goes back from the call until the texts hold `budget`
    characters, and starts with the latest instruction before the call wherever that lies. None when the call is
    not in the transcript's last INSTRUCTION_SEARCH_BYTES."""
    found = _entries_from_call(session_id, tool_use_id)
    if found is None:
        return None
    entries, start = found
    parts: list[dict[str, str]] = []
    used = 0
    instructed = False
    for entry in entries[start:]:
        if used >= budget and instructed:
            break
        instruction = _instruction(entry)
        if instruction is not None:
            parts.append({**instruction, "kind": "instruction", "text": _cut(instruction["text"])})
            used += len(parts[-1]["text"])
            instructed = True
        elif used < budget:
            for part in reversed(_transcript_parts(entry)):
                parts.append(part)
                used += len(part["text"])
    return parts[::-1]


def transcript_after(session_id: str, tool_use_id: str, budget: int) -> list[dict[str, str]] | None:
    """What the session did after the call `tool_use_id`, oldest first, in the parts of transcript_before, until
    the texts hold `budget` characters: whether it found another way or still needs the call (SPEC PERM-18).
    None when the call is not in the transcript's last INSTRUCTION_SEARCH_BYTES."""
    found = _entries_from_call(session_id, tool_use_id)
    if found is None:
        return None
    entries, start = found
    parts: list[dict[str, str]] = []
    used = 0
    for entry in reversed(entries[:start]):
        if used >= budget:
            break
        instruction = _instruction(entry)
        added = (
            [{**instruction, "kind": "instruction", "text": _cut(instruction["text"])}] if instruction is not None
            else _transcript_parts(entry)
        )
        parts.extend(added)
        used += sum(len(part["text"]) for part in added)
    return parts


# The session view of the sidebar page (SPEC VIEW-26): its last parts, each text cut this long.
SESSION_LOG_PARTS = 60
SESSION_LOG_TEXT_MAX = 12000
SESSION_LOG_RESULT_MAX = 1500


def session_log(session_id: str) -> dict[str, Any]:
    """The end of the session's conversation as a classic session view shows it, oldest first: `parts`, each
    {at, kind, text}, where kind is `instruction` (with `author`, as in _instruction), `says` (the session's words,
    Markdown) or `call` (with its `id`, `tool_name`, `title`, its `result` and whether it was an `error`), and `total`, the parts
    read. The read widens from the transcript's end until it holds SESSION_LOG_PARTS parts."""
    size = TRANSCRIPT_TAIL_BYTES
    while True:
        entries = _tail_entries(session_id, size)
        parts = _log_parts(entries)
        if len(parts) >= SESSION_LOG_PARTS or size >= INSTRUCTION_SEARCH_BYTES:
            return {"parts": parts[-SESSION_LOG_PARTS:], "total": len(parts)}
        widened = _tail_entries(session_id, size * 4)
        if len(widened) == len(entries):
            return {"parts": parts, "total": len(parts)}
        size *= 4


def _log_parts(entries: list[Any]) -> list[dict[str, Any]]:
    """session_log's parts from transcript entries given newest first; a call's result comes after it, so the
    results are gathered on the way back and joined to their calls."""
    results: dict[str, tuple[str, bool]] = {}
    parts: list[dict[str, Any]] = []
    for entry in entries:
        instruction = _instruction(entry)
        if instruction is not None:
            parts.append({**instruction, "kind": "instruction"})
            continue
        if not isinstance(entry, dict) or not isinstance(entry.get("message"), dict):
            continue
        content = entry["message"].get("content")
        at = _text(entry.get("timestamp"))[:19] + "Z"
        for block in reversed(content if isinstance(content, list) else []):
            if not isinstance(block, dict):
                continue
            if entry.get("type") == "user" and block.get("type") == "tool_result":
                result = block.get("content")
                if isinstance(result, list):
                    result = "\n".join(_text(part.get("text")) for part in result if isinstance(part, dict))
                results[_text(block.get("tool_use_id"))] = (_text(result), bool(block.get("is_error")))
            elif entry.get("type") == "assistant" and block.get("type") == "text" and _text(block.get("text")).strip():
                parts.append({"at": at, "kind": "says", "text": _text(block["text"])[:SESSION_LOG_TEXT_MAX]})
            elif entry.get("type") == "assistant" and block.get("type") == "tool_use":
                tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                title, text = _call_words(_text(block.get("name")), tool_input)
                result, error = results.get(_text(block.get("id")), ("", False))
                cut = result if len(result) <= SESSION_LOG_RESULT_MAX else result[:SESSION_LOG_RESULT_MAX] + " …"
                parts.append({"at": at, "kind": "call", "id": _text(block.get("id")), "tool_name": _text(block.get("name")),
                              "title": title, "text": text[:SESSION_LOG_TEXT_MAX], "result": cut, "error": error})
    return parts[::-1]


def _call_words(name: str, tool_input: dict[str, Any]) -> tuple[str, str]:
    """A tool call as one title line and its input, as a terminal shows it."""
    home = str(Path.home())
    short = lambda value: _text(value).replace(home, "~")
    if name == "Bash":
        return _text(tool_input.get("description")), short(tool_input.get("command"))
    if name in ("Read", "Write", "Edit"):
        return short(tool_input.get("file_path")), ""
    if name in ("Grep", "Glob"):
        return _text(tool_input.get("pattern")), short(tool_input.get("path"))
    if name == "Agent":
        return _text(tool_input.get("description")), _text(tool_input.get("prompt"))
    if name == "Skill":
        return _text(tool_input.get("skill")), _text(tool_input.get("args"))
    return "", json.dumps(tool_input, ensure_ascii=False)


def _entries_from_call(session_id: str, tool_use_id: str) -> tuple[list[Any], int] | None:
    """The transcript's last entries, newest first, and the index of the one that makes the call `tool_use_id`; it
    reads further back, up to INSTRUCTION_SEARCH_BYTES, until it finds the call. None when it does not."""
    size = TRANSCRIPT_EXPLAIN_BYTES
    while True:
        entries = _tail_entries(session_id, size)
        start = next(
            (index for index, entry in enumerate(entries) if any(call["id"] == tool_use_id for call in _tool_calls(entry))),
            None,
        )
        if start is not None:
            return entries, start
        if size >= INSTRUCTION_SEARCH_BYTES:
            return None
        size *= 4


SKILL_BASE = "Base directory for this skill: "


def _skill_loads(entry: Any) -> tuple[bool, list[tuple[str, str]]]:
    """(carried, [(base directory, loaded text)]) for the skills a transcript entry loads: the Skill tool's load, or,
    carried, the list of skills Claude Code carries over a compaction or into a resumed transcript (`invoked_skills`),
    each with the text it was loaded with."""
    if not isinstance(entry, dict):
        return False, []
    texts: list[str] = []
    carried = False
    if entry.get("type") == "user" and entry.get("isMeta") and isinstance(entry.get("message"), dict):
        content = entry["message"].get("content")
        texts = [content] if isinstance(content, str) else [
            block["text"] for block in content or [] if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
    elif entry.get("type") == "attachment" and isinstance(entry.get("attachment"), dict):
        if entry["attachment"].get("type") == "invoked_skills":
            texts = [_text(skill.get("content")) for skill in entry["attachment"].get("skills") or []]
            carried = True
    return carried, [
        (text[len(SKILL_BASE):].split("\n", 1)[0].strip(), text) for text in texts if text.startswith(SKILL_BASE)
    ]


def _skill_version(folder: str, loaded_at: str, text: str) -> str:
    """The skill's last commit before it was loaded, when its folder is in a git checkout; else `md5:` and the start
    of the loaded text's MD5, which also holds any arguments the skill was given."""
    real = Path(folder).resolve()
    if real.is_dir():
        found = subprocess.run(
            ["git", "-C", str(real), "log", "-1", "--format=%h", f"--before={loaded_at}", "--", "."],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if found.returncode == 0 and found.stdout.strip():
            return found.stdout.strip()
        if found.returncode != 0 and "not a git repository" not in found.stderr:
            raise RuntimeError(f"git log in {real} failed: {found.stderr.strip()}")
    return "md5:" + hashlib.md5(text.encode()).hexdigest()[:12]


def skills_loaded(session_id: str, before: str) -> list[dict[str, str]]:
    """The skills the session had loaded by `before` (ISO UTC), for a writing example (SPEC FEEDBACK-8): each
    {name, folder, version, loaded_at} from its latest load, in the order first loaded; [] without a transcript.

    The transcript is read whole, but only lines that name a skill's base directory are parsed."""
    paths = list(PROJECTS_DIR.glob(f"*/{session_id}.jsonl"))
    if not paths:
        return []
    path = max(paths, key=lambda candidate: candidate.stat().st_mtime)
    loads: dict[str, tuple[str, str, str]] = {}
    marker = SKILL_BASE.encode()
    with path.open("rb") as handle:
        for line in handle:
            if marker not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue  # a line Claude Code is still writing
            at = _text(entry.get("timestamp"))
            if at[:19] > before[:19]:
                break
            carried, found = _skill_loads(entry)
            for folder, text in found:
                # A carried skill was loaded before; its own load, when the transcript has it, tells when.
                if not carried or Path(folder).name not in loads:
                    loads[Path(folder).name] = (folder, at, text)
    return [
        {"name": name, "folder": folder, "version": _skill_version(folder, at, text), "loaded_at": at}
        for name, (folder, at, text) in loads.items()
    ]


def _transcript_parts(entry: Any) -> list[dict[str, str]]:
    """What the session said, its calls and their results in one transcript entry, in order (transcript_before)."""
    if not isinstance(entry, dict) or not isinstance(entry.get("message"), dict):
        return []
    at = _text(entry.get("timestamp"))[:19] + "Z"
    content = entry["message"].get("content")
    parts = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if entry.get("type") == "assistant" and block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append({"at": at, "kind": "says", "text": _cut(block["text"])})
        elif entry.get("type") == "assistant" and block.get("type") == "tool_use":
            text = json.dumps(block.get("input"), ensure_ascii=False)
            parts.append({"at": at, "kind": "call", "tool_name": _text(block.get("name")), "text": _cut(text)})
        elif entry.get("type") == "user" and block.get("type") == "tool_result":
            result = block.get("content")
            if isinstance(result, list):
                result = "\n".join(_text(part.get("text")) for part in result if isinstance(part, dict))
            parts.append({"at": at, "kind": "result", "text": _cut(_text(result))})
    return parts


def _cut(text: str) -> str:
    return text if len(text) <= TRANSCRIPT_PART_MAX else text[:TRANSCRIPT_PART_MAX] + f"… ({len(text)} characters)"


def turn_ended(session_id: str) -> bool:
    """The session's last turn is over: the newest entry of its transcript is Claude Code's note of the
    turn's length (`turn_duration`) or a reply that ended the turn. While a turn runs, the newest is a tool
    call, a tool's result or the prompt. Background work the session started can run on after its turn."""
    for entry in _tail_entries(session_id, TRANSCRIPT_TAIL_BYTES):
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "system" and entry.get("subtype") == "turn_duration":
            return True
        if entry.get("type") == "assistant":
            message = entry.get("message")
            return isinstance(message, dict) and message.get("stop_reason") == "end_turn"
        if entry.get("type") == "user":
            return False
    return False


@dataclass(frozen=True)
class LimitStop:
    at: str  # ISO time of the turn that ended on the limit
    resets: datetime | None  # when the limit ends, as the stop's text says; None when the text names no time


# "You've hit your weekly limit · resets Jan 15, 10am (UTC)", "You've hit your session limit · resets 10:00am (UTC)"
RESETS = re.compile(
    r"resets (?:(?P<month>[A-Z][a-z]{2}) (?P<day>\d{1,2}), )?(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?(?P<half>am|pm)"
    r" \((?P<zone>[^)]+)\)"
)
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# The model Claude Code names on an assistant entry it wrote itself, without a call: an error such as the usage
# limit, or a note.
SYNTHETIC_MODEL = "<synthetic>"


@dataclass(frozen=True)
class Usage:
    """What the end of a transcript says about Claude's usage."""
    stop: LimitStop | None  # the last turn, when it ended on the usage limit
    answer: tuple[datetime, bool] | None  # Claude's last answer and whether it went through


# transcript_usage's answer for each session: (transcript, its size, its modification time in ns, answer). Every
# poll asks it of every stopped session, and most transcripts do not change between polls.
_usages: dict[str, tuple[str, int, int, Usage]] = {}


def transcript_usage(session_id: str) -> Usage:
    """What the end of the session's transcript says about usage. The answer is kept while the transcript stays the
    same size and age; one that could not be read is read again on the next ask."""
    try:
        found = [(path, path.stat()) for path in PROJECTS_DIR.glob(f"*/{session_id}.jsonl")]
    except OSError as exc:
        log.error("could not read the transcript of session %s: %s", session_id, exc)
        return Usage(stop=None, answer=None)
    if not found:
        return Usage(stop=None, answer=None)
    path, stat = max(found, key=lambda pair: pair[1].st_mtime_ns)
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    kept = _usages.get(session_id)
    if kept is not None and kept[:3] == key:
        return kept[3]
    entries = _read_tail(path, TRANSCRIPT_TAIL_BYTES)
    if entries is None:
        return Usage(stop=None, answer=None)
    found_usage = Usage(stop=_limit_stop_of(entries), answer=_answer_of(entries))
    _usages[session_id] = (*key, found_usage)
    return found_usage


def limit_stop(session_id: str) -> LimitStop | None:
    """The session's last turn when it ended on Claude's usage limit, else None.

    Claude Code ends the turn with an assistant entry marked `"error": "rate_limit"`, its text the line
    the terminal shows: "You've hit your weekly limit · resets Jan 15, 10am (UTC)".
    """
    return transcript_usage(session_id).stop


def last_answer(session_id: str, since: datetime) -> tuple[datetime, bool] | None:
    """When Claude last answered the session and whether that answer went through (an answer that ended on an error,
    such as the usage limit, did not), when it came after `since`; None otherwise."""
    answer = transcript_usage(session_id).answer
    return answer if answer is not None and answer[0] > since else None


def _limit_stop_of(entries: list[Any]) -> LimitStop | None:
    for entry in entries:
        if isinstance(entry, dict) and entry.get("type") == "assistant":
            if entry.get("error") != "rate_limit":
                return None
            at = _text(entry.get("timestamp"))
            content = entry.get("message", {}).get("content") if isinstance(entry.get("message"), dict) else None
            words = " ".join(part.get("text", "") for part in content if isinstance(part, dict)) if isinstance(
                content, list
            ) else ""
            stopped = _parse_iso(at)
            return LimitStop(at=at, resets=limit_resets(words, stopped) if stopped else None)
    return None


def _answer_of(entries: list[Any]) -> tuple[datetime, bool] | None:
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        message = entry.get("message")
        # Claude Code writes some notes itself, with no call ("No response requested."): they say nothing of usage.
        if entry.get("error") is None and isinstance(message, dict) and message.get("model") == SYNTHETIC_MODEL:
            continue
        at = _parse_iso(_text(entry.get("timestamp")))
        return (at, entry.get("error") is None) if at is not None else None
    return None


# The turn a sign-out ended is the transcript's last assistant entry, a short one; a few entries can follow it.
LOGIN_STOP_TAIL_BYTES = 32 * 1024


def login_stop(session_id: str) -> datetime | None:
    """When the session's last turn ended on "Login expired", else None (SPEC UI-40).

    With the account signed out, Claude Code ends the turn with an assistant entry marked
    `"error": "authentication_failed"`, its text "Login expired · Please run /login".
    """
    for entry in _tail_entries(session_id, LOGIN_STOP_TAIL_BYTES):
        if isinstance(entry, dict) and entry.get("type") == "assistant":
            if entry.get("error") != "authentication_failed":
                return None
            return _parse_iso(_text(entry.get("timestamp")))
    return None


# Stop texts whose reset time limit_resets could not read, logged once each.
_unread_stops: set[str] = set()


def limit_resets(words: str, stopped: datetime) -> datetime | None:
    """When the limit in a stop's text ends: its day and time, or its time on the first day after `stopped` that
    has it. None, logged, when the text names no time Cockpit can read."""
    found = RESETS.search(words)
    if found is None:
        # Every poll reads the stop again; one line per text is enough.
        if words not in _unread_stops:
            _unread_stops.add(words)
            log.error("a usage-limit stop names no reset time Cockpit can read: %r", words)
        return None
    try:
        zone = ZoneInfo(found["zone"])
    except (ZoneInfoNotFoundError, ValueError):
        log.error("a usage-limit stop names a time zone Cockpit does not know: %r", words)
        return None
    hour = int(found["hour"]) % 12 + (12 if found["half"] == "pm" else 0)
    local = stopped.astimezone(zone)
    try:
        if found["month"]:
            when = local.replace(
                month=MONTHS.index(found["month"]) + 1, day=int(found["day"]), hour=hour,
                minute=int(found["minute"] or 0), second=0, microsecond=0,
            )
            if when < local - timedelta(days=1):
                when = when.replace(year=when.year + 1)
            return when
        when = local.replace(hour=hour, minute=int(found["minute"] or 0), second=0, microsecond=0)
    except ValueError:
        log.error("a usage-limit stop names a reset time that does not exist: %r", words)
        return None
    return when if when > local else when + timedelta(days=1)


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _in_flight(value: Any) -> list[str]:
    kinds = value.get("kinds") if isinstance(value, dict) else None
    return [kind for kind in kinds if isinstance(kind, str)] if isinstance(kinds, list) else []


def _effort(flags: Any) -> str:
    if not isinstance(flags, list) or "--effort" not in flags[:-1]:
        return ""
    return _text(flags[flags.index("--effort") + 1])


def running_version(pid: int) -> str:
    """The Claude Code release process `pid` runs: the name of its executable, which the installer names after
    the release (`2.1.300`; `2.1.291 (deleted)` once a newer install removed it). Empty when the process is gone,
    or is another user's, which no session of ours is."""
    try:
        executable = Path(f"/proc/{pid}/exe").readlink()
    except (FileNotFoundError, PermissionError):
        return ""
    return executable.name.removesuffix(" (deleted)")


def _session(account: Account, row: dict[str, Any], warnings: list[str]) -> Session:
    short_id = _text(row.get("id"))
    job = _read_json(account.jobs / short_id / "state.json", warnings) if short_id else {}
    started_ms = row.get("startedAt")
    started = (
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_ms / 1000)) if isinstance(started_ms, int) else ""
    )
    pid = row.get("pid")
    return Session(
        id=short_id,
        session_id=_text(row.get("sessionId")),
        name=_text(row.get("name")) or _text(job.get("name")),
        state=_text(row.get("state")),
        pid=pid if isinstance(pid, int) else None,
        status=_text(row.get("status")),
        tempo=_text(job.get("tempo")),
        job_state=_text(job.get("state")),
        in_flight=_in_flight(job.get("inFlight")),
        cwd=_text(row.get("cwd")),
        detail=_text(job.get("detail")),
        needs=_text(job.get("needs")),
        intent=_text(job.get("intent")),
        updated_at=_text(job.get("updatedAt")) or started,
        links=_links(job.get("children")),
        effort=_effort(job.get("respawnFlags")),
        version=running_version(pid) if isinstance(pid, int) else "",
        account=account.name,
    )


def list_account(account: Account, warnings: list[str]) -> list[Session]:
    """The sessions in one account's `claude agents` list; problems with individual job files are appended to
    `warnings`."""
    result = subprocess.run(
        claude_command(account, ["claude", "agents", "--json", "--all"]),
        capture_output=True, text=True, timeout=30, check=False,
    )
    if result.returncode != 0:
        # systemd-run says here when it cannot find `claude`, as in a shell without the user's login PATH.
        raise subprocess.SubprocessError(
            f"`claude agents --json --all` exited with code {result.returncode}"
            f"{'' if account.default else f' on the {account.name} account'}. Its output: {result.stderr.strip() or 'none'}"
        )
    rows = json.loads(result.stdout)
    if not isinstance(rows, list):
        raise ValueError("`claude agents --json --all` did not answer with a list")  # noqa: TRY004 - bad output
    return [
        _session(account, row, warnings)
        for row in rows if isinstance(row, dict) and isinstance(row.get("sessionId"), str)
    ]


@dataclass(frozen=True)
class Move:
    """The user moved the session to another account (moves.py, SPEC SESSION-18)."""
    account: str
    at: str  # ISO time of the move


def list_sessions(
    all_accounts: tuple[Account, ...], moved: dict[str, Move], warnings: list[str]
) -> tuple[list[Session], list[str]]:
    """The sessions of every account in use (accounts.in_use), each tagged with its account (merge), and why each
    failed listing failed. A failed listing is logged and named in `warnings`; the other accounts' sessions still
    list."""
    lists, failed = [], []
    for account in accounts.listed(all_accounts, warnings):
        try:
            lists.append(list_account(account, warnings))
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            log.error("listing the %s account's Claude sessions failed: %s", account.name, exc)
            warnings.append(f"Listing the {account.name} account's Claude sessions failed: {exc}")
            failed.append(str(exc))
    return merge(lists, moved, warnings), failed


def merge(lists: list[list[Session]], moved: dict[str, Move], warnings: list[str]) -> list[Session]:
    """One entry per session from the accounts' lists. A session the user moved shows in the lists of both
    accounts once it has run on the new one; it belongs to the account it ran on last: the one whose process
    runs, else the one that saw its latest change. A move newer than that puts it on the account it was moved
    to, where its next resume runs. A stopped session on a retired account resumes on the default one
    (accounts.current_name). Two live processes of one session are a problem the page shows."""
    by_id: dict[str, list[Session]] = {}
    for listed in lists:
        for session in listed:
            by_id.setdefault(session.session_id, []).append(session)
    merged = []
    for session_id, copies in by_id.items():
        alive = [copy for copy in copies if copy.alive]
        if len(alive) > 1:
            accounts_named = ", ".join(copy.account for copy in alive)
            log.error("session %s runs on more than one Claude account: %s", session_id, accounts_named)
            warnings.append(f"Session {copies[0].id} runs on more than one Claude account at once: {accounts_named}")
        session = max(alive or copies, key=lambda copy: copy.updated_at)
        move = moved.get(session_id)
        if move is not None and move.account != session.account and move.at > session.updated_at:
            session = replace(session, account=move.account)
        if not session.alive:
            session = replace(session, account=accounts.current_name(session.account))
        merged.append(session)
    return merged


def find_session(
    all_accounts: tuple[Account, ...], moved: dict[str, Move], removed: list[Session], session_id: str
) -> Session | None:
    """A fresh lookup that skips the cache: delivery must know whether the process runs now. Raises when a
    listing failed and the session may run on that account: it is in no list, or it was moved, so another
    account may hold the copy that runs. A session Cockpit removed from `claude agents` (`removed`) is found
    stopped, as it was, unless a list has it again."""
    warnings: list[str] = []
    listed, failed = list_sessions(all_accounts, moved, warnings)
    found = next((session for session in listed if session.session_id == session_id), None)
    if failed and (found is None or session_id in moved):
        raise subprocess.SubprocessError("; ".join(failed))
    return next((session for session in with_removed(listed, removed, moved) if session.session_id == session_id), None)


def with_removed(listed: list[Session], removed: list[Session], moved: dict[str, Move]) -> list[Session]:
    """`listed` merged with the sessions Cockpit removed from `claude agents` (removals.py), as merge merges the
    accounts' lists: a removed session that was resumed since is listed as it runs now, its record wins over an older
    copy another account still lists, and a move since its removal puts it on the account it was moved to. On a tie
    the listed copy wins, as for a removal whose answer was lost that did not happen."""
    return merge([listed, removed], moved, [])


def running(listed: list[Session]) -> dict[str, int]:
    """How many sessions run on each Claude account, by account name: those alive whose turn is not over (busy_with),
    which go on using the account's week (SPEC SESSION-17)."""
    counts: dict[str, int] = {}
    for session in listed:
        if session.account and session.busy_with:
            counts[session.account] = counts.get(session.account, 0) + 1
    return counts


def as_dict(session: Session) -> dict[str, Any]:
    return asdict(session) | {
        "alive": session.alive, "working_now": session.working_now, "waits_on_dialog": session.waits_on_dialog,
        "busy_with": session.busy_with,
    }


@dataclass(frozen=True)
class Listing:
    """One listing of every account: its sessions with the warnings, account problems and failed accounts of that
    same listing."""

    sessions: list[Session]
    warnings: list[str]
    account_problems: list[accounts.Problem]
    failed: frozenset[str]


class SessionCache:
    """One listing shared by all requests; problems surface as the listing's `warnings` and `account_problems`.

    `snapshot(wait=False)` answers at once: with a listing older than CACHE_SECONDS it starts the next one in the
    background (SPEC UI-39). `snapshot(wait=True)`, for decisions on whether a session exists, waits for a listing
    that is at most CACHE_SECONDS old. Both wait for the first listing and for the first after `invalidate`.

    When an account's listing fails, its last good list stays and `failed` names the account: right after a
    start that list is empty. Each listing also checks that the accounts still share what they share
    (accounts.link_problems): the problems are `account_problems`, which the page shows with their paths collapsed,
    and are logged as an error each time they change.
    """

    def __init__(
        self, all_accounts: tuple[Account, ...], moved: Callable[[], dict[str, Move]], removed: Callable[[], list[Session]]
    ) -> None:
        self._accounts = all_accounts
        self._moved = moved
        self._removed = removed
        self._lock = threading.Lock()
        # One listing at a time; `_lock` guards the fields and is never held while listing.
        self._listing = threading.Lock()
        # When the last finished listing started and finished, and when the list was last invalidated
        # (time.monotonic()). Age counts from the finish, so a slow listing is still reused for CACHE_SECONDS;
        # invalidation compares with the start, so a listing under way when it came does not count.
        self._started = float("-inf")
        self._finished = float("-inf")
        self._invalidated_at = float("-inf")
        self._refreshing = False
        self._lists: dict[str, list[Session]] = {}
        self._account_problems: list[accounts.Problem] = []
        self._last = Listing([], [], [], frozenset())

    def get(self) -> list[Session]:
        return self.snapshot(wait=False).sessions

    def last(self) -> list[Session]:
        """The last finished listing's sessions, however old, without starting a listing: for a loop that runs more
        often than sessions should be listed. The sweeper's wake loop lists every WAKE_POLL_SECONDS. Before the first
        listing has finished, at Cockpit's start, it waits for one, so that no caller takes "none yet" for "none"."""
        with self._lock:
            if self._finished != float("-inf"):
                return self._last.sessions
        return self.snapshot(wait=True).sessions

    def snapshot(self, wait: bool) -> Listing:
        with self._lock:
            if self._usable(wait):
                return self._last
        with self._listing:
            # A listing that finished while this call waited for its turn may already do.
            with self._lock:
                usable = self._usable(wait)
            if not usable:
                self._list()
        with self._lock:
            return self._last

    def _usable(self, wait: bool) -> bool:
        """Whether the last listing answers a call now; the caller holds `_lock`. A call that need not wait gets
        an expired listing and starts the next one in the background."""
        if self._started <= self._invalidated_at:
            return False
        if time.monotonic() - self._finished <= CACHE_SECONDS:
            return True
        if wait:
            return False
        if not self._refreshing:
            self._refreshing = True
            threading.Thread(target=self._refresh, name="session-listing", daemon=True).start()
        return True

    def invalidate(self) -> None:
        with self._lock:
            self._invalidated_at = time.monotonic()

    def _refresh(self) -> None:
        try:
            with self._listing:
                # A waiting call may have listed while this one waited for its turn.
                with self._lock:
                    usable = self._usable(wait=True)
                if not usable:
                    self._list()
        except Exception:
            log.exception("listing Claude sessions in the background failed")
        finally:
            with self._lock:
                self._refreshing = False

    def _list(self) -> None:
        """One listing of every account; the caller holds `_listing`."""
        started = time.monotonic()
        warnings: list[str] = []
        problems = accounts.link_problems(self._accounts)
        if problems and problems != self._account_problems:
            log.error("Claude accounts no longer share what they should: %s",
                      "; ".join(f"{problem.text} ({problem.detail})" for problem in problems))
        self._account_problems = problems
        failed: set[str] = set()
        used = accounts.listed(self._accounts, warnings)
        # A failed listing keeps the account's last list; an account no longer in use (its sign-in gone) has none.
        lists = {name: listed for name, listed in self._lists.items() if name in {account.name for account in used}}
        for account in used:
            try:
                lists[account.name] = list_account(account, warnings)
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                log.error("listing the %s account's Claude sessions failed: %s", account.name, exc)
                warnings.append(f"Listing the {account.name} account's Claude sessions failed: {exc}")
                failed.add(account.name)
        moved = self._moved()
        merged = with_removed(merge(list(lists.values()), moved, warnings), self._removed(), moved)
        with self._lock:
            self._lists = lists
            self._last = Listing(merged, warnings, problems, frozenset(failed))
            self._started = started
            self._finished = time.monotonic()
