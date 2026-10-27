# Python: the domain layer, file by file

## What it is

The domain layer is `src/capacity/domain/`, the innermost folder. It holds the rules of capacity planning in plain Python and imports nothing but the standard library: the CPU work a request does, how latency is summarized, how a knee is found in a stepped load test, how a safe load and a fleet are computed, and how a readiness review reaches a decision. This note walks through every file. It continues the Python note, which explains the language features (dataclasses, enums, type hints); read that one first.

## Why this repository uses it

The lesson's decisions rest on arithmetic: which step of the load test is the knee, what load is safe, how many instances survive losing a zone, when a review says LAUNCH. Keeping that arithmetic in a layer with no side effects lets a test check it in microseconds with the lesson's own numbers (a knee at 550, a safe load of 400, five instances for the peak and eight in all).

## Where it appears

| File | What it holds |
| --- | --- |
| `capacity/__init__.py`, `domain/__init__.py` | Package markers. |
| `domain/errors.py` | Named errors. |
| `domain/cpu.py` | The CPU work a request does. |
| `domain/latency.py` | Percentiles and per-step summaries of latency samples. |
| `domain/knee.py` | Step results, finding the knee, choosing the safe load. |
| `domain/plan.py` | The capacity plan formula. |
| `domain/readiness.py` | Verdicts, review items and the decision rule. |
| `domain/telemetry.py` | Metric names, histogram buckets and the set of exported metrics. |
| `domain/requests.py`, `domain/slo.py` | One request, the SLO formulas (shared with the first project). |

## How it works underneath

Classes are **frozen dataclasses** (created once, never changed) whose `__post_init__` refuses invalid values. Functions take plain values and return plain values. Nothing here reads a file, a clock or the network, so the layer is deterministic: the same steps always give the same knee.

## Walkthrough of this repository's use

### `capacity/__init__.py` and `domain/__init__.py`

```python
"""SLOs, error budgets and burn-rate alerts for a URL shortener."""
```

```python
"""Pure rules: journeys, SLO arithmetic, the budget policy and fault plans. Depends on the standard library only."""
```

A folder with an `__init__.py` is a Python **package**. The docstrings describe the project and the layer.

### `domain/errors.py`

```python
"""Named errors of the domain."""

from __future__ import annotations


class DomainError(Exception):
    """Base class for every error this project raises on purpose."""


class InvalidSloError(DomainError):
    """An SLO definition is incomplete or inconsistent."""


class NoKneeError(DomainError):
    """The load test never reached a step where latency rose sharply, so there is no knee to plan from."""


class InvalidPlanError(DomainError):
    """A capacity plan input is out of range or the plan cannot meet its own requirement."""


class InvalidReviewError(DomainError):
    """A readiness review item is missing its owner, its evidence or its date."""
```

`DomainError` is the base of every error raised on purpose. `InvalidSloError`: an SLO definition is incomplete. `NoKneeError`: the load test never reached a step where latency rose sharply, so there is no knee to plan from. `InvalidPlanError`: a plan input is out of range. `InvalidReviewError`: a review item is missing its owner, evidence or date.

### `domain/cpu.py`: real work

```python
"""The service's real work: CPU burned per request, not a sleep standing in for it."""

from __future__ import annotations

import hashlib

DEFAULT_ROUNDS = 3000


def cpu_work(rounds: int = DEFAULT_ROUNDS) -> str:
    """Chain SHA-256 `rounds` times and return the hex digest.

    The time this takes is real CPU time on the machine it runs on, so the knee a load test finds is an actual
    resource limit. Cost scales linearly with `rounds` (about a third of a microsecond each on a current laptop).
    """
    digest = hashlib.sha256(b"x").digest()
    for _ in range(rounds):
        digest = hashlib.sha256(digest).digest()
    return digest.hex()
```

`cpu_work(rounds)` chains SHA-256 `rounds` times (3,000 by default) starting from `sha256(b"x")` and returns the final digest as hex. The time it takes is real CPU time, so a load test finds an actual resource limit instead of a sleep pretending to be work. The docstring says the cost grows linearly with `rounds`.

### `domain/latency.py`: percentiles

```python
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
```

`percentile(values, fraction)` is the **nearest-rank** method: sort the values and take the one at position `ceil(fraction * count)` (at least 1, at most the last). `Sample` is one request: when it was scheduled (`scheduled_s`), how long until its full response (`latency_s`, measured from the **scheduled** arrival, not from when it was actually sent) and whether it succeeded. `summarize` turns a step's samples into a `StepResult`: counts, errors, achieved rate, p50 and p99 and max in milliseconds (rounded to two decimals) and the optional CPU utilization. `window_p99s` groups samples into windows of `window_s` seconds of scheduled time and returns each window's p99, so slow drift during a long run cannot hide inside one average.

### `domain/knee.py`: finding the knee

```python
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
    attempts: int = 1

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

```

`BASELINE_STEPS = 3` is how many early steps define "flat". `RISE_FACTOR = 3.0` is how much higher than the baseline p99 counts as a jump. `STEP_ROUNDING = 25` is the multiple the safe load rounds down to. `StepResult` is one step of the load test (target and achieved rate, request and error counts, p50, p99, the CPU share, the maximum and how many attempts it took). `Knee` holds the load where latency rose sharply, the last good load before it, the baseline and knee p99 and the CPU share at the knee; `saturated_resource` names **cpu** only when the process used at least 90% of its limit, otherwise it says no resource was measured at its limit: the resource is named from evidence, not guessed from the shape of the curve.

```python
def looks_broken(step: StepResult, baseline_p99_ms: float, rise_factor: float = RISE_FACTOR) -> bool:
    """True when the step's p99 is more than `rise_factor` times the flat baseline, or it fails over 1% of requests."""
    return step.p99_ms > rise_factor * baseline_p99_ms or step.error_rate > 0.01


def baseline_p99(steps: list[StepResult]) -> float:
    """The median p99 of the first three steps: what \"flat\" looks like on this machine."""
    return float(median(step.p99_ms for step in steps[:BASELINE_STEPS]))


def find_knee(steps: list[StepResult], rise_factor: float = RISE_FACTOR) -> Knee:
    """The first step whose p99 exceeds `rise_factor` times the flat baseline, or that fails more than 1% of requests,
    and stays that bad on the next step (a spike that goes away again is noise, not a knee).

    The baseline is the median p99 of the first three steps. Steps must be in rising order of load. The last step
    needs no successor: the test stops a couple of steps past the knee, so the final step is a candidate by itself.
    """
    if len(steps) <= BASELINE_STEPS:
        raise NoKneeError(f"need more than {BASELINE_STEPS} steps to see a flat part and a knee")
    ordered = sorted(steps, key=lambda step: step.target_rps)
    baseline = baseline_p99(ordered)

    def broken(step: StepResult) -> bool:
        return looks_broken(step, baseline, rise_factor)

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
```

`looks_broken(step, baseline, rise_factor)` is true when the step's p99 is more than three times the baseline or it failed more than 1% of requests. `baseline_p99` is the median p99 of the first three steps. `find_knee` sorts the steps by target load, computes the baseline, and returns the first step that looks broken **and stays broken** on the next step (a spike that goes away again is noise, not a knee; the last step needs no successor). It raises `NoKneeError` when there are three steps or fewer or no sharp rise ever persists. `safe_load(knee, margin, step)` is the knee times (1 − margin), rounded **down** to a multiple of 25, never below 25; the margin (25% by default) is a judgment call: it covers measurement noise, a different traffic mix and a busy neighbor.

### `domain/plan.py`: the fleet

```python
"""The capacity plan: how many instances serve the projected peak, and survive losing a zone."""

from __future__ import annotations

import math
from dataclasses import dataclass

from capacity.domain.errors import InvalidPlanError


@dataclass(frozen=True, slots=True)
class CapacityPlan:
    """The inputs, the formula's intermediate results and the answer, so a reviewer can check every step."""

    current_peak_rps: float
    growth_factor: float
    safe_rps_per_instance: float
    zones: int
    future_peak_rps: float
    instances_for_peak: int
    total_instances: int
    distribution: tuple[int, ...]
    surviving_after_zone_loss: int

    @property
    def surviving_capacity_rps(self) -> float:
        """What the instances left after losing the largest zone can carry at their safe load."""
        return self.surviving_after_zone_loss * self.safe_rps_per_instance

    def formula(self) -> str:
        """The calculation, written out."""
        return (
            f"future_peak = {self.current_peak_rps:g} x {self.growth_factor:g} = {self.future_peak_rps:g} rps; "
            f"instances_for_peak = ceil({self.future_peak_rps:g} / {self.safe_rps_per_instance:g}) "
            f"= {self.instances_for_peak}; "
            f"total = smallest fleet over {self.zones} zones whose largest zone can be lost and still leave "
            f"{self.instances_for_peak} = {self.total_instances} ({'/'.join(map(str, self.distribution))}); "
            f"survivors after losing a zone of {max(self.distribution)} = {self.surviving_after_zone_loss}"
        )


def spread(total: int, zones: int) -> tuple[int, ...]:
    """Spread instances over zones as evenly as possible, larger zones first (8 over 3 zones is 3, 3, 2)."""
    base, extra = divmod(total, zones)
    return tuple(base + (1 if index < extra else 0) for index in range(zones))


def plan_capacity(
    current_peak_rps: float, growth_factor: float, safe_rps_per_instance: float, zones: int = 3
) -> CapacityPlan:
    """Size the fleet from the projected peak, then add instances until the surviving fleet still carries the full peak.

    Headroom is computed from the projected peak (today's peak times growth), never from today's, because the
    fleet has to survive a zone loss and the growth at the same time. The check uses the actual distribution:
    losing the largest zone must leave at least `instances_for_peak` instances.
    """
    if min(current_peak_rps, growth_factor, safe_rps_per_instance) <= 0 or zones < 2:
        raise InvalidPlanError("peak, growth and safe load must be positive and there must be at least two zones")
    future_peak = current_peak_rps * growth_factor
    needed = math.ceil(future_peak / safe_rps_per_instance)
    total = needed
    while True:
        distribution = spread(total, zones)
        survivors = total - max(distribution)
        if survivors >= needed:
            return CapacityPlan(
                current_peak_rps,
                growth_factor,
                safe_rps_per_instance,
                zones,
                future_peak,
                needed,
                total,
                distribution,
                survivors,
            )
        total += 1
```

`CapacityPlan` records the inputs, the intermediate results and the answer so a reviewer can check every step; `formula()` writes the calculation out as one line of text (it is saved as evidence and quoted in the capacity plan). `spread(total, zones)` spreads instances over zones as evenly as possible, larger zones first (8 over 3 zones is 3, 3, 2), using `divmod`. `plan_capacity` refuses non-positive inputs or fewer than two zones, computes the projected peak (today's peak × growth), the instances needed (`ceil(peak / safe)`), and then adds instances one at a time until the instances left after losing the largest zone still carry the peak. Headroom comes from the projected peak, never today's, because the fleet must survive a zone loss and the growth together.

### `domain/readiness.py`: the review

```python
"""The readiness review: every checklist item gets a verdict, an owner and evidence; the review ends in one decision."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from capacity.domain.errors import InvalidReviewError


class Verdict(StrEnum):
    """The only three answers. "Probably fine" is not one of them."""

    PASS = "PASS"
    FAIL = "FAIL"
    ACCEPTED_RISK = "ACCEPTED RISK"


class Decision(StrEnum):
    """The only three outcomes of a review."""

    LAUNCH = "LAUNCH"
    LAUNCH_WITH_CONDITIONS = "LAUNCH WITH CONDITIONS"
    DONT_LAUNCH = "DON'T LAUNCH YET"


@dataclass(frozen=True, slots=True)
class ReviewItem:
    """One line of the checklist.

    A FAIL names the date by which it is fixed. `blocking` marks a failure that must be fixed before launch rather
    than after. An ACCEPTED RISK names the condition that reopens it. Both need an owner, which is a role.
    """

    area: str
    check: str
    verdict: Verdict
    evidence: str
    owner: str
    due: date | None = None
    revisit_when: str = ""
    blocking: bool = False

    def __post_init__(self) -> None:
        """A verdict without its evidence, owner, date or revisit condition is a complaint, not a finding."""
        if not self.evidence.strip():
            raise InvalidReviewError(f"{self.area}: a verdict needs evidence")
        if self.verdict is not Verdict.PASS and not self.owner.strip():
            raise InvalidReviewError(f"{self.area}: a {self.verdict.value} needs an owner")
        if self.verdict is Verdict.FAIL and self.due is None:
            raise InvalidReviewError(f"{self.area}: a FAIL needs a date")
        if self.verdict is Verdict.ACCEPTED_RISK and not self.revisit_when.strip():
            raise InvalidReviewError(f"{self.area}: an ACCEPTED RISK needs the condition that reopens it")


def decide(items: list[ReviewItem]) -> Decision:
    """DON'T LAUNCH if a failure blocks, LAUNCH WITH CONDITIONS if failures are due by a date, else LAUNCH."""
    failures = [item for item in items if item.verdict is Verdict.FAIL]
    if any(item.blocking for item in failures):
        return Decision.DONT_LAUNCH
    return Decision.LAUNCH_WITH_CONDITIONS if failures else Decision.LAUNCH
```

`Verdict` has the only three answers (PASS, FAIL, ACCEPTED RISK) and `Decision` the only three outcomes (LAUNCH, LAUNCH WITH CONDITIONS, DON'T LAUNCH YET). `ReviewItem.__post_init__` refuses a verdict without evidence, a non-PASS without an owner, a FAIL without a date and an ACCEPTED RISK without the condition that reopens it: "a verdict without its evidence, owner, date or revisit condition is a complaint, not a finding". `decide(items)` is DON'T LAUNCH YET if any failure is blocking, LAUNCH WITH CONDITIONS if there are other failures, otherwise LAUNCH.

### `domain/telemetry.py`

```python
"""Metric names the generated rules read: the Envoy edge's, and the instances' own."""

from __future__ import annotations

# Envoy's HTTP connection manager is the edge: every request passes through it, so its counts and latency
# histogram are what users experience. Its latency buckets are in milliseconds.
EDGE_PREFIX = 'envoy_http_conn_manager_prefix="ingress_http"'
EDGE_RESPONSES = "envoy_http_downstream_rq_xx"
EDGE_DURATION_BUCKET = "envoy_http_downstream_rq_time_bucket"
EDGE_DURATION_COUNT = "envoy_http_downstream_rq_time_count"
EDGE_LATENCY_BUCKETS_MS: tuple[float, ...] = (
    0.5,
    1,
    5,
    10,
    25,
    50,
    100,
    250,
    500,
    1000,
    2500,
    5000,
    10000,
    30000,
    60000,
)
EDGE_HEALTHY_MEMBERS = 'envoy_cluster_membership_healthy{envoy_cluster_name="app"}'

# The instances' own metrics, scraped per instance.
APP_REQUESTS_TOTAL = "app_requests_total"
APP_DURATION_SECONDS = "app_request_duration_seconds"
APP_INFLIGHT = "app_inflight_requests"
APP_LATENCY_BUCKETS_SECONDS: tuple[float, ...] = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5)

# Every metric a dashboard or alert may query. The end-to-end test checks each name against the running system's
# own endpoints, so this set cannot drift from what is really exported.
EXPORTED_METRICS: frozenset[str] = frozenset(
    {
        "envoy_http_downstream_rq_xx",
        "envoy_http_downstream_rq_total",
        "envoy_http_downstream_rq_time_bucket",
        "envoy_http_downstream_rq_time_count",
        "envoy_http_downstream_rq_time_sum",
        "envoy_cluster_membership_healthy",
        "envoy_cluster_membership_total",
        "app_requests_total",
        "app_request_duration_seconds_bucket",
        "app_request_duration_seconds_count",
        "app_request_duration_seconds_sum",
        "app_inflight_requests",
        "process_cpu_seconds_total",
        "ledger_pool_in_use",
        "ledger_pool_waiting",
        "ledger_pool_size",
        "ledger_requests_total",
        "ledger_pool_timeouts_total",
        "up",
    }
)
RECORDED_RULE_PREFIX = "slo:"
```

The names the generated rules read. Envoy's HTTP connection manager is the edge: its response counts (`envoy_http_downstream_rq_xx`), request-time histogram (`..._rq_time_bucket`, `..._count`, in **milliseconds**, with the bucket list `EDGE_LATENCY_BUCKETS_MS`) and the number of healthy cluster members are what users experience. `EDGE_PREFIX` selects the ingress listener's series. The instances' own metrics (`app_requests_total`, `app_request_duration_seconds` in seconds, `app_inflight_requests`) are scraped per instance. `EXPORTED_METRICS` is the set of every name a dashboard or alert may use; an end-to-end test checks each against the running system, and the readiness review checks the dashboard against it. `RECORDED_RULE_PREFIX = "slo:"` marks names of recording rules, which are allowed too.

### `domain/requests.py` and `domain/slo.py` (shared with the first project)

### `domain/requests.py`

```python
"""One request as the edge saw it: the unit every SLI is counted in."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RequestEvent:
    """A single line of the request log.

    `minute` counts minutes from the start of the measurement window (fractional), which is what
    the 28-day budget arithmetic indexes by.
    """

    journey: str
    status: int
    latency_ms: float
    minute: float
```

`RequestEvent` is one line of the request log: the journey, the HTTP status, the latency in milliseconds and `minute`, how many (fractional) minutes after the start of the window the request happened. Every SLI in the project is counted in these events.

### `domain/slo.py`: the formulas

```python
"""SLIs, SLOs, error budgets and burn rates.

Every SLI is a fraction of good events over valid events. The error budget is what the target allows to
be bad, and the burn rate says how fast an interval spends it relative to a budget that would last the
whole window exactly.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from capacity.domain.errors import InvalidSloError

WINDOW_DAYS = 28
MINUTES_PER_DAY = 1440
```

The docstring defines the vocabulary: an SLI is good events over valid events; the error budget is what the target allows to be bad; the burn rate says how fast an interval spends the budget compared with spending it evenly over the window. `WINDOW_DAYS = 28` and `MINUTES_PER_DAY = 1440` are the constants the arithmetic uses.

```python
class SliKind(StrEnum):
    """What an SLI counts as good."""

    AVAILABILITY = "availability"
    LATENCY = "latency"
```

`SliKind` has two members, `availability` and `latency`; the kind decides what "good" means.

```python
@dataclass(frozen=True, slots=True)
class Slo:
    """A target for one SLI of one journey over a rolling window.

    `valid` and `good` are the human-readable definitions that go into the SLO document; the arithmetic is
    fixed by `kind`: availability counts a request good when its status is below 500 (a redirect, or a
    correct 404 for an unknown code); latency looks only at successful requests and counts one good when
    it was served under `threshold_ms`.
    """

    name: str
    journey: str
    kind: SliKind
    target: float
    valid: str
    good: str
    why: str
    threshold_ms: float | None = None
    window_days: int = WINDOW_DAYS

    def __post_init__(self) -> None:
        """Validate the definition so a broken SLO file fails when loaded, not when evaluated."""
        if not 0.0 < self.target < 1.0:
            raise InvalidSloError(f"{self.name}: the target must be strictly between 0 and 1")
        if self.kind is SliKind.LATENCY and (self.threshold_ms is None or self.threshold_ms <= 0):
            raise InvalidSloError(f"{self.name}: a latency SLO needs a positive threshold_ms")
        if self.kind is SliKind.AVAILABILITY and self.threshold_ms is not None:
            raise InvalidSloError(f"{self.name}: an availability SLO has no threshold_ms")
        if not (self.valid.strip() and self.good.strip() and self.why.strip()):
            raise InvalidSloError(f"{self.name}: valid, good and why must each say something")
```

`Slo` holds a name, the journey, the kind, the target (for example 0.9995), human-readable definitions of what counts as valid and good and why, an optional latency threshold and the window length. `__post_init__` is the safety net for a hand-edited YAML file: the target must be strictly between 0 and 1; a latency SLO needs a positive threshold; an availability SLO must not have one; the three texts must not be empty. A broken SLO file therefore fails when it is loaded, not weeks later when an alert misbehaves.

```python
@dataclass(frozen=True, slots=True)
class BurnAlert:
    """A multiwindow burn-rate alert: it fires only when both windows burn faster than `factor`.

    `min_bad_events` is the low-traffic guard: the long window must also hold at least that many bad events.
    A journey with a couple of requests a minute would otherwise page on two unlucky failures in an hour.
    """

    name: str
    severity: str
    factor: float
    long_minutes: int
    short_minutes: int
    min_bad_events: int = 10

    def __post_init__(self) -> None:
        """The short window must be shorter, and severity is page or ticket."""
        if self.severity not in {"page", "ticket"}:
            raise InvalidSloError(f"{self.name}: severity is page or ticket")
        if not 0 < self.short_minutes < self.long_minutes or self.factor <= 0:
            raise InvalidSloError(f"{self.name}: need 0 < short window < long window and a positive factor")
        if self.min_bad_events < 1:
            raise InvalidSloError(f"{self.name}: min_bad_events must be at least 1")

    def budget_consumed_fraction(self, window_days: int = WINDOW_DAYS) -> float:
        """The share of the whole window's budget the long window spends at exactly this burn rate."""
        return self.factor * self.long_minutes / (window_days * MINUTES_PER_DAY)
```

`BurnAlert` is one row of the alert table: a name, a severity (`page` or `ticket`), the burn-rate `factor`, the long and short window lengths in minutes, and `min_bad_events`, the guard for quiet journeys. The checks require a known severity, a short window shorter than the long one with a positive factor, and at least one bad event. `budget_consumed_fraction` answers "how much of the whole 28-day budget does the long window spend at exactly this burn rate?": `factor * long_minutes / (window_days * MINUTES_PER_DAY)`. For the fast alert that is 14.4 × 60 ÷ 40,320 ≈ 2.1%.

### The rest of `domain/slo.py`: properties, defaults and formulas

```python
@property
def allowed_bad_fraction(self) -> float:
    """The share of valid events the target lets be bad: 1 minus the target."""
    return round(1.0 - self.target, 12)

@property
def window_minutes(self) -> int:
    """The window length in minutes."""
    return self.window_days * MINUTES_PER_DAY

@property
def budget_minutes(self) -> float:
    """The budget as minutes of full outage over the window."""
    return self.window_minutes * self.allowed_bad_fraction

def budget_requests(self, valid_events: int) -> float:
    """The budget as a count of bad requests, for the number of valid requests in the window."""
    return valid_events * self.allowed_bad_fraction

def judge(self, status: int, latency_ms: float) -> tuple[bool, bool]:
    """Return (valid, good) for one request."""
    if self.kind is SliKind.AVAILABILITY:
        return True, status < 500
    succeeded = status < 500
    assert self.threshold_ms is not None
    return succeeded, succeeded and latency_ms < self.threshold_ms

```

Four read-only properties derive numbers from the definition: `allowed_bad_fraction` is 1 minus the target (rounded to 12 places to remove floating-point noise), `window_minutes` is days times 1,440, `budget_minutes` is the window times the allowed fraction and `budget_requests(valid_events)` is the budget as a number of bad requests. `judge(status, latency_ms)` returns `(valid, good)`: for availability every request is valid and good when the status is below 500; for latency only successful requests are valid and good ones are faster than the threshold.

```python
# The starting point from the track's SLO alerting lesson, for a 28-day window.
DEFAULT_ALERTS: tuple[BurnAlert, ...] = (
    BurnAlert("fast_burn", "page", 14.4, 60, 5),
    BurnAlert("slow_burn", "page", 6.0, 360, 30),
    BurnAlert("budget_leak", "ticket", 1.0, 4320, 360),
)


def burn_rate(bad: int, valid: int, allowed_bad_fraction: float) -> float:
    """Observed bad fraction over the allowed bad fraction; 1.0 spends the budget exactly over the window."""
    if valid == 0:
        return 0.0
    return (bad / valid) / allowed_bad_fraction


def budget_remaining_pct(bad: int, valid: int, allowed_bad_fraction: float) -> float:
    """Percent of the window's error budget still unspent given the bad and valid counts so far."""
    return 100.0 * (1.0 - burn_rate(bad, valid, allowed_bad_fraction))
```

`DEFAULT_ALERTS` is the lesson's starting alert table for a 28-day window: fast burn (factor 14.4, long window 60 minutes, short 5, page), slow burn (6, 360, 30, page) and budget leak (1, 4320, 360, ticket). `burn_rate(bad, valid, allowed)` is the observed bad fraction over the allowed one (0 for no traffic). `budget_remaining_pct` is 100 times (1 minus the burn rate).

## Try it yourself

1. `.venv/bin/python -c "from capacity.domain.knee import StepResult, find_knee; s=lambda r,p: StepResult(r,r,r*10,0,2.0,p,0.5); print(find_knee([s(100,5),s(200,5),s(300,6),s(400,90),s(450,200)]))"` prints a `Knee` at 400 rps with last good 300.
2. `.venv/bin/python -c "from capacity.domain.plan import spread; print(spread(8,3), spread(11,3))"` prints `(3, 3, 2) (4, 4, 3)`.
3. `.venv/bin/python -c "from capacity.domain.readiness import *; from datetime import date; i=ReviewItem('a','c',Verdict.FAIL,'e','owner',date(2026,11,3),blocking=True); print(decide([i]))"` prints `DON'T LAUNCH YET`.
4. `.venv/bin/python -c "from capacity.domain.telemetry import EXPORTED_METRICS; print(len(EXPORTED_METRICS))"` prints the number of exported names (19).
5. `.venv/bin/python -m pytest tests/unit/test_knee_and_plan.py tests/unit/test_readiness.py -q` runs the tests of this layer.

## Mistakes people make

- Naming a bottleneck from the shape of a curve. The knee names a resource only with CPU evidence.
- Planning headroom from today's peak instead of the projected one.
- Treating one noisy step as a knee. A knee must persist on the next step.
- A verdict without evidence. The review refuses it.

## Check yourself

1. What makes a step "look broken"?
   *Answer: p99 more than three times the baseline, or more than 1% of requests failing.*
2. Why must the knee persist on the next step?
   *Answer: a spike that goes away again is noise, not a knee.*
3. How is the safe load computed?
   *Answer: the knee times (1 - margin), rounded down to a multiple of 25.*
4. How does `plan_capacity` choose the total?
   *Answer: start at `ceil(peak / safe)` and add instances until losing the largest zone still leaves enough.*
5. When does the decision say DON'T LAUNCH YET?
   *Answer: when any failing item is marked blocking.*

## Further reading

- `statistics.median`: <https://docs.python.org/3.12/library/statistics.html#statistics.median>
- `math.ceil` and `math.floor`: <https://docs.python.org/3.12/library/math.html>
- Google SRE book, load balancing and capacity planning: <https://sre.google/sre-book/software-engineering-in-sre/>
