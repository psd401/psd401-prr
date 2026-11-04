#!/usr/bin/env bash
# ABOUTME: Runs the Playwright acceptance flows. --smoke runs a short subset.
# ABOUTME: Playwright's own exit code is the gate; there is no evidence validator.
set -euo pipefail

# Smoke = the flows that gate a merge, while nightly.yml runs all of them.
# Login, all six role shells, the case tabs, and message disposition, plus two
# the thinner gate should not be without: matter kinds is the rule 13 boundary
# that decides whether a case answers to RCW 42.56 or FERPA, and the reviewer
# flow asserts no requester PII reaches a record reviewer. Edit this list here;
# it is the only copy.
SMOKE_SPECS=(
  e2e/staff-login-open-case.spec.ts
  e2e/role-aware-work-inbox.spec.ts
  e2e/officer-new-case.spec.ts
  e2e/officer-message-disposition.spec.ts
  e2e/officer-matter-kinds.spec.ts
  e2e/reviewer-record-work-no-pii.spec.ts
)

# A quarantined flow still runs -- in its own non-gating lane (FLAKY.md).
# The gating lanes below exclude it by title tag so one unreliable flow cannot
# block a merge, and --quarantine runs exactly those flows.
QUARANTINE_TAG='@quarantine'

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
evidence="$root/.verification"

mode="${1:-}"
case "$mode" in
  --smoke|--quarantine|"") ;;
  *) echo "usage: $0 [--smoke|--quarantine]" >&2; exit 2 ;;
esac

# The quarantine lane runs after a gating lane in the same job, so it must not
# delete the evidence that lane just produced. It proves the gating lane really
# ran by looking for the marker that lane writes: without that check, a
# standalone --quarantine run would leave whatever stale screenshots happened
# to be sitting in .verification/ and present them as this run's evidence.
marker="$evidence/.gating-lane-ran"
if [ "$mode" != "--quarantine" ] || [ ! -f "$marker" ]; then
  rm -f "$evidence"/*.png "$evidence"/*.json
  rm -rf "$evidence/playwright-results" "$evidence/playwright-report"
  # The e2e filesystem object store (config.e2e_settings) is reseeded each run.
  rm -rf "$evidence/e2e-objects"
  rm -f "$marker"
fi
if [ "$mode" != "--quarantine" ]; then
  mkdir -p "$evidence"
  : > "$marker"
fi

cd "$root/frontend"
case "$mode" in
  --smoke)
    bun run test:e2e -- --grep-invert "$QUARANTINE_TAG" "${SMOKE_SPECS[@]}"
    ;;
  --quarantine)
    # Nothing quarantined is the healthy state, and it is not a failure.
    bun run test:e2e -- --grep "$QUARANTINE_TAG" --pass-with-no-tests
    ;;
  "")
    bun run test:e2e -- --grep-invert "$QUARANTINE_TAG"
    ;;
esac
