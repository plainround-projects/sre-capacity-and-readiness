#!/bin/sh
# Create the ledger's database role with the least privilege it needs. The password comes from the environment.
set -eu
psql -v ON_ERROR_STOP=1 -v app_password="$LEDGER_DB_PASSWORD" --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
CREATE ROLE ledger_app LOGIN PASSWORD :'app_password' NOSUPERUSER NOCREATEDB NOCREATEROLE;
SQL
