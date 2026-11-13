#!/bin/bash
# Installs Cockpit's activity recorder on this Mac (SPEC TRACK-9), or removes it.
#
#   curl -fsSL __ORIGIN__/activity/install.sh | bash -s -- CODE       installs; Cockpit's Your time shows this line
#   curl -fsSL __ORIGIN__/activity/install.sh | bash -s -- uninstall  removes everything it installed
#
# CODE works once, for 15 minutes: the installer trades it with Cockpit for this Mac's key.
#
# Once a minute it sends Cockpit the app in front, the site's host when Chrome, Brave, Edge or Arc is in front
# (never the full address or the page title), and whether the keyboard or mouse went untouched for the last minute.
# A minute in a private window is not recorded at all. Safari and Firefox show as the app only: neither tells a
# script whether its window is private. Everything it keeps on the Mac is readable by this user only.
set -euo pipefail
umask 077

LABEL="com.cockpit.activity"
DIR="$HOME/Library/Application Support/Cockpit"
HEADER="$DIR/activity-header"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

if [ "$(uname)" != "Darwin" ]; then
  echo "This recorder runs on a Mac only." >&2
  exit 1
fi

# Stops the recorder and waits until launchd no longer has it; when it still does, says $1 and fails.
unload() {
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  for _ in 1 2 3 4 5; do
    launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || return 0
    sleep 1
  done
  echo "Could not stop the recorder that runs now. $1" >&2
  exit 1
}

if [ "${1:-}" = "uninstall" ]; then
  unload "Nothing was removed. Restart the Mac and run this line again."
  # Cockpit stops taking this Mac's key; the minutes it has stay there.
  told=000
  if [ -f "$HEADER" ]; then
    told="$(curl -sS --max-time 20 -o /dev/null -w '%{http_code}' -H @"$HEADER" -X POST \
      "__ORIGIN__/api/activity/disconnect")" || told=000
  fi
  rm -f "$PLIST"
  rm -rf "$DIR"
  echo "Removed Cockpit's activity recorder from this Mac."
  case "$told" in
    200 | 401) echo "Cockpit no longer takes its key; the minutes Cockpit has stay there." ;;
    *) echo "Could not tell Cockpit (answer $told), so it still takes this Mac's key until another Mac line is used." >&2 ;;
  esac
  exit 0
fi

NEW_LINE="In Cockpit's Your time, press New key (or Connect a Mac) and paste the new line."
NEW_LINE_LATER="press New key in Cockpit's Your time and paste the new line."
CODE="${1:-}"
case "$CODE" in
  "" | *[!A-Za-z0-9_-]*)
    echo "This line has no code. $NEW_LINE" >&2
    exit 1 ;;
esac

mkdir -p "$DIR" "$HOME/Library/LaunchAgents"
chmod 700 "$DIR"
chmod -R go-rwx "$DIR"
# The code is traded for this Mac's key, which Cockpit sends once; the key before it stops working now. curl reads the
# code from its input and the key's header from a file, and the answer goes to a file this user alone can read, so
# neither shows in Terminal or the list of running programs.
answer="$(printf '{"code":"%s"}' "$CODE" | curl -sS --max-time 20 -o "$DIR/claim.json" -w '%{http_code}' \
  -H "Content-Type: application/json" --data @- "__ORIGIN__/api/activity/claim")" || answer=000
case "$answer" in
  200) ;;
  401)
    rm -f "$DIR/claim.json"
    echo "This line was used already or is over 15 minutes old. $NEW_LINE" >&2
    exit 1 ;;
  *)
    rm -f "$DIR/claim.json"
    echo "Could not reach Cockpit (answer $answer). Check this Mac is online, then run this line again." >&2
    exit 1 ;;
esac
KEY="$(sed -n 's/^{"key":"\([A-Za-z0-9_-]*\)"}$/\1/p' "$DIR/claim.json")"
rm -f "$DIR/claim.json"
if [ -z "$KEY" ]; then
  echo "Cockpit's answer held no key. $NEW_LINE" >&2
  exit 1
fi
# The key before no longer works, so the new one goes in place first: a recorder that runs now reads it each minute.
printf 'Authorization: Bearer %s\n' "$KEY" > "$HEADER"

unload "It goes on with the new key. To update it, restart the Mac, then $NEW_LINE_LATER"
rm -f "$DIR/activity-key"
cat > "$DIR/activity.sh" <<'RECORDER'
#!/bin/bash
# Cockpit's activity recorder: launchd runs it once a minute. Written by Cockpit's installer.
set -u
umask 077
ORIGIN="__ORIGIN__"
DIR="$HOME/Library/Application Support/Cockpit"
QUEUE="$DIR/queue"
PAUSED="$DIR/paused"
LOG="$DIR/activity.log"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$LOG"; }

# Text as a JSON string: control characters dropped, backslashes and quotes escaped.
json() { printf '%s' "$1" | tr -d '\000-\037' | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }

# The URL of the browser's front tab, or "private" for a private window. Fails when the browser cannot be asked.
front_url() {
  case "$1" in
    "Google Chrome" | "Brave Browser" | "Microsoft Edge")
      osascript -e 'with timeout of 5 seconds' -e "tell application \"$1\"" \
        -e 'if mode of front window is "incognito" then return "private"' \
        -e 'get URL of active tab of front window' -e 'end tell' -e 'end timeout' 2>/dev/null ;;
    Arc)
      osascript -e 'with timeout of 5 seconds' -e 'tell application "Arc"' \
        -e 'if incognito of front window then return "private"' \
        -e 'get URL of active tab of front window' -e 'end tell' -e 'end timeout' 2>/dev/null ;;
  esac
}

record_minute() {
  local minute idle app url host away site
  minute="$(date -u +%Y-%m-%dT%H:%M)"
  # Nanoseconds since the last key press or mouse move. Unknown, the minute is left out rather than called active.
  idle="$(ioreg -c IOHIDSystem | awk '/HIDIdleTime/ {print $NF; exit}')"
  case "$idle" in
    "" | *[!0-9]*) log "could not read the idle time (${idle:-nothing}); left this minute out"; return ;;
  esac
  app="$(lsappinfo info -only name "$(lsappinfo front)" 2>/dev/null | sed -n 's/.*"LSDisplayName"="\(.*\)".*/\1/p')"
  [ -n "$app" ] || app="unknown"
  url=""
  case "$app" in
    "Google Chrome" | "Brave Browser" | "Microsoft Edge" | Arc)
      # A private window's minute is not recorded at all, idle or not, and neither is one that cannot be checked.
      if ! url="$(front_url "$app")"; then
        log "could not ask $app for its front tab (Automation allowed for it?); left this minute out"
        return
      fi
      [ "$url" != private ] || return ;;
  esac
  away=false
  if [ "$idle" -ge 60000000000 ]; then away=true; fi
  case "$app" in loginwindow | ScreenSaverEngine) away=true ;; esac
  site=null
  if [ "$away" = false ]; then
    host="$(printf '%s' "$url" | sed -nE 's#^[Hh][Tt][Tt][Pp][Ss]?://([^/?#@]*@)?([^/:?#]+).*#\2#p' \
      | tr 'A-Z' 'a-z' | sed 's/^www\.//')"
    case "$host" in
      "" | *[!a-z0-9.-]*) ;;
      *) site="\"$host\"" ;;
    esac
  fi
  printf '{"minute":"%s","app":"%s","site":%s,"away":%s}\n' "$minute" "$(json "$app")" "$site" "$away" >> "$QUEUE"
  tail -n 1440 "$QUEUE" > "$QUEUE.tmp" && mv "$QUEUE.tmp" "$QUEUE"
}

send() {
  local body code
  if [ -f "$PAUSED" ] || [ ! -s "$QUEUE" ]; then
    body='{"minutes":[]}'
  else
    body="{\"minutes\":[$(paste -s -d, "$QUEUE")]}"
  fi
  code="$(printf '%s' "$body" | curl -sS --max-time 20 -o "$DIR/reply" -w '%{http_code}' \
    -H @"$DIR/activity-header" -H "Content-Type: application/json" \
    --data-binary @- "$ORIGIN/api/activity/minutes" 2>>"$LOG")" || code=000
  case "$code" in
    200)
      rm -f "$QUEUE"
      # Cockpit refuses a bad minute on its own and stores the rest.
      grep -q '"refused":0' "$DIR/reply" || log "Cockpit refused some minutes: $(cat "$DIR/reply")"
      if grep -q '"paused":true' "$DIR/reply"; then touch "$PAUSED"; else rm -f "$PAUSED"; fi ;;
    400 | 422)
      # Cockpit refused the whole send; set it aside, so it can be looked at and later minutes still go.
      log "Cockpit refused the send ($code): $(cat "$DIR/reply"); kept it in queue.refused"
      mv "$QUEUE" "$QUEUE.refused" 2>/dev/null ;;
    401)
      log "Cockpit no longer takes this key: press Connect a Mac there and run its line again; keeping the minutes" ;;
    *)
      log "could not reach Cockpit ($code); keeping the minutes for the next try" ;;
  esac
}

[ -f "$PAUSED" ] || record_minute
send
if [ -f "$LOG" ]; then tail -n 200 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"; fi
RECORDER

# The plist is XML, so a home path with & or < in it is escaped there.
XML_DIR=$(printf '%s' "$DIR" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g')
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$XML_DIR/activity.sh</string></array>
  <key>StartInterval</key><integer>60</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$XML_DIR/activity.log</string>
  <key>StandardErrorPath</key><string>$XML_DIR/activity.log</string>
</dict>
</plist>
PLIST
chmod 600 "$PLIST"

launchctl bootstrap "$DOMAIN" "$PLIST"
echo "Cockpit's activity recorder runs now, once a minute."
echo "macOS may ask once per browser to let it read the front tab's address: press Allow."
echo "To remove it: curl -fsSL __ORIGIN__/activity/install.sh | bash -s -- uninstall"
