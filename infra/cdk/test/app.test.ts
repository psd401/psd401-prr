// ABOUTME: Contract for the assembled CDK app: the district tag set on every
// ABOUTME: resource, and the cdk-nag and Guard rule packs that gate synthesis.

import { describe, expect, test } from "bun:test";
import { Validations, type Stack } from "aws-cdk-lib";
import { CfnSubnet } from "aws-cdk-lib/aws-ec2";
import { Bucket } from "aws-cdk-lib/aws-s3";
import { Template } from "aws-cdk-lib/assertions";
import { type IConstruct } from "constructs";

import { createPrrApp, PSD_DISTRICT_TAGS } from "../lib/app";

// psd-dev-standards standards/08, District defaults: "Tags on everything,
// applied mechanically: psd:application, psd:environment, psd:owner,
// psd:managed-by, psd:repo, psd:data-classification
// (public|internal|partner-pii|student-pii)."
const REQUIRED_TAG_KEYS = [
  "psd:application",
  "psd:environment",
  "psd:owner",
  "psd:managed-by",
  "psd:repo",
  "psd:data-classification",
] as const;

const DATA_CLASSIFICATIONS: readonly string[] = [
  "public",
  "internal",
  "partner-pii",
  "student-pii",
];

// Resource types that carry no `Tags` property in CloudFormation. Asserting
// tags on these would be asserting something CloudFormation cannot express.
const UNTAGGABLE = new Set([
  "AWS::EC2::SubnetRouteTableAssociation",
  "AWS::EC2::Route",
  "AWS::EC2::EIPAssociation",
  "AWS::EC2::SecurityGroupIngress",
  "AWS::EC2::SecurityGroupEgress",
  "AWS::IAM::InstanceProfile",
  "AWS::IAM::Policy",
  "AWS::S3::BucketPolicy",
  "AWS::SNS::TopicPolicy",
  "AWS::SNS::Subscription",
  "AWS::SSM::Document",
  "AWS::SecretsManager::SecretTargetAttachment",
  "AWS::SecretsManager::ResourcePolicy",
  "AWS::DLM::LifecyclePolicy",
  "AWS::CloudWatch::Alarm",
]);

// Resource types whose `Tags` cannot change in place. CloudFormation replaces
// an AWS::EC2::KeyPair when its tags change, and the instance names the key
// pair, so a tag edit here replaces the instance and its inline root volume
// (PostgreSQL and the audit trail). These keep the tags they were deployed
// with; the test below pins them.
const REPLACE_ON_TAG_CHANGE = new Set(["AWS::EC2::KeyPair"]);

// Synthesizing the whole app takes seconds, mostly in the hosting stack's
// user data and SSM documents. Every assertion below reads the same
// deterministic output, so synthesize once.
let cached: Record<string, Template> | undefined;

function templatesOf(): Record<string, Template> {
  return (cached ??= synthesize());
}

function synthesize(): Record<string, Template> {
  const app = createPrrApp();
  const assembly = app.synth({ validateOnSynthesis: false });
  const templates: Record<string, Template> = {};
  for (const artifact of assembly.stacks) {
    templates[artifact.stackName] = Template.fromJSON(artifact.template);
  }
  return templates;
}

function tagsOf(properties: unknown): Record<string, string> {
  const raw = (properties as { Tags?: unknown } | undefined)?.Tags;
  if (!Array.isArray(raw)) return {};
  const tags: Record<string, string> = {};
  for (const entry of raw) {
    const { Key, Value } = entry as { Key?: string; Value?: string };
    if (typeof Key === "string" && typeof Value === "string") {
      tags[Key] = Value;
    }
  }
  return tags;
}

describe("cdk-nag rule pack", () => {
  // The real gate. cdk-nag v3 runs as a CDK policy validation plugin, so an
  // ERROR-level finding that nobody acknowledged interrupts synthesis. CI
  // runs the same thing through `cdk synth`; running it here means a new
  // unjustified wildcard or a dropped control fails in `bun test` too,
  // seconds after the edit rather than minutes later on a pull request.
  // 30s, not bun's 5s default: a full synthesis with the rule pack running
  // took 5.4s on a cold CI runner, and a gate that fails on runner speed
  // teaches people to re-run rather than to read.
  test("synthesizes with no unacknowledged finding", () => {
    // `validateOnSynthesis: true` is the default, and stated anyway: this
    // assertion means nothing if validation is off, and the helper below
    // turns it off deliberately, so leaving it implicit here invites the
    // reader to wonder which one applies.
    expect(() =>
      createPrrApp().synth({ validateOnSynthesis: true }),
    ).not.toThrow();
  }, 30_000);

  // The test above would pass just as happily if the rule pack were never
  // registered, so this one proves it is: an unlogged bucket added to the
  // real app trips AwsSolutions-S1, and nothing acknowledges it. If
  // registration is removed from createPrrApp, synthesis stops throwing and
  // this fails.
  test("the app it builds really is gated on the pack", () => {
    const app = createPrrApp();
    const hosting = app.node.children.find(
      (child) => child.node.id === "psd401-prr-prod",
    );
    expect(hosting).toBeDefined();
    new Bucket(hosting as Stack, "UnacknowledgedNagProbe");
    expect(() => app.synth({ validateOnSynthesis: true })).toThrow();
  }, 30_000);

  // The same proof for the Control Tower Guard rules (CfnGuardValidator). The
  // probe is a subnet that hands out public addresses: CT.EC2.PR.11 rejects
  // it and no AwsSolutions rule looks at subnets, so only the Guard plugin
  // can make this synthesis throw.
  test("the app it builds really is gated on the Guard rules", () => {
    const app = createPrrApp();
    const hosting = app.node.children.find(
      (child) => child.node.id === "psd401-prr-prod",
    );
    expect(hosting).toBeDefined();
    new CfnSubnet(hosting as Stack, "UnacknowledgedGuardProbe", {
      vpcId: "vpc-00000000000000000",
      cidrBlock: "10.255.0.0/28",
      mapPublicIpOnLaunch: true,
    });
    expect(() => app.synth({ validateOnSynthesis: true })).toThrow();
  }, 30_000);

  // standards/08: "Suppressions use acknowledge({id, reason})" and "Every new
  // suppression is a mandatory human-review item." A suppression with an
  // empty or throwaway reason is the failure mode that makes the rule pack
  // decorative, so the reasons are asserted, not just their presence.
  test("every acknowledgment carries a substantive reason", () => {
    const app = createPrrApp();
    const acknowledged: { path: string; id: string; reason: string }[] = [];
    const walk = (node: IConstruct): void => {
      for (const entry of node.node.metadata) {
        if (entry.type !== Validations.ACKNOWLEDGED_RULES_METADATA_KEY) {
          continue;
        }
        // Each acknowledge() call adds its own metadata entry holding one
        // `<pack>::<ruleId>` to reason pair, so walk every entry and every
        // key within it.
        for (const [id, reason] of Object.entries(
          entry.data as Record<string, string>,
        )) {
          acknowledged.push({ path: node.node.path, id, reason });
        }
      }
      for (const child of node.node.children) walk(child);
    };
    walk(app);

    // aws-cdk-lib acknowledges its own template-linter findings on constructs
    // it builds; those reasons are the library's to write, not ours. Listing
    // them means a new library-authored suppression still shows up here.
    const LIBRARY_AUTHORED = ["CloudFormation-Validate::W9010"];
    const ours = acknowledged.filter(
      (rule) => !LIBRARY_AUTHORED.includes(rule.id),
    );

    expect(ours.length).toBeGreaterThan(0);
    for (const rule of ours) {
      // The pack-qualified form - `AwsSolutions::AwsSolutions-<rule>` or
      // `cdk-validator-cfnguard::<rule>` - is what a failing synth tells the
      // reader to write. A bare rule ID is treated as an annotation rule for
      // backwards compatibility and would stop working if that fallback is
      // ever dropped.
      expect(
        rule.id,
        `${rule.path} acknowledges a rule without naming its pack`,
      ).toMatch(/^(AwsSolutions::AwsSolutions-|cdk-validator-cfnguard::)\w/);
      // Long enough to state evidence rather than "not applicable".
      expect(
        rule.reason.length,
        `${rule.path} acknowledges ${rule.id} without stating why`,
      ).toBeGreaterThan(80);
    }
  });
});

describe("district tag set", () => {
  test("declares exactly the six district tag keys", () => {
    expect(Object.keys(PSD_DISTRICT_TAGS).sort()).toEqual(
      [...REQUIRED_TAG_KEYS].sort(),
    );
  });

  test("classifies data on the only three permitted values", () => {
    for (const template of Object.values(templatesOf())) {
      for (const resource of Object.values(
        template.toJSON().Resources as Record<
          string,
          { Type: string; Properties?: unknown }
        >,
      )) {
        const classification = tagsOf(resource.Properties)[
          "psd:data-classification"
        ];
        if (classification !== undefined) {
          expect(DATA_CLASSIFICATIONS).toContain(classification);
        }
      }
    }
    // The first test to build the memoized templates pays for the synthesis.
  }, 30_000);

  test("tags every taggable resource in both stacks", () => {
    const missing: string[] = [];
    for (const [stackName, template] of Object.entries(templatesOf())) {
      const resources = template.toJSON().Resources as Record<
        string,
        { Type: string; Properties?: unknown }
      >;
      for (const [logicalId, resource] of Object.entries(resources)) {
        if (UNTAGGABLE.has(resource.Type)) continue;
        if (REPLACE_ON_TAG_CHANGE.has(resource.Type)) continue;
        const tags = tagsOf(resource.Properties);
        for (const key of REQUIRED_TAG_KEYS) {
          if (!tags[key]) missing.push(`${stackName}/${logicalId} ${key}`);
        }
      }
    }
    expect(missing).toEqual([]);
  });

  // `cdk diff` against production caught this, not a test: the district tag
  // set changed the key pair's tags, which CloudFormation can only apply by
  // replacing the key pair, which changes the instance's KeyName, which
  // replaces the instance. These are the tags the live key pair carries
  // (read from the deployed stack and from EC2, 2026-10-19). Change them only
  // as part of a reviewed plan to replace the instance.
  test("keeps the key pair's deployed tags so the instance is not replaced", () => {
    const hosting = templatesOf()["psd401-prr-prod"];
    expect(hosting).toBeDefined();
    const keyPairs = Object.values(
      hosting!.toJSON().Resources as Record<
        string,
        { Type: string; Properties?: { Tags?: unknown } }
      >,
    ).filter((resource) => resource.Type === "AWS::EC2::KeyPair");
    expect(keyPairs).toHaveLength(1);
    expect(keyPairs[0]?.Properties?.Tags).toEqual([
      { Key: "Application", Value: "psd401-prr" },
      { Key: "Environment", Value: "prod" },
      { Key: "ManagedBy", Value: "cdk" },
    ]);
  });

  test("names this repository and the production environment", () => {
    const hosting = templatesOf()["psd401-prr-prod"];
    expect(hosting).toBeDefined();
    const resources = hosting!.toJSON().Resources as Record<
      string,
      { Properties?: unknown }
    >;
    const bucket = tagsOf(resources["RecordsBucket880EC200"]?.Properties);
    expect(bucket["psd:application"]).toBe("psd401-prr");
    expect(bucket["psd:environment"]).toBe("prod");
    expect(bucket["psd:repo"]).toBe("psd401/psd401-prr");
    expect(bucket["psd:managed-by"]).toBe("cdk");
    // The records bucket holds FERPA-covered student records.
    expect(bucket["psd:data-classification"]).toBe("student-pii");
  });

  // Terraform state holds project and organization identifiers, never a
  // record or a requester, so it must not be over-classified either.
  test("classifies Terraform state as internal, not student PII", () => {
    const state = templatesOf()["psd401-prr-terraform-state"];
    expect(state).toBeDefined();
    const resources = state!.toJSON().Resources as Record<
      string,
      { Properties?: unknown }
    >;
    const tags = tagsOf(resources["StateBucket446E0578"]?.Properties);
    expect(tags["psd:data-classification"]).toBe("internal");
  });
});
