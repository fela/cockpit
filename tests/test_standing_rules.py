from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import pytest

from cockpit import cli
from cockpit.standing_rules import ALLOW, COCKPIT_COMMANDS, COCKPIT_COMMANDS_LEFT_TO_AUTO_MODE, install, with_rules

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
        ("Bash", "Bash(bash *)"),
        ("Bash", "Bash(sh *)"),
        ("Bash", "Bash(zsh *)"),
        ("Bash", "Bash(eval *)"),
        *(("Bash", f"Bash({wrapper} *)") for wrapper in ("timeout", "env", "xargs", "nohup", "nice", "command", "time")),
        ("mcp__claude_ai_Linear__save_comment", None),
        ("Artifact", None),
    }
    assert all(h["command"] == str(HOOK) and h["onFailure"] == "block" for g in groups for h in g["hooks"])


def test_no_rule_for_a_command_that_reads_or_writes_a_given_file() -> None:
    for rule in ALLOW:
        assert not rule.startswith(("Bash(cat", "Bash(grep", "Bash(sed", "Bash(head", "Bash(tail", "Bash(echo"))
        assert not rule.startswith(("Bash(git diff", "Bash(git log", "Bash(git show"))
        assert not rule.startswith(("Bash(cockpit *", "Bash(cockpit spec", "Bash(cockpit image", "Bash(cockpit page"))


def test_github_posts_go_through_auto_mode() -> None:
    assert all(not rule.startswith(("Bash(gh pr comment", "Bash(gh issue comment", "Bash(gh pr review")) for rule in ALLOW)


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
