from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cockpit import spec
from tests.conftest import World

COCKPIT_SKILL = """---
name: cockpit
description: "How sessions use Cockpit."
---

# Cockpit

Sessions post steps.

```bash
cockpit step put

# a comment, not a heading
```

- One step at a time.
"""
REVIEW_SKILL = """---
name: review-pr
description: Review a PR.
---

# Review

Read the diff.
"""


def git(repo: Path, *command: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *command], check=True, capture_output=True, text=True).stdout


def clone(remote: Path, target: Path) -> Path:
    subprocess.run(["git", "clone", "-q", "-b", "skills", str(remote), str(target)], check=True, capture_output=True)
    git(target, "config", "user.name", "Test")
    git(target, "config", "user.email", "test@example.test")
    return target


@pytest.fixture
def remote(world: World, tmp_path: Path) -> Path:
    """A bare remote with skills/ on branch skills, cloned where the app reads them."""
    remote = tmp_path / "skills-remote.git"
    seed = tmp_path / "skills-seed"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "skills", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "skills", str(seed)], check=True)
    git(seed, "config", "user.name", "Test")
    git(seed, "config", "user.email", "test@example.test")
    for name, text in (("cockpit", COCKPIT_SKILL), ("review-pr", REVIEW_SKILL)):
        (seed / "skills" / name).mkdir(parents=True)
        (seed / "skills" / name / "SKILL.md").write_text(text)
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "skills")
    git(seed, "push", "-q", str(remote), "skills")
    clone(remote, tmp_path / "skills-repo")
    return remote


def comment(world: World, target: str) -> None:
    posted = world.client.post(
        "/api/feedback",
        json={"text": "Too long", "target": target, "quote": "Sessions post steps.", "step_id": None, "item_id": None, "seconds": 40},
        headers=world.user(),
    )
    assert posted.status_code == 200


def test_skills_are_listed_with_description_length_and_unread_comments(world: World, remote: Path) -> None:
    comment(world, "skill:cockpit:Cockpit:Sessions post steps.")
    listed = world.client.get("/api/skills").json()
    assert [(skill["name"], skill["description"], skill["unread_comments"]) for skill in listed["skills"]] == [
        ("cockpit", "How sessions use Cockpit.", 1),
        ("review-pr", "Review a PR.", 0),
    ]
    assert listed["skills"][1]["words"] == len(REVIEW_SKILL.split())
    assert world.client.get("/skills").status_code == 200
    assert world.client.get("/skills/cockpit").status_code == 200


def test_a_skill_shows_frontmatter_and_code_whole_with_comments_on_blocks(world: World, remote: Path) -> None:
    comment(world, "skill:cockpit:Cockpit:Sessions post steps.")
    page = world.client.get("/api/skills/cockpit").json()
    assert [block["kind"] for block in page["blocks"]] == ["code", "heading", "paragraph", "code", "item"]
    front, _, paragraph, fence, _ = page["blocks"]
    assert front["text"].startswith("---\nname: cockpit") and front["text"].endswith("\n---")
    assert "# a comment, not a heading" in fence["text"] and fence["label"] == "a code block under Cockpit"
    assert paragraph["key"] == "skill:cockpit:Cockpit:Sessions post steps."
    assert [entry["text"] for entry in paragraph["comments"]] == ["Too long"]
    assert page["unplaced_comments"] == []


def test_user_edit_of_a_skill_is_committed_and_pushed_on_top_of_newer_work(world: World, remote: Path, tmp_path: Path) -> None:
    other = clone(remote, tmp_path / "elsewhere")
    (other / "skills" / "review-pr" / "SKILL.md").write_text(REVIEW_SKILL + "\nThen the tests.\n")
    git(other, "commit", "-q", "-am", "review-pr: tests")
    git(other, "push", "-q", "origin", "skills")

    response = world.client.post(
        "/api/skills/cockpit/part",
        json={"old": "Sessions post steps.", "new": "Sessions post short steps."},
        headers=world.user(),
    )
    assert response.status_code == 200 and response.json()["committed"] is True, response.json()
    pushed = subprocess.run(
        ["git", "--git-dir", str(remote), "log", "--format=%s", "skills"], check=True, capture_output=True, text=True
    ).stdout.split("\n")
    assert pushed[:2] == ["cockpit: Sam edited a paragraph under Cockpit on Cockpit", "review-pr: tests"]
    text = (tmp_path / "skills-repo" / "skills" / "cockpit" / "SKILL.md").read_text()
    assert "Sessions post short steps." in text


def test_only_user_edits_a_skill_and_only_skills_that_exist(world: World, remote: Path) -> None:
    edit = {"old": "Sessions post steps.", "new": "x"}
    assert world.client.post("/api/skills/cockpit/part", json=edit, headers=world.agent()).status_code == 403
    assert world.client.get("/api/skills/missing").status_code == 404
    assert world.client.get("/api/skills/Not_A_Skill").status_code == 404
    assert world.client.post("/api/skills/missing/part", json=edit, headers=world.user()).status_code == 404


def test_a_skill_edit_waits_for_the_checkout_to_be_on_the_skills_branch(world: World, remote: Path, tmp_path: Path) -> None:
    git(tmp_path / "skills-repo", "switch", "-q", "-c", "elsewhere")
    response = world.client.post(
        "/api/skills/cockpit/part", json={"old": "Sessions post steps.", "new": "x"}, headers=world.user()
    )
    assert response.status_code == 409 and "is on elsewhere, not skills" in response.json()["detail"]


def test_uncommitted_work_that_clashes_with_the_pull_stops_a_skill_edit_without_conflict_markers(world: World, remote: Path, tmp_path: Path) -> None:
    """The pull succeeds while putting back clashing work with conflict markers. The skills
    checkout still sets uncommitted work aside; the deploy checkout refuses it (CODE-8)."""
    skill = Path("skills") / "cockpit" / "SKILL.md"
    other = clone(remote, tmp_path / "elsewhere")
    (other / skill).write_text(COCKPIT_SKILL.replace("One step at a time.", "Two steps at a time."))
    git(other, "commit", "-q", "-am", "cockpit upstream")
    git(other, "push", "-q", "origin", "skills")
    checkout = tmp_path / "skills-repo"
    (checkout / skill).write_text(COCKPIT_SKILL.replace("One step at a time.", "Three steps at a time."))

    edit = {"old": "Sessions post steps.", "new": "Sessions post short steps."}
    response = world.client.post("/api/skills/cockpit/part", json=edit, headers=world.user())
    assert response.status_code == 409 and "kept in git's stash" in response.json()["detail"], response.json()
    text = (checkout / skill).read_text()
    assert "<<<<<<<" not in text and "Two steps" in text
    assert "Three steps" in git(checkout, "stash", "show", "-p")
    assert git(checkout, "diff", "--name-only", "--diff-filter=U") == ""
    pushed = subprocess.run(
        ["git", "--git-dir", str(remote), "log", "--format=%s", "skills"], check=True, capture_output=True, text=True
    ).stdout.split("\n")
    assert pushed[0] == "cockpit upstream"


def test_a_clash_on_an_oddly_named_file_or_one_deleted_upstream_leaves_the_skills_checkout_usable(world: World, remote: Path, tmp_path: Path) -> None:
    """Such files once stayed unmerged, or took a neighbour along."""
    checkout = tmp_path / "skills-repo"
    # A leading space and brackets: once trimmed, and read as a pattern that also matched " a 1.md".
    note = " a [1].md"
    for name in (note, " a 1.md", "old.md"):
        (checkout / name).write_text("one\n")
    git(checkout, "add", "--", note, " a 1.md", "old.md")
    git(checkout, "commit", "-q", "-m", "Two files")
    git(checkout, "push", "-q", "origin", "skills")
    other = clone(remote, tmp_path / "elsewhere")
    (other / note).write_text("two\n")
    git(other, "rm", "-q", "old.md")
    git(other, "commit", "-q", "-am", "Upstream")
    git(other, "push", "-q", "origin", "skills")
    (checkout / note).write_text("three\n")
    (checkout / " a 1.md").write_text("mine\n")
    (checkout / "old.md").write_text("three\n")

    edit = {"old": "Sessions post steps.", "new": "Sessions post short steps."}
    response = world.client.post("/api/skills/cockpit/part", json=edit, headers=world.user())
    assert response.status_code == 409 and "fixing by hand" not in response.json()["detail"], response.json()
    assert git(checkout, "diff", "--name-only", "--diff-filter=U") == ""
    assert (checkout / note).read_text() == "two\n" and not (checkout / "old.md").exists()
    assert (checkout / " a 1.md").read_text() == "mine\n"
    # With the clash cleared, the next edit goes through and is pushed.
    response = world.client.post("/api/skills/cockpit/part", json=edit, headers=world.user())
    assert response.status_code == 200 and response.json()["committed"] is True, response.json()


def test_a_folded_description_reads_as_one_line() -> None:
    head = "---\nname: cockpit\ndescription: >-\n  Read and update Cockpit,\n  the user's work page.\nmetadata: x"
    assert spec.frontmatter_description(head) == "Read and update Cockpit, the user's work page."
    assert spec.frontmatter_description('---\ndescription: "Review a PR."') == "Review a PR."
