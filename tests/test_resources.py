"""Processor time and memory per task and part (resources.py, SPEC TRACK-16), read from a fake /proc tree."""

from __future__ import annotations

import errno
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from cockpit import db, resources
from tests.conftest import World

AGENT_SESSION = "a1a1a1a1-0000-4000-8000-000000000001"
OTHER_SESSION = "b2b2b2b2-0000-4000-8000-000000000002"
HZ = 100
UTCZ = ZoneInfo("UTC")


@dataclass
class Proc:
    pid: int
    comm: str
    ppid: int
    cmd: str
    start: int
    ticks: int = 0
    child_ticks: int = 0
    rss_pages: int = 256
    pss_kb: int = 1024
    swap_kb: int = 0
    env: str = ""


@dataclass
class FakeProc:
    """A /proc directory the sampler reads: programs, and the host's counters."""

    root: Path
    programs: dict[int, Proc] = field(default_factory=dict)
    run_ns: int = 0
    cpu_wait_us: int = 0
    avail_kb: int = 8 * 1024 * 1024
    schedstat: bool = True

    def add(self, proc: Proc) -> Proc:
        self.programs[proc.pid] = proc
        return proc

    def end(self, pid: int) -> None:
        del self.programs[pid]

    def write(self) -> None:
        for path in self.root.iterdir() if self.root.exists() else ():
            if path.name.isdigit() and int(path.name) not in self.programs:
                for child in path.iterdir():
                    child.unlink()
                path.rmdir()
        self.root.mkdir(exist_ok=True)
        for proc in self.programs.values():
            here = self.root / str(proc.pid)
            here.mkdir(exist_ok=True)
            # pid (comm) state ppid pgrp session tty tpgid flags minflt cminflt majflt cmajflt utime stime cutime
            # cstime priority nice threads itrealvalue starttime
            fields = [proc.ppid, 0, 0, 0, 0, 0, 0, 0, 0, 0, proc.ticks, 0, proc.child_ticks, 0, 20, 0, 1, 0, proc.start]
            (here / "stat").write_text(f"{proc.pid} ({proc.comm}) S " + " ".join(map(str, fields)))
            (here / "cmdline").write_bytes(proc.cmd.replace(" ", "\0").encode())
            (here / "statm").write_text(f"1000 {proc.rss_pages} 0 0 0 0 0")
            (here / "smaps_rollup").write_text(f"Rss: 1 kB\nPss: {proc.pss_kb} kB\nSwapPss: {proc.swap_kb} kB\n")
            (here / "environ").write_bytes(proc.env.encode())
        (self.root / "stat").write_text("cpu 0 0 0 0 0 0 0 0\n")
        (self.root / "meminfo").write_text(f"MemTotal: 16777216 kB\nMemAvailable: {self.avail_kb} kB\n"
                                           "SwapTotal: 0 kB\nSwapFree: 0 kB\n")
        (self.root / "loadavg").write_text("1.50 1.0 1.0 1/100 1")
        if self.schedstat:
            (self.root / "schedstat").write_text(f"version 15\ntimestamp 1\ncpu0 0 0 0 0 0 0 {self.run_ns} 0 0\n")
        elif (self.root / "schedstat").exists():
            (self.root / "schedstat").unlink()
        (self.root / "pressure").mkdir(exist_ok=True)
        (self.root / "pressure" / "cpu").write_text(f"some avg10=0 avg60=0 avg300=0 total={self.cpu_wait_us}\n")
        (self.root / "pressure" / "memory").write_text("some avg10=0 avg60=0 avg300=0 total=0\n")


@pytest.fixture
def proc(tmp_path: Path) -> FakeProc:
    return FakeProc(tmp_path / "proc")


def sampler_for(proc: FakeProc) -> resources.Sampler:
    return resources.Sampler(proc.root, os.getuid(), HZ, 4)


WALL = datetime(2026, 11, 11, 12, 0, 5, tzinfo=UTC).timestamp()


def step(sampler: resources.Sampler, proc: FakeProc, at: float, working: bool = True) -> None:
    proc.write()
    sampler.sample(WALL + at, 1000 + at, {10: AGENT_SESSION}, {AGENT_SESSION: working})


def cpu_of(sampler: resources.Sampler) -> dict[tuple[str, str], float]:
    out: dict[tuple[str, str], float] = {}
    for (_bucket, session, part), totals in sampler.use.items():
        if totals.cpu_s:
            out[(session, part)] = round(out.get((session, part), 0.0) + totals.cpu_s, 3)
    return out


def agent_tree(proc: FakeProc) -> None:
    proc.add(Proc(1, "systemd", 0, "/sbin/init", start=1))
    proc.add(Proc(10, "claude", 1, "/home/u/.local/share/claude/versions/2.1/claude --resume x", start=100))


def test_a_programs_time_goes_to_its_session_and_part(proc: FakeProc) -> None:
    agent_tree(proc)
    test_run = proc.add(Proc(20, "python", 10, "python -m pytest -q", start=200))
    sampler = sampler_for(proc)
    step(sampler, proc, 0)
    proc.programs[10].ticks += 50
    test_run.ticks += 300
    step(sampler, proc, 5)
    assert cpu_of(sampler) == {(AGENT_SESSION, "Claude agent"): 0.5, (AGENT_SESSION, "Python tests"): 3.0}


def test_time_before_the_first_sample_or_across_a_long_gap_is_not_counted(proc: FakeProc) -> None:
    agent_tree(proc)
    proc.programs[10].ticks = 5000
    sampler = sampler_for(proc)
    step(sampler, proc, 0)
    proc.programs[10].ticks += 1000
    step(sampler, proc, resources.LONGEST_GAP + 1)
    assert cpu_of(sampler) == {}
    assert sampler.host == {}


def test_a_command_that_starts_and_ends_between_samples_counts_as_short_commands(proc: FakeProc) -> None:
    agent_tree(proc)
    sampler = sampler_for(proc)
    step(sampler, proc, 0)
    proc.programs[10].child_ticks += 40  # a child the sampler never saw
    step(sampler, proc, 5)
    assert cpu_of(sampler) == {(AGENT_SESSION, "Short commands"): 0.4}


def test_a_command_the_sampler_saw_end_is_not_counted_twice(proc: FakeProc) -> None:
    agent_tree(proc)
    shell = proc.add(Proc(30, "bash", 10, "bash -c make", start=300))
    sampler = sampler_for(proc)
    step(sampler, proc, 0)
    shell.ticks += 20
    step(sampler, proc, 5)
    # The shell ends: its 20 ticks join the agent's child counters, plus 10 it used after the last sample.
    proc.end(30)
    proc.programs[10].child_ticks += 30
    step(sampler, proc, 10)
    assert cpu_of(sampler) == {(AGENT_SESSION, "Shell commands"): 0.2, (AGENT_SESSION, "Short commands"): 0.1}


def test_when_a_parent_ends_with_its_child_the_time_lands_on_the_nearest_running_ancestor(proc: FakeProc) -> None:
    agent_tree(proc)
    shell = proc.add(Proc(30, "bash", 10, "bash -c make", start=300))
    tool = proc.add(Proc(31, "git", 30, "git status", start=310))
    sampler = sampler_for(proc)
    step(sampler, proc, 0)
    shell.ticks += 10
    tool.ticks += 20
    step(sampler, proc, 5)
    proc.end(30)
    proc.end(31)
    proc.programs[10].child_ticks += 30  # both programs' time, seen and credited already
    step(sampler, proc, 10)
    assert cpu_of(sampler) == {(AGENT_SESSION, "Shell commands"): 0.1, (AGENT_SESSION, "git and gh"): 0.2}


def test_a_program_outside_any_agent_takes_the_session_in_its_environment(proc: FakeProc) -> None:
    agent_tree(proc)
    loose = proc.add(Proc(40, "python3", 1, "python3 job.py", start=400, env=f"A=1\0CLAUDE_CODE_SESSION_ID={OTHER_SESSION}\0"))
    server = proc.add(Proc(50, "python3", 1, "/srv/cockpit-server --port 1", start=500))
    sampler = sampler_for(proc)
    step(sampler, proc, 0)
    loose.ticks += 10
    server.ticks += 10
    step(sampler, proc, 5)
    assert cpu_of(sampler) == {(OTHER_SESSION, "Other Python"): 0.1, ("", "Cockpit server"): 0.1}


def test_a_claude_started_by_cockpits_server_is_a_model_call(proc: FakeProc) -> None:
    proc.add(Proc(1, "systemd", 0, "/sbin/init", start=1))
    proc.add(Proc(50, "python3", 1, "/srv/cockpit-server", start=500))
    call = proc.add(Proc(51, "claude", 50, "claude -p hello", start=510))
    sampler = sampler_for(proc)
    step(sampler, proc, 0)
    call.ticks += 10
    step(sampler, proc, 5)
    assert cpu_of(sampler) == {("", "Cockpit model calls"): 0.1}


def test_idle_time_is_what_the_session_used_while_not_working(proc: FakeProc) -> None:
    agent_tree(proc)
    sampler = sampler_for(proc)
    step(sampler, proc, 0, working=False)
    proc.programs[10].ticks += 10
    step(sampler, proc, 5, working=False)
    totals = next(t for (_b, s, p), t in sampler.use.items() if p == "Claude agent")
    assert (totals.cpu_s, totals.idle_cpu_s) == (0.1, 0.1)


def test_memory_is_the_proportional_share_times_the_seconds_it_covers(proc: FakeProc) -> None:
    agent_tree(proc)
    proc.programs[10].pss_kb = 2048
    sampler = sampler_for(proc)
    for n in range(resources.MEMORY_EVERY):
        step(sampler, proc, n * 5)
    proc.programs[10].pss_kb = 4096  # read at the next memory sample, a minute after the first
    for n in range(resources.MEMORY_EVERY, 2 * resources.MEMORY_EVERY):
        step(sampler, proc, n * 5)
    mem = {p: t.mem_mb_s for (_b, s, p), t in sampler.use.items() if s == AGENT_SESSION and t.mem_mb_s}
    # Every counted sample adds the latest reading for the five seconds since the sample before; the first counts none.
    assert mem == {"Claude agent": 2.0 * (resources.MEMORY_EVERY - 1) * 5 + 4.0 * resources.MEMORY_EVERY * 5}
    peak = max(t.session_peak_mb for (_b, s, p), t in sampler.use.items() if p == "Claude agent")
    assert peak == 4.0


def test_busy_time_comes_from_the_schedulers_clock(proc: FakeProc) -> None:
    agent_tree(proc)
    sampler = sampler_for(proc)
    step(sampler, proc, 0)
    proc.run_ns += 7_500_000_000
    proc.cpu_wait_us += 250_000
    step(sampler, proc, 5)
    (host,) = sampler.host.values()
    assert (host.sampled_s, host.busy_s, host.cpu_wait_s, host.load_s) == (5, 7.5, 0.25, 7.5)


def test_a_machine_without_schedstat_fails_loudly(proc: FakeProc) -> None:
    agent_tree(proc)
    proc.schedstat = False
    with pytest.raises(RuntimeError, match="schedstat"):
        step(sampler_for(proc), proc, 0)


def test_take_names_the_minutes_busiest_sessions(proc: FakeProc) -> None:
    agent_tree(proc)
    sampler = sampler_for(proc)
    step(sampler, proc, 0)
    proc.programs[10].ticks += 250
    step(sampler, proc, 5)
    taken = sampler.take()
    now = {row["session_id"]: row for row in taken.now}
    assert now[AGENT_SESSION] == {"session_id": AGENT_SESSION, "cores": 0.5, "mem_mb": 1.0,
                                  "parts": {"Claude agent": 0.5}}
    assert now[""]["parts"] == {}  # the machine's own programs, such as init, used nothing
    assert sampler.use == {} and sampler.minute_s == 0


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "r.db")
    db.migrate(connection)
    yield connection
    connection.close()


def add_item(conn, title: str, sessions: list[str]) -> int:
    import json

    cur = conn.execute(
        "INSERT INTO items (key, title, project, sessions, stage, created_at, updated_at, updated_by)"
        " VALUES (?, ?, 'cockpit', ?, 'building', '2026-11-11T00:00:00Z', '2026-11-11T00:00:00Z', 'test')",
        (f"note:{title}", title, json.dumps(sessions)),
    )
    return int(cur.lastrowid or 0)


def taken_at(bucket: int, rows: dict[tuple[str, str], tuple[float, float]], sampled: float = 600) -> resources.Taken:
    use = {(bucket, s, p): resources.Totals(cpu_s=c, mem_mb_s=m, session_peak_mb=m / sampled, part_peak_mb=m / sampled)
           for (s, p), (c, m) in rows.items()}
    host = {bucket: resources.Host(sampled_s=sampled, busy_s=sum(c for c, _m in rows.values()),
                                   avail_mb_s=8192 * sampled)}
    return resources.Taken(use, host, [{"session_id": AGENT_SESSION, "cores": 1.0, "mem_mb": 500, "parts": {}}])


NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


def test_the_report_adds_up_each_task_and_part(conn) -> None:
    item = add_item(conn, "Server page", [AGENT_SESSION, OTHER_SESSION])
    bucket = int(NOW.timestamp()) - 3600
    resources.write(conn, taken_at(bucket, {
        (AGENT_SESSION, "Claude agent"): (600.0, 600 * 1024.0),
        (OTHER_SESSION, "Python tests"): (1200.0, 600 * 512.0),
        ("", "Cockpit server"): (60.0, 600 * 256.0),
    }), NOW)
    out = resources.report(conn, "day", NOW, UTCZ, 4, 16384)
    task = out["tasks"][0]
    assert (task["item_id"], task["title"], task["core_hours"]) == (item, "Server page", 0.5)
    assert task["session_ids"] == [AGENT_SESSION, OTHER_SESSION] or task["session_ids"] == [OTHER_SESSION, AGENT_SESSION]
    assert [p["part"] for p in task["parts"]] == ["Python tests", "Claude agent"]
    assert task["peak_gb"] == 1.5
    assert out["tasks"][1]["item_id"] is None
    assert [p["part"] for p in out["parts"]] == ["Python tests", "Claude agent", "Cockpit server"]
    (slot,) = out["series"]
    assert slot["cpu"]["Tests"] == 2.0 and slot["cpu"]["Claude agents"] == 1.0
    assert slot["mem_gb"]["Claude agents"] == 1.0
    assert slot["used_gb"] == 8.0
    assert out["machine"] == {"cores": 4, "mem_total_gb": 16.0, "sampled_hours": 0.167, "busy": 3.1,
                              "used_gb": 8.0, "cpu_wait": 0.0, "mem_wait": 0.0}


def test_writes_into_one_bucket_add_up(conn) -> None:
    bucket = int(NOW.timestamp()) - 3600
    for _ in range(2):
        resources.write(conn, taken_at(bucket, {(AGENT_SESSION, "Claude agent"): (60.0, 6000.0)}, sampled=300), NOW)
    row = conn.execute("SELECT cpu_s, mem_mb_s FROM resource_use").fetchone()
    assert (row["cpu_s"], row["mem_mb_s"]) == (120.0, 12000.0)
    assert conn.execute("SELECT sampled_s FROM resource_host").fetchone()[0] == 600


def test_now_tasks_names_each_sessions_task(conn) -> None:
    assert resources.now_tasks(conn) is None
    item = add_item(conn, "Server page", [AGENT_SESSION])
    resources.write(conn, taken_at(int(NOW.timestamp()), {}), NOW)
    now = resources.now_tasks(conn)
    assert now is not None
    assert now["at"] == "2026-11-11T12:00:00Z"
    assert now["tasks"][0] == {"session_id": AGENT_SESSION, "session_ids": [AGENT_SESSION], "cores": 1.0,
                               "mem_mb": 500, "parts": [], "item_id": item, "title": "Server page", "project": "cockpit"}


def test_a_session_belongs_to_the_task_that_lists_it_first(conn) -> None:
    add_item(conn, "Side task", [OTHER_SESSION, AGENT_SESSION])
    main = add_item(conn, "Main task", [AGENT_SESSION])
    assert resources.session_titles(conn, {AGENT_SESSION, ""})[AGENT_SESSION]["item_id"] == main


def test_old_rows_fold_into_daily_totals_and_every_day_stays_on_record(conn) -> None:
    old = int((NOW - timedelta(days=resources.KEEP_DAYS + 2)).timestamp())
    recent = int((NOW - timedelta(hours=1)).timestamp())
    for bucket in (old, old + 600, recent):
        resources.write(conn, taken_at(bucket // 600 * 600, {(AGENT_SESSION, "Claude agent"): (3600.0, 3600 * 1024.0)}), NOW)
    assert resources.fold(conn, NOW, UTCZ) == 2
    assert conn.execute("SELECT count(*) FROM resource_use").fetchone()[0] == 1
    days = resources.daily(conn, NOW, UTCZ)
    assert [d["day"] for d in days] == [(NOW - timedelta(days=resources.KEEP_DAYS + 2)).date().isoformat(), "2026-11-11"]
    assert days[0]["core_hours"] == {"Claude agents": 2.0} and days[0]["gb_hours"] == {"Claude agents": 2.0}
    assert days[0]["sampled_hours"] == 0.33
    assert resources.fold(conn, NOW, UTCZ) == 0


def test_the_server_page_and_its_figures_load(world: World) -> None:
    page = world.client.get("/server")
    assert page.status_code == 200 and "server.js" in page.text
    conn = db.connect(world.db_path)
    try:
        resources.write(conn, taken_at(int(datetime.now(UTC).timestamp()) // 600 * 600 - 600,
                                       {(AGENT_SESSION, "Claude agent"): (60.0, 6000.0)}), NOW)
    finally:
        conn.close()
    for span in ("day", "week", "month"):
        figures = world.client.get(f"/api/resources?range={span}", headers=world.user())
        assert figures.status_code == 200, figures.text
        body = figures.json()
        assert body["range"] == span and body["families"] == list(resources.FAMILIES)
        assert body["tasks"][0]["session_ids"] == [AGENT_SESSION]
        assert body["now"]["tasks"][0]["session_id"] == AGENT_SESSION
        assert body["daily"][-1]["core_hours"] == {"Claude agents": round(60 / 3600, 3)}
    assert world.client.get("/api/resources?range=year", headers=world.user()).status_code == 400
    state = world.client.get("/api/state", headers=world.user()).json()
    assert state["usage"]["machine"]["tasks_now"]["tasks"][0]["session_id"] == AGENT_SESSION


def test_a_child_reaped_while_a_sample_is_read_is_not_counted_twice(proc: FakeProc) -> None:
    agent_tree(proc)
    shell = proc.add(Proc(30, "bash", 10, "bash -c make", start=300))
    sampler = sampler_for(proc)
    step(sampler, proc, 0)
    shell.ticks += 100
    step(sampler, proc, 5)
    proc.end(30)  # gone, but the agent's counters were read before they took in the shell's time
    step(sampler, proc, 10)
    proc.programs[10].child_ticks += 110  # the shell's credited second, and 10 ticks it used at the end
    step(sampler, proc, 15)
    assert cpu_of(sampler) == {(AGENT_SESSION, "Shell commands"): 1.0, (AGENT_SESSION, "Short commands"): 0.1}


def test_a_child_reaped_after_a_long_gap_is_not_counted_twice(proc: FakeProc) -> None:
    agent_tree(proc)
    shell = proc.add(Proc(30, "bash", 10, "bash -c make", start=300))
    sampler = sampler_for(proc)
    step(sampler, proc, 0)
    shell.ticks += 100
    step(sampler, proc, 5)
    proc.end(30)  # gone, but the agent's counters were read before they took in the shell's time
    step(sampler, proc, 10)
    # A stalled sample: the gap is credited nothing, and the agent's counters still lack the shell.
    step(sampler, proc, 11 + resources.LONGEST_GAP)
    proc.programs[10].child_ticks += 110  # the shell's credited second, and 10 ticks it used at the end
    step(sampler, proc, 16 + resources.LONGEST_GAP)
    assert cpu_of(sampler) == {(AGENT_SESSION, "Shell commands"): 1.0, (AGENT_SESSION, "Short commands"): 0.1}


def test_a_background_program_keeps_its_session_after_its_parent_is_forgotten(proc: FakeProc) -> None:
    agent_tree(proc)
    proc.add(Proc(30, "bash", 10, "bash -c 'pytest &'", start=300))
    tests = proc.add(Proc(40, "python", 30, "python -m pytest -q", start=400))
    sampler = sampler_for(proc)
    step(sampler, proc, 0)
    proc.end(30)
    tests.ppid = 1
    at = 0
    while at <= resources.FORGET_SECONDS + 10:
        at += 5
        step(sampler, proc, at)
    assert (30, 300) not in sampler.programs
    tests.ticks += 100
    step(sampler, proc, at + 5)
    assert cpu_of(sampler) == {(AGENT_SESSION, "Python tests"): 1.0}


def test_a_program_that_runs_another_in_its_place_is_classified_again(proc: FakeProc) -> None:
    agent_tree(proc)
    shell = proc.add(Proc(30, "bash", 10, "bash -c 'exec ./check'", start=300))
    sampler = sampler_for(proc)
    step(sampler, proc, 0)
    shell.comm, shell.cmd = "python", "python -m pytest -q"
    shell.ticks += 100
    step(sampler, proc, 5)
    assert cpu_of(sampler) == {(AGENT_SESSION, "Python tests"): 1.0}


def test_a_program_run_in_place_under_the_same_name_is_classified_again_within_a_minute(proc: FakeProc) -> None:
    agent_tree(proc)
    launcher = proc.add(Proc(30, "python", 10, "python launch.py", start=300))
    sampler = sampler_for(proc)
    step(sampler, proc, 0)
    launcher.cmd = "python -m pytest -q"
    for n in range(1, resources.MEMORY_EVERY + 1):
        step(sampler, proc, n * 5)
    launcher.ticks += 100
    step(sampler, proc, (resources.MEMORY_EVERY + 1) * 5)
    assert cpu_of(sampler) == {(AGENT_SESSION, "Python tests"): 1.0}


def test_child_time_a_parent_owed_when_it_ended_is_not_counted_twice(proc: FakeProc) -> None:
    agent_tree(proc)
    proc.add(Proc(30, "bash", 10, "bash -c make", start=300))
    child = proc.add(Proc(40, "cc", 30, "cc -c x.c", start=400))
    sampler = sampler_for(proc)
    step(sampler, proc, 0)
    child.ticks += 100
    step(sampler, proc, 5)
    proc.end(40)  # gone before the shell's counters took in its time
    step(sampler, proc, 10)
    proc.end(30)  # the shell collected it, then ended itself
    proc.programs[10].child_ticks += 100
    step(sampler, proc, 15)
    assert cpu_of(sampler) == {(AGENT_SESSION, "Other"): 1.0}


def test_peaks_add_up_what_is_held_at_once(proc: FakeProc) -> None:
    agent_tree(proc)
    proc.programs[10].pss_kb = 1024 * 1024
    proc.add(Proc(11, "claude", 1, "claude --resume y", start=110, pss_kb=1024 * 1024))
    proc.add(Proc(12, "python", 10, "python -m pytest -q", start=120, pss_kb=512 * 1024))
    sampler = sampler_for(proc)
    proc.write()
    sampler.sample(WALL, 1000, {10: AGENT_SESSION, 11: OTHER_SESSION}, {})
    rows = {(s, p): t for (_b, s, p), t in sampler.use.items()}
    assert (rows[(AGENT_SESSION, "Claude agent")].session_peak_mb, rows[(AGENT_SESSION, "Claude agent")].part_peak_mb) == (1536, 2048)
    assert (rows[(AGENT_SESSION, "Python tests")].session_peak_mb, rows[(AGENT_SESSION, "Python tests")].part_peak_mb) == (1536, 512)


def test_the_reports_peaks_count_what_was_held_at_once(conn) -> None:
    add_item(conn, "Server page", [AGENT_SESSION])
    bucket = int(NOW.timestamp()) - 3600
    # One session ran its agent and its tests one after the other, never more than 1 GB at once; another session's
    # agent held 1 GB at the same time as the first one's.
    use = {
        (bucket, AGENT_SESSION, "Claude agent"): resources.Totals(cpu_s=60, session_peak_mb=1024, part_peak_mb=2048),
        (bucket, AGENT_SESSION, "Python tests"): resources.Totals(cpu_s=30, session_peak_mb=1024, part_peak_mb=1024),
        (bucket, OTHER_SESSION, "Claude agent"): resources.Totals(cpu_s=10, session_peak_mb=1024, part_peak_mb=2048),
    }
    resources.write(conn, resources.Taken(use, {bucket: resources.Host(sampled_s=600)}, []), NOW)
    out = resources.report(conn, "day", NOW, UTCZ, 4, 16384)
    assert out["tasks"][0]["peak_gb"] == 1.0
    assert {p["part"]: p["peak_gb"] for p in out["parts"]} == {"Claude agent": 2.0, "Python tests": 1.0}


def test_a_failed_host_reading_changes_nothing(proc: FakeProc) -> None:
    agent_tree(proc)
    sampler = sampler_for(proc)
    step(sampler, proc, 0)
    proc.programs[10].ticks += 100
    proc.schedstat = False
    with pytest.raises(RuntimeError, match="schedstat"):
        step(sampler, proc, 5)
    assert (sampler.samples, sampler.last_at, cpu_of(sampler)) == (1, 1000, {})
    proc.schedstat = True
    step(sampler, proc, 10)
    assert cpu_of(sampler) == {(AGENT_SESSION, "Claude agent"): 1.0}


def test_a_take_whose_write_failed_is_kept_for_the_next(proc: FakeProc) -> None:
    agent_tree(proc)
    sampler = sampler_for(proc)
    step(sampler, proc, 0)
    proc.programs[10].ticks += 100
    step(sampler, proc, 5)
    sampler.give_back(sampler.take())
    proc.programs[10].ticks += 50
    step(sampler, proc, 10)
    assert cpu_of(sampler) == {(AGENT_SESSION, "Claude agent"): 1.5}
    assert sum(h.sampled_s for h in sampler.host.values()) == 10


def test_an_unreadable_file_is_logged_once_and_the_program_still_counts(proc: FakeProc, caplog) -> None:
    agent_tree(proc)
    sampler = sampler_for(proc)
    proc.write()
    for pid in ("10", "1"):  # a file that cannot be read as one, so the reading itself is broken
        (proc.root / pid / "smaps_rollup").unlink()
        (proc.root / pid / "smaps_rollup").mkdir()
    with caplog.at_level(logging.ERROR, logger=resources.log.name):
        sampler.sample(WALL, 1000, {10: AGENT_SESSION}, {})
        sampler.samples = 0  # read memory again
        sampler.sample(WALL + 5, 1005, {10: AGENT_SESSION}, {})
    assert [r.message for r in caplog.records if "smaps_rollup" in r.message] and len(caplog.records) == 1
    assert sampler.failures == {("smaps_rollup", errno.EISDIR)}
    mem = {p: t.mem_mb_s for (_b, s, p), t in sampler.use.items() if s == AGENT_SESSION}
    assert mem == {"Claude agent": 5.0}  # its resident 1 MB, for five seconds


def test_a_program_that_shuts_out_its_owner_counts_as_a_system_program(proc: FakeProc, caplog) -> None:
    if os.getuid() == 0:
        pytest.skip("root reads every file")
    agent_tree(proc)
    proc.programs[10].pss_kb = 4096
    proc.add(Proc(30, "ssh-agent", 1, "/usr/bin/ssh-agent -D", start=150))
    sampler = sampler_for(proc)
    proc.write()
    (proc.root / "30" / "environ").chmod(0)
    (proc.root / "30" / "smaps_rollup").chmod(0)
    with caplog.at_level(logging.DEBUG, logger=resources.log.name):
        sampler.sample(WALL, 1000, {10: AGENT_SESSION}, {})  # reads the agent's 4 MB share
        (proc.root / "10" / "smaps_rollup").chmod(0)  # an agent that shuts itself later keeps its part
        sampler.samples = 0  # read memory again
        sampler.sample(WALL + 5, 1005, {10: AGENT_SESSION}, {})
    assert not caplog.records and not sampler.failures
    mem = {(s, p): t.mem_mb_s for (_b, s, p), t in sampler.use.items() if t.mem_mb_s}
    # Resident sizes, 1 MB for five seconds: the agent's old 4 MB reading no longer counts for it.
    assert mem[("", "System services")] >= 5.0 and mem[(AGENT_SESSION, "Claude agent")] == 5.0


def test_an_agent_whose_open_files_are_refused_counts_by_its_resident_size(proc: FakeProc, caplog) -> None:
    if os.getuid() == 0:
        pytest.skip("root reads every file")
    agent_tree(proc)
    proc.programs[10].pss_kb, proc.programs[10].swap_kb = 4096, 1024
    sampler = sampler_for(proc)
    with caplog.at_level(logging.DEBUG, logger=resources.log.name):
        for at in (0, 20, 40, 60):  # memory is read at 0 only; the agent's open files are searched at 0 and 60
            if at == 60:
                (proc.root / "10" / "fd").mkdir(mode=0)
            proc.write()
            sampler.sample(WALL + at, 1000 + at, {}, {})
    assert not caplog.records and not sampler.failures
    agent = [t for (_b, s, p), t in sampler.use.items() if p == "Claude agent"]
    # 4 MB and 1 MB of swap for 40 seconds, then its resident 1 MB and no swap for the last 20.
    assert (sum(t.mem_mb_s for t in agent), sum(t.swap_mb_s for t in agent)) == (180.0, 40.0)


def test_a_shut_program_that_runs_an_open_one_in_its_place_is_the_users_again(proc: FakeProc) -> None:
    if os.getuid() == 0:
        pytest.skip("root reads every file")
    agent_tree(proc)
    shell = proc.add(Proc(30, "bash", 10, "bash -c 'exec ./check'", start=300))
    sampler = sampler_for(proc)
    proc.write()
    (proc.root / "30" / "environ").chmod(0)
    sampler.sample(WALL, 1000, {10: AGENT_SESSION}, {AGENT_SESSION: True})
    (proc.root / "30" / "environ").chmod(0o644)
    shell.comm, shell.cmd = "python", "python -m pytest -q"
    shell.ticks += 100
    step(sampler, proc, 5)
    assert cpu_of(sampler) == {(AGENT_SESSION, "Python tests"): 1.0}


def test_now_tasks_adds_up_each_tasks_sessions(conn) -> None:
    item = add_item(conn, "Server page", [AGENT_SESSION, OTHER_SESSION])
    loose = "c3c3c3c3-0000-4000-8000-000000000003"
    now = [
        {"session_id": AGENT_SESSION, "cores": 0.5, "mem_mb": 100, "parts": {"Claude agent": 0.3, "Python tests": 0.2}},
        {"session_id": OTHER_SESSION, "cores": 0.4, "mem_mb": 200, "parts": {"Python tests": 0.4}},
        {"session_id": loose, "cores": 0.8, "mem_mb": 50, "parts": {"Codex agent": 0.8}},
    ]
    resources.write(conn, resources.Taken({}, {}, now), NOW)
    report = resources.now_tasks(conn)
    assert report is not None
    tasks = report["tasks"]
    assert tasks[0] == {"session_id": AGENT_SESSION, "session_ids": [AGENT_SESSION, OTHER_SESSION], "item_id": item,
                        "title": "Server page", "project": "cockpit", "cores": 0.9, "mem_mb": 300,
                        "parts": [{"part": "Python tests", "cores": 0.6}, {"part": "Claude agent", "cores": 0.3}]}
    assert (tasks[1]["session_id"], tasks[1]["item_id"], len(tasks)) == (loose, None, 2)


def test_recent_days_count_each_row_at_its_own_offset(conn) -> None:
    berlin = ZoneInfo("Europe/Berlin")
    # 00:30 on 25 October in Berlin, still summer time (UTC+2); the clocks went back at 03:00 that night.
    summer = int(datetime(2026, 10, 24, 22, 30, tzinfo=UTC).timestamp())
    for bucket in (summer, int(NOW.timestamp()) - 3600):
        resources.write(conn, taken_at(bucket, {(AGENT_SESSION, "Claude agent"): (3600.0, 0.0)}), NOW)
    assert [d["day"] for d in resources.daily(conn, NOW, berlin)] == ["2026-10-25", "2026-11-11"]


@pytest.mark.parametrize(("comm", "cmd", "part"), [
    ("claude", "claude --bg-pty-host x", "Claude terminal host"),
    ("claude", "claude daemon", "Claude background daemon"),
    ("node", "node /usr/lib/codex app-server", "Codex agent"),
    ("codex", "codex app-server --enable fast_mode", "Codex agent"),
    ("node", "node /home/sam/.bun/bin/codex exec --json", "Codex agent"),
    ("bun", "bun /home/sam/.bun/install/global/node_modules/@openai/codex/bin/codex.js exec", "Codex agent"),
    ("codex-code-mode", "/home/sam/.bun/install/global/node_modules/@openai/codex-linux-x64/vendor/"
     "x86_64-unknown-linux-musl/bin/codex-code-mode-host", "Codex agent"),
    ("python", "/home/sam/.local/share/cockpit/venv/bin/python -c import runpy,sys; sys.path.insert(0,sys.argv.pop(1));"
     " runpy.run_module('cockpit.codex_worker',run_name='__main__') /tmp/code /tmp/cockpit.db codex", "Codex agent"),
    ("python", "python -m pytest tests/test_codex_agent.py", "Python tests"),
    ("git", "git log -- src/cockpit/codex_agent.py", "git and gh"),
    ("rg", "rg codex src", "Shell commands"),
    ("chrome", "/opt/chrome --headless", "Headless browser"),
    ("bun", "bun tests/browser/x.cjs", "Browser test scripts"),
    ("gh", "gh pr view", "git and gh"),
    ("rg", "rg foo", "Shell commands"),
    ("uv", "uv run x", "Other Python"),
    ("ld", "ld a.o", "Other"),
])
def test_parts_by_program(comm: str, cmd: str, part: str) -> None:
    assert resources.kind_of(comm, cmd) == part
    assert part in resources.PARTS
