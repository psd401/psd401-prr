#!/bin/bash
# PostToolUse hook: after an agent edits a Terraform root, check formatting
# and validate it the way CI does (psd-dev-standards standards/08, layer 1
# "in the agent loop"). Exit 2 hands the failure back to the agent so it fixes
# it before moving on.
set -u

if ! command -v jq >/dev/null 2>&1; then
  echo "terraform-check hook skipped: jq is not installed, so the edited path can't be read." >&2
  exit 0
fi

file_path=$(jq -r '.tool_input.file_path // empty')
case "$file_path" in
  */infra/terraform/*.tf | */infra/terraform/*.tfvars) ;;
  *) exit 0 ;;
esac

if ! command -v terraform >/dev/null 2>&1; then
  echo "terraform-check hook skipped: terraform is not installed." >&2
  exit 0
fi

root=$(dirname "$file_path")
cd "$root" || exit 0

if ! out=$(terraform fmt -check -diff 2>&1); then
  printf 'terraform fmt -check found unformatted files in %s; run terraform fmt:\n%s\n' "$root" "$(printf '%s' "$out" | tail -40)" >&2
  exit 2
fi

# The same init CI runs: no backend, so the S3 state is never reached, and a
# read-only lockfile, so the committed provider hashes are never rewritten.
if [ ! -d .terraform ]; then
  if ! out=$(terraform init -backend=false -input=false -lockfile=readonly -no-color 2>&1); then
    printf 'terraform init failed in %s:\n%s\n' "$root" "$(printf '%s' "$out" | tail -40)" >&2
    exit 2
  fi
fi

if ! out=$(terraform validate -no-color 2>&1); then
  printf 'terraform validate failed after editing %s:\n%s\n' "$file_path" "$(printf '%s' "$out" | tail -40)" >&2
  exit 2
fi
exit 0
