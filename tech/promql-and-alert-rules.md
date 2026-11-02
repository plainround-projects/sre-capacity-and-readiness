# PromQL, recording rules and burn-rate alerts

## What it is

**PromQL** is Prometheus's query language. A query is an expression that returns numbers computed from stored time series.

- `envoy_http_downstream_rq_xx{envoy_response_code_class="5"}` is a **selector**: the series of that metric whose labels match. `=` matches exactly; `=~"5.."` matches a regular expression; `!~` is "does not match".
- `rate(x[5m])` is the per-second average increase of a counter over the last five minutes (handling restarts). `increase(x[1h])` is the total increase over an hour.
- `sum(...)` adds series together. `a / b` divides. `a > b` keeps results where the comparison is true. `a and b` keeps results of `a` that also exist in `b`. `or vector(0)` supplies 0 when the left side is empty.
- `absent(x)` returns 1 when no series match `x`.

A **recording rule** saves an expression's result under a new name. An **alerting rule** fires while its expression returns results.

**Burn rate** (SRE): how fast you are spending your error budget compared with spending it evenly over the window. A burn rate of 1 means the budget would last exactly the 28 days. A burn rate of 14.4 sustained for an hour spends 2% of a 28-day budget (14.4 × 1 hour ÷ 672 hours).

## Why this repository uses it

This repository has two kinds of alert. The **capacity alert** watches the number of healthy instances in Envoy (`FleetBelowPeakCapacity`); it is written by hand and covered in the promtool note. The **symptom alerts** page on how fast the error budget burns, using two windows at once, and are measured **at the Envoy edge**. The rules for two SLOs and three alert levels are repetitive and easy to get wrong by hand, so this repository **generates** them from `slo/slos.yaml` (the same generator design as the first project of the track, with Envoy's metric names) and checks them with replay tests.

## Where it appears

| File | Role |
| --- | --- |
| `src/capacity/application/rules.py` | The generator: builds the rule dictionary. |
| `slo/generated/rules.yml` | Output, production windows. |
| `slo/generated/rules.demo.yml` | Output, every window ÷ 60. |
| `slo/slos.yaml` | The inputs: targets, thresholds, alert table, `min_bad_events` (10 here). |
| `slo/alerts/fleet.rules.yml` | The hand-written capacity alerts (plain alerting rules, not generated). |
| `docs/adr/0002-envoy-as-the-edge.md` | Why the SLIs are measured at Envoy. |
| `docs/runbooks/*.md` | Linked from each alert's `runbook_url` annotation. |

## How it works underneath

For each SLO and each window (5m, 30m, 1h, 6h, 3d, 28d) the generator records `slo:sli_error:ratio_rate<window>`, the **bad fraction**:

- Availability: `(sum(rate(envoy_http_downstream_rq_xx{<ingress>,envoy_response_code_class="5"}[w])) or vector(0)) / sum(rate(envoy_http_downstream_rq_xx{<ingress>}[w]))`, i.e. 5xx responses over all responses at the Envoy edge (`<ingress>` stands for `envoy_http_conn_manager_prefix="ingress_http"`).
- Latency: `1 - (good / valid)`, where good is the Envoy request-time histogram bucket at the threshold (`le="50"`, because Envoy's buckets are in **milliseconds**) and valid is the histogram count.

It also records `slo:error_budget_remaining:ratio = 1 - (28d bad fraction / allowed bad fraction)`, and `slo:sli_bad_events:increase<window>` (the number of bad events), used as the low-traffic guard.

An alert for factor F, long window L and short window S fires when all of these hold at once:

1. The long-window bad fraction is above F × (1 − target).
2. The short-window bad fraction is above the same threshold.
3. The long window holds at least `min_bad_events` bad events.

The long window proves the burn is significant; the short window proves it is still happening, so the alert resolves soon after recovery; the floor stops a quiet journey from paging on a few unlucky failures.

## Walkthrough of this repository's use

### The generated availability alert

```yaml
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
```

- `alert:` names it (`ApiAvailabilityFastBurn`).
- `expr: |-` is a multi-line expression. `(14.4 * 0.001)` is factor × allowed bad fraction (the target 99.9% leaves 0.1% = 0.001): the bad fraction that burns 14.4 times too fast.
- The three lines joined by `and` are the three conditions above. Labels such as `{slo="api_availability"}` select the recorded series for this SLO.
- `labels` set `severity: page`, the SLO and the journey. `annotations` hold the text; `runbook_url` points to the runbook.

### The slow-burn and budget-leak alerts

```yaml
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
```

The slow-burn alert (6x) uses 6h and 30m windows and pages; the budget-leak alert (1x) uses 3d and 6h and opens a ticket.

### Recording rules

```yaml
# Generated by `capacity-slo rules` from slo/slos.yaml. Do not edit; `make rules-check` fails on drift.
# Production profile.
groups:
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
```

Each `record:` entry has an `expr` and labels `slo` and `journey`. The group `interval: 30s` sets how often it is evaluated.

### The generator

```python
def error_ratio_expr(slo: Slo, window: str) -> str:
    """PromQL for the bad fraction of valid events over `window`, measured at the Envoy edge."""
    if slo.kind is SliKind.AVAILABILITY:
        bad = f"sum(rate({EDGE_RESPONSES}{{{EDGE_PREFIX},{BAD_CLASS}}}[{window}])) or vector(0)"
        valid = f"sum(rate({EDGE_RESPONSES}{{{EDGE_PREFIX}}}[{window}]))"
        return f"({bad}) / {valid}"
    assert slo.threshold_ms is not None
    good = f'sum(rate({EDGE_DURATION_BUCKET}{{{EDGE_PREFIX},le="{bucket_label(slo.threshold_ms)}"}}[{window}]))'
    valid = f"sum(rate({EDGE_DURATION_COUNT}{{{EDGE_PREFIX}}}[{window}]))"
    return f"1 - ({good} / {valid})"


def bad_events_expr(slo: Slo, window: str) -> str:
    """PromQL for the number of bad events in the window: the low-traffic guard's input."""
    if slo.kind is SliKind.AVAILABILITY:
        return f"sum(increase({EDGE_RESPONSES}{{{EDGE_PREFIX},{BAD_CLASS}}}[{window}])) or vector(0)"
    assert slo.threshold_ms is not None
    good = f'sum(increase({EDGE_DURATION_BUCKET}{{{EDGE_PREFIX},le="{bucket_label(slo.threshold_ms)}"}}[{window}]))'
    valid = f"sum(increase({EDGE_DURATION_COUNT}{{{EDGE_PREFIX}}}[{window}]))"
    return f"{valid} - {good}"
```

`error_ratio_expr` and `bad_events_expr` build the expression strings from the SLO. In f-strings `{{` and `}}` produce literal braces (PromQL selectors use braces). `bucket_label` checks that the threshold in milliseconds is one of Envoy's bucket boundaries (`EDGE_LATENCY_BUCKETS_MS`: 0.5, 1, 5, 10, 25, 50, 100, ...) and raises if not; 50 ms is one of them. The names of the metrics live in `domain/telemetry.py` (`EDGE_RESPONSES`, `EDGE_DURATION_BUCKET`, `EDGE_DURATION_COUNT`, `EDGE_PREFIX`).

```python
def build_rules(
    slos: Sequence[Slo],
    alerts: Sequence[BurnAlert],
    window_scale: float = 1.0,
    profile: str = "",
    interval: str = "30s",
) -> dict[str, Any]:
    """Return the rule-file document: a group per SLO (recording rules, then alerts) and a blind-spot alert."""
    suffix = camel(profile) if profile else ""
    group_suffix = f"-{profile}" if profile else ""
    window_seconds = {window for alert in alerts for window in (alert.long_minutes * 60, alert.short_minutes * 60)}
    window_seconds.add(WINDOW_DAYS * 86400)
    windows = sorted({round(seconds / window_scale) for seconds in window_seconds})
    groups: list[dict[str, Any]] = []
    for slo in slos:
        recording: list[dict[str, Any]] = []
        alert_rules: list[dict[str, Any]] = []
        labels = {"slo": slo.name, "journey": slo.journey}
        for seconds in windows:
            text = duration(seconds)
            recording.append(
                {
                    "record": f"slo:sli_error:ratio_rate{text}",
                    "expr": error_ratio_expr(slo, text),
                    "labels": dict(labels),
                }
            )
        long_text = duration(WINDOW_DAYS * 86400 / window_scale)
        recording.append(
            {
                "record": "slo:error_budget_remaining:ratio",
                "expr": f'1 - (slo:sli_error:ratio_rate{long_text}{{slo="{slo.name}"}} / {slo.allowed_bad_fraction})',
                "labels": dict(labels),
            }
        )
        for long_seconds in sorted({alert.long_minutes * 60 for alert in alerts}):
            text = duration(long_seconds / window_scale)
            recording.append(
                {
                    "record": f"slo:sli_bad_events:increase{text}",
                    "expr": bad_events_expr(slo, text),
                    "labels": dict(labels),
                }
            )
        for alert in alerts:
            long_text = duration(alert.long_minutes * 60 / window_scale)
            short_text = duration(alert.short_minutes * 60 / window_scale)
            threshold = f"({alert.factor:g} * {slo.allowed_bad_fraction:g})"
            alert_rules.append(
                {
                    "alert": f"{camel(slo.name)}{camel(alert.name)}{suffix}",
                    "expr": (
                        f'slo:sli_error:ratio_rate{long_text}{{slo="{slo.name}"}} > {threshold}\n'
                        f'and slo:sli_error:ratio_rate{short_text}{{slo="{slo.name}"}} > {threshold}\n'
                        f'and slo:sli_bad_events:increase{long_text}{{slo="{slo.name}"}} >= {alert.min_bad_events}'
                    ),
                    "labels": {"severity": alert.severity, **labels},
                    "annotations": {
                        "summary": f"{slo.name} burns its error budget at more than {alert.factor:g}x",
                        "description": (
                            f"Both the {long_text} and {short_text} windows exceed burn rate {alert.factor:g} "
                            f"(about {alert.budget_consumed_fraction() * 100:.1f}% of the {slo.window_days}-day budget "
                            f"in {long_text} at that rate) and the {long_text} window holds at least "
                            f"{alert.min_bad_events} bad events. Target {slo.target * 100:g}%."
                        ),
                        "runbook_url": f"docs/runbooks/{slo.journey.replace('_', '-')}.md",
                    },
                }
            )
        # One group per SLO keeps the recording rules and the alerts that read them in order within a group.
        groups.append({"name": f"slo-{slo.name}{group_suffix}", "interval": interval, "rules": recording + alert_rules})
    groups.append(
        {
            "name": f"slo-meta{group_suffix}",
            "interval": interval,
            "rules": [
                {
                    "alert": f"EdgeMetricsAbsent{suffix}",
                    "expr": f"absent({EDGE_RESPONSES})",
                    "for": duration(300 / window_scale),
                    "labels": {"severity": "page"},
                    "annotations": {
                        "summary": "The edge exports no request metrics, so every SLI is blind",
                        "runbook_url": "docs/runbooks/edge-metrics-absent.md",
                    },
                }
            ],
        }
    )
    return {"groups": groups}
```

`build_rules(slos, alerts, window_scale, profile, interval)` returns the whole document: for every SLO a group with recording rules, then alerts. `window_scale` divides every window: `1.0` is production; `60.0` makes the demo profile where the 1h window becomes 1m and the 5m window 5s. `duration(seconds)` formats `3600` as `1h`. `camel("api", "availability")` gives `ApiAvailability`. The final group `slo-meta` has the blind-spot alert.

### The blind-spot alert

```yaml
- alert: EdgeMetricsAbsent
  expr: absent(envoy_http_downstream_rq_xx)
  for: 5m
  labels:
    severity: page
  annotations:
    summary: The edge exports no request metrics, so every SLI is blind
    runbook_url: docs/runbooks/edge-metrics-absent.md
```

`absent(envoy_http_downstream_rq_xx)` returns 1 when Envoy exports nothing. `for: 5m` requires it to hold for five minutes. If the edge died, every SLI would be silent and no burn alert could fire, so this alert pages for the silence itself.

### The demo profile is not loaded here

```yaml
# The demo-window rules (slo/generated/rules.demo.yml) are not loaded here: evaluating them every second would compete
# with the load tests for CPU, and nothing in this project needs alerts that fire in seconds.
global:
  scrape_interval: 2s
  evaluation_interval: 2s

rule_files:
  - /etc/prometheus/rules/generated/rules.yml
  - /etc/prometheus/rules/alerts/fleet.rules.yml
```

The generator also writes a **demo profile** (`slo/generated/rules.demo.yml`, windows divided by 60) but this repository's Prometheus loads only the production rules and the fleet alerts. The comment at the top says why: evaluating demo rules every second would compete with the load tests for CPU, and nothing in this exercise needs an alert that fires in seconds. The capacity alert below is the fast one.

### The capacity alert

```yaml
# The capacity plan as an alert. The plan says 5 healthy instances are needed to carry the projected peak at the
# measured safe load (tests/unit/test_evidence.py keeps this number equal to the plan's evidence). Below 5
# the fleet cannot meet the SLO at peak even though no request has failed yet.
# Generated by `capacity plan` from docs/evidence/plan.json. Do not edit.
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
          summary: Fewer than 5 healthy instances, which is the minimum that carries the projected peak
          description: A zone may be down. At the projected peak the surviving instances are above their safe load.
          runbook_url: docs/runbooks/zone-loss.md
      - alert: FleetBelowFullStrength
        expr: envoy_cluster_membership_healthy{envoy_cluster_name="app"} < 8
        for: 30s
        labels:
          severity: ticket
          service: app
        annotations:
          summary: At least one instance is unhealthy; the fleet has lost some of its headroom
          description: Capacity is still enough for peak while 5 or more are healthy, but another loss may not be.
          runbook_url: docs/runbooks/zone-loss.md
```

`envoy_cluster_membership_healthy{envoy_cluster_name="app"} < 5` is true when fewer than five instances are healthy. `for: 10s` requires it to hold for ten seconds (five evaluations of 2 s). Five is the number the plan says carries the projected peak at the measured safe load: below it the fleet cannot meet the SLO at peak even though no request has failed yet. The second alert (`< 8`, `for: 30s`, `severity: ticket`) says some headroom is gone. A test keeps the number in the rule equal to the plan's evidence.

## Try it yourself

1. `make rules` regenerates the files. `git diff --stat` shows no change.
2. `grep -c "alert:" slo/generated/rules.yml` counts the alerts (7: three for each of the two SLOs and the blind-spot alert).
3. In the Prometheus UI (`make up` first) run `slo:error_budget_remaining:ratio`.
4. `make rules-check` passes; edit one number in `slo/generated/rules.yml` and it fails with a drift message.
5. With the fleet up, `docker compose stop app-a1 app-a2 app-a3 app-b1`, then watch <http://localhost:47290/alerts>: `FleetBelowFullStrength` goes pending and then firing; with four healthy instances `FleetBelowPeakCapacity` fires too. `docker compose start app-a1 app-a2 app-a3 app-b1` clears them.

## Mistakes people make

- Using the raw counter in an alert instead of `rate()`.
- Alerting on one window: a spike pages; a slow leak is missed.
- Forgetting `or vector(0)`, so a healthy hour (no 5xx series) returns nothing.
- No floor on bad events, so a quiet journey pages on two failures.

## Check yourself

1. What does burn rate 1 mean?
   *Answer: spending the error budget exactly evenly over the window.*
2. Why two windows?
   *Answer: the long one shows significance, the short one shows it is still happening.*
3. What does `min_bad_events` stop?
   *Answer: paging on a tiny number of failures when traffic is very low.*
4. Why `or vector(0)`?
   *Answer: when no bad series exist the division would otherwise return no result.*
5. What does `window_scale` do?
   *Answer: divides all windows so a demo profile fires within seconds.*

## Further reading

- PromQL: <https://prometheus.io/docs/prometheus/3.5/querying/basics/>
- Alerting rules: <https://prometheus.io/docs/prometheus/3.5/configuration/alerting_rules/>
- Recording rules: <https://prometheus.io/docs/prometheus/3.5/configuration/recording_rules/>
- Google SRE Workbook, alerting on SLOs: <https://sre.google/workbook/alerting-on-slos/>
