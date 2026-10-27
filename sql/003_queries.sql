-- The queries the ledger and the operators run. Run with: make psql < sql/003_queries.sql

-- The ledger's one write: append an entry (pg_sleep stands in for a heavier query, the time a connection is held).
INSERT INTO entries (account_id, amount_cents) VALUES (1, 1500) RETURNING id;

-- Balance of one account, served by idx_entries_account_created.
SELECT account_id, sum(amount_cents) AS balance_cents, count(*) AS entries FROM entries WHERE account_id = 1 GROUP BY account_id;

-- The restore test's checksum: row count and an order-independent digest of every entry.
SELECT count(*) AS rows, md5(string_agg(id || ':' || account_id || ':' || amount_cents, ',' ORDER BY id)) AS checksum FROM entries;

-- During a pool incident: how many connections the ledger holds and what they are doing.
SELECT state, count(*) FROM pg_stat_activity WHERE datname = 'ledger' AND usename = 'ledger_app' GROUP BY state;
