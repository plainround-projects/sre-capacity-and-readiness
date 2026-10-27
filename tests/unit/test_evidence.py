"""The committed measurements, held to the lesson's Done when lines. `make run` regenerates the evidence; these tests
fail if what it measured no longer supports what the plan, the alerts, the runbooks and the decision say."""

from pathlib import Path

import pytest
import yaml

from capacity.application import launch_decision
from capacity.application.readiness import Reviewer
from capacity.domain.plan import plan_capacity
from capacity.infrastructure.project_files import FileSystemProject, JsonEvidence, SloFilesCatalog, YamlChecklist

SLO_P99_MS = 50.0


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


def test_step_three_the_plan_is_the_formula_applied_to_the_measured_safe_load(evidence):
    plan, knee = evidence("plan"), evidence("knee")
    recomputed = plan_capacity(plan["current_peak_rps"], plan["growth_factor"], knee["safe_rps"], zones=3)
    assert plan["future_peak_rps"] == 2000
    assert plan["instances_for_peak"] == recomputed.instances_for_peak
    assert plan["total_instances"] == recomputed.total_instances
    assert plan["distribution"] == list(recomputed.distribution)
    assert plan["surviving_after_zone_loss"] * knee["safe_rps"] >= plan["future_peak_rps"]


def test_step_three_the_planned_fleet_minus_a_zone_was_run_at_the_projected_peak_and_held(evidence):
    plan, zone = evidence("plan"), evidence("fleet-zone-loss")
    assert plan["verified_by_lost_zone_run"] is True
    assert zone["healthy_instances"] == plan["surviving_after_zone_loss"]
    assert zone["target_rps"] == plan["future_peak_rps"]
    assert zone["within_slo"] is True
    assert zone["errors"] == 0


def test_the_fleet_alert_threshold_is_the_number_of_instances_the_plan_needs_to_survive(evidence, root):
    document = yaml.safe_load((root / "slo" / "alerts" / "fleet.rules.yml").read_text())
    rules = {rule["alert"]: rule for group in document["groups"] for rule in group["rules"]}
    plan = evidence("plan")
    assert f"< {plan['instances_for_peak']}" in rules["FleetBelowPeakCapacity"]["expr"]
    assert f"< {plan['total_instances']}" in rules["FleetBelowFullStrength"]["expr"]


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


def test_step_five_the_restore_was_executed_and_matches_the_source(evidence):
    restore = evidence("restore")
    assert restore["matched"] is True
    assert restore["rows"] == restore["source_rows"] > 0
    assert restore["restore_seconds"] <= restore["rto_target_seconds"]


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
