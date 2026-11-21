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

The weekly digest (SPEC VIEW-37) is on when the setup's features name DIGEST_FEATURE. On Sunday from DIGEST_HOUR,
once that day's briefing is stored or DIGEST_WAIT_HOURS have passed, Cockpit makes the digest a task of its own and
starts a new session on it, handed the morning briefings of the week to that Sunday, to pick out what matters most. It
is stored with the briefings, of kind 'weekly', and is read and commented on like a briefing. The week stays the
current one until the next Sunday's DIGEST_HOUR, so a digest Cockpit could not ask for on Sunday is asked for later
that week, and a failed request is asked again until then.

With the setup's features naming SETTINGS_FEATURE, the Settings page (SPEC CONTROL-35) changes the briefing's hour and
the digest's day and hour, or turns the digest off (`schedule`); the times above are then the user's.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
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
DIGEST_FEATURE = "weekly_digest"
SUNDAY = 6
DIGEST_HOUR = 8
# How long after the digest's hour it waits for that day's briefing.
DIGEST_WAIT_HOURS = 4
SETTINGS_FEATURE = "settings_page"
# What the Settings page changes, and its values when the user has not; the kv record `schedule` holds only the ones
# the user changed. A digest day of None is no digest; days count from Monday, 0.
SCHEDULE = {"briefing_hour": BRIEFING_HOUR, "digest_day": SUNDAY, "digest_hour": DIGEST_HOUR}


@dataclass(frozen=True)
class Kind:
    """The morning briefing or the weekly digest: its rows' `kind`, the kv records of its last request and of the
    newest one the user read, its task's key and title, what errors call it and when it comes, and its read step's
    words."""

    name: str
    launch_key: str
    read_key: str
    item_prefix: str
    title: str
    what: str
    when: str
    words: str
    ask: str
    why: str
    reasons: list[str]


DAILY = Kind(
    "daily", "briefing_launch", "briefing_read", ITEM_PREFIX, "Briefing", "briefing", "each morning",
    "morning briefing", READ_ASK, READ_WHY, READ_REASONS,
)
WEEKLY = Kind(
    "weekly", "digest_launch", "digest_read", "note:digest-", "Weekly digest", "digest", "once a week",
    "weekly digest", "Read the weekly digest on your team",
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


def settings_on(setup: Setup) -> bool:
    """Whether this install has the Settings page (SPEC CONTROL-35)."""
    return SETTINGS_FEATURE in setup.features


def schedule(conn: sqlite3.Connection, setup: Setup) -> dict[str, int | None]:
    """When the briefing and the digest come: SCHEDULE with the user's changes, while the Settings page is on."""
    if not settings_on(setup):
        return dict(SCHEDULE)
    return SCHEDULE | (db.kv_get(conn, "schedule") or {})


def put_schedule(
    conn: sqlite3.Connection, briefing_hour: int, digest_day: int | None, digest_hour: int
) -> dict[str, int | None]:
    """Set the whole schedule; the kv record keeps only the values that differ from SCHEDULE."""
    wanted = {"briefing_hour": briefing_hour, "digest_day": digest_day, "digest_hour": digest_hour}
    with db.transaction(conn):
        db.kv_set(conn, "schedule", {name: value for name, value in wanted.items() if value != SCHEDULE[name]})
    return wanted


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


def _briefing_retry(conn: sqlite3.Connection, record: dict[str, Any], launched: dict[str, Any]) -> datetime | None:
    """When Cockpit asks again for a briefing whose message failed: RETRY_MINUTES later, only while that day's
    briefing is not stored, as a message whose delivery is uncertain may have reached its session after all."""
    if launched["state"] != "failed" or _written(conn, DAILY, record["day"]):
        return None
    return datetime.fromisoformat(launched["created_at"]) + timedelta(minutes=RETRY_MINUTES)


def _day_item(conn: sqlite3.Connection, kind: Kind, day: str, summary: str) -> dict[str, Any]:
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
    conn: sqlite3.Connection, setup: Setup, kind: Kind, day: str, since: str, until: str, text: str, now: datetime
) -> int:
    """Make `day`'s task of `kind` and queue `text` to a new session on it, recording the request and its window;
    returns the message."""
    with db.transaction(conn):
        item = _day_item(conn, kind, day, f"A session is writing the {kind.words} for {{day}}")
        cursor = conn.execute(
            "INSERT INTO messages (author, item_id, new_cwd, new_name, text, state, created_at, as_prompt)"
            " VALUES ('cockpit', ?, ?, ?, ?, 'queued', ?, 0)",
            (item["id"], str(setup.checkouts), item["title"][: store.NEW_SESSION_NAME_CHARS], text,
             f"{now:%Y-%m-%dT%H:%M:%SZ}"),
        )
        db.kv_set(
            conn, kind.launch_key, {"day": day, "since": since, "until": until, "message_id": cursor.lastrowid}
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
        retry = _briefing_retry(conn, record, store.get_message(conn, record["message_id"]))
        if retry is None or now < retry:
            return None
        since = datetime.fromisoformat(record["since"])
    elif local.hour < schedule(conn, setup)["briefing_hour"]:
        return None
    elif record is not None:
        since = datetime.fromisoformat(record["until"] if _written(conn, DAILY, record["day"]) else record["since"])
    else:
        since = now - timedelta(hours=BRIEFING_HOURS)
    text = prompt(setup, people, day, since, now, error_stops.fixed(conn, since, now))
    return _queue(conn, setup, DAILY, day, f"{since:%Y-%m-%dT%H:%M:%SZ}", f"{now:%Y-%m-%dT%H:%M:%SZ}", text, now)


def digest_on(setup: Setup) -> bool:
    """Whether this install has the weekly digest (SPEC VIEW-37)."""
    return DIGEST_FEATURE in setup.features


def _digest_week(conn: sqlite3.Connection, setup: Setup, moment: datetime) -> date | None:
    """The day of the digest whose week is current at `moment`: the last digest day whose digest hour has come, in
    the user's time zone; None with no digest. The scheduler, its retry and the retry's limit all ask this, so they
    agree on where each week ends."""
    when = schedule(conn, setup)
    if when["digest_day"] is None:
        return None
    shifted = (moment.astimezone(setup.timezone) - timedelta(hours=when["digest_hour"])).date()
    return shifted - timedelta(days=(shifted.weekday() - when["digest_day"]) % 7)


def _digest_retry(
    conn: sqlite3.Connection, setup: Setup, record: dict[str, Any], launched: dict[str, Any], now: datetime
) -> datetime | None:
    """When Cockpit asks again for a digest whose message failed: RETRY_MINUTES later, while a team is set, the
    request's week is the current one at `now` and the digest is not stored, as for a briefing, and only when that
    comes before the next week's digest. A retry time the schedule now places before the week's start, after the
    user moved the digest later or changed the time zone, still counts: the retry runs once the week is current.
    A "No digest" saved between the two schedule reads means no retry. The scheduler and the page both ask this, so the page promises only the retry the scheduler runs."""
    current = _digest_week(conn, setup, now)
    if launched["state"] != "failed" or not team(conn) or current is None or record["day"] != current.isoformat():
        return None
    if _written(conn, WEEKLY, record["day"]):
        return None
    retry = datetime.fromisoformat(launched["created_at"]) + timedelta(minutes=RETRY_MINUTES)
    week = _digest_week(conn, setup, retry)
    return retry if week is not None and week <= current else None


def _quoted(body: str) -> str:
    """A briefing's text inside the digest prompt: agent text, so defused (store.defused), and unable to close its
    own tag early."""
    return store.defused(body).replace("</briefing", "<\\/briefing")


def digest_prompt(setup: Setup, day: str, week: list[dict[str, Any]]) -> str:
    """What the digest session is asked to do for digest day `day`, from that week's morning briefings `week`,
    oldest first (SPEC VIEW-37)."""
    name = setup.name
    end = date.fromisoformat(day)
    day_words = end.strftime("%A %-d %B")
    read_days = f"{end:%A} or {end + timedelta(days=1):%A}"
    since = datetime.fromisoformat(week[0]["since"])
    until = datetime.fromisoformat(week[-1]["until"])
    briefings = "\n\n".join(
        f"<briefing day=\"{datetime.fromisoformat(each['day']):%A %-d %B}\">\n{_quoted(each['body'])}\n</briefing>"
        for each in week
    )
    return f"""Write {name}'s weekly digest for the week to {day_words}. {name} reads it on {read_days} \
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

A second put replaces the first. Cockpit shows the whole digest on this task's card, where {name} can select any \
part of it and comment. Each comment comes to you as a step handed to you: answer it as the cockpit skill says, and \
when a comment shows the digest was wrong or unclear, put the corrected digest again. Then end your turn."""


def launch_digest_once(conn: sqlite3.Connection, setup: Setup, now: datetime) -> int | None:
    """Queue the current week's digest request when the digest is on, a team is set and the request is due (SPEC
    VIEW-37); returns the message to deliver. It is due from the digest day's hour once that day's briefing is
    stored or DIGEST_WAIT_HOURS have passed, and asked again as `_digest_retry` says; a week without a briefing gets
    no digest. The schedule's reads, the check and the queuing are one transaction, so a digest stored meanwhile is
    never asked for again and a save of No digest meanwhile stops the request."""
    if not digest_on(setup) or not team(conn):
        return None
    with db.transaction(conn):
        end = _digest_week(conn, setup, now)
        if end is None:
            return None
        day = end.isoformat()
        due = datetime.combine(end, time(schedule(conn, setup)["digest_hour"]), setup.timezone)
        waited = now >= due + timedelta(hours=DIGEST_WAIT_HOURS)
        record = db.kv_get(conn, WEEKLY.launch_key)
        if record is not None and record["day"] > day:
            return None  # a poll that read the clock before the last request's
        if record is not None and record["day"] == day:
            retry = _digest_retry(conn, setup, record, store.get_message(conn, record["message_id"]), now)
            if retry is None or now < retry:
                return None
            # The new request replaces the failed one, which is set aside so a retry of it cannot start a second
            # session on the digest.
            conn.execute(
                "UPDATE messages SET dismissed_at = ?, dismissed_by = 'cockpit', dismiss_reason = ? WHERE id = ?",
                (f"{now:%Y-%m-%dT%H:%M:%SZ}", "Cockpit asked for the weekly digest again", record["message_id"]),
            )
        elif not waited and not _written(conn, DAILY, day):
            return None
        week = db.rows(
            conn.execute(
                "SELECT day, since, until, body FROM briefings WHERE kind = 'daily' AND day BETWEEN ? AND ?"
                " ORDER BY day",
                ((end - timedelta(days=6)).isoformat(), day),
            ).fetchall()
        )
        if not week:
            return None
        text = digest_prompt(setup, day, week)
        return _queue(conn, setup, WEEKLY, day, week[0]["since"], week[-1]["until"], text, now)


def put(conn: sqlite3.Connection, actor: str, day: str, body: str) -> dict[str, Any]:
    """Store the briefing for `day`, the day Cockpit last asked for one; a second put that day replaces it. An
    earlier day's briefing may be corrected, as its task stays open for comments after the next morning's request,
    and keeps its window; a first put for an earlier day is refused, as it was finished too late."""
    return _put(conn, DAILY, actor, day, body)


def put_digest(conn: sqlite3.Connection, setup: Setup, actor: str, day: str, body: str) -> dict[str, Any]:
    """Store the weekly digest for digest day `day`, as `put` stores a briefing; refused while the digest is off."""
    if not digest_on(setup):
        raise store.Invalid(f"the weekly digest is off; the setup's features turn it on with {DIGEST_FEATURE!r}")
    return _put(conn, WEEKLY, actor, day, body)


def _put(conn: sqlite3.Connection, kind: Kind, actor: str, day: str, body: str) -> dict[str, Any]:
    text = body.strip()
    if not text:
        raise store.Invalid(f"the {kind.what} is empty")
    if len(text) > BODY_LIMIT:
        raise store.Invalid(f"the {kind.what} has {len(text)} characters; at most {BODY_LIMIT}")
    now = db.now_iso()
    with db.transaction(conn):
        record = db.kv_get(conn, kind.launch_key)
        if record is None:
            raise store.Invalid(
                f"Cockpit has not asked for a {kind.what} yet; it starts a {kind.what} session {kind.when}"
            )
        if record["day"] != day:
            updated = conn.execute(
                "UPDATE briefings SET body = ?, created_by = ?, updated_at = ? WHERE kind = ? AND day = ?",
                (text, actor, now, kind.name, day),
            )
            if not updated.rowcount:
                raise store.Conflict(
                    f"Cockpit now asks for the {kind.what} for {record['day']}, so the one for {day} is not stored"
                )
        else:
            conn.execute(
                "INSERT INTO briefings (kind, day, since, until, body, created_by, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(kind, day) DO UPDATE SET since = excluded.since,"
                " until = excluded.until, body = excluded.body, created_by = excluded.created_by,"
                " updated_at = excluded.updated_at",
                (kind.name, day, record["since"], record["until"], text, actor, now, now),
            )
    found = conn.execute("SELECT id FROM briefings WHERE kind = ? AND day = ?", (kind.name, day)).fetchone()
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
    """The read step's text for briefing `found`: the whole briefing on the card, so the user reads it and selects
    any part to comment on there. A step's body may hold no commit hash or session id, so these leave it; the
    Briefing tab keeps them. It clears `details`, which a put would otherwise keep from a card made before the whole
    briefing showed."""
    return {"body": _without_ids(found["body"]), "details": [], "size": "small", "progress": 0}


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
        item = _day_item(conn, kind, found["day"], f"The {kind.words} for {{day}} is ready to read")
        first_put = conn.execute(
            "SELECT 1 FROM steps WHERE item_id = ? AND need = ?", (item["id"], READ_NEED)
        ).fetchone() is None
        score = {"score": READ_SCORE, "reasons": kind.reasons} if first_put else {}
        step = {"item_id": item["id"], "need": READ_NEED, "ask": kind.ask, "why": kind.why} | step_text(found)
        store.upsert_step(conn, step | score, "cockpit")
        if first_put:
            sweeper.reorder_soon(conn, f"the {kind.words} just came in")


def read_on_finish(conn: sqlite3.Connection, item: dict[str, Any]) -> None:
    """The user pressed Task done on a briefing's item: that briefing counts as read on its page too."""
    if item["stage"] != "done":
        return
    for kind in KINDS.values():
        if item["key"].startswith(kind.item_prefix):
            day = item["key"].removeprefix(kind.item_prefix)
            found = conn.execute("SELECT id FROM briefings WHERE kind = ? AND day = ?", (kind.name, day)).fetchone()
            if found is not None:
                mark_read(conn, found[0])


def get(conn: sqlite3.Connection, briefing_id: int) -> dict[str, Any]:
    """The briefing with its HTML."""
    found = db.row(conn.execute("SELECT * FROM briefings WHERE id = ?", (briefing_id,)).fetchone())
    if found is None:
        raise store.NotFound(f"briefing {briefing_id} does not exist")
    return found | {"body_html": render.markdown_html(found["body"], store.mention_refs(conn))}


def _read_id(conn: sqlite3.Connection, kind: Kind) -> int:
    return db.kv_get(conn, kind.read_key) or 0


def mark_read(conn: sqlite3.Connection, briefing_id: int) -> None:
    """The user opened this briefing on the Briefing tab, or pressed Task done on its card; it and every older one of
    its kind count as read on the tab. Its card stays as it is: the user reads and comments there, and Task done
    closes it."""
    kind = KINDS[get(conn, briefing_id)["kind"]]
    with db.transaction(conn):
        db.kv_set(conn, kind.read_key, max(_read_id(conn, kind), briefing_id))


def launch_status(conn: sqlite3.Connection, setup: Setup, kind: Kind, now: datetime) -> dict[str, Any] | None:
    """The last request for a briefing of `kind`: its day and window, whether its message reached a session, and
    when Cockpit asks again, if it will at `now`."""
    record = db.kv_get(conn, kind.launch_key)
    if record is None:
        return None
    launched = store.get_message(conn, record["message_id"])
    if kind is WEEKLY:
        retry = _digest_retry(conn, setup, record, launched, now)
    else:
        retry = _briefing_retry(conn, record, launched)
    return {
        "day": record["day"], "since": record["since"], "until": record["until"], "asked_at": launched["created_at"],
        "state": launched["state"], "detail": launched["detail"], "session_id": launched["session_id"],
        "written": _written(conn, kind, record["day"]),
        "retry_at": None if retry is None else f"{retry:%Y-%m-%dT%H:%M:%SZ}",
    }


def listing(conn: sqlite3.Connection, setup: Setup, now: datetime) -> dict[str, Any]:
    """The page's data: the newest briefings without their text, which one is read, the last request and the
    team; with the digest on, the digests among them, the newest digest read and the last digest request."""
    kinds = [DAILY.name, WEEKLY.name] if digest_on(setup) else [DAILY.name]
    briefings = db.rows(
        conn.execute(
            "SELECT id, kind, day, since, until, created_at, updated_at FROM briefings"
            f" WHERE kind IN ({', '.join('?' for _ in kinds)}) ORDER BY day DESC, id DESC LIMIT ?",
            (*kinds, LISTED),
        ).fetchall()
    )
    found = {
        "briefings": briefings, "read_id": _read_id(conn, DAILY), "launch": launch_status(conn, setup, DAILY, now),
        "hour": schedule(conn, setup)["briefing_hour"], "team": team(conn),
    }
    if digest_on(setup):
        found |= {"digest_read_id": _read_id(conn, WEEKLY), "digest": launch_status(conn, setup, WEEKLY, now)}
    return found


def _newest(conn: sqlite3.Connection, kind: Kind) -> tuple[int, str, bool] | None:
    """The newest briefing of `kind`: its id, its day and whether it is unread."""
    found = conn.execute(
        "SELECT id, day FROM briefings WHERE kind = ? ORDER BY day DESC LIMIT 1", (kind.name,)
    ).fetchone()
    return None if found is None else (found[0], found[1], found[0] > _read_id(conn, kind))


def status(conn: sqlite3.Connection, setup: Setup) -> dict[str, Any] | None:
    """For the tab's mark: the newest briefing, and whether the user has opened it; with the digest on, it is also
    unread while the newest digest is."""
    newest = _newest(conn, DAILY)
    if newest is None:
        return None
    digest = _newest(conn, WEEKLY) if digest_on(setup) else None
    return {"id": newest[0], "day": newest[1], "unread": newest[2] or (digest is not None and digest[2])}
