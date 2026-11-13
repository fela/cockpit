"""How much processor time and memory each task, and each part of it, uses over time (SPEC TRACK-16).

Cockpit's server samples every program on the host every SAMPLE_SECONDS and credits what it used to a task and a
part. The task is the Claude session the program works for; the part is the kind of program: the agent itself, a
test run, a headless browser, a shell command (PARTS). Ten-minute totals go to `resource_use`, the host's own
readings to `resource_host`. Those rows stay KEEP_DAYS days; older ones fold into daily totals in `resource_days`
and `resource_host_days`, kept for good, with days in the user's timezone. The Server page (/server) charts them.

How a program finds its session, in order:
1. A Claude agent: the session Cockpit's session listing names for its process; else a session id on its command
   line (`--resume`, `--session-id`); else an open file inside a session's own directory.
2. Any other program: the session of the program that started it, up the chain; else CLAUDE_CODE_SESSION_ID in
   its environment. Claude's terminal host takes its agent's session.
3. A program with none, such as Cockpit's server or Codex's app server (which runs every Codex thread in one
   program, so Codex threads cannot be told apart), is recorded with session_id ''.

Processor: each program's own time (utime + stime in /proc/PID/stat), in core-seconds. A program that starts and
ends between two samples is counted through its parent's child-time counters (cutime + cstime), less what the
sampler already counted for the children it saw end, as the parent's session's part "Short commands". A parent can
collect a child's exit while a sample is being read: its counters, read first, miss the child the sample then finds
ended. That shortfall is taken off later short commands, once the counters include the child.

Memory: each program's proportional share (Pss: a page shared by several programs is split between them) and its
share of swapped-out pages (SwapPss), from smaps_rollup once every MEMORY_EVERY samples. Every sample adds each
running program's latest reading times the seconds since the last sample, as MB-seconds, so an average over any
window is the sum over the seconds sampled in it (`resource_host.sampled_s`). A program not read yet counts by its
resident size, as do programs of other users, which keep smaps_rollup closed. A program that runs another program in
its place (same process, new name) is classified again. The peaks are the most memory, counted the same way, held at
one sample: by the row's session across its parts (`session_peak_mb`), and by the row's part across all sessions
(`part_peak_mb`).

Idle: the same figures for the time the session was not working (Session.working_now), which shows what sessions
cost while they wait.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from cockpit import db, parts

log = logging.getLogger(__name__)

SAMPLE_SECONDS = 5
MEMORY_EVERY = 12  # samples: smaps_rollup once a minute, as it walks every page table
FLUSH_SECONDS = 60
BUCKET_SECONDS = 600
KEEP_DAYS = 30
FOLD_SECONDS = 3600
# A gap longer than this between two samples, such as a stalled server, is not counted as sampled time.
LONGEST_GAP = 30
# Programs that ended this long ago are forgotten; until then their children can still find their session.
FORGET_SECONDS = 600
NOW_KEY = "resources_now"
NOW_TASKS = 10  # tasks kept for "Right now"

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
CMD_SESSION = re.compile(r"--(?:session-id|resume)[ =](?:\S*/)?(" + UUID + ")")
FD_SESSION = (
    re.compile(r"/tmp/claude-\d+/[^/]+/(" + UUID + ")/"),
    re.compile(r"\.claude/projects/[^/]+/(" + UUID + r")\.jsonl"),
)
ENV_SESSION = re.compile(rb"(?:^|\0)CLAUDE_CODE_SESSION_ID=(" + UUID.encode() + rb")")
# Codex's own program, as the word of its command line that names it: its `codex` command or `codex.js` script, or a
# binary from its npm packages (`@openai/codex-linux-x64/.../codex-code-mode-host`).
CODEX_PROGRAM = re.compile(r"(?:^|/)codex(?:\.js)?$|/@openai/codex\b")
# Interpreters whose first argument is the program they run (`node /usr/lib/codex app-server`).
INTERPRETERS = frozenset(("node", "bun"))
# Cockpit's worker that drives Codex, which Python runs as a module.
CODEX_WORKER = "runpy.run_module('cockpit.codex_worker'"
SHELL_COMMANDS = frozenset(
    ("bash", "sh", "timeout", "sleep", "tee", "head", "tail", "grep", "xargs", "find", "awk", "sed", "cat", "ps", "rg")
)

AGENT = "Claude agent"
SPARE = "Spare agent"
TERMINAL = "Claude terminal host"
MODEL_CALLS = "Cockpit model calls"
SERVER = "Cockpit server"
SHORT = "Short commands"
KERNEL = "Kernel"
SYSTEM = "System services"
# Each part belongs to one family; the Server page's charts stack the families, in this order and colour.
FAMILIES: dict[str, tuple[str, ...]] = {
    "Claude agents": (AGENT, SPARE, TERMINAL, "Claude background daemon"),
    "Codex agents": ("Codex agent",),
    "Tests": ("Python tests", "Browser test scripts"),
    "Browsers": ("Headless browser",),
    "Commands": ("Shell commands", SHORT, "git and gh", "Other Python"),
    "Cockpit": (SERVER, MODEL_CALLS),
    "Other": ("T3 server", *parts.process_labels().values(), KERNEL, SYSTEM, "Other"),
}
PARTS = tuple(part for parts in FAMILIES.values() for part in parts)
FAMILY_OF = {part: family for family, parts in FAMILIES.items() for part in parts}


def kind_of(comm: str, cmd: str) -> str:
    """The part a program is, from its name in /proc/PID/stat and its command line."""
    if "bg-pty-host" in cmd:
        return TERMINAL
    if "claude daemon" in cmd:
        return "Claude background daemon"
    if "--bg-spare" in cmd or re.search(r"/claude/versions/|(^|/)claude( |$)", cmd) or comm == "claude":
        return AGENT
    if _codex(comm, cmd):
        return "Codex agent"
    if "cockpit-server" in cmd:
        return SERVER
    if "pytest" in cmd or comm == "pytest":
        return "Python tests"
    if "chrome" in comm or "chrome-headless" in cmd or "chromium" in cmd:
        return "Headless browser"
    if "t3 serve" in cmd:
        return "T3 server"
    for pattern, label in parts.process_labels().items():
        if re.match(pattern, cmd):
            return label
    if comm in ("bun", "node", "MainThread") or "tests/browser" in cmd or "node" in comm:
        return "Browser test scripts"
    if comm in ("git", "gh"):
        return "git and gh"
    if comm in SHELL_COMMANDS:
        return "Shell commands"
    if "python" in comm or comm == "uv":
        return "Other Python"
    return "Other"


def _codex(comm: str, cmd: str) -> bool:
    """Whether the program is Codex itself, from its name or the program its command line runs; an argument that only
    mentions Codex (`pytest tests/test_codex_agent.py`) is none."""
    words = cmd.split()
    programs = words[:2] if words and os.path.basename(words[0]) in INTERPRETERS else words[:1]
    return comm == "codex" or any(CODEX_PROGRAM.search(word) for word in programs) or CODEX_WORKER in cmd


@dataclass
class Stat:
    comm: str
    ppid: int
    ticks: int  # utime + stime
    child_ticks: int  # cutime + cstime: the time of children it waited for
    start: int  # clock ticks after boot


def parse_stat(text: str) -> Stat:
    comm = text[text.index("(") + 1 : text.rindex(")")]
    rest = text[text.rindex(")") + 2 :].split()
    return Stat(comm, int(rest[1]), int(rest[11]) + int(rest[12]), int(rest[13]) + int(rest[14]), int(rest[19]))


def parse_rollup(text: str) -> tuple[int, int]:
    """Pss and SwapPss in kB from /proc/PID/smaps_rollup."""
    pss = swap = 0
    for line in text.splitlines():
        name, _, value = line.partition(":")
        if name == "Pss":
            pss = int(value.split()[0])
        elif name == "SwapPss":
            swap = int(value.split()[0])
    return pss, swap


@dataclass
class Program:
    pid: int
    start: int
    ppid: int
    part: str
    cmd: str
    parent: tuple[int, int] | None  # the program that started it, as (pid, start)
    comm: str  # its name in /proc/PID/stat; a change means it ran another program, so it is classified again
    own: str  # the session found from the program itself, or ''
    # The session of the program that started it, kept so it outlives that program's record; else
    # CLAUDE_CODE_SESSION_ID in its environment; else ''.
    env: str
    ticks: int
    child_ticks: int
    owned: bool  # run by the user
    mine: bool  # the user's own and open to it, so its environ and smaps_rollup can be read
    rss_kb: int = 0
    pss_kb: int | None = None  # the latest smaps_rollup reading; None before the first
    swap_kb: int = 0
    owed_ticks: int = 0  # child time counted as ended but not yet in its counters (see the module doc)
    gone_at: float | None = None
    looked_at: float = 0.0  # when a Claude agent's open files were last searched for its session


@dataclass
class Totals:
    cpu_s: float = 0.0
    idle_cpu_s: float = 0.0
    mem_mb_s: float = 0.0
    idle_mem_mb_s: float = 0.0
    swap_mb_s: float = 0.0
    session_peak_mb: float = 0.0
    part_peak_mb: float = 0.0
    programs: int = 0

    def add(self, other: Totals) -> None:
        """Another reading of the same row: sums add, peaks keep the larger."""
        for name in ("cpu_s", "idle_cpu_s", "mem_mb_s", "idle_mem_mb_s", "swap_mb_s"):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.session_peak_mb = max(self.session_peak_mb, other.session_peak_mb)
        self.part_peak_mb = max(self.part_peak_mb, other.part_peak_mb)
        self.programs = max(self.programs, other.programs)


@dataclass
class Host:
    sampled_s: float = 0.0
    busy_s: float = 0.0  # core-seconds the processors ran anything
    steal_s: float = 0.0  # core-seconds the hypervisor gave to other machines
    load_s: float = 0.0  # 1-minute load average x seconds
    avail_mb_s: float = 0.0  # memory new work can still get (MemAvailable) x seconds
    swap_mb_s: float = 0.0  # swap in use x seconds
    cpu_wait_s: float = 0.0  # seconds some program waited for a processor (/proc/pressure/cpu "some")
    mem_wait_s: float = 0.0  # seconds some program waited for memory (/proc/pressure/memory "some")

    def add(self, other: Host) -> None:
        for name in Host.__dataclass_fields__:
            setattr(self, name, getattr(self, name) + getattr(other, name))


@dataclass
class HostReading:
    """The host's counters at one sample."""

    cpu: list[int]  # /proc/stat's cpu line: user nice system idle iowait irq softirq steal
    run_ns: int
    waits: tuple[int, int]  # microseconds some program waited for a processor, and for memory
    avail_kb: int
    swap_kb: int
    load: float


@dataclass
class Taken:
    """What the sampler gathered since the last take: rows to add, and the minute's busiest sessions."""

    use: dict[tuple[int, str, str], Totals]
    host: dict[int, Host]
    now: list[dict[str, Any]]


@dataclass
class Sampler:
    """Reads a /proc tree; `proc` is a fake directory in tests."""

    proc: Path
    uid: int
    tick_hz: int
    page_kb: int
    programs: dict[tuple[int, int], Program] = field(default_factory=dict)
    use: dict[tuple[int, str, str], Totals] = field(default_factory=dict)
    host: dict[int, Host] = field(default_factory=dict)
    # Since the last take, per (session, part): core-seconds; and the memory held at the latest sample.
    minute_cpu: dict[tuple[str, str], float] = field(default_factory=dict)
    minute_mem: dict[tuple[str, str], float] = field(default_factory=dict)
    minute_s: float = 0.0
    samples: int = 0
    last_at: float | None = None
    last_host: HostReading | None = None
    failures: set[tuple[str, int]] = field(default_factory=set)  # (file name, errno) already logged

    def _read(self, *parts: str | int) -> bytes | None:
        """A program's file, or None when the program has ended. Any other failure is logged, once per file name
        and error, and also gives None, so one unreadable program cannot stop the record of the others."""
        path = self.proc.joinpath(*map(str, parts))
        try:
            return path.read_bytes()
        except (FileNotFoundError, ProcessLookupError):
            return None
        except OSError as error:
            self._failed(path.name, error)
            return None

    def _failed(self, name: str, error: OSError) -> None:
        if (name, error.errno or 0) not in self.failures:
            self.failures.add((name, error.errno or 0))
            log.error("reading /proc/*/%s failed, so that reading is missing for such programs: %s", name, error)

    def sample(self, wall: float, mono: float, sessions: dict[int, str], working: dict[str, bool]) -> None:
        """One sample. `sessions` maps a running agent's process to its session, from Cockpit's session listing;
        `working` says for each session whether it is working now."""
        reading = self._read_host()  # first: when the host cannot be read, the sample changes nothing
        memory = self.samples % MEMORY_EVERY == 0
        self.samples += 1
        dt = mono - self.last_at if self.last_at is not None else 0.0
        # Use across a longer gap, or before the first sample, is not credited: the time was not sampled.
        counted = 0 < dt <= LONGEST_GAP
        if not counted:
            dt = 0.0
        self.last_at = mono
        stats = {}
        for name in os.listdir(self.proc):
            if name.isdigit():
                raw = self._read(name, "stat")
                if raw is not None:
                    stats[int(name)] = parse_stat(raw.decode(errors="replace"))
        seen_now = {(pid, stat.start) for pid, stat in stats.items()}
        ended = [prog for key, prog in self.programs.items() if key not in seen_now and prog.gone_at is None]
        for prog in ended:
            prog.gone_at = mono
        for key in [key for key, prog in self.programs.items() if prog.gone_at and mono - prog.gone_at > FORGET_SECONDS]:
            del self.programs[key]
        # A program that ended adds its whole use to its parent's child counters, and that use was credited
        # up to the last sample. When its parent ended too, the use lands on the nearest ancestor still running.
        ended_by_pid = {prog.pid: prog for prog in ended}
        ended_ticks: dict[int, int] = {}
        for prog in ended:
            pid = prog.ppid
            while pid not in stats and pid in ended_by_pid and ended_by_pid[pid] is not prog:
                pid = ended_by_pid[pid].ppid
            # Child time it still owed (counted already, not yet in its counters) reaches the ancestor's counters too.
            ended_ticks[pid] = ended_ticks.get(pid, 0) + prog.ticks + prog.child_ticks - prog.owed_ticks

        bucket = int(wall) // BUCKET_SECONDS * BUCKET_SECONDS
        held: dict[tuple[str, str], list[float]] = {}  # (session, part) -> [MB, programs] at this sample
        for pid in sorted(stats, key=lambda pid: stats[pid].start):  # parents before their children
            stat = stats[pid]
            key = (pid, stat.start)
            prog = self.programs.get(key)
            new = prog is None
            if prog is None:
                prog = self.programs[key] = self._program(pid, stat, stats, mono)
            elif stat.comm != prog.comm or (memory and prog.mine and self._cmdline(pid)[:300] not in (prog.cmd, "")):
                # A new name shows at once; a program run in place under the same name, such as python running
                # python, shows by its command line, read once a minute.
                self._reclassify(prog, stat)
            used = stat.ticks - (0 if new else prog.ticks)
            unseen = stat.child_ticks - (0 if new else prog.child_ticks) - ended_ticks.get(pid, 0)
            # A child counted as ended can be missing from its parent's counters until the next sample (the
            # module doc); the shortfall is carried and taken off later short commands, also across a sample that
            # credits nothing.
            owed = unseen + prog.owed_ticks
            short, prog.owed_ticks = max(owed, 0) if counted else 0, min(owed, 0)
            prog.ticks, prog.child_ticks, prog.ppid = stat.ticks, stat.child_ticks, stat.ppid
            if prog.part == AGENT:
                self._match_agent(prog, sessions, mono)
            raw = self._read(pid, "statm")
            if raw is not None:
                prog.rss_kb = int(raw.split()[1]) * self.page_kb
            if memory and prog.mine:
                self._read_memory(prog)
            session = self.session_of(prog)
            part = SPARE if prog.part == AGENT and not session and "--bg-spare" in prog.cmd else prog.part
            idle = bool(session) and not working.get(session, False)
            mb = (prog.rss_kb if prog.pss_kb is None else prog.pss_kb) / 1024
            if counted:
                self._add(bucket, session, part, max(used, 0) / self.tick_hz, idle)
                if short:
                    self._add(bucket, session, SHORT, short / self.tick_hz, idle)
                totals = self.use.setdefault((bucket, session, part), Totals())
                totals.mem_mb_s += mb * dt
                totals.swap_mb_s += prog.swap_kb / 1024 * dt
                if idle:
                    totals.idle_mem_mb_s += mb * dt
            if part != KERNEL:
                hold = held.setdefault((session, part), [0.0, 0])
                hold[0] += mb
                hold[1] += 1
        by_session: dict[str, float] = {}
        by_part: dict[str, float] = {}
        for (session, part), (mb, _count) in held.items():
            by_session[session] = by_session.get(session, 0.0) + mb
            by_part[part] = by_part.get(part, 0.0) + mb
        for (session, part), (_mb, count) in held.items():
            totals = self.use.setdefault((bucket, session, part), Totals())
            totals.session_peak_mb = max(totals.session_peak_mb, by_session[session])
            totals.part_peak_mb = max(totals.part_peak_mb, by_part[part])
            totals.programs = max(totals.programs, int(count))
        self.minute_mem = {key: mb for key, (mb, _count) in held.items()}
        self._add_host(bucket, dt, reading)
        self.minute_s += dt

    def _program(self, pid: int, stat: Stat, stats: dict[int, Stat], mono: float) -> Program:
        parent_stat = stats.get(stat.ppid)
        parent_key = (stat.ppid, parent_stat.start) if parent_stat else None
        parent = self.programs.get(parent_key) if parent_key else None
        try:
            mine = os.stat(self.proc / str(pid)).st_uid == self.uid
        except (FileNotFoundError, ProcessLookupError):
            mine = False
        except OSError as error:
            self._failed("(directory)", error)
            mine = False
        prog = Program(
            pid=pid, start=stat.start, ppid=stat.ppid, part="", cmd="", parent=parent_key, comm=stat.comm, own="",
            env=self.session_of(parent) if parent else "", ticks=stat.ticks, child_ticks=stat.child_ticks, owned=mine,
            mine=mine, looked_at=mono - FLUSH_SECONDS,
        )
        environ = self._open(prog)
        self._classify(prog, parent)
        if not prog.env and prog.mine and prog.part not in (AGENT, MODEL_CALLS, TERMINAL):
            found = ENV_SESSION.search(environ or b"")
            prog.env = found.group(1).decode() if found else ""
        return prog

    def _classify(self, prog: Program, parent: Program | None) -> None:
        """Sets the program's part and command line, and the session its command line names."""
        cmd = self._cmdline(prog.pid)
        if not cmd:
            part = KERNEL
        elif not prog.mine:
            part = SYSTEM
        else:
            part = kind_of(prog.comm, cmd)
        if part == AGENT and parent is not None and parent.part == SERVER:
            part = MODEL_CALLS
        found = CMD_SESSION.search(cmd) if part == AGENT else None
        prog.part, prog.cmd = part, cmd[:300]
        if found:
            prog.own = found.group(1)

    def _cmdline(self, pid: int) -> str:
        return (self._read(pid, "cmdline") or b"").replace(b"\0", b" ").decode(errors="replace").strip()

    def _open(self, prog: Program) -> bytes | None:
        """Settles whether the user's program is open to the user, before it is classified: its environ, or None."""
        prog.mine = prog.owned
        return self._private(prog, "environ") if prog.owned else None

    def _reclassify(self, prog: Program, stat: Stat) -> None:
        """The program ran another in its place (same pid and start), such as a shell that ran the tests."""
        prog.comm = stat.comm
        self._open(prog)  # the new program may be open where the old one was shut, or the other way round
        self._classify(prog, self.programs.get(prog.parent) if prog.parent else None)
        prog.pss_kb, prog.swap_kb = None, 0  # counted by its resident size until the next memory sample

    def _match_agent(self, prog: Program, sessions: dict[int, str], mono: float) -> None:
        listed = sessions.get(prog.pid)
        if listed:
            prog.own = listed
        elif not prog.own and prog.mine and mono - prog.looked_at >= FLUSH_SECONDS:
            prog.looked_at = mono
            prog.own = self._fd_session(prog)
        if prog.own and prog.parent:
            parent = self.programs.get(prog.parent)
            if parent is not None and parent.part == TERMINAL:
                parent.own = prog.own

    def _fd_session(self, prog: Program) -> str:
        fds = self.proc / str(prog.pid) / "fd"
        try:
            names = os.listdir(fds)
        except (FileNotFoundError, ProcessLookupError):
            return ""
        except PermissionError:
            self._shut(prog)
            return ""
        except OSError as error:
            self._failed("fd", error)
            return ""
        for name in names:
            try:
                target = os.readlink(fds / name)
            except (FileNotFoundError, ProcessLookupError):
                continue
            except OSError as error:
                self._failed("fd/*", error)
                continue
            for pattern in FD_SESSION:
                found = pattern.search(target)
                if found:
                    return found.group(1)
        return ""

    def _private(self, prog: Program, name: str) -> bytes | None:
        """A file only the program's owner may read, or None. A program can shut out its owner too (ssh-agent does,
        to keep its keys from the user's other programs); it then counts as not the user's own: a system program,
        measured by its resident size."""
        try:
            return (self.proc / str(prog.pid) / name).read_bytes()
        except (FileNotFoundError, ProcessLookupError):
            return None
        except PermissionError:
            self._shut(prog)
            return None
        except OSError as error:
            self._failed(name, error)
            return None

    @staticmethod
    def _shut(prog: Program) -> None:
        """The program refused its owner: from now on it is measured by its resident size alone."""
        prog.mine, prog.pss_kb, prog.swap_kb = False, None, 0

    def _read_memory(self, prog: Program) -> None:
        raw = self._private(prog, "smaps_rollup")
        # Without a reading the program counts by its resident size, so an old reading never stands in for it.
        prog.pss_kb, prog.swap_kb = parse_rollup(raw.decode(errors="replace")) if raw is not None else (None, 0)

    def session_of(self, prog: Program) -> str:
        """The program's session, or the nearest one up the chain of programs that started it."""
        at: Program | None = prog
        for _ in range(64):
            if at is None:
                return ""
            if at.own or at.env:
                return at.own or at.env
            at = self.programs.get(at.parent) if at.parent else None
        return ""

    def _add(self, bucket: int, session: str, part: str, cpu: float, idle: bool) -> None:
        totals = self.use.setdefault((bucket, session, part), Totals())
        totals.cpu_s += cpu
        if idle:
            totals.idle_cpu_s += cpu
        self.minute_cpu[(session, part)] = self.minute_cpu.get((session, part), 0.0) + cpu

    def _read_host(self) -> HostReading:
        """The host's counters. Every file is required, except the pressure files on a kernel without them."""
        cpu = [int(value) for value in (self.proc / "stat").read_bytes().split(b"\n", 1)[0].split()[1:]]
        meminfo = _meminfo((self.proc / "meminfo").read_bytes())
        return HostReading(
            cpu=cpu, run_ns=_run_ns(self.proc / "schedstat"),
            waits=(self._wait_us("cpu"), self._wait_us("memory")),
            avail_kb=meminfo["MemAvailable"], swap_kb=meminfo["SwapTotal"] - meminfo["SwapFree"],
            load=float((self.proc / "loadavg").read_bytes().split()[0]),
        )

    def _wait_us(self, name: str) -> int:
        """The `some` line's total, in microseconds, from a /proc/pressure file; 0 where the kernel has none."""
        try:
            raw = (self.proc / "pressure" / name).read_bytes()
        except FileNotFoundError:
            return 0
        found = re.search(rb"^some .*total=(\d+)", raw, re.MULTILINE)
        if found is None:
            raise RuntimeError(f"/proc/pressure/{name} has no 'some' line: {raw[:200]!r}")
        return int(found.group(1))

    def _add_host(self, bucket: int, dt: float, now: HostReading) -> None:
        before, self.last_host = self.last_host, now
        if not dt or before is None:
            return
        host = self.host.setdefault(bucket, Host())
        host.sampled_s += dt
        # Busy time comes from the scheduler's own clock, the one each program's time is measured on.
        # /proc/stat counts in timer ticks, a coarser clock that can read lower.
        host.busy_s += (now.run_ns - before.run_ns) / 1e9
        # /proc/stat: user nice system idle iowait irq softirq steal
        host.steal_s += (now.cpu[7] - before.cpu[7] if len(now.cpu) > 7 else 0) / self.tick_hz
        host.load_s += now.load * dt
        host.avail_mb_s += now.avail_kb / 1024 * dt
        host.swap_mb_s += now.swap_kb / 1024 * dt
        host.cpu_wait_s += (now.waits[0] - before.waits[0]) / 1e6
        host.mem_wait_s += (now.waits[1] - before.waits[1]) / 1e6

    def take(self) -> Taken:
        """The rows gathered since the last take, and each session's use in that time, per part."""
        per: dict[str, dict[str, Any]] = {}
        for (session, part), cpu in self.minute_cpu.items():
            row = per.setdefault(session, {"session_id": session, "cores": 0.0, "mem_mb": 0.0, "parts": {}})
            cores = cpu / self.minute_s if self.minute_s else 0.0
            row["cores"] += cores
            row["parts"][part] = row["parts"].get(part, 0.0) + cores
        for (session, _part), mb in self.minute_mem.items():
            per.setdefault(session, {"session_id": session, "cores": 0.0, "mem_mb": 0.0, "parts": {}})["mem_mb"] += mb
        now = [
            {"session_id": row["session_id"], "cores": round(row["cores"], 3), "mem_mb": round(row["mem_mb"], 1),
             "parts": {part: round(cores, 3) for part, cores in row["parts"].items() if cores}}
            for row in per.values()
        ]
        taken = Taken(self.use, self.host, now)
        self.use, self.host, self.minute_cpu, self.minute_s = {}, {}, {}, 0.0
        return taken

    def give_back(self, taken: Taken) -> None:
        """Keeps a take whose write failed, so the next write adds it."""
        for key, totals in taken.use.items():
            self.use.setdefault(key, Totals()).add(totals)
        for bucket, host in taken.host.items():
            self.host.setdefault(bucket, Host()).add(host)


def _run_ns(path: Path) -> int:
    """Nanoseconds all processors spent running programs, from /proc/schedstat's per-processor lines."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError as error:
        raise RuntimeError("/proc/schedstat is missing; the machine's busy time cannot be measured") from error
    return sum(int(line.split()[7]) for line in raw.decode().splitlines() if line.startswith("cpu"))


def _meminfo(raw: bytes) -> dict[str, int]:
    out = {}
    for line in raw.decode(errors="replace").splitlines():
        name, _, value = line.partition(":")
        if value.split():
            out[name] = int(value.split()[0])
    return out


def write(conn: sqlite3.Connection, taken: Taken, at: datetime) -> None:
    """Add what a take gathered to the ten-minute rows: sums add, peaks keep the larger."""
    with db.transaction(conn):
        conn.executemany(
            """INSERT INTO resource_use (bucket, session_id, part, cpu_s, idle_cpu_s, mem_mb_s, idle_mem_mb_s,
                   swap_mb_s, session_peak_mb, part_peak_mb, programs)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (bucket, session_id, part) DO UPDATE SET
                   cpu_s = cpu_s + excluded.cpu_s, idle_cpu_s = idle_cpu_s + excluded.idle_cpu_s,
                   mem_mb_s = mem_mb_s + excluded.mem_mb_s, idle_mem_mb_s = idle_mem_mb_s + excluded.idle_mem_mb_s,
                   swap_mb_s = swap_mb_s + excluded.swap_mb_s,
                   session_peak_mb = max(session_peak_mb, excluded.session_peak_mb),
                   part_peak_mb = max(part_peak_mb, excluded.part_peak_mb), programs = max(programs, excluded.programs)""",
            [
                (bucket, session, part, t.cpu_s, t.idle_cpu_s, t.mem_mb_s, t.idle_mem_mb_s, t.swap_mb_s,
                 t.session_peak_mb, t.part_peak_mb, t.programs)
                for (bucket, session, part), t in taken.use.items()
            ],
        )
        conn.executemany(
            """INSERT INTO resource_host (bucket, sampled_s, busy_s, steal_s, load_s, avail_mb_s, swap_mb_s,
                   cpu_wait_s, mem_wait_s)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (bucket) DO UPDATE SET
                   sampled_s = sampled_s + excluded.sampled_s, busy_s = busy_s + excluded.busy_s,
                   steal_s = steal_s + excluded.steal_s, load_s = load_s + excluded.load_s,
                   avail_mb_s = avail_mb_s + excluded.avail_mb_s, swap_mb_s = swap_mb_s + excluded.swap_mb_s,
                   cpu_wait_s = cpu_wait_s + excluded.cpu_wait_s, mem_wait_s = mem_wait_s + excluded.mem_wait_s""",
            [
                (bucket, h.sampled_s, h.busy_s, h.steal_s, h.load_s, h.avail_mb_s, h.swap_mb_s, h.cpu_wait_s,
                 h.mem_wait_s)
                for bucket, h in taken.host.items()
            ],
        )
        db.kv_set(conn, NOW_KEY, {"at": at.isoformat().replace("+00:00", "Z"), "tasks": taken.now})


def fold(conn: sqlite3.Connection, now: datetime, zone: ZoneInfo) -> int:
    """Fold ten-minute rows of whole days older than KEEP_DAYS into daily totals, then delete them. Returns how
    many ten-minute rows it folded."""
    keep_from = datetime.combine(now.astimezone(zone).date() - timedelta(days=KEEP_DAYS), datetime.min.time(), zone)
    cutoff = int(keep_from.timestamp())

    def day_of(bucket: int) -> str:
        return datetime.fromtimestamp(bucket, zone).date().isoformat()

    with db.transaction(conn):
        rows = conn.execute("SELECT * FROM resource_use WHERE bucket < ?", (cutoff,)).fetchall()
        days: dict[tuple[str, str, str], Totals] = {}
        for row in rows:
            days.setdefault((day_of(row["bucket"]), row["session_id"], row["part"]), Totals()).add(
                Totals(**{name: row[name] for name in Totals.__dataclass_fields__}))
        conn.executemany(
            """INSERT INTO resource_days (day, session_id, part, cpu_s, idle_cpu_s, mem_mb_s, idle_mem_mb_s,
                   swap_mb_s, session_peak_mb, part_peak_mb, programs)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (day, session_id, part) DO UPDATE SET
                   cpu_s = cpu_s + excluded.cpu_s, idle_cpu_s = idle_cpu_s + excluded.idle_cpu_s,
                   mem_mb_s = mem_mb_s + excluded.mem_mb_s, idle_mem_mb_s = idle_mem_mb_s + excluded.idle_mem_mb_s,
                   swap_mb_s = swap_mb_s + excluded.swap_mb_s,
                   session_peak_mb = max(session_peak_mb, excluded.session_peak_mb),
                   part_peak_mb = max(part_peak_mb, excluded.part_peak_mb), programs = max(programs, excluded.programs)""",
            [
                (day, session, part, t.cpu_s, t.idle_cpu_s, t.mem_mb_s, t.idle_mem_mb_s, t.swap_mb_s,
                 t.session_peak_mb, t.part_peak_mb, t.programs)
                for (day, session, part), t in days.items()
            ],
        )
        host_rows = conn.execute("SELECT * FROM resource_host WHERE bucket < ?", (cutoff,)).fetchall()
        host_days: dict[str, Host] = {}
        for row in host_rows:
            host_days.setdefault(day_of(row["bucket"]), Host()).add(
                Host(**{name: row[name] for name in Host.__dataclass_fields__}))
        conn.executemany(
            """INSERT INTO resource_host_days (day, sampled_s, busy_s, steal_s, load_s, avail_mb_s, swap_mb_s,
                   cpu_wait_s, mem_wait_s)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (day) DO UPDATE SET
                   sampled_s = sampled_s + excluded.sampled_s, busy_s = busy_s + excluded.busy_s,
                   steal_s = steal_s + excluded.steal_s, load_s = load_s + excluded.load_s,
                   avail_mb_s = avail_mb_s + excluded.avail_mb_s, swap_mb_s = swap_mb_s + excluded.swap_mb_s,
                   cpu_wait_s = cpu_wait_s + excluded.cpu_wait_s, mem_wait_s = mem_wait_s + excluded.mem_wait_s""",
            [
                (day, h.sampled_s, h.busy_s, h.steal_s, h.load_s, h.avail_mb_s, h.swap_mb_s, h.cpu_wait_s,
                 h.mem_wait_s)
                for day, h in host_days.items()
            ],
        )
        conn.execute("DELETE FROM resource_use WHERE bucket < ?", (cutoff,))
        conn.execute("DELETE FROM resource_host WHERE bucket < ?", (cutoff,))
    return len(rows)


def track_forever(db_path: Path, listing: Callable[[], list[Any]], zone: Callable[[], ZoneInfo]) -> None:
    """Sample every SAMPLE_SECONDS, write every FLUSH_SECONDS, fold old rows every FOLD_SECONDS. `listing` gives
    Cockpit's current session listing (Session objects) and `zone` the user's timezone, which the user can change."""
    sampler = Sampler(Path("/proc"), os.getuid(), os.sysconf("SC_CLK_TCK"), os.sysconf("SC_PAGE_SIZE") // 1024)
    next_write = time.monotonic() + FLUSH_SECONDS
    next_fold = time.monotonic()
    while True:
        started = time.monotonic()
        try:
            listed = listing()
            sampler.sample(
                time.time(), started, {s.pid: s.session_id for s in listed if s.pid},
                {s.session_id: s.working_now for s in listed},
            )
        except Exception:
            log.exception("sampling processor and memory use failed; this sample is missing")
        if started >= next_write:
            next_write = started + FLUSH_SECONDS
            taken = sampler.take()
            try:
                conn = db.connect(db_path)
                try:
                    write(conn, taken, datetime.now(UTC))
                finally:
                    conn.close()
            except Exception:
                sampler.give_back(taken)
                log.exception("writing processor and memory use failed; the data is kept for the next write")
        if started >= next_fold:
            next_fold = started + FOLD_SECONDS
            try:
                conn = db.connect(db_path)
                try:
                    folded = fold(conn, datetime.now(UTC), zone())
                finally:
                    conn.close()
                if folded:
                    log.info("folded %s ten-minute resource rows into daily totals", folded)
            except Exception:
                log.exception("folding old processor and memory rows into daily totals failed")
        time.sleep(max(SAMPLE_SECONDS - (time.monotonic() - started), 0.5))


# --- Reading the record back, for the Server page ---

RANGES = {"day": (timedelta(days=1), 600), "week": (timedelta(days=7), 3600), "month": (timedelta(days=30), 6 * 3600)}


def _offset_case(zone: ZoneInfo, first: int, last: int) -> str:
    """SQL for each row's offset from UTC in `zone` at the start of its bucket, for buckets from `first` to `last`,
    so a day or a slot is the user's own across a clock change. Clock changes fall on bucket starts."""

    def offset(bucket: int) -> int:
        return int((datetime.fromtimestamp(bucket, zone).utcoffset() or timedelta()).total_seconds())

    first = first // BUCKET_SECONDS * BUCKET_SECONDS
    current = offset(first)
    cases = []
    for bucket in range(first + BUCKET_SECONDS, last + 1, BUCKET_SECONDS):
        if offset(bucket) != current:
            cases.append(f"WHEN bucket < {bucket} THEN {current}")
            current = offset(bucket)
    return f"(CASE {' '.join(cases)} ELSE {current} END)" if cases else str(current)


def report(conn: sqlite3.Connection, span: str, now: datetime, zone: ZoneInfo, cores: int,
           mem_total_mb: float) -> dict[str, Any]:
    """The Server page's figures over the last `span` (a RANGES key). Sessions of one task add up to one row; a
    session without a task is its own row. SQLite adds the rows up, so a month of them stays quick to read."""
    length, step = RANGES[span]
    begin = (int(now.timestamp()) - int(length.total_seconds())) // BUCKET_SECONDS * BUCKET_SECONDS
    offset = _offset_case(zone, begin, int(now.timestamp()))
    slot = f"((bucket + {offset}) / :step * :step - {offset})"
    where = {"begin": begin, "step": step}
    host_series: dict[int, Host] = {}
    for row in conn.execute(
        f"""SELECT {slot} AS at, sum(sampled_s) AS sampled_s, sum(busy_s) AS busy_s, sum(steal_s) AS steal_s,
                   sum(load_s) AS load_s, sum(avail_mb_s) AS avail_mb_s, sum(swap_mb_s) AS swap_mb_s,
                   sum(cpu_wait_s) AS cpu_wait_s, sum(mem_wait_s) AS mem_wait_s
            FROM resource_host WHERE bucket >= :begin GROUP BY at ORDER BY at""", where):
        host_series[row["at"]] = Host(**{name: row[name] for name in Host.__dataclass_fields__})
    families = list(FAMILIES)
    cpu = {at: dict.fromkeys(families, 0.0) for at in host_series}
    mem = {at: dict.fromkeys(families, 0.0) for at in host_series}
    for row in conn.execute(
        f"""SELECT {slot} AS at, part, sum(cpu_s) AS cpu_s, sum(mem_mb_s) AS mem_mb_s FROM resource_use
            WHERE bucket >= :begin GROUP BY at, part""", where):
        if row["at"] in cpu:
            family = FAMILY_OF.get(row["part"], "Other")
            cpu[row["at"]][family] += row["cpu_s"]
            mem[row["at"]][family] += row["mem_mb_s"]
    use_rows = conn.execute(
        """SELECT session_id, part, sum(cpu_s) AS cpu_s, sum(idle_cpu_s) AS idle_cpu_s, sum(mem_mb_s) AS mem_mb_s,
                  sum(idle_mem_mb_s) AS idle_mem_mb_s, max(part_peak_mb) AS part_peak_mb
           FROM resource_use WHERE bucket >= ? GROUP BY session_id, part""", (begin,)).fetchall()
    titles = session_titles(conn, {row["session_id"] for row in use_rows})

    def task_key(session: str) -> str:
        return str(titles[session]["item_id"]) if session in titles else session

    tasks: dict[str, dict[str, Any]] = {}
    parts: dict[str, dict[str, float]] = {}
    for row in use_rows:
        task = tasks.setdefault(task_key(row["session_id"]), {
            "session_ids": [], **(titles.get(row["session_id"]) or {"item_id": None, "title": "", "project": ""}),
            "cpu_s": 0.0, "idle_cpu_s": 0.0, "mem_mb_s": 0.0, "idle_mem_mb_s": 0.0, "peak_mb": 0.0, "parts": {},
        })
        if row["session_id"] not in task["session_ids"]:
            task["session_ids"].append(row["session_id"])
        for name in ("cpu_s", "idle_cpu_s", "mem_mb_s", "idle_mem_mb_s"):
            task[name] += row[name]
        task_part = task["parts"].setdefault(row["part"], [0.0, 0.0])
        task_part[0] += row["cpu_s"]
        task_part[1] += row["mem_mb_s"]
        part = parts.setdefault(row["part"], {"cpu_s": 0.0, "idle_cpu_s": 0.0, "mem_mb_s": 0.0, "peak_mb": 0.0})
        part["cpu_s"] += row["cpu_s"]
        part["idle_cpu_s"] += row["idle_cpu_s"]
        part["mem_mb_s"] += row["mem_mb_s"]
        part["peak_mb"] = max(part["peak_mb"], row["part_peak_mb"])
    # A task's peak: per ten-minute row, the most each of its sessions held at once, added up over its sessions.
    held: dict[tuple[int, str], float] = {}
    for row in conn.execute(
        "SELECT bucket, session_id, max(session_peak_mb) AS peak_mb FROM resource_use WHERE bucket >= ? "
        "GROUP BY bucket, session_id", (begin,)):
        key = (row["bucket"], task_key(row["session_id"]))
        held[key] = held.get(key, 0.0) + row["peak_mb"]
    for (_bucket, key), mb in held.items():
        tasks[key]["peak_mb"] = max(tasks[key]["peak_mb"], mb)
    total_s = sum(h.sampled_s for h in host_series.values())

    def hours(seconds: float) -> float:
        return round(seconds / 3600, 3)

    def share(part: float, whole: float) -> float:
        return round(part / whole, 3) if whole else 0.0

    series = []
    for at, h in sorted(host_series.items()):
        seconds = h.sampled_s or 1.0
        series.append({
            "at": datetime.fromtimestamp(at, UTC).isoformat().replace("+00:00", "Z"),
            "sampled_s": round(h.sampled_s),
            "cpu": {family: round(cpu[at][family] / seconds, 3) for family in families},
            "mem_gb": {family: round(mem[at][family] / seconds / 1024, 3) for family in families},
            "busy": round(h.busy_s / seconds, 3),
            "load": round(h.load_s / seconds, 2),
            "used_gb": round((mem_total_mb - h.avail_mb_s / seconds) / 1024, 3),
            "swap_gb": round(h.swap_mb_s / seconds / 1024, 3),
            "cpu_wait": round(h.cpu_wait_s / seconds, 3),
            "mem_wait": round(h.mem_wait_s / seconds, 3),
        })
    task_list = [
        {
            "item_id": task["item_id"], "title": task["title"], "project": task["project"],
            "session_ids": task["session_ids"],
            "core_hours": hours(task["cpu_s"]), "gb_hours": hours(task["mem_mb_s"] / 1024),
            "idle_cpu_share": share(task["idle_cpu_s"], task["cpu_s"]),
            "idle_mem_share": share(task["idle_mem_mb_s"], task["mem_mb_s"]),
            "peak_gb": round(task["peak_mb"] / 1024, 2),
            "parts": [
                {"part": name, "core_hours": hours(cpu_s), "gb_hours": hours(mem_mb_s / 1024)}
                for name, (cpu_s, mem_mb_s) in sorted(task["parts"].items(), key=lambda pair: -pair[1][0])
            ],
        }
        for task in tasks.values()
    ]
    task_list.sort(key=lambda task: (-task["core_hours"], -task["gb_hours"]))
    part_list = [
        {"part": name, "family": FAMILY_OF.get(name, "Other"), "core_hours": hours(p["cpu_s"]),
         "gb_hours": hours(p["mem_mb_s"] / 1024), "idle_cpu_share": share(p["idle_cpu_s"], p["cpu_s"]),
         "peak_gb": round(p["peak_mb"] / 1024, 2)}
        for name, p in sorted(parts.items(), key=lambda pair: (-pair[1]["cpu_s"], -pair[1]["mem_mb_s"]))
    ]

    def mean(total: float) -> float | None:
        return total / total_s if total_s else None

    busy = mean(sum(h.busy_s for h in host_series.values()))
    avail = mean(sum(h.avail_mb_s for h in host_series.values()))
    cpu_wait = mean(sum(h.cpu_wait_s for h in host_series.values()))
    mem_wait = mean(sum(h.mem_wait_s for h in host_series.values()))
    machine = {
        "cores": cores, "mem_total_gb": round(mem_total_mb / 1024, 1), "sampled_hours": hours(total_s),
        "busy": None if busy is None else round(busy, 2),
        "used_gb": None if avail is None else round((mem_total_mb - avail) / 1024, 2),
        "cpu_wait": None if cpu_wait is None else round(cpu_wait, 3),
        "mem_wait": None if mem_wait is None else round(mem_wait, 3),
    }
    return {
        "range": span, "step_s": step, "families": families, "series": series, "tasks": task_list[:40],
        "parts": part_list, "machine": machine,
    }


def daily(conn: sqlite3.Connection, now: datetime, zone: ZoneInfo) -> list[dict[str, Any]]:
    """Every recorded day, oldest first: the hours sampled, and core-hours and GB-hours per family. Days older
    than KEEP_DAYS come from the daily totals, recent ones from the ten-minute rows, each in the user's day at its
    own time, as folding counts it."""
    first, last = conn.execute(
        "SELECT min(bucket), max(bucket) FROM (SELECT bucket FROM resource_host UNION ALL SELECT bucket FROM resource_use)"
    ).fetchone()
    offset = _offset_case(zone, first, last) if first is not None else "0"
    out: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        f"""SELECT day, sum(sampled_s) AS sampled_s FROM (
               SELECT day, sampled_s FROM resource_host_days
               UNION ALL SELECT date(bucket + {offset}, 'unixepoch') AS day, sampled_s FROM resource_host)
           GROUP BY day ORDER BY day"""):
        out[row["day"]] = {"day": row["day"], "sampled_hours": round(row["sampled_s"] / 3600, 2),
                           "core_hours": {}, "gb_hours": {}}
    for row in conn.execute(
        f"""SELECT day, part, sum(cpu_s) AS cpu_s, sum(mem_mb_s) AS mem_mb_s FROM (
               SELECT day, part, cpu_s, mem_mb_s FROM resource_days
               UNION ALL SELECT date(bucket + {offset}, 'unixepoch') AS day, part, cpu_s, mem_mb_s FROM resource_use)
           GROUP BY day, part"""):
        day = out.get(row["day"])
        if day is None:
            continue
        family = FAMILY_OF.get(row["part"], "Other")
        day["core_hours"][family] = day["core_hours"].get(family, 0.0) + row["cpu_s"] / 3600
        day["gb_hours"][family] = day["gb_hours"].get(family, 0.0) + row["mem_mb_s"] / 1024 / 3600
    for day in out.values():
        for name in ("core_hours", "gb_hours"):
            day[name] = {family: round(value, 3) for family, value in day[name].items()}
    return list(out.values())


def now_tasks(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """The tasks that used most in the sampler's last minute, or None before the first write. Sessions of one task
    add up to one row; a session without a task is its own row. `session_id` is the row's busiest session."""
    stored = db.kv_get(conn, NOW_KEY)
    if stored is None:
        return None
    titles = session_titles(conn, {row["session_id"] for row in stored["tasks"]})
    tasks: dict[str, dict[str, Any]] = {}
    for row in sorted(stored["tasks"], key=lambda row: -row["cores"]):
        title = titles.get(row["session_id"])
        task = tasks.setdefault(str(title["item_id"]) if title else row["session_id"], {
            "session_id": row["session_id"], "session_ids": [],
            **(title or {"item_id": None, "title": "", "project": ""}), "cores": 0.0, "mem_mb": 0.0, "parts": {},
        })
        task["session_ids"].append(row["session_id"])
        task["cores"] += row["cores"]
        task["mem_mb"] += row["mem_mb"]
        for part, cores in row["parts"].items():
            task["parts"][part] = task["parts"].get(part, 0.0) + cores
    out = [
        {**task, "cores": round(task["cores"], 2), "mem_mb": round(task["mem_mb"]),
         "parts": [{"part": part, "cores": round(cores, 2)}
                   for part, cores in sorted(task["parts"].items(), key=lambda pair: -pair[1])[:3]]}
        for task in tasks.values()
    ]
    out.sort(key=lambda task: (-task["cores"], -task["mem_mb"]))
    return {"at": stored["at"], "tasks": out[:NOW_TASKS]}


def session_titles(conn: sqlite3.Connection, session_ids: set[str]) -> dict[str, dict[str, Any]]:
    """Each of these sessions' task: the item that lists it earliest among its sessions (its primary session
    first), the oldest such item on a tie. Sessions no item lists are left out."""
    out: dict[str, dict[str, Any]] = {}
    rows = conn.execute(
        """SELECT items.id, items.title, items.project, j.value AS session_id FROM items, json_each(items.sessions) j
           WHERE j.value IN (SELECT value FROM json_each(?)) ORDER BY j.key DESC, items.id DESC""",
        (json.dumps(sorted(session_ids - {""})),),
    )
    for row in rows:
        out[row["session_id"]] = {"item_id": row["id"], "title": row["title"], "project": row["project"] or ""}
    return out


def memory_total_mb() -> float:
    return _meminfo(Path("/proc/meminfo").read_bytes()).get("MemTotal", 0) / 1024
