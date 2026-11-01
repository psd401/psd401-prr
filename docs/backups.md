# Application backup and restore runbook

Production has two independent recovery layers:

- AWS Data Lifecycle Manager takes a daily encrypted EBS snapshot and retains
  14 snapshots. This is whole-host disaster recovery, not records retention.
- `prr-app-backup@prod.timer` creates a daily PostgreSQL custom-format dump and
  a canonical, independently readable audit-ledger export in
  `psd401-prr-backups-prod`.

Operational status verified 2026-08-25: the timer is enabled and active,
manifest-marked uploads exist through that date, the CloudWatch application
backup alarm is healthy, and the encrypted daily EBS snapshot policy is
running. The retained 2026-08-25 isolated drill restored the newest selected
backup into a temporary encrypted, no-ingress recovery host and a PostgreSQL 16
container with `--network none`, proving 15,385 audit rows through ID 15,385;
the entire temporary stack was then deleted. Its machine-readable evidence is
[`operations/production-restore-drill-2026-08-25.json`](operations/production-restore-drill-2026-08-25.json).
The companion
[`operations/production-restore-drill-tools-2026-08-25.json`](operations/production-restore-drill-tools-2026-08-25.json)
names the immutable versioned S3 archive and binds its checksum to the exact two
repository tool files executed during the drill. The migration gate recomputes
both tool sizes and SHA-256 digests from the selected release tree.
That evidence authorizes only the exact additive migration set pinned by the
reviewed release. A migration-free replacement still requires its own new
complete backup before cutover.

The application backup is append-only. The host can create a new object only
when the key does not already exist; it cannot list, read, overwrite, or delete
anything in the backup bucket. S3 versioning and a retained bucket provide an
additional recovery boundary if an administrator changes that policy later.

## What one successful backup contains

Every run gets a UUID and writes this prefix:

```text
app-backups/YYYY/MM/DD/<backup-id>/
  audit.jsonl.gz
  database.dump
  manifest.json
```

`audit.jsonl.gz` is generated first and contains the complete append-only audit
ledger in primary-key order. `database.dump` is a PostgreSQL custom-format dump
that passes `pg_restore --list`. `manifest.json` records the release image,
environment, creation time, object sizes, SHA-256 values, and audit row/max-ID
boundary. The manifest uploads last and is the commit marker; a prefix without
it is incomplete and must not be selected for restore.

All three uploads require `If-None-Match: *`, send an S3 SHA-256 checksum, and
request server-side encryption. Temporary files are mode 600 inside
`/srv/prr/backups` and are removed after every run. The only retained local
artifact is `last-success.json`, which contains no secrets.

## Initial turn-up only: install the job

Deploy the CDK stack first so the instance role has its conditional S3 write
and CloudWatch metric permission. After shipping the same release to the host
and starting its containers:

```bash
cd /srv/prr/app
sudo infra/aws/install-app-backup.sh prod
```

The installer copies root-owned scripts and systemd units, enables the timer,
and immediately runs a backup. It does not read or copy application secrets.
The backup commands execute inside the already-running web and database
containers.

Production has already completed this installation. Do not rerun the installer
from an arbitrary host tree or use it as a recurring update path. A future
installer change must be delivered from an exact merged SHA by the approved
serialized deployment mechanism, under the same lock and provenance record as
the application release.

Verify the installation:

```bash
sudo systemctl is-enabled prr-app-backup@prod.timer
sudo systemctl is-active prr-app-backup@prod.timer
sudo systemctl status prr-app-backup@prod.service --no-pager
sudo journalctl -u prr-app-backup@prod.service -n 100 --no-pager
sudo python3 -m json.tool /srv/prr/backups/last-success.json
```

The service log must end with an S3 `manifest.json` URI. Do not treat an enabled
timer or an incomplete prefix as evidence of a successful backup.

The timer runs daily at 09:30 UTC with up to 15 minutes of random delay. It is
persistent, so a missed run starts after boot. Run one manually with:

```bash
sudo systemctl start prr-app-backup@prod.service
```

Concurrent runs are rejected by a local lock and do not create a second
backup.

## Monitoring and failure response

Each completed run publishes `PSD401/PRR AppBackupSuccess=1` with the `prod`
environment dimension. A failed run publishes `0` when the AWS CLI is
available. The CDK alarm
`psd401-prr-prod-app-backup-missing-or-failed` also treats missing daily data as
a breach.

For an alarm:

1. Inspect `journalctl` for the first failing command; later cleanup messages
   are not the root cause.
2. Confirm the web and database containers are healthy and the host has free
   space under `/srv/prr`.
3. Confirm the instance still has the `psd401-prr-prod-runtime` role and can
   reach S3 and CloudWatch through IMDSv2.
4. Start the service manually after fixing the cause and require a new
   `manifest.json` URI. Never reuse or alter an incomplete prefix.
5. Perform the restore drill below if the failure involved PostgreSQL tooling,
   the manifest, checksums, or S3.

The alarm emails `techalerts@psd401.net` through the `psd401-prr-prod-alarms` SNS
topic when it fires and when it clears. Alarm state and service logs remain the
authoritative checks.

## Restore drill

Complete and retain this drill before authorizing any migration-bearing or
data-destructive deployment. The recurring path rejects pending migrations
unless the reviewed deployer pins the exact additive set, file digests, and
retained drill evidence, so it cannot be used to bypass this requirement. A
quarterly drill and a drill after PostgreSQL major-version changes remain
recommended operational practice even when no such release is scheduled.

The production host intentionally cannot retrieve backups. Use a separately
authorized AWS operator identity to select a prefix that ends in
`manifest.json`, then download exactly that prefix only to a named, protected
recovery environment approved for production requester/FERPA data. An ordinary
developer workstation is not such an environment, and local development must
use synthetic fixtures. Disable every application/provider integration on the
recovery host and record the temporary-data retention/removal owner before the
transfer:

```bash
install -d -m 700 /tmp/prr-restore-drill
aws s3 cp \
  s3://psd401-prr-backups-prod/app-backups/YYYY/MM/DD/<backup-id>/ \
  /tmp/prr-restore-drill/ \
  --recursive
```

Copy `backup_manifest.py` and `verify-app-backup-restore.sh` from one exact,
reviewed release to the protected recovery environment. Do not transfer the
backup to or run the drill on the live production application host. Then:

```bash
PRR_BACKUP_HELPER=/path/to/backup_manifest.py \
  /path/to/verify-app-backup-restore.sh /tmp/prr-restore-drill
```

The verifier recomputes every checksum, starts a disposable `postgres:16`
container with `--network none`, restores the dump into a new database, checks
that migrations exist, and proves the restored audit table reaches the audit
boundary recorded in the export. It never connects to an application
database, and it removes the disposable container on success or failure.

After evidence is recorded, securely remove the temporary downloaded artifacts
under the approved recovery-host procedure. Do not improvise deletion or move
the artifacts to a developer machine.

Record the selected manifest URI, release commit, drill time, verifier output,
and operator in the district operations log. Exercise this after initial
installation and before the migration-bearing or data-destructive work described
above.

## Retention and deletion

There is deliberately no lifecycle deletion rule and no backup-delete
permission in the runtime role. The 14-snapshot DLM setting is a recovery
window, not authorization to dispose of application backups or public records.
Once the CIO records the governing retention schedule, encode it as a separate
tested infrastructure change that preserves legal holds. Until then, backups
accumulate and must not be deleted.
