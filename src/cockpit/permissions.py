"""The user's Claude Code permissions on Cockpit: the rules (SPEC PERM-2) and the tool calls
The user's hooks report (SPEC PERM-1).

Claude Code reads two allow lists from ~/.claude/settings.json and reloads the file in
running sessions when it changes:

- `permissions.allow`: patterns on tool calls, such as `Bash(gh pr view *)`. A call that
  matches runs without asking; in auto mode it skips the auto-mode check.
- `autoMode.allow`: plain sentences the auto-mode check weighs when it judges an action.
  The entry `$defaults` stands for Claude Code's built-in sentences; a list without it
  replaces them.

Only the user changes them, on the page. The auto-mode check stops agents from loosening
their own rules, and the server holds to the same.

Two hooks report tool calls. `PermissionDenied` runs `cockpit permission report` when auto
mode blocks a call; the user can add a rule for it, and Cockpit tells the session to retry.
`PermissionRequest` runs `cockpit permission ask` while a session shows a permission dialog;
the hook waits for the user's answer here, and whichever of the dialog and Cockpit answers first wins.

The session that made a call can take it back once it no longer needs it (SPEC PERM-6),
and propose the auto-mode sentence that would allow it (SPEC PERM-7). While the user works on
the call's card, a withdrawal waits until the user answers or stops working on it, and the
answer still adds its rule (SPEC PERM-9).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import sqlite3
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from cockpit import config, db, patterns, rank, store
from cockpit.config import user_name
from cockpit.sessions import Session

log = logging.getLogger(__name__)

ListName = Literal["allow", "auto"]
RequestKind = Literal["denied", "asked"]
LIST_WORDS = {"allow": "tool rules", "auto": "auto-mode sentences"}
DEFAULTS = "$defaults"
RULE = re.compile(r"[A-Za-z][\w.-]*(\(.+\))?")
RULE_MAX = 500
SENTENCE_MIN = 10
SENTENCE_MAX = 500
# Rules that let a shell run any command at all. Auto mode drops them anyway; in the
# other modes they would turn every check off.
ANY_COMMAND = frozenset({"Bash", "Bash(*)", "Bash(:*)", "Bash(* *)"})

# Held while the file is written; reentrant because rule changes call the file writers.
_write_lock = threading.RLock()


def _read(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    settings = json.loads(text)
    if not isinstance(settings, dict):
        raise store.Invalid(f"{path} does not hold a JSON object")
    return settings


def _write(path: Path, settings: dict[str, Any]) -> None:
    """Replace the file in one step, so Claude Code never reads half of it."""
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=".settings-", suffix=".json")
    try:
        with os.fdopen(handle, "w") as file:
            json.dump(settings, file, indent=2, ensure_ascii=False)
            file.write("\n")
        os.chmod(temp, mode)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def rules(path: Path) -> dict[str, Any]:
    settings = _read(path)
    auto = settings.get("autoMode", {}).get("allow")
    return {
        "path": str(path),
        "mode": settings.get("permissions", {}).get("defaultMode", "default"),
        "allow": list(settings.get("permissions", {}).get("allow", [])),
        # No list means only the built-in sentences apply.
        "auto": [entry for entry in auto if entry != DEFAULTS] if auto is not None else [],
        "auto_keeps_defaults": auto is None or DEFAULTS in auto,
    }


def auto_sentences(path: Path) -> list[str]:
    """The user's auto-mode sentences as they stand, to tell which blocked calls a sentence should already have
    allowed (SPEC PERM-11). A settings file that does not read leaves that out, with an error in the log, rather
    than the card or its explanation."""
    try:
        return rules(path)["auto"]
    except (ValueError, store.Invalid) as exc:
        log.error("cannot read %s for the blocked-call cards: %s", path, exc)
        return []


def check_rule(list_name: ListName, rule: str) -> str:
    """The rule as it is stored, or Invalid with what is wrong."""
    rule = rule.strip()
    if list_name == "allow":
        if len(rule) > RULE_MAX or not RULE.fullmatch(rule):
            raise store.Invalid("a rule is a tool name with an optional pattern, such as Bash(gh pr view *) or WebFetch(domain:github.com)")
        if rule in ANY_COMMAND:
            raise store.Invalid(f"{rule} allows any command; name the command, such as Bash(npm test *)")
        return rule
    if "\n" in rule or not SENTENCE_MIN <= len(rule) <= SENTENCE_MAX:
        raise store.Invalid(f"an auto-mode sentence is one line of {SENTENCE_MIN} to {SENTENCE_MAX} characters")
    if rule == DEFAULTS:
        raise store.Invalid("$defaults is not a sentence")
    return rule


def add(path: Path, list_name: ListName, rule: str) -> tuple[str, bool]:
    """Add a rule; returns it as stored and whether it was new."""
    rule = check_rule(list_name, rule)
    with _write_lock:
        settings = _read(path)
        if list_name == "allow":
            entries = settings.setdefault("permissions", {}).setdefault("allow", [])
        else:
            # A first sentence keeps the built-in ones next to it.
            entries = settings.setdefault("autoMode", {}).setdefault("allow", [DEFAULTS])
        if rule in entries:
            return rule, False
        entries.append(rule)
        _write(path, settings)
    return rule, True


def remove(path: Path, list_name: ListName, rule: str) -> None:
    with _write_lock:
        settings = _read(path)
        section = "permissions" if list_name == "allow" else "autoMode"
        entries = settings.get(section, {}).get("allow", [])
        if rule == DEFAULTS or rule not in entries:
            raise store.NotFound(f"no such rule in {section}.allow: {rule}")
        entries.remove(rule)
        _write(path, settings)


def replace(path: Path, list_name: ListName, old: str, new: str) -> tuple[str, bool]:
    """Change a rule in place, at its position in the list; returns the new rule as stored and whether the file
    changed. A change already made (`new` there, `old` gone) changes nothing, so an answer that failed after it can
    be sent again, as an add can."""
    new = check_rule(list_name, new)
    with _write_lock:
        settings = _read(path)
        section = "permissions" if list_name == "allow" else "autoMode"
        entries = settings.get(section, {}).get("allow", [])
        if new == old:
            raise store.Invalid("the changed rule is the same as the rule it replaces")
        if old not in entries and new in entries:
            return new, False
        if old == DEFAULTS or old not in entries:
            raise store.NotFound(f"no such rule in {section}.allow: {old}")
        if new in entries:
            raise store.Invalid(f"{new} is a rule already; remove the old one instead of changing it")
        entries[entries.index(old)] = new
        _write(path, settings)
    return new, True


def add_rule(conn: sqlite3.Connection, path: Path, list_name: ListName, rule: str) -> tuple[str, bool]:
    """The user adds a lasting rule; returns it as stored and whether it was new."""
    with _write_lock:
        rule, added = add(path, list_name, rule)
        if added:
            with db.transaction(conn):
                patterns.record(conn, "permission.rule_added", "user", None, None, None, {"list": list_name, "rule": rule})
    return rule, added


def remove_rule(conn: sqlite3.Connection, path: Path, list_name: ListName, rule: str) -> None:
    """The user removes a rule."""
    with _write_lock:
        remove(path, list_name, rule)
        with db.transaction(conn):
            patterns.record(conn, "permission.rule_removed", "user", None, None, None, {"list": list_name, "rule": rule})


def edit_rule(conn: sqlite3.Connection, path: Path, list_name: ListName, old: str, new: str) -> str:
    """The user changes a rule in place (SPEC PERM-11), so an almost-right rule is corrected rather than joined by
    another; returns the new rule as stored."""
    with _write_lock:
        stored, changed = replace(path, list_name, old, new)
        edit = {"list": list_name, "old": old, "rule": stored}
        if not changed and not _edited(conn, edit):
            raise store.NotFound(f"no such rule to change: {old}")
        if changed:
            with db.transaction(conn):
                patterns.record(conn, "permission.rule_edited", "user", None, None, None, edit)
    return stored


def _edited(conn: sqlite3.Connection, edit: dict[str, str]) -> bool:
    """Whether the last change the user made to either rule was this one, so an answer sent again finds its change
    made; a rule removed and another added by other means, before or after, is no such change."""
    rules = (edit["old"], edit["rule"])
    last = conn.execute(
        "SELECT kind, data FROM events WHERE kind IN ('permission.rule_added', 'permission.rule_removed',"
        " 'permission.rule_edited') AND json_extract(data, '$.list') = ?"
        " AND (json_extract(data, '$.rule') IN (?, ?) OR json_extract(data, '$.old') IN (?, ?))"
        " ORDER BY id DESC LIMIT 1",
        (edit["list"], *rules, *rules),
    ).fetchone()
    return last is not None and last["kind"] == "permission.rule_edited" and json.loads(last["data"]) == edit


# Tool calls the hooks report (SPEC PERM-1)

# A waiting hook gives up after this long and leaves the dialog to the session.
# The hook's own timeout in settings.json is longer, so the hook ends first and
# says so; a minute after the hook would have given up, Cockpit counts it as gone,
# in case it died. The card stays while the session waits at that dialog, and an
# answer then restarts the session (SPEC PERM-8).
ASK_WAIT_SECONDS = 1500
ASKED_SHOWN_SECONDS = ASK_WAIT_SECONDS + 60
# The session's job file marks it at a dialog within seconds of the hook's report, or of the hook giving up, which can
# come 25 minutes after the report (SPEC PERM-8).
DIALOG_MATCH_SECONDS = 60
# Once the user answered a dialog, the session listing (reused for sessions.CACHE_SECONDS) and the job file can show
# the session at it a while longer; a dialog still open after this long is one the answer did not reach.
ANSWER_SETTLE_SECONDS = 120
DENIED_SHOWN_HOURS = 24
# A refusal waits briefly so its explanation includes what the session did next (SPEC PERM-18).
HOLD_SECONDS = 60
# A refusal Claude has not read by then shows anyway.
HOLD_MAX_SECONDS = 4 * 60
# A dialog that closes in its session keeps a closed card briefly, so it stays visible while read (SPEC PERM-23).
SETTLED_SHOWN_SECONDS = 60
TOOL_INPUT_MAX = 100_000
SUMMARY_MAX = 2_000
# A dialog allowed by restart may repeat its call within this many minutes.
RESTART_ALLOW_MINUTES = 15
# The user works on a call's card for this long after their last press or typing on it (SPEC PERM-9).
USER_ON_CARD_MINUTES = 30
# Read rules cover the search tools, which take a directory; Edit rules cover every
# tool that writes files.
DIRECTORY_TOOLS = frozenset({"Glob", "Grep", "LS"})
EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
SUBCOMMAND = re.compile(r"[a-z][\w:.-]*")
ENV_ASSIGNMENT = re.compile(r"[A-Za-z_]\w*=.*")
# Words that set up the shell before the command that matters, and programs that only start another one.
SHELL_SETUP = frozenset({"cd", "export", "source", ".", "set", "unset"})
WRAPPERS = frozenset({"bash", "sh", "zsh", "env", "sudo", "timeout", "nohup", "time", "xargs", "exec"})
# Wrappers that run the command after them as it is: what the call does is that command. timeout's options that
# take a value come before its duration.
PASS_THROUGH = frozenset({"timeout", "nohup", "time"})
TIMEOUT_VALUE_OPTIONS = frozenset({"-s", "-k", "--signal", "--kill-after"})
# Production by name, also inside an identifier such as example-org-prod, prod_db or example_org_prod.
PRODUCTION = re.compile(r"(?<![a-z])prod(?:uction)?(?![a-z])", re.IGNORECASE)
JOB_FOLDERS = "session job folders (~/.claude/jobs)"
# How a path into a job folder reads, in every Claude account's folder (SPEC SESSION-17): ~/.claude/jobs/ and
# ~/.claude-work/jobs/. The sentences above name the default one only; the rules cover both.
JOB_FOLDER_PARTS = tuple(f"/{account.folder.name}/jobs/" for account in config.ACCOUNTS)

# What a blocked call does, for the suggested auto-mode sentences (SPEC PERM-4).
CLAUDE_TMP = f"/tmp/claude-{os.getuid()}"  # Claude Code's task output files
# The lexer joins punctuation in a row into one token, such as `);`; a token of only these ends a command.
COMMAND_BREAK_CHARS = frozenset("();|&\n")
WRITE_REDIRECTS = frozenset({">", ">>", ">|", "&>", "&>>"})
SHELL_KEYWORDS = frozenset({"if", "then", "else", "elif", "do", "while", "until", "!", "{", "}"})
LOOP_HEADS = frozenset({"for", "done", "fi", "esac", "case"})
READ_PROGRAMS = frozenset({
    "cat", "head", "tail", "grep", "egrep", "rg", "ls", "find", "wc", "jq", "cut", "sort", "uniq", "echo", "printf",
    "pgrep", "stat", "diff", "file", "tree", "date", "which", "pwd", "true", "basename", "dirname", "realpath", "du",
    "df", "ps", "tr", "column", "nl", "sleep", "readlink",
})
READ_GIT = frozenset({
    "log", "show", "diff", "status", "rev-parse", "ls-files", "blame", "grep", "describe", "shortlog", "cat-file",
    "merge-base",
})
# git commands whose first two words (or the one word, alone) make them read-only: `git worktree list` reads,
# `git worktree add` writes; a bare `git branch` lists branches, `git branch new-name` makes one.
READ_GIT_PAIRS = frozenset({
    ("worktree", "list"), ("stash", "list"), ("branch",), ("branch", "--list"), ("branch", "--show-current"),
    ("branch", "-a"), ("branch", "-r"),
})
READ_GH = frozenset({
    ("pr", "view"), ("pr", "list"), ("pr", "diff"), ("pr", "checks"), ("issue", "view"), ("issue", "list"),
    ("run", "view"), ("run", "list"), ("repo", "view"),
})
GH_WRITE_FLAGS = frozenset({"-X", "--method", "-f", "-F", "--field", "--raw-field", "--input"})
FIND_ACTIONS = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint"})
GIT_WRITES = frozenset({
    "add", "commit", "push", "apply", "checkout", "switch", "rebase", "cherry-pick", "restore", "rm", "mv", "stash",
    "reset", "merge", "pull", "am", "revert", "worktree",
})
BUILD_PROGRAMS = frozenset({"pytest", "uv", "npm", "npx", "pnpm", "yarn", "make", "ruff", "tsc", "cargo", "go"})
PROCESS_PROGRAMS = frozenset({"kill", "pkill", "killall", "systemctl"})
READ_TOOLS = DIRECTORY_TOOLS | {"Read"}
CD_TARGET = re.compile(r"(?:^|&&|;|\n)\s*cd\s+([^\s;&|]+)")
COCKPIT = re.compile(r"\bcockpit\b", re.IGNORECASE)
GITHUB = re.compile(r"\bgithub\b|\bpull requests?\b", re.IGNORECASE)
PULL_REQUEST = re.compile(r"\bPRs?\b")
# Each kind of work gets one firm, general sentence (SPEC PERM-12). READ_WORK explicitly names the credential files
# excluded from that permission.
READ_WORK = (
    "Sessions may run read-only commands anywhere in {home} and {tmp}, including session job folders, task output"
    " files and every checkout in {checkouts}: cat, sed -n, head, tail, grep, ls, find, git log/show/diff/status,"
    " gh pr view, gh api GET calls, cockpit get and list calls, and a python3 or jq one-off that only prints their"
    " output. Credential files stay out: ~/.ssh, ~/.config/gh, ~/.config/gcloud,"
    " ~/.claude/.credentials.json and .env files."
)
JOB_FOLDER_WORK = (
    f"Sessions may write, edit and delete drafts, scripts and other files in {JOB_FOLDERS}, and run scripts there"
    " that only work on files in those folders."
)
COCKPIT_WORK = (
    "Sessions may read and update Cockpit through the cockpit command, its API and scripts in their job folder:"
    " items, steps, the spec, feedback, messages to other sessions, and withdrawing their own blocked calls or"
    " proposing a sentence for them."
)
GITHUB_WORK = (
    "Sessions may work with pull requests and issues on GitHub: read them, comment,"
    " reply to and resolve review threads, push branches and open pull requests."
)
PROCESS_WORK = (
    "Sessions may stop processes that Claude Code sessions started, such as hung runs, test servers, watchers and"
    " Codex runs: kill by process id, or pkill -f with a pattern that names that process's command or folder."
)
# Connector tools get the sentence for their kind of work in that service before the sentence for the whole connector.
# The first action word in the tool's name determines its kind: mcp__claude_ai_Notion__notion-create-pages means create.
CONNECTOR_WORK = {
    "Sessions may read information in {service}.": {
        "get", "list", "search", "fetch", "read", "query", "find", "view", "download", "export",
    },
    "Sessions may add information to {service}.": {"create", "add", "append", "insert", "upload", "duplicate"},
    "Sessions may add and change information in {service}.": {"save", "upsert"},
    "Sessions may change information in {service}.": {"update", "edit", "move", "rename", "set", "mark", "assign"},
    "Sessions may delete information in {service}.": {"delete", "remove", "archive"},
    "Sessions may send messages and comments in {service}.": {"send", "post", "reply", "comment", "schedule"},
}
CONNECTOR_TOOL = re.compile(r"mcp__[A-Za-z0-9_]+?__[A-Za-z0-9_-]+")
AGENT_TOOLS = frozenset({"Agent", "Task"})
# A helper agent's own calls are checked like the session's, so this sentence allows only starting it.
HELPER_AGENTS = "Sessions may start helper agents (subagents) to do parts of their work."

# Tool rules at a few widths, from this one command up (SPEC PERM-11). A rule names a script, a program's subcommands,
# or the one command, rather than allowing a whole program everywhere as `Bash(python3 *)` would.
INTERPRETERS = frozenset({"python", "python3", "node", "deno", "bun", "ruby", "perl", "php", "lua", "tsx", "ts-node"})
# Programs whose first words name what they do (`git status`, `gh pr view`); a rule may cover each of those.
SUBCOMMAND_PROGRAMS = frozenset({
    "git", "gh", "cockpit", "npm", "pnpm", "yarn", "cargo", "go", "docker", "kubectl", "gsutil", "bq",
    "make", "pip", "uv", "codex", "alembic", "terraform", "caddy", "claude",
})
SUBCOMMAND_DEPTH = 3
# Runners that start the program named after them; the rule names that program too.
RUNNERS = (("uv", "run"), ("npx",), ("uvx",))
# Shell text after which an exact rule would match more than the one command, or not the command that runs; a
# rule is one line.
INEXACT = ("$", "*", "`", "<<", "\n")
LEADING_KEYWORDS = re.compile(r"^(?:(?:if|then|else|elif|do|while|until|!|\{)\s+)*")
LEADING_ASSIGNMENT = re.compile(r"""[A-Za-z_]\w*=(?:'[^']*'|"[^"]*"|[^\s'"])*\s""")
TRAILING_REDIRECT = re.compile(r"""(?:\s+\d*(?:>>?|>&|&>>?|<)\s*(?:'[^']*'|"[^"]*"|[^\s'"<>|&;]+))+\s*$""")

# What a call changes, for the card (SPEC PERM-3): an edit's text, or what a script the call runs says it does.
SCRIPT_SUFFIXES = frozenset({".py", ".sh", ".bash", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl"})
SCRIPT_HEAD_BYTES = 16_384
SCRIPT_FIRST_LINES = 12
CHANGE_MAX = 700
# Shells whose `-c` text is a command of its own, with its own scripts.
SHELLS = frozenset({"bash", "sh", "zsh"})
# The entry file for a command's folder, first found first: `node tasks.js export-list` runs
# src/export-list/index.ts relative to the directory containing tasks.js.
FOLDER_ENTRIES = ("index.ts", "index.js", "main.ts", "main.js", "main.py", "__main__.py")
# Files that hold a token wherever they are: a project's .npmrc as much as the home folder's.
SECRET_NAMES = (".netrc", ".git-credentials", ".npmrc", ".pypirc")
# Credential places whose files the card never shows and the explanation never reads, under home: Cockpit's own
# sign-in secrets among them, and Codex's. The side chat's copy may read none of them either (sidechat.SECRET_FILES).
SECRET_PLACES = (
    ".ssh", ".config/gh", ".config/gcloud", ".config/git/credentials", ".gnupg", ".aws", ".kube", ".docker",
    *SECRET_NAMES,
    *(str(path.relative_to(config.HOME)) for path in (
        config.TOKEN_PATH, config.LOCAL_LOGIN_PATH, config.COOKIE_KEY_PATH, config.GOOGLE_CLIENT_PATH, config.SECRETS_DIR,
        *(home / "auth.json" for home in config.CODEX_HOMES),
    )),
    *(f"{account.folder.name}/.credentials.json" for account in config.ACCOUNTS),
)
# Auto mode judges a call with the session's earlier calls: a block in the hour before can explain a stopped read.
EARLIER_BLOCK_MINUTES = 60
# Tools whose calls change nothing outside the session: they read, search or keep the session's own task list.
QUIET_TOOLS = READ_TOOLS | {
    "ToolSearch", "Skill", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "TaskOutput", "TodoWrite", "WebSearch",
    "WebFetch", "Monitor", "ScheduleWakeup",
}
# A connector's tool that only reads, by its name: get_issue, list_comments, slack_read_channel, notion-fetch.
CONNECTOR_READ = re.compile(r"(?:^|[_-])(?:get|list|search|read|fetch|query)(?:[_-]|$)")
HOLD = re.compile(
    r"\b(?:change|post|push|merge|write|commit|delete|build)\s+nothing\b|\bnothing\s+(?:changes|goes up|is posted)\b"
    r"|\bnothing in \w+ changes\b|\bmake no \w+ changes\b|\breading only\b|\bread-only work\b",
    re.IGNORECASE,
)
SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+|\n+")
SECRET_VARIABLE = re.compile(r"\$\{?((?:[A-Z][A-Z0-9_]*)?(?:PASSWORD|PASSWD|TOKEN|SECRET|API_KEY)[A-Z0-9_]*)")
SECRET_WORD = re.compile(r"token|secret|password|passwd|credential|api_key", re.IGNORECASE)
CREDENTIAL_LABELS = frozenset({"Credential Exploration", "Credential Materialization"})
# How far back a blocked call's session is searched for the earlier call auto mode judged it with (`earlier_call`).
EARLIER_CALL_MINUTES = 30
# Auto mode names the rule a blocked call broke only by a label such as "[Auto-Mode Bypass]"; the card says it
# in plain words (SPEC PERM-8, PERM-13). These are the labels it gave on the user's machine.
LABEL = re.compile(r"\[(.+)\]")
LABEL_MEANINGS = {
    "Auto-Mode Bypass": "a way around an earlier block or a guard on the session",
    "Create Unsafe Agents": "starting or stopping agents or processes outside the session's own checks",
    "Credential Exploration": "a search for passwords, tokens or keys",
    "Credential Materialization": "writing out passwords, tokens or keys where they could leak",
    "External System Writes": "a write to a system outside this machine, such as GitHub, Linear or Slack",
    "Feature Flag Writes": "a change to a feature flag",
    "Git Destructive": "a git command that can lose commits or work",
    "Instruction Poisoning": "text that plants instructions for Claude or other agents",
    "Interfere With Workloads": "a risk to other work running on this machine or elsewhere",
    "Irreversible Local Destruction": "deleting or overwriting files on this machine for good",
    "Merge Without Review": "merging a change nobody reviewed",
    "Modify Shared Resources": "a change to something other people or services share",
    "PII Data Handling": "handling personal data about people",
    "Production Deploy": "a deploy or change to production",
    "Public Data-Sharing Upload": "an upload that shares data in public",
    "Safety Bypass Flag": "a flag that turns off a safety check",
    "Security Weaken": "a change that weakens a security setting",
    "Self-Modification": "a change to Claude's own settings, permissions or instructions",
    "Session Transcript Tampering": "a change to a session's transcript",
    "Unrequested Commit in a Connected App": "a commit or post in a connected app that nobody asked for",
}
# Labels for work that auto mode lets through when the user asked for it: the user's own words to the session
# answer them. Production, credentials and destruction stay with rules and the user's decision.
REQUEST_LABELS = frozenset({
    "Auto-Mode Bypass", "External System Writes", "Feature Flag Writes", "Merge Without Review",
    "Modify Shared Resources", "Unrequested Commit in a Connected App",
})
QUOTE_MAX = 200


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def call_summary(tool_name: str, tool_input: dict[str, Any]) -> str:
    """The call in the words the user reads it: the command, or the tool and its target."""
    if tool_name == "Bash":
        text = str(tool_input.get("command", ""))
    else:
        target = next(
            (tool_input[key] for key in ("file_path", "notebook_path", "path", "url", "query", "pattern") if key in tool_input),
            None,
        )
        text = f"{tool_name} {target}" if target is not None else f"{tool_name} {json.dumps(tool_input, ensure_ascii=False)}"
    return text if len(text) <= SUMMARY_MAX else text[:SUMMARY_MAX] + f"… ({len(text)} characters)"


def _rule_path(path: Path, home: Path) -> str:
    """A path as a rule writes it: ~/ under home, // for any other absolute path."""
    if path.is_relative_to(home):
        return "~/" + str(path.relative_to(home)) if path != home else "~"
    return "/" + str(path)


def _path_rules(tool: str, path: Path | None, directory: Path, home: Path) -> list[dict[str, str]]:
    """Rules for the one file, when there is one, then for everything under its folder."""
    path = Path(_expanded(str(path), home)) if path is not None else None
    directory = Path(_expanded(str(directory), home))
    folder = _rule_path(directory, home).rstrip("/")
    rules = [{"rule": f"{tool}({_rule_path(path, home)})", "covers": "only this file"}] if path is not None else []
    return [*rules, {"rule": f"{tool}({folder}/**)", "covers": f"every file under {folder}"}]


def _raw_commands(command: str) -> list[str]:
    """The simple commands of a shell command as written, split at &&, ||, ;, |, & and line ends outside quotes."""
    parts: list[str] = []
    start, quote, index = 0, "", 0
    while index < len(command):
        char = command[index]
        if quote:
            if char == quote:
                quote = ""
            elif char == "\\" and quote == '"':
                index += 1
        elif char == "\\":
            index += 1
        elif char in "'\"":
            quote = char
        elif char in ";|&\n" and not (char == "&" and ">" in (command[index - 1 : index], command[index + 1 : index + 2])):
            parts.append(command[start:index])
            if command.startswith(("&&", "||"), index):
                index += 1
            start = index + 1
        index += 1
    parts.append(command[start:])
    return [part.strip() for part in parts if part.strip()]


def _simple_commands(command: str) -> list[tuple[str, list[str]]] | None:
    """Each command a tool rule could match, as its text without leading shell keywords and trailing redirections
    (Claude Code matches rules on that), with its words. Leading assignments stay: a rule without `FOO=1` does not
    match `FOO=1 cmd` (checked on Claude Code 2.1.295). Commands that only set up the shell are left out. None when
    a rule could not match the call's commands: a heredoc, a command substitution, or text that does not parse."""
    if any(mark in command for mark in ("<<", "$(", "`")):
        return None
    commands = []
    for raw in _raw_commands(command):
        parsed = _parts(raw)
        if parsed is None or len(parsed) > 1:
            return None
        if parsed:
            commands.append((TRAILING_REDIRECT.sub("", LEADING_KEYWORDS.sub("", raw)), parsed[0]))
    return commands


def _command_rules(text: str, words: list[str]) -> list[dict[str, str]]:
    """Tool rules for one command, narrowest first: the command itself, then the script it runs with any arguments,
    or each of its subcommand prefixes (`git push origin *`, `git push *`). A program that starts other commands
    (bash, sudo, xargs) gets none."""
    if (words[0] in WRAPPERS and words[0] not in PASS_THROUGH) or _unwrapped(words)[0] in WRAPPERS:
        return []
    exact = (
        [] if any(mark in text for mark in INEXACT)
        else [{"rule": f"Bash({_escaped(text)})", "covers": "only this exact command"}]
    )
    # A rule matches the assignments and a `timeout 900` in front of the command as written, so a wider rule would
    # have to repeat them: the exact command is the rule that fits.
    if words[0] in PASS_THROUGH or LEADING_ASSIGNMENT.match(text):
        return exact
    runner = next((runner for runner in RUNNERS if tuple(words[: len(runner)]) == runner), ())
    program, rest = (words[len(runner)], words[len(runner) + 1 :]) if len(words) > len(runner) else ("", [])
    prefix = " ".join([*runner, program])
    script = None
    if Path(program).name in INTERPRETERS and rest and not rest[0].startswith("-"):
        script, rest = f"{prefix} {rest[0]}", rest[1:]
    elif "/" in program and (program.startswith("./") or Path(program).suffix in SCRIPT_SUFFIXES):
        script = prefix
    if script is not None:
        if exact:
            exact[0]["covers"] = "only this exact command, with whatever the script holds when it runs"
        wider = [{"rule": f"Bash({_escaped(script)} *)", "covers": "this script with any arguments, whatever it holds when it runs"}]
        return exact + (wider if rest and not any(mark in script for mark in INEXACT) else [])
    if program not in SUBCOMMAND_PROGRAMS or (program == "gh" and rest[:1] == ["api"]):
        return exact
    subcommands = []
    for word in rest[:SUBCOMMAND_DEPTH]:
        if not SUBCOMMAND.fullmatch(word):
            break
        subcommands.append(word)
    return exact + [
        {"rule": f"Bash({' '.join([prefix, *subcommands[:depth]])} *)", "covers": f"every command that starts with {' '.join([prefix, *subcommands[:depth]])}"}
        for depth in range(len(subcommands), 0, -1)
    ]


def _escaped(text: str) -> str:
    """Text as Claude Code writes it between a rule's parentheses, which it reads back unescaped."""
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def suggest_rules(tool_name: str, tool_input: dict[str, Any], cwd: str, home: Path) -> dict[str, Any]:
    """Tool rules that would let this call run, narrowest first, each with what it covers, and the commands of the
    call they leave to auto mode (SPEC PERM-11). Empty when there is none to offer.

    A rule covers one command: the first one that does more than read, or the first one when all of them read.
    The user sees the whole call next to them, picks one and may edit it before adding it.
    """
    credentials = may_reach_credentials({**tool_input, "cwd": cwd}, home)
    if tool_name == "Bash":
        commands = _simple_commands(str(tool_input.get("command", ""))) or []
        main = next((command for command in commands if not _reads(command[1])), commands[0] if commands else None)
        rules = _command_rules(*main) if main is not None else []
        rules = [rule for rule in rules if len(rule["rule"]) <= RULE_MAX and rule["rule"] not in ANY_COMMAND]
        # A call that reaches for a credential gets only its exact command: a wildcard would let every other read of
        # that secret run unasked.
        if credentials:
            rules = [rule for rule in rules if "*" not in rule["rule"]]
        others = [text for text, words in commands if (text, words) != main and not _reads(words)] if rules else []
        return {"rules": rules, "others": others}
    if tool_name == "WebFetch":
        host = urlsplit(str(tool_input.get("url", ""))).hostname
        return {"rules": [{"rule": f"WebFetch(domain:{host})", "covers": f"every page on {host}"}] if host and not credentials else [], "others": []}
    if tool_name in DIRECTORY_TOOLS:
        rules = [] if credentials else _path_rules("Read", None, Path(str(tool_input.get("path") or cwd)), home)
        return {"rules": rules, "others": []}
    path = next((tool_input[key] for key in ("file_path", "notebook_path") if key in tool_input), None)
    if path is not None and tool_name in READ_TOOLS | EDIT_TOOLS:
        tool = "Read" if tool_name == "Read" else "Edit"
        rules = _path_rules(tool, Path(str(path)), Path(str(path)).parent, home)
        if credentials:
            rules = [rule for rule in rules[:1] if not any(mark in rule["rule"] for mark in "*?[")]
        return {"rules": rules, "others": []}
    return {"rules": [] if credentials else [{"rule": tool_name, "covers": "every call of this tool"}], "others": []}


def call_description(tool_input: dict[str, Any]) -> str | None:
    """What the call is for, in the session's own words: Claude Code's Bash tool takes a description (SPEC PERM-3)."""
    description = tool_input.get("description")
    return description.strip() if isinstance(description, str) and description.strip() else None


def _unwrapped(words: list[str]) -> list[str]:
    """The command that a `timeout 900`, `nohup` or `time` in front of it runs; the words as they are without one."""
    rest = words
    while rest and rest[0] in PASS_THROUGH:
        name, rest = rest[0], rest[1:]
        while rest and rest[0].startswith("-"):
            option, rest = rest[0], rest[1:]
            if name == "timeout" and option in TIMEOUT_VALUE_OPTIONS:
                rest = rest[1:]
        if name == "timeout":
            rest = rest[1:]
    return rest or words


def _program(command: str) -> str | None:
    """The program the command is about, as `git push` or `pytest`, or None when a wrapper hides it."""
    for words in _parts(command) or []:
        words = _unwrapped(words)
        if words[0] in WRAPPERS:
            return None
        name = Path(words[0]).name
        return f"{name} {words[1]}" if len(words) > 1 and SUBCOMMAND.fullmatch(words[1]) else name
    return None


def read_work(home: Path) -> str:
    """READ_WORK for this install: the checkouts folder as `~/...` when it is under home."""
    checkouts = config.setup().checkouts
    shown = f"~/{checkouts.relative_to(home)}" if checkouts.is_relative_to(home) else str(checkouts)
    return READ_WORK.format(home=home, tmp=CLAUDE_TMP, checkouts=shown)


def _repository(folder: str, home: Path) -> str:
    """The checkout a folder belongs to, as the user reads it: `~/dev/app and its worktrees`."""
    if _in_job_path(folder):
        return JOB_FOLDERS
    directory = Path(folder.split("/.claude/worktrees/")[0])
    if not directory.is_relative_to(home) or directory == home:
        return str(directory)
    checkouts = config.setup().checkouts
    if directory.is_relative_to(checkouts) and directory != checkouts:
        checkout = checkouts / directory.relative_to(checkouts).parts[0]
        shown = "~/" + str(checkout.relative_to(home))
        return f"{shown} and its worktrees"
    for candidate in (directory, *directory.parents):
        if (candidate / ".git").exists():
            return f"~/{candidate.relative_to(home)} and its worktrees"
        if candidate == home:
            break
    return "~/" + str(directory.relative_to(home))


def _parts(command: str) -> list[list[str]] | None:
    """The simple commands in a shell command (_command_words) without the ones that only set up the shell (cd,
    export) or open a loop; None when it does not parse."""
    words = _command_words(command)
    return None if words is None else [part for part in words if part[0] not in SHELL_SETUP and part[0] not in LOOP_HEADS]


def _command_words(command: str) -> list[list[str]] | None:
    """The simple commands in a shell command, each as its words, without leading variable assignments and shell
    keywords; None when it does not parse. A heredoc's body is data, so the parse stops at `<<`. A word runs up to
    a space or a shell operator, as in the shell: `$CLAUDE_JOB_DIR/tmp` and `HEAD:refs/heads/x` are one word each."""
    lexer = shlex.shlex(command.partition("<<")[0], posix=True, punctuation_chars="();<>|&\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    parts: list[list[str]] = []
    words: list[str] = []
    for token in [*tokens, ";"]:
        if not set(token) <= COMMAND_BREAK_CHARS:
            words.append(token)
            continue
        if token.startswith("(") and words and words[-1].endswith("$"):
            # `$(` opens a command substitution: in `B=$(readlink x)`, readlink is a command of its own.
            words[-1] = words[-1][:-1]
            if not words[-1]:
                words.pop()
        while words and (ENV_ASSIGNMENT.fullmatch(words[0]) or words[0] in SHELL_KEYWORDS):
            words.pop(0)
        if words:
            parts.append(words)
        words = []
    return parts


def _reads(words: list[str]) -> bool:
    """The simple command only reads: a reading program, `sed -n`, a reading git or gh subcommand, or a cockpit get
    or list, with no output written to a file."""
    for index, word in enumerate(words):
        if word in WRITE_REDIRECTS and words[index + 1 : index + 2] != ["/dev/null"]:
            return False
    words = _unwrapped(words)
    name, rest = Path(words[0]).name, words[1:]
    if name == "sed":
        return "-n" in rest and not any(word.startswith("-i") for word in rest)
    if name == "find":
        return not FIND_ACTIONS & set(rest)
    if name == "git":
        return (rest[:1] != [] and rest[0] in READ_GIT) or tuple(rest[:2]) in READ_GIT_PAIRS
    if name == "gh":
        return tuple(rest[:2]) in READ_GH or (rest[:1] == ["api"] and not GH_WRITE_FLAGS & set(rest))
    if name == "cockpit":
        return rest[:1] == ["get"] or (len(rest) > 1 and rest[1] in ("get", "list"))
    if name == "env":
        return not rest
    return name in READ_PROGRAMS


def _builds(words: list[str]) -> bool:
    """The simple command changes, builds or tests code: a git command that writes, or a build or test tool."""
    words = _unwrapped(words)
    name = Path(words[0]).name
    return (name == "git" and words[1:2] != [] and words[1] in GIT_WRITES and not _reads(words)) or name in BUILD_PROGRAMS


def _command_text(command: str) -> str:
    """The words of a shell command that say what it does. Text the command carries stays out: a heredoc's body
    and quoted prose such as a comment body or a commit message (a word with spaces in it). A `-c` script is the
    command itself, so it stays."""
    parts = _parts(command)
    if parts is None:
        return command.partition("<<")[0]
    return " ".join(
        word
        for words in parts
        for index, word in enumerate(words)
        if not any(space in word for space in " \t\n") or words[index - 1 : index] == ["-c"]
    )


def _script(words: list[str]) -> str | None:
    """The script file a simple command runs: the file after an interpreter (`python3 x.py`, `uv run python x.py`,
    `bash x.sh`), or the command itself when it is a script's path."""
    words = _unwrapped(words)
    runner = next((runner for runner in RUNNERS if tuple(words[: len(runner)]) == runner), ())
    program, after = words[len(runner) : len(runner) + 1], words[len(runner) + 1 : len(runner) + 2]
    if not program:
        return None
    if Path(program[0]).name in INTERPRETERS | {"bash", "sh", "zsh"}:
        return after[0] if after and not after[0].startswith("-") else None
    return program[0] if "/" in program[0] and Path(program[0]).suffix in SCRIPT_SUFFIXES else None


def _in_job_folder(text: str) -> bool:
    """The text names a session job folder: by its path, or by the variable Claude Code sets to it."""
    return _in_job_path(text) or "CLAUDE_JOB_DIR" in text


def _in_job_path(text: str) -> bool:
    return any(part in text for part in JOB_FOLDER_PARTS)


def _job_script(words: list[str]) -> bool:
    script = _script(words)
    return script is not None and _in_job_folder(script)


def _expanded(path: str, home: Path) -> str:
    """A path with a leading ~ or $HOME written out, and a session's $CLAUDE_JOB_DIR as a job folder, so the place
    it names shows (`_repository`)."""
    for variable in ("${CLAUDE_JOB_DIR}", "$CLAUDE_JOB_DIR"):
        if path.startswith(variable):
            return f"{home}/.claude/jobs/session{path.removeprefix(variable)}"
    for variable in ("${HOME}", "$HOME", "~"):
        if path == variable or path.startswith(f"{variable}/"):
            return f"{home}{path.removeprefix(variable)}"
    return path


def _substitutions(command: str) -> list[str]:
    """Executed `$()` and backtick bodies, including inside double quotes; single quotes and escapes stay literal."""
    found: list[str] = []

    def scan(start: int, closing: str) -> int:
        quote = ""
        index = start
        while index < len(command):
            char = command[index]
            if quote == "'":
                if char == "'":
                    quote = ""
            elif char == "\\":
                index += 1
            elif char == closing and not quote:
                return index
            elif command.startswith("$(", index) or char == "`":
                body = index + (2 if char == "$" else 1)
                end = scan(body, ")" if char == "$" else "`")
                if end < len(command):
                    found.append(command[body:end])
                index = end
            elif char in {"'", '"'} and (not quote or char == quote):
                quote = "" if quote else char
            elif char == "(" and not quote:
                index = scan(index + 1, ")")
            index += 1
        return index

    scan(0, "")
    return found


def credentials_in(tool_input: dict[str, Any], home: Path) -> list[str]:
    """What the call reaches for that holds a secret (SPEC PERM-13), as the card names it: the credential files
    and folders under home (SECRET_PLACES), credential filenames anywhere (SECRET_NAMES), and commands with
    secret-output modes; secret variables name the source when no file does."""
    command = str(tool_input.get("command", "")).partition("<<")[0]
    # A command's directory locates its arguments; a file search can use the directory itself as its target.
    text = json.dumps({key: value for key, value in tool_input.items() if key != "cwd" or not command}, ensure_ascii=False)
    cwd = _expanded(str(tool_input.get("cwd", "")), home)
    commands = _located(command, Path(cwd), home)
    # Literal arguments use each command's directory, without reading the filesystem.
    paths = [(Path(cwd), str(tool_input[key])) for key in ("file_path", "notebook_path", "path") if key in tool_input]
    paths += [(folder, word) for folder, words in commands for word in words[1:]]
    # A command with only flags can read its current directory, as `ls -la` does.
    paths += [(folder, ".") for folder, words in commands if all(word.startswith("-") for word in words[1:])]
    for folder, path in paths:
        path = _expanded(path, home)
        if folder.is_absolute() and not any(mark in path for mark in "$*?[] \t\n") and not path.startswith("-"):
            text += " " + json.dumps(os.path.normpath(folder / path))
    homes = "|".join(re.escape(prefix) for prefix in ("~", "$HOME", "${HOME}", str(home)))
    places = "|".join(re.escape(place) for place in SECRET_PLACES)
    found = [f"~/{match[1]}" for match in re.finditer(rf"(?:{homes})/((?:{places})(?:/[\w.@-]+)*)(?![\w.@-])", text)]
    if not found:
        names = "|".join(re.escape(name) for name in SECRET_NAMES)
        found = [match[0] for match in re.finditer(rf"(?<![\w.-])(?:{names})(?![\w-])", text)]
        found += [match[0] for match in re.finditer(
            r"(?<![\w.-])(?:\.env(?:\.[\w.*?-]+|\*)?|[\w*?.-]+\.(?:key|pem))(?![\w.-])", text,
        )]
    if not found:
        found = [f"${match[1]}" for match in SECRET_VARIABLE.finditer(text)]
    if not found:
        for words in _parts(command) or []:
            # Shell scripts contain commands; quoted prose stays a single word when matching printer modes.
            folder = next((place for place, part in commands if part == words), Path())
            words = _unwrapped(words)
            while len(words) > 1 and Path(words[0]).name in {"env", "sudo"}:
                wrapper, words = Path(words[0]).name, words[1:]
                while words and (words[0].startswith("-") or ENV_ASSIGNMENT.fullmatch(words[0])):
                    option, words = words[0], words[1:]
                    if wrapper == "sudo" and option in {"-u", "--user", "-g", "--group"}:
                        words = words[1:]
                words = _unwrapped(words) if words else []
            if not words:
                continue
            program = Path(words[0]).name
            if program in {"echo", "printf"}:
                continue
            if program in SHELLS:
                for index, word in enumerate(words[1:], 1):
                    if re.fullmatch(r"-[a-z]*c[a-z]*", word) and index + 1 < len(words):
                        found.extend(credentials_in({"command": words[index + 1], "cwd": str(folder)}, home))
                        break
                continue
            lowered = {word.lower() for word in words}
            if program == "printenv":
                found.extend(f"${word}" for word in words[1:] if SECRET_VARIABLE.fullmatch(f"${word}"))
            if (
                ({"auth", "token"} <= lowered)
                or bool(lowered & {"print-access-token", "print-identity-token", "get-access-token", "get-secret-value", "export-credentials"})
                or {"secrets", "versions", "access"} <= lowered
                or {"credential", "show"} <= lowered
                or {"credential", "fill"} <= lowered
                or ({"config", "view"} <= lowered and any(word.split("=", 1)[0] in {"--raw", "--flatten"} for word in lowered))
                or ({"auth", "status"} <= lowered and any(
                    word.split("=", 1)[0] == "--show-token" or re.fullmatch(r"-[a-z]*t[a-z]*", word.split("=", 1)[0])
                    for word in lowered
                ))
                or {"kv", "get"} <= lowered
            ):
                found.append(" ".join(words))
    inspected: set[str] = set()
    for raw in _raw_commands(command):
        parts = _parts(raw) or []
        folders = [place for place, words in commands if parts and words == parts[0]] or [Path()]
        for script in _substitutions(raw):
            inspected.add(script)
            for folder in folders:
                found.extend(credentials_in({"command": script, "cwd": str(folder)}, home))
    # Unquoted separators inside a substitution can span several raw commands.
    for script in _substitutions(command):
        if script not in inspected:
            found.extend(credentials_in({"command": script}, home))
    return list(dict.fromkeys(found))


def may_reach_credentials(tool_input: dict[str, Any], home: Path) -> bool:
    """Identified sources and broad credential words narrow the card's lasting choices."""
    command = str(tool_input.get("command", "")).partition("<<")[0]
    return bool(credentials_in(tool_input, home) or SECRET_WORD.search(command))


def connector_service(tool_name: str) -> str:
    """mcp__claude_ai_Google_Calendar__list_events is a tool of the Google Calendar connector."""
    return tool_name.split("__")[1].removeprefix("claude_ai_").replace("_", " ")


def connector_work(tool_name: str) -> str | None:
    """The sentence for the kind of work a connector's tool does in its service, or None when its name says no
    known kind."""
    words = re.split(r"[-_]", tool_name.split("__", 2)[2].lower())
    for word in words:
        for sentence, kinds in CONNECTOR_WORK.items():
            if word in kinds:
                return sentence.format(service=connector_service(tool_name))
    return None


def suggest_sentences(tool_name: str, tool_input: dict[str, Any], cwd: str, home: Path) -> list[str]:
    """Auto-mode sentences for the call (SPEC PERM-4), each kind of work's sentences in turn; the page starts the box
    with the first."""
    return list(dict.fromkeys(sentence for kind in work_kinds(tool_name, tool_input, cwd, home) for sentence in kind))


def work_kinds(tool_name: str, tool_input: dict[str, Any], cwd: str, home: Path) -> list[list[str]]:
    """Each kind of work the call does, as the auto-mode sentences that would each allow it, narrowest first:
    stated for wherever that kind of work happens, so one press covers the session's next calls of that kind. A
    call of no known kind gets its program in its checkout, or its connector's kind of work or the whole connector.
    The user picks a sentence and may edit it before adding it.
    """
    # The user supplies the credential scope; ordinary work sentences cover other actions on the card.
    if may_reach_credentials({**tool_input, "cwd": cwd}, home):
        return []
    # Only a Bash call's description says what the call is for; another tool's `description` is text it carries.
    description = (call_description(tool_input) or "") if tool_name == "Bash" else ""
    command = str(tool_input.get("command", "")) if tool_name == "Bash" else ""
    parts =(_parts(command) or []) if command else []
    names = {Path(_unwrapped(words)[0]).name for words in parts}
    path = str(next((tool_input[key] for key in ("file_path", "notebook_path", "path") if key in tool_input), ""))
    # A file tool works on its path, and a command that starts with `cd` works where it moves to.
    moved = CD_TARGET.search(command)
    folder = _expanded(path or (moved.group(1).strip("'\"") if moved else cwd), home)
    folder = folder if folder.startswith("/") else cwd
    place = _repository(folder, home)
    # The words the call is about: a heredoc's body is data the call carries, not what it does.
    words = f"{command.partition('<<')[0]} {description}"
    reads = tool_name in READ_TOOLS or (bool(parts) and "<<" not in command and all(_reads(part) for part in parts))
    # A job folder's script run from a checkout works on that checkout: a script that edits a checkout's files needs the
    # checkout's permission sentence rather than the job-folder sentence.
    checkout_script = place.endswith(" and its worktrees") and any(_job_script(part) for part in parts)
    kinds = [
        ((tool_name in EDIT_TOOLS and not _in_job_path(path)) or any(_builds(part) for part in parts) or checkout_script)
        and f"Sessions may change, build, test, commit and push code in {place}, on their own branches.",
        _in_job_folder(f"{command} {path}") and not reads and not checkout_script and JOB_FOLDER_WORK,
        ("cockpit" in names or COCKPIT.search(description)) and COCKPIT_WORK,
        ("gh" in names or GITHUB.search(words) or PULL_REQUEST.search(description)) and GITHUB_WORK,
        names & PROCESS_PROGRAMS and PROCESS_WORK,
        reads and folder.startswith((f"{home}/", f"{CLAUDE_TMP}/")) and read_work(home),
    ]
    # Each kind is its sentence or a falsy value (False, None, an empty set or string).
    # Connector sentences below may be None; `shown` drops those.
    found: list[list[str | None]] = [[kind] for kind in kinds if isinstance(kind, str) and kind]
    if not found:
        program = _program(command) if command else None
        if program:
            found.append([f"Sessions may run `{program}` commands in {place}."])
        elif tool_name.startswith("mcp__"):
            # Either sentence allows the call: its kind of work in the service, or the whole connector.
            found.append([connector_work(tool_name), f"Sessions may use every tool of the {connector_service(tool_name)} connector."])
        elif tool_name in AGENT_TOOLS:
            # A helper agent does the work its prompt names; the connector tools it names say which.
            named = dict.fromkeys(CONNECTOR_TOOL.findall(str(tool_input.get("prompt", ""))))
            found += [[connector_work(name), f"Sessions may use every tool of the {connector_service(name)} connector."] for name in named]
            found.append([HELPER_AGENTS])
        elif not command:
            found.append([f"Sessions may use {tool_name} in {place}."])
    shown = [
        [sentence for sentence in kind if sentence and "\n" not in sentence and SENTENCE_MIN <= len(sentence) <= SENTENCE_MAX]
        for kind in found
    ]
    return [kind for kind in shown if kind]


def get_request(conn: sqlite3.Connection, request_id: int) -> dict[str, Any]:
    request = db.row(conn.execute("SELECT * FROM permission_requests WHERE id = ?", (request_id,)).fetchone())
    if request is None:
        raise store.NotFound(f"permission request {request_id} does not exist")
    return request


def own_worktree(tool_name: str, tool_input: dict[str, Any], cwd: str, checkouts_dir: Path) -> bool:
    """The call enters an existing worktree of one of the user's checkouts: `<checkouts>/<repo>/.claude/worktrees/<name>`.

    Claude Code asks before EnterWorktree moves a session to a worktree outside its current
    repository's own `.claude/worktrees/`, as a safety check no allow rule answers, so a session
    started in ~/dev is asked for every worktree. The user chose to have Cockpit allow these
    (SPEC PERM-8). The path is resolved, symlinks and `..` included, before it is compared.
    """
    path = tool_input.get("path")
    if tool_name != "EnterWorktree" or not isinstance(path, str) or not path.strip():
        return False
    target = (Path(cwd) / Path(path).expanduser()).resolve()
    root = checkouts_dir.resolve()
    if not target.is_relative_to(root):
        return False
    parts = target.relative_to(root).parts
    return len(parts) == 4 and parts[1:3] == (".claude", "worktrees") and (root / parts[0] / ".git").exists()


def report(
    conn: sqlite3.Connection,
    actor: str,
    kind: RequestKind,
    session_id: str,
    cwd: str,
    tool_name: str,
    tool_input: dict[str, Any],
    tool_use_id: str,
    reason: str,
    asked_by: dict[str, str] | None,
    earlier: dict[str, Any] | None,
    checkouts_dir: Path,
) -> dict[str, Any]:
    """Record a call a hook reported. A denial of a call already open counts up instead. `asked_by` is the
    latest instruction the session got before a denial (sessions.last_instruction), None for a dialog;
    `earlier` the session's earlier call that auto mode most likely judged a denial with (`earlier_call`), or None.

    Some dialogs are recorded as allowed by Cockpit, and the waiting hook answers them at once: entering a
    worktree of one of the user's checkouts (`own_worktree`), and the call a session makes again after the user
    allowed it from a dialog whose hook had stopped waiting (`allowed_before_restart`).
    """
    text = json.dumps(tool_input, sort_keys=True, ensure_ascii=False)
    if len(text) > TOOL_INPUT_MAX:
        raise store.Invalid(f"tool_input is {len(text)} characters as JSON; Cockpit takes at most {TOOL_INPUT_MAX}")
    now = db.now_iso()
    why = None
    if kind == "asked" and own_worktree(tool_name, tool_input, cwd, checkouts_dir):
        why = f"a worktree of {user_name()}'s checkouts"
    elif kind == "asked" and (earlier := allowed_before_restart(conn, session_id, cwd, tool_name, text, now)) is not None:
        why = f"{user_name()} allowed it at {earlier['resolved_at'][11:16]} UTC on request {earlier['id']}, then Cockpit restarted the session"
    if why is not None:
        with db.transaction(conn):
            cursor = conn.execute(
                "INSERT INTO permission_requests (kind, state, session_id, cwd, tool_name, tool_input, tool_use_id,"
                " reason, count, created_at, last_at, resolved_at) VALUES ('asked', 'allowed', ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (session_id, cwd, tool_name, text, tool_use_id, reason, now, now, now),
            )
            patterns.record(conn, "permission.requested", actor, None, None, None, {"kind": kind, "tool": tool_name})
            patterns.record(
                conn, "permission.allowed", "cockpit", None, None, None, {"kind": kind, "tool": tool_name, "rule": None, "why": why},
            )
        return get_request(conn, cursor.lastrowid)  # ty: ignore[invalid-argument-type]
    with db.transaction(conn):
        existing = None
        # The same call in another folder is another call: its card names the folder it runs in (PERM-26).
        if kind == "denied":
            existing = conn.execute(
                "SELECT id FROM permission_requests WHERE kind = 'denied' AND state = 'open'"
                " AND session_id = ? AND cwd = ? AND tool_name = ? AND tool_input = ?",
                (session_id, cwd, tool_name, text),
            ).fetchone()
        if existing is not None:
            conn.execute(
                "UPDATE permission_requests SET count = count + 1, last_at = ?, reason = ?, tool_use_id = ?,"
                " asked_by = ?, earlier_call = ? WHERE id = ?",
                (now, reason, tool_use_id, json.dumps(asked_by), json.dumps(earlier), existing["id"]),
            )
            request_id = existing["id"]
        else:
            cursor = conn.execute(
                "INSERT INTO permission_requests (kind, state, session_id, cwd, tool_name, tool_input, tool_use_id,"
                " reason, asked_by, earlier_call, count, created_at, last_at) VALUES (?, 'open', ?, ?, ?, ?, ?, ?, ?, ?,"
                " 1, ?, ?)",
                (
                    kind, session_id, cwd, tool_name, text, tool_use_id, reason, json.dumps(asked_by),
                    json.dumps(earlier), now, now,
                ),
            )
            request_id = cursor.lastrowid
        # A repeat is recorded too: the same call refused again wakes the sweeper like the first (MSG-5).
        patterns.record(
            conn, "permission.requested", actor, None, None, None,
            {"kind": kind, "tool": tool_name, "request_id": request_id, "session_id": session_id},
        )
    return get_request(conn, request_id)  # ty: ignore[invalid-argument-type]


def label_of(reason: str) -> str | None:
    """Auto mode's label in a block's reason, such as "Credential Exploration" from "[Credential Exploration]"."""
    found = LABEL.fullmatch(reason.strip())
    return found[1] if found else None


def _kept_in_session(words: list[str]) -> list[str]:
    """A simple command's words without its redirections into session job folders, Claude Code's task output files
    or /dev/null: output written there stays with the session."""
    kept: list[str] = []
    index = 0
    while index < len(words):
        target = words[index + 1] if index + 1 < len(words) else ""
        if words[index] in WRITE_REDIRECTS and (_in_job_folder(target) or target.startswith((CLAUDE_TMP, "/dev/null"))):
            index += 2
            continue
        kept.append(words[index])
        index += 1
    return kept


def stays_local(tool_name: str, tool_input: dict[str, Any]) -> bool:
    """The call only reads, or writes only in session job folders and Claude Code's task output files: nothing that
    auto mode's labels are about. A shell command with a heredoc counts as more, since its commands after the heredoc
    do not parse; so does a call of another tool, such as Agent."""
    if tool_name in QUIET_TOOLS:
        return True
    if tool_name in EDIT_TOOLS:
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        return _in_job_folder(path) or path.startswith(CLAUDE_TMP)
    if tool_name.startswith("mcp__"):
        return bool(CONNECTOR_READ.search(tool_name.split("__")[-1]))
    command = str(tool_input.get("command", "")) if tool_name == "Bash" else ""
    parts = _parts(command) if command and "<<" not in command else None
    return bool(parts) and all(_reads(_kept_in_session(words)) for words in parts)


def earlier_call(calls: list[dict[str, Any]], label: str, home: Path) -> dict[str, Any] | None:
    """The session's earlier call that auto mode most likely judged a call it blocked under `label` with (SPEC
    PERM-13), from the session's calls before it (sessions.calls_before, newest first): under a credentials label
    the latest that reached for credentials, under any other label the latest that did more than `stays_local`.
    As the card names it: when it ran, its description and command, and the credentials it reached for.
    None when there is none."""
    for call in calls:
        found = credentials_in(call["tool_input"], home)
        judged = bool(found) if label in CREDENTIAL_LABELS else not stays_local(call["tool_name"], call["tool_input"])
        if not judged:
            continue
        return {
            "at": call["at"],
            "description": call_description(call["tool_input"]) if call["tool_name"] == "Bash" else None,
            "summary": call_summary(call["tool_name"], call["tool_input"]),
            "credentials": found,
        }
    return None


def _shown_path(path: Path, home: Path) -> str:
    return f"~/{path.relative_to(home)}" if path.is_relative_to(home) and path != home else str(path)


def _clip(text: str) -> str:
    return text if len(text) <= CHANGE_MAX else text[:CHANGE_MAX].rstrip() + "…"


def _script_words(head: str) -> str:
    """A script's own account of what it does: its docstring, its leading comment block or its leading comment
    lines, after the #! and coding lines; "" when it starts with code."""
    lines = head.splitlines()
    start = 0
    while start < len(lines) and (not lines[start].strip() or re.match(r"#!|#.*coding[:=]", lines[start])):
        start += 1
    rest = "\n".join(lines[start:])
    for opening, closing in (('"""', '"""'), ("'''", "'''"), ("/*", "*/")):
        if rest.startswith(opening):
            end = rest.find(closing, len(opening))
            block = rest[len(opening) : end if end != -1 else None]
            return "\n".join(line.strip().removeprefix("*").strip() for line in block.strip().splitlines()).strip()
    comments = []
    for line in lines[start:]:
        if not line.lstrip().startswith(("#", "//")):
            break
        comments.append(line.lstrip().lstrip("#/").strip())
    return "\n".join(comments).strip()


def showable(path: Path, home: Path) -> Path | None:
    """`path` resolved, when Cockpit may show the file and hand it to Claude: a file under home or Claude Code's
    /tmp/claude-<uid>, never one in a credential place, a .env file or a key; else None. Both the path as named and
    the file it resolves to are checked, against the credential places as named and as they resolve, so a link to a
    credential file, or a credential file that is a link, is no way round. So is a
    link inside a credential folder: a dotfiles manager's ~/.aws/credentials -> ~/dotfiles/aws.ini. So is a hard link
    to a file in a credential place, which has another name for the same file."""
    try:
        resolved = path.resolve()
        if not (resolved.is_relative_to(home) or resolved.is_relative_to(CLAUDE_TMP)):
            return None
        places = [home / place for place in SECRET_PLACES]
        contents = [place_contents(place, home) for place in places]
        places += [place.resolve() for place in places] + [target for targets, _ in contents for target in targets]
        for named in (Path(os.path.abspath(path)), resolved):
            if any(named.is_relative_to(place) for place in places) or named.name.startswith(".env") or named.name in SECRET_NAMES:
                return None
            if named.suffix in (".pem", ".key"):
                return None
        if not resolved.is_file():
            return None
        info = resolved.stat()
        return None if any((info.st_dev, info.st_ino) in files for _, files in contents) else resolved
    # Python 3.12 raises RuntimeError for a link loop, among the credential places too: the file stays closed.
    except (OSError, RuntimeError):
        return None


# Where a credential folder keeps only its command logs, over 100,000 files, which `place_contents` passes by.
LOG_FOLDERS = (".config/gcloud/logs",)


def place_contents(place: Path, home: Path) -> tuple[list[Path], set[tuple[int, int]]]:
    """What the links anywhere in a credential place resolve to (~/.aws/sso/cache/session.json among them), and the
    identity (device, inode) of each file in it, the place itself when it is a file, in the folders links lead to as
    well, each folder once, outside LOG_FOLDERS. A folder it cannot list raises, so `showable` closes every file
    rather than miss one; a missing place holds nothing."""
    # By name: a log folder linked to another place leaves that place to be walked.
    logs = {home / name for name in LOG_FOLDERS}
    passed: set[str] = set()
    targets: list[Path] = []
    files: set[tuple[int, int]] = set()
    folders = [place]
    while folders:
        top = folders.pop()
        real = os.path.realpath(top)
        if top in logs or real in passed:
            continue
        passed.add(real)
        try:
            entries = list(os.scandir(top))
        except FileNotFoundError:
            continue
        except NotADirectoryError:
            info = os.stat(top)
            files.add((info.st_dev, info.st_ino))
            continue
        for entry in entries:
            if entry.is_symlink():
                targets.append(Path(entry.path).resolve())
            if entry.is_dir():
                folders.append(Path(entry.path))
            elif entry.is_file():
                info = entry.stat()
                files.add((info.st_dev, info.st_ino))
    return targets, files


def _named(word: str, folder: Path, home: Path) -> Path:
    """The path a shell word names, read from `folder`: `~/x` under home, a relative path under `folder`."""
    if word == "~" or word.startswith("~/"):
        return home / word[2:]
    return Path(os.path.normpath(folder / word))


def call_scripts(tool_input: dict[str, Any], cwd: str, home: Path) -> list[Path]:
    """The files a shell command runs as scripts (SPEC PERM-3), each read from the folder its command runs in: the
    call's folder, or the one a `cd` earlier in the command moved to. The commands of a `bash -c` text count, and
    so does the file a script runs by the name its first argument gives (_dispatched). Each file once, in order."""
    return list(dict.fromkeys(_scripts_in(str(tool_input.get("command", "")), Path(cwd), home)))


# The options of `sudo` and `env` that take no argument and keep the folder.
WRAPPER_FLAGS = {"sudo": frozenset({"-n", "-E", "-H", "-k", "-S"}), "env": frozenset({"-i"})}


def _bare(words: list[str]) -> list[str] | None:
    """The command behind `sudo` and `env` and the wrappers _unwrapped knows; None when a wrapper's option may take
    an argument or move to another folder, as `sudo -u root` and `env --chdir=/tmp` do, or sets HOME."""
    inner = _unwrapped(words)
    while len(inner) > 1 and inner[0] in WRAPPER_FLAGS:
        flags, rest = WRAPPER_FLAGS[inner[0]], inner[1:]
        while len(rest) > 1 and (rest[0].startswith("-") or ENV_ASSIGNMENT.fullmatch(rest[0])):
            if (rest[0].startswith("-") and rest[0] not in flags) or rest[0].startswith("HOME="):
                return None
            rest = rest[1:]
        inner = _unwrapped(rest)
    return inner


def _located(command: str, folder: Path, home: Path) -> list[tuple[Path, list[str]]]:
    """The simple commands of a shell command, each with the folder it runs in: `folder`, or the one a `cd` earlier
    in the command moved to. The commands of a `bash -c` text count, also behind `sudo`; a `cd` itself is left out,
    and so is every command after a `cd` to a folder the shell names (`cd $TARGET`), as Cockpit cannot say where."""
    found: list[tuple[Path, list[str]]] = []
    for words in _command_words(command) or []:
        if words[0] == "cd":
            target = [word for word in words[1:] if word not in ("--", "-L", "-P")]
            if target and set(target[0]) & set("$`"):
                break
            folder = _named(target[0], folder, home) if target else home
            continue
        inner = _bare(words) or words
        found.append((folder, words))
        if Path(inner[0]).name in SHELLS and len(inner) > 2 and re.fullmatch(r"-[a-z]*c", inner[1]):
            found += _located(inner[2], folder, home)
            continue
    return found


def _scripts_in(command: str, folder: Path, home: Path) -> list[Path]:
    found: list[Path] = []
    for place, words in _located(command, folder, home):
        script = _script(words)
        if script is None:
            continue
        path = _named(script, place, home)
        inner = _unwrapped(words)
        found += [path, *_dispatched(path, inner[inner.index(script) + 1 :])]
    return found


def call_removals(tool_input: dict[str, Any], cwd: str, home: Path) -> list[Path]:
    """The paths an `rm` in a shell command deletes, each read from the folder its command runs in (_located), in
    order and each once; an `rm` behind `sudo` or `env` counts (_bare). Only paths the command spells out count:
    a pattern (`*.tmp`), a brace list, a word with a variable and a redirection's target are left out, and a
    command with a parenthesis (a `$( )` or a subshell, whose `cd` the parse would carry past its end), a backtick, a
    comment, a quoted or escaped `~` (`'~/x'`, `\\~/x`, `~"/x"`, `~\\/x`: a literal `~`, which the parse, dropping quotes,
    would read as home) or a new HOME names none, as the parse loses what they change; what they name is the
    shell's to say (SPEC PERM-3)."""
    command = str(tool_input.get("command", ""))
    if (
        "(" in command or "`" in command or "HOME=" in command or re.search(r"(^|\s)#", command)
        or re.search(r"['\"\\]~|~['\"\\]", command)
    ):
        return []
    found: list[Path] = []
    for place, words in _located(command, Path(cwd), home):
        inner = _bare(words)
        if inner is None or Path(inner[0]).name != "rm":
            continue
        options = True
        skip = False
        for index, word in enumerate(inner[1:], 1):
            following = inner[index + 1] if index + 1 < len(inner) else ""
            # A file descriptor sits right against its redirection, as in `2>/dev/null`; `123 > log` deletes 123.
            descriptor = word.isdigit() and following[:1] in ("<", ">") and f"{word}{following[0]}" in command
            redirect = set(word) <= set("<>&|") or descriptor
            if skip or redirect:
                # A redirection's target follows its operator.
                skip = redirect and not descriptor
            elif options and word == "--":
                options = False
            elif not (options and word.startswith("-")) and not set(word) & set("*?[{$`"):
                found.append(_named(word, place, home))
    return list(dict.fromkeys(found))


def _dispatched(script: Path, arguments: list[str]) -> list[Path]:
    """The file a script runs by the name its first argument gives: a script file, or a folder's entry file
    (FOLDER_ENTRIES) and README.md, beside the script or in its src folder. `node tasks.js export-list`
    runs src/export-list/index.ts relative to the directory containing tasks.js. [] when the argument names no such file."""
    if not arguments or arguments[0].startswith("-"):
        return []
    for base in (script.parent, script.parent / "src"):
        named = base / arguments[0]
        if named.is_dir():
            entry = next((named / name for name in FOLDER_ENTRIES if (named / name).is_file()), None)
            readme = named / "README.md"
            return [path for path in (entry, readme if readme.is_file() else None) if path is not None]
        if named.suffix in SCRIPT_SUFFIXES and named.is_file():
            return [named]
    return []


def _script_change(path: Path, home: Path, made_at: str) -> dict[str, str | None] | None:
    """What a script the call runs says it does, else its first lines; None for a file the card does not show
    (showable) or one that is not a script. The card reads only the file's start."""
    path = showable(path, home)  # ty: ignore[invalid-assignment]
    if path is None or path.suffix not in SCRIPT_SUFFIXES:
        return None
    try:
        with path.open("rb") as file:
            head = file.read(SCRIPT_HEAD_BYTES).decode(errors="replace")
        changed = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except OSError:
        return None
    said = _script_words(head)
    shown = _shown_path(path, home)
    return {
        "title": f"Runs {shown}, which says:" if said else f"Runs {shown}, which starts:",
        "text": _clip(said or "\n".join(head.splitlines()[:SCRIPT_FIRST_LINES])),
        "note": (
            f"The file changed after the call, at {changed:%H:%M} UTC."
            if changed > datetime.fromisoformat(made_at) else None
        ),
    }


def call_changes(
    tool_name: str, tool_input: dict[str, Any], cwd: str, home: Path, made_at: str
) -> list[dict[str, str | None]]:
    """What the call changes, in words the user can judge without opening the session (SPEC PERM-3): an edit's old and
    new text, or what each script file the call runs says it does. `made_at` is when the call was first made."""
    if tool_name in EDIT_TOOLS:
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        edits = tool_input.get("edits") if tool_name == "MultiEdit" else [tool_input]
        lines = [
            f"{mark} {line}"
            for edit in (edits if isinstance(edits, list) else [])
            if isinstance(edit, dict)
            for key, mark in (("old_string", "-"), ("new_string", "+"), ("content", "+"), ("new_source", "+"))
            if isinstance(edit.get(key), str)
            for line in edit[key].splitlines()
        ]
        verb = "Writes" if tool_name == "Write" else "Changes"
        return [{"title": f"{verb} {_shown_path(Path(path), home)}:", "text": _clip("\n".join(lines)), "note": None}] if lines and path else []
    if tool_name != "Bash":
        return []
    changes: list[dict[str, str | None]] = []
    for path in call_scripts(tool_input, cwd, home):
        change = _script_change(path, home, made_at)
        if change is not None and change not in changes:
            changes.append(change)
    return changes


def _covered_by(kinds: list[list[str]], allowed: list[str]) -> list[str]:
    """The allowed auto-mode sentences that already cover every kind of work the call does (SPEC PERM-11), or []
    when a kind is not covered. A kind is covered by any one of its sentences: the same sentence, or the same words
    up to its first colon, another wording of it such as the one before a rules review."""
    covering: list[str] = []
    for kind in kinds:
        heads = {sentence.split(":")[0] for sentence in kind}
        found = next((other for other in allowed if other.split(":")[0] in heads), None)
        if found is None:
            return []
        if found not in covering:
            covering.append(found)
    return covering


def _earlier_block(conn: sqlite3.Connection, request: dict[str, Any]) -> dict[str, Any] | None:
    """The request of the session's last call that auto mode blocked in the hour before this one first came."""
    since = datetime.fromisoformat(request["created_at"]) - timedelta(minutes=EARLIER_BLOCK_MINUTES)
    return db.row(
        conn.execute(
            "SELECT * FROM permission_requests WHERE session_id = ? AND kind = 'denied' AND id < ? AND created_at >= ?"
            " ORDER BY id DESC LIMIT 1",
            (request["session_id"], request["id"], _iso(since)),
        ).fetchone()
    )


def open_requests(
    conn: sqlite3.Connection, now: datetime, home: Path, auto_allowed: list[str], dialogs: dict[str, datetime]
) -> list[dict[str, Any]]:
    """Open requests the user can still act on, waiting dialogs first, each with suggestions, `held` (true for a
    refusal younger than HOLD_SECONDS, or HOLD_MAX_SECONDS while Claude has not read it), `moved_past` (true for a
    refusal the session got past without the user) (SPEC PERM-18) and `settled` (true for a dialog that closed in its
    session without the user's answer in the last SETTLED_SHOWN_SECONDS, which the page shows closed, PERM-23). `auto_allowed` is the auto-mode list as
    it stands, to tell which calls a sentence should already have allowed. `dialogs`: session UUID to when its open
    dialog came (store.open_dialogs). A dialog keeps its card while its session waits at it, after the hook stopped
    waiting too (SPEC PERM-8); `hook_ended_at` says since when."""
    asked_since = min([now - timedelta(seconds=ASKED_SHOWN_SECONDS)] + [
        opened - timedelta(seconds=ASK_WAIT_SECONDS + DIALOG_MATCH_SECONDS) for opened in dialogs.values()
    ])
    requests = db.rows(
        conn.execute(
            "SELECT * FROM permission_requests WHERE (kind = 'asked' AND state IN ('open', 'expired') AND created_at >= ?)"
            " OR (kind = 'asked' AND state = 'answered' AND resolved_at >= ?)"
            " OR (kind = 'denied' AND state = 'open' AND last_at >= ?)"
            " ORDER BY kind = 'asked' DESC, last_at DESC, id DESC",
            (
                _iso(asked_since), _iso(now - timedelta(seconds=SETTLED_SHOWN_SECONDS)),
                _iso(now - timedelta(hours=DENIED_SHOWN_HOURS)),
            ),
        ).fetchall()
    )
    return [
        request | shown(conn, request, home, auto_allowed)
        | {
            "hook_ended_at": hook_ended_at(request, now), "held": held(request, now), "moved_past": moved_past(request),
            "settled": request["state"] == "answered",
        }
        for request in requests
        if request["state"] == "answered" or on_page(conn, request, now, dialogs)
    ]


def held(request: dict[str, Any], now: datetime) -> bool:
    """A refusal waits before it shows: HOLD_SECONDS, and until Claude has read it, at most HOLD_MAX_SECONDS."""
    if request["kind"] != "denied":
        return False
    age = now - datetime.fromisoformat(request["created_at"])
    return age < timedelta(seconds=HOLD_SECONDS) or (
        request["explanation"] is None and age < timedelta(seconds=HOLD_MAX_SECONDS)
    )


def moved_past(request: dict[str, Any]) -> bool:
    """Claude read in the transcript that the session got past this refusal without it, and the session has not
    made the call again since: the refusal's count is still the one Claude read. A dialog never folds away: its
    session waits at it."""
    explanation = request["explanation"] or {}
    return (
        request["kind"] == "denied" and bool(explanation.get("moved_on")) and request["count"] == explanation["count"]
    )


def on_page(conn: sqlite3.Connection, request: dict[str, Any], now: datetime, dialogs: dict[str, datetime]) -> bool:
    """The page shows this request's card: an open refusal, or a dialog that Cockpit's hook or its session still
    waits at (SPEC PERM-8). `dialogs`: session UUID to when its open dialog came."""
    if request["kind"] == "denied":
        return request["state"] == "open"
    return hook_waits(request, now) or at_dialog(conn, request, dialogs)


def just_answered(conn: sqlite3.Connection, now: datetime, dialogs: dict[str, datetime]) -> set[str]:
    """The sessions at a dialog the user answered in the last ANSWER_SETTLE_SECONDS: the hook passes the answer on, or
    the answer restarts the session, so the dialog is about to close and the page offers no Restart for it
    (SPEC PERM-8). A dialog the hook saw close in its session counts too: the session listing can still show it for
    a while, and a Restart card would flash up (PERM-23). Only a dialog that came before the answer counts: one that
    came after it is the session's next dialog; `resolved_at` keeps whole seconds, so one in the same second counts.
    `dialogs`: session UUID to when its open dialog came."""
    answered = db.rows(
        conn.execute(
            "SELECT id, session_id, created_at, resolved_at FROM permission_requests"
            " WHERE kind = 'asked' AND state IN ('allowed', 'denied', 'answered') AND resolved_at >= ?",
            (_iso(now - timedelta(seconds=ANSWER_SETTLE_SECONDS)),),
        ).fetchall()
    )
    return {
        request["session_id"]
        for request in answered
        if at_dialog(conn, request, dialogs)
        and dialogs[request["session_id"]].replace(microsecond=0) <= datetime.fromisoformat(request["resolved_at"])
    }


def hook_waits(request: dict[str, Any], now: datetime) -> bool:
    """Cockpit's hook still waits on this dialog, so it answers the dialog with the user's answer. Once it stopped
    waiting, only a restart of the session answers the dialog (SPEC PERM-8)."""
    return request["state"] == "open" and request["created_at"] >= _iso(now - timedelta(seconds=ASKED_SHOWN_SECONDS))


def hook_ended_at(request: dict[str, Any], now: datetime) -> str | None:
    """When the hook stopped waiting on a dialog's request: when it gave up, or when it would have, had it died."""
    if request["kind"] != "asked" or hook_waits(request, now):
        return None
    return request["resolved_at"] or _iso(datetime.fromisoformat(request["created_at"]) + timedelta(seconds=ASK_WAIT_SECONDS))


def at_dialog(conn: sqlite3.Connection, request: dict[str, Any], dialogs: dict[str, datetime]) -> bool:
    """The session still waits at the dialog this request reported. `dialogs`: session UUID to when its open
    dialog came: when the hook reported it, or when the hook gave up on it. A later report from the session means its
    open dialog is that one."""
    opened = dialogs.get(request["session_id"])
    if opened is None:
        return False
    since_report = opened - datetime.fromisoformat(request["created_at"])
    window = timedelta(seconds=DIALOG_MATCH_SECONDS)
    if abs(since_report) <= window:
        return True
    return abs(since_report - timedelta(seconds=ASK_WAIT_SECONDS)) <= window and conn.execute(
        "SELECT 1 FROM permission_requests WHERE kind = 'asked' AND session_id = ? AND id > ? LIMIT 1",
        (request["session_id"], request["id"]),
    ).fetchone() is None


def allowed_before_restart(
    conn: sqlite3.Connection, session_id: str, cwd: str, tool_name: str, tool_input: str, now: str
) -> dict[str, Any] | None:
    """The request of this same call, from the same folder, that the user allowed from a dialog whose hook had
    stopped waiting, when Cockpit restarted the session for it (`restart_with_answer`) at most RESTART_ALLOW_MINUTES ago and
    the session has not made the call since. The call the session makes again is then allowed at once, once."""
    since = _iso(datetime.fromisoformat(now) - timedelta(minutes=RESTART_ALLOW_MINUTES))
    return db.row(
        conn.execute(
            "SELECT * FROM permission_requests AS allowed WHERE kind = 'asked' AND state = 'allowed'"
            " AND message_id IS NOT NULL AND session_id = ? AND cwd = ? AND tool_name = ? AND tool_input = ?"
            " AND resolved_at >= ?"
            " AND NOT EXISTS (SELECT 1 FROM permission_requests AS later WHERE later.id > allowed.id"
            " AND later.session_id = allowed.session_id AND later.cwd = allowed.cwd"
            " AND later.tool_name = allowed.tool_name AND later.tool_input = allowed.tool_input)"
            " ORDER BY id DESC LIMIT 1",
            (session_id, cwd, tool_name, tool_input, since),
        ).fetchone()
    )


def restart_with_answer(
    conn: sqlite3.Connection, request: dict[str, Any], session: Session, checkouts_dir: Path
) -> dict[str, Any]:
    """The user answered a dialog its hook no longer waits on (SPEC PERM-8): queue the message that restarts the
    session with the answer, and keep it on the request. After an allow, the call the session makes again is
    allowed at once (`allowed_before_restart`)."""
    opened = request["created_at"][11:16]
    call = store.defused(call_summary(request["tool_name"], request["tool_input"]))
    answer = "allowed" if request["state"] == "allowed" else "denied"
    text = (
        f"{user_name()} {answer} the call that waited at a dialog in your terminal since {opened} UTC, from Cockpit:"
        f"\n\n{call}\n\nCockpit had stopped waiting on that dialog, so it could answer it only by restarting this session."
    )
    if answer == "allowed":
        text += " Make the call again, unchanged: Cockpit allows its dialog at once."
        if request["rule"] is not None:
            text += f" Also, {_effect(request['rule'], request['rule_list'], None)}."
    else:
        text += " Leave that call: go on without it, or say in your reply what you need it for."
    message = store.restart_message(conn, session, checkouts_dir, text)
    conn.execute("UPDATE permission_requests SET message_id = ? WHERE id = ?", (message["id"], request["id"]))
    return message


# Words in Claude's sentence that touch the user's hard rails: production and secrets stay with the user.
HARD_RAIL_WORDS = re.compile(r"credential|secret|password|token|\bkeys?\b", re.IGNORECASE)


def action_sentence(request: dict[str, Any], credentials: list[str], auto_allowed: list[str]) -> str | None:
    """Claude's sentence for an auto-mode block (explain.py), when the card may offer it (SPEC PERM-13). The prompt
    asks Claude to leave it out for production writes and credentials, and Cockpit holds to that itself, since the
    sentence comes from a model that read the call: none for a call that names production or reaches for credentials,
    or a sentence that names either. None too for a sentence already allowed, which auto mode stopped the call under."""
    sentence = str((request["explanation"] or {}).get("sentence", "")) if request["kind"] == "denied" else ""
    call = json.dumps(request["tool_input"], ensure_ascii=False)
    if "\n" in sentence or not SENTENCE_MIN <= len(sentence) <= SENTENCE_MAX or sentence in auto_allowed:
        return None
    if credentials or PRODUCTION.search(call) or PRODUCTION.search(sentence) or HARD_RAIL_WORDS.search(sentence):
        return None
    return sentence


def shown(conn: sqlite3.Connection, request: dict[str, Any], home: Path, auto_allowed: list[str]) -> dict[str, Any]:
    """What the page shows next to an open request, and Claude reads to explain it (explain.py); the session's own
    sentence comes first."""
    tool_name, tool_input, cwd = request["tool_name"], request["tool_input"], request["cwd"]
    credentials = credentials_in({**tool_input, "cwd": cwd}, home)
    sensitive = may_reach_credentials({**tool_input, "cwd": cwd}, home)
    proposed = None if sensitive else request["proposed_sentence"]
    first = [proposed] if proposed is not None else []
    kinds = work_kinds(tool_name, tool_input, cwd, home)
    suggested = suggest_sentences(tool_name, tool_input, cwd, home)
    # No sentence on a call's other work allows the credentials it reaches for (SPEC PERM-13).
    action = None if sensitive else action_sentence(request, credentials, auto_allowed)
    if action is not None:
        # Claude's sentence for the blocked action itself leads the kinds of work (SPEC PERM-13). The sentences already
        # allowed drop out: auto mode stopped the call under them, and the card names them as covering.
        suggested = [action] + [sentence for sentence in suggested if sentence not in auto_allowed and sentence != action]
    rules = suggest_rules(tool_name, tool_input, cwd, home)
    covered_by = [] if sensitive else _covered_by(kinds, auto_allowed)
    cause = block_cause(conn, request, credentials, bool(covered_by), rules)
    return {
        "summary": call_summary(tool_name, tool_input),
        "description": call_description(tool_input),
        "changes": call_changes(tool_name, tool_input, cwd, home, request["created_at"]),
        "suggested_rules": rules["rules"],
        "other_commands": rules["others"],
        "suggested_sentences": first + [sentence for sentence in suggested if sentence != proposed],
        "action_sentence": action,
        "covered_by": covered_by,
        "cause": cause,
        # A blocked call gets its cause's draft, or a direct request for the call (SPEC PERM-13).
        "ask_draft": (cause and cause["draft"]) or ask_for_call(request) if request["kind"] == "denied" else None,
    }


def ask_for_call(request: dict[str, Any]) -> str:
    """The message in the user's own words that asks a blocked call's session to make the call (SPEC PERM-13). The call
    is an agent's text the user's message quotes, so a sender line in it is marked (`store.defused`)."""
    summary = store.defused(call_summary(request["tool_name"], request["tool_input"]))
    return f"I want this done: go ahead with this call, which auto mode blocked at {request['last_at'][11:16]} UTC:\n\n```\n{summary}\n```"


def author_words(author: str) -> str:
    """Who gave an instruction (sessions.last_instruction), as the card names them."""
    if author.startswith("agent:"):
        return f"agent {author.removeprefix('agent:')}"
    return {"cockpit": "Cockpit itself", "session": "another Claude session"}.get(author, user_name())


def _quoted(text: str) -> str:
    return f"“{_clipped(text)}”"


def block_cause(
    conn: sqlite3.Connection, request: dict[str, Any], credentials: list[str], covered: bool, rules: dict[str, Any]
) -> dict[str, str | None] | None:
    """Why auto mode stopped a call, in plain words (SPEC PERM-13): `text` says what its label means, read
    against what the session was doing; `draft` is the message the user can send the session in their own words
    when Cockpit words one for this cause (the work someone else asked for, under REQUEST_LABELS), else None, and
    the card's Send as you then asks for the call itself (`ask_for_call`); `help`, for a call an
    auto-mode sentence already allows (`covered`), what changes which button the user should press: an earlier block
    to answer first, an instruction to hold off, or that no tool rule of `rules` (suggest_rules) fits; else None.
    The rule editor shows the tool rule that would help and the commands it leaves to auto mode, and Claude's
    reading of the call (explain.py) says the cause, so the card shows `text` only until that reading is in.
    None for a dialog and for a block without a label. A covered call gets no draft of its own unless someone
    else's message explains the block.

    A credentials block (CREDENTIAL_LABELS) names the `credentials` the call reaches for (credentials_in), or
    else the session's earlier call that did (`earlier_call`), which auto mode judged this one with. For another
    covered call, `_covered_cause` guesses what in the session made auto mode stop it.

    The session's latest instruction can explain a block: work another agent asked for is work the user did not
    ask for, as auto mode reads it, and a test run toward it is stopped with the commit it leads to.
    """
    label = label_of(request["reason"])
    if request["kind"] != "denied" or label is None:
        return None
    meaning = LABEL_MEANINGS.get(label)
    stopped = f"Auto mode stopped it as {meaning} (“{label}”)." if meaning else f"Auto mode stopped it under “{label}”."
    summary = store.defused(call_summary(request["tool_name"], request["tool_input"]))
    blocked_at = request["last_at"][11:16]
    call_draft = ask_for_call(request)
    advice = _rule_help(rules) if covered else None
    if label in CREDENTIAL_LABELS:
        return {"text": f"{stopped} {_credentials_cause(request, credentials)}", "draft": None, "help": advice}
    asked = request["asked_by"]
    not_yours, own_draft = _not_yours(asked, call_draft, summary, blocked_at) if label in REQUEST_LABELS else ("", None)
    if covered:
        guessed = _covered_cause(conn, request, label, stopped, not_yours, own_draft, call_draft, _rule_help(rules))
        if guessed is not None:
            return guessed
    if label not in REQUEST_LABELS:
        judged = (
            "Cockpit found no earlier block of the session and no instruction to hold off, so auto mode most likely"
            " judged the call itself that way."
            if covered else
            "It judges a call together with what the session did before it, so a harmless command can be stopped too."
        )
        return {"text": f"{stopped} {judged}", "draft": None, "help": advice}
    if asked is None:
        return {
            "text": f"{stopped} Cockpit found no instruction to the session before it.",
            "draft": None if covered else call_draft,
            "help": advice,
        }
    if asked["author"] == "user":
        return {
            "text": f"{stopped} Your latest message to the session, at {asked['at'][11:16]} UTC, did not ask for it as"
            f" auto mode read it: {_quoted(asked['text'])}",
            "draft": None if covered else call_draft,
            "help": advice,
        }
    if asked["author"] == "cockpit":
        return {
            "text": (
                f"{stopped} The session's latest message before it came from Cockpit itself at {asked['at'][11:16]} UTC,"
                f" not from you: {_quoted(asked['text'])} Auto mode counts only your own words as a request, and a"
                " message Cockpit sends for you does not count as yours."
            ),
            "draft": own_draft,
            "help": advice,
        }
    return {
        "text": (
            f"{stopped} The session's latest instruction came from {author_words(asked['author'])} at"
            f" {asked['at'][11:16]} UTC, not from you: {_quoted(asked['text'])} Auto mode counts only your own words"
            " as a request, so it treats the work that message asked for as unrequested, and this call as part of it."
        ),
        "draft": own_draft,
        "help": advice,
    }


def _not_yours(
    asked: dict[str, str] | None, call_draft: str, summary: str, blocked_at: str
) -> tuple[str, str | None]:
    """For a block under REQUEST_LABELS, whose work auto mode wants the user to ask for: the sentence saying the
    session's latest instruction came from someone else, and the message in the user's own words that asks for the
    work. ("", None) when the instruction is the user's or there is none."""
    if asked is None or asked["author"] == "user":
        return "", None
    at = asked["at"][11:16]
    if asked["author"] == "cockpit":
        return (
            f" The session's latest message before it came from Cockpit itself at {at} UTC, not from you, and auto mode"
            " counts only your own words as a request.",
            call_draft,
        )
    who = author_words(asked["author"])
    return (
        f" The session's latest instruction came from {who} at {at} UTC, not from you, and auto mode counts only"
        " your own words as a request.",
        f"I ask for this myself: go ahead with the work {who} asked you for at {at} UTC, including this call, which"
        f" auto mode blocked at {blocked_at} UTC:\n\n```\n{summary}\n```",
    )


def _covered_cause(
    conn: sqlite3.Connection,
    request: dict[str, Any],
    label: str,
    stopped: str,
    not_yours: str,
    own_draft: str | None,
    call_draft: str,
    advice: str | None,
) -> dict[str, str | None] | None:
    """What most likely made auto mode stop a call an auto-mode sentence already allows (SPEC PERM-13). Auto mode
    judges a call together with the session's calls before it and says only a label, so this is a guess, in this
    order: the session's block in the hour before (`_earlier_block`); a sentence of the session's latest
    instruction that tells it to hold off (`_hold`), for a call that does more than `stays_local`; for a call that
    does not, the session's latest earlier call that did (`earlier_call`). None when none of them applies.
"""
    asked = request["asked_by"]
    block = _earlier_block(conn, request)
    if block is not None:
        gap = _gap(datetime.fromisoformat(request["created_at"]) - datetime.fromisoformat(block["created_at"]))
        block_label = label_of(block["reason"])
        how = (
            " under the same label" if block_label == label
            else f" as {LABEL_MEANINGS[block_label]}" if block_label in LABEL_MEANINGS
            else f" under “{block_label}”" if block_label is not None
            else ""
        )
        hold = None if stays_local(block["tool_name"], block["tool_input"]) else _hold(block["asked_by"])
        against = (
            f" That call most likely went against {_instruction_words(block['asked_by'])}: “{hold}”" if hold is not None
            else not_yours
        )
        first = f"Request {block['id']} is still open: answer it first, and this call may then go through."
        return {
            "text": (
                f"{stopped} It judges a call together with the session's calls before it, and most likely stopped this"
                f" one for the call it blocked {gap} earlier{how} (request {block['id']}):"
                f" `{_clipped(call_summary(block['tool_name'], block['tool_input']))}`.{against}"
            ),
            "draft": own_draft,
            "help": " ".join(part for part in (first if block["state"] == "open" else None, advice) if part) or None,
        }
    local = stays_local(request["tool_name"], request["tool_input"])
    hold = None if local else _hold(asked)
    if hold is not None:
        return {
            "text": (
                f"{stopped} {_upper_first(_instruction_words(asked))}, says: “{hold}” Auto mode most likely stopped the"
                " call because it goes against that instruction."
            ),
            "draft": call_draft,
            "help": (
                "A tool rule would let calls like it through against that instruction too. If you want this one done,"
                " send the session your own words."
            ),
        }
    if not local:
        return None
    reads = f"{stopped} This call only reads, or writes only to the session's job folder"
    earlier = request["earlier_call"]
    if earlier is None:
        unknown = "" if not_yours else " What else auto mode judged it with is unknown."
        text = (
            f"{reads}, and Cockpit found no call of the session in the {EARLIER_CALL_MINUTES} minutes before it that did"
            f" more.{not_yours or unknown}"
        )
    else:
        text = (
            f"{reads}. Auto mode judges a call together with the session's calls before it, and"
            f" {_earlier_call_words(request, earlier, 'did more than read')}{not_yours}"
        )
    return {"text": text, "draft": own_draft, "help": advice}


def _instruction_words(asked: dict[str, str]) -> str:
    """The session's latest instruction, as a cause names it: "your latest message to the session, at 06:21 UTC"."""
    at = asked["at"][11:16]
    if asked["author"] == "user":
        return f"your latest message to the session, at {at} UTC"
    return f"the session's latest instruction, from {author_words(asked['author'])} at {at} UTC"


def _upper_first(text: str) -> str:
    return text[:1].upper() + text[1:]


def _hold(asked: dict[str, str] | None) -> str | None:
    """The sentence of an instruction that tells the session to hold off a change (HOLD), clipped, or None."""
    if asked is None:
        return None
    found = next((sentence for sentence in SENTENCE_BREAK.split(asked["text"]) if HOLD.search(sentence)), None)
    return _clipped(found) if found is not None else None


def _rule_help(rules: dict[str, Any]) -> str | None:
    """For a call an auto-mode sentence already allows (SPEC PERM-13): that no tool rule fits it, so Add rule cannot
    take calls like it past auto mode; else None. The rule that fits, and the commands of the call it leaves to
    auto mode, the card's rule editor shows itself."""
    return None if rules["rules"] else "No tool rule fits this call, so auto mode judges every call like it."


def _credentials_cause(request: dict[str, Any], credentials: list[str]) -> str:
    """What made auto mode see a credentials block (SPEC PERM-13): the credentials the call itself reaches for, or
    else the session's earlier call that did, which it judged this one with."""
    if credentials:
        return (
            f"The call itself reaches for credentials: {', '.join(credentials)}. A sentence that allows the work done"
            " with them does not allow reaching for them."
        )
    earlier = request["earlier_call"]
    if earlier is None:
        return (
            "This call reaches for no credentials, and Cockpit found no call of the session in the"
            f" {EARLIER_CALL_MINUTES} minutes before it that did. Auto mode judges a call together with what the"
            " session did before it, so a harmless command can be stopped too."
        )
    reached = f"reached for {', '.join(earlier['credentials'])}"
    return (
        "This call reaches for no credentials. Auto mode judges a call together with the session's calls before it,"
        f" and {_earlier_call_words(request, earlier, reached)}"
    )


def _earlier_call_words(request: dict[str, Any], earlier: dict[str, Any], did: str) -> str:
    """The session's earlier call that auto mode most likely judged a blocked call with (`earlier_call`), as a
    cause names it, with what it `did`."""
    gap = _gap(datetime.fromisoformat(request["last_at"]) - datetime.fromisoformat(earlier["at"]))
    what = f"“{earlier['description']}”" if earlier["description"] else f"`{_clipped(earlier['summary'])}`"
    return f"most likely stopped this one for the call at {earlier['at'][11:19]} UTC, {gap} earlier, which {did}: {what}."


def _gap(span: timedelta) -> str:
    seconds = max(0, round(span.total_seconds()))
    if seconds == 0:
        return "less than a second"  # a call made beside it in the same message
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes = round(seconds / 60)
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def _clipped(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= QUOTE_MAX else line[:QUOTE_MAX].rstrip() + "…"


def _open_request(conn: sqlite3.Connection, request_id: int) -> dict[str, Any]:
    request = get_request(conn, request_id)
    if request["state"] != "open":
        raise store.Conflict(f"permission request {request_id} is already {request['state']}")
    return request


def _answerable(conn: sqlite3.Connection, request_id: int, hook_ended: bool) -> dict[str, Any]:
    """A request the user can answer: an open one, or one its session took back; with `hook_ended`, also a dialog
    whose hook gave up while its session still waits at it (SPEC PERM-8)."""
    request = get_request(conn, request_id)
    if request["state"] not in ("open", "withdrawn", "expired") or (request["state"] == "expired" and not hook_ended):
        raise store.Conflict(f"permission request {request_id} is already {request['state']}")
    return request


def taken_back(request: dict[str, Any]) -> bool:
    return request["withdrawn_at"] is not None


def _close_taken_back(
    conn: sqlite3.Connection, request_id: int, rule: str | None, list_name: ListName | None
) -> dict[str, Any]:
    """The user answered a call its session took back: the session is told nothing, and the request
    ends withdrawn with the rule the user added, if any."""
    conn.execute(
        "UPDATE permission_requests SET state = 'withdrawn', resolved_at = coalesce(resolved_at, ?),"
        " rule = coalesce(?, rule), rule_list = coalesce(?, rule_list) WHERE id = ?",
        (db.now_iso(), rule, list_name, request_id),
    )
    return get_request(conn, request_id)


def _own_open_request(conn: sqlite3.Connection, request_id: int, session_id: str) -> dict[str, Any]:
    """The request, when the session that made the call asks. Every agent holds the same
    token, so the session check guards against a mistaken id, not against a forgery."""
    request = get_request(conn, request_id)
    if request["session_id"] != session_id:
        raise store.Forbidden(
            f"permission request {request_id} belongs to session {request['session_id']}, not {session_id}"
        )
    return _open_request(conn, request_id)


def _take_back(conn: sqlite3.Connection, actor: str, request: dict[str, Any], now: datetime) -> None:
    """Withdraw an open request, inside the caller's transaction.

    While the user works on its card (`user_on_card`), only `withdrawn_at` is set and the
    request stays open: the user's answer or `finish_withdrawals` closes it.
    """
    if not taken_back(request):
        patterns.record(
            conn, "permission.withdrawn", actor, None, None, None, {"kind": request["kind"], "tool": request["tool_name"]}
        )
    if user_on_card(request, now):
        conn.execute(
            "UPDATE permission_requests SET withdrawn_at = coalesce(withdrawn_at, ?) WHERE id = ?",
            (_iso(now), request["id"]),
        )
    else:
        conn.execute(
            "UPDATE permission_requests SET state = 'withdrawn', resolved_at = ?, withdrawn_at = coalesce(withdrawn_at, ?)"
            " WHERE id = ?",
            (_iso(now), _iso(now), request["id"]),
        )


def withdraw(conn: sqlite3.Connection, actor: str, request_id: int, session_id: str, now: datetime) -> dict[str, Any]:
    """The session takes back its own open call: it got past it another way or no longer needs it."""
    _own_open_request(conn, request_id, session_id)
    with db.transaction(conn):
        request = get_request(conn, request_id)
        if request["state"] != "open":
            raise store.Conflict(f"permission request {request_id} was answered meanwhile")
        _take_back(conn, actor, request, now)
    return get_request(conn, request_id)


def withdraw_all(conn: sqlite3.Connection, actor: str, session_id: str, now: datetime) -> list[dict[str, Any]]:
    """The session takes back every open call it made; returns them, oldest first."""
    with db.transaction(conn):
        requests = db.rows(
            conn.execute(
                "SELECT * FROM permission_requests WHERE session_id = ? AND state = 'open' ORDER BY id", (session_id,)
            ).fetchall()
        )
        for request in requests:
            _take_back(conn, actor, request, now)
    return [get_request(conn, request["id"]) for request in requests]


def user_on_card(request: dict[str, Any], now: datetime) -> bool:
    """The user pressed or typed on the call's card within USER_ON_CARD_MINUTES."""
    active = rank.parse_time(request["user_active_at"])
    return active is not None and now - active < timedelta(minutes=USER_ON_CARD_MINUTES)


def mark_user_active(conn: sqlite3.Connection, request_id: int) -> dict[str, Any]:
    """The user is working on the open request's card on the page; a withdrawal waits meanwhile."""
    with db.transaction(conn):
        _open_request(conn, request_id)
        conn.execute("UPDATE permission_requests SET user_active_at = ? WHERE id = ?", (db.now_iso(), request_id))
    return get_request(conn, request_id)


def finish_withdrawals(conn: sqlite3.Connection, now: datetime) -> list[int]:
    """Close the withdrawals that waited for the user, once the user stopped working on the card; returns their ids."""
    held = db.rows(
        conn.execute(
            "SELECT * FROM permission_requests WHERE state = 'open' AND withdrawn_at IS NOT NULL ORDER BY id"
        ).fetchall()
    )
    finished = []
    for request in held:
        if user_on_card(request, now):
            continue
        # The user's page may mark the card again meanwhile; then the withdrawal keeps waiting.
        if conn.execute(
            "UPDATE permission_requests SET state = 'withdrawn', resolved_at = ?"
            " WHERE id = ? AND state = 'open' AND user_active_at IS ?",
            (_iso(now), request["id"], request["user_active_at"]),
        ).rowcount:
            finished.append(request["id"])
    return finished


def propose(conn: sqlite3.Connection, actor: str, request_id: int, session_id: str, sentence: str) -> dict[str, Any]:
    """The session writes the auto-mode sentence that would allow its call; a later one replaces it."""
    request = _own_open_request(conn, request_id, session_id)
    sentence = check_rule("auto", sentence)
    with db.transaction(conn):
        if conn.execute(
            "UPDATE permission_requests SET proposed_sentence = ? WHERE id = ? AND state = 'open'",
            (sentence, request_id),
        ).rowcount == 0:
            raise store.Conflict(f"permission request {request_id} was answered meanwhile")
        patterns.record(
            conn, "permission.proposed", actor, None, None, None,
            {"kind": request["kind"], "tool": request["tool_name"], "sentence": sentence},
        )
    return get_request(conn, request_id)


def _effect(rule: str, list_name: ListName, replaced: str | None) -> str:
    if list_name == "allow":
        change = f"changed the tool rule `{replaced}` to `{rule}`" if replaced else f"added `{rule}` to the Claude Code tool rules"
        return f"{user_name()} {change}, so a matching call now runs without the auto-mode check"
    change = f"changed the auto-mode sentence \"{replaced}\" to \"{rule}\"" if replaced else f"added the auto-mode sentence \"{rule}\""
    return f"{user_name()} {change}, which the auto-mode check weighs from now on"


def _with_note(text: str, note: str | None) -> str:
    return text if note is None else f"{text}\n\n{user_name()}'s note with this answer: {note}"


def _retry_author(note: str | None) -> str:
    """Who a retry message comes from. With a note it carries the user's own words, so it goes as the user's message,
    which auto mode counts as the user's request (SPEC PERM-7); otherwise it comes from Cockpit itself."""
    return "cockpit" if note is None else "user"


def _check_note(request: dict[str, Any], note: str | None) -> None:
    """Refuse a note that no message would carry, before any rule changes, so the page keeps it in its box."""
    if note is None:
        return
    if request["kind"] != "denied":
        raise store.Invalid("a note goes with the retry of a call auto mode blocked; a dialog's call gets no message")
    if taken_back(request):
        raise store.Invalid("the session took this call back, so no retry goes to it to carry the note")


def _retry_text(request: dict[str, Any], rule: str, list_name: ListName, replaced: str | None) -> str:
    blocked_at = request["last_at"][11:16]
    return (
        f"The call that auto mode blocked in this session at {blocked_at} UTC is allowed now: {_effect(rule, list_name, replaced)}. Your session"
        " reloads the rules by itself. Retry the call if you still need it:\n\n"
        f"{store.defused(call_summary(request['tool_name'], request['tool_input']))}"
    )


def allow(
    conn: sqlite3.Connection,
    settings_path: Path,
    checkouts_dir: Path,
    request_id: int,
    rule: str | None,
    list_name: ListName | None,
    restart: Session | None,
    note: str | None,
    replaces: str | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The user allows a reported call; returns the request and, for a denied call, the retry message to send, with
    the user's `note` when they wrote one. With `replaces`, the rule is that rule of the list changed in place
    (SPEC PERM-11) rather than a new one beside it.

    A denied call runs again under a rule.
    An asked call runs once without a rule, or always with one. A call its session took
    back keeps the rule, and no message goes. `restart` is the session at a dialog its hook
    stopped waiting on: the answer then goes as the message that restarts it (`restart_with_answer`).
    """
    request = _answerable(conn, request_id, restart is not None)
    if (rule is None) != (list_name is None):
        raise store.Invalid("give a rule together with its list, or neither")
    if request["kind"] == "denied" and rule is None:
        raise store.Invalid("auto mode already blocked this call; it runs again under a rule")
    if replaces is not None and rule is None:
        raise store.Invalid("a rule to replace goes with the rule that replaces it")
    _check_note(request, note)
    if rule is not None and list_name is not None:
        if replaces is None:
            rule, _ = add_rule(conn, settings_path, list_name, rule)
        else:
            rule = edit_rule(conn, settings_path, list_name, replaces, rule)
    with db.transaction(conn):
        # Read here: the session can take the call back while the rule above is saved.
        if taken_back(get_request(conn, request_id)):
            return _close_taken_back(conn, request_id, rule, list_name), None
        if conn.execute(
            "UPDATE permission_requests SET state = 'allowed', resolved_at = ?, rule = ?, rule_list = ?"
            " WHERE id = ? AND state = ?",
            (db.now_iso(), rule, list_name, request_id, request["state"]),
        ).rowcount == 0:
            raise store.Conflict(f"permission request {request_id} changed meanwhile; look at it again")
        message = None
        if restart is not None:
            message = restart_with_answer(conn, get_request(conn, request_id), restart, checkouts_dir)
        elif request["kind"] == "denied":
            message = store.create_message(
                conn,
                _retry_author(note),
                _with_note(_retry_text(request, rule, list_name, replaces), note),  # ty: ignore[invalid-argument-type]
                {"session_id": request["session_id"]},
                checkouts_dir,
            )
            conn.execute("UPDATE permission_requests SET message_id = ? WHERE id = ?", (message["id"], request_id))
        patterns.record(
            conn, "permission.allowed", "user", None, None, None,
            {"kind": request["kind"], "tool": request["tool_name"], "rule": rule, "replaced": replaces},
        )
    return get_request(conn, request_id), message


def allow_step(
    conn: sqlite3.Connection, settings_path: Path, checkouts_dir: Path, step_id: int, seen: dict[str, str],
    seen_session: str,
) -> dict[str, Any]:
    """The user pressed Allow on a step (SPEC NEXT-18): add its rule, and hand the step to its session to go ahead.

    `seen` is the permission ({list, rule}) and `seen_session` the session told, as the user's page showed them;
    when either changed since, the press is refused. The checks, the rule and the hand-off happen under the settings
    lock in one transaction, so an agent's change to the step waits until the press is done . The press
    is final (NEXT-41): the step keeps `permission_allowed_at`, its card shows the rule allowed in place of the
    button, and a second press is refused. Returns the message.
    """
    with _write_lock, db.transaction(conn):
        step = store.get_step(conn, step_id)
        store.check_open_for_hand_off(step)
        wanted = step["permission"]
        if wanted is None:
            raise store.Conflict(f"step {step_id} asks for no permission")
        if wanted != seen:
            raise store.Conflict(f"step {step_id}'s permission changed since your page showed it; read it again")
        if step["permission_allowed_at"]:
            raise store.Conflict(
                f"you allowed step {step_id}'s permission at {step['permission_allowed_at'][11:16]} UTC, and Cockpit"
                " added its rule then"
            )
        session_id = store.target_session(conn, step["item_id"], step_id)
        if session_id is None:
            raise store.Conflict(f"step {step_id} has no session to tell")
        store.check_seen_target(step_id, session_id, seen_session)
        rule, _ = add_rule(conn, settings_path, wanted["list"], wanted["rule"])
        conn.execute("UPDATE steps SET permission_allowed_at = ? WHERE id = ?", (db.now_iso(), step_id))
        text = (
            f"{user_name()} pressed Allow on step {step_id}: {_effect(rule, wanted['list'], None)}. Your session reloads"
            " the rules by itself, so go ahead with the work the step asked about."
        )
        return store.hand_off(conn, step_id, "cockpit", text, {"session_id": session_id}, checkouts_dir, "allowed")


def tell(conn: sqlite3.Connection, checkouts_dir: Path, request_id: int, text: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The user writes to a blocked call's session from its card (SPEC PERM-13); returns the request and the message
    to send. The message arrives as the user's own prompt, which auto mode counts as the user's request, so it answers a
    block on work nobody asked for (`block_cause`). The request ends answered, or withdrawn when its session
    took the call back."""
    request = _answerable(conn, request_id, False)
    if request["kind"] != "denied":
        raise store.Invalid("a dialog waits for its answer in the session; allow or deny it")
    with db.transaction(conn):
        if taken_back(get_request(conn, request_id)):
            _close_taken_back(conn, request_id, None, None)
        elif conn.execute(
            "UPDATE permission_requests SET state = 'answered', resolved_at = ? WHERE id = ? AND state = 'open'",
            (db.now_iso(), request_id),
        ).rowcount == 0:
            raise store.Conflict(f"permission request {request_id} was answered meanwhile")
        message = store.create_message(conn, "user", text, {"session_id": request["session_id"]}, checkouts_dir)
        conn.execute("UPDATE permission_requests SET message_id = ? WHERE id = ?", (message["id"], request_id))
        patterns.record(
            conn, "permission.told", "user", None, None, None, {"tool": request["tool_name"], "reason": request["reason"]}
        )
    return get_request(conn, request_id), message


def close(
    conn: sqlite3.Connection, checkouts_dir: Path, request_id: int, state: Literal["denied", "dismissed"], restart: Session | None
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The user denies a waiting dialog's call, or dismisses a request without acting on it; returns the request and,
    with `restart` (as for `allow`), the message that restarts the session with the denial.

    A call its session took back ends withdrawn.
    """
    request = _answerable(conn, request_id, restart is not None)
    if state == "denied" and request["kind"] != "asked":
        raise store.Conflict("auto mode already denied this call; dismiss it instead")
    with db.transaction(conn):
        if taken_back(get_request(conn, request_id)):
            return _close_taken_back(conn, request_id, None, None), None
        if conn.execute(
            "UPDATE permission_requests SET state = ?, resolved_at = ? WHERE id = ? AND state = ?",
            (state, db.now_iso(), request_id, request["state"]),
        ).rowcount == 0:
            raise store.Conflict(f"permission request {request_id} changed meanwhile; look at it again")
        patterns.record(
            conn, f"permission.{state}", "user", None, None, None, {"kind": request["kind"], "tool": request["tool_name"]}
        )
        message = restart_with_answer(conn, get_request(conn, request_id), restart, checkouts_dir) if restart else None
    return get_request(conn, request_id), message


def settle(conn: sqlite3.Connection, request_id: int, state: Literal["answered", "expired"]) -> dict[str, Any]:
    """The waiting hook stopped: the dialog was answered in the session, or it gave up.

    The user's answer may have landed at the same moment; then the request keeps it.
    """
    request = get_request(conn, request_id)
    if request["kind"] != "asked":
        raise store.Invalid("only a waiting dialog's request settles")
    conn.execute(
        "UPDATE permission_requests SET state = ?, resolved_at = ? WHERE id = ? AND state = 'open'",
        (state, db.now_iso(), request_id),
    )
    return get_request(conn, request_id)
