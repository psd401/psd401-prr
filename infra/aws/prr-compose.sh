#!/bin/bash
# ABOUTME: The only safe way to run docker compose against the live release.
# ABOUTME: Resolves the release, the image, and the Gmail activation overlay.
#
# Why this exists. Gmail inbound polling is enabled by the presence of
# /srv/prr/.gmail-inbound-activation.compose.yml, which docker compose applies
# only when a caller passes it with -f. A caller who forgets is not warned:
# "overlay not passed" and "feature deliberately off" produce byte-identical
# commands, so an ordinary `docker compose up -d web worker beat` silently
# turns off inbound requester mail. That happened four times in one night, and
# each time the only symptom was a CloudWatch alarm nobody was watching.
#
# The deployment script already gets this right -- it detects the mode and
# routes every invocation through compose_mode. This gives operators and
# out-of-band automation the same guarantee, so the safe path is also the
# convenient one.
#
# Usage: prr-compose up -d --no-deps --force-recreate web worker beat
#        prr-compose ps
#        prr-compose logs --tail 50 beat
set -euo pipefail

release="$(readlink -f /srv/prr/cdk-current 2>/dev/null || true)"
if [ -z "$release" ] || [ ! -d "$release" ]; then
  echo "no current release at /srv/prr/cdk-current" >&2
  exit 69
fi
for required in docker-compose.deploy.yml docker-compose.aws.yml; do
  if [ ! -f "$release/$required" ]; then
    echo "release is missing $required" >&2
    exit 69
  fi
done

compose_files=(-f "$release/docker-compose.deploy.yml" -f "$release/docker-compose.aws.yml")

# The overlay's existence IS the enabled state. Including it whenever it is
# present is what makes this wrapper safe: an operator cannot disable inbound
# mail by forgetting a flag, and cannot enable it by adding one either.
activation_overlay=/srv/prr/.gmail-inbound-activation.compose.yml
gmail_mode=disabled
if [ -e "$activation_overlay" ]; then
  compose_files+=(-f "$activation_overlay")
  gmail_mode=enabled
fi

# Compose needs the image reference the live services already run. Taking it
# from the running container rather than rebuilding a tag means this wrapper
# never silently moves a service onto a different build.
if [ -z "${PRR_RELEASE_IMAGE:-}" ]; then
  running_web="$(docker ps -q \
    --filter label=com.docker.compose.project=app \
    --filter label=com.docker.compose.service=web | head -1)"
  if [ -n "$running_web" ]; then
    PRR_RELEASE_IMAGE="$(docker inspect --format '{{.Config.Image}}' "$running_web")"
  elif [ -r /srv/prr/cdk-current-version ]; then
    PRR_RELEASE_IMAGE="psd401-prr:cdk-$(cat /srv/prr/cdk-current-version)"
  else
    echo "cannot resolve the release image; set PRR_RELEASE_IMAGE" >&2
    exit 69
  fi
  export PRR_RELEASE_IMAGE
fi

if [ "${PRR_COMPOSE_QUIET:-0}" != "1" ]; then
  echo "prr-compose: release=$release gmail=$gmail_mode image=$PRR_RELEASE_IMAGE" >&2
fi

exec docker compose \
  --project-directory "$release" \
  --project-name app \
  "${compose_files[@]}" \
  "$@"
