# Prometheus v3.5.0

## What it is

Prometheus is a monitoring server. Every few seconds it **scrapes** (fetches over HTTP) the `/metrics` page of each program it is told about, and stores the numbers with their timestamps in its own **time-series database** (TSDB). A **time series** is a sequence of (time, value) pairs for one metric with one set of labels. Prometheus can then answer questions with its query language (PromQL) and evaluate **rules** that either record new series or raise alerts.

## Why this repository uses it

Prometheus is where the fleet becomes visible over time. It scrapes Envoy (the edge), every instance and the ledger every two seconds. The SLO rules compute the symptom alerts from Envoy's numbers, and the fleet alert watches how many instances Envoy considers healthy. The dashboard file's queries and the readiness review's checks all assume these metrics exist in Prometheus.

## Where it appears

| File | Role |
| --- | --- |
| `slo/prometheus.yml` | Scrape and rule-evaluation configuration. |
| `slo/alerts/fleet.rules.yml` | The hand-written capacity alerts. |
| `slo/generated/rules.yml` | The generated SLO rules (production windows; the demo profile file is generated too but not loaded). |
| `docker-compose.yml` | The `prometheus` service, image `prom/prometheus:v3.5.0`. |
| `dashboards/fleet.json` | Queries Prometheus (see the Grafana dashboard note). |

## How it works underneath

1. **Scrape.** Every `scrape_interval`, Prometheus sends `GET /metrics` to each target (`lb:9901`, `app-a1:9101`, `ledger:9102`, ...), parses the text, attaches the labels `job` and `instance`, and appends the samples to its TSDB.
2. **Evaluate.** Every `evaluation_interval` (and per group `interval` in a rule file) it runs each rule's expression. A **recording rule** stores the result as a new series (for example `slo:sli_error:ratio_rate5m`). An **alerting rule** has a condition; while the condition returns any result the alert is **pending**, then **firing** after the optional `for:` time.
3. **Query.** An HTTP API (port 9090) answers PromQL queries, lists active alerts and shows readiness at `/-/ready`.
4. **Retention.** Data older than `--storage.tsdb.retention.time` (1 day here) is deleted.

An alert disappears from the active list when its condition stops being true; Prometheus then sends a "resolved" state to whatever receives alerts. This repository does not run an Alertmanager (the program that routes alerts to a pager); the alert list itself is the output.

## Walkthrough of this repository's use

### The configuration file

```yaml
# The demo-window rules (slo/generated/rules.demo.yml) are not loaded here: evaluating them every second would compete
# with the load tests for CPU, and nothing in this project needs alerts that fire in seconds.
global:
  scrape_interval: 2s
  evaluation_interval: 2s

rule_files:
  - /etc/prometheus/rules/generated/rules.yml
  - /etc/prometheus/rules/alerts/fleet.rules.yml

scrape_configs:
  - job_name: edge
    metrics_path: /stats/prometheus
    static_configs:
      - targets: ["lb:9901"]
  - job_name: instances
    static_configs:
      - targets: ["app-a1:9101", "app-a2:9101", "app-a3:9101"]
        labels: {zone: a}
      - targets: ["app-b1:9101", "app-b2:9101", "app-b3:9101"]
        labels: {zone: b}
      - targets: ["app-c1:9101", "app-c2:9101", "app-c3:9101"]
        labels: {zone: c}
  - job_name: ledger
    static_configs:
      - targets: ["ledger:9102", "ledger:9103"]
```

- The comment at the top says the demo-window rules are not loaded: evaluating them every second would compete with the load tests for CPU.
- `scrape_interval: 2s` and `evaluation_interval: 2s`: collect and evaluate every two seconds. Fast enough to see a zone loss within seconds, slow enough not to disturb the measurement.
- `rule_files` are the production SLO rules and the fleet alerts, at the paths where the compose file mounts them.
- `scrape_configs` holds three jobs. `edge` scrapes `lb:9901` at `metrics_path: /stats/prometheus` (Envoy's Prometheus-format statistics on its admin port). `instances` scrapes all nine instances' ops ports (`app-a1:9101` ...) in three `static_configs` blocks, each adding a label `zone: a|b|c` to the series it scrapes, so a query can group by zone. `ledger` scrapes `ledger:9102`. Names such as `lb` and `app-a1` are Compose service names, which are DNS names inside the project's network.

### The Compose service

```yaml
prometheus:
  image: prom/prometheus:v3.5.0
  restart: unless-stopped
  cpus: 0.5
  cpuset: "9-11"
  command:
    - --config.file=/etc/prometheus/prometheus.yml
    - --storage.tsdb.retention.time=1d
  ports:
    - "127.0.0.1:${PROMETHEUS_PORT:-47290}:9090"
  volumes:
    - ./slo/prometheus.yml:/etc/prometheus/prometheus.yml:ro
    - ./slo/generated:/etc/prometheus/rules/generated:ro
    - ./slo/alerts:/etc/prometheus/rules/alerts:ro
  depends_on:
    lb:
      condition: service_healthy
  healthcheck:
    test: ["CMD", "wget", "-qO-", "http://localhost:9090/-/ready"]
    interval: 5s
    timeout: 15s
    retries: 40
```

`cpus: 0.5` caps Prometheus so it cannot take CPU from the load tests. The two `command` flags: `--config.file=/etc/prometheus/prometheus.yml` and `--storage.tsdb.retention.time=1d` (keep one day). The three volumes mount the config file, the generated rules and the hand-written alerts read-only. `depends_on` Envoy being healthy. The health check uses `wget` against `/-/ready`. The port is published as `127.0.0.1:47290`.

### What the experiments read from it

The experiments mostly read from the instances and Envoy directly (the knee test reads `process_cpu_seconds_total` from the instance's own `/metrics`, and the zone-loss test reads Envoy's membership count), because they need exact before-and-after numbers. Prometheus is the place where a person watches the same quantities over time, and the place the alerts live.

## Try it yourself

1. `make up`, then open <http://localhost:47290/targets>: the targets of the three jobs show state UP.
2. In <http://localhost:47290/graph> run `envoy_cluster_membership_healthy{envoy_cluster_name="app"}` (8) and `ledger_pool_size` (20).
3. Run `slo:sli_error:ratio_rate5m` (a recording rule's output) in the same page.
4. <http://localhost:47290/alerts> lists all alerting rules. `FleetBelowPeakCapacity` and `FleetBelowFullStrength` appear here; stop a zone (`docker compose stop app-a1 app-a2 app-a3`) and watch them.
5. `curl -s localhost:47290/api/v1/alerts` shows the alerts as JSON.

## Mistakes people make

- Scraping too slowly for short windows: an alert on three consecutive samples needs a scrape interval much shorter than the incident.
- Forgetting that a counter that restarts at zero looks like a drop (`rate()` handles resets).
- Alerting on a metric that can disappear without an `absent()` alert (this repository has `EdgeMetricsAbsent`).
- Publishing the Prometheus port to the whole network.

## Check yourself

1. What does "scrape" mean?
   *Answer: Prometheus fetches `/metrics` from a target on a schedule and stores the samples.*
2. What is a recording rule?
   *Answer: a rule whose result is saved as a new time series.*
3. What does `--storage.tsdb.retention.time=1d` do?
   *Answer: deletes data older than one day.*
4. How does `app-a1` get a DNS name in the network?
   *Answer: Compose names it after the service.*
5. Which port exposes the HTTP API?
   *Answer: 9090 inside the container (47190 on your machine).*

## Further reading

- Prometheus v3.5 documentation: <https://prometheus.io/docs/prometheus/3.5/>
- Configuration: <https://prometheus.io/docs/prometheus/3.5/configuration/configuration/>
- Querying API: <https://prometheus.io/docs/prometheus/3.5/querying/api/>
