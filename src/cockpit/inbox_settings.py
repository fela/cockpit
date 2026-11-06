"""Inbox settings (SPEC UI-70): how the sweeper ranks the user's work, and what it checks for new work.

How the inbox is ranked is a list of factors, most important first, that the user edits and reorders on the page; the sweeper
weighs them together for each step. What to check is one free text. Cockpit's own list and text live here, in code,
so an improvement to them reaches every user who has not written their own; the database (kv `inbox_settings`) keeps
only what the user changed. An empty list or a blank text gives Cockpit's back. The sweeper reads both with
`cockpit inbox-settings` at the start of each sweep and follows them.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from cockpit import db, store

KEY = "inbox_settings"
POINTS = 30
POINT_CHARS = 600
TEXT_CHARS = 6000

# Shown above the factors on the page and to the sweeper: how the factors combine.
RULES_LEAD = ("These factors compete. For each step, weigh them together and judge for yourself; a factor higher in "
              "the list counts for more than one lower down.")

# What makes a step worth the user's attention now, most important first, in the user's voice.
RULES = [
    "Whole work item is close to done and fresh in mind: the last small step before work ships or closes, work I put "
    "a lot of time into, and work I touched recently",
    "Work I worked on recently, the more recent the easier it is to work on. Weight this very highly if it was "
    "minutes ago, less if was hours etc",
    "The morning briefing: I want to read it early in the day.",
    "Near instant to finish: a glance, a one-press answer, an approval, a merge, or confirming that finished work is "
    "done. A long review or design choice counts for less, unless I am already in the middle of it.",
    "Someone waits for it: a result or answer to something I asked, a step an agent returned with its reply, or a "
    "short question from a teammate.",
    "It's in my current priorities, especially if priorities was edited recently",
    "Obviously urgent (blocking someone, upcoming deadline)",
    "Urgent or high item (urgent higher)",
    "Long overdue: work nobody looked at for days gains a little, once it is checked that it still holds.",
]

# What the sweeper checks regularly for new work, in the user's voice.
SOURCES = (
    "- GitHub: open PRs assigned to me, PRs that ask for my review, and my own drafts; new reviews, review threads,"
    " comments and failing CI on my PRs; open issues assigned to me.\n"
    "- Linear: my notifications from the last three days, and tickets assigned to me that are in progress, in the "
    "current cycle, urgent or high.\n"
    "- Slack: messages from the last three days that mention me or are sent to me, ask something, and have no reply"
    " from me; and messages I saved for later, each as a task of its own.\n"
    "- Notion: comments, mentions and review requests for me from the last three days."
)


def get(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Both settings as the user sees them, the user's own where they changed one, each with Cockpit's beside it."""
    stored = db.kv_get(conn, KEY) or {}
    return {
        "rules": {"lead": RULES_LEAD, "points": stored.get("rules") or RULES, "default_points": RULES},
        "sources": {"text": stored.get("sources") or SOURCES, "default_text": SOURCES},
    }


def put(conn: sqlite3.Connection, rules: list[str], sources: str) -> dict[str, dict[str, Any]]:
    """Replace both settings. Cockpit's own, an empty list or a blank text stores nothing, so it follows Cockpit's."""
    points = [" ".join(point.split()) for point in rules]
    points = [point for point in points if point]
    if len(points) > POINTS:
        raise store.Invalid(f"How your inbox is ranked holds at most {POINTS} points")
    if any(len(point) > POINT_CHARS for point in points):
        raise store.Invalid(f"each point is at most {POINT_CHARS} characters")
    text = sources.strip()
    if len(text) > TEXT_CHARS:
        raise store.Invalid(f"What to check is at most {TEXT_CHARS} characters")
    stored: dict[str, Any] = {}
    if points and points != RULES:
        stored["rules"] = points
    if text and text != SOURCES:
        stored["sources"] = text
    with db.transaction(conn):
        db.kv_set(conn, KEY, stored)
    return get(conn)


def as_text(conn: sqlite3.Connection, user: str) -> str:
    """Both settings as the sweeper reads them, under a heading each."""
    shown = get(conn)
    points = "\n".join(f"{n}. {point}" for n, point in enumerate(shown["rules"]["points"], start=1))
    text = (f"## How {user}'s inbox is ranked\n\n{RULES_LEAD}\n\n{points}\n\n"
            f"## What to check for new work\n\nCheck these for work that needs {user}:\n\n{shown['sources']['text']}")
    return store.defused(text)
