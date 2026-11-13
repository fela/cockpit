#!/bin/sh
# Set up the second Claude account's config folder, ~/.claude-work (SPEC SESSION-17).
#
# The folder gets one symlink per entry the two accounts share, each pointing at the same entry in
# ~/.claude, and nothing else: Claude Code creates the rest (sign-in, .claude.json, daemon, jobs) on
# first use. projects is the exception: it is a bind mount of ~/.claude/projects, written to /etc/fstab,
# because Claude Code asks before every write through a symlink that leads outside a session's folders,
# which would stop every auto-memory write on the second account (SPEC PERM-15). An earlier symlink there, or
# a plain folder a failed run or boot left, is replaced by the mount. Keep the lists the same as
# SHARED_ENTRIES and MOUNTED_ENTRIES in src/cockpit/config.py; Cockpit checks these entries on every
# session listing and shows a broken one on its page.
#
# Running it again changes nothing, and it repairs a mount that failed at boot. It stops, changing
# nothing, when an entry is a real file or folder (for projects: a file, or another mount), a link to
# somewhere else, or when /etc/fstab already mounts something else there: fix that one by hand first.
# The mount needs sudo.
#
# Afterwards sign the work account in: CLAUDE_CONFIG_DIR=~/.claude-work claude, then /login.
set -eu
[ "$(uname -s)" = Linux ] || { echo "second-account.sh sets up a bind mount in /etc/fstab and runs on Linux only." >&2; exit 1; }

home_folder="$HOME/.claude"
work_folder="$HOME/.claude-work"
linked="settings.json skills plugins agents statusline-command.sh"
mounted="projects"
# nofail keeps a failed mount from stopping the boot; x-systemd.before keeps nofail from also dropping the
# order, so the mount is done before any user session (and so any Claude session) starts.
mount_options="bind,nofail,x-systemd.before=systemd-user-sessions.service"

# Move what folder $1 (relative, ending in /) of $2 holds into the same place in $3, where $2 and $3 are on one
# mount so mv renames. A folder both sides have is merged; any other name $3 already has gets the first free
# .from-mount-swap.N name beside it, so nothing is overwritten. $4 is $3's real path, for the messages.
move_across() {
  local rel="$1" from="$2" to="$3" real="$4" path name dest n
  for path in "$from/$rel"* "$from/$rel".[!.]* "$from/$rel"..?*; do
    if [ ! -e "$path" ] && [ ! -L "$path" ]; then continue; fi
    name=${path#"$from/"}
    if [ -d "$path" ] && [ ! -L "$path" ] && [ -d "$to/$name" ] && [ ! -L "$to/$name" ]; then
      move_across "$name/" "$from" "$to" "$real"
      rmdir "$path" 2>/dev/null || printf '%s\n' "something wrote into $name again during the move: rerun this script" >&2
    else
      dest="$name"
      n=0
      # mv -n renames only when the name is free at that instant, so a file a session creates in between stays;
      # the entry is then still here and tries the next name.
      while [ -e "$path" ] || [ -L "$path" ]; do
        if [ "$n" -gt 0 ]; then dest="$name.from-mount-swap.$n"; fi
        n=$((n + 1))
        if [ -e "$to/$dest" ] || [ -L "$to/$dest" ]; then continue; fi
        mv -n -T "$path" "$to/$dest" || true
        if { [ -e "$path" ] || [ -L "$path" ]; } && [ ! -e "$to/$dest" ] && [ ! -L "$to/$dest" ]; then
          printf '%s\n' "could not move $path to $real/$dest; rerun this script once that is fixed" >&2
          exit 1
        fi
      done
      if [ "$dest" = "$name" ]; then
        printf '%s\n' "moved $name, written while the folder was not mounted, into $real"
      else
        printf '%s\n' "$real/$name exists on both sides; the copy written while not mounted is $real/$dest" >&2
      fi
    fi
  done
}

for name in $linked $mounted; do
  entry="$work_folder/$name"
  if [ -L "$entry" ]; then
    if [ "$(readlink "$entry")" != "$home_folder/$name" ]; then
      printf '%s\n' "$entry links to $(readlink "$entry"), not $home_folder/$name; fix it by hand" >&2
      exit 1
    fi
  elif [ -e "$entry" ]; then
    case " $mounted " in
      *" $name "*)
        if mountpoint -q "$entry"; then
          if [ "$entry" -ef "$home_folder/$name" ]; then continue; fi
          printf '%s\n' "$entry is a mount of something other than $home_folder/$name; unmount it by hand" >&2
          exit 1
        fi
        if [ -d "$entry" ]; then continue; fi ;;
    esac
    printf '%s\n' "$entry is a real file or folder, not a link to $home_folder/$name; move it aside first" >&2
    exit 1
  fi
done
for name in $mounted; do
  entry="$work_folder/$name"
  if [ -L "$home_folder" ] || [ -L "$home_folder/$name" ] || mountpoint -q "$home_folder/$name"; then
    printf '%s\n' "$home_folder/$name is a link or a mount; this script expects a plain folder there" >&2
    exit 1
  fi
  other=$(awk -v source="$home_folder/$name" -v dest="$entry" '$1 !~ /^#/ && $2 == dest && $1 != source { print $1 }' /etc/fstab)
  if [ -n "$other" ]; then
    printf '%s\n' "/etc/fstab already mounts $other on $entry; remove that row by hand" >&2
    exit 1
  fi
done

mkdir -p "$work_folder"
chmod 700 "$work_folder"
for name in $linked; do
  entry="$work_folder/$name"
  if [ ! -L "$entry" ]; then
    ln -s "$home_folder/$name" "$entry"
    printf '%s\n' "linked $entry -> $home_folder/$name"
  fi
  if [ ! -e "$home_folder/$name" ]; then
    printf '%s\n' "note: $home_folder/$name does not exist yet; the link waits for it"
  fi
done
for name in $mounted; do
  entry="$work_folder/$name"
  target="$home_folder/$name"
  mkdir -p "$target"
  if ! awk -v source="$target" -v dest="$entry" '$1 == source && $2 == dest { found = 1 } END { exit !found }' /etc/fstab; then
    # A last row without its newline would swallow the new one.
    if [ -n "$(tail -c 1 /etc/fstab)" ]; then echo | sudo tee -a /etc/fstab >/dev/null; fi
    printf '%s %s none %s 0 0\n' "$target" "$entry" "$mount_options" | sudo tee -a /etc/fstab >/dev/null
    printf '%s\n' "added the $entry mount to /etc/fstab"
  fi
  if ! mountpoint -q "$entry"; then
    # Sessions write here all the time, so the folder is without its link only between these commands. A write
    # in that gap lands in the plain folder under the mount; the step after mounting moves it across.
    if [ -L "$entry" ]; then rm "$entry"; fi
    mkdir -p "$entry"
    # After the link is gone: systemd resolves links in fstab paths, so a reload before would name the wrong mount.
    sudo systemctl daemon-reload
    sudo mount "$entry"
    if ! mountpoint -q "$entry" || [ ! "$entry" -ef "$target" ]; then
      printf '%s\n' "$entry did not end up as a bind mount of $target; check /etc/fstab and findmnt $entry" >&2
      exit 1
    fi
    printf '%s\n' "mounted $target on $entry"
  fi

  # The plain folder under the mount holds what sessions wrote while it was not mounted: in the swap above, or
  # while a mount had failed at boot. A non-recursive bind of $HOME shows it and the shared folder on one mount,
  # so each file moves across by rename: nothing is copied or deleted, and a session still writing to a file
  # keeps writing to the moved one. This runs on every run, so a rerun finishes a move that stopped.
  peek=$(mktemp -d)
  sudo mount --bind "$HOME" "$peek"
  under="$peek/.claude-work/$name"
  shared="$peek/.claude/$name"
  # Both paths on the bind itself, or a link would lead out of it and mv would copy instead of renaming.
  if [ "$under" -ef "$target" ] || [ ! "$shared" -ef "$target" ] ||
    [ "$(findmnt -n -o TARGET -T "$under")" != "$peek" ] || [ "$(findmnt -n -o TARGET -T "$shared")" != "$peek" ]; then
    sudo umount "$peek"
    rmdir "$peek"
    printf '%s\n' "$HOME's bind does not show the folder under $entry beside $target; move its files by hand" >&2
    exit 1
  fi
  move_across "" "$under" "$shared" "$target"
  sudo umount "$peek"
  rmdir "$peek"
done
printf '%s\n' "done; sign the work account in: CLAUDE_CONFIG_DIR=$work_folder claude, then /login"
