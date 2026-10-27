# 0003. One core per instance, real CPU work on the event loop

**Status:** accepted, 2026-10-27

## Context

The lesson wants a knee that is a real resource limit, found on an instance with fixed CPU and memory limits, "so one
instance's capacity is a stable number", and warns that a `sleep()` stands in for work only by making the knee an
artifact.

## Decision

Each instance is one container with `cpus: 1.0` and 256 MiB. A request runs `cpu_work(rounds)` (a chain of SHA-256,
3,000 rounds by default, about a millisecond) on the asyncio event loop, deliberately blocking it. One instance is one
event loop is one core, so its capacity is one over the cost of a request, and the queue that forms behind a busy
loop is the saturation the load test must find. Handling requests in threads would add GIL contention without adding
capacity.

The default is 3,000 rounds rather than the lesson's 6,000 so that the projected-peak fleet test (2,000 requests a
second through five to eight instances, a load balancer, a ledger and PostgreSQL, and the load generator) fits on a
12-core laptop. `WORK_ROUNDS=6000` restores the lesson's cost; every number in the plan comes from measuring, not
from the lesson's.

## Consequences

Past the knee an instance's health endpoint also queues (same loop). That is realistic, and the reason the fleet is
sized to stay below the knee rather than to survive above it.
