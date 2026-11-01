# TFLint configuration for the Google identity root.
#
# psd-dev-standards standards/08 names TFLint with the aws ruleset; this is
# the one Terraform root in this repository and it manages Google resources,
# so it gets the google ruleset instead. The bundled terraform ruleset covers
# the HashiCorp style and naming rules the same standard requires.
#
# CODEOWNERS covers this file: loosening a rule here is a human-review item.

plugin "terraform" {
  enabled = true
  preset  = "recommended"
}

plugin "google" {
  enabled = true
  version = "0.40.0"
  source  = "github.com/terraform-linters/tflint-ruleset-google"
}
