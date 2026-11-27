# Projects and Cockpit itself

Open this when you create a project, change Cockpit's code, or the user says something about Cockpit.
The parent `SKILL.md` applies too.

## Projects

An item's `project` is an existing broad area or body of work spanning many tasks. `cockpit project list` shows them.
Keep at most ten projects.
Create a project with `cockpit project put --as NAME`, using JSON containing `name` and `about`.
A project with its own git checkout gets `checkout`, its absolute path inside the checkouts folder; new sessions for its tasks start there.
Create one when work begins a new broad area or substantial body of work. Keep related tasks together.
Each project has a logo beside its tasks. A background session tidies projects, assigns ungrouped work and maintains logos.
Use the installation's Cockpit project for its code, spec, skills and step-writing work.

## Changing Cockpit

The configured `spec_repo` identifies Cockpit's checkout. Build Cockpit changes in a worktree of that repository.
Test them, commit the result to the configured `deploy_branch`, and run `cockpit deploy` under the user's update policy.
Put a tested and reviewed Cockpit change live without asking first.
When the change's own new tests pass and only older tests fail for a reason outside it, put it live without asking.
Rerun the full tests once that reason clears, and fix or roll back what fails.
Then put one finish step that shows a screenshot of the change on the live page.
Keep the deployment checkout clean so spec edits can commit independently.

## Cockpit's spec

Record the user's statements about Cockpit in its spec in the same turn.
This includes display, ranking and writing, from sessions, messages, feedback and spec comments.
Preserve their exact words and time as evidence. Attribute a sweeper idea started by the user to the sweeper.
Starting a suggestion records the user's action; it does not make the suggestion their original words.

The configured `spec_repo` holds `docs/SPEC.md`. Read it when building or testing Cockpit changes.
Preserve requirement IDs and its eight-column table. Use the CLI to avoid overwriting concurrent edits:

```bash
cockpit spec get --out SPEC.md
cockpit spec edit --old-file OLD.md --new-file NEW.md --as NAME
cockpit spec put --version VERSION SPEC.md --as NAME
```

`edit` replaces one exact block. `put` replaces the whole spec against the version returned by `get`.
The server commits spec edits. Re-read after a version conflict and follow the installation's update policy.
For small internal choices, pick the reasonable default and record the decision in the requirement's Last change.
Ask when a choice changes what the user sees or does in a way they may object to.
