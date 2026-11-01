#!/bin/bash
# ABOUTME: Rehearses the synthesized CDK deployment script against warm, cold
# ABOUTME: and partial production stacks. Requires a running Docker daemon.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cdk_dir="$(cd "$here/.." && pwd)"

docker info >/dev/null 2>&1 || {
  echo "rehearsal requires a running Docker daemon" >&2
  exit 69
}

work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT
cp -R "$here/bin" "$work/bin"
cp "$here/run.sh" "$work/run.sh"

# Rehearse exactly what CDK would send to Systems Manager, not a stale copy.
(cd "$cdk_dir" && bun run "$here/extract-deploy.ts") > "$work/deploy.sh"
test -s "$work/deploy.sh"

# The script targets Ubuntu: bash 5 array expansion, flock, /srv/prr, and an
# `ubuntu` account all have to be real for the rehearsal to mean anything.
docker run --rm -v "$work:/rehearsal" "${PRR_REHEARSAL_IMAGE:-ubuntu:24.04}" bash -c '
  apt-get update -qq >/dev/null 2>&1
  apt-get install -y -qq python3 >/dev/null 2>&1
  exec bash /rehearsal/run.sh'
