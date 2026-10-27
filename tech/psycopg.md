# psycopg 3.2.9 and psycopg_pool 3.2.6

## What it is

**psycopg** (version 3, imported as `psycopg`) is the Python library for talking to PostgreSQL. It opens a **connection** to the server, sends SQL and returns rows as tuples. `psycopg[binary]` also installs `psycopg-binary`, a pre-built copy of the C library it needs, so no compiler is required.

**psycopg_pool** is a separate package from the same project that provides **connection pools**. A pool opens a fixed number of connections once and lends them out: `async with pool.connection() as connection:` borrows one and returns it at the end of the block. If all are in use, a caller **waits**, up to a timeout, and then gets an error (`PoolTimeout`).

## Why this repository uses it

The lesson's second bottleneck is a shared connection pool. The ledger service needs a pool whose size is a visible setting (`POOL_SIZE`), whose wait time is bounded (`POOL_TIMEOUT_S`), and whose statistics (in use, waiting) can be exported. psycopg_pool's `AsyncConnectionPool` does all of that and is what a production service would use. (The previous project of the track, `sre-game-day`, wrote a pool by hand to show the mechanism; this one uses the library's pool and measures it.)

## Where it appears

| File | Use |
| --- | --- |
| `interfaces/ledger_app.py` | Creates the `AsyncConnectionPool`, borrows connections per request, maps `PoolTimeout` to a 503, reads `pool.get_stats()` for the gauges. |
| `infrastructure/settings.py` | `DATABASE_URL`, `POOL_SIZE`, `POOL_TIMEOUT_S`, `HOLD_MS` for the ledger. |
| `docker-compose.yml` | The ledger service's environment. |
| `pyproject.toml`, `requirements*.lock` | Pin `psycopg[binary]==3.2.9`, `psycopg-binary==3.2.9` and `psycopg-pool==3.2.6`. |
| `pyproject.toml` | The import-linter contract forbids `psycopg` and `psycopg_pool` in the domain and application layers. |

## How it works underneath

- `AsyncConnectionPool(conninfo, min_size, max_size, timeout, kwargs, open)` keeps between `min_size` and `max_size` open connections. With `min_size == max_size` the pool has a **fixed size** and opens that many connections up front. `timeout` is how long `pool.connection()` waits for a free connection before raising `PoolTimeout`.
- `kwargs={"autocommit": True}` is passed to every connection: each statement commits immediately and no transaction stays open on an idle pooled connection.
- `open=False` creates the pool object without opening connections; `await pool.open(wait=True, timeout=30)` opens them and waits until they are ready.
- `pool.get_stats()` returns a dictionary of numbers, including `pool_size` (connections currently in the pool), `pool_available` (connections idle in the pool) and `requests_waiting` (callers waiting for a connection). In use = size − available.
- `await connection.execute(sql, params)` runs a statement and returns a cursor; `await cursor.fetchone()` returns the next row or `None`. `%(name)s` placeholders take values from a dictionary.

## Walkthrough of this repository's use

### Creating and closing the pool

```python
async def run(settings: LedgerSettings) -> None:
    """Open the pool, serve until told to stop, then close the pool."""
    pool = AsyncConnectionPool(
        settings.database_url,
        min_size=settings.pool_size,
        max_size=settings.pool_size,
        timeout=settings.pool_timeout_s,
        kwargs={"autocommit": True},
        open=False,
    )
    await pool.open(wait=True, timeout=30)

    def in_use() -> float:
        stats = pool.get_stats()
        return float(stats["pool_size"] - stats["pool_available"])

    metrics = LedgerMetrics(in_use, lambda: float(pool.get_stats()["requests_waiting"]), settings.pool_size)

    async def ready() -> bool:
        try:
            async with pool.connection(timeout=1.0) as connection:
                await connection.execute("SELECT 1")
        except Exception:
            return False
        return True

    try:
        await serve(
            [
                (build_app(pool, metrics, settings.hold_ms / 1000.0), settings.port),
                (build_ops_app(metrics.render, ready), settings.ops_port),
            ]
        )
    finally:
        await pool.close()

```

- The pool is created with `min_size=max_size=settings.pool_size` (20 by default), `timeout=settings.pool_timeout_s` (0.4 s), `kwargs={"autocommit": True}` and `open=False`. `await pool.open(wait=True, timeout=30)` then opens all the connections and waits up to 30 seconds.
- `in_use()` computes `stats["pool_size"] - stats["pool_available"]` and the second lambda returns `stats["requests_waiting"]`. They are handed to `LedgerMetrics`, which exports them as gauges evaluated at scrape time (see the prometheus-client note).
- `ready()` is the readiness check: borrow a connection with a 1-second timeout and run `SELECT 1`; any error means "not ready".
- `try: await serve(...) finally: await pool.close()` closes the connections when the service stops.

### Using it per request

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

`async with app[POOL].connection() as connection:` borrows a connection. If none is available within 0.4 s, `PoolTimeout` is raised, the ledger counts it (`ledger_pool_timeouts_total`) and answers 503. The block's end returns the connection even if an exception happened.

### The numbers that matter

Each request holds a connection for `HOLD_MS` (10 ms) plus the insert time. With 20 connections the ledger can complete roughly 20 / 0.010 = 2,000 requests a second at best: the projected peak. A pool sized exactly at that product has no headroom; the pool sweep (`make pool-sweep`) steps the size up until the SLO holds, and `docs/capacity-plan.md` records both the naive size that failed and the size that held. Waiting requests (`ledger_pool_waiting`) climbing while `ledger_pool_in_use` sits at the pool size is the signature of exhaustion; the runbook `docs/runbooks/pool-exhaustion.md` says what to do.

### Settings

```python
class LedgerSettings:
    """Settings of the shared ledger service and its connection pool, the dependency every instance calls."""

    database_url: str
    pool_size: int
    pool_timeout_s: float
    hold_ms: float
    port: int
    ops_port: int
    log_level: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> LedgerSettings:
        """Read and validate. DATABASE_URL has no default."""
        url = env.get("DATABASE_URL", "").strip()
        if not url.startswith("postgresql://"):
            raise SettingsError("DATABASE_URL must be set to a postgresql:// URL")
        return cls(
            database_url=url,
            pool_size=int(_number(env, "POOL_SIZE", 20, 1, 90)),
            pool_timeout_s=_number(env, "POOL_TIMEOUT_S", 0.4, 0.01, 30.0),
            hold_ms=_number(env, "HOLD_MS", 10.0, 0.0, 1000.0),
            port=_port(env, "PORT", 8082),
            ops_port=_port(env, "OPS_PORT", 9102),
            log_level=_text(env, "LOG_LEVEL", "INFO").upper(),
        )
```

`DATABASE_URL` is the connection string (user, password, host `db`, port, database); the ledger uses the limited role `ledger_app`.

## Try it yourself

1. `.venv/bin/python -c "import psycopg, psycopg_pool; print(psycopg.__version__, psycopg_pool.__version__)"` prints `3.2.9 3.2.6`.
2. With the fleet up: `curl -s localhost:47284/metrics | grep ledger_pool` prints `ledger_pool_size 20.0`, `ledger_pool_in_use` and `ledger_pool_waiting`.
3. `make pool-sweep` (needs the fleet and about a minute) steps the pool size and writes `docs/evidence/pool-sweep.json`.
4. `echo "SELECT state, count(*) FROM pg_stat_activity WHERE usename = 'ledger_app' GROUP BY state;" | make psql` shows the ledger's 20 connections, mostly `idle`.
5. `.venv/bin/python -m pytest tests/integration -q` runs the integration tests, which use a stand-in ledger (no database).

## Mistakes people make

- A pool with no timeout: callers pile up forever. Here the wait is bounded and ends in a fast 503.
- Sizing the pool without the server's total connection limit in mind.
- Leaving a transaction open on a pooled connection. Here `autocommit` is on.
- Forgetting to close the pool at shutdown.
- Believing more service instances raise the ledger's capacity. They share the pool.

## Check yourself

1. What does `min_size == max_size` mean?
   *Answer: a fixed-size pool that opens all its connections up front.*
2. What happens when the pool is exhausted?
   *Answer: callers wait up to the timeout; then `PoolTimeout` is raised and the ledger answers 503.*
3. How is "in use" computed?
   *Answer: `pool_size - pool_available` from `pool.get_stats()`.*
4. Why `autocommit: True`?
   *Answer: no transaction is left open on an idle pooled connection.*
5. What limits the ledger's throughput?
   *Answer: pool size divided by the time each request holds a connection.*

## Further reading

- psycopg 3 documentation: <https://www.psycopg.org/psycopg3/docs/>
- psycopg_pool: <https://www.psycopg.org/psycopg3/docs/advanced/pool.html>
- Connection strings: <https://www.postgresql.org/docs/18/libpq-connect.html#LIBPQ-CONNSTRING>
