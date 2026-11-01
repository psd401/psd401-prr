variable "project_id" {
  description = "Globally unique Google Cloud project ID for shared PRR identity."
  type        = string
}

variable "project_name" {
  description = "Human-readable project name."
  type        = string
  default     = "PSD401 PRR Identity"
}

variable "organization_id" {
  description = "PSD401 Google Cloud organization numeric ID."
  type        = string
}

variable "labels" {
  description = "Additional project labels."
  type        = map(string)
  default     = {}
}

variable "aws_runtimes" {
  description = "The production AWS runtime, the only one allowed to impersonate the dedicated group-reader, Gmail, and Vault identities without Google private keys."
  type = map(object({
    account_id = string
    role_name  = string
  }))
  default = {}

  validation {
    condition = alltrue([
      for name, runtime in var.aws_runtimes :
      contains(["prod"], name) &&
      can(regex("^[0-9]{12}$", runtime.account_id)) &&
      can(regex("^[A-Za-z0-9+=,.@_-]{1,64}$", runtime.role_name))
    ])
    error_message = "aws_runtimes may contain only the production AWS account/role pair."
  }
}
