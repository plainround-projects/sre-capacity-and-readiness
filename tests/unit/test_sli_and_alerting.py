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


class TestLatencyHistogram:
    def test_p95_is_within_one_percent(self):
        histogram = LatencyHistogram()
        for value in range(1, 1001):
            histogram.add(float(value))
        assert histogram.percentile(0.95) == pytest.approx(950, rel=0.02)

    def test_an_empty_histogram_reports_zero(self):
        assert LatencyHistogram().percentile(0.95) == 0.0


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
