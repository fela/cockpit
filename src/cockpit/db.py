"""SQLite storage: schema, migrations and row helpers."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cockpit import parts

MIGRATIONS: list[str] = [
    """
    CREATE TABLE goals (
        id INTEGER PRIMARY KEY,
        week TEXT NOT NULL,
        text TEXT NOT NULL,
        position INTEGER NOT NULL
    );
    CREATE INDEX goals_week ON goals(week, position);

    CREATE TABLE items (
        id INTEGER PRIMARY KEY,
        key TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        summary TEXT NOT NULL DEFAULT '',
        project TEXT NOT NULL DEFAULT '',
        stage TEXT CHECK (stage IN ('planning', 'building', 'review', 'dev', 'staging', 'prod', 'done')),
        waiting_on TEXT NOT NULL DEFAULT '[]',
        priority TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('high', 'normal', 'low')),
        goal_id INTEGER REFERENCES goals(id) ON DELETE SET NULL,
        blocks TEXT NOT NULL DEFAULT '',
        sessions TEXT NOT NULL DEFAULT '[]',
        links TEXT NOT NULL DEFAULT '[]',
        pinged_at TEXT,
        touched_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        updated_by TEXT NOT NULL,
        archived INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE steps (
        id INTEGER PRIMARY KEY,
        item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        need TEXT NOT NULL,
        session_id TEXT,
        ask TEXT NOT NULL,
        why TEXT NOT NULL DEFAULT '',
        body TEXT NOT NULL DEFAULT '',
        details TEXT NOT NULL DEFAULT '[]',
        draft TEXT NOT NULL DEFAULT '',
        score INTEGER NOT NULL DEFAULT 50 CHECK (score BETWEEN 0 AND 100),
        reasons TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'dismissed')),
        pin INTEGER,
        nudge INTEGER NOT NULL DEFAULT 0 CHECK (nudge BETWEEN -30 AND 30),
        snoozed_until TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        updated_by TEXT NOT NULL,
        closed_at TEXT,
        UNIQUE (item_id, need)
    );
    CREATE INDEX steps_open ON steps(status, item_id);

    CREATE TABLE messages (
        id INTEGER PRIMARY KEY,
        author TEXT NOT NULL,
        item_id INTEGER REFERENCES items(id) ON DELETE SET NULL,
        session_id TEXT,
        new_cwd TEXT,
        new_name TEXT,
        text TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('queued', 'sending', 'sent', 'failed')),
        via TEXT CHECK (via IN ('socket', 'resume', 'new')),
        detail TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        sent_at TEXT
    );

    CREATE TABLE feedback (
        id INTEGER PRIMARY KEY,
        step_id INTEGER REFERENCES steps(id) ON DELETE SET NULL,
        item_id INTEGER REFERENCES items(id) ON DELETE SET NULL,
        text TEXT NOT NULL,
        created_at TEXT NOT NULL,
        read_at TEXT
    );

    CREATE TABLE kv (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    # Withdrawn steps keep their id (a deleted id would be reused by the next
    # step, and a stale click would act on it). The user's own item fields are
    # recorded so a sweep leaves them alone. Lists stored as JSON null by an
    # earlier bug are repaired. The events log feeds `cockpit patterns`.
    """
    ALTER TABLE steps ADD COLUMN withdrawn_at TEXT;
    ALTER TABLE steps ADD COLUMN prepared_by TEXT NOT NULL DEFAULT '';
    ALTER TABLE steps ADD COLUMN prepared_at TEXT NOT NULL DEFAULT '';
    UPDATE steps SET prepared_by = CASE WHEN updated_by = 'user' THEN '' ELSE updated_by END,
                     prepared_at = CASE WHEN updated_by = 'user' THEN created_at ELSE updated_at END;
    DROP INDEX steps_open;
    CREATE INDEX steps_open ON steps(status, withdrawn_at, item_id);

    ALTER TABLE items ADD COLUMN user_fields TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE messages ADD COLUMN step_id INTEGER REFERENCES steps(id) ON DELETE SET NULL;
    UPDATE items SET waiting_on = '[]' WHERE waiting_on = 'null';
    UPDATE items SET sessions = '[]' WHERE sessions = 'null';
    UPDATE items SET links = '[]' WHERE links = 'null';
    UPDATE steps SET details = '[]' WHERE details = 'null';
    UPDATE steps SET reasons = '[]' WHERE reasons = 'null';

    CREATE TABLE events (
        id INTEGER PRIMARY KEY,
        at TEXT NOT NULL,
        actor TEXT NOT NULL,
        kind TEXT NOT NULL,
        item_id INTEGER,
        step_id INTEGER,
        need TEXT,
        data TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX events_at ON events(at);
    """,
    # Feedback can point at any part of the page or the spec: `target` names it
    # (`spec:SPEC-7`, `page:<what>`) and `quote` keeps the text the user saw there.
    """
    ALTER TABLE feedback ADD COLUMN target TEXT NOT NULL DEFAULT '';
    ALTER TABLE feedback ADD COLUMN quote TEXT NOT NULL DEFAULT '';
    """,
    # A ready step waits as "ready next" until one of the user's places frees;
    # `queued_at` is when it took a place. Steps open before this keep a place
    # only when the user pinned them; the server's next fill gives the strongest
    # of the rest the free places. The sweeper is woken by events after
    # `sweep_seen_event`; events from before this migration are history, not news.
    """
    ALTER TABLE steps ADD COLUMN queued_at TEXT;
    UPDATE steps SET queued_at = created_at WHERE withdrawn_at IS NULL AND pin IS NOT NULL;
    INSERT INTO kv (key, value) SELECT 'sweep_seen_event', COALESCE(MAX(id), 0) FROM events;
    """,
    # The user can hand a step to an agent with a message: `handoff_message_id`
    # names that message, and the step stays out of the user's queue and ready next
    # until the agent closes or returns it, or the user takes it back.
    """
    ALTER TABLE steps ADD COLUMN handoff_message_id INTEGER REFERENCES messages(id);
    """,
    # Tool calls that the user's hooks report: `denied` when auto mode blocked one,
    # `asked` when a session shows a permission dialog and the hook waits for
    # The user's answer on Cockpit. A repeated denial of the same call counts up.
    """
    CREATE TABLE permission_requests (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('denied', 'asked')),
        state TEXT NOT NULL CHECK (state IN ('open', 'allowed', 'denied', 'dismissed', 'answered', 'expired')),
        session_id TEXT NOT NULL,
        cwd TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        tool_input TEXT NOT NULL,
        tool_use_id TEXT NOT NULL,
        reason TEXT NOT NULL,
        count INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        last_at TEXT NOT NULL,
        resolved_at TEXT,
        rule TEXT,
        rule_list TEXT,
        message_id INTEGER REFERENCES messages(id)
    );
    CREATE INDEX permission_requests_open ON permission_requests(state, last_at);
    """,
    # When a step was last checked against live state, by whom, and what was checked.
    # An agent putting the step's text checks it as part of the put.
    """
    ALTER TABLE steps ADD COLUMN checked_at TEXT;
    ALTER TABLE steps ADD COLUMN checked_by TEXT NOT NULL DEFAULT '';
    ALTER TABLE steps ADD COLUMN check_note TEXT NOT NULL DEFAULT '';
    UPDATE kv SET value = json_set(value, '$.kind', 'sweep', '$.step_ids', json('[]')) WHERE key = 'sweep_launch';
    """,
    # When the user last worked on the step on the page: opened it, wrote in its
    # composer, followed a link in it, copied from it, or sent its session a message.
    """
    ALTER TABLE steps ADD COLUMN user_active_at TEXT;
    """,
    # The user's guidance on priorities becomes a list of messages to the sweeper:
    # each is one instruction, kept until the user drops it. The single text it
    # replaces, when not empty, becomes the first message.
    # An item's priority gains `urgent`. The CHECK constraint lists the values,
    # so `items` is rebuilt (https://www.sqlite.org/lang_altertable.html, "Making
    # Other Kinds Of Table Schema Changes"): `migrate` runs this with foreign keys
    # off, which keeps the drop of `items` from cascading into `steps`, and checks
    # every reference before the commit. `items` has no indexes besides the one
    # its UNIQUE key creates.
    # Raise and Lower are gone, and with them the step's nudge.
    """
    CREATE TABLE guidance (
        id INTEGER PRIMARY KEY,
        text TEXT NOT NULL,
        created_at TEXT NOT NULL,
        dropped_at TEXT
    );
    INSERT INTO guidance (text, created_at)
        SELECT text, strftime('%Y-%m-%dT%H:%M:%SZ', 'now') FROM (
            SELECT trim(json_extract(value, '$'), ' ' || char(9, 10, 13)) AS text FROM kv WHERE key = 'guidance'
        )
        WHERE text != '';
    DELETE FROM kv WHERE key = 'guidance';

    CREATE TABLE new_items (
        id INTEGER PRIMARY KEY,
        key TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        summary TEXT NOT NULL DEFAULT '',
        project TEXT NOT NULL DEFAULT '',
        stage TEXT CHECK (stage IN ('planning', 'building', 'review', 'dev', 'staging', 'prod', 'done')),
        waiting_on TEXT NOT NULL DEFAULT '[]',
        priority TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('urgent', 'high', 'normal', 'low')),
        goal_id INTEGER REFERENCES goals(id) ON DELETE SET NULL,
        blocks TEXT NOT NULL DEFAULT '',
        sessions TEXT NOT NULL DEFAULT '[]',
        links TEXT NOT NULL DEFAULT '[]',
        pinged_at TEXT,
        touched_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        updated_by TEXT NOT NULL,
        archived INTEGER NOT NULL DEFAULT 0,
        user_fields TEXT NOT NULL DEFAULT '[]'
    );
    INSERT INTO new_items (
        id, key, title, summary, project, stage, waiting_on, priority, goal_id, blocks, sessions, links,
        pinged_at, touched_at, created_at, updated_at, updated_by, archived, user_fields
    )
    SELECT
        id, key, title, summary, project, stage, waiting_on, priority, goal_id, blocks, sessions, links,
        pinged_at, touched_at, created_at, updated_at, updated_by, archived, user_fields
    FROM items;
    DROP TABLE items;
    ALTER TABLE new_items RENAME TO items;

    ALTER TABLE steps DROP COLUMN nudge;
    """,
    # The user's own to-do list: a line of text the user writes, kept until the user ticks it
    # off. It has no item, step, session or rank; agents read the open ones.
    """
    CREATE TABLE todos (
        id INTEGER PRIMARY KEY,
        text TEXT NOT NULL,
        created_at TEXT NOT NULL,
        done_at TEXT
    );
    """,
    # A to-do the user adds is an item the sweeper triages, not a list of its own:
    # `todo` marks an item the user added with Add. Each open to-do becomes such an
    # item, keyed `todo-<item id>`, with the user's text as its summary, which stays
    # as the user wrote it; done ones go with the table.
    # The user attaches images to an item; the bytes live here so the daily backup
    # covers them. Image ids are never reused (AUTOINCREMENT), because the page
    # caches an image by its URL for good.
    """
    ALTER TABLE items ADD COLUMN todo INTEGER NOT NULL DEFAULT 0 CHECK (todo IN (0, 1));
    INSERT INTO items (key, title, summary, todo, user_fields, created_at, updated_at, updated_by)
        SELECT 'todo-from-' || id, trim(substr(text, 1, instr(text || char(10), char(10)) - 1)), text, 1,
            '["summary"]', created_at, created_at, 'user'
        FROM todos WHERE done_at IS NULL ORDER BY id;
    UPDATE items SET key = 'todo-' || id WHERE key LIKE 'todo-from-%';
    DROP TABLE todos;

    CREATE TABLE item_images (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        content_type TEXT NOT NULL CHECK (content_type IN ('image/png', 'image/jpeg', 'image/gif', 'image/webp')),
        size INTEGER NOT NULL,
        content BLOB NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX item_images_item ON item_images(item_id, id);
    """,
    # A session takes back a blocked call it no longer needs (state `withdrawn`) and proposes the auto-mode sentence
    # for one (`proposed_sentence`). The user can allow a blocked call once (history
    """
    CREATE TABLE new_permission_requests (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('denied', 'asked')),
        state TEXT NOT NULL
            CHECK (state IN ('open', 'allowed', 'denied', 'dismissed', 'answered', 'expired', 'withdrawn')),
        session_id TEXT NOT NULL,
        cwd TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        tool_input TEXT NOT NULL,
        tool_use_id TEXT NOT NULL,
        reason TEXT NOT NULL,
        count INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        last_at TEXT NOT NULL,
        resolved_at TEXT,
        rule TEXT,
        rule_list TEXT,
        message_id INTEGER REFERENCES messages(id),
        proposed_sentence TEXT,
        rule_once INTEGER NOT NULL DEFAULT 0 CHECK (rule_once IN (0, 1)),
        rule_expires_at TEXT,
        rule_ended_at TEXT
    );
    INSERT INTO new_permission_requests (
        id, kind, state, session_id, cwd, tool_name, tool_input, tool_use_id, reason, count, created_at, last_at,
        resolved_at, rule, rule_list, message_id
    )
    SELECT
        id, kind, state, session_id, cwd, tool_name, tool_input, tool_use_id, reason, count, created_at, last_at,
        resolved_at, rule, rule_list, message_id
    FROM permission_requests;
    DROP TABLE permission_requests;
    ALTER TABLE new_permission_requests RENAME TO permission_requests;
    CREATE INDEX permission_requests_open ON permission_requests(state, last_at);
    CREATE INDEX permission_requests_once ON permission_requests(rule_once, rule_ended_at);
    """,
    # Agents attach images too: `added_by` is `user` or `agent:<name>`; the
    # images before this are the user's. A failed message can be dismissed once its
    # work was done another way: the dismissal is a note on the failed message
    # (who, when, why), not a state of its own, so `state` keeps saying how
    # delivery ended and a restore clears the note.
    """
    ALTER TABLE item_images ADD COLUMN added_by TEXT NOT NULL DEFAULT 'user';
    ALTER TABLE messages ADD COLUMN dismissed_at TEXT;
    ALTER TABLE messages ADD COLUMN dismissed_by TEXT;
    ALTER TABLE messages ADD COLUMN dismiss_reason TEXT;
    """,
    # One-click buttons on a step: `answers` the user can send as their reply,
    # `command` ({run, cwd}) Cockpit runs as the user when they press Run, and
    # `permission` ({list, rule}) Cockpit adds when they press Allow; JSON null
    # when the step has none. A run keeps its command (`command_text`, which
    # decodes as text, unlike the step's JSON `command`), output and exit code,
    # since the step's command can change after it.
    """
    ALTER TABLE steps ADD COLUMN answers TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE steps ADD COLUMN command TEXT NOT NULL DEFAULT 'null';
    ALTER TABLE steps ADD COLUMN permission TEXT NOT NULL DEFAULT 'null';

    CREATE TABLE command_runs (
        id INTEGER PRIMARY KEY,
        step_id INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
        session_id TEXT NOT NULL,
        command_text TEXT NOT NULL,
        cwd TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        exit_code INTEGER,
        output TEXT NOT NULL DEFAULT '',
        result_message_id INTEGER REFERENCES messages(id)
    );
    CREATE INDEX command_runs_step ON command_runs(step_id, id);
    """,
    # Time the user spends on a step (SPEC TRACK-2): a span runs from a start until a
    # pause, a start on another step (`switch`), or the step leaving the user (the
    # hand-off actions `hand_off`, `ran` and `allowed`, or done, dismiss, withdraw,
    # snooze). At most one span runs at a time: the partial index holds one row
    # with no `ended_at`.
    """
    CREATE TABLE time_spans (
        id INTEGER PRIMARY KEY,
        step_id INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        ended_by TEXT CHECK (
            ended_by IN ('pause', 'switch', 'hand_off', 'ran', 'allowed', 'done', 'dismiss', 'withdraw', 'snooze')
        ),
        CHECK ((ended_at IS NULL) = (ended_by IS NULL))
    );
    CREATE UNIQUE INDEX time_spans_running ON time_spans((ended_at IS NULL)) WHERE ended_at IS NULL;
    CREATE INDEX time_spans_step ON time_spans(step_id, id);
    """,
    # A session's withdrawal waits while the user works on the call's card (SPEC PERM-9):
    # `user_active_at` is when the user last worked on the card, `withdrawn_at` when the
    # session took the call back. The request stays open until the user answers or
    # stops working on it; an answer then still adds its rule.
    """
    ALTER TABLE permission_requests ADD COLUMN user_active_at TEXT;
    ALTER TABLE permission_requests ADD COLUMN withdrawn_at TEXT;
    UPDATE permission_requests SET withdrawn_at = resolved_at WHERE state = 'withdrawn';
    """,
    # The user's own messages arrive as the user's prompt (SPEC MSG-3): `as_prompt` is 1 for a
    # message the user wrote, which a live session gets by a stop and a resume once its
    # turn is over. `waits_since` is when such a message first found the session
    # mid-turn; the message stays queued with it set while it waits.
    """
    ALTER TABLE messages ADD COLUMN as_prompt INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE messages ADD COLUMN waits_since TEXT;
    """,
    # The user's time (SPEC TRACK-3): each minute with the user's input on a Cockpit page. The
    # history starts with the minutes of the user's writes in the events log; kv
    # `input_minutes_since` says from when input on a page counts as well.
    """
    CREATE TABLE input_minutes (minute TEXT PRIMARY KEY);
    INSERT OR IGNORE INTO input_minutes (minute) SELECT substr(at, 1, 16) FROM events WHERE actor = 'user';
    INSERT INTO kv (key, value) VALUES ('input_minutes_since', json_quote(strftime('%Y-%m-%dT%H:%M:%SZ', 'now')));
    """,
    # A message about a step remembers the step as its author saw it: `step_ask` is the
    # step's ask when the message was written, and `hands_off` is 1 for a message that
    # handed the step over. A message that waited for the session's turn quotes that
    # ask and says what changed since (delivery.compose). Older hand-offs are found by
    # their step event, written in the same second as the message.
    """
    ALTER TABLE messages ADD COLUMN step_ask TEXT;
    ALTER TABLE messages ADD COLUMN hands_off INTEGER NOT NULL DEFAULT 0;
    UPDATE messages SET step_ask = (SELECT ask FROM steps WHERE steps.id = messages.step_id) WHERE step_id IS NOT NULL;
    UPDATE messages SET hands_off = 1
        WHERE id IN (SELECT handoff_message_id FROM steps WHERE handoff_message_id IS NOT NULL)
        OR EXISTS (
            SELECT 1 FROM events e WHERE e.step_id = messages.step_id AND e.at = messages.created_at
            AND e.kind IN ('step.hand_off', 'step.allowed', 'step.ran')
        );
    """,
    # The user's goals and their priority messages to the sweeper become one list (SPEC
    # CONTROL-12), in the user's order, with no week: `position` orders the goals that
    # are not dropped (1 is the most important), `paused_at` sets one aside until
    # The user resumes it (CONTROL-11), and `dropped_at` takes it back. The goals come
    # first with their ids, so items keep theirs, newest week first; then the
    # priority messages, newest first, with ids after the goals', dropped ones
    # last. Their events name the goal they became. `goals` is rebuilt the way
    # migration 9 rebuilt `items`.
    """
    CREATE TABLE new_goals (
        id INTEGER PRIMARY KEY,
        text TEXT NOT NULL,
        position INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        paused_at TEXT,
        dropped_at TEXT
    );
    INSERT INTO new_goals (id, text, position, created_at, dropped_at)
    SELECT id, text, row_number() OVER (ORDER BY source, newest DESC, place), created_at, dropped_at FROM (
        SELECT id, text, 0 AS source, week AS newest, position AS place,
            coalesce(
                (SELECT min(at) FROM events WHERE kind = 'goals.saved' AND json_extract(data, '$.week') = goals.week),
                strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            ) AS created_at,
            NULL AS dropped_at
        FROM goals
        UNION ALL
        SELECT (SELECT coalesce(max(id), 0) FROM goals) + id, text, 1 + (dropped_at IS NOT NULL), id, 0, created_at, dropped_at
        FROM guidance
    );
    UPDATE events SET
        kind = 'goal.' || substr(kind, length('guidance.') + 1),
        data = json_object('goal_id', (SELECT coalesce(max(id), 0) FROM goals) + json_extract(data, '$.guidance_id'))
    WHERE kind IN ('guidance.added', 'guidance.dropped', 'guidance.restored');
    DROP TABLE guidance;
    DROP TABLE goals;
    ALTER TABLE new_goals RENAME TO goals;
    """,
    # What anyone but the user closed (SPEC VIEW-4): a step done or dismissed, an item
    # archived or moved to stage done, with the closer's one-line note, until the user
    # marks it seen. `ref_id` is the step's or the item's id, by `kind`. The last
    # two days come from the steps and items as they stand; an item's closing time
    # there is its last update.
    """
    CREATE TABLE closures (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('step', 'item')),
        ref_id INTEGER NOT NULL,
        item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        action TEXT NOT NULL CHECK (action IN ('done', 'dismissed', 'archived', 'stage done')),
        closed_by TEXT NOT NULL,
        closed_at TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        seen_at TEXT
    );
    CREATE INDEX closures_closed_at ON closures(closed_at);
    INSERT INTO closures (kind, ref_id, item_id, action, closed_by, closed_at)
    SELECT 'step', id, item_id, status, updated_by, closed_at FROM steps
    WHERE status != 'open' AND updated_by != 'user' AND closed_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-2 days');
    INSERT INTO closures (kind, ref_id, item_id, action, closed_by, closed_at)
    SELECT 'item', id, id, CASE WHEN archived = 1 THEN 'archived' ELSE 'stage done' END, updated_by, updated_at FROM items
    WHERE (archived = 1 OR stage = 'done') AND updated_by != 'user'
        AND updated_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-2 days');
    """,
    # How long the user wrote each feedback (SPEC TRACK-2); null on feedback sent before it was timed.
    """
    ALTER TABLE feedback ADD COLUMN seconds INTEGER;
    """,
    # The user's Archive on a session's last open step (SPEC SESSION-4): Cockpit tells the session with
    # `message_id` (none for a session already stopped), which may keep itself running, and stops it once
    # that turn is over. The user's reopen of the step cancels the archive; a later message to the session
    # revives it.
    """
    CREATE TABLE session_archives (
        id INTEGER PRIMARY KEY,
        session_id TEXT NOT NULL,
        step_id INTEGER NOT NULL REFERENCES steps(id),
        message_id INTEGER REFERENCES messages(id),
        asked_at TEXT NOT NULL,
        kept_at TEXT,
        kept_by TEXT,
        keep_reason TEXT NOT NULL DEFAULT '',
        stopped_at TEXT,
        failure TEXT NOT NULL DEFAULT '',
        cancelled_at TEXT,
        revived_at TEXT
    );
    CREATE INDEX session_archives_session ON session_archives(session_id);
    """,
    # The morning briefing on the user's team (SPEC VIEW-8): one per day in the user's timezone,
    # covering `since` to `until`; a second put on the same day replaces the text.
    """
    CREATE TABLE briefings (
        id INTEGER PRIMARY KEY,
        day TEXT NOT NULL UNIQUE,
        since TEXT NOT NULL,
        until TEXT NOT NULL,
        body TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    # Who opened each open item's pull request (SPEC VIEW-5, github.py), asked once per PR: `ref` is
    # `owner/repo#number`, `pull` is 0 when the number is an issue, `mine` is 1 when the user opened it.
    """
    CREATE TABLE pull_authors (
        ref TEXT PRIMARY KEY,
        pull INTEGER NOT NULL CHECK (pull IN (0, 1)),
        author TEXT NOT NULL,
        mine INTEGER NOT NULL CHECK (mine IN (0, 1)),
        checked_at TEXT NOT NULL
    );
    """,
    """
    ALTER TABLE permission_requests ADD COLUMN once_rules TEXT NOT NULL DEFAULT '[]';
    """,
    # The latest instruction a blocked call's session got before the block, read from its transcript when the
    # block is reported (SPEC PERM-13): {author, at, text}, or null when none was found.
    """
    ALTER TABLE permission_requests ADD COLUMN asked_by TEXT NOT NULL DEFAULT 'null';
    """,
    # A hand-off the user sent waits for Take back (SPEC MSG-8): nothing of it goes while the page's notice with Take
    # back shows. The page releases it once that notice is gone; at `take_back_until` the wake loop releases it
    # itself, for a page that closed meanwhile. Null once released, and on every other message.
    """
    ALTER TABLE messages ADD COLUMN take_back_until TEXT;
    """,
    # A call auto mode blocked under a credentials label (SPEC PERM-13): the session's latest earlier call that reached
    # for credentials, read from its transcript when the block is reported, as auto mode judges a call together with the
    # calls before it: {at, description, summary, credentials, allowed_once}, or null.
    # `allowed_once` holds history from older versions.
    """
    ALTER TABLE permission_requests ADD COLUMN credential_call TEXT NOT NULL DEFAULT 'null';
    """,
    # The same for every label: under another label, the session's latest earlier call that did more than read or
    # work in its job folder (permissions.earlier_call). The credentials calls stored so far keep their meaning.
    """
    ALTER TABLE permission_requests RENAME COLUMN credential_call TO earlier_call;
    """,
    # The user takes back time a timer counted by mistake (SPEC TRACK-4). `away`: the user said the timer ran on after
    # they left, and the span ends when their input on Cockpit stopped. `corrected`: the user set the item's time lower
    # while this span ran, so it ends earlier and a new span runs on from then; it also marks a span the user added by
    # setting the time higher (SPEC TRACK-7). `away_kept_until`: the user said they worked through a stretch without
    # input up to then, so only a later stretch asks again.
    """
    CREATE TABLE time_spans_new (
        id INTEGER PRIMARY KEY,
        step_id INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        ended_by TEXT CHECK (
            ended_by IN (
                'pause', 'switch', 'hand_off', 'ran', 'allowed', 'done', 'dismiss', 'withdraw', 'snooze',
                'away', 'corrected'
            )
        ),
        away_kept_until TEXT,
        CHECK ((ended_at IS NULL) = (ended_by IS NULL))
    );
    INSERT INTO time_spans_new (id, step_id, started_at, ended_at, ended_by)
        SELECT id, step_id, started_at, ended_at, ended_by FROM time_spans;
    DROP TABLE time_spans;
    ALTER TABLE time_spans_new RENAME TO time_spans;
    CREATE UNIQUE INDEX time_spans_running ON time_spans((ended_at IS NULL)) WHERE ended_at IS NULL;
    CREATE INDEX time_spans_step ON time_spans(step_id, id);
    """,
    # What a call on the page does, in Claude's words (SPEC PERM-3, explain.py): {plain}, or {error} when Claude
    # gave no answer; null until Cockpit asked.
    """
    ALTER TABLE permission_requests ADD COLUMN explanation TEXT NOT NULL DEFAULT 'null';
    """,
    # Two removed parts: the header list of what agents closed, which the user marked seen (SPEC VIEW-4; Finished this
    # week shows who closed each step and why), and `cockpit session keep` after the user's Archive (SPEC SESSION-4; the
    # sweep's check of each archive is the override).
    """
    ALTER TABLE closures DROP COLUMN seen_at;
    ALTER TABLE session_archives DROP COLUMN kept_at;
    ALTER TABLE session_archives DROP COLUMN kept_by;
    ALTER TABLE session_archives DROP COLUMN keep_reason;
    """,
    """
    CREATE TABLE held_changes (
        id INTEGER PRIMARY KEY,
        step_id INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK (kind IN ('step_put', 'step_action', 'item_put')),
        actor TEXT NOT NULL,
        data TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    ALTER TABLE steps DROP COLUMN user_active_at;
    ALTER TABLE steps DROP COLUMN pin;
    """,
    # Sessions share HTML pages on an item instead of Claude artifacts: a page keeps its id when its session
    # replaces it, so a link to /pages/<id> stays good across versions.
    """
    CREATE TABLE item_pages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        title TEXT NOT NULL,
        html TEXT NOT NULL,
        size INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        added_by TEXT NOT NULL
    );
    CREATE INDEX item_pages_item ON item_pages(item_id, id);
    """,
    # The user's own actor is 'user' and the columns that hold the user's part say user. Installs before this one
    # wrote the first user's name instead, so every actor that is neither Cockpit nor an agent becomes 'user'.
    # `items` and `permission_requests` are rebuilt as in migration 9 and copied by column position, since the column
    # they rename has the old name there and the new one on a new install; their indexes are made again.
    # `item_images.added_by` moves to a new column for its new default.
    """
    UPDATE events SET actor = 'user' WHERE actor != 'cockpit' AND actor NOT LIKE 'agent:%';
    UPDATE held_changes SET actor = 'user' WHERE actor != 'cockpit' AND actor NOT LIKE 'agent:%';
    UPDATE messages SET author = 'user' WHERE author != 'cockpit' AND author NOT LIKE 'agent:%';
    UPDATE messages SET dismissed_by = 'user' WHERE dismissed_by != 'cockpit' AND dismissed_by NOT LIKE 'agent:%';
    UPDATE steps SET updated_by = 'user' WHERE updated_by != 'cockpit' AND updated_by NOT LIKE 'agent:%';
    UPDATE items SET updated_by = 'user' WHERE updated_by != 'cockpit' AND updated_by NOT LIKE 'agent:%';
    UPDATE item_pages SET added_by = 'user' WHERE added_by != 'cockpit' AND added_by NOT LIKE 'agent:%';
    ALTER TABLE item_images RENAME COLUMN added_by TO added_by_before;
    ALTER TABLE item_images ADD COLUMN added_by TEXT NOT NULL DEFAULT 'user';
    UPDATE item_images SET added_by = CASE WHEN added_by_before LIKE 'agent:%' THEN added_by_before ELSE 'user' END;
    ALTER TABLE item_images DROP COLUMN added_by_before;
    UPDATE kv SET key = 'user_seen' WHERE key LIKE '%\\_seen' ESCAPE '\\';
    UPDATE kv SET key = 'user_input' WHERE key LIKE '%\\_input' ESCAPE '\\';

    CREATE TABLE new_items (
        id INTEGER PRIMARY KEY,
        key TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        summary TEXT NOT NULL DEFAULT '',
        project TEXT NOT NULL DEFAULT '',
        stage TEXT CHECK (stage IN ('planning', 'building', 'review', 'dev', 'staging', 'prod', 'done')),
        waiting_on TEXT NOT NULL DEFAULT '[]',
        priority TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('urgent', 'high', 'normal', 'low')),
        goal_id INTEGER REFERENCES goals(id) ON DELETE SET NULL,
        blocks TEXT NOT NULL DEFAULT '',
        sessions TEXT NOT NULL DEFAULT '[]',
        links TEXT NOT NULL DEFAULT '[]',
        pinged_at TEXT,
        touched_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        updated_by TEXT NOT NULL,
        archived INTEGER NOT NULL DEFAULT 0,
        user_fields TEXT NOT NULL DEFAULT '[]',
        todo INTEGER NOT NULL DEFAULT 0 CHECK (todo IN (0, 1))
    );
    INSERT INTO new_items SELECT * FROM items;
    DROP TABLE items;
    ALTER TABLE new_items RENAME TO items;

    CREATE TABLE new_permission_requests (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('denied', 'asked')),
        state TEXT NOT NULL
            CHECK (state IN ('open', 'allowed', 'denied', 'dismissed', 'answered', 'expired', 'withdrawn')),
        session_id TEXT NOT NULL,
        cwd TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        tool_input TEXT NOT NULL,
        tool_use_id TEXT NOT NULL,
        reason TEXT NOT NULL,
        count INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        last_at TEXT NOT NULL,
        resolved_at TEXT,
        rule TEXT,
        rule_list TEXT,
        message_id INTEGER REFERENCES messages(id),
        proposed_sentence TEXT,
        rule_once INTEGER NOT NULL DEFAULT 0 CHECK (rule_once IN (0, 1)),
        rule_expires_at TEXT,
        rule_ended_at TEXT,
        user_active_at TEXT,
        withdrawn_at TEXT,
        once_rules TEXT NOT NULL DEFAULT '[]',
        asked_by TEXT NOT NULL DEFAULT 'null',
        earlier_call TEXT NOT NULL DEFAULT 'null',
        explanation TEXT NOT NULL DEFAULT 'null'
    );
    INSERT INTO new_permission_requests SELECT * FROM permission_requests;
    DROP TABLE permission_requests;
    ALTER TABLE new_permission_requests RENAME TO permission_requests;
    CREATE INDEX permission_requests_open ON permission_requests(state, last_at);
    CREATE INDEX permission_requests_once ON permission_requests(rule_once, rule_ended_at);
    """,
    # The user pins an item so it stays in the Pinned list: when they pinned it, or null. Only the user sets it.
    """
    ALTER TABLE items ADD COLUMN pinned_at TEXT;
    """,
    # Errors Cockpit's own pages met (SPEC CODE-2, page_errors.py): one row per page, place and message, with
    # how often it was seen and whether a session took it to fix.
    """
    CREATE TABLE page_errors (
        id INTEGER PRIMARY KEY,
        fingerprint TEXT NOT NULL UNIQUE,
        page TEXT NOT NULL,
        place TEXT NOT NULL,
        message TEXT NOT NULL,
        stack TEXT NOT NULL,
        count INTEGER NOT NULL,
        first_at TEXT NOT NULL,
        last_at TEXT NOT NULL,
        taken_at TEXT,
        taken_note TEXT
    );
    """,
    # An item's history (SPEC VIEW-10, VIEW-13). `step_versions`: each text of a step as a put left it. Steps
    # from before keep only their last text, as one version at the time it was written. `session_turns`: each
    # turn of a session linked to an item, with the reply that ended it, read from the session's transcript
    # (`transcript_reads` keeps how far). `summaries`: one line on a long text, which Claude writes in the
    # background; `ref` names the text, e.g. `message:10`, and `at` is when it was written. An item's story is
    # kept here too, as `story:<item id>:<hash of its input>`.
    """
    CREATE TABLE step_versions (
        id INTEGER PRIMARY KEY,
        step_id INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
        at TEXT NOT NULL,
        actor TEXT NOT NULL,
        ask TEXT NOT NULL,
        why TEXT NOT NULL,
        body TEXT NOT NULL,
        details TEXT NOT NULL,
        draft TEXT NOT NULL,
        answers TEXT NOT NULL,
        command TEXT NOT NULL,
        permission TEXT NOT NULL
    );
    CREATE INDEX step_versions_step ON step_versions(step_id, at);
    INSERT INTO step_versions (step_id, at, actor, ask, why, body, details, draft, answers, command, permission)
        SELECT id, COALESCE(NULLIF(prepared_at, ''), updated_at), COALESCE(NULLIF(prepared_by, ''), updated_by),
               ask, why, body, details, draft, answers, command, permission
        FROM steps;

    CREATE TABLE session_turns (
        id INTEGER PRIMARY KEY,
        session_id TEXT NOT NULL,
        started_at TEXT NOT NULL,
        ended_at TEXT NOT NULL,
        reply TEXT NOT NULL,
        UNIQUE (session_id, ended_at)
    );
    CREATE TABLE transcript_reads (
        path TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        offset INTEGER NOT NULL
    );

    CREATE TABLE summaries (
        ref TEXT PRIMARY KEY,
        at TEXT NOT NULL,
        text TEXT NOT NULL,
        summary TEXT,
        failed TEXT,
        asked_at TEXT NOT NULL,
        made_at TEXT
    );
    """,
    # How big the user's part of a step is and how far its item's work is: the session that puts the step sets both,
    # the sweeper fills in what is missing, and the page draws one small bar from them. Null until someone set it.
    """
    ALTER TABLE steps ADD COLUMN size TEXT CHECK (size IN ('small', 'medium', 'large'));
    ALTER TABLE steps ADD COLUMN progress INTEGER CHECK (progress BETWEEN 0 AND 100);
    """,
    # Complete task and Close item. Each one-press answer is `{text, closes}`: an answer that closes completes its
    # item. `item_finishes`: each time the user completed or closed an item, with what it was before and the steps it
    # closed, so Undo puts all of it back (`reopened_at`). A message whose answer completed its item has `completes`
    # 1, and its delivery says so.
    """
    UPDATE steps SET answers = (
        SELECT json_group_array(json_object('text', value, 'closes', json('false'))) FROM json_each(steps.answers)
    ) WHERE answers != '[]';
    UPDATE step_versions SET answers = (
        SELECT json_group_array(json_object('text', value, 'closes', json('false'))) FROM json_each(step_versions.answers)
    ) WHERE answers != '[]';
    ALTER TABLE messages ADD COLUMN completes INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE item_finishes (
        id INTEGER PRIMARY KEY,
        item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        step_id INTEGER REFERENCES steps(id) ON DELETE SET NULL,
        how TEXT NOT NULL CHECK (how IN ('complete', 'close')),
        reason TEXT NOT NULL,
        message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
        before_fields TEXT NOT NULL,
        closed_steps TEXT NOT NULL,
        at TEXT NOT NULL,
        reopened_at TEXT
    );
    CREATE INDEX item_finishes_item ON item_finishes(item_id, id);
    """,
    # Good writing examples: a step, a shared page or any part of a tab the user marked as well written, with a copy
    # of its text as it was (`text` in `format`: the step's Markdown, the page's HTML, or the part's words), the
    # user's note on why, where it came from, and the session and actor that wrote it, so agents can read how it was
    # made.
    """
    CREATE TABLE writing_examples (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('step', 'page', 'part')),
        title TEXT NOT NULL,
        target TEXT NOT NULL,
        text TEXT NOT NULL,
        format TEXT NOT NULL CHECK (format IN ('markdown', 'html', 'text')),
        note TEXT NOT NULL,
        step_id INTEGER REFERENCES steps(id) ON DELETE SET NULL,
        item_id INTEGER REFERENCES items(id) ON DELETE SET NULL,
        page_id INTEGER REFERENCES item_pages(id) ON DELETE SET NULL,
        session_id TEXT,
        session_name TEXT NOT NULL,
        written_by TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """,
    # The owning session's latest status on an item the user added with Add. The user's words stay in `summary`; an
    # agent's summary on such an item lands here, and the item's card shows both.
    """
    ALTER TABLE items ADD COLUMN agent_summary TEXT NOT NULL DEFAULT '';
    """,
    # When the user last saw an item: their latest act on it on the page, an event of theirs or a timer start on one
    # of its steps, and whether that act set it aside (a snooze, a lowered priority). Filled from the event log and
    # the timers; a snooze that was undone since does not count. Older priority edits do not say what the priority
    # was before, so one counts as setting the item aside unless the item is now urgent, which only a raise reaches.
    """
    ALTER TABLE items ADD COLUMN seen_at TEXT;
    ALTER TABLE items ADD COLUMN set_aside INTEGER NOT NULL DEFAULT 0;
    UPDATE items SET seen_at = NULLIF(MAX(
        COALESCE((SELECT MAX(at) FROM events WHERE actor = 'user' AND item_id = items.id), ''),
        COALESCE((SELECT MAX(t.started_at) FROM time_spans t JOIN steps s ON s.id = t.step_id WHERE s.item_id = items.id), '')
    ), '');
    UPDATE items SET set_aside = 1 WHERE EXISTS (
        SELECT 1 FROM events e
        WHERE e.id = (SELECT MAX(id) FROM events WHERE actor = 'user' AND item_id = items.id) AND e.at = items.seen_at
          AND ((e.kind = 'step.snooze' AND (SELECT snoozed_until FROM steps WHERE id = e.step_id) IS NOT NULL)
            OR (e.kind = 'item.edited' AND e.data LIKE '%"priority"%' AND items.priority != 'urgent'))
    );
    """,
    # Each progress estimate a step put gave, with its time, so Cockpit can check the estimates against how the work
    # went once its item finished. Until now only a step's latest progress was kept: each step that has one starts
    # with that, at the step's last put. `items.done_at`: when the item last moved to stage done, by anyone. Earlier
    # moves were not all kept, so an item at stage done now starts with the latest of: the user's Completes that were
    # not undone and moved it from another stage, agents' moves to stage done, and the user's edits of its stage
    # (which record that the stage changed, not to what); else its last update.
    """
    ALTER TABLE items ADD COLUMN done_at TEXT;
    UPDATE items SET done_at = COALESCE((SELECT MAX(at) FROM (
        SELECT at FROM item_finishes WHERE item_id = items.id AND how = 'complete' AND reopened_at IS NULL
            AND json_extract(before_fields, '$.stage') IS NOT 'done'
        UNION ALL SELECT closed_at FROM closures WHERE item_id = items.id AND action = 'stage done'
        UNION ALL SELECT at FROM events WHERE item_id = items.id AND actor = 'user' AND kind = 'item.edited'
            AND EXISTS (SELECT 1 FROM json_each(events.data, '$.fields') WHERE value = 'stage'))), updated_at)
    WHERE stage = 'done';
    CREATE TABLE progress_marks (
        id INTEGER PRIMARY KEY,
        step_id INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
        item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        at TEXT NOT NULL,
        progress INTEGER NOT NULL CHECK (progress BETWEEN 0 AND 100)
    );
    CREATE INDEX progress_marks_item ON progress_marks(item_id, at);
    INSERT INTO progress_marks (step_id, item_id, at, progress)
        SELECT id, item_id, COALESCE(NULLIF(prepared_at, ''), updated_at), progress FROM steps WHERE progress IS NOT NULL;
    """,
    # A page's revision goes up by one on every update. A step that shows the page as a figure names the revision in
    # the frame's address, so the frame loads again after any update; `updated_at` keeps whole seconds and stays the
    # same for two updates within one.
    """
    ALTER TABLE item_pages ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
    """,
    # Codex sessions: the provider a new session was asked for, and the threads Cockpit started. It ran before
    # main's migrations above on installs from the macOS/Codex branch; migrate() renumbers those databases
    # (CODEX_AHEAD).
    """
    ALTER TABLE messages ADD COLUMN provider TEXT CHECK (provider IN ('claude', 'codex'));
    CREATE TABLE codex_threads (
        session_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        cwd TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'stopped',
        detail TEXT NOT NULL DEFAULT ''
    );
    """,
    # When the user took a ready-next step to the top of their queue. The step leads the queue while `queued_at` is
    # still the place that take gave it (store.taken).
    """
    ALTER TABLE steps ADD COLUMN taken_at TEXT;
    """,
    # Sessions the user moved to another Claude account: the last move of each.
    """
    CREATE TABLE session_moves (
        session_id TEXT PRIMARY KEY,
        account TEXT NOT NULL,
        at TEXT NOT NULL
    );
    """,
    # The page's actions by the id the page gave each one: a row is written as the first try starts (status NULL)
    # and holds the answer once it succeeded, so a repeat after a lost answer gets that answer and the action happens
    # once. `at`: when it started, then was answered.
    """
    CREATE TABLE page_actions (
        id TEXT PRIMARY KEY,
        method TEXT NOT NULL,
        path TEXT NOT NULL,
        status INTEGER,
        content_type TEXT,
        body BLOB,
        at TEXT NOT NULL
    );
    CREATE INDEX page_actions_at ON page_actions(at);
    """,
    # Moves the user asked for while the session was busy: Cockpit carries each out once the session's turn is over
    # (moves.waits_due). `actor`: who asked, as the move records it.
    """
    CREATE TABLE session_move_waits (
        session_id TEXT PRIMARY KEY,
        account TEXT NOT NULL,
        actor TEXT NOT NULL,
        asked_at TEXT NOT NULL
    );
    """,
    # A step's progress is the share of the user's own time on the item's work that is behind them (SPEC NEXT-35
    """
    ALTER TABLE progress_marks ADD COLUMN of_user_time INTEGER NOT NULL DEFAULT 0 CHECK (of_user_time IN (0, 1));
    UPDATE steps SET progress = NULL;
    UPDATE held_changes SET data = json_remove(data, '$.progress') WHERE kind = 'step_put';
    """,
    # Side chats: the user's questions about an item's work to a read-only copy of its session (sidechat.py).
    # `usage`: the copy's own token use, from `claude -p`'s result; `sent_message_id`: the message that sent the
    # question on to the session itself, once the user did.
    """
    CREATE TABLE side_chats (
        id INTEGER PRIMARY KEY,
        item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        session_id TEXT NOT NULL,
        question TEXT NOT NULL,
        answer TEXT,
        error TEXT,
        state TEXT NOT NULL CHECK (state IN ('asking', 'answered', 'failed')),
        asked_at TEXT NOT NULL,
        answered_at TEXT,
        usage TEXT NOT NULL DEFAULT 'null',
        sent_message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL
    );
    CREATE INDEX side_chats_item ON side_chats(item_id, id);
    """,
    # Replies between sessions: the session an agent sent a message from, so the delivered text can say how to
    # answer it (delivery.compose). Claude Code's SendMessage only reaches sessions on its own account, and an
    # agent's --as name is no session name.
    """
    ALTER TABLE messages ADD COLUMN from_session TEXT;
    """,
    """
    ALTER TABLE writing_examples ADD COLUMN verdict TEXT NOT NULL DEFAULT 'good' CHECK (verdict IN ('good', 'bad'));
    """,
    """
    ALTER TABLE writing_examples ADD COLUMN skills TEXT NOT NULL DEFAULT '[]';
    """,
    """
    DELETE FROM summaries WHERE ref LIKE 'story:%';
    """,
    # Each line of an item's story starts with its UTC time, which the page shows as an age:
    # the stories with a day and month first are written again.
    """
    DELETE FROM summaries WHERE ref LIKE 'story:%';
    """,
    # The id of the page's press that started a span: a pause the page pressed after that start finds the span by
    # it, also when the start's answer never came back. Spans from before, and from pages loaded before, have none.
    """
    ALTER TABLE time_spans ADD COLUMN start_press TEXT;
    """,
    # The step's last version when the message went from it (SPEC NEXT-37): an agent's put after a hand-off adds a later
    # version, also in the same second. Messages from before get the last version from an earlier second: a version in
    # the message's own second counts as written after it, so an agent's return is let through rather than refused.
    """
    ALTER TABLE messages ADD COLUMN step_version INTEGER;
    UPDATE messages SET step_version = (
        SELECT MAX(v.id) FROM step_versions v
        WHERE v.step_id = messages.step_id AND julianday(v.at) < julianday(messages.created_at)
    ) WHERE step_id IS NOT NULL;
    """,
    # A step's own events: placing steps reads the last take of the queued steps (`store._started_in_place`).
    """
    CREATE INDEX events_step ON events(step_id, kind, at);
    """,
    # The finished sessions Cockpit took off `claude agents` (removals.py, SPEC SESSION-4): the session as Cockpit
    # listed it just before, the options its job file held, which `claude rm` deletes and a later resume passes again,
    # and the git worktree it ran in (JSON: path, repo, branch, commit), which `claude rm` deletes when clean and a
    # later resume adds again. `refusal`: why `claude rm` refused, such as a worktree with unpushed work; Cockpit then
    # leaves it listed.
    """
    CREATE TABLE session_removals (
        session_id TEXT PRIMARY KEY,
        session TEXT NOT NULL,
        flags TEXT NOT NULL,
        worktree TEXT,
        asked_at TEXT NOT NULL,
        removed_at TEXT,
        refusal TEXT
    );
    """,
    # When the user last opened, pressed or answered in a queued step on the page: from then on it keeps its place,
    # as a step whose timer ran there does.
    """
    ALTER TABLE steps ADD COLUMN held_at TEXT;
    """,
    # An agent's return no longer places a step as the user's take (SPEC RANK-18): the steps an agent returned keep
    # their places as ordinary ones, so only the user's own takes (Take, and Take back, a return by the user) lead.
    """
    UPDATE steps SET taken_at = NULL WHERE taken_at IS NOT NULL AND taken_at = queued_at AND EXISTS (
        SELECT 1 FROM events e WHERE e.id = (
            SELECT MAX(l.id) FROM events l WHERE l.step_id = steps.id AND l.kind IN ('step.take', 'step.return')
        ) AND e.kind = 'step.return' AND e.actor != 'user'
    );
    """,
    # Comments the user leaves on a shared page: on words or on a picked part of the page's `revision`. Unsent ones
    # wait for the user's answer to `step_id` (none: the Pages tab, where only Add as new item takes them);
    # `message_id` is the hand-off that carried one, `item_id` the item Add as new item made from it.
    """
    CREATE TABLE page_comments (
        id INTEGER PRIMARY KEY,
        page_id INTEGER NOT NULL REFERENCES item_pages(id) ON DELETE CASCADE,
        revision INTEGER NOT NULL,
        step_id INTEGER REFERENCES steps(id) ON DELETE SET NULL,
        anchor TEXT NOT NULL,
        text TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('unsent', 'sent', 'added')),
        message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
        item_id INTEGER REFERENCES items(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX page_comments_page ON page_comments(page_id, id);
    CREATE INDEX page_comments_step ON page_comments(step_id) WHERE state = 'unsent';
    CREATE INDEX page_comments_message ON page_comments(message_id);
    """,
    # Cockpit's own work is the items whose project is 'cockpit', which the page tags Cockpit. Puts store the other
    # spellings as it (store.COCKPIT_PROJECT_SPELLINGS); this moves the items written before, under the tool's old
    # name among them. It trims what Python's str.strip() does.
    """
    UPDATE items SET project = 'cockpit' WHERE lower(trim(project, char(9, 10, 11, 12, 13, 28, 29, 30, 31, 32, 133, 160,
        5760, 8192, 8193, 8194, 8195, 8196, 8197, 8198, 8199, 8200, 8201, 8202, 8232, 8233, 8239, 8287, 12288)))
        IN ('cockpit', 'desk');
    """,
    # The account the user prefers for new work keeps the length they chose, which the usage card marks. A choice
    # saved before gets the shortest length that still covers its time left.
    """
    UPDATE kv SET value = json_set(value, '$.minutes',
        CASE WHEN (julianday(json_extract(value, '$.until')) - julianday('now')) * 1440 <= 30 THEN 30
             WHEN (julianday(json_extract(value, '$.until')) - julianday('now')) * 1440 <= 120 THEN 120
             ELSE 1440 END)
    WHERE key = 'account_preference' AND json_type(value) = 'object' AND json_extract(value, '$.minutes') IS NULL;
    """,
    # When the user pressed Allow on the step's permission: the press is final, so the card shows it allowed in
    # place of the button until the step asks for another rule.
    """
    ALTER TABLE steps ADD COLUMN permission_allowed_at TEXT;
    """,
    # An early look: a session's early result or question while its work goes on, shown only while the user's queue
    # has room no other step wants.
    """
    ALTER TABLE steps ADD COLUMN early INTEGER NOT NULL DEFAULT 0 CHECK (early IN (0, 1));
    """,
    # Which of the page's controls the user presses, and which hover texts they stop on: one row per day, page,
    # control and kind ('press', 'hover', or 'seen' once a day the control was on the page), so rarely used controls
    # can move out of the way and unused ones go. A page sends a batch again until Cockpit takes it;
    # `control_batches` holds the batches taken in the last two days, so none counts twice.
    """
    CREATE TABLE control_uses (
        day TEXT NOT NULL,
        page TEXT NOT NULL,
        control TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('press', 'hover', 'seen')),
        count INTEGER NOT NULL,
        PRIMARY KEY (day, page, control, kind)
    );
    CREATE TABLE control_batches (id TEXT PRIMARY KEY, day TEXT NOT NULL);
    """,
    # Send, keep step: the user's message on a step that gives the session a head start while the step stays open
    # and in place in the user's queue. Its delivery tells the session to leave the step to the user.
    """
    ALTER TABLE messages ADD COLUMN keeps_step INTEGER NOT NULL DEFAULT 0 CHECK (keeps_step IN (0, 1));
    """,
    # What the user picked for a new session: on a message that starts one, its model, effort and account ({"model",
    # "effort", "account"}, null for the defaults); on a to-do the user added, the same with the provider, for the
    # session started for it; on a Codex thread, the model and effort its resumes keep.
    """
    ALTER TABLE messages ADD COLUMN new_options TEXT;
    ALTER TABLE items ADD COLUMN start_options TEXT;
    ALTER TABLE codex_threads ADD COLUMN model TEXT;
    ALTER TABLE codex_threads ADD COLUMN effort TEXT;
    """,
    """
    ALTER TABLE writing_examples ADD COLUMN step_version_id INTEGER REFERENCES step_versions(id) ON DELETE SET NULL;
    UPDATE writing_examples SET step_version_id = (
        SELECT MAX(v.id) FROM step_versions v WHERE v.step_id = writing_examples.step_id AND v.at <= writing_examples.created_at
        AND '# ' || v.ask
            || CASE WHEN v.why != '' THEN char(10, 10) || v.why ELSE '' END
            || CASE WHEN v.body != '' THEN char(10, 10) || v.body ELSE '' END
            || COALESCE((SELECT group_concat(char(10, 10) || '## ' || json_extract(d.value, '$.title') || char(10, 10)
                || json_extract(d.value, '$.body'), '') FROM (SELECT value FROM json_each(v.details) ORDER BY key) d), '')
            || CASE WHEN v.draft != '' THEN char(10, 10) || '## Draft' || char(10, 10) || v.draft ELSE '' END
            = writing_examples.text
    ) WHERE kind = 'step';
    """,
    # Send, Ask and Fork. Ask: a side chat's question and answer go to the session (`sent_message_id` now names that
    # message) and show above the box they were asked from, a step's (`step_id`) or the item's (null), placed among
    # the messages by `after_message_id`, the newest message when it was asked (for a failed ask, when it failed).
    # Fork: a message that starts a session forked from another (`fork_of`). Send, keep step (CONTROL-22) is gone
    # with its column.
    """
    ALTER TABLE side_chats ADD COLUMN step_id INTEGER REFERENCES steps(id) ON DELETE SET NULL;
    ALTER TABLE side_chats ADD COLUMN after_message_id INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE messages ADD COLUMN fork_of TEXT;
    ALTER TABLE messages DROP COLUMN keeps_step;
    """,
    # Cards that left the user's page within a minute of showing, without the user's press (SPEC PERM-24, flashes.py):
    # a request, waiting session or Codex question in Blocked and waiting, or a step in the user's queue. `ref` is
    # its id, `label` what the card said, `outcome` what took it away as Cockpit's records tell.
    """
    CREATE TABLE flashes (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('request', 'session', 'codex', 'step')),
        ref TEXT NOT NULL,
        label TEXT NOT NULL,
        page TEXT NOT NULL,
        shown_at TEXT NOT NULL,
        gone_at TEXT NOT NULL,
        outcome TEXT NOT NULL,
        recorded_at TEXT NOT NULL
    );
    """,
    # The tasks a session proposes the user closes with one press, each with why it is done: [{item_id, why}], or
    # null on any other step.
    """
    ALTER TABLE steps ADD COLUMN closes TEXT;
    """,
    # The page stops a timer itself once it sees no activity for three minutes, so "Forgot to pause the timer?" and
    # its "No, keep it", which `away_kept_until` held, are gone.
    """
    ALTER TABLE time_spans DROP COLUMN away_kept_until;
    """,
    """
    ALTER TABLE items ADD COLUMN doing TEXT NOT NULL DEFAULT '';
    """,
    # Fire and forget: a to-do the user added with Add's Fire and forget. Its session works to the end on its own,
    # asks only what blocks it, and closes the item itself with a note, which the user's next step names in one line.
    """
    ALTER TABLE items ADD COLUMN fire_and_forget INTEGER NOT NULL DEFAULT 0 CHECK (fire_and_forget IN (0, 1));
    """,
    # A step's list of tasks to close (SPEC CONTROL-23) is part of its text, so a rewrite of only a task or its why is
    # a new version. Each step's latest version takes the list the step holds now.
    """
    ALTER TABLE step_versions ADD COLUMN closes TEXT;
    UPDATE step_versions SET closes = (SELECT closes FROM steps WHERE steps.id = step_versions.step_id)
        WHERE id IN (SELECT MAX(id) FROM step_versions GROUP BY step_id);
    """,
    """
    CREATE TABLE page_choices (
        page_id INTEGER NOT NULL REFERENCES item_pages(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        chosen TEXT NOT NULL,
        label TEXT NOT NULL,
        step_id INTEGER REFERENCES steps(id) ON DELETE SET NULL,
        message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (page_id, name)
    );
    CREATE INDEX page_choices_step ON page_choices(step_id) WHERE message_id IS NULL;
    CREATE INDEX page_choices_message ON page_choices(message_id);
    ALTER TABLE messages ADD COLUMN page_answers TEXT;
    """,
    """
    ALTER TABLE steps ADD COLUMN reply_hint TEXT NOT NULL DEFAULT '';
    DELETE FROM summaries WHERE ref LIKE 'story:%' AND CAST(substr(ref, 7, instr(substr(ref, 7), ':') - 1) AS INTEGER)
        IN (SELECT item_id FROM steps WHERE status = 'open' AND withdrawn_at IS NULL);
    """,
    """
    CREATE TABLE projects (
        name TEXT PRIMARY KEY COLLATE NOCASE,
        about TEXT NOT NULL DEFAULT '',
        logo TEXT,
        logo_at TEXT,
        aliases TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        updated_by TEXT NOT NULL
    );
    INSERT OR IGNORE INTO projects (name, created_at, updated_at, updated_by)
        SELECT trim(project), min(created_at), min(created_at), 'cockpit' FROM items WHERE trim(project) != ''
        GROUP BY trim(project) ORDER BY count(*) DESC;
    INSERT OR IGNORE INTO projects (name, created_at, updated_at, updated_by)
        VALUES ('cockpit', strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), 'cockpit');
    UPDATE items SET project = (SELECT name FROM projects WHERE name = trim(items.project)) WHERE trim(project) != '';
    """,
    # The user's own messages show in their own words; Claude no longer summarises them.
    "DELETE FROM summaries WHERE ref LIKE 'message:%'",
    """
    DELETE FROM kv WHERE key IN ('ideas', 'ideas_set_aside');
    """,
    # Changes reaching staging and prod wake their item's session (SPEC STATE-6, stages.py): `stage_arrivals` holds
    # each (item, stage) Cockpit announced, with the pull requests it named and its message; `pull_stages` the pull
    # requests found on prod, which stay there, so GitHub is not asked about them again.
    """
    CREATE TABLE stage_arrivals (
        item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        stage TEXT NOT NULL CHECK (stage IN ('staging', 'prod')),
        pulls TEXT NOT NULL,
        message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
        noticed_at TEXT NOT NULL,
        PRIMARY KEY (item_id, stage)
    );
    CREATE TABLE pull_stages (
        ref TEXT PRIMARY KEY,
        stage TEXT NOT NULL CHECK (stage = 'prod'),
        checked_at TEXT NOT NULL
    );
    """,
    # A step's reply hint (NEXT-51) is part of its text, so a rewrite of only the hint is a new version and a verdict
    # on the earlier text stays with that text. Each step's latest version takes the hint the step holds now.
    """
    ALTER TABLE step_versions ADD COLUMN reply_hint TEXT NOT NULL DEFAULT '';
    UPDATE step_versions SET reply_hint = (SELECT reply_hint FROM steps WHERE steps.id = step_versions.step_id)
        WHERE id IN (SELECT MAX(id) FROM step_versions GROUP BY step_id);
    """,
    # Every wait on someone other than an agent carries when to look again, and may carry a check Cockpit runs
    # (SPEC STATE-8, wakes.py); the scan of every item's pull requests goes. `wakes` holds, per item and wait (the
    # wait's JSON), the check's failures in a row and when Cockpit last woke the item's session for it.
    """
    DROP TABLE stage_arrivals;
    DROP TABLE pull_stages;
    CREATE TABLE wakes (
        item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        wait TEXT NOT NULL,
        failures INTEGER NOT NULL DEFAULT 0,
        error TEXT NOT NULL DEFAULT '',
        woke_at TEXT,
        PRIMARY KEY (item_id, wait)
    );
    """,
    # When and by whom an archived item was archived (SPEC VIEW-20): `updated_at` and `updated_by` move with every later
    # put, such as an agent's summary. Stored items take it from the newest close or archive on record, the user's or an
    # agent's, or else their last update. An older edit of `archived` can lack the value; the item is
    # archived now, so the newest record of an archive is the one that holds.
    """
    ALTER TABLE items ADD COLUMN archived_at TEXT;
    ALTER TABLE items ADD COLUMN archived_by TEXT;
    CREATE TEMP TABLE item_closes AS
        SELECT e.item_id, e.at, e.actor AS who FROM events e WHERE e.kind = 'item.closed'
            OR (e.kind = 'item.edited' AND (json_extract(e.data, '$.archived') = 1
                OR (json_extract(e.data, '$.archived') IS NULL
                    AND EXISTS (SELECT 1 FROM json_each(e.data, '$.fields') WHERE value = 'archived'))))
        UNION ALL
        SELECT c.ref_id, c.closed_at, c.closed_by FROM closures c WHERE c.kind = 'item' AND c.action = 'archived';
    UPDATE items SET
        archived_at = COALESCE(
            (SELECT c.at FROM item_closes c WHERE c.item_id = items.id ORDER BY c.at DESC LIMIT 1), updated_at),
        archived_by = COALESCE(
            (SELECT c.who FROM item_closes c WHERE c.item_id = items.id ORDER BY c.at DESC LIMIT 1), updated_by)
        WHERE archived = 1;
    DROP TABLE item_closes;
    """,
    """
    ALTER TABLE command_runs ADD COLUMN process_id INTEGER;
    """,
    """
    CREATE TABLE top_cards (
        id INTEGER PRIMARY KEY,
        need TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        body TEXT NOT NULL DEFAULT '',
        answers TEXT NOT NULL DEFAULT '[]',
        ends_at TEXT,
        session_id TEXT,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        done_at TEXT,
        done_by TEXT,
        answer TEXT
    );
    """,
    # A public page's link (SPEC API-5). public_token is the secret in it, null until the page is first made public.
    # Stopping sharing clears only `public`, so sharing again brings back the same link; New link replaces the token.
    """
    ALTER TABLE item_pages ADD COLUMN public_token TEXT;
    ALTER TABLE item_pages ADD COLUMN public INTEGER NOT NULL DEFAULT 0 CHECK (public IN (0, 1));
    CREATE UNIQUE INDEX item_pages_public_token ON item_pages(public_token) WHERE public_token IS NOT NULL;
    """,
    # Who can open a page at its link (SPEC API-7, API-8), in place of public's yes or no: only the user ('me'), the
    # people listed in item_page_people once they sign in with Google ('people'), or anyone with the link ('anyone').
    # The list stays while the page is shared otherwise, so choosing Specific people again brings it back.
    """
    ALTER TABLE item_pages ADD COLUMN access TEXT NOT NULL DEFAULT 'me' CHECK (access IN ('me', 'people', 'anyone'));
    UPDATE item_pages SET access = 'anyone' WHERE public = 1;
    ALTER TABLE item_pages DROP COLUMN public;
    CREATE TABLE item_page_people (
        page_id INTEGER NOT NULL REFERENCES item_pages(id) ON DELETE CASCADE,
        email TEXT NOT NULL,
        added_at TEXT NOT NULL,
        PRIMARY KEY (page_id, email)
    );
    """,    """
    ALTER TABLE top_cards ADD COLUMN seen_at TEXT;
    """,
    # Scheduled prompts (SPEC BG-14, schedules.py). weekday is 0 for Monday, null for a daily one; `at` is HH:MM
    # in the user's zone. last_slot is the UTC time of the latest slot that ran or was skipped, written in the same
    # transaction as its run's message, so no slot starts twice.
    """
    CREATE TABLE schedules (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        prompt TEXT NOT NULL,
        cwd TEXT NOT NULL,
        every TEXT NOT NULL CHECK (every IN ('daily', 'weekly')),
        weekday INTEGER CHECK (weekday BETWEEN 0 AND 6),
        at TEXT NOT NULL,
        title TEXT NOT NULL,
        project TEXT NOT NULL,
        author TEXT NOT NULL,
        paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_slot TEXT NOT NULL,
        last_message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
        last_item_id INTEGER REFERENCES items(id) ON DELETE SET NULL,
        error TEXT,
        CHECK ((every = 'daily') = (weekday IS NULL))
    );
    """,
    # An agent merges a side item into the item it belongs with (SPEC NEXT-55): the side item's steps, messages and
    # attachments move over, and the side item is archived with `merged_into`. `steps.merged_from` keeps the item each
    # moved step came from, and `item_merges` keeps the merge itself for the page's merge banner and the history.
    # `held_changes` is rebuilt for its new kind, a merge that waits while the user's timer runs on either item.
    """
    ALTER TABLE items ADD COLUMN merged_into INTEGER;
    ALTER TABLE steps ADD COLUMN merged_from INTEGER;
    CREATE TABLE item_merges (
        id INTEGER PRIMARY KEY,
        item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        from_item_id INTEGER NOT NULL,
        from_key TEXT NOT NULL,
        from_title TEXT NOT NULL,
        moved_steps TEXT NOT NULL DEFAULT '[]',
        note TEXT NOT NULL DEFAULT '',
        actor TEXT NOT NULL,
        at TEXT NOT NULL
    );
    CREATE INDEX item_merges_item ON item_merges(item_id, at);
    CREATE TABLE held_changes_new (
        id INTEGER PRIMARY KEY,
        step_id INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK (kind IN ('step_put', 'step_action', 'item_put', 'item_merge')),
        actor TEXT NOT NULL,
        data TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    INSERT INTO held_changes_new SELECT id, step_id, kind, actor, data, created_at FROM held_changes;
    DROP TABLE held_changes;
    ALTER TABLE held_changes_new RENAME TO held_changes;
    """,
    # A Claude session Cockpit started on the default model records that model, so the wrong-model warning keeps
    # judging it by the model it started on after the default changes. Until now the default was Opus 5.5.
    """
    UPDATE messages SET new_options = json_set(
        COALESCE(new_options, json_object('model', NULL, 'effort', NULL, 'account', NULL)), '$.model', 'claude-opus-5-5'
    )
    WHERE via = 'new' AND session_id IS NOT NULL AND json_extract(new_options, '$.model') IS NULL
        AND session_id NOT IN (SELECT session_id FROM codex_threads);
    """,
    # An agent's rewrite of a step the user's timer ran on before (SPEC NEXT-61), with the agent's one line on what
    # changed, or "": the page shows it as a band on the card (store.step_changes says when).
    """
    CREATE TABLE step_changes (
        id INTEGER PRIMARY KEY,
        step_id INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
        note TEXT NOT NULL,
        actor TEXT NOT NULL,
        at TEXT NOT NULL
    );
    CREATE INDEX step_changes_step ON step_changes(step_id, at);
    """,
    # A step's core links: up to three [{label, url}] the step is about, shown as chips above its answers. They are part
    # of the step's text, so its versions keep them too.
    """
    ALTER TABLE steps ADD COLUMN core_links TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE step_versions ADD COLUMN core_links TEXT NOT NULL DEFAULT '[]';
    """,
    # Work that repeats, such as a scheduled review (SPEC BG-3): the sidebar lists it under Background. Every run a
    # scheduled prompt started is such work.
    """
    ALTER TABLE items ADD COLUMN recurring INTEGER NOT NULL DEFAULT 0 CHECK (recurring IN (0, 1));
    UPDATE items SET recurring = 1 WHERE key LIKE 'note:schedule-%';
    """,
    # A step that is useful only until a time (SPEC NEXT-60), such as a sign-in link that stops working: the time,
    # and the time Cockpit told the step's session that it passed with the step still open.
    """
    ALTER TABLE steps ADD COLUMN expires_at TEXT;
    ALTER TABLE steps ADD COLUMN expiry_told_at TEXT;
    """,
    # Since when a message waits, or waited before it went, for a place among the sessions that work at once
    # (store.WORKING_LIMIT, SPEC SESSION-23). A queued message never has this and `waits_since` together.
    """
    ALTER TABLE messages ADD COLUMN capacity_since TEXT;
    """,
    # What Cockpit can be studied by (SPEC TRACK-12 to TRACK-14). `log_counts`: each kind of line the server logged at
    # warning or above, counted per day (server_logs). `request_stats`: how each route answered whom, per day and status
    # (request_stats). `page_presses`: each press on the user's page, when and on which task (control_uses).
    """
    CREATE TABLE log_counts (
        day TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        level TEXT NOT NULL,
        logger TEXT NOT NULL,
        message TEXT NOT NULL,
        count INTEGER NOT NULL,
        last_at TEXT NOT NULL,
        PRIMARY KEY (day, fingerprint)
    );
    CREATE TABLE request_stats (
        day TEXT NOT NULL,
        method TEXT NOT NULL,
        route TEXT NOT NULL,
        status INTEGER NOT NULL,
        caller TEXT NOT NULL,
        count INTEGER NOT NULL,
        total_ms INTEGER NOT NULL,
        max_ms INTEGER NOT NULL,
        slow INTEGER NOT NULL,
        PRIMARY KEY (day, method, route, status, caller)
    );
    CREATE TABLE page_presses (
        id INTEGER PRIMARY KEY,
        at TEXT NOT NULL,
        page TEXT NOT NULL,
        control TEXT NOT NULL,
        item_id INTEGER,
        step_id INTEGER
    );
    CREATE INDEX page_presses_at ON page_presses(at);
    """,
    # A Codex thread keeps its selected speed when Cockpit reconnects to it.
    """
    ALTER TABLE codex_threads ADD COLUMN speed TEXT NOT NULL DEFAULT 'standard'
        CHECK (speed IN ('standard', 'fast', 'ultrafast'));
    """,
    # How a session finds a picked part again (SPEC FEEDBACK-7): its tag, id and classes, a CSS path, the framed
    # page, data and ARIA attributes, its place and size in the window and the theme; "" for feedback on no part.
    """
    ALTER TABLE feedback ADD COLUMN detail TEXT NOT NULL DEFAULT '';
    """,
    # How much processor time and memory each task and part used (resources.py, SPEC TRACK-16): ten-minute totals per
    # session and part, the host's own readings, and the daily totals older rows fold into. Buckets are epoch seconds.
    """
    CREATE TABLE resource_use (
        bucket INTEGER NOT NULL,
        session_id TEXT NOT NULL,
        part TEXT NOT NULL,
        cpu_s REAL NOT NULL,
        idle_cpu_s REAL NOT NULL,
        mem_mb_s REAL NOT NULL,
        idle_mem_mb_s REAL NOT NULL,
        swap_mb_s REAL NOT NULL,
        session_peak_mb REAL NOT NULL,
        part_peak_mb REAL NOT NULL,
        programs INTEGER NOT NULL,
        PRIMARY KEY (bucket, session_id, part)
    );
    CREATE TABLE resource_host (
        bucket INTEGER PRIMARY KEY,
        sampled_s REAL NOT NULL,
        busy_s REAL NOT NULL,
        steal_s REAL NOT NULL,
        load_s REAL NOT NULL,
        avail_mb_s REAL NOT NULL,
        swap_mb_s REAL NOT NULL,
        cpu_wait_s REAL NOT NULL,
        mem_wait_s REAL NOT NULL
    );
    CREATE TABLE resource_days (
        day TEXT NOT NULL,
        session_id TEXT NOT NULL,
        part TEXT NOT NULL,
        cpu_s REAL NOT NULL,
        idle_cpu_s REAL NOT NULL,
        mem_mb_s REAL NOT NULL,
        idle_mem_mb_s REAL NOT NULL,
        swap_mb_s REAL NOT NULL,
        session_peak_mb REAL NOT NULL,
        part_peak_mb REAL NOT NULL,
        programs INTEGER NOT NULL,
        PRIMARY KEY (day, session_id, part)
    );
    CREATE TABLE resource_host_days (
        day TEXT PRIMARY KEY,
        sampled_s REAL NOT NULL,
        busy_s REAL NOT NULL,
        steal_s REAL NOT NULL,
        load_s REAL NOT NULL,
        avail_mb_s REAL NOT NULL,
        swap_mb_s REAL NOT NULL,
        cpu_wait_s REAL NOT NULL,
        mem_wait_s REAL NOT NULL
    );
    """,
    # The images the user sent with a message (SPEC MSG-18), as ids of images on the message's task; the delivered
    # text names them so the session can read them.
    """
    ALTER TABLE messages ADD COLUMN images TEXT NOT NULL DEFAULT '[]';
    """,
    # What the user's Mac records each minute (SPEC TRACK-9, activity.py): the app in front, a browser's site (host
    # only, or NULL) and whether the user was away. Minutes past 30 days fold into daily totals, `site` '' for none.
    # The user's own category per app or site (SPEC TRACK-11) beats the defaults in code.
    """
    CREATE TABLE activity_minutes (minute TEXT PRIMARY KEY, app TEXT NOT NULL, site TEXT, away INTEGER NOT NULL);
    CREATE TABLE activity_days (
        day TEXT NOT NULL,
        app TEXT NOT NULL,
        site TEXT NOT NULL,
        away INTEGER NOT NULL,
        minutes INTEGER NOT NULL,
        PRIMARY KEY (day, app, site, away)
    );
    CREATE TABLE activity_categories (name TEXT PRIMARY KEY, category TEXT NOT NULL);
    """,
    # Turns that ended on an API error and Cockpit's one resend for each (SPEC SWEEP-30, error_stops.py). `outcome` is
    # '' while the resend waits for its answer, then fixed, failed or other (a usage-limit or sign-out stop took
    # over); a stop that followed a failed resend is kept as its repeat, which gets no message. `item_id` is the
    # incident item a failure went on.
    """
    CREATE TABLE error_stops (
        id INTEGER PRIMARY KEY,
        session_id TEXT NOT NULL,
        name TEXT NOT NULL,
        account TEXT NOT NULL,
        model TEXT NOT NULL,
        stopped_at TEXT NOT NULL,
        kind TEXT NOT NULL,
        error TEXT NOT NULL,
        message_id INTEGER REFERENCES messages(id),
        outcome TEXT NOT NULL DEFAULT '',
        tried TEXT NOT NULL DEFAULT '',
        settled_at TEXT,
        item_id INTEGER REFERENCES items(id),
        UNIQUE (session_id, stopped_at)
    );
    """,
    # Run on a step with no session starts a new one (SPEC NEXT-18): the run's session is null until the start
    # message names it.
    """
    CREATE TABLE command_runs_new (
        id INTEGER PRIMARY KEY,
        step_id INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
        session_id TEXT,
        command_text TEXT NOT NULL,
        cwd TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        exit_code INTEGER,
        output TEXT NOT NULL DEFAULT '',
        result_message_id INTEGER REFERENCES messages(id),
        process_id INTEGER
    );
    INSERT INTO command_runs_new SELECT id, step_id, session_id, command_text, cwd, started_at, finished_at, exit_code,
        output, result_message_id, process_id FROM command_runs;
    DROP TABLE command_runs;
    ALTER TABLE command_runs_new RENAME TO command_runs;
    CREATE INDEX command_runs_step ON command_runs(step_id, id);
    """,
    # Empty: it keeps the numbering of the databases where this migration created a part's table, which the parts now
    # create themselves (parts.create_tables).
    "SELECT 1",
    # Empty: it keeps the numbering of the databases where this migration added a part's column to items, which the
    # part now keeps in a table of its own and removes from items (parts.create_tables).
    "SELECT 1",
    # A project's own checkout, where a new session for its items starts (SPEC SESSION-24), or null.
    """
    ALTER TABLE projects ADD COLUMN checkout TEXT;
    """,
    # Empty: it keeps the numbering of the databases where this migration created a part's table, which the parts now
    # create themselves (parts.create_tables).
    "SELECT 1",
    # A connector sign-in Cockpit runs for the step (SPEC SESSION-25): what its card shows, as JSON, or null.
    """
    ALTER TABLE steps ADD COLUMN signin TEXT NOT NULL DEFAULT 'null';
    """,
    # The state of each pull request the open items link to (SPEC NEXT-66, github.py), as the page draws its links:
    # `ref` is `owner/repo#number`; a merged PR is not asked again.
    """
    CREATE TABLE pull_states (
        ref TEXT PRIMARY KEY,
        state TEXT NOT NULL CHECK (state IN ('open', 'draft', 'merged', 'closed')),
        checked_at TEXT NOT NULL
    );
    """,
]

JSON_COLUMNS = frozenset(
    {
        "waiting_on", "sessions", "links", "user_fields", "details", "reasons", "data", "tool_input", "answers",
        "command", "permission", "once_rules", "asked_by", "earlier_call", "explanation", "before_fields", "closed_steps",
        "usage", "skills", "anchor", "new_options", "start_options", "closes", "chosen", "moved_steps", "core_links", "images",
        "signin",
    }
)


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso_ms() -> str:
    """Now to the millisecond, for times that order events within one second."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _private_file(path: Path) -> None:
    """Create privately before SQLite opens the file, and tighten an existing file in place.

    An existing file is tightened by path and never opened here. Closing any descriptor on the database drops every
    lock this process holds on it (POSIX locks are per process), including those of its other open connections.
    Another process's connection that then closes thinks it is the last one: it checkpoints and deletes the
    write-ahead log under them, and a write is lost or SQLite reports "disk I/O error" and "malformed".
    """
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        os.chmod(path, 0o600)
        return
    try:
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def connect(path: Path) -> sqlite3.Connection:
    """One connection per request or worker, in autocommit mode.

    Multi-statement writes use `with transaction(conn)`; no transaction is held
    across a subprocess or network call. A write that waits longer than the busy
    timeout raises `sqlite3.OperationalError` and the request fails with a 503.
    The timeout is 30 s, so short writes still land when the host is swapping
    under heavy load.
    FastAPI opens, uses and closes a request's connection on different
    threadpool threads (one at a time), so the same-thread check is off.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    _private_file(path)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
    except BaseException:
        conn.close()
        raise
    return conn


def locked(exc: BaseException) -> bool:
    """Whether `exc` is a write that waited out the busy timeout (connect)."""
    return isinstance(exc, sqlite3.OperationalError) and str(exc) == "database is locked"


@contextmanager
def transaction(conn: sqlite3.Connection) -> Generator[None]:
    """One transaction; inside another it is a savepoint, so a failure undoes its own writes and the caller's stay."""
    if conn.in_transaction:
        conn.execute("SAVEPOINT nested")
        try:
            yield
        except BaseException:
            conn.execute("ROLLBACK TO nested")
            conn.execute("RELEASE nested")
            raise
        conn.execute("RELEASE nested")
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


class MigrationError(Exception):
    """A migration left a reference pointing at a row that does not exist; it was rolled back."""


CODEX_AHEAD = 41
CODEX_MIGRATION = 47


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def migrate(conn: sqlite3.Connection) -> None:
    """Run the pending migrations, then create the tables of this install's parts that are missing."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    applied = set()
    if CODEX_AHEAD <= version < CODEX_MIGRATION and _has_table(conn, "codex_threads"):
        applied = {CODEX_MIGRATION}
        if not _has_table(conn, "item_finishes"):
            version = CODEX_AHEAD - 1
    pending = list(enumerate(MIGRATIONS[version:], start=version + 1))
    if pending:
        _run(conn, pending, applied)
    # The tables of this install's parts, after Cockpit's own, which they may refer to.
    parts.create_tables(conn)


def _run(conn: sqlite3.Connection, pending: list[tuple[int, str]], applied: set[int]) -> None:
    """Run the `pending` migrations by number, each in its own transaction, those in `applied` as no-ops.

    Foreign keys are off while they run, because a migration that rebuilds a
    table drops it, and with foreign keys on that drop cascades into the tables
    that reference it. `PRAGMA foreign_keys` has no effect inside a transaction,
    so it is switched around the migrations, and `PRAGMA foreign_key_check`
    stands in for it before each commit.
    """
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        for number, script in pending:
            if number in applied:
                script = "SELECT 1"
            try:
                conn.executescript(f"BEGIN; {script}; PRAGMA user_version = {number};")
                broken = conn.execute("PRAGMA foreign_key_check").fetchall()
                if broken:
                    raise MigrationError(
                        f"migration {number} breaks references (table, rowid, parent): "
                        + ", ".join(f"({record[0]}, {record[1]}, {record[2]})" for record in broken[:10])
                    )
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def row(record: sqlite3.Row | None) -> dict[str, Any] | None:
    if record is None:
        return None
    columns = record.keys()
    # Named SQLite row lookups scan its columns. Copy by position when the names are distinct; duplicate names
    # retain SQLite's first-column lookup, including its case-insensitive ASCII matching.
    result = dict(zip(columns, record)) if len({name.lower() for name in columns}) == len(columns) else dict(record)
    return _decoded_row(result, JSON_COLUMNS & result.keys())


def _decoded_row(result: dict[str, Any], columns: frozenset[str]) -> dict[str, Any]:
    for column in columns:
        value = result[column]
        if value is None:
            continue
        if value == "[]":
            result[column] = []
        elif value == "null":
            result[column] = None
        else:
            result[column] = json.loads(value)
    return result


def rows(records: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    result = []
    columns: list[str] | None = None
    distinct = False
    json_columns: frozenset[str] = frozenset()
    for record in records:
        if columns is None:
            columns = record.keys()
            distinct = len({name.lower() for name in columns}) == len(columns)
            json_columns = JSON_COLUMNS.intersection(columns)
        values = (_decoded_row(dict(zip(columns, record)), json_columns) if distinct and record.keys() == columns
                  else _decoded_row(dict(record), JSON_COLUMNS.intersection(record.keys())))
        result.append(values)
    return result


def kv_get(conn: sqlite3.Connection, key: str) -> Any:
    record = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return None if record is None else json.loads(record["value"])


def kv_set(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )


def backup(conn: sqlite3.Connection, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    _private_file(target)
    destination = sqlite3.connect(target)
    try:
        conn.backup(destination)
    finally:
        destination.close()
