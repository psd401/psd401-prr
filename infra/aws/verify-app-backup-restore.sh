#!/bin/bash
# ABOUTME: Restores one downloaded app backup into an isolated PostgreSQL container.
# ABOUTME: Verifies schema and audit evidence without touching an application database.
set -Eeuo pipefail
umask 077

PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH

if [ "$#" -ne 1 ]; then
  echo "usage: $0 BACKUP_DIRECTORY" >&2
  exit 64
fi
backup_dir=$(cd "$1" && pwd -P)
helper="${PRR_BACKUP_HELPER:-/usr/local/lib/prr-backup/backup_manifest.py}"
test -x "$helper" || { echo "backup manifest helper is not executable" >&2; exit 69; }
python3 "$helper" verify --manifest "$backup_dir/manifest.json" --directory "$backup_dir"

container="prr-backup-restore-$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
cleanup() {
  local status=$?
  trap - EXIT
  docker rm -f "$container" >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT

docker run --detach --rm \
  --name "$container" \
  --network none \
  --env POSTGRES_HOST_AUTH_METHOD=trust \
  postgres:16 >/dev/null
for _ in $(seq 1 60); do
  if docker exec "$container" pg_isready --username postgres --dbname postgres >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker exec "$container" pg_isready --username postgres --dbname postgres >/dev/null
docker exec "$container" createdb --username postgres prr_restore
docker exec -i "$container" pg_restore \
  --exit-on-error \
  --no-owner \
  --no-acl \
  --username postgres \
  --dbname prr_restore <"$backup_dir/database.dump"

restored=$(docker exec "$container" psql \
  --username postgres \
  --dbname prr_restore \
  --tuples-only \
  --no-align \
  --field-separator '|' \
  --command 'SELECT count(*), COALESCE(max(id), 0) FROM audit_auditlog')
IFS='|' read -r restored_count restored_max_id <<<"$restored"
migration_count=$(docker exec "$container" psql \
  --username postgres \
  --dbname prr_restore \
  --tuples-only \
  --no-align \
  --command 'SELECT count(*) FROM django_migrations')
if [ "$migration_count" -le 0 ]; then
  echo "restored database has no migration evidence" >&2
  exit 1
fi
python3 "$helper" verify-restored \
  --manifest "$backup_dir/manifest.json" \
  --restored-count "$restored_count" \
  --restored-max-id "$restored_max_id"
echo "backup restore verified: ${restored_count} audit rows, max ID ${restored_max_id}"
