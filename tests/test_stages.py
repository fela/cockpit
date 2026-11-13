from __future__ import annotations

import json
import subprocess

from cockpit import stages, wakes

REPO = "example-org/app"


def merge(base: str, commit: str, *carriers: stages.Carrier) -> stages.Merge:
    return stages.Merge(base, commit, "2026-01-15T10:00:00Z", tuple(carriers))


def test_a_pull_request_reaches_a_stage_by_promotion_or_by_a_pull_request_that_names_it() -> None:
    on = {("staging", "aaaa111"), ("prod", "cccc333")}

    def holds(repo: str, branch: str, commit: str) -> bool:
        return (branch, commit) in on

    # A promotion: staging holds the merge commit, prod does not.
    assert stages.reached(REPO, merge("dev", "aaaa111"), holds) == stages.Reached("staging", "", "")
    # A cherry-pick into staging names it; prod holds the cherry-pick's own merge commit.
    picked = stages.Carrier(20, "staging", "cccc333", "2026-01-15T12:00:00Z")
    assert stages.reached(REPO, merge("dev", "bbbb222", picked), holds) == stages.Reached("prod", "#20", "")
    # A later pull request into dev that names it carries nothing on.
    follow_up = stages.Carrier(30, "dev", "cccc333", "2026-01-15T11:00:00Z")
    assert stages.reached(REPO, merge("dev", "bbbb222", follow_up), holds) == stages.Reached("dev", "", "2026-01-15T10:00:00Z")


def test_each_later_stage_is_checked_on_its_own_furthest_first() -> None:
    # A cherry-pick straight into prod, with nothing on staging.
    to_prod = stages.Carrier(40, "prod", "dddd444", "2026-01-15T13:00:00Z")
    assert stages.reached(REPO, merge("dev", "aaaa111", to_prod), lambda r, b, c: False).stage == "prod"
    # Prod holds the merge commit; staging does not.
    assert stages.reached(REPO, merge("dev", "aaaa111"), lambda r, b, c: b == "prod") == stages.Reached("prod", "", "")
    # A pull request into a feature branch is not on dev until a stage branch holds it.
    feature = stages.Merge("feature/reminders", "eeee555", "2026-01-15T10:00:00Z", ())
    assert stages.reached(REPO, feature, lambda r, b, c: False) is None
    assert stages.reached(REPO, feature, lambda r, b, c: b == "staging").stage == "staging"
    # No merge commit from GitHub: only the carriers count, and a carrier without one still names its stage.
    bare = stages.Merge("dev", "", "2026-01-15T10:00:00Z", ())
    assert stages.reached(REPO, bare, lambda r, b, c: True).stage == "dev"
    picked = stages.Carrier(20, "staging", "", "2026-01-15T12:00:00Z")
    assert stages.reached(REPO, merge("dev", "aaaa111", picked), lambda r, b, c: c == "").stage == "staging"


def test_a_feature_branch_merge_waits_until_a_stage_holds_it() -> None:
    feature = merge("feature/exports", "aaaa111")
    ref = f"{REPO}#10"
    check = {"kind": "reached", "stage": "dev", "pulls": ["https://github.com/example-org/app/pull/10"]}
    asked: list[tuple[str, str, str]] = []

    def absent(repo: str, branch: str, commit: str) -> bool:
        asked.append((repo, branch, commit))
        return False

    reached = wakes.reacher(lambda ref: feature, absent)
    assert not wakes.holds(check, reached)
    assert not wakes.holds(check, reached)
    assert asked == [(REPO, branch, "aaaa111") for branch in reversed(stages.STAGES)]
    for stage in stages.STAGES:
        reached = wakes.reacher(lambda ref: feature, lambda repo, branch, commit, stage=stage: branch == stage)
        assert reached(ref) == stage
        assert wakes.holds(check, reached)


def answer(stage_branches: bool, merge_commit: dict | None) -> str:
    refs = {stage: ({"name": stage} if stage_branches else None) for stage in stages.STAGES}
    pull = {"__typename": "PullRequest", "merged": True, "mergedAt": "2026-01-15T10:00:00Z", "baseRefName": "dev",
            "mergeCommit": merge_commit, "timelineItems": {"nodes": []}}
    return json.dumps(refs | {"issueOrPullRequest": pull})


def test_only_repositories_with_all_three_stage_branches_count(monkeypatch) -> None:
    replies: list[str] = []
    monkeypatch.setattr(stages, "_gh", lambda args: subprocess.CompletedProcess(args, 0, stdout=replies.pop(0), stderr=""))
    replies.append(answer(False, {"oid": "aaaa111"}))
    assert stages.ask_merge(f"{REPO}#10") is None
    replies.append(answer(True, None))
    assert stages.ask_merge(f"{REPO}#10") == stages.Merge("dev", "", "2026-01-15T10:00:00Z", ())
    replies.append(answer(True, {"oid": "aaaa111"}))
    assert stages.ask_merge(f"{REPO}#10") == stages.Merge("dev", "aaaa111", "2026-01-15T10:00:00Z", ())
