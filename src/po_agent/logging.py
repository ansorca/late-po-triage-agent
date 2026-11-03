"""Structured (JSON-lines) logging with automatic per-request context.

`log_context(request_id=...)` stores values in a ContextVar. asyncio copies the context into
every task, so concurrent sessions each get their own values without passing them around.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import IO, Any

_context: ContextVar[dict[str, Any] | None] = ContextVar("log_context", default=None)


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    token = _context.set({**(_context.get() or {}), **fields})
    try:
        yield
    finally:
        _context.reset(token)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
            **(_context.get() or {}),
            **getattr(record, "fields", {}),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class StructuredLogger:
    """Thin wrapper: `log.info("event_name", key=value)` -> one JSON line."""

    def __init__(self, name: str) -> None:
        self._log = logging.getLogger(name)

    def debug(self, event: str, **fields: Any) -> None:
        self._log.debug(event, extra={"fields": fields})

    def info(self, event: str, **fields: Any) -> None:
        self._log.info(event, extra={"fields": fields})

    def warning(self, event: str, **fields: Any) -> None:
        self._log.warning(event, extra={"fields": fields})

    def error(self, event: str, **fields: Any) -> None:
        self._log.error(event, extra={"fields": fields})

    def exception(self, event: str, **fields: Any) -> None:
        self._log.exception(event, extra={"fields": fields})


def get_logger(name: str) -> StructuredLogger:
    return StructuredLogger(name)


def configure_logging(level: str = "INFO", stream: IO[str] | None = None) -> None:
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("po_agent")
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    root.propagate = False
