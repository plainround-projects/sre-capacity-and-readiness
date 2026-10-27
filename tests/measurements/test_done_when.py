"""The lesson's Done when lines, held against the measurements `make run` left in docs/evidence.

These depend on the machine that took the measurements: a contended host shows a late load generator, a knee that is
noise, or a fleet that cannot hold the projected peak, and these tests then fail and say so. Run them with
`make test-measurements` after `make run`; `make run` ends with them.
"""

import pytest

SLO_P99_MS = 50.0


pytestmark = pytest.mark.measurements


def test_step_one_the_knee_was_found_and_cpu_was_the_resource_that_saturated(evidence):
    knee = evidence("knee")
    assert knee["knee"] is not None
    assert knee["knee"]["saturated_resource"] == "cpu"
    assert knee["knee"]["cpu_utilization_at_knee"] >= 0.9
    steps = knee["steps"]
    flat = [step["p99_ms"] for step in steps if step["target_rps"] < knee["knee"]["knee_rps"]]
    assert max(flat) < SLO_P99_MS, "p99 was flat and inside the SLO on the good side of the knee"
    assert knee["knee"]["knee_p99_ms"] > 3 * knee["knee"]["baseline_p99_ms"], "and jumped on the other side"


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


def test_step_three_the_planned_fleet_minus_a_zone_was_run_at_the_projected_peak_and_held(evidence):
    plan, zone = evidence("plan"), evidence("fleet-zone-loss")
    assert plan["verified_by_lost_zone_run"] is True
    assert zone["healthy_instances"] == plan["surviving_after_zone_loss"]
    assert zone["target_rps"] == plan["future_peak_rps"]
    assert zone["within_slo"] is True
    assert zone["errors"] == 0


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


def test_a_rolling_restart_and_a_rollback_under_load_cost_no_request(evidence):
    rollout = evidence("rollout")
    assert rollout["restarted"] == 16
    assert rollout["errors"] == 0
    assert rollout["within_slo"] is True
    assert rollout["rollback_ok"] is True


def test_a_frozen_dependency_fails_requests_fast_and_leaves_the_instance_healthy(evidence):
    dependency = evidence("dependency")
    assert dependency["fail_fast"] is True
    assert dependency["slowest_503_ms"] <= dependency["bound_ms"]
    assert dependency["instance_healthy"] is True
