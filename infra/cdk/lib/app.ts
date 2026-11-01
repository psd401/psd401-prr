// ABOUTME: Assembles the CDK app: both stacks, the district tag set applied
// ABOUTME: mechanically at the app level, and the cdk-nag and Guard rule packs.

import {
  App,
  Tags,
  Validations,
  type IPolicyValidationContext,
  type IPolicyValidationPlugin,
  type PolicyValidationPluginReport,
} from "aws-cdk-lib";
import { CfnGuardValidator } from "@cdklabs/cdk-validator-cfnguard";
import { AwsSolutionsChecks } from "cdk-nag";

import { prodConfig } from "./config";
import { PrrHostingStack } from "./prr-hosting-stack";
import { TerraformStateStack } from "./terraform-state-stack";

/**
 * The district tag set from psd-dev-standards standards/08, District
 * defaults. Applied with `Tags.of(app)` so no resource can be added without
 * them, and so a reviewer reads one list instead of auditing per-stack calls.
 *
 * `psd:environment` is a single value because production is the sole deployed
 * target (docs/aws-deploy.md); the dev environment was decommissioned
 * 2026-10-13. It is read from `prodConfig` rather than written out, so a
 * second environment cannot be mis-tagged as prod.
 *
 * `psd:data-classification` differs per stack, so the value here is only a
 * fail-closed default that each stack overrides.
 */
export const PSD_DISTRICT_TAGS = {
  "psd:application": "psd401-prr",
  // From the config, not a literal: the two must not be able to disagree.
  "psd:environment": prodConfig.envName,
  "psd:owner": "krishagel",
  "psd:managed-by": "cdk",
  "psd:repo": "psd401/psd401-prr",
  // Overridden per stack. Present here so every resource carries the key even
  // if a future stack forgets; `student-pii` is the fail-closed default.
  "psd:data-classification": "student-pii",
} as const satisfies Record<string, string>;

/**
 * CfnGuardValidator, with every finding it reports marked as an error.
 *
 * The plugin reports a violation with no `severity`. CDK fails synthesis on
 * the plugin's `success` flag until any of its rules is acknowledged; from
 * then on it recomputes `success` from the remaining violations and counts
 * only `error` and `fatal` ones. So as soon as one Guard rule is
 * acknowledged, every other Guard finding stops failing synthesis and is only
 * printed. Marking each finding an error keeps the gate a gate. The name is
 * unchanged, because acknowledgment IDs are namespaced by it.
 */
class GatingCfnGuardValidator implements IPolicyValidationPlugin {
  private readonly inner = new CfnGuardValidator();
  readonly name = this.inner.name;
  readonly version = this.inner.version;
  readonly ruleIds = this.inner.ruleIds;

  validate(context: IPolicyValidationContext): PolicyValidationPluginReport {
    const report = this.inner.validate(context);
    return {
      ...report,
      violations: report.violations.map((violation) => ({
        ...violation,
        severity: violation.severity ?? "error",
      })),
    };
  }
}

/**
 * Builds the app exactly as `bin/prr.ts` deploys it, so the tag set and the
 * rule pack are testable rather than only observable in a real `cdk synth`.
 */
export function createPrrApp(): App {
  const app = new App();

  for (const [key, value] of Object.entries(PSD_DISTRICT_TAGS)) {
    Tags.of(app).add(key, value);
  }

  // psd-dev-standards standards/08, CDK rules: the cdk-nag v3 AwsSolutions
  // pack via Validations.of(app). v3 runs inside CDK's policy validation
  // framework, so an unacknowledged ERROR-level finding fails `cdk synth` -
  // including the credential-free synth in CI - rather than printing a
  // warning nobody reads. `verbose` puts each rule's explanation in the
  // failure, which is what the reader of a failing synth needs.
  //
  // Every acknowledgment lives next to the resource it concerns, with its
  // evidence, in lib/prr-hosting-stack.ts and lib/terraform-state-stack.ts.
  // Adding one is a human-review item: agents may propose, never merge.
  Validations.of(app).addPlugins(
    new AwsSolutionsChecks(app, { verbose: true }),
    // standards/08 pairs the pack with CfnGuardValidator: AWS Control Tower's
    // proactive-control Guard rules, a second rule set over the same
    // templates. Acknowledged per resource like cdk-nag's.
    new GatingCfnGuardValidator(),
  );

  new PrrHostingStack(app, "psd401-prr-prod", prodConfig);
  new TerraformStateStack(app, "psd401-prr-terraform-state", {
    env: {
      account: prodConfig.account,
      region: prodConfig.region,
    },
  });

  return app;
}
