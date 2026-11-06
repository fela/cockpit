"""What a call on the page does, in Claude's words, from the session's transcript and the files the call runs
(SPEC PERM-3, explain.py)."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cockpit import cli, codex_agent, config, db, explain, permissions, sessions, sidechat
from tests.conftest import SESSION_A, World
from tests.test_api import agent_client
from tests.test_block_cause import typed, write_transcript
from tests.test_permission_requests import report, state

NOW = datetime(2026, 1, 15, 11, 0, tzinfo=UTC)
REFUSAL = {
    "command": "cd ~/dev/app/scripts && bash -c 'timeout 30 node tasks.js export-list --format unknown 2>&1 | head -5'",
    "description": "Check how Tasks reports an unsupported export format",
}
ANSWER = {
    "heading": "Check the export format error",
    "plain": "The session checks the Tasks export command with an unsupported format. The command reports the"
    " accepted formats before it opens the output file.",
    "sentence": "Sessions may check the Tasks export command with an unsupported format.",
    "moved_on": "",
}


def soon() -> datetime:
    """A moment just after the requests a test reports now: explain_next reads a refusal only once it is
    permissions.HOLD_SECONDS old, here 0."""
    return datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1)


def stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def scripts_folder(home: Path) -> Path:
    """Tasks' command dispatcher, with a separate entry point and README for each command."""
    folder = home / "dev/app/scripts"
    target = folder / "src/export-list"
    target.mkdir(parents=True)
    (folder / "tasks.js").write_text("#!/usr/bin/env node\n/**\n * Tasks commands: lists, reminders, sharing and exports.\n */\n")
    (target / "index.ts").write_text(
        "/**\n * Export a Tasks list as CSV or JSON.\n */\n"
        "if (!['csv', 'json'].includes(format)) throw new Error('Choose CSV or JSON');\n"
    )
    (target / "README.md").write_text("# export-list\n\nChoose CSV or JSON for a list export.\n")
    return folder


def call(at: str, tool_use_id: str, tool_input: dict[str, Any], says: str = "") -> dict[str, Any]:
    content = ([{"type": "text", "text": says}] if says else []) + [
        {"type": "tool_use", "id": tool_use_id, "name": "Bash", "input": tool_input}
    ]
    return {"type": "assistant", "timestamp": at, "message": {"role": "assistant", "content": content}}


def result(at: str, tool_use_id: str, text: str) -> dict[str, Any]:
    return {
        "type": "user", "timestamp": at,
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": text}]},
    }


PREVIEW = {"command": "node tasks.js export-list --format csv --preview", "description": "Preview a list export"}
PREVIEW_OUTPUT = "List: Weekend\n10 tasks ready for CSV export."


def session_transcript(folder: Path) -> None:
    write_transcript(
        folder,
        typed("2026-01-15T10:00:00.000Z", "Add CSV and JSON list exports to Tasks (ENG-123)."),
        call("2026-01-15T10:10:00.000Z", "toolu_preview", PREVIEW, says="Preview the CSV export first."),
        result("2026-01-15T10:20:00.000Z", "toolu_preview", PREVIEW_OUTPUT),
        call("2026-01-15T10:30:00.000Z", "toolu_1", REFUSAL),
        call("2026-01-15T11:00:00.000Z", "toolu_later", {"command": "git commit -m done"}),
    )


def report_refusal(world: World) -> dict[str, Any]:
    body = {
        "kind": "denied", "session_id": SESSION_A, "cwd": "/home/sam/dev", "tool_name": "Bash",
        "tool_input": REFUSAL, "tool_use_id": "toolu_1", "reason": "[Modify Shared Resources]",
    }
    response = agent_client(world).post("/api/permissions/requests", json=body, headers=world.agent("permission-hook"))
    assert response.status_code == 200, response.text
    return response.json()


def test_the_scripts_a_command_runs_follow_cd_bash_c_and_a_dispatcher(tmp_path: Path) -> None:
    home = tmp_path
    folder = scripts_folder(home)
    found = permissions.call_scripts(REFUSAL, str(home / "dev/app"), home)
    assert found == [folder / "tasks.js", folder / "src/export-list/index.ts", folder / "src/export-list/README.md"]
    # A first argument that names nothing beside the script, or a flag, adds nothing.
    assert permissions.call_scripts({"command": f"node {folder}/tasks.js --help"}, str(home), home) == [folder / "tasks.js"]
    assert permissions.call_scripts({"command": f"cd {folder} && node tasks.js other"}, "/", home) == [folder / "tasks.js"]


def test_cockpits_and_codexs_sign_in_secrets_are_never_read_for_the_explanation(tmp_path: Path) -> None:
    home = tmp_path
    # the home folder's other credential files too, and keys anywhere.
    secrets = [".config/cockpit/cookie-key", ".config/cockpit/token", ".config/cockpit/google-oauth-client.json", ".codex/auth.json",
               ".codex-2/auth.json", ".npmrc", ".netrc", ".git-credentials", ".config/git/credentials", ".pypirc", ".aws/credentials",
               ".gnupg/private-keys-v1.d/key.key", ".kube/config", ".docker/config.json", "deploy/server.pem"]
    for name in [*secrets, ".config/cockpit/setup.toml"]:
        (home / name).parent.mkdir(parents=True, exist_ok=True)
        (home / name).write_text("signing-key-0123456789")
    request = {"tool_name": "Bash", "cwd": str(home), "created_at": "2026-01-20T10:00:00+00:00",
               "tool_input": {"command": " && ".join(f"python3 ~/{name}" for name in [".config/cockpit/setup.toml", *secrets])}}
    assert [permissions.showable(home / name, home) for name in secrets] == [None] * len(secrets)
    shown = explain._files(request, home)
    assert "setup.toml" in shown and shown.count("signing-key-0123456789") == 1


def test_a_link_is_no_way_round_the_credential_places(tmp_path: Path) -> None:
    # a credential file that is a link, a credential folder that is one, and a link
    # to a key under another name.
    home = tmp_path
    (home / "dotfiles/aws").mkdir(parents=True)
    for name in ("dotfiles/npm-config", "dotfiles/aws/credentials", "deploy/server-key"):
        (home / name).parent.mkdir(parents=True, exist_ok=True)
        (home / name).write_text("signing-key-0123456789")
    (home / ".npmrc").symlink_to(home / "dotfiles/npm-config")
    (home / ".aws").symlink_to(home / "dotfiles/aws")
    (home / "server.key").symlink_to(home / "deploy/server-key")
    for name in (".npmrc", "dotfiles/npm-config", ".aws/credentials", "dotfiles/aws/credentials", "server.key"):
        assert permissions.showable(home / name, home) is None, name
    assert permissions.showable(home / "deploy/server-key", home) == home / "deploy/server-key"
    # A link inside a credential folder closes the file it points to.
    (home / ".kube").mkdir()
    (home / "dotfiles/kube.yaml").write_text("token: kube-0123456789")
    (home / ".kube/config").symlink_to(home / "dotfiles/kube.yaml")
    (home / ".docker/contexts").mkdir(parents=True)
    (home / "dotfiles/docker").mkdir()
    (home / ".docker/contexts/meta").symlink_to(home / "dotfiles/docker")
    (home / "dotfiles/docker/config.json").write_text("{}")
    # At any depth.
    (home / "dotfiles/aws/sso/cache").mkdir(parents=True)
    (home / "dotfiles/aws-sso.json").write_text('{"accessToken": "sso-0123456789"}')
    (home / "dotfiles/aws/sso/cache/session.json").symlink_to(home / "dotfiles/aws-sso.json")
    # Inside a folder a link leads to, and through a link loop that ends.
    (home / "vault").mkdir()
    (home / "vault/token.json").write_text('{"accessToken": "sso-0123456789"}')
    (home / "aws-cache").mkdir()
    (home / "aws-cache/token.json").symlink_to(home / "vault/token.json")
    (home / "aws-cache/again").symlink_to(home / "dotfiles/aws")
    (home / "dotfiles/aws/cache").symlink_to(home / "aws-cache")
    for name in ("dotfiles/kube.yaml", "dotfiles/docker/config.json", "dotfiles/aws-sso.json", "vault/token.json"):
        assert permissions.showable(home / name, home) is None, name
    # A hard link is another name for a credential file, a place's own or one in its folder.
    (home / ".config/git").mkdir(parents=True)
    (home / ".config/git/credentials").write_text("https://sam:ghp_0123456789@github.com")
    (home / ".ssh").mkdir()
    (home / ".ssh/id_ed25519").write_text("signing-key-0123456789")
    os.link(home / ".config/git/credentials", home / "review.py")
    os.link(home / ".ssh/id_ed25519", home / "deploy.py")
    (home / "notes.md").write_text("notes")
    os.link(home / "notes.md", home / "notes-again.md")
    request = {"tool_name": "Bash", "cwd": str(home), "created_at": "2026-01-20T10:00:00+00:00",
               "tool_input": {"command": "python3 ~/review.py && python3 ~/deploy.py"}}
    assert "ghp_0123456789" not in explain._files(request, home) and permissions.showable(home / "deploy.py", home) is None
    assert permissions.showable(home / "notes-again.md", home) == home / "notes-again.md"
    # A credential folder it cannot list closes every file.
    assert permissions.showable(home / "notes.md", home) == home / "notes.md"
    (home / "aws-cache").chmod(0o111)
    try:
        assert permissions.showable(home / "notes.md", home) is None
    finally:
        (home / "aws-cache").chmod(0o755)


@pytest.mark.parametrize("layout", ["direct", "root-alias", "nested-alias", "logs-alias"])
def test_google_cloud_files_and_aliases_stay_out_of_explanations_and_ask_reads(tmp_path: Path, layout: str) -> None:
    home = tmp_path
    cloud = home / ".config/gcloud"
    target = cloud if layout == "direct" else home / "dotfiles/cloud"
    target.mkdir(parents=True)
    data = target / "settings.py"
    data.write_text("# Synthetic cloud settings\n")
    if layout != "direct":
        link = cloud if layout == "root-alias" else cloud / ("logs" if layout == "logs-alias" else "cache")
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
        named = link / data.name
    else:
        named = data
    (home / "tasks.py").write_text("# List tasks\n")
    request = {"tool_name": "Bash", "cwd": str(home), "created_at": "2026-01-20T10:00:00+00:00",
               "tool_input": {"command": f"python3 {named} && python3 {data} && python3 ~/tasks.py"}}
    assert permissions.showable(named, home) is None
    assert permissions.showable(data, home) is None
    shown = explain._files(request, home)
    assert "# List tasks" in shown
    assert "Synthetic cloud settings" not in shown
    denied = sidechat.denied_reads(home)
    assert {"~/.config/gcloud", "~/.config/gcloud/**", f"/{target}", f"/{target}/**"} <= set(denied)


def test_a_token_file_is_closed_at_any_depth(tmp_path: Path) -> None:
    # A project's .npmrc holds a token as much as the home folder's does.
    home = tmp_path
    for name in ("dev/app/.npmrc", "dev/app/.pypirc", "dev/app/.git-credentials", "dev/app/.netrc"):
        (home / name).parent.mkdir(parents=True, exist_ok=True)
        (home / name).write_text("//registry.npmjs.org/:_authToken=npm_0123456789")
        assert permissions.showable(home / name, home) is None, name
        assert f"**/{Path(name).name}" in sidechat.SECRET_FILES
    (home / "dev/app/npmrc.md").write_text("notes")
    assert permissions.showable(home / "dev/app/npmrc.md", home) == home / "dev/app/npmrc.md"
    # a link loop among the credential places keeps the file closed, and raises nothing.
    (home / ".npmrc").symlink_to(home / ".npmrc")
    assert permissions.showable(home / "dev/app/npmrc.md", home) is None


@pytest.mark.parametrize(
    ("command", "credential"),
    [
        ("gh auth token", "gh auth token"),
        ("kubectl config view --raw", "kubectl config view --raw"),
        ("bash -lc 'kubectl config view --raw'", "kubectl config view --raw"),
        ("bash -ec 'kubectl config view --raw'", "kubectl config view --raw"),
        ("env VAR=x kubectl config view --raw", "kubectl config view --raw"),
        ("sudo kubectl config view --raw", "kubectl config view --raw"),
        ("sudo -u ana kubectl config view --raw", "kubectl config view --raw"),
        ("sudo -n bash -c 'kubectl config view --raw'", "kubectl config view --raw"),
        ('echo "$(kubectl config view --raw --minify)"', "kubectl config view --raw --minify"),
        ('echo `kubectl config view --raw --minify`', "kubectl config view --raw --minify"),
        ('printf "%s" "$(kubectl config view --raw)"', "kubectl config view --raw"),
        ("printenv GITHUB_TOKEN", "$GITHUB_TOKEN"),
        ("az account get-access-token", "az account get-access-token"),
        ("aws configure export-credentials", "aws configure export-credentials"),
        ("git credential fill", "git credential fill"),
        ("tasks config view --raw=true", "tasks config view --raw=true"),
        ("tasks config view --flatten", "tasks config view --flatten"),
        ("tasks auth status --show-token", "tasks auth status --show-token"),
        ("tasks auth status -t", "tasks auth status -t"),
        ("gh auth status -t=true", "gh auth status -t=true"),
        ("gh auth status -t=1", "gh auth status -t=1"),
        ("gh auth status -at=true", "gh auth status -at=true"),
        ("tasks --hostname example.test auth token", "tasks --hostname example.test auth token"),
        ("tasks --quiet auth print-identity-token", "tasks --quiet auth print-identity-token"),
        ("tasks secrets versions access latest", "tasks secrets versions access latest"),
        ("tasks get-secret-value", "tasks get-secret-value"),
        ("tasks kv get tasks", "tasks kv get tasks"),
        ("tasks auth print-access-token", "tasks auth print-access-token"),
        ('kubectl get pods --token "$TOKEN"', "$TOKEN"),
        ("npm publish --userconfig ../tasks/.npmrc", ".npmrc"),
        ("cat /srv/tasks/.env", ".env"),
        ("cat /srv/tasks/.env.local", ".env.local"),
        ("cat /srv/tasks/service.key", "service.key"),
        ("cat /srv/tasks/service.pem", "service.pem"),
    ],
)
def test_secret_sources_explain_the_block_and_the_earlier_call(world: World, command: str, credential: str) -> None:
    tool_input = {"command": command}
    request = report(world, "denied", tool_input, reason="[Credential Exploration]", cwd="/srv/tasks")
    home = Path("/home/ana")
    allowed = permissions.suggest_sentences("Bash", tool_input, "/srv/tasks", home)
    assert allowed == []
    allowed = [permissions.GITHUB_WORK, permissions.read_work(home)]
    conn = db.connect(world.db_path)
    try:
        stored = db.row(conn.execute("SELECT * FROM permission_requests WHERE id = ?", (request["id"],)).fetchone())
        stored["explanation"] = {"sentence": "Sessions may inspect the Tasks service configuration."}
        shown = permissions.shown(conn, stored, home, allowed)
        text = explain._stop(conn, stored, home, allowed)
    finally:
        conn.close()
    assert shown["action_sentence"] is None
    assert shown["covered_by"] == []
    assert f"The call itself reaches for credentials: {credential}." in text
    assert "already allow calls like this one" not in text
    earlier = permissions.earlier_call(
        [
            {"tool_name": "Bash", "tool_input": {"command": "git status"}, "at": "2026-01-15T10:02:00Z"},
            {"tool_name": "Bash", "tool_input": tool_input, "at": "2026-01-15T10:01:00Z"},
        ],
        "Credential Exploration", home,
    )
    assert earlier is not None
    assert earlier["summary"] == command
    assert earlier["credentials"] == [credential]


@pytest.mark.parametrize("command", [
    "cat .ssh/id_rsa", "cd .ssh && cat id_rsa", "cd tasks/../.ssh && cat id_rsa",
    "cd .ssh; cat id_rsa", "pwd && cd .ssh && cat id_rsa", "cd .ssh\ncat id_rsa",
    "cd tasks; cd ../.ssh; cat id_rsa", "cd .ssh; cat id_rsa; cd ..; cat README.md",
])
def test_relative_credential_paths_appear_in_the_explanation(world: World, command: str) -> None:
    home = Path("/home/ana")
    request = report(world, "denied", {"command": command}, cwd=str(home), reason="[Credential Exploration]")
    conn = db.connect(world.db_path)
    try:
        text = explain._stop(conn, request, home, [])
    finally:
        conn.close()
    assert "The call itself reaches for credentials:" in text
    assert "~/.ssh/id_rsa" in text
    assert "This call reaches for no credentials" not in text


def test_literal_credential_words_keep_the_actual_earlier_source_in_the_explanation(world: World) -> None:
    home = Path("/home/ana")
    literal = {"command": "echo 'tasks auth token'"}
    assert permissions.credentials_in(literal, home) == []
    earlier = permissions.earlier_call([
        {"tool_name": "Bash", "tool_input": literal, "at": "2026-01-15T10:02:00Z"},
        {"tool_name": "Bash", "tool_input": {"command": "gh auth token"}, "at": "2026-01-15T10:01:00Z"},
    ], "Credential Exploration", home)
    assert earlier is not None and earlier["credentials"] == ["gh auth token"]
    request = report(world, "denied", literal, cwd="/srv/tasks", reason="[Credential Exploration]")
    request["earlier_call"] = earlier
    request["explanation"] = {"sentence": "Sessions may print task descriptions."}
    request["proposed_sentence"] = "Sessions may print task descriptions."
    conn = db.connect(world.db_path)
    try:
        shown = permissions.shown(conn, request, home, [permissions.read_work(home)])
        text = explain._stop(conn, request, home, [])
    finally:
        conn.close()
    assert shown["suggested_sentences"] == []
    assert shown["action_sentence"] is None
    assert shown["covered_by"] == []
    assert "This call reaches for no credentials" in text
    assert "which reached for gh auth token" in text
    assert "The call itself reaches for credentials:" not in text


def test_claude_reads_what_each_path_the_call_deletes_is(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path
    work = home / "dev/app"
    (work / "build").mkdir(parents=True)
    (work / "build/a.o").write_text("x")
    (work / "notes.txt").write_text("hello")
    (work / ".venv").symlink_to("../app-main/.venv")
    command = (
        "cd ~/dev/app && rm .venv 2>/dev/null && timeout 9 rm -rf -- build notes.txt gone *.tmp \"$HOME/x\""
        " && bash -c 'rm -f notes.txt' && sudo -n env A=1 rm -f gone2 > log && git add -A"
    )
    request = {"tool_name": "Bash", "cwd": str(home), "tool_input": {"command": command}}
    assert permissions.call_removals(request["tool_input"], str(home), home) == [
        work / ".venv", work / "build", work / "notes.txt", work / "gone", work / "gone2",
    ]
    text = explain._removals(request, home)
    assert text.startswith("# What the call deletes, as Cockpit found it at ")
    assert text.splitlines()[2:] == [
        f"- {work / '.venv'}: a link to ../app-main/.venv",
        f"- {work / 'build'}: a folder with 1 entry",
        f"- {work / 'notes.txt'}: a file of 5 bytes",
        f"- {work / 'gone'}: not there",
        f"- {work / 'gone2'}: not there",
    ]
    # Only paths the command spells out count; where the shell decides, Cockpit names nothing.
    for unsure, named in [
        ("env --chdir=/tmp rm -rf build", []),
        ("sudo -u root rm -rf build", []),
        ("env -u PYTHONPATH rm -rf build", []),
        ("cd $TARGET && rm -rf build", []),
        ("rm -f build$(date +%s)", []),
        ("rm -f {one,two} build", [work / "build"]),
        ("rm -f build # keep; rm -f notes.txt", []),
        ("env HOME=/tmp bash -c 'rm -f ~/build'", []),
        ("cd -- $TARGET && rm -f build", []),
        ("env -0 rm -f build", []),
        ("env -- -i rm -f build", []),
        ("cd -- sub && rm -f build", [work / "sub" / "build"]),
        ("rm -f 123 > log", [work / "123"]),
        ("rm -f build 2>/dev/null", [work / "build"]),
        ("sudo -n bash -c 'rm -rf build'", [work / "build"]),
        # the parse lost the subshell's end and the quotes, so it named only /tmp/cache, and home.
        ("(cd /tmp; rm -rf cache); rm -rf cache", []),
        ("rm -rf '~/build'", []),
        ("rm -rf \\~/build", []),
        ('rm -rf ~"/build"', []),
        ("rm -rf ~\\/build", []),
        ("rm -rf ~\\/", []),
        ('rm -rf "~"', []),
    ]:
        assert permissions.call_removals({"command": unsure}, str(work), home) == named, unsure
    monkeypatch.setattr(explain, "ENTRIES_MAX", 0)
    assert f"- {work / 'build'}: a folder with more than 0 entries" in explain._removals(request, home)
    assert explain._removals(request | {"tool_input": {"command": "git status"}}, home) == ""
    assert explain._removals(request | {"tool_name": "Write"}, home) == ""


def test_the_card_shows_what_the_script_a_dispatcher_runs_says(tmp_path: Path) -> None:
    home = tmp_path
    scripts_folder(home)
    changes = permissions.call_changes("Bash", REFUSAL, str(home / "dev/app"), home, "2026-01-01T00:00:00Z")
    assert [change["title"] for change in changes] == [
        "Runs ~/dev/app/scripts/tasks.js, which says:",
        "Runs ~/dev/app/scripts/src/export-list/index.ts, which says:",
    ]
    assert changes[1]["text"] == "Export a Tasks list as CSV or JSON."


def test_the_transcript_before_a_call_ends_with_it_and_starts_with_the_instruction(transcripts: Path) -> None:
    session_transcript(transcripts)
    parts = sessions.transcript_before(SESSION_A, "toolu_1", 10_000)
    assert [(part["at"], part["kind"]) for part in parts] == [
        ("2026-01-15T10:00:00Z", "instruction"),
        ("2026-01-15T10:10:00Z", "says"),
        ("2026-01-15T10:10:00Z", "call"),
        ("2026-01-15T10:20:00Z", "result"),
        ("2026-01-15T10:30:00Z", "call"),
    ]
    assert parts[0]["author"] == "user"
    assert parts[3]["text"] == PREVIEW_OUTPUT
    assert json.loads(parts[4]["text"]) == REFUSAL
    # Over the budget, the instruction before the call still comes first.
    short = sessions.transcript_before(SESSION_A, "toolu_1", 10)
    assert [part["kind"] for part in short] == ["instruction", "call"]
    assert sessions.transcript_before(SESSION_A, "toolu_missing", 10_000) is None


def test_claude_reads_the_call_its_files_the_session_and_the_global_instructions(
    world: World, transcripts: Path, tmp_path: Path
) -> None:
    home = tmp_path
    scripts_folder(home)
    session_transcript(transcripts)
    (home / "AGENTS.md").write_text("Run the Tasks service locally with make serve.\n")
    request = report_refusal(world)
    conn = db.connect(world.db_path)
    try:
        conn.execute("UPDATE permission_requests SET cwd = ? WHERE id = ?", (str(home / "dev/app"), request["id"]))
        conn.execute(
            "INSERT INTO items (key, title, summary, sessions, created_at, updated_at, updated_by)"
            " VALUES ('linear:ENG-123', 'Tasks: export a list', 'CSV preview ready', ?, ?, ?, 'agent:test')",
            (json.dumps([SESSION_A]), "2026-01-15T10:00:00Z", "2026-01-15T10:00:00Z"),
        )
        stored = db.row(conn.execute("SELECT * FROM permission_requests WHERE id = ?", (request["id"],)).fetchone())
        text = explain.material(conn, stored, home, [])
        # The user's sentences that already allow calls like it (PERM-11), here for a PR comment the same way.
        comment = {"command": "gh pr comment 10 --body hi", "description": "Comment on the PR"}
        allowed = permissions.suggest_sentences("Bash", comment, stored["cwd"], home)
        covered = explain._stop(conn, stored | {"tool_input": comment}, home, allowed)
        # A dialog has no auto-mode label to explain.
        assert explain._stop(conn, stored | {"kind": "asked"}, home, allowed) == ""
    finally:
        conn.close()
    assert "Auto mode stopped it with the label [Modify Shared Resources]." in text
    # Cockpit's own reading of the cause (PERM-13).
    assert "# Why auto mode stopped it\n\nAuto mode gives only a label." in text
    assert "Auto mode stopped it as a change to something other people or services share (“Modify Shared Resources”)." in text
    assert "already allow calls like this one" not in text
    assert allowed and f"already allow calls like this one, so the stop came from something else:\n- {allowed[0]}" in covered
    assert "Check how Tasks reports an unsupported export format" in text
    assert "Choose CSV or JSON" in text
    assert "Choose CSV or JSON for a list export." in text
    assert "- Tasks: export a list: CSV preview ready" in text
    assert "] Instruction from Sam:\nAdd CSV and JSON list exports to Tasks (ENG-123)." in text
    assert PREVIEW_OUTPUT in text
    # What the session did after the call comes after it, under its own heading (PERM-18).
    assert text.index("# The session's transcript after the call") < text.index("git commit -m done")
    assert "make serve" in text


def test_each_open_call_is_explained_once_and_a_failure_shows_on_the_card(
    world: World, transcripts: Path, tmp_path: Path
) -> None:
    home = tmp_path
    (home / "AGENTS.md").write_text("Run the Tasks service locally with make serve.\n")
    session_transcript(transcripts)
    request = report_refusal(world)
    assert state(world)["permission_requests"][0]["explanation"] is None
    asked: list[str] = []

    def ask(text: str) -> dict[str, str]:
        asked.append(text)
        return ANSWER

    now = soon()
    conn = db.connect(world.db_path)
    try:
        assert explain.explain_next(conn, home, world.claude_settings, ask, now, {}) == request["id"]
        assert explain.explain_next(conn, home, world.claude_settings, ask, now, {}) is None
    finally:
        conn.close()
    assert len(asked) == 1
    assert state(world)["permission_requests"][0]["explanation"] == ANSWER | {"count": 1}

    other = agent_client(world).post("/api/permissions/requests", json={
        "kind": "denied", "session_id": SESSION_A, "cwd": "/home/sam", "tool_name": "Bash",
        "tool_input": {"command": "make deploy"}, "tool_use_id": "toolu_2", "reason": "[Production Deploy]",
    }, headers=world.agent("permission-hook")).json()

    def fail(text: str) -> dict[str, str]:
        raise explain.ExplainError("the work Claude account is signed out", "work: claude exited with 1: {...}")

    now = soon()
    conn = db.connect(world.db_path)
    try:
        assert explain.explain_next(conn, home, world.claude_settings, fail, now, {}) == other["id"]
        # PERM-17: the card says why in one sentence, and Cockpit asks again RETRY_MINUTES later, not before.
        later = now + timedelta(minutes=explain.RETRY_MINUTES)
        assert explain.explain_next(conn, home, world.claude_settings, ask, later - timedelta(seconds=1), {}) is None
        cards = {card["id"]: card for card in state(world)["permission_requests"]}
        assert cards[other["id"]]["explanation"] == {
            "error": "No explanation yet: the work Claude account is signed out. Cockpit tries again every 10 minutes.",
            "failed_at": stamp(now),
        }
        assert explain.explain_next(conn, home, world.claude_settings, ask, later, {}) == other["id"]
    finally:
        conn.close()
    cards = {card["id"]: card for card in state(world)["permission_requests"]}
    assert cards[other["id"]]["explanation"] == ANSWER | {"count": 1}
    assert len(asked) == 2


def test_an_error_stored_before_retries_existed_is_tried_again_after_a_new_call(
    world: World, transcripts: Path, tmp_path: Path
) -> None:
    (tmp_path / "AGENTS.md").write_text("Run the Tasks service locally with make serve.\n")
    session_transcript(transcripts)
    old = report_refusal(world)
    conn = db.connect(world.db_path)
    try:
        conn.execute(
            "UPDATE permission_requests SET explanation = ? WHERE id = ?",
            (json.dumps({"error": "claude exited with 1: {\"type\":\"result\"}"}), old["id"]),
        )
        newer = agent_client(world).post("/api/permissions/requests", json={
            "kind": "denied", "session_id": SESSION_A, "cwd": "/home/sam", "tool_name": "Bash",
            "tool_input": {"command": "make deploy"}, "tool_use_id": "toolu_2", "reason": "[Production Deploy]",
        }, headers=world.agent("permission-hook")).json()
        # A call never tried comes first; the old error is tried next.
        now = soon()
        assert explain.explain_next(conn, tmp_path, world.claude_settings, lambda text: ANSWER, now, {}) == newer["id"]
        assert explain.explain_next(conn, tmp_path, world.claude_settings, lambda text: ANSWER, now, {}) == old["id"]
    finally:
        conn.close()
    assert all(card["explanation"]["plain"] == ANSWER["plain"] for card in state(world)["permission_requests"])


def test_a_dialog_whose_hook_gave_up_is_tried_again_while_its_session_waits_at_it(world: World, tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("Run the Tasks service locally with make serve.\n")
    request = agent_client(world).post("/api/permissions/requests", json={
        "kind": "asked", "session_id": SESSION_A, "cwd": "/home/sam", "tool_name": "Bash",
        "tool_input": {"command": "git push --tags"}, "tool_use_id": "toolu_1", "reason": "",
    }, headers=world.agent("permission-hook")).json()
    agent_client(world).post(
        f"/api/permissions/requests/{request['id']}/settle", json={"state": "expired"}, headers=world.agent()
    )
    opened = datetime.fromisoformat(request["created_at"])
    later = opened + timedelta(hours=25)
    conn = db.connect(world.db_path)
    try:
        explain.save(conn, request["id"], explain._failure("No explanation yet: Claude's run failed", opened))
        assert explain.explain_next(conn, tmp_path, world.claude_settings, lambda text: ANSWER, later, {}) is None
        assert explain.explain_next(conn, tmp_path, world.claude_settings, lambda text: ANSWER, later, {SESSION_A: opened}) == request["id"]
        assert db.row(conn.execute(
            "SELECT explanation FROM permission_requests WHERE id = ?", (request["id"],)
        ).fetchone())["explanation"] == ANSWER | {"count": 1}
    finally:
        conn.close()


def test_a_refusal_waits_a_minute_and_one_the_session_got_past_folds_away_until_it_calls_again(
    world: World, transcripts: Path, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(permissions, "HOLD_SECONDS", 60)
    monkeypatch.setattr(permissions, "HOLD_MAX_SECONDS", 240)
    (tmp_path / "AGENTS.md").write_text("Run the Tasks service locally with make serve.\n")
    write_transcript(
        transcripts,
        typed("2026-01-15T10:00:00.000Z", "Fix the flaky delivery test."),
        call("2026-01-15T10:10:00.000Z", "toolu_1", REFUSAL),
        call("2026-01-15T10:10:05.000Z", "toolu_read", {"file_path": "/home/sam/dev/app/scripts/tasks.js"},
             says="Auto mode stopped that; I'll read the file with the Read tool instead."),
        result("2026-01-15T10:10:06.000Z", "toolu_read", "#!/usr/bin/env node"),
    )
    request = report_refusal(world)
    created = datetime.fromisoformat(request["created_at"])
    asked: list[str] = []

    def ask(text: str) -> dict[str, str]:
        asked.append(text)
        return ANSWER | {"moved_on": "It read the file with the Read tool instead."}

    def cards(now: datetime) -> list[dict[str, Any]]:
        return [card for card in permissions.open_requests(conn, now, tmp_path, [], {}) if not card["held"]]

    conn = db.connect(world.db_path)
    try:
        # For its first minute, the card waits and Claude does not read it yet.
        early = created + timedelta(seconds=30)
        assert cards(early) == []
        assert explain.explain_next(conn, tmp_path, world.claude_settings, ask, early, {}) is None
        # Unread after a minute, it still waits, up to HOLD_MAX_SECONDS.
        assert cards(created + timedelta(seconds=61)) == []
        assert [card["id"] for card in cards(created + timedelta(seconds=240))] == [request["id"]]
        read_at = created + timedelta(seconds=61)
        assert explain.explain_next(conn, tmp_path, world.claude_settings, ask, read_at, {}) == request["id"]
        # Claude read what the session did after the call.
        after = asked[0][asked[0].index("# The session's transcript after the call, oldest first"):]
        assert "I'll read the file with the Read tool instead." in after and "Its turn" in after
        [card] = cards(read_at)
        assert card["moved_past"] is True
    finally:
        conn.close()
    # While the page holds the card back, the session still finds its call in its own list, to withdraw it.
    page = state(world)
    assert page["permission_requests"] == [] and page["moved_past"] == []
    assert f"request {request['id']}," in cli.permission_list(page, SESSION_A)
    # Once shown, the page lists it apart, out of the Blocked count, and so does the session's list.
    monkeypatch.setattr(permissions, "HOLD_SECONDS", 0)
    page = state(world)
    assert page["permission_requests"] == [] and page["counts"]["blocked"] == 0
    assert [card["id"] for card in page["moved_past"]] == [request["id"]]
    assert f"request {request['id']}," in cli.permission_list(page, SESSION_A)
    # The session makes the call again, in the same second as Claude's reading or later: it still needs it, and
    # the card is back with the others.
    again = agent_client(world).post("/api/permissions/requests", json={
        "kind": "denied", "session_id": SESSION_A, "cwd": "/home/sam/dev", "tool_name": "Bash",
        "tool_input": REFUSAL, "tool_use_id": "toolu_2", "reason": "[Modify Shared Resources]",
    }, headers=world.agent("permission-hook")).json()
    assert again["id"] == request["id"]
    page = state(world)
    assert [card["id"] for card in page["permission_requests"]] == [request["id"]] and page["moved_past"] == []


def test_a_dialog_is_shown_at_once_and_never_folds_away(world: World, monkeypatch) -> None:
    monkeypatch.setattr(permissions, "HOLD_SECONDS", 60)
    monkeypatch.setattr(permissions, "HOLD_MAX_SECONDS", 240)
    request = agent_client(world).post("/api/permissions/requests", json={
        "kind": "asked", "session_id": SESSION_A, "cwd": "/home/sam", "tool_name": "Bash",
        "tool_input": {"command": "git push --tags"}, "tool_use_id": "toolu_1", "reason": "",
    }, headers=world.agent("permission-hook")).json()
    [card] = state(world)["permission_requests"]
    assert card["id"] == request["id"] and card["moved_past"] is False
    # Even when Claude's reading says the session moved on, its session still waits at the dialog.
    conn = db.connect(world.db_path)
    try:
        explain.save(conn, request["id"], ANSWER | {"moved_on": "It pushed the branch alone.", "count": 1})
    finally:
        conn.close()
    page = state(world)
    assert [card["id"] for card in page["permission_requests"]] == [request["id"]] and page["moved_past"] == []


def test_each_account_is_tried_and_the_card_names_each_cause_once(monkeypatch) -> None:
    tried: list[str] = []

    def ask(account: config.Account, text: str) -> dict[str, str]:
        tried.append(account.name)
        if account.name == "work":
            raise explain.ExplainError(explain._why_failed(account, "Failed to authenticate: OAuth session expired"), "raw")
        return ANSWER

    monkeypatch.setattr(explain, "ask_claude", ask)
    assert explain.ask_accounts([config.ACCOUNTS[1], config.ACCOUNTS[0]], "the material") == ANSWER
    assert tried == ["work", config.ACCOUNTS[0].name]

    with pytest.raises(explain.ExplainError) as failed:
        explain.ask_accounts([config.ACCOUNTS[1], config.ACCOUNTS[1]], "the material")
    assert failed.value.plain == "the work Claude account is signed out"
    assert str(failed.value) == "raw; raw"


def test_with_every_account_signed_out_the_card_says_so_and_waits_to_try_again(monkeypatch) -> None:
    """the account pick's SignedOut went past explain_next, so the card kept saying Claude reads it and the
    worker tried again every few seconds."""
    from cockpit import server

    monkeypatch.setattr(server.accounts, "usable", lambda accounts, now: [])
    with pytest.raises(explain.ExplainError) as failed:
        server.explain_ask(codex_agent.ProviderSessions(None, None), "the material")
    assert failed.value.plain == "every Claude account is signed out"


def test_a_fault_while_gathering_the_material_shows_on_the_card_rather_than_retrying_for_ever(
    world: World, transcripts: Path, monkeypatch
) -> None:
    request = report_refusal(world)

    def broken(conn, request, home, auto_allowed):
        raise ValueError("bad timestamp")

    monkeypatch.setattr(explain, "material", broken)
    now = soon()
    conn = db.connect(world.db_path)
    try:
        assert explain.explain_next(conn, Path("/home/sam"), world.claude_settings, lambda text: ANSWER, now, {}) == request["id"]
        assert explain.explain_next(conn, Path("/home/sam"), world.claude_settings, lambda text: ANSWER, now, {}) is None
    finally:
        conn.close()
    assert state(world)["permission_requests"][0]["explanation"] == {
        "error": "Cockpit could not read what the call runs (bad timestamp). Cockpit tries again every 10 minutes.",
        "failed_at": stamp(now),
    }


def test_a_missing_global_instructions_file_leaves_the_section_out(world: World, transcripts: Path, tmp_path: Path) -> None:
    session_transcript(transcripts)
    request = report_refusal(world)
    conn = db.connect(world.db_path)
    try:
        assert explain.explain_next(conn, tmp_path, world.claude_settings, lambda text: ANSWER, soon(), {}) == request["id"]
    finally:
        conn.close()
    explanation = state(world)["permission_requests"][0]["explanation"]
    assert "error" not in explanation
    assert explain._global_instructions(tmp_path) == ""


def test_global_instructions_that_lead_to_a_credential_file_are_not_read(
    world: World, transcripts: Path, tmp_path: Path
) -> None:
    # ~/AGENTS.md is a file put into Claude's prompt, so it passes permissions.showable like the call's.
    session_transcript(transcripts)
    (tmp_path / ".aws").mkdir()
    (tmp_path / ".aws/credentials").write_text("aws_secret_access_key = hunter2\n")
    (tmp_path / "AGENTS.md").symlink_to(tmp_path / ".aws/credentials")
    report_refusal(world)
    read: list[str] = []
    conn = db.connect(world.db_path)
    try:
        explain.explain_next(conn, tmp_path, world.claude_settings, lambda text: read.append(text) or ANSWER, soon(), {})
    finally:
        conn.close()
    assert read == []
    error = state(world)["permission_requests"][0]["explanation"]["error"]
    assert error.startswith(f"Cockpit could not read what the call runs ({tmp_path / 'AGENTS.md'} leads to a file")


def test_a_call_answered_before_claude_got_to_it_is_left(world: World, transcripts: Path) -> None:
    request = report_refusal(world)
    world.client.post(f"/api/permissions/requests/{request['id']}/dismiss", json={}, headers=world.user())
    conn = db.connect(world.db_path)
    try:
        assert explain.explain_next(conn, Path("/home/sam"), world.claude_settings, lambda text: ANSWER, soon(), {}) is None
    finally:
        conn.close()


def fake_run(stdout: str, returncode: int = 0):
    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert args[args.index("--tools") + 1] == ""
        assert args[args.index("--setting-sources") + 1] == ""
        assert "--no-session-persistence" in args
        assert kwargs["input"] == "the material"
        # The one-shot runs on the account it was given: here the work one.
        assert f"CLAUDE_CONFIG_DIR={config.ACCOUNTS[1].folder}" in args
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")
    return run


def test_ask_claude_takes_the_structured_answer_and_refuses_any_other(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(explain, "WORK_DIR", tmp_path / "explain")
    answer = {"type": "result", "is_error": False, "structured_output": ANSWER, "duration_ms": 9000, "total_cost_usd": 0.2}
    monkeypatch.setattr(explain.subprocess, "run", fake_run(json.dumps(answer)))
    assert explain.ask_claude(config.ACCOUNTS[1], "the material") == ANSWER

    monkeypatch.setattr(explain.subprocess, "run", fake_run(json.dumps({"is_error": False, "result": "no form"})))
    with pytest.raises(explain.ExplainError, match="wrong form") as failed:
        explain.ask_claude(config.ACCOUNTS[1], "the material")
    assert failed.value.plain == "Claude's answer came back garbled"
    monkeypatch.setattr(explain.subprocess, "run", fake_run("[]"))
    with pytest.raises(explain.ExplainError, match="no JSON object") as failed:
        explain.ask_claude(config.ACCOUNTS[1], "the material")
    monkeypatch.setattr(explain.subprocess, "run", fake_run("Claude usage limit reached", returncode=1))
    with pytest.raises(explain.ExplainError, match="exited with 1: Claude usage limit reached") as failed:
        explain.ask_claude(config.ACCOUNTS[1], "the material")
    assert failed.value.plain == "the work Claude account is at its usage limit"
    expired = {"type": "result", "is_error": True, "result": "Failed to authenticate: OAuth session expired and could not be refreshed"}
    monkeypatch.setattr(explain.subprocess, "run", fake_run(json.dumps(expired), returncode=1))
    with pytest.raises(explain.ExplainError, match="OAuth session expired") as failed:
        explain.ask_claude(config.ACCOUNTS[1], "the material")
    assert failed.value.plain == "the work Claude account is signed out"
    # The cause comes from the run's message, not from counters around it such as depth_limit.
    other = {"type": "result", "is_error": True, "refused": {"depth_limit": 0}, "result": "API Error: 500"}
    monkeypatch.setattr(explain.subprocess, "run", fake_run(json.dumps(other), returncode=1))
    with pytest.raises(explain.ExplainError) as failed:
        explain.ask_claude(config.ACCOUNTS[1], "the material")
    assert failed.value.plain == "Claude's run on the work account failed"
    monkeypatch.setattr(explain.subprocess, "run", fake_run("Not logged in · Please run /login", returncode=1))
    with pytest.raises(explain.ExplainError) as failed:
        explain.ask_claude(config.ACCOUNTS[1], "the material")
    assert failed.value.plain == "the work Claude account is signed out"


AMEND = {
    "command": "git -C /home/sam/.claude/jobs/aaaaaaaa/tmp/app -c user.email=1+sam@users.noreply.github.com"
    " commit -q --amend --no-edit --reset-author",
}
AMEND_SENTENCE = "Sessions may amend their own unpushed commits and change their author name and email."


def test_claudes_sentence_for_the_blocked_action_leads_and_the_sentences_already_allowed_drop_out(world: World) -> None:
    body = {
        "kind": "denied", "session_id": SESSION_A, "cwd": "/home/sam/.claude/jobs/aaaaaaaa", "tool_name": "Bash",
        "tool_input": AMEND, "tool_use_id": "toolu_amend", "reason": "[Git Destructive]",
    }
    request = agent_client(world).post("/api/permissions/requests", json=body, headers=world.agent("permission-hook")).json()
    before = state(world)["permission_requests"][0]
    assert before["suggested_sentences"] == [permissions.JOB_FOLDER_WORK, permissions.GITHUB_WORK]
    assert before["action_sentence"] is None
    settings = json.loads(world.claude_settings.read_text())
    settings.setdefault("autoMode", {})["allow"] = ["$defaults", permissions.JOB_FOLDER_WORK, permissions.GITHUB_WORK]
    world.claude_settings.write_text(json.dumps(settings))
    assert state(world)["permission_requests"][0]["covered_by"] == [permissions.JOB_FOLDER_WORK, permissions.GITHUB_WORK]

    conn = db.connect(world.db_path)
    try:
        explain.save(conn, request["id"], {"plain": "The session fixes its commit's author email.", "sentence": AMEND_SENTENCE})
    finally:
        conn.close()
    shown = state(world)["permission_requests"][0]
    assert shown["action_sentence"] == AMEND_SENTENCE
    assert shown["suggested_sentences"] == [AMEND_SENTENCE]
    assert shown["covered_by"] == [permissions.JOB_FOLDER_WORK, permissions.GITHUB_WORK]


def test_an_empty_or_overlong_sentence_leaves_cockpits_suggestions(world: World) -> None:
    request = report_refusal(world)
    before = state(world)["permission_requests"][0]["suggested_sentences"]
    for sentence in ("", "x" * (permissions.SENTENCE_MAX + 1)):
        conn = db.connect(world.db_path)
        try:
            conn.execute("UPDATE permission_requests SET explanation = 'null' WHERE id = ?", (request["id"],))
            explain.save(conn, request["id"], {"plain": "Fine.", "sentence": sentence})
        finally:
            conn.close()
        shown = state(world)["permission_requests"][0]
        assert (shown["suggested_sentences"], shown["action_sentence"]) == (before, None)


def test_a_sentence_already_allowed_is_left_out_and_one_already_suggested_shows_once(world: World) -> None:
    body = {
        "kind": "denied", "session_id": SESSION_A, "cwd": "/home/sam/.claude/jobs/aaaaaaaa", "tool_name": "Bash",
        "tool_input": AMEND, "tool_use_id": "toolu_amend", "reason": "[Git Destructive]",
    }
    request = agent_client(world).post("/api/permissions/requests", json=body, headers=world.agent("permission-hook")).json()

    def shown_with(sentence: str) -> dict[str, Any]:
        conn = db.connect(world.db_path)
        try:
            conn.execute("UPDATE permission_requests SET explanation = 'null' WHERE id = ?", (request["id"],))
            explain.save(conn, request["id"], {"plain": "Fine.", "sentence": sentence})
        finally:
            conn.close()
        return state(world)["permission_requests"][0]

    # Claude names a sentence Cockpit suggests anyway: it leads, once.
    shown = shown_with(permissions.JOB_FOLDER_WORK)
    assert shown["suggested_sentences"] == [permissions.JOB_FOLDER_WORK, permissions.GITHUB_WORK]
    assert shown["action_sentence"] == permissions.JOB_FOLDER_WORK
    # Auto mode stopped the call under the sentence Claude names: adding it again would change nothing.
    settings = json.loads(world.claude_settings.read_text())
    settings.setdefault("autoMode", {})["allow"] = ["$defaults", AMEND_SENTENCE]
    world.claude_settings.write_text(json.dumps(settings))
    shown = shown_with(AMEND_SENTENCE)
    assert shown["suggested_sentences"] == [permissions.JOB_FOLDER_WORK, permissions.GITHUB_WORK]
    assert shown["action_sentence"] is None


def test_cockpit_offers_no_sentence_of_claudes_on_production_or_credentials() -> None:
    def request(command: str, sentence: str) -> dict[str, Any]:
        return {"kind": "denied", "tool_input": {"command": command}, "explanation": {"plain": "", "sentence": sentence}}

    harmless = "Sessions may run the Tasks export tests in their own job folder."
    assert permissions.action_sentence(request("python check.py --stage dev", harmless), [], []) == harmless
    # The call names production, though the sentence does not.
    for command in ("python export.py --stage prod", "python reminders.py --environment production"):
        assert permissions.action_sentence(request(command, harmless), [], []) is None
    assert permissions.PRODUCTION.search("python products.py --stage dev") is None
    # The call reaches for credentials.
    assert permissions.action_sentence(request("python check.py", harmless), ["~/.config/gh/credentials"], []) is None
    # The sentence names production or a secret.
    for sentence in (
        "Sessions may publish the Tasks production release.",
        "Sessions may print an API token.",
        "Sessions may read the credentials file to run the export check.",
    ):
        assert permissions.action_sentence(request("python check.py", sentence), [], []) is None
    # A dialog's call has no auto-mode sentence.
    assert permissions.action_sentence(request("python check.py", harmless) | {"kind": "dialog"}, [], []) is None


def test_unreadable_global_instructions_still_raise(tmp_path, monkeypatch):
    path = tmp_path / "AGENTS.md"
    path.write_text("Rules")
    read_text = Path.read_text

    def read(self, *args, **kwargs):
        if self == path:
            raise PermissionError("cannot read instructions")
        return read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    with pytest.raises(PermissionError, match="cannot read"):
        explain._global_instructions(tmp_path)
