#!/bin/bash
# ABOUTME: Installs and immediately exercises the daily AWS app-backup timer.
# ABOUTME: Writes no secrets; the job uses Docker locally and the EC2 instance role.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "run this installer with sudo" >&2
  exit 1
fi
environment="${1:-prod}"
case "$environment" in
  ""|*[!a-z0-9-]*|-*|*-) echo "invalid backup environment" >&2; exit 64 ;;
esac
script_dir=$(cd "$(dirname "$0")" && pwd -P)

install -d -o ubuntu -g ubuntu -m 700 /srv/prr/backups
install -d -o root -g root -m 755 /usr/local/lib/prr-backup
install -o root -g root -m 755 "$script_dir/backup_manifest.py" \
  /usr/local/lib/prr-backup/backup_manifest.py
install -o root -g root -m 755 "$script_dir/run-app-backup.sh" \
  /usr/local/sbin/prr-run-app-backup
install -o root -g root -m 755 "$script_dir/verify-app-backup-restore.sh" \
  /usr/local/sbin/prr-verify-app-backup-restore
install -o root -g root -m 644 "$script_dir/prr-app-backup@.service" \
  /etc/systemd/system/prr-app-backup@.service
install -o root -g root -m 644 "$script_dir/prr-app-backup@.timer" \
  /etc/systemd/system/prr-app-backup@.timer

# Root-volume usage reporting. Installed alongside the backup because both are
# host-health jobs that publish a PSD401/PRR metric an alarm watches, and
# because a full disk breaks backups too.
install -o root -g root -m 755 "$script_dir/report-disk-usage.sh" \
  /usr/local/sbin/prr-report-disk-usage
install -o root -g root -m 644 "$script_dir/prr-disk-usage@.service" \
  /etc/systemd/system/prr-disk-usage@.service
install -o root -g root -m 644 "$script_dir/prr-disk-usage@.timer" \
  /etc/systemd/system/prr-disk-usage@.timer

systemctl daemon-reload
systemctl enable --now "prr-app-backup@${environment}.timer"
systemctl start "prr-app-backup@${environment}.service"
systemctl is-active --quiet "prr-app-backup@${environment}.timer"
echo "installed and verified prr-app-backup@${environment}.timer"

systemctl enable --now "prr-disk-usage@${environment}.timer"
systemctl start "prr-disk-usage@${environment}.service"
systemctl is-active --quiet "prr-disk-usage@${environment}.timer"
echo "installed and verified prr-disk-usage@${environment}.timer"
