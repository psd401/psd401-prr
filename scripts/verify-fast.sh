#!/usr/bin/env bash
# ABOUTME: Runs the test suites concurrently where that is safe, serially where it is not.
# ABOUTME: --backend-only while iterating on Python; --full adds postgres and the browser suite.
#
# The one thing worth knowing: the Django suite is sharded by app label and the
# shards run together, because config/test_settings.py uses SQLite :memory: so
# each process gets a private database. That is ~4m instead of ~10m serial.
#
# vitest runs alone afterwards. Running it alongside the Django shards was
# measured at 4h09m with 17 phantom failures from worker starvation -- vitest
# spawns its own pool and does not share the machine well.
#
# `manage.py test --parallel` is not used: it dies with "cannot pickle
# 'traceback' object" and names no test. Sharding needs none of that machinery.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

PY="${PY:-$(bash scripts/prr-python.sh)}"
export PYTHON_DOTENV_DISABLED=1

MODE="${1:-default}"
case "$MODE" in
  default|--backend-only|--full|--patch-coverage) ;;
  *) echo "usage: $0 [--backend-only|--full|--patch-coverage]" >&2; exit 2 ;;
esac

# --patch-coverage is the local copy of the CI gate in
# .github/workflows/psd-ci-jobs.yml: run both suites under coverage, then
# require 80% of the lines this branch adds to be covered
# (psd-dev-standards standards/05 rule 4). It is slower than the default lane
# because instrumentation is not free, which is why it is opt-in.
COVERAGE_BASE="${PRR_PATCH_COVERAGE_BASE:-origin/main}"
if [ "$MODE" = "--patch-coverage" ]; then
  # sysmon is CPython 3.12's own coverage core: measured at 94s on the
  # correspondence shard against 164s for coverage.py's default trace core
  # (and 101s with no coverage at all). Line coverage only, which is all this
  # gate measures.
  export COVERAGE_CORE=sysmon
  TEST_CMD="$PY -m coverage run --rcfile=pyproject.toml manage.py test"
  rm -f .coverage .coverage.*
else
  TEST_CMD="$PY manage.py test"
fi

# A quarantined test still runs -- in the non-gating lane, not here
# (FLAKY.md).
GATING="--settings=config.test_settings --exclude-tag=quarantine"

# A deleted app leaves __pycache__ behind, and Path.exists() is true for a
# directory holding only that -- enough to fail "the retired app is gone" tests
# locally while CI, checking out clean, stays green.
find apps config integrations -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || :

LOGS="$(mktemp -d)"
FAILED=()

run() { # run <name> <command>
  bash -c "$2" >"$LOGS/$1.log" 2>&1 &
  eval "pid_$1=$!"
}

reap() {
  for name in "$@"; do
    eval "wait \$pid_$name" && echo "  PASS  $name" || { echo "  FAIL  $name"; FAILED+=("$name"); }
  done
}

# Lint and typecheck first, in the same lane shape CI uses (`backend-static`
# in .github/workflows/psd-ci-jobs.yml). They are seconds against the suite's
# minutes, and a ruff finding is faster to read here than in a CI log.
echo "backend static (lint, typecheck ratchet)"
run ruff    "$PY -m ruff check ."
run pyright "$PY scripts/pyright_baseline.py"
reap ruff pyright

echo "backend (sharded, concurrent)"
run correspondence "$TEST_CMD apps.correspondence $GATING"
run api            "$TEST_CMD apps.api $GATING"
run records        "$TEST_CMD apps.records $GATING"
run rest           "$TEST_CMD apps.audit apps.configuration apps.integrations apps.work_queue $GATING"
reap correspondence api records rest

if [ "$MODE" != "--backend-only" ]; then
  echo "frontend (alone)"
  if [ "$MODE" = "--patch-coverage" ]; then
    run frontend 'cd frontend && bun run test:coverage && bun run lint && bun run typecheck && bun run build'
  else
    run frontend 'cd frontend && bun run test && bun run lint && bun run typecheck && bun run build'
  fi
  reap frontend
fi

if [ "$MODE" = "--patch-coverage" ] && [ "${#FAILED[@]}" -eq 0 ]; then
  echo "patch coverage (vs $COVERAGE_BASE)"
  "$PY" -m coverage combine
  "$PY" -m coverage json -o .verification/backend-coverage.json
  if ! "$PY" scripts/patch_coverage.py \
    --base "$COVERAGE_BASE" \
    --coverage .verification/backend-coverage.json \
    --coverage .verification/frontend-coverage/coverage-final.json; then
    FAILED+=("patch-coverage")
  fi
fi

if [ "$MODE" = "--full" ]; then
  echo "postgres + browser"
  run postgres "bash scripts/run-postgres-contract-tests.sh"
  reap postgres
  run e2e 'bash scripts/run-casework-acceptance.sh'
  reap e2e
fi

if [ "${#FAILED[@]}" -eq 0 ]; then
  echo "GREEN — logs: $LOGS"
  exit 0
fi

echo "RED — failing: ${FAILED[*]}"
for name in "${FAILED[@]}"; do
  echo; echo "===== $name (last 40 lines) ====="; tail -40 "$LOGS/$name.log"
done
exit 1
