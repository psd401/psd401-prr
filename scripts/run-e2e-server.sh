#!/usr/bin/env bash
set -euo pipefail

PRR_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRR_REPOSITORY_ROOT="$(cd "$PRR_SCRIPT_DIR/.." && pwd)"
PRR_E2E_PYTHON="$(bash "$PRR_REPOSITORY_ROOT/scripts/prr-python.sh")"

mkdir -p "$PRR_REPOSITORY_ROOT/.verification"
cd "$PRR_REPOSITORY_ROOT"

export DJANGO_SETTINGS_MODULE=config.e2e_settings
export PRR_AUTH_MODE=local
export PYTHON_DOTENV_DISABLED=1

rm -f "$PRR_REPOSITORY_ROOT/.verification/e2e.sqlite3"
"$PRR_E2E_PYTHON" manage.py migrate --noinput
"$PRR_E2E_PYTHON" manage.py seed_casework_test_fixture
exec "$PRR_E2E_PYTHON" manage.py runserver 127.0.0.1:8000 --noreload
