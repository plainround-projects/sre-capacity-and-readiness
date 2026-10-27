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
