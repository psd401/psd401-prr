// ABOUTME: Reviewed suppressions shared by the two static IaC checkers, so a
// ABOUTME: finding both tools raise is justified once rather than twice.

import * as cdk from "aws-cdk-lib";
import { type IConstruct } from "constructs";

/**
 * Records a Checkov suppression on one synthesized resource.
 *
 * Checkov reads `Metadata.checkov.skip` on a CloudFormation resource, which
 * is the only per-resource channel available from CDK: the templates are
 * generated, so there is nowhere to put an inline `checkov:skip` comment. The
 * alternative - `skip-check` in a config file - would disable the rule for
 * every future resource too, which is the opposite of what a reviewed
 * exception should do.
 *
 * `comment` is the evidence. Checkov prints it, and CI diff-detects new
 * entries, so adding one is a human-review item.
 */
export function skipCheckov(
  construct: IConstruct,
  skips: readonly { readonly id: string; readonly comment: string }[],
): void {
  const resource = construct.node.defaultChild ?? construct;
  if (!cdk.CfnResource.isCfnResource(resource)) {
    throw new Error(
      `Cannot suppress Checkov on ${construct.node.path}: it has no ` +
        "CloudFormation resource to carry the metadata.",
    );
  }
  // addMetadata is a plain key assignment, so a second call would silently
  // discard the first call's reviewed evidence and quietly stop suppressing
  // what it covered. One call per resource, and say so if that is violated.
  if (resource.cfnOptions.metadata?.checkov !== undefined) {
    throw new Error(
      `${construct.node.path} already has Checkov suppressions; add to that ` +
        "one call rather than making a second, which would replace them.",
    );
  }
  resource.addMetadata("checkov", { skip: skips });
}

/**
 * Findings that cdk-nag and Checkov both raise about the same design
 * decision. The text is the evidence both tools print, written once.
 */
export const SHARED_SUPPRESSION_REASONS = {
  accessLogBucketSelfLogging:
    "This is the server access log bucket for the records and backups " +
    "buckets. Logging it to itself would log every log delivery in a loop, " +
    "and logging it to another bucket only moves the question one bucket " +
    "along. Writes to it come from S3 log delivery alone, granted by a " +
    "bucket policy scoped to this account; versioning keeps any overwrite " +
    "recoverable.",
  terraformStateAccessLogging:
    "Terraform state is written by a small, named set of roles, and the " +
    "bucket is versioned: every state version is retained, so an " +
    "unexpected change is visible in the version history. A server access " +
    "log bucket would add another unmonitored, unexpiring bucket holding " +
    "nothing the version history does not already show.",
  publicWebPorts:
    "Ports 80 and 443 are deliberately public: this host serves the public " +
    "records request form and the staff workspace. Every other port is " +
    "closed. Port 80 carries no application traffic - Caddy answers the ACME " +
    "HTTP-01 challenge and redirects everything else to HTTPS, and Django's " +
    "SECURE_SSL_REDIRECT is on by default outside DEBUG (config/settings.py) " +
    "- so a requester's submission is never accepted in plaintext. On 443, " +
    "the controls are the application's own authentication and rate limits.",
} as const;

/**
 * Acknowledges Control Tower Guard rules (CfnGuardValidator) on a construct.
 *
 * The Guard plugin reports a finding under its rule *name*, prefixed with the
 * plugin name, and that pair is what `acknowledge()` takes. Wrapped so a call
 * site lists rule names and reasons rather than repeating the prefix.
 */
export function acknowledgeGuard(
  construct: IConstruct,
  rules: Readonly<Record<string, string>>,
): void {
  for (const [rule, reason] of Object.entries(rules)) {
    cdk.Validations.of(construct).acknowledge({
      id: `cdk-validator-cfnguard::${rule}`,
      reason,
    });
  }
}

const NO_LIFECYCLE_RULE =
  "No bucket in this app has a lifecycle rule. The district's retention " +
  "schedule under RCW 40.14 is unconfirmed, so an expiration or " +
  "noncurrent-version rule would delete public records, their backups, " +
  "their access logs or Terraform state on a guess, and early destruction " +
  "is the legal exposure; storage cost is not. The hosting stack test " +
  "asserts no LifecycleConfiguration exists.";

/**
 * Control Tower Guard rules every S3 bucket in this app raises, with the
 * evidence for leaving each one as it is. The four buckets were designed to
 * one standard, so the reasons hold for all of them.
 */
export const GUARD_S3_BASELINE = {
  s3_version_lifecycle_policy_check: NO_LIFECYCLE_RULE,
  s3_lifecycle_policy_check: NO_LIFECYCLE_RULE,
  s3_event_notifications_enabled_check:
    "No system consumes S3 events from this bucket, so a notification " +
    "configuration would add a target with no subscriber and no control. " +
    "Changes stay visible in the bucket's version history and, for the " +
    "records and backups buckets, in their server access logs.",
  s3_bucket_object_lock_enabled_check:
    "Object Lock puts a retention period on every object, and in compliance " +
    "mode no one can shorten it. The retention schedule under RCW 40.14 is " +
    "unconfirmed, so any period chosen now is a guess that could block " +
    "lawful destruction. Versioning keeps every overwritten or deleted " +
    "version recoverable, and the runtime role can only add new backups, " +
    "never overwrite or delete one.",
  s3_bucket_default_encryption_kms_check:
    "Every object is encrypted at rest with SSE-S3 (AES-256). A KMS key " +
    "would add a key policy as a second authorization layer, but switching " +
    "is not a toggle: S3 server access log delivery cannot write to a " +
    "KMS-default bucket, and the runtime role's backup writes are " +
    "conditioned on s3:x-amz-server-side-encryption = AES256. Access is " +
    "limited by IAM and block-public-access.",
} as const;
