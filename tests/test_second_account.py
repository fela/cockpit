"""The real setup shell refuses unsupported systems before running setup commands."""

import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(("kernel", "status"), [("Darwin", 0), ("FreeBSD", 0), ("", 0), ("", 1)])
def test_unsupported_platform_refuses_before_setup(tmp_path, kernel, status):
    home = tmp_path / "home"
    home.mkdir()
    commands = tmp_path / "commands.log"
    stubs = tmp_path / "bin"
    stubs.mkdir()
    uname = stubs / "uname"
    uname.write_text(
        '#!/bin/sh\nprintf "uname %s\\n" "$*" >> "$COMMAND_LOG"\n'
        'printf "%s\\n" "$TEST_KERNEL"\nexit "$TEST_UNAME_STATUS"\n'
    )
    uname.chmod(0o755)
    for name in (
        "mkdir", "chmod", "ln", "mount", "sudo", "systemctl", "tee", "rm", "rmdir", "mv",
        "mktemp", "awk", "readlink", "mountpoint", "findmnt", "tail", "umount",
    ):
        stub = stubs / name
        stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "{name} $*" >> "$COMMAND_LOG"\nexit 99\n')
        stub.chmod(0o755)
    script = Path(__file__).resolve().parents[1] / "scripts" / "second-account.sh"
    done = subprocess.run(
        ["/bin/sh", str(script)],
        env={
            "HOME": str(home), "PATH": str(stubs), "COMMAND_LOG": str(commands),
            "TEST_KERNEL": kernel, "TEST_UNAME_STATUS": str(status),
        },
        capture_output=True, text=True, check=False, timeout=5,
    )
    assert done.returncode == 1
    assert done.stderr == "second-account.sh sets up a bind mount in /etc/fstab and runs on Linux only.\n"
    assert done.stdout == ""
    assert commands.read_text().splitlines() == ["uname -s"]
    assert home.is_dir()
    assert list(home.rglob("*")) == []
