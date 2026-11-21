"""Local administrator commands. Secrets are entered over stdin/files and never returned by the HTTP API."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from cockpit import config, gateway_claude, gateway_oauth
from cockpit.gateway_store import ACCESS, PROVIDERS, GatewayError, Store, _private


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="cockpit connectors", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    client = commands.add_parser("client", help="independent coding-client credentials")
    clients = client.add_subparsers(dest="action", required=True)
    create = clients.add_parser("create", help="print one new raw token to stdout; store only its hash")
    create.add_argument("name")
    create.add_argument("--access", choices=sorted(ACCESS), required=True)
    create.add_argument("--show-token", action="store_true", required=True,
                        help="explicitly print this new credential to stdout once")
    clients.add_parser("list")
    revoke = clients.add_parser("revoke")
    revoke.add_argument("id")
    access_update = clients.add_parser("access-update", help="change an existing client's access while preserving its token")
    access_update.add_argument("id")
    access_update.add_argument("--access", choices=sorted(ACCESS), required=True)
    share = commands.add_parser("share-claude", help="use an existing Cockpit-managed Claude connector sign-in")
    share.add_argument("provider", choices=PROVIDERS)
    share.add_argument("--account", required=True, help="configured Claude account name")
    share.add_argument("--server", required=True, help="HTTP MCP server in that account")
    configure = commands.add_parser("configure", help="replace this connector's dedicated OAuth identity")
    configure.add_argument("provider", choices=PROVIDERS)
    configure.add_argument("--redirect-uri", required=True, help="http://localhost:8766/callback")
    configure.add_argument("--client-id", help="your own registered app; required for Slack")
    configure.add_argument("--client-secret-file", type=Path, help="owned mode-0600 file; never a Claude credential file")
    configure.add_argument("--scope", required=True, help="space-separated OAuth scopes; Slack uses comma-separated user scopes")
    for name, help_text in (("signin", "print a new sign-in URL"),
                            ("callback", "read the whole callback URL from stdin"),
                            ("signout", "remove this connector's local grant")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("provider", choices=PROVIDERS)
    commands.add_parser("status", help="configured connector names and sign-in state, without credentials")
    args = parser.parse_args(argv)
    try:
        store = Store(config.DB_PATH.parent / "connectors")
        if args.command == "client":
            if args.action == "create":
                identity, token = store.create_client(args.name, args.access)
                print(json.dumps({"id": identity, "token": token}))
            elif args.action == "list":
                print(json.dumps(store.clients(), indent=2))
            elif args.action == "access-update":
                store.update_access(args.id, args.access)
                print(f"Client access updated to {args.access}")
            else:
                store.revoke(args.id)
                print("Client revoked")
        elif args.command == "share-claude":
            gateway_claude.bind(store, args.provider, args.account, args.server)
            print(f"Sharing {args.provider} from the {args.account} Claude account; Claude Code owns renewal")
        elif args.command == "configure":
            redirect = gateway_oauth.redirect_uri(args.redirect_uri)
            secret = None
            if args.client_secret_file:
                if args.provider == "slack":
                    raise GatewayError("Use a dedicated Slack public PKCE app; its localhost flow sends no client secret")
                path = args.client_secret_file.expanduser()
                if path.name == ".credentials.json" or any(part.startswith(".claude") for part in path.parts):
                    raise GatewayError("Use a Cockpit-owned client secret file outside coding-agent directories")
                _private(path, False)
                secret = path.read_text().strip()
                if not secret:
                    raise GatewayError("Client secret file is empty")
            if args.provider == "slack" and not args.client_id:
                raise GatewayError("Slack requires your own dedicated public PKCE app's client ID")
            if secret and not args.client_id:
                raise GatewayError("A configured client secret also requires its client ID")
            store.configure(args.provider, {"client_id": args.client_id, "client_secret": secret,
                                            "redirect_uri": redirect, "scope": args.scope})
            print(f"Configured {args.provider}; run cockpit connectors signin {args.provider}")
        elif args.command == "signin":
            print(gateway_oauth.begin(store, args.provider))
        elif args.command == "callback":
            gateway_oauth.finish(store, args.provider, sys.stdin.read().strip())
            print(f"Signed in to {args.provider}")
        elif args.command == "signout":
            store.signout(args.provider)
            print(f"Signed out of {args.provider}")
        else:
            rows = []
            for provider in PROVIDERS:
                try:
                    row = store.provider(provider)
                except GatewayError:
                    rows.append({"provider": provider, "configured": False})
                    continue
                state = gateway_claude.status(row)
                expires = row["grant"].get("expires") if row["grant"] else None
                if gateway_claude.shared(row) and row["grant"]:
                    try:
                        _, entry, _ = gateway_claude.source(provider, row["config"]["source"])
                        expires = entry["expiresAt"] / 1000
                        state["source_available"] = expires > time.time()
                    except GatewayError as exc:
                        state |= {"source_available": False, "error": str(exc)}
                rows.append({"provider": provider, "configured": True, **state, "expires": expires})
            print(json.dumps(rows, indent=2))
    except (GatewayError, OSError, ValueError) as exc:
        # Filesystem errors can name a path, while OAuth/body/header values never appear in our safe errors.
        print(str(exc), file=sys.stderr)
        return 1
    return 0
