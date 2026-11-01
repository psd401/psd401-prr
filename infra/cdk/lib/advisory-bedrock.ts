// ABOUTME: Least-privilege Bedrock grant for the production runtime role.
// ABOUTME: Scoped to the advisory model's US cross-region inference profile.

import * as iam from "aws-cdk-lib/aws-iam";

export const ADVISORY_BEDROCK_REGION = "us-west-2";
export const ADVISORY_BEDROCK_MODEL_ID = "us.amazon.nova-2-lite-v1:0";

const FOUNDATION_MODEL_ID = "amazon.nova-2-lite-v1:0";
const DESTINATION_REGIONS = ["us-east-1", "us-east-2", "us-west-2"] as const;

export function grantAdvisoryBedrockInference(
  role: iam.Role,
  account: string,
): void {
  const profileArn =
    `arn:aws:bedrock:${ADVISORY_BEDROCK_REGION}:${account}:inference-profile/` +
    ADVISORY_BEDROCK_MODEL_ID;

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
          `arn:aws:bedrock:${region}::foundation-model/${FOUNDATION_MODEL_ID}`,
      ),
      conditions: {
        StringLike: { "bedrock:InferenceProfileArn": profileArn },
      },
    }),
  );
}
