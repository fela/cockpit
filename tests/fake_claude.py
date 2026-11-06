"""A stand-in for `claude auth login --claudeai`: `fake_claude.py DEFAULT_FOLDER auth login --claudeai`.

It prints Claude's link as a terminal hyperlink and reads the code from its terminal, as Claude Code does. The code
that signs in is GOOD_CODE; it then writes a fresh sign-in to the account's folder: CLAUDE_CONFIG_DIR, or
DEFAULT_FOLDER when that is unset. Any other code fails the way Claude's does, and like Claude the process then
keeps running.
"""

import json
import os
import sys
import time
from pathlib import Path

GOOD_CODE = "good-code#state"
LINK = "https://claude.com/cai/oauth/authorize?code=true&client_id=x&state=s"


def main() -> None:
    folder = Path(os.environ.get("CLAUDE_CONFIG_DIR") or sys.argv[1])
    if sys.argv[2:] != ["auth", "login", "--claudeai"]:
        sys.exit(f"fake_claude.py does not know {sys.argv[2:]}")
    print("Opening browser to sign in…", end="\r\n")
    print(f"If the browser didn't open, visit: \x1b]8;;{LINK}\x07\x1b[94m{LINK}\x1b[39m\x1b]8;;\x07", end="\r\n")
    print("Paste code here if prompted > ", end="", flush=True)
    code = sys.stdin.readline().strip()
    if code != GOOD_CODE:
        print("Login failed: Request failed with status code 400", end="\r\n", flush=True)
        time.sleep(600)
        return
    folder.mkdir(parents=True, exist_ok=True)
    expires = int((time.time() + 30 * 86400) * 1000)
    oauth = {"accessToken": "fresh", "refreshToken": "fresh", "refreshTokenExpiresAt": expires}
    (folder / ".credentials.json").write_text(json.dumps({"claudeAiOauth": oauth}))
    print("Login successful.", end="\r\n", flush=True)


if __name__ == "__main__":
    main()
