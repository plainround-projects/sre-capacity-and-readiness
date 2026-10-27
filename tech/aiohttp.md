# aiohttp 3.14

## What it is

aiohttp is a Python library for HTTP. It has two halves: a **server** side (you register functions for URLs and it runs them when requests arrive) and a **client** side (you send requests to other servers). Both are built on `asyncio` (see the asyncio note).

## Why this repository uses it

Two small web services are needed: the **instance** (a request handler that burns CPU and optionally calls the ledger) and the **ledger** (a handler that holds a pooled database connection for a while). The instance also needs an HTTP **client** to call the ledger with a hard time limit. aiohttp covers server and client in one small library, and its test server lets the tests run real HTTP on a free port. The load generator itself does *not* use aiohttp: it speaks HTTP over raw streams to avoid its own overhead (see the asyncio note).

## Where it appears

| File | Use |
| --- | --- |
| `interfaces/instance_app.py` | Server: `GET /work`. Client: `ClientSession` with a total timeout, to `POST /entries` on the ledger. |
| `interfaces/ledger_app.py` | Server: `POST /entries`, with a psycopg connection pool behind it. |
| `infrastructure/ops.py` | A small server with `/healthz`, `/readyz`, `/metrics` on each process's ops port. |
| `infrastructure/http_runtime.py` | Starts applications with `AppRunner` and `TCPSite`. |
| `tests/integration/test_instance_and_loadgen.py` | `aiohttp.test_utils.TestServer` starts real servers on free ports. |

## How it works underneath

- A **web.Application** holds a **router** (a table from method + path to handler function). A **handler** is `async def handler(request) -> response`.
- A **request** object has the method, path, query string, headers and body (`await request.json()`). A **response** carries a status code, headers and a body (`web.json_response(...)`).
- The server listens on a **TCP socket** (address plus port). When a connection arrives, aiohttp reads the HTTP request, finds the handler, awaits it and writes the response. Many handlers can wait at once, but they share one thread (see the asyncio note).
- A **ClientSession** keeps a pool of connections to servers, so repeated calls reuse them. `ClientTimeout(total=..., connect=...)` bounds a whole call and the connect step.
- **AppKey** is a typed key for storing things on the application: `app[SETTINGS]`.

## Walkthrough of this repository's use

### The instance's one route

```python
log = logging.getLogger(__name__)
SETTINGS = web.AppKey("settings", InstanceSettings)
METRICS = web.AppKey("metrics", InstanceMetrics)
SESSION = web.AppKey("session", aiohttp.ClientSession)
RNG = web.AppKey("rng", random.Random)


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
```

- The `AppKey(...)` lines declare typed keys for settings, metrics, the HTTP session and a random generator.
- `work` builds a small body (`zone`, `instance`) and, after the CPU work, a `digest`: the first eight characters of the SHA-256 chain, so every request really computes something. It returns `web.json_response(body, status=status, headers={...})` with three response headers: `X-Zone`, `X-Instance` and `X-Version`. Those headers are how the experiments can tell which instance and which release answered (the rollout test checks `X-Version`).

### Calling the ledger with a hard limit

```python
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


def build_app(settings: InstanceSettings, metrics: InstanceMetrics) -> web.Application:
    """The public application; the ledger session opens and closes with it."""
    app = web.Application()
    app[SETTINGS], app[METRICS], app[RNG] = settings, metrics, random.Random()

    async def session_context(application: web.Application) -> AsyncIterator[None]:
        timeout = aiohttp.ClientTimeout(total=settings.ledger_timeout_s, connect=settings.ledger_timeout_s)
        application[SESSION] = aiohttp.ClientSession(timeout=timeout, connector=aiohttp.TCPConnector(limit=0))
        yield
        await application[SESSION].close()

    app.cleanup_ctx.append(session_context)
    app.router.add_get("/work", work)
    return app
```

`_record` posts `{"account": ..., "amount_cents": ...}` with `session.post(url, json=payload)` (`json=` encodes the body and sets the content type). It returns the instance's own status when the ledger says 200, and otherwise a **fast 503**: a non-200 answer becomes `ledger answered N`, and a client error or timeout becomes `ledger unavailable`. The session's `ClientTimeout(total=settings.ledger_timeout_s, connect=settings.ledger_timeout_s)` makes the longest wait 0.5 s by default. This is the dependency-failure behaviour the readiness review checks: with the ledger frozen, requests fail fast and the instance stays healthy. `TCPConnector(limit=0)` removes the default cap of 100 simultaneous connections.

`session_context` is an async generator used as a **cleanup context**: before `yield` it opens the session at start-up, after `yield` it closes it at shutdown.

### The ledger's route

```python
# One statement: hold the connection for the configured time (a stand-in for a heavier query), then append the entry.
_INSERT = (
    "WITH slept AS (SELECT pg_sleep(%(hold)s)) "
    "INSERT INTO entries (account_id, amount_cents) SELECT %(account)s, %(amount)s FROM slept RETURNING id"
)


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


def build_app(pool: AsyncConnectionPool, metrics: LedgerMetrics, hold_s: float) -> web.Application:
    """The public application."""
    app = web.Application()
    app[POOL], app[METRICS], app[HOLD_S] = pool, metrics, hold_s
    app.router.add_post("/entries", post_entry)
    return app
```

`post_entry` parses the JSON body, returns 400 for malformed input, borrows a pooled connection, runs `_INSERT`, and returns `{"id": ...}`. `_INSERT` is one SQL statement: `WITH slept AS (SELECT pg_sleep(%(hold)s)) INSERT ... SELECT ... FROM slept RETURNING id`. `pg_sleep(0.01)` makes the database hold the connection for 10 ms before the insert happens (a stand-in for a heavier query). `PoolTimeout` becomes 503; any other error is logged with its traceback and answered 500.

### The ops app

```python
def build_ops_app(
    render_metrics: Callable[[], bytes], is_ready: Callable[[], Awaitable[bool]] | None = None
) -> web.Application:
    """`/healthz` says the process is alive, `/readyz` that its dependency answers, `/metrics` the Prometheus text."""
    app = web.Application()

    async def healthz(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def readyz(_: web.Request) -> web.Response:
        ready = await is_ready() if is_ready else True
        return web.json_response({"status": "ready" if ready else "not ready"}, status=200 if ready else 503)

    async def metrics(_: web.Request) -> web.Response:
        return web.Response(body=render_metrics(), content_type="text/plain", charset="utf-8")

    app.router.add_get("/healthz", healthz)
    app.router.add_get("/readyz", readyz)
    app.router.add_get("/metrics", metrics)
    return app
```

Three endpoints: `/healthz` (alive), `/readyz` (runs an injected readiness check; the ledger's check runs `SELECT 1` on a pooled connection) and `/metrics` (Prometheus text).

### Test servers

```python
def settings(ledger_url=None, rounds=200, timeout=0.3):
    return InstanceSettings("b", "b2", "v7", rounds, ledger_url, timeout, 0, 0, "INFO")


async def start(app):
    server = TestServer(app)
    await server.start_server()
    return server


@pytest.fixture
async def instance():
    metrics = InstanceMetrics("b", "b2")
    server = await start(instance_app.build_app(settings(), metrics))
    ops = await start(build_ops_app(metrics.render))
    yield server, ops, metrics
    await server.close()
    await ops.close()


async def get(url):
    async with aiohttp.ClientSession() as session, session.get(url) as response:
        return response.status, dict(response.headers), await response.text()


```

`TestServer(app)` starts a real server on a free port in the test process; the test then talks to it with a real client or with the project's own load generator. The `instance` fixture builds the real instance app and its ops app.

## Try it yourself

1. With the fleet up: `curl -i localhost:47280/work` shows `200`, the JSON body, and lower-case `x-zone`, `x-instance`, `x-version` headers (Envoy lower-cases header names).
2. `curl -s localhost:47282/work` talks to `app-a1` directly (its port is published).
3. `curl -s localhost:47283/metrics | grep app_requests_total` shows the counters on the instance's ops port.
4. `curl -i localhost:47283/healthz` returns `200` and `{"status": "ok"}`.
5. `curl -s localhost:47284/metrics | grep ledger_pool` shows the ledger's pool gauges (its ops port is published; its service port is not, so you reach `/entries` only through an instance).

## Mistakes people make

- Blocking calls inside handlers (apart from the deliberate CPU work in the instance).
- Forgetting to close a `ClientSession`.
- No timeout on a call to a dependency: one slow ledger would hang every instance. Here every call has a hard limit.
- Returning 500 for a dependency problem. A 503 says "unavailable, try again" and is counted as an availability failure by the SLO.

## Check yourself

1. What does `X-Version` let the rollout test do?
   *Answer: tell which release answered a request.*
2. What happens when the ledger does not answer in time?
   *Answer: the instance returns a fast 503 `ledger unavailable` instead of hanging.*
3. What does `TCPConnector(limit=0)` change?
   *Answer: it removes the cap on simultaneous connections.*
4. How does the instance behave with no `LEDGER_URL`?
   *Answer: it does only the CPU work and returns 200.*
5. Why does the ledger insert use `pg_sleep`?
   *Answer: to hold the pooled connection for a known time so the pool becomes the next bottleneck.*

## Further reading

- aiohttp 3.14 docs: <https://docs.aiohttp.org/en/v3.14.5/>
