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
