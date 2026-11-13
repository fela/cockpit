"use strict";

// Cockpit's page: polls /api/state and renders it. Every text field goes in with
// textContent; only the server's *_html fields (Markdown rendered without raw
// HTML, links allowlisted) use innerHTML.

const POLL_MS = 5000;
// Back from the background, the cards stay as drawn and in use this long while the quick check asks which of them
// still stand (SPEC UI-60): longer than the check's round trip from afar to a busy host, which can take up to two
// seconds. Past it the page cannot say that its cards are current, and veils them.
const CATCH_UP_GRACE_MS = 2500;
// A tab in the background asks the quick list this often, and the whole state only when a card it draws has gone.
const HIDDEN_CHECK_MS = 30000;
// The page with the list of items at its left (VIEW-26), served at /; /classic serves the page without it (sidebar.js).
const SIDEBAR = document.body.dataset.layout === "sidebar";
// One try of an image upload may take this long on a slow connection before it is sent again.
const UPLOAD_TRY_MS = 120e3;
const RERENDER_MS = 60000;
// A press longer than this is no click (a long press to select, or a lost release): redraws go ahead.
const PRESS_HOLD_MS = 3000;
const PRESS_CLICK_WAIT_MS = 400;
const QUEUE_TOP = 6;
// The user's places (store.QUEUE_LIMIT).
const QUEUE_LIMIT = 4;
// How long Busy's folded heading stays lit after a message took a finished task back to Busy (SPEC CONTROL-28).
const REOPENED_HEADING_MS = 8000;
// The closed steps shown in Finished this week before their "Show N more" (SPEC VIEW-4).
const CLOSED_TOP = 8;
const SESSIONS_TOP = 10;
// The most time the user adds to an item at once (TRACK-7), as store.ADD_LIMIT.
const ADD_LIMIT = 24 * 3600;
// How long the page skips polled states that may already count time being added (TRACK-7, see `refresh`).
const ADDS_WAIT_MS = 30000;
// A message's delivery state, as its mark's tooltip.
const MESSAGE_STATES = {
  queued: "Queued",
  sending: "Sending",
  waiting: "Waits for the session's turn to end, or for a subagent it runs, at most 15 minutes; then Cockpit stops the session and resumes it with your message as your own prompt",
  sent: "Delivered",
  held: "In the session's queue: it reads it once the dialog in its terminal is answered",
  place: "Waits for a free place among the working sessions; Cockpit sends it when one finishes",
  failed: "Did not arrive",
};

// A message the session has in its queue behind an open dialog shows as held, not delivered; one of
// The user's that waits for the session's turn to end (MSG-3) shows as waiting; one that waits for a place among
// the working sessions (SESSION-23) shows as place.
function messageDelivery(message) {
  if (message.state === "queued" && message.waits_since) return "waiting";
  if (message.state === "queued" && message.capacity_since) return "place";
  return message.behind_dialog ? "held" : message.state;
}
// Send now on a message that waits for a place (SESSION-23): it goes past the limit and stops no session.
function sendNowButton(message) {
  return h("button", { class: "button small ghost", type: "button", onclick: () => sendNow(message) },
    icon("send"), h("span", { class: "label", text: "Send now" }));
}
// Since when a message waits for a place; from LONG_WAIT_MINUTES also how long.
function placeWords(message) {
  const minutes = Math.floor(Math.max(0, Date.now() - new Date(message.capacity_since).getTime()) / 60000);
  return `Waits since ${clock(message.capacity_since)}${minutes >= LONG_WAIT_MINUTES ? ` (${span(minutes)})` : ""} for a free place:`
    + ` at most ${ui.state.counts.working_limit} sessions work at once`;
}
// Interrupt and send on a message that waits (MSG-3): its label says that it stops the session's turn, so it needs no
// hover.
function interruptButton(message) {
  return h("button", { class: "button small ghost interrupt", type: "button", onclick: () => sendNow(message) },
    icon("send"), h("span", { class: "label", text: "Interrupt and send" }));
}
// A waiting message says since when; from LONG_WAIT_MINUTES it also says how long. Cockpit sends it itself
// after 15 minutes (MSG-7), so only a wait past LATE_WAIT_MINUTES is red: then Interrupt and send is the user's to press.
// A late wait's sentence leads with a red mark, the alert and how long, and the sentence itself stays quiet.
const LONG_WAIT_MINUTES = 10;
const LATE_WAIT_MINUTES = 20;

function waitWords(message) {
  const minutes = Math.floor(Math.max(0, Date.now() - new Date(message.waits_since).getTime()) / 60000);
  const late = minutes >= LATE_WAIT_MINUTES;
  const long = minutes >= LONG_WAIT_MINUTES && !late;
  return { late, span: span(minutes), since: `since ${clock(message.waits_since)}${long ? ` (${span(minutes)})` : ""}` };
}

function lateMark(wait) {
  if (!wait || !wait.late) return null;
  return h("span", { class: "late-mark", title: `Waiting ${wait.span}; Interrupt and send sends it now` },
    icon("alert"), wait.span);
}

// A failed message someone set aside (MSG-5): its text reached the session another way, or it is no
// longer needed. Not a failure the user has to act on, so never red.
function setAsideWords(message) {
  return `Cockpit did not send it; ${actorWords(message.dismissed_by)} set it aside at ${clock(message.dismissed_at)}`
    + (message.dismiss_reason ? `: ${message.dismiss_reason}` : "");
}
// A line-2 row shows this many tags; "+N" stands for the rest.
const ROW_TAGS = 2;
const TOAST_MS = 8000;
// Phones get the one-row action bar; touch screens get no key hints.
const PHONE = window.matchMedia("(max-width: 640px)");
const TOUCH = window.matchMedia("(hover: none)");
// Who the work waits for (SPEC VIEW-2), in the order of store.BALLS.
const BALLS = [
  ["you", "Waiting for you"],
  ["people", "Waiting for another person"],
  ["bots", "Waiting for a bot"],
  ["merge", "Waiting for a merge to the next stage"],
  ["agents", "With agents"],
  ["nobody", "No one has the next move"],
  ["done", "Finished this week"],
];
// The groups below the queue (SPEC VIEW-27), in the order of store.GROUPS: Busy, the agents' work while it moves, and
// Waiting, the rest of the open work, each row with a short hover line (`workTip`). Work that waits for the user has
// no group there: it shows as its steps in the queue. All start folded.
const GROUPS = [
  ["you", "Waiting for you"],
  ["busy", "Busy"],
  ["waiting", "Waiting"],
  ["done", "Finished this week"],
];
const GROUPS_COLLAPSED = new Set(["busy", "waiting", "done"]);

// A row's hover line in Busy or Waiting (SPEC VIEW-27): what the agent works on, or what the item waits for, with its
// time in the user's own zone, the hour today and the date on another day: "its session resumes 3 Nov".
function workTip(item) {
  if (!item.tip) return "";
  if (!item.tip_at) return item.tip;
  const date = new Date(item.tip_at);
  const day = date.toDateString() === new Date().toDateString() ? clock(item.tip_at)
    : `${date.getDate()} ${date.toLocaleDateString([], { month: "short" })}`;
  return `${item.tip} ${day}`;
}
const PATTERN_DAYS = 14;
const STAGE_LABELS = {
  planning: "planning", building: "building", review: "in review",
  dev: "on dev", staging: "on staging", prod: "in prod", done: "done",
};
const SEEN_KEY = "cockpit.seenBefore";
const DRAFTS_KEY = "cockpit.drafts";
// The target each draft was written to (a composer's choice), kept with the drafts.
const TARGETS_KEY = "cockpit.draft-targets";
const STARTS_KEY = "cockpit.draft-starts";
// The rule each rule draft changes in place (PERM-11), kept with the drafts.
const REPLACING_KEY = "cockpit.rule-replacing";
const SENT_KEY = "cockpit.sent";
// What the page had open when the user pressed Reload (see reloadKeepingPlace).
const REOPEN_KEY = "cockpit.reopen";
// An item's priority, in the order a click on its tag steps through (right click goes back).
const PRIORITIES = ["normal", "high", "urgent", "low"];
// How often, at most, the page tells the server the user is on a permission card (PERM-9).
const ACTIVE_EVERY_MS = 60e3;
// How long the page counts a step's removal from the queue as the user's after the user archived its item,
// for the server's next state to move it out.
const REMOVED_MS = 60e3;
// The hand-off flight: motion.css's --dur-move and --ease-out, the page's move.
const FLIGHT_MS = 240;
const EASE_OUT = "cubic-bezier(0.23, 1, 0.32, 1)";
const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)");

const reopened = takeReopened();

const ui = {
  state: null,
  // Cockpit's time zone and any question about the browser's (zones.state, SPEC CONTROL-32), from the last report.
  zone: null,
  // The composers a poll left in place this redraw (render).
  heldKeys: new Set(),
  fingerprint: "",
  usagePrint: "",
  // When the page last drew (Date.now(); 0 before the first render).
  renderedAt: 0,
  lastOk: null,
  // When the page asked for the state it holds (ms; 0 before the first), which may be well before it came.
  stateAskedAt: 0,
  // While the page catches up after the tab comes back (SPEC UI-60): when it started (ms), else null.
  catchUpFrom: null,
  // The parts that stay in view meanwhile because the user types in them.
  catchUpKept: [],
  // The timer that veils the parts once the grace (CATCH_UP_GRACE_MS) passed without the quick check's answer.
  catchUpVeil: null,
  // Set once every part that waits is veiled: the quick check failed, or did not answer within the grace.
  catchUpAll: false,
  // The quick check's answer when it named cards that left: those cards are veiled, the others stay in use.
  catchUpListed: null,
  // Set while a tab in the background asks the quick list.
  hiddenCheck: false,
  // The quick list a background check last fetched the whole state for.
  hiddenListed: "",
  // Set when a catch-up ended on a failed state: the cards it showed again are old, so the banner says so at once.
  catchUpFailed: false,
  // The server's clock less the browser's, in ms, as of the last state.
  clockOffset: 0,
  error: "",
  // The queue order the last redraw drew (step ids).
  order: [],
  // The queue steps the user opened, answered in, is on or paused on (NEXT-16): step id → the place the page
  // draws it at. A step takes the place it shows at when the user presses or types in it or its timer starts,
  // and keeps it until the user presses Resorted or it leaves the queue; it moves up as steps above it leave,
  // never down. The other steps follow the server's order as it changes.
  placed: new Map(reopened.placed),
  // Per queue step: its hold on the server, asked or granted, with the place time it is for (holdPlace); it goes
  // when the step leaves the queue or comes back with a new place time (placeSteps).
  holdAsks: new Map(),
  // Steps the page placed in a place the server has not given them yet (a freed place `refilled` fills before the
  // close's answer comes): id → whether this press placed it. Their hold is asked once the server queues them.
  holdLater: new Map(),
  next: null,
  // On the sidebar page (SPEC UI-52): the warning the user opened on the card, the one the card shows, and the
  // queue step whose row the warnings' rows follow (renderSelected).
  warningOpen: null,
  shownWarning: null,
  // The warnings that waited when the step on Next up came up there, and each one that came after, in the order
  // they came: {stepId, came: Map(key → 0 for one already there, then 1, 2, …), count} (`warningsSince`).
  warningsSeen: null,
  // A sign-in waiting for the code its page shows, on its warning (waitForCode).
  signIn: null,
  expanded: new Set(reopened.expanded),
  // Inbox's extra steps fold again after a reload: folded is their default (SPEC VIEW-30).
  open: new Set(reopened.open.filter((key) => key !== "sidebar:more")),
  // The snooze time a warning's menu asks to confirm, by the menu's key (`warningMenu`): an index into snoozeTimes().
  confirmSnooze: new Map(),
  // Warning snoozes on their way to Cockpit (`setWarningSnooze`): key → { until (null to unsnooze), confirmed }. One stays
  // until a state asked after Cockpit confirmed it (`refresh`), so an older poll does not undo it on the page.
  snoozing: new Map(),
  // Messages on warnings on their way to Cockpit (`warningMessageBox`): key → { confirmed }. One stays until a state
  // asked after Cockpit confirmed it (`refresh`), so an older poll does not bring the warning back.
  handing: new Map(),
  closed: new Set(reopened.closed),
  // Drafts of the step feedback VIEW-29 removed ("fb:step:<id>") are dropped: nothing shows or sends them any more.
  drafts: new Map(readStored(DRAFTS_KEY, []).filter(([key]) => !String(key).startsWith("fb:step:"))),
  choices: new Map(readStored(TARGETS_KEY, [])),
  // Per draft box: the height the user dragged its corner to, so a redraw draws the box at that height again.
  fieldHeights: new Map(),
  // Per box: what a new session from it starts with (startPicks), kept with the box's draft. A send takes the
  // picks with it, and a refused send puts them back with its words.
  starts: new Map(readStored(STARTS_KEY, [])),
  // The side chat questions the last state showed still asking, null before the first state (MSG-11).
  sideAsking: null,
  busy: new Set(),
  // The words of a Priorities save the server refused, which the next edit starts from; null otherwise.
  goalsUnsaved: null,
  inboxUnsaved: null,
  // The settings a save is sending, and a save made while it is on its way, which goes once it is done.
  inboxSaving: null,
  inboxNext: null,
  // The save on its way and what follows it, which a save made meanwhile holds the page open for (holdLeave).
  inboxSent: null,
  // Priorities saved while the last save was on its way, which go once it is done; null otherwise.
  goalsNext: null,
  goalsSent: null,
  // The user's last press on Update until what it started is over (SPEC SWEEP-6), or null.
  updateRun: null,
  showAllQueue: false,
  showAllClosed: false,
  // The items a part keeps off the main page that were opened here by their id.
  openedHidden: new Set(),
  // Set by a press on a step's size bar until Your time has scrolled to the estimates (SPEC NEXT-35).
  toEstimates: false,
  showAllSessions: false,
  seenBefore: readStored(SEEN_KEY, null),
  pendingRender: false,
  // When the user pressed a pointer that has not yet clicked (performance.now()), or null; redraws wait for the click.
  pressedAt: null,
  // What that press went down on, or null: a new toast waits while it is in the shown one (`toast`).
  pressedOn: null,
  // Whether a pointer is down, perhaps still drawing a text selection: the Comment button waits for its release.
  selecting: false,
  releaseTimer: null,
  // Per permission request id: why the server refused the user's last answer, shown on the card.
  requestErrors: new Map(),
  // The team typed into the first-use question so far (see `teamAsk`).
  teamDraft: "",
  // Per figure frame's address: the height its page last reported (see `fitFigure`).
  figureHeights: new Map(),
  // The addresses of the figure pages too tall for a frame, which show as a link to the page (see `linkFigure`).
  figureLinked: new Set(),
  // Set by the user's own actions: the next render redraws every section, even the one they are in.
  actionRender: false,
  // What the last render drew from (see `drawnPrint`): an action's confirming state that
  // matches it needs no second redraw.
  drawnPrint: "",
  // Since when the page has read the state without a long gap (`refresh`).
  watchedSince: null,
  refreshing: false,
  refreshAgain: false,
  toastTimer: null,
  // The number the last toast got, and the number of the toast shown.
  toastCount: 0,
  toastShown: 0,
  // The shown toast's time left in ms, counted from `toastSince` while its timer runs.
  toastLeft: 0,
  toastSince: 0,
  // What runs once the shown toast leaves without its button pressed (`toast`'s `action.gone`), or null.
  toastGone: null,
  // The toast that waits while the shown one's Add more box is open, or while the user presses in the shown one
  // ({id, text, fail, action, tip, setups, forPress}), or null.
  toastWaiting: null,
  // Shows a toast that waits for a press once PRESS_HOLD_MS has passed since the press went down (`toast`).
  toastPressTimer: null,
  // What `addToToast` added to each toast, by its number, {words, tip}: `retoast` keeps it when it rewords that toast.
  toastAdded: new Map(),
  // Set when a redraw removed the box the user was typing in: keys they go on
  // typing must not trigger the d/x/z shortcuts. Cleared by their next click or focus.
  focusLost: false,
  // True while a redraw puts keyboard focus back on the control it replaced: that focus is not the user
  // working on a step or a card.
  refocusing: false,
  // The step the user last paused on this page, which the page then does not start again on its own (TRACK-6).
  pausedStepId: null,
  // The step whose timer stopped because the user showed another item (leaveTimedStep), until the card stops
  // showing it; it then starts on its own when it shows again (startOpened).
  leftStepId: null,
  // Activity on the page (TRACK-5): the user's last input on it while it had focus (ms, 0 before any); the last
  // second the page was on screen (ms, 0 before any); the step
  // the page last started on its own as the user opened it; the question after a stretch without activity (the
  // state's `away`) while it shows; the stretch the page saw, until it read a state asked after (`idleGap`); the
  // page's last stop of the timer for the stretch, which settles to its question's id, and whether it is on its way;
  // and the first and last input before the page's first state (ms; null for none).
  earlyInput: null,
  // The user's latest input on this page after a stretch without activity on this device (`AWAY_MS`): when it came,
  // and the device's last activity before it (ms). Null before any.
  back: null,
  // The question this page last answered with Return on its own (`returnUnasked`), by its step and start.
  returned: null,
  stopping: false,
  lastInput: 0,
  lastSeen: 0,
  // When this page last told Cockpit it was on screen with the timer running (ms; 0 before any, or after a failed try).
  seenReported: 0,
  // The counts of the user's inputs that this page's own presence reports, starts and answers made (`input_count`),
  // each with when Cockpit's answer came (ms): the latest few.
  ownInputs: [],
  autoStarted: null,
  idle: null,
  gap: null,
  idleStop: null,
  patterns: null,
  patternsError: "",
  time: null,
  timeError: "",
  // The Mac's record (TRACK-9): today's summary for "Your time", its error, the Terminal line a new key came
  // with (shown until the panel reloads), and per item where its timer time went on the Mac (TRACK-10).
  mac: null,
  macError: "",
  macInstall: "",
  activity: new Map(),
  permissions: null,
  permissionsError: "",
  // Picking a part of the page: null, "feedback" (feedback mode) or "add" (for the Add box); and the
  // part picked for feedback ({el, from, target, quote, step_id, item_id}).
  picking: null,
  pick: null,
  // The last pick's click: {x, y, parts, index}, the guesses at that spot and the one shown (pick.js).
  guess: null,
  // While the user writes feedback (TRACK-2): {since, stepId}, the step whose timer waits, or null.
  feedbackHold: null,
  // The user's changes the page shows before the server confirms them (see `optimistic`).
  pending: new Map(),
  pendingCount: 0,
  // Numbers the moments a change was confirmed and a state was asked for, in the order they came;
  // performance.now() reads the same for both too often to order them.
  tick: 0,
  // Per step id: the action or hand-off on its way to the server, which an undo of it waits for.
  inFlight: new Map(),
  // Per session id: the hand-offs to it on their way to the server (each one's settling promise), which a message
  // sent to that session after them waits for (`sendMessage`), so the message reaches the session after them.
  handingOff: new Map(),
  // Per step id, the last whole-step Good writing or Hard to understand mark sent, from its card or the pick box: the
  // next goes after it, so the server keeps the verdict pressed last.
  writingSent: new Map(),
  // The last timer press on its way to the server: the next press goes once it went through or failed. It
  // resolves to {stepId, press} when the last start since a pause that went through was a start (`press`: its
  // id), else null.
  timerSent: Promise.resolve(null),
  // Numbers the timer presses, so each is its own action and none waits for or drops another (see `sendTimer`).
  timerPresses: 0,
  // Items the user completed or closed whose finish is on its way: their lamps stay off the status lights.
  finishing: new Set(),
  // Per item: its latest change on its way, which the next one waits for, and its latest archive or
  // unarchive on its way (see `patchItem`).
  patching: new Map(),
  archiving: new Map(),
  // Per step: the last message the user sent from its composer ({text, at, version}), shown as a
  // small mark until the agent rewrites the step.
  sent: new Map(readStored(SENT_KEY, [])),
  // A counter for the page's local ids (`local-<n>`) and the keys of its runs.
  sendCount: 0,
  // Per permission card: when the page last told the server the user is on it (Date.now()).
  requestActiveAt: new Map(),
  // Set once a failed report showed its toast; later failures only go to the console.
  activeWarned: false,
  // The steps the last render drew open, with an unsent draft, timed or placed: id → {step, list, index}.
  held: new Map(),
  // Held steps that then left the lists: they stay drawn, dimmed, until the user closes them.
  left: new Map(),
  // Steps the user took out of the queue on the page (closed, snoozed, handed off, or their item
  // archived): these leave without a kept row. Step id → when (Date.now()).
  removedByUser: new Map(),
  // The same steps for flashes.js, kept for a minute even once they are out of the queue (`removedByUser` goes then):
  // a card the user's press took away is no flash, though Cockpit may not have heard of the press yet (PERM-24).
  // Step id → when (performance.now()).
  tookAway: new Map(),
  // The steps the user's press took out of the queue, each with the server's time of the state the press was made on
  // (no later than the time Cockpit took it out): an Undo sends back only steps placed since (`givenBack`).
  leftAt: new Map(),
  // Per step id: the words the page first drew for it, so an agent's rewrite shows as an update.
  seenText: new Map(),
  // Permission cards whose rule editor the user closed: the top card's editor stays closed for them.
  closedRules: new Set(),
  // Per rule draft key: the rule the draft changes in place (PERM-11), when the user pressed its Edit.
  replacing: new Map(readStored(REPLACING_KEY, [])),
  // The Add box under the header: whether it shows, and the images pasted into it that wait for
  // Enter ([{file, url}], `url` a local blob).
  adding: false,
  addImages: [],
  // The part of the page attached to the to-do in the Add box (CONTROL-9), shaped like `pick`, or null.
  addPart: null,
  // True while Feedback's pick runs before the Add box opens (openAddWithPart).
  addPartFirst: false,
  // Whether the Add box was opened with a click or tap (`pointerLast`), so closing it gives no key the focus.
  addByPointer: false,
  // Whether the to-do in the Add box starts in the Pinned list (CONTROL-13).
  addPinned: false,
  // The priority picked in the Add box (CONTROL-10), or null for none: then words such as "urgent" in the text set it.
  addPriority: null,
  // The to-do the Add box adds more words to (CONTROL-18), {id, title}, or null when it makes a new item.
  addTo: null,
  // The to-dos sent with Add in the last JUST_ADDED_MS, or all while one is on its way, oldest first: {added, title,
  // known, at}, `added.id` the local id until the server answers, `known` the ids the page listed before the Add
  // (addedItemIn).
  addsSent: [],
  // Adds not sent yet, by the new to-do's local id, while their Undo is on offer (UNDO_ADD_MS): {at, send, undo}.
  addsHeld: new Map(),
  // Closes whose Undo sits in Finished this week (`closedNotice`), while it is on offer: {at, itemIds, timer, undo, end}.
  closesHeld: new Set(),
  // Per step with a list of tasks to close (`closesBlock`): the ids of the tasks the user unticked to keep open.
  closesSkip: new Map(),
  // Busy's folded heading says a message took a finished task back to Busy (showReopened).
  reopenedHeld: null,
  // Hand-offs whose Undo sits in Busy (`handedNotice`), while it is on offer: {at, stepId, to, timer, undo, end, drop}.
  handsHeld: new Set(),
  // The interval that counts the held rows' seconds down (sidebar.js `tickUndo`), or null.
  undoTick: null,
  // The to-do just added, whose row the list at the left scrolls into view once (sidebar.js `showArrived`), or null:
  // {sent, id, scrolled}, `sent` its entry in addsSent, `id` its local id.
  arrived: null,
  // Images on their way up, per item id: [{id: "local-<n>", url, savedId}], shown from the local
  // blob until the state lists the saved image (`savedId`, set once the server took it).
  uploading: new Map(),
  // Images waiting in a message box to go with its words (SPEC MSG-18), by the box's key: entries as `uploading`'s,
  // their `savedId` and `savedOn` (the item) set once uploaded, so a refused Send does not upload them twice.
  composerImages: new Map(),
  // Adds still running, by the new item's id: Add more on that item waits for its Add, whose first message carries
  // the text it was made with (CONTROL-18).
  addsRunning: new Map(),
  // Counts each time the user went to an item or a session, so a click that opens an item later knows whether the
  // user went elsewhere meanwhile (`openWhenMade`).
  navigations: 0,
  // Image ids the user removed on the page.
  removedImages: new Set(),
  // The open lightbox: {urls, at}, the images it steps through and the one it shows (`openLightbox`); null when closed.
  lightbox: null,
  // Where a one-finger touch on the lightbox began, for its swipe.
  lightboxTouch: null,
  // Blocked cards past the first three show only after "+N more".
  showAllBlocked: false,
  showMovedPast: false,
  // The Cockpit that sent this page (UI-13): a state from another one shows the Reload button.
  version: document.querySelector('meta[name="cockpit-version"]')?.content || null,
  // Messages the user sent again: they stay in Messages' short list while they go out.
  retried: new Set(),
  // Sessions the user restarted from a dialog (PERM-8): session UUID to the message that resumes it, null
  // while the server has not answered yet.
  restarts: new Map(),
  // Sessions whose Restart found no dialog left (PERM-8): session UUID to when, so the card leaves at once.
  dialogsGone: new Map(),
  // Items' histories (VIEW-10): item id → { data, version, at, loading, error }, fetched when drawn.
  histories: new Map(),
  // Top cards this page reported as seen (`markTopCardsSeen`); a failed report takes its card out again.
  topSeen: new Set(),
};
// The side column's feedback box is gone (FEEDBACK-9): words left in it carry on in the pick box.
if (ui.drafts.get("fb:general")) {
  ui.drafts.set("fb:pick", [ui.drafts.get("fb:pick"), ui.drafts.get("fb:general")].filter(Boolean).join("\n\n"));
}
ui.drafts.delete("fb:general");

// Storage is a per-browser convenience (drafts, the "new" marker); the page works without it.
function readStored(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    return raw === null ? fallback : JSON.parse(raw);
  } catch {
    return fallback;
  }
}

function writeStored(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Private windows and blocked storage: drafts then live only in this tab.
  }
}

// Read once at load: what the page had open before it reloaded itself, so it opens the same rows
// with the same steps placed in the queue.
function takeReopened() {
  const none = { expanded: [], open: [], closed: [], y: null, placed: [] };
  try {
    const raw = sessionStorage.getItem(REOPEN_KEY);
    sessionStorage.removeItem(REOPEN_KEY);
    return raw === null ? none : { ...none, ...JSON.parse(raw) };
  } catch {
    return none;
  }
}

function saveDrafts() {
  writeStored(DRAFTS_KEY, [...ui.drafts].filter(([, text]) => text));
  // The list at the left marks an item holding a draft; typing, sending and a refused send's words coming back all pass here.
  if (typeof showItemDraftMarks === "function") showItemDraftMarks();
  // A blocked call's card keeps its list choice under the card, its drafts under the card and list.
  writeStored(TARGETS_KEY, [...ui.choices].filter(([key, choice]) =>
    ui.drafts.get(key) || ui.drafts.get(`${key}:${choice}`) || ui.replacing.has(`${key}:${choice}`)));
  // A new session's picks, also for a box with no words yet; sending its words drops them (takeDraft).
  writeStored(STARTS_KEY, [...ui.starts]);
  // A rule edit's target stays while its call is open: an answer sent and left by a reload that then fails puts
  // the words back, and they still change that rule.
  const open = ui.state && new Set([...ui.state.permission_requests, ...(ui.state.moved_past || [])].map((request) => request.id));
  writeStored(REPLACING_KEY, [...ui.replacing].filter(([key]) => ui.drafts.get(key) || !open || open.has(Number(key.split(":")[2]))));
}

// After a send, clear the draft only if the user has not typed more since: what
// they added while the request ran stays in the box. The redraw after the
// action then shows the box empty or with their addition.
function clearDraft(key, sent) {
  if (ui.drafts.get(key) !== sent) return;
  ui.drafts.delete(key);
  ui.choices.delete(key);
  saveDrafts();
}

// A send that the server refused puts its words back, ahead of anything the user typed since, also in another
// tab where the user types in that box (`tellPutBack`).
function restoreDraft(key, sent) {
  const now = (ui.drafts.get(key) || "").trim();
  ui.drafts.set(key, now ? `${sent.trim()}\n\n${now}` : sent);
  saveDrafts();
  // An open Add box, which render leaves alone, gets the words too, or typing there would write over them. A composer
  // draws them from ui.drafts; an input event there would also record its To before restoreChoice puts the sent one back.
  const area = key === ADD_KEY ? document.querySelector(`#add-box textarea[data-key="${ADD_KEY}"]`) : null;
  if (area && area.value.trim() === now) {
    const top = now ? `${sent.trim()}\n\n` : sent;
    if (window.CockpitEditor) window.CockpitEditor.prepend(area, top);
    else {
      area.value = top + area.value;
      area.dispatchEvent(new Event("input", { bubbles: true }));
    }
  }
  tellPutBack(key, sent, ui.drafts.get(key));
}

// The "To" choice a refused or put-back send was made with goes back with its words, so Send goes where it went
// before; a choice the user made since stays.
function restoreChoice(key, choice) {
  if (!choice || ui.choices.has(key)) return;
  ui.choices.set(key, choice);
  saveDrafts();
}

// The box writes to another "To" than `choice` since: Send there would take words meant for `choice` to that one.
function writesElsewhere(key, choice) {
  const since = ui.choices.get(key);
  return Boolean(choice && since && since !== choice);
}

// A box's "To" choice and a new session's picks go back, unless the box writes to another To since: then it keeps
// what it holds, and this returns false, as `putBack` does. Picks the user made since stay.
function restoreTarget(key, { choice, picks }) {
  if (writesElsewhere(key, choice)) return false;
  restoreChoice(key, choice);
  if (picks && !ui.starts.has(key)) {
    ui.starts.set(key, picks);
    saveDrafts();
    drawStartLines();
  }
  return true;
}

// A refused send's words and "To" choice go back into their box, unless the box writes to another To since,
// also when the user emptied it again: Send would take the words to that one. Then the box keeps what it holds,
// the refusal's notice keeps the refused words (`NOT_PUT_BACK`), and this returns false.
const NOT_PUT_BACK = "The box now writes to another session, so your words stay in this notice; hover to read them.";
// `picks`: what a new session from the box was to start with (startPicks), or null; picks made since stay.
function putBack(key, sent, choice, picks) {
  if (writesElsewhere(key, choice)) return false;
  const restoring = picks && !ui.starts.has(key);
  if (restoring) ui.starts.set(key, picks);
  restoreDraft(key, sent);
  restoreChoice(key, choice);
  // An open box, such as Add, is not drawn again by render: its line shows the picks Send now uses.
  if (restoring) drawStartLines();
  return true;
}

// Every start line draws again from ui.starts. Copies made for previews and closing animations have no draw.
function drawStartLines() {
  for (const holder of document.querySelectorAll(".start-options")) {
    if (typeof holder.draw === "function") drawStartLine(holder);
  }
}

// One start line draws again, its focused button, or the button of the list focus is in, keeping focus.
function drawStartLine(holder) {
  const focused = holder.contains(document.activeElement) ? document.activeElement : null;
  const button = focused && focused.closest(".start-pick")?.querySelector(":scope > .start-button");
  const again = focused && (button ? `[data-key="${CSS.escape(button.dataset.key)}"]` : ".start-button");
  holder.draw();
  // A control the new picks took away (Account, once the agent is Codex) hands focus to the line's first one; a
  // line still waiting for the page's state has none.
  if (again) (holder.querySelector(again) || holder.querySelector(".start-button"))?.focus();
}

// An open list holds back the page's keyboard shortcuts, so the lists of a line that hides close, also in ui.open
// so the next box drawn does not open them again. Whether one was open, so Escape closes it before the box around it.
function closeStartPicks(holder) {
  const open = [...holder.querySelectorAll(".start-pick[open]")];
  for (const each of open) {
    ui.open.delete(each.dataset.menu);
    each.open = false;
  }
  return open.length > 0;
}

// What the page offers a new session, as drawn: a start line drawn with other choices draws again (render).
function startOffers(state) {
  return JSON.stringify([state.session_options, state.session_accounts, state.default_provider]);
}

function saveSent() {
  // A sent mark's link to the send before it, whether Cockpit refused it, and its notice live only in this page.
  writeStored(SENT_KEY, [...ui.sent].map(([id, { earlier, refused, notice, ...mark }]) => [id, mark]));
}

// DOM

function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [name, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (name === "class") el.className = value;
    else if (name === "text") el.textContent = value;
    else if (name === "html") { el.innerHTML = value; Icons.markMentions(el); markPulls(el); }
    else if (name === "value") el.value = value;
    // The main page's policy blocks style attributes, while styles set through the element itself still apply.
    else if (name === "style") el.style.cssText = value;
    else if (name.startsWith("on")) el.addEventListener(name.slice(2), value);
    else el.setAttribute(name, value === true ? "" : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

// Icons: icons.js draws them for the board and the spec page.
const { icon } = Icons;

// A button that shows only an icon: its words are the tooltip and what a screen reader says.
function iconButton(name, words, props) {
  return h("button", { ...props, class: `${props.class} icon-only`, type: "button", title: words, "aria-label": words.split("\n")[0] }, icon(name));
}

function fill(id, ...nodes) {
  document.getElementById(id).replaceChildren(...nodes.flat(Infinity).filter(Boolean));
}

// A <details> keeps the user's choice across redraws: `save(open)` runs on the click on its summary,
// before the browser's toggle event. A redraw can run between the two (a press on a step redraws
// it), and with the toggle event alone it drew the details with the old state, then opened it
// unasked on the redraw after. The toggle event still saves a change made without that click (a
// menu closed by a click elsewhere).
function rememberOpen(el, save) {
  el.addEventListener("click", (event) => {
    const summary = event.target.closest("summary");
    if (event.defaultPrevented || !summary || summary.parentElement !== el
      || event.target.closest("a, button, input, select, textarea, label")) return;
    save(!el.open);
  });
  el.addEventListener("toggle", () => save(el.open));
}

function disclosure(key, cls, summary, body, openByDefault) {
  const el = h("details", { class: cls });
  el.open = ui.open.has(key) || (openByDefault && !ui.closed.has(key));
  rememberOpen(el, (open) => {
    if (open) { ui.open.add(key); ui.closed.delete(key); } else { ui.open.delete(key); ui.closed.add(key); }
  });
  el.append(h("summary", {}, summary), ...[body].flat(Infinity).filter(Boolean));
  return el;
}

function sectionHead(title, ...rest) {
  return h("div", { class: "section-head" }, h("h2", { text: title }), rest);
}

// Time

function ago(iso) {
  if (!iso) return "";
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 172800) return `${Math.floor(seconds / 3600)} h ago`;
  return `${Math.floor(seconds / 86400)} d ago`;
}

// How fresh a moment is (VIEW-19): 1 up to two minutes (also a moment the server's clock puts ahead of the browser's),
// falling on a log scale to 0 at an hour; null from an hour on and for no time.
function freshness(iso) {
  const minutes = (Date.now() - new Date(iso).getTime()) / 60000;
  if (Number.isNaN(minutes) || minutes >= 60) return null;
  if (minutes <= 2) return 1;
  return 1 - Math.log(minutes / 2) / Math.log(30);
}

// A history entry's age, on a green that fades as the entry gets older; `tickTimers` keeps both current.
function histWhen(iso) {
  return showFresh(h("span", { class: "hist-when", "data-time": iso, "data-ago": iso, text: ago(iso) }));
}

function showFresh(el) {
  const fresh = freshness(el.dataset.ago);
  el.classList.toggle("fresh", fresh !== null);
  el.style.setProperty("--fresh", fresh === null ? "0" : fresh.toFixed(3));
  return el;
}

// The short age a row shows ("now", "33m", "18h", "2d"); the exact time is in its tooltip.
function age(iso) {
  if (!iso) return "";
  const minutes = Math.floor(Math.max(0, Date.now() - new Date(iso).getTime()) / 60000);
  if (minutes < 1) return "now";
  if (minutes < 60) return `${minutes}m`;
  if (minutes < 2880) return `${Math.floor(minutes / 60)}h`;
  return `${Math.floor(minutes / 1440)}d`;
}

function span(minutes) {
  if (minutes < 60) return `${minutes} min`;
  if (minutes < 2880) return `${Math.floor(minutes / 60)} h`;
  return `${Math.floor(minutes / 1440)} d`;
}

const TIME_FORMAT = { hour: "2-digit", minute: "2-digit", hourCycle: "h23" };

function clock(iso) {
  return iso ? new Date(iso).toLocaleTimeString([], TIME_FORMAT) : "";
}

function when(date) {
  const sameDay = date.toDateString() === new Date().toDateString();
  const time = date.toLocaleTimeString([], TIME_FORMAT);
  return sameDay ? time : `${date.toLocaleDateString([], { weekday: "short" })} ${time}`;
}

function isoUtc(date) {
  return date.toISOString().replace(/\.\d{3}Z$/, "Z");
}

// The snooze choices in a step's menu (VIEW-29) are 5m, 2h, 12h and 3d; "Other time…" takes the user's own words
// and snoozes at once while Cockpit reads them (`openSnoozeWords`). Z snoozes Next up for SNOOZE_KEY_HOURS.
const SNOOZE_KEY_HOURS = 2;
function snoozeTimes() {
  const now = Date.now();
  return [
    ["5 minutes", new Date(now + 5 * 60e3)],
    ["2 hours", new Date(now + SNOOZE_KEY_HOURS * 3600e3)],
    ["12 hours", new Date(now + 12 * 3600e3)],
    ["3 days", new Date(now + 3 * 86400e3)],
  ];
}

// API

function jsonRequest(method, body) {
  return {
    method,
    headers: body === undefined ? {} : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
  };
}

async function api(method, path, body) {
  const options = jsonRequest(method, body);
  // A read waits through a restart of Cockpit; an action is sent again until Cockpit has it (CODE-5).
  return answer(method === "GET" ? await readThroughRestart(path, options) : await sendAction(path, options, TRY_MS, null));
}

// An action that carries words the user wrote in a box: `words` is {key, text}, the box's draft key and its
// text as sent. When a closed or reloaded page left the action behind and Cockpit then does not take it, the
// words go back into that box (errors.js, CODE-5), with their "To" choice and their new session's picks.
async function apiWritten(method, path, body, words) {
  return answer(await sendAction(path, jsonRequest(method, body), TRY_MS,
    { store: DRAFTS_KEY, targets: TARGETS_KEY, starts: STARTS_KEY, ...words }));
}

// A Claude account's sign-in code: a credential that works once, so it is sent once from this page
// and never kept in the browser's storage for a later try the way other actions are (CODE-5).
async function apiCredential(path, body) {
  const response = await fetch(path, jsonRequest("POST", body));
  return answer({ response, data: await answerJson(response) });
}

// A timer's start, sent as the action `press` with the JSON body {press}, which Cockpit does once however often
// it arrives (actions.py): like a read, it waits through a restart of Cockpit or a connection that is not back
// yet, and is tried again until `giveUpAt` (a `performance.now()`). `pressed`: `awayClock()` at the press (errors.js).
async function apiStart(path, press, giveUpAt, pressed) {
  const headers = { "content-type": "application/json", "x-cockpit-action": press };
  const options = { method: "POST", credentials: "same-origin", headers, body: JSON.stringify({ press }) };
  return answer(await readWithin(path, options, giveUpAt, pressed));
}

// A timer's pause, which Cockpit applies at the time of the press, `pressedAt` (a Date.now()), whenever it
// arrives: tried until Cockpit answers (errors.js). `startPress`: as in store.pause_timer_at.
async function apiPressed(path, pressedAt, startPress) {
  return answer(await sendPressed(path, pressedAt, startPress));
}

// An image (a File or Blob) as the raw request body, with its own type as Content-Type.
async function upload(path, blob) {
  return answer(await sendAction(path, {
    method: "POST",
    headers: { "content-type": blob.type },
    body: blob,
    credentials: "same-origin",
  }, UPLOAD_TRY_MS, null));
}

// The answer boot.js asked for before the page's scripts loaded (UI-39), taken once; later reads ask Cockpit again.
async function bootOr(name, path) {
  const early = window.CockpitBoot?.[name];
  if (!early) return api("GET", path);
  delete window.CockpitBoot[name];
  return answer(await early);
}

// Keep the server's state apart from local optimistic edits. A checkpoint is used only with the exact state
// it describes; a restart or an expired checkpoint sends a complete state. The checkpoint stays in memory.
let stateSnapshot = null;
let stateCursor = null;
async function readState(path) {
  const early = window.CockpitBoot?.state;
  if (early) delete window.CockpitBoot.state;
  const options = jsonRequest("GET", undefined);
  options.headers["x-cockpit-sync"] = "1";
  if (stateCursor) options.headers["x-cockpit-since"] = stateCursor;
  const result = early ? await early : await readThroughRestart(path, options);
  const data = answer(result);
  const base = result.response.headers.get("x-cockpit-base");
  if (base && (base !== stateCursor || !stateSnapshot)) {
    stateSnapshot = null;
    stateCursor = null;
    throw new Error("Cockpit sent changes for a different state; fetching the full state again");
  }
  const next = base ? { ...stateSnapshot, ...data.changes } : data;
  if (base) for (const key of data.removed) delete next[key];
  // The rest of the page can apply local edits without changing this checkpoint's data.
  stateSnapshot = structuredClone(next);
  stateCursor = result.response.headers.get("x-cockpit-state");
  return next;
}

function answer({ response, data }) {
  if (response.status === 401) {
    location.href = "/auth/google";
    throw Object.assign(new Error("Signed out"), { status: 401 });
  }
  if (!response.ok) {
    const error = new Error(data.detail || `Cockpit answered ${response.status}`);
    // The status, for callers that expect one refusal (a 409 for a step that closed meanwhile), and whether
    // Cockpit stopped during the action, so it may or may not have happened (actions.CUT_OFF).
    error.status = response.status;
    error.cutOff = Boolean(data.cut_off);
    throw error;
  }
  return data;
}

// A refusal in the user's words: what did not happen, then why; the server's own words go in the tooltip.
function refused(what, error) {
  const closed = /^step \d+ is (\w+)$/.exec(error.message);
  const why = closed ? `the step was already ${closed[1]}` : error.message.replace(/^(step|item|message|image) \d+ /, "the $1 ");
  return Object.assign(new Error(`${what}: ${why}`, { cause: error }), { tip: error.message });
}

// The helper pages the install's parts serve (the state's `helper_pages`, absent without parts: each a path, a title,
// a mark and search words): the brand menu lists them after Usage, and the center layer opens them as it opens
// Cockpit's own (CENTER_HELPERS).
function listHelperPages(pages) {
  const list = document.querySelector(".brand-menu .menu-list");
  let after = list.querySelector('a[href="/usage"]');
  for (const page of pages) {
    CENTER_HELPERS[page.path] = page.title;
    let link = list.querySelector(`a[href="${page.path}"]`);
    if (!link) {
      link = h("a", { href: page.path }, h("span", { class: "menu-label" }, icon(page.mark), page.title));
      after.after(link);
    }
    after = link;
  }
}

async function refresh() {
  if (ui.refreshing) { ui.refreshAgain = true; return; }
  ui.refreshing = true;
  const okBefore = ui.lastOk;
  const watchedBefore = ui.watchedSince;
  try {
    const asked = ++ui.tick;
    const askedAt = Date.now();
    // A tab in the background is no sign the user is at the page (sweeper.user_seen, SWEEP-11).
    const state = await readState(withOpened("/api/state", document.hidden ? { background: "1" } : {}));
    // A pending change that adds to what the state holds (time added, TRACK-7) may already be in a state
    // asked before the server confirmed it, and would then count twice: such a state is skipped. The
    // refresh after the confirmation is asked later and shows it. A change whose answer takes longer than
    // ADDS_WAIT_MS skips no more states, so one stalled request cannot hold the page still.
    const adding = (change) => change.apply.adds > Date.now() - ADDS_WAIT_MS && (change.confirmed === null || change.confirmed >= asked);
    if ([...ui.pending.values()].some(adding)) return;
    // The top blocked card folds its rule editor when a rule already allows the call, so the rules
    // load before the first drawing with a blocked card: it is drawn once, at its size (UI-16).
    if ((state.permission_requests.length || (state.moved_past || []).length) && ui.permissions === null && !ui.permissionsError) {
      await loadPermissions();
    }
    // A state asked for after the server confirmed a change already shows it. The drawing then
    // follows this state, also when nothing else in it changed.
    let dropped = false;
    for (const [key, change] of ui.pending) {
      if (change.confirmed !== null && change.confirmed < asked) { ui.pending.delete(key); dropped = true; }
    }
    for (const [key, change] of ui.snoozing) if (change.confirmed !== null && change.confirmed < asked) ui.snoozing.delete(key);
    for (const [key, change] of ui.handing) if (change.confirmed !== null && change.confirmed < asked) ui.handing.delete(key);
    // Claude usage changes on most polls; it redraws only its own pill in the header (UI-16).
    const { now, usage, ...rest } = state;
    const fingerprint = JSON.stringify(window.cockpitPart?.boardState?.(rest) || rest);
    const changed = fingerprint !== ui.fingerprint;
    const usagePrint = JSON.stringify([usage, window.cockpitPart?.usageKey?.(state)]);
    const usageChanged = usagePrint !== ui.usagePrint;
    ui.state = state;
    noticeCodexSignIns(state);
    noticeSnoozeReads(state);
    window.cockpitPart?.notice?.(state);
    if (state.helper_pages) listHelperPages(state.helper_pages);
    // Once Delete settled, a refreshed absence leaves nothing to retry, also after a failed request.
    for (const id of commentDeletions.keys()) {
      if (!ui.busy.has(`page-comment:${id}`) && !state.page_comments.some((comment) => comment.id === id)) commentDeletions.delete(id);
    }
    ui.stateAskedAt = askedAt;
    ui.clockOffset = Date.parse(now) - Date.now();
    ui.fingerprint = fingerprint;
    ui.usagePrint = usagePrint;
    const recovered = ui.error !== "";
    const okAt = new Date();
    // Since when the page has read every state with no gap longer than ARRIVED_MS between them: a change in that time
    // happened at most one poll before the page saw it (sidebar.js `busyArrivals`).
    if (!ui.lastOk || okAt - ui.lastOk > ARRIVED_MS) ui.watchedSince = okAt.getTime();
    ui.lastOk = okAt;
    ui.error = "";
    ui.catchUpFailed = false;
    forgetSentMarks(state);
    settleUploads(state);
    noticeSideAnswers(state);
    const usageGenerationBefore = usageGeneration;
    const fresh = Date.now() - ui.renderedAt < RERENDER_MS;
    if (ui.actionRender && !ui.pending.size && !ui.pendingRender && fresh && drawnPrint(state) === ui.drawnPrint) {
      // The user's change already drew this state before the server confirmed it: a second
      // redraw would only restart the motion. The queue keeps the order it shows.
      ui.actionRender = false;
      ui.order = shownOrder(state.queue);
      renderStatus();
    } else if (changed || !fresh || ui.pendingRender || ui.actionRender
      || (dropped && drawnPrint(drawn(state)) !== ui.drawnPrint)) safeRender();
    else renderStatus();
    if (usageChanged && usageGeneration === usageGenerationBefore) { renderUsage(state); fitHeader(); }
    // A state with nothing new to draw shows the page is current; one held back for the user's press or selection
    // is not drawn yet.
    if (!ui.pendingRender) caughtUp(askedAt);
    // The banner sits above the board: it comes and goes inside a redraw, so what the user reads holds still.
    if (recovered) Motion.redraw(() => renderBanner(state), "poll");
  } catch (error) {
    logError("refreshing the page", error);
    // A state that came but did not draw counts as not read: the next poll draws it again, and a lasting
    // failure reaches the banner.
    ui.fingerprint = "";
    ui.lastOk = okBefore;
    ui.watchedSince = watchedBefore;
    // The banner says the cards are old; they show again under it.
    if (ui.catchUpFrom !== null) ui.catchUpFailed = true;
    endCatchUp();
    // Reload gives an older page the code for the running Cockpit's state.
    ui.error = newerVersion() ? `this page is older than the running Cockpit; press Reload (${error.message})` : error.message;
    renderStatus();
    if (ui.state) Motion.redraw(() => renderBanner(ui.state), "poll");
    else {
      Loading.failed(document);
      Loading.failed(document.getElementById("next"), `Cockpit did not send the page (${error.message}); trying again`);
    }
  } finally {
    // The first drawing is done: rules boot.js read that it did not need would be stale by the time they are.
    window.CockpitBoot = null;
    // The installed app's launch opens onto the page once it has drawn (UI-80).
    window.CockpitLaunch?.ready();
    ui.refreshing = false;
    if (ui.refreshAgain) { ui.refreshAgain = false; refresh(); }
  }
}

// `path` with `params`, and the hidden items this page opened (ui.openedHidden), which the state and the quick list then hold.
function withOpened(path, params) {
  const query = new URLSearchParams(params);
  if (ui.openedHidden.size) query.set("opened", [...ui.openedHidden].join(","));
  return query.size ? `${path}?${query}` : path;
}

// Resolves to whether the work went through.
async function run(key, work, success) {
  if (ui.busy.has(key)) return false;
  ui.busy.add(key);
  let done = false;
  try {
    await work();
    done = true;
    if (success) success();
  } catch (error) {
    logError(`action ${key}`, error);
    // A refusal puts back drafts, rows and boxes: the redraw after it shows them.
    ui.pendingRender = true;
    // A refusal may offer to try again (a pause that did not land, whose timer may run on).
    toast(error.message, true, error.retry || null, error.tip);
  } finally {
    ui.busy.delete(key);
    ui.actionRender = true;
    await refresh();
  }
  return done;
}

// An undo pressed while the action or hand-off it undoes is on its way waits for that one: the
// server takes it only once that one went through, and nothing is left to undo when it did not.
// `undoing`: whether the request is itself an undo, which a press after it waits for in turn.
function track(stepId, request, undoing) {
  const entry = { request, undoing };
  ui.inFlight.set(stepId, entry);
  request.then(() => { if (ui.inFlight.get(stepId) === entry) ui.inFlight.delete(stepId); });
}

// Optimistic changes: each of the user's clicks shows its effect at once. `apply` turns
// a state into the state with the change; the page draws the server's state with
// every pending change applied. A change stays until the server confirmed it and a
// state asked for after that arrived. If the server refuses, the change leaves at
// once, `undo` puts back what else it changed (or is null), and the error goes on. The page shows the change gone
// once a press under way ends, or, when the user is selecting text then, once the selection is cleared (safeRender).
// `own`: whether the user's press made the change. One the page makes itself, such as the timer it starts when the
// user comes back (startOpened), draws once the user's press has ended (renderAfterPress): drawn during it, it would
// swap the button under the finger for a new one, and the press would do nothing.
async function optimistic(apply, work, undo, own = true) {
  const draw = own ? () => render(false) : renderAfterPress;
  const key = ++ui.pendingCount;
  const change = { apply, confirmed: null };
  ui.pending.set(key, change);
  draw();
  try {
    const answer = await work();
    change.confirmed = ++ui.tick;
    return answer;
  } catch (error) {
    ui.pending.delete(key);
    if (undo) undo();
    // The refusal comes later than the press that made the change, maybe while the user presses something else: it
    // waits for that press to end too, whoever made the change.
    renderAfterPress();
    throw error;
  }
}

// The state as drawn: the server's, with the user's pending changes on top.
function drawn(state) {
  let shown = state;
  // Each change sees the places the ones before it freed already filled, as the server answers them one by one: a
  // timer started on the step that took a freed place, then an Undo of the close, leave that step in the queue.
  let order = 0;
  for (const change of ui.pending.values()) {
    const after = change.apply(shown);
    shown = resorted(shown, refilled(givenBack(shown, after), order), order);
    order++;
  }
  return shown;
}

// After a change that took steps from the user's queue (Done, Dismiss, Snooze, a hand-off, Task done, Close item),
// each waiting step scored above a queued step the user has not worked on in its place takes that place, whatever
// the gap, as store._resort_after_close does with the press: the queue moves once, with the close.
// The highest waiting step meets the lowest movable step first; an early look takes only an early look's place.
// A step whose time runs out keeps its place (NEXT-60).
function resorted(before, after, order) {
  const still = new Set([...after.queue, ...after.ready_next].map((step) => step.id));
  if (!before.queue.some((step) => !still.has(step.id))) return after;
  const movable = after.queue.filter((step) => !step.started_in_place && !ui.holdAsks.has(step.id) && !taken(step)
    && !expiring(step) && !step.snoozed_until && !timerRuns(step)).sort((a, b) => a.score - b.score);
  const waiting = [...after.ready_next].sort(byRank);
  let shown = after;
  for (const early of [false, true]) {
    const strong = waiting.filter((step) => Boolean(step.early) === early);
    const weak = movable.filter((step) => Boolean(step.early) === early);
    for (let at = 0; at < Math.min(strong.length, weak.length) && strong[at].score > weak[at].score; at++) {
      shown = withStep(shown, weak[at], { queued_at: null, placed_order: undefined });
      shown = withStep(shown, strong[at], { queued_at: after.now, taken_at: null, placed_order: order });
    }
  }
  return shown;
}

// The state after a change that brought steps back to the queue (an Undo of a close or a snooze, a reopen), with
// the queue as it was, as store._give_back_places leaves it: while the queue is over QUEUE_LIMIT, the steps placed
// since the returning steps left (`ui.leftAt`), last placed first, such as one that took the place a close freed
// (`refilled`), go back to Ready next. The steps coming back, the steps the user took to the top, the steps whose
// time runs out (NEXT-60) and the step whose timer runs keep their places.
function givenBack(before, after) {
  const had = new Set(before.queue.map((step) => step.id));
  const back = new Set(after.queue.filter((step) => !had.has(step.id)).map((step) => step.id));
  if (!back.size || after.queue.length <= QUEUE_LIMIT) return after;
  // A step closed before this page loaded left at a time the page does not know: any place may be newer.
  const left = [...back].map((id) => ui.leftAt.get(id) || "").sort()[0];
  const later = after.queue.filter((step) => !back.has(step.id) && !taken(step) && !expiring(step) && !timerRuns(step) && step.queued_at >= left)
    .sort((a, b) => b.queued_at.localeCompare(a.queued_at) || placedOrder(b) - placedOrder(a) || b.id - a.id);
  let shown = after;
  for (const step of later.slice(0, after.queue.length - QUEUE_LIMIT)) shown = withStep(shown, step, { queued_at: null, placed_order: undefined });
  return shown;
}

// The places the user's pending changes freed, given to the strongest ready-next steps, early looks last, as the
// server fills them (store.fill_queue). Next up then shows after a close the step the server's answer leads with,
// and holds that one. The server's own state never has a free place while a step waits in Ready next.
// `order`: the pending change's place among the user's pending changes. Of two places given in the same second, one
// this page gave counts as later than one Cockpit gave, and one it gave for a later change as later than one it gave
// before (`givenBack`).
function refilled(state, order) {
  const free = QUEUE_LIMIT - state.queue.length;
  if (free <= 0 || !state.ready_next.length) return state;
  const joining = [...state.ready_next].sort(byRank).sort((a, b) => Boolean(a.early) - Boolean(b.early)).slice(0, free);
  const ids = new Set(joining.map((step) => step.id));
  // The server gives the place its own time; the state's own time keeps this drawing the same between redraws.
  const placed = state.now;
  return recount({
    ...state,
    queue: ranked([...state.queue, ...joining.map((step) => ({ ...step, queued_at: placed, taken_at: null, placed_order: order }))]),
    ready_next: state.ready_next.filter((step) => !ids.has(step.id)),
  });
}

function placedOrder(step) {
  return step.placed_order ?? -1;
}

const STEP_LISTS = ["queue", "ready_next", "snoozed", "with_agents"];

// Counts the header shows that follow from the lists.
function recount(state) {
  return {
    ...state,
    counts: { ...state.counts, waiting_on_user: state.queue.length, ready_next: state.ready_next.length, with_agents: state.with_agents.length },
  };
}

// The state with the timer stopped when it runs on one of `ids`, for a press that takes those steps from the user:
// Cockpit stops it then. Until the press's answer comes, the page would otherwise still count the timer as running, and
// the step Next up shows next would get no start, so no hold, and the answer could put another step over it (NEXT-16).
function timerStopped(state, ids) {
  const ran = state.timer;
  return ran && ids.includes(ran.step_id) ? timedState(state, ran.step_id, false, Date.now()) : state;
}

// The queue in the server's order (rank.order): the steps whose time still runs, the soonest to end first (NEXT-60),
// then the steps the user took to the top, newest take first, then the rest by the sweeper's score (RANK-18).
function ranked(queue) {
  const ends = (step) => expiring(step) ? Date.parse(step.expires_at) : Number.MAX_VALUE;
  const takenAt = (step) => taken(step) ? step.taken_at : "";
  return [...queue].sort((a, b) => ends(a) - ends(b) || takenAt(b).localeCompare(takenAt(a)) || byRank(a, b));
}

// The highest score first, then the oldest, then the lowest id, as rank.order sorts.
function byRank(a, b) {
  return b.score - a.score || a.created_at.localeCompare(b.created_at) || a.id - b.id;
}

// Whether the user took the step to the top of their queue and it still holds that place (NEXT-36, rank.taken).
function taken(step) {
  return Boolean(step.taken_at) && step.taken_at === step.queued_at;
}

// Whether the step is useful only until a time that has not passed yet (SPEC NEXT-60, rank.expiring), by the
// server's clock: a browser whose clock runs behind would show a dead link as one that still works.
function expiring(step) {
  return Boolean(step.expires_at) && Date.parse(step.expires_at) > Date.now() + ui.clockOffset;
}

// The state with one step's fields changed where it stands, in whichever list holds it.
function withStepFields(state, id, fields) {
  const shown = { ...state };
  for (const name of STEP_LISTS) shown[name] = state[name].map((step) => (step.id === id ? { ...step, ...fields } : step));
  return shown;
}

// The state with the step, changed by `fields`, back in the queue or ready next.
function withStep(state, step, fields) {
  const changed = { ...step, ...fields };
  const shown = withoutStep(state, step.id);
  if (changed.queued_at) shown.queue = ranked([...shown.queue, changed]);
  else shown.ready_next = [...shown.ready_next, changed].sort(byRank);
  return recount(shown);
}

// What each action looks like on the page before the server answers.
function applyAction(state, step, action, body) {
  const current = STEP_LISTS.flatMap((name) => state[name]).find((other) => other.id === step.id) || step;
  if (action === "done" || action === "dismiss") return withoutStep(timerStopped(state, [step.id]), step.id);
  if (action === "snooze" && body.until !== null) return withoutStep(timerStopped(state, [step.id]), step.id);
  if (action === "snooze") return withStep(state, current, { snoozed_until: null });
  if (action === "reopen") {
    // The closed list has only the step's title and ask; its full row comes with the next state.
    const shown = current.item ? withStep(state, current, { status: "open" }) : state;
    return { ...shown, closed_recently: { ...state.closed_recently, steps: state.closed_recently.steps.filter((other) => other.id !== step.id) } };
  }
  if (action === "return") {
    // A return replayed on a state where Cockpit already took the step back leaves it where Cockpit put it.
    if (!state.with_agents.some((other) => other.id === step.id)) return state;
    // The user's Take back puts the step at the top of their queue, as a take (store.step_action). The hand-off
    // stopped its timer, so its new place has no time in it.
    return takePlace(state, { ...current, handoff: null, handoff_message_id: null, timer: null }, false);
  }
  if (action === "take") {
    // A pending take replayed on a state that already has it (a taken step in the queue) leaves it as the server says.
    if (taken(current) && state.queue.some((other) => other.id === step.id)) return state;
    // The take gives the step a new place, so only a timer that runs now has run in it (`_started_in_place`);
    // a step that waits in Ready next has none.
    return takePlace(state, current, timerRuns(current));
  }
  return state;
}

// The state with the step at the top of the user's queue, as store._take_place puts it. A place now, first; while
// the queue is over four places, its last step the user has not worked on in its place waits in Ready next, else
// its last paused one. As on the server, a take leads the last one even in the same second. A snoozed step keeps
// its snooze and holds the place for when it wakes.
function takePlace(state, step, startedInPlace) {
  const last = Math.max(0, ...STEP_LISTS.flatMap((name) => state[name]).filter((other) => other.taken_at).map((other) => Date.parse(other.taken_at) + 1000));
  const now = isoUtc(new Date(Math.max(Date.now(), last)));
  const placed = { ...step, queued_at: now, taken_at: now, started_in_place: startedInPlace };
  const sleeping = placed.snoozed_until && Date.parse(placed.snoozed_until) > Date.now();
  const without = withoutStep(state, step.id);
  let shown = sleeping ? recount({ ...without, snoozed: [...without.snoozed, placed] }) : withStep(state, placed, {});
  while (shown.queue.length > QUEUE_LIMIT) {
    const others = shown.queue.filter((other) => other.id !== step.id && !timerRuns(other));
    // An early look the user has not worked on waits first: the step the take sends back takes its place
    // (store.fill_queue, SPEC NEXT-42).
    const fresh = others.filter((other) => !other.started_in_place);
    const back = fresh.filter((other) => other.early && !taken(other) && !other.snoozed_until).at(-1) || fresh.at(-1) || others.at(-1);
    if (!back) break;
    shown = withStep(shown, back, { queued_at: null, started_in_place: false });
  }
  return shown;
}

// What the actions that take a step off the page say afterwards, and the action that undoes each.
const ACTION_TOASTS = {
  done: ["Done", (step) => act(step, "reopen")],
  dismiss: ["Dismissed", (step) => act(step, "reopen")],
  snooze: ["Snoozed", (step) => act(step, "snooze", { until: null })],
};
// What a refusal of each action says first.
const ACTION_REFUSALS = {
  done: "Not closed", dismiss: "Not dismissed", snooze: "Not snoozed", reopen: "Not reopened",
  return: "Not taken back", take: "Not taken",
};

// Every step action shows at once: closing ones take the step off the page, the others put it
// back. If the server refuses, the step goes back where it was.
function act(step, action, body = {}) {
  // Undo, Unsnooze and Take back show at once, also while the action they undo is on its way (UI-8), and an
  // action pressed again after an undo still on its way (a slow connection, CODE-5) waits for that undo. Each
  // goes once Cockpit answered the press before it; an undo has nothing left to undo when that one did not
  // go through. A second press of the same kind while the first is on its way does nothing.
  const undoing = action === "reopen" || action === "return" || (action === "snooze" && body.until === null);
  if (!body.words) wordReads.delete(`step:${step.id}`);
  if (!undoing) followQueue(step);
  const before = ui.inFlight.get(step.id);
  if (before && before.undoing === undoing) return Promise.resolve(false);
  // A step the user takes out of the queue on the page leaves without a kept "Left your queue" row.
  if (action === "done" || action === "dismiss" || (action === "snooze" && body.until !== null)) {
    takenByUser(step.id);
    ui.leftAt.set(step.id, ui.state.now);
  }
  const outcome = { answer: null };
  // Words in an open Add more box for this step, which the notice Take back replaces would drop (MSG-10).
  const following = action === "return" ? (document.querySelector(`#toast .toast-follow[data-step="${step.id}"] textarea`)?.value || "").trim() : "";
  // The sent mark of the message a Take back undoes, as the press found it, and the box its words go back to.
  const takenMark = action === "return" ? ui.sent.get(step.id) : undefined;
  const draftKey = backKey(step, takenMark);
  // Settles with whether Cockpit took this press, before the redraw that follows it: the next press waits for this.
  let answered;
  const sent = new Promise((resolve) => { answered = resolve; });
  const acting = run(`act:${step.id}:${++ui.sendCount}`, async () => {
    try {
      outcome.answer = await optimistic((state) => applyAction(state, step, action, body), async () => {
        const went = before ? await before.request : true;
        if (undoing && !went) return null;
        if (undoing) await afterTimerPresses();
        const path = `/api/steps/${step.id}/${action}`;
        // A Take back keeps its step's reply box with it, for the words it stops and those in Add more.
        if (action === "return") return apiWritten("POST", path, body, { key: draftKey, text: following, back: true });
        // Done archives the session the card named (SESSION-4): the server refuses it when the step goes elsewhere now.
        return api("POST", path, action === "done" ? { ...body, session_id: step.target ? step.target.session_id : null } : body);
      });
      answered(true);
    } catch (error) {
      answered(false);
      const failure = refused(ACTION_REFUSALS[action], error);
      if (!following) throw failure;
      // The Add more box went with the notice: its words wait in the step's reply box and in the error's tip.
      if (!(ui.drafts.get(draftKey) || "").includes(following)) restoreDraft(draftKey, following);
      throw Object.assign(failure, { tip: `${failure.tip}\nYour words, not sent: ${following}` });
    }
  }, action === "return" ? () => tookBack(step, outcome.answer, following, takenMark) : null);
  track(step.id, sent, undoing);
  if (before) holdLeave(sent);
  actToast(step, action, body, acting, outcome);
  if (action === "return") {
    acting.then((done) => {
      if (done && outcome.answer && (outcome.answer.unsent || following)) document.querySelector(`textarea[data-key="${CSS.escape(draftKey)}"]`)?.focus();
    });
  }
  return acting;
}

// The box a Take back puts its words in: the one the message was sent from, else the step's reply box.
function backKey(step, takenMark) {
  return (takenMark && takenMark.key) || `msg:step:${step.id}`;
}

// Take back (MSG-8) says what became of the message. One it stopped was not sent: the user's words, and any in an open
// Add more box (`following`), go back into the step's reply box, open, to add to and send again, and the sent mark of
// the failed send goes. One that had already gone stays with the session.
function tookBack(step, answer, following, takenMark) {
  if (!answer) return;
  const key = backKey(step, takenMark);
  const words = [answer.unsent, following].filter(Boolean).join("\n\n");
  // The message was not sent, so the box writes where it was going, also when only page comments went with it;
  // a box the user set to another To while Take back was on its way keeps it, and the words stay in the notice.
  const elsewhere = Boolean(answer.unsent_to) && !restoreTarget(key, unsentTarget(answer.unsent_to));
  if (words && !elsewhere) {
    // A second tab may still hold the words as its draft.
    if (!(ui.drafts.get(key) || "").includes(words)) restoreDraft(key, words);
    ui.expanded.add(key);
    ui.expanded.add(`step:${step.id}`);
  }
  // Its images stay on the item and go back above the box, to go with the words again (SPEC MSG-18).
  const staged = new Set(stagedImages(key).map((entry) => entry.savedId));
  const images = elsewhere ? [] : answer.unsent_images.filter((id) => !staged.has(id));
  restoreStaged(key, images.map((id) => ({ id: `img-${++ui.sendCount}`, url: `/item-images/${id}`, file: null, savedId: id, savedOn: step.item_id })));
  // A Send made while this Take back was on its way (`handOffStep`) has its own receipt and its own notice
  // with Take back; this notice would take that one's place, and so let the newer message go at once.
  const later = ui.sent.get(step.id);
  const sentSince = Boolean(later) && later !== takenMark && !later.refused;
  // The message it stopped was not sent: its mark goes, and a Send made since (`handOffStep`) keeps its own mark,
  // and on a refusal goes back past the stopped one.
  if (answer.stopped && takenMark) {
    takenMark.refused = true;
    if (ui.sent.get(step.id) === takenMark) {
      ui.sent.delete(step.id);
      saveSent();
    }
  }
  const back = following ? "; what you added is in the step's reply box" : "";
  // While that Send's notice still shows, what the user still needs to know from this Take back joins it instead.
  if (sentSince) {
    const more = elsewhere ? `Your earlier message was not sent.${words ? ` ${NOT_PUT_BACK}` : ""}`
      : answer.stopped ? "" : "Your earlier message had already gone to the session.";
    if (more ? addToToast(later.notice, more, elsewhere && words ? `Your words, not sent: ${words}` : "") : toastShows(later.notice)) return;
  }
  if (elsewhere) {
    toast(`Taken back. Your message was not sent.${words ? ` ${NOT_PUT_BACK}` : ""}`, true, null, words && `Your words, not sent: ${words}`);
  } else if (answer.unsent) {
    toast("Taken back. Your message was not sent; its words are back in the step's reply box to add to and send.");
  } else if (answer.stopped) {
    toast(`Taken back before Cockpit sent its message to the session${back}.`);
  } else {
    toast(`Taken back. The message had already gone to the session${back || "; anything more goes from the reply box"}.`);
  }
}

// Reopen, unsnooze and return only undo, so they say nothing. Undo shows at once (see `track`).
// Done and Archive say what happens now: the first sentence of the step's "After." paragraph when it
// has one (UI-9), then what becomes of the step's session (SESSION-4) once the server answered;
// without either, what is next up once the server's state after the action is in (it fills the freed
// place from ready next). Dismiss and Snooze say only the verb. The closed step's ask and what is next
// up are in the toast's tooltip.
function actToast(step, action, body, acting, outcome) {
  if (!(action in ACTION_TOASTS) || (action === "snooze" && body.until === null)) return;
  const [doneVerb, undo] = ACTION_TOASTS[action];
  const verb = action === "done" && ui.state && archivesSession(step, ui.state) ? "Archived" : doneVerb;
  const ask = plainText(step.ask_html);
  const undoing = { label: "Undo", run: () => undo(step) };
  const after = action === "done" ? afterSentence(step) : "";
  const said = action !== "snooze" ? verb
    : body.words ? `Snoozed while Cockpit reads “${body.words.text}”` : `Snoozed until ${when(new Date(body.until))}`;
  const first = after ? `${verb}: ${after}` : `${said}.`;
  const shown = toast(first, false, undoing, `${verb}: ${ask}`);
  acting.then(() => {
    const next = nextUp();
    const nextWords = next ? `Next up: ${clip(plainText(next.ask_html), 90)}` : "Nothing else is waiting for you.";
    const end = action === "done" && outcome.answer ? outcome.answer.session_end : null;
    // Done goes on to the next step, and its session's words go in the tooltip; Archive says what
    // becomes of the session in place of Next up.
    const archiving = end && end.how === "archiving" ? sessionEndWords(end) : "";
    const tip = [`${verb}: ${ask}`, nextWords, archiving ? "" : sessionEndWords(end)].filter(Boolean).join("\n");
    const text = [first, archiving || (action === "done" && !after ? nextWords : "")]
      .filter(Boolean).join(" ");
    retoast(shown, text, tip);
  });
}

// The first sentence of a step's "After." paragraph (what the user's Done or Archive sets off), or "".
function afterSentence(step) {
  if (!step.body_html) return "";
  const paragraph = labelledBody(`after:${step.id}`, step.body_html).querySelector(".label-after");
  if (!paragraph) return "";
  const words = labelText(paragraph);
  const sentence = (words.match(/^.+?[.!?](?=\s|$)/) || [words])[0];
  return clip(sentence, 90);
}

// Finishing an item (CONTROL-15): the user's one action that ends an item. On the item's last open step, Task done
// completes it in one press, with what the reply box holds as the user's last message; a closing answer completes
// it with that reply; Close task…, in the step's ⋯ menu, asks why and archives the item. Every open step of the item
// closes, its session hears it and is archived on its last open step (SESSION-4), and the item's lamp leaves the
// status lights. Undo puts all of it back.

// How many other steps of the step's item are open on the page.
function itemOthers(step, state) {
  return STEP_LISTS.flatMap((name) => state[name]).filter((other) => other.id !== step.id && other.item_id === step.item_id).length;
}

// Whether this step's key is Task done: it is a finish step (need `complete…`, which also stands
// for an agent's close of the item, CONTROL-17), a result to read (need `read:…`, VIEW-16), or the item's last open
// step. A result to read ends the task with Task done, or with an answer that names the next action, so it never shows
// a Done that closes only the step (CONTROL-15). A step that lists tasks to close (CONTROL-23) has its own plum key for
// those, so its item does not finish from it.
function finishesItem(step, state) {
  if (step.closes) return false;
  return /^(complete(:|$)|read:)/.test(step.need) || itemOthers(step, state) === 0;
}

// The state with the item's open steps gone, and the item done (complete) or archived (close), each in Finished
// this week saying so (VIEW-20): done by you with the closing answer, or closed as not needed with the reason.
function finished(given, step, how, said, reason) {
  const state = timerStopped(given, STEP_LISTS.flatMap((name) => given[name]).filter((other) => other.item_id === step.item_id).map((other) => other.id));
  const shown = { ...state };
  for (const name of STEP_LISTS) shown[name] = state[name].filter((other) => other.item_id !== step.item_id);
  const now = new Date().toISOString();
  if (how === "close") {
    shown.items = state.items.filter((item) => item.id !== step.item_id);
    const row = { id: step.item_id, key: step.item.key, title: step.item.title, project: step.item.project, stage: step.item.stage, action: "archived", closed_at: now, closed_by: "user", note: reason };
    shown.closed_recently = { ...state.closed_recently, items: [row, ...state.closed_recently.items.filter((other) => other.id !== step.item_id)] };
  } else {
    const ended = { by: "user", note: said || "", at: now };
    shown.items = state.items.map((item) => (item.id === step.item_id ? { ...item, stage: "done", waiting_on: [], ball: "done", group: "done", tip: "", tip_at: null, ended } : item));
  }
  return closedAs(recount(shown), "finished", step.item_id, true);
}

// The items the user closed in the last day (CONTROL-17), by id as the server sends them (`closed_by_user`):
// `finished` with Task done, a closing answer or Close item, `archived` in All work. A close adds its item at
// once and its Undo drops it; by id, a change replayed on a state that already holds it counts once, and an
// item closed both ways counts once.
function closedAs(state, list, itemId, add) {
  const ids = state.closed_by_user[list].filter((id) => id !== itemId);
  return { ...state, closed_by_user: { ...state.closed_by_user, [list]: add ? [...ids, itemId] : ids } };
}

function closedCount(state) {
  return new Set([...state.closed_by_user.finished, ...state.closed_by_user.archived]).size;
}

// The item's open steps and the item as they were before the finish, for Undo.
function beforeFinish(state, step) {
  const lists = Object.fromEntries(STEP_LISTS.map((name) => [name, state[name].filter((other) => other.item_id === step.item_id)]));
  return { lists, itemId: step.item_id, item: state.items.find((item) => item.id === step.item_id) || null };
}

function beforeIds(before) {
  return STEP_LISTS.flatMap((name) => before.lists[name]).map((step) => step.id);
}

// The state with the item and its open steps back as they were before the finish.
function unfinished(state, before) {
  const shown = { ...state };
  for (const name of STEP_LISTS) {
    const back = before.lists[name].filter((step) => !STEP_LISTS.some((list) => state[list].some((other) => other.id === step.id)));
    shown[name] = name === "queue" ? ranked([...state.queue, ...back]) : [...state[name], ...back];
  }
  if (before.item) shown.items = [...state.items.filter((item) => item.id !== before.item.id), before.item];
  shown.closed_recently = { ...state.closed_recently, items: state.closed_recently.items.filter((item) => item.id !== before.itemId) };
  return closedAs(closedAs(recount(shown), "finished", before.itemId, false), "archived", before.itemId, false);
}

// An Undo's request goes after the timer presses made before it: a start on the step that took the place the close
// freed then reaches Cockpit first, and that step keeps its place (store._give_back_places keeps the running step),
// as it does on the page (`givenBack`). It waits at most TIMER_FIRST_MS, so a start whose answer was lost does not
// hold the Undo, and the closes sent after it, for the minute that start is tried again (`sendTimer`).
const TIMER_FIRST_MS = 5000;
function afterTimerPresses() {
  return Promise.race([ui.timerSent, new Promise((resolve) => setTimeout(resolve, TIMER_FIRST_MS))]);
}

// Closes and their Undos, of one task or many, send their requests in the order the user pressed them: a close
// pressed while an Undo's reopens are still on their way waits for them, so it does not reach the server first.
let finishRequests = Promise.resolve();
let turnsWaiting = 0;
function inTurn(send) {
  turnsWaiting += 1;
  const sending = finishRequests.then(send).finally(() => { turnsWaiting -= 1; });
  finishRequests = sending.catch(() => {});
  return sending;
}

// `answer`: the closing answer's text, or null for Task done and Close item. `note`: the last message typed in the
// step's reply box before Task done, "" for none. `images`: the reply box's staged images that go with the last
// message (SPEC MSG-18), which go up to the item first; [] for none.
function finishItem(step, how, answer, note, images, reason, words, pressed) {
  const key = `finish:${step.item_id}`;
  if (ui.busy.has(key)) return Promise.resolve(false);
  followQueue(step);
  const title = step.item.title;
  const now = drawn(ui.state);
  const others = itemOthers(step, now);
  const before = beforeFinish(now, step);
  closeAnimation(stepCard(step.id, pressed), pressed, how === "complete");
  for (const id of beforeIds(before)) {
    takenByUser(id);
    ui.leftAt.set(id, ui.state.now);
  }
  ui.finishing.add(step.item_id);
  const said = answer || note;
  // A last message and its images go back to the reply box they came from once: on Undo, or when Cockpit refuses
  // the finish, before the page redraws the step.
  let givenBack = !note;
  const giveBack = () => {
    if (givenBack) return;
    givenBack = true;
    restoreDraft(`msg:step:${step.id}`, note);
    restoreStaged(`msg:step:${step.id}`, images);
  };
  const outcome = { answer: null };
  // Settles with whether Cockpit took the finish, before the redraw that follows it: an undo waits for this.
  let answered;
  const sent = new Promise((resolve) => { answered = resolve; });
  const finishing = run(key, async () => {
    try {
      outcome.answer = await optimistic((state) => finished(state, step, how, said, reason),
        async () => {
          // Task done answers the step, so it carries the choices too; Close item goes without them. The waits, and
          // the upload of the images, are inside the finish's turn, so an Undo pressed meanwhile goes after it rather
          // than holding it up.
          return inTurn(async () => {
            if (how === "complete") await choicesSaved(step);
            // The session the page showed: the server refuses the finish when the step goes elsewhere now.
            const body = {
              step_id: step.id, how, answer, note, images: await uploadStaged(step.item_id, images), reason,
              session_id: step.target ? step.target.session_id : null,
            };
            return words ? apiWritten("POST", `/api/items/${step.item_id}/finish`, body, words) : api("POST", `/api/items/${step.item_id}/finish`, body);
          });
        }, giveBack);
      answered(true);
    } catch (error) {
      answered(false);
      ui.finishing.delete(step.item_id);
      throw refused(how === "complete" ? "Not completed" : "Not closed", error);
    }
  }, () => { ui.finishing.delete(step.item_id); });
  // A finish can wait in inTurn before it has an outbox entry, after Task done took its words from the draft.
  holdLeave(finishing);
  track(step.id, sent, false);
  const verb = how === "complete" ? "Completed" : "Closed";
  const steps = others ? ` and its ${plural(others + 1, "step", "steps")}` : "";
  const first = closedWords("Closed!");
  const tip = [`${verb}: ${title}${steps}.`, answer && `Your answer: ${answer}`, note && `Your last message: ${note}`, how === "close" && `Why: ${reason}`].filter(Boolean).join("\n");
  // A closing answer or last message waits for this notice like a hand-off (MSG-8): it goes once the notice is gone.
  const mark = { id: null };
  const undo = () => {
    giveBack();
    return reopenItem(step, finishing, before);
  };
  // Task done with no words still carries the step's page comments and choices when it has some; the release
  // does nothing when the finish told no session.
  const shown = closedNotice(first, [step.item_id], undo, said || how === "complete" ? () => releaseHandOff(finishing, mark) : null, tip);
  // What becomes of the session goes in the tooltip, so the notice stays its two words and Undo.
  finishing.then((done) => {
    if (!done || !outcome.answer) return;
    if (outcome.answer.message) mark.id = outcome.answer.message.id;
    const end = sessionEndWords(outcome.answer.session_end);
    if (end && shown !== null) retoast(shown, first, `${tip}\n${end}`);
  });
  return finishing;
}

// The words of the notice after the user closes a task where the list is not shown (CONTROL-17): `head` and the
// count of tasks the user closed in the last day, this one included (`closedCount`), counted once the close's change
// is in.
function closedWords(head) {
  return `${head} (${closedCount(drawn(ui.state))} in the last 24h)`;
}

// Whether a close goes to Finished this week in the list at the left: not on the classic page, which draws no
// list, nor on a phone's item screen, which hides it.
function closeListShown() {
  return SIDEBAR && !phoneItemScreen();
}

// Whether a close's, an Add's or a hand-off's Undo is held in the list (`closedNotice`, `handedNotice`, the Add):
// on the page with the list at the left, also on a phone's item screen, whose bar's Your items holds it for the list
// out of sight (sidebar.js `buildPhoneBar`). The classic page draws no list and shows a notice.
const undoHeldInList = () => SIDEBAR;

const phoneItemScreen = () => onPhone() && !phoneListShown();

// `undo` runs on Undo; `gone`, when set, once the notice leaves without it (toast's `action.gone`). With no `undo`
// the notice is its words alone.
function closedToast(text, undo, gone, tip) {
  const shown = toast(text, false, undo ? { label: "Undo", run: undo, gone } : null, tip);
  const el = document.getElementById("toast");
  el.classList.add("closed");
  el.prepend(icon("check"));
  return shown;
}

// How long a close's Undo stays in Finished this week: as long as the notice shows.
const UNDO_CLOSE_MS = TOAST_MS;

// The notice after a close, and its Undo. On the page with the list at the left, the closed tasks go to Finished this
// week and the Undo sits there for UNDO_CLOSE_MS: on the group's heading while it is folded, as each task's row once it
// is open (`closeHeld` in sidebar.js), and no notice shows at the foot, as Finished this week's count rolls up with the
// close; on a phone's item screen, which hides the list, on the bar's Your items. It returns null then. The classic
// page draws no list: there the notice shows and offers Undo, and it returns the notice's number. `itemIds`: the tasks the close ended. `gone`, when set, runs once
// the Undo time ends without it, as the notice's would (MSG-8).
function closedNotice(text, itemIds, undo, gone, tip) {
  if (!undoHeldInList()) return closedToast(text, undo, gone, tip);
  const hold = { at: Date.now(), itemIds, timer: null };
  const release = () => {
    if (!ui.closesHeld.has(hold)) return false;
    clearTimeout(hold.timer);
    ui.closesHeld.delete(hold);
    return true;
  };
  hold.undo = () => { if (!release()) return; undo(); render(false); };
  hold.end = () => { if (!release()) return; if (gone) gone(); render(false); };
  hold.timer = setTimeout(hold.end, UNDO_CLOSE_MS);
  ui.closesHeld.add(hold);
  render(false);
  return null;
}

// A page hidden while a close's Undo is on offer ends it now, so a last message held for it goes out: a phone may
// stop the page's timers in the background.
// A hand-off's Undo in Busy ends the same way, so its message goes out.
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) return;
  for (const hold of [...ui.closesHeld, ...ui.handsHeld]) hold.end();
});

// Close in an expanded All work row archives the item at once, with the same animation and notice as a close
// from its step (CONTROL-17). Undo shows the row again at once; its request waits for the close's (`patchItem`).
function closeItemRow(item, pressed) {
  closeAnimation(pressed.closest(".row") || pressed, pressed, false);
  patchItem(item, { archived: true }, null);
  closedNotice(closedWords("Closed!"), [item.id], () => patchItem(item, { archived: false }, null), null,
    `Closed: ${item.title}. Finished this week, at the end of the list, keeps it with Unarchive.`);
}

// Undo of Task done or Close item: the item and its steps show again at once; once the finish went
// through, the server puts them back.
function reopenItem(step, finishing, before) {
  ui.finishing.delete(step.item_id);
  for (const id of beforeIds(before)) ui.removedByUser.delete(id);
  return run(`reopen:${step.item_id}`, async () => {
    try {
      const sending = inTurn(async () => {
        if (!(await finishing)) return null;
        await afterTimerPresses();
        return api("POST", `/api/items/${step.item_id}/reopen`);
      });
      holdLeave(sending);
      await optimistic((state) => unfinished(state, before), () => sending);
    } catch (error) {
      throw refused("Not undone", error);
    }
  });
}

// A finish step (need `complete…`, rank.FINISH_NEED): its own task closes with its close key.
function finishStep(step) {
  return /^complete(:.+)?$/.test(step.need);
}

// Close these tasks (CONTROL-23): a session lists the tasks it judges done on a step of its own, each with why. The
// step's card shows them all ticked; the user unticks any to keep open, and one plum key with the stack icon closes the
// rest, each as its Task done would, with the close animation and one notice whose Undo brings them back with the step.
// On a finish step the list is the card's one close key: the card's own task heads it, always ticked, and closes too.
function closesBlock(step, state) {
  const skips = ui.closesSkip;
  const skip = skips.get(step.id) || new Set();
  const items = new Map(state.items.filter((item) => item.stage !== "done").map((item) => [item.id, item]));
  // A task that closed since the session listed it leaves the list.
  const listed = step.closes.filter((entry) => items.has(entry.item_id));
  const ticked = listed.filter((entry) => !skip.has(entry.item_id));
  const row = (entry) => {
    const kept = skip.has(entry.item_id);
    return h("li", { class: `close-list-row${kept ? " kept" : ""}` },
      h("label", { class: "close-list-pick" },
        h("input", {
          type: "checkbox", checked: kept ? null : true, "data-key": `closes:${step.id}:${entry.item_id}`,
          onchange: (event) => {
            if (event.currentTarget.checked) skip.delete(entry.item_id); else skip.add(entry.item_id);
            skips.set(step.id, skip);
            render(false);
          },
        }),
        h("span", { class: "close-list-title", text: items.get(entry.item_id).title }),
        h("span", { class: "close-list-why", text: entry.why })));
  };
  const own = finishStep(step);
  const count = ticked.length + (own ? 1 : 0);
  const ownRow = own && h("li", { class: "close-list-row own" },
    h("label", { class: "close-list-pick", title: "This card's own task closes with the list" },
      h("input", { type: "checkbox", checked: true, disabled: true }),
      h("span", { class: "close-list-title", text: step.item.title }),
      h("span", { class: "close-list-why", text: "This task" })));
  return h("div", { class: "close-list", role: "group", "aria-label": "Tasks to close" },
    h("ul", { class: "close-list-rows" }, ownRow, listed.map(row)),
    h("div", { class: "step-keys close-list-keys" },
      h("button", {
        class: "button task-done close-list-go", type: "button", disabled: count ? null : true,
        onclick: (event) => closeListed(step, ticked.map((entry) => items.get(entry.item_id)), event.currentTarget.closest(".card"), event.currentTarget),
      }, icon("stack"), h("span", { class: "label", text: count ? `Close ${plural(count, "task", "tasks")}` : "Close" }))));
}

// The state with the step gone from the lists, as Done shows it.
function withoutStep(state, stepId) {
  const shown = { ...state };
  for (const name of STEP_LISTS) shown[name] = state[name].filter((other) => other.id !== stepId);
  return recount(shown);
}

// Completes the ticked tasks of the step's list with one press: the close plays on `card` from the key `pressed`,
// the step closes with them, the plum notice counts them, and one Undo for all sits in Finished this week (or on the
// notice where the list is not shown; `closedNotice`). A task that changed
// since the session listed it stays open, and the notice says how many did; when none closed, the step stays.
// On a finish step the card's own task closes last with them, so its steps and the item go too (`own`).
function closeListed(step, items, card, pressed) {
  const key = `close-tasks:${step.id}`;
  const closesOwn = finishStep(step);
  if (ui.busy.has(key) || !(items.length || closesOwn)) return Promise.resolve(false);
  followQueue(step);
  const now = drawn(ui.state);
  const tasks = items.map((item) => ({ item_id: item.id, item }));
  const befores = tasks.map((task) => beforeFinish(now, task));
  const own = closesOwn
    ? beforeFinish(now, step)
    : { lists: Object.fromEntries(STEP_LISTS.map((name) => [name, now[name].filter((other) => other.id === step.id)])), itemId: step.item_id, item: null };
  const ended = closesOwn ? [...tasks, step] : tasks;
  const titles = closesOwn ? [...items.map((item) => item.title), step.item.title] : items.map((item) => item.title);
  closeAnimation(card, pressed, true);
  for (const before of [...befores, own]) {
    for (const id of beforeIds(before)) {
      takenByUser(id);
      ui.leftAt.set(id, ui.state.now);
    }
  }
  for (const task of ended) ui.finishing.add(task.item_id);
  const result = { finished: [], refused: [], step: null };
  const finishing = run(key, async () => {
    try {
      Object.assign(result, await optimistic(
        (state) => withoutStep(timerStopped(ended.reduce((shown, task) => finished(shown, task, "complete", null, ""), state), [step.id]), step.id),
        () => inTurn(() => api("POST", `/api/steps/${step.id}/close-tasks`, { item_ids: tasks.map((task) => task.item_id) }))));
    } catch (error) {
      throw refused("Not closed", error);
    } finally {
      for (const task of ended) ui.finishing.delete(task.item_id);
      ui.closesSkip.delete(step.id);
    }
  });
  holdLeave(finishing);
  const tip = `Completed ${plural(ended.length, "task", "tasks")}:\n${titles.map((title) => `- ${title}`).join("\n")}`;
  // The Undo in Finished this week covers the tasks this close ended; once the server answered, only those it closed.
  const held = ended.map((task) => task.item_id);
  const shown = closedNotice(closedWords(`Closed ${ended.length}!`), held,
    () => reopenTasks(befores, own, finishing, result), null, tip);
  finishing.then((done) => {
    if (!done || !result.refused.length) return;
    const stayed = new Set(result.refused.map((entry) => entry.item_id));
    held.splice(0, held.length, ...held.filter((id) => !stayed.has(id)));
    render(false);
    for (const before of befores) if (stayed.has(before.itemId)) for (const id of beforeIds(before)) ui.removedByUser.delete(id);
    if (!result.finished.length) for (const id of beforeIds(own)) ui.removedByUser.delete(id);
    const open = items.filter((item) => stayed.has(item.id)).map((item) => `- ${item.title}: ${result.refused.find((entry) => entry.item_id === item.id).error}`);
    const words = result.finished.length
      ? `Closed ${result.finished.length} of ${ended.length}! ${result.refused.length} changed and stay open`
      : `None closed: the ${plural(tasks.length, "task", "tasks")} changed and stay open`;
    const why = `${tip}\nStayed open, since they changed after the session listed them:\n${open.join("\n")}`;
    // With no notice shown (the list at the left), the tasks that stayed open still get one.
    if (shown === null) toast(words, false, null, why); else retoast(shown, words, why);
  });
  return finishing;
}

// Undo of Close these tasks: the tasks and the step (`own`) show again at once; once the close went through, the
// server reopens each task it completed (`result.finished`, read once the close answered), one after the other, so
// each step gets its place back, and then the step, when the press closed it (`result.step`) and its own task's
// reopen did not already bring it back (a finish step, whose task is in `result.finished`). A reopen the server
// refuses does not stop the others; the notice names what stayed closed and offers to try that again. Each press's
// Undo is its own action, so one's does not swallow another's.
function reopenTasks(befores, own, finishing, result) {
  for (const before of [...befores, own]) for (const id of beforeIds(before)) ui.removedByUser.delete(id);
  return run(`reopen:tasks:${beforeIds(own).join(",")}:${befores.map((before) => before.itemId).join(",")}`, async () => {
    const failed = [];
    let stepFailed = false;
    const sending = inTurn(async () => {
      if (!(await finishing)) return;
      await afterTimerPresses();
      for (const itemId of result.finished) {
        try {
          await api("POST", `/api/items/${itemId}/reopen`);
        } catch (error) {
          failed.push({ itemId, error });
        }
      }
      if (result.step && result.step.status === "done" && !result.finished.includes(result.step.item_id)) {
        try {
          await api("POST", `/api/steps/${result.step.id}/reopen`, {});
        } catch (error) {
          stepFailed = true;
          failed.push({ itemId: null, error });
        }
      }
      if (failed.length) throw new Error(failed.map((entry) => entry.error.message).join("; "));
    });
    holdLeave(sending);
    try {
      await optimistic((state) => [...befores, own].reduce((shown, before) => unfinished(shown, before), state), () => sending);
    } catch (error) {
      const again = failed.filter((entry) => entry.itemId !== null).map((entry) => entry.itemId);
      const count = again.length + (stepFailed ? 1 : 0);
      const words = again.length ? `${plural(again.length, "task", "tasks")} not undone` : "Not undone";
      throw Object.assign(refused(words, error), count
        ? { retry: { label: "Try again", run: () => reopenTasks(befores.filter((before) => again.includes(before.itemId)), own, Promise.resolve(true), { finished: again, step: stepFailed ? result.step : null }) } }
        : {});
    }
  });
}

// Close item asks why, under the step's keys, and names the item that closes: the whole item, not only the step.
function closeBox(step) {
  const key = `close:${step.id}`;
  const area = draftArea(key, "Why close it? The session and the sweep read this.", 2, "");
  const send = () => {
    const reason = (ui.drafts.get(key) || "").trim();
    if (!reason) { area.focus(); toast("Say why first", true); return; }
    takeDraft(key);
    ui.expanded.delete(key);
    finishItem(step, "close", null, "", [], reason, { key, text: reason }, closeKey).then((done) => {
      if (done || ui.drafts.get(key)) return;
      restoreDraft(key, reason);
      ui.expanded.add(key);
      safeRender();
    });
  };
  area.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); send(); }
  });
  const steps = itemOthers(step, drawn(ui.state)) + 1;
  const closeKey = h("button", { class: "button close-item", type: "button", "data-key": "Ctrl+Enter", onclick: send }, icon("archive"), h("span", { class: "label", text: "Close item" }));
  return h("div", { class: "close-box composer filled", role: "group", "aria-label": "Close item" },
    h("p", { class: "close-words" },
      h("span", { text: "Close item " }), h("b", { text: step.item.title }),
      h("span", { text: `: the whole item closes with its ${plural(steps, "open step", "open steps")}, not only this step. It goes to Finished this week at the end of the list, and Undo brings it back.` })),
    area,
    h("div", { class: "composer-foot" },
      h("button", { class: "button ghost", type: "button", onclick: () => toggle(key) }, h("span", { class: "label", text: "Cancel" })),
      closeKey));
}

// A secret for the step's session (MSG-15), such as an API key: More → Give a secret… opens this box above the
// reply box. Its name and value live in this page's memory only (`secretFields`), never in the drafts' storage, and
// go once, like a sign-in code (`apiCredential`). Cockpit writes the value to a file only this user may read and
// hands the step to its session with the file's path and the words in the reply box.
const secretFields = new Map();

function secretBox(step, replyKey) {
  const key = `secret:${step.id}`;
  const target = step.target ? sessionLabel(sessionMap(ui.state).get(step.target.session_id) || step.target, "") : "";
  const field = (part, attributes) => {
    const el = h("input", { ...attributes, "data-key": `${key}:${part}`, autocomplete: "off", spellcheck: "false" });
    el.value = (secretFields.get(key) || {})[part] || "";
    el.addEventListener("input", () => { secretFields.set(key, { ...secretFields.get(key), [part]: el.value }); });
    el.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); send(); }
      else if (event.key === "Escape") { event.preventDefault(); close(); }
    });
    return el;
  };
  const name = field("name", { type: "text", class: "field secret-name", placeholder: "tasks-export-key", "aria-label": "Name",
    pattern: "[A-Za-z0-9_\\-]{1,64}", maxlength: 64 });
  const value = field("value", { type: "password", class: "field secret-value", placeholder: "Value", "aria-label": "Value" });
  const close = () => { secretFields.delete(key); toggle(key); };
  const send = () => {
    const picked = secretFields.get(key) || {};
    if (!step.target || ui.busy.has(key)) return;
    if (!/^[A-Za-z0-9_-]{1,64}$/.test(picked.name || "")) { name.focus(); toast("Name: letters, digits, - and _ only", true); return; }
    if (!(picked.value || "").trim()) { value.focus(); toast("Paste the value first", true); return; }
    sendSecret(step, replyKey, key, picked.name, picked.value, target);
  };
  return h("div", { class: "secret-box composer filled", role: "group", "aria-label": "Give a secret" },
    h("p", { class: "close-words" },
      h("span", { text: "Give " }), h("b", { text: target || "the session" }),
      h("span", { text: " a secret. It goes to a file only you can read; the session gets its path." })),
    h("div", { class: "secret-fields" }, name, value),
    h("div", { class: "composer-foot" },
      h("button", { class: "button ghost", type: "button", onclick: close }, h("span", { class: "label", text: "Cancel" })),
      h("button", { class: "button primary", type: "button", disabled: !step.target, title: step.target ? null : "No session to give it to", onclick: send },
        icon("key"), h("span", { class: "label", text: "Give secret" }))));
}

// Opening the box puts the cursor in its name; closing forgets what it held.
function openSecret(key) {
  const opening = !ui.expanded.has(key);
  if (!opening) secretFields.delete(key);
  toggle(key);
  if (opening) document.querySelector(`[data-key="${CSS.escape(`${key}:name`)}"]`)?.focus();
}

// The step goes to its session at once, with no Take back: the file is written. The words in the reply box go with
// it: they leave the box at once and come back if the server refuses, as a hand-off's do (`handOffDraft`).
function sendSecret(step, replyKey, key, name, value, target) {
  const sent = ui.drafts.get(replyKey) || "";
  const choice = ui.choices.get(replyKey) || null;
  const picks = ui.starts.get(replyKey) || null;
  takeDraft(replyKey);
  // The box closes and forgets its fields at once; a refusal opens it again with them, unless it holds newer ones.
  secretFields.delete(key);
  ui.expanded.delete(key);
  const handoff = {
    session_id: step.target.session_id, new_cwd: null, text: `Stored secret ${name}`, author: "user",
    state: "queued", detail: "", created_at: new Date().toISOString(),
  };
  followQueue(step);
  takenByUser(step.id);
  // Settles with whether Cockpit took the secret: a Take back or other press on the step waits for it (`track`).
  let answered;
  const taken = new Promise((resolve) => { answered = resolve; });
  let back = true;
  const reopens = reopening(step.item_id);
  const reopen = reopenBy(reopens, step.item_id);
  const giving = run(key, async () => {
    try {
      await optimistic((state) => reopen(handedOff(state, step, handoff)),
        async () => {
          await choicesSaved(step);
          return afterFinishes(() => apiCredential(`/api/steps/${step.id}/secret`,
            { name, value, text: sent.trim(), session_id: step.target.session_id }));
        },
        () => {
          back = putBack(replyKey, sent, choice, picks) !== false;
          takeBackReopened(reopens);
          if (!secretFields.has(key)) secretFields.set(key, { name, value });
          ui.expanded.add(key);
        });
      answered(true);
    } catch (error) {
      answered(false);
      throw keptInNotice(refused("Secret not given", error), back, sent.trim());
    }
    if (reopens) reopenConfirmed(reopens);
    else toast(`Gave ${target} the secret ${name}`);
  }, null);
  track(step.id, taken, false);
  return giving;
}

// The close (CONTROL-15, CONTROL-17): every close of a task plays one of eleven animations from the close lab, picked
// at random and never the same twice in a row. The key that closed it springs back from its press while light floods
// it, and a ring leaves it, with sparks when the task is done (`sparks`); then the card, or the All work row, goes its
// way: torn into paper pieces, stamped DONE, slung into the notice, burst into confetti, turned into coins, poured into
// a portal, or, on the GPU (close-gpu.js), burnt from the key, burnt by flames climbing from the bottom, blown away as
// dust, drained into the key or blasted apart. Copies do the moving, so the page redraws underneath at once, and the
// CSS ones mostly move only transform and opacity, so the compositor runs them. The key goes down as the pointer does
// (its `:active` look), so the press shows before the page redraws. `torn`: the step's card or the row, or null;
// `pressed`: the key in it, or null. With reduced motion, a copy fades.
const GPU_CLOSES = ["burn", "ignite", "snap", "vortex", "blast"];
const CLOSES = {
  tear: closeTear, stamp: closeStamp, slingshot: closeSlingshot, confetti: closeConfetti, coin: closeCoin, portal: closePortal,
  ...Object.fromEntries(GPU_CLOSES.map((name) => [name, GpuCloses[name]])),
};
const CLOSE_IN = "cubic-bezier(0.5, 0, 0.75, 0)";
const CLOSE_SPRING = "cubic-bezier(0.2, 0.9, 0.3, 1.35)";
let lastClose = null;

function closeAnimation(torn, pressed, sparks) {
  const from = torn && torn.getBoundingClientRect();
  if (!from || !from.width) return;
  // The GPU closes join the pick once their shaders are ready, for a card it can paint: an embedded page, video or
  // canvas in it would show blank.
  const gpu = GpuCloses.ready() && !torn.querySelector("iframe, video, canvas");
  const names = Object.keys(CLOSES).filter((name) => name !== lastClose && (gpu || !GPU_CLOSES.includes(name)));
  const name = REDUCED.matches ? "fade" : names[Math.floor(Math.random() * names.length)];
  if (name !== "fade") lastClose = name;
  const key = pressed && torn.contains(pressed) ? pressed.getBoundingClientRect() : null;
  // Read before the copies go in, so the press forces no style work: how far down the key is now (the
  // pointer's press, or none from the keyboard) and its corners, for the ring.
  const look = key && getComputedStyle(pressed);
  const down = key ? new DOMMatrixReadOnly(look.transform.replace("none", "matrix(1, 0, 0, 1, 0, 0)")).a : 1;
  const corners = key && look.borderRadius;
  const ground = Pick.ground(torn);
  if (key) pressed.dataset.pressed = "";
  const copy = closeCopy(torn, from, ground);
  if (key) delete pressed.dataset.pressed;
  const close = {
    name, from, key, sparks, copy,
    // Where the key's middle is, in the page and in the card's own pixels; the card's middle without a key.
    x: key ? key.left + key.width / 2 : from.left + from.width / 2,
    y: key ? key.top + key.height / 2 : from.top + from.height / 2,
    pressIn: key && down > 0.99 ? 90 : 0,
  };
  close.kx = close.x - from.left;
  close.ky = close.y - from.top;
  if (name === "fade") {
    if (torn.closest("#next")) Motion.coverNext();
    const sheet = closePiece(close, from, copy);
    sheet.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 300, fill: "both" }).finished.catch(() => {}).then(() => sheet.remove());
    if (torn.closest("#next")) Motion.arrive(() => sheet.isConnected ? cardLeft(sheet) : 0);
    return;
  }
  // On Next up the step after this one is drawn under the copy at once, so the card burns, tears or flies off
  // the page with the next step already there. That step starts a shade darker and lightens as the
  // card leaves.
  const onNext = Boolean(torn.closest("#next"));
  if (onNext) Motion.coverNext();
  const had = new Set(document.querySelectorAll(".close-fx, .tear-piece"));
  CLOSES[name](close);
  if (onNext) Motion.arrive(() => cardGone(had));
  if (key) pressKey(copy.hasAttribute("data-pressed") ? copy : copy.querySelector("[data-pressed]"), key, corners, down, close.pressIn, sparks);
}

// How long from now until the card a close just tore, stamped or flung is out of sight: the end of the longest
// animation on the boxes it added (those not in `had`) that hold a copy of the card. NaN while there are none or
// none of them moves yet (Tear and Portal build their pieces a frame or more later), and on the GPU closes, which
// draw the card on a canvas.
function cardGone(had) {
  let end = NaN;
  for (const piece of document.querySelectorAll(".close-fx, .tear-piece")) {
    if (had.has(piece) || !piece.querySelector(".close-copy")) continue;
    const left = cardLeft(piece);
    if (!Number.isNaN(left)) end = Number.isNaN(end) ? left : Math.max(end, left);
  }
  return end;
}

// The ms until the last finite animation on `piece` itself ends: those inside it are the card's own (a pulse on a
// copied key runs for ever) and end with it. NaN when none.
function cardLeft(piece) {
  let end = NaN;
  for (const animation of piece.getAnimations()) {
    const { endTime } = animation.effect.getComputedTiming();
    if (!Number.isFinite(endTime) || animation.playState === "finished") continue;
    const left = endTime - (animation.currentTime || 0);
    end = Number.isNaN(end) ? left : Math.max(end, left);
  }
  return end;
}

// A copy of `el` at its size (`from`), out of the page's reach: no ids, no step, no pointer. A row is
// see-through on its list; its copy takes the colour it stands on (`ground`).
function closeCopy(el, from, ground) {
  const copy = el.cloneNode(true);
  copy.removeAttribute("data-step");
  for (const inner of copy.querySelectorAll("[id]")) inner.removeAttribute("id");
  copy.classList.add("close-copy");
  Object.assign(copy.style, { width: `${from.width}px`, height: `${from.height}px`, background: ground });
  return copy;
}

// A box fixed over the page at `rect`, holding `child`, that leaves once its animations end.
function closePiece(close, rect, child, style) {
  const piece = h("div", { class: "close-fx", "aria-hidden": "true", "data-close": close.name }, ...(child ? [child] : []));
  piece.inert = true;
  Object.assign(piece.style, { left: `${rect.left}px`, top: `${rect.top}px`, width: `${rect.width}px`, height: `${rect.height}px` }, style || {});
  document.body.append(piece);
  return piece;
}

function closeEnd(animation, piece) {
  animation.finished.catch(() => {}).then(() => piece.remove());
}

// Where the close is counted. On a phone's item screen, the Undo on the bar's Your items. On the page with the list at
// the left, Finished this week: the closed task's row
// while the group is open, its heading's count while it is folded (`closedNotice`); a group scrolled out of the
// list's view, the list's edge on the way to it. Elsewhere the closed notice's check, once the notice shows; without
// one, the corner it shows in. Read a frame after the press, when the notice is in: it is still lifting and growing
// into place then, so the check's middle is mapped from the notice as drawn to the notice as laid out.
function noticeMark() {
  const bar = SIDEBAR && phoneItemScreen() && document.querySelector(".phone-items.holding > .busy-undo > .icon");
  if (bar) {
    const rect = bar.getBoundingClientRect();
    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, el: bar };
  }
  if (closeListShown()) {
    const list = document.getElementById("sidebar");
    const group = list && list.querySelector(".sidebar-group.done");
    const row = group && group.open ? group.querySelector(".held-close .sidebar-lead") : null;
    const el = row || (group && group.querySelector(":scope > summary .group-count"));
    if (el) {
      // Where it lands: motion.js may still be gliding the heading or row (`translate`) into place.
      const at = el.getBoundingClientRect();
      let dx = 0;
      let dy = 0;
      for (let up = el; up && up !== list; up = up.parentElement) {
        const [x = 0, y = 0] = (getComputedStyle(up).translate || "").split(" ").map((part) => parseFloat(part) || 0);
        dx += x;
        dy += y;
      }
      const rect = new DOMRect(at.left - dx, at.top - dy, at.width, at.height);
      const view = list.getBoundingClientRect();
      const y = rect.top + rect.height / 2;
      const inView = y >= view.top && y <= view.bottom;
      return { x: rect.left + rect.width / 2, y: Math.min(Math.max(y, view.top + 8), view.bottom - 8), el: inView ? el : null };
    }
  }
  const toastEl = document.getElementById("toast");
  const mark = !toastEl.hidden && toastEl.classList.contains("closed") ? toastEl.querySelector(":scope > .icon") : null;
  if (!mark) return { x: innerWidth - 48, y: innerHeight - 40, el: null };
  const rect = mark.getBoundingClientRect();
  const drawn = toastEl.getBoundingClientRect();
  if (!drawn.width || !drawn.height) return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, el: mark };
  return {
    x: toastEl.offsetLeft + ((rect.left + rect.width / 2 - drawn.left) * toastEl.offsetWidth) / drawn.width,
    y: toastEl.offsetTop + ((rect.top + rect.height / 2 - drawn.top) * toastEl.offsetHeight) / drawn.height,
    el: mark,
  };
}

// The notice's check swells as something lands in it, `after` ms from now.
function noticePop(mark, after, size = 1.7) {
  if (!mark.el) return;
  mark.el.animate([{ transform: "scale(1)" }, { transform: `scale(${size})`, offset: 0.3, easing: CLOSE_SPRING }, { transform: "scale(1)" }],
    { duration: 380, delay: after, easing: EASE_OUT });
}

// Tear: the card tears from the key and breaks into many pieces that spin and fall away. The pieces are built
// a few a frame while the key springs back, and start together as it settles.
function closeTear(close) {
  const { from } = close;
  const sheet = closePiece(close, from, close.copy);
  sheet.classList.add("tear-piece");
  const tearAt = document.timeline.currentTime + close.pressIn + 200;
  requestAnimationFrame(() => {
    const pieces = tearPieces(sheet, from, { x: close.kx, y: close.ky });
    const built = [];
    const build = () => {
      const batch = pieces.splice(0, 3).map((make) => make());
      for (const { piece } of batch) piece.dataset.close = close.name;
      sheet.before(...batch.map(({ piece }) => piece));
      built.push(...batch);
      if (pieces.length) { requestAnimationFrame(build); return; }
      const start = Math.max(tearAt, document.timeline.currentTime);
      for (const { flight } of built) flight.startTime = start;
      const gone = sheet.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 1, fill: "forwards" });
      gone.startTime = start;
      closeEnd(gone, sheet);
    };
    build();
  });
}

// Stamp: a plum DONE stamp falls onto the card and slams down; the card squashes under it and jolts, ink rings
// out, a beat of stillness, then the stamped card drops away.
function closeStamp(close) {
  const { from, pressIn } = close;
  const { copy } = close;
  const sheet = closePiece(close, from, copy);
  const fit = Math.min(1, (from.height - 8) / 60, (from.width - 32) / 210);
  const half = 105 * fit;
  const sx = Math.max(16 + half, Math.min(from.width - 16 - half, from.width * 0.58));
  const sy = from.height * 0.45;
  const stamp = h("div", { class: "close-stamp", text: "Done" });
  Object.assign(stamp.style, { left: `${sx}px`, top: `${sy}px` });
  const ring = h("div", { class: "close-ring" });
  Object.assign(ring.style, { left: `${sx - 30}px`, top: `${sy - 30}px` });
  sheet.append(stamp, ring);
  const tilt = -9 + jitterOf(4);
  const fall = 110;
  const hit = pressIn + fall;
  stamp.animate([
    { transform: `translate(-50%, -50%) rotate(${tilt - 8}deg) scale(${2.6 * fit})`, opacity: 0 },
    { opacity: 1, offset: 0.4 },
    { transform: `translate(-50%, -50%) rotate(${tilt}deg) scale(${fit})`, opacity: 1 },
  ], { duration: fall, delay: pressIn, easing: CLOSE_IN, fill: "both" });
  // The impact: the card squashes and jolts, and an ink ring leaves the stamp.
  const jolt = [5, 3.4, 2, 1, 0.4].map((px) => `translate(${jitterOf(2 * px).toFixed(1)}px, ${jitterOf(2 * px).toFixed(1)}px)`);
  copy.animate([
    { transform: "none" }, { transform: `${jolt[0]} scale(1.012, 0.965)` }, { transform: `${jolt[1]} scale(0.995, 1.008)` },
    { transform: jolt[2] }, { transform: jolt[3] }, { transform: jolt[4] }, { transform: "none" },
  ], { duration: 220, delay: hit, easing: "ease-out" });
  ring.animate([{ transform: "scale(0.6)", opacity: 0.9 }, { transform: `scale(${(half * 2 / 60) * 1.6})`, opacity: 0 }],
    { duration: 340, delay: hit, easing: EASE_OUT });
  closeEnd(sheet.animate([
    { transform: "none", opacity: 1 },
    { transform: "translateY(-6px) scale(1.01)", opacity: 1, offset: 0.25 },
    { transform: "translateY(60px) scale(0.94) rotate(1.5deg)", opacity: 0 },
  ], { duration: 300, delay: hit + 90, easing: CLOSE_IN, fill: "both" }), sheet);
}

// The stamp's worn ink: a mask of specks, drawn once while the page is idle.
(window.requestIdleCallback || setTimeout)(() => {
  const canvas = h("canvas", { width: "160", height: "160" });
  const paint = canvas.getContext("2d");
  paint.fillRect(0, 0, 160, 160);
  paint.globalCompositeOperation = "destination-out";
  for (let i = 0; i < 900; i += 1) {
    paint.fillStyle = `rgba(0, 0, 0, ${Math.random() * 0.9})`;
    paint.beginPath();
    paint.arc(Math.random() * 160, Math.random() * 160, Math.random() * 1.8, 0, Math.PI * 2);
    paint.fill();
  }
  document.documentElement.style.setProperty("--close-grunge", `url(${canvas.toDataURL()})`);
});

// Slingshot: the card pulls back as if on a band, stretches as it launches, and streaks to where the close is
// counted (`noticeMark`: Finished this week, or the closed notice's check), which swells as it lands.
function closeSlingshot(close) {
  const { from, pressIn } = close;
  const sheet = closePiece(close, from, close.copy, { transformOrigin: "50% 50%" });
  const pull = 10;
  const pullFor = 90;
  const fly = 260;
  requestAnimationFrame(() => {
    const mark = noticeMark();
    const dx = mark.x - (from.left + from.width / 2);
    const dy = mark.y - (from.top + from.height / 2);
    const length = Math.hypot(dx, dy) || 1;
    const ux = dx / length;
    const uy = dy / length;
    const angle = (Math.atan2(dy, dx) * 180) / Math.PI;
    const total = pullFor + fly;
    const back = pullFor / total;
    const stretch = `rotate(${angle}deg) scale(1.08, 0.94) rotate(${-angle}deg)`;
    closeEnd(sheet.animate([
      { transform: "none", easing: "cubic-bezier(0.3, 0, 0.2, 1)" },
      { transform: `translate(${-ux * pull}px, ${-uy * pull}px) scale(0.975)`, offset: back, easing: "cubic-bezier(0.6, 0, 0.9, 0.6)" },
      { transform: `translate(${dx * 0.35}px, ${dy * 0.35}px) ${stretch} scale(0.66)`, offset: back + (1 - back) * 0.4, opacity: 1 },
      { transform: `translate(${dx}px, ${dy}px) scale(0.03)`, opacity: 0 },
    ], { duration: total, delay: pressIn, fill: "both" }), sheet);
    // Streaks behind it.
    for (let i = 0; i < 3; i += 1) {
      const sx = from.left + from.width / 2 + jitterOf(from.width * 0.6);
      const sy = from.top + from.height / 2 + jitterOf(from.height * 0.5);
      const reach = Math.min(length, 260);
      const streak = closePiece(close, { left: sx - reach, top: sy - 1, width: reach, height: 2 }, null, { transformOrigin: "100% 50%" });
      streak.classList.add("close-streak");
      const turn = `rotate(${angle}deg)`;
      closeEnd(streak.animate([
        { transform: `${turn} translateX(0) scaleX(0)`, opacity: 0 },
        { transform: `${turn} translateX(${length * 0.4}px) scaleX(1)`, opacity: 0.8, offset: 0.4 },
        { transform: `${turn} translateX(${length}px) scaleX(0.2)`, opacity: 0 },
      ], { duration: fly * 0.9, delay: pressIn + pullFor + i * 25, easing: "cubic-bezier(0.4, 0, 0.6, 1)", fill: "both" }), streak);
    }
    noticePop(mark, pressIn + total - 10);
  });
}

// Confetti: the card swells a touch, then shrinks into the key as a confetti cannon fires from it: paper
// pieces flip in three dimensions, slow in the air and fall.
function closeConfetti(close) {
  const { from, pressIn } = close;
  const sheet = closePiece(close, from, close.copy, { transformOrigin: `${close.kx}px ${close.ky}px` });
  closeEnd(sheet.animate([
    { transform: "scale(1)", opacity: 1 },
    { transform: "scale(1.02)", offset: 0.25, opacity: 1, easing: "cubic-bezier(0.5, 0, 0.9, 0.5)" },
    { transform: "scale(0.02)", opacity: 0 },
  ], { duration: 260, delay: pressIn, fill: "both" }), sheet);
  const width = close.key ? close.key.width : 40;
  const pieces = [];
  for (let i = 0; i < 90; i += 1) {
    const angle = ((-90 + jitterOf(70)) * Math.PI) / 180;
    const speed = 850 * (0.45 + Math.random() * 0.55);
    pieces.push({ kind: "paper", x: close.x + jitterOf(width * 0.6), y: close.y, vx: Math.cos(angle) * speed, vy: Math.sin(angle) * speed,
      g: 1500, drag: 3.2, w: 6 + Math.random() * 4, h: 3 + Math.random() * 2, rot: Math.random() * 6, vr: jitterOf(14),
      flip: Math.random() * 6, vf: 8 + Math.random() * 10, c: CONFETTI_COLOURS[i % CONFETTI_COLOURS.length], life: 0.9 + Math.random() * 0.5, t: -(pressIn + 140) / 1000 });
  }
  for (let i = 0; i < 10; i += 1) {
    const angle = (i / 10) * Math.PI * 2 + jitterOf(0.4);
    const speed = 260 + Math.random() * 260;
    pieces.push({ kind: "spark", x: close.x, y: close.y, vx: Math.cos(angle) * speed, vy: Math.sin(angle) * speed,
      g: 300, drag: 6, w: 2.4, c: CONFETTI_COLOURS[i % CONFETTI_COLOURS.length], life: 0.32 + Math.random() * 0.18, t: -(pressIn + 120) / 1000 });
  }
  throwConfetti(close, pieces);
}

const CONFETTI_COLOURS = ["oklch(0.5 0.16 318)", "oklch(0.62 0.17 318)", "oklch(0.78 0.11 318)", "oklch(0.555 0.145 52)", "oklch(0.85 0.12 85)"];
const confetti = { canvas: null, pieces: [], last: 0, scale: 1 };

// Confetti and sparks on one canvas over the page, drawn only while pieces fly, and gone once they land.
function throwConfetti(close, pieces) {
  confetti.pieces.push(...pieces);
  if (confetti.canvas) return;
  const canvas = h("canvas", { class: "close-fx close-confetti", "aria-hidden": "true", "data-close": close.name });
  confetti.scale = Math.min(2, window.devicePixelRatio || 1);
  canvas.width = Math.round(innerWidth * confetti.scale);
  canvas.height = Math.round(innerHeight * confetti.scale);
  document.body.append(canvas);
  confetti.canvas = canvas;
  confetti.last = performance.now();
  requestAnimationFrame(drawConfetti);
}

function drawConfetti(now) {
  const { canvas, scale } = confetti;
  const dt = Math.min(0.05, (now - confetti.last) / 1000);
  confetti.last = now;
  const paint = canvas.getContext("2d");
  paint.setTransform(1, 0, 0, 1, 0, 0);
  paint.clearRect(0, 0, canvas.width, canvas.height);
  paint.setTransform(scale, 0, 0, scale, 0, 0);
  confetti.pieces = confetti.pieces.filter((piece) => {
    piece.t += dt;
    if (piece.t < 0) return true;
    if (piece.t > piece.life) return false;
    piece.vy += piece.g * dt;
    const slow = Math.exp(-piece.drag * dt);
    piece.vx *= slow;
    piece.vy *= slow;
    piece.x += piece.vx * dt;
    piece.y += piece.vy * dt;
    const s = piece.t / piece.life;
    paint.globalAlpha = s < 0.6 ? 1 : 1 - (s - 0.6) / 0.4;
    if (piece.kind === "spark") {
      paint.strokeStyle = piece.c;
      paint.lineWidth = piece.w * (1 - s * 0.7);
      paint.lineCap = "round";
      paint.beginPath();
      paint.moveTo(piece.x, piece.y);
      paint.lineTo(piece.x - piece.vx * 0.035, piece.y - piece.vy * 0.035);
      paint.stroke();
    } else {
      piece.rot += piece.vr * dt;
      piece.flip += piece.vf * dt;
      const cos = Math.cos(piece.rot);
      const sin = Math.sin(piece.rot);
      const tilt = Math.cos(piece.flip);
      paint.setTransform(scale * cos, scale * sin, -scale * sin * tilt, scale * cos * tilt, scale * piece.x, scale * piece.y);
      paint.fillStyle = piece.c;
      paint.fillRect(-piece.w / 2, -piece.h / 2, piece.w, piece.h);
      paint.setTransform(scale, 0, 0, scale, 0, 0);
    }
    return true;
  });
  paint.globalAlpha = 1;
  if (confetti.pieces.length) { requestAnimationFrame(drawConfetti); return; }
  canvas.remove();
  confetti.canvas = null;
}

// Coin: the card collapses into the key, and three plum coins pop out of it, spin as they arc up and drop
// where the close is counted (`noticeMark`: Finished this week, or the closed notice's check), which swells at
// each catch.
function closeCoin(close) {
  const { from, pressIn } = close;
  const sheet = closePiece(close, from, close.copy, { transformOrigin: `${close.kx}px ${close.ky}px` });
  closeEnd(sheet.animate([
    { transform: "scale(1)", opacity: 1, easing: "cubic-bezier(0.55, 0, 0.8, 0.4)" },
    { transform: "scale(0.04)", opacity: 0 },
  ], { duration: 200, delay: pressIn, fill: "both" }), sheet);
  const size = 26;
  const coins = 3;
  const fly = 300;
  requestAnimationFrame(() => {
    const mark = noticeMark();
    for (let i = 0; i < coins; i += 1) {
      const coin = closePiece(close, { left: close.x - size / 2, top: close.y - size / 2, width: size, height: size }, null);
      coin.classList.add("close-coin");
      coin.textContent = "+";
      const dx = mark.x - close.x + jitterOf(10);
      const dy = mark.y - close.y;
      const side = (i - (coins - 1) / 2) * 34;
      const delay = pressIn + 120 + i * 55;
      const frames = [];
      for (let j = 0; j <= 12; j += 1) {
        const u = j / 12;
        // Out from the key to the side first, then a curve into the notice.
        const pop = Math.sin((Math.min(1, u * 3) * Math.PI) / 2) * (1 - u);
        const x = dx * u * u + side * pop;
        const y = dy * u - 70 * 4 * u * (1 - u) - 18 * pop;
        const grow = u < 0.15 ? 0.3 + (u / 0.15) * 0.95 : 1.25 - 0.75 * u;
        frames.push({ offset: u, transform: `translate(${x.toFixed(1)}px, ${y.toFixed(1)}px) rotateY(${(u * 540).toFixed(0)}deg) scale(${grow.toFixed(3)})`, opacity: u > 0.92 ? 0 : 1 });
      }
      closeEnd(coin.animate(frames, { duration: fly, delay, easing: "cubic-bezier(0.3, 0, 0.6, 1)", fill: "both" }), coin);
      noticePop(mark, delay + fly - 20, i ? 1.35 : 1.7);
    }
  });
}

// Portal: a plum portal opens at the key and the card pours into it like cloth through a ring: slices of it
// nearest the key narrow and vanish first, the far ones follow. Each slice is a clipped copy that moves; one
// whole copy holds the card while the slices are built a few a frame, with the key's spring going on in each.
function closePortal(close) {
  const { from, pressIn } = close;
  const sheet = closePiece(close, from, close.copy);
  const slices = Math.max(4, Math.min(14, Math.round(from.height / 18)));
  const tall = from.height / slices;
  const mouth = 34;
  const pour = 300;
  const lag = 110;
  const portal = closePiece(close, { left: close.x - mouth, top: close.y - mouth * 0.35, width: mouth * 2, height: mouth * 0.7 }, null);
  portal.classList.add("close-portal");
  closeEnd(portal.animate([
    { transform: "scale(0)" }, { transform: "scale(1.1)", offset: 0.18, easing: "ease-out" }, { transform: "scale(1)", offset: 0.8 }, { transform: "scale(0)" },
  ], { duration: pour + lag + 120, delay: pressIn, easing: "cubic-bezier(0.3, 0.7, 0.4, 1)", fill: "both" }), portal);
  const pourAt = document.timeline.currentTime + pressIn + 40;
  const far = Math.max(close.ky, from.height - close.ky) || 1;
  requestAnimationFrame(() => {
    const twin = movingClone(sheet.firstChild);
    const left = Array.from({ length: slices }, (_, i) => i);
    const pours = [];
    const build = () => {
      for (const i of left.splice(0, 4)) {
        const top = i * tall;
        const copy = twin();
        copy.style.top = `${-top}px`;
        const slice = closePiece(close, { left: from.left, top: from.top + top, width: from.width, height: tall + 0.6 }, copy,
          { transformOrigin: `${close.kx}px ${tall / 2}px` });
        slice.classList.add("close-slice");
        sheet.before(slice);
        const rise = close.ky - (top + tall / 2);
        const flow = slice.animate([
          { transform: "none", opacity: 1, easing: "cubic-bezier(0.5, 0, 0.75, 0)" },
          { transform: `translateY(${rise * 0.35}px) scale(0.55, 0.8)`, offset: 0.55, opacity: 1, easing: "cubic-bezier(0.2, 0, 0.6, 1)" },
          { transform: `translateY(${rise}px) scale(0.02, 0.2)`, opacity: 0.3 },
        ], { duration: pour, delay: (Math.abs(rise) / far) * lag, fill: "both" });
        flow.pause();
        closeEnd(flow, slice);
        pours.push(flow);
      }
      if (left.length) { requestAnimationFrame(build); return; }
      const start = Math.max(pourAt, document.timeline.currentTime);
      for (const flow of pours) flow.startTime = start;
      const gone = sheet.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 1, fill: "forwards" });
      gone.startTime = start;
      closeEnd(gone, sheet);
    };
    build();
  });
}

// The key in the copy (`button`, or null) springs back from the press while light floods it from the
// middle, and a ring with the key's `corners` leaves the key at `at`. `down`: the key's scale as it is now;
// pressed from the keyboard it is 1, and the key first goes down here, for `pressIn` ms.
function pressKey(button, at, corners, down, pressIn, sparks) {
  const spring = "cubic-bezier(0.2, 0.9, 0.3, 1.3)";
  const reach = Math.hypot(at.width, at.height);
  if (button) {
    const flood = h("span", { class: "press-flood", "aria-hidden": "true" });
    Object.assign(flood.style, { width: `${reach}px`, height: `${reach}px`, left: `${(at.width - reach) / 2}px`, top: `${(at.height - reach) / 2}px` });
    button.classList.add("pressing");
    button.append(flood);
    button.animate([
      { transform: `scale(${down})`, easing: pressIn ? "cubic-bezier(0.3, 0, 0.2, 1)" : spring },
      ...(pressIn ? [{ transform: "scale(0.93)", offset: pressIn / (pressIn + 250), easing: spring }] : []),
      { transform: "scale(1.035)", offset: (pressIn + 150) / (pressIn + 250), easing: "ease-in-out" },
      { transform: "scale(1)" },
    ], { duration: pressIn + 250, fill: "both" });
    flood.animate([{ transform: "scale(0)", opacity: 0.6 }, { transform: "scale(1)", opacity: 0.6, offset: 0.4 }, { transform: "scale(1)", opacity: 0 }],
      { duration: 460, delay: pressIn * 0.6, easing: EASE_OUT, fill: "both" });
  }
  const ring = h("div", { class: "press-ring", "aria-hidden": "true" });
  Object.assign(ring.style, { left: `${at.left}px`, top: `${at.top}px`, width: `${at.width}px`, height: `${at.height}px`, borderRadius: corners });
  document.body.append(ring);
  ring.animate([{ transform: "scale(1)", opacity: 0.9 }, { transform: `scale(${1 + 28 / at.width}, ${1 + 28 / at.height})`, opacity: 0 }],
    { duration: 600, delay: pressIn + 20, easing: EASE_OUT, fill: "both" }).finished.catch(() => {}).then(() => ring.remove());
  if (sparks) burst(at.left + at.width / 2, at.top + at.height / 2, pressIn + 20);
}

// The pieces the copy in `sheet` (at `from`) tears into, as functions that each build one piece and return it
// with its flight, paused. A jagged tear runs from top to foot through `at`, the key's middle in the copy's
// own pixels; finer cracks cut each side into pieces about 130 px across, at most 3 by 4 a side. The two
// sides tug and swing open round the foot of the tear, then each piece flies off on its own, the ones by the
// key first, spinning as paper does, and falls away. The pieces share their edges exactly, so they make the
// copy whole until they move. Each piece is a copy clipped to its shape, with a torn edge a shade off the
// card on every side that was torn; its box is the shape's, so its layer stays small.
function tearPieces(sheet, from, at) {
  const w = from.width;
  const tall = from.height;
  const tearX = Math.min(Math.max(at.x, w * 0.25), w * 0.75);
  const left = Math.min(3, Math.max(1, Math.round(tearX / 130)));
  const right = Math.min(3, Math.max(1, Math.round((w - tearX) / 130)));
  const xs = [...Array.from({ length: left }, (_, i) => (tearX * i) / left), tearX,
    ...Array.from({ length: right }, (_, i) => tearX + ((w - tearX) * (i + 1)) / right)];
  const rows = Math.min(4, Math.max(1, Math.round(tall / 130)));
  const ys = Array.from({ length: rows + 1 }, (_, j) => (tall * j) / rows);
  const cols = xs.length - 1;
  // The corners where cracks meet move off the grid; the tear's own corners stay near its line.
  const corner = xs.map((x, i) => ys.map((y, j) => [
    i === 0 || i === cols ? x : x + jitterOf(i === left ? 10 : (xs[i + 1] - xs[i - 1]) * 0.22),
    j === 0 || j === rows ? y : y + jitterOf((ys[j + 1] - ys[j - 1]) * 0.25),
  ]));
  // An edge from a to b: straight on the copy's border, otherwise teeth every few pixels.
  const edge = (a, b, torn, rough, step) => {
    if (!torn) return [a, b];
    const length = Math.hypot(b[0] - a[0], b[1] - a[1]);
    const count = Math.max(1, Math.round(length / step));
    const normal = [(a[1] - b[1]) / length, (b[0] - a[0]) / length];
    const points = [a];
    for (let k = 1; k < count; k += 1) {
      const off = jitterOf(rough * 2);
      points.push([a[0] + ((b[0] - a[0]) * k) / count + normal[0] * off, a[1] + ((b[1] - a[1]) * k) / count + normal[1] * off]);
    }
    points.push(b);
    return points;
  };
  const across = xs.map((_, i) => ys.map((_, j) => i < cols && edge(corner[i][j], corner[i + 1][j], j > 0 && j < rows, 2.2, 11)));
  const down = xs.map((_, i) => ys.map((_, j) => j < rows && edge(corner[i][j], corner[i][j + 1], i > 0 && i < cols, i === left ? 3.5 : 2.2, i === left ? 7 : 11)));
  // The sides swing open round the foot of the tear by an angle that keeps a far corner within 40 px.
  const foot = [corner[left][rows][0], tall];
  const open = Math.min(9, (40 / Math.max(foot[0], w - foot[0])) * (180 / Math.PI));
  const reachFromKey = Math.hypot(Math.max(tearX, w - tearX), Math.max(at.y, tall - at.y)) || 1;
  // What moves inside the copy (the key's spring, its flood of light) goes on moving inside every piece.
  const twin = movingClone(sheet.firstChild);
  const pieces = [];
  for (let i = 0; i < cols; i += 1) {
    for (let j = 0; j < rows; j += 1) pieces.push(() => {
      const sides = [
        { points: across[i][j], torn: j > 0 },
        { points: down[i + 1][j], torn: i + 1 < cols },
        { points: [...across[i][j + 1]].reverse(), torn: j + 1 < rows },
        { points: [...down[i][j]].reverse(), torn: i > 0 },
      ];
      const outline = sides.flatMap((side, n) => (n ? side.points.slice(1) : side.points)).slice(0, -1);
      const box = {
        left: Math.floor(Math.min(...outline.map(([x]) => x))), top: Math.floor(Math.min(...outline.map(([, y]) => y))),
        right: Math.ceil(Math.max(...outline.map(([x]) => x))), bottom: Math.ceil(Math.max(...outline.map(([, y]) => y))),
      };
      const pw = box.right - box.left;
      const ph = box.bottom - box.top;
      const local = ([x, y]) => `${(x - box.left).toFixed(1)}px ${(y - box.top).toFixed(1)}px`;
      const copy = twin();
      Object.assign(copy.style, { left: `${-box.left}px`, top: `${-box.top}px` });
      const piece = h("div", { class: "tear-piece tear-shard", "aria-hidden": "true" }, copy);
      piece.inert = true;
      const lines = sides.filter((side) => side.torn)
        .map((side) => `<polyline points="${side.points.map(([x, y]) => `${(x - box.left).toFixed(1)},${(y - box.top).toFixed(1)}`).join(" ")}"/>`);
      piece.insertAdjacentHTML("beforeend", `<svg class="tear-edge" viewBox="0 0 ${pw} ${ph}" width="${pw}" height="${ph}">${lines.join("")}</svg>`);
      const middle = [(box.left + box.right) / 2, (box.top + box.bottom) / 2];
      Object.assign(piece.style, {
        left: `${from.left + box.left}px`, top: `${from.top + box.top}px`, width: `${pw}px`, height: `${ph}px`,
        clipPath: `polygon(${outline.map(local).join(", ")})`, transformOrigin: `${middle[0] - box.left}px ${middle[1] - box.top}px`,
      });
      return { piece, flight: flyAway(piece, middle, i < left ? -1 : 1, { foot, open, at: [tearX, at.y], reach: reachFromKey }) };
    });
  }
  return pieces;
}

// Makes copies of `source` in which what moves inside it (the key's spring, its flood of light) goes on
// moving, in step with it.
function movingClone(source) {
  const moving = [source, ...source.querySelectorAll("*")].flatMap((el, n) => el.getAnimations().map((anim) => ({ n, anim })));
  return () => {
    const copy = source.cloneNode(true);
    const twins = moving.length ? [copy, ...copy.querySelectorAll("*")] : [];
    for (const { n, anim } of moving) {
      const twin = twins[n].animate(anim.effect.getKeyframes(), anim.effect.getTiming());
      if (anim.startTime === null) twin.currentTime = anim.currentTime;
      else twin.startTime = anim.startTime;
    }
    return copy;
  };
}

// One piece's flight. `middle`: its middle in the copy's pixels; `dir`: its side of the tear. Until it goes,
// it turns with its side round the foot of the tear (written as a turn round its own middle and a shift);
// then it is thrown away from the key and up a little, falls under gravity, spins in three dimensions and
// shrinks, and fades over the last part. The flight is sampled into keyframes run at an even pace. Returns
// the flight paused at its start; setting its start time sets it off.
function flyAway(piece, middle, dir, tear) {
  const toFoot = [middle[0] - tear.foot[0], middle[1] - tear.foot[1]];
  const swung = (dx, dy, deg) => {
    const turn = (deg * Math.PI) / 180;
    return {
      x: dx + toFoot[0] * Math.cos(turn) - toFoot[1] * Math.sin(turn) - toFoot[0],
      y: dy + toFoot[0] * Math.sin(turn) + toFoot[1] * Math.cos(turn) - toFoot[1],
      z: deg, rx: 0, ry: 0, scale: 1, opacity: 1,
    };
  };
  const away = [middle[0] - tear.at[0], middle[1] - tear.at[1]];
  const distance = Math.hypot(away[0], away[1]) || 1;
  const near = distance / tear.reach;
  const launch = 250 + near * 170 + Math.random() * 50;
  const flying = 820 + Math.random() * 220;
  const total = launch + flying;
  const open = swung(dir * 12, -4, dir * tear.open);
  const speed = [dir * 70 + (away[0] / distance) * 190 + jitterOf(80), -(150 + Math.random() * 170) + (away[1] / distance) * 110];
  const spin = { z: dir * (35 + Math.random() * 150), rx: jitterOf(300), ry: jitterOf(300) };
  const frames = [
    { at: 0, pose: swung(0, 0, 0), easing: "cubic-bezier(0.3, 0, 0.2, 1)" },
    { at: 90, pose: swung(dir * 2, -3, dir * tear.open * 0.12), easing: "cubic-bezier(0.2, 0.8, 0.3, 1)" },
    { at: 250, pose: open, easing: "linear" },
  ];
  if (launch > 251) frames.push({ at: launch, pose: open, easing: "linear" });
  const samples = 10;
  for (let k = 1; k <= samples; k += 1) {
    const s = k / samples;
    const t = (s * flying) / 1000;
    const turned = 1 - (1 - s) ** 1.6;
    frames.push({ at: launch + s * flying, easing: "linear", pose: {
      x: open.x + speed[0] * t, y: open.y + speed[1] * t + 1000 * t * t,
      z: open.z + spin.z * turned, rx: spin.rx * turned, ry: spin.ry * turned,
      scale: 1 - 0.2 * s, opacity: s < 0.55 ? 1 : 1 - (s - 0.55) / 0.45,
    } });
  }
  const flight = piece.animate(frames.map(({ at, pose, easing }) => ({
    offset: at / total,
    easing,
    opacity: pose.opacity,
    transform: `perspective(700px) translate(${pose.x.toFixed(1)}px, ${pose.y.toFixed(1)}px) rotate(${pose.z.toFixed(2)}deg) rotateX(${pose.rx.toFixed(1)}deg) rotateY(${pose.ry.toFixed(1)}deg) scale(${pose.scale.toFixed(3)})`,
  })), { duration: total, fill: "both" });
  flight.pause();
  flight.finished.catch(() => {}).then(() => piece.remove());
  return flight;
}

function jitterOf(size) {
  return (Math.random() - 0.5) * size;
}

// Sparks from the key's middle, `after` milliseconds into the press.
function burst(x, y, after) {
  const count = 14;
  for (let i = 0; i < count; i += 1) {
    const angle = (i / count) * Math.PI * 2 + (Math.random() - 0.5) * 0.35;
    const reach = 60 + Math.random() * 50;
    const spark = h("span", { class: `press-spark${i % 3 === 0 ? " warm" : ""}`, "aria-hidden": "true" });
    Object.assign(spark.style, { left: `${x}px`, top: `${y}px`, rotate: `${angle}rad` });
    document.body.append(spark);
    spark.animate([
      { translate: "0 0", scale: "1 1", opacity: 1 },
      { translate: `${Math.cos(angle) * reach}px ${Math.sin(angle) * reach}px`, scale: ".2 .6", opacity: 0 },
    ], { duration: 560 + Math.random() * 200, delay: after, easing: "cubic-bezier(0.16, 1, 0.3, 1)" })
      .finished.catch(() => {}).then(() => spark.remove());
  }
}

// A labelled paragraph's words without its label.
function labelText(paragraph) {
  const copy = paragraph.cloneNode(true);
  copy.querySelectorAll(".label-mark, .label-words").forEach((el) => el.remove());
  return copy.textContent.replace(/\s+/g, " ").trim();
}

// The step Next up shows: the first of the queue in the order the user sees.
function nextUp() {
  if (!ui.state) return null;
  const queue = drawn(ui.state).queue;
  const id = shownOrder(queue)[0];
  return queue.find((step) => step.id === id) || null;
}

// The words of the server's rendered Markdown, for places that show plain text.
function plainText(html) {
  return h("span", { html }).textContent;
}

// The state with the item changed: archived items leave with their steps for the closed list, as the user's
// close; an unarchived one leaves the closed list and, when the page has its full row, comes back.
function withItem(state, item, fields) {
  if (fields.archived === true) {
    const timed = timerStopped(state, STEP_LISTS.flatMap((name) => state[name]).filter((step) => step.item_id === item.id).map((step) => step.id));
    const shown = { ...timed, items: state.items.filter((other) => other.id !== item.id) };
    for (const name of STEP_LISTS) shown[name] = state[name].filter((step) => step.item_id !== item.id);
    const row = { id: item.id, key: item.key, title: item.title, project: item.project, stage: item.stage, action: "archived", closed_at: new Date().toISOString(), closed_by: "user", note: "" };
    shown.closed_recently = { ...state.closed_recently, items: [row, ...state.closed_recently.items.filter((other) => other.id !== item.id)] };
    return closedAs(recount(shown), "archived", item.id, true);
  }
  if (fields.archived === false) {
    const closed = { ...state.closed_recently, items: state.closed_recently.items.filter((other) => other.id !== item.id) };
    const items = "ball" in item ? [{ ...item, archived: false }, ...state.items.filter((other) => other.id !== item.id)] : state.items;
    // Back from Close item it is open again; a completed item stays counted (store.closed_by_user).
    const open = closedAs({ ...state, items, closed_recently: closed }, "archived", item.id, false);
    return item.stage === "done" ? open : closedAs(open, "finished", item.id, false);
  }
  // A pin shows as the time it was pinned, kept while the item stays pinned (store.upsert_item).
  const { pinned, ...rest } = fields;
  const shownFields = pinned === undefined ? fields : { ...rest, pinned_at: pinned ? item.pinned_at || new Date().toISOString() : null };
  const patch = (other) => (other.id === item.id ? { ...other, ...shownFields } : other);
  const shown = { ...state, items: state.items.map(patch) };
  for (const name of STEP_LISTS) {
    shown[name] = state[name].map((step) => (step.item_id === item.id ? { ...step, item: patch(step.item) } : step));
  }
  return shown;
}

// The priority tag (CONTROL-7): a click steps the item's priority up through PRIORITIES, a right
// click or Shift+Enter steps it down; both wrap. `about`: a line naming the item, or "".
// The tag is the level's icon alone (bars for low, normal and high, the alert sign for urgent);
// the word is in its tooltip.
const PRIORITY_ICONS = { urgent: "alert", high: "bars-high", normal: "bars-normal", low: "bars-low" };

function priorityTag(item, about) {
  const level = item.priority;
  const step = (by) => setPriority(item, PRIORITIES[(PRIORITIES.indexOf(level) + by + PRIORITIES.length) % PRIORITIES.length]);
  const words = `${level[0].toUpperCase()}${level.slice(1)} priority`;
  return h("button", {
    type: "button",
    class: `prio prio-${level}`,
    title: [about, `${words}. ${TOUCH.matches ? "Tap to raise; after urgent comes low." : "Click to raise, right-click to lower."}`].filter(Boolean).join("\n"),
    "aria-label": words,
    onclick: (event) => { event.stopPropagation(); step(1); },
    oncontextmenu: (event) => { event.preventDefault(); event.stopPropagation(); step(-1); },
    onkeydown: (event) => {
      // While picking, Enter picks the tag (the picker takes its click) rather than lowering the priority.
      if (event.key === "Enter" && event.shiftKey && !ui.picking) { event.preventDefault(); event.stopPropagation(); step(-1); }
    },
  }, icon(PRIORITY_ICONS[level]));
}

// The tag shows the new level at once; a right click steps back, so there is no toast. The queue
// keeps the order the user sees, so a click never moves the step under the pointer (NEXT-16): the sort
// button shows when the new rank changes the order. A refusal puts the old level back with a red toast.
function setPriority(item, level) {
  patchItem(item, { priority: level }, null);
}

// `message`: a toast to show at once ({text, undo}), or null. Its undo waits for the change.
function patchItem(item, fields, message) {
  // Archiving the item takes its steps out of the queue by the user's hand.
  if (fields.archived && ui.state) {
    for (const step of ui.state.queue) if (step.item_id === item.id) takenByUser(step.id);
  }
  // Changes of one item reach the server in the order pressed, each on the page at once, so Close, Undo, Close
  // leaves it closed (CONTROL-17) and an older change sent again after a dropped connection never lands after
  // a newer one (CODE-5); an unarchive after a refused archive has nothing to undo.
  // `sent` settles with whether Cockpit took this change, before the redraw that follows it: the next one waits for it.
  const earlier = ui.patching.get(item.id);
  const previous = "archived" in fields ? ui.archiving.get(item.id) : undefined;
  let answered;
  const sent = new Promise((resolve) => { answered = resolve; });
  const patching = run(`item:${item.id}:${++ui.sendCount}`, () => optimistic(
    (state) => withItem(state, item, fields),
    async () => {
      try {
        if (earlier !== undefined) await earlier;
        const answer = (previous === undefined || (await previous) || fields.archived) ? await api("PATCH", `/api/items/${item.id}`, fields) : null;
        answered(true);
        return answer;
      } catch (error) {
        answered(false);
        throw error;
      }
    },
    null));
  ui.patching.set(item.id, sent);
  if (earlier !== undefined || previous !== undefined) holdLeave(sent);
  sent.then(() => { if (ui.patching.get(item.id) === sent) ui.patching.delete(item.id); });
  if ("archived" in fields) {
    ui.archiving.set(item.id, sent);
    sent.then(() => { if (ui.archiving.get(item.id) === sent) ui.archiving.delete(item.id); });
  }
  if (message) toast(message.text, false, { label: message.undo.label, run: () => patching.then(message.undo.run) });
  return patching;
}

// The text of a box the user sends from leaves it at once. Focus leaves with it, and the
// keys they go on typing do not reach the d/x/z shortcuts.
function takeDraft(key) {
  ui.drafts.delete(key);
  ui.choices.delete(key);
  ui.starts.delete(key);
  saveDrafts();
  const el = document.activeElement;
  if (el && el.dataset && el.dataset.key === key) { el.blur(); ui.focusLost = true; }
}

// What the sent mark on a step remembers: it goes once an agent rewrites the step.
function stepVersion(step) {
  return step.prepared_at || step.created_at;
}

// Rows that stay drawn after their step left (held now, or kept) keep their mark.
// An Ask the last state showed as asking and this one shows answered gets a toast (MSG-11).
function noticeSideAnswers(state) {
  const chats = state.side_chats || [];
  const was = ui.sideAsking;
  ui.sideAsking = new Set(chats.filter((chat) => chat.state === "asking").map((chat) => chat.id));
  if (!was) return;
  const done = chats.filter((chat) => was.has(chat.id) && chat.state !== "asking");
  if (!done.length) return;
  if (done.length > 1) {
    const failed = done.filter((chat) => chat.state === "failed").length;
    toast(`${done.length} quick answers came back${failed ? `, ${failed} without an answer` : ""}`, failed > 0);
    return;
  }
  const item = state.items.find((each) => each.id === done[0].item_id);
  const where = item ? ` on “${clip(item.title, 60)}”` : "";
  if (done[0].state === "answered") toast(`Your quick question has an answer${where}`);
  else toast(`Your quick question got no answer${where}: ${done[0].error}`, true);
}

function forgetSentMarks(state) {
  const versions = new Map(STEP_LISTS.flatMap((name) => state[name]).map((step) => [step.id, stepVersion(step)]));
  let changed = false;
  for (const [id, mark] of ui.sent) {
    // A sent mark still local after a reload, whose send the outbox finished (errors.js), takes the server's message.
    if (typeof mark.id === "string") {
      // The newest such message: the same words sent twice name the later send.
      const message = state.messages.filter((each) => each.step_id === id && each.author === "user" && each.text === mark.text
        && Date.parse(each.created_at) >= Date.parse(mark.at) - 60e3).sort((a, b) => b.id - a.id)[0];
      if (message) { mark.id = message.id; changed = true; }
    }
    if (!versions.has(id) && (ui.held.has(id) || ui.left.has(id))) continue;
    if (versions.get(id) !== mark.version) { ui.sent.delete(id); changed = true; }
  }
  if (changed) saveSent();
}

// On a closed row: the user wrote from this step.
function sentMark(step) {
  const mark = ui.sent.get(step.id);
  if (!mark || mark.version !== stepVersion(step)) return null;
  return h("span", { class: "sent-mark", role: "img", "aria-label": "You sent a message from this step",
    title: `You sent to ${mark.to || "the session"}: ${clip(mark.text, 300)}`, "data-time": mark.at }, icon("send"));
}

// Send: the box empties and closes at once, the message shows under Messages as
// sending, and the step (when it is from a step) gets a sent mark. If the server
// refuses, the words go back into the box and the toast says why.
// `context`: {item_id, step_id} or {}; `where`: {session_id} or {new_cwd}; `label`: the target's name.
function sendMessage(key, context, where, label, step) {
  const sent = ui.drafts.get(key) || "";
  const text = sent.trim();
  if (!text) return null;
  // The box's staged images go on the message's task (SPEC MSG-18); a message with none keeps them and its words.
  const itemId = messageItem(context, step, where);
  if (stagedImages(key).length && itemId === null) {
    toast("Not sent: images go on a task, and this message has none. Remove the images, or send from the task", true);
    return null;
  }
  // A to-do the user added has no session until its first message started one. Words sent meanwhile to the box's
  // default, a new session, go as Add more's do: they join that first message while it waits, rather than start a
  // second session without the to-do's text. Another checkout, or picks for the new session, start one.
  const todo = where.new_cwd && !step && !ui.starts.has(key) && drawn(ui.state).items.find((one) => one.id === context.item_id);
  if (todo && todo.stage !== "done" && !todo.archived && todo.todo && todo.key.startsWith("todo-") && !todo.sessions.length
    && where.new_cwd === defaultCheckout(todo.project || null, ui.state)) {
    const wasOpen = ui.expanded.has(key);
    const choice = ui.choices.get(key) || null;
    takeDraft(key);
    ui.expanded.delete(key);
    // Its images go on the to-do, which the first session's message lists (delivery._new_item_session).
    const staged = takeStaged(key);
    return sendMore(todo, key, sent, choice, staged, () => {
      restoreStaged(key, staged);
      const back = putBack(key, sent, choice, null);
      if (back && wasOpen) ui.expanded.add(key);
      return back;
    });
  }
  const at = new Date().toISOString();
  const local = {
    id: `local-${++ui.sendCount}`, author: "user", text, state: "sending", created_at: at, detail: "",
    session_id: where.session_id || null, new_cwd: where.new_cwd || null, new_name: null,
  };
  const wasOpen = ui.expanded.has(key);
  // A refusal puts back the mark of the message sent before this one.
  const earlier = step ? ui.sent.get(step.id) : null;
  const choice = ui.choices.get(key) || null;
  const picks = ui.starts.get(key) || null;
  takeDraft(key);
  const images = takeStaged(key);
  ui.expanded.delete(key);
  // The step's sent mark: the local message until the server names it.
  const mark = { text, at, version: step ? stepVersion(step) : null, to: label, id: local.id, earlier };
  if (step) { ui.sent.set(step.id, mark); saveSent(); }
  let back = true;
  // The hand-offs to the same session still on their way, such as one waiting for its step's page choices to save,
  // go first: this message comes after them in the session.
  const going = where.session_id ? ui.handingOff.get(where.session_id) : null;
  const handing = going ? Promise.all(going) : null;
  if (handing) holdLeave(handing);
  return run(`${key}:${local.id}`, async () => {
    try {
      const words = { key, text: sent, choice, picks };
      const reopens = reopening(context.item_id);
      const reopen = reopenBy(reopens, context.item_id);
      const answer = await optimistic((state) => reopen({ ...state, messages: [local, ...state.messages] }),
        async () => {
          if (handing) await handing;
          const ids = await uploadStaged(itemId, images);
          return afterFinishes(() => apiWritten("POST", "/api/messages", { text, target: { ...context, ...where }, images: ids }, words));
        },
        () => {
          restoreStaged(key, images);
          back = putBack(key, sent, choice, picks);
          if (wasOpen) ui.expanded.add(key);
          takeBackReopened(reopens);
          // Only while the mark is still this message's: a later send keeps its own. It goes back to the
          // latest earlier send that was not refused too.
          mark.refused = true;
          let previous = earlier;
          while (previous?.refused) previous = previous.earlier;
          if (!step || ui.sent.get(step.id) !== mark) return;
          if (previous) ui.sent.set(step.id, previous);
          else ui.sent.delete(step.id);
          saveSent();
        });
      mark.id = answer.id;
      if (step) saveSent();
      reopenConfirmed(reopens);
    } catch (error) {
      throw keptInNotice(refused(`Not sent to ${label}`, error), back, text);
    }
  }, () => setTimeout(refresh, 2500));
}

// The task as the page lists it when it has ended, which the user's message takes up again (SPEC CONTROL-28): its item,
// or its row in Finished this week when the task was closed as not needed and has only that row. Null while open.
function finishedTask(state, itemId) {
  if (itemId == null) return null;
  const item = state.items.find((one) => one.id === itemId);
  if (item) return item.stage === "done" || item.archived ? item : null;
  return state.closed_recently.items.find((one) => one.id === itemId) || null;
}

// The state with a finished task back in Busy, as the server has it once it took the message
// (store.reopen_for_message). It reads the state it is given, so a second Send on the task keeps it in Busy when the
// first is refused. A task with only a closed row has no item to show in Busy until the page reads it.
function reopenedByMessage(state, itemId) {
  if (!finishedTask(state, itemId) || !state.items.some((item) => item.id === itemId)) return state;
  const elsewhere = (step) => step.item_id !== itemId;
  const items = state.items.map((item) => (item.id === itemId
    ? { ...item, stage: "building", waiting_on: [], archived: 0, ball: "agents", group: "busy", tip: "", tip_at: null, why: "", ended: null }
    : item));
  const closed = { ...state.closed_recently, items: state.closed_recently.items.filter((other) => other.id !== itemId) };
  const reopened = {
    ...state, items, closed_recently: closed, queue: state.queue.filter(elsewhere), ready_next: state.ready_next.filter(elsewhere),
    timer: state.timer && state.timer.item_id === itemId ? null : state.timer,
  };
  return closedAs(closedAs(reopened, "finished", itemId, false), "archived", itemId, false);
}

// What this Send does to the drawn state: only a Send that found the task finished moves it, or one right behind such a
// Send, which keeps it moved should the first be refused. An older message still on its way leaves a later finish alone.
function reopenBy(reopens, itemId) {
  return reopens || ui.reopenedHeld?.id === itemId ? (state) => reopenedByMessage(state, itemId) : (state) => state;
}

// The finished task the user's words are about to reopen, with the page saying so, or null.
function reopening(itemId) {
  const task = finishedTask(drawn(ui.state), itemId);
  if (task) showReopened(task);
  return task;
}

// Says it three ways, since the task left the place the user last saw it: a notice, its row first and lit in Busy, and
// Busy's heading lit while Busy is folded (sidebar.js `reopenedHeading`), which the list scrolls to.
function showReopened(task) {
  clearTimeout(ui.reopenedHeld?.timer);
  const hold = { id: task.id, at: Date.now(), timer: null };
  hold.timer = setTimeout(() => {
    if (ui.reopenedHeld === hold) ui.reopenedHeld = null;
    safeRender();
  }, REOPENED_HEADING_MS);
  ui.reopenedHeld = hold;
  ui.arrived = { sent: null, id: task.id, scrolled: false };
  toast(`Reopened “${task.title}”. It is back in Busy.`);
}

// A finish pressed just before may still wait in `inTurn`, with the task finished only on this page. Words sent now go
// after it, or the server would see an open task and the finish would close it again. With nothing waiting they go at once.
function afterFinishes(send) {
  return turnsWaiting ? inTurn(send) : send();
}

// The server took the words. A close whose Undo is still on offer ends now, as the words took the task up again.
function reopenConfirmed(task) {
  if (!task) return;
  for (const hold of [...ui.closesHeld]) if (hold.itemIds.includes(task.id)) hold.end();
  refresh();
}

// The server refused the words, so the task stayed finished. Another task's notice and lit heading stay.
function takeBackReopened(task) {
  if (!task) return;
  if (ui.reopenedHeld?.id === task.id) {
    clearTimeout(ui.reopenedHeld.timer);
    ui.reopenedHeld = null;
  }
  if (ui.arrived?.id === task.id) ui.arrived = null;
}

// A refusal whose words did not go back into their box (`putBack`) says so, and its tooltip holds them.
function keptInNotice(error, back, text) {
  return back ? error : Object.assign(error, { message: `${error.message}. ${NOT_PUT_BACK}`, tip: `${error.tip}\nYour words, not sent: ${text}` });
}

// Ask (MSG-11): the words in the box go to a read-only copy of the session, and stay in the box for the user to
// go on writing. The question shows above the box at once, and the copy's answer under it; the server then sends
// both to the session (sidechat.answer).
function askQuestion(key, itemId, stepId, sessionId, label) {
  const text = (ui.drafts.get(key) || "").trim();
  if (!text) return null;
  const local = {
    id: `local-${++ui.sendCount}`, item_id: itemId, step_id: stepId, session_id: sessionId, question: text, answer: null,
    answer_html: "", error: null, state: "asking", asked_at: new Date().toISOString(), answered_at: null,
    sent_message_id: null, sent_state: null, sent_at: null,
  };
  return run(`${key}:${local.id}`, async () => {
    try {
      const asked = optimistic((state) => ({ ...state, side_chats: [...(state.side_chats || []), local] }),
        () => api("POST", `/api/items/${itemId}/ask`, { session_id: sessionId, step_id: stepId, text }), null);
      toast(`Asked a copy of ${label}; the answer shows above the box`);
      // A copy can answer before any state shows its question asking; the next state still toasts the answer.
      const chat = await asked;
      const seen = (ui.state.side_chats || []).find((each) => each.id === chat.id);
      if (ui.sideAsking && !(seen && seen.state !== "asking")) ui.sideAsking.add(chat.id);
    } catch (error) {
      throw refused("Not asked", error);
    }
  });
}

// Fork (MSG-11): the words start a new item, whose session starts from this session's conversation and works on
// its own; nothing goes back, and the step stays as it was. The box empties at once, and if the server refuses,
// the words and the box's To and start picks go back into it, as Send's do (`putBack`).
function forkSession(key, itemId, sessionId, label) {
  const sent = ui.drafts.get(key) || "";
  const text = sent.trim();
  if (!text) return null;
  const choice = ui.choices.get(key) || null;
  const picks = ui.starts.get(key) || null;
  takeDraft(key);
  render(false);
  return run(`${key}:fork:${++ui.sendCount}`, async () => {
    try {
      const forked = await apiWritten("POST", `/api/items/${itemId}/fork`, { session_id: sessionId, text },
        { key, text: sent, choice, picks });
      toast(`Forked ${label}: the new session works on “${clip(forked.item.title, 60)}”`);
    } catch (error) {
      const back = putBack(key, sent, choice, picks);
      throw keptInNotice(refused(`Not forked from ${label}`, error), back, text);
    }
  }, () => setTimeout(refresh, 2500));
}

// Above a box: the quick questions the user asked from it with Ask (MSG-11), each with the copy's answer and where
// the two stand on their way to the session. A Send from the box after the answer took them along, and they leave
// (sidechat.recent). `step`: the box's step, or null for the item's own box.
function askBlock(itemId, stepId, state) {
  const chats = (state.side_chats || []).filter((chat) => chat.item_id === itemId && (chat.step_id ?? null) === stepId);
  const sessions = sessionMap(state);
  const entry = (chat) => {
    const name = sessionLabel(sessions.get(chat.session_id), "") || chat.session_id.slice(0, 8);
    const message = chat.sent_message_id !== null ? state.messages.find((each) => each.id === chat.sent_message_id) : null;
    const answer = chat.answer_html && prose(`ask:${chat.id}`, "ask-line prose", chat.answer_html);
    let status = null;
    if (chat.state === "failed") status = h("p", { class: "ask-state fail-text", text: `No answer: ${chat.error}` });
    else if (chat.sent_state === "sent") status = h("p", { class: "ask-state", "data-time": chat.sent_at, text: `Reached ${name} at ${clock(chat.sent_at)}.` });
    else if (chat.sent_state === "failed") {
      status = h("p", { class: "ask-state fail-text" }, `Did not reach ${name}${message && message.detail ? `: ${message.detail}` : ""}. `,
        h("button", { class: "button small ghost", type: "button", onclick: () => retryMessage({ id: chat.sent_message_id }) }, "Send again"));
    } else if (chat.state === "answered") status = h("p", { class: "ask-state", text: `Goes to ${name} with your reply, or when its current turn ends.` });
    return h("div", { class: `ask-entry ${chat.state}`, "data-side-chat": chat.id },
      h("p", { class: "ask-who", "data-time": chat.asked_at, text: `You asked · ${clock(chat.asked_at)}` }),
      h("p", { class: "ask-line ask-question", text: chat.question }),
      chat.state === "asking"
        ? h("p", { class: "ask-who asking" }, h("span", { class: "state-dot sending", role: "img", "aria-label": "Answering" }), `A copy of ${name} is answering…`)
        : answer && h("p", { class: "ask-who", "data-time": chat.answered_at, text: `Quick answer from a copy of ${name} · ${clock(chat.answered_at)}` }),
      answer,
      status);
  };
  return h("section", { class: "ask-block", "aria-label": "Quick questions", "data-ask-item": itemId, "data-ask-step": stepId ?? "" },
    chats.map(entry));
}

// A section the poll leaves alone while the user types in it still shows each answer as it arrives: its Ask
// blocks are drawn again in place, and the reply box beside them stays as it is.
function redrawAskBlocks(ids, state) {
  for (const id of ids) {
    for (const old of document.querySelectorAll(`#${id} .ask-block`)) {
      const stepId = old.dataset.askStep === "" ? null : Number(old.dataset.askStep);
      old.replaceWith(askBlock(Number(old.dataset.askItem), stepId, state));
    }
  }
}

function handedOff(state, step, handoff) {
  const shown = withoutStep(timerStopped(state, [step.id]), step.id);
  shown.with_agents = [...shown.with_agents, { ...step, queued_at: null, handoff }];
  return recount(shown);
}

// Where the user acts on a step: the card or row that holds the key they pressed; else Next up's card, then the step's
// row in the queue, and the list on the left only when the step shows nowhere else.
function stepCard(stepId, pressed) {
  const own = pressed && pressed.closest(`[data-step="${stepId}"]`);
  if (own) return own;
  const shown = [...document.querySelectorAll(`[data-step="${stepId}"]`)].filter((el) => !el.closest(".motion-ghost, .close-copy, #toast"));
  return shown.find((el) => el.closest("#next")) || shown.find((el) => el.closest("#queue")) || shown.find((el) => !el.closest("#sidebar")) || shown[0] || null;
}

// Where the copy of a step's ask starts its flight: the step, or what the user pressed or wrote in
// when the step's top is off screen.
function flightStart(step, pressed) {
  const card = stepCard(step.id, null);
  const stepRect = card ? card.getBoundingClientRect() : null;
  return stepRect && stepRect.top < 0 && pressed ? pressed.getBoundingClientRect() : stepRect;
}

// Hand off the reply the user wrote: the draft leaves the box, and comes back if the server refuses.
function handOffDraft(key, step, where, label) {
  const sent = ui.drafts.get(key) || "";
  const text = sent.trim();
  // The user's page comments and choices on the step go with the answer (NEXT-38, NEXT-48), so they alone are an answer too.
  if (!text && !stepPageComments(step, ui.state).length && !stepPageChoices(step, ui.state).length) return null;
  const fromRect = flightStart(step, document.querySelector(`textarea[data-key="${CSS.escape(key)}"]`));
  const choice = ui.choices.get(key) || null;
  const picks = ui.starts.get(key) || null;
  takeDraft(key);
  const images = takeStaged(key);
  return handOffStep(step, text, where, images, label, fromRect, { key, text: sent, choice, picks }, () => {
    restoreStaged(key, images);
    return putBack(key, sent, choice, picks);
  });
}

// Choices still saving from a page go with the user's answer to the step (SPEC NEXT-48): every answer waits for
// them, Send, Task done and a secret alike, and refuses with the reason while one made for this step
// failed to save. Leaving asks first while they save, as the answer is not on its way yet.
async function choicesSaved(step) {
  const choices = (async () => {
    // A comment hidden by Delete is still on the server until that request lands. Check again after each wait,
    // since more deletions or choices can arrive while the other kind saves.
    const pending = () => [...commentDeletions.values()].filter((entry) => entry.stepId === step.id);
    let deletions = pending();
    for (;;) {
      if ((await Promise.all(deletions.map((entry) => entry.sent))).some((done, index) => !done && pending().includes(deletions[index]))) {
        throw new Error("A page comment was not deleted; delete it again, then send");
      }
      await CockpitChoices.settled(step.id);
      deletions = pending();
      if (!deletions.length) return;
    }
  })();
  holdLeave(choices.catch(() => {}));
  await choices;
}

// Hand off: like Send, and the step leaves the user's queue at once for You handed these under With agents,
// a copy of its ask flying there. If the server refuses, the step comes back and
// `undo` (or null) puts back what else changed, and returns false when the words stay in the refusal's notice
// (`putBack`). `images`: the box's staged images (SPEC MSG-18), which go up to the step's item first.
function handOffStep(step, text, where, images, label, fromRect, words, undo) {
  followQueue(step);
  // Before the redraw takes Next up's card away: a copy of it leaves for Busy while the next step shows under it.
  const away = sendAway(step);
  const handoff = {
    session_id: where.session_id || null, new_cwd: where.new_cwd || null, text, author: "user",
    state: "queued", detail: "", created_at: new Date().toISOString(),
  };
  takenByUser(step.id);
  // The step's sent mark, for its closed row: the message, once the server names it.
  const earlier = ui.sent.get(step.id);
  // `key`: the box the words were written in, where Take back puts them (the step's reply box, or a session's
  // conversation box, sidebar.js's drawSessionComposer).
  const mark = { itemId: step.item_id, text, at: handoff.created_at, version: stepVersion(step), to: label, id: `local-${++ui.sendCount}`, key: words ? words.key : `msg:step:${step.id}`, earlier };
  ui.sent.set(step.id, mark);
  saveSent();
  // Settles with whether Cockpit took the hand-off, before the redraw that follows it: Take back waits for this.
  let answered;
  const sent = new Promise((resolve) => { answered = resolve; });
  // A Take back still on its way goes first, as a press after it waits for it (`act`).
  const before = ui.inFlight.get(step.id);
  let back = true;
  const reopens = reopening(step.item_id);
  const reopen = reopenBy(reopens, step.item_id);
  const handing = run(`hand-off:${step.id}:${mark.id}`, async () => {
    try {
      const message = await optimistic((state) => reopen(handedOff(state, step, handoff)),
        async () => {
          if (before) await before.request;
          await choicesSaved(step);
          const body = { text, images: await uploadStaged(step.item_id, images), ...where };
          return afterFinishes(() => (words ? apiWritten("POST", `/api/steps/${step.id}/hand-off`, body, words) : api("POST", `/api/steps/${step.id}/hand-off`, body)));
        },
        () => {
          if (undo) back = undo() !== false;
          takeBackReopened(reopens);
          // As in `sendMessage`: back to the latest earlier send that went, while the mark is still this one's.
          mark.refused = true;
          let previous = earlier;
          while (previous?.refused) previous = previous.earlier;
          if (ui.sent.get(step.id) !== mark) return;
          if (previous) ui.sent.set(step.id, previous); else ui.sent.delete(step.id);
          saveSent();
        });
      answered(true);
      mark.id = message.id;
      takeBackDeadline(mark, message);
      saveSent();
      reopenConfirmed(reopens);
    } catch (error) {
      answered(false);
      throw keptInNotice(refused(`Not sent to ${label}`, error), back, text);
    }
  }, () => setTimeout(refresh, 2500));
  track(step.id, sent, false);
  if (before) holdLeave(sent);
  if (handoff.session_id) {
    const id = handoff.session_id;
    if (!ui.handingOff.has(id)) ui.handingOff.set(id, new Set());
    ui.handingOff.get(id).add(sent);
    sent.then(() => {
      const going = ui.handingOff.get(id);
      if (!going) return;
      going.delete(sent);
      if (!going.size) ui.handingOff.delete(id);
    });
  }
  const shown = handedNotice(step, label, sent, () => releaseHandOff(handing, mark), reopens);
  mark.notice = shown;
  if (shown !== null) offerFollowUp(shown, handing, mark, label, step.id);
  if (away) away(); else flyToAgents(step, fromRect);
  return handing;
}

// How long a hand-off's Undo stays in Busy: as long as the notice with Take back shows where there is no list.
const UNDO_HAND_MS = TOAST_MS;

// Where a sent step goes to wait for its Undo time (MSG-8). On the page with the list at the left, the step goes to
// Busy, and its Undo sits there for UNDO_HAND_MS: on Busy's heading while Busy is folded, as the step's row once it is
// open (`handHeld` in sidebar.js), and on a phone's item screen, which hides the list, on the bar's Your items; no
// notice shows. The message waits on the server until the Undo time ends (`gone`), and Undo takes the step back as
// Take back does. The classic page draws no list: there the notice with Take back and Add more (MSG-10) shows, and
// its number comes back; on the list page this returns null. So it does when another step of the item stays in Inbox or
// Waiting next, or is with an agent already: the list draws each item once, by that step, so it draws no row for the
// sent step to hold the Undo.
// `sent` settles with whether Cockpit took the hand-off.
function handedNotice(step, label, sent, gone, reopens) {
  const listed = [...ui.state.queue, ...ui.state.ready_next, ...ui.state.with_agents].some((other) => other.item_id === step.item_id && other.id !== step.id && !other.left);
  if (!undoHeldInList() || listed) {
    // This notice replaces the one that said the task reopened, so it says that too.
    const reopened = reopens ? `Reopened “${reopens.title}”, back in Busy. ` : "";
    return toast(`${reopened}Sent to ${label}; the step comes back only if it needs you`, false,
      { label: "Take back", run: () => takeBack(step), gone },
      "Your message goes once this notice is gone; until then Take back stops it for certain.");
  }
  const hold = { at: Date.now(), stepId: step.id, to: label, timer: null };
  const release = () => {
    if (!ui.handsHeld.has(hold)) return false;
    clearTimeout(hold.timer);
    ui.handsHeld.delete(hold);
    return true;
  };
  hold.undo = () => takeBack(step);
  hold.end = () => { if (hold.returning || !release()) return; gone(); render(false); };
  // A refused hand-off, or a Take back Cockpit took, ends the Undo with nothing to release.
  hold.drop = () => { if (release()) render(false); };
  // A Take back on its way (`takeBack`) stops the Undo's time; should Cockpit refuse it, the message goes as it would
  // have once the time ran out.
  // A Take back pressed again after a refusal takes over: only the latest one settles the Undo.
  hold.returning = false;
  hold.takingBack = (acting) => {
    hold.returning = true;
    hold.acting = acting;
    clearTimeout(hold.timer);
    acting.then((done) => {
      if (hold.acting !== acting) return;
      hold.returning = false;
      if (done) hold.drop(); else hold.end();
    });
  };
  hold.timer = setTimeout(hold.end, UNDO_HAND_MS);
  ui.handsHeld.add(hold);
  sent.then((done) => { if (!done) hold.drop(); });
  render(false);
  return null;
}

// Add more (MSG-10): while the notice with Take back shows, the user writes a follow-up for the same session.
// The notice waits while the box is open, and so does the message on the server (FOLLOW_HOLD_MS). The follow-up
// joins the first message while that waits for the notice; once the first went, it goes after it.
const FOLLOW_HOLD_MS = 20 * 1000;
// A notice stays while the box is open, the pointer rests on it or focus is in it (runToast), and Take back keeps its
// promise only while the server's wait runs (MSG-8), so the wait starts again for as long as the notice stays.
function holdWhileNoticeStays(shown, mark) {
  if (mark.holdsFor === shown) return;
  mark.holdsFor = shown;
  const el = document.getElementById("toast");
  const holding = setInterval(() => {
    if (shown !== ui.toastShown || el.hidden) { clearInterval(holding); return; }
    if (el.matches(":hover, :focus-within, .following")) holdMessage(mark);
  }, FOLLOW_HOLD_MS);
}
function holdMessage(mark) {
  if (typeof mark.id !== "number" || (mark.takeBackUntil && mark.takeBackUntil <= Date.now())) return;
  return api("POST", `/api/messages/${mark.id}/hold`).then((message) => takeBackDeadline(mark, message))
    .catch((error) => logError("hold message", error));
}
// Take back follows the server's deadline even while hover or a follow-up pauses the notice timer.
function takeBackDeadline(mark, message) {
  clearTimeout(mark.takeBackTimer);
  mark.takeBackUntil = Date.parse(message.take_back_until) || Date.now();
  const expire = () => {
    if (Date.now() < mark.takeBackUntil) return;
    if (ui.toastWaiting?.id === mark.notice) {
      ui.toastWaiting.setups.push(expire);
      return;
    }
    if (mark.notice !== ui.toastShown) return;
    const button = document.getElementById("toast").querySelector(".toast-action");
    if (button) {
      button.disabled = true;
      button.title = "The message is no longer held for Take back.";
    }
  };
  mark.takeBackTimer = setTimeout(expire, Math.max(0, mark.takeBackUntil - Date.now()));
}
// This box belongs to the notice and has no stored draft. Leaving asks while it holds words still being written.
addEventListener("beforeunload", (event) => {
  const box = document.querySelector("#toast:not([hidden]) .toast-follow textarea");
  if (box?.value.trim()) event.preventDefault();
});
function offerFollowUp(shown, handing, mark, label, stepId) {
  const waiting = ui.toastWaiting;
  if (waiting && waiting.id === shown) {
    waiting.setups.push(() => offerFollowUp(shown, handing, mark, label, stepId));
    return;
  }
  const el = document.getElementById("toast");
  if (shown !== ui.toastShown || el.hidden) return;
  holdWhileNoticeStays(shown, mark);
  el.insertBefore(h("button", { type: "button", class: "toast-more",
    onclick: () => openFollowUp(shown, handing, mark, label, stepId) }, "Add more"), el.querySelector(".toast-time"));
}

function openFollowUp(shown, handing, mark, label, stepId) {
  const el = document.getElementById("toast");
  if (shown !== ui.toastShown || el.hidden) return;
  const box = h("textarea", { rows: 3, "aria-label": `More for ${label}`, placeholder: `More for ${label}…` });
  const close = () => {
    form.remove();
    el.classList.remove("following");
    offerFollowUp(shown, handing, mark, label, stepId);
    runToast();
    showWaitingToast();
  };
  const send = () => {
    const text = box.value.trim();
    if (!text) { box.focus(); return; }
    form.remove();
    el.classList.remove("following");
    sendFollowUp(shown, handing, mark, label, stepId, text);
  };
  box.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); send(); }
    else if (event.key === "Escape") { event.preventDefault(); close(); }
  });
  const form = h("div", { class: "toast-follow", "data-step": stepId }, box,
    h("div", { class: "toast-follow-actions" },
      h("button", { type: "button", class: "toast-follow-cancel", onclick: close }, "Cancel"),
      h("button", { type: "button", class: "toast-follow-send", onclick: send }, icon("send"), "Send")));
  el.querySelector(".toast-more")?.remove();
  el.classList.add("following");
  // A toast that waited for the press that opened the box now waits for the box.
  if (ui.toastWaiting) ui.toastWaiting.forPress = false;
  el.insertBefore(form, el.querySelector(".toast-time"));
  holdMessage(mark);
  holdToast();
  box.focus();
}

// The follow-up goes once the server named the first message; the notice says where it went and starts its
// time again, so Take back still stops both while it shows.
function sendFollowUp(shown, handing, mark, label, stepId, text) {
  retoast(shown, `Added for ${label}; it goes with your message once this notice is gone.`, "");
  offerFollowUp(shown, handing, mark, label, stepId);
  // The task can have finished elsewhere since the hand-off; the server then reopens it for these words.
  // It waits in the page behind its hand-off, so leaving asks first until it went.
  holdLeave(run(`follow-up:${mark.id}:${++ui.sendCount}`, async () => {
    if (!(await handing) || typeof mark.id !== "number") throw Object.assign(new Error("Not added: your message did not go"), { tip: `Your words: ${text}` });
    try {
      const reopens = finishedTask(drawn(ui.state), mark.itemId);
      const added = await afterFinishes(() => api("POST", `/api/messages/${mark.id}/follow-up`, { text }));
      if (added.joined) takeBackDeadline(mark, added);
      if (added.joined) { mark.text = `${mark.text}\n\n${text}`; saveSent(); }
      else retoast(shown, `Sent to ${label} after your first message, which had already gone.`, "");
      if (reopens && added.reopened) { showReopened(reopens); reopenConfirmed(reopens); }
    } catch (error) {
      throw Object.assign(refused("Not added", error), { tip: `${error.message}\nYour words: ${text}` });
    }
  }, null));
}

// The notice with Take back after a hand-off is gone (MSG-8): the server kept the message back meanwhile,
// and it goes now. A hand-off still on its way is released once the server answered; a refused one has
// nothing to release. Should the release fail, the server sends the message itself a minute after Send.
function releaseHandOff(handing, mark) {
  handing.then((done) => {
    if (!done || typeof mark.id !== "number") return;
    run(`release:${mark.id}`, async () => {
      try {
        await api("POST", `/api/messages/${mark.id}/release`);
      } catch (error) {
        throw refused("Not sent yet, Cockpit sends it itself a minute after you sent it", error);
      }
    }, null);
  });
}

// Run and Allow (NEXT-18): the server does the work as the user and hands the step to its session
// with a message of its own; the step leaves for You handed these at once and comes back if refused.
function pressStepButton(step, path, body, handoffText, refusal, pressed, after) {
  const before = ui.inFlight.get(step.id);
  if (before && !before.undoing) return Promise.resolve(false);
  followQueue(step);
  const now = new Date().toISOString();
  const handoff = {
    session_id: body.session_id, new_cwd: body.new_cwd, text: handoffText, author: "cockpit",
    state: "queued", detail: "", created_at: now,
  };
  // A run shows as running under You handed these until the server's state says how it ended.
  const shown = path === "run" ? { ...step, run: { started_at: now, finished_at: null, exit_code: null, output: "" } } : step;
  const fromRect = flightStart(step, pressed);
  takenByUser(step.id);
  let answered;
  const sent = new Promise((resolve) => { answered = resolve; });
  const pressing = run(`${path}:${step.id}:${++ui.sendCount}`, async () => {
    try {
      await optimistic((state) => handedOff(state, shown, handoff),
        async () => {
          if (before) await before.request;
          await choicesSaved(step);
          return api("POST", `/api/steps/${step.id}/${path}`, body);
        });
      answered(true);
    } catch (error) {
      answered(false);
      throw refused(refusal, error);
    }
  }, after);
  track(step.id, sent, false);
  if (before) holdLeave(sent);
  flyToAgents(step, fromRect);
  return pressing;
}

function runCommand(step, label, pressed) {
  const { run: command, cwd } = step.command;
  const seen = { command, cwd, ...pressTarget(step) };
  const running = pressStepButton(step, "run", seen, `Ran: ${command}`, "Not run", pressed, () => setTimeout(refresh, 2500));
  toast(`Running; ${label} gets the output`, false, null, `${command}\nin ${cwd}`);
  return running;
}

function allowPermission(step, label, pressed) {
  const { list, rule } = step.permission;
  const seen = { list, rule, ...pressTarget(step) };
  const allowing = pressStepButton(step, "allow", seen, `Allowed: ${rule}`, "Not allowed", pressed, () => {
    setTimeout(refresh, 2500);
    if (ui.permissions && ui.permissions !== "loading") loadPermissions();
  });
  toast(`Rule added; ${label} goes ahead`);
  return allowing;
}

// How a step's command run went, in a word or two.
function runWords(run) {
  if (!run.finished_at) return "Running";
  return run.exit_code === null ? "Did not finish" : `Exit ${run.exit_code}`;
}

// The end of a run's output, for a tooltip.
function outputTail(run) {
  const output = run.output.trimEnd();
  if (!output) return run.finished_at ? "It printed nothing." : "";
  return output.length > RUN_TIP_CHARS ? `…${output.slice(-RUN_TIP_CHARS)}` : output;
}

// A copy of the step's ask flies from where the step was to its row under Busy in All work, or to that group's
// heading when the group is closed; the count there bumps.
// Send on Next up: the card lifts off the page and flies into Busy in the list on the left, shrinking as it goes, while the
// step after it already stands where it was. Simpler than a close: one glide of transform and opacity. Called before
// the redraw, it copies the card and asks motion.js to draw the next card in place (Motion.coverNext); the function
// it returns starts the flight once the redraw drew Busy with the step in it. Null when the step is not on Next up.
// The copy narrows to the card's top, the ask, and lands as a strip on the step's row in Busy, or on Busy's heading
// while Busy is folded, which then lights up.
function sendAway(step) {
  const card = document.querySelector(`#next > [data-step="${step.id}"]:not(.motion-ghost)`);
  const from = card && card.getBoundingClientRect();
  if (!from || !from.width || from.bottom <= 0 || from.top >= innerHeight) return null;
  const sheet = h("div", { class: "close-fx send-away", "aria-hidden": "true" }, closeCopy(card, from, Pick.ground(card)));
  sheet.inert = true;
  Object.assign(sheet.style, { left: `${from.left}px`, top: `${from.top}px`, width: `${from.width}px`, height: `${from.height}px` });
  document.body.append(sheet);
  Motion.coverNext();
  // Busy's place for the step: its row while Busy is open, else its heading. A folded Busy still draws the row while
  // it glides there from Inbox, so only an open Busy's row is a place.
  const busyPlace = () => {
    const group = document.querySelector("#sidebar .sidebar-group.busy");
    const row = group && group.open && group.querySelector(`li[data-step="${step.id}"]`);
    const target = row && row.getBoundingClientRect().height ? row : group && group.querySelector(":scope > summary");
    return { target, count: group && group.querySelector(".group-count") };
  };
  return () => {
    const { target } = busyPlace();
    const to = target && settledRect(target);
    // A poll's redraw may have replaced Busy while the copy flew, so the landing lights up the Busy drawn then.
    const land = () => {
      const now = busyPlace();
      // A heading that holds the Undo lights up and rolls its count on its own (sidebar.js `headingUndo`).
      if (now.target && now.target.classList.contains("holding")) return;
      if (now.count) now.count.animate([{ scale: "1" }, { scale: "1.25" }, { scale: "1" }], { duration: 200, easing: EASE_OUT });
      if (now.target && !REDUCED.matches) {
        now.target.animate([{ backgroundColor: "var(--accent-bg)" }, { backgroundColor: "transparent" }], { duration: 700, easing: "ease-out" });
      }
    };
    const done = () => { sheet.remove(); land(); };
    if (REDUCED.matches) {
      sheet.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 200, fill: "both" }).finished.catch(() => {}).then(done);
      return;
    }
    sheet.style.transformOrigin = "0 0";
    // Busy on screen: the copy narrows to its top strip, the ask, and that strip lands on Busy at Busy's width.
    // Without the list on the left, or with Busy scrolled out of its sight, the card lifts up and away.
    const list = document.getElementById("sidebar");
    const sight = list && list.getBoundingClientRect();
    const shown = to && to.width && sight && to.bottom > Math.max(0, sight.top) && to.top < Math.min(innerHeight, sight.bottom);
    if (!shown) {
      sheet.animate([
        { transform: "translate(0, 0) scale(1)", opacity: 1 },
        { transform: `translate(${from.width * 0.05}px, -48px) scale(0.9)`, opacity: 0 },
      ], { duration: SEND_AWAY_MS, easing: EASE_OUT, fill: "both" }).finished.catch(() => {}).then(done);
      return;
    }
    const strip = Math.min(from.height, SEND_STRIP_PX);
    const scale = Math.max(0.12, Math.min(0.5, to.width / from.width));
    const dx = to.left + to.width / 2 - (from.left + (from.width * scale) / 2);
    const dy = to.top + to.height / 2 - (from.top + (strip * scale) / 2);
    const cut = `inset(0px 0px ${from.height - strip}px 0px round 12px)`;
    // Each leg eases on its own: a lift, the glide to Busy that slows as it lands, and a short fade there.
    sheet.animate([
      { transform: "translate(0, 0) scale(1)", clipPath: "inset(0px 0px 0px 0px round 12px)", opacity: 1, easing: EASE_OUT },
      { transform: "translate(0, -10px) scale(0.98)", clipPath: cut, opacity: 1, offset: 0.2, easing: SEND_GLIDE },
      { transform: `translate(${dx}px, ${dy}px) scale(${scale})`, clipPath: cut, opacity: 1, offset: 0.8, easing: "ease-in" },
      { transform: `translate(${dx}px, ${dy}px) scale(${scale})`, clipPath: cut, opacity: 0 },
    ], { duration: SEND_AWAY_MS, fill: "both" }).finished.catch(() => {}).then(done);
  };
}
const SEND_AWAY_MS = 520;
const SEND_GLIDE = "cubic-bezier(0.5, 0, 0.2, 1)";
// The height of the card's top that lands on Busy: its title and ask.
const SEND_STRIP_PX = 140;

// Where an element of the list on the left stands once motion.js's glide (a `translate` from where it was) ends: the
// redraw a Send brings moves Busy up by the row that left Inbox.
function settledRect(el) {
  const rect = el.getBoundingClientRect();
  const [x = 0, y = 0] = (getComputedStyle(el).translate || "").split(" ").map((part) => parseFloat(part) || 0);
  return new DOMRect(rect.left - x, rect.top - y, rect.width, rect.height);
}

function flyToAgents(step, from) {
  const group = document.querySelector("#work .group.busy");
  if (!group || !from || !from.width) return;
  const count = group.querySelector(".group-count");
  const bump = () => { if (count) count.animate([{ scale: "1" }, { scale: "1.15" }, { scale: "1" }], { duration: 180, easing: EASE_OUT }); };
  if (REDUCED.matches) { bump(); return; }
  const row = group.querySelector(`li[data-step="${step.id}"]`);
  const target = row && row.getBoundingClientRect().height ? row : group.querySelector(":scope > summary");
  const to = target.getBoundingClientRect();
  const ghost = h("div", { class: "flight", "aria-hidden": "true" }, icon("handoff"), h("span", { text: plainText(step.ask_html) }));
  document.body.append(ghost);
  const startX = from.left + Math.min(24, from.width / 4);
  const startY = from.top + Math.min(20, from.height / 4);
  ghost.style.left = `${startX}px`;
  ghost.style.top = `${startY}px`;
  ghost.style.maxWidth = `${Math.max(160, Math.min(360, from.width - 48))}px`;
  // A target below the screen: the copy flies to the screen's bottom edge, the way to it.
  const endY = Math.min(Math.max(to.top, 8), window.innerHeight - 48);
  const flight = ghost.animate([
    { translate: "0 0", scale: "1", opacity: 0 },
    { translate: "0 0", scale: "1", opacity: 1, offset: 0.12 },
    { translate: `${to.left - startX}px ${endY - startY}px`, scale: ".85", opacity: 0.9, offset: 0.88 },
    { translate: `${to.left - startX}px ${endY - startY}px`, scale: ".8", opacity: 0 },
  ], { duration: FLIGHT_MS, easing: EASE_OUT });
  flight.finished.catch(() => {}).then(() => { ghost.remove(); bump(); });
}

// Writing feedback is timed apart from the steps (TRACK-2). From the first word or the pick until Send
// or closing the box, the step whose timer ran waits: its timer pauses and shows greyed, and the step
// stays uncovered. The header ticks the feedback's own time. A reload forgets the hold; the step then
// stays paused like any other.
function holdForFeedback() {
  if (ui.feedbackHold) return;
  const ran = ui.state && drawn(ui.state).timer;
  const stepId = ran ? ran.step_id : null;
  ui.feedbackHold = { since: Date.now(), stepId, itemId: ran ? ran.item_id : null };
  if (stepId !== null) sendTimer(stepId, false);
  render(false);
}

// Ends the hold: the waiting step's timer runs again; returns the seconds the user wrote.
function releaseFeedbackHold() {
  const hold = ui.feedbackHold;
  if (!hold) return 0;
  ui.feedbackHold = null;
  if (hold.stepId !== null) sendTimer(hold.stepId, true);
  render(false);
  return Math.round((Date.now() - hold.since) / 1000);
}

// Back to the waiting step while the feedback is still open: its timer runs again, and the feedback keeps
// counting until Send; the report gives the time both ran to the step.
function backToStep(stepId) {
  ui.feedbackHold.stepId = null;
  sendTimer(stepId, true);
  render(false);
}

// The page leaves the step that waits for the feedback: it shows another item (`itemId`), or the phone's list hides
// every card (null). The step then stays paused when the feedback ends, as a step left while its timer ran does
// (TRACK-8).
function leaveFeedbackHoldStep(itemId) {
  if (ui.feedbackHold && ui.feedbackHold.itemId !== itemId) ui.feedbackHold.stepId = null;
}

function heldForFeedback(step) {
  return Boolean(ui.feedbackHold && ui.feedbackHold.stepId === step.id);
}

// Feedback leaves its box at once; if the server refuses, the words go back and
// `reopen` (or null) shows the box again.
function sendFeedback(key, body, reopen) {
  const sent = ui.drafts.get(key) || "";
  const text = sent.trim();
  if (!text) { toast("Write the feedback first", true); return null; }
  const seconds = releaseFeedbackHold();
  takeDraft(key);
  ui.expanded.delete(key);
  render(false);
  toast("Feedback saved; a sweep reads it within minutes");
  return run(`${key}:${++ui.sendCount}`, async () => {
    try {
      await apiWritten("POST", "/api/feedback", { text, seconds, ...body }, { key, text: sent });
    } catch (error) {
      // The box of another part, picked since, or kept words of one closed, keeps its own words and part; these
      // wait for it (`waitForPickBox`). Signed out, the page goes to sign-in and no part stays picked, so the words
      // go back into the stored box.
      const notice = `Feedback not saved: ${error.message}`;
      if (key === PICK_KEY && error.status !== 401 && pickBoxTaken()) throw waitForPickBox(sent, reopen, notice, error);
      restoreDraft(key, sent);
      if (reopen) reopen();
      throw new Error(notice, { cause: error });
    }
  });
}

// Feedback mode (FEEDBACK-3): the user points at any part of the board and says
// what should change. The feedback names where it was (`page:<section>/step N`)
// and quotes what the part said, so the reader knows what the user meant after the
// page changes. The pick box sits outside the redrawn sections, so a poll never
// replaces what they are typing.

// What a pick lands on: the nearest of these around the element under the pointer (pick.js finds it). A line
// of a step's history is a part of its own, so a session's reply there keeps that session (FEEDBACK-8). The
// Add box and the header are parts too, so Wider from the Add box's text steps out to the box, then the
// header, before the whole page.
const PICKABLE = "input, textarea, select, button, a, summary, iframe, .answers, .step-keys, .actions, .chip, .count, .rank, .row-age, h1, h2, h3, p, li, .hist-line, .hint, .row, .card, .side-row, .draft, details, section, .counts, .usage, .sweep-status, .add-inner, #header, .banner, .banner-area, aside, .rail-top, .rail-search, .rail-foot, .key-tray, [data-tip], [data-time]";
const PICK_KEY = "fb:pick";
let hovered = null;

// The hover mark draws a box around the part under the pointer, labelled with the place the feedback would
// name (pickPlace), so the user sees what a click picks before clicking. The chosen part has its own mark.
function markHover(el) {
  const part = el === ui.pick?.el ? null : el;
  if (part === hovered) return;
  if (hovered) hovered.classList.remove("pick-hover");
  hovered = part;
  if (part) part.classList.add("pick-hover");
  Pick.mark("hover", part, part ? pickPlace(part).target.replace(/^page:/, "") : null);
}

// `purpose`: "feedback" opens the feedback box on the part; "add" attaches it to the to-do in the Add box.
function startPicking(purpose) {
  ui.picking = purpose;
  document.body.classList.add("picking");
  document.getElementById("feedback-mode").classList.toggle("on", purpose === "feedback" || ui.addPartFirst);
  const bar = document.getElementById("pick-bar");
  bar.classList.toggle("for-add", purpose === "add");
  renderPickBar();
  bar.hidden = false;
  if (purpose === "feedback") holdForFeedback();
}

// Once a part is picked, the bar says that a click on the same spot tries the next guess there (SPEC FEEDBACK-7).
function renderPickBar() {
  const purpose = ui.picking;
  const tap = TOUCH.matches ? "Tap" : "Click";
  const words = purpose === "add" ? (ui.addPartFirst ? `${tap} the part your to-do is about` : `${tap} the part to attach to your to-do`)
    : ui.pick ? `${tap} it again for another guess`
      : `${tap} the part your feedback is about`;
  document.getElementById("pick-bar").replaceChildren(
    icon("pick"),
    h("span", { text: words }),
    purpose === "feedback" && h("button", {
      class: "button small ghost whole-page", type: "button", title: "Whole page",
      onclick: () => choosePick(document.body, document.body, null),
    }, icon("wider"), h("span", { class: "label", text: "Whole page" })),
    h("button", { class: "button small ghost", type: "button", onclick: stopPicking }, TOUCH.matches ? "Stop" : "Stop (Esc)"));
}

function stopPicking() {
  const purpose = ui.picking;
  ui.picking = null;
  document.body.classList.remove("picking");
  document.getElementById("feedback-mode").classList.remove("on");
  document.getElementById("pick-bar").hidden = true;
  markHover(null);
  // Feedback's pick stopped before a part was picked: there is no to-do to write, so the Add box stays closed.
  // From the keyboard, the focus goes back to Feedback.
  if (purpose === "add" && ui.addPartFirst) {
    closeAddBox();
    if (!ui.addByPointer && !document.activeElement?.offsetParent) document.getElementById("feedback-mode").focus();
  } else if (purpose === "add") renderAddBox();
  // Leaving feedback mode without a part ends the feedback's time.
  if (purpose === "feedback" && !ui.pick) releaseFeedbackHold();
}

// Where a part is: the target the feedback names, and the step, item and session around it.
function pickPlace(el) {
  if (el === document.body) return { target: "page:whole page", step: null, item: null, session: null };
  const area = el.closest("[id]");
  const step = el.closest("[data-step]");
  const item = el.closest("[data-item]");
  const session = el.closest("[data-session]");
  // A part of a page framed in Cockpit's, such as Usage in the center layer, starts with that page's path.
  const framed = Pick.framedPath(el);
  const parts = framed ? [framed, area ? area.id : "page"] : [area ? area.id : "page"];
  if (step) parts.push(`step ${step.dataset.step}`);
  else if (item) parts.push(`item ${item.dataset.item}`);
  if (session) parts.push(`session ${session.dataset.session}`);
  const kind = [...el.classList].find((name) => !name.startsWith("pick-")) || el.tagName.toLowerCase();
  if (![area, step, item, session].includes(el)) parts.push(kind);
  return { target: `page:${parts.join("/")}`, step, item, session };
}

// A pick: its place, the words it shows, and `detail`, how a session finds the part again (pick.js).
function pickTarget(el) {
  if (el === document.body) {
    return { el, target: "page:whole page", quote: "", words: "", detail: Pick.describe(el), step_id: null, item_id: null, session_id: null, page_id: null };
  }
  const { target, step, item, session } = pickPlace(el);
  const text = Pick.words(el).replace(/\s+/g, " ").trim();
  // A link to a page shared on an item (`/pages/N`): marked as a writing example, the page itself is kept.
  const page = el.closest("a[href]");
  const pageId = page && new URL(page.href).pathname.match(/^\/pages\/(\d+)$/);
  return {
    el,
    target,
    quote: clip(text, 2000),
    // The whole passage, for a writing example (the server takes up to 20,000 characters).
    words: clip(text, 20000),
    step_id: step ? Number(step.dataset.step) : null,
    item_id: !step && item ? Number(item.dataset.item) : null,
    session_id: session ? session.dataset.session : null,
    page_id: pageId ? Number(pageId[1]) : null,
    detail: Pick.describe(el),
  };
}

// `from`: the part the user clicked, or last went Sideways to. `side`: the parts Sideways goes through, or null
// (pick.js step). Feedback mode stays on until the user writes, so a click on the same spot tries the next guess there.
function choosePick(el, from, side) {
  if (ui.pick) ui.pick.el.classList.remove("pick-chosen");
  ui.pick = { ...pickTarget(el), from, side };
  el.classList.add("pick-chosen");
  markHover(null);
  if (ui.picking === "feedback") renderPickBar();
  renderPick();
  document.querySelector(`[data-key="${PICK_KEY}"]`).focus();
}

// The picker's own controls, which no pick or step lands on: the pick bar and box, and the Add box while a part
// is picked for it. Feedback on the Add box itself can pick inside it.
const FEEDBACK_CONTROLS = "#pick-bar, #pick-box";
const ADD_CONTROLS = "#pick-bar, #pick-box, #add-box";

// Wider, Narrower and Sideways go by what the user sees (pick.js step, SPEC FEEDBACK-7); Wider ends at the whole
// page (FEEDBACK-4). They are icons with their words on hover (style.css shows the words on touch screens).
function widerNarrower(part, small) {
  const size = small ? "button small ghost" : "button ghost";
  const controls = small ? ADD_CONTROLS : FEEDBACK_CONTROLS;
  const button = (name, title) => h("button", { class: size, type: "button", title, disabled: !Pick.step(name, part, PICKABLE, controls),
    onclick: () => (small ? stepAddPart(Pick.step(name, ui.addPart, PICKABLE, controls)) : stepPick(Pick.step(name, ui.pick, PICKABLE, controls))) },
  icon(name), h("span", { class: "label", text: title }));
  return [button("wider", "Wider"), button("narrower", "Narrower"), button("sideways", "Sideways")];
}

// A step goes from the part the page now shows in the pick's place (Pick.now), and is null when there is none. `next` is
// {part, from, side} or null. The part comes into view when the step went to one mostly out of it.
function stepPick(next) {
  if (next && Pick.alive(next.part)) { Pick.reveal(next.part); choosePick(next.part, next.from, next.side); return; }
  toast("The page redrew since you picked; pick the part again", true);
  startPicking("feedback");
}

// The draft stays when the box closes, so a comment survives picking another part; its time ends.
function closePick() {
  if (ui.pick) ui.pick.el.classList.remove("pick-chosen");
  ui.pick = null;
  releaseFeedbackHold();
  renderPick();
  reopenWaitingPick();
}

// Refused words whose part's box gave way to another part's: each waits with its words and `reopen` (or null, for
// words from a page the user left, which name no part), and its box comes back once the pick box is closed with
// no words left in it.
const waitingPicks = [];

// Whether the pick box is open, or closed with words kept for the next part: words put back now would join them.
function pickBoxTaken() {
  return Boolean(ui.pick || (ui.drafts.get(PICK_KEY) || "").trim());
}

// Returns the error the refusal's notice shows; `notice` says what was not saved.
function waitForPickBox(sent, reopen, notice, cause) {
  waitingPicks.push({ sent, reopen, notice });
  return new Error(`${notice}. Your words wait, and come back on their part once this box is sent or closed empty`, { cause });
}

// With the pick box closed and empty, the oldest refused feedback that waits gets its box back on its own part.
function reopenWaitingPick() {
  if (pickBoxTaken() || !waitingPicks.length) return;
  const { sent, reopen, notice } = waitingPicks.shift();
  if (sent.trim()) restoreDraft(PICK_KEY, sent);
  if (reopen) reopen(); else ui.expanded.add(PICK_KEY);
  toast(`${notice}; it is back in its box`, true);
}

// Leaving the page (to sign in after any refused request, a reload, a closed tab): the stored box takes the waiting
// words ahead of its own, so they wait there on the next load, where no part stays picked. The page itself keeps
// them waiting: come back from the browser's back-forward cache, it is the same page, and storage goes back to
// what the page holds, unless another tab wrote the drafts since (`leftDrafts`, what this page wrote).
let leftDrafts = null;
addEventListener("pagehide", () => {
  if (!waitingPicks.length) return;
  const words = [...waitingPicks.map(({ sent }) => sent.trim()), (ui.drafts.get(PICK_KEY) || "").trim()].filter(Boolean);
  const drafts = [...new Map([...ui.drafts, [PICK_KEY, words.join("\n\n")]])].filter(([, text]) => text);
  writeStored(DRAFTS_KEY, drafts);
  leftDrafts = JSON.stringify(drafts);
});
addEventListener("pageshow", (event) => {
  let unchanged = false;
  try { unchanged = leftDrafts !== null && localStorage.getItem(DRAFTS_KEY) === leftDrafts; } catch { /* storage blocked */ }
  if (event.persisted && unchanged) saveDrafts();
  leftDrafts = null;
});

// Opens the pick box again on `pick`, the part the refused words were about.
function reopenPick(pick) {
  ui.pick = pick;
  if (pick.el.isConnected) pick.el.classList.add("pick-chosen");
  renderPick();
}

// The box closes at once; if the server refuses, it opens again with the words and the part.
function sendPick() {
  const pick = ui.pick;
  const { target, quote, detail, step_id, item_id } = pick;
  const sending = sendFeedback(PICK_KEY, { target, quote, detail, step_id, item_id }, () => reopenPick(pick));
  if (sending) {
    pick.el.classList.remove("pick-chosen");
    ui.pick = null;
    renderPick();
    reopenWaitingPick();
  }
}

// Good writing and Hard to understand (SPEC FEEDBACK-8): the picked part is kept as an example of writing to
// reproduce (`verdict` good) or to avoid (bad), and the box's words, if any, are the note on why. A whole step
// card keeps the step, a link to a shared page keeps the page, and any other part keeps the words the user saw.
function markPickWriting(verdict) {
  const pick = ui.pick;
  const kind = pick.el.matches("[data-step]") ? "step" : pick.page_id !== null ? "page" : "part";
  const sent = ui.drafts.get(PICK_KEY) || "";
  takeDraft(PICK_KEY);
  releaseFeedbackHold();
  pick.el.classList.remove("pick-chosen");
  ui.pick = null;
  renderPick();
  if (ui.picking === "feedback") stopPicking();
  reopenWaitingPick();
  markWriting({
    verdict, kind, target: kind === "part" ? pick.target : "", quote: kind === "part" ? pick.words : "", note: sent.trim(),
    step_id: pick.step_id, item_id: pick.item_id, page_id: pick.page_id, session_id: pick.session_id,
  }, sent.trim() ? { key: PICK_KEY, text: sent } : null,
  (error) => {
    // The box of another part, picked since, or kept words of one closed, keeps its own words and part; these wait
    // for it. Signed out, the page goes to sign-in and no part stays picked, so the words go back into the stored box.
    if (error.status !== 401 && pickBoxTaken()) {
      waitingPicks.push({ sent, reopen: () => reopenPick(pick), notice: `Not saved as ${WRITING_WORDS[verdict]}: ${error.message}` });
      return ". Your words wait, and come back on their part once this box is sent or closed empty";
    }
    restoreDraft(PICK_KEY, sent);
    reopenPick(pick);
    return "";
  });
}

// What a writing example is called by its verdict, in toasts and the menus.
const WRITING_WORDS = { good: "good writing", bad: "hard to understand" };

// `words`: the note's {key, text} when the user wrote one, or null. `reopen` (or null) takes the error, shows the
// box again with the words when the server refuses, and returns what the notice adds about them.
function markWriting(body, words, reopen) {
  const name = WRITING_WORDS[body.verdict];
  return run(`writing:${++ui.sendCount}`, async () => {
    const send = () => (words ? apiWritten("POST", "/api/writing", body, words) : api("POST", "/api/writing", body));
    try {
      await (body.kind === "step" ? inStepWritingOrder(body.step_id, send) : send());
    } catch (error) {
      const kept = reopen ? reopen(error) : "";
      throw new Error(`Not saved as ${name}: ${error.message}${kept}`, { cause: error });
    }
    toast(`Saved as ${name}`, false, { label: "See all", run: () => { location.href = "/writing"; } });
  });
}

// Good writing or Hard to understand pressed on a step card (FEEDBACK-8): the key shows pressed at once, settles once
// Cockpit keeps the mark (the step's `writing_verdict` in the next state), and comes back up with a short error when it
// refuses. A mark replaces the one the step's text had. The other key pressed while the first is on its way shows at
// once too, and is sent once the first has its answer. The settled key is the whole confirmation: no notice, since
// pressing again changes the mark.
function markStepWriting(step, verdict) {
  const name = WRITING_WORDS[verdict];
  const body = { verdict, kind: "step", target: "", quote: "", note: "", step_id: step.id, item_id: null, page_id: null, session_id: null };
  return run(`writing:step:${step.id}:${++ui.sendCount}`, async () => {
    try {
      await optimistic((state) => withStepFields(state, step.id, { writing_verdict: verdict, writing_pending: true }),
        () => inStepWritingOrder(step.id, () => api("POST", "/api/writing", body)));
    } catch (error) {
      throw new Error(`Not saved as ${name}: ${error.message}`, { cause: error });
    }
  });
}

// The pressed key pressed again clears the step's mark: the key comes up at once, in press order with
// the step's other marks, and shows pressed again with a short error when Cockpit refuses.
function clearStepWriting(step) {
  return run(`writing:step:${step.id}:${++ui.sendCount}`, async () => {
    try {
      await optimistic((state) => withStepFields(state, step.id, { writing_verdict: null, writing_pending: false }),
        () => inStepWritingOrder(step.id, () => api("DELETE", `/api/steps/${step.id}/writing`)));
    } catch (error) {
      throw new Error(`Mark not cleared: ${error.message}`, { cause: error });
    }
  });
}

// Whole-step marks on one step, from its card or the pick box, go to Cockpit one at a time in press order, so the last
// press is the one kept (FEEDBACK-8). `send` starts once the step's mark before it has its answer; held back meanwhile,
// the mark is not yet kept in the browser, so leaving the page asks first.
async function inStepWritingOrder(stepId, send) {
  const before = ui.writingSent.get(stepId);
  let answered;
  const sent = new Promise((resolve) => { answered = resolve; });
  ui.writingSent.set(stepId, sent);
  if (before) holdLeave(sent);
  try {
    await before;
    return await send();
  } finally {
    answered();
    if (ui.writingSent.get(stepId) === sent) ui.writingSent.delete(stepId);
  }
}

function renderPick() {
  const box = document.getElementById("pick-box");
  const pick = ui.pick;
  if (!pick) {
    box.hidden = true;
    box.replaceChildren();
    Pick.mark("chosen", null, null);
    Pick.dock(box, null, 0);
    return;
  }
  const area = draftArea(PICK_KEY, "What should change here?", 5, "");
  area.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); sendPick(); }
  });
  area.addEventListener("input", () => { if (ui.picking === "feedback") stopPicking(); });
  // On the whole page the head says so and nothing is wider; Narrower stays while it leads back toward
  // a part picked inside.
  const whole = pick.el === document.body;
  const steps = widerNarrower(pick, false);
  box.replaceChildren(...[
    h("div", { class: "pick-head" }, h("span", { class: "kind-chip note" }, icon("feedback"), h("span", { text: "Feedback" })),
      h("strong", { text: "on" }), h("span", { class: "meta", text: pick.target.replace(/^page:/, ""), title: pickTitle(pick) }),
      iconButton("x", "Close", { class: "button ghost small close", onclick: closePick })),
    pickPreview(pick, Math.min(688, window.innerWidth - 64)),
    area,
    h("div", { class: "actions" },
      whole ? !steps[1].disabled && steps[1] : steps,
      h("button", { class: "button ghost", type: "button", title: "Pick again", onclick: () => startPicking("feedback") },
        icon("pick"), h("span", { class: "label", text: "Pick again" })),
      h("button", { class: "button primary send", type: "button", onclick: sendPick }, icon("send"), "Send")),
    !whole && h("div", { class: "pick-writing" },
      h("span", { class: "meta", text: "Keep as an example of" }),
      h("button", { class: "button small ghost", type: "button", onclick: () => markPickWriting("good") },
      icon("writing-good"), "Good writing"),
      h("button", { class: "button small ghost", type: "button", onclick: () => markPickWriting("bad"),
        title: "Keep as an example of writing to avoid" },
      icon("writing-bad"), "Hard to understand")),
  ].filter(Boolean));
  box.hidden = false;
  // The chosen part keeps its mark while the user writes, and the box stands beside it, the page not darkened.
  Pick.mark("chosen", pick.el, pick.target.replace(/^page:/, ""));
  Pick.dock(box, pick.el, pickTop());
}

// What a hover over the pick's name or copy shows: everything the feedback sends to find the part.
function pickTitle(pick) {
  return `${pick.target}\n${pick.detail}`;
}

// The highest the pick box may stand: under the header, and under the pick bar while it shows.
function pickTop() {
  const bar = document.getElementById("pick-bar");
  const above = bar.hidden ? document.getElementById("header") : bar;
  return Math.max(16, above.getBoundingClientRect().bottom + 8);
}

// The picked part as the user sees it, scaled into the box where it fits (pick.js), with its words under a
// copy too small to read; else its words.
function pickPreview(pick, width) {
  const copy = Pick.copy(pick.el, width, 180);
  if (copy) {
    // Set through the style object: the page's content policy drops style attributes.
    const preview = h("div", { class: "pick-preview", title: pickTitle(pick) }, copy);
    preview.style.setProperty("--ground", Pick.ground(pick.el));
    if (!copy.classList.contains("small") || !pick.quote) return preview;
    return h("div", { class: "pick-shown" }, preview, h("blockquote", { class: "pick-quote", text: clip(pick.quote, 300) }));
  }
  return pick.quote ? h("blockquote", { class: "pick-quote", text: clip(pick.quote, 300) }) : null;
}

// Update shows "starting…" at once; the redraw after the request takes it off again.
function fullSweep() {
  const started = run("update", () => api("POST", "/api/sweeps/run"));
  if (ui.state) renderSweep(ui.state);
  ui.pendingRender = true;
  return started;
}

// Whether Update is out of reach: it is starting, or a sweep or check runs.
function updateBlocked(state) {
  return ui.busy.has("update") || Boolean(state && (state.sweep_running || state.checking));
}

// A press on Update says at once what it started, and again once that is over (SPEC SWEEP-6): `ui.updateRun`
// holds the press, with the message that launched it, until the server's `sweep_launch` for that message ends.
function update() {
  if (updateBlocked(ui.state)) return Promise.resolve(false);
  const top = ui.state ? ui.state.queue.slice(0, ui.state.check_top) : [];
  const press = { sweep: top.length === 0, messageId: null };
  ui.updateRun = press;
  toast(press.sweep ? "Your queue is empty, so a full sweep started" : `Checking your top ${plural(top.length, "step", "steps")} against live state`);
  const started = run("update", async () => { press.messageId = (await api("POST", "/api/update")).id; });
  started.then((ok) => { if (!ok && ui.updateRun === press) ui.updateRun = null; });
  if (ui.state) renderSweep(ui.state);
  ui.pendingRender = true;
  return started;
}

// Says how the press's sweep or check ended, once the server has it as done, failed or timed out; what ran
// is the server's word (`kind`), as another tab may have changed the queue before the press arrived. A
// snapshot from before the launch (a refresh sent before the POST came back) is passed over. Launches are
// numbered in order and a new one starts only once the last is over, so a later one means this one ended.
function updateFinished(state) {
  const press = ui.updateRun;
  const launch = state.sweep_launch;
  if (!press || press.messageId === null || !launch || launch.message_id < press.messageId) return;
  if (launch.message_id === press.messageId && (launch.state === "starting" || launch.state === "running")) return;
  ui.updateRun = null;
  if (launch.message_id > press.messageId) {
    toast("What your Update started has ended; another sweep or check has started since");
    return;
  }
  const sweep = launch.kind !== "check";
  const what = launch.kind === "reorder" ? "The reorder" : sweep ? "The sweep" : "The check of your top steps";
  if (launch.state !== "done") {
    toast(`${what} ${launch.state === "failed" ? "failed" : "timed out"}: ${launch.detail}`, true);
    return;
  }
  if (sweep) {
    toast(`${what} finished`, false, null, (state.last_sweep && state.last_sweep.summary) || "");
    return;
  }
  toast(`Your top ${plural(launch.step_ids.length, "step is", "steps are")} checked against live state`);
}

// How long something under way has run: "under a minute", "4 min", "2 h".
function runningFor(iso) {
  const minutes = Math.floor(Math.max(0, Date.now() - new Date(iso).getTime()) / 60e3);
  return minutes < 1 ? "under a minute" : span(minutes);
}

// Toast

// A toast shows for TOAST_MS, and the bar along its foot shrinks over that time (UI-24). While the
// pointer rests on it or its button has focus, the time stops, so Undo stays in reach; it runs on
// with the time left once the pointer leaves.
// `action`: the toast's button, {label, run}, or null; its `gone`, when set, runs once the toast leaves
// without the button pressed: its time ran out or another toast took its place (MSG-8).
// `tip`: more words for the toast's tooltip, or "". Returns the toast's number, for `retoast` and `offerFollowUp`.
// While the shown toast's Add more box is open (MSG-10), a new toast waits: it shows once the box closes or the
// shown toast goes, and what `retoast` and `offerFollowUp` did to it meanwhile applies then. Of several waiting,
// the newest shows; each one it passes over leaves as if its time ran out.
// A new toast also waits while the user presses in the shown one, until the press ends (releasePress): shown at
// once, it would take the place of the button under the finger, such as Take back, and the press would do nothing.
// It waits at most until PRESS_HOLD_MS after the latest press went down, should no release come (a context menu can
// take it).
// A press held past PRESS_HOLD_MS holds no toast back. A toast that shows passes over any that still waits.
function toast(text, fail = false, action = null, tip = "") {
  const id = ++ui.toastCount;
  const el = document.getElementById("toast");
  const following = el.classList.contains("following");
  const pressedIn = pressHeld() && ui.pressedOn instanceof Node && el.contains(ui.pressedOn);
  const passed = ui.toastWaiting;
  const waits = !el.hidden && (following || pressedIn);
  if (waits) ui.toastWaiting = { id, text, fail, action, tip, setups: [], forPress: !following };
  else if (passed) ui.toastWaiting = null;
  if (waits && !following) armToastPressTimer();
  if (passed && ui.toastWaiting !== passed) {
    ui.toastAdded.delete(passed.id);
    if (passed.action && passed.action.gone) passed.action.gone();
  }
  if (!waits) showToast(id, text, fail, action, tip);
  return id;
}

function showToast(id, text, fail, action, tip) {
  const el = document.getElementById("toast");
  leaveToast();
  el.replaceChildren(...[fail && icon("alert"), h("span", { class: "toast-text", text, title: tip || null })].filter(Boolean));
  // The press reads what the toast holds (Take back, the Add more box's words) before a waiting toast takes its place.
  if (action) el.append(h("button", { type: "button", class: "toast-action", onclick: () => { ui.toastGone = null; closeToast(); action.run(); showWaitingToast(); } }, action.label));
  el.append(h("span", { class: "toast-time", "aria-hidden": "true" }));
  el.className = fail ? "toast fail" : "toast";
  el.hidden = false;
  ui.toastGone = (action && action.gone) || null;
  ui.toastShown = id;
  ui.toastLeft = TOAST_MS;
  runToast();
}

// The shown toast leaves without its button pressed: its `gone` runs, once.
function leaveToast() {
  ui.toastAdded.delete(ui.toastShown);
  const gone = ui.toastGone;
  ui.toastGone = null;
  if (gone) gone();
}

// Completes the words of a toast that still shows, in place: no second entrance, and it stays its full time.
function retoast(shown, text, tip) {
  const added = ui.toastAdded.get(shown);
  if (added) {
    text = withSentence(text, added.words);
    tip = [tip, added.tip].filter(Boolean).join("\n");
  }
  const waiting = ui.toastWaiting;
  if (waiting && waiting.id === shown) {
    Object.assign(waiting, { text, tip });
    return;
  }
  const el = document.getElementById("toast");
  if (shown !== ui.toastShown || el.hidden) return;
  const words = el.querySelector(".toast-text");
  words.textContent = text;
  words.title = tip;
  const bar = el.querySelector(".toast-time");
  bar.style.animation = "none";
  void bar.offsetWidth;
  bar.style.animation = "";
  ui.toastLeft = TOAST_MS;
  runToast();
}

// Adds a sentence to a toast that still shows or waits, keeping its time and its button; returns whether it did.
function addToToast(shown, words, tip) {
  if (!toastShows(shown)) return false;
  const before = ui.toastAdded.get(shown) || { words: "", tip: "" };
  ui.toastAdded.set(shown, { words: [before.words, words].filter(Boolean).join(" "), tip: [before.tip, tip].filter(Boolean).join("\n") });
  const waiting = ui.toastWaiting;
  const el = document.getElementById("toast");
  if (waiting && waiting.id === shown) {
    Object.assign(waiting, { text: withSentence(waiting.text, words), tip: [waiting.tip, tip].filter(Boolean).join("\n") });
    return true;
  }
  const text = el.querySelector(".toast-text");
  text.textContent = withSentence(text.textContent, words);
  // motion.js has moved the tooltip from `title` to `data-tip` by now.
  if (tip) text.title = [text.dataset.tip || text.title, tip].filter(Boolean).join("\n");
  return true;
}

// Whether toast number `shown` still shows, or waits to show.
function toastShows(shown) {
  if (shown === null || shown === undefined) return false;
  return ui.toastWaiting?.id === shown || (shown === ui.toastShown && !document.getElementById("toast").hidden);
}

// A toast's words with one more sentence after them.
function withSentence(text, words) {
  return `${text.replace(/\.$/, "")}. ${words}`;
}

// Runs the shown toast's time left, unless the pointer rests on it or its button has focus.
function runToast() {
  const el = document.getElementById("toast");
  clearTimeout(ui.toastTimer);
  ui.toastTimer = null;
  if (el.hidden || el.matches(":hover, :focus-within, .following")) return;
  ui.toastSince = performance.now();
  ui.toastTimer = setTimeout(hideToast, ui.toastLeft);
}

function holdToast() {
  if (ui.toastTimer === null) return;
  clearTimeout(ui.toastTimer);
  ui.toastTimer = null;
  ui.toastLeft = Math.max(0, ui.toastLeft - (performance.now() - ui.toastSince));
}

function hideToast() {
  closeToast();
  showWaitingToast();
}

function closeToast() {
  clearTimeout(ui.toastTimer);
  ui.toastTimer = null;
  document.getElementById("toast").hidden = true;
  leaveToast();
}

// A toast that waits for a press shows PRESS_HOLD_MS after the latest press went down, should no release come.
// A new press restarts the time (the pointerdown listener by `releasePress`).
function armToastPressTimer() {
  clearTimeout(ui.toastPressTimer);
  ui.toastPressTimer = setTimeout(() => { if (ui.toastWaiting?.forPress) showWaitingToast(); },
    Math.max(0, PRESS_HOLD_MS - (performance.now() - ui.pressedAt)));
}

// The toast that waited while an Add more box was open, with what was done to it meanwhile.
function showWaitingToast() {
  const waiting = ui.toastWaiting;
  ui.toastWaiting = null;
  if (!waiting) return;
  showToast(waiting.id, waiting.text, waiting.fail, waiting.action, waiting.tip);
  for (const setup of waiting.setups) setup();
}

// Pieces

function sessionMap(state) {
  return new Map(state.sessions.map((session) => [session.session_id, session]));
}

// The session the user's Done or Archive on a step acts on (SESSION-4): `label` names it, `others` counts
// the other open steps for it, `finished` says the session reports its work done. On its last open step
// the button is Archive, which archives the session; otherwise it is Done, and the session keeps running.
// Null for a step without a session on this machine, and for the sweep's own session, which is never
// archived this way. The step's target carries the session's name and
// state even when the session list leaves it out (finished over a day ago).
function doneSession(step, state) {
  const target = step.target;
  // Done on a result to read (need `read:…`, VIEW-16) closes only that step on the server, so it ends nothing.
  if (!target || target.state === "unknown" || /^read:/.test(step.need)) return null;
  if (target.is_sweep || target.standing) return null;
  const listed = sessionMap(state).get(target.session_id);
  const others = STEP_LISTS.flatMap((name) => state[name])
    .filter((other) => other.id !== step.id && other.target && other.target.session_id === target.session_id).length;
  const label = target.name ? `session "${target.name}"` : listed ? sessionLabel(listed, "") : "its session";
  return { label, others, finished: target.state === "done" };
}

// "step 1 of 2" beside the item's title when the item has other open steps, so steps of one task read as one
// (SESSION-4); the tooltip lists them. Null when the step is its item's only open step.
function stepOf(step, state) {
  const steps = STEP_LISTS.flatMap((name) => state[name]).filter((other) => other.item_id === step.item_id);
  const place = steps.findIndex((other) => other.id === step.id) + 1;
  // A card kept on screen after an agent closed its step (`left`) is no longer among them.
  if (steps.length < 2 || place === 0) return null;
  const lines = steps.map((other) => `${other.id === step.id ? "This step" : "Also"}: ${clip(plainText(other.ask_html), 60)}`);
  return h("span", {
    class: "step-of", title: lines.join("\n"),
    text: `step ${place} of ${steps.length}`,
  });
}

// The step is the last open one of its session, so closing it archives the session (SESSION-4).
function archivesSession(step, state) {
  const ending = doneSession(step, state);
  return ending !== null && ending.others === 0;
}

// What the server says Done or Archive does to the step's session, as a sentence; "" when it had none.
function sessionEndWords(end) {
  if (!end) return "";
  const label = end.name ? `session "${end.name}"` : "its session";
  if (end.how === "kept") return `${end.name ? `Session "${end.name}"` : "Its session"} keeps running for its ${plural(end.other_steps, "other open step", "other open steps")}.`;
  return `Cockpit tells ${label} and stops it after that turn. The sweep checks the archive and brings the session back when work is left.`;
}

// The repo a checkout path is in: its last part, with a Claude worktree folded into its repo.
function repoOf(cwd) {
  return (cwd || "").replace(/\/\.claude\/worktrees\/.*$/, "").split("/").filter(Boolean).pop() || "";
}

// A session's name (its repo and task). Without one: the repo it runs in, from the session or,
// when the state does not list it, from `cwd` ("" when that is unknown too).
function sessionLabel(session, cwd) {
  if (session && session.name) return session.name;
  const repo = repoOf(session ? session.cwd : cwd);
  return repo ? `a session in ${repo}` : "a session";
}

// A session's name as a span, led by the session's mark (UI-25): its dot, in the colour of its state.
// A session without a name keeps its id in the tooltip.
function sessionName(session, sessionId, cwd, cls) {
  return h("span", { class: `${cls} session-ref`, title: session && session.name ? null : `Session ${sessionId}` },
    sessionMark(session),
    h("span", { class: "session-ref-name", text: sessionLabel(session, cwd) }));
}

// The session's mark (UI-25): the bot in the session grey, then a dot in the colour of its state, a ring once its
// turn ended or it stopped; words beside it name the session, so screen readers skip the mark.
function sessionMark(session) {
  const state = (session && session.state && sessionState(session)) || (session && session.alive ? "idle" : "stopped");
  return h("span", { class: "session-mark", "aria-hidden": "true" }, sessionBot(), h("span", { class: `dot ${state}` }));
}

// The bot in the session grey (UI-25): a session wherever one shows, and on a button that starts a new one.
function sessionBot() {
  const bot = Icons.icon("bot");
  bot.classList.add("session-bot");
  return bot;
}

// Where a press on a step with no session starts one (SPEC NEXT-18): where the step's reply box would, the project's
// checkout with the item's picks from Add, else Cockpit's defaults (`composer`).
function newSessionWhere(step) {
  const item = ui.state.items.find((each) => each.id === step.item_id);
  const picks = startPicks(`msg:step:${step.id}`, (item && item.start_options) || null);
  return {
    new_cwd: defaultCheckout(item ? item.project : null, ui.state) || null, ...picks,
    provider: picks.provider || ui.state.default_provider || "claude",
  };
}

// What Run and Allow send about the session: the one the page shows, or with none where a new one starts.
function pressTarget(step) {
  return step.target
    ? { session_id: step.target.session_id, new_cwd: null, provider: null, model: null, effort: null, account: null,
        speed: null }
    : { session_id: null, ...newSessionWhere(step) };
}

// A link that opens a session in iTerm2 on the user's Mac (SESSION-6). iTerm2 3.5 or later runs the command
// in an `iterm2:/command?c=` link after a dialog that shows it, where the user picks a new window or tab. The
// command is the setup file's `attach` with the session's id in place of {session} (SCOPE-5); `cockpit attach`
// shows the session in the user's tmux, in the window it opened for that session before or a new one, and a
// stopped session resumes. A touch screen has no iTerm2, so the page leaves the link out there, and so does a
// session Cockpit took off `claude agents` (removals.py): a message resumes it. The click's toast says the two
// confirmations still to come, and its tooltip holds the command. On the page with the list of items (VIEW-26) the
// key shows the session's conversation in the page instead; that view keeps the iTerm2 link.
function itermLink(session, props, ...children) {
  if (SIDEBAR && session.session_id) return sessionViewButton(session, props, ...children);
  return itermAnchor(session, props, ...children);
}

function itermAnchor(session, props, ...children) {
  if (session.provider === "codex" || session.removed_at) return null;
  if (TOUCH.matches || !session.id) return null;
  const command = ui.state.attach.replaceAll("{session}", session.id);
  return h("a", {
    ...props, href: `iterm2:/command?c=${encodeURIComponent(command)}`,
    title: "Open in iTerm2",
    onclick: () => toast("Opening iTerm2: allow it in Chrome, then confirm the command in iTerm2"),
  }, ...children);
}

function actorWords(actor) {
  if (actor === "user") return "you";
  return (actor || "").replace(/^agent:/, "");
}

function isNew(step) {
  return Boolean(ui.seenBefore) && (step.queued_at || step.created_at) > ui.seenBefore;
}

function stageChip(stage) {
  const words = STAGE_LABELS[stage] || stage;
  return stage ? h("span", { class: `chip stage stage-${stage}`, text: words }) : null;
}

// Whom an item waits on, in words: a person or bot by name, an agent by its session's name.
function waitingWho(entry, sessions) {
  return entry.kind === "agent" ? sessionLabel(sessions.get(entry.who), "") : entry.who;
}

// An item in review names its review (STATE-1): "your review" when a step of it that asks the user to act
// waits in the user's queue (the item's ball is the user's; a result to read leaves it with the others), else by the first entry of `waiting_on`: a bot ("review-bot review"), a person ("review · Alex"),
// an agent ("agent review"). Stage words are lower case like the other stages; names keep theirs.
// The tooltip lists everyone it waits on.
function reviewChip(item, sessions) {
  const names = item.waiting_on.map((entry) => waitingWho(entry, sessions));
  const tip = names.length ? `Waiting on ${names.length > 1 ? `${names.slice(0, -1).join(", ")} and ${names.at(-1)}` : names[0]}` : "No one named: the next sweep fills this in";
  const first = item.waiting_on[0];
  let text;
  let cls = "chip stage stage-review";
  if (item.asks_user) { text = "your review"; cls += " yours"; }
  else if (!first) { text = "review · no one named"; cls += " unnamed"; }
  else if (first.kind === "bot") text = `${first.who} review`;
  else if (first.kind === "agent") text = "agent review";
  else text = `review · ${first.who}`;
  if (names.length > 1 && text !== "your review") text += ` +${names.length - 1}`;
  return h("span", { class: cls, title: tip, text });
}

// The stage chip of an item: a review names its review, the other stages their stage.
function itemStageChip(item, sessions) {
  return item.stage === "review" ? reviewChip(item, sessions) : stageChip(item.stage);
}

// A to-do added with Fire and forget (CONTROL-24) says so, since its session asks only what blocks it. A plain
// to-do carries no tag of its own (CONTROL-16).
function fireAndForgetChip(item) {
  if (!item.todo || !item.fire_and_forget) return null;
  return h("span", { class: "chip todo", title: "Fire and forget: back only if it needs you" },
    icon("send"), h("span", { class: "chip-words", text: "Fire and forget" }));
}

// Cockpit's own work carries the Cockpit tag (SPEC UI-43), drawn by icons.js for every page.
const cockpitChip = Icons.cockpitTag;

function blocksChip(item) {
  return item.blocks ? h("span", { class: "chip blocks", title: `Blocks: ${item.blocks}` }, icon("blocks"), h("span", { class: "chip-words", text: clip(item.blocks, 28) })) : null;
}

// Agent work that stopped (SPEC SWEEP-20): none of the item's sessions has worked for half an hour. On an urgent
// or high item it is red, as on its lamp, so work that matters and stopped stands out from work that runs.
const raisedPriority = (item) => item.priority === "urgent" || item.priority === "high";

function stalledChip(item) {
  if (!item.stalled_since) return null;
  return h("span", {
    class: `chip stalled${raisedPriority(item) ? " raised" : ""}`,
    title: `Stalled: no session has worked since ${when(new Date(item.stalled_since))}`,
  }, icon("alert"), h("span", { class: "chip-words", text: `Stalled ${age(item.stalled_since)}` }));
}

// Whom an item outside review waits on besides agents (the session dots show those): a clock and the name.
function waitingChips(item) {
  return item.waiting_on.filter((entry) => entry.kind !== "agent").map((entry) =>
    h("span", { class: "chip waiting" }, icon("clock"), entry.who));
}

function clip(text, length) {
  return text.length > length ? `${text.slice(0, length - 1)}…` : text;
}

// The parts of a line with a "·" between each two.
function dotted(parts) {
  return parts.filter(Boolean).flatMap((part, index) => index ? [h("span", { class: "sep", text: "·" }), part] : [part]);
}

// Markdown links as their words, for one-line previews and hover texts: "[words](url)", also with brackets in the
// url or a "title" after it.
function linkWords(text) {
  return text.replace(/\[([^\]]+)\]\((?:[^()\s]|\([^()\s]*\))+(?:\s+"[^"]*")?\)/g, "$1");
}

function stateDot(state, title) {
  return h("span", { class: `dot ${state || "unknown"}`, title: title || state || "unknown" });
}

// A session's state for its dot: a working session that waits for the user's approval (tempo "blocked")
// is "blocked"; one whose turn ended, while a background task or a timer can wake it, is "idle";
// only one doing work now is "working".
function sessionState(session) {
  if (session.state !== "working" || session.working_now) return session.state;
  return session.tempo === "blocked" ? "blocked" : "idle";
}

// A pull request on GitHub as `owner/repo#number`, as `state.pull_states` keys it (github.py's PULL_URL), or null.
const PULL_URL = /^https:\/\/github\.com\/([\w.-]+\/[\w.-]+)\/pull\/([1-9]\d*)(?:[/?#]|$)/;
function pullRef(url) {
  const match = PULL_URL.exec(url);
  return match ? `${match[1]}#${match[2]}` : null;
}

// A pull request's state, "open", "draft", "merged" or "closed", or null while the server has not asked GitHub yet
// (NEXT-66).
function pullState(ref) {
  return (ui.state && ui.state.pull_states[ref]) || null;
}

// A link's source, as the icon before its words; a pull request's icon draws its state.
function linkIcon(url) {
  if (url.startsWith(`${location.origin}/pages/`) || url.startsWith(`${location.origin}/pages?`)) return "doc";
  const ref = pullRef(url);
  if (ref) return pullState(ref) ? `pr-${pullState(ref)}` : "pr";
  const host = (() => { try { return new URL(url).hostname; } catch { return ""; } })();
  if (host.endsWith("slack.com")) return "slack";
  if (host === "github.com") return "github";
  if (host === "linear.app") return "linear";
  if (host === "docs.google.com") return "doc";
  return "link";
}

const LINKS_TOP = 4;

// An item's links with the HTML pages sessions shared on it first (API-3), each page once: an item link to
// one of its pages leaves; `item` may be null. `step`: the step the links show on, or null. On a step that takes
// the user's answer, a page opens in the Pages view for that step, where comments on the page go with the answer
// (NEXT-38).
function itemLinks(item, step) {
  if (!item) return [];
  const own = (page) => `${location.origin}/pages/${page.id}`;
  const pages = (item.pages || []).map((page) => ({ label: page.title, url: step && takesComments(step) ? pageForStep(page.id, step, false) : own(page), page: true }));
  const pageUrls = new Set((item.pages || []).map(own));
  return [...pages, ...item.links.filter((link) => !pageUrls.has(link.url))];
}

// The id of the shared page a link opens on this Cockpit, as `/pages/ID` or the Pages view's `/pages?id=ID…`, or null.
function sharedPageId(url) {
  const address = (() => { try { return new URL(url, location.href); } catch { return null; } })();
  if (!address || address.origin !== location.origin) return null;
  const id = /^\/pages\/(\d+)$/.exec(address.pathname)?.[1] || (address.pathname === "/pages" ? address.searchParams.get("id") : null);
  return id && /^\d+$/.test(id) ? Number(id) : null;
}

// What makes two links the same link: a shared page's id, else the address.
function linkKey(url) {
  const pageId = sharedPageId(url);
  return pageId === null ? url : `page:${pageId}`;
}

const takesComments = (step) => !step.handoff && !step.left;
// `comment`: the page opens with its comment column, as for the "Comment on this page" link.
const pageForStep = (pageId, step, comment) => `${location.origin}/pages?id=${pageId}&step=${step.id}${comment ? "&comment=1" : ""}`;

// Under each shared page drawn in a step's text is a link that opens it full size (NEXT-56), over Cockpit's page
// (page-overlay.js). A link to a shared page on a line of its own draws as the page's button (NEXT-57).
// In a step's text that takes the user's answer, a link to a shared page opens it in the Pages
// view for the step, and under each shared page drawn in the text is also the way to comment on it there.
// Returns `body`.
// Any other link to a shared page, in a sentence, shows the page icon before its words.
function pageComments(body, step) {
  const answers = takesComments(step);
  for (const link of body.querySelectorAll("a[href]")) {
    const pageId = sharedPageId(link.href);
    if (pageId === null) continue;
    if (/^\/pages\/\d+$/.test(link.pathname) && !link.search && !link.hash) {
      if (answers) link.href = pageForStep(pageId, step, false);
      // A link that is all its paragraph holds, on a line of its own in the text, is the page's button (NEXT-57).
      const line = link.parentElement;
      if (line.matches(".prose > p, .prose details > p") && line.childNodes.length === 1) {
        pageButton(link, link.textContent.trim());
        continue;
      }
    }
    link.classList.add("page-link");
    link.prepend(icon("doc"));
  }
  for (const frame of body.querySelectorAll(".figure iframe.figure-frame")) {
    const match = /^\/pages\/(\d+)\/figure/.exec(frame.dataset.src || frame.getAttribute("src") || "");
    if (!match) continue;
    const pageId = Number(match[1]);
    const name = frame.getAttribute("title") || "";
    const links = [h("a", {
      class: "figure-open", href: answers ? pageForStep(pageId, step, false) : `${location.origin}/pages/${pageId}`,
      title: "Open this page at full size", "data-page-name": name,
    }, icon("wider"), "Open full size")];
    if (answers) {
      // Choices made in the figure go with the answer to this step (NEXT-48, page-choices.js).
      frame.dataset.step = String(step.id);
      links.push(h("a", {
        class: "figure-comment", href: pageForStep(pageId, step, true), target: "_blank", rel: "noopener",
        title: "Your comments go with your answer to this step", "data-page-name": name,
      }, icon("chat"), "Comment on this page"));
    }
    frame.closest(".figure").append(h("span", { class: "figure-links" }, ...links));
    if (ui.figureLinked.has(figureAddress(frame))) linkFigure(frame);
  }
  return body;
}

// A shared page's button in a step's text (NEXT-57): the page's name and "Open page" on a row of its own, which
// opens the page over Cockpit's page like any link to a page (page-overlay.js).
function pageButton(link, name) {
  link.classList.add("page-button");
  link.dataset.pageName = name;
  link.replaceChildren(icon("doc"), h("span", { class: "page-button-name", text: name }),
    h("span", { class: "page-button-open" }, "Open page", icon("chevron-right")));
}

// A figure page too tall for its frame would scroll inside the step, so the step shows the page's button in the
// frame's place (NEXT-57): the "Open full size" link, drawn as the button under the page's name.
function linkFigure(frame) {
  const figure = frame.closest(".figure");
  // A frame loading ahead or set aside for a redraw is on no card; the card draws the button when it draws.
  if (figure) {
    figure.classList.add("figure-linked");
    figure.querySelector(".figure-caption")?.remove();
    const open = figure.querySelector("a.figure-open");
    // The name is the words the step gave the figure, which `pageComments` put on the link.
    pageButton(open, open.dataset.pageName || "Shared page");
    open.title = "Too tall to show in the step";
    figure.prepend(open);
    // The opened page has its own way to comment, so the button stands alone like one from a link.
    figure.querySelector(".figure-links").remove();
  }
  frame.remove();
}

// The user's page comments that go with their answer to a step (NEXT-38), from the state.
function stepPageComments(step, state) {
  return (state.page_comments || []).filter((comment) => comment.step_id === step.id);
}

// The user's choices on pages that go with their answer to a step (NEXT-48), from the state.
function stepPageChoices(step, state) {
  return (state.page_choices || []).filter((choice) => choice.step_id === step.id);
}

// Links as a row of source icon and words, and past four the rest behind "+N". A link the text above already holds is
// left out, except the item's pages, which are always listed (API-3).
function shownLinks(links, html) {
  const inText = (link) => html.includes(`href="${link.url}"`) || html.includes(`href="${link.url.replace(/&/g, "&amp;")}"`);
  return links.filter((link) => link.page || !inText(link));
}

function linkAnchor(link, props) {
  const words = link.label.replace(/^(Slack|GitHub|Linear|PR|Doc|Docs):\s*/i, "");
  const ref = pullRef(link.url);
  const state = ref && pullState(ref);
  const title = state ? { title: `${props.title || words} · ${state}` } : {};
  return h("a", { ...props, ...title, href: link.url, target: "_blank", rel: "noopener noreferrer" },
    icon(linkIcon(link.url)), h("span", { text: words }));
}

// A link to a pull request in text the server rendered (render.py's src-github) draws its state too (marks.css), and
// names it on hover.
function markPulls(root) {
  for (const link of root.querySelectorAll("a.src-github")) {
    const ref = pullRef(link.href);
    if (!ref) continue;
    const state = pullState(ref);
    link.classList.add("pull");
    if (state) {
      link.classList.add(`pr-${state}`);
      link.title = `${link.title || link.textContent} · ${state}`;
    }
  }
}

function linkList(key, links, html) {
  const shown = shownLinks(links, html);
  if (!shown.length) return null;
  const anchor = (link) => linkAnchor(link, {});
  const rest = shown.slice(LINKS_TOP);
  const more = rest.length > 0 && h("details", { class: "menu links-more" },
    h("summary", { class: "button small ghost", title: `${rest.length} more link${rest.length === 1 ? "" : "s"}` }, `+${rest.length}`),
    h("div", { class: "menu-list" }, rest.map(anchor)));
  if (more) {
    more.open = ui.open.has(`${key}:links`);
    rememberOpen(more, (open) => { if (open) ui.open.add(`${key}:links`); else ui.open.delete(`${key}:links`); });
  }
  return h("div", { class: "links" }, shown.slice(0, LINKS_TOP).map(anchor), more);
}

// A step's links on its card, under its text and above its answers: the links the step is about (its core links), a
// chip each, then the quiet "Other N" chip, which opens the task's other links in two groups: the task's pages and
// links ("On this task"), then the links written in its other steps and in the user's messages on it ("Mentioned
// earlier"). A link shows once, in its first place; one the step's text holds is left out, except a page. A core link
// to one of the task's pages opens as `itemLinks` opens a page. `full`: the step's item as `stepItem` finds it, or
// null; `html`: the step's text. Null when the step has no links to show.
function coreLinks(key, step, full, html) {
  const pages = new Set(full ? full.pages.map((page) => page.id) : []);
  const seen = new Set();
  const unseen = (link) => {
    const same = linkKey(link.url);
    if (seen.has(same)) return false;
    seen.add(same);
    return true;
  };
  const core = step.core_links.filter(unseen).map((link) => {
    const pageId = sharedPageId(link.url);
    const page = pages.has(pageId);
    return { label: link.label, url: page && takesComments(step) ? pageForStep(pageId, step, false) : link.url, page };
  });
  const task = shownLinks(full ? itemLinks(full, step) : step.item.links, html).filter(unseen);
  const mentioned = shownLinks(full ? full.mentioned_links.filter((link) => link.step_id !== step.id) : [], html).filter(unseen);
  const count = task.length + mentioned.length;
  if (core.length === 0 && count === 0) return null;
  let other = null;
  if (count > 0) {
    const openKey = `${key}:links-other`;
    const group = (head, links) => links.length > 0 && [h("p", { class: "links-head", text: head }), links.map((link) => linkAnchor(link, {}))];
    other = h("details", { class: "menu links-other" },
      h("summary", { class: "link-chip quiet", title: `${count} other link${count === 1 ? "" : "s"} of this task` },
        "Other ", h("b", { text: String(count) }), icon("chevron")),
      h("div", { class: "menu-list" }, group("On this task", task), group("Mentioned earlier", mentioned)));
    other.open = ui.open.has(openKey);
    rememberOpen(other, (open) => { if (open) ui.open.add(openKey); else ui.open.delete(openKey); });
    // A list that would reach past the window's right edge opens leftwards by as much; a phone's is a sheet
    // (style.css). Set through the style object, as the page's CSP refuses style attributes.
    other.addEventListener("toggle", () => {
      const list = other.querySelector(".menu-list");
      list.style.left = "";
      if (!other.open || !list.offsetParent || getComputedStyle(list).position === "fixed") return;
      const over = list.getBoundingClientRect().right - (document.documentElement.clientWidth - 12);
      if (over > 0) list.style.left = `${-over}px`;
    });
  }
  return h("div", { class: "core-links" }, core.map((link) => linkAnchor(link, { class: "link-chip", title: link.label })), other);
}

function draftArea(key, placeholder, rows, initial) {
  const el = h("textarea", { rows, placeholder, "data-key": key });
  el.value = ui.drafts.has(key) ? ui.drafts.get(key) : initial;
  const height = ui.fieldHeights.get(key);
  if (height) el.style.height = height;
  const stepId = /^(?:msg|fb):step:(\d+)$/.exec(key)?.[1];
  el.addEventListener("input", () => {
    ui.drafts.set(key, el.value);
    saveDrafts();
    if (stepId) showDraftMarks(stepId);
  });
  return el;
}

// A composer's text field: the Vim editor's field when that editor loaded, else a plain one like it. While the text
// has a quote line, the words show through a copy drawn under the transparent text of the box, where each quote
// line reads as a tinted block with a bar at its left and no visible ">". The copy keeps every character, with the
// box's font and padding, so its lines wrap where the box's do and the caret stays on its letter.
function composerField(area, send) {
  const field = window.CockpitEditor ? window.CockpitEditor.create(area, { submit: send }) : h("div", { class: "prompt-field" }, area);
  const copy = h("div", { class: "quote-copy", "aria-hidden": "true" });
  field.prepend(copy);
  const sync = () => {
    const quoting = hasQuote(area.value);
    field.classList.toggle("quoting", quoting);
    if (!quoting) return;
    const style = getComputedStyle(area);
    for (const name of ["fontFamily", "fontSize", "fontWeight", "fontStyle", "letterSpacing", "lineHeight", "paddingTop", "paddingBottom", "paddingLeft", "marginTop", "marginBottom", "tabSize"]) {
      copy.style[name] = style[name];
    }
    // The box's scrollbar takes width from its text; the copy, which never shows one, gives up the same width.
    copy.style.paddingRight = `${parseFloat(style.paddingRight) + area.offsetWidth - area.clientWidth}px`;
    copy.replaceChildren(...area.value.split("\n").map((line) => {
      const marker = QUOTE_LINE.exec(line)?.[0];
      if (!marker) return h("div", { text: line || "​" });
      return h("div", { class: "quote-line" }, h("span", { class: "quote-mark", text: marker }), line.slice(marker.length) || "​");
    }));
    copy.scrollTop = area.scrollTop;
  };
  area.addEventListener("input", sync);
  area.addEventListener("scroll", () => { copy.scrollTop = area.scrollTop; });
  requestAnimationFrame(sync);
  return field;
}

function focusDraft(area, options) {
  if (window.CockpitEditor) window.CockpitEditor.focus(area, options);
  else area?.focus(options);
}

// Reasons every step of a kind has, which say nothing about this one: the ask says them already.
const QUIET_REASONS = /^(question for you|unanswered since\b|quick decision$)/i;

// At most two of the sweeper's reasons, leaving out the quiet ones and those that repeat whom the
// item blocks (its blocks tag names them).
function shownReasons(step) {
  const blocked = new Set((step.item.blocks.match(/\b[A-Z][\w-]+/g) || []));
  return step.reasons.filter((reason) => !QUIET_REASONS.test(reason)
    && !(reason.match(/\b[A-Z][\w-]+/g) || []).some((word) => blocked.has(word))).slice(0, 2);
}

function reasonChips(step) {
  return shownReasons(step).map((reason) => h("span", { class: "chip reason", text: reason }));
}

// An early look (SPEC NEXT-42): a session's early result or question while its work goes on. It takes a place only
// while no other step waits for one: a tag on the card, an hourglass after a queue row's title.
const EARLY_TITLE = "Early look: the session shares this while it keeps working. It shows only while your queue has room no other step needs.";

function earlyChip(step) {
  return step.early ? h("span", { class: "chip early", title: EARLY_TITLE }, icon("hourglass"), h("span", { class: "chip-words", text: "Early look" })) : null;
}

function earlyMark(step) {
  return step.early ? h("span", { class: "quick-mark early-mark", role: "img", "aria-label": "Early look", title: EARLY_TITLE }, icon("hourglass")) : null;
}

// "45 s", "6 min", "1 h 12 min": how long the user read a card.
function readWords(seconds) {
  // Non-breaking spaces keep a number with its unit on a narrow screen.
  if (seconds < 60) return `${seconds}\u00a0s`;
  const minutes = Math.round(seconds / 60);
  return minutes < 60 ? `${minutes}\u00a0min` : `${Math.floor(minutes / 60)}\u00a0h ${minutes % 60}\u00a0min`;
}

// An agent rewrote this card after the user read it (SPEC NEXT-61), until the user reads the new card, writes or
// presses on the task: a teal band above the merge band. It says how long the user read the card before the first
// rewrite they have not seen and when they stopped, the time of the latest rewrite, and each distinct note once.
// `item`: the step's item as the state lists it, or null.
function changedBanner(step, item) {
  const changes = item ? item.changes.filter((change) => change.step_id === step.id) : [];
  if (!changes.length) return null;
  const first = changes[0];
  const last = changes[changes.length - 1].at;
  const notes = [...new Set(changes.map((change) => change.note).filter(Boolean))];
  // Relative times, kept current by `tickTimers`; the exact time shows on hover.
  const since = (iso) => h("span", { "data-time": iso, "data-ago": iso, text: ago(iso) });
  return h("section", { class: "changed-banner", "aria-label": "Changed after you read it" },
    icon("pencil"),
    h("div", { class: "changed-text" },
      h("p", { class: "changed-title" }, "Changed ", since(last)),
      h("p", { class: "changed-why" }, `You read the earlier version for ${readWords(first.read_seconds)}, until `,
        since(first.read_until), "."),
      notes.map((note) => h("p", { class: "changed-note", text: note }))));
}

// A step useful only until a time (SPEC NEXT-60): a red band at the top of the card with the time and a live count of
// what is left, and once the time passed, a muted band that says so and whether Cockpit asked the session to renew it.
// A card kept on the page after its step closed has no band.
function expiryBanner(step) {
  if (!step.expires_at || step.left) return null;
  const until = clock(step.expires_at);
  if (!expiring(step)) {
    return h("section", { class: "expiry-banner ended", "aria-label": "Its time ran out" },
      icon("timer"),
      h("p", { class: "expiry-title" }, `Its time ran out at ${until}.`,
        step.expiry_told_at && " The session was asked to renew it."));
  }
  return h("section", { class: "expiry-banner", "aria-label": `Works until ${until}` },
    icon("timer"),
    h("p", { class: "expiry-title" }, `Works until ${until} `,
      h("span", { class: "expiry-left" },
        h("span", { "data-expires": step.expires_at, text: timerClock(secondsLeft(step.expires_at)) }), " left")));
}

// Another task merged into this one (SPEC NEXT-55), until the user's next message or press on it: a band under the
// card's heading in the merge pink, naming the task that joined, why, and the cards it had open, which this card now
// covers. `item`: the step's item as the state lists it, or null.
function mergedBanner(item) {
  if (!item || !item.merges.length) return null;
  return h("section", { class: "merged-banner", "aria-label": "Merged in" }, item.merges.map((merge) =>
    h("div", { class: "merged-one" },
      icon("joined"),
      h("div", { class: "merged-text" },
        h("p", { class: "merged-title" }, h("span", { class: "merged-word", text: "Merged in: " }), merge.from_title,
          h("span", { class: "merged-when", "data-time": merge.at, "data-ago": merge.at, text: ago(merge.at) })),
        h("p", { class: "merged-note", text: merge.note }),
        merge.moved_steps.length > 0 && [
          h("p", { class: "merged-cards-head", text: merge.moved_steps.length === 1 ? "Its card, which this one now covers:" : "Its cards, which this one now covers:" }),
          h("ul", { class: "merged-cards" }, merge.moved_steps.map((card) => h("li", { text: linkWords(card.ask) }))),
        ]))));
}

// The merge on a closed queue row: the joining arrows after its title, in the merge pink.
function mergedMark(item) {
  if (!item || !item.merges.length) return null;
  const words = `Merged in: ${item.merges.map((merge) => merge.from_title).join(", ")}`;
  return h("span", { class: "quick-mark merged-mark", role: "img", "aria-label": words, title: words }, icon("joined"));
}

// A quick decision: a bolt before the ask, and after a queue row's title.
function quickMark(step) {
  if (!step.reasons.some((reason) => /^quick decision$/i.test(reason))) return null;
  return h("span", { class: "quick-mark", role: "img", "aria-label": "Quick decision", title: "Quick decision" }, icon("bolt"));
}

// How big the user's part of a step is (the bar's height) and how much of the user's time on its item is behind
// them (the fill, left to right), as the step's session judged them (SPEC NEXT-35); no bar while either is unset.
// Once enough estimates are checked against the user's timer, the fill takes off their typical lead
// (ui.state.progress_correction). A press opens Your time, lower on the page, where both estimates are checked.
function sizeBar(step) {
  if (!step.size || step.progress === null) return null;
  const correction = ui.state && ui.state.progress_correction;
  const shown = correction ? Math.min(100, Math.max(0, step.progress - correction.median_gap)) : step.progress;
  const words = `Your part: ${step.size}, ${shown}% behind you`;
  const fill = h("span", { class: "size-fill" });
  fill.style.width = `${shown}%`;
  if (SIDEBAR) return h("span", { class: `size-bar size-${step.size}`, role: "img", "aria-label": words, title: words }, h("span", { class: "size-track" }, fill));
  return h("a", { class: `size-bar size-${step.size}`, href: "#home/time", "aria-label": `${words}. How accurate these estimates are`,
    title: words, onclick: (event) => { event.preventDefault(); event.stopPropagation(); openEstimates(); } },
  h("span", { class: "size-track" }, fill));
}

// Your time, lower on the page, open at the checks of the size and progress estimates (SPEC NEXT-35): renderTime scrolls
// to them once the report is in.
function openEstimates() {
  ui.open.add("time");
  ui.closed.delete("time");
  ui.toEstimates = true;
  scrollToId("time");
  if (ui.time === null && !ui.timeError) loadTime();
  else renderTime();
}

// New since the user last looked: a small amber dot.
function newDot(step) {
  return isNew(step) ? h("span", { class: "new-dot", role: "img", "aria-label": "New", title: "New" }) : null;
}

function rankTitle(step) {
  return `Sweep score ${step.score}`;
}

// `cls`: the menu's own class besides "menu" ("snooze", "more", …). An entry whose action is null is a line of text,
// such as when the step was checked.
function menu(key, label, entries, cls) {
  const el = h("details", { class: `menu ${cls}` });
  el.open = ui.open.has(key);
  rememberOpen(el, (open) => { if (open) ui.open.add(key); else ui.open.delete(key); });
  el.append(
    h("summary", { class: "button" }, label),
    h("div", { class: "menu-list" }, entries.map(([text, hint, onclick]) => onclick === null
      ? h("p", { class: "menu-note" }, text)
      : h("button", {
        type: "button",
        onclick: () => { ui.open.delete(key); el.open = false; onclick(); },
      }, h("span", { class: "menu-label" }, text), hint && h("span", { class: "when", text: hint })))),
  );
  // A menu that opens upward (Next up's keys) with more lines than the room above it, below a header that stays on
  // screen, is cut to that room and scrolls inside, so its first lines stay in view. The room is measured from what
  // the menu sits on, which its opening animation leaves in place.
  el.addEventListener("toggle", () => {
    if (!el.open || !el.closest(".next-card > .actions")) return;
    const list = el.querySelector(".menu-list");
    list.style.maxHeight = "";
    // A redraw can detach the card before the toggle event arrives; measure attached menus only.
    if (!list.offsetParent) return;
    const header = document.getElementById("header");
    const ceiling = header && /sticky|fixed/.test(getComputedStyle(header).position) ? Math.max(0, header.getBoundingClientRect().bottom) : 0;
    const room = list.offsetParent.getBoundingClientRect().top - 6 - ceiling - 8;
    if (list.offsetHeight > room) list.style.maxHeight = `${Math.max(room, 120)}px`;
  });
  return el;
}

// How a message reaches a session. "unknown": the session is not in `claude agents`, so a resume would fail.
function sessionStatus(session) {
  return session.waits_on_dialog ? `live, at a dialog in its terminal (${session.needs || "it waits for an answer"}); it reads messages once that is answered`
    : session.alive ? "live"
    : session.state === "unknown" ? "not found on this machine"
    : `${session.state}, Cockpit resumes it`;
}

// A new session for an item starts in its project's own checkout while it is one of `state.checkouts` (SPEC
// SESSION-24), else in the checkout named after its project, else in the first one (~/dev).
function defaultCheckout(project, state) {
  const own = project && state.projects.find((one) => one.name === project)?.checkout;
  if (own && state.checkouts.includes(own)) return own;
  // Only the folder's own checkouts match by name; a project's deeper checkout serves only its own project.
  const folder = state.checkouts[0];
  return state.checkouts.find((checkout) => project && checkout === `${folder}/${project}`) || folder;
}

// What a new session from the box under `key` starts with (SPEC SESSION-22): the agent, model, reasoning effort,
// speed and Claude account the user picked, else `saved` (what an item kept from Add, or null), each null while it is
// Cockpit's default. A pick the page no longer offers (an agent since turned off, a model taken out, an account
// gone) counts as the default, so the line shows what Send sends and Send is not refused for it.
function startPicks(key, saved) {
  const picks = ui.starts.get(key) || saved || { provider: null, model: null, effort: null, account: null, speed: null };
  const offered = ui.state && ui.state.session_options;
  if (!offered) return picks;
  if (picks.provider && !offered[picks.provider]) return { provider: null, model: null, effort: null, account: null, speed: null };
  const provider = picks.provider || ui.state.default_provider || "claude";
  const choices = offered[provider];
  if (!choices) return picks;
  const model = choices.models.find((each) => each.name === picks.model) || choices.models[0];
  return {
    provider: picks.provider,
    model: choices.models.some((each) => each.name === picks.model) ? picks.model : null,
    effort: choices.efforts.includes(picks.effort) ? picks.effort : null,
    speed: (model.speeds || []).some((each) => each.name === picks.speed) ? picks.speed : null,
    account: provider === "claude" && (ui.state.session_accounts || []).some((each) => each.name === picks.account) ? picks.account : null,
  };
}

// The efforts in rising order: an effort's place is how many of its icon's five bars are lit (icons.js).
const EFFORTS = ["low", "medium", "high", "xhigh", "max"];

// An account's letter, as its button shows it: W for work, P for personal (config.ACCOUNTS).
function accountLetter(account) {
  return h("span", { class: "account-letter", "aria-hidden": "true", text: account.letter });
}

// The settings a new session starts with (SPEC SESSION-22): one small button per choice beside the target, each with
// its icon, which opens that choice's own list. Agent shows while both Claude and Codex are on, Account while a
// Claude session has two accounts to pick from. Picking another agent puts the rest back to that agent's defaults.
function startOptions(key, saved) {
  const holder = h("div", { class: "start-options" });
  const draw = () => {
    const state = ui.state;
    // The Add box opens before the page has its state; render draws the line once it does.
    holder.dataset.offers = state ? startOffers(state) : "";
    if (!state) { holder.replaceChildren(); return; }
    const offered = state.session_options || {};
    const picks = startPicks(key, saved);
    const provider = picks.provider || state.default_provider || "claude";
    const choices = offered[provider];
    if (!choices) { holder.replaceChildren(); return; }
    const model = choices.models.find((each) => each.name === picks.model) || choices.models[0];
    const effort = picks.effort || choices.effort;
    const speeds = model.speeds || [];
    const speed = speeds.find((each) => each.name === picks.speed) || speeds[0];
    const accounts = provider === "claude" ? state.session_accounts || [] : [];
    const account = accounts.find((each) => each.name === picks.account) || null;
    const agents = Object.keys(offered);
    const agentName = (name) => (name === "codex" ? "Codex" : "Claude");
    const effortIcon = (name) => icon(`effort-${EFFORTS.indexOf(name) + 1}`);
    const set = (field, value) => {
      // The agent already picked keeps the other picks; focus goes back to the button either way.
      if (field !== "provider" || value !== provider) {
        const next = field === "provider"
          ? { provider: value, model: null, effort: null, account: null, speed: null }
          : { ...picks, [field]: value };
        ui.starts.set(key, next);
        saveDrafts();
        draw();
      }
      const again = holder.querySelector(`[data-key="${CSS.escape(`${key}:${field}`)}"]`);
      if (again) again.focus();
    };
    // One choice's button and its list. `shown`: the button's icon and words; `entries`: [value, icon, words, hint]
    // per line of the list, the value null for the default where the pick is stored as null (Automatic).
    const pick = (field, label, shown, current, entries) => {
      const menuKey = `start:${key}:${field}`;
      const el = h("details", { class: "menu start-pick", "data-menu": menuKey });
      el.open = ui.open.has(menuKey);
      rememberOpen(el, (isOpen) => { if (isOpen) ui.open.add(menuKey); else ui.open.delete(menuKey); });
      // Escape closes the open list alone, back on its button; in Add it leaves the dialog open.
      el.addEventListener("keydown", (event) => {
        if (event.key !== "Escape" || !el.open) return;
        event.preventDefault();
        event.stopPropagation();
        ui.open.delete(menuKey);
        el.open = false;
        el.querySelector(".start-button").focus();
      });
      el.append(
        h("summary", { class: "start-button", "data-key": `${key}:${field}`, title: `${label}: ${shown.title}`, "aria-label": `${label}: ${shown.title}` },
          shown.icon, shown.words && h("span", { class: "start-words", text: shown.words }), icon("chevron")),
        h("div", { class: "menu-list" }, entries.map(([value, mark, words, hint]) => h("button", {
          type: "button", class: value === current ? "picked" : "", "data-value": value || "", "aria-current": value === current ? "true" : null,
          onclick: () => { ui.open.delete(menuKey); el.open = false; set(field, value); },
        }, h("span", { class: "menu-label" }, mark, words), h("span", { class: "when" }, hint, value === current && icon("check"))))));
      return el;
    };
    const isDefault = (yes) => (yes ? "default" : "");
    const defaultAgent = state.default_provider || "claude";
    // Agent and Account show only with more than one to pick from; replaceChildren would write a false as text.
    holder.replaceChildren(...[
      agents.length > 1 && pick("provider", "Agent", { icon: icon(provider), words: agentName(provider), title: agentName(provider) }, provider,
        agents.map((name) => [name, icon(name), agentName(name), isDefault(name === defaultAgent)])),
      pick("model", "Model", { icon: null, words: model.label, title: model.label }, model.name,
        choices.models.map((each, at) => [each.name, null, each.label, isDefault(at === 0)])),
      pick("effort", "Effort", { icon: effortIcon(effort), words: effort, title: `${effort} effort` }, effort,
        choices.efforts.map((each) => [each, effortIcon(each), each, isDefault(each === choices.effort)])),
      speeds.length > 1 && pick("speed", "Speed", { icon: null, words: speed.label, title: `${speed.label} · ${speed.hint}` }, speed.name,
        speeds.map((each) => [each.name, null, each.label, each.hint])),
      accounts.length > 1 && pick("account", "Account",
        account ? { icon: accountLetter(account), words: "", title: `${account.name} account` }
          : { icon: icon("meter"), words: "Auto", title: "Automatic: the account whose week resets soonest" },
        picks.account,
        [[null, icon("meter"), "Automatic", "favours soonest reset"],
          ...accounts.map((each) => [each.name, accountLetter(each), each.name, ""])]),
    ].filter(Boolean));
  };
  holder.draw = draw;
  draw();
  return holder;
}

// The composer: one text box, the target shown and changeable (a step's reply box has none: it goes to the step's
// session, or starts a new one). Option values name the target itself ("session:<id>", "new:<checkout>"), and typing
// binds the draft to the target shown, so a redraw that changes the default target keeps an unsent draft addressed
// where the user wrote it.
// `session`: the default target ({session_id, name, state, alive, working_now}) or null.
// `context`: {item_id, step_id} sent with the message ({} for a bare session).
// `project`: picks the checkout for a new session (null: the first checkout).
// `step`: the step this composer answers, or null.
// `handOff`: whether Send hands that step to the session (CONTROL-5): the step leaves the user's queue and waits under
// You handed these until the session closes it or gives it back. Otherwise Send only sends.
// When the box writes to the item's Claude session, the menu of the arrow attached to Send has Ask and Fork (MSG-11):
// Ask puts the words to a read-only copy of the session, which answers above the box while the words stay in it; Fork
// starts a new item whose session starts from this one's conversation. Neither hands off the step.
// Empty and not in use, the composer is one line; the target (when shown) and Send show once the user writes.
// `toSession`: the box writes only to `session` when there is one, and then shows no picker. With a `step` it never
// shows one: without a session its words start a new session in the project's checkout, at the defaults.
function composer(key, { session, context, project, step, handOff, toSession }) {
  const state = ui.state;
  const sessions = sessionMap(state);
  // Each option: its value, its label, the target's name for the sent mark and toast, and how it is reached.
  const options = [];
  // The item's Claude session that Ask and Fork copy, with its name, or null.
  let side = null;
  if (session) {
    const known = sessions.get(session.session_id) || session;
    const name = sessionLabel(known, "");
    options.push([`session:${session.session_id}`, name, name, sessionStatus(known)]);
    if (context.item_id && (known.provider || "claude") === "claude") side = { id: session.session_id, name };
  }
  const stored = ui.choices.get(key);
  if (stored && stored.startsWith("session:") && ui.drafts.get(key) && !options.some(([value]) => value === stored)) {
    const known = sessions.get(stored.slice("session:".length));
    const name = sessionLabel(known, "");
    options.push([stored, name, name, `where you started this draft${known ? `; ${sessionStatus(known)}` : ""}`]);
  }
  // A new session's agent, model, effort and account are picked beside the target (startOptions).
  for (const checkout of state.checkouts) {
    const repo = repoOf(checkout);
    const home = state.home;
    const folder = home && checkout.startsWith(`${home}/`) ? `~${checkout.slice(home.length)}` : checkout;
    options.push([`new:${checkout}`, `New session: ${repo}`, `a new session in ${repo}`, `starts in ${folder}`, checkout]);
  }
  if (!options.length) return h("p", { class: "note", text: "Add a checkout to start a session" });
  const fallback = session ? `session:${session.session_id}` : `new:${defaultCheckout(project, state)}`;
  // A step's reply box shows no picker and no start line (SPEC SESSION-22): its words go to the step's own session, or
  // with none start a new session in the project's checkout with the item's picks from Add, else Cockpit's defaults.
  // The item card's and the session view's boxes on the sidebar page show none while they have a session. Every
  // other box picks where the words go, or which checkout a new session starts in and with what.
  const fixed = Boolean(toSession && (session || step));
  if (fixed) {
    // A choice or picks kept from before give way to the box's one target, also in what Send records.
    if (stored && stored !== fallback) ui.choices.set(key, fallback);
    ui.starts.delete(key);
  }
  const choice = !fixed && options.some(([value]) => value === stored) ? stored : fallback;
  const select = h("select", { "data-key": `${key}:to`, "aria-label": "To", onchange: () => { ui.choices.set(key, select.value); saveDrafts(); } },
    options.map(([value, label, name, status, cwd]) => h("option", {
      value, selected: value === choice, "data-name": name, "data-status": status, "data-cwd": cwd,
    }, label)));
  // A to-do's first session starts with what the user picked on Add, shown here until changed (SPEC SESSION-22).
  const saved = (context.item_id && state.items.find((item) => item.id === context.item_id)?.start_options) || null;
  const starting = startOptions(key, saved);
  const name = () => select.selectedOptions[0].dataset.name;
  // The step's reply hint says what kind of answer it expects (NEXT-51); it is a placeholder, never a draft.
  const hint = step ? step.reply_hint || "Reply…" : "Message…";
  const area = draftArea(key, finishedTask(state, step ? step.item_id : context.item_id) ? `${hint} Sending reopens this task` : hint, 1, "");
  area.setAttribute("aria-label", step ? "Reply" : "Message");
  // `drafted`: the box holds words not sent; other boxes styled as composers stay `filled` while open.
  const box = h("div", { class: "composer inline-send" });
  const where = () => {
    const picked = select.value;
    if (picked.startsWith("session:")) return { session_id: picked.slice("session:".length) };
    const picks = startPicks(key, saved);
    // The start names its agent and the picks the line shows, also the item's own from Add, so Cockpit starts
    // exactly those and does not fill in the item's picks itself (store.create_message).
    return { new_cwd: select.selectedOptions[0].dataset.cwd, ...picks, provider: picks.provider || ui.state.default_provider || "claude" };
  };
  const send = () => (handOff ? handOffDraft(key, step, where(), name()) : sendMessage(key, context, where(), name(), step));
  const comments = handOff ? stepPageComments(step, state) : [];
  const choices = handOff ? stepPageChoices(step, state) : [];
  const carried = comments.length + choices.length;
  // Page comments and choices alone are an answer, so Send shows with them as with words.
  box.classList.toggle("filled", Boolean(area.value.trim()) || carried > 0);
  box.classList.toggle("drafted", Boolean(area.value.trim()));
  const sendButton = iconButton("send", "Send", { class: "button send", "data-key": "Ctrl+Enter", disabled: !area.value.trim() && !carried, onclick: send });
  // The arrow attached to Send opens the other ways to send: Attach images, Ask and Fork.
  const ways = h("details", { class: "menu send-ways" });
  const waysKey = `menu:send:${key}`;
  ways.open = ui.open.has(waysKey);
  rememberOpen(ways, (open) => { if (open) ui.open.add(waysKey); else ui.open.delete(waysKey); });
  const closeWays = () => { ui.open.delete(waysKey); ways.open = false; };
  // The menu rows' second lines already say what each does; the hover says the one result they leave out.
  const askButton = side && h("button", {
    class: "ask-way", type: "button", disabled: !area.value.trim(),
    title: "A read-only copy of the session answers above this box",
    onclick: () => { closeWays(); askQuestion(key, context.item_id, context.step_id ?? null, side.id, side.name); },
  }, h("span", { class: "menu-label" }, icon("chat"), "Ask"), h("span", { class: "when", text: "quick answer; keep writing" }));
  const forkButton = side && h("button", {
    class: "fork-way", type: "button", disabled: !area.value.trim(),
    title: step ? "Nothing goes back to this session; the step stays" : "Nothing goes back to this session",
    onclick: () => { closeWays(); forkSession(key, context.item_id, side.id, side.name); },
  }, h("span", { class: "menu-label" }, icon("fork"), "Fork"), h("span", { class: "when", text: "new item from this conversation" }));
  // Images go with the words to the message's task (SPEC MSG-18), so the box takes them while it has one.
  const attachable = () => messageItem(context, step, where()) !== null;
  const picker = h("input", {
    type: "file", accept: [...IMAGE_TYPES].join(","), multiple: true, hidden: true, tabindex: "-1",
    onchange: () => { stageComposerImages(key, imageFiles(picker)); picker.value = ""; },
  });
  const attachButton = h("button", {
    class: "attach-way", type: "button", title: "They go on this task with your message",
    onclick: () => { closeWays(); picker.click(); },
  }, h("span", { class: "menu-label" }, icon("image"), "Attach images"), h("span", { class: "when", text: "or paste or drop them in the box" }));
  const staged = h("div", { class: "thumbs small composer-images", "data-staged": key, hidden: !stagedImages(key).length }, stagedThumbs(key));
  box.addEventListener("paste", (event) => {
    const files = imageFiles(event.clipboardData);
    if (!files.length || !attachable()) return;
    event.preventDefault();
    stageComposerImages(key, files);
  });
  const carriesFiles = (event) => event.dataTransfer && [...event.dataTransfer.types].includes("Files");
  box.addEventListener("dragover", (event) => {
    if (!carriesFiles(event) || !attachable()) return;
    event.preventDefault();
    box.classList.add("drop-over");
  });
  box.addEventListener("dragleave", (event) => { if (!box.contains(event.relatedTarget)) box.classList.remove("drop-over"); });
  box.addEventListener("drop", (event) => {
    if (!carriesFiles(event) || !attachable()) return;
    event.preventDefault();
    box.classList.remove("drop-over");
    stageComposerImages(key, imageFiles(event.dataTransfer));
  });
  const waysSummary = h("summary", { class: "button send-more", "aria-label": "Other ways to send", title: "Other ways to send" }, icon("chevron"));
  ways.append(waysSummary, h("div", { class: "menu-list" }, attachButton, askButton, forkButton));
  area.addEventListener("input", () => {
    ui.choices.set(key, select.value);
    saveDrafts();
    const filled = Boolean(area.value.trim());
    box.classList.toggle("filled", filled || carried > 0);
    box.classList.toggle("drafted", filled);
    sendButton.disabled = !filled && !carried;
    if (!filled) closeStartPicks(starting);
    if (askButton) askButton.disabled = !filled;
    if (forkButton) forkButton.disabled = !filled;
  });
  area.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || !(event.ctrlKey || event.metaKey)) return;
    event.preventDefault();
    send();
  });
  // The hover texts say what Send does, for the target shown.
  const explain = () => {
    const option = select.selectedOptions[0];
    const fresh = select.value.startsWith("new:");
    select.title = `To ${name()}: ${option.dataset.status}`;
    starting.hidden = !fresh;
    // The buttons show only for a new session and once the box has words (an empty box drawn after Send too).
    if (!fresh || !area.value.trim()) closeStartPicks(starting);
    // Ask and Fork go with the item's own session, so they show while the box writes to that session; Attach images
    // while the message has a task. The arrow shows while one of them does.
    const withSide = Boolean(side) && select.value === `session:${side.id}`;
    if (askButton) askButton.hidden = !withSide;
    if (forkButton) forkButton.hidden = !withSide;
    attachButton.hidden = !attachable();
    ways.hidden = !withSide && attachButton.hidden;
    // An open menu holds back the page's keyboard shortcuts, so a menu that leaves the page closes.
    if (ways.hidden) closeWays();
    sendButton.title = handOff
      ? (fresh ? "Send: a new session takes the step until it needs you" : "Send: the session takes the step until it needs you")
      : (fresh ? "Send: starts a new session with your message" : "Send: the session gets it once its turn ends");
    sendButton.setAttribute("aria-label", "Send");
  };
  select.addEventListener("change", explain);
  explain();
  box.append(
    ...(comments.length > 0 ? [pendingComments(step, comments)] : []),
    ...(choices.length > 0 ? [pendingChoices(step, choices)] : []),
    ...(fixed ? [] : [h("div", { class: "composer-target" }, session && stateDot(session.alive ? "working" : session.state, null), select, starting)]),
    staged,
    composerField(area, send),
    h("div", { class: "composer-foot" }, h("div", { class: "send-split" }, sendButton, ways)),
    picker);
  // Nothing shows under the box about a message sent from it: Messages and the handed row say how it is doing, and the
  // sweeper resends one that failed.
  return box;
}

// While a page shows beside the step's card (page-overlay.js), the comments and choices the user leaves on it show open
// above the card's reply box, which sends them.
const besidePage = (step) => window.PageOverlay?.besideStep() === step.id;

// Above a step's reply box: the user's page comments that go with the answer (NEXT-38), each with what it is on
// and a link to it on its page, and Delete.
function pendingComments(step, comments) {
  const count = comments.length;
  return h("details", { class: "page-comments-pending", open: besidePage(step) },
    h("summary", null, h("b", { text: `${count} page comment${count === 1 ? "" : "s"}` }), " go with this answer"),
    h("ul", null, comments.map((comment) => h("li", null,
      h("a", { href: pageForStep(comment.page_id, step, true), target: "_blank", rel: "noopener", text: comment.page_title }),
      h("span", { class: "page-comment-on", text: ` · ${PageComments.onWords(comment.anchor)}` }),
      h("span", { class: "page-comment-text", text: comment.text }),
      iconButton("x", "Delete comment", { class: "button small ghost", onclick: () => deletePageComment(comment) })))));
}

// Above a step's reply box: the user's choices on pages that go with the answer (NEXT-48), each with its page and
// what was chosen; the page changes or clears a choice.
function pendingChoices(step, choices) {
  const count = choices.length;
  const shown = (choice) => choice.label || (Array.isArray(choice.chosen) ? choice.chosen.join(", ") : choice.chosen) || "cleared";
  return h("details", { class: "page-comments-pending", open: besidePage(step) },
    h("summary", null, h("b", { text: `${count} choice${count === 1 ? "" : "s"}` }), " on pages go with this answer"),
    h("ul", null, choices.map((choice) => h("li", null,
      h("a", { href: pageForStep(choice.page_id, step, false), text: choice.page_title }),
      h("span", { class: "page-comment-on", text: ` · ${choice.name}` }),
      h("span", { class: "page-comment-text", text: shown(choice) })))));
}

const commentDeletions = new Map();
function deletePageComment(comment) {
  const key = `page-comment:${comment.id}`;
  if (ui.busy.has(key)) return commentDeletions.get(comment.id).sent;
  const sent = run(key, async () => {
    try {
      await optimistic((state) => ({ ...state, page_comments: state.page_comments.filter((entry) => entry.id !== comment.id) }),
        () => api("DELETE", `/api/page-comments/${comment.id}`), null);
    } catch (error) {
      if (error.status !== 404) throw refused("Comment not deleted", error);
    }
    dispatchEvent(new CustomEvent("cockpit:page-comments-changed"));
  }, null);
  commentDeletions.set(comment.id, { stepId: comment.step_id, sent });
  // A refused deletion stays until Delete is tried again or refresh finds the comment gone.
  sent.then((done) => { if (done && commentDeletions.get(comment.id)?.sent === sent) commentDeletions.delete(comment.id); });
  return sent;
}

// A delivery state as a mark: a dot while it goes out, a green check once delivered,
// a red dot when it did not arrive. `detail`: the server's delivery note, for the tooltip.
function deliveryMark(delivery, detail) {
  const words = MESSAGE_STATES[delivery] || delivery;
  const title = delivery === "sent" ? words : [words, detail].filter(Boolean).join("\n");
  if (delivery === "sent") return h("span", { class: "delivered", role: "img", "aria-label": words, title }, icon("check"));
  return h("span", { class: `state-dot ${delivery}`, role: "img", "aria-label": words, title });
}

// Inline comments (NEXT-33): words the user selects in a step get a Comment button under them. It quotes
// the words into the step's reply box with the cursor under the quote, so each comment follows the
// part it is about, and Send takes them to the step's session like any reply. While text is selected
// the page does not redraw (safeRender), so the button stays by its words.
const QUOTE_CHARS = 600;
const quoteButton = h("button", {
  class: "button small quote-button", type: "button", hidden: true,
  title: "Quote this part in your reply",
}, icon("chat"), h("span", { class: "label", text: "Comment" }));

// The selection, when it lies in one step whose reply box is on the page: its words and that box's key.
function quotable() {
  const selection = window.getSelection();
  if (ui.picking || !selection || selection.isCollapsed || !selection.rangeCount) return null;
  const active = document.activeElement;
  if (active && active.matches(FIELDS)) return null;
  const text = selection.toString().trim();
  const range = selection.getRangeAt(0);
  const around = range.commonAncestorContainer;
  const el = around.nodeType === Node.ELEMENT_NODE ? around : around.parentElement;
  const step = el && !el.closest(".composer, .motion-ghost") && el.closest("[data-step]");
  if (!text || !step) return null;
  const key = `msg:step:${step.dataset.step}`;
  return step.querySelector(`textarea[data-key="${key}"]`) ? { text, range, key } : null;
}

// Written only when it changes: every press and selection change asks.
function showQuoteButton(shown) {
  if (quoteButton.hidden === shown) quoteButton.hidden = !shown;
}

// Under the selection's last line, its right edge at the selection's end, inside the window.
function placeQuoteButton() {
  const quote = ui.selecting ? null : quotable();
  showQuoteButton(Boolean(quote));
  if (!quote) return;
  const lines = quote.range.getClientRects();
  const last = lines.length ? lines[lines.length - 1] : quote.range.getBoundingClientRect();
  const width = quoteButton.offsetWidth;
  const left = Math.min(Math.max(last.right - width, 8), document.documentElement.clientWidth - width - 8);
  quoteButton.style.left = `${Math.round(left + window.scrollX)}px`;
  quoteButton.style.top = `${Math.round(last.bottom + window.scrollY + 6)}px`;
}

// The words as a Markdown quote after what the box holds, then an empty line for the comment.
function quoteIntoReply({ text, key }) {
  const quote = clip(text, QUOTE_CHARS).split(/\n+/).map((line) => line.trim()).filter(Boolean)
    .map((line) => `> ${line}`).join("\n");
  const before = (ui.drafts.get(key) || "").trimEnd();
  ui.drafts.set(key, `${before ? `${before}\n\n` : ""}${quote}\n\n`);
  saveDrafts();
  window.getSelection().removeAllRanges();
  showQuoteButton(false);
  render(false);
  const area = document.querySelector(`textarea[data-key="${CSS.escape(key)}"]`);
  if (!area) return;
  focusDraft(area);
  if (window.CockpitEditor) window.CockpitEditor.end(area);
  else area.setSelectionRange(area.value.length, area.value.length);
}

// Step card, used for Next up and for an expanded queue row.

// A draft longer than this folds to its first lines, with "Show whole draft".
const DRAFT_FOLD_LINES = 6;
const DRAFT_FOLD_CHARS = 480;
// How much of a run's output a You handed these row shows on hover.
const RUN_TIP_CHARS = 1500;

// The item of a step as All work lists it (with its images), or null.
function stepItem(step, state) {
  return state.items.find((other) => other.id === step.item_id) || null;
}

// What the step's ⋯ menu says about the step, a line each: when it was checked, what is ready, where the step's work
// sits, who prepared it, why it ranks where it does. `checked`: when the step was last checked against live state,
// for a top step in Next up, or null. `ready`: what the agent made ready, or "".
function aboutStep(step, group, checked, ready) {
  const item = step.item;
  return [
    ready && `Ready: ${ready}`,
    item.project && `Project: ${item.project}`,
    item.stage && `Stage: ${STAGE_LABELS[item.stage] || item.stage}`,
    step.prepared_by && `Prepared by ${actorWords(step.prepared_by)} ${ago(step.prepared_at)}`,
    checked,
    group === "next" && rankTitle(step),
  ].filter(Boolean);
}

// A step's details as collapsed sections, one under the other: each shows its whole title and opens
// in place to its text, the way a Markdown <details> block in the body does. Several can be open at
// once, and a redraw keeps each as the user left it.
// `step`: the step the details belong to, whose page links open the page for it (NEXT-38). `otherImages`:
// the closed section of the item's other images, last, or null.
function detailList(key, details, step, otherImages) {
  if (!details.length && !otherImages) return null;
  return h("div", { class: "details" }, details.map((detail, index) => {
    const openKey = `${key}:detail:${index}`;
    const body = prose(openKey, "prose", detail.body_html);
    pageComments(body, step);
    return disclosure(openKey, "detail", detail.title, body, false);
  }), otherImages);
}

// The text to post: a tinted box with Copy in its corner. A long one folds to its first lines;
// Copy takes the whole text either way.
function draftBox(key, step) {
  const text = step.draft;
  const long = text.split("\n").length > DRAFT_FOLD_LINES || text.length > DRAFT_FOLD_CHARS;
  const openKey = `${key}:draft-open`;
  const folded = long && !ui.open.has(openKey);
  const words = text.split(/\s+/).filter(Boolean).length;
  return h("div", { class: `draft${folded ? " folded" : ""}`, role: "group", "aria-label": "Draft" },
    iconButton("copy", "Copy draft", {
      class: "button small ghost draft-copy",
      onclick: (event) => copy(text, event.currentTarget, step.draft_html),
    }),
    pageComments(prose(`${key}:draft`, "prose", step.draft_html), step),
    long && h("button", {
      class: "link-button draft-fold", type: "button", "aria-expanded": String(!folded),
      onclick: () => { if (folded) ui.open.add(openKey); else ui.open.delete(openKey); render(false); },
    }, folded ? `Show whole draft (${words} words)` : "Show less"));
}

// group: "next" (the Next up card), or the list of an expanded row: "queue", "ready" or "snoozed".
function stepBody(step, state, group) {
  const isNext = group === "next";
  // "working": the step the user handed to an agent, on the sidebar's card while the agent works on it. It keeps the
  // step's text and links, with no answers, keys or menu, and ends in what the agent is doing (`workingNote`).
  const working = group === "working";
  // The step's own controls: none on a step that left the lists, nor on one an agent holds.
  const live = !step.left && !working;
  const item = step.item;
  const key = `step:${step.id}`;
  const replyKey = `msg:step:${step.id}`;
  const body = step.body_html ? labelledBody(`${key}:body`, step.body_html) : null;
  if (body) pageComments(body, step);
  // Next up shows the After line whole, so it needs no tooltip of the same words.
  if (isNext && body) for (const after of body.querySelectorAll(".label-after")) after.removeAttribute("title");
  // The Ready paragraph becomes a line in More; the draft below shows what is ready.
  const readyParagraph = body && body.querySelector(":scope > .label-ready");
  const ready = readyParagraph ? labelText(readyParagraph) : "";
  if (readyParagraph) readyParagraph.remove();
  // The user's message the body quotes in full ("You wrote …:"), which the history then leaves out; a quote cut
  // short leaves the history's line, which holds the whole message.
  const quoteLabel = body && [...body.querySelectorAll(":scope > p > strong:first-child")]
    .find((label) => label.textContent.startsWith("You wrote"));
  const quoted = quoteLabel ? quoteLabel.parentElement.textContent.slice(quoteLabel.textContent.length).trim() : null;
  const tags = [
    earlyChip(step),
    fireAndForgetChip(item),
    blocksChip(item),
  ].filter(Boolean);
  // The header keeps the title, with the Cockpit mark at its end for Cockpit's own work, and the timer; the priority,
  // when the step was checked and why it ranks where it does are in the ⋯ menu.
  const about = aboutStep(step, group, isNext && isTop(step, state) ? checkWords(step, state) : null, ready);
  // A snoozed step the user picked in the sidebar is the card, with no queue row above it to show its ask.
  // The card heads with the task's name and the ask as its short line under it (NEXT-49, UI-68).
  const headed = isNext || (SIDEBAR && (group === "snoozed" || working));
  // The task's title ends in its marks, each a small picture: Cockpit's key on Cockpit's own work, the
  // pencil while the reply box holds an unsent draft. The title is the card's heading, or the head's line on a row.
  const titleMarks = [cockpitChip(item.project), !step.left && draftMark(step, isNext)];
  const ask = headed && [
    // The heading carries no New dot: the card on top is the one to read either way (NEXT-49).
    h("h1", { class: "task-heading" }, quickMark(step), h("span", { class: "task-heading-text", text: item.title }), titleMarks),
    shortLine(step),
  ];
  // A page linked from the short line opens for the step too, beside its card (NEXT-38, NEXT-47).
  if (ask) pageComments(ask[1], step);
  const closeKey = `close:${step.id}`;
  const closeOpen = ui.expanded.has(closeKey);
  const secretKey = `secret:${step.id}`;
  const secretOpen = ui.expanded.has(secretKey);
  const linkHtml = [step.ask_html, step.why_html, step.body_html, step.draft_html].filter(Boolean).join("");
  const full = stepItem(step, state);
  const target = step.target
    ? sessionLabel(sessionMap(state).get(step.target.session_id) || step.target, "")
    : ["a new session", repoOf(newSessionWhere(step).new_cwd)].filter(Boolean).join(" in ");
  const ending = doneSession(step, state);
  const ends = ending !== null && ending.others === 0;
  // Archive is the main button only when the step's session reports its work done and this is its
  // last open step (SESSION-4); Run or Allow, when the step has one, stays the main button.
  const archiveFirst = ends && ending.finished && !step.command && !(step.permission && !step.permission_allowed_at);
  // On the item's last open step the key is Task done, which completes the task in one press (CONTROL-15).
  const completes = finishesItem(step, state);
  // A step shows all of its content at once; opening it starts its timer (UI-19, TRACK-2, `startOpened`).
  const [strip, otherImages] = full ? stepImages(key, full, isNext ? "large" : "small", step) : [null, null];
  const content = [
    historyFold(item, state, step.id, quoted, body ? [...body.querySelectorAll("blockquote")].filter((quote) => !quote.closest("details")).map((quote) => quote.textContent) : []),
    // Steps from before the short line carry a why; it opens the content rather than sitting in the head (NEXT-49).
    snoozeUnreadNote(step, state),
    step.why_html && pageComments(h("p", { class: "why", title: isNext ? null : plainText(step.why_html), html: step.why_html }), step),
    body && body.children.length > 0 && body,
    strip,
    detailList(key, step.details, step, otherImages),
    step.draft && draftBox(key, step),
  ].filter(Boolean);
  const reasons = reasonChips(step);
  // Task done stands with the step's answers, as one more answer that completes the task (NEXT-53). Beside a closing
  // answer it would do the same thing, so it hides until the reply box holds a last message, when it is the one key
  // that completes the task with those words (CONTROL-31, `showDraftMarks`).
  const taskDoneKey = completes && h("button", {
    class: "button answer closes task-done", type: "button",
    hidden: closingAnswer(step) && !(ui.drafts.get(replyKey) || "").trim(),
    "data-key": isNext ? "E" : null, "data-done-step": step.id, "data-beside-closer": closingAnswer(step) ? "" : null,
    "data-no-session": step.target ? null : "",
    title: step.target ? "Closes the task; your typed words go as a last message" : "Closes the task; it has no session to take typed words",
    onclick: (event) => taskDone(step, replyKey, event.currentTarget),
  }, icon("done"), h("span", { class: "answer-words label", text: taskDoneWords(replyKey, Boolean(step.target)) }));
  // A list of tasks to close is the card's one close key (CONTROL-23), so the card shows no Done or Archive beside it.
  const doneKey = completes || step.closes ? null
    // A square check closes only this step; a key that ends more says so in words (SESSION-4): Task done above, Archive
    // session here.
    : ends
      ? h("button", {
        class: archiveFirst ? "button primary archive-key" : "button archive-key", type: "button",
        "data-key": isNext ? "E" : null, onclick: () => act(step, "done"),
      }, icon("archive"), h("span", { class: "label", text: "Archive session" }))
      : iconButton("check", "Done", { class: "button", "data-key": isNext ? "E" : null, onclick: () => act(step, "done") });
  return [
    expiryBanner(step),
    h("div", { class: "step-context" },
      !headed && h("span", { class: "title-line" }, h("span", { class: "item-title", title: item.title, text: item.title }), titleMarks),
      stepOf(step, state),
      h("span", { class: "context-tags" }, tags),
      h("span", { class: "context-marks" }, isNext && updatedMark(step, true),
        live && stepMenu(step, state, { group, about, full, closeOpen, secretKey, secretOpen }))),
    ask,
    changedBanner(step, full),
    mergedBanner(full),
    isNext && step.left && leftNote(step),
    reasons.length > 0 && h("div", { class: "chips" }, reasons),
    // Run and Allow, with what the command does and its last run, come right under the ask, above the
    // step's history and body (NEXT-44): the press the step asks for is the first thing on the card.
    live && step.command && commandBlock(step, target),
    live && step.permission && permissionBlock(step, target),
    live && step.signin && !step.signin.finished_at && signinBlock(step),
    lastRun(key, step),
    content,
    // The links the step is about, and behind Other the task's others, sit under its text, above its answers.
    coreLinks(key, step, full, linkHtml),
    live && step.closes && closesBlock(step, state),
    // A step that left the lists keeps its texts and reply box; there is nothing left to act on. The
    // one-press answers lead the action bar on a row of their own, so on Next up the bar that rides the
    // screen's bottom carries them and never covers them. The done key makes the row under them; every other
    // control sits in the ⋯ menu at the head's right (VIEW-29).
    live && (step.answers.length > 0 || taskDoneKey || doneKey) && h("div", { class: "actions" },
      (step.answers.length > 0 || taskDoneKey) && answerButtons(step, target, taskDoneKey),
      doneKey && h("div", { class: "step-tail" }, h("span", { class: "step-keys" }, doneKey))),
    live && closeOpen && closeBox(step),
    // The task's details show once the user asks for them from the menu, and the fold's own summary hides them again.
    live && full && ui.open.has(`task:${step.id}`) && taskFold(full, step, state),
    askBlock(item.id, step.id, state),
    live && secretOpen && secretBox(step, replyKey),
    working && workingNote(step, state),
    composer(replyKey, {
      session: step.target,
      context: { item_id: item.id, step_id: step.id },
      project: item.project,
      step,
      handOff: live,
      toSession: true,
    }),
  ];
}

// The step's one menu, the ⋯ at the right of its head (VIEW-29). The head keeps the item's title, its tags and the
// time; the menu holds the rest in groups: the step's sessions, each opening its conversation, Snooze and Priority; the
// step's time; the two writing marks (FEEDBACK-8); the task's details; what the step was checked against, then Dismiss,
// Close task and Give a secret. The task's links are on the card, under the step's text (`coreLinks`). Snooze and
// Priority open their choices inside the menu and turn their chevron, so pressing either only shows more choices; every
// other entry acts and closes the menu. `about`: the lines from `aboutStep`; `full`: the step's item as `stepItem`
// finds it, or null.
// Where each open step menu's list was scrolled, so a redraw keeps a phone's sheet where the user left it.
const stepMenuScroll = new Map();

function stepMenu(step, state, { group, about, full, closeOpen, secretKey, secretOpen }) {
  const key = `menu:step:${step.id}`;
  const snoozeKey = `${key}:snooze`;
  const isNext = group === "next";
  const el = h("details", { class: "menu step-menu" });
  el.open = ui.open.has(key);
  const forget = () => { ui.open.delete(key); ui.open.delete(snoozeKey); ui.open.delete(`${key}:priority`); };
  rememberOpen(el, (open) => { if (open) ui.open.add(key); else forget(); });
  const close = () => {
    forget();
    el.open = false;
    el.querySelector(":scope > summary")?.focus({ preventScroll: true });
  };
  // A list taller than the room below the ⋯ scrolls inside, so its last entries stay in reach; a phone's sheet has a
  // height of its own (style.css). Set through the style object, as the page's CSP refuses style attributes.
  el.addEventListener("toggle", () => {
    const list = el.querySelector(".menu-list");
    list.style.maxHeight = "";
    // A menu closed without a redraw opens again with Snooze's and Priority's choices folded, as `forget` dropped
    // their keys.
    if (!el.open) {
      for (const choices of el.querySelectorAll(".menu-choices")) choices.setAttribute("hidden", "");
      for (const opener of el.querySelectorAll(".menu-opens")) opener.setAttribute("aria-expanded", "false");
      stepMenuScroll.delete(key);
    }
    if (!el.open || !list.offsetParent || getComputedStyle(list).position === "fixed") return;
    list.style.maxHeight = `${Math.max(window.innerHeight - list.getBoundingClientRect().top - 12, 200)}px`;
  });
  // A redraw opens the new list where the old one was scrolled; set on the toggle a menu drawn open fires.
  el.addEventListener("toggle", () => { if (el.open) el.querySelector(".menu-list").scrollTop = stepMenuScroll.get(key) || 0; });
  el.addEventListener("toggle", () => { if (el.open) loadActivityNotes(el); });
  // Up and Down move through the entries the menu shows, Home and End go to its ends (WAI-ARIA menu pattern);
  // Escape, which closes it, is the page's (keyboard.js).
  el.addEventListener("keydown", (event) => {
    if (!el.open || !["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    const entries = [...el.querySelectorAll(".menu-list [role^=menuitem]")].filter((entry) => entry.getClientRects().length);
    if (entries.length === 0) return;
    event.preventDefault();
    event.stopPropagation();
    const at = entries.indexOf(document.activeElement);
    const next = event.key === "Home" ? 0 : event.key === "End" ? entries.length - 1
      : event.key === "ArrowDown" ? (at + 1) % entries.length : (at <= 0 ? entries.length : at) - 1;
    entries[next].focus();
  });
  const entry = (iconName, words, hint, onclick) => h("button", { type: "button", role: "menuitem", onclick: () => { close(); onclick(); } },
    h("span", { class: "menu-label" }, icon(iconName), words), hint && h("span", { class: "when", text: hint }));

  // The step's own session first, then the task's others; a session the page cannot open is a line of text.
  const own = step.target && step.target.session_id;
  // A task closed as not needed lists no sessions.
  const listed = full && full.step_sessions ? itemSessions(full) : [];
  const ids = [...new Set([own, ...listed].filter(Boolean))];
  const known = sessionMap(state);
  const sessions = ids.map((id) => {
    const session = known.get(id) || (id === own ? step.target : { session_id: id });
    const doing = capitalize((session.state && AGENT_DOING[sessionState(session)]) || (session.alive ? "live" : "stopped"));
    const small = h("small", { text: doing });
    const words = [sessionMark(session), h("span", { class: "menu-two" }, h("span", { class: "menu-name", text: sessionLabel(session, "") }), small)];
    const opens = itermLink(session, { class: "menu-session", role: "menuitem" }, words);
    if (!opens) return h("p", { class: "menu-note menu-session" }, words);
    small.textContent = `${doing} · ${opens.tagName === "A" ? "open it in iTerm2" : "open the conversation"}`;
    opens.addEventListener("click", close);
    return opens;
  });

  let snooze;
  if (group === "snoozed") {
    snooze = [entry("clock", "Unsnooze", null, () => act(step, "snooze", { until: null }))];
  } else {
    const shown = ui.open.has(snoozeKey);
    const choices = h("div", { class: "menu-choices", role: "group", "aria-label": "Snooze for", hidden: !shown },
      snoozeTimes().map(([words, date], index) => h("button", {
        type: "button", role: "menuitem", title: `Until ${when(date)}`,
        // Counted from the press, not from when the menu was drawn.
        onclick: () => { close(); act(step, "snooze", { until: isoUtc(snoozeTimes()[index][1]) }); },
      }, h("span", { class: "menu-label", text: words }),
      h("span", { class: "when" }, isNext && words === "2 hours" && h("kbd", { class: "menu-key", text: "Z" }), when(date)))),
      h("button", { type: "button", role: "menuitem", onclick: () => { close(); openSnoozeWords(step.item.title, null, "", (until, words) => snoozeStepInWords(step, until, words)); } },
        h("span", { class: "menu-label", text: "Other time…" })));
    const opener = h("button", {
      type: "button", class: "menu-opens", role: "menuitem", "aria-expanded": String(shown),
      onclick: () => {
        const open = choices.hidden;
        choices.hidden = !open;
        opener.setAttribute("aria-expanded", String(open));
        if (open) ui.open.add(snoozeKey); else ui.open.delete(snoozeKey);
      },
    }, h("span", { class: "menu-label" }, icon("clock"), "Snooze"), h("span", { class: "when" }, icon("chevron-right")));
    snooze = [opener, choices];
  }

  // The item's priority (CONTROL-7), moved here from the head: it opens its four levels inside the menu, as Snooze
  // does, the current one checked.
  const item = step.item;
  const priorityKey = `${key}:priority`;
  let priority = [];
  if (item && !step.left) {
    const shown = ui.open.has(priorityKey);
    const levels = h("div", { class: "menu-choices", role: "group", "aria-label": "Priority", hidden: !shown },
      ["urgent", "high", "normal", "low"].map((level) => {
        const on = item.priority === level;
        return h("button", {
          type: "button", role: "menuitemradio", "aria-checked": String(on),
          onclick: () => { close(); if (!on) setPriority(item, level); },
        }, h("span", { class: "menu-label" }, icon(PRIORITY_ICONS[level]), capitalize(level)), on && h("span", { class: "when" }, icon("check")));
      }));
    const opener = h("button", {
      type: "button", class: "menu-opens", role: "menuitem", "aria-expanded": String(shown), "data-priority": item.priority,
      onclick: () => {
        const open = levels.hidden;
        levels.hidden = !open;
        opener.setAttribute("aria-expanded", String(open));
        if (open) ui.open.add(priorityKey); else ui.open.delete(priorityKey);
      },
    }, h("span", { class: "menu-label" }, icon(PRIORITY_ICONS[item.priority]), "Priority"),
    h("span", { class: "when" }, capitalize(item.priority), icon("chevron-right")));
    priority = [opener, levels];
  }

  // The verdict the step's text holds shows checked; the other replaces it, and choosing it again
  // clears it.
  const mark = (verdict, iconName, words, tip) => {
    const on = step.writing_verdict === verdict;
    return h("button", {
      type: "button", role: "menuitemcheckbox", "aria-checked": String(on),
      class: `writing-${verdict}${on && step.writing_pending ? " writing-pending" : ""}`,
      title: on ? "Choose again to clear the mark" : tip,
      onclick: () => { close(); if (on) clearStepWriting(step); else markStepWriting(step, verdict); },
    }, h("span", { class: "menu-label" }, icon(iconName), words), on && h("span", { class: "when" }, icon("check")));
  };

  const taskKey = `task:${step.id}`;
  const task = full && entry("info", ui.open.has(taskKey) ? "Hide task details" : "Task details", null, () => {
    if (ui.open.has(taskKey)) ui.open.delete(taskKey); else ui.open.add(taskKey);
    render(false);
    if (ui.open.has(taskKey)) document.querySelector(`[data-step="${step.id}"] .task-details`)?.scrollIntoView({ block: "nearest" });
  });
  const groups = [
    [...sessions, ...snooze, ...priority],
    timeEntries(step, full, group === "snoozed", close),
    [mark("good", "writing-good", "Good writing", null),
      mark("bad", "writing-bad", "Hard to understand", "Keep this step as an example of writing to avoid")],
    [task],
    [...about.map((line) => h("p", { class: "menu-note", text: line })),
      // Dismiss closes only this step; Close task closes the whole task and asks why (CONTROL-15).
      entry("x", "Dismiss this step: not needed", isNext ? "X" : null, () => act(step, "dismiss")),
      entry("archive", closeOpen ? "Cancel closing the task" : "Close task…", null, () => toggle(`close:${step.id}`)),
      entry("key", secretOpen ? "Cancel the secret" : "Give a secret…", null, () => openSecret(secretKey))],
  ].map((members) => members.filter(Boolean)).filter((members) => members.length > 0);
  const menuName = step.timer && step.timer.started_at ? "Timing this task; session, Snooze and more" : "Session, Snooze and more";
  el.append(
    // Back from a session opened here puts the focus on ⋯, as the menu has closed (`closeSessionView`).
    // While the step's timer runs, ⋯'s label says so (VIEW-29): the menu holds the step's time. It carries no dot.
    h("summary", { class: "button icon-only step-menu-key", title: menuName, "aria-label": menuName,
      "data-sessions": ids.join(" ") || null }, icon("more")),
    h("div", { class: "menu-list", role: "menu", "aria-label": "Session, Snooze and more",
      onscroll: (event) => stepMenuScroll.set(key, event.currentTarget.scrollTop) },
      groups.map((members) => h("div", { class: "menu-group", role: "none" }, members))));
  return el;
}

// "Other time…" under Snooze (VIEW-29): the user types when the step should come back in their own words, "friday
// morning" or "just before standup". The snooze happens at once, until SNOOZE_READ_MINUTES from now; Cockpit then
// reads the words with the coming week of the user's calendar (snooze_words.py) and moves the snooze to that time, or
// brings the step back with the reason (`noticeSnoozeReads`).
// `title` names what snoozes; `note`, or null, says more under it; `words` fills the field ("" for none); `snooze`
// takes the stand-in time and the words with the user's time zone.
function openSnoozeWords(title, note, words, snooze) {
  const field = h("input", { class: "field time-field snooze-words", type: "text", autocomplete: "off", spellcheck: "true",
    maxlength: "200", "data-key": "snooze:words", "aria-label": "When it comes back", "aria-describedby": "time-hint",
    placeholder: "friday morning, in 40 minutes", value: words });
  const hint = h("p", { class: "time-hint", id: "time-hint", "aria-live": "polite", text: "Type a day, a time or both, in your own words" });
  const form = h("form", { class: "time-form", onsubmit: (event) => {
    event.preventDefault();
    const text = field.value.trim();
    if (!text) {
      hint.textContent = "Type when it should come back first";
      hint.classList.add("refused");
      field.focus();
      return;
    }
    timeDialog().close();
    snooze(isoUtc(new Date(Date.now() + SNOOZE_READ_MINUTES * 60000)), { text, tz: Intl.DateTimeFormat().resolvedOptions().timeZone });
  } },
  h("h2", { id: "time-dialog-title", text: "Snooze until" }),
  h("p", { class: "time-item", text: title }),
  note && h("p", { class: "time-note", text: note }),
  h("div", { class: "time-well" }, field),
  hint,
  h("div", { class: "time-actions" }, h("button", { class: "button primary", type: "submit", text: "Snooze" }),
    h("button", { class: "button ghost", type: "button", onclick: () => timeDialog().close() }, "Cancel")));
  showTimeDialog("snooze", form, field);
}

// How long a snooze in words lasts while Cockpit reads the words.
const SNOOZE_READ_MINUTES = 30;

// Snoozes typed in words this page sent, by target ("step:ID", "warning:KEY"): the words, and the id of the read the
// target had before, so the page tells the outcome of its own read only, once. Acting on the target again drops it.
const wordReads = new Map();

function snoozeStepInWords(step, until, words) {
  noteWordRead(`step:${step.id}`, words);
  return act(step, "snooze", { until, words });
}

function noteWordRead(target, words) {
  const before = (ui.state.snooze_reads || {})[target];
  wordReads.set(target, { words: words.text, before: before ? before.id : null });
}

// Tells how Cockpit read the words of a snooze this page sent, once the read is done: the time it moved the snooze
// to, with Undo, or the reason the step or warning is back, with Other time… to type the words again.
function noticeSnoozeReads(state) {
  const reads = state.snooze_reads || {};
  for (const [target, sent] of wordReads) {
    const entry = reads[target];
    if (!entry || entry.id === sent.before || entry.words !== sent.words || entry.status === "reading") continue;
    wordReads.delete(target);
    const [kind, name] = [target.slice(0, target.indexOf(":")), target.slice(target.indexOf(":") + 1)];
    const step = kind === "step" ? STEP_LISTS.flatMap((list) => state[list]).find((other) => String(other.id) === name) : null;
    const warning = kind === "warning" ? allWarnings(state).find((other) => other.key === name) : null;
    const tip = `You typed “${entry.words}”`;
    if (entry.status === "read") {
      const undo = step ? () => act(step, "snooze", { until: null }) : warning ? () => setWarningSnooze(warning, null, null) : null;
      toast(`Snoozed until ${when(new Date(entry.until))}`, false, undo && { label: "Undo", run: undo }, tip);
    } else {
      const again = step ? () => openSnoozeWords(step.item.title, null, entry.words, (until, words) => snoozeStepInWords(step, until, words))
        : warning ? () => openWarningSnoozeWords(warning, entry.words) : null;
      toast(`Not snoozed: ${entry.reason}`, false, again && { label: "Other time…", run: again }, tip);
    }
  }
}

// The reason a step snoozed in words is back, on its card until another snooze, or null.
function snoozeUnreadNote(step, state) {
  const entry = (state.snooze_reads || {})[`step:${step.id}`];
  if (!entry || entry.status !== "unread" || step.snoozed_until) return null;
  return h("p", { class: "snooze-unread", title: `You typed “${entry.words}”` }, icon("clock"), `Not snoozed: ${entry.reason}`);
}

// Whether Cockpit is still reading the words the step or warning was snoozed in.
function readingWords(state, target) {
  const entry = (state.snooze_reads || {})[target];
  return Boolean(entry) && entry.status === "reading";
}

// Task done on an item's last open step: one press completes the task, and what the user typed in the step's reply
// box goes with it to the session as their last message, with the images staged in the box. With no session to
// tell, the text stays in the box and nothing closes; Undo or a refused finish puts the words and images back
// (`finishItem`), ahead of anything added since. Images go only with words (SPEC MSG-18): with none typed they stay in
// the box. While a finish of the item is on its way, a second press does nothing.
function taskDone(step, replyKey, pressed) {
  const note = (ui.drafts.get(replyKey) || "").trim();
  if (!note) return finishItem(step, "complete", null, "", [], "", null, pressed);
  if (!step.target) {
    toast("No session to send your last message to", true, null, "Clear the reply box to complete the task without it.");
    return Promise.resolve(false);
  }
  if (ui.busy.has(`finish:${step.item_id}`)) return Promise.resolve(false);
  takeDraft(replyKey);
  return finishItem(step, "complete", null, note, takeStaged(replyKey), "", { key: replyKey, text: note }, pressed);
}

// The step's first closing answer that Cockpit did not write as an action, or undefined. On the item's last step it
// stands in for Task done while the reply box is empty (CONTROL-31).
function closingAnswer(step) {
  return step.answers.find((answer) => answer.closes && !answer.action);
}

// The Task done key's words: "Task done", or "Task done + send" while the reply box holds a last message and the step
// has a session to send it to (`hasSession`). Without one the press sends nothing (`taskDone`), so the words stay.
function taskDoneWords(replyKey, hasSession) {
  return hasSession && (ui.drafts.get(replyKey) || "").trim() ? "Task done + send" : "Task done";
}

// One-click answers (NEXT-21): each hands the step to its session with that reply. An answer shows its whole words,
// over two lines at most, with no hover: the card moving away and the notice with Undo show what it did. A closing
// answer (CONTROL-15) wears the completion colour and its mark: it sends the reply and completes the task, as Task
// done does. An answer Cockpit wrote with an `action` (a move step's, SESSION-18) does that action on the page itself
// and goes to no session. A closing answer works on a step with no session too: it completes the task, and there is
// nobody to tell. Any other answer on a step with no session starts a new session with the step and the answer, as
// Send does, and carries the session bot to say so. `taskDone`: the Task done key, last among the answers, or null.
function answerButtons(step, target, taskDone) {
  return h("div", { class: "answers", role: "group", "aria-label": "Answers" },
    step.answers.map(({ text, closes, action }) => h("button", {
      class: `button answer${closes ? " closes" : ""}`, type: "button",
      "aria-label": closes ? `${text} (completes the task)` : action || step.target ? text : `${text} (starts a new session)`,
      title: action || closes || step.target ? null : `${text}\nStarts a new session`,
      onclick: (event) => (action
        ? answerAction(step, action)
        : closes
          ? finishItem(step, "complete", text, "", [], "", null, event.currentTarget)
          : handOffStep(step, text, step.target ? { session_id: step.target.session_id } : newSessionWhere(step), [], target,
            flightStart(step, event.currentTarget), null, null)),
    }, closes && icon("done"), !action && !closes && !step.target && sessionBot(), h("span", { class: "answer-words", text }))),
  taskDone || null);
}

// A move Cockpit recommends (SESSION-18): the session stays where it is until the user answers "Yes, move it",
// which stops it on its account and continues it on the other; the move closes the step. "No, let it wait"
// dismisses the step.
const MOVE_NEED = /^move:([0-9a-f-]{36}):([a-z]+):/;

function answerAction(step, action) {
  if (action === "dismiss") return act(step, "dismiss");
  const [, sessionId, account] = step.need.match(MOVE_NEED);
  return run(`move:${sessionId}`, async () => {
    const moved = await api("POST", `/api/sessions/${sessionId}/move`, { account });
    toast(moved.detail, false, null, "");
  });
}

// A command only the user may run (NEXT-18), in full: Run sends the server the command shown here, and the server
// runs only that. What it does, part by part, comes first in the session's words (NEXT-40).
function commandBlock(step, target) {
  const { run: command, cwd, explain_html: explained } = step.command;
  return h("div", { class: "step-run", role: "group", "aria-label": "Command" },
    explained && prose(`run:${step.id}`, "prose step-run-explain", explained),
    h("div", { class: "step-run-head" },
      h("span", { text: `Runs as you in ${cwd}` }),
      iconButton("copy", "Copy command", { class: "button small ghost", onclick: (event) => copy(command, event.currentTarget, null) })),
    h("pre", { class: "step-run-text" }, shellCall(command)),
    h("button", {
      class: "button primary", type: "button",
      title: `Run: Cockpit runs this as you in ${cwd} (bash, stopped after 30 minutes), ${step.target ? "hands the step to" : "starts a new session with the step,"} ${target} and sends it the output.${step.target ? "" : "\nStarts a new session"}`,
      onclick: (event) => runCommand(step, target, event.currentTarget),
    }, icon("play"), h("span", { class: "label", text: "Run" }), !step.target && sessionBot()));
}

const PERMISSION_EFFECTS = {
  allow: "Adds a tool rule to your settings: matching calls run without asking.",
  auto: "Adds an auto-mode sentence to your settings: auto mode allows the calls it describes.",
};

// A permission the step's session needs (NEXT-18): Allow adds it and tells the session to go ahead. Once
// pressed, the block says when, in place of the button (NEXT-41).
function permissionBlock(step, target) {
  const { list, rule } = step.permission;
  const allowed = step.permission_allowed_at;
  if (allowed) {
    return h("div", { class: "step-run", role: "group", "aria-label": "Permission" },
      h("div", { class: "step-run-head" }, icon("check"),
        h("span", {}, "You allowed this ", h("span", { "data-time": allowed, "data-ago": allowed, text: ago(allowed) }),
          `; Cockpit added the ${RULE_LISTS[list].name} to your settings then.`)),
      h(list === "allow" ? "pre" : "p", { class: "step-run-text", text: rule }));
  }
  return h("div", { class: "step-run", role: "group", "aria-label": "Permission" },
    h("div", { class: "step-run-head" }, icon("shield"), h("span", { text: PERMISSION_EFFECTS[list] })),
    h(list === "allow" ? "pre" : "p", { class: "step-run-text", text: rule }),
    h("button", {
      class: "button primary", type: "button",
      title: step.target ? null : "Starts a new session",
      onclick: (event) => allowPermission(step, target, event.currentTarget),
    }, icon("check"), h("span", { class: "label", text: "Allow" }), !step.target && sessionBot()));
}

// A connector sign-in (SESSION-25): the link to the connector's sign-in page, then a field for the address the browser
// lands on after the user approves, which Cockpit turns into the account's sign-in. The address works once, so it
// stays in this page's memory only (`signinFields`) and goes once (`apiCredential`). The link leads while the field
// is empty; Finish sign-in leads once it holds an address. What went wrong with the last address shows above the field.
const signinFields = new Map();

function connectorLabel(server) {
  return server.charAt(0).toUpperCase() + server.slice(1);
}

function signinBlock(step) {
  const { server, account, host, link, failure } = step.signin;
  const key = `signin:${step.id}`;
  const sending = ui.busy.has(key);
  const label = connectorLabel(server);
  const open = h("a", { class: "button", href: link, target: "_blank", rel: "noopener noreferrer" },
    icon("key"), h("span", { class: "label", text: "Open sign-in link" }));
  const finish = h("button", { class: "button", type: "submit", disabled: sending },
    icon("check"), h("span", { class: "label", text: sending ? "Finishing…" : "Finish sign-in" }));
  const field = h("input", {
    // A credential the user can read as they paste it, which the feedback picker and a GPU close leave out (pick.js).
    class: "field signin-address", type: "text", "data-credential": true, "data-key": key, autocomplete: "off",
    spellcheck: "false", readonly: sending, placeholder: "http://localhost:…/callback?code=…",
    "aria-label": "The address your browser landed on",
  });
  field.value = signinFields.get(key) || "";
  const lead = () => {
    const filled = Boolean(field.value.trim());
    open.classList.toggle("primary", !filled);
    finish.classList.toggle("primary", filled);
  };
  lead();
  field.addEventListener("input", () => { signinFields.set(key, field.value); lead(); });
  const form = h("form", { class: "signin-form", onsubmit: (event) => { event.preventDefault(); finishSignin(step, field); } },
    h("div", { class: "signin-row" }, field, finish));
  return h("div", { class: "step-run signin-block", role: "group", "aria-label": `Sign in to ${label}` },
    h("div", { class: "step-run-head" }, icon("key"),
      h("span", { text: `Signs the ${account} Claude account in to ${label}, through ${host}` })),
    h("div", { class: "signin-open" }, open),
    failure && h("p", { class: "signin-failure fail-text", role: "alert" }, icon("alert"), h("span", { text: failure })),
    form);
}

// Cockpit finishes the sign-in and hands the step to its session, or answers what went wrong, which the step then
// shows; a new link means the old address is spent, so the field empties.
function finishSignin(step, field) {
  const key = `signin:${step.id}`;
  const address = (signinFields.get(key) || "").trim();
  if (!address) { field.focus(); toast("Paste the address your browser landed on first", true); return; }
  const { link, server } = step.signin;
  const finishing = run(key, async () => {
    let done;
    try {
      done = await apiCredential(`/api/steps/${step.id}/signin`, { address });
    } catch (error) {
      throw refused("Not signed in", error);
    }
    if (done.failure) {
      if (done.step.signin.link !== link) signinFields.delete(key);
      return;
    }
    signinFields.delete(key);
    takenByUser(step.id);
    toast(`Signed in to ${connectorLabel(server)}: ${plural(done.step.signin.tools, "tool", "tools")}; the session goes on`);
  }, null);
  safeRender();
  return finishing;
}

// The step's last finished command run, folded: how it ended and its output.
function lastRun(key, step) {
  const run = step.run;
  if (!run || !run.finished_at) return null;
  return disclosure(`${key}:run`, "step-run-last",
    [`Last run: ${runWords(run).toLowerCase()}, `, h("span", { "data-time": run.finished_at, text: age(run.finished_at) })],
    h("pre", { class: "step-run-text", text: run.output || "It printed nothing." }), false);
}

// A figure in a step (SPEC NEXT-17) is a shared page in a sandboxed frame or an item image. The page reports its
// height (app.FIGURE_SIZE_SCRIPT) and its frame takes it, kept per address, so a redraw draws the frame at its
// height at once. Only a frame of this page's figures may set its height. A page taller than a frame may be shows
// as a link in every step that draws it (NEXT-57), remembered per address, so a redraw does not load it again.
const FIGURE_HEIGHT = { min: 80, max: 900 };

window.addEventListener("message", (event) => {
  const height = event.data && event.data.cockpitFigure;
  if (typeof height !== "number" || !Number.isFinite(height)) return;
  const frames = [...document.querySelectorAll("iframe.figure-frame")];
  const frame = frames.find((el) => el.contentWindow === event.source);
  if (!frame) return;
  const address = figureAddress(frame);
  if (height > FIGURE_HEIGHT.max) {
    ui.figureLinked.add(address);
    for (const each of frames) if (figureAddress(each) === address) linkFigure(each);
    return;
  }
  ui.figureHeights.set(address, Math.max(FIGURE_HEIGHT.min, Math.ceil(height)));
  fitFigure(frame);
});

// A frame drawn by `prose` holds its address in data-src until `placeFigures` gives it one. The address names
// the page's revision, so a frame kept through redraws loads again once the page changes or is removed.
function figureAddress(frame) {
  return frame.dataset.src || frame.getAttribute("src");
}

function fitFigure(frame) {
  const height = ui.figureHeights.get(figureAddress(frame));
  if (height) frame.style.height = `${height}px`;
}

// A frame drawn anew loads its page anew, so a chart would go blank on each redraw. Before a section is
// redrawn, its figure frames move aside, still loaded (moveBefore keeps a frame's page as it is), and after the
// redraw each takes the place of a new frame for the same page; a new frame with none to take its place loads.
const figurePark = h("div", { hidden: true });

function parkFigures(ids) {
  if (!figurePark.isConnected) document.body.append(figurePark);
  if (!figurePark.moveBefore) return;
  for (const id of ids) {
    for (const frame of document.getElementById(id).querySelectorAll("iframe.figure-frame")) figurePark.moveBefore(frame, null);
  }
}

function placeFigures() {
  const parked = new Map();
  // A frame loaded ahead for a step about to show (`warmNext`) serves too, once its frames are parked and kept.
  const kept = figurePark.moveBefore ? [...figurePark.children, ...figureWarm.querySelectorAll("iframe.figure-frame")] : [];
  for (const frame of kept) parked.set(figureAddress(frame), [...(parked.get(figureAddress(frame)) || []), frame]);
  for (const frame of document.querySelectorAll("iframe.figure-frame[data-src]")) {
    const kept = (parked.get(frame.dataset.src) || []).shift();
    if (kept) {
      frame.parentNode.moveBefore(kept, frame);
      // The kept frame now shows the page for the new frame's step, so the page's choices go to that step.
      if (frame.dataset.step) kept.dataset.step = frame.dataset.step;
      else delete kept.dataset.step;
      frame.remove();
    } else {
      Loading.frame(frame);
      frame.src = frame.dataset.src;
      delete frame.dataset.src;
    }
  }
  figurePark.replaceChildren();
}

// The steps that may take Next up's card next, the first few of the queue the card does not show, load ahead
// (UI-16): their histories, figure images and figure pages. The card that takes over after a Send or a close then
// draws at its full height at once, rather than growing as each part arrives and pushing the rest down. A figure
// page loads in a frame out of sight, in a paragraph as wide as one on the card, so it reports the height it will
// have there before its card shows, and that frame then moves onto the card (`placeFigures`).
const WARM_STEPS = 3;
const figureWarm = h("div", { class: "prose figure-warm", "aria-hidden": "true" });
let warmImages = new Map();

function warmNext(state) {
  const onCard = new Set([...document.querySelectorAll("#next [data-step]")].map((el) => Number(el.dataset.step)));
  const byId = new Map(state.queue.map((step) => [step.id, step]));
  const steps = ui.order.filter((id) => byId.has(id) && !onCard.has(id)).slice(0, WARM_STEPS).map((id) => byId.get(id));
  const images = new Map();
  const frames = new Map();
  for (const step of steps) {
    warmHistory(step.item_id, state);
    for (const id of figureImages(step)) {
      const image = warmImages.get(id) || new Image();
      if (!image.src) image.src = `/item-images/${id}`;
      images.set(id, image);
    }
    const html = [step.body_html, ...step.details.map((detail) => detail.body_html)].join("");
    if (!html.includes("figure-frame")) continue;
    const holder = document.createElement("template");
    holder.innerHTML = html;
    for (const frame of holder.content.querySelectorAll("iframe.figure-frame")) frames.set(frame.getAttribute("src"), frame);
  }
  warmImages = images;
  if (!figureWarm.isConnected) document.body.append(figureWarm);
  const card = document.querySelector("#next .prose");
  figureWarm.style.width = `${card ? card.clientWidth : document.getElementById("next").clientWidth}px`;
  for (const holder of [...figureWarm.children]) {
    const frame = holder.querySelector("iframe.figure-frame");
    if (frame && frames.has(figureAddress(frame))) frames.delete(figureAddress(frame));
    else holder.remove();
  }
  const shown = new Set([...document.querySelectorAll("iframe.figure-frame")].map(figureAddress));
  for (const [address, frame] of frames) {
    if (shown.has(address) || ui.figureLinked.has(address)) continue;
    // Out of sight a lazy frame would wait to load until it shows.
    frame.removeAttribute("loading");
    figureWarm.append(h("p", {}, frame));
  }
}

// Server Markdown in a block. Its collapsible blocks (<details>) start closed, and a redraw
// keeps each as the user left it.
function prose(key, cls, html) {
  const el = h("div", { class: cls, html });
  el.querySelectorAll("iframe.figure-frame").forEach((frame) => {
    frame.dataset.src = frame.getAttribute("src");
    frame.removeAttribute("src");
    fitFigure(frame);
  });
  el.querySelectorAll("img.figure-image").forEach((image) => {
    image.addEventListener("click", (event) => { event.stopPropagation(); openLightbox(image.getAttribute("src"), image); });
  });
  el.querySelectorAll("details").forEach((fold, index) => {
    const id = `${key}:fold:${index}`;
    fold.open = ui.open.has(id);
    rememberOpen(fold, (open) => { if (open) ui.open.add(id); else ui.open.delete(id); });
  });
  return el;
}

// The sweeper's labelled paragraphs in a step body: per label, the mark that stands for it.
const BODY_LABELS = {
  "What changes.": ["info", "what"],
  "What you decide.": ["arrow", "part"],
  "Ready.": ["check", "ready"],
  "After.": ["clock", "after"],
};

function isLabel(node) {
  return Boolean(node) && node.nodeName === "STRONG" && Object.hasOwn(BODY_LABELS, node.textContent.trim());
}

// A label that starts a line of a paragraph or a list item, after a single line break. Markdown keeps such a line
// in the paragraph or list item above it.
function startsLine(node) {
  const before = node.previousSibling;
  return isLabel(node) && Boolean(before) && before.nodeType === Node.TEXT_NODE && /\n\s*$/.test(before.textContent)
    && ["P", "LI"].includes(node.parentNode.nodeName) && !node.parentNode.closest("details, blockquote");
}

// Moves a label that starts a line, with the rest of its line and paragraph, into a paragraph of its own at the
// body's top level. Whatever followed it inside its containers goes on after that paragraph, in copies of the
// containers, so the rest of a list stays a list and the reading order holds.
function liftLabel(body, label) {
  const before = label.previousSibling;
  before.textContent = before.textContent.replace(/\s+$/, "");
  const block = label.parentNode;
  const run = [];
  for (let node = label; node; node = node.nextSibling) run.push(node);
  const paragraph = h("p", {}, run);
  let rest = null;
  let top = block;
  for (; top.parentNode !== body; top = top.parentNode) {
    const copy = top.parentNode.cloneNode(false);
    if (rest) copy.append(rest);
    while (top.nextSibling) copy.append(top.nextSibling);
    rest = copy.textContent.trim() ? copy : null;
  }
  top.after(paragraph);
  if (rest) paragraph.after(rest);
}

// A paragraph that starts with a label gets its mark in the margin in place of the bold words.
// The words stay in the text, hidden, so a copy of the paragraph still has them.
// A label that starts a line becomes the start of a paragraph first, also after a list.
// "What you decide." and what follows it up to the next label (a list of choices) form one block.
function labelledBody(key, html) {
  const body = prose(key, "prose body", html);
  for (const label of [...body.querySelectorAll("strong")].filter(startsLine)) liftLabel(body, label);
  let part = null;
  for (const block of [...body.children]) {
    const first = [...block.childNodes].find((node) => node.nodeType !== Node.TEXT_NODE || node.textContent.trim());
    if (block.tagName !== "P" || !isLabel(first)) {
      if (part) part.append(block);
      continue;
    }
    const label = first.textContent.trim();
    const [name, kind] = BODY_LABELS[label];
    const words = label.replace(/\.$/, "");
    block.classList.add("labelled", `label-${kind}`);
    first.classList.add("label-words");
    first.setAttribute("aria-hidden", "true");
    block.prepend(h("span", { class: "label-mark", role: "img", "aria-label": words, title: words }, icon(name)));
    body.classList.add("has-labels");
    // After is one quiet line, whole on hover. The words sit in their own span, so the clamp leaves the margin mark
    // alone.
    if (kind === "after") {
      block.append(h("span", { class: "label-text" }, [...block.childNodes].filter((node) => !(node.classList && node.classList.contains("label-mark")))));
    }
    if (kind === "after") block.title = labelText(block);
    part = null;
    if (kind === "part") {
      part = h("div", { class: "label-part-block" });
      block.replaceWith(part);
      part.append(block);
    }
  }
  return body;
}

// The words a menu's summary stands for: its tooltip and what a screen reader says.
// `iconOnly`: the summary shows only an icon, so it gets the square button shape.
function labelSummary(menuEl, words, iconOnly) {
  const summary = menuEl.querySelector("summary");
  summary.title = words;
  summary.setAttribute("aria-label", words.split("\n")[0]);
  if (iconOnly) summary.classList.add("icon-only");
}

// A copy shows on its button: the icon turns into a check for 1.5 s. With `html` (a draft as the page
// shows it), the clipboard holds both: a rich editor (Slack, Notion, Gmail) pastes it formatted,
// and a plain text box (a GitHub comment) pastes the Markdown.
async function copy(text, button, html) {
  try {
    if (html && window.ClipboardItem) {
      await navigator.clipboard.write([new ClipboardItem({
        "text/plain": new Blob([text], { type: "text/plain" }),
        "text/html": new Blob([html], { type: "text/html" }),
      })]);
    } else {
      await navigator.clipboard.writeText(text);
    }
  } catch (error) {
    logError("copying", error);
    toast("The browser blocked copying", true);
    return;
  }
  button.replaceChildren(icon("check"));
  button.classList.add("copied");
  setTimeout(() => { button.replaceChildren(icon("copy")); button.classList.remove("copied"); }, 1500);
}

// PERM-9: a press or typing on a permission card tells the server the user is on it, at most once a
// minute. While the user works on a card, a session's withdrawal of the call waits, so the user's answer
// still lands and adds its rule. A 409 means the call ended meanwhile.
function markRequestActive(requestId) {
  const last = ui.requestActiveAt.get(requestId);
  if (last !== undefined && Date.now() - last < ACTIVE_EVERY_MS) return;
  ui.requestActiveAt.set(requestId, Date.now());
  api("POST", `/api/permissions/requests/${requestId}/active`).catch((error) => {
    if (error.status === 409) return;
    logError("marking that you are on a call", error);
    if (ui.activeWarned) { console.warn(`Could not tell Cockpit you are on request ${requestId}: ${error.message}`); return; }
    ui.activeWarned = true;
    toast("Could not mark that you are on this call", true, null, error.message);
  });
}

function hasDraft(stepId) {
  return (ui.drafts.get(`msg:step:${stepId}`) || "").trim() !== "";
}

// A step that holds words the user wrote and did not send: a plain mark on its card and its row.
// It is drawn on every step, hidden while there is no draft, so typing shows and hides it without a redraw.
function draftMark(step, isNext) {
  return h("span", {
    class: "draft-mark", role: "img", "aria-label": "Unsent draft", "data-tip": "Unsent draft", "data-draft-step": step.id, hidden: !hasDraft(step.id),
  }, icon("pencil"), h("span", { class: "label", text: isNext ? "Unsent draft" : "Draft" }));
}

// The same, as a small blue triangle in the top right corner of the step's whole card or row, so
// a partial answer shows while the reply box is scrolled away or folded. The chip says it in words.
function draftCorner(step) {
  return h("span", { class: "draft-corner", "aria-hidden": "true", "data-draft-step": step.id, hidden: !hasDraft(step.id) });
}

function showDraftMarks(stepId) {
  const shown = Boolean(hasDraft(stepId));
  for (const mark of document.querySelectorAll(`[data-draft-step="${stepId}"]`)) mark.hidden = !shown;
  for (const key of document.querySelectorAll(`[data-done-step="${stepId}"]`)) {
    key.querySelector(".label").textContent = taskDoneWords(`msg:step:${stepId}`, !key.hasAttribute("data-no-session"));
  }
  // Beside a closing answer, Task done shows only while it would send a last message (CONTROL-31).
  const typed = Boolean((ui.drafts.get(`msg:step:${stepId}`) || "").trim());
  for (const key of document.querySelectorAll(`[data-done-step="${stepId}"][data-beside-closer]`)) key.hidden = !typed;
}

// Time on a step (TRACK-2): the user starts a step's timer with its play button and pauses it; one
// timer runs at a time. The server stops it when the step leaves the user. Play and pause show at once.

// The state with `stepId`'s timer started (`running`) or paused at `at` (Date.now()): a timer
// running on another step stops, as on the server, and the item's time and the header follow.
function timedState(state, stepId, running, at) {
  const ran = state.timer;
  const step = STEP_LISTS.flatMap((name) => state[name]).find((other) => other.id === stepId);
  const runsHere = Boolean(ran && ran.step_id === stepId);
  if (!step || runsHere === running || (running && step.handoff_message_id)) return state;
  const shown = { ...state, timer: null };
  const changeStep = (id, change) => {
    for (const name of STEP_LISTS) shown[name] = shown[name].map((other) => other.id === id ? { ...other, timer: change(other.timer) } : other);
  };
  const changeItem = (id, change) => {
    shown.items = shown.items.map((item) => item.id === id ? { ...item, time_spent: change(item.time_spent) } : item);
  };
  if (ran) {
    const spent = Math.max(0, at - new Date(ran.started_at).getTime()) / 1000;
    const stop = (timer) => timer && { ...timer, seconds: timer.seconds + spent, started_at: null };
    changeStep(ran.step_id, stop);
    changeItem(ran.item_id, stop);
  }
  if (running) {
    const now = new Date(at).toISOString();
    changeStep(stepId, (timer) => ({ item_id: step.item_id, seconds: timer ? timer.seconds : 0, started_at: now }));
    // A queued step the timer ran on keeps its place on a take, as on the server (`_started_in_place`).
    const inPlace = step.started_in_place || Boolean(step.queued_at);
    for (const name of STEP_LISTS) shown[name] = shown[name].map((other) => other.id === stepId ? { ...other, started_in_place: inPlace } : other);
    changeItem(step.item_id, (spent) => ({ seconds: spent ? spent.seconds : 0, started_at: now }));
    const item = shown.items.find((other) => other.id === step.item_id);
    shown.timer = { step_id: stepId, item_id: step.item_id, item_title: step.item.title, started_at: now, seconds: item ? item.time_spent.seconds : 0 };
  }
  return shown;
}

// The user's play or pause. Play while writing feedback: the step runs, and no step waits for the feedback.
// `own`: false for the page's own start (startOpened), which `optimistic` draws once the user's press has ended.
function setTimer(stepId, running, own = true) {
  if (running && ui.feedbackHold) ui.feedbackHold.stepId = null;
  ui.pausedStepId = running ? null : stepId;
  // The user's play, on Next up or a step below it, spends the card's one start (`startOpened`): a play given up or
  // refused goes back, and the page starts no other step in its place.
  if (running) {
    const card = document.querySelector("#next .next-card[data-step]");
    ui.autoStarted = card ? Number(card.dataset.step) : stepId;
  }
  return sendTimer(stepId, running, own);
}

// Play on a snoozed step: it wakes into its place, as Unsnooze does, and its timer starts
// once Cockpit has it awake, since Cockpit starts no timer on a snoozed step.
function wakeAndStart(step) {
  return act(step, "snooze", { until: null }).then((woken) => woken && setTimer(step.id, true));
}

// A step card's class for its timer (TRACK-6): " on-clock" while it runs, " paused-clock" once it ran and
// stopped, nothing before its first play or while feedback holds it.
function clockClass(step) {
  if (step.left || step.handoff_message_id || !step.timer || heldForFeedback(step)) return "";
  return step.timer.started_at ? " on-clock" : " paused-clock";
}

// A press on a phone that just woke often comes before its connection is back: the press shows at once and is sent
// again until Cockpit has it. Presses reach Cockpit in the order the user made them, each as its own action, so a later
// press is neither dropped nor overtaken by an earlier one still waiting for the connection. Cockpit starts a timer
// when the start arrives, so a start it has not started sending within TIMER_PRESS_MS of being made is given up: it
// goes back on the page, and the user sees that it failed, with Start now to start it afresh. A try already on its way
// when that time is up runs to its end. A pause is never given up: it tells Cockpit how long ago it was pressed, and
// Cockpit stops the span that ran at the press, and no timer started since on another page. Behind this page's start of
// the step it names that start, whose span then stops too when the start landed after the press, also when its answer
// was lost. A pause that fails all the same (Cockpit refused it) offers to pause again, as the timer may still run.
// Leaving the page asks first while any press is on its way.
const TIMER_PRESS_MS = 60e3;

// The user's play (`running`) or pause on a step. A 409 means the step left the user or the timer
// stopped meanwhile; the state after it shows how the timer stands.
// `own`: as for setTimer; the press is sent at once either way, so the time counts from it.
function sendTimer(stepId, running, own = true) {
  const at = Date.now();
  const giveUpAt = performance.now() + TIMER_PRESS_MS;
  const awayAtPress = awayClock();
  const action = running ? "start" : "pause";
  return run(`timer:${stepId}:${action}:${++ui.timerPresses}`, async () => {
    const path = `/api/steps/${stepId}/timer/${action}`;
    const press = crypto.randomUUID();
    const before = ui.timerSent;
    const sent = before.then((last) => running ? apiStart(path, press, giveUpAt, awayAtPress)
      : apiPressed(path, at, last && last.stepId === stepId ? last.press : null));
    // A pause that failed leaves the start before it as the one a next pause names.
    const pressed = running ? { stepId, press } : null;
    ui.timerSent = sent.then(() => pressed, () => running ? pressed : before);
    if (running) sent.then((data) => noteOwnInput(data.input_count), () => {});
    holdLeave(sent);
    try {
      await optimistic((state) => timedState(state, stepId, running, at),
        () => sent.catch((error) => { if (error.status !== 409 || error.cutOff) throw error; }), null, own);
    } catch (error) {
      // A start given up because the page was asleep, offline or in the background for most of TIMER_PRESS_MS: the
      // toast says which ("hidden" for the background) and for how long, in words short enough for its two lines in the
      // side column, explains on hover, and offers to start the timer now. It is not logged as a page error (errors.js,
      // `pageAway`).
      if (running && error.pageAway) {
        // One unbroken length: "3h 26m" does not split across the toast's lines.
        const away = (error.awaySeconds < 60 ? `${error.awaySeconds} s` : durationWords(error.awaySeconds)).replace(/ /g, "\u00a0");
        const where = error.offline ? "offline" : "in the background";
        throw Object.assign(new Error(`Timer not started: ${error.offline ? "offline" : "hidden"} ${away}`, { cause: error }), {
          tip: `You pressed Start, then this page was ${where} or your device asleep for ${away}, so Cockpit never got the press.`,
          retry: { label: "Start now", run: () => setTimer(stepId, true) },
        });
      }
      if (running) throw refused("Timer not started", error);
      throw Object.assign(refused("Timer not paused", error), { retry: { label: "Pause", run: () => setTimer(stepId, false) } });
    }
  });
}

// {seconds, started_at}: the seconds of ended spans, plus the running span's while one runs.
function timerSeconds(spent) {
  const running = spent.started_at ? Math.max(0, Date.now() - new Date(spent.started_at).getTime()) / 1000 : 0;
  return spent.seconds + running;
}

// A running clock: "4:05", "1:02:09".
function timerClock(seconds) {
  const whole = Math.floor(seconds);
  const pad = (number) => String(number).padStart(2, "0");
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor(whole / 60) % 60;
  return hours ? `${hours}:${pad(minutes)}:${pad(whole % 60)}` : `${minutes}:${pad(whole % 60)}`;
}

// A stopped total: "<1m", "12m", "1h 12m".
function timerTotal(seconds) {
  const minutes = Math.floor(seconds / 60);
  if (minutes < 1) return "<1m";
  return minutes < 60 ? `${minutes}m` : `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

// The whole seconds until `iso` by the server's clock, and 0 once it passed.
function secondsLeft(iso) {
  return Math.max(0, Math.ceil((Date.parse(iso) - Date.now() - ui.clockOffset) / 1000));
}

// An item's time; `tickTimers` moves it on each second while it runs.
function timerClockEl(spent) {
  return h("span", { class: "timer-clock", "data-timer-start": spent.started_at, "data-timer-seconds": spent.seconds,
    text: spent.started_at ? timerClock(timerSeconds(spent)) : timerTotal(spent.seconds) });
}

function tickTimers() {
  checkIdle();
  if (document.hidden) return;
  // A paused clock pulses with the seconds (TRACK-6); one class on the page keeps every paused clock in
  // step, and a redraw does not restart it.
  document.body.classList.toggle("pulse-dim");
  for (const el of document.querySelectorAll("[data-timer-start]")) {
    const text = timerClock(timerSeconds({ seconds: Number(el.dataset.timerSeconds), started_at: el.dataset.timerStart }));
    if (el.textContent !== text) el.textContent = text;
  }
  for (const el of document.querySelectorAll("[data-total-since]")) {
    const text = durationWords((Date.now() - Date.parse(el.dataset.totalSince)) / 1000);
    if (el.textContent !== text) el.textContent = text;
  }
  for (const el of document.querySelectorAll("[data-ago]")) {
    const text = ago(el.dataset.ago);
    if (el.textContent !== text) el.textContent = text;
    showFresh(el);
  }
  // A step's time left (SPEC NEXT-60); once it runs out, the page draws once more, in the look after the time.
  // The card's band changes here, since a draw leaves a card alone while the user types in it.
  let ended = false;
  for (const el of document.querySelectorAll("[data-expires]")) {
    const expires_at = el.dataset.expires;
    const left = secondsLeft(expires_at);
    el.textContent = timerClock(left);
    if (left === 0) {
      el.removeAttribute("data-expires");
      el.closest(".expiry-banner")?.replaceWith(expiryBanner({ expires_at }));
      ended = true;
    }
  }
  if (ended) safeRender();
}

// The step's time in its ⋯ menu (VIEW-29, TRACK-2): the item's time, ticking while this step's timer runs, opens its
// correction (TRACK-4); a step whose timer stands has Start the timer. The card's orange stripe shows a running timer
// (TRACK-6). The running timer has no Pause: it stops when the user hands the step off, closes or snoozes it, or goes
// idle (TRACK-2, TRACK-5). On a snoozed step Start wakes it first, as Unsnooze does, since a snoozed step's timer does
// not run. `close` closes the menu.
function timeEntries(step, item, snoozed, close) {
  if (step.left || step.handoff_message_id) return [];
  const timer = step.timer;
  const spent = (item && item.time_spent) || { seconds: 0, started_at: null };
  const press = (onclick) => () => { close(); onclick(); };
  const line = (iconEl, words, hint, props) => h("button", { type: "button", role: "menuitem", ...props },
    h("span", { class: "menu-label" }, iconEl, words), hint && h("span", { class: "when menu-time" }, hint));
  const correct = (words, hint, timing, title) => item && line(icon("clock"), words, hint, { "data-timer": timing, title,
    onclick: press(() => openCorrect(item.id, item.title, spent)) });
  if (heldForFeedback(step)) {
    return [line(icon("play"), "Back to this step's timer", timerTotal(timerSeconds(spent)), { "data-timer": "held",
      title: "Your feedback keeps its own time", onclick: press(() => backToStep(step.id)) })];
  }
  const where = item && spent.seconds > 0 && activityNote(item.id);
  if (timer && timer.started_at) {
    return [correct("Timing this task", timerClockEl(spent), "running",
      "Stops when you send, close or snooze; press to edit"), where].filter(Boolean);
  }
  return [
    line(icon("play"), snoozed ? "Unsnooze and start the timer" : "Start the timer", null, {
      "data-timer": "start", onclick: press(() => snoozed ? wakeAndStart(step) : setTimer(step.id, true)),
    }),
    spent.seconds > 0 && correct("Time on this task", timer
      ? h("span", { class: "paused-time", title: "Paused", text: timerClock(timerSeconds(spent)) })
      : timerTotal(timerSeconds(spent)), "stopped", "Set the time by hand"),
    where,
  ].filter(Boolean);
}

// Where the task's timer time went on the Mac (TRACK-10), as "On your Mac: 30m Cursor, 10m github.com": the
// menu fills it when it opens (`loadActivityNotes`), and it stays hidden while the Mac recorded none of it.
function activityNote(itemId) {
  const el = h("p", { class: "menu-note activity-note", "data-activity-item": itemId, hidden: true });
  const known = ui.activity.get(itemId);
  if (known) fillActivityNote(el, known);
  return el;
}

const ACTIVITY_NOTE_LABELS = 4;

function fillActivityNote(el, breakdown) {
  const parts = breakdown.labels.slice(0, ACTIVITY_NOTE_LABELS).map((row) => `${timerTotal(row.minutes * 60)} ${row.label}`);
  const rest = breakdown.labels.slice(ACTIVITY_NOTE_LABELS).reduce((sum, row) => sum + row.minutes, 0);
  if (rest > 0) parts.push(`${timerTotal(rest * 60)} other`);
  if (breakdown.away > 0) parts.push(`${timerTotal(breakdown.away * 60)} away`);
  el.textContent = `On your Mac: ${parts.join(", ")}`;
  el.title = "Recorded minutes inside this task's timer, by app or site";
  el.hidden = parts.length === 0;
}

function loadActivityNotes(menuEl) {
  for (const el of menuEl.querySelectorAll("[data-activity-item]")) {
    const itemId = Number(el.dataset.activityItem);
    api("GET", `/api/items/${itemId}/activity`).then((breakdown) => {
      ui.activity.set(itemId, breakdown);
      fillActivityNote(el, breakdown);
    }, (error) => {
      console.error(`the Mac's record for item ${itemId} did not load`, error);
      el.textContent = `Your Mac's record did not load: ${error.message}`;
      el.hidden = false;
    });
  }
}

// An item's time on its All work row, ticking while one of its steps' timers runs; it opens the time's
// correction (TRACK-4) without opening the row.
function timeChip(item) {
  const spent = item.time_spent;
  if (!spent) return null;
  return h("button", {
    class: `time-chip${spent.started_at ? " running" : ""}`, type: "button",
    title: "Edit tracked time",
    onclick: (event) => { event.stopPropagation(); openCorrect(item.id, item.title, spent); },
  }, spent.started_at ? h("span", { class: "timer-dot" }) : icon("clock"), timerClockEl(spent));
}

// Taking back time (TRACK-4). One dialog over the page, outside the redrawn sections: the user sets an item's
// time by hand, from any of its time readouts: lower to take back time (TRACK-4), higher or "+20m" to add time
// the timers missed (TRACK-7).

function timeDialog() {
  return document.getElementById("time-dialog");
}

// "1h 12m", "45m", "0m": a time the user can type back as it reads.
function durationWords(seconds) {
  const minutes = Math.floor(seconds / 60);
  return minutes < 60 ? `${minutes}m` : `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

// What the user typed as a time, in seconds, or null: "1h 10m", "1h", "70m", "70" (minutes), "1:10".
function parseDuration(text) {
  const words = text.trim().toLowerCase();
  const colon = /^(\d+):([0-5]?\d)$/.exec(words);
  if (colon) return (Number(colon[1]) * 60 + Number(colon[2])) * 60;
  const parts = /^(?:(\d+)\s*h)?\s*(?:(\d+)\s*m?)?$/.exec(words);
  if (!words || !parts || (parts[1] === undefined && parts[2] === undefined)) return null;
  return (Number(parts[1] || 0) * 60 + Number(parts[2] || 0)) * 60;
}

// A total that counts on while `since` (an ISO time) is in the past: "1h 52m".
function totalSince(since) {
  return h("span", { "data-total-since": since, text: durationWords((Date.now() - Date.parse(since)) / 1000) });
}

function showTimeDialog(mode, content, focus) {
  const dialog = timeDialog();
  dialog.dataset.mode = mode;
  dialog.replaceChildren(h("div", { class: `time-panel ${mode}`, tabindex: "-1" }, content));
  if (!dialog.open) dialog.showModal();
  // Focus goes to the panel or the field, never straight to a button: keys the user was typing elsewhere
  // when the question opened press nothing.
  (focus || dialog.querySelector(".time-panel")).focus();
}

// `spent`: the item's time as {seconds, started_at} when the dialog opened. Each check reads the item's time as
// drawn now, so a change that went through or failed meanwhile counts. It opens on the item's time.
function openCorrect(itemId, title, spent) {
  const now = () => timerSeconds(drawn(ui.state).items.find((item) => item.id === itemId)?.time_spent || spent);
  const typed = durationWords(now());
  const field = h("input", { class: "field time-field", type: "text", inputmode: "text", autocomplete: "off", spellcheck: "false",
    "data-key": "time:correct", "aria-label": "New time", "aria-describedby": "time-hint", value: typed });
  const hint = h("p", { class: "time-hint", id: "time-hint", "aria-live": "polite" });
  const set = h("button", { class: "button primary", type: "submit" });
  let edited = false;
  let rechecking = false;
  // What the field asks for, or null: { add } seconds for "+20m", which Cockpit adds to the time it counted,
  // or { total } seconds for a time.
  const wanted = () => {
    const text = field.value.trim();
    const seconds = parseDuration(text.startsWith("+") ? text.slice(1) : text);
    if (seconds === null) return null;
    return text.startsWith("+") ? { add: seconds } : { total: seconds };
  };
  const check = () => {
    const want = wanted();
    const counted = now();
    const change = want && ("add" in want ? want.add : want.total - counted);
    const tooMuch = want !== null && change > ADD_LIMIT;
    // One change to an item's time at a time: the next is counted from what the last one left. The dialog
    // looks again until the last one went through.
    const waiting = ui.busy.has(`time:${itemId}`);
    if (waiting && !rechecking) {
      rechecking = true;
      setTimeout(() => { rechecking = false; if (field.isConnected) check(); }, 250);
    }
    const ok = want !== null && edited && Math.round(change) !== 0 && !tooMuch && !waiting;
    const words = (seconds) => seconds < 60 ? "under a minute" : durationWords(seconds);
    hint.classList.toggle("refused", want === null || tooMuch);
    hint.textContent = want === null ? "Type a time like 45m, 1h 10m or 1:10, or +20m to add"
      : !edited ? "Type the time you spent"
        : tooMuch ? `Adds at most ${ADD_LIMIT / 3600} hours at once`
          : waiting ? "Cockpit is still taking your last change; try again in a moment"
            : !ok ? `The timers counted ${durationWords(counted)}`
              : change > 0 ? `Adds ${words(change)}` : `Removes ${words(-change)}`;
    set.disabled = !ok;
    set.textContent = !ok ? "Set" : "add" in want ? `Add ${durationWords(want.add)}` : `Set to ${durationWords(want.total)}`;
    return ok ? want : null;
  };
  field.addEventListener("input", () => { edited = true; check(); });
  const form = h("form", { class: "time-form", onsubmit: (event) => {
    event.preventDefault();
    const want = check();
    if (want === null) return;
    timeDialog().close();
    if ("add" in want) addItemTime(itemId, want.add, `Added ${durationWords(want.add)}`);
    else setItemTime(itemId, Math.round(want.total), `Time set to ${durationWords(want.total)}`);
  } },
  h("h2", { id: "time-dialog-title", text: "Correct the time" }),
  h("p", { class: "time-item", text: title }),
  h("div", { class: "time-well" }, field),
  hint,
  h("div", { class: "time-actions" }, set,
    h("button", { class: "button ghost", type: "button", onclick: () => timeDialog().close() }, "Cancel")));
  check();
  showTimeDialog("correct", form, field);
  field.select();
}

function setItemTime(itemId, seconds, words) {
  const at = Date.now();
  return run(`time:${itemId}`, async () => {
    try {
      await optimistic((state) => timeCorrected(state, itemId, seconds, at), () => api("POST", `/api/items/${itemId}/time`, { seconds }));
    } catch (error) {
      throw refused("Time not changed", error);
    }
  }, () => toast(words));
}

function addItemTime(itemId, seconds, words) {
  // Past ADDS_WAIT_MS the page takes polled states again (see `refresh`) and shows the server's time as it is.
  const pressed = Date.now();
  const add = Object.assign((state) => Date.now() - pressed < ADDS_WAIT_MS ? timeAdded(state, itemId, seconds) : state, { adds: pressed });
  return run(`time:${itemId}`, async () => {
    try {
      await optimistic(add, () => api("POST", `/api/items/${itemId}/time/add`, { seconds }));
    } catch (error) {
      throw refused("Time not added", error);
    }
  }, () => toast(words));
}

// `seconds` added to the item's time (TRACK-7): a running timer runs on as it was, as on the server. It adds
// to the state it is given, so `refresh` skips states that may already count it (its `adds`: when it was pressed).
function timeAdded(state, itemId, seconds) {
  const add = (spent) => spent && { ...spent, seconds: spent.seconds + seconds };
  const shown = { ...state, items: state.items.map((item) => item.id === itemId ? { ...item, time_spent: add(item.time_spent) } : item) };
  if (state.timer && state.timer.item_id === itemId) shown.timer = { ...state.timer, seconds: state.timer.seconds + seconds };
  return shown;
}

// The item's time set to `seconds` at `at`: a running timer runs on from then, as on the server.
function timeCorrected(state, itemId, seconds, at) {
  const now = new Date(at).toISOString();
  const set = (spent) => spent && { ...spent, seconds, started_at: spent.started_at ? now : null };
  const shown = { ...state, items: state.items.map((item) => item.id === itemId ? { ...item, time_spent: set(item.time_spent) } : item) };
  if (state.timer && state.timer.item_id === itemId) shown.timer = { ...state.timer, seconds, started_at: now };
  return shown;
}

// Activity and the timer (TRACK-5, UI-19). Activity is the user's input on a page with focus (presence.js), and the
// mouse moving or the wheel over Cockpit on screen, in front or behind another window, a Cockpit page open beside the
// step included (page-overlay.js passes on what the page's frame reports). Cockpit in front with the mouse still is
// no activity: the user may be doing something else.
const FADE_AFTER_MS = 60 * 1000;
const FADE_MS = 60 * 1000;
const AWAY_MS = FADE_AFTER_MS + FADE_MS;
const START_INPUT_MS = 2 * 60 * 1000;
const SEEN_REPORT_MS = 60 * 1000;
const SEEN_STORE_MS = 500;
const SEEN_TRY_MS = 10 * 1000;
// What Cockpit answers while it restarts (a deploy): the next second tries again.
const RESTARTING = [502, 503, 504];
const ACTIVITY_KEY = "cockpit.activity";
// The input that brought the user back to the device (`ui.back`), for its other tabs too.
const BACK_KEY = "cockpit.back";
// The question after a stretch without activity asks only within an hour of its start, and on the device the user
// was on then: activity on it a minute or less before the stretch began, a margin for another device's activity,
// which Cockpit hears of at most once a minute, and for clocks apart.
const ASK_WITHIN_MS = 60 * 60 * 1000;
const ELSEWHERE_MS = 60 * 1000;

// Activity on this device: this page's, and other tabs' (ACTIVITY_KEY).
function localActivity() {
  return Math.max(ui.lastInput, ui.lastSeen, readStored(ACTIVITY_KEY, 0));
}

// The state's `input` sits beside the timer, not in it: a timer the page draws before Cockpit confirmed it keeps it.
function lastActivity(shown) {
  const times = [localActivity(), Date.parse(shown.timer.started_at)];
  if (shown.input.last) times.push(Date.parse(shown.input.last));
  return Math.max(...times);
}

// Whether this page asks about a stretch without activity that began at `since` (ms), at `at` (ms), with the device's
// last activity before then at `before` (ms). It asks only on the device the user was on when the stretch began, and
// within an hour of its start. Otherwise the time away was no work: the page shows no question, and the user's next
// input here starts the timer from then, as Return does.
function asksHere(since, before, at) {
  return before >= since - ELSEWHERE_MS && at - since <= ASK_WITHIN_MS;
}

// The device's last activity before the user came back after `since` (ms): before the input that brought them back
// (`ui.back`, or another tab's on the device), so each of its pages asks just as that input decided. That input ends
// the stretch, so it comes at least AWAY_MS after its start, less ELSEWHERE_MS for a stretch timed by another device's
// clock; an earlier one is where the stretch began.
function activityBefore(since) {
  const back = [ui.back, readStored(BACK_KEY, null)]
    .find((input) => input && input.at >= since + AWAY_MS - ELSEWHERE_MS);
  return back ? back.before : localActivity();
}

// The stretch without activity the page saw at `at`, taken before anything that ends it counts: `since` its start,
// on the step whose timer ran, and `seen` the count of the user's inputs it went by (`input.count`).
function idleGap(shown, at) {
  return { stepId: shown.timer.step_id, since: lastActivity(shown), at, seen: shown.input.count };
}

// A count of the user's inputs that this page's own input made, as Cockpit answered it: a presence report, a start,
// an answer. A page that slept before its state had it still stops the timer for the stretch after it. Timed by the
// answer, not the request: a request that waited through the stretch (a retry after sleep) counts what came
// meanwhile too.
function noteOwnInput(count) {
  // A start Cockpit took before it counted inputs replays its answer without one (actions.py).
  if (!Number.isInteger(count)) return;
  ui.ownInputs = [...ui.ownInputs.slice(-19), { at: Date.now(), count }];
}
addEventListener("cockpit:counted", (event) => noteOwnInput(event.detail));

// The count of the user's inputs the stop of a stretch names: the state's it was measured by, or a later one that
// Cockpit gave this page's own input before the page saw the stretch. One it gave later (a start while the stop
// waited) is news to the stretch, as anything from another page is.
function gapSeen(gap) {
  const own = ui.ownInputs.filter((input) => input.at <= gap.at).map((input) => input.count);
  return Math.max(gap.seen, ...own);
}

// Each second: the question shows while Cockpit holds one, the veil follows the time without activity, and the step
// on the card starts.
function checkIdle() {
  const shown = ui.state && drawn(ui.state);
  if (!shown) return;
  // Input before the first state: the stretch before its first input is measured now, and the one after its last
  // below, both before the input is let go, which counts as activity from then on.
  const early = ui.earlyInput;
  if (early) {
    const timer = !shown.away && shown.timer;
    if (timer && early.first - lastActivity(shown) >= AWAY_MS) ui.gap = idleGap(shown, early.first);
    ui.lastInput = Math.max(ui.lastInput, early.last);
    ui.earlyInput = null;
  }
  if (shown.away) {
    // The question Cockpit holds takes the stretch this page measured too.
    if (early || ui.gap) {
      ui.gap = null;
      settle();
    }
    // The page's Return without asking comes with the user's input: the `cockpit:input` listener, or here for input
    // before the first state, judged by the device's activity before it.
    if (early && !asksHere(Date.parse(shown.away.since), early.before, early.first)) {
      returnUnasked(shown.away, early.before, early.first);
      return;
    }
    if (asksHere(Date.parse(shown.away.since), activityBefore(Date.parse(shown.away.since)), Date.now())) showIdle(shown.away);
    else if (ui.idle) closeIdle();
    else clearIdleVeil();
    // A tab in the background polls nothing: it still learns when the question was answered elsewhere.
    if (document.hidden && !ui.refreshing && Date.now() - ui.stateAskedAt >= POLL_MS) refresh();
    return;
  }
  // Answered here or on another page, or a timer started.
  if (ui.idle) closeIdle();
  const timer = shown.timer;
  // The stretch is measured before this second in front counts: a page that slept, or comes back to the front,
  // stops the timer at its last activity.
  if (timer && !ui.gap && Date.now() - lastActivity(shown) >= AWAY_MS) ui.gap = idleGap(shown, Date.now());
  if (early && !ui.gap) settle();
  const quiet = timer ? Date.now() - lastActivity(shown) : 0;
  if (ui.gap) askIdle();
  else if (quiet >= FADE_AFTER_MS) fadeIdle(quiet - FADE_AFTER_MS);
  else clearIdleVeil();
  if (!ui.feedbackHold) startOpened(shown);
}

// The veil fades in from where the time without activity has got to, so a page that comes back to the front late
// shows it as far along as it is. It sits in the top layer, over a page open over Cockpit's too, and takes the
// pointer, so a move over any part of the window reaches this page and clears it before a press lands. Set through
// the style object: the page's Content Security Policy drops a style attribute.
// Held here, not looked up: it may sit in a dialog that left the page.
const IDLE_VEIL = document.getElementById("idle-veil");
function fadeIdle(elapsed) {
  const veil = IDLE_VEIL;
  // A modal dialog (a page over the whole window, the time dialog) makes the rest of the page inert, the top layer
  // too: the veil goes inside the topmost one, so the pointer still reaches it, and back to the body after.
  const host = [...document.querySelectorAll("dialog:modal")].at(-1) || document.body;
  if (veil.matches(":popover-open") && veil.parentElement === host) return;
  if (veil.matches(":popover-open")) veil.hidePopover();
  if (veil.parentElement !== host) host.append(veil);
  veil.style.animationDelay = `-${elapsed}ms`;
  veil.showPopover();
}

function clearIdleVeil() {
  if (IDLE_VEIL.matches(":popover-open")) IDLE_VEIL.hidePopover();
  if (IDLE_VEIL.parentElement !== document.body) document.body.append(IDLE_VEIL);
}

// The mouse over Cockpit while the page's state has the timer running is activity (TRACK-5): for other Cockpit tabs, which read it under ACTIVITY_KEY as they read input (presence.js), and, at most once a minute,
// for other devices through Cockpit (`/api/presence/seen`, the state's `input`). Cockpit keeps it apart from input,
// so the mouse passing over a page behind another window keeps neither the sweeps (SWEEP-11) nor the time sheet
// going. A page whose state has no timer notes nothing: one that comes back to the front with an old state would
// otherwise hide a stretch away from the page that has the timer.
function noteSeen() {
  const now = Date.now();
  if (now - ui.lastSeen < SEEN_STORE_MS) return;
  ui.lastSeen = now;
  try {
    localStorage.setItem(ACTIVITY_KEY, String(now));
  } catch {
    // Private windows and blocked storage: other tabs see this page through Cockpit then.
  }
  if (now - ui.seenReported < SEEN_REPORT_MS) return;
  ui.seenReported = now;
  // One try, never retried: Cockpit stamps the report with its own time, so a report retried after the laptop
  // slept would cover the time away. The next move sends a new one instead. Unlike a failed answer,
  // Cockpit out of reach (a restart, the network) is no error here.
  fetch("/api/presence/seen", { method: "POST", credentials: "same-origin", signal: AbortSignal.timeout(SEEN_TRY_MS) })
    .catch((failure) => {
      throw Object.assign(failure, { unreached: true });
    })
    .then((response) => {
      if (!response.ok) throw Object.assign(new Error(`HTTP ${response.status}`), { status: response.status });
      // The connection can break after the answer's status and before its body, which is Cockpit out of reach too.
      return response.text().catch((failure) => {
        throw Object.assign(failure, { unreached: true });
      });
    })
    // The count the report made: this page's own report is not news to its stop of the timer.
    .then((text) => noteOwnInput(JSON.parse(text).input_count))
    .catch((error) => {
      ui.seenReported = 0;
      if (!error.unreached && !RESTARTING.includes(error.status)) logError("reporting the page in front", error);
    });
}

// The timer stops at the start of the stretch (`ui.gap`) and the page asks how to go on. A page in the background
// polls nothing, so it first needs a state asked after it saw the stretch, for input another page reported in it
// (`input.last`; this page reports none while the stretch waits): the user may have been working there. Until one
// came, each check asks for it; a refresh already on its way was asked too early, and one that fails leaves the
// stretch waiting. The stop names the `input.count` the stretch was measured by, not the newer state's, and Cockpit
// refuses it when input, a start or an answer came since: a page whose clock runs ahead cannot tell those from its
// stretch by their times.
function askIdle() {
  const gap = ui.gap;
  if (ui.stateAskedAt < gap.at) {
    if (!ui.refreshing) refresh();
    return;
  }
  ui.gap = null;
  const shown = drawn(ui.state);
  const timer = shown.timer;
  const since = timer && Math.max(gap.since, shown.input.last ? Date.parse(shown.input.last) : 0);
  // A timer started in the stretch (Return on another page) has activity in it.
  if (shown.away || !timer || timer.step_id !== gap.stepId || Date.parse(timer.started_at) > gap.since
    || gap.at - since < AWAY_MS) {
    settle();
    return;
  }
  // An input ended the stretch when it was what measured it (the `cockpit:input` listener).
  const back = ui.back && ui.back.at >= gap.at ? ui.back : null;
  ui.idleStop = stopIdle(timer, since, gapSeen(gap), Boolean(back) && !asksHere(since, back.before, back.at));
}

// The timer stops at `since` (ms), and the question shows at once; once Cockpit answers, the box carries the
// question's id, and a stop Cockpit refused or that asks nothing takes it (and the stop) away again. With `back`,
// the user is back where the question asks nothing (`asksHere`): the page answers it with Return in the same
// action, and shows the timer running from now at once. Settles to the id of the stop's question, or null.
async function stopIdle(timer, since, seen, back) {
  const at = isoUtc(new Date(since));
  const seconds = timer.seconds + Math.max(0, since - Date.parse(timer.started_at)) / 1000;
  const away = { step_id: timer.step_id, item_id: timer.item_id, item_title: timer.item_title, since: at, seconds };
  const returned = Date.now();
  let made = { stopped: true, asked: !back };
  // An input while the stop is on its way, where the page asks nothing: the timer shows running from it at once, and
  // the page answers the question with Return once the stop went through.
  const late = () => (!back && ui.back && ui.back.at >= returned && !asksHere(since, ui.back.before, ui.back.at)
    ? ui.back : null);
  // The time Return left out, with its question, once Cockpit took the Return: its toast offers Undo.
  let leftOut = null;
  ui.stopping = true;
  await run(`away:${timer.step_id}`, async () => {
    try {
      const answer = await optimistic((state) => {
        if (!made.stopped) return state;
        const lateBack = late();
        const stopped = { ...timedState(state, timer.step_id, false, since), away: made.asked && !lateBack ? away : state.away };
        if (back) return timedState(stopped, timer.step_id, true, returned);
        return lateBack ? timedState(stopped, timer.step_id, true, lateBack.at) : stopped;
      }, async () => {
        const stop = await api("POST", `/api/steps/${timer.step_id}/timer/away`, { since: at, seen });
        if (!back || !stop.away) return stop;
        const answered = await api("POST", `/api/steps/${timer.step_id}/timer/answer`, { question: stop.away, add: false });
        noteOwnInput(answered.input_count);
        if (answered.answered) leftOut = { question: stop.away, seconds: answered.left_out };
        // A question answered first elsewhere is open no longer: this page asks nothing either.
        return { stopped: true, away: null };
      });
      away.id = answer.away;
      made = { stopped: answer.stopped, asked: Boolean(answer.away) };
      render(false);
    } catch (error) {
      throw refused("Timer not stopped", error);
    }
  });
  ui.stopping = false;
  settle();
  if (leftOut) toastLeftOut(away, leftOut.question, leftOut.seconds);
  const lateBack = away.id && late();
  if (lateBack) returnUnasked(away, lateBack.before, lateBack.at);
  return away.id || null;
}

// Input held back while the page checked a stretch without activity counts now (presence.js).
function settle() {
  dispatchEvent(new Event("cockpit:settled"));
}

// The question Cockpit holds (`away`), drawn again only when it is another one.
function showIdle(away) {
  const shownAway = ui.idle;
  ui.idle = away;
  if (shownAway && shownAway.step_id === away.step_id && shownAway.since === away.since) return;
  // The question appears as an ordinary dialog over the dimmed page, and the veil that warned of it goes.
  clearIdleVeil();
  // The dialog shows only the time away and the two answers.
  const { since } = away;
  const dialog = document.getElementById("idle");
  dialog.replaceChildren(h("div", { class: "time-panel", tabindex: "-1" },
    h("h2", { id: "idle-title" }, "Back after ", totalSince(since)),
    h("div", { class: "time-actions" },
      h("button", { class: "button primary", type: "button", title: "Leaves out the time away",
        onclick: () => answerIdle(ui.idle, false, true) }, icon("play"), "Return"),
      h("button", { class: "button", type: "button", title: "Counts the time away too",
        onclick: () => answerIdle(ui.idle, true, true) }, icon("plus"), h("span", {}, "Add +", totalSince(since))))));
  if (!dialog.open) dialog.showModal();
  // Focus goes to the panel, never straight to a button: a key the user presses on coming back presses nothing.
  dialog.querySelector(".time-panel").focus();
}

function closeIdle() {
  ui.idle = null;
  document.getElementById("idle").close();
  clearIdleVeil();
}

// Return or Add. Cockpit takes the first answer to the question from any page and runs the step's timer on: from
// now for Return, from where it stopped for Add, so the time without activity counts once. A later answer, from
// another page or after a timer started, does nothing. The answer names the question the user saw: one this page
// asks before its stop went through gets its id from that stop, so it cannot arrive first. A refused answer (Add
// after more than a day) keeps the question, with Cockpit's reason in the toast. A Return Cockpit took offers Undo
// in its toast, which counts the time away after all, as Add would have (`undoReturn`). `pressed` is the user's
// press on the dialog; without it the page answers Return itself (`asksHere`), and the view stays where the
// user is.
async function answerIdle(away, add, pressed) {
  if (!away) return;
  // The press is the activity, not the end of the wait for Cockpit: the page may sleep meanwhile.
  const at = Date.now();
  // This page's stop holds the question's action until its refresh is through, even once it has the id.
  const stopped = await ui.idleStop;
  const question = away.id || stopped;
  if (!question) return;
  // The timer runs on with its step in sight, as Continue brings it back: the phone's back gesture may have shown
  // the list behind the question (TRACK-8).
  if (pressed && SIDEBAR) selectItem(away.item_id);
  let leftOut = null;
  const answered = await run(`away:${away.step_id}`, async () => {
    // The box goes with the press, as the timer shows running at once. A refused answer leaves the question in
    // Cockpit's state, and the next second's check asks it again.
    if (ui.idle) closeIdle();
    try {
      await optimistic((state) => state.away && state.away.step_id === away.step_id
        ? { ...timedState(state, away.step_id, true, add ? Date.parse(away.since) : Date.now()), away: null } : state,
      () => api("POST", `/api/steps/${away.step_id}/timer/answer`, { question, add }).then((data) => {
        noteOwnInput(data.input_count);
        if (data.answered && !add) leftOut = data.left_out;
        return data;
      }));
    } catch (error) {
      throw refused(add ? "Time not added" : "Timer not started", error);
    }
  });
  if (answered) ui.lastInput = Math.max(ui.lastInput, at);
  if (leftOut !== null) toastLeftOut(away, question, leftOut);
}

function toastLeftOut(away, question, seconds) {
  toast(`Left out ${durationWords(seconds)} away`, false, { label: "Undo", run: () => undoReturn(away, question, seconds) });
}

// Undo of Return: the time away counts after all, on the step and its item, as Add would have counted it. Like time
// added by hand, the change adds to the state it is given (`adds`, see `refresh`).
function undoReturn(away, question, seconds) {
  const pressed = Date.now();
  const added = (spent) => spent && { ...spent, seconds: spent.seconds + seconds };
  const add = Object.assign((state) => {
    if (Date.now() - pressed >= ADDS_WAIT_MS) return state;
    const shown = timeAdded(state, away.item_id, seconds);
    for (const name of STEP_LISTS) {
      shown[name] = state[name].map((step) => step.id === away.step_id ? { ...step, timer: added(step.timer) } : step);
    }
    return shown;
  }, { adds: pressed });
  return run(`away-undo:${away.step_id}`, async () => {
    try {
      await optimistic(add, () => api("POST", `/api/steps/${away.step_id}/timer/answer/undo`, { question }));
    } catch (error) {
      throw refused("Time not added", error);
    }
  });
}

// Opening a step starts its timer (UI-19, TRACK-2): the step on the card, once the user is active on the page and no
// timer runs. A step the user paused on this page waits for their play, and the page starts each opened step once,
// counting the user's own play, so a start Cockpit refused or the page gave up is not sent again each second.
// The first input after a while is often a press on the card's own buttons, so the start often comes during it: the
// start goes to Cockpit at once, and the card shows it once the press has ended (`optimistic`).
function startOpened(shown) {
  const card = document.querySelector("#next .next-card[data-step]");
  const onCard = card && card.checkVisibility() ? Number(card.dataset.step) : null;
  // A step the user left (leaveTimedStep) may stay on the card for a moment while the next view comes in; it starts
  // again once the card has stopped showing it, or the user's next move to a view (sidebar.js) ends the hold.
  if (onCard !== ui.leftStepId) ui.leftStepId = null;
  // While Next up waits to be current (UI-60), its card may be a step that already went.
  if (shown.timer || ui.picking || document.hidden || !document.hasFocus() || catchingUp("next")) return;
  if (Date.now() - ui.lastInput >= START_INPUT_MS) return;
  if (onCard === null) return;
  const id = onCard;
  if (id === ui.pausedStepId || id === ui.autoStarted || id === ui.leftStepId) return;
  const step = [...shown.queue, ...shown.ready_next].find((other) => other.id === id);
  if (!step || step.left || step.handoff_message_id || heldForFeedback(step)) return;
  // A Take back on its way draws the step on the card while Cockpit may still have it with the agent, which refuses
  // a start: the start waits until the Take back went through and the state has the step back.
  if (ui.inFlight.has(id) || ui.state.with_agents.some((other) => other.id === id)) return;
  ui.autoStarted = id;
  setTimer(id, true, false);
}

// An input first takes the stretch without activity before it, so a page that slept or reloaded stops the timer at
// the stretch's start and asks, however long its check of Cockpit's state takes. Before the page's first state, and
// until the check and its stop are through (`settle`), presence.js holds the input back from other tabs and the
// server, so a stop is not refused for this page's own input.
addEventListener("cockpit:input", (event) => {
  const now = Date.now();
  // presence.js stores the input under ACTIVITY_KEY after this, so the device's activity is still the one before it.
  const before = localActivity();
  if (now - before >= AWAY_MS) {
    ui.back = { at: now, before };
    writeStored(BACK_KEY, ui.back);
  }
  if (!ui.state) {
    ui.earlyInput = ui.earlyInput ? { ...ui.earlyInput, last: now } : { first: now, last: now, before };
    event.preventDefault();
    return;
  }
  const shown = drawn(ui.state);
  const timer = !shown.away && shown.timer;
  if (timer && !ui.gap && now - lastActivity(shown) >= AWAY_MS) ui.gap = idleGap(shown, now);
  ui.lastInput = now;
  if (ui.gap) {
    event.preventDefault();
    askIdle();
  } else if (ui.stopping) {
    event.preventDefault();
    // The input that brought the user back draws the timer running at once, where the page asks nothing (`stopIdle`).
    if (ui.back && ui.back.at === now) render(false);
  }
  else if (shown.away) returnUnasked(shown.away, before, now);
  else clearIdleVeil();
});

// The user's input at `at` (ms), with the device's last activity before it at `before` (ms), while Cockpit holds the
// question `away`: where this page asks nothing (`asksHere`), the input answers Return, once per question, so a
// Return Cockpit refused leaves the question to the next stretch, not a toast with each input.
function returnUnasked(away, before, at) {
  if (asksHere(Date.parse(away.since), before, at)) {
    checkIdle();
    return;
  }
  // By step and start: a question this page asked before its stop went through has no id yet.
  const key = `${away.step_id} ${away.since}`;
  if (ui.returned === key) return;
  ui.returned = key;
  if (ui.idle) closeIdle();
  answerIdle(away, false, false);
}

// The mouse moving, or the wheel, over Cockpit on screen is activity (TRACK-5), behind another window too, and clears
// the veil: the user can undo a fade without first clicking into the window. presence.js counts it as input only on
// a page with focus. A stretch it ends is first measured and asked about by the next second's check.
let pointerX = null;
let pointerY = null;
function noteAside(event) {
  if (!event.isTrusted) return;
  if (event.type === "pointermove") {
    if (event.screenX === pointerX && event.screenY === pointerY) return;
    pointerX = event.screenX;
    pointerY = event.screenY;
  }
  noteMoved();
}
function noteMoved() {
  if (document.visibilityState !== "visible" || !ui.state) return;
  const shown = drawn(ui.state);
  if (shown.away || !shown.timer || ui.gap || ui.stopping) return;
  if (Date.now() - lastActivity(shown) >= AWAY_MS) return;
  noteSeen();
  clearIdleVeil();
}
addEventListener("pointermove", noteAside, { capture: true, passive: true });
addEventListener("wheel", noteAside, { capture: true, passive: true });
// Activity over a Cockpit page beside the step or over the window counts for the step that opened it. When no step
// opened the page, it counts as activity on Cockpit, without affecting another step's timer.
addEventListener("cockpit:page-activity", (event) => {
  const timer = ui.state && drawn(ui.state).timer;
  if (event.detail === null || (timer && timer.step_id === event.detail)) noteMoved();
});
// Back to the window, by the keyboard too, is activity: the check runs at once, so a press before the next second does
// not land on the veil.
addEventListener("focus", () => {
  noteMoved();
  checkIdle();
});
document.getElementById("idle").addEventListener("cancel", (event) => event.preventDefault());

// The steps the user works on in this redraw: open, with an unsent draft or a running timer, or placed.
// Per step: where it is.
function holdSteps(lists) {
  ui.held = new Map();
  for (const [list, steps] of Object.entries(lists)) {
    steps.forEach((step, index) => {
      if (step.left) return;
      if (ui.expanded.has(`step:${step.id}`) || hasDraft(step.id) || timerRuns(step) || ui.placed.has(step.id)) {
        ui.held.set(step.id, { step, list, index });
      }
    });
  }
}

// The user's press takes the step out of the queue on the page.
function takenByUser(id) {
  ui.removedByUser.set(id, Date.now());
  ui.tookAway.set(id, performance.now());
}

// A held step missing from the state is kept, unless the user closed it: by a press on this page, or anywhere else,
// such as another tab. One holding an unsent draft here stays kept, so the
// words are not lost.
function keepLeftSteps(state) {
  const present = new Set(STEP_LISTS.flatMap((name) => state[name]).map((step) => step.id));
  const closedByUser = new Set(state.closed_recently.steps
    .filter((step) => step.closed_by === "user" && !hasDraft(step.id)).map((step) => step.id));
  const queued = new Set(state.queue.map((step) => step.id));
  const now = Date.now();
  for (const id of ui.left.keys()) if (present.has(id) || closedByUser.has(id)) ui.left.delete(id);
  for (const [id, place] of ui.held) {
    if (!present.has(id) && !ui.removedByUser.has(id) && !closedByUser.has(id) && !ui.left.has(id)) ui.left.set(id, place);
  }
  // The user's removal is done once the step is out of the queue; one the server kept there counts no more.
  for (const [id, at] of ui.removedByUser) if (!queued.has(id) || now - at > REMOVED_MS) ui.removedByUser.delete(id);
}

// A list with its kept steps back where they were drawn.
function withLeft(steps, list) {
  const places = [...ui.left.values()].filter((place) => place.list === list).sort((a, b) => a.index - b.index);
  const shown = [...steps];
  for (const place of places) shown.splice(Math.min(place.index, shown.length), 0, { ...place.step, left: true });
  return shown;
}

// Closing a kept row lets the step go, with its draft and sent mark.
function dropLeft(stepId) {
  ui.left.delete(stepId);
  ui.held.delete(stepId);
  for (const key of [`step:${stepId}`, `msg:step:${stepId}`]) {
    ui.expanded.delete(key);
    ui.drafts.delete(key);
    ui.choices.delete(key);
  }
  saveDrafts();
  if (ui.sent.delete(stepId)) saveSent();
  render(false);
}

function leftNote(step) {
  return h("div", { class: "left-note" },
    h("span", { class: "left-words", title: "This step left your queue; your draft is kept" },
      icon("info"), "Left your queue"),
    iconButton("x", "Close this row; its draft goes with it", {
      class: "button small ghost", onclick: (event) => { event.stopPropagation(); dropLeft(step.id); },
    }));
}

// Resorted, on a placed step: the queue's order changed while the user had this step open, or a step in Ready next
// scores clearly above a step the user opened, so it would have taken that place. A press puts this step in its
// ranked place and Next up shows the highest-ranked step (pausing this step's timer if it runs); with a step waiting,
// the press takes that step to the top instead (Take it).
function reorderButton(step, state, isNext) {
  if (step.left) return null;
  // On the sidebar page a waiting warning comes next, so the arrows on Next up turn red for it (SPEC UI-52). They
  // pulse for a warning that came after the step came up, and name the newest first; a change of order under the
  // step since then shows the Resorted key instead of the still red arrows of warnings that were already there.
  if (SIDEBAR && isNext) {
    const { fresh, older } = warningsSince(step, warningList(state));
    if (fresh.length) return warningKey([...fresh, ...older], true);
    if (older.length && !movedUnder(step, state)) return warningKey(older, false);
  }
  if (!movedUnder(step, state)) return null;
  const waiting = keptOut(state);
  const first = waiting || state.queue[0];
  const name = `"${first.item.title}"${waiting ? ` (score ${waiting.score})` : ""}`;
  const news = waiting ? `${name} waits in Inbox below your four places` : first.id === step.id ? "this step now ranks first" : `${name} now ranks first`;
  const words = `Resorted: ${news}. This step keeps its place because you opened it.\n`
    + (waiting ? `Press to take ${name} to Next up; the last step you are not on waits below your four places.`
      : "Press to put it in its ranked place and show the first-ranked step.");
  const props = { class: "button small ghost reorder resorted",
    onclick: (event) => { event.stopPropagation(); if (waiting) takeStep(waiting); else reorder(step); } };
  const key = iconButton("sort", words, props);
  // On Next up it sits on the card's top edge, before the card in #next, and stays in sight below the header
  // while any of the card shows.
  return isNext ? h("div", { class: "resorted-pin" }, key) : key;
}

// Whether the Resorted key shows on the step: the user holds it, and the queue's order changed under it or a step
// scored well above it waits in Ready next because of it.
function movedUnder(step, state) {
  if (!ui.placed.has(step.id)) return false;
  return Boolean(keptOut(state)) || state.queue.findIndex((other) => other.id === step.id) !== ui.order.indexOf(step.id);
}

// The waiting warnings split by when they came: `fresh`, newest first, came after the step came up on Next up;
// `older` were already there. A step that comes up starts with every waiting warning as older; a warning that goes
// and comes back counts as new.
function warningsSince(step, warnings) {
  const keys = new Set(warnings.map((warning) => warning.key));
  if (!ui.warningsSeen || ui.warningsSeen.stepId !== step.id) {
    ui.warningsSeen = { stepId: step.id, came: new Map([...keys].map((key) => [key, 0])), count: 0 };
  }
  const { came } = ui.warningsSeen;
  for (const key of came.keys()) if (!keys.has(key)) came.delete(key);
  for (const key of keys) if (!came.has(key)) came.set(key, ++ui.warningsSeen.count);
  const fresh = warnings.filter((warning) => came.get(warning.key) > 0).sort((a, b) => came.get(b.key) - came.get(a.key));
  return { fresh, older: warnings.filter((warning) => came.get(warning.key) === 0) };
}

// The change-order arrows while a warning waits after the step on Next up (SPEC UI-52): dark red, in the Resorted
// key's place, and pulsing while one came after the step came up (`news`). Hovering or focusing them shows a small
// card with the next warning's few words and its fix; pressing them opens the warning on Next up.
function warningKey(warnings, news) {
  const [next] = warnings;
  const key = h("button", {
    class: `button small reorder warn-key${news ? " warn-new" : ""}`, type: "button", "aria-label": `Next: ${next.short}. Press to open it`,
    onclick: (event) => { event.stopPropagation(); openWarning(next.key); },
  }, icon("sort"));
  const peek = h("div", { class: "warn-peek", "data-warning": next.key },
    h("p", { class: "warn-peek-title" }, icon(next.mark), h("span", { text: next.short })),
    next.fix && h("div", { class: "warn-peek-actions" }, fixButton(next.fix)),
    warnings.length > 1 && h("p", { class: "warn-peek-more", text: `+${warnings.length - 1} more` }));
  // A redraw draws a new pin whose card starts hidden, so a pointer resting on the card's button would find the
  // step card under it and the card would stay shut. A pin drawn while the old one was open starts open; once the
  // redraw has put it in place and restored focus, it stays open only while the pointer is over it or focus is in
  // it, and shuts when either leaves.
  const open = Boolean(document.querySelector("#next .warn-pin:is(:hover, :focus-within)"));
  const shut = (pin) => pin.classList.remove("peek-open");
  const pin = h("div", {
    class: `resorted-pin warn-pin${open ? " peek-open" : ""}`,
    onmouseleave: (event) => shut(event.currentTarget),
    onfocusout: (event) => { if (!event.currentTarget.contains(event.relatedTarget)) shut(event.currentTarget); },
  }, key, peek);
  if (open) requestAnimationFrame(() => {
    const under = pointerAt && document.elementFromPoint(pointerAt.x, pointerAt.y);
    if (!pin.contains(document.activeElement) && !(under && pin.contains(under))) shut(pin);
  });
  return pin;
}

// Where a mouse or pen last was, in window coordinates: a pin redrawn under it finds whether it still rests there.
// A finger rests nowhere once lifted, so touch leaves no position.
let pointerAt = null;
document.addEventListener("pointermove", (event) => {
  pointerAt = event.pointerType === "touch" ? null : { x: event.clientX, y: event.clientY };
}, { passive: true });

// How far a ready-next step's score must pass a queued step's to take its place (store.PLACE_MARGIN).
const PLACE_MARGIN = 10;

// The strongest step in Ready next that would have taken a place by score, had the user not opened the step
// (placed) that holds it.
function keptOut(state) {
  const opened = state.queue.filter((step) => ui.placed.has(step.id) && !taken(step));
  if (!opened.length) return null;
  // Another step takes an opened early look's place whatever the scores; an early look takes only an early look's
  // place (SPEC NEXT-42, store.fill_queue and store._stronger_first).
  const wouldTake = (waiting) => opened.some((step) => (step.early && !waiting.early)
    || (!step.early === !waiting.early && waiting.score - step.score > PLACE_MARGIN));
  // Other steps come before early looks, as when places fill (store._ordinary_first).
  return [...state.ready_next].sort(byRank).sort((a, b) => Boolean(a.early) - Boolean(b.early)).find(wouldTake) || null;
}

// The user opened, pressed or started answering a step in their queue (NEXT-16): it keeps the place it shows
// at, on this page and on the server, so no other step replaces it or pushes it out of the queue. A step its
// running timer placed gets the server's hold too. The server refuses the hold of a step that lost its place
// before the press arrived; the page then lets go of the place it gave it.
function holdPlace(stepId) {
  if (!ui.state) return;
  const step = drawn(ui.state).queue.find((one) => one.id === stepId);
  const at = ui.order.indexOf(stepId);
  if (!step || at < 0) return;
  const placedHere = !ui.placed.has(stepId);
  if (placedHere) ui.placed.set(stepId, at);
  // Kept as "Left your queue" if an agent closes it before the next redraw (keepLeftSteps).
  if (!ui.held.has(stepId)) ui.held.set(stepId, { step, list: "queue", index: at });
  askHold(stepId, placedHere);
}

// The server's hold for a step the page placed. A step that has its place only on this page so far waits in
// `ui.holdLater` until the server queues it (`askLaterHolds`).
function askHold(stepId, placedHere) {
  // The server's hold lasts while the step keeps this place, also after Resorted, so it is asked once per place.
  if (ui.holdAsks.has(stepId)) return;
  const step = ui.state.queue.find((one) => one.id === stepId);
  if (!step) {
    if (!ui.holdLater.has(stepId)) ui.holdLater.set(stepId, placedHere);
    return;
  }
  ui.holdLater.delete(stepId);
  const ask = { queuedAt: step.queued_at };
  ui.holdAsks.set(stepId, ask);
  const forget = () => {
    if (ui.holdAsks.get(stepId) !== ask) return;
    ui.holdAsks.delete(stepId);
    if (placedHere) ui.placed.delete(stepId);
  };
  api("POST", `/api/steps/${stepId}/hold`).then((answer) => { if (!answer.held) forget(); }, (error) => {
    forget();
    logError("keeping your step in its place", error);
    toast("Could not keep this step in its place", true, null, error.message);
  });
}

// Take it (NEXT-36), on a ready-next row: the step takes a place now, first in the queue.
function takeButton(step) {
  return h("button", { class: "button small take", type: "button", "aria-keyshortcuts": "t",
    title: "To the top of your queue now (key T)",
    onclick: (event) => { event.stopPropagation(); takeStep(step); } },
  icon("up"), h("span", { class: "label", text: "Take it" }));
}

// The user chose the step for the top, so steps held in their places give them up, also one whose timer runs
// (NEXT-16 holds a step against agents' changes, not against the user's own choice). With the drawn order
// forgotten too, `placeSteps` holds a running step at its new place in the server's order.
function takeStep(step) {
  ui.placed.clear();
  ui.order = [];
  showQueueTop();
  leaveTimerFor(step);
  act(step, "take");
  scrollToId("next");
}

// Take back leads the queue as a take does (CONTROL-3, MSG-8): no step the user read holds a place above it.
// A hand-off whose Undo is on offer in Busy waits for the Take back's answer (`handedNotice`).
function takeBack(step) {
  ui.placed.clear();
  ui.order = [];
  // A second press while a Take back is on its way does nothing (`act`), so it leaves the Undo to the first.
  const again = ui.inFlight.get(step.id)?.undoing === true;
  leaveTimerFor(step);
  const acting = act(step, "return");
  if (!again) for (const hold of [...ui.handsHeld]) if (hold.stepId === step.id) hold.takingBack(acting);
  render(false);
}

// Take it and Take back put the step on the sidebar page's card unless the card shows another item, so a timer on
// another step stops as it does when the user shows another item (TRACK-8, leaveTimedStep), and the step starts its own.
function leaveTimerFor(step) {
  if (!SIDEBAR || (ui.selItem !== null && ui.selItem !== step.item_id)) return;
  const timer = ui.state && drawn(ui.state).timer;
  if (timer && timer.step_id !== step.id) leaveTimedStep(timer);
}

function reorder(step) {
  ui.placed.delete(step.id);
  for (const id of ui.placed.keys()) if (!timerRuns(stepOnPage(id))) ui.placed.delete(id);
  showQueueTop();
  if (timerRuns(step)) setTimer(step.id, false);
  else render(false);
  scrollToId("next");
}

function timerRuns(step) {
  return Boolean(step && step.timer && step.timer.started_at);
}

// The words of a step that the user reads. An agent's rewrite changes them; a check or a new score does not.
function stepWords(step) {
  return JSON.stringify([step.ask_html, step.why, step.body_html, step.details, step.draft, step.answers, step.command, step.permission, step.closes, step.reply_hint, step.core_links]);
}

// The words of each step as the user last saw them. A step the user reads (placed, or Next up) keeps the
// words it had until the user presses it; the others take each rewrite as it comes.
function seeWords(steps, nextId) {
  for (const step of steps) {
    if (step.left) continue;
    if (!ui.seenText.has(step.id) || (step.id !== nextId && !ui.placed.has(step.id))) ui.seenText.set(step.id, stepWords(step));
  }
}

function stepOnPage(stepId) {
  return ui.state && STEP_LISTS.flatMap((name) => ui.state[name]).find((step) => step.id === stepId);
}

// An agent rewrote the step since the page first drew it (NEXT-28): a mark until the user presses the step.
function updatedMark(step, isNext) {
  if (step.left || !ui.seenText.has(step.id) || ui.seenText.get(step.id) === stepWords(step)) return null;
  return h("span", {
    class: "updated-mark", role: "img", "aria-label": "Updated", "data-time": step.prepared_at,
    title: `Updated by ${actorWords(step.prepared_by)} at ${clock(step.prepared_at)}`,
  }, icon("pencil"), isNext && h("span", { class: "label", text: "Updated" }));
}

// Sections

// The queue order the user sees (NEXT-16): the server's, with each placed step at its place.
function shownOrder(queue) {
  const ids = queue.map((step) => step.id);
  const order = ids.filter((id) => !ui.placed.has(id));
  const placed = [...ui.placed].filter(([id]) => ids.includes(id)).sort((a, b) => a[1] - b[1]);
  for (const [id, index] of placed) order.splice(Math.min(index, order.length), 0, id);
  return order;
}

// A queue step whose timer runs takes the place it was drawn at, or holds the one it has; a step
// that left the queue gives its place up.
function placeSteps(queue) {
  const ids = new Set(queue.map((step) => step.id));
  for (const id of ui.placed.keys()) if (!ids.has(id)) ui.placed.delete(id);
  // A hold is for one place: a step that left and came back between two polls has a new place time.
  const queuedAt = new Map(queue.map((step) => [step.id, step.queued_at]));
  for (const [id, ask] of ui.holdAsks) if (queuedAt.get(id) !== ask.queuedAt) ui.holdAsks.delete(id);
  // A step that left from above a placed step lets it move up; nothing moves it down.
  for (const [id, index] of ui.placed) {
    const drawnAt = ui.order.indexOf(id);
    if (drawnAt >= 0) ui.placed.set(id, Math.min(index, ui.order.slice(0, drawnAt).filter((other) => ids.has(other)).length));
  }
  for (const step of queue) {
    if (!timerRuns(step) || ui.placed.has(step.id)) continue;
    const drawnAt = ui.order.indexOf(step.id);
    ui.placed.set(step.id, drawnAt >= 0 ? drawnAt : shownOrder(queue).indexOf(step.id));
  }
}

function displayedQueue(queue) {
  const byId = new Map(queue.map((step) => [step.id, step]));
  ui.order = shownOrder(queue);
  return ui.order.map((id) => byId.get(id));
}

function plural(value, one, many) {
  return `${value} ${value === 1 ? one : many}`;
}

function scrollToId(id) {
  document.getElementById(id).scrollIntoView({ behavior: "smooth" });
}

// The failed messages, in the work list's Your latest message view; the list when none is drawn.
function openFailures() {
  // The page with the list of items at its left has no list of work: /classic opens them.
  if (SIDEBAR) { location.assign("/classic#board/failed"); return; }
  showMessages();
  const first = document.querySelector("#work .msg.failed");
  for (let group = first?.closest("details"); group; group = group.parentElement.closest("details")) group.open = true;
  (first || document.getElementById("work")).scrollIntoView({ behavior: "smooth", block: "center" });
}

// The work list sorted by the user's latest message, each row with the messages it got; a search filter would
// hide them, so it goes.
function showMessages() {
  writeStored("cockpit:work-sort", "messages");
  dropFind();
  render(false);
}

// The header shows no counts (UI-26): the queue, the status strip and Blocked already say what waits and who works. A
// failed message or a blocked call goes to the sweeper first, and what needs the user comes as a step (MSG-5).
function renderCounts(state) {
  // The Briefing tab's light: a morning briefing the user has not opened yet (SPEC VIEW-8). A phone
  // shows it on the brand key from the body's attribute (style.css).
  const unread = Boolean(state.briefing && state.briefing.unread);
  document.body.toggleAttribute("data-briefing-unread", unread);
  for (const link of document.querySelectorAll('a[href="/briefing"]')) link.classList.toggle("unread", unread);
  // The feedback no sweep has picked up yet, as a number on the Feedback key and its phone menu entry: it
  // grows while sweeps are stuck.
  const feedback = state.unread_feedback;
  document.getElementById("feedback-mode").classList.toggle("counted", feedback > 0);
  for (const count of document.querySelectorAll(".feedback-count")) {
    count.hidden = feedback === 0;
    count.textContent = feedback || "";
  }
  document.getElementById("feedback-mode").title = feedback > 0
    ? `Feedback: ${feedback} not picked up yet`
    : "Feedback: pick a part of the page, then add a to-do about it";
}

// Above 1000 px the tabs, the timer, the usage and the keys share one line. When they do not fit there, the
// header makes room in steps until they do (style.css, "tight-"): the usage wells shorten, then four tabs
// move into the brand menu. At 1000 px and below the brand, the tabs and the keys share the first line; when
// the keys would wrap to a line of their own (a zoomed-in screen, UI-5), the four tabs move into the brand
// menu instead.
// Each fit starts from the full header, so a wider window gives the room back.
const WIDE_HEADER = window.matchMedia("(min-width: 1001px)");
const TIGHT_STEPS = ["tight-usage", "tight-tabs"];
function fitHeader() {
  // The page with the rail has no top bar (sidebar.js).
  if (SIDEBAR) return;
  const inner = document.querySelector(".top-inner");
  inner.classList.remove(...TIGHT_STEPS);
  if (!WIDE_HEADER.matches) {
    if (inner.querySelector(".top-right").offsetTop > inner.querySelector(".brand-menu").offsetTop) inner.classList.add("tight-tabs");
    return;
  }
  for (const step of TIGHT_STEPS) {
    if (inner.scrollWidth <= inner.clientWidth) return;
    inner.classList.add(step);
  }
}

// Claude usage (SPEC UI-12, UI-37): one line per signed-in Claude account, in the same order on every poll, with no
// words. A short bar in a column of its own is the account's 5-hour window, a long bar its 7-day window; both are the
// same height. Each fills to the share used in its pace's colour, as in the terminal's status line (green behind the
// window's clock, orange a little ahead, red well ahead), with a tick where the clock is; a window at its limit fills
// red. The line new sessions start on (SPEC SESSION-17) is tinted, and an amber key ends the line of an account whose
// sign-in ends within SIGN_IN_WARN_MS. One hover card for the whole display draws the same bars larger and names
// them, with each account's address, when each window resets (how long until, and the local time), when its sign-in
// ends and the usage-limit resets it has banked.
const USAGE_STALE_MS = 30 * 60 * 1000;
const SIGN_IN_WARN_MS = 3 * 86400e3;

// How long until a time, in the words the page uses for spans: "45 min", "4 h 12 min", "3 d 22 h".
function countdown(date) {
  const minutes = Math.max(0, Math.round((date.getTime() - Date.now()) / 60000));
  if (minutes < 60) return `${minutes} min`;
  const [big, small] = minutes < 1440 ? [`${Math.floor(minutes / 60)} h`, minutes % 60] : [`${Math.floor(minutes / 1440)} d`, Math.floor((minutes % 1440) / 60)];
  return small ? `${big} ${small} ${minutes < 1440 ? "min" : "h"}` : big;
}

// A day in the user's local time is "today", "tomorrow", or the weekday and date, such as "Thursday 9 Oct".
function dayWords(date) {
  const day = new Date(date);
  day.setHours(0, 0, 0, 0);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const days = Math.round((day.getTime() - today.getTime()) / 86400e3);
  if (days === 0) return "today";
  if (days === 1) return "tomorrow";
  return `${date.toLocaleDateString([], { weekday: "long" })} ${date.getDate()} ${date.toLocaleDateString([], { month: "short" })}`;
}

// "in 4 d 5 h, Thursday 23 Oct at 17:30": how long until a time, then the time in the user's own zone.
function inAndAt(iso) {
  const date = new Date(iso);
  return `in ${countdown(date)}, ${dayWords(date)} at ${clock(iso)}`;
}

// An account no session has reported on yet counts as stale: its bars are tracks alone, dimmed.
function usageStale(account) {
  return !account.captured_at || Date.now() - new Date(account.captured_at).getTime() > USAGE_STALE_MS;
}

function signInEnding(account) {
  return Boolean(account.sign_in_ends) && new Date(account.sign_in_ends).getTime() - Date.now() < SIGN_IN_WARN_MS;
}

function signedOut(account) {
  return Boolean(account.sign_in_ends) && new Date(account.sign_in_ends).getTime() <= Date.now();
}

// A week's bar in the hover card: filled to the share used, a tick where the week is. A week with no use
// since it reset is its track alone. The page's content policy refuses style attributes, so the shares go
// in through the style object.
function usageBar(limit) {
  const bar = h("span", { class: "acct-bar" });
  if (limit.used === null) return bar;
  bar.classList.add(limit.used >= 100 ? "full" : limit.pace);
  bar.style.setProperty("--used", `${Math.min(100, limit.used)}%`);
  bar.style.setProperty("--gone", `${Math.min(100, limit.elapsed)}%`);
  if (limit.used > 0) bar.append(h("span", { class: "acct-fill" }));
  bar.append(h("span", { class: "acct-tick" }));
  return bar;
}

function weekOf(account) {
  return account.windows.find((limit) => limit.key === "seven_day");
}

// The weekly limits' lanes (SPEC UI-57, UI-73). Every lane uses one scale, LANE_PX pixels per percent of a week, and
// ends half a week after now. A week whose use runs to that end or past it would draw as full however much is left,
// so its lane runs on to that week's reset, and the room left shows as the empty end of its bar. An account's week is
// a bar placed by how much of it has passed, outlined in its pace colour: use up to now is striped, use beyond now
// (ahead of an even pace) solid. A lane starts where its earliest week starts, so each family's mark sits right
// beside its bars. A week with no use since it reset is its outline alone, a week no session has read the whole
// lane's outline. The scale is set so the whole foot (lanes, rack, the light and Reload) fits the rail's one line;
// while the sign-in key shows (UI-37) the lanes narrow to make room for it.
function weekLane(family, weeks, scale) {
  const known = (week) => week && Number.isFinite(week.elapsed);
  const startOf = (week) => (known(week) ? Math.max(0, 50 - weekGone(week)) : 0);
  // Only weeks that were read set where the lane starts and ends: an unread week spans the whole lane.
  const read = weeks.filter(({ week }) => known(week)).map(({ week }) => week);
  const from = read.length ? Math.min(...read.map(startOf)) : 0;
  const to = Math.max(100, ...read.filter((week) => week.used !== null && weekUseEnd(week) >= 100).map(weekResetEnd));
  const lane = h("span", { class: `lane ${family}` });
  lane.dataset.units = String(to - from);
  lane.style.setProperty("--lane", `${Math.round(scale * (to - from))}px`);
  lane.style.setProperty("--now", `${(((50 - from) / (to - from)) * 100).toFixed(2)}%`);
  for (const { week, stale } of weeks) lane.append(laneBar(week, stale, from, to, startOf(week)));
  lane.append(h("span", { class: "lane-now" }));
  return lane;
}

// A family's mark beside its lane. The group carries the lane's span in week percent as --units, so in the rail's foot,
// where the lanes stretch to fill the line, both lanes keep one length per percent.
function family(name, mark, lane) {
  const group = h("span", { class: `fam ${name}` }, mark, lane);
  group.style.setProperty("--units", lane.dataset.units);
  return group;
}

// How much of a full seven days a week has used up, in percent, held to 0–100 however the meter reports it. A week
// counted to an earlier end than its reset (SPEC UI-75) reports its share of that shorter span.
function weekGone(week) {
  return (Math.min(100, Math.max(0, week.elapsed)) * week.span) / 100;
}

// Where a read week resets, and where its use reaches, on a lane whose now line stands at 50.
const weekResetEnd = (week) => 50 - weekGone(week) + week.span;
const weekUseEnd = (week) => 50 - weekGone(week) + (week.used * week.span) / 100;

function laneBar(week, stale, from, to, start) {
  const bar = h("span", { class: stale ? "wk stale" : "wk" });
  // Window percent → percent of the lane, and → percent of the bar.
  const lane = (x) => ((Math.min(to, Math.max(from, x)) - from) / (to - from)) * 100;
  const known = week && Number.isFinite(week.elapsed);
  const L = known ? start : from, R = known ? Math.min(to, weekResetEnd(week)) : to;
  bar.style.setProperty("--x", `${lane(L).toFixed(2)}%`);
  bar.style.setProperty("--w", `${(lane(R) - lane(L)).toFixed(2)}%`);
  // A week that resets after the lane's end is cut there; its outline fades out, so it shows no end of its own.
  if (known && weekResetEnd(week) > to) bar.classList.add("cut");
  if (!known || week.used === null) {
    bar.classList.add("unused");
    return bar;
  }
  bar.classList.add(week.used >= 100 ? "full" : week.pace);
  const inBar = (x) => ((Math.min(R, Math.max(L, x)) - L) / (R - L)) * 100;
  const part = (cls, x1, x2) => {
    const piece = h("i", { class: cls });
    piece.style.setProperty("--x", `${inBar(x1).toFixed(2)}%`);
    piece.style.setProperty("--w", `${(inBar(x2) - inBar(x1)).toFixed(2)}%`);
    return piece;
  };
  const end = weekUseEnd(week);
  const striped = Math.min(end, 50);
  if (inBar(striped) > 0) bar.append(part(inBar(striped) >= 99.9 ? "s whole" : "s", L, striped));
  if (end > 50) bar.append(part(inBar(striped) <= 0 ? "a whole" : "a", 50, end));
  return bar;
}

// Small drawings for the foot, in the bars' style: fine outlines over a light tint.
const svgTemplate = document.createElement("template");
function drawing(markup, cls) {
  svgTemplate.innerHTML = `<svg class="${cls}" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">${markup}</svg>`;
  const svg = svgTemplate.content.firstChild;
  svg.remove();
  return svg;
}

// Claude's mark: its rays filled solid in Claude's orange (style.css .fam-mark.claude).
function claudeMark() {
  let rays = "";
  for (let k = 0; k < 8; k++) {
    const long = k % 2 === 0, r1 = long ? 2.5 : 3.1, r2 = long ? 7.4 : 6;
    rays += `<rect x="${(8 + r1).toFixed(2)}" y="7.05" width="${(r2 - r1).toFixed(2)}" height="1.9" rx=".95" transform="rotate(${k * 45 - 90} 8 8)"/>`;
  }
  return drawing(`<g fill="currentColor" stroke="currentColor" stroke-width=".9">${rays}</g><circle cx="8" cy="8" r="1.15" fill="currentColor"/>`, "fam-mark claude");
}

// The mark of one of Claude's models: its first letter in Codex's tinted square, in Claude's orange.
const letterMark = (letter, model) => drawing(`<rect x="1.8" y="2.3" width="12.4" height="11.4" rx="3" fill="currentColor" fill-opacity=".14" stroke="currentColor" stroke-width="1"/><text x="8" y="11.1" text-anchor="middle" font-size="8.5" font-weight="700" fill="currentColor">${letter}</text>`, `fam-mark claude ${model}`);

const codexMark = () => drawing(`<rect x="1.8" y="2.3" width="12.4" height="11.4" rx="3" fill="currentColor" fill-opacity=".14" stroke="currentColor" stroke-width="1"/><path d="M4.9 5.9 7 7.8 4.9 9.7" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/><rect x="8.1" y="9.2" width="3.5" height="1.6" rx=".8" fill="currentColor"/>`, "fam-mark codex");

// The machine load colours (SPEC UI-69, UI-73): one smooth scale from green at 0 to orange at 1. A
// straight blend passes through a greenish yellow, so the colour's strength dips in the middle while its hue turns:
// the scale fades through a muted warm grey. Both themes' colours go on the element and the stylesheet picks one
// (.ramp).
function rampColour(t, dark) {
  const [g, o] = dark ? [[0.76, 0.14, 150], [0.74, 0.18, 54]] : [[0.53, 0.13, 150], [0.62, 0.19, 48]];
  const mix = (a, b, x) => a + (b - a) * x;
  const ease = (x) => x * x * (3 - 2 * x);
  const light = mix(g[0], o[0], t) + (dark ? 0.02 : 0.05) * Math.sin(Math.PI * t);
  const strength = Math.max(0.035, Math.abs(mix(g[1], -o[1], t)));
  return `oklch(${light.toFixed(3)} ${strength.toFixed(3)} ${mix(g[2], o[2], ease(ease(t))).toFixed(1)})`;
}

function ramped(node, t) {
  const x = Math.min(1, Math.max(0, t));
  node.classList.add("ramp");
  node.style.setProperty("--ramp-light", rampColour(x, false));
  node.style.setProperty("--ramp-dark", rampColour(x, true));
  return node;
}

// A window's two lines beside its label: the shares, then when it resets.
function usageWindowLine(limit, reported) {
  if (limit.used === null) return [h("span", { class: "uc-label", text: limit.label }), h("span", { class: "uc-window", text: reported ? "No use since it reset" : "Not reported yet" })];
  return [
    h("span", { class: `uc-label ${limit.used >= 100 ? "full" : limit.pace}`, text: limit.label }),
    h("span", { class: "uc-window" },
      h("span", { text: limit.counted_until
        ? `${limit.used}% used, ${limit.elapsed}% of the time until ${dayWords(new Date(limit.counted_until))} at ${clock(limit.counted_until)} gone`
        : `${limit.used}% used, ${limit.elapsed}% of the window gone` }),
      h("span", { text: `Resets ${inAndAt(limit.resets_at)}` })),
  ];
}

// An account's week in the card: its bar, then its lines. A Claude account with a weekly limit that counts one model's
// use alone (usage.py model_week) shows two named weeks, "All models" and the model's, each with its own bar and lines.
function usageWeeks(week, modelWeeks, reported) {
  if (!modelWeeks.length) return [h("div", { class: "uc-graphic" }, usageBar(week)), h("div", { class: "uc-windows" }, usageWindowLine(week, reported))];
  return h("div", { class: "uc-weeks" }, [{ ...week, label: "All models" }, ...modelWeeks].flatMap((limit) => {
    const [label, words] = usageWindowLine(limit, reported);
    return [label, h("div", { class: "uc-week" }, usageBar(limit), words)];
  }));
}

// The account's banked usage-limit resets (resets.py): how many, and from when and until when each can be used.
// Claude Code offers to spend one when a session reaches a limit.
function bankedLines(resets) {
  if (!resets) return [];
  const grants = resets.grants.filter((grant) => grant.left > 0 && (!grant.ends_at || new Date(grant.ends_at).getTime() > Date.now()));
  if (!grants.length) return [h("span", { class: "uc-dim", text: "No banked usage-limit resets" })];
  const cooldown = resets.cooldown_until && new Date(resets.cooldown_until).getTime() > Date.now() ? resets.cooldown_until : null;
  return grants.map((grant) => {
    let when = "not usable yet";
    if (grant.paused) when = "paused";
    else if (cooldown) when = `usable ${inAndAt(cooldown)}`;
    else if (grant.usable_now) when = "usable now";
    else if (grant.needs_limit) when = "usable once this account reaches a limit";
    const until = grant.ends_at ? `; it expires ${inAndAt(grant.ends_at)}` : "";
    const count = grant.left === 1 ? "1 banked usage-limit reset" : `${grant.left} banked usage-limit resets`;
    return h("span", { class: grant.usable_now && !cooldown ? "uc-good" : "", text: `${count}: ${when}${until}` });
  });
}

function usageCardAccount(account, starts) {
  const seven = weekOf(account);
  const lines = [];
  if (account.at_limit_until) lines.push(h("span", { class: "uc-warn", text: `At its usage limit until ${dayWords(new Date(account.at_limit_until))} at ${clock(account.at_limit_until)}` }));
  if (account.sign_in_ends) {
    const ends = new Date(account.sign_in_ends);
    lines.push(signedOut(account)
      ? h("span", { class: "uc-warn", text: "Signed out: its sign-in has ended. Ask a session to sign it in again." })
      : signInEnding(account)
        ? h("span", { class: "uc-warn", text: `Sign-in ends ${inAndAt(account.sign_in_ends)}. Ask a session to sign it in again.` })
        : h("span", { class: "uc-dim", text: `Signed in until ${dayWords(ends)} at ${clock(account.sign_in_ends)}` }));
  }
  lines.push(...bankedLines(account.resets));
  // The picker counts what they use yet against its week (SESSION-17).
  if (account.running) lines.push(h("span", { class: "uc-dim", text: account.running === 1 ? "1 session running on it" : `${account.running} sessions running on it` }));
  if (!account.captured_at) lines.push(h("span", { class: "uc-dim", text: "Usage not reported yet" }));
  else if (usageStale(account)) lines.push(h("span", { class: "uc-dim", text: `Numbers reported ${ago(account.captured_at)}` }));
  return h("div", { class: starts ? "uc-account starts" : "uc-account" },
    h("div", { class: "uc-who" },
      h("span", { class: "uc-email", text: account.email }),
      h("span", { class: "uc-folder", text: account.folder ? `Claude, ${account.folder}` : "Claude" }),
      starts && h("span", { class: "uc-tag", text: account.preferred_until ? `New sessions start here, your choice until ${clock(account.preferred_until)}` : "New sessions start here" })),
    usageWeeks(seven, account.model_weeks, Boolean(account.captured_at)),
    lines.length > 0 && h("div", { class: "uc-notes" }, lines));
}

// A Codex account's week (codex.py, CodexIndex.weeks): Codex's own weekly limit, apart from Claude's.
function usageCardCodex({ email, folder, week }) {
  const seven = week ? { ...week, label: "7d" } : { used: null, label: "7d" };
  const lines = [];
  if (!week) lines.push(h("span", { class: "uc-dim", text: "No Codex session here has read its meter yet" }));
  else if (usageStale(week)) lines.push(h("span", { class: "uc-dim", text: `Numbers reported ${ago(week.captured_at)}` }));
  if (week) lines.push(...bankedLines(week.resets));
  const entry = ui.state.codex_sign_ins.find((sign) => sign.folder === folder);
  if (entry && entry.problem === "signed_out") lines.push(h("span", { class: "uc-warn", text: "Signed out: sign in from its warning in Inbox" }));
  else if (entry && entry.signing_in) lines.push(h("span", { class: "uc-warn", text: "Signing in: the code is on its warning in Inbox" }));
  return h("div", { class: "uc-account" },
    h("div", { class: "uc-who" },
      h("span", { class: "uc-email", text: email || `~/${folder}` }),
      h("span", { class: "uc-folder", text: email ? `Codex, ~/${folder}` : "Codex" })),
    h("div", { class: "uc-graphic" }, usageBar(seven)),
    h("div", { class: "uc-windows" }, usageWindowLine(seven, Boolean(week))),
    lines.length > 0 && h("div", { class: "uc-notes" }, lines),
    entry && !entry.problem && !entry.signing_in && h("div", { class: "uc-choice uc-codex-sign-in" },
      h("button", { type: "button", "data-focus": `codex-sign-in-${folder}`, onclick: () => prepareCodexSignIn(folder) }, "Sign in again")));
}

function usageCard(lines, starting, codex) {
  return h("div", { class: "usage-card" },
    lines.map((account) => usageCardAccount(account, account === starting)),
    codex.map(usageCardCodex),
    usageChoice(lines));
}

// The user's choice to send new sessions to one account for a while ahead of the usual order (SESSION-17), as on
// the Usage page: one row of lengths per signed-in account, and the way back while a choice holds. The length
// chosen shows filled with a cross and a bar along its bottom for how much of it has passed: pressing it again
// undoes the choice, and another length replaces it.
const PREFER_FOR = [[30, "30 min"], [120, "2 h"], [1440, "1 day"]];

function usageChoice(lines) {
  const folders = lines.filter((account) => account.folder && !signedOut(account));
  if (folders.length < 2) return null;
  const chosen = lines.find((account) => account.preferred_until);
  return h("div", { class: "uc-choice" },
    h("span", { class: "uc-dim", text: chosen ? `You chose ${chosen.folder} for new sessions until ${clock(chosen.preferred_until)}. Press its length again to undo, or another to change it.` : "Choose where new sessions start for a while" }),
    folders.map((account) => h("span", { class: "uc-prefer" },
      h("span", { class: "uc-prefer-name", text: account.folder }),
      h("span", { class: "uc-prefer-for", role: "group", "aria-label": `Start new sessions on ${account.folder} for` },
        PREFER_FOR.map(([minutes, words]) => account.preferred_minutes === minutes
          ? h("button", {
            type: "button", class: "uc-chosen", "aria-pressed": "true", "data-focus": `prefer-${account.folder}-${minutes}`,
            "aria-label": `Undo: start new sessions by the usual order, not on ${account.folder} for ${words}`,
            onclick: () => preferAccount(null, 0, ""),
          }, words, h("span", { class: "uc-undo", "aria-hidden": "true", text: "×" }),
          preferPassed(account, "uc-passed"))
          : h("button", {
            type: "button", text: words, "aria-pressed": "false", "data-focus": `prefer-${account.folder}-${minutes}`,
            "aria-label": `Start new sessions on ${account.folder} for ${words}`,
            onclick: () => preferAccount(account.folder, minutes, words),
          }))))),
    chosen && h("button", {
      type: "button", class: "uc-usual", text: "Back to the usual order", "aria-label": "Back to the usual order",
      onclick: () => preferAccount(null, 0, ""),
    }));
}

// The bar along the bottom of the chosen length's button, filled by how much of that length has passed.
function preferPassed(account, cls) {
  const left = (new Date(account.preferred_until).getTime() - Date.now()) / 60000;
  const strip = h("span", { class: cls, "aria-hidden": "true" });
  strip.style.setProperty("--passed", `${Math.min(100, Math.max(0, 100 * (1 - left / account.preferred_minutes))).toFixed(1)}%`);
  return strip;
}

// The toast shows at once; a refusal replaces it with the reason (run).
let preferring = Promise.resolve();
function preferAccount(account, minutes, words) {
  toast(account ? `New sessions start on ${account} for the next ${words}, unless it is at its usage limit` : "New sessions start by the usual order again");
  preferring = preferring.then(() => run("prefer", () => api("POST", "/api/accounts/prefer", { account, minutes })));
  holdLeave(preferring);
  return preferring;
}

// A part may supply the limits shown in the foot.
let usageGeneration = 0;
function renderUsage(state) {
  usageGeneration++;
  if (window.cockpitPart?.renderUsage?.(state)) return;
  fill("usage", usagePill(state.usage));
}

// The weekly limits (SPEC UI-57, UI-73): Claude's mark and lane, its accounts personal above work, then a hairline and Codex's mark and
// lane, in the order of config.CODEX_HOMES (weekLane); while a Claude sign-in ends soon (UI-37) the amber key follows
// Claude's lane. The words, resets and the choice of where new sessions start are in the hover card.
function usagePill({ accounts, error, codex, machine }) {
  // A failed read of Claude's report shows its words in Claude's place, a path's folders left out ("usage.json
  // is unreadable: …"); the Codex weeks, read apart from it, still show.
  if (error) {
    return [h("span", { class: "usage fail", role: "img", "aria-label": `Claude usage failed: ${error}`, title: `Claude usage: ${error}` },
      icon("alert"), h("span", { class: "usage-error", text: error.replace(/(^|\s)\/\S*\//g, "$1") })),
    usageCaps([], codex, machine)];
  }
  return usageCaps(accounts, codex, machine);
}

function usageCaps(accounts, codex, machine) {
  if (!accounts.length && !codex.length && !machine) return null;
  // The signed-in accounts by folder name, so no line moves when another account becomes the pick. With
  // no folder signed in with any reported address, the latest report alone.
  const signed = accounts.filter((account) => account.folder).sort((a, b) => a.folder.localeCompare(b.folder));
  const lines = signed.length ? signed : accounts.slice(0, 1);
  // The server puts the account new sessions go to first (SPEC SESSION-17), and a signed-out one last: one signed out
  // first means every one is, and new sessions start nowhere.
  const starting = lines.length > 1 && accounts[0].folder && !signedOut(accounts[0]) ? accounts[0] : null;
  const ending = lines.some(signInEnding);
  const scale = ending ? 0.38 : 0.46;
  // A week read with no use since its reset is "unused"; a meter no session has read is not reported.
  const share = (week) => (!week ? "not reported yet" : week.used === null ? "unused" : `${week.used}%`);
  // A Claude account's week for all models, then each weekly limit that counts one model alone: "62% (Fable 38%)".
  const shares = (week, modelWeeks) => `${share(week)}${modelWeeks.length ? ` (${modelWeeks.map((limit) => `${limit.model} ${share(limit)}`).join(", ")})` : ""}`;
  const said = [
    lines.length && `Claude: ${lines.map((account) => `${account.folder || account.email} ${shares(weekOf(account), account.model_weeks)}${signInEnding(account) ? ", sign-in ends soon" : ""}`).join(", ")}`,
    codex.length && `Codex: ${codex.map(({ email, folder, week }) => `${email || folder} ${share(week)}`).join(", ")}`,
  ].filter(Boolean).join("; ");
  // Claude's family with its key, Codex's and the host's lamp, a hairline between each two shown.
  const groups = [
    lines.length > 0 && [family("claude", claudeMark(), weekLane("claude", lines.map((account) => ({ week: weekOf(account), stale: usageStale(account) })), scale)),
      ending && h("span", { class: "acct-key" }, icon("key"))],
    codex.length > 0 && family("codex", codexMark(), weekLane("codex", codex.map(({ week }) => ({ week, stale: !week || usageStale(week) })), scale)),
    machineLamp(machine),
  ].filter(Boolean);
  // A group, not an image, so a screen reader reaches the host's lamp inside it with its own words.
  const pill = h("span", { class: "usage caps", role: "group", tabindex: "0", "aria-label": `Weekly usage. ${said}`, "data-tip-card": "usage" },
    groups.flatMap((group, i) => (i ? [h("span", { class: "fam-sep" }), group] : [group])));
  pill.tipCard = () => usageCard(lines, starting, codex);
  return pill;
}

// The host's load (SPEC UI-69, UI-73, machine.py): a rack of three slots after the lanes, filled as far as the
// processor, memory and disk are used, in one colour on the smooth scale for the busiest of them (machineStrain). Its
// own hover card, which the tooltip picks over the lanes' as the innermost, holds the numbers. A reading that failed
// shows the rack empty and grey. The rack links to the Server page (SPEC TRACK-16), and its card names the three tasks
// that used most in the last minute.
function machineLamp(machine) {
  if (!machine || machine.unavailable) return null;
  const rows = machine.error ? [] : machineRows(machine);
  const said = machine.error ? `could not be read: ${machine.error}` : rows.map(([, words]) => words).join("; ");
  const shares = machine.error ? null : machineShares(machine);
  const lamp = h("a", { class: shares ? "machine-lamp" : "machine-lamp fail", href: "/server", "aria-label": `Host: ${said}. Opens Server.`, "data-tip-card": "machine" },
    machineRack(shares || [0, 0, 0]));
  if (shares) ramped(lamp, machineStrain(machine, shares));
  const busiest = (machine.tasks_now?.tasks || []).slice(0, 3);
  lamp.tipCard = () => h("div", { class: "usage-card machine-card" },
    h("div", { class: "uc-who" }, h("span", { class: "uc-email", text: "Host" }), h("span", { class: "uc-folder", text: "the machine all sessions run on" })),
    machine.error
      ? h("span", { class: "uc-warn", text: `Its load could not be read: ${machine.error}` })
      : h("div", { class: "mc-rows" }, rows.map(([kind, words]) => h("span", { class: `mc-row ${kind}`, text: words }))),
    busiest.length > 0 && h("div", { class: "mc-tasks" },
      h("span", { class: "uc-dim", text: "Using most in the last minute" }),
      busiest.map((task) => h("span", { class: "mc-task" },
        h("span", { class: "mc-task-name", text: task.title || (task.session_id ? `Session ${task.session_id.slice(0, 8)}` : "The machine's own programs") }),
        h("span", { class: "uc-dim", text: `${task.cores < 10 ? task.cores.toFixed(1) : Math.round(task.cores)} cores, ${(task.mem_mb / 1024).toFixed(1)} GB` })))),
    h("span", { class: "uc-dim mc-more", text: "Click for each task's use over time" }));
  return lamp;
}

// How much of the processor, memory and disk is in use, each 0–1; the processor counts as full from one job per core.
// A reading with a figure missing or a total of zero gives null, drawn like a failed reading; the server's log has
// the cause.
function machineShares(machine) {
  const { cores, load, memory_used_gb: used, memory_total_gb: total, disk_free_gb: free, disk_total_gb: disk } = machine;
  if (![cores, load, used, total, free, disk].every(Number.isFinite) || cores <= 0 || total <= 0 || disk <= 0) return null;
  return [Math.min(1, load / cores), used / total, 1 - free / disk];
}

// How busy the host is, 0 (green) to 1 (orange): the busiest of the processor (from half a job per core to three),
// memory (from half full to 95 %) and disk (from three-quarters full to 95 %).
function machineStrain(machine, [, memory, disk]) {
  return Math.max((machine.load / machine.cores - 0.5) / 2.5, (memory - 0.5) / 0.45, (disk - 0.75) / 0.2, 0);
}

function machineRack(shares) {
  return drawing(shares.map((share, i) => {
    const y = 2.2 + i * 4.3;
    return `<rect x="1.7" y="${y}" width="12.6" height="3" rx="1.5" fill="currentColor" fill-opacity=".12" stroke="currentColor" stroke-width=".8"/>`
      + (share > 0 ? `<rect x="1.7" y="${y}" width="${Math.max(3, 12.6 * Math.min(1, share)).toFixed(2)}" height="3" rx="1.5" fill="currentColor"/>` : "");
  }).join(""), "machine-rack");
}

// One line per reading, in its own colour: how far the processor is over or under its cores, memory, disk. The
// processor's words follow the server's level, so they agree with the colour where the rounded load sits on a line.
function machineRows(machine) {
  const times = machine.load / machine.cores;
  const cpu = machine.cpu === "busy"
    ? `Processor: ${times < 10 ? times.toFixed(1) : Math.round(times)}× more work than its ${machine.cores} cores`
    : times >= 1 ? `Processor: all ${machine.cores} cores busy, some work waits`
      : `Processor: about ${Math.round(times * 100)}% of ${machine.cores} cores busy`;
  const swap = machine.swap_used_gb >= 0.5 ? `, ${machine.swap_used_gb} GB moved to swap` : "";
  const diskUsed = Math.round(100 * (1 - machine.disk_free_gb / machine.disk_total_gb));
  return [
    [machine.cpu, cpu],
    [machine.memory, `Memory: ${machine.memory_used_gb} of ${machine.memory_total_gb} GB in use${swap}`],
    [machine.disk, `Disk: ${diskUsed}% full, ${machine.disk_free_gb} GB free`],
  ];
}

// A Claude account that is signed out, or whose sign-in ends soon, shows at the top of the page with a button that
// signs it in again (SPEC UI-37, UI-40). The tab opens inside the user's click, and Claude's page
// loads in it once `claude auth login` prints its link; the warning then takes the code Claude's page shows.
function claudeSignInWarning(entry) {
  const key = `claude:${entry.account}`;
  return {
    key, tone: entry.signed_out ? "fail" : "soon", mark: entry.signed_out ? "alert" : "clock", tip: null,
    short: entry.signed_out ? `Claude ${entry.account} is signed out` : `Claude ${entry.account}'s sign-in ends soon`,
    title: entry.text, more: null,
    fix: { words: ui.busy.has(key) ? "Starting…" : `Sign in to ${entry.account}`, primary: entry.signed_out, busy: ui.busy.has(key),
      onclick: () => startClaudeSignIn(entry.account) },
    close: null, extra: null,
  };
}

// Signing a Codex account in from the page (SPEC UI-77, codex_login.py). The reverse of a Claude sign-in: Cockpit shows
// a code to enter on OpenAI's page, and the state says when Codex saw it. Starting ends the folder's present sign-in,
// so the words below show before the press, on the warning a problem raises or the hover card's Sign in again opens.
const CODEX_START_WARNING = "Starting signs this account out at once: a Codex job that starts or needs a new token before you finish fails its turn, and a code left to run out (15 minutes) leaves it signed out.";
// The folders whose Sign in again was pressed, waiting for Start or Not now.
const codexPrepared = new Set();

function codexSignInKey(folder) {
  return `codex-sign-in:${folder}`;
}

function codexSignInWarning(entry) {
  const { folder, email, held, problem, signing_in: signingIn, doubled, error } = entry;
  const key = codexSignInKey(folder);
  const name = `~/${folder}`;
  const base = { key, tip: null, close: null, extra: null, more: null };
  const busy = ui.busy.has(key);
  const start = (words, primary) => ({ words: busy ? "Starting…" : words, primary, busy, onclick: () => startCodexSignIn(folder) });
  if (signingIn) {
    return { ...base, tone: "soon", mark: "clock", short: `Signing in Codex ${name}`,
      title: `Enter this code on OpenAI's page to sign in Codex ${name}`, extra: signingIn.code ? codexSignInCode(entry) : null };
  }
  const startWarning = email ? ` ${CODEX_START_WARNING}` : "";
  const failed = error ? `The last sign-in ended: ${error} ` : "";
  if (problem === "signed_out") {
    const who = held || email;
    return { ...base, tone: "fail", mark: "alert", short: `Codex ${name} is signed out`,
      title: `Codex ${name}${who ? ` (${who})` : ""} is signed out`,
      more: `${failed}Codex jobs on this account fail until it signs in.${startWarning}`,
      fix: start(`Sign in to ${name}`, true) };
  }
  if (problem === "other_account") {
    return { ...base, tone: "fail", mark: "alert", short: `Codex ${name} holds another account`,
      title: doubled ? `Codex ${name} holds ${email}, which another Codex folder holds too` : `Codex ${name} now holds ${email}; it held ${held}`,
      more: `${doubled ? "Sign it in with another account." : `Sign in again with ${held}, or keep ${email} if you meant it.`}${startWarning}`,
      fix: start("Sign in again", true),
      close: doubled ? null : { words: `Keep ${email}`, busy: ui.busy.has(`${key}:keep`), onclick: () => keepCodexAccount(folder) } };
  }
  if (!codexPrepared.has(folder)) return null;
  return { ...base, tone: "soon", mark: "alert", short: `Sign Codex ${name} in again`,
    title: `Sign Codex ${name} in again?`, more: `It holds ${email}. ${CODEX_START_WARNING}`,
    fix: start("Start sign-in", true),
    close: { words: "Not now", onclick: () => { codexPrepared.delete(folder); render(false); } } };
}

// OpenAI's link and code. The code sits in a field marked as a credential, which the feedback picker and a GPU close
// leave out (pick.js, close-gpu.js).
function codexSignInCode({ folder, held, signing_in: { url, code, ends_at: endsAt } }) {
  return signInCodeBlock(`Code for Codex ~/${folder}`, url, code,
    `${held ? `Sign in with ${held}: OpenAI signs in whichever account your browser has open. ` : ""}The code ends at ${clock(endsAt)}. This card closes when Codex sees it.`);
}

function signInCodeBlock(label, url, code, note) {
  const field = h("input", { class: "field codex-code", type: "text", readonly: true, "data-credential": true, autocomplete: "off",
    spellcheck: "false", "aria-label": label, onfocus: (event) => event.target.select() });
  field.value = code;
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      toast("Copied the code");
    } catch (error) {
      logError("copying the Codex code", error);
      toast("The code did not copy: select it and copy it by hand", true);
    }
  };
  // Opening OpenAI's page copies the code in the same press, so the code is ready to paste there.
  return h("div", { class: "codex-sign-in" },
    field,
    h("div", { class: "codex-sign-in-keys" },
      h("a", { class: "button primary", href: url, target: "_blank", rel: "noopener noreferrer", text: "Copy the code and open OpenAI's page", onclick: copy }),
      h("button", { class: "button ghost", type: "button", onclick: copy }, "Copy the code")),
    h("p", { class: "codex-sign-in-note", text: note }));
}

// A warning an install's part gives (the state's `part_warnings`, absent without parts, parts.py): its own key, tone
// ("fail" or "soon"), words, and the page its one press opens.
function partWarning(entry) {
  return {
    key: entry.key, tone: entry.tone, mark: entry.tone === "fail" ? "alert" : "clock",
    tip: null, short: entry.text, title: entry.text, more: entry.more,
    fix: { words: entry.fix.words, primary: true, busy: false, onclick: () => { location.href = entry.fix.href; } },
    close: null, extra: null,
  };
}

function startCodexSignIn(folder) {
  const key = codexSignInKey(folder);
  return run(key, async () => {
    try {
      await api("POST", `/api/codex/${encodeURIComponent(folder)}/sign-in`);
    } catch (error) {
      throw refused(`The sign-in of Codex ~/${folder} did not start`, error);
    }
    codexPrepared.delete(folder);
  });
}

function keepCodexAccount(folder) {
  return run(`${codexSignInKey(folder)}:keep`, async () => {
    try {
      await api("POST", `/api/codex/${encodeURIComponent(folder)}/keep`);
    } catch (error) {
      throw refused(`Codex ~/${folder} did not change`, error);
    }
  });
}

// Whether the user just asked for this sign-in, which an older snooze of its warning leaves showing.
function codexSignInAsked(state, key) {
  return (state.codex_sign_ins || []).some((entry) => codexSignInKey(entry.folder) === key && (entry.signing_in || codexPrepared.has(entry.folder)));
}

// The hover card's Sign in again, there while the account still works: the warning with Start opens first.
function prepareCodexSignIn(folder) {
  codexPrepared.add(folder);
  if (SIDEBAR) openWarning(codexSignInKey(folder));
  else render(false);
}

// A sign-in that finished since the last state: which account the folder held before and holds now. A mismatch also
// stays as a warning (codex_login.status).
const codexFinishedSeen = new Map();
let codexFinishedRead = false;

function noticeCodexSignIns(state) {
  for (const { folder, finished, problem, signing_in: signingIn } of state.codex_sign_ins) {
    // A problem or a login under way takes over from a Sign in again still open, which must not return after it.
    if (problem || signingIn) codexPrepared.delete(folder);
    if (!finished || codexFinishedSeen.get(folder) === finished.at) continue;
    codexFinishedSeen.set(folder, finished.at);
    if (!codexFinishedRead) continue;
    const { before, after } = finished;
    toast(before === after ? `Signed in: Codex ~/${folder} holds ${after}, as before` : `Signed in: Codex ~/${folder} held ${before || "no account"} and now holds ${after || "no account"}`, before !== after);
  }
  codexFinishedRead = true;
}

function startClaudeSignIn(account) {
  if (ui.busy.has(`claude:${account}`)) return;
  const tab = window.open("", "_blank");
  const started = startSignIn(`claude:${account}`);
  return run(`claude:${account}`, async () => {
    let url, attempt;
    try {
      ({ url, attempt } = await api("POST", `/api/claude/${encodeURIComponent(account)}/sign-in`));
      if (tab) { tab.opener = null; tab.location.href = url; }
    } catch (error) {
      if (tab) tab.close();
      throw refused(`The ${account} account's sign-in did not start`, error);
    }
    if (started !== signInStarts) return;
    waitForCode({
      key: `claude:${account}`, url, page: "Claude's page", label: "Code from Claude's page",
      send: async (code) => {
        await apiCredential(`/api/claude/${encodeURIComponent(account)}/code`, { attempt, code });
        return `Signed in to the ${account} account. Sessions that answered "Login expired" continue within a minute.`;
      },
    });
  });
}

// Signing in on the page (SPEC UI-40, UI-53): once the sign-in page's link is known, the warning it belongs to
// takes the code itself, on the card where it opens (the sidebar page's Next up, the classic page's banner), with no
// dialog over the page. `ui.signIn` is the one sign-in waiting: its warning's `key`, the page's `url` and name, the
// field's `label`, `send(code)`, which resolves to the words that say it signed in, and the `code` typed so far, which a redraw puts
// back and which never goes to the browser's storage, being a credential that works once. Only the sign-in pressed
// last waits (`signInStarts`). The field has the focus as the card shows, and again when the user comes back to
// Cockpit's tab. One code per sign-in: signed in or refused, the form goes, and the next try starts over from the
// warning.
let signInStarts = 0;
// The warning key of the sign-in pressed last, which a snooze of that warning drops while it starts (`snoozeWarning`).
let signInStarting = null;

// A new sign-in ends the one waiting at once: Cockpit ends its login process, so its code would only be refused.
function startSignIn(key) {
  if (ui.signIn && !ui.signIn.sending) {
    ui.signIn = null;
    render(false);
  }
  signInStarting = key;
  return ++signInStarts;
}

function waitForCode({ key, url, page, label, send }) {
  ui.signIn = { key, url, page, label, send, code: "", sending: false };
  if (SIDEBAR) openWarning(key);
  else render(false);
  focusSignInCode();
}

function signInKey(key) {
  return `sign-in:${key}`;
}

function focusSignInCode() {
  const field = document.querySelector(".sign-in-code");
  if (field && !field.readOnly) field.focus();
}

// The warning with its sign-in waiting: the form in place of its fix.
function withSignIn(warning) {
  const signIn = ui.signIn;
  if (!signIn || signIn.key !== warning.key) return warning;
  return { ...warning, fix: null, extra: signInForm(signIn) };
}

function signInForm(signIn) {
  const field = h("input", {
    // A credential the user can read as they paste it, which the feedback picker and a GPU close leave out (pick.js).
    class: "field sign-in-code", type: "text", "data-credential": true, "data-key": signInKey(signIn.key), placeholder: signIn.label,
    "aria-label": signIn.label, autocomplete: "off", spellcheck: "false", readonly: signIn.sending,
  });
  field.value = signIn.code;
  const signInButton = h("button", { class: "button primary", type: "submit", disabled: signIn.sending || !signIn.code.trim() },
    signIn.sending ? "Signing in…" : "Sign in");
  field.addEventListener("input", () => {
    signIn.code = field.value;
    signInButton.disabled = !field.value.trim();
  });
  const cancel = () => {
    if (ui.signIn === signIn && !signIn.sending) ui.signIn = null;
    render(false);
  };
  const form = h("form", { class: "sign-in-form", onsubmit: (event) => { event.preventDefault(); sendSignInCode(signIn); } },
    h("a", { class: "sign-in-page", href: signIn.url, target: "_blank", rel: "noopener noreferrer", text: `Open ${signIn.page}` }),
    h("div", { class: "sign-in-row" }, field, signInButton,
      h("button", { class: "button ghost", type: "button", disabled: signIn.sending, onclick: cancel }, "Cancel")));
  form.addEventListener("keydown", (event) => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); cancel(); } });
  return form;
}

async function sendSignInCode(signIn) {
  const code = signIn.code.trim();
  if (!code || signIn.sending || ui.signIn !== signIn) return;
  signIn.sending = true;
  render(false);
  let words = null, failure = null;
  try {
    words = await signIn.send(code);
  } catch (error) {
    logError(`sign-in ${signIn.key}`, error);
    failure = error;
  }
  if (ui.signIn === signIn) ui.signIn = null;
  if (failure) toast(failure.message, true, failure.retry || null, failure.tip);
  else toast(words);
  // The form goes now: the state from the server may hold nothing new to draw.
  render(false);
  ui.actionRender = true;
  refresh();
}

// The last sweep, full or light (its summary starts "Full:" or "Light:"): `hint` beside "Full sweep
// now", `line` for the tooltips, `detail` for the sweep menu's tooltip, `time` for the exact time, and
// whether it failed.
function sweepInfo(state) {
  const sweep = state.last_sweep;
  if (state.sweep_running) {
    const since = clock(sweep.started_at);
    return { hint: `since ${since}`, line: `A sweep is running since ${since}`, detail: `Started by ${actorWords(sweep.by)}`, time: sweep.started_at, failed: false };
  }
  if (sweep && sweep.error) {
    return { hint: `failed ${age(sweep.finished_at)}`, line: `The last sweep failed ${ago(sweep.finished_at)}`, detail: sweep.error, time: sweep.finished_at, failed: true };
  }
  if (sweep && sweep.finished_at) {
    return { hint: age(sweep.finished_at), line: `Last sweep ${ago(sweep.finished_at)}`, detail: sweep.summary || "", time: sweep.finished_at, failed: false };
  }
  if (sweep) {
    return { hint: "stopped", line: "The last sweep stopped without finishing", detail: `Started ${clock(sweep.started_at)} by ${actorWords(sweep.by)}`, time: sweep.started_at, failed: true };
  }
  return { hint: "none yet", line: "No sweep yet", detail: "", time: null, failed: false };
}

// The slowest sweep of each kind over the last week, as one line (SPEC SWEEP-16, UI-46): "Slowest in 7 days:
// full 9 min, light 5 min". `cockpit patterns` gives the counts and medians.
function sweepTimesWords(state) {
  const span = (seconds) => seconds < 60 ? `${seconds} s` : `${Math.round(seconds / 60)} min`;
  const kinds = [["full", "full"], ["light", "light"], ["fill", "quick"], ["reorder", "reorder"]].filter(([kind]) => state.sweep_times[kind]);
  if (!kinds.length) return "";
  return `Slowest in ${state.sweep_time_days} days: ${kinds.map(([kind, name]) => `${name} ${span(state.sweep_times[kind].slowest_seconds)}`).join(", ")}`;
}

// Whether the sweeper checks this step: it is among the first places of the user's queue.
function isTop(step, state) {
  return state.queue.slice(0, state.check_top).some((top) => top.id === step.id);
}

function checkedTitle(step) {
  if (!step.checked_at) return "Not checked against live state yet";
  return `Checked ${ago(step.checked_at)} by ${actorWords(step.checked_by)}${step.check_note ? `: ${step.check_note}` : ""}`;
}

// When a top step was last checked against live state, as a mark: a quiet check within the check
// interval, a clock once a check is overdue, a blue square while one runs, and no mark before the
// first check. The hover says when, by whom and what was checked; `ready`, what the agent made ready, or "".
// Next up's info mark says when too.
function checkedMark(step, state, ready) {
  const checking = Boolean(state.checking && state.checking.step_ids.includes(step.id));
  const checkedAge = step.checked_at ? Date.now() - new Date(step.checked_at).getTime() : null;
  const kind = checking ? "checking" : checkedAge === null ? "none" : checkedAge < state.check_every_minutes * 60e3 ? "fresh" : "stale";
  if (kind === "none") return null;
  const words = checking ? "Checking now" : kind === "stale" ? `Check overdue: checked ${ago(step.checked_at)}` : `Checked ${ago(step.checked_at)}`;
  const title = [
    checking ? "Checking now" : kind === "stale" ? "Check overdue" : null,
    !checking && checkedTitle(step),
    kind === "stale" && `A check is due every ${state.check_every_minutes} min`,
    ready && `Ready: ${ready}`,
  ].filter(Boolean).join("\n");
  return h("span", { class: `checked ${kind}`, role: "img", "aria-label": words, title, "data-time": step.checked_at || null },
    kind === "fresh" ? icon("check") : kind === "stale" ? icon("clock") : null);
}

// When a top step was last checked, in words for the step's ⋯ menu: "Checking now", "Check overdue: …" or "Checked …".
function checkWords(step, state) {
  if (state.checking && state.checking.step_ids.includes(step.id)) return "Checking now";
  if (!step.checked_at) return checkedTitle(step);
  const overdue = Date.now() - new Date(step.checked_at).getTime() >= state.check_every_minutes * 60e3;
  return overdue ? `Check overdue: ${checkedTitle(step).replace(/^C/, "c")}` : checkedTitle(step);
}

// Update is an icon with a dot for how fresh the check of the top steps is; its hover is short: what it
// does and where the check stands, how long a running one has run (SPEC SWEEP-6). The menu beside it runs
// a full sweep and says when the last sweep, full or light, ran. A failed sweep turns that menu red.
// Sweeps stopped while the user was away show as one button, Restart sweeps (SPEC SWEEP-11): in a "Sweeps paused"
// line above the rail's foot, as an icon in the top bar on /classic. Writing on Cockpit starts them again too.
function renderSweep(state) {
  const resume = document.getElementById("resume-sweeps");
  resume.hidden = !state.sweeps_paused;
  resume.disabled = ui.busy.has("resume");
  resume.title = state.sweeps_paused ? `Restart sweeps: they stopped at ${clock(state.sweeps_paused.at)} while you were away` : "";
  const pausedLine = document.getElementById("sweeps-paused");
  if (pausedLine) {
    pausedLine.hidden = !state.sweeps_paused;
    pausedLine.title = state.sweeps_paused ? `Sweeps stopped at ${clock(state.sweeps_paused.at)} while you were away` : "";
  }
  const sweep = sweepInfo(state);
  const starting = ui.busy.has("update");
  const blocked = updateBlocked(state);
  const sweepMenu = menu("menu:sweep", icon("chevron"), [[[icon("radar"), "Full sweep now"], sweep.hint, fullSweep]], "sweep");
  const summary = sweepMenu.querySelector("summary");
  summary.className = `button sweep-summary${sweep.failed ? " fail" : ""}`;
  summary.setAttribute("aria-label", "Full sweep");
  // A failed sweep's error is the one detail worth a line; a finished sweep's own summary is the sweeper's shorthand.
  summary.title = [sweep.line, sweep.failed ? clip(sweep.detail, 60) : "", sweepTimesWords(state)].filter(Boolean).join("\n");
  if (sweep.time) summary.dataset.time = sweep.time;
  const entry = sweepMenu.querySelector(".menu-list button");
  entry.disabled = blocked;
  if (sweep.failed) entry.querySelector(".when").classList.add("fail-text");
  fill("sweep-status", sweepMenu);

  const top = state.queue.slice(0, state.check_top);
  const checks = top.map((step) => step.checked_at).filter(Boolean).sort();
  const checked = top.length > 0 && checks.length === top.length;
  // The lamp bar on Update: blue while busy, green while the top steps' oldest check is within the
  // check interval, unlit after it or when one of them was never checked.
  let sub;
  let dotKind;
  if (starting) { sub = "Starting"; dotKind = "busy"; }
  else if (state.sweep_running) { sub = `A sweep has run for ${runningFor(state.last_sweep.started_at)}`; dotKind = "busy"; }
  else if (state.checking) { sub = `Checking the top steps: ${runningFor(state.checking.since)} so far`; dotKind = "busy"; }
  else if (!top.length) { sub = "Your queue is empty: Update runs a full sweep"; dotKind = "none"; }
  else if (!checked) { sub = "Not all top steps are checked yet"; dotKind = "none"; }
  else {
    const fresh = Date.now() - new Date(checks[0]).getTime() < state.check_every_minutes * 60e3;
    sub = `Top steps checked ${ago(checks[0])}`;
    dotKind = fresh ? "fresh" : "stale";
  }
  const button = document.getElementById("update");
  button.disabled = blocked;
  button.classList.toggle("busy", blocked);
  document.getElementById("update-sub").className = `update-sub ${dotKind}`;
  button.title = sub;
  updateFinished(state);
}

// A failed poll turns the live dot red at once; the banner follows after a minute without a good poll,
// so one dropped request does not flash red across the page.
const BANNER_AFTER_MS = 60e3;

function wrongModelWarning(wrong) {
  const intended = wrong.intended;
  const name = wrong.name.length > 60 ? `${wrong.name.slice(0, 59)}…` : wrong.name;
  const calls = wrong.calls === 1 ? "1 call" : `${wrong.calls} calls`;
  const key = `wrong-model:${wrong.session_id}:${wrong.model}`;
  return {
    key, tone: "fail", mark: "alert", tip: `Session ${wrong.session_id}, from ${when(new Date(wrong.first))}`,
    short: `“${name}” ran on ${wrong.model}`,
    title: `Session “${name}” ran on ${wrong.model} instead of ${intended}: ${calls}, the last at ${when(new Date(wrong.last))}.`,
    more: wrong.back ? `It is back on ${intended}.` : `/model ${intended} in that session moves it back.`,
    fix: { words: ui.busy.has(key) ? "Starting…" : "Investigate", busy: ui.busy.has(key),
      onclick: () => investigateWrongModel(wrong) },
    close: { title: "Stays closed while the session stays on this model", busy: ui.busy.has(key),
      onclick: () => dismissWrongModel(wrong) },
    extra: null,
  };
}

// A card a session put for what is not a step on a task, such as a meeting that starts soon. It draws like a warning, above
// every step and in none of the four places, in a calm colour rather than red, as nothing is broken.
function topCardWarning(card) {
  const key = `top:${card.id}`;
  const answer = (words) => run(key, async () => {
    await api("POST", `/api/top-cards/${card.id}/done`, words === null ? {} : { answer: words });
    ui.state.top_cards = ui.state.top_cards.filter((row) => row.id !== card.id);
    render(false);
  });
  const busy = ui.busy.has(key);
  return {
    key, tone: "top", mark: "shield", tip: null, short: card.title, title: card.title, noun: "card",
    more: card.body || null, fix: null,
    close: card.answers.length ? null : { words: "Done", title: "Takes this card down", busy, onclick: () => answer(null) },
    extra: card.answers.length ? h("div", { class: "answers", role: "group", "aria-label": "Answers" }, card.answers.map((words) =>
      h("button", { class: "button answer", type: "button", disabled: busy, onclick: () => answer(words) },
        h("span", { class: "answer-words", text: words })))) : null,
    row: { "data-top": card.id },
  };
}

// Tell Cockpit once that a top card was on a screen the user had in front of them, so its sender can
// fall back to another channel only for a card nobody saw. A hidden tab, a snoozed card or one drawn nowhere counts
// as unseen. A failed report is tried again on the next redraw.
function markTopCardsSeen(state) {
  if (document.visibilityState !== "visible") return;
  for (const card of state.top_cards || []) {
    if (card.seen_at || ui.topSeen.has(card.id)) continue;
    const drawn = [...document.querySelectorAll(`[data-warning="top:${card.id}"]`)]
      .some((el) => !el.classList.contains("warn-snoozed") && el.getClientRects().length > 0);
    if (!drawn) continue;
    ui.topSeen.add(card.id);
    api("POST", `/api/top-cards/${card.id}/seen`, {}).catch(() => ui.topSeen.delete(card.id));
  }
}

// The close on a wrong-model banner: Cockpit keeps it, so every tab and device drops the banner while the session
// stays on that model (SPEC SESSION-20).
function dismissWrongModel(wrong) {
  return run(`wrong-model:${wrong.session_id}:${wrong.model}`, async () => {
    await api("POST", "/api/wrong-models/dismiss", { session_id: wrong.session_id, model: wrong.model });
    // By session and model, since a poll that landed meanwhile replaced the rows.
    ui.state.wrong_models = ui.state.wrong_models.filter((row) => row.session_id !== wrong.session_id || row.model !== wrong.model);
    render(false);
  });
}

// Investigate on a wrong-model banner: Cockpit adds a to-do that asks a new session in Cockpit's checkout why that
// session changed model, and closes the banner, as the to-do now follows it (SPEC SESSION-20).
function investigateWrongModel(wrong) {
  return run(`wrong-model:${wrong.session_id}:${wrong.model}`, async () => {
    const item = await api("POST", "/api/wrong-models/investigate", { session_id: wrong.session_id, model: wrong.model });
    // By session and model, since a poll that landed meanwhile replaced the rows.
    ui.state.wrong_models = ui.state.wrong_models.filter((row) => row.session_id !== wrong.session_id || row.model !== wrong.model);
    render(false);
    toast("A new session is investigating", false, { label: "Open", run: () => openAdded(item.id) });
  });
}

// What is broken now or needs seeing to soon, red before amber (SPEC UI-52), each as one description that the page
// draws in its places: on the sidebar page a row in Your queue, the card on Next up and the small card under the
// current step's change-order arrows; on the classic page a card at the top. `key` names it while it lasts, `short`
// is the few words a row and the small card show, `tip` its hover text, `fix` the button that fixes it, `close` how it closes
// (null for one that goes only once fixed), `extra` what goes under its words. Red is something broken now, amber something to see to soon.
// A top card comes first, as what the user should do now.
const WARN_TONES = ["top", "fail", "soon", "question"];

function warningList(state) {
  return allWarnings(state).filter((warning) => !warningSnoozedUntil(state, warning.key) && !warningHanded(state, warning.key));
}

// The warnings that still hold but that the user snoozed (SPEC UI-72), the soonest to wake first, each with its
// `snoozedUntil`: Waiting lists them under Snoozed by you, as it does snoozed steps.
function snoozedWarnings(state) {
  return allWarnings(state).map((warning) => ({ ...warning, snoozedUntil: warningSnoozedUntil(state, warning.key) }))
    .filter((warning) => warning.snoozedUntil && !warningHanded(state, warning.key))
    .sort((a, b) => a.snoozedUntil.localeCompare(b.snoozedUntil));
}

// Until when a warning is snoozed, or null. A snooze or Undo on its way counts already, so a poll answered before
// Cockpit has it does not move the row back; a sign-in the user started brings the warning back at once.
function warningSnoozedUntil(state, key) {
  const until = ui.snoozing.has(key) ? ui.snoozing.get(key).until : (state.snoozed_warnings || {})[key];
  if (!until || new Date(until).getTime() <= Date.now() || signInWaitsOn(state, key)) return null;
  return until;
}

// Whether the user's message on a warning handed it to the to-do it made (SPEC UI-82, store.handed_warnings): the
// warning stays out of Inbox while that to-do is open. A message on its way counts already; a sign-in waiting on the
// warning brings it back, as it does a snoozed one.
function warningHanded(state, key) {
  return (ui.handing.has(key) || key in (state.handed_warnings || {})) && !signInWaitsOn(state, key);
}

// Whether a sign-in waits on the warning's card, including one an install's part starts.
function signInWaitsOn(state, key) {
  return (ui.signIn && ui.signIn.key === key) || codexSignInAsked(state, key)
    || Boolean(window.cockpitPart?.signInWaitsOn?.(state, key));
}

// Every warning that holds now, snoozed or not.
function allWarnings(state) {
  // Signed-out Claude accounts appear first because their sessions need sign-in to continue.
  const list = state.claude_sign_ins.map(claudeSignInWarning);
  list.push(...(state.top_cards || []).map(topCardWarning));
  list.push(...state.codex_sign_ins.map(codexSignInWarning).filter(Boolean));
  list.push(...(state.part_warnings || []).map(partWarning));
  list.push(...(window.cockpitPart?.warnings?.(state) || []));
  // A session that answered on another model than the one every session runs on, for a day after (SPEC SESSION-20).
  list.push(...state.wrong_models.map(wrongModelWarning));
  list.push(...blockedWarnings(state));
  list.push(...state.warnings.map((warning) => ({
    key: `warning:${warning}`, tone: "soon", mark: "alert", tip: null, short: warning, title: warning, more: null,
    fix: null, close: null, extra: null,
  })));
  // What an account shares, in plain words, with the paths collapsed (SPEC SESSION-19).
  list.push(...state.account_problems.map((problem) => ({
    key: `account-problem:${problem.text}`, tone: "soon", mark: "alert", tip: null, short: problem.text, title: problem.text,
    more: null, fix: null, close: null,
    extra: disclosure(`account-problem:${problem.text}`, "more warn-detail", "Details",
      problem.detail.split("\n").map((line) => h("div", { class: "warn-paths", text: line })), false),
  })));
  // A top card, then red before amber; within a colour, the order above. Array sort is stable.
  return list.map(withSignIn).sort((a, b) => WARN_TONES.indexOf(a.tone) - WARN_TONES.indexOf(b.tone));
}

// A warning's ⋯ menu on its card (SPEC UI-72) holds Snooze with a step's times and "Other time…"; a time asks once more
// in the menu, saying what stays broken meanwhile, before the warning moves to Waiting's Snoozed by you. A snoozed
// warning's menu holds Unsnooze, as a snoozed step's does. A blocked call's card has its own controls and no menu.
function warningMenu(warning) {
  const key = `menu:warning:${warning.key}`;
  const snoozeKey = `${key}:snooze`;
  const el = h("details", { class: "menu step-menu warn-menu" });
  el.open = ui.open.has(key);
  const forget = () => { ui.open.delete(key); ui.open.delete(snoozeKey); ui.confirmSnooze.delete(key); };
  rememberOpen(el, (open) => { if (open) ui.open.add(key); else forget(); });
  const close = () => {
    forget();
    el.open = false;
    el.querySelector(":scope > summary")?.focus({ preventScroll: true });
  };
  const list = h("div", { class: "menu-list", role: "menu", "aria-label": "Snooze and more" });
  // Only the list's contents change, so the page's outside-click check still finds the menu on the press's path.
  const fill = () => {
    const hours = ui.confirmSnooze.get(key);
    let snooze;
    if (warning.snoozedUntil) {
      snooze = [h("button", { type: "button", role: "menuitem", onclick: () => { close(); unsnoozeWarning(warning); } },
        h("span", { class: "menu-label" }, icon("clock"), "Unsnooze"))];
    } else if (hours !== undefined) {
      const [words] = snoozeTimes()[hours];
      snooze = [h("div", { class: "warn-snooze-confirm", role: "group", "aria-label": "Confirm the snooze" },
        h("p", { class: "menu-note", text: warning.noun ? `Hide this ${warning.noun} for ${words}?` : `Hide this warning for ${words}? ${warning.more || "The problem stays until it is fixed."}` }),
        h("div", { class: "warn-snooze-keys" },
          // Counted from the confirming press, not from the first.
          h("button", { type: "button", class: "button small primary", role: "menuitem",
            onclick: () => { close(); snoozeWarning(warning, isoUtc(snoozeTimes()[hours][1]), `for ${words}`, null); } }, `Snooze ${words}`),
          h("button", { type: "button", class: "button small ghost", role: "menuitem",
            onclick: () => { ui.confirmSnooze.delete(key); fill(); list.querySelector(".menu-choices [role=menuitem]")?.focus(); } }, "Cancel")))];
    } else {
      const shown = ui.open.has(snoozeKey);
      const choices = h("div", { class: "menu-choices", role: "group", "aria-label": "Snooze for", hidden: !shown },
        snoozeTimes().map(([words, date], index) => h("button", {
          type: "button", role: "menuitem", title: `Until ${when(date)}`,
          onclick: () => { ui.confirmSnooze.set(key, index); fill(); list.querySelector(".warn-snooze-confirm .primary")?.focus(); },
        }, h("span", { class: "menu-label", text: words }), h("span", { class: "when", text: when(date) }))),
        // The dialog, which names what stays broken, is the confirmation here.
        h("button", { type: "button", role: "menuitem", onclick: () => {
          close();
          openWarningSnoozeWords(warning, "");
        } }, h("span", { class: "menu-label", text: "Other time…" })));
      const opener = h("button", {
        type: "button", class: "menu-opens", role: "menuitem", "aria-expanded": String(shown),
        onclick: () => {
          const open = choices.hidden;
          choices.hidden = !open;
          opener.setAttribute("aria-expanded", String(open));
          if (open) ui.open.add(snoozeKey); else ui.open.delete(snoozeKey);
        },
      }, h("span", { class: "menu-label" }, icon("clock"), "Snooze"), h("span", { class: "when" }, icon("chevron-right")));
      snooze = [opener, choices];
    }
    list.replaceChildren(h("div", { class: "menu-group", role: "none" }, snooze));
  };
  fill();
  // A menu closed without a redraw opens again with Snooze's times folded, as `forget` dropped their key.
  el.addEventListener("toggle", () => { if (!el.open) fill(); });
  const name = warning.snoozedUntil ? "Unsnooze" : "Snooze";
  el.append(h("summary", { class: "button icon-only step-menu-key", title: name, "aria-label": name }, icon("more")), list);
  return el;
}

// "Other time…" on a warning: the dialog, which names what stays broken, is the confirmation here.
function openWarningSnoozeWords(warning, words) {
  openSnoozeWords(warning.short, warning.noun ? `The ${warning.noun} moves to Waiting until then.` : `The warning moves to Waiting until then. ${warning.more || "The problem stays until it is fixed."}`,
    words, (until, typed) => {
      noteWordRead(`warning:${warning.key}`, typed);
      snoozeWarning(warning, until, `while Cockpit reads “${typed.text}”`, typed);
    });
}

// Snoozes a warning on every page until `until` (store.snooze_warning), with Undo in the toast; `phrase` says how
// long: "for 2 hours", "while Cockpit reads “friday”". `words`, or null, are the user's words for the time, which
// Cockpit reads to move the snooze (`openSnoozeWords`). The card then shows what it would with the warning gone, as
// after a step's snooze.
function snoozeWarning(warning, until, phrase, words) {
  if (ui.warningOpen === warning.key) ui.warningOpen = null;
  const snoozed = setWarningSnooze(warning, until, words);
  toast(`Snoozed “${warning.short}” ${phrase}`, false, { label: "Undo", run: () => setWarningSnooze(warning, null, null) });
  return snoozed;
}

// Brings a snoozed warning back to Inbox now; the card stays on it.
function unsnoozeWarning(warning) {
  const woken = setWarningSnooze(warning, null, null);
  toast(`Unsnoozed “${warning.short}”`, false, { label: "Undo", run: () => setWarningSnooze(warning, warning.snoozedUntil, null) });
  return woken;
}

// The last change sent for each warning, and a count that names each change's action.
const warningSnoozeSends = new Map();
let warningSnoozeCount = 0;

// Each change waits for the one before it on the same warning, so an Unsnooze pressed while its snooze is on its way
// reaches Cockpit after it, and the last press is the one Cockpit keeps. While a change waits in the page's memory
// only, leaving the page asks first. A confirmed change stays in `ui.snoozing` until a state asked after it arrives
// (`refresh`), so an older poll does not move the row back.
function setWarningSnooze(warning, until, words) {
  if (!words) wordReads.delete(`warning:${warning.key}`);
  // A snooze, also an Unsnooze's Undo, ends a sign-in waiting on this warning's card: the user chose to leave it for
  // now. One still starting is dropped too, so its late answer does not open the card again (`startSignIn`).
  if (until) {
    if (ui.signIn && ui.signIn.key === warning.key && !ui.signIn.sending) ui.signIn = null;
    if (signInStarting === warning.key) { signInStarts++; signInStarting = null; }
  }
  const change = { until, confirmed: null };
  ui.snoozing.set(warning.key, change);
  render(false);
  warningSnoozeCount += 1;
  const id = warningSnoozeCount;
  const before = warningSnoozeSends.get(warning.key);
  const sent = (before || Promise.resolve()).then(() => run(`snooze:${warning.key}:${id}`, async () => {
    try {
      const answer = await api("POST", "/api/warnings/snooze", words ? { key: warning.key, until, words } : { key: warning.key, until });
      ui.state.snoozed_warnings = answer.snoozed_warnings;
      change.confirmed = ++ui.tick;
    } catch (error) {
      if (ui.snoozing.get(warning.key) === change) ui.snoozing.delete(warning.key);
      throw error;
    }
  }));
  if (before) holdLeave(sent);
  warningSnoozeSends.set(warning.key, sent);
  sent.then(() => { if (warningSnoozeSends.get(warning.key) === sent) warningSnoozeSends.delete(warning.key); });
  return sent;
}

// A warning as a card at the top of the classic page, and the page's own refresh failure on both pages.
function warnCard({ key, tone, mark, tip, title, more, fix, close, extra }) {
  return h("article", { class: `card warn-card ${tone}`, title: tip, "data-warning": key },
    icon(mark),
    h("div", { class: "warn-text" },
      h("p", { class: "warn-title", text: title }),
      more && h("p", { class: "warn-more", text: more })),
    fix && h("div", { class: "warn-actions" }, fixButton(fix), close && close.words && h("button", {
      class: "button small ghost", type: "button", disabled: close.busy, onclick: close.onclick }, close.words)),
    close && !close.words && h("div", { class: "warn-close" }, h("button", {
      class: "button small ghost icon-only", type: "button", "aria-label": "Close this warning", title: close.title,
      disabled: close.busy, onclick: close.onclick }, icon("x"))),
    extra && h("div", { class: "warn-extra" }, extra));
}

// The sidebar page shows a warning on Next up as a step: its words where a step has its ask and why, and its fix
// where a step has its answers (SPEC UI-52). A blocked call, dialog or Codex question brings its own card.
function warningCard(warning) {
  if (warning.card) {
    const card = warning.card();
    card.classList.add("warn-blocked");
    card.dataset.warning = warning.key;
    card.dataset.glide = `warning ${warning.key}`;
    return card;
  }
  return h("article", { class: `card next-card warn-step${warning.tone === "top" ? " top" : ""}`, "data-warning": warning.key, "data-glide": `warning ${warning.key}`, title: warning.tip },
    h("div", { class: "warn-step-head" }, warningMenu(warning)),
    h("h1", { class: "ask" }, h("span", { class: "warn-step-mark" }, icon(warning.mark)), h("span", { class: "ask-text", text: warning.title })),
    warning.more && h("p", { class: "why", text: warning.more }),
    warning.snoozedUntil && h("p", { class: "warn-snoozed-note" }, icon("clock"), `Snoozed until ${when(new Date(warning.snoozedUntil))}`),
    warning.extra && h("div", { class: "warn-extra" }, warning.extra),
    (warning.fix || warning.close) && h("div", { class: "answers", role: "group", "aria-label": "Answers" },
      warning.fix && h("button", { class: "button answer", type: "button", disabled: warning.fix.busy, onclick: warning.fix.onclick },
        h("span", { class: "answer-words", text: warning.fix.words })),
      warning.close && h("button", { class: "button answer", type: "button", disabled: warning.close.busy,
        onclick: warning.close.onclick, title: warning.close.title }, h("span", { class: "answer-words", text: warning.close.words || "Close this warning" }))),
    warningMessageBox(warning));
}

// A message on a warning, in place of its fix (SPEC UI-82): it becomes a to-do with the warning attached, as Add makes
// one, and a new session in Cockpit's checkout acts on it. The warning goes to that to-do: it leaves Inbox while the
// to-do is open, and the card moves on as after a snooze.
function warningMessageBox(warning) {
  const key = `msg:warning:${warning.key}`;
  const area = draftArea(key, "Message… a new session acts on it", 1, "");
  area.setAttribute("aria-label", "Message");
  const box = h("div", { class: "composer inline-send warn-message" });
  const send = () => {
    const text = (ui.drafts.get(key) || "").trim();
    if (!text) return;
    takeDraft(key);
    const change = { confirmed: null };
    ui.handing.set(warning.key, change);
    if (ui.warningOpen === warning.key) ui.warningOpen = null;
    render(false);
    const quote = [warning.title, warning.more].filter(Boolean).join(" ");
    return run(key, async () => {
      let answer;
      try {
        answer = await apiWritten("POST", "/api/warnings/message",
          { key: warning.key, text, part: { target: "page:next/warning", quote, detail: "" } }, { key, text });
      } catch (error) {
        if (ui.handing.get(warning.key) === change) ui.handing.delete(warning.key);
        restoreDraft(key, text);
        render(false);
        throw refused("Not sent", error);
      }
      ui.state.handed_warnings = answer.handed_warnings;
      change.confirmed = ++ui.tick;
      toast("A new session has your message; the warning returns if it still holds when that to-do closes", false,
        { label: "Open", run: () => openAdded(answer.item.id) });
    });
  };
  const sendButton = iconButton("send", "Send", { class: "button send", "data-key": "Ctrl+Enter", disabled: !area.value.trim(), onclick: send });
  sendButton.title = "Send: a new to-do with this warning attached, and a new session that starts on it";
  const shown = () => {
    const filled = Boolean(area.value.trim());
    box.classList.toggle("filled", filled);
    box.classList.toggle("drafted", filled);
    sendButton.disabled = !filled;
  };
  area.addEventListener("input", shown);
  area.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || !(event.ctrlKey || event.metaKey)) return;
    event.preventDefault();
    send();
  });
  shown();
  box.append(
    composerField(area, send),
    h("div", { class: "composer-foot" }, h("div", { class: "send-split" }, sendButton)));
  return box;
}

// A warning's fix as a small key, on a classic page's card and in the small card under the red arrows.
function fixButton(fix) {
  return h("button", { class: `button small${fix.primary ? " primary" : ""}`, type: "button", disabled: fix.busy, onclick: fix.onclick }, fix.words);
}

// On the sidebar page the refresh failure stays a card at the top: it says the page itself is behind. The
// question about the user's team is a plain card there too, as it is no warning.
function renderBanner(state) {
  const cards = [];
  if (ui.error && (!ui.lastOk || ui.catchUpFailed || Date.now() - ui.lastOk.getTime() > BANNER_AFTER_MS)) {
    cards.push(warnCard({
      key: "refresh", tone: "fail", mark: "alert", tip: null, title: `Cockpit could not refresh: ${ui.error}.`,
      more: `Showing what it had at ${ui.lastOk ? clock(ui.lastOk.toISOString()) : "load"}.`,
      fix: null, close: null, extra: null,
    }));
  }
  if (!SIDEBAR) {
    cards.push(...warningList(state).map((warning) => {
      const card = warnCard(warning);
      if (warning.key.startsWith("account-problem:")) card.classList.add("account-problem");
      return card;
    }));
  }
  if (state.team === null) cards.push(teamAsk());
  if (ui.zone?.ask) cards.push(zoneAsk(ui.zone));
  fill("banner", cards);
  keepPulses();
}

// A poll draws the warnings and the list's rows again. Every glow and pulse keeps time with the page's clock from its start, so a new
// row or card pulses on from where the one it replaced was, without a jump, and all of them pulse together.
const PULSES = new Set(["warn-glow", "warn-pulse", "warn-row-pulse", "fresh-pulse"]);

function keepPulses() {
  for (const pulse of document.getAnimations()) {
    if (PULSES.has(pulse.animationName)) pulse.startTime = 0;
  }
}

// The first time the user opens Cockpit, it asks who they work with most closely: the morning briefing
// follows them (SPEC VIEW-8). Skip names no one, so no briefing comes; the Briefing page changes it later.
const TEAM_KEY = "team-ask";
const TEAM_PLACEHOLDER = "One person a line, with what they do: Ana works on Tasks reminders";

function teamAsk() {
  // The field reads only while a save is on its way: the banner goes once it is saved, with any words typed since.
  const saving = (text) => { field.readOnly = true; saveTeam(text); };
  const save = h("button", {
    class: "button small primary", type: "button",
    disabled: !ui.teamDraft.trim() || ui.busy.has("team"), onclick: () => saving(ui.teamDraft),
  }, "Save");
  const field = h("textarea", {
    class: "field team-field", rows: 3, "data-key": TEAM_KEY, value: ui.teamDraft, readonly: ui.busy.has("team"),
    placeholder: TEAM_PLACEHOLDER, "aria-label": "The people you work with most closely",
  });
  field.addEventListener("input", () => {
    ui.teamDraft = field.value;
    save.disabled = !field.value.trim();
  });
  const card = warnCard({
    key: "team", tone: "question", mark: "log", tip: null,
    title: "Each morning you get a briefing on what they did",
    more: "Name your team or the colleagues you follow; you can change them on the Briefing page.",
    fix: null, close: null,
    extra: [field, h("span", { class: "team-ask-actions" },
      h("button", {
        class: "button small ghost", type: "button", disabled: ui.busy.has("team"),
        onclick: () => saving(""),
      }, "Skip"),
      save)],
  });
  card.classList.add("team-ask");
  return card;
}

function saveTeam(text) {
  return run("team", () => api("PUT", "/api/team", { team: text }), () => {
    ui.teamDraft = "";
    toast(text.trim() ? "Team saved; the Briefing page shows your briefing each morning" : "No briefing; the Briefing page can name your team later");
    refresh();
  });
}

// The user's time zone (SPEC CONTROL-32). The page reports its browser's zone on opening, on coming back into view and
// each hour. The browser takes it from the device's settings, so a VPN leaves it alone. Once the browser has shown
// a zone with another clock than Cockpit's for six hours, Cockpit asks here whether to switch; Keep stops the
// question for this trip. The brand menu shows the zone and opens a picker.
// Read at each use, so a page left open follows the device into a new zone.
const browserZone = () => Intl.DateTimeFormat().resolvedOptions().timeZone;
const ZONE_REPORT_MS = 60 * 60e3;

// "America/Argentina/Buenos_Aires" reads as Buenos Aires.
const zonePlace = (zone) => zone.split("/").pop().replaceAll("_", " ");
const zoneClock = (zone) => new Date().toLocaleTimeString([], { timeZone: zone, hour: "2-digit", minute: "2-digit", hourCycle: "h23" });

function setZone(answer) {
  const askedBefore = JSON.stringify(ui.zone?.ask ?? null);
  ui.zone = answer;
  zoneEntry.querySelector(".when").textContent = zonePlace(answer.zone);
  zoneEntry.title = `Cockpit runs on ${answer.zone} time; press to change it`;
  if (JSON.stringify(answer.ask) !== askedBefore && ui.state) Motion.redraw(() => renderBanner(ui.state), "poll");
}

async function reportZone() {
  try {
    setZone(await api("POST", "/api/timezone/report", { zone: browserZone() }));
  } catch (error) {
    logError("reporting the browser's time zone", error);
  }
}

function zoneAsk(answer) {
  const place = zonePlace(answer.ask.zone);
  const home = zonePlace(answer.zone);
  const hours = Math.floor((Date.now() - Date.parse(answer.ask.since)) / 3600e3);
  const span = hours >= 48 ? `${Math.floor(hours / 24)} days` : `${hours} hours`;
  const busy = ui.busy.has("zone");
  const card = warnCard({
    key: "zone", tone: "question", mark: "clock", tip: null,
    title: `Your browser has shown ${place} time for ${span}`,
    more: `It is ${zoneClock(answer.ask.zone)} there and ${zoneClock(answer.zone)} in ${home}. Cockpit runs on ${home} time `
      + "for your morning briefing, scheduled prompts, the sweeper's work hours and the days in your time report.",
    fix: null, close: null,
    extra: [h("span", { class: "team-ask-actions" },
      h("button", {
        class: "button small ghost", type: "button", disabled: busy,
        onclick: () => run("zone", () => api("POST", "/api/timezone/keep", { zone: answer.ask.zone }).then(setZone),
          () => toast(`Cockpit stays on ${home} time; it asks again on a later trip to ${place}`)),
      }, `Keep ${home} time`),
      h("button", {
        class: "button small primary", type: "button", disabled: busy,
        onclick: () => saveZone(answer.ask.zone),
      }, `Switch to ${place} time`))],
  });
  card.classList.add("zone-ask");
  return card;
}

function saveZone(zone) {
  return run("zone", () => api("PUT", "/api/timezone", { zone }).then(setZone), () => {
    toast(`Cockpit now runs on ${zonePlace(zone)} time`);
    reportZone();
  });
}

// The brand menu's entry, above Sign out and below the Vim switch (prefs.js), with Cockpit's zone on its right.
const zoneEntry = h("button", { class: "zone-setting", type: "button", onclick: () => openZoneDialog() },
  h("span", { class: "menu-label" }, icon("clock"), "Time zone"), h("span", { class: "when" }));

// The picker lists every zone the browser knows, each with its time now. "Use this device's zone" picks the
// browser's own zone in one press when it differs.
async function openZoneDialog() {
  const dialog = document.getElementById("zone-dialog");
  zoneEntry.closest("details")?.removeAttribute("open");
  if (dialog.open) return;
  let current;
  try {
    current = (await api("GET", "/api/timezone")).zone;
  } catch (error) {
    toast(`Time zone not loaded: ${error.message}`, true);
    return;
  }
  const device = browserZone();
  const names = [...new Set([...(Intl.supportedValuesOf?.("timeZone") ?? []), current, device])].sort();
  const pick = h("select", { class: "field zone-pick", id: "zone-pick", "aria-label": "Time zone" },
    names.map((name) => h("option", { value: name, selected: name === current, text: `${name.replaceAll("_", " ")} · ${zoneClock(name)}` })));
  const own = device !== current && !device.startsWith("Etc/") && device !== "UTC";
  dialog.replaceChildren(h("form", {
    class: "time-panel zone-panel", tabindex: "-1",
    onsubmit: (event) => {
      event.preventDefault();
      dialog.close();
      if (pick.value !== current) saveZone(pick.value);
    },
  },
  h("h2", { id: "zone-dialog-title", text: "Time zone" }),
  h("p", { class: "time-note", text: "Cockpit uses it for your morning briefing, scheduled prompts, the sweeper's work hours and the days in your time report." }),
  pick,
  own && h("button", {
    class: "button small ghost zone-own", type: "button", onclick: () => { pick.value = device; },
  }, `Use this device's zone: ${zonePlace(device)}`),
  h("div", { class: "time-actions" },
    h("button", { class: "button primary", type: "submit" }, "Save"),
    h("button", { class: "button ghost", type: "button", onclick: () => dialog.close() }, "Cancel"))));
  dialog.showModal();
  pick.focus();
}

function resumeSweeps() {
  return run("resume", () => api("POST", "/api/sweeps/resume"), () => toast("Sweeps restarted"));
}

// Nothing waiting for the user (UI-10): one line that says what is true now, from the last sweep and the
// sessions at work. A sweep that ran over ten minutes ago adds Update, to look now. Between sweeps nothing
// runs on a clock with the queue empty: the automatic check covers only steps in the queue, and the
// sweeper wakes when the board changes.
const EMPTY_FRESH_MS = 10 * 60e3;

function emptyNotice(state) {
  const sweep = state.last_sweep;
  const working = state.sessions.filter((session) => session.working_now && !session.is_sweep && !session.standing);
  const waiting = state.waiting_sessions.length;
  const parts = [];
  let older = false;
  if (state.sweep_running) parts.push("A sweep is looking for new asks.");
  else {
    if (working.length) {
      const names = working.slice(0, 2).map((session) => sessionLabel(session, ""));
      const rest = working.length - names.length;
      parts.push(`${plural(working.length, "session", "sessions")} working: ${names.join(", ")}${rest ? ` and ${rest} more` : ""}.`);
    }
    const finished = sweep && sweep.finished_at ? new Date(sweep.finished_at).getTime() : null;
    if (finished !== null) parts.push(`Last sweep ${ago(sweep.finished_at)}.`);
    older = finished === null || Date.now() - finished > EMPTY_FRESH_MS;
  }
  if (waiting) parts.push(`${plural(waiting, "session waits", "sessions wait")} at a dialog, above.`);
  return h("div", { class: "empty-card" },
    h("span", { class: "empty-check", "aria-hidden": "true" }, icon("check")),
    h("strong", { text: "Nothing waiting for you" }),
    parts.length > 0 && h("span", { class: "empty-more", text: parts.join(" ") }),
    older && iconButton("retry", "Update", { class: "button small ghost", onclick: update }));
}

function renderNext(step, state) {
  // The keys act on the step Next up shows, not on a kept one.
  ui.next = step && !step.left ? step : null;
  if (!step) { fill("next", emptyNotice(state)); return; }
  // data-parts: a poll's redraw keeps the part the user reads in place (motion.js).
  // A to-do's step takes pasted and dropped images, as the item's row did before it showed only as its steps.
  const full = stepItem(step, state);
  fill("next", reorderButton(step, state, true), h("article", { class: `card next-card${step.left ? " left" : ""}${clockClass(step)}`,
    "data-step": step.id, "data-parts": true, "data-drop-item": full && full.todo ? full.id : null }, mergedNote(state), stepBody(step, state, "next"),
    !step.left && draftCorner(step)));
}

// One line on top of Next up for the items an agent closed with no finish step: their PR merged (CONTROL-15).
function mergedNote(state) {
  const closes = state.merged_closes;
  if (closes.length === 0) return null;
  const names = closes.map((close) => (close.pull ? `${close.pull} merged: ${close.title}` : `${close.title}: ${close.note}`));
  const lines = closes.map((close) => (close.pull
    ? `${close.pull} merged, so ${close.closed_by.replace(/^agent:/, "")} closed "${close.title}" at ${clock(close.closed_at)}`
    : `${close.closed_by.replace(/^agent:/, "")} finished the fire-and-forget to-do "${close.title}" at ${clock(close.closed_at)}: ${close.note}`));
  return h("p", { class: "merged-note", title: `${lines.join("\n")}\nYou find ${closes.length === 1 ? "it" : "them"} under Finished this week. This line goes once you act on a step.` },
    icon("done"), h("span", { text: `Closed: ${names.join("; ")}` }));
}

// group: "queue" (The user's places), "ready" (ready next, waiting for a place) or "snoozed". Line 1: the priority,
// the item's short title, the ask, and marks; line 2 (closed rows only): the Cockpit tag (UI-43) and, on a phone, the
// priority. The side holds the size, the check and the age. A closed row leaves the image count and the sweeper's
// reasons to the open step.
function queueRow(step, state, group) {
  const key = `step:${step.id}`;
  const snoozed = group === "snoozed";
  const open = ui.expanded.has(key);
  const full = stepItem(step, state);
  // The item's short title leads, so the ask can be the bare question (SPEC SESSION-3); an open row shows it above the step.
  const lead = !open && h("span", { class: "row-item", title: step.item.title, text: step.item.title });
  const title = opens(h("span", { class: "row-title", title: plainText(step.ask_html), html: step.ask_html }), key);
  // An open row's ask is its step's ask, whose page links open beside the row (NEXT-47).
  if (open) pageComments(title, step);
  const tag = step.left ? null : priorityTag(step.item, "");
  const since = group === "queue" ? step.queued_at : step.created_at;
  // A top step's check mark rides in the age's slot, just left of the age.
  const check = group === "queue" && isTop(step, state) ? checkedMark(step, state, "") : null;
  const line2 = open ? [] : [!step.left && PHONE.matches && tag, cockpitChip(step.item.project)].filter(Boolean);
  return h("div", { class: `row step-row${open ? " open" : ""}${step.left ? " left" : ""}${clockClass(step)}`, "data-step": step.id, "data-drop-item": full && full.todo ? full.id : null },
    h("div", { class: "row-head", onclick: (event) => { if (!event.target.closest("a, button")) toggle(key); } },
      !PHONE.matches && (tag || h("span", { class: "prio-slot" })),
      h("div", { class: "row-main" },
        h("div", { class: "row-line1" }, newDot(step), lead, !open && stepOf(step, state), title, quickMark(step), earlyMark(step), !open && mergedMark(full), group === "queue" && reorderButton(step, state, false), !open && !step.left && draftMark(step, false), updatedMark(step, false)),
        line2.length > 0 && h("div", { class: "row-line2" }, line2), step.left && leftNote(step)),
      h("div", { class: "row-side" },
        group === "ready" && !step.left && takeButton(step),
        !open && sentMark(step),
        !open && sizeBar(step),
        snoozed
          ? (readingWords(state, `step:${step.id}`)
            ? h("span", { class: "row-age", title: `Cockpit is reading when it comes back; until then, snoozed until ${when(new Date(step.snoozed_until))}` }, icon("clock"), "Reading")
            : h("span", { class: "row-age", "data-time": step.snoozed_until, title: `Snoozed until ${when(new Date(step.snoozed_until))}` }, icon("clock"), when(new Date(step.snoozed_until))))
          : h("span", { class: `row-age${check && check.classList.contains("stale") ? " overdue" : ""}`, "data-time": since, title: rankTitle(step) }, check, age(since)))),
    open && h("div", { class: "row-body" }, h("div", { class: "step-full" }, stepBody(step, state, group))),
    !step.left && draftCorner(step));
}

// A group's summary: its name, its count, and what the group is in the tooltip.
function groupSummary(name, count, tip) {
  return h("span", { class: "group-name", title: tip }, name, h("span", { class: "group-count", text: count }));
}

const CLOSED_ICONS = { done: "check", dismissed: "x" };

// How a closed step was closed: the user's Done or Archive is done; an agent's done is handled, in grey,
// since only the user's press finishes a task (SPEC CONTROL-17) and the plum check is the user's.
function closedWord(step) {
  if (step.status === "dismissed") return "Dismissed";
  return step.closed_by === "user" ? "Done" : "Handled";
}

function closedClass(step) {
  return step.status === "done" && step.closed_by !== "user" ? "handled" : step.status;
}

// Who closed something, in words: the sweeper, Cockpit, a session by its name, or an agent by the name
// it gave with --as.
function closerWords(actor, sessions) {
  if (actor === "cockpit") return "Cockpit";
  const name = actorWords(actor);
  if (name === "sweep") return "The sweeper";
  const short = /^claude-([0-9a-f]{8})$/.exec(name);
  const session = short && sessions.find((one) => one.session_id.startsWith(short[1]));
  return session && session.name ? session.name : name;
}

// A closed row's title; when an agent or Cockpit closed it, a second line says who and why (SPEC VIEW-4).
function closedTitle(title, closedBy, note, how, state) {
  if (closedBy === "user") return title;
  const who = closerWords(closedBy, state.sessions);
  return h("div", { class: "closed-words" }, title,
    h("span", { class: "row-sub closed-by", title: note ? `${how} by ${who}\n${note}` : `${how} by ${who}`, text: note ? `${who} · ${note}` : `${how} by ${who}` }));
}

// How a finished task ended, for its row in Finished this week (SPEC VIEW-20): done or closed as not needed, who
// ended it and why, so an agent's close stays reviewable (VIEW-4). `entry` is a done item (its `ended`, from
// store.how_done) or an archived one from `closed_recently.items`. Returns {kind, words}: kind "done" (the user's
// plum check), "handled" (an agent's grey check) or "archived".
function endedHow(entry, state) {
  const said = (words, note) => (note ? `${words}: ${note}` : words);
  if (entry.action === "archived") {
    return entry.closed_by === "user"
      ? { kind: "archived", words: said("Closed as not needed by you", entry.note) }
      : { kind: "archived", words: said(`Archived by ${closerWords(entry.closed_by, state.sessions)}`, entry.note) };
  }
  const ended = entry.ended;
  if (!ended) return { kind: "handled", words: "Moved to done" };
  if (ended.by === "user") return { kind: "done", words: said("Done by you", ended.note) };
  return { kind: "handled", words: said(`Done by ${closerWords(ended.by, state.sessions)}`, ended.note) };
}

const ENDED_ICONS = { done: "check", handled: "check", archived: "archive" };

function endedLine(how) {
  return h("span", { class: `row-sub ended-line ${how.kind}`, title: how.words },
    h("span", { class: `ended-icon ${how.kind}`, "aria-hidden": "true" }, icon(ENDED_ICONS[how.kind])), h("span", { text: how.words }));
}

// When a finished task ended, for the order of Finished this week.
function endedAt(entry) {
  if (entry.action === "archived") return entry.closed_at;
  return (entry.ended && entry.ended.at) || entry.done_at || entry.updated_at;
}

// A task closed as not needed in the last week, in Finished this week beside the done ones (SPEC VIEW-20): its
// title, how it ended and who closed it, and Unarchive (VIEW-4). Search's Closed earlier group (VIEW-21) shows
// older closed work the same way; a task done before this week has no Unarchive.
function closedItemRow(entry, state) {
  return h("div", { class: "row closed-item", "data-item": entry.id },
    h("div", { class: "row-head" },
      h("div", { class: "row-main" },
        h("div", { class: "row-line1" }, h("span", { class: "row-title", text: entry.title })),
        h("div", { class: "row-line2" }, cockpitChip(entry.project), endedLine(endedHow(entry, state)))),
      h("div", { class: "row-side" },
        entry.action === "archived" && iconButton("back", "Unarchive", { class: "button small ghost reopen", onclick: () => patchItem(entry, { archived: false }, null) }),
        h("span", { class: "row-age", "data-time": entry.closed_at, title: "Closed", text: age(entry.closed_at) }))));
}

// The steps closed in the last day, folded at the end of Finished this week. Each row: a status icon, the ask,
// a short age; under the ask, who closed it and their note when it was not the user (SPEC VIEW-4), and Reopen.
// The closes are newest first, the first CLOSED_TOP shown.
function closedSteps(state) {
  const { steps } = state.closed_recently;
  if (!steps.length) return null;
  const stepRows = steps.map((step) => h("li", { "data-at": step.closed_at },
    h("span", { class: `closed-icon ${closedClass(step)}`, role: "img", "aria-label": closedWord(step).toLowerCase(), title: `${closedWord(step)} by ${closerWords(step.closed_by, state.sessions)}` },
      icon(CLOSED_ICONS[step.status] || "check")),
    cockpitChip(step.item_project),
    closedTitle(h("span", { class: "row-title", title: step.item_title, html: step.ask_html }), step.closed_by, step.note, closedWord(step), state),
    h("span", { class: "row-age", "data-time": step.closed_at, text: age(step.closed_at) }),
    iconButton("back", "Reopen", { class: "button small ghost reopen", onclick: () => act(step, "reopen") })));
  const shown = ui.showAllClosed ? stepRows : stepRows.slice(0, CLOSED_TOP);
  return disclosure("closed-steps", "more closed-steps", groupSummary("Steps closed in the last day", steps.length, null),
    h("ul", { class: "closed-list" }, shown,
      stepRows.length > CLOSED_TOP && h("li", { class: "more-row" },
        h("button", { class: "link-button", type: "button", onclick: () => { ui.showAllClosed = !ui.showAllClosed; render(false); } },
          ui.showAllClosed ? "Show fewer" : `Show ${stepRows.length - shown.length} more`))), false);
}

// Finished this week (SPEC VIEW-20): the tasks that ended in the last week, done or closed as not needed, in one
// list, newest first, each saying how it ended; then the steps closed in the last day, folded. `done`: the done
// items it shows as rows.
function finishedGroup(done, state, sessions) {
  const closed = state.closed_recently.items.filter((entry) => entry.action === "archived");
  const rows = [
    ...done.map((item) => ({ at: endedAt(item), row: itemRow(item, state, sessions, null) })),
    ...closed.map((entry) => ({ at: endedAt(entry), row: closedItemRow(entry, state) })),
  ].sort((a, b) => b.at.localeCompare(a.at)).map((entry) => entry.row);
  const steps = closedSteps(state);
  if (!rows.length && !steps) return null;
  return disclosure("ball:done", "group done", ["Finished this week", h("span", { class: "group-count", text: rows.length })],
    [rows.length > 0 && h("div", { class: "list" }, rows), steps], false);
}

// What a session holding a handed-off step is doing now, by its dot's state (`sessionState`).
const AGENT_DOING = {
  working: "working on it",
  idle: "idle, waits on a background task",
  blocked: "waits for a permission",
  done: "finished its turn, step still open",
  stopped: "stopped",
  failed: "failed",
};

function capitalize(words) {
  return `${words[0].toUpperCase()}${words.slice(1)}`;
}

// Steps the user handed to an agent, at the top of All work's With agents: who has each, what
// that session is doing now, and since when; what the user wrote is in the tooltip. One that did not arrive
// says so in red, with Retry. Its item keeps its own row in All work, which the arrow key opens; an item that
// left All work (done more than a week ago) has no row and no key.
function handedList(state, steps) {
  if (!steps.length) return null;
  const sessions = sessionMap(state);
  const items = new Map(state.items.map((item) => [item.id, item]));
  const rows = steps.map((step) => {
    const handoff = step.handoff;
    const to = handoff.session_id ? sessionLabel(sessions.get(handoff.session_id), "") : `a new session in ${repoOf(handoff.new_cwd)}`;
    const setAside = handoff.state === "failed" && Boolean(handoff.dismissed_at);
    const failed = handoff.state === "failed" && !setAside;
    const waiting = messageDelivery(handoff) === "waiting";
    const wait = waiting && waitWords(handoff);
    // Kept back while Take back can still stop it (MSG-8): until the notice after Send is gone.
    const kept = handoff.state === "queued" && Boolean(handoff.take_back_until);
    const message = failed && state.messages.find((other) => other.id === step.handoff_message_id);
    // A run the user started with this hand-off (Run hands off and starts the run together).
    const ran = step.run && step.run.started_at >= handoff.created_at ? step.run : null;
    const trouble = failed || (ran && ran.finished_at && ran.exit_code !== 0);
    const session = handoff.session_id && sessions.get(handoff.session_id);
    const doing = kept ? "your message goes shortly; Take back still stops it"
      : waiting ? `your message waits ${wait.since} for its turn to end` : !failed && session ? AGENT_DOING[sessionState(session)] : null;
    const sub = [ran && runWords(ran), failed ? "Not delivered" : setAside ? `reached ${to} another way` : to, doing].filter(Boolean).join(" · ");
    return h("li", { "data-step": step.id },
      h("span", { class: `closed-icon${failed ? " failed" : ""}`, role: "img", "aria-label": failed ? "Not delivered" : "With an agent",
        title: failed ? `Did not arrive: ${handoff.detail}` : setAside ? setAsideWords(handoff) : `${capitalize(to)} has it` },
        icon(failed ? "alert" : "handoff")),
      cockpitChip(step.item.project),
      h("span", { class: "row-title", title: `${step.item.title}\n${handoff.author === "cockpit" ? "Cockpit sent" : "You wrote"}: ${handoff.text}`, html: step.ask_html }),
      lateMark(wait),
      h("span", { class: `row-sub${trouble ? " fail-text" : ""}`, title: ran ? outputTail(ran) : null, text: sub }),
      h("span", { class: "row-age", "data-time": handoff.created_at, text: ago(handoff.created_at) }),
      message && iconButton("retry", "Retry", { class: "button small ghost", onclick: () => retryMessage(message) }),
      (waiting || kept) && interruptButton(handoff),
      items.has(step.item_id) && iconButton("arrow", "Open task",
        { class: "button small ghost handed-open", onclick: () => window.openInWork(step.item_id, items.get(step.item_id).group) }),
      iconButton("back", "Take back: to the top of your queue", { class: "button small ghost", onclick: () => takeBack(step) }));
  });
  return h("div", { class: "with-agents" },
    h("p", { class: "handed-head", title: "An agent works on each; it gives it back if it needs you",
    text: `You handed these (${steps.length})` }),
    h("ul", { class: "closed-list" }, rows));
}

// `ready`, `snoozed`: those lists with their kept steps.
function renderQueue(rest, readySteps, snoozedSteps, state) {
  if (!rest.length && !readySteps.length && !snoozedSteps.length) { fill("queue"); return; }
  const shown = ui.showAllQueue ? rest : rest.slice(0, QUEUE_TOP);
  const hidden = rest.length - shown.length;
  const list = rest.length
    ? h("div", { class: "list" },
        shown.map((step) => queueRow(step, state, "queue")),
        rest.length > QUEUE_TOP && h("div", { class: "more-row" },
          h("button", { class: "link-button", type: "button", onclick: () => { ui.showAllQueue = !ui.showAllQueue; render(false); } },
            ui.showAllQueue ? "Show fewer" : `Show ${hidden} more`)))
    : null;
  const ready = readySteps.length
    ? disclosure("ready-next", "group ready-next", groupSummary("Waiting next", readySteps.length,
        "Ready, waiting for one of your four places"),
        h("div", { class: "list" }, readySteps.map((step) => queueRow(step, state, "ready"))), false)
    : null;
  const snoozed = snoozedSteps.length
    ? disclosure("snoozed", "group snoozed", groupSummary("Snoozed", snoozedSteps.length, null),
        h("div", { class: "list" }, snoozedSteps.map((step) => queueRow(step, state, "snoozed"))), false)
    : null;
  fill("queue", list, ready, snoozed);
}

// Tool calls the user's permission hooks reported (PERM-1): a session's dialog
// waiting for an answer, or a call auto mode blocked. A waiting dialog's Run lets
// that one call go ahead (PERM-27); a blocked call runs again under a rule (Cockpit adds it to the
// settings file and tells the session to retry), or once (PERM-7: a sentence
// naming this call for 15 minutes). The rule editor offers suggestions to pick
// from, and the user's pick or own rule is kept per request and list as a draft.

// Why a session in auto mode still asks (PERM-27): Claude Code's own checks ask before some calls, such as deleting
// files or entering a worktree, outside auto mode's judgement. Saved rules do not prevent later dialogs of the same
// kind, so a dialog's card offers Run and Deny only.
const DIALOG_WHY = "Claude Code asks before some calls, such as deleting files, even in auto mode, and no rule"
  + " stops it asking. Run lets this one call go ahead and saves nothing.";

function requestKind(request) {
  return request.kind === "asked" ? "Waiting for your permission" : "Blocked by auto mode";
}

// Auto mode gives its reason as a category tag such as "[Auto-Mode Bypass]"; the card says it in plain
// words (PERM-8). It judges a call with what the session did before it, so the tag often fits the session's
// course more than the command: a read-only `git log` can be stopped as a bypass after a guard has refused
// the session's earlier git commands.
function blockTag(request) {
  return (request.reason.match(/^\[(.+)\]$/) || [])[1];
}

// What stopped the call, as the card's one line: the kind and auto mode's rule class.
function requestLine(request) {
  if (request.kind === "asked") return "Claude Code asks you first";
  if (request.reason === "Classifier unavailable") return "Auto mode · Checker unavailable";
  const tag = blockTag(request);
  if (tag) return `Auto mode · ${tag}`;
  return !request.reason || /gave no explanation/.test(request.reason) ? "Auto mode · No reason given" : `Auto mode · ${request.reason}`;
}

// The same in plain words, with what the user can do, for the hover card (PERM-8). A block under a label has
// its cause from the server, read against what the session was doing (PERM-13).
function requestWhy(request) {
  if (request.kind === "asked" && request.hook_ended_at) {
    return `${DIALOG_ENDED}\nCockpit stopped holding the dialog open at ${clock(request.hook_ended_at)}.`;
  }
  if (request.kind === "asked") return DIALOG_WHY;
  if (request.reason === "Classifier unavailable") {
    return "Auto mode's checker was unavailable, so this call was stopped without being judged. If the call is fine, allow it.";
  }
  if (request.cause) return request.cause.text;
  const stopped = !request.reason || /gave no explanation/.test(request.reason) ? "Auto mode stopped it and gave no reason." : `Auto mode stopped it: ${request.reason}`;
  return `${stopped} It judges a call with what the session did before it, so a harmless command can be stopped too. If the call is fine, allow it.`;
}

// A blocked call's editor opens on Sentence. A dialog has no editor (PERM-27).
function ruleListOf(request) {
  return ui.choices.get(`perm:req:${request.id}`) || "auto";
}

function ruleDraftKey(request, list) {
  return `perm:req:${request.id}:${list}`;
}

// Unpicked, the tool rule is the second narrowest, which reaches past this one call (PERM-11).
function ruleDraft(request, list) {
  const key = ruleDraftKey(request, list);
  if (ui.drafts.has(key)) return ui.drafts.get(key);
  const rules = request.suggested_rules;
  if (list === "allow") return rules.length ? rules[Math.min(1, rules.length - 1)].rule : "";
  // A sentence already allowed did not stop auto mode from blocking the call, so the first one that is not starts the
  // box; the user can still pick one that is.
  const sentences = request.suggested_sentences;
  return sentences.find((sentence) => !alreadyAllowed("auto", sentence)) || sentences[0] || "";
}

function withoutRequest(state, id) {
  const requests = state.permission_requests.filter((other) => other.id !== id);
  const past = (state.moved_past || []).filter((other) => other.id !== id);
  return { ...state, permission_requests: requests, moved_past: past };
}

// An answer takes the card off at once; if the server refuses, the card comes back
// in place with the reason on it, `undo` (or null) puts back what else changed, and
// a toast says it when the card is out of view. Resolves to the server's answer, or null.
// `words` ({key, text}, or null): the user's words in the answer, put back in their box when a closed or
// reloaded page left the answer behind and Cockpit then did not take it (apiWritten).
function answerRequest(request, action, body, undo, words) {
  let answered = null;
  ui.requestErrors.delete(request.id);
  return run(`perm:req:${request.id}`, async () => {
    try {
      answered = await optimistic((state) => withoutRequest(state, request.id),
        () => {
          const path = `/api/permissions/requests/${request.id}/${action}`;
          return words ? apiWritten("POST", path, body, words) : api("POST", path, body);
        }, undo);
    } catch (error) {
      showRequestError(request, error.message);
      // With the card back in view its notice says it, and the toast that announced the answer goes.
      const notice = document.querySelector(`:is(#blocked, #next) [data-request="${request.id}"] .request-error`);
      const box = notice && notice.getBoundingClientRect();
      if (box && box.top >= 0 && box.bottom <= window.innerHeight) {
        logError("answering a call", error);
        hideToast();
        return;
      }
      throw new Error(`The answer did not go through, the call is back: ${error.message}`, { cause: error });
    }
  }, () => { if (ui.permissions && ui.permissions !== "loading") loadPermissions(); }).then(() => answered);
}

// PERM-9: what the page says when the session took the call back before the user's answer landed.
function withdrawnLine(answered) {
  return answered && answered.state === "withdrawn"
    ? `The session no longer needs this call: it withdrew it at ${clock(answered.withdrawn_at)}.`
    : null;
}

// A dialog Cockpit's hook stopped waiting on after 25 minutes, while the session still waits at it (PERM-8):
// only a restart of the session answers it now. The card says what the user's answer does; the hook's end time
// stays in the tooltip.
const DIALOG_ENDED = "The session is stuck at this question until you answer. Your answer restarts it: after Run it"
  + " makes the call again, which then goes through; after Deny it is told you said no.";

// An answer whose only words come once it landed: Allow on a dialog, Deny, Dismiss. An answer to a dialog
// the hook stopped waiting on restarts its session with a message (PERM-8).
function answerQuietly(request, action, body) {
  answerRequest(request, action, body, null, null).then((answered) => {
    const late = withdrawnLine(answered);
    if (late) toast(late);
    else if (answered && request.hook_ended_at && answered.message_id) toast("Restarting the session with your answer");
  });
}

// The card's notice; null clears it.
function showRequestError(request, text) {
  if (text) ui.requestErrors.set(request.id, text); else ui.requestErrors.delete(request.id);
  render(false);
}

function allowWithRule(request) {
  const list = ruleListOf(request);
  const key = ruleDraftKey(request, list);
  const sent = ruleDraft(request, list);
  const rule = (sent || "").trim();
  if (!rule) {
    showRequestError(request, `Write the ${RULE_LISTS[list].name} first.`);
    const field = document.querySelector(`:is(#blocked, #next) [data-key="${CSS.escape(key)}"]`);
    if (field) field.focus();
    return;
  }
  // A rule in place already only sends the retry, and Undo would remove a rule the user had before.
  const existing = alreadyAllowed(list, rule);
  // A draft started from a rule's Edit changes that rule in place (PERM-11).
  const replaces = existing ? null : ui.replacing.get(key) || null;
  if (!replaces) ui.replacing.delete(key);
  clearDraft(key, sent);
  const note = takeNote(request);
  const body = { rule, list, note: note.text, ...(replaces ? { replaces } : {}) };
  const answering = answerRequest(request, "allow", body, () => {
    ui.drafts.set(key, sent);
    saveDrafts();
    note.putBack();
  }, note.words || { key, text: sent });
  if (replaces) answering.then((answered) => { if (answered) { ui.replacing.delete(key); saveDrafts(); } });
  if (existing) {
    const told = toast("Retry sent");
    answering.then((answered) => {
      const late = withdrawnLine(answered);
      if (late) retoast(told, late, "");
    });
    return;
  }
  const done = replaces ? "Rule changed" : "Rule added";
  const undo = replaces ? () => changeRuleBack(list, rule, replaces) : () => removeRule(list, rule, false);
  const shown = toast(done, false, {
    label: "Undo",
    run: () => answering.then((answered) => { if (answered) undo(); }),
  }, [replaces && `Was: ${replaces}`, request.kind === "denied" && "The session is told to retry"].filter(Boolean).join("\n"));
  answering.then((answered) => {
    const late = withdrawnLine(answered);
    if (late) retoast(shown, `${done}. ${late}`, "");
  });
}

// PERM-7: a short note the user adds to a rule for a blocked call. It goes with the retry, which then comes as
// the user's own message, so the note can say what to do or watch for.
function noteKey(request) {
  return `perm:req:${request.id}:note`;
}

function noteOpen(request) {
  return ui.expanded.has(`note:${request.id}`) || Boolean((ui.drafts.get(noteKey(request)) || "").trim());
}

// The note an Allow sends (`text`, null when there is none), taken out of its box. `putBack` returns it when the
// answer fails on this page; `words` is it for apiWritten, which returns it after a reload.
function takeNote(request) {
  const key = noteKey(request);
  const sent = ui.drafts.get(key) || "";
  const text = sent.trim() || null;
  if (text) clearDraft(key, sent);
  return { text, putBack: () => { if (text) { ui.drafts.set(key, sent); saveDrafts(); } }, words: text ? { key, text: sent } : null };
}

function noteBox(request) {
  const key = noteKey(request);
  const field = h("input", {
    type: "text", class: "field", maxlength: 300, "data-key": key, "aria-label": "Note for the session",
    placeholder: "A note for the session, sent with your answer",
  });
  field.value = ui.drafts.get(key) || "";
  field.addEventListener("input", () => {
    ui.drafts.set(key, field.value);
    saveDrafts();
  });
  return h("div", { class: "allow-note" }, field);
}

function toggleNote(request) {
  const open = noteOpen(request);
  if (open) {
    ui.expanded.delete(`note:${request.id}`);
    clearDraft(noteKey(request), ui.drafts.get(noteKey(request)));
  } else {
    ui.expanded.add(`note:${request.id}`);
  }
  render(false);
  if (!open) document.querySelector(`:is(#blocked, #next) [data-key="${CSS.escape(noteKey(request))}"]`)?.focus();
}

// PERM-13: the user asks for a blocked call in their own words, which auto mode counts as a request. The card's
// box holds a message that asks for it; Send as you delivers it as the user's own prompt, and the card closes.
function tellKey(request) {
  return `perm:req:${request.id}:tell`;
}

function tellSession(request) {
  const key = tellKey(request);
  const sent = ui.drafts.has(key) ? ui.drafts.get(key) : request.ask_draft;
  const text = sent.trim();
  if (!text) {
    showRequestError(request, "Write the message first.");
    document.querySelector(`:is(#blocked, #next) [data-key="${CSS.escape(key)}"]`)?.focus();
    return;
  }
  clearDraft(key, sent);
  // A note the user wrote beside it goes at the message's end, so nothing they wrote is lost.
  const note = takeNote(request);
  const shown = toast("Sent as your message");
  const putBack = () => { ui.drafts.set(key, sent); saveDrafts(); note.putBack(); };
  // After a reload the message box takes back the whole message, the note in it, so the note comes back once.
  const words = { key, text: note.text ? `${sent}\n\n${note.text}` : sent };
  answerRequest(request, "tell", { text: note.text ? `${text}\n\n${note.text}` : text }, putBack, words).then((answered) => {
    const late = withdrawnLine(answered);
    if (late) retoast(shown, `Sent. ${late}`, text);
  });
}

function tellBox(request) {
  const area = draftArea(tellKey(request), "Your message to the session", 8, request.ask_draft);
  area.classList.add("field");
  area.setAttribute("aria-label", "Your message to the session");
  return h("div", { class: "tell" },
    h("p", { class: "tell-title", text: "Ask for it yourself: the session gets this as your own message, and auto mode counts your own words as a request." }),
    area);
}

// Text with `code` spans, as the server writes them.
function codeSpans(text) {
  return text.split(/`([^`]+)`/).map((part, index) => (index % 2 ? h("code", { text: part }) : part));
}

// Whether a sentence is in the auto-mode list already (the permissions as last read, or not yet read).
function alreadyAllowed(list, rule) {
  return Boolean(ui.permissions && ui.permissions !== "loading" && ui.permissions[list].includes(rule));
}

// PERM-11: the user edits a rule the card names, in its list's own box; the press then changes that rule in place
// rather than adding another beside it.
function startRuleEdit(request, list, rule) {
  const key = ruleDraftKey(request, list);
  ui.choices.set(`perm:req:${request.id}`, list);
  ui.replacing.set(key, rule);
  ui.drafts.set(key, rule);
  saveDrafts();
  ui.open.add(`perm:req:${request.id}:own:${list}`);
  ui.closedRules.delete(`rule:${request.id}`);
  ui.expanded.add(`rule:${request.id}`);
  render(false);
  const field = document.querySelector(`:is(#blocked, #next) [data-key="${CSS.escape(key)}"]`);
  if (field) field.focus();
}

function editRuleButton(request, list, rule, text) {
  return h("button", {
    class: "link-button edit-rule", type: "button",
    onclick: () => startRuleEdit(request, list, rule),
  }, icon("pencil"), text);
}

// The rule editor: a toggle between an auto-mode sentence and a tool rule. Both show their suggestions as
// options: the sentences with the session's own proposal first, the tool rules narrowest first, each with
// what it covers (PERM-11). "Write my own" opens a box. The editor folds away from a chevron at the end of
// its own bar, so the card's × means only Dismiss.
function ruleEditor(request, sessions, close) {
  const list = ruleListOf(request);
  const key = ruleDraftKey(request, list);
  const choiceKey = `perm:req:${request.id}`;
  const ownKey = `perm:req:${request.id}:own:${list}`;
  const draft = ruleDraft(request, list);
  const pick = (value) => { ui.choices.set(choiceKey, value); ui.expanded.add(`rule:${request.id}`); render(false); };
  const seg = h("div", { class: "seg", role: "group", "aria-label": "Kind of rule" },
    [["auto", "Sentence", "An auto-mode sentence tells the auto-mode check what is fine; it still judges each call"],
     ["allow", "Tool rule", "A tool rule lets every matching call run without asking and skips the auto-mode check"]]
      .map(([value, words, tip]) => h("button", { type: "button", class: value === list ? "on" : null, "aria-pressed": String(value === list), title: tip, onclick: () => pick(value) }, words)));
  const fold = iconButton("chevron", "Close editor", { class: "button ghost rule-fold", onclick: close });
  const onInput = (box) => {
    // Text that becomes, or stops being, a rule in place turns the card's button between Retry and adding or
    // changing the rule, so the card redraws then.
    const flips = alreadyAllowed(list, ruleDraft(request, list).trim()) !== alreadyAllowed(list, box.value.trim());
    ui.drafts.set(key, box.value);
    saveDrafts();
    if (flips) render(false);
    // The user is changing what the notice was about: it goes.
    if (ui.requestErrors.delete(request.id)) box.closest(".blocked-card").querySelector(".request-error")?.remove();
  };
  const allow = list === "allow";
  const values = allow ? request.suggested_rules.map((rule) => rule.rule) : request.suggested_sentences;
  const own = ui.open.has(ownKey) || !values.length || (Boolean(draft) && !values.includes(draft));
  const option = (value, ...content) => h("button", {
    class: "sentence-option", type: "button", role: "radio", "aria-checked": String(!own && draft === value),
    onclick: () => { ui.drafts.set(key, value); ui.replacing.delete(key); saveDrafts(); ui.open.delete(ownKey); render(false); },
  }, ...content);
  const already = (value) => alreadyAllowed(list, value) && h("span", {
    class: "already-rule", text: "already a rule",
    title: allow ? "Already a tool rule" : "Already a rule; auto mode still blocked this call",
  });
  // An option that is a rule already gets its Edit just below it (PERM-11; a button cannot hold another).
  const withEdit = (value, shownOption) => [shownOption, alreadyAllowed(list, value) && editRuleButton(request, list, value, "Edit this rule")];
  const proposer = sessionLabel(sessions.get(request.session_id), request.cwd);
  const options = values.length > 0 && h("div", {
    class: "sentence-options", role: "radiogroup",
    "aria-label": allow ? "Suggested tool rules, narrowest first" : "Suggested sentences, one per kind of work the call does",
  }, allow
    ? request.suggested_rules.map(({ rule, covers }) => withEdit(rule, option(rule, h("span", { class: "sentence-text" },
      h("code", { class: "rule-text", text: rule }), already(rule), h("span", { class: "rule-covers", text: covers })))))
    : values.map((sentence) => withEdit(sentence, option(sentence, h("span", { class: "sentence-text" }, sentence, already(sentence)),
      sentence === request.proposed_sentence && h("span", { class: "proposed", role: "img", "aria-label": `Proposed by ${proposer}`, title: `Proposed by ${proposer}` }, icon("chat"))))));
  // Once Claude's reading is in, the note above the editor no longer quotes the sentences that already cover the
  // call; those that are not among the options get their Edit on a line of their own here.
  const covering = allow || !readingIn(request) ? []
    : request.covered_by.filter((sentence) => !values.includes(sentence) && alreadyAllowed(list, sentence));
  const coveringLine = covering.length > 0 && h("div", { class: "rule-covering" },
    h("span", { text: covering.length > 1 ? "Your sentences that already allow this call:" : "Your sentence that already allows this call:" }),
    covering.map((sentence) => h("span", { class: "rule-covering-one" }, h("q", { title: sentence, text: clip(sentence, 160) }), editRuleButton(request, list, sentence, "Edit this rule"))));
  const replacing = own ? ui.replacing.get(key) : null;
  const editing = replacing && h("p", { class: "rule-editing" },
    h("span", { title: `Was: ${replacing}`, text: `Your edit changes this ${RULE_LISTS[list].name} in place. ` }),
    h("button", { class: "link-button", type: "button", onclick: () => { ui.replacing.delete(key); saveDrafts(); render(false); } }, "Add a new rule instead"));
  let box = null;
  if (own) {
    box = allow
      ? h("input", { class: "field", type: "text", "data-key": key, spellcheck: "false", placeholder: RULE_LISTS.allow.placeholder, "aria-label": "Tool rule" })
      : h("textarea", { class: "field", rows: 2, "data-key": key, placeholder: RULE_LISTS.auto.placeholder, "aria-label": "Your sentence" });
    box.value = draft;
    box.addEventListener("input", () => onInput(box));
    if (allow) box.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); allowWithRule(request); } });
  }
  const writeOwn = values.length > 0 && !own && h("button", {
    class: "link-button write-own", type: "button",
    onclick: () => {
      ui.open.add(ownKey);
      ui.replacing.delete(key);
      saveDrafts();
      render(false);
      const field = document.querySelector(`:is(#blocked, #next) [data-key="${CSS.escape(key)}"]`);
      if (field) field.focus();
    },
  }, icon("pencil"), "Write my own");
  // A tool rule matches one command of the call; auto mode still judges the others, listed one per line and each cut
  // to its first line, so a long commit message folds away (PERM-22).
  const others = allow && request.other_commands.length > 0 && h("div", { class: "rule-others" },
    h("p", { text: `Auto mode still judges the call's other ${request.other_commands.length > 1 ? "commands" : "command"}:` }),
    h("ul", {}, request.other_commands.map((command) => {
      const first = command.split("\n")[0];
      return h("li", { title: command }, h("code", { text: first === command ? command : `${first} …` }));
    })));
  return h("div", { class: "rule-draft" }, seg, fold, coveringLine, options, editing, box, writeOwn, others);
}

// A command or a script's words (text, or shellCall's coloured parts), clamped to a few lines; a press shows all
// of it, or clamps it again. Selecting text to copy does not.
function clampedText(key, content, title) {
  const open = ui.expanded.has(key);
  return h("pre", {
    class: `call${open ? " expanded" : ""}`, title,
    onclick: (event) => {
      if (String(window.getSelection()) || !(open || event.currentTarget.classList.contains("clipped"))) return;
      toggle(key);
    },
  }, content);
}

// PERM-22: a shell command one part per line, its words coloured by kind (shell.js). The text is
// the command's own, so a copy of it still runs.
function shellCall(command) {
  return CockpitShell.lines(command).map((line, index) => [
    index > 0 && "\n",
    line.indent,
    line.tokens.map((token) => (token.kind === "text" ? token.text : h("span", { class: `sh sh-${token.kind}`, text: token.text }))),
  ]);
}

// PERM-3: what the call does, in Claude's words, from the session's transcript and the files the call runs
// (explain.py): one or two short sentences on what is at stake and the one fact that explains the stop.
// Cockpit asks once the card comes; until the answer is in, the card says Claude is reading. When the run failed,
// the server's one sentence says why and when Cockpit tries again (PERM-17).
function callExplanation(request) {
  const explanation = request.explanation;
  if (explanation === null) {
    return request.state === "open" && h("p", { class: "explain-note" }, h("span", { text: "Reading the call…" }));
  }
  if (explanation.error) {
    const text = explanation.failed_at ? explanation.error : "Retrying the explanation…";
    return h("p", { class: "explain-note failed" }, icon("alert"), h("span", { text }));
  }
  const reading = h("p", { class: "explain" },
    codeSpans(explanation.plain ?? explanation.short));
  // A refusal the session got past without the user says how (PERM-18).
  return request.moved_past
    ? [h("p", { class: "explain-note" }, h("span", { text: `Already handled: ${explanation.moved_on}` })), reading]
    : reading;
}

// What the call changes (PERM-3): an edit's old and new lines, or what each script it runs says it does,
// in its own words or its first lines.
function callChanges(request) {
  return request.changes.map((change, index) => h("div", { class: "change" },
    h("p", { class: "change-title", text: change.title }),
    clampedText(`change:${request.id}:${index}`, change.text, null),
    change.note && h("p", { class: "change-note" }, icon("alert"), h("span", { text: change.note }))));
}

// Claude's reading says why auto mode stopped the call, and which of the user's sentences already allow it
// (PERM-3); until it is in, or when it failed or predates that, the card shows Cockpit's own cause and
// quotes the sentences (PERM-13, PERM-11).
function readingIn(request) {
  return Boolean(request.explanation && request.explanation.plain);
}

// PERM-11: a sentence the user allowed already covers the call, and auto mode stopped it anyway. The server's
// help says only what changes which button to press: an earlier block to answer first, an instruction to
// hold off, or that no tool rule fits (PERM-13); the rule editor shows the rule that would help.
function coveredNote(request) {
  const help = request.cause && request.cause.help;
  const quote = !readingIn(request) && request.covered_by.length > 0;
  if (!quote && !help) return null;
  return h("div", { class: "request-note covered" }, icon("info"), h("span", {},
    quote && [
      request.covered_by.length > 1 ? "Your auto-mode sentences already allow this call: " : "Your auto-mode sentence already allows this call: ",
      request.covered_by.map((sentence, index) => [index > 0 && " and ", h("q", { title: sentence, text: clip(sentence, 120) }),
        alreadyAllowed("auto", sentence) && [" ", editRuleButton(request, "auto", sentence, "Edit")]]),
      /[.!?]$/.test(clip(request.covered_by.at(-1), 120)) ? "" : ".", help ? " " : ""],
    help ? codeSpans(help) : ""));
}

// The kind of a request as an icon: an amber shield for a dialog, a red ban for a blocked call,
// with the count as a badge; its hover card says why in plain words.
function requestTip(request) {
  return [
    requestKind(request),
    requestWhy(request),
    request.count > 1 && `Auto mode blocked this call ${request.count} times`,
  ].filter(Boolean).join("\n");
}

function requestIcon(request) {
  const asked = request.kind === "asked";
  const tip = requestTip(request);
  return h("span", { class: `kind ${request.kind}`, role: "img", "aria-label": requestKind(request), title: tip },
    icon(asked ? "shield" : "ban"), request.count > 1 && h("b", { class: "badge", text: request.count }));
}

// A request as one line: its icon, the call, the session and the age. A click opens the card.
function blockedLine(request, sessions) {
  const key = `req:${request.id}`;
  return h("div", { class: `blocked-line ${request.kind}`, "data-request": request.id, "data-session": request.session_id, onclick: () => toggle(key) },
    requestIcon(request),
    opens(h("code", { class: "call-line", title: request.summary, text: request.summary }), key),
    sessionName(sessions.get(request.session_id), request.session_id, request.cwd, "who"),
    h("span", { class: "row-age", "data-time": request.last_at, text: age(request.last_at) }));
}

// The folder the call runs in, under the call (PERM-26): a relative path such as `rm .venv` means
// something only with it. The user's home folder shows as `~`.
function callFolder(cwd) {
  if (!cwd) return null;
  const home = ui.state.home;
  const folder = home && (cwd === home || cwd.startsWith(`${home}/`)) ? `~${cwd.slice(home.length)}` : cwd;
  return h("p", { class: "call-folder", title: cwd }, "Runs in ", h("code", { text: folder }));
}

// The open card. The top card (`top`) opens with its rule editor on the auto-mode sentences (on the tool
// rules when a sentence already allows the call, PERM-11), unless the user closed it, the screen is a phone's,
// where the editor would push Next up's ask below the fold, or the session's rule is in place already: then
// Retry is all the call needs, and a quiet line beside it says so and opens the editor. Another card's editor
// opens on "Add rule…". The second press applies the rule. A block auto mode lifts when the user asks for the
// work in their own words (PERM-13) opens on that message instead, with Send as you as its first answer and the
// rule editor closed: every blocked call does, but one that may read production's secrets.
function blockedCard(request, sessions, top) {
  const asked = request.kind === "asked";
  const tell = !asked && Boolean(request.ask_draft);
  const list = ruleListOf(request);
  const key = ruleDraftKey(request, list);
  const ruleKey = `rule:${request.id}`;
  // A blocked call whose chosen rule is one already: the press only tells the session to retry.
  const retryOnly = !asked && alreadyAllowed(list, ruleDraft(request, list).trim());
  // The session's own rule in place starts the editor closed; once the user opened the editor, picked in it
  // or wrote a rule, it stays as they left it.
  const openByDefault = top && !PHONE.matches && !tell && !(retryOnly && !ui.drafts.has(key));
  // A dialog offers Run and Deny without a rule: saved rules do not prevent later dialogs of the same kind
  // (PERM-27).
  const ruleOpen = !asked && (ui.expanded.has(ruleKey) || (openByDefault && !ui.closedRules.has(ruleKey)));
  const closeRule = () => {
    ui.expanded.delete(ruleKey);
    ui.closedRules.add(ruleKey);
    render(false);
  };
  const openRule = () => {
    ui.closedRules.delete(ruleKey);
    ui.expanded.add(ruleKey);
    render(false);
    const field = document.querySelector(`:is(#blocked, #next) [data-key="${CSS.escape(key)}"]`);
    if (field) field.focus();
  };
  // A draft started from a rule's Edit changes that rule (PERM-11).
  const changing = ruleOpen && !retryOnly && ui.replacing.has(key);
  const ruleButton = h("button", {
    class: "button", type: "button",
    title: retryOnly
      ? null
      : changing
        ? "Changes the rule below, in every session.\nThen the session is told to retry."
        : ruleOpen
          ? "Adds the rule below, kept for every session.\nThen the session is told to retry."
          : "Add rule: a sentence or tool rule for every session.\nThen the session is told to retry.",
    onclick: ruleOpen || retryOnly ? () => allowWithRule(request) : openRule,
  }, icon(retryOnly ? "retry" : changing ? "pencil" : "plus"), h("span", {
    class: "label",
    text: retryOnly ? "Retry"
      : changing ? "Change rule and retry" : ruleOpen ? "Add rule and retry" : "Add rule…",
  }));
  const ruleNote = retryOnly && !ruleOpen && h("button", {
    class: "link-button rule-note", type: "button", "aria-expanded": "false",
    title: `In place: ${clip(ruleDraft(request, list).trim(), 50)}`,
    onclick: openRule,
  }, h("span", { text: "A rule already allows this" }), icon("chevron"));
  const ended = asked && Boolean(request.hook_ended_at);
  // A call its session took back gets no retry, so nothing would carry a note.
  const noting = !asked && !request.withdrawn_at && noteOpen(request);
  const buttons = asked
    ? [
        h("button", { class: "button primary", type: "button",
          title: ended
            ? "Run: only this call goes ahead; no rule is saved.\nThe session restarts and makes the call again."
            : "Run: only this call goes ahead; no rule is saved.\nThe session's own dialog stays open; the first answer counts.",
          onclick: () => answerQuietly(request, "allow", { rule: null, list: null }) },
          icon("play"), h("span", { class: "label", text: "Run" })),
        iconButton("ban", ended
          ? "Deny: only this call is refused; nothing is saved.\nThe session restarts and is told you said no."
          : "Deny: only this call is refused; nothing is saved.\nThe session is told you refused it.",
          { class: "button ghost push", onclick: () => answerQuietly(request, "deny", undefined) }),
      ]
    : [
        tell && h("button", { class: "button primary", type: "button",
          title: "Send as you: your settings stay the same.\nThe session gets the message above as yours, when its task ends.",
          onclick: () => tellSession(request) },
          icon("send"), h("span", { class: "label", text: "Send as you" })),
        ruleButton,
        ruleNote,
        !request.withdrawn_at && iconButton("chat", noting ? "Remove the note" : "Add note: a line sent with your answer.\nAfter Add rule, the retry then waits until the session's task ends.",
          { class: `button ghost note-toggle${noting ? " on" : ""}`, "aria-pressed": String(noting), onclick: () => toggleNote(request) }),
        iconButton("x", "Dismiss: nothing changes and the session is not told.\nThe call stays blocked and leaves this list.", { class: "button ghost push", onclick: () => answerQuietly(request, "dismiss", undefined) }),
      ];
  // Allow lets the whole call run, so the card shows the call whole, as the summary stops at 2,000 characters and
  // leaves parts of the input out (a Write's content, an Edit's `replace_all`, a NotebookEdit's `edit_mode`): a shell
  // command with its options other than its description under it as JSON (`dangerouslyDisableSandbox`), and another
  // tool's input as JSON. A long one stays clamped until opened. It shows also when
  // the heading is the call itself, which a phone clamps to two lines.
  const options = request.tool_name === "Bash" && request.tool_input
    && Object.fromEntries(Object.entries(request.tool_input).filter(([key]) => key !== "command" && key !== "description"));
  const shown = options && Object.keys(options).length > 0 ? `\n${JSON.stringify(options)}` : "";
  const whole = request.tool_name === "Bash" ? String(request.tool_input?.command ?? request.summary)
    : request.tool_input ? `${request.tool_name} ${JSON.stringify(request.tool_input)}`
    : request.summary;
  const call = clampedText(`call:${request.id}`,
    request.tool_name === "Bash" ? [shellCall(whole), shown] : whole, request.summary);
  const error = ui.requestErrors.get(request.id);
  // Claude's plain heading once its reading is in (PERM-3): the session's own line can carry spec ids, feedback
  // references and times. The session's line stays in the tooltip.
  const heading = request.explanation?.heading || request.description;
  // A heading in words is set in the serif; the call itself, when it is the heading, stays in the sans (look.css).
  const what = h("b", { class: `what${heading ? " words" : ""}`, title: request.description || request.summary, text: heading || request.summary });
  const head = h("div", { class: "blocked-head", onclick: top ? null : (event) => { if (!event.target.closest("a, button")) toggle(`req:${request.id}`); } },
    requestIcon(request),
    top ? what : opens(what, `req:${request.id}`),
    sessionName(sessions.get(request.session_id), request.session_id, request.cwd, "who"),
    h("span", { class: "row-age", "data-time": request.last_at, text: age(request.last_at) }));
  return h("div", { class: `card blocked-card ${request.kind}${ruleOpen ? " rule-open" : ""}${tell ? " tell-open" : ""}${noting ? " note-open" : ""}`, "data-session": request.session_id, "data-request": request.id },
    head,
    h("p", { class: "why", title: requestTip(request), text: requestLine(request) }),
    callExplanation(request),
    request.cause && !readingIn(request) && h("p", { class: "cause" }, codeSpans(request.cause.text)),
    call,
    callFolder(request.cwd),
    callChanges(request),
    // A dialog shows no sentences that cover it, which never answer a dialog, and no note on why it asks: Run does
    // what the user expects, so the card needs no explanation (PERM-27).
    !asked && coveredNote(request),
    ended && h("p", { class: "request-note dialog-ended" }, icon("retry"), h("span", { text: DIALOG_ENDED })),
    tell && tellBox(request),
    ruleOpen && ruleEditor(request, sessions, closeRule),
    noting && noteBox(request),
    request.withdrawn_at && h("p", { class: "request-note" }, icon("back"), h("span", {
      text: `The session no longer needs this call: it withdrew it at ${clock(request.withdrawn_at)}. Your answer still adds its rule; otherwise the card goes 30 minutes after you last worked on it.`,
    })),
    error && h("p", { class: "request-error", role: "alert" }, icon("alert"), h("span", { text: error })),
    h("div", { class: "actions" }, buttons));
}

// A dialog that closed in its session without an answer from here (PERM-23): answered in the terminal, or closed on its
// own, as Claude Code's removal safety check does after two minutes in a background session. The card the user may be
// reading stays a minute, with nothing to press, and fades over that minute; the server then leaves it out. The
// negative delay keeps the fade where it was across redraws.
function settledCard(request, sessions) {
  const card = h("div", { class: "card blocked-card settled", "data-session": request.session_id, "data-request": request.id },
    h("div", { class: "blocked-head" },
      h("span", { class: "kind settled", role: "img", "aria-label": "Dialog closed", title: "The dialog closed in its session" }, icon("check")),
      h("b", { class: "what", title: request.description || request.summary, text: request.explanation?.heading || request.description || request.summary }),
      sessionName(sessions.get(request.session_id), request.session_id, request.cwd, "who"),
      h("span", { class: "row-age", "data-time": request.last_at, text: age(request.last_at) })),
    // Claude's reading when it is in; a reading still to come or one that failed is moot now.
    request.explanation?.plain && h("p", { class: "explain" }, codeSpans(request.explanation.plain)),
    clampedText(`call:${request.id}`, request.tool_name === "Bash"
      ? [shellCall(String(request.tool_input?.command ?? request.summary))] : request.summary, request.summary),
    callFolder(request.cwd),
    h("p", { class: "request-note settled-note" }, h("span", {
      text: `The dialog closed in the session at ${clock(request.resolved_at)}, answered there or on its own, and the session went on. Nothing to do here.`,
    })));
  // The server's clock, so a browser clock that runs off does not start the fade late or end it early.
  card.style.animationDelay = `${-Math.max(0, (Date.now() + ui.clockOffset - new Date(request.resolved_at).getTime()) / 1000)}s`;
  return card;
}

// A session at a dialog in its terminal that Cockpit cannot answer (its hook gave up, or it came before
// the hooks): one line that says where to answer it, and Reply opens its last message and a box. A session
// that stopped to ask the user something is a step in the queue instead (PERM-8).
function waitingCard(session, state) {
  const key = `msg:session:${session.session_id}`;
  const open = ui.expanded.has(key);
  const restarting = restartUnderWay(session, state);
  const { heading: needs, command } = dialogAsk(session);
  const kind = h("span", { class: "kind asked", role: "img", "aria-label": "A dialog in its terminal", title: "Terminal dialog" }, icon("shield"));
  return h("div", { class: `card waiting-card${open ? " open" : ""}`, "data-session": session.session_id },
    h("div", { class: "blocked-head", onclick: (event) => { if (!event.target.closest("a, button")) toggle(key); } },
      kind,
      h("span", { class: "what", title: needs, text: needs }),
      sessionName(session, session.session_id, session.cwd, "who"),
      h("span", { class: "row-age", "data-time": session.updated_at, text: age(session.updated_at) }),
      restarting
        ? h("span", { class: "restarting", text: "Restarting…" })
        : h("button", {
          class: "button small", type: "button", onclick: () => restartSession(session),
          title: "The session makes the call again; you answer it here",
        }, icon("retry"), "Restart"),
      itermLink(session, { class: "button small" }, icon("terminal"), "iTerm2"),
      iconButton(open ? "x" : "reply", open ? "Close" : "Reply", { class: "button small ghost", onclick: () => toggle(key) })),
    command && clampedText(`call:session:${session.session_id}`, [shellCall(command)], command),
    h("p", {
      class: "dialog-note",
      text: "Cockpit cannot answer this dialog: it waits in the session's terminal, and the session reads no messages until it is answered. "
        + (TOUCH.matches ? "Restart, and the session asks again here." : "Restart, and the session asks again here; or answer it in iTerm2."),
    }),
    open && session.last_message_html && prose(`${key}:last`, "prose last-message", session.last_message_html),
    open && linkList(key, session.links, session.last_message_html || ""),
    open && composer(key, { session, context: {}, project: null, step: null, handOff: false, toSession: false }));
}

// What a dialog in a session's terminal asks, as a heading in words, with the shell command apart: Claude Code's own
// line is "approve Bash:" and then the whole command.
function dialogAsk(session) {
  const needs = session.needs || session.detail || "A dialog waits for an answer";
  const bash = needs.match(/^approve Bash: ([\s\S]*)$/);
  return bash ? { heading: "The session asks to run a command", command: bash[1] } : { heading: needs, command: null };
}

// A restart is under way from the user's press until its message has gone out, and for 20 s after: the
// session listing can still show the dialog that long. A failed restart message offers Restart again.
const RESTART_SETTLE_MS = 20000;

function restartUnderWay(session, state) {
  if (!ui.restarts.has(session.session_id)) return false;
  const message = state.messages.find((other) => other.id === ui.restarts.get(session.session_id));
  if (!message) return true;
  if (message.state === "sent") return Date.now() - new Date(message.sent_at).getTime() < RESTART_SETTLE_MS;
  return message.state !== "failed";
}

// A dialog Cockpit cannot answer (PERM-8): the server stops the session and resumes it with a message,
// and the call the session makes again comes here as a permission request.
function restartSession(session) {
  ui.restarts.set(session.session_id, null);
  render(false);
  let gone = false;
  return run(`restart:${session.session_id}`, async () => {
    try {
      const message = await api("POST", `/api/sessions/${session.session_id}/restart`);
      ui.restarts.set(session.session_id, message.id);
    } catch (error) {
      ui.restarts.delete(session.session_id);
      // The dialog closed after the page last looked: nothing waits, and the card leaves.
      if (error.status === 409 && error.message.includes("no longer waits at a dialog")) {
        gone = true;
        ui.dialogsGone.set(session.session_id, Date.now());
        return;
      }
      throw refused("Not restarted", error);
    }
  }, () => toast(gone
    ? `Nothing to restart: ${session.name || session.id} no longer waits at a dialog`
    : `Restarting ${session.name || session.id}: when it makes the call again, it comes here`));
}

// A session whose Restart found no dialog left keeps no card while the session listing catches up.
function dialogGone(session) {
  const at = ui.dialogsGone.get(session.session_id);
  return at !== undefined && Date.now() - at < RESTART_SETTLE_MS;
}

// Blocked tool calls and waiting sessions, above Next up: an agent stops on each. The first
// request is open; past three the rest are behind "+N more".
const BLOCKED_TOP = 3;

// A secret answer to Codex while the user types it: in memory only, so a redraw keeps it and no storage holds it.
const secretAnswers = new Map();

function secretField(key) {
  const el = h("input", { type: "password", autocomplete: "off", placeholder: "Your answer…", "data-key": key });
  el.value = secretAnswers.get(key) || "";
  el.addEventListener("input", () => { secretAnswers.set(key, el.value); });
  return el;
}

function withoutCodexRequest(state, id) {
  return { ...state, codex_requests: (state.codex_requests || []).filter((other) => other.id !== id) };
}

function codexRequestCard(request) {
  const p = request.params;
  const session = sessionMap(ui.state).get(p.threadId);
  const errorKey = `codex:${request.id}`;
  // An answer takes the card off at once, as on a Claude call's card (PERM-30); if Cockpit or Codex refuses it, the
  // card comes back with the reason on it. An answer to a secret question goes once from this page, like a sign-in
  // code, never into the outbox's storage, and stays in its box until it went through.
  const send = (result, secret) => {
    ui.requestErrors.delete(errorKey);
    return run(`codex:${request.id}`, async () => {
      const path = `/api/codex/requests/${request.id}/answer`;
      // The reason is stored before optimistic() redraws the card it gives back, so it comes back with the card.
      const answer = async () => {
        try {
          return await (secret ? apiCredential(path, result) : api("POST", path, result));
        } catch (error) {
          ui.requestErrors.set(errorKey, error.message);
          throw new Error(`The answer did not go through, the request is back: ${error.message}`, { cause: error });
        }
      };
      await optimistic((state) => withoutCodexRequest(state, request.id), answer, null);
      for (const key of secretAnswers.keys()) if (key.startsWith(`codex:${request.id}:`)) secretAnswers.delete(key);
    });
  };
  const title = request.method === "item/tool/requestUserInput" ? "Codex needs your answer" : "Codex requests permission";
  const error = ui.requestErrors.get(errorKey);
  const content = h("div", { class: "blocked-card", "data-codex": request.id },
    h("h3", { text: title }), h("p", { text: session?.name || p.threadId }),
    p.reason && h("p", { text: p.reason }),
    error && h("p", { class: "request-error", role: "alert" }, icon("alert"), h("span", { text: error })));
  if (request.method === "item/tool/requestUserInput") {
    const fields = new Map();
    for (const q of p.questions || []) {
      content.append(h("p", { text: q.question }));
      if (q.options?.length) content.append(h("ul", {}, q.options.map((o) => h("li", {
        text: `${o.label}${o.description ? `: ${o.description}` : ""}`,
      }))));
      const field = q.isSecret ? secretField(`codex:${request.id}:${q.id}`) : draftArea(`codex:${request.id}:${q.id}`, "Your answer…", 2, "");
      field.setAttribute("aria-label", q.question);
      fields.set(q.id, field);
      content.append(field);
    }
    content.append(h("button", { type: "button", class: "button", onclick: () => send({
      answers: Object.fromEntries([...fields].map(([id, field]) => [id, { answers: [field.value] }])),
    }, (p.questions || []).some((q) => q.isSecret)) }, "Send answer"));
  } else {
    const details = Object.fromEntries(Object.entries({
      command: p.command || p.item?.command, cwd: p.cwd, network: p.networkApprovalContext,
      permissions: p.permissions, changes: p.item?.changes, grantRoot: p.grantRoot,
    }).filter(([, value]) => value !== undefined && value !== null));
    // The whole request, clamped like a Claude call's, since Allow approves all of it (pattern 14).
    content.append(clampedText(`codex-call:${request.id}`, JSON.stringify(details, null, 2), null));
    // The folder in plain sight too, as a long command can push it out of the clamped request (PERM-26).
    const folder = callFolder(p.cwd);
    if (folder) content.append(folder);
    const offered = p.availableDecisions;
    if (!offered || offered.includes("accept")) content.append(h("button", {
      type: "button", class: "button", onclick: () => send({ decision: "accept" }, false),
    }, request.method === "item/permissions/requestApproval" ? "Allow for this turn"
      : request.method === "item/commandExecution/requestApproval" ? "Run" : "Allow once"));
    const denial = !offered || offered.includes("decline") ? "decline" : "cancel";
    content.append(h("button", { type: "button", class: "button ghost", onclick: () => send({ decision: denial }, false) }, "Deny"));
  }
  return content;
}

// The sessions as a blocked call's card names them: by their item's short title when they work on one, the item that
// names them first before any other. A background session's own name is the start of the message that began it, which
// can carry feedback numbers and times (PERM-3).
function cardSessions(state) {
  const sessions = sessionMap(state);
  // The first item that names a session first among its sessions wins; else the first item that lists it.
  const titles = new Map();
  for (const item of state.items) if (item.sessions.length && !titles.has(item.sessions[0])) titles.set(item.sessions[0], item.title);
  for (const item of state.items) for (const id of itemSessions(item)) if (!titles.has(id)) titles.set(id, item.title);
  for (const [id, title] of titles) sessions.set(id, { ...(sessions.get(id) || { session_id: id }), name: title });
  return sessions;
}

// The shown requests with the closed dialogs among them, in the server's order (dialogs first, newest first), so a
// card that closes stays where the user saw it (PERM-23).
function inPlace(shown, settled) {
  const rank = (request) => [request.kind === "asked" ? 0 : 1, -Date.parse(request.last_at), -request.id];
  const before = (a, b) => {
    const [x, y] = [rank(a), rank(b)];
    const at = x.findIndex((value, index) => value !== y[index]);
    return at >= 0 && x[at] < y[at];
  };
  const out = [...shown];
  for (const request of settled) {
    const at = out.findIndex((other) => before(request, other));
    out.splice(at < 0 ? out.length : at, 0, request);
  }
  return out;
}

// The rule editor's first list on a request's open card: a sentence when Claude suggests one, unless a sentence
// already allows the call; a covered call opens on tool rules (PERM-11), unless Claude named a sentence for the
// blocked action (PERM-13).
function openOnSentence(request) {
  if (request.suggested_sentences.length && (!request.covered_by.length || request.action_sentence) && !ui.choices.has(`perm:req:${request.id}`)) {
    ui.choices.set(`perm:req:${request.id}`, "auto");
  }
}

// On the page with the rail, each blocked call, dialog, Codex question and session at a terminal dialog is a warning,
// like other sign-in warnings (SPEC PERM-25, UI-52): a dark red row at the top of Inbox, above the step the user
// is on, which opens its card on Next up. A dialog that closed in its session stays only while its card is open there
// (PERM-23). The classic page keeps the Blocked and waiting panel above Next up.
function blockedWarnings(state) {
  if (!SIDEBAR) return [];
  const sessions = cardSessions(state);
  const named = sessionMap(state);
  const who = (session, id) => (session && session.name) || id.slice(0, 8);
  const codex = (state.codex_requests || []).map((request) => {
    const asks = request.method === "item/tool/requestUserInput";
    const title = `${asks ? "Codex needs your answer" : "Codex requests permission"}: ${who(named.get(request.params.threadId), request.params.threadId)}`;
    return {
      key: `codex:${request.id}`, tone: "fail", mark: asks ? "chat" : "shield", tip: title, short: title, title,
      more: null, fix: null, close: null, extra: null, row: { "data-codex": request.id },
      card: () => h("article", { class: "card next-card codex-next" }, codexRequestCard(request)),
    };
  });
  // The card on Next up, whether the user opened it or it came up first (ui.shownWarning, from the last drawing).
  const open = ui.warningOpen || ui.shownWarning;
  const settled = (state.settled_requests || []).filter((request) => open === `request:${request.id}`);
  const requests = inPlace(state.permission_requests, settled).map((request) => {
    const heading = request.explanation?.heading || request.description || request.summary;
    const title = `${requestKind(request)}: ${heading}`;
    return {
      key: `request:${request.id}`, tone: "fail", mark: request.settled ? "check" : request.kind === "asked" ? "shield" : "ban",
      tip: `${title}\n${who(sessions.get(request.session_id), request.session_id)}`, short: heading, title,
      more: null, fix: null, close: null, extra: null, row: { "data-request": request.id },
      card: () => {
        if (request.settled) return settledCard(request, sessions);
        openOnSentence(request);
        return blockedCard(request, sessions, true);
      },
    };
  });
  const waiting = state.waiting_sessions.filter((session) => !dialogGone(session)).map((session) => {
    const needs = dialogAsk(session).heading;
    return {
      key: `session:${session.session_id}`, tone: "fail", mark: "shield", tip: `${needs}\n${who(session, session.session_id)}`,
      short: needs, title: needs, more: null, fix: null, close: null, extra: null, row: { "data-session": session.session_id },
      card: () => waitingCard(session, state),
    };
  });
  return [...codex, ...requests, ...waiting];
}

function renderBlocked(state) {
  // The page with the rail shows these as warnings in Inbox (blockedWarnings).
  if (SIDEBAR) { fill("blocked"); return; }
  const requests = state.permission_requests;
  const waiting = state.waiting_sessions.filter((session) => !dialogGone(session));
  const codex = state.codex_requests || [];
  // Refusals the sessions got past without the user fold under one line at the end (PERM-18).
  const past = state.moved_past || [];
  const settled = state.settled_requests || [];
  if (!requests.length && !waiting.length && !codex.length && !past.length && !settled.length) { fill("blocked"); return; }
  // The sentence options mark the ones already allowed, from the rules as last read.
  if ((requests.length || past.length) && ui.permissions === null && !ui.permissionsError) loadPermissions();
  const sessions = cardSessions(state);
  const top = requests[0];
  if (top) openOnSentence(top);
  const shown = ui.showAllBlocked ? requests : requests.slice(0, BLOCKED_TOP);
  const hidden = requests.length - shown.length;
  // One panel; its heading, "Blocked and waiting", is the panel's ::after (marks.css).
  fill("blocked",
    h("div", { class: "blocked-list" },
      codex.map(codexRequestCard),
      inPlace(shown, settled).map((request) => (request.settled ? settledCard(request, sessions)
        : request === top || ui.expanded.has(`req:${request.id}`) ? blockedCard(request, sessions, request === top)
          : blockedLine(request, sessions))),
      requests.length > BLOCKED_TOP && h("button", {
        class: "link-button more-row", type: "button", onclick: () => { ui.showAllBlocked = !ui.showAllBlocked; render(false); },
      }, ui.showAllBlocked ? "Show fewer" : `+${hidden} more`),
      waiting.map((session) => waitingCard(session, state)),
      past.length > 0 && h("button", {
        class: "link-button more-row", type: "button", onclick: () => { ui.showMovedPast = !ui.showMovedPast; render(false); },
      }, ui.showMovedPast ? "Hide the calls sessions got past"
        : `${past.length} blocked ${past.length === 1 ? "call" : "calls"} the sessions got past without you`),
      ui.showMovedPast && past.map((request) => (ui.expanded.has(`req:${request.id}`)
        ? blockedCard(request, sessions, false)
        : blockedLine(request, sessions)))));
}

// An item's controls in its open row: priority tag, Priorities and stage menus, whose goal it blocks, archive.
// `place`: what keys this copy's menus and field, `${item.id}` on the item's row and `${item.id}:step:N` in step N's
// task fold, so two copies on the page keep their own open menu and focus.
// Per Blocks box, its latest save on its way: only that one puts its words back when refused.
const blocksSaves = new Map();

function itemControls(item, state, place) {
  const places = goalPlaces(state.goals);
  const goals = state.goals.filter((goal) => typeof goal.id === "number")
    .map((goal) => [goal.id, places.has(goal.id) ? `${places.get(goal.id)}. ${goal.text}` : `Paused: ${goal.text}`]);
  const goalNow = goals.find(([id]) => id === item.goal_id);
  const goalMenu = menu(`menu:goal:${place}`, [icon("target"), h("span", { class: "label", text: goalNow ? clip(goalNow[1], 28) : "No priority" })], [
    ["No priority", null, () => patchItem(item, { goal_id: null }, null)],
    ...goals.map(([id, text]) => [clip(text, 60), null, () => patchItem(item, { goal_id: id }, null)]),
  ], "goal-menu");
  const goal = goalNow && state.goals.find((other) => other.id === goalNow[0]);
  if (goal) labelSummary(goalMenu, places.has(goal.id) ? `Priority ${places.get(goal.id)}: ${goal.text}` : `Paused priority: ${goal.text}`, false);
  const stageMenu = menu(`menu:stage:${place}`, [h("span", { class: "label", text: item.stage ? STAGE_LABELS[item.stage] : "No stage" }), icon("chevron")], [
    ["No stage", null, () => patchItem(item, { stage: null }, null)],
    ...Object.entries(STAGE_LABELS).map(([value, label]) => [label, null, () => patchItem(item, { stage: value }, null)]),
  ], "stage-menu");
  const blocksKey = `blocks:${place}`;
  const blocks = h("input", { class: "field", "data-key": blocksKey, value: ui.drafts.get(blocksKey) ?? item.blocks, placeholder: "Blocks whose goal?", "aria-label": "Blocks" });
  blocks.addEventListener("input", () => { ui.drafts.set(blocksKey, blocks.value); saveDrafts(); });
  blocks.addEventListener("keydown", (event) => { if (event.key === "Enter") blocks.blur(); });
  blocks.addEventListener("change", () => {
    if (!blocks.isConnected) return; // A redraw replaced this box; its draft is in the new one.
    const sent = blocks.value;
    const fields = { blocks: sent.trim() };
    clearDraft(blocksKey, sent);
    const save = {};
    blocksSaves.set(blocksKey, save);
    // In order with the item's other changes (patchItem); a refused save puts its words back unless newer words
    // wait in the box or a later save went after it.
    patchItem(item, fields, null).then((done) => {
      if (blocksSaves.get(blocksKey) === save) blocksSaves.delete(blocksKey);
      else return;
      if (done || ui.drafts.has(blocksKey)) return;
      ui.drafts.set(blocksKey, sent);
      saveDrafts();
      render(false);
    });
  });
  return h("div", { class: "item-controls" },
    priorityTag(item, ""),
    pinKey(item, "button ghost"),
    goalMenu,
    stageMenu,
    h("label", { class: "blocks-field" }, icon("blocks"), blocks));
}

// The sessions on an item, each with its state, what it does now, its marks, Claude use and age (VIEW-3: the list of
// work shows each session in its item's row). In the row (`talk`), a press on a session after the first opens a
// composer to it; the row's own composer talks to the first.
// The sessions an item lists, in its row or on its step's card. A session's name opens its own composer where the
// session lives (sessionHome), so its draft has one field on the page; elsewhere the name goes there.
function sessionList(linked, item, step, state) {
  return linked.length > 0 && h("div", { class: "session-list" }, linked.map(([sessionId, session]) => {
    const key = `msg:side:${sessionId}`;
    const home = session && sessionHome(state, sessionId);
    const talks = Boolean(home) && home.item.id === item.id && (home.step?.id ?? null) === (step?.id ?? null) && !ui.heldKeys.has(key);
    const name = sessionName(session, sessionId, "", "name");
    if (talks) { opens(name, key); name.addEventListener("click", () => toggle(key)); }
    // The page with the list of items opens any session's conversation by its id, also one no longer listed in
    // the state's sessions, as the list at the left did before its sessions moved to the card.
    else if (session || SIDEBAR) {
      name.setAttribute("role", "button");
      name.tabIndex = 0;
      name.addEventListener("click", () => openKeyboardSession(sessionId));
      name.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        openKeyboardSession(sessionId);
      });
    }
    return h("div", { class: "session-item", "data-session": sessionId },
      h("span", { class: "session-entry" }, sessionBot(), stateDot(session ? sessionState(session) : "stopped", session ? sessionState(session) : "not running"),
        name,
        session && sessionMarks(session),
        session && session.detail && h("span", { class: "meta", title: session.detail, text: clip(session.detail, 80) }),
        session && sessionSpend(session),
        session && h("span", { class: "row-age", "data-time": session.updated_at, text: age(session.updated_at) })),
      talks && ui.expanded.has(key) && composer(key, { session, context: {}, project: null, step: null, handOff: false, toSession: false }));
  }));
}

// The task behind a step (VIEW-18): work that waits for the user shows as its steps and has no row of its own, so
// each of its step cards folds in what the row held: the project, where it stands (its stage chip, which names the
// review it waits on, whom else it waits on, its summary and the session's latest status), the sessions, and the
// priority, pin, goal, stage and blocks controls.
function taskFold(item, step, state) {
  const sessions = sessionMap(state);
  const key = `task:${step.id}`;
  return h("div", { class: "details task-details" }, disclosure(key, "detail", "Task details",
    // The task's project shows in More, so the fold leaves it out.
    h("div", { class: "task-body" },
      h("p", { class: "task-stands" }, itemStageChip(item, sessions), item.stage === "review" ? [] : waitingChips(item)),
      item.summary && item.summary !== item.title && h("p", { class: "item-note" }, summaryWords(item, "summary")),
      item.agent_summary && h("p", { class: "item-status" }, h("span", { class: "item-status-label", text: "Latest from the session" }), summaryWords(item, "agent_summary")),
      sessionList(itemSessions(item).map((sessionId) => [sessionId, sessions.get(sessionId)]), item, step, state),
      itemControls(item, state, `${item.id}:step:${step.id}`)), false));
}

// History (VIEW-10, VIEW-13): what the user asked or did on an item, what came back, and how the time
// between went. The server pairs them (GET /api/items/{id}/history); the page fetches an item's history
// when it draws it and again once the item or its steps change, or after HISTORY_STALE_MS, also while nothing
// redraws the page (`refreshShownHistories`).
// Short, so a session's reply shows soon after Cockpit read it.
const HISTORY_STALE_MS = 15000;
// A text this long shows whole; a longer one shows its summary, or its first words until Claude wrote one.
const HISTORY_SHORT = 140;
const TIME_KIND_WORDS = {
  agent: "agents working",
  you: "you on it",
  waiting_on_you: "waiting on you",
  waiting: "waiting",
};
const TIME_KINDS = Object.keys(TIME_KIND_WORDS);

// What the page knows that changes an item's history: the item and its open steps. Every place that shows
// the history asks with this one version, so two of them never take turns fetching it.
function historyVersion(itemId, state) {
  const item = state.items.find((other) => other.id === itemId);
  const steps = [...state.queue, ...state.ready_next, ...state.snoozed, ...state.with_agents]
    .filter((step) => step.item_id === itemId)
    .sort((a, b) => a.id - b.id)
    .map((step) => `${step.id}:${step.updated_at}:${step.handoff_message_id}`);
  // The user's messages on the item, so one they just sent shows at once, again with the words Add more joins to
  // it, and again when it reaches the session.
  const said = state.messages.filter((message) => message.author === "user" && message.item_id === itemId)
    .map((message) => `${message.id}:${message.state}:${message.text}`);
  return [item ? item.updated_at : "", ...steps, ...said].join("|");
}

function historyOf(itemId, version) {
  const known = ui.histories.get(itemId);
  const fresh = known && known.version === version && Date.now() - known.at < HISTORY_STALE_MS;
  if (!fresh && !(known && known.loading)) loadHistory(itemId, known, version, false);
  // A fetch begun ahead (`warmHistory`) now has a card waiting for it, so it redraws when it lands.
  if (known && known.loading) known.quiet = false;
  return known || null;
}

// The history of an item whose step is about to show on Next up (`warmNext`), fetched before it shows so its card
// draws whole at once rather than with a ghost the history then pushes down. It redraws nothing when it lands,
// unless a card asked for it in the meantime.
function warmHistory(itemId, state) {
  const version = historyVersion(itemId, state);
  const known = ui.histories.get(itemId);
  const fresh = known && known.version === version && Date.now() - known.at < HISTORY_STALE_MS;
  if (!fresh && !(known && known.loading)) loadHistory(itemId, known, version, true);
}

// What a history says, without the times that run on with the clock: `now` and each gap's time split.
function historyPrint(data) {
  return JSON.stringify({ ...data, now: null, entries: data.entries.map((entry) => ({ ...entry, after: { ...entry.after, time: null } })) });
}

// Fetches an item's history and redraws when what it says changed or it did not load.
function loadHistory(itemId, known, version, quiet) {
  ui.histories.set(itemId, { ...known, loading: true, quiet });
  api("GET", `/api/items/${itemId}/history`).then(
    (data) => {
      const wanted = !ui.histories.get(itemId).quiet;
      ui.histories.set(itemId, { data, version, at: Date.now(), loading: false, error: "" });
      if (wanted && (!known || !known.data || known.error || historyPrint(data) !== historyPrint(known.data))) safeRender();
    },
    (error) => {
      console.error(`history of item ${itemId} did not load`, error);
      const wanted = !ui.histories.get(itemId).quiet;
      ui.histories.set(itemId, { ...known, version, at: Date.now(), loading: false, error: error.message });
      if (wanted) safeRender();
    },
  );
}

// A history on the page refetches once HISTORY_STALE_MS old, also while nothing else redraws the page: a
// session's reply can come in with no other change to the page's state.
function refreshShownHistories() {
  for (const section of document.querySelectorAll("section:is(.history, .hist-fold-card)[data-item]")) {
    const itemId = Number(section.dataset.item);
    const known = ui.histories.get(itemId);
    if (known && !known.loading && Date.now() - known.at >= HISTORY_STALE_MS) loadHistory(itemId, known, known.version, false);
  }
}

// A moment in a tooltip: its time today, else its date and time; a weekday alone is unclear past a week.
function stamp(iso) {
  const date = new Date(iso);
  const time = date.toLocaleTimeString([], TIME_FORMAT);
  if (date.toDateString() === new Date().toDateString()) return time;
  return `${date.toLocaleDateString([], { day: "numeric", month: "short" })} ${time}`;
}

function longest(seconds) {
  if (seconds < 60) return `${Math.round(seconds)} s`;
  return span(Math.floor(seconds / 60));
}

// A text: whole when short, else its summary (or first words) with Show all. Shown whole, a session's reply is
// `html`, the server's rendering with its mentions of other items as links (SPEC UI-74); other texts pass null.
function historyText(key, text, summary, html) {
  const whole = text.length <= HISTORY_SHORT || ui.expanded.has(key);
  if (text.length <= HISTORY_SHORT) return h("span", html ? { class: "hist-text", html } : { class: "hist-text", text });
  // Until Claude summarised it, a text shows its first words, with markdown links as their labels.
  const plain = () => text.replace(/\[([^\]]+)\]\([^)]+\)/g, "$1").replace(/[`*]/g, "").replace(/\s+/g, " ");
  const shown = whole ? text : clip(summary || plain(), summary ? 2 * HISTORY_SHORT : HISTORY_SHORT);
  return h("span", { class: `hist-text${whole ? " whole" : ""}` },
    whole && html ? h("span", { html }) : shown, " ",
    h("button", { class: "link-button hist-more", type: "button", onclick: (event) => { event.stopPropagation(); toggle(key); } },
      whole ? "Show less" : "Show all"));
}

const ANSWER_WORDS = {
  waiting: "No answer yet",
  none: "No answer before your next action",
  unsent: "Not with the session yet",
};

// `whole`: the answer to the step card's current exchange, whose reply shows whole, since the user waits for it
function answerLine(key, entry, data, whole) {
  const answer = entry.answer;
  let who = "Step";
  let text;
  if (answer.kind === "step") {
    text = historyText(`${key}:answer`, [answer.text, answer.why].filter(Boolean).join(" "), answer.summary, null);
  } else if (answer.kind === "closed") {
    who = answer.action === "done" ? "Closed" : "Dismissed";
    text = historyText(`${key}:answer`, answer.text, null, null);
  } else if (answer.kind === "reply") {
    who = "Reply";
    text = whole && answer.text ? h("span", { class: "hist-text whole", html: answer.text_html })
      : historyText(`${key}:answer`, answer.text || "Ended its turn without a reply", answer.summary, answer.text ? answer.text_html : null);
  } else {
    who = "";
    text = h("span", { class: "hist-text quiet", text: ANSWER_WORDS[answer.kind] });
  }
  return h("div", { class: "hist-line hist-answer", "data-session": answer.kind === "reply" ? answer.session_id : null },
    who && h("span", { class: "hist-who", "data-time": answer.at || null, text: who }), text);
}

// `latest`: this is the user's latest message on the item, which stands out in the user's colour (VIEW-15).
// The user's message shows in their own words: its first words with Show all, never a summary.
function youLine(key, entry, latest) {
  return h("div", { class: `hist-line hist-you${latest ? " latest" : ""}` },
    h("span", { class: "hist-who", text: "You" }),
    entry.you.kind !== "message" ? h("span", { class: "hist-text", text: entry.you.text })
      : hasQuote(entry.you.text) ? quotedHistory(`${key}:you`, entry.you.text)
        : historyText(`${key}:you`, entry.you.text, null, null));
}

// A message of the user's with a quote in it, as history shows it: shut, its first words with each quote on the line;
// with Show all, every word, each quote a block of its own.
function quotedHistory(key, text) {
  const open = ui.expanded.has(key);
  const more = quotedWords(text, false).more;
  return h("span", { class: `hist-text${open || more <= 0 ? " whole" : ""}` },
    quotedWords(text, open || more <= 0).nodes,
    more > 0 && [" ", h("button", { class: "link-button hist-more", type: "button", onclick: (event) => { event.stopPropagation(); toggle(key); } },
      open ? "Show less" : "Show all")]);
}

function stepTag(entry, data) {
  const step = entry.step_id && data.steps[entry.step_id];
  // A step that came with a merged task (SPEC NEXT-55) always says which task it came from, in the merge pink.
  if (step && step.merged_from) {
    const ask = linkWords(step.ask);
    return h("span", { class: "chip hist-tag merged", "data-tip": ask, text: clip(`From ${step.merged_from.title}: ${ask}`, 48) });
  }
  const stepIds = new Set(data.entries.map((other) => other.step_id).filter(Boolean));
  if (stepIds.size < 2 || !entry.step_id) return null;
  const ask = step && linkWords(step.ask);
  return step && h("span", { class: "chip hist-tag", "data-tip": ask, text: clip(ask, 48) });
}

function historyEntry(key, entry, data, compact, latest) {
  return h("li", { class: `hist-entry${compact ? " compact" : ""}` },
    histWhen(entry.at),
    h("div", { class: "hist-body" },
      stepTag(entry, data),
      youLine(key, entry, latest),
      answerLine(key, entry, data, false),
      entry.notes.map((note) => h("div", { class: "hist-note", text: note }))));
}

// VIEW-13: the time from one action to the next as a bar, a segment per stretch, with the totals beside
// it and each stretch's times on hover.
function timeBar(key, after, label) {
  const time = after.time;
  const total = (new Date(time.to) - new Date(time.from)) / 1000;
  if (total < 60) return null;
  const segments = time.segments.map(([from, to, kind]) => {
    const seconds = (new Date(to) - new Date(from)) / 1000;
    const part = h("span", {
      class: `time-part ${kind}`,
      "data-tip": `${capitalize(TIME_KIND_WORDS[kind])} · ${longest(seconds)} · ${stamp(from)}–${stamp(to)}`,
    });
    // The page's Content-Security-Policy drops style attributes; a style set from script stands.
    part.style.flexGrow = String(Math.max(seconds, total / 400));
    return part;
  });
  const totals = timeTotals(time.seconds);
  const others = after.others.length > 0 && disclosure(`${key}:others`, "hist-others",
    after.others_line,
    h("ul", {}, after.others.map((other) => h("li", {},
      h("span", { class: "hist-who", "data-time": other.at, text: `${capitalize(other.author)}, ${ago(other.at)}` }),
      h("span", { class: "hist-text", text: clip(other.text.replace(/\s+/g, " "), 240) })))));
  return h("li", { class: "hist-gap" },
    h("span", { class: "hist-when", text: label || longest(total) }),
    h("div", { class: "hist-body" },
      h("div", { class: "time-bar", role: "img", tabindex: "0", "aria-label": `${longest(total)}: ${totals.map((el) => el.textContent).join(", ")}` }, segments),
      h("div", { class: "time-totals" }, totals),
      others || (after.others_line && h("div", { class: "hist-note", text: `Between: ${after.others_line}` }))));
}

// A time bar's legend: each kind with a minute or more, and its total.
function timeTotals(seconds) {
  return TIME_KINDS.filter((kind) => seconds[kind] >= 60)
    .map((kind) => h("span", { class: "time-total" }, h("i", { class: `time-swatch ${kind}`, "aria-hidden": "true" }), `${TIME_KIND_WORDS[kind]} ${longest(seconds[kind])}`));
}

// The folded Earlier line's bar: all the time of the exchanges it holds in one bar, a part per kind
// as wide as its share, with the totals on hover as on the bars between exchanges.
function earlierTime(entries) {
  const seconds = Object.fromEntries(TIME_KINDS.map((kind) => [kind, 0]));
  for (const entry of entries) for (const kind of TIME_KINDS) seconds[kind] += entry.after.time.seconds[kind];
  const total = TIME_KINDS.reduce((sum, kind) => sum + seconds[kind], 0);
  if (total < 60) return null;
  const parts = TIME_KINDS.filter((kind) => seconds[kind] > 0).map((kind) => {
    const part = h("span", { class: `time-part ${kind}`, "data-tip": `${capitalize(TIME_KIND_WORDS[kind])}, ${longest(seconds[kind])} of ${longest(total)}` });
    part.style.flexGrow = String(seconds[kind]);
    return part;
  });
  const totals = timeTotals(seconds);
  return h("span", { class: "hist-earlier-time" },
    h("span", { class: "time-bar", role: "img", "aria-label": `${longest(total)} in all: ${totals.map((el) => el.textContent).join(", ")}` }, parts),
    // The bar's label reads the totals out, so the summary's name does not repeat them.
    h("span", { class: "time-totals", "aria-hidden": "true" }, totals));
}

function startEntry(start) {
  return h("li", { class: "hist-entry compact" },
    histWhen(start.at),
    h("div", { class: "hist-body" }, h("div", { class: "hist-line" },
      h("span", { class: "hist-who", text: `${capitalize(start.author)} started this` }),
      h("span", { class: "hist-text", text: start.text }))));
}

// The story so far on a step card: Claude's short line per turn of the work, from how it started to where it stands
function storyEntry(story) {
  if (!story) return null;
  let note = null;
  if (story.failed) note = h("span", { class: "hist-note fail-text", text: `${story.lines ? "Not updated" : "Not written"}: ${story.failed}` });
  else if (!story.lines) note = h("span", { class: "hist-note", text: "Writing the history…" });
  else if (!story.current) note = h("span", { class: "hist-note", text: "Updating the history…" });
  return h("li", { class: "hist-story" },
    h("span", { class: "hist-when", "data-time": story.made_at || null, text: "So far" }),
    h("div", { class: "hist-body" },
      story.lines && h("ol", { class: "hist-story-lines" }, story.lines.map((line) => h("li", {},
        h("span", { class: "hist-story-date", "data-time": line.at, text: ago(line.at) }),
        h("span", { class: "hist-story-text", text: line.text })))),
      note));
}

// The history of an item, as compact on the item row as on a step card: the story so far, Earlier folded, the
// previous exchange in quiet lines and the current one. `stepId`: on a step card, the current exchange is the
// user's last action on that step (or on the item as a whole), and the step below is its full answer; on the item
// row (null), it is the item's last exchange, with its answer.
// `quoted`: the user's message the step's body quotes in full, or null.
function historyBlock(item, state, stepId, quoted) {
  const known = historyOf(item.id, historyVersion(item.id, state));
  // The item row keeps its own folds, so an Earlier opened on a step card leaves the row compact.
  const key = stepId === null ? `hist:${item.id}:row` : `hist:${item.id}`;
  if (!known || !known.data) {
    if (known && known.error) return h("p", { class: "hist-error fail-text", text: `History did not load: ${known.error}` });
    return Loading.waiting(`history:${item.id}`, Loading.ghost("small w-30|w-80|w-60", "Loading the history", "hist-ghost"));
  }
  return Loading.arrive(`history:${item.id}`, historyDrawn(item, known.data, stepId, key, quoted));
}

// A step card's history, folded (NEXT-50, NEXT-52): the story's one sentence on the whole task, when this is not
// the task's first exchange; the user's own latest message, its first words and how long ago, a reminder of what
// they last said, which opens to the whole message; and one line that opens the whole history as historyBlock
// draws it. `quoted`: the user's message the step's body quotes in full ("You wrote …:"), or null; `quotes`: the
// texts of the body's quoted blocks. A message the body already quotes stays out of the fold.
function historyFold(item, state, stepId, quoted, quotes) {
  const known = historyOf(item.id, historyVersion(item.id, state));
  const key = `hist:${item.id}`;
  if (!known || !known.data) {
    if (known && known.error) return h("p", { class: "hist-error fail-text", text: `History did not load: ${known.error}` });
    return Loading.waiting(`history:${item.id}`, Loading.ghost("small w-60|w-30", "Loading the history", "hist-ghost"));
  }
  const data = known.data;
  const entries = data.entries;
  if (!entries.length && !data.start) return Loading.arrive(`history:${item.id}`, null);
  const summary = entries.length > 1 && data.story && data.story.summary;
  const said = entries.filter((entry) => entry.you.kind === "message").at(-1);
  const flat = (text) => text.split(/\s+/).join(" ").trim();
  // Left out only when the body quotes the whole message: a quote cut short leaves the reminder, which opens the rest.
  const message = said && flat(said.you.text);
  const shownSaid = said && quoted !== message && !quotes.some((quote) => flat(quote).includes(message)) ? said : null;
  const count = entries.length + (data.start ? 1 : 0);
  const first = data.start ? data.start.at : entries[0].at;
  const whole = historyDrawn(item, data, stepId, key, quoted);
  return Loading.arrive(`history:${item.id}`, h("section", { class: "hist-fold-card", "aria-label": "History", "data-item": item.id },
    summary && h("p", { class: "fold-summary", text: summary }),
    // Keyed by the message's time, so a newer message opens cut again.
    shownSaid && saidLine(`${key}:said:${shownSaid.at}`, shownSaid, mergedFrom(shownSaid, data)),
    whole && disclosure(`${key}:all`, "fold-all",
      h("span", {}, `History: ${count} ${count === 1 ? "exchange" : "exchanges"}, started `,
        h("span", { "data-time": first, "data-ago": first, text: ago(first) })),
      whole)));
}

// Words of the user's latest message shown before "N more words" opens the rest.
const SAID_WORDS = 24;
// Words of each quote in it shown before the rest opens.
const SAID_QUOTE_WORDS = 12;

// A line starting with ">" is a Markdown quote, as Comment puts the words the user selected on a card (NEXT-33, NEXT-63).
const QUOTE_LINE = /^[ \t]*>[ \t]?/;

function hasQuote(text) {
  return text.split("\n").some((line) => QUOTE_LINE.test(line));
}

// The text as its quoted and plain parts in order, [{ quote, text }], a quote's text without its markers.
function quoteRuns(text) {
  const runs = [];
  for (const line of text.split("\n")) {
    const quote = QUOTE_LINE.test(line);
    const last = runs.at(-1);
    const words = quote ? line.replace(QUOTE_LINE, "") : line;
    if (last && last.quote === quote) last.lines.push(words);
    else runs.push({ quote, lines: [words] });
  }
  return runs.map(({ quote, lines }) => ({ quote, text: lines.join("\n").trim() })).filter((run) => run.text);
}

// The user's words with each quote in them drawn as a quote, not as ">" lines. Open, every word shows, a quote as a
// block of its own; shut, a quote shows its first SAID_QUOTE_WORDS words on the line and the rest their first
// SAID_WORDS words. `more`: how many words shut leaves out.
function quotedWords(text, open) {
  let budget = SAID_WORDS;
  let more = 0;
  const nodes = [];
  for (const run of quoteRuns(text)) {
    const words = run.text.split(/\s+/).filter(Boolean);
    const room = open ? words.length : run.quote ? SAID_QUOTE_WORDS : Math.max(budget, 0);
    const shown = words.length > room ? `${words.slice(0, room).join(" ")}…` : run.text;
    more += Math.max(words.length - room, 0);
    if (!run.quote) budget -= words.length;
    if (run.quote) nodes.push(h("span", { class: "said-quote", text: open ? run.text : shown }));
    else if (room > 0) nodes.push(h("span", { class: "said-plain", text: open ? run.text : shown }));
  }
  return { nodes, more };
}

// The task a history entry's step came from when it was merged into this one (SPEC NEXT-55), or null.
function mergedFrom(entry, data) {
  const step = entry.step_id && data.steps[entry.step_id];
  return step && step.merged_from ? step.merged_from.title : null;
}

// The user's latest message on the task, as the fold keeps it in view: who and when, then its first words. `merged`:
// the title of the task the message was on before it merged into this one, or null.
function saidLine(key, entry, merged) {
  const open = ui.expanded.has(key);
  const more = quotedWords(entry.you.text, false).more;
  return h("p", { class: `fold-said${open ? " whole" : ""}` },
    h("span", { class: "fold-said-who" }, "You, ", merged && h("span", { class: "merged-from", text: `on ${merged}, ` }),
      h("span", { "data-time": entry.at, "data-ago": entry.at, text: ago(entry.at) })),
    h("span", { class: "fold-said-text" }, quotedWords(entry.you.text, open || more <= 0).nodes),
    more > 0 && h("button", {
      class: "link-button fold-said-more", type: "button", "aria-expanded": String(open),
      onclick: (event) => { event.stopPropagation(); toggle(key); },
    }, open ? "Show less" : `${more} more ${more === 1 ? "word" : "words"}`));
}

// The short line under the task's name (NEXT-49): what happened, then after the first ";" the next decision, which
// reads darker so the eye lands on what the user does. An ask with no ";" reads whole as the next decision.
function shortLine(step) {
  const line = h("p", { class: "ask short-line" });
  const text = h("span", { class: "ask-text", html: step.ask_html });
  line.append(text);
  const walker = document.createTreeWalker(text, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const at = node.data.indexOf(";");
    if (at < 0) continue;
    const range = document.createRange();
    range.setStart(text, 0);
    range.setEnd(node, at + 1);
    const done = h("span", { class: "short-done" });
    done.append(range.extractContents());
    const next = h("span", { class: "short-next" });
    next.append(...text.childNodes);
    text.append(done, next);
    return line;
  }
  text.classList.add("short-next");
  return line;
}

// historyBlock's history once it has loaded; null when there is nothing to tell.
function historyDrawn(item, data, stepId, key, quoted) {
  const entries = data.entries;
  if (!entries.length && !data.start) return null;
  const rows = [];
  // The user's latest message, usually the ask that matters now (VIEW-15).
  const latest = entries.map((entry) => entry.you.kind).lastIndexOf("message");
  // The current exchange is the user's last action on this step or on the item as a whole, a press folded into
  // an entry included; a step the user has not acted on answers the item's last action, whichever step that was on.
  const actedOn = (entry) => entry.step_id === stepId || entry.folded.some((press) => press.step_id === stepId);
  const onStep = stepId !== null && entries.some(actedOn);
  let current = entries.length - 1;
  if (onStep) entries.forEach((entry, index) => { if (actedOn(entry) || entry.step_id === null) current = index; });
  if (current < 0) return h("section", { class: "history", "aria-label": "History", "data-item": item.id }, h("ol", { class: "hist-list" }, startEntry(data.start)));
  const earlier = current - 1;
  const older = entries.slice(0, Math.max(0, earlier));
  if (earlier >= 0 || data.start) rows.push(storyEntry(data.story));
  if (older.length || data.start) {
    rows.push(h("li", { class: "hist-earlier" }, disclosure(`${key}:earlier`, "hist-fold",
      [`Earlier (${older.length + (data.start ? 1 : 0)})`, earlierTime(older)],
      h("ol", { class: "hist-list" }, data.start && startEntry(data.start),
        older.map((entry, index) => [historyEntry(`${key}:${index}`, entry, data, true, index === latest), timeBar(`${key}:${index}`, entry.after, null)])))));
  }
  if (earlier >= 0) {
    rows.push(historyEntry(`${key}:${earlier}`, entries[earlier], data, true, earlier === latest));
    rows.push(timeBar(`${key}:${earlier}`, entries[earlier].after, null));
  }
  const now = entries[current];
  rows.push(h("li", { class: "hist-entry current" },
    histWhen(now.at),
    // On the step card the ask stands above, so the entry does not tag it with the step again.
    h("div", { class: "hist-body" }, now.step_id === stepId ? null : stepTag(now, data),
      now.step_id === stepId && now.you.kind === "message" && quoted === now.you.text.split(/\s+/).join(" ").trim() ? null : youLine(`${key}:${current}`, now, current === latest),
      now.answer.kind === "step" && now.answer.step_id === stepId && now.answer.is_current ? null : answerLine(`${key}:${current}`, now, data, true),
      now.notes.map((note) => h("div", { class: "hist-note", text: note })))));
  rows.push(timeBar(`${key}:${current}`, now.after, current === entries.length - 1 ? "since" : null));
  return h("section", { class: "history", "aria-label": "History", "data-item": item.id }, h("ol", { class: "hist-list" }, rows));
}

// `line`: the sweep's line on an item under Matters most, which stands in for its summary in the closed row, or null.
function itemRow(item, state, sessions, line) {
  const key = `item:${item.id}`;
  const open = ui.expanded.has(key);
  const linked = itemSessions(item).map((sessionId) => [sessionId, sessions.get(sessionId)]);
  const primary = linked[0];
  const phoneRow = PHONE.matches && !open;
  // A closed row keeps the Cockpit tag (UI-43), whom the item waits on and the user's own mark (what it
  // blocks); Fire and forget and the stage show once it opens. An open review names whom it waits on in its stage
  // chip.
  const tags = [cockpitChip(item.project), stalledChip(item), open && fireAndForgetChip(item), blocksChip(item), item.stage === "review" && open ? [] : waitingChips(item)].flat().filter(Boolean);
  // A closed row's line 2 keeps one line: the first tags, then "+N" with the rest in its tooltip. An
  // open row, and a closed one on a phone, shows them all.
  const rowTags = open || phoneRow ? tags.length : ROW_TAGS;
  const extra = tags.slice(rowTags);
  const shownTags = extra.length
    ? [...tags.slice(0, rowTags), h("span", { class: "chip more-tags", title: extra.map((chip) => chip.title || chip.textContent).join("\n"), text: `+${extra.length}` })]
    : tags;
  // An open row shows the full note in its body; the head keeps the tags. On an item the user added, the
  // owning session's latest status (VIEW-16) stands in for the user's own words, which the open row shows.
  const sub = line || item.agent_summary || item.summary;
  // A finished task says how it ended in place of the note, open or not (VIEW-20).
  // A done item still waiting on someone shows in their group, and still says who closed it and why (VIEW-4).
  const ended = item.ball === "done" || (item.stage === "done" && item.ended) ? endedHow(item, state) : null;
  const note = !ended && !open && sub && sub !== item.title && sub;
  const head = h("div", { class: "row-head", onclick: () => toggle(key) },
    h("div", { class: "row-main" },
      h("div", { class: "row-line1" }, opens(h("span", { class: "row-title", title: workTip(item) || null, text: item.title }), key)),
      (tags.length > 0 || note || ended) && h("div", { class: "row-line2" },
        shownTags,
        ended && endedLine(ended),
        note && h("span", { class: "row-sub", title: !line && item.agent_summary ? `Latest from the session: ${item.agent_summary}` : null, text: note }))),
    h("div", { class: "row-side" },
      timeChip(item),
      item.open_steps > 0 && h("span", { class: "count-chip", title: `${plural(item.open_steps, "step", "steps")} waiting for you`, text: item.open_steps }),
      open && h("span", { class: "stage-slot" }, itemStageChip(item, sessions)),
      h("span", { class: "dots" }, linked.slice(0, 3).map(([sessionId, session]) =>
        stateDot(session ? sessionState(session) : "stopped", session ? `${sessionLabel(session, "")}: ${sessionState(session)}` : "Not running"))),
      h("span", { class: "row-age", "data-time": item.updated_at, title: `Updated by ${actorWords(item.updated_by)}`, text: age(item.updated_at) })));
  // High and urgent items carry the priority bars in the row's slot, and a red edge once their work stalled.
  const raised = raisedPriority(item) ? ` ${item.priority}${item.stalled_since ? " stalled" : ""}` : "";
  if (!open) return h("div", { class: `row${raised}`, "data-item": item.id }, head);
  return h("div", { class: `row open${raised}`, "data-item": item.id, "data-drop-item": item.todo ? item.id : null }, head,
    h("div", { class: "row-body" },
      item.project && h("p", { class: "item-project", title: item.key, text: item.project }),
      item.summary && item.summary !== item.title && h("p", { class: "item-note" }, summaryWords(item, "summary")),
      item.agent_summary && h("p", { class: "item-status" }, h("span", { class: "item-status-label", text: "Latest from the session" }), summaryWords(item, "agent_summary")),
      thumbs(shownImages(item), "small"),
      historyBlock(item, state, null, null),
      linkList(key, itemLinks(item, null), ""),
      sessionList(linked, item, null, state),
      h("div", { class: "item-controls-row" }, itemControls(item, state, `${item.id}`), h("button", {
        class: "button close-item", type: "button",
        onclick: (event) => closeItemRow(item, event.currentTarget),
      }, icon("archive"), h("span", { class: "label", text: "Close" }))),
      askBlock(item.id, null, state),
      composer(`msg:item:${item.id}`, {
        session: primary ? {
          session_id: primary[0],
          name: primary[1] ? primary[1].name : "",
          state: primary[1] ? primary[1].state : "unknown",
          alive: primary[1] ? primary[1].alive : false,
          working_now: primary[1] ? primary[1].working_now : false,
        } : null,
        context: { item_id: item.id },
        project: item.project,
        step: null,
        handOff: false,
        toSession: false,
      })));
}

// Pin (CONTROL-13): a pinned item stays in the Pinned list until the user unpins it. Only the user pins.
function pinKey(item, cls) {
  const pinned = Boolean(item.pinned_at);
  return iconButton("pin", pinned ? "Unpin: take it off the Pinned list" : "Pin", {
    class: `${cls} pin-key${pinned ? " on" : ""}`, "aria-pressed": String(pinned),
    onclick: () => patchItem(item, { pinned: !pinned }, null),
  });
}

// The Pinned list (CONTROL-13), at the top of the side on both views: the items the user pinned, the
// first pinned first, each with its open steps and stage. A click opens the item in All work.
// An archived item leaves it with the rest of All work.
function renderPinned(state) {
  const items = state.items.filter((item) => item.pinned_at).sort((a, b) => a.pinned_at.localeCompare(b.pinned_at));
  if (!items.length) { fill("pinned"); return; }
  const sessions = sessionMap(state);
  fill("pinned", sectionHead("Pinned"), h("ul", { class: "side-list pin-list" }, items.map((item) => h("li", { "data-pinned": item.id },
    h("button", {
      class: "pin-open", type: "button", title: item.title,
      onclick: () => window.openInWork(item.id, item.group),
    },
      h("span", { class: "pin-title", text: item.title }),
      h("span", { class: "pin-line2" },
        cockpitChip(item.project),
        item.open_steps > 0 && h("span", { class: "count-chip", title: `${plural(item.open_steps, "step", "steps")} waiting for you`, text: item.open_steps }),
        itemStageChip(item, sessions),
        (item.agent_summary || item.summary) && (item.agent_summary || item.summary) !== item.title && h("span", { class: "pin-sub", text: item.agent_summary || item.summary }))),
    pinKey(item, "button small ghost reveal")))));
}

// The one list of work and sessions (VIEW-3, VIEW-18), the board the Sessions tab scrolls to. It continues the queue's
// list under no heading of its own; its head holds Sort, Group and New session (VIEW-21). Each session shows once: in
// the row of the item that lists it, or in a row of its own when no item does. Grouped by who moves next, as it was
// before Sessions joined it, each item shows once on the page: an item with a step on Next up, in the queue, Ready next
// or Snoozed shows as that step, so no group here repeats it, also when its session works on and its step is a result
// to read. Matters most comes first: the sweep's pick of the 3–5 that matter most among what waits on someone else
// (VIEW-17), each with the sweep's line. Then the groups by who has the next move. With agents starts with the steps
// the user handed an agent, the asks themselves; each one's item keeps its own row in its group. Each group says in a
// line what it holds. Sessions on no item come next, the first ten until Show all. Finished this week ends the list:
// the tasks done or closed as not needed in the last week, with the last day's closed steps folded at its end
// (VIEW-20). Every other grouping shows all the open work and the sessions on no item, with nothing left out for
// showing above.
function renderWork(state) {
  // On the page with the list of items, the list lives in the sidebar; below the card stay only a search's matches
  // and the new session's composer. The steps closed in the last day stay on the classic page.
  if (SIDEBAR && !ui.find) {
    const newKey = "msg:new-session";
    fill("work", ui.expanded.has(newKey) && h("div", { class: "new-session-box" },
      composer(newKey, { session: null, context: {}, project: null, step: null, handOff: false, toSession: false })));
    return;
  }
  const sessions = sessionMap(state);
  if (ui.find) { renderFoundWork(state, sessions); return; }
  const sort = storedChoice("cockpit:work-sort", WORK_SORTS, "recent");
  const group = storedChoice("cockpit:work-group", WORK_GROUPS, "next");
  if (sort === "messages") { renderMessageWork(state, sessions, group); return; }
  const entries = (items) => items.map((item) => workEntry(item, null, sessions)).sort(workSorter(sort));
  const loose = looseSessions(state).map((session) => workEntry(null, session, sessions)).sort(workSorter(sort));
  const row = (entry) => entry.item ? itemRow(entry.item, state, sessions, null) : sessionRow(entry.session, state);
  const count = (number) => h("span", { class: "group-count", text: number });
  const head = workHead(state, sort, group, WORK_GROUPS);
  if (group !== "next") {
    const all = [...entries(state.items.filter((item) => !item.archived && item.ball !== "done")), ...loose].sort(workSorter(sort));
    const list = !all.length ? h("p", { class: "note", text: "Add work or start a session" })
      : group === "none" ? h("div", { class: "list" }, all.map(row))
        : groupEntries(group, all).map((part) => disclosure(`view:${group}:${part.name}`, "group view", [part.name, count(part.entries.length)],
          h("div", { class: "list" }, part.entries.map(row)), true));
    // The steps handed to an agent keep their list, with Take back, as at the top of With agents.
    fill("work", head, state.with_agents.length > 0 && handedList(state, state.with_agents), list);
    return;
  }
  const cut = !ui.showAllSessions && loose.length > SESSIONS_TOP;
  const looseGroup = loose.length > 0 && disclosure("ball:loose", "group loose", [LOOSE_NAME, count(loose.length)],
    [h("div", { class: "list" }, (cut ? loose.slice(0, SESSIONS_TOP) : loose).map(row),
        loose.length > SESSIONS_TOP && h("div", { class: "more-row" },
          h("button", { class: "link-button", type: "button", onclick: () => { ui.showAllSessions = !ui.showAllSessions; render(false); } },
            cut ? `Show all ${loose.length}` : "Show fewer")))], true);
  const items = new Map(state.items.map((item) => [item.id, item]));
  const asSteps = new Set([...state.queue, ...state.ready_next, ...state.snoozed].map((step) => step.item_id));
  const focus = state.in_progress.filter((entry) => items.has(entry.item_id) && !asSteps.has(entry.item_id));
  const focusIds = new Set(focus.map((entry) => entry.item_id));
  const mattersMost = focus.length > 0 && disclosure("in-progress", "group in-progress",
    [h("span", { class: "group-name" }, "Matters most"),
      h("span", { class: "group-count", text: focus.length })],
    [h("div", { class: "list" }, focus.map(({ item_id: id, line }) => itemRow(items.get(id), state, sessions, line)))], true);
  const groups = GROUPS.filter(([group]) => group !== "you").map(([group, label]) => {
    // A handed step shows in its item's group: in Waiting once its agent stopped more than an hour ago.
    const handedSteps = state.with_agents.filter((step) => (items.get(step.item_id)?.group === "waiting") === (group === "waiting"));
    const handed = group === "done" ? null : handedList(state, handedSteps);
    const shown = state.items.filter((item) => item.group === group && !focusIds.has(item.id) && !asSteps.has(item.id));
    if (group === "done") return [looseGroup, finishedGroup(shown, state, sessions)];
    const total = shown.length + (handed ? handedSteps.length : 0);
    if (!total) return null;
    return disclosure(`group:${group}`, `group ${group}`, [label, count(total)],
      [handed, h("div", { class: "list" }, entries(shown).map(row))],
      !GROUPS_COLLAPSED.has(group));
  });
  fill("work", head, mattersMost, groups);
}

// Search's filter on the page (VIEW-21): search jumps to the matches and filters the list, with closed work as
// an option. The command menu's "Show matches on the page"
// (keyboard.js) sets `ui.find` to {text, closed, target, found, error}, also through #find/<words> and
// #find-closed/<words> from another page. The list of work then shows every task holding all the words or with a
// session that holds them, also one whose step shows above, in its group by who moves next, and every session on
// no item that holds them. With `closed` the server adds the closed work it still keeps (`found`), in a Closed
// group. A bar at the top of the list holds the words, the switch, the counts and Clear. The filter lives in this
// tab until Clear.
function wordsOf(text) {
  const words = text.toLowerCase().split(/\s+/).filter(Boolean);
  return words.length ? words : null;
}

function findWords() {
  return ui.find ? wordsOf(ui.find.text) : null;
}

function holdsWords(words, ...texts) {
  const text = texts.filter(Boolean).join(" ").toLowerCase();
  return words.every((word) => text.includes(word));
}

// The same words the server searches in closed work (store.find_closed); a leading # selects an item ID (UI-74).
function itemMatches(item, words) {
  return words.every((word) => (/^#\d+$/.test(word) ? word === `#${item.id}`
    : holdsWords([word], item.title, item.summary, item.agent_summary, item.key, item.project)));
}

// A session by its own words and the words of the work it is on, as the command menu finds it.
function sessionMatches(session, words, state) {
  const work = state.items.filter((item) => itemSessions(item).includes(session.session_id));
  return holdsWords(words, session.name, session.cwd, session.session_id, session.detail, session.needs, session.provider,
    session.account, ...work.flatMap((item) => [item.title, item.summary, item.agent_summary, item.key, item.project]));
}

// What the filter shows for the words: the open tasks that hold them or list a session that does, the sessions on no
// item that hold them, and how many sessions hold them in all.
function foundWork(state, words) {
  const matching = state.sessions.filter((session) => sessionMatches(session, words, state));
  const ids = new Set(matching.map((session) => session.session_id));
  const loose = new Set(looseSessions(state).map((session) => session.session_id));
  return {
    items: state.items.filter((item) => itemFound(item, words, ids)),
    loose: matching.filter((session) => loose.has(session.session_id)),
    sessions: matching.length,
  };
}

// `text` is the field's value as typed, spaces and all, so typing on keeps them; the words are it trimmed.
function setFind(text, closed, target, jump) {
  const words = text.trim();
  const changed = !ui.find || ui.find.text.trim() !== words || ui.find.closed !== closed;
  ui.find = { text, closed, target, found: changed ? null : ui.find.found, error: changed ? "" : ui.find.error };
  writeStored("cockpit:find-closed", closed);
  history.replaceState(history.state, "", words ? `#find${closed ? "-closed" : ""}/${encodeURIComponent(words)}` : `${location.pathname}${location.search}`);
  if (closed && changed && findWords()) loadClosedWork();
  // The matches show on a phone's item screen.
  if (SIDEBAR && jump) phoneOpen(true);
  render(false);
  if (jump) jumpToFound();
}

// Clearing from the bar takes its field away; focus goes to the header's Search, where searches start, so the
// keys go on working.
function clearFind() {
  const fromBar = document.activeElement?.closest?.(".find-bar");
  if (fromBar) document.activeElement.blur();
  dropFind();
  render(false);
  if (fromBar) { ui.focusLost = false; document.querySelector(".command-trigger")?.focus({ preventScroll: true }); }
}

function dropFind() {
  ui.find = null;
  if (location.hash.startsWith("#find")) history.replaceState(history.state, "", `${location.pathname}${location.search}`);
}

// Opening one task or session the filter hides, from the command menu, a link or a lamp, drops the filter first,
// so the task or session shows. The caller draws the page.
function revealSession(sessionId) {
  const words = findWords();
  const session = words && ui.state?.sessions.find((one) => one.session_id === sessionId);
  if (words && !(session && sessionMatches(session, words, ui.state))) dropFind();
}

window.revealItem = (itemId) => {
  const words = findWords();
  if (!words || !ui.state) return;
  const item = ui.state.items.find((one) => one.id === itemId);
  const ids = new Set(ui.state.sessions.filter((session) => sessionMatches(session, words, ui.state)).map((session) => session.session_id));
  if (!(item && itemFound(item, words, ids))) { dropFind(); render(false); }
};

// An open task the filter shows: it holds the words, or lists a session that does (`ids`).
function itemFound(item, words, ids) {
  return !item.archived && (itemMatches(item, words) || itemSessions(item).some((id) => ids.has(id)));
}

let findTimer = null;
function typeFind(text) {
  clearTimeout(findTimer);
  findTimer = setTimeout(() => { if (ui.find) setFind(text, ui.find.closed, null, false); }, 200);
}

async function loadClosedWork() {
  const text = ui.find.text.trim();
  const current = () => ui.find && ui.find.closed && ui.find.text.trim() === text;
  try {
    const answer = await api("GET", `/api/items/closed?q=${encodeURIComponent(text)}`);
    if (!current()) return;
    ui.find.found = answer.items;
  } catch (error) {
    if (!current()) return;
    ui.find.error = `Closed work not loaded: ${error.message}`;
  }
  render(false);
  if (ui.find.target) jumpToFound();
}

// Brings the matches into view: the task the menu picked (`target`), else the list's top. A load waits for the first
// redraw, which draws them.
function jumpToFound() {
  if (!ui.state) { document.addEventListener("cockpit:redraw", jumpToFound, { once: true }); return; }
  const target = ui.find && ui.find.target !== null && document.querySelector(`#work .row[data-item="${ui.find.target}"]`);
  if (target) {
    ui.find.target = null;
    for (let group = target.closest("details"); group; group = group.parentElement.closest("details")) group.open = true;
    target.scrollIntoView({ block: "center" });
    window.CockpitKeys?.highlight(target, false);
    return;
  }
  document.getElementById("work").scrollIntoView({ block: "start" });
}

// #find/<words>, #find-closed/<words> and either with /<item id>: the filter, from a link or another page.
function followFind() {
  const match = location.hash.match(/^#find(-closed)?\/([^/]*)(?:\/(\d+))?$/);
  if (!match) return;
  let text = match[2];
  try { text = decodeURIComponent(text); } catch { /* a hand-typed % stays as it is */ }
  setFind(text.trim(), Boolean(match[1]), match[3] ? Number(match[3]) : null, true);
}

function findBar(tasks, sessionCount) {
  const field = h("input", {
    type: "search", class: "find-field", "data-key": "find", value: ui.find.text, placeholder: "Words in a task or a session",
    "aria-label": "Filter work and sessions", autocomplete: "off",
    oninput: (event) => typeFind(event.target.value),
    onkeydown: (event) => { if (event.key === "Escape") { event.preventDefault(); clearFind(); } },
  });
  return h("div", { class: "find-bar", role: "search" },
    h("span", { class: "find-label", text: "Filter" }), field,
    h("label", { class: "find-closed" },
      h("input", { type: "checkbox", "data-key": "find:closed", checked: ui.find.closed, onchange: (event) => setFind(ui.find.text, event.target.checked, null, false) }),
      "Include closed"),
    findWords() && h("span", { class: "find-count", text: `${plural(tasks, "task", "tasks")}, ${plural(sessionCount, "session", "sessions")}` }),
    h("button", { class: "button small ghost find-clear", type: "button", onclick: clearFind }, "Clear"));
}

function renderFoundWork(state, sessions) {
  const words = findWords();
  if (!words) { fill("work", findBar(0, 0)); return; }
  const { items: found, loose, sessions: sessionCount } = foundWork(state, words);
  const looseGroup = loose.length > 0 && disclosure("find:loose", "group loose", [LOOSE_NAME, h("span", { class: "group-count", text: loose.length })],
    h("div", { class: "list" }, loose.map((session) => sessionRow(session, state))), true);
  const groups = GROUPS.filter(([group]) => group !== "done").map(([group, label]) => {
    const shown = found.filter((item) => item.group === group);
    return shown.length > 0 && disclosure(`find:${group}`, `group ${group}`, [label, h("span", { class: "group-count", text: shown.length })],
      h("div", { class: "list" }, shown.map((item) => itemRow(item, state, sessions, null))), true);
  });
  const archived = state.closed_recently.items.filter((entry) => entry.action === "archived" && holdsWords(words, entry.title, entry.key));
  const finished = [
    ...found.filter((item) => item.group === "done").map((item) => ({ at: endedAt(item), row: itemRow(item, state, sessions, null) })),
    ...archived.map((entry) => ({ at: endedAt(entry), row: closedItemRow(entry, state) })),
  ].sort((a, b) => b.at.localeCompare(a.at)).map((entry) => entry.row);
  const finishedGroup = finished.length > 0 && disclosure("find:done", "group done", ["Finished this week", h("span", { class: "group-count", text: finished.length })],
    h("div", { class: "list" }, finished), true);
  const onPage = new Set([...found.map((item) => item.id), ...archived.map((entry) => entry.id)]);
  const older = (ui.find.found || []).filter((entry) => !onPage.has(entry.id));
  const closedGroup = ui.find.closed && (ui.find.error ? h("p", { class: "note fail-text", text: ui.find.error })
    : ui.find.found === null ? h("p", { class: "note find-note", text: "Looking through closed work…" })
      : older.length > 0 && disclosure("find:closed", "group closed-found", [
        h("span", { class: "group-name", title: "Done or closed as not needed before this week, newest first; at most 50" }, "Closed earlier"),
        h("span", { class: "group-count", text: older.length })],
      h("div", { class: "list" }, older.map((entry) => closedItemRow(entry, state))), true));
  const tasks = found.length + archived.length + older.length;
  const nothing = !tasks && !loose.length && !(ui.find.closed && ui.find.found === null) && h("p", { class: "note find-note",
    text: ui.find.closed ? "No task or session holds these words, open or closed." : "No open task or session holds these words. Include closed to search the work closed earlier." });
  fill("work", findBar(tasks, sessionCount), groups, looseGroup, finishedGroup, closedGroup, nothing);
}

// Images on items (CONTROL-8): pasted or dropped onto the add box, a to-do row or an open to-do,
// shown as thumbnails; a click opens one full size.
const IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]);
const IMAGE_MAX = 10 * 1024 * 1024;

// The image files in a paste or a drop.
function imageFiles(transfer) {
  return transfer ? [...transfer.files].filter((file) => file.type.startsWith("image/")) : [];
}

// Why the server would refuse an image, in the user's words, or "".
function imageProblem(file) {
  if (!IMAGE_TYPES.has(file.type)) return "Cockpit takes PNG, JPEG, GIF and WebP images";
  if (file.size > IMAGE_MAX) return "an image can be at most 10 MB";
  return "";
}

// The page shows an image from its bytes until the server has it (the page's rules allow data: images).
function readImage(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

// An image on its way up: {id, url, file, savedId}; `url` fills in once the file is read.
function localImage(file) {
  const entry = { id: `img-${++ui.sendCount}`, url: "", file, savedId: null };
  readImage(file).then((url) => { entry.url = url; render(false); renderAddBox(); },
    (error) => toast(`Image not shown: the browser could not read it (${error.message})`, true));
  return entry;
}

// The images an item shows: the saved ones the user has not removed, then those still on their way up.
function shownImages(item) {
  const savedIds = new Set(item.images.map((image) => image.id));
  const saved = item.images.filter((image) => !ui.removedImages.has(image.id))
    .map((image) => ({ id: image.id, url: `/item-images/${image.id}`, saved: true, by: image.added_by, at: image.created_at }));
  const local = (ui.uploading.get(item.id) || []).filter((entry) => !savedIds.has(entry.savedId))
    .map((entry) => ({ id: entry.id, url: entry.url, saved: false, by: "user" }));
  return [...saved, ...local];
}

// The item images a step's text shows as figures (SPEC NEXT-17), which its card leaves out of the strip.
function figureImages(step) {
  const html = [step.body_html, ...step.details.map((detail) => detail.body_html)].join("");
  return new Set([...html.matchAll(/class="figure-image" src="\/item-images\/(\d+)"/g)].map((match) => Number(match[1])));
}

// The item images a step's card shows besides the figures its text places: the user's own images, those on
// their way up and the agents' images added in this step's round (since the item's step before it was put,
// `round_from`) stay in the strip; the agents' images from earlier rounds wait under a closed "Other images"
// (NEXT-17).
function stepImages(key, item, size, step) {
  const figures = figureImages(step);
  const rest = shownImages(item).filter((image) => !(image.saved && figures.has(image.id)));
  const earlier = (image) => image.saved && image.by !== "user" && Boolean(step.round_from) && image.at < step.round_from;
  const strip = rest.filter((image) => !earlier(image));
  const other = rest.filter(earlier);
  return [thumbs(strip, size),
    other.length > 0 ? disclosure(`${key}:other-images`, "detail", `Other images on this task (${other.length})`, thumbs(other, size), false) : null];
}

// `size`: "large" (Next up, about 96 px high) or "small" (rows and the editor, about 48 px).
function thumbs(images, size) {
  if (!images.length) return null;
  return h("div", { class: `thumbs ${size}` }, images.map((image) => h("span", { class: `thumb${image.saved ? "" : " uploading"}` },
    image.url && h("button", { class: "thumb-open", type: "button", title: "Open image", "aria-label": "Open image full size",
      onclick: (event) => { event.stopPropagation(); openLightbox(image.url, event.currentTarget); } }, h("img", { src: image.url, alt: "" })),
    image.saved && h("button", { class: "thumb-remove", type: "button", title: "Remove image", "aria-label": "Remove this image",
      onclick: (event) => { event.stopPropagation(); removeImage(image.id); } }, icon("x")))));
}

// Images for an item that exists: each shows at once and goes up; a refusal takes it off with a red toast.
function attachImages(itemId, files) {
  for (const file of files) {
    const problem = imageProblem(file);
    if (problem) { toast(`Image not added: ${problem}`, true); continue; }
    const entry = localImage(file);
    ui.uploading.set(itemId, [...(ui.uploading.get(itemId) || []), entry]);
    sendImage(itemId, entry);
  }
  render(false);
}

function sendImage(itemId, entry) {
  return run(`image:${entry.id}`, async () => {
    try {
      const saved = await upload(`/api/items/${itemId}/images`, entry.file);
      entry.savedId = saved.id;
    } catch (error) {
      ui.uploading.set(itemId, (ui.uploading.get(itemId) || []).filter((other) => other !== entry));
      throw refused("Image not added", error);
    }
  }, null);
}

// Uploads the state lists as saved stop showing from their local copy.
function settleUploads(state) {
  const saved = new Map(state.items.map((item) => [item.id, new Set(item.images.map((image) => image.id))]));
  for (const [itemId, entries] of ui.uploading) {
    const ids = saved.get(itemId);
    const left = entries.filter((entry) => entry.savedId === null || !ids || !ids.has(entry.savedId));
    if (left.length) ui.uploading.set(itemId, left); else ui.uploading.delete(itemId);
  }
  const listed = new Set(state.items.flatMap((item) => item.images.map((image) => image.id)));
  for (const id of ui.removedImages) if (!listed.has(id)) ui.removedImages.delete(id);
}

// × on a thumbnail: the image goes at once; a refusal brings it back with a red toast.
function removeImage(imageId) {
  ui.removedImages.add(imageId);
  run(`image:remove:${imageId}`, async () => {
    try {
      await api("DELETE", `/api/item-images/${imageId}`);
    } catch (error) {
      ui.removedImages.delete(imageId);
      throw refused("Image not removed", error);
    }
  }, null);
  render(false);
}

// Images sent with a message (SPEC MSG-18): pasted or dropped into any message box, or picked with Attach images under
// Send's arrow, they wait above the box. On Send they go up to the message's task, and the message names them, so its
// session reads them with `cockpit image get`. The server holds a message to the same number as an item.
const MESSAGE_IMAGES_MAX = 10;

// The task a message's images go on: the box's item, the step's, or the item the session works on; null when none.
function messageItem(context, step, where) {
  if (context.item_id != null) return context.item_id;
  if (step) return step.item_id;
  if (!where.session_id) return null;
  const item = drawn(ui.state).items.find((one) => one.sessions.includes(where.session_id));
  return item ? item.id : null;
}

function stagedImages(key) {
  return ui.composerImages.get(key) || [];
}

function stageComposerImages(key, files) {
  const entries = [...stagedImages(key)];
  for (const file of files) {
    const problem = imageProblem(file) || (entries.length >= MESSAGE_IMAGES_MAX ? `a message carries at most ${MESSAGE_IMAGES_MAX} images` : "");
    if (problem) { toast(`Image not added: ${problem}`, true); continue; }
    const entry = { id: `img-${++ui.sendCount}`, url: "", file, savedId: null, savedOn: null };
    readImage(file).then((url) => { entry.url = url; drawStaged(key); },
      (error) => toast(`Image not shown: the browser could not read it (${error.message})`, true));
    entries.push(entry);
  }
  ui.composerImages.set(key, entries);
  drawStaged(key);
}

// The box's staged images, which leave it as Send takes them.
function takeStaged(key) {
  const entries = stagedImages(key);
  ui.composerImages.delete(key);
  drawStaged(key);
  return entries;
}

// A refused Send puts its images back in the box, before any staged since.
function restoreStaged(key, entries) {
  if (!entries.length) return;
  ui.composerImages.set(key, [...entries, ...stagedImages(key)]);
  drawStaged(key);
}

// Uploads the images not yet on this item and returns all their ids, in order.
async function uploadStaged(itemId, entries) {
  for (const entry of entries) {
    if (entry.savedOn === itemId) continue;
    try {
      entry.savedId = (await upload(`/api/items/${itemId}/images`, entry.file)).id;
    } catch (error) {
      throw Object.assign(new Error(`image not added: ${error.message}`, { cause: error }), { status: error.status });
    }
    entry.savedOn = itemId;
  }
  return entries.map((entry) => entry.savedId);
}

function stagedThumbs(key) {
  return stagedImages(key).map((entry) => h("span", { class: "thumb" },
    entry.url && h("button", { class: "thumb-open", type: "button", title: "Open image", "aria-label": "Open image full size",
      onclick: (event) => openLightbox(entry.url, event.currentTarget) }, h("img", { src: entry.url, alt: "" })),
    h("button", { class: "thumb-remove", type: "button", title: "Remove image", "aria-label": "Remove this image",
      onclick: () => {
        ui.composerImages.set(key, stagedImages(key).filter((other) => other !== entry));
        drawStaged(key);
      } }, icon("x"))));
}

// Every box drawn for this key shows its staged images (the same draft can show in two places).
function drawStaged(key) {
  for (const strip of document.querySelectorAll(`[data-staged="${CSS.escape(key)}"]`)) {
    strip.replaceChildren(...stagedThumbs(key));
    strip.hidden = !stagedImages(key).length;
  }
}

// The lightbox shows one image full size. `from` is the thumbnail or figure the user pressed: the lightbox steps
// through every image on the same card, or in the same strip off a card, in the order the card shows them, with
// the arrow buttons, the left and right arrow keys or a swipe (NEXT-64).
function openLightbox(url, from) {
  const scope = from && (from.closest("[data-step], article") || from.closest(".thumbs, [data-staged]"));
  const urls = scope
    ? [...new Set([...scope.querySelectorAll("img.figure-image, .thumb-open img")].map((image) => image.getAttribute("src")).filter(Boolean))]
    : [];
  ui.lightbox = { urls: urls.includes(url) ? urls : [url], at: Math.max(0, urls.indexOf(url)) };
  drawLightbox();
  document.getElementById("lightbox").hidden = false;
}

function drawLightbox() {
  const { urls, at } = ui.lightbox;
  const box = document.getElementById("lightbox");
  const many = urls.length > 1;
  box.replaceChildren(
    h("img", { src: urls[at], alt: "" }),
    iconButton("x", "Close", { class: "button ghost lightbox-close", onclick: closeLightbox }),
    many && iconButton("chevron-left", "Previous image (←)", {
      class: "button ghost lightbox-step lightbox-prev", disabled: at === 0, onclick: () => stepLightbox(-1),
    }),
    many && iconButton("chevron-right", "Next image (→)", {
      class: "button ghost lightbox-step lightbox-next", disabled: at === urls.length - 1, onclick: () => stepLightbox(1),
    }),
    many && h("span", { class: "lightbox-count", text: `${at + 1} of ${urls.length}` }),
  );
}

// `by` is -1 for the previous image and 1 for the next; the first and last images stay put.
function stepLightbox(by) {
  const at = ui.lightbox.at + by;
  if (at < 0 || at >= ui.lightbox.urls.length) return;
  ui.lightbox.at = at;
  drawLightbox();
}

function closeLightbox() {
  const box = document.getElementById("lightbox");
  box.hidden = true;
  box.replaceChildren();
  ui.lightbox = null;
}

// The Add dialog (CONTROL-8): a panel over the dimmed page that makes a tracked item, set apart from feedback,
// which only leaves a note for the next sweep. Enter adds and closes it, Shift+Enter starts a new line; on a touch
// screen, whose keyboard has no Shift+Enter, Enter starts a new line and the Add key adds (CONTROL-8). Escape,
// × or a press beside the panel closes it; the draft stays. Images pasted, dropped or picked from files wait beside the
// text and go up once the item exists. A long text goes whole on the item as a page, and the item's summary
// keeps its start (store.add_item); ADD_MAX is store.ADD_MAX_CHARS.
const ADD_KEY = "add:new";
const ADD_MAX = 1_000_000;

// Whether the user's last press was a click or tap rather than a key: the Add box notes it as it opens, and closing
// it gives the focus back to Add only for a box opened from the keyboard.
let pointerLast = false;
addEventListener("pointerdown", () => { pointerLast = true; }, true);
addEventListener("keydown", () => { pointerLast = false; }, true);

function openAddBox() {
  ui.adding = true;
  ui.addByPointer = pointerLast;
  renderAddBox();
}

// Add more (CONTROL-18): the Add box opens on a to-do the user added, and its words join that item.
function openAddMore(item) {
  if (ui.picking === "add") stopPicking();
  ui.addTo = { id: item.id, title: item.title };
  ui.adding = true;
  ui.addByPointer = pointerLast;
  renderAddBox();
}

// How long a to-do sent with Add counts as just added (`ui.addsSent`, and Busy in the list at the left).
const JUST_ADDED_MS = 30 * 60 * 1000;
// How long a row that came into Busy stays lit at its top.
const ARRIVED_MS = 60 * 1000;
// How long an Add waits before it goes to the server, while its row in Busy is an Undo button.
const UNDO_ADD_MS = 6000;

// The to-do whose Add more words came back unsent (a refusal, then a reload): the Add box offers it, so the words
// still reach their item (CONTROL-18). A to-do just added shows in Busy in the list at the left instead. Null when none.
function unsentAddMore(state) {
  return state.items.find((item) => (ui.drafts.get(addMoreKey(item.id)) || "").trim()) || null;
}

// Opens a to-do the user added in All work, or its step, once the Add box is out of the way. Until a state from
// the server replaces it, the page draws a new to-do under its local id; Open waits for the server's, up to 10 s.
async function openAdded(itemId) {
  if (ui.adding) closeAddBox();
  for (let tries = 0; tries < 50 && !drawn(ui.state).items.some((one) => one.id === itemId); tries++) {
    refresh();
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  if (!drawn(ui.state).items.some((one) => one.id === itemId)) {
    toast("The new to-do is not on the page yet; press Open again in a moment", true);
    return;
  }
  openKeyboardItem(itemId);
}

function closeAddBox() {
  const inside = document.getElementById("add-box").contains(document.activeElement);
  ui.adding = false;
  ui.addTo = null;
  ui.addPartFirst = false;
  // A list left open in Add does not open again with the next Add.
  for (const each of [...ui.open]) if (each.startsWith(`start:${ADD_KEY}:`)) ui.open.delete(each);
  if (ui.picking === "add") stopPicking();
  renderAddBox();
  // Focus goes back to Add for a dialog opened with a key. One opened with a click or tap only lets go of the focus:
  // focus moved from the text box shows the focus ring, and + would stay outlined.
  // The flag stays, so a box that comes back after a refused Add closes the same way.
  if (inside && ui.addByPointer) document.activeElement.blur();
  else if (inside) (SIDEBAR ? document.querySelector(".sidebar-add") : document.getElementById("add"))?.focus();
}

// Feedback: what + does, with the part picked first. The pick bar shows while the user points at the
// part; once picked, the Add box opens with it attached, over any words already waiting there. Stopping the pick
// before a part is picked closes it again (stopPicking). Pressed while picking, it stops the pick.
function openAddWithPart() {
  if (ui.picking) { stopPicking(); return; }
  ui.addTo = null;
  ui.adding = true;
  ui.addByPointer = pointerLast;
  ui.addPartFirst = true;
  startPicking("add");
  renderAddBox();
}

// Feedback instead: the words move to the feedback box, after any feedback draft already there, and the box
// opens once the user picks the part they are about.
function addAsFeedback() {
  const words = (ui.drafts.get(ADD_KEY) || "").trim();
  if (words) {
    const before = (ui.drafts.get(PICK_KEY) || "").trim();
    ui.drafts.set(PICK_KEY, before ? `${before}\n\n${words}` : words);
    ui.drafts.delete(ADD_KEY);
    saveDrafts();
  }
  closeAddBox();
  startPicking("feedback");
}

// A part of the page for the to-do (CONTROL-9): the Add dialog shows where it is and what it says, with
// Wider and Narrower as in feedback mode; Add sends its place and text with the user's words.
function attachPart(el, from, side) {
  ui.addPart = { ...pickTarget(el), from, side };
  ui.addPartFirst = false;
  stopPicking();
  renderAddBox();
}

// `next` is {part, from, side} or null, as for stepPick.
function stepAddPart(next) {
  if (next && Pick.alive(next.part)) { Pick.reveal(next.part); attachPart(next.part, next.from, next.side); return; }
  toast("The page redrew since you picked; pick the part again", true);
  startPicking("add");
  renderAddBox();
}

function addPartBox(part) {
  return h("div", { class: "add-part" },
    h("div", { class: "pick-head" },
      icon("pick"),
      h("strong", { text: "Attached" }),
      h("span", { class: "meta", text: part.target.replace(/^page:/, ""), title: pickTitle(part) }),
      widerNarrower(part, true),
      iconButton("x", "Remove attachment", { class: "button small ghost", onclick: () => { ui.addPart = null; renderAddBox(); } })),
    pickPreview(part, Math.min(368, window.innerWidth - 88)));
}

function stageImages(files) {
  for (const file of files) {
    const problem = imageProblem(file);
    if (problem) { toast(`Image not added: ${problem}`, true); continue; }
    ui.addImages.push(localImage(file));
  }
  renderAddBox();
}

// While the user picks a part to attach, the dialog steps aside so the page can be clicked, and comes back
// with the part once it is picked or the pick stops.
function renderAddBox() {
  const box = document.getElementById("add-box");
  if (!ui.adding || ui.picking === "add") {
    const old = box.querySelector(".add-panel");
    if (old) Pick.dock(old, null, 0);
    Pick.mark("attached", null, null);
    box.hidden = true;
    box.classList.remove("beside");
    box.replaceChildren();
    return;
  }
  const opening = box.hidden;
  // A redraw replaces the control that has focus; the one with the same name takes it back.
  const focused = box.contains(document.activeElement) ? document.activeElement.getAttribute("aria-label") : null;
  const target = ui.addTo;
  const unsent = !target && ui.state ? unsentAddMore(drawn(ui.state)) : null;
  const submit = target ? addMore : () => addItem(false);
  const area = target
    ? draftArea(addMoreKey(target.id), "The rest of what you meant to write", 3, "")
    : draftArea(ADD_KEY, "What needs doing? The first line is the title", 3, "");
  area.setAttribute("aria-label", target ? "More words" : "New item");
  // A phone's keyboard then shows its return key, which starts a new line.
  if (TOUCH.matches) area.setAttribute("enterkeyhint", "enter");
  area.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing && !TOUCH.matches) { event.preventDefault(); submit(); }
    else if (event.key === "Escape") {
      event.preventDefault();
      if (lightboxOpen()) closeLightbox();
      else if (!closeStartPicks(document.getElementById("add-box"))) closeAddBox();
    }
  });
  area.addEventListener("paste", (event) => {
    const files = imageFiles(event.clipboardData);
    if (files.length) { event.preventDefault(); stageImages(files); }
  });
  const staged = ui.addImages.length > 0 && h("div", { class: "thumbs small" }, ui.addImages.map((entry) => h("span", { class: "thumb" },
    entry.url && h("button", { class: "thumb-open", type: "button", title: "Open image", "aria-label": "Open image full size", onclick: (event) => openLightbox(entry.url, event.currentTarget) }, h("img", { src: entry.url, alt: "" })),
    h("button", { class: "thumb-remove", type: "button", title: "Remove image", "aria-label": "Remove this image",
      onclick: () => { ui.addImages = ui.addImages.filter((other) => other !== entry); renderAddBox(); } }, icon("x")))));
  // Nothing above the text box but what changes: Add more names its to-do; a new item shows a line only
  // for Add more words that came back unsent, not the to-do added last. Escape or a press beside the
  // panel closes it; Leave feedback instead is in Add's arrow menu.
  box.setAttribute("aria-label", target ? `Add more to ${target.title}` : "Add to your work");
  const head = target
    ? [h("div", { class: "add-head" },
        h("h2", { text: target.title }),
        h("button", { class: "link-button", type: "button", onclick: () => { ui.addTo = null; renderAddBox(); } },
          "New item instead"))]
    : [unsent && h("div", { class: "add-recent" },
        h("span", { class: "add-recent-words" }, h("span", { class: "add-recent-when", text: "Your unsent words for" }),
          h("span", { class: "add-recent-title", title: unsent.summary, text: unsent.title })),
        h("button", { class: "button small ghost", type: "button", onclick: () => openAdded(unsent.id) }, "Open"),
        h("button", { class: "button small ghost", type: "button", onclick: () => openAddMore(unsent) }, "Add more"))];
  // The image key opens the browser's file picker; its images wait like pasted ones.
  const picker = h("input", { type: "file", accept: [...IMAGE_TYPES].join(","), multiple: true, hidden: true, tabindex: "-1",
    onchange: () => stageImages(imageFiles(picker)) });
  // The panel stays the same element across redraws, so a docked panel keeps its place while the user steps.
  let panel = box.querySelector(".add-panel");
  if (!panel) {
    panel = h("div", { class: "add-panel" });
    // Tab stays within the panel while it is open, and goes to the full-size image while one is open over it.
    panel.addEventListener("keydown", (event) => {
      if (event.key !== "Tab" || lightboxOpen()) return;
      const keys = [...panel.querySelectorAll("button, summary, textarea, select, [tabindex]:not([tabindex='-1'])")].filter((el) => !el.disabled && el.offsetParent);
      const edge = event.shiftKey ? keys[0] : keys[keys.length - 1];
      if (document.activeElement === edge) { event.preventDefault(); (event.shiftKey ? keys[keys.length - 1] : keys[0]).focus(); }
    });
  }
  panel.classList.toggle("more", Boolean(target));
  panel.replaceChildren(
    ...head.filter(Boolean),
    h("div", { class: "add-inner" },
      !target && ui.addPart && addPartBox(ui.addPart),
      area,
      staged),
    h("div", { class: "add-foot" },
      iconButton("image", "Add images from your files; you can also paste or drop them into the box", {
        class: "button ghost", onclick: () => picker.click(),
      }),
      picker,
      !target && iconButton("pick", "Attach part", {
        class: "button ghost", onclick: () => { startPicking("add"); renderAddBox(); },
      }),
      !target && iconButton("pin", ui.addPinned ? "Unpin" : "Pin it: it starts in the Pinned list", {
        class: `button ghost pin-key${ui.addPinned ? " on" : ""}`, "aria-pressed": String(ui.addPinned),
        onclick: () => { ui.addPinned = !ui.addPinned; renderAddBox(); },
      }),
      !target && h("div", { class: "add-starts" }, addPriorityPick(), startOptions(ADD_KEY, null)),
      target
        ? h("button", { class: "button primary", type: "button", "data-key": "Enter", onclick: submit },
          icon("plus"), h("span", { class: "label", text: "Add to it" }))
        : addSplit(submit)));
  if (panel.parentElement !== box) box.replaceChildren(panel);
  // With a part attached, the page stays as bright as it was, the part is outlined, and the panel stands beside
  // it (pick.js dock), as the feedback box does, so the user sees what is attached while stepping to another part.
  const part = target ? null : ui.addPart;
  box.classList.toggle("beside", Boolean(part));
  box.hidden = false;
  Pick.mark("attached", part && part.el, part ? part.target.replace(/^page:/, "") : null);
  Pick.dock(panel, part && part.el, pickTop());
  const again = focused && [...panel.querySelectorAll("[aria-label]")].find((el) => el.getAttribute("aria-label") === focused);
  if (again) again.focus();
  else if (focused !== null || opening) area.focus();
}

// The new to-do's priority (CONTROL-10): a button like the start line's, which opens the four levels. With none
// picked it shows Normal, and words such as "urgent" in the text still set the priority; a level picked wins over them.
function addPriorityPick() {
  const menuKey = `start:${ADD_KEY}:priority`;
  const level = ui.addPriority || "normal";
  const el = h("details", { class: "menu start-pick add-priority", "data-menu": menuKey });
  el.open = ui.open.has(menuKey);
  rememberOpen(el, (isOpen) => { if (isOpen) ui.open.add(menuKey); else ui.open.delete(menuKey); });
  // Escape closes the open list alone, back on its button; the Add dialog stays open.
  el.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !el.open) return;
    event.preventDefault();
    event.stopPropagation();
    ui.open.delete(menuKey);
    el.open = false;
    el.querySelector(".start-button").focus();
  });
  const words = `${capitalize(level)} priority`;
  el.append(
    h("summary", { class: "start-button", "data-key": `${ADD_KEY}:priority`, "data-priority": level, title: `Priority: ${words}`, "aria-label": `Priority: ${words}` },
      icon(PRIORITY_ICONS[level]), h("span", { class: "start-words", text: capitalize(level) }), icon("chevron")),
    h("div", { class: "menu-list" }, ["urgent", "high", "normal", "low"].map((each) => h("button", {
      type: "button", class: each === level ? "picked" : "", "data-priority": each, "aria-current": each === level ? "true" : null,
      onclick: () => {
        ui.open.delete(menuKey);
        ui.addPriority = each;
        renderAddBox();
        document.querySelector(`#add-box [data-key="${CSS.escape(`${ADD_KEY}:priority`)}"]`)?.focus();
      },
    }, h("span", { class: "menu-label" }, icon(PRIORITY_ICONS[each]), capitalize(each)),
    h("span", { class: "when" }, each === "normal" ? "default" : "", each === level && icon("check"))))));
  return el;
}

// Add with its arrow attached, as Send has one (CONTROL-24): the arrow opens Fire and forget, which adds the item for
// a session that works to the end on its own and comes back only when it needs the user.
function addSplit(submit) {
  const menuKey = "menu:add-ways";
  const ways = h("details", { class: "menu send-ways add-ways" });
  ways.open = ui.open.has(menuKey);
  rememberOpen(ways, (open) => { if (open) ui.open.add(menuKey); else ui.open.delete(menuKey); });
  // Escape closes the open menu alone, back on its arrow; the Add dialog stays open.
  ways.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !ways.open) return;
    event.preventDefault();
    event.stopPropagation();
    ui.open.delete(menuKey);
    ways.open = false;
    ways.querySelector("summary").focus();
  });
  ways.append(
    h("summary", { class: "button primary send-more", "aria-label": "Other ways to add", title: "Other ways to add" }, icon("chevron")),
    h("div", { class: "menu-list" }, h("button", {
      class: "forget-way", type: "button",
      title: "It closes the item itself when done",
      onclick: () => { ui.open.delete(menuKey); ways.open = false; addItem(true); },
    }, h("span", { class: "menu-label" }, icon("send"), "Fire and forget"), h("span", { class: "when", text: "back only if it needs you" })),
    h("button", {
      class: "feedback-way", type: "button",
      onclick: () => { ui.open.delete(menuKey); ways.open = false; addAsFeedback(); },
    }, h("span", { class: "menu-label" }, icon("feedback"), "Leave feedback instead"), h("span", { class: "when", text: "a note for the sweep" }))));
  return h("div", { class: "send-split add-split" },
    h("button", { class: "button primary send", type: "button", "data-key": "Enter", onclick: submit },
      icon("plus"), h("span", { class: "label", text: "Add" })),
    ways);
}

function lightboxOpen() {
  return !document.getElementById("lightbox").hidden;
}

// Focus that leaves the open Add dialog for the page behind it comes back to its text box; the image viewer,
// feedback mode and toasts over it keep theirs.
document.addEventListener("focusin", (event) => {
  if (!ui.adding || ui.picking || ui.pick || lightboxOpen()) return;
  if (event.target.closest && event.target.closest("#add-box, #lightbox, #pick-bar, #pick-box, #toast, #time-dialog")) return;
  const area = document.querySelector(`#add-box [data-key="${ADD_KEY}"]`);
  if (area) area.focus();
});

// The server's item for an Add in `state`, or null: the item it answered with or, while that answer is on the way, a
// to-do the page did not list before the Add, with the same title, which a poll can bring first. Adds of the same text
// on their way each take the oldest such to-do left, in the order they were sent, so each draws as one row.
function addedItemIn(state, sent) {
  if (typeof sent.added.id === "number") return state.items.find((one) => one.id === sent.added.id) || null;
  const taken = new Set(ui.addsSent.map((one) => one.added.id));
  const fresh = state.items
    .filter((one) => /^todo-\d+$/.test(one.key) && one.title.slice(0, 200) === sent.title)
    .sort((a, b) => a.id - b.id);
  for (const add of ui.addsSent) {
    if (typeof add.added.id === "number" || add.title !== sent.title) continue;
    const item = fresh.find((one) => !add.known.has(one.id) && !taken.has(one.id));
    if (add === sent) return item || null;
    if (item) taken.add(item.id);
  }
  return null;
}

// The new to-do shows at once under Busy: on the page with the list at the left, the Add box flies into its row,
// which is an Undo button for UNDO_ADD_MS, its bar running down; on the classic page, and on a phone's
// item screen, which hides the list, the notice offers Undo. Only then does the Add go to the server, so Undo leaves nothing behind: the words, images and part go back
// into the box, as they do when the server refuses the Add. The server's item replaces the local one. Once the images
// are up, a new session starts on it with the user's words (CONTROL-8), so its first message lists them; it starts
// where a new session for an item without a project does. If the start is refused, a red toast says so, and the
// sweeper starts it.
function addItem(fireAndForget) {
  const sent = ui.drafts.get(ADD_KEY) || "";
  const text = sent.trim();
  if (!text) return;
  if (text.length > ADD_MAX) { toast(`Not added: a to-do can be at most ${ADD_MAX.toLocaleString("en")} characters`, true); return; }
  const staged = ui.addImages;
  const part = ui.addPart;
  const pinned = ui.addPinned;
  const picked = ui.addPriority;
  const start = startPicks(ADD_KEY, null);
  const now = new Date().toISOString();
  const local = {
    id: `local-${++ui.sendCount}`, key: "todo-…", todo: 1, title: clip(text.split("\n")[0].trim(), 300), summary: clip(text, 2000),
    project: "", stage: null, waiting_on: [], priority: picked || "normal", goal_id: null, blocks: "", sessions: [], step_sessions: [], links: [],
    pinged_at: null, touched_at: null, created_at: now, updated_at: now, updated_by: "user", archived: 0,
    user_fields: picked ? ["summary", "priority"] : ["summary"], agent_summary: "",
    pinned_at: pinned ? now : null,
    fire_and_forget: fireAndForget ? 1 : 0, open_steps: 0, ball: "agents", group: "busy", tip: "", tip_at: null, doing: "", last_agent_message_at: null, untriaged: true, images: [],
  };
  ui.drafts.delete(ADD_KEY);
  ui.starts.delete(ADD_KEY);
  saveDrafts();
  ui.addImages = [];
  ui.addPart = null;
  ui.addPinned = false;
  ui.addPriority = null;
  if (staged.length) ui.uploading.set(local.id, staged);
  // Where the Add box was, for its flight into the new row.
  const box = document.querySelector("#add-box .add-panel, #add-box textarea");
  const from = box && box.getBoundingClientRect();
  closeAddBox();
  // `made` settles with the server's id once it made the item, or with null after Undo or a refusal; `item` is the
  // server's item from its answer.
  let madeAs;
  const added = { id: local.id, title: local.title, localId: local.id, item: null, made: new Promise((resolve) => { madeAs = resolve; }) };
  const outgoing = { added, title: local.title.slice(0, 200), known: new Set(ui.state ? drawn(ui.state).items.map((one) => one.id) : []), at: Date.now() };
  // While an Add is on its way, all are kept, however old: a poll may bring its item before the answer, and the answered
  // ids of the others keep it from taking theirs (addedItemIn).
  const onItsWay = ui.addsSent.some((one) => typeof one.added.id !== "number");
  ui.addsSent = [...ui.addsSent.filter((one) => onItsWay || Date.now() - one.at < JUST_ADDED_MS), outgoing];
  // A state that already holds the new item, also one that came before the server's answer, draws it in place of the
  // local one. Once the server answered, its item shows until a state holds it, so the row opens at once rather than
  // after the next poll, which a busy server can take seconds to bring.
  const withLocal = (state) => (addedItemIn(state, outgoing) ? state : { ...state, items: [...state.items, added.item || local] });
  // Undo, or a refusal: the words, images and part go back into the Add box, which opens.
  const putBack = () => {
    madeAs(null);
    ui.uploading.delete(local.id);
    ui.addsSent = ui.addsSent.filter((one) => one !== outgoing);
    if (ui.arrived && ui.arrived.sent === outgoing) ui.arrived = null;
    // The picks first, so the draft's save keeps them.
    if (!ui.starts.has(ADD_KEY)) ui.starts.set(ADD_KEY, start);
    restoreDraft(ADD_KEY, sent);
    ui.addImages = [...staged, ...ui.addImages];
    if (part && !ui.addPart) ui.addPart = part;
    ui.addPinned = ui.addPinned || pinned;
    ui.addPriority = ui.addPriority || picked;
    // The words go back to a new to-do, also when the box is open on another to-do's Add more meanwhile.
    ui.addTo = null;
    ui.adding = true;
    renderAddBox();
  };
  const send = () => {
    const adding = run(`add:${local.id}`, async () => {
      let item;
      try {
        item = await optimistic(withLocal,
          () => apiWritten("POST", "/api/items/add", { text, part: part && { target: part.target, quote: part.quote, detail: part.detail }, pinned, fire_and_forget: fireAndForget, priority: picked, ...start },
            { key: ADD_KEY, text: sent, picks: start }),
          putBack);
      } catch (error) {
        throw refused("Not added", error);
      }
      added.id = item.id;
      added.item = item;
      madeAs(item.id);
      ui.addsRunning.set(item.id, adding);
      safeRender();
      // The images go up now that the item exists.
      ui.uploading.delete(local.id);
      if (staged.length) ui.uploading.set(item.id, staged);
      await Promise.all(staged.map((entry) => sendImage(item.id, entry)));
      try {
        await api("POST", "/api/messages", {
          text: item.summary,
          target: { item_id: item.id, step_id: null, session_id: null, new_cwd: defaultCheckout(item.project || null, ui.state) },
        });
      } catch (error) {
        throw refused("Added, but its session did not start; the sweeper starts it", error);
      }
    }, null);
    // Images go up only once the item exists and the browser keeps none of them, so leaving asks first until then.
    if (staged.length) holdLeave(adding);
    return adding;
  };
  // While the Undo is on offer the local row is a pending change of its own; sending swaps it for the Add's.
  const holdKey = ++ui.pendingCount;
  ui.pending.set(holdKey, { apply: withLocal, confirmed: null });
  let settle;
  // Settles with {adding}, the Add's run, once it is sent, or with null after Undo.
  const settled = new Promise((resolve) => { settle = resolve; });
  const release = () => {
    if (ui.addsHeld.get(local.id) !== hold) return false;
    clearTimeout(hold.timer);
    ui.addsHeld.delete(local.id);
    ui.pending.delete(holdKey);
    return true;
  };
  // `row`: the Undo is the to-do's row in Busy, as the list at the left shows (on a phone's item screen, which hides
  // the list, it sits on the bar's Your items); else it is the notice's button.
  const hold = {
    at: Date.now(), timer: null, row: undoHeldInList(), notice: null,
    send: () => {
      if (!release()) return;
      settle({ adding: send() });
      // Sent while its notice still offers Undo (the page went to the background): the notice offers Open instead.
      if (hold.notice !== null && (hold.notice === ui.toastShown || hold.notice === ui.toastWaiting?.id)) noticeSent();
    },
    undo: () => { if (!release()) return; settle(null); putBack(); render(false); },
  };
  ui.addsHeld.set(local.id, hold);
  // Open and Add more wait for the whole Add: the row then has its id, and Add more's words reach the session's
  // first message rather than a text that message no longer reads.
  added.known = settled.then((sent) => sent && sent.adding.then(() => { ui.addsRunning.delete(added.id); return typeof added.id === "number"; }));
  // Leaving the page while the Undo is on offer asks first, as the Add has not gone out yet.
  holdLeave(settled.then((sent) => sent && sent.adding));
  if (hold.row) {
    ui.arrived = { sent: outgoing, id: local.id, scrolled: false };
    hold.timer = setTimeout(hold.send, UNDO_ADD_MS);
    render(false);
    flyToRow(from, local.id, local.title);
    return;
  }
  // The classic page draws no list: the notice offers Undo, and the Add goes out once the notice leaves without it.
  const words = fireAndForget ? `Fire and forget: ${clip(local.title, 60)}. Back only if it needs you.` : `Added: ${clip(local.title, 60)}. A session starts on it.`;
  // The notice offers Add more too, so one sent too soon is finished (CONTROL-18).
  const noticeSent = () => offerAddMore(toast(words, false, { label: "Open", run: () => whenAdded(added, openAdded) }), added);
  hold.notice = toast(words, false, { label: "Undo", run: hold.undo, gone: hold.send });
  offerAddMore(hold.notice, added);
  render(false);
}

// A page hidden while an Add waits for its Undo time sends it now: a phone may stop the page's timers in the
// background, and the Add would wait until the page comes back.
document.addEventListener("visibilitychange", () => {
  if (!document.hidden || !ui.addsHeld.size) return;
  for (const hold of [...ui.addsHeld.values()]) hold.send();
  render(false);
});

// Runs `then` with the new item's id once the server made it; a refused Add has none, and its words are back in
// the Add box.
function whenAdded(added, then) {
  added.known.then((known) => { if (known) then(added.id); });
}

// A row whose Add is on its way to the server has no item to open yet: a click on it opens the item as soon as the
// server made it, unless the user went to an item or a session meanwhile, or clicked another such row.
function openWhenMade(localId) {
  const sent = ui.addsSent.find((one) => one.added.localId === localId);
  if (!sent) return;
  const at = ++ui.navigations;
  sent.added.made.then((id) => { if (id !== null && ui.navigations === at) selectItem(id); });
}

function offerAddMore(shown, added) {
  // A notice that waits behind an open follow-up box gets its button once it shows.
  const waiting = ui.toastWaiting;
  if (waiting && waiting.id === shown) {
    waiting.setups.push(() => offerAddMore(shown, added));
    return;
  }
  const el = document.getElementById("toast");
  if (shown !== ui.toastShown || el.hidden) return;
  el.insertBefore(h("button", { type: "button", class: "toast-more",
    onclick: () => { closeToast(); whenAdded(added, (id) => openAddMore({ id, title: added.title })); } }, "Add more"), el.querySelector(".toast-time"));
}

function addMoreKey(itemId) {
  return `add:more:${itemId}`;
}

// The words join the end of the to-do's text at once; the server sends them to its session, or to its first
// message while that session starts. If the server refuses, the words go back into the box.
function addMore() {
  const target = ui.addTo;
  const itemId = target.id;
  const key = addMoreKey(itemId);
  const sent = ui.drafts.get(key) || "";
  if (!sent.trim()) return;
  const staged = ui.addImages;
  ui.drafts.delete(key);
  saveDrafts();
  ui.addImages = [];
  closeAddBox();
  sendMore(target, key, sent, null, staged, () => {
    restoreDraft(key, sent);
    ui.addImages = [...staged, ...ui.addImages];
    ui.addTo = target;
    ui.adding = true;
    renderAddBox();
  });
}

// `target`: the to-do, {id, title}; `key`: the box the words came from; `sent`: the words as written; `choice`: that
// box's To when sent, or null for the Add box, which has none; `putWordsBack`: puts
// them back in that box when the server refuses, and returns false when it could not.
function sendMore(target, key, sent, choice, staged, putWordsBack) {
  const itemId = target.id;
  const text = sent.trim();
  const reopens = reopening(itemId);
  const reopen = reopenBy(reopens, itemId);
  if (!reopens) toast(`Added to ${clip(target.title, 60)}. Its session gets your words.`, false, { label: "Open", run: () => openAdded(itemId) });
  if (staged.length) ui.uploading.set(itemId, staged);
  let back = true;
  const adding = run(`add-more:${itemId}:${++ui.sendCount}`, async () => {
    // The images go first, so the session finds them on the item when the words reach it, and the words carry their
    // ids (SPEC MSG-18). An image the item refuses stops the words too, and both go back where they were written;
    // those already up are not sent again.
    await ui.addsRunning.get(itemId);
    let images;
    try {
      images = await uploadStaged(itemId, staged);
    } catch (error) {
      ui.uploading.set(itemId, (ui.uploading.get(itemId) || []).filter((entry) => !staged.includes(entry) || entry.savedOn === itemId));
      back = putWordsBack() !== false;
      throw keptInNotice(refused("Not added", error), back, text);
    }
    try {
      await optimistic((state) => reopen({ ...state, items: state.items.map((one) => one.id === itemId ? { ...one, summary: `${one.summary}\n\n${text}`, summary_html: undefined } : one) }),
        () => afterFinishes(() => apiWritten("POST", `/api/items/${itemId}/add-more`, { text, images }, { key, text: sent, choice })),
        () => { back = putWordsBack() !== false; takeBackReopened(reopens); });
      reopenConfirmed(reopens);
    } catch (error) {
      throw keptInNotice(refused(staged.length ? "Images added, words not" : "Not added", error), back, text);
    }
  }, null);
  if (staged.length) holdLeave(adding);
  return adding;
}

// An orange dot on a key whose editor holds words that are not saved (Priorities, Inbox settings). Its hover says so.
const EDIT_DOT_WORDS = "Your edit is not saved yet; press to finish it";
const editDot = () => h("span", { class: "edit-dot", "aria-hidden": "true" });

// Priorities and Inbox settings (SPEC UI-65, UI-70): one view in the card's place, as a session's conversation is,
// opened by the Priorities row at the top of the rail and by the key on the Inbox heading. It holds the priorities,
// how the inbox is ranked and what to check for new work, and one Save sends what changed. Back, or Escape, goes back
// to the card and keeps the edit; each part's edit is kept in this browser on its own, so each opener carries the
// orange dot for its own part. `from` names the part the view opened on, whose opener takes the focus back.
const settingsView = { open: false, opening: false, from: null };

// The user's priorities (SPEC CONTROL-12, UI-65): one row at the top of the rail, above Search, reading "Priorities"
// and then a short summary Claude wrote of the priorities that hold, or those priorities themselves when they fit the
// row, "[all paused]" when every one is paused, or "[empty]" while there is none. Until the summary is written the row
// shows the list, cut off with an ellipsis. Its hover holds the whole list: the priorities that hold numbered, then the
// paused ones. A click opens Priorities and inbox (openSettings) in the card's place, with the priorities first, a
// numbered list to drag into order, with a pause on each priority; Save writes the whole list at once and wakes the
// sweeper. The row is marked current while that view shows. While the view holds an edit of the priorities that is
// not saved, an orange dot follows the word Priorities, so an edit left behind by Back or a reload is not forgotten.
function renderGoals(state) {
  const lines = state.goals.filter((goal) => !goal.paused_at).map((goal) => goal.text);
  const paused = state.goals.filter((goal) => goal.paused_at).map((goal) => goal.text);
  const empty = !state.goals.length;
  const shown = empty ? "[empty]" : lines.length ? state.goals_summary ?? lines.join("; ") : "[all paused]";
  const unsaved = goalsDraft(state) !== null || readStored("cockpit.unsaved:/api/goals", null) !== null;
  const listed = [...lines.map((line, index) => `${index + 1}. ${clip(line, 60)}`), ...paused.map((line) => `Paused: ${clip(line, 60)}`)];
  fill("goals",
    h("button", {
      class: `goals-box${lines.length ? "" : " empty"}${unsaved ? " unsaved" : ""}`, type: "button",
      "aria-label": `Priorities: ${empty ? "empty" : [...lines, ...paused.map((line) => `paused: ${line}`)].join("; ")}.${unsaved ? " Your edit is not saved." : ""} Edit them`,
      title: [unsaved && EDIT_DOT_WORDS, empty ? "What matters in your work; press to edit" : listed.join("\n")].filter(Boolean).join("\n\n"),
      "aria-current": settingsView.open ? "true" : null, onclick: () => openSettings("goals"),
    },
    h("span", { class: "goals-line" },
      h("span", { class: "goals-label", text: "Priorities" }),
      unsaved && editDot(),
      " ",
      h("span", { class: "goals-text", text: shown }))));
}

// A priority as the editor and the whole-list save hold it: the stored goal it is (its id, or null for a new one), its
// words, and whether it is paused. The id lets a priority keep its items when it is moved and reworded at once, and
// tells apart two priorities with the same words. A goal the page shows before the server has answered has a local
// id, which names no stored goal yet: it is sent as null, and the server finds it by its words.
const goalEntries = (goals) => goals.map((goal) => ({ id: goal.id, text: goal.text, paused: Boolean(goal.paused_at) }));
const keptEntries = (entries) => entries
  .map((entry) => ({ id: Number.isInteger(entry.id) ? entry.id : null, text: entry.text.trim(), paused: entry.paused }))
  .filter((entry) => entry.text);
// Two lists a save would leave as they are: the same words, paused or not, in the same order, and the same goal in
// each place. An entry without an id stands for the goal with its words, as the server reads it, so it matches any id;
// two goals with the same words swapped are a change.
const sameEntries = (a, b) => {
  const [left, right] = [keptEntries(a), keptEntries(b)];
  return left.length === right.length && left.every((entry, index) => entry.text === right[index].text
    && entry.paused === right[index].paused && (entry.id === null || right[index].id === null || entry.id === right[index].id));
};
// Words kept in this browser before priorities could pause were plain lines: a draft's `text`, a refused save's `texts`.
const storedEntries = (stored) => stored.goals?.map((entry) => ({ id: entry.id ?? null, text: entry.text, paused: entry.paused }))
  ?? (stored.texts ?? stored.text.split("\n")).map((text) => ({ id: null, text, paused: false }));

// The editor's priorities while they differ from the saved list, kept in this browser so Escape, a click away or a
// reload loses nothing: {goals, at}, or null when there is no such edit.
const GOALS_DRAFT_KEY = "cockpit.draft:goals";
function goalsDraft(state) {
  const draft = readStored(GOALS_DRAFT_KEY, null);
  return draft && !sameEntries(storedEntries(draft), goalEntries(state.goals)) ? { goals: storedEntries(draft), at: draft.at } : null;
}

// The priorities in order. Each row has a grip to drag it, or to move it with the arrow keys while the grip has
// focus; its place among the priorities that hold, or the pause mark while it is paused; its words in a box that
// grows with them; a key to pause or resume it; and a key to remove it. Enter in a priority starts a new one below
// it. An empty list starts with one empty row to write in. The rows, not each box, are the edit, read again into
// `entries` after every change, and each change is passed to changed().
function goalRows(entries, changed) {
  const list = h("ol", { class: "goal-entries", "aria-labelledby": "settings-goals-title" });
  const sync = () => {
    entries.splice(0, entries.length, ...[...list.children].map((row) => ({
      id: row.goalId, text: row.querySelector(".goal-entry-text").value, paused: row.classList.contains("paused"),
    })));
    let place = 0;
    [...list.children].forEach((row) => {
      const paused = row.classList.contains("paused");
      const name = paused ? "this paused priority" : `priority ${++place}`;
      const rank = row.querySelector(".goal-entry-rank");
      rank.replaceChildren(paused ? icon("pause") : `${place}.`);
      rank.title = paused ? "Paused: the sweeper leaves it aside" : "";
      row.querySelector(".goal-entry-text").setAttribute("aria-label", paused ? "Paused priority" : `Priority ${place}`);
      row.querySelector(".inbox-grip").setAttribute("aria-label", `Move ${name}: drag it, or press the up and down arrows`);
      const toggle = row.querySelector(".goal-entry-pause");
      toggle.replaceChildren(icon(paused ? "play" : "pause"));
      toggle.setAttribute("aria-pressed", String(paused));
      toggle.setAttribute("aria-label", paused ? "Resume this priority" : `Pause ${name}`);
      toggle.title = paused ? "Resume: the sweeper ranks by it again" : "Pause: the sweeper leaves it aside until you resume it";
      row.querySelector(".inbox-remove").setAttribute("aria-label", `Remove ${name}`);
    });
    changed();
  };
  const order = rowOrder(list, sync);
  const row = (entry) => {
    const box = h("textarea", { class: "field goal-entry-text", rows: 1, spellcheck: "true", oninput: sync,
      placeholder: "What matters in your work now?" });
    box.value = entry.text;
    box.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" || event.shiftKey || event.ctrlKey || event.metaKey || event.isComposing) return;
      event.preventDefault();
      const next = row({ id: null, text: "", paused: false });
      item.after(next);
      sync();
      next.querySelector(".goal-entry-text").focus();
    });
    const toggle = h("button", { class: "button small ghost icon-only goal-entry-pause", type: "button",
      onclick: () => { item.classList.toggle("paused"); sync(); } });
    const remove = h("button", { class: "button small ghost icon-only inbox-remove", type: "button",
      onclick: () => {
        const focus = item.nextElementSibling || item.previousElementSibling;
        item.remove();
        sync();
        (focus?.querySelector(".goal-entry-text") || add).focus();
      } }, icon("x"));
    const item = h("li", { class: `goal-entry${entry.paused ? " paused" : ""}` },
      order.grip(() => item), h("span", { class: "goal-entry-rank" }), box, toggle, remove);
    item.goalId = entry.id;
    return item;
  };
  const add = h("button", { class: "button small ghost", type: "button",
    onclick: () => {
      const next = row({ id: null, text: "", paused: false });
      list.append(next);
      sync();
      next.querySelector(".goal-entry-text").focus();
    } }, icon("plus"), h("span", { class: "label", text: "Add a priority" }));
  list.replaceChildren(...(entries.length ? entries : [{ id: null, text: "", paused: false }]).map(row));
  sync();
  return h("div", { class: "goal-rows" }, list, h("div", { class: "inbox-part-actions" }, add));
}

// Moving the rows of an ordered list (the priorities, the inbox's ranking factors): grip(rowOf) makes a row's grip,
// which drags the row rowOf() returns, or moves it with the arrow keys while the grip has focus. After each move
// sync() reads the list again. The drag listens on the document: moving the row in the list would end a pointer
// capture on its grip.
function rowOrder(list, sync) {
  const move = (row, by) => {
    if (by < 0 && !row.previousElementSibling) return;
    if (by > 0 && !row.nextElementSibling) return;
    list.insertBefore(row, by < 0 ? row.previousElementSibling : row.nextElementSibling.nextElementSibling);
    sync();
  };
  const drag = (row, event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    row.classList.add("dragging");
    const follow = (moved) => {
      if (moved.pointerId !== event.pointerId) return;
      const before = [...list.children].find((other) => other !== row && moved.clientY < other.getBoundingClientRect().top + other.offsetHeight / 2) ?? null;
      if (before === row.nextElementSibling) return;
      list.insertBefore(row, before);
      sync();
    };
    const end = (ended) => {
      if (ended.pointerId !== event.pointerId) return;
      row.classList.remove("dragging");
      document.removeEventListener("pointermove", follow);
      document.removeEventListener("pointerup", end);
      document.removeEventListener("pointercancel", end);
      sync();
    };
    document.addEventListener("pointermove", follow);
    document.addEventListener("pointerup", end);
    document.addEventListener("pointercancel", end);
  };
  const grip = (rowOf) => {
    const key = h("button", { class: "button small ghost icon-only inbox-grip", type: "button",
      onpointerdown: (event) => drag(rowOf(), event),
      onkeydown: (event) => {
        const by = { ArrowUp: -1, ArrowDown: 1 }[event.key];
        if (!by) return;
        event.preventDefault();
        move(rowOf(), by);
        key.focus();
      } }, icon("grip"));
    return key;
  };
  return { grip };
}

// The inbox's words while they differ from the settings the view opened on, kept in this browser so Back, a click
// away or a reload loses nothing: {rules, sources, at}, or null when there is no such edit. While one is kept, the
// key on the Inbox heading carries the orange dot.
const INBOX_DRAFT_KEY = "cockpit.draft:inbox-settings";
const inboxDraft = () => readStored(INBOX_DRAFT_KEY, null);

// Each part opens on an edit not yet saved, then on the words of a save still on its way or refused, then on the
// settings as stored. The inbox's settings come from the server; while they load, a second press opens nothing more,
// so a late answer never replaces words being written. When they fail to load, the priorities still open, and the
// inbox's part says what went wrong.
async function openSettings(part) {
  if (settingsView.open) { showSettingsPart(part); return; }
  if (settingsView.opening) return;
  const navigation = ui.navigations;
  let inbox = ui.inboxUnsaved || ui.inboxNext || ui.inboxSaving;
  let inboxError = "";
  if (!inbox) {
    settingsView.opening = true;
    try {
      inbox = await api("GET", "/api/inbox-settings");
    } catch (error) {
      inboxError = error.message;
    } finally {
      settingsView.opening = false;
    }
    // The user went elsewhere while the settings loaded.
    if (ui.navigations !== navigation) return;
  }
  const opening = ++ui.navigations;
  const fromList = onPhone() && phoneListAsked();
  // The view takes the card's place, so the step on the clock stops, as showing another item does (TRACK-8).
  const timer = ui.state && drawn(ui.state).timer;
  if (timer) leaveTimedStep(timer);
  leaveFeedbackHoldStep(null);
  await slideView("in", () => {
    // The transition draws the view a frame later; a row picked or a conversation opened meanwhile goes first.
    if (ui.navigations !== opening) return;
    if (fromList) showPhoneItem(false);
    closeSessionView(false);
    Object.assign(settingsView, { open: true, from: part });
    document.body.classList.add("viewing-settings");
    drawSettings(inbox, inboxError);
    render(false);
    window.scrollTo({ top: 0 });
  });
  if (settingsView.open) showSettingsPart(part);
}

// The part the user asked for: the priorities, with the cursor at the end of the last one (on a phone, the heading,
// so the keyboard stays down until the user picks a box), or how the inbox is ranked.
function showSettingsPart(part) {
  const view = document.getElementById("settings-view");
  const last = [...view.querySelectorAll(".goal-entry-text")].at(-1);
  if (part === "goals" && !onPhone() && last) {
    last.focus();
    last.setSelectionRange(last.value.length, last.value.length);
    return;
  }
  const heading = view.querySelector(part === "goals" ? "#settings-goals-title" : "#inbox-rules-title");
  heading.setAttribute("tabindex", "-1");
  heading.focus({ preventScroll: true });
  if (part === "goals") window.scrollTo({ top: 0 });
  else heading.closest(".inbox-part").scrollIntoView({ block: "start", behavior: "smooth" });
}

function drawSettings(inbox, inboxError) {
  const state = drawn(ui.state);
  const goalsRecoveredBody = readStored("cockpit.unsaved:/api/goals", null);
  const goalsRecovered = goalsRecoveredBody && storedEntries(goalsRecoveredBody);
  const goalsKept = goalsDraft(state);
  const entries = structuredClone(goalsKept?.goals ?? ui.goalsUnsaved ?? goalsRecovered ?? goalEntries(state.goals));
  const goalsChanged = () => !sameEntries(entries, goalEntries(drawn(ui.state).goals));
  // The inbox's words: null when they did not load.
  const draft = inbox ? structuredClone(inbox) : null;
  const inPage = ui.inboxUnsaved || ui.inboxNext || ui.inboxSaving;
  const inboxKept = draft && inboxDraft();
  const inboxRecovered = draft && !inPage && !inboxKept ? readStored("cockpit.unsaved:/api/inbox-settings", null) : null;
  const words = (from) => JSON.stringify([from.rules.points, from.sources.text]);
  const opened = draft && words(inbox);
  const start = inboxKept || inboxRecovered;
  if (start) {
    draft.rules.points = start.rules;
    draft.sources.text = start.sources;
  }
  const inboxChanged = () => Boolean(draft) && words(draft) !== opened;
  // A refused save is sent again by Save, as an edit is.
  const inboxToSave = () => inboxChanged() || Boolean(draft && (ui.inboxUnsaved || inboxRecovered));
  const keep = () => {
    const now = new Date().toISOString();
    writeStored(GOALS_DRAFT_KEY, goalsChanged() ? { goals: entries, at: readStored(GOALS_DRAFT_KEY, null)?.at ?? now } : null);
    if (draft) writeStored(INBOX_DRAFT_KEY, inboxChanged() ? { rules: draft.rules.points, sources: draft.sources.text, at: inboxDraft()?.at ?? now } : null);
    discard.hidden = !(goalsChanged() || ui.goalsUnsaved || goalsRecoveredBody || inboxToSave());
  };
  const back = () => slideView("back", () => closeSettings(true));
  const save = () => {
    writeStored(GOALS_DRAFT_KEY, null);
    saveGoals(entries);
    if (inboxToSave()) {
      writeStored(INBOX_DRAFT_KEY, null);
      saveInboxSettings(draft);
    }
    back();
  };
  // Discard drops the edits and the refused words, in this page and as kept in this browser. Back keeps them.
  const discardAll = () => {
    writeStored(GOALS_DRAFT_KEY, null);
    writeStored("cockpit.unsaved:/api/goals", null);
    ui.goalsUnsaved = null;
    if (draft) {
      writeStored(INBOX_DRAFT_KEY, null);
      writeStored("cockpit.unsaved:/api/inbox-settings", null);
      ui.inboxUnsaved = null;
    }
    back();
  };
  const discard = h("button", { class: "button ghost settings-discard", type: "button", onclick: discardAll }, "Discard");
  const rows = goalRows(entries, keep);
  const inboxParts = draft
    ? [
      inboxRecovered && h("p", { class: "fail-text settings-note", role: "alert", text: "These inbox settings were not saved. Edit them here and press Save to retry." }),
      inboxKept && h("p", { class: "fail-text settings-note inbox-draft-note", role: "status", text: `Your inbox edit from ${when(new Date(inboxKept.at))} is not saved yet. Save it, or Discard to go back to the saved settings.` }),
      inboxRules(draft.rules, keep),
      inboxSources(draft.sources, keep),
    ]
    : h("section", { class: "inbox-part" },
      h("h3", { class: "inbox-part-title", id: "inbox-rules-title", text: "How your inbox is ranked" }),
      h("p", { class: "fail-text", role: "alert", text: `Inbox settings not loaded: ${inboxError}. Go back and open them again to retry.` }));
  fill("settings-view", h("article", {
    class: "card settings-card",
    onkeydown: (event) => {
      if (event.key === "Escape" && !event.isComposing) { event.preventDefault(); back(); }
    },
  },
  h("header", { class: "session-head settings-head" },
    h("button", { class: "button small ghost", type: "button", onclick: back }, icon("back"), h("span", { class: "label", text: "Back" })),
    h("h2", { class: "settings-title", text: "Priorities and inbox" })),
  h("form", {
    class: "settings-form",
    onsubmit: (event) => { event.preventDefault(); save(); },
    onkeydown: (event) => { if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); save(); } },
  },
  h("section", { class: "inbox-part" },
    h("h3", { class: "inbox-part-title", id: "settings-goals-title", text: "Priorities" }),
    h("p", { class: "inbox-part-note", text: "Most important first; drag to reorder. The sweeper ranks your work by them, and leaves a paused one aside until you resume it." }),
    goalsRecovered && !goalsKept && h("p", { class: "fail-text settings-note", role: "alert", text: "These priorities were not saved. Edit them here and press Save to retry." }),
    goalsKept && h("p", { class: "fail-text settings-note goals-draft-note", role: "status", text: `Your edit from ${when(new Date(goalsKept.at))} is not saved yet. Save it, or Discard to go back to the saved list.` }),
    rows),
  inboxParts,
  h("div", { class: "settings-actions" },
    h("button", { class: "button primary", type: "submit", "data-key": "Ctrl+Enter" }, "Save"),
    discard))));
  keep();
}

// Leaving the view: the openers are drawn again, so each shows the dot for an edit left behind. Back (`back`) gives
// the focus to the opener the view opened from; a row picked in the rail or a conversation opened keeps its own.
function closeSettings(back) {
  if (!settingsView.open) return;
  const from = settingsView.from;
  Object.assign(settingsView, { open: false, from: null });
  document.body.classList.remove("viewing-settings");
  // The card shows again, so a step the view left starts again (leaveTimedStep).
  ui.leftStepId = null;
  fill("settings-view");
  renderGoals(drawn(ui.state));
  document.querySelector(".inbox-settings-open")?.replaceWith(inboxSettingsKey());
  if (!back) return;
  ui.navigations++;
  render(false);
  document.querySelector(from === "inbox" ? ".inbox-settings-open" : "#goals .goals-box")?.focus({ preventScroll: true });
}


// A key that puts Cockpit's own words back. It stays in view, and does nothing while the words are Cockpit's.
function inboxReset(onReset) {
  return h("button", { class: "button small ghost inbox-reset", type: "button", onclick: onReset },
    icon("back"), h("span", { class: "label", text: "Cockpit's default" }));
}

// How your inbox is ranked: the factors in order of importance, numbered. Each point has a grip to drag it, or to move it with the
// arrow keys while the grip has focus (rowOrder); its words in a box that grows with them; and a key to remove it. Enter in a
// point starts a new point below it. The list, not each box, is the draft, read again after every change, and each
// change is passed to changed().
function inboxRules(settings, changed) {
  const list = h("ol", { class: "inbox-points", "aria-labelledby": "inbox-rules-title" });
  const boxes = () => [...list.querySelectorAll(".inbox-point-text")];
  const sync = () => {
    settings.points = boxes().map((box) => box.value);
    [...list.children].forEach((row, index) => {
      const n = index + 1;
      row.querySelector(".inbox-rank").textContent = `${n}.`;
      row.querySelector(".inbox-point-text").setAttribute("aria-label", `Point ${n}`);
      row.querySelector(".inbox-grip").setAttribute("aria-label", `Move point ${n}: drag it, or press the up and down arrows`);
      row.querySelector(".inbox-remove").setAttribute("aria-label", `Remove point ${n}`);
    });
    reset.disabled = JSON.stringify(settings.points.map((point) => point.trim()).filter(Boolean)) === JSON.stringify(settings.default_points);
    changed();
  };
  const order = rowOrder(list, sync);
  const point = (words) => {
    const box = h("textarea", { class: "field inbox-point-text", rows: 1, spellcheck: "true", oninput: sync });
    box.value = words;
    box.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" || event.shiftKey || event.ctrlKey || event.metaKey || event.isComposing) return;
      event.preventDefault();
      const next = point("");
      row.after(next);
      sync();
      next.querySelector(".inbox-point-text").focus();
    });
    const remove = h("button", { class: "button small ghost icon-only inbox-remove", type: "button",
      onclick: () => {
        const focus = row.nextElementSibling || row.previousElementSibling;
        row.remove();
        sync();
        (focus?.querySelector(".inbox-point-text") || add).focus();
      } }, icon("x"));
    const row = h("li", { class: "inbox-point" }, order.grip(() => row), h("span", { class: "inbox-rank", "aria-hidden": "true" }), box, remove);
    return row;
  };
  const add = h("button", { class: "button small ghost", type: "button",
    onclick: () => {
      const row = point("");
      list.append(row);
      sync();
      row.querySelector(".inbox-point-text").focus();
    } }, icon("plus"), h("span", { class: "label", text: "Add a point" }));
  const reset = inboxReset(() => {
    list.replaceChildren(...settings.default_points.map(point));
    sync();
    list.querySelector(".inbox-point-text").focus();
  });
  list.replaceChildren(...settings.points.map(point));
  sync();
  return h("section", { class: "inbox-part" },
    h("h3", { class: "inbox-part-title", id: "inbox-rules-title", text: "How your inbox is ranked" }),
    h("p", { class: "inbox-part-note", text: settings.lead }),
    list,
    h("div", { class: "inbox-part-actions" }, add, reset));
}

// What to check: one text box. Each change is passed to changed().
function inboxSources(settings, changed) {
  const words = h("textarea", {
    id: "inbox-sources", class: "field inbox-text", rows: 8, spellcheck: "true",
    "aria-describedby": "inbox-sources-note",
    oninput: () => { settings.text = words.value; reset.disabled = settings.text.trim() === settings.default_text; changed(); },
  });
  words.value = settings.text;
  const reset = inboxReset(() => { settings.text = settings.default_text; words.value = settings.text; reset.disabled = true; changed(); words.focus(); });
  reset.disabled = settings.text.trim() === settings.default_text;
  return h("section", { class: "inbox-part" },
    h("label", { class: "inbox-part-title", for: "inbox-sources", text: "What to check for new work" }),
    h("p", { class: "inbox-part-note", id: "inbox-sources-note", text: "On each sweep it checks these for work that needs you, and adds a task for each it finds." }),
    words,
    h("div", { class: "inbox-part-actions" }, reset));
}

// The page sends both texts; the server keeps only one that differs from Cockpit's own. If it refuses, the editor
// opens again on the refused words. A save made while the last one is on its way goes once that one is done, and
// until then it lives only in this page, so leaving the page asks first.
function saveInboxSettings(draft) {
  ui.inboxUnsaved = null;
  if (ui.busy.has("inbox-settings:set")) {
    ui.inboxNext = draft;
    holdLeave(ui.inboxSent);
    return null;
  }
  const body = { rules: draft.rules.points, sources: draft.sources.text };
  ui.inboxSaving = draft;
  ui.inboxSent = run("inbox-settings:set", async () => {
    try {
      await api("PUT", "/api/inbox-settings", body);
    } catch (error) {
      ui.inboxUnsaved = ui.inboxNext || draft;
      ui.inboxNext = null;
      throw new Error(`Inbox settings not saved: ${error.message}`, { cause: error });
    }
    toast("Inbox settings saved. The sweeper follows them from its next sweep.");
  }, null).then(() => {
    ui.inboxSaving = null;
    const next = ui.inboxNext;
    ui.inboxNext = null;
    if (next) saveInboxSettings(next);
  });
  return ui.inboxSent;
}

// The list shows the new priorities at once; one whose words stay keeps its goal, as on the server (store.set_goals),
// and pauses or holds as the editor left it. If the server refuses, the list goes back and the next edit starts from
// the refused priorities. A save made while the last one is on its way goes once that one is done, and until then it
// lives only in this page, so leaving the page asks first.
function saveGoals(edited) {
  if (ui.busy.has("goals:set")) {
    ui.goalsNext = edited;
    holdLeave(ui.goalsSent);
    return null;
  }
  const goals = keptEntries(edited);
  const before = drawn(ui.state).goals;
  ui.goalsUnsaved = null;
  if (sameEntries(goals, goalEntries(before))) {
    writeStored("cockpit.unsaved:/api/goals", null);
    return null;
  }
  const now = new Date().toISOString();
  // The list the server will return, as store.set_goals pairs it: a priority with an id keeps that goal, one without
  // takes a goal with the same words that no other priority claimed, else it is new.
  const local = (current) => {
    const claimed = new Set(goals.map((entry) => entry.id).filter((id) => current.some((goal) => goal.id === id)));
    return goals.map((entry, index) => {
      let same = claimed.has(entry.id) ? current.find((goal) => goal.id === entry.id) : null;
      if (!same) {
        same = current.find((goal) => !claimed.has(goal.id) && goal.text === entry.text);
        if (same) claimed.add(same.id);
      }
      const paused_at = entry.paused ? same?.paused_at ?? now : null;
      return same ? { ...same, text: entry.text, position: index + 1, paused_at }
        : { id: `local-${++ui.sendCount}`, text: entry.text, position: index + 1, created_at: now, paused_at };
    });
  };
  ui.goalsSent = run("goals:set", async () => {
    try {
      await optimistic((state) => ({ ...state, goals: local(state.goals), goals_summary: null }),
        () => api("PUT", "/api/goals", { goals }),
        () => { ui.goalsUnsaved = goals; });
    } catch (error) {
      throw new Error(`Priorities not saved: ${error.message}`, { cause: error });
    }
  }, null).then(() => {
    const next = ui.goalsNext;
    ui.goalsNext = null;
    if (next !== null) saveGoals(next);
  });
  return ui.goalsSent;
}

// The user's goals by place: the first goal that is not paused is 1, as rank.py counts them.
function goalPlaces(goals) {
  const places = new Map();
  for (const goal of goals) if (!goal.paused_at) places.set(goal.id, places.size + 1);
  return places;
}

// Where your Archive of a session stands (SESSION-4), as a card: the state, then why; an archived
// session leaves Sessions.
function archiveWords(archive) {
  if (archive.state === "failed") return `Not archived\n${archive.failure}`;
  return `Archiving\nYou archived step ${archive.step_id}, its last open step. Cockpit stops the session once its turn on that news is over.`;
}

// What a session's Claude calls cost so far (SESSION-9): $6,349 / $138 / $4.20, cents only under $10.
function spendDollars(usd) {
  const digits = usd < 10 ? 2 : 0;
  return `$${usd.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

// The session's use in the measure last picked on the Usage page (SESSION-10), the share of a week unless
// another was picked: 4.3 % / $103 / $2.15.
function spendShown(spend) {
  let measure = "week";
  try { measure = localStorage.getItem("cockpit:usage-measure") || "week"; } catch { /* storage blocked */ }
  if (measure === "api") return spendDollars(spend.usd);
  if (measure === "plan") return spendDollars(spend.plan_usd);
  return `${spend.week_percent.toFixed(spend.week_percent < 10 ? 1 : 0)} %`;
}

function spendWords(spend) {
  const count = (tokens) => tokens >= 1e6 ? `${(tokens / 1e6).toFixed(1)} M` : `${Math.round(tokens / 1e3)} k`;
  return [
    `Claude use of this session and its subagents: ${spendDollars(spend.usd)} at API list prices`,
    `${spend.week_percent.toFixed(1)} % of one account's weekly limit, ${spendDollars(spend.plan_usd)} at the plan's price`,
    `Input ${count(spend.tokens.fresh)} uncached, ${count(spend.tokens.cache_write)} written to the cache, ${count(spend.tokens.cache_read)} read from it; output ${count(spend.tokens.output)}`,
    "The Usage page has the details.",
  ].join("\n");
}

function sessionOrder(session) {
  return { blocked: 0, working: 1, idle: 2, failed: 3, done: 4, stopped: 5 }[sessionState(session)] ?? 6;
}

// How the one list of work sorts and groups (VIEW-3, VIEW-21), as the user last picked in this browser. Who moves next
// is the list as it was, its groups by who has the next move; every other grouping shows all the open work, also the
// work whose steps show above.
const WORK_SORTS = { recent: "Most recent", messages: "Your latest message", active: "Active first", use: "Most Claude use", name: "Name" };
const WORK_GROUPS = { next: "Who moves next", state: "By session state", stage: "By stage", priority: "By priority",
  repo: "By repo", account: "By account", none: "No groups" };
// Claude Code can list a session with no state; it groups and shows as not known.
const SESSION_STATE_GROUPS = { blocked: "Waiting for a permission", working: "Working", idle: "Waiting on a background task",
  failed: "Failed", done: "Finished their turn", stopped: "Stopped", "": "State not known" };
const PRIORITY_GROUPS = [["urgent", "Urgent"], ["high", "High priority"], ["normal", "Normal priority"], ["low", "Low priority"]];
const LOOSE_NAME = "Sessions on no item";

function storedChoice(key, choices, fallback) {
  const value = readStored(key, fallback);
  return Object.hasOwn(choices, value) ? value : fallback;
}

// The sessions of the last day that no open item lists: Cockpit's own sweep, sessions of finished tasks,
// and work not on Cockpit. Each has a row of its own in the list; every other session shows in its item's row.
function looseSessions(state) {
  return state.sessions.filter((session) => !listingItem(state, session.session_id));
}

// The done or closed item a session worked on, the newest that lists it, or null. Only for a session on no open item.
function endedItem(state, sessionId) {
  return [...state.items, ...state.ended_items].filter((item) => item.sessions.includes(sessionId))
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at))[0] || null;
}

// The open item a session works on, neither archived nor finished: the first that lists it, else the first whose
// steps it asks or answers.
function listingItem(state, sessionId) {
  const open = state.items.filter((item) => !item.archived && item.ball !== "done");
  return open.find((item) => item.sessions.includes(sessionId)) || open.find((item) => item.step_sessions.includes(sessionId));
}

// An open item's sessions: those it lists, then the sessions that ask or answer its steps (`step_sessions`), as
// the overview and the server's lamp count them (VIEW-2).
function itemSessions(item) {
  return [...item.sessions, ...item.step_sessions.filter((id) => !item.sessions.includes(id))];
}

// Where a session's composer lives: the first place the page draws its open item, which is the item's step on Next
// up, in the queue, Ready next or Snoozed (in that order, as openItemStep finds it), else the item's row in the list.
// Null for a session on no open item, whose own row holds its composer.
function sessionHome(state, sessionId) {
  const item = listingItem(state, sessionId);
  if (!item) return null;
  const step = [ui.next, ...state.queue, ...state.ready_next, ...state.snoozed].find((one) => one && one.item_id === item.id);
  return { item, step: step || null };
}

// A row of the list: an item with the sessions of the last day it lists (`live`, its first session first), or a
// session no item lists.
function workEntry(item, session, sessions) {
  return { item, session, live: item ? itemSessions(item).map((id) => sessions.get(id)).filter(Boolean) : [session] };
}

// When the row last changed: the item, or the latest turn of a session on it.
function entryTime(entry) {
  return [entry.item ? entry.item.updated_at : "", ...entry.live.map((session) => session.updated_at || "")]
    .reduce((latest, at) => at > latest ? at : latest, "");
}

function entryLabel(entry) {
  return entry.item ? entry.item.title : sessionLabel(entry.session, entry.session.cwd);
}

// The row's most active session: the one Active first puts first.
function entryLead(entry) {
  return [...entry.live].sort((a, b) => sessionOrder(a) - sessionOrder(b))[0];
}

function workSorter(sort) {
  const recent = (a, b) => entryTime(b).localeCompare(entryTime(a));
  const use = (entry) => entry.live.reduce((sum, session) => sum + (session.spend ? session.spend.usd : 0), 0);
  const order = (entry) => entryLead(entry) ? sessionOrder(entryLead(entry)) : 6;
  if (sort === "active") return (a, b) => order(a) - order(b) || recent(a, b);
  if (sort === "use") return (a, b) => use(b) - use(a) || recent(a, b);
  if (sort === "name") return (a, b) => entryLabel(a).localeCompare(entryLabel(b), undefined, { sensitivity: "base" }) || recent(a, b);
  return recent;
}

// A row's group's name, and its place among the groups: by session state in the order Active first uses, stages
// and priorities in their own order, the others in the order of their first row, with the rows that belong
// nowhere last. An item goes by its most active session's state, and by its first session's repo and account.
function entryGroup(group, entry) {
  const { item } = entry;
  const lead = entryLead(entry);
  const first = entry.session || entry.live[0];
  if (group === "state") return lead ? { name: SESSION_STATE_GROUPS[sessionState(lead)] || sessionState(lead), rank: sessionOrder(lead) } : { name: "No session in the last day", rank: 7 };
  if (!item && (group === "stage" || group === "priority")) return { name: LOOSE_NAME, rank: 9 };
  if (group === "stage") return item.stage ? { name: capitalize(STAGE_LABELS[item.stage] || item.stage), rank: Object.keys(STAGE_LABELS).indexOf(item.stage) }
    : { name: "No stage yet", rank: 8 };
  if (group === "priority") {
    const at = PRIORITY_GROUPS.findIndex(([priority]) => priority === item.priority);
    return at < 0 ? { name: "Normal priority", rank: 2 } : { name: PRIORITY_GROUPS[at][1], rank: at };
  }
  if (group === "repo") {
    const repo = (item && item.project) || repoOf(first && first.cwd);
    return repo ? { name: repo, rank: 0 } : { name: "No repo known", rank: 1 };
  }
  if (!first) return { name: "No session in the last day", rank: 2 };
  if (first.provider === "codex") return { name: "Codex", rank: 0 };
  return first.account ? { name: `${capitalize(first.account)} account`, rank: 0 } : { name: "Account not known", rank: 1 };
}

// The rows in their groups: [{name, entries}] in the groups' order, the sorted order inside each.
function groupEntries(group, entries) {
  const groups = new Map();
  entries.forEach((entry, at) => {
    const { name, rank } = entryGroup(group, entry);
    if (!groups.has(name)) groups.set(name, { name, rank, first: at, entries: [] });
    groups.get(name).entries.push(entry);
  });
  return [...groups.values()].sort((a, b) => a.rank - b.rank || a.first - b.first);
}

function listPicker(label, key, choices, value, pick) {
  return h("label", { class: "list-pick" }, h("span", { text: label }),
    h("select", { "data-key": key, onchange: (event) => pick(event.target.value) },
      Object.entries(choices).map(([id, words]) => h("option", { value: id, selected: id === value }, words))));
}

// The list's head: Sort and Group, and New session with its composer under it once pressed.
function workHead(state, sort, group, groups) {
  const newKey = "msg:new-session";
  const starting = ui.expanded.has(newKey);
  return [
    h("div", { class: "list-picks work-picks" },
      listPicker("Sort", "work:sort", WORK_SORTS, sort, (value) => { writeStored("cockpit:work-sort", value); render(false); }),
      listPicker("Group", "work:group", groups, group, (value) => { writeStored("cockpit:work-group", value); render(false); }),
      h("button", {
        class: "button small ghost new-session", type: "button", "aria-expanded": String(starting),
        onclick: () => toggle(newKey),
      }, icon(starting ? "x" : "plus"), h("span", { class: "label", text: starting ? "Close" : "New session" }))),
    starting && composer(newKey, { session: null, context: {}, project: null, step: null, handOff: false, toSession: false }),
  ];
}

// What marks a session beside its name: Codex, the sweeper, the move to another account it waits for, the user's
// Archive of it, and the link that opens it in iTerm2.
function sessionMarks(session) {
  return [
    session.standing && h("span", {
      class: "side-mark", role: "img", "aria-label": session.standing_name, title: session.standing_name,
    }, icon("shield")),
    session.provider === "codex" && h("span", { class: "side-mark", text: "Codex" }),
    session.is_sweep && h("span", { class: "side-mark", role: "img", "aria-label": "Sweep", title: "Sweeper" }, icon("radar")),
    session.move_waits && h("span", {
      class: "side-mark", role: "img", "aria-label": `Moves to ${session.move_waits}`,
      title: `Moves to the ${session.move_waits} account once nothing runs in it (${(session.busy_with || []).join(", ") || "now"})`,
    }, icon("clock")),
    session.archive && h("span", {
      class: `side-mark archive ${session.archive.state}`, role: "img", "aria-label": archiveWords(session.archive).split("\n")[0], title: archiveWords(session.archive),
    }, icon("archive")),
    itermLink(session, { class: "side-mark iterm", "aria-label": "Open in iTerm2" }, icon("terminal")),
  ];
}

function sessionSpend(session) {
  return session.spend && h("a", { class: "side-spend", href: "/usage", title: spendWords(session.spend) }, spendShown(session.spend));
}

// Where a session on no item came from and what is left to do with it, in one line, so the user sees whether to
// close it or give it an item: Cockpit's own sweep needs no item made for it; a session
// waiting on a dialog or request is answered under Blocked and waiting; a stopped session needs no closing; a running
// background Claude session Cockpit stops once idle (delivery.idle_stop_after).
function sessionOrigin(session, ended) {
  if (session.standing) return session.standing_description;
  if (session.is_sweep) return "Cockpit's own session: it ranks and checks your steps. You need not make an item for it.";
  const start = session.started;
  const day = start && dayWords(new Date(start.created_at));
  const by = !start ? "Started outside Cockpit"
    : `Started by ${start.author === "cockpit" ? "Cockpit" : start.author === "user" ? "you" : `agent ${actorWords(start.author)}`}`
      + (day === "today" ? " today" : ` on ${day}`);
  if (session.waits_on_dialog) return `${by}. It waits on you: answer it ${SIDEBAR ? "from its red row in Inbox" : "under Blocked and waiting"}.`;
  if (!session.alive) return `${by}. ${ended ? "Stopped, with its task finished" : "Stopped"}: nothing to close.`;
  if (session.provider === "codex") return `${by}. A Codex thread, still open.`;
  // Cockpit stops only a background session (one with a job file) once it is idle.
  const left = session.job_state ? "Still running; Cockpit stops it after an hour idle, or 15 minutes when memory is low."
    : "Still running in its terminal.";
  return `${by}. ${left}`;
}

// A session no item lists (VIEW-3), as a row of the list like an item's: its name, what it does now, its marks,
// Claude use, state and age; a press opens a composer to it. A session with a card under Blocked and waiting
// shows its dialog there, not twice.
function sessionRow(session, state) {
  const key = `msg:side:${session.session_id}`;
  const open = ui.expanded.has(key) && !ui.heldKeys.has(key);
  const carded = state.waiting_sessions.some((one) => one.session_id === session.session_id);
  const detail = !carded && (session.needs || session.detail);
  // Cockpit's own sweep goes by what it is. A session whose task is done or closed goes by the task's
  // title, marked so. Either way the session's own name shows on hover.
  const own = session.is_sweep ? "Cockpit sweep" : null;
  const ended = !own && endedItem(state, session.session_id);
  const name = sessionLabel(session, session.cwd);
  const title = own || (ended && ended.title);
  const origin = sessionOrigin(session, ended);
  const head = h("div", { class: "row-head", onclick: (event) => { if (!event.target.closest("a, button")) toggle(key); } },
    h("div", { class: "row-main" },
      h("div", { class: "row-line1" },
        opens(h("span", { class: "row-title", title: title ? `Session: ${name}` : session.name ? null : `Session ${session.session_id}`,
          text: title || name }), key),
        sessionMarks(session)),
      h("div", { class: "row-line2" },
        ended && h("span", { class: "chip ended-chip", text: ended.archived ? "Task closed" : "Task done" }),
        h("span", { class: "row-sub session-origin", text: origin })),
      detail && h("div", { class: "row-line2" }, h("span", { class: "row-sub", title: detail, text: detail }))),
    h("div", { class: "row-side" },
      sessionSpend(session),
      h("span", { class: "dots" }, stateDot(sessionState(session), sessionState(session) ? capitalize(sessionState(session)) : "State not known")),
      h("span", { class: "row-age", "data-time": session.updated_at, text: age(session.updated_at) })));
  return h("div", { class: `row session-row${open ? " open" : ""}`, "data-session": session.session_id }, head,
    open && h("div", { class: "row-body" }, composer(key, { session, context: {}, project: null, step: null, handOff: false, toSession: false })));
}

// A retry shows the message as sending at once; if the server refuses, it shows as failed again.
// It stays in the short list until the user leaves the page, so the row does not vanish under the click.
function retryMessage(message) {
  ui.retried.add(message.id);
  return run(`retry:${message.id}`, async () => {
    try {
      const task = finishedTask(drawn(ui.state), message.item_id);
      const sent = await optimistic(
        (shown) => ({ ...shown, messages: shown.messages.map((other) => other.id === message.id ? { ...other, state: "sending", detail: "" } : other) }),
        () => afterFinishes(() => api("POST", `/api/messages/${message.id}/retry`)), null);
      if (sent.reopened && task) { showReopened(task); reopenConfirmed(task); }
    } catch (error) {
      throw refused("Not sent again", error);
    }
  }, null);
}

// Interrupt and send on a message that waits for its session's turn (MSG-3): the session stops at once and
// resumes with it. Send now on one that waits for a place (SESSION-23): it goes past the limit. Either shows as
// sending at once; if the server refuses, it shows as waiting again.
function sendNow(message) {
  return run(`send-now:${message.id}`, async () => {
    const sending = (other) => other.id === message.id
      ? { ...other, state: "sending", waits_since: null, take_back_until: null, capacity_since: null } : other;
    try {
      await optimistic((shown) => ({
        ...shown,
        messages: shown.messages.map(sending),
        with_agents: shown.with_agents.map((step) => step.handoff && step.handoff.id === message.id ? { ...step, handoff: sending(step.handoff) } : step),
      }), () => api("POST", `/api/messages/${message.id}/send-now`), null);
    } catch (error) {
      throw refused("Not sent now", error);
    }
  }, null);
}

// A failed message set aside (MSG-5): its work was done another way or is no longer needed.
// It turns quiet at once; Undo restores it first, because the server refuses a retry of a dismissed
// message.
function dismissMessage(message) {
  const dismissing = run(`message:dismiss:${message.id}`, async () => {
    try {
      await optimistic((shown) => ({
        ...shown,
        messages: shown.messages.map((other) => other.id === message.id ? { ...other, dismissed_at: new Date().toISOString(), dismissed_by: "user" } : other),
      }), () => api("POST", `/api/messages/${message.id}/dismiss`, { reason: null }), null);
    } catch (error) {
      throw refused("Not dismissed", error);
    }
  }, null);
  toast("Dismissed", false, { label: "Undo", run: () => dismissing.then(() => restoreMessage(message)) });
}

function restoreMessage(message) {
  return run(`message:restore:${message.id}`, async () => {
    try {
      await optimistic((shown) => ({
        ...shown,
        messages: shown.messages.map((other) => other.id === message.id ? { ...other, dismissed_at: null, dismissed_by: null, dismiss_reason: null } : other),
      }), () => api("POST", `/api/messages/${message.id}/restore`), null);
    } catch (error) {
      throw refused("Not restored", error);
    }
  }, null);
}

// Where a message went: a session by its name, or a new session (a plus and its name).
function messageTarget(message, sessions) {
  if (message.session_id) return sessionName(sessions.get(message.session_id), message.session_id, "", "to");
  const repo = repoOf(message.new_cwd);
  const name = message.new_name || `a new session in ${repo}`;
  // The name sits in its own span: a flex box clips its text without the ellipsis.
  return h("span", { class: "to new", title: `New session in ${repo || "a checkout"}: ${name}` },
    icon("plus"), h("span", { class: "to-name", text: name }));
}

function messageRow(message, sessions, named) {
  const failed = message.state === "failed" && !message.dismissed_at;
  const dismissed = message.state === "failed" && Boolean(message.dismissed_at);
  const wait = messageDelivery(message) === "waiting" && waitWords(message);
  const why = message.failure || (message.detail || "").split("\n")[0] || "Did not arrive";
  const sentBy = `Sent by ${message.author === "user" ? "you" : actorWords(message.author)}`;
  const dismissedWords = dismissed && setAsideWords(message);
  return h("li", { class: `msg ${dismissed ? "dismissed" : messageDelivery(message)}${named ? "" : " inline"}`, "data-message": message.id },
    h("div", { class: "msg-head" },
      dismissed
        ? h("span", { class: "state-dot dismissed", role: "img", "aria-label": "Set aside", title: dismissedWords })
        : deliveryMark(messageDelivery(message), failed ? "" : message.detail),
      named ? messageTarget(message, sessions) : h("span", { class: "to" }),
      dismissed && iconButton("back", "Restore: show it as failed again", { class: "button small ghost reveal", onclick: () => restoreMessage(message) }),
      h("span", { class: "row-age", title: sentBy, "data-time": message.created_at, text: ago(message.created_at) })),
    hasQuote(message.text)
      ? h("div", { class: "msg-text", title: message.text }, quotedWords(linkWords(message.text), true).nodes)
      : h("div", { class: "msg-text", title: message.text, text: linkWords(message.text) }),
    failed && h("div", { class: "msg-error" },
      h("span", { class: "msg-error-text", title: message.detail || why, text: why }),
      iconButton("retry", "Retry", { class: "button small ghost", onclick: () => retryMessage(message) }),
      iconButton("x", "Clear it: the work was done another way or is no longer needed", { class: "button small ghost", onclick: () => dismissMessage(message) })),
    wait && h("div", { class: "msg-wait" },
      h("span", { class: "msg-wait-text" }, lateMark(wait), `Waits ${wait.since} for the session's turn to end`),
      interruptButton(message)),
    messageDelivery(message) === "place" && h("div", { class: "msg-wait" },
      h("span", { class: "msg-wait-text", text: placeWords(message) }),
      sendNowButton(message)));
}

// The messages a row of the list shows: the user's own, and from anyone the failed ones, which need their Retry and
// Clear (or Restore, once cleared), and the waiting ones, which need their Interrupt and send or Send now.
function shownMessage(message) {
  return message.author === "user" || message.state === "failed" || needsUser(message) || ui.retried.has(message.id);
}

// A message with a control the user may want: a failed one, or one that waits for its session's turn or for a place.
function needsUser(message) {
  return (message.state === "failed" && !message.dismissed_at) || ["waiting", "place"].includes(messageDelivery(message));
}

// The work list sorted by Your latest message puts the work and sessions the user wrote to in the last day first, open
// or closed, the latest message first, each with its messages under it: the newest ROW_MESSAGES until More, and any
// older one that failed or waits. The rest of the open work follows as Most recent sorts it, so opening any task or
// session still finds its row. A message belongs to the task it names, else to the first row that holds its session:
// open tasks, then closed ones, then sessions on no task. Messages no row holds (to an archived task, or a session that
// never started) end the list. Who moves next means nothing here, so the view offers the other groupings.
const ROW_MESSAGES = 3;

function renderMessageWork(state, sessions, group) {
  const groups = Object.fromEntries(Object.entries(WORK_GROUPS).filter(([id]) => id !== "next"));
  const shownGroup = group === "next" ? "none" : group;
  const open = state.items.filter((item) => !item.archived && item.ball !== "done");
  const closed = state.items.filter((item) => item.archived || item.ball === "done");
  const holders = [...open, ...closed].map((item) => workEntry(item, null, sessions))
    .concat(looseSessions(state).map((session) => workEntry(null, session, sessions)))
    .map((entry) => ({ entry, ids: new Set(entry.item ? itemSessions(entry.item) : [entry.session.session_id]), messages: [] }));
  const loose = [];
  const listed = new Set(state.items.map((item) => item.id));
  for (const message of state.messages.filter(shownMessage)) {
    // A message to a task the page no longer lists (archived) goes with the other messages, not under another
    // task that shares its session.
    const holder = holders.find(({ entry }) => entry.item && message.item_id === entry.item.id)
      || (!message.item_id || listed.has(message.item_id) ? holders.find(({ ids }) => ids.has(message.session_id)) : null);
    if (holder) holder.messages.push(message);
    else loose.push(message);
  }
  const written = holders.filter((holder) => holder.messages.length)
    .map(({ entry, messages }) => ({ ...entry, messages }))
    .sort((a, b) => b.messages[0].created_at.localeCompare(a.messages[0].created_at));
  const quiet = holders.filter(({ entry, messages }) => !messages.length && (!entry.item || (!entry.item.archived && entry.item.ball !== "done")))
    .map(({ entry }) => ({ ...entry, messages: [] })).sort(workSorter("recent"));
  const rows = [...written, ...quiet];
  const row = (entry) => {
    const el = entry.item ? itemRow(entry.item, state, sessions, null) : sessionRow(entry.session, state);
    if (entry.messages.length) el.append(rowMessages(entry, sessions));
    return el;
  };
  const count = (number) => h("span", { class: "group-count", text: number });
  const list = !rows.length && !loose.length ? h("p", { class: "note", text: "Add work or start a session" })
    : shownGroup === "none" ? h("div", { class: "list" }, rows.map(row))
      : groupEntries(shownGroup, rows).map((part) => disclosure(`view:${shownGroup}:${part.name}`, "group view", [part.name, count(part.entries.length)],
        h("div", { class: "list" }, part.entries.map(row)), true));
  // The steps handed to an agent keep their list, with Take back, as in every other view.
  fill("work", workHead(state, "messages", shownGroup, groups), state.with_agents.length > 0 && handedList(state, state.with_agents),
    !written.length && h("p", { class: "note group-caption", text: "You sent no messages in the last day; the open work follows, the most recent first." }),
    list,
    loose.length > 0 && disclosure("msgs:loose", "group loose", ["Messages to closed or no work", count(loose.length)],
      h("ul", { class: "side-list msg-list" }, loose.map((message) => messageRow(message, sessions, true))), true));
}

// A row's messages, newest first. The session each went to shows only when the task has more than one, or for an
// agent's failed message, whose sender then shows in its tooltip.
function rowMessages(entry, sessions) {
  const key = `msgs:${entry.item ? `item:${entry.item.id}` : entry.session.session_id}`;
  const all = ui.expanded.has(key);
  const named = Boolean(entry.item && itemSessions(entry.item).length > 1);
  const shown = all ? entry.messages : entry.messages.filter((message, at) => at < ROW_MESSAGES || needsUser(message));
  return h("div", { class: "row-msgs" },
    h("ul", { class: "side-list msg-list" }, shown.map((message) => messageRow(message, sessions, named || message.author !== "user"))),
    entry.messages.length > shown.length || (all && entry.messages.length > ROW_MESSAGES) ? h("button", { class: "link-button", type: "button", onclick: () => toggle(key) },
      all ? "Fewer" : `${entry.messages.length - shown.length} more`) : null);
}

// Patterns load when the user opens the panel, and again only on Reload: they move slowly.
function loadPatterns() {
  ui.patterns = "loading";
  ui.patternsError = "";
  renderPatterns();
  api("GET", `/api/patterns?days=${PATTERN_DAYS}`)
    .then((report) => { ui.patterns = report; }, (error) => { ui.patterns = null; ui.patternsError = error.message; })
    .finally(renderPatterns);
}

// The columns of the steps-by-kind table: the outcome's key in the report, its icon and its words.
const PATTERN_COLUMNS = [
  ["created", "plus", "Made"],
  ["done", "check", "Done or archived by you"],
  ["closed by agents", "bolt", "Closed by agents"],
  ["dismissed", "x", "Dismissed by you"],
  ["withdrawn", "back", "Withdrawn before you got to them"],
  ["snoozed", "clock", "Snoozed"],
  ["taken to the top", "up", "Taken to the top by you"],
  ["handed off", "handoff", "Given to agents"],
];

function kindTable(rows) {
  const columns = PATTERN_COLUMNS.filter(([key]) => rows.some((row) => row[key]));
  return h("table", { class: "pattern-table" },
    h("thead", {}, h("tr", {}, h("th", { scope: "col", text: "Kind" }),
      columns.map(([, name, words]) => h("th", { scope: "col", title: words, "aria-label": words }, icon(name))))),
    h("tbody", {}, rows.map((row) => h("tr", {}, h("th", { scope: "row", text: row.name }),
      columns.map(([key]) => h("td", { text: row[key] || "" }))))));
}

// Recent sweeps as bars, oldest first: the height is how long the sweep took, red when it failed.
function sweepBars(sweeps) {
  const longest = Math.max(1, ...sweeps.map((sweep) => sweep.minutes || 0));
  return h("div", { class: "sweep-bars", role: "list", "aria-label": "Recent sweeps" }, [...sweeps].reverse().map((sweep) => {
    const failed = Boolean(sweep.error) && sweep.minutes !== null;
    const words = [
      [sweep.minutes === null ? "not finished" : `${sweep.minutes} min`, plural(sweep.steps_put, "step", "steps"),
        sweep.minutes_to_first_step !== null && `first after ${sweep.minutes_to_first_step} min`].filter(Boolean).join(" · "),
      failed && sweep.error,
    ].filter(Boolean).join("\n");
    const bar = h("span", { class: `sweep-bar${failed ? " fail" : ""}${sweep.minutes === null ? " open" : ""}`, role: "listitem", title: words, "data-time": sweep.started_at });
    bar.style.height = `${Math.max(8, Math.round(((sweep.minutes || 0) / longest) * 100))}%`;
    return bar;
  }));
}

function patternsBody(report) {
  return [
    h("h3", { text: "Repeats" }),
    report.repeats.length
      ? h("ul", { class: "pattern-list" }, report.repeats.map((repeat) => h("li", { text: repeat })))
      : h("p", { class: "note", text: "No repeated issues" }),
    report.by_kind.length > 0 && [h("h3", { text: "Steps by kind" }), kindTable(report.by_kind)],
    report.median_minutes_to_done !== null && h("p", { class: "note", text: `Median to done: ${span(report.median_minutes_to_done)}` }),
    report.sweeps.length > 0 && [h("h3", { text: "Recent sweeps" }), sweepBars(report.sweeps)],
  ];
}

function renderPatterns() {
  let body;
  if (ui.patternsError) body = h("p", { class: "note fail-text", text: `Could not load patterns: ${ui.patternsError}` });
  else if (ui.patterns === null || ui.patterns === "loading") body = Loading.waiting("patterns", Loading.ghost("w-80|w-60|w-70", "Loading the patterns"));
  else body = Loading.arrive("patterns", patternsBody(ui.patterns));
  const reload = (ui.patternsError || (ui.patterns && ui.patterns !== "loading"))
    && iconButton("retry", "Reload", { class: "button small ghost head-action", onclick: (event) => { event.preventDefault(); loadPatterns(); } });
  const el = disclosure("patterns", "more", [h("span", { class: "summary-text", title: `Last ${PATTERN_DAYS} days`, text: "Patterns" }), reload], body, false);
  // Redraws set `open` too, which fires toggle again: load only what has not been asked for.
  el.addEventListener("toggle", () => { if (el.open && ui.patterns === null && !ui.patternsError) loadPatterns(); });
  fill("patterns", el);
}

// The user's time (TRACK-3) loads when the user opens the panel, and again only on Reload, like Patterns. The
// days are the user's, in the browser's time zone.
const TIME_DAYS = 14;

function loadTime() {
  ui.time = "loading";
  ui.timeError = "";
  renderTime();
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const time = api("GET", `/api/time?days=${TIME_DAYS}&tz=${encodeURIComponent(zone)}`)
    .then((report) => { ui.time = report; }, (error) => { ui.time = null; ui.timeError = error.message; });
  Promise.all([time, loadMac()]).finally(renderTime);
}

// Today on the Mac (TRACK-9) in the setup's zone, beside the timer's days in "Your time"; its own error leaves the rest showing.
function loadMac() {
  return api("GET", "/api/activity/day")
    .then((summary) => { ui.mac = summary; ui.macError = ""; }, (error) => { ui.mac = null; ui.macError = error.message; });
}

function macAction(method, path, body, doing) {
  return api(method, path, body).then(
    (reply) => loadMac().then(() => { renderTime(); return reply; }),
    (error) => { toast(`Could not ${doing}: ${error.message}`, true); },
  );
}

const MAC_CATEGORIES = [
  ["work", "Work"],
  ["neutral", "Neutral"],
  ["distraction", "Distraction"],
  ["unsorted", "Unsorted"],
];

// Pressing a label's category moves it on: work, neutral, distraction, then work again.
const NEXT_CATEGORY = { work: "neutral", neutral: "distraction", distraction: "work", unsorted: "work" };
const MAC_LABELS = 10;

function macBar(summary) {
  const total = Math.max(1, summary.minutes + summary.away);
  const parts = [...MAC_CATEGORIES.map(([key]) => [key, summary.categories[key]]), ["away", summary.away]];
  return h("span", { class: "mac-bar", "aria-hidden": "true" }, parts.filter(([, minutes]) => minutes > 0).map(([key, minutes]) => {
    const part = h("span", { class: `mac-${key}` });
    part.style.width = `${(minutes / total) * 100}%`;
    return part;
  }));
}

function macKeyButton(words, tip) {
  return h("button", { class: "button small ghost", type: "button", title: tip, onclick: () =>
    macAction("POST", "/api/activity/key", {}, "make the Terminal line for the Mac").then((reply) => {
      if (!reply) return;
      ui.macInstall = reply.install;
      renderTime();
    }) }, words);
}

function macInstallLine() {
  if (!ui.macInstall) return null;
  return h("div", { class: "mac-install" },
    h("p", { class: "note", text: "Paste this line into Terminal on your Mac within 15 minutes; it works once. macOS asks you to Allow it once per browser." }),
    h("pre", {}, h("code", { text: ui.macInstall })),
    h("button", { class: "button small", type: "button", onclick: (event) => copy(ui.macInstall, event.currentTarget) }, "Copy line"));
}

function macBody(summary) {
  const mac = summary.mac;
  if (!mac.connected) {
    return [h("p", { class: "note", text: "Connect your Mac to see which apps and sites your time went to, and how much of it was outside any task's timer." }),
      macKeyButton("Connect a Mac", "Makes the Terminal line that installs the recorder"), macInstallLine()];
  }
  const seen = mac.last_seen ? `Last heard from ${ago(mac.last_seen)}` : "Not heard from yet";
  const pause = h("button", { class: "button small ghost", type: "button", onclick: () =>
    macAction("POST", "/api/activity/pause", { paused: !mac.paused }, mac.paused ? "resume recording" : "pause recording") },
  mac.paused ? "Resume" : "Pause");
  const totals = [
    ...MAC_CATEGORIES.map(([key, words]) => [words, summary.categories[key] * 60, key]),
    ["Away", summary.away * 60, "away"],
  ].filter(([, seconds]) => seconds > 0);
  const labels = summary.labels.slice(0, MAC_LABELS).map((row) => h("li", {},
    h("span", { class: "time-name", title: row.label, text: row.label }),
    h("button", { class: `mac-category mac-${row.category}`, type: "button", title: "Press to change its category",
      onclick: () => macAction("PUT", "/api/activity/categories", { name: row.label, category: NEXT_CATEGORY[row.category] }, "change the category") },
    MAC_CATEGORIES.find(([key]) => key === row.category)[1]),
    h("span", { class: "time-value", text: timerTotal(row.minutes * 60) })));
  return [
    h("p", { class: "note mac-state" }, mac.paused ? "Paused. " : "", seen, " ", pause, " ", macKeyButton("New key", "Makes a new Terminal line; the Mac's old key stops once you run it")),
    macInstallLine(),
    summary.minutes + summary.away > 0
      ? [macBar(summary),
        h("ul", { class: "time-list mac-totals" }, totals.map(([words, seconds, key]) => h("li", {},
          h("span", { class: "time-name" }, h("span", { class: `mac-dot mac-${key}`, "aria-hidden": "true" }), words),
          h("span", { class: "time-value", text: timerTotal(seconds) })))),
        summary.outside_tasks !== null && h("p", { class: "note", text: `${timerTotal(summary.outside_tasks * 60)} of the time at your Mac was outside any task's timer.` }),
        h("ul", { class: "time-list mac-labels" }, labels)]
      : h("p", { class: "note", text: "Nothing recorded today yet." }),
  ];
}

const DAY_FORMAT = { weekday: "short", day: "numeric", month: "short" };

function dayName(date, back) {
  if (back === 0) return "Today";
  if (back === 1) return "Yesterday";
  return new Date(`${date}T12:00:00`).toLocaleDateString([], DAY_FORMAT);
}

// A day's bar against the longest day: time on steps in the accent colour of a running timer, active time without a timer in grey.
function dayBar(day, longest) {
  const steps = h("span", { class: "day-bar-steps" });
  const feedback = h("span", { class: "day-bar-feedback" });
  const free = h("span", { class: "day-bar-free" });
  steps.style.width = `${(day.on_steps / longest) * 100}%`;
  feedback.style.width = `${(day.on_feedback / longest) * 100}%`;
  free.style.width = `${(day.active_without_timer / longest) * 100}%`;
  return h("span", { class: "day-bar", "aria-hidden": "true" }, steps, feedback, free);
}

// Rows of a name and a time, the longest first.
function timeList(rows) {
  return h("ul", { class: "time-list" }, rows.map(([name, seconds]) => h("li", {},
    h("span", { class: "time-name", title: name, text: name }),
    h("span", { class: "time-value", text: timerTotal(seconds) }))));
}

// Per size its session gave a step, the steps your timer ran on, their time together and the middle one's
// (SPEC NEXT-35), so the sizes can be checked against the time they took.
function sizeTable(sizes) {
  return h("table", { class: "pattern-table size-table" },
    h("thead", {}, h("tr", {}, h("th", { scope: "col", text: "Size" }), h("th", { scope: "col", text: "Steps" }),
      h("th", { scope: "col", text: "Total" }), h("th", { scope: "col", title: "Half the steps took less time; half took more", text: "Typical" }))),
    h("tbody", {}, sizes.map((row) => h("tr", {},
      h("th", { scope: "row" }, h("span", { class: `size-bar size-${row.size}`, "aria-hidden": "true" }, h("span", { class: "size-track" })),
        ` ${row.size}`),
      h("td", { text: row.steps }), h("td", { text: timerTotal(row.seconds) }), h("td", { text: timerTotal(row.median) })))));
}

// How the progress estimates on finished work compare with the share of the user's timer time on the task that
// had passed when each was given (SPEC NEXT-35), in a few sentences: those given as the user's time, whether the
// bar takes their gap off, and the earlier ones, given as how far the item's work was.
function progressWords(report) {
  const ahead = (check) => (check.median_gap > 0 ? `${check.median_gap} points ahead of` : check.median_gap < 0 ? `${-check.median_gap} points behind` : "level with");
  const check = report.progress;
  const words = [check
    ? `On ${plural(check.items, "finished task", "finished tasks")}, ${plural(check.estimates, "estimate", "estimates")} of your time: typically ${ahead(check)} the share of your timer time on the task that had passed, and ${check.median_off} points off either way.`
    : "No finished task has an estimate of your time yet. Once one finishes, this compares each estimate with the share of your timer time on the task that had passed."];
  words.push(report.progress_correction
    ? `The bars take that gap off each estimate.`
    : `The bars show the estimates as given until ${TIME_CORRECTION_FROM} are checked.`);
  const before = report.progress_before;
  if (before) words.push(`Earlier estimates measured how far the task's work was; against your timer, ${plural(before.estimates, "estimate", "estimates")} of that kind on ${plural(before.items, "finished task", "finished tasks")} were typically ${ahead(before)} it.`);
  return words.join(" ");
}

// timesheet.CORRECTION_FROM, in words.
const TIME_CORRECTION_FROM = "8 estimates on 3 finished tasks";

// [key, name, what it counts, the icon heading the column in place of the name or null]. Feedback gets
// an icon so the table fits the side column.
const TIME_COLUMNS = [
  ["worked", "Worked", "Active time, step timers and feedback", null],
  ["on_steps", "On steps", "Time tracked on steps", null],
  ["on_feedback", "Feedback", "Feedback", "feedback"],
  ["active_without_timer", "No timer", "Active time outside steps and feedback", null],
];

function timeBody(report) {
  const longest = Math.max(1, ...report.days.map((day) => day.worked));
  const shown = (seconds) => (seconds > 0 ? timerTotal(seconds) : "–");
  const days = report.days.map((day, back) => ({ ...day, name: dayName(day.date, back) })).filter((day, back) => back === 0 || day.worked > 0);
  const since = report.input_since && new Date(report.input_since);
  return [
    h("h3", { text: "On your Mac today" }),
    ui.macError ? h("p", { class: "note fail-text", text: `Could not load your Mac's record: ${ui.macError}` }) : ui.mac && macBody(ui.mac),
    h("h3", { text: "Your days" }),
    h("table", { class: "pattern-table time-table" },
      h("thead", {}, h("tr", {}, h("th", { scope: "col", text: "Day" }),
        TIME_COLUMNS.map(([, words, tip, head]) => h("th", { scope: "col", title: tip, "aria-label": words },
          head ? icon(head) : words)),
        h("th", { scope: "col", class: "bar-cell", "aria-hidden": "true" }))),
      h("tbody", {}, days.map((day) => h("tr", {}, h("th", { scope: "row", text: day.name }),
        TIME_COLUMNS.map(([key]) => h("td", { text: shown(day[key]) })),
        h("td", { class: "bar-cell" }, dayBar(day, longest))))),
      h("tfoot", {}, h("tr", {}, h("th", { scope: "row", text: `${TIME_DAYS} days` }),
        TIME_COLUMNS.map(([key]) => h("td", { text: shown(report.totals[key]) })), h("td", { class: "bar-cell" })))),
    report.items.length > 0 && [h("h3", { text: "Per task, on steps" }),
      timeList(report.items.slice(0, 12).map((item) => [item.title, item.seconds]))],
    report.kinds.length > 0 && [h("h3", { text: "Per kind of step" }), timeList(report.kinds.map((kind) => [kind.kind, kind.seconds]))],
    h("div", { id: "estimates" },
      report.sizes.length > 0 && [h("h3", { text: "Per size of step" }), sizeTable(report.sizes)],
      h("h3", { text: "Progress estimates" }), h("p", { class: "note", text: progressWords(report) })),
    h("p", { class: "note", text: [
      "Active: input on a Cockpit page, with gaps of at most 5 minutes.",
      since && `Before ${since.toLocaleDateString([], DAY_FORMAT)} ${clock(report.input_since)}, only your actions on Cockpit count as input.`,
    ].filter(Boolean).join(" ") }),
  ];
}

// On the sidebar page "Your time" is a view in the center layer (center.js), opened from the logo menu or a #time link;
// on /classic it is the folded panel below the card.
function renderTime() {
  let body;
  if (ui.timeError) body = h("p", { class: "note fail-text", text: `Could not load your time: ${ui.timeError}` });
  else if (ui.time === null || ui.time === "loading") body = Loading.waiting("time", Loading.ghost("w-70|w-90|w-50", "Loading your time"));
  else body = Loading.arrive("time", timeBody(ui.time));
  const reload = (ui.timeError || (ui.time && ui.time !== "loading"))
    && iconButton("retry", "Reload", { class: "button small ghost head-action", onclick: (event) => { event.preventDefault(); loadTime(); } });
  if (SIDEBAR) {
    const view = document.querySelector(".center-layer:not([hidden]) .center-body.center-time");
    if (!view) return;
    view.replaceChildren(h("div", { class: "center-time-page" }, body));
    view.closest(".center-layer").querySelector(".center-tools").replaceChildren(...[reload].filter(Boolean));
    return;
  }
  const el = disclosure("time", "more", [h("span", { class: "summary-text", title: `Last ${TIME_DAYS} days`, text: "Your time" }), reload], body, false);
  // Redraws set `open` too, which fires toggle again: load only what has not been asked for.
  el.addEventListener("toggle", () => { if (el.open && ui.time === null && !ui.timeError) loadTime(); });
  fill("time", el);
  const estimates = document.getElementById("estimates");
  if (ui.toEstimates && (estimates || ui.timeError)) {
    ui.toEstimates = false;
    if (estimates) estimates.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

// Permission rules live in the user's Claude Code settings file, not on the board: they load when
// The user opens the panel or a blocked card needs them, and each change answers with the new lists.
// Blocked cards mark the sentences already allowed from them, so a load with cards redraws those.
// Resolves once the load settled.
function loadPermissions() {
  ui.permissions = "loading";
  ui.permissionsError = "";
  renderPermissions();
  return bootOr("permissions", "/api/permissions")
    .then((rules) => { ui.permissions = rules; }, (error) => { ui.permissions = null; ui.permissionsError = error.message; })
    .finally(() => {
      if (ui.state && (ui.state.permission_requests.length || (ui.state.moved_past || []).length)) safeRender();
      else renderPermissions();
    });
}

const RULE_LISTS = {
  allow: { name: "tool rule", placeholder: "Bash(npm test *)" },
  auto: { name: "auto-mode sentence", placeholder: "Running the Tasks tests in ~/dev/app is fine." },
};

// Rule changes show in the lists at once; if the settings file refuses one, the
// lists go back, `undo` (or null) puts back what else changed, and the toast says why.
let ruleChanges = Promise.resolve();

function changeRules(list, rule, change, words, undo) {
  const changing = ruleChanges.then(() => {
    const before = ui.permissions;
    const loaded = before && before !== "loading";
    if (loaded) {
      const rules = change === "add" ? [...before[list].filter((other) => other !== rule), rule] : before[list].filter((other) => other !== rule);
      ui.permissions = { ...before, [list]: rules };
      renderPermissions();
    }
    return run(`perm:${change}:${list}:${++ui.sendCount}`, async () => {
      try {
        const path = change === "add" ? "/api/permissions/rules" : "/api/permissions/rules/remove";
        const answer = await (words ? apiWritten("POST", path, { list, rule }, words) : api("POST", path, { list, rule }));
        ui.permissions = answer;
        ui.pendingRender = true;
        if (change === "add" && !answer.added) toast(`Already there: ${answer.rule}`);
      } catch (error) {
        if (loaded) ui.permissions = before;
        if (undo) undo();
        renderPermissions();
        throw new Error(`${change === "add" ? "Not added" : "Not removed"}: ${error.message}`, { cause: error });
      }
    }, null);
  });
  ruleChanges = changing.catch(() => {});
  holdLeave(changing);
  return changing;
}

// The new rule shows in its list in place; an empty box adds nothing (its Add is hidden).
function addRule(list) {
  const key = `perm:add:${list}`;
  const sent = ui.drafts.get(key) || "";
  const rule = sent.trim();
  if (!rule) return;
  clearDraft(key, sent);
  changeRules(list, rule, "add", { key, text: sent }, () => restoreDraft(key, sent));
}

// Undo of a rule changed from a blocked call's card (PERM-11): the server changes it back where it stands.
function changeRuleBack(list, rule, old) {
  const changing = ruleChanges.then(() => {
    const before = ui.permissions;
    const loaded = before && before !== "loading";
    if (loaded) {
      ui.permissions = { ...before, [list]: before[list].map((other) => other === rule ? old : other) };
      renderPermissions();
    }
    return run(`perm:edit:${list}:${++ui.sendCount}`, async () => {
      try {
        ui.permissions = await api("POST", "/api/permissions/rules/edit", { list, old: rule, rule: old });
        ui.pendingRender = true;
      } catch (error) {
        if (loaded) ui.permissions = before;
        renderPermissions();
        throw new Error(`Not changed back: ${error.message}`, { cause: error });
      }
    }, null);
  });
  ruleChanges = changing.catch(() => {});
  holdLeave(changing);
  return changing;
}

// `announce`: a toast with Undo (× in the panel), or none (Undo of a toast that added the rule).
function removeRule(list, rule, announce) {
  const removing = changeRules(list, rule, "remove", null, null);
  if (announce) toast("Removed", false, { label: "Undo", run: () => removing.then(() => changeRules(list, rule, "add", null, null)) }, rule);
}

function ruleList(list, rules) {
  if (!rules.length) return h("p", { class: "note", text: "Add a rule below" });
  return h("ul", { class: "rule-list" }, rules.map((rule) => h("li", {},
    list === "allow" ? h("code", { text: rule }) : h("span", { class: "rule-sentence", title: rule, text: rule }),
    iconButton("x", "Remove rule", { class: "button small ghost reveal", onclick: () => removeRule(list, rule, true) }))));
}

function ruleAdder(list) {
  const key = `perm:add:${list}`;
  const box = list === "allow"
    ? h("input", { class: "field", type: "text", "data-key": key, placeholder: RULE_LISTS[list].placeholder, spellcheck: "false", "aria-label": `New ${RULE_LISTS[list].name}` })
    : h("textarea", { class: "field", rows: 1, "data-key": key, placeholder: RULE_LISTS[list].placeholder, "aria-label": `New ${RULE_LISTS[list].name}` });
  box.value = ui.drafts.get(key) || "";
  const adder = h("div", { class: `rule-adder${box.value.trim() ? " filled" : ""}` });
  box.addEventListener("input", () => {
    ui.drafts.set(key, box.value);
    saveDrafts();
    adder.classList.toggle("filled", Boolean(box.value.trim()));
  });
  box.addEventListener("keydown", (event) => {
    const adds = list === "allow" ? event.key === "Enter" : event.key === "Enter" && (event.ctrlKey || event.metaKey);
    if (adds) { event.preventDefault(); addRule(list); }
  });
  adder.append(box, iconButton("plus", "Add rule", {
    class: "button small add-rule", "data-key": list === "allow" ? "Enter" : "Ctrl+Enter", onclick: () => addRule(list),
  }));
  return adder;
}

function permissionsBody(rules) {
  return [
    h("h3", { title: "Matching calls run without asking", text: "Tool rules" }),
    ruleList("allow", rules.allow),
    ruleAdder("allow"),
    h("h3", {
      title: rules.auto_keeps_defaults ? "Auto mode weighs these beside Claude Code's own" : "Auto mode weighs these",
      text: "Auto-mode sentences",
    }),
    !rules.auto_keeps_defaults && h("p", { class: "note fail-text", text: "These sentences replace Claude Code's built-in rules" }),
    ruleList("auto", rules.auto),
    ruleAdder("auto"),
  ];
}

function renderPermissions() {
  let body;
  if (ui.permissionsError) {
    body = [h("p", { class: "note fail-text", text: `Could not read the rules: ${ui.permissionsError}` }),
      h("div", { class: "panel-foot" }, iconButton("retry", "Retry", { class: "button small", onclick: loadPermissions }))];
  } else if (ui.permissions === null || ui.permissions === "loading") {
    body = Loading.waiting("permissions", Loading.ghost("w-60|w-80|w-70", "Loading the permission rules"));
  } else {
    body = Loading.arrive("permissions", permissionsBody(ui.permissions));
  }
  const loaded = ui.permissions && ui.permissions !== "loading";
  const summary = h("span", { class: "summary-text", title: loaded ? `Mode: ${ui.permissions.mode}` : null, text: "Permissions" });
  const el = disclosure("permissions", "more", summary, body, false);
  el.addEventListener("toggle", () => { if (el.open && ui.permissions === null && !ui.permissionsError) loadPermissions(); });
  fill("permissions", el);
}

// The page's link to the server: a dot in the header, grey while polls work, red when one failed,
// with the time in its tooltip. A newer server (UI-13) shows Reload; drafts live in the browser's
// storage, so a reload keeps them.
function renderStatus() {
  const live = document.getElementById("live");
  const when = (date) => date.toLocaleTimeString([], { ...TIME_FORMAT, second: "2-digit" });
  if (ui.error) {
    live.className = "live-dot fail";
    live.title = `Not updated since ${ui.lastOk ? when(ui.lastOk) : "the page loaded"}: ${ui.error}`;
    live.setAttribute("aria-label", "Not updating");
  } else if (ui.lastOk) {
    live.className = "live-dot ok";
    live.title = `Updated ${when(ui.lastOk)}`;
    live.setAttribute("aria-label", "Up to date");
  }
  document.getElementById("reload").hidden = !newerVersion();
}

function newerVersion() {
  return Boolean(ui.version && ui.state && ui.state.version && ui.state.version !== ui.version);
}
// errors.js marks the errors of a page older than the running Cockpit.
window.pageOutdated = newerVersion;

// A newer Cockpit shows the Reload button and waits for the user (UI-13): the page never reloads by
// itself, since the user may be reading it or working on a step in another tab. Reload brings back the
// open rows, toggles, the steps placed in the queue and the scroll position; drafts live in
// storage anyway.
function reloadKeepingPlace() {
  try {
    sessionStorage.setItem(REOPEN_KEY, JSON.stringify({
      expanded: [...ui.expanded], open: [...ui.open], closed: [...ui.closed], y: window.scrollY,
      placed: [...ui.placed],
    }));
  } catch {
    // Blocked storage: the page comes back with its default rows open.
  }
  location.reload();
}

const FIELDS = "input, textarea, select, [contenteditable]";
// The controls keyboard focus can rest on besides fields, and the parts of the page that hold them:
// a redraw finds a control again in the same part, by its name, or by its place when its name changed
// with the press (Answer becomes Close).
const CONTROLS = "a[href], button, summary, [tabindex='0']";
const HOMES = ["data-step", "data-item", "data-request", "data-session", "data-side-chat"];

function captureFocus() {
  const editor = window.CockpitEditor?.captureFocus();
  if (editor) return editor;
  const el = document.activeElement;
  if (el && el.matches(FIELDS)) {
    return { key: el.dataset.key || "", start: el.selectionStart, end: el.selectionEnd, length: el.value?.length, scroll: el.scrollTop };
  }
  if (!el || !el.matches(CONTROLS)) return null;
  const home = controlHome(el);
  if (!home) return null;
  return { control: el, home, name: controlName(el), place: homeControls(home).indexOf(el) };
}

// A selector for the part of the page a control sits in: its section and, inside it, its row or card.
function controlHome(el) {
  const section = el.closest("[id]");
  if (!section) return null;
  const row = el.closest(HOMES.map((name) => `[${name}]`).join(", "));
  const name = row && section.contains(row) && HOMES.find((attribute) => row.hasAttribute(attribute));
  const inside = name ? ` [${name}="${CSS.escape(row.getAttribute(name))}"]` : "";
  return `#${CSS.escape(section.id)}${inside}`;
}

function homeControls(home) {
  const el = document.querySelector(home);
  return el ? [...el.querySelectorAll(CONTROLS)] : [];
}

function controlName(el) {
  return `${el.tagName} ${el.getAttribute("aria-label") || el.textContent.trim()}`;
}

function refocus(el) {
  ui.refocusing = true;
  try {
    focusDraft(el, { preventScroll: true });
  } finally {
    ui.refocusing = false;
  }
}

function restoreFocus(focus) {
  if (!focus) return;
  if (focus.editor && focus.target.isConnected) { refocus(focus.target); return; }
  if (focus.control) {
    const active = document.activeElement;
    if (focus.control.isConnected || (active && active !== document.body)) return;
    const controls = homeControls(focus.home);
    const el = controls.find((control) => controlName(control) === focus.name) || controls[focus.place];
    if (el) refocus(el);
    return;
  }
  const el = focus.key && document.querySelector(`[data-key="${CSS.escape(focus.key)}"]`);
  if (!el) {
    // The box the user was typing in is gone; what they type next must not hit d/x/z.
    if (!document.activeElement || !document.activeElement.matches(FIELDS)) ui.focusLost = true;
    return;
  }
  if (el === document.activeElement) return;
  refocus(el);
  if (typeof focus.start === "number" && el.setSelectionRange) {
    // A caret at the end of the box stays at the end when the redraw brings it words: Take back's words come back
    // into the box the page focused while it was still empty, and the user adds to them there.
    const atEnd = focus.start === focus.end && focus.end === focus.length;
    const [start, end] = atEnd ? [el.value.length, el.value.length] : [focus.start, focus.end];
    try { el.setSelectionRange(start, end); } catch { /* select elements */ }
  }
  el.scrollTop = focus.scroll;
}

// Each entry: the page sections one renderer fills.
const SECTIONS = [
  [["pinned"], renderPinned],
  [["blocked"], renderBlocked],
  [["next", "queue"], renderSteps],
  // After the steps, which set the queue's order and the item the card shows.
  ...(SIDEBAR ? [[["sidebar"], renderSidebar]] : []),
  [["work"], renderWork],
  [["goals"], renderGoals],
  // The sidebar page leaves out the panels below the card: Patterns, Your time and Permissions.
  ...(SIDEBAR ? [] : [
    [["patterns"], renderPatterns],
    [["time"], renderTime],
    [["permissions"], renderPermissions],
  ]),
];

function renderSteps(state) {
  placeSteps(state.queue);
  const queue = withLeft(displayedQueue(state.queue), "queue");
  const ready = withLeft(state.ready_next, "ready");
  const snoozed = withLeft(state.snoozed, "snoozed");
  seeWords([...queue, ...ready, ...snoozed], queue.length && !queue[0].left ? queue[0].id : null);
  holdSteps({ queue, ready, snoozed });
  if (SIDEBAR) { renderSelected(state, { queue, ready, snoozed }); fill("queue"); return; }
  renderNext(queue[0], state);
  renderQueue(queue.slice(1), ready, snoozed, state);
}

// The user's press took Next up's step away, so they have moved on from the steps they opened before it: those give up
// the places they held on this page and Next up shows the queue's first step by rank, which `holdFollowing` then holds.
// A step whose timer runs in `state` (the state as drawn, with the user's pending pauses) or that holds the user's
// unsent draft keeps its place. Without this, a step the user once opened lower down climbed as the steps above it
// closed and took Next up over a higher one.
function releasePlaces(state) {
  const steps = new Map(STEP_LISTS.flatMap((name) => state[name]).map((step) => [step.id, step]));
  for (const id of ui.placed.keys()) if (!timerRuns(steps.get(id)) && !hasDraft(id)) ui.placed.delete(id);
}

// The holds that waited for the server to queue their steps: asked once it does, dropped once the page lets go of
// the place.
function askLaterHolds() {
  for (const [id, placedHere] of ui.holdLater) {
    if (!ui.placed.has(id)) ui.holdLater.delete(id);
    else askHold(id, placedHere);
  }
}

// When the user's press took Next up's step away (a close, Done, a hand-off, a snooze, an archive), the step shown
// in its place holds its place at once (NEXT-16). The timer's start would hold it too, but that waits for the page's
// focus and the next second's tick, and the press's answer may meanwhile put a newer, higher step over it.
function holdFollowing(took, after) {
  if (!took || !after || after.id === took.id || ui.placed.has(after.id)) return;
  holdPlace(after.id);
}

// A section the user types in, or one holding a picked part (holdsPick).
function holdsField(ids) {
  const el = document.activeElement;
  return holdsPick(ids) || ids.some((id) => el && el.matches(FIELDS) && document.getElementById(id).contains(el));
}

// A section holding the part the user picked for feedback, or attached in the open Add box. Every redraw leaves it
// as it is, the user's own presses' too (such as the task timer starting), since a redraw would replace the part and
// its outline, the box beside it and Wider, Narrower and Sideways would lose it.
function holdsPick(ids) {
  const picked = [ui.pick, ui.adding && ui.addPart].filter((pick) => pick && pick.el.isConnected).map((pick) => pick.el);
  return ids.some((id) => picked.some((part) => document.getElementById(id).contains(part)));
}

// Whether the user is pressing: from the press until its click has landed, or the press ended without one
// (releasePress), for at most PRESS_HOLD_MS.
function pressHeld() {
  return ui.pressedAt !== null && performance.now() - ui.pressedAt < PRESS_HOLD_MS;
}

// A redraw the user's press did not cause waits while a press is held or text is selected, as a poll's does
// (safeRender), and then draws as the change's own redraw would have. The timer a first selection on a card starts
// (startOpened) would otherwise redraw the card and drop that selection before the user can press Comment.
function renderAfterPress() {
  if (!pressHeld() && !textSelected()) {
    render(false);
    return;
  }
  ui.pendingRender = true;
  ui.actionRender = true;
  renderStatus();
}

function textSelected() {
  const selection = window.getSelection();
  return Boolean(selection && !selection.isCollapsed && selection.toString().trim());
}

function safeRender() {
  if (pressHeld() || textSelected()) {
    ui.pendingRender = true;
    renderStatus();
    return;
  }
  const keepFocused = !ui.actionRender;
  ui.actionRender = false;
  render(keepFocused);
}

// What a redraw draws from besides the user's choices on the page: the state less its
// clock and Claude usage (drawn on its own), the queue order the user sees, and whether Update is starting.
function drawnPrint(state) {
  return JSON.stringify([{ ...state, now: null, usage: null }, shownOrder(state.queue), ui.busy.has("update")]);
}

// `keepFocused`: a poll leaves alone the section the user is typing in or choosing
// from, and catches up on a later poll once they have left it. After their own
// action or click everything redraws, and focus returns to the same box.
function render(keepFocused) {
  if (!ui.state) return;
  // A start line render leaves in place (the open Add box's) draws again once the choices it was drawn with change,
  // also the first time the state arrives, so it shows what Send sends.
  const offers = startOffers(ui.state);
  for (const holder of document.querySelectorAll(".start-options")) {
    if (typeof holder.draw === "function" && holder.dataset.offers !== offers) drawStartLine(holder);
  }
  const state = drawn(ui.state);
  ui.drawnPrint = drawnPrint(state);
  ui.pendingRender = false;
  // Read before keepLeftSteps forgets the user's removal of a step that is gone from the drawn state.
  const took = ui.next && ui.removedByUser.has(ui.next.id) ? ui.next : null;
  if (took) releasePlaces(state);
  keepLeftSteps(state);
  Icons.setProjects(state.projects || []);
  const focus = captureFocus();
  keepFieldHeights();
  // The composers in sections this poll leaves alone, which no other section draws again (sessionList, sessionRow).
  ui.heldKeys = new Set(SECTIONS.filter(([ids]) => holdsPick(ids) || (keepFocused && holdsField(ids)))
    .flatMap(([ids]) => ids.flatMap((id) => [...document.querySelectorAll(`#${id} textarea[data-key]`)].map((field) => field.dataset.key))));
  // The first drawing takes the place of the ghosts the page opened with (UI-48).
  if (!ui.renderedAt) Loading.settle(document.querySelector(".layout"));
  // motion.js animates the steps and rows that this redraw adds, removes or moves.
  // The sections this redraw leaves as they were.
  const held = [];
  Motion.redraw(() => {
    renderCounts(state);
    renderUsage(state);
    renderSweep(state);
    fitHeader();
    renderBanner(state);
    for (const [ids, draw] of SECTIONS) {
      if (holdsPick(ids) || (keepFocused && holdsField(ids))) { ui.pendingRender = true; held.push(...ids); redrawAskBlocks(ids, state); continue; }
      parkFigures(ids);
      draw(state);
    }
    placeFigures();
    // Inside the redraw: a clamp class that changes after it would start motion's
    // animations on the blocked cards on every poll.
    markClippedCalls();
    // A page open beside a step's card keeps the new card in its column at the left (page-overlay.js).
    window.PageOverlay?.redrawn();
  }, keepFocused ? "poll" : "action");
  holdFollowing(took, ui.next);
  askLaterHolds();
  keepPulses();
  // views.js redraws the overview's lights on it.
  document.dispatchEvent(new Event("cockpit:redraw"));
  warmNext(state);
  renderStatus();
  markTopCardsSeen(state);
  restoreFocus(focus);
  ui.renderedAt = Date.now();
  if (!held.some(catchingUp)) caughtUp(ui.stateAskedAt);
  if (reopened.y !== null) {
    window.scrollTo(0, reopened.y);
    reopened.y = null;
  }
}

// A box's corner drag sets its inline height; draftArea draws the box again at that height.
function keepFieldHeights() {
  for (const area of document.querySelectorAll("textarea[data-key]")) {
    if (area.style.height) ui.fieldHeights.set(area.dataset.key, area.style.height);
  }
}

// A command clamped to its lines fades at the bottom; the class says it is cut.
function markClippedCalls() {
  for (const call of document.querySelectorAll(":is(#blocked, #next) pre.call")) {
    call.classList.toggle("clipped", !call.classList.contains("expanded") && call.scrollHeight > call.clientHeight + 1);
  }
}

function toggle(key) {
  if (ui.expanded.has(key)) ui.expanded.delete(key);
  else {
    ui.expanded.add(key);
    // Opening a step's row, by pointer or keyboard, holds its place (NEXT-16).
    if (key.startsWith("step:")) holdPlace(Number(key.slice("step:".length)));
  }
  render(false);
}

// A row that opens on a click anywhere on its head opens from the keyboard on its title: a tab stop
// that Enter or Space presses and that says whether the row is open. Only the title takes the role,
// since the head also holds buttons and links of its own.
function opens(el, key) {
  el.setAttribute("role", "button");
  el.tabIndex = 0;
  el.setAttribute("aria-expanded", String(ui.expanded.has(key)));
  el.addEventListener("keydown", (event) => {
    if ((event.key !== "Enter" && event.key !== " ") || event.target !== el || event.repeat) return;
    event.preventDefault();
    toggle(key);
  });
  return el;
}

// Events

// Single-key shortcuts act on the Next up step. They hold back while the user may
// still be typing: right after the user's text box vanished, or with an unsent reply
// or feedback on that step.
{
  const toastEl = document.getElementById("toast");
  toastEl.style.setProperty("--toast-ms", `${TOAST_MS}ms`);
  toastEl.addEventListener("pointerenter", holdToast);
  toastEl.addEventListener("focusin", holdToast);
  // After the pointer leaves or focus moves out; `runToast` waits while either still holds it.
  toastEl.addEventListener("pointerleave", () => setTimeout(runToast));
  toastEl.addEventListener("focusout", () => setTimeout(runToast));
}

function nextStepAction(key) {
  const step = ui.next;
  if (!step) return;
  if (nextVeiled()) {
    toast("Updating: Next up shows again in a moment", false);
    return;
  }
  if (ui.focusLost) {
    toast("Keys paused: click the page", true);
    return;
  }
  // A reason typed in Close item's box counts too: E would otherwise finish the task past it. On the
  // item's last step E is Task done, which sends the reply box's text as the last message, as its key says.
  const finishing = key === "e" && finishesItem(step, drawn(ui.state));
  const reply = `msg:step:${step.id}`;
  const unsent = [...(finishing ? [] : [reply]), `close:${step.id}`].some((draft) => (ui.drafts.get(draft) || "").trim());
  if (unsent) {
    toast("Keys paused: unsent text on this step", true, null, "Send or clear the reply or close reason on this step first.");
    return;
  }
  // With Task done hidden beside a closing answer (CONTROL-31), E presses that answer.
  const closer = finishing && !(ui.drafts.get(reply) || "").trim() && closingAnswer(step);
  if (closer) finishItem(step, "complete", closer.text, "", [], "", null, document.querySelector(`[data-step="${step.id}"] .answers .button.closes:not(.task-done)`));
  else if (finishing) taskDone(step, reply, document.querySelector(`[data-step="${step.id}"] .task-done`));
  else if (key === "e") act(step, "done");
  else if (key === "x") act(step, "dismiss");
  else if (key === "z") act(step, "snooze", { until: isoUtc(new Date(Date.now() + SNOOZE_KEY_HOURS * 3600e3)) });
}

document.addEventListener("keydown", (event) => {
  if (event.defaultPrevented || event.isComposing || event.keyCode === 229) return;
  if (document.querySelector("dialog[open]")) return;
  if (event.metaKey || event.ctrlKey || event.altKey || event.repeat) return;
  if (event.key === "Escape" && ui.picking) { stopPicking(); return; }
  if (event.key === "Escape" && ui.pick) { closePick(); return; }
  if (!document.getElementById("lightbox").hidden) {
    if (event.key === "Escape") closeLightbox();
    else if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      stepLightbox(event.key === "ArrowLeft" ? -1 : 1);
    }
    return;
  }
  if (event.target.closest && event.target.closest("input, textarea, select, [contenteditable]")) return;
  if (event.key !== "Escape" && (document.querySelector("details.menu[open]") || window.getSelection()?.toString())) return;
  if (event.key === "Escape" && ui.adding) { if (!closeStartPicks(document.getElementById("add-box"))) closeAddBox(); return; }
  // The Add dialog is modal: the page's keys wait until it closes.
  if (ui.adding && !ui.picking) return;
  if (event.key === "a") {
    event.preventDefault();
    openAddBox();
    return;
  }
  if (event.key === "f") {
    event.preventDefault();
    openAddWithPart();
    return;
  }
  if (ui.picking) return;
  if (event.key === "u") {
    event.preventDefault();
    update();
    return;
  }
  const step = ui.next;
  if (!step || !["r", "e", "x", "z"].includes(event.key)) return;
  // An opened view (center.js) covers Next up, so its keys wait as they do for a card off screen.
  if (document.documentElement.classList.contains("center-open")) {
    toast("R, E, X and Z act on Next up: close this view first", false);
    return;
  }
  // Next up's keys act on its card only while some of it is on screen: lower on the page, in the list of work or below
  // (VIEW-3), they would close a step the user cannot see.
  const card = document.getElementById("next")?.getBoundingClientRect();
  if (!card || card.bottom <= header.getBoundingClientRect().bottom || card.top >= window.innerHeight) {
    toast("R, E, X and Z act on Next up: scroll up to it", false);
    return;
  }
  if (nextVeiled()) {
    toast("Updating: Next up shows again in a moment", false);
    return;
  }
  if (event.key === "r") {
    const area = document.querySelector(`[data-key="msg:step:${step.id}"]`);
    if (area) { event.preventDefault(); focusDraft(area); }
    return;
  }
  if (!["e", "x", "z"].includes(event.key)) return;
  nextStepAction(event.key);
});

for (const type of ["pointerdown", "focusin"]) {
  document.addEventListener(type, () => { ui.focusLost = false; });
}

// A press or keyboard focus on a step in the user's lists clears its Updated mark.
function pressedStep(event) {
  if (ui.picking || ui.refocusing || !event.target.closest) return null;
  const card = event.target.closest("#next [data-step], #queue [data-step]");
  return card ? Number(card.dataset.step) : null;
}
for (const type of ["pointerdown", "focusin"]) {
  document.addEventListener(type, (event) => {
    const id = pressedStep(event);
    if (id === null) return;
    const step = stepOnPage(id);
    if (!step) return;
    // The mark goes after the click has landed.
    const news = ui.seenText.get(id) !== stepWords(step);
    ui.seenText.set(id, stepWords(step));
    if (news) { ui.actionRender = true; ui.pendingRender = true; }
  });
}
// Showing another item takes the timer along (TRACK-8): the step left stops without a notice, so no timer runs on a
// step out of sight, and it is not the user's pause, so the step starts again on its own when it shows once more
// (startOpened), as the shown item's step starts on its own card. A press elsewhere on the page leaves the timer
// running (TRACK-6).
function leaveTimedStep(timer) {
  if (ui.autoStarted === timer.step_id) ui.autoStarted = null;
  ui.leftStepId = timer.step_id;
  return sendTimer(timer.step_id, false);
}

// Pressing, opening (Enter or Space) or typing in a step of the queue, Next up or an open row, or focusing its
// reply box, holds its place (NEXT-16); moving the highlight over rows with the keyboard does not. Resorted and
// Take it are the user asking for the rank's order instead.
for (const type of ["pointerdown", "keydown", "focusin", "input"]) {
  document.addEventListener(type, (event) => {
    // A pick (app.js's picker) only points at the step; it does not work on it.
    if (ui.refocusing || ui.picking || !event.isTrusted || !event.target.closest || event.target.closest(".reorder, .take")) return;
    if (type === "keydown" && event.key !== "Enter" && event.key !== " ") return;
    if (type === "focusin" && !event.target.matches("textarea, input, select, [contenteditable]")) return;
    const card = event.target.closest("#next [data-step], #queue [data-step]");
    if (card) holdPlace(Number(card.dataset.step));
  }, true);
}

// Pressing, typing or focusing on a permission card is working on it.
for (const type of ["pointerdown", "focusin", "input"]) {
  document.addEventListener(type, (event) => {
    const card = !ui.refocusing && !ui.picking && event.target.closest && event.target.closest(":is(#blocked, #next) [data-request], #sidebar li.warn-row[data-request]");
    if (card) markRequestActive(Number(card.dataset.request));
  }, { passive: true });
}
setInterval(tickTimers, 1000);

// The header is solid; once the page scrolls under it, a hairline sets it off.
const header = document.getElementById("header");
function markScrolled() {
  header.classList.toggle("scrolled", window.scrollY > 0);
}
window.addEventListener("scroll", markScrolled, { passive: true });
markScrolled();

// The header's height as --top-h: it wraps to two lines on a narrow screen and grows with the Add box, and
// a section scrolled into view, the pick bar and toasts placed under the header use it (UI-20).
new ResizeObserver(() => document.documentElement.style.setProperty("--top-h", `${header.offsetHeight}px`)).observe(header);
// The header fits again when the window or a font changes its room; in the next frame, as a fit resizes
// what it observes.
let fitFrame = 0;
const headerRoom = new ResizeObserver(() => { cancelAnimationFrame(fitFrame); fitFrame = requestAnimationFrame(fitHeader); });
headerRoom.observe(document.querySelector(".top-inner"));

// While picking, a click picks the part instead of acting on it: it runs
// first (capture) and stops the click before any button, link or row sees it.
// The picker's own controls work as usual, and so does the Add box while it waits for its part.
// The feedback key can be picked too (SPEC FEEDBACK-7): while picking, the bar's Stop, Escape and F stop.
function pickControls() {
  return ui.picking === "add" ? ADD_CONTROLS : FEEDBACK_CONTROLS;
}

// A click within 8 px of the last one takes the next of that spot's guesses (pick.js); one elsewhere starts
// over with the part under it. A click from the keyboard (Enter or Space on a focused control) has no spot:
// it picks the control, and a second one on the same control takes the next guess.
// A click on the page that finds no part (the page's own margin) still acts on nothing.
document.addEventListener("click", (event) => {
  if (!ui.picking || event.target.closest(pickControls())) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  const keyboard = event.detail === 0;
  const first = keyboard ? Pick.of(event.target, PICKABLE) : Pick.at(event.clientX, event.clientY, PICKABLE, pickControls());
  if (!first) return;
  const last = ui.guess;
  const same = last && (keyboard ? last.target === event.target : !last.target && Math.hypot(event.clientX - last.x, event.clientY - last.y) <= 8);
  const again = same && last.parts.every((part) => part.isConnected);
  if (again) last.index = (last.index + 1) % last.parts.length;
  else ui.guess = { x: event.clientX, y: event.clientY, target: keyboard ? event.target : null, parts: Pick.guesses(first, PICKABLE, pickControls()), index: 0 };
  const el = ui.guess.parts[ui.guess.index];
  if (ui.picking === "add") attachPart(el, el, null);
  else choosePick(el, el, null);
}, true);

// While picking, a right click picks nothing and does nothing on the page: no menu of the page's own (the priority
// tag lowers its level on one) and no browser menu.
document.addEventListener("contextmenu", (event) => {
  if (!ui.picking || event.target.closest(pickControls())) return;
  event.preventDefault();
  event.stopImmediatePropagation();
}, true);

// The mark follows the pointer once a frame: a disabled control lets the pointer through, so moving onto one
// brings no pointerover. It listens on the window before the page's hold, which keeps pointer moves from the page.
let hoverFrame = 0;
addEventListener("pointermove", (event) => {
  if (!ui.picking || event.pointerType === "touch") return;
  cancelAnimationFrame(hoverFrame);
  hoverFrame = requestAnimationFrame(() => { if (ui.picking) markHover(Pick.at(event.clientX, event.clientY, PICKABLE, pickControls())); });
}, { capture: true, passive: true });

// While picking, a press or the pointer's arrival on the page reaches none of its own handlers; the click that
// follows picks (pick.js).
Pick.holdPage(() => Boolean(ui.picking), pickControls);

// The click's path is the one it started with, so a press that redraws its menu (and takes the pressed
// element out of the page) still counts as inside that menu.
document.addEventListener("click", (event) => {
  const path = event.composedPath();
  for (const open of document.querySelectorAll("details.menu[open]")) {
    if (!path.includes(open)) open.open = false;
  }
});

document.addEventListener("selectionchange", () => {
  const selection = window.getSelection();
  // Catch up once a text selection is released; a box the user is typing in moves the caret, which is no reason to redraw.
  const typing = document.activeElement && document.activeElement.matches(FIELDS);
  if (ui.pendingRender && selection && selection.isCollapsed && !typing) safeRender();
  requestAnimationFrame(placeQuoteButton);
});

// The Comment button shows once a selection is drawn, and pressing it keeps the selection.
document.body.append(quoteButton);
quoteButton.addEventListener("pointerdown", (event) => event.preventDefault());
quoteButton.addEventListener("click", () => {
  const quote = quotable();
  if (quote) quoteIntoReply(quote);
  else showQuoteButton(false);
});
document.addEventListener("pointerdown", (event) => {
  if (event.target === quoteButton || quoteButton.contains(event.target)) return;
  ui.selecting = true;
  showQuoteButton(false);
});
for (const type of ["pointerup", "pointercancel"]) {
  document.addEventListener(type, () => {
    ui.selecting = false;
    requestAnimationFrame(placeQuoteButton);
  });
}
window.addEventListener("resize", () => requestAnimationFrame(placeQuoteButton));

// A redraw swaps a section's buttons for new ones, so one between the user's press and the click
// drops the click: pressing a button moves focus out of a box, which lets a waiting redraw run.
// Redraws wait from the press until the click has landed, or the press ended without one; so does a toast that
// came while the user pressed in the shown one (`toast`).
function releasePress(delay) {
  clearTimeout(ui.releaseTimer);
  ui.releaseTimer = setTimeout(() => {
    ui.pressedAt = null;
    ui.pressedOn = null;
    if (ui.toastWaiting?.forPress) showWaitingToast();
    if (ui.pendingRender) safeRender();
  }, delay);
}
document.addEventListener("pointerdown", (event) => {
  clearTimeout(ui.releaseTimer);
  ui.pressedAt = performance.now();
  ui.pressedOn = event.target;
  if (ui.toastWaiting?.forPress) armToastPressTimer();
}, true);
// On a touch screen the click comes in its own task after the release.
document.addEventListener("pointerup", () => releasePress(PRESS_CLICK_WAIT_MS), true);
document.addEventListener("pointercancel", () => releasePress(0), true);
document.addEventListener("click", () => releasePress(0), true);

// Back from the background, the cards the page shows may have left meanwhile: the state takes seconds to come, and
// they would show until it does. When the state the page holds is older than a poll, the page asks the quick check
// which of its cards still stand, and catches up until a state asked after the return is drawn (SPEC UI-60). Meanwhile
// the open card, Blocked and waiting and the queue stay as drawn and in use: a yes from the quick check ends the
// catch-up unseen. A no veils the cards it names as gone, and only those: blurred, faded and inert under a thin
// moving line over each part, so a card that left cannot be read or pressed before it goes. Next up is veiled whole,
// and its keys wait, when its card left or the queue's top changed. When the quick check fails, or has not answered
// within CATCH_UP_GRACE_MS, every part is veiled and inert (no clicks, focus or keyboard rows, keyboard.js) until
// the answer or the state came. A part with a box the user types in stays in view; a state that fails to come shows
// the cards under the banner. The sidebar part is its queue group's list (sidebar.css). Throughout, the page's
// redraws are still (motion.js), so no card that left plays its exit.
const CATCH_UP_SECTIONS = ["next", "blocked", "queue", "sidebar"];

function startCatchUp() {
  if (!ui.state || Date.now() - ui.stateAskedAt < POLL_MS) return;
  // A return during a catch-up starts it again, with its own grace and parts.
  clearCatchUpMarks();
  ui.catchUpFrom = Date.now();
  ui.catchUpAll = false;
  ui.catchUpListed = null;
  // A box keeps every part its section redraws with (SECTIONS): Next up's box keeps the queue on the classic page.
  ui.catchUpKept = SECTIONS.map(([ids]) => ids).filter((ids) => ids.some((id) => CATCH_UP_SECTIONS.includes(id))
    && ids.every((id) => document.getElementById(id)) && holdsField(ids)).flat();
  document.body.dataset.catchUpKept = ui.catchUpKept.join(" ");
  document.body.classList.add("catching-up");
  ui.catchUpVeil = setTimeout(veilCatchUp, CATCH_UP_GRACE_MS);
  checkListed(ui.catchUpFrom, ui.state);
}

// Veils every part that waits: the page cannot say which of its cards still stand.
function veilCatchUp() {
  if (ui.catchUpFrom === null) return;
  ui.catchUpAll = true;
  document.body.classList.add("catch-up-noted", "catch-up-veiled");
  markCatchUp();
}

// The element that holds a catch-up part's cards: the section, or the sidebar's queue list.
function catchUpPart(id) {
  return id === "sidebar" ? document.querySelector("#sidebar .sidebar-group.queue .sidebar-list") : document.getElementById(id);
}

// Marks what waits busy and inert: every part once all are veiled, else the cards the quick check named as gone, and
// Next up whole when its card is one of them or the queue's top changed. A redraw may draw the cards anew, so each
// redraw marks again.
function markCatchUp() {
  const wait = (part) => {
    part.setAttribute("aria-busy", "true");
    part.inert = true;
  };
  if (ui.catchUpAll) {
    for (const id of CATCH_UP_SECTIONS) {
      const part = catchUpPart(id);
      if (part && catchingUp(id)) wait(part);
    }
    return;
  }
  if (!ui.catchUpListed) return;
  const { gone, topMoved } = goneCards(ui.state, ui.catchUpListed);
  const partOf = (el) => ["next", "blocked", "queue"].find((id) => el.closest(`#${id}`)) || "sidebar";
  const left = gone.filter((el) => catchingUp(partOf(el)));
  const next = document.getElementById("next");
  const nextWhole = Boolean(next) && catchingUp("next") && (topMoved || left.some((el) => next.contains(el)));
  for (const el of left) {
    // A card inside one that is veiled already is veiled with it.
    if ((nextWhole && next.contains(el)) || left.some((other) => other !== el && other.contains(el))) continue;
    el.classList.add("catch-up-gone");
    el.inert = true;
  }
  if (!nextWhole) return;
  wait(next);
  for (const card of next.children) card.classList.add("catch-up-gone");
}

function clearCatchUpMarks() {
  clearTimeout(ui.catchUpVeil);
  document.body.classList.remove("catching-up", "catch-up-noted", "catch-up-veiled");
  for (const id of CATCH_UP_SECTIONS) {
    const part = catchUpPart(id);
    if (!part) continue;
    part.removeAttribute("aria-busy");
    part.inert = false;
  }
  for (const el of document.querySelectorAll(".catch-up-gone")) {
    el.classList.remove("catch-up-gone");
    el.inert = false;
  }
}
document.addEventListener("cockpit:redraw", () => { if (ui.catchUpFrom !== null) markCatchUp(); });

// The quick answer to "is all this still there?": the ids the page lists, without the rest of the state, which takes
// seconds on a busy host. When nothing the page shows has gone and Next up shows the same step, the catch-up ends at
// once; the state follows and draws what is new. Otherwise the cards that left are veiled until the state came, and
// the others show again when the grace had veiled every part.
async function checkListed(from, shown) {
  let listed;
  try {
    listed = await api("GET", withOpened("/api/listed", {}));
  } catch {
    if (ui.catchUpFrom === from) veilCatchUp();
    return;
  }
  if (ui.catchUpFrom !== from) return;
  if (stillListed(shown, listed)) {
    endCatchUp();
    return;
  }
  clearCatchUpMarks();
  ui.catchUpAll = false;
  ui.catchUpListed = listed;
  document.body.classList.add("catching-up", "catch-up-noted");
  markCatchUp();
}

// A tab in the background keeps what it draws current, so that coming back finds nothing gone: every HIDDEN_CHECK_MS
// it asks the quick list, and the whole state only when a card it draws has gone or moved. A list the same as at the
// last such refresh asks nothing more, so a card the quick list does not cover (a sign-in warning) costs one state, not
// one a check. A redraw held for the user's selection waits for the user. The browser may run the check less often in
// a tab hidden for long (Chrome: once a minute after five minutes).
async function checkHidden() {
  if (!document.hidden || !ui.state || ui.refreshing || ui.hiddenCheck || ui.pendingRender) return;
  ui.hiddenCheck = true;
  try {
    const listed = await api("GET", withOpened("/api/listed", {}));
    const print = JSON.stringify(listed);
    if (document.hidden && print !== ui.hiddenListed && !stillListed(ui.state, listed)) {
      ui.hiddenListed = print;
      refresh();
    }
  } catch (error) {
    // The return asks again and shows the cards only once they are current.
    console.warn("Cockpit's background check of the cards failed:", error);
  } finally {
    ui.hiddenCheck = false;
  }
}

// Whether every card the page draws in the parts that wait still stands as drawn, by the quick answer `listed`: a
// step in the same group (a row the user kept after its step left, in ui.left, stays whatever the state says), a
// blocked call in the same list, a session still at its dialog, a Codex question still open, an item on All work or
// off it as before; the handed step Next up shows still with its agent; and Next up's queue top the same. A warning
// the quick answer does not cover (a sign-in, a wrong model, an account problem), a redraw the page put
// off, or a refusal the classic page counts under "got past without you" that went, keeps the wait for the whole
// state.
const REQUEST_LISTS = ["permission_requests", "settled_requests", "moved_past", "held_requests"];

function stillListed(shown, listed) {
  const { gone, topMoved, other } = goneCards(shown, listed);
  return !gone.length && !topMoved && !other;
}

// What the quick answer `listed` says changed in the parts that wait: the drawn cards that no longer stand (`gone`),
// whether Next up's queue top changed (`topMoved`), and whether something it names no card for needs the whole
// state (`other`).
function goneCards(shown, listed) {
  const ids = (list) => (list || []).map((entry) => entry.id);
  const groups = (lists) => new Map(Object.entries(lists).flatMap(([group, members]) => members.map((id) => [id, group])));
  const stepWas = groups({ queue: ids(shown.queue), ready_next: ids(shown.ready_next), snoozed: ids(shown.snoozed), with_agents: ids(shown.with_agents) });
  const stepIs = groups(listed.steps);
  const requestWas = groups(Object.fromEntries(REQUEST_LISTS.map((key) => [key, ids(shown[key])])));
  const requestIs = groups(Object.fromEntries(REQUEST_LISTS.map((key) => [key, listed[key]])));
  const waiting = new Set(listed.waiting_sessions);
  const codex = new Set(listed.codex_requests.map(String));
  const items = new Set(listed.items);
  const itemsWere = new Set(shown.items.map((item) => item.id));
  const drawn = (selector) => [...document.querySelectorAll(`:is(#next, #blocked, #queue, #sidebar .sidebar-group.queue) ${selector}`)]
    .filter((el) => !el.closest(".motion-ghost, .close-copy"));
  const stepStands = (id) => ui.left.has(id) || (stepIs.has(id) && stepIs.get(id) === stepWas.get(id));
  const requestStands = (id) => requestIs.has(id) && requestIs.get(id) === requestWas.get(id);
  const warningStands = (key) => {
    const [kind, ...rest] = key.split(":");
    const id = rest.join(":");
    if (kind === "request") return requestStands(Number(id));
    if (kind === "session") return waiting.has(id);
    if (kind === "codex") return codex.has(id);
    return null;
  };
  const warnings = drawn("[data-warning]").map((el) => [el, warningStands(el.dataset.warning)]);
  return {
    gone: [
      ...drawn("[data-step]").filter((el) => !stepStands(Number(el.dataset.step))),
      ...drawn("[data-request]").filter((el) => !requestStands(Number(el.dataset.request))),
      ...warnings.filter(([, stands]) => stands === false).map(([el]) => el),
      ...drawn(".waiting-card[data-session]").filter((el) => !waiting.has(el.dataset.session)),
      ...drawn("[data-codex]").filter((el) => !codex.has(el.dataset.codex)),
      ...drawn("[data-item]").filter((el) => items.has(Number(el.dataset.item)) !== itemsWere.has(Number(el.dataset.item))),
      ...drawn("[data-handed]").filter((el) => stepIs.get(Number(el.dataset.handed)) !== "with_agents"),
    ],
    topMoved: (shown.queue[0]?.id ?? null) !== (listed.steps.queue[0] ?? null),
    other: Boolean(ui.pendingRender) || warnings.some(([, stands]) => stands === null)
      || !(SIDEBAR || ids(shown.moved_past).every((id) => requestIs.get(id) === "moved_past")),
  };
}

// Whether the section with this id waits for the page to be current.
function catchingUp(id) {
  return ui.catchUpFrom !== null && CATCH_UP_SECTIONS.includes(id) && !ui.catchUpKept.includes(id);
}

// Whether Next up is veiled while the page catches up: its keys wait.
function nextVeiled() {
  return catchingUp("next") && Boolean(document.getElementById("next")?.inert);
}

function caughtUp(askedAt) {
  if (ui.catchUpFrom !== null && askedAt >= ui.catchUpFrom) endCatchUp();
}

function endCatchUp() {
  if (ui.catchUpFrom === null) return;
  ui.catchUpFrom = null;
  ui.catchUpAll = false;
  ui.catchUpListed = null;
  ui.catchUpKept = [];
  delete document.body.dataset.catchUpKept;
  clearCatchUpMarks();
  // flashes.js counts the cards from here.
  document.dispatchEvent(new Event("cockpit:caught-up"));
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    if (ui.state) writeStored(SEEN_KEY, ui.state.now);
  } else {
    ui.seenBefore = readStored(SEEN_KEY, ui.seenBefore);
    tickTimers();
    startCatchUp();
    refresh();
  }
});

// A connection that comes back shows the latest state at once, rather than at the next poll.
window.addEventListener("online", () => refresh());

// Actions a closed or reloaded page left behind went from this page (errors.js, CODE-5). One Cockpit did not
// take puts its words back into their box, open, and the toast says why; the redraw after the refresh shows it.
window.addEventListener("cockpit:not-sent", (event) => {
  const { words, message, reason } = event.detail;
  ui.pendingRender = true;
  const tip = words ? `Your words, not sent: ${words.text}` : "";
  if (!words || words.store !== DRAFTS_KEY) {
    toast(message, true, null, tip);
    return;
  }
  event.preventDefault();
  // Feedback from the side column's box, gone since FEEDBACK-9, comes back in the pick box.
  const key = words.key === "fb:general" ? PICK_KEY : words.key;
  // A pick box open on another part, or closed with words kept for the next part, keeps its own words and part;
  // these wait for it to close empty. They name no part.
  if (key === PICK_KEY && pickBoxTaken()) {
    toast(waitForPickBox(words.text, null, `Not sent from the page you left: ${reason}`, null).message, true, null, tip);
    return;
  }
  const back = putBack(key, words.text, words.choice, words.picks || null);
  if (back) ui.expanded.add(key);
  toast(`Not sent from the page you left: ${reason}. ${back ? "Your words are back in their box." : NOT_PUT_BACK}`, true, null, tip);
});
window.addEventListener("cockpit:sent-left", () => refresh());

// A Take back left behind went through: the words it stopped, and any written with it, go back into the step's reply
// box, which writes where the stopped message was going (errors.js reads it from the answer).
window.addEventListener("cockpit:left-answered", (event) => {
  const { words, back } = event.detail;
  if (words.store !== DRAFTS_KEY) return;
  event.preventDefault();
  ui.pendingRender = true;
  if (!restoreTarget(words.key, words)) {
    if (back) toast(`Taken back from the page you left. ${NOT_PUT_BACK}`, true, null, `Your words, not sent: ${back}`);
    return;
  }
  if (!back) return;
  if (!(ui.drafts.get(words.key) || "").includes(back)) restoreDraft(words.key, back);
  ui.expanded.add(words.key);
  toast("Taken back from the page you left. Your words are back in the step's reply box.");
});

// Another tab changed the stored drafts (the user typed or sent there, or it put back the words of an action
// not sent): every tab saves its drafts on each keystroke, so the stored ones are the latest, and this tab takes
// them, except the box the user types in here (a textarea or the Vim editor, both carry the draft's key).
// Words another tab put back into that box come as their own notice ("cockpit.put-back", errors.js) and join it
// at its top as an edit, so Send and the next save see them. An open pick box keeps its words too: they go with
// the part picked in this tab, so feedback put back from elsewhere, maybe on another part, stays in a notice.
function typingKey() {
  const focused = document.activeElement;
  return (focused && focused.dataset && focused.dataset.key) || null;
}

function takeStoredDrafts(typing) {
  const stored = new Map(readStored(DRAFTS_KEY, []));
  let changed = false;
  for (const key of new Set([...stored.keys(), ...ui.drafts.keys()])) {
    if (key === typing || (key === PICK_KEY && ui.pick) || (stored.get(key) || "") === (ui.drafts.get(key) || "")) continue;
    if (stored.get(key)) ui.drafts.set(key, stored.get(key));
    else ui.drafts.delete(key);
    changed = true;
  }
  return changed;
}

window.addEventListener("storage", (event) => {
  // A "To" choice another tab stored (words put back with the choice they were sent with, errors.js) joins
  // this tab's choices for boxes it has none for; a choice made here stays.
  if (event.key === TARGETS_KEY) {
    for (const [key, choice] of readStored(TARGETS_KEY, [])) if (!ui.choices.has(key)) ui.choices.set(key, choice);
    return;
  }
  if (event.key === DRAFTS_KEY) {
    if (takeStoredDrafts(typingKey()) && ui.state) safeRender();
    return;
  }
  // A new session's picks go between tabs: the stored ones, which every save writes, are the latest. Every start
  // line draws them again, also the one the user is in (render leaves the Add box's alone), so the line always
  // shows what Send would start; focus stays on the same control.
  if (event.key === STARTS_KEY) {
    ui.starts = new Map(readStored(STARTS_KEY, []));
    drawStartLines();
    if (ui.state) safeRender();
    return;
  }
  // The rule a draft changes in place (PERM-11) goes with the draft between tabs.
  // The box the user types in here keeps its own, as its draft does.
  if (event.key === REPLACING_KEY) {
    const typing = typingKey();
    const mine = ui.replacing.get(typing);
    ui.replacing = new Map(readStored(REPLACING_KEY, []));
    if (mine) ui.replacing.set(typing, mine);
    else ui.replacing.delete(typing);
    if (ui.state) safeRender();
    return;
  }
  if (event.key !== "cockpit.put-back" || !event.newValue) return;
  const { key, text, after } = JSON.parse(event.newValue);
  if (key === PICK_KEY && ui.pick && text.trim()) {
    toast(`Feedback from another page did not go through. It said: ${text.trim()}`, true, null);
    return;
  }
  const typing = typingKey();
  const area = key === typing ? document.querySelector(`textarea[data-key="${CSS.escape(key)}"]`) : null;
  // A box focused only after it took the stored drafts holds the words already.
  if (!area || !text.trim() || area.value === after) return;
  // The other drafts first, so the save the edit makes keeps them.
  takeStoredDrafts(typing);
  const top = area.value.trim() ? `${text.trim()}\n\n` : text.trim();
  if (window.CockpitEditor) window.CockpitEditor.prepend(area, top);
  else {
    area.value = top + area.value;
    area.dispatchEvent(new Event("input", { bubbles: true }));
  }
});

window.addEventListener("pagehide", () => {
  if (ui.state) writeStored(SEEN_KEY, ui.state.now);
});

// A phone's one-row action bar and the touch wording follow the screen as it changes.
for (const query of [PHONE, TOUCH]) query.addEventListener("change", () => render(false));

let resizeFrame = 0;
window.addEventListener("resize", () => {
  cancelAnimationFrame(resizeFrame);
  resizeFrame = requestAnimationFrame(markClippedCalls);
});

document.getElementById("update").addEventListener("click", update);
document.getElementById("resume-sweeps").addEventListener("click", resumeSweeps);
// Update's arrow turns once when the user presses it, then rests (UI-23).
document.getElementById("update").addEventListener("click", (event) => {
  if (event.currentTarget.disabled || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const arrow = event.currentTarget.querySelector(".icon");
  if (arrow) arrow.animate([{ transform: "rotate(0)" }, { transform: "rotate(360deg)" }], { duration: 640, easing: "cubic-bezier(0.23, 1, 0.32, 1)" });
});
document.getElementById("feedback-mode").addEventListener("click", openAddWithPart);
// On a phone the header has no room for the feedback button; the brand menu holds it instead (UI-20).
document.getElementById("feedback-menu").addEventListener("click", (event) => {
  event.target.closest("details").open = false;
  openAddWithPart();
});
document.getElementById("add").addEventListener("click", () => { if (ui.adding) closeAddBox(); else openAddBox(); });
document.getElementById("reload").addEventListener("click", reloadKeepingPlace);
// A click on the dark backdrop closes the full-size image; a click on the image does not.
document.getElementById("lightbox").addEventListener("click", (event) => { if (event.target.id === "lightbox") closeLightbox(); });
// A sideways swipe on a touch screen, at least 40 px and more sideways than up or down, shows the previous or next
// image.
document.getElementById("lightbox").addEventListener("touchstart", (event) => {
  ui.lightboxTouch = event.touches.length === 1 ? { x: event.touches[0].clientX, y: event.touches[0].clientY } : null;
}, { passive: true });
document.getElementById("lightbox").addEventListener("touchend", (event) => {
  if (ui.lightboxTouch === null || ui.lightbox === null) return;
  const sideways = event.changedTouches[0].clientX - ui.lightboxTouch.x;
  const upright = event.changedTouches[0].clientY - ui.lightboxTouch.y;
  ui.lightboxTouch = null;
  if (Math.abs(sideways) >= 40 && Math.abs(sideways) > 2 * Math.abs(upright)) stepLightbox(sideways < 0 ? 1 : -1);
});
window.addEventListener("focus", focusSignInCode);
document.getElementById("add-box").addEventListener("click", (event) => { if (event.target.id === "add-box") closeAddBox(); });
// A click beside the panel closes the time dialog as Escape does.
timeDialog().addEventListener("click", (event) => { if (event.target === timeDialog()) timeDialog().close(); });

// Images pasted onto a to-do (its row or its open editor) go onto that item; the add box and message boxes take their
// own (`composer`).
document.addEventListener("paste", (event) => {
  if (event.defaultPrevented) return;
  const zone = event.target.closest && event.target.closest("[data-drop-item]");
  const files = imageFiles(event.clipboardData);
  if (!zone || !files.length) return;
  event.preventDefault();
  attachImages(Number(zone.dataset.dropItem), files);
});

// Images dropped onto a to-do or the add box; the zone under the pointer shows that it takes them.
function dropZone(event) {
  if (event.defaultPrevented) return null;
  const carriesFiles = event.dataTransfer && [...event.dataTransfer.types].includes("Files");
  return carriesFiles && event.target.closest ? event.target.closest("[data-drop-item], #add-box") : null;
}
document.addEventListener("dragover", (event) => {
  const zone = dropZone(event);
  if (!zone) return;
  event.preventDefault();
  zone.classList.add("drop-over");
});
document.addEventListener("dragleave", (event) => {
  const zone = dropZone(event);
  if (zone && !zone.contains(event.relatedTarget)) zone.classList.remove("drop-over");
});
document.addEventListener("drop", (event) => {
  const zone = dropZone(event);
  if (!zone) return;
  event.preventDefault();
  zone.classList.remove("drop-over");
  const files = imageFiles(event.dataTransfer);
  if (zone.id === "add-box") stageImages(files);
  else attachImages(Number(zone.dataset.dropItem), files);
});

async function openKeyboardItem(itemId) {
  const item = ui.state?.items.find((entry) => entry.id === itemId);
  const shown = await window.openInWork?.(itemId, item?.group);
  if (shown) window.CockpitKeys?.highlight(shown);
}

// Work that waits for the user shows as its steps, not as a row (VIEW-18). This shows the item's first step: Next
// up, or its row in the queue, Ready next or Snoozed, opened. Null when the item has no step there.
function openItemStep(itemId) {
  if (SIDEBAR) return showInSidebar(itemId);
  if (!ui.state) return null;
  const state = drawn(ui.state);
  // Opening a step from search or the pinned list holds its place (NEXT-16).
  if (ui.next && ui.next.item_id === itemId) {
    holdPlace(ui.next.id);
    document.getElementById("next").scrollIntoView({ block: "start" });
    return document.querySelector(`#next [data-step="${ui.next.id}"]`);
  }
  for (const [steps, group] of [[state.queue, null], [state.ready_next, "ready-next"], [state.snoozed, "snoozed"]]) {
    const step = steps.find((one) => one.item_id === itemId);
    if (!step) continue;
    if (group) { ui.open.add(group); ui.closed.delete(group); } else ui.showAllQueue = true;
    ui.expanded.add(`step:${step.id}`);
    holdPlace(step.id);
    render(false);
    const row = document.querySelector(`#queue .row[data-step="${step.id}"]`);
    if (row) row.scrollIntoView({ block: "center" });
    return row;
  }
  return null;
}
window.openItemStep = openItemStep;

// A session's own composer (VIEW-3), where sessionHome puts it: under Task details on its item's step card, in its
// item's row in the list of work, or in the session's own row when no open item lists it.
async function openKeyboardSession(sessionId) {
  if (!ui.state) return;
  if (SIDEBAR) {
    await openSessionView(sessionId, openedFrom(null, sessionId));
    focusDraft(document.querySelector("#session-composer textarea"), { preventScroll: true });
    return;
  }
  revealSession(sessionId);
  const home = sessionHome(drawn(ui.state), sessionId);
  const key = `msg:side:${sessionId}`;
  ui.expanded.add(key);
  ui.showAllSessions = true;
  let place;
  if (home?.step) {
    ui.open.add(`task:${home.step.id}`);
    ui.closed.delete(`task:${home.step.id}`);
    render(false);
    place = openItemStep(home.item.id);
  } else if (home) {
    ui.expanded.add(`item:${home.item.id}`);
    render(false);
    place = await window.openInWork?.(home.item.id, home.item.group);
  } else {
    render(false);
    place = document.querySelector(`#work .row[data-session="${CSS.escape(sessionId)}"]`);
    for (let group = place?.closest("details"); group; group = group.parentElement.closest("details")) group.open = true;
  }
  const field = place?.querySelector(`textarea[data-key="${CSS.escape(key)}"]`);
  if (!field) return;
  field.closest(".composer").scrollIntoView({ block: "center" });
  focusDraft(field, { preventScroll: true });
}

function keyboardReply(row) {
  if (row.dataset.session) { openKeyboardSession(row.dataset.session); return; }
  // A warning takes no reply: it has only its fix and close buttons.
  if (row.dataset.warning) return;
  // A line in the sidebar: its item's card takes the reply.
  if (SIDEBAR && row.closest("#sidebar")) {
    phoneOpen(true);
    selectItem(Number(row.dataset.item));
    const card = document.querySelector("#next .next-card");
    if (card) keyboardReply(card);
    return;
  }
  if (row.dataset.item) {
    ui.expanded.add(`item:${row.dataset.item}`);
  } else if (row.dataset.step) {
    ui.expanded.add(`step:${row.dataset.step}`);
    holdPlace(Number(row.dataset.step));
  } else return;
  const attr = row.dataset.item ? "data-item" : "data-step";
  const value = row.getAttribute(attr);
  render(false);
  // A row or step card also holds composers to the item's sessions; R writes in the row's or step's own.
  const own = `[data-key="msg:${row.dataset.item ? "item" : "step"}:${CSS.escape(value)}"]`;
  focusDraft(document.querySelector(`[${attr}="${CSS.escape(value)}"] .composer textarea${own}`));
}

// New session sits in the list's head, which the filter's bar takes the place of, so it clears the filter.
function startKeyboardSession() {
  // The new-session box is on a phone's item screen.
  if (SIDEBAR) phoneOpen(true);
  if (ui.find) dropFind();
  ui.expanded.add("msg:new-session");
  render(false);
  const field = document.querySelector('#work textarea[data-key="msg:new-session"]');
  field?.closest(".composer").scrollIntoView({ block: "center" });
  focusDraft(field, { preventScroll: true });
}

window.CockpitKeys?.connect({
  state: () => ui.state,
  busy: () => Boolean(ui.picking || ui.pick),
  add: openAddBox,
  openItem: openKeyboardItem,
  openSession: openKeyboardSession,
  find: (text, closed, target) => setFind(text, closed, target, true),
  // What the filter would show for the words, for the menu's "Show matches on the page".
  findCount: (text) => {
    const words = wordsOf(text);
    if (!ui.state || !words) return null;
    const found = foundWork(ui.state, words);
    return { tasks: found.items.length, sessions: found.sessions };
  },
  // G P opens Priorities and inbox; G M sorts the work list by Your latest message.
  showSection: (id) => {
    if (id === "goals") openSettings("goals");
    else if (id === "messages") showMessages();
    else if (id === "failed") openFailures();
  },
  reply: keyboardReply,
  // T on a highlighted ready-next row (NEXT-36); false leaves the key alone on any other row.
  take: (row) => {
    const step = drawn(ui.state).ready_next.find((other) => other.id === Number(row.dataset.step));
    if (!step || ui.picking || ui.pick) return false;
    takeStep(step);
    return true;
  },
  actions: () => [
    { id: "add", title: "Add work", words: "create new task todo", keys: ["C"], run: openAddBox },
    { id: "update", title: "Update board", words: "refresh sync sweep", keys: ["U"], run: update },
    { id: "new-session", title: "Start a new agent session", words: "new codex claude conversation", run: startKeyboardSession },
    { id: "vim", title: `${window.CockpitEditor?.get() ? "Disable" : "Enable"} Vim editing`,
      words: "settings preferences editor prompts keybindings", run: () => window.CockpitEditor?.set(!window.CockpitEditor.get()) },
    ...(ui.next ? [
      { id: "done", title: "Mark Next up done", keys: ["E"], run: () => nextStepAction("e") },
      { id: "dismiss", title: "Dismiss Next up", keys: ["X"], run: () => nextStepAction("x") },
      { id: "snooze", title: "Snooze Next up for two hours", keys: ["Z"], run: () => nextStepAction("z") },
    ] : []),
  ],
});

// A mention of another item in a step's or a session's words opens that item (SPEC UI-74): its step or its row, as
// Search opens it; an item the page does not list, closed a while ago, shows through the filter with closed work,
// found by its id ("#10"). A press with a modifier key opens the page anew in a tab of its own, as any link does.
async function openMention(itemId) {
  let listed = ui.state && ui.state.items.find((item) => item.id === itemId);
  // An item a part keeps off the page is opened by its id: the next state holds it.
  if (!listed && ui.state && !ui.openedHidden.has(itemId)) {
    ui.openedHidden.add(itemId);
    const asked = ui.tick;
    refresh();
    while (ui.tick === asked || ui.refreshing) await new Promise((resolve) => setTimeout(resolve, 50));
    listed = ui.state.items.find((item) => item.id === itemId);
  }
  const shown = await window.openInWork?.(itemId, listed?.group);
  if (shown) { window.CockpitKeys?.highlight(shown); return; }
  setFind(`#${itemId}`, true, itemId, true);
}

// An item's summary or its session's latest status (`field`), with its mentions of other items as links (UI-74):
// the server's HTML, or the words as they are on an item the page has drawn before the server answered.
function summaryWords(item, field) {
  const html = item[`${field}_html`];
  return html === undefined ? item[field] : h("span", { html });
}

document.addEventListener("click", (event) => {
  const link = event.target.closest?.("a.item-ref");
  if (!link || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const match = link.getAttribute("href").match(/^\/#item\/(\d+)$/);
  if (!match) return;
  event.preventDefault();
  openMention(Number(match[1]));
});

// #item/<id>: a mention's link from another page, or opened in a tab of its own.
function followItem() {
  const match = location.hash.match(/^#item\/(\d+)$/);
  if (!match) return;
  history.replaceState(history.state, "", `${location.pathname}${location.search}`);
  const open = () => openMention(Number(match[1]));
  if (ui.state) open(); else document.addEventListener("cockpit:redraw", open, { once: true });
}

if (SIDEBAR) {
  registerCenterView("time", {
    title: "Your time",
    draw: () => { if (ui.time === null && !ui.timeError) loadTime(); else renderTime(); },
  });
}

// #time: "Your time" from the logo menu or a link, such as the one on the step that connects a Mac. The sidebar page
// opens it in the center layer; /classic opens its panel below the card.
function followTime() {
  if (location.hash !== "#time") return;
  history.replaceState(history.state, "", `${location.pathname}${location.search}`);
  document.querySelector(".brand-menu")?.removeAttribute("open");
  const open = SIDEBAR ? () => openCenter("time") : openEstimates;
  if (ui.state) open(); else document.addEventListener("cockpit:redraw", open, { once: true });
}

// The logo menu's entry, above the plain page for emergencies.
document.querySelector('.brand-menu a[href="/emergency"]')
  ?.before(h("a", { href: "#time" }, h("span", { class: "menu-label" }, icon("hourglass"), "Your time")));

followFind();
followItem();
followTime();
window.addEventListener("hashchange", followFind);
window.addEventListener("hashchange", followItem);
window.addEventListener("hashchange", followTime);
// A page closed over this one (NEXT-47): what the user sent, commented or chose there shows at once.
window.addEventListener("cockpit:page-closed", () => refresh());
// A link to a task in that layer, such as a briefing's mention of it, closed the layer to open the task here.
window.addEventListener("cockpit:open-item", (event) => openMention(event.detail));
// A comment left on a page shown beside its step's card shows above the card's reply box at once.
window.addEventListener("cockpit:page-changed", () => refresh());
// Words selected or a part picked on a page beside its step's card go into the card's reply box as a quote, with the
// cursor under it, as words selected in the card's own text do.
window.addEventListener("cockpit:page-quote", (event) => quoteIntoReply({ text: event.detail.text, key: `msg:step:${event.detail.stepId}` }));
// A choice saved from a page shows on its step's card: once the user stops choosing, and the layer, if one is
// open, redraws the page when it closes.
let choiceRefresh = null;
window.addEventListener("cockpit:choice-saved", () => {
  clearTimeout(choiceRefresh);
  choiceRefresh = setTimeout(() => {
    if (!document.querySelector("dialog.page-overlay[open]:not(.page-overlay-beside)")) refresh();
  }, 1000);
});
refresh();
setInterval(() => { if (!document.hidden) refresh(); }, POLL_MS);
document.querySelector(".brand-menu .menu-list form")?.before(zoneEntry);
reportZone();
setInterval(() => { if (!document.hidden) reportZone(); }, ZONE_REPORT_MS);
document.addEventListener("visibilitychange", () => { if (!document.hidden) reportZone(); });
setInterval(checkHidden, HIDDEN_CHECK_MS);
setInterval(() => { if (!document.hidden) refreshShownHistories(); }, 5000);
