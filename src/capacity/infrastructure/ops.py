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
