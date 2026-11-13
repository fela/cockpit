"""A PreToolUse hook that holds two posting rules: a comment or review an agent posts under
the user's account says in its first line that Claude Code wrote it, and an HTML page a session
shares goes on Cockpit, never to Claude artifacts.

Where permission settings let Linear comments run without the auto-mode check, this hook is what keeps
the rule for them. It also checks GitHub comments, replies and reviews (from `gh pr comment`, `gh pr review`,
`gh issue comment` and `gh api`) and Slack posts, which still go to the auto-mode check. Of the Artifact tool's calls, it lets through only those
that read or show an artifact that already exists (SPEC API-3).

Claude Code runs the hook before each matching call, with the call as JSON on stdin. The hook
refuses a post whose text lacks "[Claude Code]" in its first non-empty line: it exits 2 with the
reason on stderr, which Claude Code shows the session. Every other call passes with exit 0, and
the usual permission rules decide it. A post whose text the hook cannot find is refused, so a
command shape the hook does not know never lets an unattributed post through.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from dataclasses import dataclass
from typing import Any

from cockpit.config import user_name

MARK = "[Claude Code]"
# Linear's comment tool from the claude.ai connector, and from Linear's MCP server added to the machine as `linear`.
LINEAR_COMMENTS = ("mcp__claude_ai_Linear__save_comment", "mcp__linear__save_comment")
# Slack's tools that post text under the user's account, each with the field that holds the text: messages sent now,
# scheduled or left as a draft, and the comment on a file shared to a channel. From the claude.ai connector, and from
# Slack's MCP server added to the machine as `slack`.
SLACK_POSTS = {
    f"{server}__{tool}": field
    for server in ("mcp__claude_ai_Slack", "mcp__slack")
    for tool, field in (
        ("slack_send_message", "message"),
        ("slack_schedule_message", "message"),
        ("slack_send_message_draft", "message"),
        ("slack_complete_file_upload", "initial_comment"),
    )
}
ARTIFACT = "Artifact"
# The Artifact tool's actions that publish or change nothing; a call without an action publishes.
ARTIFACT_READS = frozenset({"read", "list", "open"})

# The rest of a heredoc's declaration: its operator, `-` when it strips leading tabs, and its delimiter (quoted, which
# keeps its text as written, or bare; `REVIEW-BODY` as much as `EOF`).
HEREDOC_START = re.compile(r"<<(-?)[ \t]*(?:'([^'\n]+)'|\"([\w.-]+)\"|\\?([\w.-]+))")
# Where `_scan` took out a heredoc's text: the operator's place holds its number between two of these.
HEREDOC_MARK = "\x00"
# `$(cat <<'EOF' ... EOF)` as a --body value, its heredoc's text taken out.
CAT_HEREDOC = re.compile(rf"\$\(\s*cat\s*<<{HEREDOC_MARK}(\d+){HEREDOC_MARK}\s*\)")
# A comment starts at a `#` that starts a word.
WORD_START = " \t\n;&|()<>"
# The characters that end a command, and those of a redirection's operator, as `_words` splits them.
SHELL_BREAKS = ";&|()\n"
REDIRECTS = "<>&|"
ANSI_C_STRING = re.compile(r"\$'((?:[^'\\]|\\.)*)'", re.DOTALL)
# The escapes of bash's `$'...'` strings that can change a text's first line; others stay as written.
ANSI_C_ESCAPE = re.compile(r"\\(.)", re.DOTALL)
ANSI_C_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", "'": "'", '"': '"'}
# The gh commands that post text under the user's account.
POSTS = (("pr", "comment"), ("pr", "review"), ("issue", "comment"))
ASSIGNMENT = re.compile(r"\w+=.*", re.DOTALL)
# The options of these gh commands that take a value: a body option gives the text (or the file that holds it), a
# repo option the repository.
BODY_OPTIONS = {"--body": "text", "--body-file": "file"}
REPO_OPTIONS = {"--repo"}
SHORT_BODY = {"b": "text", "F": "file"}
SHORT_REPO = "R"
GITHUB_URL = re.compile(r"^https://github\.com/([^/\s]+/[^/\s]+)")
# gh api's options that take a value, each with what the value gives: a field sent as written ("text"), a field whose
# `@FILE` value sends that file's text ("typed"), the file that holds the whole request ("input"), the method, or
# nothing the hook reads (""). Its other options take none (`--paginate`, `-i`).
API_OPTIONS = {
    "--raw-field": "text", "--field": "typed", "--input": "input", "--method": "method", "--header": "", "--jq": "",
    "--template": "", "--hostname": "", "--cache": "", "--preview": "",
}  # fmt: skip
API_SHORT = {"f": "text", "F": "typed", "X": "method", "H": "", "q": "", "t": "", "p": ""}
# The REST addresses a GitHub post goes to: an issue's, pull request's, commit's or gist's comments, a review comment's
# replies, and a pull request's reviews.
API_POSTS = re.compile(r"(?:^|/)(?:comments|replies|reviews)$")
# The REST addresses that change a post's text: one comment or review (`issues/comments/ID`, `pulls/comments/ID`,
# `comments/ID`, `gists/GIST/comments/ID`, `pulls/N/reviews/ID`), and the submission of a pending review with its
# text (`pulls/N/reviews/ID/events`). A DELETE to one removes the post and sends no text.
API_EDITS = re.compile(r"(?:^|/)(?:comments/[^/]+|reviews/[^/]+(?:/events)?)$")
# What a GraphQL mutation that posts text names: a `body`, or a mutation that adds or edits a comment or a review (its
# text may come inside an input object, `addComment(input: $input)`, `updateIssueComment(input: $input)`).
GRAPHQL_POST = re.compile(r"\bbody\b|\b(?:add|update)\w*(?:Comment|Review)\w*|\bsubmitPullRequestReview\b")
# A mutation's `body` argument fed by its $body variable, the one text of a mutation the hook reads, and a `body`
# argument fed by anything else.
BODY_VARIABLE = re.compile(r"(?<![$\w])body\s*:\s*\$body\b")
OTHER_BODY = re.compile(r"(?<![$\w])body\s*:(?!\s*\$body\b)")
# Text that may run a GitHub post, read loosely for a call the hook cannot split into words, once quotes and
# backslashes are taken out: `gh`, then `comment` or `review` as words, or `api` and then a body field, a mutation or
# --input; an API read of `/comments` or `reviewThreads` is none.
GH_POST = re.compile(r"\bgh\b[\s\S]*(?:\b(?:comment|review)\b|\bapi\b[\s\S]*(?:\bbody=(?!=)|\bmutation\b|--input\b))")
# Outside quotes, bash expands a word that holds these (pathname and brace expansion); `_scan` puts GLOB_MARK before
# each, so a word that holds one is no text or path the hook reads as written.
GLOBS = "*?[{"
GLOB_MARK = "\x01"
# A variable in braces, whose `{` is no brace expansion (`${CLAUDE_JOB_DIR}/tmp/review.md`).
PARAMETER = re.compile(r"\$\{\w+\}")
# `_scan` puts DESCRIPTOR_MARK after digits written against a redirection's operator (`2<<`, `2>`): bash reads them as
# the descriptor it redirects, and `123 <<` as an argument and stdin's redirection.
DESCRIPTOR_MARK = "\x02"
DESCRIPTOR = re.compile(r"(?:^|[\s;&|()<>])\d+$")
# A wrapper's option that starts the command after it in another folder (`env -C DIR`, `env --chdir=DIR`, `sudo -D`).
CHDIR = re.compile(r"-[^-]*[CD]|--chdir")
# Programs that run a script of their own (`bash -c SCRIPT`, `bash <<'EOF'`), whose posts the hook checks too.
SHELLS = frozenset({"bash", "sh", "zsh"})
# `Earlier.changed` for a call with a substitution: its commands run before the command they are in, and may change
# any file before any post reads it.
ANY_TIME = sys.maxsize
# A call that turns on noclobber, where `cat > FILE` leaves a file that exists as it was.
NOCLOBBER = re.compile(r"\bnoclobber\b|\b(?:set|bash|sh|dash|zsh|ksh)\b[^\n;&|]*?\s-[a-zA-Z]*C")
# What `_script` gives for a shell that reads its script from the call's stdin, which a pipe may feed.
STDIN_SCRIPT = "\x03"
XARGS_REFUSAL = (
    "This GitHub post runs under xargs, which puts words from its input into gh's (`-I{}`, or at the end), so the"
    " attribution hook cannot read its text. Post in a command of its own."
)


def refusal(call: dict[str, Any]) -> str | None:
    """Why the call breaks the posting rule, or None when it does not."""
    tool = call.get("tool_name")
    tool_input = call.get("tool_input") or {}
    if tool in LINEAR_COMMENTS:
        return _check(tool_input.get("body"), "This Linear comment")
    if tool in SLACK_POSTS:
        return _slack_refusal(tool_input, SLACK_POSTS[tool])
    if tool == ARTIFACT:
        if tool_input.get("action") in ARTIFACT_READS:
            return None
        return (
            f"{user_name()} shares HTML pages through Cockpit, not Claude artifacts. Write the page as one"
            " self-contained HTML file and run `cockpit page add ITEM --file PAGE.html --title \"...\" --as NAME`"
            " (ITEM: your item's key or id); `cockpit page update ID ...` replaces it under the same link."
            " Link the printed URL from your step or message."
        )
    if tool == "Bash":
        command = tool_input.get("command") or ""
        return _bash_refusal(command, command, Earlier(folder=call.get("cwd"), files={}, changed=-1))
    return None


@dataclass
class Shell:
    """A shell call as bash reads its words: `text` without its heredocs' text, comments and line continuations, each
    heredoc's operator holding its number in `heredocs`; each heredoc's text, with whether bash expands it; and the
    text of each command substitution inside double quotes or backticks, whose commands are no words of `text`."""

    text: str
    heredocs: list[tuple[str, bool]]
    substitutions: list[str]


@dataclass
class Post:
    """A GitHub post's command as gh reads its arguments: the options that give its text, each as its kind ("text",
    "file", or for gh api's --input the file of a JSON request, "json" or "graphql") and value, the repositories it
    names, its stdin's sources, each as its kind ("heredoc", "file", "text" or "other") and value, a GraphQL call's
    queries, each as its kind (the query as "text", the "file" that holds it, or the "graphql" request file of
    --input) and value, and whether it is a `gh api` call."""

    bodies: list[tuple[str, str]]
    repos: list[str]
    stdin: list[tuple[str, str]]
    queries: list[tuple[str, str]]
    api: bool


@dataclass
class Earlier:
    """What the commands before a post in its call did, as far as the hook can follow them: the folder the post runs
    in (None when the hook cannot tell); the files a plain `cat > FILE <<'EOF'` wrote, by which file it is
    (`_identity`), each with its place among the commands and its text (None when the hook cannot tell whether it
    ran); and the place of the last other command, which may have changed any file or variable (-1 for none)."""

    folder: str | None
    files: dict[str, tuple[int, tuple[str, bool] | None]]
    changed: int


def _example() -> str:
    return f"**[Claude Code]** _written and posted by Claude Code on {user_name()}'s behalf_"


def _slack_refusal(tool_input: dict[str, Any], field: str) -> str | None:
    """Why a Slack post breaks the posting rule, or None. A file upload that names no channel shares nothing."""
    if field == "initial_comment" and not tool_input.get("channel_id"):
        return None
    text = tool_input.get(field)
    if not text:
        return (
            f"This Slack post goes out under {user_name()}'s account without text in `{field}`. Give it text that"
            f" starts with the line: {_example()}"
        )
    return _check(text, "This Slack post")


def _check(text: str | None, what: str) -> str | None:
    if text is None:
        return (
            f"{what} has no text the attribution hook can read. Pass the text with --body \"...\", "
            f"--body-file FILE, or --body-file - with a heredoc, and start it with the line: {_example()}"
            " The hook reads a body file only when nothing but `cd`s, other posts and plain `cat > FILE <<'EOF'`"
            " writes runs before the post in its call; write it in an earlier call otherwise."
        )
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if MARK in first:
        return None
    return (
        f"{what} goes out under {user_name()}'s account, so its first line must say Claude Code wrote it "
        f"on {user_name()}'s behalf. Start the text with: {_example()}"
    )


def _bash_refusal(command: str, whole: str, earlier: Earlier) -> str | None:
    """Why a shell call's GitHub posts break the posting rule, or None. `whole` is the call the session made, of
    which `command` is a part, and `earlier` what ran before it. Each simple command is read as bash runs it: a
    relative body file from the folder the call's `cd`s moved to, and a body file the call wrote before the post
    from what wrote it, as far as the hook can tell that each ran (`_certainty`)."""
    shell = _scan(command)
    words = _words(shell.text) if shell else None
    if shell is None or words is None:
        if GH_POST.search(re.sub(r"[\"'\\]", "", command)):
            return (
                "The attribution hook cannot split this call into words (unbalanced quotes, an unended heredoc, a"
                " `$(...)` or backtick in a heredoc bash expands, or a `$'...'` escape it does not read). Write the"
                " post's text to a file, pass it with --body-file FILE (with gh api, `-F body=@FILE`), and start it"
                f" with the line: {_example()}"
            )
        return None
    # A substitution's commands run where the hook cannot follow them, and before the command they are in: what the
    # call wrote may be in any file, also one a post in the same command reads.
    # A script of its own (a substitution's, a shell's) starts where the call may have changed any file before it.
    for inner in shell.substitutions:
        reason = _bash_refusal(inner, whole, Earlier(folder=None, files={}, changed=0))
        if reason:
            return reason
    if shell.substitutions:
        earlier = Earlier(earlier.folder, {}, ANY_TIME)
    # The state a command sees if it runs: `sure` after its list's head, `chain` inside the `&&` chain it is in.
    sure, chain = earlier, Earlier(earlier.folder, dict(earlier.files), earlier.changed)
    # CDPATH sends `cd project` to a folder it lists; noclobber keeps a write from replacing a file.
    searched = bool(os.environ.get("CDPATH")) or "CDPATH" in whole
    clobbers = not NOCLOBBER.search(whole.replace("\\\n", "")) and "noclobber" not in os.environ.get("SHELLOPTS", "")
    lead = "sure"
    for place, (command_words, depth, before, after) in enumerate(_commands(words), start=1):
        if before in (";", "&", "||"):
            chain = Earlier(sure.folder, dict(sure.files), sure.changed)
        lead = _certainty(lead, before)
        certainty = lead if depth == 0 and before != "|" and after not in ("|", "&", "||") else "unsure"
        if not command_words:
            continue
        assignments = 0
        while assignments < len(command_words) and ASSIGNMENT.fullmatch(command_words[assignments]):
            assignments += 1
        program = command_words[assignments:]
        script = _script(program, shell)
        if script == STDIN_SCRIPT:
            if before == "|" and GH_POST.search(re.sub(r"[\"'\\]", "", command)):
                return (
                    "This call pipes a script into a shell, and the attribution hook cannot read what the script posts."
                    " Give the shell its script with `bash <<'EOF'` or `bash -c`, or post in a call of its own."
                )
        elif script is not None:
            # xargs puts words from its input into the script too (`xargs -I{} bash -c 'gh pr comment 1 --body "{}"'`).
            under_xargs = any(os.path.basename(word) == "xargs" for word in program)
            if under_xargs and GH_POST.search(re.sub(r"[\"'\\]", "", script)):
                return XARGS_REFUSAL
            reason = _bash_refusal(script, whole, Earlier(folder=None, files={}, changed=0))
            if reason:
                return reason
        found = _gh_post(program)
        if found is not None:
            wrapper, post, post_words = found
            # A delete posts nothing, nor does a call for gh's help; one that also carries a body is checked like any
            # post.
            quiet = ("--delete-last" in program or {"--help", "-h"} & set(post_words)) and not post.bodies
            if not quiet and any(os.path.basename(word) == "xargs" for word in wrapper):
                return XARGS_REFUSAL
            # A wrapper that starts gh in another folder leaves a relative body file unknown.
            moved = any(CHDIR.match(word) for word in wrapper)
            reason = None if quiet else _post_refusal(
                post, shell, Earlier(None, chain.files, chain.changed) if moved else chain
            )
            if reason:
                return reason
            # A post's output, or gh's help, can go to a file.
            if not _writes(program):
                continue
        elif program[:1] in (["cd"], ["pushd"]) and not _writes(program):
            # `cd x 2>&1` moves to x alone.
            arguments = _redirections(program[1:])[0]
            options = ""
            while arguments and re.fullmatch(r"-[LPe@]+", arguments[0]):
                options += arguments[0]
                arguments = arguments[1:]
            arguments = arguments[1:] if arguments[:1] == ["--"] else arguments
            target = _expand(arguments[0] if arguments else "~", chain.changed)
            # bash stays where it is when cd has more than one folder; pushd without one swaps folders.
            one = len(arguments) == 1 or (not arguments and program[0] == "cd")
            folder = None
            relative = target is not None and not os.path.isabs(target) and target.split("/")[0] not in (".", "..")
            if (
                one and target is not None and target != "-" and (chain.folder or os.path.isabs(target))
                and not (searched and relative)
            ):
                # bash follows the folder's path as written (`cd link && cd ..` goes back), its links resolved with -P;
                # it fails where a folder before a `..` is missing, and stays where it was.
                folder = os.path.join(chain.folder or "", target)
                if ".." in target.split("/") and not os.path.isdir(folder):
                    folder = None
                physical = re.findall("[LP]", options)[-1:] == ["P"]
                if folder is not None:
                    folder = os.path.realpath(folder) if physical else os.path.normpath(folder)
            chain.folder = folder if certainty != "unsure" else None
            sure.folder = folder if certainty == "sure" else None
            continue
        elif clobbers and (written := _written(program, shell, chain.folder, chain.changed)):
            key, text = written
            chain.files[key] = (place, text if certainty != "unsure" else None)
            sure.files[key] = (place, text if certainty == "sure" else None)
            continue
        # Any other command may change any file, the folder (`if cd x`, `popd`, `source`) or a variable.
        for state in (sure, chain):
            # A substitution's ANY_TIME stays.
            state.changed = max(state.changed, place)
            if {"cd", "pushd", "popd"} & set(program) or program[:1] in (["eval"], ["source"], ["."]):
                state.folder = None
    return None


def _certainty(lead: str, before: str) -> str:
    """Whether a command ran, for the commands after it, from its list operator and that of the command before it:
    "sure" for the head of a list, "chain" for a command an unbroken `&&` chain from the head leads to (it ran when
    a later command in the chain runs), "unsure" after `||`. A pipe keeps what came before."""
    if before in (";", "&"):
        return "sure"
    if before == "||":
        return "unsure"
    if before == "&&":
        return "chain" if lead != "unsure" else "unsure"
    return lead


def _writes(program: list[str]) -> bool:
    """Whether a command sends output to a file (`>`, `2>`, `>>`, `&>`, `>& FILE`); `2>&1` and `>&-` only join or close
    its outputs, and `/dev/null` drops them."""
    return any(
        ">" in operator and target != "/dev/null" and not (operator.endswith("&") and re.fullmatch(r"\d*-?", target))
        for _, operator, target in _redirections(program)[1]
    )


def _redirect(word: str) -> bool:
    """Whether a word is a redirection's operator (`<`, `<<`, `2>`'s `>`, `>&`)."""
    return set(word) <= set(REDIRECTS) and bool(set(word) & set("<>"))


def _redirections(words: list[str]) -> tuple[list[str], list[tuple[str, str, str]]]:
    """A command's words without its redirections, which bash takes out of the arguments, and each redirection as its
    descriptor (`2` of `2<<`, `_scan`'s DESCRIPTOR_MARK; "" for none written), operator and target."""
    plain: list[str] = []
    redirections: list[tuple[str, str, str]] = []
    descriptor = ""
    index = 0
    while index < len(words):
        word = words[index]
        index += 1
        if word.endswith(DESCRIPTOR_MARK):
            descriptor = word.removesuffix(DESCRIPTOR_MARK)
        elif _redirect(word):
            redirections.append((descriptor, word, words[index] if index < len(words) else ""))
            descriptor = ""
            index += 1
        else:
            plain.append(word)
    return plain, redirections


def _gh_post(program: list[str]) -> tuple[list[str], Post, list[str]] | None:
    """The words in front of a GitHub post's `gh`, the post, and its words after its subcommand (`gh pr comment`,
    `gh api`), with options gh took before it (`gh pr -R x comment`, `gh --repo x pr comment`, `gh pr --body TEXT
    comment`) put first; None for a command that runs no post. Any word in front of `gh` may run it (`then gh`,
    `xargs gh`)."""
    for index, word in enumerate(program):
        if os.path.basename(word) != "gh":
            continue
        names: list[str] = []
        moved: list[str] = []
        # bash takes a redirection out of gh's words wherever it stands (`gh pr 2>/dev/null comment`, `gh --repo
        # 2>/dev/null R pr comment`, `< review.md gh pr comment`); the post's words keep it, in order, for its stdin.
        wrapper, leading = _redirections(program[:index])
        rest, redirections = _redirections(program[index + 1 :])
        redirected = [
            part
            for descriptor, operator, target in (*leading, *redirections)
            for part in (*([descriptor + DESCRIPTOR_MARK] if descriptor else []), operator, target)
        ]
        # An option the hook does not know may take the word after it (`gh pr --attach shot.png comment`): that word
        # is its value unless it leads on to a post.
        unknown = False
        while rest and len(names) < 2 and names != ["api"]:
            word, rest = rest[0], rest[1:]
            if word in (*REPO_OPTIONS, *BODY_OPTIONS, *(f"-{char}" for char in (*SHORT_BODY, SHORT_REPO))):
                moved += [word, *rest[:1]]
                rest = rest[1:]
                unknown = False
            elif word.startswith("-"):
                moved.append(word)
                unknown = "=" not in word
            elif unknown and not any(post[: len(names) + 1] == (*names, word) for post in (*POSTS, ("api",))):
                moved.append(word)
                unknown = False
            else:
                names.append(word)
                unknown = False
        words = moved + rest + redirected
        if tuple(names) in POSTS:
            return wrapper, _post(words), words
        if names == ["api"] and (post := _api_post(words)) is not None:
            return wrapper, post, words
    return None


def _api_post(words: list[str]) -> Post | None:
    """A `gh api` call as a post, from its words after `api`; None for a call that posts no text. A REST call posts
    when it sends a request body (fields or --input, a method other than GET or HEAD) to an address that ends in
    `comments`, `replies` or `reviews`, or sends one with a method other than DELETE to one comment or review
    (`API_EDITS`). A GraphQL call may post when a query of it is a mutation that posts text (`_posts_text`), or is one
    the call does not hold (`-F query=@FILE`, --input), which `_post_refusal` reads. The post's text is its `body` field
    (sent as written, or the file a typed field's `@FILE` names), or the JSON request --input sends."""
    words, redirections = _redirections(words)
    address = None
    methods: list[str] = []
    fields: list[tuple[str, str, str]] = []
    inputs: list[str] = []
    options = True
    index = 0
    while index < len(words):
        word = words[index]
        index += 1
        if options and word == "--":
            options = False
            continue
        if options and word.startswith("--"):
            name, equals, value = word.partition("=")
            if name not in API_OPTIONS:
                continue
            kind = API_OPTIONS[name]
        elif options and len(word) > 1 and word.startswith("-"):
            # Short options run together (`-iXPOST`), read as gh reads them: an option followed by `=` and more takes
            # that as its value, also one that needs none (`-f=body=TEXT`, `-i=t`); else the first that takes a value
            # takes the rest of the word, or the next word.
            position = next(
                (
                    at
                    for at in range(1, len(word))
                    if word[at] in API_SHORT or (word[at + 1 : at + 2] == "=" and word[at + 2 :])
                ),
                None,
            )
            if position is None or word[position] not in API_SHORT:
                continue
            kind = API_SHORT[word[position]]
            equals = word[position + 1 :]
            value = equals[1:] if len(equals) > 1 and equals[0] == "=" else equals
        else:
            address = word if address is None else address
            continue
        if not equals:
            value = words[index] if index < len(words) else ""
            index += 1
        if kind == "method":
            methods.append(value.upper())
        elif kind == "input":
            inputs.append(value)
        elif kind and "=" in value:
            key, _, field = value.partition("=")
            fields.append((kind, key, field))
    bodies = [
        ("file", field[1:]) if kind == "typed" and field.startswith("@") else ("text", field)
        for kind, key, field in fields
        if key == "body"
    ]
    stdin = _stdin(redirections)
    # The address's path, without its query string or `#` part, neither of which GitHub's routing reads.
    endpoint = re.split("[?#]", (address or "").replace(GLOB_MARK, ""))[0].rstrip("/")
    if endpoint.split("/")[-1] == "graphql":
        requests = [("graphql", path) for path in inputs]
        queries = [
            ("file", field[1:]) if kind == "typed" and field.startswith("@") else ("text", field.replace(GLOB_MARK, ""))
            for kind, key, field in fields
            if key == "query"
        ]
        if all(kind == "text" and not _posts_text(query) for kind, query in queries + requests):
            return None
        return Post(bodies=bodies + requests, repos=[], stdin=stdin, queries=queries + requests, api=True)
    # gh sends a GET unless told otherwise, or a POST when the call gives fields or --input.
    method = methods[-1] if methods else ("POST" if fields or inputs else "GET")
    if (method not in ("GET", "HEAD") and API_POSTS.search(endpoint)) or (
        method not in ("GET", "HEAD", "DELETE") and API_EDITS.search(endpoint)
    ):
        return Post(bodies=bodies + [("json", path) for path in inputs], repos=[], stdin=stdin, queries=[], api=True)
    return None


def _posts_text(query: str) -> bool:
    """The GraphQL query is a mutation that posts text."""
    return bool(re.search(r"\bmutation\b", query) and GRAPHQL_POST.search(query))


def _query(source: tuple[str, str], post: Post, shell: Shell, earlier: Earlier) -> str | None:
    """A GraphQL call's query from one of its sources (`Post.queries`): the query as the call writes it, the text of
    the file that holds it, or the `query` of the JSON request --input sends; None for one the hook cannot read, or
    whose `$`s, backticks and backslashes bash may change."""
    kind, value = source
    if kind == "text":
        return value
    text = _stdin_text(post, shell, earlier) if value == "-" else _file(value, earlier)
    if text is None or (text[1] and set("$`\\") & set(text[0])):
        return None
    if kind == "file":
        return text[0]
    try:
        request = json.loads(text[0])
    except ValueError:
        return None
    query = request.get("query") if isinstance(request, dict) else None
    return query if isinstance(query, str) else None


def _script(program: list[str], shell: Shell) -> str | None:
    """The script a command runs as a shell call of its own: `bash -c SCRIPT` (or sh, zsh, with other options and
    their values around `-c`, `-o pipefail`), a script on stdin when no script file is named (`bash <<'EOF'`, `bash -s
    -- ARG <<'EOF'`, `bash <<< SCRIPT`), or `eval WORDS`; None for any other command."""
    for index, word in enumerate(program):
        if word == "eval":
            return " ".join(program[index + 1 :]).replace(GLOB_MARK, "")
        if os.path.basename(word) not in SHELLS:
            continue
        words, redirections = _redirections(program[index + 1 :])
        options = ""
        while words and len(words[0]) > 1 and words[0][0] in "-+" and words[0] != "--":
            option, words = words[0], words[1:]
            if option in ("--rcfile", "--init-file"):
                words = words[1:]
            elif not option.startswith("--"):
                options += option[1:]
                # `-o NAME` and `-O NAME` set an option by its name.
                words = words[1:] if set(option[1:]) & set("oO") else words
        words = words[1:] if words[:1] == ["--"] else words
        # `-n` reads the script and runs none of it.
        if "n" in options:
            return ""
        if "c" in options:
            return (words[0] if words else "").replace(GLOB_MARK, "")
        if words and "s" not in options:
            return None
        stdin = [(operator, target) for descriptor, operator, target in redirections if descriptor in ("", "0")]
        operator, target = stdin[-1] if stdin else ("", "")
        if operator == "<<":
            text = _heredoc(target, shell)
            return text[0] if text else None
        if operator == "<<<":
            return target.replace(GLOB_MARK, "")
        return STDIN_SCRIPT if not operator else None
    return None


def _written(
    program: list[str], shell: Shell, folder: str | None, changed: int
) -> tuple[str, tuple[str, bool]] | None:
    """Which file a plain `cat > FILE <<'EOF'` (or `cat <<'EOF' > FILE`) writes (`_identity`) and its heredoc's text,
    its path expanded as `_expand` does after the command at `changed`; None for any other command, or a file the hook
    cannot tell."""
    if program[:1] != ["cat"] or len(program) != 5:
        return None
    pairs = dict(zip(program[1::2], program[2::2]))
    if set(pairs) != {">", "<<"}:
        return None
    text = _heredoc(pairs["<<"], shell)
    path = _expand(pairs[">"], changed)
    if text is None or path is None or not (os.path.isabs(path) or folder):
        return None
    full = os.path.join(folder or "", path)
    # A file the write cannot replace keeps what it holds.
    if os.path.exists(full) and not os.access(full, os.W_OK):
        return None
    return _identity(full), text


def _expand(path: str, changed: int) -> str | None:
    """A path as bash expands it: from the hook's own environment, which only another command can change (`export`),
    so None for a path that names a variable after one (`changed`, the place of the last, -1 for none); None too when
    it names a variable the hook has no value for or the folder (`$PWD`, `$OLDPWD`, which a `cd` changes), or holds a
    pattern bash would expand."""
    if GLOB_MARK in path or "`" in path or re.search(r"\$\{?(?:PWD|OLDPWD)\b", path) or ("$" in path and changed >= 0):
        return None
    # Unquoted, bash splits a variable's value at a space and expands its patterns (`cat > $DRAFT` fails as an
    # ambiguous redirect), and the hook does not know whether it was quoted.
    if any(re.search(r"[\s*?\[]", os.environ.get(name, "")) for name in re.findall(r"\$\{?(\w+)", path)):
        return None
    path = os.path.expanduser(os.path.expandvars(path))
    return None if "$" in path else path


def _identity(path: str) -> str:
    """Which file a path opens: its device and inode when it exists, so a link to it is the same file, else the path
    with its folders' links resolved."""
    try:
        status = os.stat(path)
    except OSError:
        return os.path.realpath(path)
    return f"{status.st_dev}:{status.st_ino}"


def _scan(command: str) -> Shell | None:
    """The call as bash reads its words (Shell); None when its quotes or heredocs do not end, a heredoc bash expands
    runs a command (`$(...)` or a backtick), or a `$'...'` string has an escape the hook does not decode. Single
    quotes keep their text as written; a backslash at a line's end joins the lines outside them; a comment runs to
    its line's end, which still ends its command; a heredoc's text starts at the next line end outside quotes, inside
    a `$(...)` too, and is no word; digits against a redirection's operator get DESCRIPTOR_MARK; a `$'...'` string is
    its text, quoted; a `$(...)` inside double quotes and a
    backtick substitution keep their text as one word, the commands in it kept apart in `substitutions`; and outside
    quotes, GLOB_MARK goes before each of GLOBS."""
    out: list[str] = []
    heredocs: list[tuple[str, bool]] = []
    substitutions: list[str] = []
    pending: list[tuple[str, bool, bool]] = []
    # "" the shell, '"' double quotes, "(" a substitution or group, where quotes start anew; each with where a
    # substitution inside double quotes starts, else -1.
    stack: list[tuple[str, int]] = [("", -1)]
    index, end = 0, len(command)
    while index < end:
        char = command[index]
        if char == "\\" and index + 1 < end:
            if command[index + 1] != "\n":
                out.append(command[index : index + 2])
            index += 2
            continue
        if char == "`":
            close = index + 1
            while close < end and command[close] != "`":
                close += 2 if command[close] == "\\" else 1
            if close >= end:
                return None
            substitutions.append(re.sub(r"\\([`$\\])", r"\1", command[index + 1 : close]))
            out.append(command[index : close + 1])
            index = close + 1
            continue
        if stack[-1][0] == '"':
            if command.startswith("$(", index):
                stack.append(("(", index + 2))
                out.append("$(")
                index += 2
                continue
            if char == '"':
                stack.pop()
            out.append(char)
            index += 1
            continue
        if char in "<>" and DESCRIPTOR.search("".join(out[-16:])):
            out.append(DESCRIPTOR_MARK)
        if char == "'":
            close = command.find("'", index + 1)
            if close < 0:
                return None
            out.append(command[index : close + 1])
            index = close + 1
        elif string := ANSI_C_STRING.match(command, index):
            if any(escape[1] not in ANSI_C_ESCAPES for escape in ANSI_C_ESCAPE.finditer(string[1])):
                return None
            out.append(shlex.quote(ANSI_C_ESCAPE.sub(lambda escape: ANSI_C_ESCAPES[escape[1]], string[1])))
            index = string.end()
        elif char == '"':
            stack.append(('"', -1))
            out.append(char)
            index += 1
        # An escaped space or operator (`cd PR\\ #10`) is part of its word, so no comment starts after it.
        elif char == "#" and (previous := "".join(out[-1:]))[-1:] in ("", *WORD_START) and previous[:1] != "\\":
            close = command.find("\n", index)
            index = end if close < 0 else close
        elif command.startswith("<<<", index):
            out.append("<<<")
            index += 3
        elif start := HEREDOC_START.match(command, index):
            delimiter = start[2] or start[3] or start[4]
            out.append(f" <<{HEREDOC_MARK}{len(heredocs) + len(pending)}{HEREDOC_MARK} ")
            pending.append((delimiter, start[1] == "-", start[4] is not None and "\\" not in start[0]))
            index = start.end()
        elif parameter := PARAMETER.match(command, index):
            # `${NAME}` is a variable, no brace expansion.
            out.append(parameter[0])
            index = parameter.end()
        elif char == "\n" and pending:
            out.append("\n")
            index += 1
            for delimiter, tabs, expands in pending:
                lines: list[str] = []
                while True:
                    if index >= end:
                        return None
                    close = command.find("\n", index)
                    line = command[index : end if close < 0 else close]
                    stripped = line.lstrip("\t") if tabs else line
                    if stripped == delimiter:
                        index = end if close < 0 else close + 1
                        break
                    if stripped.startswith(delimiter + ")") and any(kind == "(" and at >= 0 for kind, at in stack):
                        # Inside `$(...)` or `<(...)`, `EOF)` ends the heredoc, and the `)` the substitution.
                        index += len(line) - len(stripped) + len(delimiter)
                        break
                    lines.append(stripped)
                    index = end if close < 0 else close + 1
                text = "\n".join(lines)
                if expands and ("$(" in text or "`" in text):
                    return None
                heredocs.append((text.replace("\\\n", "") if expands else text, expands))
            pending = []
        else:
            if char == "(":
                # `$(...)`, `<(...)` and `>(...)` outside quotes run commands too.
                stack.append(("(", index + 1 if "".join(out[-1:]).endswith(("$", "<", ">")) else -1))
            elif char == ")" and stack[-1][0] == "(":
                if stack[-1][1] >= 0:
                    substitutions.append(command[stack[-1][1] : index])
                stack.pop()
            out.append(GLOB_MARK + char if char in GLOBS else char)
            index += 1
    if pending or stack[-1][0] == '"':
        return None
    return Shell(text="".join(out), heredocs=heredocs, substitutions=substitutions)


def _words(text: str) -> list[str] | None:
    """The words of `_scan`'s text, with each run of shell operators (`&&`, `;`, `2>`'s `>`) a word of its own."""
    lexer = shlex.shlex(text, posix=True, punctuation_chars=SHELL_BREAKS + "<>")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return None


def _commands(words: list[str]) -> list[tuple[list[str], int, str, str]]:
    """Each simple command's words, with how many groups or substitutions it is inside and the list operators before
    and after it (`_operator`)."""
    commands: list[tuple[list[str], int, str, str]] = []
    current: list[str] = []
    depth = 0
    before = ";"
    for word in words:
        if word and all(char in SHELL_BREAKS for char in word):
            # A line end after `&&`, `||` or `|` continues the list, a comment between them too (`a && # note`).
            if not current and set(word) == {"\n"} and before in ("&&", "||", "|"):
                continue
            commands.append((current, depth, before, _operator(word)))
            current = []
            before = _operator(word)
            depth = max(0, depth + word.count("(") - word.count(")"))
        else:
            current.append(word)
    commands.append((current, depth, before, ";"))
    return commands


def _operator(word: str) -> str:
    """The list operator in a run of shell breaks: `&&`, `||`, `|` or `&`, else `;` (a line end or a group's
    parenthesis)."""
    return next((operator for operator in ("&&", "||", "|", "&") if operator in word), ";")


def _post_refusal(post: Post, shell: Shell, earlier: Earlier) -> str | None:
    """Why a GitHub post breaks the posting rule, or None. Repeated body, stdin and repository
    arguments are refused because gh uses their last value."""
    # gh takes a repository as [HOST/]OWNER/REPO or its URL, and GitHub reads its name in any case.
    repos = list(dict.fromkeys(
        re.sub(r"^(?:https?://)?github\.com/", "", repo.casefold()).removesuffix("/").removesuffix(".git")
        for repo in post.repos
    ))
    if len(post.bodies) > 1 and post.api:
        return f"This GitHub post gives its text {len(post.bodies)} times, in body fields or --input. Give it once."
    if len(post.bodies) > 1:
        return f"This GitHub post gives its text {len(post.bodies)} times, and gh posts only the last. Give it once."
    if len(post.stdin) > 1:
        return f"This GitHub post redirects its stdin {len(post.stdin)} times, and gh reads only the last. Give it once."
    if len(repos) > 1:
        return f"This GitHub post names its repository {len(repos)} times ({', '.join(repos)}). Name it once."
    # A GraphQL call posts nothing when the hook reads each of its queries and none posts text; one that posts gives
    # the hook its text only as the $body variable its `body` arguments take.
    queries = [_query(source, post, shell, earlier) for source in post.queries]
    if queries and all(query is not None and not _posts_text(query) for query in queries):
        return None
    followed = all(
        query is not None and (not _posts_text(query) or (BODY_VARIABLE.search(query) and not OTHER_BODY.search(query)))
        for query in queries
    )
    body = _body(post, shell, earlier) if followed else None
    if body is not None and GLOB_MARK in body[0]:
        return (
            "This GitHub post's text has an unquoted `*`, `?`, `[` or `{`, which bash may expand into other text."
            f" Quote the text, and start it with the line: {_example()}"
        )
    if body is not None and body[1]:
        first = next((line for line in body[0].splitlines() if line.strip()), "")
        if set("$`") & set(first.partition(MARK)[0]):
            return (
                "This GitHub post's first line has a `$` or a backtick before [Claude Code], and bash may expand it"
                f" into other text. Start the text with the line: {_example()}"
            )
    if body is None and post.api:
        return (
            "This GitHub post through gh api has no text the attribution hook can read. Give the text as its body"
            " field (`-f body=...`, `-F body=@FILE`, or `-F body=@-` with a heredoc), or as `body` in the JSON that"
            " `--input FILE` sends; in a GraphQL mutation, as the $body variable its `body:` argument takes, the"
            " query written in the call or in a file the hook can read. Start the text with the line:"
            f" {_example()} The hook reads a file only when nothing but `cd`s, other posts and plain"
            " `cat > FILE <<'EOF'` writes runs before the post in its call, and JSON from a heredoc only when the"
            " heredoc is quoted (`<<'EOF'`)."
        )
    return _check(body[0] if body else None, "This GitHub post")


def _stdin(redirections: list[tuple[str, str, str]]) -> list[tuple[str, str]]:
    """A command's stdin's sources, from its redirections (`_redirections`), each as its kind ("heredoc", "file",
    "text" or "other") and value. `2<<'EOF'` feeds another descriptor, not stdin."""
    kinds = {"<": "file", "<<": "heredoc", "<<<": "text"}
    return [
        (kinds.get(operator, "other"), target)
        for descriptor, operator, target in redirections
        if "<" in operator and descriptor in ("", "0")
    ]


def _post(words: list[str]) -> Post:
    """The post as gh reads its arguments, from its words after `gh pr comment`."""
    words, redirections = _redirections(words)
    post = Post(bodies=[], repos=[], stdin=_stdin(redirections), queries=[], api=False)
    options = True
    index = 0
    while index < len(words):
        word = words[index]
        index += 1
        value = words[index] if index < len(words) else ""
        if word == "--":
            options = False
            continue
        if not options:
            if match := GITHUB_URL.match(word):
                post.repos.append(match.group(1))
            continue
        name, equals, attached = word.partition("=")
        if name in BODY_OPTIONS.keys() | REPO_OPTIONS:
            value = attached if equals else value
            index += not equals
            if name in BODY_OPTIONS:
                post.bodies.append((BODY_OPTIONS[name], value))
            else:
                post.repos.append(value)
        elif word.startswith("-") and not word.startswith("--"):
            # Short options run together (`-cbTEXT`): the first that takes a value takes the rest, or the next word.
            for position, char in enumerate(word[1:], start=2):
                if char in SHORT_BODY.keys() | {SHORT_REPO}:
                    # gh drops an `=` between a short option and its value (`-F=body.md`).
                    value = word[position:].removeprefix("=") or value
                    index += not word[position:]
                    if char in SHORT_BODY:
                        post.bodies.append((SHORT_BODY[char], value))
                    else:
                        post.repos.append(value)
                    break
        elif match := GITHUB_URL.match(word):
            post.repos.append(match.group(1))
    return post


def _body(post: Post, shell: Shell, earlier: Earlier) -> tuple[str, bool] | None:
    """The text the post gives, with whether bash expands it from what the call says: its body option's text, the
    file it names, or its stdin's heredoc, file or string, read from the post's own words alone; for gh api's
    --input, the `body` of the JSON request in that file (a GraphQL request's in its variables). None when it gives
    none the hook can read."""
    if not post.bodies:
        return None
    kind, value = post.bodies[0]
    if kind == "text":
        if cat := CAT_HEREDOC.fullmatch(value):
            return _heredoc(f"{HEREDOC_MARK}{cat[1]}{HEREDOC_MARK}", shell)
        return value, True
    text = _stdin_text(post, shell, earlier) if value == "-" else _file(value, earlier)
    if kind == "file" or text is None:
        return text
    # bash may change the `$`s, backticks and backslashes of a text it expands, and with them the JSON's text.
    if text[1] and set("$`\\") & set(text[0]):
        return None
    try:
        request = json.loads(text[0])
    except ValueError:
        return None
    if kind == "graphql" and isinstance(request, dict):
        request = request.get("variables")
    body = request.get("body") if isinstance(request, dict) else None
    return (body, False) if isinstance(body, str) else None


def _stdin_text(post: Post, shell: Shell, earlier: Earlier) -> tuple[str, bool] | None:
    """The text of the post's stdin, with whether bash expands it: its heredoc, file or string; None for none the
    hook can read."""
    if not post.stdin:
        return None
    source, target = post.stdin[0]
    if source == "heredoc":
        return _heredoc(target, shell)
    if source == "file":
        return _file(target, earlier)
    return (target + "\n", True) if source == "text" else None


def _heredoc(mark: str, shell: Shell) -> tuple[str, bool] | None:
    """The heredoc whose number `mark` holds, as `_scan` marked it; None for any other word."""
    number = mark.removeprefix(HEREDOC_MARK).removesuffix(HEREDOC_MARK)
    return shell.heredocs[int(number)] if number.isdigit() and int(number) < len(shell.heredocs) else None


def _file(path: str, earlier: Earlier) -> tuple[str, bool] | None:
    """A body file's text as the post reads it: what a `cat > FILE <<'EOF'` before it in the call wrote, else the
    file on disk; None when the hook cannot tell which file bash opens, or what is in it: another command ran after
    that write, or before the post when nothing wrote it, and may have changed it."""
    expanded = _expand(path, earlier.changed)
    if expanded is None or not (os.path.isabs(expanded) or earlier.folder):
        return None
    full = os.path.join(earlier.folder or "", expanded)
    written = earlier.files.get(_identity(full))
    if written is not None and written[0] > earlier.changed:
        return written[1]
    if written is not None or earlier.changed >= 0:
        return None
    try:
        with open(full, encoding="utf-8") as file:
            return file.read(), False
    except OSError:
        return None


def main() -> None:
    try:
        call = json.load(sys.stdin)
    except ValueError as exc:
        print(f"The attribution hook could not read the call: {exc}", file=sys.stderr)
        sys.exit(2)
    reason = refusal(call)
    if reason:
        print(reason, file=sys.stderr)
        sys.exit(2)
