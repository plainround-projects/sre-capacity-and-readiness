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


def build_app(pool: AsyncConnectionPool, metrics: LedgerMetrics, hold_s: float) -> web.Application:
    """The public application."""
    app = web.Application()
    app[POOL], app[METRICS], app[HOLD_S] = pool, metrics, hold_s
    app.router.add_post("/entries", post_entry)
    return app


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

    def stop(*_: object) -> None:
        for child in children:
            child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    for child in children:
        child.join()
    sys.exit(max((child.exitcode or 0) for child in children))
