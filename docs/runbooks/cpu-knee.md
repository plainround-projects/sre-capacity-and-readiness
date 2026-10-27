# Runbook: CPU saturation past the knee

## Symptoms

- p99 at the edge climbs sharply while the request rate rises only a little; `ApiLatencyFastBurn` or `SlowBurn`.
- CPU of the instances (dashboard panel "CPU used by each instance") is near 1.0, the container's whole limit.
- Requests are slow but succeed: errors appear only later, as timeouts.

## What is known

One instance saturates at **@KNEE@ requests a second** (measured: p99 jumps from @BASE@ ms to @KNEEP99@ ms there, with
@CPU@ of its one core in use). The safe load is **@SAFE@ requests a second** per instance. The fleet is planned so
that every surviving instance stays at or below that, even with a zone lost.

## Mitigate

1. Shed load at the edge if the cause is traffic above the plan (the projected peak is @PEAK@ requests a second).
2. Add instances: raise the count in `docker-compose.yml`, `docker compose up -d`, and wait for Envoy to show the new
   members healthy. Capacity grows by @SAFE@ requests a second per instance.
3. If a release caused it (cost per request went up), roll back: the rollout test shows a roll to the previous version
   costs no request.

## Diagnose

- Compare the edge request rate with `healthy instances x @SAFE@`. Above it, the fleet is simply too small for the
  traffic; below it, look for a change in cost per request (a new release, a bigger payload).
- Per-instance CPU uneven means an uneven spread: check Envoy's `/clusters` for one instance carrying more.
- Repeat the knee test on one instance (`make knee`) to see if the knee has moved.
