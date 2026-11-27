# PR and team work

Open this when the work has a PR, review, merge, deployment, or text posted under the user's name.
The parent `SKILL.md` and [Writing a card](writing-a-card.md) apply too.

## Work the session does itself

The user supplies judgment. Finish the mechanics within their authorization:

- Mark finished drafts ready. Resolve base conflicts, rerun checks and handle answerable review threads.
- Request the team's code-review bot when CI is green, other reviews are resolved and the branch is settled.
  Use the repository's documented request with agent attribution. Keep the reviewed head stable until its verdict.
  Retry a stopped review once. Use the team's available independent review process when a bot cannot finish.
- Combine your own PRs for one feature. Keep distinct repositories, features, authors and backports separate.
- Start and message sessions. Perform merges and shared actions when already authorized.
- When another person has the next move, keep ownership and assignment current under repository policy.
  Tell the user who holds it, in the step or summary. Respect existing requests and working hours.
- Follow an authorized change through its actual deployment stages. Check it and update the stage at each stop.
  Keep a handed merge step while promised follow-up remains. Set a release wait with the appropriate `reached` check, as [Wake conditions](waits.md#wake-conditions) describes.
  Keep all change and release PR links. On waking, rewrite that handed merge step as the next trial and return it, or put the finish step as under [Handed a step](handoffs.md#handed-a-step). A new step on the item is refused while the merge step stays handed.
  If the user closes an item early, move ongoing follow-up to an open linked item with its own wake condition.
- Carry out the follow-up implied by the user's answer in the same turn. Update the summary with what you did.
  Use the team's configured workflow and keep the next owner explicit.
- On another person's PR, prepare base conflict fixes within repository permissions. Leave substantive fixes and requested changes with the author.
  Keep the item waiting on that author until ready. Settle author-reviewer disagreements before asking the user.
- Once authorized, combine approval, optional thread cleanup and merge when the user expects you to finish them.
  Where authors merge their own work, draft a short verdict and useful hints, then wait on the author.
  Recheck the reviewed commit and CI before any authorized merge. Name the owner of post-merge work.

## Posting and teammates

- Follow the user's posting grant. Bot-review requests and bot-thread replies may be posted only within that grant.
  Attribute them, judge each finding, fix it or explain the existing behavior, then resolve eligible threads.
  Put questions requiring the user's judgment in `decide:` steps. Prepare human replies, reviews and approvals in `draft`.
- Ask teammates for facts or actions only they can supply. Put other unresolved questions under "Open questions" without assigning them.
- Put product choices in `decide:` steps, and record your own reversible choices, as [Decisions](writing-a-card.md#decisions) describes.
- When a reply only explains or agrees, report back as [Replies without a card](handoffs.md#replies-without-a-card) describes.
