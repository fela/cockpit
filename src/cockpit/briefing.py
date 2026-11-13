"""The morning briefing on the user's team (SPEC VIEW-8).

The team is the people the user works with most closely, in the user's own words (kv `team`): the page
asks for them the first time the user opens Cockpit, and the Briefing page changes them. Empty text means
the user named no one, and no briefing is asked for; null means the page has not asked yet.

Once a day, from BRIEFING_HOUR in the user's timezone, while a team is set, Cockpit makes that day's briefing a task
of its own, a high-priority item, and starts a new session on it to write what the team did since the last
briefing's window. The session reads GitHub, Linear, Slack and Cockpit, writes the briefing in plain words and
stores it with `cockpit briefing put`. The whole briefing then shows on the task's card, a step to read
(`ask_to_read`), where the user selects any part of it to comment on; the comments go to that session like any
reply. Task done on the card marks the briefing read on the Briefing tab too (`read_on_finish`); opening it on
the tab marks only the tab (`mark_read`). A launch whose message did not reach a session is tried again
RETRY_MINUTES later, on the same day, unless that day's briefing was stored: a message whose delivery is
uncertain may have reached the session, whose turn has written the briefing by then.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from cockpit import db, error_stops, projects, render, store, sweeper
from cockpit.config import Setup

BRIEFING_HOUR = 7
# The first briefing's window; each later one starts where the one before ended.
BRIEFING_HOURS = 24
RETRY_MINUTES = 60
BODY_LIMIT = 20000
# The page lists this many earlier briefings.
LISTED = 30
TEAM_LIMIT = 2000
# Each briefing has its own item and a step that asks the user to read it.
ITEM_PREFIX = "note:briefing-"
# Its step's body holds the whole briefing, so the card's body limits leave it out (store.WHOLE_BODY_NEED).
READ_NEED = store.WHOLE_BODY_NEED
READ_ASK = "Read the morning briefing on your team"
READ_WHY = "Your daily catch-up on the team. Select any part to comment on it; Task done marks it read."
# The briefing can come in while sweeps are paused (sweeper.PAUSE_AFTER_MINUTES), so it starts with a score of its
# own rather than an unranked step's, and the next sweep judges it like any other.
READ_SCORE = 90
READ_REASONS = ["daily briefing"]
# A new briefing shows this project's logo from the start; a Cockpit without it leaves the briefing to the tidy session
# (projects.py).
PROJECT = "team"
# A link's address or a bare web address (kept, group 1), or a commit hash or session id outside them.
ADDRESS_OR_ID = re.compile(rf"(\]\([^)]*\)|{store.BARE_URL.pattern})|{store.HEX_ID.pattern}", re.IGNORECASE)

log = logging.getLogger(__name__)


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


def prompt(setup: Setup, people: str, day: str, since: datetime, until: datetime, fixed_stops: list[str]) -> str:
    """What the briefing session is asked to do for `day`, covering `since` to `until`, on `people`. An agent may
    set the team, so its words go in defused (store.defused), as all agent text Cockpit forwards does. `fixed_stops`
    are the sessions Cockpit got going again after an API error in the window (error_stops.fixed), which the writer
    may mention."""
    name = setup.name
    stops = "" if not fixed_stops else (
        f"Cockpit's own sessions in that window: these stopped on an API error, and Cockpit's resend of the last \
message got them going again. Mention them in a line if {name} would want to know, or leave them out:\n"
        + "\n".join(f"- {store.defused(line)}" for line in fixed_stops) + "\n\n"
    )
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

{stops}Write the briefing in Markdown, in plain words that {name} follows without opening a link. Keep it short: {name} \
reads all of it in about three minutes, so the whole briefing stays under 500 words. Cockpit's page shows the day and \
the window above it, so begin with the first section:
- `## Executive summary` comes first: at most five bullets of one sentence each and 70 words in all, the things that matter most to \
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

A second put on the same day replaces the first. Cockpit shows the whole briefing on this task's card, where {name} \
can select any part of it and comment. Each comment comes to you as a step handed to you: answer it as the cockpit \
skill says, and when a comment shows the briefing was wrong or unclear, put the corrected briefing again. Then end \
your turn."""


def _retried(conn: sqlite3.Connection, record: dict[str, Any], launched: dict[str, Any]) -> bool:
    """Whether Cockpit asks again for a briefing whose message failed: only while that day's briefing is not
    stored, as a message whose delivery is uncertain may have reached its session after all."""
    written = conn.execute("SELECT 1 FROM briefings WHERE day = ?", (record["day"],)).fetchone() is not None
    return launched["state"] == "failed" and not written


def _day_item(conn: sqlite3.Connection, day: str, summary: str) -> dict[str, Any]:
    """The task for `day`'s briefing, made when Cockpit asks for it, so the session writing it is that task's
    session (SPEC VIEW-8); `summary` says where it stands, with "{day}" for the day in words. It shows the team
    project's logo from the start, unless the item has a project already; a Cockpit without that project leaves it
    to the tidy session (projects.py)."""
    key = f"{ITEM_PREFIX}{day}"
    found = conn.execute("SELECT project FROM items WHERE key = ?", (key,)).fetchone()
    project = projects.resolve(conn, PROJECT) if found is None or not found[0] else None
    words = datetime.fromisoformat(day)
    return store.upsert_item(
        conn,
        {
            "key": key, "title": f"Briefing {words:%a %-d %b}", "priority": "high",
            "summary": summary.format(day=f"{words:%A %-d %B}"),
        }
        | ({"project": project} if project else {}),
        "cockpit",
    )


def launch_once(conn: sqlite3.Connection, setup: Setup, now: datetime) -> int | None:
    """Queue today's briefing request when it is due and a team is set: a new session on the day's task, which
    this makes; returns the message to deliver. The window starts where the last day's ended, so a late morning
    leaves no gap; the first covers BRIEFING_HOURS."""
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
        written = conn.execute("SELECT 1 FROM briefings WHERE day = ?", (record["day"],)).fetchone() is not None
        since = datetime.fromisoformat(record["until"] if written else record["since"])
    else:
        since = now - timedelta(hours=BRIEFING_HOURS)
    text = prompt(setup, people, day, since, now, error_stops.fixed(conn, since, now))
    created = f"{now:%Y-%m-%dT%H:%M:%SZ}"
    with db.transaction(conn):
        item = _day_item(conn, day, "A session is writing the morning briefing for {day}")
        cursor = conn.execute(
            "INSERT INTO messages (author, item_id, new_cwd, new_name, text, state, created_at, as_prompt)"
            " VALUES ('cockpit', ?, ?, ?, ?, 'queued', ?, 0)",
            (item["id"], str(setup.checkouts), item["title"][: store.NEW_SESSION_NAME_CHARS], text, created),
        )
        db.kv_set(
            conn, "briefing_launch",
            {"day": day, "since": f"{since:%Y-%m-%dT%H:%M:%SZ}", "until": f"{now:%Y-%m-%dT%H:%M:%SZ}",
             "message_id": cursor.lastrowid},
        )
    return cursor.lastrowid


def put(conn: sqlite3.Connection, actor: str, day: str, body: str) -> dict[str, Any]:
    """Store the briefing for `day`, the day Cockpit last asked for one; a second put that day replaces it. An
    earlier day's briefing may be corrected, as its task stays open for comments after the next morning's request,
    and keeps its window; a first put for an earlier day is refused, as it was finished too late."""
    text = body.strip()
    if not text:
        raise store.Invalid("the briefing is empty")
    if len(text) > BODY_LIMIT:
        raise store.Invalid(f"the briefing has {len(text)} characters; at most {BODY_LIMIT}")
    now = db.now_iso()
    with db.transaction(conn):
        record = db.kv_get(conn, "briefing_launch")
        if record is None:
            raise store.Invalid("Cockpit has not asked for a briefing yet; it starts a briefing session each morning")
        if record["day"] != day:
            updated = conn.execute(
                "UPDATE briefings SET body = ?, created_by = ?, updated_at = ? WHERE day = ?", (text, actor, now, day)
            )
            if not updated.rowcount:
                raise store.Conflict(
                    f"Cockpit now asks for the briefing for {record['day']}, so the one for {day} is not stored"
                )
        else:
            conn.execute(
                "INSERT INTO briefings (day, since, until, body, created_by, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(day) DO UPDATE SET since = excluded.since,"
                " until = excluded.until, body = excluded.body, created_by = excluded.created_by,"
                " updated_at = excluded.updated_at",
                (day, record["since"], record["until"], text, actor, now, now),
            )
    found = conn.execute("SELECT id FROM briefings WHERE day = ?", (day,)).fetchone()
    stored = get(conn, found[0])
    try:
        ask_to_read(conn, stored)
    except store.Invalid:
        # The briefing is stored and on its page; only its step failed, and the error log says so.
        log.exception("the step to read briefing %s could not be put", stored["id"])
    return stored


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


def step_text(found: dict[str, Any]) -> dict[str, Any]:
    """The read step for briefing `found`: the whole briefing on the card, so the user reads it and selects any part
    to comment on there. A step's body may hold no commit hash or session id, so these leave it; the Briefing tab
    keeps them. It clears `details`, which a put would otherwise keep from a card made before the whole briefing
    showed."""
    return {
        "ask": READ_ASK, "why": READ_WHY, "body": _without_ids(found["body"]), "details": [], "size": "small",
        "progress": 0,
    }


def ask_to_read(conn: sqlite3.Connection, found: dict[str, Any]) -> None:
    """Put briefing `found` in the user's queue as a step to read, on its day's high-priority task (SPEC VIEW-8), so
    it comes in each day with the rest of the user's work. A second put the same day rewrites the open step, also
    one the user handed to the session with a comment, as the session corrects the briefing; once the user pressed
    Task done, the step stays closed. Only the first put sets the starting score, so a rewrite leaves what the
    sweeper decided since; it also asks for one reorder of the queue, so the order is right while sweeps are
    paused (SPEC SWEEP-25)."""
    with db.transaction(conn):
        closed = conn.execute(
            "SELECT 1 FROM items WHERE key = ? AND (stage = 'done' OR archived)", (f"{ITEM_PREFIX}{found['day']}",)
        ).fetchone()
        if closed:
            return
        item = _day_item(conn, found["day"], "The morning briefing for {day} is ready to read")
        first_put = conn.execute(
            "SELECT 1 FROM steps WHERE item_id = ? AND need = ?", (item["id"], READ_NEED)
        ).fetchone() is None
        score = {"score": READ_SCORE, "reasons": READ_REASONS} if first_put else {}
        store.upsert_step(conn, {"item_id": item["id"], "need": READ_NEED} | step_text(found) | score, "cockpit")
        if first_put:
            sweeper.reorder_soon(conn, "the morning briefing just came in")


def read_on_finish(conn: sqlite3.Connection, item: dict[str, Any]) -> None:
    """The user pressed Task done on a briefing's item: that briefing counts as read on its page too."""
    if not item["key"].startswith(ITEM_PREFIX) or item["stage"] != "done":
        return
    found = conn.execute("SELECT id FROM briefings WHERE day = ?", (item["key"].removeprefix(ITEM_PREFIX),)).fetchone()
    if found is not None:
        mark_read(conn, found[0])


def get(conn: sqlite3.Connection, briefing_id: int) -> dict[str, Any]:
    """The briefing with its HTML."""
    found = db.row(conn.execute("SELECT * FROM briefings WHERE id = ?", (briefing_id,)).fetchone())
    if found is None:
        raise store.NotFound(f"briefing {briefing_id} does not exist")
    return found | {"body_html": render.markdown_html(found["body"], store.mention_refs(conn))}


def _read_id(conn: sqlite3.Connection) -> int:
    return db.kv_get(conn, "briefing_read") or 0


def mark_read(conn: sqlite3.Connection, briefing_id: int) -> None:
    """The user opened this briefing on the Briefing tab, or pressed Task done on its card; it and every older one
    count as read on the tab. Its card stays as it is: the user reads and comments there, and Task done closes it."""
    get(conn, briefing_id)
    with db.transaction(conn):
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
