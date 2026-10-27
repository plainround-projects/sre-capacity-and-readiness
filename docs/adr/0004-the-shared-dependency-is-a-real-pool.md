# 0004. The shared dependency is a real connection pool in front of PostgreSQL

**Status:** accepted, 2026-10-27

## Context

The lesson's fourth step reintroduces "a real shared dependency every instance calls, sized by its own capacity, not
by instance count", and says the textbook sizing (peak rate times average hold time) is a floor, not the answer.

## Decision

A small `ledger` service owns a `psycopg_pool` of PostgreSQL connections with a hard size. Each request holds a
connection for a configurable time (`HOLD_MS`, 10 ms: one `pg_sleep` plus an insert in a single statement) and waits
at most `POOL_TIMEOUT_S` (0.4 s) for one before returning a 503. The naive size is `ceil(2000 x 0.010) = 20`.
`pool-sweep` runs the projected peak against sizes from the naive one upward, adopts the smallest that holds the SLO
plus 25 percent, and confirms it with a second run. Every instance calls the ledger with a 0.5 s timeout and turns any
failure into a fast 503.

## Consequences

Adding instances does not enlarge the ledger's pool: the pool, not the CPU, is what saturates once the fleet is big
enough, which is the point. The ledger is also where the backup and restore test has real data to restore.
