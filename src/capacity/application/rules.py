"""Generate Prometheus recording and alert rules from the SLO definitions.

The rules are code produced from `slo/slos.yaml`, never edited by hand. `window_scale` divides every window,
which makes a demo profile whose alerts fire in seconds instead of hours; scale 1 is the production profile.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from capacity.domain.slo import WINDOW_DAYS, BurnAlert, SliKind, Slo
from capacity.domain.telemetry import (
    EDGE_DURATION_BUCKET,
    EDGE_DURATION_COUNT,
    EDGE_LATENCY_BUCKETS_MS,
    EDGE_PREFIX,
    EDGE_RESPONSES,
)

BAD_CLASS = 'envoy_response_code_class="5"'


def duration(seconds: float) -> str:
    """Format seconds as a Prometheus duration using the largest exact unit (`5m`, `1h`, `90s`)."""
    whole = round(seconds)
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if whole >= size and whole % size == 0:
            return f"{whole // size}{unit}"
    return f"{whole}s"


def bucket_label(threshold_ms: float) -> str:
    """The `le` label value for a latency threshold; raises when the edge has no such bucket boundary."""
    if threshold_ms not in EDGE_LATENCY_BUCKETS_MS:
        raise ValueError(
            f"no histogram bucket at {threshold_ms:g} ms; the edge's buckets are {EDGE_LATENCY_BUCKETS_MS}"
        )
    return f"{threshold_ms:g}"


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


def camel(*parts: str) -> str:
    """`follow_link`, `availability` -> `FollowLinkAvailability` for alert names."""
    return "".join(word.capitalize() for part in parts for word in part.split("_"))


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
