"""Run the browser checks: every state check, and the control-use regression when Playwright is configured."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# The state checks run page code in Node or Bun with simulated browser state, without a browser or server.
STATE_CHECKS = sorted((ROOT / "tests" / "browser").glob("*_state.cjs"))
# Where Bun's installer puts it, which is not always on PATH.
BUN_AT_HOME = Path.home() / ".bun" / "bin" / "bun"


def script_runtime() -> str | None:
    """Bun on PATH, else Bun where its installer puts it, else Node, else None.

    Bun comes first because the checks use `CustomEvent`, which Node 18 lacks.
    """
    bun = shutil.which("bun")
    if bun:
        return bun
    if BUN_AT_HOME.exists():
        return str(BUN_AT_HOME)
    return shutil.which("node")


@pytest.mark.parametrize("check", STATE_CHECKS, ids=[check.name for check in STATE_CHECKS])
def test_state_check_passes(check: Path):
    runtime = script_runtime()
    if runtime is None:
        pytest.skip("neither node nor bun is installed")
    done = subprocess.run(
        ["nice", "-n", "19", runtime, str(check)],
        cwd=ROOT, capture_output=True, text=True, check=False, timeout=300,
    )
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.skipif(not os.environ.get("PLAYWRIGHT_MODULE"), reason="set PLAYWRIGHT_MODULE to run browser checks")
def test_control_use_failure_is_stored():
    node = shutil.which("node")
    assert node is not None, "Node is required for browser checks"
    done = subprocess.run(
        ["nice", "-n", "19", node, "tests/browser/control_use_errors.cjs"],
        cwd=ROOT, capture_output=True, text=True, check=False, timeout=300,
    )
    assert done.returncode == 0, done.stdout + done.stderr
