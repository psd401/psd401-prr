# AWS hosting CDK

Three stacks in account `338414773271`, `us-west-2`:

- **`psd401-prr-prod`** hosts production at `https://prr.psd401.net`: a
  dedicated t3a.large host in a subnet whose only route leaves through the
  internet gateway (the subnet belongs to psd-aws-infra), a records bucket, a write-only backups bucket, narrow SSM
  managed-node permissions, fixed parameter-free email diagnostics and
  inbound-disabled-incident activation documents,
  an empty Google OIDC client-secret container, a daily EBS snapshot policy,
  a missing-or-failed backup alarm, a root-disk-filling alarm, a
  missing-or-failed inbound Gmail poll alarm, and an SNS topic that emails
  every alarm transition to `alarmEmails` in `lib/config.ts`. The host's daily
  PostgreSQL/audit backup job is installed from `infra/aws` after application
  deployment; see `docs/backups.md`.
- **`psd401-prr-terraform-state`** is the independent bootstrap for the shared
  Google-identity Terraform state: one private, encrypted, versioned, retained
  S3 bucket. Terraform's native S3 lock file prevents concurrent writers.

All AWS resources for this application are created here; nothing is
provisioned from the console or the CLI.

The subnet is the point of the design. Every existing subnet in this VPC
shares a route table that reaches on-prem over a VPN gateway and two VPC
peerings. An internet-facing host holding FERPA-covered records must not have
those paths, so the host sits in a subnet of its own with its own route table,
and the stack never references the shared DMZ route table, NACL, or subnet.
This stack built that subnet; psd-aws-infra now owns it, and the stack finds
it the psd-dev-standards 08 way: the one subnet in the VPC tagged
`psd:shared = network` and `Name = psd401-prr-<env>`
([`lib/shared-subnet.ts`](lib/shared-subnet.ts)). Synth fails unless exactly
one subnet carries both tags.

**Moved to psd-aws-infra (2026-11-03).** psd-dev-standards 08 gives every
subnet in a shared VPC to psd-aws-infra, so this stack handed over the
subnet, its route table, the association and the default route
(psd401/psd-aws-infra#42); psd-aws-infra imports them. The isolation above
doesn't change: the subnet keeps its own route table with only the internet
route. The four left this stack with Retain deployed first (#22).

The instance's `SubnetId` couldn't change in place: CloudFormation replaces
an instance for any `SubnetId` change, including a reference swapped for the
same literal ID, and the root volume holds PostgreSQL. (Observed, not tested:
unexecuted change sets against the live stack on 2026-11-02 reported
`Replacement: True`, `SubnetId`, Always, Static.) So the instance and its
Elastic IP association, both retained (#24), left the stack and came back by
`cdk import` with the subnet as a literal ID (#25). Both keep Retain.

Neither stack owns the VPC, the internet gateway, the Route 53 zone, any DNS
record, the Elastic IP allocation, or the CDK bootstrap stack. Production
associates `eipalloc-0f263927cd1983f48` (54.68.97.81) without owning it: both
public DNS and the district's internal DNS already answer with that address,
so no `cdk destroy` may release it.

`psd401-prr-prod-runtime` is the IAM role name
[`../terraform/google-identity`](../terraform/google-identity) matches on in
its `aws_runtimes` map, and its provider extracts it out of an assumed-role
ARN. Changing that name, or calling as a user rather than assuming the role,
breaks keyless Google group lookups.

The application association preserves a validated retained Gmail inbound
activation across routine image replacement, performs an immediate bounded
stored-receipt reconciliation and checkpoint replay, and then requires a
subsequent beat/worker poll to advance the checkpoint. It also checks the public
intake bootstrap, its two cookies, and its built asset with non-submitting GETs.
These checks are part of the single CDK deployment and create no synthetic
public-records request; they do not exercise a public API submission.

## Operating

```text
bun install
bun test
bunx cdk diff psd401-prr-prod
bunx cdk deploy psd401-prr-prod
bunx cdk deploy psd401-prr-terraform-state
```

`bun test` and `bunx cdk synth` need no AWS credentials: the Ubuntu 24.04 AMI
lookup and the subnet tag lookup are cached in the committed
`cdk.context.json`. Refresh the AMI deliberately, not routinely, and only
alongside a maintenance window:

```text
bunx cdk context --reset "ssm:account=338414773271:parameterName=/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id:region=us-west-2"
bunx cdk synth
```

A new AMI id replaces the instance. The root volume has
`DeleteOnTermination: false`, so the old volume survives, but the host is
rebuilt and its Docker state is gone.

The cached subnet lookup is the same kind of value: a different subnet ID
replaces the instance and its PostgreSQL root volume. Reset its
`cc-api-provider:...:typeName=AWS::EC2::Subnet` key only after a tag change in
psd-aws-infra, and read the `cdk diff` before deploying.

Termination protection is on for both the stack and the instance. Removing a
stack means clearing both first, and the buckets and secret are retained
rather than deleted.

The security group admits only 80 and 443; there is no SSH ingress, and
Systems Manager is the only management path. Verify the host is registered:

```text
aws ssm describe-instance-information --profile psd401-prr-prod \
  --region us-west-2 --filters Key=InstanceIds,Values=<instance-id>
```

Require `PingStatus=Online` before invoking either fixed email document. The
runtime role has no Parameter Store read, and the stack defines no parameterized
or general deployment document. The activation document is limited to one
reviewed, parameter-free operation for the exact disabled-inbound incident; it
cannot select a command, path, image, mailbox, or value. It serializes with
deployments and backups, creates and validates new image-matching backup evidence,
rejects unresolved deployment journals and pending migrations, retains fixed
private recovery evidence, validates and fingerprints both complete private
runtime files, and requires their derived complete service environments to
match the existing containers before any recreation. The adopted baseline must
also render the retained release-tree context and prove that it matches the
legacy live context before that retained context can recreate a service. It
recovers valid interrupted-activation residue—including the adopted baseline's
expected temporary mixed working-directory labels and a service container
missing mid-recreation—only when at least one validated service container remains
as live evidence, before requiring all processes to exist and run. It pins first-start mutations off
and requires object storage, ClamAV, Redis, and Gmail principal readiness. It
stops beat first and gives the Celery worker an unbounded warm-shutdown grace
period so active acknowledged legal work is not killed. The replacement beat
then enqueues a new minute-poll task, and the replacement worker must complete
that exact task and advance the Gmail checkpoint
before it reports success. Checkpoint admission reserves the document's entire
timeout as margin inside the bounded Gmail resync window.
Before activation, the diagnostic is expected to return a nonzero overall status
because inbound is intentionally disabled; its detailed evidence must otherwise
be complete.

The original dedicated key resource remains associated only because removing
`KeyName` would replace the data-bearing instance; it is not a documented
operational path and must not be retrieved or used. Its identifier and
private-key parameter are not stack outputs. The outputs report both bucket
names, the backup alarm, both fixed email document names, the OIDC secret ARN,
the runtime role name and ARN, and the Elastic IP allocation.

The stack creates the Gmail missing-or-failed alarm with missing telemetry
treated as breaching. Each recipient must confirm the SNS subscription email
AWS sends after the first deploy that creates the topic; until then no alarm
email is delivered. The fixed diagnostic document remains the canonical
PII-free detailed health check.

Deploying is one step of turning up an environment. Continue with
[`../../docs/aws-deploy.md`](../../docs/aws-deploy.md).
