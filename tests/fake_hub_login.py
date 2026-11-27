"""A stand-in for the hub's Codex device login, run over SSH: prints OpenAI's link and code as CLIProxyAPI does, then
waits for `finish` (signed in, exit 0) or `fail` (OpenAI refused, also exit 0 as CLIProxyAPI's logins do) to appear in the folder it is given."""

from __future__ import annotations

import sys
import time
from pathlib import Path

folder = Path(sys.argv[1])
print("Starting Codex device authentication...", flush=True)
if (folder / "no-link").exists():
    print("error: device authorization failed: 403", flush=True)
    sys.exit(1)
print("Codex device URL: https://auth.openai.com/codex/device", flush=True)
print("Codex device code: WXYZ-1234", flush=True)
while True:
    if (folder / "finish").exists():
        print("Codex authentication successful! Saved to codex-me@example.com-plus.json", flush=True)
        sys.exit(0)
    if (folder / "fail").exists():
        print("error: codex device authentication failed: code WXYZ-1234 expired", flush=True)
        sys.exit(0)
    time.sleep(0.02)
