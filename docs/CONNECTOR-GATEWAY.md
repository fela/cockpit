# Connect Linear, Slack and Notion through Cockpit

Cockpit provides one private Model Context Protocol (MCP) endpoint for coding clients. Each client gets its
own revocable credential and explicit read or read/write access. OAuth authorizations for the services
stay on Cockpit's server. An administrator grants each client read or read/write access, and changes it
later with the same command.

## What the server permits

Clients connect to `http://127.0.0.1:18790/api/connectors/mcp` through a dedicated SSH tunnel to the server's
`127.0.0.1:8790`. The gateway accepts only these two literal Host headers, a direct loopback connection,
and its own bearer credential. Browser Origin and forwarding headers are refused. The public reverse
proxy must return 404 for this exact path. Its SSH identity must permit local forwarding to that one
destination, with remote forwarding, shells, agent forwarding and other destinations disabled.

The ordinary Cockpit agent token and browser cookies cannot enter the gateway. Gateway tokens cannot
authenticate ordinary Cockpit API requests, even when such a request also carries a browser cookie.
The SSH credential provides transport access only. Each MCP HTTP request checks its gateway token again.

Names have a service prefix, such as `linear__get_issue`. Permissions are enforced on the server:

| Client access | Tools available |
| --- | --- |
| `read` | The reviewed read list in `connector_gateway.py`, intersected with the provider's published tools. Notion data-source queries require explicit `rows` or `view` mode. |
| `read_write` | Every valid tool the provider currently publishes, using its original input schema. Calls can create, update, delete or publish without individual approval, within the service's OAuth permissions. |

Both roles require a currently published tool and valid arguments. External schema references are refused.
The gateway treats unfamiliar names and unreviewed query modes as requiring write authority, regardless
of a provider's `readOnlyHint`. A write token grants connector operations only; administration remains a
local CLI action on the server.

`cockpit__connector_status` reports the caller's actual `read` or `read_write` mode, provider availability,
sign-in state and safe errors, including before service sign-in. A failed provider is logged at error
level and does not hide healthy providers. Catalog checks run concurrently with a 15-second deadline per
provider. A tool call has a 60-second deadline, including credential acquisition.

The gateway sends a tool call once and refuses tool-call redirects. If a write loses its result, times
out or cannot record completion, it reports **may have happened** and logs the unknown outcome. Inspect
the service before retrying. The gateway keeps no duplicate-request ledger: a client resubmitting a
write is a new call and can repeat its effect. Service error results can also follow a partial change.

## Reuse the existing Claude connector sign-in on the server

An administrator can share an existing Cockpit-managed Claude Code sign-in with gateway clients. This
uses the authorization already stored in the selected Claude account. It needs no new provider app,
consent flow or copied token. The existing Cockpit connector sign-in card remains the place to sign in
again when the provider requires it.

Run each command with the configured Claude account and HTTP MCP server names. These examples use the
`personal` account and servers named after each provider:

```sh
cockpit connectors share-claude linear --account personal --server linear
cockpit connectors share-claude slack --account personal --server slack
cockpit connectors share-claude notion --account personal --server notion
cockpit connectors status
```

The selected server must use the exact provider URL fixed in `gateway_store.py`. The binding records the
account profile and a fingerprint of its server configuration. It rejects changes to either until an
administrator shares that source again. Its private database records the binding and whether sharing is
enabled; source access tokens, refresh tokens and client secrets stay in Claude's own credential file. Source
files must be owned private regular files, and their final path components must not be symlinks.

Claude Code remains the renewal owner. Each acquisition rereads the source. When a token has five
minutes or less left, the gateway runs the installed Claude CLI's `mcp get SERVER` with that account's
environment and rereads the file. A successful exit is sufficient only when the reread contains a valid
token with more than five minutes left. This uses Claude's connection and tool-list check, without a
model request or a tool call. The native CLI controls its own refresh locking and retry policy. The
gateway adds no subprocess retry and makes no refresh POST for a shared source. Its provider lock
coalesces gateway checks across processes.

Sharing depends on the installed Claude Code CLI and its local state. The gateway resolves `claude` on
PATH or at the native installer's `~/.local/bin/claude` location. A native check has a 60-second bound;
a 15-second catalog deadline can return first while the worker keeps its lock until the child finishes
or is reaped. The check uses a fresh private working directory and rejects inherited project MCP or Git
configuration, including ancestor `.mcp.json` files and same-name local server entries. The native
command uses the bound user server; an administrator must keep the gateway data folder outside projects. Both output streams are discarded. A failed check records a nonsecret source fingerprint
and a one-minute cooldown. A changed source is read again immediately. Tokens and the bound source are
checked again before a tool is dispatched, alongside gateway client access and whether sharing was
revoked or reconfigured.

`status` labels these bindings `authorization_source: claude` and `grant_stored: false`; it reports local
source availability without contacting the service. The MCP status tool checks live service availability.
`cockpit connectors signin` and `callback` direct a shared binding to the existing Cockpit sign-in card.
Gateway `signout` disables sharing. It leaves Claude's own authorization and
running sessions in place; re-enabling sharing requires the explicit `share-claude` command. To revoke
the underlying authorization too, use the provider's account settings. The gateway does not create
credential backups.

For an account/server configured as a shared source, the existing Cockpit sign-in flow also leaves renewal
to Claude, including while gateway sharing is disabled. Its final connection check verifies the new token without spending the refresh token as a probe. This
sharing applies to the selected local authorization; provider account selection and consent still
follow that provider's own flow.

## Optionally use independent service identities on the server

Run the commands as the server's trusted administrator from the deployed Cockpit environment. These
commands manage a separate `connectors/gateway.sqlite3` beside Cockpit's main database. The directory
requires mode 0700; the database and lock files require mode 0600. Existing open permissions are refused.
For an independent authorization, configure a dedicated Cockpit client ID. This optional path stores
its own grant and uses its own refresh handling, separately from a shared Claude binding.

Linear and Notion publish dynamic client registration. Each registers a client named **Cockpit connectors**
once and keeps that identity for later sign-ins:

```sh
cockpit connectors configure linear --redirect-uri http://localhost:8766/callback --scope ''
cockpit connectors configure notion --redirect-uri http://localhost:8766/callback --scope default
```

An empty Linear scope uses its documented MCP default of read/write access. Use `--scope read` for a
dedicated service authorization that can only read.

Slack needs a fresh dedicated internal Slack app owned by this setup. Enable its MCP setting and public
Proof Key for Code Exchange (PKCE) authorization, register `http://localhost:8766/callback`, and configure
its own client ID. PKCE binds consent to this sign-in attempt. Public app mode sends no client secret;
the service data stays private. This mode is permanent for that app. Slack documents a 30-day refresh-token limit
for this app mode, so a later sign-in can be necessary. A published partner app's example ID is not
Cockpit's identity. Set user scopes for the operations being enabled; this example includes the read
and write scopes advertised by Slack's MCP metadata:

```sh
cockpit connectors configure slack --redirect-uri http://localhost:8766/callback \
  --client-id YOUR_OWN_SLACK_APP_CLIENT_ID \
  --scope 'canvases:read,channels:history,channels:read,emoji:read,files:read,groups:history,groups:read,im:history,im:read,lists:read,mpim:history,mpim:read,reactions:read,search:read.files,search:read.im,search:read.mpim,search:read.private,search:read.public,search:read.users,users:read,users:read.email,canvases:write,channels:write,chat:write,files:write,groups:write,im:write,lists:write,mpim:write,reactions:write'
```

For each service, start sign-in and open the printed URL:

```sh
cockpit connectors signin linear
```

After consent, the browser redirects to the localhost callback. No listener is needed; a connection error
is expected. Copy the whole redirected address from the address bar, start the following command on the server,
paste the address into its standard input, and end the
input with Ctrl-D. Use the corresponding provider name for Slack and Notion.

```sh
cockpit connectors callback linear
cockpit connectors status
```

The callback accepts the configured address only. It verifies state, PKCE and any supplied issuer, and
expires after ten minutes. The pending code is consumed before exchange. Refresh operations use a
cross-process lock and durable token rotation. A refresh removes the saved old grant before posting;
an interrupted or failed exchange requires sign-in instead of replaying a possibly spent token. A
credential lock wait ends after 20 seconds. `status` reports saved state; it does not claim the grant
has passed a live service call. Use the MCP status tool for that check.

```sh
cockpit connectors signout slack
```

For an independent identity, sign-out removes Cockpit's local grant and pending authorization. To withdraw
that authorization at the service too, revoke the dedicated app in that service's settings. Reconfiguring a connector also removes
its local grant. Each call rechecks the current client access, revocation and service identity after
schema retrieval, immediately before execution starts. An operation already started can finish after
revocation or downgrade.

## Give each coding client its own token

Token creation is a local administrator command. Raw output requires an explicit flag and appears once
on standard output; the store retains only a SHA-256 hash. Redirect that output straight to a private
file when provisioning a new client. Existing tokens remain valid when access changes. Keep the
resulting credential out of transcripts and repositories.

```sh
umask 077
cockpit connectors client create 'Remote Codex' --access read_write --show-token > /tmp/codex-connector.json
cockpit connectors client create 'Remote Claude' --access read --show-token > /tmp/claude-connector.json
cockpit connectors client list
cockpit connectors client revoke CLIENT_ID
```

To change an existing client's access while keeping its token, use its ID from `client list`:

```sh
cockpit connectors client access-update CLIENT_ID --access read_write
```

`client access-update CLIENT_ID --access read` downgrades a client. `client list` shows each saved role.
The migration assigns `read` to every existing credential; it promotes no clients automatically.

Configure each client's HTTP MCP transport with the loopback URL above and its own token in
`Authorization: Bearer TOKEN`. The maintained Python MCP SDK handles Streamable HTTP on both sides.
The gateway constructs upstream headers from its service grant alone. It forwards no incoming bearer
token, cookie, client session ID or forwarding header. Upstream URLs are fixed in code; OAuth metadata
and endpoints are restricted to each provider's HTTPS hosts, with redirects disabled.

## Authored posts keep their agent attribution

Agents must include the existing required first-line attribution in posts under the user's name, including
Slack messages, Linear comments and ticket bodies, and Notion comments. The gateway forwards validated
arguments unchanged. It neither inserts attribution nor checks provider-specific text fields; the
agent is responsible for following its instructions. For example, a Codex-authored post begins with:

```markdown
**[Codex]** _written and posted by Codex on the user's behalf_
```

## What the security boundary covers

This isolates remote coding clients from service credentials and from Cockpit's ordinary API role.
It trusts the server's administrator OS account and the three service providers. An agent or person with
that OS account's file access can read or alter the store and code. It does not protect against a
compromised administrator, server or service provider.

Audit rows contain timestamps, client IDs, provider and tool names, and status only. Arguments, service
responses and credentials are excluded. The gateway also sanitizes MCP and HTTP client logs within
gateway parsing and upstream tasks before Cockpit's handlers can save validation exceptions containing
private request or response bodies. Other Cockpit requests' logs keep their normal content. Service responses still reach the
authorized coding client and its own transcript.

This implementation has fake-upstream coverage for authentication, role separation, server-enforced
read/write permissions, schema validation, downgrade/revocation during schema retrieval, write timeouts,
headers, safe logs, state/PKCE, serialized refresh, shared source validation, native CLI renewal and
source changes before dispatch. Authenticated connection checks are deployment steps. This document does not establish that live grants are configured.

## Sources for the reviewed contracts

- [Claude Code MCP CLI and configuration scopes](https://code.claude.com/docs/en/mcp)
- [Official Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)
- [Linear MCP server](https://linear.app/docs/mcp) and [Linear OAuth](https://linear.app/developers/oauth-2-0-authentication)
- [Slack MCP server and tool scope table](https://docs.slack.dev/ai/slack-mcp-server/)
- [Slack public PKCE flow](https://docs.slack.dev/authentication/using-pkce/)
- [Notion client authorization](https://developers.notion.com/guides/mcp/build-mcp-client)
- Notion's [search](https://developers.notion.com/guides/mcp/tools-search-reference),
  [database queries](https://developers.notion.com/guides/mcp/tools-databases-reference),
  [files](https://developers.notion.com/guides/mcp/tools-files-reference),
  [comments](https://developers.notion.com/guides/mcp/tools-comments-reference),
  [pages](https://developers.notion.com/guides/mcp/tools-pages-reference),
  [skills and agents](https://developers.notion.com/guides/mcp/tools-skills-agents-reference),
  and [calendars](https://developers.notion.com/guides/mcp/tools-calendars-reference) tool references

The allowlist defines read authority, not which names every workspace exposes. Read clients require a
reviewed entry; read/write clients can use new provider-published tools without an allowlist change.
