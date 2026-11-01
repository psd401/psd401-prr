#!/bin/bash
# ABOUTME: Produces verified PostgreSQL and audit backups and appends them to S3.
# ABOUTME: A manifest uploaded last is the commit marker for one complete backup.
set -Eeuo pipefail
umask 077

PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/aws-cli/current/bin:/snap/bin
export PATH

environment="${PRR_ENVIRONMENT:-prod}"
bucket="${PRR_BACKUP_BUCKET:-psd401-prr-backups-${environment}}"
state_dir="${PRR_BACKUP_STATE_DIR:-/srv/prr/backups}"
helper="${PRR_BACKUP_HELPER:-/usr/local/lib/prr-backup/backup_manifest.py}"
work_dir=""
backup_complete=false

case "$environment" in
  ""|*[!a-z0-9-]*|-*|*-) echo "invalid backup environment" >&2; exit 64 ;;
esac
expected_bucket="psd401-prr-backups-${environment}"
if [ "$bucket" != "$expected_bucket" ]; then
  echo "backup bucket must be ${expected_bucket}" >&2
  exit 64
fi
for routing_variable in COMPOSE_FILE DOCKER_HOST DOCKER_CONTEXT; do
  routing_value="${!routing_variable:-}"
  if [ -n "$routing_value" ]; then
    echo "${routing_variable} must be unset for app backups" >&2
    exit 64
  fi
done
unset COMPOSE_FILE DOCKER_HOST DOCKER_CONTEXT
for command in aws docker flock gzip python3; do
  command -v "$command" >/dev/null || { echo "missing required command: $command" >&2; exit 69; }
done
test -x "$helper" || { echo "backup manifest helper is not executable" >&2; exit 69; }
install -d -m 700 "$state_dir"
lock_fd="${PRR_BACKUP_LOCK_FD:-}"
if [ -n "$lock_fd" ]; then
  case "$lock_fd" in
    *[!0-9]*|"") echo "invalid inherited backup lock descriptor" >&2; exit 64 ;;
  esac
  inherited_lock=$(readlink -f "/proc/$$/fd/$lock_fd" 2>/dev/null || true)
  expected_lock=$(readlink -f "$state_dir/backup.lock" 2>/dev/null || true)
  if [ -z "$inherited_lock" ] || [ "$inherited_lock" != "$expected_lock" ]; then
    echo "inherited backup lock does not match the backup state directory" >&2
    exit 64
  fi
else
  exec 9>"$state_dir/backup.lock"
  lock_fd=9
fi
if ! flock -n "$lock_fd"; then
  echo "another app backup is already running" >&2
  exit 75
fi

publish_metric() {
  local value="$1"
  aws cloudwatch put-metric-data \
    --namespace PSD401/PRR \
    --metric-data "MetricName=AppBackupSuccess,Dimensions=[{Name=Environment,Value=${environment}}],Value=${value},Unit=Count" \
    --no-cli-pager >/dev/null
}

cleanup() {
  local status=$?
  trap - EXIT
  if [ "$backup_complete" != true ]; then
    publish_metric 0 || true
  fi
  if [ -n "$work_dir" ]; then
    rm -rf -- "$work_dir"
  fi
  exit "$status"
}
trap cleanup EXIT

work_dir=$(mktemp -d "$state_dir/.backup.XXXXXXXX")
install -d -m 700 "$work_dir/docker-config"
DOCKER_CONFIG="$work_dir/docker-config"
export DOCKER_CONFIG
audit_path="$work_dir/audit.jsonl.gz"
database_path="$work_dir/database.dump"
manifest_path="$work_dir/manifest.json"
backup_id=$(python3 -c 'import uuid; print(uuid.uuid4())')
created_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
container_for_service() {
  local service="$1"
  local containers=()
  mapfile -t containers < <(
    docker --host unix:///var/run/docker.sock ps -q \
      --filter label=com.docker.compose.project=app \
      --filter "label=com.docker.compose.service=${service}" \
      --filter status=running
  )
  if [ "${#containers[@]}" -ne 1 ]; then
    echo "${service} must have exactly one running container" >&2
    return 70
  fi
  printf '%s\n' "${containers[0]}"
}

web_container=$(container_for_service web)
db_container=$(container_for_service db)
docker --host unix:///var/run/docker.sock exec "$web_container" \
  python manage.py export_audit_log | gzip -n -9 >"$audit_path"
docker --host unix:///var/run/docker.sock exec "$db_container" pg_dump \
  --username prr \
  --dbname prr \
  --format=custom \
  --no-owner \
  --no-acl >"$database_path"
docker --host unix:///var/run/docker.sock exec -i "$db_container" \
  pg_restore --list <"$database_path" >/dev/null
image_id=""
for service in web worker beat; do
  service_container=$(container_for_service "$service")
  service_image_id=$(docker --host unix:///var/run/docker.sock inspect --format '{{.Image}}' "$service_container")
  if [[ ! "$service_image_id" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo "${service} image ID is unavailable" >&2
    exit 70
  fi
  if [ -z "$image_id" ]; then
    image_id="$service_image_id"
  elif [ "$service_image_id" != "$image_id" ]; then
    echo "application services do not share one running image" >&2
    exit 70
  fi
done

python3 "$helper" create \
  --database "$database_path" \
  --audit "$audit_path" \
  --output "$manifest_path" \
  --backup-id "$backup_id" \
  --created-at "$created_at" \
  --environment "$environment" \
  --image-id "$image_id" \
  --bucket "$bucket"
python3 "$helper" verify --manifest "$manifest_path" --directory "$work_dir"

s3_prefix="app-backups/${created_at:0:4}/${created_at:5:2}/${created_at:8:2}/${backup_id}"
upload() {
  local path="$1"
  local content_type="$2"
  local name checksum_hex checksum_base64
  name=$(basename "$path")
  checksum_hex=$(sha256sum "$path" | cut -d" " -f1)
  checksum_base64=$(python3 "$helper" checksum-base64 "$path")
  aws s3api put-object \
    --bucket "$bucket" \
    --key "$s3_prefix/$name" \
    --body "$path" \
    --if-none-match "*" \
    --checksum-algorithm SHA256 \
    --checksum-sha256 "$checksum_base64" \
    --server-side-encryption AES256 \
    --content-type "$content_type" \
    --metadata "sha256=${checksum_hex},backup-id=${backup_id}" \
    --no-cli-pager >/dev/null
}

upload "$audit_path" application/gzip
upload "$database_path" application/vnd.postgresql.custom
upload "$manifest_path" application/json
publish_metric 1
# The manifest is the durable commit marker; local status evidence is secondary.
backup_complete=true
install -m 600 "$manifest_path" "$state_dir/last-success.json"
echo "app backup complete: s3://${bucket}/${s3_prefix}/manifest.json"
