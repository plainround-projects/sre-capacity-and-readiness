# Grafana dashboard JSON

## What it is

Grafana is a program that draws charts from monitoring data, including Prometheus. A **dashboard** is a page of **panels** (charts). Grafana stores a dashboard as a JSON document: a title, a time range, a refresh interval and a list of panels, where each panel has a type, a position on a grid, a unit, and one or more **queries** (**targets**) written in the data source's own language. For Prometheus that language is PromQL. A dashboard file can be imported into any Grafana.

## Why this repository uses it

The readiness review asks for a dashboard whose every panel shows something the system really exports. The repository does not run a Grafana server; it commits the dashboard as a file, and the review (a program) checks the file. That keeps the dashboard as reviewable as code and lets a real Grafana import the same file later.

## Where it appears

| File | Use |
| --- | --- |
| `dashboards/fleet.json` | The dashboard: eight panels about the fleet at the edge. |
| `src/capacity/application/readiness.py` | `_dashboard_check` loads it and verifies every query's metric names. |
| `src/capacity/domain/telemetry.py` | `EXPORTED_METRICS`: the set of metric names a dashboard or alert may use. |
| `tests/e2e/test_fleet_smoke.py` | Checks every name in `EXPORTED_METRICS` against the running system's own endpoints. |

## How it works underneath

A dashboard JSON document has these top-level keys: `title`, `uid` (a stable id), `schemaVersion` (the version of Grafana's dashboard format), `version`, `tags`, `time` (the range shown), `refresh` and `panels`. Each panel has: `id`, `type` (here `timeseries`: a line chart over time), `title`, `gridPos` (`x`, `y`, `w`, `h` on a 24-unit-wide grid), `fieldConfig.defaults.unit` (how to print values: `reqps`, `ms`, `percentunit`...), and `targets`: a list of queries each with `expr` (the PromQL), `legendFormat` (a label template, where `{{instance}}` inserts the series' `instance` label) and `refId` (A, B, C).

## Walkthrough of this repository's use

### The frame

```json
{
  "title": "Capacity and readiness: the fleet at the edge",
  "uid": "capacity-fleet",
  "schemaVersion": 39,
  "version": 1,
  "tags": [
    "sre",
    "capacity"
  ],
  "time": {
    "from": "now-30m",
    "to": "now"
  },
  "refresh": "5s",
  "panels": [
    {
      "id": 1,
      "type": "timeseries",
      "title": "Edge requests per second",
      "gridPos": {
        "h": 8,
        "w": 12,
        "x": 0,
        "y": 0
```

`"title": "Capacity and readiness: the fleet at the edge"`, `"uid": "capacity-fleet"`, `"schemaVersion": 39`, `"time": {"from": "now-30m", "to": "now"}` (show the last 30 minutes) and `"refresh": "5s"`. The first panel starts at grid position `x: 0, y: 0` and is 12 wide and 8 high; the second is at `x: 12`, so they sit side by side.

### The eight panels

| # | Title | Query (abridged) | What it shows |
| --- | --- | --- | --- |
| 1 | Edge requests per second | `sum(rate(envoy_http_downstream_rq_total{...}[30s]))` | Traffic at Envoy. |
| 2 | Edge p99 latency against the 50 ms SLO | `histogram_quantile(0.99, sum by (le) (rate(envoy_http_downstream_rq_time_bucket{...}[30s])))` | p99 computed from the histogram buckets. |
| 3 | Edge 5xx ratio | 5xx rate divided by the total rate | Error share. |
| 4 | Healthy instances (five carry the projected peak) | `envoy_cluster_membership_healthy{envoy_cluster_name="app"}` and `..._total` | Fleet health, with the number the plan needs in the title. |
| 5 | CPU used by each instance (1.0 is the limit) | `rate(process_cpu_seconds_total{job="instances"}[30s])` | CPU cores used per instance, legend `{{instance}}`. |
| 6 | Ledger pool: in use, waiting and size | `ledger_pool_in_use`, `ledger_pool_waiting`, `ledger_pool_size` | The shared pool: the next bottleneck. |
| 7 | Availability budget burn rate | `slo:sli_error:ratio_rate1h{slo="api_availability"} / 0.001` | Burn rate: 1.0 spends the budget exactly. |
| 8 | Latency budget burn rate | `slo:sli_error:ratio_rate1h{slo="api_latency"} / 0.01` | Same for latency. |

`rate(x[30s])` is the per-second increase over 30 seconds; `histogram_quantile(0.99, ...)` estimates the 99th percentile from bucket counts; `sum by (le)` adds the series of all Envoy workers while keeping the bucket boundary label `le` (see the PromQL note). Dividing the recorded error ratio by the allowed bad fraction (0.001 for 99.9%, 0.01 for 99%) gives the burn rate.

### The review reads it

```python
def _dashboard_check(self) -> tuple[bool, str]:
    paths = self._files.glob("dashboards/*.json")
    if not paths:
        return False, "no dashboard under dashboards/"
    unknown: set[str] = set()
    queries = 0
    for path in paths:
        for expression in _expressions(json.loads(self._files.read(path))):
            queries += 1
            unknown |= {
                name
                for name in _metric_names(expression)
                if name not in EXPORTED_METRICS and not name.startswith(RECORDED_RULE_PREFIX)
            }
    if unknown:
        return False, f"panels query metrics nothing exports: {sorted(unknown)}"
    return (
        True,
        f"{len(paths)} dashboard, {queries} queries, every metric is one the edge, an "
        "instance or the ledger exports",
    )
```

`_dashboard_check` finds `dashboards/*.json`, collects every `expr`, extracts metric names and fails if any name is not exported and not a recorded `slo:` rule. A passing result prints the number of dashboards and queries.

## Try it yourself

1. `.venv/bin/python -c "import json; d=json.load(open('dashboards/fleet.json')); print(d['title'], d['uid'], len(d['panels']))"` prints `Capacity and readiness: the fleet at the edge capacity-fleet 8`.
2. `.venv/bin/python -c "import json; [print(p['id'], p['title']) for p in json.load(open('dashboards/fleet.json'))['panels']]"` lists the panels.
3. Paste panel 2's `expr` into Prometheus (`http://localhost:47290/graph`) while a load test runs: it plots p99 in milliseconds.
4. Edit a copy of the dashboard so a query uses `no_such_metric` and run the review's dashboard check against it (`.venv/bin/python -m pytest tests/unit/test_readiness.py -q -k dashboard`): the tests show a dashboard querying a name nothing exports fails.
5. In a Grafana you control, use Dashboards > New > Import and upload `dashboards/fleet.json`; point the data source at Prometheus.

## Mistakes people make

- A dashboard panel that queries a metric no longer exported. It draws nothing and nobody notices until an incident. The review catches it.
- Units that do not match the data (the Envoy histogram is in milliseconds; the instances' histograms are in seconds).
- Putting a number in a title that drifts from the plan. The test that ties the fleet alert to the plan keeps the "five" honest.

## Check yourself

1. What is a panel's `targets` list?
   *Answer: the queries (PromQL) the panel plots.*
2. What does `gridPos` say?
   *Answer: where the panel sits on the dashboard's grid and how big it is.*
3. Why is the burn rate `ratio / 0.001`?
   *Answer: the error ratio divided by the allowed bad fraction; 1.0 spends the error budget exactly over the window.*
4. How does the review know a metric is real?
   *Answer: it compares each name with `EXPORTED_METRICS`, which an end-to-end test checks against the running system.*
5. Is a Grafana server needed to use the file?
   *Answer: not in this repository; it is a file the review reads, and a Grafana can import it.*

## Further reading

- Grafana dashboard JSON model: <https://grafana.com/docs/grafana/latest/dashboards/build-dashboards/view-dashboard-json-model/>
- Import a dashboard: <https://grafana.com/docs/grafana/latest/dashboards/build-dashboards/import-dashboards/>
