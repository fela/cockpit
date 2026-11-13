from __future__ import annotations

import fnmatch
import inspect
import json
import re
from pathlib import Path

import pytest

from cockpit import cli
from cockpit.attribution import refusal
from cockpit.standing_rules import (
    ALLOW,
    COCKPIT_COMMANDS,
    COCKPIT_COMMANDS_LEFT_TO_AUTO_MODE,
    HOOKED_BASH,
    install,
    with_rules,
)

HOOK = Path("/venv/bin/cockpit-attribution-hook")
SETTINGS = {
    "permissions": {
        "allow": ["Bash(gh pr edit:*)", "Bash(cockpit get *)", "EnterWorktree"],
        "deny": [],
        "ask": ["Bash(git push *)"],
        "defaultMode": "auto",
    },
    "autoMode": {"allow": ["$defaults", "Writing step drafts in a session's job folder is fine."]},
    "hooks": {"PermissionDenied": [{"hooks": [{"type": "command", "command": "cockpit permission report"}]}]},
}


def test_adds_the_rules_and_keeps_everything_else() -> None:
    settings = with_rules(SETTINGS, HOOK)
    allow = settings["permissions"]["allow"]
    assert allow[:3] == SETTINGS["permissions"]["allow"]
    assert allow[3:] == [rule for rule in ALLOW if rule != "Bash(cockpit get *)"]
    assert {key: settings["permissions"][key] for key in ("deny", "ask", "defaultMode")} == {
        key: SETTINGS["permissions"][key] for key in ("deny", "ask", "defaultMode")
    }
    assert settings["autoMode"] == SETTINGS["autoMode"]
    assert settings["hooks"]["PermissionDenied"] == SETTINGS["hooks"]["PermissionDenied"]
    assert SETTINGS["permissions"]["allow"] == ["Bash(gh pr edit:*)", "Bash(cockpit get *)", "EnterWorktree"]


def test_the_hook_checks_every_comment_and_review() -> None:
    groups = with_rules(SETTINGS, HOOK)["hooks"]["PreToolUse"]
    checked = {(group["matcher"], handler.get("if")) for group in groups for handler in group["hooks"]}
    assert checked == {
        ("Bash", "Bash(gh *)"),
        ("Bash", "Bash(/usr/bin/gh *)"),
        ("Bash", "Bash(/bin/gh *)"),
        ("Bash", "Bash(bash *)"),
        ("Bash", "Bash(sh *)"),
        ("Bash", "Bash(zsh *)"),
        ("Bash", "Bash(eval *)"),
        *(
            ("Bash", f"Bash({wrapper} *)")
            for wrapper in (
                "timeout", "env", "xargs", "nohup", "nice", "command", "time", "sudo", "exec", "setsid", "stdbuf",
            )
        ),
        ("mcp__claude_ai_Linear__save_comment", None),
        ("mcp__linear__save_comment", None),
        *(
            (f"{server}__slack_{tool}", None)
            for server in ("mcp__claude_ai_Slack", "mcp__slack")
            for tool in ("send_message", "schedule_message", "send_message_draft", "complete_file_upload")
        ),
        ("Artifact", None),
    }
    assert all(h["command"] == str(HOOK) and h["onFailure"] == "block" for g in groups for h in g["hooks"])


@pytest.mark.parametrize(
    "command",
    [
        "sudo -u sam gh pr comment 10 --body hi",
        "exec gh pr comment 10 --body hi",
        "setsid gh pr comment 10 --body hi",
        "stdbuf -oL gh pr comment 10 --body hi",
        "/usr/bin/gh pr comment 10 --body hi",
        "/bin/gh pr comment 10 --body hi",
    ],
)
def test_a_wrapped_post_is_hooked_and_refused(command: str) -> None:
    # fnmatch stands in for Claude Code's match of an `if` pattern, whose `*` takes the rest of the command.
    assert any(fnmatch.fnmatchcase(f"Bash({command})", pattern) for pattern in HOOKED_BASH)
    assert "first line" in refusal({"tool_name": "Bash", "tool_input": {"command": command}})


def test_no_rule_for_a_command_that_reads_or_writes_a_given_file() -> None:
    for rule in ALLOW:
        assert not rule.startswith(("Bash(cat", "Bash(grep", "Bash(sed", "Bash(head", "Bash(tail", "Bash(echo"))
        assert not rule.startswith(("Bash(git diff", "Bash(git log", "Bash(git show"))
        assert not rule.startswith(("Bash(cockpit *", "Bash(cockpit spec", "Bash(cockpit image", "Bash(cockpit page"))


def test_github_posts_go_through_auto_mode() -> None:
    assert all(not rule.startswith(("Bash(gh pr comment", "Bash(gh issue comment", "Bash(gh pr review")) for rule in ALLOW)


SLACK_TOOLS_THAT_READ = [
    "read_canvas", "read_channel", "read_file", "read_list", "read_thread", "read_user_profile", "search_channels",
    "search_emojis", "search_public", "search_public_and_private", "search_users", "list_channel_members",
    "list_user_channels", "get_reactions",
]  # fmt: skip
SLACK_TOOLS_THAT_WRITE = [
    "send_message", "schedule_message", "send_message_draft", "complete_file_upload", "get_file_upload_url",
    "add_reaction", "create_canvas", "update_canvas", "create_conversation", "create_list", "update_list",
    "add_list_record", "update_list_record",
]  # fmt: skip


def test_slack_reads_are_allowed_and_slack_writes_go_through_auto_mode() -> None:
    for server in ("mcp__claude_ai_Slack", "mcp__slack"):
        rules = [rule for rule in ALLOW if rule.startswith(f"{server}__")]
        for tool in SLACK_TOOLS_THAT_READ:
            assert any(fnmatch.fnmatchcase(f"{server}__slack_{tool}", rule) for rule in rules), tool
        for tool in SLACK_TOOLS_THAT_WRITE:
            assert not any(fnmatch.fnmatchcase(f"{server}__slack_{tool}", rule) for rule in rules), tool


def test_every_cockpit_command_is_allowed_or_left_to_auto_mode() -> None:
    """A new cockpit command gets a rule only once someone checks that it reads and writes no file it is given."""
    commands = re.findall(r'(?<!\w)commands\.add_parser\(\s*"([\w-]+)"', inspect.getsource(cli.main))
    assert sorted(commands) == sorted(COCKPIT_COMMANDS + COCKPIT_COMMANDS_LEFT_TO_AUTO_MODE)




def test_applying_twice_changes_nothing() -> None:
    once = with_rules(SETTINGS, HOOK)
    assert with_rules(once, HOOK) == once


def test_install_writes_once_and_keeps_the_first_version(tmp_path: Path) -> None:
    hook = tmp_path / "cockpit-attribution-hook"
    hook.write_text("")
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(SETTINGS, indent=2) + "\n")
    diff = install(path, hook)
    assert '+      "Bash(cockpit step *)",' in diff
    assert json.loads(path.read_text()) == with_rules(SETTINGS, hook)
    assert json.loads((tmp_path / "settings.json.before-standing-rules").read_text()) == SETTINGS
    assert "nothing changed" in install(path, hook)


def test_install_refuses_without_the_hook(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(SETTINGS))
    with pytest.raises(SystemExit, match="not installed"):
        install(path, tmp_path / "cockpit-attribution-hook")
    assert json.loads(path.read_text()) == SETTINGS
