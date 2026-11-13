"""Who opened the pull request each open item is about (SPEC VIEW-5), so the overview tags the user's own PRs
apart from the ones the user reviews.

An item's PR is its `gh:` key, or else its first link to a GitHub pull request. Cockpit asks GitHub through
the `gh` command, which is signed in as the user, so `viewerDidAuthor` says whether the user opened it. A PR's author
never changes: Cockpit asks once per PR and keeps the answer in `pull_authors`. A `gh:` key can name an issue;
its row says so, and the page tags it as no PR. A lookup that fails is logged as an error and tried again
after RETRY_AFTER; until then the page tags the item by its step alone.

`pull_merged` asks whether an item's PR merged, for an agent's close of the item (SPEC CONTROL-15): a merge
needs no finish step from the user.

`check_states` keeps the state of every pull request the open items link to, so the page draws each link to one as
open, draft, merged or closed (SPEC NEXT-66). It asks GitHub about them in one query per STATE_BATCH, every
CHECK_SECONDS, and stops asking about a PR once it merged.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from cockpit import db

log = logging.getLogger(__name__)

CHECK_SECONDS = 60
TIMEOUT_SECONDS = 30
RETRY_AFTER = timedelta(hours=1)
STATE_BATCH = 50
GH_KEY = re.compile(r"^gh:([\w.-]+/[\w.-]+)#(\d+)$")
# A PR number has no leading zero, so it goes into a GraphQL query as written.
PULL_URL = re.compile(r"^https://github\.com/([\w.-]+/[\w.-]+)/pull/([1-9]\d*)(?:[/?#]|$)")
QUERY = (
    "query($owner: String!, $repo: String!, $number: Int!) { repository(owner: $owner, name: $repo) {"
    " issueOrPullRequest(number: $number) { __typename"
    " ... on PullRequest { viewerDidAuthor author { login } } ... on Issue { viewerDidAuthor author { login } } } } }"
)
MERGED_QUERY = (
    "query($owner: String!, $repo: String!, $number: Int!) { repository(owner: $owner, name: $repo) {"
    " issueOrPullRequest(number: $number) { __typename ... on PullRequest { merged } } } }"
)

STATE_PART = (
    'p{index}: repository(owner: "{owner}", name: "{name}") {{ issueOrPullRequest(number: {number}) {{ __typename'
    " ... on PullRequest {{ state isDraft }} }} }}"
)


@dataclass(frozen=True)
class Pull:
    """What GitHub says about a number: whether it is a pull request, who opened it, and whether the user did."""

    pull: bool
    author: str
    mine: bool


class GitHubError(Exception):
    """GitHub did not answer about an issue or PR."""


def url_ref(url: str) -> str | None:
    """The pull request `url` names, as `owner/repo#number`, or None when it names none."""
    match = PULL_URL.match(url)
    return f"{match[1]}#{match[2]}" if match else None


def pull_ref(item: dict[str, Any]) -> str | None:
    """The item's PR as `owner/repo#number`, or None when it names none."""
    match = GH_KEY.match(item["key"])
    if match:
        return f"{match[1]}#{match[2]}"
    for link in item["links"]:
        ref = url_ref(link["url"])
        if ref:
            return ref
    return None


def _number(query: str, ref: str) -> dict[str, Any]:
    """GitHub's answer to `query` about the issue or pull request `ref` (`owner/repo#number`)."""
    repo, number = ref.split("#")
    owner, name = repo.split("/")
    try:
        result = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={query}", "-f", f"owner={owner}", "-f", f"repo={name}",
             "-F", f"number={number}", "--jq", ".data.repository.issueOrPullRequest"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitHubError(f"gh took over {TIMEOUT_SECONDS} s") from exc
    except OSError as exc:
        raise GitHubError(f"gh did not start: {exc}") from exc
    if result.returncode != 0:
        raise GitHubError(" ".join(result.stderr.split()) or f"gh exited with {result.returncode}")
    try:
        answer = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise GitHubError(f"gh answered no JSON: {result.stdout[:200]!r}") from exc
    if not isinstance(answer, dict):
        raise GitHubError(f"GitHub knows no issue or pull request {ref}")
    return answer


def pull_merged(ref: str) -> bool:
    """Whether `ref` is a pull request that merged; False for an issue."""
    return bool(_number(MERGED_QUERY, ref).get("merged"))


def ask_github(ref: str) -> Pull:
    answer = _number(QUERY, ref)
    return Pull(
        pull=answer["__typename"] == "PullRequest",
        author=(answer["author"] or {}).get("login", ""),
        mine=answer["viewerDidAuthor"],
    )


def check_once(conn: sqlite3.Connection, ask: Callable[[str], Pull], failed: dict[str, datetime], now: datetime) -> list[str]:
    """Ask GitHub about each open item's PR it has not answered yet, and keep the answers; returns their refs.

    `failed` holds when each lookup last failed, so a broken one is asked again only after RETRY_AFTER.
    """
    known = {record["ref"] for record in conn.execute("SELECT ref FROM pull_authors")}
    items = db.rows(conn.execute("SELECT key, links FROM items WHERE archived = 0 AND stage IS NOT 'done'").fetchall())
    refs = sorted({ref for item in items if (ref := pull_ref(item)) and ref not in known})
    recorded = []
    for ref in refs:
        if ref in failed and now - failed[ref] < RETRY_AFTER:
            continue
        try:
            answer = ask(ref)
        except GitHubError as exc:
            failed[ref] = now
            log.error("asking GitHub who opened %s failed: %s", ref, exc)
            continue
        failed.pop(ref, None)
        conn.execute(
            "INSERT INTO pull_authors (ref, pull, author, mine, checked_at) VALUES (?, ?, ?, ?, ?)",
            (ref, int(answer.pull), answer.author, int(answer.mine), now.strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        recorded.append(ref)
    return recorded


def pulls(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Each known pull request by its ref, as `state.items[].pull` shows it."""
    return {
        record["ref"]: {"ref": record["ref"], "mine": bool(record["mine"])}
        for record in conn.execute("SELECT ref, mine FROM pull_authors WHERE pull = 1")
    }


@dataclass(frozen=True)
class States:
    """GitHub's answer about some pull requests: the state of each it knows (`found`), and why it gave none for
    the others (`missing`)."""

    found: dict[str, str]
    missing: dict[str, str]


def _state(answer: dict[str, Any]) -> str:
    if answer["state"] == "MERGED":
        return "merged"
    if answer["state"] == "CLOSED":
        return "closed"
    return "draft" if answer["isDraft"] else "open"


def ask_states(refs: list[str]) -> States:
    """The state of each pull request in `refs`, from one GitHub query. GitHub answers the PRs it can and names
    the others in its errors, with gh exiting nonzero, so the answer is read whatever gh's exit code."""
    parts = []
    for index, ref in enumerate(refs):
        repo, number = ref.split("#")
        owner, name = repo.split("/")
        parts.append(STATE_PART.format(index=index, owner=owner, name=name, number=number))
    try:
        result = subprocess.run(
            ["gh", "api", "graphql", "-f", "query={ " + " ".join(parts) + " }"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitHubError(f"gh took over {TIMEOUT_SECONDS} s") from exc
    except OSError as exc:
        raise GitHubError(f"gh did not start: {exc}") from exc
    try:
        answer = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise GitHubError(" ".join(result.stderr.split()) or f"gh answered no JSON: {result.stdout[:200]!r}") from exc
    if not isinstance(answer, dict) or not isinstance(answer.get("data"), dict):
        raise GitHubError(" ".join(result.stderr.split()) or f"gh answered no data: {result.stdout[:200]!r}")
    errors = {
        error["path"][0]: error.get("message", "")
        for error in answer.get("errors", []) if isinstance(error, dict) and error.get("path")
    }
    found: dict[str, str] = {}
    missing: dict[str, str] = {}
    for index, ref in enumerate(refs):
        part = answer["data"].get(f"p{index}")
        number = part["issueOrPullRequest"] if isinstance(part, dict) else None
        if isinstance(number, dict) and number["__typename"] == "PullRequest":
            found[ref] = _state(number)
        elif isinstance(number, dict):
            missing[ref] = "it is an issue"
        else:
            missing[ref] = errors.get(f"p{index}") or "GitHub knows no such pull request"
    return States(found=found, missing=missing)


def check_states(
    conn: sqlite3.Connection, ask: Callable[[list[str]], States], refs: list[str], failed: dict[str, datetime],
    now: datetime,
) -> list[str]:
    """Ask GitHub the state of each pull request in `refs` that has not merged, and keep the answers; returns the
    refs whose state changed.

    `failed` holds when each lookup last failed, so a broken one is asked again only after RETRY_AFTER.
    """
    known = {record["ref"]: record["state"] for record in conn.execute("SELECT ref, state FROM pull_states")}
    asking = [
        ref for ref in refs
        if known.get(ref) != "merged" and not (ref in failed and now - failed[ref] < RETRY_AFTER)
    ]
    changed = []
    for start in range(0, len(asking), STATE_BATCH):
        batch = asking[start:start + STATE_BATCH]
        try:
            answer = ask(batch)
        except GitHubError as exc:
            for ref in batch:
                failed[ref] = now
            log.error("asking GitHub the state of %s failed: %s", ", ".join(batch), exc)
            continue
        for ref, reason in answer.missing.items():
            failed[ref] = now
            log.error("asking GitHub the state of %s failed: %s", ref, reason)
        for ref, state in answer.found.items():
            failed.pop(ref, None)
            conn.execute(
                "INSERT INTO pull_states (ref, state, checked_at) VALUES (?, ?, ?)"
                " ON CONFLICT (ref) DO UPDATE SET state = excluded.state, checked_at = excluded.checked_at",
                (ref, state, now.strftime("%Y-%m-%dT%H:%M:%SZ")),
            )
            if known.get(ref) != state:
                changed.append(ref)
    return changed


def pull_states(conn: sqlite3.Connection) -> dict[str, str]:
    """Each known pull request's state by its ref, as `state.pull_states` shows it."""
    return {record["ref"]: record["state"] for record in conn.execute("SELECT ref, state FROM pull_states")}
