"""Percentiles and per-step summaries of raw latency samples."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from capacity.domain.knee import StepResult


def percentile(values: Sequence[float], fraction: float) -> float:
    """The nearest-rank percentile (0.99 for p99) of unsorted values; 0.0 when there are none."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


@dataclass(frozen=True, slots=True)
class Sample:
    """One request: when it was scheduled to arrive, how long until its response was complete, and the outcome.

    `latency_s` is measured from the scheduled arrival, not from when the generator got round to sending it, so a
    queue anywhere (in the generator, the network or the service) shows up in the number instead of being hidden.
    """

    scheduled_s: float
    latency_s: float
    ok: bool


def summarize(
    samples: Sequence[Sample], target_rps: float, duration_s: float, cpu_utilization: float | None = None
) -> StepResult:
    """Collapse a step's samples into the numbers the knee search and the plan use."""
    latencies_ms = [sample.latency_s * 1000.0 for sample in samples]
    errors = sum(1 for sample in samples if not sample.ok)
    return StepResult(
        target_rps=target_rps,
        achieved_rps=len(samples) / duration_s if duration_s else 0.0,
        requests=len(samples),
        errors=errors,
        p50_ms=round(percentile(latencies_ms, 0.50), 2),
        p99_ms=round(percentile(latencies_ms, 0.99), 2),
        max_ms=round(max(latencies_ms, default=0.0), 2),
        cpu_utilization=None if cpu_utilization is None else round(cpu_utilization, 3),
    )


def window_p99s(samples: Sequence[Sample], window_s: float) -> list[float]:
    """p99 in milliseconds for each consecutive window of scheduled time, to see drift during a long run."""
    if not samples:
        return []
    buckets: dict[int, list[float]] = {}
    for sample in samples:
        buckets.setdefault(int(sample.scheduled_s // window_s), []).append(sample.latency_s * 1000.0)
    return [round(percentile(buckets[index], 0.99), 2) for index in sorted(buckets)]
