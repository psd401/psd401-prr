# ABOUTME: One Google project serving production OIDC/group/Gmail identity.
# ABOUTME: Production (prr.psd401.net) is the only federated AWS runtime.
resource "google_project" "identity" {
  # checkov:skip=CKV_GCP_27:auto_create_network is a create-time field on an
  # existing project, so setting it now would force project replacement -
  # which deletion_policy = PREVENT exists to block. This project runs no
  # compute: it holds service-account identities and one Secret Manager
  # container, and nothing is attached to the default network.
  # checkov:skip=CKV2_GCP_5:Admin Activity audit logs are always on and
  # cannot be disabled. Data Access logs for allServices would record every
  # group-membership read the production runtime makes - several per staff
  # sign-in - in a project with no log reader and no retention budget, and
  # the identity decisions those reads feed are already recorded in the
  # application's own append-only AuditLog.
  project_id      = var.project_id
  name            = var.project_name
  org_id          = var.organization_id
  deletion_policy = "PREVENT"
  labels = merge({
    application = "psd401-prr"
    purpose     = "staff-identity"
    managed_by  = "terraform"
  }, var.labels)

  # The billing link is environment record, owned outside this public repo
  # (psd-dev-standards standard 14: billing IDs live in psd-gcp-infra). It
  # stays exactly as linked in GCP; Terraform neither sets nor unlinks it.
  lifecycle {
    ignore_changes = [billing_account]
  }
}

provider "google" {
  project = var.project_id
}

locals {
  application_hosts = {
    prod = "prr.psd401.net"
  }
  aws_runtimes_enabled = length(var.aws_runtimes) > 0
}

resource "google_project_service" "cloud_identity" {
  project            = google_project.identity.project_id
  service            = "cloudidentity.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "gmail" {
  project            = google_project.identity.project_id
  service            = "gmail.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "vault" {
  project            = google_project.identity.project_id
  service            = "vault.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "iam" {
  project            = google_project.identity.project_id
  service            = "iam.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "iam_credentials" {
  project            = google_project.identity.project_id
  service            = "iamcredentials.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "security_token_service" {
  project            = google_project.identity.project_id
  service            = "sts.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "secret_manager" {
  project            = google_project.identity.project_id
  service            = "secretmanager.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "resource_manager" {
  project            = google_project.identity.project_id
  service            = "cloudresourcemanager.googleapis.com"
  disable_on_destroy = false
}

resource "google_service_account" "group_reader" {
  project      = google_project.identity.project_id
  account_id   = "prr-google-group-reader"
  display_name = "PRR Google group membership reader"
  description  = "Read-only identity used by PRR production to verify configured role-group membership."

  depends_on = [
    google_project_service.iam,
    google_project_service.iam_credentials,
    google_project_service.resource_manager,
  ]
}

resource "google_service_account" "gmail" {
  project      = google_project.identity.project_id
  account_id   = "prr-gmail"
  display_name = "PRR Gmail transport"
  description  = "Keyless identity used only for Gmail transport as the pinned public-records mailbox."

  depends_on = [
    google_project_service.gmail,
    google_project_service.iam,
    google_project_service.iam_credentials,
    google_project_service.resource_manager,
  ]
}

resource "google_service_account" "vault" {
  project      = google_project.identity.project_id
  account_id   = "prr-vault"
  display_name = "PRR Google Vault"
  description  = "Keyless identity used only for Google Vault matters, counts, holds, and exports."

  depends_on = [
    google_project_service.vault,
    google_project_service.iam,
    google_project_service.iam_credentials,
    google_project_service.resource_manager,
  ]
}

# The two AWS runtimes share one pool in the one Google project, but each gets
# a provider restricted to its exact account and role. Both may impersonate
# only the three application service accounts below. No Google private key is
# created for either identity.
resource "google_iam_workload_identity_pool" "aws_runtime" {
  # The pool is one optional shared object; providers below fan out per role.
  for_each = local.aws_runtimes_enabled ? toset(["shared"]) : toset([])

  project                   = google_project.identity.project_id
  workload_identity_pool_id = "prr-aws-runtime"
  display_name              = "PRR AWS runtimes"
  description               = "Federates the production PRR runtime role."

  depends_on = [
    google_project_service.iam,
    google_project_service.iam_credentials,
    google_project_service.resource_manager,
    google_project_service.security_token_service,
  ]
}

resource "google_iam_workload_identity_pool_provider" "aws_runtime" {
  # checkov:skip=CKV_GCP_125:The rule targets CI-platform OIDC providers and
  # looks for a repository-scoped subject condition. This is an AWS provider,
  # not an OIDC one, and no CI platform is federated here at all - the
  # contract test in apps/api/tests/test_google_identity_infrastructure.py
  # enforces that. It carries the equivalent control: attribute_condition
  # pins both the exact AWS account and the exact runtime role name, and
  # var.aws_runtimes is validated to the single production pair.
  for_each = var.aws_runtimes

  project                            = google_project.identity.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.aws_runtime["shared"].workload_identity_pool_id
  workload_identity_pool_provider_id = each.key == "prod" ? "prr-aws-provider" : "prr-${each.key}-aws-provider"
  display_name                       = "PRR ${upper(each.key)} AWS provider"
  description                        = "Accepts only the configured PRR ${each.key} AWS runtime role."
  attribute_mapping = {
    "google.subject"        = "assertion.arn"
    "attribute.aws_account" = "assertion.account"
    "attribute.aws_role"    = "assertion.arn.extract('assumed-role/{role_name}/')"
  }
  attribute_condition = "attribute.aws_account == '${each.value.account_id}' && attribute.aws_role == '${each.value.role_name}'"

  aws {
    account_id = each.value.account_id
  }

  depends_on = [google_project_service.security_token_service]
}

resource "google_service_account_iam_member" "aws_runtime_group_reader" {
  for_each = var.aws_runtimes

  service_account_id = google_service_account.group_reader.name
  role               = "roles/iam.workloadIdentityUser"
  member = format(
    "principalSet://iam.googleapis.com/projects/%s/locations/global/workloadIdentityPools/%s/attribute.aws_role/%s",
    google_project.identity.number,
    google_iam_workload_identity_pool.aws_runtime["shared"].workload_identity_pool_id,
    each.value.role_name,
  )
}

resource "google_service_account_iam_member" "aws_runtime_gmail" {
  for_each = var.aws_runtimes

  service_account_id = google_service_account.gmail.name
  role               = "roles/iam.workloadIdentityUser"
  member = format(
    "principalSet://iam.googleapis.com/projects/%s/locations/global/workloadIdentityPools/%s/attribute.aws_role/%s",
    google_project.identity.number,
    google_iam_workload_identity_pool.aws_runtime["shared"].workload_identity_pool_id,
    each.value.role_name,
  )
}

resource "google_service_account_iam_member" "aws_runtime_vault" {
  for_each = var.aws_runtimes

  service_account_id = google_service_account.vault.name
  role               = "roles/iam.workloadIdentityUser"
  member = format(
    "principalSet://iam.googleapis.com/projects/%s/locations/global/workloadIdentityPools/%s/attribute.aws_role/%s",
    google_project.identity.number,
    google_iam_workload_identity_pool.aws_runtime["shared"].workload_identity_pool_id,
    each.value.role_name,
  )
}

# Domain-wide delegation adds a Workspace subject to the service-account JWT.
# google-auth therefore calls signJwt, which Workload Identity User does not
# include. Token Creator is scoped to this dedicated Gmail account and the two
# exact AWS runtime-role principals; it is never granted on the group reader.
resource "google_service_account_iam_member" "aws_runtime_gmail_token_creator" {
  for_each = var.aws_runtimes

  service_account_id = google_service_account.gmail.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member = format(
    "principalSet://iam.googleapis.com/projects/%s/locations/global/workloadIdentityPools/%s/attribute.aws_role/%s",
    google_project.identity.number,
    google_iam_workload_identity_pool.aws_runtime["shared"].workload_identity_pool_id,
    each.value.role_name,
  )
}

resource "google_service_account_iam_member" "aws_runtime_vault_token_creator" {
  for_each = var.aws_runtimes

  service_account_id = google_service_account.vault.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member = format(
    "principalSet://iam.googleapis.com/projects/%s/locations/global/workloadIdentityPools/%s/attribute.aws_role/%s",
    google_project.identity.number,
    google_iam_workload_identity_pool.aws_runtime["shared"].workload_identity_pool_id,
    each.value.role_name,
  )
}

# Terraform owns the shared container and IAM only. The Google bootstrap puts
# the value directly in the approved runtime stores so it never appears in
# configuration, plan output, or Terraform state.
resource "google_secret_manager_secret" "oidc_client_secret" {
  project   = google_project.identity.project_id
  secret_id = "prr-google-oidc-client-secret"

  replication {
    auto {}
  }

  depends_on = [google_project_service.secret_manager]
}
