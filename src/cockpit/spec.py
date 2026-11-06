"""Markdown files the user reads and edits block by block: the living spec and the user's skills.

The spec is docs/SPEC.md in Cockpit's repo, on the branch Cockpit deploys. Each
skill is a SKILL.md in the checkout every session loads the user's skills from, on
its sync branch.

The server is the one writer that both the page and every session can reach, so
edits from either go through here. A part edit names the exact block it
replaces; a whole edit names the version it started from. Either fails with a
conflict when the file changed underneath it, and each edit is committed on its
own and pushed to the configured branch. Spec edits are pushed so a later deploy includes them (SPEC CODE-7). The user's
edit of a requirement row replaces its "Last change" cell, which holds only the
latest change (the repo's history holds the earlier ones); agents write that cell
themselves, as the spec's rules ask.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from cockpit import render, store
from cockpit.config import user_name

log = logging.getLogger(__name__)

SPEC_FILE = "docs/SPEC.md"
SKILL_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
REQUIREMENT_ID = re.compile(r"[A-Z]+-\d+")
TABLE_SEPARATOR = re.compile(r"\|[\s:|-]+\|?")
UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")
# A numbered step, "7. ": a list item of its own, like a "- " item, so a comment or edit lands on that step.
NUMBERED = re.compile(r"(\d+)\. ")
GIT_TIMEOUT_SECONDS = 30

# One edit at a time: read, check, write and commit happen under this lock.
_lock = threading.Lock()


@dataclass(frozen=True)
class Doc:
    """One file the page shows: where it is, how its blocks are keyed, and what an edit does in git."""

    repo: Path
    file: str
    # Starts each block's key, and so the target of the user's comments on it.
    key_prefix: str
    # "the spec", "the cockpit skill": in errors and on the page.
    name: str
    # Starts the commit message.
    subject: str
    # The branch an edit pulls first and pushes its commit to. None: commit only.
    branch: str | None
    # The checkout holds only its branch (the deploy checkout, SPEC CODE-8): an edit refuses uncommitted changes
    # there instead of setting them aside in git's stash.
    clean: bool


def spec_doc(repo: Path, branch: str) -> Doc:
    """The spec, synced on `branch`, the one Cockpit deploys; "" (deploy turned off) commits only."""
    return Doc(repo, SPEC_FILE, "spec:", "the spec", "Spec", branch or None, True)


def skill_doc(repo: Path, folder: str, branch: str, name: str) -> Doc:
    """The user's skill `name`: a folder of its own in `folder` of the skills checkout, synced on `branch`."""
    file = f"{folder}/{name}/SKILL.md"
    if not SKILL_NAME.fullmatch(name) or not (repo / file).is_file():
        raise store.NotFound(f"no skill named {name} in {repo / folder}")
    return Doc(repo, file, f"skill:{name}:", f"the {name} skill", name, branch, False)


def skills(repo: Path, folder: str) -> list[dict[str, Any]]:
    """The user's skills in `folder` of the skills checkout by name, each with the description from its
    frontmatter and its length in words."""
    where = repo / folder
    if not where.is_dir():
        raise store.NotFound(f"no skills at {where}")
    found = []
    for path in sorted(where.glob("*/SKILL.md")):
        text = path.read_text()
        head = text.split("\n---", 1)[0] if text.startswith("---\n") else ""
        found.append({"name": path.parent.name, "description": frontmatter_description(head), "words": len(text.split())})
    return found


def frontmatter_description(head: str) -> str:
    """The `description:` of a skill's frontmatter: on its own line, or a YAML block (`>-`, `|`) of the indented
    lines below it."""
    lines = head.split("\n")
    start = next((i for i, line in enumerate(lines) if line.startswith("description:")), None)
    if start is None:
        return ""
    value = lines[start].removeprefix("description:").strip()
    if value[:1] not in (">", "|"):
        return value.strip("\"'")
    block = []
    for line in lines[start + 1:]:
        if line.strip() and not line.startswith((" ", "\t")):
            break
        block.append(line.strip())
    return " ".join(part for part in block if part)


def version(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def read(doc: Doc) -> str:
    path = doc.repo / doc.file
    try:
        return path.read_text()
    except FileNotFoundError as exc:
        raise store.NotFound(f"{doc.name} is missing: no file at {path}") from exc


def cells(line: str) -> list[str]:
    inner = line.strip().removeprefix("|")
    if inner.endswith("|") and not inner.endswith("\\|"):
        inner = inner[:-1]
    return [cell.strip().replace("\\|", "|") for cell in UNESCAPED_PIPE.split(inner)]


def row_text(values: list[str]) -> str:
    return "| " + " | ".join(" ".join(value.split()).replace("|", "\\|") for value in values) + " |"


def _words(text: str, length: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= length else flat[: length - 1] + "…"


def _block(kind: str, lines: list[str], start: int, section: str, prefix: str, **extra: Any) -> dict[str, Any]:
    text = "\n".join(lines)
    where = section or "the top"
    if kind == "row" and REQUIREMENT_ID.fullmatch(extra["cells"][0]):
        key, label = f"{prefix}{extra['cells'][0]}", extra["cells"][0]
    elif kind == "row":
        key, label = f"{prefix}{section}:{_words(extra['cells'][0], 40)}", f"a row under {where}"
    elif kind == "heading":
        key, label = f"{prefix}#{extra['title']}", f"the heading {extra['title']}"
    else:
        noun = {"item": "a list item", "code": "a code block"}.get(kind, "a paragraph")
        key, label = f"{prefix}{section}:{_words(text, 60)}", f"{noun} under {where}"
    return {"kind": kind, "text": text, "start": start, "end": start + len(lines), "section": section,
            "key": key, "label": label, **extra}


def blocks(markdown: str, prefix: str) -> list[dict[str, Any]]:
    """Headings, paragraphs, list items (bulleted or numbered), table rows and code blocks, each with its exact source text and line range.

    A table's header and separator lines are not blocks: they change only through a whole edit. A skill's
    frontmatter is a code block, and so is a fenced block, blank lines and `#` lines inside it included.
    """
    lines = markdown.split("\n")
    found: list[dict[str, Any]] = []
    section = ""
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
        elif index == 0 and line == "---":
            end = next((number for number in range(1, len(lines)) if lines[number] == "---"), len(lines) - 1)
            found.append(_block("code", lines[: end + 1], 0, section, prefix))
            index = end + 1
        elif (fence := render.fence_after(None, line)) is not None:
            # CommonMark's closing rule, as the page renders it: a "```python" line inside the block stays code.
            start = index
            index += 1
            while index < len(lines) and render.fence_after(fence, lines[index]) is not None:
                index += 1
            index = min(index + 1, len(lines))
            found.append(_block("code", lines[start:index], start, section, prefix))
        elif line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            section = line[level:].strip()
            found.append(_block("heading", [line], index, section, prefix, level=level, title=section))
            index += 1
        elif line.startswith("|"):
            start = index
            while index < len(lines) and lines[index].startswith("|"):
                index += 1
            table = lines[start:index]
            has_header = len(table) > 1 and TABLE_SEPARATOR.fullmatch(table[1].strip()) is not None
            columns = cells(table[0]) if has_header else []
            first_row = start + 2 if has_header else start
            for number in range(first_row, index):
                found.append(_block("row", [lines[number]], number, section, prefix, columns=columns, cells=cells(lines[number])))
        elif line.startswith("- ") or NUMBERED.match(line):
            start = index
            index += 1
            while index < len(lines) and lines[index].startswith("  ") and lines[index].strip():
                index += 1
            found.append(_block("item", lines[start:index], start, section, prefix))
        else:
            start = index
            while (
                index < len(lines) and lines[index].strip()
                and not lines[index].startswith(("#", "|", "- ")) and not NUMBERED.match(lines[index])
                and render.fence_after(None, lines[index]) is None
            ):
                index += 1
            found.append(_block("paragraph", lines[start:index], start, section, prefix))
    return found


def page_blocks(markdown: str, prefix: str) -> list[dict[str, Any]]:
    """Blocks with HTML for the page: Markdown rendered without raw HTML, links allowlisted."""
    shown = []
    for block in blocks(markdown, prefix):
        if block["kind"] == "row":
            block["cells_html"] = [render.markdown_inline_html(cell, render.NO_REFS) for cell in block["cells"]]
        elif block["kind"] == "item":
            # The page numbers a numbered item itself, so its text renders without the number.
            numbered = NUMBERED.match(block["text"])
            block["number"] = int(numbered.group(1)) if numbered else None
            block["html"] = render.markdown_html(block["text"][numbered.end():] if numbered else block["text"].removeprefix("- "), render.NO_REFS)
        elif block["kind"] == "code":
            fenced = block["text"] if render.FENCE.match(block["text"]) else f"```\n{block['text']}\n```"
            block["html"] = render.markdown_html(fenced, render.NO_REFS)
        elif block["kind"] == "paragraph":
            block["html"] = render.markdown_html(block["text"], render.NO_REFS)
        shown.append(block)
    return shown


def edit_part(doc: Doc, old: str, new: str, actor: str, now: datetime) -> dict[str, Any]:
    """Replace the one block whose text is `old`; an empty `new` removes it."""
    with _lock:
        _sync(doc)
        current = read(doc)
        matches = [block for block in blocks(current, doc.key_prefix) if block["text"] == old]
        if not matches:
            raise store.Conflict(f"this part of {doc.name} changed since you opened it; reload and edit it again")
        if len(matches) > 1:
            raise store.Conflict(f"this text appears {len(matches)} times in {doc.name}; edit the whole file instead")
        block = matches[0]
        new = new.replace("\r\n", "\n").strip("\n")
        if new == block["text"]:
            return _save(doc, current, current, "")  # unchanged: no stamp, no commit
        if actor == "user":
            new = _stamp_row(block, new, now)
        lines = current.split("\n")
        lines[block["start"] : block["end"]] = new.split("\n") if new.strip() else []
        start = block["start"]
        if not new.strip() and 0 < start < len(lines) and not lines[start - 1].strip() and not lines[start].strip():
            del lines[start]
        text = "\n".join(lines)
        what = block["label"] if new.strip() else f"{block['label']} (removed)"
        return _save(doc, current, text, f"{doc.subject}: {_who(actor)} edited {what} on Cockpit")


def edit_all(doc: Doc, base_version: str, text: str, actor: str) -> dict[str, Any]:
    with _lock:
        _sync(doc)
        current = read(doc)
        if version(current) != base_version:
            raise store.Conflict(f"{doc.name} changed since you opened it; reload, then make your edit again")
        text = text.replace("\r\n", "\n")
        if not text.strip():
            raise store.Invalid(f"{doc.name} cannot be empty")
        if not text.endswith("\n"):
            text += "\n"
        if text == current:
            return _save(doc, current, current, "")  # unchanged: no commit
        what = _changed_words(blocks(current, doc.key_prefix), blocks(text, doc.key_prefix))
        return _save(doc, current, text, f"{doc.subject}: {_who(actor)} edited {what} on Cockpit")


def _who(actor: str) -> str:
    return user_name() if actor == "user" else actor


def _stamp_row(block: dict[str, Any], new: str, now: datetime) -> str:
    """The user's edit of a requirement row becomes its "Last change" cell; the repo's history holds the earlier ones."""
    if block["kind"] != "row" or "Last change" not in block["columns"] or "\n" in new or not new.startswith("|"):
        return new
    values = cells(new)
    if len(values) != len(block["columns"]):
        return new
    index = block["columns"].index("Last change")
    stamp = f"{now:%Y-%m-%d} edited by {user_name()} on Cockpit"
    values[index] = stamp
    return row_text(values)


def _changed_words(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> str:
    old_texts = {block["text"] for block in before}
    new_texts = {block["text"] for block in after}
    added = [block for block in after if block["text"] not in old_texts]
    removed = [block for block in before if block["text"] not in new_texts]
    ids = list(dict.fromkeys(
        block["label"] for block in added + removed if block["kind"] == "row" and REQUIREMENT_ID.fullmatch(block["label"])
    ))
    others = max(
        sum(1 for block in added if block["label"] not in ids),
        sum(1 for block in removed if block["label"] not in ids),
    )
    parts = ids + ([f"{others} other part{'s' if others != 1 else ''}"] if others else [])
    if not parts:
        return "the spacing"
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _sync(doc: Doc) -> None:
    """The checkout must be on the doc's branch, and is brought up to date with it before an edit."""
    if doc.branch is None:
        return
    ok, branch = _git(doc.repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    if not ok:
        raise store.Conflict(f"could not read the branch of {doc.repo}: {branch}")
    if branch != doc.branch:
        raise store.Conflict(f"{doc.repo} is on {_shown(branch)}, not {doc.branch}; switch it back to edit {doc.name}")
    _pull(doc)


def _pull(doc: Doc) -> None:
    if doc.clean:
        _require_clean(doc)
    ok, heads = _git(doc.repo, ["ls-remote", "--heads", "origin", f"refs/heads/{doc.branch}"])
    if not ok:
        raise store.Conflict(f"could not check {doc.branch} on the remote for {doc.repo}: {heads}")
    if not heads:
        return  # The first push creates a new personal branch.
    # Explicit both ways, so a rebase.autoStash setting cannot set aside work that arrived after the check.
    autostash = "--no-autostash" if doc.clean else "--autostash"
    ok, error = _git(doc.repo, ["pull", "-q", "--rebase", autostash, "origin", str(doc.branch)])
    if not ok:
        _git(doc.repo, ["rebase", "--abort"])  # leave the checkout usable; the error is what counts
        raise store.Conflict(f"could not bring {doc.repo} up to date with {doc.branch}: {error}")
    # The pull succeeds even when uncommitted work it set aside clashes with what came in: git puts it back with
    # conflict markers, which the edit would then commit and push. Those files go back to the commit, the work
    # stays in git's stash, and the edit stops.
    ok, clashed = _git(doc.repo, ["diff", "-z", "--name-only", "--diff-filter=U"])
    if not ok or clashed:
        files = [name for name in clashed.split("\0") if name] if ok else []
        left = _restore(doc.repo, files) if files else clashed
        raise store.Conflict(
            f"uncommitted work in {doc.repo} clashed with {doc.branch} on GitHub"
            f" ({_shown(', '.join(files) or clashed)}); it is kept in git's stash, and nothing was pushed"
            + (f". The checkout still needs fixing by hand: {_shown(left)}" if left else "")
        )


def _require_clean(doc: Doc) -> None:
    """Work in progress belongs in a worktree of its own: a pull here would set it aside, and a restart would run it.

    Untracked files are left out: a pull leaves them alone, and old nested worktrees sit there as such.
    """
    # Staged and unstaged changes both count, also a file staged and then put back; a submodule counts when its
    # commit or tracked files changed, whatever diff.ignoreSubmodules says.
    ok, status = _git(doc.repo, [
        "status", "-z", "--porcelain=v1", "--untracked-files=no", "--ignore-submodules=untracked",
    ])
    if not ok:
        raise store.Conflict(f"could not read the state of {doc.repo}: {status}")
    files, entries = [], iter(status.split("\0"))
    for entry in entries:
        if entry:
            files.append(entry[3:])
            if {"R", "C"} & set(entry[:2]):
                next(entries, None)  # a rename's or copy's old name follows as an entry of its own
    if files:
        raise store.Conflict(
            f"{doc.repo} has uncommitted changes ({_shown(', '.join(files))}), so {doc.name} cannot be brought up to date"
            f" with {doc.branch} there. That checkout holds only {doc.branch}: move the work into a worktree of its"
            f" own (git worktree add), commit it and merge it to {doc.branch}"
        )


def _restore(repo: Path, files: list[str]) -> str:
    """Put each clashed file back as the commit has it, or remove it when the commit deleted it; say what is left."""
    for name in files:
        in_head, _ = _git(repo, ["cat-file", "-e", f"HEAD:{name}"])
        # Literal, so a name like "a [1].md" touches only that file and not "a 1.md".
        _git(repo, ["--literal-pathspecs", *(["checkout", "HEAD"] if in_head else ["rm", "-q", "-f"]), "--", name])
    ok, left = _git(repo, ["diff", "-z", "--name-only", "--diff-filter=U"])
    return left.replace("\0", ", ").strip(", ") if ok else left


def _save(doc: Doc, before: str, after: str, message: str) -> dict[str, Any]:
    if after == before:
        return {"version": version(before), "changed": False, "committed": True, "pushed": False, "commit_error": ""}
    path = doc.repo / doc.file
    temporary = path.with_name(f".{path.name}.cockpit-tmp")
    temporary.write_text(after)
    os.replace(temporary, path)
    error = _commit(doc, message)
    # With deploy turned off (no branch) the commit stays in the checkout.
    return {"version": version(after), "changed": True, "committed": error is None,
            "pushed": error is None and doc.branch is not None, "commit_error": error or ""}


def _commit(doc: Doc, message: str) -> str | None:
    """Commit the file alone, and push the commit to the doc's branch; returns what failed, or None.

    In a skills checkout, uncommitted changes an agent left in that file go into the same commit.
    """
    ok, error = _git(doc.repo, ["commit", "-q", "-m", message, "--", doc.file])
    if ok and doc.branch is not None:
        try:
            _pull(doc)
        except store.Conflict as exc:
            ok, error = False, str(exc)
        else:
            ok, error = _git(doc.repo, ["push", "-q", "origin", f"HEAD:{doc.branch}"])
        if not ok:
            _, sha = _git(doc.repo, ["rev-parse", "--short", "HEAD"])
            error = f"{doc.name} was committed in {doc.repo} as {sha} but not pushed to {doc.branch}: {error}"
    if ok:
        return None
    log.error("%s was saved but not committed and published: %s", doc.name, error)
    return error


def _git(repo: Path, args: list[str]) -> tuple[bool, str]:
    """Runs git in `repo`: (True, its output) or (False, what went wrong).

    Output is decoded as file names are, so a name that is not UTF-8, or holds a carriage return, goes back to git
    unchanged; `_shown` makes such a name safe for a message.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, timeout=GIT_TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    stdout = os.fsdecode(result.stdout)
    if result.returncode == 0:
        return True, stdout.rstrip()  # a file name in -z output may start with a space
    output = result.stderr.decode(errors="replace") or stdout
    return False, _shown(output.strip()[-500:]) or f"git exited {result.returncode}"


def _shown(text: str) -> str:
    """`text` with each byte git gave that is not UTF-8 as a replacement character, fit for a JSON reply."""
    return text.encode(errors="surrogateescape").decode(errors="replace")
