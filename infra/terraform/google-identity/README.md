# Google identity Terraform

This is one Terraform root and one shared Google identity project serving
production at `https://prr.psd401.net`, the only environment. The retired
district-hosted dev box's provider, grants, and callback were removed on
2026-10-13.

The project has one internal OAuth client, a keyless group-reader service
account, a separate keyless Gmail transport service account, the Google APIs
required for identity, Gmail, and AWS federation, and one empty OAuth
client-secret container. Terraform outputs the exact production callback URL
for registration on the OAuth client. A Workspace super administrator
assigns the group-reader service account the read-only **Groups Reader** role once; it is
never added to each application role group and never receives Groups Editor,
Groups Administrator, or domain-wide delegation. The Gmail identity receives
only the two Workspace domain-wide delegation scopes in the operator runbook.

The production AWS runtime has its own restricted provider in the shared
Workload Identity pool and may impersonate only the read-only group reader and
the dedicated `prr-gmail` identity. The Gmail account alone grants the exact
production runtime principal Service Account Token Creator so google-auth can
sign its domain-delegated Workspace JWT. `aws_runtimes` accepts only the
`prod` key; adding another runtime means changing that validation in review.

Terraform does **not** create service-account keys, OAuth client secret
versions, DNS records, TLS certificates, GitHub Workload Identity Federation,
or GitHub Actions deployment access. Developers run and test locally without
any of those things.

The `psd401-prr-terraform-state` CDK stack bootstraps the encrypted, versioned,
private S3 backend. Terraform uses S3-native conditional locking, so simultaneous
plans and applies cannot both write the shared state. Operators authenticate
with short-lived AWS IAM Identity Center sessions; no AWS credentials belong
in this root or its backend configuration.

Install Terraform 1.16.x. Stable, non-secret inputs—including the
project and exact production AWS role—are committed in
`identity.auto.tfvars`. This is deliberate: shared state alone is unsafe if two
operators supply different runtime allowlists. Do not create a local
`terraform.tfvars` for routine work.

The project's billing link is not managed here: Terraform ignores
`billing_account`, so the link stays as set in GCP. Billing IDs belong to
`psd-gcp-infra` (psd-dev-standards standard 14).
Then select an AWS profile with access to the state bucket:

```text
aws sso login --profile psd401-prr-prod
AWS_PROFILE=psd401-prr-prod terraform init
AWS_PROFILE=psd401-prr-prod terraform plan -out=production-identity.tfplan
AWS_PROFILE=psd401-prr-prod terraform show -no-color production-identity.tfplan
AWS_PROFILE=psd401-prr-prod terraform show -json production-identity.tfplan | jq -e '[.resource_changes[]? | select(.change.actions | index("delete"))] | length == 0'
AWS_PROFILE=psd401-prr-prod terraform apply production-identity.tfplan
```

Review the complete saved plan, run the `jq` gate, and apply exactly that plan
file. The gate fails on any delete or replace: routine work never destroys a
production identity resource. A destroy is its own reviewed change: replace
this gate with one that fails on any delete outside the approved addresses and
on any change to a `["prod"]` address, and record both gates in the PR. Treat the
plan as sensitive operator material and remove it from the protected workstation after the apply and evidence capture; `*.tfplan` is
gitignored.

Never use `-lock=false` during routine work. If a failed operation leaves a
lock behind, verify no other operator is running before using the exact lock ID
reported by Terraform with `terraform force-unlock`.

Use `AWS_PROFILE=psd401-prr-prod terraform output oidc_callback_urls` for the
redirect URIs. The Workspace operator also needs
`terraform output -raw gmail_service_account_unique_id` as the numeric OAuth client
ID for the Gmail delegation grant. Continue with
[`../../../docs/google-identity-bootstrap.md`](../../../docs/google-identity-bootstrap.md).
