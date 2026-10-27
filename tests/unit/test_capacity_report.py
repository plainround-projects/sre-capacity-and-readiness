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
