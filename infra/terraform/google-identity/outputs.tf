output "project_id" {
  value = google_project.identity.project_id
}

output "group_reader_service_account" {
  value = google_service_account.group_reader.email
}

output "group_reader_unique_id" {
  value = google_service_account.group_reader.unique_id
}

output "gmail_service_account" {
  description = "Dedicated keyless Gmail transport service-account email."
  value       = google_service_account.gmail.email
}

output "gmail_service_account_unique_id" {
  description = "Numeric OAuth client ID used for Workspace domain-wide delegation."
  value       = google_service_account.gmail.unique_id
}

output "vault_service_account" {
  description = "Dedicated keyless Google Vault service-account email."
  value       = google_service_account.vault.email
}

output "vault_service_account_unique_id" {
  description = "Numeric OAuth client ID used for Vault domain-wide delegation."
  value       = google_service_account.vault.unique_id
}

output "oidc_client_secret_resource" {
  value = google_secret_manager_secret.oidc_client_secret.id
}

output "oidc_callback_urls" {
  value = {
    for name, hostname in local.application_hosts : name => "https://${hostname}/api/staff/auth/oidc/callback/"
  }
}

output "aws_runtime_workload_identity_providers" {
  description = "Production provider resource name."
  value = {
    for name, provider in google_iam_workload_identity_pool_provider.aws_runtime : name => provider.name
  }
}

output "aws_runtime_workload_identity_provider" {
  description = "Production provider compatibility alias."
  value       = try(google_iam_workload_identity_pool_provider.aws_runtime["prod"].name, null)
}
