"""Run the control-use browser regression when Playwright is configured."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not os.environ.get("PLAYWRIGHT_MODULE"), reason="set PLAYWRIGHT_MODULE to run browser checks")
def test_control_use_failure_is_stored():
    node = shutil.which("node")
    assert node is not None, "Node is required for browser checks"
    root = Path(__file__).resolve().parents[1]
    done = subprocess.run(
        ["nice", "-n", "19", node, "tests/browser/control_use_errors.cjs"],
        cwd=root, capture_output=True, text=True, check=False, timeout=120,
    )
    assert done.returncode == 0, done.stdout + done.stderr
