# Runbook: shared pool exhaustion (the ledger)

## Symptoms

- Instances answer 503 with `ledger answered 503` or `ledger unavailable`; the edge 5xx ratio rises while instance CPU
  is not saturated.
- Dashboard panel "Ledger pool": in use equal to the pool size, waiting above zero.
- p99 rises first (requests wait up to `POOL_TIMEOUT_S`), then errors.

## What is known

The ledger's pool is a **shared** resource: adding instances does not enlarge it. Sizing it as peak rate times hold
time (**@NAIVE@** connections for @PEAK@ requests a second at 10 ms) failed at the projected peak; **@POOL@** held it
(see the sweep in `docs/capacity-plan.md`).

## Mitigate

1. Raise the pool: `POOL_SIZE=@POOL@ docker compose up -d --no-deps --force-recreate ledger` (the database allows 100
   connections in total; keep the sum under that).
2. If the database is the limit, shed or queue writes at the instances: the instances already fail fast (bound: the
   ledger timeout) instead of hanging.

## Diagnose

- `make psql < sql/003_queries.sql` shows how many connections the ledger holds and their state.
- Hold time up (a slow query) with unchanged traffic means a database problem, not a sizing one: the naive formula
  assumed 10 ms.
- Traffic up means resize: connections needed is at least peak rate times hold time, plus a margin for bursts.
