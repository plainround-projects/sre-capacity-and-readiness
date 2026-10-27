# Logging (Python `logging` and the logfmt format)

## What it is

`logging` is a Python standard-library module. Instead of `print`, code writes **log records** with a **level** (DEBUG, INFO, WARNING, ERROR, CRITICAL), a message and optional extra fields. Handlers decide where the records go; a **formatter** decides what the line looks like.

**logfmt** is a simple line format: `key=value` pairs separated by spaces, for example `ts=2026-10-27T10:00:00.123Z level=info logger=capacity.http_runtime msg=listening port=8080`. It is easy to read for people and easy to search with tools.

## Why this repository uses it

While you run experiments you read the processes' logs to see what happened and when. Structured lines with the same fields everywhere can be filtered by key.

## Where it appears

| File | Use |
| --- | --- |
| `infrastructure/logging_setup.py` | `LogfmtFormatter` and `configure_logging(level)`. |
| `interfaces/ledger_app.py`, `instance_app.py`, `http_runtime.py` | `log = logging.getLogger(__name__)` and calls such as `log.info("listening", extra={"port": port})`. |
| `docker-compose.yml` | `LOG_LEVEL` environment variable. |
| `infrastructure/settings.py` | Validates `LOG_LEVEL`. |

## How it works underneath

- `logging.getLogger("name")` returns a logger. Loggers form a tree by dotted name; the **root logger** is the top.
- When code calls `log.info(...)`, the logger creates a `LogRecord` object holding the time, level, name, message and any `extra` fields, then passes it to its **handlers**.
- A `StreamHandler(sys.stderr)` writes to **standard error**. Container platforms collect stdout and stderr, so `docker logs` shows them.
- `setLevel("INFO")` drops DEBUG records. `extra={"port": 8080}` adds fields to the record as attributes.

## Walkthrough of this repository's use

```python
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
```

- `_RESERVED = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}`: builds the set of attribute names every record already has. Anything not in it was added through `extra=`, so it is a custom field to print.
- `_quote` returns the value as text unchanged when it has no space, `=` or `"`; otherwise it wraps it in double quotes and escapes backslashes and quotes. That keeps each line unambiguous.
- `format`: builds `stamp` from `record.created` (seconds since 1970) in UTC with milliseconds and a trailing `Z`. It starts a dictionary with `ts`, `level`, `logger`, `msg`, then adds each custom field. If the record carries an exception, only the last line of its traceback becomes an `error` field. Finally `" ".join(f"{key}={_quote(value)}" ...)` makes the line.
- `configure_logging`: creates the handler, replaces the root logger's handlers (`root.handlers[:] = [handler]` so calling twice does not duplicate output), sets the level, and turns the noisy `aiohttp.access` logger down to WARNING.

### Using it

```python
    log.info("listening", extra={"port": port})
await stop.wait()
log.info("shutting down")
```

`log.info("listening", extra={"port": port})` produces `... level=info logger=capacity.infrastructure.http_runtime msg=listening port=8080`.

```python
except Exception:
    log.exception("ledger insert failed")
    app[METRICS].request(500)
    return web.json_response({"error": "internal error"}, status=500)
```

When a ledger insert fails for an unexpected reason, `log.exception("ledger insert failed")` writes the message **and** the traceback's last line as an `error` field, then the handler answers 500. Expected problems (a full pool) are counted in metrics instead of logged, so a flood of 503s does not flood the log.

## Try it yourself

1. `docker compose logs ledger | head -5` shows logfmt lines, starting with `msg=listening`.
2. `docker compose logs ledger | grep level=error` finds unexpected ledger failures (none in a healthy run).
3. `LOG_LEVEL=DEBUG make up` (if you recreate the stack) lowers the threshold; most records stay INFO because the code logs little.
4. `.venv/bin/python -c "from capacity.infrastructure.logging_setup import configure_logging; import logging; configure_logging('INFO'); logging.getLogger('demo').info('hello world', extra={'k': 'a b'})"` prints a line with `msg="hello world" k="a b"`.
5. `grep -rn "extra=" src/capacity | head` lists the places where fields are attached to log lines.

## Mistakes people make

- Using `print` for diagnostics: no level, no time, no fields.
- Logging secrets. The admin token and the database password are never logged.
- Adding handlers on every call, producing duplicate lines. `configure_logging` is safe to call twice.
- Building the message with f-strings of many values; here fields are separate.

## Check yourself

1. What is the root logger?
   *Answer: the top logger in the tree; its handlers receive every record unless a logger stops propagation.*
2. What does `extra=` do?
   *Answer: attaches additional fields to the log record.*
3. Why quote values that contain spaces?
   *Answer: so each `key=value` pair can be split unambiguously.*
4. Why log to standard error?
   *Answer: container runtimes collect stdout/stderr as the logs.*
5. Why lower the `aiohttp.access` logger?
   *Answer: it would log every request line; the structured request log is written by the API on purpose.*

## Further reading

- `logging` (Python 3.12): <https://docs.python.org/3.12/library/logging.html>
- logfmt (informal convention): <https://brandur.org/logfmt>
