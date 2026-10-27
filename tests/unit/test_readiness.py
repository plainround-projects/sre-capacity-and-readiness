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
