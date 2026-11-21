"""Redact SDK records before Cockpit's log handlers can persist a connector payload or header.

The MCP SDK can log validation exceptions containing rejected client requests or provider responses.
A task-local context covers its child tasks and HTTP clients while leaving other Cockpit requests' logs unchanged.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
from collections.abc import Generator
from typing import Any

_upstream: contextvars.ContextVar[tuple[str, bool] | None] = contextvars.ContextVar("cockpit_connector_log_context", default=None)


def _install() -> None:
    previous = logging.getLogRecordFactory()

    def safe_record(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        context = _upstream.get()
        if context is not None and (
            record.name == "root" or record.name.startswith(("mcp.", "httpx", "httpcore"))
        ):
            provider, catalog = context
            kind = record.exc_info[0].__name__ if record.exc_info and record.exc_info[0] else "none"
            # For catalog checks the gateway logs the final failure. These SDK records report the same
            # receive failure or a late event trying to reach a stream that is already being closed.
            if catalog and (
                (record.name == "root" and record.funcName == "_receive_loop" and kind == "GatewayError")
                or (record.name == "mcp.client.streamable_http" and record.funcName == "_handle_sse_event"
                    and kind in {"BrokenResourceError", "ClosedResourceError"})
            ):
                record.levelno = logging.DEBUG
                record.levelname = "DEBUG"
            record.msg = "Connector upstream event: provider=%s logger=%s exception=%s"
            record.args = (provider, record.name, kind)
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return record

    logging.setLogRecordFactory(safe_record)


_install()


@contextlib.contextmanager
def private_upstream(provider: str, *, catalog: bool = False) -> Generator[None]:
    token = _upstream.set((provider, catalog))
    try:
        yield
    finally:
        _upstream.reset(token)
