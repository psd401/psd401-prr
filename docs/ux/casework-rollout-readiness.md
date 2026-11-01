# Casework rollout completion

Issue #154 completed the rebuilt casework experience: role journeys,
accessibility, bounded 2,000-message behavior, PostgreSQL performance budgets,
staff guidance, and production rollout.

The implementation is merged and live at `prr.psd401.net`. The deployed merge
commit is `fd5ae4d002d232e91215cabe3287e918d193c326`.

Future application releases use the single production command:

```bash
cd infra/cdk && bunx cdk deploy psd401-prr-prod --require-approval never --profile psd401-prr-prod
```

Nothing runs between merge and that command. CDK performs backup, migration,
container replacement, health handling, and automatic rollback inside it.

Staff guidance remains in
[docs/casework-staff-guide.md](../casework-staff-guide.md), and the acceptance
record remains in [docs/ux/casework-uat.md](casework-uat.md).
