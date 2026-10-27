"""Turn a stream of request events into SLIs, budgets and burn rates.

Counting is exact: every event is judged once and added to a per-minute tally, so the 28-day fraction is
the true good over valid and nothing is smoothed or pre-aggregated before the ratio is taken.
"""

from __future__ import annotations

import math
from array import array
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from capacity.domain.requests import RequestEvent
from capacity.domain.slo import Slo, budget_remaining_pct, burn_rate

_HISTOGRAM_RATIO = 1.01  # every bucket is 1 percent wider than the one before, so percentiles are within 1 percent


class MinuteTally:
    """Valid and good counts per minute for one SLO."""

    def __init__(self, minutes: int) -> None:
        """Allocate a zeroed tally for `minutes` minutes."""
        self.valid = array("I", bytes(4 * minutes))
        self.good = array("I", bytes(4 * minutes))

    @property
    def minutes(self) -> int:
        """Number of minutes covered."""
        return len(self.valid)

    def add(self, minute: int, valid: bool, good: bool) -> None:
        """Count one judged event into its minute. Events outside the window are ignored."""
        if valid and 0 <= minute < len(self.valid):
            self.valid[minute] += 1
            if good:
                self.good[minute] += 1

    def totals(self, start: int = 0, end: int | None = None) -> tuple[int, int]:
        """(valid, good) summed over minutes [start, end)."""
        stop = len(self.valid) if end is None else end
        return sum(self.valid[start:stop]), sum(self.good[start:stop])


@dataclass(slots=True)
class LatencyHistogram:
    """A log-bucketed latency histogram that answers percentiles within about 1 percent."""

    counts: dict[int, int] = field(default_factory=dict)
    total: int = 0

    def add(self, latency_ms: float) -> None:
        """Record one latency."""
        bucket = math.floor(math.log(max(latency_ms, 0.001)) / math.log(_HISTOGRAM_RATIO))
        self.counts[bucket] = self.counts.get(bucket, 0) + 1
        self.total += 1

    def percentile(self, fraction: float) -> float:
        """The latency at the given fraction (0.95 for p95), taken at the middle of its bucket."""
        if self.total == 0:
            return 0.0
        rank = fraction * self.total
        seen = 0
        for bucket in sorted(self.counts):
            seen += self.counts[bucket]
            if seen >= rank:
                return float(_HISTOGRAM_RATIO ** (bucket + 0.5))
        return 0.0


@dataclass(frozen=True, slots=True)
class SloReport:
    """The numbers for one SLO over the whole window."""

    slo: Slo
    valid: int
    good: int
    sli: float
    burn_rate: float
    budget_remaining_pct: float
    budget_minutes: float
    budget_requests: float
    p95_latency_ms: float

    def to_dict(self) -> dict[str, float | str]:
        """A flat, rounded view for printing and JSON."""
        return {
            "slo": self.slo.name,
            "sli": round(self.sli, 5),
            "target": self.slo.target,
            "burn_rate": round(self.burn_rate, 3),
            "budget_remaining_pct": round(self.budget_remaining_pct, 1),
            "budget_minutes_total_28d": round(self.budget_minutes, 2),
            "budget_requests": round(self.budget_requests, 1),
            "p95_latency_ms": round(self.p95_latency_ms, 1),
        }


class SliAccumulator:
    """Counts events against a set of SLOs in one pass."""

    def __init__(self, slos: Sequence[Slo]) -> None:
        """Prepare one tally per SLO and one latency histogram per journey."""
        self._slos = tuple(slos)
        self.tallies: dict[str, MinuteTally] = {slo.name: MinuteTally(slo.window_minutes) for slo in self._slos}
        self.latency: dict[str, LatencyHistogram] = {}
        self._by_journey: dict[str, list[Slo]] = {}
        for slo in self._slos:
            self._by_journey.setdefault(slo.journey, []).append(slo)

    def add(self, event: RequestEvent) -> None:
        """Judge one event against every SLO of its journey."""
        slos = self._by_journey.get(event.journey)
        if slos is None:
            return
        minute = int(event.minute)
        for slo in slos:
            valid, good = slo.judge(event.status, event.latency_ms)
            self.tallies[slo.name].add(minute, valid, good)
        if event.status < 500:
            self.latency.setdefault(event.journey, LatencyHistogram()).add(event.latency_ms)

    def consume(self, events: Iterable[RequestEvent]) -> SliAccumulator:
        """Add every event and return self, for chaining."""
        for event in events:
            self.add(event)
        return self

    def reports(self) -> dict[str, SloReport]:
        """One report per SLO over the whole window."""
        return {slo.name: self._report(slo) for slo in self._slos}

    def _report(self, slo: Slo) -> SloReport:
        valid, good = self.tallies[slo.name].totals()
        bad = valid - good
        rate = burn_rate(bad, valid, slo.allowed_bad_fraction)
        histogram = self.latency.get(slo.journey)
        return SloReport(
            slo=slo,
            valid=valid,
            good=good,
            sli=good / valid if valid else 1.0,
            burn_rate=rate,
            budget_remaining_pct=budget_remaining_pct(bad, valid, slo.allowed_bad_fraction),
            budget_minutes=slo.budget_minutes,
            budget_requests=slo.budget_requests(valid),
            p95_latency_ms=histogram.percentile(0.95) if histogram else 0.0,
        )


def worst_remaining(reports: Mapping[str, SloReport]) -> float:
    """The lowest budget-remaining percent across SLOs: the policy acts on the worst one."""
    return min((report.budget_remaining_pct for report in reports.values()), default=100.0)
