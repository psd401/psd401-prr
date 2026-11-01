#!/bin/bash
# ABOUTME: Publishes root-volume usage to CloudWatch so a filling disk is seen.
# ABOUTME: Read-only: it measures and reports, and never deletes anything.
#
# Why this exists. Nothing on this host reported disk usage, and nothing ever
# reclaimed the images each deployment builds. Thirty-four accumulated, the
# 48G root volume reached 97%, and the first symptom was a CloudFormation
# failure: at that point the SSM document worker could no longer write its own
# stdout, so every deployment failed to stabilize with an error that named
# neither the disk nor the cause. A full disk on this host also risks
# PostgreSQL writes and the append-only audit trail, which is the part that
# must never fail quietly.
#
# Deployments now prune superseded images, which removes that specific cause.
# This closes the general one: usage is measured every few minutes and alarmed
# on well before anything breaks, whatever the growth turns out to be --
# database growth with caseload, log accumulation, or a transient spike while
# converting a large record.
set -euo pipefail

environment="${PRR_ENVIRONMENT:-prod}"
case "$environment" in
  ""|*[!a-z0-9-]*|-*|*-) echo "invalid environment" >&2; exit 64 ;;
esac

mount_point="${PRR_DISK_MOUNT:-/}"

# `df -P` is the POSIX output format: one line per filesystem, fixed columns,
# no wrapping on long device names. Capacity is reported as an integer
# percentage with a trailing "%", which is stripped here.
used_percent="$(df -P "$mount_point" | awk 'NR==2 {gsub(/%/, "", $5); print $5}')"

if ! printf '%s\n' "$used_percent" | grep -Eq '^[0-9]{1,3}$'; then
  echo "could not read disk usage for $mount_point" >&2
  exit 65
fi
if [ "$used_percent" -gt 100 ]; then
  echo "implausible disk usage: ${used_percent}%" >&2
  exit 65
fi

aws cloudwatch put-metric-data \
  --namespace PSD401/PRR \
  --metric-data "MetricName=RootDiskUsedPercent,Dimensions=[{Name=Environment,Value=${environment}}],Value=${used_percent},Unit=Percent" \
  --no-cli-pager >/dev/null

echo "root volume ${used_percent}% used"
