---
name: sign-in-links
description: >-
  Get the user signed in to a service on this machine with the first link you send.
  Use before calling an `authenticate` tool, starting a login command, or sending the user
  a sign-in link or code. Use when a connector, MCP server or command-line tool reports
  that it needs sign-in, when a sign-in link failed or expired, and when writing the
  Cockpit step that asks for a sign-in.
---

# Sign-in links

The user answers a Cockpit step minutes or hours after it appears, usually from a phone or a laptop that is not this machine.
The sign-in commands of Claude Code and Codex expect a person at this machine's keyboard who finishes within five minutes.
A link fails when the program that made it has stopped waiting by the time the user answers.
Each failed link costs the user another round of opening, approving and pasting, so make the first link the one that works.

Steps and their fields (`need`, `ask`, `reply_hint`, `decide:`) are defined in the `cockpit` skill.
"The user" is the person who signs in. "This machine's account" is the operating-system account the agent runs as.

## 1. Check that the user is needed

- From a new process, call one read-only tool of the service. A stale "needs sign-in" marker can outlive a working sign-in.
- Look for a stored sign-in that renews itself, and try the renewal.
- Look in Cockpit for an open sign-in step for the same service, account and machine. Join it; one sign-in gets one step.

## 2. Choose the route that waits on the user least

| What needs the sign-in | Route |
| --- | --- |
| A local Claude or Codex account | Cockpit's own sign-in on the page. Point the user to it. |
| A Claude Code connector reached over HTTP, such as Slack or Linear | `cockpit signin start`, as the next section describes. Cockpit makes the link, puts the card and finishes the sign-in. |
| Another connector or MCP server that signs in through a browser and returns to a `localhost` address | A link whose pending sign-in is saved to a file, as section 3 describes. This machine sets no deadline on it; the service may set one. |
| A service that offers only a device code, such as GitHub's command-line tool | The device code. The user types a short code on the service's site and pastes nothing back. It has a deadline, so section 3's last rule applies. |
| A service that also offers a long-lived key | Sign-in with renewal first. Offer the key in a `decide:` step when the sign-in keeps lapsing, naming what the key can do. The user hands it over with "Give a secret" in the step's ⋯ menu, which writes it to a private file and keeps it out of messages and transcripts. |

### A Claude Code connector: `cockpit signin start`

```bash
cockpit signin start --server linear --item KEY --why "Reads the ticket ENG-123" --as NAME
```

`--server` is the connector's name in `/mcp`; `--why` is one line telling the user what the sign-in is for.
The command runs on the Claude account of the session's configuration folder and first checks the sign-in that account has.

- `working: …` means the sign-in works, after a renewal when its token had ended. Go on with the work.
  When this session's connector tools still answer that they need a sign-in, `/mcp` reconnects them; a new session has them.
- `asks: …` means Cockpit put a sign-in card on the item, or found the one already up. End your turn.
  The user opens the link, approves and pastes the address into the card's sign-in box.
  Cockpit trades it for tokens, stores them where Claude Code reads them, renews them once and checks that the connector lists its tools.
  It then hands the card to this session with a message saying how many tools the connector lists and until when the sign-in lasts.
  Close the card with `cockpit step done` once you have what you needed.

The card is not the item's card, so it sits beside one that is already open. Cockpit writes its text; leave it as it is.
When the command refuses, it says why, such as a connector that is not in the account's configuration. Sections 3 to 8 below cover the routes Cockpit does not run.

## 3. Make a link that still works when the user reaches it

A browser sign-in has two halves: this machine makes a link and a secret, and later trades the user's pasted answer plus that secret for the sign-in.
Save the pending sign-in to a file only this machine's account can read, in a folder outside any repository:
the secret (the PKCE verifier and the `state` value), the client ID and redirect address the link used, and the time.
The second half reads that file, so a session restart, a reconnect or a slow answer leaves the link working.
Delete the file once the trade succeeds.

- Run each half as its own short script run, so nothing has to stay running between the link and the answer.
  Claude Code's `mcp__<server>__authenticate` tool and `claude mcp login`, and Codex's `codex mcp login`, keep the secret in memory and stop waiting after five minutes.
  Use them when the user is at the terminal now.
- Use the client registration, redirect address and scopes the agent tool already holds for the server, and store the result where that tool reads it.
- Leave a working sign-in alone. Starting a new sign-in for a server can clear the one stored for it.
- The service may set its own limit on a link. State it when its documentation gives one.
- When a route has a deadline, such as a device code, ask first with a step whose answer is "I'm here; send it".
  Send the code within a minute of that answer and put the deadline in the ask line, as a clock time in the user's time zone.

## 4. Keep one pending link per sign-in

Before making a link, look for a pending one on an open step and send that one again.
When you replace a link, delete the old pending file and rewrite the same step under the same `need`, so the page shows only the newest.

## 5. Write the step so one read is enough

A sign-in gets a short step of its own, written for the route. For a browser link it says:

- the service, and the account or workspace to choose in the browser;
- what the sign-in is for and which sessions get it. Check before promising that it holds across a change of agent account;
- the three actions: open the link, approve, then copy the whole address from the browser's address bar and paste it as the reply;
- that the browser ends on a "This site can't be reached" page at an address starting `http://localhost`, and that this page is the expected ending;
- when to act: the link waits, and the paste follows the approval within ten minutes;
- how long the sign-in lasts and whether it renews itself;
- on a repeat ask, in the opening sentence, why the earlier sign-in did not hold and what is different now.

Set `reply_hint` to "Paste the whole address from the browser's address bar".
Show the link's host in the text so the user can see it is the service's own site.
For a device code, the step shows the service's page, the code and the deadline.

> **ask:** connect Slack for the agents; open the link and paste the address back
>
> Agents on this machine cannot read Slack yet. [Sign in to Slack](https://slack.com/oauth/...) and choose the Example workspace.
> The browser then shows "This site can't be reached" at an address starting `http://localhost`. That is expected.
> Copy that whole address and paste it here right away. Open the link whenever it suits you; it waits.
> The sign-in then renews itself; I tell you here when it works.

## 6. When the answer arrives

- Trade the pasted address for tokens before any other work. The service's code inside the address works once, and most services end it about ten minutes after the user approves.
- Take the whole address or its query part; both carry the code and `state`. Compare `state` with the saved one before the trade.
  On a mismatch, keep the pending file, tell the user the paste does not match the current link, and send that link again.
- An `error` in the address means the service or the user declined. Tell the user what it says.
- Tokens are secrets, and so is the pasted address until it is traded.
  Keep them out of every text you write: steps, summaries, commits, logs and messages to other sessions. Refer to a link by the time it was made.
- Store tokens in a file only this machine's account can read. Record when the access expires and whether a renewal token came with it.

## 7. Confirm, then tell the user

- From a new process, call one read-only tool. Restart a session that was running before the sign-in if it still does not list the service's tools.
- Clear the stale "needs sign-in" marker.
- Run the renewal once now and store the tokens it returns before reporting. A renewal token often works once, and a broken renewal then shows today.
- Tell the user in two sentences: connected as which account, on which machine, until when, and whether it renews itself.
  When it failed, say whether anything was stored, so the next ask is no surprise.

## 8. When a sign-in fails

Read the log and the service's reply before sending another link. Establish the cause first; the time since the link was made helps.

| Error | Cause | Next move |
| --- | --- | --- |
| "No OAuth flow is in progress" | The process that made the link stopped waiting: a restart, a reconnect or its timeout. | Make a file-backed link (section 3). |
| "Authentication timeout" | The agent tool's login command waited its five minutes. | Make a file-backed link (section 3). |
| A `state` mismatch | The paste belongs to another link. | Send the current link again. |

The next step tells the user in one sentence what went wrong and in one what changed.
After two failed links for the same sign-in, change the route before sending a third.

## 9. Keep it working

- A sign-in belongs to one machine and one agent configuration folder. Record where it lives, never the secret, where the installation keeps its machine facts.
- When a renewal later fails, the session that owns the work puts one step. A session that meets the missing sign-in during other work finishes the parts that need no sign-in and reports the rest to that owner.

## Where the tools keep sign-ins

| Tool | Stored sign-in | Notes |
| --- | --- | --- |
| Claude Code | `mcpOAuth` in `.credentials.json` in its configuration folder; "needs sign-in" marker in `mcp-needs-auth-cache.json` | `claude mcp login NAME --no-browser` in a terminal takes the pasted address. `claude -p` cannot run a sign-in. |
| Codex | Set by `mcp_oauth_credentials_store`; a server can also take a key through `bearer_token_env_var` | `codex mcp login NAME --no-browser` takes the pasted address. `codex login --device-auth` signs the account in by device code. |
