"""The model hub's part (parts.installed, with a [hub] table in the setup file): the hub's accounts and controls on the
Usage page, its sign-in routes, the graph of the allowance left, the tightness reading the sweeper checks, and the
hub's warnings at the foot of the main page."""
from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from pydantic import StringConstraints

from cockpit import config, db, hub, hub_failing, hub_left, hub_tightness
from cockpit.app import Body, Conn
from cockpit.parts import Context, Part, Tags

log = logging.getLogger(__name__)

# How long a tightness reading is kept before it is worked out again: the hub's limits and the use records change
# little in five minutes, and the sweeper reads it every 15 to 30.
TIGHTNESS_SECONDS = 300


class HubCallback(Body):
    # The whole address of the page Claude sent the browser to, or its `code#state`.
    address: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4096)]


class HubAccount(Body):
    # The account's id in the hub's read: the name of its file on the hub.
    id: Annotated[str, StringConstraints(min_length=1, max_length=300)]


class HubAccountDisabled(HubAccount):
    disabled: bool


class HubRouting(Body):
    routing: Literal["round-robin", "weighted-round-robin", "fill-first"]


class HubRestart(Body):
    what: Literal["hub", "tunnel"]


def part(context: Context) -> Part:
    def after_read(accounts: list[dict[str, Any]], now: datetime) -> None:
        """Cockpit's own investigation of a provider whose requests through the hub fail in numbers (SPEC UI-90)."""
        conn = db.connect(context.db_path)
        try:
            sent = hub_failing.investigate(conn, accounts, now, context.spec_repo, context.checkouts_dir, context.providers)
        finally:
            conn.close()
        for message_id in sent:
            context.deliver(message_id)

    setup = config.setup().hub
    if setup is None:
        raise ValueError("the hub's part needs a [hub] table in the setup file")
    hub_reader = hub.Hub(setup.url, setup.key_file, setup.sign_in, setup.tunnel_state, setup.restarts, context.db_path,
                         after_read)
    hub.register_availability_reader(context.db_path, hub_reader)

    def register(app: FastAPI) -> None:
        # The hub's page until it joined the Usage page; old links keep working.
        @app.get("/hub")
        def hub_page() -> RedirectResponse:
            return RedirectResponse("/usage", status_code=308)

        # Signing a Codex or Claude account into the CLIProxyAPI hub (SPEC UI-81): the hub's link, which the state then
        # shows with Codex's code.
        @app.post("/api/hub/{provider}/sign-in", dependencies=[Depends(context.user_only)])
        def hub_sign_in(provider: str) -> dict[str, Any]:
            return {"ok": True, "url": hub_reader.start_sign_in(provider, datetime.now(UTC))}

        # The address Claude sent the user's browser to, for the hub's waiting Claude login.
        @app.post("/api/hub/claude/callback", dependencies=[Depends(context.user_only)])
        def hub_claude_callback(body: HubCallback) -> dict[str, bool]:
            hub_reader.submit_callback(body.address)
            return {"ok": True}

        # The hub's part of the Usage page (SPEC UI-84, UI-85): the hub as last read, None on a machine
        # without one, so asking costs the providers nothing. The controls are the user's; each answers with the hub as
        # read after the change.
        @app.get("/api/hub")
        def get_hub(request: Request) -> dict[str, Any]:
            return {"hub": hub_read(datetime.now(UTC), request.state.me == "user")}

        # The Usage page's graph of each provider's weekly allowance left (SPEC UI-87): the hub's accounts but the
        # signed-out ones, from the limits Cockpit kept of them (hub_left.py); None on a machine without a hub or before
        # its first read.
        @app.get("/api/hub/left")
        def get_hub_left(conn: Conn) -> dict[str, Any]:
            now = datetime.now(UTC)
            read = hub_reader.read(now, False)
            if read is None or read["accounts"] is None:
                return {"left": None}
            shown = [hub.limits_key(each) for each in read["accounts"] if each["state"] != "signed_out"]
            return {"left": hub_left.graph(hub_left.rows(conn, now.timestamp()), shown, now.timestamp()),
                    "tightness": tight(read, now)}

        @app.post("/api/hub/accounts/disabled", dependencies=[Depends(context.user_only)])
        def hub_account_disabled(body: HubAccountDisabled) -> dict[str, Any]:
            return {"hub": hub_reader.set_disabled(body.id, body.disabled, datetime.now(UTC))}

        @app.post("/api/hub/accounts/sign-out", dependencies=[Depends(context.user_only)])
        def hub_account_sign_out(body: HubAccount) -> dict[str, Any]:
            return {"hub": hub_reader.sign_out(body.id, datetime.now(UTC))}

        @app.post("/api/hub/accounts/end-rest", dependencies=[Depends(context.user_only)])
        def hub_account_end_rest(body: HubAccount) -> dict[str, Any]:
            return {"hub": hub_reader.end_rest(body.id, datetime.now(UTC))}

        @app.post("/api/hub/routing", dependencies=[Depends(context.user_only)])
        def hub_routing(body: HubRouting) -> dict[str, Any]:
            return {"hub": hub_reader.set_routing(body.routing, datetime.now(UTC))}

        @app.post("/api/hub/restart", dependencies=[Depends(context.user_only)])
        def hub_restart(body: HubRestart) -> dict[str, Any]:
            return {"hub": hub_reader.restart(body.what)}

    def hub_read(now: datetime, show_code: bool) -> dict[str, Any] | None:
        """The hub as read (hub.Hub.read) with each provider's tightness reading, as the state and /api/hub send it."""
        read = hub_reader.read(now, show_code)
        return None if read is None else read | {"tightness": tight(read, now)}

    def state(now: datetime, is_user: bool) -> dict[str, Any]:
        # The hub's controls sit on the Usage page, so the keyboard switcher finds that page by the hub's words too.
        return {"hub": hub_read(now, is_user), "page_words": {"/usage": "hub accounts proxy sign in"}}

    # Each provider's tightness reading (hub_tightness.py) as last worked out, and when it was asked for. The state asks for
    # it every few seconds; once it is TIGHTNESS_SECONDS old, a thread works it out again (some seconds of reading the
    # use records) while the requests go on with the kept one.
    tight_lock = threading.Lock()
    tight_kept: dict[str, Any] = {"at": float("-inf"), "found": None, "working": False}

    def tight(read: dict[str, Any], now: datetime) -> dict[str, Any] | None:
        """The readings as last worked out for the hub's accounts, starting a new working out when they are old; None
        before the hub's first read lists its accounts, until the first working out ends, and after one failed."""
        if read["accounts"] is None:
            return None
        with tight_lock:
            if not tight_kept["working"] and now.timestamp() - tight_kept["at"] >= TIGHTNESS_SECONDS:
                tight_kept["at"] = now.timestamp()
                tight_kept["working"] = True
                try:
                    threading.Thread(
                        target=work_out, args=(read["accounts"], now), name="hub-tightness", daemon=True
                    ).start()
                except Exception:
                    tight_kept["working"] = False
                    raise
            return tight_kept["found"]

    def work_out(accounts: list[dict[str, Any]], now: datetime) -> None:
        """Work out the readings for the hub's `accounts` at `now` and keep them; on an error, log it and keep None,
        tried again TIGHTNESS_SECONDS later."""
        found = None
        try:
            conn = db.connect(context.db_path)
            try:
                rows = hub_left.rows(conn, now.timestamp())
                hub_codex = hub_left.codex_weeks(conn)
            finally:
                conn.close()
            uses = hub_tightness.provider_uses(context.spend_index, context.codex_index, hub_codex, now)
            found = hub_tightness.readings(accounts, rows, uses, config.setup().timezone, now.timestamp())
        except Exception:
            # The page and the sweeper go on without the reading; the error log raises the alert.
            log.exception("hub tightness: could not work out the reading")
        with tight_lock:
            tight_kept["found"] = found
            tight_kept["working"] = False

    return Part(register=register, state=state, first_scripts=(), page_tags={
        "index.html": Tags(styles=("hub-foot.css",), scripts=("hub-foot.js",)),
        "usage.html": Tags(styles=("hub-accounts.css", "hub.css", "hub-left.css"),
                           scripts=("hub-accounts.js", "hub-left.js", "hub.js")),
    })
