# Python: the infrastructure layer, file by file

## What it is

The infrastructure layer is `src/capacity/infrastructure/`. It holds the **adapters**: the code that touches the outside world. It reads and writes files, runs `docker compose`, speaks raw HTTP over a socket to generate load, serves HTTP with aiohttp, exports Prometheus metrics, parses environment variables and formats log lines. It implements the ports the application layer defines, and it may import the domain and application layers. Nothing in the domain or application layer imports it (`import-linter` enforces that).

## Why this repository uses it

The lesson needs real measurements: real CPU burned by real requests, a real load balancer, a real pool. Those need real sockets and real processes, but the decisions made from them must stay testable. Keeping the sockets in this layer means the knee search, the plan and the review can be tested without them, and the adapters can be tested on their own against a small local server.

## Where it appears

| File | What it adapts |
| --- | --- |
| `infrastructure/__init__.py` | Package marker. |
| `infrastructure/settings.py` | Environment variables to validated settings. |
| `infrastructure/logging_setup.py` | Structured logfmt logging. |
| `infrastructure/metrics.py` | Prometheus metrics of an instance and of the ledger. |
| `infrastructure/ops.py` | The `/healthz`, `/readyz` and `/metrics` app every process exposes. |
| `infrastructure/http_runtime.py` | Serving aiohttp apps with a graceful shutdown. |
| `infrastructure/openloop.py` | The open-loop load generator and the CPU probe. |
| `infrastructure/compose.py` | Driving Docker Compose and the load balancer from the host. |
| `infrastructure/project_files.py` | The repository's files, the evidence and the checklist. |
| `infrastructure/slo_files.py` | Reading `slo/slos.yaml` and writing the generated rule files. |

## How it works underneath

An adapter is a class whose methods have the same names and types as a port, so the application layer can use it without knowing it is real. The adapters here use `async` code for network work (the event loop runs many requests at once on one thread) and plain blocking calls for host-side tooling (`subprocess`, `urllib`).

## Walkthrough of this repository's use

### `infrastructure/__init__.py`

```python
"""Adapters: HTTP servers and clients, PostgreSQL, Docker Compose, files, metrics, logging and settings."""
```

### `infrastructure/settings.py`: configuration from the environment

```python
"""Configuration from the environment, validated once at start-up (twelve-factor)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from capacity.domain.cpu import DEFAULT_ROUNDS


class SettingsError(ValueError):
    """An environment variable is missing, malformed or out of range."""


def _text(env: Mapping[str, str], name: str, default: str) -> str:
    value = env.get(name, default).strip()
    if not value:
        raise SettingsError(f"{name} must not be empty")
    return value


def _number(env: Mapping[str, str], name: str, default: float, low: float, high: float) -> float:
    raw = env.get(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError as error:
        raise SettingsError(f"{name} must be a number, got {raw!r}") from error
    if not low <= value <= high:
        raise SettingsError(f"{name} must be between {low:g} and {high:g}, got {value:g}")
    return value


def _port(env: Mapping[str, str], name: str, default: int) -> int:
    return int(_number(env, name, default, 1, 65535))
```

The module reads configuration from environment variables (the twelve-factor rule: configuration lives in the environment, not in code) and validates it **once at start-up**, so a typo fails the container immediately instead of an hour into a run. `SettingsError` is a `ValueError` subclass for a missing, malformed or out-of-range value. `_text` returns a stripped string and refuses an empty one. `_number` returns the variable as a float (or the default when it is not set), turns `float()`'s `ValueError` into a `SettingsError` that quotes the bad input with `!r` and refuses values outside `[low, high]`. `_port` is a number between 1 and 65,535 as an `int`.

```python
@dataclass(frozen=True, slots=True)
class InstanceSettings:
    """Settings of one service instance."""

    zone: str
    instance: str
    version: str
    work_rounds: int
    ledger_url: str | None
    ledger_timeout_s: float
    port: int
    ops_port: int
    log_level: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> InstanceSettings:
        """Read and validate. An empty LEDGER_URL means the instance has no shared dependency."""
        ledger = env.get("LEDGER_URL", "").strip().rstrip("/")
        return cls(
            zone=_text(env, "ZONE", "a"),
            instance=_text(env, "INSTANCE", "a1"),
            version=_text(env, "VERSION", "v1"),
            work_rounds=int(_number(env, "WORK_ROUNDS", DEFAULT_ROUNDS, 1, 10_000_000)),
            ledger_url=ledger or None,
            ledger_timeout_s=_number(env, "LEDGER_TIMEOUT_S", 0.5, 0.05, 30.0),
            port=_port(env, "PORT", 8080),
            ops_port=_port(env, "OPS_PORT", 9101),
            log_level=_text(env, "LOG_LEVEL", "INFO").upper(),
        )
```

`InstanceSettings` is everything one service instance needs: its zone, name, version, the rounds of CPU work per request, the optional ledger URL and timeout, its two ports and its log level. `from_env` reads them with defaults (`ZONE=a`, `INSTANCE=a1`, `VERSION=v1`, port 8080, ops port 9101). An empty `LEDGER_URL` becomes `None`: the instance then has no shared dependency, which is what the knee test measures.

```python
@dataclass(frozen=True, slots=True)
class LedgerSettings:
    """Settings of the shared ledger service and its connection pool, the dependency every instance calls."""

    database_url: str
    pool_size: int
    pool_timeout_s: float
    hold_ms: float
    port: int
    ops_port: int
    log_level: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> LedgerSettings:
        """Read and validate. DATABASE_URL has no default."""
        url = env.get("DATABASE_URL", "").strip()
        if not url.startswith("postgresql://"):
            raise SettingsError("DATABASE_URL must be set to a postgresql:// URL")
        return cls(
            database_url=url,
            pool_size=int(_number(env, "POOL_SIZE", 20, 1, 90)),
            pool_timeout_s=_number(env, "POOL_TIMEOUT_S", 0.4, 0.01, 30.0),
            hold_ms=_number(env, "HOLD_MS", 10.0, 0.0, 1000.0),
            port=_port(env, "PORT", 8082),
            ops_port=_port(env, "OPS_PORT", 9102),
            log_level=_text(env, "LOG_LEVEL", "INFO").upper(),
        )
```

`LedgerSettings` is the shared ledger service's configuration. `DATABASE_URL` has no default and must start with `postgresql://`. `POOL_SIZE` (default 20, at most 90), `POOL_TIMEOUT_S` (0.4), `HOLD_MS` (10, the time a request holds its connection, which models the real work done while holding it), the ports 8082 and 9102 and the log level.

### `infrastructure/logging_setup.py`

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

`logfmt` is one line per record of `key=value` pairs, easy for people to read and for tools to parse. `_RESERVED` is the set of attribute names every `LogRecord` already has (plus `message` and `asctime`), so anything else on the record must have been added by `extra={...}`. `_quote` leaves a value bare unless it is empty or contains a space, an equals sign or a quote, in which case it wraps it in double quotes and escapes backslashes and quotes. `LogfmtFormatter.format` builds the fields: the UTC timestamp to the millisecond with a `Z`, the lower-case level, the logger name and the message, then the extra fields, and appends the last line of an exception as `error`. `configure_logging` replaces the root logger's handlers with one that writes to standard error (so calling it twice is safe), sets the level and quiets aiohttp's access log.

### `infrastructure/metrics.py`

```python
"""Prometheus metrics of an instance and of the ledger."""

from __future__ import annotations

import time
from collections.abc import Callable

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.core import CounterMetricFamily
from prometheus_client.registry import Collector

from capacity.domain.telemetry import (
    APP_DURATION_SECONDS,
    APP_INFLIGHT,
    APP_LATENCY_BUCKETS_SECONDS,
    APP_REQUESTS_TOTAL,
)


class ProcessCpuCollector(Collector):
    """`process_cpu_seconds_total` from the interpreter's own clock, so it exists on every platform.

    The stock process collector reads /proc and so exists only on Linux. The load test reads this counter before and
    after each step to learn how much of its one core the instance used.
    """

    def collect(self) -> list[CounterMetricFamily]:
        """One counter: user plus system CPU seconds this process has used."""
        family = CounterMetricFamily("process_cpu_seconds", "Total user and system CPU time spent in seconds.")
        family.add_metric([], time.process_time())
        return [family]

```

`prometheus_client` is the library that exposes metrics in Prometheus's text format. `ProcessCpuCollector` is a custom collector that reports `process_cpu_seconds_total` from `time.process_time()` (user plus system CPU of this process). The stock collector reads `/proc` and exists only on Linux; this one works on every platform, and it is the counter the load test reads before and after each step to learn how much of its one core the instance used.

```python
class InstanceMetrics:
    """What an instance exports. `process_cpu_seconds_total` is what the load test reads to name the resource that
    saturated."""

    def __init__(self, zone: str, instance: str) -> None:
        """Create the collectors in their own registry; `app_info` carries the zone and instance."""
        self.registry = CollectorRegistry()
        self.registry.register(ProcessCpuCollector())
        constant = {"zone": zone, "instance": instance}
        self._requests = Counter(
            APP_REQUESTS_TOTAL, "Requests this instance answered.", ["code"], registry=self.registry
        )
        self._duration = Histogram(
            APP_DURATION_SECONDS,
            "Seconds to answer a request.",
            buckets=APP_LATENCY_BUCKETS_SECONDS,
            registry=self.registry,
        )
        self._inflight = Gauge(APP_INFLIGHT, "Requests being handled right now.", registry=self.registry)
        self._info = Gauge("app_info", "Which instance this is.", list(constant), registry=self.registry)
        self._info.labels(**constant).set(1)
        for code in ("200", "503"):
            self._requests.labels(code)

    def observe(self, status: int, seconds: float) -> None:
        """Count one answered request."""
        self._requests.labels(str(status)).inc()
        self._duration.observe(seconds)

    def inflight(self) -> Gauge:
        """The in-flight gauge, for `track_inprogress`."""
        return self._inflight

    def render(self) -> bytes:
        """The text exposition format."""
        return bytes(generate_latest(self.registry))

```

`InstanceMetrics` gives the instance its own `CollectorRegistry` (a private set of metrics, so tests do not share state). It creates a counter of requests by response `code`, a histogram of seconds to answer (with the buckets from the domain), a gauge of requests in flight and `app_info` labelled with the zone and instance and set to 1. The `for code in ("200", "503")` line creates both label values at zero so the series exist before the first request. `observe` counts one answered request and records its duration; `inflight` returns the gauge for `track_inprogress`; `render` returns the text exposition format.

```python
class LedgerMetrics:
    """The shared pool's vital signs: how many connections are in use, how many requests wait, and how many gave up."""

    def __init__(self, in_use: Callable[[], float], waiting: Callable[[], float], size: int) -> None:
        """`in_use` and `waiting` are read at scrape time, so the gauges are never stale."""
        self.registry = CollectorRegistry()
        Gauge("ledger_pool_in_use", "Pool connections in use.", registry=self.registry).set_function(in_use)
        Gauge("ledger_pool_waiting", "Requests waiting for a pool connection.", registry=self.registry).set_function(
            waiting
        )
        Gauge("ledger_pool_size", "Hard limit on pool connections.", registry=self.registry).set(size)
        self._requests = Counter(
            "ledger_requests_total", "Requests the ledger answered.", ["code"], registry=self.registry
        )
        self._timeouts = Counter(
            "ledger_pool_timeouts_total", "Requests that gave up waiting for a connection.", registry=self.registry
        )

    def request(self, status: int) -> None:
        """Count one answered request."""
        self._requests.labels(str(status)).inc()

    def timeout(self) -> None:
        """Count one pool wait that timed out."""
        self._timeouts.inc()

    def render(self) -> bytes:
        """The text exposition format."""
        return bytes(generate_latest(self.registry))
```

`LedgerMetrics` exports the shared pool's vital signs: connections in use and requests waiting (gauges whose values come from functions called **at scrape time**, so they are never stale), the pool's size, a counter of answered requests by code and a counter of pool waits that timed out.

### `infrastructure/ops.py`

```python
"""The operations app every process exposes on its own port: health, readiness and metrics."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from aiohttp import web


def build_ops_app(
    render_metrics: Callable[[], bytes], is_ready: Callable[[], Awaitable[bool]] | None = None
) -> web.Application:
    """`/healthz` says the process is alive, `/readyz` that its dependency answers, `/metrics` the Prometheus text."""
    app = web.Application()

    async def healthz(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def readyz(_: web.Request) -> web.Response:
        ready = await is_ready() if is_ready else True
        return web.json_response({"status": "ready" if ready else "not ready"}, status=200 if ready else 503)

    async def metrics(_: web.Request) -> web.Response:
        return web.Response(body=render_metrics(), content_type="text/plain", charset="utf-8")

    app.router.add_get("/healthz", healthz)
    app.router.add_get("/readyz", readyz)
    app.router.add_get("/metrics", metrics)
    return app
```

Every process exposes a small second web app on its own port. `/healthz` answers 200 `{"status": "ok"}` while the process is alive. `/readyz` calls `is_ready()` (when given) and answers 200 `ready` or 503 `not ready`; the instance uses it to say whether the ledger answers. `/metrics` returns the Prometheus text. The handlers are nested functions that capture `render_metrics` and `is_ready`.

### `infrastructure/http_runtime.py`

```python
"""Run aiohttp applications until SIGTERM or SIGINT, then finish in-flight requests and close."""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Sequence

from aiohttp import web

log = logging.getLogger(__name__)


async def serve(
    apps: Sequence[tuple[web.Application, int]],
    stop: asyncio.Event | None = None,
    shutdown_timeout: float = 10.0,
    host: str = "0.0.0.0",
) -> None:
    """Start every application on its port and block until `stop` is set or a termination signal arrives."""
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    runners: list[web.AppRunner] = []
    try:
        for app, port in apps:
            runner = web.AppRunner(app, access_log=None, shutdown_timeout=shutdown_timeout)
            await runner.setup()
            runners.append(runner)
            await web.TCPSite(runner, host, port).start()
            log.info("listening", extra={"port": port})
        await stop.wait()
        log.info("shutting down")
    finally:
        for runner in runners:
            await runner.cleanup()
```

`serve` starts every application on its port and blocks until a stop event is set or SIGTERM or SIGINT arrives (`loop.add_signal_handler` turns a signal into `stop.set()`). Each application gets a `web.AppRunner` (with the access log off and a shutdown timeout so in-flight requests finish) and a `TCPSite`. The `finally` block cleans every runner up even if start-up fails half-way. This is what makes a rolling restart lose no request: Docker sends SIGTERM, the process stops listening, finishes what it is doing and exits.

### `infrastructure/openloop.py`: the open-loop generator

```python
"""An open-loop HTTP/1.1 load generator over raw asyncio streams.

Open loop: each request is scheduled to arrive at a fixed time whether or not earlier ones have finished, and its
latency runs from that scheduled time to the end of its response. A closed-loop generator (send the next request
after the last one returns) can never show queuing delay, because it never lets a queue form.
"""

from __future__ import annotations

import asyncio
import re
from collections import deque
from dataclasses import dataclass

from capacity.domain.latency import Sample, percentile

_CPU_LINE = re.compile(r"^process_cpu_seconds_total\s+([0-9.eE+-]+)\s*$", re.MULTILINE)


@dataclass(slots=True)
class _Connection:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter

```

The docstring is the lesson of this file: in an **open loop** each request is scheduled to arrive at a fixed time whether or not earlier ones finished, and its latency runs from that scheduled time to the end of its response. A closed-loop generator (send the next after the last returns) can never show queuing delay, because it never lets a queue form. `_CPU_LINE` is a compiled regular expression that finds the `process_cpu_seconds_total` line. `_Connection` pairs an asyncio `StreamReader` and `StreamWriter`.

```python
class RawOpenLoopRunner:
    """Sends `GET path` at a fixed rate over a pool of keep-alive connections."""

    def __init__(
        self, host: str, port: int, path: str = "/work", max_connections: int = 512, timeout_s: float = 5.0
    ) -> None:
        """`max_connections` bounds requests in flight; a request that waits for a connection still counts that wait."""
        self._host = host
        self._port = port
        self._request = f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: capacity-loadtest\r\n\r\n".encode()
        self._limit = asyncio.Semaphore(max_connections)
        self._timeout_s = timeout_s
        self._idle: deque[_Connection] = deque()
```

`RawOpenLoopRunner` speaks HTTP/1.1 itself over asyncio streams. The constructor prebuilds the request bytes (`GET <path> HTTP/1.1`, `Host`, `User-Agent`, a blank line, encoded), a semaphore that bounds requests in flight (a request that waits for a connection still counts that wait in its latency) and a deque of idle keep-alive connections.

```python
async def run(self, rps: float, duration_s: float) -> tuple[list[Sample], float]:
    """Send `rps * duration_s` requests on schedule; return all samples and the generator's own p99 lag in ms."""
    loop = asyncio.get_running_loop()
    total = int(rps * duration_s)
    start = loop.time()
    samples: list[Sample] = []
    lags: list[float] = []
    tasks: set[asyncio.Task[None]] = set()

    async def one(scheduled: float) -> None:
        ok = await self._request_once()
        samples.append(Sample(scheduled - start, loop.time() - scheduled, ok))

    index = 0
    while index < total:
        now = loop.time()
        due = min(total, int((now - start) * rps) + 1)
        while index < due:
            scheduled = start + index / rps
            lags.append(now - scheduled)
            task = loop.create_task(one(scheduled))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
            index += 1
        await asyncio.sleep(max(0.0005, start + index / rps - loop.time()))
    if tasks:
        await asyncio.gather(*tasks)
    return samples, percentile([lag * 1000.0 for lag in lags], 0.99)
```

`run(rps, duration_s)` computes the total number of requests, notes the start time, and loops: it asks how many requests are **due** by now (`int((now - start) * rps) + 1`), and for each creates a task, remembering how late it started (`lags`), then sleeps until the next scheduled time. The nested `one(scheduled)` awaits a request and appends a `Sample` whose latency is measured from the **scheduled** time. At the end it waits for every task and returns the samples and the generator's own p99 lag in milliseconds.

```python
async def close(self) -> None:
    """Close every idle connection."""
    while self._idle:
        self._idle.popleft().writer.close()

async def _request_once(self) -> bool:
    async with self._limit:
        connection: _Connection | None = None
        try:
            connection = self._idle.pop() if self._idle else await self._connect()
            status, keep_alive = await asyncio.wait_for(self._exchange(connection), self._timeout_s)
        except (OSError, asyncio.IncompleteReadError, TimeoutError, ValueError):
            if connection is not None:
                connection.writer.close()
            return False
        if keep_alive:
            self._idle.append(connection)
        else:
            connection.writer.close()
        return status == 200
```

`close` closes every idle connection. `_request_once` takes a slot from the semaphore, reuses an idle connection or opens a new one, runs the exchange under a timeout and counts any socket error, timeout, short read or malformed response as a failed request (closing that connection). A keep-alive connection goes back to the idle deque; otherwise it is closed. Success is a status of exactly 200.

```python
async def _connect(self) -> _Connection:
    reader, writer = await asyncio.wait_for(asyncio.open_connection(self._host, self._port), self._timeout_s)
    return _Connection(reader, writer)

async def _exchange(self, connection: _Connection) -> tuple[int, bool]:
    connection.writer.write(self._request)
    await connection.writer.drain()
    status = int((await connection.reader.readline()).split()[1])
    length, keep_alive = 0, True
    while True:
        line = await connection.reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        name, _, value = line.decode("latin-1").partition(":")
        if name.lower() == "content-length":
            length = int(value.strip())
        elif name.lower() == "connection" and value.strip().lower() == "close":
            keep_alive = False
    if length:
        await connection.reader.readexactly(length)
    return status, keep_alive
```

`_connect` opens a connection with a timeout. `_exchange` writes the request, waits for the buffer to flush (`drain`), reads the status line, then reads headers until the blank line, noting `Content-Length` and whether the server said `Connection: close`, and reads exactly that many body bytes so the connection is clean for reuse.

```python
class MetricsCpuProbe:
    """Reads `process_cpu_seconds_total` from an instance's ops port: how much CPU the process has used so far."""

    def __init__(self, host: str, port: int) -> None:
        """`port` is the instance's ops port."""
        self._host = host
        self._port = port

    async def cpu_seconds(self) -> float:
        """Cumulative CPU seconds of the instance."""
        reader, writer = await asyncio.open_connection(self._host, self._port)
        try:
            writer.write(f"GET /metrics HTTP/1.0\r\nHost: {self._host}\r\n\r\n".encode())
            await writer.drain()
            text = (await reader.read()).decode("utf-8", "replace")
        finally:
            writer.close()
        match = _CPU_LINE.search(text)
        if match is None:
            raise ValueError("the instance exports no process_cpu_seconds_total")
        return float(match.group(1))
```

`MetricsCpuProbe` is the `CpuProbe`: it opens a connection to the instance's ops port, sends `GET /metrics`, reads the whole reply, finds the `process_cpu_seconds_total` line with the regular expression and returns its value, or raises `ValueError` when the instance exports no such counter.

### `infrastructure/compose.py`: the experiments' hands

```python
"""Docker Compose and the stack's HTTP surfaces, driven from the host: the experiments' hands."""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_ATTEMPTS = 3
_RETRY_PAUSE_SECONDS = 2.0
_TRANSIENT = ("Bad Gateway", "request returned", "connection reset", "EOF", "i/o timeout")
HEALTHY_MEMBERS_URL = "http://localhost:47281/stats?filter=cluster.app.membership_healthy"


class ComposeError(RuntimeError):
    """A docker compose command failed."""

```

The experiments run on the host and drive the stack with Docker Compose. `subprocess` runs commands, `urllib` makes plain HTTP calls. `_ATTEMPTS = 3` and `_RETRY_PAUSE_SECONDS = 2.0` set the retry policy; `_TRANSIENT` lists the messages a busy Docker daemon returns for a failure that is worth retrying ("Bad Gateway", a reset connection). `HEALTHY_MEMBERS_URL` is Envoy's admin statistic of healthy members of the `app` cluster. `ComposeError` is what a failed command raises.

```python
class Compose:
    """Thin wrapper over the `docker compose` command for this project."""

    def __init__(self, project_dir: Path) -> None:
        """`project_dir` holds docker-compose.yml."""
        self._dir = project_dir

    def run(
        self, *args: str, env: dict[str, str] | None = None, stdin: bytes | None = None, timeout: float = 600
    ) -> bytes:
        """Run `docker compose <args>` and return standard output; raise ComposeError on a non-zero exit.

        A busy Docker daemon sometimes answers with a transient error ("Bad Gateway", a reset connection); those are
        retried, anything else fails at once.
        """
        for attempt in range(1, _ATTEMPTS + 1):
            result = subprocess.run(
                ["docker", "compose", *args],
                cwd=self._dir,
                env={**os.environ, **(env or {})},
                input=stdin,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            if result.returncode == 0:
                return result.stdout
            stderr = result.stderr.decode(errors="replace")
            if attempt == _ATTEMPTS or not any(marker in stderr for marker in _TRANSIENT):
                raise ComposeError(f"docker compose {' '.join(args)} failed: {stderr[-800:]}")
            time.sleep(attempt * _RETRY_PAUSE_SECONDS)
        raise AssertionError("unreachable")  # pragma: no cover
```

`Compose.run` executes `docker compose <args>` in the project directory with the current environment plus any extras, captures output and returns standard output. A non-zero exit is retried (waiting 2, then 4 seconds) only if stderr contains a transient marker; anything else raises `ComposeError` with the last 800 characters of stderr at once. The final `raise AssertionError("unreachable")` satisfies the type checker and is excluded from coverage.

```python
def recreate(self, services: Sequence[str], env: dict[str, str] | None = None) -> None:
    """Recreate the services (stop the old container gracefully, start a new one) and wait until healthy."""
    self.run("up", "-d", "--no-deps", "--force-recreate", "--wait", *services, env=env)

def stop(self, services: Sequence[str]) -> None:
    """Stop the services."""
    self.run("stop", *services)

def start(self, services: Sequence[str]) -> None:
    """Start stopped services and wait until healthy."""
    self.run("up", "-d", "--no-deps", "--wait", *services)

def pause(self, service: str) -> None:
    """Freeze a service: it still accepts connections but never answers (the worst dependency failure)."""
    self.run("pause", service)

def unpause(self, service: str) -> None:
    """Resume a paused service."""
    self.run("unpause", service)

def db(self, *args: str, stdin: bytes | None = None) -> bytes:
    """Run a command inside the database container as the superuser."""
    return self.run("exec", "-T", "db", *args, stdin=stdin)

def loadtest(self, *args: str, timeout: float = 1800) -> dict[str, Any]:
    """Run the load generator inside the compose network and parse its JSON report."""
    output = self.run("run", "--rm", "-T", "loadtest", *args, timeout=timeout)
    document: dict[str, Any] = json.loads(output)
    return document

def start_loadtest(self, *args: str) -> subprocess.Popen[bytes]:
    """Start the load generator in the background; read its report with `communicate()`."""
    return subprocess.Popen(
        ["docker", "compose", "run", "--rm", "-T", "loadtest", *args],
        cwd=self._dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
```

One small method per action: `recreate` replaces containers gracefully and waits until healthy; `stop` and `start` stop and start services; `pause` freezes a service so it still accepts connections but never answers (the worst dependency failure) and `unpause` resumes it; `db` runs a command in the database container; `loadtest` runs the load generator inside the compose network and parses its JSON report; `start_loadtest` starts it in the background and returns the `Popen` so the caller can run a rollout while the load runs and read the report with `communicate()`.

```python
def healthy_members() -> int:
    """Healthy instances according to the load balancer, or 0 when it cannot be reached."""
    try:
        with urllib.request.urlopen(HEALTHY_MEMBERS_URL, timeout=3) as response:
            return int(response.read().decode().strip().rsplit(":", 1)[1])
    except (urllib.error.URLError, TimeoutError, ValueError, IndexError):
        return 0


def wait_for_members(expected: int, timeout_s: float = 90.0) -> None:
    """Block until the load balancer sees exactly `expected` healthy instances."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if healthy_members() == expected:
            return
        time.sleep(0.5)
    raise TimeoutError(f"the load balancer did not reach {expected} healthy instances within {timeout_s:g}s")


def versions_seen(url: str, samples: int) -> set[str]:
    """The `X-Version` of `samples` responses through the load balancer."""
    seen: set[str] = set()
    for _ in range(samples):
        with urllib.request.urlopen(f"{url}/work", timeout=5) as response:
            seen.add(response.headers.get("X-Version", "?"))
    return seen
```

`healthy_members` asks Envoy how many instances it counts as healthy (returning 0 if it cannot be reached), `wait_for_members` blocks until the count is exactly the expected number and raises `TimeoutError` otherwise, and `versions_seen` samples the `X-Version` header of responses through the load balancer, which is how the rollout test proves both versions served traffic.

### `infrastructure/project_files.py`: the repository, the evidence and the checklist

```python
"""Adapters over the repository on disk: its files, its measured evidence and its checklist configuration."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from capacity.application.ports import SloCatalog
from capacity.domain.slo import BurnAlert, Slo
from capacity.infrastructure.slo_files import load_spec, render_rule_files


class FileSystemProject:
    """Read-only access to the repository root."""

    def __init__(self, root: Path) -> None:
        """`root` is the repository root."""
        self._root = root

    @property
    def root(self) -> Path:
        """The repository root."""
        return self._root

    def exists(self, relative: str) -> bool:
        """True when the file or folder exists."""
        return (self._root / relative).exists()

    def read(self, relative: str) -> str:
        """The file's text."""
        return (self._root / relative).read_text(encoding="utf-8")

    def glob(self, pattern: str) -> list[str]:
        """Relative paths matching a glob, sorted."""
        return sorted(str(path.relative_to(self._root)) for path in self._root.glob(pattern))

```

`FileSystemProject` is the `ProjectFiles` adapter: read-only access to the repository root, with `exists`, `read` (UTF-8 text), `glob` (relative paths, sorted) and the `root` property.

```python
class JsonEvidence:
    """Evidence documents under `docs/evidence/<name>.json`, written by the make targets."""

    def __init__(self, directory: Path) -> None:
        """`directory` holds the JSON files."""
        self._directory = directory

    def load(self, name: str) -> dict[str, object] | None:
        """The evidence called `name`, or None when the experiment has not been run."""
        path = self._directory / f"{name}.json"
        if not path.exists():
            return None
        document: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
        return document

    def save(self, name: str, document: dict[str, Any]) -> Path:
        """Write the evidence, creating the folder."""
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._directory / f"{name}.json"
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        return path
```

`JsonEvidence` is the `EvidenceStore`: `load(name)` reads `<directory>/<name>.json` (or returns `None` when the experiment has not been run) and `save(name, document)` creates the folder, writes the JSON indented with a trailing newline and returns the path.

```python
class YamlChecklist:
    """`readiness/checklist.yaml`: owners, fix dates and blocking flags per item, and the accepted risks."""

    def __init__(self, path: Path) -> None:
        """`path` is the YAML file."""
        self._document: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))

    def owner(self, item_id: str) -> str:
        """The role that owns the item."""
        return str(self._document["items"][item_id]["owner"])

    def due(self, item_id: str) -> date:
        """The date by which a failure of the item is fixed."""
        value = self._document["items"][item_id]["fix_by"]
        return value if isinstance(value, date) else date.fromisoformat(str(value))

    def blocking(self, item_id: str) -> bool:
        """True when a failure of the item must be fixed before launch."""
        return bool(self._document["items"][item_id].get("blocking", False))

    def accepted_risks(self) -> list[dict[str, str]]:
        """The accepted risks, each with area, check, evidence, owner and revisit_when."""
        return [{key: str(value) for key, value in risk.items()} for risk in self._document.get("accepted_risks", [])]
```

`YamlChecklist` reads `readiness/checklist.yaml` with `yaml.safe_load` (the safe loader builds only plain data, never arbitrary objects). `owner`, `due` (YAML may already have parsed the date; otherwise `date.fromisoformat` does), `blocking` (default false) and `accepted_risks` (every value turned into text) answer the `ChecklistConfig` port.

```python
class SloFilesCatalog(SloCatalog):
    """The SLO definitions and the generated rules, read from `slo/`."""

    def __init__(self, root: Path) -> None:
        """`root` is the repository root."""
        self._root = root
        self._spec = load_spec(root / "slo" / "slos.yaml")

    def slos(self) -> list[Slo]:
        """Every SLO."""
        return list(self._spec.slos)

    def alerts(self) -> list[BurnAlert]:
        """The burn-rate alert table."""
        return list(self._spec.alerts)

    def rules_in_sync(self) -> bool:
        """True when the committed rule files equal what the generator produces from the definitions."""
        return all(
            (self._root / path).exists() and (self._root / path).read_text(encoding="utf-8") == text
            for path, text in render_rule_files(self._spec).items()
        )
```

`SloFilesCatalog` implements `SloCatalog` over `slo/slos.yaml`. `rules_in_sync` regenerates both rule files in memory and compares each, byte for byte, with the committed file: any difference, or a missing file, is "out of date".

### `infrastructure/slo_files.py`: the SLO file and the generated rule files

```python
"""Read the SLO file, and write generated files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from capacity.application import rules
from capacity.domain.errors import InvalidSloError
from capacity.domain.slo import BurnAlert, SliKind, Slo


@dataclass(frozen=True, slots=True)
class JourneyDoc:
    """A journey's rank and its one-sentence definition of working."""

    name: str
    rank: int
    working: str


@dataclass(frozen=True, slots=True)
class SloSpec:
    """Everything `slo/slos.yaml` defines."""

    window_days: int
    journeys: tuple[JourneyDoc, ...]
    slos: tuple[Slo, ...]
    alerts: tuple[BurnAlert, ...]


def _load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    if not isinstance(document, dict):
        raise InvalidSloError(f"{path} must contain a mapping")
    return document
```

`JourneyDoc` is a journey's rank and its one-sentence definition of working. `SloSpec` is everything `slo/slos.yaml` defines. `_load` opens the file, parses it with `yaml.safe_load` and refuses anything that is not a mapping.

```python
def load_spec(path: Path) -> SloSpec:
    """Parse `slos.yaml`; any missing or inconsistent field raises InvalidSloError."""
    document = _load(path)
    try:
        window_days = int(document["window_days"])
        journeys = tuple(
            JourneyDoc(name, int(body["rank"]), str(body["working"]).strip())
            for name, body in document["journeys"].items()
        )
        slos = tuple(
            Slo(
                name=entry["name"],
                journey=entry["journey"],
                kind=SliKind(entry["kind"]),
                target=float(entry["target"]),
                valid=str(entry["valid"]).strip(),
                good=str(entry["good"]).strip(),
                why=str(entry["why"]).strip(),
                threshold_ms=float(entry["threshold_ms"]) if "threshold_ms" in entry else None,
                window_days=window_days,
            )
            for entry in document["slos"]
        )
        default_guard = int(document.get("min_bad_events", 10))
        alerts = tuple(
            BurnAlert(
                e["name"],
                e["severity"],
                float(e["factor"]),
                int(e["long_minutes"]),
                int(e["short_minutes"]),
                int(e.get("min_bad_events", default_guard)),
            )
            for e in document["alerts"]
        )
    except (KeyError, TypeError, ValueError) as error:
        raise InvalidSloError(f"{path}: {error!r}") from error
    known = {journey.name for journey in journeys}
    unknown = {slo.journey for slo in slos} - known
    if unknown:
        raise InvalidSloError(f"{path}: SLOs reference undefined journeys {sorted(unknown)}")
    return SloSpec(window_days, journeys, slos, alerts)
```

`load_spec` turns the parsed YAML into domain objects: the window, the journeys, each `Slo` (the optional `threshold_ms` becomes `None` when absent) and each `BurnAlert` (with a default `min_bad_events` from the file). A missing key, wrong type or bad number (`KeyError`, `TypeError`, `ValueError`) is converted to `InvalidSloError` naming the file, and an SLO that points at an undefined journey is refused too, so a broken definition fails when loaded, not when evaluated.

```python
def _literal_for_multiline(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


class _RuleDumper(yaml.SafeDumper):
    """Dump multi-line expressions as literal blocks so the generated file reads like hand-written rules."""


_RuleDumper.add_representer(str, _literal_for_multiline)


def dump_rules(document: dict[str, Any], header: str) -> str:
    """Serialize a rule document with a generated-file banner."""
    body = yaml.dump(document, Dumper=_RuleDumper, sort_keys=False, width=10_000, allow_unicode=True)
    return "".join(f"# {line}\n" for line in header.splitlines()) + body
```

`_literal_for_multiline` and `_RuleDumper` make PyYAML write a string that contains newlines as a literal block (`|`), so a multi-line PromQL expression in the generated file reads like a hand-written rule. `dump_rules` serializes a rule document without sorting keys, with a very wide line limit and the generated-file banner as comment lines.

```python
DEMO_WINDOW_SCALE = 60.0
BANNER = "Generated by `capacity-slo rules` from slo/slos.yaml. Do not edit; `make rules-check` fails on drift."


def render_rule_files(spec: SloSpec) -> dict[Path, str]:
    """The production and demo Prometheus rule files, as relative path to text."""
    production = rules.build_rules(spec.slos, spec.alerts, 1.0, "", "30s")
    demo = rules.build_rules(spec.slos, spec.alerts, DEMO_WINDOW_SCALE, "demo", "1s")
    return {
        Path("slo/generated/rules.yml"): dump_rules(production, BANNER + "\nProduction profile."),
        Path("slo/generated/rules.demo.yml"): dump_rules(
            demo, BANNER + f"\nDemo profile: every window divided by {DEMO_WINDOW_SCALE:g}."
        ),
    }
```

`DEMO_WINDOW_SCALE = 60.0` divides every window by 60 for the demo profile (so alerts fire in seconds). `BANNER` tells readers the file is generated and that `make rules-check` fails on drift. `render_rule_files` builds the production rules (scale 1, 30-second evaluation interval) and the demo rules (scale 60, 1-second interval, alert names with a `Demo` suffix) and returns the two files as relative path to text.

## Try it yourself

1. `.venv/bin/python -c "from capacity.infrastructure.settings import InstanceSettings; print(InstanceSettings.from_env({}).port)"` prints `8080`.
2. `.venv/bin/python -c "from capacity.infrastructure.settings import InstanceSettings; InstanceSettings.from_env({'PORT':'99999'})"` ends with `SettingsError: PORT must be between 1 and 65535, got 99999`.
3. `.venv/bin/python -c "import logging; from capacity.infrastructure.logging_setup import configure_logging; configure_logging('INFO'); logging.getLogger('demo').info('hello world', extra={'port': 8080})"` prints one logfmt line with `msg="hello world" port=8080`.
4. `.venv/bin/python -m pytest tests/integration -q` runs the adapter tests (they start local servers on free ports).

## Mistakes people make

- Measuring from the moment of sending instead of the scheduled arrival (the coordinated-omission mistake). The generator measures from the schedule.
- Reading a number from a generator that fell behind. The lag is reported next to the samples.
- Letting a typo in an environment variable surface an hour later. Settings are validated at start-up.
- Putting `import aiohttp` in the domain or application layer. `import-linter` fails the build.

## Check yourself

1. Why does the generator keep a bounded semaphore?
   *Answer: to bound requests in flight; a request that waits for a connection still counts that wait in its latency.*
2. Why does the instance export its own `process_cpu_seconds_total`?
   *Answer: the stock collector exists only on Linux; the load test needs the counter on every platform to name the saturated resource.*
3. What does `Compose.run` retry?
   *Answer: only failures whose error text looks like a busy daemon (a transient marker), up to three attempts.*
4. How does `rules_in_sync` decide?
   *Answer: it regenerates both rule files in memory and compares them byte for byte with the committed ones.*
5. What does `pause` do to a service, and why use it?
   *Answer: it freezes the process so it accepts connections but never answers, the worst way for a dependency to fail.*

## Further reading

- `asyncio` streams: <https://docs.python.org/3.12/library/asyncio-stream.html>
- `subprocess.run`: <https://docs.python.org/3.12/library/subprocess.html#subprocess.run>
- `prometheus_client`: <https://prometheus.github.io/client_python/>
- Coordinated omission: <https://www.scylladb.com/2021/04/22/on-coordinated-omission/>
- The twelve-factor app, configuration: <https://12factor.net/config>
