"""An instance, a stand-in ledger and the open-loop generator over real sockets, in one process."""

import asyncio
import time

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from capacity.application.loadtest import knee_test, timed_run
from capacity.domain.cpu import cpu_work
from capacity.infrastructure.metrics import InstanceMetrics
from capacity.infrastructure.openloop import MetricsCpuProbe, RawOpenLoopRunner
from capacity.infrastructure.ops import build_ops_app
from capacity.infrastructure.settings import InstanceSettings
from capacity.interfaces import instance_app

pytestmark = pytest.mark.integration


def settings(ledger_url=None, rounds=200, timeout=0.3):
    return InstanceSettings("b", "b2", "v7", rounds, ledger_url, timeout, 0, 0, "INFO")


async def start(app):
    server = TestServer(app)
    await server.start_server()
    return server


@pytest.fixture
async def instance():
    metrics = InstanceMetrics("b", "b2")
    server = await start(instance_app.build_app(settings(), metrics))
    ops = await start(build_ops_app(metrics.render))
    yield server, ops, metrics
    await server.close()
    await ops.close()


async def get(url):
    async with aiohttp.ClientSession() as session, session.get(url) as response:
        return response.status, dict(response.headers), await response.text()


async def test_an_instance_answers_with_its_zone_instance_and_version(instance):
    server, _, _ = instance
    status, headers, body = await get(f"http://127.0.0.1:{server.port}/work")
    assert status == 200
    assert (headers["X-Zone"], headers["X-Instance"], headers["X-Version"]) == ("b", "b2", "v7")
    assert '"digest"' in body


async def test_each_request_does_real_cpu_work_that_shows_up_in_the_process_cpu_counter(instance):
    server, ops, _ = instance
    probe = MetricsCpuProbe("127.0.0.1", ops.port)
    before = await probe.cpu_seconds()
    for _ in range(40):
        await get(f"http://127.0.0.1:{server.port}/work")
    assert await probe.cpu_seconds() > before


async def test_the_instance_exports_request_and_latency_metrics(instance):
    server, ops, _ = instance
    await get(f"http://127.0.0.1:{server.port}/work")
    _, _, text = await get(f"http://127.0.0.1:{ops.port}/metrics")
    assert 'app_requests_total{code="200"} 1.0' in text
    assert "app_request_duration_seconds_bucket" in text
    assert "app_inflight_requests" in text


class FakeLedger:
    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = 0

    async def handler(self, request):
        self.calls += 1
        if self.behavior == "hang":
            await asyncio.sleep(30)
        if self.behavior == "error":
            return web.json_response({"error": "busy"}, status=503)
        return web.json_response({"id": 1})

    def app(self):
        app = web.Application()
        app.router.add_post("/entries", self.handler)
        return app


async def with_ledger(behavior):
    ledger = FakeLedger(behavior)
    ledger_server = await start(ledger.app())
    metrics = InstanceMetrics("b", "b2")
    server = await start(instance_app.build_app(settings(f"http://127.0.0.1:{ledger_server.port}"), metrics))
    return ledger, ledger_server, server


async def test_a_healthy_ledger_is_called_once_per_request():
    ledger, ledger_server, server = await with_ledger("ok")
    try:
        assert (await get(f"http://127.0.0.1:{server.port}/work"))[0] == 200
        assert ledger.calls == 1
    finally:
        await server.close()
        await ledger_server.close()


async def test_a_ledger_that_answers_503_becomes_a_503_not_a_hang():
    _, ledger_server, server = await with_ledger("error")
    try:
        status, _, body = await get(f"http://127.0.0.1:{server.port}/work")
        assert status == 503
        assert "ledger answered 503" in body
    finally:
        await server.close()
        await ledger_server.close()


async def test_a_frozen_ledger_fails_the_request_within_the_timeout():
    _, ledger_server, server = await with_ledger("hang")
    try:
        started = time.perf_counter()
        status, _, _ = await get(f"http://127.0.0.1:{server.port}/work")
        elapsed = time.perf_counter() - started
        assert status == 503
        assert 0.25 <= elapsed < 0.8
    finally:
        await server.close()
        await ledger_server.close()


async def test_a_dead_ledger_fails_the_request_fast():
    _, ledger_server, server = await with_ledger("ok")
    await ledger_server.close()
    try:
        started = time.perf_counter()
        status, _, body = await get(f"http://127.0.0.1:{server.port}/work")
        assert status == 503
        assert "ledger unavailable" in body
        assert time.perf_counter() - started < 0.3
    finally:
        await server.close()


class TestOpenLoopRunner:
    async def test_it_sends_the_scheduled_number_of_requests_and_counts_them_all(self, instance):
        server, _, _ = instance
        runner = RawOpenLoopRunner("127.0.0.1", server.port)
        samples, lag = await runner.run(rps=200, duration_s=1.0)
        await runner.close()
        assert len(samples) == 200
        assert all(sample.ok for sample in samples)
        assert lag >= 0

    async def test_latency_includes_the_time_a_request_waited_behind_a_busy_server(self):
        """A server that handles one request at a time makes later arrivals queue; an open-loop generator sees it."""
        lock = asyncio.Lock()

        async def slow(_):
            async with lock:
                await asyncio.sleep(0.02)
            return web.json_response({})

        app = web.Application()
        app.router.add_get("/work", slow)
        server = await start(app)
        runner = RawOpenLoopRunner("127.0.0.1", server.port)
        try:
            samples, _ = await runner.run(rps=100, duration_s=1.0)  # 100 rps into a 50 rps server
        finally:
            await runner.close()
            await server.close()
        slowest = max(sample.latency_s for sample in samples)
        assert slowest > 0.3

    async def test_a_dead_port_is_all_errors_not_an_exception(self):
        runner = RawOpenLoopRunner("127.0.0.1", 9, timeout_s=0.5)
        samples, _ = await runner.run(rps=20, duration_s=0.5)
        assert samples
        assert not any(sample.ok for sample in samples)

    async def test_the_knee_test_runs_end_to_end_against_a_real_instance(self, instance):
        server, ops, _ = instance
        runner = RawOpenLoopRunner("127.0.0.1", server.port)
        report = await knee_test(runner, MetricsCpuProbe("127.0.0.1", ops.port), [20, 40, 60, 80], step_seconds=0.5)
        await runner.close()
        assert len(report.steps) == 4
        assert all(step.requests > 0 for step in report.steps)

    async def test_a_timed_run_reports_windows(self, instance):
        server, _, _ = instance
        runner = RawOpenLoopRunner("127.0.0.1", server.port)
        report = await timed_run(runner, "soak", 100, 2.0, slo_p99_ms=1000, window_s=1.0)
        await runner.close()
        assert len(report.window_p99_ms) == 2
        assert report.within_slo


def test_cpu_work_costs_real_time_that_grows_with_the_rounds():
    started = time.perf_counter()
    cpu_work(2000)
    small = time.perf_counter() - started
    started = time.perf_counter()
    cpu_work(40_000)
    assert time.perf_counter() - started > small
