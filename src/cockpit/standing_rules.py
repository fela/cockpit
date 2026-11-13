"""Permission rules for Claude Code sessions, kept in git (SPEC PERM-12).

`cockpit-standing-rules` adds missing rules and the attribution hook to the user's
settings. Matching calls skip auto mode, so the rules cover Cockpit commands without
file arguments, git status, GitHub pull-request reads, Linear reads, Linear comments and Slack reads.
File reads, GitHub comments and reviews, and Slack posts and edits go through auto mode.

The attribution hook checks GitHub posts on every repository, Linear comments and Slack posts.
It also checks shell wrappers, reads comment body files, and refuses Artifact
publishes so shared HTML pages go through `cockpit page`.

The installer preserves other settings and refuses to write if its hook is missing.
"""

from __future__ import annotations

import copy
import difflib
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from cockpit import config, permissions
from cockpit.attribution import SLACK_POSTS

# The cockpit commands sessions use, none of which takes a file path.
COCKPIT_COMMANDS = [
    "get", "item", "step", "send", "sweep", "feedback", "goals", "patterns", "message", "permission", "errors",
    "briefing", "writing", "progress", "flashes", "project", "inbox-settings", "ui-usage", "schedule",
    "signin",
]  # fmt: skip
# `spec`, `image` and `page` read or write any path they are given, and `spec put` and `spec edit` commit the
# file they read to Cockpit's GitHub repository. `attach` opens a tmux window for the page's link. `session close`
# stops another session's process.
# `logs`, `presses` and `requests` are not yet checked for the file paths they take. `activity` reads the user's Mac
# activity, which stays behind a prompt until the user allows it. A command a part adds (cockpit.parts) gets no rule
# here, so it is left to auto mode as well.
COCKPIT_COMMANDS_LEFT_TO_AUTO_MODE = [
    "spec", "image", "page", "attach", "session", "top", "time", "logs", "presses", "requests", "activity",
]  # fmt: skip
# Linear's tools come from the claude.ai connector, which only sessions signed in to that Claude account have, or from
# Linear's MCP server added to the machine as `linear`, which every account on the machine shares.
LINEAR_SERVERS = ("mcp__claude_ai_Linear", "mcp__linear")
LINEAR_COMMENTS = tuple(f"{server}__save_comment" for server in LINEAR_SERVERS)
# Slack's tools come from the same two sources: the claude.ai connector, or Slack's MCP server added as `slack`.
SLACK_SERVERS = ("mcp__claude_ai_Slack", "mcp__slack")
# Slack's tools that only read: channels, threads, files, canvases, lists, profiles, searches and reactions.
# Not `slack_get_*`: `slack_get_file_upload_url` starts an upload.
SLACK_READS = ("slack_read_*", "slack_search_*", "slack_list_*", "slack_get_reactions")
ALLOW = [
    *(f"Bash(cockpit {command} *)" for command in COCKPIT_COMMANDS),
    # Not git diff, log or show: they write the file --output names, and `git diff --no-index` prints any file.
    "Bash(git status *)",
    "Bash(gh pr view *)",
    "Bash(gh pr checks *)",
    *(f"{server}__{tool}" for server in LINEAR_SERVERS for tool in ("get_*", "list_*", "save_comment")),
    *(f"{server}__{tool}" for server in SLACK_SERVERS for tool in SLACK_READS),
]
HOOK_NAME = "cockpit-attribution-hook"
# The calls the hook checks; `if` narrows a Bash handler to one command, written as an allow rule is. Every gh call,
# by name or by the paths it has on this machine, since gh takes options before `comment` (`gh pr -R x comment`),
# every shell or eval, whose script may post, and every program that starts another, which may be gh
# (`timeout 30 gh`, `sudo gh`). The hook delays each call it checks, so it stays off other calls.
HOOKED_BASH = [
    f"Bash({program} *)"
    for program in (
        "gh", "/usr/bin/gh", "/bin/gh", "bash", "sh", "zsh", "eval", "timeout", "env", "xargs", "nohup", "nice",
        "command", "time", "sudo", "exec", "setsid", "stdbuf",
    )
]
# Sessions share HTML pages on Cockpit (`cockpit page`); the hook refuses an Artifact publish (SPEC API-3).
ARTIFACT = "Artifact"


def _handler(hook: Path, condition: str | None) -> dict[str, Any]:
    entry: dict[str, Any] = {"type": "command", "command": str(hook), "timeout": 10, "onFailure": "block"}
    if condition:
        entry["if"] = condition
    return entry


def with_rules(settings: dict[str, Any], hook: Path) -> dict[str, Any]:
    """`settings` with the standing rules and the hook added; every other entry stays as it is."""
    settings = copy.deepcopy(settings)
    allow = settings.setdefault("permissions", {}).setdefault("allow", [])
    allow.extend(rule for rule in ALLOW if rule not in allow)
    groups = settings.setdefault("hooks", {}).setdefault("PreToolUse", [])
    # The hook's groups are replaced whole, so a change to them here reaches the file.
    groups[:] = [group for group in groups if all(h.get("command") != str(hook) for h in group.get("hooks", []))]
    groups.append({"matcher": "Bash", "hooks": [_handler(hook, condition) for condition in HOOKED_BASH]})
    groups.extend({"matcher": tool, "hooks": [_handler(hook, None)]} for tool in (*LINEAR_COMMENTS, *SLACK_POSTS))
    groups.append({"matcher": ARTIFACT, "hooks": [_handler(hook, None)]})
    return settings


def install(path: Path, hook: Path) -> str:
    """Write the standing rules into the settings file at `path`, keeping its first version next
    to it; returns what changed, as a diff."""
    if not hook.is_file():
        raise SystemExit(f"{hook} is not installed, so every comment it checks would be refused. Nothing changed.")
    old = permissions._read(path)
    new = with_rules(old, hook)
    if new == old:
        return f"{path} already holds the standing rules; nothing changed."
    backup = path.with_name(path.name + ".before-standing-rules")
    if path.exists() and not backup.exists():
        shutil.copy2(path, backup)
    permissions._write(path, new)
    diff = difflib.unified_diff(
        json.dumps(old, indent=2, ensure_ascii=False).splitlines(),
        json.dumps(new, indent=2, ensure_ascii=False).splitlines(),
        str(backup),
        str(path),
        lineterm="",
    )
    return "\n".join(diff)


def main() -> None:
    print(install(config.CLAUDE_SETTINGS, Path(sys.executable).parent / HOOK_NAME))
