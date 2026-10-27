# Python 3.12

## What it is

Python is a programming language. You write instructions in text files ending in `.py`, and a program called the **interpreter** reads and runs them. This repository is written in Python 3.12 (the Dockerfile uses 3.12.8, and `requires-python = ">=3.12"`).

A few words you will meet:

- A **module** is one `.py` file. A **package** is a folder of modules with an `__init__.py` file.
- A **function** is a named block of code (`def name(...)`). A **class** is a blueprint for objects, bundling data and functions.
- A **type hint** is a note such as `x: int` saying what kind of value something is. Python ignores hints when running; the tool `mypy` checks them (see the mypy note).
- The **standard library** is the large set of modules that come with Python (`hashlib`, `math`, `statistics`, `asyncio`, `json`, `subprocess`, ...).

## Why this repository uses it

The lesson is about capacity: measure one instance, compute a fleet, find the next limit, review readiness. The measuring code (percentiles, the knee search, the plan formula) must be short enough to read and check by hand. Python gives all of that, plus a service whose CPU cost per request is real, in one language.

## Where it appears

| Folder | What the Python there does |
| --- | --- |
| `src/capacity/domain/` | Pure rules: the CPU work, percentiles, the knee search and safe load, the capacity plan formula, the readiness verdicts and decision rule, SLO arithmetic. Standard library only. |
| `src/capacity/application/` | Use cases: the stepped knee test and timed runs, the readiness checks, the capacity-plan and launch-decision renderers, rule generation, and the ports they need. |
| `src/capacity/infrastructure/` | Adapters: the open-loop HTTP generator, `docker compose`, evidence and project files, metrics, settings, logs. |
| `src/capacity/interfaces/` | The programs: the instance, the ledger, the load test, the experiments, the plan and review commands, the rule generator. |
| `tests/` | Unit, integration, end-to-end and measurement tests. |

The four-layer split is explained in `ARCHITECTURE.md`; the import-linter note shows how it is enforced.

## How it works underneath

1. You run `python file.py` or a command like `capacity-instance`. The interpreter reads the file, turns it into **bytecode** (a compact internal form) and runs that.
2. `import x` finds module `x`, runs it once and keeps the result in memory.
3. Everything is an **object**. Variables are names that point at objects.
4. Python has **no braces**: indentation (four spaces here) marks blocks.
5. Errors are **exceptions**: `raise` throws one, `try`/`except` catches it.

## Walkthrough of this repository's use

### Real CPU work: `hashlib`

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

`hashlib.sha256(b"x").digest()` computes a SHA-256 hash (a fixed-size fingerprint of some bytes) and returns it as `bytes`. `b"x"` is a **bytes literal**: a sequence of byte values, not text. The loop feeds each digest back into the hash `rounds` times; `for _ in range(rounds)` repeats without needing the counter (`_` is a throwaway name). Nothing here sleeps: the time it takes is real CPU time on the machine, so a load test on this service finds a real resource limit. `DEFAULT_ROUNDS = 3000` is a constant (capital letters by convention); `.hex()` turns the final bytes into text.

### Dataclasses, properties and `None`

```python
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

`@dataclass(frozen=True, slots=True)` is a **decorator**, a line starting with `@` that modifies the class below it. A dataclass writes the boring parts (`__init__`, a readable `repr`, equality) from the list of fields. `frozen=True` makes instances **immutable**. `slots=True` stores fields compactly. `cpu_utilization: float | None = None` means "a float or `None` (no value)", with `None` as the default. `attempts: int = 1` is a field with a default.

`@property` lets code write `step.error_rate` (no brackets) while a method computes it. `self.errors / self.requests if self.requests else 0.0` is a **conditional expression**: the value before `if` when the condition holds, else the value after `else` (here it avoids dividing by zero).

### Finding the knee: sorting, nested functions, loops

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

- `median(step.p99_ms for step in steps[:BASELINE_STEPS])` takes the median of a **generator expression** over a **slice** (`steps[:3]` is the first three items).
- `sorted(steps, key=lambda step: step.target_rps)` sorts by one field; `lambda step: step.target_rps` is a tiny unnamed function.
- `broken` is a **nested function** that uses `baseline` from the surrounding function (a **closure**).
- `for index in range(BASELINE_STEPS, len(ordered))` loops over positions. `persists = index + 1 == len(ordered) or broken(ordered[index + 1])` combines a comparison and a call with `or`, which stops at the first true part.
- `raise NoKneeError(...)` signals "no knee found".
- `safe_load` uses `math.floor(...)` and integer arithmetic: `max(step, math.floor(knee.knee_rps * (1.0 - margin) / step) * step)` rounds down to a multiple of 25, at least 25.

### Percentiles

```python
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
```

`percentile` is the **nearest-rank** method: sort the values, take position `ceil(fraction * count)`. `Sequence[float]` is a type hint for any ordered collection of floats. `Sample` records one request: when it was scheduled, how long until the full response, and whether it was OK.

### Another dataclass `replace`, and `await` in a loop

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

`dataclasses.replace(best, attempts=2)` returns a copy of a frozen dataclass with one field changed (it cannot be modified in place). `async def`/`await` are explained in the asyncio note. `sum(1 for step in results if step.target_rps > found.knee_rps) >= 2` counts matching items with a generator expression. `try`/`except NoKneeError` turns "no knee" into a report with `None` values instead of a crash.

### The plan formula

```python
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

`divmod(total, zones)` returns the quotient and the remainder at once: 8 over 3 zones is `(2, 2)`, so two zones get 3 and one gets 2. `tuple(base + (1 if index < extra else 0) for index in range(zones))` builds the distribution. `plan_capacity` is a `while True` loop: start from the minimum number of instances for the projected peak (`math.ceil`) and add one until the instances left after losing the largest zone still carry the peak. `return` inside the loop exits when the condition is met.

### Enums and a rule that decides

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

`Verdict` and `Decision` are `StrEnum`s: fixed sets of names that are also strings. `ReviewItem.__post_init__` runs after the automatic `__init__` and refuses a verdict that has no evidence, a FAIL with no date or an ACCEPTED RISK with no revisit condition. `decide` is a short function with `any(...)` and a conditional expression: a blocking failure means "DON'T LAUNCH YET", any other failure "LAUNCH WITH CONDITIONS", none "LAUNCH".

### Calling another program: `subprocess`

```python
def run(
    self, *args: str, env: dict[str, str] | None = None, stdin: bytes | None = None, timeout: float = 600
) -> bytes:
    """Run `docker compose <args>` and return standard output; raise ComposeError on a non-zero exit.

    A busy Docker daemon sometimes answers with a transient error ("Bad Gateway", a reset connection); those are
    retried, anything else fails at once.
    """
    for attempt in range(1, _ATTEMPTS + 1):
        result = subprocess.run(
            ["docker", "compose", *args],
            cwd=self._dir,
            env={**os.environ, **(env or {})},
            input=stdin,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout
        stderr = result.stderr.decode(errors="replace")
        if attempt == _ATTEMPTS or not any(marker in stderr for marker in _TRANSIENT):
            raise ComposeError(f"docker compose {' '.join(args)} failed: {stderr[-800:]}")
        time.sleep(attempt * _RETRY_PAUSE_SECONDS)
    raise AssertionError("unreachable")  # pragma: no cover
```

`subprocess.run(["docker", "compose", *args], ...)`: the list is the program and its arguments (`*args` unpacks a tuple of arguments into the list). `capture_output=True` collects the output; `check=False` means "do not raise on a non-zero exit, I will look at `returncode`". The loop retries only when the error text contains one of the known transient markers (`any(marker in stderr for marker in _TRANSIENT)`), waiting longer each time (`time.sleep(attempt * _RETRY_PAUSE_SECONDS)`).

### Reading and writing files

```python
class JsonEvidence:
    """Evidence documents under `docs/evidence/<name>.json`, written by the make targets."""

    def __init__(self, directory: Path) -> None:
        """`directory` holds the JSON files."""
        self._directory = directory

    def load(self, name: str) -> dict[str, object] | None:
        """The evidence called `name`, or None when the experiment has not been run."""
        path = self._directory / f"{name}.json"
        if not path.exists():
            return None
        document: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
        return document

    def save(self, name: str, document: dict[str, Any]) -> Path:
        """Write the evidence, creating the folder."""
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._directory / f"{name}.json"
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        return path
```

`Path.read_text`, `write_text`, `json.loads`, `json.dumps(document, indent=2)`: evidence is JSON on disk. `dict[str, object] | None` as a return type says "a dictionary or None when the experiment has not run".

### The command line

```python
def build_parser() -> argparse.ArgumentParser:
    """The command-line interface."""
    parser = argparse.ArgumentParser(prog="capacity-loadtest", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    knee = sub.add_parser("knee", help="step the load up against one instance until p99 climbs sharply")
    knee.add_argument("--target", required=True, help="the instance, for example http://app-a1:8080")
    knee.add_argument("--ops", required=True, help="the instance's ops port, for CPU, for example http://app-a1:9101")
    knee.add_argument("--steps", default=DEFAULT_STEPS, help="comma-separated requests per second")
    knee.add_argument("--step-seconds", type=float, default=10.0, help="the lesson uses 120 for the real thing")
    knee.add_argument("--margin", type=float, default=0.25, help="how far below the knee to set the safe load")
    for name, helptext in (
        ("soak", "hold one instance at the safe load"),
        ("fleet", "load the fleet through the load balancer"),
    ):
        run = sub.add_parser(name, help=helptext)
        run.add_argument("--target", required=True)
        run.add_argument("--rps", type=float, required=True)
        run.add_argument("--seconds", type=float, required=True)
        run.add_argument("--label", default=name)
        run.add_argument("--slo-p99-ms", type=float, default=50.0)
        run.add_argument("--window", type=float, default=10.0, help="seconds per p99 window")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point of `capacity-loadtest`."""
    configure_logging("WARNING")
    args = build_parser().parse_args(argv)
    result = asyncio.run(_knee(args) if args.command == "knee" else _timed(args))
```

`argparse` builds the command line. `add_subparsers` creates `knee`, `soak` and `fleet`. `type=float` converts the text; `required=True` makes an option mandatory; `default=` supplies a value; `help=` is the text shown by `--help`.

### Standard library modules used

| Module | Where | What it does |
| --- | --- | --- |
| `hashlib`, `math`, `statistics` | domain | The CPU work, ceil/floor, median. |
| `dataclasses`, `enum`, `typing` | everywhere | Data containers, enums, Protocol. |
| `asyncio` | generator, ledger, instance | See the asyncio note. |
| `argparse` | `interfaces/*_cli.py` | Command-line arguments. |
| `json`, `pathlib` | evidence, reports | Files and JSON. |
| `re` | open-loop probe, report | See the regular expressions note. |
| `subprocess`, `time`, `urllib.request` | `compose.py` | Run Docker Compose, wait, poll Envoy. |
| `collections.deque` | `openloop.py` | A queue of idle connections. |
| `logging` | `logging_setup.py` | See the logging note. |

## Try it yourself

1. `.venv/bin/python -c "from capacity.domain.cpu import cpu_work; import time; t=time.perf_counter(); cpu_work(); print(round((time.perf_counter()-t)*1000,2), 'ms')"` prints about 1 ms: the cost of one request on this machine.
2. `.venv/bin/python -c "from capacity.domain.plan import plan_capacity; print(plan_capacity(1000, 2, 400).formula())"` prints the written-out formula: 5 instances needed, 8 in total as 3/3/2, 5 survivors.
3. `.venv/bin/python -c "from capacity.domain.latency import percentile; print(percentile([1,2,3,4,100], 0.99))"` prints `100`.
4. `.venv/bin/python -c "from capacity.domain.plan import plan_capacity; plan_capacity(1000, 2, 0)"` fails with `InvalidPlanError`.
5. `.venv/bin/python -m pytest tests/unit/test_knee_and_plan.py -q` runs the knee and plan tests in a fraction of a second.

## Mistakes people make

- Comparing floats with `==`. The tests use `pytest.approx`.
- Dividing by zero when there are no samples; the code returns 0.0 for an empty list.
- Mutating a frozen dataclass; use `replace`.
- Treating a measurement taken on a busy machine as a fact. The generator reports its own lag so you can tell.

## Check yourself

1. What does `frozen=True` do and how do you change a field anyway?
   *Answer: the instance cannot be modified; `dataclasses.replace` makes a changed copy.*
2. What does `divmod(8, 3)` return?
   *Answer: `(2, 2)`: quotient and remainder.*
3. How is the nearest-rank p99 chosen?
   *Answer: sort the values and take the one at position `ceil(0.99 * count)`.*
4. Why does `cpu_work` hash in a loop instead of sleeping?
   *Answer: it burns real CPU, so the knee found is a real resource limit.*
5. Why `check=False` in `subprocess.run`?
   *Answer: the code inspects the exit code itself so it can retry transient failures.*

## Further reading

- Python 3.12 tutorial: <https://docs.python.org/3.12/tutorial/>
- `dataclasses`: <https://docs.python.org/3.12/library/dataclasses.html>
- `hashlib`: <https://docs.python.org/3.12/library/hashlib.html>
- `statistics.median`: <https://docs.python.org/3.12/library/statistics.html#statistics.median>
- `subprocess`: <https://docs.python.org/3.12/library/subprocess.html>
