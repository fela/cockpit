from __future__ import annotations

import io
import json
import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cockpit import cli, db, permissions, sweeper
from tests.conftest import SESSION_A, SESSION_B, World
from tests.test_api import agent_client, at_dialog
from tests.test_cli import route_cli_to

PUSH = {"command": "git push origin user/x", "description": "Push the branch"}
HOME = Path("/home/sam")


def report(
    world: World, kind: str, tool_input: dict, tool_name: str = "Bash", reason: str = "", cwd: str = "/home/sam/dev/app"
) -> dict:
    body = {
        "kind": kind, "session_id": SESSION_A, "cwd": cwd, "tool_name": tool_name,
        "tool_input": tool_input, "tool_use_id": "toolu_1", "reason": reason,
    }
    response = agent_client(world).post("/api/permissions/requests", json=body, headers=world.agent("permission-hook"))
    assert response.status_code == 200, response.text
    return response.json()


def answer(world: World, request_id: int, action: str, body: dict | None = None):
    return world.client.post(f"/api/permissions/requests/{request_id}/{action}", json=body, headers=world.user())


def state(world: World) -> dict:
    return world.client.get("/api/state", headers=world.user()).json()


def test_the_same_denied_call_in_another_folder_is_its_own_card(world: World) -> None:
    """A card names the folder its call runs in (SPEC PERM-26), so `rm .venv` refused in two checkouts is two calls."""
    first = report(world, "denied", {"command": "rm .venv"}, cwd="/home/sam/dev/a")
    other = report(world, "denied", {"command": "rm .venv"}, cwd="/home/sam/dev/b")
    again = report(world, "denied", {"command": "rm .venv"}, cwd="/home/sam/dev/a")
    assert other["id"] != first["id"] and other["cwd"] == "/home/sam/dev/b"
    assert again["id"] == first["id"] and again["count"] == 2


def test_a_repeated_denial_counts_up_and_waiting_dialogs_come_first(world: World) -> None:
    first = report(world, "denied", PUSH, reason="pushes to a shared branch")
    again = report(world, "denied", PUSH, reason="still a push")
    other = report(world, "denied", {"command": "rm -rf build"})
    asked = report(world, "asked", PUSH)
    assert again["id"] == first["id"]
    assert again["count"] == 2
    assert again["reason"] == "still a push"
    shown = state(world)
    ids = [entry["id"] for entry in shown["permission_requests"]]
    assert ids[0] == asked["id"]
    assert sorted(ids[1:]) == sorted([first["id"], other["id"]])
    assert shown["counts"]["blocked"] == 3
    entry = shown["permission_requests"][0]
    assert entry["summary"] == "git push origin user/x"
    assert entry["suggested_rules"] == [
        {"rule": "Bash(git push origin user/x)", "covers": "only this exact command"},
        {"rule": "Bash(git push origin *)", "covers": "every command that starts with git push origin"},
        {"rule": "Bash(git push *)", "covers": "every command that starts with git push"},
    ]
    assert entry["description"] == "Push the branch"
    assert entry["suggested_sentences"] == [
        "Sessions may change, build, test, commit and push code in /home/sam/dev/app, on their own branches.",
    ]
    assert (entry["changes"], entry["other_commands"], entry["covered_by"]) == ([], [], [])


def test_a_denied_call_runs_again_only_under_a_rule(world: World) -> None:
    request = report(world, "denied", PUSH)
    assert answer(world, request["id"], "allow", {"rule": None, "list": None}).status_code == 400
    assert answer(world, request["id"], "allow", {"rule": "Bash(git push *)", "list": None}).status_code == 400
    assert answer(world, request["id"], "deny").status_code == 409
    assert json.loads(world.claude_settings.read_text())["permissions"]["allow"] == ["Bash(gh pr view *)"]


def test_allowing_a_denied_call_adds_the_rule_and_tells_the_session_to_retry(world: World) -> None:
    stopped = world.cache.sessions[0]

    def resume(account, args, cwd):
        world.cache.sessions = [replace(stopped, pid=99)]  # the resumed process appears
        return world.runner(account, args, cwd)

    world.deliverer.run = resume
    world.runner.output = f"backgrounded · {SESSION_A[:8]}\n"
    request = report(world, "denied", PUSH)
    response = answer(world, request["id"], "allow", {"rule": "Bash(git push origin user/*)", "list": "allow"})
    assert response.status_code == 200, response.text
    allowed = response.json()
    assert (allowed["state"], allowed["rule"], allowed["rule_list"]) == ("allowed", "Bash(git push origin user/*)", "allow")
    settings = json.loads(world.claude_settings.read_text())
    assert settings["permissions"]["allow"] == ["Bash(gh pr view *)", "Bash(git push origin user/*)"]
    conn = db.connect(world.db_path)
    try:
        message = db.row(conn.execute("SELECT * FROM messages WHERE id = ?", (allowed["message_id"],)).fetchone())
        kinds = [row["kind"] for row in conn.execute("SELECT kind FROM events ORDER BY id")]
    finally:
        conn.close()
    assert message["author"] == "cockpit"
    assert message["session_id"] == SESSION_A
    assert "`Bash(git push origin user/*)`" in message["text"]
    assert message["text"].endswith("Retry the call if you still need it:\n\ngit push origin user/x")
    # The session has no live process in the fixture, so the message went out by resume.
    assert world.runner.calls and "--resume" in world.runner.calls[-1][0]
    assert kinds[-3:] == ["permission.rule_added", "permission.allowed", "message.sent"]
    assert {"permission.requested", "permission.rule_added", "permission.allowed"} <= set(sweeper.QUIET_KINDS)
    assert answer(world, request["id"], "allow", {"rule": "Bash(git push *)", "list": "allow"}).status_code == 409
    assert state(world)["permission_requests"] == []


def test_an_auto_mode_sentence_can_allow_a_denied_call(world: World) -> None:
    request = report(world, "denied", PUSH)
    sentence = "Pushing to my own user/ branches is fine."
    allowed = answer(world, request["id"], "allow", {"rule": sentence, "list": "auto"}).json()
    assert json.loads(world.claude_settings.read_text())["autoMode"]["allow"][-1] == sentence
    conn = db.connect(world.db_path)
    try:
        text = conn.execute("SELECT text FROM messages WHERE id = ?", (allowed["message_id"],)).fetchone()["text"]
    finally:
        conn.close()
    assert f'auto-mode sentence "{sentence}"' in text


def test_a_waiting_dialog_can_run_without_a_rule_or_be_denied(world: World) -> None:
    once = report(world, "asked", PUSH)
    allowed = answer(world, once["id"], "allow", {"rule": None, "list": None}).json()
    assert (allowed["state"], allowed["rule"], allowed["message_id"]) == ("allowed", None, None)
    assert json.loads(world.claude_settings.read_text())["permissions"]["allow"] == ["Bash(gh pr view *)"]
    assert agent_client(world).get(f"/api/permissions/requests/{once['id']}", headers=world.agent()).json()["state"] == "allowed"

    denied = report(world, "asked", {"command": "git push --force"})
    assert answer(world, denied["id"], "deny").json()["state"] == "denied"

    dismissed = report(world, "denied", {"command": "curl https://example.test"})
    assert answer(world, dismissed["id"], "dismiss").json()["state"] == "dismissed"
    assert state(world)["permission_requests"] == []


def test_the_hook_settling_keeps_an_answer_user_gave_at_the_same_time(world: World) -> None:
    request = report(world, "asked", PUSH)
    answer(world, request["id"], "allow", {"rule": None, "list": None})
    settle = agent_client(world).post(
        f"/api/permissions/requests/{request['id']}/settle", json={"state": "answered"}, headers=world.agent()
    )
    assert settle.json()["state"] == "allowed"

    unanswered = report(world, "asked", {"command": "git push --tags"})
    settled = agent_client(world).post(
        f"/api/permissions/requests/{unanswered['id']}/settle", json={"state": "expired"}, headers=world.agent()
    )
    assert settled.json()["state"] == "expired"
    assert answer(world, unanswered["id"], "allow", {"rule": None, "list": None}).status_code == 409

    denial = report(world, "denied", PUSH)
    refused = agent_client(world).post(
        f"/api/permissions/requests/{denial['id']}/settle", json={"state": "answered"}, headers=world.agent()
    )
    assert refused.status_code == 400



def test_a_dialog_that_closes_in_its_session_stays_a_minute_marked_closed(world: World) -> None:
    # PERM-23: Claude Code's removal safety check closes its dialog on its own after two minutes. The card
    # stays, marked closed, so it does not go from under the user's eyes with a Restart card in its place.
    removal = {"command": 'rm -rf "$CLAUDE_JOB_DIR/tmp/clone"'}
    request = report(world, "asked", removal)
    world.cache.sessions = [replace(at_dialog(SESSION_A), updated_at=request["created_at"])]
    assert [card["id"] for card in state(world)["permission_requests"]] == [request["id"]]
    settled = agent_client(world).post(
        f"/api/permissions/requests/{request['id']}/settle", json={"state": "answered"}, headers=world.agent()
    )
    assert settled.json()["state"] == "answered"
    # The session listing still shows the dialog: neither the card nor a Restart card for the session leaves a gap.
    page = state(world)
    assert page["permission_requests"] == [] and page["counts"]["blocked"] == 0
    assert [(card["id"], card["settled"]) for card in page["settled_requests"]] == [(request["id"], True)]
    assert page["waiting_sessions"] == [] and page["counts"]["sessions_waiting"] == 0
    # A minute on, the card has gone, and the Restart card still keeps away while the listing catches up.
    now = datetime.now(UTC)
    conn = db.connect(world.db_path)
    try:
        conn.execute(
            "UPDATE permission_requests SET created_at = ?, resolved_at = ? WHERE id = ?",
            (permissions._iso(now - timedelta(seconds=90)), permissions._iso(now - timedelta(seconds=61)), request["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    world.cache.sessions = [replace(at_dialog(SESSION_A), updated_at=permissions._iso(now - timedelta(seconds=90)))]
    page = state(world)
    assert page["settled_requests"] == [] and page["permission_requests"] == [] and page["waiting_sessions"] == []
    # A dialog that came after the close is the session's next one, and its Restart card shows.
    world.cache.sessions = [replace(at_dialog(SESSION_A), updated_at=permissions._iso(now - timedelta(seconds=50)))]
    assert [session["session_id"] for session in state(world)["waiting_sessions"]] == [SESSION_A]
    # An answer from the page leaves at once: the user knows where it went.
    answered = report(world, "asked", {"command": "git push --tags"})
    answer(world, answered["id"], "allow", {"rule": None, "list": None})
    assert state(world)["settled_requests"] == []


def test_an_answer_in_the_dialogs_own_second_keeps_its_restart_card_away(world: World) -> None:
    # The session listing gives the dialog's time to the millisecond; an answer keeps whole seconds.
    request = report(world, "asked", PUSH)
    conn = db.connect(world.db_path)
    try:
        conn.execute(
            "UPDATE permission_requests SET state = 'allowed', created_at = ?, resolved_at = ? WHERE id = ?",
            ("2026-01-30T12:00:00Z", "2026-01-30T12:00:00Z", request["id"]),
        )
        opened = datetime(2026, 1, 30, 12, 0, 0, 500000, tzinfo=UTC)
        now = datetime(2026, 1, 30, 12, 0, 5, tzinfo=UTC)
        assert permissions.just_answered(conn, now, {SESSION_A: opened}) == {SESSION_A}
        assert permissions.just_answered(conn, now, {SESSION_A: opened + timedelta(seconds=1)}) == set()
    finally:
        conn.close()


def test_only_user_answers_requests(world: World) -> None:
    request = report(world, "asked", PUSH)
    for action, body in (("allow", {"rule": None, "list": None}), ("deny", None), ("dismiss", None)):
        response = agent_client(world).post(
            f"/api/permissions/requests/{request['id']}/{action}", json=body, headers=world.agent()
        )
        assert response.status_code == 403, action
    assert state(world)["permission_requests"][0]["state"] == "open"


def test_old_requests_drop_off_the_page(world: World) -> None:
    asked = report(world, "asked", PUSH)
    denied = report(world, "denied", PUSH)
    conn = db.connect(world.db_path)
    try:
        now = datetime.now(UTC)
        shown = permissions.open_requests(conn, now, HOME, [], {})
        assert {entry["id"] for entry in shown} == {asked["id"], denied["id"]}
        later = now + timedelta(seconds=permissions.ASKED_SHOWN_SECONDS + 5)
        assert [entry["id"] for entry in permissions.open_requests(conn, later, HOME, [], {})] == [denied["id"]]
        next_day = now + timedelta(hours=permissions.DENIED_SHOWN_HOURS, minutes=1)
        assert permissions.open_requests(conn, next_day, HOME, [], {}) == []
    finally:
        conn.close()


# Rules at a few widths, narrowest first, for the user to pick from (SPEC PERM-11). None allows a whole program
# everywhere: an interpreter gets its script, a plain command only itself.
@pytest.mark.parametrize(
    ("tool_name", "tool_input", "rules"),
    [
        (
            "Bash",
            {"command": "cd ~/dev/app && git push origin x"},
            ["Bash(git push origin x)", "Bash(git push origin x *)", "Bash(git push origin *)", "Bash(git push *)"],
        ),
        # A rule matches leading assignments and a timeout only as written (Claude Code 2.1.295).
        ("Bash", {"command": "FOO=1 BAR=2 uv run pytest -q"}, ["Bash(FOO=1 BAR=2 uv run pytest -q)"]),
        ("Bash", {"command": "timeout 900 uv run pytest -q 2>&1 | tail -5"}, ["Bash(timeout 900 uv run pytest -q)"]),
        ("Bash", {"command": "cd x && UV=$CLAUDE_JOB_DIR/v uv run pytest"}, []),
        ("Bash", {"command": "./deploy.sh --prod"}, ["Bash(./deploy.sh --prod)", "Bash(./deploy.sh *)"]),
        ("Bash", {"command": "ls | head"}, ["Bash(ls)"]),
        ("Bash", {"command": "git status --short | head"}, ["Bash(git status --short)", "Bash(git status *)"]),
        ("Bash", {"command": "cd /tmp"}, []),
        ("Bash", {"command": "python3 /home/sam/.claude/jobs/ab/tmp/export_text.py"}, ["Bash(python3 /home/sam/.claude/jobs/ab/tmp/export_text.py)"]),
        (
            "Bash",
            {"command": "uv run python scripts/run.py --limit 5 2>&1 | tail -3"},
            ["Bash(uv run python scripts/run.py --limit 5)", "Bash(uv run python scripts/run.py *)"],
        ),
        ("Bash", {"command": "python3 -m pytest -q"}, ["Bash(python3 -m pytest -q)"]),
        # Claude Code reads a rule's backslashes and parentheses escaped.
        ("Bash", {"command": "python3 -c 'print(1)'"}, ["Bash(python3 -c 'print\\(1\\)')"]),
        ("Bash", {"command": "grep -n 'a\\|b' x.py"}, ["Bash(grep -n 'a\\\\|b' x.py)"]),
        ("Bash", {"command": "python3 - <<'EOF'\nprint(1)\nEOF"}, []),
        ("Bash", {"command": "rm -rf $HOME/x"}, []),
        ("Bash", {"command": "bash -c 'git push'"}, []),
        ("Bash", {"command": "gh api repos/x/y/pulls --jq '.[].title'"}, ["Bash(gh api repos/x/y/pulls --jq '.[].title')"]),
        # A call that reaches for a credential gets its exact command only.
        ("Bash", {"command": "kubectl get pods --kubeconfig ~/.kube/config"}, ["Bash(kubectl get pods --kubeconfig ~/.kube/config)"]),
        ("Bash", {"command": "npm publish --userconfig ~/.npmrc"}, ["Bash(npm publish --userconfig ~/.npmrc)"]),
        ("WebFetch", {"url": "https://docs.example.test/a?b=1", "prompt": "read"}, ["WebFetch(domain:docs.example.test)"]),
        ("Read", {"file_path": "/home/sam/.config/gh/notes.txt"}, ["Read(~/.config/gh/notes.txt)"]),
        ("Read", {"file_path": "/etc/hosts"}, ["Read(//etc/hosts)", "Read(//etc/**)"]),
        ("Grep", {"pattern": "x"}, ["Read(~/dev/app/**)"]),
        ("Glob", {"pattern": "*.py", "path": "/srv/data"}, ["Read(//srv/data/**)"]),
        ("Write", {"file_path": "/home/sam/dev/app/a.py", "content": "x"}, ["Edit(~/dev/app/a.py)", "Edit(~/dev/app/**)"]),
        ("mcp__claude_ai_Linear__save_issue", {"title": "x"}, ["mcp__claude_ai_Linear__save_issue"]),
    ],
)
def test_suggested_rules_go_from_narrow_to_wide(tool_name: str, tool_input: dict, rules: list) -> None:
    suggested = permissions.suggest_rules(tool_name, tool_input, "/home/sam/dev/app", HOME)
    assert [rule["rule"] for rule in suggested["rules"]] == rules


SECRET_COMMANDS = [
    "gh auth token",
    "gh auth token --hostname example.test",
    "gh --hostname example.test auth token",
    "gh auth status --show-token",
    "gh auth status -t",
    "gh auth status -t=true",
    "gh auth status -t=1",
    "gh auth status -at=true",
    "gh auth status -t=false",
    "tasks --quiet auth print-identity-token",
    "tasks --project tasks secrets versions access latest",
    "tasks get-secret-value --name tasks",
    "tasks --endpoint example.test kv --format json get tasks",
    "tasks config view --raw=true",
    "tasks config --context tasks view --raw=false",
    "tasks config view --raw true",
    "tasks --context tasks config view --flatten",
    "tasks config view --flatten=true",
    "tasks config view --flatten false",
    "tasks auth --hostname example.test status -t",
    "tasks auth status -at",
    "tasks credential show",
    "kubectl config view --raw",
    "kubectl config view --raw=true",
    "kubectl config view --flatten",
    "cloudcli --quiet auth print-identity-token",
    "cloudcli secrets versions access latest --secret=db_password --project=tasks-staging",
    "kubectl config view --minify --raw",
    "tasks auth print-access-token",
    "tasks auth print-identity-token",
    "printenv GITHUB_TOKEN",
    "printenv TASKS_API_KEY",
    "az account get-access-token",
    "aws configure export-credentials",
    "git credential fill",
]
WORD_ONLY_COMMANDS = [
    "tasks --PASSWORD=example list",
    "tasks /srv/tasks/API_KEY/value",
    "tasks passwd tasks",
    "tasks secrets list",
    "pytest -k tokenizer",
    "echo 'tasks auth token'",
    "echo tasks auth token",
    "printf '%s' tasks auth token",
]
SECRET_FILE_COMMANDS = [
    f"npm publish --userconfig {prefix}{name}"
    for name in (".npmrc", ".netrc", ".pypirc", ".git-credentials", ".env", ".env.local", "service.key", "service.pem")
    for prefix in ("", "./", "../tasks/", "/srv/tasks/", "/home/ana/dev/tasks/", "~/")
]
SECRET_VARIABLES = [
    f"{prefix}{name}"
    for name in ("TOKEN", "PASSWORD", "SECRET", "API_KEY", "PASSWD")
    for prefix in ("", "TASKS_")
]


@pytest.mark.parametrize("command", SECRET_COMMANDS + SECRET_FILE_COMMANDS + WORD_ONLY_COMMANDS)
def test_credentials_get_only_an_exact_rule_and_save_that_rule(world: World, command: str) -> None:
    tool_input = {"command": command}
    assert bool(permissions.credentials_in(tool_input, Path("/home/ana"))) == (command not in WORD_ONLY_COMMANDS)
    assert permissions.may_reach_credentials(tool_input, Path("/home/ana"))
    suggested = permissions.suggest_rules("Bash", tool_input, "/srv/tasks", Path("/home/ana"))
    assert [entry["rule"] for entry in suggested["rules"]] == [f"Bash({command})"]
    request = report(world, "denied", tool_input, cwd="/srv/tasks", reason="[Credential Exploration]")
    card = state(world)["permission_requests"][0]
    if command in WORD_ONLY_COMMANDS:
        assert "This call reaches for no credentials" in card["cause"]["text"]
    else:
        assert "The call itself reaches for credentials:" in card["cause"]["text"]
    assert card["suggested_sentences"] == []
    assert card["suggested_rules"] == suggested["rules"]
    rule = card["suggested_rules"][0]["rule"]
    response = answer(world, request["id"], "allow", {"rule": rule, "list": "allow"})
    assert response.status_code == 200, response.text
    saved = json.loads(world.claude_settings.read_text())["permissions"]["allow"]
    assert saved == ["Bash(gh pr view *)", rule]


@pytest.mark.parametrize("command", [
    "bash -c 'tasks config view --raw=true'",
    "bash -c 'tasks config view --flatten=true'",
    "bash -c 'tasks auth status -t'",
    "bash -lc 'kubectl config view --raw'",
    "bash -ec 'kubectl config view --raw'",
    "env VAR=x kubectl config view --raw",
    "sudo kubectl config view --raw",
    "sudo -u ana kubectl config view --raw",
    "sudo -n bash -c 'kubectl config view --raw'",
    'echo "$(kubectl config view --raw --minify)"',
    'echo `kubectl config view --raw --minify`',
    'printf "%s" "$(kubectl config view --raw)"',
    "TASKS_TOKEN=example git status",
    "tasks secrets list | git status",
    "cd ~/.ssh && ls",
])
def test_credential_wrappers_and_shell_setup_offer_only_narrow_choices(command: str) -> None:
    tool_input = {"command": command}
    assert permissions.may_reach_credentials(tool_input, HOME)
    assert all("*" not in rule["rule"] for rule in permissions.suggest_rules("Bash", tool_input, "/srv/tasks", HOME)["rules"])
    assert permissions.suggest_sentences("Bash", tool_input, "/srv/tasks", HOME) == []


@pytest.mark.parametrize("tool_name", ["Read", "Edit", "Write", "MultiEdit", "NotebookEdit"])
@pytest.mark.parametrize("path", [
    "/srv/tasks/.npmrc", "~/.ssh/id_rsa", "/home/sam/.ssh/id_rsa",
    "/srv/tasks/.env", "/srv/tasks/.env.local", "/srv/tasks/service.key", "/srv/tasks/service.pem",
])
def test_credential_file_cards_offer_and_save_only_the_exact_file(world: World, tool_name: str, path: str, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: HOME))
    key = "notebook_path" if tool_name == "NotebookEdit" else "file_path"
    tool_input = {key: path}
    rule_tool = "Read" if tool_name == "Read" else "Edit"
    rule_path = path.replace("/home/sam/", "~/") if path.startswith(("~", "/home/sam/")) else "/" + path
    expected = [{"rule": f"{rule_tool}({rule_path})", "covers": "only this file"}]
    assert permissions.suggest_rules(tool_name, tool_input, "/srv/tasks", HOME)["rules"] == expected
    request = report(world, "denied", tool_input, tool_name=tool_name, cwd="/srv/tasks")
    card = state(world)["permission_requests"][0]
    assert card["suggested_rules"] == expected
    assert card["suggested_sentences"] == []
    response = answer(world, request["id"], "allow", {"rule": expected[0]["rule"], "list": "allow"})
    assert response.status_code == 200, response.text
    assert json.loads(world.claude_settings.read_text())["permissions"]["allow"] == [
        "Bash(gh pr view *)", expected[0]["rule"],
    ]


@pytest.mark.parametrize("tool_name", ["Grep", "Glob", "LS"])
@pytest.mark.parametrize("tool_input,cwd", [
    ({"path": "~/.ssh"}, "/srv/tasks"),
    ({"path": "/srv/tasks/.npmrc"}, "/srv/tasks"),
    ({"pattern": "*.pub", "path": "/home/sam/.ssh"}, "/srv/tasks"),
    ({"pattern": "**/.npmrc", "path": "/srv/tasks"}, "/srv/tasks"),
    ({"pattern": "value", "glob": ".env*", "path": "/srv/tasks"}, "/srv/tasks"),
    ({"pattern": "**/.env.*", "path": "/srv/tasks"}, "/srv/tasks"),
    ({"pattern": "*.key", "path": "/srv/tasks"}, "/srv/tasks"),
    ({"pattern": "**/*.pem", "path": "/srv/tasks"}, "/srv/tasks"),
    ({"path": "/srv/tasks/.env"}, "/srv/tasks"),
    ({"pattern": "*"}, "/home/sam/.ssh"),
])
def test_credential_search_cards_offer_no_directory_rule(world: World, tool_name: str, tool_input: dict, cwd: str, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: HOME))
    assert permissions.suggest_rules(tool_name, tool_input, cwd, HOME)["rules"] == []
    report(world, "denied", tool_input, tool_name=tool_name, cwd=cwd)
    shown = state(world)["permission_requests"][0]
    assert shown["suggested_rules"] == []
    assert shown["suggested_sentences"] == []
    assert json.loads(world.claude_settings.read_text())["permissions"]["allow"] == ["Bash(gh pr view *)"]


@pytest.mark.parametrize("tool_name,tool_input", [
    ("WebFetch", {"url": "https://example.test/.npmrc"}),
    ("ToolSearch", {"query": "Read ~/.ssh/id_rsa"}),
    ("Read", {"path": "~/.ssh/id_rsa"}),
    ("Read", {"file_path": "/srv/tasks/*.npmrc"}),
    ("Read", {"file_path": "~/.ssh/id_?"}),
    ("Edit", {"file_path": "~/.ssh/id_[ab]"}),
])
def test_credentials_get_no_domain_tool_or_wildcard_file_rule(tool_name: str, tool_input: dict) -> None:
    assert permissions.credentials_in(tool_input, HOME)
    assert permissions.suggest_rules(tool_name, tool_input, "/srv/tasks", HOME)["rules"] == []


@pytest.mark.parametrize("tool_name,tool_input", [
    ("Bash", {"command": "gh auth token"}),
    ("Bash", {"command": "tasks config view --flatten"}),
    ("Bash", {"command": "tasks --project tasks secrets versions access latest"}),
    ("Bash", {"command": "echo 'tasks auth token'"}),
    ("Bash", {"command": "pytest -k tokenizer"}),
    ("Bash", {"command": "gh auth status -t=true"}),
    ("Bash", {"command": "bash -lc 'kubectl config view --raw'"}),
    ("Bash", {"command": "sudo -u ana kubectl config view --raw"}),
    ("Bash", {"command": "env VAR=x kubectl config view --raw"}),
    ("Bash", {"command": 'echo "$(kubectl config view --raw --minify)"'}),
    ("Bash", {"command": 'echo `kubectl config view --raw --minify`'}),
    ("Bash", {"command": "printenv GITHUB_TOKEN"}),
    ("Bash", {"command": "az account get-access-token"}),
    ("Bash", {"command": "aws configure export-credentials"}),
    ("Bash", {"command": "git credential fill"}),
    ("Read", {"file_path": "/srv/tasks/.npmrc"}),
    ("Read", {"file_path": "~/.ssh/id_rsa"}),
    ("Grep", {"path": "~/.ssh", "pattern": "tasks"}),
])
def test_credential_cards_start_with_user_written_sentences(world: World, tool_name: str, tool_input: dict) -> None:
    assert permissions.suggest_sentences(tool_name, tool_input, "/srv/tasks", HOME) == []
    request = report(world, "denied", tool_input, tool_name=tool_name, cwd="/srv/tasks", reason="[Credential Exploration]")
    proposal = permissions.GITHUB_WORK
    conn = db.connect(world.db_path)
    try:
        permissions.propose(conn, "agent:tasks", request["id"], SESSION_A, proposal)
    finally:
        conn.close()
    card = state(world)["permission_requests"][0]
    assert card["proposed_sentence"] == proposal
    assert card["suggested_sentences"] == []
    assert card["action_sentence"] is None
    assert card["covered_by"] == []
    assert answer(world, request["id"], "allow", {"rule": "", "list": "auto"}).status_code == 400
    sentence = "Sessions may read the Tasks staging credentials."
    response = answer(world, request["id"], "allow", {"rule": sentence, "list": "auto"})
    assert response.status_code == 200, response.text
    assert json.loads(world.claude_settings.read_text())["autoMode"]["allow"][-1] == sentence


@pytest.mark.parametrize("name", SECRET_VARIABLES)
@pytest.mark.parametrize("braces", [False, True])
def test_secret_variables_get_no_lasting_rule(world: World, name: str, braces: bool) -> None:
    variable = "${" + name + "}" if braces else "$" + name
    tool_input = {"command": f'kubectl get pods --token "{variable}"'}
    assert permissions.credentials_in(tool_input, Path("/home/ana")) == [f"${name}"]
    assert permissions.suggest_rules("Bash", tool_input, "/srv/tasks", Path("/home/ana"))["rules"] == []
    report(world, "denied", tool_input, cwd="/srv/tasks")
    assert state(world)["permission_requests"][0]["suggested_rules"] == []
    assert json.loads(world.claude_settings.read_text())["permissions"]["allow"] == ["Bash(gh pr view *)"]


@pytest.mark.parametrize(
    ("command", "rules"),
    [
        ("ls -la", ["Bash(ls -la)"]),
        ("git status", ["Bash(git status)", "Bash(git status *)"]),
        ("gh auth status", ["Bash(gh auth status)", "Bash(gh auth status *)", "Bash(gh auth *)"]),
        ("kubectl config view", ["Bash(kubectl config view)", "Bash(kubectl config view *)", "Bash(kubectl config *)"]),
        ("npm publish --userconfig npmrc-docs", ["Bash(npm publish --userconfig npmrc-docs)", "Bash(npm publish *)"]),
    ],
)
def test_ordinary_commands_keep_their_usual_rules(command: str, rules: list[str]) -> None:
    tool_input = {"command": command, "description": "Document gh auth token and tasks auth print-access-token"}
    assert permissions.credentials_in(tool_input, Path("/home/ana")) == []
    suggested = permissions.suggest_rules("Bash", tool_input, "/srv/tasks", Path("/home/ana"))
    assert [entry["rule"] for entry in suggested["rules"]] == rules


@pytest.mark.parametrize("command,cwd", [
    ("cat .ssh/id_rsa", "/home/sam"),
    ("cat ./.ssh/id_rsa", "/home/sam"),
    ("cat ../.ssh/id_rsa", "/home/sam/tasks"),
    ("cd .ssh && cat id_rsa", "/home/sam"),
    ("cd ../.ssh && cat id_rsa", "/home/sam/tasks"),
    ("cd '.ssh' && cat id_rsa", "/home/sam"),
    ("cd .ssh; cat id_rsa", "/home/sam"),
    ("pwd && cd .ssh && cat id_rsa", "/home/sam"),
    ("cd .ssh\ncat id_rsa", "/home/sam"),
    ("pwd\ncd .ssh\ncat id_rsa", "/home/sam"),
    ("cd tasks; cd ../.ssh; cat id_rsa", "/home/sam"),
    ("cd .ssh; cat id_rsa; cd ..; cat README.md", "/home/sam"),
])
def test_relative_credential_commands_offer_exact_rules_and_user_written_sentences(
    world: World, command: str, cwd: str, monkeypatch,
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: HOME))
    tool_input = {"command": command}
    assert permissions.credentials_in({**tool_input, "cwd": cwd}, HOME) == ["~/.ssh/id_rsa"]
    report(world, "denied", tool_input, cwd=cwd, reason="[Credential Exploration]")
    shown = state(world)["permission_requests"][0]
    assert len(shown["suggested_rules"]) == 1
    assert "*" not in shown["suggested_rules"][0]["rule"]
    assert shown["suggested_sentences"] == []
    assert "The call itself reaches for credentials:" in shown["cause"]["text"]
    assert "~/.ssh/id_rsa" in shown["cause"]["text"]


def test_a_tool_rule_names_the_commands_it_leaves_to_auto_mode() -> None:
    command = "python3 ~/.claude/jobs/ab/tmp/export_text.py; git diff --stat; python3 ~/.claude/jobs/ab/tmp/count_tasks.py 2>/dev/null | tail -1"
    suggested = permissions.suggest_rules("Bash", {"command": command}, "/home/sam/dev/app", HOME)
    assert suggested["rules"] == [{
        "rule": "Bash(python3 ~/.claude/jobs/ab/tmp/export_text.py)",
        "covers": "only this exact command, with whatever the script holds when it runs",
    }]
    assert suggested["others"] == ["python3 ~/.claude/jobs/ab/tmp/count_tasks.py"]


READS = permissions.read_work(HOME)
CODE_IN_APP = "Sessions may change, build, test, commit and push code in ~/dev/app and its worktrees, on their own branches."


# Invented Tasks calls cover each work kind (SPEC PERM-4), with a sentence naming its scope.
@pytest.mark.parametrize(
    ("tool_name", "tool_input", "cwd", "sentences"),
    [
        (
            "Bash",
            {"command": "cd /home/sam/dev/app && FOO=1 uv run pytest -q", "description": "Run the tests"},
            "/home/sam/dev/app/.claude/worktrees/x",
            [CODE_IN_APP],
        ),
        (
            "Edit",
            {"file_path": "/home/sam/dev/app/.claude/worktrees/x/src/app.py"},
            "/home/sam/dev",
            [CODE_IN_APP],
        ),
        (
            "Bash",
            {"command": "bash -c 'python /home/sam/.claude/jobs/ab/tmp/read_lists.py'", "description": "Check export output."},
            "/home/sam/dev",
            [permissions.JOB_FOLDER_WORK],
        ),
        (
            "Bash",
            {
                "command": "gh api repos/example-org/app/issues/20/comments --jq '.[] | .body'",
                "description": "Read the export discussion",
            },
            "/home/sam/dev/app",
            [permissions.GITHUB_WORK, READS],
        ),
        (
            "Bash",
            {"command": "cockpit permission list --session ab", "description": "List this session's blocked calls"},
            "/home/sam/dev/app",
            [permissions.COCKPIT_WORK, READS],
        ),
        (
            "Bash",
            {
                "command": "cat > /home/sam/.claude/jobs/ab/tmp/item.json <<'EOF'\n{\"summary\": \"export ready\"}\nEOF",
                "description": "Prepare a Cockpit export status note",
            },
            "/home/sam/dev/app",
            [permissions.JOB_FOLDER_WORK, permissions.COCKPIT_WORK],
        ),
        (
            # A job folder's script run from a checkout works on that checkout.
            "Bash",
            {"command": "python3 /home/sam/.claude/jobs/ab/tmp/export_text.py; git diff --stat", "description": "Add text export"},
            "/home/sam/dev/app/.claude/worktrees/x",
            [CODE_IN_APP],
        ),
        (
            "Bash",
            {"command": "kill 1000; sleep 1; pgrep -fa 'export-worker' | head -1", "description": "Stop the export worker"},
            "/home/sam/dev",
            [permissions.PROCESS_WORK],
        ),
        (
            "Bash",
            {
                "command": "cat /tmp/tasks-export.log; git branch --list 'export/*';"
                " git worktree list",
                "description": "Read the export log",
            },
            "/home/sam/dev/app",
            [READS],
        ),
        (
            "Bash",
            {"command": "terraform plan -out plan.bin"},
            "/home/sam/dev/infra",
            ["Sessions may run `terraform plan` commands in ~/dev/infra and its worktrees."],
        ),
        ("Read", {"file_path": "/etc/hosts"}, "/srv/data", ["Sessions may use Read in /etc/hosts."]),
        (
            "Bash",
            {"command": "gh pr comment 20 --body 'Added text export for completed tasks'", "description": "Comment on the export change"},
            "/home/sam/dev/app",
            [permissions.GITHUB_WORK],
        ),
        (
            "Bash",
            {"command": "cat > /home/sam/.claude/jobs/ab/tmp/note.md <<'EOF'\nThe export file opens\nEOF"},
            "/home/sam/dev",
            [permissions.JOB_FOLDER_WORK],
        ),
        (
            "mcp__claude_ai_Linear__save_comment",
            {"issueId": "ENG-123", "body": "The export now includes completed tasks.", "description": "export ready"},
            "/home/sam/dev",
            ["Sessions may add and change information in Linear.", "Sessions may use every tool of the Linear connector."],
        ),
        (
            "mcp__claude_ai_Google_Calendar__list_events",
            {"calendarId": "primary"},
            "/home/sam/dev",
            ["Sessions may read information in Google Calendar.", "Sessions may use every tool of the Google Calendar connector."],
        ),
        (
            "Agent",
            {
                "description": "Write list export notes",
                "prompt": "1. Read /home/sam/.claude/jobs/ab/tmp/lists.json.\n2. Load the tool with ToolSearch query"
                " \"select:mcp__claude_ai_Notion__notion-create-pages\".\n3. Call mcp__claude_ai_Notion__notion-create-pages once.",
                "subagent_type": "general-purpose",
            },
            "/home/sam/dev",
            ["Sessions may add information to Notion.", "Sessions may use every tool of the Notion connector.", permissions.HELPER_AGENTS],
        ),
        ("Agent", {"description": "Find the export formatter", "prompt": "Find where text exports add list titles."}, "/home/sam/dev", [permissions.HELPER_AGENTS]),
        (
            "mcp__claude_ai_Datadog__custom_action",
            {},
            "/home/sam/dev",
            ["Sessions may use every tool of the Datadog connector."],
        ),
        (
            "Bash",
            {"command": 'ls docs; cat README.md'},
            "/home/sam/dev/app",
            [READS],
        ),
        (
            "Bash",
            {
                "command": "cd $CLAUDE_JOB_DIR/tmp/export-check && PYTHONPATH=. timeout 900 uv run pytest tests/ -q"
                " -p no:cacheprovider 2>&1 | tail -30"
            },
            "/home/sam/dev/app",
            [
                "Sessions may change, build, test, commit and push code in session job folders (~/.claude/jobs), on"
                " their own branches.",
                permissions.JOB_FOLDER_WORK,
            ],
        ),
        (
            "Bash",
            {"command": "timeout 60 cockpit get --json | head -50"},
            "/home/sam/dev",
            [permissions.COCKPIT_WORK, READS],
        ),
        (
            "Bash",
            {"command": 'for folder in text csv; do rm -rf "build/$folder"; done; df -h /'},
            "/home/sam/dev/app",
            ["Sessions may run `rm` commands in ~/dev/app and its worktrees."],
        ),
    ],
)
def test_suggested_sentences_name_the_kind_of_work_and_where(
    tool_name: str, tool_input: dict, cwd: str, sentences: list
) -> None:
    assert permissions.suggest_sentences(tool_name, tool_input, cwd, HOME) == sentences


def test_either_connector_sentence_covers_its_call_and_a_helper_agent_needs_both_kinds() -> None:
    notion = "mcp__claude_ai_Notion__notion-create-pages"
    kinds = permissions.work_kinds(notion, {}, "/home/sam/dev", HOME)
    whole = "Sessions may use every tool of the Notion connector."
    assert permissions._covered_by(kinds, [whole]) == [whole]
    assert permissions._covered_by(kinds, ["Sessions may add information to Notion."]) == ["Sessions may add information to Notion."]
    assert permissions._covered_by(kinds, ["Sessions may read information in Notion."]) == []
    agent = permissions.work_kinds("Agent", {"prompt": f"Call {notion} once."}, "/home/sam/dev", HOME)
    assert permissions._covered_by(agent, [whole]) == []
    assert permissions._covered_by(agent, [whole, permissions.HELPER_AGENTS]) == [whole, permissions.HELPER_AGENTS]


def test_a_settings_file_that_does_not_read_leaves_the_cards_up(world: World) -> None:
    request = report(world, "denied", {"command": "git status --short | head"})
    world.claude_settings.write_text("{ half written")
    response = world.client.get("/api/state", headers=world.user())
    assert response.status_code == 200
    assert [(entry["id"], entry["covered_by"]) for entry in response.json()["permission_requests"]] == [(request["id"], [])]


def test_the_card_shows_what_a_script_or_an_edit_changes(tmp_path: Path) -> None:
    home = tmp_path
    script = home / ".claude/jobs/ab/tmp/export_text.py"
    script.parent.mkdir(parents=True)
    script.write_text('#!/usr/bin/env python3\n"""Write each task on its own line.\n\nIncludes completed tasks in text exports."""\n\nimport re\n')
    plain = home / "dev/run.sh"
    plain.parent.mkdir(parents=True)
    plain.write_text("set -e\nmake test\n")
    secret = home / ".config/gh/load.sh"
    secret.parent.mkdir(parents=True)
    secret.write_text("# loads the passwords\n")
    made_at = "2099-01-01T00:00:00Z"  # after the files were written

    changes = permissions.call_changes(
        "Bash", {"command": f"python3 {script} && bash {plain}; bash {secret}"}, str(home), home, made_at
    )
    assert changes == [
        {
            "title": "Runs ~/.claude/jobs/ab/tmp/export_text.py, which says:",
            "text": "Write each task on its own line.\n\nIncludes completed tasks in text exports.",
            "note": None,
        },
        {"title": "Runs ~/dev/run.sh, which starts:", "text": "set -e\nmake test", "note": None},
    ]
    # A script that changed after the call says so: the user reads what runs now, not what the session ran.
    changed = permissions.call_changes("Bash", {"command": f"python3 {script}"}, str(home), home, "2020-01-01T00:00:00Z")
    assert changed[0]["note"].startswith("The file changed after the call, at ")

    edit = {"file_path": str(home / "dev/a.py"), "old_string": "x = 1", "new_string": "x = 2\ny = 3"}
    assert permissions.call_changes("Edit", edit, str(home), home, made_at) == [
        {"title": "Changes ~/dev/a.py:", "text": "- x = 1\n+ x = 2\n+ y = 3", "note": None},
    ]
    write = {"file_path": str(home / "dev/b.md"), "content": "hello"}
    assert permissions.call_changes("Write", write, str(home), home, made_at)[0]["title"] == "Writes ~/dev/b.md:"


def test_a_call_a_sentence_already_allows_names_it_and_the_earlier_block_in_its_cause(world: World) -> None:
    earlier = report(world, "denied", {"command": "python3 /home/sam/.claude/jobs/ab/tmp/export_text.py"}, reason="[Modify Shared Resources]")
    status = {"command": "git status --short | head", "description": "Check the worktree"}
    read = report(world, "denied", status, reason="[Modify Shared Resources]")
    conn = db.connect(world.db_path)
    try:
        now = datetime.now(UTC)
        reads = permissions.read_work(HOME)
        # The allowed sentence may be worded differently after the colon, as before a rules review.
        allowed = [reads.split(":")[0] + ": cat, ls and git status."]
        shown = {entry["id"]: entry for entry in permissions.open_requests(conn, now, HOME, allowed, {})}
        assert shown[read["id"]]["covered_by"] == allowed
        assert f"(request {earlier['id']}): `python3 /home/sam/.claude/jobs/ab/tmp/export_text.py`." in shown[read["id"]]["cause"]["text"]
        assert shown[read["id"]]["cause"]["help"].startswith(f"Request {earlier['id']} is still open")
        assert [rule["rule"] for rule in shown[read["id"]]["suggested_rules"]] == ["Bash(git status --short)", "Bash(git status *)"]
        # The script's own call is not one a sentence allows.
        assert (shown[earlier["id"]]["covered_by"], shown[earlier["id"]]["cause"]["help"]) == ([], None)
        assert permissions.open_requests(conn, now, HOME, [], {})[0]["covered_by"] == []
    finally:
        conn.close()


def transcript_line(*blocks: dict, role: str = "assistant") -> str:
    return json.dumps({"type": role, "message": {"role": role, "content": list(blocks)}}) + "\n"


def hook_input(transcript: Path, tool_input: dict) -> str:
    return json.dumps({
        "session_id": SESSION_A, "transcript_path": str(transcript), "cwd": "/home/sam/dev/app",
        "permission_mode": "auto", "hook_event_name": "PermissionRequest", "tool_name": "Bash",
        "tool_input": tool_input, "permission_suggestions": [],
    })


OLDER_PUSH = (
    transcript_line({"type": "tool_use", "id": "toolu_old", "name": "Bash", "input": PUSH})
    + transcript_line({"type": "tool_result", "tool_use_id": "toolu_old", "content": "done"}, role="user")
)
PUSH_NOW = transcript_line({"type": "text", "text": "pushing"}, {"type": "tool_use", "id": "toolu_now", "name": "Bash", "input": PUSH})
PUSH_NOW_RESULT = transcript_line({"type": "tool_result", "tool_use_id": "toolu_now", "content": "ok"}, role="user")


@pytest.fixture
def waiting_hook(world: World, monkeypatch, tmp_path: Path):
    """The CLI wired to the test app, with a transcript holding an older identical call and the one the dialog is about."""
    route_cli_to(world, monkeypatch)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(OLDER_PUSH + PUSH_NOW)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(hook_input(transcript, PUSH)))
    return transcript


def settled_state(world: World) -> str:
    conn = db.connect(world.db_path)
    try:
        return conn.execute("SELECT state FROM permission_requests").fetchone()["state"]
    finally:
        conn.close()


def test_ask_prints_an_allow_when_user_allows_on_cockpit(world: World, waiting_hook: Path, monkeypatch, capsys) -> None:
    def user_answers(seconds: float) -> None:
        answer(world, state(world)["permission_requests"][0]["id"], "allow", {"rule": None, "list": None})

    monkeypatch.setattr(cli.time, "sleep", user_answers)
    assert cli.main(["permission", "ask", "--as", "permission-hook"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}


def test_ask_prints_a_deny_when_user_denies_on_cockpit(world: World, waiting_hook: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli.time, "sleep", lambda seconds: answer(world, state(world)["permission_requests"][0]["id"], "deny")
    )
    assert cli.main(["permission", "ask", "--as", "permission-hook"]) == 0
    decision = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["decision"]
    assert decision == {"behavior": "deny", "message": "Sam denied this call from Cockpit."}


def test_ask_ends_quietly_when_the_session_answers_first(world: World, waiting_hook: Path, monkeypatch, capsys) -> None:
    rounds = []

    def session_answers(seconds: float) -> None:
        rounds.append(seconds)
        if len(rounds) == 2:
            with waiting_hook.open("a") as file:
                file.write(PUSH_NOW_RESULT)

    monkeypatch.setattr(cli.time, "sleep", session_answers)
    assert cli.main(["permission", "ask", "--as", "permission-hook"]) == 0
    assert capsys.readouterr().out == ""
    assert len(rounds) == 2
    assert state(world)["permission_requests"] == []
    assert settled_state(world) == "answered"


def test_ask_finds_a_call_written_after_it_started(world: World, waiting_hook: Path, monkeypatch, capsys) -> None:
    """The older identical call already has a result, so the hook waits for the new one."""
    waiting_hook.write_text(OLDER_PUSH)
    writes = {2: PUSH_NOW, 3: PUSH_NOW_RESULT}
    rounds = []

    def transcript_grows(seconds: float) -> None:
        rounds.append(seconds)
        with waiting_hook.open("a") as file:
            file.write(writes.get(len(rounds), ""))

    monkeypatch.setattr(cli.time, "sleep", transcript_grows)
    assert cli.main(["permission", "ask", "--as", "permission-hook"]) == 0
    assert len(rounds) == 3
    assert settled_state(world) == "answered"


def test_ask_notices_a_result_written_with_the_call(world: World, waiting_hook: Path, monkeypatch, capsys) -> None:
    waiting_hook.write_text(OLDER_PUSH)

    def session_answers_at_once(seconds: float) -> None:
        with waiting_hook.open("a") as file:
            file.write(PUSH_NOW + PUSH_NOW_RESULT)

    monkeypatch.setattr(cli.time, "sleep", session_answers_at_once)
    assert cli.main(["permission", "ask", "--as", "permission-hook"]) == 0
    assert settled_state(world) == "answered"


def test_ask_gives_up_after_the_wait(world: World, waiting_hook: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(permissions, "ASK_WAIT_SECONDS", 0)
    assert cli.main(["permission", "ask", "--as", "permission-hook"]) == 0
    assert capsys.readouterr().out == ""
    assert settled_state(world) == "expired"


def test_ask_takes_an_answer_that_lands_as_the_wait_ends(world: World, waiting_hook: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(permissions, "ASK_WAIT_SECONDS", 0)
    real_call = cli.call

    def user_answers_first(method, path, body, agent):
        if path.endswith("/settle"):
            answer(world, int(path.split("/")[-2]), "allow", {"rule": None, "list": None})
        return real_call(method, path, body, agent)

    monkeypatch.setattr(cli, "call", user_answers_first)
    assert cli.main(["permission", "ask", "--as", "permission-hook"]) == 0
    decision = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["decision"]
    assert decision == {"behavior": "allow"}
    assert settled_state(world) == "allowed"


def test_report_sends_the_whole_input_and_records_the_denial(world: World, monkeypatch) -> None:
    # Allow lets the whole call run, so the card and its explanation get the whole call, not a clipped one.
    route_cli_to(world, monkeypatch)
    command = f"echo {'y' * 5_000} && cat ~/.ssh/id_ed25519"
    payload = {
        "session_id": SESSION_A, "transcript_path": "/nonexistent", "cwd": "/home/sam", "hook_event_name": "PermissionDenied",
        "tool_name": "Bash", "tool_input": {"command": command, "description": "Echo"},
        "tool_use_id": "toolu_9", "reason": "reads a private key",
    }
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(payload)))
    assert cli.main(["permission", "report", "--as", "permission-hook"]) == 0
    request = state(world)["permission_requests"][0]
    assert (request["tool_use_id"], request["reason"]) == ("toolu_9", "reads a private key")
    assert request["tool_input"]["command"] == command
    # A long reason is clipped to what the API takes, its length included.
    payload |= {"tool_use_id": "toolu_10", "reason": "x" * 2_001}
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(payload)))
    assert cli.main(["permission", "report", "--as", "permission-hook"]) == 0
    reason = next(row["reason"] for row in state(world)["permission_requests"] if row["tool_use_id"] == "toolu_10")
    assert len(reason) == cli.REASON_MAX and reason.endswith("… (2001 characters)")


def test_a_call_too_long_to_show_whole_stays_with_its_session(world: World, monkeypatch, capsys) -> None:
    route_cli_to(world, monkeypatch)
    payload = {
        "session_id": SESSION_A, "transcript_path": "/nonexistent", "cwd": "/home/sam", "hook_event_name": "PermissionRequest",
        "tool_name": "Write", "tool_input": {"file_path": "/etc/x", "content": "y" * permissions.TOOL_INPUT_MAX},
        "tool_use_id": "toolu_9", "reason": "writes outside the project",
    }
    for action in ("report", "ask"):
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(payload)))
        assert cli.main(["permission", action, "--as", "permission-hook"]) == 0
        captured = capsys.readouterr()
        assert captured.out == "" and "so this one stays with the session" in captured.err
    assert state(world)["permission_requests"] == []


def test_report_fails_loudly_on_input_it_cannot_use(world: World, monkeypatch, capsys) -> None:
    route_cli_to(world, monkeypatch)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps({"session_id": SESSION_A})))
    assert cli.main(["permission", "report", "--as", "permission-hook"]) == 1
    assert "has no cwd, tool_name, tool_input" in capsys.readouterr().err


def worktree_hook(world: World, monkeypatch, path: str) -> None:
    route_cli_to(world, monkeypatch)
    # The server allows worktrees under the user's home folder; the test's checkouts stand in for it.
    monkeypatch.setattr(Path, "home", lambda: world.checkout.parent.parent)
    payload = {
        "session_id": SESSION_A, "transcript_path": "/nonexistent", "cwd": str(world.checkout.parent),
        "permission_mode": "auto", "hook_event_name": "PermissionRequest", "tool_name": "EnterWorktree",
        "tool_input": {"path": path}, "permission_suggestions": [],
    }
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(payload)))


def test_ask_allows_entering_a_worktree_under_home_at_once(world: World, monkeypatch, capsys) -> None:
    worktree_hook(world, monkeypatch, str(world.checkout / ".claude" / "worktrees" / "fix-x"))
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: pytest.fail("the hook waited"))
    assert cli.main(["permission", "ask", "--as", "permission-hook"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}
    assert settled_state(world) == "allowed"
    assert state(world)["permission_requests"] == []


def test_ask_leaves_a_folder_that_is_no_worktree_to_user(world: World, monkeypatch, capsys) -> None:
    worktree_hook(world, monkeypatch, "/home/sam/.claude/jobs/x/tmp/wt-app")
    monkeypatch.setattr(permissions, "ASK_WAIT_SECONDS", 0)
    assert cli.main(["permission", "ask", "--as", "permission-hook"]) == 0
    assert capsys.readouterr().out == ""
    assert settled_state(world) == "expired"


@pytest.mark.parametrize(("tool_name", "path", "allowed"), [
    ("EnterWorktree", "{home}/dev/app/.claude/worktrees/fix-x", True),
    ("EnterWorktree", "app/.claude/worktrees/fix-x", True),
    # A repository outside the folder of checkouts, and one a folder deeper inside it.
    ("EnterWorktree", "{home}/tool/.claude/worktrees/fix-x", True),
    ("EnterWorktree", "{home}/dev/group/site/.claude/worktrees/fix-x", True),
    ("EnterWorktree", "{home}/dev/app/.claude/worktrees/fix-x/sub", False),
    ("EnterWorktree", "{home}/dev/app/.claude/worktrees/../../../../../escape", False),
    ("EnterWorktree", "{home}/dev/app/.claude/worktrees/link", False),
    ("EnterWorktree", "{home}/dev/not-a-repo/.claude/worktrees/fix-x", False),
    ("EnterWorktree", "{home}/dev/app/worktrees/fix-x", False),
    ("EnterWorktree", "{home}/dev/app", False),
    ("EnterWorktree", "{outside}/app/.claude/worktrees/fix-x", False),
    ("EnterWorktree", "", False),
    ("Bash", "{home}/dev/app/.claude/worktrees/fix-x", False),
])
def test_only_a_worktree_of_a_repository_under_home_is_allowed(tmp_path: Path, tool_name: str, path: str, allowed: bool) -> None:
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    for repository in (home / "dev" / "app", home / "tool", home / "dev" / "group" / "site", outside / "app"):
        (repository / ".git").mkdir(parents=True)
    (home / "dev" / "not-a-repo").mkdir()
    (home / "dev" / "app" / ".claude" / "worktrees").mkdir(parents=True)
    (home / "dev" / "app" / ".claude" / "worktrees" / "link").symlink_to(outside)
    tool_input = {"path": path.format(home=home, outside=outside)}
    assert permissions.own_worktree(tool_name, tool_input, str(home / "dev"), home) is allowed


def job_folder(home: Path, folder: str, session_id: str) -> Path:
    """A background session's job folder as Claude Code makes it in the account folder `folder` under `home`; its
    scratch folder `tmp`."""
    job = home / folder / "jobs" / session_id[:8]
    (job / "tmp").mkdir(parents=True)
    (job / "state.json").write_text(json.dumps({"sessionId": session_id}))
    return (job / "tmp").resolve()


def test_a_background_session_has_the_tmp_of_its_job_folder_in_either_account(tmp_path: Path, claude_accounts) -> None:
    home = tmp_path / "home"
    personal, work = claude_accounts
    scratch_a, scratch_b = job_folder(home, personal.folder.name, SESSION_A), job_folder(home, work.folder.name, SESSION_B)
    assert permissions.own_scratch(SESSION_A, home) == scratch_a
    assert permissions.own_scratch(SESSION_B, home) == scratch_b
    # Without the UUID check, `../../x` would name the folder home/x, which looks like a job folder of that session.
    (home / "x" / "tmp").mkdir(parents=True)
    (home / "x" / "state.json").write_text(json.dumps({"sessionId": "../../x"}))
    assert permissions.own_scratch("../../x", home) is None


SPOILED_JOB_FOLDERS = {
    "no job folder": lambda job, outside: shutil.rmtree(job),
    "no state.json": lambda job, outside: (job / "state.json").unlink(),
    "state.json not JSON": lambda job, outside: (job / "state.json").write_text("{"),
    "state.json no object": lambda job, outside: (job / "state.json").write_text(json.dumps([SESSION_A])),
    "another session's": lambda job, outside: (job / "state.json").write_text(json.dumps({"sessionId": SESSION_B})),
    "no tmp": lambda job, outside: (job / "tmp").rmdir(),
    "tmp a link": lambda job, outside: ((job / "tmp").rmdir(), (job / "tmp").symlink_to(outside)),
    # The linked folder holds the session's state.json and a real tmp, but the job folder is not Claude Code's own.
    "job folder a link": lambda job, outside: (shutil.move(job, outside / "job"), job.symlink_to(outside / "job")),
}


@pytest.mark.parametrize("spoil", SPOILED_JOB_FOLDERS.values(), ids=SPOILED_JOB_FOLDERS.keys())
def test_no_scratch_folder_without_a_job_folder_of_the_sessions_own(tmp_path: Path, claude_accounts, spoil) -> None:
    home, outside = tmp_path / "home", tmp_path / "outside"
    outside.mkdir()
    spoil(job_folder(home, claude_accounts[0].folder.name, SESSION_A).parent, outside)
    assert permissions.own_scratch(SESSION_A, home) is None


@pytest.mark.parametrize(("command", "removal"), [
    # Nothing but `rm` on full paths in the scratch folder.
    ("rm -f {scratch}/a.png", "only"),
    ("rm -rf {scratch}/shots", "only"),
    ("rm -f {scratch}/shots/*.png {scratch}/b.txt", "only"),
    ("rm -f {scratch}/new/a.png", "only"),
    ("rm -rf -- {scratch}/a && rm -fv {scratch}/b ; rm --recursive --force {scratch}/c\nrm {scratch}/d", "only"),
    ("rm -f {scratch}/link", "only"),  # the link itself
    # Deletions in the scratch folder among other commands, or written with variables, quotes, `cd` or relative paths.
    ("S={scratch}/shots; rm -f $S/*.png", "among"),
    ('S={scratch}/shots\nrm -f "$S"/*.png\nfor y in 1 2; do echo $y; done', "among"),
    ("rm -f $CLAUDE_JOB_DIR/tmp/a.txt", "among"),
    ("rm -f ${{CLAUDE_JOB_DIR}}/tmp/a.txt", "among"),
    (
        "mkdir -p {scratch}/sp && cd {scratch}/sp && rm -f * ; (timeout 60 python3 run.py &) ;"
        " for i in $(seq 1 10); do test -f done && break; done; ls; tail -5 log.txt",
        "among",
    ),
    ("J={scratch}; cd $J/work; rm -f run/* out/log.jsonl; python3 - <<'EOF'\nprint(1)\nEOF", "among"),
    ('rm -f "{scratch}/a.txt"', "among"),
    ("cd {scratch} && rm -f a.txt", "among"),
    ("rm -f {scratch}/a.txt && echo done", "among"),
    ("rm -f {scratch}/a.txt | cat", "among"),
    ("rm -f {scratch}/a.txt || true", "among"),
    ("rm -f {scratch}/a.txt &", "among"),
    ("rm -f {scratch}/a.txt > /dev/null", None),
    # The scratch folder itself, what lies beside it, and paths rm would follow elsewhere.
    ("rm -rf {scratch}", None),
    ("rm -rf {scratch}/", None),
    ("rm -f {scratch}/../state.json", None),
    ("rm -f {job}/state.json", None),
    ("rm -rf {job}/tmp", None),
    ("rm -rf {scratch}//a", None),
    ("rm -rf {scratch}/./a", None),
    ("rm -rf {scratch}/a/..", None),
    ("rm -rf {scratch}/a/", None),
    ("rm -rf {scratch}/.*", None),
    ("rm -rf {scratch}/*/b", None),
    ("rm -rf {scratch}/link/*", None),
    ("rm -rf {scratch}/link/x", None),
    # A link on the way to the target, even one that stays in the scratch folder, and one that leads back to itself.
    ("rm -f {scratch}/to_shots/a.png", None),
    ("rm -f {scratch}/loop/a", None),
    ("rm -f {scratch}/a && rm -f {scratch}/loop/a", None),
    # `**` can match the folder itself.
    ("rm -rf {scratch}/**", None),
    ("rm -rf {scratch}/shots/**", None),
    # An option after the first target is a target: rm reads it as a file's name when POSIXLY_CORRECT is set.
    ("rm -rf {scratch}/a -f", None),
    ("rm {scratch}/a -rf", None),
    ("rm -rf -- {scratch}/a", "only"),
    # Deletions elsewhere, also beside one in the scratch folder.
    ("rm -rf {home}/dev/project/.venv", None),
    ("rm .venv", None),
    ("rm -rf {other}/a", None),
    ("rm -f {scratch}/a.txt; rm -rf {home}/dev/x", None),
    ("T={home}/dev/.sbx-test-$$ && mkdir $T && touch $T/a ; rm -rf $T", None),
    # The shell reads `r''m` and `\rm` as rm.
    ("rm -f {scratch}/a; r''m -rf /etc", None),
    ("rm -f {scratch}/a; \\rm -rf /etc", None),
    # Words the reading cannot follow, and rm where it cannot follow it.
    ("rm -f ~/x", None),
    ("rm -f {scratch}/$(whoami)", None),
    ("rm -f {scratch}/a{{1,2}}", None),
    ("rm -f '{scratch}/a b'", None),
    ("sudo rm -f {scratch}/a", None),
    ("find {scratch} -name x | xargs rm", None),
    ("/bin/rm -f {scratch}/a", None),
    ("for f in a b; do rm -f {scratch}/$f; done", None),
    ("(cd {scratch}; rm -f *)", None),
    ("rm -i {scratch}/a", None),
    ("rm --no-preserve-root {scratch}/a", None),
    ("rm -rf", None),
    # A variable or folder set anew before the deletion.
    ("S={scratch}; S=/etc; rm -f $S/a", None),
    ("S={scratch}; read S; rm -f $S/a", None),
    ("cd {scratch}; cd /etc; rm -f a", None),
    ("cd {scratch}; cd -; rm -f a", None),
    ("cd {scratch}; pushd /etc; rm -f a", None),
    # No deletion.
    ("ls {scratch}", None),
    ("echo rm", None),
])
def test_what_a_call_deletes_in_the_sessions_scratch_folder(tmp_path: Path, command: str, removal: str | None) -> None:
    home, outside = tmp_path / "home", tmp_path / "outside"
    scratch = job_folder(home, ".claude", SESSION_A)
    other = job_folder(home, ".claude", SESSION_B)
    (scratch / "shots").mkdir()
    outside.mkdir()
    (scratch / "link").symlink_to(outside)
    (scratch / "to_shots").symlink_to(scratch / "shots")
    (scratch / "loop").symlink_to(scratch / "loop")
    text = command.format(scratch=scratch, job=scratch.parent, home=home, other=other)
    assert permissions.scratch_removal(text, scratch) == removal


def scratch_hook(world: World, monkeypatch, home: Path, command: str) -> None:
    """The CLI wired to the test app, with SESSION_A's dialog on the Bash call `command`; the server's home is `home`."""
    route_cli_to(world, monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: home)
    tool_input = {"command": command, "description": "x"}
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(hook_input(Path("/nonexistent"), tool_input)))


def query(world: World, sql: str) -> list[tuple]:
    conn = db.connect(world.db_path)
    try:
        return [tuple(row) for row in conn.execute(sql)]
    finally:
        conn.close()


PERMISSION_EVENTS = "SELECT actor, kind FROM events WHERE kind LIKE 'permission.%' ORDER BY id"


def test_ask_allows_a_call_that_only_deletes_in_the_scratch_folder_at_once(
    world: World, monkeypatch, capsys, tmp_path: Path, claude_accounts
) -> None:
    scratch = job_folder(tmp_path / "home", claude_accounts[0].folder.name, SESSION_A)
    scratch_hook(world, monkeypatch, tmp_path / "home", f"rm -f {scratch}/a.png {scratch}/b.txt")
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: pytest.fail("the hook waited"))
    assert cli.main(["permission", "ask", "--as", "permission-hook"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}
    assert settled_state(world) == "allowed"
    assert state(world)["permission_requests"] == []
    assert query(world, PERMISSION_EVENTS)[-1] == ("cockpit", "permission.allowed")


def test_ask_sends_a_call_that_deletes_in_the_scratch_folder_among_more_back_at_once(
    world: World, monkeypatch, capsys, tmp_path: Path, claude_accounts
) -> None:
    scratch = job_folder(tmp_path / "home", claude_accounts[0].folder.name, SESSION_A)
    scratch_hook(world, monkeypatch, tmp_path / "home", f"rm -f {scratch}/a.png && ls {scratch}")
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: pytest.fail("the hook waited"))
    assert cli.main(["permission", "ask", "--as", "permission-hook"]) == 0
    output = json.loads(capsys.readouterr().out)
    advice = permissions.split_advice(scratch)
    assert output == {
        "hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "deny", "message": advice}}
    }
    assert str(scratch) in advice
    assert settled_state(world) == "denied"
    assert state(world)["permission_requests"] == []
    assert query(world, PERMISSION_EVENTS)[-1] == ("cockpit", "permission.denied")


def test_ask_leaves_the_same_mixed_call_made_again_to_user(
    world: World, monkeypatch, capsys, tmp_path: Path, claude_accounts
) -> None:
    scratch = job_folder(tmp_path / "home", claude_accounts[0].folder.name, SESSION_A)
    monkeypatch.setattr(permissions, "ASK_WAIT_SECONDS", 0)
    outputs = []
    for _ in range(2):
        scratch_hook(world, monkeypatch, tmp_path / "home", f"rm -f {scratch}/a.png && ls {scratch}")
        assert cli.main(["permission", "ask", "--as", "permission-hook"]) == 0
        outputs.append(capsys.readouterr().out)
    assert json.loads(outputs[0])["hookSpecificOutput"]["decision"]["behavior"] == "deny"
    assert outputs[1] == ""
    assert query(world, "SELECT state FROM permission_requests ORDER BY id") == [("denied",), ("expired",)]


@pytest.mark.parametrize(("own_job", "command"), [
    (True, "rm -rf {home}/dev/app/build"),
    (True, "rm -f {scratch}/a.png; rm -rf {home}/dev/app/build"),
    # A session with no job folder has no scratch folder.
    (False, "rm -f {scratch}/a.png"),
])
def test_ask_leaves_other_deletions_to_user(
    world: World, monkeypatch, capsys, tmp_path: Path, claude_accounts, own_job: bool, command: str
) -> None:
    home, folder = tmp_path / "home", claude_accounts[0].folder.name
    scratch = job_folder(home, folder, SESSION_A) if own_job else home / folder / "jobs" / SESSION_A[:8] / "tmp"
    scratch_hook(world, monkeypatch, home, command.format(scratch=scratch, home=home))
    monkeypatch.setattr(permissions, "ASK_WAIT_SECONDS", 0)
    assert cli.main(["permission", "ask", "--as", "permission-hook"]) == 0
    assert capsys.readouterr().out == ""
    assert settled_state(world) == "expired"
