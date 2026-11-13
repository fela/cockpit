"""Why a message failed, in plain words for the user, from the detail delivery (delivery.py) recorded.

The message list (store.failure_sentence) and the repeats in the patterns
summary both read failures through `failure_reason`, so the same cause reads
the same in both places.
"""

from __future__ import annotations

import re

# The first pattern that matches the detail wins; any other detail shows its first line.
FAILURE_REASONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"Workspace not trusted"), "the workspace was not trusted"),
    (re.compile(r"^No Claude session "), "Claude has no session with that id on this machine"),
    (re.compile(r"^Could not reach the live session"), "the running session did not take the message"),
    (re.compile(r"^The session's directory .* no longer exists"), "the session's directory is gone"),
    (re.compile(r"^(?:The session was already running, so )?Claude started a copy"), "Claude started a copy of the session with it instead"),
    (re.compile(r"^Cockpit restarted before sending"), "Cockpit restarted before sending it"),
    (re.compile(r"^Delivery uncertain"), "Cockpit could not tell whether it got through"),
]


def failure_reason(detail: str) -> str:
    detail = detail.strip()
    reason = next((words for pattern, words in FAILURE_REASONS if pattern.search(detail)), None)
    if reason is not None:
        return reason
    return detail.splitlines()[0] if detail else "no reason was recorded"
