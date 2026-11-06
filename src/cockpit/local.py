"""Bootstrap a local install and manage the macOS LaunchAgent. No Google account required."""

from __future__ import annotations

import argparse
import fcntl
import getpass
import io
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import IO

from cockpit import auth, cli, config

LABEL = "cockpit.server"


def codex_command() -> str:
    """Prefer a working CLI on PATH, then the CLI bundled with the desktop app."""
    candidates = [shutil.which("codex"),
                  "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
                  "/Applications/Codex.app/Contents/Resources/codex"]
    for candidate in candidates:
        if candidate:
            try:
                result = subprocess.run([candidate, "--version"], capture_output=True, timeout=10, check=False)
                if result.returncode == 0:
                    return candidate
            except (OSError, subprocess.SubprocessError):
                pass
    return "codex"


def initialize(repo: Path, provider: str, name: str, timezone: str) -> Path:
    if config.SETUP_PATH.exists():
        raise ValueError(f"{config.SETUP_PATH} already exists; edit it to change your setup")
    repo = repo.resolve()
    if not (repo / "docs" / "SPEC.md").is_file():
        raise ValueError("--repo must name your Cockpit checkout (with docs/SPEC.md)")
    branch = subprocess.run(["git", "-C", str(repo), "branch", "--show-current"],
                            capture_output=True, text=True, check=True).stdout.strip()
    values = {
        "name": name, "email": f"{getpass.getuser()}@localhost", "auth_mode": "local",
        "origin": config.API_BASE, "providers": [provider], "default_provider": provider,
        "codex_command": codex_command(), "checkouts": str(repo.parent), "spec_repo": str(repo),
        "skills_repo": str(repo), "skills_folder": "src/cockpit/skills", "skills_branch": branch,
        "deploy_branch": branch, "attach": "cockpit attach {session}", "timezone": timezone,
    }
    # Validate before writing a durable setup or any credentials.
    text = "".join(f"{key} = {json.dumps(value, ensure_ascii=False)}\n" for key, value in values.items())
    with tempfile.TemporaryDirectory() as folder:
        trial = Path(folder) / "setup.toml"
        trial.write_text(text)
        config.read_setup(trial)
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(config.SETUP_PATH, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as handle:
        handle.write(text)
    auth.read_or_create_secret(config.LOCAL_LOGIN_PATH)
    config.setup.cache_clear()
    return config.SETUP_PATH


def launch_agent(python: Path, log_dir: Path, path: str) -> bytes:
    return plistlib.dumps({
        "Label": LABEL, "ProgramArguments": [str(python), "-m", "cockpit.server"],
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
        "EnvironmentVariables": {"PATH": path, "PYTHONUNBUFFERED": "1"},
        "StandardOutPath": str(log_dir / "server.log"), "StandardErrorPath": str(log_dir / "server.log"),
    })


def service(action: str) -> str:
    if sys.platform != "darwin":
        raise ValueError("cockpit service manages macOS launchd; on Linux use cockpit-server or systemd")
    target = f"gui/{os.getuid()}"
    plist = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"

    def launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=check)

    if action == "install":
        config.setup()  # Refuse an unconfigured service before writing anything.
        logs = config.DATA_DIR / "logs"
        logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_bytes(launch_agent(Path(sys.executable), logs, os.environ.get("PATH", "/usr/bin:/bin")))
        launchctl("bootout", f"{target}/{LABEL}", check=False)
        launchctl("bootstrap", target, str(plist))
        return f"Cockpit starts at login and is running at {config.setup().origin}. Run cockpit login. Logs: {logs / 'server.log'}"
    if action == "stop":
        launchctl("bootout", f"{target}/{LABEL}")
        return "Cockpit stopped."
    if action == "start":
        if launchctl("print", f"{target}/{LABEL}", check=False).returncode:
            launchctl("bootstrap", target, str(plist))
        launchctl("kickstart", "-k", f"{target}/{LABEL}")
        return "Cockpit started."
    return launchctl("print", f"{target}/{LABEL}").stdout


# `cockpit deploy` restarts the service this long after it returns, so the agent that ran it (a Codex
# agent is a child of the server it restarts) can finish its report first.
RESTART_DELAY_SECONDS = 3
# $1 the delay, $2 the service, $3 the launchd domain, $4 the plist: load the service if it is not, then restart it.
RESTART_SCRIPT = ('sleep "$1"; launchctl print "$2" >/dev/null 2>&1 || launchctl bootstrap "$3" "$4";'
                  ' exec launchctl kickstart -k "$2"')
# $1 the delay: restart the systemd user unit of TECH.md section 13.
SYSTEMD_RESTART_SCRIPT = 'sleep "$1"; exec systemctl --user restart cockpit'


def _plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _systemd_unit() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / "cockpit.service"


def service_python() -> Path:
    """The Python the service runs: the LaunchAgent's when one is installed, else this command's."""
    if sys.platform == "darwin" and _plist().is_file():
        return Path(plistlib.loads(_plist().read_bytes())["ProgramArguments"][0])
    return Path(sys.executable)


def _run(args: list[str], what: str) -> bytes:
    result = subprocess.run(args, capture_output=True, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout).decode(errors="replace").strip()[-2000:]
        raise ValueError(f"{what} failed: {detail or f'exit code {result.returncode}'}")
    return result.stdout


def _detach(args: list[str], lock: IO) -> None:
    """Start `args` in a new session, so it outlives this command and the server whose children may have run it.
    It holds the deploy lock until it ends, so the next deploy waits for this restart. Started by a Codex agent under
    the server, it is stopped with the service it restarts, which frees the lock while the service starts; a deploy
    that slips into that gap restarts the service again after it installs."""
    subprocess.Popen(args, start_new_session=True, pass_fds=(lock.fileno(),),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _uv() -> str:
    found = shutil.which("uv") or next(
        (str(path) for path in (Path.home() / ".local/bin/uv", Path("/opt/homebrew/bin/uv")) if path.is_file()), None)
    if found is None:
        raise ValueError("cockpit deploy needs uv on PATH")
    return found


def deploy() -> str:
    """Bring deploy_branch up to origin's, install its last commit into the service's venv, push the branch, and
    restart the service. One deploy runs at a time, so two sessions deploying at once cannot leave the older commit
    installed last.

    The fast-forward lets a session in a worktree of its own deploy what it merged on origin: Claude Code refuses
    such a session any git command in the deploy checkout. It runs only while the checkout is on the
    deploy branch with no uncommitted changes to tracked files (CODE-8). A branch that split from origin's is
    refused, and so is a push origin rejects (the branch is behind or split from origin's), both before the service's
    venv changes.
    Only committed code is installed (git archive), so a checkout mid-edit never reaches the page. The commit is
    installed into a scratch venv and imported there first; if that fails the running Cockpit is left alone.
    The restart changes the page's build version, so open pages offer Reload (SPEC UI-13).
    """
    config.DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(config.DATA_DIR / "deploy.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _deploy(lock)


def _catch_up(git: list[str], repo: Path, branch: str) -> None:
    """Fast-forward the checked-out deploy branch to origin's; a branch origin lacks is left for the push to add."""
    refused = f"bringing {branch} up to origin/{branch} (nothing was deployed)"
    dirty = _run([*git, "status", "--porcelain", "--untracked-files=no"], "reading the checkout's state").decode()
    if dirty.strip():
        files = ", ".join(line[3:] for line in dirty.splitlines())
        raise ValueError(f"{refused} failed: {repo} has uncommitted changes ({files}). That checkout holds only {branch}:"
                         f" move the work into a worktree of its own, commit it and merge it to {branch}")
    listed = subprocess.run([*git, "ls-remote", "--exit-code", "--heads", "origin", f"refs/heads/{branch}"],
                            capture_output=True, check=False)
    if listed.returncode == 2:
        return
    if listed.returncode:
        raise ValueError(f"{refused} failed: {(listed.stderr or listed.stdout).decode(errors='replace').strip()[-500:]}")
    _run([*git, "fetch", "-q", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"], refused)
    _run([*git, "merge", "-q", "--ff-only", "--no-autostash", "--no-overwrite-ignore", f"refs/remotes/origin/{branch}"],
         refused)


def _deploy(lock: IO) -> str:
    setup = config.setup()
    branch, repo = setup.deploy_branch, setup.spec_repo
    if not branch:
        raise ValueError(f"set deploy_branch in {config.SETUP_PATH} to the branch of {repo} that Cockpit runs")
    git = ["git", "-C", str(repo)]
    has_origin = subprocess.run([*git, "remote", "get-url", "origin"], capture_output=True, check=False).returncode == 0
    current = _run([*git, "branch", "--show-current"], "reading the checkout's branch").decode().strip()
    notes = []
    if current != branch:
        notes.append(f"{repo} is on {current or 'a detached HEAD'}, not {branch}; deployed {branch} as committed,"
                     " without bringing it up to origin's.")
    elif has_origin:
        _catch_up(git, repo, branch)
    commit = _run([*git, "rev-parse", "--verify", f"{branch}^{{commit}}"], f"finding {branch}").decode().strip()
    uv, python = _uv(), service_python()
    with tempfile.TemporaryDirectory(prefix="cockpit-deploy-") as folder:
        source, scratch = Path(folder) / "src", Path(folder) / "venv"
        source.mkdir()
        archive = _run([*git, "archive", "--format=tar", commit], "git archive")
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(source, filter="data")
        _run([uv, "venv", "-q", "--python", str(python), str(scratch)], "creating a scratch venv")
        _run([uv, "pip", "install", "-q", "--python", str(scratch / "bin" / "python"), str(source)],
             f"installing {branch} into a scratch venv")
        try:
            _run([str(scratch / "bin" / "python"), "-c", "import cockpit.server, cockpit.cli"], "importing it")
        except ValueError as exc:
            raise ValueError(f"{branch} at {commit[:12]} does not start, so nothing was deployed. {exc}") from exc
        if has_origin:
            # Before the live install: a push origin rejects means the branch split from origin's meanwhile.
            _run([*git, "push", "-q", "origin", f"{commit}:refs/heads/{branch}"],
                 f"pushing {branch} at {commit[:12]} to origin (nothing was deployed)")
        _run([uv, "pip", "install", "-q", "--reinstall-package", "cockpit", "--python", str(python), str(source)],
             f"installing {branch} into {python.parent.parent}")
    if sys.platform == "darwin" and _plist().is_file():
        target = f"gui/{os.getuid()}"
        _detach(["/bin/sh", "-c", RESTART_SCRIPT, "sh", str(RESTART_DELAY_SECONDS), f"{target}/{LABEL}", target,
                 str(_plist())], lock)
        restart = f"Cockpit restarts in {RESTART_DELAY_SECONDS} s; open pages then offer Reload."
    elif sys.platform.startswith("linux") and _systemd_unit().is_file():
        _detach(["/bin/sh", "-c", SYSTEMD_RESTART_SCRIPT, "sh", str(RESTART_DELAY_SECONDS)], lock)
        restart = f"Cockpit restarts in {RESTART_DELAY_SECONDS} s; open pages then offer Reload."
    else:
        restart = "Restart cockpit-server (systemctl --user restart cockpit on Linux) to run it."
    return " ".join([f"Deployed {branch} at {commit[:12]}.", restart, *notes])


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="cockpit " + argv[0])
    if argv[0] == "setup":
        parser.add_argument("--local", action="store_true", required=True)
        parser.add_argument("--repo", type=Path, default=Path.cwd())
        parser.add_argument("--provider", choices=("claude", "codex"), default="codex")
        parser.add_argument("--name", default=getpass.getuser())
        parser.add_argument("--timezone", default="UTC")
    elif argv[0] == "service":
        parser.add_argument("action", choices=("install", "start", "stop", "status"))
    elif argv[0] == "deploy":
        parser.description = ("Fast-forward deploy_branch (setup.toml) to origin's, install its last commit into the"
                              " service's venv, push the branch, and restart the service; open pages then offer"
                              " Reload. Uncommitted changes are not deployed, and a branch that split from origin's or"
                              " a commit that does not import is refused.")
    args = parser.parse_args(argv[1:])
    try:
        if argv[0] == "setup":
            print(f"Created {initialize(args.repo, args.provider, args.name, args.timezone)}. "
                  "Run cockpit-server, or cockpit service install on macOS.")
        elif argv[0] == "service":
            print(service(args.action))
        elif argv[0] == "deploy":
            print(deploy())
        else:
            setup = config.setup()
            if setup.auth_mode != "local":
                webbrowser.open(setup.origin)
            else:
                token = auth.read_or_create_secret(config.LOCAL_LOGIN_PATH)
                request = urllib.request.Request(
                    config.API_BASE + auth.LOCAL_SIGN_IN_PATH + "/code", method="POST",
                    headers={"Authorization": f"Bearer {token}", "Origin": setup.origin,
                             "Host": setup.origin.removeprefix("http://")},
                )
                deadline = time.monotonic() + 15
                while True:
                    try:
                        with cli.LOCAL_OPENER.open(request, timeout=10) as response:
                            code = json.load(response)["code"]
                        break
                    except urllib.error.HTTPError as exc:
                        raise ValueError(f"Cockpit refused local sign-in (HTTP {exc.code}); run cockpit login again") from exc
                    except OSError as exc:
                        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
                        if isinstance(reason, ConnectionRefusedError) and time.monotonic() < deadline:
                            time.sleep(0.5)
                            continue
                        raise ValueError("Cockpit is not running; start cockpit-server and run cockpit login again") from exc
                address = setup.origin + auth.LOCAL_SIGN_IN_PATH + "#" + code
                if not webbrowser.open(address):
                    print(f"Open this one-time address within two minutes:\n{address}")
                    return 0
            print("Opened Cockpit in your browser.")
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"cockpit: {exc}", file=sys.stderr)
        return 1
    return 0
