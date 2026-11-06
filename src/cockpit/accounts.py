"""The Claude accounts Cockpit runs sessions on, and which one new work goes to (SPEC SESSION-17).

Each account (config.ACCOUNTS) has a config folder of its own: its sign-in, background daemon, jobs folder and
`claude agents` list. Every `claude` Cockpit runs names its account: the default one with CLAUDE_CONFIG_DIR
unset, any other with CLAUDE_CONFIG_DIR set to its folder (claude_env). A session stays on the account it started
on, so its resumes, messages, stops and limit wakes run there; only a new session, or a one-shot `claude -p`,
picks one (pick).

The default account is always in use. Another one is in use once its folder holds a sign-in; until then Cockpit
runs everything on the default one, and the page says so when the folder exists without a sign-in (in_use).

The setup file can retire an account for good (retired, SPEC UI-78): nothing new goes to it and the page stops
mentioning it. Transcripts are shared, so its stopped sessions resume on the default account (current_name).

What the accounts share lives in the default folder, linked from every other folder (config.SHARED_ENTRIES), or
for projects bind-mounted there (config.MOUNTED_ENTRIES). Claude Code replaces a file it rewrites instead of
writing through the link, so a link that became a real file or folder splits the account off: link_problems
finds that, and the page shows it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cockpit import config, db, resets, usage
from cockpit.config import MOUNTED_ENTRIES, SHARED_ENTRIES, Account

log = logging.getLogger(__name__)

# The accounts whose folder is missing, logged once per process: before the setup that is the normal state.
_told_missing: set[str] = set()
# How long before a sign-in ends the page starts to say so.
SIGN_IN_WARNING = timedelta(days=3)


class SignedOut(Exception):
    """No account new work may go to is signed in (usable)."""


def claude_env(account: Account) -> list[str]:
    """The `env` words that run a `claude` command on `account`, whatever Cockpit's own environment holds."""
    if account.default:
        return ["env", "-u", "CLAUDE_CONFIG_DIR"]
    return ["env", f"CLAUDE_CONFIG_DIR={account.folder}"]


def retired(account: Account) -> bool:
    """Whether the setup file took the account out of use for good (SPEC UI-78)."""
    return account.name in config.setup().retired_accounts


def current_name(name: str) -> str:
    """The account a stopped session listed on account `name` resumes on: the default one when the setup file retired
    `name`, else `name` itself."""
    if name in config.setup().retired_accounts:
        return next(account.name for account in config.ACCOUNTS if account.default)
    return name


def signed_in(account: Account) -> bool:
    # macOS keeps normal Claude logins in the Keychain; Claude reports a missing login when called.
    if sys.platform == "darwin":
        return account.default or account.folder.is_dir()
    return (account.folder / ".credentials.json").is_file()


def _oauth(account: Account) -> dict[str, Any] | None:
    """The sign-in file's `claudeAiOauth` entry; None without a sign-in file or one that cannot be read. Callers
    read single fields of it and never log it, as it holds the tokens."""
    try:
        oauth = json.loads((account.folder / ".credentials.json").read_text()).get("claudeAiOauth")
    except FileNotFoundError:
        return None
    except (OSError, ValueError, AttributeError) as exc:
        # The type only: a decode error's text quotes the file's bytes, which hold the tokens.
        log.error("reading the %s account's sign-in failed: %s", account.name, type(exc).__name__)
        return None
    return oauth if isinstance(oauth, dict) else None


def _emptied(account: Account) -> bool:
    """Whether Claude Code emptied the account's sign-in: when it fails to renew its access for good, it keeps the
    file with blank tokens and the old refreshTokenExpiresAt."""
    oauth = _oauth(account) if signed_in(account) else None
    return oauth is not None and "refreshToken" in oauth and not oauth["refreshToken"]


def sign_in_expires(account: Account) -> datetime | None:
    """When the account's sign-in ends: Claude renews its access on its own until its refresh token expires,
    and after that every call fails until the user signs in again. A sign-in Claude emptied (_emptied) ended when
    the file last changed. None without a sign-in or an expiry."""
    if _emptied(account):
        return sign_in_changed(account)
    oauth = _oauth(account)
    expires = oauth.get("refreshTokenExpiresAt") if oauth else None
    return datetime.fromtimestamp(expires / 1000, UTC) if isinstance(expires, (int, float)) else None


def sign_in_changed(account: Account) -> datetime | None:
    """When the account's sign-in file last changed: a sign-in, or Claude renewing its access. None without one."""
    try:
        return datetime.fromtimestamp((account.folder / ".credentials.json").stat().st_mtime, UTC)
    except FileNotFoundError:
        return None


def signed_out(account: Account, now: datetime) -> bool:
    """Whether every call on the account answers "Login expired" until the user signs in again (SPEC UI-37): Claude
    emptied its sign-in (_emptied), or the sign-in ended (sign_in_expires). False without a sign-in file."""
    expires = sign_in_expires(account)
    return _emptied(account) or (expires is not None and expires <= now)


def usable(accounts: list[Account], now: datetime) -> list[Account]:
    """The accounts new work may go to: those not signed out (signed_out). Empty when every one is."""
    return [account for account in accounts if not signed_out(account, now)]


def sign_ins(accounts: tuple[Account, ...], now: datetime) -> list[dict[str, Any]]:
    """For the top of the page, each account that is signed out (signed_out), or whose sign-in ends within
    SIGN_IN_WARNING (SPEC UI-37, UI-40): its name, whether it is signed out, when its sign-in ends or ended, what that
    means in words, and the command that signs it in again from a terminal."""
    found = []
    others = usable(in_use(accounts, []), now)
    for account in accounts:
        if retired(account):
            continue
        expires = sign_in_expires(account)
        command = " ".join([*claude_env(account), "claude", "auth", "login"])
        if signed_out(account, now):
            goes = "go to the other account" if others else "start nowhere until one is signed in"
            text = (
                f"The {account.name} Claude account is signed out: its sessions answer \"Login expired\" and new"
                f" sessions {goes}."
            )
        elif expires is not None and expires - now <= SIGN_IN_WARNING:
            text = f"The {account.name} Claude account's sign-in ends {expires:%a %d %b %H:%M} UTC."
        else:
            continue
        found.append({
            "account": account.name,
            "signed_out": signed_out(account, now),
            "ends_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ") if expires else None,
            "text": text,
            "command": command,
        })
    return found


def in_use(accounts: tuple[Account, ...], warnings: list[str]) -> list[Account]:
    """The accounts sessions run on: the default one, and each other one not retired whose folder holds a sign-in. A
    folder without one adds a line to `warnings`, which the page shows; a missing folder is logged once."""
    used = []
    for account in accounts:
        if retired(account):
            continue
        if account.default or signed_in(account):
            used.append(account)
        elif account.folder.is_dir():
            warnings.append(
                f"The {account.name} Claude account's folder {account.folder} has no sign-in, so every session runs on"
                f" the other account. Sign in there: CLAUDE_CONFIG_DIR={account.folder} claude, then /login."
            )
        elif account.name not in _told_missing:
            _told_missing.add(account.name)
            log.warning("no folder %s for the %s Claude account; sessions run without it", account.folder, account.name)
    return used


def listed(accounts: tuple[Account, ...], warnings: list[str]) -> list[Account]:
    """The accounts whose `claude agents` lists Cockpit reads: those in use (in_use), and each retired one whose folder
    remains, since its old sessions stay on the page."""
    used = in_use(accounts, warnings)
    return [account for account in accounts if account in used or (retired(account) and account.folder.is_dir())]


def email(account: Account) -> str:
    """The address the account's folder is signed in with (its state file's `oauthAccount`); empty when the file
    is missing or names none."""
    try:
        state = json.loads(account.state.read_text())
    except FileNotFoundError:
        return ""
    except (OSError, ValueError) as exc:
        log.error("reading the %s account's state %s failed: %s", account.name, account.state, exc)
        return ""
    oauth = state.get("oauthAccount") if isinstance(state, dict) else None
    address = oauth.get("emailAddress") if isinstance(oauth, dict) else None
    return address if isinstance(address, str) else ""


def readings(accounts: list[Account], records_path: Path) -> dict[str, dict[str, Any] | None]:
    """Each account's newest usage by account name (usage.latest): its record from the status line's file
    (usage.py), or Cockpit's own reading of the usage endpoint where that is newer; None for an account with
    neither. The record is matched by the address the account's folder is signed in with, not by the record's
    `config_dir`, because a folder may have held several accounts over time."""
    try:
        records = json.loads(records_path.read_text())["accounts"]
        if not isinstance(records, dict):
            raise TypeError(f"accounts is a {type(records).__name__}")
    except FileNotFoundError:
        records = {}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.error("reading Claude usage from %s failed: %r", records_path, exc)
        records = {}
    found: dict[str, dict[str, Any] | None] = {}
    for account in accounts:
        address = email(account)
        record = records.get(address) if address else None
        found[account.name] = usage.latest(address, record if isinstance(record, dict) else None) if address else None
    return found


def pick(
    accounts: list[Account], found: dict[str, dict[str, Any] | None], now: datetime, preference: usage.Preference | None,
    running: dict[str, int],
) -> tuple[Account, str]:
    """The account a new session or a one-shot call goes to, with the reason in words (usage.pick_key): the one the
    user prefers for now (`preference`, set on the page), while it is at no limit; else one that
    holds a banked usage-limit reset that clears its week and expires, while it is at no limit and its 5-hour window
    is not near one, so the week is used up before the reset is lost; else the one with the highest usage.pick_score
    of those at no limit (its week ending soon, its week's room for a session once the sessions running on it, counted
in `running` by account name, have had theirs, its week lasting to its reset at its rate so far, its 5-hour window
    lasting); when all
    are at one, one with a banked usage-limit reset that ends its limit now, else the one free soonest. Its first turn then ends
    on the limit, and limits.sync continues it once that account has room, as for any session the limit stopped.
    Ties go to the earlier account in config.ACCOUNTS."""
    rooms = [
        (account, usage.room(
            found.get(account.name), now.timestamp(), resets.spendable(email(account), now),
            resets.expiring(email(account), now), running.get(account.name, 0),
        ))
        for account in accounts
    ]
    preferred = preference.account if preference is not None else None
    account, room = min(rooms, key=lambda pair: usage.pick_key(pair[1], pair[0].name == preferred))
    if preference is not None and account.name == preferred and room.blocked_until is None:
        why = f"you chose it for new sessions until {preference.until.astimezone(now.tzinfo):%Y-%m-%d %H:%M} UTC"
    elif room.reset_ends is not None:
        ends = datetime.fromtimestamp(room.reset_ends, now.tzinfo)
        why = (f"it holds a banked usage-limit reset that expires at {ends:%Y-%m-%d %H:%M} UTC, so its week is used up"
               f" first; {_room_words(room, now)}")
    elif room.blocked_until is None:
        why = f"it scores {usage.pick_score(room):.2g} for new work: {_room_words(room, now)}" + "".join(
            f"; the {other.name} account scores {usage.pick_score(other_room):.2g}: {_room_words(other_room, now)}"
            for other, other_room in rooms
            if other is not account and other_room.blocked_until is None
        )
    elif room.banked:
        why = (f"every account is at a limit; this one holds {room.banked} banked usage-limit reset"
               f"{'s' if room.banked > 1 else ''} that end{'' if room.banked > 1 else 's'} it, to spend in Claude"
               " Code's limit dialog")
    else:
        free = datetime.fromtimestamp(room.blocked_until, now.tzinfo)
        why = f"every account is at a limit; this one's ends first, at {free:%Y-%m-%d %H:%M} UTC"
    return account, why


def _room_words(room: usage.Room, now: datetime) -> str:
    """The parts of an account's pick_score, in words."""
    resets_at = datetime.fromtimestamp(room.week_resets, now.tzinfo)
    left = f"{100 - room.week_used:.0f} % of it left"
    if room.running:
        each = f"{usage.RUNNING_SESSION_USE:g} %"
        sessions = f"1 session running on it uses {each}" if room.running == 1 else (
            f"{room.running} sessions running on it use {each} each")
        left += f", {room.week_left:.1f} % after the {sessions}"
    if room.week_left > 0 and room.week_runs_out_early >= 1:
        left += f", which at its rate so far runs out {room.week_runs_out_early:.0f} h before then"
    return (f"its week resets at {resets_at:%Y-%m-%d %H:%M} UTC, in {room.week_hours:.0f} h, with {left}, and its 5-hour"
            f" window is on course for {room.five_projected:.0f} %")


# How long the user can prefer an account for new work, in minutes (the page's choices): half an hour, two hours, a day.
PREFER_MINUTES = (30, 120, 1440)
PREFERENCE_KEY = "account_preference"


def preference(conn: sqlite3.Connection, now: datetime) -> usage.Preference | None:
    """The account the user prefers for new work now (prefer), None when none is set or it has ended."""
    saved = db.kv_get(conn, PREFERENCE_KEY)
    if saved is None:
        return None
    until = datetime.fromisoformat(saved["until"])
    return usage.Preference(saved["account"], until, saved["minutes"]) if until > now else None


def prefer(conn: sqlite3.Connection, accounts: tuple[Account, ...], name: str | None, minutes: int, now: datetime) -> None:
    """The user's choice on the page: new work goes to the account folder `name` for the next `minutes`, ahead of the
    usual order (pick); None clears it, and the usual order applies again."""
    if name is None:
        db.kv_set(conn, PREFERENCE_KEY, None)
        return
    if name not in {account.name for account in accounts}:
        raise ValueError(f"no account folder is named {name}")
    if minutes not in PREFER_MINUTES:
        raise ValueError(f"an account is preferred for {', '.join(map(str, PREFER_MINUTES))} minutes, not {minutes}")
    until = now + timedelta(minutes=minutes)
    db.kv_set(conn, PREFERENCE_KEY, {"account": name, "until": until.isoformat(), "minutes": minutes})
    log.info("new work prefers the %s account until %s", name, until.isoformat())


@dataclass(frozen=True)
class Problem:
    """Something wrong with what an account shares, for the page (SPEC SESSION-19): `text` says in plain words what
    is wrong, what it breaks and what to do; `detail` names the paths, one line each, and shows collapsed."""

    text: str
    detail: str


# Each shared entry in words: what it is, and what an account that stops sharing it loses. An entry it stopped
# sharing may be missing or a copy of its own, so the words say what no longer reaches it, not what it lacks.
SHARED_WORDS = {
    "projects": (
        "transcripts and memory notes",
        "A memory note saved on one account is missing on the other, and a session cannot be resumed or moved"
        " across.",
    ),
    "settings.json": (
        "settings",
        "Changes to your permission rules, auto-mode sentences and Cockpit's hooks no longer reach its sessions, so"
        " they may ask before calls you allowed, and their permission dialogs may not show on Cockpit.",
    ),
    "skills": ("skills", "Skills you add or change no longer reach its sessions."),
    "plugins": ("plugins", "Plugins you add or change no longer reach its sessions."),
    "agents": ("subagents", "Subagents you add or change no longer reach its sessions."),
    "statusline-command.sh": (
        "status line",
        "Its status line may stop reporting its usage; the Usage page and the account Cockpit picks for new sessions"
        f" then go by Cockpit's own reading, taken every {resets.EVERY_SECONDS // 60} minutes.",
    ),
}
# The fix, named in the details so the words stay plain.
SCRIPT = "Repair: scripts/second-account.sh in Cockpit's repo. It stops and says why on anything it cannot fix itself."


def link_problems(accounts: tuple[Account, ...]) -> list[Problem]:
    """What is wrong with what each account other than the default one shares, at most three problems per account:
    the entries it stopped sharing (a real file or folder, a link that points elsewhere, a missing one, or a mounted
    entry that is not a bind mount of its target), a mounted entry still reached through the link it replaced, and
    the entries that could not be checked. Only folders that exist are checked. Sharing is set up by
    scripts/second-account.sh with a bind mount in /etc/fstab, so it is checked on Linux only."""
    if sys.platform != "linux":
        return []
    home = next(account for account in accounts if account.default)
    problems = []
    for account in accounts:
        if account.default or retired(account):
            continue
        try:
            if not account.folder.is_dir():
                continue
        except OSError as exc:
            problems.append(unchecked(account, home, [f"Checking {account.folder} failed: {exc}"]))
            continue
        split, linked, failed = [], [], []
        for name in SHARED_ENTRIES:
            entry, target = account.folder / name, home.folder / name
            try:
                if name in MOUNTED_ENTRIES and entry.is_symlink() and _link_target(entry) == _link_target(target):
                    linked.append(f"{entry} is a link to {target}; it should be a bind mount of it (SPEC PERM-15).")
                    continue
                problem = entry_problem(account, name, entry, target)
            # Link loops raise RuntimeError on Python 3.12 and OSError on newer versions.
            except (OSError, RuntimeError) as exc:
                failed.append(f"Checking {entry} failed: {exc}")
                continue
            if problem:
                split.append((name, problem))
        if split:
            # "your" before each, so "transcripts and memory notes" reads as one thing in the list.
            things = [f"your {SHARED_WORDS[name][0]}" for name, _ in split]
            listed = things[0] if len(things) == 1 else f"{', '.join(things[:-1])} and {things[-1]}"
            losses = " ".join(SHARED_WORDS[name][1] for name, _ in split)
            problems.append(Problem(
                f"The {account.name} account has stopped sharing {listed} with the {home.name} account."
                f" {losses} Ask a session to repair it with the script named in the details.",
                "\n".join([*(problem for _, problem in split), SCRIPT]),
            ))
        if linked:
            problems.append(Problem(
                f"Sessions on the {account.name} account ask you before every memory note they save, because they"
                " reach the shared transcripts folder through a link. Ask a session to mount the folder instead, with"
                " the script named in the details.",
                "\n".join([*linked, SCRIPT]),
            ))
        if failed:
            problems.append(unchecked(account, home, failed))
    return problems


def unchecked(account: Account, home: Account, failures: list[str]) -> Problem:
    return Problem(
        f"Cockpit could not check what the {account.name} account shares with the {home.name} account, so it cannot"
        " tell whether its sessions see your transcripts, memory notes, settings and skills. Ask a session to look"
        " into the error in the details.",
        "\n".join(failures),
    )


def _link_target(path: Path) -> Path:
    """Resolve loops strictly while allowing shared entries whose target is not there yet."""
    try:
        return path.resolve(strict=True)
    except FileNotFoundError:
        return path.resolve()


def entry_problem(account: Account, name: str, entry: Path, target: Path) -> str | None:
    """What is wrong with one shared entry of `account`, as a line for the details, or None when it is right."""
    if name in MOUNTED_ENTRIES:
        if bind_mount_of(entry, target):
            return None
        if entry.is_symlink():
            return f"{entry} links to {entry.readlink()}; it should be a bind mount of {target}."
        if entry.is_dir():
            return (
                f"{entry} is a folder of its own, not a bind mount of {target}. A mount that failed at boot, or the"
                f" moment the script swaps a link for the mount, leaves it so: the {account.name} account's sessions"
                " write there meanwhile, and the script moves what they wrote across. A mount of another folder looks"
                " the same here; the script stops on that one."
            )
        if entry.exists():
            return f"{entry} is a file; it should be a bind mount of {target}. Move it aside, then run the script."
        return f"{entry} is missing; the script mounts {target} there."
    if entry.is_symlink():
        if _link_target(entry) != _link_target(target):
            return f"{entry} links to {entry.readlink()}, not to {target}; point it back, then run the script."
        return None
    if entry.exists():
        return (
            f"{entry} is a real {'folder' if entry.is_dir() else 'file'}, not a link to {target}. Claude Code writes a"
            f" new file in place of a link when it rewrites one, so it may hold changes made on the {account.name}"
            f" account: merge them into {target}, remove it, then run the script."
        )
    return f"{entry} is missing; the script links it to {target}."


def bind_mount_of(entry: Path, target: Path) -> bool:
    """Whether `entry` is a real folder that is `target` itself, as a bind mount of it makes it."""
    return entry.is_dir() and not entry.is_symlink() and target.is_dir() and entry.samefile(target)
