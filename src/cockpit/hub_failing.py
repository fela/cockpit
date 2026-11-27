"""Cockpit's own investigation of a provider whose requests through the hub fail in numbers (SPEC UI-52, UI-90).

The page warns when at least FAILING_COUNT of a provider's requests through the hub failed in the last FAILING_SECONDS
and they are at least FAILING_SHARE of that provider's requests (hubFailingWarning in static/hub-foot.js, which
uses the same numbers). After each read of the hub, the same test runs here; for a provider it holds for, Cockpit
starts a session on its own in its checkout, on an item that says Cockpit started it and what it saw, and hands the
warning to that item, as the user's message on a warning does (SPEC UI-82). The session runs on another enabled
provider when there is one, since the failing one may refuse it too. It starts none while the warning is handed to an
open item or snoozed, and at most one per provider in AGAIN_SECONDS, so failures that go on cost one session, not one
a minute.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cockpit import db, projects, store

if TYPE_CHECKING:
    import sqlite3

log = logging.getLogger(__name__)

FAILING_SECONDS = 20 * 60
FAILING_COUNT = 20
FAILING_SHARE = 0.25
AGAIN_SECONDS = 6 * 3600
PROVIDERS = {"codex": "Codex", "claude": "Claude"}
STARTED_KEY = "hub_failing_started"
ACTOR = "cockpit"


def warning_key(provider: str) -> str:
    """The key of the page's warning for `provider`, which a hand-off or snooze names."""
    return f"hub-failing-{provider}"


def failing(accounts: list[dict[str, Any]], read_at: datetime) -> dict[str, tuple[int, int]]:
    """The providers whose requests fail in numbers in the read of `read_at`, each with its failed requests and all
    its requests in the last FAILING_SECONDS: every span of the provider's accounts (hub.traffic) that starts in them.
    The page gets the read's time in whole seconds, so the window starts from that."""
    since = read_at.replace(microsecond=0) - timedelta(seconds=FAILING_SECONDS)
    counts: dict[str, list[int]] = {}
    for account in accounts:
        if account["provider"] not in PROVIDERS:
            continue
        count = counts.setdefault(account["provider"], [0, 0])
        for span in account["traffic"]:
            if datetime.fromisoformat(span["at"]) < since:
                continue
            count[0] += span["failed"]
            count[1] += span["succeeded"] + span["failed"]
    return {provider: (failed, total) for provider, (failed, total) in counts.items()
            if failed >= FAILING_COUNT and failed >= FAILING_SHARE * total}


def message(provider: str, failed: int, total: int, now: datetime) -> str:
    """What Cockpit tells the session it starts for `provider`'s failures: that Cockpit started it on its own, what
    it saw and when, and what to do."""
    name = PROVIDERS[provider]
    return (
        f"Cockpit started this investigation on its own; the user did not ask for it. At {now:%Y-%m-%d %H:%M} UTC its"
        f" warning “{name} requests through the hub are failing” came up: {failed} of {total} {name} requests"
        f" through the CLIProxyAPI hub failed in the last {FAILING_SECONDS // 60} minutes, so sessions using {name}"
        " get refused. The warning is handed to this item and stays out of the user's Inbox while the item is open.\n\n"
        "Find which session or program sends the failing requests and why the provider refuses them. Start with the"
        " hub's newest error logs (the request-error-logs of its management API, listed on Cockpit's Usage page), each"
        f" {name} account's recent requests and holds in the hub's credentials, and the {name} processes on this"
        " machine with their working folders and the sessions and items they belong to. When one session runs many"
        " calls at once, tell that session's item through Cockpit to run fewer at a time. Fix what lies in Cockpit or"
        " its setup. Report on this item, in its first lines, that Cockpit started it on its own and what it saw, then"
        " what you found and what you changed."
    )


def hand_back_failed(conn: sqlite3.Connection) -> None:
    """Gives a warning back to Inbox when the session Cockpit started for it could not start: the investigation's
    message failed while the warning is still handed to its item. The six hours before the next investigation still
    hold, so a cause that keeps every session from starting opens no item on each read."""
    with db.transaction(conn):
        started: dict[str, dict[str, Any]] = db.kv_get(conn, STARTED_KEY) or {}
        handed = store.handed_warnings(conn)
        for provider, record in started.items():
            key = warning_key(provider)
            if handed.get(key) != record["item_id"]:
                continue
            row = conn.execute("SELECT state, detail FROM messages WHERE id = ?", (record["message_id"],)).fetchone()
            if row["state"] != "failed":
                continue
            store.unhand_warning(conn, key)
            log.error("Cockpit's investigation of failing %s requests on item %s could not start (%s); its warning is"
                      " back in Inbox", provider, record["item_id"], row["detail"])


def investigate(conn: sqlite3.Connection, accounts: list[dict[str, Any]], read_at: datetime, spec_repo: Path,
                checkouts_dir: Path, providers: tuple[str, ...]) -> list[int]:
    """Start Cockpit's investigation for each provider failing in the read of `read_at` that needs one, on the first
    of the enabled `providers` (the default first) other than the failing one; returns the messages to deliver."""
    hand_back_failed(conn)
    found = failing(accounts, read_at)
    sent = []
    for provider, (failed, total) in sorted(found.items()):
        key = warning_key(provider)
        # The checks and the writes share one write transaction, so a snooze, a hand-off or another read cannot slip
        # in between them.
        with db.transaction(conn):
            if key in store.handed_warnings(conn) or key in store.snoozed_warnings(conn, read_at):
                continue
            started: dict[str, str] = db.kv_get(conn, STARTED_KEY) or {}
            last = started.get(provider)
            if last is not None and read_at - datetime.fromisoformat(last["at"]) < timedelta(seconds=AGAIN_SECONDS):
                continue
            project = projects.resolve(conn, store.COCKPIT_PROJECT)
            item = store.upsert_item(conn, {
                "key": f"note:{key}-{read_at:%Y-%m-%d-%H%M}", "title": f"{PROVIDERS[provider]} hub requests failing",
                "priority": "high",
                "summary": f"Cockpit started this on its own: {failed} of {total} {PROVIDERS[provider]} requests through"
                           f" the hub failed in the 20 minutes to {read_at:%H:%M} UTC",
            } | ({"project": project} if project else {}), ACTOR)
            runs_on = next((each for each in providers if each != provider), provider)
            sent.append(store.create_message(
                conn, ACTOR, message(provider, failed, total, read_at),
                {"item_id": item["id"], "step_id": None, "session_id": None, "new_cwd": str(spec_repo),
                 "provider": runs_on}, checkouts_dir,
            )["id"])
            db.kv_set(conn, STARTED_KEY, started | {provider: {"at": read_at.isoformat(), "item_id": item["id"],
                                                               "message_id": sent[-1]}})
            store.hand_warning(conn, key, item["id"])
        log.warning("%d of %d %s requests through the hub failed; started Cockpit's investigation on item %s",
                    failed, total, provider, item["id"])
    return sent
