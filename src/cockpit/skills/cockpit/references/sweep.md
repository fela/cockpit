# Sweep

1. Read `cockpit get --json` and `cockpit sweep --help`. Register the start with
   `cockpit sweep start --help`, then use its documented command.
2. Read `cockpit inbox-settings`: the factors for ranking the user's work, most important
   first, and the places to check for new work, each in the user's own words. Follow both. Check current items, linked sessions, failed messages and open steps.
   Inspect live GitHub, Linear, Slack or Notion state only through tools you actually
   have. A full sweep reads every place the settings list that you have, such as Slack messages the user should answer, Linear
   comments, Notion comments and mentions, and documents a teammate asks the user to
   review wherever the ask is made. Each that needs the user becomes an item with a
   `reply:` step and a draft. Post messages to other people only with the user's
   authorization.
3. Update work that has changed. Ask the owning session to prepare missing evidence
   using `cockpit send`. Avoid repeatedly messaging a session already working on it.
   Triage the to-dos the user added (section "To-dos").
   In every sweep, light ones too, give each bot, person or release in an item's
   `waiting_on` that lacks an `until` one, and a `check` where Cockpit can check the wait
   (`cockpit item put --help`), and change a condition that no longer fits the work. Put
   the whole list back with `cockpit item put --as sweep` and tell the owning session.
4. Rank actionable steps by the factors in `cockpit inbox-settings`, most important
   first. They compete: weigh them together for each step, a factor higher in the list
   counting for more than one lower down.
   Preserve the user's pinned order, edits and active timer. Prepare short, concrete
   steps with evidence and recommendations. Do not invent work to fill the queue.
   Prepare in two depths. The four queue places and one or two spares get full
   preparation. Up to four more items get a quick step from what their session already
   knows, in about ten minutes, its body opening with "Quickly prepared: <what is not
   done yet>". Ask a session to finish its quick step when the step rises into the
   places or the spares, and sooner when fewer than four fully prepared steps are left,
   so the user always has something to do when they clear steps faster than expected.
5. Handle unread feedback (section "Feedback").
6. Finish using `cockpit sweep finish --help` and its documented command. Start the
   summary with the sweep's kind: `Full:` when you read every source, `Fill:` for a
   fill, otherwise `Light:`. Cockpit counts a full sweep only by that word, and while
   none has finished in the last few hours `sweep finish` warns you: run a full sweep
   next. Say which sources were checked and which were unavailable. Record failures rather than success
   when you could not complete the sweep. Finish the sweep before an authorized service
   restart.

# To-dos

Add on the page starts a session for each to-do the user adds (SPEC CONTROL-8); that
session reads the user's words and puts the item's steps. An item with `untriaged:
true` in `cockpit get --json` (`cockpit get` lists it first) is a to-do with no
session, no open step and no one it waits on: one the user added whose session did not
start, or a to-do whose last step closed. Read it and decide what comes next:

- The user added it and its session did not start: start it with `cockpit send --item
  <id> --new-in <checkout> --as sweep` and the user's words, in the checkout the words
  point to. When that fails too, put one step for the user that says so, with the error.
- Only the user can do what is left (a reminder, a call, an errand, a decision, a reply
  only they can give): put one step for the user on that item and start no session.
  The step says what to do, plainly, with anything that helps (a link, a deadline). You
  keep the item current yourself.
- Agent work moves it forward (research, code, drafting, a review): start a session
  on it with `cockpit send --item <id> --new-in <checkout> --as sweep`; that session puts its
  steps.
- Unclear: put one step that asks the user which they want, with your guess first.

Handle a reminder directly as a step when the user is the only one who can act.

# Feedback

`cockpit feedback` lists unread comments on Cockpit. `target` identifies the part
picked (`page:<area>/…`, `spec:<block>`, `skill:<name>:<part>`); `quote` holds its
text for context. Read the requested outcome before changing anything.

The configured `spec_repo` holds the code and spec. Skills live under
`skills_repo`/`skills_folder`. Use the installation's configured branch and update
policy. A clean deployment checkout allows spec edits to commit independently.

1. Prepare the change in a separate worktree. Keep it small and follow the
   repository's style. Record lasting behaviour in `docs/SPEC.md`, preserving row
   IDs and the eight-column table. Use neutral examples in shared documentation.
2. Test the affected behaviour. From the checkout, use `.venv/bin/python -m pytest`
   for Python tests and the repository's lint and type checks. Check interface
   changes in a browser when available. Record what could and could not be checked.
3. Have an independent reviewer inspect the diff, request and affected
   requirements. Fix verified findings and check the affected behaviour again.
4. Follow the user's authorization and repository policy for committing, pushing
   and updating the service. For a configured macOS service, `cockpit deploy`
   installs committed code and schedules a restart. Manual services require
   reinstalling into their environment and restarting through their service manager.
5. When a choice is needed, put a step with concrete options, a recommendation and
   the prepared result. Name any missing permission or tool that prevents progress.
6. Mark handled feedback read with `cockpit feedback --mark-read sweep --ids N[,M]`.
   When a service restart will end the session, finish the sweep before restarting.

# Fill

The user's queue is about to run dry (at most one step besides early looks) and they
have Cockpit open. Register and finish as in "Sweep", but stop at the first few things
you can make ready at once: reviews requested of the user, PRs assigned to them with
no obvious next step, tickets of theirs with nothing happening, replies they owe,
follow-ups that closed items named. The bar is low: any piece of work the user can do
now, with a clear first action, goes in. Put one to three short, prepared steps.
First ask the sessions of quick steps already put to finish them (step 4 of "Sweep").

When fewer than two steps still wait for the user after that, and no urgent or high
item is about to bring one, prepare one or two pieces of work that no item covers yet,
each up to one concrete action of the user's, such as a PR to approve or a reply to
post. Put a `note:idea-<slug>` item for each (skip a key that already exists: it was
offered before), then start a session in its checkout with `cockpit send --new-in <checkout> --as sweep`.
That session does every part of the work that changes nothing shared: it reads, reviews,
runs tests, builds a change on a branch of its own, and drafts the reply or ticket.
Follow the user's permissions for shared changes: approvals, merges, pull requests,
posts, ticket changes and deployments. The session then puts an ordinary step asking for that action, with the prepared work in it and
answers that name the action plus "Not now". The user can also reply to it. Score these
below every step something waits on. Do the same in a full sweep when the queue is that
short. Finish with "prepared: <n>" in the summary.

# Check

Check only the specified steps against live state and mark them with `cockpit step
checked --help`. Revise or withdraw stale steps, preserving the user's work and edits.

# Reorder

A short sweep that rescores the user's queue and ready next from what their steps and items say now, so the order is
right when the user looks. Run `cockpit sweep start --as sweep --reorder`, which leaves what changed for the next
wake-up. Read `cockpit get --json` and `cockpit inbox-settings`, and put only `item_key`, `need`, `score` and `reasons`
for the steps whose score changes. Collect nothing and ask no session for anything. When the message says the user is
still away, keep first a quick step that will hold, as under "Pause". Finish within two minutes with
`cockpit sweep finish <id> --as sweep --summary "Reorder: what moved; what is first"`.

# Pause

Check the specified top steps, leave a useful next action for the returning user, and
finish. Do not schedule your own polling loop; Cockpit manages wake-ups.
