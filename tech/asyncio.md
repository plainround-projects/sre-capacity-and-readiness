# asyncio (concurrency in Python)

## What it is

`asyncio` is a module in Python's standard library for running many waiting tasks at once without using many threads. Think of a cook who starts the soup, and while it simmers chops vegetables, instead of staring at the pot. The cook is **one thread**; the waiting is the network or the clock.

- An **event loop** is the cook: a loop that runs ready tasks and sleeps until something (a network reply, a timer) wakes one.
- A **coroutine** is a function defined with `async def`. Calling it does not run it; it returns an object the loop can run.
- `await` means "pause me here until this finishes, and let the loop run other things meanwhile."
- A **task** (`asyncio.create_task`) is a coroutine scheduled to run alongside others.

## Why this repository uses it

Three places need it. The **load generator** must send requests on an exact schedule and keep hundreds in flight at once; threads would add their own jitter. The **instance** serves requests and deliberately does its CPU work on the event loop, so one instance is exactly one core and a queue forms behind a busy loop: that queue is the saturation the lesson looks for. The **ledger** awaits database connections from a pool and answers 503 quickly when none is free.

## Where it appears

| File | What it does with asyncio |
| --- | --- |
| `infrastructure/openloop.py` | The open-loop generator: scheduled arrivals, tasks, a `Semaphore`, raw TCP streams, `wait_for`, `gather`. |
| `application/loadtest.py` | The knee test and timed runs `await` the runner and the CPU probe. |
| `interfaces/loadtest_cli.py` | `asyncio.run(...)` runs one experiment. |
| `interfaces/instance_app.py` | `async def work` handler; the CPU work runs on the loop on purpose. |
| `interfaces/ledger_app.py` | `AsyncConnectionPool`; `async with pool.connection()`; `PoolTimeout` becomes a 503. |
| `infrastructure/http_runtime.py` | Runs web apps until a signal arrives. |
| `tests/` | `pytest-asyncio` runs `async def` tests. |

## How it works underneath

1. `asyncio.run(main())` creates a loop, runs `main()` until it ends, and closes the loop.
2. The loop keeps a list of tasks and runs one until it reaches an `await` on something not ready. Then it runs another. **Only one thing runs at a time.**
3. A call that does not `await` (such as `cpu_work`) keeps the loop busy until it returns. Nothing else on that loop moves meanwhile. Most programs avoid that; this one chooses it for the instance so that its capacity is one over the cost of a request.
4. `asyncio.open_connection(host, port)` opens a TCP connection and returns a **reader** and a **writer** (streams): `await reader.readline()` waits for a line, `writer.write(...)` queues bytes and `await writer.drain()` waits until they are sent.
5. A **Semaphore** limits how many tasks may be inside a block at once.
6. `asyncio.wait_for(x, timeout)` waits at most `timeout` seconds and raises `TimeoutError` otherwise.

## Walkthrough of this repository's use

### The schedule: the heart of an open-loop generator

```python
async def run(self, rps: float, duration_s: float) -> tuple[list[Sample], float]:
    """Send `rps * duration_s` requests on schedule; return all samples and the generator's own p99 lag in ms."""
    loop = asyncio.get_running_loop()
    total = int(rps * duration_s)
    start = loop.time()
    samples: list[Sample] = []
    lags: list[float] = []
    tasks: set[asyncio.Task[None]] = set()

    async def one(scheduled: float) -> None:
        ok = await self._request_once()
        samples.append(Sample(scheduled - start, loop.time() - scheduled, ok))

    index = 0
    while index < total:
        now = loop.time()
        due = min(total, int((now - start) * rps) + 1)
        while index < due:
            scheduled = start + index / rps
            lags.append(now - scheduled)
            if len(tasks) >= self._backlog:
                # The service is so far behind that requests are piling up unsent. Count the request as failed
                # now instead of queueing a task for it: thousands of waiting tasks would slow the generator itself.
                samples.append(Sample(scheduled - start, loop.time() - scheduled, False))
            else:
                task = loop.create_task(one(scheduled))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            index += 1
        await asyncio.sleep(max(0.0005, start + index / rps - loop.time()))
    if tasks:
        await asyncio.gather(*tasks)
    return samples, percentile([lag * 1000.0 for lag in lags], 0.99)
```

- `loop.time()` is the loop's monotonic clock in seconds. `start = loop.time()` marks the beginning; request number `i` is **scheduled** at `start + i / rps`.
- `due = min(total, int((now - start) * rps) + 1)` computes how many requests should have been sent by now. The inner `while index < due:` sends all that are due: if the generator woke up late, it sends the missed ones immediately instead of dropping them, and records how late each was (`lags.append(now - scheduled)`).
- Each request is its own task (`loop.create_task(one(scheduled))`), not awaited here: the loop keeps scheduling no matter how slow the responses are. This is **open loop**. `tasks.add(task)` and `task.add_done_callback(tasks.discard)` keep a reference until the task ends.
- `await asyncio.sleep(max(0.0005, start + index / rps - loop.time()))` sleeps until the next request is due (at least half a millisecond).
- `one(scheduled)` records `Sample(scheduled - start, loop.time() - scheduled, ok)`: the latency is measured from the **scheduled** time, not from when the request was actually sent. If the generator, the network or the service queued the request, that wait is in the number. A closed-loop generator would hide it.
- At the end `await asyncio.gather(*tasks)` waits for the requests still in flight, and the method returns the samples and the generator's own p99 lag (how late it sent), which the load-test code uses to warn that the generator itself was the bottleneck.

### Raw streams instead of a library

```python
async def _request_once(self) -> bool:
    async with self._limit:
        connection: _Connection | None = None
        try:
            connection = self._idle.pop() if self._idle else await self._connect()
            status, keep_alive = await asyncio.wait_for(self._exchange(connection), self._timeout_s)
        except (OSError, asyncio.IncompleteReadError, TimeoutError, ValueError):
            if connection is not None:
                connection.writer.close()
            return False
        if keep_alive:
            self._idle.append(connection)
        else:
            connection.writer.close()
        return status == 200

async def _connect(self) -> _Connection:
    reader, writer = await asyncio.wait_for(asyncio.open_connection(self._host, self._port), self._timeout_s)
    return _Connection(reader, writer)

async def _exchange(self, connection: _Connection) -> tuple[int, bool]:
    connection.writer.write(self._request)
    await connection.writer.drain()
    status = int((await connection.reader.readline()).split()[1])
    length, keep_alive = 0, True
    while True:
        line = await connection.reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        name, _, value = line.decode("latin-1").partition(":")
        if name.lower() == "content-length":
            length = int(value.strip())
        elif name.lower() == "connection" and value.strip().lower() == "close":
            keep_alive = False
    if length:
        await connection.reader.readexactly(length)
    return status, keep_alive
```

- `async with self._limit:` takes a permit from the `Semaphore(max_connections)`; a request that must wait for a permit still counts that wait in its latency.
- `self._idle.pop() if self._idle else await self._connect()` reuses an idle keep-alive connection, or opens a new one. `deque` is a double-ended queue.
- `asyncio.wait_for(self._exchange(connection), self._timeout_s)` bounds one exchange.
- `_exchange` writes the HTTP request, reads the status line (`int(... .split()[1])` is the status code), reads headers until a blank line (`b"\r\n"`), notes `content-length` and whether the server asked to close, and then `readexactly(length)` reads the body. HTTP details are in the HTTP note.
- `except (OSError, asyncio.IncompleteReadError, TimeoutError, ValueError)` turns any network problem into "this request failed" and closes the connection instead of reusing it.

### The instance: CPU work on the loop, on purpose

```python
async def work(request: web.Request) -> web.Response:
    """GET /work: burn CPU for the configured number of rounds, then record an entry in the ledger if one is configured.

    The CPU work runs on the event loop on purpose. One instance is one core, so its capacity is one over the cost
    of a request, and a queue that forms behind a busy loop is exactly the saturation the load test must find.
    """
    app = request.app
    settings = app[SETTINGS]
    started = time.perf_counter()
    status, body = 200, {"zone": settings.zone, "instance": settings.instance}
    with app[METRICS].inflight().track_inprogress():
        body["digest"] = cpu_work(settings.work_rounds)[:8]
        if settings.ledger_url is not None:
            status, body = await _record(app, settings.ledger_url, status, body)
    app[METRICS].observe(status, time.perf_counter() - started)
    return web.json_response(
        body,
        status=status,
        headers={"X-Zone": settings.zone, "X-Instance": settings.instance, "X-Version": settings.version},
    )


async def _record(
    app: web.Application, ledger_url: str, status: int, body: dict[str, str]
) -> tuple[int, dict[str, str]]:
    """One bounded call to the ledger. A refusal, a timeout or a non-200 answer is a fast 503, never a hang."""
    payload = {"account": app[RNG].randint(1, 100), "amount_cents": app[RNG].randint(1, 10_000)}
    try:
        async with app[SESSION].post(f"{ledger_url}/entries", json=payload) as response:
            if response.status == 200:
                return status, body
            return 503, {**body, "error": f"ledger answered {response.status}"}
    except (aiohttp.ClientError, TimeoutError):
        return 503, {**body, "error": "ledger unavailable"}
```

`work` is an `async def` handler, but `cpu_work(settings.work_rounds)` is an ordinary function call that takes about a millisecond. While it runs, the loop serves nothing else. The docstring explains the choice. With a ledger configured, `await _record(...)` makes one bounded call to the ledger; a refusal, a timeout or a non-200 answer becomes a fast 503, never a hang. `with app[METRICS].inflight().track_inprogress():` counts requests in progress.

### The ledger: an async connection pool

```python
async def post_entry(request: web.Request) -> web.Response:
    """POST /entries {account, amount_cents} -> 200 {id}; 503 when no pooled connection frees up in time."""
    app = request.app
    try:
        body: dict[str, Any] = await request.json()
        params = {"hold": app[HOLD_S], "account": int(body["account"]), "amount": int(body["amount_cents"])}
    except (ValueError, KeyError, TypeError):
        app[METRICS].request(400)
        return web.json_response({"error": "send JSON with account and amount_cents"}, status=400)
    try:
        async with app[POOL].connection() as connection:
            cursor = await connection.execute(_INSERT, params)
            row = await cursor.fetchone()
    except PoolTimeout:
        app[METRICS].timeout()
        app[METRICS].request(503)
        return web.json_response({"error": "ledger busy: pool exhausted"}, status=503)
    except Exception:
        log.exception("ledger insert failed")
        app[METRICS].request(500)
        return web.json_response({"error": "internal error"}, status=500)
    app[METRICS].request(200)
    return web.json_response({"id": row[0] if row else None})
```

`async with app[POOL].connection() as connection:` borrows a pooled connection and returns it at the end of the block, even on error. If none is free within the pool's timeout, `PoolTimeout` is raised; the handler counts it and answers 503 `ledger busy: pool exhausted`.

### Serving and shutdown

```python
async def serve(
    apps: Sequence[tuple[web.Application, int]],
    stop: asyncio.Event | None = None,
    shutdown_timeout: float = 10.0,
    host: str = "0.0.0.0",
    reuse_port: bool = False,
    drain_s: float = 0.0,
    on_drain: Callable[[], None] | None = None,
) -> None:
    """Start every application on its port and block until `stop` is set or a termination signal arrives.

    On the way out the process first says it is draining (`on_drain`) and keeps serving for `drain_s` seconds, so a
    load balancer's health checks can take it out of rotation before it stops accepting connections.
    """
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    runners: list[web.AppRunner] = []
    try:
        for app, port in apps:
            runner = web.AppRunner(app, access_log=None, shutdown_timeout=shutdown_timeout)
            await runner.setup()
            runners.append(runner)
            await web.TCPSite(runner, host, port, reuse_port=reuse_port).start()
            log.info("listening", extra={"port": port})
        await stop.wait()
        log.info("draining", extra={"seconds": drain_s})
        if on_drain is not None:
            on_drain()
        await asyncio.sleep(drain_s)
        log.info("shutting down")
    finally:
        for runner in runners:
            await runner.cleanup()
```

An `asyncio.Event` is a flag tasks can wait on. SIGTERM and SIGINT call `stop.set()`; `await stop.wait()` ends; `finally:` cleans up each runner, with `shutdown_timeout` letting in-flight requests finish. The compose file's `stop_grace_period: 15s` gives that time before Docker kills the process.

### The experiment orchestration

```python
async def knee_test(
    runner: OpenLoopRunner,
    probe: CpuProbe | None,
    steps_rps: list[float],
    step_seconds: float,
    margin: float = 0.25,
) -> KneeReport:
    """Step the load up, record each step, stop once the knee is clearly passed, and choose the safe load below it.

    The search stops once two steps beyond a knee that has persisted have been measured, which is enough to see the
    shape on both sides of it without driving the instance into the ground. A spike that goes away is not a knee.
    """
    results: list[StepResult] = []
    worst_lag = 0.0
    for rps in steps_rps:
        before = await probe.cpu_seconds() if probe else None
        samples, lag = await runner.run(rps, step_seconds)
        after = await probe.cpu_seconds() if probe else None
        utilization = None if before is None or after is None else (after - before) / step_seconds
        step = summarize(samples, rps, step_seconds, utilization)
        if len(results) >= BASELINE_STEPS and looks_broken(step, baseline_p99(results)):
            step = await _retry(runner, probe, step, rps, step_seconds)
        results.append(step)
        worst_lag = max(worst_lag, lag)
        found = _knee_so_far(results)
        if found is not None and sum(1 for step in results if step.target_rps > found.knee_rps) >= 2:
            break
    try:
        knee = find_knee(results)
    except NoKneeError:
        return KneeReport(tuple(results), None, None, margin, worst_lag)
    return KneeReport(tuple(results), knee, safe_load(knee, margin), margin, worst_lag)

```

`await probe.cpu_seconds()` reads the instance's CPU counter before and after each step; `(after - before) / step_seconds` is the CPU used per second: 1.0 means the whole core.

## Try it yourself

1. `.venv/bin/python -c "import asyncio; print(asyncio.run(asyncio.sleep(0.1, 'done')))"` prints `done`.
2. With the fleet up: `docker compose stop app-a1`, then `docker compose logs app-a1 | tail -3`: the last lines include `shutting down` (the SIGTERM handler ran).
3. `.venv/bin/python -m pytest tests/integration/test_instance_and_loadgen.py -q` runs the generator against a real instance in one event loop.
4. `docker compose run --rm -T loadtest soak --target http://app-a1:8080 --rps 50 --seconds 5 --label demo` runs a 5-second open-loop test and prints one JSON document with `generator_lag_p99_ms` in it (it should be a few milliseconds on a quiet machine).
5. Compare `generator_lag_p99_ms` with `p99_ms` in the report: if the lag is large, the number measures the generator, not the service.

## Mistakes people make

- Closed-loop load generation (send, wait, send): it can never show queueing.
- Measuring latency from the moment of sending, not from the scheduled arrival.
- Running CPU-heavy work on an event loop *by accident*. Here it is deliberate and documented.
- Creating tasks and losing the reference; they may be garbage-collected. The generator keeps them in a set.
- Trusting a load test run on a machine that is busy with other work.

## Check yourself

1. What makes the generator open loop?
   *Answer: requests are scheduled by the clock and started as tasks without waiting for earlier responses.*
2. What is latency measured from, and why?
   *Answer: from the scheduled arrival, so any queue (generator, network or service) shows up in the number.*
3. Why does the instance burn CPU on the event loop?
   *Answer: so one instance is one core and a queue forms when it is saturated, which is what the knee test must find.*
4. What does the `Semaphore` in the runner limit?
   *Answer: how many requests are in flight at once.*
5. What does `await asyncio.gather(*tasks)` do at the end of a run?
   *Answer: waits for all requests still in flight.*

## Further reading

- `asyncio` (Python 3.12): <https://docs.python.org/3.12/library/asyncio.html>
- Streams: <https://docs.python.org/3.12/library/asyncio-stream.html>
- Synchronization primitives (`Semaphore`, `Event`): <https://docs.python.org/3.12/library/asyncio-sync.html>
