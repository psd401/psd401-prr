// ABOUTME: Bootstrap storage for shared, locked Terraform state.
// ABOUTME: This stack is separate because a backend cannot create itself.

import * as cdk from "aws-cdk-lib";
import * as s3 from "aws-cdk-lib/aws-s3";
import { type Construct } from "constructs";

import {
  acknowledgeGuard,
  GUARD_S3_BASELINE,
  SHARED_SUPPRESSION_REASONS,
  skipCheckov,
} from "./suppressions";


export class TerraformStateStack extends cdk.Stack {
  constructor(
    scope: Construct,
    id: string,
    props: cdk.StackProps,
  ) {
    super(scope, id, {
      ...props,
      stackName: "psd401-prr-terraform-state",
      description: "Shared, versioned Terraform state for PSD401 PRR infrastructure",
      terminationProtection: true,
    });

    // The district tag set is applied once at the app level (lib/app.ts).
    // Terraform state holds project, organization and role identifiers - no
    // record, no requester - so it must not be over-classified as student
    // PII. The higher priority is what makes this win over the app-level
    // default for the same key.
    cdk.Tags.of(this).add("psd:data-classification", "internal", {
      priority: 200,
    });
    cdk.Tags.of(this).add("Purpose", "terraform-state");

    const stateBucket = new s3.Bucket(this, "StateBucket", {
      bucketName: `psd401-prr-terraform-state-${this.account}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      versioned: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      autoDeleteObjects: false,
    });

    // Server access logging is off; the reason both tools print is in
    // SHARED_SUPPRESSION_REASONS, written once.
    cdk.Validations.of(stateBucket).acknowledge({
      id: "AwsSolutions::AwsSolutions-S1",
      reason: SHARED_SUPPRESSION_REASONS.terraformStateAccessLogging,
    });
    skipCheckov(stateBucket, [
      {
        id: "CKV_AWS_18",
        comment: SHARED_SUPPRESSION_REASONS.terraformStateAccessLogging,
      },
    ]);

    acknowledgeGuard(stateBucket, {
      ...GUARD_S3_BASELINE,
      s3_bucket_logging_enabled_check:
        SHARED_SUPPRESSION_REASONS.terraformStateAccessLogging,
    });

    new cdk.CfnOutput(this, "StateBucketName", {
      value: stateBucket.bucketName,
      description: "S3 bucket used by Terraform remote state backends",
    });
  }
}
