"""Evaluate multiwindow burn-rate alerts over per-minute tallies, with the same rule the Prometheus alerts use."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import accumulate

from capacity.application.sli import MinuteTally
from capacity.domain.slo import BurnAlert, Slo


@dataclass(frozen=True, slots=True)
class Firing:
    """One alert that fired during the replay: when it first fired and how long it stayed firing."""

    slo: str
    alert: str
    severity: str
    first_minute: int
    firing_minutes: int
    peak_long_burn: float


def evaluate(slo: Slo, tally: MinuteTally, alerts: Sequence[BurnAlert]) -> list[Firing]:
    """Replay the whole tally and report every alert that fires at least once.

    At the end of each minute an alert fires when the burn rate over its long window and over its short
    window both exceed its factor and the long window holds at least `min_bad_events` bad events. A window
    with no valid events burns nothing, and an alert is not evaluated until its long window is full of data.
    """
    valid_prefix = [0, *accumulate(tally.valid)]
    bad_prefix = [0, *accumulate(valid - good for valid, good in zip(tally.valid, tally.good, strict=True))]
    allowed = slo.allowed_bad_fraction
    results: list[Firing] = []
    for alert in alerts:
        first = -1
        firing_minutes = 0
        peak = 0.0
        for end in range(alert.long_minutes, tally.minutes + 1):
            long_burn = _window_burn(valid_prefix, bad_prefix, end, alert.long_minutes, allowed)
            if (
                long_burn <= alert.factor
                or bad_prefix[end] - bad_prefix[end - alert.long_minutes] < alert.min_bad_events
            ):
                continue
            short_burn = _window_burn(valid_prefix, bad_prefix, end, alert.short_minutes, allowed)
            if short_burn > alert.factor:
                first = end - 1 if first < 0 else first
                firing_minutes += 1
                peak = max(peak, long_burn)
        if first >= 0:
            results.append(Firing(slo.name, alert.name, alert.severity, first, firing_minutes, peak))
    return results


def _window_burn(valid_prefix: list[int], bad_prefix: list[int], end: int, width: int, allowed: float) -> float:
    start = max(0, end - width)
    valid = valid_prefix[end] - valid_prefix[start]
    if valid == 0:
        return 0.0
    return (bad_prefix[end] - bad_prefix[start]) / valid / allowed


def verdict(firings: Sequence[Firing]) -> str:
    """PAGE when any page-severity alert fired, TICKET when only ticket alerts did, otherwise NONE."""
    if any(firing.severity == "page" for firing in firings):
        return "PAGE"
    return "TICKET" if firings else "NONE"
