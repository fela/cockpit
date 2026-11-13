"""The demo's invented work: a person called Sam, three projects, and the items, steps, sessions and transcripts a
week of their work leaves. Nothing here comes from a real board.

build.py calls `seed` with the clock already patched (`Clock`), so each record is written at the time it says.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cockpit import briefing, db, projects, store

# Session UUIDs from fixed names, so a rebuild gives the same ones.
NS = uuid.UUID("5d0c0c3e-8a51-4d6c-9a70-1f0d0b2d7c11")


def session_id(name: str) -> str:
    return str(uuid.uuid5(NS, name))


# The invented sessions: name, folder under ~/dev, state, status, tempo, item key.
SESSIONS = [
    ("list sharing", "tasks-app", "done", "idle", "idle"),
    ("reminder default", "tasks-app", "done", "idle", "idle"),
    ("pricing page", "website", "done", "idle", "idle"),
    ("sync on trains", "tasks-app", "done", "idle", "idle"),
    ("dark mode", "tasks-app", "working", "busy", "active"),
    ("release notes", "tasks-app", "done", "idle", "idle"),
    ("staging migration", "tasks-app", "done", "idle", "idle"),
    ("csv export", "tasks-app", "done", "idle", "idle"),
    ("onboarding copy", "website", "done", "idle", "idle"),
    ("flaky sync tests", "tasks-app", "stopped", "", ""),
    ("driving licence", "", "done", "idle", "idle"),
]

LOGO = {
    "tasks": (
        # A clipboard: a tick in a square would read as a checkbox beside each task.
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><rect x="4.5" y="4" width="15" height="18.5" rx="2.5"'
        ' fill="#4f7cff"/><rect x="8.5" y="2" width="7" height="4.5" rx="1.5" fill="#2b4fc4"/><path d="M8.5 11.5h7M8.5 15h7'
        'M8.5 18.5h4" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/></svg>'
    ),
    "website": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" fill="#1fa37a"/>'
        '<path d="M3.5 12h17M12 3c3 3.2 3 14.8 0 18M12 3c-3 3.2-3 14.8 0 18" fill="none" stroke="#fff"'
        ' stroke-width="1.6"/></svg>'
    ),
    "home": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6h-6v6H4'
        'a1 1 0 0 1-1-1z" fill="#e8913a"/></svg>'
    ),
    "cockpit": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="5"'
        ' fill="#7a5af5"/><path d="M7 15a5 5 0 0 1 10 0" fill="none" stroke="#fff" stroke-width="2"'
        ' stroke-linecap="round"/><path d="M12 15l3-4" stroke="#fff" stroke-width="2" stroke-linecap="round"/></svg>'
    ),
}

# Every prepared reply says so, so nobody mistakes the demo for a session at work.
DEMO_NOTE = "\n\n*Demo: this reply was written in advance; no agent ran.*"
# Where the build puts the visitor's words: their answer, or the text they added.
ANSWER = "QQANSWERQQ"


def follow_ups(now: datetime) -> list[dict[str, Any]]:
    """What a session would do with each answer, prepared for the demo to play back.

    Each entry names the item and the need of the step the visitor answers, the answers it is for (`match`, lower
    case; None for any), what the session works on meanwhile (`doing`), the item's fields after, and the step the
    session puts next. An answer no entry matches gets the plain reply (`GENERIC`).
    """
    slot = [(now + timedelta(days=days)).strftime("%a %-d %b") for days in (11, 13, 17)]
    return [
        {"key": "gh:example-org/tasks-app#41", "need": "merge", "match": None, "doing": "Merging list sharing",
         "item": {"stage": "staging", "summary": "Merged; on staging, reaches the app with Thursday's release"},
         "step": {"need": "read:merged", "ask": "List sharing is merged and on staging",
                  "body": "I merged PR #41. On staging, two test accounts shared a list and both could add and tick"
                          " tasks.\n\n**Next:** it reaches the app with Thursday's release; I tell you when it is"
                          " live." + DEMO_NOTE,
                  "answers": [{"text": "Thanks, keep me posted", "closes": False}], "size": "small", "progress": 95}},
        {"key": "note:reminder-default", "need": "decide:default-time",
         "match": ["same time tomorrow", "9:00 the next morning", "ask once, then remember"],
         "doing": "Building the default reminder time",
         "item": {"stage": "review", "summary": "Built; Sam tries it"},
         "step": {"need": "review:default-time", "ask": "The default reminder time is built; try it?",
                  "body": f"New reminders now default to **{ANSWER}**, as you picked. Settings → Reminders can"
                          " change it later.\n\nAll 220 tests pass, 6 of them new for the default." + DEMO_NOTE,
                  "answers": [{"text": "Looks good, ship it", "closes": False}, {"text": "Change it", "closes": False}],
                  "size": "small", "progress": 60}},
        {"key": "note:pricing-page", "need": "decide:layout", "match": None, "doing": "Building the picked layout",
         "item": {"stage": "review", "summary": "The picked layout is on the preview site"},
         "step": {"need": "read:layout-built", "ask": "The pricing layout you picked is on the preview site",
                  "body": "I removed the other layout and finished yours: plan names, prices and the phone view."
                          "\n\n**Next:** Alex reviews the copy, then it goes live." + DEMO_NOTE,
                  "answers": [{"text": "Send it to Alex", "closes": False}], "size": "small", "progress": 70}},
        {"key": "note:sync-slow", "need": "read:cause", "match": ["build the batching"],
         "doing": "Building the sync batching",
         "item": {"stage": "review", "summary": "Batching built; 30 changes sync in about a second"},
         "step": {"need": "review:batching", "ask": "Sync batching is built; merge it?",
                  "body": "Queued changes now go together, up to 50 per request. On the recorded train journey, 30"
                          " changes reach the server in 1.1 seconds instead of 12.4.\n\nPriya reviewed it; the tests"
                          " pass." + DEMO_NOTE,
                  "answers": [{"text": "Merge it", "closes": False}], "size": "small", "progress": 80}},
        {"key": "note:sync-slow", "need": "read:cause", "match": ["ask priya first"], "doing": "Asking Priya",
         "item": {"summary": "Priya agrees with batching, under 50 changes a request"},
         "step": {"need": "read:priya", "ask": "Priya agrees: batch the changes, at most 50 at a time",
                  "body": "I asked Priya. She agrees, with one limit: under 50 changes per request, so the server's"
                          " memory stays flat.\n\n**Next:** I build it with that limit, about a day of work."
                          + DEMO_NOTE,
                  "answers": [{"text": "Build it", "closes": False}], "size": "small", "progress": 20}},
        {"key": "note:driving-licence", "need": "define", "match": ["find me an appointment slot"],
         "doing": "Looking for appointment slots",
         "item": {"summary": "Three appointment slots found; Sam picks one"},
         "step": {"need": "decide:slot", "ask": "Which licence office appointment should I book?",
                  "body": "Three free slots at the licence office:\n\n| Slot | Where |\n| --- | --- |\n"
                          f"| {slot[0]}, 9:40 | City centre |\n| {slot[1]}, 14:15 | City centre |\n"
                          f"| {slot[2]}, 11:00 | North branch |\n\nBring the old licence and two photos."
                          + DEMO_NOTE,
                  "answers": [{"text": f"{slot[0]}, 9:40", "closes": False},
                              {"text": f"{slot[1]}, 14:15", "closes": False},
                              {"text": "I'll book it myself; close", "closes": True}],
                  "size": "small", "progress": 30}},
        {"key": "note:release-notes", "need": "post:notes", "match": None, "doing": "Posting the release notes",
         "item": {"summary": "Posted in #announcements and the changelog"},
         "step": {"need": "read:posted", "ask": "The 2.4 release notes are posted",
                  "body": "I posted your notes in #announcements and on the website's changelog. Nothing else is"
                          " left on this task." + DEMO_NOTE,
                  "size": "small", "progress": 95}},
    ]


# The reply for any other answer: what a session would do, said plainly.
GENERIC = {
    "doing": "Reading your answer",
    "step": {"need": "read:answer", "ask": "Your answer reached the session",
             "body": f"You wrote: “{ANSWER}”\n\nIn real use, the session reads it, does the work and comes back here"
                     " with the result or its next question. This demo has no session, so this one ends here."
                     + DEMO_NOTE,
             "size": "small", "progress": 50},
}

# After Run: the command's result, then the session's note.
RUN = {
    "output": "Operations to perform:\n  Apply all migrations: shares\nRunning migrations:\n  Applying 0042_shares... OK\n",
    "doing": "Running the migration on staging",
    "item": {"summary": "Staging migrated; list sharing works there"},
    "step": {"need": "read:migrated", "ask": "The shares table is on staging",
             "body": "The migration ran: `Applying 0042_shares... OK`. Staging has the table now, and sharing a list"
                     " works there." + DEMO_NOTE,
             "size": "small", "progress": 95},
}

# A to-do the visitor adds: the question its session would ask first.
ADDED = {
    "doing": "Reading your new to-do",
    "step": {"need": "define", "ask": "What counts as done for this to-do?",
             "body": f"You added: “{ANSWER}”\n\nIn real use, a session starts on it at once, reads your words and"
                     " asks what it needs, like this." + DEMO_NOTE,
             "answers": [{"text": "Do it and tell me when it's done", "closes": False},
                         {"text": "I'll do it myself; close", "closes": True}],
             "size": "small", "progress": 0},
}

PRICING_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { color-scheme: light dark; --ink: #1d2433; --soft: #5b6475; --line: #d9dde5; --card: #fff; --accent: #4f7cff; }
  @media (prefers-color-scheme: dark) { :root { --ink: #e8ebf2; --soft: #a3abbb; --line: #333a48; --card: #1b2029; } }
  body { font: 15px/1.5 system-ui, sans-serif; color: var(--ink); margin: 0; padding: 16px; background: transparent; }
  h2 { font-size: 16px; margin: 0 0 4px; }
  p { margin: 0 0 12px; color: var(--soft); }
  .options { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }
  label { display: block; border: 1px solid var(--line); border-radius: 10px; padding: 12px; background: var(--card);
          cursor: pointer; }
  label:has(input:checked) { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
  .mock { display: flex; gap: 6px; margin: 10px 0; height: 72px; align-items: flex-end; }
  .mock div { flex: 1; border-radius: 6px; background: color-mix(in srgb, var(--accent) 25%, transparent); }
  .mock .hi { background: var(--accent); }
  small { color: var(--soft); }
</style></head>
<body>
<h2>Two layouts for the pricing page</h2>
<p>Both use the same three plans and prices. Pick the one to ship; the session builds it next.</p>
<div class="options">
  <label><input type="radio" name="layout" value="columns" data-label="Three columns"> <b>Three columns</b>
    <div class="mock"><div style="height:70%"></div><div class="hi" style="height:100%"></div><div style="height:70%"></div></div>
    <small>The middle plan stands out. Easy to compare at a glance on a laptop.</small></label>
  <label><input type="radio" name="layout" value="slider" data-label="One card with a slider"> <b>One card with a slider</b>
    <div class="mock"><div class="hi" style="height:100%"></div></div>
    <small>Pick a team size and the card shows the price. Better on phones; one more click to compare.</small></label>
</div>
<p style="margin-top:12px"><label style="display:inline;border:0;padding:0;background:none">
  <input type="checkbox" name="yearly" value="yes" data-label="Show yearly prices first"> Show yearly prices first</label></p>
</body></html>
"""

SYNC_REPORT = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { color-scheme: light dark; --ink: #1d2433; --soft: #5b6475; --line: #d9dde5; --bar: #4f7cff; --old: #c2c8d4; }
  @media (prefers-color-scheme: dark) { :root { --ink: #e8ebf2; --soft: #a3abbb; --line: #333a48; --old: #4a5263; } }
  body { font: 15px/1.55 system-ui, sans-serif; color: var(--ink); max-width: 680px; margin: 0 auto; padding: 20px 16px; }
  h1 { font-size: 22px; margin: 0 0 6px; } h2 { font-size: 16px; margin: 24px 0 6px; }
  p, li { color: var(--ink); } .soft { color: var(--soft); }
  .row { display: grid; grid-template-columns: 120px 1fr 64px; gap: 8px; align-items: center; margin: 6px 0; }
  .track { height: 14px; border-radius: 7px; background: color-mix(in srgb, var(--old) 35%, transparent); }
  .fill { height: 100%; border-radius: 7px; background: var(--old); } .fill.new { background: var(--bar); }
  code { font-size: 13px; }
</style></head>
<body>
<h1>Why sync is slow on trains</h1>
<p class="soft">Measured on a phone with a throttled network: 400 ms round trips, 2% packet loss.</p>
<h2>What happens</h2>
<p>The app sends each change and waits for the server to answer before it sends the next one. On a good
network that is quick. On a train, 30 queued changes take 30 round trips, so the last change waits about 12 seconds,
longer when a request is lost and retried.</p>
<h2>Time until 30 changes reach the server</h2>
<div class="row"><span>Today</span><div class="track"><div class="fill" style="width:100%"></div></div><b>12.4 s</b></div>
<div class="row"><span>Batched</span><div class="track"><div class="fill new" style="width:9%"></div></div><b>1.1 s</b></div>
<h2>The fix</h2>
<ul>
  <li>Send queued changes together, up to 50 per request.</li>
  <li>Keep the order: the server applies a batch in the order the changes were made.</li>
  <li>Retry a failed batch as a whole, so a change is never applied twice.</li>
</ul>
<p class="soft">About a day of work, with tests that replay a recorded train journey.</p>
</body></html>
"""

SPEC_TEXT = """# Tasks app: spec

The Tasks app keeps to-do lists that sync across a phone, a laptop and the web.

## Requirements

| ID | Requirement | Origin | Strength | Status | Evidence | Last change | Comments |
| --- | --- | --- | --- | --- | --- | --- | --- |
| LIST-1 | A list can be shared with other people, who can add and tick tasks. | User | Must | Building | "let me share the grocery list" | 2026-10-28 created (Claude) |  |
| LIST-2 | Tasks keep the order the user put them in, on every device. | User | Must | Live | "the order keeps jumping" | 2026-10-21 created (Claude) |  |
| SYNC-1 | Changes made offline reach the server within a few seconds of the network coming back. | User | Must | Building | "sync is slow on the train" | 2026-11-02 created (Claude) |  |
| SYNC-2 | A change is never applied twice, even when a request is retried. | Claude | Must | Live | follows from SYNC-1 | 2026-11-02 created (Claude) |  |
| REMIND-1 | New reminders get a sensible default time. | User | Should | Not done | "I always have to pick a time" | 2026-11-04 created (Claude) |  |
| LOOK-1 | The app has a dark theme that can follow the system setting. | User | Should | Building | "dark mode please" | 2026-11-05 created (Claude) |  |
| EXPORT-1 | A list can be exported as CSV with its due dates. | User | Could | Live | "I need it in a spreadsheet" | 2026-11-03 created (Claude) |  |

## Notes

- This spec is part of the demo: it is invented, like everything else on these pages.
- The real Cockpit keeps its own spec here, and sessions update it when the user asks for a change.
"""

SKILLS = {
    "tasks-app": (
        "How to work in the Tasks app repository: build, test and release.",
        "# Tasks app\n\nRun `make test` before every commit. The sync tests replay recorded network traces from"
        " `tests/traces`.\n\n## Releasing\n\n1. Bump the version in `app.json`.\n2. Write the release notes in"
        " `CHANGES.md`.\n3. Open a PR and ask Sam to merge it.\n",
    ),
    "website": (
        "How to change the marketing website and preview it.",
        "# Website\n\nThe site is static. `make preview` serves it on a local port.\n\n## Copy\n\nKeep sentences short."
        " Alex reviews every change to the pricing or onboarding pages.\n",
    ),
    "writing-style": (
        "Write clearly for Sam: lead with the answer, then the why, in plain words.",
        "# Writing style\n\n- Lead with the answer.\n- One idea per sentence.\n- Name things the way Sam does.\n"
        "- Put details Sam may skip in collapsed sections.\n",
    ),
}


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class Seeder:
    def __init__(self, conn, clock, home: Path, checkouts: Path):
        self.conn = conn
        self.clock = clock
        self.home = home
        self.checkouts = checkouts
        self.items: dict[str, dict[str, Any]] = {}
        self.steps: dict[str, dict[str, Any]] = {}

    def at(self, hours_ago: float) -> None:
        self.clock.set(-hours_ago)

    def item(self, fields: dict[str, Any], actor: str = "agent:demo") -> dict[str, Any]:
        found = store.upsert_item(self.conn, fields, actor)
        item = found.get("item", found) if isinstance(found, dict) else found
        self.items[fields["key"]] = store.get_item(self.conn, item["id"])
        return self.items[fields["key"]]

    def step(self, key: str, fields: dict[str, Any], actor: str = "agent:demo") -> dict[str, Any]:
        result = store.upsert_step(self.conn, {"item_id": self.items[key]["id"], **fields}, actor)
        self.steps[key] = result.get("step", result)
        return self.steps[key]

    def timed(self, step_id: int, hours_ago: float, minutes: float) -> None:
        """Sam's timer ran on the step for `minutes`, starting `hours_ago`; the clock ends where it began."""
        before = self.clock.offset
        self.at(hours_ago)
        store.start_timer(self.conn, step_id, None)
        self.at(hours_ago - minutes / 60)
        store.pause_timer(self.conn, step_id)
        self.clock.offset = before

    def hand_off(self, step_id: int, text: str, sent_after_s: int = 20) -> int:
        result = store.hand_off_step(self.conn, step_id, text, {"session_id": None, "new_cwd": None}, [], self.checkouts)
        message = result.get("message", result)
        sent = iso(datetime.now(UTC) + timedelta(seconds=sent_after_s))
        self.conn.execute(
            "UPDATE messages SET state = 'sent', sent_at = ?, via = 'socket', take_back_until = NULL WHERE id = ?",
            (sent, message["id"]),
        )
        self.conn.commit()
        return message["id"]

    def message(self, key: str | None, session: str | None, text: str, author: str = "user") -> None:
        now = iso(datetime.now(UTC))
        self.conn.execute(
            "INSERT INTO messages (author, item_id, session_id, text, state, via, created_at, sent_at)"
            " VALUES (?, ?, ?, ?, 'sent', 'socket', ?, ?)",
            (author, self.items[key]["id"] if key else None, session, text, now, now),
        )
        self.conn.commit()


def cwd(home: Path, folder: str) -> str:
    return str(home / "dev" / folder) if folder else str(home)


def seed(conn, clock, home: Path, checkouts: Path) -> Seeder:
    s = Seeder(conn, clock, home, checkouts)
    sid = {name: session_id(name) for name, *_ in SESSIONS}

    s.at(24 * 8)
    projects.put(conn, "agent:demo", "tasks", "The Tasks app: shared to-do lists on phone, laptop and web", LOGO["tasks"], False, None, s.checkouts)
    projects.put(conn, "agent:demo", "website", "The marketing website, its pricing and onboarding pages", LOGO["website"], False, None, s.checkouts)
    projects.put(conn, "agent:demo", "home", "Sam's own errands and paperwork", LOGO["home"], False, None, s.checkouts)
    projects.put(conn, "agent:demo", "cockpit", "Cockpit itself: its briefings, spec and skills", LOGO["cockpit"], False, None, s.checkouts)
    goals = {goal["text"]: goal["id"] for goal in store.set_goals(conn, [
        (None, "Ship list sharing to everyone this week", False),
        (None, "Launch the new pricing page", False),
        (None, "Keep sync reliable on bad networks", False),
    ])}
    briefing.put_team(conn, "Alex (design, reviews the website), Priya (backend, owns sync)")

    # Finished work: two tasks weeks old (only search with Include closed finds them), three from this week.
    for hours, key, title, project, summary in (
        (24 * 26, "note:app-icons", "Refresh the app icons", "tasks", "New icons shipped in 2.1 on every platform"),
        (24 * 19, "note:welcome-email", "Welcome email copy", "website", "The welcome email is rewritten and live"),
        (24 * 6, "note:list-order", "Fix list ordering", "tasks", "Tasks keep their order on every device; shipped in 2.3"),
        (24 * 5, "note:due-dates", "Add due dates", "tasks", "Due dates shipped; reminders use them"),
        (24 * 3, "note:footer-links", "Website footer links", "website", "Footer links fixed and live"),
    ):
        s.at(hours)
        s.item({"key": key, "title": title, "project": project, "stage": "building", "summary": summary})
        check = s.step(key, {"need": "review", "ask": f"{title}: check it works", "size": "medium", "progress": 50})
        s.timed(check["id"], hours - 1, 12 + hours / 10)
        s.at(hours - 2)
        store.step_action(conn, check["id"], "done", {}, "user")
        s.item({"key": key, "stage": "done"}, "user")

    # The merge step: list sharing.
    key = "gh:example-org/tasks-app#41"
    s.at(30)
    s.item({
        "key": key, "title": "List sharing", "project": "tasks", "stage": "review",
        "summary": "Alex approved and checks pass; ready for Sam to merge",
        "sessions": [sid["list sharing"]], "goal_id": goals.get("Ship list sharing to everyone this week"),
        "links": [{"label": "PR #41: share a list", "url": "https://git.example.com/example-org/tasks-app/pull/41"}],
        "doing": "Waiting for the merge",
    })
    s.at(2)
    s.step(key, {
        "need": "merge", "session_id": sid["list sharing"],
        "ask": "List sharing is approved and passing; merge it?",
        "body": (
            "**What it does.** Anyone with the link can join a list, add tasks and tick them off. The owner can"
            " remove people.\n\n**Checked.** Alex approved the change. All 214 tests pass, including new ones for"
            " two people ticking the same task at once.\n\n**After the merge.** I follow it through staging and tell"
            " you when it reaches the app."
        ),
        "details": [{"title": "What changed", "body": "- New `shares` table with the owner and members of each list.\n"
                     "- Invite links expire after 7 days.\n- The list screen shows who else is on the list."}],
        "answers": [{"text": "Merge it", "closes": False}, {"text": "I merged it on GitHub", "closes": False}],
        "size": "small", "progress": 90, "score": 88,
        "reasons": ["Blocks this week's goal", "Approved and ready"],
    })

    # A decision.
    key = "note:reminder-default"
    s.at(20)
    s.item({
        "key": key, "title": "Reminder default time", "project": "tasks", "stage": "planning",
        "summary": "Needs Sam's choice of default time before building", "sessions": [sid["reminder default"]],
    })
    s.at(5)
    s.step(key, {
        "need": "decide:default-time", "session_id": sid["reminder default"],
        "ask": "Which time should new reminders get when you don't pick one?",
        "body": (
            "Today a new reminder has no time, so the app asks every time. I suggest **same time tomorrow**: "
            "it needs no setting and fits most tasks. You can also choose 9:00 the next morning or have the app "
            "remember your first choice."
        ),
        "details": [{"title": "The three choices", "body": (
            "| Option | What happens | Trade-off |\n| --- | --- | --- |\n"
            "| 9:00 the next morning | Every new reminder rings at 9:00 | Simple; wrong for evening tasks |\n"
            "| Same time tomorrow | Rings 24 hours after you made it | Matches when you think of things |\n"
            "| Ask once, then remember | Asks the first time, then reuses your pick | One more screen, once |"
        )}],
        "answers": [
            {"text": "Same time tomorrow", "closes": False},
            {"text": "9:00 the next morning", "closes": False},
            {"text": "Ask once, then remember", "closes": False},
        ],
        "size": "small", "progress": 20, "score": 72, "reasons": ["Blocks the reminder work"],
    })

    # A review with a figure the user picks on.
    key = "note:pricing-page"
    s.at(26)
    s.item({
        "key": key, "title": "New pricing page", "project": "website", "stage": "building",
        "summary": "Two layouts built; Sam picks one", "sessions": [sid["pricing page"]],
        "goal_id": goals.get("Launch the new pricing page"),
    })
    pricing = store.add_item_page(conn, s.items[key]["id"], "Pricing layouts", PRICING_PAGE, "agent:demo")
    s.at(3)
    s.step(key, {
        "need": "decide:layout", "session_id": sid["pricing page"],
        "ask": "Pick the layout for the new pricing page",
        "body": (
            f"![The two layouts](page:{pricing['id']})\n\n"
            "Pick one in the figure, then press **Send**. Both are built; the other one goes away.\n\n"
            "**My pick:** three columns. Most visits come from laptops, where comparing side by side is quickest."
        ),
        "answers": [{"text": "Ship the one I picked", "closes": False}],
        "size": "medium", "progress": 40, "score": 70, "reasons": ["This week's goal"],
    })

    # A result to read, with a report.
    key = "note:sync-slow"
    s.at(28)
    s.item({
        "key": key, "title": "Slow sync on trains", "project": "tasks", "stage": "planning",
        "summary": "Cause found: changes go one at a time; fix proposed", "sessions": [sid["sync on trains"]],
        "goal_id": goals.get("Keep sync reliable on bad networks"),
    })
    report = store.add_item_page(conn, s.items[key]["id"], "Why sync is slow on trains", SYNC_REPORT, "agent:demo")
    s.at(4)
    s.step(key, {
        "need": "read:cause", "session_id": sid["sync on trains"],
        "ask": "Sync is slow on trains because changes go one at a time",
        "body": (
            "The app waits for the server to answer each change before it sends the next. With 30 changes queued on"
            " a slow network, the last one waits about 12 seconds.\n\n"
            f"Sending them in batches brings that to about a second. [The report]({'page:' + str(report['id'])})"
            " has the measurements.\n\n**Next:** I can build the batching, about a day of work."
        ),
        "answers": [{"text": "Build the batching", "closes": False}, {"text": "Ask Priya first", "closes": False}],
        "size": "small", "progress": 10, "score": 66, "reasons": ["Goal: reliable sync"],
    })

    # Ready next: a plain to-do.
    key = "note:driving-licence"
    s.at(50)
    s.item({"key": key, "title": "Renew driving licence", "project": "home", "stage": "planning", "todo": True,
            "summary": "Expires in October; renewal takes about six weeks", "sessions": [sid["driving licence"]]})
    s.step(key, {
        "need": "define", "session_id": sid["driving licence"],
        "ask": "What counts as done for the driving licence renewal?",
        "body": "Your driving licence expires on 9 October. Renewal takes about six weeks, so applying by the end of"
                " August is safe.",
        "answers": [
            {"text": "Find me an appointment slot", "closes": False},
            {"text": "I'll do it myself; close", "closes": True},
        ],
        "size": "small", "progress": 0, "score": 40,
    })

    # Ready next: a finish step.
    key = "note:csv-export"
    s.at(40)
    s.item({"key": key, "title": "CSV export", "project": "tasks", "stage": "staging",
            "summary": "Live in 2.3; first exports look right", "sessions": [sid["csv export"]]})
    s.at(6)
    s.step(key, {
        "need": "complete", "session_id": sid["csv export"],
        "ask": "CSV export is live in the app; close the task?",
        "body": (
            "Lists now export as CSV with their due dates, from the list's menu. I checked the export logs:"
            " 37 people used it since Tuesday, with no errors."
        ),
        "answers": [{"text": "Close it", "closes": True}],
        "size": "small", "progress": 95, "score": 45,
    })
    s.item({"key": key, "stage": "prod"})

    # Ready next: a draft to post.
    key = "note:release-notes"
    s.at(10)
    s.item({"key": key, "title": "Release notes 2.4", "project": "tasks", "stage": "building",
            "summary": "Draft ready for Sam to post", "sessions": [sid["release notes"]]})
    s.step(key, {
        "need": "post:notes", "session_id": sid["release notes"],
        "ask": "Post these release notes for 2.4?",
        "body": "I drafted them from the merged changes since 2.3. Edit the draft if you like, then send.",
        "draft": (
            "**Tasks 2.4**\n\n- Share a list with anyone: they can add and tick tasks.\n- Reminders get a default"
            " time.\n- Sync is faster on slow networks.\n- Dark mode follows your system setting."
        ),
        "reply_hint": "Edit the notes, or say what to change",
        "answers": [{"text": "Post them", "closes": False}],
        "size": "small", "progress": 30, "score": 38,
    })

    # Ready next: a command to run.
    key = "note:db-migration"
    s.at(9)
    s.item({"key": key, "title": "Staging migration", "project": "tasks", "stage": "staging",
            "summary": "The shares table needs creating on staging", "sessions": [sid["staging migration"]]})
    s.step(key, {
        "need": "run:migrate", "session_id": sid["staging migration"],
        "ask": "Run the database migration on staging?",
        "body": "Staging needs the new table for list sharing. Sessions may not change staging, so it needs your Run.",
        "command": {
            "run": "./manage.py migrate --database staging",
            "cwd": cwd(home, "tasks-app"),
            "explain": (
                "- `./manage.py migrate` applies the migrations that are not applied yet.\n"
                "- `--database staging` points it at the staging database.\n\n"
                "It adds one table, `shares`, and changes no existing rows."
            ),
        },
        "size": "small", "progress": 50, "score": 35,
    })

    # Handed: the user answered, the session is on it.
    key = "note:dark-mode"
    s.at(6)
    s.item({"key": key, "title": "Dark mode", "project": "tasks", "stage": "building",
            "summary": "Sam chose to follow the system; adding the switch", "sessions": [sid["dark mode"]],
            "doing": "Adding the theme switch to settings"})
    dark = s.step(key, {
        "need": "decide:theme", "session_id": sid["dark mode"],
        "ask": "Should dark mode follow the phone's setting?",
        "body": "The app can switch with the system, or offer its own switch.",
        "answers": [{"text": "Follow the system", "closes": False}, {"text": "Its own switch", "closes": False}],
        "size": "small", "progress": 30,
    })
    s.at(0.6)
    s.hand_off(dark["id"], "Yes, follow the system; add a switch in settings too")

    # Waiting on a person, a release, an agent's rest.
    s.at(12)
    s.item({"key": "note:onboarding-copy", "title": "Onboarding copy", "project": "website", "stage": "review",
            "summary": "New welcome screens written; Alex reviews them", "sessions": [sid["onboarding copy"]],
            "waiting_on": [{"kind": "person", "who": "Alex", "what": "review",
                            "until": iso(datetime.now(UTC) + timedelta(hours=30))}]})
    s.at(22)
    s.item({"key": "note:ios-release", "title": "iOS release 2.3", "project": "tasks", "stage": "staging",
            "summary": "Submitted; waiting for the app store review",
            "waiting_on": [{"kind": "release", "who": "the app store review", "what": "approval",
                            "until": iso(datetime.now(UTC) + timedelta(hours=48))}]})
    s.at(3)
    s.item({"key": "note:flaky-tests", "title": "Flaky sync tests", "project": "tasks", "stage": "building",
            "summary": "Fixed one race; rerunning tonight to be sure", "sessions": [sid["flaky sync tests"]],
            "waiting_on": [{"kind": "agent", "who": sid["flaky sync tests"],
                            "until": iso(datetime.now(UTC) + timedelta(hours=9)),
                            "why": "Rerun the tests after tonight's build"}]})

    # Snoozed by the user.
    key = "note:spring-sale"
    s.at(30)
    s.item({"key": key, "title": "Plan the spring sale", "project": "website", "stage": "planning",
            "summary": "Ideas listed; Sam picks after the pricing launch"})
    sale = s.step(key, {
        "need": "decide:sale", "ask": "Which spring sale should we plan?",
        "body": "Three ideas: 20% off yearly plans, a free month for teams, or a referral bonus.",
        "size": "medium", "progress": 0,
    })
    s.at(29)
    store.step_action(conn, sale["id"], "snooze", {"until": iso(datetime.now(UTC) + timedelta(days=4))}, "user")

    # Messages Sam sent, shown on the sessions and in history.
    s.at(28)
    s.message("note:sync-slow", sid["sync on trains"], "Sync takes ages on the train. Can you find out why?")
    s.at(26)
    s.message("note:pricing-page", sid["pricing page"], "Build two layouts for the pricing page so I can compare")
    s.at(20)
    s.message("note:reminder-default", sid["reminder default"], "New reminders should get a time by default")

    # Time on the timers.
    for key, hours, minutes in (("gh:example-org/tasks-app#41", 1.5, 10), ("note:pricing-page", 2.5, 14),
                                ("note:sync-slow", 3.5, 6), ("note:reminder-default", 4.5, 4)):
        s.timed(s.steps[key]["id"], hours, minutes)

    s.at(0)
    return s


def put_writing(conn, seeded: Seeder, sessions: list) -> None:
    """Two marks Sam left on the Writing page: one step as a good example, one sentence as hard to follow."""
    store.add_writing_example(
        conn, "good", "step", "", "", "Says what was checked and what happens after; I could merge without opening the PR.",
        seeded.steps["gh:example-org/tasks-app#41"]["id"], None, None, None, sessions,
    )
    store.add_writing_example(
        conn, "bad", "part", f"step:{seeded.steps['note:sync-slow']['id']}",
        "With 30 changes queued on a slow network, the last one waits about 12 seconds.",
        "Which network? Say it was the train, like in my question.",
        seeded.steps["note:sync-slow"]["id"], None, None, None, sessions,
    )


def put_briefing(conn, clock) -> None:
    """This morning's briefing on the team, as the briefing session would write it."""
    clock.set(-5)
    now = datetime.now(UTC)
    day = now.date().isoformat()
    created = iso(now)
    cursor = conn.execute(
        "INSERT INTO messages (author, new_cwd, new_name, text, state, via, created_at, sent_at, as_prompt)"
        " VALUES ('cockpit', ?, 'briefing', 'Write the briefing', 'sent', 'new', ?, ?, 0)",
        ("/", created, created),
    )
    db.kv_set(conn, "briefing_launch", {
        "day": day, "since": iso(now - timedelta(hours=24)), "until": created, "message_id": cursor.lastrowid,
    })
    conn.commit()
    clock.set(-4.5)
    # A mention of a task links to it: clicked in the briefing's layer, it closes the layer and opens the task.
    onboarding = conn.execute("SELECT id FROM items WHERE key = 'note:onboarding-copy'").fetchone()[0]
    briefing.put(conn, "agent:briefing", day, f"""Alex reviewed list sharing and Priya merged the sync retry fix.

## Alex

- Approved [PR #41, list sharing](https://git.example.com/example-org/tasks-app/pull/41) after two rounds of comments
  on the invite screen.
- Started on the onboarding illustrations; asked in #design whether the welcome screen should show a sample list.
- Started reading your new welcome screens, item {onboarding}; comments by tomorrow.

## Priya

- Merged [PR #39, retry failed sync requests](https://git.example.com/example-org/tasks-app/pull/39).
- Opened a ticket about the sync server's memory use under load; no fix yet.

## For you

- Alex is waiting on your merge of list sharing.
- Priya asked whether batching sync changes is planned; the sync report answers that.
""")


def write_transcripts(projects_dir: Path, home: Path, start: datetime) -> None:
    """A short Claude transcript per session: Sam's first message, a few tool calls and the session's reply. The
    Usage page and the session log read these."""
    plans = {
        "list sharing": ("tasks-app", "Add list sharing: anyone with a link can join a list", [
            ("Run the tests", "make test", "214 passed in 38.2s"),
            ("Open the PR", "gh pr create --fill", "https://git.example.com/example-org/tasks-app/pull/41"),
        ], "The PR is open and the tests pass. I asked Alex for a review."),
        "reminder default": ("tasks-app", "New reminders should get a time by default", [
            ("Find where reminders get their time", "grep -rn remind_at src/", "src/reminders.py:42: remind_at = None"),
        ], "There are three sensible defaults; I put the choice on Cockpit."),
        "pricing page": ("website", "Build two layouts for the pricing page so I can compare", [
            ("Preview the site", "make preview", "Serving on a local port"),
            ("Check the pages build", "make build", "Built 18 pages"),
        ], "Both layouts are built. The figure on Cockpit shows them side by side."),
        "sync on trains": ("tasks-app", "Sync takes ages on the train. Can you find out why?", [
            ("Replay a slow network trace", "make replay TRACE=train-commute", "30 changes synced in 12.4s"),
            ("Try the batched client", "make replay TRACE=train-commute BATCH=50", "30 changes synced in 1.1s"),
        ], "Changes go one at a time. Batching brings 12 seconds down to about one; the report is on Cockpit."),
        "dark mode": ("tasks-app", "Add a dark theme", [
            ("Run the UI tests", "make test-ui", "86 passed in 21.0s"),
        ], "Following the system setting now; adding the switch in settings."),
        "release notes": ("tasks-app", "Draft the 2.4 release notes", [
            ("List merged changes since 2.3", "git log --oneline v2.3..main", "4 changes"),
        ], "The draft is on Cockpit for you to edit and post."),
        "staging migration": ("tasks-app", "Get list sharing onto staging", [
            ("Check pending migrations", "./manage.py showmigrations --database staging", "[ ] 0042_shares"),
        ], "Staging needs the migration; I asked you to run it."),
        "csv export": ("tasks-app", "Let me export a list as CSV", [
            ("Run the export tests", "make test K=export", "12 passed"),
        ], "Live in 2.3. I asked you to confirm the task is done."),
        "onboarding copy": ("website", "Rewrite the welcome screens", [
            ("Check the copy fits on a phone", "make screenshots", "6 screenshots written"),
        ], "Alex is reviewing the new copy."),
        "flaky sync tests": ("tasks-app", "The sync tests fail now and then. Fix them", [
            ("Run the sync tests 50 times", "make test K=sync REPEAT=50", "50 runs, 0 failures"),
        ], "Fixed a race between two retries. I'll rerun after tonight's build to be sure."),
        "driving licence": ("", "Remind me to renew my driving licence", [],
                            "Your driving licence expires on 9 October; I put a to-do on Cockpit."),
    }
    models = ["claude-opus-5-5", "claude-sonnet-5-5"]
    for at, (name, (folder, first, calls, reply)) in enumerate(plans.items()):
        sid = session_id(name)
        here = cwd(home, folder)
        moment = start - timedelta(hours=40 - at * 3)
        entries = []

        def stamp() -> str:
            return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")  # noqa: B023 (reads the latest moment on purpose)

        entries.append({"type": "user", "message": {"role": "user", "content": first}, "timestamp": stamp(),
                        "cwd": here, "sessionId": sid, "uuid": str(uuid.uuid5(NS, sid + "u0"))})
        model = models[at % 2]
        for number, (description, command, output) in enumerate(calls):
            moment += timedelta(minutes=3)
            tool = f"toolu_{at:02d}{number:02d}"
            entries.append({"type": "assistant", "message": {
                "model": model, "id": f"msg_{at:02d}{number:02d}", "role": "assistant",
                "content": [{"type": "tool_use", "id": tool, "name": "Bash",
                             "input": {"description": description, "command": command}}],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 1200 + number * 300, "output_tokens": 350 + at * 40,
                          "cache_read_input_tokens": 42000 + at * 5000, "cache_creation_input_tokens": 3000},
            }, "timestamp": stamp(), "cwd": here, "sessionId": sid})
            moment += timedelta(seconds=40)
            entries.append({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool, "content": output, "is_error": False}]},
                "timestamp": stamp(), "cwd": here, "sessionId": sid})
        moment += timedelta(minutes=2)
        entries.append({"type": "assistant", "message": {
            "model": model, "id": f"msg_{at:02d}99", "role": "assistant",
            "content": [{"type": "text", "text": reply}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 900, "output_tokens": 600 + at * 50,
                      "cache_read_input_tokens": 50000 + at * 4000, "cache_creation_input_tokens": 2000},
        }, "timestamp": stamp(), "cwd": here, "sessionId": sid})
        folder_name = "-" + here.strip("/").replace("/", "-").replace(".", "-")
        path = projects_dir / folder_name / f"{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))


def write_skills(repo: Path) -> None:
    for name, (description, body) in SKILLS.items():
        folder = repo / "skills" / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}")
