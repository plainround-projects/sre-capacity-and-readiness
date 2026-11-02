# Python: the interfaces layer, file by file

## What it is

The interfaces layer is `src/capacity/interfaces/`, the outermost layer. It holds the **entry points**: the programs a person or a container starts. There are six: `capacity-instance` (one service instance), `capacity-ledger` (the shared dependency), `capacity-loadtest` (the load generator), `capacity-experiments` (the container experiments), `capacity-slo` (the rule generator) and `capacity` (the plan, the report and the review). An entry point reads its arguments or environment, builds the adapters it needs, calls a use case and prints the result. It contains almost no logic of its own. This note walks through every file.

## Why this repository uses it

Anything that decides something belongs in the domain or application layer where it can be tested without Docker. What is left for this layer is wiring, so it stays short and the end-to-end test and the make targets cover it.

## Where it appears

| File | Command | What it does |
| --- | --- | --- |
| `interfaces/__init__.py` | | Package marker. |
| `interfaces/instance_app.py` | `capacity-instance` | The service: `GET /work` burns real CPU and optionally calls the ledger. |
| `interfaces/ledger_app.py` | `capacity-ledger` | The shared dependency with a hard-limited PostgreSQL pool. |
| `interfaces/loadtest_cli.py` | `capacity-loadtest` | The knee test, the soak and the fleet run. |
| `interfaces/experiments_cli.py` | `capacity-experiments` | Zone loss, full fleet, pool sweep, dependency, rollout, restore. |
| `interfaces/slo_cli.py` | `capacity-slo` | Generate or check the Prometheus rule files. |
| `interfaces/capacity_cli.py` | `capacity` | Size the fleet, write the report, run the readiness review. |

## How it works underneath

`pyproject.toml` lists each command under `[project.scripts]` as `name = "module:function"`; installing the package creates an executable that imports the module and calls the function. `argparse` turns the command line into a namespace, and the handler runs. A web entry point builds an aiohttp `Application`, stores its collaborators under typed `AppKey`s and hands it to `serve`.

## Walkthrough of this repository's use

### `interfaces/__init__.py`

```python
"""Entry points: the instance, the ledger, the load generator, the experiments and the SLO and capacity tools."""
```

### `interfaces/instance_app.py`: one service instance

```python
"""One service instance: real CPU work per request, and optionally a call to the shared ledger."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from collections.abc import AsyncIterator

import aiohttp
import uvloop
from aiohttp import web

from capacity.domain.cpu import cpu_work
from capacity.infrastructure.http_runtime import serve
from capacity.infrastructure.logging_setup import configure_logging
from capacity.infrastructure.metrics import InstanceMetrics
from capacity.infrastructure.ops import build_ops_app
from capacity.infrastructure.settings import InstanceSettings

log = logging.getLogger(__name__)
SETTINGS = web.AppKey("settings", InstanceSettings)
METRICS = web.AppKey("metrics", InstanceMetrics)
SESSION = web.AppKey("session", aiohttp.ClientSession)
RNG = web.AppKey("rng", random.Random)
```

`random` picks the ledger entry's account and amount; `time.perf_counter()` is a high-resolution timer. `web.AppKey(name, type)` is a typed key for the application's shared state, so mypy knows what `app[SETTINGS]` is. The four keys hold the settings, the metrics, the ledger HTTP session and a random generator.

```python
async def work(request: web.Request) -> web.Response:
    """GET /work: burn CPU for the configured number of rounds, then record an entry in the ledger if one is configured.

    The CPU work runs on the event loop on purpose. One instance is one core, so its capacity is one over the cost
    of a request, and a queue that forms behind a busy loop is exactly the saturation the load test must find.
    """
    app = request.app
    settings = app[SETTINGS]
    started = time.perf_counter()
    status, body = 200, {"zone": settings.zone, "instance": settings.instance}
    with app[METRICS].inflight().track_inprogress():
        body["digest"] = cpu_work(settings.work_rounds)[:8]
        if settings.ledger_url is not None:
            status, body = await _record(app, settings.ledger_url, status, body)
    app[METRICS].observe(status, time.perf_counter() - started)
    return web.json_response(
        body,
        status=status,
        headers={"X-Zone": settings.zone, "X-Instance": settings.instance, "X-Version": settings.version},
    )
```

`work` handles `GET /work`. The docstring says why the CPU work runs **on the event loop on purpose**: one instance is one core, so its capacity is one over the cost of a request, and a queue that forms behind a busy loop is exactly the saturation the load test must find. The handler notes the start time, builds the response body (zone and instance), enters the in-flight gauge (`track_inprogress` counts requests being handled), burns CPU with `cpu_work(rounds)` and keeps the first eight characters of the digest, and, when a ledger is configured, calls `_record`. It then records the status and duration and returns the JSON with `X-Zone`, `X-Instance` and `X-Version` headers; the version header is how the rollout test sees which release answered.

```python
async def _record(
    app: web.Application, ledger_url: str, status: int, body: dict[str, str]
) -> tuple[int, dict[str, str]]:
    """One bounded call to the ledger. A refusal, a timeout or a non-200 answer is a fast 503, never a hang."""
    payload = {"account": app[RNG].randint(1, 100), "amount_cents": app[RNG].randint(1, 10_000)}
    try:
        async with app[SESSION].post(f"{ledger_url}/entries", json=payload) as response:
            if response.status == 200:
                return status, body
            return 503, {**body, "error": f"ledger answered {response.status}"}
    except (aiohttp.ClientError, TimeoutError):
        return 503, {**body, "error": "ledger unavailable"}
```

`_record` makes one bounded call to the ledger: it posts a random entry; a 200 leaves the answer as it is, any other status becomes a 503 whose body says what the ledger answered, and a connection error or a timeout becomes a 503 `ledger unavailable`. The point: a broken dependency produces a fast 503, never a hang.

```python
def build_app(settings: InstanceSettings, metrics: InstanceMetrics) -> web.Application:
    """The public application; the ledger session opens and closes with it."""
    app = web.Application()
    app[SETTINGS], app[METRICS], app[RNG] = settings, metrics, random.Random()

    async def session_context(application: web.Application) -> AsyncIterator[None]:
        timeout = aiohttp.ClientTimeout(total=settings.ledger_timeout_s, connect=settings.ledger_timeout_s)
        application[SESSION] = aiohttp.ClientSession(timeout=timeout, connector=aiohttp.TCPConnector(limit=0))
        yield
        await application[SESSION].close()

    app.cleanup_ctx.append(session_context)
    app.router.add_get("/work", work)
    return app
```

`build_app` stores the settings, metrics and a random generator, and registers a `cleanup_ctx` generator, an aiohttp pattern for resources that live as long as the application: before the `yield` it opens an `aiohttp.ClientSession` whose total and connect timeout are the ledger timeout (and whose connection limit is off, `limit=0`); after the `yield` it closes the session. It routes `GET /work` to the handler.

```python
async def run(settings: InstanceSettings) -> None:
    """Build from settings and serve until told to stop; in-flight requests finish before the process exits."""
    metrics = InstanceMetrics(settings.zone, settings.instance)
    draining = {"now": False}
    await serve(
        [
            (build_app(settings, metrics), settings.port),
            (build_ops_app(metrics.render, is_draining=lambda: draining["now"]), settings.ops_port),
        ],
        drain_s=settings.drain_s,
        on_drain=lambda: draining.update(now=True),
    )


def main() -> None:
    """Entry point of `capacity-instance`."""
    settings = InstanceSettings.from_env(os.environ)
    configure_logging(settings.log_level)
    # uvloop cuts the cost of everything around the CPU work (the server, and the call to the ledger). The CPU work
    # itself is unchanged, so the instance still saturates its core where the load test finds it.
    asyncio.run(run(settings), loop_factory=uvloop.new_event_loop)
```

`run` builds the metrics, then serves the public app and the ops app together. `main` reads the settings from `os.environ`, configures logging and starts the event loop with `asyncio.run`.

#### Draining and a faster loop

```python
draining = {"now": False}
await serve(
    [
        (build_app(settings, metrics), settings.port),
        (build_ops_app(metrics.render, is_draining=lambda: draining["now"]), settings.ops_port),
    ],
    drain_s=settings.drain_s,
    on_drain=lambda: draining.update(now=True),
```

`run` keeps a one-item dictionary as a mutable flag. The ops app's `/healthz` reads it; `serve` sets it through `on_drain` when the process is told to stop, and keeps both apps serving for `settings.drain_s` seconds before closing.

```python
# uvloop cuts the cost of everything around the CPU work (the server, and the call to the ledger). The CPU work
# itself is unchanged, so the instance still saturates its core where the load test finds it.
asyncio.run(run(settings), loop_factory=uvloop.new_event_loop)
```

`main` runs the instance on **uvloop**, a faster replacement for asyncio's event loop (see the uvloop note). It cuts the cost of everything around the CPU work (the server, and the call to the ledger); the SHA-256 work itself is unchanged, so the instance still saturates its core where the load test finds it.

### `interfaces/ledger_app.py`: the shared dependency

```python
"""The shared ledger: the dependency every instance calls, with a hard-limited pool of PostgreSQL connections."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import multiprocessing
import os
import signal
import sys
from typing import Any

import uvloop
from aiohttp import web
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from capacity.infrastructure.http_runtime import serve
from capacity.infrastructure.logging_setup import configure_logging
from capacity.infrastructure.metrics import LedgerMetrics
from capacity.infrastructure.ops import build_ops_app
from capacity.infrastructure.settings import LedgerSettings

log = logging.getLogger(__name__)
POOL = web.AppKey("pool", AsyncConnectionPool)
METRICS = web.AppKey("metrics", LedgerMetrics)
HOLD_S = web.AppKey("hold_s", float)

# One statement: hold the connection for the configured time (a stand-in for a heavier query), then append the entry.
_INSERT = (
    "WITH slept AS (SELECT pg_sleep(%(hold)s)) "
    "INSERT INTO entries (account_id, amount_cents) SELECT %(account)s, %(amount)s FROM slept RETURNING id"
)
```

The ledger is the dependency every instance calls, with a hard-limited pool of PostgreSQL connections (`psycopg_pool.AsyncConnectionPool`). `_INSERT` is one SQL statement: a `WITH` clause runs `pg_sleep(hold)` to hold the connection for the configured time (a stand-in for a heavier query), then `INSERT ... SELECT ... FROM slept RETURNING id` appends the entry and returns its id. `%(name)s` are named placeholders that psycopg fills in safely, never by string pasting.

```python
async def post_entry(request: web.Request) -> web.Response:
    """POST /entries {account, amount_cents} -> 200 {id}; 503 when no pooled connection frees up in time."""
    app = request.app
    try:
        body: dict[str, Any] = await request.json()
        params = {"hold": app[HOLD_S], "account": int(body["account"]), "amount": int(body["amount_cents"])}
    except (ValueError, KeyError, TypeError):
        app[METRICS].request(400)
        return web.json_response({"error": "send JSON with account and amount_cents"}, status=400)
    try:
        async with app[POOL].connection() as connection:
            cursor = await connection.execute(_INSERT, params)
            row = await cursor.fetchone()
    except PoolTimeout:
        app[METRICS].timeout()
        app[METRICS].request(503)
        return web.json_response({"error": "ledger busy: pool exhausted"}, status=503)
    except Exception:
        log.exception("ledger insert failed")
        app[METRICS].request(500)
        return web.json_response({"error": "internal error"}, status=500)
    app[METRICS].request(200)
    return web.json_response({"id": row[0] if row else None})
```

`post_entry` handles `POST /entries`. It parses the JSON body and converts the two numbers to integers; bad input is a counted 400. It then borrows a pooled connection (`async with pool.connection()`), runs the insert and fetches the row. If no connection frees up within the pool timeout, `PoolTimeout` becomes a counted pool timeout and a fast 503 `ledger busy: pool exhausted`: the visible symptom of pool exhaustion. Any other exception is logged and returns 500. Success returns `{"id": ...}`.

```python
def build_app(pool: AsyncConnectionPool, metrics: LedgerMetrics, hold_s: float) -> web.Application:
    """The public application."""
    app = web.Application()
    app[POOL], app[METRICS], app[HOLD_S] = pool, metrics, hold_s
    app.router.add_post("/entries", post_entry)
    return app
```

`build_app` stores the pool, metrics and hold time under their keys and routes `POST /entries`.

```python
async def run(settings: LedgerSettings) -> None:
    """Open the pool, serve until told to stop, then close the pool."""
    pool = AsyncConnectionPool(
        settings.database_url,
        min_size=settings.pool_size,
        max_size=settings.pool_size,
        timeout=settings.pool_timeout_s,
        kwargs={"autocommit": True},
        open=False,
    )
    await pool.open(wait=True, timeout=30)

    def in_use() -> float:
        stats = pool.get_stats()
        return float(stats["pool_size"] - stats["pool_available"])

    metrics = LedgerMetrics(in_use, lambda: float(pool.get_stats()["requests_waiting"]), settings.pool_size)

    async def ready() -> bool:
        try:
            async with pool.connection(timeout=1.0) as connection:
                await connection.execute("SELECT 1")
        except Exception:
            return False
        return True

    try:
        await serve(
            [
                (build_app(pool, metrics, settings.hold_ms / 1000.0), settings.port),
                (build_ops_app(metrics.render, ready), settings.ops_port),
            ],
            reuse_port=settings.workers > 1,
        )
    finally:
        await pool.close()
```

`run` creates the pool with the minimum equal to the maximum (so it is exactly `POOL_SIZE` connections), the acquire timeout, autocommit on and `open=False` (it is opened explicitly), then `await pool.open(wait=True, timeout=30)`. `in_use` is the pool size minus the available connections, read from `get_stats()`, and `LedgerMetrics` reads it and `requests_waiting` at scrape time. `ready` is the readiness check: borrow a connection for at most a second and run `SELECT 1`. The `finally` closes the pool when serving stops.

```python
def pool_share(total: int, workers: int, index: int) -> int:
    """Worker `index`'s part of a pool of `total` connections: as even as possible, the parts adding up to `total`."""
    share, extra = divmod(total, workers)
    return share + (1 if index < extra else 0)


def _worker(settings: LedgerSettings, index: int) -> None:
    """One worker process: its share of the pool, its own ops port (9102 + index), the shared public port."""
    configure_logging(settings.log_level)
    own = dataclasses.replace(
        settings, pool_size=pool_share(settings.pool_size, settings.workers, index), ops_port=settings.ops_port + index
    )
    asyncio.run(run(own), loop_factory=uvloop.new_event_loop)


def main() -> None:
    """Entry point of `capacity-ledger`.

    One Python event loop spent a whole core at about 1,750 requests a second, below the projected peak, so the
    ledger runs `WORKERS` processes that share the public port (SO_REUSEPORT). The pool is split between them
    (POOL_SIZE connections in all), each worker has its own ops port, and a stop signal is passed on to all of them.
    """
    settings = LedgerSettings.from_env(os.environ)
    configure_logging(settings.log_level)
    if settings.workers == 1:
        asyncio.run(run(settings), loop_factory=uvloop.new_event_loop)
        return
    context = multiprocessing.get_context("fork")
    children = [context.Process(target=_worker, args=(settings, index)) for index in range(settings.workers)]
    for child in children:
        child.start()
```

`main` reads `LedgerSettings` from the environment and runs the ledger.

#### Several workers behind one port

One Python event loop spent a whole core at about 1,750 requests a second, below the projected peak. The ledger now runs `WORKERS` processes.

```python
def pool_share(total: int, workers: int, index: int) -> int:
    """Worker `index`'s part of a pool of `total` connections: as even as possible, the parts adding up to `total`."""
    share, extra = divmod(total, workers)
    return share + (1 if index < extra else 0)
```

`pool_share(total, workers, index)` splits the pool as evenly as possible with the parts adding up to the total: 25 over two workers is 13 and 12.

```python
def _worker(settings: LedgerSettings, index: int) -> None:
    """One worker process: its share of the pool, its own ops port (9102 + index), the shared public port."""
    configure_logging(settings.log_level)
    own = dataclasses.replace(
        settings, pool_size=pool_share(settings.pool_size, settings.workers, index), ops_port=settings.ops_port + index
    )
    asyncio.run(run(own), loop_factory=uvloop.new_event_loop)
```

`_worker` is the body of one child process: logging, then a copy of the settings with its own pool share and its own ops port (9102 plus its number), then the usual `run` on uvloop.

```python
def main() -> None:
    """Entry point of `capacity-ledger`.

    One Python event loop spent a whole core at about 1,750 requests a second, below the projected peak, so the
    ledger runs `WORKERS` processes that share the public port (SO_REUSEPORT). The pool is split between them
    (POOL_SIZE connections in all), each worker has its own ops port, and a stop signal is passed on to all of them.
    """
    settings = LedgerSettings.from_env(os.environ)
    configure_logging(settings.log_level)
    if settings.workers == 1:
        asyncio.run(run(settings), loop_factory=uvloop.new_event_loop)
        return
    context = multiprocessing.get_context("fork")
    children = [context.Process(target=_worker, args=(settings, index)) for index in range(settings.workers)]
    for child in children:
        child.start()

    def stop(*_: object) -> None:
        for child in children:
            child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    for child in children:
        child.join()
    sys.exit(max((child.exitcode or 0) for child in children))
```

`main` runs a single worker in-process, as before, when `WORKERS` is 1. Otherwise it creates the children with the `fork` start method (each child is a copy of the parent), starts them, installs a handler so that SIGTERM or SIGINT in the parent `terminate`s every child (which gives each its own graceful shutdown), waits for them with `join`, and exits with the largest exit code. All workers bind the public port with `reuse_port`, so the kernel balances new connections between them. Prometheus scrapes both ops ports (`ledger:9102` and `ledger:9103`).

### `interfaces/loadtest_cli.py`: the load generator

```python
"""`capacity-loadtest`: the knee test, the soak and the fleet run. Prints one JSON document on standard output."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit

import uvloop

from capacity.application.loadtest import knee_test, timed_run
from capacity.infrastructure.logging_setup import configure_logging
from capacity.infrastructure.openloop import MetricsCpuProbe, RawOpenLoopRunner

DEFAULT_STEPS = "100,200,300,400,450,500,550,600,650,700,750,800,900,1000,1200,1400,1600"


def _runner(target: str) -> RawOpenLoopRunner:
    parts = urlsplit(target)
    return RawOpenLoopRunner(parts.hostname or "localhost", parts.port or 80)


def _probe(ops_url: str) -> MetricsCpuProbe:
    ops = urlsplit(ops_url)
    return MetricsCpuProbe(ops.hostname or "localhost", ops.port or 9101)


async def _knee(args: argparse.Namespace) -> tuple[dict[str, Any], str | None]:
    """The knee test, repeated up to `--attempts` times until it is trustworthy; the last result and its problem."""
    document: dict[str, Any] = {}
    problem: str | None = None
    for attempt in range(1, args.attempts + 1):
        runner = _runner(args.target)
        steps = [float(value) for value in args.steps.split(",")]
        report = await knee_test(runner, _probe(args.ops), steps, args.step_seconds, args.margin)
        await runner.close()
        document = {
            "experiment": "knee",
            "step_seconds": args.step_seconds,
            "test_attempts": attempt,
            **report.to_dict(),
        }
        problem = report.problem
        if problem is None:
            break
        print(f"knee attempt {attempt} discarded: {problem}", file=sys.stderr)
    return document, problem


async def _timed(args: argparse.Namespace) -> tuple[dict[str, Any], str | None]:
    runner = _runner(args.target)
    probe = _probe(args.ops) if args.ops else None
    report = await timed_run(
        runner, args.label, args.rps, args.seconds, args.slo_p99_ms, args.window, probe, args.attempts
    )
    await runner.close()
    return {"experiment": args.command, **report.to_dict()}, report.problem
```

The program prints one JSON document on standard output. `DEFAULT_STEPS` is the list of target rates for the knee search. `_runner` builds a `RawOpenLoopRunner` from a URL's host and port. `_knee` builds the runner and a CPU probe on the instance's ops port, splits the comma-separated steps into numbers, runs `knee_test`, closes the runner and returns the report with the step length. `_timed` runs a soak or fleet run for the given rate and time.

```python
def build_parser() -> argparse.ArgumentParser:
    """The command-line interface."""
    parser = argparse.ArgumentParser(prog="capacity-loadtest", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    knee = sub.add_parser("knee", help="step the load up against one instance until p99 climbs sharply")
    knee.add_argument("--target", required=True, help="the instance, for example http://app-a1:8080")
    knee.add_argument("--ops", required=True, help="the instance's ops port, for CPU, for example http://app-a1:9101")
    knee.add_argument("--steps", default=DEFAULT_STEPS, help="comma-separated requests per second")
    knee.add_argument("--step-seconds", type=float, default=10.0, help="the lesson uses 120 for the real thing")
    knee.add_argument("--margin", type=float, default=0.25, help="how far below the knee to set the safe load")
    knee.add_argument(
        "--attempts", type=int, default=3, help="repeat the whole test this often until it is trustworthy"
    )
    for name, helptext in (
        ("soak", "hold one instance at the safe load"),
        ("fleet", "load the fleet through the load balancer"),
    ):
        run = sub.add_parser(name, help=helptext)
        run.add_argument("--target", required=True)
        run.add_argument("--rps", type=float, required=True)
        run.add_argument("--seconds", type=float, required=True)
        run.add_argument("--label", default=name)
        run.add_argument("--slo-p99-ms", type=float, default=50.0)
        run.add_argument("--window", type=float, default=10.0, help="seconds per p99 window")
        run.add_argument("--ops", default="", help="the instance's ops port, to tell a saturated core from a busy host")
        run.add_argument(
            "--attempts", type=int, default=3, help="run again this often when a window spiked with no errors"
        )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point of `capacity-loadtest`."""
    configure_logging("WARNING")
    args = build_parser().parse_args(argv)
    # uvloop keeps the generator's own scheduling delay small at 2,000 requests a second.
    result, problem = asyncio.run(
        _knee(args) if args.command == "knee" else _timed(args), loop_factory=uvloop.new_event_loop
    )
    if problem is not None:
        sys.exit(f"not a valid measurement, nothing written: {problem}. Run again when the machine is quiet.")
    print(json.dumps(result, indent=2))
```

`build_parser` defines three subcommands: `knee` (with `--target`, `--ops`, `--steps`, `--step-seconds` and `--margin`) and `soak` and `fleet`, which share `--target`, `--rps`, `--seconds`, `--label`, `--slo-p99-ms` and `--window`. `main` runs the chosen one with `asyncio.run` and, if the generator's own p99 lag is over `GENERATOR_LAG_LIMIT_MS`, prints a warning on standard error that the numbers include the generator's own queueing, then prints the JSON.

#### The generator on uvloop

```python
# uvloop keeps the generator's own scheduling delay small at 2,000 requests a second.
result, problem = asyncio.run(
    _knee(args) if args.command == "knee" else _timed(args), loop_factory=uvloop.new_event_loop
```

`asyncio.run` accepts a `loop_factory` since Python 3.12; passing `uvloop.new_event_loop` keeps the generator's own scheduling delay small at 2,000 requests a second.

#### Refusing to save a measurement that cannot be trusted

```python
def _probe(ops_url: str) -> MetricsCpuProbe:
    ops = urlsplit(ops_url)
    return MetricsCpuProbe(ops.hostname or "localhost", ops.port or 9101)
```

`_probe` builds the CPU probe from the instance's ops URL.

```python
async def _knee(args: argparse.Namespace) -> tuple[dict[str, Any], str | None]:
    """The knee test, repeated up to `--attempts` times until it is trustworthy; the last result and its problem."""
    document: dict[str, Any] = {}
    problem: str | None = None
    for attempt in range(1, args.attempts + 1):
        runner = _runner(args.target)
        steps = [float(value) for value in args.steps.split(",")]
        report = await knee_test(runner, _probe(args.ops), steps, args.step_seconds, args.margin)
        await runner.close()
        document = {
            "experiment": "knee",
            "step_seconds": args.step_seconds,
            "test_attempts": attempt,
            **report.to_dict(),
        }
        problem = report.problem
        if problem is None:
            break
        print(f"knee attempt {attempt} discarded: {problem}", file=sys.stderr)
    return document, problem
```

`_knee` repeats the whole knee test, up to `--attempts` times (three by default), until `KneeReport.problem` is `None`. Each discarded attempt prints why on standard error. It returns the last document and its problem.

```python
async def _timed(args: argparse.Namespace) -> tuple[dict[str, Any], str | None]:
    runner = _runner(args.target)
    probe = _probe(args.ops) if args.ops else None
    report = await timed_run(
        runner, args.label, args.rps, args.seconds, args.slo_p99_ms, args.window, probe, args.attempts
    )
    await runner.close()
    return {"experiment": args.command, **report.to_dict()}, report.problem
```

`_timed` builds the probe only if `--ops` was given and passes the attempts to `timed_run`.

```python
# uvloop keeps the generator's own scheduling delay small at 2,000 requests a second.
result, problem = asyncio.run(
    _knee(args) if args.command == "knee" else _timed(args), loop_factory=uvloop.new_event_loop
)
if problem is not None:
    sys.exit(f"not a valid measurement, nothing written: {problem}. Run again when the machine is quiet.")
print(json.dumps(result, indent=2))
```

`main` exits with a message and prints **nothing on standard output** when the measurement is not valid, so the Makefile's redirect leaves no evidence. `--attempts` (knee and the timed runs) and `--ops` (the timed runs) are the new options; the parser lines were quoted above.

### `interfaces/slo_cli.py`: the rule generator

```python
"""`capacity-slo`: generate the Prometheus SLO rules from slo/slos.yaml, or check the committed files."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from capacity.infrastructure.slo_files import load_spec, render_rule_files


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point of `capacity-slo`."""
    parser = argparse.ArgumentParser(prog="capacity-slo", description=__doc__)
    parser.add_argument("--slos", default="slo/slos.yaml")
    parser.add_argument("command", choices=["rules"])
    parser.add_argument("--check", action="store_true", help="fail instead of writing when a file is out of date")
    args = parser.parse_args(argv)
    drift = 0
    for path, text in render_rule_files(load_spec(Path(args.slos))).items():
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                print(f"{path} is out of date; run `make rules`", file=sys.stderr)
                drift = 1
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            print(f"wrote {path}")
    sys.exit(drift)
```

`capacity-slo rules` renders the production and demo rule files from `slo/slos.yaml`. With `--check` it writes nothing and exits 1 (printing which file) when a committed file is missing or differs from what the definitions generate; without it, it writes each file, creating the folder. `sys.exit(drift)` turns the drift flag into the exit status the make target and CI read.

### `interfaces/experiments_cli.py`: the container experiments

```python
"""`capacity-experiments`: the experiments that need containers stopped, paused, recreated and loaded.

Each experiment prints what it measured and writes it to `docs/evidence/<name>.json`, which the readiness review and
the tests read. Everything here drives the running stack from the host.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from capacity.application.experiments import (
    choose_pool_size,
    fleet_instances,
    naive_pool_size,
    spare_instances,
    sweep_sizes,
)
from capacity.infrastructure.compose import Compose, versions_seen, wait_for_members
from capacity.infrastructure.logging_setup import configure_logging
from capacity.infrastructure.project_files import JsonEvidence

MAX_LOAD_PER_CPU = 0.6  # the one-minute load average per CPU above which the host is too busy to measure on
LEDGER_URL = "http://ledger:8082"
SLO_P99_MS = "50"
FLEET = "http://lb:8080"

```

The docstring: each experiment prints what it measured and writes it to `docs/evidence/<name>.json`, which the readiness review and the tests read, and everything here drives the running stack from the host. The constants name the stack: the host load per CPU above which `preflight` refuses to measure, the ledger's URL inside the compose network, the 50 ms SLO and the load balancer's URL. Which instances run, and the peak they must carry, now come from the plan (`PlannedFleet`, below).

```python
def _say(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class PlannedFleet:
    """The fleet `make plan` decided on, read from its evidence: which instances run and the peak they must carry."""

    def __init__(self, evidence: JsonEvidence) -> None:
        """Read `plan.json`; exit with a message when the plan has not been made."""
        plan = evidence.load("plan")
        if plan is None:
            sys.exit("no plan yet: run `make plan` (after `make knee`) first")
        distribution = [int(str(size)) for size in plan["distribution"]]  # type: ignore[attr-defined]
        self.instances = fleet_instances(distribution)
        self.zone_a = fleet_instances(distribution[:1])
        self.spares = spare_instances(distribution)
        self.peak_rps = float(str(plan["future_peak_rps"]))

    @property
    def size(self) -> int:
        """Instances in the fleet."""
        return len(self.instances)


def preflight() -> dict[str, Any]:
    """Refuse to measure on a busy machine: the numbers would describe the machine, not the service."""
    load = os.getloadavg()[0]
    limit = MAX_LOAD_PER_CPU * (os.cpu_count() or 1)
    if load > float(os.environ.get("MAX_LOAD", limit)):
        sys.exit(
            f"the machine is busy: one-minute load {load:.1f} is above {limit:.1f}. Measurements taken now would "
            "describe the machine, not the service. Stop other work and run again (or set MAX_LOAD to accept it)."
        )
    return {"experiment": "preflight", "load_1min": round(load, 2), "limit": round(limit, 2)}


def _fleet(compose: Compose, label: str, rps: float, seconds: float) -> dict[str, Any]:
    return compose.loadtest(
        "fleet",
        "--target",
        FLEET,
        "--rps",
        str(rps),
        "--seconds",
        str(seconds),
        "--label",
        label,
        "--slo-p99-ms",
        SLO_P99_MS,
    )
```

`_say` prints progress on standard error (keeping standard output for the JSON). `_fleet` runs a fleet load test through the load balancer at the given rate and time with the 50 ms SLO.

```python
def set_fleet(compose: Compose, evidence: JsonEvidence) -> dict[str, Any]:
    """Run exactly the instances the plan says: start those it names, stop the spares, wait for the balancer."""
    fleet = PlannedFleet(evidence)
    _say(f"running {fleet.size} instances; spares: {', '.join(fleet.spares) or 'none'}")
    if fleet.spares:
        compose.stop(fleet.spares)
    compose.start(fleet.instances)
    wait_for_members(fleet.size)
    return {"experiment": "set-fleet", "instances": fleet.instances, "spares": fleet.spares}


def zone_loss(compose: Compose, evidence: JsonEvidence, seconds: float) -> dict[str, Any]:
    """Lose the largest zone, wait until the balancer has noticed, and run the projected peak on the survivors."""
    fleet = PlannedFleet(evidence)
    survivors = fleet.size - len(fleet.zone_a)
    _say(f"stopping zone a (the largest zone, {len(fleet.zone_a)} instances)")
    compose.stop(fleet.zone_a)
    try:
        wait_for_members(survivors)
        report = _fleet(compose, "zone-a-down", fleet.peak_rps, seconds)
    finally:
        compose.start(fleet.zone_a)
        wait_for_members(fleet.size)
    report["healthy_instances"] = survivors
    evidence.save("fleet-zone-loss", report)
    return report


def full_fleet(compose: Compose, evidence: JsonEvidence, seconds: float) -> dict[str, Any]:
    """The whole planned fleet at the projected peak with no shared dependency: the CPU-only baseline."""
    fleet = PlannedFleet(evidence)
    report = _fleet(compose, f"fleet-{fleet.size}", fleet.peak_rps, seconds)
    report["healthy_instances"] = fleet.size
    evidence.save("fleet-full", report)
    return report
```

`zone_loss` reads the planned fleet, stops zone A (the largest zone), waits until the balancer counts exactly the survivors (the fleet minus zone A), runs the projected peak on them and, in a `finally`, restarts the zone and waits for the whole fleet, so a failed run still leaves the stack whole. `full_fleet` runs the same peak on the whole planned fleet with no shared dependency: the CPU-only baseline. `set_fleet` (above them) starts the instances the plan names and stops the spares.

```python
def pool_sweep(compose: Compose, evidence: JsonEvidence, seconds: float, hold_ms: float) -> dict[str, Any]:
    """Reintroduce the shared ledger and step its connection pool up from the naive size until the SLO holds."""
    fleet = PlannedFleet(evidence)
    _say(f"linking all {fleet.size} instances to the ledger")
    compose.recreate(fleet.instances, env={"LEDGER_URL": LEDGER_URL})
    wait_for_members(fleet.size)
    naive = naive_pool_size(fleet.peak_rps, hold_ms)
    results: list[dict[str, Any]] = []
    for size in sweep_sizes(naive):
        _say(f"ledger pool of {size}")
        compose.recreate(["ledger"], env={"POOL_SIZE": str(size), "LEDGER_URL": LEDGER_URL})
        report = _fleet(compose, f"pool-{size}", fleet.peak_rps, seconds)
        report["pool_size"] = size
        results.append(report)
    chosen = choose_pool_size(results)
    if chosen is None:
        sys.exit("no pool size in the sweep held the SLO; extend the sweep")
    _say(f"adopting a pool of {chosen}: the smallest that held, plus 25 percent headroom")
    compose.recreate(["ledger"], env={"POOL_SIZE": str(chosen), "LEDGER_URL": LEDGER_URL})
    resized = _fleet(compose, f"pool-{chosen}", fleet.peak_rps, seconds)
    resized["pool_size"] = chosen
    evidence.save("fleet-pool-naive", results[0])
    evidence.save("fleet-pool-resized", resized)
    summary = {
        "naive_pool_size": naive,
        "hold_ms": hold_ms,
        "peak_rps": fleet.peak_rps,
        "chosen_pool_size": chosen,
        "sweep": results,
    }
    evidence.save("pool-sweep", summary)
    return summary
```

`pool_sweep` reintroduces the ledger: it links all the planned instances to it, computes the naive pool size from the plan's projected peak, and for each size in the sweep recreates the ledger with that `POOL_SIZE` and runs the peak. It chooses the smallest size that held plus 25% (or exits with a message that no size held), re-runs the chosen size to confirm, saves the naive result, the resized result and the whole sweep as evidence.

```python
def dependency(compose: Compose, evidence: JsonEvidence, ledger_timeout_s: float) -> dict[str, Any]:
    """Freeze the ledger (it accepts connections and never answers) and check that requests fail within a bound."""
    compose.recreate(PlannedFleet(evidence).instances[:1], env={"LEDGER_URL": LEDGER_URL})
    compose.pause("ledger")
    try:
        report = compose.loadtest(
            "fleet",
            "--target",
            "http://app-a1:8080",
            "--rps",
            "20",
            "--seconds",
            "6",
            "--label",
            "ledger-frozen",
            "--window",
            "2",
        )
        healthy = _get("http://localhost:47283/healthz")
    finally:
        compose.unpause("ledger")
    bound_ms = ledger_timeout_s * 1000 + 250
    document = {
        "experiment": "dependency",
        "requests": report["requests"],
        "errors": report["errors"],
        "slowest_503_ms": report["max_ms"],
        "bound_ms": bound_ms,
        "fail_fast": report["errors"] == report["requests"] and report["max_ms"] <= bound_ms,
        "instance_healthy": healthy,
    }
    evidence.save("dependency", document)
    return document
```

`dependency` links one instance to the ledger, freezes the ledger with `pause` (it accepts connections and never answers), and sends 20 requests a second for 6 seconds. It then asks the instance's health endpoint whether it is still healthy, and unfreezes the ledger in a `finally`. The pass condition is `fail_fast`: every request failed **and** the slowest failure took no longer than the ledger timeout plus 250 ms. The instance must also stay healthy.

```python
def rollout(compose: Compose, evidence: JsonEvidence, rps: float) -> dict[str, Any]:
    """Roll every instance to v2 one at a time under load, then roll back to v1 the same way, and count failures."""
    fleet = PlannedFleet(evidence)
    compose.recreate(fleet.instances, env={"APP_VERSION": "v1", "LEDGER_URL": LEDGER_URL})
    wait_for_members(fleet.size)
    seconds = 40 + 2 * fleet.size * 12
    load = compose.start_loadtest(
        "fleet", "--target", FLEET, "--rps", str(rps), "--seconds", str(seconds), "--label", "rollout"
    )
    time.sleep(3)
    restarted = 0
    for version in ("v2", "v1"):
        _say(f"rolling to {version}, one instance at a time")
        for service in fleet.instances:
            compose.recreate([service], env={"APP_VERSION": version, "LEDGER_URL": LEDGER_URL})
            wait_for_members(fleet.size)
            restarted += 1
        seen = versions_seen("http://localhost:47280", 60)
        _say(f"versions answering after the roll to {version}: {sorted(seen)}")
        if seen != {version}:
            load.kill()
            sys.exit(f"after rolling to {version} the fleet still answered with {sorted(seen)}")
    stdout, stderr = load.communicate(timeout=seconds + 120)
    if load.returncode != 0:
        sys.exit(f"the load generator failed: {stderr.decode(errors='replace')[-500:]}")
    report: dict[str, Any] = json.loads(stdout)
    document = {
        "experiment": "rollout",
        "restarted": restarted,
        "rps": rps,
        "seconds": seconds,
        "requests": report["requests"],
        "errors": report["errors"],
        "p99_ms": report["p99_ms"],
        "within_slo": report["within_slo"],
        "rollback_ok": True,
    }
    evidence.save("rollout", document)
    return document
```

`rollout` puts all instances on version v1 and starts a load generator in the background for long enough to cover the whole roll. It then recreates the instances one at a time, first to v2 and then back to v1, waiting for the whole planned fleet to be healthy after each, and after each full roll samples 60 responses to check that only the expected version answers (otherwise it kills the load and exits). It then reads the load generator's report and saves the restart count, the error count, the p99 and whether it stayed within the SLO.

```python
def restore(compose: Compose, evidence: JsonEvidence, rto_target_s: float) -> dict[str, Any]:
    """Back the ledger up, restore the copy into a scratch database and compare it with the source row by row."""
    query = (
        "SELECT count(*) || ' ' || md5(string_agg(id || ':' || account_id || ':' || amount_cents, ',' ORDER BY id)) "
        "FROM entries"
    )

    def fingerprint(database: str) -> tuple[int, str]:
        rows, checksum = compose.db("psql", "-U", "postgres", "-d", database, "-At", "-c", query).decode().split()
        return int(rows), checksum

    source = fingerprint("ledger")
    started = time.perf_counter()
    dump = compose.db("pg_dump", "-U", "postgres", "-Fc", "ledger")
    backup_s = time.perf_counter() - started
    compose.db("psql", "-U", "postgres", "-c", "DROP DATABASE IF EXISTS ledger_restore")
    compose.db("psql", "-U", "postgres", "-c", "CREATE DATABASE ledger_restore")
    started = time.perf_counter()
    compose.db("pg_restore", "-U", "postgres", "-d", "ledger_restore", "--no-owner", "--no-privileges", stdin=dump)
    restore_s = time.perf_counter() - started
    restored = fingerprint("ledger_restore")
    compose.db("psql", "-U", "postgres", "-c", "DROP DATABASE ledger_restore")
    document = {
        "experiment": "restore",
        "rows": restored[0],
        "source_rows": source[0],
        "checksum": restored[1],
        "matched": restored == source,
        "dump_bytes": len(dump),
        "backup_seconds": round(backup_s, 2),
        "restore_seconds": round(restore_s, 2),
        "rto_target_seconds": rto_target_s,
    }
    evidence.save("restore", document)
    return document
```

`restore` is the backup test. `query` returns the row count and an MD5 checksum of every row in `entries`, ordered by id, in one line; `fingerprint(database)` runs it with `psql` inside the database container and returns `(rows, checksum)`. The experiment fingerprints the source, takes a custom-format `pg_dump`, creates a scratch database, restores into it with `pg_restore`, fingerprints the copy, drops the scratch database and saves whether the two fingerprints match, the dump size and the times.

```python
def _get(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return bool(response.status == 200)
    except (urllib.error.URLError, TimeoutError):
        return False


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point of `capacity-experiments`."""
    configure_logging("WARNING")
    parser = argparse.ArgumentParser(prog="capacity-experiments", description=__doc__)
    parser.add_argument("--project", default=".")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight")
    sub.add_parser("set-fleet")
    for name in ("zone-loss", "full-fleet"):
        sub.add_parser(name).add_argument("--seconds", type=float, default=40.0)
    pool = sub.add_parser("pool-sweep")
    pool.add_argument("--seconds", type=float, default=30.0)
    pool.add_argument("--hold-ms", type=float, default=10.0)
    sub.add_parser("dependency").add_argument("--ledger-timeout-s", type=float, default=0.5)
    sub.add_parser("rollout").add_argument("--rps", type=float, default=1000.0)
    sub.add_parser("restore").add_argument("--rto-target-s", type=float, default=300.0)
    args = parser.parse_args(argv)
    project = Path(args.project).resolve()
    compose, evidence = Compose(project), JsonEvidence(project / "docs" / "evidence")
    if args.command == "preflight":
        result = preflight()
    elif args.command == "set-fleet":
        result = set_fleet(compose, evidence)
    elif args.command == "zone-loss":
        result = zone_loss(compose, evidence, args.seconds)
    elif args.command == "full-fleet":
        result = full_fleet(compose, evidence, args.seconds)
    elif args.command == "pool-sweep":
        result = pool_sweep(compose, evidence, args.seconds, args.hold_ms)
    elif args.command == "dependency":
        result = dependency(compose, evidence, args.ledger_timeout_s)
    elif args.command == "rollout":
        result = rollout(compose, evidence, args.rps)
    else:
        result = restore(compose, evidence, args.rto_target_s)
    print(json.dumps(result, indent=2))
```

`_get` returns whether a URL answers 200 within 3 seconds. `main` defines one subcommand per experiment with its options, builds the `Compose` and the evidence store under `docs/evidence` and prints the chosen experiment's result as JSON.

#### The plan decides which instances run

```python
class PlannedFleet:
    """The fleet `make plan` decided on, read from its evidence: which instances run and the peak they must carry."""

    def __init__(self, evidence: JsonEvidence) -> None:
        """Read `plan.json`; exit with a message when the plan has not been made."""
        plan = evidence.load("plan")
        if plan is None:
            sys.exit("no plan yet: run `make plan` (after `make knee`) first")
        distribution = [int(str(size)) for size in plan["distribution"]]  # type: ignore[attr-defined]
        self.instances = fleet_instances(distribution)
        self.zone_a = fleet_instances(distribution[:1])
        self.spares = spare_instances(distribution)
        self.peak_rps = float(str(plan["future_peak_rps"]))

    @property
    def size(self) -> int:
        """Instances in the fleet."""
        return len(self.instances)
```

`PlannedFleet` reads `docs/evidence/plan.json` (exiting with "no plan yet" if there is none) and exposes the instances to run (`fleet_instances` of the plan's distribution), zone A's instances (the largest zone), the spares and the projected peak. Every experiment now builds one instead of using fixed lists of eight names and a fixed 2,000 requests a second.

```python
def preflight() -> dict[str, Any]:
    """Refuse to measure on a busy machine: the numbers would describe the machine, not the service."""
    load = os.getloadavg()[0]
    limit = MAX_LOAD_PER_CPU * (os.cpu_count() or 1)
    if load > float(os.environ.get("MAX_LOAD", limit)):
        sys.exit(
            f"the machine is busy: one-minute load {load:.1f} is above {limit:.1f}. Measurements taken now would "
            "describe the machine, not the service. Stop other work and run again (or set MAX_LOAD to accept it)."
        )
    return {"experiment": "preflight", "load_1min": round(load, 2), "limit": round(limit, 2)}
```

`preflight` compares the one-minute load average (`os.getloadavg()[0]`) with `MAX_LOAD_PER_CPU` (0.6) times the CPU count, or with the `MAX_LOAD` environment variable when set, and exits with a message when the machine is busier. Otherwise it returns the numbers it saw.

```python
def set_fleet(compose: Compose, evidence: JsonEvidence) -> dict[str, Any]:
    """Run exactly the instances the plan says: start those it names, stop the spares, wait for the balancer."""
    fleet = PlannedFleet(evidence)
    _say(f"running {fleet.size} instances; spares: {', '.join(fleet.spares) or 'none'}")
    if fleet.spares:
        compose.stop(fleet.spares)
    compose.start(fleet.instances)
    wait_for_members(fleet.size)
    return {"experiment": "set-fleet", "instances": fleet.instances, "spares": fleet.spares}
```

`set_fleet` stops the spare instances, starts the planned ones and waits until Envoy counts exactly the fleet's size. `main` has two new subcommands, `preflight` and `set-fleet`.

### `interfaces/capacity_cli.py`: the plan, the report and the review

```python
"""`capacity`: the capacity plan from the measured safe load, and the readiness review."""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from capacity.application import capacity_report, launch_decision
from capacity.application.experiments import COMPOSE_FLEET
from capacity.application.readiness import Reviewer
from capacity.domain.plan import plan_capacity
from capacity.infrastructure.logging_setup import configure_logging
from capacity.infrastructure.project_files import FileSystemProject, JsonEvidence, SloFilesCatalog, YamlChecklist
from capacity.infrastructure.slo_files import render_fleet_files

_MEASURED = re.compile(r"<!-- measured -->.*?<!-- /measured -->", re.DOTALL)
PEAK_RPS = 1000.0
GROWTH = 2.0
ZONES = 3

```

`_MEASURED` is a regular expression that finds the text between `<!-- measured -->` and `<!-- /measured -->` (with `DOTALL`, so `.` also matches line breaks). The constants are the lesson's facts: a peak of 1,000, growth of 2, three zones and the nine instances `docker-compose.yml` defines (the plan may need fewer; it must not need more).

```python
def cmd_plan(args: argparse.Namespace) -> int:
    """Size the fleet from the measured safe load, save it as evidence, and say if the lost-zone run confirmed it."""
    evidence = JsonEvidence(Path(args.evidence))
    knee = evidence.load("knee")
    safe = (
        float(args.safe_rps)
        if args.safe_rps
        else (float(str(knee["safe_rps"])) if knee and knee.get("safe_rps") else 0.0)
    )
    if safe <= 0:
        sys.exit("no safe load: run `make knee` first or pass --safe-rps")
    plan = plan_capacity(args.peak, args.growth, safe, ZONES)
    print(plan.formula())
    zone_run = evidence.load("fleet-zone-loss")
    verified = bool(zone_run and zone_run.get("within_slo") is True)
    evidence.save(
        "plan",
        {
            "current_peak_rps": plan.current_peak_rps,
            "growth_factor": plan.growth_factor,
            "safe_rps_per_instance": plan.safe_rps_per_instance,
            "future_peak_rps": plan.future_peak_rps,
            "instances_for_peak": plan.instances_for_peak,
            "total_instances": plan.total_instances,
            "distribution": list(plan.distribution),
            "surviving_after_zone_loss": plan.surviving_after_zone_loss,
            "formula": plan.formula(),
            "verified_by_lost_zone_run": verified,
        },
    )
    print(f"verified by running {plan.surviving_after_zone_loss} instances at {plan.future_peak_rps:g} rps: {verified}")
    for path, text in render_fleet_files(
        plan.instances_for_peak, plan.total_instances, plan.surviving_after_zone_loss
    ).items():
        (Path(args.root) / path).write_text(text, encoding="utf-8")
    print("wrote the fleet alert thresholds: page below", plan.instances_for_peak, "ticket below", plan.total_instances)
    if plan.total_instances > COMPOSE_FLEET:
        print(
            f"the plan needs {plan.total_instances} instances but docker-compose.yml defines {COMPOSE_FLEET}: add "
            "instances there (and to deploy/envoy.yaml and the fleet alert) before trusting the fleet runs",
            file=sys.stderr,
        )
        return 1
    return 0
```

`cmd_plan` loads the measured safe load (from `--safe-rps` or the knee evidence; exits with a message if there is none), computes the plan, prints its formula and saves everything as evidence, including whether the lost-zone run confirmed it (`verified_by_lost_zone_run`). If the plan needs more instances than `docker-compose.yml` defines it prints how to add them and returns 1.

```python
def cmd_report(args: argparse.Namespace) -> int:
    """Write docs/capacity-plan.md from the evidence."""
    root = Path(args.root)
    evidence = JsonEvidence(root / "docs" / "evidence")
    absent = capacity_report.missing(evidence)
    if absent:
        sys.exit(f"missing evidence: {', '.join(absent)}; run `make run`")
    (root / "docs" / "capacity-plan.md").write_text(capacity_report.render(evidence), encoding="utf-8")
    for name, facts in capacity_report.runbook_facts(evidence).items():
        path = root / "docs" / "runbooks" / name
        block = f"<!-- measured -->\n{facts}\n<!-- /measured -->"
        path.write_text(_MEASURED.sub(block.replace("\\", "\\\\"), path.read_text(encoding="utf-8")), encoding="utf-8")
    print("wrote docs/capacity-plan.md and the measured facts in the runbooks")
    return 0
```

`cmd_report` refuses to run while evidence is missing (and names it), writes `docs/capacity-plan.md` and, for each runbook, replaces the text between the `measured` markers with the facts from the evidence, so a runbook always quotes the measurement.

```python
def cmd_review(args: argparse.Namespace) -> int:
    """Run the readiness review; with --write, write docs/launch-decision.md."""
    root = Path(args.root)
    reviewer = Reviewer(
        FileSystemProject(root),
        JsonEvidence(root / "docs" / "evidence"),
        SloFilesCatalog(root),
        YamlChecklist(root / "readiness" / "checklist.yaml"),
    )
    items, decision = reviewer.review()
    print(launch_decision.console(items, decision))
    if args.write:
        (root / "docs" / "launch-decision.md").write_text(launch_decision.markdown(items, decision), encoding="utf-8")
        print("wrote docs/launch-decision.md")
    return 0
```

`cmd_review` wires the four adapters into a `Reviewer`, prints the console report and, with `--write`, writes `docs/launch-decision.md`.

```python
def build_parser() -> argparse.ArgumentParser:
    """The command-line interface."""
    parser = argparse.ArgumentParser(prog="capacity", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="size the fleet from the measured safe load")
    plan.add_argument("--evidence", default="docs/evidence")
    plan.add_argument("--root", default=".", help="the repository, where the fleet alert files are written")
    plan.add_argument("--peak", type=float, default=PEAK_RPS, help="today's peak requests per second")
    plan.add_argument("--growth", type=float, default=GROWTH, help="growth factor over six months")
    plan.add_argument("--safe-rps", type=float, default=0.0, help="override the measured safe load per instance")
    plan.set_defaults(handler=cmd_plan)
    report = sub.add_parser("report", help="write docs/capacity-plan.md from the evidence")
    report.add_argument("--root", default=".")
    report.set_defaults(handler=cmd_report)
    review = sub.add_parser("review", help="run the readiness review")
    review.add_argument("--root", default=".")
    review.add_argument("--write", action="store_true", help="write docs/launch-decision.md")
    review.set_defaults(handler=cmd_review)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point of `capacity`."""
    configure_logging("WARNING")
    args = build_parser().parse_args(argv)
    sys.exit(args.handler(args))

```

`build_parser` defines `plan`, `report` and `review` with their options, each setting its `handler` default. `main` configures logging, parses the arguments and exits with the handler's status.

#### The plan also writes the fleet alert thresholds

```python
for path, text in render_fleet_files(
    plan.instances_for_peak, plan.total_instances, plan.surviving_after_zone_loss
).items():
    (Path(args.root) / path).write_text(text, encoding="utf-8")
print("wrote the fleet alert thresholds: page below", plan.instances_for_peak, "ticket below", plan.total_instances)
```

After saving the plan, `cmd_plan` renders the fleet alert rules and their promtool replay for the plan's numbers and writes them under `--root` (the repository, `.` by default). `COMPOSE_FLEET` now comes from the application layer and is 9, so the command only fails when the plan needs more than nine instances.

## Try it yourself

1. `.venv/bin/capacity-slo rules --check` exits 0 and prints nothing when the committed rule files are current.
2. `.venv/bin/capacity --help` lists `plan`, `report` and `review`.
3. `.venv/bin/capacity-loadtest knee --help` shows the knee options and their defaults.
4. `.venv/bin/python -m pytest tests/integration/test_instance_and_loadgen.py -q` starts an instance in-process and drives it with the generator.

## Mistakes people make

- Putting a decision in the entry point, where only Docker can test it. Decisions live in the application layer.
- A handler that hangs when a dependency is slow. `_record` bounds the call and turns every failure into a fast 503.
- Running an experiment that leaves the stack broken when it fails. Zone loss, the dependency freeze and the pool sweep restore state in `finally`.
- Quoting a number in a runbook by hand. `capacity report` writes it from the evidence.

## Check yourself

1. Why does the CPU work run on the event loop?
   *Answer: one instance is one core, so the queue behind a busy loop is the saturation the test must find.*
2. What does the instance return when the ledger times out?
   *Answer: a fast 503 with `ledger unavailable`.*
3. What makes `fail_fast` true in the dependency experiment?
   *Answer: every request failed, and the slowest failure took no longer than the ledger timeout plus 250 ms.*
4. How does `capacity report` keep runbooks honest?
   *Answer: it rewrites the text between the `measured` markers from the evidence.*
5. What does `capacity-slo rules --check` do?
   *Answer: it writes nothing and exits 1 if a committed rule file differs from what the definitions generate.*

## Further reading

- `argparse`: <https://docs.python.org/3.12/library/argparse.html>
- aiohttp server, `cleanup_ctx` and `AppKey`: <https://docs.aiohttp.org/en/stable/web_advanced.html>
- `psycopg_pool`: <https://www.psycopg.org/psycopg3/docs/advanced/pool.html>
- PostgreSQL `pg_dump` and `pg_restore`: <https://www.postgresql.org/docs/current/app-pgdump.html>
