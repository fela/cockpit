"""Where Cockpit keeps its files and how it is reached.

What differs between installs (SPEC SCOPE-5) is in one setup file, CONFIG_DIR/setup.toml:
the user's name, the Google account that may sign in, the page's address, the folders
Cockpit works in, the commands it shows and the user's timezone. The people the morning
briefing follows are set on the page (briefing.py). A missing file or required key stops Cockpit at start
with an error that names it. `docs/setup.example.toml` shows every key. The install's parts (parts.py) may read tables
of their own from the same file.
"""

from __future__ import annotations

import functools
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cockpit.parts import TableReader, model_routes, setup_tables

HOME = Path.home()
DATA_DIR = HOME / ".local" / "share" / "cockpit"
DB_PATH = DATA_DIR / "cockpit.db"
BACKUP_DIR = DATA_DIR / "backups"
BACKUP_KEEP_DAYS = 14
CONFIG_DIR = HOME / ".config" / "cockpit"
TOKEN_PATH = CONFIG_DIR / "token"
LOCAL_LOGIN_PATH = CONFIG_DIR / "local-login-token"
COOKIE_KEY_PATH = CONFIG_DIR / "cookie-key"
GOOGLE_CLIENT_PATH = CONFIG_DIR / "google-oauth-client.json"
SETUP_PATH = CONFIG_DIR / "setup.toml"
# The secrets the user hands sessions from a step (SPEC MSG-15), a file each, readable only by this user.
SECRETS_DIR = CONFIG_DIR / "secrets"
# Connector sign-ins waiting for the address the user pastes on the card (connector_signin.py), a file each.
SIGNINS_DIR = CONFIG_DIR / "signins"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8790
API_BASE = f"http://{LISTEN_HOST}:{LISTEN_PORT}"
# Each live Claude Code session listens on <pid>.sock here.
SOCKETS_DIR = (Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "cc-socks"
               if sys.platform == "linux" else Path(f"/tmp/cc-socks-{os.getuid()}"))
SWEEP_PROMPT = (
    "Use the cockpit skill, read its references/sweep.md, and run a full sweep (section \"Sweep\"): check every"
    " source, decide what has priority, and get the top items ready."
)
# The user's Claude Code settings: the allow lists the page reads and changes.
CLAUDE_SETTINGS = HOME / ".claude" / "settings.json"
# The Claude rate limits and account the user's status line saves (usage.py), one record per account.
CLAUDE_RATE_LIMITS = HOME / ".claude" / "rate-limits.json"
# Claude Code's native installer puts each release in ~/.local/share/claude/versions/<version> and points this
# command at the newest one.
CLAUDE_COMMAND = HOME / ".local" / "bin" / "claude"
# The model every Claude session's own conversation runs on: Cockpit starts sessions and explains calls with it, and
# the page warns about a session that answers on any other (SPEC SESSION-20). A full name rather than an alias such
# as `opus`, which Claude Code points at whichever model is newest, so a change of model is a change here.
MAIN_MODEL = "claude-opus-5-5"
# What the user may pick for a new session (SPEC SESSION-22), per provider: its models as (full name, label), the
# default first, and its reasoning efforts, the default named apart. Claude's default is MAIN_MODEL at high effort
# (SESSION-2); Codex's is the first model listed below, also at high effort, at Standard speed.
SESSION_OPTIONS: dict[str, dict[str, Any]] = {
    "claude": {
        "models": [(MAIN_MODEL, "Opus 5.5"), ("claude-fable-5-2", "Fable 5.2"), ("claude-sonnet-5-5", "Sonnet 5.5"),
                   ("claude-haiku-5-5", "Haiku 5.5")],
        "efforts": ["low", "medium", "high", "xhigh", "max"],
        "effort": "high",
    },
    "codex": {
        "models": [("gpt-6.1-sol", "GPT-6.1 Sol"), ("gpt-6.1-terra", "GPT-6.1 Terra"), ("gpt-6-luna", "GPT-6 Luna"),
                   ("gpt-6-astra", "GPT-6 Astra")],
        "efforts": ["low", "medium", "high", "xhigh"],
        "effort": "high",
        "speeds": [
            {"name": "standard", "label": "Standard", "hint": "default"},
            {"name": "fast", "label": "Fast", "hint": "2.5× included usage"},
            {"name": "ultrafast", "label": "Ultrafast", "hint": "8× included usage · Pro $500"},
        ],
    },
}


def session_speeds(provider: str, model: str) -> list[dict[str, str]]:
    """The speeds supported by this model; Ultrafast also needs an eligible provider account."""
    return [speed for speed in SESSION_OPTIONS[provider].get("speeds", [])
            if speed["name"] != "ultrafast" or model in ("gpt-6.1-sol", "gpt-6-astra")]


def validate_speed(provider: str, model: str, speed: str | None) -> None:
    if speed is not None and speed not in [each["name"] for each in session_speeds(provider, model)]:
        raise ValueError(f"{provider} model {model} offers no speed {speed}")


def codex_model(model: str, speed: str) -> str:
    """The model name sent to Codex, including any route this install reserves for its speed."""
    return model_routes().get(("codex", model, speed), model)


@dataclass(frozen=True)
class Account:
    """One Claude subscription signed in on this machine, in a config folder of its own (SPEC SESSION-17).

    The folder holds the account's sign-in (.credentials.json), its background daemon, its jobs folder and so
    its `claude agents` list. Its state file (.claude.json) names the signed-in account (`oauthAccount`) and
    records per checkout whether the user trusts it.
    """

    # What the page and the logs call it.
    name: str
    # The one letter a new session's account button shows (SPEC SESSION-22): W for work, P for personal.
    letter: str
    folder: Path
    state: Path
    # Claude's own default folder: `claude` runs there with CLAUDE_CONFIG_DIR unset. Set to ~/.claude, Claude reads
    # ~/.claude/.claude.json instead of ~/.claude.json, which holds no trust.
    default: bool

    @property
    def jobs(self) -> Path:
        return self.folder / "jobs"


# Both accounts are signed in at once. The personal one is Claude's default folder, used alone until the work
# folder holds a sign-in; scripts/second-account.sh sets the work folder up.
ACCOUNTS = (
    Account(name="personal", letter="P", folder=HOME / ".claude", state=HOME / ".claude.json", default=True),
    Account(name="work", letter="W", folder=HOME / ".claude-work", state=HOME / ".claude-work" / ".claude.json", default=False),
)
# What every account shares with the default one: a symlink in its folder to the same entry in ~/.claude.
# Transcripts and auto-memory, the permission rules and hooks Cockpit writes, skills, plugins, agents and the
# status line script, which writes every account's usage into CLAUDE_RATE_LIMITS. The rest stays per folder.
SHARED_ENTRIES = ("projects", "settings.json", "skills", "plugins", "agents", "statusline-command.sh")
# The shared entries another folder holds as a bind mount of the ~/.claude entry instead of a symlink. Claude Code
# asks before every write through a symlink that leads outside the session's folders, and allow rules cannot
# open it, so with projects as a symlink every auto-memory write on another account raises a dialog (SPEC PERM-15).
MOUNTED_ENTRIES = ("projects",)

# The Codex accounts signed in on this machine, one folder (CODEX_HOME) each, with its own sign-in (auth.json),
# session files and weekly limit (SPEC SESSION-10). ~/.codex is Codex's default; a folder that does not exist
# counts no use.
CODEX_HOMES = (HOME / ".codex", HOME / ".codex-2")


def account(name: str) -> Account:
    """The account called `name`; an unknown name is a fault in Cockpit."""
    found = next((each for each in ACCOUNTS if each.name == name), None)
    if found is None:
        raise LookupError(f"no Claude account {name!r}; config.ACCOUNTS has {', '.join(a.name for a in ACCOUNTS)}")
    return found


class SetupError(Exception):
    """The setup file is missing, or a key in it is missing or wrong."""


@dataclass(frozen=True)
class Offload:
    """A second machine that runs the slow commands sessions hand to `cockpit-offload`, so they leave this one's cores
    to the sessions (SPEC BG-29)."""

    # Its SSH host, as this machine's SSH setup names it. It has the same user, home folder and tools.
    host: str
    # The main checkouts whose commands may run there, with every worktree of each.
    repos: tuple[Path, ...]


@dataclass(frozen=True)
class Setup:
    """One install's user and folders, read from the setup file."""

    # What the page and the messages to sessions call the user.
    name: str
    # The one Google account that may sign in on the page.
    email: str
    # The page's address, as the browser reaches it.
    origin: str
    # The folder of checkouts: offered as a new session's folder, and where the sweep session runs.
    checkouts: Path
    # The checkout whose docs/SPEC.md the spec page shows and commits to.
    spec_repo: Path
    # The checkout every session on this machine loads the user's skills from; the skills page edits them there.
    skills_repo: Path
    # The skills' folder inside skills_repo, a folder per skill holding its SKILL.md.
    skills_folder: str
    # The branch of skills_repo an edit on the skills page pulls first and pushes to.
    skills_branch: str
    # The command a terminal on the user's computer runs to open a session; {session} stands for its id.
    attach: str
    # The user's timezone: the morning briefing is ready at BRIEFING_HOUR there.
    timezone: ZoneInfo
    # The tables the install's parts read, by table name, each as its reader returned it.
    parts: dict[str, Any]
    auth_mode: str = "google"
    providers: tuple[str, ...] = ("claude",)
    default_provider: str = "claude"
    codex_command: str = "codex"
    claude_sockets_dir: str = ""
    # The branch of spec_repo that `cockpit deploy` installs and restarts the service on; "" turns deploy off.
    deploy_branch: str = ""
    # The address public pages open at (SPEC API-5), a host of their own where nobody signs in; "" turns them off.
    public_origin: str = ""
    # Accounts the user can use up before their week resets: address to the time (seconds) that stops mattering (SPEC UI-75).
    last_use: dict[str, float] = field(default_factory=dict)
    # The Claude accounts (ACCOUNTS, by name) taken out of use for good (accounts.retired, SPEC UI-78).
    retired_accounts: tuple[str, ...] = ()
    # The machine that runs sessions' slow commands ([offload], SPEC BG-29); None runs them here.
    offload: Offload | None = None


PATH_KEYS = {"checkouts", "spec_repo", "skills_repo"}


def validate_public_origin(path: Path, public_origin: str, origin: str, auth_mode: str) -> None:
    """A page anyone can open runs on a host of its own, never Cockpit's, so a hostile one can't pose as Cockpit."""
    if auth_mode == "local":
        raise SetupError(f"{path}: local mode has no public pages, so leave public_origin out")
    parts = urlsplit(public_origin)
    if parts.scheme != "https" or not parts.netloc or parts.path or parts.query or parts.fragment:
        raise SetupError(f"{path}: public_origin must be an https:// address without a path or a trailing /")
    if parts.port == 443 or parts.netloc != parts.netloc.lower():
        # The server matches a request's Origin and Host against this value, in the form browsers send them.
        canonical = f"https://{parts.hostname}" + (f":{parts.port}" if parts.port not in (None, 443) else "")
        raise SetupError(f"{path}: public_origin must be written as browsers send it: {canonical}")
    if parts.netloc.lower() == urlsplit(origin).netloc.lower():
        raise SetupError(f"{path}: public_origin must be a different host from origin, so a public page can't pose as Cockpit")


def read_setup(path: Path, tables: dict[str, TableReader]) -> Setup:
    """The setup in `path`, checked: every key present, no unknown key, each value in its form. `tables` are the
    parts' tables, each checked by its own reader."""
    if not path.is_file():
        raise SetupError(f"no setup file at {path}; copy docs/setup.example.toml there and fill in every key")
    try:
        values: dict[str, Any] = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise SetupError(f"{path} is not valid TOML: {exc}") from exc
    keys = [field.name for field in fields(Setup) if field.name != "parts"]
    taken = sorted(set(tables) & {*keys, "parts"})
    if taken:
        raise ValueError(f"a part's setup table has the name of a setup key: {', '.join(taken)}")
    if "team" in values:
        raise SetupError(
            f"{path}: the team is set on Cockpit's page now: copy your [[team]] tables somewhere, remove them "
            "from this file, and paste the people into the question Cockpit asks on its first page"
        )
    optional = {
        "auth_mode", "providers", "default_provider", "codex_command", "claude_sockets_dir", "deploy_branch",
        "public_origin", "last_use", "retired_accounts", "offload",
    }
    missing = [key for key in keys if key not in values and key not in optional]
    unknown = sorted(set(values) - set(keys) - set(tables))
    if missing or unknown:
        problems = [f"missing {', '.join(missing)}" if missing else "", f"unknown {', '.join(unknown)}" if unknown else ""]
        raise SetupError(f"{path}: {'; '.join(problem for problem in problems if problem)} (docs/setup.example.toml lists every key)")
    part_tables = {name: read(path, values.pop(name, None)) for name, read in tables.items()}
    values = {"auth_mode": "google", "providers": ["claude"], "default_provider": "claude",
              "codex_command": "codex", "claude_sockets_dir": "", "deploy_branch": "", "public_origin": "",
              "last_use": {}, "retired_accounts": [], "offload": None, **values}
    may_be_empty = {"claude_sockets_dir", "deploy_branch", "public_origin"}
    wrong = [key for key in keys if key not in {"providers", "last_use", "retired_accounts", "offload"}
             and (not isinstance(values[key], str) or (not values[key].strip() and key not in may_be_empty))]
    if wrong:
        raise SetupError(f"{path}: {', '.join(wrong)} must be text that is not empty")
    try:
        timezone = ZoneInfo(values["timezone"])
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise SetupError(f"{path}: timezone must be a zone name such as Europe/Berlin, not {values['timezone']!r}") from exc
    paths = {key: Path(values[key]).expanduser() for key in PATH_KEYS if values[key]}
    relative = [key for key, value in paths.items() if not value.is_absolute()]
    if relative:
        raise SetupError(f"{path}: {', '.join(relative)} must be absolute paths or start with ~/")
    if "@" not in values["email"]:
        raise SetupError(f"{path}: email must be a Google account's address, such as you@example.com")
    if values["auth_mode"] not in {"local", "google"}:
        raise SetupError(f"{path}: auth_mode must be local or google")
    if values["auth_mode"] == "local":
        validate_local_origin(values["origin"])
    elif not values["origin"].startswith("https://") or values["origin"].endswith("/"):
        raise SetupError(f"{path}: origin must be the page's https:// address without a trailing /")
    public_origin = str(values["public_origin"])
    if public_origin:
        validate_public_origin(path, public_origin, values["origin"], values["auth_mode"])
    if "{session}" not in values["attach"]:
        raise SetupError(f"{path}: attach must contain {{session}}, where the session's id goes")
    providers = values["providers"]
    if (not isinstance(providers, list) or not providers or any(p not in ("claude", "codex") for p in providers)
            or len(set(providers)) != len(providers)):
        raise SetupError(f"{path}: providers must be a non-empty list of claude and/or codex")
    if values["default_provider"] not in providers:
        raise SetupError(f"{path}: default_provider must be enabled in providers")
    last_use = values["last_use"]
    if (not isinstance(last_use, dict)
            or any("@" not in address or not isinstance(end, datetime) or end.tzinfo is None for address, end in last_use.items())):
        raise SetupError(f"{path}: last_use must give each address a time with its offset, such as "
                         '{"you@example.com" = 2030-01-15T10:00:00Z}')
    retired_accounts = values["retired_accounts"]
    others = {account.name for account in ACCOUNTS if not account.default}
    if not isinstance(retired_accounts, list) or any(not isinstance(name, str) or name not in others for name in retired_accounts):
        raise SetupError(f"{path}: retired_accounts must list Claude accounts other than the default one, such as"
                         f" {sorted(others)}")
    checked: dict[str, Any] = {**values, **paths, "timezone": timezone, "parts": part_tables, "providers": tuple(providers),
                               "offload": _read_offload(path, values["offload"]),
                               "retired_accounts": tuple(retired_accounts),
                               "last_use": {address: end.timestamp() for address, end in last_use.items()}}
    return Setup(**checked)


def _read_offload(path: Path, table: Any) -> Offload | None:
    """The [offload] table, checked; None when the setup has none."""
    if table is None:
        return None
    if (not isinstance(table, dict) or set(table) != {"host", "repos"} or not isinstance(table["host"], str)
            or not re.fullmatch(r"[A-Za-z0-9][\w.@-]*", table["host"]) or not isinstance(table["repos"], list)
            or not table["repos"] or any(not isinstance(repo, str) for repo in table["repos"])):
        raise SetupError(f"{path}: [offload] takes host, an SSH host name, and repos, a list of main checkouts")
    repos = tuple(Path(repo).expanduser() for repo in table["repos"])
    if any(not repo.is_absolute() for repo in repos):
        raise SetupError(f"{path}: [offload] repos must be absolute paths or start with ~/")
    return Offload(host=table["host"], repos=repos)


def validate_local_origin(origin: str) -> None:
    """Local mode is deliberately limited to the server's loopback HTTP address."""
    if origin not in {f"http://127.0.0.1:{LISTEN_PORT}", f"http://localhost:{LISTEN_PORT}"}:
        raise SetupError(f"local origin must be http://127.0.0.1:{LISTEN_PORT} or http://localhost:{LISTEN_PORT}")


@functools.cache
def setup() -> Setup:
    """This install's setup, read once from SETUP_PATH."""
    return read_setup(SETUP_PATH, setup_tables())


def user_name() -> str:
    """What the page and the messages to sessions call the user."""
    return setup().name


def cockpit_skill_folder() -> Path:
    user = setup()
    return user.skills_repo / user.skills_folder / "cockpit"


def api_base() -> str:
    """Reach local mode under its configured Host; Google deployments use the loopback listener."""
    configured = setup()
    return configured.origin if configured.auth_mode == "local" else API_BASE


def private_data_dirs() -> None:
    """Create and tighten the two directories that hold private database files."""
    for path in (DATA_DIR, BACKUP_DIR):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
