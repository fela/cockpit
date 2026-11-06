from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cockpit import spec, store
from tests.conftest import ORIGIN, SPEC_TEXT, World


@pytest.mark.parametrize("clean", [True, False])
def test_first_document_save_pushes_a_branch_missing_from_remote(tmp_path, monkeypatch, clean):
    doc = spec.Doc(tmp_path, "SPEC.md", "spec:", "the spec", "Spec", "my-main", clean)
    calls = []

    def git(repo, args):
        assert repo == tmp_path
        calls.append(args)
        if args[0] == "rev-parse":
            return True, "my-main"
        if args[0] == "pull":
            return False, "couldn't find remote ref my-main"
        return True, ""

    monkeypatch.setattr(spec, "_git", git)
    spec._sync(doc)
    result = spec._save(doc, "before", "after", "Edit the spec")
    assert result["committed"] and result["pushed"]
    assert (tmp_path / doc.file).read_text() == "after"
    assert ["ls-remote", "--heads", "origin", "refs/heads/my-main"] in calls
    assert sum(args[0] == "ls-remote" for args in calls) == 2
    assert not any(args[0] == "pull" for args in calls)
    assert calls[-1] == ["push", "-q", "origin", "HEAD:my-main"]


@pytest.mark.parametrize("remote", ["exists", "error", "pull error"])
@pytest.mark.parametrize("clean", [True, False])
def test_document_pull_preserves_existing_branch_and_remote_errors(tmp_path, monkeypatch, remote, clean):
    doc = spec.Doc(tmp_path, "SPEC.md", "spec:", "the spec", "Spec", "my-main", clean)
    calls = []

    def git(repo, args):
        calls.append(args)
        if args[0] == "ls-remote":
            return (False, "remote unavailable") if remote == "error" else (True, "abc\trefs/heads/my-main")
        if args[0] == "pull" and remote == "pull error":
            return False, "pull refused"
        return True, ""

    monkeypatch.setattr(spec, "_git", git)
    if remote == "error":
        with pytest.raises(store.Conflict, match="remote unavailable"):
            spec._pull(doc)
        assert not any(args[0] == "pull" for args in calls)
    elif remote == "pull error":
        with pytest.raises(store.Conflict, match="pull refused"):
            spec._pull(doc)
        assert calls[-1] == ["rebase", "--abort"]
    else:
        spec._pull(doc)
        autostash = "--no-autostash" if clean else "--autostash"
        assert ["pull", "-q", "--rebase", autostash, "origin", "my-main"] in calls


def spec_text(world: World) -> str:
    return (world.spec_repo / "docs" / "SPEC.md").read_text()


def commits(world: World) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(world.spec_repo), "log", "--format=%s"], check=True, capture_output=True, text=True
    )
    return result.stdout.splitlines()


def block(world: World, key: str) -> dict:
    blocks = world.client.get("/api/spec").json()["blocks"]
    return next(entry for entry in blocks if entry["key"] == key)


def test_blocks_split_headings_paragraphs_items_and_rows() -> None:
    found = spec.blocks(SPEC_TEXT, "spec:")
    assert [entry["kind"] for entry in found] == [
        "heading", "paragraph", "heading", "row", "row", "heading", "item", "item",
    ]
    one, two = (entry for entry in found if entry["kind"] == "row")
    assert one["key"] == "spec:ONE-1" and one["columns"][6] == "Last change"
    assert two["cells"][1] == "A pipe | inside a cell."
    assert spec.row_text(two["cells"]) == two["text"]
    assert found[6]["label"] == "a list item under Notes"


def test_a_fence_line_with_text_after_it_stays_inside_the_code_block() -> None:
    text = "# Run\n\n```bash\ncat <<'EOF'\n```python\n# not a heading\nEOF\n```\n\nAfter the block.\n"
    found = spec.blocks(text, "skill:cockpit:")
    assert [entry["kind"] for entry in found] == ["heading", "code", "paragraph"]
    assert found[1]["text"].endswith("EOF\n```") and found[2]["text"] == "After the block."
    # Backticks closed on the same line are inline code, so the line is a paragraph, not a fence.
    assert [(entry["kind"], entry["text"]) for entry in spec.blocks("```inline```\nmore\n", "skill:cockpit:")] == [
        ("paragraph", "```inline```\nmore")
    ]


def test_numbered_steps_are_items_of_their_own_with_their_number() -> None:
    text = "# Sweep\n\nDo this in order:\n1. Start the sweep.\n2. Collect what needs Sam:\n   - **Sessions**: the state.\n10. Finish.\n"
    found = spec.page_blocks(text, "skill:cockpit:")
    assert [entry["kind"] for entry in found] == ["heading", "paragraph", "item", "item", "item"]
    assert found[1]["text"] == "Do this in order:"
    assert [entry["number"] for entry in found[2:]] == [1, 2, 10]
    assert found[3]["text"] == "2. Collect what needs Sam:\n   - **Sessions**: the state."
    assert found[3]["html"].startswith("<p>Collect what needs Sam:</p>") and "<strong>Sessions</strong>" in found[3]["html"]
    assert spec.page_blocks("- One step at a time.\n", "skill:cockpit:")[0]["number"] is None


def test_spec_page_shows_blocks_with_their_comments(world: World) -> None:
    posted = world.client.post(
        "/api/feedback",
        json={"text": "Should be Must", "target": "spec:ONE-1", "quote": "| ONE-1 | ...", "step_id": None, "item_id": None, "seconds": 40},
        headers=world.user(),
    )
    assert posted.status_code == 200
    world.client.post(
        "/api/feedback",
        json={"text": "gone", "target": "spec:OLD-9", "quote": "old row", "step_id": None, "item_id": None, "seconds": 40},
        headers=world.user(),
    )
    page = world.client.get("/api/spec").json()
    assert page["version"] == spec.version(SPEC_TEXT)
    row = next(entry for entry in page["blocks"] if entry["key"] == "spec:ONE-1")
    assert [comment["text"] for comment in row["comments"]] == ["Should be Must"]
    assert row["cells_html"][5] == "2026-01-04 09:00:00 &quot;one thing&quot;"
    assert [comment["text"] for comment in page["unplaced_comments"]] == ["gone"]
    assert world.client.get("/spec").status_code == 200


def test_feedback_target_must_name_the_spec_or_the_page(world: World) -> None:
    response = world.client.post(
        "/api/feedback",
        json={"text": "x", "target": "elsewhere", "quote": "", "step_id": None, "item_id": None, "seconds": 40},
        headers=world.user(),
    )
    assert response.status_code == 400


def test_user_edits_a_row_and_it_is_stamped_and_committed(world: World) -> None:
    row = block(world, "spec:ONE-1")
    values = list(row["cells"])
    values[3] = "Must"
    response = world.client.post(
        "/api/spec/part", json={"old": row["text"], "new": spec.row_text(values)}, headers=world.user()
    )
    assert response.status_code == 200
    assert response.json()["committed"] is True
    text = spec_text(world)
    edited = next(line for line in text.splitlines() if line.startswith("| ONE-1"))
    assert "| Must |" in edited
    # The cell holds only the latest change; the repo's history holds the earlier ones.
    assert re.fullmatch(r"\d{4}-\d\d-\d\d edited by Sam on Cockpit", spec.cells(edited)[6])
    assert text.count("Sam edited") == 0  # no change log: git keeps the history
    assert commits(world)[0] == "Spec: Sam edited ONE-1 on Cockpit"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def test_a_spec_edit_lands_on_top_of_code_pushed_elsewhere_and_is_pushed(world: World, tmp_path: Path) -> None:
    """CODE-7: a code push from a worktree no longer leaves the deploy checkout behind and ahead of the remote."""
    remote = git(world.spec_repo, "remote", "get-url", "origin").strip()
    worktree = tmp_path / "worktree"
    subprocess.run(["git", "clone", "-q", "-b", "main", remote, str(worktree)], check=True, capture_output=True)
    git(worktree, "config", "user.name", "Test")
    git(worktree, "config", "user.email", "test@example.test")
    (worktree / "code.py").write_text("print('new')\n")
    git(worktree, "add", "code.py")
    git(worktree, "commit", "-q", "-m", "Code from a worktree")
    git(worktree, "push", "-q", "origin", "main")

    row = block(world, "spec:ONE-2")
    response = world.client.post("/api/spec/part", json={"old": row["text"], "new": row["text"].replace("Could", "Should")}, headers=world.user())
    assert response.status_code == 200 and response.json()["committed"] is True, response.json()
    assert commits(world)[:2] == ["Spec: Sam edited ONE-2 on Cockpit", "Code from a worktree"]
    # The checkout and the remote hold the same commits: nothing ahead, nothing behind.
    git(world.spec_repo, "fetch", "-q", "origin")
    assert git(world.spec_repo, "rev-list", "--left-right", "--count", "HEAD...origin/main").split() == ["0", "0"]
    assert (world.spec_repo / "code.py").is_file()


def test_a_spec_edit_refuses_uncommitted_work_in_the_deploy_checkout_and_leaves_it_there(world: World) -> None:
    """CODE-8: spec edits preserve uncommitted changes in their checkout."""
    # A name that is not UTF-8, and a rename only the work tree knows of.
    odd = os.fsdecode(b"caf\xff.md")
    (world.spec_repo / odd).write_text("one\n")
    (world.spec_repo / "Rold.py").write_text("print('a file long enough to be seen as renamed')\n")
    git(world.spec_repo, "add", "--", odd, "Rold.py")
    git(world.spec_repo, "commit", "-q", "-m", "Two files")
    git(world.spec_repo, "push", "-q", "origin", "main")
    (world.spec_repo / odd).write_text("two\n")
    os.rename(world.spec_repo / "Rold.py", world.spec_repo / "anew.py")
    git(world.spec_repo, "add", "-N", "anew.py")
    # Git's own setting must not bring the stash back.
    git(world.spec_repo, "config", "rebase.autoStash", "true")
    (world.spec_repo / "code.py").write_text("print('half done')\n")
    git(world.spec_repo, "add", "code.py")
    # A change staged and then put back in the file still counts.
    (world.spec_repo / "docs" / "SPEC.md").write_text(SPEC_TEXT + "\nA note.\n")
    git(world.spec_repo, "add", "docs/SPEC.md")
    (world.spec_repo / "docs" / "SPEC.md").write_text(SPEC_TEXT)
    # Untracked files block nothing: a pull leaves them alone, and old nested worktrees sit there as such.
    (world.spec_repo / "tmp").mkdir()
    (world.spec_repo / "tmp" / "leftover.txt").write_text("x\n")
    head = git(world.spec_repo, "rev-parse", "HEAD")

    row = block(world, "spec:ONE-2")
    edit = {"old": row["text"], "new": row["text"].replace("Could", "Should")}
    response = world.client.post("/api/spec/part", json=edit, headers=world.user())
    detail = response.json()["detail"]
    assert response.status_code == 409 and "uncommitted changes (anew.py, caf�.md, code.py, docs/SPEC.md)" in detail, detail
    assert "worktree of its own" in detail and "leftover" not in detail
    assert git(world.spec_repo, "stash", "list") == "" and git(world.spec_repo, "rev-parse", "HEAD") == head
    assert (world.spec_repo / "code.py").is_file() and spec_text(world) == SPEC_TEXT
    assert "A note." in git(world.spec_repo, "diff", "--cached")

    # Once the work is gone, the edit goes through and is pushed; the untracked folder stays.
    git(world.spec_repo, "reset", "-q", "--hard")
    response = world.client.post("/api/spec/part", json=edit, headers=world.user())
    assert response.status_code == 200 and response.json()["committed"] is True, response.json()
    assert (world.spec_repo / "tmp" / "leftover.txt").is_file()


def test_a_spec_edit_waits_for_the_checkout_to_be_on_the_deploy_branch(world: World) -> None:
    git(world.spec_repo, "switch", "-q", "-c", "elsewhere")
    row = block(world, "spec:ONE-2")
    response = world.client.post("/api/spec/part", json={"old": row["text"], "new": row["text"].replace("Could", "Should")}, headers=world.user())
    assert response.status_code == 409 and "is on elsewhere, not main" in response.json()["detail"]
    assert spec_text(world) == SPEC_TEXT


def test_part_edit_conflicts_when_the_text_moved(world: World) -> None:
    stale = "| ONE-1 | An older wording. | User | Should | Live | x | y |  |"
    response = world.client.post("/api/spec/part", json={"old": stale, "new": "anything"}, headers=world.user())
    assert response.status_code == 409
    assert spec_text(world) == SPEC_TEXT


def test_removing_a_list_item_and_an_agent_edit_without_stamps(world: World) -> None:
    client = TestClient(world.client.app, base_url=ORIGIN)
    response = client.post(
        "/api/spec/part", json={"old": "- First note.", "new": ""}, headers=world.agent("spec-example")
    )
    assert response.status_code == 200
    text = spec_text(world)
    assert "First note" not in text and "- Second note." in text
    assert "Sam edited" not in text
    assert commits(world)[0] == "Spec: agent:spec-example edited a list item under Notes (removed) on Cockpit"


def test_whole_edit_checks_the_version_and_names_what_changed(world: World) -> None:
    page = world.client.get("/api/spec").json()
    new_text = page["text"].replace("The tool does one thing.", "The tool does two things.")
    stale = world.client.put("/api/spec", json={"version": "0" * 16, "text": new_text}, headers=world.user())
    assert stale.status_code == 409
    response = world.client.put("/api/spec", json={"version": page["version"], "text": new_text}, headers=world.user())
    assert response.status_code == 200
    text = spec_text(world)
    assert text == new_text
    assert response.json()["version"] == spec.version(text)


def test_an_unchanged_edit_writes_nothing(world: World) -> None:
    before = commits(world)
    page = world.client.get("/api/spec").json()
    client = TestClient(world.client.app, base_url=ORIGIN)
    response = client.put("/api/spec", json={"version": page["version"], "text": page["text"]}, headers=world.agent())
    assert response.json()["changed"] is False
    whole = world.client.put("/api/spec", json={"version": page["version"], "text": page["text"]}, headers=world.user())
    assert whole.json()["changed"] is False
    row = block(world, "spec:ONE-1")
    part = world.client.post("/api/spec/part", json={"old": row["text"], "new": row["text"]}, headers=world.user())
    assert part.json()["changed"] is False
    assert commits(world) == before and spec_text(world) == SPEC_TEXT


def test_a_failed_commit_keeps_the_edit_and_says_so(world: World) -> None:
    lock = world.spec_repo / ".git" / "index.lock"
    lock.write_text("")
    row = block(world, "spec:ONE-2")
    response = world.client.post("/api/spec/part", json={"old": row["text"], "new": row["text"].replace("Could", "Should")}, headers=world.user())
    assert response.status_code == 200
    assert response.json()["committed"] is False and "index.lock" in response.json()["commit_error"]
    assert "| Should |" in next(line for line in spec_text(world).splitlines() if line.startswith("| ONE-2"))
