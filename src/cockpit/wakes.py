"""Every wait wakes its item again (SPEC STATE-8).

An open item that waits on a bot, a person or a release says when to look again: the wait's `until`. It may also
carry a `check` Cockpit can run, such as "these pull requests reached prod" (stages.reached, cherry-picks included).
Every CHECK_SECONDS Cockpit looks at each such wait. Once its time has come, its check holds, or its check failed
FAILURES_TO_WAKE times in a row, Cockpit messages the item's session, as Cockpit itself, to look at the item and give
it its next move: a step for the user, or a new wait with a new time to look again. With no session on the item, a
new one starts in the checkout of the item's project. While the wait stays as it was, Cockpit asks again every
WAKE_AGAIN, so work does not rest on a wait that came and went; a changed wait starts over. Which waits a session
sets, and how the sweeper sets or changes them, is described in the bundled cockpit skill."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from cockpit import db, rank, stages, store

log = logging.getLogger(__name__)

CHECK_SECONDS = 600
WAKE_AGAIN = timedelta(hours=2)
FAILURES_TO_WAKE = 3


@dataclass(frozen=True)
class Wake:
    """A wait that wakes its item now, and why: "time", "check" or "failed" (with the check's last error)."""

    item: dict[str, Any]
    wait: dict[str, Any]
    why: str
    error: str = ""


def _key(wait: dict[str, Any]) -> str:
    return json.dumps(wait, sort_keys=True)


def _ref(url: str) -> str:
    match = stages.PULL_URL.match(url)
    assert match is not None
    return f"{match[1]}#{match[2]}"


def holds(check: dict[str, Any], reached: Callable[[str], str | None]) -> bool:
    """Whether `check` holds. `reached` gives the furthest stage a pull request reached, or None for one that did not
    merge into a stage branch; it raises stages.GitHubError when GitHub does not answer."""
    wanted = stages.STAGES.index(check["stage"])
    for url in check["pulls"]:
        stage = reached(_ref(url))
        if stage is None or stages.STAGES.index(stage) < wanted:
            return False
    return True


def reacher(
    ask: Callable[[str], stages.Merge | None], ask_holds: Callable[[str, str, str], bool]
) -> Callable[[str], str | None]:
    """`reached` for `holds`, asking GitHub about each pull request and branch once a pass."""
    answers: dict[str, str | None] = {}
    held: dict[tuple[str, str, str], bool] = {}

    def holds_once(repo: str, branch: str, commit: str) -> bool:
        if (repo, branch, commit) not in held:
            held[(repo, branch, commit)] = ask_holds(repo, branch, commit)
        return held[(repo, branch, commit)]

    def reached(ref: str) -> str | None:
        if ref not in answers:
            merge = ask(ref)
            stage = None if merge is None else stages.reached(ref.split("#")[0], merge, holds_once)
            answers[ref] = None if stage is None else stage.stage
        return answers[ref]

    return reached


def due(conn: sqlite3.Connection, reached: Callable[[str], str | None], now: datetime) -> list[Wake]:
    """The waits that wake their items now, with each check's failures counted; forgets the waits that went. GitHub
    is asked outside any transaction, so its answers do not hold up the database."""
    items = db.rows(conn.execute(
        "SELECT * FROM items WHERE archived = 0 AND (stage IS NULL OR stage != 'done') ORDER BY id"
    ).fetchall())
    known = {
        (record["item_id"], record["wait"]): record
        for record in db.rows(conn.execute("SELECT * FROM wakes").fetchall())
    }
    found: dict[int, Wake] = {}
    counted: list[tuple[int, str, int, str]] = []
    current = set()
    for item in items:
        for wait in item["waiting_on"]:
            if wait["kind"] == "agent" or not (wait.get("until") or wait.get("check")):
                continue
            key = (item["id"], _key(wait))
            # One wake an item a pass: two messages to an item without a session would start two sessions. Its other
            # waits come up again in the next pass.
            seen = key in current or item["id"] in found
            current.add(key)
            if seen:
                continue
            record = known.get(key) or {"failures": 0, "error": ""}
            woke_at = rank.parse_time(record.get("woke_at"))
            if woke_at is not None and now - woke_at < WAKE_AGAIN:
                continue
            until = rank.parse_time(wait.get("until"))
            if until is not None and until <= now:
                found[item["id"]] = Wake(item, wait, "time")
                continue
            if not wait.get("check"):
                continue
            try:
                met = holds(wait["check"], reached)
            except stages.GitHubError as exc:
                log.error("checking item %s's wait on %s failed: %s", item["id"], wait["who"], exc)
                counted.append((item["id"], key[1], record["failures"] + 1, str(exc)))
                if record["failures"] + 1 >= FAILURES_TO_WAKE:
                    found[item["id"]] = Wake(item, wait, "failed", str(exc))
                continue
            counted.append((item["id"], key[1], 0, ""))
            if met:
                found[item["id"]] = Wake(item, wait, "check")
    with db.transaction(conn):
        for item_id, wait_key, failures, error in counted:
            conn.execute(
                "INSERT INTO wakes (item_id, wait, failures, error) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (item_id, wait) DO UPDATE SET failures = excluded.failures, error = excluded.error",
                (item_id, wait_key, failures, error),
            )
        for item_id, wait_key in known.keys() - current:
            conn.execute("DELETE FROM wakes WHERE item_id = ? AND wait = ?", (item_id, wait_key))
    return list(found.values())


def _label(url: str) -> str:
    repo, number = _ref(url).split("#")
    return f"[{repo.split('/')[1]}#{number}]({url})"


def message_text(wake: Wake) -> str:
    """What Cockpit tells the item's session: which wait woke it and why, and the next move it owes."""
    item, wait = wake.item, wake.wait
    what = f" ({wait['what']})" if wait.get("what") else ""
    check = wait.get("check")
    if wake.why == "check":
        assert check is not None
        came = f"Its check holds: {', '.join(_label(url) for url in check['pulls'])} reached {check['stage']}."
    elif wake.why == "time":
        came = f"Its time to look again, {wait['until']}, has come."
    else:
        came = (f"Its check failed {FAILURES_TO_WAKE} times in a row, last with: {wake.error}. Look at the wait"
                " yourself, and fix or replace the check.")
    return (
        f"Item {item['id']} ({item['title']}, stage {item['stage'] or 'none'}) waits on {wait['kind']}"
        f" {wait['who']}{what}. {came}\n\n"
        "Look at where the work stands now and, in this turn, give the item its next move: a step for the user when"
        " they have something to do, or a new wait with a new `until` and, where Cockpit can check it, a new `check`."
        " Move the item's stage when the work moved."
        f" Cockpit asks again in {WAKE_AGAIN.seconds // 3600} hours while this wait stays as it is. If this"
        " item is not yours any more, say so in the summary and run `cockpit sweep request`."
    )


def _checkout(item: dict[str, Any], wait: dict[str, Any], checkouts_dir: Path) -> str:
    """Where a new session for the item starts: the checkout named like its project, or like the repository of a
    pull request its wait checks or it links, else the checkouts folder."""
    refs = [_ref(url) for url in (wait.get("check") or {}).get("pulls", [])] + stages.pull_refs(item)
    names = [item["project"] or ""] + [ref.split("#")[0].split("/")[1] for ref in refs]
    places = {Path(place).name.casefold(): place for place in store.checkouts(checkouts_dir)}
    found = next((places[name.casefold()] for name in names if name and name.casefold() in places), None)
    if found is None:
        log.error("no checkout for item %s (project %r) under %s; its new session starts there",
                  item["id"], item["project"], checkouts_dir)
        return str(checkouts_dir)
    return found


def announce(conn: sqlite3.Connection, wake: Wake, checkouts_dir: Path, now: datetime) -> int | None:
    """Queue the message to the item's session and note the wake; returns the message's id, or None when the wait
    changed or the item closed meanwhile."""
    with db.transaction(conn):
        item = store.get_item(conn, wake.item["id"])
        if item["archived"] or item["stage"] == "done" or wake.wait not in item["waiting_on"]:
            return None
        session = store.target_session(conn, item["id"], None)
        target: dict[str, Any] = {"item_id": item["id"]}
        if session is None:
            target["new_cwd"] = _checkout(item, wake.wait, checkouts_dir)
        message = store.create_message(conn, "cockpit", message_text(wake), target, checkouts_dir)
        conn.execute(
            "INSERT INTO wakes (item_id, wait, woke_at) VALUES (?, ?, ?)"
            " ON CONFLICT (item_id, wait) DO UPDATE SET woke_at = excluded.woke_at",
            (item["id"], _key(wake.wait), f"{now:%Y-%m-%dT%H:%M:%SZ}"),
        )
    return message["id"]


def check_once(
    conn: sqlite3.Connection, reached: Callable[[str], str | None], checkouts_dir: Path, now: datetime
) -> list[int]:
    """Wake each item whose wait came (`due`); returns the messages to deliver."""
    wakes = due(conn, reached, now)
    sent = []
    for wake in wakes:
        # One item's failure, such as a locked database, leaves the other items' messages to be delivered.
        try:
            message_id = announce(conn, wake, checkouts_dir, now)
        except Exception:
            log.exception("waking item %s for its wait on %s failed", wake.item["id"], wake.wait["who"])
            continue
        if message_id is not None:
            sent.append(message_id)
    return sent
