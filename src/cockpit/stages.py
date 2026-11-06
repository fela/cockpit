"""Which stage a merged pull request reached, for the checks on waits (SPEC STATE-8, wakes.py).

A repository that releases in stages has the branches `dev`, `staging` and `prod`; a pull request merged into one of
them is on that stage. It is on a later stage when the branch holds its merge commit (a promotion such as "dev ->
staging"), or when a pull request into that branch names it and merged (a cherry-pick such as "Tasks reminders
to staging (cherry-picks of #10, #20)"), or holds the merge commit of such a pull request.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from cockpit import github

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 30
# The release stages, each a branch of the same name, in the order a change goes through them.
STAGES = ("dev", "staging", "prod")
PULL_URL = re.compile(r"^https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)(?:[/?#]|$)")
MERGE_QUERY = (
    "query($owner: String!, $repo: String!, $number: Int!) { repository(owner: $owner, name: $repo) {"
    ' dev: ref(qualifiedName: "refs/heads/dev") { name } staging: ref(qualifiedName: "refs/heads/staging") { name }'
    ' prod: ref(qualifiedName: "refs/heads/prod") { name }'
    " issueOrPullRequest(number: $number) { __typename ... on PullRequest { merged mergedAt baseRefName"
    " mergeCommit { oid } timelineItems(itemTypes: [CROSS_REFERENCED_EVENT], last: 100) { nodes {"
    " ... on CrossReferencedEvent { source { __typename ... on PullRequest { number merged mergedAt baseRefName"
    " mergeCommit { oid } repository { nameWithOwner } } } } } } } } } }"
)


@dataclass(frozen=True)
class Carrier:
    """A merged pull request into a stage branch of the same repository that names another one."""

    number: int
    base: str
    commit: str
    merged_at: str


@dataclass(frozen=True)
class Merge:
    """A merged pull request in a repository that releases in stages, with the merged pull requests into its stage
    branches that name it. `base` may be another branch, such as a feature branch; `commit` is "" when GitHub
    gives no merge commit."""

    base: str
    commit: str
    merged_at: str
    carriers: tuple[Carrier, ...]


@dataclass(frozen=True)
class Reached:
    """The furthest stage a pull request reached, and how: `via` names the pull request that carried it there, or
    is "" when the branch holds its own merge commit or it merged there itself."""

    stage: str
    via: str
    at: str


class GitHubError(Exception):
    """GitHub did not answer."""


def _gh(args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["gh", *args], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise GitHubError(f"gh took over {TIMEOUT_SECONDS} s") from exc
    except OSError as exc:
        raise GitHubError(f"gh did not start: {exc}") from exc


def ask_merge(ref: str) -> Merge | None:
    """GitHub's answer for `ref` (`owner/repo#number`): None unless it is a pull request merged into a stage branch."""
    repo, number = ref.split("#")
    owner, name = repo.split("/")
    result = _gh([
        "api", "graphql", "-f", f"query={MERGE_QUERY}", "-f", f"owner={owner}", "-f", f"repo={name}",
        "-F", f"number={number}", "--jq", ".data.repository",
    ])
    if result.returncode != 0:
        raise GitHubError(" ".join(result.stderr.split()) or f"gh exited with {result.returncode}")
    try:
        answer = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise GitHubError(f"gh answered no JSON: {result.stdout[:200]!r}") from exc
    if not isinstance(answer, dict) or not isinstance(answer.get("issueOrPullRequest"), dict):
        raise GitHubError(f"GitHub knows no issue or pull request {ref}")
    if not all(answer.get(stage) for stage in STAGES):
        return None
    answer = answer["issueOrPullRequest"]
    if answer.get("__typename") != "PullRequest" or not answer["merged"]:
        return None
    carriers = []
    for node in answer["timelineItems"]["nodes"]:
        source = (node or {}).get("source") or {}
        if (
            source.get("__typename") == "PullRequest" and source["merged"] and source["baseRefName"] in STAGES
            and source["repository"]["nameWithOwner"].casefold() == repo.casefold()
        ):
            commit = (source.get("mergeCommit") or {}).get("oid", "")
            carriers.append(Carrier(source["number"], source["baseRefName"], commit, source["mergedAt"]))
    own = (answer.get("mergeCommit") or {}).get("oid", "")
    return Merge(answer["baseRefName"], own, answer["mergedAt"], tuple(carriers))


def ask_holds(repo: str, branch: str, commit: str) -> bool:
    """Whether `branch` of `repo` holds `commit`."""
    result = _gh(["api", f"repos/{repo}/compare/{branch}...{commit}", "--jq", ".status"])
    if result.returncode != 0:
        raise GitHubError(" ".join(result.stderr.split()) or f"gh exited with {result.returncode}")
    return result.stdout.strip() in ("behind", "identical")


def reached(repo: str, merge: Merge, holds: Callable[[str, str, str], bool]) -> Reached | None:
    """The furthest stage `merge` reached, asking about the furthest stage first. A pull request into another
    branch, such as a feature branch, returns None until a stage branch holds it."""
    staged = merge.base in STAGES
    start = STAGES.index(merge.base) if staged else 0
    # Only a pull request into a later stage carries the change on: a later one into the same stage may hold
    # other work.
    later = STAGES[start + 1:] if staged else STAGES
    for branch in reversed(later):
        named = next((carrier for carrier in merge.carriers if carrier.base == branch), None)
        if named is not None:
            return Reached(branch, f"#{named.number}", named.merged_at)
        commits = [(merge.commit, "")] if merge.commit else []
        commits += [
            (carrier.commit, f"#{carrier.number}") for carrier in merge.carriers
            if carrier.commit and carrier.base in later and STAGES.index(carrier.base) < STAGES.index(branch)
        ]
        held = next((via for commit, via in commits if holds(repo, branch, commit)), None)
        if held is not None:
            return Reached(branch, held, "")
    return Reached(STAGES[start], "", merge.merged_at) if staged else None


def pull_refs(item: dict[str, Any]) -> list[str]:
    """Every pull request the item names, its `gh:` key first, as `owner/repo#number`."""
    refs = []
    match = github.GH_KEY.match(item["key"])
    if match:
        refs.append(f"{match[1]}#{match[2]}")
    for link in item["links"]:
        match = PULL_URL.match(link["url"])
        if match and f"{match[1]}#{match[2]}" not in refs:
            refs.append(f"{match[1]}#{match[2]}")
    return refs
