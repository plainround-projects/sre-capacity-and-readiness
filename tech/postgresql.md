# PostgreSQL 18

## What it is

PostgreSQL ("Postgres") is a database **server**: a program that runs all the time, keeps tables on disk, and answers requests from other programs over the network (default port 5432). Programs connect, authenticate as a **role**, send SQL, and read results. Each connection costs the server memory and a process, so servers limit how many connections they accept (by default 100).

## Why this repository uses it

The lesson's second bottleneck is a **shared** resource that adding instances does not enlarge: the ledger's connection pool in front of one database. A real PostgreSQL gives real connections, a real limit, and real tools to back up and restore, which the readiness review demands evidence for: "a restore from backup has actually been tested".

## Where it appears

| File | Use |
| --- | --- |
| `docker-compose.yml` | The `db` service, image `postgres:18`, with a health check and the init files. |
| `sql/000_roles.sh`, `001_schema.sql`, `002_seed.sql` | Run by the image on first start (see the Bash and SQL notes). |
| `sql/003_queries.sql` | Queries for looking around, including `pg_stat_activity`. |
| `src/capacity/interfaces/ledger_app.py` | The ledger's pool of connections to the server (see the psycopg note). |
| `src/capacity/interfaces/experiments_cli.py` | The restore experiment drives `pg_dump`, `psql` and `pg_restore`; the dependency experiment freezes the ledger. |
| `Makefile` | `psql` opens the client inside the `db` container. |
| `.env.example` | `POSTGRES_USER`, `POSTGRES_PASSWORD`, `LEDGER_DB_PASSWORD`, `DB_PORT`, `POOL_SIZE`, `POOL_TIMEOUT_S`, `HOLD_MS`. |

## How it works underneath

- The `postgres` server process listens on port 5432. For each client connection it starts a **backend process**; that is why connections are expensive and why a pool reuses them.
- The server starts with an empty **data directory** the first time. The official Docker image then runs the files in `/docker-entrypoint-initdb.d/` and starts normally.
- **Backup and restore.** `pg_dump -Fc` writes a database to a single compressed file in "custom format". `pg_restore` reads such a file into another database. A restore is only proven when the restored data is compared with the source.
- `pg_stat_activity` is a view with one row per backend process: user, state (`active`, `idle`, ...).
- `pg_isready` asks the server whether it accepts connections; the health check uses it.

## Walkthrough of this repository's use

### The service

```yaml
db:
  image: postgres:18
  cpuset: "9-11"
  # The ledger writes an entry per request, 2,000 times a second at the projected peak. Waiting for a disk flush on
  # every commit stalled requests for 100 to 400 ms on a laptop's virtual disk and hid the pool; the entries are
  # test data, so a commit may return before its flush.
  command: ["postgres", "-c", "synchronous_commit=off"]
  restart: unless-stopped
  environment:
    POSTGRES_DB: ledger
    POSTGRES_USER: ${POSTGRES_USER:-postgres}
    POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-local-dev-only}
    LEDGER_DB_PASSWORD: ${LEDGER_DB_PASSWORD:-local-dev-only-app}
  volumes:
    - ./sql/000_roles.sh:/docker-entrypoint-initdb.d/000_roles.sh:ro
    - ./sql/001_schema.sql:/docker-entrypoint-initdb.d/001_schema.sql:ro
    - ./sql/002_seed.sql:/docker-entrypoint-initdb.d/002_seed.sql:ro
  ports:
    - "127.0.0.1:${DB_PORT:-47232}:5432"
  healthcheck:
    test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-postgres} -d ledger"]
    interval: 3s
    timeout: 5s
    retries: 20

```

- `image: postgres:18` pins the major version.
- `POSTGRES_DB: ledger` has the image create the database; `POSTGRES_USER` and `POSTGRES_PASSWORD` create the administrator (local placeholders by default). `LEDGER_DB_PASSWORD` is read by the init script for the limited role.
- The three read-only mounts place the init files in the start-up folder.
- Port mapping `127.0.0.1:47232:5432` lets only your own computer connect.
- The health check is `pg_isready -U ... -d ledger` every 3 seconds, up to 20 tries.
- There is no volume declared: the data lives and dies with the container, so every `make up` starts from the same seeded 5,000 entries.

### Backup and restore, proven

```python
def restore(compose: Compose, evidence: JsonEvidence, rto_target_s: float) -> dict[str, Any]:
    """Back the ledger up, restore the copy into a scratch database and compare it with the source row by row."""
    query = (
        "SELECT count(*) || ' ' || md5(string_agg(id || ':' || account_id || ':' || amount_cents, ',' ORDER BY id)) "
        "FROM entries"
    )

    def fingerprint(database: str) -> tuple[int, str]:
        rows, checksum = compose.db("psql", "-U", "postgres", "-d", database, "-At", "-c", query).decode().split()
        return int(rows), checksum

    source = fingerprint("ledger")
    started = time.perf_counter()
    dump = compose.db("pg_dump", "-U", "postgres", "-Fc", "ledger")
    backup_s = time.perf_counter() - started
    compose.db("psql", "-U", "postgres", "-c", "DROP DATABASE IF EXISTS ledger_restore")
    compose.db("psql", "-U", "postgres", "-c", "CREATE DATABASE ledger_restore")
    started = time.perf_counter()
    compose.db("pg_restore", "-U", "postgres", "-d", "ledger_restore", "--no-owner", "--no-privileges", stdin=dump)
    restore_s = time.perf_counter() - started
    restored = fingerprint("ledger_restore")
    compose.db("psql", "-U", "postgres", "-c", "DROP DATABASE ledger_restore")
    document = {
        "experiment": "restore",
        "rows": restored[0],
        "source_rows": source[0],
        "checksum": restored[1],
        "matched": restored == source,
        "dump_bytes": len(dump),
        "backup_seconds": round(backup_s, 2),
        "restore_seconds": round(restore_s, 2),
        "rto_target_seconds": rto_target_s,
    }
    evidence.save("restore", document)
    return document
```

The restore experiment: compute a fingerprint of the source (`count(*)` plus an `md5` of every entry in `id` order); `pg_dump -U postgres -Fc ledger` into memory; `DROP DATABASE IF EXISTS ledger_restore` and `CREATE DATABASE ledger_restore`; `pg_restore` the dump into it; compute the same fingerprint there and compare; time the backup and the restore; then drop the scratch database. The result (`rows`, `checksum`, `backup_seconds`, `restore_seconds`, `rto_target_seconds`, `matched`) is saved as `docs/evidence/restore.json`. **RTO** (recovery time objective) is how long you can afford to be down while restoring; the target here is 300 seconds.

### The connection limit and the pool

The server accepts up to 100 connections; the ledger's pool is 20 by default. A single ledger, with 20 connections held for 10 ms each, can complete at most about 20 / 0.010 = 2,000 requests a second: exactly the projected peak. That is why a pool sized "peak rate times hold time" is too small (no headroom for bursts) and the sweep finds a larger one that holds. No number of extra service instances changes this ceiling.

### Looking at connections from the server

```sql
-- During a pool incident: how many connections the ledger holds and what they are doing.
SELECT state, count(*) FROM pg_stat_activity WHERE datname = 'ledger' AND usename = 'ledger_app' GROUP BY state;
```

`SELECT state, count(*) FROM pg_stat_activity WHERE datname = 'ledger' AND usename = 'ledger_app' GROUP BY state;` counts the ledger's connections by state during a pool incident.

## Try it yourself

1. `make up`, then `docker compose exec db pg_isready -U postgres -d ledger` prints `/var/run/postgresql:5432 - accepting connections`.
2. `echo "SELECT version();" | make psql` prints a version starting with `PostgreSQL 18`.
3. `echo "SELECT count(*) FROM accounts;" | make psql` prints `100`.
4. `make restore-test` (about 5 seconds) prints the fingerprint match and the restore time.
5. `echo "SHOW max_connections;" | make psql` prints `100`.

## Mistakes people make

- A backup that was never restored. The restore test restores and compares.
- Sizing a pool without thinking of the server's connection limit across all instances.
- Using the administrator account in the application.
- Expecting `docker compose down` (without `-v`) to reset the data.

## Check yourself

1. What proves a restore worked?
   *Answer: the restored database has the same row count and the same checksum as the source.*
2. Why can adding instances not raise the ledger's capacity?
   *Answer: they share one pool of connections in front of one database.*
3. What does `pg_dump -Fc` produce?
   *Answer: a compressed custom-format backup file that `pg_restore` can read.*
4. What is an RTO?
   *Answer: the longest recovery (restore) time you can accept.*
5. When do the init files run?
   *Answer: only when the data directory is first created.*

## Further reading

- PostgreSQL 18 manual: <https://www.postgresql.org/docs/18/>
- `pg_dump`: <https://www.postgresql.org/docs/18/app-pgdump.html>
- `pg_restore`: <https://www.postgresql.org/docs/18/app-pgrestore.html>
- Docker image: <https://hub.docker.com/_/postgres>
