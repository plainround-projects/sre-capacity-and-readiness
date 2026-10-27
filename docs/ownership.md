# Ownership and on-call

*Roles, never names: a role survives people leaving the team.*

## Who owns what

| Thing | Owner (role) |
| --- | --- |
| The service and its SLOs | **service owner** |
| The capacity plan and the fleet size | **capacity owner** |
| The ledger database, backups and the restore test | **database on-call** |
| Releases, rollout and rollback | **release owner** |
| This document, the rota and the runbooks | **on-call lead** |
| Escalation beyond the rota | **engineering manager** |

## On-call

- **Primary** on-call: receives every page, acknowledges within 5 minutes, runs the incident (see
  [runbooks](runbooks/)).
- **Secondary** on-call: backs the primary up; paged automatically if the primary does not acknowledge within 5
  minutes, and the person to hand over to after 12 hours.
- Shifts are one week, handed over on a weekday morning with the open tickets and any silenced alerts.

## Escalation path

1. Primary on-call (page).
2. Secondary on-call (automatic after 5 minutes without acknowledgement).
3. On-call lead (for a decision the rota cannot make: stop a release, declare Sev1, call the database on-call).
4. Engineering manager (Sev1 lasting more than 30 minutes, or customer-facing communication).

## Runbooks

| Alert or failure | Runbook |
| --- | --- |
| `FleetBelowPeakCapacity`, `FleetBelowFullStrength`, a zone lost | [zone-loss.md](runbooks/zone-loss.md) |
| p99 climbing with CPU near the limit, SLO latency burn | [cpu-knee.md](runbooks/cpu-knee.md) |
| Ledger errors, 503s from the dependency, pool in use at its size | [pool-exhaustion.md](runbooks/pool-exhaustion.md) |
| Any SLO burn-rate alert | [api.md](runbooks/api.md) |
| `EdgeMetricsAbsent` | [api.md](runbooks/api.md) |
