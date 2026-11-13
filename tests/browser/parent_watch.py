"""Ends a browser test's server together with the test that started it, and leaves nothing of its run behind.

A test stopped before its own cleanup runs (SIGTERM to the test alone, a crash) would leave its server running,
holding its database copy, and such copies add up on the disk. With stop_with_parent the server sends itself SIGTERM
within a second of the process that started it ending, and after LIFETIME_HOURS at the latest. SIGTERM and SIGHUP
raise SystemExit, so the server's `finally` runs; there `stopped` ignores any further signal, and when the test has
ended it clears the work folder as work.cjs would, since the server may have written to it after the test's own
cleanup ran."""

from __future__ import annotations

import os
import shutil
import signal
import sys
import threading
import time
from pathlib import Path

# The server exits on its own this long after it starts. A session starts a preview as a background command, and
# stopping the session leaves the command running. A test killed at once (SIGKILL) before its server read its parent
# leaves the server watching the process that adopted it, so this also bounds that case.
LIFETIME_HOURS = 6
STOPS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)


def stop(signum: int, frame: object) -> None:
    sys.exit(128 + signum)


def stop_with_parent(work: Path) -> int:
    """Starts the watch and returns the parent's pid, for `stopped`."""
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, stop)
    parent = os.getppid()
    # The test makes the work folder before it starts the server and removes it as it ends (work.cjs): a test that
    # ended before `parent` was read leaves no folder, and the server must not serve into one it would remake.
    if not work.is_dir():
        sys.exit(f"{work} is gone: the test that started this server has ended")

    def watch() -> None:
        while os.getppid() == parent:
            time.sleep(1)
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=watch, daemon=True).start()
    lifetime = threading.Timer(LIFETIME_HOURS * 3600, os.kill, args=(os.getpid(), signal.SIGTERM))
    lifetime.daemon = True
    lifetime.start()
    return parent


def stopped(work: Path, parent: int) -> None:
    """Called first in the server's `finally`, after uvicorn has handed the signals back."""
    for signum in STOPS:
        signal.signal(signum, signal.SIG_IGN)
    if os.getppid() == parent or not work.is_dir():
        return
    # The test has ended, so its exit handler has already run: what is here now, the server wrote after it.
    kept = False
    for entry in work.iterdir():
        if entry.name.endswith(".png"):
            kept = True
        elif entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)
    if not kept:
        shutil.rmtree(work, ignore_errors=True)
