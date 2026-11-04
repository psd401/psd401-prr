// ABOUTME: Least-privilege Bedrock grant for the production runtime role.
// ABOUTME: An explicit allow-list of US cross-region inference profiles.

import * as iam from "aws-cdk-lib/aws-iam";

export const ADVISORY_BEDROCK_REGION = "us-west-2";
export const ADVISORY_BEDROCK_MODEL_ID = "us.amazon.nova-2-lite-v1:0";

/**
 * Every model the advisory settings may name. A model not listed here is an
 * AccessDenied in production, which the application records as a failed
 * run -- never a wrong proposal. Each profile routes only to US regions, so
 * request and record text stays in the United States. Keep this list in step
 * with docs/advisory-ai.md (a backend test checks the two agree).
 */
export const ADVISORY_BEDROCK_MODELS = [
  { profileId: "us.amazon.nova-2-lite-v1:0", foundationModelId: "amazon.nova-2-lite-v1:0" },
  { profileId: "us.anthropic.claude-opus-5-5", foundationModelId: "anthropic.claude-opus-5-5" },
  { profileId: "us.anthropic.claude-fable-5-1", foundationModelId: "anthropic.claude-fable-5-1" },
  { profileId: "us.anthropic.claude-sonnet-5-5", foundationModelId: "anthropic.claude-sonnet-5-5" },
  { profileId: "us.anthropic.claude-haiku-5-5", foundationModelId: "anthropic.claude-haiku-5-5" },
  { profileId: "us.openai.gpt-6.1-sol", foundationModelId: "openai.gpt-6.1-sol" },
  { profileId: "us.openai.gpt-6.1-terra", foundationModelId: "openai.gpt-6.1-terra" },
] as const;

const DESTINATION_REGIONS = ["us-east-1", "us-east-2", "us-west-2"] as const;

export function grantAdvisoryBedrockInference(
  role: iam.Role,
  account: string,
): void {
  for (const model of ADVISORY_BEDROCK_MODELS) {
    const profileArn =
      `arn:aws:bedrock:${ADVISORY_BEDROCK_REGION}:${account}:inference-profile/` +
      model.profileId;

    role.addToPolicy(
      new iam.PolicyStatement({
        actions: ["bedrock:InvokeModel"],
        resources: [profileArn],
      }),
    );
    role.addToPolicy(
      new iam.PolicyStatement({
        actions: ["bedrock:InvokeModel"],
        resources: DESTINATION_REGIONS.map(
          (region) =>
            `arn:aws:bedrock:${region}::foundation-model/${model.foundationModelId}`,
        ),
        conditions: {
          StringLike: { "bedrock:InferenceProfileArn": profileArn },
        },
      }),
    );
  }
}
