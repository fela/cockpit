"""Cockpit's rules on top of the tables: upserts, the user's controls, goals, images, pages, messages, sweeps, and the page state."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from html import escape as html_escape
from pathlib import Path
from typing import Any

from cockpit import accounts, config, db, github, history, patterns, projects, rank, timesheet
from cockpit.config import user_name
from cockpit.failures import failure_reason
from cockpit.render import (
    NO_REFS,
    Refs,
    markdown_html,
    markdown_inline_html,
    mentions_html,
    step_figures,
    step_markdown_html,
)
from cockpit.sessions import Session, as_dict, last_message, skills_loaded

log = logging.getLogger(__name__)

# A sweep that has written nothing for this long counts as abandoned.
SWEEP_SILENT_MINUTES = 20
RECENT_MESSAGES = 30
DONE_RECENT_DAYS = 7
CLOSED_RECENT_HOURS = 24
# The sweep checks the archives the user made in this window (SPEC SESSION-4).
ARCHIVE_CHECK_HOURS = 48
SESSION_RECENT_HOURS = 24
# An item's title is at most this long; one the user adds takes the first line of the user's text, clipped.
TITLE_CHARS = 300
# The user's Add takes text up to ADD_MAX_CHARS. Past ADD_SUMMARY_CHARS the summary keeps the start and the
# whole text goes on the item as a page, which the session reads (SPEC CONTROL-8).
ADD_MAX_CHARS = 1_000_000
ADD_SUMMARY_CHARS = 2000
# The priorities from lowest to highest: lowering an item's priority sets it aside (`upsert_item`).
PRIORITY_ORDER = ("low", "normal", "high", "urgent")
# The priority a to-do takes from the user's own words in Add (SPEC RANK-17): a sentence or clause of its own that
# says it, such as "High priority." or "This is urgent: …", not a mention inside other words ("why high priority
# steps wait", "not urgent"). A line break inside a sentence is a space. The first that matches wins.
ADD_CLAUSE_BREAK = re.compile(r"[.!?:;]|\n[ \t]*\n")
ADD_PRIORITY_WORDS = tuple(
    (name, re.compile(rf"(?:(?:this is|it['’]?s|very) )*(?:{words})", re.IGNORECASE))
    for name, words in (("urgent", r"urgent|asap"), ("high", r"high[- ]?prio(?:rity)?"), ("low", r"low[- ]?prio(?:rity)?"))
)
ADD_PAGE_TITLE = "Your whole text"
# An agent's title says in 3 to 4 words what the item is about ("Fix recurring reminders"); it heads the item's
# step, whose ask is the decision's question (SPEC SESSION-3). Cockpit's own items and the user's keep theirs.
AGENT_TITLE_WORDS = 5
AGENT_TITLE_CHARS = 40
# Cockpit's own work is the items whose project is this; the page tags each of them and their steps Cockpit (SPEC
# UI-43). A put stores the other spellings sessions use, and the tool's old name, as this one: any case, with any
# whitespace around it (the migration trims the same characters).
COCKPIT_PROJECT = "cockpit"
COCKPIT_PROJECT_SPELLINGS = frozenset({"cockpit", "desk"})
# Images the user attaches to an item: at most this many per item, each at most this big.
ITEM_IMAGES_MAX = 10
IMAGE_MAX_BYTES = 10 * 1024 * 1024
# The image types an item takes, each with a test of the file's first bytes.
# Cockpit serves the bytes back under the stored type, so they must be that type;
# SVG is not among them, since it can carry script.
IMAGE_TYPES: dict[str, Callable[[bytes], bool]] = {
    "image/png": lambda content: content.startswith(b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": lambda content: content.startswith(b"\xff\xd8\xff"),
    "image/gif": lambda content: content.startswith((b"GIF87a", b"GIF89a")),
    "image/webp": lambda content: content[:4] == b"RIFF" and content[8:12] == b"WEBP",
}
IMAGE_FIELDS = "id, item_id, content_type, size, created_at, added_by"
# HTML pages sessions share on an item (SPEC API-3): at most this many per item, each at most this big.
ITEM_PAGES_MAX = 10
PAGE_MAX_BYTES = 10 * 1024 * 1024
PAGE_FIELDS = "id, item_id, title, size, created_at, updated_at, revision, added_by"
NEW_SESSION_NAME_CHARS = 60
# The sweeper's session has this name (sweeper.py starts it); the page and the sweep leave it out of the work.
SWEEP_SESSION_NAME = "Cockpit sweep"
# Sweep sessions from before the rename from Desk keep their old name.
SWEEP_SESSION_NAMES = frozenset({SWEEP_SESSION_NAME, "Desk sweep"})
BRIEFING_SESSION_NAME = "Cockpit briefing"

# Item fields that are the user's judgement. Once the user sets one on the page, agent
# upserts leave it alone and report it in `kept`. Stage, summary and the rest
# are facts the sweep reads from the sources, so agents keep updating them;
# only an item the user added keeps its summary, the user's own words (add_item).
USER_ITEM_FIELDS = frozenset({"priority", "goal_id", "blocks", "archived"})
# Step actions only the user may take; agents may close (done, dismiss), withdraw,
# yield a place to a stronger step, or return a step the user handed them.
USER_ACTIONS = frozenset({"reopen", "snooze", "take"})
AGENT_ACTIONS = frozenset({"done", "dismiss", "withdraw", "yield", "return"})
# The step the user is on is the step whose timer runs (SPEC NEXT-16): what an agent does to it waits until the
# timer stops (`release_held`).
HELD_ACTIONS = frozenset({"done", "dismiss", "withdraw", "yield"})
# The fields the user reads on a step; a put that carries one records a check.
STEP_TEXT_FIELDS = frozenset(
    {"ask", "why", "body", "details", "draft", "answers", "command", "permission", "closes", "reply_hint"}
)
# What a step reverts to when an agent puts a withdrawn step again.
STEP_DEFAULTS: dict[str, Any] = {
    "session_id": None,
    "why": "",
    "body": "",
    "details": [],
    "draft": "",
    "reply_hint": "",
    "score": 50,
    "reasons": [],
    "size": None,
    "progress": None,
    "answers": [],
    "command": None,
    "permission": None,
    "closes": None,
    "early": False,
}
# One-click buttons (SPEC NEXT-21, NEXT-18): at most four answers of one short
# line each (`{text, closes}`; one that closes completes the item, SPEC CONTROL-15), and a command of at most this many characters.
ANSWERS_MAX = 4
ANSWER_CHARS = 80
# A step whose need starts with one of these asks the user to let the session act, so it offers that action as an
# answer ("Merge it") that is not a closing one (SPEC NEXT-21): without it the user cannot tell which press merges.
ACTION_NEED = re.compile(r"(?:merge|approve)\b")
# A merge step stays with its session after the merge, which follows the change through dev, staging and prod and
# checks it runs, so none of its answers closes the item; "I merged it on GitHub" hands it off.
MERGE_NEED = re.compile(r"merge\b")
# A move Cockpit recommends (moves.py, SPEC SESSION-18). Only Cockpit sets its answers, whose `action` the page
# carries out itself.
MOVE_NEED = re.compile(r"^move:(?P<session_id>[0-9a-f-]{36}):(?P<account>[a-z]+):")
COMMAND_CHARS = 2000
# A command comes with what it does, part by part, in plain words, shown above it (SPEC NEXT-40): the user reads
# that before pressing Run rather than the shell.
COMMAND_EXPLAIN_CHARS = 1200
# An agent's message may not carry a line that reads like one of Cockpit's sender headers (delivery.prefixed):
# the user's or an agent's, under Cockpit's old name Desk included (sessions with the old skill still trust
# that one), or Cockpit's own.
FORGED_HEADER = re.compile(r"(?im)^[^\w\n]*message from (?:.*?\bvia (?:cockpit|desk)\b|cockpit itself\b)")


def defused(text: str) -> str:
    """Agent-written text with each line that looks like a sender header marked, so it cannot pass for one where
    Cockpit forwards it inside a message: a command's output, a step's ask and body."""
    return FORGED_HEADER.sub(lambda match: "[quoted] " + match.group(), text)


# Open steps the queue can show: not closed, not withdrawn.
OPEN = "s.status = 'open' AND s.withdrawn_at IS NULL"
# A message that started a new session for an item whose UUID Cockpit did not find
# (delivery.NEW_SESSION_LOOKUP_SECONDS), so the item is not linked to it. Messages are `m`.
UNLINKED_NEW = "m.via = 'new' AND m.state = 'sent' AND m.session_id IS NULL AND m.item_id IS NOT NULL"
# A message starting a new session for an item that may still start it: waiting or on its way, or failed with
# "Delivery uncertain" and not set aside, so the session may have started (SPEC MSG-5). Messages are `m`.
STARTING_NEW = (
    "m.new_cwd IS NOT NULL AND m.item_id IS NOT NULL AND (m.state IN ('queued', 'sending')"
    " OR (m.state = 'failed' AND m.dismissed_at IS NULL AND m.detail LIKE 'Delivery uncertain%'))"
)
# An open item the user added with Add whose session did not start: not done, no session, no
# open step, no one it waits on, and no session Cockpit started or may be starting for it
# without linking it, which would otherwise get a second one. Add starts a session for each
# to-do (SPEC CONTROL-8), so this lists only starts that failed; the sweeper starts
# those. Items are `i`.
UNTRIAGED = (
    "i.todo = 1 AND i.archived = 0 AND i.stage IS NOT 'done'"
    " AND json_array_length(i.sessions) = 0 AND json_array_length(i.waiting_on) = 0"
    f" AND NOT EXISTS (SELECT 1 FROM steps s WHERE s.item_id = i.id AND {OPEN})"
    f" AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.item_id = i.id AND (({UNLINKED_NEW}) OR ({STARTING_NEW})))"
)
# Feedback on a part of the spec, of a skill or of the page names it with one of these.
FEEDBACK_TARGET_PREFIXES = ("spec:", "skill:", "page:")
# The user sees at most this many steps. Further ready steps wait as "ready next",
# and the strongest by rank takes a place as soon as one frees.
QUEUE_LIMIT = 4
# The most time the user adds to an item at once (SPEC TRACK-7): a day, well past any session the timers missed.
ADD_LIMIT = 24 * 3600
# The user reads a step cold, so its ask and why stay short, and the body answers
# The user's questions under bold headings (SPEC NEXT-3); all three keep commit
# hashes and session ids out; those belong in its details. Limits count the
# visible text: link labels count, URLs do not. Quoted lines (`>`) are another
# person's words, such as the Slack message a reply answers, shown in full
# (SPEC NEXT-24): they count toward no limit and may hold ids.
TEXT_LIMITS = {"ask": 120, "why": 160, "body": 1800, "reply_hint": 80}
# Collapsed sections a step may carry, and the visible characters in each, counted as
# above: few and short enough that the user can read the whole step (SPEC NEXT-3).
DETAILS_LIMIT = 3
DETAIL_CHARS = 2500
MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
BARE_URL = re.compile(r"https?://\S+", re.IGNORECASE)
HEX_ID = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")
QUOTED_LINE = re.compile(r"(?m)^ {0,3}>.*\n?")


class Conflict(Exception):
    """The request clashes with the current state (HTTP 409)."""


class NotFound(Exception):
    """The row does not exist (HTTP 404)."""


class Invalid(Exception):
    """The request is well-formed but cannot be carried out (HTTP 400)."""


class Forbidden(Exception):
    """This caller may not do this (HTTP 403)."""


class TooLarge(Exception):
    """The request body is over its limit (HTTP 413)."""


ITEM_JSON = ("waiting_on", "sessions", "links", "user_fields")
STEP_JSON = ("details", "reasons", "answers", "command", "permission", "closes")


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _encode(fields: dict[str, Any], json_columns: tuple[str, ...]) -> dict[str, Any]:
    encoded = {}
    for name, value in fields.items():
        if name in json_columns:
            encoded[name] = json.dumps(value)
        elif isinstance(value, bool):
            encoded[name] = int(value)
        else:
            encoded[name] = value
    return encoded


def get_item(conn: sqlite3.Connection, item_id: int) -> dict[str, Any]:
    item = db.row(conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone())
    if item is None:
        raise NotFound(f"item {item_id} does not exist")
    return item


def get_step(conn: sqlite3.Connection, step_id: int) -> dict[str, Any]:
    step = db.row(conn.execute("SELECT * FROM steps WHERE id = ?", (step_id,)).fetchone())
    if step is None:
        raise NotFound(f"step {step_id} does not exist")
    return step


def other_open_steps(conn: sqlite3.Connection, session_id: str, step_id: int) -> int:
    """How many steps besides this one on the user's page target the session, by their own session or by
    their item's first one, as `target_session` picks."""
    return conn.execute(
        "SELECT COUNT(*) FROM steps s JOIN items i ON i.id = s.item_id"
        f" WHERE {OPEN} AND i.archived = 0 AND s.id != ?"
        " AND COALESCE(s.session_id, json_extract(i.sessions, '$[0]')) = ?",
        (step_id, session_id),
    ).fetchone()[0]


def close_refusal(conn: sqlite3.Connection, session: Session) -> str | None:
    """Why Cockpit keeps a session when the sweeper closes it (delivery.Deliverer.close), or None. A turn under way, a dialog,
    a subagent, an open step or a message on its way means a live session is not done; background commands and
    scheduled prompts end with the stop, and the sweeper weighs them when it judges. A stopped session with an open
    step is not done either: closing it would keep the usage limit's reset from waking it (limits.ended_on_purpose)."""
    refusal = stop_refusal(conn, session) if session.alive else None
    if refusal is not None:
        return refusal
    steps = other_open_steps(conn, session.session_id, 0)
    if steps:
        return f"{steps} open step{'s' if steps > 1 else ''} on Cockpit go{'' if steps > 1 else 'es'} to it"
    return None


def stop_refusal(conn: sqlite3.Connection, session: Session) -> str | None:
    """Why stopping a live session's process now would cut something off (Cockpit's idle stop, delivery.idle_stop),
    or None. An open step needs no process: the user's answer resumes the session (SPEC SESSION-15)."""
    if session.name in SWEEP_SESSION_NAMES:
        return "the sweeper closes other sessions, not itself"
    if not session.job_state:
        return "it is not a background session"
    if session.waits_on_dialog:
        return "a dialog in its terminal waits for an answer"
    if session.working_now:
        return "its turn is under way"
    if session.runs_subagent:
        return "a subagent it started still runs"
    if conn.execute(
        "SELECT 1 FROM messages WHERE session_id = ? AND state IN ('queued', 'sending')", (session.session_id,)
    ).fetchone():
        return "a message to it waits or is being sent"
    return None


def check_agent_title(fields: dict[str, Any], actor: str) -> None:
    """Refuse an agent's item write whose title is past the limit (SPEC SESSION-3). The routes check it as the
    write arrives, so an archive held while the user was on its step replays as it was accepted."""
    if not actor.startswith("agent:") or "title" not in fields:
        return
    title = fields["title"]
    words = len(title.split())
    if words > AGENT_TITLE_WORDS or len(title) > AGENT_TITLE_CHARS:
        raise Invalid(
            f"Title {title!r} has {words} words and {len(title)} characters. The limit is {AGENT_TITLE_WORDS} words and"
            f" {AGENT_TITLE_CHARS} characters. Name what the item is about in 3 to 4 words, such as"
            " \"Fix recurring reminders\"."
        )


# Words a session may use for the user in `waiting_on`, besides the user's name.
USER_WORDS = frozenset({"you", "user", "the user", "me"})


def check_agent_waiting(fields: dict[str, Any], actor: str) -> None:
    """Refuse an agent's item write that names the user as a person the item waits on (SPEC STATE-1). The user's
    part is a step: an entry with no step behind it made the item read as waiting on the user while their queue was
    empty . The routes check it as the write arrives, like `check_agent_title`."""
    if not actor.startswith("agent:") or not fields.get("waiting_on"):
        return
    name = user_name().casefold()
    names = {name, name.split()[0]} | USER_WORDS
    for entry in fields["waiting_on"]:
        # The person, without a note after it: "Sam: review of app#10" and "Sam (review)" name Sam, "Sam and Ana" does not.
        who = re.split(r"\s*[:(]|\s+[-–—]\s+", entry["who"].strip(), maxsplit=1)[0].strip().casefold()
        if entry["kind"] == "person" and who in names:
            raise Invalid(
                f"waiting_on names {entry['who']!r}, the user. Put {user_name()}'s part in a step with `cockpit step"
                f" put`, and leave {user_name()} out of waiting_on."
            )


def _check_goal(conn: sqlite3.Connection, fields: dict[str, Any]) -> None:
    goal_id = fields.get("goal_id")
    if goal_id is None:
        return
    goal = conn.execute("SELECT dropped_at FROM goals WHERE id = ?", (goal_id,)).fetchone()
    if goal is None:
        raise Invalid(f"goal {goal_id} does not exist")
    if goal["dropped_at"]:
        raise Invalid(f"goal {goal_id} was dropped")


def upsert_item(conn: sqlite3.Connection, fields: dict[str, Any], actor: str) -> dict[str, Any]:
    """Create or update the item with `fields["key"]`; only the given fields change.

    Fields the user set on the page (USER_ITEM_FIELDS) stay as the user set them when an
    agent writes; the response lists the ones it left alone in `kept`. On an item the user
    added with Add, whose summary is the user's text, an agent's summary goes to
    `agent_summary`, the owning session's latest status that the card shows beside it
    (SPEC VIEW-16). Only the user closes a task (SPEC CONTROL-17): an agent's put that would
    archive the item or move it to stage done leaves both as they are, and the user finishes it
    on the finish step the agent put first (`_ask_to_finish`), whose id the response gives in
    `finish_step`; without an open one the put is refused (`_require_finish_step`, SPEC
    CONTROL-19). `note` becomes a merged close's note or a returned finish step's check note; a
    note on any other put is refused. An agent's put
    that leaves shipped work with no next move is refused until the agent puts the finish step (`_refuse_untended`,
    SPEC STATE-5); `finish_how` says which close asked. An agent's move to stage done of an item whose PR merged closes it with
    no finish step (`_merged_pull`, SPEC CONTROL-15): the response names the PR in `merged`, and the user's next
    step shows a line on it (`merged_closes`). So does an agent's move to stage done, with a note, of a to-do the
    user added as fire and forget (SPEC CONTROL-24): the response's `alone` is true. `pinned` is
    the user's alone (SPEC CONTROL-13): it sets `pinned_at`, which keeps the time of the
    first pin while the item stays pinned.
    """
    now = db.now_iso()
    note = fields.get("note", "")
    merged = _merged_pull(conn, fields, actor)
    with db.transaction(conn):
        _check_goal(conn, fields)  # here: asking GitHub above takes seconds, and the user can drop the goal meanwhile
        existing = db.row(conn.execute("SELECT * FROM items WHERE key = ?", (fields["key"],)).fetchone())
        # Only a put that leaves shipped work with no next move is refused (SPEC STATE-5): one that finds it so goes
        # through, so a finish step the user dismissed is not asked for again on every put.
        untended = existing is not None and _shipped_untended(conn, existing, None)
        unwoken_before = existing is not None and _unwoken(existing) is not None
        changes = {name: value for name, value in fields.items() if name not in ("key", "note", "pinned")}
        if (changes.get("project") or "").strip().casefold() in COCKPIT_PROJECT_SPELLINGS:
            changes["project"] = COCKPIT_PROJECT
        elif (changes.get("project") or "").strip():
            # Every task is in a project that exists (SPEC CONTROL-26): a session decides between the projects
            # and a new one it creates, rather than a new spelling starting one by accident.
            found = projects.resolve(conn, changes["project"])
            if found is None:
                raise projects.unknown(conn, changes["project"])
            changes["project"] = found
        elif "project" in changes:
            changes["project"] = ""
        if "pinned" in fields:
            if actor != "user":
                raise Forbidden(f"only {user_name()} pins or unpins an item")
            changes["pinned_at"] = ((existing and existing["pinned_at"]) or now) if fields["pinned"] else None
        kept: list[str] = []
        closing = bool(changes.get("archived")) or changes.get("stage") == "done"
        finishes = None
        alone = False
        if actor != "user":
            # What the agent's close would have done: the user decides it on the finish step. A task the user
            # already finished (stage done) or closed asks nothing more, and keeps its stage: a close that also
            # names a stage does not bring it back.
            if existing and (existing["archived"] or existing["stage"] == "done"):
                if closing:
                    changes.pop("stage", None)
            elif changes.get("archived"):
                finishes = "close"
            elif changes.get("stage") == "done":
                finishes = "complete"
            # The PR GitHub confirmed must still be the item's: a put in between may have changed its links.
            if (
                finishes == "complete" and merged is not None and existing is not None
                and _pull_of(existing, fields) == merged and _merge_closes(conn, existing)
            ):
                finishes = None
            else:
                merged = None
                # A fire-and-forget to-do's session closes it itself (SPEC CONTROL-24), on the terms a merge does.
                if (
                    finishes == "complete" and existing is not None and existing["fire_and_forget"]
                    and _merge_closes(conn, existing)
                ):
                    if not note.strip():
                        raise Invalid(FORGOTTEN_NOTE.format(user=user_name()))
                    finishes = None
                    alone = True
            if finishes is not None:
                _require_finish_step(conn, existing)
            if changes.get("archived"):
                del changes["archived"]
            if changes.get("stage") == "done" and merged is None and not alone:
                del changes["stage"]
            if existing is not None and existing["stage"] == "done" and "stage" in changes:
                # Moving a finished task to another stage reopens it (SPEC CONTROL-27); a close put, whose stage
                # Cockpit drops above, and a put of its summary or links do not.
                refuse_on_closed_item(existing, actor, f"This put would move it back to stage {changes['stage']}.")
        if note and not closing:
            raise Invalid("a note goes only with a put that archives the item or moves it to stage done")
        if existing is not None and actor != "user":
            # The user's words on an item they added stay; the session's own line on it is its status beside them.
            if "summary" in changes and "summary" in existing["user_fields"]:
                changes["agent_summary"] = changes.pop("summary")
            kept = sorted(
                name for name in changes if name in existing["user_fields"] and changes[name] != existing[name]
            )
            changes = {name: value for name, value in changes.items() if name not in existing["user_fields"]}
        if actor == "user":
            chosen = set(existing["user_fields"] if existing else []) | (changes.keys() & USER_ITEM_FIELDS)
            edited = sorted(changes.keys())
            changes["user_fields"] = sorted(chosen)
        values = _encode(changes, ITEM_JSON) | {"updated_at": now, "updated_by": actor}
        if changes.get("stage") == "done" and not (existing and existing["stage"] == "done"):
            values["done_at"] = now
        if changes.get("archived") and not (existing and existing["archived"]):
            values |= {"archived_at": now, "archived_by": actor}
        if existing is None:
            if fields["key"].startswith("todo-"):
                raise NotFound(f"no item with key {fields['key']}; only {user_name()}'s Add makes a todo- item")
            if not fields.get("title"):
                raise Invalid("a new item needs a title")
            values |= {"key": fields["key"], "created_at": now}
            columns = ", ".join(values)
            marks = ", ".join("?" for _ in values)
            item_id = conn.execute(f"INSERT INTO items ({columns}) VALUES ({marks})", tuple(values.values())).lastrowid
        else:
            item_id = existing["id"]
            assignments = ", ".join(f"{name} = ?" for name in values)
            conn.execute(f"UPDATE items SET {assignments} WHERE id = ?", (*values.values(), item_id))
        if actor == "user":
            # Lowering the priority sets the item aside, which the sweeper reads (SPEC RANK-14).
            before = existing["priority"] if existing else None
            lowered = before is not None and PRIORITY_ORDER.index(changes.get("priority", before)) < PRIORITY_ORDER.index(before)
            # An edit of `archived` keeps its value: the close count reads the user's latest one (closed_by_user).
            archived = {"archived": bool(changes["archived"])} if "archived" in changes else {}
            patterns.record(conn, "item.edited", actor, item_id, None, None, {"fields": edited, "aside": lowered} | archived)
        if merged is not None:
            _close_by_agent(conn, item_id, "item.merged", {"pull": merged, "note": note}, actor, note, now)  # ty: ignore[invalid-argument-type]
        elif alone:
            _close_by_agent(conn, item_id, "item.finished_alone", {"note": note}, actor, note, now)  # ty: ignore[invalid-argument-type]
        item = get_item(conn, item_id)  # ty: ignore[invalid-argument-type]
        if finishes is None and actor.startswith("agent:") and not untended and _shipped_untended(conn, item, None):
            what = f"This put leaves new item {item['key']}" if existing is None else f"This put leaves item {item_id}"
            _refuse_untended(conn, item, what, existing is None, None)
        unwoken = _unwoken(item) if actor.startswith("agent:") and finishes is None else None
        if unwoken is not None and ("waiting_on" in fields or not unwoken_before):
            raise Invalid(UNWOKEN.format(what=unwoken))
        finish_step = _ask_to_finish(conn, item, actor, note, now) if finishes else None
        # The user's close in All work ends the timer on the item's step, as Close item does (SPEC CONTROL-17).
        archived_now = item["archived"] and not (existing and existing["archived"])
        on = running_step(conn) if archived_now and actor == "user" else None
        stopped = on is not None and get_step(conn, on)["item_id"] == item_id and _stop_timer(conn, on, "dismiss", now) > 0
        if archived_now:
            _drop_away(conn, item_id)  # ty: ignore[invalid-argument-type]
        # Archiving or restoring an item changes which steps count for the queue.
        if archived_now and actor == "user":
            # Archiving leaves the steps' fields as they were, so they say which ones showed in the queue.
            steps = db.rows(conn.execute("SELECT * FROM steps WHERE item_id = ? AND status = 'open'", (item_id,)).fetchall())
            moment = datetime.now(UTC)
            _resort_after_close(conn, moment, any(_in_queue(other, moment) for other in steps))
        else:
            moment = datetime.now(UTC)
            if existing and existing["archived"] and not item["archived"]:
                restored = [row[0] for row in conn.execute(
                    "SELECT id FROM steps WHERE item_id = ? AND status = 'open'", (item_id,)
                )]
                archived_at = conn.execute(
                    "SELECT MAX(at) FROM events WHERE item_id = ? AND kind = 'item.edited'"
                    " AND json_extract(data, '$.archived') = 1", (item_id,)
                ).fetchone()[0]
                left = rank.parse_time(archived_at or existing["updated_at"])
                assert left is not None
                _give_back_places(conn, restored, left, moment)
            fill_queue(conn, moment)
        if finish_step:  # a new or returned finish step takes a place by its score, as a step put does
            _swap_places(conn, datetime.now(UTC))
    if stopped:
        release_held(conn)
    return get_item(conn, item_id) | {  # ty: ignore[invalid-argument-type]
        "kept": kept, "held": None, "finish_step": finish_step, "finish_how": finishes if finish_step else None,
        "merged": merged, "alone": alone,
    }


# Why an agent's close of an item with no open finish step is refused (SPEC CONTROL-19).
NO_FINISH_STEP = (
    "{first} a step with need {need}, with what {user} asked and when, what was done and how it was"
    " checked, what is left and who has it, and a closing answer. That step asks {user} to close the task, and the"
    " item keeps the stage its work is in. A close put is needed only for a merged PR, which closes its item with no"
    " finish step when GitHub confirms the merge and no other step is open on the item."
)


# The events of an agent's close that asks no finish step (`_close_by_agent`), as an SQL list.
AGENT_CLOSES = "('item.merged', 'item.finished_alone')"

# Why an agent's close of a fire-and-forget to-do without a note is refused (SPEC CONTROL-24).
FORGOTTEN_NOTE = (
    "This to-do is fire and forget: a put with stage done closes it with no finish step, and {user} reads only its"
    ' note, in one line on their next step. Add a "note" saying what was done and where it is.'
)


def _require_finish_step(conn: sqlite3.Connection, item: dict[str, Any] | None) -> None:
    """An agent's close needs a finish step open on the item: the session that closes a task writes it, checked
    like any step, rather than Cockpit filling a form around its note (SPEC CONTROL-19). The refusal names the need
    to put: a withdrawn finish step's, to write it again, or else the first one free."""
    steps = [] if item is None else db.rows(
        conn.execute("SELECT need, status, withdrawn_at FROM steps WHERE item_id = ? ORDER BY id", (item["id"],)).fetchall()
    )
    finishing = [step for step in steps if rank.finishing(step)]
    if any(step["status"] == "open" and not step["withdrawn_at"] for step in finishing):
        return
    need = _finish_need(finishing)
    first = ("No finish step is open on this item. Put it first:" if item is not None else
             "This item does not exist yet, so it has no finish step. Put the item first at the stage its work is"
             " in, then")
    raise Invalid(NO_FINISH_STEP.format(first=first, need=need, user=user_name()))


def _check_closes(conn: sqlite3.Connection, item_id: int, closes: list[dict[str, Any]]) -> None:
    """A step's list of tasks to close (SPEC CONTROL-23) names open tasks, each once and with why it is done. The
    step's own item is not on it: its finish step closes it."""
    listed: set[int] = set()
    for entry in closes:
        other = entry["item_id"]
        if other == item_id:
            raise Invalid(f"the list names the step's own item {item_id}; its finish step closes it")
        if other in listed:
            raise Invalid(f"the list names item {other} twice")
        listed.add(other)
        if not entry["why"].strip():
            raise Invalid(f"item {other} needs a why: what shows the task is done")
        try:
            _check_step_text({"body": entry["why"]})
        except Invalid as error:
            raise Invalid(f"the why for item {other} becomes its finish step's body: {error}") from None
        item = get_item(conn, other)
        if item["archived"] or item["stage"] == "done":
            raise Invalid(f"item {other} is already {'closed' if item['archived'] else 'complete'}")


def finish_step_for(conn: sqlite3.Connection, item_id: int, why: str, actor: str) -> int:
    """The step a task on a list to close (SPEC CONTROL-23) finishes from: its open finish step, or else a new one
    that `actor`, the session that listed it, writes from its why. The finish and its Undo then keep a step as Task
    done's do. A task closed or completed since the list, also by a merge (CONTROL-15), is refused."""
    item = get_item(conn, item_id)
    if item["archived"] or item["stage"] == "done":
        raise Conflict(f"item {item_id} is already {'closed' if item['archived'] else 'complete'}")
    steps = db.rows(conn.execute("SELECT id, need, status, withdrawn_at FROM steps WHERE item_id = ? ORDER BY id", (item_id,)).fetchall())
    finishing = [step for step in steps if rank.finishing(step)]
    ready = next((step["id"] for step in finishing if step["status"] == "open" and not step["withdrawn_at"]), None)
    if ready is not None:
        return ready
    need = _finish_need(finishing)
    fields = {"item_id": item_id, "need": need, "ask": "Is this task done?", "body": why}
    return upsert_step(conn, fields, actor)["step"]["id"]


def _finish_need(finishing: list[dict[str, Any]]) -> str:
    """The need an agent puts an item's finish step on, given its finish steps: a withdrawn one's, to write it
    again, or else the first one free."""
    withdrawn = next((step["need"] for step in finishing if step["status"] == "open"), None)
    return withdrawn or _free_finish_need({step["need"] for step in finishing})


def _free_finish_need(taken: set[str]) -> str:
    """The first finish need an item has free: complete, then complete:2 and on (a closed need stays closed)."""
    return next(need for need in ("complete", *(f"complete:{n}" for n in range(2, len(taken) + 2))) if need not in taken)


def _merged_pull(conn: sqlite3.Connection, fields: dict[str, Any], actor: str) -> str | None:
    """The PR of the item, as `owner/repo#number`, when an agent's put moves the open item to stage done and GitHub
    reports the PR merged: a merge needs no finish step from the user (SPEC CONTROL-15)."""
    if actor == "user" or fields.get("stage") != "done" or fields.get("archived"):
        return None
    existing = db.row(conn.execute("SELECT * FROM items WHERE key = ?", (fields["key"],)).fetchone())
    if existing is None or existing["archived"] or existing["stage"] == "done":
        return None
    ref = _pull_of(existing, fields)
    if ref is None:
        return None
    try:
        return ref if github.pull_merged(ref) else None
    except github.GitHubError as exc:
        log.error("could not ask GitHub whether %s merged, so item %s gets a finish step: %s", ref, existing["id"], exc)
        return None


def _pull_of(item: dict[str, Any], fields: dict[str, Any]) -> str | None:
    """The PR of the item as the put leaves it: the put's links replace the item's."""
    return github.pull_ref(item | ({"links": fields["links"]} if "links" in fields else {}))


def _merge_closes(conn: sqlite3.Connection, item: dict[str, Any]) -> bool:
    """Whether the merge of the item's PR closes it now: no step but a finish step is open on it, the user handed
    none of those back to an agent (what they asked may go beyond the merge, SPEC CONTROL-15), and the user is not
    on one of its steps. Otherwise the user finishes it on a finish step, which shows what is still open."""
    open_steps = db.rows(
        conn.execute("SELECT * FROM steps WHERE item_id = ? AND status = 'open' AND withdrawn_at IS NULL", (item["id"],)).fetchall()
    )
    on = running_step(conn)
    return all(
        rank.finishing(step) and step["handoff_message_id"] is None and step["id"] != on
        for step in open_steps
    )


def _close_by_agent(
    conn: sqlite3.Connection, item_id: int, kind: str, data: dict[str, Any], actor: str, note: str, now: str
) -> None:
    """Record an agent's close that asks no finish step: an `item.merged` event for an item whose PR merged (SPEC
    CONTROL-15), an `item.finished_alone` event for a fire-and-forget to-do (CONTROL-24). Also a closure, which the
    item's row in Finished this week names (`how_done`); the user's next step names the event (`merged_closes`). Its
    finish steps close as done."""
    _record_closure(conn, "item", item_id, item_id, "stage done", actor, now, note)
    patterns.record(conn, kind, actor, item_id, None, None, data)
    _drop_away(conn, item_id)
    finishing = [
        step for step in db.rows(conn.execute("SELECT * FROM steps WHERE item_id = ? AND status = 'open'", (item_id,)).fetchall())
        if rank.finishing(step)
    ]
    for step in finishing:
        conn.execute(
            "UPDATE steps SET status = 'done', closed_at = ?, handoff_message_id = NULL, withdrawn_at = NULL,"
            " queued_at = NULL, updated_at = ?, updated_by = ? WHERE id = ?",
            (now, now, actor, step["id"]),
        )
        _record_closure(conn, "step", step["id"], item_id, "done", actor, now, note)
        patterns.record_step(conn, "done", actor, get_step(conn, step["id"]))


def _shipped_untended(conn: sqlite3.Connection, item: dict[str, Any], closing: int | None) -> bool:
    """Shipped work with no next move (SPEC STATE-5): the item is on prod, open, with nothing in `waiting_on` and no
    open step but step `closing`, which an agent is closing. A step whose close or withdraw is held until the user's
    timer stops counts as closed, so the close of another step that would leave none is refused now, not that held
    close when it replays. An agent's put or close that leaves it so is refused
    (`_refuse_untended`), so it does not sit under "no one has the next move" until a sweep."""
    if item["stage"] != "prod" or item["archived"] or item["waiting_on"]:
        return False
    return conn.execute(
        f"SELECT 1 FROM steps s WHERE s.item_id = ? AND s.id IS NOT ? AND {OPEN} AND NOT EXISTS (SELECT 1 FROM"
        " held_changes h WHERE h.step_id = s.id AND h.kind = 'step_action'"
        " AND json_extract(h.data, '$.action') IN ('done', 'dismiss', 'withdraw'))",
        (item["id"], closing),
    ).fetchone() is None


def _unwoken(item: dict[str, Any]) -> str | None:
    """Why the open item would wait with nothing to wake it (SPEC STATE-8), or None. Each wait on a bot, a person or a
    release says when to look again (`until`); Cockpit wakes the item's session then, or sooner when the wait's
    check holds (wakes.py). An agent's own session is its work's waker, and stall detection notices when it stops
    (SPEC SWEEP-20)."""
    if item["archived"] or item["stage"] == "done":
        return None
    bare = [entry for entry in item["waiting_on"] if entry["kind"] != "agent" and not entry.get("until")]
    return f"waiting_on has {bare[0]['kind']} {bare[0]['who']!r} with no until" if bare else None


# Why an agent's put that leaves an item waiting with nothing to wake it is refused (SPEC STATE-8).
UNWOKEN = (
    "Nothing would wake this item again: {what}. Give each bot, person or release in waiting_on an until, the time"
    " Cockpit wakes the item's session to look again, and, where Cockpit can check it, a check that wakes it sooner,"
    ' such as {{"kind": "reached", "stage": "prod", "pulls": ["<PR url>"]}} for a release.'
)


# Why an agent's put or close that leaves shipped work with no next move is refused (SPEC STATE-5).
UNTENDED = (
    "{what} on prod with nothing in waiting_on and no open step, so no one would have its next move. {first} its"
    " finish step on need {need}{session}, with an ask that names the change in plain words, where {user} sees it,"
    " how you checked it, and a closing answer. {then} When work is left, put a step or waiting_on that holds it instead."
)
# The why of a returned finish step whose reply is one sentence.
REPLIED_WHY = "{who} replied to your message here. Your press closes the task."
# Where the first sentence of an agent's note ends: at a full stop, question or exclamation mark before a space, or
# at a line break.
SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\s*\n\s*")


def _refuse_untended(
    conn: sqlite3.Connection, item: dict[str, Any], what: str, new: bool, holder: str | None
) -> None:
    """An agent's put or close that would leave shipped work with no next move is refused until the agent puts the
    item's finish step (SPEC STATE-5): the session that shipped the work knows what changed, and Cockpit only has its
    status to quote. Raised inside the caller's transaction, so nothing of the put or close is kept. `holder` is the
    session a step being closed is handed to: its finish step closes that step too (`last_handed_step`), so nothing
    is left to do again."""
    finishing = [
        step for step in db.rows(conn.execute("SELECT need, status FROM steps WHERE item_id = ? ORDER BY id", (item["id"],)).fetchall())
        if rank.finishing(step)
    ]
    first = "Put the item first at the stage before prod, then put" if new else "First put"
    raise Invalid(UNTENDED.format(
        what=what, first=first, need=_finish_need(finishing), user=user_name(),
        session=f" with session_id {holder}" if holder else "",
        then="That one put closes this handed step too." if holder else "Then do this again.",
    ))


def _clip(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _task_lines(item: dict[str, Any]) -> list[str]:
    """Where the task stands, the line a finish step Cockpit writes opens with, so the user recalls the task without
    opening its history . The task's title heads every step already."""
    stands = _stands(item)
    return [f"**Where it stands:** {_clip(stands, 300)}"] if stands else []


def _stands(item: dict[str, Any]) -> str:
    """Where the task stands in the session's words, or "" when that only repeats the title."""
    # On a to-do the user added, the summary is the user's own words and the session's status is kept apart.
    stands = ((item["agent_summary"] if _asked_by_user(item) else item["summary"]) or "").strip()
    return "" if stands == item["title"] else stands


def _returned_finish_text(item: dict[str, Any], who: str, note: str, asked: dict[str, Any]) -> dict[str, str]:
    """The ask, why and body of a finish step the user handed an agent, which the agent gives back with its reply
    (`note`): the user's message and the reply first, then where the task stands, so the user finds the
    answer on the step they asked on (SPEC NEXT-37). The ask is the reply's
    first sentence, what was done ("PR renamed"), and the why the rest of it."""
    sentences = SENTENCE_END.split(note.strip(), maxsplit=1)
    rest = sentences[1] if len(sentences) > 1 else ""
    ask = _clip(sentences[0].removesuffix("."), TEXT_LIMITS["ask"])
    why = _clip(f"{who}: {rest}", TEXT_LIMITS["why"]) if rest else REPLIED_WHY.format(who=who)
    # The reply shows whole in the body only when the ask and why had to cut it; otherwise the step would say it
    # twice.
    whole = ask == sentences[0].removesuffix(".") and (not rest or why == f"{who}: {rest}")
    lines = _reply_lines(asked, who, note)
    return {"ask": ask, "why": why, "body": "\n\n".join([*(lines[:-1] if whole else lines), *_task_lines(item)])}


def _reply_lines(asked: dict[str, Any], who: str, note: str) -> list[str]:
    """The first lines of a step an agent gives back: the user's message on it, then the agent's reply. A hand-off
    by Run or Allow is Cockpit's message, which the user did not write, so only the reply shows."""
    reply = f"**{who}'s reply:** {_clip(note, 800)}"
    if asked["author"] != "user":
        return [reply]
    moment = rank.parse_time(asked["created_at"])
    when = f" on {moment.day} {moment:%b}" if moment else ""
    return [f"**You wrote{when}:** {_clip(asked['text'], 300)}", reply]


def _answer_on_step(conn: sqlite3.Connection, step: dict[str, Any], actor: str, note: str, now: str) -> None:
    """An agent gives back a step the user handed it: its reply to the user's message goes first on that step
    (SPEC NEXT-37), inside the caller's transaction. A finish step is written again from the item and the reply
    (`_returned_finish_text`), whose note `_require_done_note` asks for: an agent's rewrite of it since the hand-off
    says what was still left, which the reply now answers. Another step that
    an agent wrote again since the hand-off keeps that text; one left as it was opens with the message and the note,
    and without a note it stays with the agent, since its text answers nothing yet."""
    asked = get_message(conn, step["handoff_message_id"])
    who = actor.removeprefix("agent:")
    if rank.finishing(step):
        text = _returned_finish_text(get_item(conn, step["item_id"]), who, note, asked)
    elif conn.execute(
        "SELECT 1 FROM step_versions WHERE step_id = ? AND actor != 'user' AND id > ?",
        (step["id"], asked["step_version"] or 0),
    ).fetchone():
        return
    elif not note.strip():
        words = asked["text"] if len(asked["text"]) <= 200 else asked["text"][:199] + "…"
        raise Invalid(
            f"{user_name()} handed you step {step['id']} with this message: \"{words}\". The step still reads as"
            " before. Put the answer on it first with `cockpit step put` on the same item and need: its first lines"
            f" answer each question plainly, and its ask is what {user_name()} decides now. Then return it. Or return"
            f" it with --note \"<your reply>\"; Cockpit puts your reply first on the step, under {user_name()}'s"
            " message."
        )
    else:
        text = {"body": "\n\n".join([*_reply_lines(asked, who, note), step["body"]]).rstrip()}
    assignments = ", ".join(f"{name} = ?" for name in text)
    conn.execute(f"UPDATE steps SET {assignments} WHERE id = ?", (*text.values(), step["id"]))
    history.record_version(conn, step["id"], actor, now)


def _require_done_note(conn: sqlite3.Connection, step: dict[str, Any], note: str) -> None:
    """An agent gives back a finish step the user handed it with a message only with a note on what came of that
    message, which the user reads on the step as its check note (SPEC CONTROL-15)."""
    if not note.strip():
        raise Invalid(_keep_handed_finish(conn, step))


def _keep_handed_finish(conn: sqlite3.Connection, step: dict[str, Any]) -> str:
    """Why an agent keeps a finish step the user handed it until all the user's message asked is done."""
    asked = get_message(conn, step["handoff_message_id"])["text"]
    words = asked if len(asked) <= 200 else asked[:199] + "…"
    return (
        f"Step {step['id']} asks {user_name()} to finish item {step['item_id']}. {user_name()} handed it to you with"
        f" this message: \"{words}\". Give it back once all of that is done, with a note that says what was done,"
        " such as --note \"The Tasks CSV export now includes completed tasks. The export check passed.\""
        " While part of it is pending,"
        " such as a PR to merge and ship or a review, keep the step. Rewrite its ask as what is left and who holds"
        " it, and set the item's waiting_on."
    )


def _ask_to_finish(conn: sqlite3.Connection, item: dict[str, Any], actor: str, note: str, now: str) -> int:
    """An agent would have closed the item: the user finishes it on the item's open finish step instead (SPEC
    CONTROL-17), which `upsert_item` makes sure is there (SPEC CONTROL-19). It stays as it is, unless the user handed
    it to an agent ("not done yet"): then it comes back to the user, with the note as its check note, which an
    agent's close must give (`_require_done_note`). Returns the step's id."""
    shown = next(
        step for step in db.rows(conn.execute(
            "SELECT * FROM steps WHERE item_id = ? AND status = 'open' AND withdrawn_at IS NULL ORDER BY id", (item["id"],)
        ).fetchall())
        if rank.finishing(step)
    )
    if shown["handoff_message_id"] is not None:
        _require_done_note(conn, shown, note)
        _answer_on_step(conn, shown, actor, note, now)
        conn.execute(
            "UPDATE steps SET handoff_message_id = NULL, checked_at = ?, checked_by = ?, check_note = ?,"
            " updated_at = ?, updated_by = ? WHERE id = ?",
            (now, actor, note, now, actor, shown["id"]),
        )
        patterns.record_step(conn, "return", actor, get_step(conn, shown["id"]))
    return shown["id"]


def item_by_key(conn: sqlite3.Connection, key: str) -> dict[str, Any]:
    """The item with every step it ever had (open, closed, withdrawn), so an agent sees which needs are taken, and
    the user's messages on it, so a session that closes a task it did not work on can say what the user asked
    (SPEC CONTROL-19)."""
    item = db.row(conn.execute("SELECT * FROM items WHERE key = ?", (key,)).fetchone())
    if item is None:
        raise NotFound(f"no item with key {key}")
    steps = db.rows(conn.execute("SELECT * FROM steps WHERE item_id = ? ORDER BY id", (item["id"],)).fetchall())
    messages = db.rows(conn.execute(
        "SELECT id, step_id, step_ask, text, created_at FROM messages WHERE item_id = ? AND author = 'user' ORDER BY id",
        (item["id"],),
    ).fetchall())
    return _with_attachments(conn, [item])[0] | {"steps": steps, "messages": messages}


def patch_item(conn: sqlite3.Connection, item_id: int, fields: dict[str, Any], actor: str) -> dict[str, Any]:
    key = get_item(conn, item_id)["key"]
    return upsert_item(conn, fields | {"key": key}, actor)


def untriaged_ids(conn: sqlite3.Connection) -> set[int]:
    """The items the user added that wait for the sweeper (UNTRIAGED)."""
    return {record["id"] for record in conn.execute(f"SELECT i.id FROM items i WHERE {UNTRIAGED}").fetchall()}


def unlinked_warnings(conn: sqlite3.Connection) -> list[str]:
    """A warning for each item, not archived and still without a session, for which Cockpit
    started a session whose UUID it did not find (UNLINKED_NEW)."""
    records = conn.execute(
        "SELECT m.id, m.item_id, m.detail, i.title FROM messages m JOIN items i ON i.id = m.item_id"
        f" WHERE {UNLINKED_NEW} AND i.archived = 0 AND json_array_length(i.sessions) = 0 ORDER BY m.id"
    ).fetchall()
    return [
        f"Item {record['item_id']} ({record['title']}) has no session. Message {record['id']}: {record['detail'].rstrip('.')}."
        " Find the session's UUID with `claude agents` and put it in the item's sessions, or archive the item."
        for record in records
    ]


def clip_title(text: str) -> str:
    """The title Add gives a to-do: the text's first line, clipped to TITLE_CHARS."""
    first_line = text.split("\n", 1)[0].strip()
    return first_line if len(first_line) <= TITLE_CHARS else first_line[: TITLE_CHARS - 1] + "…"


def add_item(
    conn: sqlite3.Connection, text: str, part: dict[str, str] | None, pinned: bool, start: dict[str, str | None],
    fire_and_forget: bool,
) -> dict[str, Any]:
    """The user adds an item with Add: a to-do the sweeper picks up (SPEC CONTROL-8, SESSION-5).

    The user's text is the summary, which stays as the user wrote it (it is in
    `user_fields`); its first line, clipped, is the title, which the sweeper may
    rename. A part of the page the user attached ({target, quote}, SPEC CONTROL-9)
    ends the summary on a line of its own. A text past ADD_SUMMARY_CHARS goes
    whole on the item as a page, and the summary keeps its start with a line
    that names the page. A `pinned` to-do starts in the Pinned
    list (SPEC CONTROL-13). "Urgent", "high priority" or "low priority" in the
    text sets the item's priority, as the user's own edit would (SPEC RANK-17).
    `start` holds the provider, model, effort and account the user picked for
    its session (SPEC SESSION-22), each None for the default; the session
    started for it gets them (create_message). A `fire_and_forget` to-do's
    session works to the end on its own and closes it itself (SPEC CONTROL-24).
    The key is `todo-<id>`, a form agents' keys cannot take. Returns the item as
    `state.items` shows it.
    """
    text = text.strip()
    if not text:
        raise Invalid("the item's text is empty")
    if len(text) > ADD_MAX_CHARS:
        raise Invalid(f"the item's text is over {ADD_MAX_CHARS:,} characters")
    clauses = [" ".join(clause.split()) for clause in ADD_CLAUSE_BREAK.split(text)]
    priority = next(
        (name for name, words in ADD_PRIORITY_WORDS if any(words.fullmatch(clause) for clause in clauses)), None
    )
    title = clip_title(text)
    attached = ""
    if part is not None:
        quote = " ".join(part["quote"].split())
        attached = f"\n\nAttached part of the page ({part['target']})" + (f': "{quote}"' if quote else ", which shows no text")
    provider, options = start_options(start["provider"], {key: start[key] for key in START_OPTION_KEYS})
    kept = {"provider": provider, **(options or dict.fromkeys(START_OPTION_KEYS))}
    now = db.now_iso()
    with db.transaction(conn):
        item_id = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM items").fetchone()[0]
        conn.execute(
            "INSERT INTO items (id, key, title, summary, todo, fire_and_forget, user_fields, pinned_at, created_at,"
            " updated_at, updated_by) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, 'user')",
            (item_id, f"todo-{item_id}", title, text + attached, int(fire_and_forget), json.dumps(["summary"]),
             now if pinned else None, now, now),
        )
        if any(kept.values()):
            conn.execute("UPDATE items SET start_options = ? WHERE id = ?", (json.dumps(kept), item_id))
        if priority is not None:
            conn.execute(
                "UPDATE items SET priority = ?, user_fields = ? WHERE id = ?",
                (priority, json.dumps(["summary", "priority"]), item_id),
            )
        if len(text) > ADD_SUMMARY_CHARS:
            html = _text_page(text)
            page_id = conn.execute(
                "INSERT INTO item_pages (item_id, title, html, size, created_at, updated_at, added_by)"
                " VALUES (?, ?, ?, ?, ?, ?, 'user')",
                (item_id, ADD_PAGE_TITLE, html, _page_size(html), now, now),
            ).lastrowid
            summary = (
                f"{_clip_at_break(text, ADD_SUMMARY_CHARS)}…\n\n[The text continues on this item's page"
                f' "{ADD_PAGE_TITLE}", {len(text):,} characters in all. Read it with `cockpit page get {page_id}'
                ' --out FILE`.]'
            )
            conn.execute("UPDATE items SET summary = ? WHERE id = ?", (summary + attached, item_id))
        patterns.record(conn, "item.added", "user", item_id, None, None, {})
        item: dict[str, Any] = _on_board(get_item(conn, item_id), {}, set(), set(), set(), set(), set(), {}, untriaged_ids(conn), {}, {}) | {"step_sessions": [], "stalled_since": None, "ended": None}
        item |= work_group(item, {}, {}, False, datetime.now(UTC))
        refs = mention_refs(conn)
        item |= {"summary_html": mentions_html(item["summary"], refs), "agent_summary_html": mentions_html(item["agent_summary"], refs)}
        return _with_attachments(conn, [item])[0]


def fork(
    conn: sqlite3.Connection, item_id: int, session_id: str, cwd: str, text: str, checkouts_dir: Path
) -> dict[str, Any]:
    """Fork (SPEC MSG-11): the user starts a new line of work from the conversation of item `item_id`'s session
    `session_id`, which works in `cwd`. The words become a to-do item, as Add makes one (`add_item`), on the same
    project; its session starts as a writable fork of that session (delivery.Deliverer._fork). Nothing goes back
    to the first session, and the step the user wrote on stays as it was. The fork runs on the first session's
    model and effort, which the delivery records on the message once it reads them . Returns the item
    and the queued message that starts the fork."""
    with db.transaction(conn):
        project = get_item(conn, item_id)["project"]
        item = add_item(conn, text, None, False, dict.fromkeys(("provider", *START_OPTION_KEYS)), False)
        conn.execute("UPDATE items SET project = ? WHERE id = ?", (project, item["id"]))
        message = create_message(
            conn, "user", text, {"item_id": item["id"], "new_cwd": cwd, "fork_of": session_id}, checkouts_dir
        )
    return {"item": item | {"project": project}, "message": message}


def add_more(conn: sqlite3.Connection, item_id: int, text: str, checkouts_dir: Path) -> dict[str, Any]:
    """The user adds words to a to-do they added, such as one sent before they finished writing (SPEC CONTROL-18).
    The words join the end of the item's text and go to its session. While the session is still starting, they
    join its first message, so it reads them together; with no session and no first message, the sweeper starts
    the session on the whole text. A first message still starting its session raises Conflict, as
    `add_follow_up` does. Returns the item and the message that carries the words, or None."""
    text = text.strip()
    if not text:
        raise Invalid("the words to add are empty")
    with db.transaction(conn):
        item = get_item(conn, item_id)
        if not item["key"].startswith("todo-"):
            raise Invalid(f"item {item_id} is not one you added; write to its session instead")
        conn.execute(
            "UPDATE items SET summary = summary || ?, updated_at = ?, updated_by = 'user' WHERE id = ?",
            (f"\n\n{text}", db.now_iso(), item_id),
        )
        session_id = target_session(conn, item_id, None)
        first = conn.execute(
            "SELECT id FROM messages WHERE item_id = ? AND author = 'user' AND new_cwd IS NOT NULL"
            " AND state IN ('queued', 'sending') ORDER BY id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        if session_id is not None:
            target = {"item_id": item_id, "step_id": None, "session_id": session_id, "new_cwd": None}
            message = create_message(conn, "user", f"More for this to-do:\n\n{text}", target, checkouts_dir)
        elif first is not None:
            message = add_follow_up(conn, first[0], text, checkouts_dir)
        else:
            message = None
        reopen_for_message(conn, item_id)
        patterns.record(conn, "item.added_more", "user", item_id, None, None, {"message_id": message and message["id"]})
    return {"item": get_item(conn, item_id), "message": message}


def _clip_at_break(text: str, limit: int) -> str:
    """The start of `text`, at most `limit` characters, ending at its last line break, or else its last space,
    in the final fifth."""
    start = text[:limit]
    for mark in ("\n", " "):
        cut = start.rfind(mark)
        if cut > limit * 0.8:
            return start[:cut].rstrip()
    return start


def _text_page(text: str) -> str:
    """The user's added text as a page of its own, shown as written."""
    return (
        '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        f"<title>{ADD_PAGE_TITLE}</title><style>:root{{color-scheme:light dark}}"
        "body{margin:0;padding:16px;background:Canvas;color:CanvasText}"
        "pre{margin:0;white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.5 ui-monospace,monospace}</style></head>"
        f"<body><pre>{html_escape(text, quote=False)}</pre></body></html>"
    )


def _with_attachments(conn: sqlite3.Connection, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each item with its images' and pages' metadata in upload order, never their content."""
    images: dict[int, list[dict[str, Any]]] = {item["id"]: [] for item in items}
    pages: dict[int, list[dict[str, Any]]] = {item["id"]: [] for item in items}
    marks = ", ".join("?" for _ in images)
    for image in conn.execute(
        f"SELECT {IMAGE_FIELDS} FROM item_images WHERE item_id IN ({marks}) ORDER BY id", list(images)
    ).fetchall():
        images[image["item_id"]].append(
            {key: image[key] for key in ("id", "content_type", "size", "created_at", "added_by")}
        )
    for page in conn.execute(
        f"SELECT {PAGE_FIELDS} FROM item_pages WHERE item_id IN ({marks}) ORDER BY id", list(pages)
    ).fetchall():
        pages[page["item_id"]].append(
            {key: page[key] for key in ("id", "title", "size", "created_at", "updated_at", "added_by")}
        )
    return [item | {"images": images[item["id"]], "pages": pages[item["id"]]} for item in items]


def _image_info(conn: sqlite3.Connection, image_id: int) -> dict[str, Any]:
    image = db.row(conn.execute(f"SELECT {IMAGE_FIELDS} FROM item_images WHERE id = ?", (image_id,)).fetchone())
    if image is None:
        raise NotFound(f"image {image_id} does not exist")
    return image


def item_image(conn: sqlite3.Connection, image_id: int) -> dict[str, Any]:
    """One image with its bytes in `content`, to serve under its `content_type`."""
    image = db.row(conn.execute("SELECT * FROM item_images WHERE id = ?", (image_id,)).fetchone())
    if image is None:
        raise NotFound(f"image {image_id} does not exist")
    return image


def image_type(content: bytes) -> str | None:
    """The IMAGE_TYPES media type whose first bytes `content` starts with, or None."""
    return next((media_type for media_type, matches in IMAGE_TYPES.items() if matches(content)), None)


def add_item_image(
    conn: sqlite3.Connection, item_id: int, content_type: str, content: bytes, actor: str
) -> dict[str, Any]:
    """The user or an agent attaches an image to an item, e.g. a screenshot or a graph.

    `content_type` is the request's header; its media type must be one of
    IMAGE_TYPES and the bytes must start the way that type does. The image
    records `actor` as `added_by`. Returns its metadata, without the bytes.
    """
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type not in IMAGE_TYPES:
        raise Invalid(
            f"Content-Type {media_type or '(none)'} is not an image type Cockpit keeps; send one of {', '.join(IMAGE_TYPES)}"
        )
    if len(content) > IMAGE_MAX_BYTES:
        raise TooLarge(f"the image is over {IMAGE_MAX_BYTES // (1024 * 1024)} MB, the most Cockpit keeps")
    if not IMAGE_TYPES[media_type](content):
        raise Invalid(f"the body is not an {media_type} file: its first bytes do not match that type")
    with db.transaction(conn):
        get_item(conn, item_id)
        count = conn.execute("SELECT COUNT(*) FROM item_images WHERE item_id = ?", (item_id,)).fetchone()[0]
        if count >= ITEM_IMAGES_MAX:
            raise Conflict(f"item {item_id} already has {ITEM_IMAGES_MAX} images, the most it holds; remove one first")
        cursor = conn.execute(
            "INSERT INTO item_images (item_id, content_type, size, content, created_at, added_by)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (item_id, media_type, len(content), content, db.now_iso(), actor),
        )
        patterns.record(conn, "item.image_added", actor, item_id, None, None, {"image_id": cursor.lastrowid})
        return _image_info(conn, cursor.lastrowid)  # ty: ignore[invalid-argument-type]


def remove_item_image(conn: sqlite3.Connection, image_id: int, actor: str) -> dict[str, Any]:
    """The user removes any image from an item; an agent only one it added. Returns the removed image's metadata."""
    with db.transaction(conn):
        image = _image_info(conn, image_id)
        if actor != "user" and image["added_by"] != actor:
            raise Forbidden(
                f"image {image_id} was added by {image['added_by']}; an agent can remove only images it added"
            )
        conn.execute("DELETE FROM item_images WHERE id = ?", (image_id,))
        patterns.record(conn, "item.image_removed", actor, image["item_id"], None, None, {"image_id": image_id})
    return image


def _page_info(conn: sqlite3.Connection, page_id: int) -> dict[str, Any]:
    page = db.row(conn.execute(f"SELECT {PAGE_FIELDS} FROM item_pages WHERE id = ?", (page_id,)).fetchone())
    if page is None:
        raise NotFound(f"page {page_id} does not exist")
    return page


def item_page(conn: sqlite3.Connection, page_id: int) -> dict[str, Any]:
    """One page with its HTML in `html`."""
    page = db.row(conn.execute("SELECT * FROM item_pages WHERE id = ?", (page_id,)).fetchone())
    if page is None:
        raise NotFound(f"page {page_id} does not exist")
    return page


def on_all_work(item: dict[str, Any], now: datetime) -> bool:
    """Whether All work shows the item: open, or done in the last DONE_RECENT_DAYS, or pinned however long ago it
    was done (SPEC CONTROL-13). A later put on a done item moves its `updated_at`, not when it was done."""
    if item["archived"]:
        return False
    done_at = item["done_at"] or item["updated_at"]
    return item["stage"] != "done" or done_at >= _iso(now - timedelta(days=DONE_RECENT_DAYS)) or bool(item["pinned_at"])


CLOSED_FOUND_MAX = 50


# A search word that names an item by its id, as a mention's link searches for it (UI-74).
ITEM_ID_WORD = re.compile(r"#\d+")


def find_closed(conn: sqlite3.Connection, query: str, now: datetime) -> list[dict[str, Any]]:
    """The closed items whose title, summary, key or project hold every word of `query` (a leading # selects an item ID), for search with Include
    closed (SPEC VIEW-21): archived ones, and done ones All work no longer shows, the latest change first, at most
    CLOSED_FOUND_MAX. Each in the shape of Finished this week's entries (`_closed_recently`), with `ended` for a
    done one, so the page shows how it ended the same way."""
    words = query.lower().split()
    if not words:
        return []
    text = "lower(title || ' ' || summary || ' ' || agent_summary || ' ' || key || ' ' || project)"

    def matching(word: str) -> str:
        return "'#' || id = ?" if ITEM_ID_WORD.fullmatch(word) else f"instr({text}, ?) > 0"

    rows = db.rows(conn.execute(
        "SELECT id, key, title, project, stage, archived, archived_at, archived_by, updated_at, updated_by, pinned_at, done_at FROM items"
        f" WHERE (archived = 1 OR stage = 'done') AND {' AND '.join(map(matching, words))}"
        " ORDER BY updated_at DESC, id DESC",
        words,
    ).fetchall())
    found = [row for row in rows if not on_all_work(row, now)][:CLOSED_FOUND_MAX]
    ended = how_done(conn, [row["id"] for row in found if not row["archived"]])
    return [
        {
            "id": row["id"], "key": row["key"], "title": row["title"], "project": row["project"], "stage": row["stage"],
            "action": "archived" if row["archived"] else "stage done",
            "closed_at": row["archived_at"] if row["archived"] else row["done_at"] or row["updated_at"],
            "closed_by": row["archived_by"] if row["archived"] else row["updated_by"], "note": "", "ended": ended.get(row["id"]),
            "done_at": row["done_at"], "updated_at": row["updated_at"],
        }
        for row in found
    ]


def all_pages(conn: sqlite3.Connection, now: datetime) -> list[dict[str, Any]]:
    """Every shared page with its item's title, stage and state, last changed first, for the Pages tab (SPEC API-4).
    `item_on_board` says whether All work shows the item, so the tab links to it only then; `item_has_session` whether
    comments can go to the item's session. Metadata only, never the HTML."""
    pages = db.rows(conn.execute(
        "SELECT p.id, p.title, p.size, p.created_at, p.updated_at, p.revision, p.added_by, p.item_id,"
        " i.title AS item_title, i.project AS item_project, i.stage AS item_stage, i.archived AS item_archived, i.sessions != '[]' AS item_has_session,"
        " i.updated_at AS item_updated_at, i.done_at AS item_done_at, i.pinned_at AS item_pinned_at"
        " FROM item_pages p JOIN items i ON i.id = p.item_id ORDER BY p.updated_at DESC, p.id DESC"
    ).fetchall())
    for page in pages:
        item = {"archived": page["item_archived"], "stage": page["item_stage"], "updated_at": page.pop("item_updated_at"),
                "done_at": page.pop("item_done_at"), "pinned_at": page.pop("item_pinned_at")}
        page["item_on_board"] = on_all_work(item, now)
        page["item_has_session"] = bool(page["item_has_session"])
    return pages


def _page_size(html: str) -> int:
    size = len(html.encode())
    if size > PAGE_MAX_BYTES:
        raise TooLarge(f"the page is over {PAGE_MAX_BYTES // (1024 * 1024)} MB, the most Cockpit keeps")
    return size


def add_item_page(conn: sqlite3.Connection, item_id: int, title: str, html: str, actor: str) -> dict[str, Any]:
    """An agent or the user shares an HTML page on an item, e.g. a report or a plain-words explainer (SPEC API-3).
    Returns the page's metadata."""
    size = _page_size(html)
    with db.transaction(conn):
        get_item(conn, item_id)
        count = conn.execute("SELECT COUNT(*) FROM item_pages WHERE item_id = ?", (item_id,)).fetchone()[0]
        if count >= ITEM_PAGES_MAX:
            raise Conflict(
                f"item {item_id} already has {ITEM_PAGES_MAX} pages, the most it holds; update or remove one first"
            )
        now = db.now_iso()
        cursor = conn.execute(
            "INSERT INTO item_pages (item_id, title, html, size, created_at, updated_at, added_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (item_id, title, html, size, now, now, actor),
        )
        patterns.record(conn, "item.page_added", actor, item_id, None, None, {"page_id": cursor.lastrowid})
        return _page_info(conn, cursor.lastrowid)  # ty: ignore[invalid-argument-type]


def _check_page_owner(page: dict[str, Any], actor: str, verb: str) -> None:
    if actor != "user" and page["added_by"] != actor:
        raise Forbidden(f"page {page['id']} was added by {page['added_by']}; an agent can {verb} only pages it added")


def update_item_page(conn: sqlite3.Connection, page_id: int, title: str, html: str, actor: str) -> dict[str, Any]:
    """The agent that added a page replaces its title and HTML; the page keeps its id, so links to it stay good."""
    size = _page_size(html)
    with db.transaction(conn):
        page = _page_info(conn, page_id)
        _check_page_owner(page, actor, "update")
        conn.execute(
            "UPDATE item_pages SET title = ?, html = ?, size = ?, updated_at = ?, revision = revision + 1 WHERE id = ?",
            (title, html, size, db.now_iso(), page_id),
        )
        patterns.record(conn, "item.page_updated", actor, page["item_id"], None, None, {"page_id": page_id})
        return _page_info(conn, page_id)


def remove_item_page(conn: sqlite3.Connection, page_id: int, actor: str) -> dict[str, Any]:
    """The user removes any page from an item; an agent only one it added. Returns the removed page's metadata."""
    with db.transaction(conn):
        page = _page_info(conn, page_id)
        _check_page_owner(page, actor, "remove")
        conn.execute("DELETE FROM item_pages WHERE id = ?", (page_id,))
        patterns.record(conn, "item.page_removed", actor, page["item_id"], None, None, {"page_id": page_id})
    return page


PAGE_COMMENT_CHARS = 4000

# A page comment's step takes new comments while the user still answers it: open, and not with an agent.
STEP_TAKES_COMMENTS = "s.status = 'open' AND s.withdrawn_at IS NULL AND s.handoff_message_id IS NULL"


def page_comments(conn: sqlite3.Connection, page_id: int) -> list[dict[str, Any]]:
    """Every comment the user left on a page (SPEC NEXT-38), oldest first. `step_open` says whether an unsent
    comment's step still takes it; one whose step closed goes only with Add as new item."""
    _page_info(conn, page_id)
    return db.rows(conn.execute(
        f"SELECT c.*, COALESCE({STEP_TAKES_COMMENTS}, 0) AS step_open FROM page_comments c"
        " LEFT JOIN steps s ON s.id = c.step_id WHERE c.page_id = ? ORDER BY c.id",
        (page_id,),
    ).fetchall())


def unsent_step_comments(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The unsent page comments waiting for the user's answer to an open step, with their page's title, for the
    step cards: those comments go with the answer (SPEC NEXT-38)."""
    return db.rows(conn.execute(
        "SELECT c.id, c.page_id, c.step_id, c.anchor, c.text, p.title AS page_title FROM page_comments c"
        f" JOIN item_pages p ON p.id = c.page_id JOIN steps s ON s.id = c.step_id"
        f" WHERE c.state = 'unsent' AND {STEP_TAKES_COMMENTS} ORDER BY c.id"
    ).fetchall())


def add_page_comment(
    conn: sqlite3.Connection, page_id: int, revision: int, step_id: int | None, anchor: dict[str, str], text: str
) -> dict[str, Any]:
    """The user comments on words or a part of a page's `revision`, the one their screen shows (SPEC NEXT-38). With
    `step_id` the comment waits to go with the answer to that step, which must still take it."""
    text = text.strip()
    if not text:
        raise Invalid("the comment is empty")
    with db.transaction(conn):
        page = _page_info(conn, page_id)
        if page["revision"] != revision:
            raise Conflict(f"page {page_id} changed since it was opened; it shows the new version now")
        if step_id is not None:
            check_open_for_hand_off(get_step(conn, step_id))
        now = db.now_iso()
        cursor = conn.execute(
            "INSERT INTO page_comments (page_id, revision, step_id, anchor, text, state, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, 'unsent', ?, ?)",
            (page_id, revision, step_id, json.dumps(anchor), text, now, now),
        )
        return _page_comment(conn, cursor.lastrowid)  # ty: ignore[invalid-argument-type]


def _page_comment(conn: sqlite3.Connection, comment_id: int) -> dict[str, Any]:
    comment = db.row(conn.execute(
        f"SELECT c.*, COALESCE({STEP_TAKES_COMMENTS}, 0) AS step_open FROM page_comments c"
        " LEFT JOIN steps s ON s.id = c.step_id WHERE c.id = ?",
        (comment_id,),
    ).fetchone())
    if comment is None:
        raise NotFound(f"page comment {comment_id} does not exist")
    return comment


def remove_page_comment(conn: sqlite3.Connection, comment_id: int) -> dict[str, Any]:
    """The user deletes a comment that has not gone anywhere yet. Returns it."""
    with db.transaction(conn):
        comment = _page_comment(conn, comment_id)
        if comment["state"] != "unsent":
            raise Conflict(f"page comment {comment_id} was already {comment['state']}; it stays")
        conn.execute("DELETE FROM page_comments WHERE id = ?", (comment_id,))
    return comment


# The line that follows the user's words in a hand-off carrying page comments: "Sam's 2 page comments on this step:".
COMMENTS_HEADER_END = "on this step:"


def _own_words(message: dict[str, Any]) -> str:
    """A hand-off message's text without the page comments and choices `hand_off` added after the user's words
    (`page_answers`): what Take back puts back in the reply box, as the comments and choices themselves wait again,
    and where Add more joins its words (SPEC NEXT-38, NEXT-48)."""
    added = message["page_answers"]
    text = message["text"]
    if not added or not text.endswith(added):
        return text
    return text[: len(text) - len(added)].removesuffix("\n\n")


def _one_line(text: str) -> str:
    """A page's title, words or path on one line. The page's scripts can propose anything, and an agent names the
    title, so none can start a line that passes for a sender header in a message."""
    return " ".join(text.split())


def _comment_lines(conn: sqlite3.Connection, comments: list[dict[str, Any]]) -> str:
    """Page comments as text a session reads: per page its title, id and version, then each comment on its words
    or part, numbered."""
    pages: dict[int, list[dict[str, Any]]] = {}
    for comment in comments:
        pages.setdefault(comment["page_id"], []).append(comment)
    blocks = []
    for page_id, on_page in pages.items():
        page = _page_info(conn, page_id)
        lines = [
            f'On the page "{_one_line(page["title"])}" (page {page_id}, version {page["revision"]};'
            f" `cockpit page get {page_id} --out FILE` saves it):"
        ]
        for number, comment in enumerate(on_page, 1):
            anchor = comment["anchor"]
            if anchor["kind"] == "text":
                on = f'the words "{_one_line(anchor["quote"])}"'
            else:
                shown = _clip(_one_line(anchor["text"]), 200)
                on = f"the part `{_one_line(anchor['path'])}`" + (f' showing "{shown}"' if shown else "")
            older = f" (left on version {comment['revision']})" if comment["revision"] != page["revision"] else ""
            lines.append(f"{number}. On {on}{older}: {comment['text']}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def add_page_comments_item(conn: sqlite3.Connection, page_id: int, comment_ids: list[int]) -> dict[str, Any]:
    """Add as new item (SPEC NEXT-38): the user opens unsent comments on a page as a to-do of its own, as Add does.
    The comments are marked added to the item in the same transaction. Returns the item as `add_item` does."""
    if not comment_ids:
        raise Invalid("no page comments given")
    with db.transaction(conn):
        page = _page_info(conn, page_id)
        comments = [_page_comment(conn, comment_id) for comment_id in dict.fromkeys(comment_ids)]
        for comment in comments:
            if comment["page_id"] != page_id:
                raise Invalid(f"page comment {comment['id']} is on page {comment['page_id']}, not page {page_id}")
            if comment["state"] != "unsent":
                raise Conflict(f"page comment {comment['id']} was already {comment['state']}")
        body = (
            f'Comments on the page "{_one_line(page["title"])}"\n\n'
            f"{user_name()} chose to open these page comments as a separate item.\n\n{_comment_lines(conn, comments)}"
        )
        item = add_item(conn, body, None, False, {"provider": None, **dict.fromkeys(START_OPTION_KEYS)}, False)
        conn.execute(
            f"UPDATE page_comments SET state = 'added', item_id = ?, updated_at = ?"
            f" WHERE id IN ({','.join('?' * len(comments))})",
            (item["id"], db.now_iso(), *[comment["id"] for comment in comments]),
        )
    return item


def send_page_comments(
    conn: sqlite3.Connection, page_id: int, comment_ids: list[int], text: str, checkouts_dir: Path
) -> dict[str, Any]:
    """Send to the item's session (SPEC NEXT-38): unsent comments on a page that no open step takes go, after the
    user's words, as a message to the primary session of the page's item, and are marked sent in the same
    transaction. Returns the queued message."""
    if not comment_ids:
        raise Invalid("no page comments given")
    with db.transaction(conn):
        page = _page_info(conn, page_id)
        comments = [_page_comment(conn, comment_id) for comment_id in dict.fromkeys(comment_ids)]
        for comment in comments:
            if comment["page_id"] != page_id:
                raise Invalid(f"page comment {comment['id']} is on page {comment['page_id']}, not page {page_id}")
            if comment["state"] != "unsent":
                raise Conflict(f"page comment {comment['id']} was already {comment['state']}")
            if comment["step_open"]:
                raise Conflict(f"page comment {comment['id']} goes with step {comment['step_id']}'s answer")
        count = f"{len(comments)} page comment{'s' if len(comments) > 1 else ''}"
        body = "\n\n".join(part for part in (text.strip(), f"{user_name()}'s {count}:", _comment_lines(conn, comments)) if part)
        target = {"item_id": page["item_id"], "step_id": None, "session_id": None, "new_cwd": None}
        message = create_message(conn, "user", body, target, checkouts_dir)
        reopened = reopen_for_message(conn, page["item_id"])
        conn.execute(
            f"UPDATE page_comments SET state = 'sent', message_id = ?, updated_at = ?"
            f" WHERE id IN ({','.join('?' * len(comments))})",
            (message["id"], db.now_iso(), *[comment["id"] for comment in comments]),
        )
    return message | {"reopened": reopened}


# Choices on shared pages (SPEC NEXT-48): a page's named form controls (radio buttons, checkboxes, selects, text
# boxes) are choices the user makes on the page. Each is saved as the user makes it, and the session reads them with
# `cockpit page choices`; a choice made while a step had the page open also goes with the user's next answer to it.
CHOICE_NAME_MAX = 200
CHOICE_VALUE_MAX = 2000
CHOICE_VALUES_MAX = 100
CHOICES_PER_PAGE_MAX = 200
# The line that follows the user's words and page comments in a hand-off carrying choices.
CHOICES_HEADER_END = "on this item's pages:"
# A choice the user cleared, as a session reads it.
CLEARED_CHOICE = "(cleared: nothing chosen)"


def page_choices(conn: sqlite3.Connection, page_id: int) -> list[dict[str, Any]]:
    """The user's choices on a page, by control name (SPEC NEXT-48). `sent` says whether the choice went with an
    answer as it stands now."""
    _page_info(conn, page_id)
    return [
        choice | {"sent": choice["message_id"] is not None}
        for choice in db.rows(conn.execute(
            "SELECT * FROM page_choices WHERE page_id = ? ORDER BY name", (page_id,)
        ).fetchall())
    ]


def set_page_choice(
    conn: sqlite3.Connection, page_id: int, name: str, chosen: str | list[str] | None, label: str, step_id: int | None
) -> list[dict[str, Any]]:
    """The user chose on a page (SPEC NEXT-48): `chosen` is the control's value, a list for checkboxes and multiple
    selects, or None (or "" or []) when the user cleared it. A cleared choice is kept as one, so the page shows the
    control cleared when it opens again and the session reads that the user cleared it, where a control the page
    checked or filled in to start with would otherwise come back as it began . With `step_id`, the choice
    goes with the user's next answer to that step, which must still take it; without, the session reads it when it
    asks. Returns the page's choices."""
    name = name.strip()
    if not name or len(name) > CHOICE_NAME_MAX:
        raise Invalid(f"a choice needs a name of 1 to {CHOICE_NAME_MAX} characters")
    values = chosen if isinstance(chosen, list) else [] if chosen is None else [chosen]
    if len(values) > CHOICE_VALUES_MAX or any(len(value) > CHOICE_VALUE_MAX for value in values):
        raise Invalid(f"a choice holds at most {CHOICE_VALUES_MAX} values of {CHOICE_VALUE_MAX} characters")
    label = _clip(_one_line(label), CHOICE_VALUE_MAX)
    if chosen == "":
        chosen = None
    with db.transaction(conn):
        _page_info(conn, page_id)
        if step_id is not None:
            check_open_for_hand_off(get_step(conn, step_id))
        known = conn.execute(
            "SELECT COUNT(*) FROM page_choices WHERE page_id = ? AND name != ?", (page_id, name)
        ).fetchone()[0]
        if known >= CHOICES_PER_PAGE_MAX:
            raise Invalid(f"a page keeps at most {CHOICES_PER_PAGE_MAX} choices")
        conn.execute(
            "INSERT INTO page_choices (page_id, name, chosen, label, step_id, message_id, updated_at)"
            " VALUES (?, ?, ?, ?, ?, NULL, ?) ON CONFLICT (page_id, name) DO UPDATE SET chosen = excluded.chosen,"
            " label = excluded.label, step_id = excluded.step_id, message_id = NULL, updated_at = excluded.updated_at",
            (page_id, name, json.dumps(chosen), "" if chosen in (None, []) else label, step_id, db.now_iso()),
        )
    return page_choices(conn, page_id)


def unsent_step_choices(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The choices waiting to go with the user's answer to an open step, with their page's title, for the step
    cards (SPEC NEXT-48)."""
    return db.rows(conn.execute(
        "SELECT c.page_id, c.name, c.chosen, c.label, c.step_id, p.title AS page_title FROM page_choices c"
        " JOIN item_pages p ON p.id = c.page_id JOIN steps s ON s.id = c.step_id"
        f" WHERE c.message_id IS NULL AND {STEP_TAKES_COMMENTS} ORDER BY c.page_id, c.name"
    ).fetchall())


def _shown_choice(choice: dict[str, Any]) -> str:
    """A choice as a session reads it: its value, and the words its control shows where they say more, or that the
    user cleared it. The page's script sets both, so they stay on one line."""
    chosen = choice["chosen"]
    if chosen in (None, []):
        return CLEARED_CHOICE
    value = _one_line(", ".join(chosen) if isinstance(chosen, list) else chosen)
    label = choice["label"]
    return f"{value} ({label})" if label and label != value else value


def _choice_lines(conn: sqlite3.Connection, choices: list[dict[str, Any]]) -> str:
    """Choices as text a session reads: per page its title and id, then each control's name and value."""
    pages: dict[int, list[dict[str, Any]]] = {}
    for choice in choices:
        pages.setdefault(choice["page_id"], []).append(choice)
    blocks = []
    for page_id, on_page in pages.items():
        page = _page_info(conn, page_id)
        lines = [f'On the page "{_one_line(page["title"])}" (page {page_id}; `cockpit page choices {page_id}` lists them):']
        # The page's author names the controls, so a name can say anything; a line that starts like a sender
        # header is marked as quoted (pattern 8).
        lines += [defused(f"- {_one_line(choice['name'])}: {_shown_choice(choice)}") for choice in on_page]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def refuse_on_closed_item(item: dict[str, Any], actor: str, what: str) -> None:
    """An agent writes no new ask on a task the user finished or closed (SPEC CONTROL-27): a new step there reached
    the user's queue on an item that showed as done, and a stage put reopened it. A step still open on it, such as
    one the user handed over, is still answered on."""
    if not actor.startswith("agent:") or not (item["archived"] or item["stage"] == "done"):
        return
    how = "closed" if item["archived"] else "completed"
    raise Conflict(
        f"Item {item['id']} ({item['title']}) is closed: {user_name()} {how} it. {what} Put the result on a new"
        f" item, or on the item {user_name()} asked on, and link this one there if it helps. Only {user_name()}"
        " reopens a closed item."
    )


# What a refusal names as the session to put a finish step with, when a hand-off's new session has no id yet.
UNKNOWN_HOLDER = "<your session id>"
# Why a handed step whose message is not yet `sent` stays open on shipped work (SPEC STATE-5). A message being sent can
# still go back to waiting, so only `sent` counts as having reached the session. Delivery may have got through all the
# same, so the text says it is unconfirmed, and return is the way out when the state does not move.
UNREACHED = (
    "Cockpit has not confirmed that {user}'s message on step {step} reached its session, so a close or a finish step"
    " waits for now. Read the message when it arrives and do this again; if it failed to send or this refusal keeps"
    ' coming, give the step back with `cockpit step return {step} --note "<your reply>"`.'
)


def _handed_steps(conn: sqlite3.Connection, item_id: int) -> list[dict[str, Any]]:
    """The item's open steps that are with an agent, each with the session it is handed to."""
    return db.rows(conn.execute(
        f"SELECT s.id, s.item_id, s.need, m.session_id, m.state FROM steps s JOIN messages m ON m.id = s.handoff_message_id"
        f" WHERE s.item_id = ? AND {OPEN} ORDER BY s.id",
        (item_id,),
    ).fetchall())


def _asks_anew(conn: sqlite3.Connection, fields: dict[str, Any]) -> bool:
    """Whether the put makes a new ask: its need is free on the item, or its step was withdrawn."""
    existing = conn.execute(
        "SELECT withdrawn_at FROM steps WHERE item_id = ? AND need = ?", (fields["item_id"], fields["need"])
    ).fetchone()
    return existing is None or existing["withdrawn_at"] is not None


def _close_hint(conn: sqlite3.Connection, step: dict[str, Any]) -> str:
    """How the session holding a handed step that is not a finish step closes it, as the refused put's hint says."""
    if _shipped_untended(conn, get_item(conn, step["item_id"]), step["id"]):
        if step["state"] != "sent":
            return ". " + UNREACHED.format(user=user_name(), step=step["id"])
        return (
            f", or when nothing is left for {user_name()} on it, put the item's finish step with session_id"
            f" {step['session_id'] or UNKNOWN_HOLDER}: that one put closes step {step['id']} too"
        )
    return f", or close it when nothing is left for {user_name()} on it"


def last_handed_step(conn: sqlite3.Connection, fields: dict[str, Any], actor: str) -> int | None:
    """The step that an agent's put of a new finish step closes with it, or None. It is the step the put's session
    holds when closing it alone would be refused for leaving shipped work with no next move (SPEC STATE-5), the one
    case where the session cannot close first and put after. A finish step the user handed over stays answered
    on itself, and another session's step stays with that session. A hand-off still waiting to go stays open too, so
    Take back still works and the user's message is not overtaken; one that started a new session Cockpit could not
    name yet has no holder to compare, so the put's session counts as it."""
    if not actor.startswith("agent:") or not fields.get("session_id") or not rank.finishing(fields):
        return None
    if not _asks_anew(conn, fields):
        return None
    for step in _handed_steps(conn, fields["item_id"]):
        if (
            step["session_id"] in (fields["session_id"], None)
            and step["state"] == "sent"
            and not rank.finishing(step)
            and _shipped_untended(conn, get_item(conn, fields["item_id"]), step["id"])
        ):
            return step["id"]
    return None


def refuse_answer_elsewhere(conn: sqlite3.Connection, fields: dict[str, Any], actor: str) -> None:
    """An agent's put of a new step on an item while one of its steps is with an agent is refused: the user asked
    on that step, so the answer and any decision that follows go on it . The hint names the step handed to the
    put's session when there is one, else every handed step of the item. A finish step that closes the handed step
    with it (`last_handed_step`) goes through."""
    if actor == "user" or not _asks_anew(conn, fields) or last_handed_step(conn, fields, actor) is not None:
        return
    handed = _handed_steps(conn, fields["item_id"])
    if not handed:
        return
    own = [step for step in handed if fields.get("session_id") and step["session_id"] == fields["session_id"]]
    named = "; ".join(
        f"step {step['id']}: a put on need {step['need']}, then `cockpit step return {step['id']}"
        + (' --note "<your reply>"`' if rank.finishing(step) else f"`{_close_hint(conn, step)}")
        for step in own or handed
    )
    raise Conflict(
        f"{user_name()} handed a step on this item to an agent. The answer, and any decision that follows, goes on"
        f" that step, not on a new one. Answer on {named}"
    )


def upsert_step(conn: sqlite3.Connection, fields: dict[str, Any], actor: str) -> dict[str, Any]:
    """Create or update the step for (item, need). A closed step stays closed.

    Returns the step and, when nothing changed because the step was closed,
    `skipped` with the reason, so a sweep never reopens what the user finished or
    dismissed. A withdrawn step comes back as new: its fields start from
    STEP_DEFAULTS and its age from now. A new step waits as ready next and
    takes a place in the user's queue at once when one is free (`queued_at` says
    which), or by its score (`_stronger_first`). The text the user reads must fit
    TEXT_LIMITS. An agent's put on the step whose timer runs waits until the
    timer stops (`held` says so).
    """
    now = db.now_iso()
    item_id = fields["item_id"]
    with db.transaction(conn):
        # Read in the put's transaction, so a completion or close that lands first is the one it sees.
        item = get_item(conn, item_id)  # raises NotFound for an unknown item
        existing = db.row(
            conn.execute("SELECT * FROM steps WHERE item_id = ? AND need = ?", (item_id, fields["need"])).fetchone()
        )
        if existing is None or existing["withdrawn_at"] is not None or existing["status"] != "open":
            # A new ask; a step still open on the item, such as one the user handed over, is still answered on.
            refuse_on_closed_item(item, actor, "A new step here would reach their queue on a finished task.")
        if existing is not None and existing["status"] != "open":
            return {
                "step": existing,
                "skipped": f"step {existing['id']} was {existing['status']} by {existing['updated_by']}; "
                "use a new need for a new ask",
                "held": None,
            }
        sent = {name: value for name, value in fields.items() if name not in ("item_id", "need")}
        stamp = {"updated_at": now, "updated_by": actor}
        changed = {name for name, value in sent.items() if existing is None or existing[name] != value}
        if existing is None or existing["withdrawn_at"] is not None or changed - {"score", "reasons"}:
            # The sweeper's ranking of a step, also when it repeats the step's text, leaves who prepared it as it was.
            stamp |= {"prepared_at": now, "prepared_by": actor}
        if STEP_TEXT_FIELDS & sent.keys():
            # The skill has agents check live state just before they write a step's text.
            stamp |= {"checked_at": now, "checked_by": actor, "check_note": ""}
        if (existing is None or existing["withdrawn_at"] is not None) and not fields.get("ask"):
            raise Invalid("a new step needs an ask")
        _check_step_text(sent)
        if sent.get("closes"):
            _check_closes(conn, item_id, sent["closes"])
        if ACTION_NEED.match(fields["need"]) and (existing is None or existing["withdrawn_at"] or "answers" in sent):
            answers = sent.get("answers") or []
            if not any(not answer["closes"] for answer in answers):
                raise Invalid(
                    f"A {fields['need']!r} step offers its action as an answer that does not close, such as"
                    ' "Merge it". Its ask says what the session does on that press.'
                )
        if MOVE_NEED.match(fields["need"]) and "answers" in sent and actor != "cockpit":
            raise Invalid(
                f"Cockpit sets the answers of a move step ({fields['need']!r}): the page carries each one out itself,"
                " and a plain answer would message the stopped session. Leave answers out of the put."
            )
        if MERGE_NEED.match(fields["need"]) and any(answer["closes"] for answer in sent.get("answers") or []):
            raise Invalid(
                f"A {fields['need']!r} step stays with its session after the merge. The session follows the change"
                ' through the configured release stages. Offer "I merged it on GitHub" without closes, so the press hands the'
                " step to the session."
            )
        _check_figures(conn, item_id, [sent.get("body") or "", *(detail["body"] for detail in sent.get("details") or [])])
        if existing is not None and actor != "user" and _waits(conn, existing["id"]):
            return {"step": existing, "skipped": None, "held": _hold(conn, existing["id"], "step_put", actor, fields)}
        if existing is None:
            values = _encode(sent, STEP_JSON) | stamp | {"item_id": item_id, "need": fields["need"], "created_at": now}
            columns = ", ".join(values)
            marks = ", ".join("?" for _ in values)
            step_id = conn.execute(f"INSERT INTO steps ({columns}) VALUES ({marks})", tuple(values.values())).lastrowid
            event = "created"
        else:
            step_id = existing["id"]
            values = _encode(sent, STEP_JSON) | stamp
            event = "updated"
            # The user's Allow stays final while the step asks for the same rule; another rule, or the step put back
            # after a withdrawal, is a new ask (NEXT-41).
            if "permission" in sent and sent["permission"] != existing["permission"]:
                values["permission_allowed_at"] = None
            if existing["withdrawn_at"]:
                values = _encode(STEP_DEFAULTS | sent, STEP_JSON) | stamp | {
                    "withdrawn_at": None, "created_at": now, "permission_allowed_at": None,
                }
                event = "created"
            assignments = ", ".join(f"{name} = ?" for name in values)
            conn.execute(f"UPDATE steps SET {assignments} WHERE id = ?", (*values.values(), step_id))
        # A new progress estimate is kept with its time, to check against the user's time (SPEC NEXT-35).
        before = None if existing is None or existing["withdrawn_at"] else existing["progress"]
        if sent.get("progress") is not None and sent["progress"] != before:
            conn.execute(
                "INSERT INTO progress_marks (step_id, item_id, at, progress, of_user_time) VALUES (?, ?, ?, ?, 1)",
                (step_id, item_id, now, sent["progress"]),
            )
        history.record_version(conn, step_id, actor, now)  # ty: ignore[invalid-argument-type]
        patterns.record_step(conn, event, actor, get_step(conn, step_id))  # ty: ignore[invalid-argument-type]
        now_moment = datetime.now(UTC)
        fill_queue(conn, now_moment)
        _swap_places(conn, now_moment)
        step = get_step(conn, step_id)  # ty: ignore[invalid-argument-type]
    return {"step": step, "skipped": None, "held": None}


def _visible_text(markdown: str) -> str:
    return BARE_URL.sub("", MARKDOWN_LINK.sub(r"\1", markdown)).replace("**", "").replace("`", "")


def _check_step_text(sent: dict[str, Any]) -> None:
    problems = []
    # Cockpit sends an answer as the user's message and a command inside its own.
    for text in [*(answer["text"] for answer in sent.get("answers") or []), (sent.get("command") or {}).get("run", "")]:
        if FORGED_HEADER.search(text):
            problems.append("an answer or command may not contain a line like Cockpit's sender header")
    for name, limit in TEXT_LIMITS.items():
        # The reply hint shows as plain text, a placeholder, so every character of it counts.
        raw = sent.get(name) or ""
        text = raw if name == "reply_hint" else _visible_text(QUOTED_LINE.sub("", raw))
        if len(text) > limit:
            problems.append(f"{name} is {len(text)} characters; the limit is {limit}")
        if match := HEX_ID.search(text):
            problems.append(f"{name} contains {match.group()}; put commit hashes and session ids in details")
    if len(sent.get("details") or []) > DETAILS_LIMIT:
        problems.append(
            f"details has {len(sent['details'])} sections; the limit is {DETAILS_LIMIT}: merge them or leave the least needed out"
        )
    for detail in sent.get("details") or []:
        text = _visible_text(QUOTED_LINE.sub("", detail["body"]))
        if len(text) > DETAIL_CHARS:
            problems.append(
                f"details section {detail['title']!r} is {len(text)} characters; the limit is {DETAIL_CHARS}: "
                "keep what the reader may check and link the rest"
            )
    if problems:
        raise Invalid("; ".join(problems))


def _check_figures(conn: sqlite3.Connection, item_id: int, texts: list[str]) -> None:
    """Each figure a step shows is a shared page or an image on the step's own item (SPEC NEXT-17)."""
    problems = []
    for kind, number in sorted(set().union(*(step_figures(text) for text in texts))):
        table = "item_pages" if kind == "page" else "item_images"
        found = conn.execute(f"SELECT item_id FROM {table} WHERE id = ?", (number,)).fetchone()
        if found is None or found["item_id"] != item_id:
            problems.append(f"{kind}:{number} is not a {kind} on item {item_id}; add it with `cockpit {kind} add {item_id}`")
    if problems:
        raise Invalid("; ".join(problems))


def _ranked_open(
    conn: sqlite3.Connection, items: dict[int, dict[str, Any]], now: datetime
) -> dict[str, list[dict[str, Any]]]:
    """Open steps of the given (unarchived) items: the user's queue, ready next and snoozed, each in rank order,
    and the steps the user handed to agents, in the order the user handed them."""
    # `items` was read just before: a step whose item another request created in between joins on the next read.
    open_steps = [
        step for step in db.rows(
            conn.execute(f"SELECT s.* FROM steps s JOIN items i ON i.id = s.item_id WHERE {OPEN} AND i.archived = 0").fetchall()
        )
        if step["item_id"] in items
    ]
    ordered = rank.order([step for step in open_steps if step["handoff_message_id"] is None], items, now)
    handed = rank.order([step for step in open_steps if step["handoff_message_id"] is not None], items, now)
    return {
        "queue": [step for step in ordered["queue"] if step["queued_at"]],
        "ready_next": [step for step in ordered["queue"] if not step["queued_at"]],
        "snoozed": ordered["snoozed"],
        "with_agents": sorted(handed["queue"] + handed["snoozed"], key=lambda step: step["handoff_message_id"]),
    }


def listed_steps(conn: sqlite3.Connection, now: datetime) -> dict[str, list[int]]:
    """The ids of the open steps in each list the page shows (the user's queue, ready next, snoozed and the steps
    handed to agents), in the page's order, without the rest of the state: a page back from the background checks
    the cards it shows against them (SPEC UI-60)."""
    # Only the items with an open step: the ranking reads no other, and reading all of them is most of the time.
    items = {
        item["id"]: item for item in db.rows(conn.execute(
            f"SELECT * FROM items WHERE archived = 0 AND id IN (SELECT s.item_id FROM steps s WHERE {OPEN})"
        ).fetchall())
    }
    ordered = _ranked_open(conn, items, now)
    return {group: [step["id"] for step in steps] for group, steps in ordered.items()}


def at_dialogs(sessions: list[Session]) -> list[Session]:
    """The Claude sessions at a dialog in their terminal, which Blocked and waiting lists (SPEC PERM-8); the sweep's
    own session stays off it."""
    return [
        session for session in sessions
        if session.provider != "codex" and session.waits_on_dialog and session.name not in SWEEP_SESSION_NAMES
    ]


def queue_top(conn: sqlite3.Connection, now: datetime, count: int) -> list[dict[str, Any]]:
    """The first `count` steps of the user's queue, in the order the page shows them."""
    return _ranked_open(conn, _unarchived_items(conn), now)["queue"][:count]


def check_step(conn: sqlite3.Connection, step_id: int, note: str, actor: str) -> dict[str, Any]:
    """Record that an agent checked an open step against live state and found it still valid."""
    with db.transaction(conn):
        step = get_step(conn, step_id)
        if step["status"] != "open" or step["withdrawn_at"]:
            raise Conflict(f"step {step_id} is {'withdrawn' if step['withdrawn_at'] else step['status']}")
        conn.execute(
            "UPDATE steps SET checked_at = ?, checked_by = ?, check_note = ? WHERE id = ?",
            (db.now_iso(), actor, note, step_id),
        )
        step = get_step(conn, step_id)
        patterns.record_step(conn, "checked", actor, step)
    return step


# Time on a step (SPEC TRACK-2): the user starts a step's timer with its play button, one timer
# runs at a time, and the timer stops when the user pauses it or the step leaves the user. The step
# whose timer runs is the step the user is on (SPEC NEXT-16).


def running_step(conn: sqlite3.Connection) -> int | None:
    """The step whose timer runs, which is the step the user is on; None while no timer runs."""
    record = conn.execute("SELECT step_id FROM time_spans WHERE ended_at IS NULL").fetchone()
    return record["step_id"] if record else None


def hold_step(conn: sqlite3.Connection, step_id: int) -> dict[str, Any]:
    """The user opened, pressed or started answering a queued step on the page (SPEC NEXT-16): it keeps its place
    from now on, as a step whose timer ran there does (`_started_in_place`). A step with no place is left as it is;
    `held` says whether this one has a place to keep."""
    moment = datetime.now(UTC)
    with db.transaction(conn):
        # A press that arrives after the step's snooze holds nothing: the snooze let the place go.
        if _snoozed(get_step(conn, step_id), moment):
            return {"held": False}
        held = conn.execute(
            "UPDATE steps SET held_at = ? WHERE id = ? AND queued_at IS NOT NULL AND status = 'open'"
            " AND withdrawn_at IS NULL AND handoff_message_id IS NULL",
            (_iso(moment), step_id),
        ).rowcount
    return {"held": held == 1}


def _hold(conn: sqlite3.Connection, step_id: int, kind: str, actor: str, data: dict[str, Any]) -> str:
    """Keep an agent's write to a step it waits on (`_waits`) until its release, inside the caller's transaction;
    returns what the agent is told."""
    conn.execute(
        "INSERT INTO held_changes (step_id, kind, actor, data, created_at) VALUES (?, ?, ?, ?, ?)",
        (step_id, kind, actor, json.dumps(data), db.now_iso()),
    )
    if running_step(conn) != step_id:
        return f"An earlier change to step {step_id} waits to apply. This change applies right after it."
    return (
        f"{user_name()}'s timer runs on step {step_id}. This change waits until {user_name()} pauses the timer or"
        f" the step leaves them. Until then `cockpit get` marks the step {user_name().upper()} IS ON IT."
    )


def _waits(conn: sqlite3.Connection, step_id: int) -> bool:
    """An agent's write to this step waits (`_hold`): the user's timer runs on it, or an earlier write to it still
    waits for its release, which a write applied now would overtake."""
    return conn.execute(
        "SELECT 1 FROM time_spans WHERE step_id = ? AND ended_at IS NULL UNION ALL SELECT 1 FROM held_changes WHERE step_id = ?",
        (step_id, step_id),
    ).fetchone() is not None


def release_held(conn: sqlite3.Connection) -> int:
    """Apply, oldest first, the agents' changes that waited for a step's timer to stop; returns how many
    applied. One the step no longer takes (The user closed it meanwhile) is logged and dropped; any other error
    leaves the step's changes held and propagates."""
    steps = [
        row[0] for row in conn.execute(
            "SELECT step_id FROM held_changes h WHERE NOT EXISTS"
            " (SELECT 1 FROM time_spans t WHERE t.step_id = h.step_id AND t.ended_at IS NULL) GROUP BY step_id ORDER BY MIN(id)"
        )
    ]
    applied = 0
    for step_id in steps:
        # A step's changes are read, deleted and applied in one transaction, so they apply in the order they were
        # made: when the wake loop and the user's pause release at the same moment, the one that deletes the rows
        # applies them; a timer started on the step since the first read keeps them all held; an agent's write that
        # arrives after the pause and before this release waits behind them (`_waits`), and none of them waits for
        # another while they apply, since the step has no held row left. An error that propagates leaves them all held.
        with db.transaction(conn):
            if conn.execute("SELECT 1 FROM time_spans WHERE step_id = ? AND ended_at IS NULL", (step_id,)).fetchone():
                continue
            changes = db.rows(conn.execute("SELECT * FROM held_changes WHERE step_id = ? ORDER BY id", (step_id,)).fetchall())
            conn.execute("DELETE FROM held_changes WHERE step_id = ?", (step_id,))
            applied += sum(_apply_held(conn, change) for change in changes)
    return applied


def _apply_held(conn: sqlite3.Connection, change: dict[str, Any]) -> bool:
    """Apply one held change inside the release's transaction; False when the step no longer takes it."""
    data = change["data"]
    try:
        if change["kind"] == "step_put":
            upsert_step(conn, data, change["actor"])
        elif change["kind"] == "step_action":
            step_action(conn, change["step_id"], data["action"], data["body"], change["actor"])
        else:
            upsert_item(conn, data, change["actor"])
    except (Conflict, Invalid, NotFound, Forbidden) as error:
        if change["kind"] == "item_put":
            # A held item close without a finish step would go unreported
            # (SPEC CONTROL-19), so it is an error to see, not a step that moved on.
            log.error("%s's close of item %s, held on step %s since %s, is dropped: %s",
                      change["actor"], data.get("key"), change["step_id"], change["created_at"], error)
            return False
        log.info("%s's %s on step %s, held while %s was on it, no longer applies: %s",
                 change["actor"], change["kind"], change["step_id"], user_name(), error)
        return False
    return True


def start_timer(conn: sqlite3.Connection, step_id: int, press: str | None) -> dict[str, Any]:
    """Start the timer on an open step the user holds; one running on another step stops (`switch`),
    and what agents did to that step meanwhile applies. `press`: the page's id for the press, kept on the span
    (`pause_timer_at`); None for a page that sends no press id. The page sends a start as an action named by its
    press (actions.py), so a repeat after a lost answer gets the first answer and does nothing. A repeat the
    action record let through (the first try failed after its span began, applying held changes) finds the span
    by its press and does nothing either.

    Returns {"started": False} when the step's timer already runs or the press began a span before.
    """
    with db.transaction(conn):
        step = get_step(conn, step_id)
        if (reason := _untimeable(conn, step)) is not None:
            raise Conflict(reason)
        if running_step(conn) == step_id:
            return {"started": False}
        if press is not None and conn.execute("SELECT 1 FROM time_spans WHERE start_press = ?", (press,)).fetchone():
            return {"started": False}
        now = db.now_iso()
        conn.execute("UPDATE time_spans SET ended_at = ?, ended_by = 'switch' WHERE ended_at IS NULL", (now,))
        conn.execute(
            "INSERT INTO time_spans (step_id, started_at, start_press) VALUES (?, ?, ?)", (step_id, now, press)
        )
        # A start anywhere answers the question after a stop for no activity, as Return would (`_away`). It counts
        # as the user's input, so a stop a page worked out before it, for the span it ended, is refused (`stop_away`).
        db.kv_set(conn, "away", None)
        count = _count_input(conn)
        patterns.mark_seen(conn, step["item_id"], now, False)
    release_held(conn)
    return {"started": True, "input_count": count}


def _untimeable(conn: sqlite3.Connection, step: dict[str, Any]) -> str | None:
    """Why the user cannot run a timer on the step, or None when they can."""
    if step["status"] != "open" or step["withdrawn_at"]:
        return f"step {step['id']} is {'withdrawn' if step['withdrawn_at'] else step['status']}"
    if step["handoff_message_id"] is not None:
        return f"step {step['id']} is with an agent"
    if _snoozed(step, datetime.now(UTC)):
        return f"step {step['id']} is snoozed"
    if get_item(conn, step["item_id"])["archived"]:
        return f"step {step['id']}'s item {step['item_id']} is closed"
    return None


def pause_timer(conn: sqlite3.Connection, step_id: int) -> dict[str, Any]:
    """The user pauses the step's running timer; it stays paused until the user presses play. What agents did to
    the step meanwhile applies now."""
    with db.transaction(conn):
        get_step(conn, step_id)
        if not _stop_timer(conn, step_id, "pause", db.now_iso()):
            raise Conflict(f"no timer runs on step {step_id}")
    release_held(conn)
    return {"paused": True}


def pause_timer_at(
    conn: sqlite3.Connection, step_id: int, pressed: datetime, start_press: str | None
) -> dict[str, Any]:
    """The user paused the step's timer at `pressed`, however late the pause arrives; otherwise as `pause_timer`.

    The span that ran on the step at the press ends at the press: a timer started since, on another page, runs on,
    and one a done, a hand-off, a switch or another page's pause ended after the press ends at the press instead.
    Times are kept to the second: when one span ended and the next started in the press's second (another page
    paused and started the timer again then), the earlier one is the one that ran, and the later one runs on.
    `start_press`: the id of the page's start of the step that came just before this pause, or None. The span that
    start began at or after the press (both pressed while the page had no connection) ends where it started, also
    when the start's answer never came back. A repeat after a lost answer is the page's action, which Cockpit
    answers once (actions.py).
    """
    with db.transaction(conn):
        get_step(conn, step_id)
        at = _iso(min(pressed, datetime.now(UTC)))
        span = conn.execute(
            "SELECT id, ended_at, started_at AS stop FROM time_spans WHERE step_id = ? AND start_press = ?"
            " AND started_at >= ?",
            (step_id, start_press, at),
        ).fetchone()
        if span is None:
            span = conn.execute(
                "SELECT id, ended_at, ? AS stop FROM time_spans WHERE step_id = ? AND started_at <= ?"
                " AND (ended_at IS NULL OR ended_at >= ?) ORDER BY started_at, id LIMIT 1",
                (at, step_id, at, at),
            ).fetchone()
        if span is None:
            raise Conflict(f"no timer ran on step {step_id} when the pause was pressed")
        if span["ended_at"] is None or span["ended_at"] > span["stop"]:
            conn.execute("UPDATE time_spans SET ended_at = ?, ended_by = 'pause' WHERE id = ?", (span["stop"], span["id"]))
    if span["ended_at"] is None:
        release_held(conn)
    return {"paused": True}


def note_input(conn: sqlite3.Connection, at: str) -> int:
    """The user's input reached Cockpit at `at` (kv `user_input`), counted (`_count_input`). One transaction, so a stop
    never sees the time without the count. Returns the count."""
    with db.transaction(conn):
        db.kv_set(conn, "user_input", at)
        return _count_input(conn)


def note_seen(conn: sqlite3.Connection, now: datetime) -> int | None:
    """A page with the running timer was on screen at `now` (SPEC TRACK-5; kv `timer_on_screen`), at most once a minute:
    activity for the timer on every device, counted (`_count_input`) so a stop worked out before it does nothing. It
    is not input: sweeps (SPEC SWEEP-11) and the time sheet go by input only, so a page left open keeps neither going.
    Returns the count it made; None when it noted nothing."""
    with db.transaction(conn):
        noted = db.kv_get(conn, "timer_on_screen")
        if noted is not None and now - datetime.fromisoformat(noted) < timedelta(minutes=1):
            return None
        db.kv_set(conn, "timer_on_screen", now.strftime("%Y-%m-%dT%H:%M:%SZ"))
        return _count_input(conn)


def _count_input(conn: sqlite3.Connection) -> int:
    """kv `input_count` counts the user's inputs that reached Cockpit (`note_input`, and each timer start), so a page's
    stop of the timer can tell input came after the state it went by, even within one second (`stop_away`). Returns
    the new count, which the page that sent the input goes by too: its own input is not news to it."""
    count = _input_count(conn) + 1
    db.kv_set(conn, "input_count", count)
    return count


def _input_count(conn: sqlite3.Connection) -> int:
    return db.kv_get(conn, "input_count") or 0


def _input(conn: sqlite3.Connection) -> dict[str, Any]:
    """When a Cockpit page last reported the user's input (`/api/presence`, at most once a minute; SPEC SWEEP-11) or
    the running timer's page on screen (`note_seen`), whichever came later (None before either), and the count of
    both up to it. The page counts it as activity beside its own when it asks whether the user left the running timer
    (SPEC TRACK-5), so input or a page on screen on another device keeps the timer going. One statement reads them
    all, so they belong together: a page that went by a newer count with an older time would stop the timer despite
    the activity between them."""
    stored = {
        row["key"]: json.loads(row["value"])
        for row in conn.execute("SELECT key, value FROM kv WHERE key IN ('user_input', 'timer_on_screen', 'input_count')")
    }
    times = [time for time in (stored.get("user_input"), stored.get("timer_on_screen")) if time]
    return {"last": max(times) if times else None, "count": stored.get("input_count") or 0}


def stop_away(conn: sqlite3.Connection, step_id: int, since: str, seen: int) -> dict[str, Any]:
    """The page saw no activity from `since` on while the step's timer ran (SPEC TRACK-5): the span that ran at
    `since` ends then, as if the user had paused it then; also when a close, a hand-off or another step's timer
    ended it later . When the timer ran until now, every page asks whether the time away counts
    (`_away`), and `away` names that question. `seen` is the count of the user's inputs the page knew of
    (the state's `input.count`): {"stopped": False} when input reached Cockpit since, from another device, a start or an answer to
    the question, such as Add, that a delayed stop from another page would undo. When another page's stop of the same
    stretch came first, its question is this stop's too."""
    with db.transaction(conn):
        step = get_step(conn, step_id)
        if _input_count(conn) != seen:
            return {"stopped": False, "away": None}
        # A page whose clock runs ahead of the server's names a time still to come: the timer stops now.
        since = min(since, db.now_iso())
        asked = _away(conn)
        if asked is not None and asked["step_id"] == step_id:
            stopped = conn.execute("SELECT started_at FROM time_spans WHERE id = ?", (db.kv_get(conn, "away")["span_id"],)).fetchone()
            if stopped is not None and stopped["started_at"] <= since:
                return {"stopped": True, "away": asked["id"]}
        span = conn.execute(
            "SELECT id, ended_at FROM time_spans WHERE step_id = ? AND started_at <= ?"
            " AND (ended_at IS NULL OR ended_at >= ?) ORDER BY started_at DESC, id DESC LIMIT 1",
            (step_id, since, since),
        ).fetchone()
        if span is None:
            raise Conflict(f"step {step_id}'s timer did not run at {since}")
        conn.execute("UPDATE time_spans SET ended_at = ?, ended_by = 'away' WHERE id = ?", (since, span["id"]))
        question = None
        if span["ended_at"] is None and _untimeable(conn, step) is None:
            question = uuid.uuid4().hex
            db.kv_set(conn, "away", {"id": question, "span_id": span["id"], "step_id": step_id, "since": since})
    release_held(conn)
    return {"stopped": True, "away": question}


def _away(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """The question every page asks after a timer stopped for no activity (SPEC TRACK-5), while it waits for the
    user's answer (kv `away`, which `stop_away` sets): its `id`, the step and when its timer stopped. A start
    anywhere answers it, as Return would; the step leaving the user (`_stop_timer`), its item's close or a correction
    of its item's time drops it (`_drop_away`). None also while the step cannot be timed or is gone."""
    away = db.kv_get(conn, "away")
    if away is None:
        return None
    record = conn.execute("SELECT * FROM steps WHERE id = ?", (away["step_id"],)).fetchone()
    step = db.rows([record])[0] if record else None
    if step is None or _untimeable(conn, step) is not None:
        return None
    now = datetime.now(UTC).replace(microsecond=0)
    item = get_item(conn, step["item_id"])
    return {
        "id": away["id"],
        "step_id": step["id"],
        "item_id": item["id"],
        "item_title": item["title"],
        "since": away["since"],
        "seconds": sum(length for _, length in _item_spans(conn, item["id"], now)),
    }


def _drop_away(conn: sqlite3.Connection, item_id: int) -> None:
    """A correction of the item's time, or its close, settles the question about its step's time without activity."""
    away = db.kv_get(conn, "away")
    if away is None:
        return
    step = conn.execute("SELECT item_id FROM steps WHERE id = ?", (away["step_id"],)).fetchone()
    if step is None or step["item_id"] == item_id:
        db.kv_set(conn, "away", None)


def answer_away(conn: sqlite3.Connection, step_id: int, question: str, add: bool) -> dict[str, Any]:
    """The user's answer to the question (`_away`, by its id) after the step's timer stopped for no activity (SPEC
    TRACK-5). Return (`add` false) starts the timer from now; Add runs the stopped span on, so the time without
    activity counts once. Cockpit takes the first answer: {"answered": False} when that question is no longer open
    (answered on another page, a timer started, the step left the user, or a later question)."""
    with db.transaction(conn):
        away = _away(conn)
        if away is None or (away["step_id"], away["id"]) != (step_id, question):
            return {"answered": False}
        if not add:
            start_timer(conn, step_id, None)
        else:
            if datetime.now(UTC) - rank.parse_time(away["since"]) > timedelta(seconds=ADD_LIMIT):  # ty: ignore[unsupported-operator]
                raise Invalid(f"adds at most {ADD_LIMIT // 3600} hours at once")
            span_id = db.kv_get(conn, "away")["span_id"]
            conn.execute("UPDATE time_spans SET ended_at = NULL, ended_by = NULL WHERE id = ?", (span_id,))
            db.kv_set(conn, "away", None)
        # The answer is the user's input, which a stop from before it must not undo (`stop_away`).
        count = note_input(conn, db.now_iso())
    return {"answered": True, "input_count": count}


def _item_spans(conn: sqlite3.Connection, item_id: int, now: datetime) -> list[tuple[sqlite3.Row, int]]:
    """The item's timer spans, newest first, each with its length in seconds (a running one up to `now`)."""
    get_item(conn, item_id)
    spans = conn.execute(
        "SELECT t.id, t.step_id, t.started_at, t.ended_at FROM time_spans t JOIN steps s ON s.id = t.step_id"
        " WHERE s.item_id = ? ORDER BY t.started_at DESC, t.id DESC",
        (item_id,),
    ).fetchall()
    return [
        (span, round(((rank.parse_time(span["ended_at"]) or now) - rank.parse_time(span["started_at"])).total_seconds()))  # ty: ignore[unsupported-operator]
        for span in spans
    ]


def _add_span(conn: sqlite3.Connection, item_id: int, spans: list[tuple[sqlite3.Row, int]], seconds: int, now: datetime) -> None:
    """Add `seconds` the timers missed on the item (SPEC TRACK-7): work before its newest span started, so a
    running timer runs on as it was. The span goes at the latest time before then that no timer span and no
    feedback the user wrote covers, so the timesheet, which counts overlapping time once, counts all of it."""
    if seconds > ADD_LIMIT:
        raise Invalid(f"adds at most {ADD_LIMIT // 3600} hours at once")
    if not spans:
        raise Invalid(f"item {item_id} has no timed step to add time to: start a step's timer first")
    newest = spans[0][0]
    end: datetime = rank.parse_time(newest["started_at"])  # ty: ignore[invalid-assignment]
    while True:
        start = end - timedelta(seconds=seconds)
        # Taken time: each span with a length (a play and a pause in one second leave an empty one), and each
        # feedback's writing time, which ends when it was sent (timesheet.py).
        (clash,) = conn.execute(
            """SELECT MIN(started) FROM (
                   SELECT started_at AS started, COALESCE(ended_at, :now) AS ended FROM time_spans
                   WHERE ended_at IS NULL OR ended_at > started_at
                   UNION ALL
                   SELECT strftime('%Y-%m-%dT%H:%M:%SZ', created_at, printf('-%d seconds', seconds)), created_at
                   FROM feedback WHERE seconds > 0
               ) WHERE started < :end AND ended > :start""",
            {"now": _iso(now), "end": _iso(end), "start": _iso(start)},
        ).fetchone()
        if clash is None:
            break
        end = rank.parse_time(clash)  # ty: ignore[invalid-assignment]
    conn.execute(
        "INSERT INTO time_spans (step_id, started_at, ended_at, ended_by) VALUES (?, ?, ?, 'corrected')",
        (newest["step_id"], _iso(start), _iso(end)),
    )


def add_item_time(conn: sqlite3.Connection, item_id: int, seconds: int) -> dict[str, Any]:
    """The user adds `seconds` to the time their steps' timers counted on the item (SPEC TRACK-7)."""
    with db.transaction(conn):
        now = datetime.now(UTC).replace(microsecond=0)
        _add_span(conn, item_id, _item_spans(conn, item_id, now), seconds, now)
        _drop_away(conn, item_id)
    return {"added": seconds}


def set_item_time(conn: sqlite3.Connection, item_id: int, seconds: int) -> dict[str, Any]:
    """The user sets the time their steps' timers counted on the item to `seconds`. Lower: the newest time goes
    first, and a running timer keeps running from now. Higher: the difference is added as in `add_item_time`."""
    with db.transaction(conn):
        now = datetime.now(UTC).replace(microsecond=0)
        _drop_away(conn, item_id)
        lengths = _item_spans(conn, item_id, now)
        total = sum(length for _, length in lengths)
        if seconds > total:
            _add_span(conn, item_id, lengths, seconds - total, now)
            return {"seconds": seconds}
        cut = total - seconds
        for span, length in lengths:
            if cut <= 0:
                break
            take = min(cut, length)
            cut -= take
            if take == length:
                conn.execute("DELETE FROM time_spans WHERE id = ?", (span["id"],))
            elif span["ended_at"] is None:
                conn.execute(
                    "UPDATE time_spans SET ended_at = ?, ended_by = 'corrected' WHERE id = ?",
                    (_iso(now - timedelta(seconds=take)), span["id"]),
                )
            else:
                conn.execute(
                    "UPDATE time_spans SET ended_at = ? WHERE id = ?",
                    (_iso(rank.parse_time(span["ended_at"]) - timedelta(seconds=take)), span["id"]),  # ty: ignore[unsupported-operator]
                )
            if span["ended_at"] is None:
                conn.execute("INSERT INTO time_spans (step_id, started_at) VALUES (?, ?)", (span["step_id"], _iso(now)))
    return {"seconds": seconds}


def _stop_timer(conn: sqlite3.Connection, step_id: int, reason: str, now: str) -> int:
    """End the step's running span with `reason`, inside the caller's transaction; 1 if one ran. The step leaves
    the user (a close, a snooze, a hand-off) or the user paused it: a question about its time without activity
    goes too (`_away`)."""
    away = db.kv_get(conn, "away")
    if away is not None and away["step_id"] == step_id:
        db.kv_set(conn, "away", None)
    return conn.execute(
        "UPDATE time_spans SET ended_at = ?, ended_by = ? WHERE step_id = ? AND ended_at IS NULL", (now, reason, step_id)
    ).rowcount


def _timers(conn: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    """Per step with any tracked time: the item, the seconds of its ended spans, and when its
    running span started (None when none runs)."""
    return {
        record["step_id"]: {
            "item_id": record["item_id"],
            "seconds": record["seconds"],
            "started_at": record["started_at"],
        }
        for record in conn.execute(
            "SELECT t.step_id, s.item_id,"
            " SUM(CASE WHEN t.ended_at IS NULL THEN 0"
            " ELSE strftime('%s', t.ended_at) - strftime('%s', t.started_at) END) AS seconds,"
            " MAX(CASE WHEN t.ended_at IS NULL THEN t.started_at END) AS started_at"
            " FROM time_spans t JOIN steps s ON s.id = t.step_id GROUP BY t.step_id"
        ).fetchall()
    }


def _unarchived_items(conn: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    return {item["id"]: item for item in db.rows(conn.execute("SELECT * FROM items WHERE archived = 0").fetchall())}


def _ordinary_first(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ready-next steps in the order they take free places: an early look only after every other step (SPEC NEXT-42),
    so it shows while the user has room that no other step wants."""
    return sorted(steps, key=lambda step: bool(step["early"]))


def fill_queue(conn: sqlite3.Connection, now: datetime) -> None:
    """Give each free place in the user's queue to the ready-next step with the highest score, early looks last
    (`_ordinary_first`); call inside the write's transaction. Then each other step still waiting takes the place of
    a movable early look (`_movable`), weakest first, whatever their scores (SPEC NEXT-42): the user's queue filled
    up again, so the early look waits until there is room. This runs on every fill, so a take, an Undo or a restored
    item cannot leave an early look in a place another step waits for."""
    ranked = _ranked_open(conn, _unarchived_items(conn), now)
    free = max(0, QUEUE_LIMIT - len(ranked["queue"]))
    waiting = _ordinary_first(ranked["ready_next"])
    # An ordinary place ends an earlier take, whose time may be a second ahead (`step_action` "take").
    for step in waiting[:free]:
        conn.execute("UPDATE steps SET queued_at = ?, taken_at = NULL WHERE id = ?", (_iso(now), step["id"]))
    ordinary = [step for step in waiting[free:] if not step["early"]]
    if ordinary:
        held = _started_in_place(conn)
        early = [step for step in ranked["queue"] if step["early"] and _movable(step, held)]
        early.sort(key=lambda step: step["score"])
        _swap(conn, list(zip(ordinary, early)), now)


def _started_in_place(conn: sqlite3.Connection) -> set[int]:
    """The queued steps whose timer ran while they held their place, running now or paused since (also one started
    in ready next that took a place while it ran), and those the user opened or answered in there (`hold_step`):
    each keeps that place (SPEC NEXT-16) when another step would take it by score (`_stronger_first`) or by a take
    (`_take_place`, while other steps can make room). A take's place time can run seconds ahead of the clock
    (`_take_place`), so a taken step's place starts at its last take event instead (Take, or Take back: a return by
    the user). Times are to the second. A span that a hand-off ended (the user's message, Run or Allow: `hand_off`)
    in the second a place started belongs to the place the hand-off took the step out of, not to the one its quick
    return gave it; Undo's old place started before it and keeps it. A hand-off, snooze or withdraw clears a hold,
    so one at the second of the place time is the user's on this place."""
    return {
        row[0] for row in conn.execute(
            "WITH placed AS (SELECT s.id, CASE WHEN s.taken_at IS s.queued_at THEN COALESCE((SELECT MAX(e.at)"
            " FROM events e WHERE e.step_id = s.id AND (e.kind = 'step.take' OR (e.kind = 'step.return'"
            " AND e.actor = 'user'))), s.queued_at) ELSE s.queued_at END AS at"
            f" FROM steps s WHERE s.queued_at IS NOT NULL AND {OPEN})"
            " SELECT DISTINCT t.step_id FROM time_spans t JOIN placed p ON p.id = t.step_id"
            " WHERE t.ended_at IS NULL OR julianday(t.ended_at) > julianday(p.at) OR (julianday(t.ended_at) ="
            " julianday(p.at) AND t.ended_by NOT IN ('hand_off', 'ran', 'allowed'))"
            " UNION SELECT p.id FROM placed p JOIN steps s ON s.id = p.id WHERE julianday(s.held_at) >= julianday(p.at)"
        )
    }


# How far a ready-next step's score must pass a queued step's to take its place (`_stronger_first`), so a small
# change of scores between sweeps does not move places back and forth while the user works through the queue.
PLACE_MARGIN = 10


def _in_queue(step: dict[str, Any], now: datetime) -> bool:
    """Whether the step shows in the user's queue: it has a place, is not asleep and is not with an agent. A
    snoozed step keeps its place time while it sleeps, so `queued_at` alone does not say."""
    return step["queued_at"] is not None and not _snoozed(step, now) and step["handoff_message_id"] is None


def _resort_after_close(conn: sqlite3.Connection, now: datetime, took_place: bool) -> None:
    """`fill_queue` after the user's press took steps away (Done, Dismiss, Snooze, a hand-off, Task done, Close
    item, an archive); then, when one of them showed in the queue (`took_place`, from `_in_queue` before the
    press), each waiting step scored
    above a movable queued step takes its place, whatever the gap (SPEC RANK-19). The places move once,
    with the press, while the user's eyes are on the queue anyway, so the margin that keeps places still between
    presses is not needed. The page draws the same move with the press (`resorted` in app.js)."""
    fill_queue(conn, now)
    if took_place:
        _stronger_first(conn, now, 0)


def _swap_places(conn: sqlite3.Connection, now: datetime) -> None:
    """`_stronger_first` after a put or return, unless a sweep runs: it scores the steps one put at a time, so a swap
    then would weigh a new score against one not yet rescored . The sweep's finish swaps once
    (`finish_sweep`); a sweep that goes silent stops counting as running (`sweep_running`), and the next put swaps."""
    if sweep_running(conn, now) is None:
        _stronger_first(conn, now, PLACE_MARGIN)


def _stronger_first(conn: sqlite3.Connection, now: datetime, margin: int) -> None:
    """A ready-next step whose score passes a queued step's by more than `margin` takes its place, and the
    other step waits as ready next (SPEC RANK-16, RANK-18): a place someone took while scores were lower does not
    keep a stronger step out. Only movable steps (`_movable`) give up their places. The highest waiting step meets
    the lowest movable step first. Runs with PLACE_MARGIN when a step is put or returned (`_swap_places`) and when a
    sweep finishes, and with no margin after the user's close (`_resort_after_close`); not on every fill, so the
    user's Undo (`_give_back_places`) leaves the queue as it was."""
    ranked = _ranked_open(conn, _unarchived_items(conn), now)
    held = _started_in_place(conn)
    movable = sorted((step for step in ranked["queue"] if _movable(step, held)), key=lambda step: step["score"])
    pairs = []
    # An early look takes only an early look's place, never another step's; another step takes an early look's
    # place whatever the scores, in `fill_queue` (SPEC NEXT-42).
    for early in (False, True):
        strong_steps = [step for step in ranked["ready_next"] if bool(step["early"]) is early]
        weak_steps = [step for step in movable if bool(step["early"]) is early]
        for strong, weak in zip(strong_steps, weak_steps):
            if strong["score"] - weak["score"] <= margin:
                break
            pairs.append((strong, weak))
    _swap(conn, pairs, now)


def _movable(step: dict[str, Any], held: set[int]) -> bool:
    """A queued step another step may take the place of: not one whose timer ran in its place or the user opened
    there (`held`, from `_started_in_place`), and not one the user took or snoozed (also once the snooze woke it)."""
    return step["id"] not in held and not rank.taken(step) and not step["snoozed_until"]


def _swap(conn: sqlite3.Connection, pairs: list[tuple[dict[str, Any], dict[str, Any]]], now: datetime) -> None:
    """Each (strong, weak) pair: the strong step takes the weak step's place, and the weak step waits in ready next."""
    for strong, weak in pairs:
        conn.execute("UPDATE steps SET queued_at = NULL WHERE id = ?", (weak["id"],))
        conn.execute("UPDATE steps SET queued_at = ?, taken_at = NULL WHERE id = ?", (_iso(now), strong["id"]))


def _give_back_places(conn: sqlite3.Connection, back_ids: list[int], left: datetime, now: datetime) -> None:
    """Steps that come back to their places (a reopen, a snooze undone, or the steps of an undone finish) leave
    the queue as it was: steps that took a place at or after `left`, when they left the queue, return to ready
    next, newest place first, while the queue is over QUEUE_LIMIT. The steps coming back, the step the user
    is on and the steps the user took to the top (SPEC NEXT-36) keep their places."""
    queue = _ranked_open(conn, _unarchived_items(conn), now)["queue"]
    keep = {*back_ids, running_step(conn)}
    later = [
        step for step in queue
        if step["id"] not in keep
        and not rank.taken(step)
        and rank.parse_time(step["queued_at"]) >= left  # ty: ignore[unsupported-operator]
    ]
    later.sort(key=lambda step: (step["queued_at"], step["id"]), reverse=True)  # times are to the second
    for step in later[: max(0, len(queue) - QUEUE_LIMIT)]:
        conn.execute("UPDATE steps SET queued_at = NULL WHERE id = ?", (step["id"],))


def _take_place(conn: sqlite3.Connection, step_id: int, now_moment: datetime) -> None:
    """Put a step at the top of the user's queue, as a take (SPEC NEXT-36). Times are to the second: a take in the
    same second as the last one is a second later, so it leads. While the queue is over QUEUE_LIMIT, its last steps
    other than this one and the one the user is on go back to ready next: first those the user did not start in
    their place, then, when only started ones are left, the weakest paused one (`_started_in_place`), so the queue
    keeps its four places. A snoozed step keeps its snooze and holds the place for when it wakes, so it counts only
    once it shows."""
    last = rank.parse_time(conn.execute("SELECT MAX(taken_at) FROM steps").fetchone()[0])
    taken = _iso(max(now_moment, last + timedelta(seconds=1)) if last else now_moment)
    conn.execute("UPDATE steps SET queued_at = ?, taken_at = ? WHERE id = ?", (taken, taken, step_id))
    queue = _ranked_open(conn, _unarchived_items(conn), now_moment)["queue"]
    started = _started_in_place(conn)
    others = [other for other in queue if other["id"] not in {step_id, running_step(conn)}]
    # The last steps go first, so the started steps stand before the others.
    others = [other for other in others if other["id"] in started] + [other for other in others if other["id"] not in started]
    over = len(queue) - QUEUE_LIMIT
    for other in others[-over:] if over > 0 else []:
        conn.execute("UPDATE steps SET queued_at = NULL WHERE id = ?", (other["id"],))


def _asked_by_user(item: dict[str, Any]) -> bool:
    """An open item the user added with Add (keys `todo-<id>`, which only Add makes), not one an agent marked a to-do."""
    return item["key"].startswith("todo-") and not item["archived"]


def _snoozed(step: dict[str, Any], now: datetime) -> bool:
    until = rank.parse_time(step["snoozed_until"])
    return until is not None and until > now


def _set_aside_unsent(conn: sqlite3.Connection, message_id: int, now: str) -> bool:
    """The user took a step back before its hand-off message went out (it waited for Take back, SPEC MSG-8, or for
    the session's turn, SPEC MSG-3): the message is set aside unsent, as a dismissed failure (SPEC MSG-5).
    True when it was still waiting and is now set aside."""
    updated = conn.execute(
        "UPDATE messages SET state = 'failed', detail = ?, dismissed_at = ?, dismissed_by = 'user', dismiss_reason = ?,"
        " take_back_until = NULL WHERE id = ? AND state = 'queued'",
        (
            f"Not sent: {user_name()} took the step back first.",
            now,
            f"{user_name()} took the step back before it was sent",
            message_id,
        ),
    )
    if updated.rowcount:
        # Its page comments wait for the next answer again (SPEC NEXT-38).
        conn.execute(
            "UPDATE page_comments SET state = 'unsent', message_id = NULL, updated_at = ? WHERE message_id = ?",
            (now, message_id),
        )
        conn.execute("UPDATE page_choices SET message_id = NULL WHERE message_id = ?", (message_id,))
        message = get_message(conn, message_id)
        patterns.record(
            conn, "message.dismissed", "user", message["item_id"], message["step_id"], None,
            {"message_id": message_id, "reason": message["dismiss_reason"]},
        )
    return bool(updated.rowcount)


def step_action(
    conn: sqlite3.Connection, step_id: int, action: str, body: dict[str, Any], actor: str
) -> dict[str, Any]:
    """The user's controls on one step, and the agents' done, dismiss, withdraw, yield and return.

    The user's controls (USER_ACTIONS) stay until the user changes them; no agent can set
    or undo them. `take` gives a ready-next step a place now, first in the queue (SPEC NEXT-36);
    when that puts the queue over QUEUE_LIMIT, its last step that the user is not on goes back to
    ready next. `withdraw` takes an open step out of the queue until an agent
    puts it again; `yield` swaps a queued step with the strongest ready-next step,
    which must outrank it (409 otherwise); the yielded step keeps its text;
    a step the user snoozed is the user's. `return`
    gives a step the user handed to an agent back to ready next (the agent returns
    it, or the user takes it back); closing or withdrawing it ends the hand-off too.
    Any place the action frees goes to the strongest ready-next step. An agent's
    close, withdraw or yield of the step whose timer runs waits until the timer
    stops (`held` says so); the user's action that stops it applies what waited.
    Only the user's finish closes a finish step (SPEC CONTROL-17): an agent's done or
    dismiss on one the user handed it returns it to the user (`returned` says so, and
    the note becomes the step's check note), and on any other it is refused. An agent
    returns a finish step the user handed it only with a note on what was done
    (`_require_done_note`, CONTROL-15), and does not withdraw it; an agent's note on a return
    is the step's check note. A returned step opens with the agent's reply to the user's message
    (`_answer_on_step`, NEXT-37).
    """
    if action not in USER_ACTIONS | AGENT_ACTIONS:
        raise NotFound(f"unknown action {action!r}")
    if action in USER_ACTIONS and actor != "user":
        raise Forbidden(f"only {user_name()} can {action} a step; agents can mark it done, dismiss or withdraw it")
    if action == "snooze" and "until" not in body:
        raise Invalid("snooze needs until: a time, or null to wake the step now")
    if "note" in body and action not in ("done", "dismiss", "return"):
        raise Invalid("a note goes only with done, dismiss or return")
    now_moment = datetime.now(UTC)
    now = _iso(now_moment)
    stopped = None
    returned = False
    with db.transaction(conn):
        step = get_step(conn, step_id)
        finishing = rank.finishing(step) and step["status"] == "open" and not step["withdrawn_at"]
        if actor != "user" and action in ("done", "dismiss") and finishing:
            if step["handoff_message_id"] is None:
                raise Forbidden(
                    f"step {step_id} asks {user_name()} to finish item {step['item_id']}. Only {user_name()}'s press"
                    " closes a task. Withdraw the step when work is left."
                )
            action, returned = "return", True
        if actor != "user" and action == "return" and finishing and step["handoff_message_id"] is not None:
            _require_done_note(conn, step, body.get("note", ""))
        if actor != "user" and action == "return" and step["handoff_message_id"] is not None and step["status"] == "open":
            _answer_on_step(conn, step, actor, body.get("note", ""), now)
        if actor != "user" and action == "withdraw" and finishing and step["handoff_message_id"] is not None:
            raise Forbidden(_keep_handed_finish(conn, step))
        if actor != "user" and action == "return" and body.get("note"):
            conn.execute(
                "UPDATE steps SET checked_at = ?, checked_by = ?, check_note = ? WHERE id = ?",
                (now, actor, body.get("note", ""), step_id),
            )
        # An agent's close of the item's last open step that would leave shipped work with no next move is refused
        # before it is held, so the agent hears it (SPEC STATE-5). A withdrawn step held nothing.
        if actor.startswith("agent:") and action in ("done", "dismiss") and step["status"] == "open" and not step["withdrawn_at"]:
            item = get_item(conn, step["item_id"])
            if _shipped_untended(conn, item, step_id):
                holder = None
                if step["handoff_message_id"] is not None:
                    asked = get_message(conn, step["handoff_message_id"])
                    if asked["state"] != "sent":
                        raise Invalid(UNREACHED.format(user=user_name(), step=step_id))
                    holder = asked["session_id"] or UNKNOWN_HOLDER
                _refuse_untended(conn, item, f"Closing step {step_id} leaves item {item['id']}", False, holder)
        if actor != "user" and action in HELD_ACTIONS and _waits(conn, step_id):
            return step | {"held": _hold(conn, step_id, "step_action", actor, {"action": action, "body": body})}
        # An agent closes a withdrawn step whose need is gone for good, without bringing it back to the user's
        # queue first; the user's click on a card that was withdrawn meanwhile is stale.
        if step["withdrawn_at"] and (actor == "user" or action not in ("done", "dismiss")):
            raise Conflict(
                f"step {step_id} was withdrawn by {step['updated_by']}; it returns when an agent puts it again,"
                " and an agent's done or dismiss closes it"
            )
        if action == "reopen":
            if step["status"] == "open":
                raise Conflict(f"step {step_id} is already open")
            conn.execute("UPDATE steps SET status = 'open', closed_at = NULL WHERE id = ?", (step_id,))
            _give_back_places(conn, [step_id], rank.parse_time(step["closed_at"]), now_moment)  # ty: ignore[invalid-argument-type]
        elif step["status"] != "open":
            raise Conflict(f"step {step_id} is {step['status']}")
        elif action == "withdraw":
            if step["snoozed_until"]:
                raise Conflict(f"step {step_id} has {user_name()}'s snooze; update its text instead")
            conn.execute(
                "UPDATE steps SET withdrawn_at = ?, queued_at = NULL, handoff_message_id = NULL, held_at = NULL WHERE id = ?",
                (now, step_id),
            )
        elif action == "take":
            ranked = _ranked_open(conn, _unarchived_items(conn), now_moment)
            if step_id not in {other["id"] for other in ranked["ready_next"]}:
                raise Conflict(f"step {step_id} is no longer waiting for a place in Inbox")
            _take_place(conn, step_id, now_moment)
        elif action == "yield":
            queued = _ranked_open(conn, _unarchived_items(conn), now_moment)["queue"]
            if step_id not in {other["id"] for other in queued}:
                raise Conflict(f"step {step_id} has no place in {user_name()}'s queue")
            if step["snoozed_until"]:
                raise Conflict(f"step {step_id} has {user_name()}'s snooze; it keeps its place")
            if rank.taken(step):
                raise Conflict(f"{user_name()} took step {step_id} to the top of their queue; it keeps its place")
            # A yield of a step whose timer ran applies once the user pauses it (SWEEP-19); one they opened or
            # answered in keeps its place until they press Resorted (NEXT-16).
            if step["held_at"] and step_id in _started_in_place(conn):
                raise Conflict(f"{user_name()} opened step {step_id} in its place; it keeps its place")
            conn.execute("UPDATE steps SET queued_at = NULL WHERE id = ?", (step_id,))
            # A swap, so it works while the queue holds more than QUEUE_LIMIT (woken snoozes, reopened steps).
            ready = _ordinary_first(_ranked_open(conn, _unarchived_items(conn), now_moment)["ready_next"])
            if ready[0]["id"] == step_id:
                raise Conflict(f"no ready-next step outranks step {step_id}; it keeps its place")
            conn.execute("UPDATE steps SET queued_at = ?, taken_at = NULL WHERE id = ?", (now, ready[0]["id"]))
        elif action in ("done", "dismiss"):
            status = "done" if action == "done" else "dismissed"
            conn.execute(
                "UPDATE steps SET status = ?, closed_at = ?, handoff_message_id = NULL, withdrawn_at = NULL WHERE id = ?",
                (status, now, step_id),
            )
            if actor != "user":
                _record_closure(conn, "step", step_id, step["item_id"], status, actor, now, body.get("note", ""))
        elif action == "return":
            if step["handoff_message_id"] is None:
                raise Conflict(f"step {step_id} is not with an agent")
            if actor == "user" and _set_aside_unsent(conn, step["handoff_message_id"], now):
                stopped = get_message(conn, step["handoff_message_id"])
            # The user's Take back puts the step at the top of their queue, as a take; an agent's return takes a
            # place by the step's score, as any step does (SPEC RANK-18).
            conn.execute("UPDATE steps SET handoff_message_id = NULL WHERE id = ?", (step_id,))
            if actor == "user":
                _take_place(conn, step_id, now_moment)
        elif action == "snooze":
            conn.execute(
                "UPDATE steps SET snoozed_until = ?, held_at = CASE WHEN ? IS NULL THEN held_at END WHERE id = ?",
                (body["until"], body["until"], step_id),
            )
            if body["until"] is None and _snoozed(step, now_moment):
                # Woken early, as Undo after Snooze does: the step left the queue at its last snooze.
                slept = conn.execute(
                    "SELECT MAX(at) FROM events WHERE step_id = ? AND kind = 'step.snooze'", (step_id,)
                ).fetchone()[0]
                if slept:
                    _give_back_places(conn, [step_id], rank.parse_time(slept), now_moment)  # ty: ignore[invalid-argument-type]
        if action in ("done", "dismiss", "withdraw") or (action == "snooze" and body["until"] is not None):
            _stop_timer(conn, step_id, action, now)
        conn.execute("UPDATE steps SET updated_at = ?, updated_by = ? WHERE id = ?", (now, actor, step_id))
        patterns.record_step(conn, action, actor, get_step(conn, step_id))
        if actor == "user" and (action in ("done", "dismiss") or (action == "snooze" and body["until"] is not None)):
            _resort_after_close(conn, now_moment, _in_queue(step, now_moment))
        else:
            fill_queue(conn, now_moment)
        if action == "return" and actor != "user":
            _swap_places(conn, now_moment)
        step = get_step(conn, step_id)
    if actor == "user":
        release_held(conn)
    if returned:
        step["returned"] = (
            f"step {step_id} is back in {user_name()}'s queue. Only {user_name()} finishes item {step['item_id']}, by"
            " pressing Task done, a closing answer or Close."
        )
    if action == "return":
        # Take back says what became of the message: `stopped` when it had not gone yet, and
        # `unsent` the words of a stopped message of the user's own, which the page puts back in the reply box,
        # and `unsent_to` where they were going, which the box writes to again: the session, or the checkout and
        # picks a new session was to start with.
        step["stopped"] = stopped is not None
        own = stopped is not None and stopped["author"] == "user"
        step["unsent"] = _own_words(stopped) if own else None
        step["unsent_to"] = {
            "session_id": stopped["session_id"], "new_cwd": stopped["new_cwd"], "provider": stopped["provider"],
            "options": stopped["new_options"],
        } if own else None
    return step


# How long a hand-off the user sent waits for Take back at most (SPEC MSG-8). The page releases it sooner, once its
# notice with Take back is gone (8 s, longer while the pointer rests on it); this limit covers a page that
# closed meanwhile.
TAKE_BACK_LIMIT = timedelta(seconds=60)


def hand_off_step(
    conn: sqlite3.Connection, step_id: int, text: str, target: dict[str, Any], checkouts_dir: Path
) -> dict[str, Any]:
    """The user's message hands the step to an agent; the step leaves the user's queue and ready next.

    `target` is {session_id, new_cwd}, both None for the step's own session.
    Its place goes to the strongest ready-next step, and what agents did to it
    while its timer ran applies now. It stays with the agent until someone
    closes, withdraws or returns it.
    The message waits for Take back (SPEC MSG-8): the page releases it once its
    notice with Take back is gone (`release_message`), and the wake loop at
    TAKE_BACK_LIMIT after the hand-off (delivery.take_back_due). Returns the
    queued message.
    """
    with db.transaction(conn):
        message = hand_off(conn, step_id, "user", text, target, checkouts_dir, "hand_off")
        reopened = reopen_for_message(conn, message["item_id"])
        until = _iso(datetime.now(UTC) + TAKE_BACK_LIMIT)
        conn.execute("UPDATE messages SET take_back_until = ? WHERE id = ?", (until, message["id"]))
    release_held(conn)
    return get_message(conn, message["id"]) | {"reopened": reopened}


def release_message(conn: sqlite3.Connection, message_id: int) -> bool:
    """End a hand-off's wait for Take back (SPEC MSG-8). True when the message is now free to go; false when
    it no longer waited for Take back: the user took the step back, or it was released or sent now before. From
    the release on it waits for its turn (`waits_since`), so a later prompt checked before it goes still waits
    behind it (`delivery._older_waits`)."""
    get_message(conn, message_id)
    released = conn.execute(
        "UPDATE messages SET take_back_until = NULL, waits_since = COALESCE(waits_since, ?)"
        " WHERE id = ? AND state = 'queued' AND take_back_until IS NOT NULL",
        (db.now_iso(), message_id),
    )
    return released.rowcount == 1


def hold_message(conn: sqlite3.Connection, message_id: int) -> bool:
    """The user is still writing more for a hand-off whose notice with Take back shows (SPEC MSG-10): its wait for
    Take back starts again. True while it still waited; false once it was released, taken back or sent."""
    get_message(conn, message_id)
    held = conn.execute(
        "UPDATE messages SET take_back_until = ? WHERE id = ? AND state = 'queued' AND take_back_until IS NOT NULL",
        (_iso(datetime.now(UTC) + TAKE_BACK_LIMIT), message_id),
    )
    return held.rowcount == 1


def add_follow_up(conn: sqlite3.Connection, message_id: int, text: str, checkouts_dir: Path) -> dict[str, Any]:
    """The user adds to a message they sent, from the notice with Take back (SPEC MSG-10). While the first message
    has not gone, the follow-up joins its end, so the session reads both as one message, in order. Once it went,
    the follow-up goes as a new message to the session the first one reached, and delivery keeps it after the
    first. Returns the message that carries the follow-up, with `joined` saying which happened and `reopened` whether
    it took a finished task up again."""
    if not text.strip():
        raise Invalid("the follow-up is empty")
    until = _iso(datetime.now(UTC) + TAKE_BACK_LIMIT)
    # One transaction, so a failure part way (a locked database at the event) leaves the text as it was and the
    # page's repeat of the action adds the follow-up once, and a Take back before it is seen here.
    with db.transaction(conn):
        first = get_message(conn, message_id)
        if first["author"] != "user":
            raise Invalid(f"message {message_id} is {first['author']}'s; the user adds only to their own messages")
        if first["dismissed_at"] is not None:
            raise Conflict(f"message {message_id} was not sent: {first['detail'] or 'it was set aside'}")
        # On a hand-off that carries page comments or choices the follow-up joins the user's words, before them, so
        # Take back puts all of them back in the reply box (`_own_words`).
        own = _own_words(first)
        joined_text = "\n\n".join(part for part in (own, text.strip(), first["page_answers"]) if part)
        joined = conn.execute(
            "UPDATE messages SET text = ?, take_back_until = CASE WHEN take_back_until IS NULL THEN NULL ELSE ? END"
            " WHERE id = ? AND state = 'queued'",
            (joined_text, until, message_id),
        ).rowcount == 1
        if joined:
            carrier = get_message(conn, message_id)
        else:
            first = get_message(conn, message_id)
            if first["session_id"] is None:
                # A new session's first message that failed, or is still starting it, names no session to follow.
                raise Conflict(f"message {message_id} has not reached a session: {first['detail'] or first['state']}")
            target = {"item_id": first["item_id"], "step_id": first["step_id"], "session_id": first["session_id"], "new_cwd": None}
            carrier = create_message(conn, "user", text.strip(), target, checkouts_dir)
        reopened = first["item_id"] is not None and reopen_for_message(conn, first["item_id"])
        patterns.record(conn, "message.followed_up", "user", first["item_id"], first["step_id"], None,
                        {"message_id": message_id, "joined": joined})
    return carrier | {"joined": joined, "reopened": reopened}


def _takes_page_answers(conn: sqlite3.Connection, step_id: int) -> bool:
    """Whether page comments or choices wait to go with the user's next answer to the step (`hand_off`)."""
    return conn.execute(
        "SELECT EXISTS (SELECT 1 FROM page_comments WHERE step_id = ? AND state = 'unsent')"
        " OR EXISTS (SELECT 1 FROM page_choices WHERE step_id = ? AND message_id IS NULL)",
        (step_id, step_id),
    ).fetchone()[0] == 1


def check_open_for_hand_off(step: dict[str, Any]) -> None:
    if step["status"] != "open" or step["withdrawn_at"]:
        raise Conflict(f"step {step['id']} is not open")
    if step["handoff_message_id"] is not None:
        raise Conflict(f"step {step['id']} is already with an agent; take it back first")


def hand_off(
    conn: sqlite3.Connection,
    step_id: int,
    author: str,
    text: str,
    target: dict[str, Any],
    checkouts_dir: Path,
    action: str,
) -> dict[str, Any]:
    """Hand the step over with a message, inside the caller's transaction; `action` names the step event.

    The user's own message (`hand_off`), or Cockpit's after the user pressed Run or Allow on the step. The user's
    message carries their unsent page comments on the step after their words (SPEC NEXT-38), and marks them sent.
    """
    now = datetime.now(UTC)
    step = get_step(conn, step_id)
    check_open_for_hand_off(step)
    comments = db.rows(conn.execute(
        "SELECT * FROM page_comments WHERE step_id = ? AND state = 'unsent' ORDER BY page_id, id", (step_id,)
    ).fetchall()) if author == "user" else []
    choices = db.rows(conn.execute(
        "SELECT * FROM page_choices WHERE step_id = ? AND message_id IS NULL ORDER BY page_id, name", (step_id,)
    ).fetchall()) if author == "user" else []
    added = []
    if comments:
        count = f"{len(comments)} page comment{'s' if len(comments) > 1 else ''}"
        added += [f"{user_name()}'s {count} {COMMENTS_HEADER_END}", _comment_lines(conn, comments)]
    if choices:
        added += [f"{user_name()}'s choices {CHOICES_HEADER_END}", _choice_lines(conn, choices)]
    page_answers = "\n\n".join(added) or None
    text = "\n\n".join(part for part in (text.strip(), page_answers) if part)
    message = create_message(conn, author, text, target | {"item_id": step["item_id"], "step_id": step_id}, checkouts_dir)
    conn.execute("UPDATE messages SET hands_off = 1, page_answers = ? WHERE id = ?", (page_answers, message["id"]))
    if comments:
        conn.execute(
            f"UPDATE page_comments SET state = 'sent', message_id = ?, updated_at = ? WHERE id IN ({','.join('?' * len(comments))})",
            (message["id"], _iso(now), *[comment["id"] for comment in comments]),
        )
    if choices:
        conn.execute(
            "UPDATE page_choices SET message_id = ? WHERE step_id = ? AND message_id IS NULL", (message["id"], step_id)
        )
    conn.execute(
        "UPDATE steps SET handoff_message_id = ?, queued_at = NULL, held_at = NULL, updated_at = ?, updated_by = 'user'"
        " WHERE id = ?",
        (message["id"], _iso(now), step_id),
    )
    _stop_timer(conn, step_id, action, _iso(now))
    patterns.record_step(conn, action, "user", get_step(conn, step_id))
    _resort_after_close(conn, now, _in_queue(step, now))
    return get_message(conn, message["id"])


# The user's Archive on a session's last open step (SPEC SESSION-4). Cockpit tells the session, which reads it
# once its turn is over and finishes up; then Cockpit stops it (delivery.archives_due). A stopped session
# whose turn ended done leaves the main view of `claude agents`, and Cockpit leaves it out of Sessions. Its
# conversation stays, and a message brings it back: the sweep checks each archive and sends that message
# when work is left (recent_archives).
ARCHIVE_PENDING = "a.stopped_at IS NULL AND a.cancelled_at IS NULL AND a.revived_at IS NULL AND a.failure = ''"


def archive_text(step_id: int) -> str:
    return (
        f"{user_name()} archived step {step_id}, your last open step on Cockpit, so Cockpit archives this session."
        " Once this turn is over, Cockpit stops the session, and it leaves the main view of Cockpit and of"
        " `claude agents`. The conversation stays, and a later message brings the session back.\n\n"
        "Use this turn to finish up. Commit and push your work, and bring your Cockpit item up to date. End the"
        f" turn without a question for {user_name()}.\n\n"
        "When work that needs this session is left, such as a PR waiting on CI or a review, say so in your item's"
        " summary. The sweep checks every archived session and sends it a message when its work needs it. Put a"
        f" step if {user_name()} has to act on it."
    )


def pending_archive(conn: sqlite3.Connection, session_id: str) -> dict[str, Any] | None:
    return db.row(
        conn.execute(
            f"SELECT a.* FROM session_archives a WHERE a.session_id = ? AND {ARCHIVE_PENDING} ORDER BY a.id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    )


def get_archive(conn: sqlite3.Connection, archive_id: int) -> dict[str, Any]:
    archive = db.row(conn.execute("SELECT * FROM session_archives WHERE id = ?", (archive_id,)).fetchone())
    if archive is None:
        raise NotFound(f"no session archive {archive_id}")
    return archive


def archive_session(
    conn: sqlite3.Connection, session: Session, step: dict[str, Any], checkouts_dir: Path, text: str,
    told: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Record the user's archive of the session, inside the caller's transaction. `told` is a message the
    session gets anyway that says so (a closing answer, SPEC CONTROL-15): Cockpit stops the session once its
    turn on that message is over, and returns None. Otherwise a running session, or a stopped one whose turn
    did not end done (it still shows in `claude agents`), gets Cockpit's message with `text`: returns it,
    queued to wait for the session's turn. A stopped session whose turn ended done is archived as it is.
    Returns None then, and when an archive of the session is already pending."""
    if pending_archive(conn, session.session_id) is not None:
        return None
    now = db.now_iso()
    message = None
    if told is None and (session.alive or session.state != "done"):
        message = cockpit_prompt(conn, session.session_id, step["id"], text, checkouts_dir)
    waits_on = told or message
    conn.execute(
        "INSERT INTO session_archives (session_id, step_id, message_id, asked_at, stopped_at) VALUES (?, ?, ?, ?, ?)",
        (session.session_id, step["id"], waits_on["id"] if waits_on else None, now, None if waits_on else now),
    )
    patterns.record(
        conn, "session.archived", "user", step["item_id"], step["id"], step["need"],
        {"session_id": session.session_id, "told": waits_on is not None},
    )
    return message


def cockpit_prompt(
    conn: sqlite3.Connection, session_id: str, step_id: int, text: str, checkouts_dir: Path
) -> dict[str, Any]:
    """Cockpit's message to a session about the user's action on a step. Like the user's own messages, it waits
    for the session's turn to end, so the session reads it before Cockpit stops it."""
    message = create_message(conn, "cockpit", text, {"session_id": session_id, "step_id": step_id}, checkouts_dir)
    conn.execute("UPDATE messages SET as_prompt = 1 WHERE id = ?", (message["id"],))
    return get_message(conn, message["id"])


# Complete task and Close item (SPEC CONTROL-15): the user's one action that ends an item. Complete moves it
# to stage done, Close archives it with the user's reason; both close every open step of the item, and the
# item's lamp leaves the status lights. The step's session hears it and, on its last open step, is archived
# as under SESSION-4. Each finish keeps what it changed, so Undo (`reopen_item`) puts it all back.
FINISH_HOWS = ("complete", "close")
# The most tasks one step lists to close (SPEC CONTROL-23).
CLOSES_MAX = 50


def finish_text(how: str, item: dict[str, Any], reason: str, archiving: bool) -> str:
    """Cockpit's message to the session when the user completed or closed its item without an answer."""
    did = (
        f"{user_name()} completed item {item['id']}, {item['title']}, on Cockpit. Cockpit closed its steps and moved"
        " it to stage done."
        if how == "complete"
        else f"{user_name()} closed item {item['id']}, {item['title']}, on Cockpit. Cockpit closed its steps and"
        f" archived the item. {user_name()}'s reason: {reason}"
    )
    then = (
        " Once this turn is over, Cockpit stops and archives this session, and it leaves the main view of Cockpit"
        " and of `claude agents`. The conversation stays, and a later message brings the session back."
        if archiving
        else ""
    )
    return (
        f"{did}{then}\n\n"
        f"Use this turn to finish up. Commit and push your work, and end the turn without a question for {user_name()}."
        " If work on the item is left after all, such as a PR still waiting on CI or a release, put the item back"
        " at the stage the work is in. Say what is left in its summary, and put a step if"
        f" {user_name()} has to act on it."
    )


def closed_tasks_text(
    conn: sqlite3.Connection, step: dict[str, Any], finished: list[int], refused: list[dict[str, Any]]
) -> str:
    """Cockpit's message to the session whose list of tasks to close (SPEC CONTROL-23) the user pressed: which
    tasks closed, which Cockpit refused and why, and which the user left unticked."""
    def named(item_id: int) -> str:
        found = conn.execute("SELECT title FROM items WHERE id = ?", (item_id,)).fetchone()
        return f"item {item_id}, {found['title']}" if found else f"item {item_id}, no longer on Cockpit"

    done = set(finished) | {entry["item_id"] for entry in refused}
    parts = [f"{user_name()} pressed Close on your list of tasks on step {step['id']}, and Cockpit completed these:"]
    parts += [f"- {named(item_id)}" for item_id in finished]
    if refused:
        parts += ["Cockpit kept these open, since they changed after you listed them:"]
        parts += [f"- {named(entry['item_id'])}: {entry['error']}" for entry in refused]
    kept = [entry["item_id"] for entry in step["closes"] or [] if entry["item_id"] not in done]
    if kept:
        parts += [f"{user_name()} unticked these, so they stay open:"]
        parts += [f"- {named(item_id)}" for item_id in kept]
    parts.append(
        f"Each completed task's session heard it from Cockpit. Cockpit closed step {step['id']}. Undo on"
        f" {user_name()}'s page reopens the tasks and the step. End this turn without a question for {user_name()}."
    )
    return "\n".join(parts)


def undo_closed_tasks(conn: sqlite3.Connection, step_id: int, checkouts_dir: Path) -> dict[str, Any] | None:
    """Undo of a list of tasks to close (SPEC CONTROL-23) reopens its step last: Cockpit's message that named the
    closed tasks is set aside while it still waits, or, once the session has it, a second message says the user
    undid it. Returns that second message, to deliver now, or None."""
    told = conn.execute(
        "SELECT id, state, session_id FROM messages WHERE step_id = ? AND author = 'cockpit' AND as_prompt = 1"
        " ORDER BY id DESC LIMIT 1",
        (step_id,),
    ).fetchone()
    if told is None:
        return None
    now = _iso(datetime.now(UTC))
    if told["state"] == "queued":
        _set_aside_unsent(conn, told["id"], now)
        return None
    if told["state"] not in ("sending", "sent") or told["session_id"] is None:
        return None
    return cockpit_prompt(
        conn, told["session_id"], step_id,
        f"{user_name()} undid the close of your list on step {step_id}: the tasks on it are open again, and so is"
        " the step. Carry on as before.",
        checkouts_dir,
    )


def get_finish(conn: sqlite3.Connection, item_id: int) -> dict[str, Any] | None:
    """The item's latest finish that still holds, or None."""
    return db.row(
        conn.execute(
            "SELECT * FROM item_finishes WHERE item_id = ? AND reopened_at IS NULL ORDER BY id DESC LIMIT 1", (item_id,)
        ).fetchone()
    )


def _finish_archive(conn: sqlite3.Connection, finish: dict[str, Any]) -> dict[str, Any] | None:
    """The session archive the finish asked for, unless it was called off."""
    return db.row(
        conn.execute(
            "SELECT * FROM session_archives WHERE step_id = ? AND cancelled_at IS NULL AND asked_at >= ?"
            " ORDER BY id DESC LIMIT 1",
            (finish["step_id"], finish["at"]),
        ).fetchone()
    )


def _retire_finish(conn: sqlite3.Connection, finish: dict[str, Any], now: str, why: str) -> None:
    """The item's work went on after `finish`: its stage moved back or it was unarchived and the user finishes it
    again, or the user's message took it up again. Undo no longer puts the old finish back. Its archive, while still
    to happen, is called off, and Cockpit's message that announced it is set aside while it still waits, so a new
    finish archives the session and tells it, and that finish's Undo calls both off. `why` ends "Not sent: <user>
    ..." and the dismissal's reason."""
    conn.execute("UPDATE item_finishes SET reopened_at = ? WHERE id = ?", (now, finish["id"]))
    archive = _finish_archive(conn, finish)
    if archive is not None and archive["stopped_at"] is None and archive["revived_at"] is None and not archive["failure"]:
        conn.execute("UPDATE session_archives SET cancelled_at = ? WHERE id = ?", (now, archive["id"]))
    if finish["message_id"] is None:
        return
    told = get_message(conn, finish["message_id"])
    if told["author"] != "cockpit":
        return  # the user's closing answer goes as the user sent it
    updated = conn.execute(
        "UPDATE messages SET state = 'failed', detail = ?, dismissed_at = ?, dismissed_by = 'user', dismiss_reason = ?,"
        " take_back_until = NULL WHERE id = ? AND state = 'queued'",
        (
            f"Not sent: {user_name()} {why}.",
            now,
            f"{user_name()} {why}",
            told["id"],
        ),
    )
    if updated.rowcount:
        patterns.record(
            conn, "message.dismissed", "user", told["item_id"], told["step_id"], None,
            {"message_id": told["id"], "reason": f"{user_name()} {why}"},
        )


def finish_item(
    conn: sqlite3.Connection, item_id: int, step_id: int, how: str, answer: str | None, note: str, reason: str,
    seen_session: str | None, session: Session | None, checkouts_dir: Path,
) -> dict[str, Any]:
    """The user completes or closes the item (SPEC CONTROL-15) from its open step `step_id`.

    `answer` is the text of one of the step's closing answers, and `note` the last message the user typed in
    the step's reply box before Task done ("" for none): either goes to the step's session as the user's
    message, which waits for Take back like any hand-off (SPEC MSG-8) and tells the session that it completed
    the item. Close needs a reason and takes neither. `seen_session` is the step's session as the user's page
    showed it, checked again inside the transaction (`check_seen_target`), so the answer and the archive reach the
    session shown. `session` is the step's session as Cockpit lists it, None when there is none to tell (or it
    is the sweep's): with no other open steps it is archived as under
    SESSION-4, after its turn on the answer or on Cockpit's message; otherwise Cockpit's message only tells it.
    Returns {item, steps (the ids it closed), message (the answer), send (Cockpit's message to deliver now),
    session_end ({name, how: archiving|kept, other_steps}, or None)}.
    """
    if how not in FINISH_HOWS:
        raise Invalid(f"how is one of {', '.join(FINISH_HOWS)}")
    if how == "close" and not reason.strip():
        raise Invalid("closing an item needs a reason")
    if (answer is not None or note.strip()) and how != "complete":
        raise Invalid("a closing answer or last message completes the item; Close item takes a reason instead")
    if answer is not None and note.strip():
        raise Invalid("a closing answer is the last message; send one or the other")
    now_moment = datetime.now(UTC)
    now = _iso(now_moment)
    with db.transaction(conn):
        item = get_item(conn, item_id)
        earlier = get_finish(conn, item_id)
        if item["archived"] or (earlier is not None and earlier["how"] == "complete" and item["stage"] == "done"):
            raise Conflict(f"item {item_id} is already {'closed' if item['archived'] else 'complete'}")
        if earlier is not None:
            _retire_finish(conn, earlier, now, "finished the item again, and that finish tells the session")
        step = get_step(conn, step_id)
        if step["item_id"] != item_id:
            raise Invalid(f"step {step_id} belongs to item {step['item_id']}, not item {item_id}")
        if step["status"] != "open" or step["withdrawn_at"]:
            raise Conflict(f"step {step_id} is not open")
        check_seen_target(step_id, target_session(conn, item_id, step_id), seen_session)
        closing = db.rows(
            conn.execute(
                "SELECT id, queued_at, withdrawn_at, handoff_message_id, snoozed_until FROM steps"
                " WHERE item_id = ? AND status = 'open'"
                " ORDER BY id",
                (item_id,),
            ).fetchall()
        )
        message = None
        if answer is not None and not any(choice["text"] == answer and choice["closes"] for choice in step["answers"]):
            raise Invalid(f"step {step_id} has no closing answer {answer!r}")
        said = answer if answer is not None else note.strip()
        # Task done with no words still answers the step, so the page comments and choices waiting for that answer
        # go with it, and the session reads them (SPEC NEXT-38, NEXT-48). With no session to read them, or
        # with the step already with its session, they stay on the pages and the finish goes on.
        carries = (
            how == "complete" and step["handoff_message_id"] is None and target_session(conn, item_id, step_id) is not None
            and _takes_page_answers(conn, step_id)
        )
        if said or carries:
            target = {"session_id": None, "new_cwd": None}
            message = hand_off(conn, step_id, "user", said, target, checkouts_dir, "hand_off")
            until = _iso(now_moment + TAKE_BACK_LIMIT)
            conn.execute("UPDATE messages SET take_back_until = ?, completes = 1 WHERE id = ?", (until, message["id"]))
            message = get_message(conn, message["id"])
        status = "done" if how == "complete" else "dismissed"
        closed_ids = [record["id"] for record in closing]
        for closed in closed_ids:
            conn.execute(
                "UPDATE steps SET status = ?, closed_at = ?, handoff_message_id = NULL, withdrawn_at = NULL,"
                " queued_at = NULL, updated_at = ?, updated_by = 'user' WHERE id = ?",
                (status, now, now, closed),
            )
            action = "done" if how == "complete" else "dismiss"
            _stop_timer(conn, closed, action, now)
            patterns.record_step(conn, action, "user", get_step(conn, closed))
        before = {name: item[name] for name in ("stage", "waiting_on", "archived", "user_fields")}
        if how == "complete":
            conn.execute(
                "UPDATE items SET done_at = CASE WHEN stage = 'done' THEN done_at ELSE ? END, stage = 'done',"
                " waiting_on = '[]', updated_at = ?, updated_by = 'user' WHERE id = ?",
                (now, now, item_id),
            )
        else:
            chosen = sorted(set(item["user_fields"]) | {"archived"})
            conn.execute(
                "UPDATE items SET archived = 1, archived_at = ?, archived_by = 'user', user_fields = ?, updated_at = ?,"
                " updated_by = 'user' WHERE id = ?",
                (now, json.dumps(chosen), now, item_id),
            )
        send = None
        session_end = None
        if session is not None:
            others = other_open_steps(conn, session.session_id, step_id)
            name = shown_session_names(conn, [session])[session.session_id]
            if others:
                if message is None:
                    send = cockpit_prompt(
                        conn, session.session_id, step_id, finish_text(how, item, reason.strip(), False), checkouts_dir
                    )
                session_end = {"name": name, "how": "kept", "other_steps": others}
            else:
                send = archive_session(
                    conn, session, step, checkouts_dir, finish_text(how, item, reason.strip(), True), message
                )
                session_end = {"name": name, "how": "archiving", "other_steps": 0}
        told = message or send
        conn.execute(
            "INSERT INTO item_finishes (item_id, step_id, how, reason, message_id, before_fields, closed_steps, at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item_id, step_id, how, reason.strip(), told["id"] if told else None,
                json.dumps(before), json.dumps(closing), now,
            ),
        )
        patterns.record(
            conn, "item.completed" if how == "complete" else "item.closed", "user", item_id, step_id, step["need"],
            {"reason": reason.strip(), "answer": answer, "note": note.strip(), "steps": closed_ids},
        )
        _resort_after_close(conn, now_moment, any(_in_queue(other, now_moment) for other in closing))
    release_held(conn)
    return {
        "item": get_item(conn, item_id), "steps": closed_ids, "message": message, "send": send, "session_end": session_end,
    }


def reopen_item(conn: sqlite3.Connection, item_id: int, checkouts_dir: Path) -> dict[str, Any]:
    """Undo the user's latest Complete task or Close item on the item: its stage, waiting list and archive go
    back to what they were, the steps it closed reopen in their places, and the session's archive is called
    off. A message that told the session and still waits (for Take back or its turn) is set aside; one that
    reached it gets a second message from Cockpit that says so. Returns {finish, send}: `send` is that
    second message, to deliver now, or None."""
    now_moment = datetime.now(UTC)
    now = _iso(now_moment)
    with db.transaction(conn):
        finish = get_finish(conn, item_id)
        if finish is None:
            raise Conflict(f"item {item_id} has no complete or close to undo")
        before = finish["before_fields"]
        # The finish chose only `archived` for the user; a field the user set since (a priority) stays theirs.
        user_fields = sorted(set(get_item(conn, item_id)["user_fields"]) - ({"archived"} - set(before["user_fields"])))
        conn.execute(
            "UPDATE items SET done_at = CASE WHEN ? = 'done' AND stage IS NOT 'done' THEN ? ELSE done_at END, stage = ?,"
            " waiting_on = ?, archived = ?, user_fields = ?, updated_at = ?, updated_by = 'user' WHERE id = ?",
            (
                before["stage"], now, before["stage"], json.dumps(before["waiting_on"]), before["archived"],
                json.dumps(user_fields), now, item_id,
            ),
        )
        restored = []
        for closed in finish["closed_steps"]:
            was = closed if isinstance(closed, dict) else {"id": closed}
            step_id = was["id"]
            step = get_step(conn, step_id)
            if step["status"] == "open" or step["closed_at"] != finish["at"]:
                continue
            conn.execute(
                "UPDATE steps SET status = 'open', closed_at = NULL, queued_at = ?, withdrawn_at = ?, handoff_message_id = ?"
                " WHERE id = ?",
                (was.get("queued_at"), was.get("withdrawn_at"), was.get("handoff_message_id"), step_id),
            )
            restored.append(step_id)
            patterns.record_step(conn, "reopen", "user", get_step(conn, step_id))
        # All at once: one restored step's place must not count as a later place against another's.
        _give_back_places(conn, restored, rank.parse_time(finish["at"]), now_moment)  # ty: ignore[invalid-argument-type]
        archive = _finish_archive(conn, finish)
        if archive is not None:
            conn.execute("UPDATE session_archives SET cancelled_at = ? WHERE id = ?", (now, archive["id"]))
        send = None
        if finish["message_id"] is not None:
            told = get_message(conn, finish["message_id"])
            if told["state"] == "queued":
                _set_aside_unsent(conn, told["id"], now)
            elif told["state"] in ("sending", "sent") and told["session_id"] is not None:
                done = "completed" if finish["how"] == "complete" else "closed"
                item = get_item(conn, item_id)
                send = cockpit_prompt(
                    conn, told["session_id"], finish["step_id"],
                    f"{user_name()} undid the {done} item {item_id}, {item['title']}. The item is back at stage"
                    f" {item['stage']} and its steps are open again. Cockpit no longer archives this session."
                    " Carry on as before.",
                    checkouts_dir,
                )
        conn.execute("UPDATE item_finishes SET reopened_at = ? WHERE id = ?", (now, finish["id"]))
        patterns.record(conn, "item.reopened", "user", item_id, finish["step_id"], None, {"how": finish["how"]})
        fill_queue(conn, now_moment)
    return {"finish": finish, "send": send}


def reopen_for_message(conn: sqlite3.Connection, item_id: int) -> bool:
    """The user's message on a finished task takes it up again (SPEC CONTROL-28): the task leaves Finished this week
    for Busy, at stage building with nothing to wait on, whatever stage it ended at, and its session answers on it
    as on any open task (CONTROL-27 refuses a step only while the task is closed). The steps the finish closed stay
    closed, and asks a close left open are withdrawn, so the task is the agents' and not back in the Inbox: the message
    is the new ask, and the session puts again what it still needs. Undo no longer puts the finish back. No change to
    an open task. Returns whether it reopened."""
    item = get_item(conn, item_id)
    if not item["archived"] and item["stage"] != "done":
        return False
    now = db.now_iso()
    conn.execute(
        "UPDATE items SET stage = 'building', waiting_on = '[]', archived = 0, user_fields = ?, updated_at = ?,"
        " updated_by = 'user' WHERE id = ?",
        (json.dumps([name for name in item["user_fields"] if name != "archived"]), now, item_id),
    )
    asks = db.rows(conn.execute(
        "SELECT * FROM steps WHERE item_id = ? AND status = 'open' AND withdrawn_at IS NULL"
        " AND handoff_message_id IS NULL AND (snoozed_until IS NULL OR snoozed_until <= ?)",
        (item_id, now),
    ).fetchall())
    for ask in asks:
        _stop_timer(conn, ask["id"], "withdraw", now)
        # An agent's change that waited for the step's timer would put the ask back when the timer stops.
        conn.execute("DELETE FROM held_changes WHERE step_id = ?", (ask["id"],))
        conn.execute(
            "UPDATE steps SET withdrawn_at = ?, queued_at = NULL, held_at = NULL, snoozed_until = NULL WHERE id = ?",
            (now, ask["id"]),
        )
        patterns.record_step(conn, "withdraw", "user", get_step(conn, ask["id"]))
    finish = get_finish(conn, item_id)
    if finish is not None:
        _retire_finish(conn, finish, now, "took the item up again with a message")
    patterns.record(
        conn, "item.reopened", "user", item_id, finish["step_id"] if finish else None, None,
        {"how": finish["how"] if finish else "agent", "by": "message"},
    )
    if asks:
        fill_queue(conn, datetime.now(UTC))
    return True


def cancel_archive(conn: sqlite3.Connection, step_id: int, checkouts_dir: Path) -> dict[str, Any] | None:
    """The user reopened the step their Archive closed, so the archive no longer holds: a stopped session shows
    in Sessions again, and one still to be stopped keeps running. When Cockpit's message already reached the
    session, a second one says so, waiting behind the first; returns that one to send."""
    with db.transaction(conn):
        archive = db.row(
            conn.execute(
                "SELECT * FROM session_archives WHERE step_id = ? AND cancelled_at IS NULL ORDER BY id DESC LIMIT 1",
                (step_id,),
            ).fetchone()
        )
        if archive is None:
            return None
        now = db.now_iso()
        conn.execute("UPDATE session_archives SET cancelled_at = ? WHERE id = ?", (now, archive["id"]))
        step = get_step(conn, step_id)
        patterns.record(
            conn, "session.archive_cancelled", "user", step["item_id"], step_id, step["need"],
            {"session_id": archive["session_id"]},
        )
        if archive["message_id"] is None or archive["stopped_at"] or archive["failure"]:
            return None
        if get_message(conn, archive["message_id"])["state"] == "queued":
            _set_aside_unsent(conn, archive["message_id"], now)
            return None
        message = create_message(
            conn, "cockpit",
            f"{user_name()} reopened step {step_id}, so Cockpit no longer archives this session. Carry on as before.",
            {"session_id": archive["session_id"], "step_id": step_id}, checkouts_dir,
        )
        conn.execute("UPDATE messages SET as_prompt = 1 WHERE id = ?", (message["id"],))
        return get_message(conn, message["id"])


def end_archive(conn: sqlite3.Connection, archive_id: int, failure: str) -> None:
    """Cockpit stopped the session (`failure` empty) or left it running and says why."""
    with db.transaction(conn):
        archive = get_archive(conn, archive_id)
        conn.execute(
            "UPDATE session_archives SET stopped_at = ?, failure = ? WHERE id = ?",
            (None if failure else db.now_iso(), failure, archive_id),
        )
        step = get_step(conn, archive["step_id"])
        patterns.record(
            conn, "session.archive_failed" if failure else "session.stopped", "cockpit", step["item_id"], step["id"],
            step["need"], {"session_id": archive["session_id"], "failure": failure},
        )


def session_starts(conn: sqlite3.Connection, session_ids: set[str]) -> dict[str, dict[str, Any]]:
    """The message each session started with, by session id: its author, the name it gave the session, its item
    and when it was written. A session started outside Cockpit has none."""
    marks = ",".join("?" * len(session_ids))
    return {
        start["session_id"]: start
        for start in db.rows(conn.execute(
            "SELECT session_id, author, new_name, item_id, created_at FROM messages WHERE id IN"
            f" (SELECT MIN(id) FROM messages WHERE new_cwd IS NOT NULL AND session_id IN ({marks}) GROUP BY session_id)",
            sorted(session_ids),
        ).fetchall())
    }


def shown_session_names(conn: sqlite3.Connection, sessions: list[Session]) -> dict[str, str]:
    """Each session's name as the page and `cockpit get` show it, by session id (SPEC SESSION-3): a session Cockpit
    started on an item, still under the name Cockpit gave it, goes by the item's title now, cut as at the start. A
    to-do's session starts under the user's whole first words, the title the to-do has then, and `claude agents` keeps
    that name; once an agent gives the item its short title, the session shows that. A session renamed since keeps
    its own name. Only for showing: checks such as which session is the sweep go by `Session.name`."""
    starts = session_starts(conn, {session.session_id for session in sessions}) if sessions else {}
    item_ids = sorted({start["item_id"] for start in starts.values() if start["item_id"] is not None})
    titles = dict(conn.execute(
        f"SELECT id, title FROM items WHERE id IN ({','.join('?' * len(item_ids))})", item_ids
    ).fetchall()) if item_ids else {}

    def shown(session: Session) -> str:
        start = starts.get(session.session_id)
        if start is None:
            return session.name
        title = titles.get(start["item_id"])
        if not title or not start["new_name"] or session.name != start["new_name"]:
            return session.name
        return title[:NEW_SESSION_NAME_CHARS]

    return {session.session_id: shown(session) for session in sessions}


def archive_states(conn: sqlite3.Connection, alive: set[str]) -> dict[str, dict[str, Any]]:
    """Each session's latest archive that still holds, by session id. `state` is archiving (Cockpit's
    message waits, or the session's turn on it runs), archived (stopped; Sessions leaves it out) or failed
    (with why). A session running again after its archive is back."""
    states: dict[str, dict[str, Any]] = {}
    for archive in db.rows(
        conn.execute(
            "SELECT * FROM session_archives WHERE cancelled_at IS NULL AND revived_at IS NULL ORDER BY id"
        ).fetchall()
    ):
        if archive["failure"]:
            state = "failed"
        elif archive["stopped_at"]:
            state = "archived"
        else:
            state = "archiving"
        states[archive["session_id"]] = {
            "state": state, "step_id": archive["step_id"], "failure": archive["failure"],
        }
    return {
        session_id: entry for session_id, entry in states.items()
        if not (entry["state"] == "archived" and session_id in alive)
    }


def recent_archives(conn: sqlite3.Connection, now: datetime) -> list[dict[str, Any]]:
    """The user's archives of the last ARCHIVE_CHECK_HOURS, newest first, for the sweep to check that each makes
    sense (SPEC SESSION-4)."""
    return db.rows(
        conn.execute(
            "SELECT a.*, s.ask AS step_ask, s.item_id FROM session_archives a JOIN steps s ON s.id = a.step_id"
            " WHERE a.asked_at >= ? ORDER BY a.id DESC",
            (_iso(now - timedelta(hours=ARCHIVE_CHECK_HOURS)),),
        ).fetchall()
    )


def get_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    run = db.row(conn.execute("SELECT * FROM command_runs WHERE id = ?", (run_id,)).fetchone())
    if run is None:
        raise NotFound(f"no command run {run_id}")
    return run


def latest_runs(conn: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    """Each step's newest command run, by step id."""
    return {
        run["step_id"]: run
        for run in db.rows(
            conn.execute(
                "SELECT * FROM command_runs WHERE id IN (SELECT MAX(id) FROM command_runs GROUP BY step_id)"
            ).fetchall()
        )
    }


def goal_list(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The user's goals that are not dropped, most important first (`position` 1, 2, ...), paused ones in their place."""
    return db.rows(
        conn.execute(
            "SELECT id, text, position, created_at, paused_at FROM goals WHERE dropped_at IS NULL ORDER BY position, id"
        ).fetchall()
    )


def get_goal(conn: sqlite3.Connection, goal_id: int) -> dict[str, Any]:
    goal = db.row(conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone())
    if goal is None:
        raise NotFound(f"goal {goal_id} does not exist")
    return goal


def _held_goal(conn: sqlite3.Connection, goal_id: int) -> dict[str, Any]:
    goal = get_goal(conn, goal_id)
    if goal["dropped_at"]:
        raise Conflict(f"goal {goal_id} was dropped at {goal['dropped_at']}")
    return goal


def _goal_order(conn: sqlite3.Connection, without: int) -> list[int]:
    """The ids of the goals that are not dropped, in order, less `without`."""
    return [
        record["id"]
        for record in conn.execute(
            "SELECT id FROM goals WHERE dropped_at IS NULL AND id != ? ORDER BY position, id", (without,)
        ).fetchall()
    ]


def _number_goals(conn: sqlite3.Connection, ids: list[int]) -> None:
    conn.executemany("UPDATE goals SET position = ? WHERE id = ?", list(enumerate(ids, start=1)))


def _put_goal_at(conn: sqlite3.Connection, goal_id: int, position: int) -> None:
    """Place the goal at `position` among the goals that are not dropped (past the end: last), and number them."""
    ids = _goal_order(conn, goal_id)
    ids.insert(min(position, len(ids) + 1) - 1, goal_id)
    _number_goals(conn, ids)


def add_goal(conn: sqlite3.Connection, text: str) -> dict[str, Any]:
    """The user adds a goal at the top of the list. It holds until the user pauses or drops it, and wakes the sweeper."""
    if not text.strip():
        raise Invalid("the goal is empty")
    with db.transaction(conn):
        cursor = conn.execute(
            "INSERT INTO goals (text, position, created_at) VALUES (?, 0, ?)", (text.strip(), db.now_iso())
        )
        goal_id: int = cursor.lastrowid  # ty: ignore[invalid-assignment]
        _put_goal_at(conn, goal_id, 1)
        patterns.record(conn, "goal.added", "user", None, None, None, {"goal_id": goal_id})
        return get_goal(conn, goal_id)


def set_goals(conn: sqlite3.Connection, texts: list[str]) -> list[dict[str, Any]]:
    """The user writes the whole list at once, one goal per entry, most important first (UI-65).

    Every goal in the list holds: the box shows no pause, so a paused goal the list keeps is resumed. A goal whose text
    stays keeps its id and items, wherever it moves. A changed entry rewords the goal that
    stood in that place, when that goal's text appears nowhere in the new list; an entry moved and changed at once is
    a new goal, so items never follow a goal to words it did not have. The goals left over are dropped,
    the entries left over are added. Each change is recorded as its own goal event, so the sweeper reads it.
    """
    wanted = [text.strip() for text in texts if text.strip()]
    with db.transaction(conn):
        current = goal_list(conn)
        kept: list[dict[str, Any] | None] = [None] * len(wanted)
        unused = list(current)
        for index, text in enumerate(wanted):
            same = next((goal for goal in unused if goal["text"] == text), None)
            if same is not None:
                kept[index] = same
                unused.remove(same)
        for index, text in enumerate(wanted):
            in_place = next((goal for goal in unused if goal["position"] == index + 1), None)
            if kept[index] is None and in_place is not None:
                conn.execute("UPDATE goals SET text = ? WHERE id = ?", (text, in_place["id"]))
                patterns.record(conn, "goal.edited", "user", None, None, None, {"goal_id": in_place["id"]})
                kept[index] = in_place
                unused.remove(in_place)
        for goal in unused:
            conn.execute("UPDATE goals SET dropped_at = ? WHERE id = ?", (db.now_iso(), goal["id"]))
            patterns.record(conn, "goal.dropped", "user", None, None, None, {"goal_id": goal["id"]})
        ids: list[int] = []
        for index, text in enumerate(wanted):
            goal = kept[index]
            if goal is None:
                cursor = conn.execute("INSERT INTO goals (text, position, created_at) VALUES (?, 0, ?)", (text, db.now_iso()))
                added: int = cursor.lastrowid  # ty: ignore[invalid-assignment]
                patterns.record(conn, "goal.added", "user", None, None, None, {"goal_id": added, "place": index + 1})
                ids.append(added)
            else:
                if goal["position"] != index + 1:
                    patterns.record(
                        conn, "goal.moved", "user", None, None, None,
                        {"goal_id": goal["id"], "from": goal["position"], "to": index + 1},
                    )
                if goal["paused_at"] is not None:
                    conn.execute("UPDATE goals SET paused_at = NULL WHERE id = ?", (goal["id"],))
                    patterns.record(conn, "goal.resumed", "user", None, None, None, {"goal_id": goal["id"]})
                ids.append(goal["id"])
        _number_goals(conn, ids)
        return goal_list(conn)


def reword_goal(conn: sqlite3.Connection, goal_id: int, text: str) -> dict[str, Any]:
    """The user rewords a goal; it keeps its place, its items and its pause."""
    if not text.strip():
        raise Invalid("the goal is empty")
    with db.transaction(conn):
        goal = _held_goal(conn, goal_id)
        if goal["text"] == text.strip():
            return goal
        conn.execute("UPDATE goals SET text = ? WHERE id = ?", (text.strip(), goal_id))
        patterns.record(conn, "goal.edited", "user", None, None, None, {"goal_id": goal_id})
        return get_goal(conn, goal_id)


def move_goal(conn: sqlite3.Connection, goal_id: int, position: int) -> dict[str, Any]:
    """The user moves a goal to `position` (1 is the top; past the end, the bottom); the others close up around it."""
    with db.transaction(conn):
        goal = _held_goal(conn, goal_id)
        _put_goal_at(conn, goal_id, position)
        moved = get_goal(conn, goal_id)
        if moved["position"] != goal["position"]:
            patterns.record(
                conn, "goal.moved", "user", None, None, None,
                {"goal_id": goal_id, "from": goal["position"], "to": moved["position"]},
            )
        return moved


def pause_goal(conn: sqlite3.Connection, goal_id: int) -> dict[str, Any]:
    """The user sets a goal aside: it keeps its place, and the sweeper and the ranking leave it out until the user resumes it."""
    with db.transaction(conn):
        goal = _held_goal(conn, goal_id)
        if goal["paused_at"]:
            raise Conflict(f"goal {goal_id} was already paused at {goal['paused_at']}")
        conn.execute("UPDATE goals SET paused_at = ? WHERE id = ?", (db.now_iso(), goal_id))
        patterns.record(conn, "goal.paused", "user", None, None, None, {"goal_id": goal_id})
        return get_goal(conn, goal_id)


def resume_goal(conn: sqlite3.Connection, goal_id: int) -> dict[str, Any]:
    with db.transaction(conn):
        goal = _held_goal(conn, goal_id)
        if goal["paused_at"] is None:
            raise Conflict(f"goal {goal_id} is not paused")
        conn.execute("UPDATE goals SET paused_at = NULL WHERE id = ?", (goal_id,))
        patterns.record(conn, "goal.resumed", "user", None, None, None, {"goal_id": goal_id})
        return get_goal(conn, goal_id)


def drop_goal(conn: sqlite3.Connection, goal_id: int) -> dict[str, Any]:
    """The user takes back a goal that no longer holds. It keeps its last position, for the Undo."""
    with db.transaction(conn):
        _held_goal(conn, goal_id)
        conn.execute("UPDATE goals SET dropped_at = ? WHERE id = ?", (db.now_iso(), goal_id))
        _number_goals(conn, _goal_order(conn, goal_id))
        patterns.record(conn, "goal.dropped", "user", None, None, None, {"goal_id": goal_id})
        return get_goal(conn, goal_id)


def restore_goal(conn: sqlite3.Connection, goal_id: int) -> dict[str, Any]:
    """The user undoes a drop: the goal holds again in its old place, with its id, items and pause."""
    with db.transaction(conn):
        goal = get_goal(conn, goal_id)
        if goal["dropped_at"] is None:
            raise Conflict(f"goal {goal_id} was not dropped")
        conn.execute("UPDATE goals SET dropped_at = NULL WHERE id = ?", (goal_id,))
        _put_goal_at(conn, goal_id, goal["position"])
        patterns.record(conn, "goal.restored", "user", None, None, None, {"goal_id": goal_id})
        return get_goal(conn, goal_id)


def checkouts(checkouts_dir: Path) -> list[str]:
    """Where a new session may start: the checkouts directory itself first, then each git checkout in it."""
    found = []
    for path in checkouts_dir.iterdir():
        try:
            if path.is_dir() and (path / ".git").exists():
                found.append(str(path))
        except OSError as exc:
            log.error("checking checkout %s failed: %s", path, exc)
    return [str(checkouts_dir)] + sorted(found)


START_OPTION_KEYS = ("model", "effort", "account")


def still_offered(provider: str | None, chosen: dict[str, str | None]) -> tuple[str | None, dict[str, str | None], list[str]]:
    """Picks kept from earlier (an item's from Add) with each one the setup no longer offers left to the default, as
    the page's startPicks does: all of them for an agent since turned off. Also returns the picks it dropped."""
    if provider is not None and provider not in config.setup().providers:
        return None, {key: None for key in START_OPTION_KEYS}, [f"agent {provider}"]
    runs = provider or config.setup().default_provider
    offered = config.SESSION_OPTIONS[runs]
    allowed = {
        "model": [name for name, _ in offered["models"]],
        "effort": offered["efforts"],
        "account": [each.name for each in accounts.in_use(config.ACCOUNTS, [])] if runs == "claude" else [],
    }
    kept = {key: value if value is None or value in allowed[key] else None for key, value in chosen.items()}
    return provider, kept, [f"{key} {chosen[key]}" for key in START_OPTION_KEYS if chosen[key] != kept[key]]


def start_options(provider: str | None, chosen: dict[str, str | None]) -> tuple[str | None, dict[str, str | None] | None]:
    """The provider and the model, effort and account the user picked for a new session (SPEC SESSION-22), checked
    against what config.SESSION_OPTIONS offers for that provider: None for a choice left to the default, and for
    the options when every one is. A Codex session runs on the one Codex login Cockpit's app server uses."""
    if provider is not None and provider not in config.setup().providers:
        raise Invalid(f"provider {provider} is not enabled")
    if not any(chosen.values()):
        return provider, None
    runs = provider or config.setup().default_provider
    offered = config.SESSION_OPTIONS[runs]
    model, effort, account = (chosen[key] for key in START_OPTION_KEYS)
    if model is not None and model not in [name for name, _ in offered["models"]]:
        raise Invalid(f"{runs} offers no model {model}")
    if effort is not None and effort not in offered["efforts"]:
        raise Invalid(f"{runs} offers no effort {effort}")
    if account is not None:
        if runs != "claude":
            raise Invalid("only a Claude session picks its account")
        if account not in [each.name for each in config.ACCOUNTS]:
            raise Invalid(f"no Claude account {account}")
    return provider, {"model": model, "effort": effort, "account": account}


def create_message(
    conn: sqlite3.Connection,
    author: str,
    text: str,
    target: dict[str, Any],
    checkouts_dir: Path,
) -> dict[str, Any]:
    """Record a queued message with a resolved target; delivery happens afterwards. A message the user wrote
    arrives as the user's own prompt (SPEC MSG-3, delivery.py)."""
    if not text.strip():
        raise Invalid("the message is empty")
    if author == "cockpit":
        # Cockpit's own words carry a title, the user's reason or a check's output: the action goes, and a line in
        # them that reads as a sender header is marked.
        text = defused(text)
    elif author != "user" and FORGED_HEADER.search(text):
        raise Invalid("a message from an agent may not contain a line like Cockpit's sender header ('Message from … via Cockpit' or 'Message from Cockpit itself')")
    item_id = target.get("item_id")
    step_id = target.get("step_id")
    session_id = target.get("session_id")
    new_cwd = target.get("new_cwd")
    # Fork (SPEC MSG-11): the new session starts from this session's conversation, in its folder (`new_cwd`) and on
    # its account and model.
    fork_of = target.get("fork_of")
    provider = target.get("provider")
    chosen = {key: target.get(key) for key in START_OPTION_KEYS}
    if (provider is not None or any(chosen.values())) and (new_cwd is None or fork_of is not None):
        raise Invalid("provider, model, effort and account are only set when starting a new session, not a fork")
    if fork_of is not None and new_cwd is None:
        raise Invalid("a fork starts in the folder of the session it forks")
    new_name = None
    step_ask = None
    step_version = None
    if step_id is not None:
        step = get_step(conn, step_id)
        if item_id is not None and item_id != step["item_id"]:
            raise Invalid(f"step {step_id} belongs to item {step['item_id']}, not item {item_id}")
        item_id = step["item_id"]
        step_ask = step["ask"]
        step_version = conn.execute("SELECT MAX(id) FROM step_versions WHERE step_id = ?", (step_id,)).fetchone()[0]
    # A to-do's first session starts with what the user picked on Add, unless the start names its own; once a
    # session starts on the item (delivery's _after_send clears them), later ones start at the defaults (SPEC SESSION-22).
    if new_cwd is not None and fork_of is None and item_id is not None and provider is None and not any(chosen.values()):
        kept = conn.execute("SELECT start_options FROM items WHERE id = ?", (item_id,)).fetchone()
        if kept is not None and kept[0] is not None:
            stored = json.loads(kept[0])
            provider, chosen, dropped = still_offered(stored["provider"], {key: stored[key] for key in START_OPTION_KEYS})
            if dropped:
                log.error("item %s's picks from Add no longer offered start at the defaults: %s", item_id, ", ".join(dropped))
    provider, new_options = start_options(provider, chosen)
    if session_id is None and new_cwd is None:
        if item_id is None:
            raise Invalid("pick where the message goes: an item, a session, or a new session")
        session_id = target_session(conn, item_id, step_id)
        if session_id is None:
            raise Invalid("this item has no linked session; pick a session or start a new one")
    if new_cwd is not None:
        if session_id is not None:
            raise Invalid("a message goes to one session or a new session, not both")
        if fork_of is None and new_cwd not in checkouts(checkouts_dir):
            raise Invalid(f"{new_cwd} is not a checkout under {checkouts_dir}")
        title = get_item(conn, item_id)["title"] if item_id is not None else text.strip().splitlines()[0]
        new_name = title[:NEW_SESSION_NAME_CHARS]
    if item_id is not None:
        get_item(conn, item_id)
    cursor = conn.execute(
        "INSERT INTO messages (author, item_id, step_id, step_ask, step_version, session_id, new_cwd, new_name, text,"
        " state, created_at, as_prompt, provider, new_options, fork_of) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)",
        (
            author, item_id, step_id, step_ask, step_version, session_id, new_cwd, new_name, text, db.now_iso(),
            author == "user", provider, json.dumps(new_options) if new_options is not None else None, fork_of,
        ),
    )
    if session_id is not None:
        _revive_archives(conn, session_id)
    return get_message(conn, cursor.lastrowid)  # ty: ignore[invalid-argument-type]


def started_models(conn: sqlite3.Connection) -> dict[str, str]:
    """The model each session Cockpit started was started on, by session id: the one the user picked (SPEC
    SESSION-22), else config.MAIN_MODEL. The wrong-model warning counts it as the session's intended model (SESSION-20)."""
    return dict(conn.execute(
        "SELECT session_id, COALESCE(json_extract(new_options, '$.model'), ?) FROM messages WHERE via = 'new'"
        " AND session_id IS NOT NULL", (config.MAIN_MODEL,)
    ).fetchall())


WRONG_MODELS_DISMISSED_KEY = "wrong_models_dismissed"


def dismiss_wrong_model(
    conn: sqlite3.Connection, session_id: str, model: str, calls: int, now: datetime, keep_hours: int
) -> None:
    """The user's close on a wrong-model banner that showed `calls` calls (SPEC SESSION-20): it stays away until the
    session answers on that model again, so its count grows. Closes older than `keep_hours`, the time a banner shows
    for, are dropped."""
    since = (now - timedelta(hours=keep_hours)).isoformat()
    with db.transaction(conn):
        kept = (db.kv_get(conn, WRONG_MODELS_DISMISSED_KEY) or {}).items()
        dismissed = {key: close for key, close in kept if close["at"] >= since}
        dismissed[f"{session_id} {model}"] = {"at": now.isoformat(), "calls": calls}
        db.kv_set(conn, WRONG_MODELS_DISMISSED_KEY, dismissed)


def not_dismissed(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The wrong-model rows without those the user closed with no call on that model since (dismiss_wrong_model)."""
    dismissed = db.kv_get(conn, WRONG_MODELS_DISMISSED_KEY) or {}

    def closed(row: dict[str, Any]) -> bool:
        close = dismissed.get(f"{row['session_id']} {row['model']}")
        return close is not None and row["calls"] <= close["calls"]

    return [row for row in rows if not closed(row)]


WARNINGS_SNOOZED_KEY = "warnings_snoozed"


def snooze_warning(conn: sqlite3.Connection, key: str, until: str | None, now: datetime) -> None:
    """The user's snooze on a warning in Inbox (SPEC UI-72), by the warning's key ("claude:personal", "claude:work"):
    it stays out of Inbox until `until`, on every page; None wakes it. Snoozes already over are dropped."""
    with db.transaction(conn):
        kept = db.kv_get(conn, WARNINGS_SNOOZED_KEY) or {}
        snoozed = {name: end for name, end in kept.items() if datetime.fromisoformat(end) > now and name != key}
        if until is not None:
            snoozed[key] = until
        db.kv_set(conn, WARNINGS_SNOOZED_KEY, snoozed)


def snoozed_warnings(conn: sqlite3.Connection, now: datetime) -> dict[str, str]:
    """The warnings snoozed now (snooze_warning): each key with the time it comes back."""
    kept = db.kv_get(conn, WARNINGS_SNOOZED_KEY) or {}
    return {key: until for key, until in kept.items() if datetime.fromisoformat(until) > now}


def chosen_effort(conn: sqlite3.Connection, session_id: str) -> str | None:
    """The effort the user picked when `session_id` started (SPEC SESSION-22), or None for the default."""
    found = conn.execute(
        "SELECT json_extract(new_options, '$.effort') FROM messages WHERE via = 'new' AND session_id = ?"
        " AND json_extract(new_options, '$.effort') IS NOT NULL ORDER BY id LIMIT 1", (session_id,)
    ).fetchone()
    return found[0] if found else None


def mark_sender(conn: sqlite3.Connection, message_id: int, session_id: str) -> dict[str, Any]:
    """Record the session an agent's message comes from (SPEC MSG-12), so its receiver can reply there."""
    conn.execute("UPDATE messages SET from_session = ? WHERE id = ?", (session_id, message_id))
    return get_message(conn, message_id)


def _revive_archives(conn: sqlite3.Connection, session_id: str) -> None:
    """A message to an archived session brings it back (SPEC SESSION-4): Cockpit leaves it running and shows
    it in Sessions again. Cockpit's archive message, when it still waits, is set aside unsent; the user's own
    closing words that the archive waited on still go."""
    now = db.now_iso()
    for archive in db.rows(
        conn.execute(
            "SELECT a.* FROM session_archives a WHERE a.session_id = ? AND a.revived_at IS NULL"
            " AND a.cancelled_at IS NULL AND a.failure = ''",
            (session_id,),
        ).fetchall()
    ):
        conn.execute("UPDATE session_archives SET revived_at = ? WHERE id = ?", (now, archive["id"]))
        if archive["message_id"] is not None:
            conn.execute(
                "UPDATE messages SET state = 'failed', detail = ?, dismissed_at = ?, dismissed_by = 'cockpit',"
                " dismiss_reason = ? WHERE id = ? AND state = 'queued' AND author = 'cockpit'",
                (
                    "Not sent: a later message to the session brought it back first.", now,
                    "a later message to the session brought it back before this one went out", archive["message_id"],
                ),
            )


def get_message(conn: sqlite3.Connection, message_id: int) -> dict[str, Any]:
    message = db.row(conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone())
    if message is None:
        raise NotFound(f"message {message_id} does not exist")
    return message


def retry_message(conn: sqlite3.Connection, message_id: int, actor: str) -> dict[str, Any]:
    """Queue a failed message again. The user may retry any message; an agent only its own. A dismissed one is
    refused until the user restores it, so a retry never undoes a dismissal on its own."""
    message = get_message(conn, message_id)
    if actor != "user" and message["author"] != actor:
        raise Forbidden(f"message {message_id} is {message['author']}'s; an agent can retry only its own messages")
    with db.transaction(conn):
        updated = conn.execute(
            "UPDATE messages SET state = 'queued', detail = '', waits_since = NULL"
            " WHERE id = ? AND state = 'failed' AND dismissed_at IS NULL",
            (message_id,),
        )
        if updated.rowcount == 0:
            message = get_message(conn, message_id)
            if message["dismissed_at"] is not None:
                raise Conflict(
                    f"message {message_id} was dismissed by {message['dismissed_by']}; {user_name()} restores it before"
                    " a retry"
                )
            raise Conflict(f"message {message_id} is {message['state']}; only failed messages can be retried")
        reopened = _retry_reopens(conn, message, actor) and reopen_for_message(conn, message["item_id"])
    return get_message(conn, message_id) | {"reopened": reopened}


def from_ask(conn: sqlite3.Connection, message_id: int) -> bool:
    """The message takes an Ask's question and answer to the session (SPEC MSG-11)."""
    return conn.execute("SELECT 1 FROM side_chats WHERE sent_message_id = ?", (message_id,)).fetchone() is not None


def _retry_reopens(conn: sqlite3.Connection, message: dict[str, Any], actor: str) -> bool:
    """The user sends their own words again, which take up a finished task as they would the first time (SPEC
    CONTROL-28). The closing answer or last message of the finish that holds is part of that finish, and an Ask's
    answer handed on is no new words."""
    if actor != "user" or message["author"] != "user" or message["item_id"] is None or from_ask(conn, message["id"]):
        return False
    finish = get_finish(conn, message["item_id"])
    return finish is None or finish["message_id"] != message["id"]


def send_now_message(conn: sqlite3.Connection, message_id: int) -> dict[str, Any]:
    """The user's Send now on a message that waits for its session's turn to end (SPEC MSG-3), or for Take back
    (SPEC MSG-8): the caller stops the session at once and resumes it with the message."""
    message = get_message(conn, message_id)
    if message["state"] != "queued" or (message["waits_since"] is None and message["take_back_until"] is None):
        raise Conflict(f"message {message_id} is {message['state']}; only a message that waits can be sent now")
    release_message(conn, message_id)
    return get_message(conn, message_id)


def dismiss_message(conn: sqlite3.Connection, message_id: int, reason: str | None, actor: str) -> dict[str, Any]:
    """Set a message aside: a failed one whose work was done another way or is no longer needed, or one that
    waits for its session's turn (SPEC MSG-3) whose text reached the session another way (SPEC MSG-5).

    The user may leave out the reason; an agent gives one line. A waiting message
    becomes `failed` with "Not sent", so the user can restore and retry it. Either
    records who dismissed it, when and why; the failure counts and lists leave
    it out from then on.
    """
    reason = reason.strip() if reason is not None else None
    if actor != "user" and not reason:
        raise Invalid("an agent says why it dismisses a message, e.g. 'the item has a working session since 10:00'")
    if reason is not None and "\n" in reason:
        raise Invalid("the reason is one line")
    with db.transaction(conn):
        updated = conn.execute(
            "UPDATE messages SET dismissed_at = ?, dismissed_by = ?, dismiss_reason = ?,"
            " state = 'failed', detail = CASE WHEN state = 'queued' THEN ? ELSE detail END"
            " WHERE id = ? AND dismissed_at IS NULL"
            " AND (state = 'failed' OR (state = 'queued' AND waits_since IS NOT NULL))",
            (db.now_iso(), actor, reason or None, "Not sent: set aside while it waited for the session's turn.",
             message_id),
        )
        message = get_message(conn, message_id)
        if updated.rowcount == 0:
            if message["dismissed_at"] is not None:
                raise Conflict(f"message {message_id} was already dismissed by {message['dismissed_by']}")
            raise Conflict(
                f"message {message_id} is {message['state']}. Only a failed message, or one that waits for its"
                " session's turn, can be dismissed."
            )
        patterns.record(
            conn, "message.dismissed", actor, message["item_id"], message["step_id"], None,
            {"message_id": message_id, "reason": message["dismiss_reason"]},
        )
    return message


def restore_message(conn: sqlite3.Connection, message_id: int) -> dict[str, Any]:
    """The user undoes a dismissal: the failed message counts and shows again."""
    with db.transaction(conn):
        updated = conn.execute(
            "UPDATE messages SET dismissed_at = NULL, dismissed_by = NULL, dismiss_reason = NULL"
            " WHERE id = ? AND dismissed_at IS NOT NULL",
            (message_id,),
        )
        message = get_message(conn, message_id)
        if updated.rowcount == 0:
            raise Conflict(f"message {message_id} is not dismissed")
        patterns.record(
            conn, "message.restored", "user", message["item_id"], message["step_id"], None, {"message_id": message_id}
        )
    return message


def failure_sentence(message: dict[str, Any], session_names: dict[str, str]) -> str:
    """One plain sentence for the user on what failed: the message's target and the reason delivery recorded."""
    if message["new_cwd"] is not None:
        subject, failed, happened = f"The new session in {Path(message['new_cwd']).name}", "could not start", "started"
    else:
        session_id = message["session_id"] or ""
        name = session_names.get(session_id) or f"session {session_id[:8]}"
        subject, failed, happened = f"The message to {name}", "was not delivered", "arrived"
    detail = message["detail"].strip()
    if detail.startswith("Delivery uncertain"):
        return f"{subject}: Cockpit could not tell whether it {happened}; check the session before retrying"
    return f"{subject} {failed}: {failure_reason(detail)}"


def behind_dialog(message: dict[str, Any], dialogs: dict[str, datetime]) -> bool:
    """The message reached a live session's queue after a dialog opened in its terminal, and the dialog is
    still open: the session reads it once the dialog is answered. `dialogs`: session UUID to when its
    open dialog came."""
    opened = dialogs.get(message["session_id"] or "")
    sent = rank.parse_time(message["sent_at"])
    return opened is not None and message["state"] == "sent" and sent is not None and sent >= opened


def open_dialogs(sessions: list[Session]) -> dict[str, datetime]:
    """Session UUID to when the dialog it waits at in its terminal came. A session's job file changes state
    when its dialog opens, so its last change is when the dialog came."""
    return {
        session.session_id: opened
        for session in sessions
        if session.waits_on_dialog and (opened := rank.parse_time(session.updated_at)) is not None
    }


def restart_text(session: Session) -> str:
    """What the user's Restart on a session at a dialog Cockpit cannot answer tells it (SPEC PERM-8): to make the
    call again, which brings the request to the user."""
    opened = open_dialogs([session]).get(session.session_id)
    if opened is None:
        raise Conflict("the session no longer waits at a dialog in its terminal")
    asked = f' ("{session.needs}")' if session.needs else ""
    return (
        f"{user_name()} restarted this session from Cockpit. A dialog in your terminal{asked} had waited since"
        f" {opened:%H:%M} UTC, and Cockpit cannot answer a dialog there. If you still need that call, make it"
        f" again. Its permission request now comes to {user_name()} on Cockpit."
    )


def restart_message(conn: sqlite3.Connection, session: Session, checkouts_dir: Path, text: str) -> dict[str, Any]:
    """Queue the message that resumes a session Cockpit restarts from a dialog in its terminal (SPEC PERM-8),
    `text` first. It repeats the messages that reached its queue behind the dialog: the stopped process may
    never have read them."""
    opened = open_dialogs([session]).get(session.session_id)
    if opened is None:
        raise Conflict("the session no longer waits at a dialog in its terminal")
    held = [
        message
        for message in db.rows(
            conn.execute(
                "SELECT * FROM messages WHERE session_id = ? AND state = 'sent' ORDER BY id", (session.session_id,)
            )  # ty: ignore[invalid-argument-type]
        )
        if behind_dialog(message, {session.session_id: opened})
    ]
    if held:
        text += "\n\nThese messages reached your queue while the dialog was open, so you may not have read them:"
        for message in held:
            sender = {"user": user_name(), "cockpit": "Cockpit"}.get(message["author"], message["author"].replace(":", " "))
            text += f"\n\n{rank.parse_time(message['sent_at']):%H:%M} UTC, from {sender}:\n{message['text']}"
    return create_message(conn, "cockpit", text, {"session_id": session.session_id}, checkouts_dir)


def target_session(conn: sqlite3.Connection, item_id: int, step_id: int | None) -> str | None:
    """The step's own target if it names one, else the item's first (primary) session."""
    if step_id is not None:
        step = get_step(conn, step_id)
        if step["item_id"] != item_id:
            raise Invalid(f"step {step_id} belongs to item {step['item_id']}, not item {item_id}")
        if step["session_id"]:
            return step["session_id"]
    sessions = get_item(conn, item_id)["sessions"]
    return sessions[0] if sessions else None


def check_seen_target(step_id: int, session_id: str | None, seen: str | None) -> None:
    """Refuse a press on Run, Allow or a finish key when the step's session (`target_session`) is no longer the
    one the user's page showed (`seen`, None for no session), so the press reaches the session shown."""
    if session_id != seen:
        raise Conflict(f"step {step_id} goes to another session since your page showed it; read it again")


def add_feedback(
    conn: sqlite3.Connection, text: str, step_id: int | None, item_id: int | None, target: str, quote: str,
    seconds: int,
) -> dict[str, Any]:
    if not text.strip():
        raise Invalid("the feedback is empty")
    if target and not target.startswith(FEEDBACK_TARGET_PREFIXES):
        raise Invalid(f"a feedback target starts with {' or '.join(FEEDBACK_TARGET_PREFIXES)}")
    need = None
    if step_id is not None:
        step = get_step(conn, step_id)
        item_id, need = step["item_id"], step["need"]
    elif item_id is not None:
        get_item(conn, item_id)
    with db.transaction(conn):
        cursor = conn.execute(
            "INSERT INTO feedback (step_id, item_id, text, target, quote, seconds, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (step_id, item_id, text, target, quote, seconds, db.now_iso()),
        )
        patterns.record(
            conn, "feedback.added", "user", item_id, step_id, need, {"feedback_id": cursor.lastrowid, "target": target}
        )
    return db.row(conn.execute("SELECT * FROM feedback WHERE id = ?", (cursor.lastrowid,)).fetchone())  # ty: ignore[invalid-return-type]


def doc_comments(conn: sqlite3.Connection, prefix: str, since: str) -> list[dict[str, Any]]:
    """The user's comments on targets starting with `prefix`: all unread ones, and read ones from `since` on."""
    return db.rows(
        conn.execute(
            "SELECT id, target, quote, text, created_at, read_at FROM feedback"
            " WHERE substr(target, 1, length(?)) = ? AND (read_at IS NULL OR created_at >= ?) ORDER BY created_at",
            (prefix, prefix, since),
        ).fetchall()
    )


def feedback_list(conn: sqlite3.Connection, unread_only: bool) -> list[dict[str, Any]]:
    where = "WHERE f.read_at IS NULL" if unread_only else ""
    return db.rows(
        conn.execute(
            "SELECT f.*, s.ask AS step_ask, i.title AS item_title FROM feedback f"
            " LEFT JOIN steps s ON s.id = f.step_id LEFT JOIN items i ON i.id = f.item_id"
            f" {where} ORDER BY f.created_at"
        ).fetchall()
    )


def mark_feedback_read(conn: sqlite3.Connection, ids: list[int]) -> None:
    conn.executemany("UPDATE feedback SET read_at = ? WHERE id = ? AND read_at IS NULL", [(db.now_iso(), i) for i in ids])


# Good writing examples (SPEC FEEDBACK-8): what the user marked as well written on Cockpit. A step keeps its
# Markdown and a shared page its HTML, as they were when marked; a part of a tab keeps the words the user saw.
WRITING_KINDS = ("step", "page", "part")
# good: writing to reproduce; bad: writing the user found hard to understand, to avoid (SPEC FEEDBACK-8).
WRITING_VERDICTS = ("good", "bad")


def _step_markdown(step: dict[str, Any]) -> str:
    parts = [f"# {step['ask']}", step["why"], step["body"]]
    parts += [f"## {detail['title']}\n\n{detail['body']}" for detail in step["details"]]
    if step["draft"]:
        parts.append(f"## Draft\n\n{step['draft']}")
    if step["closes"]:
        parts.append("## Close these tasks\n\n" + "\n".join(f"- Item {entry['item_id']}: {entry['why']}" for entry in step["closes"]))
    if step["reply_hint"]:
        parts.append(f"## Reply hint\n\n{step['reply_hint']}")
    return "\n\n".join(part for part in parts if part)


def _tab_title(target: str) -> str:
    """The tab a part was on, from its target: `page:/usage/spend/td` is on the Usage page."""
    tab = re.match(r"page:/([a-z]+)", target)
    return f"{tab[1].capitalize()} page" if tab else "Cockpit page"


def add_writing_example(
    conn: sqlite3.Connection, verdict: str, kind: str, target: str, quote: str, note: str, step_id: int | None,
    item_id: int | None, page_id: int | None, session_id: str | None, sessions: list[Session],
) -> dict[str, Any]:
    """Keep a copy of what the user marked as good or as hard to understand (`verdict`), with who wrote it and the
    skills that session had loaded when it wrote it (`sessions.skills_loaded`). `session_id`: the session the picked
    part names (a message in a step's history), else null and the step's or item's session counts as the writer.
    A whole step marked again on the same text replaces the earlier mark: one verdict per step text."""
    if verdict not in WRITING_VERDICTS:
        raise Invalid(f"a writing example's verdict is one of {', '.join(WRITING_VERDICTS)}")
    if kind not in WRITING_KINDS:
        raise Invalid(f"a writing example's kind is one of {', '.join(WRITING_KINDS)}")
    written_by = ""
    # When it was written, for the skills loaded by then: a page's last change or a step's last text, else (a message
    # the part names, or words on a tab) now, which counts any skill loaded since too.
    written_at = db.now_iso()
    step_version_id = None
    if kind == "page":
        if page_id is None:
            raise Invalid("a page example names its page")
        page = item_page(conn, page_id)
        title, text, fmt, item_id, written_by = page["title"], page["html"], "html", page["item_id"], page["added_by"]
        target = target or f"page:/pages/{page_id}"
        written_at = page["updated_at"]
    elif step_id is not None:
        item_id = get_step(conn, step_id)["item_id"]
        # The step's last text is its newest `step_versions` row (a put of only the score, size or progress adds
        # none): its words, writer and time come from that one row, so a put meanwhile cannot pair one text with
        # another's writer.
        text_version = db.row(conn.execute(
            "SELECT * FROM step_versions WHERE step_id = ? ORDER BY id DESC LIMIT 1", (step_id,)
        ).fetchone())
        assert text_version is not None, f"step {step_id} has no text version"
        if kind == "step" or session_id is None:
            written_by, written_at = text_version["actor"], text_version["at"]
        session_id = session_id or target_session(conn, item_id, step_id)
        if kind == "step":
            title, text, fmt, target = text_version["ask"], _step_markdown(text_version), "markdown", f"step:{step_id}"
            step_version_id = text_version["id"]
        else:
            title, text, fmt = text_version["ask"], quote, "text"
    elif kind == "step":
        raise Invalid("a step example names its step")
    else:
        title, text, fmt = (get_item(conn, item_id)["title"] if item_id is not None else _tab_title(target)), quote, "text"
    if item_id is not None and session_id is None:
        sessions_of_item = get_item(conn, item_id)["sessions"]
        session_id = sessions_of_item[0] if sessions_of_item else None
    if not text.strip():
        raise Invalid("the marked part has no text")
    session_name = shown_session_names(conn, sessions).get(session_id, "") if session_id else ""
    # Read before the transaction: a long transcript takes seconds.
    skills = skills_loaded(session_id, written_at) if session_id else []
    with db.transaction(conn):
        if step_version_id is not None:
            conn.execute(
                "DELETE FROM writing_examples WHERE kind = 'step' AND step_id = ? AND step_version_id = ?",
                (step_id, step_version_id),
            )
        cursor = conn.execute(
            "INSERT INTO writing_examples (verdict, kind, title, target, text, format, note, step_id, item_id, page_id,"
            " session_id, session_name, written_by, skills, step_version_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (verdict, kind, title, target, text, fmt, note.strip(), step_id, item_id, page_id, session_id, session_name,
             written_by, json.dumps(skills), step_version_id, db.now_iso()),
        )
        patterns.record(
            conn, "writing.marked", "user", item_id, step_id, None,
            {"example_id": cursor.lastrowid, "kind": kind, "verdict": verdict},
        )
    return writing_example(conn, cursor.lastrowid)  # ty: ignore[invalid-argument-type]


def _writing_out(example: dict[str, Any]) -> dict[str, Any]:
    """A step's Markdown also rendered, for the page; a page's HTML and a part's words show as they are."""
    return example | {"text_html": markdown_html(example["text"], NO_REFS) if example["format"] == "markdown" else ""}


def writing_example(conn: sqlite3.Connection, example_id: int) -> dict[str, Any]:
    example = db.row(
        conn.execute(
            "SELECT w.*, i.title AS item_title FROM writing_examples w LEFT JOIN items i ON i.id = w.item_id"
            " WHERE w.id = ?",
            (example_id,),
        ).fetchone()
    )
    if example is None:
        raise NotFound(f"writing example {example_id} does not exist")
    return _writing_out(example)


def writing_examples(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every example, newest first."""
    rows = db.rows(
        conn.execute(
            "SELECT w.*, i.title AS item_title FROM writing_examples w LEFT JOIN items i ON i.id = w.item_id"
            " ORDER BY w.created_at DESC, w.id DESC"
        ).fetchall()
    )
    return [_writing_out(row) for row in rows]


def remove_writing_example(conn: sqlite3.Connection, example_id: int) -> dict[str, Any]:
    example = writing_example(conn, example_id)
    conn.execute("DELETE FROM writing_examples WHERE id = ?", (example_id,))
    return example


def clear_step_writing(conn: sqlite3.Connection, step_id: int) -> dict[str, Any]:
    """Clear the verdict the step's current text holds: the user pressed its pressed key on the card again . A mark on an earlier text of the step stays; with none on this text, nothing changes."""
    step = get_step(conn, step_id)
    with db.transaction(conn):
        cursor = conn.execute(
            "DELETE FROM writing_examples WHERE kind = 'step' AND step_id = ?"
            " AND step_version_id = (SELECT MAX(v.id) FROM step_versions v WHERE v.step_id = ?)",
            (step_id, step_id),
        )
        patterns.record(conn, "writing.cleared", "user", step["item_id"], step_id, None, {"removed": cursor.rowcount})
    return {"removed": cursor.rowcount}


def sweep_last_active(conn: sqlite3.Connection, sweep: dict[str, Any]) -> datetime:
    """The latest write by the sweep's agent since it started: its heartbeat. Its events count, which a later
    edit by someone else does not overwrite and which a step check (no `updated_at`) also writes; the delivery
    worker's records of its messages do not, as a restart marks a dead sweep's messages failed."""
    by = sweep["by"]
    latest = conn.execute(
        "SELECT MAX(at) FROM ("
        " SELECT MAX(at) AS at FROM events WHERE at >= ? AND actor = ? AND kind NOT IN ('message.sent', 'message.failed')"
        " UNION ALL SELECT MAX(updated_at) FROM items WHERE updated_by = ?"
        " UNION ALL SELECT MAX(updated_at) FROM steps WHERE updated_by = ?"
        " UNION ALL SELECT MAX(created_at) FROM messages WHERE author = ?)",
        (sweep["started_at"], by, by, by, by),
    ).fetchone()[0]
    started = rank.parse_time(sweep["started_at"])
    written = rank.parse_time(latest)
    return max(started, written) if written else started  # ty: ignore[invalid-argument-type, invalid-return-type]


def sweep_running(conn: sqlite3.Connection, now: datetime) -> dict[str, Any] | None:
    sweep = db.kv_get(conn, "last_sweep")
    if sweep is None or sweep.get("finished_at"):
        return None
    if now - sweep_last_active(conn, sweep) > timedelta(minutes=SWEEP_SILENT_MINUTES):
        return None
    return sweep


def start_sweep(conn: sqlite3.Connection, actor: str, reorder: bool) -> dict[str, Any]:
    with db.transaction(conn):
        running = sweep_running(conn, datetime.now(UTC))
        if running is not None:
            raise Conflict(f"a sweep started at {running['started_at']} by {running['by']} is still running")
        sweep = {
            "id": uuid.uuid4().hex[:12],
            "by": actor,
            "started_at": db.now_iso(),
            "finished_at": None,
            "summary": "",
            "error": "",
            "reorder": reorder,
        }
        db.kv_set(conn, "last_sweep", sweep)
        # The sweep reads the whole state now, so earlier changes no longer call for a wake-up. A reorder (SPEC SWEEP-25)
        # reads only what it scores, so they still do.
        if not reorder:
            db.kv_set(conn, "sweep_seen_event", conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0])
        patterns.record(conn, "sweep.started", actor, None, None, None, {"sweep_id": sweep["id"]})
    return sweep


def finish_sweep(conn: sqlite3.Connection, sweep_id: str, summary: str, error: str, actor: str) -> dict[str, Any]:
    """Record the sweep's end. `warning` is set when a light sweep or fill, not a reorder, finished while a full one was due (SPEC
    SCOPE-3)."""
    warning = ""
    if not error and patterns.sweep_kind(summary) not in ("full", "reorder") and patterns.full_sweep_due(conn, datetime.now(UTC)):
        warning = (
            f"no full sweep finished in the last {patterns.FULL_SWEEP_HOURS} hours. Run one next, and read every"
            " configured source for new work, and report any source you could not read."
        )
    with db.transaction(conn):
        sweep = db.kv_get(conn, "last_sweep")
        if sweep is None or sweep["id"] != sweep_id:
            raise Conflict(f"sweep {sweep_id} is not the latest sweep; a newer one replaced it")
        if sweep["finished_at"]:
            raise Conflict(f"sweep {sweep_id} already finished")
        sweep |= {"finished_at": db.now_iso(), "summary": summary, "error": error}
        db.kv_set(conn, "last_sweep", sweep)
        _stronger_first(conn, datetime.now(UTC), PLACE_MARGIN)  # the sweep's scores, all in (`_swap_places`)
        patterns.record(
            conn, "sweep.finished", actor, None, None, None, {"sweep_id": sweep_id, "summary": summary, "error": error}
        )
    return sweep | {"warning": warning}


# The work in progress the sweeper picks for the sidebar (SPEC VIEW-17): the few pieces of work that matter most
# among those waiting on someone other than the user, each with one short line on what it waits for.
IN_PROGRESS_MAX = 5
IN_PROGRESS_LINE_MAX = 90


def put_in_progress(conn: sqlite3.Connection, actor: str, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace the list; each entry is {"item_id", "line"}, on an open item, at most once."""
    if len(entries) > IN_PROGRESS_MAX:
        raise Invalid(f"{len(entries)} entries; at most {IN_PROGRESS_MAX}")
    kept: list[dict[str, Any]] = []
    for entry in entries:
        line = entry["line"].strip()
        if line.splitlines() != [line] or len(line) > IN_PROGRESS_LINE_MAX:
            raise Invalid(f"item {entry['item_id']}'s line must be one line of 1 to {IN_PROGRESS_LINE_MAX} characters")
        item = get_item(conn, entry["item_id"])
        if item["archived"] or item["stage"] == "done":
            raise Invalid(f"item {item['id']} is {'archived' if item['archived'] else 'done'}; the list holds work in progress")
        if any(other["item_id"] == item["id"] for other in kept):
            raise Invalid(f"item {item['id']} is listed twice")
        kept.append({"item_id": item["id"], "line": line})
    with db.transaction(conn):
        db.kv_set(conn, "in_progress", {"entries": kept, "by": actor, "at": db.now_iso()})
    return kept


# Who holds the ball on an item, first match wins: who the work waits for (SPEC VIEW-2). "you" is derived from
# the user's open, unsnoozed steps, whether in the user's queue or ready next for a place; a step the user handed
# to an agent gives the ball to agents. "merge" is work waiting for a release or a merge to its next stage (dev to
# staging, staging to prod). Work before dev with nothing to wait for is agent work, planning among it: the agents' while
# the item has a session. "nobody" is work no one is moving, such as work in prod with nothing waiting: the sweep's
# to fix.
BALLS = ("you", "people", "bots", "merge", "agents", "nobody", "done")
_WAITING_BALL = {"agent": "agents", "bot": "bots", "person": "people"}


# SQL for "the step `s` asks the user to act", the opposite of `is_read`.
ASKS = "s.need NOT LIKE 'read:%'"


def is_read(step: dict[str, Any]) -> bool:
    """A step that brings the user a result to read (need `read:…`, VIEW-16): it tells, and asks nothing the work
    waits for."""
    return step["need"].startswith("read:")


def ball_holder(
    item: dict[str, Any], asking_items: set[int], reading_items: set[int], working_items: set[int], handed_items: set[int],
    messaged_items: set[int],
) -> str:
    """Who the item's work waits for (SPEC VIEW-2). An open, unsnoozed step that asks the user to act puts it with the
    user. A read step leaves the work with whoever does it, and with the user, who has the result left to read, once
    nothing else holds it: no session working on it, nothing in `waiting_on`. An item in `messaged_items`, with the
    user's message on its way to a session that may not exist yet, is the agents' as well."""
    if item["id"] in asking_items:
        return "you"
    if item["id"] in handed_items:
        return "agents"
    if item["stage"] == "done":
        return "done"
    kinds = {entry["kind"] for entry in item["waiting_on"]}
    for kind in ("agent", "bot", "person"):
        if kind in kinds:
            return _WAITING_BALL[kind]
    if "release" in kinds or item["stage"] in ("dev", "staging"):
        return "merge"
    reading = item["id"] in reading_items
    if reading:
        return "agents" if item["id"] in working_items else "you"
    if item["stage"] in (None, "planning", "building", "review") and (item["sessions"] or item["id"] in messaged_items):
        return "agents"
    return "nobody"


# A session sent the user's message shows as working a moment later; until then its last change can be days old. A
# message still on its way after MESSAGE_GOES_WITHIN is stuck, and its session's stall shows again.
MESSAGE_STARTS_WITHIN = timedelta(minutes=1)
MESSAGE_GOES_WITHIN = timedelta(minutes=5)


def messaged_item_ids(conn: sqlite3.Connection, now: datetime) -> set[int]:
    """The items with a message from the user on its way to a session, written or with its item changed just now (a
    retried message keeps its old time, and its reopening changes the item), or sent to one in the last moment. The
    agents have that work from Send on, so the item shows in Busy (SPEC CONTROL-28), not as idle until the session
    starts."""
    goes_since = _iso(now - MESSAGE_GOES_WITHIN)
    return {row[0] for row in conn.execute(
        "SELECT DISTINCT m.item_id FROM messages m JOIN items i ON i.id = m.item_id WHERE m.author = 'user'"
        " AND ((m.state IN ('queued', 'sending') AND (m.created_at >= ? OR i.updated_at >= ?))"
        " OR (m.state = 'sent' AND m.sent_at >= ?))",
        (goes_since, goes_since, _iso(now - MESSAGE_STARTS_WITHIN)),
    )}


# Agent work on an item has stalled once every session on it has sat this long with nothing under way (SPEC SWEEP-20).
STALL_AFTER = timedelta(minutes=30)


def working(session: Session | None) -> bool:
    """The session works on its item: it is alive and its turn is not over (a turn, a background command, a
    scheduled prompt, a dialog). The sweeper works for every item, so it counts for none."""
    return session is not None and session.alive and not session.turn_over and session.name not in SWEEP_SESSION_NAMES


def stalled_since(
    item: dict[str, Any], step_sessions: list[str], snoozed: bool, messaged: bool, by_id: dict[str, Session], now: datetime
) -> str | None:
    """When the agents' work on `item` stopped: the last change of its sessions (the item's own, the agents it waits
    on and those its open steps name), once none of them has had a turn under way, a background command, subagent
    or scheduled prompt, or a dialog waiting in its terminal for STALL_AFTER. An agent it waits on `until` a time
    rests until then, also with its session stopped (a handover days away needs no running session), and the work
    stalls STALL_AFTER after it, which wakes the sweeper for urgent and high work (sweeper.note_stalls). None for an item whose next move is not the agents', one with a step the user snoozed (theirs to come
    back to), while the user's message is on its way to a session (`messaged`, `messaged_items`), while one of its
    sessions works, and while Cockpit does not list one of the item's or its steps'
    sessions (an interactive session, which has no job file, may be working, as `page_state`'s working_items
    counts it)."""
    if item["archived"] or item["ball"] != "agents" or snoozed or messaged:
        return None
    if any(session_id not in by_id for session_id in item["sessions"] + step_sessions):
        return None
    agents = [entry for entry in item["waiting_on"] if entry["kind"] == "agent"]
    ids = item["sessions"] + [entry["who"] for entry in agents] + step_sessions
    known = [
        by_id[session_id] for session_id in dict.fromkeys(ids)
        if session_id in by_id and by_id[session_id].name not in SWEEP_SESSION_NAMES
    ]
    if not known or any(working(session) for session in known):
        return None
    changes = [rank.parse_time(session.updated_at) for session in known] + [
        rank.parse_time(entry["until"]) for entry in agents
        if entry.get("until")
    ]
    last = max((change for change in changes if change is not None), default=None)
    return _iso(last) if last is not None and now - last >= STALL_AFTER else None


# The two groups below the user's queue (SPEC VIEW-27). Busy is the agents' work while it moves: a session works on it,
# its step was handed to one, or its agent picks it up within BUSY_WITHIN. Waiting is every other open item, agent work
# that stopped longer ago or picks up later among it. "you" and "done" are as `ball_holder` has them.
GROUPS = ("you", "busy", "waiting", "done")
BUSY_WITHIN = timedelta(hours=1)
# Each row's hover line says in a few words what the agent works on or what the item waits for. `tip_at`, when set, is
# the time the page adds to it in the user's own time zone.
TIP_CHARS = 48
# The parts of Waiting, by what the work waits for, in the order the page shows them: work that needs a fresh look first (no next move, an agent that
# stopped), then work on others' move (a person, a bot), then releases nearest prod first, then agents that resume at
# a set time. The page adds the user's own snoozed steps last, with the rests the user chose (`by_user`, wait
# "snoozed").
WAIT_KINDS = ("nobody", "stopped", "person", "bot", "staging", "dev", "release", "resumes")
# An agent's `doing`: what it works on now, which Busy's hover line shows (SPEC VIEW-27).
DOING_CHARS = 60
AGENT_NAME_CHARS = 32
# Why an agent's work rests until its `until`, which Waiting's hover line says.
WHY_CHARS = 60


def _short(text: str, chars: int) -> str:
    """`text` on one line, its Markdown links and marks as plain words, cut to `chars` with an ellipsis."""
    text = " ".join(re.sub(r"[*`]", "", re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)).split())
    return text if len(text) <= chars else text[: chars - 1].rstrip() + "…"


def work_group(
    item: dict[str, Any], handed_asks: dict[int, str], names: dict[str, str], active: bool, now: datetime
) -> dict[str, Any]:
    """The item's group on the page and its hover line (SPEC VIEW-27): {group, wait, tip, tip_at, why}. Busy's line is the
    agent's goal: the ask of the step the user handed it, else the `doing` the agent put, else where the item stands.
    Waiting's names what the item waits for: "review: Alex", "review-bot", "on staging", the agent and when it picks up,
    or the agent that stopped and when. `wait` sorts a Waiting item into its part of Waiting by what it waits for
    (WAIT_KINDS, or "snoozed" for a rest the user chose; None outside Waiting), and `why` says why a resting agent's
    work picks up only then. `names` holds the listed sessions' names by id; `active` says one of the
    item's sessions works now, which keeps it Busy whatever another agent's `until`. `item["stalled_since"]` and
    `item["ball"]` are already set."""
    ball = item["ball"]
    if ball in ("you", "done"):
        return {"group": ball, "wait": None, "tip": "", "tip_at": None, "why": ""}

    def agent(who: str) -> str:
        # A name too long to show whole is most often the opening words of a prompt, which read as a task, not a name.
        name = " ".join(names.get(who, "" if _looks_like_id(who) else who).split())
        if not name or len(name) > AGENT_NAME_CHARS or name.casefold() == item["title"].casefold():
            return "its agent"
        return f"agent “{name}”"

    if ball == "agents":
        stopped = item["stalled_since"] and now - rank.parse_time(item["stalled_since"]) >= BUSY_WITHIN  # ty: ignore[unsupported-operator]
        later = sorted(
            ((rank.parse_time(entry["until"]), entry) for entry in item["waiting_on"]
             if entry["kind"] == "agent" and entry.get("until")),
            key=lambda pair: pair[0],
        )
        agents = [entry["who"] for entry in item["waiting_on"] if entry["kind"] == "agent"] + item["sessions"]
        # A rest the user chose, such as a reminder they asked for, is their snooze: it stays snoozed until
        # its time, as a snoozed step does, rather than turning Busy in its last hour.
        if not active and later and all(at - now > (timedelta(0) if entry.get("by_user") else BUSY_WITHIN) for at, entry in later):  # ty: ignore[unsupported-operator]
            resumes, entry = later[0]
            return {"group": "waiting", "wait": "snoozed" if entry.get("by_user") else "resumes",
                    "tip": f"{agent(entry['who'])} resumes", "tip_at": _iso(resumes), "why": entry.get("why") or ""}  # ty: ignore[invalid-argument-type]  # Entries with no until were filtered out above.
        if stopped and not active:
            return {"group": "waiting", "wait": "stopped", "tip": f"{agent(agents[0]) if agents else 'its agent'} stopped",
                    "tip_at": item["stalled_since"], "why": ""}
        goal = _short(handed_asks.get(item["id"]) or item["doing"] or item["agent_summary"] or item["summary"], TIP_CHARS)
        # A to-do's summary is often its title's own words, which the hover already shows above the line.
        same = goal.rstrip("…").casefold().startswith(_short(item["title"], 24).rstrip("…").casefold())
        return {"group": "busy", "wait": None, "tip": "" if same else goal, "tip_at": None, "why": ""}
    if ball in ("people", "bots"):
        entry = next(entry for entry in item["waiting_on"] if entry["kind"] == ("person" if ball == "people" else "bot"))
        who = _short(entry["who"], 32)
        return {"group": "waiting", "wait": "person" if ball == "people" else "bot",
                "tip": f"{entry['what']}: {who}" if entry.get("what") else who, "tip_at": None, "why": ""}
    if ball == "merge":
        release = next((entry["who"] for entry in item["waiting_on"] if entry["kind"] == "release"), "release")
        tip = f"on {item['stage']}" if item["stage"] in ("dev", "staging") else _short(release, TIP_CHARS)
        wait = {"dev": "dev", "staging": "staging"}.get(item["stage"], "release")
        return {"group": "waiting", "wait": wait, "tip": tip, "tip_at": None, "why": ""}
    return {"group": "waiting", "wait": "nobody", "tip": "no next move", "tip_at": None, "why": ""}


def _looks_like_id(who: str) -> bool:
    """A session uuid in `waiting_on`, whose session Cockpit no longer lists, rather than a name."""
    return re.fullmatch(r"[0-9a-f]{8}-[0-9a-f-]{27}", who) is not None


def sessions_of_steps(steps: list[dict[str, Any]]) -> dict[int, list[str]]:
    """Each item's sessions by its open `steps`: the session a step asks for, and for a step handed to an agent
    (its `handoff` attached) the session the user's message went to. The overview and move recommendations
    count an item's sessions as its `sessions` and these."""
    by_item: dict[int, list[str]] = {}
    for step in steps:
        for session_id in (step["session_id"], (step.get("handoff") or {}).get("session_id")):
            if session_id:
                by_item.setdefault(step["item_id"], []).append(session_id)
    return by_item


def open_steps_with_handoff(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every open step, a handed one with its `handoff` message, for `sessions_of_steps`."""
    steps = db.rows(conn.execute(f"SELECT s.* FROM steps s WHERE {OPEN}").fetchall())
    for step in steps:
        if step["handoff_message_id"]:
            step["handoff"] = get_message(conn, step["handoff_message_id"])
    return steps


def _on_board(
    item: dict[str, Any],
    queued_by_item: dict[int, int],
    asking_items: set[int],
    reading_items: set[int],
    working_items: set[int],
    handed_items: set[int],
    messaged_items: set[int],
    last_send_by_item: dict[int, str],
    untriaged: set[int],
    item_time: dict[int, dict[str, Any]],
    pulls: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """The item as `state.items` shows it, before its images are added."""
    ref = github.pull_ref(item)
    return item | {
        "open_steps": queued_by_item.get(item["id"], 0),
        "ball": ball_holder(item, asking_items, reading_items, working_items, handed_items, messaged_items),
        # An open, unsnoozed step asks the user to act: a held ball with only results to read does not.
        "asks_user": item["id"] in asking_items,
        "last_agent_message_at": last_send_by_item.get(item["id"]),
        "untriaged": item["id"] in untriaged,
        "time_spent": item_time.get(item["id"]),
        # The item's pull request and whether the user opened it, once GitHub has said (github.py).
        "pull": pulls.get(ref) if ref else None,
    }


def _writing_verdicts(conn: sqlite3.Connection) -> dict[int, str]:
    """The verdict the user gave each step's current text with Good writing or Hard to understand (FEEDBACK-8), by
    step id: the step card shows it pressed. Older duplicates on one text  yield to the newest."""
    return dict(conn.execute(
        "SELECT w.step_id, w.verdict FROM writing_examples w WHERE w.kind = 'step'"
        " AND w.step_version_id = (SELECT MAX(v.id) FROM step_versions v WHERE v.step_id = w.step_id) ORDER BY w.id"
    ).fetchall())


def mention_refs(conn: sqlite3.Connection) -> Refs:
    """What a mention of another item in the page's text links to (SPEC UI-74): every item's title and project, and
    the item each step is on."""
    return Refs(
        {row["id"]: (row["title"], row["project"]) for row in conn.execute("SELECT id, title, project FROM items")},
        dict(conn.execute("SELECT id, item_id FROM steps").fetchall()),
    )


def _rendered_step(step: dict[str, Any], revisions: dict[int, int], verdicts: dict[int, str], refs: Refs) -> dict[str, Any]:
    """The step with its text as HTML and its writing verdict (`verdicts`, from `_writing_verdicts`); `revisions` holds
    every shared page's revision by id, for its figures, and `refs` what its mentions of other items link to."""
    return step | {
        "writing_verdict": verdicts.get(step["id"]),
        "ask_html": markdown_inline_html(step["ask"], refs),
        # The why line is one line too, and its links show as links (SPEC NEXT-5).
        "why_html": markdown_inline_html(step["why"], refs),
        "body_html": step_markdown_html(step["body"], revisions, refs),
        # A draft is posted elsewhere as it stands, where a link to Cockpit's page means nothing.
        "draft_html": markdown_html(step["draft"], NO_REFS),
        "details": [detail | {"body_html": step_markdown_html(detail["body"], revisions, refs)} for detail in step["details"]],
        # A command put before NEXT-40 has no explanation; it shows without one.
        "command": step["command"] and step["command"] | {"explain_html": markdown_html(step["command"].get("explain", ""), refs)},
    }


def _record_closure(
    conn: sqlite3.Connection, kind: str, ref_id: int, item_id: int, action: str, actor: str, at: str, note: str
) -> None:
    conn.execute(
        "INSERT INTO closures (kind, ref_id, item_id, action, closed_by, closed_at, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (kind, ref_id, item_id, action, actor, at, note),
    )


def _closed_recently(conn: sqlite3.Connection, now: datetime, refs: Refs) -> dict[str, list[dict[str, Any]]]:
    """Steps closed and items moved to stage done by an agent in the last day, and items archived in the last
    DONE_RECENT_DAYS, each with who closed it and the closer's note, or the user's reason from Close item: the user
    sees what agents closed and can undo it after the toast is gone (SPEC VIEW-4), and Finished this week lists the
    archived items beside the done ones (VIEW-20). All of them, with no cap: the sweeper checks every agent's close
    of the day from this list, and the page folds all but the newest few steps ."""
    since = _iso(now - timedelta(hours=CLOSED_RECENT_HOURS))
    week = _iso(now - timedelta(days=DONE_RECENT_DAYS))
    # A close by anyone but the user has its closure row; the one at the step's closing time is this close.
    steps = db.rows(
        conn.execute(
            "SELECT s.id, s.item_id, s.ask, s.status, s.closed_at, COALESCE(c.closed_by, s.updated_by) AS closed_by,"
            " COALESCE(c.note, '') AS note, i.title AS item_title, i.project AS item_project FROM steps s JOIN items i ON i.id = s.item_id"
            " LEFT JOIN closures c ON c.kind = 'step' AND c.ref_id = s.id AND c.closed_at = s.closed_at"
            " WHERE s.status != 'open' AND s.closed_at >= ? ORDER BY s.closed_at DESC, s.id DESC",
            (since,),
        ).fetchall()
    )
    # An archived item closed when it was archived (a later put moves its updated_at, not this), with the reason of the user's Close item when that close is the
    # latest of the user's closes, Undos and archive edits of it since its latest close (events order by id, as two
    # in one second share their time); an item still at stage done, at the agent's closure that moved it there (the item's own
    # updated_at moves with every later put).
    items = db.rows(
        conn.execute(
            "SELECT * FROM ("
            " SELECT i.id, i.key, i.title, i.project, i.stage, 'archived' AS action, i.archived_at AS closed_at,"
            " i.archived_by AS closed_by, COALESCE(c.note, (SELECT CASE WHEN e.kind = 'item.closed'"
            " THEN json_extract(e.data, '$.reason') END FROM events e WHERE e.item_id = i.id AND e.at >= (SELECT MAX(at)"
            " FROM item_finishes WHERE item_id = i.id AND how = 'close')"
            " AND (e.kind IN ('item.closed', 'item.reopened')"
            " OR (e.kind = 'item.edited' AND json_extract(e.data, '$.archived') IS NOT NULL))"
            " ORDER BY e.id DESC LIMIT 1), '') AS note FROM items i"
            " LEFT JOIN closures c"
            " ON c.kind = 'item' AND c.ref_id = i.id AND c.action = 'archived' AND c.closed_at = i.archived_at"
            " WHERE i.archived = 1 AND i.archived_at >= ?"
            " UNION ALL"
            " SELECT i.id, i.key, i.title, i.project, i.stage, 'stage done', c.closed_at, c.closed_by, c.note FROM closures c"
            " JOIN items i ON i.id = c.ref_id"
            " WHERE c.kind = 'item' AND c.action = 'stage done' AND c.closed_at >= ? AND i.archived = 0"
            " AND i.stage = 'done' AND c.id = (SELECT MAX(id) FROM closures WHERE kind = 'item' AND ref_id = i.id)"
            ") ORDER BY closed_at DESC, id DESC",
            (week, since),
        ).fetchall()
    )
    return {"steps": [step | {"ask_html": markdown_inline_html(step["ask"], refs)} for step in steps], "items": items}


def how_done(conn: sqlite3.Connection, item_ids: list[int]) -> dict[int, dict[str, str]]:
    """Who moved each of these done items to stage done, and why, for Finished this week (SPEC VIEW-20): the user's
    Task done or closing answer (`note` the answer's text), or an agent's close with its note (VIEW-4), whichever came
    last since the item's latest move to done (`done_at`). The user's Undo of a Task done takes that one back, so an
    agent's close before it counts again. Events order by id, since two in one second share their time. An item
    moved there some other way, such as the user's stage edit, is left out."""
    if not item_ids:
        return {}
    marks = ",".join("?" * len(item_ids))
    rows = conn.execute(
        "SELECT e.item_id, e.kind, e.at, e.actor, e.data FROM events e JOIN items i ON i.id = e.item_id"
        f" WHERE e.item_id IN ({marks}) AND e.kind IN ('item.completed', {AGENT_CLOSES[1:-1]}, 'item.reopened')"
        " AND e.at >= COALESCE(i.done_at, '') ORDER BY e.id",
        item_ids,
    ).fetchall()
    ends: dict[int, list[dict[str, str]]] = {}
    for item_id, kind, at, actor, data in rows:
        fields = json.loads(data)
        entries = ends.setdefault(item_id, [])
        if kind == "item.reopened":
            users = [entry for entry in entries if entry["by"] == "user"]
            if fields["how"] == "complete" and users:
                entries.remove(users[-1])
            continue
        # A completion's note is its closing answer or the last message sent with Task done; older events may have no
        # "note".
        note = (fields["answer"] or fields.get("note") or "") if kind == "item.completed" else fields["note"] or ""
        entries.append({"by": actor, "note": note, "at": at})
    return {item_id: entries[-1] for item_id, entries in ends.items() if entries}


def merged_closes(conn: sqlite3.Connection, now: datetime) -> list[dict[str, Any]]:
    """The items an agent closed because their PR merged (SPEC CONTROL-15), or because it finished a fire-and-forget
    to-do (CONTROL-24; `pull` null), after the user last acted
    on a step (any `step.*` event of the user's, or Task done), in the last day, oldest first, each by its latest
    such close and the PR GitHub confirmed then: the user's next step names them in one line, so the user sees the outcome.
    Events order by id, since two in one second share
    their time."""
    merges = db.rows(
        conn.execute(
            "SELECT e.id, e.item_id, e.actor, e.at, e.data, i.title FROM events e JOIN items i ON i.id = e.item_id"
            f" WHERE e.kind IN {AGENT_CLOSES} AND e.at >= ? AND i.stage = 'done' AND i.archived = 0"
            f" AND e.id = (SELECT MAX(id) FROM events WHERE kind IN {AGENT_CLOSES} AND item_id = e.item_id) ORDER BY e.id",
            (_iso(now - timedelta(hours=CLOSED_RECENT_HOURS)),),
        ).fetchall()
    )
    if not merges:
        return []
    acted = conn.execute(
        "SELECT MAX(id) FROM events WHERE at >= ? AND actor = 'user' AND (kind LIKE 'step.%' OR kind = 'item.completed')",
        (merges[0]["at"],),
    ).fetchone()[0] or 0
    return [
        {"item_id": merge["item_id"], "title": merge["title"], "pull": merge["data"].get("pull"),
         "note": merge["data"].get("note") or "", "closed_by": merge["actor"], "closed_at": merge["at"]}
        for merge in merges if merge["id"] > acted
    ]


def closed_by_user(conn: sqlite3.Connection, now: datetime) -> dict[str, list[int]]:
    """The items the user closed in the last day that are still closed, by id (SPEC CONTROL-17): `finished`,
    ended with Task done, a closing answer or Close item and not reopened; `archived`, closed in All work: the
    user's latest edit of `archived` archived it, in that time, with no agent's archive after it. An item can be
    in both. The
    notice after a close counts them; the page adds and drops ids as the user closes and undoes, so a change
    it replays on a state that already holds it counts once."""
    since = _iso(now - timedelta(hours=CLOSED_RECENT_HOURS))
    finished = conn.execute(
        "SELECT DISTINCT f.item_id FROM item_finishes f JOIN items i ON i.id = f.item_id"
        " WHERE f.reopened_at IS NULL AND f.at >= ? AND (i.archived = 1 OR i.stage = 'done') ORDER BY f.item_id",
        (since,),
    ).fetchall()
    archived = conn.execute(
        "SELECT DISTINCT e.item_id FROM events e JOIN items i ON i.id = e.item_id"
        " WHERE e.at >= ? AND e.actor = 'user' AND e.kind = 'item.edited' AND i.archived = 1"
        " AND EXISTS (SELECT 1 FROM json_each(e.data, '$.fields') WHERE value = 'archived')"
        " AND COALESCE(json_extract(e.data, '$.archived'), 1) = 1"
        " AND NOT EXISTS (SELECT 1 FROM events later WHERE later.id > e.id AND later.item_id = e.item_id"
        " AND later.actor = 'user' AND later.kind = 'item.edited'"
        " AND EXISTS (SELECT 1 FROM json_each(later.data, '$.fields') WHERE value = 'archived'))"
        " AND NOT EXISTS (SELECT 1 FROM closures c WHERE c.kind = 'item' AND c.ref_id = i.id"
        " AND c.action = 'archived' AND c.closed_at > e.at)"
        " ORDER BY e.item_id",
        (since,),
    ).fetchall()
    return {"finished": [row[0] for row in finished], "archived": [row[0] for row in archived]}


def steps_closed(conn: sqlite3.Connection, since: datetime) -> list[dict[str, Any]]:
    """When each step closed since `since`, and whether done or dismissed: the Usage page counts them per day
    (SPEC SESSION-14). A step reopened and closed again counts at its last close."""
    return db.rows(conn.execute(
        "SELECT closed_at, status FROM steps WHERE status != 'open' AND closed_at >= ? ORDER BY closed_at",
        (_iso(since),),
    ).fetchall())


def page_state(
    conn: sqlite3.Connection, me: str, sessions: list[Session], warnings: list[str], checkouts_dir: Path
) -> dict[str, Any]:
    """Everything the page and `cockpit get` show, in one response."""
    now = datetime.now(UTC)
    names = shown_session_names(conn, sessions)
    items = _unarchived_items(conn)
    revisions = dict(conn.execute("SELECT id, revision FROM item_pages").fetchall())
    verdicts = _writing_verdicts(conn)
    refs = mention_refs(conn)
    ordered = {
        group: [_rendered_step(step, revisions, verdicts, refs) for step in steps] for group, steps in _ranked_open(conn, items, now).items()
    }
    by_session = {session.session_id: session for session in sessions}
    session_names = {session_id: f'session "{name}"' for session_id, name in names.items() if name}
    dialogs = open_dialogs(sessions)

    def target(step: dict[str, Any]) -> dict[str, Any] | None:
        session_id = step["session_id"] or (step["item"]["sessions"][0] if step["item"]["sessions"] else None)
        if session_id is None:
            return None
        session = by_session.get(session_id)
        return {
            "session_id": session_id,
            "provider": session.provider if session else "claude",
            # The short id `claude attach` takes; empty for a session no longer on this machine.
            "id": session.id if session else "",
            "name": names[session.session_id] if session else "",
            "state": session.state if session else "unknown",
            "alive": session.alive if session else False,
            "working_now": session.working_now if session else False,
            # The server never archives the sweep's session on a Done; the page predicts the same from this.
            "is_sweep": session is not None and session.name in SWEEP_SESSION_NAMES,
        }

    runs = latest_runs(conn)
    # The page ticks a running timer from `started_at`, so the state changes only when a timer
    # starts or stops, not on every poll.
    timers = _timers(conn)
    # The page's Take moves the same step back to ready next as `_take_place`.
    started = _started_in_place(conn)
    for group in ordered.values():
        for step in group:
            step["target"] = target(step)
            step["run"] = runs.get(step["id"])
            step["timer"] = timers.get(step["id"])
            step["user_on_it"] = bool(step["timer"] and step["timer"]["started_at"])
            step["started_in_place"] = step["id"] in started
    item_time: dict[int, dict[str, Any]] = {}
    for timer in timers.values():
        spent = item_time.setdefault(timer["item_id"], {"seconds": 0, "started_at": None})
        spent["seconds"] += timer["seconds"]
        spent["started_at"] = spent["started_at"] or timer["started_at"]
    running_timer = next(((step_id, timer) for step_id, timer in timers.items() if timer["started_at"]), None)
    for step in ordered["with_agents"]:
        step["handoff"] = get_message(conn, step["handoff_message_id"])

    # Blocked and waiting lists the sessions at a dialog in their terminal, even with a step in the user's view:
    # only an answer to the dialog moves one on (SPEC PERM-8). A session that stopped to ask the user something
    # reaches them as a step (questions.py). The Markdown is for agents (`cockpit get --json`), the HTML for the page.
    waiting_sessions = []
    for session in at_dialogs(sessions):
        text = last_message(session.session_id)
        waiting_sessions.append(as_dict(session) | {
            "name": names[session.session_id], "last_message": text, "last_message_html": markdown_html(text, refs),
        })
    # Read without the recent-history limit, so the sweep's flood guard sees every earlier send.
    last_send_by_session: dict[str, str] = {}
    last_send_by_item: dict[int, str] = {}
    for record in conn.execute(
        "SELECT session_id, item_id, MAX(created_at) AS at FROM messages"
        " WHERE author LIKE 'agent:%' GROUP BY session_id, item_id"
    ).fetchall():
        if record["session_id"]:
            sid = record["session_id"]
            last_send_by_session[sid] = max(last_send_by_session.get(sid, ""), record["at"])
        if record["item_id"] is not None:
            last_send_by_item[record["item_id"]] = max(last_send_by_item.get(record["item_id"], ""), record["at"])

    recent_since = _iso(now - timedelta(hours=SESSION_RECENT_HOURS))
    archives = archive_states(conn, {session.session_id for session in sessions if session.alive})
    move_waits = {row["session_id"]: row["account"] for row in conn.execute("SELECT session_id, account FROM session_move_waits")}
    queue = ordered["queue"]
    queued_by_item: dict[int, int] = {}
    for step in queue + ordered["ready_next"]:
        queued_by_item[step["item_id"]] = queued_by_item.get(step["item_id"], 0) + 1
    asking_items = {step["item_id"] for step in queue + ordered["ready_next"] if not is_read(step)}
    reading_items = {step["item_id"] for step in queue + ordered["ready_next"] if is_read(step)}
    handed_items = {step["item_id"] for step in ordered["with_agents"]}
    messaged_items = messaged_item_ids(conn, now)
    step_sessions = sessions_of_steps([step for group in ordered.values() for step in group])
    # A session Cockpit does not list (an interactive one, with no job file) may be working: only a listed one
    # shows that its work stopped, as in `stalled_since`.
    working_items = {
        item["id"] for item in items.values()
        if any(
            session_id not in by_session or working(by_session[session_id])
            for session_id in item["sessions"] + step_sessions.get(item["id"], [])
        )
    }
    untriaged = untriaged_ids(conn)
    pulls = github.pulls(conn)
    item_list = _with_attachments(conn, [
        _on_board(
            item, queued_by_item, asking_items, reading_items, working_items, handed_items, messaged_items, last_send_by_item,
            untriaged, item_time, pulls,
        )
        for item in items.values()
        if on_all_work(item, now)
    ])
    item_list.sort(key=lambda item: item["updated_at"], reverse=True)
    listed_items = {item["id"] for item in item_list}
    listed_sessions = [
        session
        for session in sorted(sessions, key=lambda session: session.updated_at, reverse=True)
        # A session Cockpit took off `claude agents` is finished, whatever state its transcript last showed.
        if (session.alive or (session.state == "blocked" and not session.removed_at) or session.updated_at >= recent_since)
        and archives.get(session.session_id, {}).get("state") != "archived"
    ]
    listed_session_ids = {session.session_id for session in listed_sessions}
    starts = session_starts(conn, listed_session_ids)
    # A snoozed result to read keeps its item from stalling only when nothing else holds it (VIEW-2): an item
    # handed to an agent, or waiting on one, still stalls when that agent stops.
    agent_held = handed_items | {
        item["id"] for item in items.values() if any(entry["kind"] == "agent" for entry in item["waiting_on"])
    }
    snoozed_items = {
        step["item_id"] for step in ordered["snoozed"] if not is_read(step) or step["item_id"] not in agent_held
    }
    ended = how_done(conn, [item["id"] for item in item_list if item["stage"] == "done"])
    handed_asks = {step["item_id"]: step["ask"] for step in ordered["with_agents"]}
    session_names_by_id = {session_id: name for session_id, name in names.items() if name}
    for item in item_list:
        item["ended"] = ended.get(item["id"]) if item["stage"] == "done" else None
        # The card shows both with their mentions of other items as links (UI-74).
        item["summary_html"] = mentions_html(item["summary"], refs)
        item["agent_summary_html"] = mentions_html(item["agent_summary"], refs)
        # The page's lamp counts these with the item's own sessions, as working_items does (VIEW-2).
        item["step_sessions"] = list(dict.fromkeys(step_sessions.get(item["id"], [])))
        item["stalled_since"] = stalled_since(
            item, item["step_sessions"], item["id"] in snoozed_items, item["id"] in messaged_items, by_session, now
        )
        held_by = item["sessions"] + [entry["who"] for entry in item["waiting_on"] if entry["kind"] == "agent"]
        active = any(working(by_session[sid]) for sid in held_by + item["step_sessions"] if sid in by_session)
        item |= work_group(item, handed_asks, session_names_by_id, active, now)

    balls = [item["ball"] for item in item_list]
    in_progress_ids = {
        item["id"] for item in item_list if not item["archived"] and item["stage"] != "done" and item["ball"] != "you"
    }
    sweep = db.kv_get(conn, "last_sweep")
    running = sweep_running(conn, now) is not None
    state_warnings = list(warnings) + unlinked_warnings(conn)
    closed_recently = _closed_recently(conn, now, refs)
    closed = closed_by_user(conn, now)
    if sweep is not None and not sweep["finished_at"] and not running:
        started, last = rank.parse_time(sweep["started_at"]), sweep_last_active(conn, sweep)
        state_warnings.append(
            f"The sweep that started at {started:%H:%M} UTC last wrote at {last:%H:%M} UTC and stopped without"
            " finishing."
        )
    return {
        "now": db.now_iso(),
        "me": me,
        "counts": {
            "waiting_on_user": len(queue),
            "sessions_waiting": len(waiting_sessions),
            # Doing work now: not idle with only a background task left, and not the sweeper.
            "agents_working": sum(
                1 for session in sessions if session.working_now and session.name not in SWEEP_SESSION_NAMES
            ),
            **{ball: balls.count(ball) for ball in BALLS},
            "busy": sum(1 for item in item_list if item["group"] == "busy"),
            "waiting": sum(1 for item in item_list if item["group"] == "waiting"),
            "ready_next": len(ordered["ready_next"]),
            "snoozed": len(ordered["snoozed"]),
            "with_agents": len(ordered["with_agents"]),
            "stalled": sum(1 for item in item_list if item["stalled_since"]),
            "failed_messages": conn.execute(
                "SELECT COUNT(*) FROM messages WHERE state = 'failed' AND dismissed_at IS NULL"
            ).fetchone()[0],
            "closed_by_user": len(set(closed["finished"]) | set(closed["archived"])),
        },
        "oldest_waiting_minutes": (
            int((now - min(rank.parse_time(step["queued_at"]) for step in queue)).total_seconds() // 60)  # ty: ignore[invalid-argument-type]
            if queue
            else None
        ),
        "queue": queue,
        "ready_next": ordered["ready_next"],
        "snoozed": ordered["snoozed"],
        "with_agents": ordered["with_agents"],
        "closed_recently": closed_recently,
        "merged_closes": merged_closes(conn, now),
        "closed_by_user": closed,
        "waiting_sessions": waiting_sessions,
        "items": item_list,
        # The ended items `items` leaves out (closed, or done long ago) whose sessions the page lists, so a session
        # of an ended task on no open item goes by the task's title (SPEC VIEW-3).
        "ended_items": [
            item
            for item in db.rows(conn.execute("SELECT id, title, sessions, updated_at, archived FROM items").fetchall())
            if item["id"] not in listed_items and any(session_id in listed_session_ids for session_id in item["sessions"])
        ],
        "goals": goal_list(conn),
        "sessions": [
            as_dict(session)
            | {
                "name": names[session.session_id],
                "is_sweep": session.name in SWEEP_SESSION_NAMES
                or starts.get(session.session_id, {}).get("new_name") in SWEEP_SESSION_NAMES,
                "is_briefing": starts.get(session.session_id, {}).get("new_name") == BRIEFING_SESSION_NAME,
                # Who started it and when, from the message Cockpit started it with; None for a session started
                # in a terminal, so a session on no item says where it came from (SPEC VIEW-3).
                "started": starts.get(session.session_id),
                "last_agent_message_at": last_send_by_session.get(session.session_id),
                "archive": archives.get(session.session_id),
                # Why the sweeper cannot close it now; None when it can (SPEC SESSION-13).
                "close_refusal": close_refusal(conn, session) if session.alive else None,
                # A move the user asked for waits for this account to take it (moves.wait); None means there is no
                # pending move.
                "move_waits": move_waits.get(session.session_id),
            }
            for session in listed_sessions
        ],
        # The user's archives of the last two days, for the sweep to check (SPEC SESSION-4).
        "recent_archives": recent_archives(conn, now),
        "linked_sessions": sorted({sid for item in items.values() for sid in item["sessions"]}),
        # Every failed message that is not dismissed stays listed with its Retry,
        # and every waiting one with its Send now, however many messages came
        # after it; a dismissed one shows like any other message while it is
        # among the newest, so the list does not grow. The user's own messages
        # of the last day all stay, for the work list's Your latest message view.
        "messages": [
            message | {
                "failure": failure_sentence(message, session_names) if message["state"] == "failed" else None,
                "behind_dialog": behind_dialog(message, dialogs),
            }
            for message in db.rows(
                conn.execute(
                    "SELECT * FROM messages WHERE (state = 'failed' AND dismissed_at IS NULL)"
                    " OR (state = 'queued' AND waits_since IS NOT NULL)"
                    " OR (author = 'user' AND created_at >= ?)"
                    " OR id IN (SELECT id FROM messages ORDER BY id DESC LIMIT ?) ORDER BY id DESC",
                    (recent_since, RECENT_MESSAGES),
                ).fetchall()
            )
        ],
        "unread_feedback": len(feedback_list(conn, unread_only=True)),
        # The running timer for the header: its step and item, and the item's time before this span.
        "timer": {
            "step_id": running_timer[0],
            "item_id": running_timer[1]["item_id"],
            "item_title": get_item(conn, running_timer[1]["item_id"])["title"],
            "started_at": running_timer[1]["started_at"],
            "seconds": item_time[running_timer[1]["item_id"]]["seconds"],
        } if running_timer else None,
        # The user's last input on any page, which the page counts as activity, and the count of inputs (SPEC TRACK-5).
        "input": _input(conn),
        # The question after a timer stopped for no activity, while the user has not answered it (SPEC TRACK-5).
        "away": _away(conn),
        "last_sweep": sweep,
        "sweep_running": running,
        "sweep_times": patterns.sweep_times(conn, patterns.SWEEP_TIME_DAYS, now),
        "sweep_time_days": patterns.SWEEP_TIME_DAYS,
        # How far the progress estimates run ahead of the user's time, for the bar to take off (SPEC NEXT-35).
        "progress_correction": timesheet.progress_correction(conn),
        # The sweeper's list, less the work since closed or now waiting on the user (SPEC VIEW-17).
        "in_progress": [
            entry for entry in (db.kv_get(conn, "in_progress") or {"entries": []})["entries"]
            if entry["item_id"] in in_progress_ids
        ],
        "checkouts": checkouts(checkouts_dir),
        # Comments on shared pages that go with the user's answer to their step (SPEC NEXT-38).
        "page_comments": unsent_step_comments(conn),
        # Choices on shared pages that go with the user's answer to their step (SPEC NEXT-48).
        "page_choices": unsent_step_choices(conn),
        "warnings": state_warnings,
    }
