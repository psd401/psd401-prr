# ADR 0002: production-only direct CDK deployments

- **Status:** Accepted
- **Date:** 2026-08-25

## Decision

`prr.psd401.net` is the only deployment target. The retired
`dev-prr.psd401.net` environment is not part of application delivery.

After application changes merge to `main`, deploy with one command:

```bash
cd infra/cdk && bunx cdk deploy psd401-prr-prod --require-approval never --profile psd401-prr-prod
```

Nothing runs between merge and that command.

CDK owns the application source asset and container rollout. Backup, migration,
health handling, and automatic rollback are internal steps of the same CDK
operation.

## Consequences

- A merged change can move directly to production with the CDK command.
- Development and CI validation do not authorize or block deployment.
- No application source is copied manually to the host.
- The existing production host and private runtime configuration are reused.
