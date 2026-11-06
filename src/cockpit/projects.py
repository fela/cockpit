"""Projects: every task is in one, and each has a logo Cockpit designs (SPEC VIEW-34, SWEEP-23, CONTROL-26).

A project is a name that items.project holds, a line on what it covers (`about`), the names of projects merged into
it (`aliases`) and an SVG logo. A put of an item naming a project that does not exist is refused with the list of
projects, so the session decides whether the task belongs to one of them or needs a new one, which it creates with
`cockpit project put` (CONTROL-26). A name that differs only in case, or a merged project's name, is stored as the
project's own name.

Projects are a few broad areas of work, or big bodies of work that span many tasks, at most MAX_PROJECTS, since each
shows its logo before every task in the list.
Now and then Cockpit asks one long-lived session, named "Cockpit projects", to tidy them (SWEEP-23): put each task,
open or closed, without a project into the one it belongs to, merge narrow projects into the broad area they belong
to, and design a logo for each project without one. As with the briefing, a launch goes to the session the last one
reached while that session exists, else to a new one. It is due TIDY_HOURS after the last, and sooner, though at most
once every TIDY_SOON_MINUTES, while a task has gone UNSORTED_MINUTES without a project or a project has no logo.
"""

from __future__ import annotations

import json
import re
import sqlite3
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from cockpit import db, store
from cockpit.config import Setup

SESSION_NAME = "Cockpit projects"
NAME_CHARS = 40
ABOUT_CHARS = 200
LOGO_BYTES = 6000
MAX_PROJECTS = 10
TIDY_HOURS = 24
TIDY_SOON_MINUTES = 60
# A new task's own session sets its project first (CONTROL-26); the tidy session takes the ones it left.
UNSORTED_MINUTES = 30
# A launch whose message failed is tried again after this long.
RETRY_MINUTES = 60

SessionExists = Callable[[str], bool]

# What a logo may hold: shapes, groups, gradients and a title, with their presentation attributes. No scripts,
# styles, links, images, text or foreign content: the page shows a logo as an <img>, which runs nothing, and the
# logo itself is served with a policy that loads nothing (app.py), so this keeps it a plain drawing.
SVG = "{http://www.w3.org/2000/svg}"
ELEMENTS = frozenset({
    "svg", "g", "path", "circle", "ellipse", "rect", "line", "polyline", "polygon", "defs", "linearGradient",
    "radialGradient", "stop", "title", "clipPath",
})
ATTRIBUTES = frozenset({
    "xmlns", "viewBox", "width", "height", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "fx", "fy",
    "d", "points", "fill", "fill-opacity", "fill-rule", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin",
    "stroke-opacity", "stroke-dasharray", "opacity", "transform", "id", "offset", "stop-color", "stop-opacity",
    "gradientUnits", "gradientTransform", "clip-path", "clip-rule", "clipPathUnits", "spreadMethod",
    "stroke-miterlimit", "role", "aria-label", "focusable",
})
LOCAL_REF = re.compile(r"^url\(#[\w-]+\)$")


def _row(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": record["name"],
        "about": record["about"],
        "aliases": json.loads(record["aliases"]),
        "has_logo": record["logo"] is not None,
        "logo_at": record["logo_at"],
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
    }


def listing(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every project with its count of open items, the busiest first."""
    counts = {
        row["project"]: row["n"]
        for row in conn.execute(
            "SELECT project, count(*) AS n FROM items WHERE archived = 0 AND coalesce(stage, '') != 'done'"
            " GROUP BY project"
        )
    }
    found = [_row(record) | {"open_items": counts.get(record["name"], 0)}
             for record in db.rows(conn.execute("SELECT * FROM projects").fetchall())]
    return sorted(found, key=lambda project: (-project["open_items"], project["name"].casefold()))


def resolve(conn: sqlite3.Connection, name: str) -> str | None:
    """The project `name` names, in any case, or as the name of a project merged into it; None for none."""
    words = name.strip()
    found = conn.execute("SELECT name FROM projects WHERE name = ?", (words,)).fetchone()
    if found is not None:
        return found["name"]
    for record in conn.execute("SELECT name, aliases FROM projects"):
        if words.casefold() in (alias.casefold() for alias in json.loads(record["aliases"])):
            return record["name"]
    return None


def unknown(conn: sqlite3.Connection, name: str) -> store.Invalid:
    """The refusal of a put naming a project that does not exist: it lists the projects, so the session decides."""
    names = ", ".join(project["name"] for project in listing(conn)) or "none yet"
    return store.Invalid(
        f"there is no project {name.strip()!r}. The projects: {names}. They are a few broad areas of work and big bodies"
        " of work: put the item in the one it belongs to. Only work that starts a new broad area, or a big body of"
        " work that will span many tasks, gets a new project, created"
        ' first with `cockpit project put` ({"name": ..., "about": "one line on what it covers"})'
    )


def check_logo(svg: str) -> str:
    """The logo as stored: a plain SVG drawing with a viewBox, at most LOGO_BYTES; Invalid otherwise."""
    text = svg.strip()
    if len(text.encode()) > LOGO_BYTES:
        raise store.Invalid(f"the logo has {len(text.encode())} bytes; at most {LOGO_BYTES}")
    if "<!" in text or "<?" in text.replace('<?xml version="1.0" encoding="UTF-8"?>', ""):
        raise store.Invalid("the logo may hold no doctype, entities or processing instructions")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise store.Invalid(f"the logo is not well-formed SVG: {exc}") from exc
    if root.tag != f"{SVG}svg":
        raise store.Invalid('the logo must be one <svg xmlns="http://www.w3.org/2000/svg"> element')
    if not root.get("viewBox"):
        raise store.Invalid("the logo's <svg> needs a viewBox")
    for element in root.iter():
        tag = element.tag.removeprefix(SVG)
        if tag not in ELEMENTS or not element.tag.startswith(SVG):
            raise store.Invalid(f"the logo may not hold <{tag}>; it may hold {', '.join(sorted(ELEMENTS))}")
        for attribute, value in element.attrib.items():
            if attribute not in ATTRIBUTES:
                raise store.Invalid(f"the logo may not use the attribute {attribute!r}")
            if "url(" in value and not LOCAL_REF.match(value.strip()):
                raise store.Invalid(f"the logo's {attribute} may point only inside the logo: url(#id)")
    return text


def _project(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    found = resolve(conn, name)
    if found is None:
        raise store.NotFound(f"no project {name.strip()!r}")
    return db.row(conn.execute("SELECT * FROM projects WHERE name = ?", (found,)).fetchone())  # ty: ignore[invalid-return-type]


def put(conn: sqlite3.Connection, actor: str, name: str, about: str | None, logo: str | None) -> dict[str, Any]:
    """Create the project `name`, or change its `about` or logo; None leaves a field as it is."""
    words = name.strip()
    if not words or len(words) > NAME_CHARS or "\n" in words or "/" in words:
        raise store.Invalid(f"a project's name is one line of 1 to {NAME_CHARS} characters, without /")
    if about is not None and len(about.strip()) > ABOUT_CHARS:
        raise store.Invalid(f"a project's about is at most {ABOUT_CHARS} characters")
    checked = check_logo(logo) if logo is not None else None
    now = db.now_iso()
    with db.transaction(conn):
        found = resolve(conn, words)
        if found is None:
            count = conn.execute("SELECT count(*) FROM projects").fetchone()[0]
            if count >= MAX_PROJECTS:
                names = ", ".join(project["name"] for project in listing(conn))
                raise store.Invalid(
                    f"there are {count} projects already, the most there may be: {names}. Projects are a few broad"
                    " areas and big bodies of work. Put the task in the one it fits, or merge two projects first with"
                    " `cockpit project merge`"
                )
            conn.execute(
                "INSERT INTO projects (name, about, created_at, updated_at, updated_by) VALUES (?, ?, ?, ?, ?)",
                (words, (about or "").strip(), now, now, actor),
            )
            found = words
        elif about is not None:
            conn.execute(
                "UPDATE projects SET about = ?, updated_at = ?, updated_by = ? WHERE name = ?",
                (about.strip(), now, actor, found),
            )
        if checked is not None:
            conn.execute(
                "UPDATE projects SET logo = ?, logo_at = ?, updated_at = ?, updated_by = ? WHERE name = ?",
                (checked, now, now, actor, found),
            )
    return _row(_project(conn, found))


def logo(conn: sqlite3.Connection, name: str) -> str:
    found = _project(conn, name)
    if found["logo"] is None:
        raise store.NotFound(f"project {found['name']!r} has no logo yet")
    return found["logo"]


def merge(conn: sqlite3.Connection, actor: str, name: str, into: str) -> dict[str, Any]:
    """Move every item of project `name` into `into`, and keep `name` as an alias of it, so a later put naming it
    lands there too."""
    now = db.now_iso()
    with db.transaction(conn):
        source = _project(conn, name)
        target = _project(conn, into)
        if source["name"] == target["name"]:
            raise store.Invalid(f"{source['name']!r} and {into.strip()!r} are the same project")
        if source["name"] == store.COCKPIT_PROJECT:
            raise store.Invalid(f"the {store.COCKPIT_PROJECT} project is Cockpit's own work and stays")
        aliases = [*json.loads(target["aliases"]), source["name"], *json.loads(source["aliases"])]
        moved = conn.execute("UPDATE items SET project = ? WHERE project = ?", (target["name"], source["name"])).rowcount
        conn.execute("DELETE FROM projects WHERE name = ?", (source["name"],))
        conn.execute(
            "UPDATE projects SET aliases = ?, about = CASE WHEN about = '' THEN ? ELSE about END,"
            " updated_at = ?, updated_by = ? WHERE name = ?",
            (json.dumps(sorted(set(aliases), key=str.casefold)), source["about"], now, actor, target["name"]),
        )
    return _row(_project(conn, target["name"])) | {"moved_items": moved}


def assign(conn: sqlite3.Connection, name: str, item_ids: list[int]) -> dict[str, Any]:
    """Put the items in project `name`, quietly: a project is no news for the sweeper or the item's session."""
    with db.transaction(conn):
        found = _project(conn, name)["name"]
        for item_id in item_ids:
            store.get_item(conn, item_id)
        conn.executemany("UPDATE items SET project = ? WHERE id = ?", [(found, item_id) for item_id in item_ids])
    return {"project": found, "items": item_ids}


def remove(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    """Remove a project no item is in."""
    with db.transaction(conn):
        found = _project(conn, name)["name"]
        if found == store.COCKPIT_PROJECT:
            raise store.Invalid(f"the {store.COCKPIT_PROJECT} project is Cockpit's own work and stays")
        used = conn.execute("SELECT count(*) FROM items WHERE project = ?", (found,)).fetchone()[0]
        if used:
            raise store.Invalid(f"{used} items are in {found!r}; merge it into another project instead")
        conn.execute("DELETE FROM projects WHERE name = ?", (found,))
    return {"removed": found}


def unsorted(conn: sqlite3.Connection, now: datetime) -> list[dict[str, Any]]:
    """Items without a project, open or closed, that their sessions had UNSORTED_MINUTES to sort."""
    since = f"{now - timedelta(minutes=UNSORTED_MINUTES):%Y-%m-%dT%H:%M:%SZ}"
    return db.rows(conn.execute(
        "SELECT id, key, title FROM items WHERE trim(project) = '' AND created_at <= ? ORDER BY id",
        (since,),
    ).fetchall())


def tidy_prompt(setup: Setup, unsorted_items: list[dict[str, Any]], without_logo: list[str]) -> str:
    """What the tidy session is asked to do. Item titles and project names come from agents, so they go in defused."""
    name = setup.name
    items = "\n".join(f"- {item['id']}: {item['title']}" for item in unsorted_items) or "- none"
    logos = ", ".join(without_logo) or "none"
    return store.defused(f"""Tidy the projects on Cockpit, {name}'s work page. Every task on Cockpit is in a project. \
Projects are a few broad areas of {name}'s work, such as a product repository, research, infrastructure, the team or \
Cockpit itself, and big bodies of work that span many tasks, such as a new export format: at most {MAX_PROJECTS}, and fewer \
is better. Each has its own logo before every task in the list at \
the left, so every extra project is one more logo to tell apart. Keep projects broad and few.

Read them first: `cockpit project list` shows each project with what it covers and its open items, and \
`cockpit get --json` shows every item's title, summary, links and project.

1. Merge each narrow project, one about a small topic of a few tasks, into the broad area it belongs to, and \
projects that are the same thing under two names. A big body of work that spans many tasks keeps its own project. \
Merge with `cockpit project merge NAME --into OTHER --as projects`. The items move along, and a later put naming NAME lands \
in OTHER. Keep the broader name, or the name the most items use. Update the kept project's about so it names what it \
now covers.
2. Put each item without a project, open or closed, into the one it belongs to: `cockpit project assign NAME --items ID,ID \
--as projects`. These items have none:
{items}
   Judge from the item's title, summary, links and sessions' folders. Put it in the project it fits. Create a new one \
only for work that starts a new broad area or a big body of work that will span many tasks, with `cockpit project put --as projects` and the JSON \
{{"name": "...", "about": "..."}} on stdin. A name is short and lowercase, like the repository's name or one or two \
plain words.
3. Give each project without an about a line on what it covers, with `cockpit project put`.
4. Design a logo for each project without one ({logos}), and for each project you create. A logo is a small badge \
that shows at 14 to 20 pixels before the task's title in the list at the left, on a light and a dark page. Draw it as \
one SVG: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">`, a rounded square filling it (`rx` about 5) in \
one colour of the project's own, and on it one bold, simple glyph in white or near-black, whichever reads better on \
that colour: a letter, a symbol or a plain shape that says what the project is about. Give every project a colour and \
glyph that tell it apart from the others at a glance. \
Use only shapes (path, rect, circle, ellipse, line, polyline, polygon, g, gradients); no text elements, styles, \
scripts or images. \
Store it with `cockpit project put --as projects` and {{"name": "...", "logo": "<svg ...>...</svg>"}} on stdin. \
Cockpit's own project ({store.COCKPIT_PROJECT}) keeps the Cockpit logo and needs none.

Change nothing else on Cockpit and post nothing anywhere. Then end your turn with a one-line note of what you changed.""")


def _launch_session(conn: sqlite3.Connection, record: dict[str, Any] | None, exists: SessionExists) -> str | None:
    """The session the last launch reached, while it exists."""
    if record is None:
        return None
    launched = store.get_message(conn, record["message_id"])
    if launched["state"] != "sent" or not exists(launched["session_id"]):
        return None
    return launched["session_id"]


def tidy_due(conn: sqlite3.Connection, now: datetime) -> tuple[list[dict[str, Any]], list[str]] | None:
    """The unsorted items and projects without a logo when a tidy is due, else None."""
    record = db.kv_get(conn, "projects_tidy")
    unsorted_items = unsorted(conn, now)
    without_logo = [
        row["name"] for row in conn.execute(
            "SELECT name FROM projects WHERE logo IS NULL AND name != ? ORDER BY name COLLATE NOCASE",
            (store.COCKPIT_PROJECT,),
        )
    ]
    if record is not None:
        launched = store.get_message(conn, record["message_id"])
        since = now - datetime.fromisoformat(launched["created_at"])
        if launched["state"] in ("queued", "sending"):
            return None
        if launched["state"] == "failed":
            if since < timedelta(minutes=RETRY_MINUTES):
                return None
        elif since < timedelta(hours=TIDY_HOURS) and (
            since < timedelta(minutes=TIDY_SOON_MINUTES) or not (unsorted_items or without_logo)
        ):
            return None
    return unsorted_items, without_logo


def launch_once(conn: sqlite3.Connection, setup: Setup, exists: SessionExists, now: datetime) -> int | None:
    """Queue a tidy request when one is due; returns the message to deliver."""
    due = tidy_due(conn, now)
    if due is None:
        return None
    text = tidy_prompt(setup, *due)
    session = _launch_session(conn, db.kv_get(conn, "projects_tidy"), exists)
    created = f"{now:%Y-%m-%dT%H:%M:%SZ}"
    with db.transaction(conn):
        if session is not None:
            cursor = conn.execute(
                "INSERT INTO messages (author, session_id, text, state, created_at, as_prompt)"
                " VALUES ('cockpit', ?, ?, 'queued', ?, 0)",
                (session, text, created),
            )
        else:
            cursor = conn.execute(
                "INSERT INTO messages (author, new_cwd, new_name, text, state, created_at, as_prompt)"
                " VALUES ('cockpit', ?, ?, ?, 'queued', ?, 0)",
                (str(setup.checkouts), SESSION_NAME, text, created),
            )
        db.kv_set(conn, "projects_tidy", {"message_id": cursor.lastrowid})
    return cursor.lastrowid
