# 0002. Envoy as the load balancer and the place SLIs are measured

**Status:** accepted, 2026-10-27

## Context

The fleet needs a load balancer that health-checks instances, retries a refused connection onto another one, and
exports request counts and a latency histogram. The SLIs should be measured where users enter, not inside the
instances (which cannot see a request that never reached them).

## Decision

Envoy fronts the eight instances in three localities (zones). Active health checks hit each instance's ops port
every second; one unhealthy answer removes it. A request that hits a refused or reset connection is retried once on
another instance, so a restarting instance (the rollout test) or a stopped zone (the zone-loss test) does not reach a
user. Envoy's own `envoy_http_downstream_rq_xx` and `envoy_http_downstream_rq_time_*` series are the SLIs; the
generated rules read those, not the instances' metrics.

## Alternatives

- **nginx.** Lighter, but open-source nginx exports no per-code counts or latency histogram without a module.
- **A Python proxy** (as in the `sre-slos-and-budget` project). Fine at 50 requests a second; at 2,000 it would be
  the bottleneck the test is looking for in the service.
- **Client-side balancing in the load generator.** Cheapest, but leaves the health-check and retry behavior untested.

## Consequences

The load balancer is a single container, a single point of failure in this build. Its latency buckets are fixed by
Envoy in milliseconds (0.5, 1, 5, 10, 25, 50, 100, ...), so the 50 ms SLO threshold must be one of them; the rule
generator refuses a threshold that is not.
