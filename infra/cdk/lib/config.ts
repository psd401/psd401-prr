// ABOUTME: Typed per-environment configuration for the psd401-prr hosting
// ABOUTME: stack. Every value below is a non-secret, already-live AWS ID.

export interface PrrEnvConfig {
  readonly envName: "dev" | "prod";
  readonly account: string;
  readonly region: string;
  readonly vpcId: string;
  readonly vpcCidr: string;
  readonly availabilityZone: string;
  // Only prod pins to an existing Elastic IP allocation; dev has the stack
  // allocate and own its own EIP.
  readonly eipAllocationId?: string;
  readonly hostname: string;
  readonly instanceType: string;
  readonly volumeSizeGiB: number;
  // Who the CloudWatch alarms email, through one SNS topic. Each address must
  // confirm the subscription from AWS's email before any alarm reaches it.
  readonly alarmEmails: readonly string[];
}

// Ubuntu 24.04 LTS amd64 gp3 AMI, refreshed automatically via SSM parameter
// lookup at synth time. Refresh the cached value in cdk.context.json with
// `cdk context --reset "ssm:account=...:parameterName=<this path>..."` per
// the README's AMI refresh procedure (this replaces the instance).
export const UBUNTU_2404_AMD64_GP3_SSM_PARAM =
  "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id";

const shared = {
  account: "338414773271",
  region: "us-west-2",
  vpcId: "vpc-020d508e4786a5803",
  vpcCidr: "172.25.0.0/16",
  availabilityZone: "us-west-2a",
  // 8 GiB, not 4: ClamAV holds its signature database in memory and roughly
  // doubles that during a reload, alongside Postgres and the application.
  instanceType: "t3a.large",
  volumeSizeGiB: 50,
  // The district technology alerts mailbox (Hagel, 2026-10-13).
  alarmEmails: ["techalerts@psd401.net"],
} as const;

export const prodConfig: PrrEnvConfig = {
  ...shared,
  envName: "prod",
  // 54.68.97.81 — pinned: both public and district-internal DNS already
  // point here. Never create a second EIP for prod.
  eipAllocationId: "eipalloc-0f263927cd1983f48",
  hostname: "prr.psd401.net",
};
