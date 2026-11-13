"""How loaded the host is, for the header's rack beside the weekly limits (SPEC UI-69, UI-73).

Three readings, each green, orange or red in the pace words the usage bars use (`under`, `near`, `over`), or dark
yellow (`busy`) for the processor; the hover card colours each line by its level. The rack itself takes one colour
on a smooth green-to-orange scale from the raw figures (app.js machineStrain), so these levels do not set it:

- Processor: the 1-minute load average (jobs running or waiting for a core) against the machine's cores. Below
  two jobs per core it is green; from two dark yellow, as each job waits about as long as it runs. A busy
  processor only slows work down, and a machine running several sessions is often that busy, so it stops at dark
  yellow; orange and red are left for memory and disk, which stop work when they run out.
- Memory: the share in use, from the kernel's estimate of what new work can still get (`MemAvailable`). Orange
  from 80 %, red from 90 %.
- Disk: the share of the root disk in use. Orange from 85 %, red from 95 %.

The numbers are rounded, so the header redraws only when one moves visibly.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

GB = 1024**3
# From best to worst: the overall level is the worst of the readings.
LEVELS = ("under", "busy", "near", "over")


def level(share: float, near: float, over: float) -> str:
    if share >= over:
        return "over"
    if share >= near:
        return "near"
    return "under"


def measure(loadavg: str, meminfo: str, cores: int, disk_total: int, disk_free: int) -> dict[str, Any]:
    """The readings from /proc/loadavg's and /proc/meminfo's text, the core count and the root disk's bytes."""
    load = float(loadavg.split()[0])
    kib = {name: int(rest.split()[0]) for name, rest in (line.split(":", 1) for line in meminfo.splitlines() if ":" in line)}
    memory_total = kib["MemTotal"] * 1024
    memory_used = memory_total - kib["MemAvailable"] * 1024
    swap_used = (kib["SwapTotal"] - kib["SwapFree"]) * 1024
    readings = {
        "cpu": "busy" if load / cores >= 2 else "under",
        "memory": level(memory_used / memory_total, 0.8, 0.9),
        "disk": level((disk_total - disk_free) / disk_total, 0.85, 0.95),
    }
    return {
        "cores": cores,
        "load": round(load, 1),
        "memory_used_gb": round(memory_used / GB, 1),
        "memory_total_gb": round(memory_total / GB),
        "swap_used_gb": round(swap_used / GB, 1),
        "disk_free_gb": round(disk_free / GB),
        "disk_total_gb": round(disk_total / GB),
        **readings,
        "level": max(readings.values(), key=LEVELS.index),
    }


def read() -> dict[str, Any]:
    """The host now. A reading that fails shows its error on the rack, and the rest of the page still loads."""
    if not Path("/proc/loadavg").exists():
        return {"unavailable": "Load readings are not available on this system"}
    try:
        disk = os.statvfs("/")
        return measure(
            Path("/proc/loadavg").read_text(), Path("/proc/meminfo").read_text(), os.cpu_count() or 1,
            disk.f_blocks * disk.f_frsize, disk.f_bavail * disk.f_frsize,
        )
    except (OSError, ValueError, KeyError, IndexError, ZeroDivisionError) as error:
        log.error("reading the host's load failed: %s", error)
        return {"error": str(error)}
