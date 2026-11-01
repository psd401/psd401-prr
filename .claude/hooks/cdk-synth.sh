#!/bin/bash
# PostToolUse hook: after an agent edits the CDK app, typecheck and synth it
# the way CI does (psd-dev-standards standards/08, layer 1 "in the agent loop").
# Exit 2 hands the failure back to the agent so it fixes it before moving on.
set -u

if ! command -v jq >/dev/null 2>&1; then
  echo "cdk-synth hook skipped: jq is not installed, so the edited path can't be read." >&2
  exit 0
fi

file_path=$(jq -r '.tool_input.file_path // empty')
case "$file_path" in
  */infra/cdk/bin/*.ts | */infra/cdk/lib/*.ts | */infra/cdk/cdk.json | */infra/cdk/cdk.context.json) ;;
  *) exit 0 ;;
esac

cdk_dir="${CLAUDE_PROJECT_DIR:-.}/infra/cdk"
if [ ! -d "$cdk_dir/node_modules" ]; then
  echo "cdk-synth hook skipped: run 'bun install' in infra/cdk to enable it." >&2
  exit 0
fi

cd "$cdk_dir" || exit 0

# CI synthesizes with no AWS credentials (lookups come from the committed
# cdk.context.json), so drop the profile here too: a synth that only works
# with credentials is a regression, not a pass.
if ! out=$(bun run typecheck 2>&1); then
  printf 'CDK typecheck failed after editing %s:\n%s\n' "$file_path" "$(printf '%s' "$out" | tail -40)" >&2
  exit 2
fi
if ! out=$(env -u AWS_PROFILE bunx cdk synth --quiet 2>&1); then
  printf 'cdk synth failed after editing %s:\n%s\n' "$file_path" "$(printf '%s' "$out" | tail -40)" >&2
  exit 2
fi
exit 0
