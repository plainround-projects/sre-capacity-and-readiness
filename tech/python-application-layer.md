# Python: the application layer, file by file

## What it is

The application layer is `src/capacity/application/`. It holds the **use cases**: step the load up and find the knee, run a soak, choose a pool size, replay burn-rate alerts over a request log, generate the Prometheus rules, render the capacity report and run the readiness review. It may import the domain layer and the standard library. Whatever it needs from the outside world (a load generator, a CPU reading, the repository's files, the stored evidence) it asks for through a **port**: a `Protocol` class that says "anything with these methods will do". This note walks through every file of the layer.

## Why this repository uses it

A load test normally takes minutes and needs Docker. By putting the load generator behind a port, a unit test can feed `knee_test` a scripted runner whose latency jumps at 400 requests a second and check in milliseconds that the knee is found, the retry happens and the safe load is 300. The readiness review works the same way: its checks read files and evidence through ports, so a test can give it a half-empty repository and watch the right check fail.

## Where it appears

| File | What it does |
| --- | --- |
| `application/__init__.py` | Package marker. |
| `application/ports.py` | The interfaces the use cases ask for. |
| `application/loadtest.py` | The knee search, the soak and the timed run. |
| `application/experiments.py` | The pool-size decisions. |
| `application/sli.py`, `application/alerting.py` | Counting events into SLIs and replaying burn-rate alerts. |
| `application/rules.py` | Generating the Prometheus rule files from the SLO definitions. |
| `application/capacity_report.py` | Rendering `docs/capacity-plan.md` and the runbook facts. |
| `application/readiness.py` | The eight-item readiness review. |
| `application/launch_decision.py` | Rendering the review as text and as `docs/launch-decision.md`. |

## How it works underneath

A class receives its collaborators in its constructor (**dependency injection**): `Reviewer(files, evidence, slos, config)` never opens a file itself. A `Protocol` is checked only by the type checker (mypy), not at run time: any object with the right methods fits, with no inheritance. Functions that talk to a port are `async` when the port is (`async def run(...)`), and the caller uses `await` to wait for the answer.

## Walkthrough of this repository's use

### `application/__init__.py`

```python
"""Use cases: load tests, SLI reports, alert replay, rule generation and the readiness review."""
```

A one-line docstring that names what the layer holds.

### `application/ports.py`: what the use cases ask for

```python
"""Ports: what the load-test use cases and the readiness review ask the outside world for."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Protocol

from capacity.domain.latency import Sample
from capacity.domain.slo import BurnAlert, Slo


class OpenLoopRunner(Protocol):
    """Sends requests on a fixed schedule, whether or not earlier ones finished."""

    async def run(self, rps: float, duration_s: float) -> tuple[list[Sample], float]:
        """Return every request's sample and the generator's own p99 lag in milliseconds (how late it sent)."""
        ...
```

The docstring says a port is what a use case asks the outside world for. `from __future__ import annotations` makes type hints text so they can mention classes defined later. `date` is a calendar date, `Path` a file-system path, `Protocol` the base class for ports. `Sample` is one measured request and `BurnAlert` and `Slo` come from the domain. `OpenLoopRunner.run(rps, duration_s)` sends requests on a fixed schedule whether or not earlier ones finished and returns every request's sample plus the generator's own p99 lag (how late it sent, in milliseconds). The `...` body means "no code here; a real class provides it".

```python
class CpuProbe(Protocol):
    """Reads how much CPU time the process under test has used."""

    async def cpu_seconds(self) -> float:
        """Cumulative CPU seconds of the instance."""
        ...


class Clock(Protocol):
    """Time, injected so tests are deterministic."""

    def monotonic(self) -> float:
        """Seconds on a monotonic clock."""
        ...

    def today(self) -> date:
        """Today's date."""
        ...
```

`CpuProbe.cpu_seconds()` returns the cumulative CPU seconds the instance under test has used; two readings and a division give the utilization of a step. `Clock` has `monotonic()` (seconds that only go forward, for measuring durations) and `today()` (the calendar date, for review due dates); injecting it lets a test fix time.

```python
class ProjectFiles(Protocol):
    """Read-only access to the repository, for checks that look at files."""

    def exists(self, relative: str) -> bool:
        """True when the file or folder exists."""
        ...

    def read(self, relative: str) -> str:
        """The file's text."""
        ...

    def glob(self, pattern: str) -> list[str]:
        """Relative paths matching a glob."""
        ...

    @property
    def root(self) -> Path:
        """The repository root."""
        ...
```

`ProjectFiles` gives the review read-only access to the repository: `exists(relative)`, `read(relative)` (the text), `glob(pattern)` (relative paths matching, such as `dashboards/*.json`) and the `root` property (decorated with `@property`, so it is read like an attribute).

```python
class EvidenceStore(Protocol):
    """Measured results written by the make targets, one JSON document per experiment."""

    def load(self, name: str) -> dict[str, object] | None:
        """The evidence called `name`, or None when the experiment has not been run."""
        ...


class SloCatalog(Protocol):
    """The SLO definitions and their generated artifacts, as the readiness review sees them."""

    def slos(self) -> list[Slo]:
        """Every SLO defined in `slo/slos.yaml`."""
        ...

    def alerts(self) -> list[BurnAlert]:
        """The burn-rate alert table."""
        ...

    def rules_in_sync(self) -> bool:
        """True when the committed Prometheus rule files are exactly what the definitions generate."""
        ...


class ChecklistConfig(Protocol):
    """Who owns each checklist item and by when a failure of it is fixed."""

    def owner(self, item_id: str) -> str:
        """The role that owns the item."""
        ...

    def due(self, item_id: str) -> date:
        """The date by which a failure of the item is fixed."""
        ...

    def blocking(self, item_id: str) -> bool:
        """True when a failure of the item must be fixed before launch."""
        ...

    def accepted_risks(self) -> list[dict[str, str]]:
        """Risks the team has accepted: area, check, evidence, owner, revisit_when."""
        ...
```

`EvidenceStore.load(name)` returns the JSON document a make target wrote for an experiment (or `None` when it has not been run): the review never trusts a claim, only a measurement. `SloCatalog` gives the SLO definitions, the burn-alert table and whether the committed rule files equal what the definitions generate. `ChecklistConfig` gives, for each checklist item, the owning role, the date by which a failure is fixed, whether a failure blocks launch, and the list of accepted risks.

### `application/loadtest.py`: the three load tests

```python
"""The three load tests: the stepped knee search, the long soak at the chosen load, and the fleet run."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from capacity.application.ports import CpuProbe, OpenLoopRunner
from capacity.domain.errors import NoKneeError
from capacity.domain.knee import BASELINE_STEPS, Knee, StepResult, baseline_p99, find_knee, looks_broken, safe_load
from capacity.domain.latency import Sample, summarize, window_p99s

GENERATOR_LAG_LIMIT_MS = 5.0
SATURATED_CPU = 0.9  # a core this busy is saturated; a run that spiked with the core idler than this was disturbed

```

The docstring names the three tests. `dataclass` builds simple record classes, `replace` makes a modified copy of a frozen one. `GENERATOR_LAG_LIMIT_MS = 5.0` is the most the generator may be late and still be trusted: if the tool measuring the instance falls behind, its numbers describe the tool.

```python
@dataclass(frozen=True, slots=True)
class KneeReport:
    """The steps, the knee found in them (None when the load never reached it) and the load chosen below it."""

    steps: tuple[StepResult, ...]
    knee: Knee | None
    safe_rps: int | None
    margin: float
    generator_lag_p99_ms: float

    @property
    def problem(self) -> str | None:
        """Why these numbers cannot be trusted, or None. An untrustworthy knee is never saved as evidence."""
        if self.generator_lag_p99_ms > GENERATOR_LAG_LIMIT_MS:
            return f"the load generator itself was late by {self.generator_lag_p99_ms:.1f} ms at p99"
        if self.knee is None:
            return "the load never reached a knee"
        if self.knee.saturated_resource != "cpu":
            used = self.knee.cpu_utilization_at_knee
            share = "an unmeasured share" if used is None else f"{used * 100:.0f}%"
            return (
                f"p99 rose at {self.knee.knee_rps:g} rps with {share} of the core in use, so the host, not the "
                "instance, ran short of something"
            )
        return None

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
```

`KneeReport` holds the steps, the knee found (or `None` when the load never reached it), the safe load chosen below it, the margin and the generator's worst lag. `to_dict` produces the JSON that is saved as evidence; the knee is written out with its `saturated_resource` so the report names the bottleneck from measurement.

```python
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
```

`_step_dict` writes one step as a plain dictionary, rounding the achieved rate to one decimal.

```python
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

```

`knee_test` walks the list of target rates. For each it reads the CPU counter before and after (`probe` may be `None`), runs the open-loop load, computes `utilization = CPU seconds used / step seconds` and summarizes the samples. After the first `BASELINE_STEPS` steps, a step that `looks_broken` against the baseline is run once more (`_retry`). It stops early once the knee has been found and two steps beyond it are measured, which shows the curve on both sides without driving the instance into the ground. If `find_knee` raises `NoKneeError` the report carries `knee=None` and `safe_rps=None`, an honest "the load never reached the knee".

```python
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
```

`_retry` runs a suspicious step again and keeps the better (lower p99) of the two, saying it took two attempts: a real knee is there both times, while a stall on a shared machine (a neighbor, a garbage collection, a descheduled generator) is not. `_knee_so_far` is `find_knee` that returns `None` instead of raising, used for the early stop.

```python
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

    @property
    def spiked(self) -> bool:
        """True when a window left the SLO although no request failed: saturation, or a disturbed machine."""
        return self.result.errors == 0 and not self.within_slo

    @property
    def problem(self) -> str | None:
        """Why these numbers cannot be trusted, or None. An untrustworthy run is never saved as evidence."""
        if self.generator_lag_p99_ms > GENERATOR_LAG_LIMIT_MS:
            return f"the load generator itself was late by {self.generator_lag_p99_ms:.1f} ms at p99"
        used = self.result.cpu_utilization
        if self.spiked and used is not None and used < SATURATED_CPU:
            return (
                f"a window's p99 left the SLO with no errors while the instance used only {used * 100:.0f}% of its "
                "core, so the host, not the instance, was the cause"
            )
        return None

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
```

`RunReport` is one timed run at a fixed load. `within_slo` is true only when the run had no errors **and** every window's p99 stayed at or under the SLO; per-window p99s keep a slow drift from hiding in an overall average. `to_dict` adds the error rate and the verdict to the step fields.

```python
async def timed_run(
    runner: OpenLoopRunner,
    label: str,
    rps: float,
    seconds: float,
    slo_p99_ms: float,
    window_s: float = 10.0,
    probe: CpuProbe | None = None,
    attempts: int = 1,
) -> RunReport:
    """Run at `rps` for `seconds` and report overall and per-window p99, so slow drift cannot hide in an average.

    A run that spiked with no failed request (or with a late generator) is run again, up to `attempts` times, and the
    last one is reported: a real shortage is there every time, a stall on a shared machine is not.
    """
    report: RunReport | None = None
    for attempt in range(1, attempts + 1):
        before = await probe.cpu_seconds() if probe else None
        samples, lag = await runner.run(rps, seconds)
        after = await probe.cpu_seconds() if probe else None
        utilization = None if before is None or after is None else (after - before) / seconds
        report = _report(label, rps, seconds, slo_p99_ms, window_s, samples, lag, utilization)
        report = replace(report, result=replace(report.result, attempts=attempt))
        if lag <= GENERATOR_LAG_LIMIT_MS and not report.spiked:
            break
    assert report is not None
    return report


def _report(
    label: str,
    rps: float,
    seconds: float,
    slo: float,
    window_s: float,
    samples: list[Sample],
    lag: float,
    utilization: float | None,
) -> RunReport:
    return RunReport(
        label,
        rps,
        seconds,
        summarize(samples, rps, seconds, utilization),
        tuple(window_p99s(samples, window_s)),
        slo,
        lag,
    )
```

`timed_run` runs at the given rate for the given seconds and builds the report; `_report` summarizes the samples and the per-window p99s (windows of 10 seconds by default).

#### Runs that cannot be trusted

A load test on a shared machine can produce a clean-looking number that describes the machine. The use case now says so itself.

```python
SATURATED_CPU = 0.9  # a core this busy is saturated; a run that spiked with the core idler than this was disturbed
```

`SATURATED_CPU = 0.9` is the share of a core above which the instance counts as saturated; below it, a latency problem is probably not the instance's. `KneeReport.problem` and `RunReport.problem` return a sentence saying why the numbers cannot be trusted, or `None`:

```python
def problem(self) -> str | None:
    """Why these numbers cannot be trusted, or None. An untrustworthy knee is never saved as evidence."""
    if self.generator_lag_p99_ms > GENERATOR_LAG_LIMIT_MS:
        return f"the load generator itself was late by {self.generator_lag_p99_ms:.1f} ms at p99"
    if self.knee is None:
        return "the load never reached a knee"
    if self.knee.saturated_resource != "cpu":
        used = self.knee.cpu_utilization_at_knee
        share = "an unmeasured share" if used is None else f"{used * 100:.0f}%"
        return (
            f"p99 rose at {self.knee.knee_rps:g} rps with {share} of the core in use, so the host, not the "
            "instance, ran short of something"
        )
    return None
```

For a knee: the load generator itself was late by more than 5 ms at p99 (it was not sending on schedule, so the latency includes its own queueing); no knee was found; or a knee was found where the instance used less than 90% of its core, so something else (the host) ran short.

```python
def spiked(self) -> bool:
    """True when a window left the SLO although no request failed: saturation, or a disturbed machine."""
    return self.result.errors == 0 and not self.within_slo
```

For a timed run, `spiked` is true when a window's p99 left the SLO although no request failed. A real shortage of capacity looks the same, so the second property needs more evidence:

```python
def problem(self) -> str | None:
    """Why these numbers cannot be trusted, or None. An untrustworthy run is never saved as evidence."""
    if self.generator_lag_p99_ms > GENERATOR_LAG_LIMIT_MS:
        return f"the load generator itself was late by {self.generator_lag_p99_ms:.1f} ms at p99"
    used = self.result.cpu_utilization
    if self.spiked and used is not None and used < SATURATED_CPU:
        return (
            f"a window's p99 left the SLO with no errors while the instance used only {used * 100:.0f}% of its "
            "core, so the host, not the instance, was the cause"
        )
    return None
```

A late generator is a problem; so is a spike with no errors while the instance used under 90% of its core (the probe measured it), which says the host, not the instance, caused the spike.

```python
async def timed_run(
    runner: OpenLoopRunner,
    label: str,
    rps: float,
    seconds: float,
    slo_p99_ms: float,
    window_s: float = 10.0,
    probe: CpuProbe | None = None,
    attempts: int = 1,
) -> RunReport:
    """Run at `rps` for `seconds` and report overall and per-window p99, so slow drift cannot hide in an average.

    A run that spiked with no failed request (or with a late generator) is run again, up to `attempts` times, and the
    last one is reported: a real shortage is there every time, a stall on a shared machine is not.
    """
    report: RunReport | None = None
    for attempt in range(1, attempts + 1):
        before = await probe.cpu_seconds() if probe else None
        samples, lag = await runner.run(rps, seconds)
        after = await probe.cpu_seconds() if probe else None
        utilization = None if before is None or after is None else (after - before) / seconds
        report = _report(label, rps, seconds, slo_p99_ms, window_s, samples, lag, utilization)
        report = replace(report, result=replace(report.result, attempts=attempt))
        if lag <= GENERATOR_LAG_LIMIT_MS and not report.spiked:
            break
    assert report is not None
    return report
```

`timed_run` now takes an optional CPU `probe` and a number of `attempts`. It runs the load, reads the CPU before and after, builds the report with the utilization and records which attempt this was. It stops at the first run whose generator was on time and that did not spike without errors; otherwise it runs again, up to `attempts` times, and reports the last. A stall on a shared machine does not repeat; a real shortage does. The `assert report is not None` tells the type checker that the loop ran at least once.

### `application/experiments.py`: pool-size decisions

```python
"""Pure decisions the experiments make: which pool size to adopt, and whether a run proved what it set out to."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

POOL_HEADROOM = 1.25
POOL_STEP = 5
ZONES = ("a", "b", "c")
COMPOSE_FLEET = 9  # instances docker-compose.yml defines: three in each zone


def naive_pool_size(peak_rps: float, hold_ms: float) -> int:
    """The textbook sizing: peak requests per second times the average time a request holds a connection."""
    return math.ceil(peak_rps * hold_ms / 1000.0)


def sweep_sizes(naive: int) -> list[int]:
    """Pool sizes to try, from the naive number upward in steps, until well past it."""
    return [naive, naive + 4, naive + 10, naive + 20]


def choose_pool_size(results: list[dict[str, Any]]) -> int | None:
    """The smallest size that held the SLO, plus 25 percent headroom rounded up to a step of five; None if none held.

    The smallest size that holds is the knee of the pool, not a place to operate: a pool exactly big enough has no
    room for a burst, so the adopted size keeps a margin above it.
    """
    holding = sorted(int(str(result["pool_size"])) for result in results if result["within_slo"] is True)
    if not holding:
        return None
    return math.ceil(holding[0] * POOL_HEADROOM / POOL_STEP) * POOL_STEP
```

`POOL_HEADROOM = 1.25` is the 25% margin and `POOL_STEP = 5` the rounding step. `naive_pool_size` is the textbook rule, peak requests per second times the seconds a request holds a connection (hold time in milliseconds divided by 1,000), rounded up with `math.ceil`. `sweep_sizes` lists the pool sizes to try: the naive size, then 4, 10 and 20 more. `choose_pool_size` sorts the sizes whose run held the SLO, takes the smallest, multiplies by 1.25, rounds **up** to a multiple of five and returns it (or `None` when no size held). The smallest size that holds is the pool's knee, not a place to operate.

#### Which instances run

```python
ZONES = ("a", "b", "c")
COMPOSE_FLEET = 9  # instances docker-compose.yml defines: three in each zone
```

`ZONES` are the three zone letters and `COMPOSE_FLEET` the nine instances `docker-compose.yml` defines.

```python
def fleet_instances(distribution: Sequence[int]) -> list[str]:
    """The service names of a fleet spread as `distribution` says over zones a, b and c, such as (3, 3, 2)."""
    return [
        f"app-{zone}{number}"
        for zone, count in zip(ZONES, distribution, strict=False)
        for number in range(1, count + 1)
    ]
```

`fleet_instances((3, 3, 2))` pairs each zone letter with its count (`zip`, with `strict=False` because a plan may give fewer counts than there are zones) and numbers the instances from 1: `app-a1` to `app-c2`.

```python
def spare_instances(distribution: Sequence[int]) -> list[str]:
    """The instances compose defines that the plan does not run."""
    wanted = set(fleet_instances(distribution))
    return [name for name in fleet_instances((COMPOSE_FLEET // len(ZONES),) * len(ZONES)) if name not in wanted]
```

`spare_instances` is every instance compose defines (a fleet of 3 in each of the 3 zones) that the plan does not use: for `(3, 3, 2)` it is `["app-c3"]`. `make set-fleet` stops them.

### `application/sli.py`: counting events

```python
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

```

The docstring states the counting rule: every event is judged once and added to a per-minute tally, so the 28-day fraction is the true good over valid and nothing is smoothed first. `array` is a compact typed list, `math` gives `log` and `floor`. `_HISTOGRAM_RATIO = 1.01` makes every latency bucket 1% wider than the one before, so percentiles are within 1%.

```python
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

```

`MinuteTally` keeps two arrays of unsigned 32-bit integers (`"I"`), one slot per minute, for the **valid** and the **good** count. `bytes(4 * minutes)` is a block of zero bytes of the right size. `add` counts one judged event into its minute and silently ignores events outside the window; `totals(start, end)` sums the counts over minutes `[start, end)`.

```python
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

```

`LatencyHistogram` answers percentiles without keeping every latency: `add` puts a latency in bucket `floor(log(latency) / log(1.01))` (latency is floored at 0.001 ms to keep `log` defined), and `percentile` walks the buckets in order until the running count reaches `fraction * total`, then reports the middle of that bucket as `1.01 ** (bucket + 0.5)`.

```python
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

```

`SloReport` is the finished numbers for one SLO over the whole window; `to_dict` rounds them for printing and JSON.

```python
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

```

`SliAccumulator` counts events against several SLOs in one pass. Its constructor builds one `MinuteTally` per SLO and an index from journey to its SLOs. `add` judges an event against every SLO of its journey and records the latency of successful ones in that journey's histogram. `consume` adds a whole stream and returns `self`. `reports` builds one `SloReport` per SLO; `_report` computes the SLI (good over valid, or 1.0 when there is no traffic), the burn rate, the budget remaining and the p95.

```python
def worst_remaining(reports: Mapping[str, SloReport]) -> float:
    """The lowest budget-remaining percent across SLOs: the policy acts on the worst one."""
    return min((report.budget_remaining_pct for report in reports.values()), default=100.0)
```

`worst_remaining` is the lowest budget-remaining percent across SLOs (100 when there are none), because a policy acts on the worst one.

### `application/alerting.py`: replaying burn-rate alerts

```python
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

```

`Firing` is one alert that fired during a replay: which SLO, which alert, its severity, the first minute it fired, how many minutes it stayed firing and the highest long-window burn.

```python
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
```

`evaluate` replays a whole tally. It builds **prefix sums** (`accumulate`): `valid_prefix[i]` is the total valid events in the first `i` minutes, so any window's total is one subtraction. For each alert and each end minute from the first full long window onward, it computes the long-window burn and skips unless it exceeds the factor **and** the long window holds at least `min_bad_events` bad events; then it computes the short-window burn and counts a firing minute only when that exceeds the factor too. This is the same rule the Prometheus alerts use.

```python
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
```

`_window_burn` is bad over valid over the allowed fraction for the last `width` minutes (0 when there is no traffic). `verdict` summarizes a list of firings: PAGE if any page-severity alert fired, TICKET if only tickets did, otherwise NONE.

### `application/rules.py`: generating the Prometheus rules

```python
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

```

The docstring says the rules are produced from `slo/slos.yaml` and never edited by hand; `window_scale` divides every window to make a demo profile that fires in seconds. The imports bring the edge metric names; `BAD_CLASS` is the label selector for 5xx responses at the edge (`envoy_response_code_class="5"`).

```python
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
```

`duration` formats seconds as a Prometheus duration using the largest exact unit (`1h`, `5m`, `90s`). `bucket_label` returns the `le` value for a latency threshold and raises `ValueError` when the edge has no histogram bucket at that boundary, which turns a typo into an error at generation time instead of a silently wrong alert.

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

`error_ratio_expr` builds the PromQL for the bad fraction over a window, measured at the Envoy edge. Availability: the rate of 5xx responses (or `vector(0)` when there are none) divided by the rate of all responses. Latency: 1 minus the rate of requests at or under the threshold bucket divided by the rate of all requests. `bad_events_expr` is the same with `increase` instead of `rate`, giving the number of bad events for the low-traffic guard.

```python
def camel(*parts: str) -> str:
    """`follow_link`, `availability` -> `FollowLinkAvailability` for alert names."""
    return "".join(word.capitalize() for part in parts for word in part.split("_"))

```

`camel` joins snake_case words into CamelCase for alert names.

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
```

`build_rules` returns the rule-file document. It collects every window the alerts use (long and short, in seconds) plus the 28-day window, divides by `window_scale` and sorts them. For each SLO it adds a recording rule `slo:sli_error:ratio_rate<window>` for every window, the budget-remaining rule built from the 28-day one, and the bad-events rules for each long window.

```python
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
```

For each burn alert it writes an alerting rule named from the SLO and alert (`CamelCase`, plus the profile suffix) whose expression is three conditions joined by `and`: the long window's error ratio is above the threshold (`factor × allowed fraction`), the short window's is too, and the long window holds at least `min_bad_events` bad events. The labels carry the severity, SLO and journey; the annotations explain the alert in words and point at the runbook.

```python
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

One group per SLO keeps each recording rule next to the alerts that read it. A final group `slo-meta` holds the blind-spot alert `EdgeMetricsAbsent`, which fires when `absent(envoy_http_downstream_rq_xx)` stays true for five minutes (scaled): if the edge exports nothing, every SLI is blind and that is itself a page.

### `application/capacity_report.py`: the capacity plan from the evidence

```python
"""Render `docs/capacity-plan.md` from the measurements, so the plan's numbers cannot drift from the evidence."""

from __future__ import annotations

from typing import Any

from capacity.application.ports import EvidenceStore

REQUIRED = (
    "knee",
    "soak",
    "plan",
    "fleet-full",
    "fleet-zone-loss",
    "pool-sweep",
    "fleet-pool-naive",
    "fleet-pool-resized",
)


def missing(evidence: EvidenceStore) -> list[str]:
    """Names of the evidence documents the report needs that have not been produced yet."""
    return [name for name in REQUIRED if evidence.load(name) is None]


def render(evidence: EvidenceStore) -> str:
    """The capacity plan as Markdown: facts, the knee, the safe load, the formula, the fleet checks, the next "
    "bottleneck."""
    docs: dict[str, Any] = {name: evidence.load(name) for name in REQUIRED}
    return "\n".join(
        [
            *_header(docs["plan"]),
            *_knee_section(docs["knee"]),
            *_safe_load_section(docs["knee"], docs["soak"]),
            *_fleet_section(docs["plan"], docs["fleet-full"], docs["fleet-zone-loss"]),
            *_pool_section(docs["pool-sweep"], docs["fleet-pool-resized"]),
        ]
    )

```

`REQUIRED` lists the eight evidence documents the report needs. `missing` returns the names not produced yet, so `make plan-doc` can say "run `make run`" instead of failing in the middle. `render` loads each document and joins the section lists with newlines; the plan's numbers cannot drift from the evidence because they are never typed in.

```python
def _header(plan: dict[str, Any]) -> list[str]:
    return [
        "# Capacity plan",
        "",
        "Generated by `make plan-doc` from the measurements in `docs/evidence/`. Every number below traces to one "
        "of the",
        "lesson's three facts (1,000 requests a second at peak, 2,000 in six months, p99 under 50 ms) or to something",
        "measured in this repository.",
        "",
        "## The facts",
        "",
        f"- Peak today: **{plan['current_peak_rps']:g}** requests a second. In six months: "
        f"**{plan['future_peak_rps']:g}** "
        f"(growth {plan['growth_factor']:g}x).",
        "- Latency SLO: **p99 under 50 ms**.",
        "- Each instance is a container with a fixed 1.0 CPU and 256 MiB; a request burns real CPU "
        "(`WORK_ROUNDS` rounds of SHA-256).",
        "",
    ]

```

`_header` writes the title, the sentence that says the file is generated, and "The facts": today's peak, the six-month peak, the growth, the 50 ms p99 target and the instance size.

```python
def _knee_section(knee: dict[str, Any]) -> list[str]:
    detail = knee["knee"]
    lines = [
        "## 1. The knee of one instance",
        "",
        f"Open-loop stepped load, {knee['step_seconds']:g} seconds a step, latency from each request's scheduled "
        f"arrival.",
        "",
        "| Target rps | Achieved rps | p50 ms | p99 ms | Errors | CPU of its one core |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for step in knee["steps"]:
        cpu = "-" if step["cpu_utilization"] is None else f"{step['cpu_utilization'] * 100:.0f}%"
        lines.append(
            f"| {step['target_rps']:g} | {step['achieved_rps']:g} | {step['p50_ms']} | {step['p99_ms']} "
            f"| {step['errors']} | {cpu} |"
        )
    lines += [
        "",
        f"p99 stays near {detail['baseline_p99_ms']} ms up to **{detail['last_good_rps']:g} rps** and jumps to "
        f"**{detail['knee_p99_ms']} ms at {detail['knee_rps']:g} rps**. The instance was using "
        f"{detail['cpu_utilization_at_knee'] * 100:.0f}% of its one core there and nothing else is in the request "
        f"path, "
        f"so the resource that saturated first is **{detail['saturated_resource']}**.",
        "",
    ]
    return lines
```

`_knee_section` writes the table of steps (target and achieved rate, p50, p99, errors, CPU of its one core) and the sentence that names the knee, the baseline and the saturated resource.

```python
def _safe_load_section(knee: dict[str, Any], soak: dict[str, Any]) -> list[str]:
    return [
        "## 2. The safe load per instance",
        "",
        f"**{knee['safe_rps']} requests a second**: {knee['margin_below_knee'] * 100:.0f}% below the knee of "
        f"{knee['knee']['knee_rps']:g}, rounded down to a multiple of 25. The margin is a judgment call, not a "
        f"derivation: "
        "it covers measurement noise, a different traffic mix and a busy neighbor.",
        "",
        f"A {soak['seconds']:g}-second soak at that load (the lesson's real run is 30 minutes: `SOAK_SECONDS=1800`) "
        f"kept p99 "
        f"at {soak['p99_ms']} ms overall, worst {soak['window_p99_ms'] and max(soak['window_p99_ms'])} ms in any "
        f"10-second window, with {soak['errors']} errors.",
        "",
    ]
```

`_safe_load_section` states the safe load, the margin and why it is a judgment call, and the soak's result: overall p99, the worst window and the error count.

```python
def _fleet_section(plan: dict[str, Any], full: dict[str, Any], zone: dict[str, Any]) -> list[str]:
    spread = "/".join(map(str, plan["distribution"]))
    return [
        "## 3. The fleet, with headroom for a lost zone",
        "",
        "```text",
        plan["formula"],
        "```",
        "",
        f"Total **{plan['total_instances']}** instances in three zones ({spread}); losing the largest zone leaves "
        f"**{plan['surviving_after_zone_loss']}**, which carry {plan['future_peak_rps']:g} rps at "
        f"{plan['safe_rps_per_instance']:g} each. Headroom is computed from the six-month peak, not today's.",
        "",
        "| Run (open loop, through Envoy) | Healthy instances | Target rps | p99 ms | Errors | Within SLO |",
        "| --- | --- | --- | --- | --- | --- |",
        _row(full, "all up, no shared dependency yet"),
        _row(zone, "the largest zone stopped"),
        "",
    ]
```

`_fleet_section` shows the formula, the number of instances in three zones, the survivors after losing the largest zone, and a table of the two fleet runs (all up, and the largest zone stopped).

```python
def _pool_section(sweep: dict[str, Any], resized: dict[str, Any]) -> list[str]:
    lines = [
        "## 4. The next bottleneck: the shared ledger's connection pool",
        "",
        f"Adding the ledger back (every request records an entry and holds a pooled connection for "
        f"{sweep['hold_ms']:g} ms) "
        f"and sizing its pool as peak times hold time, **{sweep['naive_pool_size']}** connections, "
        "was not enough. Stepping the pool up at the projected peak:",
        "",
        "| Pool size | p99 ms | Error rate | Within SLO |",
        "| --- | --- | --- | --- |",
    ]
    for run in sweep["sweep"]:
        verdict = "yes" if run["within_slo"] else "no"
        lines.append(f"| {run['pool_size']} | {run['p99_ms']} | {run['error_rate']} | {verdict} |")
    lines += [
        "",
        f"The smallest size that held, plus 25% headroom, rounded up to a multiple of five, is "
        f"**{resized['pool_size']}**; "
        f"confirmed at the projected peak: p99 {resized['p99_ms']} ms, {resized['errors']} errors. "
        "The pool belongs in the plan next to the instance count: adding instances does not enlarge it.",
        "",
    ]
    return lines
```

`_pool_section` explains the next bottleneck, the shared ledger's connection pool: the naive size failed, the table shows the sweep and the resized pool is the smallest size that held plus 25% headroom.

```python
def _row(run: dict[str, Any], label: str) -> str:
    verdict = "yes" if run["within_slo"] else "no"
    cells = [label, run["healthy_instances"], f"{run['target_rps']:g}", run["p99_ms"], run["errors"], verdict]
    return "| " + " | ".join(str(cell) for cell in cells) + " |"
```

`_row` renders one fleet run as a Markdown table row.

```python
def runbook_facts(evidence: EvidenceStore) -> dict[str, str]:
    """The measured numbers each runbook quotes, by runbook file name; `make plan-doc` writes them into the runbooks."""
    docs: dict[str, Any] = {name: evidence.load(name) for name in REQUIRED}
    knee, plan, sweep, resized = docs["knee"], docs["plan"], docs["pool-sweep"], docs["fleet-pool-resized"]
    detail = knee["knee"]
    spread = "/".join(map(str, plan["distribution"]))
    return {
        "cpu-knee.md": (
            f"One instance saturates at **{detail['knee_rps']:g} requests a second**: p99 jumps from "
            f"{detail['baseline_p99_ms']} ms to {detail['knee_p99_ms']} ms there, with "
            f"{detail['cpu_utilization_at_knee'] * 100:.0f}% of its one core in use. The safe load is "
            f"**{knee['safe_rps']} requests a second** per instance."
        ),
        "pool-exhaustion.md": (
            f"Sizing the pool as peak rate times hold time (**{sweep['naive_pool_size']}** connections for "
            f"{plan['future_peak_rps']:g} requests a second at {sweep['hold_ms']:g} ms) failed at the projected peak; "
            f"**{resized['pool_size']}** held it (the sweep is in `docs/capacity-plan.md`)."
        ),
        "zone-loss.md": (
            f"The fleet is **{plan['total_instances']}** instances over three zones ({spread}). Losing the largest "
            f"zone "
            f"leaves **{plan['surviving_after_zone_loss']}**, and {plan['surviving_after_zone_loss']} instances at "
            f"{plan['safe_rps_per_instance']:g} requests a second carry the projected peak of "
            f"{plan['future_peak_rps']:g}. "
            f"`FleetBelowPeakCapacity` pages below **{plan['instances_for_peak']}** healthy instances and "
            f"`FleetBelowFullStrength` opens a ticket below **{plan['total_instances']}**."
        ),
    }
```

`runbook_facts` returns, by runbook file name, the measured sentence each runbook quotes between the `<!-- measured -->` markers: the knee and safe load for `cpu-knee.md`, the pool sizes for `pool-exhaustion.md` and the fleet and alert thresholds for `zone-loss.md`. `make plan-doc` writes them in, so a runbook cannot disagree with the measurement.

### `application/readiness.py`: the review

```python
"""The readiness review: run every check against the repository and the measured evidence, then decide.

A check that cannot find its evidence fails. The review is only worth running if it can fail something, so no check
passes on the strength of a file merely existing when the lesson asks for a measurement.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from capacity.application.ports import ChecklistConfig, EvidenceStore, ProjectFiles, SloCatalog
from capacity.domain.readiness import Decision, ReviewItem, Verdict, decide
from capacity.domain.telemetry import EXPORTED_METRICS, RECORDED_RULE_PREFIX

REQUIRED_RUNBOOKS = {
    "CPU saturation past the knee": "docs/runbooks/cpu-knee.md",
    "Shared pool exhaustion": "docs/runbooks/pool-exhaustion.md",
    "Loss of a zone": "docs/runbooks/zone-loss.md",
}
RUNBOOK_HEADINGS = ("Symptoms", "Mitigate", "Diagnose")
_PROMQL_WORDS = {
    "sum", "rate", "increase", "irate", "avg", "max", "min", "count", "vector", "time", "abs", "ceil", "floor",
    "histogram_quantile", "clamp_min", "clamp_max", "label_replace", "by", "without", "on", "ignoring",
}  # fmt: skip

```

The docstring states the rule that makes a review worth running: a check that cannot find its evidence fails, and no check passes because a file merely exists when the lesson asks for a measurement. `REQUIRED_RUNBOOKS` maps each of the top three failures to its runbook file; `RUNBOOK_HEADINGS` are the three sections each must have. `_PROMQL_WORDS` is the set of PromQL function and keyword names to ignore when looking for metric names (`# fmt: skip` tells the formatter to leave the set as written).

```python
@dataclass(frozen=True, slots=True)
class Check:
    """One checklist line: an id (for the owner and date in the checklist file), its area, what it asserts, and how."""

    item_id: str
    area: str
    asserts: str
    run: Callable[[], tuple[bool, str]]


class Reviewer:
    """Runs the checklist."""

    def __init__(self, files: ProjectFiles, evidence: EvidenceStore, slos: SloCatalog, config: ChecklistConfig) -> None:
        """Wire the ports."""
        self._files = files
        self._evidence = evidence
        self._slos = slos
        self._config = config

```

`Check` is one checklist line: an id (for the owner and date in the checklist file), the area, what it asserts and a function that runs it and returns `(passed, evidence)`. `Reviewer` receives the four ports.

```python
def checks(self) -> list[Check]:
    """The checklist, in the order a reviewer would walk it."""
    return [
        Check(
            "slos",
            "SLOs and burn-rate alerts",
            "Page and ticket alerts are generated from the SLO file, in sync, and replayed with promtool",
            self._slo_check,
        ),
        Check("dashboards", "Dashboards", "Every panel queries a metric the system exports", self._dashboard_check),
        Check(
            "runbooks",
            "Runbooks for the top three failures",
            "CPU knee, pool exhaustion and zone loss each have symptoms, mitigation and diagnosis",
            self._runbook_check,
        ),
        Check(
            "rollout",
            "Safe rollout and rollback",
            "A rolling restart and a rollback ran under load with no failed request and p99 inside the SLO",
            self._rollout_check,
        ),
        Check(
            "dependencies",
            "Dependency failure behavior",
            "With the ledger frozen, requests fail fast with a 503 and the instance stays healthy",
            self._dependency_check,
        ),
        Check(
            "restore",
            "Backup and restore",
            "A backup was restored into a scratch database and matches the source row for row",
            self._restore_check,
        ),
        Check(
            "ownership",
            "On-call ownership",
            "An owner, a primary and secondary on-call role, an escalation path and the runbooks are written down",
            self._ownership_check,
        ),
        Check(
            "capacity",
            "Capacity",
            "The knee, the soak, the lost-zone run and the resized pool all held the SLO",
            self._capacity_check,
        ),
    ]
```

`checks` lists the eight checks in the order a reviewer would walk them: SLOs and burn-rate alerts, dashboards, runbooks, safe rollout and rollback, dependency failure, backup and restore, on-call ownership and capacity.

```python
def review(self) -> tuple[list[ReviewItem], Decision]:
    """Every checklist item with its verdict, then the decision."""
    items = [self._item(check) for check in self.checks()]
    for risk in self._config.accepted_risks():
        items.append(
            ReviewItem(
                risk["area"],
                risk["check"],
                Verdict.ACCEPTED_RISK,
                risk["evidence"],
                risk["owner"],
                revisit_when=risk["revisit_when"],
            )
        )
    return items, decide(items)

def _item(self, check: Check) -> ReviewItem:
    passed, evidence = check.run()
    owner = self._config.owner(check.item_id)
    if passed:
        return ReviewItem(check.area, check.asserts, Verdict.PASS, evidence, owner)
    return ReviewItem(
        check.area,
        check.asserts,
        Verdict.FAIL,
        evidence,
        owner,
        due=self._config.due(check.item_id),
        blocking=self._config.blocking(check.item_id),
    )
```

`review` runs every check, then appends one ACCEPTED RISK item per risk in the checklist file and asks the domain's `decide` for the decision. `_item` turns a result into a `ReviewItem`: a pass carries its evidence and owner; a failure also carries the due date and whether it blocks launch.

```python
def _slo_check(self) -> tuple[bool, str]:
    slos, alerts = self._slos.slos(), self._slos.alerts()
    problems = []
    if not slos:
        problems.append("no SLO defined")
    if {alert.severity for alert in alerts} != {"page", "ticket"}:
        problems.append("the alert table needs both a page and a ticket")
    if not self._slos.rules_in_sync():
        problems.append("slo/generated is out of date with slo/slos.yaml")
    problems += [
        f"{test} is missing"
        for test in ("slo/tests/burn_rate.test.yml", "slo/tests/fleet.test.yml")
        if not self._files.exists(test)
    ]
    if problems:
        return False, "; ".join(problems)
    return (
        True,
        f"{len(slos)} SLOs with {len(alerts)} burn alerts each, generated rules in sync, promtool replays present",
    )
```

`_slo_check` collects problems instead of stopping at the first: no SLO defined, an alert table without both page and ticket, generated rules out of date, a missing promtool replay file. It passes only with none, and the evidence sentence counts the SLOs and alerts.

```python
def _dashboard_check(self) -> tuple[bool, str]:
    paths = self._files.glob("dashboards/*.json")
    if not paths:
        return False, "no dashboard under dashboards/"
    unknown: set[str] = set()
    queries = 0
    for path in paths:
        for expression in _expressions(json.loads(self._files.read(path))):
            queries += 1
            unknown |= {
                name
                for name in _metric_names(expression)
                if name not in EXPORTED_METRICS and not name.startswith(RECORDED_RULE_PREFIX)
            }
    if unknown:
        return False, f"panels query metrics nothing exports: {sorted(unknown)}"
    return (
        True,
        f"{len(paths)} dashboard, {queries} queries, every metric is one the edge, an "
        "instance or the ledger exports",
    )
```

`_dashboard_check` loads every `dashboards/*.json`, pulls out every PromQL expression and every metric name in them, and fails on any name that is neither in `EXPORTED_METRICS` nor a recording rule (`slo:` prefix): a panel that queries a metric nothing exports shows an empty graph during an incident.

```python
def _runbook_check(self) -> tuple[bool, str]:
    problems = []
    for failure, path in REQUIRED_RUNBOOKS.items():
        if not self._files.exists(path):
            problems.append(f"{failure}: {path} is missing")
            continue
        text = self._files.read(path).lower()
        missing = [heading for heading in RUNBOOK_HEADINGS if heading.lower() not in text]
        if missing:
            problems.append(f"{failure}: no {', '.join(missing)} section")
    knee = self._evidence.load("knee")
    knee_runbook = "docs/runbooks/cpu-knee.md"
    if knee is not None and self._files.exists(knee_runbook):
        measured = (_knee_rps(knee), str(knee.get("safe_rps")))
        if not all(number in self._files.read(knee_runbook) for number in measured):
            problems.append(
                f"cpu-knee.md does not quote the measured knee ({measured[0]}) and safe load ({measured[1]})"
            )
    if problems:
        return False, "; ".join(problems)
    return (
        True,
        "three runbooks with symptoms, mitigation and diagnosis; "
        "the CPU one quotes the measured knee and safe load",
    )
```

`_runbook_check` fails for a missing runbook or one without Symptoms, Mitigate and Diagnose sections, and compares the CPU runbook with the measured knee and safe load: a runbook quoting numbers that do not match the measurement fails.

```python
def _rollout_check(self) -> tuple[bool, str]:
    data = self._evidence.load("rollout")
    if data is None:
        return False, "no rollout evidence: run `make rollout-test`"
    passed = data.get("within_slo") is True and data.get("rollback_ok") is True
    return passed, (
        f"{data.get('restarted')} instance restarts (roll to v2, roll back to v1) under {data.get('rps')} rps: "
        f"{data.get('errors')} failed requests, p99 {data.get('p99_ms')} ms"
    )

def _dependency_check(self) -> tuple[bool, str]:
    data = self._evidence.load("dependency")
    if data is None:
        return False, "no dependency evidence: run `make dependency-test`"
    passed = data.get("fail_fast") is True and data.get("instance_healthy") is True
    return passed, (
        f"ledger frozen: slowest 503 after {data.get('slowest_503_ms')} ms (bound {data.get('bound_ms')} ms), "
        f"instance healthy: {data.get('instance_healthy')}"
    )

def _restore_check(self) -> tuple[bool, str]:
    data = self._evidence.load("restore")
    if data is None:
        return False, "restore not yet tested: run `make restore-test`"
    within_target = float(str(data.get("restore_seconds"))) <= float(str(data.get("rto_target_seconds")))
    return data.get("matched") is True and within_target, (
        f"{data.get('rows')} rows restored in {data.get('restore_seconds')} s "
        f"(target {data.get('rto_target_seconds')} s), "
        f"row count and checksum match the source: {data.get('matched')}"
    )
```

`_rollout_check`, `_dependency_check` and `_restore_check` read the evidence documents of the rollout, dependency and restore tests. Each fails with a "run `make ...`" message when the evidence is absent. The rollout needs `within_slo` and `rollback_ok`; the dependency test needs `fail_fast` and `instance_healthy`; the restore needs a match **and** a restore time within the target.

```python
def _ownership_check(self) -> tuple[bool, str]:
    path = "docs/ownership.md"
    if not self._files.exists(path):
        return False, f"{path} is missing"
    text = self._files.read(path).lower()
    missing = [word for word in ("owner", "primary", "secondary", "escalation", "runbooks") if word not in text]
    if missing:
        return False, f"{path} does not say: {', '.join(missing)}"
    return True, "owner, primary and secondary on-call roles, escalation path and runbook index are written down"
```

`_ownership_check` reads `docs/ownership.md` and fails if it does not mention owner, primary, secondary, escalation and runbooks.

```python
def _capacity_check(self) -> tuple[bool, str]:
    names = ("knee", "soak", "fleet-zone-loss", "fleet-pool-naive", "fleet-pool-resized")
    loaded = {name: self._evidence.load(name) for name in names}
    missing = [name for name, document in loaded.items() if document is None]
    if missing:
        return False, f"missing capacity evidence ({', '.join(missing)}): run `make run`"
    knee, soak, zone, naive, resized = (loaded[name] or {} for name in names)
    problems = []
    if knee.get("knee") is None:
        problems.append("the load test never found a knee")
    if soak.get("within_slo") is not True:
        problems.append("the soak at the safe load left the SLO")
    if zone.get("within_slo") is not True:
        problems.append("the fleet minus a zone left the SLO at projected peak")
    if resized.get("within_slo") is not True:
        problems.append("the resized pool still left the SLO")
    if naive.get("within_slo") is True:
        problems.append("the naive pool did not fail, so the next bottleneck was not found")
    if problems:
        return False, "; ".join(problems)
    return True, (
        f"knee {_knee_rps(knee)} rps, safe {knee.get('safe_rps')} rps held {soak.get('seconds')} s "
        f"(p99 {soak.get('p99_ms')} ms); "
        f"lost-zone fleet at {zone.get('target_rps')} rps p99 {zone.get('p99_ms')} ms; "
        f"naive pool of {naive.get('pool_size')} failed "
        f"(p99 {naive.get('p99_ms')} ms, error rate {naive.get('error_rate')}), "
        f"pool of {resized.get('pool_size')} held (p99 {resized.get('p99_ms')} ms)"
    )
```

`_capacity_check` requires the evidence of the knee, soak, lost-zone and both pool runs, then collects problems: no knee, a soak that left the SLO, a lost-zone run that left the SLO, a resized pool that still failed, or a **naive pool that did not fail** (then the next bottleneck was not found). Passing quotes the numbers.

```python
def _knee_rps(knee: dict[str, Any]) -> str:
    detail = knee.get("knee")
    return "?" if not isinstance(detail, dict) else f"{detail.get('knee_rps'):g}"
```

`_knee_rps` formats the knee's rate or `?` when there is none.

```python
def _expressions(node: Any) -> list[str]:
    """Every PromQL expression in a dashboard JSON document."""
    found: list[str] = []
    if isinstance(node, dict):
        if isinstance(node.get("expr"), str):
            found.append(node["expr"])
        for value in node.values():
            found.extend(_expressions(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_expressions(value))
    return found


def _metric_names(expression: str) -> set[str]:
    """Identifiers in a PromQL expression, with selectors, ranges, strings and grouping clauses taken out."""
    bare = re.sub(r'"[^"]*"', "", re.sub(r"\{[^}]*\}", "", re.sub(r"\[[^\]]*\]", "", expression)))
    bare = re.sub(r"\b(?:by|without|on|ignoring)\s*\([^)]*\)", "", bare)
    names = set(re.findall(r"[a-zA-Z_:][a-zA-Z0-9_:]*", bare))
    return {name for name in names if name not in _PROMQL_WORDS}
```

`_expressions` walks a dashboard's JSON (dictionaries and lists, recursively) and collects every `expr` string. `_metric_names` strips label selectors, ranges, strings and grouping clauses from an expression with regular expressions, finds every identifier and drops the PromQL words, leaving the metric names.

### `application/launch_decision.py`: the one-page decision

```python
"""Render the review as the one-page launch decision."""

from __future__ import annotations

from capacity.domain.readiness import Decision, ReviewItem, Verdict


def console(items: list[ReviewItem], decision: Decision) -> str:
    """The review as plain lines, one per item, then the decision and its conditions."""
    lines = ["Readiness review:"]
    for item in items:
        mark = {Verdict.PASS: "PASS", Verdict.FAIL: "FAIL", Verdict.ACCEPTED_RISK: "ACCEPTED RISK"}[item.verdict]
        lines.append(f"  {item.area} {'.' * max(3, 48 - len(item.area))} {mark}")
    lines += ["", f"Decision: {decision.value}"]
    lines += [f"  - {line}" for line in conditions(items)]
    return "\n".join(lines)


def conditions(items: list[ReviewItem]) -> list[str]:
    """One line per failure, owner and date on the same line as the blocker itself."""
    return [
        f"{item.area}: {item.evidence}. Owner: {item.owner}, by {item.due}{' (blocks launch)' if item.blocking else ''}"
        for item in items
        if item.verdict is Verdict.FAIL
    ]
```

`console` prints a line per item (area, dots, verdict), then `Decision:` and the conditions. `conditions` writes one line per failure with the evidence, the owner, the due date and "(blocks launch)" when it blocks.

```python
def markdown(items: list[ReviewItem], decision: Decision) -> str:
    """`docs/launch-decision.md`: the decision first, then the table, the conditions and the accepted risks."""
    lines = [
        "# Launch decision",
        "",
        "Generated by `make review` from the repository and the measured evidence in `docs/evidence/`. "
        "Do not edit by hand; `make test` fails when this file differs from what the review produces.",
        "",
        f"## Decision: {decision.value}",
        "",
    ]
    failures = conditions(items)
    if failures:
        lines += ["Conditions (each has an owner and a date):", ""]
        lines += [f"- {line}" for line in failures]
    else:
        lines.append("No condition: every item passed or is an accepted risk with an owner.")
    lines += ["", "## The checklist", "", "| Area | Verdict | Owner | Evidence |", "| --- | --- | --- | --- |"]
    for item in items:
        owner = item.owner or "-"
        lines.append(f"| {item.area} | {item.verdict.value} | {owner} | {item.evidence} |")
    risks = [item for item in items if item.verdict is Verdict.ACCEPTED_RISK]
    if risks:
        lines += ["", "## Accepted risks", ""]
        lines += [f"- **{item.area}** (owner: {item.owner}). Reopen when: {item.revisit_when}" for item in risks]
    return "\n".join(lines) + "\n"
```

`markdown` writes `docs/launch-decision.md`: the decision first, then the conditions (or the sentence that there are none), the checklist table and the accepted risks with the condition that reopens each. `make test` fails when the committed file differs from what the review produces.

## Try it yourself

1. `.venv/bin/python -c "from capacity.application.experiments import naive_pool_size, sweep_sizes; n=naive_pool_size(2000,10); print(n, sweep_sizes(n))"` prints `20 [20, 24, 30, 40]`.
2. `.venv/bin/python -c "from capacity.application.rules import duration; print(duration(3600), duration(90), duration(300))"` prints `1h 90s 5m`.
3. `.venv/bin/python -c "from capacity.application.experiments import choose_pool_size; print(choose_pool_size([{'pool_size':24,'within_slo':False},{'pool_size':30,'within_slo':True}]))"` prints `40`.
4. `.venv/bin/python -m pytest tests/unit/test_latency_and_loadtest.py tests/unit/test_readiness.py -q` runs this layer's load-test and review tests.

## Mistakes people make

- A review that passes because a file exists. These checks read the content and the measurement.
- Naming the bottleneck from the shape of the curve instead of from CPU evidence.
- Trusting a number from a generator that fell behind. `GENERATOR_LAG_LIMIT_MS` exists so the tool's own lag is checked.
- Averaging a long run. The per-window p99 keeps a slow drift visible.

## Check yourself

1. Why is `OpenLoopRunner` a port?
   *Answer: so a test can pass a scripted runner and exercise the knee search without Docker or a clock.*
2. Why does `knee_test` run a broken-looking step twice?
   *Answer: a real knee shows both times; a stall on a shared machine does not.*
3. How is the adopted pool size chosen?
   *Answer: the smallest size that held the SLO, times 1.25, rounded up to a multiple of five.*
4. When does a multiwindow burn alert fire?
   *Answer: when the long and the short window both burn faster than the factor and the long window holds enough bad events.*
5. What happens to a review check whose evidence is missing?
   *Answer: it fails, with a message that says which make target to run.*

## Further reading

- `typing.Protocol`: <https://docs.python.org/3.12/library/typing.html#typing.Protocol>
- `itertools.accumulate`: <https://docs.python.org/3.12/library/itertools.html#itertools.accumulate>
- Prometheus recording rules: <https://prometheus.io/docs/prometheus/latest/configuration/recording_rules/>
- Google SRE workbook, alerting on SLOs: <https://sre.google/workbook/alerting-on-slos/>
