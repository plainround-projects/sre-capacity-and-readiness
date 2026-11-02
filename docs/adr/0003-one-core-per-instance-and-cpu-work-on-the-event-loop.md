# 0003. One core per instance, real CPU work on the event loop

**Status:** accepted, 2026-10-27

## Context

The lesson wants a knee that is a real resource limit, found on an instance with fixed CPU and memory limits, "so one
instance's capacity is a stable number", and warns that a `sleep()` stands in for work only by making the knee an
artifact.

## Decision

Each instance is one container with `cpus: 1.0` and 256 MiB. A request runs `cpu_work(rounds)` (a chain of SHA-256,
the rounds set by `WORK_ROUNDS`) on the asyncio event loop, deliberately blocking it. One instance is one
event loop is one core, so its capacity is one over the cost of a request, and the queue that forms behind a busy
loop is the saturation the load test must find. Handling requests in threads would add GIL contention without adding
capacity.

Compose runs 6,000 rounds (about two milliseconds, the lesson's) and the program's own default stays 3,000. A first
run at 3,000 rounds did not give a clean knee on a 12-core laptop with the load generator, Envoy and the other sessions
sharing the Docker VM: p99 rose at 70% of the core, and the generator itself fell behind above 700 requests a second. At
6,000 rounds the knee falls near 500 requests a second, where the generator is comfortable and the queue behind a busy
loop, not jitter, drives p99. Aggregate cost at the projected peak is 2,000 requests a second times two milliseconds,
about four cores, which the VM can give. Every number in the plan comes from measuring, not from the lesson's.

## Consequences

Past the knee an instance's health endpoint also queues (same loop). That is realistic, and the reason the fleet is
sized to stay below the knee rather than to survive above it.
