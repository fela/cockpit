# Browser tests

The fixtures describe Tasks, a made-up to-do app with lists, reminders, sharing and
exports. The servers build synthetic records in temporary databases and use fake
agent sessions. `serve_preview.py` uses the same data without write delays.

## Which checks run where

`uv run --extra test pytest tests/test_browser_checks.py` runs these files:

- every `*_state.cjs` check in this folder.
  They run page code with simulated browser state and need no browser or server. Pytest runs them with `bun` from
  `PATH`, else `~/.bun/bin/bun`, else `node`, and skips them when it finds neither. With `node` they need Node 19
  or later: two of them use the global `CustomEvent`, which Node 18 lacks.
- `control_use_errors.cjs`, when `PLAYWRIGHT_MODULE` is set.

The other checks run by hand from the checkout, one at a time: `node tests/browser/<name>.cjs`. Each needs
Node 18 or later, Playwright and Chromium (the setup is under Keyboard navigation; every check reads
`PLAYWRIGHT_MODULE` and `BROWSER_EXECUTABLE`).

- `icon_warming.cjs`, `local_login.cjs`, `reviewed_fixes.cjs` and `usage_tables.cjs` start no server and need nothing
  more.
- The others start this checkout's Cockpit on a synthetic database themselves, through `serve_preview.py`,
  `serve_keyboard.py` or `serve_slow.py`, so they also need the test virtualenv (`uv sync --extra test`).

`shots.cjs` takes screenshots of a preview you start yourself. `work.cjs` and `machine_fixture.cjs` are helpers the
checks load.

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

## Quotes in the reply box and in sent messages

`quote_display.cjs` checks that a quote made with Comment reads as a quote, never as ">" lines (SPEC NEXT-63), at
desktop light, desktop dark and phone width on a synthetic database through `serve_preview.py`. A sent message with a
quote must show it in the card's fold, opened by Show all and in the history without a ">". After Comment and typing,
the reply box's own letters must be transparent over a copy whose quote line has its bar, holding exactly the box's
words and wrapping to the same height. It saves screenshots of both in its work folder.

## Time zone question and picker

`zone_ask.cjs` sets Cockpit to Jakarta and runs the browser in Tokyo, seen there for seven hours, on a synthetic
database through `serve_preview.py` (SPEC CONTROL-32). At desktop light it checks the question card and that Keep
removes it; at phone dark it switches through the brand menu's picker and checks the work folder's setup file. It
saves screenshots of the card and the picker.

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

## State synchronization and performance

`state_sync.cjs` checks incremental state reads, missing-checkpoint recovery,
isolation from local edits, and enabling Vim while its bundle loads or browser
storage is blocked. `render_work_state.cjs` checks folded row construction,
usage redraw counts and clock updates without a browser. `performance.cjs`
compares this checkout with a supplied baseline on the same synthetic board:
`node tests/browser/performance.cjs BASELINE_CHECKOUT OUTPUT_JSON`. It alternates
five cold loads per version and records compressed bytes, redraws and polls.

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

- The checkout's virtualenv (`uv sync --extra test`), which `serve_slow.py` runs in as `.venv/bin/python`.
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

## A weekly limit for one model

`usage_model_limits.cjs` checks a Claude account's weekly limit that counts one model's use alone, beside the week all
models share (SPEC UI-12, SESSION-10), on local accounts. It starts `serve_preview.py` and opens `/classic`
at 1440 px in both themes, then `/usage` at 1280 and 390 px:

- the usage display's spoken words give both figures, "personal 62% (Fable 38%)";
- the hover card names both weeks, "All models" and the model, each with its own bar and its use and reset;
- an account without such a limit keeps its one "7d" bar;
- the Usage page has one meter named for the model, after its account's own;
- nothing wider than the window;
- no page errors.

```
node tests/browser/usage_model_limits.cjs
```

It prints one line per check and exits 0 when all passed. The hover card and the Usage page's meters are saved as
`card.png`, `limits.png` and `limits-phone.png` in the work folder. It writes `model-limits.json` there once the
harness is up, since the harness empties the folder at startup.

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

## Cockpit installed as an app

`installed_app.cjs` installs Cockpit in Chromium, opens it in its own window and takes it through an update
(SPEC UI-13 and UI-79). It needs the same things as above. Two copies of the checkout, the second with one changed stylesheet,
take turns behind one address, so a real restart stands in for a deploy.

- Chromium accepts the manifest, and the files installing needs are open without a sign-in;
- the installed window opens at the start address and reports the `standalone` display mode;
- the main page, the installed window and the spec, briefing, usage and pages pages all show Reload after the
  restart, and none reloads by itself;
- a page whose first answer already comes from the new build still shows Reload, because each page knows which
  Cockpit sent it;
- Reload goes away if Cockpit goes back to the page's version, and returns when the new version runs again;
- pressing Reload brings the new files, and the installed window is still an app afterwards;
- writing nobody sent survives Reload: a spec edit and comment come back open, an edit of a part that changed comes
  back in a banner with the newer text left alone, team words on the briefing come back, and a comment placed on a
  shared page's words comes back and still lands on those words;
- Reload on the spec leaves the part you were reading in view, and Reload on the usage page comes back to the same place.

```
node tests/browser/installed_app.cjs
```

It prints one line per check, keeps its screenshots in a temporary folder it names, and exits 0 when all passed.
The run is headless, so it cannot check the system's own install prompt or a Google sign-in inside the window.

## Timer presses: a press goes through a connection that is not back yet

`timer_retry.cjs` checks the timer's play and pause on the main page at phone width, against
`serve_preview.py` (needing the same things as above):

- a play whose first two requests fail to connect shows at once, reaches Cockpit and shows no failure;
- presses made offline (pause one step, play another) show at once and reach Cockpit in the order made;
- play, pause and play made offline leave the step running; pause, play and pause leave none running, once
  all three presses have reached Cockpit;
- a play still offline a minute after it was made is given up unsent once online, shows "Timer not started"
  with how long the page was away and a Start now button, goes back on the page, and is not logged; Start now
  starts the timer; a play made just after an offline pause, which waits behind it for over a minute, is given up
  the same way and not logged;
- a play Cockpit never receives shows "Timer not started" after about 30 s of tries, goes back on the page,
  and is logged.

```
node tests/browser/timer_retry.cjs
```

It prints one line per check and exits 0 when all passed. A run takes about three minutes.

## Timer start during a press: the press still lands

`timer_start_press.cjs` checks the timer the page starts on its own on Next up's card (UI-19, TRACK-2) against
`serve_preview.py` (needing the same things as above), with the mouse at desktop width and a touch at phone width.
Each round starts with the page quiet, so the press is the input that lets the page start the timer:

- a press on the card's answer, held while the page starts the timer, still sends the answer;
- the start goes to Cockpit during the press;
- a press held on the card's heading, which presses nothing, shows the running timer once it ends;
- with no press, the timer shows running within two seconds of the user's input;
- the page throws no errors.

Before the fix, the mouse round lost the press: the start redrew the card while the button was down, and the
release landed on a new button. A touch landed either way, since the browser picks a tap's target at the release.

```
node tests/browser/timer_start_press.cjs
```

It prints one line per check and exits 0 when all passed. A run takes about half a minute.

## Page links: a step card lists its item's pages

`page_links.cjs` checks the list behind a step card's "Other" chip (SPEC API-3) against `serve_preview.py` (needing
the same things as above), at phone and desktop width, for the step in Next up and a step opened in the queue:
each of the item's pages is listed once, with the page icon, though the step's text and the item's links name it
too, and a GitHub link the step's text already holds stays out of the list.

```
node tests/browser/page_links.cjs
```

It prints one line per check and exits 0 when all passed. A run takes about half a minute.

## Core links: the links a step is about, and Other

`core_links.cjs` checks the row of links under a step's text against `serve_preview.py` (needing the same things as
above), at 1440 and 390 px, light and dark. It puts three tasks in the queue:

- one whose step names two core links (a GitHub PR and the task's shared page), whose earlier step (closed) links a
  design doc and a Slack thread, and to whose session the user wrote a message with a link;
- one whose step names no core links, with one link on the task;
- one with no links at all.

It checks that the chips sit after the step's text and above its answers, with the right addresses (the page opening
for the step); that "Other N" counts the rest, under "On this task" and "Mentioned earlier", without the core links;
that the ⋯ menu holds no link; that the step without core links shows only Other and the step without links shows no
row; that the shared page's link in a sentence of the text has the page icon; that Tab reaches Other from the last
chip, Enter opens it and Escape closes it; and that at 390 px the open list stays inside the window. It saves a
screenshot per width and scheme in the work folder.

```
node tests/browser/core_links.cjs
```

It prints one line per check and exits 0 when all passed.

## Public pages: the Share menu and its link

`public_pages.cjs` checks sharing a page (SPEC API-5, API-7, API-8) against `serve_preview.py` (needing the same
things as above), at desktop light and phone dark. The test server's public address is `localhost` on the port that
Cockpit's `127.0.0.1` address uses. A page starts shared with nobody. Its Share menu sits over the page itself, fits
the screen and works from the keyboard. A phone's list row opens an unshared page rather than sharing it.

Anyone with the link asks first: cancelling leaves the page unshared, and accepting hands over the link (the
clipboard, or a phone's share sheet) and marks the page in the list. Sessions see who can open the page, but not its
link or its people. The `1 shared` button narrows the list. A second browser with no cookies then opens the link:
the page's script runs and can ask `confirm`, but it can't read cookies, save, open a window, start a download or
load anything from another website. The public address answers 404 for unshared pages and for Cockpit itself.

Specific people opens an address box. The first address shares the page with that person alone, and a visitor who
isn't signed in meets a sign-in page that doesn't name the page. A visitor signed in with an address that isn't
on the list is told so, and Sign out removes their cookie. Removing the last person shares it with nobody.
Sharing again brings back the same link and says so; Only me ends it without asking. New link ends the old link for
good. A session can stop the sharing but can't start it. Neither the Pages tab nor the public page breaks the page's
security policy. Google sign-in itself is covered by `tests/test_api.py`, with Google's answers faked.

```
node tests/browser/public_pages.cjs
```

It prints one line per check and exits 0 when all passed.

## The morning briefing opens in the layer

`briefing_overlay.cjs` checks a link to the morning briefing on a step card (SPEC NEXT-47) against `serve_preview.py`
(needing the same things as above), on `/` at desktop width and on `/classic` at phone width. The fixture stores
yesterday's briefing and has Cockpit ask for today's, which the test then posts:

- the briefing's read step does not load the briefing ahead, so it stays unread until the user opens it;
- a click on the link opens the briefing in the layer over the whole window, without Cockpit's header and tabs, with
  no new tab and the main page's address kept;
- opening it marks the briefing read and completes its read step;
- the X, Escape and the browser's Back each close the layer;
- a day in the list of earlier briefings opens in the layer, and one Back still closes it;
- a click with Ctrl leaves the main page as it is;
- a click on a task the briefing mentions closes the layer and opens that task on the main page, with the layer's
  history entry gone;
- a mention of a task closed long ago, which the page lists nowhere, opens the main page's search of closed work for
  it, and the search's address stays after the layer's Back;
- the page throws no errors and breaks no Content Security Policy rule.

Before the fix, the link opened the briefing in a new tab, with Cockpit's full header and tabs; neither Escape nor
Back closed that tab.

```
node tests/browser/briefing_overlay.cjs
```

It prints one line per check and exits 0 when all passed. A run takes about a minute.

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

## Images with any message

`attach_images.cjs` checks images sent with a message (SPEC MSG-18) against `serve_preview.py`, on a step's card at
desktop width in the light scheme and on the item's row at phone width in the dark one. Attach images shows in the
menu of the arrow beside Send. Two images picked through it wait above the box, and Remove takes one away. Send puts
the one left on the task, the message carries its id, and the box is empty of images afterwards. It saves screenshots
of the open menu and of the box with its images.

```
node tests/browser/attach_images.cjs
```

It prints one line per check and exits 0 when all passed. A run takes about a minute.

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
timer running (TRACK-6), at laptop width, against `serve_preview.py`:

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

`snooze_play.cjs` checks play on a snoozed step, at laptop and phone width, against `serve_preview.py`:

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
puts four steps, each on a new item of its own (an item shows one card at a time). It presses Next up's ask
without starting the timer, checks that J moves the highlight without holding a step and that O opens a row
and holds it, and types in the second step's reply box, then puts four urgent steps, each on an urgent item
of its own (SPEC NEXT-16). It checks that the touched steps keep
their places on the server and on the page, that the untouched steps give way, that Next up shows the
accent Resorted key whose tooltip names an urgent item, that nothing on Next up runs past its edge at
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

- with its usual marks, they sit at the card's right above the item's line, with no label before it;
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

`flash_hand_off.cjs` checks that a step the user's own send took off the page is not reported, though Cockpit has
not yet heard of the send (a regression check), on the classic and the sidebar page, on `serve_preview.py`'s
synthetic database:

- a queued step whose hand-off is held back before its request leaves the page, and the page sends no report for it,
  neither then nor once the hand-off went;
- a queued step an agent withdraws within a minute is still reported;
- the page logs no error.

```bash
PLAYWRIGHT_MODULE=/tmp/cockpit-browser-tests/node_modules/playwright node tests/browser/flash_hand_off.cjs
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

## A message on a warning

`warning_message.cjs` checks the message box on an Inbox warning (SPEC UI-82) at 1280 px light and 390 px dark,
against `serve_preview.py` with a signed-out Codex account added to each state answer and the send answered in the
browser. It checks that the fix button stays, Send is off until the box has words, a refused send keeps the warning
in Inbox and puts the words back, Ctrl+Enter sends the warning's key and the words with its title and explanation
attached, the toast offers Open, the warning leaves Inbox and the card moves on, a poll keeps it out, and it is back
once the state no longer hands it to the to-do. It saves each width's card as `warning-message-<width>.png`, and the
page after the send as `warning-message-sent-<width>.png`, in its work folder.

```bash
node tests/browser/warning_message.cjs
```

## A part's warning in Inbox

`part_warning.cjs` checks a warning that an install's part gives (the state's `part_warnings`, `src/cockpit/parts.py`)
as an Inbox warning at 1280 px light and 390 px dark, against `serve_preview.py`, which has no parts, with a made-up
part's warning added to each state answer. It checks that the row has the part's words, the opened card says what the
part said more, its one answer has the part's words and opens the part's page, nothing is wider than the window, and
the page raises no errors. It saves each width's Inbox as `part-warning-<width>.png` and the opened card as
`part-warning-card-<width>.png` in its work folder.

```bash
node tests/browser/part_warning.cjs
```

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

## Answering a Codex request

`codex_request_card.cjs` adds one Codex command request to the page's state on a synthetic database through
`serve_preview.py` (no Codex runs) and holds the answer endpoint for two seconds, at 1280 px light and 390 px dark. It
checks that Run takes the card and its Inbox row off before the server answers and that they stay off, and that a
refused Deny brings the card back with the server's reason on it (SPEC PERM-30). The page logs when the card goes off,
so a slow machine cannot blur the order; leaving-animation copies do not count as the card.

## A handed step's card while the agent works

`working_step.cjs` checks the card of a step the user handed to an agent, picked in Busy, at laptop width in light
and dark and at phone width, against `serve_preview.py`:

- the card is the handed step as it was: its title, ask, body, details and core links, with no answers, keys or menu;
- the user's words with the step head the card once, as their latest message;
- the card ends in the working panel: the agent's name, what it is doing and where the task stands, then Take back;
- the card starts no timer, and the page has no errors;
- Take back brings the step back to the queue with its answers.

```
node tests/browser/working_step.cjs
```

It prints one line per check and exits 0 when all passed. A run takes about a minute.
