"""Claude use per session and per skill, priced at API list prices (SPEC SESSION-9).

Claude Code writes each session's conversation to `~/.claude/projects/<folder>/<session id>.jsonl`, and each
subagent's to `<session id>/subagents/agent-<id>.jsonl` beside it. Every API call is an assistant entry whose
`message.usage` counts its tokens: input read fresh, input written to the prompt cache (kept 5 minutes or an
hour), input read from the cache, and output. Claude Code writes one entry per content block of a reply, all with
the same message id, and a copy of a session repeats its history under the same ids, so each message id counts once.

A turn runs from one prompt to the next prompt of the same session: one typed, or sent by Cockpit or by another
session. Background tasks' notices (a command or a subagent that finished), subagent hand-backs and a compaction's
summary continue the turn they arrive in. When such a notice wakes a session whose last reply ended its turn, the
calls up to the next reply that ends the turn are the turn's `woken` part: the work that waited for a background task.
A subagent's calls count in its session's turn that was running when the subagent started. A skill counts the turns
that loaded it, with the Skill tool (in the session or one of its subagents) or as a typed command; a turn that loaded
several skills splits its use evenly between them.

A call is a cache miss when it writes to the prompt cache again (or reads uncached) at least half of the input that
the call before it in the same transcript had cached (SESSION-16): the cache expired while the conversation paused, or
the start of the prompt changed. Its missed tokens are the shorter of the two calls' inputs less what the call read
from the cache, and its miss cost is what those tokens cost over reading them back. Every other call misses under a
thousand tokens, the end of the prompt. A transcript's first call, and the first
call after a compaction's summary, start a new prompt and miss nothing. A miss after a pause of PAUSE_MINUTES or more
counts apart, as the cache's time running out.

Each call names its model, and each turn keeps its use per model (SESSION-12): a model counts under the PRICES key
that prices it, so Haiku's subagent calls show apart from Opus's. Calls on a model that is not Claude's are not Claude use and
are left out.

Each turn has an origin, what started it (ORIGINS), read from its prompt. Each session has a kind (KINDS_OF_SESSION):
the sweeper, the one session Cockpit sends sweeps, checks and pauses to, or else what started its first turn.

Use is shown at API list prices and as an estimated share of one account's weekly limit.
WEEK_LIST_USD is the fixed reference budget used for that estimate."""

from __future__ import annotations

import bisect
import json
import logging
import re
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cockpit import codex, config

log = logging.getLogger(__name__)

WEEK_LIST_USD = 2400.0
PLAN_USD_PER_PERCENT = 0.5
REFRESH_SECONDS = 60
# Transcripts last written longer ago than this are not read; the page asks for at most this many days.
HORIZON_DAYS = 35
# A session's recent use, which sets how fast its lamp on the overview pulses while it works (SPEC VIEW-11).
RECENT_MINUTES = 10
# A cache miss after a gap this long since the call before counts as the cache's time running out (SESSION-16): a
# cache write keeps 5 minutes at the least.
PAUSE_MINUTES = 5
# The Usage page's graph per day covers this many days, whatever period the page shows (SPEC SESSION-14).
DAILY_DAYS = 30
# A session's own call on another model than config.MAIN_MODEL shows on the page for this long after it (SESSION-20).
WRONG_MODEL_HOURS = 24
# $ per million tokens, by a part of the model's name, tried in this order: (input, output, cache read). The part
# also names the model on the Usage page.
# Writing to the cache costs 1.25 times input for 5 minutes and 2 times input for an hour.
PRICES = {
    "opus-5-5": (4.0, 20.0, 0.20),
    "sonnet-5-5": (2.0, 10.0, 0.10),
    "haiku-5-5": (0.10, 0.50, 0.01),
    "fable-5-2": (8.0, 40.0, 0.20),
    "fable-5-1": (10.0, 50.0, 0.25),
    "fable-5": (10.0, 50.0, 1.0),
    "opus-5": (5.0, 25.0, 0.50),
    "sonnet-5": (2.0, 10.0, 0.20),
    "opus-4": (5.0, 25.0, 0.50),
    "haiku-4-5": (1.0, 5.0, 0.10),
}
# Models whose calls cost more when the prompt is long: the prompt's size in tokens above which they do, and their
# prices then. A call's prompt is its input read fresh, written to the cache and read from it.
LONG_PROMPT_PRICES = {"haiku-5-5": (100_000, (0.50, 2.50, 0.05))}
# A background command's or subagent's notice that it finished.
NOTICES = ("<task-notification", "<agent-message")
# Prompts that continue the turn they arrive in rather than start one.
CONTINUES = (
    *NOTICES, "<system-reminder", "<local-command", "Caveat:", "<bash-", "<artifact-content-authored-by-others",
    "This session is being continued", "<command-message",
)
# The entry Claude Code writes where it compacted the conversation, which starts the prompt anew.
COMPACTED = '"subtype":"compact_boundary"'
# A message that Cockpit or another session sends over a session's socket arrives as a prompt that starts with this.
PEER_PROMPT = "Another Claude session sent a message:"
COMMAND = re.compile(r"<command-name>/?([^<\s]+)</command-name>")

# What started a turn:
#   typed        a prompt typed in a terminal
#   user_cockpit the user's message on Cockpit (sessions.prefixed)
#   sweep        a sweep, check or pause Cockpit gave the sweeper, also when the user pressed Update
#   sweeper_asks a message from the sweeper, which sends as `sweep` in a sweep and as `check` in a check
#   session_asks a message from another session, through Cockpit or Claude Code
#   cockpit      Cockpit's own other messages: a command's result, an archived step, usage back after a limit
#   script       a prompt to `claude -p` or the Agent SDK
#   unknown      calls whose prompt was not read
ORIGINS = ("typed", "user_cockpit", "sweep", "sweeper_asks", "session_asks", "cockpit", "script", "unknown")
SWEEPER_PROMPTS = ("Cockpit wake-up", "Cockpit check", "Cockpit pause", "Cockpit fill", "Cockpit reorder", config.SWEEP_PROMPT)
SWEEPER_AGENTS = ("sweep", "check")
COCKPIT_ITSELF = "Message from Cockpit itself"
AGENT_MESSAGE = re.compile(r"Message from agent ([a-z0-9-]+) via Cockpit")
USER_MESSAGE = re.compile(r"Message from [^\n]{1,60} via Cockpit:")
SESSION_MESSAGES = ("<cross-session-message", 'Session "', "This session was created by")
# A session's kind: the sweeper, or else by what started its first turn whose prompt was read.
KINDS_OF_SESSION = {
    "typed": "terminal", "user_cockpit": "user_cockpit", "sweeper_asks": "sweeper_started",
    "session_asks": "session_started", "cockpit": "cockpit_started", "script": "script", "unknown": "unknown",
}
# The most expensive turns of a period the page lists.
TOP_TURNS = 50
# The turns with the costliest cache misses the page lists.
MISS_TURNS = 10
# The start of a turn's prompt kept to show it.
PROMPT_CHARS = 240
# Claude Code writes an assistant entry's message as {"model", "id", ..., "content", ..., "usage", ...} and its
# timestamp after the message. These read a call without parsing its content, which can run to megabytes; an entry
# laid out otherwise is parsed whole.
CALL_HEAD = re.compile(r'"message":\{"model":"([^"]*)","id":"([^"]*)"')
USAGE_KEY = '"usage":{"input_tokens":'
STAMP_KEY = '"timestamp":"'
STOP_KEY = '"stop_reason":'
SKILL_CALL = re.compile(r'"name":"Skill","input":\{"skill":"([^"]+)"')
DECODER = json.JSONDecoder()


KINDS = ("fresh", "cache_write", "cache_read", "output")


@dataclass
class Use:
    """Tokens of each kind and what they cost at list prices: input read fresh, input written to the cache, input
    read from the cache, output."""

    tokens: dict[str, int] = field(default_factory=lambda: dict.fromkeys(KINDS, 0))
    usd: dict[str, float] = field(default_factory=lambda: dict.fromkeys(KINDS, 0.0))
    calls: int = 0
    subagent_usd: float = 0.0  # the part of usd that subagents' calls cost
    woken_usd: float = 0.0  # the part of usd that calls after a background task woke the session cost
    misses: int = 0  # calls that were cache misses
    miss_tokens: int = 0  # input the cache had held and the misses wrote again
    miss_usd: float = 0.0  # what writing them again cost over reading them back
    paused_miss_usd: float = 0.0  # the part of miss_usd after a pause of PAUSE_MINUTES or more

    def add(self, other: Use, share: float) -> None:
        for kind in KINDS:
            self.tokens[kind] += round(other.tokens[kind] * share)
            self.usd[kind] += other.usd[kind] * share
        self.calls += round(other.calls * share)
        self.subagent_usd += other.subagent_usd * share
        self.woken_usd += other.woken_usd * share
        self.misses += round(other.misses * share)
        self.miss_tokens += round(other.miss_tokens * share)
        self.miss_usd += other.miss_usd * share
        self.paused_miss_usd += other.paused_miss_usd * share

    def total(self) -> float:
        return sum(self.usd.values())

    def as_dict(self) -> dict[str, Any]:
        usd = self.total()
        percent = usd / WEEK_LIST_USD * 100
        return {
            "tokens": dict(self.tokens), "usd_by_kind": {kind: round(value, 2) for kind, value in self.usd.items()},
            "calls": self.calls, "usd": round(usd, 2), "week_percent": round(percent, 2),
            "plan_usd": round(percent * PLAN_USD_PER_PERCENT, 2), "subagent_usd": round(self.subagent_usd, 2),
            "woken_usd": round(self.woken_usd, 2), "misses": self.misses, "miss_tokens": self.miss_tokens,
            "miss_usd": round(self.miss_usd, 2), "paused_miss_usd": round(self.paused_miss_usd, 2),
        }


@dataclass
class Turn:
    start: str
    origin: str  # one of ORIGINS
    prompt: str = ""  # the start of the prompt that started it
    skills: list[str] = field(default_factory=list)
    use: Use = field(default_factory=Use)
    models: dict[str, Use] = field(default_factory=dict)  # its use per PRICES key


@dataclass
class ModelCalls:
    """A session's own calls on one model: the first and last one's time, and their message ids."""

    first: str
    last: str
    ids: set[str] = field(default_factory=set)


@dataclass
class Transcript:
    """What has been read of one transcript file so far."""

    session_id: str
    subagent: bool
    offset: int = 0
    title: str = ""  # the name the session was given, from its latest custom-title entry
    first_prompt: str = ""
    cwd: str = ""
    first: str = ""  # times of its first and last priced call
    last: str = ""
    started: str = ""  # The subagent's first prompt, before its first priced answer.
    # Whether its last reply ended the turn, so the session waited for a prompt.
    waiting: bool = True
    # Whether a background task's notice woke it and no reply has ended the turn since.
    woken: bool = False
    turns: list[Turn] = field(default_factory=list)
    # A subagent's file counts as one block of use, with the skills it loaded, in the turn running when it started.
    use: Use = field(default_factory=Use)
    models: dict[str, Use] = field(default_factory=dict)
    skills: list[str] = field(default_factory=list)
    # Each priced call of the last RECENT_MINUTES before its latest call: its time and what it cost.
    recent: list[tuple[str, float]] = field(default_factory=list)
    # The input of its last priced call, which the cache holds after it, and that call's time; 0 once a compaction
    # starts the prompt anew.
    cached: int = 0
    cached_at: str = ""
    # Its own calls (not a subagent's, not in a `claude -p` turn) by model, and its latest such call's time and model.
    own_models: dict[str, ModelCalls] = field(default_factory=dict)
    latest: tuple[str, str] = ("", "")
    # The models someone picked with /model in this session: each is the one its next own call ran on.
    picked: set[str] = field(default_factory=set)
    picking: bool = False


@dataclass
class SessionModels:
    """A session's own calls by model over all its transcripts, and what wrong_models shows of it."""

    session_id: str
    title: str
    first_prompt: str
    cwd: str
    per_model: dict[str, ModelCalls]
    picked: set[str]
    latest: str  # the model of its latest own call


class Group:
    """Use summed by a name (a skill, an origin, a kind of session), with how many turns and sessions it covers."""

    def __init__(self) -> None:
        self.use: dict[str, Use] = {}
        self.turns: dict[str, int] = {}
        self.sessions: dict[str, set[str]] = {}

    def add(self, name: str, session_id: str, use: Use, share: float) -> None:
        self.use.setdefault(name, Use()).add(use, share)
        self.turns[name] = self.turns.get(name, 0) + 1
        self.sessions.setdefault(name, set()).add(session_id)

    def rows(self, key: str) -> list[dict[str, Any]]:
        """One row per name, the most expensive first."""
        rows = [
            {key: name, "turns": self.turns[name], "sessions": len(self.sessions[name]), "use": use.as_dict()}
            for name, use in self.use.items()
        ]
        return sorted(rows, key=lambda row: -row["use"]["usd"])


def priced_as(model: str) -> str | None:
    """The PRICES key that prices a model's calls."""
    return next((part for part in PRICES if part in model), None)


def add_models(into: dict[str, Use], models: dict[str, Use]) -> None:
    for name, use in models.items():
        into.setdefault(name, Use()).add(use, 1)


def call_prices(model: str, usage: dict[str, Any]) -> tuple[float, float, float]:
    """The prices of a call on the PRICES key `model`, by the size of its prompt."""
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    prompt = sum(int(usage.get(key) or 0) for key in keys)
    if model in LONG_PROMPT_PRICES and prompt > LONG_PROMPT_PRICES[model][0]:
        return LONG_PROMPT_PRICES[model][1]
    return PRICES[model]


def call_use(usage: dict[str, Any], prices: tuple[float, float, float]) -> Use:
    write = usage.get("cache_creation") or {}
    write_5m = int(write.get("ephemeral_5m_input_tokens") or 0)
    write_1h = int(write.get("ephemeral_1h_input_tokens") or 0)
    if not write:
        write_5m = int(usage.get("cache_creation_input_tokens") or 0)
    fresh = int(usage.get("input_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    output = int(usage.get("output_tokens") or 0)
    per_input, per_output, per_read = prices
    return Use(
        tokens={"fresh": fresh, "cache_write": write_5m + write_1h, "cache_read": cache_read, "output": output},
        usd={
            "fresh": fresh * per_input / 1e6, "cache_write": (write_5m * 1.25 + write_1h * 2) * per_input / 1e6,
            "cache_read": cache_read * per_read / 1e6, "output": output * per_output / 1e6,
        },
        calls=1,
    )


def mark_miss(use: Use, usage: dict[str, Any], read_price: float, cached: int, paused: bool) -> int:
    """Count a call's cache miss into its use, given the input the call before it had cached; return the call's
    input, which the cache holds after it."""
    fresh = int(usage.get("input_tokens") or 0)
    write = int(usage.get("cache_creation_input_tokens") or 0)
    read = int(usage.get("cache_read_input_tokens") or 0)
    missed = max(0, min(cached, fresh + write + read) - read)
    if missed and missed * 2 >= min(cached, fresh + write + read):
        # The missed tokens cost what this call paid per token it did not read from the cache.
        paid = (use.usd["fresh"] + use.usd["cache_write"]) / (fresh + write)
        use.misses = 1
        use.miss_tokens = missed
        use.miss_usd = missed * max(0.0, paid - read_price / 1e6)
        use.paused_miss_usd = use.miss_usd if paused else 0.0
    return fresh + write + read


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text")
    return ""


def prompt(entry: dict[str, Any]) -> tuple[bool, str, str | None]:
    """Whether a user entry starts a turn, its text without a socket message's wrapper, and the command it typed."""
    text = _text((entry.get("message") or {}).get("content")).strip()
    if entry.get("isMeta"):
        if not text.startswith(PEER_PROMPT):
            return False, text, None
        text = text.removeprefix(PEER_PROMPT).strip()
    else:
        command = COMMAND.search(text[:300])
        if command:
            return True, text, command.group(1)
    if not text or text.startswith(CONTINUES):
        return False, text, None
    return True, text, None


def origin(text: str, script: bool) -> str:
    """What started a turn, from its prompt (see ORIGINS)."""
    if script:
        return "script"
    # Cockpit sends the sweeper its prompts as itself, and as the user when they press Update.
    body = text.partition("\n\n")[2]
    if text.startswith(COCKPIT_ITSELF):
        return "sweep" if body.startswith(SWEEPER_PROMPTS) else "cockpit"
    if text.startswith(SWEEPER_PROMPTS):
        return "sweep"
    agent = AGENT_MESSAGE.match(text)
    if agent:
        return "sweeper_asks" if agent.group(1) in SWEEPER_AGENTS else "session_asks"
    if USER_MESSAGE.match(text):
        return "sweep" if body.startswith(SWEEPER_PROMPTS) else "user_cockpit"
    if text.startswith(SESSION_MESSAGES):
        return "session_asks"
    return "typed"


def kind_of_session(turns: list[Turn]) -> str:
    """The sweeper, or what started the session's first turn whose prompt was read (see KINDS_OF_SESSION)."""
    if any(turn.origin == "sweep" for turn in turns):
        return "sweeper"
    first = next((turn.origin for turn in turns if turn.origin != "unknown"), "unknown")
    return KINDS_OF_SESSION[first]


@dataclass
class Call:
    model: str
    message_id: str
    usage: dict[str, Any]
    stamp: str
    skills: list[str]  # the skills it loaded with the Skill tool
    ends_turn: bool  # whether the reply ended the turn rather than asked for a tool's result


def _call(line: str) -> Call | None:
    """An assistant entry's call; None when it has no usage."""
    head = CALL_HEAD.search(line, 0, 400)
    at = line.find(USAGE_KEY)
    stamp_at = line.rfind(STAMP_KEY)
    stop_at = line.rfind(STOP_KEY, 0, at)
    if head and at > 0 and stamp_at > at and stop_at > 0:
        usage, _ = DECODER.raw_decode(line, at + len('"usage":'))
        stop, _ = DECODER.raw_decode(line, stop_at + len(STOP_KEY))
        stamp = line[stamp_at + len(STAMP_KEY):line.index('"', stamp_at + len(STAMP_KEY))]
        skills = SKILL_CALL.findall(line) if '"name":"Skill"' in line else []
        return Call(head.group(1), head.group(2), usage, stamp, skills, stop != "tool_use")
    entry = json.loads(line)
    message = entry.get("message") or {}
    if not message.get("usage") or not message.get("id"):
        return None
    skills = [
        str((block.get("input") or {}).get("skill") or "")
        for block in message.get("content") or []
        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Skill"
    ]
    return Call(str(message.get("model") or ""), str(message["id"]), message["usage"], str(entry.get("timestamp") or ""),
                [skill for skill in skills if skill], message.get("stop_reason") != "tool_use")


def names_model(name: str, model: str) -> bool:
    """Whether the `model` value of a Claude settings file, a full model id or an alias such as "fable" or
    "opus[1m]", names the model id `model`."""
    name = name.removesuffix("[1m]")
    return model == name or model.startswith(f"claude-{name}-")


class SpendIndex:
    def __init__(self, projects_dir: Path) -> None:
        self.projects_dir = projects_dir
        self._lock = threading.Lock()
        self._files: dict[Path, Transcript] = {}
        # Message id to the output tokens counted for it.
        self._seen: dict[str, int] = {}
        self.unpriced: set[str] = set()
        # Claude settings files whose model could not be read, each logged once.
        self.unreadable: set[Path] = set()
        self.ready_at: str | None = None
        # Grows whenever a refresh changes what was read, so per_session and wrong_models work out their sums once
        # per change and not on every page poll.
        self._version = 0
        self._uses: tuple[int, list[tuple[str, dict[str, Any], list[Transcript]]]] | None = None
        self._model_calls: tuple[int, list[SessionModels]] | None = None

    def refresh(self, now: datetime) -> None:
        """Read what was appended to every transcript written within HORIZON_DAYS."""
        oldest = (now - timedelta(days=HORIZON_DAYS)).timestamp()
        for path in self.projects_dir.glob("**/*.jsonl"):
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime < oldest:
                continue
            known = self._files.get(path)
            if known is not None and stat.st_size == known.offset:
                continue
            try:
                self._read(path, known, stat.st_size)
            except OSError as exc:
                log.error("reading %s for Claude use failed: %s", path, exc)
        self.ready_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _read(self, path: Path, known: Transcript | None, size: int) -> None:
        if known is None:
            parts = path.relative_to(self.projects_dir).parts
            subagent = "subagents" in parts
            known = Transcript(session_id=parts[1] if subagent else path.stem, subagent=subagent)
            # The page reads only the transcripts in _files, so a new one is read without the lock.
            with path.open("rb") as handle:
                self._lines(path, known, handle)
            with self._lock:
                self._files[path] = known
                self._version += 1
            return
        if size < known.offset:
            log.error("%s shrank from %d to %d bytes; Claude use keeps what it read", path, known.offset, size)
            known.offset = size
        # What a known transcript gained is read first and counted under one lock. Taken per line, the lock would go
        # to the page's polls between every two lines and slow a refresh down.
        with path.open("rb") as handle:
            handle.seek(known.offset)
            added = handle.readlines()
        with self._lock:
            try:
                self._lines(path, known, added)
            finally:
                # Lines counted before an unexpected error still change the sums.
                self._version += 1

    def _lines(self, path: Path, known: Transcript, lines: Iterable[bytes]) -> None:
        for raw in lines:
            # A line still being written is read on the next refresh.
            if not raw.endswith(b"\n"):
                break
            try:
                self._line(known, raw.decode("utf-8", errors="replace"))
            except (ValueError, KeyError, TypeError) as exc:
                # Claude Code sometimes writes two entries into one line; that line's call is not counted.
                log.warning("%s at byte %d is not one transcript entry, skipped: %s", path, known.offset, exc)
            known.offset += len(raw)

    def _line(self, known: Transcript, line: str) -> None:
        if '"type":"assistant"' in line:
            if USAGE_KEY in line:
                self._assistant(known, line)
            return
        if COMPACTED in line:
            known.cached = 0
            return
        if '"type":"custom-title"' in line:
            known.title = str(json.loads(line).get("customTitle") or "")
            return
        if '"type":"user"' not in line or '"type":"tool_result"' in line:
            return
        entry = json.loads(line)
        if known.subagent:
            known.started = known.started or str(entry.get("timestamp") or "")
            return
        if entry.get("type") != "user" or entry.get("isSidechain"):
            return
        if not known.cwd and entry.get("cwd"):
            known.cwd = str(entry["cwd"])
        starts, text, command = prompt(entry)
        if command == "model":
            known.picking = True
        stamp = str(entry.get("timestamp") or "")
        if not starts or not stamp:
            if known.waiting and text.startswith(NOTICES):
                known.woken = True
            return
        known.first_prompt = known.first_prompt or text[:200]
        began = origin(text, str(entry.get("entrypoint") or "").startswith("sdk"))
        known.turns.append(Turn(start=stamp, origin=began, prompt=text[:PROMPT_CHARS], skills=[command] if command else []))
        known.waiting = known.woken = False

    def _assistant(self, known: Transcript, line: str) -> None:
        call = _call(line)
        if call is None:
            return
        loaded = known.skills if known.subagent else (known.turns[-1].skills if known.turns else [])
        for skill in call.skills:
            if skill not in loaded:
                loaded.append(skill)
        woken = known.woken
        if not known.subagent:
            known.waiting = call.ends_turn
            known.woken = known.woken and not call.ends_turn
            # Each transcript judges its own calls, so a copy of the session read first hides none of them; a turn
            # a `claude -p` run started names its own model, also in a session resumed that way.
            script = bool(known.turns) and known.turns[-1].origin == "script"
            if not call.model.startswith("<") and not script:
                known.latest = max(known.latest, (call.stamp, call.model))
                if known.picking:
                    known.picked.add(call.model)
                    known.picking = False
                # Every model: for a session the user started on another model, config.MAIN_MODEL is the wrong one.
                calls = known.own_models.setdefault(call.model, ModelCalls(first=call.stamp, last=call.stamp))
                calls.last = max(calls.last, call.stamp)
                # One reply is several entries with the same message id.
                calls.ids.add(call.message_id)
        # Claude Code writes one reply as several entries with the same message id. In a subagent's transcript the
        # output count grows from entry to entry, so a later entry counts the output it adds.
        counted = self._seen.get(call.message_id)
        output = int(call.usage.get("output_tokens") or 0)
        if call.model.startswith("<") or (counted is not None and output <= counted):
            return
        self._seen[call.message_id] = output
        model = priced_as(call.model)
        if model is None:
            # A model that is not Claude's has no Claude price; its calls are no Claude use, so they count nowhere here.
            if "claude" in call.model and call.model not in self.unpriced:
                log.error("no price for Claude model %r; its calls count as $0", call.model)
                self.unpriced.add(call.model)
            return
        prices = call_prices(model, call.usage)
        if counted is None:
            use = call_use(call.usage, prices)
            paused = bool(known.cached_at and call.stamp) and call.stamp >= _minutes_before(known.cached_at, -PAUSE_MINUTES)
            known.cached = mark_miss(use, call.usage, prices[2], known.cached, paused)
            known.cached_at = call.stamp or known.cached_at
        else:
            use = call_use({"output_tokens": output - counted}, prices)
            use.calls = 0
        known.first = known.first or call.stamp
        known.last = max(known.last, call.stamp)
        if call.stamp:
            known.recent = [*_since(known.recent, _minutes_before(call.stamp, RECENT_MINUTES)), (call.stamp, use.total())]
        if known.subagent:
            use.subagent_usd = use.total()
            known.use.add(use, 1)
            add_models(known.models, {model: use})
            return
        if woken:
            use.woken_usd = use.total()
        if not known.turns:
            known.turns.append(Turn(start=call.stamp, origin="unknown"))
        known.turns[-1].use.add(use, 1)
        add_models(known.turns[-1].models, {model: use})

    def _sessions(self) -> dict[str, list[Transcript]]:
        """Each session's transcripts, its own first: a session resumed in a new folder has two."""
        sessions: dict[str, list[Transcript]] = {}
        for transcript in self._files.values():
            sessions.setdefault(transcript.session_id, []).append(transcript)
        for transcripts in sessions.values():
            transcripts.sort(key=lambda transcript: (transcript.subagent, not transcript.title))
        return sessions

    @staticmethod
    def _turns(transcripts: list[Transcript]) -> list[Turn]:
        """A session's turns, each with the use and skills of the subagents that started in it."""
        own = sorted((turn for transcript in transcripts if not transcript.subagent for turn in transcript.turns),
                     key=lambda turn: turn.start)
        turns = [Turn(start=turn.start, origin=turn.origin, prompt=turn.prompt, skills=list(turn.skills)) for turn in own]
        for turn, source in zip(turns, own):
            turn.use.add(source.use, 1)
            add_models(turn.models, source.models)
        starts = [turn.start for turn in turns]
        for sub in sorted((t for t in transcripts if t.subagent and t.use.calls), key=lambda t: t.started or t.first):
            started = sub.started or sub.first
            at = bisect.bisect_right(starts, started) - 1
            if at < 0:
                # A subagent whose session's prompts were not read (written before HORIZON_DAYS) starts a turn.
                turns.insert(0, Turn(start=started, origin="unknown"))
                starts.insert(0, started)
                at = 0
            turns[at].use.add(sub.use, 1)
            add_models(turns[at].models, sub.models)
            turns[at].skills.extend(skill for skill in sub.skills if skill not in turns[at].skills)
        return turns

    def summary(self, since: datetime) -> dict[str, Any]:
        """Use of the turns started since `since`: in total, per session, per skill, per origin, per kind of session,
        per model, the most expensive turns and the turns with the costliest cache misses."""
        cutoff = since.strftime("%Y-%m-%dT%H:%M:%S")
        total = Use()
        per_skill, per_origin, per_kind, per_model = Group(), Group(), Group(), Group()
        rows, turns = [], []
        with self._lock:
            for session_id, transcripts in self._sessions().items():
                session_use, session_turns = Use(), []
                every_turn = list(self._turns(transcripts))
                for turn in every_turn:
                    if turn.start < cutoff or not turn.use.calls:
                        continue
                    session_use.add(turn.use, 1)
                    session_turns.append(turn)
                if not session_turns:
                    continue
                # What the session is comes from all its turns, also those before the period or with no call (a
                # sweep request the usage limit stopped), so it does not change with the period.
                kind = kind_of_session(every_turn)
                for turn in session_turns:
                    for skill in turn.skills or [""]:
                        per_skill.add(skill, session_id, turn.use, 1 / max(1, len(turn.skills)))
                    per_origin.add(turn.origin, session_id, turn.use, 1)
                    per_kind.add(kind, session_id, turn.use, 1)
                    for model, use in turn.models.items():
                        per_model.add(model, session_id, use, 1)
                    turns.append((session_id, turn))
                total.add(session_use, 1)
                own = transcripts[0]
                rows.append({
                    "session_id": session_id, "title": own.title, "first_prompt": own.first_prompt, "cwd": own.cwd,
                    "kind": kind, "first": min(transcript.first for transcript in transcripts if transcript.first),
                    "last": max(transcript.last for transcript in transcripts),
                    "turns": len(session_turns), "use": session_use.as_dict(),
                })
        rows.sort(key=lambda row: -row["use"]["usd"])
        turns.sort(key=lambda pair: -pair[1].use.total())
        missed = sorted((pair for pair in turns if pair[1].use.misses), key=lambda pair: -pair[1].use.miss_usd)

        def turn_row(session_id: str, turn: Turn) -> dict[str, Any]:
            return {"session_id": session_id, "start": turn.start, "origin": turn.origin, "prompt": turn.prompt,
                    "skills": turn.skills, "use": turn.use.as_dict()}

        return {
            "ready_at": self.ready_at, "since": cutoff + "Z", "total": total.as_dict(), "sessions": rows,
            "skills": per_skill.rows("skill"), "origins": per_origin.rows("origin"),
            "session_kinds": per_kind.rows("kind"),
            "models": [
                row | {"prices": dict(zip(("input", "output", "cache_read"), PRICES[row["model"]], strict=True))}
                for row in per_model.rows("model")
            ],
            "top_turns": [turn_row(session_id, turn) for session_id, turn in turns[:TOP_TURNS]],
            "miss_turns": [turn_row(session_id, turn) for session_id, turn in missed[:MISS_TURNS]],
            "unpriced_models": sorted(self.unpriced), "week_list_usd": WEEK_LIST_USD,
            "plan_usd_per_percent": PLAN_USD_PER_PERCENT,
        }

    def hours(self, since: datetime) -> list[dict[str, Any]]:
        """Use per hour of the turns started since `since`, counted as summary() counts them."""
        cutoff = since.strftime("%Y-%m-%dT%H:%M:%S")
        per_hour: dict[str, Use] = {}
        with self._lock:
            for transcripts in self._sessions().values():
                for turn in self._turns(transcripts):
                    if turn.start >= cutoff and turn.use.calls:
                        per_hour.setdefault(turn.start[:13] + ":00:00Z", Use()).add(turn.use, 1)
        return [{"hour": hour, "use": per_hour[hour].as_dict()} for hour in sorted(per_hour)]

    def wrong_models(self, now: datetime, started: dict[str, str]) -> list[dict[str, Any]] | None:
        """None until the first refresh has read every transcript, as a part-read index misses sessions still on the
        wrong model. Then each session whose own conversation answered on a model other than its intended one in the last
        WRONG_MODEL_HOURS, one row per model, the latest first (SPEC SESSION-20), with `intended` and `back`: whether the session's
        latest own call was on an intended model again. For a session Cockpit started, the intended model is the one
        it was started on (`started`, by session id: the user's pick or config.MAIN_MODEL, SPEC SESSION-22); a
        session another session, a script or a terminal started was started on its model on purpose, so for it the
        intended model is the one its first own call ran on. A model someone chose for the session counts as intended
        too: one picked with /model in it, or one named by the `model` setting of its folder's Claude settings, as
        Claude Code moves a running session to that model. Subagents and the turns of `claude -p` runs name their own
        model, so their calls are left out. A session resumed in another folder repeats its replies there; each
        reply counts once."""
        if self.ready_at is None:
            return None
        since = (now - timedelta(hours=WRONG_MODEL_HOURS)).strftime("%Y-%m-%dT%H:%M:%S")
        rows = []
        # The settings files are read outside the lock, once per folder.
        settings_models: dict[str, list[str]] = {}
        for session in self._session_models():
            per_model = session.per_model
            intended = started.get(session.session_id) or min(per_model, key=lambda model: per_model[model].first)
            if session.cwd not in settings_models:
                settings_models[session.cwd] = self._settings_models(session.cwd)
            named = settings_models[session.cwd]
            chosen = {intended, *session.picked,
                      *(model for model in per_model if any(names_model(name, model) for name in named))}
            recent = {model: wrong for model, wrong in per_model.items() if wrong.last >= since and model not in chosen}
            if not recent:
                continue
            back = session.latest in chosen
            rows.extend(
                {"session_id": session.session_id, "title": session.title, "first_prompt": session.first_prompt,
                 "model": model, "intended": intended, "first": wrong.first, "last": wrong.last,
                 "calls": len(wrong.ids), "back": back}
                for model, wrong in recent.items()
            )
        rows.sort(key=lambda row: row["last"], reverse=True)
        return rows

    def _session_models(self) -> list[SessionModels]:
        """Each session's own calls by model over all its transcripts, worked out once per change to the index."""
        with self._lock:
            if self._model_calls is not None and self._model_calls[0] == self._version:
                return self._model_calls[1]
            sessions = []
            for session_id, transcripts in self._sessions().items():
                per_model: dict[str, ModelCalls] = {}
                for transcript in transcripts:
                    for model, wrong in transcript.own_models.items():
                        into = per_model.setdefault(model, ModelCalls(first=wrong.first, last=wrong.last))
                        into.first, into.last = min(into.first, wrong.first), max(into.last, wrong.last)
                        into.ids |= wrong.ids
                if not per_model:
                    continue
                own = transcripts[0]
                sessions.append(SessionModels(
                    session_id=session_id, title=own.title, first_prompt=own.first_prompt, cwd=own.cwd,
                    per_model=per_model, picked={model for transcript in transcripts for model in transcript.picked},
                    latest=max(transcript.latest for transcript in transcripts)[1],
                ))
            self._model_calls = (self._version, sessions)
            return sessions

    def _settings_models(self, cwd: str) -> list[str]:
        """The `model` values in the Claude settings of the folder `cwd` (settings.json and settings.local.json)."""
        names = []
        for path in (Path(cwd) / ".claude" / "settings.json", Path(cwd) / ".claude" / "settings.local.json"):
            if not cwd or not path.is_file():
                continue
            try:
                settings = json.loads(path.read_text())
            except (OSError, ValueError) as error:
                if path not in self.unreadable:
                    log.error("cannot read the model from Claude settings %s: %s", path, error)
                    self.unreadable.add(path)
                continue
            if isinstance(settings, dict) and isinstance(settings.get("model"), str):
                names.append(settings["model"])
        return names

    def latest_model(self, session_id: str) -> str | None:
        """The model of the session's latest own call (not a subagent's, not in a `claude -p` turn), or None before
        its first one."""
        with self._lock:
            latest = max((transcript.latest for transcript in self._sessions().get(session_id, [])), default=("", ""))
        return latest[1] or None

    def per_session(self, now: datetime) -> dict[str, dict[str, Any]]:
        """Every session's use over all its transcripts hold, for the Sessions panel, with `recent_usd`: what its
        calls (its subagents' too) cost in the last RECENT_MINUTES."""
        since = (now - timedelta(minutes=RECENT_MINUTES)).strftime("%Y-%m-%dT%H:%M:%S")
        with self._lock:
            # The sums change only with the index; the recent share changes with `now`.
            if self._uses is None or self._uses[0] != self._version:
                uses = []
                for session_id, transcripts in self._sessions().items():
                    use = Use()
                    for transcript in transcripts:
                        use.add(transcript.use, 1)
                        for turn in transcript.turns:
                            use.add(turn.use, 1)
                    if use.calls:
                        uses.append((session_id, use.as_dict(), transcripts))
                self._uses = (self._version, uses)
            return {
                session_id: use | {"recent_usd": round(
                    sum(usd for transcript in transcripts for _, usd in _since(transcript.recent, since)), 2,
                )}
                for session_id, use, transcripts in self._uses[1]
            }


def _minutes_before(stamp: str, minutes: int) -> str:
    """The time `minutes` before a transcript's timestamp, in the timestamps' own form for comparing as text."""
    at = datetime.fromisoformat(stamp) - timedelta(minutes=minutes)
    return at.strftime("%Y-%m-%dT%H:%M:%S")


def _since(calls: list[tuple[str, float]], since: str) -> list[tuple[str, float]]:
    return [(stamp, usd) for stamp, usd in calls if stamp >= since]


def refresh_forever(index: SpendIndex, codex_index: codex.CodexIndex) -> None:
    while True:
        started = time.monotonic()
        try:
            index.refresh(datetime.now(UTC))
        except Exception:
            log.exception("counting Claude use from the transcripts failed")
        try:
            codex_index.refresh(datetime.now(UTC))
        except Exception:
            log.exception("counting Codex use from its session files failed")
        log.info("Claude and Codex use counted in %.1f s", time.monotonic() - started)
        time.sleep(REFRESH_SECONDS)
