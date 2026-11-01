---
type: Operations
title: AWS Production Deployment
description: The single CDK production deployment command for prr.psd401.net.
tags: [operations, deployment, aws, cdk, production]
---

# AWS production deployment

Production application releases use the existing `psd401-prr-prod` CDK stack.

From merged `main`, run:

```bash
cd infra/cdk && bunx cdk deploy psd401-prr-prod --require-approval never --profile psd401-prr-prod
```

That is the complete deployment procedure. Nothing runs between merge and the
command.

CDK packages the repository as an application asset, sends it to the existing
host, builds the image, creates an application backup, applies Django
migrations, replaces the application containers, and automatically restores
the previous image if replacement fails.

The host's private `.env` and `.env.app` files remain in place. They are not
included in the CDK source asset.

The retired development environment is not a deployment target.

## What a release verifies

Beyond the image rollout, the same CDK transaction:

- takes a fresh application backup before migrations ([backups.md](backups.md));
- preserves an already-approved inbound Gmail activation only from the exact private
  activation overlay, converts stored eligible unmatched receipts, and replays the
  durable Gmail checkpoint, then requires a later minute-scheduled poll to advance
  the checkpoint. A failed or missing poll rolls the containers back instead of
  leaving intake silently disabled ([correspondence.md](../domain/correspondence.md));
- checks `/request` without submitting, requires the signed-form bootstrap cookies,
  and fetches the built JavaScript asset.

The CloudWatch alarm `psd401-prr-prod-gmail-inbound-missing-or-failed` breaches on two
consecutive failed health values or two minutes of missing poll telemetry. The stack's
alarms (this one, the app backup alarm, and the root-disk alarm) all publish to one SNS
topic, `psd401-prr-<env>-alarms`, on both ALARM and OK transitions. That topic emails the
addresses listed in `alarmEmails` in [`infra/cdk/lib/config.ts`](../../infra/cdk/lib/config.ts).
Each address must confirm the AWS subscription email before it receives anything. The
[backups page](backups.md#monitoring-and-failure-response) and
[background jobs](../architecture/background-jobs.md#gmail-poll-telemetry) describe the
metrics behind these alarms.

## Settings are not part of a release

Application behavior that a district tunes (switches, limits, domains, Google
identities, and sign-in) is changed on **Configuration > District settings** with a
written reason. It takes effect in web, worker, and beat without a deploy. See
[configuration.md](../architecture/configuration.md). Operator-held `.env` and
`.env.app` files are not copied into the CDK source asset, and a line for a
moved setting in `.env.app` does nothing once the import has run.
