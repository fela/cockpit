---
name: hub-setup
description: >-
  Set up a model hub for Cockpit: install CLIProxyAPI, configure it, sign Claude and Codex accounts in,
  point Claude Code and Codex at it, and add the `[hub]` table to Cockpit's setup file.
  Use when the user asks for a hub, a CLIProxyAPI install, or hub accounts on Cockpit's Usage page.
---

# Hub setup

The hub is CLIProxyAPI (https://github.com/router-for-me/CLIProxyAPI), a local proxy that holds the user's Claude
and Codex sign-ins and serves them to coding agents; Cockpit shows its accounts on the Usage page.
The setup makes two secrets: the **management key**, which controls the hub, and the **client key**, which agents send
with each request. Keep both, and the account sign-ins in the hub's `auth-dir`, in files only this machine's account
can read. Keep them out of chat, steps, commits, logs and every repository; refer to them by file path.
Check each command against the current docs (the README, https://help.router-for.me, `config.example.yaml`) first.

## 1. Install CLIProxyAPI

Each install method puts the program and its config file in a different place; the later steps call them PROGRAM and
CONFIG:

| Install | Command | PROGRAM | CONFIG | Service |
|---|---|---|---|---|
| Linux installer | `curl -fsSL https://raw.githubusercontent.com/router-for-me/cliproxyapi-installer/refs/heads/master/cliproxyapi-installer \| bash` | `~/cliproxyapi/cli-proxy-api` | `~/cliproxyapi/config.yaml` | `cliproxyapi.service` (user) |
| macOS, Homebrew | `brew install cliproxyapi` | `cliproxyapi` in `$(brew --prefix)/bin` | `cliproxyapi.conf` in `$(brew --prefix)/etc` | `brew services` |

Homebrew builds the program with CONFIG as its default, so `cliproxyapi` reads it with no `--config`. Other routes: AUR
`cli-proxy-api-bin` (config `~/.cli-proxy-api/config.yaml`), Docker `eceasy/cli-proxy-api`, source; check their docs
for the program and config paths.

Based on: cliproxyapi-installer README, "Installation Directory" (https://github.com/router-for-me/cliproxyapi-installer); Quick Start, macOS and Arch Linux (https://help.router-for.me/introduction/quick-start.html); Homebrew formula `cliproxyapi.rb` (https://github.com/Homebrew/homebrew-core/blob/main/Formula/c/cliproxyapi.rb), `std_go_args`, `etc.install` and `DefaultConfigPath`.

## 2. Configure it

Make each key in its own private file, keeping a key file that already holds a key. The hub replaces a plaintext
`secret-key` with its hash when it loads the config, so the key file is the only readable copy:

```bash
umask 077 && mkdir -p ~/.config/cockpit
for k in hub-management-key hub-client-key; do f=~/.config/cockpit/$k; [ -s $f ] || { openssl rand -hex 32 > $f.new && mv $f.new $f; }; chmod 600 $f; done
```

Set these fields in CONFIG, copying each key in with a short script that reads its file and prints nothing:

```yaml
server:
  host: "127.0.0.1"        # this machine only
  port: 8317
management:
  allow-remote: false      # localhost only; the key is still required
  secret-key: "<contents of hub-management-key>"
access:
  api-keys:
    - "<contents of hub-client-key>"
oauth:
  auth-dir: "~/.cli-proxy-api"   # where account sign-ins are saved
```

An older file keeps `host`, `port`, `remote-management`, `api-keys` and `auth-dir` at the top level; edit those in
place (the legacy map in `config.example.yaml`). Start the hub: `systemctl --user enable --now cliproxyapi.service`
(Linux) or `brew services start cliproxyapi` (macOS). For a hub on another machine, keep these settings there and
reach it through an SSH tunnel such as `ssh -N -L 8317:127.0.0.1:8317 HUB`, run as a user service; the hub then sees
requests from localhost (not confirmed in the docs).

Based on: `config.example.yaml`, sections `server`, `management`, `access`, `oauth` and the legacy map; Basic Configuration (https://help.router-for.me/configuration/basic.html); Management API v8, Authentication (https://help.router-for.me/management/apiv8.html).

## 3. Sign accounts in

Run each login with PROGRAM and CONFIG from step 1, so the sign-in lands in `auth-dir`. With the Linux installer:

```bash
~/cliproxyapi/cli-proxy-api --config ~/cliproxyapi/config.yaml --claude-login --no-browser   # callback port 54545
~/cliproxyapi/cli-proxy-api --config ~/cliproxyapi/config.yaml --codex-login --no-browser    # callback port 1455
```

With Homebrew, run `cliproxyapi` with the same login flags; it reads CONFIG by default.

`--no-browser` prints the link. The provider sends the browser back to the callback port on the hub's machine, so the
user signs in from a browser there or through an SSH forward of that port. Codex also has `--codex-device-login`,
which needs no callback (in `cmd/server/main.go`; not confirmed in the docs). Repeat a login to add another account.
When the user is away from the machine, follow the `sign-in-links` skill.

Based on: provider pages for Claude Code and Codex (https://help.router-for.me/configuration/provider/claude-code.html, /codex.html); Authentication Directory (https://help.router-for.me/configuration/auth-dir.html); cliproxyapi-installer README, "After Installation" (login flags).

## 4. Point Claude Code and Codex at the hub

Claude Code: merge these into `env` in `~/.claude/settings.json`, keeping the user's other settings:

```json
{ "env": { "ANTHROPIC_BASE_URL": "http://127.0.0.1:8317", "ANTHROPIC_AUTH_TOKEN": "<contents of hub-client-key>" } }
```

Codex: add this provider to `~/.codex/config.toml`. This mode leaves the user's own ChatGPT sign-in in `auth.json`:

```toml
model_provider = "cliproxyapi"
[model_providers.cliproxyapi]
name = "OpenAI"
base_url = "http://127.0.0.1:8317/v1"
wire_api = "responses"
requires_openai_auth = true
http_headers = { "X-Api-Key" = "<contents of hub-client-key>" }
```

Based on: Agent clients, Claude Code (https://help.router-for.me/agent-client/claude-code.html) and Codex, "OAuth Login Mode" (https://help.router-for.me/agent-client/codex.html).

## 5. Add the hub to Cockpit

Append to `~/.config/cockpit/setup.toml`, with PROGRAM and CONFIG as full paths (`~` and `$(brew --prefix)` written
out, since Cockpit runs the command without a shell). With the Linux installer:

```toml
[hub]
url = "http://127.0.0.1:8317"
key_file = "~/.config/cockpit/hub-management-key"
[hub.sign_in]
codex = ["/full/path/cliproxyapi/cli-proxy-api", "--config", "/full/path/cliproxyapi/config.yaml", "--codex-device-login", "--no-browser"]
claude = ["/full/path/cliproxyapi/cli-proxy-api", "--config", "/full/path/cliproxyapi/config.yaml", "--claude-login", "--no-browser"]
[hub.restarts]
hub = ["systemctl", "--user", "restart", "cliproxyapi.service"]
```

- With Homebrew, each sign-in command starts with `"/opt/homebrew/bin/cliproxyapi"` and needs no `--config`
  (`/usr/local/bin` on Intel Macs; `brew --prefix` prints the prefix), and the hub restart is
  `["brew", "services", "restart", "cliproxyapi"]` (a Homebrew command; not confirmed in the CLIProxyAPI docs).
- For a remote hub, set `tunnel_state` to a command that prints `active` while the tunnel is up, add its `tunnel`
  restart, and start each sign-in command with `"ssh", "-tt", "HUB"`, so the login prints its link at once and reads
  the address the user pastes into Cockpit (not confirmed in the docs). Write only the keys the user has commands for.
- Restart Cockpit so it reads the file. Cockpit has no `restart` command: on macOS run
  `cockpit service stop && cockpit service start`; on Linux restart `cockpit-server` the way it runs.

Based on: Cockpit README, "Install and run locally"; cliproxyapi-installer README, "Installation Directory" and "After Installation" (paths and service name); Quick Start, macOS (Homebrew prefix paths).

## 6. Check it works

```bash
printf 'Authorization: Bearer %s\n' "$(cat ~/.config/cockpit/hub-management-key)" |  # the key stays off curl's command line
  curl -s -o /dev/null -w '%{http_code}\n' -H @- http://127.0.0.1:8317/v8/management/credentials  # 200: the key works
claude -p "Reply with OK" && codex exec "Reply with OK"  # requests through the hub, from a new shell
```

A 404 means `secret-key` is empty, 401 a missing or wrong key, 403 a remote request with `allow-remote: false`. Five
failed keys from one address ban it for about 30 minutes, so fix the key before trying again. Then open Cockpit's
Usage page: its Accounts section lists each signed-in account.

Based on: Management API v8, Authentication, Errors and Operational endpoints (https://help.router-for.me/management/apiv8.html; docs/management-api-v8.md).
