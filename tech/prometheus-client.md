# prometheus-client 0.22.1

## What it is

`prometheus-client` is the official Python library for **exposing metrics** in the format Prometheus reads. A **metric** is a number a program keeps about itself, such as "requests answered so far". The library gives you objects (`Counter`, `Gauge`, `Histogram`) to update, and a function that prints all current values as text.

- A **Counter** only goes up (it resets to zero when the program restarts). Used for "how many requests".
- A **Gauge** goes up and down. Used for "how many connections are held now".
- A **Histogram** counts observations into buckets ("how many requests took under 0.25 seconds") and also keeps the total count and sum.
- **Labels** are extra name/value tags on a metric, such as `code="200"` and `code="503"`. Each combination of label values is a separate **series**.

## Why this repository uses it

Two kinds of numbers matter here. The knee test needs to know how much of its one core an instance used during a step; it reads `process_cpu_seconds_total` from the instance's own `/metrics`. The ledger's connection pool is the second bottleneck; its gauges (`ledger_pool_in_use`, `ledger_pool_waiting`, `ledger_pool_size`) are what a person watches and what the dashboard and runbook point at. Prometheus scrapes both.

## Where it appears

| File | Use |
| --- | --- |
| `infrastructure/metrics.py` | `ProcessCpuCollector`, `InstanceMetrics` (request counter, latency histogram, in-flight gauge, `app_info`) and `LedgerMetrics` (pool gauges and counters). |
| `domain/telemetry.py` | The metric names, the buckets and `EXPORTED_METRICS`, the set every dashboard query must come from. |
| `infrastructure/ops.py` | The `/metrics` endpoint serves `render()`. |
| `infrastructure/openloop.py` | `MetricsCpuProbe` reads `process_cpu_seconds_total` from that endpoint. |
| `tests/e2e/test_fleet_smoke.py` | Checks every name in `EXPORTED_METRICS` against the running system. |

## How it works underneath

- Every metric belongs to a **registry**, a dictionary of collectors. This repository creates its own registry per object, so tests never share state.
- `generate_latest(registry)` writes the **text exposition format**: lines like `app_requests_total{code="200"} 1520.0`.
- A **Counter** only goes up; a **Gauge** goes up and down; a **Histogram** counts observations into buckets and also keeps `_count` and `_sum`. **Labels** are tags such as `code`; each combination is a separate series.
- A **Collector** is an object with a `collect()` method that produces metric families when asked. It is how a metric can be computed at scrape time.
- `Gauge.set_function(f)` makes the gauge call `f()` when scraped, so the value is never stale.
- `Gauge.track_inprogress()` is a context manager: the gauge goes up when the block starts and down when it ends.

## Walkthrough of this repository's use

### CPU time, on every platform

```python
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

The standard process collector reads `/proc`, which exists only on Linux, so on a Mac the metric would be missing. `ProcessCpuCollector` is a custom collector: `collect` returns a counter family named `process_cpu_seconds` (the library adds `_total`) whose value is `time.process_time()`, the CPU seconds (user plus system) this process has used. The knee test reads it before and after each step; the difference divided by the step length is the share of the core in use (1.0 is the whole core). `CounterMetricFamily` comes from `prometheus_client.core`.

### An instance's metrics

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

- `Counter(APP_REQUESTS_TOTAL, "...", ["code"], registry=...)`: a counter with one label, the status code. `Histogram(APP_DURATION_SECONDS, ..., buckets=APP_LATENCY_BUCKETS_SECONDS)`: the instance's own latency histogram, in seconds. `Gauge(APP_INFLIGHT, ...)`: requests being handled right now (the handler wraps its work in `track_inprogress()`).
- `app_info` is a gauge with the labels `zone` and `instance`, set to 1: the usual way to attach descriptive labels to an instance.
- The final loop creates the `200` and `503` series at 0 up front, so the first increase is visible to `rate()`.
- `observe` does `.inc()` and `.observe(seconds)`; `render` returns the text.

### The ledger's pool

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

`LedgerMetrics` takes two functions: `in_use` and `waiting`, and ties gauges to them with `set_function`. The ledger passes `pool.get_stats()`-based lambdas, so the gauges report the real pool at scrape time. `ledger_pool_size` is a gauge set once. Two counters: requests by code, and `ledger_pool_timeouts_total` (waits that gave up).

### Names the dashboard may use

```python
EDGE_HEALTHY_MEMBERS = 'envoy_cluster_membership_healthy{envoy_cluster_name="app"}'

# The instances' own metrics, scraped per instance.
APP_REQUESTS_TOTAL = "app_requests_total"
APP_DURATION_SECONDS = "app_request_duration_seconds"
APP_INFLIGHT = "app_inflight_requests"
APP_LATENCY_BUCKETS_SECONDS: tuple[float, ...] = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5)
```

`APP_LATENCY_BUCKETS_SECONDS` lists the instance histogram's boundaries (5 ms to 2.5 s). The Envoy names and buckets are in the same file. `EXPORTED_METRICS` (further down) is the complete list; the end-to-end test proves each exists on the running system, and the readiness review proves every dashboard query uses only these.

## Try it yourself

1. With the fleet up: `curl -s localhost:47283/metrics | grep process_cpu_seconds_total` prints a line like `process_cpu_seconds_total 12.34`. Repeat after `curl localhost:47282/work` a few times: it rises.
2. `curl -s localhost:47283/metrics | grep app_info` prints `app_info{instance="a1",zone="a"} 1.0`.
3. `curl -s localhost:47284/metrics | grep ledger_pool` prints the three pool gauges.
4. `.venv/bin/python -c "from capacity.infrastructure.metrics import InstanceMetrics; m=InstanceMetrics('a','a1'); m.observe(200,0.01); print(m.render().decode()[:400])"` prints the exposition text.
5. `.venv/bin/python -m pytest tests/e2e -q -m e2e` (fleet up) checks that every name in `EXPORTED_METRICS` really exists.

## Mistakes people make

- Reading CPU from a library that only works on Linux when developing on a Mac.
- High-cardinality labels. Here labels are only status code, zone and instance.
- Using the global registry in tests.
- Dashboards that query metrics nothing exports. The review catches it.

## Check yourself

1. What does `ProcessCpuCollector` provide and why is it custom?
   *Answer: `process_cpu_seconds_total` from `time.process_time()`, because the stock collector reads `/proc` and works only on Linux.*
2. What does `set_function` do?
   *Answer: the gauge's value is computed by a function at scrape time.*
3. How is the share of the core computed?
   *Answer: (CPU seconds after minus before) divided by the step's seconds.*
4. What is `EXPORTED_METRICS` for?
   *Answer: the set of names a dashboard or alert may use; an end-to-end test checks it against the running system.*
5. Why create the `200` and `503` series at start?
   *Answer: so the first real increase is visible to `rate()`.*

## Further reading

- prometheus-client 0.22 (Python): <https://prometheus.github.io/client_python/>
- Custom collectors: <https://prometheus.github.io/client_python/collector/custom/>
- Exposition format: <https://prometheus.io/docs/instrumenting/exposition_formats/>
