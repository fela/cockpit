"""Sessions' slow commands run on a second machine, the offload host, so this machine's cores stay with the
sessions (SPEC BG-29).

A session runs a command it expects to be slow, such as a full test suite, as `cockpit-offload 'COMMAND'`. When the
command runs one of the slow programs below in a checkout the setup's [offload] names, and every other part of it
only changes folder or reads its output, it goes to the offload host. Any other command runs here, with a line
saying why.

There, `cockpit-offload` copies the checkout to the same path on the offload host, which has the same user, home
folder and tools, then runs the command with this shell's PATH and streams its output. It copies back the files the
command wrote and ends with the command's exit code. When the host cannot be reached it says so, records a page
error for the sweep, and runs the command here.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from cockpit import config, db, page_errors

WRAPPER_NAME = "cockpit-offload"
# The programs whose runs may go to the offload host: test runners, bun and Python scripts. A program that acts
# outside the checkout's files, such as git, keeps the command here, since what it does there would be lost.
HEAVY = frozenset({"pytest", "py.test", "bun", "python"})
# bun's commands that may go: those that test or build the checkout. Others, such as `bun install` (whose packages
# stay on the host) or `bun -e` (which runs code of its own), keep the command here.
BUN_COMMANDS = frozenset({"test", "run", "build"})
# What may sit beside a slow program in one command: changing folder, and reading or printing its output.
BESIDE = frozenset({
    "cd", "echo", "printf", "true", "pwd", "date", "sleep", "tail", "head", "grep", "wc", "sort", "uniq", "cut", "tr",
    "tee", "cat", "ls", "export",
})  # fmt: skip
# Programs that start the next word as the command: they are read past to the program they run. Each has the
# options that take the next word as their value, and a pattern for the options that stand alone. Any other option
# (such as env -S, which runs a command of its own) keeps the command here.
WRAPPERS = {
    "timeout": ({"-s", "--signal", "-k", "--kill-after"}, r"--preserve-status|--foreground|-v|--verbose|--signal=.+|--kill-after=.+|-[sk].+"),
    "env": ({"-u", "--unset"}, r"-i|--ignore-environment|--unset=.+|-u.+"),
    "nice": ({"-n", "--adjustment"}, r"-n.+|--adjustment=.+|-\d+"),
    "time": (set(), r"-p"),
    "stdbuf": ({"-i", "-o", "-e"}, r"-[ioe].+|--(input|output|error)=.+"),
}  # fmt: skip
# Python's options before -m or a script: those that take a value, and a pattern for those that stand alone. Any
# other (such as -c, which runs code of its own) keeps the command here.
PYTHON_OPTIONS_WITH_VALUES = {"-W", "-X"}
PYTHON_OPTIONS_ALONE = r"-[BEIOPSbdqsuv]+|-W.+|-X.+"
# The device files a command may name that are the same on both machines and hold nothing to copy back.
DEVICES = (Path("/dev/null"), Path("/dev/stdout"), Path("/dev/stderr"), Path("/dev/tty"), Path("/dev/fd"))
UV_RUN_OPTIONS_WITH_VALUES = {"--extra", "--group", "--with", "--python", "-p", "--package", "--env-file", "--index"}
# The variables the command sees there besides those it sets itself; any other it reads keeps it here.
FORWARDED = (
    "PATH", "LANG", "LC_ALL", "TERM", "TMPDIR", "CLAUDE_JOB_DIR", "PLAYWRIGHT_MODULE", "BROWSER_EXECUTABLE",
    "NO_COLOR", "FORCE_COLOR", "COLUMNS",
)  # fmt: skip
# Folders the offload host holds as this machine does: its tool installs (scripts/offload-host.sh copies them).
TOOLCHAINS = tuple(config.HOME / part for part in (".bun", ".local/bin", ".local/share/uv", ".cache/ms-playwright"))
SEPARATORS = frozenset({"&&", "||", ";", "|", "|&", "\n"})
REDIRECTS = frozenset({">", ">>", ">|", "&>", "&>>", "<", ">&"})
SHELL_PUNCTUATION = "();<>|&\n"
# The one connection every copy and run reuses; it closes after ten idle minutes.
CONTROL_DIR = config.HOME / ".cache" / "cockpit-offload"
# A connection that stops answering ends after about a minute, so no copy or run waits on it, or keeps a checkout
# locked, for longer.
SSH_OPTIONS = (
    "-o", f"ControlPath={CONTROL_DIR}/%C", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=4",
)  # fmt: skip
CONNECT_SECONDS = 15
# Copies leave out caches; a main checkout's copy leaves out its worktrees, which are copied one by one as used.
CACHES = ("--exclude=__pycache__", "--exclude=.pytest_cache")
WORKTREES = "--exclude=/.claude/worktrees/"
# What never comes back: the repository, installed packages and caches are the host's own copies.
NOT_COPIED_BACK = (".git", ".venv", "node_modules", "__pycache__", ".pytest_cache")
TAG = "[cockpit offload]"


class OffloadError(Exception):
    """The offload host could not take the command."""


@dataclass(frozen=True)
class Plan:
    """What one command needs on the offload host."""

    host: str
    # The checkouts it reads, main checkouts first, each copied whole.
    roots: tuple[Path, ...]
    # The checkouts it runs in, which stay locked while it runs.
    ran_in: tuple[Path, ...]
    # Files and folders outside the checkouts it names (under /tmp or the session's job folder): copied there when
    # they exist, and back afterwards.
    extras: tuple[Path, ...]
    # The main checkouts among the roots, whose copies leave out their worktrees.
    mains: tuple[Path, ...]


def _tokens(command: str) -> list[str] | None:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=SHELL_PUNCTUATION)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    # A comment can hide the rest of its line, and what follows on the next one, from this reading.
    return None if any(token.startswith("#") for token in tokens) else tokens


def _segments(tokens: list[str]) -> list[list[str]] | None:
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in SEPARATORS:
            segments.append([])
        elif token and all(char in SHELL_PUNCTUATION for char in token) and token not in REDIRECTS:
            return None  # a subshell, a heredoc or an operator this reading does not know
        else:
            segments[-1].append(token)
    return [segment for segment in segments if segment]


def _checkout(folder: Path) -> tuple[Path, Path] | None:
    """The checkout `folder` is in, and that checkout's main checkout."""
    try:
        result = subprocess.run(
            ["git", "-C", str(folder), "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir"],
            capture_output=True, text=True, timeout=5, check=False,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = result.stdout.split("\n")
    if result.returncode != 0 or len(lines) < 2:
        return None
    top = Path(lines[0])
    common = Path(lines[1])
    return (top, common.parent) if common.name == ".git" else None


def _expand(word: str, env: Mapping[str, str]) -> str:
    """`word` with ~ and the variables `env` holds filled in, as the shell would; others stay as $NAME."""
    word = re.sub(r"\$(?:\{([A-Za-z_]\w*)\}|([A-Za-z_]\w*))", lambda m: env.get(m[1] or m[2], m[0]), word)
    return os.path.expanduser(word)


def _inside(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


def _program(words: list[str]) -> tuple[str, list[str]] | None:
    """The program a command runs and its arguments, read past variable settings and the programs that start it."""
    while words and re.match(r"[A-Za-z_]\w*=", words[0]):
        words = words[1:]
    while words:
        name = os.path.basename(words[0])
        if name in WRAPPERS:
            with_values, alone = WRAPPERS[name]
            words = words[1:]
            while words and (words[0].startswith("-") or (name == "env" and re.match(r"[A-Za-z_]\w*=", words[0]))):
                if words[0] in with_values:
                    words = words[2:]
                elif words[0].startswith("-") and not re.fullmatch(alone, words[0]):
                    return None
                else:
                    words = words[1:]
            if name == "timeout":
                words = words[1:]  # the duration
        elif name == "uv" and words[1:2] == ["run"]:
            words = words[2:]
            while words and words[0].startswith("-"):
                if words[0] in {"--directory", "--project"} or words[0].startswith(("--directory=", "--project=")):
                    return None
                words = words[2:] if words[0] in UV_RUN_OPTIONS_WITH_VALUES else words[1:]
        elif re.fullmatch(r"python3?(\.\d+)?", name):
            words = words[1:]
            while words and words[0].startswith("-") and words[0] != "-m":
                if words[0] in PYTHON_OPTIONS_WITH_VALUES:
                    words = words[2:]
                elif re.fullmatch(PYTHON_OPTIONS_ALONE, words[0]):
                    words = words[1:]
                else:
                    return None
            if not words:
                return None
            if words[0] != "-m":
                return "python", words  # a script: the first word is its path
            if len(words) < 2:
                return None
            words = words[1:]
        else:
            return name, words[1:]
    return None


def plan(command: str, cwd: Path, env: Mapping[str, str], offload: config.Offload) -> Plan | None:
    """What `command`, run in `cwd`, needs on the offload host; None when it runs here."""
    if any(mark in command for mark in ("$(", "`", "<(", ">(")):
        return None
    tokens = _tokens(command)
    segments = _segments(tokens) if tokens is not None else None
    if not segments:
        return None
    set_here = {match.group(1) for token in tokens or [] if (match := re.match(r"([A-Za-z_]\w*)=", token))}
    read = set(re.findall(r"\$\{?([A-Za-z_]\w*)", command))
    if read - set(FORWARDED) - set_here:
        return None
    job = env.get("CLAUDE_JOB_DIR")
    allowed_extras = [Path("/tmp"), *([Path(job)] if job else [])]
    folder = cwd
    folders = [cwd]
    paths: list[Path] = []
    named: list[Path] = []  # every word, read as a path from its folder
    heavy = False
    for segment in segments:
        words: list[str] = []
        for index, token in enumerate(segment):
            if index and segment[index - 1] in REDIRECTS:
                if segment[index - 1] != ">&" or not (token.isdigit() or token == "-"):
                    target = _expand(token, env)
                    if "$" in target:
                        return None
                    paths.append(folder / target)
            elif token in REDIRECTS:
                if words and words[-1].isdigit():
                    words.pop()  # the file descriptor of `2>`
            else:
                words.append(token)
        found = _program(words)
        if found is None:
            return None
        name, args = found
        if name == "bun" and (not args or args[0] not in BUN_COMMANDS):
            return None
        if name in HEAVY:
            heavy = True
        elif name not in BESIDE:
            return None
        for word in words:
            value = word.split("=", 1)[1] if "=" in word and not word.startswith(("/", "~")) else word
            expanded = _expand(value, env)
            if "$" in expanded:
                return None
            named.append(Path(os.path.normpath(folder / expanded)))
            if expanded.startswith("/") or ".." in Path(expanded).parts:
                paths.append(folder / expanded)
        if name == "cd":
            target = _expand(args[0], env) if args else str(config.HOME)
            folder = Path(os.path.normpath(folder / target))
            folders.append(folder)
    if not heavy:
        return None
    roots: list[Path] = []
    mains: list[Path] = []
    ran_in: list[Path] = []
    repos = {repo.resolve() for repo in offload.repos}
    for place in folders:
        found_checkout = _checkout(place)
        if found_checkout is None or found_checkout[1].resolve() not in repos:
            return None
        top, main = found_checkout
        for root in (main, top):
            if root not in roots:
                roots.append(root)
        if main not in mains:
            mains.append(main)
        if top not in ran_in:
            ran_in.append(top)
    # A main checkout's copy leaves out its worktrees, so a word naming a worktree the command does not run in, such
    # as a script there, would read a stale copy or none.
    tops = [root for root in roots if root not in mains]
    for path in named:
        if any(_inside(path, main / ".claude" / "worktrees") for main in mains) and not any(
            _inside(path, top) for top in tops
        ):
            return None
    extras: list[Path] = []
    for raw in paths:
        path = Path(os.path.normpath(raw))
        if any(_inside(path, root) for root in roots) or any(_inside(path, tool) for tool in TOOLCHAINS):
            continue
        if not _inside(path, config.HOME) and not _inside(path, Path("/tmp")):
            if any(_inside(path, device) for device in DEVICES) or (path.exists() and not os.access(path, os.W_OK)):
                continue  # a system path such as /dev/null or /usr/bin/python3: the same on both machines
            return None  # a file the command may write, such as /var/tmp/x.xml or /dev/shm/x, which would not come back
        if any(_inside(root, path) for root in roots) or not any(
            _inside(path, allowed) and path != allowed for allowed in allowed_extras
        ):
            return None
        anchor = next((parent for parent in (path, *path.parents) if parent.name == "node_modules"), path)
        if os.path.realpath(anchor) != str(anchor):
            return None  # it goes through a symlink, so copying it could reach a folder the command does not name
        if anchor not in extras:
            extras.append(anchor)
    return Plan(host=offload.host, roots=tuple(roots), ran_in=tuple(ran_in), extras=tuple(extras), mains=tuple(mains))


@contextlib.contextmanager
def _lock(name: str) -> Iterator[None]:
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONTROL_DIR / f"{name}.lock", "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _connect(host: str) -> None:
    """Open the shared connection to `host`, unless it is open already."""
    with _lock("connect"):
        check = subprocess.run(
            ["ssh", *SSH_OPTIONS, "-O", "check", host], capture_output=True, timeout=CONNECT_SECONDS, check=False
        )
        if check.returncode == 0:
            return
        # The connection goes to the background, so its output goes to a file: a pipe would stay open as long as it.
        with tempfile.TemporaryFile() as errors:
            try:
                opened = subprocess.run(
                    ["ssh", *SSH_OPTIONS, "-o", "ControlMaster=yes", "-o", "ControlPersist=600",
                     "-o", f"ConnectTimeout={CONNECT_SECONDS}", "-f", "-N", host],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors, timeout=CONNECT_SECONDS * 2, check=False,
                    start_new_session=True,
                )  # fmt: skip
            except subprocess.TimeoutExpired as exc:
                raise OffloadError(f"ssh to {host} did not connect within {CONNECT_SECONDS * 2} s") from exc
            if opened.returncode != 0:
                errors.seek(0)
                raise OffloadError(f"ssh to {host} failed: {errors.read().decode(errors='replace').strip()}")


def _rsync(*args: str) -> None:
    result = subprocess.run(
        ["rsync", "-a", "--timeout=120", "-e", shlex.join(["ssh", *SSH_OPTIONS]), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise OffloadError(f"rsync {' '.join(args[-2:])} failed ({result.returncode}): {result.stderr.strip()}")


def _hold(root: Path) -> IO[str]:
    """Lock `root` against other offloaded commands that copy it; closing the returned file unlocks it."""
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(CONTROL_DIR / f"root-{hashlib.sha1(str(root).encode()).hexdigest()[:16]}.lock", "w")  # noqa: SIM115
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"{TAG} Waiting for another command that uses {root} to finish.", file=sys.stderr, flush=True)
        fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


def _push(work: Plan) -> None:
    for root in work.roots:
        skip = (WORKTREES,) if root in work.mains else ()
        _rsync("--delete", "--mkpath", *CACHES, *skip, f"{root}/", f"{work.host}:{root}/")
    for extra in work.extras:
        if extra.is_dir():
            _rsync("--delete", "--mkpath", f"{extra}/", f"{work.host}:{extra}/")
        elif extra.exists():
            _rsync("--mkpath", str(extra), f"{work.host}:{extra}")


def _changed_there(work: Plan, root: Path, marker: str) -> list[str]:
    """The files in `root` on the host that changed after `marker` was made, relative to `root`."""
    prune = [part for name in NOT_COPIED_BACK for part in ("-name", name, "-prune", "-o")]
    if root in work.mains:
        prune += ["-path", "./.claude/worktrees", "-prune", "-o"]
    # -cnewer reads the time a file last changed on disk, which a command cannot set back, as a copy that keeps
    # its source's modification time does.
    find = ["find", ".", *prune, "(", "-type", "f", "-o", "-type", "l", ")", "-cnewer", marker, "-print0"]
    listed = subprocess.run(
        ["ssh", *SSH_OPTIONS, work.host, f"cd {shlex.quote(str(root))} && {shlex.join(find)}"],
        capture_output=True, timeout=120, check=False,
    )  # fmt: skip
    if listed.returncode != 0:
        raise OffloadError(f"listing the changed files in {root} failed: {listed.stderr.decode(errors='replace')}")
    return [os.fsdecode(name) for name in listed.stdout.split(b"\0") if name]


def _pull(work: Plan, marker: str, copied_at: int) -> None:
    """Copy back the files the command changed there, keeping any that changed here meanwhile."""
    try:
        _pull_changed(work, marker, copied_at)
    finally:
        subprocess.run(
            ["ssh", *SSH_OPTIONS, work.host, f"rm -f {shlex.quote(marker)}"],
            capture_output=True,
            timeout=60,
            check=False,
        )


def _pull_changed(work: Plan, marker: str, copied_at: int) -> None:
    for root in work.roots:
        changed = _changed_there(work, root, marker)
        kept = [
            name for name in changed if os.path.lexists(root / name) and (root / name).lstat().st_mtime_ns > copied_at
        ]
        if kept:
            print(
                f"{TAG} These files changed here while the command ran there, so this machine's version stays:"
                f" {', '.join(kept)}",
                file=sys.stderr, flush=True,
            )  # fmt: skip
        wanted = [name for name in changed if name not in kept]
        if wanted:
            with tempfile.NamedTemporaryFile("wb") as listing:
                listing.write(b"\0".join(os.fsencode(name) for name in wanted))
                listing.flush()
                # Every listed file changed there, so each is copied even when its size and time look the same.
                _rsync("--from0", f"--files-from={listing.name}", "--ignore-times", f"{work.host}:{root}/", f"{root}/")
    for extra in work.extras:
        extra.parent.mkdir(parents=True, exist_ok=True)
        _rsync("--update", "--ignore-missing-args", f"{work.host}:{extra}", f"{extra.parent}/")


def _remote_script(command: str, cwd: Path, env: Mapping[str, str], work: Plan, marker: str) -> str:
    """The script the host runs: the command in its own process group, which ends when the ssh to it ends."""
    exports = " ".join(shlex.quote(f"{name}={env[name]}") for name in FORWARDED if name in env)
    parents = " ".join(
        shlex.quote(str(path)) for path in (Path(marker).parent, *(extra.parent for extra in work.extras))
    )
    # A file the command names that is gone here goes there too, so the command never reads an earlier run's copy.
    gone = " ".join(shlex.quote(str(extra)) for extra in work.extras if not os.path.lexists(extra))
    return "\n".join([
        f"cd {shlex.quote(str(cwd))} || exit 1",
        *([f"rm -rf -- {gone} || exit 1"] if gone else []),
        f"mkdir -p {parents} && touch {shlex.quote(marker)} || exit 1",
        *([f"export {exports}"] if exports else []),
        "exec 3<&0",
        f"setsid bash -c {shlex.quote(command)} </dev/null & pid=$!",
        # Nothing comes on stdin: it ends when the ssh does, as when the session stops the command. The signals go to
        # the process too, which has no group of its own until setsid has made one.
        '{ while read -r _ <&3; do :; done; kill -TERM -- -"$pid" "$pid"; sleep 5; kill -KILL -- -"$pid" "$pid"; }'
        " >/dev/null 2>&1 &",
        "watch=$!",
        'wait "$pid"; code=$?',
        'kill "$watch" 2>/dev/null',
        # Whatever the command left running in its group ends with it.
        'kill -KILL -- -"$pid" 2>/dev/null',
        'exit "$code"',
    ])  # fmt: skip


def _record(place: str, message: str) -> None:
    """Say loudly that the offload failed, and put it on the sweep's list of errors to fix."""
    print(f"{TAG} {message}", file=sys.stderr, flush=True)
    try:
        conn = db.connect(config.DB_PATH)
        try:
            page_errors.record(conn, "offload", place, message, "", 1, db.now_iso(), False)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - the error above is already on screen; this one joins it
        print(f"{TAG} Could not record this error for the sweep either: {exc}", file=sys.stderr, flush=True)


def _here(command: str, cwd: Path) -> int:
    return subprocess.run(["bash", "-c", command], cwd=cwd, check=False).returncode


def _there(command: str, cwd: Path, env: Mapping[str, str], work: Plan) -> int | None:
    """Run `command` on the host and return its exit code; None when the checkout could not be copied there.

    The checkouts the command runs in stay locked until their files are back, so no other run copies over them
    meanwhile; the checkouts it only reads are locked while they are copied."""
    held = {root: _hold(root) for root in sorted(work.roots)}
    started = time.monotonic()
    copied_at = time.time_ns()
    try:
        try:
            _connect(work.host)
            _push(work)
        except (OffloadError, OSError, subprocess.TimeoutExpired) as exc:
            _record("copy", f"Could not use {work.host}, so this runs here instead: {exc}")
            return None
        for root, handle in held.items():
            if root not in work.ran_in:
                handle.close()
        moved = " The `cd` in it changed folder there only." if "cd" in (_tokens(command) or []) else ""
        print(
            f"{TAG} Running on {work.host}, the machine that runs slow commands, from a copy of this checkout"
            f" ({time.monotonic() - started:.1f} s to copy).{moved}",
            file=sys.stderr, flush=True,
        )  # fmt: skip
        marker = f"{CONTROL_DIR}/run-{uuid.uuid4().hex}"
        script = _remote_script(command, cwd, env, work, marker)
        # stdin stays open and empty: when this process ends, ssh's stdin closes and the host stops the command.
        with subprocess.Popen(
            ["ssh", *SSH_OPTIONS, work.host, f"bash -c {shlex.quote(script)}"], stdin=subprocess.PIPE
        ) as ssh:
            code = ssh.wait()
        if code == 255:
            print(
                f"{TAG} ssh ended with 255: the connection to {work.host} may have broken.", file=sys.stderr, flush=True
            )
        try:
            _pull(work, marker, copied_at)
        except (OffloadError, OSError, subprocess.TimeoutExpired) as exc:
            _record("copy back", f"The command ran on {work.host}, but its files did not all come back: {exc}")
        return code
    finally:
        for handle in held.values():
            handle.close()


def _venv_left_out(work: Plan) -> Path | None:
    """A `.venv` link in a checkout the command runs in whose target the copy leaves out, such as another worktree's
    `.venv`: on the host it would point at nothing."""
    tops = [root for root in work.roots if root not in work.mains]
    for top in work.ran_in:
        link = top / ".venv"
        if not link.is_symlink():
            continue
        target = Path(os.path.realpath(link))
        if not any(_inside(target, root) for root in tops) and not any(
            _inside(target, main) and not _inside(target, main / ".claude" / "worktrees") for main in work.mains
        ):
            return link
    return None


def run(command: str, cwd: Path, env: Mapping[str, str]) -> int:
    """Run `command` from `cwd` on the offload host when it can go there, else here; returns its exit code."""
    offload = config.setup().offload
    if offload is None:
        print(f"{TAG} This machine's setup names no offload host, so this runs here.", file=sys.stderr, flush=True)
        return _here(command, cwd)
    work = plan(command, cwd, env, offload)
    if work is None:
        print(
            f"{TAG} This runs here: it is not a test run, bun or Python script in {', '.join(map(str, offload.repos))}"
            " (or a worktree of one) whose every part this check can read.",
            file=sys.stderr, flush=True,
        )  # fmt: skip
        return _here(command, cwd)
    link = _venv_left_out(work)
    if link is not None:
        print(
            f"{TAG} This runs here: {link} links to {os.path.realpath(link)}, which the copy to {work.host} leaves out."
            f" To run it there, replace the link with the checkout's own: `rm .venv && uv sync` in {link.parent}.",
            file=sys.stderr, flush=True,
        )  # fmt: skip
        return _here(command, cwd)
    code = _there(command, cwd, env, work)
    return _here(command, cwd) if code is None else code


def main() -> None:
    parser = argparse.ArgumentParser(
        prog=WRAPPER_NAME, description="Run a slow command on the offload host, or here when it cannot go there."
    )
    parser.add_argument("command", help="the command, as one shell string, run from the current folder")
    args = parser.parse_args()
    sys.exit(run(args.command, Path.cwd(), os.environ))
