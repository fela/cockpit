#!/bin/sh
# Runs scripts/second-account.sh against a scratch HOME and a fake /etc/fstab, inside a private mount
# namespace (run as: sudo unshare -m --propagation private sh scripts/second-account-test.sh). Nothing here reaches the real
# fstab or the real ~/.claude folders.
set -u
script=$(dirname "$(readlink -f "$0")")/second-account.sh
base=$(mktemp -d)
fake_home="$base/home"
fstab="$base/fstab"

reset() {
  umount "$fake_home/.claude-work/projects" 2>/dev/null
  rm -rf "$fake_home"
  mkdir -p "$fake_home/.claude/projects/p1" "$fake_home/.claude-work" "$fake_home/.claude/skills" "$fake_home/.claude/plugins" "$fake_home/.claude/agents"
  echo old > "$fake_home/.claude/projects/p1/a.jsonl"
  touch "$fake_home/.claude/settings.json" "$fake_home/.claude/statusline-command.sh"
  # No final newline, which an append must not swallow.
  printf 'LABEL=root / ext4 defaults 0 1' > "$fstab"
}
run() {
  HOME="$fake_home" sh "$script"
  echo "exit $?"
}
show() {
  echo "--- fstab"; cat "$fstab"; echo "--- findmnt"; findmnt -n -o SOURCE,TARGET "$fake_home/.claude-work/projects" || echo "not mounted"
  echo "--- files seen through the work path"; (cd "$fake_home/.claude-work/projects" && find . -type f | sort)
}

mount --bind "$fstab" /etc/fstab 2>/dev/null || { touch "$fstab"; mount --bind "$fstab" /etc/fstab; }

echo "=== 1. symlink today -> mount"
reset
ln -s "$fake_home/.claude/projects" "$fake_home/.claude-work/projects"
for n in settings.json skills plugins agents statusline-command.sh; do ln -s "$fake_home/.claude/$n" "$fake_home/.claude-work/$n"; done
run; show

echo "=== 2. run again: nothing changes"
run; show

echo "=== 3. failed boot: plain folder with a session's writes, fstab row present"
umount "$fake_home/.claude-work/projects"
mkdir -p "$fake_home/.claude-work/projects/p1" "$fake_home/.claude-work/projects/p2"
echo during > "$fake_home/.claude-work/projects/p1/a.jsonl"
echo new > "$fake_home/.claude-work/projects/p2/b.jsonl"
run; show
echo "--- under the mount (should be empty)"
mkdir -p "$base/peek"; mount --bind "$fake_home/.claude-work" "$base/peek"; find "$base/peek/projects" -mindepth 1; umount "$base/peek"

echo "=== 3b. a session keeps writing to a file under the mount; a name collides with an earlier saved copy"
umount "$fake_home/.claude-work/projects"
mkdir -p "$fake_home/.claude-work/projects/p1/memory"
echo note > "$fake_home/.claude-work/projects/p1/memory/n.md"
ln -s /nowhere "$fake_home/.claude-work/projects/p1/link"
echo hidden > "$fake_home/.claude-work/projects/p1/.hidden"
echo again > "$fake_home/.claude-work/projects/p1/a.jsonl"
exec 9>>"$fake_home/.claude-work/projects/p1/a.jsonl"
run
echo "appended after the move" >&9
exec 9>&-
show
echo "--- a.jsonl.from-mount-swap.1 and .2 (the second holds the later append)"
cat "$fake_home/.claude/projects/p1/a.jsonl.from-mount-swap.1"; echo ..; cat "$fake_home/.claude/projects/p1/a.jsonl.from-mount-swap.2"
ls -la "$fake_home/.claude/projects/p1/link"
echo "--- under the mount (should be empty)"
mount --bind "$fake_home/.claude-work" "$base/peek"; find "$base/peek/projects" -mindepth 1; umount "$base/peek"

echo "=== 3c. a commented fstab row for the same path does not block"
printf '#/old %s none bind 0 0\n' "$fake_home/.claude-work/projects" >> "$fstab"
run

echo "=== 4. conflicting fstab row"
reset
ln -s "$fake_home/.claude/projects" "$fake_home/.claude-work/projects"
printf '\n/elsewhere %s none bind 0 0\n' "$fake_home/.claude-work/projects" >> "$fstab"
run; ls -la "$fake_home/.claude-work/projects"

echo "=== 5. the shared folder is itself a link elsewhere: refused before anything moves"
reset
mkdir -p "$base/elsewhere"
rm -rf "$fake_home/.claude/projects"
ln -s "$base/elsewhere" "$fake_home/.claude/projects"
mkdir -p "$fake_home/.claude-work/projects"
echo x > "$fake_home/.claude-work/projects/x"
for n in settings.json skills plugins agents statusline-command.sh; do ln -s "$fake_home/.claude/$n" "$fake_home/.claude-work/$n"; done
run
umount "$fake_home/.claude-work/projects" 2>/dev/null
echo "--- the plain folder's file is still there:"; ls "$fake_home/.claude-work/projects"
rm -rf "$base/elsewhere"

umount "$fake_home/.claude-work/projects" 2>/dev/null
umount /etc/fstab
rm -rf "$base"
