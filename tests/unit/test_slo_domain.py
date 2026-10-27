import pytest

from capacity.domain.errors import InvalidSloError
from capacity.domain.slo import DEFAULT_ALERTS, BurnAlert, SliKind, Slo, budget_remaining_pct, burn_rate


def availability(target: float = 0.9995) -> Slo:
    return Slo("a", "follow_link", SliKind.AVAILABILITY, target, "all", "below 500", "because")


def latency(threshold_ms: float = 100) -> Slo:
    return Slo("l", "follow_link", SliKind.LATENCY, 0.95, "ok", "fast", "because", threshold_ms)


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
