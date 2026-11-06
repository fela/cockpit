"""Save Claude Code's status-line rate limits for Cockpit on macOS and Linux.

Run with the signed-in account's email as the only argument; JSON arrives on stdin.
The script reads only its own usage file, and accepts account identity explicitly.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


def capture(payload: dict[str, Any], email: str, destination: Path, config_dir: str) -> None:
    limits = payload.get("rate_limits")
    if limits is None:
        print("Claude usage not reported yet")
        return
    record = {
        "account": {"emailAddress": email}, "rate_limits": limits,
        "captured_at": time.time(), "session_id": payload["session_id"], "config_dir": config_dir,
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Sessions and accounts share this file. Lock the read/update and replace it atomically for readers.
    with destination.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            saved = json.loads(destination.read_text())
        except FileNotFoundError:
            saved = {"accounts": {}}
        saved["accounts"][email] = record
        with tempfile.NamedTemporaryFile(mode="w", dir=destination.parent, delete=False) as output:
            temporary = Path(output.name)
            try:
                json.dump(saved, output)
                output.close()
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
    words = [f"{label}: {limits[key]['used_percentage']:g}%" for key, label in
             (("five_hour", "5h"), ("seven_day", "7d")) if limits.get(key) is not None]
    print(" | ".join(words) or "Claude usage not reported yet")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email", help="email of the Claude account this status line runs for")
    args = parser.parse_args()
    folder = Path.home() / ".claude"
    capture(json.load(sys.stdin), args.email, folder / "rate-limits.json", os.environ.get("CLAUDE_CONFIG_DIR", str(folder)))


if __name__ == "__main__":
    main()
