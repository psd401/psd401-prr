# Files

- [AWS Production Deployment](aws-deployment.md) - The single CDK production deployment command for prr.psd401.net.
- [Application Backup and Restore](backups.md) - The two production recovery layers for PSD401 PRR - the EBS snapshot policy and the append-only daily PostgreSQL plus audit-ledger application backup - with the manifest commit marker, monitoring, and the restore drill required before migration-bearing releases.
- [Deployment Guide](deployment.md) - Local development, the Docker Compose file layering for the AWS host and its object-store overlays, the Caddy TLS front end and forwarded-proto trust, the prr-compose host wrapper that keeps the Gmail activation overlay applied, and the direct production CDK deployment.
- [Testing and Quality Gates](testing.md) - Which test lanes exist for the PSD401 PRR repository, what each one proves, the verify-fast script and its flags, the Postgres contract lane, patch coverage, the flaky-test quarantine, and mutation testing. Read before choosing a validation command or changing a gate.
