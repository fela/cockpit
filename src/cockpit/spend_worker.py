"""Parse Claude transcripts in a child process; publish parsed records to the web server."""

from __future__ import annotations

import logging
import multiprocessing
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path

from cockpit import spend

log = logging.getLogger(__name__)


class _PublishError(RuntimeError):
    """A parsed record could not reach the web server; the worker stops rather than completing a partial refresh."""


class _PublishingIndex(spend.SpendIndex):
    def __init__(self, projects_dir: Path, output: Connection) -> None:
        super().__init__(projects_dir)
        self.output = output

    def _read(self, path: Path, known: spend.Transcript | None, size: int) -> None:
        super()._read(path, known, size)
        # send serializes before returning, so a later refresh cannot mutate a record still being sent.
        try:
            self.output.send(("transcript", path, self._files[path], self.unpriced))
        except Exception as exc:
            raise _PublishError(f"could not publish parsed usage for {path}") from exc


class _ForwardLogs(logging.Handler):
    def __init__(self, output: Connection) -> None:
        super().__init__()
        self.output = output

    def emit(self, record: logging.LogRecord) -> None:
        self.output.send(("log", record.name, record.levelno, self.format(record)))


def _run(projects_dir: Path, output: Connection) -> None:
    root = logging.getLogger()
    root.handlers = [_ForwardLogs(output)]
    root.setLevel(logging.INFO)
    index = _PublishingIndex(projects_dir, output)
    try:
        while True:
            started = time.monotonic()
            try:
                index.refresh(datetime.now(UTC))
            except _PublishError:
                raise
            except (BrokenPipeError, EOFError):
                return
            except Exception:
                log.exception("counting Claude use from the transcripts failed")
            else:
                output.send(("ready", index.ready_at))
                log.info("Claude use counted in %.1f s", time.monotonic() - started)
            time.sleep(spend.REFRESH_SECONDS)
    except (BrokenPipeError, EOFError):
        pass  # The web server closed its end of the private pipe.
    finally:
        output.close()


@dataclass
class Worker:
    index: spend.SpendIndex
    process: BaseProcess
    connection: Connection
    stopping: threading.Event
    thread: threading.Thread | None = None

    def receive(self) -> None:
        try:
            while True:
                kind, *values = self.connection.recv()
                if kind == "transcript":
                    path, transcript, unpriced = values
                    with self.index._lock:
                        self.index._files[path] = transcript
                        self.index.unpriced = unpriced
                        self.index._version += 1
                elif kind == "ready":
                    self.index.ready_at = values[0]
                elif kind == "log":
                    name, level, message = values
                    logging.getLogger(name).log(level, message)
                else:
                    raise ValueError(f"unknown usage-index message: {kind}")
        except Exception:
            if not self.stopping.is_set():
                log.exception("Claude usage index worker stopped; showing the last received usage")

    def stop(self) -> None:
        self.stopping.set()
        if self.process.is_alive():
            self.process.terminate()
        self.process.join(5)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(5)
        self.connection.close()
        if self.thread is not None:
            self.thread.join(1)


def start(index: spend.SpendIndex) -> Worker:
    """Start a parser with its own Python interpreter; only the parent/child pipe carries parsed records."""
    context = multiprocessing.get_context("spawn")
    reader, writer = context.Pipe(duplex=False)
    process = context.Process(target=_run, args=(index.projects_dir, writer), name="cockpit-usage-index", daemon=True)
    try:
        process.start()
    except BaseException:
        reader.close()
        writer.close()
        raise
    writer.close()
    worker = Worker(index, process, reader, threading.Event())
    worker.thread = threading.Thread(target=worker.receive, name="claude-spend", daemon=True)
    worker.thread.start()
    return worker
