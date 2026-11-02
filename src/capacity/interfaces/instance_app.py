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
