# Runbook: loss of a zone

## Symptoms

- `FleetBelowFullStrength` (ticket): fewer healthy instances than the fleet has. `FleetBelowPeakCapacity` (page): fewer
  than the minimum that carries the projected peak (both numbers are in the facts above).
- Dashboard panel "Healthy instances" drops by the size of a zone; edge p99 rises if the remaining instances are above
  their safe load.

## What is known

<!-- measured -->
Run `make run` to measure this: the numbers are written here from `docs/evidence`.
<!-- /measured -->

This was run, not assumed: `make zone-loss` stops the largest zone and runs the projected peak on the survivors
(see `docs/capacity-plan.md`).

## Mitigate

1. Confirm what is down: `docker compose ps`, and Envoy's view at <http://localhost:47281/clusters>.
2. Below the minimum, the fleet cannot carry the peak: shed load at the edge and add instances in the surviving
   zones until healthy members times the safe load covers the traffic.
3. Restore the zone: `docker compose up -d --wait app-a1 app-a2 app-a3` (substitute the zone's instances) and wait for
   Envoy to show them healthy.

## Diagnose

- One instance down is a crash (logs: `docker compose logs app-a1`); a whole zone down is the zone or the host.
- Check whether the loss was graceful (a rollout) or not: the rollout test restarts one instance at a time and keeps
  the fleet at 7 or more.
