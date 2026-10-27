# PromQL: the generated rule files, line by line

## What it is

`slo/generated/rules.yml` and `slo/generated/rules.demo.yml` are Prometheus **rule files** written by the program `application/rules.py` from `slo/slos.yaml`; nobody edits them by hand. Each file is a list of **groups**. A group has a name, an evaluation interval and a list of rules. A rule is either a **recording rule** (`record:` a new series from an expression) or an **alerting rule** (`alert:` a condition that fires). This note quotes every line of both files, one group at a time, and explains what the group's lines mean. The PromQL note explains the language and the burn-rate idea; read it first.

## Why this repository uses it

The rules are repetitive on purpose: the same six windows and three alerts for every SLO. Generating them removes the typing mistakes a person would make, and quoting them here lets a reader check that nothing is hidden: every number in an alert is a number from `slos.yaml`.

## Where it appears

| File | Role |
| --- | --- |
| `slo/generated/rules.yml` | The production profile: windows of 5 minutes to 28 days, group interval 30 s. |
| `slo/generated/rules.demo.yml` | The demo profile: every window divided by 60, group interval 1 s, alert names ending in `Demo`. |
| `slo/slos.yaml` | The source of every number below. |
| `application/rules.py` | The generator. |

## How it works underneath

For every SLO the generator writes, in this order: recording rules `slo:sli_error:ratio_rate<window>` for each window (the bad fraction over that window); `slo:error_budget_remaining:ratio`; recording rules `slo:sli_bad_events:increase<window>` for each long window (the low-traffic guard's input); then three alerts. Recording rules come before the alerts that read them. At the end a group `slo-meta` holds the blind-spot alert.

An alert's `expr` has three lines joined by `and`: the long-window bad fraction is above `factor * allowed_bad_fraction`, the short-window bad fraction is above it too, and the long window holds at least `min_bad_events` bad events. `labels` give the severity, the SLO and the journey; `annotations` give a summary, a description and a runbook link.

## Walkthrough of this repository's use

### `slo/generated/rules.yml`: production profile

#### Group `slo-api_availability`

```yaml
- name: slo-api_availability
  interval: 30s
  rules:
  - record: slo:sli_error:ratio_rate5m
    expr: (sum(rate(envoy_http_downstream_rq_xx{envoy_http_conn_manager_prefix="ingress_http",envoy_response_code_class="5"}[5m])) or vector(0)) / sum(rate(envoy_http_downstream_rq_xx{envoy_http_conn_manager_prefix="ingress_http"}[5m]))
    labels:
      slo: api_availability
      journey: api
  - record: slo:sli_error:ratio_rate30m
    expr: (sum(rate(envoy_http_downstream_rq_xx{envoy_http_conn_manager_prefix="ingress_http",envoy_response_code_class="5"}[30m])) or vector(0)) / sum(rate(envoy_http_downstream_rq_xx{envoy_http_conn_manager_prefix="ingress_http"}[30m]))
    labels:
      slo: api_availability
      journey: api
  - record: slo:sli_error:ratio_rate1h
    expr: (sum(rate(envoy_http_downstream_rq_xx{envoy_http_conn_manager_prefix="ingress_http",envoy_response_code_class="5"}[1h])) or vector(0)) / sum(rate(envoy_http_downstream_rq_xx{envoy_http_conn_manager_prefix="ingress_http"}[1h]))
    labels:
      slo: api_availability
      journey: api
  - record: slo:sli_error:ratio_rate6h
    expr: (sum(rate(envoy_http_downstream_rq_xx{envoy_http_conn_manager_prefix="ingress_http",envoy_response_code_class="5"}[6h])) or vector(0)) / sum(rate(envoy_http_downstream_rq_xx{envoy_http_conn_manager_prefix="ingress_http"}[6h]))
    labels:
      slo: api_availability
      journey: api
  - record: slo:sli_error:ratio_rate3d
    expr: (sum(rate(envoy_http_downstream_rq_xx{envoy_http_conn_manager_prefix="ingress_http",envoy_response_code_class="5"}[3d])) or vector(0)) / sum(rate(envoy_http_downstream_rq_xx{envoy_http_conn_manager_prefix="ingress_http"}[3d]))
    labels:
      slo: api_availability
      journey: api
  - record: slo:sli_error:ratio_rate28d
    expr: (sum(rate(envoy_http_downstream_rq_xx{envoy_http_conn_manager_prefix="ingress_http",envoy_response_code_class="5"}[28d])) or vector(0)) / sum(rate(envoy_http_downstream_rq_xx{envoy_http_conn_manager_prefix="ingress_http"}[28d]))
    labels:
      slo: api_availability
      journey: api
  - record: slo:error_budget_remaining:ratio
    expr: 1 - (slo:sli_error:ratio_rate28d{slo="api_availability"} / 0.001)
    labels:
      slo: api_availability
      journey: api
  - record: slo:sli_bad_events:increase1h
    expr: sum(increase(envoy_http_downstream_rq_xx{envoy_http_conn_manager_prefix="ingress_http",envoy_response_code_class="5"}[1h])) or vector(0)
    labels:
      slo: api_availability
      journey: api
  - record: slo:sli_bad_events:increase6h
    expr: sum(increase(envoy_http_downstream_rq_xx{envoy_http_conn_manager_prefix="ingress_http",envoy_response_code_class="5"}[6h])) or vector(0)
    labels:
      slo: api_availability
      journey: api
  - record: slo:sli_bad_events:increase3d
    expr: sum(increase(envoy_http_downstream_rq_xx{envoy_http_conn_manager_prefix="ingress_http",envoy_response_code_class="5"}[3d])) or vector(0)
    labels:
      slo: api_availability
      journey: api
  - alert: ApiAvailabilityFastBurn
    expr: |-
      slo:sli_error:ratio_rate1h{slo="api_availability"} > (14.4 * 0.001)
      and slo:sli_error:ratio_rate5m{slo="api_availability"} > (14.4 * 0.001)
      and slo:sli_bad_events:increase1h{slo="api_availability"} >= 10
    labels:
      severity: page
      slo: api_availability
      journey: api
    annotations:
      summary: api_availability burns its error budget at more than 14.4x
      description: Both the 1h and 5m windows exceed burn rate 14.4 (about 2.1% of the 28-day budget in 1h at that rate) and the 1h window holds at least 10 bad events. Target 99.9%.
      runbook_url: docs/runbooks/api.md
  - alert: ApiAvailabilitySlowBurn
    expr: |-
      slo:sli_error:ratio_rate6h{slo="api_availability"} > (6 * 0.001)
      and slo:sli_error:ratio_rate30m{slo="api_availability"} > (6 * 0.001)
      and slo:sli_bad_events:increase6h{slo="api_availability"} >= 10
    labels:
      severity: page
      slo: api_availability
      journey: api
    annotations:
      summary: api_availability burns its error budget at more than 6x
      description: Both the 6h and 30m windows exceed burn rate 6 (about 5.4% of the 28-day budget in 6h at that rate) and the 6h window holds at least 10 bad events. Target 99.9%.
      runbook_url: docs/runbooks/api.md
  - alert: ApiAvailabilityBudgetLeak
    expr: |-
      slo:sli_error:ratio_rate3d{slo="api_availability"} > (1 * 0.001)
      and slo:sli_error:ratio_rate6h{slo="api_availability"} > (1 * 0.001)
      and slo:sli_bad_events:increase3d{slo="api_availability"} >= 10
    labels:
      severity: ticket
      slo: api_availability
      journey: api
    annotations:
      summary: api_availability burns its error budget at more than 1x
      description: Both the 3d and 6h windows exceed burn rate 1 (about 10.7% of the 28-day budget in 3d at that rate) and the 3d window holds at least 10 bad events. Target 99.9%.
      runbook_url: docs/runbooks/api.md
- name: slo-api_latency
```

This group is for the SLO `api_availability` (availability, target 99.9%, so the allowed bad fraction is 0.001; journey `api`). The six `slo:sli_error:ratio_rate<window>` rules each compute the bad fraction over one window at the Envoy edge: the rate of 5xx responses (`envoy_http_downstream_rq_xx` with `envoy_response_code_class="5"`, or 0 when none exist because of `or vector(0)`) divided by the rate of all responses of the ingress listener (`envoy_http_conn_manager_prefix="ingress_http"`). The three `slo:sli_bad_events:increase<window>` rules count the bad events in each alert's long window. Every rule carries the labels `slo` and `journey`; the alerts add `severity` and an annotation set (a summary, a description with the percent of budget spent and a `runbook_url`). The alert thresholds are the burn factor times 0.001: `(14.4 * 0.001)`, `(6 * 0.001)` and `(1 * 0.001)` for the fast-burn page, the slow-burn page and the budget-leak ticket. The `error_budget_remaining` rule divides the 28-day bad fraction by 0.001 and subtracts it from 1, so 1.0 means untouched and 0 means spent. All three alerts also require at least 10 bad events in their long window.

#### Group `slo-api_latency`

```yaml
- name: slo-api_latency
  interval: 30s
  rules:
  - record: slo:sli_error:ratio_rate5m
    expr: 1 - (sum(rate(envoy_http_downstream_rq_time_bucket{envoy_http_conn_manager_prefix="ingress_http",le="50"}[5m])) / sum(rate(envoy_http_downstream_rq_time_count{envoy_http_conn_manager_prefix="ingress_http"}[5m])))
    labels:
      slo: api_latency
      journey: api
  - record: slo:sli_error:ratio_rate30m
    expr: 1 - (sum(rate(envoy_http_downstream_rq_time_bucket{envoy_http_conn_manager_prefix="ingress_http",le="50"}[30m])) / sum(rate(envoy_http_downstream_rq_time_count{envoy_http_conn_manager_prefix="ingress_http"}[30m])))
    labels:
      slo: api_latency
      journey: api
  - record: slo:sli_error:ratio_rate1h
    expr: 1 - (sum(rate(envoy_http_downstream_rq_time_bucket{envoy_http_conn_manager_prefix="ingress_http",le="50"}[1h])) / sum(rate(envoy_http_downstream_rq_time_count{envoy_http_conn_manager_prefix="ingress_http"}[1h])))
    labels:
      slo: api_latency
      journey: api
  - record: slo:sli_error:ratio_rate6h
    expr: 1 - (sum(rate(envoy_http_downstream_rq_time_bucket{envoy_http_conn_manager_prefix="ingress_http",le="50"}[6h])) / sum(rate(envoy_http_downstream_rq_time_count{envoy_http_conn_manager_prefix="ingress_http"}[6h])))
    labels:
      slo: api_latency
      journey: api
  - record: slo:sli_error:ratio_rate3d
    expr: 1 - (sum(rate(envoy_http_downstream_rq_time_bucket{envoy_http_conn_manager_prefix="ingress_http",le="50"}[3d])) / sum(rate(envoy_http_downstream_rq_time_count{envoy_http_conn_manager_prefix="ingress_http"}[3d])))
    labels:
      slo: api_latency
      journey: api
  - record: slo:sli_error:ratio_rate28d
    expr: 1 - (sum(rate(envoy_http_downstream_rq_time_bucket{envoy_http_conn_manager_prefix="ingress_http",le="50"}[28d])) / sum(rate(envoy_http_downstream_rq_time_count{envoy_http_conn_manager_prefix="ingress_http"}[28d])))
    labels:
      slo: api_latency
      journey: api
  - record: slo:error_budget_remaining:ratio
    expr: 1 - (slo:sli_error:ratio_rate28d{slo="api_latency"} / 0.01)
    labels:
      slo: api_latency
      journey: api
  - record: slo:sli_bad_events:increase1h
    expr: sum(increase(envoy_http_downstream_rq_time_count{envoy_http_conn_manager_prefix="ingress_http"}[1h])) - sum(increase(envoy_http_downstream_rq_time_bucket{envoy_http_conn_manager_prefix="ingress_http",le="50"}[1h]))
    labels:
      slo: api_latency
      journey: api
  - record: slo:sli_bad_events:increase6h
    expr: sum(increase(envoy_http_downstream_rq_time_count{envoy_http_conn_manager_prefix="ingress_http"}[6h])) - sum(increase(envoy_http_downstream_rq_time_bucket{envoy_http_conn_manager_prefix="ingress_http",le="50"}[6h]))
    labels:
      slo: api_latency
      journey: api
  - record: slo:sli_bad_events:increase3d
    expr: sum(increase(envoy_http_downstream_rq_time_count{envoy_http_conn_manager_prefix="ingress_http"}[3d])) - sum(increase(envoy_http_downstream_rq_time_bucket{envoy_http_conn_manager_prefix="ingress_http",le="50"}[3d]))
    labels:
      slo: api_latency
      journey: api
  - alert: ApiLatencyFastBurn
    expr: |-
      slo:sli_error:ratio_rate1h{slo="api_latency"} > (14.4 * 0.01)
      and slo:sli_error:ratio_rate5m{slo="api_latency"} > (14.4 * 0.01)
      and slo:sli_bad_events:increase1h{slo="api_latency"} >= 10
    labels:
      severity: page
      slo: api_latency
      journey: api
    annotations:
      summary: api_latency burns its error budget at more than 14.4x
      description: Both the 1h and 5m windows exceed burn rate 14.4 (about 2.1% of the 28-day budget in 1h at that rate) and the 1h window holds at least 10 bad events. Target 99%.
      runbook_url: docs/runbooks/api.md
  - alert: ApiLatencySlowBurn
    expr: |-
      slo:sli_error:ratio_rate6h{slo="api_latency"} > (6 * 0.01)
      and slo:sli_error:ratio_rate30m{slo="api_latency"} > (6 * 0.01)
      and slo:sli_bad_events:increase6h{slo="api_latency"} >= 10
    labels:
      severity: page
      slo: api_latency
      journey: api
    annotations:
      summary: api_latency burns its error budget at more than 6x
      description: Both the 6h and 30m windows exceed burn rate 6 (about 5.4% of the 28-day budget in 6h at that rate) and the 6h window holds at least 10 bad events. Target 99%.
      runbook_url: docs/runbooks/api.md
  - alert: ApiLatencyBudgetLeak
    expr: |-
      slo:sli_error:ratio_rate3d{slo="api_latency"} > (1 * 0.01)
      and slo:sli_error:ratio_rate6h{slo="api_latency"} > (1 * 0.01)
      and slo:sli_bad_events:increase3d{slo="api_latency"} >= 10
    labels:
      severity: ticket
      slo: api_latency
      journey: api
    annotations:
      summary: api_latency burns its error budget at more than 1x
      description: Both the 3d and 6h windows exceed burn rate 1 (about 10.7% of the 28-day budget in 3d at that rate) and the 3d window holds at least 10 bad events. Target 99%.
      runbook_url: docs/runbooks/api.md
- name: slo-meta
```

This group is for the SLO `api_latency` (latency, target 99%, so the allowed bad fraction is 0.01; journey `api`). The six `slo:sli_error:ratio_rate<window>` rules each compute the bad fraction over one window at the Envoy edge as 1 minus the share of requests faster than the threshold: the rate of the histogram bucket `envoy_http_downstream_rq_time_bucket{le="50"}` (requests answered in 50 ms or less; Envoy times are in milliseconds) divided by the rate of `envoy_http_downstream_rq_time_count` (all requests). The three `slo:sli_bad_events:increase<window>` rules are the count minus the bucket, the number of too-slow requests. The generator refuses a threshold that is not a bucket boundary of the edge's histogram. The alert thresholds are the burn factor times 0.01: `(14.4 * 0.01)`, `(6 * 0.01)` and `(1 * 0.01)` for the fast-burn page, the slow-burn page and the budget-leak ticket. The `error_budget_remaining` rule divides the 28-day bad fraction by 0.01 and subtracts it from 1, so 1.0 means untouched and 0 means spent. All three alerts also require at least 10 bad events in their long window.

#### Group `slo-meta`

```yaml
- name: slo-meta
  interval: 30s
  rules:
  - alert: EdgeMetricsAbsent
    expr: absent(envoy_http_downstream_rq_xx)
    for: 5m
    labels:
      severity: page
    annotations:
      summary: The edge exports no request metrics, so every SLI is blind
      runbook_url: docs/runbooks/edge-metrics-absent.md
```

This group holds one alert, `EdgeMetricsAbsent`. `absent(envoy_http_downstream_rq_xx)` is 1 only when that series does not exist at all, and `for: 5m` (5s in the demo profile) means it must stay true that long. If Envoy stops exporting request metrics, every SLI above would silently read no data and no burn alert could fire; this alert pages for the blind spot itself.

### `slo/generated/rules.demo.yml`: demo profile

#### Group `slo-api_availability-demo`

```yaml
- name: slo-api_availability-demo
  interval: 1s
  rules:
  - record: slo:sli_error:ratio_rate5s
    expr: (sum(rate(envoy_http_downstream_rq_xx{envoy_http_conn_manager_prefix="ingress_http",envoy_response_code_class="5"}[5s])) or vector(0)) / sum(rate(envoy_http_downstream_rq_xx{envoy_http_conn_manager_prefix="ingress_http"}[5s]))
    labels:
      slo: api_availability
      journey: api
  - record: slo:sli_error:ratio_rate30s
    expr: (sum(rate(envoy_http_downstream_rq_xx{envoy_http_conn_manager_prefix="ingress_http",envoy_response_code_class="5"}[30s])) or vector(0)) / sum(rate(envoy_http_downstream_rq_xx{envoy_http_conn_manager_prefix="ingress_http"}[30s]))
    labels:
      slo: api_availability
      journey: api
  - record: slo:sli_error:ratio_rate1m
    expr: (sum(rate(envoy_http_downstream_rq_xx{envoy_http_conn_manager_prefix="ingress_http",envoy_response_code_class="5"}[1m])) or vector(0)) / sum(rate(envoy_http_downstream_rq_xx{envoy_http_conn_manager_prefix="ingress_http"}[1m]))
    labels:
      slo: api_availability
      journey: api
  - record: slo:sli_error:ratio_rate6m
    expr: (sum(rate(envoy_http_downstream_rq_xx{envoy_http_conn_manager_prefix="ingress_http",envoy_response_code_class="5"}[6m])) or vector(0)) / sum(rate(envoy_http_downstream_rq_xx{envoy_http_conn_manager_prefix="ingress_http"}[6m]))
    labels:
      slo: api_availability
      journey: api
  - record: slo:sli_error:ratio_rate72m
    expr: (sum(rate(envoy_http_downstream_rq_xx{envoy_http_conn_manager_prefix="ingress_http",envoy_response_code_class="5"}[72m])) or vector(0)) / sum(rate(envoy_http_downstream_rq_xx{envoy_http_conn_manager_prefix="ingress_http"}[72m]))
    labels:
      slo: api_availability
      journey: api
  - record: slo:sli_error:ratio_rate672m
    expr: (sum(rate(envoy_http_downstream_rq_xx{envoy_http_conn_manager_prefix="ingress_http",envoy_response_code_class="5"}[672m])) or vector(0)) / sum(rate(envoy_http_downstream_rq_xx{envoy_http_conn_manager_prefix="ingress_http"}[672m]))
    labels:
      slo: api_availability
      journey: api
  - record: slo:error_budget_remaining:ratio
    expr: 1 - (slo:sli_error:ratio_rate672m{slo="api_availability"} / 0.001)
    labels:
      slo: api_availability
      journey: api
  - record: slo:sli_bad_events:increase1m
    expr: sum(increase(envoy_http_downstream_rq_xx{envoy_http_conn_manager_prefix="ingress_http",envoy_response_code_class="5"}[1m])) or vector(0)
    labels:
      slo: api_availability
      journey: api
  - record: slo:sli_bad_events:increase6m
    expr: sum(increase(envoy_http_downstream_rq_xx{envoy_http_conn_manager_prefix="ingress_http",envoy_response_code_class="5"}[6m])) or vector(0)
    labels:
      slo: api_availability
      journey: api
  - record: slo:sli_bad_events:increase72m
    expr: sum(increase(envoy_http_downstream_rq_xx{envoy_http_conn_manager_prefix="ingress_http",envoy_response_code_class="5"}[72m])) or vector(0)
    labels:
      slo: api_availability
      journey: api
  - alert: ApiAvailabilityFastBurnDemo
    expr: |-
      slo:sli_error:ratio_rate1m{slo="api_availability"} > (14.4 * 0.001)
      and slo:sli_error:ratio_rate5s{slo="api_availability"} > (14.4 * 0.001)
      and slo:sli_bad_events:increase1m{slo="api_availability"} >= 10
    labels:
      severity: page
      slo: api_availability
      journey: api
    annotations:
      summary: api_availability burns its error budget at more than 14.4x
      description: Both the 1m and 5s windows exceed burn rate 14.4 (about 2.1% of the 28-day budget in 1m at that rate) and the 1m window holds at least 10 bad events. Target 99.9%.
      runbook_url: docs/runbooks/api.md
  - alert: ApiAvailabilitySlowBurnDemo
    expr: |-
      slo:sli_error:ratio_rate6m{slo="api_availability"} > (6 * 0.001)
      and slo:sli_error:ratio_rate30s{slo="api_availability"} > (6 * 0.001)
      and slo:sli_bad_events:increase6m{slo="api_availability"} >= 10
    labels:
      severity: page
      slo: api_availability
      journey: api
    annotations:
      summary: api_availability burns its error budget at more than 6x
      description: Both the 6m and 30s windows exceed burn rate 6 (about 5.4% of the 28-day budget in 6m at that rate) and the 6m window holds at least 10 bad events. Target 99.9%.
      runbook_url: docs/runbooks/api.md
  - alert: ApiAvailabilityBudgetLeakDemo
    expr: |-
      slo:sli_error:ratio_rate72m{slo="api_availability"} > (1 * 0.001)
      and slo:sli_error:ratio_rate6m{slo="api_availability"} > (1 * 0.001)
      and slo:sli_bad_events:increase72m{slo="api_availability"} >= 10
    labels:
      severity: ticket
      slo: api_availability
      journey: api
    annotations:
      summary: api_availability burns its error budget at more than 1x
      description: Both the 72m and 6m windows exceed burn rate 1 (about 10.7% of the 28-day budget in 72m at that rate) and the 72m window holds at least 10 bad events. Target 99.9%.
      runbook_url: docs/runbooks/api.md
- name: slo-api_latency-demo
```

This group is for the SLO `api_availability` (availability, target 99.9%, so the allowed bad fraction is 0.001; journey `api`). The six `slo:sli_error:ratio_rate<window>` rules each compute the bad fraction over one window at the Envoy edge: the rate of 5xx responses (`envoy_http_downstream_rq_xx` with `envoy_response_code_class="5"`, or 0 when none exist because of `or vector(0)`) divided by the rate of all responses of the ingress listener (`envoy_http_conn_manager_prefix="ingress_http"`). The three `slo:sli_bad_events:increase<window>` rules count the bad events in each alert's long window. Every rule carries the labels `slo` and `journey`; the alerts add `severity` and an annotation set (a summary, a description with the percent of budget spent and a `runbook_url`). The alert thresholds are the burn factor times 0.001: `(14.4 * 0.001)`, `(6 * 0.001)` and `(1 * 0.001)` for the fast-burn page, the slow-burn page and the budget-leak ticket. The `error_budget_remaining` rule divides the 28-day bad fraction by 0.001 and subtracts it from 1, so 1.0 means untouched and 0 means spent. All three alerts also require at least 10 bad events in their long window.

#### Group `slo-api_latency-demo`

```yaml
- name: slo-api_latency-demo
  interval: 1s
  rules:
  - record: slo:sli_error:ratio_rate5s
    expr: 1 - (sum(rate(envoy_http_downstream_rq_time_bucket{envoy_http_conn_manager_prefix="ingress_http",le="50"}[5s])) / sum(rate(envoy_http_downstream_rq_time_count{envoy_http_conn_manager_prefix="ingress_http"}[5s])))
    labels:
      slo: api_latency
      journey: api
  - record: slo:sli_error:ratio_rate30s
    expr: 1 - (sum(rate(envoy_http_downstream_rq_time_bucket{envoy_http_conn_manager_prefix="ingress_http",le="50"}[30s])) / sum(rate(envoy_http_downstream_rq_time_count{envoy_http_conn_manager_prefix="ingress_http"}[30s])))
    labels:
      slo: api_latency
      journey: api
  - record: slo:sli_error:ratio_rate1m
    expr: 1 - (sum(rate(envoy_http_downstream_rq_time_bucket{envoy_http_conn_manager_prefix="ingress_http",le="50"}[1m])) / sum(rate(envoy_http_downstream_rq_time_count{envoy_http_conn_manager_prefix="ingress_http"}[1m])))
    labels:
      slo: api_latency
      journey: api
  - record: slo:sli_error:ratio_rate6m
    expr: 1 - (sum(rate(envoy_http_downstream_rq_time_bucket{envoy_http_conn_manager_prefix="ingress_http",le="50"}[6m])) / sum(rate(envoy_http_downstream_rq_time_count{envoy_http_conn_manager_prefix="ingress_http"}[6m])))
    labels:
      slo: api_latency
      journey: api
  - record: slo:sli_error:ratio_rate72m
    expr: 1 - (sum(rate(envoy_http_downstream_rq_time_bucket{envoy_http_conn_manager_prefix="ingress_http",le="50"}[72m])) / sum(rate(envoy_http_downstream_rq_time_count{envoy_http_conn_manager_prefix="ingress_http"}[72m])))
    labels:
      slo: api_latency
      journey: api
  - record: slo:sli_error:ratio_rate672m
    expr: 1 - (sum(rate(envoy_http_downstream_rq_time_bucket{envoy_http_conn_manager_prefix="ingress_http",le="50"}[672m])) / sum(rate(envoy_http_downstream_rq_time_count{envoy_http_conn_manager_prefix="ingress_http"}[672m])))
    labels:
      slo: api_latency
      journey: api
  - record: slo:error_budget_remaining:ratio
    expr: 1 - (slo:sli_error:ratio_rate672m{slo="api_latency"} / 0.01)
    labels:
      slo: api_latency
      journey: api
  - record: slo:sli_bad_events:increase1m
    expr: sum(increase(envoy_http_downstream_rq_time_count{envoy_http_conn_manager_prefix="ingress_http"}[1m])) - sum(increase(envoy_http_downstream_rq_time_bucket{envoy_http_conn_manager_prefix="ingress_http",le="50"}[1m]))
    labels:
      slo: api_latency
      journey: api
  - record: slo:sli_bad_events:increase6m
    expr: sum(increase(envoy_http_downstream_rq_time_count{envoy_http_conn_manager_prefix="ingress_http"}[6m])) - sum(increase(envoy_http_downstream_rq_time_bucket{envoy_http_conn_manager_prefix="ingress_http",le="50"}[6m]))
    labels:
      slo: api_latency
      journey: api
  - record: slo:sli_bad_events:increase72m
    expr: sum(increase(envoy_http_downstream_rq_time_count{envoy_http_conn_manager_prefix="ingress_http"}[72m])) - sum(increase(envoy_http_downstream_rq_time_bucket{envoy_http_conn_manager_prefix="ingress_http",le="50"}[72m]))
    labels:
      slo: api_latency
      journey: api
  - alert: ApiLatencyFastBurnDemo
    expr: |-
      slo:sli_error:ratio_rate1m{slo="api_latency"} > (14.4 * 0.01)
      and slo:sli_error:ratio_rate5s{slo="api_latency"} > (14.4 * 0.01)
      and slo:sli_bad_events:increase1m{slo="api_latency"} >= 10
    labels:
      severity: page
      slo: api_latency
      journey: api
    annotations:
      summary: api_latency burns its error budget at more than 14.4x
      description: Both the 1m and 5s windows exceed burn rate 14.4 (about 2.1% of the 28-day budget in 1m at that rate) and the 1m window holds at least 10 bad events. Target 99%.
      runbook_url: docs/runbooks/api.md
  - alert: ApiLatencySlowBurnDemo
    expr: |-
      slo:sli_error:ratio_rate6m{slo="api_latency"} > (6 * 0.01)
      and slo:sli_error:ratio_rate30s{slo="api_latency"} > (6 * 0.01)
      and slo:sli_bad_events:increase6m{slo="api_latency"} >= 10
    labels:
      severity: page
      slo: api_latency
      journey: api
    annotations:
      summary: api_latency burns its error budget at more than 6x
      description: Both the 6m and 30s windows exceed burn rate 6 (about 5.4% of the 28-day budget in 6m at that rate) and the 6m window holds at least 10 bad events. Target 99%.
      runbook_url: docs/runbooks/api.md
  - alert: ApiLatencyBudgetLeakDemo
    expr: |-
      slo:sli_error:ratio_rate72m{slo="api_latency"} > (1 * 0.01)
      and slo:sli_error:ratio_rate6m{slo="api_latency"} > (1 * 0.01)
      and slo:sli_bad_events:increase72m{slo="api_latency"} >= 10
    labels:
      severity: ticket
      slo: api_latency
      journey: api
    annotations:
      summary: api_latency burns its error budget at more than 1x
      description: Both the 72m and 6m windows exceed burn rate 1 (about 10.7% of the 28-day budget in 72m at that rate) and the 72m window holds at least 10 bad events. Target 99%.
      runbook_url: docs/runbooks/api.md
- name: slo-meta-demo
```

This group is for the SLO `api_latency` (latency, target 99%, so the allowed bad fraction is 0.01; journey `api`). The six `slo:sli_error:ratio_rate<window>` rules each compute the bad fraction over one window at the Envoy edge as 1 minus the share of requests faster than the threshold: the rate of the histogram bucket `envoy_http_downstream_rq_time_bucket{le="50"}` (requests answered in 50 ms or less; Envoy times are in milliseconds) divided by the rate of `envoy_http_downstream_rq_time_count` (all requests). The three `slo:sli_bad_events:increase<window>` rules are the count minus the bucket, the number of too-slow requests. The generator refuses a threshold that is not a bucket boundary of the edge's histogram. The alert thresholds are the burn factor times 0.01: `(14.4 * 0.01)`, `(6 * 0.01)` and `(1 * 0.01)` for the fast-burn page, the slow-burn page and the budget-leak ticket. The `error_budget_remaining` rule divides the 28-day bad fraction by 0.01 and subtracts it from 1, so 1.0 means untouched and 0 means spent. All three alerts also require at least 10 bad events in their long window.

#### Group `slo-meta-demo`

```yaml
- name: slo-meta-demo
  interval: 1s
  rules:
  - alert: EdgeMetricsAbsentDemo
    expr: absent(envoy_http_downstream_rq_xx)
    for: 5s
    labels:
      severity: page
    annotations:
      summary: The edge exports no request metrics, so every SLI is blind
      runbook_url: docs/runbooks/edge-metrics-absent.md
```

This group holds one alert, `EdgeMetricsAbsent`. `absent(envoy_http_downstream_rq_xx)` is 1 only when that series does not exist at all, and `for: 5m` (5s in the demo profile) means it must stay true that long. If Envoy stops exporting request metrics, every SLI above would silently read no data and no burn alert could fire; this alert pages for the blind spot itself.

## Try it yourself

1. `grep -c "record:" slo/generated/rules.yml` counts the recording rules; `grep -c "alert:" slo/generated/rules.yml` counts the alerts (three per SLO plus the blind-spot alert).
2. `diff <(sed 's/\[5m\]/[5s]/g' slo/generated/rules.yml | head -20) <(head -20 slo/generated/rules.demo.yml)` shows that the first lines differ only in the profile banner, the group name (`-demo`) and the interval (`30s` against `1s`).
3. `make rules` regenerates both files; `git diff --stat` shows no change.
4. `make rules-check` fails after you edit one number in a generated file.
5. In the Prometheus UI, enter any `expr` from this note (with the stack up) to see what it returns.

## Mistakes people make

- Editing a generated file by hand. The next `make rules` overwrites it and `make rules-check` fails.
- Reading an alert without the recording rules it uses: the `slo:...` names are series defined earlier in the same group.
- Forgetting that the demo profile has different window names (`ratio_rate5s`, `ratio_rate1m`, `ratio_rate672m`), so a demo alert reads demo recording rules only.

## Check yourself

1. In which order are rules written inside a group?
   *Answer: recording rules for the windows, the budget-remaining rule, the bad-event counts, then the three alerts.*
2. What does `or vector(0)` do?
   *Answer: gives 0 when no failing series exist, so the division still returns a number.*
3. What makes the demo profile fast?
   *Answer: every window is divided by 60 and the group interval is 1 second.*
4. What is the third line of an alert expression?
   *Answer: the guard that the long window holds at least the minimum number of bad events.*
5. Where does each number in an alert come from?
   *Answer: from `slo/slos.yaml` (target, factor, windows, `min_bad_events`).*

## Further reading

- Recording rules: <https://prometheus.io/docs/prometheus/3.5/configuration/recording_rules/>
- Alerting rules: <https://prometheus.io/docs/prometheus/3.5/configuration/alerting_rules/>
