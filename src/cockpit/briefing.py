"""The morning briefing on the user's team (SPEC VIEW-8).

The team is the people the user works with most closely, in the user's own words (kv `team`): the page
asks for them the first time the user opens Cockpit, and the Briefing page changes them. Empty text means
the user named no one, and no briefing is asked for; null means the page has not asked yet.

Once a day, from the briefing hour in the user's timezone (settings.py, BRIEFING_HOUR by default), while a team is set, Cockpit makes that day's briefing a task
of its own, a high-priority item, and starts a new session on it to write what the team did since the last
briefing's window. The session reads GitHub, Linear, Slack and Cockpit, writes the briefing in plain words and
stores it with `cockpit briefing put`. The whole briefing then shows on the task's card, a step to read
(`ask_to_read`), where the user selects any part of it to comment on; the comments go to that session like any
reply. Task done on the card marks the briefing read on the Briefing tab too (`read_on_finish`); opening it on
the tab marks only the tab (`mark_read`). A launch whose message did not reach a session is tried again
RETRY_MINUTES later, on the same day, unless that day's briefing was stored: a message whose delivery is
uncertain may have reached the session, whose turn has written the briefing by then.

The weekly digest (SPEC VIEW-36) comes the same way on the digest's day from its hour (settings.py; Sunday 08:00 by
default), beside that day's morning briefing: a task of its own and a new session, asked to pick out what matters
most from the week's morning briefings, which Cockpit hands it, so the user catches up and readies for the week
ahead. It waits for that day's briefing for DIGEST_WAIT_HOURS. Digests are stored with the briefings, of kind
"weekly", and show on the Briefing page.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from cockpit import db, error_stops, projects, render, settings, store, sweeper
from cockpit.config import Setup

# The briefing's hour by default; the user sets it on the Settings page (settings.py).
BRIEFING_HOUR = settings.DEFAULTS["briefing_hour"]
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
# The weekly digest (SPEC VIEW-36), on the day and from the hour the Settings page sets (settings.py), after that
# day's briefing, on the morning briefings of the DIGEST_DAYS days to that day. Without that day's briefing, it
# waits DIGEST_WAIT_HOURS and then goes on the days it has.
DIGEST_DAYS = 7
DIGEST_WAIT_HOURS = 4


@dataclass(frozen=True)
class Kind:
    """What sets a morning briefing apart from a weekly digest: the kv record of its last request, its task's key and
    title, and the words of its read step."""

    name: str
    words: str
    launch_key: str
    read_key: str
    item_prefix: str
    title: str
    read_ask: str
    read_why: str
    read_reasons: list[str]


DAILY = Kind(
    "daily", "morning briefing", "briefing_launch", "briefing_read", ITEM_PREFIX, "Briefing", READ_ASK, READ_WHY,
    READ_REASONS,
)
WEEKLY = Kind(
    "weekly", "weekly digest", "digest_launch", "digest_read", "note:digest-", "Weekly digest",
    "Read the weekly digest on your team",
    "Your catch-up on the week, ahead of the next. Select any part to comment on it; Task done marks it read.",
    ["weekly digest"],
)
KINDS = {kind.name: kind for kind in (DAILY, WEEKLY)}
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


def _written(conn: sqlite3.Connection, kind: Kind, day: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM briefings WHERE kind = ? AND day = ?", (kind.name, day)
    ).fetchone() is not None


def _retried(conn: sqlite3.Connection, record: dict[str, Any], launched: dict[str, Any], kind: Kind = DAILY) -> bool:
    """Whether Cockpit asks again for a briefing whose message failed: only while that day's briefing is not
    stored, as a message whose delivery is uncertain may have reached its session after all."""
    return launched["state"] == "failed" and not _written(conn, kind, record["day"])


def _day_item(conn: sqlite3.Connection, day: str, summary: str, kind: Kind = DAILY) -> dict[str, Any]:
    """The task for `day`'s briefing of `kind`, made when Cockpit asks for it, so the session writing it is that
    task's session (SPEC VIEW-8); `summary` says where it stands, with "{day}" for the day in words. It shows the
    team project's logo from the start, unless the item has a project already; a Cockpit without that project leaves
    it to the tidy session (projects.py)."""
    key = f"{kind.item_prefix}{day}"
    found = conn.execute("SELECT project FROM items WHERE key = ?", (key,)).fetchone()
    project = projects.resolve(conn, PROJECT) if found is None or not found[0] else None
    words = datetime.fromisoformat(day)
    return store.upsert_item(
        conn,
        {
            "key": key, "title": f"{kind.title} {words:%a %-d %b}", "priority": "high",
            "summary": summary.format(day=f"{words:%A %-d %B}"),
        }
        | ({"project": project} if project else {}),
        "cockpit",
    )


def _queue(
    conn: sqlite3.Connection, setup: Setup, kind: Kind, day: str, window: tuple[str, str], text: str, now: datetime
) -> int:
    """Make `day`'s task of `kind` and queue `text` to a new session on it, recording the request and its `window`
    (since, until); returns the message."""
    created = f"{now:%Y-%m-%dT%H:%M:%SZ}"
    with db.transaction(conn):
        item = _day_item(conn, day, f"A session is writing the {kind.words} for {{day}}", kind)
        cursor = conn.execute(
            "INSERT INTO messages (author, item_id, new_cwd, new_name, text, state, created_at, as_prompt)"
            " VALUES ('cockpit', ?, ?, ?, ?, 'queued', ?, 0)",
            (item["id"], str(setup.checkouts), item["title"][: store.NEW_SESSION_NAME_CHARS], text, created),
        )
        db.kv_set(
            conn, kind.launch_key,
            {"day": day, "since": window[0], "until": window[1], "message_id": cursor.lastrowid},
        )
    return cursor.lastrowid


def launch_once(conn: sqlite3.Connection, setup: Setup, now: datetime) -> int | None:
    """Queue today's briefing request when it is due and a team is set: a new session on the day's task, which
    this makes; returns the message to deliver. The window starts where the last day's ended, so a late morning
    leaves no gap; the first covers BRIEFING_HOURS."""
    people = team(conn)
    if not people:
        return None
    local = now.astimezone(setup.timezone)
    day = local.date().isoformat()
    record = db.kv_get(conn, DAILY.launch_key)
    if record is not None and record["day"] == day:
        launched = store.get_message(conn, record["message_id"])
        waited = now - datetime.fromisoformat(launched["created_at"])
        if not _retried(conn, record, launched) or waited < timedelta(minutes=RETRY_MINUTES):
            return None
        since = datetime.fromisoformat(record["since"])
    elif local.hour < settings.schedule(conn)["briefing_hour"]:
        return None
    elif record is not None:
        since = datetime.fromisoformat(record["until"] if _written(conn, DAILY, record["day"]) else record["since"])
    else:
        since = now - timedelta(hours=BRIEFING_HOURS)
    text = prompt(setup, people, day, since, now, error_stops.fixed(conn, since, now))
    return _queue(conn, setup, DAILY, day, (f"{since:%Y-%m-%dT%H:%M:%SZ}", f"{now:%Y-%m-%dT%H:%M:%SZ}"), text, now)


def digest_prompt(setup: Setup, day: str, week: list[dict[str, Any]]) -> str:
    """What the digest session is asked to do for `day`, from that week's morning briefings `week`, oldest
    first (SPEC VIEW-36). The briefings are agent text, so they go in defused (store.defused)."""
    name = setup.name
    day_words = datetime.fromisoformat(day).strftime("%A %-d %B")
    since = datetime.fromisoformat(week[0]["since"])
    until = datetime.fromisoformat(week[-1]["until"])
    briefings = "\n\n".join(
        f"<briefing day=\"{datetime.fromisoformat(each['day']):%A %-d %B}\">\n{store.defused(each['body'])}\n</briefing>"
        for each in week
    )
    weekday = datetime.fromisoformat(day).strftime("%A")
    return f"""Write {name}'s weekly digest for the week to {day_words}. {name} reads it on {weekday} \
morning to catch up on the week at a high level and get ready for the week ahead. It draws on the week's morning \
briefings, which covered {_local_words(since, setup)} to {_local_words(until, setup)}, {name}'s time \
({setup.timezone.key}):

{briefings}

This is not a summary of each briefing. Pick out what matters most across the week: threads that moved over several \
days, decisions made or still open, what waits on {name}, changes to {name}'s own work, and what comes up in the \
week ahead. Leave out what was settled and no longer matters, and the small news of a single day. `cockpit get` and \
`cockpit goals` show {name}'s goals and open work, which tell you what matters to {name}. Where a briefing leaves an \
outcome open, such as whether a PR merged later, you may check it on GitHub, Linear or Slack; do not gather the \
week's activity again.

Write the digest in Markdown, in plain words that {name} follows without opening a link. Keep it short: {name} reads \
all of it in about four minutes, so the whole digest stays under 700 words. Cockpit's page shows the week above it, \
so begin with the first section:
- `## Executive summary` comes first and is at least half of the digest: the handful of things that matter most from \
the week and for the week ahead, each with enough context to follow it cold. End it with what {name} should watch \
for or decide in the coming week. {name} can stop reading after it and know the week.
- Divide the rest across a short section per person: two or three bullets on what they moved forward this week and \
why it matters. One line is enough for a quiet week.
- Leave out what the summary already says unless the detail adds something {name} needs.
- Explain the context of each point the first time it comes up, and name each PR or ticket by what it is, with its \
link after the name, as the briefings do.

This is reading only: post, comment or change nothing on GitHub, Linear or Slack. The one write is storing the \
digest, with the Markdown on stdin:

cockpit briefing put --weekly --day {day} --as briefing <<'EOF'
<the digest>
EOF

A second put on the same day replaces the first. Cockpit shows the whole digest on this task's card, where {name} \
can select any part of it and comment. Each comment comes to you as a step handed to you: answer it as the cockpit \
skill says, and when a comment shows the digest was wrong or unclear, put the corrected digest again. Then end \
your turn."""


def launch_digest_once(conn: sqlite3.Connection, setup: Setup, now: datetime) -> int | None:
    """Queue the weekly digest request when it is due (SPEC VIEW-36): on the digest's day from its hour (settings.py),
    while a team is set, once that day's morning briefing is stored or DIGEST_WAIT_HOURS have passed, on the morning
    briefings of the DIGEST_DAYS days to that day; returns the message to deliver. A week without a briefing gets no
    digest, and neither does a schedule without a digest day. A failed request is tried again as the briefing's
    is."""
    if not team(conn):
        return None
    local = now.astimezone(setup.timezone)
    day = local.date().isoformat()
    record = db.kv_get(conn, WEEKLY.launch_key)
    if record is not None and record["day"] == day:
        launched = store.get_message(conn, record["message_id"])
        waited = now - datetime.fromisoformat(launched["created_at"])
        if not _retried(conn, record, launched, WEEKLY) or waited < timedelta(minutes=RETRY_MINUTES):
            return None
    else:
        when = settings.schedule(conn)
        if (
            local.weekday() != when["digest_day"]
            or local.hour < when["digest_hour"]
            or (not _written(conn, DAILY, day) and local.hour < when["digest_hour"] + DIGEST_WAIT_HOURS)
        ):
            return None
    first = (local.date() - timedelta(days=DIGEST_DAYS - 1)).isoformat()
    week = db.rows(
        conn.execute(
            "SELECT day, since, until, body FROM briefings WHERE kind = 'daily' AND day BETWEEN ? AND ? ORDER BY day",
            (first, day),
        ).fetchall()
    )
    if not week:
        return None
    return _queue(conn, setup, WEEKLY, day, (week[0]["since"], week[-1]["until"]), digest_prompt(setup, day, week), now)


def put(conn: sqlite3.Connection, actor: str, day: str, body: str, kind: str = "daily") -> dict[str, Any]:
    """Store the briefing of `kind` ("daily", or "weekly" for the digest) for `day`, the day Cockpit last asked for
    one of that kind; a second put that day replaces it. An earlier day's briefing may be corrected, as its task
    stays open for comments after the next request, and keeps its window; a first put for an earlier day is refused,
    as it was finished too late."""
    asked = KINDS.get(kind)
    if asked is None:
        raise store.Invalid(f"a briefing is daily or weekly, not {kind!r}")
    text = body.strip()
    what = "briefing" if asked is DAILY else asked.words
    if not text:
        raise store.Invalid(f"the {what} is empty")
    if len(text) > BODY_LIMIT:
        raise store.Invalid(f"the {what} has {len(text)} characters; at most {BODY_LIMIT}")
    now = db.now_iso()
    with db.transaction(conn):
        record = db.kv_get(conn, asked.launch_key)
        if record is None:
            when = "each morning" if asked is DAILY else "once a week"
            raise store.Invalid(f"Cockpit has not asked for a {what} yet; it starts a {what} session {when}")
        if record["day"] != day:
            updated = conn.execute(
                "UPDATE briefings SET body = ?, created_by = ?, updated_at = ? WHERE kind = ? AND day = ?",
                (text, actor, now, kind, day),
            )
            if not updated.rowcount:
                raise store.Conflict(
                    f"Cockpit now asks for the {what} for {record['day']}, so the one for {day} is not stored"
                )
        else:
            conn.execute(
                "INSERT INTO briefings (kind, day, since, until, body, created_by, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(kind, day) DO UPDATE SET since = excluded.since,"
                " until = excluded.until, body = excluded.body, created_by = excluded.created_by,"
                " updated_at = excluded.updated_at",
                (kind, day, record["since"], record["until"], text, actor, now, now),
            )
    found = conn.execute("SELECT id FROM briefings WHERE kind = ? AND day = ?", (kind, day)).fetchone()
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
    kind = KINDS[found.get("kind", "daily")]
    return {
        "ask": kind.read_ask, "why": kind.read_why, "body": _without_ids(found["body"]), "details": [],
        "size": "small", "progress": 0,
    }


def ask_to_read(conn: sqlite3.Connection, found: dict[str, Any]) -> None:
    """Put briefing `found` in the user's queue as a step to read, on its day's high-priority task (SPEC VIEW-8), so
    it comes in each day with the rest of the user's work. A second put the same day rewrites the open step, also
    one the user handed to the session with a comment, as the session corrects the briefing; once the user pressed
    Task done, the step stays closed. Only the first put sets the starting score, so a rewrite leaves what the
    sweeper decided since; it also asks for one reorder of the queue, so the order is right while sweeps are
    paused (SPEC SWEEP-25)."""
    kind = KINDS[found["kind"]]
    with db.transaction(conn):
        closed = conn.execute(
            "SELECT 1 FROM items WHERE key = ? AND (stage = 'done' OR archived)", (f"{kind.item_prefix}{found['day']}",)
        ).fetchone()
        if closed:
            return
        item = _day_item(conn, found["day"], f"The {kind.words} for {{day}} is ready to read", kind)
        first_put = conn.execute(
            "SELECT 1 FROM steps WHERE item_id = ? AND need = ?", (item["id"], READ_NEED)
        ).fetchone() is None
        score = {"score": READ_SCORE, "reasons": kind.read_reasons} if first_put else {}
        store.upsert_step(conn, {"item_id": item["id"], "need": READ_NEED} | step_text(found) | score, "cockpit")
        if first_put:
            sweeper.reorder_soon(conn, f"the {kind.words} just came in")


def read_on_finish(conn: sqlite3.Connection, item: dict[str, Any]) -> None:
    """The user pressed Task done on a briefing's or digest's item: it counts as read on its page too."""
    if item["stage"] != "done":
        return
    for kind in KINDS.values():
        if item["key"].startswith(kind.item_prefix):
            day = item["key"].removeprefix(kind.item_prefix)
            found = conn.execute("SELECT id FROM briefings WHERE kind = ? AND day = ?", (kind.name, day)).fetchone()
            if found is not None:
                mark_read(conn, found[0])
            return


def _read_ids(conn: sqlite3.Connection) -> dict[str, int]:
    """The newest briefing of each kind the user has read; every older one of that kind counts as read too."""
    return {name: db.kv_get(conn, kind.read_key) or 0 for name, kind in KINDS.items()}


def _with_read(found: dict[str, Any], read: dict[str, int]) -> dict[str, Any]:
    return found | {"unread": found["id"] > read[found["kind"]]}


def get(conn: sqlite3.Connection, briefing_id: int) -> dict[str, Any]:
    """The briefing with its HTML, and whether the user has opened it."""
    found = db.row(conn.execute("SELECT * FROM briefings WHERE id = ?", (briefing_id,)).fetchone())
    if found is None:
        raise store.NotFound(f"briefing {briefing_id} does not exist")
    html = render.markdown_html(found["body"], store.mention_refs(conn))
    return _with_read(found, _read_ids(conn)) | {"body_html": html}


def mark_read(conn: sqlite3.Connection, briefing_id: int) -> None:
    """The user opened this briefing on the Briefing tab, or pressed Task done on its card; it and every older one of
    its kind count as read on the tab. Its card stays as it is: the user reads and comments there, and Task done
    closes it."""
    kind = KINDS[get(conn, briefing_id)["kind"]]
    with db.transaction(conn):
        db.kv_set(conn, kind.read_key, max(db.kv_get(conn, kind.read_key) or 0, briefing_id))


def launch_status(conn: sqlite3.Connection, kind: Kind = DAILY) -> dict[str, Any] | None:
    """The last request for a briefing of `kind`: its day and window, and whether its message reached a session."""
    record = db.kv_get(conn, kind.launch_key)
    if record is None:
        return None
    launched = store.get_message(conn, record["message_id"])
    retry_at = None
    if _retried(conn, record, launched, kind):
        retry = datetime.fromisoformat(launched["created_at"]) + timedelta(minutes=RETRY_MINUTES)
        retry_at = f"{retry:%Y-%m-%dT%H:%M:%SZ}"
    return {
        "day": record["day"], "since": record["since"], "until": record["until"], "asked_at": launched["created_at"],
        "state": launched["state"], "detail": launched["detail"], "session_id": launched["session_id"],
        "written": _written(conn, kind, record["day"]), "retry_at": retry_at,
    }


def listing(conn: sqlite3.Connection) -> dict[str, Any]:
    """The page's data: the newest briefings and digests without their text, newest first, each saying whether the
    user has opened it; the newest morning briefing read, the last request of each kind, and the team."""
    read = _read_ids(conn)
    briefings = db.rows(
        conn.execute(
            "SELECT id, kind, day, since, until, created_at, updated_at FROM briefings ORDER BY day DESC, id DESC"
            " LIMIT ?",
            (LISTED,),
        ).fetchall()
    )
    return {
        "briefings": [_with_read(each, read) for each in briefings], "read_id": read["daily"],
        "launch": launch_status(conn),
        "digest": launch_status(conn, WEEKLY), "hour": settings.schedule(conn)["briefing_hour"], "team": team(conn),
    }


def status(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """For the tab's mark: the newest briefing, and whether the newest of either kind is one the user has not
    opened."""
    read = _read_ids(conn)
    newest = db.rows(
        conn.execute(
            "SELECT id, kind, day FROM briefings WHERE id IN (SELECT MAX(id) FROM briefings GROUP BY kind)"
            " ORDER BY day DESC, id DESC"
        ).fetchall()
    )
    if not newest:
        return None
    return {
        "id": newest[0]["id"], "day": newest[0]["day"],
        "unread": any(each["id"] > read[each["kind"]] for each in newest),
    }
