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
