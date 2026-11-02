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
