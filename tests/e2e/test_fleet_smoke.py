"""Smoke tests of the running fleet. Needs `make up` first."""

import json
import urllib.request

import pytest

from capacity.domain.telemetry import EXPORTED_METRICS

pytestmark = pytest.mark.e2e
EDGE = "http://localhost:47280"
ENVOY_ADMIN = "http://localhost:47281"
INSTANCE_OPS = "http://localhost:47283"
LEDGER_OPS = "http://localhost:47284"
PROMETHEUS = "http://localhost:47290"


def fetch(url: str) -> tuple[int, dict[str, str], str]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, dict(response.headers), response.read().decode()
    except OSError as error:
        pytest.fail(f"{url} is not reachable ({error}); run `make up` first")


def test_requests_through_the_edge_reach_every_instance_in_every_zone():
    seen = set()
    zones = set()
    for _ in range(120):
        status, headers, _ = fetch(f"{EDGE}/work")
        assert status == 200
        seen.add(headers["X-Instance"])
        zones.add(headers["X-Zone"])
    assert zones == {"a", "b", "c"}
    assert len(seen) == 8


def test_the_load_balancer_sees_eight_healthy_instances():
    _, _, body = fetch(f"{ENVOY_ADMIN}/stats?filter=cluster.app.membership_healthy")
    assert body.strip().endswith(": 8")


def test_every_metric_the_dashboards_and_alerts_query_is_really_exported():
    exported = "\n".join(
        fetch(url)[2] for url in (f"{ENVOY_ADMIN}/stats/prometheus", f"{INSTANCE_OPS}/metrics", f"{LEDGER_OPS}/metrics")
    )
    missing = sorted(name for name in EXPORTED_METRICS - {"up"} if name not in exported)
    assert missing == []


def test_prometheus_scrapes_the_edge_the_instances_and_the_ledger():
    _, _, body = fetch(f"{PROMETHEUS}/api/v1/targets")
    targets = json.loads(body)["data"]["activeTargets"]
    jobs = {target["labels"]["job"] for target in targets}
    assert jobs == {"edge", "instances", "ledger"}
    assert len([t for t in targets if t["labels"]["job"] == "instances"]) == 8


def test_prometheus_loaded_the_generated_slo_rules_and_the_fleet_alerts():
    _, _, body = fetch(f"{PROMETHEUS}/api/v1/rules")
    names = {rule["name"] for group in json.loads(body)["data"]["groups"] for rule in group["rules"]}
    assert {"ApiAvailabilityFastBurn", "ApiLatencyFastBurn", "FleetBelowPeakCapacity", "EdgeMetricsAbsent"} <= names
