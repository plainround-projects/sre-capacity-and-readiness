# Runbook: SLO burn-rate alerts and EdgeMetricsAbsent

Alerts: `ApiAvailability{FastBurn,SlowBurn,BudgetLeak}`, `ApiLatency{FastBurn,SlowBurn,BudgetLeak}` and
`EdgeMetricsAbsent`. They measure what callers see at the Envoy edge, and they are slower than the cause-based
alerts (`FleetBelowPeakCapacity`) on purpose.

1. **Look.** `curl -s localhost:47290/api/v1/query --data-urlencode 'query=slo:sli_error:ratio_rate5m'` shows which
   SLO is burning; the dashboard `dashboards/fleet.json` shows edge rate, p99, 5xx ratio, healthy instances, CPU per
   instance and the ledger pool side by side.
2. **Classify.** Healthy instances below 8: [zone-loss.md](zone-loss.md). CPU of every instance near 1.0 with p99
   climbing: [cpu-knee.md](cpu-knee.md). Ledger pool in use at its size, or instances answering 503 with
   `ledger answered 503`: [pool-exhaustion.md](pool-exhaustion.md).
3. **Latency burn without errors** usually means the fleet is past its safe load; compare the edge request rate with
   the plan (`docs/capacity-plan.md`).
4. **`EdgeMetricsAbsent`:** Envoy exports nothing, so every SLI is blind. `docker compose ps lb`, then
   `docker compose logs --tail=50 lb`; check <http://localhost:47281/ready>. Do not release until it is back.
