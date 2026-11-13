"""serve_preview.py's synthetic board, plus eight days of made-up processor and memory readings (SPEC TRACK-16).

The readings follow a daily rhythm: the two fixture sessions run their agents, tests and browsers in the working
hours, Cockpit's own server and the machine's services run all day. Nothing is read from this host's /proc.

Usage: .venv/bin/python tests/browser/serve_resources.py <temporary-work-folder>
"""

from __future__ import annotations

import math
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import serve_slow

from cockpit import db, resources
from tests.conftest import SESSION_A, SESSION_B

serve_slow.SLOW_SECONDS = 0
board = serve_slow.seed_board

# (session, part): (cores at the day's busiest, memory in MB, works only in the day's working hours)
PARTS = {
    (SESSION_A, "Claude agent"): (0.35, 620, True),
    (SESSION_A, "Python tests"): (1.6, 900, True),
    (SESSION_A, "Headless browser"): (0.9, 1400, True),
    (SESSION_A, "Shell commands"): (0.2, 120, True),
    (SESSION_B, "Claude agent"): (0.25, 540, True),
    (SESSION_B, "Codex agent"): (0.3, 380, True),
    (SESSION_B, "git and gh"): (0.1, 60, True),
    ("", "Cockpit server"): (0.15, 310, False),
    ("", "Cockpit model calls"): (0.2, 450, True),
    ("", "System services"): (0.08, 700, False),
    ("", "Kernel"): (0.05, 0, False),
}


# The page measures memory against this host's own total, so the made-up free memory starts from it too.
TOTAL_MB = resources.memory_total_mb()


def readings(bucket: int) -> resources.Taken:
    """One ten-minute slot's totals: each part at its share of the day's rhythm."""
    hour = (bucket % 86400) / 3600
    working = max(0.0, math.sin((hour - 6) / 14 * math.pi)) if 6 <= hour <= 20 else 0.0
    use = {}
    for (session, part), (cores, mem_mb, daytime) in PARTS.items():
        level = working if daytime else 0.6 + 0.4 * working
        wobble = 0.8 + 0.4 * ((bucket // 600 * 7 + len(part)) % 10) / 10
        cpu = cores * level * wobble * 600
        mem = mem_mb * (0.3 + 0.7 * level) * 600
        idle = 0.2 if part == "Claude agent" else 0.0
        use[(bucket, session, part)] = resources.Totals(
            cpu_s=cpu, idle_cpu_s=cpu * idle, mem_mb_s=mem, idle_mem_mb_s=mem * idle,
            session_peak_mb=mem / 600 * 1.3, part_peak_mb=mem / 600 * 1.3, programs=3)
    busy = sum(t.cpu_s for t in use.values())
    used = sum(t.mem_mb_s for t in use.values())
    host = {bucket: resources.Host(sampled_s=600, busy_s=busy * 1.05, load_s=busy * 1.4, avail_mb_s=TOTAL_MB * 600 - used * 1.6,
                                   cpu_wait_s=busy * 0.05, mem_wait_s=0)}
    return resources.Taken(use, host, [])


def seed(path: Path) -> None:
    """The board, then eight days of readings up to the current ten minutes, and what runs right now."""
    board(path)
    now = datetime.now(UTC)
    last = int(now.timestamp()) // 600 * 600
    with db.connect(path) as conn:
        for bucket in range(last - 8 * 86400, last + 1, 600):
            resources.write(conn, readings(bucket), now)
        db.kv_set(conn, resources.NOW_KEY, {"at": now.isoformat().replace("+00:00", "Z"), "tasks": [
            {"session_id": SESSION_A, "cores": 1.4, "mem_mb": 2900,
             "parts": {"Python tests": 1.1, "Headless browser": 0.2, "Claude agent": 0.1}},
            {"session_id": "", "cores": 0.3, "mem_mb": 1000, "parts": {"Cockpit server": 0.2, "System services": 0.1}},
        ]})


serve_slow.seed_board = seed

if __name__ == "__main__":
    serve_slow.main(Path(sys.argv[1]).resolve())
