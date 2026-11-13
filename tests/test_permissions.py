from __future__ import annotations

import json
from pathlib import Path

import pytest

from cockpit import db, permissions, store, sweeper
from tests.conftest import World
from tests.test_api import agent_client

SENTENCE = "Running the cockpit CLI against the local Cockpit server is fine."


def settings_of(world: World) -> dict:
    return json.loads(world.claude_settings.read_text())


def add(world: World, list_name: str, rule: str):
    return world.client.post("/api/permissions/rules", json={"list": list_name, "rule": rule}, headers=world.user())


def remove(world: World, list_name: str, rule: str):
    return world.client.post("/api/permissions/rules/remove", json={"list": list_name, "rule": rule}, headers=world.user())


def test_lists_read_both_allow_lists(world: World) -> None:
    rules = agent_client(world).get("/api/permissions", headers=world.agent()).json()
    assert rules["mode"] == "auto"
    assert rules["allow"] == ["Bash(gh pr view *)"]
    assert rules["auto"] == ["Reading files anywhere under the home directory is fine."]
    assert rules["auto_keeps_defaults"] is True


def test_adding_and_removing_keeps_the_rest_of_the_file(world: World) -> None:
    response = add(world, "allow", "  Bash(cockpit get *) ")
    assert response.status_code == 200, response.text
    assert response.json()["rule"] == "Bash(cockpit get *)"
    assert response.json()["added"] is True
    assert add(world, "auto", SENTENCE).status_code == 200
    settings = settings_of(world)
    assert settings["model"] == "opus"
    assert settings["permissions"]["ask"] == ["Bash(git push *)"]
    assert settings["permissions"]["allow"] == ["Bash(gh pr view *)", "Bash(cockpit get *)"]
    assert settings["autoMode"]["allow"][0] == "$defaults"
    assert settings["autoMode"]["allow"][-1] == SENTENCE
    assert world.claude_settings.stat().st_mode & 0o777 == 0o600

    assert add(world, "allow", "Bash(cockpit get *)").json()["added"] is False
    assert settings_of(world)["permissions"]["allow"].count("Bash(cockpit get *)") == 1

    assert remove(world, "allow", "Bash(cockpit get *)").status_code == 200
    assert remove(world, "auto", SENTENCE).json()["auto"] == ["Reading files anywhere under the home directory is fine."]
    assert remove(world, "allow", "Bash(cockpit get *)").status_code == 404
    assert remove(world, "auto", "$defaults").status_code == 404
    assert "$defaults" in settings_of(world)["autoMode"]["allow"]


def test_a_rule_is_changed_where_it_stands(world: World) -> None:
    # SPEC PERM-11: a rule that is almost right is changed in place, rather than joined by another.
    add(world, "allow", "Bash(cockpit get *)")
    changed = permissions.replace(world.claude_settings, "allow", "Bash(gh pr view *)", " Bash(gh pr view 10) ")
    assert changed == ("Bash(gh pr view 10)", True)
    # The same change again, as an answer sent again after it failed: nothing changes.
    assert permissions.replace(world.claude_settings, "allow", "Bash(gh pr view *)", "Bash(gh pr view 10)") == ("Bash(gh pr view 10)", False)
    settings = settings_of(world)
    assert settings["permissions"]["allow"] == ["Bash(gh pr view 10)", "Bash(cockpit get *)"]
    assert settings["model"] == "opus"
    assert settings["permissions"]["ask"] == ["Bash(git push *)"]
    for old, new, error in (
        ("Bash(gone *)", "Bash(here *)", store.NotFound),
        ("Bash(cockpit get *)", "Bash(cockpit get *)", store.Invalid),
        ("Bash(cockpit get *)", "Bash(gh pr view 10)", store.Invalid),
        ("Bash(cockpit get *)", "Bash(*)", store.Invalid),
    ):
        with pytest.raises(error):
            permissions.replace(world.claude_settings, "allow", old, new)
    with pytest.raises(store.NotFound):
        permissions.replace(world.claude_settings, "auto", "$defaults", SENTENCE)
    assert settings_of(world) == settings


def test_undo_changes_a_rule_back(world: World) -> None:
    add(world, "allow", "Bash(cockpit get *)")
    body = {"list": "allow", "old": "Bash(gh pr view *)", "rule": "Bash(gh pr view 10)"}
    assert agent_client(world).post("/api/permissions/rules/edit", json=body, headers=world.agent()).status_code == 403
    response = world.client.post("/api/permissions/rules/edit", json=body, headers=world.user())
    assert response.status_code == 200, response.text
    assert response.json()["allow"] == ["Bash(gh pr view 10)", "Bash(cockpit get *)"]
    back = {"list": "allow", "old": "Bash(gh pr view 10)", "rule": "Bash(gh pr view *)"}
    assert world.client.post("/api/permissions/rules/edit", json=back, headers=world.user()).json()["allow"] == [
        "Bash(gh pr view *)", "Bash(cockpit get *)",
    ]
    gone = {"list": "allow", "old": "Bash(gh pr view 10)", "rule": "Bash(gh pr view 20)"}
    assert world.client.post("/api/permissions/rules/edit", json=gone, headers=world.user()).status_code == 404
    # The same change sent again finds it made; the same rules reached by a remove and an add are no such change.
    assert world.client.post("/api/permissions/rules/edit", json=back, headers=world.user()).status_code == 200
    world.client.post("/api/permissions/rules/remove", json={"list": "allow", "rule": "Bash(cockpit get *)"}, headers=world.user())
    add(world, "allow", "Bash(cockpit get 1)")
    other = {"list": "allow", "old": "Bash(cockpit get *)", "rule": "Bash(cockpit get 1)"}
    assert world.client.post("/api/permissions/rules/edit", json=other, headers=world.user()).status_code == 404
    # Nor after an edit long gone: changed, changed back, then the same rules reached by a remove and an add.
    world.client.post("/api/permissions/rules/edit", json=body, headers=world.user())
    world.client.post("/api/permissions/rules/edit", json=back, headers=world.user())
    world.client.post("/api/permissions/rules/remove", json={"list": "allow", "rule": "Bash(gh pr view *)"}, headers=world.user())
    add(world, "allow", "Bash(gh pr view 10)")
    assert world.client.post("/api/permissions/rules/edit", json=body, headers=world.user()).status_code == 404


def test_first_sentence_keeps_the_built_in_ones(world: World) -> None:
    world.claude_settings.write_text("{}\n")
    assert agent_client(world).get("/api/permissions", headers=world.agent()).json()["auto_keeps_defaults"] is True
    add(world, "auto", SENTENCE)
    assert settings_of(world)["autoMode"]["allow"] == ["$defaults", SENTENCE]


def test_bad_rules_are_refused(world: World) -> None:
    for rule in ("Bash", "Bash(*)", "rm -rf /", "Bash(", "(Bash)"):
        assert add(world, "allow", rule).status_code == 400, rule
    for sentence in ("short", "two\nlines are not one sentence", "$defaults"):
        assert add(world, "auto", sentence).status_code == 400, sentence
    assert settings_of(world)["permissions"]["allow"] == ["Bash(gh pr view *)"]


def test_only_user_changes_rules(world: World) -> None:
    for path in ("/api/permissions/rules", "/api/permissions/rules/remove"):
        response = agent_client(world).post(path, json={"list": "allow", "rule": "Bash(gh pr view *)"}, headers=world.agent())
        assert response.status_code == 403
    assert settings_of(world)["permissions"]["allow"] == ["Bash(gh pr view *)"]


@pytest.mark.parametrize(
    "words",
    [
        ["gh", "api", "--method=POST", "repos/o/r/issues/1/comments", "--raw-field=body=hi"],
        ["gh", "api", "-XPOST", "repos/o/r/issues/1/comments"],
        ["gh", "api", "repos/o/r/issues/1/comments", "-fbody=hi"],
        ["gh", "api", "-ifbody=hi", "repos/o/r/issues/1/comments"],
        ["gh", "api", "-iXPOST", "repos/o/r/issues/1/comments"],
        ["gh", "api", "repos/o/r/issues/1/comments", "--field=body=hi"],
        ["gh", "api", "repos/o/r/issues/1/comments", "--input=body.json"],
        ["sort", "-o", "/home/sam/x.txt", "a.txt"],
        ["sort", "-uo", "a.txt", "a.txt"],
        ["sort", "--output=/home/sam/x.txt", "a.txt"],
        ["date", "-s", "2020-01-01"],
        ["date", "--set=2020-01-01"],
        ["find", ".", "-fprintf", "/home/sam/out.txt", "%p"],
        ["find", ".", "-fprint0", "/home/sam/out.txt"],
        ["find", ".", "-fls", "/home/sam/out.txt"],
    ],
)
def test_a_command_that_writes_is_no_read(words: list[str]) -> None:
    assert not permissions._reads(words)


@pytest.mark.parametrize(
    "words",
    [
        ["gh", "api", "repos/o/r/pulls", "--jq", ".[].title"],
        ["gh", "api", "-i", "repos/o/r/pulls", "-q.[].files"],
        ["gh", "api", "-i=f", "repos/o/r/pulls"],
        ["sort", "-u", "a.txt"],
        ["date", "-Iseconds"],
        ["date", "+%s"],
        ["find", ".", "-name", "*.py", "-print0"],
    ],
)
def test_a_command_that_only_reads_is_a_read(words: list[str]) -> None:
    assert permissions._reads(words)


@pytest.mark.parametrize(
    "command",
    [
        "cd '/home/sam/dev/my app' && uv run pytest -q",
        'cd "$HOME/dev/my app"; uv run pytest -q',
        "cd -- /home/sam/dev/my\\ app && uv run pytest -q",
        "cd && cd '/home/sam/dev/my app' && uv run pytest -q",
    ],
)
def test_a_quoted_cd_names_the_whole_folder(command: str) -> None:
    sentence = "Sessions may change, build, test, commit and push code in ~/dev/my app and its worktrees, on their own branches."
    assert permissions.suggest_sentences("Bash", {"command": command}, "/home/sam/dev", Path("/home/sam")) == [sentence]


def test_rule_changes_are_recorded_and_leave_the_sweeper_asleep(world: World) -> None:
    add(world, "allow", "Bash(cockpit get *)")
    remove(world, "allow", "Bash(cockpit get *)")
    conn = db.connect(world.db_path)
    try:
        kinds = [row["kind"] for row in conn.execute("SELECT kind FROM events ORDER BY id")]
        assert kinds[-2:] == ["permission.rule_added", "permission.rule_removed"]
        assert set(kinds[-2:]) <= set(sweeper.QUIET_KINDS)
    finally:
        conn.close()
