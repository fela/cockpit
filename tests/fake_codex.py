#!/usr/bin/env python3
"""A stand-in for `codex login --device-auth`, which deletes CODEX_HOME's auth.json and prints Codex's link and code.

It waits for CODEX_HOME/finish: its text is the address to sign in, written to auth.json, or "fail" to end as a code that
ran out. It refuses the real account folders, since the point of a stand-in is that no test signs them out.
"""

import base64
import json
import os
import sys
import time
from pathlib import Path

CODE = "ABCD-EFGH"


def part(value: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()


def main() -> int:
    home = Path(os.environ["CODEX_HOME"]).resolve()
    # The default paths stay reserved when absent; discovered folders and their link targets are reserved too.
    user_home = Path.home()
    reserved = {user_home / ".codex", user_home / ".codex-2", *user_home.glob(".codex*")}
    if home in {folder.resolve() for folder in reserved}:
        print("error: the stand-in refuses a real account folder")
        return 2
    assert sys.argv[1:] == ["login", "--device-auth"], sys.argv
    (home / "auth.json").unlink(missing_ok=True)
    if (home / "no-link").exists():
        print("Error: could not reach OpenAI", flush=True)
        return 1
    print(
        "\n\x1b[1mWelcome to Codex\x1b[0m\n\n1. Open this link in your browser and sign in to your account\n"
        "   \x1b[94mhttps://auth.openai.com/codex/device\x1b[0m\n\n2. Enter this one-time code \x1b[90m(expires in 15 minutes)\x1b[0m\n"
        f"   \x1b[94m{CODE}\x1b[0m\n\nContinue only if you started this login in Codex.\n\n",
        flush=True,
    )
    finish = home / "finish"
    # The file appears empty first and gets its text a moment later: wait for the text.
    while not (finish.exists() and (said := finish.read_text().strip())):
        time.sleep(0.05)
    finish.unlink()
    if said == "fail":
        print("Error logging in with device code: device auth timed out after 15 minutes", flush=True)
        return 1
    token = f"{part({'alg': 'none'})}.{part({'email': said})}.x"
    (home / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"id_token": token}}))
    print("Successfully logged in", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
