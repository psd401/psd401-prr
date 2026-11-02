// ABOUTME: One dedicated public-records host per environment: a hardened EC2
// ABOUTME: instance in an isolated subnet, its buckets, role, and snapshots.

import * as cdk from "aws-cdk-lib";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cloudwatchActions from "aws-cdk-lib/aws-cloudwatch-actions";
import * as dlm from "aws-cdk-lib/aws-dlm";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as s3assets from "aws-cdk-lib/aws-s3-assets";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as sns from "aws-cdk-lib/aws-sns";
import * as snsSubscriptions from "aws-cdk-lib/aws-sns-subscriptions";
import * as ssm from "aws-cdk-lib/aws-ssm";
import type { Construct } from "constructs";
import * as path from "node:path";

import { grantAdvisoryBedrockInference } from "./advisory-bedrock";
import { UBUNTU_2404_AMD64_GP3_SSM_PARAM, type PrrEnvConfig } from "./config";
import {
  acknowledgeGuard,
  GUARD_S3_BASELINE,
  SHARED_SUPPRESSION_REASONS,
  skipCheckov,
} from "./suppressions";

export interface PrrHostingStackProps extends cdk.StackProps {}

export class PrrHostingStack extends cdk.Stack {
  constructor(
    scope: Construct,
    id: string,
    config: PrrEnvConfig,
    props: PrrHostingStackProps = {},
  ) {
    super(scope, id, {
      ...props,
      stackName: `psd401-prr-${config.envName}`,
      description: `Public records request host, ${config.envName} (${config.hostname})`,
      // Literal account/region, not CDK_DEFAULT_*: CI must synthesize this
      // app without AWS credentials.
      env: { account: config.account, region: config.region },
      // The root volume holds FERPA-covered records. A stray `cdk destroy` or
      // console terminate has to fail loudly rather than take the data.
      terminationProtection: true,
    });

    // Grant S3 log delivery through the access-log bucket's policy, not the
    // legacy LogDeliveryWrite ACL, which forces ObjectWriter ownership. Set
    // on this stack rather than in cdk.json so the tests, which build their
    // own App, synthesize exactly what deploys; it must precede the buckets.
    this.node.setContext(
      "@aws-cdk/aws-s3:serverAccessLogsUseBucketPolicy",
      true,
    );

    // The district tag set is applied once at the app level (lib/app.ts).
    // Only the classification is stack-specific: everything here sits on, or
    // holds, FERPA-covered student records. The higher priority is what makes
    // this win over the app-level default for the same key.
    cdk.Tags.of(this).add("psd:data-classification", "student-pii", {
      priority: 200,
    });

    const vpc = ec2.Vpc.fromVpcAttributes(this, "Vpc", {
      vpcId: config.vpcId,
      vpcCidrBlock: config.vpcCidr,
      availabilityZones: [config.availabilityZone],
    });

    // The host's subnet belongs to psd-aws-infra, which owns every subnet in a
    // shared VPC (psd-dev-standards 08 "Shared network";
    // psd401/psd-aws-infra#42). It is not one of the VPC's other subnets:
    // those share a route table that reaches on-prem over a VPN gateway and
    // two VPC peerings, and an internet-facing host holding student records
    // must not have those paths. This one has its own route table whose only
    // route goes to the internet gateway, and it assigns a public address at
    // launch so user data reaches the internet before the Elastic IP lands.
    const subnet = ec2.Subnet.fromSubnetAttributes(this, "Subnet", {
      subnetId: config.subnetId,
      availabilityZone: config.availabilityZone,
    });
    cdk.Annotations.of(subnet).acknowledgeWarning(
      "@aws-cdk/aws-ec2:noSubnetRouteTableId",
      "This stack places the host in the subnet and never reads or adds to " +
        "its route table, which psd-aws-infra owns.",
    );

    const securityGroup = new ec2.SecurityGroup(this, "SecurityGroup", {
      vpc,
      securityGroupName: `psd401-prr-${config.envName}`,
      description: `Public records request host, ${config.envName}`,
      allowAllOutbound: true,
    });
    securityGroup.addIngressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(80),
      "ACME HTTP-01 challenge and the redirect to HTTPS",
    );
    securityGroup.addIngressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(443),
      "Requester and staff web traffic",
    );
    // No SSH ingress: Systems Manager is the only management path.
    // Narrowing these rules would mean an allowlist of the general public.
    // The reason both tools print is in SHARED_SUPPRESSION_REASONS, written
    // once.
    const publicWebPorts =
      `${SHARED_SUPPRESSION_REASONS.publicWebPorts} The host is ` +
      `${config.hostname}.`;
    cdk.Validations.of(securityGroup).acknowledge({
      id: "AwsSolutions::AwsSolutions-EC23",
      reason: publicWebPorts,
    });
    skipCheckov(securityGroup, [
      { id: "CKV_AWS_260", comment: publicWebPorts },
    ]);

    // Retain the original dedicated key association so this in-place hardening
    // does not replace the data-bearing instance. It is not a documented
    // operational access path; Systems Manager below is.
    const keyPair = new ec2.KeyPair(this, "KeyPair", {
      keyPairName: `psd401-prr-${config.envName}`,
      type: ec2.KeyPairType.ED25519,
    });
    // Frozen at its deployed tags, outside the district tag set. A key pair's
    // tags cannot be updated in place: CloudFormation replaces the key pair,
    // and the instance names it, so any tag change replaces the instance and
    // its inline root volume - PostgreSQL and the audit trail. The override
    // replaces whatever Tags.of() would render. test/app.test.ts pins these.
    (keyPair.node.defaultChild as ec2.CfnKeyPair).addPropertyOverride("Tags", [
      { Key: "Application", Value: "psd401-prr" },
      { Key: "Environment", Value: config.envName },
      { Key: "ManagedBy", Value: "cdk" },
    ]);

    // Server access logs for the records and backups buckets. SSE-S3, not
    // KMS: S3 log delivery cannot write to an SSE-KMS default-encrypted
    // target. Retained with no lifecycle rule, like every bucket here: the
    // log keys name cases, and their retention schedule under RCW 40.14 is
    // unconfirmed, so nothing deletes them automatically.
    const accessLogsBucket = new s3.Bucket(this, "AccessLogsBucket", {
      bucketName: `psd401-prr-access-logs-${config.envName}`,
      versioned: true,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      // With ownership enforced, CDK grants log delivery through the bucket
      // policy (scoped to this account) instead of the legacy ACL.
      objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    cdk.Validations.of(accessLogsBucket).acknowledge({
      id: "AwsSolutions::AwsSolutions-S1",
      reason: SHARED_SUPPRESSION_REASONS.accessLogBucketSelfLogging,
    });
    skipCheckov(accessLogsBucket, [
      {
        id: "CKV_AWS_18",
        comment: SHARED_SUPPRESSION_REASONS.accessLogBucketSelfLogging,
      },
    ]);

    const recordsBucket = new s3.Bucket(this, "RecordsBucket", {
      bucketName: `psd401-prr-records-${config.envName}`,
      versioned: true,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      serverAccessLogsBucket: accessLogsBucket,
      serverAccessLogsPrefix: "records/",
      // No lifecycle rule anywhere in this stack. The district's retention
      // schedule under RCW 40.14 is unconfirmed, and deleting a public record
      // early is the legal exposure; storage cost is not.
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    const backupsBucket = new s3.Bucket(this, "BackupsBucket", {
      bucketName: `psd401-prr-backups-${config.envName}`,
      versioned: true,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      serverAccessLogsBucket: accessLogsBucket,
      serverAccessLogsPrefix: "backups/",
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    for (const bucket of [accessLogsBucket, recordsBucket, backupsBucket]) {
      acknowledgeGuard(bucket, GUARD_S3_BASELINE);
    }
    acknowledgeGuard(accessLogsBucket, {
      s3_bucket_logging_enabled_check:
        SHARED_SUPPRESSION_REASONS.accessLogBucketSelfLogging,
    });

    // The name is a contract: infra/terraform/google-identity matches this
    // exact string when it federates the host to Google Workspace.
    const runtimeRole = new iam.Role(this, "RuntimeRole", {
      roleName: `psd401-prr-${config.envName}-runtime`,
      assumedBy: new iam.ServicePrincipal("ec2.amazonaws.com"),
      description: `Public records request host runtime, ${config.envName}`,
    });

    // DJANGO_SECRET_KEY and PRR_SETTINGS_ENCRYPTION_KEY, read by every Django
    // process at start through this role (psd-dev-standards standards/04 rule
    // 15a; config/runtime_secrets.py). L1 on purpose: the L2 Secret generates
    // a random value by default, and a new signing key would strand every
    // secret in the settings store. The stack creates the empty container
    // only; an operator copies the live values in (docs/aws-deploy.md).
    // Retained, so deleting or replacing the stack never loses the key. The
    // Google OIDC client secret is not here: it is a write-only district
    // setting stored encrypted in the application (Phase C, #262).
    const runtimeSecret = new secretsmanager.CfnSecret(this, "RuntimeSecret", {
      name: `psd401-prr/${config.envName}/runtime`,
      description:
        "Django signing key and settings-store key for the PRR runtime. " +
        "Copied from the live host, never generated.",
    });
    runtimeSecret.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);
    // Two distinct findings about this one secret: no rotation, and no
    // customer-managed key. Each reason below is the whole justification -
    // the structured text is what the tools print to whoever hits the
    // finding, so it is not restated as a comment here.
    cdk.Validations.of(runtimeSecret).acknowledge({
      id: "AwsSolutions::AwsSolutions-SMG4",
      reason:
        "This value is the settings-store encryption key as well as the " +
        "Django signing key (config/runtime_secrets.py). Automatic rotation " +
        "would strand every encrypted district setting and invalidate every " +
        "session and signed token at once, so the key is copied from the " +
        "live host and never rotated; a deliberate rotation is an " +
        "application-level migration, not a Secrets Manager schedule.",
    });
    skipCheckov(runtimeSecret, [
      {
        id: "CKV_AWS_149",
        comment:
          "AWS-managed aws/secretsmanager encryption. Read access is a " +
          "single named role; a customer-managed key would add another " +
          "recovery dependency around the one value the host cannot be " +
          "rebuilt without, for no additional access control.",
      },
    ]);
    runtimeRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["secretsmanager:GetSecretValue"],
        resources: [runtimeSecret.ref],
      }),
    );
    const emailDiagnosticsDocumentName =
      `psd401-prr-${config.envName}-email-diagnostics`;
    const gmailActivationTimeoutSeconds = 7200;
    // Run Command needs a managed-node message channel, but the AWS-managed
    // policy also grants account-wide Parameter Store reads. Containers can
    // reach this role through IMDSv2, so grant only registration/message
    // actions and this stack's fixed, operation-specific documents.
    runtimeRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "ssm:DescribeAssociation",
          "ssm:ListAssociations",
          "ssm:ListInstanceAssociations",
          "ssm:UpdateAssociationStatus",
          "ssm:UpdateInstanceAssociationStatus",
          "ssm:UpdateInstanceInformation",
        ],
        resources: ["*"],
      }),
    );
    runtimeRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "ssmmessages:CreateControlChannel",
          "ssmmessages:CreateDataChannel",
          "ssmmessages:OpenControlChannel",
          "ssmmessages:OpenDataChannel",
        ],
        resources: ["*"],
      }),
    );
    // us-west-2 predates the Message Gateway endpoint; retain the legacy
    // delivery/reply channel until AWS confirms this account no longer uses it.
    runtimeRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "ec2messages:AcknowledgeMessage",
          "ec2messages:DeleteMessage",
          "ec2messages:FailMessage",
          "ec2messages:GetEndpoint",
          "ec2messages:GetMessages",
          "ec2messages:SendReply",
        ],
        resources: ["*"],
      }),
    );
    runtimeRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["ssm:DescribeDocument", "ssm:GetDocument"],
        resources: [
          cdk.Stack.of(this).formatArn({
            service: "ssm",
            resource: "document",
            resourceName: emailDiagnosticsDocumentName,
          }),
        ],
      }),
    );

    // Google Workload Identity Federation proves this exact AWS role by
    // presenting a signed GetCallerIdentity request. This action has no data
    // or mutation surface and is the only STS action the runtime receives.
    runtimeRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["sts:GetCallerIdentity"],
        resources: ["*"],
      }),
    );
    grantAdvisoryBedrockInference(runtimeRole, config.account);

    const quarantinePrefixes = [
      "quarantine/intake/*",
      "quarantine/correspondence/raw/*",
      "quarantine/correspondence/attachments/*",
      "quarantine/vault-messages/*",
      // Legacy files being migrated from a retired source system are scanned
      // in quarantine exactly like any other untrusted bytes.
      "quarantine/legacy-files/*",
      // Files a Records Officer uploads onto a case wait here for ClamAV
      // before anything is retained under records/.
      "quarantine/record-uploads/*",
    ];
    const readableWritablePrefixes = [
      ...quarantinePrefixes,
      "intake/*",
      "correspondence/attachments/*",
      "records/*",
      // Reviewable PDF renditions of records that are not themselves PDFs.
      // Separate from records/ because the original is the record and this is
      // only the copy a human reads and redacts.
      "derivatives/*",
      "productions/*",
      "exemption-logs/*",
      "vault-exports/*",
      "vault-messages/*",
    ];

    // Reads and writes stay inside application-generated namespaces. The
    // application uses single-object operations only; it never creates
    // multipart uploads.
    runtimeRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["s3:GetObject", "s3:PutObject"],
        resources: readableWritablePrefixes.map((prefix) =>
          recordsBucket.arnForObjects(prefix),
        ),
      }),
    );
    runtimeRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["s3:GetBucketVersioning"],
        resources: [recordsBucket.bucketArn],
      }),
    );
    runtimeRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["s3:ListBucketVersions"],
        resources: [recordsBucket.bucketArn],
        conditions: {
          StringLike: { "s3:prefix": quarantinePrefixes },
        },
      }),
    );
    // Physical deletion is required for quarantine cleanup, including every
    // version and delete marker, but must mirror secure_files.py's generated
    // key allowlist. Clean and responsive records remain non-deletable.
    runtimeRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["s3:DeleteObject", "s3:DeleteObjectVersion"],
        resources: quarantinePrefixes.map((prefix) =>
          recordsBucket.arnForObjects(prefix),
        ),
      }),
    );
    // Write-only backups: a compromised host can add a backup but cannot read
    // or destroy the ones already there.
    runtimeRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["s3:PutObject"],
        resources: [backupsBucket.arnForObjects("app-backups/*")],
        conditions: {
          Null: { "s3:if-none-match": "false" },
          StringEquals: { "s3:x-amz-server-side-encryption": "AES256" },
        },
      }),
    );
    runtimeRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["cloudwatch:PutMetricData"],
        resources: ["*"],
        conditions: {
          StringEquals: { "cloudwatch:namespace": "PSD401/PRR" },
        },
      }),
    );

    const userData = ec2.UserData.forLinux();
    userData.addCommands(
      "set -Eeuo pipefail",
      // Retry, because a cold host often races the network coming up, but
      // give up eventually: an unreachable mirror should leave a reason in
      // the console log rather than a machine that boots and hangs forever.
      "retry() {",
      '  local attempts=30 delay=10 n=1',
      '  until "$@"; do',
      '    if [ "$n" -ge "$attempts" ]; then',
      '      echo "giving up on: $* (after $attempts attempts)" >&2',
      "      return 1",
      "    fi",
      '    n=$((n + 1)); sleep "$delay"',
      "  done",
      "}",
      "retry sh -c 'curl -fsSL https://get.docker.com | sh'",
      "systemctl enable --now docker",
      "usermod -aG docker ubuntu",
      "retry snap install aws-cli --classic",
      "mkdir -p /srv/prr/secrets",
      "chown -R ubuntu:ubuntu /srv/prr",
      "chmod 700 /srv/prr/secrets",
    );

    const instance = new ec2.Instance(this, "Instance", {
      vpc,
      vpcSubnets: { subnets: [subnet] },
      instanceName: `psd401-prr-${config.envName}`,
      instanceType: new ec2.InstanceType(config.instanceType),
      machineImage: ec2.MachineImage.fromSsmParameter(
        UBUNTU_2404_AMD64_GP3_SSM_PARAM,
        // Resolved at synth and cached in cdk.context.json so CI needs no AWS
        // credentials. Refreshing the cache replaces the instance.
        { cachedInContext: true },
      ),
      securityGroup,
      keyPair,
      role: runtimeRole,
      creditSpecification: ec2.CpuCredits.UNLIMITED,
      disableApiTermination: true,
      // Set directly rather than through requireImdsv2, which would wrap the
      // instance in a launch template.
      httpTokens: ec2.HttpTokens.REQUIRED,
      // The application runs in containers on a Docker bridge network, which
      // costs one hop; the default limit of 1 would leave it with no way to
      // get S3 or Secrets Manager credentials.
      httpPutResponseHopLimit: 2,
      blockDevices: [
        {
          deviceName: "/dev/sda1",
          volume: ec2.BlockDeviceVolume.ebs(config.volumeSizeGiB, {
            volumeType: ec2.EbsDeviceVolumeType.GP3,
            // Account-level EBS encryption by default is off, so state it.
            encrypted: true,
            // The records volume outlives the instance on purpose.
            deleteOnTermination: false,
          }),
        },
      ],
      userData,
    });
    cdk.Tags.of(instance).add("psd401-prr-backup", config.envName);
    // EC2 detailed (one-minute) monitoring is off. It would buy nothing here:
    // the alarms this stack actually relies on - the daily app backup, root
    // disk fill, inbound Gmail polling - are custom PSD401/PRR metrics the
    // CloudWatch agent publishes on this host, not EC2 hypervisor metrics,
    // and a single long-lived instance has no scaling decision to make at
    // one-minute resolution.
    const noDetailedMonitoring =
      "Operational alarms on this host are custom PSD401/PRR metrics from " +
      "the CloudWatch agent (app backup, root disk, Gmail polling), not " +
      "five-minute EC2 metrics, so detailed monitoring would add cost " +
      "without informing any alarm or scaling decision.";
    cdk.Validations.of(instance).acknowledge({
      id: "AwsSolutions::AwsSolutions-EC28",
      reason: noDetailedMonitoring,
    });
    acknowledgeGuard(instance, {
      ec2_instance_detailed_monitoring_enabled_check: noDetailedMonitoring,
      ec2_instance_no_public_ip_check:
        "This host serves the public records request form, so it is " +
        "reachable from the internet by design, at the Elastic IP associated " +
        "below; the security group opens only 80 and 443. The rule can only " +
        "be met through a NetworkInterfaces block, and moving to one " +
        "replaces the instance, whose inline root volume holds PostgreSQL " +
        "and the audit trail.",
      ec2_instance_nitro_encryption_in_transit_check:
        "This rule asks for an instance family that encrypts traffic between " +
        "instances. There is one instance: PostgreSQL, Redis, ClamAV and the " +
        "application run on it (docker-compose.deploy.yml), so there is no " +
        "instance-to-instance traffic. Client traffic is TLS at Caddy, and " +
        "calls to AWS and Google APIs are TLS.",
    });

    // Subnet handover (README, "Moving to psd-aws-infra"): CloudFormation
    // replaces an instance for any SubnetId change, so the instance and its
    // Elastic IP association leave the stack ("detached") and come back by
    // resource import ("import"). An import rejects every other change, so
    // both phases name the instance by ID; the final template, with no
    // context, points back at the instance. Each phase is deployed once.
    const handover: unknown = this.node.tryGetContext("subnetHandover");
    if (handover !== undefined && handover !== "detached" && handover !== "import") {
      throw new Error(
        `subnetHandover must be "detached" or "import", not ${JSON.stringify(handover)}`,
      );
    }
    const instanceId =
      handover === undefined ? instance.instanceId : config.handoverInstanceId;

    const gmailInboundActivationOverlay = [
      "services:",
      "  web:",
      "    environment: &gmail-inbound-activation",
      '      PRR_GMAIL_ENABLED: "true"',
      '      PRR_GMAIL_INBOUND_POLL_ENABLED: "true"',
      '      RUN_MIGRATIONS: "false"',
      '      AUTO_SEED: "false"',
      "  worker:",
      "    environment: *gmail-inbound-activation",
      "  beat:",
      "    environment: *gmail-inbound-activation",
      "",
    ].join("\n");
    const directDeploymentGmailModePython = [
      "import json",
      "import os",
      "import pathlib",
      "import stat",
      "import subprocess",
      "import sys",
      "overlay = pathlib.Path(sys.argv[1])",
      "container_ids = sys.argv[2:]",
      "if container_ids and (len(container_ids) != 3 or len(set(container_ids)) != 3): raise SystemExit('live Gmail deployment evidence is incomplete')",
      `expected_overlay = ${JSON.stringify(gmailInboundActivationOverlay)}.encode("utf-8")`,
      "overlay_present = overlay.exists() or overlay.is_symlink()",
      "if overlay_present:",
      "    evidence = overlay.lstat()",
      "    if overlay.is_symlink() or not stat.S_ISREG(evidence.st_mode) or evidence.st_uid != 0 or evidence.st_nlink != 1 or stat.S_IMODE(evidence.st_mode) != 0o600: raise SystemExit('Gmail activation overlay is invalid')",
      "    descriptor = os.open(overlay, os.O_RDONLY | os.O_NOFOLLOW)",
      "    try:",
      "        opened = os.fstat(descriptor)",
      "        if (opened.st_dev, opened.st_ino) != (evidence.st_dev, evidence.st_ino): raise SystemExit('Gmail activation overlay changed during validation')",
      "        payload = os.read(descriptor, len(expected_overlay) + 1)",
      "    finally: os.close(descriptor)",
      "    if payload != expected_overlay: raise SystemExit('Gmail activation overlay content is invalid')",
      "if not container_ids:",
      "    print('absent', 'enabled' if overlay_present else 'disabled')",
      "    raise SystemExit(0)",
      "containers = json.loads(subprocess.check_output(['docker', 'inspect', *container_ids], text=True))",
      "if not isinstance(containers, list) or len(containers) != 3: raise SystemExit('live Gmail deployment evidence is incomplete')",
      "services = set()",
      "images = set()",
      "working_dirs = set()",
      "live_modes = set()",
      "for container in containers:",
      "    config = container.get('Config') or {}",
      "    labels = config.get('Labels') or {}",
      "    service = labels.get('com.docker.compose.service')",
      "    working_dir = labels.get('com.docker.compose.project.working_dir')",
      "    config_files = labels.get('com.docker.compose.project.config_files')",
      "    if labels.get('com.docker.compose.project') != 'app' or service not in {'web', 'worker', 'beat'} or not working_dir: raise SystemExit('live Gmail deployment labels are invalid')",
      "    base_files = f'{working_dir}/docker-compose.deploy.yml,{working_dir}/docker-compose.aws.yml'",
      "    activated_files = f'{base_files},{overlay}'",
      "    environment = dict(item.split('=', 1) for item in config.get('Env', []) if '=' in item)",
      "    switches = (environment.get('PRR_GMAIL_ENABLED'), environment.get('PRR_GMAIL_INBOUND_POLL_ENABLED'))",
      "    if config_files == base_files and switches == ('false', 'false'): live_mode = 'disabled'",
      "    elif config_files == activated_files and switches == ('true', 'true'): live_mode = 'enabled'",
      "    else: raise SystemExit('live Gmail deployment state is invalid')",
      "    services.add(service)",
      "    images.add(container.get('Image'))",
      "    working_dirs.add(working_dir)",
      "    live_modes.add(live_mode)",
      "if services != {'web', 'worker', 'beat'} or len(images) != 1 or len(working_dirs) != 1 or len(live_modes) != 1: raise SystemExit('live Gmail deployment state is inconsistent')",
      "if 'enabled' in live_modes and not overlay_present: raise SystemExit('enabled Gmail deployment is missing its activation overlay')",
      "previous_gmail_inbound_mode = next(iter(live_modes))",
      "gmail_inbound_mode = 'enabled' if overlay_present else 'disabled'",
      "print(previous_gmail_inbound_mode, gmail_inbound_mode)",
    ];
    // A cold start has no live container to copy `.env.app` from, so durable
    // deployment evidence supplies it instead: the last release CDK published
    // through /srv/prr/cdk-current, otherwise the operator-managed runtime
    // configuration. Each candidate must resolve inside its own canonical
    // directory and stay private and operator-owned, so a replaced symlink
    // cannot inject a different runtime environment into the release.
    const coldStartRuntimeEnvPython = [
      "import pathlib",
      "import pwd",
      "import re",
      "import stat",
      "candidates = (",
      "    (pathlib.Path('/srv/prr/cdk-current/.env.app'), re.compile(r'/srv/prr/cdk-releases/[0-9a-f]{16,128}/[.]env[.]app')),",
      "    (pathlib.Path('/srv/prr/runtime-config/.env.app'), re.compile(r'/srv/prr/runtime-config/[.]env[.]app')),",
      ")",
      "for candidate, canonical in candidates:",
      "    try: resolved = candidate.resolve(strict=True)",
      "    except OSError: continue",
      "    if canonical.fullmatch(str(resolved)) is None: raise SystemExit('cold start runtime configuration path is not canonical')",
      "    evidence = resolved.lstat()",
      "    if not stat.S_ISREG(evidence.st_mode) or evidence.st_nlink != 1: raise SystemExit('cold start runtime configuration is not a regular file')",
      "    if evidence.st_uid not in {0, pwd.getpwnam('ubuntu').pw_uid} or stat.S_IMODE(evidence.st_mode) & 0o077: raise SystemExit('cold start runtime configuration is not private and operator-owned')",
      "    print(resolved)",
      "    break",
      "else: raise SystemExit('cold start runtime configuration is unavailable')",
    ];
    // The deployment owns exactly two things in the runtime file: whether this
    // start migrates and whether it seeds. It used to also require the Gmail
    // switches to be present and false here, because the only safe way to turn
    // inbound polling on was an activation overlay layered over a disabled
    // base. Those switches are district configuration now (apps.configuration,
    // Application Admin, reason recorded), so the runtime file has no opinion
    // about them and the deployment must not demand one -- a deploy that
    // failed because an operator had tidied a line that no longer does
    // anything would be a deploy blocked by its own obsolete paperwork.
    const directDeploymentRuntimeFilePython = [
      "import pathlib",
      "import sys",
      "path = pathlib.Path(sys.argv[1])",
      "lines = path.read_text(encoding='utf-8').splitlines()",
      "for key in ('RUN_MIGRATIONS', 'AUTO_SEED'):",
      "    replacement = f'{key}=false'",
      "    indexes = [index for index, line in enumerate(lines) if line.startswith(f'{key}=')]",
      "    if indexes:",
      "        lines[indexes[0]] = replacement",
      "        for index in reversed(indexes[1:]): del lines[index]",
      "    else: lines.append(replacement)",
      "path.write_text('\\n'.join(lines) + '\\n', encoding='utf-8')",
    ];
    const directDeploymentSwitchCheckPython = [
      "import os",
      "from django.conf import settings",
      'expected = os.environ["PRR_EXPECT_GMAIL_MODE"]',
      'actual = "enabled" if settings.PRR_GMAIL_ENABLED and settings.PRR_GMAIL_INBOUND_POLL_ENABLED else "disabled" if not settings.PRR_GMAIL_ENABLED and not settings.PRR_GMAIL_INBOUND_POLL_ENABLED else "mixed"',
      'raise SystemExit(0 if actual == expected else "replacement Gmail runtime switches do not match the validated pre-deploy state")',
    ].join("; ");
    const directDeploymentReplayPython = [
      "import datetime as dt",
      "import json",
      "import logging",
      "from django.conf import settings",
      "from django.utils import timezone",
      "from apps.configuration import runtime",
      "from apps.correspondence.gmail_sync import poll_inbound_mailbox, system_inbound_principal",
      "from apps.correspondence.models import GmailSyncState",
      "from apps.records.email_intake import reconcile_stored_unmatched_email_intake",
      "logging.disable(logging.CRITICAL)",
      "actor = system_inbound_principal()",
      "before = reconcile_stored_unmatched_email_intake(actor=actor)",
      "state = GmailSyncState.objects.filter(mailbox=runtime.get(\"PRR_GMAIL_MAILBOX\"), environment=settings.PRR_ENVIRONMENT).first()",
      "now = timezone.now()",
      `fresh = bool(state is not None and state.history_id and state.last_synced_at is not None and now - (dt.timedelta(days=runtime.get("PRR_GMAIL_INBOUND_LOOKBACK_DAYS")) - dt.timedelta(seconds=${gmailActivationTimeoutSeconds})) <= state.last_synced_at <= now + dt.timedelta(minutes=5))`,
      "if not fresh:",
      '    raise SystemExit("gmail_checkpoint_outside_resync_window")',
      "result = poll_inbound_mailbox()",
      "after = reconcile_stored_unmatched_email_intake(actor=actor)",
      'safe = {key: result.get(key) for key in ("status","received","deduplicated","label_failures","resynced") if key in result}',
      'safe["reconciled"] = int(before["converted"]) + int(after["converted"])',
      'safe["remaining_eligible"] = int(after["remaining_eligible"])',
      'print(json.dumps(safe, sort_keys=True, separators=(",", ":")))',
      'raise SystemExit(0 if result.get("status") == "ok" and after["remaining_eligible"] == 0 else 1)',
    ].join("\n");

    // With worker and beat stopped, snapshot the queued Gmail poll task IDs.
    // A replacement beat must enqueue a new task and a replacement worker must
    // complete that exact task before a deployment can succeed.
    const gmailScheduledPollQueuePython = [
      "import json",
      "import re",
      "import uuid",
      "import redis",
      "from django.conf import settings",
      "from config.celery import app",
      "queue = app.conf.task_default_queue",
      "if not isinstance(queue, str) or re.fullmatch(r'[A-Za-z0-9._-]{1,128}', queue) is None: raise SystemExit('Celery queue name is invalid')",
      "client = redis.Redis.from_url(settings.CELERY_BROKER_URL)",
      "if client.llen(queue) > 10000: raise SystemExit('Celery queue is too large for deployment proof')",
      "identifiers = set()",
      "for raw in client.lrange(queue, 0, 9999):",
      "    envelope = json.loads(raw)",
      "    headers = envelope.get('headers') if isinstance(envelope, dict) else None",
      "    if not isinstance(headers, dict) or headers.get('task') != 'apps.correspondence.tasks.poll_inbound_gmail': continue",
      "    identifier = headers.get('id')",
      "    try: parsed = uuid.UUID(identifier)",
      "    except (TypeError, ValueError, AttributeError): raise SystemExit('Queued Gmail poll identifier is invalid')",
      "    if str(parsed) != identifier: raise SystemExit('Queued Gmail poll identifier is not canonical')",
      "    identifiers.add(identifier)",
    ];
    const gmailScheduledPollQueueBaselinePython = [
      ...gmailScheduledPollQueuePython,
      "print(','.join(sorted(identifiers)))",
    ];
    const gmailScheduledPollQueueCandidatePython = [
      "import os",
      ...gmailScheduledPollQueuePython,
      "baseline_text = os.environ.get('PRR_GMAIL_SCHEDULE_QUEUE_BASELINE', '')",
      "baseline = set(filter(None, baseline_text.split(',')))",
      "for identifier in baseline:",
      "    try: parsed = uuid.UUID(identifier)",
      "    except (TypeError, ValueError, AttributeError): raise SystemExit('Queued Gmail poll baseline is invalid')",
      "    if str(parsed) != identifier: raise SystemExit('Queued Gmail poll baseline is not canonical')",
      "new_identifiers = identifiers - baseline",
      "ready = bool(new_identifiers)",
      "print(sorted(new_identifiers)[0] if ready else '')",
      "raise SystemExit(0 if ready else 1)",
    ];
    const gmailScheduledPollBaselinePython = [
      "from django.conf import settings",
      "from apps.configuration import runtime",
      "from apps.correspondence.models import GmailSyncState",
      "state = GmailSyncState.objects.filter(mailbox=runtime.get(\"PRR_GMAIL_MAILBOX\"), environment=settings.PRR_ENVIRONMENT).first()",
      "ready = state is not None and state.last_synced_at is not None",
      'print(state.last_synced_at.isoformat() if ready else "")',
      "raise SystemExit(0 if ready else 1)",
    ].join("; ");
    const gmailScheduledPollVerifierPython = [
      "import datetime as dt",
      "import os",
      "from django.conf import settings",
      "from apps.configuration import runtime",
      "from apps.correspondence.models import GmailSyncState",
      "from config.celery import app",
      'baseline = dt.datetime.fromisoformat(os.environ["PRR_GMAIL_SCHEDULE_BASELINE"])',
      'task_id = os.environ["PRR_GMAIL_SCHEDULE_TASK_ID"]',
      "result = app.AsyncResult(task_id)",
      "payload = result.result if result.successful() else None",
      "state = GmailSyncState.objects.filter(mailbox=runtime.get(\"PRR_GMAIL_MAILBOX\"), environment=settings.PRR_ENVIRONMENT).first()",
      'completed = bool(isinstance(payload, dict) and payload.get("status") == "ok")',
      "advanced = bool(completed and state is not None and state.last_synced_at is not None and state.last_synced_at > baseline)",
      "raise SystemExit(0 if advanced else 1)",
    ].join("; ");
    const publicIntakeCookieValidatorPython = [
      "import pathlib",
      "import sys",
      "path = pathlib.Path(sys.argv[1])",
      "hostname = sys.argv[2]",
      "cookies = {}",
      "for raw_line in path.read_text(encoding='utf-8').splitlines():",
      "    http_only = raw_line.startswith('#HttpOnly_')",
      "    line = raw_line.removeprefix('#HttpOnly_') if http_only else raw_line",
      "    if not line or line.startswith('#'): continue",
      "    fields = line.split('\\t')",
      "    if len(fields) == 7: cookies[fields[5]] = (fields[0], fields[2], fields[3], fields[6], http_only)",
      "csrf = cookies.get('csrftoken')",
      "form = cookies.get('prr_public_intake')",
      "valid_csrf = bool(csrf and csrf[0] == hostname and csrf[1] == '/' and csrf[2] == 'TRUE' and csrf[3] and not csrf[4])",
      "valid_form = bool(form and form[0] == hostname and form[1] == '/api/public/requests/' and form[2] == 'TRUE' and form[3] and form[4])",
      "raise SystemExit(0 if valid_csrf and valid_form else 'public intake bootstrap cookies are invalid')",
    ];
    const publicIntakePageValidatorPython = [
      "import pathlib",
      "import re",
      "import sys",
      "from html.parser import HTMLParser",
      "class ScriptParser(HTMLParser):",
      "    def __init__(self):",
      "        super().__init__()",
      "        self.module_sources = []",
      "    def handle_starttag(self, tag, attrs):",
      "        values = dict(attrs)",
      "        if tag == 'script' and values.get('type') == 'module' and values.get('src'): self.module_sources.append(values['src'])",
      "page = pathlib.Path(sys.argv[1]).read_text(encoding='utf-8')",
      "if 'Frontend build not found' in page: raise SystemExit('public intake frontend fallback is active')",
      "parser = ScriptParser()",
      "parser.feed(page)",
      "if len(parser.module_sources) != 1 or re.fullmatch(r'/static/app/assets/[A-Za-z0-9._-]+[.]js', parser.module_sources[0]) is None: raise SystemExit('public intake frontend entry asset is invalid')",
      "print(parser.module_sources[0])",
    ];

    // CDK owns the application release. A normal `cdk deploy` uploads this
    // source asset and waits for Systems Manager to rebuild and replace the
    // production application containers in the same CDK operation.
    const applicationSource = new s3assets.Asset(this, "ApplicationSource", {
      path: path.resolve(__dirname, "../../.."),
      exclude: [
        ".git",
        ".git/**",
        ".verification",
        ".verification/**",
        ".venv",
        ".venv/**",
        ".env",
        ".env.*",
        "**/.env",
        "**/.env.*",
        "node_modules",
        "node_modules/**",
        "**/node_modules",
        "**/node_modules/**",
        "frontend/node_modules",
        "frontend/node_modules/**",
        "frontend/dist",
        "frontend/dist/**",
        "infra/cdk/cdk.out",
        "infra/cdk/cdk.out/**",
        "**/cdk.out",
        "**/cdk.out/**",
        "**/__pycache__",
        "**/*.pyc",
        "db.sqlite3",
        // Terraform provider binaries dominated this asset (232MB of ~240MB)
        // and terraform.tfvars carries operator-only values that are gitignored
        // for exactly that reason. Neither belongs on the application host.
        ".terraform",
        "**/.terraform",
        "**/.terraform/**",
        "*.tfvars",
        "**/*.tfvars",
        "**/*.tfstate",
        "**/*.tfstate.*",
        // Local agent scratch. These are gitignored but were still packaged,
        // which shipped other branches' source to production and -- because
        // they change constantly -- made identical code produce a new asset
        // hash, and therefore a spurious "new release" and container
        // replacement, on every deploy.
        ".claude",
        ".claude/**",
        ".codex-runs",
        ".codex-runs/**",
        ".psd",
        ".psd/**",
        "outputs",
        "outputs/**",
        // Documentation and generated wiki: never read at runtime.
        "openwiki",
        "openwiki/**",
        "docs/screenshots",
        "docs/screenshots/**",
        "**/.DS_Store",
        // Test files. `bun run build` is the only frontend step the image runs,
        // so none of these reach production. Packaging them also meant a
        // synthesized asset under cdk.out held a second copy of every .test.ts
        // in the repository, which `bun test` then discovered and ran against a
        // stale snapshot -- including of this very file.
        "**/*.test.ts",
        "**/*.test.tsx",
        "infra/cdk/test",
        "infra/cdk/test/**",
        "infra/cdk/rehearsal",
        "infra/cdk/rehearsal/**",
        "frontend/e2e",
        "frontend/e2e/**",
        "frontend/src/test",
        "frontend/src/test/**",
      ],
    });
    applicationSource.grantRead(runtimeRole);

    // Every wildcard in the runtime role's policy, with the evidence
    // psd-dev-standards standards/08 asks for. This is the complete list:
    // cdk-nag reports one finding per distinct wildcard, so an unlisted one
    // fails `cdk synth`, and adding an entry here is a human-review item.
    //
    // Two kinds appear below. The first is `Resource::*` on actions that
    // genuinely have no resource to name - AWS rejects a resource ARN on
    // them. The second is a trailing `/*` inside a single bucket, which is
    // how a prefix is scoped at all; those are narrower than the bucket, not
    // wider.
    // `CfnResource.logicalId` is still a token mid-construction; the stack
    // resolves it. cdk-nag renders an unresolved ARN reference as
    // `<LogicalId.Arn>`, which is what the finding ID below has to contain.
    const logicalIdOf = (bucket: s3.Bucket): string =>
      this.getLogicalId(bucket.node.defaultChild as cdk.CfnElement);
    const runtimeRoleWildcards: readonly { id: string; reason: string }[] = [
      {
        id: "AwsSolutions::AwsSolutions-IAM5[Resource::*]",
        reason:
          "Four grants carry no resource: the Systems Manager node " +
          "registration, message-channel and legacy ec2messages actions, " +
          "sts:GetCallerIdentity (the Google federation identity proof, " +
          "which has no data surface), and cloudwatch:PutMetricData. AWS " +
          "defines none of these with a resource ARN. Each is narrowed by " +
          "what it can do instead: the SSM grants replace the AWS-managed " +
          "policy precisely to drop its account-wide Parameter Store reads, " +
          "document access is pinned to this stack's one diagnostic " +
          "document, and PutMetricData is conditioned on the PSD401/PRR " +
          "namespace.",
      },
      // Derived from the grant itself, never re-typed: a prefix added to
      // readableWritablePrefixes without a matching acknowledgment fails
      // `cdk synth`, and one removed would otherwise leave a stale
      // acknowledgment that cdk-nag never reports as unused.
      ...readableWritablePrefixes.map((prefix) => ({
        id:
          `AwsSolutions::AwsSolutions-IAM5[Resource::<${logicalIdOf(recordsBucket)}` +
          `.Arn>/${prefix}]`,
        reason:
          "A key prefix inside the one records bucket. The wildcard is the " +
          "prefix: object keys are generated per case, so they cannot be " +
          "enumerated in a policy. This is narrower than granting the " +
          "bucket, and the destructive actions reach only the quarantine " +
          "prefixes.",
      })),
      {
        id:
          "AwsSolutions::AwsSolutions-IAM5[Resource::<" +
          `${logicalIdOf(backupsBucket)}.Arn>/app-backups/*]`,
        reason:
          "Write-only append into one prefix of the backups bucket, " +
          "conditioned on if-none-match so an existing backup cannot be " +
          "overwritten. The wildcard is the timestamped object name.",
      },
      {
        id:
          "AwsSolutions::AwsSolutions-IAM5[Resource::arn:<AWS::Partition>:s3:::" +
          `${applicationSource.s3BucketName}/*]`,
        reason:
          "CDK's own asset bucket, read so the host can fetch the release " +
          "archive this stack uploaded. Generated by grantRead on the asset; " +
          "the object key is the asset hash, which is not known until synth.",
      },
      ...["s3:GetObject*", "s3:GetBucket*", "s3:List*"].map((action) => ({
        id: `AwsSolutions::AwsSolutions-IAM5[Action::${action}]`,
        reason:
          "Emitted by aws-cdk-lib's own grantRead on the deployment asset, " +
          "which standards/08 requires us to use instead of hand-written " +
          "JSON. The grant is scoped to the CDK asset bucket above.",
      })),
    ];
    for (const { id, reason } of runtimeRoleWildcards) {
      cdk.Validations.of(runtimeRole).acknowledge({ id, reason });
    }
    // Checkov raises one finding for the whole policy rather than one per
    // wildcard, so its evidence is a pointer to the list above rather than a
    // second write-up of it.
    const runtimeRolePolicy = runtimeRole.node.tryFindChild("DefaultPolicy");
    if (runtimeRolePolicy === undefined) {
      throw new Error(
        "RuntimeRole has no DefaultPolicy to carry its Checkov evidence; " +
          "a grant must have moved or been removed.",
      );
    }
    skipCheckov(runtimeRolePolicy, [
      {
        id: "CKV_AWS_111",
        comment:
          "Every wildcard in this policy is acknowledged individually, with " +
          "its own evidence, for cdk-nag AwsSolutions-IAM5 in the " +
          "runtimeRoleWildcards table in lib/prr-hosting-stack.ts. An " +
          "unlisted wildcard fails `cdk synth`, so this suppression cannot " +
          "outlive that list.",
      },
    ]);

    const applicationVersion = applicationSource.assetHash.slice(0, 40);
    const applicationRelease = `/srv/prr/cdk-releases/${applicationSource.assetHash}`;
    const directDeploymentScript = [
      "#!/bin/bash",
      // errtrace: without it the ERR trap below is not inherited by shell
      // functions, and every mutating command here runs through
      // compose_runtime/compose_mode/compose_base. A failed container
      // replacement would exit without ever rolling back.
      "set -Eeuo pipefail",
      "umask 077",
      'if test "${PRR_CDK_DEPLOYMENT_LOCKED:-0}" != "1"; then',
      '  exec python3 - "$0" <<\'PY\'',
      "import fcntl",
      "import os",
      "import pathlib",
      "import pwd",
      "import stat",
      "import sys",
      'path = pathlib.Path("/srv/prr/deployment.lock")',
      "descriptor = None",
      "try:",
      "    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)",
      "    evidence = os.fstat(descriptor)",
      '    allowed_uids = {0, pwd.getpwnam("ubuntu").pw_uid}',
      "    if path.is_symlink() or not stat.S_ISREG(evidence.st_mode) or evidence.st_uid not in allowed_uids or evidence.st_nlink != 1 or stat.S_IMODE(evidence.st_mode) & 0o077: raise OSError('deployment lock is invalid')",
      "    os.fchmod(descriptor, 0o600)",
      "    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)",
      "    retained = fcntl.fcntl(descriptor, fcntl.F_DUPFD, 10)",
      "    os.close(descriptor)",
      "    descriptor = None",
      "    os.dup2(retained, 8, inheritable=True)",
      "    os.close(retained)",
      "    environment = os.environ.copy()",
      '    environment["PRR_CDK_DEPLOYMENT_LOCKED"] = "1"',
      '    os.execve("/bin/bash", ["/bin/bash", os.path.abspath(sys.argv[1])], environment)',
      "except (BlockingIOError, OSError):",
      '    print("application_deployment_lock_failed", file=sys.stderr)',
      "    raise SystemExit(75)",
      "finally:",
      "    if descriptor is not None:",
      "        try: os.close(descriptor)",
      "        except OSError: pass",
      "PY",
      "fi",
      `release=${applicationRelease}`,
      `version=${applicationVersion}`,
      `asset_bucket=${applicationSource.s3BucketName}`,
      `asset_key=${applicationSource.s3ObjectKey}`,
      'archive="/srv/prr/.cdk-application-${version}.zip"',
      'image="psd401-prr:cdk-${version}"',
      "mkdir -p /srv/prr/cdk-releases",
      'if test ! -d "$release"; then',
      '  stage="${release}.stage"',
      '  rm -rf -- "$stage"',
      '  mkdir "$stage"',
      '  aws s3 cp "s3://${asset_bucket}/${asset_key}" "$archive" --no-progress',
      '  python3 -m zipfile -e "$archive" "$stage"',
      '  mv "$stage" "$release"',
      "fi",
      'test -f "$release/docker-compose.deploy.yml"',
      'test -f "$release/docker-compose.aws.yml"',
      'chmod 755 "$release/docker/entrypoint.sh"',
      'activation_overlay="/srv/prr/.gmail-inbound-activation.compose.yml"',
      'previous_web_id="$(docker ps -q --filter label=com.docker.compose.project=app --filter label=com.docker.compose.service=web)"',
      'previous_worker_id="$(docker ps -q --filter label=com.docker.compose.project=app --filter label=com.docker.compose.service=worker)"',
      'previous_beat_id="$(docker ps -q --filter label=com.docker.compose.project=app --filter label=com.docker.compose.service=beat)"',
      // A hotfix is needed most when production is already down, so a stopped
      // stack has to stay deployable. All three services live is the ordinary
      // rolling replacement. None live is a cold start: there is no previous
      // runtime to read evidence from, stop, or roll back to. A partial runtime
      // is neither, and its live evidence cannot be trusted, so it stays a loud
      // failure rather than becoming a silent half-cold deployment.
      'if test -n "$previous_web_id" && test -n "$previous_worker_id" && test -n "$previous_beat_id"; then',
      "  deployment_start_mode=warm",
      'elif test -z "$previous_web_id" && test -z "$previous_worker_id" && test -z "$previous_beat_id"; then',
      "  deployment_start_mode=cold",
      "else",
      '  echo "application_partial_runtime_detected" >&2',
      "  false",
      "fi",
      "previous_container_ids=()",
      'if test "$deployment_start_mode" = warm; then previous_container_ids=("$previous_web_id" "$previous_worker_id" "$previous_beat_id"); fi',
      'gmail_modes="$(python3 - "$activation_overlay" ${previous_container_ids[@]+"${previous_container_ids[@]}"} <<\'PY\'',
      ...directDeploymentGmailModePython,
      "PY",
      ')"',
      'read -r previous_gmail_inbound_mode gmail_inbound_mode <<< "$gmail_modes"',
      'if test "$deployment_start_mode" = warm; then',
      '  test "$previous_gmail_inbound_mode" = enabled -o "$previous_gmail_inbound_mode" = disabled',
      "else",
      '  test "$previous_gmail_inbound_mode" = absent',
      "fi",
      'test "$gmail_inbound_mode" = enabled -o "$gmail_inbound_mode" = disabled',
      'if test "$deployment_start_mode" = warm; then',
      '  previous_working_dir="$(docker inspect --format \'{{ index .Config.Labels "com.docker.compose.project.working_dir" }}\' "$previous_worker_id")"',
      '  test -f "$previous_working_dir/.env.app"',
      '  runtime_app_env="$previous_working_dir/.env.app"',
      "else",
      "  previous_working_dir=",
      '  runtime_app_env="$(python3 - <<\'PY\'',
      ...coldStartRuntimeEnvPython,
      "PY",
      ')"',
      "fi",
      'test -f "$runtime_app_env"',
      // Stage the runtime environment before replacing the release copy. When a
      // release is redeployed onto itself the source and the destination are the
      // same file, and removing it first would destroy the only copy.
      'runtime_app_env_stage="$(mktemp /srv/prr/.cdk-runtime-env.XXXXXX)"',
      'cat -- "$runtime_app_env" > "$runtime_app_env_stage"',
      'rm -f -- "$release/.env" "$release/.env.app"',
      'ln -s /srv/prr/runtime-config/.env "$release/.env"',
      'mv -- "$runtime_app_env_stage" "$release/.env.app"',
      'chmod 600 "$release/.env.app"',
      'python3 - "$release/.env.app" <<\'PY\'',
      ...directDeploymentRuntimeFilePython,
      "PY",
      'docker build --quiet --build-arg "PRR_RELEASE_SHA=$version" --tag "$image" "$release"',
      'replacement_image_id="$(docker image inspect --format \'{{.Id}}\' "$image")"',
      'test -n "$replacement_image_id"',
      'compose_base() { docker compose --project-directory "$release" --project-name app -f "$release/docker-compose.deploy.yml" -f "$release/docker-compose.aws.yml" "$@"; }',
      'compose_mode() { mode="$1"; shift; if test "$mode" = enabled; then compose_base -f "$activation_overlay" "$@"; else compose_base "$@"; fi; }',
      'compose_runtime() { compose_mode "$gmail_inbound_mode" "$@"; }',
      'compose_previous_base() { docker compose --project-directory "$previous_working_dir" --project-name app -f "$previous_working_dir/docker-compose.deploy.yml" -f "$previous_working_dir/docker-compose.aws.yml" "$@"; }',
      'compose_previous_runtime() { if test "$previous_gmail_inbound_mode" = enabled; then compose_previous_base -f "$activation_overlay" "$@"; else compose_previous_base "$@"; fi; }',
      "verify_runtime_switches() {",
      '  expected_mode="$1"',
      '  expected_files="$release/docker-compose.deploy.yml,$release/docker-compose.aws.yml"',
      '  if test "$expected_mode" = enabled; then expected_files="$expected_files,$activation_overlay"; fi',
      "  for service in web worker beat; do",
      '    service_id="$(compose_mode "$expected_mode" ps -q "$service")"',
      '    test -n "$service_id"',
      '    test "$(docker inspect --format \'{{.Image}}\' "$service_id")" = "$replacement_image_id"',
      '    test "$(docker inspect --format \'{{ index .Config.Labels "com.docker.compose.project.working_dir" }}\' "$service_id")" = "$release"',
      '    test "$(docker inspect --format \'{{ index .Config.Labels "com.docker.compose.project.config_files" }}\' "$service_id")" = "$expected_files"',
      `    docker exec -e PRR_EXPECT_GMAIL_MODE="$expected_mode" "$service_id" python manage.py shell --no-imports -c '${directDeploymentSwitchCheckPython}'`,
      "  done",
      "}",
      'if test "$deployment_start_mode" = warm; then previous_image="$(docker inspect --format \'{{.Image}}\' "$previous_worker_id")"; else previous_image=; fi',
      "verify_previous_runtime() {",
      '  expected_files="$previous_working_dir/docker-compose.deploy.yml,$previous_working_dir/docker-compose.aws.yml"',
      '  if test "$previous_gmail_inbound_mode" = enabled; then expected_files="$expected_files,$activation_overlay"; fi',
      "  for service in web worker beat; do",
      '    service_id="$(compose_previous_runtime ps -q "$service")"',
      '    test -n "$service_id"',
      '    test "$(docker inspect --format \'{{.Image}}\' "$service_id")" = "$previous_image"',
      '    test "$(docker inspect --format \'{{ index .Config.Labels "com.docker.compose.project.working_dir" }}\' "$service_id")" = "$previous_working_dir"',
      '    test "$(docker inspect --format \'{{ index .Config.Labels "com.docker.compose.project.config_files" }}\' "$service_id")" = "$expected_files"',
      `    docker exec -e PRR_EXPECT_GMAIL_MODE="$previous_gmail_inbound_mode" "$service_id" python manage.py shell --no-imports -c '${directDeploymentSwitchCheckPython}'`,
      "  done",
      "}",
      "deployment_mutated=0",
      "rollback() {",
      '  code="${1:-$?}"',
      "  trap - ERR TERM INT HUP",
      // errtrace inherits this trap into every command substitution too, and
      // several of those are expected to fail: the scheduled-poll probe below
      // returns non-zero on each retry until beat enqueues its task. Only the
      // top-level shell may restore anything; a subshell just carries the
      // status out, exactly as errexit alone would have.
      '  if test "$BASHPID" != "$$"; then exit "$code"; fi',
      '  if test "$deployment_mutated" -eq 1; then',
      '    if test "$deployment_start_mode" = warm; then',
      // `$previous_image` is a bare sha256 image ID, and Compose treats a
      // bare digest as something to PULL: it asks a registry for a repository
      // literally named "sha256" and fails with "pull access denied". The
      // rollback therefore could never restore anything -- it died on its
      // first `up`, leaving the failed deployment in place. Give the image a
      // local tag first so Compose resolves it locally instead.
      '      rollback_tag="psd401-prr:rollback-${version}"',
      '      docker tag "$previous_image" "$rollback_tag" || true',
      '      export PRR_RELEASE_IMAGE="$rollback_tag"',
      "      compose_previous_runtime up -d --no-deps --force-recreate web worker beat caddy || true",
      "      verify_previous_runtime || true",
      "    else",
      // A cold start has no previous image or previous runtime to restore:
      // production was already down before this deployment touched anything.
      // Fail loudly and leave the staged release, the unchanged
      // /srv/prr/cdk-current pointer, and the container state in place so the
      // next attempt starts from the same evidence rather than from a
      // half-restored runtime invented here.
      '      echo "application_cold_start_rollback_unavailable" >&2',
      "    fi",
      "  fi",
      '  exit "$code"',
      "}",
      "trap rollback ERR",
      "trap 'rollback 143' TERM INT HUP",
      'install -o root -g root -m 755 "$release/infra/aws/run-app-backup.sh" /usr/local/sbin/prr-run-app-backup',
      // Root-volume usage reporting, refreshed with every release so the unit
      // files and the alarm cannot drift apart.
      'install -o root -g root -m 755 "$release/infra/aws/report-disk-usage.sh" /usr/local/sbin/prr-report-disk-usage',
      // The safe compose entrypoint. Gmail's enabled state is the presence of
      // an overlay file that plain `docker compose` applies only when a caller
      // remembers -f, so an ordinary recreate silently turns off inbound
      // requester mail. This wrapper always resolves the overlay, the release,
      // and the running image, which makes the correct invocation the
      // convenient one instead of the one you have to remember.
      'install -o root -g root -m 755 "$release/infra/aws/prr-compose.sh" /usr/local/sbin/prr-compose',
      'install -o root -g root -m 644 "$release/infra/aws/prr-disk-usage@.service" /etc/systemd/system/prr-disk-usage@.service',
      'install -o root -g root -m 644 "$release/infra/aws/prr-disk-usage@.timer" /etc/systemd/system/prr-disk-usage@.timer',
      "systemctl daemon-reload",
      `systemctl enable --now prr-disk-usage@${config.envName}.timer`,
      "run_application_backup() {",
      '  if ! /usr/bin/timeout --signal=TERM --kill-after=15s 600s systemctl --wait start prr-app-backup@prod.service; then systemctl stop prr-app-backup@prod.service >/dev/null 2>&1 || true; echo "application_backup_unavailable" >&2; return 1; fi',
      "}",
      "await_container_health() {",
      '  await_mode="$1"',
      '  await_service="$2"',
      '  await_accept="$3"',
      '  await_failure="$4"',
      "  for attempt in $(seq 1 60); do",
      '    await_id="$(compose_mode "$await_mode" ps -q "$await_service")"',
      '    if test -n "$await_id"; then',
      "      health=\"$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \"$await_id\")\"",
      '      case " $await_accept " in *" $health "*) return 0 ;; esac',
      "    fi",
      '    if test "$attempt" -eq 60; then echo "$await_failure" >&2; return 1; fi',
      "    sleep 5",
      "  done",
      "}",
      'if test "$deployment_start_mode" = warm; then run_application_backup; fi',
      'export PRR_RELEASE_IMAGE="$image"',
      "export PRR_RUN_MIGRATIONS=false",
      "export PRR_AUTO_SEED=false",
      "deployment_mutated=1",
      // A cold start has no running application for the backup gate to dump, and
      // that gate is not optional on a statutory-compliance system. Bring the
      // dependencies up without recreating any that survived, then start the
      // replacement services in the disabled Gmail mode so no inbound polling or
      // scheduled work runs against the pre-migration schema. The backup that
      // follows is still taken before `manage.py migrate`, the only state
      // mutation in this script, so it remains a true pre-deployment restore
      // point. The tail below then recreates web, worker, and beat in the real
      // Gmail mode exactly as it does for a rolling replacement.
      'if test "$deployment_start_mode" = cold; then',
      "  compose_mode disabled up -d --no-deps --no-recreate db redis clamav",
      "  await_container_health disabled db healthy cold_start_dependency_unavailable",
      "  await_container_health disabled redis healthy cold_start_dependency_unavailable",
      "  await_container_health disabled clamav healthy cold_start_dependency_unavailable",
      "  compose_mode disabled up -d --no-deps --force-recreate web worker beat",
      '  await_container_health disabled web "healthy running" cold_start_web_unavailable',
      "  run_application_backup",
      "fi",
      // Run a management command in the release's migrate service, with
      // first-start behaviour off.
      'release_manage() { compose_runtime run --rm --no-deps --entrypoint /usr/bin/env -e RUN_MIGRATIONS=false -e AUTO_SEED=false migrate python manage.py "$@"; }',
      "release_manage migrate --noinput",
      // Write the currently deployed value of each application-managed
      // switch into the settings store, once, so the first release after
      // those switches moved changes nothing about how production behaves.
      // It runs here rather than in the container entrypoint because this
      // deployment replaces the entrypoint to migrate, so the entrypoint's
      // own import step never executes in production. It runs in the worker
      // service, under compose_runtime, because that is the environment the
      // poller actually runs with: the Gmail activation overlay sets the
      // Gmail switches for web, worker, and beat only, and Compose merges by
      // service name, so the migrate service never sees them. Importing
      // there would store Gmail off on a host where inbound polling is on --
      // for good, since the import never overwrites a stored row. It is
      // idempotent, so every later deployment is a no-op here, and it
      // precedes the replacement services so they start with the store
      // already authoritative.
      "compose_runtime run --rm --no-deps --entrypoint /usr/bin/env -e RUN_MIGRATIONS=false -e AUTO_SEED=false worker python manage.py import_environment_settings",
      // Every stored settings secret must open under the release's key. A
      // wrong key does not crash anything -- stored secrets just read as unset
      // and Google sign-in quietly stops -- so this fails the deploy, and the
      // rollback trap restores the previous release, before the replacement
      // web container takes traffic. Prints counts and key names only.
      "release_manage verify_settings_secrets",
      "compose_runtime up -d --no-deps --force-recreate web",
      'web_id="$(compose_runtime ps -q web)"',
      'test -n "$web_id"',
      "for attempt in $(seq 1 60); do",
      "  health=\"$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \"$web_id\")\"",
      '  if test "$health" = healthy -o "$health" = running; then break; fi',
      '  if test "$attempt" -eq 60; then echo "replacement_web_unavailable" >&2; false; fi',
      "  sleep 5",
      "done",
      'if test "$deployment_start_mode" = warm; then',
      '  docker stop --signal=TERM -t 60 "$previous_beat_id" >/dev/null',
      '  /usr/bin/timeout --signal=TERM --kill-after=15s 300s docker stop --signal=TERM -t -1 "$previous_worker_id" >/dev/null',
      "fi",
      // Whether inbound polling is on is decided in the settings store now,
      // not by the activation overlay. Gate the continuity proof on the
      // store: gating on the overlay file would skip the proof for a
      // mailbox an administrator enabled in the application, and would run
      // it -- and fail the deploy -- for one they turned off while the file
      // is still on disk. An unreadable store fails the deploy rather than
      // reading as "off" and silently skipping the proof.
      "gmail_store_mode=\"$(docker exec \"$web_id\" python manage.py shell --no-imports -c 'from apps.configuration import runtime; print(\"enabled\" if runtime.get(\"PRR_GMAIL_ENABLED\", raise_on_store_error=True) and runtime.get(\"PRR_GMAIL_INBOUND_POLL_ENABLED\", raise_on_store_error=True) else \"disabled\")')\"",
      'test "$gmail_store_mode" = enabled -o "$gmail_store_mode" = disabled',
      'if test "$gmail_store_mode" = enabled; then',
      '  docker exec "$web_id" python manage.py verify_gmail',
      "  replay_status=0",
      `  replay_output="$(docker exec "$web_id" /usr/bin/timeout --signal=TERM --kill-after=15s 300s python manage.py shell --no-imports -c '${directDeploymentReplayPython}' 2>/dev/null)" || replay_status=$?`,
      '  if test "$replay_status" -ne 0; then echo "gmail_post_deploy_replay_failed" >&2; false; fi',
      '  printf "%s\\n" "$replay_output"',
      '  scheduled_poll_queue_baseline="$(docker exec -i "$web_id" python manage.py shell --no-imports <<\'PY\'',
      ...gmailScheduledPollQueueBaselinePython,
      "PY",
      ')"',
      "  find_new_scheduled_poll() {",
      '    docker exec -i -e PRR_GMAIL_SCHEDULE_QUEUE_BASELINE="$scheduled_poll_queue_baseline" "$web_id" python manage.py shell --no-imports <<\'PY\'',
      ...gmailScheduledPollQueueCandidatePython,
      "PY",
      "  }",
      "  compose_runtime up -d --no-deps --force-recreate beat",
      "  scheduled_poll_task_id=",
      "  for attempt in $(seq 1 90); do",
      '    if scheduled_poll_task_id="$(find_new_scheduled_poll 2>/dev/null)" && test -n "$scheduled_poll_task_id"; then break; fi',
      "    sleep 2",
      "  done",
      '  if test -z "$scheduled_poll_task_id"; then echo "gmail_post_deploy_poll_unavailable" >&2; false; fi',
      '  printf "%s\\n" "$scheduled_poll_task_id" | grep -Eq \'^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$\'',
      `  scheduled_poll_baseline="$(docker exec "$web_id" python manage.py shell --no-imports -c '${gmailScheduledPollBaselinePython}')"`,
      '  printf "%s\\n" "$scheduled_poll_baseline" | grep -Eq \'^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]{1,6})?\\+00:00$\'',
      "  compose_runtime up -d --no-deps --force-recreate worker caddy",
      '  verify_runtime_switches "$gmail_inbound_mode"',
      "  gmail_post_deploy_poll_ready=0",
      "  for attempt in $(seq 1 60); do",
      `    if docker exec -e PRR_GMAIL_SCHEDULE_BASELINE="$scheduled_poll_baseline" -e PRR_GMAIL_SCHEDULE_TASK_ID="$scheduled_poll_task_id" "$web_id" python manage.py shell --no-imports -c '${gmailScheduledPollVerifierPython}' >/dev/null 2>&1; then gmail_post_deploy_poll_ready=1; break; fi`,
      "    sleep 5",
      "  done",
      '  if test "$gmail_post_deploy_poll_ready" -ne 1; then echo "gmail_post_deploy_poll_unavailable" >&2; false; fi',
      "else",
      "  compose_runtime up -d --no-deps --force-recreate worker beat caddy",
      '  verify_runtime_switches "$gmail_inbound_mode"',
      "fi",
      'public_intake_cookie_jar="$(mktemp /tmp/prr-public-intake-cookie.XXXXXX)"',
      'public_intake_page="$(mktemp /tmp/prr-public-intake-page.XXXXXX)"',
      'cleanup_public_intake_probe() { rm -f -- "$public_intake_cookie_jar" "$public_intake_page"; }',
      "trap cleanup_public_intake_probe EXIT",
      "public_intake_ready=0",
      "for attempt in $(seq 1 6); do",
      `  if curl --proto '=https' --connect-timeout 10 --max-time 30 --fail --silent --show-error --output "$public_intake_page" --cookie-jar "$public_intake_cookie_jar" "https://${config.hostname}/request"; then public_intake_ready=1; break; fi`,
      "  sleep 5",
      "done",
      'if test "$public_intake_ready" -ne 1; then echo "public_intake_bootstrap_unavailable" >&2; false; fi',
      'if ! python3 - "$public_intake_cookie_jar" "' + config.hostname + '" <<\'PY\'',
      ...publicIntakeCookieValidatorPython,
      "PY",
      "then echo \"public_intake_bootstrap_unavailable\" >&2; false; fi",
      'if ! public_intake_asset="$(python3 - "$public_intake_page" <<\'PY\'',
      ...publicIntakePageValidatorPython,
      "PY",
      ')"; then echo "public_intake_asset_unavailable" >&2; false; fi',
      `if ! curl --proto '=https' --connect-timeout 10 --max-time 30 --fail --silent --show-error --output /dev/null "https://${config.hostname}$public_intake_asset"; then echo "public_intake_asset_unavailable" >&2; false; fi`,
      "cleanup_public_intake_probe",
      "trap - EXIT",
      "trap - ERR TERM INT HUP",
      'ln -sfn "$release" /srv/prr/cdk-current',
      'printf "%s\\n" "$version" > /srv/prr/cdk-current-version',
      'rm -f -- "$archive"',
      // Reclaim the images this deploy just superseded. Every deploy builds a
      // new image and nothing else ever removed the old ones: 34 accumulated
      // on the 48G root volume and filled it to 97%, at which point the SSM
      // document worker could no longer write its own stdout and *every*
      // subsequent deployment failed to stabilize -- an outage in waiting that
      // looked like a CloudFormation problem.
      //
      // Placed here on purpose: after the new containers are verified healthy
      // and after the ERR trap is disarmed, and made non-fatal. Housekeeping
      // must never be able to roll back a deployment that already succeeded.
      //
      // `docker image prune -a` is NOT used, because it judges an image solely
      // by whether a container references it -- and the rollback target has no
      // container. Using it deleted the image `deployed-release.json` pins,
      // leaving the stack with a rollback record pointing at nothing.
      // Reclaiming disk must never cost the ability to go back.
      "prr_pinned_images() {",
      "  python3 - <<'PY'",
      "import json",
      "import pathlib",
      "path = pathlib.Path('/srv/prr/deployed-release.json')",
      "pinned = set()",
      "if path.exists():",
      "    try: record = json.loads(path.read_text(encoding='utf-8'))",
      "    except ValueError: record = {}",
      "    if isinstance(record, dict):",
      "        # The live release and every retained rollback target.",
      "        for scope in (record, record.get('rollback')):",
      "            if isinstance(scope, dict):",
      "                value = scope.get('image_id')",
      "                if isinstance(value, str) and value.startswith('sha256:'):",
      "                    pinned.add(value)",
      "print(chr(10).join(sorted(pinned)))",
      "PY",
      "}",
      // Redirection lives at the call site so the heredoc line ends where the
      // deployment-script syntax check expects it to.
      'prr_pinned="$(prr_pinned_images 2>/dev/null || true)"',
      "for prr_image in $(docker images -q --no-trunc | sort -u); do",
      '  case "$prr_pinned" in *"$prr_image"*) continue ;; esac',
      // Plain `rmi`, never `-f`: Docker already refuses to remove an image any
      // container references, so the running services protect themselves and
      // anything still in use is simply skipped.
      '  docker rmi "$prr_image" >/dev/null 2>&1 || true',
      "done",
      // Build cache holds no provenance, so it can go wholesale.
      "docker builder prune -f >/dev/null 2>&1 || true",
      // Reclaim the release directories this deploy superseded. Every deploy
      // unpacks a full copy of the source asset into
      // /srv/prr/cdk-releases/<hash>/ and nothing ever removed the old ones:
      // 78 had accumulated on the same 48G root volume the image prune above
      // exists to protect. Each also carries its own `.env.app`, which is how
      // one signing key ended up in 77 copies and had to be removed from all
      // of them by hand.
      //
      // Placed with the image prune on purpose, for the same reasons: after
      // the new containers are verified healthy, after the ERR trap is
      // disarmed, and non-fatal.
      //
      // What survives is everything that is or could become live. Python
      // decides, because deciding safely means reading a JSON record,
      // resolving a symlink, and rejecting a name that is not a release hash.
      "prr_prunable_releases() {",
      "  python3 - \"$release\" <<'PY'",
      "import json",
      "import os",
      "import pathlib",
      "import re",
      "import stat",
      "import subprocess",
      "import sys",
      "releases = pathlib.Path('/srv/prr/cdk-releases')",
      "# A margin, so a release nothing happens to pin is still on the host to",
      "# look at after a bad deploy.",
      "keep_recent = 3",
      "name_pattern = re.compile(r'^[0-9a-f]{16,128}$')",
      "# A path anywhere inside a release directory pins that release, so a",
      "# reference to one file in it counts as much as one to the directory.",
      "path_pattern = re.compile(r'^/srv/prr/cdk-releases/([0-9a-f]{16,128})(?:/.*)?$')",
      "pinned = set()",
      "def pin(value):",
      "    if not isinstance(value, str): return",
      "    match = path_pattern.match(value)",
      "    if match: pinned.add(match.group(1))",
      "    elif name_pattern.match(value): pinned.add(value)",
      "# The release this deployment just made live.",
      "pin(sys.argv[1])",
      "# And the live release as the host itself resolves it: the symlink",
      "# prr-compose follows. Reading it rather than trusting the argument keeps",
      "# whatever is actually serving out of the prune.",
      "pin(os.path.realpath('/srv/prr/cdk-current'))",
      "# Every release the deployment record names -- the live one and the",
      "# rollback target. The whole record is walked rather than known keys:",
      "# its schema is owned outside this script, and keeping one directory too",
      "# many costs disk while keeping one too few costs the ability to go back.",
      "# Today nothing in a CDK deploy writes this file; it is read if present.",
      "record_path = pathlib.Path('/srv/prr/deployed-release.json')",
      "record = {}",
      "if record_path.exists():",
      "    # A record that is present but unreadable or corrupt is not evidence",
      "    # that it pins nothing, so it refuses to decide rather than dropping",
      "    # the rollback pin it cannot read.",
      "    try: record = json.loads(record_path.read_text(encoding='utf-8'))",
      "    except (OSError, ValueError): raise SystemExit(69)",
      "def pin_tree(node, depth=0):",
      "    if depth > 8: return",
      "    if isinstance(node, dict):",
      "        for value in node.values(): pin_tree(value, depth + 1)",
      "    elif isinstance(node, list):",
      "        for value in node: pin_tree(value, depth + 1)",
      "    else: pin(node)",
      "pin_tree(record)",
      "# Every container, and for each one its",
      "# com.docker.compose.project.working_dir label -- where that project's",
      "# compose files and .env.app live, which this script's own rollback path",
      "# reads to restore the previous runtime -- plus every bind-mount source,",
      "# which also catches a container someone started by hand against an old",
      "# release to look at it. `--all` so that a container which is merely",
      "# stopped keeps the directory it would need on its next start.",
      // One path per line, and the separator is the template's own `println`
      // rather than a `\n` escape on purpose: this literal is Python source,
      // so Python would turn the escape into a real line feed and hand Go a
      // quoted string with a newline in it, which its template lexer rejects
      // ("unterminated quoted string"). `docker inspect` then exits non-zero,
      // the enumeration below raises, and the prune refuses to run at all --
      // the silent non-prune this whole step exists to prevent.
      "container_format = '{{ index .Config.Labels \"com.docker.compose.project.working_dir\" }}{{ println }}{{ range .Mounts }}{{ println .Source }}{{ end }}'",
      "def docker(*argv):",
      "    return subprocess.run(('docker',) + argv, capture_output=True, text=True, timeout=120, check=True).stdout",
      "# Failing to read any of this is not evidence that nothing holds a",
      "# release, so it prunes nothing at all -- and exits non-zero so the",
      "# deployment output says so. Silence is how the disk filled the first",
      "# time; a prune that quietly stops running would fill it again.",
      "try:",
      "    for container in docker('ps', '--all', '--quiet', '--no-trunc').split():",
      "        for line in docker('inspect', '--format', container_format, container).splitlines(): pin(line.strip())",
      "except (OSError, subprocess.SubprocessError):",
      "    raise SystemExit(69)",
      "candidates = []",
      "try: entries = sorted(releases.iterdir())",
      "except OSError: raise SystemExit(69)",
      "for entry in entries:",
      "    # Anything that is not a release-hash directory is left alone: a",
      "    # half-unpacked `<hash>.stage`, a symlink, an operator's own file.",
      "    if not name_pattern.match(entry.name): continue",
      "    try: evidence = entry.lstat()",
      "    except OSError: continue",
      "    if not stat.S_ISDIR(evidence.st_mode): continue",
      "    candidates.append((evidence.st_mtime, entry.name))",
      "for _, name in sorted(candidates, reverse=True)[:keep_recent]: pinned.add(name)",
      "print(chr(10).join(sorted(name for _, name in candidates if name not in pinned)))",
      "PY",
      "}",
      // As with the image prune above, the redirection lives at the call site
      // so the heredoc ends where the deployment-script syntax check expects.
      // A non-zero exit means the selector refused to decide, which is
      // reported rather than treated as "nothing to prune".
      'if prr_prunable="$(prr_prunable_releases 2>/dev/null)"; then',
      "  for prr_release_name in $prr_prunable; do",
      // Python already validated every name, and this checks again: with no
      // hex-only name able to hold a slash, a dot, a glob or whitespace, the
      // path below can only ever be one directory directly under the releases
      // root. Deleting a tree is the one step here that cannot be undone.
      '    case "$prr_release_name" in *[!0-9a-f]*|"") continue ;; esac',
      '    rm -rf -- "/srv/prr/cdk-releases/$prr_release_name" || true',
      "  done",
      "else",
      '  echo "release prune skipped: the release inventory could not be read"',
      "fi",
      // Recorded in the deployment output so the trend is visible before the
      // volume is full again rather than after.
      'echo "release directories after prune: $(find /srv/prr/cdk-releases -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d \' \')"',
      'echo "root filesystem after prune: $(df -h / | awk \'NR==2 {print $4\" free (\"$5\" used)\"}\')"',
      'echo "application containers deployed by CDK"',
    ].join("\n");

    const applicationDeployment = new ssm.CfnAssociation(
      this,
      "ApplicationDeployment",
      {
        associationName: `psd401-prr-${config.envName}-application`,
        name: "AWS-RunShellScript",
        parameters: {
          commands: [directDeploymentScript],
          executionTimeout: ["3300"],
        },
        targets: [{ key: "InstanceIds", values: [instanceId] }],
        waitForSuccessTimeoutSeconds: 3600,
      },
    );
    applicationDeployment.node.addDependency(applicationSource);

    // Keep this diagnostic compatible with the image already running in
    // production. The Gmail switches are read from the settings store when the
    // running image has one -- once a switch has a stored value the
    // environment attribute is no longer what the application consults -- and
    // from Django settings only on an image that predates the store. It emits
    // no mailbox, history, message, or requester identifiers.
    const gmailCheckpointPython = [
      "import importlib, importlib.util, json",
      "from django.conf import settings",
      "from django.utils import timezone",
      "from apps.correspondence.models import GmailSyncState",
      'runtime = importlib.import_module("apps.configuration.runtime") if importlib.util.find_spec("apps.configuration") is not None else None',
      'gmail_enabled = bool(runtime.get("PRR_GMAIL_ENABLED") if runtime is not None else settings.PRR_GMAIL_ENABLED)',
      'inbound_enabled = bool(runtime.get("PRR_GMAIL_INBOUND_POLL_ENABLED") if runtime is not None else settings.PRR_GMAIL_INBOUND_POLL_ENABLED)',
      'mailbox = runtime.get("PRR_GMAIL_MAILBOX") if runtime is not None else getattr(settings, "PRR_GMAIL_MAILBOX", "")',
      'stale_after_seconds = int(runtime.get("PRR_GMAIL_INBOUND_STALE_SECONDS") if runtime is not None else getattr(settings, "PRR_GMAIL_INBOUND_STALE_SECONDS", 300))',
      "state = GmailSyncState.objects.filter(mailbox=mailbox, environment=settings.PRR_ENVIRONMENT).first()",
      "last_synced_at = state.last_synced_at if state is not None else None",
      "age_seconds = max(0, int((timezone.now() - last_synced_at).total_seconds())) if last_synced_at is not None else None",
      'status = "disabled" if not inbound_enabled else "gmail_disabled" if not gmail_enabled else "never_synced" if last_synced_at is None else "stale" if age_seconds > stale_after_seconds else "healthy"',
      'healthy = status == "healthy"',
      'print(json.dumps({"status": status, "healthy": healthy, "configuration": {"environment": settings.PRR_ENVIRONMENT, "gmail_enabled": gmail_enabled, "inbound_poll_enabled": inbound_enabled, "mailbox_configured": bool(mailbox)}, "checkpoint": {"present": state is not None, "history_present": bool(state is not None and state.history_id), "last_synced_at": last_synced_at.isoformat() if last_synced_at is not None else None, "age_seconds": age_seconds, "stale_after_seconds": stale_after_seconds}}, sort_keys=True, separators=(",", ":")))',
      "raise SystemExit(0 if healthy else 1)",
    ].join("; ");

    // This is intentionally parameter-free. Operators can retrieve the fixed,
    // requester-data-free state needed for an inbound-mail incident through AWS APIs,
    // without opening a shell or accepting caller-supplied host commands.
    const emailDiagnostics = new ssm.CfnDocument(this, "EmailDiagnostics", {
      documentType: "Command",
      name: emailDiagnosticsDocumentName,
      targetType: "/AWS::EC2::Instance",
      updateMethod: "NewVersion",
      content: {
        schemaVersion: "2.2",
        description:
          "Read-only PRR email worker, poller, and Gmail credential diagnostics without requester data.",
        mainSteps: [
          {
            action: "aws:runShellScript",
            name: "emailDiagnostics",
            inputs: {
              runCommand: [
                "set -eu",
                "cd /srv/prr/app",
                "export PRR_RELEASE_IMAGE=prr-diagnostics-placeholder",
                "diagnostic_status=0",
                "docker compose ps --format json web worker beat || diagnostic_status=1",
                'running_services="$(docker compose ps --services --filter status=running web worker beat | sort)" || diagnostic_status=1',
                'expected_services="$(printf \'beat\\nweb\\nworker\\n\')"',
                'test "$running_services" = "$expected_services" || diagnostic_status=1',
                `docker compose exec -T web python manage.py shell -c '${gmailCheckpointPython}' || diagnostic_status=1`,
                "docker compose exec -T web python manage.py verify_gmail || diagnostic_status=1",
                'exit "$diagnostic_status"',
              ],
            },
          },
        ],
      },
    });















    // Prod's address is load-bearing: public DNS and the district's internal
    // DNS both point at it and were communicated as final. The stack
    // associates the existing allocation and never owns it, so no destroy can
    // release the IP. Dev has no such history and allocates its own.
    let allocationId = config.eipAllocationId;
    if (!allocationId) {
      const elasticIp = new ec2.CfnEIP(this, "ElasticIp", {
        domain: "vpc",
        tags: [{ key: "Name", value: `psd401-prr-${config.envName}` }],
      });
      allocationId = elasticIp.attrAllocationId;
    }
    const elasticIpAssociation = new ec2.CfnEIPAssociation(
      this,
      "ElasticIpAssociation",
      { allocationId, instanceId: instance.instanceId },
    );
    // Handover step 2 (psd401/psd-aws-infra#42) drops the instance and this
    // association from the stack and imports them back, because changing the
    // instance's SubnetId in place replaces it. Retain keeps the host and its
    // public address when they leave the stack.
    for (const resource of [
      instance.node.defaultChild as cdk.CfnResource,
      elasticIpAssociation,
    ]) {
      resource.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);
    }
    if (handover === "detached") {
      // The instance construct stays for its profile, which the host keeps.
      instance.node.tryRemoveChild("Resource");
      this.node.tryRemoveChild("ElasticIpAssociation");
    }

    const snapshotRole = new iam.Role(this, "SnapshotPolicyRole", {
      assumedBy: new iam.ServicePrincipal("dlm.amazonaws.com"),
      description: `EBS snapshot lifecycle, psd401-prr ${config.envName}`,
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          "service-role/AWSDataLifecycleManagerServiceRole",
        ),
      ],
    });
    // The one AWS managed policy in either stack. Data Lifecycle Manager
    // defines this service-role policy and changes it when the service
    // changes; a hand-written copy would drift silently and break snapshots.
    cdk.Validations.of(snapshotRole).acknowledge({
      id:
        "AwsSolutions::AwsSolutions-IAM4[Policy::arn:<AWS::Partition>:iam::" +
        "aws:policy/service-role/AWSDataLifecycleManagerServiceRole]",
      reason:
        "AWS's own service-role policy for Data Lifecycle Manager, assumed " +
        "only by dlm.amazonaws.com and used only by this stack's snapshot " +
        "policy. Copying it by hand would drift from the service.",
    });

    // Disaster recovery only. The compound target tag is unique to this
    // stack so the policy cannot pick up another team's instances.
    new dlm.CfnLifecyclePolicy(this, "SnapshotPolicy", {
      description: `psd401-prr ${config.envName} daily EBS snapshots - 14 day retention - disaster recovery only not records retention`,
      state: "ENABLED",
      executionRoleArn: snapshotRole.roleArn,
      policyDetails: {
        policyType: "EBS_SNAPSHOT_MANAGEMENT",
        resourceTypes: ["INSTANCE"],
        targetTags: [{ key: "psd401-prr-backup", value: config.envName }],
        parameters: { excludeBootVolume: false },
        schedules: [
          {
            name: `psd401-prr-${config.envName}-daily`,
            copyTags: true,
            createRule: {
              interval: 24,
              intervalUnit: "HOURS",
              times: ["09:00"],
            },
            retainRule: { count: 14 },
          },
        ],
      },
    });

    const appBackupMetric = new cloudwatch.Metric({
      namespace: "PSD401/PRR",
      metricName: "AppBackupSuccess",
      dimensionsMap: { Environment: config.envName },
      period: cdk.Duration.days(1),
      statistic: "Minimum",
    });
    const appBackupAlarm = new cloudwatch.Alarm(this, "AppBackupAlarm", {
      alarmName: `psd401-prr-${config.envName}-app-backup-missing-or-failed`,
      alarmDescription:
        "Daily PostgreSQL/audit backup failed or stopped reporting. Restore readiness is at risk.",
      metric: appBackupMetric,
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.BREACHING,
    });

    // A filling root volume used to be invisible: no CloudWatch agent, no disk
    // metric, no alarm. It surfaced only when the SSM document worker ran out
    // of room to write its own stdout and deployments began failing to
    // stabilize -- an error naming neither the disk nor the cause. 80% leaves
    // roughly 9G of headroom on the 48G volume, which is several releases and
    // ample time to act.
    const rootDiskMetric = new cloudwatch.Metric({
      namespace: "PSD401/PRR",
      metricName: "RootDiskUsedPercent",
      dimensionsMap: { Environment: config.envName },
      period: cdk.Duration.minutes(5),
      statistic: "Maximum",
    });
    const rootDiskAlarm = new cloudwatch.Alarm(this, "RootDiskUsageAlarm", {
      alarmName: `psd401-prr-${config.envName}-root-disk-filling`,
      alarmDescription:
        "Root volume is filling. A full disk stops deployments, PostgreSQL "
        + "writes, and the append-only audit trail. Reclaim space now.",
      metric: rootDiskMetric,
      threshold: 80,
      evaluationPeriods: 2,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      // Missing data means the reporter stopped, which is itself a fault worth
      // surfacing: a silent monitor is indistinguishable from a healthy one.
      treatMissingData: cloudwatch.TreatMissingData.BREACHING,
    });

    const gmailInboundPollMetric = new cloudwatch.Metric({
      namespace: "PSD401/PRR",
      metricName: "GmailInboundPollHealth",
      dimensionsMap: { Environment: "production" },
      period: cdk.Duration.minutes(1),
      statistic: "Minimum",
    });
    const gmailInboundPollAlarm = new cloudwatch.Alarm(
      this,
      "GmailInboundPollAlarm",
      {
        alarmName:
          `psd401-prr-${config.envName}-gmail-inbound-missing-or-failed`,
        alarmDescription:
          "Inbound Gmail polling failed or stopped reporting. Public-records "
          + "email may not be entering casework. The most common cause is a "
          + "compose invocation made without the Gmail activation overlay, "
          + "which silently disables polling: use prr-compose rather than "
          + "docker compose, and confirm "
          + "/srv/prr/.gmail-inbound-activation.compose.yml is still applied.",
        metric: gmailInboundPollMetric,
        threshold: 1,
        evaluationPeriods: 2,
        datapointsToAlarm: 2,
        comparisonOperator:
          cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
        treatMissingData: cloudwatch.TreatMissingData.BREACHING,
      },
    );

    // The alarms above used to change state with no one told. One topic emails
    // the configured recipients on every transition into ALARM and back to OK,
    // so a recovery is as visible as the fault. No KMS key: CloudWatch cannot
    // publish to a topic encrypted with the AWS-managed SNS key, and the
    // messages carry only alarm names and metric values.
    const alarmTopic = new sns.Topic(this, "AlarmTopic", {
      topicName: `psd401-prr-${config.envName}-alarms`,
      displayName: `psd401-prr ${config.envName} alarms`,
      enforceSSL: true,
    });
    for (const email of config.alarmEmails) {
      alarmTopic.addSubscription(new snsSubscriptions.EmailSubscription(email));
    }
    skipCheckov(alarmTopic, [
      {
        id: "CKV_AWS_26",
        comment:
          "Alarm notifications carry a metric name, threshold and state - " +
          "no record, requester or student data. Encrypting with the " +
          "AWS-managed SNS key would stop CloudWatch publishing to the " +
          "topic, because that key's policy cannot be edited to grant the " +
          "alarm service a data key, and a silent alarm is the one failure " +
          "an alarm topic must not have. A customer-managed key would work, " +
          "at a monthly charge and another key policy to maintain.",
      },
    ]);
    const alarmAction = new cloudwatchActions.SnsAction(alarmTopic);
    for (const alarm of [appBackupAlarm, rootDiskAlarm, gmailInboundPollAlarm]) {
      alarm.addAlarmAction(alarmAction);
      alarm.addOkAction(alarmAction);
    }

    new cdk.CfnOutput(this, "InstanceId", { value: instanceId });
    new cdk.CfnOutput(this, "SubnetId", { value: subnet.subnetId });
    new cdk.CfnOutput(this, "SecurityGroupId", {
      value: securityGroup.securityGroupId,
    });
    new cdk.CfnOutput(this, "PublicAddress", {
      value: allocationId,
      description: "Elastic IP allocation serving this environment",
    });
    new cdk.CfnOutput(this, "RuntimeRoleName", {
      value: runtimeRole.roleName,
      description:
        "role_name for this environment in the Terraform aws_runtimes map",
    });
    new cdk.CfnOutput(this, "RuntimeRoleArn", { value: runtimeRole.roleArn });
    new cdk.CfnOutput(this, "RuntimeSecretArn", {
      value: runtimeSecret.ref,
      description: "Seed with the live DJANGO_SECRET_KEY and PRR_SETTINGS_ENCRYPTION_KEY; see docs/aws-deploy.md",
    });
    new cdk.CfnOutput(this, "RecordsBucketName", {
      value: recordsBucket.bucketName,
    });
    new cdk.CfnOutput(this, "BackupsBucketName", {
      value: backupsBucket.bucketName,
    });
    new cdk.CfnOutput(this, "AppBackupAlarmName", {
      value: appBackupAlarm.alarmName,
    });
    new cdk.CfnOutput(this, "GmailInboundPollAlarmName", {
      value: gmailInboundPollAlarm.alarmName,
    });
    new cdk.CfnOutput(this, "RootDiskAlarmName", {
      value: rootDiskAlarm.alarmName,
    });
    new cdk.CfnOutput(this, "EmailDiagnosticsDocumentName", {
      value: emailDiagnostics.name ?? "",
    });
    new cdk.CfnOutput(this, "ApplicationAssetHash", {
      value: applicationSource.assetHash,
    });
  }
}
