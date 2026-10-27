# 0001. Open-loop load, latency from the scheduled arrival

**Status:** accepted, 2026-10-27

## Context

A load test that sends the next request when the last one returns (closed loop) slows down exactly when the system
does, so it can never build a queue and never shows the knee. The lesson asks for an open-loop generator and warns
that short steps hide queues.

## Decision

`infrastructure/openloop.py` schedules request *i* to arrive at `start + i / rps`. It sends the request when its turn
comes whether or not earlier ones have returned, and it measures latency from the scheduled arrival to the end of the
response. A request that had to wait for a free connection, or for the generator's own event loop, counts that wait.
The generator also reports its own lag (how late it was at p99), and the CLI warns when it was more than 5 ms late,
because then the generator, not the service, is in the number.

The generator speaks HTTP/1.1 directly over asyncio streams (keep-alive pool of up to 512 connections) rather than
through an HTTP client library: at 2,000 requests a second a general client's overhead competes with what is being
measured. It runs inside the compose network (`docker compose run loadtest`), so a laptop's port forwarding is not
in the measurement. Steps last 10 seconds by default so `make run` finishes in minutes; `STEP_SECONDS=120` and
`SOAK_SECONDS=1800` give the lesson's real durations.

## Consequences

The knee test names the resource from independent evidence: it reads the instance's `process_cpu_seconds_total`
before and after each step, so "CPU was 99% of its one core at the knee" is a measurement and not an inference from
throughput flattening.
