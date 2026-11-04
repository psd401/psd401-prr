// ABOUTME: Finds the host's psd-aws-infra subnet by its standard 08 tags at
// ABOUTME: synth time, and fails synth unless exactly one subnet matches.

import * as cdk from "aws-cdk-lib";
import * as cxschema from "aws-cdk-lib/cloud-assembly-schema";
import type { Construct } from "constructs";

// psd-dev-standards 08 "Shared network": every shared network resource
// carries this tag, and an app finds it by the tag plus its stable Name.
export const SHARED_NETWORK_TAG = { key: "psd:shared", value: "network" };

export const SHARED_SUBNET_LOOKUP_PROVIDER =
  cxschema.ContextProvider.CC_API_PROVIDER;

// One row of the cached lookup: Cloud Control returns the subnet ID as the
// Identifier alongside the properties asked for.
export interface SharedSubnetCandidate {
  readonly Identifier: string;
  readonly AvailabilityZone: string;
  readonly Tags: ReadonlyArray<{ readonly Key: string; readonly Value: string }>;
}

export interface SharedSubnet {
  readonly subnetId: string;
  readonly availabilityZone: string;
}

/**
 * The Cloud Control query for every subnet in the VPC. Cloud Control matches
 * only scalar properties, so the tag filter runs here rather than in the CLI;
 * the CLI still refuses to cache an empty result.
 */
export function sharedSubnetQuery(vpcId: string) {
  return {
    typeName: "AWS::EC2::Subnet",
    propertyMatch: { VpcId: vpcId },
    propertiesToReturn: ["AvailabilityZone", "Tags"],
    expectedMatchCount: "at-least-one",
  };
}

export function lookupSharedSubnet(
  scope: Construct,
  vpcId: string,
  name: string,
): SharedSubnet {
  const wanted: Record<string, string> = {
    [SHARED_NETWORK_TAG.key]: SHARED_NETWORK_TAG.value,
    Name: name,
  };
  // Before the CLI has done the lookup, synth sees this placeholder; the CLI
  // then queries AWS, caches the answer in cdk.context.json, and synthesizes
  // again with it.
  const dummy: SharedSubnetCandidate = {
    Identifier: "subnet-00000000000000000",
    // Not Stack.availabilityZones, which would add a second lookup.
    AvailabilityZone: `${cdk.Stack.of(scope).region}a`,
    Tags: Object.entries(wanted).map(([Key, Value]) => ({ Key, Value })),
  };
  const candidates: SharedSubnetCandidate[] = cdk.ContextProvider.getValue(
    scope,
    {
      provider: SHARED_SUBNET_LOOKUP_PROVIDER,
      props: sharedSubnetQuery(vpcId),
      dummyValue: [dummy],
    },
  ).value;

  const matches = candidates.filter((subnet) =>
    Object.entries(wanted).every(([key, value]) =>
      (subnet.Tags ?? []).some((tag) => tag.Key === key && tag.Value === value),
    ),
  );
  if (matches.length !== 1) {
    throw new Error(
      `Expected exactly one subnet in ${vpcId} tagged ` +
        `${JSON.stringify(wanted)}, found ${matches.length}. The host holds ` +
        "student records and must stay in its own isolated subnet; fix the " +
        "tags in psd-aws-infra, then refresh the cached lookup with " +
        "`cdk context --reset`.",
    );
  }
  return {
    subnetId: matches[0].Identifier,
    availabilityZone: matches[0].AvailabilityZone,
  };
}
