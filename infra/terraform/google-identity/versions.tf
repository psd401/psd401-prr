terraform {
  required_version = "~> 1.16.0"

  backend "s3" {
    bucket       = "psd401-prr-terraform-state-338414773271"
    key          = "google-identity/terraform.tfstate"
    region       = "us-west-2"
    encrypt      = true
    use_lockfile = true
  }

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 7.0, < 8.0"
    }
  }
}
