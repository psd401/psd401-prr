#!/bin/bash
# ABOUTME: Container entrypoint: waits for the database, runs migrations and
# ABOUTME: seed data when opted in, provisions a superuser, then execs the CMD.
set -euo pipefail

wait_for_db() {
  python - <<'PY'
import os
import sys
import time

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.db import connections
from django.db.utils import OperationalError

conn = connections["default"]
max_attempts = int(os.environ.get("DB_WAIT_ATTEMPTS", "30"))
delay = float(os.environ.get("DB_WAIT_DELAY", "2"))

for attempt in range(1, max_attempts + 1):
    try:
        conn.ensure_connection()
        break
    except OperationalError as exc:
        if attempt == max_attempts:
            print(f"Database not reachable after {max_attempts} attempts: {exc}", file=sys.stderr)
            sys.exit(1)
        time.sleep(delay)
PY
}

wait_for_db

# Fail startup on unsafe non-debug security settings. Django's built-in deploy
# checks add warnings; project-specific checks make missing HTTPS/secure-cookie
# posture and placeholder secrets hard errors.
python manage.py check --deploy --fail-level ERROR

if [ "${RUN_MIGRATIONS:-false}" = "true" ]; then
  python manage.py migrate --noinput
  # Write the currently deployed value of each application-managed switch into
  # the settings store, once. Idempotent: a key that already has a stored value
  # is left exactly as the district set it, so this never reverts an
  # administrator's decision on a later deploy. It runs here, immediately after
  # migrate, for local and Compose flows. Production never reaches this line:
  # the CDK deployment replaces the entrypoint and runs the import itself, in
  # the worker service, so it sees the Gmail activation overlay (see
  # infra/cdk/lib/prr-hosting-stack.ts).
  python manage.py import_environment_settings
fi

if [ "${AUTO_SEED:-false}" = "true" ]; then
  python manage.py seed_exemptions
  python manage.py seed_systems
fi

# Idempotent superuser provisioning: Django's createsuperuser --noinput reads
# DJANGO_SUPERUSER_USERNAME/EMAIL/PASSWORD from the environment on its own.
# Tolerate "already exists" so re-runs (restarts, redeploys) don't fail;
# any other CommandError still aborts the entrypoint.
if [ -n "${DJANGO_SUPERUSER_USERNAME:-}" ] && [ -n "${DJANGO_SUPERUSER_EMAIL:-}" ] && [ -n "${DJANGO_SUPERUSER_PASSWORD:-}" ]; then
  if ! output=$(python manage.py createsuperuser --noinput 2>&1); then
    if echo "$output" | grep -qi "already taken"; then
      echo "Superuser ${DJANGO_SUPERUSER_USERNAME} already exists, skipping."
    else
      echo "$output" >&2
      exit 1
    fi
  else
    echo "$output"
  fi
fi

exec "$@"
