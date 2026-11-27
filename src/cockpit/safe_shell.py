"""Shell commands an ACP agent (Gemini, Grok) may run without asking the user (SPEC SESSION-30).

A command qualifies when every part of it only reads or only touches Cockpit: Cockpit's own commands that Claude
sessions run without asking (standing_rules.COCKPIT_COMMANDS), git and GitHub reads, and plain programs that print
files or text. Parts may be chained with `;`, `&&`, `||` and `|`. Anything that could write a file, run another
program, expand a variable or reach a credential asks as before: `$`, backticks, `<`, `>` other than to /dev/null
or another output stream, `&`, a newline, a comment, brace expansion, an option that writes or runs (`sort -o`,
`rg --pre`, `find -exec`, `git diff --output`, also abbreviated), and any word that names or resolves to a
credential: a hidden folder or file at the top of the home folder (`~/.ssh`, `~/.gemini`, `~/.config`), /proc, a
key or env file anywhere, or the home folder or one above it, whose recursive reads would reach those. A read that
goes below a folder (`grep -r`, `diff -r`, `rg`, `git grep`) also asks when a file it would read holds a credential's
name, or when it would read more files than are worth checking. rg and git list those files themselves (`rg --files`,
`git ls-files`), since which files they read depends on ignore files, globs and options no copy keeps up with.

The command is split into words as bash splits it, keeping which characters were quoted, so a quoted `;` stays an
argument and an unquoted glob or `~` is expanded the way bash would before its paths are checked.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
from dataclasses import dataclass

from cockpit.standing_rules import COCKPIT_COMMANDS

# Characters a safe command never holds: expansions, a second line, a carriage return that hides text, and NUL.
_REFUSED_CHARS = ("$", "`", "\n", "\r", "\0")
# Names of credential files and folders, wherever they sit, spelled in a word or in the path it resolves to.
_SECRETS = (".ssh", ".env", ".netrc", ".aws", ".gnupg", ".config", ".docker", ".kube", ".local", ".gemini", ".codex",
            ".claude.json", ".credentials", "credentials", "auth.json", "oauth", "id_rsa", "id_ed25519", "id_ecdsa",
            ".pem", ".key", "hosts.yml", "cockpit.db")
_OPERATOR_CHARS = set(";&|<>()")
_JOINS = {";", "&&", "||", "|"}
_GLOB_CHARS = set("*?")
# Programs that only print what they read; their options never write a file or run another program.
_READERS = {"ls", "cat", "head", "tail", "wc", "echo", "pwd", "grep", "cut", "tr", "diff", "stat", "which", "du",
            "df", "basename", "dirname", "realpath", "true", "nl"}
_GIT_READS = {"status", "log", "show", "diff", "rev-parse", "ls-files", "blame", "grep"}
# Git options that write a file, print a file outside the repository or run another program. Git takes any
# unambiguous start of a long option, so a start of one of these is refused too.
_GIT_REFUSED = ("output", "output-directory", "open-files-in-pager", "no-index", "ext-diff", "textconv")
_GIT_BRANCH_LISTS = {"-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose", "--show-current", "--list", "-l"}
_GH_READS = {("pr", "view"), ("pr", "checks"), ("pr", "list"), ("pr", "diff"), ("issue", "view"), ("issue", "list"),
             ("run", "view"), ("run", "list"), ("repo", "view")}
_FIND_REFUSED = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls", "-L",
                 "-follow"}
_UNIQ_FLAGS = {"--count", "--repeated", "--unique", "--ignore-case", "--zero-terminated"}
# A glob matching more paths than this asks rather than checking each.
_GLOB_MAX = 1000
# A recursive read of a folder holding more entries than this asks rather than checking each.
_TREE_MAX = 5000
# grep's options whose value may be the next word, and those that give the pattern, so the first operand is a file.
_GREP_VALUED = "efmABCdD"
_GREP_VALUED_LONG = ("regexp", "file", "max-count", "after-context", "before-context", "context", "directories",
                     "devices", "include", "exclude", "exclude-dir", "exclude-from", "label", "binary-files",
                     "group-separator")
_GIT_GREP_VALUED = "efmABC"
_GIT_GREP_VALUED_LONG = ("regexp", "file", "max-count", "after-context", "before-context", "context", "max-depth",
                         "threads")
_PATTERN_LONG = ("regexp", "file")
# rg's short options whose value may be the next word, and its options that print something other than the files
# it reads, which `rg --files` would then not list.
_RG_VALUED = "efEmjgdtTABCMr"
_RG_UNLISTED = "clq"
_RG_UNLISTED_LONG = {"--count", "--count-matches", "--files-with-matches", "--files-without-match", "--json",
                     "--quiet", "--type-list"}
# Seconds to wait for rg or git to list the files a command would read.
_LIST_TIMEOUT = 10
# Source files whose names hold a credential's name, such as `gateway_oauth.py`, are code, not credentials.
_CODE_SUFFIXES = (".py", ".pyc", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs", ".rb", ".java", ".md")


@dataclass
class _Word:
    text: str  # the word as the program gets it, quotes removed
    pattern: str  # the word as a glob pattern: quoted characters escaped
    operator: bool  # an unquoted run of operator characters, such as `|` or `2>&1`'s `>&`
    glob: bool  # holds an unquoted `*`, `?` or `[`
    tilde: bool  # starts with an unquoted `~`


def safe(command: str, cwd: str, home: str) -> bool:
    """Whether `command`, run in `cwd` by a user whose home folder is `home`, only reads or only touches Cockpit, so
    it may run without asking."""
    if not command.strip() or any(char in command for char in _REFUSED_CHARS):
        return False
    words = _split(command)
    if words is None:
        return False
    words = _without_quiet_redirects(words)
    if words is None:
        return False
    parts: list[list[_Word]] = [[]]
    for word in words:
        if word.operator:
            parts.append([])
        else:
            parts[-1].append(word)
    # Every folder a part may run in: a `cd` adds its folder, and the ones before it stay, since it may fail. bash's
    # cd takes `..` off the path it is given before following links, so a folder is kept as written and as resolved.
    folders = {os.path.normpath(cwd), os.path.realpath(cwd)}
    for part in parts:
        if not part or not all(_clear(word, folder, home) for word in part for folder in folders):
            return False
        if any(_reaches_secrets(os.path.realpath(folder), home) for folder in folders):
            return False
        texts = [word.text for word in part]
        if texts[0] == "cd":
            if len(texts) != 2 or texts[1].startswith("-") or part[1].glob:
                return False
            target = _expanded(part[1], home)
            folders |= {path for folder in folders for path in (os.path.normpath(os.path.join(folder, target)),
                                                                 os.path.realpath(os.path.join(folder, target)))}
            if any(_reaches_secrets(os.path.realpath(folder), home) for folder in folders):
                return False
        # uniq writes to its second file, which a glob may expand to.
        elif (not _safe_part(texts) or (texts[0] == "uniq" and any(word.glob for word in part))
              or _reads_below(part, folders, home)):
            return False
    return True


def _reads_below(part: list[_Word], folders: set[str], home: str) -> bool:
    """Whether the part reads the files below a folder that holds a credential, or too many files to check: grep -r
    and diff read every entry of the folders they are given, rg the files `rg --files` lists, and git grep every
    tracked file of the repository."""
    program, args = part[0].text, [word.text for word in part[1:]]
    if program == "git":
        return _git_grep_reaches_secrets(args, folders)
    if program == "rg":
        return _rg_reaches_secrets(part[1:], folders, home)
    if program == "grep" and any(_short_has(arg, "rd") or _abbreviates(arg, ("recursive", "directories"))
                                 for arg in args):
        reads_cwd = not _grep_files(args, _GREP_VALUED, _GREP_VALUED_LONG)
    elif program == "diff":
        # diff given two folders reads the files they share, and with -r every one below them.
        reads_cwd = False
    else:
        return False
    tops = {os.path.realpath(folder) for folder in folders} if reads_cwd else set()
    for word in part[1:]:
        for folder in folders:
            paths = _matches(word, folder, home) or [_expanded(word, home)]
            # An option's value after `=` is a path too, as in diff's `--from-file=left`.
            paths += [path.split("=", 1)[1] for path in paths if path.startswith("-") and "=" in path]
            for path in paths:
                resolved = os.path.realpath(os.path.join(folder, path))
                if os.path.isdir(resolved):
                    tops.add(resolved)
    return any(_tree_reaches_secrets(top, home) for top in tops)


def _grep_files(args: list[str], valued: str, valued_long: tuple[str, ...]) -> list[str]:
    """The files and folders grep or git grep is given: its operands after the pattern, or all of them when -e or -f
    gives the pattern. `valued` and `valued_long` are the options whose value may be the next word."""
    operands: list[str] = []
    pattern_given = False
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            operands += args[index + 1:]
            break
        if arg.startswith("--"):
            pattern_given = pattern_given or _abbreviates(arg, _PATTERN_LONG)
            if "=" not in arg and _abbreviates(arg, valued_long):
                index += 1
        elif arg.startswith("-") and arg != "-":
            for position, letter in enumerate(arg[1:]):
                pattern_given = pattern_given or letter in "ef"
                if letter in valued:
                    # The rest of the cluster is the value; a letter at its end takes the next word.
                    if position == len(arg) - 2:
                        index += 1
                    break
        else:
            operands.append(arg)
        index += 1
    return operands if pattern_given else operands[1:]


def _tree_reaches_secrets(top: str, home: str) -> bool:
    """Whether an entry below `top` holds a name in _SECRETS, other than a source file's, or is a link that resolves
    to a credential, or `top` holds more than _TREE_MAX entries, following links to folders as diff -r does. A `.git`
    folder is left out for its size; its entries carry git's names."""
    count = 0
    for root, dirs, files in os.walk(top, followlinks=True):
        dirs[:] = [name for name in dirs if name != ".git"]
        count += len(dirs) + len(files)
        if count > _TREE_MAX:
            return True
        for name in dirs + files:
            path = os.path.join(root, name)
            if (any(secret in name for secret in _SECRETS) and not (name in files and name.endswith(_CODE_SUFFIXES))
                    or os.path.islink(path) and _reaches_secrets(os.path.realpath(path), home)):
                return True
    return False


def _rg_reaches_secrets(words: list[_Word], folders: set[str], home: str) -> bool:
    """Whether rg, given `words`, would read a file whose path holds a credential's name, or more than _TREE_MAX
    files. rg itself lists the files (`rg --files`), with the words as bash passes them and `.` added, so the list
    also covers the folder rg reads when its words name none. The pattern, read as a path, is missing and skipped."""
    if shutil.which("rg") is None:
        return True
    for folder in folders:
        if not os.path.isdir(folder):
            continue
        args = [path for word in words for path in sorted(_matches(word, folder, home)) or [_expanded(word, home)]]
        listing = _rg_listing_args(args)
        if listing is None:
            return True
        try:
            listed = subprocess.run(["rg", "--files", *listing, "."], cwd=folder, capture_output=True,
                                    stdin=subprocess.DEVNULL, timeout=_LIST_TIMEOUT, check=False)
        except subprocess.TimeoutExpired:
            return True
        errors = [line for line in listed.stderr.decode(errors="replace").splitlines() if "(os error 2)" not in line]
        if listed.returncode not in (0, 1, 2) or errors:
            return True
        if _paths_reach_secrets(listed.stdout.decode(errors="replace").replace("\0", "\n").split("\n")):
            return True
    return False


def _rg_listing_args(args: list[str]) -> list[str] | None:
    """rg's arguments without the options that make `rg --files` print something other than the files it reads
    (-c, -l, -q, --json and the like). None when the listing cannot stand for the read: such an option follows a
    long option that may take it as its value (`--glob --count`), or --path-separator hides the folders' names."""
    kept: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            return kept + args[index:]
        if arg.startswith("--path-separator"):
            return None
        previous = args[index - 1] if index else ""
        if previous.startswith("--") and "=" not in previous and previous != "--" and (
                arg in _RG_UNLISTED_LONG or arg.startswith("-") and not arg.startswith("--")
                and any(letter in _RG_UNLISTED for letter in arg[1:])):
            return None
        if arg.startswith("-") and not arg.startswith("--") and arg != "-":
            letters = ""
            for position, letter in enumerate(arg[1:]):
                if letter in _RG_VALUED:
                    # The rest of the cluster is the value; a letter at its end takes the next word.
                    letters += arg[1 + position:]
                    if position == len(arg) - 2 and index + 1 < len(args):
                        index += 1
                        kept += ["-" + letters, args[index]]
                        letters = ""
                    break
                if letter not in _RG_UNLISTED:
                    letters += letter
            if letters:
                kept.append("-" + letters)
        elif arg not in _RG_UNLISTED_LONG:
            kept.append(arg)
        index += 1
    return kept


def _paths_reach_secrets(paths: list[str]) -> bool:
    """Whether a listing of files holds more than _TREE_MAX paths, or a path whose folders or name hold a name in
    _SECRETS, other than a source file's name."""
    paths = [path for path in paths if path]
    if len(paths) > _TREE_MAX:
        return True
    for path in paths:
        *above, name = path.split("/")
        if (any(secret in each for each in above for secret in _SECRETS)
                or any(secret in name for secret in _SECRETS) and not name.endswith(_CODE_SUFFIXES)):
            return True
    return False


def _git_grep_reaches_secrets(args: list[str], folders: set[str]) -> bool:
    """Whether `git grep` may read a credential: a tracked file or folder anywhere in the repository with a name in
    _SECRETS, other than a source file's, since pathspecs such as `..` and `:/` reach past the folder it runs in.
    Untracked files, submodules and a word before `--` that names no path, which may be a revision (`HEAD~1`), ask
    rather than being listed too."""
    while len(args) >= 2 and args[0] == "-C":
        folders = {os.path.join(folder, args[1]) for folder in folders}
        args = args[2:]
    if not args or args[0] != "grep":
        return False
    args = args[1:]
    if any(_abbreviates(arg, ("untracked", "no-exclude-standard", "recurse-submodules")) for arg in args):
        return True
    # The first `--` that is no option's value (`-e --`) ends the options and revisions. One before the pattern is
    # skipped by git, which takes the next word as the pattern (`git grep -- foo HEAD~1`); that form asks.
    end, pattern_given = len(args), False
    for index, arg in enumerate(args):
        previous = args[index - 1] if index else ""
        if (previous.startswith("-") and not previous.startswith("--") and previous[-1] in _GIT_GREP_VALUED
                or "=" not in previous and _abbreviates(previous, _GIT_GREP_VALUED_LONG)):
            continue
        if arg == "--":
            if not pattern_given:
                return True
            end = index
            break
        pattern_given = pattern_given or not arg.startswith("-") or _short_has(arg, "ef") or _abbreviates(
            arg, _PATTERN_LONG)
    before = args[:end]
    for folder in folders:
        if not all(os.path.exists(os.path.join(folder, operand))
                   for operand in _grep_files(before, _GIT_GREP_VALUED, _GIT_GREP_VALUED_LONG)):
            return True
        try:
            listed = subprocess.run(["git", "-C", folder, "ls-files", "-z", "--full-name", ":/"], capture_output=True,
                                    stdin=subprocess.DEVNULL, timeout=_LIST_TIMEOUT, check=False)
        except subprocess.TimeoutExpired:
            return True
        if listed.returncode != 0 or _paths_reach_secrets(listed.stdout.decode(errors="replace").split("\0")):
            return True
    return False


def _split(command: str) -> list[_Word] | None:
    """The command's words as bash splits them; None for a quote left open, a comment, brace expansion, or a `~`
    naming another user's home."""
    words: list[_Word] = []
    text, pattern = "", ""
    started = glob_seen = brace = tilde = False
    index = 0

    plain = True  # the word so far has no quote or backslash

    def finish() -> None:
        nonlocal text, pattern, started, glob_seen, brace, tilde, plain
        if started:
            words.append(_Word(text, pattern, False, glob_seen, tilde))
        text, pattern = "", ""
        started = glob_seen = brace = tilde = False
        plain = True

    while index < len(command):
        char = command[index]
        if char in " \t":
            finish()
            index += 1
        elif char in _OPERATOR_CHARS:
            # An unquoted stream number touching a redirect is part of it, as in `2>`; `2 >` is an argument.
            stream = text if started and plain and text in {"1", "2"} and char == ">" else ""
            if stream:
                text, pattern, started = "", "", False
            finish()
            end = index
            while end < len(command) and command[end] in _OPERATOR_CHARS:
                end += 1
            words.append(_Word(stream + command[index:end], "", True, False, False))
            index = end
        elif char == "'":
            end = command.find("'", index + 1)
            if end < 0:
                return None
            quoted = command[index + 1:end]
            text, pattern, started, plain = text + quoted, pattern + glob.escape(quoted), True, False
            index = end + 1
        elif char == '"':
            plain = False
            index += 1
            while index < len(command) and command[index] != '"':
                if command[index] == "\\" and index + 1 < len(command) and command[index + 1] in '"\\':
                    index += 1
                text, pattern = text + command[index], pattern + glob.escape(command[index])
                index += 1
            if index >= len(command):
                return None
            started = True
            index += 1
        elif char == "\\":
            if index + 1 >= len(command):
                return None
            text, pattern, started = text + command[index + 1], pattern + glob.escape(command[index + 1]), True
            plain = False
            index += 2
        else:
            if not started and char == "#":
                return None
            # Bash's bracket patterns ([[:alpha:]], [^a]) match what Python's glob does not.
            if char in "{}[":
                return None
            if not started and char == "~":
                tilde = True
            glob_seen = glob_seen or char in _GLOB_CHARS
            text, pattern, started = text + char, pattern + char, True
            index += 1
    finish()
    if any(word.tilde and not re.match(r"~(/|$)", word.text) for word in words):
        return None
    return words


def _without_quiet_redirects(words: list[_Word]) -> list[_Word] | None:
    """The words without redirections that write no file (`2>&1`, `>&2`, `2>/dev/null`); None when any other
    redirection or operator is left, such as `>`, `<`, `&`, `|&` or `(`."""
    kept: list[_Word] = []
    index = 0
    while index < len(words):
        word = words[index]
        following = words[index + 1] if index + 1 < len(words) else None
        if word.operator and following is not None and not following.operator and (
                (word.text in {">&", "1>&", "2>&"} and following.text in {"1", "2"})
                or (word.text in {">", "1>", "2>"} and following.text == "/dev/null")):
            index += 2
            continue
        if word.operator and word.text not in _JOINS:
            return None
        kept.append(word)
        index += 1
    return kept


def _expanded(word: _Word, home: str) -> str:
    return home + word.text[1:] if word.tilde else word.text


def _clear(word: _Word, folder: str, home: str) -> bool:
    """Whether the word, and every path it may stand for from `folder`, stays clear of credentials."""
    if any(secret in word.text for secret in _SECRETS):
        return False
    matches = _matches(word, folder, home)
    # A match that starts with `-` would reach the program as an option no check has seen.
    if len(matches) > _GLOB_MAX or any(match.startswith("-") for match in matches):
        return False
    paths = matches or [_expanded(word, home)]
    # An option's value after `=` is a path too, as in `--file=x`.
    paths += [path.split("=", 1)[1] for path in paths if path.startswith("-") and "=" in path]
    return not any(_reaches_secrets(os.path.realpath(os.path.join(folder, path)), home) for path in paths)


def _matches(word: _Word, folder: str, home: str) -> list[str]:
    """The paths bash expands a glob word to from `folder`; none for a word that is no glob or matches nothing."""
    if not word.glob:
        return []
    pattern = glob.escape(home) + word.pattern[1:] if word.tilde else word.pattern
    return glob.glob(pattern, root_dir=folder) if not os.path.isabs(pattern) else glob.glob(pattern)


def _reaches_secrets(path: str, home: str) -> bool:
    """Whether a resolved path is a credential, or a folder whose recursive read would reach one: a hidden entry at
    the top of the home folder or anything inside it, a name in _SECRETS, or the home folder or one above it."""
    if any(secret in path for secret in _SECRETS) or path == "/proc" or path.startswith("/proc/"):
        return True
    home = os.path.realpath(home)
    if path == home or home.startswith(path.rstrip("/") + "/"):
        return True
    inside = os.path.relpath(path, home)
    return not inside.startswith("..") and inside.split(os.sep)[0].startswith(".")


def _abbreviates(arg: str, names: tuple[str, ...]) -> bool:
    """Whether `arg` is one of the long options `names`, or a start of one, which GNU programs and git accept."""
    if not arg.startswith("--") or arg == "--":
        return False
    name = arg[2:].split("=", 1)[0]
    return any(each.startswith(name) for each in names)


def _short_has(arg: str, letters: str) -> bool:
    """Whether `arg` is a cluster of short options holding any of `letters`, as `-uo` holds `o`."""
    return re.fullmatch(r"-[^-].*", arg) is not None and any(letter in arg[1:] for letter in letters)


def _safe_part(words: list[str]) -> bool:
    program, args = words[0], words[1:]
    if program == "cockpit":
        return bool(args) and (args[0] in COCKPIT_COMMANDS or args[0] in {"--help", "-h"})
    if program == "git":
        return _safe_git(args)
    if program == "gh":
        return tuple(args[:2]) in _GH_READS and not {"--web", "-w"} & set(args)
    if program in _READERS:
        # grep -R follows links out of the folder it reads.
        return not (program == "grep" and any(_short_has(arg, "R") or _abbreviates(arg, ("dereference-recursive",))
                                              for arg in args))
    if program == "jq":
        # jq's env and $ENV hold the agent's environment; a filter file or module (-f, -L, import, include) could
        # use them unseen.
        return not any(re.search(r"\b(env|import|include)\b", arg.lower()) or _short_has(arg, "fL")
                       or _abbreviates(arg, ("from-file", "library-path")) for arg in args)
    if program == "file":
        # -C writes a compiled magic file; -z and -Z run decompressors.
        return not any(_short_has(arg, "CzZ") or _abbreviates(arg, ("compile", "uncompress", "uncompress-noreport"))
                       for arg in args)
    if program == "rg":
        # --pre and --hostname-bin run another program, -z runs decompressors, -L follows links out of the folder.
        return not any(arg.startswith(("--pre", "--hostname-bin", "--search-zip", "--follow"))
                       or _short_has(arg, "zL") for arg in args)
    if program == "sort":
        return not any(_short_has(arg, "o") or _abbreviates(arg, ("output", "compress-program")) for arg in args)
    if program == "uniq":
        # A second file name is where uniq writes, so it takes one file at most.
        files = [arg for arg in args if not arg.startswith("-")]
        flags = [arg for arg in args if arg.startswith("-")]
        return len(files) <= 1 and all(re.fullmatch(r"-[cdDuiz]+", arg) or arg in _UNIQ_FLAGS for arg in flags)
    if program == "find":
        return not _FIND_REFUSED & set(args)
    if program == "sed":
        # Printing lines by number only: `sed -n 5,20p file`.
        return (len(args) >= 2 and args[0] == "-n" and re.fullmatch(r"\d+(,\d+)?p", args[1]) is not None
                and not any(arg.startswith("-") for arg in args[2:]))
    return False


def _safe_git(args: list[str]) -> bool:
    while len(args) >= 2 and args[0] == "-C":
        args = args[2:]
    if not args:
        return False
    sub, rest = args[0], args[1:]
    if sub in _GIT_READS:
        # -O opens files in another program.
        return not any(_abbreviates(arg, _GIT_REFUSED) or arg.startswith("-O") for arg in rest)
    if sub == "branch":
        return set(rest) <= _GIT_BRANCH_LISTS
    if sub == "worktree":
        return rest in (["list"], ["list", "--porcelain"])
    if sub == "remote":
        return rest in ([], ["-v"])
    return False
