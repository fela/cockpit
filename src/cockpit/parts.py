"""Parts: features an install adds to Cockpit's public code.

An install's parts live in the package `cockpit.private`, which a copy of Cockpit may lack. Its `PARTS` lists the
part factories, its `SETUP_TABLES` the setup-file tables the parts read, its `create_tables` the database tables the
parts keep, its `HIDDEN_ITEMS` the items they keep off the main page, its `COMMANDS` the commands the parts add to
the CLI, and its `MODEL_ROUTES` any provider/model/speed aliases. `CODEX_WEEKS` reads the hub's Codex limits. `STANDING` supplies an externally supervised
session, `PAGE_OPTIONS` its page additions, `PROCESS_LABELS` labels for the resource view, and `CALENDAR` the
user's coming events, which a typed snooze time may name. `create_app` calls
each factory with a Context, and adds what the Part it gets back brings: routes, tags on pages, scripts every page
loads first and fields of /api/state.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import sqlite3

    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse

    from cockpit.codex import CodexIndex, HubReading
    from cockpit.codex_agent import ProviderSessions
    from cockpit.sessions import SessionCache
    from cockpit.spend import SpendIndex

PACKAGE: str = "cockpit.private"


@dataclass(frozen=True)
class Standing:
    """A session supervised by the install: socket delivery at once, with its lifecycle managed outside Cockpit."""

    session_id: str
    name: str
    logo: str | None
    mark: str
    description: str
    running: str
    stopped: str
    keeps: str
    copy_refusal: str
    stop_refusal: str
    halt_refusal: str
    # Delivery templates use {name}, {error} and {pid}, respectively.
    not_running: str
    unnamed: str
    socket_unreachable: str
    written: str


@dataclass(frozen=True)
class PageOptions:
    """An install's body attributes, frame origins and paths whose requests stay out of the offline outbox."""

    marks: dict[str, str | None]
    frame_origins: tuple[str, ...]
    unrecorded_prefixes: tuple[str, ...]


@dataclass(frozen=True)
class Event:
    """One event on the user's calendar."""

    title: str
    start: datetime
    end: datetime
    all_day: bool


@dataclass(frozen=True)
class Tags:
    """What a part adds to one page, each a path under /static/."""

    # Stylesheets, in place of the page's `<!-- part styles -->`.
    styles: tuple[str, ...]
    # Deferred scripts, in place of the page's `<!-- part scripts -->`: after its views, before the script that
    # starts it.
    scripts: tuple[str, ...]


@dataclass(frozen=True)
class Context:
    """What `create_app` hands every part factory."""

    # The dependency of a route only the user, signed in on the page, may call.
    user_only: Callable[[Request], None]
    # A helper page by its file name under static/, sent so Cockpit's own pages may frame it.
    framed_page: Callable[[str], HTMLResponse]
    # A helper page as sent to the request's caller: for the user, with the main page's offline outbox (errors.js).
    scoped_page: Callable[[Request, HTMLResponse], HTMLResponse]
    # The sessions on this machine; `.get()` lists them.
    session_cache: SessionCache | ProviderSessions
    # The user's Claude Code settings file, which holds their permission rules (permissions.py).
    claude_settings: Path
    # The database this app uses, for a part's persistent records.
    db_path: Path
    # Parts register routes whose responses contain secrets or personal records.
    unrecorded_paths: list[re.Pattern[str]]
    # Parts register POST routes that check a key of their own, by path, with the name of who sends there. The gate
    # lets such a request through with no cookie, token or Origin, as that actor and as nobody signed in.
    own_key_posts: dict[str, str]
    # The address the page is served at, with no trailing slash.
    origin: str
    # This machine's Claude and Codex use, read from the session transcripts.
    spend_index: SpendIndex
    codex_index: CodexIndex
    # Cockpit's own checkout, where the sessions Cockpit starts on its own begin.
    spec_repo: Path
    # The folder of the checkouts sessions may start in.
    checkouts_dir: Path
    # The enabled providers, the default first.
    providers: tuple[str, ...]
    # Delivers a queued message (store.create_message) in the background.
    deliver: Callable[[int], None]


@dataclass(frozen=True)
class Part:
    """What one part adds to the app."""

    # Adds the part's routes and exception handlers.
    register: Callable[[FastAPI], None]
    # The part's tags, by the file name of the page they go on.
    page_tags: dict[str, Tags]
    # Fields the part adds to the answer of /api/state, from the time and whether the caller is the user.
    state: Callable[[datetime, bool], dict[str, Any]]
    # Scripts every Cockpit page loads first, before its own, each a path under /static/: where a part sets what the
    # shared scripts read as they start, such as `cockpitPartPages` (keyboard.js) and `cockpitPartActions` (errors.js).
    first_scripts: tuple[str, ...]


@dataclass(frozen=True)
class Command:
    """A command a part adds to the `cockpit` CLI."""

    # The command's name, as typed after `cockpit`.
    name: str
    # Adds the command's parser: the CLI's commands, the parent parser that takes `--as NAME`, and the user's name
    # for help texts.
    add_parser: Callable[[Any, argparse.ArgumentParser, str], None]
    # Runs the parsed command with the CLI's `call(method, path, body, agent)`; the CLI prints what it returns.
    run: Callable[[argparse.Namespace, Callable[[str, str, Any, str | None], Any]], Any]


Factory = Callable[[Context], Part]
# A setup-file table's reader: the setup file's path and the table as read (None when the file has none), to the
# table's checked value. It raises config.SetupError for a wrong table.
TableReader = Callable[[Path, Any], Any]


def installed() -> list[Factory]:
    """The part factories of this install: none in a copy without the private package."""
    if importlib.util.find_spec(PACKAGE) is None:
        return []
    return importlib.import_module(PACKAGE).PARTS


def setup_tables() -> dict[str, TableReader]:
    """The setup-file tables this install's parts read, by table name."""
    if importlib.util.find_spec(PACKAGE) is None:
        return {}
    return importlib.import_module(PACKAGE).SETUP_TABLES


def create_tables(conn: sqlite3.Connection) -> None:
    """Create the tables this install's parts keep in Cockpit's database that are missing, and bring older ones to
    their current shape: db.migrate runs it after Cockpit's migrations. Nothing in a copy without the private
    package."""
    if importlib.util.find_spec(PACKAGE) is None:
        return
    importlib.import_module(PACKAGE).create_tables(conn)


def hidden_items(conn: sqlite3.Connection) -> set[int]:
    """The items this install's parts keep off the main page, unless opened by their id."""
    if importlib.util.find_spec(PACKAGE) is None:
        return set()
    return importlib.import_module(PACKAGE).HIDDEN_ITEMS(conn)


def commands() -> list[Command]:
    """The commands this install's parts add to the `cockpit` CLI: none in a copy without the private package."""
    if importlib.util.find_spec(PACKAGE) is None:
        return []
    return importlib.import_module(PACKAGE).COMMANDS


def model_routes() -> dict[tuple[str, str, str], str]:
    """An install's provider/model/speed aliases, empty in the public base."""
    if importlib.util.find_spec(PACKAGE) is None:
        return {}
    return importlib.import_module(PACKAGE).MODEL_ROUTES


def provider_availability(conn: sqlite3.Connection, now: datetime) -> dict[str, bool] | None:
    """The install's current provider availability: None without a hub, and missing keys when unknown."""
    if importlib.util.find_spec(PACKAGE) is None:
        return None
    hook = getattr(importlib.import_module(PACKAGE), "PROVIDER_AVAILABILITY", None)
    return None if hook is None else hook(conn, now)


def codex_weeks(conn: sqlite3.Connection) -> list[HubReading]:
    """Every change the install's hub kept in its Codex accounts' weekly limits, for the Codex use of a folder on the
    hub (codex.py): none in a copy without the private package or an install without a hub."""
    if importlib.util.find_spec(PACKAGE) is None:
        return []
    hook = getattr(importlib.import_module(PACKAGE), "CODEX_WEEKS", None)
    return [] if hook is None else hook(conn)


def model_capacity(conn: sqlite3.Connection, now: datetime) -> dict[str, dict[str, Any]] | None:
    """An install's saved model capacity, read without starting provider requests."""
    if importlib.util.find_spec(PACKAGE) is None:
        return None
    hook = getattr(importlib.import_module(PACKAGE), "MODEL_CAPACITY", None)
    return None if hook is None else hook(conn, now)


def standing() -> Standing | None:
    """The install's standing session, absent in a copy without the private package."""
    if importlib.util.find_spec(PACKAGE) is None:
        return None
    return importlib.import_module(PACKAGE).STANDING()


def is_standing(session_id: str | None) -> bool:
    """Whether this session's lifecycle is managed by the install's own supervisor."""
    session = standing()
    return session is not None and session_id == session.session_id


def standing_fields(session_id: str, alive: bool) -> dict[str, Any]:
    """The page's standing-session mark and description for one listed session."""
    session = standing()
    if session is None or session_id != session.session_id:
        return {"standing": False}
    return {
        "standing": True,
        "standing_name": session.mark,
        "standing_description": f"{session.description} {session.running if alive else session.stopped}",
    }


def page_options() -> PageOptions | None:
    """The install's additions to the main page, absent in the public base."""
    if importlib.util.find_spec(PACKAGE) is None:
        return None
    return importlib.import_module(PACKAGE).PAGE_OPTIONS()


def process_labels() -> dict[str, str]:
    """Command-line patterns and their resource labels, grouped under Other."""
    if importlib.util.find_spec(PACKAGE) is None:
        return {}
    return importlib.import_module(PACKAGE).PROCESS_LABELS


def calendar(now: datetime) -> list[Event]:
    """The user's events in the week from `now`, which a typed snooze time may name ("just before standup"): none in
    the public base."""
    if importlib.util.find_spec(PACKAGE) is None:
        return []
    return importlib.import_module(PACKAGE).CALENDAR(now)
