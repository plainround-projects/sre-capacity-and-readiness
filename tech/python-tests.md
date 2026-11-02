# Python: the tests, file by file

## What it is

The tests are the folder `tests/`. They are Python files that `pytest` finds and runs. A test is a function whose name starts with `test_`; it builds a small situation, calls the code and uses `assert` to say what must be true. If an `assert` is false the test fails and pytest shows what differed. This note walks through every test file of this repository, in four groups: unit tests (`tests/unit/`, pure code, milliseconds), integration tests (`tests/integration/`, real sockets inside the test process, no containers), measurement tests (`tests/measurements/`, the lesson's "Done when" lines held against the committed measurements) and the end-to-end test (`tests/e2e/`, needs the Docker Compose stack).

## Why this repository uses it

The repository claims things: the knee search finds the knee, the plan survives losing a zone, the review fails when evidence is missing, the generated rules match the definitions, and the measured numbers meet the lesson's targets. A claim without a test that can fail is only a hope. Each group answers a different question at a different cost, so the cheap ones run on every commit and the expensive ones run on request.

## Where it appears

| File | What it checks |
| --- | --- |
| `tests/conftest.py` | Shared fixtures. |
| `tests/unit/test_knee_and_plan.py` | Finding the knee, the safe load, the fleet plan. |
| `tests/unit/test_latency_and_loadtest.py` | Percentiles, summaries, the knee search with a fake runner, timed runs, pool helpers, retries. |
| `tests/unit/test_capacity_report.py` | The generated capacity report and the runbook facts. |
| `tests/unit/test_readiness.py` | The readiness review and the launch decision document. |
| `tests/unit/test_slo_domain.py`, `test_sli_and_alerting.py`, `test_settings_and_rules.py`, `test_evidence.py` | The SLO arithmetic, alert replay, rule generation, settings and the committed evidence. |
| `tests/integration/test_instance_and_loadgen.py`, `test_cli_and_adapters.py` | The instance, the load generator, the command-line programs and the adapters. |
| `tests/measurements/test_done_when.py` | The lesson's targets against the committed measurements. |
| `tests/e2e/test_fleet_smoke.py` | The running fleet. |

## How it works underneath

**Fixtures** are functions decorated with `@pytest.fixture`; a test asks for one by naming it as a parameter and pytest passes the value in. **`pytest.mark.parametrize`** runs one test body over several inputs. **`pytest.raises`** passes only if the block raises the named error. With `asyncio_mode = "auto"` in `pyproject.toml`, `async def` tests run on an event loop without a marker. A **fake** is a small class standing in for a port (`FakeRunner`, `Evidence`, `Files`), which is why the layers are separated. A module-level `pytestmark = pytest.mark.integration` puts a **marker** on every test in the file so `make` targets can select a group.

## Walkthrough of this repository's use

### `tests/conftest.py`: shared fixtures

```python
"""Shared fixtures: the project root, the real SLO spec, and the committed evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from capacity.infrastructure.slo_files import SloSpec, load_spec

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "docs" / "evidence"


@pytest.fixture(scope="session")
def root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def spec() -> SloSpec:
    return load_spec(ROOT / "slo" / "slos.yaml")


@pytest.fixture(scope="session")
def evidence() -> Any:
    def load(name: str) -> dict[str, Any]:
        document: dict[str, Any] = json.loads((EVIDENCE / f"{name}.json").read_text())
        return document

    return load
```

`conftest.py` is loaded by pytest before the tests of its folder and below. `ROOT` is the repository root (two levels above this file) and `EVIDENCE` the folder of committed measurements. Three **session-scoped** fixtures (built once per run): `root` gives the path, `spec` loads the real `slo/slos.yaml`, and `evidence` returns a function that loads a committed evidence document by name.

### `tests/unit/test_knee_and_plan.py`

```python
import pytest

from capacity.domain.errors import InvalidPlanError, NoKneeError
from capacity.domain.knee import StepResult, find_knee, safe_load
from capacity.domain.plan import plan_capacity, spread


def step(rps, p99, errors=0, cpu=None):
    return StepResult(rps, rps, 1000, errors, p99 / 2, p99, cpu)


# The lesson's own measurements: p99 flat at 12 to 18 ms through 500 rps, then 153.7 ms at 550.
LESSON = [
    step(100, 12.1),
    step(200, 13.0),
    step(300, 15.2),
    step(400, 16.3),
    step(450, 18.4),
    step(500, 17.1),
    step(550, 153.7, cpu=0.99),
    step(600, 877.0, cpu=1.0),
    step(800, 1425.3, cpu=1.0),
]

```

`step(rps, p99, errors, cpu)` builds a `StepResult` for a target rate (achieved equals target, 1,000 requests, p50 half the p99). `LESSON` is the lesson's own measurement: p99 flat at 12 to 18 ms through 500 requests a second, then 153.7 ms at 550, 877 at 600 and 1,425 at 800.

```python
class TestKnee:
    def test_the_knee_is_the_first_step_where_p99_jumps_and_the_last_good_step_is_before_it(self):
        knee = find_knee(LESSON)
        assert (knee.knee_rps, knee.last_good_rps) == (550, 500)

    def test_the_resource_is_named_from_cpu_evidence_not_from_throughput(self):
        assert find_knee(LESSON).saturated_resource == "cpu"

    def test_without_cpu_evidence_the_resource_is_not_guessed(self):
        flat = [step(rps, 10.0 if rps < 500 else 400.0) for rps in (100, 200, 300, 400, 500)]
        assert find_knee(flat).saturated_resource.startswith("unknown")

    def test_a_curve_that_never_rises_has_no_knee(self):
        with pytest.raises(NoKneeError):
            find_knee([step(rps, 12.0) for rps in (100, 200, 300, 400, 500)])

    def test_too_few_steps_cannot_show_a_flat_part_and_a_knee(self):
        with pytest.raises(NoKneeError):
            find_knee([step(100, 12.0), step(200, 500.0)])

    def test_errors_alone_also_mark_the_knee(self):
        steps = [step(100, 12.0), step(200, 12.0), step(300, 12.0), step(400, 14.0, errors=50)]
        assert find_knee(steps).knee_rps == 400

    def test_a_spike_that_goes_away_again_is_noise_not_a_knee(self):
        noisy = [step(100, 12.0), step(200, 12.0), step(300, 12.0), step(400, 90.0), step(500, 13.0), step(600, 14.0)]
        with pytest.raises(NoKneeError):
            find_knee(noisy)

    def test_a_rise_that_persists_is_the_knee_even_if_it_dips_later(self):
        steps = [step(100, 12.0), step(200, 12.0), step(300, 12.0), step(400, 90.0), step(500, 400.0), step(600, 30.0)]
        assert find_knee(steps).knee_rps == 400

    def test_steps_given_out_of_order_are_sorted(self):
        assert find_knee(list(reversed(LESSON))).knee_rps == 550

```

`TestKnee` groups the knee tests. The knee is the first step where p99 jumps and the last good step is the one before it (550 and 500). The resource is named `cpu` only from CPU evidence; without it the name starts with `unknown`. A flat curve and a curve with too few steps raise `NoKneeError`. Errors alone also mark a knee. A spike that goes away on the next step is noise, but a rise that persists is the knee even if the curve dips later. Steps given out of order are sorted first.

```python
class TestSafeLoad:
    def test_a_quarter_below_the_lessons_knee_is_the_lessons_400(self):
        assert safe_load(find_knee(LESSON)) == 400

    def test_the_margin_is_a_judgment_call_you_can_change(self):
        assert safe_load(find_knee(LESSON), margin=0.30) == 375

    def test_rounds_down_to_a_step_so_the_number_is_easy_to_say(self):
        assert safe_load(find_knee(LESSON), margin=0.25, step=50) == 400

    @pytest.mark.parametrize("margin", [0, 1, -0.2, 1.5])
    def test_the_margin_must_be_a_fraction(self, margin):
        with pytest.raises(InvalidPlanError):
            safe_load(find_knee(LESSON), margin=margin)

```

`TestSafeLoad`: a quarter below 550 is 412.5, rounded **down** to a multiple of 25 gives the lesson's 400; a 30% margin gives 375; a coarser step of 50 still gives 400. The margin must be a fraction strictly between 0 and 1: `parametrize` runs 0, 1, -0.2 and 1.5, and each must raise `InvalidPlanError`.

```python
class TestPlan:
    def test_the_lessons_numbers_give_five_instances_for_peak_and_eight_in_all(self):
        plan = plan_capacity(1000, 2, 400, zones=3)
        assert (plan.future_peak_rps, plan.instances_for_peak, plan.total_instances) == (2000, 5, 8)
        assert plan.distribution == (3, 3, 2)
        assert plan.surviving_after_zone_loss == 5

    def test_the_surviving_fleet_carries_the_whole_projected_peak(self):
        plan = plan_capacity(1000, 2, 400, zones=3)
        assert plan.surviving_capacity_rps >= plan.future_peak_rps

    def test_headroom_comes_from_the_projected_peak_not_todays(self):
        today = plan_capacity(1000, 1, 400)
        future = plan_capacity(1000, 2, 400)
        assert future.total_instances > today.total_instances

    @pytest.mark.parametrize("peak", [500, 1000, 1777, 3000, 10_000])
    def test_whatever_the_load_losing_the_largest_zone_still_leaves_enough(self, peak):
        plan = plan_capacity(peak, 2, 313, zones=3)
        assert plan.total_instances - max(plan.distribution) >= plan.instances_for_peak

    def test_more_zones_need_less_headroom(self):
        assert (
            plan_capacity(1000, 2, 400, zones=4).total_instances <= plan_capacity(1000, 2, 400, zones=3).total_instances
        )

    def test_spread_gives_larger_zones_first(self):
        assert spread(8, 3) == (3, 3, 2)
        assert spread(9, 3) == (3, 3, 3)

    def test_the_formula_is_written_out_with_every_input(self):
        text = plan_capacity(1000, 2, 400).formula()
        for part in ("1000 x 2 = 2000", "ceil(2000 / 400) = 5", "3/3/2"):
            assert part in text

    @pytest.mark.parametrize("args", [(0, 2, 400), (1000, 0, 400), (1000, 2, 0)])
    def test_inputs_must_be_positive(self, args):
        with pytest.raises(InvalidPlanError):
            plan_capacity(*args)

    def test_one_zone_cannot_survive_losing_itself(self):
        with pytest.raises(InvalidPlanError):
            plan_capacity(1000, 2, 400, zones=1)
```

`TestPlan` checks the formula with the lesson's numbers (1,000 requests a second today, twice that in six months, 400 per instance): five instances for the peak, eight in all, spread 3, 3, 2, and five surviving a zone loss that carry the whole projected peak. Headroom comes from the projected peak, not today's. Whatever the load (five peaks are tried), losing the largest zone still leaves enough. More zones need no more instances than fewer zones. `spread` gives larger zones first. The written-out formula contains every input. Non-positive inputs and a single zone raise `InvalidPlanError`.

`TestJitterIsNotAKnee` pins the floor under the baseline:

```python
class TestJitterIsNotAKnee:
    def test_a_few_milliseconds_of_jitter_on_a_tiny_baseline_is_not_a_rise(self):
        gentle = [
            step(rps, p99) for rps, p99 in ((100, 4.0), (200, 4.5), (300, 5.0), (400, 12.0), (500, 19.0), (600, 28.0))
        ]
        with pytest.raises(NoKneeError):
            find_knee(gentle)

    def test_a_rise_that_clears_three_times_the_floor_is_still_a_knee(self):
        steps = [step(rps, p99) for rps, p99 in ((100, 4.0), (200, 4.5), (300, 5.0), (400, 60.0), (500, 300.0))]
        assert find_knee(steps).knee_rps == 400
```

A curve from a 4 ms baseline that grows to 12, 19 and 28 ms is queueing and jitter, not a knee (it raises `NoKneeError`); a jump to 60 ms and then 300 ms is, and the knee is at 400.

### `tests/unit/test_latency_and_loadtest.py`

```python
import pytest

from capacity.application.experiments import choose_pool_size, naive_pool_size, sweep_sizes
from capacity.application.loadtest import knee_test, timed_run
from capacity.domain.latency import Sample, percentile, summarize, window_p99s


class FakeRunner:
    """A service whose p99 is flat until 500 rps and then jumps, like the lesson's."""

    def __init__(self, knee=500, lag_ms=0.1):
        self.knee = knee
        self.lag_ms = lag_ms
        self.calls = []

    async def run(self, rps, duration_s):
        self.calls.append(rps)
        latency = 0.015 if rps < self.knee else 0.5 + (rps - self.knee) / 1000
        count = int(rps * duration_s)
        return [Sample(index / rps, latency, True) for index in range(count)], self.lag_ms


class FakeProbe:
    """CPU seconds that rise at the rate the fake service would use: all of a core past the knee."""

    def __init__(self):
        self.now = 0.0
        self.calls = 0

    async def cpu_seconds(self):
        self.calls += 1
        self.now += 2.0 if self.calls % 2 == 0 else 0.0
        return self.now

```

`FakeRunner` is a service whose p99 is flat (15 ms) until its knee and then jumps; it stands in for `OpenLoopRunner` and records the rates it was asked for. `FakeProbe` is a CPU counter that rises 2 seconds on every second reading, which with a 1-second step is a utilization above one core, so the knee names CPU.

```python
class TestPercentiles:
    def test_nearest_rank_p99_of_one_hundred_values_is_the_hundredth_largest_but_one(self):
        assert percentile(list(range(1, 101)), 0.99) == 99

    def test_empty_input_is_zero(self):
        assert percentile([], 0.99) == 0.0

    def test_one_value_is_every_percentile(self):
        assert percentile([7.0], 0.5) == 7.0


class TestSummaries:
    def test_latency_runs_from_the_scheduled_arrival_in_milliseconds(self):
        samples = [Sample(0.0, 0.010, True), Sample(0.1, 0.020, True), Sample(0.2, 0.300, False)]
        result = summarize(samples, target_rps=10, duration_s=0.3)
        assert (result.requests, result.errors, result.p99_ms, result.max_ms) == (3, 1, 300.0, 300.0)
        assert result.error_rate == pytest.approx(1 / 3)

    def test_window_p99s_show_drift_that_an_average_would_hide(self):
        samples = [Sample(float(second), 0.010 + second * 0.001, True) for second in range(30)]
        windows = window_p99s(samples, 10.0)
        assert windows == sorted(windows)
        assert windows[-1] > windows[0]
```

`TestPercentiles`: the nearest-rank p99 of the numbers 1 to 100 is 99; an empty list gives 0; one value is every percentile. `TestSummaries`: latency is in milliseconds, the 300 ms failed request is the p99 and the max, the error rate is one third (`pytest.approx` allows floating-point error), and per-window p99s are sorted for a steadily degrading service so a drift an average hides shows in the last window.

```python
class TestKneeTest:
    async def test_it_finds_the_knee_names_cpu_and_chooses_a_safe_load(self):
        runner = FakeRunner()
        report = await knee_test(
            runner, FakeProbe(), [100, 200, 300, 400, 500, 550, 600, 700, 800, 900], step_seconds=1
        )
        assert report.knee is not None
        assert report.knee.knee_rps == 500
        assert report.safe_rps == 375
        assert report.knee.saturated_resource == "cpu"

    async def test_it_stops_two_steps_past_the_knee_instead_of_driving_the_instance_into_the_ground(self):
        runner = FakeRunner()
        await knee_test(runner, None, [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000], step_seconds=1)
        assert runner.calls == [100, 200, 300, 400, 500, 500, 600, 600, 700, 700]  # each suspicious step runs twice

    async def test_a_service_that_never_saturates_reports_no_knee_instead_of_inventing_one(self):
        report = await knee_test(FakeRunner(knee=10_000), None, [100, 200, 300, 400, 500], step_seconds=1)
        assert report.knee is None
        assert report.safe_rps is None

```

`TestKneeTest`: the search finds the knee at 500, chooses a safe load of 375 and names `cpu`. It stops two steps past the knee: the list of rates called is `[100, 200, 300, 400, 500, 500, 600, 600, 700, 700]`, where each suspicious step ran twice (the retry). A service that never saturates reports no knee and no safe load instead of inventing one.

```python
class TestTimedRun:
    async def test_a_run_inside_the_slo_is_within_it(self):
        report = await timed_run(FakeRunner(), "soak", 400, 20, slo_p99_ms=50, window_s=5)
        assert report.within_slo
        assert len(report.window_p99_ms) == 4

    async def test_one_bad_window_breaks_the_run_even_if_the_average_is_fine(self):
        class Drifting(FakeRunner):
            async def run(self, rps, duration_s):
                samples, lag = await super().run(rps, duration_s)
                return [Sample(s.scheduled_s, 0.2 if s.scheduled_s > 15 else s.latency_s, True) for s in samples], lag

        report = await timed_run(Drifting(), "soak", 100, 20, slo_p99_ms=50, window_s=5)
        assert not report.within_slo

    async def test_any_error_breaks_the_run(self):
        class Failing(FakeRunner):
            async def run(self, rps, duration_s):
                samples, lag = await super().run(rps, duration_s)
                return [*samples[:-1], Sample(1.0, 0.01, False)], lag

        assert not (await timed_run(Failing(), "fleet", 100, 5, 50)).within_slo

```

`TestTimedRun`: a run inside the SLO is within it and has four 5-second windows in 20 seconds. A `Drifting` subclass makes the last seconds slow: one bad window breaks the run even though the average is fine. A `Failing` subclass adds one failed request: any error breaks the run.

```python
class TestPoolHelpers:
    def test_the_naive_pool_is_peak_times_the_average_hold(self):
        assert naive_pool_size(2000, 10) == 20

    def test_the_sweep_starts_at_the_naive_size_and_goes_well_past_it(self):
        assert sweep_sizes(20) == [20, 24, 30, 40]

    def test_the_adopted_size_is_the_smallest_that_held_plus_a_quarter_rounded_up_to_five(self):
        results = [
            {"pool_size": 20, "within_slo": False},
            {"pool_size": 24, "within_slo": False},
            {"pool_size": 30, "within_slo": True},
            {"pool_size": 40, "within_slo": True},
        ]
        assert choose_pool_size(results) == 40

    def test_no_size_that_held_means_no_choice(self):
        assert choose_pool_size([{"pool_size": 20, "within_slo": False}]) is None

```

`TestPoolHelpers`: the naive pool is peak times the hold time (20), the sweep starts there and goes well past it, the adopted size is the smallest that held plus a quarter rounded up to five (30 × 1.25 = 37.5, so 40), and no size that held means no choice (`None`).

```python
class TestSuspiciousStepsAreRetried:
    async def test_a_stall_that_does_not_repeat_is_not_a_knee(self):
        class StallsOnce(FakeRunner):
            def __init__(self):
                super().__init__(knee=10_000)
                self.stalled = False

            async def run(self, rps, duration_s):
                samples, lag = await super().run(rps, duration_s)
                if rps == 400 and not self.stalled:
                    self.stalled = True
                    return [Sample(s.scheduled_s, 1.5, True) for s in samples], lag
                return samples, lag

        report = await knee_test(StallsOnce(), None, [100, 200, 300, 400, 500, 600], step_seconds=1)
        assert report.knee is None
        step_400 = next(step for step in report.steps if step.target_rps == 400)
        assert step_400.attempts == 2
        assert step_400.p99_ms < 50

    async def test_a_real_knee_is_there_on_the_second_try_too(self):
        runner = FakeRunner(knee=500)
        report = await knee_test(runner, None, [100, 200, 300, 400, 500, 600, 700, 800], step_seconds=1)
        assert report.knee is not None
        assert report.knee.knee_rps == 500
        assert runner.calls.count(500) == 2

    async def test_healthy_steps_run_once(self):
        runner = FakeRunner(knee=10_000)
        await knee_test(runner, None, [100, 200, 300, 400, 500], step_seconds=1)
        assert runner.calls == [100, 200, 300, 400, 500]
```

`TestSuspiciousStepsAreRetried`: a service that stalls once at 400 requests a second reports no knee, because the second attempt is fine; the step records two attempts and a p99 under 50 ms. A real knee is there on the second try too (500 was called twice). Healthy steps run once.

`TestRunsThatSpikeWithoutErrors` covers the retry and the "not evidence" rules:

```python
class TestRunsThatSpikeWithoutErrors:
    class SpikesOnce(FakeRunner):
        def __init__(self):
            super().__init__(knee=10_000)
            self.runs = 0

        async def run(self, rps, duration_s):
            self.runs += 1
            samples, lag = await super().run(rps, duration_s)
            if self.runs == 1:
                return [Sample(s.scheduled_s, 0.3 if s.scheduled_s > 3 else s.latency_s, True) for s in samples], lag
            return samples, lag

    class AlwaysSpikes(FakeRunner):
        async def run(self, rps, duration_s):
            samples, lag = await super().run(rps, duration_s)
            return [Sample(s.scheduled_s, 0.3, True) for s in samples], lag

    class BarelySlow(FakeRunner):
        async def run(self, rps, duration_s):
            samples, lag = await super().run(rps, duration_s)
            return [Sample(s.scheduled_s, 0.055, True) for s in samples], lag

    class IdleCore:
        async def cpu_seconds(self):
            return 0.0

    async def test_a_stall_that_does_not_repeat_is_run_again_and_the_clean_run_is_reported(self):
        runner = self.SpikesOnce()
        report = await timed_run(runner, "soak", 100, 10, slo_p99_ms=50, window_s=5, attempts=3)
        assert report.within_slo
        assert (runner.runs, report.result.attempts) == (2, 2)

    async def test_a_spike_that_repeats_with_the_core_idle_is_a_disturbed_machine_not_evidence(self):
        report = await timed_run(
            self.AlwaysSpikes(), "soak", 100, 10, slo_p99_ms=50, window_s=5, probe=self.IdleCore(), attempts=2
        )
        assert report.result.attempts == 2
        assert report.problem is not None
        assert "host" in report.problem

    async def test_a_spike_with_no_probe_is_reported_as_it_is(self):
        report = await timed_run(self.AlwaysSpikes(), "fleet", 100, 10, slo_p99_ms=50, window_s=5, attempts=2)
        assert not report.within_slo
        assert report.problem is None

    async def test_a_spike_a_late_generator_could_explain_is_untrustworthy(self):
        report = await timed_run(self.BarelySlow(lag_ms=40.0), "soak", 100, 5, slo_p99_ms=50, window_s=5)
        assert report.problem is not None
        assert "late" in report.problem

    async def test_a_spike_far_beyond_the_generators_lateness_is_the_services(self):
        report = await timed_run(self.AlwaysSpikes(lag_ms=9.0), "fleet", 100, 5, slo_p99_ms=50, window_s=5)
        assert not report.within_slo
        assert report.problem is None

    async def test_a_late_generator_cannot_fake_a_pass(self):
        report = await timed_run(FakeRunner(lag_ms=40.0), "soak", 100, 5, slo_p99_ms=50, window_s=5)
        assert report.within_slo
        assert report.problem is None

    async def test_a_knee_without_cpu_at_its_limit_is_not_trustworthy(self):
        report = await knee_test(FakeRunner(knee=500), None, [100, 200, 300, 400, 500, 600, 700], step_seconds=1)
        assert report.knee is not None
        assert report.problem is not None
        assert "host" in report.problem
```

Three fakes are nested in the class. `SpikesOnce` makes the first run slow after 3 seconds and later runs clean; `AlwaysSpikes` makes every run slow; `IdleCore` is a CPU probe that always reads 0, so the core looks idle. The tests say: a stall that does not repeat is run again and the clean run is reported (two runs, `attempts == 2`); a spike that repeats while the core is idle is a disturbed machine, so the report has a `problem` that mentions the host; a spike with no probe is reported as it is (a genuine shortage must not be hidden), so `problem` is `None`; a generator 40 ms late makes the run untrustworthy; and a knee found where the core was not at its limit is not trustworthy either.

### `tests/unit/test_capacity_report.py`

```python
from capacity.application import capacity_report
from capacity.application.loadtest import KneeReport
from capacity.domain.knee import Knee, StepResult

STEP = {
    "target_rps": 100,
    "achieved_rps": 100.0,
    "requests": 1000,
    "errors": 0,
    "p50_ms": 5.0,
    "p99_ms": 9.0,
    "cpu_utilization": 0.3,
}
EVIDENCE = {
    "knee": {
        "step_seconds": 10,
        "steps": [STEP, {**STEP, "target_rps": 500, "p99_ms": 120.0, "cpu_utilization": 0.99}],
        "knee": {
            "knee_rps": 500,
            "last_good_rps": 400,
            "baseline_p99_ms": 9.0,
            "knee_p99_ms": 120.0,
            "cpu_utilization_at_knee": 0.99,
            "saturated_resource": "cpu",
        },
        "safe_rps": 375,
        "margin_below_knee": 0.25,
    },
    "soak": {"seconds": 120, "p99_ms": 12.0, "window_p99_ms": [11.0, 12.5], "errors": 0},
    "plan": {
        "current_peak_rps": 1000,
        "future_peak_rps": 2000,
        "growth_factor": 2,
        "safe_rps_per_instance": 375,
        "instances_for_peak": 6,
        "total_instances": 9,
        "distribution": [3, 3, 3],
        "surviving_after_zone_loss": 6,
        "formula": "the formula, written out",
    },
    "fleet-full": {"healthy_instances": 9, "target_rps": 2000, "p99_ms": 14.0, "errors": 0, "within_slo": True},
    "fleet-zone-loss": {"healthy_instances": 6, "target_rps": 2000, "p99_ms": 20.0, "errors": 0, "within_slo": True},
    "pool-sweep": {
        "hold_ms": 10,
        "naive_pool_size": 20,
        "sweep": [
            {"pool_size": 20, "p99_ms": 523.0, "error_rate": 0.22, "within_slo": False},
            {"pool_size": 30, "p99_ms": 31.0, "error_rate": 0.0, "within_slo": True},
        ],
    },
    "fleet-pool-naive": {},
    "fleet-pool-resized": {"pool_size": 40, "p99_ms": 25.0, "errors": 0},
}


class Evidence:
    def __init__(self, documents):
        self.documents = documents

    def load(self, name):
        return self.documents.get(name)

```

The test builds an `EVIDENCE` dictionary shaped like the saved JSON documents (a two-step knee, a soak, a plan of 9 instances over three zones, the two fleet runs, a pool sweep, the naive and resized pool runs) and a tiny `Evidence` class with `load`. Nothing here needs Docker.

```python
def test_the_report_needs_every_experiment_and_says_which_are_missing():
    partial = {k: v for k, v in EVIDENCE.items() if k != "soak"}
    assert capacity_report.missing(Evidence(partial)) == ["soak"]
    assert capacity_report.missing(Evidence(EVIDENCE)) == []


def test_the_report_carries_the_knee_table_the_formula_and_the_pool_sweep():
    text = capacity_report.render(Evidence(EVIDENCE))
    for expected in (
        "| 500 | 100 |",
        "the formula, written out",
        "| 20 | 523.0 | 0.22 | no |",
        "**40**",
        "**375 requests a second**",
    ):
        assert expected in text.replace("100.0", "100") or expected in text


def test_a_knee_report_serializes_for_the_evidence_file():
    steps = (StepResult(100, 99.5, 1000, 0, 5.0, 9.0, 0.3, 21.0),)
    knee = Knee(500, 400, 9.0, 120.0, 0.99)
    document = KneeReport(steps, knee, 375, 0.25, 0.4).to_dict()
    assert document["safe_rps"] == 375
    assert document["knee"]["saturated_resource"] == "cpu"
    assert document["steps"][0]["max_ms"] == 21.0
    assert KneeReport(steps, None, None, 0.25, 0.0).to_dict()["knee"] is None


def test_the_runbook_facts_quote_the_measured_numbers_from_the_evidence():
    facts = capacity_report.runbook_facts(Evidence(EVIDENCE))
    assert set(facts) == {"cpu-knee.md", "pool-exhaustion.md", "zone-loss.md"}
    assert "**500 requests a second**" in facts["cpu-knee.md"]
    assert "**375 requests a second**" in facts["cpu-knee.md"]
    assert "**20** connections" in facts["pool-exhaustion.md"]
    assert "**40** held it" in facts["pool-exhaustion.md"]
    assert "leaves **6**" in facts["zone-loss.md"]
```

Four tests: `missing` names exactly the evidence not produced yet (`["soak"]` when the soak is removed, `[]` when complete); `render` carries the knee table row, the formula, the pool sweep row and the bold safe load and pool size (the loop passes if any expected text is in the output); a `KneeReport` serializes for the evidence file with the resource named, a step's max latency and `None` for a missing knee; and `runbook_facts` returns the three runbook names with the measured numbers quoted (knee 500, safe load 375, naive pool 20, resized pool 40, six surviving instances).

### `tests/unit/test_readiness.py`

```python
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from capacity.application import launch_decision
from capacity.application.readiness import Reviewer, _metric_names
from capacity.domain.errors import InvalidReviewError
from capacity.domain.readiness import Decision, ReviewItem, Verdict, decide
from capacity.domain.slo import DEFAULT_ALERTS, SliKind, Slo

DUE = date(2026, 11, 3)


class Files:
    def __init__(self, files: dict[str, str]):
        self.files = files
        self.root = Path(".")

    def exists(self, relative):
        return relative in self.files

    def read(self, relative):
        return self.files[relative]

    def glob(self, pattern):
        prefix, _, suffix = pattern.partition("*")
        return sorted(name for name in self.files if name.startswith(prefix) and name.endswith(suffix))


class Evidence:
    def __init__(self, documents: dict[str, dict[str, Any]]):
        self.documents = documents

    def load(self, name):
        return self.documents.get(name)


class Slos:
    def __init__(self, in_sync=True, alerts=DEFAULT_ALERTS):
        self._in_sync, self._alerts = in_sync, alerts

    def slos(self):
        return [Slo("api_availability", "api", SliKind.AVAILABILITY, 0.999, "all", "not 5xx", "why")]

    def alerts(self):
        return list(self._alerts)

    def rules_in_sync(self):
        return self._in_sync


class Config:
    def __init__(self, risks=()):
        self._risks = list(risks)

    def owner(self, item_id):
        return "owner role"

    def due(self, item_id):
        return DUE

    def blocking(self, item_id):
        return item_id == "runbooks"

    def accepted_risks(self):
        return self._risks


RUNBOOK = "# Runbook\n## Symptoms\nx\n## Mitigate\ny\n## Diagnose\nz\nThe knee is 550 rps and the safe load is 400 rps."
```

The review is tested with **fakes** of its four ports: `Files` (a dictionary of file name to text, with `exists`, `read` and a simple `glob`), `Evidence` (a dictionary of documents), `Slos` (one SLO, the default alert table and a switch for whether the generated rules are in sync) and `Config` (a fixed owner and due date, and only the `runbooks` item blocking). `RUNBOOK` is a minimal runbook with the three headings and the lesson's knee (550) and safe load (400).

```python
GOOD_FILES = {
    "slo/tests/burn_rate.test.yml": "",
    "slo/tests/fleet.test.yml": "",
    "dashboards/fleet.json": json.dumps(
        {"panels": [{"targets": [{"expr": 'sum(rate(envoy_http_downstream_rq_xx{a="b"}[5m]))'}]}]}
    ),
    "docs/runbooks/cpu-knee.md": RUNBOOK,
    "docs/runbooks/pool-exhaustion.md": RUNBOOK,
    "docs/runbooks/zone-loss.md": RUNBOOK,
    "docs/ownership.md": "Owner: x. Primary and secondary on-call. Escalation path. Runbooks index.",
}
GOOD_EVIDENCE: dict[str, dict[str, Any]] = {
    "knee": {"knee": {"knee_rps": 550}, "safe_rps": 400},
    "soak": {"within_slo": True, "seconds": 120, "p99_ms": 15.0},
    "fleet-zone-loss": {"within_slo": True, "target_rps": 2000, "p99_ms": 20.0},
    "fleet-pool-naive": {"within_slo": False, "pool_size": 20, "p99_ms": 523.0, "error_rate": 0.2},
    "fleet-pool-resized": {"within_slo": True, "pool_size": 30, "p99_ms": 31.0},
    "rollout": {"within_slo": True, "rollback_ok": True, "restarted": 16, "rps": 1000, "errors": 0, "p99_ms": 20.0},
    "dependency": {"fail_fast": True, "instance_healthy": True, "slowest_503_ms": 520, "bound_ms": 750},
    "restore": {"matched": True, "rows": 5000, "restore_seconds": 1.2, "rto_target_seconds": 300},
}


def review(files=None, evidence=None, slos=None, config=None):
    reviewer = Reviewer(
        Files(files if files is not None else GOOD_FILES),
        Evidence(evidence if evidence is not None else GOOD_EVIDENCE),
        slos or Slos(),
        config or Config(),
    )
    return reviewer.review()


def verdicts(items):
    return {item.area: item.verdict for item in items}

```

`GOOD_FILES` and `GOOD_EVIDENCE` are a repository and a set of measurements that satisfy every check. `review(...)` builds a `Reviewer` from the good defaults with any part replaced, so each test breaks exactly one thing. `verdicts(items)` maps each area to its verdict.

```python
def test_with_every_piece_of_evidence_the_decision_is_launch():
    items, decision = review()
    assert decision is Decision.LAUNCH
    assert set(verdicts(items).values()) == {Verdict.PASS}


def test_an_untested_restore_fails_and_launches_with_conditions_owned_and_dated():
    evidence = {k: v for k, v in GOOD_EVIDENCE.items() if k != "restore"}
    items, decision = review(evidence=evidence)
    restore = next(item for item in items if item.area == "Backup and restore")
    assert restore.verdict is Verdict.FAIL
    assert (restore.owner, restore.due) == ("owner role", DUE)
    assert "not yet tested" in restore.evidence
    assert decision is Decision.LAUNCH_WITH_CONDITIONS


def test_a_restore_that_does_not_match_the_source_fails():
    evidence = {**GOOD_EVIDENCE, "restore": {**GOOD_EVIDENCE["restore"], "matched": False}}
    assert verdicts(review(evidence=evidence)[0])["Backup and restore"] is Verdict.FAIL


def test_a_restore_slower_than_the_recovery_target_fails():
    evidence = {**GOOD_EVIDENCE, "restore": {**GOOD_EVIDENCE["restore"], "restore_seconds": 900}}
    assert verdicts(review(evidence=evidence)[0])["Backup and restore"] is Verdict.FAIL


def test_a_blocking_failure_means_do_not_launch_yet():
    files = {k: v for k, v in GOOD_FILES.items() if k != "docs/runbooks/zone-loss.md"}
    items, decision = review(files=files)
    assert verdicts(items)["Runbooks for the top three failures"] is Verdict.FAIL
    assert decision is Decision.DONT_LAUNCH


def test_a_runbook_that_does_not_quote_the_measured_knee_fails():
    files = {**GOOD_FILES, "docs/runbooks/cpu-knee.md": RUNBOOK.replace("550", "999")}
    assert verdicts(review(files=files)[0])["Runbooks for the top three failures"] is Verdict.FAIL


def test_a_runbook_without_a_diagnose_section_fails():
    files = {**GOOD_FILES, "docs/runbooks/pool-exhaustion.md": RUNBOOK.replace("## Diagnose", "## Other")}
    assert verdicts(review(files=files)[0])["Runbooks for the top three failures"] is Verdict.FAIL


def test_out_of_date_generated_rules_fail_the_slo_item():
    assert verdicts(review(slos=Slos(in_sync=False))[0])["SLOs and burn-rate alerts"] is Verdict.FAIL


def test_an_alert_table_without_a_ticket_fails_the_slo_item():
    only_pages = [alert for alert in DEFAULT_ALERTS if alert.severity == "page"]
    assert verdicts(review(slos=Slos(alerts=only_pages))[0])["SLOs and burn-rate alerts"] is Verdict.FAIL


def test_a_dashboard_that_queries_a_metric_nothing_exports_fails():
    bad = json.dumps({"panels": [{"expr": "sum(rate(made_up_metric_total[5m]))"}]})
    files = {**GOOD_FILES, "dashboards/fleet.json": bad}
    items, _ = review(files=files)
    dashboards = next(item for item in items if item.area == "Dashboards")
    assert dashboards.verdict is Verdict.FAIL
    assert "made_up_metric_total" in dashboards.evidence


def test_a_frozen_ledger_that_makes_requests_hang_fails_the_dependency_item():
    evidence = {**GOOD_EVIDENCE, "dependency": {**GOOD_EVIDENCE["dependency"], "fail_fast": False}}
    assert verdicts(review(evidence=evidence)[0])["Dependency failure behavior"] is Verdict.FAIL


def test_a_naive_pool_that_did_not_fail_means_the_next_bottleneck_was_never_found():
    evidence = {**GOOD_EVIDENCE, "fleet-pool-naive": {**GOOD_EVIDENCE["fleet-pool-naive"], "within_slo": True}}
    items, _ = review(evidence=evidence)
    capacity = next(item for item in items if item.area == "Capacity")
    assert capacity.verdict is Verdict.FAIL
    assert "next bottleneck" in capacity.evidence

```

With everything present the decision is LAUNCH and every item passes. Removing the restore evidence fails that item with the owner and date and gives LAUNCH WITH CONDITIONS; a restore that does not match, or is slower than the target, fails. Removing a runbook is a blocking failure, so the decision is DON'T LAUNCH YET; a runbook that does not quote the measured knee fails; one without a Diagnose section fails. Out-of-date generated rules and an alert table without a ticket alert fail the SLO item. A dashboard that queries `made_up_metric_total` fails and names it. A frozen ledger whose requests hang fails the dependency item. A naive pool that did not fail means the next bottleneck was never found.

```python
def test_accepted_risks_carry_an_owner_and_the_condition_that_reopens_them():
    risk = {
        "area": "No autoscaling",
        "check": "static fleet",
        "evidence": "steady load",
        "owner": "capacity owner",
        "revisit_when": "growth passes 2x",
    }
    items, decision = review(config=Config([risk]))
    accepted = next(item for item in items if item.verdict is Verdict.ACCEPTED_RISK)
    assert (accepted.owner, accepted.revisit_when) == ("capacity owner", "growth passes 2x")
    assert decision is Decision.LAUNCH

```

An accepted risk keeps its owner and the condition that reopens it and does not change a LAUNCH decision.

```python
class TestReviewItemRules:
    def test_a_verdict_without_evidence_is_refused(self):
        with pytest.raises(InvalidReviewError):
            ReviewItem("a", "c", Verdict.PASS, " ", "owner")

    def test_a_fail_without_a_date_is_refused(self):
        with pytest.raises(InvalidReviewError):
            ReviewItem("a", "c", Verdict.FAIL, "broken", "owner")

    def test_a_fail_without_an_owner_is_refused(self):
        with pytest.raises(InvalidReviewError):
            ReviewItem("a", "c", Verdict.FAIL, "broken", " ", due=DUE)

    def test_an_accepted_risk_without_a_revisit_condition_is_refused(self):
        with pytest.raises(InvalidReviewError):
            ReviewItem("a", "c", Verdict.ACCEPTED_RISK, "known", "owner")

    def test_the_decision_is_one_of_exactly_three_values(self):
        assert {decision.value for decision in Decision} == {"LAUNCH", "LAUNCH WITH CONDITIONS", "DON'T LAUNCH YET"}
        assert {verdict.value for verdict in Verdict} == {"PASS", "FAIL", "ACCEPTED RISK"}

    def test_no_items_is_a_launch(self):
        assert decide([]) is Decision.LAUNCH

```

`TestReviewItemRules`: a verdict without evidence, a failure without a date or without an owner, and an accepted risk without a revisit condition are all refused with `InvalidReviewError`. The decision has exactly three values and the verdict exactly three. No items is a LAUNCH.

```python
class TestDecisionDocument:
    def test_every_blocker_has_its_owner_and_date_on_the_same_line(self):
        evidence = {k: v for k, v in GOOD_EVIDENCE.items() if k != "restore"}
        items, decision = review(evidence=evidence)
        text = launch_decision.markdown(items, decision)
        line = next(line for line in text.splitlines() if line.startswith("- Backup and restore"))
        assert "Owner: owner role, by 2026-11-03" in line

    def test_the_console_summary_ends_in_the_decision_and_its_conditions(self):
        evidence = {k: v for k, v in GOOD_EVIDENCE.items() if k != "restore"}
        text = launch_decision.console(*review(evidence=evidence))
        assert "Backup and restore" in text
        assert "FAIL" in text
        assert "Decision: LAUNCH WITH CONDITIONS" in text

    def test_the_document_fits_on_one_page(self):
        items, decision = review()
        assert len(launch_decision.markdown(items, decision).splitlines()) <= 50


def test_metric_names_ignore_functions_labels_and_numbers():
    expression = (
        'histogram_quantile(0.99, sum by (le) (rate(envoy_http_downstream_rq_time_bucket{le="50"}[5m]))) > 0.05'
    )
    assert _metric_names(expression) == {"envoy_http_downstream_rq_time_bucket"}


def test_the_real_dashboard_in_the_repository_only_queries_exported_metrics(root):
    from capacity.infrastructure.project_files import FileSystemProject

    files = FileSystemProject(root)
    reviewer = Reviewer(files, Evidence({}), Slos(), Config())
    passed, evidence = reviewer._dashboard_check()
    assert passed, evidence

```

`TestDecisionDocument`: every blocker's owner and date are on the same line of the document; the console summary ends with the decision and its conditions; the document fits on one page (50 lines or fewer). `test_metric_names_ignore_functions_labels_and_numbers` checks that only the metric name is extracted from a `histogram_quantile` expression, and the last test runs the dashboard check on the **real** dashboard in the repository.

```python
class TestMoreWaysToFail:
    def test_a_missing_rollout_test_fails_the_rollout_item(self):
        evidence = {k: v for k, v in GOOD_EVIDENCE.items() if k != "rollout"}
        items, _ = review(evidence=evidence)
        rollout = next(item for item in items if item.area == "Safe rollout and rollback")
        assert rollout.verdict is Verdict.FAIL
        assert "make rollout-test" in rollout.evidence

    def test_a_rollout_that_cost_requests_fails(self):
        evidence = {**GOOD_EVIDENCE, "rollout": {**GOOD_EVIDENCE["rollout"], "within_slo": False}}
        assert verdicts(review(evidence=evidence)[0])["Safe rollout and rollback"] is Verdict.FAIL

    def test_a_missing_dependency_test_fails(self):
        evidence = {k: v for k, v in GOOD_EVIDENCE.items() if k != "dependency"}
        assert verdicts(review(evidence=evidence)[0])["Dependency failure behavior"] is Verdict.FAIL

    def test_a_missing_ownership_document_fails(self):
        files = {k: v for k, v in GOOD_FILES.items() if k != "docs/ownership.md"}
        assert verdicts(review(files=files)[0])["On-call ownership"] is Verdict.FAIL

    def test_an_ownership_document_without_a_secondary_or_an_escalation_path_fails(self):
        files = {**GOOD_FILES, "docs/ownership.md": "Owner. Primary on-call. Runbooks."}
        items, _ = review(files=files)
        ownership = next(item for item in items if item.area == "On-call ownership")
        assert ownership.verdict is Verdict.FAIL
        assert "secondary" in ownership.evidence
        assert "escalation" in ownership.evidence

    def test_missing_capacity_evidence_names_what_is_missing(self):
        evidence = {k: v for k, v in GOOD_EVIDENCE.items() if k != "soak"}
        items, _ = review(evidence=evidence)
        capacity = next(item for item in items if item.area == "Capacity")
        assert capacity.verdict is Verdict.FAIL
        assert "soak" in capacity.evidence

    def test_a_zone_loss_run_that_left_the_slo_fails_the_capacity_item(self):
        evidence = {**GOOD_EVIDENCE, "fleet-zone-loss": {**GOOD_EVIDENCE["fleet-zone-loss"], "within_slo": False}}
        items, _ = review(evidence=evidence)
        capacity = next(item for item in items if item.area == "Capacity")
        assert "lost zone" in capacity.evidence or "minus a zone" in capacity.evidence

    def test_a_load_test_that_found_no_knee_fails_the_capacity_item(self):
        evidence = {**GOOD_EVIDENCE, "knee": {"knee": None, "safe_rps": None}}
        assert verdicts(review(evidence=evidence)[0])["Capacity"] is Verdict.FAIL

    def test_no_dashboard_fails(self):
        files = {k: v for k, v in GOOD_FILES.items() if not k.startswith("dashboards/")}
        assert verdicts(review(files=files)[0])["Dashboards"] is Verdict.FAIL

    def test_missing_promtool_replays_fail_the_slo_item(self):
        files = {k: v for k, v in GOOD_FILES.items() if k != "slo/tests/fleet.test.yml"}
        assert verdicts(review(files=files)[0])["SLOs and burn-rate alerts"] is Verdict.FAIL

    def test_the_console_summary_of_a_clean_review_has_no_conditions(self):
        text = launch_decision.console(*review())
        assert "Decision: LAUNCH" in text
        assert "Owner:" not in text
```

`TestMoreWaysToFail` is one test per remaining way a check fails: a missing rollout test (the message says `make rollout-test`), a rollout that cost requests, a missing dependency test, a missing ownership document or one without a secondary or an escalation path, missing capacity evidence (naming the soak), a zone-loss run that left the SLO, a load test that found no knee, no dashboard, a missing promtool replay, and a clean review whose console has no conditions.

### `tests/unit/test_slo_domain.py`

```python
import pytest

from capacity.domain.errors import InvalidSloError
from capacity.domain.slo import DEFAULT_ALERTS, BurnAlert, SliKind, Slo, budget_remaining_pct, burn_rate


def availability(target: float = 0.9995) -> Slo:
    return Slo("a", "follow_link", SliKind.AVAILABILITY, target, "all", "below 500", "because")


def latency(threshold_ms: float = 100) -> Slo:
    return Slo("l", "follow_link", SliKind.LATENCY, 0.95, "ok", "fast", "because", threshold_ms)

```

Two helpers build SLOs: `availability(target)` (good means below 500) and `latency(threshold_ms)` (a 95% target). They use the journey name `follow_link`; any journey name would do.

```python
class TestBudgetArithmetic:
    def test_follow_link_budget_is_about_twenty_minutes_over_28_days(self):
        assert availability(0.9995).budget_minutes == pytest.approx(20.16)

    def test_halving_the_allowed_failures_halves_the_minutes(self):
        assert availability(0.9990).budget_minutes == pytest.approx(2 * availability(0.9995).budget_minutes)

    def test_budget_in_requests_scales_with_valid_volume(self):
        assert availability(0.9995).budget_requests(2_000_000) == pytest.approx(1000)

    def test_burn_rate_one_spends_exactly_the_budget(self):
        assert burn_rate(bad=1, valid=2000, allowed_bad_fraction=0.0005) == pytest.approx(1.0)

    def test_a_full_outage_on_a_99_95_slo_burns_two_thousand_times_too_fast(self):
        assert burn_rate(bad=100, valid=100, allowed_bad_fraction=0.0005) == pytest.approx(2000)

    def test_no_traffic_burns_nothing(self):
        assert burn_rate(0, 0, 0.001) == 0.0

    def test_remaining_budget_after_half_is_spent(self):
        assert budget_remaining_pct(bad=500, valid=1_000_000, allowed_bad_fraction=0.001) == pytest.approx(50.0)

```

`TestBudgetArithmetic`: a 99.95% target allows 0.05% bad over 28 days, which is 20.16 minutes; halving the allowed failures halves the minutes (99.9% against 99.95% doubles it); the budget in requests scales with volume (2,000,000 valid requests allow 1,000 bad); a burn rate of 1 spends exactly the budget; a full outage on a 99.95% SLO burns 2,000 times too fast (1 over 0.0005); no traffic burns nothing; and after 500 bad in a million at a 99.9% target half of the budget is left (50%). `pytest.approx` compares floating-point numbers with a small tolerance.

```python
class TestJudgement:
    def test_a_correct_404_is_a_good_availability_event(self):
        assert availability().judge(404, 5.0) == (True, True)

    def test_a_503_is_valid_and_bad(self):
        assert availability().judge(503, 5.0) == (True, False)

    def test_a_slow_success_is_a_bad_latency_event(self):
        assert latency(100).judge(302, 150.0) == (True, False)

    def test_a_fast_success_is_a_good_latency_event(self):
        assert latency(100).judge(302, 20.0) == (True, True)

    def test_a_failed_request_is_not_a_valid_latency_event(self):
        assert latency(100).judge(503, 2000.0) == (False, False)

```

`TestJudgement`: `judge(status, latency)` returns `(valid, good)`. A correct 404 is a good availability event, a 503 is valid and bad, a slow success is a bad latency event, a fast success a good one, and a failed request is not a valid latency event at all (the availability SLO already counts it).

```python
class TestSloValidation:
    @pytest.mark.parametrize("target", [0.0, 1.0, 1.5, -0.1])
    def test_a_target_outside_zero_and_one_is_rejected(self, target):
        with pytest.raises(InvalidSloError):
            availability(target)

    def test_a_latency_slo_needs_a_threshold(self):
        with pytest.raises(InvalidSloError):
            Slo("l", "follow_link", SliKind.LATENCY, 0.95, "v", "g", "w")

    def test_an_availability_slo_rejects_a_threshold(self):
        with pytest.raises(InvalidSloError):
            Slo("a", "follow_link", SliKind.AVAILABILITY, 0.99, "v", "g", "w", 100)

    def test_every_slo_must_say_what_is_valid_and_good(self):
        with pytest.raises(InvalidSloError):
            Slo("a", "follow_link", SliKind.AVAILABILITY, 0.99, " ", "g", "w")

```

`TestSloValidation`: a target of 0, 1, 1.5 or -0.1 is refused; a latency SLO needs a threshold; an availability SLO rejects one; and an SLO with a blank definition of valid events is refused, so a broken definition fails when loaded.

```python
class TestBurnAlerts:
    def test_default_alerts_spend_two_five_and_ten_percent_of_the_budget(self):
        shares = [round(alert.budget_consumed_fraction() * 100, 1) for alert in DEFAULT_ALERTS]
        assert shares == [2.1, 5.4, 10.7]

    def test_default_alerts_page_twice_and_ticket_once(self):
        assert [alert.severity for alert in DEFAULT_ALERTS] == ["page", "page", "ticket"]

    def test_the_short_window_must_be_shorter(self):
        with pytest.raises(InvalidSloError):
            BurnAlert("x", "page", 2, 5, 5)

    def test_severity_is_page_or_ticket(self):
        with pytest.raises(InvalidSloError):
            BurnAlert("x", "email", 2, 60, 5)
```

`TestBurnAlerts`: the three default alerts spend 2.1%, 5.4% and 10.7% of the budget in their long windows; they page twice and open a ticket once; the short window must be shorter than the long one; and the severity must be `page` or `ticket`.

### `tests/unit/test_sli_and_alerting.py`

```python
import pytest

from capacity.application import alerting
from capacity.application.sli import LatencyHistogram, MinuteTally, SliAccumulator, worst_remaining
from capacity.domain.requests import RequestEvent
from capacity.domain.slo import DEFAULT_ALERTS, SliKind, Slo

FOLLOW = Slo("follow_availability", "follow_link", SliKind.AVAILABILITY, 0.9995, "all", "below 500", "why")
LIST = Slo("list_availability", "list_links", SliKind.AVAILABILITY, 0.999, "all", "below 500", "why")
FOLLOW_FAST = Slo("follow_latency", "follow_link", SliKind.LATENCY, 0.95, "ok", "under 100", "why", 100)
MINUTES = 28 * 1440


def tally_from(
    per_minute_valid: int, bad_by_minute: dict[int, int] | None = None, minutes: int = MINUTES
) -> MinuteTally:
    """Replay data: a constant request rate with a chosen number of failures in chosen minutes."""
    tally = MinuteTally(minutes)
    bad_by_minute = bad_by_minute or {}
    for minute in range(minutes):
        bad = bad_by_minute.get(minute, 0)
        tally.valid[minute] = per_minute_valid
        tally.good[minute] = per_minute_valid - bad
    return tally
```

Three SLOs (two availability, one latency under 100 ms) and `MINUTES`, the minutes in 28 days. `tally_from(per_minute_valid, bad_by_minute, minutes)` builds replay data directly: a constant request rate with a chosen number of failures in chosen minutes, by writing into the tally's arrays.

```python
class TestAccumulator:
    def test_counts_good_over_valid_exactly(self):
        accumulator = SliAccumulator([FOLLOW])
        for status in [302] * 997 + [404] + [503] * 2:
            accumulator.add(RequestEvent("follow_link", status, 10.0, 5.5))
        report = accumulator.reports()["follow_availability"]
        assert (report.valid, report.good) == (1000, 998)
        assert report.sli == pytest.approx(0.998)

    def test_burn_rate_and_remaining_budget_follow_from_the_counts(self):
        accumulator = SliAccumulator([FOLLOW])
        for index in range(10_000):
            accumulator.add(RequestEvent("follow_link", 503 if index < 2 else 302, 10.0, index % 1000))
        report = accumulator.reports()["follow_availability"]
        assert report.burn_rate == pytest.approx(0.4)
        assert report.budget_remaining_pct == pytest.approx(60.0)

    def test_events_of_other_journeys_do_not_count(self):
        accumulator = SliAccumulator([FOLLOW])
        accumulator.add(RequestEvent("create_link", 503, 10.0, 1.0))
        assert accumulator.reports()["follow_availability"].valid == 0

    def test_events_outside_the_window_are_ignored(self):
        accumulator = SliAccumulator([FOLLOW])
        accumulator.add(RequestEvent("follow_link", 302, 10.0, MINUTES + 5.0))
        assert accumulator.reports()["follow_availability"].valid == 0

    def test_latency_slo_counts_only_successful_requests(self):
        accumulator = SliAccumulator([FOLLOW_FAST])
        accumulator.consume(
            [
                RequestEvent("follow_link", 302, 20.0, 1.0),
                RequestEvent("follow_link", 302, 250.0, 1.0),
                RequestEvent("follow_link", 503, 2000.0, 1.0),
            ]
        )
        report = accumulator.reports()["follow_latency"]
        assert (report.valid, report.good) == (2, 1)

    def test_the_worst_budget_decides_the_policy_level(self):
        accumulator = SliAccumulator([FOLLOW, LIST])
        accumulator.consume(
            [RequestEvent("list_links", 503, 5.0, 1.0)] * 3 + [RequestEvent("follow_link", 302, 5.0, 1.0)] * 1000
        )
        assert worst_remaining(accumulator.reports()) < 0

```

`TestAccumulator`: 1,000 requests with one 404 and two 503s count 1,000 valid and 998 good (a 404 is good), so the SLI is 0.998. Two failures in 10,000 requests burn at 0.4 and leave 60% of the budget. Events of other journeys and events beyond the window are ignored. A latency SLO counts only successful requests (two valid, one good). With two SLOs tracked, `worst_remaining` is the lowest budget left, which here is negative: three failures in a journey that allows almost none.

```python
class TestLatencyHistogram:
    def test_p95_is_within_one_percent(self):
        histogram = LatencyHistogram()
        for value in range(1, 1001):
            histogram.add(float(value))
        assert histogram.percentile(0.95) == pytest.approx(950, rel=0.02)

    def test_an_empty_histogram_reports_zero(self):
        assert LatencyHistogram().percentile(0.95) == 0.0

```

`TestLatencyHistogram`: for the values 1 to 1,000 the p95 from the buckets is within 2% of 950, and an empty histogram reports 0.

```python
class TestReplayedAlerts:
    """The same shapes as slo/tests/burn_rate.test.yml, replayed through the Python evaluator."""

    def test_healthy_traffic_fires_nothing(self):
        assert alerting.evaluate(FOLLOW, tally_from(50), DEFAULT_ALERTS) == []

    def test_a_ten_minute_full_outage_pages_on_the_fast_burn_alert(self):
        start = 19 * 1440 + 14 * 60
        tally = tally_from(50, dict.fromkeys(range(start, start + 10), 50))
        firings = {firing.alert: firing for firing in alerting.evaluate(FOLLOW, tally, DEFAULT_ALERTS)}
        assert firings["fast_burn"].severity == "page"
        assert firings["fast_burn"].first_minute == start
        assert alerting.verdict(list(firings.values())) == "PAGE"

    def test_the_fast_alert_clears_quickly_because_the_short_window_recovers(self):
        start = 5000
        tally = tally_from(50, dict.fromkeys(range(start, start + 10), 50))
        fast = next(f for f in alerting.evaluate(FOLLOW, tally, DEFAULT_ALERTS) if f.alert == "fast_burn")
        assert fast.firing_minutes <= 10 + 5

    def test_a_two_minute_spike_of_ten_percent_errors_pages_nobody(self):
        tally = tally_from(50, {5000: 5, 5001: 5})
        assert alerting.evaluate(FOLLOW, tally, DEFAULT_ALERTS) == []

    def test_a_steady_half_percent_error_rate_opens_a_ticket_and_never_pages(self):
        tally = tally_from(1000, dict.fromkeys(range(MINUTES), 5))
        firings = alerting.evaluate(LIST, tally, DEFAULT_ALERTS)
        assert {firing.alert for firing in firings} == {"budget_leak"}
        assert alerting.verdict(firings) == "TICKET"

    def test_a_window_with_no_traffic_burns_nothing(self):
        assert alerting.evaluate(FOLLOW, tally_from(0), DEFAULT_ALERTS) == []

    def test_verdict_of_nothing_is_none(self):
        assert alerting.verdict([]) == "NONE"
```

`TestReplayedAlerts` replays the same shapes as the promtool tests through the Python evaluator. Healthy traffic fires nothing. A ten-minute full outage pages on the fast burn alert, first firing at the first minute of the outage. The fast alert clears quickly because the short window recovers (it fires for at most 15 minutes). A two-minute spike of 10% errors pages nobody because the long window does not hold enough bad events. A steady half-percent error rate over the whole window opens only the budget-leak ticket and never pages. A window with no traffic burns nothing, and the verdict of nothing is `NONE`.

### `tests/unit/test_settings_and_rules.py`

```python
import pytest
import yaml

from capacity.application import rules
from capacity.application.experiments import fleet_instances, spare_instances
from capacity.domain.cpu import cpu_work
from capacity.domain.latency import Sample
from capacity.infrastructure.settings import InstanceSettings, LedgerSettings, SettingsError
from capacity.infrastructure.slo_files import render_fleet_files, render_rule_files


class TestSettings:
    def test_an_instance_has_defaults_and_no_dependency_unless_told(self):
        settings = InstanceSettings.from_env({})
        assert (settings.zone, settings.version, settings.work_rounds, settings.ledger_url) == ("a", "v1", 3000, None)

    def test_the_ledger_url_loses_its_trailing_slash(self):
        assert InstanceSettings.from_env({"LEDGER_URL": "http://ledger:8082/"}).ledger_url == "http://ledger:8082"

    def test_the_ledger_needs_a_database_url(self):
        with pytest.raises(SettingsError, match="DATABASE_URL"):
            LedgerSettings.from_env({})

    @pytest.mark.parametrize(
        ("name", "value"), [("POOL_SIZE", "0"), ("POOL_SIZE", "200"), ("HOLD_MS", "-1"), ("POOL_TIMEOUT_S", "x")]
    )
    def test_bad_ledger_values_are_refused_by_name(self, name, value):
        with pytest.raises(SettingsError, match=name):
            LedgerSettings.from_env({"DATABASE_URL": "postgresql://u:p@db/x", name: value})

    def test_bad_instance_values_are_refused(self):
        with pytest.raises(SettingsError, match="WORK_ROUNDS"):
            InstanceSettings.from_env({"WORK_ROUNDS": "0"})

```

`TestSettings`: an instance with an empty environment has the defaults (zone `a`, version `v1`, 3,000 rounds, the program's own default; compose sets 6,000) and no ledger; the ledger URL loses a trailing slash; the ledger needs `DATABASE_URL`; bad ledger values (`POOL_SIZE` of 0 or 200, a negative `HOLD_MS`, a non-number `POOL_TIMEOUT_S`) are refused with an error that names the variable (`match=name`); and a `WORK_ROUNDS` of 0 is refused for an instance.

```python
class TestCpuWork:
    def test_the_same_rounds_give_the_same_digest(self):
        assert cpu_work(50) == cpu_work(50)

    def test_more_rounds_change_the_digest(self):
        assert cpu_work(50) != cpu_work(51)

    def test_it_is_a_sha256_hex_digest(self):
        assert len(cpu_work(10)) == 64

```

`TestCpuWork`: the same rounds give the same digest, different rounds a different one, and the digest is a 64-character SHA-256 hex string.

```python
class TestRules:
    def test_expressions_read_the_envoy_edge_not_the_instances(self, spec):
        availability = rules.error_ratio_expr(spec.slos[0], "5m")
        assert "envoy_http_downstream_rq_xx" in availability
        assert 'envoy_response_code_class="5"' in availability
        assert "app_requests_total" not in availability

    def test_the_latency_threshold_is_the_edge_bucket_at_fifty_milliseconds(self, spec):
        expression = rules.error_ratio_expr(spec.slos[1], "5m")
        assert 'le="50"' in expression
        assert "envoy_http_downstream_rq_time_count" in expression

    def test_a_threshold_that_is_not_an_edge_bucket_is_refused(self):
        with pytest.raises(ValueError, match="no histogram bucket"):
            rules.bucket_label(40)

    def test_the_committed_rule_files_are_what_the_generator_produces(self, spec, root):
        for path, text in render_rule_files(spec).items():
            assert (root / path).read_text() == text

    def test_the_blind_spot_alert_watches_the_edge_metric(self, spec):
        document = rules.build_rules(spec.slos, spec.alerts)
        absent = next(r for g in document["groups"] for r in g["rules"] if r.get("alert") == "EdgeMetricsAbsent")
        assert absent["expr"] == "absent(envoy_http_downstream_rq_xx)"


def test_sample_is_a_plain_value_object():
    assert Sample(1.0, 0.01, True) == Sample(1.0, 0.01, True)
```

`TestRules`: the rule expressions read the Envoy edge (`envoy_http_downstream_rq_xx`, response class 5), not the instances' own `app_requests_total`; the latency expression uses the edge bucket `le="50"` and the edge's request count; a threshold that is not a bucket boundary (40 ms) is refused with `ValueError`; the committed rule files are exactly what the generator produces; and the blind-spot alert is `absent(envoy_http_downstream_rq_xx)`. The last test shows `Sample` is a plain value object: two equal samples compare equal.

`TestFleetFiles` checks the generated fleet alert files and the spare-instance helpers:

```python
class TestFleetFiles:
    def test_the_alert_thresholds_and_the_replay_follow_the_plan(self):
        files = {path.name: text for path, text in render_fleet_files(3, 5, 3).items()}
        rules = yaml.safe_load(files["fleet.rules.yml"])["groups"][0]["rules"]
        assert [rule["expr"].rsplit("< ", 1)[1] for rule in rules] == ["3", "5"]
        replay = yaml.safe_load(files["fleet.test.yml"])
        assert replay["tests"][0]["input_series"][0]["values"] == "5x9 3x39 5x30"
        assert replay["tests"][1]["input_series"][0]["values"] == "5x9 2x39 5x30"

    def test_the_spares_are_the_instances_compose_defines_and_the_plan_does_not_run(self):
        assert fleet_instances((3, 3, 2))[-1] == "app-c2"
        assert spare_instances((3, 3, 2)) == ["app-c3"]
        assert len(spare_instances((2, 2, 1))) == 4
```

For a plan that needs 3 instances for the peak of 5 in all, the rule thresholds are `< 3` and `< 5`, and the promtool replay holds 5 for 9 samples, 3 for 39 and 5 for 30, and 2 (one fewer than needed) in the paging case. `fleet_instances((3, 3, 2))` ends at `app-c2`; the spare of that fleet is `app-c3`; a fleet of 2, 2 and 1 leaves four spares.

`TestLedgerWorkers` covers the worker split:

```python
class TestLedgerWorkers:
    def test_the_pool_is_split_between_workers_without_losing_a_connection(self):
        from capacity.interfaces.ledger_app import pool_share

        assert [pool_share(40, 2, index) for index in range(2)] == [20, 20]
        assert [pool_share(25, 2, index) for index in range(2)] == [13, 12]
        assert sum(pool_share(37, 3, index) for index in range(3)) == 37

    def test_the_ledger_runs_one_worker_unless_told_otherwise(self):
        base = {"DATABASE_URL": "postgresql://u:p@db/x"}
        assert LedgerSettings.from_env(base).workers == 1
        assert LedgerSettings.from_env({**base, "WORKERS": "2"}).workers == 2
        with pytest.raises(SettingsError, match="WORKERS"):
            LedgerSettings.from_env({**base, "WORKERS": "0"})
```

`pool_share` gives 20 and 20 for a pool of 40 over two workers, 13 and 12 for 25, and always sums to the total (37 over three). The ledger runs one worker unless `WORKERS` says otherwise, and `WORKERS=0` is refused with a message naming the variable.

### `tests/unit/test_evidence.py`

```python
"""The committed measurements and the documents, runbooks and alerts built from them must agree.

`make run` regenerates the evidence and the documents together. These tests do not judge whether the machine was fast
enough (that is `tests/measurements`, run by `make test-measurements`); they fail when the plan, the alert thresholds,
the runbooks or the launch decision no longer follow from the evidence that is committed beside them.
"""

from pathlib import Path

import pytest
import yaml

from capacity.application import launch_decision
from capacity.application.readiness import Reviewer
from capacity.domain.plan import plan_capacity
from capacity.infrastructure.project_files import FileSystemProject, JsonEvidence, SloFilesCatalog, YamlChecklist

SLO_P99_MS = 50.0

pytestmark = pytest.mark.measurements

```

The module docstring says what these tests are for: the committed measurements and the documents, runbooks and alerts built from them must agree. They do not judge whether the machine was fast enough (that is `tests/measurements`); they fail when the plan, the alert thresholds, the runbooks or the launch decision no longer follow from the evidence committed beside them. `pytestmark = pytest.mark.measurements` selects them with the measurement group.

```python
def test_step_three_the_plan_is_the_formula_applied_to_the_measured_safe_load(evidence):
    plan, knee = evidence("plan"), evidence("knee")
    recomputed = plan_capacity(plan["current_peak_rps"], plan["growth_factor"], knee["safe_rps"], zones=3)
    assert plan["future_peak_rps"] == 2000
    assert plan["instances_for_peak"] == recomputed.instances_for_peak
    assert plan["total_instances"] == recomputed.total_instances
    assert plan["distribution"] == list(recomputed.distribution)
    assert plan["surviving_after_zone_loss"] * knee["safe_rps"] >= plan["future_peak_rps"]


def test_the_fleet_alert_threshold_is_the_number_of_instances_the_plan_needs_to_survive(evidence, root):
    document = yaml.safe_load((root / "slo" / "alerts" / "fleet.rules.yml").read_text())
    rules = {rule["alert"]: rule for group in document["groups"] for rule in group["rules"]}
    plan = evidence("plan")
    assert f"< {plan['instances_for_peak']}" in rules["FleetBelowPeakCapacity"]["expr"]
    assert f"< {plan['total_instances']}" in rules["FleetBelowFullStrength"]["expr"]


def test_step_five_the_restore_was_executed_and_matches_the_source(evidence):
    restore = evidence("restore")
    assert restore["matched"] is True
    assert restore["rows"] == restore["source_rows"] > 0
    assert restore["restore_seconds"] <= restore["rto_target_seconds"]

```

The plan in the evidence equals the formula applied to the measured safe load (six-month peak 2,000, same instance counts, same distribution, survivors carrying the peak). The fleet alert thresholds in `slo/alerts/fleet.rules.yml` are the instance counts the plan says (`< instances_for_peak` and `< total_instances`). The restore was executed: it matched the source, had rows, and finished within the recovery target.

```python
def test_step_six_the_decision_document_is_what_the_review_produces_from_the_evidence(root):
    reviewer = Reviewer(
        FileSystemProject(root),
        JsonEvidence(root / "docs" / "evidence"),
        SloFilesCatalog(root),
        YamlChecklist(root / "readiness" / "checklist.yaml"),
    )
    items, decision = reviewer.review()
    assert (root / "docs" / "launch-decision.md").read_text() == launch_decision.markdown(items, decision)


def test_the_decision_fits_on_one_page_and_every_blocker_has_an_owner_and_a_date(root):
    text = (root / "docs" / "launch-decision.md").read_text()
    assert len(text.splitlines()) <= 50
    decision_line = next(line for line in text.splitlines() if line.startswith("## Decision:"))
    assert decision_line.removeprefix("## Decision: ") in {"LAUNCH", "LAUNCH WITH CONDITIONS", "DON'T LAUNCH YET"}
    for line in text.splitlines():
        if line.startswith("- ") and "Owner:" in line:
            assert ", by 20" in line


@pytest.mark.parametrize("name", ["cpu-knee", "pool-exhaustion", "zone-loss"])
def test_each_runbook_for_a_failure_the_tests_found_has_symptoms_mitigation_and_diagnosis(root, name):
    text = (root / "docs" / "runbooks" / f"{name}.md").read_text().lower()
    assert all(heading in text for heading in ("symptoms", "mitigate", "diagnose"))


def test_the_cpu_runbook_quotes_the_measured_knee_and_safe_load(evidence, root):
    knee = evidence("knee")
    text = (Path(root) / "docs" / "runbooks" / "cpu-knee.md").read_text()
    assert f"{knee['knee']['knee_rps']:g}" in text
    assert str(knee["safe_rps"]) in text
```

The launch decision document equals what the review produces from the committed evidence, so it cannot be edited by hand. It fits on one page, its decision line is one of the three decisions and every line with an owner has a date (`", by 20"`). Each of the three runbooks has symptoms, mitigation and diagnosis, and the CPU runbook quotes the measured knee and safe load.

### `tests/integration/test_instance_and_loadgen.py`

```python
"""An instance, a stand-in ledger and the open-loop generator over real sockets, in one process."""

import asyncio
import time

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from capacity.application.loadtest import knee_test, timed_run
from capacity.domain.cpu import cpu_work
from capacity.infrastructure.metrics import InstanceMetrics
from capacity.infrastructure.openloop import MetricsCpuProbe, RawOpenLoopRunner
from capacity.infrastructure.ops import build_ops_app
from capacity.infrastructure.settings import InstanceSettings
from capacity.interfaces import instance_app

pytestmark = pytest.mark.integration


def settings(ledger_url=None, rounds=200, timeout=0.3):
    return InstanceSettings("b", "b2", "v7", rounds, ledger_url, timeout, 0, 0, "INFO")


async def start(app):
    server = TestServer(app)
    await server.start_server()
    return server


@pytest.fixture
async def instance():
    metrics = InstanceMetrics("b", "b2")
    server = await start(instance_app.build_app(settings(), metrics))
    ops = await start(build_ops_app(metrics.render))
    yield server, ops, metrics
    await server.close()
    await ops.close()


async def get(url):
    async with aiohttp.ClientSession() as session, session.get(url) as response:
        return response.status, dict(response.headers), await response.text()

```

Integration tests use real sockets inside the test process: aiohttp's `TestServer` starts an application on a free port, so no container is needed. `settings(...)` builds instance settings by hand (ports of 0 mean "not used"). `start(app)` starts a test server. The async `instance` **fixture** builds an instance and its ops app, hands them to the test with `yield` and closes them afterwards. `get(url)` fetches a URL and returns the status, headers and body.

```python
async def test_an_instance_answers_with_its_zone_instance_and_version(instance):
    server, _, _ = instance
    status, headers, body = await get(f"http://127.0.0.1:{server.port}/work")
    assert status == 200
    assert (headers["X-Zone"], headers["X-Instance"], headers["X-Version"]) == ("b", "b2", "v7")
    assert '"digest"' in body


async def test_each_request_does_real_cpu_work_that_shows_up_in_the_process_cpu_counter(instance):
    server, ops, _ = instance
    probe = MetricsCpuProbe("127.0.0.1", ops.port)
    before = await probe.cpu_seconds()
    for _ in range(40):
        await get(f"http://127.0.0.1:{server.port}/work")
    assert await probe.cpu_seconds() > before


async def test_the_instance_exports_request_and_latency_metrics(instance):
    server, ops, _ = instance
    await get(f"http://127.0.0.1:{server.port}/work")
    _, _, text = await get(f"http://127.0.0.1:{ops.port}/metrics")
    assert 'app_requests_total{code="200"} 1.0' in text
    assert "app_request_duration_seconds_bucket" in text
    assert "app_inflight_requests" in text

```

An instance answers with its zone, instance and version headers and a digest; each request does real CPU work that shows up in the process CPU counter read through `MetricsCpuProbe` (the counter after 40 requests is greater than before); and the instance exports the request counter (`code="200"` is 1.0 after one request), the duration histogram and the in-flight gauge.

```python
class FakeLedger:
    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = 0

    async def handler(self, request):
        self.calls += 1
        if self.behavior == "hang":
            await asyncio.sleep(30)
        if self.behavior == "error":
            return web.json_response({"error": "busy"}, status=503)
        return web.json_response({"id": 1})

    def app(self):
        app = web.Application()
        app.router.add_post("/entries", self.handler)
        return app


async def with_ledger(behavior):
    ledger = FakeLedger(behavior)
    ledger_server = await start(ledger.app())
    metrics = InstanceMetrics("b", "b2")
    server = await start(instance_app.build_app(settings(f"http://127.0.0.1:{ledger_server.port}"), metrics))
    return ledger, ledger_server, server

```

`FakeLedger` is a stand-in ledger with three behaviors: answer, answer 503, or hang for 30 seconds. `with_ledger(behavior)` starts the fake ledger and an instance pointed at it.

```python
async def test_a_healthy_ledger_is_called_once_per_request():
    ledger, ledger_server, server = await with_ledger("ok")
    try:
        assert (await get(f"http://127.0.0.1:{server.port}/work"))[0] == 200
        assert ledger.calls == 1
    finally:
        await server.close()
        await ledger_server.close()


async def test_a_ledger_that_answers_503_becomes_a_503_not_a_hang():
    _, ledger_server, server = await with_ledger("error")
    try:
        status, _, body = await get(f"http://127.0.0.1:{server.port}/work")
        assert status == 503
        assert "ledger answered 503" in body
    finally:
        await server.close()
        await ledger_server.close()


async def test_a_frozen_ledger_fails_the_request_within_the_timeout():
    _, ledger_server, server = await with_ledger("hang")
    try:
        started = time.perf_counter()
        status, _, _ = await get(f"http://127.0.0.1:{server.port}/work")
        elapsed = time.perf_counter() - started
        assert status == 503
        assert 0.25 <= elapsed < 0.8
    finally:
        await server.close()
        await ledger_server.close()


async def test_a_dead_ledger_fails_the_request_fast():
    _, ledger_server, server = await with_ledger("ok")
    await ledger_server.close()
    try:
        started = time.perf_counter()
        status, _, body = await get(f"http://127.0.0.1:{server.port}/work")
        assert status == 503
        assert "ledger unavailable" in body
        assert time.perf_counter() - started < 0.3
    finally:
        await server.close()

```

A healthy ledger is called once per request. A ledger that answers 503 becomes a 503 from the instance whose body says `ledger answered 503`. A frozen (hanging) ledger fails the request within the timeout: between 0.25 and 0.8 seconds with a 0.3 second timeout, not 30. A dead ledger (closed server) fails fast with `ledger unavailable`, in under 0.3 seconds. Each test closes both servers in a `finally`.

```python
class TestOpenLoopRunner:
    async def test_it_sends_the_scheduled_number_of_requests_and_counts_them_all(self, instance):
        server, _, _ = instance
        runner = RawOpenLoopRunner("127.0.0.1", server.port)
        samples, lag = await runner.run(rps=200, duration_s=1.0)
        await runner.close()
        assert len(samples) == 200
        assert all(sample.ok for sample in samples)
        assert lag >= 0

    async def test_latency_includes_the_time_a_request_waited_behind_a_busy_server(self):
        """A server that handles one request at a time makes later arrivals queue; an open-loop generator sees it."""
        lock = asyncio.Lock()

        async def slow(_):
            async with lock:
                await asyncio.sleep(0.02)
            return web.json_response({})

        app = web.Application()
        app.router.add_get("/work", slow)
        server = await start(app)
        runner = RawOpenLoopRunner("127.0.0.1", server.port)
        try:
            samples, _ = await runner.run(rps=100, duration_s=1.0)  # 100 rps into a 50 rps server
        finally:
            await runner.close()
            await server.close()
        slowest = max(sample.latency_s for sample in samples)
        assert slowest > 0.3

    async def test_a_dead_port_is_all_errors_not_an_exception(self):
        runner = RawOpenLoopRunner("127.0.0.1", 9, timeout_s=0.5)
        samples, _ = await runner.run(rps=20, duration_s=0.5)
        assert samples
        assert not any(sample.ok for sample in samples)

    async def test_the_knee_test_runs_end_to_end_against_a_real_instance(self, instance):
        server, ops, _ = instance
        runner = RawOpenLoopRunner("127.0.0.1", server.port)
        report = await knee_test(runner, MetricsCpuProbe("127.0.0.1", ops.port), [20, 40, 60, 80], step_seconds=0.5)
        await runner.close()
        assert len(report.steps) == 4
        assert all(step.requests > 0 for step in report.steps)

    async def test_a_timed_run_reports_windows(self, instance):
        server, _, _ = instance
        runner = RawOpenLoopRunner("127.0.0.1", server.port)
        report = await timed_run(runner, "soak", 100, 2.0, slo_p99_ms=1000, window_s=1.0)
        await runner.close()
        assert len(report.window_p99_ms) == 2
        assert report.within_slo


def test_cpu_work_costs_real_time_that_grows_with_the_rounds():
    started = time.perf_counter()
    cpu_work(2000)
    small = time.perf_counter() - started
    started = time.perf_counter()
    cpu_work(40_000)
    assert time.perf_counter() - started > small
```

`TestOpenLoopRunner`: at 200 requests a second for one second the runner sends exactly 200 requests, all succeed, and the generator lag is not negative. The queuing test is the key one: a server that handles one request at a time (an `asyncio.Lock` around a 20 ms sleep) can serve 50 a second, so 100 a second makes later arrivals queue, and because latency runs from the scheduled arrival the slowest request is over 0.3 seconds; a closed-loop generator would not show it. A dead port gives errors, not an exception. The knee test runs end to end against a real instance (four steps, each with requests), and a timed run reports two windows within a generous SLO. The last test shows `cpu_work(40_000)` takes longer than `cpu_work(2000)`.

Two more tests cover the drain and the backlog:

```python
async def test_a_service_that_never_answers_cannot_swamp_the_generator():
    """Past the backlog, scheduled requests count as failed at once instead of piling up as tasks."""

    async def never(_):
        await asyncio.sleep(30)

    app = web.Application()
    app.router.add_get("/work", never)
    server = await start(app)
    runner = RawOpenLoopRunner("127.0.0.1", server.port, max_connections=2, timeout_s=1.0)
    try:
        samples, _ = await runner.run(rps=200, duration_s=0.5)
    finally:
        await runner.close()
        await server.close()
    assert len(samples) == 100
    assert not any(sample.ok for sample in samples)
    assert sum(sample.latency_s < 0.1 for sample in samples) > 80  # shed at once, not after the 1 second timeout
```

A server that never answers, with `max_connections=2` (so a backlog of 4), receives 200 requests a second for half a second: 100 are scheduled, none succeed, and more than 80 of them fail at once (latency under 0.1 s) instead of after the 1 second timeout, so the generator never piles up tasks.

```python
async def test_a_draining_instance_says_so_on_its_health_check_and_keeps_serving():
    """On a stop the process first fails /healthz, so the load balancer drains it, and only then closes."""
    draining = {"now": False}
    metrics = InstanceMetrics("b", "b2")
    server = await start(instance_app.build_app(settings(), metrics))
    ops = await start(build_ops_app(metrics.render, is_draining=lambda: draining["now"]))
    try:
        assert (await get(f"http://127.0.0.1:{ops.port}/healthz"))[0] == 200
        draining["now"] = True
        status, _, body = await get(f"http://127.0.0.1:{ops.port}/healthz")
        assert (status, '"draining"' in body) == (503, True)
        assert (await get(f"http://127.0.0.1:{server.port}/work"))[0] == 200
    finally:
        await server.close()
        await ops.close()
```

With a mutable flag standing in for the process state, `/healthz` answers 200 before and 503 `draining` after the flag flips, while `/work` still answers 200 (a draining instance keeps serving). The next test calls `serve` with a stop event and a 0.2 second drain, and checks that `on_drain` ran once and that stopping took at least the drain time.

The drain test of `serve` in full:

```python
async def test_serve_marks_the_process_draining_before_it_stops():
    from capacity.infrastructure.http_runtime import serve

    events: list[str] = []
    stop = asyncio.Event()
    app = web.Application()
    task = asyncio.create_task(
        serve([(app, 0)], stop=stop, drain_s=0.2, on_drain=lambda: events.append("drain"), host="127.0.0.1")
    )
    await asyncio.sleep(0.2)
    started = time.perf_counter()
    stop.set()
    await task
    assert events == ["drain"]
    assert time.perf_counter() - started >= 0.2
```

`serve` runs as a task with port 0 (any free port) and a stop event. After a short wait the test records the time, sets the event, waits for `serve` to return, and checks the order of events and that at least the 0.2 second drain passed.

### `tests/integration/test_cli_and_adapters.py`

```python
"""The command-line programs and the file adapters, against real files and sockets."""

import asyncio
import json
import shutil
import subprocess
import time
from datetime import date

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from capacity.infrastructure import compose
from capacity.infrastructure.metrics import InstanceMetrics
from capacity.infrastructure.ops import build_ops_app
from capacity.infrastructure.project_files import FileSystemProject, JsonEvidence, SloFilesCatalog, YamlChecklist
from capacity.infrastructure.settings import InstanceSettings
from capacity.interfaces import capacity_cli, instance_app, loadtest_cli, slo_cli

pytestmark = pytest.mark.integration

```

The docstring: the command-line programs and the file adapters, against real files and sockets. The imports bring in the adapters, the Compose helper and the entry points. `pytestmark` marks every test `integration`.

```python
class TestAdapters:
    def test_evidence_round_trips_and_a_missing_experiment_is_none(self, tmp_path):
        evidence = JsonEvidence(tmp_path / "evidence")
        assert evidence.load("knee") is None
        evidence.save("knee", {"safe_rps": 375})
        assert evidence.load("knee") == {"safe_rps": 375}

    def test_the_project_files_read_and_glob_relative_paths(self, tmp_path):
        (tmp_path / "dashboards").mkdir()
        (tmp_path / "dashboards" / "a.json").write_text("{}")
        files = FileSystemProject(tmp_path)
        assert files.exists("dashboards/a.json")
        assert files.glob("dashboards/*.json") == ["dashboards/a.json"]
        assert files.read("dashboards/a.json") == "{}"
        assert files.root == tmp_path

    def test_the_checklist_reads_owners_dates_and_accepted_risks(self, root):
        checklist = YamlChecklist(root / "readiness" / "checklist.yaml")
        assert checklist.owner("restore") == "database on-call"
        assert checklist.due("restore") == date(2026, 11, 3)
        assert checklist.blocking("runbooks") is True
        assert checklist.blocking("restore") is False
        assert {risk["area"] for risk in checklist.accepted_risks()} >= {"Autoscaling"}

    def test_the_slo_catalog_reports_whether_the_committed_rules_are_current(self, root, tmp_path):
        assert SloFilesCatalog(root).rules_in_sync()
        shutil.copytree(root / "slo", tmp_path / "slo")
        (tmp_path / "slo" / "generated" / "rules.yml").write_text("stale")
        assert not SloFilesCatalog(tmp_path).rules_in_sync()

```

`TestAdapters`, using pytest's `tmp_path` (a fresh temporary directory per test): evidence round-trips and a missing experiment is `None`; the project files `exists`, `glob`, `read` and `root`; the checklist reads the owner, due date and blocking flag of the real `readiness/checklist.yaml` and includes the accepted risk Autoscaling; and the SLO catalog says the committed rules are current but, after a copy of `slo/` has one rule file overwritten with `"stale"`, says they are not.

```python
class TestComposeRetries:
    @staticmethod
    def _fake_docker(monkeypatch, stderrs):
        calls = []

        def fake_run(*_, **__):
            calls.append(1)
            err = stderrs[min(len(calls), len(stderrs)) - 1]
            return subprocess.CompletedProcess([], 1 if err else 0, b"out", err.encode())

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(time, "sleep", lambda _: None)
        return calls

    def test_a_transient_daemon_error_is_retried_until_it_works(self, monkeypatch, tmp_path):
        calls = self._fake_docker(monkeypatch, ["502 Bad Gateway", "connection reset", ""])
        assert compose.Compose(tmp_path).run("ps") == b"out"
        assert len(calls) == 3

    def test_a_real_error_fails_at_once(self, monkeypatch, tmp_path):
        calls = self._fake_docker(monkeypatch, ["no such service: nope"])
        with pytest.raises(compose.ComposeError, match="no such service"):
            compose.Compose(tmp_path).run("up", "nope")
        assert len(calls) == 1

    def test_a_persistent_transient_error_gives_up_after_three_tries(self, monkeypatch, tmp_path):
        calls = self._fake_docker(monkeypatch, ["Bad Gateway"])
        with pytest.raises(compose.ComposeError):
            compose.Compose(tmp_path).run("ps")
        assert len(calls) == 3

```

`TestComposeRetries` replaces `subprocess.run` and `time.sleep` using pytest's `monkeypatch` (it restores the originals after the test). `_fake_docker` returns a fake whose n-th call has the n-th stderr text (an empty text means success). A transient daemon error ("Bad Gateway", "connection reset") is retried until it works (three calls); a real error ("no such service") fails at once (one call); a persistent transient error gives up after three tries.

```python
class TestCompose:
    async def test_healthy_members_counts_hosts_that_are_healthy_and_not_pending_removal(self, monkeypatch):
        async def clusters(_):
            lines = [f"app::10.0.0.{n}:8080::health_flags::healthy" for n in range(1, 6)]
            lines += [
                "app::10.0.0.9:8080::health_flags::/pending_dynamic_removal"
            ]  # dropped from DNS, still remembered
            lines += ["app::10.0.0.8:8080::health_flags::/failed_active_hc"]
            return web.Response(text="\n".join(lines) + "\n")

        app = web.Application()
        app.router.add_get("/clusters", clusters)
        server = TestServer(app)
        await server.start_server()
        try:
            monkeypatch.setattr(compose, "CLUSTERS_URL", f"http://127.0.0.1:{server.port}/clusters")
            assert await asyncio.to_thread(compose.healthy_members) == 5
            await asyncio.to_thread(compose.wait_for_members, 5, 2.0)
            with pytest.raises(TimeoutError):
                await asyncio.to_thread(compose.wait_for_members, 8, 1.0)
        finally:
            await server.close()

    def test_an_unreachable_balancer_counts_as_zero_healthy(self, monkeypatch):
        monkeypatch.setattr(compose, "CLUSTERS_URL", "http://127.0.0.1:9/clusters")
        assert compose.healthy_members() == 0

    async def test_versions_seen_reads_the_version_header(self):
        async def work(_):
            return web.json_response({}, headers={"X-Version": "v2"})

        app = web.Application()
        app.router.add_get("/work", work)
        server = TestServer(app)
        await server.start_server()
        try:
            assert await asyncio.to_thread(compose.versions_seen, f"http://127.0.0.1:{server.port}", 3) == {"v2"}
        finally:
            await server.close()

```

`TestCompose`: `healthy_members` reads a fake `/clusters` page with five healthy hosts, one pending removal and one failed health check, and counts 5; `wait_for_members(5)` returns, and waiting for 8 raises `TimeoutError`; the blocking helpers run in `asyncio.to_thread` so they do not block the server in the same process. An unreachable balancer counts as zero healthy, and `versions_seen` reads the `X-Version` header.

```python
async def test_the_load_test_cli_runs_a_fleet_report_against_a_real_instance(capsys):
    settings = InstanceSettings("a", "a1", "v1", 100, None, 0.3, 0, 0, "INFO")
    metrics = InstanceMetrics("a", "a1")
    server = TestServer(instance_app.build_app(settings, metrics))
    ops = TestServer(build_ops_app(metrics.render))
    await server.start_server()
    await ops.start_server()
    try:
        argv = [
            "fleet",
            "--target",
            f"http://127.0.0.1:{server.port}",
            "--rps",
            "50",
            "--seconds",
            "2",
            "--label",
            "t",
            "--window",
            "1",
        ]
        await asyncio.to_thread(loadtest_cli.main, argv)
        report = json.loads(capsys.readouterr().out)
        assert (report["experiment"], report["label"], report["requests"], report["errors"]) == ("fleet", "t", 100, 0)
        knee_argv = [
            "knee", "--target", f"http://127.0.0.1:{server.port}", "--ops", f"http://127.0.0.1:{ops.port}",
            "--steps", "10,20,30,40", "--step-seconds", "0.5", "--attempts", "1",
        ]  # fmt: skip
        with pytest.raises(SystemExit, match="not a valid measurement, nothing written"):
            await asyncio.to_thread(loadtest_cli.main, knee_argv)
        assert capsys.readouterr().out == ""  # a load that never reached a knee leaves nothing to save as evidence
    finally:
        await server.close()
        await ops.close()
```

The load-test command runs a `fleet` report against a real in-process instance (100 requests, no errors, label `t`), then a `knee` run with four steps. `capsys` captures what the program printed so the test can parse the JSON; the `# fmt: skip` comment keeps the formatter from rewriting the argument list.

```python
class TestCapacityCli:
    def project(self, tmp_path, root, evidence):
        shutil.copytree(root / "slo", tmp_path / "slo")
        shutil.copytree(root / "readiness", tmp_path / "readiness")
        (tmp_path / "docs").mkdir()
        for name, document in evidence.items():
            JsonEvidence(tmp_path / "docs" / "evidence").save(name, document)
        return tmp_path

    def run(self, argv):
        with pytest.raises(SystemExit) as exit_info:
            capacity_cli.main(argv)
        return exit_info.value.code

    def test_the_plan_command_sizes_the_fleet_from_the_measured_safe_load_and_saves_it(self, tmp_path, root, capsys):
        project = self.project(tmp_path, root, {"knee": {"safe_rps": 400}, "fleet-zone-loss": {"within_slo": True}})
        assert self.run(["plan", "--evidence", str(project / "docs" / "evidence"), "--root", str(project)]) == 0
        plan = JsonEvidence(project / "docs" / "evidence").load("plan")
        assert plan is not None
        assert (plan["instances_for_peak"], plan["total_instances"], plan["verified_by_lost_zone_run"]) == (5, 8, True)
        assert "ceil(2000 / 400) = 5" in capsys.readouterr().out
        rules = (project / "slo" / "alerts" / "fleet.rules.yml").read_text()
        assert "< 5" in rules
        assert "< 8" in rules

    def test_the_plan_command_fails_when_the_plan_needs_more_instances_than_the_compose_file_defines(
        self, tmp_path, root, capsys
    ):
        project = self.project(tmp_path, root, {"knee": {"safe_rps": 200}})
        assert self.run(["plan", "--evidence", str(project / "docs" / "evidence"), "--root", str(project)]) == 1
        assert "docker-compose.yml defines 9" in capsys.readouterr().err

    def test_the_plan_command_refuses_to_guess_without_a_measurement(self, tmp_path):
        with pytest.raises(SystemExit, match="no safe load"):
            capacity_cli.main(["plan", "--evidence", str(tmp_path)])

    def test_the_review_command_prints_the_checklist_and_writes_the_decision(self, tmp_path, root, capsys):
        project = self.project(tmp_path, root, {})
        shutil.copytree(root / "dashboards", project / "dashboards")
        shutil.copytree(root / "docs" / "runbooks", project / "docs" / "runbooks")
        shutil.copy(root / "docs" / "ownership.md", project / "docs" / "ownership.md")
        (project / "slo" / "tests").mkdir(exist_ok=True)
        assert self.run(["review", "--root", str(project), "--write"]) == 0
        output = capsys.readouterr().out
        assert "Readiness review:" in output
        assert "Backup and restore" in output
        assert "Decision: " in output
        text = (project / "docs" / "launch-decision.md").read_text()
        assert text.startswith("# Launch decision")
        assert "restore not yet tested" in text

    def test_the_report_command_needs_all_the_evidence(self, tmp_path):
        with pytest.raises(SystemExit, match="missing evidence"):
            capacity_cli.main(["report", "--root", str(tmp_path)])

    def test_the_rules_command_detects_drift_and_writes(self, monkeypatch, tmp_path, root):
        (tmp_path / "slo").mkdir()
        shutil.copy(root / "slo" / "slos.yaml", tmp_path / "slo" / "slos.yaml")
        monkeypatch.chdir(tmp_path)
        for argv, expected in ((["rules", "--check"], 1), (["rules"], 0), (["rules", "--check"], 0)):
            with pytest.raises(SystemExit) as exit_info:
                slo_cli.main(argv)
            assert exit_info.value.code == expected
```

`TestCapacityCli` runs the real entry points against a copied project in `tmp_path`. `project(...)` copies `slo/` and `readiness/` and saves the given evidence; `run(argv)` expects `SystemExit` and returns its code. `plan` sizes the fleet from a measured safe load of 400 (5 for the peak, 8 in all, verified by the lost-zone run), prints the formula and writes the fleet alert thresholds (`< 5` and `< 8`) into the project; with a safe load of 200 it needs more than the 9 instances compose defines, so it exits 1 with the message; with no measurement it refuses to guess (`no safe load`). `review` prints the checklist, writes `docs/launch-decision.md` and says `restore not yet tested`. `report` needs all the evidence. The `rules` command detects drift (exit 1), writes (exit 0) and then finds none (exit 0), using `monkeypatch.chdir` to run inside the temporary project.

`TestPreflight` replaces `os.getloadavg` with a fake:

```python
class TestPreflight:
    def test_a_busy_machine_is_refused_before_anything_is_measured(self, monkeypatch):
        from capacity.interfaces import experiments_cli

        monkeypatch.delenv("MAX_LOAD", raising=False)
        monkeypatch.setattr("os.getloadavg", lambda: (99.0, 50.0, 20.0))
        with pytest.raises(SystemExit, match="the machine is busy"):
            experiments_cli.preflight()

    def test_a_quiet_machine_passes_and_the_limit_can_be_raised(self, monkeypatch):
        from capacity.interfaces import experiments_cli

        monkeypatch.setattr("os.getloadavg", lambda: (0.5, 0.5, 0.5))
        assert experiments_cli.preflight()["load_1min"] == 0.5
        monkeypatch.setattr("os.getloadavg", lambda: (99.0, 50.0, 20.0))
        monkeypatch.setenv("MAX_LOAD", "100")
        assert experiments_cli.preflight()["load_1min"] == 99.0
```

A load of 99 is refused with "the machine is busy" before anything is measured (`delenv` makes sure no `MAX_LOAD` from the environment changes the test); a load of 0.5 passes and reports what it saw; and setting `MAX_LOAD` to 100 lets the busy machine through. The import inside each test avoids loading the experiments module until it is needed.

The load-test command test also changed: the knee run over 10 to 40 requests a second now expects `SystemExit` with the message "not a valid measurement, nothing written", because a load that never reaches a knee leaves nothing to save, and checks that standard output stayed empty.

### `tests/measurements/test_done_when.py`

```python
"""The lesson's Done when lines, held against the measurements `make run` left in docs/evidence.

These depend on the machine that took the measurements: a contended host shows a late load generator, a knee that is
noise, or a fleet that cannot hold the projected peak, and these tests then fail and say so. Run them with
`make test-measurements` after `make run`; `make run` ends with them.
"""

import pytest

SLO_P99_MS = 50.0


pytestmark = pytest.mark.measurements

```

These hold the lesson's "Done when" lines against the measurements `make run` leaves in `docs/evidence`. The docstring warns they depend on the machine: a contended host shows a late load generator, a knee that is noise, or a fleet that cannot hold the projected peak, and these tests then fail and say so.

```python
def test_step_one_the_knee_was_found_and_cpu_was_the_resource_that_saturated(evidence):
    knee = evidence("knee")
    assert knee["knee"] is not None
    assert knee["knee"]["saturated_resource"] == "cpu"
    assert knee["knee"]["cpu_utilization_at_knee"] >= 0.9
    steps = knee["steps"]
    flat = [step["p99_ms"] for step in steps if step["target_rps"] < knee["knee"]["knee_rps"]]
    assert max(flat) < SLO_P99_MS, "p99 was flat and inside the SLO on the good side of the knee"
    assert knee["knee"]["knee_p99_ms"] > 3 * knee["knee"]["baseline_p99_ms"], "and jumped on the other side"

```

Step one: a knee was found, CPU was the saturated resource (at least 90% of the core), p99 was flat and under 50 ms on the good side of the knee, and the knee's p99 is more than three times the baseline. The text after the comma in an `assert` is the message pytest shows on failure.

```python
def test_step_two_the_safe_load_is_a_stated_margin_below_the_knee_and_held_for_the_whole_soak(evidence):
    knee, soak = evidence("knee"), evidence("soak")
    margin = 1 - knee["safe_rps"] / knee["knee"]["knee_rps"]
    assert 0.2 <= margin <= 0.4
    assert knee["margin_below_knee"] == 0.25
    assert soak["target_rps"] == knee["safe_rps"]
    assert soak["errors"] == 0
    assert soak["within_slo"] is True
    assert len(soak["window_p99_ms"]) >= 6, "the soak watched p99 in windows, not once"
    assert max(soak["window_p99_ms"]) <= SLO_P99_MS

```

Step two: the safe load is 20% to 40% below the knee (the stated margin is 25%), the soak ran at exactly the safe load with no errors, stayed within the SLO, and watched p99 in at least six windows, none over 50 ms.

```python
def test_step_three_the_planned_fleet_minus_a_zone_was_run_at_the_projected_peak_and_held(evidence):
    plan, zone = evidence("plan"), evidence("fleet-zone-loss")
    assert plan["verified_by_lost_zone_run"] is True
    assert zone["healthy_instances"] == plan["surviving_after_zone_loss"]
    assert zone["target_rps"] == plan["future_peak_rps"]
    assert zone["within_slo"] is True
    assert zone["errors"] == 0

```

Step three: the planned fleet minus a zone was run at the projected peak and held: the healthy instances equal the survivors in the plan, the target is the six-month peak, within the SLO with no errors.

```python
def test_step_four_the_next_bottleneck_is_the_shared_pool_and_the_plan_names_it_with_a_number(evidence, root):
    sweep, naive, resized = evidence("pool-sweep"), evidence("fleet-pool-naive"), evidence("fleet-pool-resized")
    assert sweep["naive_pool_size"] == 20
    assert naive["pool_size"] == sweep["naive_pool_size"]
    assert naive["within_slo"] is False, "the pool sized at the naive average was the next bottleneck"
    assert resized["within_slo"] is True
    assert resized["pool_size"] == sweep["chosen_pool_size"] > naive["pool_size"]
    text = (root / "docs" / "capacity-plan.md").read_text()
    assert f"{resized['pool_size']}" in text
    assert f"{naive['pool_size']}" in text

```

Step four: the naive pool size is 20, the naive pool failed, the resized pool held, the resized size is the one the sweep chose and larger than the naive one, and both numbers appear in `docs/capacity-plan.md`.

```python
def test_a_rolling_restart_and_a_rollback_under_load_cost_no_request(evidence):
    rollout, plan = evidence("rollout"), evidence("plan")
    assert rollout["restarted"] == 2 * plan["total_instances"], "every instance restarted twice: to v2, then back to v1"
    assert rollout["errors"] == 0
    assert rollout["within_slo"] is True
    assert rollout["rollback_ok"] is True


def test_a_frozen_dependency_fails_requests_fast_and_leaves_the_instance_healthy(evidence):
    dependency = evidence("dependency")
    assert dependency["fail_fast"] is True
    assert dependency["slowest_503_ms"] <= dependency["bound_ms"]
    assert dependency["instance_healthy"] is True
```

Two more: a rolling restart of 16 instance restarts (roll to v2, roll back to v1) cost no request, and a frozen dependency fails requests within the bound while the instance stays healthy.

### `tests/e2e/test_fleet_smoke.py`

```python
"""Smoke tests of the running fleet. Needs `make up` first."""

import json
import time
import urllib.request
from pathlib import Path

import pytest

from capacity.domain.telemetry import EXPORTED_METRICS

pytestmark = pytest.mark.e2e
EDGE = "http://localhost:47280"
ENVOY_ADMIN = "http://localhost:47281"
INSTANCE_OPS = "http://localhost:47283"
LEDGER_OPS = "http://localhost:47284"
PROMETHEUS = "http://localhost:47290"
INSTANCES_DEFINED = 9
PLAN = Path(__file__).resolve().parents[2] / "docs" / "evidence" / "plan.json"
# Right after `make up` every defined instance runs; once `make run` has planned the fleet, only the planned ones do.
RUNNING = json.loads(PLAN.read_text())["total_instances"] if PLAN.exists() else INSTANCES_DEFINED


def fetch(url: str) -> tuple[int, dict[str, str], str]:
    """GET a URL; header names come back lower-cased (Envoy lower-cases them)."""
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read().decode()
    except OSError as error:
        pytest.fail(f"{url} is not reachable ({error}); run `make up` first")

```

The end-to-end tests need the running stack (`make up`). The constants are the host ports of the edge, Envoy's admin interface, one instance's ops port, the ledger's ops port and Prometheus. `fetch(url)` does a GET and returns the status, lower-cased headers and body; if the URL is not reachable it fails the test with a message telling the reader to run `make up`.

```python
def test_requests_through_the_edge_reach_every_instance_in_every_zone():
    seen = set()
    zones = set()
    for _ in range(120):
        status, headers, _ = fetch(f"{EDGE}/work")
        assert status == 200
        seen.add(headers["x-instance"])
        zones.add(headers["x-zone"])
    assert zones == {"a", "b", "c"}
    assert len(seen) == RUNNING


def test_the_load_balancer_counts_exactly_the_running_instances_as_healthy():
    _, _, body = fetch(f"{ENVOY_ADMIN}/stats?filter=cluster.app.membership_healthy")
    assert body.strip().endswith(f": {RUNNING}")

```

Requests through the edge reach every instance in every zone (120 requests reach zones a, b and c and every instance that is running: all nine right after `make up`, or the plan's fleet after `make run`), and Envoy counts exactly the running instances as healthy.

```python
def test_every_metric_the_dashboards_and_alerts_query_is_really_exported():
    exported = "\n".join(
        fetch(url)[2] for url in (f"{ENVOY_ADMIN}/stats/prometheus", f"{INSTANCE_OPS}/metrics", f"{LEDGER_OPS}/metrics")
    )
    missing = sorted(name for name in EXPORTED_METRICS - {"up"} if name not in exported)
    assert missing == []


def test_prometheus_scrapes_the_edge_the_instances_and_the_ledger():
    for _ in range(30):
        targets = json.loads(fetch(f"{PROMETHEUS}/api/v1/targets")[2])["data"]["activeTargets"]
        if len(targets) >= 10:
            break
        time.sleep(1)
    jobs = {target["labels"]["job"] for target in targets}
    assert jobs == {"edge", "instances", "ledger"}
    assert len([t for t in targets if t["labels"]["job"] == "instances"]) == INSTANCES_DEFINED


def test_prometheus_loaded_the_generated_slo_rules_and_the_fleet_alerts():
    _, _, body = fetch(f"{PROMETHEUS}/api/v1/rules")
    names = {rule["name"] for group in json.loads(body)["data"]["groups"] for rule in group["rules"]}
    assert {"ApiAvailabilityFastBurn", "ApiLatencyFastBurn", "FleetBelowPeakCapacity", "EdgeMetricsAbsent"} <= names
```

Every metric the dashboards and alerts query is really exported (checked against Envoy, an instance and the ledger, ignoring `up`, which Prometheus itself makes); Prometheus scrapes the edge, the instances and the ledger (at least ten targets, nine of them instances, running or not); and Prometheus loaded the generated SLO rules and the fleet alerts, including `EdgeMetricsAbsent`.

The constants added at the top say what the smoke tests expect: `INSTANCES_DEFINED = 9` (instances compose defines), `PLAN` (the path of the plan evidence) and `RUNNING`, which is the plan's `total_instances` when the plan exists and 9 right after `make up`. So the same tests are right before and after `make run` has chosen the fleet.

## Try it yourself

1. `.venv/bin/python -m pytest tests/unit -q` runs all unit tests in a few seconds.
2. `.venv/bin/python -m pytest tests/integration -q` runs the real-socket tests.
3. `.venv/bin/python -m pytest tests/unit/test_readiness.py -q -k restore` runs only the tests whose name contains `restore`.
4. `make test-measurements` (after `make run`) holds the committed measurements to the lesson's lines.

## Mistakes people make

- A test that cannot fail. Each test here breaks exactly one thing in a good setup and asserts the right check fails.
- Hiding the cost of a slow test in the default run. Integration, measurement and end-to-end tests have markers so each group can be run on its own.
- Testing against a sleep instead of a real wait. The load-generator tests measure real time but with generous bounds.
- Editing a generated document by hand. A test compares `docs/launch-decision.md` with what the review produces.

## Check yourself

1. What does `monkeypatch` do?
   *Answer: it replaces something (a function, a variable, the working directory) for one test and puts the original back afterwards.*
2. Why does the queuing test use a lock?
   *Answer: it makes the server handle one request at a time, so arrivals beyond its capacity queue and an open-loop generator measures the wait.*
3. What is the difference between `tests/unit/test_evidence.py` and `tests/measurements/test_done_when.py`?
   *Answer: the first checks that the documents follow from the committed evidence; the second checks that the evidence meets the lesson's targets, which depends on the machine.*
4. Which tests need Docker?
   *Answer: only the end-to-end tests (and the experiments that produce the evidence).*
5. How does a test select a group?
   *Answer: a module-level `pytestmark` marker (`integration`, `measurements`, `e2e`) that `make` targets select with `-m`.*

## Further reading

- pytest fixtures: <https://docs.pytest.org/en/stable/how-to/fixtures.html>
- `pytest.mark.parametrize`: <https://docs.pytest.org/en/stable/how-to/parametrize.html>
- `monkeypatch` and `tmp_path`: <https://docs.pytest.org/en/stable/how-to/monkeypatch.html>
- pytest-asyncio: <https://pytest-asyncio.readthedocs.io/>
- aiohttp test utilities: <https://docs.aiohttp.org/en/stable/testing.html>
