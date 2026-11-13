"""The secrets the user hands a session from a step, such as an API key (SPEC MSG-15).

Each secret is a file named after it in one folder that only this user may open (0700), the file itself 0600.
The value goes to that file and nowhere else: not the database, the message, the logs or the page's storage.
The session gets the step's message, which says where the file is and how to use it without printing it.
"""

from __future__ import annotations

import logging
import os
import stat
import threading
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from cockpit.config import user_name

# Letters, digits, dash and underscore: a name that is a plain file name and nothing else (no dot, no slash).
NAME_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
# An API key or token is a line well under this; the page's field takes one line.
VALUE_LIMIT = 16384

# One secret is written at a time, so a write's leftovers of an earlier one are never another's work in progress.
_writing = threading.Lock()

log = logging.getLogger(__name__)


@dataclass
class Staged:
    """A value written beside its secret's name, waiting for `publish`."""

    path: Path
    directory: int
    partial: str
    previous: str
    published: bool = False

    def publish(self) -> None:
        """The value takes the secret's name; an earlier value stays linked aside until the block succeeded."""
        name = self.path.name
        try:
            os.link(name, self.previous, src_dir_fd=self.directory, dst_dir_fd=self.directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        os.replace(self.partial, name, src_dir_fd=self.directory, dst_dir_fd=self.directory)
        self.published = True


@contextmanager
def staged(folder: Path, name: str, value: str) -> Generator[Staged]:
    """Write `value` beside the secret `name` in `folder`; the block publishes it inside the hand-off's transaction.

    When the block fails, also in the commit after `publish`, the secret is as it was before: an earlier value
    comes back, a new name goes. The folder is opened without following a link, made 0700 (also when it was there
    before), and every write goes through it; the file is 0600 from its first byte.
    """
    with _writing:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fchmod(directory, 0o700)
            # What a stopped server left of an earlier write holds a value too.
            for entry in os.listdir(directory):
                if entry.startswith(f".{name}."):
                    os.unlink(entry, dir_fd=directory)
            # A folder in the secret's place could not be replaced: refuse it before the hand-off.
            try:
                if stat.S_ISDIR(os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode):
                    raise IsADirectoryError(f"{folder / name} is a folder")
            except FileNotFoundError:
                pass
            token = uuid.uuid4().hex
            secret = Staged(folder / name, directory, f".{name}.{token}.partial", f".{name}.{token}.previous")
            descriptor = os.open(secret.partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                                 dir_fd=directory)
            try:
                with os.fdopen(descriptor, "w") as file:
                    file.write(value)
                    file.flush()
                    os.fsync(file.fileno())
                yield secret
            except BaseException:
                # A restore that fails raises from here and keeps the earlier value in its `.previous` file.
                if secret.published:
                    if _exists(secret.previous, directory):
                        os.replace(secret.previous, name, src_dir_fd=directory, dst_dir_fd=directory)
                    else:
                        os.unlink(name, dir_fd=directory)
                _remove(secret.partial, directory)
                raise
            # The hand-off committed: a leftover that will not go must not stop its delivery. The next write of
            # this name removes it.
            try:
                _remove(secret.previous, directory)
            except OSError:
                log.exception("removing the earlier value of secret %s failed", name)
        finally:
            os.close(directory)


def _exists(name: str, directory: int) -> bool:
    try:
        os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _remove(name: str, directory: int) -> None:
    try:
        os.unlink(name, dir_fd=directory)
    except FileNotFoundError:
        pass


def message(name: str, path: Path, text: str) -> str:
    """The message that hands the step over: where the secret is, then what the user wrote with it."""
    stored = (
        f"{user_name()} stored secret {name} at {path}. Read it inside the command that needs it, such as"
        f" `KEY=\"$(cat {path})\" <command>`, so its value stays out of this conversation."
    )
    return f"{stored}\n\n{text.strip()}" if text.strip() else stored
