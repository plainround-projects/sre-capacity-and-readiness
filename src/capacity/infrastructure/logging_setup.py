"""Structured logging: one logfmt line per record, on standard error."""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime

_RESERVED = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


def _quote(value: object) -> str:
    text = str(value)
    if text and not any(char in text for char in ' ="'):
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


class LogfmtFormatter(logging.Formatter):
    """`ts=... level=... logger=... msg=... key=value`, with extra fields from `extra={...}`."""

    def format(self, record: logging.LogRecord) -> str:
        """Render one record; exceptions are appended as an `error` field."""
        stamp = datetime.fromtimestamp(record.created, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        fields = {"ts": stamp, "level": record.levelname.lower(), "logger": record.name, "msg": record.getMessage()}
        fields.update({key: value for key, value in record.__dict__.items() if key not in _RESERVED})
        if record.exc_info:
            fields["error"] = self.formatException(record.exc_info).splitlines()[-1]
        return " ".join(f"{key}={_quote(value)}" for key, value in fields.items())


def configure_logging(level: str) -> None:
    """Send every record at or above `level` to standard error as logfmt. Safe to call twice."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(LogfmtFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
