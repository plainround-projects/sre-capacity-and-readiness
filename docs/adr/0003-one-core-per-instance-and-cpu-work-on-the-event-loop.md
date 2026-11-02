# 0003. One core per instance, real CPU work on the event loop

**Status:** accepted, 2026-10-27

## Context

The lesson wants a knee that is a real resource limit, found on an instance with fixed CPU and memory limits, "so one
instance's capacity is a stable number", and warns that a `sleep()` stands in for work only by making the knee an
artifact.

## Decision

Each instance is one container with 256 MiB, pinned to a vCPU of its own (`cpuset`), and the other services share
three further cores. A first version used a `cpus: 1.0` quota instead; a quota throttles a busy container in bursts
(`nr_throttled` rose in 8 of 103 periods at 73% use, with p99 at 58 ms), so latency jumped long before the core was
full. With a core of its own, 100% of the core means the core is full, and the load test names it as the resource.

A request runs `cpu_work(rounds)` (a chain of SHA-256, the rounds set by `WORK_ROUNDS`) on the asyncio event loop,
deliberately blocking it. One instance is one event loop is one core, so its capacity is one over the cost of a
request, and the queue that forms behind a busy loop is the saturation the load test must find. Handling requests in
threads would add GIL contention without adding capacity.

Compose runs 6,000 rounds (about two milliseconds, the lesson's); the program's own default stays 3,000. At one
millisecond a request the knee moved to 1,400 requests a second and the safe load ran the core at 88%, where a
stall of a second on the host took seconds to drain. At two milliseconds the knee falls near 600 and the safe load
leaves room to recover. Aggregate cost at the projected peak is 2,000 requests a second times two milliseconds, about
four cores of the 12 the Docker VM has. Every number in the plan comes from measuring, not from the lesson's.

## Consequences

Past the knee an instance's health endpoint also queues (same loop). That is realistic, and the reason the fleet is
sized to stay below the knee rather than to survive above it.
