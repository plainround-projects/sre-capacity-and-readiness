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
