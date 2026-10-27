"""Finding the knee of a stepped load test and choosing a safe load from it."""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import median

from capacity.domain.errors import InvalidPlanError, NoKneeError

BASELINE_STEPS = 3
RISE_FACTOR = 3.0
STEP_ROUNDING = 25


@dataclass(frozen=True, slots=True)
class StepResult:
    """What one step of the load test measured, with latency counted from each request's scheduled arrival."""

    target_rps: float
    achieved_rps: float
    requests: int
    errors: int
    p50_ms: float
    p99_ms: float
    cpu_utilization: float | None = None
    max_ms: float = 0.0

    @property
    def error_rate(self) -> float:
        """Failed requests over all requests in the step."""
        return self.errors / self.requests if self.requests else 0.0


@dataclass(frozen=True, slots=True)
class Knee:
    """The load at which latency rose sharply, and the last load before it."""

    knee_rps: float
    last_good_rps: float
    baseline_p99_ms: float
    knee_p99_ms: float
    cpu_utilization_at_knee: float | None

    @property
    def saturated_resource(self) -> str:
        """Name the resource from independent evidence: CPU when the process was using nearly all of its limit."""
        if self.cpu_utilization_at_knee is not None and self.cpu_utilization_at_knee >= 0.9:
            return "cpu"
        return "unknown: throughput stopped rising but no resource was measured at its limit"


def find_knee(steps: list[StepResult], rise_factor: float = RISE_FACTOR) -> Knee:
    """The first step whose p99 exceeds `rise_factor` times the flat baseline, or that fails more than 1% of requests,
    and stays that bad on the next step (a spike that goes away again is noise, not a knee).

    The baseline is the median p99 of the first three steps. Steps must be in rising order of load. The last step
    needs no successor: the test stops a couple of steps past the knee, so the final step is a candidate by itself.
    """
    if len(steps) <= BASELINE_STEPS:
        raise NoKneeError(f"need more than {BASELINE_STEPS} steps to see a flat part and a knee")
    ordered = sorted(steps, key=lambda step: step.target_rps)
    baseline = median(step.p99_ms for step in ordered[:BASELINE_STEPS])

    def broken(step: StepResult) -> bool:
        return step.p99_ms > rise_factor * baseline or step.error_rate > 0.01

    for index in range(BASELINE_STEPS, len(ordered)):
        persists = index + 1 == len(ordered) or broken(ordered[index + 1])
        if broken(ordered[index]) and persists:
            step = ordered[index]
            return Knee(step.target_rps, ordered[index - 1].target_rps, baseline, step.p99_ms, step.cpu_utilization)
    raise NoKneeError("p99 never rose sharply and stayed up; raise the load further")


def safe_load(knee: Knee, margin: float = 0.25, step: int = STEP_ROUNDING) -> int:
    """A load a margin below the knee, rounded down to a multiple of `step`.

    The margin is a judgment call, not something derivable: it protects against measurement noise, a slightly
    different traffic mix and a busy neighbor. A quarter to 30 percent is the common starting point.
    """
    if not 0.0 < margin < 1.0:
        raise InvalidPlanError("the margin below the knee is a fraction between 0 and 1")
    return max(step, math.floor(knee.knee_rps * (1.0 - margin) / step) * step)
