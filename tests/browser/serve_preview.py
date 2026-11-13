"""Serve synthetic Cockpit records without write delays, for screenshots and browser checks.

Uses serve_slow.py's temporary database, test settings, fake sessions and cookie.json.
Static files are read from this checkout on every request.

Usage: .venv/bin/python tests/browser/serve_preview.py <temporary-work-folder>
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import serve_slow

serve_slow.SLOW_SECONDS = 0

if __name__ == "__main__":
    serve_slow.main(Path(sys.argv[1]).resolve())
