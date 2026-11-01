#!/bin/bash
# ABOUTME: Creates a non-owner runtime role and durable default privileges.
# ABOUTME: Django migrations run separately as the legacy owner; apps do not.
set -euo pipefail

: "${POSTGRES_APP_USER:?POSTGRES_APP_USER is required}"
: "${POSTGRES_APP_PASSWORD:?POSTGRES_APP_PASSWORD is required}"

host_arguments=()
if [ -n "${POSTGRES_HOST:-}" ]; then
  host_arguments=(--host "$POSTGRES_HOST")
fi

psql \
  "${host_arguments[@]}" \
  --set=ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" <<'SQL'
\getenv database_name POSTGRES_DB
\getenv owner_user POSTGRES_USER
\getenv app_user POSTGRES_APP_USER
\getenv app_password POSTGRES_APP_PASSWORD

SELECT format(
  'CREATE ROLE %I LOGIN PASSWORD %L',
  :'app_user',
  :'app_password'
)
WHERE NOT EXISTS (
  SELECT 1 FROM pg_roles WHERE rolname = :'app_user'
)
\gexec

ALTER ROLE :"app_user" WITH LOGIN PASSWORD :'app_password';
GRANT CONNECT ON DATABASE :"database_name" TO :"app_user";
GRANT USAGE ON SCHEMA public TO :"app_user";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO :"app_user";
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO :"app_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner_user" IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner_user" IN SCHEMA public
  GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO :"app_user";
SQL
