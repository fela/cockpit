"""A stand-in for the hub's Claude login, run over SSH: prints Claude's link as CLIProxyAPI does, then reads the pasted
address from its input. It echoes the line as the hub's terminal does, signs in (exit 0) when the address carries the
link's state and a code other than `refused` or `unsaved`, and otherwise fails, exiting 0 as CLIProxyAPI's logins do. An
`unsaved` code fails after the first success line, as a failed save does. With `no-link` in the folder it fails before
the link (exit 1)."""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

STATE = "state-0123456789abcdefghij"
folder = Path(sys.argv[1])
if (folder / "no-link").exists():
    print("error: claude authentication failed: could not start the callback server", flush=True)
    sys.exit(1)
print("Visit the following URL to continue authentication:", flush=True)
print(
    "https://claude.ai/oauth/authorize?code=true&client_id=client-1&response_type=code"
    f"&redirect_uri=http%3A%2F%2Flocalhost%3A54545%2Fcallback&scope=user%3Ainference&state={STATE}",
    flush=True,
)
print("Paste the Claude callback URL (or press Enter to keep waiting):", flush=True)
line = sys.stdin.readline()
print(line, end="", flush=True)
fields = parse_qs(urlsplit(line.strip()).query)
if fields.get("state") != [STATE]:
    print("error: claude authentication failed: state mismatch", flush=True)
    sys.exit(0)
code = fields["code"][0]
if code == "refused" or code.startswith("refused"):
    print(f"error: claude token exchange failed for code {code}: invalid_grant", flush=True)
    sys.exit(0)
print("Claude authentication successful", flush=True)
if code.startswith("unsaved"):
    print("Claude authentication failed: failed to save auth: disk full", flush=True)
    sys.exit(0)
print("Claude authentication successful! Saved to claude-you@example.com.json", flush=True)
