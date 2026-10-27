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
