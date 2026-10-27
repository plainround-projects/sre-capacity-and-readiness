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
