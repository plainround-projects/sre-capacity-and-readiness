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
