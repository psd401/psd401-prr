// ABOUTME: Logical-ID pins for every stateful resource in both CDK stacks.
// ABOUTME: A changed ID means CloudFormation replacement, which means data loss.

import { describe, expect, test } from "bun:test";
import { App } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";

import { prodConfig } from "../lib/config";
import { PrrHostingStack } from "../lib/prr-hosting-stack";
import { TerraformStateStack } from "../lib/terraform-state-stack";

// psd-dev-standards standards/08, CDK rules: "Logical-ID pin tests on
// stateful resources. A changed logical ID means replacement, which means
// data loss. These tests also protect the Retain step of migration."
//
// Every entry below names a live production resource. CloudFormation keys
// resources by logical ID, so renaming a construct, moving it under a
// different parent, or splitting the stack produces a *new* resource and
// deletes the old one. For these resources that is the loss of public
// records, of the backups that would restore them, of the signing key that
// decrypts every stored district setting, or of the Terraform state that
// owns the Google identity foundation.
//
// If one of these assertions fails, the change is a replacement. Do not
// update the expected ID to match: change the code back, or get explicit
// human approval for a migration that preserves the data first.

interface StatefulResource {
  readonly logicalId: string;
  readonly type: string;
  /** Stateful production resources carry Retain on both paths. */
  readonly retained: boolean;
  readonly why: string;
}

const HOSTING_STATEFUL: readonly StatefulResource[] = [
  {
    logicalId: "RecordsBucket880EC200",
    type: "AWS::S3::Bucket",
    retained: true,
    why: "Holds every released record, production, and exemption log.",
  },
  {
    logicalId: "BackupsBucketC69911ED",
    type: "AWS::S3::Bucket",
    retained: true,
    why: "Holds the write-once PostgreSQL and audit-trail backups.",
  },
  {
    logicalId: "AccessLogsBucket83982689",
    type: "AWS::S3::Bucket",
    retained: true,
    why: "Holds the server access logs for the records and backups buckets.",
  },
  {
    logicalId: "RuntimeSecret",
    type: "AWS::SecretsManager::Secret",
    retained: true,
    why:
      "Holds DJANGO_SECRET_KEY, which is also the settings-store encryption " +
      "key. Replacing it strands every encrypted district setting.",
  },
  {
    logicalId: "InstanceC1063A87",
    type: "AWS::EC2::Instance",
    // Retain since the subnet handover (psd401/psd-aws-infra#42): the
    // instance leaves the stack and is imported back with the subnet as a
    // literal ID, because changing its SubnetId in place replaces it. Its root
    // volume is an inline block device, so a replacement would take the live
    // PostgreSQL database and the audit trail with it.
    retained: true,
    why: "Its inline root volume holds PostgreSQL and the audit trail.",
  },
  {
    logicalId: "RuntimeRoleFD8790A4",
    type: "AWS::IAM::Role",
    retained: false,
    why:
      "infra/terraform/google-identity pins this role by name; a replacement " +
      "breaks Google Workload Identity Federation for Gmail and Vault.",
  },
];

const STATE_STATEFUL: readonly StatefulResource[] = [
  {
    logicalId: "StateBucket446E0578",
    type: "AWS::S3::Bucket",
    retained: true,
    why: "Holds the Terraform state for the Google identity foundation.",
  },
];

// Synthesizing the hosting stack takes seconds - most of it building the
// user data and the SSM documents - and on a cold CI runner the first call
// alone ran past bun's 5s default timeout. Every assertion below reads the
// same deterministic output, so synthesize each stack once.
let hostingCache: Template | undefined;
let stateCache: Template | undefined;

function hostingTemplate(): Template {
  return (hostingCache ??= Template.fromStack(
    new PrrHostingStack(new App(), "psd401-prr-prod", prodConfig),
  ));
}

function stateTemplate(): Template {
  return (stateCache ??= Template.fromStack(
    new TerraformStateStack(new App(), "psd401-prr-terraform-state", {
      env: { account: prodConfig.account, region: prodConfig.region },
    }),
  ));
}

function assertPinned(
  template: Template,
  resource: StatefulResource,
): void {
  const resources = template.toJSON().Resources as Record<
    string,
    { Type: string; DeletionPolicy?: string; UpdateReplacePolicy?: string }
  >;
  const found = resources[resource.logicalId];
  expect(
    found,
    `Logical ID ${resource.logicalId} is gone. ${resource.why}`,
  ).toBeDefined();
  expect(found?.Type).toBe(resource.type);
  if (resource.retained) {
    expect(found?.DeletionPolicy).toBe("Retain");
    expect(found?.UpdateReplacePolicy).toBe("Retain");
  }
}

describe("stateful logical IDs are pinned", () => {
  test("the hosting stack keeps every stateful logical ID", () => {
    const template = hostingTemplate();
    for (const resource of HOSTING_STATEFUL) {
      assertPinned(template, resource);
    }
    // 30s, not bun's 5s default: this is the first synthesis in the file and
    // a cold CI runner took 5.4s for it alone.
  }, 30_000);

  test("the Terraform state stack keeps its bucket logical ID", () => {
    const template = stateTemplate();
    for (const resource of STATE_STATEFUL) {
      assertPinned(template, resource);
    }
  });

  // The pin above is only half the protection: a resource could keep its ID
  // and still be moved into a stack that is itself replaceable. Both stacks
  // hold stateful resources, so both must refuse a plain `cdk destroy`.
  test("both stacks keep termination protection", () => {
    const app = new App();
    const hosting = new PrrHostingStack(app, "psd401-prr-prod", prodConfig);
    const state = new TerraformStateStack(app, "psd401-prr-terraform-state", {
      env: { account: prodConfig.account, region: prodConfig.region },
    });
    expect(hosting.terminationProtection).toBe(true);
    expect(state.terminationProtection).toBe(true);
  });

  // A new stateful resource that nobody pinned is the failure this guard
  // exists to prevent. Any S3 bucket or Secrets Manager secret added to
  // either stack has to be added to the lists above in the same change.
  test("no stateful resource type escapes the pin lists", () => {
    // Every type that can hold data in these stacks. EC2::Instance is here
    // because the root volume is an inline block device, so a second
    // data-bearing host would otherwise slip past both this test and the
    // fixed-ID list above. IAM::Role deliberately is not: a role holds no
    // data, and RuntimeRole appears in the pin list for a different reason -
    // Terraform matches it by name.
    const statefulTypes = [
      "AWS::S3::Bucket",
      "AWS::SecretsManager::Secret",
      "AWS::EC2::Instance",
    ];
    const cases: readonly [Template, readonly StatefulResource[]][] = [
      [hostingTemplate(), HOSTING_STATEFUL],
      [stateTemplate(), STATE_STATEFUL],
    ];
    for (const [template, pinned] of cases) {
      const resources = template.toJSON().Resources as Record<
        string,
        { Type: string }
      >;
      const unpinned = Object.entries(resources)
        .filter(([id, res]) =>
          statefulTypes.includes(res.Type) &&
          !pinned.some((entry) => entry.logicalId === id),
        )
        .map(([id, res]) => `${id} (${res.Type})`);
      expect(
        unpinned,
        "Add these stateful resources to the pin list, with a reason.",
      ).toEqual([]);
    }
  });
});
