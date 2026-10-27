# SQL (PostgreSQL dialect)

## What it is

SQL (Structured Query Language) is the language for asking a relational database to store and fetch data. Data lives in **tables** of rows and columns. A statement is one instruction ending with `;`:

- **DDL** (data definition) creates structure: `CREATE TABLE`, `CREATE INDEX`, `GRANT`, `COMMENT`.
- **DML** (data manipulation) changes rows: `INSERT`.
- **Queries** read rows: `SELECT`.

A **primary key** is a column whose values are unique and used for lookups. A **foreign key** (`REFERENCES`) says a column must hold a value that exists in another table. An **index** is a sorted copy of some columns that lets the database find rows without reading the whole table. A **role** is a database user; a **privilege** is a permission given with `GRANT`.

## Why this repository uses it

The ledger service appends entries to a database, and the capacity exercise needs a real shared dependency with a real, limited number of connections. The tables are small but real, with 5,000 historical entries so that a backup and a restore have something to prove. The SQL also encodes least privilege: the ledger's role can read accounts and append entries, and do nothing else.

## Where it appears

| File | Role |
| --- | --- |
| `sql/001_schema.sql` | DDL: `accounts`, `entries`, an index, and the grants for the `ledger_app` role. |
| `sql/002_seed.sql` | DML: 100 accounts and 5,000 deterministic historical entries. |
| `sql/003_queries.sql` | The ledger's one write, a balance query, the restore test's checksum, and a connection-count query. |
| `sql/000_roles.sh` | Creates the role (see the Bash note). |
| `interfaces/ledger_app.py` | Runs the insert from Python with named parameters. |
| `interfaces/experiments_cli.py` | The restore experiment runs the checksum query in the database container, on the source and on the restored copy. |

## How it works underneath

PostgreSQL runs the three files in `/docker-entrypoint-initdb.d/` once, as the administrator, in file-name order: the role, then the schema and grants, then the seed. After that the ledger connects as `ledger_app`. Parameters (`%(hold)s`, `%(account)s`) are sent separately from the SQL text by the driver, so data is never mixed into SQL (no **SQL injection**).

## Walkthrough of this repository's use

### The schema

```sql
-- DDL for the ledger database (PostgreSQL 18).

CREATE TABLE accounts (
    id   integer PRIMARY KEY CHECK (id > 0),
    name text NOT NULL
);

CREATE TABLE entries (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id   integer NOT NULL REFERENCES accounts (id),
    amount_cents integer NOT NULL CHECK (amount_cents <> 0),
    created_at   timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE entries IS 'Append-only: the ledger service inserts, nothing updates or deletes.';
CREATE INDEX idx_entries_account_created ON entries (account_id, created_at DESC);

-- The ledger service connects as ledger_app: it may read accounts and append entries, nothing else (least privilege).
GRANT CONNECT ON DATABASE ledger TO ledger_app;
GRANT USAGE ON SCHEMA public TO ledger_app;
GRANT SELECT ON accounts TO ledger_app;
GRANT SELECT, INSERT ON entries TO ledger_app;
```

- `CREATE TABLE accounts (id integer PRIMARY KEY CHECK (id > 0), name text NOT NULL)`: ids are positive whole numbers and unique; the name is required.
- `CREATE TABLE entries (...)`:
  - `id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY`: a big whole number that the database numbers automatically (1, 2, 3, ...). `ALWAYS` means inserts may not supply it.
  - `account_id integer NOT NULL REFERENCES accounts (id)`: every entry belongs to an existing account (a foreign key).
  - `amount_cents integer NOT NULL CHECK (amount_cents <> 0)`: money in whole cents (never floating point), never zero. `<>` means "not equal".
  - `created_at timestamptz NOT NULL DEFAULT now()`: a time stamp with time zone, filled with the current time when the insert does not give one.
- `COMMENT ON TABLE entries IS '...'` records, inside the database, that the table is append-only.
- `CREATE INDEX idx_entries_account_created ON entries (account_id, created_at DESC)`: serves "an account's newest entries" without sorting.
- The grants: `GRANT CONNECT ON DATABASE ledger TO ledger_app`, `GRANT USAGE ON SCHEMA public TO ledger_app` (a schema is a namespace inside a database), `GRANT SELECT ON accounts` and `GRANT SELECT, INSERT ON entries`. No `UPDATE`, no `DELETE`: the ledger cannot rewrite history.

### The seed

```sql
-- DML: 100 accounts and 5,000 historical entries, enough that a backup and a restore have something to prove.
-- Entry amounts are deterministic, so the restore test can compare a checksum of the source and the restored copy.

INSERT INTO accounts (id, name)
SELECT n, 'Account ' || n FROM generate_series(1, 100) AS n;

INSERT INTO entries (account_id, amount_cents, created_at)
SELECT
    1 + (n * 7) % 100,
    1 + (n * 131) % 9999,
    timestamptz '2026-09-01 00:00:00+00' + (n * interval '7 minutes')
FROM generate_series(1, 5000) AS n;
```

- `INSERT INTO accounts (id, name) SELECT n, 'Account ' || n FROM generate_series(1, 100) AS n;` inserts 100 rows. `generate_series(1, 100)` is a PostgreSQL function that produces the numbers 1 to 100, one row each, named `n`. `||` joins text.
- The second insert makes 5,000 entries. `1 + (n * 7) % 100` spreads them over accounts 1 to 100 (`%` is the remainder). `1 + (n * 131) % 9999` makes deterministic amounts. `timestamptz '2026-09-01 00:00:00+00' + (n * interval '7 minutes')` spaces the times seven minutes apart. The comment says why it is deterministic: the restore test compares a checksum of the source and the restored copy, so the data must be exactly reproducible.

### The queries

```sql
-- The queries the ledger and the operators run. Run with: make psql < sql/003_queries.sql

-- The ledger's one write: append an entry (pg_sleep stands in for a heavier query, the time a connection is held).
INSERT INTO entries (account_id, amount_cents) VALUES (1, 1500) RETURNING id;

-- Balance of one account, served by idx_entries_account_created.
SELECT account_id, sum(amount_cents) AS balance_cents, count(*) AS entries FROM entries WHERE account_id = 1 GROUP BY account_id;

-- The restore test's checksum: row count and an order-independent digest of every entry.
SELECT count(*) AS rows, md5(string_agg(id || ':' || account_id || ':' || amount_cents, ',' ORDER BY id)) AS checksum FROM entries;

-- During a pool incident: how many connections the ledger holds and what they are doing.
SELECT state, count(*) FROM pg_stat_activity WHERE datname = 'ledger' AND usename = 'ledger_app' GROUP BY state;
```

- The insert returns the new id (`RETURNING id`). In the ledger the same insert is combined with `pg_sleep` (see below).
- The balance query uses `sum(amount_cents)` and `count(*)` with `GROUP BY account_id`; the index serves it.
- The restore checksum: `count(*)` and `md5(string_agg(id || ':' || account_id || ':' || amount_cents, ',' ORDER BY id))`. `string_agg` joins all rows into one text in `id` order; `md5` turns it into a 32-character fingerprint. Equal fingerprints mean equal data.
- `pg_stat_activity` lists open connections; grouped by `state` it shows how many are `active` or `idle`.

### The same insert from Python

```python
# One statement: hold the connection for the configured time (a stand-in for a heavier query), then append the entry.
_INSERT = (
    "WITH slept AS (SELECT pg_sleep(%(hold)s)) "
    "INSERT INTO entries (account_id, amount_cents) SELECT %(account)s, %(amount)s FROM slept RETURNING id"
)
```

`WITH slept AS (SELECT pg_sleep(%(hold)s)) INSERT ... SELECT %(account)s, %(amount)s FROM slept RETURNING id`. A **CTE** (`WITH name AS (...)`) is a named sub-query. `pg_sleep(0.01)` makes the statement wait 10 ms before it inserts: the connection is held for that time, a stand-in for a heavier query. `%(name)s` marks a **named parameter**, filled from a dictionary.

## Try it yourself

1. `make up`, then `echo "SELECT count(*) FROM entries;" | make psql` prints `5000`.
2. `make psql < sql/003_queries.sql` runs the four queries.
3. `echo "SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = 'ledger_app';" | make psql` prints `f | f | f`.
4. `docker compose exec -T -e PGPASSWORD=local-dev-only-app db psql -h 127.0.0.1 -U ledger_app -d ledger -c "DELETE FROM entries;"` fails with `ERROR:  permission denied for table entries`.
5. `echo "SELECT md5(string_agg(id || ':' || account_id || ':' || amount_cents, ',' ORDER BY id)) FROM entries;" | make psql` prints a 32-character fingerprint; run it twice and it is the same.

## Mistakes people make

- Building SQL by gluing strings with user input.
- Storing money as floating point.
- Giving the application the administrator account.
- Comparing two tables "by eye" instead of with a checksum.

## Check yourself

1. What can `ledger_app` do?
   *Answer: connect, read `accounts` and read and insert `entries`; nothing else.*
2. Why is the seed deterministic?
   *Answer: so the restore test can compare a checksum of the source and the restored copy.*
3. What does `GENERATED ALWAYS AS IDENTITY` do?
   *Answer: the database numbers the rows itself and refuses a supplied id.*
4. What does `pg_sleep` do in the insert?
   *Answer: holds the pooled connection for a known time, so the pool becomes the next bottleneck.*
5. What does `md5(string_agg(... ORDER BY id))` prove?
   *Answer: equal fingerprints mean the two tables hold the same rows in the same order.*

## Further reading

- PostgreSQL 18 SQL commands: <https://www.postgresql.org/docs/18/sql-commands.html>
- `GRANT`: <https://www.postgresql.org/docs/18/sql-grant.html>
- `generate_series`: <https://www.postgresql.org/docs/18/functions-srf.html>
- `string_agg` and `md5`: <https://www.postgresql.org/docs/18/functions-aggregate.html>
