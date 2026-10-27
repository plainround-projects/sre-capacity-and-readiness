"""Metric names the generated rules read: the Envoy edge's, and the instances' own."""

from __future__ import annotations

# Envoy's HTTP connection manager is the edge: every request passes through it, so its counts and latency
# histogram are what users experience. Its latency buckets are in milliseconds.
EDGE_PREFIX = 'envoy_http_conn_manager_prefix="ingress_http"'
EDGE_RESPONSES = "envoy_http_downstream_rq_xx"
EDGE_DURATION_BUCKET = "envoy_http_downstream_rq_time_bucket"
EDGE_DURATION_COUNT = "envoy_http_downstream_rq_time_count"
EDGE_LATENCY_BUCKETS_MS: tuple[float, ...] = (
    0.5,
    1,
    5,
    10,
    25,
    50,
    100,
    250,
    500,
    1000,
    2500,
    5000,
    10000,
    30000,
    60000,
)
EDGE_HEALTHY_MEMBERS = 'envoy_cluster_membership_healthy{envoy_cluster_name="app"}'

# The instances' own metrics, scraped per instance.
APP_REQUESTS_TOTAL = "app_requests_total"
APP_DURATION_SECONDS = "app_request_duration_seconds"
APP_INFLIGHT = "app_inflight_requests"
APP_LATENCY_BUCKETS_SECONDS: tuple[float, ...] = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5)

# Every metric a dashboard or alert may query. The end-to-end test checks each name against the running system's
# own endpoints, so this set cannot drift from what is really exported.
EXPORTED_METRICS: frozenset[str] = frozenset(
    {
        "envoy_http_downstream_rq_xx",
        "envoy_http_downstream_rq_total",
        "envoy_http_downstream_rq_time_bucket",
        "envoy_http_downstream_rq_time_count",
        "envoy_http_downstream_rq_time_sum",
        "envoy_cluster_membership_healthy",
        "envoy_cluster_membership_total",
        "app_requests_total",
        "app_request_duration_seconds_bucket",
        "app_request_duration_seconds_count",
        "app_request_duration_seconds_sum",
        "app_inflight_requests",
        "process_cpu_seconds_total",
        "ledger_pool_in_use",
        "ledger_pool_waiting",
        "ledger_pool_size",
        "ledger_requests_total",
        "ledger_pool_timeouts_total",
        "up",
    }
)
RECORDED_RULE_PREFIX = "slo:"
