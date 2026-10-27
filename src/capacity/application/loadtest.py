"""The three load tests: the stepped knee search, the long soak at the chosen load, and the fleet run."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from capacity.application.ports import CpuProbe, OpenLoopRunner
from capacity.domain.errors import NoKneeError
from capacity.domain.knee import BASELINE_STEPS, Knee, StepResult, baseline_p99, find_knee, looks_broken, safe_load
from capacity.domain.latency import Sample, summarize, window_p99s

GENERATOR_LAG_LIMIT_MS = 5.0


@dataclass(frozen=True, slots=True)
class KneeReport:
    """The steps, the knee found in them (None when the load never reached it) and the load chosen below it."""

    steps: tuple[StepResult, ...]
    knee: Knee | None
    safe_rps: int | None
    margin: float
    generator_lag_p99_ms: float

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready, with the resource named."""
        return {
            "steps": [_step_dict(step) for step in self.steps],
            "knee": None
            if self.knee is None
            else {
                "knee_rps": self.knee.knee_rps,
                "last_good_rps": self.knee.last_good_rps,
                "baseline_p99_ms": self.knee.baseline_p99_ms,
                "knee_p99_ms": self.knee.knee_p99_ms,
                "cpu_utilization_at_knee": self.knee.cpu_utilization_at_knee,
                "saturated_resource": self.knee.saturated_resource,
            },
            "safe_rps": self.safe_rps,
            "margin_below_knee": self.margin,
            "generator_lag_p99_ms": round(self.generator_lag_p99_ms, 2),
        }


def _step_dict(step: StepResult) -> dict[str, Any]:
    return {
        "target_rps": step.target_rps,
        "achieved_rps": round(step.achieved_rps, 1),
        "requests": step.requests,
        "errors": step.errors,
        "p50_ms": step.p50_ms,
        "p99_ms": step.p99_ms,
        "max_ms": step.max_ms,
        "attempts": step.attempts,
        "cpu_utilization": step.cpu_utilization,
    }


async def knee_test(
    runner: OpenLoopRunner,
    probe: CpuProbe | None,
    steps_rps: list[float],
    step_seconds: float,
    margin: float = 0.25,
) -> KneeReport:
    """Step the load up, record each step, stop once the knee is clearly passed, and choose the safe load below it.

    The search stops once two steps beyond a knee that has persisted have been measured, which is enough to see the
    shape on both sides of it without driving the instance into the ground. A spike that goes away is not a knee.
    """
    results: list[StepResult] = []
    worst_lag = 0.0
    for rps in steps_rps:
        before = await probe.cpu_seconds() if probe else None
        samples, lag = await runner.run(rps, step_seconds)
        after = await probe.cpu_seconds() if probe else None
        utilization = None if before is None or after is None else (after - before) / step_seconds
        step = summarize(samples, rps, step_seconds, utilization)
        if len(results) >= BASELINE_STEPS and looks_broken(step, baseline_p99(results)):
            step = await _retry(runner, probe, step, rps, step_seconds)
        results.append(step)
        worst_lag = max(worst_lag, lag)
        found = _knee_so_far(results)
        if found is not None and sum(1 for step in results if step.target_rps > found.knee_rps) >= 2:
            break
    try:
        knee = find_knee(results)
    except NoKneeError:
        return KneeReport(tuple(results), None, None, margin, worst_lag)
    return KneeReport(tuple(results), knee, safe_load(knee, margin), margin, worst_lag)


async def _retry(
    runner: OpenLoopRunner, probe: CpuProbe | None, first: StepResult, rps: float, step_seconds: float
) -> StepResult:
    """Run a suspicious step once more and keep the better of the two.

    A real knee is there on the second try as well; a stall on a shared machine (a neighbor, a garbage collection, a
    descheduled generator) is not. The kept result says it took two attempts.
    """
    before = await probe.cpu_seconds() if probe else None
    samples, _ = await runner.run(rps, step_seconds)
    after = await probe.cpu_seconds() if probe else None
    utilization = None if before is None or after is None else (after - before) / step_seconds
    second = summarize(samples, rps, step_seconds, utilization)
    best = first if first.p99_ms <= second.p99_ms else second
    return replace(best, attempts=2)


def _knee_so_far(results: list[StepResult]) -> Knee | None:
    try:
        return find_knee(results)
    except NoKneeError:
        return None


@dataclass(frozen=True, slots=True)
class RunReport:
    """One timed run at a fixed load: soak, fleet or pool experiment."""

    label: str
    rps: float
    seconds: float
    result: StepResult
    window_p99_ms: tuple[float, ...]
    slo_p99_ms: float
    generator_lag_p99_ms: float

    @property
    def within_slo(self) -> bool:
        """True when no error occurred and no window's p99 exceeded the SLO."""
        return self.result.errors == 0 and all(p99 <= self.slo_p99_ms for p99 in self.window_p99_ms)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready."""
        return {
            "label": self.label,
            **_step_dict(self.result),
            "error_rate": round(self.result.error_rate, 4),
            "seconds": self.seconds,
            "window_p99_ms": list(self.window_p99_ms),
            "slo_p99_ms": self.slo_p99_ms,
            "within_slo": self.within_slo,
            "generator_lag_p99_ms": round(self.generator_lag_p99_ms, 2),
        }


async def timed_run(
    runner: OpenLoopRunner, label: str, rps: float, seconds: float, slo_p99_ms: float, window_s: float = 10.0
) -> RunReport:
    """Run at `rps` for `seconds` and report overall and per-window p99, so slow drift cannot hide in an average."""
    samples, lag = await runner.run(rps, seconds)
    return _report(label, rps, seconds, slo_p99_ms, window_s, samples, lag)


def _report(
    label: str, rps: float, seconds: float, slo: float, window_s: float, samples: list[Sample], lag: float
) -> RunReport:
    return RunReport(
        label, rps, seconds, summarize(samples, rps, seconds), tuple(window_p99s(samples, window_s)), slo, lag
    )
