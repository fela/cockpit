"""The morning briefing on the user's team (SPEC VIEW-8).

The team is the people the user works with most closely, in the user's own words (kv `team`): the page
asks for them the first time the user opens Cockpit, and the Briefing page changes them. Empty text means
the user named no one, and no briefing is asked for; null means the page has not asked yet.

Once a day, from BRIEFING_HOUR in the user's timezone, while a team is set, Cockpit asks one long-lived session,
named "Cockpit briefing", to write what the team did since the last briefing's window. The
session reads GitHub, Linear, Slack and Cockpit, writes the briefing in plain words and
stores it with `cockpit briefing put`. The page's Briefing tab shows it, marked until the
user opens it. Each stored briefing is also a step to read in the user's queue, on a high-priority item of its own
(`ask_to_read`); reading it in either place counts for both (`mark_read`, `read_on_finish`). As with the sweeper, the launch goes to the session the last launch reached
while that session exists, else to a new one. A launch whose message did not reach a
session is tried again RETRY_MINUTES later, on the same day, unless that day's briefing was stored: a message whose
delivery is uncertain may have reached the session, whose turn has written the briefing by then.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from cockpit import config, db, projects, render, store, sweeper
from cockpit.config import Setup

BRIEFING_HOUR = 7
# The first briefing's window; each later one starts where the one before ended.
BRIEFING_HOURS = 24
RETRY_MINUTES = 60
SESSION_NAME = store.BRIEFING_SESSION_NAME
BODY_LIMIT = 20000
# The page lists this many earlier briefings.
LISTED = 30
TEAM_LIMIT = 2000
# Each briefing has its own item and a step that asks the user to read it.
ITEM_PREFIX = "note:briefing-"
READ_NEED = "read:briefing"
READ_ASK = "Read the morning briefing on your team"
READ_WHY = "Your daily catch-up on what the team did since the last briefing. Task done marks it read."
# The briefing usually comes in while sweeps are paused for the night (sweeper.PAUSE_AFTER_MINUTES), so it starts with a
# score of its own rather than an unranked step's, and the next sweep judges it like any other.
READ_SCORE = 90
READ_REASONS = ["daily briefing"]
# A new briefing shows this project's logo from the start; a Cockpit without it leaves the briefing to the tidy session
# (projects.py).
PROJECT = "team"
SUMMARY_TITLE = "executive summary"
HEADING = re.compile(r"(?m)^##[ \t]+(.*)$")
# A link's address or a bare web address (kept, group 1), or a commit hash or session id outside them.
ADDRESS_OR_ID = re.compile(rf"(\]\([^)]*\)|{store.BARE_URL.pattern})|{store.HEX_ID.pattern}", re.IGNORECASE)

log = logging.getLogger(__name__)

SessionExists = Callable[[str], bool]


def _local_words(moment: datetime, setup: Setup) -> str:
    return moment.astimezone(setup.timezone).strftime("%a %-d %b %H:%M")


def team(conn: sqlite3.Connection) -> str | None:
    """The people the briefing follows, in the user's words; "" when the user named no one, None before the
    page asked."""
    return db.kv_get(conn, "team")


def put_team(conn: sqlite3.Connection, text: str) -> str:
    """Set the people the briefing follows; empty text says the user wants no briefing."""
    words = text.strip()
    if len(words) > TEAM_LIMIT:
        raise store.Invalid(f"the team has {len(words)} characters; at most {TEAM_LIMIT}")
    with db.transaction(conn):
        db.kv_set(conn, "team", words)
    return words


def prompt(setup: Setup, people: str, day: str, since: datetime, until: datetime) -> str:
    """What the briefing session is asked to do for `day`, covering `since` to `until`, on `people`. An agent may
    set the team, so its words go in defused (store.defused), as all agent text Cockpit forwards does."""
    name = setup.name
    day_words = datetime.fromisoformat(day).strftime("%A %-d %B")
    return f"""Write {name}'s morning briefing for {day_words}: what happened on their team from \
{_local_words(since, setup)} to {_local_words(until, setup)}, {name}'s time ({setup.timezone.key}); in UTC from \
{since:%Y-%m-%dT%H:%MZ} to {until:%Y-%m-%dT%H:%MZ}. {name} reads it at the start of their day to catch up.

The team: the people {name} works with most closely, in {name}'s words on Cockpit:
<team>
{store.defused(people)}
</team>
Where these words leave out someone's GitHub login or Slack name, find it from their name, for example from who \
reviews and merges in the repositories {name} works in.

Find out what happened in that window by reading:
- GitHub, with `gh`: the PRs each person opened, pushed to, merged or reviewed, and what they wrote on PRs and issues.
- Linear: the tickets each person created, moved or commented on.
- Slack: what each person wrote, and team threads with a decision or a question.
- Cockpit: `cockpit get` and `cockpit goals` show {name}'s goals and open work, which tell you what matters to {name}.
Also cover other work in that window that touches {name}'s goals or open work, such as a release, an incident or a \
decision.

Write the briefing in Markdown, in plain words that {name} follows without opening a link. Keep it short: {name} \
reads all of it in about three minutes, so the whole briefing stays under 500 words. Cockpit's page shows the day and \
the window above it, so begin with the first section:
- `## Executive summary` comes first: at most five bullets of one sentence each, the things that matter most to \
{name} today, such as something waiting on {name}, a decision made or asked for, or a change to {name}'s own work. \
{name} can stop reading after it and know the day.
- Then a short section per person: two or three bullets on what they did and why it matters. One line is enough for \
a quiet day.
- Then other team news, if there is any, in one or two bullets.
- Leave out what the summary already says unless the detail adds something {name} needs.
- Explain the context of each point the first time it comes up, and name each PR or ticket by what it is, with its \
link after the name: "the reminder fix ([#10](<the PR's address>))".
- Keep to what {name} would want to know: bot comments, rebases, CI reruns and Cockpit steps that did not change in \
the window stay out.

This is reading only: post, comment or change nothing on GitHub, Linear or Slack. The one write is storing the \
briefing, with the Markdown on stdin:

cockpit briefing put --day {day} --as briefing <<'EOF'
<the briefing>
EOF

A second put on the same day replaces the first. Then end your turn."""


def _retried(conn: sqlite3.Connection, record: dict[str, Any], launched: dict[str, Any]) -> bool:
    """Whether Cockpit asks again for a briefing whose message failed: only while that day's briefing is not
    stored, as a message whose delivery is uncertain may have reached its session after all."""
    written = conn.execute("SELECT 1 FROM briefings WHERE day = ?", (record["day"],)).fetchone() is not None
    return launched["state"] == "failed" and not written


def _launch_session(conn: sqlite3.Connection, record: dict[str, Any] | None, exists: SessionExists) -> str | None:
    """The session the last launch reached, while it exists."""
    if record is None:
        return None
    launched = store.get_message(conn, record["message_id"])
    if launched["state"] != "sent" or not exists(launched["session_id"]):
        return None
    return launched["session_id"]


def launch_once(conn: sqlite3.Connection, setup: Setup, exists: SessionExists, now: datetime) -> int | None:
    """Queue today's briefing request when it is due and a team is set; returns the message to deliver. The
    window starts where the last day's ended, so a late morning leaves no gap; the first covers BRIEFING_HOURS."""
    people = team(conn)
    if not people:
        return None
    local = now.astimezone(setup.timezone)
    day = local.date().isoformat()
    record = db.kv_get(conn, "briefing_launch")
    if record is not None and record["day"] == day:
        launched = store.get_message(conn, record["message_id"])
        waited = now - datetime.fromisoformat(launched["created_at"])
        if not _retried(conn, record, launched) or waited < timedelta(minutes=RETRY_MINUTES):
            return None
        since = datetime.fromisoformat(record["since"])
    elif local.hour < BRIEFING_HOUR:
        return None
    elif record is not None:
        since = datetime.fromisoformat(record["until"])
    else:
        since = now - timedelta(hours=BRIEFING_HOURS)
    text = prompt(setup, people, day, since, now)
    session = _launch_session(conn, record, exists)
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
        db.kv_set(
            conn, "briefing_launch",
            {"day": day, "since": f"{since:%Y-%m-%dT%H:%M:%SZ}", "until": f"{now:%Y-%m-%dT%H:%M:%SZ}",
             "message_id": cursor.lastrowid},
        )
    return cursor.lastrowid


def put(conn: sqlite3.Connection, actor: str, day: str, body: str) -> dict[str, Any]:
    """Store the briefing for `day`, the day Cockpit last asked for one; a second put that day replaces it. A put
    for an earlier day is refused: a briefing finished after the next morning's request would otherwise replace
    that day's ."""
    text = body.strip()
    if not text:
        raise store.Invalid("the briefing is empty")
    if len(text) > BODY_LIMIT:
        raise store.Invalid(f"the briefing has {len(text)} characters; at most {BODY_LIMIT}")
    now = db.now_iso()
    with db.transaction(conn):
        record = db.kv_get(conn, "briefing_launch")
        if record is None:
            raise store.Invalid("Cockpit has not asked for a briefing yet; it asks the briefing session each morning")
        if record["day"] != day:
            raise store.Conflict(
                f"Cockpit now asks for the briefing for {record['day']}, so the one for {day} is not stored"
            )
        conn.execute(
            "INSERT INTO briefings (day, since, until, body, created_by, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(day) DO UPDATE SET since = excluded.since,"
            " until = excluded.until, body = excluded.body, created_by = excluded.created_by,"
            " updated_at = excluded.updated_at",
            (record["day"], record["since"], record["until"], text, actor, now, now),
        )
    found = conn.execute("SELECT id FROM briefings WHERE day = ?", (record["day"],)).fetchone()
    stored = get(conn, found[0])
    try:
        ask_to_read(conn, stored, config.setup().origin)
    except store.Invalid:
        # The briefing is stored and on its page; only its step failed, and the error log says so.
        log.exception("the step to read briefing %s could not be put", stored["id"])
    return stored


def _sections(body: str) -> list[tuple[str, str]]:
    """The briefing's `## ` sections as (title, text); text before the first heading has the title ""."""
    marks = list(HEADING.finditer(body))
    sections = [("", body[: marks[0].start()] if marks else body)]
    for at, mark in enumerate(marks):
        end = marks[at + 1].start() if at + 1 < len(marks) else len(body)
        sections.append((mark.group(1).strip(), body[mark.end() : end]))
    return [(title, text.strip()) for title, text in sections if text.strip()]


def _visible(text: str) -> int:
    """Characters as a step's limits count them (`store._check_step_text`)."""
    return len(store._visible_text(store.QUOTED_LINE.sub("", text)))


def _fit(text: str, limit: int, tail: str) -> str:
    """`text` and then `tail`, cut to its first whole lines and an ellipsis so that the two fit `limit` visible
    characters."""
    lines = text.splitlines()
    joined = lambda kept, cut: "\n".join([*kept, *(["…"] if cut else []), "", tail]).strip()
    for count in range(len(lines), -1, -1):
        fitted = joined(lines[:count], count < len(lines))
        if _visible(fitted) <= limit:
            return fitted
    return tail


def _without_ids(text: str) -> str:
    """`text` with each commit hash or session id outside a link's address replaced by "…", as a step's body may
    hold none (`store._check_step_text`, which also leaves addresses out)."""
    cleaned = ADDRESS_OR_ID.sub(lambda match: match.group(1) or "…", text)
    if store.HEX_ID.search(store._visible_text(cleaned)):
        # An id split by marks, such as abc**1234**, shows whole once the marks are gone.
        cleaned = ADDRESS_OR_ID.sub(lambda match: match.group(1) or "…", cleaned.replace("**", "").replace("`", ""))
    if store.HEX_ID.search(store._visible_text(cleaned)):
        # An address the check shows as text, such as "](/commit/aaaa111)" without its label: every id goes.
        cleaned = store.HEX_ID.sub("…", cleaned)
    return cleaned


def step_text(found: dict[str, Any], origin: str) -> dict[str, Any]:
    """The read step for briefing `found`: its executive summary on the card, the rest of it in one collapsed
    section, each with a link to the whole briefing on its page at `origin` (a step shows only full web addresses
    as links). A step's body may hold no commit hash or session id, so these leave the summary; the whole briefing
    keeps them."""
    link = f"[Open the whole briefing]({origin}/briefing?id={found['id']})"
    sections = _sections(found["body"])
    first = next((at for at, (title, _) in enumerate(sections) if title.casefold() == SUMMARY_TITLE), 0)
    summary = _without_ids(sections[first][1]) if sections else ""
    rest = "\n\n".join(
        f"**{title}**\n\n{text}" if title else text for at, (title, text) in enumerate(sections) if at != first
    )
    details = [{"title": "The rest of the briefing", "body": _fit(rest, store.DETAIL_CHARS, link)}] if rest else []
    return {
        "ask": READ_ASK, "why": READ_WHY, "body": _fit(summary, store.TEXT_LIMITS["body"], link), "details": details,
        "size": "small", "progress": 0,
    }


def ask_to_read(conn: sqlite3.Connection, found: dict[str, Any], origin: str) -> None:
    """Put briefing `found` in the user's queue as a step to read, on a high-priority item of its own, so it comes in
    each day with the rest of the user's work (SPEC VIEW-8). The item has no session: Task done tells no one, and the
    long-lived briefing session is never archived with it. A second put the same day rewrites the open step; once
    the user read it, the step stays closed. Only the first put sets the project and the starting score, so a rewrite
    leaves what the sweeper or the tidy session decided since; it also asks for one reorder of the queue, so the
    order is right for the morning although sweeps are usually stopped by then (SPEC SWEEP-25). A briefing the user already opened on its page gets no
    step: the check and the put are one transaction, so an open of the page either comes first and is seen, or comes after and
    completes the step (`mark_read`)."""
    day = datetime.fromisoformat(found["day"])
    key = f"{ITEM_PREFIX}{found['day']}"
    with db.transaction(conn):
        if found["id"] <= _read_id(conn):
            return
        first_put = conn.execute(
            "SELECT 1 FROM steps s JOIN items i ON i.id = s.item_id WHERE i.key = ? AND s.need = ?", (key, READ_NEED)
        ).fetchone() is None
        project = projects.resolve(conn, PROJECT) if first_put else None
        item = store.upsert_item(
            conn,
            {
                "key": key, "title": f"Briefing {day:%a %-d %b}",
                "summary": f"Morning briefing for {day:%A %-d %B}", "priority": "high",
                "links": [{"label": "Briefing page", "url": f"{origin}/briefing?id={found['id']}"}],
            }
            | ({"project": project} if project else {}),
            "cockpit",
        )
        if item["stage"] == "done" or item["archived"]:
            return
        score = {"score": READ_SCORE, "reasons": READ_REASONS} if first_put else {}
        store.upsert_step(conn, {"item_id": item["id"], "need": READ_NEED} | step_text(found, origin) | score, "cockpit")
        if first_put:
            sweeper.reorder_soon(conn, "the morning briefing just came in")


def _read_items(conn: sqlite3.Connection, day: str) -> list[dict[str, Any]]:
    """The open read step of the briefing for `day`, with its item. A step the user handed to a session stays with
    it: the user asked something about that briefing."""
    return db.rows(
        conn.execute(
            "SELECT s.id AS step_id, i.id AS item_id FROM steps s JOIN items i ON i.id = s.item_id"
            f" WHERE s.need = ? AND i.key = ? AND {store.OPEN} AND s.handoff_message_id IS NULL"
            " AND i.archived = 0 AND i.stage IS NOT 'done'",
            (READ_NEED, f"{ITEM_PREFIX}{day}"),
        ).fetchall()
    )


def read_on_finish(conn: sqlite3.Connection, item: dict[str, Any]) -> None:
    """The user pressed Task done on a briefing's item: that briefing counts as read on its page too."""
    if not item["key"].startswith(ITEM_PREFIX) or item["stage"] != "done":
        return
    found = conn.execute("SELECT id FROM briefings WHERE day = ?", (item["key"].removeprefix(ITEM_PREFIX),)).fetchone()
    if found is not None:
        mark_read(conn, found[0])


def get(conn: sqlite3.Connection, briefing_id: int) -> dict[str, Any]:
    """The briefing with its HTML, and `to_read`: whether its read step is open, so opening an older briefing on the
    page completes that step too."""
    found = db.row(conn.execute("SELECT * FROM briefings WHERE id = ?", (briefing_id,)).fetchone())
    if found is None:
        raise store.NotFound(f"briefing {briefing_id} does not exist")
    return found | {"body_html": render.markdown_html(found["body"], store.mention_refs(conn)), "to_read": bool(_read_items(conn, found["day"]))}


def _read_id(conn: sqlite3.Connection) -> int:
    return db.kv_get(conn, "briefing_read") or 0


def mark_read(conn: sqlite3.Connection, briefing_id: int) -> None:
    """The user opened this briefing; it and every older one count as read on the Briefing page, and its own read
    step completes as the user's Task done would (`store.finish_item`), so the user can undo it there. Each briefing
    covers its own day, so older briefings' steps stay open. All of it is one transaction: a step handed to a session
    meanwhile changes its session and is left with it (`check_seen_target` refuses), and any other failure leaves the
    briefing unread so the next open tries again. The finish tells no session, so it needs no checkouts folder."""
    found = get(conn, briefing_id)
    with db.transaction(conn):
        for left in _read_items(conn, found["day"]):
            shown = store.target_session(conn, left["item_id"], left["step_id"])
            try:
                store.finish_item(conn, left["item_id"], left["step_id"], "complete", None, "", "", shown, None, Path())
            except store.Conflict:
                continue
        db.kv_set(conn, "briefing_read", max(_read_id(conn), briefing_id))


def launch_status(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """The last request for a briefing: its day and window, and whether its message reached a session."""
    record = db.kv_get(conn, "briefing_launch")
    if record is None:
        return None
    launched = store.get_message(conn, record["message_id"])
    written = conn.execute("SELECT 1 FROM briefings WHERE day = ?", (record["day"],)).fetchone() is not None
    retry_at = None
    if _retried(conn, record, launched):
        retry = datetime.fromisoformat(launched["created_at"]) + timedelta(minutes=RETRY_MINUTES)
        retry_at = f"{retry:%Y-%m-%dT%H:%M:%SZ}"
    return {
        "day": record["day"], "since": record["since"], "until": record["until"], "asked_at": launched["created_at"],
        "state": launched["state"], "detail": launched["detail"], "session_id": launched["session_id"],
        "written": written, "retry_at": retry_at,
    }


def listing(conn: sqlite3.Connection) -> dict[str, Any]:
    """The page's data: the newest briefings without their text, which one is read, the last request and the
    team."""
    briefings = db.rows(
        conn.execute(
            "SELECT id, day, since, until, created_at, updated_at FROM briefings ORDER BY day DESC LIMIT ?", (LISTED,)
        ).fetchall()
    )
    return {
        "briefings": briefings, "read_id": _read_id(conn), "launch": launch_status(conn), "hour": BRIEFING_HOUR,
        "team": team(conn),
    }


def status(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """For the tab's mark: the newest briefing, and whether the user has opened it."""
    newest = conn.execute("SELECT id, day FROM briefings ORDER BY day DESC LIMIT 1").fetchone()
    if newest is None:
        return None
    return {"id": newest[0], "day": newest[1], "unread": newest[0] > _read_id(conn)}
