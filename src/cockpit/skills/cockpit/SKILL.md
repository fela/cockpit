---
name: cockpit
description: Keep the user's Cockpit board current and prepare actionable steps for their work.
---

Cockpit is a personal work page. Use its CLI from this machine. Run `cockpit --help`,
`cockpit item put --help`, `cockpit step put --help`, and `cockpit send --help` for the
current JSON contracts. Use `--as NAME` on every write; use `--as sweep` only as sweeper.

Every item is in a project (`cockpit project list`). Projects are a few broad areas of work and
big bodies of work that span many tasks, at most ten, each with its own logo in the list. Put a
new item in the one it belongs to; create one with `cockpit project put` only for work that starts
a new broad area or a big body of work. Cockpit refuses
a project that does not exist, and a background session tidies projects and designs their logos.

Read `cockpit get --json` before changing the board. Items track work; steps are concrete
actions the user can do now. Link your session UUID to the item and step so replies return
to this conversation. Keep the board current when you finish or need input. A step should
state the decision or action, the evidence, your recommendation and useful answer choices.
Prepare and verify what you can before asking the user. Respect user edits, snoozes and timers.

Every item that waits has something that wakes it again. A bot, person or release in an item's
`waiting_on` carries `until`, the time Cockpit wakes the item's session to look again, and,
where Cockpit can check the wait itself, a `check` that wakes it sooner, such as a release
that holds once the change's PRs reached prod (`cockpit item put --help` has the form). On
that wake-up, look at where the work stands and give the item its next move in the same turn:
a step for the user, or a new wait with a new `until`.

As sweeper, read `references/sweep.md` beside this file. The user's feedback on Cockpit
itself is handled there (section "Feedback"): prepare and test the change, have it
independently reviewed, and follow the user's update policy for the configured
installation. Use only services and connectors
actually available in your session. Report unavailable sources in the sweep result; do not
claim to have checked them. Claude permission hooks and rules do not apply to Codex;
Codex approval and question cards are answered directly on Cockpit.
