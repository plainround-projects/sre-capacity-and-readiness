# YAML

## What it is

YAML is a plain-text format for writing structured data: lists, named values, nested groups. It is meant to be read by people and by programs. The file ends in `.yml` or `.yaml`; both are the same.

The data is built from three shapes:

- A **mapping** is a set of `name: value` pairs (called a dictionary or object in other languages).
- A **sequence** is a list. Each item starts with `- `.
- A **scalar** is a single value: text, a number, `true` or `false`, or `null`.

Nesting is written with **indentation using spaces**. Tabs are not allowed. Two spaces per level is the convention here.

## Why this repository uses it

The lesson says definitions are code. The SLO definition, the readiness checklist configuration, the Envoy configuration, the Prometheus config and alert rules and their tests, the Compose file and the CI workflow are all YAML. A change to an SLO, to the number of healthy instances an alert requires, or to the owner of a checklist item shows up as a small, readable diff.

## Where it appears

| File | What it holds |
| --- | --- |
| `slo/slos.yaml` | The `api` journey, two SLOs, the burn-rate alert table and the low-traffic floor. The source of truth for the generated rules. |
| `readiness/checklist.yaml` | Owner, fix date and blocking flag per checklist item; the accepted risks. |
| `deploy/envoy.yaml` | The Envoy configuration (see the Envoy note). |
| `slo/prometheus.yml` | Prometheus configuration: scrape interval, rule files, three scrape jobs. |
| `slo/alerts/fleet.rules.yml` | The hand-written capacity alerts. |
| `slo/generated/rules.yml`, `rules.demo.yml` | Prometheus rules written by `capacity-slo rules`. |
| `slo/tests/*.test.yml` | `promtool` replay tests. |
| `docker-compose.yml` | The containers (see the Docker Compose note). |
| `.github/workflows/ci.yml` | The CI workflow. |

## How it works underneath

A program reads a YAML file as text and turns it into its own data structures. Python's PyYAML reads `slos.yaml` and `checklist.yaml` with `yaml.safe_load`, which returns ordinary `dict`, `list`, `str`, `int`, `float`, `bool` and `date` values (see the PyYAML note). Prometheus, promtool, Envoy and Docker Compose have their own YAML readers.

Things that surprise beginners:

- **Types are guessed.** `0.999` is a number, `true` is a boolean, `2026-11-03` (unquoted) is a date, `"true"` in quotes is text.
- **A colon followed by a space is the separator.** Text that contains `: ` must be quoted. Keys that begin with `@` must be quoted: Envoy's `"@type"`.
- **Comments** start with `#`.
- **Long text:** `>-` after a key starts a **folded block**: the following indented lines are joined with spaces and the final newline removed.
- **Flow style** `{a: 1, b: 2}` and `[x, y]` writes a mapping or list on one line.
- **Anchors and merges:** `&name` labels a block, `*name` reuses it, `<<: *name` merges its keys into the current mapping.
- **Indentation is spaces only** (two per level here); tabs are an error.

## Walkthrough of this repository's use

### `slo/slos.yaml`

```yaml
# The SLOs of the service about to launch, measured at the Envoy edge. The p99 target is the lesson's fact:
# 99% of requests under 50 ms. The rules in slo/generated are produced from this file.
window_days: 28
min_bad_events: 10

journeys:
  api:
    rank: 1
    working: A caller asks for work and gets the answer within 50 milliseconds, every time, even with a zone down.

slos:
  - name: api_availability
    journey: api
    kind: availability
    target: 0.999
    valid: Every request that reaches the Envoy edge.
    good: Any response that is not a 5xx.
    why: >-
      The launch tolerates about 40 minutes of full outage in 28 days, which is a few bad releases caught by the
      rollout test and rolled back, or one zone lost and found by the fleet alert.
  - name: api_latency
    journey: api
    kind: latency
    target: 0.99
    threshold_ms: 50
    valid: Every request that reaches the Envoy edge.
    good: Answered in 50 ms or less, measured by the edge from request to last byte.
    why: >-
      The lesson's latency SLO, p99 under 50 ms. The capacity plan exists to keep it true at twice today's peak
      with a zone lost; this SLO is how production would notice if it stopped being true.

```

`window_days: 28` is a number; `min_bad_events: 10` is the low-traffic floor. `journeys:` is a mapping with one journey, `api`. `slos:` is a **sequence**: each item starts with `- ` and has `name`, `journey`, `kind`, `target` (0.999 means 99.9% good), `valid`, `good` and `why`; the latency item adds `threshold_ms: 50`. `why: >-` begins a folded block.

```yaml
alerts:
  - {name: fast_burn, severity: page, factor: 14.4, long_minutes: 60, short_minutes: 5}
  - {name: slow_burn, severity: page, factor: 6, long_minutes: 360, short_minutes: 30}
  - {name: budget_leak, severity: ticket, factor: 1, long_minutes: 4320, short_minutes: 360}
```

The alert table uses flow style, one item per line. The numbers are explained in the PromQL note.

### `readiness/checklist.yaml`

```yaml
# Who owns each checklist item and by when a failure of it is fixed. A failing item without an owner and a date is
# refused by the review. `blocking: true` means a failure must be fixed before launch, not after.
items:
  slos:
    owner: service owner
    fix_by: 2026-11-03
  dashboards:
    owner: service owner
    fix_by: 2026-11-03
  runbooks:
    owner: on-call lead
    fix_by: 2026-11-03
    blocking: true
  rollout:
    owner: release owner
    fix_by: 2026-11-03
    blocking: true
  dependencies:
    owner: service owner
    fix_by: 2026-11-03
  restore:
    owner: database on-call
    fix_by: 2026-11-03
  ownership:
    owner: engineering manager
    fix_by: 2026-11-03
    blocking: true
  capacity:
    owner: capacity owner
    fix_by: 2026-11-03
    blocking: true

# A risk the team knowingly launches with. Each has an owner and the condition that reopens the question.
accepted_risks:
  - area: Autoscaling
    check: The fleet is a fixed size, planned for the six-month peak
    evidence: Load in the lesson's facts is steady and grows by a known factor, and a fixed fleet of eight carries twice today's peak with a zone lost; autoscaling is the lesson's stretch goal and is not built
    owner: capacity owner
    revisit_when: Traffic passes 1.5 times today's peak, or a launch event is expected to double traffic within minutes
  - area: Failure domains of the teaching build
    check: The three zones are labels on containers of one Docker host
    evidence: The zone-loss test removes three instances at once, which models the capacity effect of a zone failure but not a shared network, power or control-plane failure
    owner: service owner
    revisit_when: The service is deployed to real zones, at which point the same test is repeated by shutting down a real zone's nodes
```

`items:` is a mapping from checklist item id to its `owner` (a role), `fix_by` (an unquoted date, which PyYAML turns into a date) and optional `blocking: true`. `accepted_risks:` is a sequence of mappings, each with `area`, `check`, `evidence`, `owner` and `revisit_when`.

### `slo/prometheus.yml`

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
      - targets: ["app-c1:9101", "app-c2:9101"]
        labels: {zone: c}
  - job_name: ledger
    static_configs:
      - targets: ["ledger:9102"]
```

`global` sets the scrape and evaluation interval (2 seconds). `rule_files` lists rule files inside the container. `scrape_configs` lists three **jobs**; the `instances` job has three `static_configs` entries, each with `targets` (a list in flow style) and `labels` (a mapping in flow style) that add a `zone` label.

### `slo/alerts/fleet.rules.yml`

```yaml
# The capacity plan as an alert. The plan says five healthy instances are needed to carry the projected peak at the
# measured safe load (tests/unit/test_evidence.py keeps this number equal to the plan's evidence). Below five
# the fleet cannot meet the SLO at peak even though no request has failed yet.
groups:
  - name: fleet-capacity
    interval: 2s
    rules:
      - alert: FleetBelowPeakCapacity
        expr: envoy_cluster_membership_healthy{envoy_cluster_name="app"} < 5
        for: 10s
        labels:
          severity: page
          service: app
        annotations:
          summary: Fewer than five healthy instances, which is the minimum that carries the projected peak
          description: A zone may be down. At the projected peak the surviving instances are above their safe load.
          runbook_url: docs/runbooks/zone-loss.md
```

A rule file has `groups`; each group has a `name`, an `interval` and `rules`. This rule has `alert`, `expr` (`envoy_cluster_membership_healthy{envoy_cluster_name="app"} < 5`), `for: 10s`, `labels` and `annotations`. The PromQL note explains the meaning.

### `deploy/envoy.yaml`

```yaml
static_resources:
  listeners:
    - name: ingress
      address:
        socket_address: {address: 0.0.0.0, port_value: 8080}
      filter_chains:
        - filters:
            - name: envoy.filters.network.http_connection_manager
              typed_config:
                "@type": type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager
                stat_prefix: ingress_http
                route_config:
                  name: local
                  virtual_hosts:
                    - name: app
                      domains: ["*"]
                      routes:
                        - match: {prefix: "/"}
                          route:
                            cluster: app
                            timeout: 2s
                            retry_policy:
                              retry_on: connect-failure,refused-stream,reset
                              num_retries: 1
                http_filters:
                  - name: envoy.filters.http.router
                    typed_config:
                      "@type": type.googleapis.com/envoy.extensions.filters.http.router.v3.Router

```

Nested mappings and sequences several levels deep. `"@type": type.googleapis.com/...` is quoted because `@` cannot start an unquoted key; the value says which kind of configuration object follows.

### The Compose anchors

```yaml
x-instance: &instance
  build: .
  image: sre-capacity-and-readiness:dev
  command: ["capacity-instance"]
  restart: unless-stopped
  read_only: true
  security_opt: ["no-new-privileges:true"]
  cap_drop: ["ALL"]
  tmpfs: ["/tmp"]
  cpus: 1.0
  mem_limit: 256m
  stop_grace_period: 15s
  healthcheck:
    test: ["CMD", "python", "-c", "import urllib.request as u; u.urlopen('http://localhost:9101/healthz', timeout=2)"]
    interval: 5s
    timeout: 15s
    retries: 40
    start_period: 10s

x-instance-env: &instance-env
  WORK_ROUNDS: ${WORK_ROUNDS:-3000}
  # Empty means no shared dependency: the first tests find the CPU knee on its own. `make run` sets it for the pool tests.
  LEDGER_URL: ${LEDGER_URL:-}
  LEDGER_TIMEOUT_S: ${LEDGER_TIMEOUT_S:-0.5}
  VERSION: ${APP_VERSION:-v1}
  LOG_LEVEL: ${LOG_LEVEL:-INFO}
```

`x-instance: &instance` labels the block and `<<: *instance` in each of the eight instance services merges it. `x-instance-env: &instance-env` does the same for the environment variables; each instance then adds `ZONE` and `INSTANCE`. One place states the CPU and memory limits, the hardening and the health check for all eight.

### The promtool test file

```yaml
# The capacity plan as an alert: five healthy instances carry the projected peak, so fewer than five pages and fewer
# than eight (any loss of headroom) opens a ticket. Samples step every 2 seconds, like the scrape interval.
rule_files:
  - ../alerts/fleet.rules.yml

evaluation_interval: 2s

tests:
  - name: losing a zone of three leaves five, which is a ticket and not a page
    interval: 2s
    input_series:
      - series: 'envoy_cluster_membership_healthy{envoy_cluster_name="app"}'
        values: "8x9 5x39 8x30"
    alert_rule_test:
      - eval_time: 60s
        alertname: FleetBelowFullStrength
        exp_alerts:
          - exp_labels: {severity: ticket, service: app, envoy_cluster_name: app}
            exp_annotations:
              summary: At least one instance is unhealthy; the fleet has lost some of its headroom
              description: Capacity is still enough for peak while five or more are healthy, but another loss may not be.
              runbook_url: docs/runbooks/zone-loss.md
      - {eval_time: 60s, alertname: FleetBelowPeakCapacity, exp_alerts: []}
      - {eval_time: 140s, alertname: FleetBelowFullStrength, exp_alerts: []}
```

The keys (`rule_files`, `evaluation_interval`, `tests`, `input_series`, `alert_rule_test`) belong to promtool's format (see the promtool note). `values: "8x9 5x39 8x30"` is a string that promtool expands.

## Try it yourself

1. `.venv/bin/python -c "import yaml; print(yaml.safe_load(open('slo/slos.yaml'))['window_days'])"` prints `28`.
2. `.venv/bin/python -c "import yaml; d=yaml.safe_load(open('readiness/checklist.yaml')); print(d['items']['capacity'])"` prints `{'owner': 'capacity owner', 'fix_by': datetime.date(2026, 11, 3), 'blocking': True}`.
3. `docker compose config | head -60` shows the Compose file with every anchor expanded.
4. Change `window_days: 28` to `window_days: twenty` and run `make rules-check`: it fails with a clear error naming the file.
5. Put a tab at the start of a line in a copy of a YAML file and load it: PyYAML reports an error about the tab character.

## Mistakes people make

- Mixing tabs and spaces.
- Unquoted values that YAML reinterprets (`no`, `on`, `1.10`, an unquoted date when you wanted text).
- Using `yaml.load` on files you did not write; this repository uses `safe_load`.
- Editing generated YAML by hand: `make rules-check` fails on drift.

## Check yourself

1. What are the three kinds of YAML data?
   *Answer: mappings, sequences and scalars.*
2. What does `<<: *instance` do?
   *Answer: merges the keys of the block labelled `&instance` into the current mapping.*
3. Why is `"@type"` quoted?
   *Answer: a plain key cannot start with `@`.*
4. What type does PyYAML give an unquoted `2026-11-03`?
   *Answer: a Python `date`.*
5. What does `>-` do before a text value?
   *Answer: starts a folded block; lines are joined with spaces and the final newline is stripped.*

## Further reading

- YAML 1.2.2 specification: <https://yaml.org/spec/1.2.2/>
- PyYAML 6.0.2 documentation: <https://pyyaml.org/wiki/PyYAMLDocumentation>
