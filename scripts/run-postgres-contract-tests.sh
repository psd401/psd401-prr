#!/usr/bin/env bash
set -euo pipefail

PRR_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRR_REPOSITORY_ROOT="$(cd "$PRR_SCRIPT_DIR/.." && pwd)"
cd "$PRR_REPOSITORY_ROOT"

PRR_CONTRACT_CONTAINER=""

cleanup_contract_database() {
  PRR_EXIT_STATUS="$?"
  trap - EXIT
  if [[ -n "$PRR_CONTRACT_CONTAINER" ]]; then
    docker rm --force "$PRR_CONTRACT_CONTAINER" >/dev/null 2>&1 || :
  fi
  exit "$PRR_EXIT_STATUS"
}

trap cleanup_contract_database EXIT

if [[ -z "${PRR_POSTGRES_TEST_DATABASE_URL:-}" ]]; then
  if ! command -v docker >/dev/null 2>&1; then
    echo "PostgreSQL contract tests require Docker or PRR_POSTGRES_TEST_DATABASE_URL." >&2
    exit 1
  fi
  PRR_CONTRACT_CONTAINER="psd401-prr-contract-${PPID}-$$"
  docker run --detach --rm \
    --name "$PRR_CONTRACT_CONTAINER" \
    --publish 127.0.0.1::5432 \
    --env POSTGRES_DB=prr_contract \
    --env POSTGRES_USER=prr_contract \
    --env POSTGRES_PASSWORD=prr-contract-only \
    postgres:16 >/dev/null

  PRR_DATABASE_READY=false
  for _attempt in {1..30}; do
    if docker exec "$PRR_CONTRACT_CONTAINER" \
      pg_isready --username prr_contract --dbname prr_contract >/dev/null 2>&1; then
      PRR_DATABASE_READY=true
      break
    fi
    sleep 1
  done
  if [[ "$PRR_DATABASE_READY" != "true" ]]; then
    echo "The disposable PostgreSQL contract database did not become ready." >&2
    exit 1
  fi
  PRR_CONTRACT_PORT="$(
    docker port "$PRR_CONTRACT_CONTAINER" 5432/tcp | sed 's/.*://'
  )"
  PRR_POSTGRES_TEST_DATABASE_URL="postgresql://prr_contract:prr-contract-only@127.0.0.1:${PRR_CONTRACT_PORT}/prr_contract"
  export PRR_POSTGRES_TEST_DATABASE_URL
  export PRR_CASEWORK_PERFORMANCE_DISPOSABLE="casework-performance-disposable"
elif [[ "${PRR_CASEWORK_PERFORMANCE_DISPOSABLE:-}" != "casework-performance-disposable" ]]; then
  echo "A caller-supplied PostgreSQL contract database requires explicit disposable acknowledgment." >&2
  exit 1
fi

PRR_CONTRACT_PYTHON="$(bash "$PRR_REPOSITORY_ROOT/scripts/prr-python.sh")"

PYTHON_DOTENV_DISABLED=1 "$PRR_CONTRACT_PYTHON" manage.py test \
  apps.api.tests.test_identity_config_concurrency \
  apps.api.tests.test_personal_api_tokens_concurrency \
  apps.api.tests.test_case_workspace_postgres \
  apps.api.tests.test_requests.RequestsOrderingTests \
  apps.api.tests.test_requests.RequestsSubjectTests \
  apps.records.tests.test_next_response_due \
  apps.api.tests.test_case_notes_api \
  apps.records.tests.test_lifecycle_authorization_concurrency \
  apps.records.tests.test_matter_kinds \
  apps.records.tests.test_automatic_record_advisory.AutomaticRecordAdvisoryTests.test_periodic_supervisor_backfills_precomputed_responsive_signal \
  apps.api.tests.test_correspondence_search_postgres \
  apps.configuration.tests.test_settings_concurrency \
  apps.correspondence.tests.test_casework_fixture_command \
  apps.correspondence.tests.test_inbound_concurrency \
  apps.correspondence.tests.test_outbound_concurrency \
  apps.correspondence.tests.test_search_projection_migration \
  apps.work_queue.tests.test_concurrency \
  --settings=config.postgres_test_settings

PYTHON_DOTENV_DISABLED=1 "$PRR_CONTRACT_PYTHON" manage.py migrate \
  --noinput --settings=config.casework_performance_settings
PYTHON_DOTENV_DISABLED=1 "$PRR_CONTRACT_PYTHON" manage.py seed_casework_test_fixture \
  --settings=config.casework_performance_settings

# Performance evidence records which commit it came from, for traceability.
# It is NOT gated on a clean worktree or on matching an externally supplied
# SHA: that turned every local run into "commit first, then measure".
PRR_PERFORMANCE_SOURCE_SHA="$(git rev-parse HEAD)"

if [[ -n "${PRR_PERFORMANCE_EVIDENCE_PATH:-}" ]]; then
  PYTHON_DOTENV_DISABLED=1 "$PRR_CONTRACT_PYTHON" manage.py measure_casework_performance \
    --runs 30 --output "$PRR_PERFORMANCE_EVIDENCE_PATH" \
    --source-sha "$PRR_PERFORMANCE_SOURCE_SHA" \
    --settings=config.casework_performance_settings
else
  PYTHON_DOTENV_DISABLED=1 "$PRR_CONTRACT_PYTHON" manage.py measure_casework_performance \
    --runs 30 --source-sha "$PRR_PERFORMANCE_SOURCE_SHA" \
    --settings=config.casework_performance_settings
fi
