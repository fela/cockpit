"""The public demo is built from this code (demo/build.py), so a call the build relies on that changed fails here."""

import subprocess
import sys
from pathlib import Path

BUILD = Path(__file__).resolve().parents[1] / "demo" / "build.py"


def test_the_demo_builds(tmp_path):
    out = tmp_path / "demo.html"
    result = subprocess.run(
        [sys.executable, str(BUILD), "--out", str(out)], capture_output=True, text=True, timeout=300, check=False
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert out.is_file()
