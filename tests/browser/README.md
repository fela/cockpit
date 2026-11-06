# Browser tests

The fixtures describe Tasks, a made-up to-do app with lists, reminders, sharing and
exports. The servers build synthetic records in temporary databases and use fake
agent sessions. `serve_preview.py` uses the same data without write delays.

## Work folders

Each test makes its work folder with `workFolder(prefix)` from `work.cjs`, under `$TMPDIR` (or `/tmp`). When the
test ends, after a pass, a failure, Ctrl-C or SIGTERM, everything in it goes except the screenshots (`*.png`), and the
folder goes too when it held none. The servers (`serve_slow.py`, `serve_preview.py`, `serve_keyboard.py`) stop
within a second of the process that started them ending (`parent_watch.py`), and clear what they wrote after it.
`serve_slow.py` removes its temporary database whenever it stops. A server needs its work
folder to exist when it starts, and a preview started by hand runs as long as the command that started it. A new
test uses `workFolder` too, so repeated runs leave only their screenshots.

## Reopening a finished task by message

`reopen_by_message.cjs` sends a message on a task in Finished this week at desktop light, desktop dark and phone
width, on a synthetic database through `serve_preview.py` (nothing reaches a real session), and checks the box's
hint, Busy's lit heading and rolled count, Finished's count, the notice, the row's place in Busy and the server's
state. At desktop light it also checks that a refused Send leaves the task finished with the words back in the box, and
that a Send behind a finish still on its way reaches the server after it (SPEC CONTROL-28).

## Vim prompt editor

`vim_editor.cjs` uses the same isolated fixture as `keyboard.cjs`, with no live data or real
agents. It covers opt-in settings, existing draft preservation, Vim motions/operators/Visual
mode, insert/normal transitions, undo/redo, search, cursor and mode preservation across
action/poll redraws, command-menu focus, Tab escape, reload and cross-tab preferences,
IME-safe sending, and dark/light/mobile layout without CSP violations or page errors.

Use the Playwright setup below, then run:

```bash
PLAYWRIGHT_MODULE=/tmp/cockpit-browser-tests/node_modules/playwright node tests/browser/vim_editor.cjs
```

`BROWSER_EXECUTABLE` also works here. The checked-in editor bundle is ready to test; after
editing its source, run `npm ci && npm run build` in `frontend/` first.

## Page review races and recovery

`reviewed_defects_state.cjs` checks pending comment deletions before Send and Add as new item, repeated Delete,
Delete retries and 404 responses, Run/Allow ordering with Take back, and retrying a failed closed-work search.
It runs without a server or account files: `node tests/browser/reviewed_defects_state.cjs`. An optional static
source directory argument runs the same checks against an earlier copy of the JavaScript.

`page_review.cjs` uses the isolated keyboard fixture for these behavior cases:

- P1: queued finishes guard leaving and send the latest words.
- P2: answers wait for comment deletion and stop when deletion is refused.
- P3: warning snoozes persist (covered by `warning_snooze.cjs`).
- P4: Add more typing guards leaving until cancellation or completion.
- P5: coalesced page choices guard leaving until the latest value saves.
- P6: Take back restores unsent words in Pages after reload.
- P7: Take back uses the server's words and explains an already delivered answer.
- P8: Narrower terminates for detached selections and equal-size ancestors.
- P9: overlapping briefing loads preserve the latest article and navigation.

```bash
PLAYWRIGHT_MODULE=/path/to/node_modules/playwright node tests/browser/page_review.cjs
node tests/browser/page_review_state.cjs
```

The companion state script executes the page's functions with controlled requests and DOM/storage substitutes,
so queue and recovery checks also run where the sandbox blocks Chromium. An optional static-source directory
argument runs those checks against an earlier copy of the JavaScript. The browser script accepts a case id
(for example `P6`) to run that check alone and supports `BROWSER_EXECUTABLE` as the keyboard check does.

## Keyboard navigation

`keyboard.cjs` starts its own fixture with a temporary database and fake sessions. It does
not copy live data or send messages to real agents. It checks command search, page jumps,
J/K and arrow navigation, opening/replying to rows, focus restoration, typing and IME guards,
modal isolation, invalid chords and navigation from every page.

It needs this checkout's test virtualenv and Playwright. For example:

```bash
npm install --prefix /tmp/cockpit-browser-tests playwright
/tmp/cockpit-browser-tests/node_modules/.bin/playwright install chromium
PLAYWRIGHT_MODULE=/tmp/cockpit-browser-tests/node_modules/playwright node tests/browser/keyboard.cjs
```

Set `BROWSER_EXECUTABLE` to use an existing Chromium-compatible browser instead of downloading
Chromium. Tests launch it with a separate temporary profile. On failure, the test prints focus
diagnostics and the path of a screenshot.

## Instant update: every control changes the page at once

`instant_update.cjs` checks that each control the user presses on the main page shows its effect before the
server answers. It starts `serve_slow.py`, which serves this checkout's Cockpit with every non-GET
`/api/` request held for 2 s, then presses each control in a headless Chromium. A control passes when
the change it should make is on the page within 1000 ms of the click: the step leaves the queue, Next
up changes, the toast appears, Update shows busy, and so on. A change that waits for the server comes
after about 2000 ms and fails. A change after 300 ms passes and its line says slow: the page's redraw on
the synthetic data takes about that long on a loaded host.

Run it from the checkout:

```
node tests/browser/instant_update.cjs
```

It prints one line per control as it goes, then a table with each control's result, the time until
the expected change showed, and the time until the first DOM change of any kind. It exits 0 when every
control passed and 1 when one failed or could not be pressed. A run takes about three minutes.

Times are wall-clock in the browser. On a busy machine (load average above about 10) a few
controls that pass at 60–200 ms can take 300–800 ms; run it again when the machine is quieter
before reading one slow control as a regression. A control that waits for the server shows
about 2000 ms whatever the load.

### What it needs

- The checkout's virtualenv (`uv sync`), which `serve_slow.py` runs in as `.venv/bin/python`.
- Node 18 or later, for `fetch`.
- Playwright and Chromium installed as described under Keyboard navigation. Every check
  uses `PLAYWRIGHT_MODULE` (default `playwright`) and `BROWSER_EXECUTABLE` (default
  Playwright's downloaded browser).
- The harness builds a temporary database, settings, account records and rate limits
  from synthetic fixtures. Transcripts come only from the temporary work folder.
  `FakeRunner` handles delivery; the sessions on the page are fakes.

The work dir is a new `cockpit-instant-*` folder under `$TMPDIR` (or `/tmp`); the run prints its path. Only its
screenshots stay after the run (see Work folders).

### What it presses

Each run snoozes the fixture database's open steps and seeds its own items, steps and permission
requests through the agent API, so it does not depend on the initial fixture records. Its first
step starts with its timer running, as a step the user is on. The run presses:

- Resorted on that step, once an agent lowered its item's priority (the step keeps Next up until then);
- the step timer's play and pause on the card, and the
  header chip's pause;
- the priority tag;
- a one-click answer; its hand-off waits for the working fake session's turn, so Interrupt and send under
  "Agents on it"; then Take back there;
- Send from a step's reply box, then the toast's Take back while the hand-off is still on its way;
  then a check that the server has the step back within 8 s, since Take back's request goes once
  the hand-off was answered;
- Archive, then Reopen under Closed;
- Archive, then the toast's Undo while the archive is still on its way, with the same server check;
- Dismiss;
- Snooze (the first time in the menu); then, once the server has answered, a check that the step
  shows under Snoozed within 6 s (it is an error when it does not; the test then redraws the page
  itself); then Unsnooze;
- feedback on a step;
- Run on a step's command (`true`, in the work dir) and Allow on a step's permission;
- Run and Deny on a permission dialog's card;
- Add (opening the box and submitting a to-do);
- feedback mode (the button, picking a part, Send);
- Priorities: Send and Drop;
- Full sweep now and Update.

## Usage colours

`usage_colours.cjs` checks the usage display's pace colours (SPEC UI-12, UI-37, UI-57, UI-73). It starts
`serve_preview.py` (the same harness with no delay, needing the same things as above) and, in a headless
Chromium, in both themes, from the harness's `rate-limits.json`: fresh reports with the 7-day window
"under" and then "near", then a report 40 minutes old with it "over":

- each Claude account's bar is drawn in the pace's colour (`--lamp-green`, `--lamp-orange-fill`, `--lamp-red`);
- each colour meets 3:1 on the ground behind the lane (the test prints the figures);
- each family's mark sits within 6 px of its first bar, with a hairline between Claude and Codex;
- the orange fill is at least 0.085 in OKLab from the amber and from the red;
- the old report keeps its colours with the bars at 40 % strength;
- no bar changes width between the reports (UI-16);
- no page errors.

This check and `machine_lamp.cjs` use fixed machine readings for host layout. A separate
unavailable-readings case checks that the usage page renders with the host indicator absent.

```
node tests/browser/usage_colours.cjs
```

It prints one line per check and exits 0 when all passed. A run takes about half a minute.

## Usage page with Codex

`usage_codex.cjs` checks the Usage page with Codex beside Claude (SPEC SESSION-10). It starts `serve_preview.py`
and opens `/usage` at 1280 and 390 px in both themes, once Cockpit has counted synthetic transcripts:

- Codex's weekly meter, its chart in the last 30 days, its tile and its card under "Where it went";
- the "Of its week" column in the Codex table;
- cards padded at most 8 px and rank bars at most 3 px high;
- nothing wider than the window;
- no page errors.

```
node tests/browser/usage_codex.cjs
```

It prints one line per check and exits 0 when all passed. The test seeds two Codex session transcripts and weekly readings before startup;
it uses no installed account files.

## Page errors: every error reaches the log, and a loop stays cheap

`page_errors.cjs` checks the pages' error log (SPEC CODE-2). It starts `serve_preview.py` (needing the same
things as above) and, in a headless Chromium:

- the main page, Spec, Briefing, Usage and Skills load without logging an error;
- an uncaught error and an unhandled rejection on the main page each reach `/api/page-errors`;
- `logError` called 500 times with one error sends one report at once and, a minute later, one report
  carrying the other 499, so the row counts 500;
- an error whose cause is Cockpit's refusal (a 409) stays off the log, and Cockpit's failure (a 500)
  goes on it;
- a poll while Cockpit restarts (three 502s in a row, then three refused connections) waits it out with no
  error;
- 40 s with the browser offline, and 40 s of refused connections while the page is hidden, are no error,
  and the poll goes on once the page is back;
- a 502 that lasts 40 s, and refused connections for 40 s, each show on the page and are logged once;
- a poll whose answer breaks off while the page reads its body (twice in a row) tries again with no error,
  and a success whose body is not JSON shows on the page and is logged;
- 40 different errors send only what is left of the page's 30 reports an hour.

```
node tests/browser/page_errors.cjs
```

It prints one line per check and exits 0 when all passed. A run takes about five minutes.

`control_use_errors.cjs` fails a real control-use batch and checks the original error through
`/api/page-errors` and SQLite. With `PLAYWRIGHT_MODULE` set, pytest also runs it through
`tests/test_browser_checks.py`.

## A page older than the running Cockpit

`stale_page.cjs` checks error logging and the Reload prompt on a page left open across a deploy
(SPEC CODE-2 and UI-13). It starts `serve_preview.py`, loads the main page, then serves fixture state
with `details` removed from the queue steps. With the page's own version, the error is logged as an error
of the running code. When the served state reports a newer version, the page shows Reload and says it is older
and to press Reload. The error log's `place` field includes ", on a page older than the running Cockpit".
Once the full fixture state is restored, the page displays the queue again.

```
node tests/browser/stale_page.cjs
```

It prints one line per check and exits 0 when all passed. A run takes about two minutes.

## Timer presses: a press goes through a connection that is not back yet

`timer_retry.cjs` checks the timer's play and pause on the main page at phone width, against
`serve_preview.py` (needing the same things as above):

- a play whose first two requests fail to connect shows at once, reaches Cockpit and shows no failure;
- presses made offline (pause one step, play another) show at once and reach Cockpit in the order made;
- play, pause and play made offline leave the step running; pause, play and pause leave none running, once
  all three presses have reached Cockpit;
- a play still offline a minute after it was made is given up unsent once online, shows "Timer not started"
  and goes back on the page;
- a play Cockpit never receives shows "Timer not started" after about 30 s of tries, goes back on the page,
  and is logged.

```
node tests/browser/timer_retry.cjs
```

It prints one line per check and exits 0 when all passed. A run takes about three minutes.

## Page links: a step card lists its item's pages

`page_links.cjs` checks a step card's links section (SPEC API-3) against `serve_preview.py` (needing the same
things as above), at phone and desktop width, for the step in Next up and a step opened in the queue: each
of the item's pages is listed once, with the page icon, though the step's text and the item's links name it
too, and a GitHub link the step's text already holds stays out of the list.

```
node tests/browser/page_links.cjs
```

It prints one line per check and exits 0 when all passed. A run takes about half a minute.

## Step images: the step's own inline, the rest closed

`step_images.cjs` checks a step card's images (SPEC NEXT-17) against `serve_preview.py`, at
phone and desktop width, for the step in Next up. The image the body places shows in the body and not in the
strip, and the user's own image shows in the strip. The two agent images the text does not place sit under a
closed "Other images on this task (2)", after the step's details. Opened, it shows them, and it stays open
through a redraw. It saves a screenshot of the step per width.

```
node tests/browser/step_images.cjs
```

It prints one line per check and exits 0 when all passed. A run takes about half a minute.

## Add: images from files

`add_image_key.cjs` checks the Add dialog's image key (SPEC CONTROL-8) against `serve_preview.py`,
at phone and desktop width. The key shows in the dialog's foot and opens a file picker that takes several
files. Two images picked through it wait in the dialog as thumbnails, and focus stays on the key. Add makes the
item with both images on it. It saves a screenshot of the dialog per width.

```
node tests/browser/add_image_key.cjs
```

It prints one line per check and exits 0 when all passed. A run takes about half a minute.

## An unsent draft is marked

`draft_marker.cjs` checks the "Unsent draft" marker, blue reply box and corner triangle for a step with
unsent reply text (SPEC NEXT-39) against `serve_preview.py`. It runs at phone and desktop width in the light theme
and at desktop width in the dark. With the box empty no mark and no triangle show. Typing in Next up's reply box shows
"Unsent draft" in the card's head at once, clear of the Next up label, as a chip in the box's blue: the same fill,
a visible frame and blue words at 4.5:1 or more. The box has no "Not sent yet" line and, as
the step has a session, no picker of where the words go; Send goes to the step's session. Beside Updated and a
running timer the mark overlaps nothing and stays in the card. While typing and away from the box, the whole box
is light blue, clearly unlike the card and an empty box, and its words keep the text colour and weight. The card
carries a small blue triangle in its top right corner, as painted on screen and covering none of its controls,
also with the window so short that the reply box is out of view. After a reload the mark is there again. A
collapsed queue row whose step holds a draft shows "Draft", in the same blue, and the blue triangle; opened, its box is marked the
same. Emptying the box takes the mark, the colour and the triangle away. It saves a screenshot of the Next up card
per run.

```
node tests/browser/draft_marker.cjs
```

It prints one line per check and exits 0 when all passed. A run takes about half a minute.

## Closing: a solid plum key, the tear, and the Closed! notice

`close_seal.cjs` checks every way to close (SPEC CONTROL-17) against `serve_preview.py` (needing the same
things as above): a closing answer, Task done and Close item on a card in Next up, and Close on an item's row
in All work on the Board. For each it checks that the key is solid plum, a ring leaves the key, the card
breaks into many pieces (at least four) with no checkmark, the notice reads
"Closed! (N in the last 24h)" with N counting up, the notice is plum with Undo, and the server's count
agrees; for the row, that Undo brings it back, and that closing the row of an item Task done completed
leaves the count as it was, also after Undo. It saves screenshots of the keys and the notice, and prints
their folder.

```
node tests/browser/close_seal.cjs
```

It prints one line per check and exits 0 when all passed. A run takes about a minute.

## Adding time to a running timer

`add_time.cjs` checks adding time to a running timer by hand (TRACK-7), at laptop and phone width, against
`serve_preview.py`:

- the running header chip shows Pause and no + , and the header fits the page;
- in the clock's "Correct the time", "+20m" offers "Add 20m" and adds 20 minutes on Cockpit while the timer
  runs on, with no question about time counted while away;
- "Correct the time" takes a higher total as well as a lower one.

```
node tests/browser/add_time.cjs [screenshot folder]
```

It prints one line per check and exits 0 when all passed. A run takes under a minute.

## Writing in the step's reply box keeps its timer running

`reply_keeps_timer.cjs` checks that working inside the step on the clock, or elsewhere on the page, keeps its
timer running (TRACK-6, ), at laptop width, against `serve_preview.py`:

- typing in the step's reply box leaves the timer running, with no "Paused: you left the step" toast;
- selecting words in the step and pressing Comment, which floats outside the card and quotes them into
  the reply box, leaves it running too, and typing on at once goes into the box;
- the command menu opened from the reply box, a redraw of the step while it is open, and Escape put the
  cursor back in the new reply box, so typing on keeps the timer running;
- focus in a field outside the step (the Add box, opened with `a`) and a click outside it leave it running
  too, with no toast.

```
node tests/browser/reply_keeps_timer.cjs [screenshot folder]
```

It prints one line per check and exits 0 when all passed. A run takes under a minute.

## Take back after Send

`take_back.cjs` checks Take back on the notice after Send (MSG-8), at laptop and phone width,
against `serve_preview.py`:

- Send on Next up hands the step off, and the notice offers Take back;
- Take back brings the step back with its reply box open, holding the words that were not sent, and the notice
  says so;
- no failed receipt stays under the box, and on Cockpit the message is set aside unsent;
- Send from the box again hands the step off with the added words.

```
node tests/browser/take_back.cjs [screenshot folder]
```

It prints one line per check and exits 0 when all passed. A run takes under a minute.

## Play on a snoozed step

`snooze_play.cjs` checks play on a snoozed step , at laptop and phone width, against
`serve_preview.py`:

- a snoozed step's row opens under Snoozed with its content covered and a play reading "Unsnooze and start";
- the play brings the step back into the queue, starts its timer and shows the content;
- two seconds later the timer still runs.

```
node tests/browser/snooze_play.cjs [screenshot folder]
```

It prints one line per check and exits 0 when all passed. A run takes under a minute.

## A session's reply on the step card

`history_refresh.cjs` checks that a reply reaches a returned step's card with no other change to the page,
against `serve_preview.py`. The harness confirms no delivery and reads no transcripts, so the
test writes the sent message and the turn into the fixture database itself:

- an idle page refetches the shown history within 20 s, and an unchanged history asks for no redraw;
- the reply, written as a turn meanwhile, shows on the card within 20 s, whole, not as a summary;
- at 390 px the whole reply wraps with no sideways scroll.

```
node tests/browser/history_refresh.cjs [screenshot folder]
```

It prints one line per check and exits 0 when all passed. A run takes about a minute.

## A step the user opened keeps its place

`held_step.cjs` starts `serve_preview.py` on a synthetic database, snoozes the seeded steps and
puts four steps on a new item. It presses Next up's ask without starting the timer, checks that J moves
the highlight without holding a step and that O opens a row and holds it, and types in the second step's
reply box, then puts four urgent steps (SPEC NEXT-16). It checks that the touched steps keep
their places on the server and on the page, that the untouched steps give way, that Next up shows the
accent Resorted key whose tooltip names the urgent item, that nothing on Next up runs past its edge at
390 px, that a press on Resorted brings the urgent step to Next up while the pressed step stays in the
queue, and that an opened row shows Resorted too and its press takes the waiting urgent step. It saves `resorted-light.png`, `resorted-dark.png` and
`resorted-phone.png` in its work folder and prints the folder.

```bash
node tests/browser/held_step.cjs
```

## Take it: a ready-next step to the top

`take_step.cjs` checks taking a ready-next step to the top of the queue (NEXT-36), at laptop and phone width,
against `serve_preview.py`:

- every ready-next row shows "Take it", and the page has no sideways scroll;
- pressing it makes the step first on Cockpit and Next up on the page, and the weakest queue step waits in
  Ready next;
- T on a highlighted ready-next row does the same.

```
node tests/browser/take_step.cjs [screenshot folder]
```

It prints one line per check and exits 0 when all passed. A run takes under a minute.

## Queue order: the user's takes, then the sweeper's score

`queue_order.cjs` checks the queue order of RANK-18 on `serve_preview.py`:

- the page lists the steps the user took first, then the rest by score;
- a step at score 5 on a new high-priority item, and a new finish step, get no place of their own;
- once the sweep's put scores the high-priority step 100, it leads the steps the user did not take.

```
node tests/browser/queue_order.cjs
```

It prints one line per check and exits 0 when all passed.

## Next up's marks on a phone

`next_strip.cjs` checks Next up's marks on a phone on `serve_preview.py` (UI-32), at 390 and 360 px unless
said otherwise:

- with its usual marks, they sit at the card's right above the item's line, which "Next up" leads;
- with every mark showing (Updated, the size bar, a timer running over an hour, the check and the info
  mark), running, paused or held, none covers the label or the item's line, they stay in the card, their
  44 px tap areas (read from the page's `::after` boxes) reach no higher than its top and stay apart and
  clear of the priority's and the chips', and the item's line starts below them;
- at every 8 px from 320 to 640, the ask stays put across those states (UI-16), and the rules above hold.

```
node tests/browser/next_strip.cjs
```

It prints one line per check, exits 0 when all passed, and saves a screenshot of the card's top per width.

## The Cockpit tag on Cockpit's own work

`cockpit_tag.cjs` checks on `serve_preview.py` that an item whose project is Cockpit, and its steps, carry the
Cockpit tag (UI-43), at 320, 390 and 1280 px in the light and the dark theme: a put of "Cockpit" stores
`cockpit`; the tag shows, its key and words visible and not clipped, in Next up's head, on a collapsed queue row,
on the item's row in the work list, in Pinned, under You handed these, in the steps closed in the last day and
Finished this week (opened), in the status lights' plate, in the search menu, and in the Pages tab's list and, on a
wide screen, its reader; a phone's one-line rows (handed and closed rows, the lights) keep the key alone; a step an
agent closes while the user has it open keeps its tag and its whole close button, open and collapsed; beside the
tag a handed step's ask loses no more than the key's room; other work carries none; the tag's amber key stands out
from the card; the page does not scroll sideways, and has no page errors.

```
node tests/browser/cockpit_tag.cjs
```

It prints one line per check, exits 0 when all passed, and saves a screenshot of the page's top per width and theme.

## Screenshots

`shots.cjs` shoots a page served by `serve_preview.py`, for working on the look; its header lists the
options.

## A dialog that closed in its session

`settled_card.cjs` checks the card of a dialog that closed in its session without the user's answer (PERM-23), at
laptop and phone width, on the keyboard fixture's isolated database:

- a dialog shows at once as a card with Run and counts in Blocked;
- once the hook settles it, the card stays in place, marked closed, with nothing to press, out of the count;
- a minute after it closed, it has gone;
- nothing is wider than the window, and the page logs no error.

```bash
PLAYWRIGHT_MODULE=/tmp/cockpit-browser-tests/node_modules/playwright node tests/browser/settled_card.cjs
```

## Cards that went within a minute

`flashes.cjs` checks the recorder of cards that leave the page soon after showing (PERM-24), on the classic and the
sidebar page, on the keyboard fixture's isolated database:

- a refusal that shows and is withdrawn by its session within a minute is noted, with what took it away, and so
  is one already there when the page loaded, since it came into view then;
- one the user pressed in before it went is not, and neither are the cards "Show fewer" folds away;
- a step that takes a queue place and is withdrawn by an agent within a minute is noted;
- the page logs no error.

```bash
PLAYWRIGHT_MODULE=/tmp/cockpit-browser-tests/node_modules/playwright node tests/browser/flashes.cjs
```

## Blocked calls the session got past

`moved_past.cjs` checks how Blocked and waiting holds back and folds refusals (PERM-18), at laptop and phone width,
on the keyboard fixture's isolated database:

- a refusal under a minute old stays off the page;
- a refusal the session still needs shows as a card and counts in Blocked;
- a refusal the session got past folds under one line at the end, out of the count, and opens to its card, which
  says first how the session got past it;
- nothing is wider than the window, and the page logs no error.

```bash
PLAYWRIGHT_MODULE=/tmp/cockpit-browser-tests/node_modules/playwright node tests/browser/moved_past.cjs
```

It prints one line per width and the folder of its screenshots, and exits 0 when all passed.

## A blocked command in parts

`command_parts.cjs` checks how a blocked Bash call's card shows its command (PERM-22). It first checks
`shell.js` alone on a few commands, then, on the keyboard fixture's isolated database, at laptop and phone width
in light and dark:

- the command shows one part per line, split after `&&`, `|` and `;`, and its text is the command's own;
- the command words, the joins and the quoted text each have their own colour;
- with Tool rule picked, the call's other commands show one per line, a commit message cut to its first line with
  the whole command in its tooltip;
- nothing is wider than the window, and the page logs no error.

```bash
PLAYWRIGHT_MODULE=/tmp/cockpit-browser-tests/node_modules/playwright node tests/browser/command_parts.cjs
```

It prints one line per width and look, and the folder of its screenshots, and exits 0 when all passed.

## Allow is final

`allow_final.cjs` checks a step's Allow (NEXT-41) on the keyboard fixture's isolated database. It presses Allow on a
step that asks for a tool rule, has the session return the step, and then checks at laptop and phone width, in light
and dark:

- the returned step says the user allowed it, with its rule and no Allow button;
- nothing is wider than the window, and the page logs no error.

```bash
PLAYWRIGHT_MODULE=/tmp/cockpit-browser-tests/node_modules/playwright node tests/browser/allow_final.cjs
```

It prints one line per width and look and the folder of its screenshots, and exits 0 when all passed.

## Codex sign-in

`codex_sign_in.cjs` presses the Codex sign-in (SPEC UI-77) at desktop light and phone dark: on scratch folders named in `codex-homes.json`, which `serve_slow.py` reads before it clears its work folder, with `tests/fake_codex.py` as the command. Without that file the server's sign-in command is `/bin/false`, as a real `codex login` deletes a folder's sign-in. Run it with `node tests/browser/codex_sign_in.cjs`.


## Isolated regression checks

`reviewed_fixes.cjs` runs the shipped save, permission-rule and machine-rack functions in Chromium with mocked
requests. It holds a rule Add while Remove and Change back wait, checks the save's leave warning through a
refused spec or skill save, and checks that an unavailable machine reading leaves out the rack.

`local_login.cjs` runs the shipped local sign-in and fetch scripts with mocked HTTP responses. It checks storage
of the page secret, reuse in another page, separation between ports, and return to sign-in when the secret is
missing. Both checks use test data only and start no server.

```bash
node tests/browser/reviewed_fixes.cjs
node tests/browser/local_login.cjs
```
