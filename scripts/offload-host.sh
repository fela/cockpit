#!/usr/bin/env bash
# Prepare the offload host (SPEC BG-29): give it this machine's tool installs at the same paths, and the system
# packages the test suites use. Safe to run again; run it after updating bun, uv or the Playwright browsers here.
#
#   scripts/offload-host.sh HOST
set -euo pipefail

host=${1:?usage: scripts/offload-host.sh HOST}

# The same release, so the copied programs find the same system libraries.
here=$(. /etc/os-release && echo "$ID $VERSION_ID")
there=$(ssh "$host" '. /etc/os-release && echo "$ID $VERSION_ID"')
if [[ $here != "$there" ]]; then
  echo "$host runs $there and this machine runs $here; the copied tools need the same release." >&2
  exit 1
fi

# Python, git and the libraries the Playwright browsers load (Playwright's list for Ubuntu 24.04).
ssh "$host" 'sudo -n DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3.12 python3.12-venv git rsync \
  libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 libatspi2.0-0t64 libcairo2 libcups2t64 libdbus-1-3 libdrm2 \
  libgbm1 libglib2.0-0t64 libnspr4 libnss3 libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 \
  libxfixes3 libxkbcommon0 libxrandr2 libxrender1 fonts-liberation >/dev/null'

for folder in .bun .local/bin .local/share/uv .cache/ms-playwright; do
  if [[ -d $HOME/$folder ]]; then
    rsync -a --delete --mkpath "$HOME/$folder/" "$host:$HOME/$folder/"
  fi
done

# Every browser binary finds its libraries there.
missing=$(ssh "$host" 'for chrome in ~/.cache/ms-playwright/*/chrome-*/chrome*; do
  [[ -f $chrome && -x $chrome ]] && ldd "$chrome" | grep "not found"; done; true')
if [[ -n $missing ]]; then
  echo "Browser libraries missing on $host:" >&2
  echo "$missing" >&2
  exit 1
fi
ssh "$host" '~/.local/bin/uv --version && ~/.bun/bin/bun --version && python3.12 --version'
echo "$host is ready to run slow commands."
