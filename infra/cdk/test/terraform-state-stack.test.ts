import { describe, test } from "bun:test";
import { App } from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";

import { TerraformStateStack } from "../lib/terraform-state-stack";


function stateTemplate(): Template {
  const app = new App();
  const stack = new TerraformStateStack(app, "psd401-prr-terraform-state", {
    env: {
      account: "338414773271",
      region: "us-west-2",
    },
  });
  return Template.fromStack(stack);
}


describe("shared Terraform state", () => {
  const template = stateTemplate();

  test("is private, encrypted, versioned, and retained", () => {
    template.resourceCountIs("AWS::S3::Bucket", 1);
    template.hasResource("AWS::S3::Bucket", {
      DeletionPolicy: "Retain",
      UpdateReplacePolicy: "Retain",
      Properties: {
        BucketName: "psd401-prr-terraform-state-338414773271",
        BucketEncryption: {
          ServerSideEncryptionConfiguration: [
            {
              ServerSideEncryptionByDefault: {
                SSEAlgorithm: "AES256",
              },
            },
          ],
        },
        VersioningConfiguration: { Status: "Enabled" },
        PublicAccessBlockConfiguration: {
          BlockPublicAcls: true,
          BlockPublicPolicy: true,
          IgnorePublicAcls: true,
          RestrictPublicBuckets: true,
        },
      },
    });
  });

  test("rejects non-TLS access", () => {
    template.hasResourceProperties("AWS::S3::BucketPolicy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: "s3:*",
            Effect: "Deny",
            Condition: {
              Bool: {
                "aws:SecureTransport": "false",
              },
            },
          }),
        ]),
      },
    });
  });

  test("does not add a DynamoDB lock table", () => {
    template.resourceCountIs("AWS::DynamoDB::Table", 0);
  });
});
